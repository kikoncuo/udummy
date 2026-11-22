#!/usr/bin/env python3
"""Local answer model check (no window, no audio, no network): loads a downloaded MLX model through core.LocalLLM, asks one text
question and, if the model reads images, one question about a made-up image drawn here. Prints load time, first-text time,
pieces per second and the process's peak memory.
    COPILOT_MODELS_DIR=<folder> <venv>/bin/python -B test_local_llm.py <model folder name>"""
import io, json, os, resource, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

name = sys.argv[1]
llm = core.LocalLLM()
t0 = time.monotonic(); load_s = llm.load(name)
out = {"model": name, "kind": core.model_kind(core.MODELS / name), "load_s": round(load_s, 1)}


def run(text, image=None):
    t = time.monotonic(); first = None; n = 0; parts = []
    for piece in llm.stream(name, "Responde en español, en una frase.", text, image, 80):
        if first is None:
            first = time.monotonic() - t
        n += 1; parts.append(piece)
    total = time.monotonic() - t
    return {"first_ms": round((first or 0) * 1000), "pieces": n, "pieces_per_s": round(n / max(total - (first or 0), 1e-6), 1), "answer": "".join(parts)[:160]}


out["text"] = run("¿Cuál es la capital de Francia?")
if llm.vision:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (480, 200), "white"); d = ImageDraw.Draw(img)
    d.rectangle([20, 40, 200, 160], fill=(220, 30, 30)); d.ellipse([260, 40, 380, 160], fill=(30, 60, 220))
    b = io.BytesIO(); img.save(b, "JPEG"); out["image"] = run("¿Qué formas y colores hay en la imagen?", b.getvalue())
out["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6)
print(json.dumps(out, ensure_ascii=False))
