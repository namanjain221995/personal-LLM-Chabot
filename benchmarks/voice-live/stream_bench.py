#!/usr/bin/env python3
"""Real-time streaming benchmark for the live-dictation path.

    python3 benchmarks/voice-live/stream_bench.py --target engine \\
        --url ws://192.168.9.68:30009/v1/stream --token-file .runtime/stt-token \\
        --set librispeech --data ~/rtvoice-bench/data --sessions 1,4,8,12 --utterances 20

WHAT IT MEASURES. N sessions stream real recorded speech concurrently, each at
exactly real-time pace in 40 ms frames — the way a browser does — through the
same WebSocket protocol production uses. For every utterance it records:

  first_partial_ms  wall time from sending the frame that contains the speech
                    onset to receiving the first non-empty partial;
  partial_lag_ms    for every partial, receive time minus the send time of the
                    audio sample it covers up to (end_sample) — how far behind
                    the live text runs;
  final_ms          wall time from sending the last voiced frame of the
                    utterance to receiving its final (includes the endpoint's
                    trailing-silence wait, which is the dominant part by design);
  WER               of the concatenated finals against the reference text
                    (lower-cased, punctuation removed; digits are NOT
                    normalised, so numeric references count as errors).

Speech onset/offset come from a simple energy detector on the reference audio
(20 ms windows above max(0.01, 3x the 20th-percentile energy)), the same rule
the offline screener used, so numbers are comparable across runs.

TARGETS. `engine` talks to the streaming engine directly (Bearer token).
`gateway` talks to the orchestrator/frontend WebSocket as a browser would: it
needs a session cookie (--cookie-file, the raw `ts_session=...` pair) and an
Origin, and an existing recording session id per stream (--session-ids-file),
which the operator creates beforehand. Nothing here creates accounts.

Standard library plus numpy, soundfile and websockets (all already present in
the benchmark venv on the worker).
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import re
import statistics
import time
import unicodedata
from pathlib import Path

import numpy as np
import soundfile as sf
import websockets

FRAME = 640  # 40 ms at 16 kHz
RATE = 16000


def norm(text: str) -> str:
    # Drop punctuation/symbols by Unicode category and KEEP combining marks: a regex word
    # class strips Devanagari vowel signs and splits every Hindi word into fragments.
    text = unicodedata.normalize("NFKC", text).lower()
    text = text.replace("\u093c", "").replace("\u0901", "\u0902")
    text = "".join(" " if unicodedata.category(ch)[0] in "PS" and ch != "'" else ch for ch in text)
    return " ".join(text.split())


def wer_counts(ref: str, hyp: str) -> tuple[int, int]:
    r, h = ref.split(), hyp.split()
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev = cur
    return d[len(h)], len(r)


def load_set(root: Path, name: str, n: int) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    if name == "librispeech":
        for trans in sorted(glob.glob(f"{root}/LibriSpeech/test-clean/*/*/*.trans.txt")):
            base = os.path.dirname(trans)
            for line in open(trans, encoding="utf-8"):
                uid, text = line.strip().split(" ", 1)
                items.append((f"{base}/{uid}.flac", text))
    else:
        lang = {"fleurs_en": "en_us", "fleurs_hi": "hi_in", "fleurs_gu": "gu_in"}[name]
        base = root / "fleurs" / lang
        for line in open(base / "test.tsv", encoding="utf-8"):
            cols = line.rstrip("\n").split("\t")
            if len(cols) >= 3 and (base / "test" / cols[1]).exists():
                items.append((str(base / "test" / cols[1]), cols[2]))
    if n and len(items) > n:
        step = len(items) / n
        items = [items[int(i * step)] for i in range(n)]
    return items


def read16k(path: str) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=True)
    audio = audio.mean(axis=1)
    if sr != RATE:
        x = np.arange(0, len(audio), sr / RATE)
        audio = np.interp(x, np.arange(len(audio)), audio).astype(np.float32)
    return audio


def speech_bounds(audio: np.ndarray) -> tuple[int, int]:
    win = 320
    e = [float(np.sqrt(np.mean(audio[i:i + win] ** 2) + 1e-12)) for i in range(0, len(audio) - win, win)]
    if not e:
        return 0, len(audio)
    floor = max(0.01, float(np.percentile(e, 20)) * 3)
    voiced = [i for i, v in enumerate(e) if v > floor]
    if not voiced:
        return 0, len(audio)
    return voiced[0] * win, (voiced[-1] + 1) * win


def pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


async def run_stream(idx: int, args, items, session_id: str | None) -> dict:
    """One session: its utterances back to back with 1.2 s of silence between
    them (so each ends in an endpoint), streamed at real-time pace."""
    gap = np.zeros(int(1.2 * RATE), dtype=np.float32)
    audio_parts, refs, bounds = [], [], []
    cursor = 0
    for path, ref in items:
        a = read16k(path)
        on, off = speech_bounds(a)
        bounds.append((cursor + on, cursor + off))
        refs.append(ref)
        audio_parts += [a, gap]
        cursor += len(a) + len(gap)
    audio = np.concatenate(audio_parts)
    send_time: dict[int, float] = {}  # sample index at frame end -> wall time sent
    events: list[tuple[float, dict]] = []
    headers = {}
    if args.target == "engine":
        url = args.url
        if args.token:
            headers["Authorization"] = f"Bearer {args.token}"
        start = {"type": "start", "sample_rate": RATE, "encoding": "pcm_s16le", "first_sample": 0,
                 "first_u": 0, "mode": "dictation", "language": args.language}
        origin = None
    else:
        url = args.url.replace("{session_id}", session_id or "")
        headers["Cookie"] = args.cookie
        start = {"type": "start", "v": 1, "encoding": "pcm_s16le", "sample_rate": RATE, "channels": 1,
                 "frame_ms": 40, "source": "mic", "resume_from_sample": 0, "next_u": 0,
                 "clock_offset_ms": 0, "language": args.language}
        origin = args.origin
    t_open = time.perf_counter()
    async with websockets.connect(url, additional_headers=headers, origin=origin, max_size=2 ** 20,
                                  subprotocols=["techsara.voice.v1"] if args.target == "gateway" else None,
                                  compression=None) as ws:
        await ws.send(json.dumps(start))
        ready = json.loads(await ws.recv())
        t_ready = time.perf_counter()

        async def reader():
            async for msg in ws:
                if isinstance(msg, bytes):
                    continue
                ev = json.loads(msg)
                events.append((time.perf_counter(), ev))
                if ev.get("type") in ("done", "error"):
                    return

        rtask = asyncio.create_task(reader())
        t0 = time.perf_counter()
        for k, i in enumerate(range(0, len(audio), FRAME)):
            target = t0 + k * FRAME / RATE
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            await ws.send(pcm16(audio[i:i + FRAME]))
            send_time[min(i + FRAME, len(audio))] = time.perf_counter()
        await ws.send(json.dumps({"type": "flush"}))
        try:
            async with asyncio.timeout(15):
                await rtask
        except TimeoutError:
            rtask.cancel()

    def sent_at(sample: int) -> float | None:
        # the first frame whose end covers `sample`
        k = ((sample + FRAME - 1) // FRAME) * FRAME
        return send_time.get(min(k, len(audio))) or send_time.get(len(audio))

    finals = [(t, e) for t, e in events if e.get("type") == "final"]
    partials = [(t, e) for t, e in events if e.get("type") == "partial"]
    errors = [e for _, e in events if e.get("type") == "error"]
    first_partial, final_lat, lags = [], [], []
    for on, off in bounds:
        onset_sent = sent_at(on + FRAME)
        p = [t for t, e in partials if e.get("end_sample", 0) > on and e.get("text")]
        if p and onset_sent:
            first_partial.append((min(p) - onset_sent) * 1000)
        f = [t for t, e in finals if e.get("end_sample", 0) >= off - RATE // 2 and e.get("start_sample", 0) <= off]
        off_sent = sent_at(off)
        if f and off_sent:
            final_lat.append((min(f) - off_sent) * 1000)
    for t, e in partials:
        s = sent_at(int(e.get("end_sample", 0)))
        if s:
            lags.append((t - s) * 1000)
    hyp = norm(" ".join(e.get("text", "") for _, e in finals))
    errs, words = wer_counts(norm(" ".join(refs)), hyp)
    return {"stream": idx, "connect_ms": (t_ready - t_open) * 1000, "first_partial_ms": first_partial,
            "final_ms": final_lat, "partial_lag_ms": lags, "errors": errors, "err": errs, "words": words,
            "audio_s": len(audio) / RATE, "partials": len(partials), "finals": len(finals)}


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = min(len(xs) - 1, max(0, int(round(p / 100 * (len(xs) - 1)))))
    return round(xs[k], 1)


async def main_async(args) -> None:
    items = load_set(Path(os.path.expanduser(args.data)), args.set, args.utterances * max(args.sessions_list))
    ids = []
    if args.target == "gateway":
        ids = [l.strip() for l in open(args.session_ids_file) if l.strip()]
    for n in args.sessions_list:
        per = [items[(i * args.utterances) % len(items):][: args.utterances] or items[: args.utterances]
               for i in range(n)]
        t = time.perf_counter()
        res = await asyncio.gather(*[run_stream(i, args, per[i], ids[i] if ids else None) for i in range(n)],
                                   return_exceptions=True)
        ok = [r for r in res if isinstance(r, dict)]
        bad = [repr(r)[:200] for r in res if not isinstance(r, dict)]
        fp = [x for r in ok for x in r["first_partial_ms"]]
        fl = [x for r in ok for x in r["final_ms"]]
        lg = [x for r in ok for x in r["partial_lag_ms"]]
        errs = sum(r["err"] for r in ok)
        words = sum(r["words"] for r in ok)
        print(json.dumps({
            "target": args.target, "set": args.set, "language": args.language, "sessions": n,
            "utterances": sum(len(p) for p in per), "wall_s": round(time.perf_counter() - t, 1),
            "wer": round(100 * errs / max(1, words), 2),
            "first_partial_ms": {"p50": pct(fp, 50), "p90": pct(fp, 90), "p99": pct(fp, 99)},
            "final_ms": {"p50": pct(fl, 50), "p90": pct(fl, 90), "p99": pct(fl, 99)},
            "partial_lag_ms": {"p50": pct(lg, 50), "p90": pct(lg, 90), "p99": pct(lg, 99)},
            "connect_ms_p50": pct([r["connect_ms"] for r in ok], 50),
            "stream_errors": sum(len(r["errors"]) for r in ok), "failed_streams": bad,
        }), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["engine", "gateway"], default="engine")
    ap.add_argument("--url", required=True, help="engine: ws://host:port/v1/stream; gateway: ws(s)://host/api/audio/sessions/{session_id}/live")
    ap.add_argument("--token-file", default="")
    ap.add_argument("--cookie-file", default="", help="gateway: file holding 'ts_session=...'")
    ap.add_argument("--origin", default="http://127.0.0.1:13000")
    ap.add_argument("--session-ids-file", default="")
    ap.add_argument("--data", default="~/rtvoice-bench/data")
    ap.add_argument("--set", default="librispeech", choices=["librispeech", "fleurs_en", "fleurs_hi", "fleurs_gu"])
    ap.add_argument("--language", default="auto")
    ap.add_argument("--sessions", default="1")
    ap.add_argument("--utterances", type=int, default=10, help="per session")
    args = ap.parse_args()
    args.sessions_list = [int(x) for x in args.sessions.split(",")]
    args.token = open(os.path.expanduser(args.token_file)).read().strip() if args.token_file else ""
    args.cookie = open(os.path.expanduser(args.cookie_file)).read().strip() if args.cookie_file else ""
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
