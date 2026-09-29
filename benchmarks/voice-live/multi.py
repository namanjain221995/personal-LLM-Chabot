#!/usr/bin/env python3
"""K decode workers (threads), each with its own recognizer; streams split across them.
If sherpa-onnx releases the GIL in decode_streams, capacity scales with K."""
import argparse, json, os, statistics, sys, threading, time
sys.path.insert(0, os.path.dirname(__file__))
from batch import build, speech_pool
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--model-dir", required=True); ap.add_argument("--chunk-ms", type=int, default=160)
ap.add_argument("--threads", type=int, default=2); ap.add_argument("--workers", type=int, default=4)
ap.add_argument("--ns", default="8,16,24,32"); ap.add_argument("--seconds", type=float, default=10); ap.add_argument("--lang", default="")
a = ap.parse_args()
recs = [build(a.model_dir, a.threads) for _ in range(a.workers)]
pool = speech_pool()
print("LOADED", flush=True)
chunk = int(16000 * a.chunk_ms / 1000)
for n in [int(x) for x in a.ns.split(",")]:
    groups = [[] for _ in range(a.workers)]
    for i in range(n):
        r = recs[i % a.workers]; s = r.create_stream()
        if a.lang: s.set_option("language", a.lang)
        groups[i % a.workers].append((i, s))
    ticks = int(a.seconds * 1000 / a.chunk_ms)
    lat = []
    lock = threading.Lock()
    def work(w):
        r = recs[w]; nxt = time.perf_counter()
        for t in range(ticks):
            for i, s in groups[w]:
                off = (int(i * len(pool) / max(1, n)) + t * chunk) % (len(pool) - chunk)
                s.accept_waveform(16000, pool[off:off + chunk])
            t0 = time.perf_counter()
            while True:
                ready = [s for _, s in groups[w] if r.is_ready(s)]
                if not ready: break
                r.decode_streams(ready)
            for _, s in groups[w]:
                if r.is_endpoint(s): r.reset(s)
            with lock: lat.append((time.perf_counter() - t0) * 1000)
            nxt += a.chunk_ms / 1000
            d = nxt - time.perf_counter()
            if d > 0: time.sleep(d)
    c0, w0 = time.process_time(), time.perf_counter()
    th = [threading.Thread(target=work, args=(w,)) for w in range(a.workers)]
    [t.start() for t in th]; [t.join() for t in th]
    cpu = time.process_time() - c0; wall = time.perf_counter() - w0
    q = statistics.quantiles(lat, n=100)
    print(json.dumps({"n": n, "workers": a.workers, "threads": a.threads, "chunk_ms": a.chunk_ms, "tick_ms_p50": round(q[49],1), "tick_ms_p95": round(q[94],1), "cpu_cores": round(cpu/wall,2), "realtime": q[94] < a.chunk_ms*0.8, "model": os.path.basename(a.model_dir.rstrip('/'))}), flush=True)
    if q[94] >= a.chunk_ms * 0.8: break
