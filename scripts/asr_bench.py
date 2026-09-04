#!/usr/bin/env python3
"""Measure what the speech-to-text engine actually does on this hardware.

Run through `scripts/asr.sh bench` (which resolves the endpoint) or directly:

    python3 scripts/asr_bench.py --base-url http://192.168.9.68:30006/v1

WHAT IT MEASURES, and why each number is here rather than a guess:

  * latency at 5 / 15 / 30 / 60 seconds of audio, because dictation latency is
    dominated by audio LENGTH, not by request count;
  * the real-time factor (seconds of audio per second of wall clock), which is
    the number that says whether this scales;
  * concurrency, because the composer is used by a workspace, not by one
    person — eight simultaneous recordings is a realistic Monday morning;
  * both of vLLM's audio endpoints, because they differ: /v1/audio/
    transcriptions returns text alone, while /v1/chat/completions returns the
    DETECTED LANGUAGE with it, and the console needs that.

Audio comes from the model authors' own published samples, resliced to the
requested lengths — no synthetic tones, which any ASR transcribes as silence
and which would make these numbers meaningless.

Standard library plus `av` where available; no repo imports, so this runs on a
bare host as easily as inside a container.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import statistics
import sys
import time
import urllib.request
import wave
from dataclasses import dataclass, field
from typing import Any

#: The model authors' published samples. English carries speech with natural
#: disfluencies; Chinese is the control that proves language identification is
#: doing something rather than defaulting.
SAMPLES = {
    "en": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-ASR-Repo/asr_en.wav",
    "zh": "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-ASR-Repo/asr_zh.wav",
}

BOUNDARY = "----techsara-asr-bench"


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as response:
        return response.read()


def wav_seconds(raw: bytes) -> float:
    with wave.open(io.BytesIO(raw)) as handle:
        return handle.getnframes() / handle.getframerate()


def resize_wav(raw: bytes, seconds: float) -> bytes:
    """Loop or cut a WAV to `seconds`, preserving its format exactly.

    Looping rather than padding with silence: a 60-second file that is 45
    seconds of silence measures the encoder, not the decoder.
    """
    with wave.open(io.BytesIO(raw)) as src:
        params = src.getparams()
        frames = src.readframes(src.getnframes())
    want = int(seconds * params.framerate)
    width = params.sampwidth * params.nchannels
    have = len(frames) // width
    if have == 0:
        raise SystemExit("the sample carried no audio frames")
    if have < want:
        repeats = -(-want // have)  # ceiling
        frames = (frames * repeats)[: want * width]
    else:
        frames = frames[: want * width]
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        dst.setnchannels(params.nchannels)
        dst.setsampwidth(params.sampwidth)
        dst.setframerate(params.framerate)
        dst.writeframes(frames)
    return out.getvalue()


def to_webm(raw_wav: bytes) -> bytes | None:
    """Re-encode to WebM/Opus — what a browser's MediaRecorder actually sends.

    Returns None when PyAV is absent, so the benchmark still runs on a host
    that only has the standard library.
    """
    try:
        import av  # type: ignore
    except Exception:
        return None
    src = av.open(io.BytesIO(raw_wav))
    buffer = io.BytesIO()
    dst = av.open(buffer, "w", format="webm")
    stream = dst.add_stream("libopus", rate=48000, layout="mono")
    for frame in src.decode(audio=0):
        frame.pts = None
        for packet in stream.encode(frame):
            dst.mux(packet)
    for packet in stream.encode():
        dst.mux(packet)
    dst.close()
    return buffer.getvalue()


def multipart(audio: bytes, filename: str, model: str, extra: dict[str, str]) -> tuple[bytes, str]:
    parts: list[bytes] = []
    for key, value in {"model": model, **extra}.items():
        parts.append(
            f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    ctype = "audio/webm" if filename.endswith(".webm") else "audio/wav"
    parts.append(
        f"--{BOUNDARY}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
    )
    parts.append(audio)
    parts.append(f"\r\n--{BOUNDARY}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={BOUNDARY}"


@dataclass
class Result:
    ok: bool
    seconds: float
    text: str = ""
    language: str = ""
    error: str = ""


@dataclass
class Series:
    label: str
    audio_seconds: float
    results: list[Result] = field(default_factory=list)

    @property
    def times(self) -> list[float]:
        return [r.seconds for r in self.results if r.ok]

    def row(self) -> str:
        good = self.times
        if not good:
            first = next((r.error for r in self.results if r.error), "no successful call")
            return f"  {self.label:<22} FAILED — {first[:60]}"
        mean = statistics.mean(good)
        p95 = max(good) if len(good) < 20 else statistics.quantiles(good, n=20)[18]
        rtf = self.audio_seconds / mean if mean else 0.0
        return (
            f"  {self.label:<22} n={len(good):<3} mean={mean:6.2f}s  "
            f"min={min(good):5.2f}s  max={max(good):5.2f}s  p95={p95:5.2f}s  "
            f"{rtf:6.1f}x real time"
        )


def post(url: str, body: bytes, content_type: str, timeout: float) -> Result:
    request = urllib.request.Request(url, data=body, headers={"Content-Type": content_type})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except Exception as exc:  # noqa: BLE001 — every failure is one row
        return Result(False, time.perf_counter() - started, error=f"{type(exc).__name__}: {exc}")
    elapsed = time.perf_counter() - started
    if "text" in payload:
        return Result(True, elapsed, text=str(payload["text"]))
    content = payload.get("choices", [{}])[0].get("message", {}).get("content", "")
    language, text = parse_chat_output(content)
    return Result(True, elapsed, text=text, language=language)


def parse_chat_output(content: str) -> tuple[str, str]:
    """Split `language English<asr_text>Hello there.` into its two halves.

    This is the model's own output contract, not a heuristic: the chat path
    always prefixes the detected language before the transcript marker.
    """
    marker = "<asr_text>"
    if content.startswith("language ") and marker in content:
        head, _, tail = content.partition(marker)
        return head[len("language "):].strip(), tail.strip()
    return "", content.strip()


def transcriptions_call(base: str, model: str, audio: bytes, name: str, timeout: float) -> Result:
    body, ctype = multipart(audio, name, model, {})
    return post(f"{base}/audio/transcriptions", body, ctype, timeout)


def chat_call(base: str, model: str, audio: bytes, name: str, timeout: float) -> Result:
    import base64

    mime = "audio/webm" if name.endswith(".webm") else "audio/wav"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "audio_url",
                        "audio_url": {
                            "url": f"data:{mime};base64,{base64.b64encode(audio).decode()}"
                        },
                    }
                ],
            }
        ],
        "max_tokens": 512,
        "temperature": 0.0,
    }
    return post(
        f"{base}/chat/completions", json.dumps(payload).encode(), "application/json", timeout
    )


async def concurrent(base: str, model: str, audio: bytes, name: str, n: int, timeout: float) -> Series:
    series = Series(f"{n} at once (15s each)", 15.0)
    loop = asyncio.get_running_loop()
    started = time.perf_counter()
    series.results = await asyncio.gather(
        *(
            loop.run_in_executor(None, transcriptions_call, base, model, audio, name, timeout)
            for _ in range(n)
        )
    )
    wall = time.perf_counter() - started
    print(f"  {n} concurrent requests finished in {wall:.2f}s wall clock")
    return series


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="e.g. http://192.168.9.68:30006/v1")
    parser.add_argument("--model", default="Qwen/Qwen3-ASR-1.7B")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--verify", action="store_true", help="one clip, print the transcript")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    print(f"engine   : {base}\nmodel    : {args.model}\n")

    print("fetching the reference samples…")
    english = fetch(SAMPLES["en"])
    chinese = fetch(SAMPLES["zh"])
    print(f"  english {wav_seconds(english):.1f}s   chinese {wav_seconds(chinese):.1f}s\n")

    if args.verify:
        webm = to_webm(resize_wav(english, 15.0))
        payload, name = (webm, "clip.webm") if webm else (english, "clip.wav")
        print(f"transcribing a 15-second clip as {name} ({len(payload) / 1024:.0f} KB)…")
        result = transcriptions_call(base, args.model, payload, name, args.timeout)
        if not result.ok:
            print(f"  FAILED: {result.error}")
            return 1
        print(f"  {result.seconds:.2f}s\n  “{result.text}”\n")
        control = chat_call(base, args.model, chinese, "control.wav", args.timeout)
        if control.ok:
            print(f"language identification control: {control.language} — “{control.text}”")
        return 0

    # Warm the engine: the first request after a load compiles kernels and is
    # not representative of anything a user will experience.
    print("warming up…")
    transcriptions_call(base, args.model, resize_wav(english, 5.0), "warm.wav", args.timeout)

    print("\nA. /v1/audio/transcriptions — WAV, by audio length")
    for seconds in (5, 15, 30, 60):
        clip = resize_wav(english, float(seconds))
        series = Series(f"{seconds}s audio (wav)", float(seconds))
        for _ in range(args.repeat):
            series.results.append(
                transcriptions_call(base, args.model, clip, "clip.wav", args.timeout)
            )
        print(series.row())

    webm15 = to_webm(resize_wav(english, 15.0))
    if webm15:
        print("\nB. the browser's own format — WebM/Opus, 15s")
        series = Series("15s audio (webm)", 15.0)
        for _ in range(args.repeat):
            series.results.append(
                transcriptions_call(base, args.model, webm15, "clip.webm", args.timeout)
            )
        print(series.row())
        wav15 = resize_wav(english, 15.0)
        print(f"     {len(wav15) / 1024:6.0f} KB as wav → {len(webm15) / 1024:.0f} KB as webm")

    print("\nC. /v1/chat/completions — the path that also reports the language")
    series = Series("15s audio (chat)", 15.0)
    for _ in range(args.repeat):
        series.results.append(
            chat_call(base, args.model, resize_wav(english, 15.0), "clip.wav", args.timeout)
        )
    print(series.row())
    detected = next((r.language for r in series.results if r.language), "—")
    print(f"     detected language: {detected}")

    print(f"\nD. concurrency — {args.concurrency} simultaneous 15-second requests")
    clip15 = webm15 or resize_wav(english, 15.0)
    name15 = "clip.webm" if webm15 else "clip.wav"
    series = asyncio.run(
        concurrent(base, args.model, clip15, name15, args.concurrency, args.timeout)
    )
    print(series.row())

    print("\nE. language identification")
    for label, clip in (("english", english), ("chinese", chinese)):
        result = chat_call(base, args.model, clip, f"{label}.wav", args.timeout)
        if result.ok:
            print(f"  {label:<8} → {result.language or '?':<10} “{result.text[:56]}”")
        else:
            print(f"  {label:<8} → FAILED {result.error[:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
