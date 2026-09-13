"""The bounded in-memory multipart reader behind `/v1/audio/transcriptions`.

Pure: no database, no app. Bodies are built by httpx, the way a real client
builds them, and fed in awkward chunk sizes so a boundary or a header split
across two reads is exercised on every test.
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, List, Tuple

import httpx
import pytest

from app.publicapi import errors, multipart


def _encode(files=None, data=None) -> Tuple[bytes, str]:
    """Always multipart: text fields travel as `(None, value)` parts, because
    httpx sends a form with no file as urlencoded."""
    parts = list(files.items()) if isinstance(files, dict) else list(files or [])
    for name, value in (data or {}).items():
        for item in value if isinstance(value, list) else [value]:
            parts.append((name, (None, item)))
    request = httpx.Request("POST", "http://x/", files=parts)
    return request.read(), request.headers["content-type"]


async def _chunks(body: bytes, size: int = 7, pulled: List[int] = None) -> AsyncIterator[bytes]:
    for start in range(0, len(body), size):
        if pulled is not None:
            pulled.append(start)
        yield body[start:start + size]


def _read(body: bytes, content_type: str, **caps):
    caps.setdefault("max_body_bytes", 10**7)
    caps.setdefault("max_file_bytes", 10**7)
    return asyncio.run(multipart.read_form(_chunks(body), content_type, **caps))


def _refusal(body: bytes, content_type: str, **caps) -> errors.ApiError:
    with pytest.raises(errors.ApiError) as raised:
        _read(body, content_type, **caps)
    return raised.value


def test_fields_and_one_file_are_read_whatever_the_chunk_boundaries():
    audio = bytes(range(256)) * 40
    body, content_type = _encode(
        files={"file": ("x.ogg", audio, "audio/ogg")},
        data={"model": "techsara-whisper", "timestamp_granularities[]": ["segment", "segment"]},
    )

    for size in (1, 7, 64, len(body)):
        form = asyncio.run(
            multipart.read_form(
                _chunks(body, size), content_type, max_body_bytes=10**7, max_file_bytes=10**7
            )
        )
        assert form.field_value("model") == "techsara-whisper"
        assert form.fields["timestamp_granularities[]"] == ["segment", "segment"]
        part = form.file("file")
        assert (part.filename, part.content_type, bytes(part.data)) == ("x.ogg", "audio/ogg", audio)
        assert form.body_bytes == len(body)


def test_a_file_over_its_cap_is_a_413_and_reading_stops_there():
    body, content_type = _encode(files={"file": ("x.wav", b"\x00" * 50_000, "audio/wav")})
    pulled: List[int] = []

    with pytest.raises(errors.ApiError) as raised:
        asyncio.run(
            multipart.read_form(
                _chunks(body, 1000, pulled), content_type, max_body_bytes=10**7, max_file_bytes=10_000
            )
        )

    assert (raised.value.code, raised.value.status) == ("request_too_large", 413)
    # It did not go on pulling the other 40 KB of a refused upload.
    assert len(pulled) < 15


def test_a_body_over_its_cap_is_a_413_by_declaration_before_a_byte_is_read():
    body, content_type = _encode(files={"file": ("x.wav", b"\x00" * 5000, "audio/wav")})
    pulled: List[int] = []

    with pytest.raises(errors.ApiError) as raised:
        asyncio.run(
            multipart.read_form(
                _chunks(body, 100, pulled),
                content_type,
                max_body_bytes=1000,
                max_file_bytes=10**7,
                declared_length=str(len(body)),
            )
        )

    assert raised.value.status == 413
    assert pulled == []


def test_a_body_that_declares_nothing_is_still_counted_as_it_arrives():
    body, content_type = _encode(files={"file": ("x.wav", b"\x00" * 5000, "audio/wav")})

    assert _refusal(body, content_type, max_body_bytes=1000).status == 413


def test_a_body_cut_off_before_its_closing_boundary_is_refused_not_half_transcribed():
    body, content_type = _encode(files={"file": ("x.wav", b"\x01" * 5000, "audio/wav")})

    refusal = _refusal(body[:-20], content_type)

    assert refusal.code == "invalid_request_error"
    assert "closing boundary" in refusal.message


def test_a_non_multipart_body_and_a_missing_boundary_are_400s():
    assert _refusal(b"{}", "application/json").param == "Content-Type"
    assert "boundary" in _refusal(b"--x--", "multipart/form-data").message


def test_two_files_and_a_repeated_plain_field_are_refused_rather_than_guessed_between():
    two_files, ct = _encode(
        files=[("file", ("a.wav", b"a" * 10, "audio/wav")), ("other", ("b.wav", b"b" * 10, "audio/wav"))]
    )
    repeated, ct2 = _encode(data={"model": ["one", "two"]})

    assert "exactly one file" in _refusal(two_files, ct).message
    assert "more than once" in _refusal(repeated, ct2).message


def test_a_long_text_field_is_a_400_naming_the_field():
    body, content_type = _encode(data={"language": "x" * 5000})

    refusal = _refusal(body, content_type, max_field_bytes=64)

    assert (refusal.status, refusal.param) == (400, "language")


def test_a_body_of_endless_parts_is_refused_at_the_part_limit():
    body, content_type = _encode(data={f"f{i}": "v" for i in range(40)})

    assert "too many parts" in _refusal(body, content_type, max_parts=16).message


def test_an_endless_part_header_is_refused():
    """Whichever notices first — this reader's 4 KiB count or the parser
    library's own header cap — the answer is the same 400 and no part is
    kept."""
    boundary = "b0undary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n"
        f"X-Padding: {'p' * 20000}\r\n\r\nvalue\r\n--{boundary}--\r\n"
    ).encode()

    refusal = _refusal(body, f"multipart/form-data; boundary={boundary}")

    assert (refusal.status, refusal.code) == (400, "invalid_request_error")
    assert refusal.message in (
        "A multipart part header is too large.",
        "The request body is not valid multipart/form-data.",
    )


def test_a_malformed_body_is_refused_with_a_fixed_sentence_that_quotes_nothing():
    boundary = "b0undary"
    body = f"--{boundary}\r\nthis is not a header line with secret-prompt\r\n\r\n".encode()

    refusal = _refusal(body, f"multipart/form-data; boundary={boundary}")

    assert refusal.status == 400
    assert "secret-prompt" not in refusal.message


def test_a_field_that_is_not_utf8_is_a_400():
    boundary = "b0undary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n".encode()
        + b"\xff\xfe"
        + f"\r\n--{boundary}--\r\n".encode()
    )

    refusal = _refusal(body, f"multipart/form-data; boundary={boundary}")

    assert (refusal.status, refusal.param) == (400, "language")
