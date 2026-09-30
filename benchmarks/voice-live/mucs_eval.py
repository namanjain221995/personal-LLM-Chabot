#!/usr/bin/env python3
"""Hinglish / Indian-English accuracy on MUCS 2021 Hindi-English test (OpenSLR 104, CC BY-SA 4.0).

Same stratified segments for every system. Systems:
  nemotron:<model_dir>:<lang>   streaming simulation, 40 ms frames, no mid-utterance reset
  whisper:<base_url>            the production whisper-large-v3 replica, auto language, one clip at a time

Metrics per system (references and hypotheses both normalised: NFKC, lower-case, punctuation removed):
  wer       plain word error rate
  cer       character error rate (spaces removed)
  tl_wer    WER after romanising Devanagari to a simple ISO-15919-like Latin form on BOTH
            sides, so a system that writes an English word in Devanagari ("ऑपरेटिंग") is judged
            closer to one that writes it in Latin ("operating"); still imperfect, identical for all systems
  script    share of hypothesis characters that are Devanagari / Latin / Arabic (Urdu script shows
            whisper's script flips)

usage: mucs_eval.py --systems ... --n-per-lecture 5 --out results.json
"""
import argparse, io, json, os, random, re, statistics, time, unicodedata, urllib.request, uuid, wave

import numpy as np
import soundfile as sf

ROOT = os.environ.get("MUCS_ROOT") or os.path.expanduser("~/rtvoice-bench/data/mucs/test")

# --- romanisation (identical for every system) ------------------------------------------
V = {"अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o", "औ": "au",
     "ऑ": "o", "ऍ": "e"}
M = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e"}
C = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
     "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
     "प": "p", "फ": "f", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "व": "v", "श": "sh",
     "ष": "sh", "स": "s", "ह": "h", "क़": "k", "ख़": "kh", "ग़": "g", "ज़": "z", "ड़": "d", "ढ़": "dh", "फ़": "f", "य़": "y"}
SIGNS = {"ं": "n", "ँ": "n", "ः": "h", "़": ""}


def romanise(text: str) -> str:
    out, i = [], 0
    chars = list(text)
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if ch in C:
            base = C[ch]
            if nxt == "़":
                i += 1
                nxt = chars[i + 1] if i + 1 < len(chars) else ""
            if nxt == "्":
                out.append(base); i += 2; continue
            if nxt in M:
                out.append(base + M[nxt]); i += 2; continue
            # inherent vowel, dropped word-finally (schwa deletion, crude)
            end = (not nxt) or (not ("ऀ" <= nxt <= "ॿ"))
            out.append(base if end else base + "a"); i += 1; continue
        if ch in V:
            out.append(V[ch]); i += 1; continue
        if ch in SIGNS:
            out.append(SIGNS[ch]); i += 1; continue
        if ch in M:
            out.append(M[ch]); i += 1; continue
        out.append(ch); i += 1
    s = "".join(out)
    # collapse doubled vowels and common spelling variants so aa/a, ee/i, oo/u do not count
    s = re.sub(r"(aa|a+)", "a", s)
    s = s.replace("ee", "i").replace("oo", "u").replace("w", "v").replace("ph", "f")
    return s


