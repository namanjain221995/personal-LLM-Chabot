#!/usr/bin/env python3
"""Offline screening of streaming ASR candidates (sherpa-onnx) on real speech.

For each utterance the audio is fed in 40 ms frames exactly as the live engine
will receive it. After every frame the recognizer decodes whatever is ready, so
the measurements are those of the streaming path, not of a whole-file decode:

- WER against the reference (lower-cased, punctuation stripped).
- RTF: decode CPU seconds / audio seconds (single stream, fixed thread count).
- first_partial_delay_ms: audio position at which the first non-empty partial
  appeared, minus the speech onset (first 30 ms frame above an energy floor).
- final_delay_ms: audio position at which the endpoint fired (or end of feed +
  trailing silence padding), minus the speech end (last frame above the floor).
- step_ms p50/p95: wall time of one decode step (the per-frame compute latency).

Usage: screen.py --model-dir DIR --kind transducer|nemo_ctc|zipformer2_ctc --set librispeech|fleurs_en|fleurs_hi|fleurs_gu --n 100 --threads 2
"""
import argparse, glob, json, os, re, statistics, sys, time, unicodedata

import numpy as np
import soundfile as sf
import sherpa_onnx

FRAME = 640  # 40 ms at 16 kHz
PAD_S = 1.2  # trailing silence appended so the endpoint can fire


def norm(text: str) -> str:
    # Drop punctuation/symbols by Unicode category and KEEP combining marks: a regex word
    # class strips Devanagari vowel signs and splits every Hindi word into fragments.
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("\u093c", "").replace("\u0901", "\u0902")
    text = "".join(" " if unicodedata.category(ch)[0] in "PS" and ch != "'" else ch for ch in text)
    return " ".join(text.split())


def wer_counts(ref, hyp):
    r, h = ref.split(), hyp.split()
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)], len(r)


def load_set(name, n):
    root = os.environ.get("RTVOICE_DATA") or os.path.expanduser("~/rtvoice-bench/data")
    items = []
    if name == "librispeech":
        for trans in sorted(glob.glob(f"{root}/LibriSpeech/test-clean/*/*/*.trans.txt")):
            base = os.path.dirname(trans)
            for line in open(trans):
                uid, text = line.strip().split(" ", 1)
                items.append((f"{base}/{uid}.flac", text))
    else:
        lang = {"fleurs_en": "en_us", "fleurs_hi": "hi_in", "fleurs_gu": "gu_in"}[name]
        base = f"{root}/fleurs/{lang}"
        wavdir = f"{base}/test"
        for line in open(f"{base}/test.tsv", encoding="utf-8"):
            cols = line.rstrip("\n").split("\t")
            if len(cols) < 3:
                continue
            path = f"{wavdir}/{cols[1]}"
            if os.path.exists(path):
                items.append((path, cols[2]))
    # deterministic spread across speakers: every k-th item
    if n and len(items) > n:
        step = len(items) / n
        items = [items[int(i * step)] for i in range(n)]
    return items


def read16k(path):
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != 16000:
        # polyphase-free linear resample is fine for screening (FLEURS is 16 kHz already)
        x = np.arange(0, len(audio), sr / 16000.0)
        audio = np.interp(x, np.arange(len(audio)), audio).astype(np.float32)
    return audio


def speech_bounds(audio):
    win = 480
    e = [float(np.sqrt(np.mean(audio[i:i + win] ** 2) + 1e-12)) for i in range(0, len(audio) - win, win)]
    if not e:
        return 0, len(audio)
    floor = max(0.01, np.percentile(e, 20) * 3)
    voiced = [i for i, v in enumerate(e) if v > floor]
    if not voiced:
        return 0, len(audio)
    return voiced[0] * win, (voiced[-1] + 1) * win


