#!/usr/bin/env python3
"""whisper-large-v3 (production replica) on the SAME utterances the streaming screener used,
scored with the same normaliser, so English and Hindi numbers compare directly."""
import io, json, sys, time, urllib.request, uuid, wave
import numpy as np
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
import screen as S

URL = "http://192.168.9.68:30007/v1/audio/transcriptions"

def transcribe(x):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\nverbose_json\r\n"
            f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"c.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode() \
        + buf.getvalue() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    j = json.load(urllib.request.urlopen(req, timeout=120))
    return j.get("text") or "", j.get("language") or ""

for name, n in [("librispeech", 60), ("fleurs_en", 40), ("fleurs_hi", 40)]:
    items = S.load_set(name, n)
    errs = words = 0; langs = {}; t0 = time.time(); samples = []
    for path, ref in items:
        x = S.read16k(path)
        hyp, lang = transcribe(x)
        langs[lang] = langs.get(lang, 0) + 1
        e, w = S.wer_counts(S.norm(ref), S.norm(hyp)); errs += e; words += w
        if len(samples) < 2: samples.append({"ref": ref[:120], "hyp": hyp[:120]})
        time.sleep(0.3)
    print(json.dumps({"system": "whisper-large-v3", "set": name, "n": len(items), "wer": round(100 * errs / words, 2),
                      "languages": langs, "wall_s": round(time.time() - t0, 1), "samples": samples}, ensure_ascii=False), flush=True)