def norm(text: str) -> str:
    """Lower-case, drop punctuation and symbols (Unicode categories P* and S*), KEEP combining
    marks (Devanagari vowel signs are category M; a regex word class would split every Hindi word),
    fold nukta and chandrabindu variants that the references themselves use inconsistently."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("़", "").replace("ँ", "ं")
    text = "".join(" " if unicodedata.category(ch)[0] in "PS" else ch for ch in text)
    return " ".join(text.split())


def edits(r, h):
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)]


def script_mix(text):
    dev = sum(1 for c in text if "ऀ" <= c <= "ॿ")
    lat = sum(1 for c in text if c.isascii() and c.isalpha())
    arab = sum(1 for c in text if "؀" <= c <= "ۿ")
    tot = max(1, dev + lat + arab)
    return {"dev": dev / tot, "lat": lat / tot, "arab": arab / tot}


# --- data ---------------------------------------------------------------------------------
def pick_docs(n_recordings):
    segs, text = [], {}
    for line in open(f"{ROOT}/transcripts/text", encoding="utf-8"):
        uid, tx = line.rstrip("\n").split(" ", 1)
        text[uid] = tx
    for line in open(f"{ROOT}/transcripts/segments", encoding="utf-8"):
        uid, rec, a, b = line.split()
        if uid in text:
            segs.append((rec, float(a), float(b), uid))
    recs = sorted({s[0] for s in segs})
    step = max(1, len(recs) // n_recordings)
    chosen = recs[::step][:n_recordings]
    items = []
    for rec in chosen:
        audio = sf.read(f"{ROOT}/{rec}.wav", dtype="float32")[0]
        for r, a, b, uid in sorted(s for s in segs if s[0] == rec):
            if b > a:
                items.append((uid, audio[int(a * 16000):int(b * 16000)], text[uid]))
    return items


def pick(n_per_lecture, seed=7):
    segs = {}
    for line in open(f"{ROOT}/transcripts/segments", encoding="utf-8"):
        uid, rec, a, b = line.split()
        segs[uid] = (rec, float(a), float(b))
    text = {}
    for line in open(f"{ROOT}/transcripts/text", encoding="utf-8"):
        uid, t = line.rstrip("\n").split(" ", 1)
        text[uid] = t
    by = {}
    for uid, (rec, a, b) in segs.items():
        if 3.0 <= b - a <= 20.0 and uid in text and len(text[uid].split()) >= 4:
            by.setdefault(rec, []).append(uid)
    rng = random.Random(seed)
    out = []
    for rec in sorted(by):
        out += rng.sample(sorted(by[rec]), min(n_per_lecture, len(by[rec])))
    audio_cache = {}
    items = []
    for uid in out:
        rec, a, b = segs[uid]
        if rec not in audio_cache:
            audio_cache[rec] = sf.read(f"{ROOT}/{rec}.wav", dtype="float32")[0]
        x = audio_cache[rec][int(a * 16000):int(b * 16000)]
        items.append((uid, x, text[uid]))
    return items


# --- systems --------------------------------------------------------------------------------
def nemotron(model_dir, lang, items, threads=2, endpoint_s=0.6):
    import glob, sherpa_onnx
    f = lambda p: sorted(glob.glob(os.path.join(model_dir, p)))[0]
    rec = sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=os.path.join(model_dir, "tokens.txt"), encoder=f("encoder*.onnx"), decoder=f("decoder*.onnx"),
        joiner=f("joiner*.onnx"), num_threads=threads, sample_rate=16000, feature_dim=80,
        enable_endpoint_detection=True, rule2_min_trailing_silence=endpoint_s, rule3_min_utterance_length=3600.0)
    hyps = []
    t0 = time.process_time()
    for uid, x, ref in items:
        s = rec.create_stream()
        if lang:
            s.set_option("language", lang)
        a = np.concatenate([np.zeros(1600, np.float32), x, np.zeros(12800, np.float32)])  # 100 ms lead, 0.8 s tail
        for i in range(0, len(a), 640):
            s.accept_waveform(16000, a[i:i + 640])
            while rec.is_ready(s):
                rec.decode_stream(s)
        s.input_finished()
        while rec.is_ready(s):
            rec.decode_stream(s)
        hyps.append(rec.get_result(s))
    return hyps, time.process_time() - t0


def chat_busy():
    """True while the main model is generating for someone (Prometheus on the head). The whisper
    leg pauses then, so this measurement never slows a real chat answer."""
    prom = os.environ.get("PROM_URL")
    if not prom:
        return False
    try:
        q = urllib.request.urlopen(prom + "/api/v1/query?query=cluster:vllm_requests_running:current", timeout=5)
        r = json.load(q)["data"]["result"]
        return bool(r) and float(r[0]["value"][1]) > 0
    except Exception:  # noqa: BLE001
        return False


def whisper(base_url, items, gap_s=0.25):
    hyps, langs = [], []
    t0 = time.time()
    paused = 0.0
    for uid, x, ref in items:
        waited = 0.0
        while chat_busy() and waited < 120:
            time.sleep(2); waited += 2
        paused += waited
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\nverbose_json\r\n"
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"c.wav\"\r\n"
                f"Content-Type: audio/wav\r\n\r\n").encode() + buf.getvalue() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"{base_url}/audio/transcriptions", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            j = json.load(urllib.request.urlopen(req, timeout=120))
            hyps.append(j.get("text") or "")
            langs.append(j.get("language") or "")
        except Exception as e:  # noqa: BLE001
            hyps.append(""); langs.append(f"error:{type(e).__name__}")
        time.sleep(gap_s)
    print(f"whisper leg paused {paused:.0f}s for live chat", flush=True)
    return hyps, time.time() - t0, langs


def score_docs(items, hyps):
    docs = {}
    for (uid, x, ref), h in zip(items, hyps):
        rec = uid.split("_")[1]
        docs.setdefault(rec, [[], []])
        docs[rec][0].append(ref); docs[rec][1].append(h)
    we = wn = ce = cn = te = tn = 0
    per = {}
    for rec, (refs, hs) in docs.items():
        r, hh = norm(" ".join(refs)), norm(" ".join(hs))
        e1 = edits(r.split(), hh.split()); we += e1; wn += len(r.split())
        ce += edits(list(r.replace(" ", "")), list(hh.replace(" ", ""))); cn += len(r.replace(" ", ""))
        tr, th = norm(romanise(" ".join(refs))), norm(romanise(" ".join(hs)))
        te += edits(tr.split(), th.split()); tn += len(tr.split())
        per[rec] = round(100 * e1 / max(1, len(r.split())), 2)
    return {"doc_wer": round(100 * we / wn, 2), "doc_cer": round(100 * ce / cn, 2), "doc_tl_wer": round(100 * te / tn, 2),
            "per_recording_wer": per}


def score(items, hyps):
    we = wn = ce = cn = te = tn = 0
    mixes = []
    for (uid, x, ref), h in zip(items, hyps):
        r, hh = norm(ref), norm(h)
        we += edits(r.split(), hh.split()); wn += len(r.split())
        ce += edits(list(r.replace(" ", "")), list(hh.replace(" ", ""))); cn += len(r.replace(" ", ""))
        tr, th = norm(romanise(ref)), norm(romanise(h))
        te += edits(tr.split(), th.split()); tn += len(tr.split())
        mixes.append(script_mix(h))
    avg = lambda k: round(statistics.mean(m[k] for m in mixes), 3) if mixes else None
    return {"wer": round(100 * we / wn, 2), "cer": round(100 * ce / cn, 2), "tl_wer": round(100 * te / tn, 2),
            "script": {"dev": avg("dev"), "lat": avg("lat"), "arab": avg("arab")}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", nargs="+", required=True)
    ap.add_argument("--n-per-lecture", type=int, default=5)
    ap.add_argument("--recordings", type=int, default=0, help="document mode: whole recordings")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    items = pick_docs(a.recordings) if a.recordings else pick(a.n_per_lecture)
    audio_s = sum(len(x) for _, x, _ in items) / 16000
    results = []
    for sysdef in a.systems:
        kind, _, rest = sysdef.partition(":")
        extra = {}
        if kind == "nemotron":
            model_dir, _, lang = rest.rpartition(":")
            hyps, cpu = nemotron(model_dir, lang, items)
            extra = {"cpu_s": round(cpu, 1), "rtf_cpu": round(cpu / audio_s, 3)}
        else:
            hyps, wall, langs = whisper(rest, items)
            extra = {"wall_s": round(wall, 1), "languages": {l: langs.count(l) for l in set(langs)}}
        res = {"system": sysdef, "n": len(items), "audio_s": round(audio_s, 1), **score(items, hyps),
               **(score_docs(items, hyps) if a.recordings else {}), **extra,
               "hyps": {items[i][0]: hyps[i] for i in range(len(items))},
               "samples": [{"ref": items[i][2], "hyp": hyps[i]} for i in range(0, len(items), max(1, len(items) // 6))][:6]}
        results.append(res)
        print(json.dumps({k: v for k, v in res.items() if k not in ("samples", "hyps")}, ensure_ascii=False), flush=True)
        json.dump(results, open(a.out, "w"), ensure_ascii=False, indent=1)
    json.dump(results, open(a.out, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
