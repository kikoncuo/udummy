#!/usr/bin/env python3
"""Headless tests of the copilot's Ajustes (engine side): the settings file, /settings, and that the prompt he saves is the one the
model gets (a stand-in model records it: no network, no cost). No window, no audio, no capture.
    <venv>/bin/python -B test_settings.py"""
import json, os, stat, sys, tempfile, threading, time, unittest, urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

D = tempfile.mkdtemp(prefix="copilot-v3-test-")
os.environ["COPILOT_HOME"] = D; os.environ["COPILOT_MODELS_DIR"] = os.path.join(D, "models"); os.environ["COPILOT_SETTINGS"] = os.path.join(D, "settings.json"); os.environ["COPILOT_TOKEN"] = "test-token"; os.environ.pop("COPILOT_SYSTEM_PROMPT", None)
os.environ["COPILOT_KEY_SERVICE"] = "copilot-test-" + os.urandom(4).hex()   # a throwaway keychain item, deleted at the end; never the real one
os.environ.pop("OPENROUTER_API_KEY", None)
FAKE_KEY = "sk-or-v1-" + "0" * 40                                             # made up: OpenRouter refuses it
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core, engine  # noqa: E402


class FakeModel:
    def __init__(self): self.systems = []; self.last = {"label": "stand-in", "provider": "none", "usd": 0}
    def stream(self, name, _u, image_png=None, user_text="", system="", **kw):
        self.systems.append(system); self.routes.append(name); yield "respuesta de prueba"
    routes = []
    def reload_key(self): pass


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Path(os.environ["COPILOT_SETTINGS"]).unlink(missing_ok=True); engine.APP.settings = engine.Settings(Path(os.environ["COPILOT_SETTINGS"]))   # defaults
        engine.APP.model = cls.fake = FakeModel()
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), engine.Handler); cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown(); core.delete_key()                              # the throwaway keychain item

    def call(self, path, body=None, token="test-token"):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}?t={token}", data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{self.port}"}, method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def ask_system(self):
        n = len(self.fake.systems); code, out = self.call("/ask", {"instruction": "en tres puntos"}); self.assertEqual(code, 200)   # an instruction: no transcript is needed
        for _ in range(100):
            if len(self.fake.systems) > n and not engine.APP.ask_lock.locked():
                return self.fake.systems[-1]
            time.sleep(0.05)
        self.fail("no ask reached the model")


class Settings(Base):

    def test_1_defaults_and_the_v2_prompt_unchanged(self):
        code, v = self.call("/settings")
        self.assertEqual(code, 200); self.assertEqual((v["prompt"], v["on_top"], v["hide_capture"], v["custom"]), (None, True, True, False))
        self.assertEqual(v["prompt_default"] + "\n" + v["guard"], core.SYSTEM_V2)
        self.assertEqual(self.ask_system(), core.SYSTEM_V2)                 # the default prompt is exactly v2's

    def test_2_his_prompt_is_saved_used_and_restored(self):
        mine = "Eres mi copiloto. Responde siempre en tres viñetas y en inglés."
        code, v = self.call("/settings", {"prompt": "  " + mine + "\n"})
        self.assertEqual((code, v["prompt"], v["custom"]), (200, mine, True))
        self.assertEqual(self.ask_system(), mine + "\n" + core.GUARD)       # his text, and the guard line always after it
        f = Path(os.environ["COPILOT_SETTINGS"]); self.assertEqual(json.loads(f.read_text())["prompt"], mine)
        self.assertEqual(stat.S_IMODE(f.stat().st_mode), 0o600)
        self.assertEqual(engine.Settings(f).values["prompt"], mine)          # survives a restart of the engine
        code, v = self.call("/settings", {"prompt": None}); self.assertEqual((v["prompt"], v["custom"]), (None, False))
        self.assertEqual(self.ask_system(), core.SYSTEM_V2)
        code, v = self.call("/settings", {"prompt": "   "}); self.assertEqual(v["prompt"], None)   # empty = default

    def test_3_switches_saved_and_kept_apart(self):
        code, v = self.call("/settings", {"on_top": False}); self.assertEqual((v["on_top"], v["hide_capture"]), (False, True))
        code, v = self.call("/settings", {"hide_capture": False}); self.assertEqual((v["on_top"], v["hide_capture"]), (False, False))
        self.assertEqual(engine.Settings(Path(os.environ["COPILOT_SETTINGS"])).values, {"prompt": None, "on_top": False, "hide_capture": False, "model": "auto_fast", "source": "audio", "hotkey": "opt_return", "hide_tip": True, "stt": None, "my_models": [], "models_dir": None})
        self.call("/settings", {"on_top": True, "hide_capture": True})

    def test_4_refusals_change_nothing(self):
        before = Path(os.environ["COPILOT_SETTINGS"]).read_text() if Path(os.environ["COPILOT_SETTINGS"]).exists() else None
        for bad in ({"prompt": 3}, {"on_top": "yes"}, {"hide_capture": 1}, {"other": True}, {"prompt": "x" * (engine.PROMPT_MAX + 1)}):
            code, out = self.call("/settings", bad); self.assertEqual(code, 400, bad)
        self.assertEqual(self.call("/settings", token="wrong")[0], 403)
        after = Path(os.environ["COPILOT_SETTINGS"]).read_text() if Path(os.environ["COPILOT_SETTINGS"]).exists() else None
        self.assertEqual(before, after)

    def test_5_a_broken_settings_file_falls_back_to_defaults(self):
        p = Path(D) / "broken.json"; p.write_text("{not json"); self.assertEqual(engine.Settings(p).values, engine.Settings.DEFAULTS)
        p.write_text(json.dumps({"prompt": 5})); self.assertEqual(engine.Settings(p).values, engine.Settings.DEFAULTS)

    def test_6_the_log_never_holds_the_prompt(self):
        secret, lines, real = "PALABRA-QUE-NO-DEBE-SALIR-EN-EL-LOG", [], engine.log
        engine.log = lambda m: lines.append(m)
        try:
            self.call("/settings", {"prompt": secret}); self.call("/settings", {"prompt": None})
        finally:
            engine.log = real
        self.assertEqual(len(lines), 2); self.assertTrue(all(secret not in l for l in lines)); self.assertIn("prompt custom", lines[0])