def build(args):
    d = args.model_dir
    def f(pattern):
        hits = sorted(glob.glob(os.path.join(d, pattern)))
        prefer = [h for h in hits if "int8" in h] if args.int8 else [h for h in hits if "int8" not in h]
        return (prefer or hits)[0]
    common = dict(tokens=os.path.join(d, "tokens.txt"), num_threads=args.threads, sample_rate=16000,
                  feature_dim=80, enable_endpoint_detection=True,
                  rule1_min_trailing_silence=2.4, rule2_min_trailing_silence=args.endpoint_s,
                  rule3_min_utterance_length=600.0, provider="cpu")
    if args.kind == "transducer":
        return sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=f("encoder*.onnx"), decoder=f("decoder*.onnx"), joiner=f("joiner*.onnx"),
            decoding_method=args.decoding, **common)
    if args.kind == "nemo_ctc":
        return sherpa_onnx.OnlineRecognizer.from_nemo_ctc(model=f("model*.onnx"), **common)
    if args.kind == "zipformer2_ctc":
        return sherpa_onnx.OnlineRecognizer.from_zipformer2_ctc(model=f("*.onnx"), **common)
    raise SystemExit("unknown kind")


def run(args):
    rec = build(args)
    items = load_set(args.set, args.n)
    errs = words = 0
    audio_s = cpu_s = 0.0
    firsts, finals, steps = [], [], []
    samples = []
    for path, ref in items:
        audio = read16k(path)
        on, off = speech_bounds(audio)
        audio = np.concatenate([audio, np.zeros(int(PAD_S * 16000), dtype=np.float32)])
        s = rec.create_stream()
        if args.lang:
            s.set_option("language", args.lang)
        texts, first_at, final_at = [], None, None
        committed = 0          # chars of the running hypothesis already emitted as finals
        was_endpoint = False
        t_cpu0 = time.process_time()
        for i in range(0, len(audio), FRAME):
            s.accept_waveform(16000, audio[i:i + FRAME])
            while rec.is_ready(s):
                t0 = time.perf_counter()
                rec.decode_stream(s)
                steps.append((time.perf_counter() - t0) * 1000)
            hyp = rec.get_result(s)
            pos = i + FRAME
            pending = hyp[committed:].strip()
            if first_at is None and pending:
                first_at = pos
            ep = rec.is_endpoint(s)
            if ep and not was_endpoint and pending:
                texts.append(pending)
                committed = len(hyp)
                final_at = pos
            was_endpoint = ep
        s.input_finished()
        while rec.is_ready(s):
            rec.decode_stream(s)
        tail = rec.get_result(s)[committed:].strip()
        if tail:
            texts.append(tail)
            final_at = final_at or len(audio)
        cpu_s += time.process_time() - t_cpu0
        audio_s += len(audio) / 16000
        hyp_all = norm(" ".join(texts))
        e, w = wer_counts(norm(ref), hyp_all)
        errs += e
        words += w
        if first_at is not None:
            firsts.append((first_at - on) / 16)
        if final_at is not None:
            finals.append((final_at - off) / 16)
        if len(samples) < 3:
            samples.append({"ref": ref[:160], "hyp": " | ".join(texts)[:160]})
    q = lambda xs, p: (statistics.quantiles(xs, n=100)[p - 1] if len(xs) >= 2 else (xs[0] if xs else None))
    out = {
        "model": os.path.basename(os.path.normpath(args.model_dir)), "set": args.set, "n": len(items),
        "threads": args.threads, "int8": args.int8, "endpoint_s": args.endpoint_s, "decoding": args.decoding, "lang": args.lang,
        "wer": round(100 * errs / max(1, words), 2), "rtf": round(cpu_s / max(1e-9, audio_s), 4),
        "first_partial_delay_ms_p50": q(firsts, 50), "first_partial_delay_ms_p95": q(firsts, 95),
        "final_delay_ms_p50": q(finals, 50), "final_delay_ms_p95": q(finals, 95),
        "step_ms_p50": q(steps, 50), "step_ms_p95": q(steps, 95), "samples": samples,
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--kind", default="transducer")
    ap.add_argument("--set", default="librispeech")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--threads", type=int, default=2)
    ap.add_argument("--int8", action="store_true")
    ap.add_argument("--endpoint-s", type=float, default=0.8)
    ap.add_argument("--decoding", default="greedy_search")
    ap.add_argument("--lang", default="")
    run(ap.parse_args())
