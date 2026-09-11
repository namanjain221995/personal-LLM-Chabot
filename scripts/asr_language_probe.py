#!/usr/bin/env python3
"""Re-transcribe windows of a stored video analysis under each language
setting, side by side, so a "the transcript is wrong" report becomes a
table (docs/video-understanding/LANGUAGE-EVIDENCE-2026-09-11.md is one).

    scripts/asr_language_probe.py --hash <64 hex> --at 790 --at 2150 \\
        [--seconds 60] [--languages auto,gu,hi,en] \\
        [--engine http://192.168.9.68:30007] [--container sf-local-ai-orchestrator-1]

Read-only: the windows are cut from `audio.wav` in the analysis directory
with ffmpeg inside the orchestrator container and posted straight to the
speech engine; nothing is written back. Each window is sent once per
setting, one at a time, so the cost is `windows x settings` clips of the
chosen length on one engine — keep it to a few, the engine is shared with
dictation and the main model's Spark.

Prints, per window and setting: the language the engine reported, the
character and cue counts, the engine's wall-clock (a looping decoder is
slow — that number is evidence too), and the first 240 characters.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import uuid

RATE = 16000


def cut(container: str, wav_path: str, start: float, seconds: float) -> bytes:
    proc = subprocess.run(
        ["docker", "exec", container, "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-ss", str(start), "-t", str(seconds), "-i", wav_path,
         "-f", "wav", "-ac", "1", "-ar", str(RATE), "-acodec", "pcm_s16le", "pipe:1"],
        capture_output=True,
    )
    if proc.returncode:
        raise SystemExit(proc.stderr.decode("utf-8", "replace")[:500])
    return proc.stdout


def transcribe(engine: str, wav: bytes, language: str | None) -> tuple[dict, float]:
    boundary = uuid.uuid4().hex.encode()
    parts = [
        b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"file\"; filename=\"w.wav\"\r\n"
        b"Content-Type: audio/wav\r\n\r\n" + wav + b"\r\n",
    ]
    fields = {"response_format": "verbose_json", "no_speech_check": "false"}
    if language:
        fields["language"] = language
    for name, value in fields.items():
        parts.append(b"--" + boundary + b"\r\nContent-Disposition: form-data; name=\"" + name.encode()
                     + b"\"\r\n\r\n" + value.encode() + b"\r\n")
    parts.append(b"--" + boundary + b"--\r\n")
    req = urllib.request.Request(
        engine.rstrip("/") + "/v1/audio/transcriptions",
        data=b"".join(parts),
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary.decode()},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(req, timeout=900) as resp:
        return json.load(resp), time.perf_counter() - started


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hash", required=True, help="the analysis's content hash (64 hex)")
    ap.add_argument("--at", type=float, action="append", required=True, help="window start, seconds (repeatable)")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--languages", default="auto,gu,hi,en", help="comma list; 'auto' = let the engine decide")
    ap.add_argument("--engine", default="http://192.168.9.68:30007")
    ap.add_argument("--container", default="sf-local-ai-orchestrator-1")
    ap.add_argument("--data-dir", default="/data/video", help="VIDEO_DATA_DIR inside the container")
    args = ap.parse_args()
    if len(args.hash) != 64:
        ap.error("--hash must be the 64-character content hash")

    wav_path = f"{args.data_dir}/{args.hash}/audio.wav"
    settings = [None if s.strip().lower() == "auto" else s.strip().lower() for s in args.languages.split(",") if s.strip()]
    for start in args.at:
        wav = cut(args.container, wav_path, start, args.seconds)
        print(f"\n=== {start:.0f}s (+{args.seconds:.0f}s) ===", flush=True)
        for language in settings:
            label = language or "auto"
            try:
                result, secs = transcribe(args.engine, wav, language)
            except Exception as exc:  # noqa: BLE001 — one failed setting is a row, not the end
                print(f"  [{label:4}] FAILED: {exc}", flush=True)
                continue
            text = (result.get("text") or "").strip()
            cues = result.get("segments") or []
            print(f"  [{label:4}] detected={str(result.get('language_code')):4} {len(text):5d} chars {len(cues):3d} cues {secs:6.1f}s", flush=True)
            print(f"         {text[:240]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