class Source(Base):
    def test_9_screen_only_sends_no_transcript_and_needs_a_screenshot(self):
        self.user_texts = []
        real = self.fake.stream
        def grab(name, _u, image_png=None, user_text="", system="", **kw):
            self.user_texts.append((user_text, image_png is not None)); yield from real(name, _u, image_png=image_png, user_text=user_text, system=system)
        self.fake.stream = grab
        try:
            engine.APP.stream.on_segment if False else None
            engine.APP.stream.segments.append({"t": 1.0, "text": "FRASE-OIDA-QUE-NO-DEBE-IR"}) if hasattr(engine.APP.stream, "segments") else None
            self.assertEqual(self.call("/settings", {"source": "nope"})[0], 400)
            self.assertEqual(self.call("/settings", {"hotkey": "cmd_q"})[0], 400); self.assertEqual(self.call("/settings", {"hide_tip": "no"})[0], 400)
            self.assertEqual(self.call("/settings", {"hotkey": "none", "hide_tip": False})[1]["hotkey"], "none"); self.call("/settings", {"hotkey": "opt_return", "hide_tip": True})
            code, v = self.call("/settings", {"source": "screen"}); self.assertEqual(v["source"], "screen")
            n = len(self.user_texts); code, out = self.call("/ask", {"instruction": "x"})
            for _ in range(100):
                if not engine.APP.ask_lock.locked(): break
                time.sleep(0.05)
            self.assertEqual(len(self.user_texts), n)                                                         # refused before the model
            self.assertIn("captura", engine.APP.history[-1].get("error", ""))
            engine.APP.shots["s1"] = b"\xff\xd8fakejpeg"
            code, out = self.call("/ask", {"instruction": "solo la letra", "shot_id": "s1"})
            for _ in range(100):
                if len(self.user_texts) > n and not engine.APP.ask_lock.locked(): break
                time.sleep(0.05)
            text, img = self.user_texts[-1]
            self.assertTrue(img); self.assertIn("No escucho el audio", text); self.assertIn("solo la letra", text); self.assertNotIn("FRASE-OIDA", text)
        finally:
            self.fake.stream = real; self.call("/settings", {"source": "audio"})


class LocalModels(Base):
    def test_10_folder_move_kinds_local_route_and_refusals(self):
        old = core.MODELS; old.mkdir(parents=True, exist_ok=True)
        (old / "mlx-community__parakeet-x").mkdir(exist_ok=True); (old / "mlx-community__parakeet-x" / "config.json").write_text('{"model_type": "parakeet"}')
        (old / "acme__tiny-vl").mkdir(exist_ok=True); (old / "acme__tiny-vl" / "config.json").write_text('{"model_type": "qwen3_5", "vision_config": {}}')
        v = self.call("/settings")[1]
        kinds = {m["name"]: m["kind"] for m in v["local_models"]}
        self.assertEqual(kinds, {"mlx-community__parakeet-x": "voice", "acme__tiny-vl": "vision"})
        self.assertEqual([m["name"] for m in v["stt_local"]], ["mlx-community__parakeet-x"])
        new = os.path.join(D, "elsewhere", "models")
        code, v = self.call("/settings", {"models_dir": new}); self.assertEqual(code, 200)
        self.assertEqual(v["models_dir_now"], str(Path(new).resolve())); self.assertTrue((Path(new) / "acme__tiny-vl" / "config.json").exists())
        self.assertFalse((old / "acme__tiny-vl").exists())                                                   # moved, not copied
        key = core.ensure_route("local:acme__tiny-vl"); self.assertEqual((core.ROUTES[key]["local"], core.ROUTES[key]["vision"]), ("acme__tiny-vl", True))
        code, v = self.call("/settings", {"model": "local:acme__tiny-vl"}); self.assertEqual(v["model"], "local:acme__tiny-vl")
        self.assertEqual(self.call("/models/delete", {"name": "acme__tiny-vl"})[0], 409)                     # in use
        for bad in ({"models_dir": ""}, {"model": "local:../x"}, {"stt": "local:../x"}, {"stt": "cloud:not an id"}, {"my_models": ["x"]}):
            self.assertEqual(self.call("/settings", bad)[0], 400, bad)
        self.assertEqual(self.call("/models/download", {"repo": "../../etc"})[0], 400)
        self.assertEqual(self.call("/models/delete", {"name": "../settings.json"})[0], 400)
        self.call("/settings", {"model": "auto_fast", "models_dir": None})


