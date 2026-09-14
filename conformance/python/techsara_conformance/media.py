"""Tiny media fixtures, made in memory with the standard library.

No binary fixture is committed (public repository, and a generated fixture
cannot drift from what the test believes it contains).
"""
from __future__ import annotations

import base64
import io
import math
import struct
import wave
import zlib


def solid_png(width: int = 64, height: int = 64, rgb: tuple = (220, 20, 20)) -> bytes:
    """A valid PNG filled with one colour (8-bit RGB, no filter)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def data_url(payload: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


def tone_wav(seconds: float = 2.0, rate: int = 16000, hz: float = 440.0) -> bytes:
    """Mono 16-bit PCM WAV. A tone, not speech: the transcription tests assert
    the response SHAPE (text is a string, usage is a duration), because a
    silence/no-speech gate may legitimately return empty text for it."""
    frames = int(seconds * rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(
            b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(frames))
        )
    return buf.getvalue()
