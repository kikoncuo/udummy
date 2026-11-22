"""Copilot engine: continuous transcription + the whole interface, on 127.0.0.1.

The native shell only captures system audio, takes screenshots and shows a web view. Everything
else lives here and in ui/, so it changes without rebuilding the signed app.

    POST /audio?sr=48000      raw mono s16le, sent by the shell about twice a second
    POST /shot                image/jpeg from the shell -> {"id": ...};  GET /shot/<id>
    POST /ask                 {"instruction": "", "shot_id": null, "model": null} -> {"id": n}
    POST /cancel | /clear | /reset
    GET  /state               snapshot for a (re)loaded page
    GET  /settings | POST /settings {"prompt": str|null (null = default), "on_top": bool, "hide_capture": bool, "model": id|null}
    POST /key {"key": "sk-or-..."} | {"delete": true}   -> keychain (never a file, never sent back)  |  POST /key/test {}
    GET  /events              server-sent events: status, segment, ask progress
    GET  / , /app.js , /style.css

Every request needs the run's token (?t=... or X-Copilot-Token) and a 127.0.0.1 Host header.
Audio, transcript, screenshots and answers are kept in this process's memory only.
The log gets one line per ask with sizes and timings, never content.
"""
from __future__ import annotations

import hmac
import json
import re
import os
import queue
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import numpy as np

import core

HERE = Path(__file__).resolve().parent
UI = HERE / "ui"
SR = core.SR
TOKEN = os.environ.get("COPILOT_TOKEN") or secrets.token_urlsafe(24)
# Measured 2026-11-12 (ctx_probe.py): first text p50 271-343 ms from 5 to 60 min of transcript on
# GPT-OSS 120B / Cerebras, so length is not what makes answers slow up to 60 min. 60 min is the
# largest size measured. There is no cap on listening: when more than 60 min are held verbatim, the
# oldest 15 min are folded into a running summary (one background model call) and the panel says so.
# Until that summary exists the full text keeps being sent: nothing is dropped silently.
MAX_TRANSCRIPT_S = float(os.environ.get("COPILOT_MAX_TRANSCRIPT_MIN", "60")) * 60
SUMMARY_BLOCK_S = float(os.environ.get("COPILOT_SUMMARY_BLOCK_MIN", "15")) * 60
SILENCE_RMS = float(os.environ.get("COPILOT_SILENCE_RMS", "0.002"))
FRAME = int(0.03 * SR)
SETTINGS_PATH = Path(os.environ.get("COPILOT_SETTINGS") or (core.HOME_DIR / "settings.json"))
LOGS = Path(os.environ.get("COPILOT_LOGS") or (core.HOME_DIR / "logs"))
PROMPT_MAX = 8000


