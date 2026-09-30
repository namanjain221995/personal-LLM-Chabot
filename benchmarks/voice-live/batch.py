#!/usr/bin/env python3
"""Batched streaming throughput on CPU (sherpa-onnx).

N concurrent streams are fed audio at exactly real-time pace in chunk-sized
ticks (one tick = one model chunk of audio per stream). Each tick decodes every
ready stream with ONE decode_streams() call, which is how the engine will run.

Reported per N: tick compute ms (p50/p95/max) vs the chunk duration (real-time
budget), CPU utilisation, and a real-time verdict. Streams start staggered so
their chunks do not all align (like real users).

Usage: batch.py --model-dir DIR --chunk-ms 160 --threads 4 --ns 1,4,8,16,32 --seconds 20
"""
import argparse, glob, os, statistics, time

import numpy as np
import soundfile as sf
import sherpa_onnx


def build(d, threads):
    f = lambda p: sorted(glob.glob(os.path.join(d, p)))[0]
    return sherpa_onnx.OnlineRecognizer.from_transducer(
        tokens=os.path.join(d, "tokens.txt"), encoder=f("encoder*.onnx"), decoder=f("decoder*.onnx"),
        joiner=f("joiner*.onnx"), num_threads=threads, sample_rate=16000, feature_dim=80,
        enable_endpoint_detection=True, rule2_min_trailing_silence=0.6, provider="cpu")


def speech_pool():
    root = os.path.join(os.environ.get("RTVOICE_DATA") or os.path.expanduser("~/rtvoice-bench/data"), "LibriSpeech/test-clean")
    files = sorted(glob.glob(f"{root}/*/*/*.flac"))[:200]
    audio = np.concatenate([sf.read(p, dtype="float32")[0] for p in files[:60]])
    return audio


def run_n(rec, pool, n, chunk_ms, seconds, lang):
    chunk = int(16000 * chunk_ms / 1000)
    streams = []
    for i in range(n):
        s = rec.create_stream()
        if lang:
            s.set_option("language", lang)
        streams.append(s)
    offsets = [int(i * len(pool) / max(1, n)) % len(pool) for i in range(n)]
    ticks = int(seconds * 1000 / chunk_ms)
    times = []
    cpu0, wall0 = time.process_time(), time.perf_counter()
    next_t = time.perf_counter()
    for t in range(ticks):
        for i, s in enumerate(streams):
            a = offsets[i] + t * chunk
            seg = pool[a % len(pool): a % len(pool) + chunk]
            if len(seg) < chunk:
                seg = np.concatenate([seg, pool[: chunk - len(seg)]])
            s.accept_waveform(16000, seg)
        t0 = time.perf_counter()
        while True:
            ready = [s for s in streams if rec.is_ready(s)]
            if not ready:
                break
            rec.decode_streams(ready)
        for s in streams:
            if rec.is_endpoint(s):
                rec.get_result(s)
                rec.reset(s)
        times.append((time.perf_counter() - t0) * 1000)
        next_t += chunk_ms / 1000
        sleep = next_t - time.perf_counter()
        if sleep > 0:
            time.sleep(sleep)
    cpu = time.process_time() - cpu0
    wall = time.perf_counter() - wall0
    q = statistics.quantiles(times, n=100)
    return {"n": n, "chunk_ms": chunk_ms, "tick_ms_p50": round(q[49], 1), "tick_ms_p95": round(q[94], 1),
            "tick_ms_max": round(max(times), 1), "cpu_cores": round(cpu / wall, 2),
            "realtime": q[94] < chunk_ms * 0.8, "audio_s_per_wall_s": round(n * ticks * chunk_ms / 1000 / wall, 1)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--chunk-ms", type=int, default=160)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--ns", default="1,4,8,16,32")
    ap.add_argument("--seconds", type=float, default=20)
    ap.add_argument("--lang", default="")
    a = ap.parse_args()
    rec = build(a.model_dir, a.threads)
    pool = speech_pool()
    import json
    for n in [int(x) for x in a.ns.split(",")]:
        r = run_n(rec, pool, n, a.chunk_ms, a.seconds, a.lang)
        r.update({"model": os.path.basename(os.path.normpath(a.model_dir)), "threads": a.threads})
        print(json.dumps(r), flush=True)
        if not r["realtime"] and n > 1:
            break