class ModelAndKey(Base):
    def test_7_default_model_from_the_picker_or_typed_reaches_the_ask(self):
        code, v = self.call("/settings"); self.assertEqual(v["picker_date"], "2026-11-21"); self.assertEqual(len(v["picker"]), 9)
        fams = [m["id"].split("/")[0] for m in v["picker"]]; self.assertEqual(len(fams), len(set(fams)))          # one per family
        self.assertIn("anthropic", fams); self.assertTrue(all(m["vision"] for m in v["picker"]))
        pid = "deepseek/deepseek-v4.1-flash-1017"
        code, v = self.call("/settings", {"model": pid}); self.assertEqual(v["model"], pid)
        self.ask_system(); self.assertEqual(self.fake.routes[-1], "or:" + pid)
        self.assertIsNone(core.ROUTES["or:" + pid]["provider"]); self.assertTrue(core.ROUTES["or:" + pid]["vision"])
        code, v = self.call("/settings", {"model": "mistralai/some-model-i-typed"}); self.assertEqual(code, 200)
        self.ask_system(); self.assertEqual(self.fake.routes[-1], "or:mistralai/some-model-i-typed"); self.assertFalse(core.ROUTES["or:mistralai/some-model-i-typed"]["vision"])
        for bad in ("no-slash", "a/b c", "../x", 'x/"y'):
            self.assertEqual(self.call("/settings", {"model": bad})[0], 400, bad)
        code, v = self.call("/settings", {"model": None}); self.ask_system(); self.assertEqual(self.fake.routes[-1], "or:" + core.FASTEST)   # none = the fastest
        code, v = self.call("/settings", {"model": "auto_cheap"}); self.ask_system(); self.assertEqual(self.fake.routes[-1], "or:" + core.CHEAPEST)
        self.assertEqual(core.ROUTES["or:" + core.FASTEST]["reasoning"], {"enabled": False, "exclude": True})                          # measured: off allowed
        self.assertEqual(core.ROUTES[core.ensure_route("openai/gpt-6.2-sol")]["reasoning"], {"effort": "low", "exclude": True})          # mandatory: low
        self.assertEqual(self.call("/settings", {"hotkey": "custom:35:command,shift"})[1]["hotkey"], "custom:35:command,shift")
        for bad in ("custom:35:hyper", "custom:x:option", "custom:35:option;rm"):
            self.assertEqual(self.call("/settings", {"hotkey": bad})[0], 400, bad)
        self.call("/settings", {"model": "auto_fast", "hotkey": "opt_return"})
        code, out = self.call("/ask", {"instruction": "x", "model": "haiku"})                              # the panel's own menu still wins
        for _ in range(100):
            if not engine.APP.ask_lock.locked() and self.fake.routes[-1] == "haiku": break
            time.sleep(0.05)
        self.assertEqual(self.fake.routes[-1], "haiku")

    def test_8_key_to_the_keychain_never_back_and_removable(self):
        code, out = self.call("/key", {"key": "not-a-key"}); self.assertEqual(code, 400)
        code, out = self.call("/key", {"key": FAKE_KEY}); self.assertEqual((code, out["key_source"]), (200, "app"))
        self.assertEqual(core._keychain_get(core.APP_KEY_ACCOUNT, core.APP_KEY_SERVICE), FAKE_KEY)
        self.assertNotIn(FAKE_KEY, json.dumps(self.call("/settings")[1]))                                  # never sent back
        self.assertFalse(any(FAKE_KEY in p.read_text(errors="replace") for p in Path(D).iterdir() if p.is_file()))   # never in a file
        code, r = self.call("/key/test", {}); self.assertEqual(code, 200); self.assertFalse(r["ok"]); self.assertNotIn(FAKE_KEY, json.dumps(r))   # refused, free
        code, out = self.call("/key", {"delete": True}); self.assertNotEqual(out["key_source"], "app")
        self.assertEqual(core._keychain_get(core.APP_KEY_ACCOUNT, core.APP_KEY_SERVICE), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