class Settings:
    """The user's settings, the only thing of the copilot written to disk besides the log: the prompt he wrote (or none: the default),
    always on top, hidden from screen capture. Owner-only file, replaced atomically. Defaults: default prompt, on top, hidden."""
    DEFAULTS = {"prompt": None, "on_top": True, "hide_capture": True, "model": "auto_fast", "source": "audio", "hotkey": "opt_return", "hide_tip": True,
                "stt": None, "my_models": [], "models_dir": None}   # models_dir: None = the default folder                  # stt: None = choose on start (local model if any, else cloud if a key, else off)
    HOTKEYS = ("opt_return", "ctrl_opt_space", "opt_space", "cmd_shift_space", "f6", "none")   # the page maps them to key codes   # source: audio | screen | both     # model: None = automatic (as v2), else an OpenRouter id

    def __init__(self, path: Path):
        self.path = path; self.lock = threading.Lock(); self.values = dict(self.DEFAULTS)
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            self.values.update(self.check(d))
        except (OSError, ValueError):
            pass

    @staticmethod
    def check(d: dict) -> dict:
        if not isinstance(d, dict) or set(d) - set(Settings.DEFAULTS):
            raise ValueError("settings: unknown field")
        out = {}
        if "prompt" in d:
            p = d["prompt"]
            if p is not None and not isinstance(p, str):
                raise ValueError("settings: prompt")
            p = (p or "").strip()
            if len(p) > PROMPT_MAX:
                raise ValueError("settings: prompt too long")
            out["prompt"] = p or None                             # empty = the default
        if "model" in d:
            m = d["model"]
            if m is not None and (not isinstance(m, str) or not (m in ("auto_fast", "auto_cheap") or core.MODEL_ID_RE.fullmatch(m.strip())
                                                                 or re.fullmatch(r"local:[A-Za-z0-9_.-]{1,120}", m.strip()))):
                raise ValueError("settings: model")
            out["model"] = m.strip() if m else None
        if "stt" in d:
            v = d["stt"]
            ok = v is None or v == "off" or (isinstance(v, str) and (
                (v.startswith("local:") and re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", v[6:]) is not None) or
                (v.startswith("cloud:") and core.MODEL_ID_RE.fullmatch(v[6:]) is not None)))
            if not ok:
                raise ValueError("settings: stt")
            out["stt"] = v
        if "models_dir" in d:
            v = d["models_dir"]
            if v is not None and (not isinstance(v, str) or not v.strip() or len(v) > 500 or "\x00" in v):
                raise ValueError("settings: models_dir")
            out["models_dir"] = os.path.expanduser(v.strip()) if v else None
        if "my_models" in d:
            v = d["my_models"]
            if not isinstance(v, list) or len(v) > 40 or not all(isinstance(x, str) and core.MODEL_ID_RE.fullmatch(x) for x in v):
                raise ValueError("settings: my_models")
            out["my_models"] = list(dict.fromkeys(v))
        if "hotkey" in d:
            if d["hotkey"] not in Settings.HOTKEYS and not re.fullmatch(r"custom:\d{1,3}:((control|option|command|shift)(,(control|option|command|shift)){0,3})?", str(d["hotkey"])):
                raise ValueError("settings: hotkey")
            out["hotkey"] = d["hotkey"]
        if "hide_tip" in d:
            if not isinstance(d["hide_tip"], bool):
                raise ValueError("settings: hide_tip")
            out["hide_tip"] = d["hide_tip"]
        if "source" in d:
            if d["source"] not in ("audio", "screen", "both"):
                raise ValueError("settings: source")
            out["source"] = d["source"]
        for k in ("on_top", "hide_capture"):
            if k in d:
                if not isinstance(d[k], bool):
                    raise ValueError("settings: " + k)
                out[k] = d[k]
        return out

    def update(self, d: dict) -> dict:
        new = self.check(d)
        with self.lock:
            self.values.update(new)
            tmp = self.path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.values, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
            return self.view()

    def view(self) -> dict:
        v = dict(self.values)
        return {**v, "prompt_default": core.DEFAULT_PROMPT, "guard": core.GUARD, "custom": v["prompt"] is not None, "prompt_max": PROMPT_MAX,
                "picker": [{"id": m["id"], "label": m["label"], "vision": m["vision"], "prices": list(m["prices"]), "first_ms": m["first_ms"]} for m in core.PICKER],
                "fastest": core.FASTEST, "cheapest": core.CHEAPEST,
                "stt_active": APP.stt.spec if "APP" in globals() else None, "stt_ready": APP.stt.ready if "APP" in globals() else False,
                "stt_local": core.local_stt_models(), "stt_catalog": core.STT_CATALOG, "stt_cloud": core.STT_CLOUD,
                "local_models": core.local_models(), "llm_catalog": core.LLM_CATALOG, "models_dir_now": str(core.MODELS),
                "local_llm_loaded": core.LOCAL_LLM.name,
                "stt_downloads": dict(APP.downloads) if "APP" in globals() else {}, "stt_error": APP.stt_error if "APP" in globals() else "", "picker_date": core.PICKER_DATE, "key_source": core.key_source()}

    def route(self) -> str | None:
        """the route for asks when the panel's own model menu says 'automático'"""
        m = self.values.get("model") or "auto_fast"                # None (older settings) = the fastest, as automatic
        return core.ensure_route(core.FASTEST if m == "auto_fast" else core.CHEAPEST if m == "auto_cheap" else m)

    def system(self) -> str:
        if os.environ.get("COPILOT_SYSTEM_PROMPT"):                # an explicit override for tests wins, as in v2
            return core.SYSTEM_V2
        return (self.values["prompt"] or core.DEFAULT_PROMPT) + "\n" + core.GUARD


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S ") + msg, flush=True)


# ---- continuous transcription -------------------------------------------------------------------
class StreamTranscriber:
    """Audio in, text segments out. Cuts at pauses so words are not split; keeps only the audio
    that has not been transcribed yet (a few seconds)."""

    MIN_CHUNK_S = 5.0     # do not transcribe less than this unless a pause closes the utterance
    MAX_CHUNK_S = 20.0    # never wait longer than this for a pause
    PAUSE_S = 0.35

    def __init__(self, stt: core.Transcriber, on_segment):
        self.stt = stt
        self.on_segment = on_segment
        self.lock = threading.Lock()          # guards pending/segments
        self.work = threading.Lock()          # one transcription at a time, in order
        self.pending = np.zeros(0, dtype=np.float32)
        self.pending_t0 = 0.0                 # stream time (s) of pending[0]
        self.stream_s = 0.0                   # audio received since start/reset
        self.segments: list[dict] = []
        self.level = 0.0
        self.last_audio_at = 0.0
        self.stt_busy_s = 0.0
        self.speech_s = 0.0
        self.last_segment_at = 0.0
        self.summary = ""
        self.summary_until = 0.0              # stream time (s) up to which the summary stands in

    def feed(self, pcm: np.ndarray, sr: int) -> None:
        audio = core.resample(pcm, sr)
        with self.lock:
            self.pending = np.concatenate([self.pending, audio])
            self.stream_s += audio.size / SR
            self.level = float(np.sqrt(np.mean(audio[-SR // 2:] ** 2))) if audio.size else 0.0
            self.last_audio_at = time.monotonic()

    @staticmethod
    def _quiet(audio: np.ndarray) -> np.ndarray:
        n = audio.size // FRAME
        if n == 0:
            return np.zeros(0, dtype=bool)
        rms = np.sqrt(np.mean(audio[:n * FRAME].reshape(n, FRAME) ** 2, axis=1))
        return rms < SILENCE_RMS

    def _take(self, force: bool) -> tuple[np.ndarray, float] | None:
        """Choose what to transcribe now. Returns (audio, stream time of its start) or None."""
        with self.lock:
            audio = self.pending
            dur = audio.size / SR
            if dur < 0.3:
                return None
            quiet = self._quiet(audio)
            pause_frames = int(self.PAUSE_S / 0.03)
            if quiet.size and quiet.all():   # only silence: drop it, time still moves on
                if force or dur > 1.0:
                    self.pending_t0 += dur
                    self.pending = np.zeros(0, dtype=np.float32)
                return None
            cut = None
            if force:
                cut = audio.size
            else:
                tail_quiet = quiet.size >= pause_frames and quiet[-pause_frames:].all()
                if tail_quiet and dur >= 1.0:             # the speaker paused: transcribe all of it
                    cut = audio.size
                elif dur >= self.MIN_CHUNK_S:             # still talking: cut at the last pause, if any
                    run = 0
                    for i in range(quiet.size - 1, int(2.0 / 0.03), -1):
                        run = run + 1 if quiet[i] else 0
                        if run >= pause_frames:
                            cut = (i + run // 2) * FRAME
                            break
                    if cut is None and dur >= self.MAX_CHUNK_S:   # no pause in 20 s: quietest frame near the end
                        n = audio.size // FRAME
                        rms = np.sqrt(np.mean(audio[:n * FRAME].reshape(n, FRAME) ** 2, axis=1))
                        lo = max(0, n - int(5.0 / 0.03))
                        cut = (lo + int(np.argmin(rms[lo:]))) * FRAME
            if not cut:
                return None
            chunk, t0 = audio[:cut], self.pending_t0
            self.pending = audio[cut:]
            self.pending_t0 += cut / SR
            return chunk, t0

    def step(self, force: bool = False) -> float:
        """Transcribe one chunk if one is ready. Returns seconds spent transcribing."""
        with self.work:
            got = self._take(force)
            if got is None:
                return 0.0
            chunk, t0 = got
            voiced = ~self._quiet(chunk)
            if voiced.sum() * 0.03 < 0.25:      # a click or a breath: nothing to transcribe
                return 0.0
            first = int(np.argmax(voiced)) * FRAME    # trim leading silence, keep its time
            text, dt = self.stt.transcribe(chunk[max(0, first - FRAME * 5):])
            self.stt_busy_s += dt
            self.speech_s += float(voiced.sum()) * 0.03
            if len(text) >= 2:
                seg = {"t": round(t0 + first / SR, 1), "text": text}
                with self.lock:
                    self.segments.append(seg)
                    self.last_segment_at = time.monotonic()
                self.on_segment(seg)
            return dt

    def flush(self) -> float:
        """Transcribe everything not yet transcribed (called on Ask). Returns seconds spent."""
        total = 0.0
        while True:
            with self.lock:
                left = self.pending.size / SR
            if left < 0.3:
                return total
            before = left
            total += self.step(force=True)
            with self.lock:
                if self.pending.size / SR >= before:
                    return total

    def run_forever(self) -> None:
        while True:
            try:
                if self.step() == 0.0:
                    time.sleep(0.15)
            except Exception as exc:
                log(f"transcriber error: {type(exc).__name__}")
                time.sleep(1)

    def reset(self) -> None:
        with self.work, self.lock:
            self.pending = np.zeros(0, dtype=np.float32)
            self.pending_t0 = self.stream_s = self.stt_busy_s = self.speech_s = 0.0
            self.segments = []
            self.summary, self.summary_until, self.last_segment_at = "", 0.0, 0.0

    def window(self) -> tuple[list[dict], float, str]:
        """What goes to the model: (verbatim segments, seconds covered by the summary, summary)."""
        with self.lock:
            return [s for s in self.segments if s["t"] >= self.summary_until], self.summary_until, self.summary

    def summary_job(self) -> tuple[list[dict], float, str] | None:
        """The next block to fold into the summary, if more than MAX_TRANSCRIPT_S is held verbatim."""
        with self.lock:
            if self.stream_s - self.summary_until <= MAX_TRANSCRIPT_S:
                return None
            until = self.summary_until + SUMMARY_BLOCK_S
            return [s for s in self.segments if self.summary_until <= s["t"] < until], until, self.summary

    def set_summary(self, text: str, until: float) -> None:
        with self.lock:
            self.summary, self.summary_until = text, until


# ---- application state ----------------------------------------------------------------------------
class App:
    def __init__(self):
        self.stt = core.Transcriber()
        self.model: core.Model | None = None
        self.stream = StreamTranscriber(self.stt, lambda seg: self.emit({"type": "segment", **seg}))
        self.subscribers: list[queue.Queue] = []
        self.sub_lock = threading.Lock()
        self.history: list[dict] = []
        self.shots: dict[str, bytes] = {}
        self.ask_lock = threading.Lock()
        self.settings = Settings(SETTINGS_PATH)
        self.downloads: dict[str, dict] = {}     # repo -> {"state": "downloading" | "done" | "error", "mb": n, "error": ...}
        self.stt_error = ""
        self.cancel = threading.Event()
        self.next_id = 1
        self.phase = "idle"   # idle | transcribing | thinking | answering

    def stt_spec(self) -> str:
        """the speech-to-text backend to use now"""
        v = self.settings.values.get("stt")
        if v:
            return v
        local = core.local_stt_models()
        if local:
            return "local:" + local[0]["name"]
        return ("cloud:" + core.STT_CLOUD[0]["id"]) if core.key_source() != "none" else "off"

    def load_stt(self) -> None:
        """(re)load the backend in the background; the panel keeps working meanwhile"""
        def run():
            spec = "off" if self.settings.values.get("source") == "screen" else self.stt_spec()
            try:
                dt = self.stt.load(spec); self.stt_error = ""
                log(f"speech-to-text: {spec.split(':')[0]} ready in {dt:.1f} s")
            except Exception as exc:  # noqa: BLE001
                self.stt_error = str(exc)[:200]; log(f"speech-to-text: {spec.split(':')[0]} failed: {type(exc).__name__}")
            self.emit({"type": "settings_changed"})
        threading.Thread(target=run, daemon=True).start()

    def warm_local(self, name: str) -> None:
        """load a chosen local answer model now, in the background, so the first question does not pay the cold start"""
        def run():
            try:
                with core.LOCAL_LLM.lock:
                    if core.LOCAL_LLM.name != name:
                        dt = core.LOCAL_LLM.load(name); log(f"local answer model ready in {dt:.1f} s")
            except Exception as exc:  # noqa: BLE001
                log(f"local answer model failed to load: {type(exc).__name__}")
            self.emit({"type": "settings_changed"})
        threading.Thread(target=run, daemon=True).start()

    def download_stt(self, repo: str) -> None:
        """Hugging Face snapshot into MODELS/<repo with __>; progress = folder size against the catalogue's size"""
        name = core.stt_dir_name(repo); dest = core.MODELS / name
        self.downloads[repo] = {"state": "downloading", "mb": 0}
        approx = next((m["approx_mb"] for m in core.STT_CATALOG + core.LLM_CATALOG if m["repo"] == repo), None)
        self.downloads[repo]["approx_mb"] = approx

        def watch():
            while self.downloads.get(repo, {}).get("state") == "downloading":
                try:
                    self.downloads[repo]["mb"] = round(sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) / 1e6)
                except OSError:
                    pass
                self.emit({"type": "settings_changed"}); time.sleep(1.5)

        def run():
            try:
                from huggingface_hub import snapshot_download
                core.MODELS.mkdir(parents=True, exist_ok=True)
                snapshot_download(repo_id=repo, local_dir=str(dest))
                if not (dest / "config.json").exists():
                    raise RuntimeError("no es un modelo de voz compatible (falta config.json)")
                self.downloads[repo] = {"state": "done", "mb": round(sum(f.stat().st_size for f in dest.rglob("*") if f.is_file()) / 1e6)}
                log(f"speech model downloaded: {self.downloads[repo]['mb']} MB")
                if core.model_kind(dest) == "voice" and not self.settings.values.get("stt"):
                    self.load_stt()                          # first local voice model: start using it
            except Exception as exc:  # noqa: BLE001
                self.downloads[repo] = {"state": "error", "error": str(exc)[:160]}; log(f"speech model download failed: {type(exc).__name__}")
            self.emit({"type": "settings_changed"})
        threading.Thread(target=watch, daemon=True).start()
        threading.Thread(target=run, daemon=True).start()

    # -- events
    def emit(self, ev: dict) -> None:
        with self.sub_lock:
            for q in self.subscribers:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    pass

    def status(self) -> dict:
        s = self.stream
        with s.lock:
            words = sum(len(x["text"].split()) for x in s.segments)
            age = time.monotonic() - s.last_audio_at if s.last_audio_at else None
            seg_age = time.monotonic() - s.last_segment_at if s.last_segment_at else None
            return {"type": "status", "phase": self.phase, "listening_s": round(s.stream_s, 1),
                    "pending_s": round(s.pending.size / SR, 1), "level": round(s.level, 4),
                    "audio_age_s": None if age is None else round(age, 1), "segments": len(s.segments),
                    "words": words, "last_text_age_s": None if seg_age is None else round(seg_age),
                    "summarised_min": round(s.summary_until / 60),
                    "stt_busy_pct": round(100 * s.stt_busy_s / s.stream_s, 1) if s.stream_s > 5 else None,
                    "spend_usd": round(core.read_spend()["total_usd"], 4), "stop_at_usd": core.STOP_AT_USD}

    def snapshot(self) -> dict:
        with self.stream.lock:
            segs = list(self.stream.segments)
        return {"status": self.status(), "segments": segs, "history": self.history,
                "models": [{"key": k, "label": r["label"], "vision": r["vision"]} for k, r in core.ROUTES.items()],
                "defaults": {"text": core.DEFAULT_TEXT_MODEL, "vision": core.DEFAULT_VISION_MODEL},
                "max_transcript_min": MAX_TRANSCRIPT_S / 60, "summary": self.stream.summary}

    # -- one ask
    def ask(self, instruction: str, shot_id: str | None, model_key: str | None, entry: dict) -> None:
        t0 = time.monotonic()
        ms = lambda: round((time.monotonic() - t0) * 1000)  # noqa: E731
        aid = entry["id"]

        def ev(kind: str, **kw):
            self.emit({"type": kind, "ask_id": aid, "ms": ms(), **kw})

        try:
            self.phase = "transcribing"
            stt_s = self.stream.flush()                       # only the last few seconds are left
            segs, trimmed_s, summary = self.stream.window()
            if self.settings.values.get("source") == "screen":       # solo pantalla: nothing heard is sent, the screenshot is the question
                segs, trimmed_s, summary = [], 0.0, ""
            image = self.shots.get(shot_id) if shot_id else None
            entry.update(stt_ms=round(stt_s * 1000), heard=" ".join(s["text"] for s in segs)[-600:],
                         transcript_s=round(self.stream.stream_s), words=sum(len(s["text"].split()) for s in segs),
                         trimmed_min=round(trimmed_s / 60))
            ev("transcript", stt_ms=entry["stt_ms"], heard=entry["heard"], words=entry["words"],
               transcript_s=entry["transcript_s"], trimmed_min=entry["trimmed_min"])
            if not segs and image is None and not instruction.strip():
                raise RuntimeError("Todavía no he oído nada que transcribir. ¿Está sonando la llamada?")
            if self.settings.values.get("source") == "screen" and image is None:
                raise RuntimeError("En «solo pantalla» cada pregunta lleva una captura y esta no ha llegado.")
            route = model_key or self.settings.route() or (core.DEFAULT_VISION_MODEL if image is not None else core.DEFAULT_TEXT_MODEL)
            if route not in core.ROUTES:
                raise RuntimeError(f"Modelo desconocido: {route}")
            if image is not None and not core.ROUTES[route]["vision"]:
                route = core.DEFAULT_VISION_MODEL
            text = core.build_user_text(segs, instruction, trimmed_s, has_image=image is not None, summary=summary,
                                        screen_only=self.settings.values.get("source") == "screen")
            self.phase = "thinking"
            # A rate-limited provider must not cost the answer: try once more on another route.
            # The spare is another model, in case this provider is rate-limited.
            spare = core.ensure_route(next(p["id"] for p in sorted(core.PICKER, key=lambda p: p["first_ms"]) if "or:" + p["id"] != route))
            backups = [r for r in (spare,) if r != route
                       and (image is None or core.ROUTES[r]["vision"])]
            for attempt, name in enumerate([route] + backups[:1]):
                t_req = time.monotonic()
                try:
                    for piece in self.model.stream(name, "", image_png=image, user_text=text, system=self.settings.system()):
                        if self.cancel.is_set():
                            break
                        if not entry["answer"]:
                            entry.update(first_ms=ms(), llm_first_ms=round((time.monotonic() - t_req) * 1000))
                            self.phase = "answering"
                            ev("first", llm_first_ms=entry["llm_first_ms"])
                        entry["answer"] += piece
                        ev("delta", text=piece)
                    break
                except core.ProviderError as exc:
                    if entry["answer"]:      # text is already on screen: keep it and say it is incomplete
                        entry["note"] = "RESPUESTA INCOMPLETA: el proveedor la cortó"
                        entry["cut"] = True
                        ev("note", text=entry["note"])
                        log(f"ask id={aid} cut mid-answer on {name}")
                        break
                    if attempt == 0 and backups:   # nothing shown yet: one try on another model
                        entry["note"] = f"{core.ROUTES[name]['label']} falló; respondo con {core.ROUTES[backups[0]]['label']}"
                        ev("note", text=entry["note"])
                        log(f"ask id={aid} provider error on {name}, retrying on {backups[0]}")
                        continue
                    raise
            last = self.model.last
            entry.update(done_ms=ms(), model=last.get("label"), provider=last.get("provider"),
                         usd=round(last.get("usd") or 0, 6), cancelled=self.cancel.is_set(), finished=True)
            ev("done", **{k: entry.get(k) for k in ("first_ms", "llm_first_ms", "stt_ms", "done_ms", "model", "usd",
                                                     "cancelled", "note")})
        except Exception as exc:
            entry.update(error=str(exc)[:300], finished=True, done_ms=ms())
            ev("error", message=entry["error"])
        finally:
            self.phase = "idle"
            self.cancel.clear()
            self.ask_lock.release()
            log(f"ask id={aid} words={entry.get('words')} shot={'yes' if shot_id else 'no'} "
                f"instruction_chars={len(instruction)} stt_ms={entry.get('stt_ms')} first_ms={entry.get('first_ms')} "
                f"done_ms={entry.get('done_ms')} error={'yes' if entry.get('error') else 'no'}")

    def summarise_forever(self) -> None:
        """Folds the oldest part into a summary once more than MAX_TRANSCRIPT_S is held verbatim."""
        model = core.Model()   # its own connection, so a summary never delays an answer
        while True:
            time.sleep(5)
            job = self.stream.summary_job()
            if job is None:
                continue
            segs, until, previous = job
            body = core.build_user_text(segs)
            if previous:
                body = "Resumen previo:\n" + previous + "\n\n" + body
            try:
                text = "".join(model.stream(core.DEFAULT_TEXT_MODEL, "", user_text=body, system=core.SUMMARY_SYSTEM,
                                            max_tokens=700)).strip()
                if len(text) < 20:
                    raise RuntimeError("empty summary")
                self.stream.set_summary(text, until)
                log(f"summary now covers the first {until / 60:.0f} min ({len(text.split())} words)")
                self.emit({"type": "summary", "until_min": round(until / 60)})
            except Exception as exc:   # keep sending the full text and try again later
                log(f"summary failed: {type(exc).__name__}")
                time.sleep(30)

    def start_ask(self, body: dict) -> tuple[int, dict]:
        if not self.ask_lock.acquire(blocking=False):
            return 409, {"error": "busy"}
        entry = {"id": self.next_id, "instruction": str(body.get("instruction") or "")[:2000],
                 "shot_id": body.get("shot_id") or None, "answer": "", "at": time.strftime("%H:%M:%S"),
                 "finished": False}
        self.next_id += 1
        self.history.append(entry)
        del self.history[:-30]
        self.emit({"type": "ask", "entry": entry})
        threading.Thread(target=self.ask, args=(entry["instruction"], entry["shot_id"], body.get("model") or None, entry),
                         daemon=True).start()
        return 200, {"id": entry["id"]}

    def clear(self, everything: bool) -> None:
        self.cancel.set() if self.ask_lock.locked() else None
        self.history.clear()
        self.shots.clear()
        if everything:
            self.stream.reset()
        self.emit({"type": "cleared", "everything": everything})


APP = App()
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/style.css": ("style.css", "text/css; charset=utf-8")}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Copilot"

    def log_message(self, *a):  # no access log: URLs carry the token
        pass

    def _ok(self) -> bool:
        url = urlsplit(self.path)
        self.q = parse_qs(url.query)
        self.route = url.path
        host = (self.headers.get("Host") or "").split(":")[0]
        token = self.headers.get("X-Copilot-Token") or (self.q.get("t") or [""])[0]
        public = (self.command == "GET" and self.route in ("/app.js", "/style.css")) or \
                 (self.command == "POST" and self.route == "/show")   # code only, and "bring the panel back" (no data in or out)
        if host not in ("127.0.0.1", "localhost") or not (public or hmac.compare_digest(token, TOKEN)):
            self._send(403, {"error": "forbidden"})
            return False
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).hostname not in ("127.0.0.1", "localhost"):
            self._send(403, {"error": "forbidden"})
            return False
        return True

    def _send(self, code: int, obj=None, body: bytes | None = None, ctype: str = "application/json"):
        data = body if body is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self, limit: int) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("too large")
        return self.rfile.read(n)

    def do_GET(self):
        if not self._ok():
            return
        if self.route in STATIC:
            name, ctype = STATIC[self.route]
            return self._send(200, body=(UI / name).read_bytes(), ctype=ctype)
        if self.route == "/state":
            return self._send(200, APP.snapshot())
        if self.route == "/settings":
            return self._send(200, APP.settings.view())
        if self.route == "/llm/search":
            q = (self.q.get("q") or [""])[0].strip().lower()[:60]
            return self._send(200, {"models": search_openrouter(q)})
        if self.route.startswith("/shot/"):
            img = APP.shots.get(self.route[6:])
            return self._send(200, body=img, ctype="image/jpeg") if img else self._send(404, {"error": "gone"})
        if self.route == "/events":
            return self._events()
        self._send(404, {"error": "not found"})

    def _events(self):
        q: queue.Queue = queue.Queue(maxsize=2000)
        with APP.sub_lock:
            APP.subscribers.append(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            while True:
                try:
                    ev = q.get(timeout=0.5)
                except queue.Empty:
                    ev = APP.status()
                self.wfile.write(b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            with APP.sub_lock:
                if q in APP.subscribers:
                    APP.subscribers.remove(q)

    def do_POST(self):
        if not self._ok():
            return
        try:
            if self.route == "/audio":
                raw = self._body(48_000 * 2 * 60)
                sr = int((self.q.get("sr") or ["48000"])[0])
                APP.stream.feed(np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0, sr)
                return self._send(200, {"ok": True})
            if self.route == "/shot":
                img = self._body(12_000_000)
                if img[:2] != b"\xff\xd8":
                    return self._send(400, {"error": "jpeg expected"})
                sid = secrets.token_hex(6)
                APP.shots[sid] = img
                for old in list(APP.shots)[:-20]:
                    del APP.shots[old]
                return self._send(200, {"id": sid, "bytes": len(img)})
            if self.route == "/show":                           # the launcher icon, clicked while the copilot is open
                APP.emit({"type": "show"})
                return self._send(200, {"ok": True})
            body = json.loads(self._body(100_000) or b"{}")
            if self.route == "/ask":
                code, out = APP.start_ask(body)
                return self._send(code, out)
            if self.route == "/cancel":
                APP.cancel.set() if APP.ask_lock.locked() else None
                return self._send(200, {"ok": True})
            if self.route == "/clear":
                APP.clear(everything=False)
                return self._send(200, {"ok": True})
            if self.route == "/reset":
                APP.clear(everything=True)
                return self._send(200, {"ok": True})
            if self.route == "/settings":
                try:
                    out = APP.settings.update(body)
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                log("settings saved: prompt " + ("custom" if out["custom"] else "default") + f", on_top {out['on_top']}, hide_capture {out['hide_capture']}, model {out['model'] or 'auto'}")   # no prompt text in the log
                if "models_dir" in body:
                    r = core.set_models_dir(out["models_dir"] or str(core.HOME_DIR / "models"), move=body.get("move", True) is not False)
                    log(f"models folder changed, {len(r['moved'])} moved")
                    out = APP.settings.view()
                if "stt" in body or "source" in body or "models_dir" in body:
                    APP.load_stt()
                if str(out.get("model") or "").startswith("local:"):
                    APP.warm_local(out["model"][6:])
                return self._send(200, out)
            if self.route in ("/stt/download", "/models/download"):
                repo = str(body.get("repo") or "").strip()
                if not core.STT_REPO_RE.fullmatch(repo):
                    return self._send(400, {"error": "Id de Hugging Face no válido (forma: usuario/modelo)."})
                if APP.downloads.get(repo, {}).get("state") == "downloading":
                    return self._send(409, {"error": "Ya se está descargando."})
                APP.download_stt(repo)
                return self._send(200, {"ok": True})
            if self.route in ("/stt/delete", "/models/delete"):
                import shutil
                name = str(body.get("name") or "")
                path = (core.MODELS / name).resolve()
                if not name or path.parent != core.MODELS.resolve() or not path.is_dir():
                    return self._send(400, {"error": "No existe ese modelo."})
                if APP.stt.spec == "local:" + name or APP.settings.values.get("model") == "local:" + name:
                    return self._send(409, {"error": "Es el que está en uso: elige otro antes de borrarlo."})
                if core.LOCAL_LLM.name == name:
                    with core.LOCAL_LLM.lock:
                        core.LOCAL_LLM.name = core.LOCAL_LLM.model = core.LOCAL_LLM.proc = None
                shutil.rmtree(path)
                return self._send(200, {"ok": True})
            if self.route == "/key":
                try:
                    if body.get("delete") is True:
                        core.delete_key(); log("key removed from the keychain")
                    else:
                        core.store_key(str(body.get("key") or "")); log("key saved in the keychain")   # never the key
                except ValueError as exc:
                    return self._send(400, {"error": str(exc)})
                except RuntimeError as exc:
                    return self._send(500, {"error": str(exc)})
                try:
                    APP.model.reload_key()
                except RuntimeError:
                    pass                                          # no key left: the next ask says so
                return self._send(200, {"ok": True, "key_source": core.key_source()})
            if self.route == "/diag":                            # what the app itself reports (flags only, no content), for diagnosis
                safe = {k: body.get(k) for k in ("what", "shell", "screen_allowed", "tap_ok", "error") if isinstance(body.get(k), (str, bool, int)) and len(str(body.get(k))) < 40}
                log("diag " + " ".join(f"{k}={v}" for k, v in safe.items()))
                return self._send(200, {"ok": True})
            if self.route == "/restart":                         # the panel's restart button: a new run.sh in its own session closes us
                import subprocess
                LOGS.mkdir(parents=True, exist_ok=True); logf = open(LOGS / "launcher.log", "a")
                subprocess.Popen(["/bin/zsh", str(HERE / "run.sh"), "--restart"], stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                                 start_new_session=True, cwd=str(HERE))
                log("restart requested from the panel")
                return self._send(200, {"ok": True})
            if self.route == "/key/test":
                try:
                    r = core.test_key(core.load_key())
                except RuntimeError as exc:
                    return self._send(200, {"ok": False, "error": str(exc)})
                except OSError:
                    return self._send(200, {"ok": False, "error": "Sin conexión con OpenRouter."})
                log(f"key test: {'ok' if r.get('ok') else 'refused'}")
                return self._send(200, {**r, "key_source": core.key_source()})
            self._send(404, {"error": "not found"})
        except ValueError:
            self._send(413, {"error": "too large"})
        except Exception as exc:
            log(f"request failed: {type(exc).__name__}")
            self._send(500, {"error": type(exc).__name__})


_OR_CACHE: dict = {"at": 0.0, "models": []}


def search_openrouter(q: str) -> list[dict]:
    """OpenRouter's public model list (no key needed), cached for an hour; the 30 best matches for q, newest first."""
    import urllib.request
    if time.time() - _OR_CACHE["at"] > 3600:
        try:
            with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=15) as r:
                _OR_CACHE["models"] = json.loads(r.read()).get("data", []); _OR_CACHE["at"] = time.time()
        except OSError:
            pass
    out = []
    for m in sorted(_OR_CACHE["models"], key=lambda m: m.get("created", 0), reverse=True):
        mid = m.get("id", "")
        if ":" in mid or (q and q not in mid.lower() and q not in (m.get("name") or "").lower()):
            continue
        p = m.get("pricing") or {}
        try:
            prices = [round(float(p.get("prompt", 0)) * 1e6, 3), round(float(p.get("completion", 0)) * 1e6, 3)]
        except ValueError:
            prices = [None, None]
        out.append({"id": mid, "name": m.get("name", mid), "prices": prices,
                    "vision": "image" in ((m.get("architecture") or {}).get("input_modalities") or [])})
        if len(out) >= 30:
            break
    return out


def main() -> int:
    if APP.settings.values.get("models_dir"):
        core.set_models_dir(APP.settings.values["models_dir"], move=False)
    APP.model = core.Model()
    APP.stt.model = APP.model
    log(f"OpenRouter connection open in {APP.model.warm() * 1000:.0f} ms" if core.key_source() != "none" else "no OpenRouter key yet")
    APP.load_stt()
    if str(APP.settings.values.get("model") or "").startswith("local:"):
        APP.warm_local(APP.settings.values["model"][6:])

    def keep_warm():
        while True:
            time.sleep(20)
            if not APP.ask_lock.locked():
                APP.model.keepalive()

    threading.Thread(target=keep_warm, daemon=True).start()
    threading.Thread(target=APP.stream.run_forever, daemon=True).start()
    threading.Thread(target=APP.summarise_forever, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", int(os.environ.get("COPILOT_PORT", "0"))), Handler)
    srv.daemon_threads = True
    log(f"COPILOT READY http://127.0.0.1:{srv.server_address[1]}")   # the token is never logged
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
