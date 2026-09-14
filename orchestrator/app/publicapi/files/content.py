"""Streaming a stored file back: one byte range or all of it, with the safe
header set (design §2.6). Used by `GET /v1/files/{id}/content` and, through
`FilesDependencies.derived`, by the derived-file route.

WHY THESE HEADERS ON EVERY BYTE ROUTE. `/v1` shares the chat application's
origin, and the chat application authenticates with the `ts_session` cookie.
An uploaded HTML or SVG must never render there: `attachment` +
`application/octet-stream` + `nosniff` + `Content-Security-Policy: sandbox`
+ `no-store` together make a download a download in every browser, whatever
the bytes are. A Bearer header cannot be attached by a navigation today; these
keep that true if a signed download URL is ever added (design §10.1).

WHY THE FILE IS OPENED BEFORE THE STATUS LINE. A purge racing a download then
either happens first (404, from the row) or after the open — and an open
descriptor keeps the inode alive, so the download completes with the bytes it
started with instead of a stream that breaks half-way.

RANGES. One `bytes=a-b`, `bytes=a-` or `bytes=-n` → 206 with `Content-Range`.
Several ranges → the whole body with 200 (a multipart/byteranges response is
legal to skip and no SDK asks for one). Unsatisfiable → 416 with
`Content-Range: bytes */N`. `If-None-Match` equal to the ETag → 304.
"""
from __future__ import annotations

import os
import re
from typing import AsyncIterator, Dict, Optional, Tuple, Union
from urllib.parse import quote

import anyio
from starlette.responses import Response, StreamingResponse

from . import wire

READ_BYTES = 1024 * 1024

_RANGE_RE = re.compile(r"^\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*$", re.IGNORECASE)

UNSATISFIABLE = "unsatisfiable"


def parse_range(header: Optional[str], size: int) -> Union[None, str, Tuple[int, int]]:
    """None → the whole body; `UNSATISFIABLE`; or an inclusive (start, end)."""
    if not header:
        return None
    value = header.strip()
    if not value.lower().startswith("bytes"):
        return None
    if "," in value:
        return None
    match = _RANGE_RE.match(value)
    if match is None:
        return None
    first, last = match.group(1), match.group(2)
    if first == "" and last == "":
        return None
    if first == "":
        suffix = int(last)
        if suffix == 0 or size == 0:
            return UNSATISFIABLE
        return (max(0, size - suffix), size - 1)
    start = int(first)
    if start >= size:
        return UNSATISFIABLE
    if last != "" and int(last) < start:
        # RFC 9110 §14.1.1: an invalid range spec is ignored, not refused.
        return None
    end = size - 1 if last == "" else min(int(last), size - 1)
    return (start, end)


def content_disposition(filename: str) -> str:
    """`attachment; filename="<ascii>"; filename*=UTF-8''<pct>` (RFC 6266).
    The ASCII fallback drops quotes, backslashes and anything outside
    printable ASCII, so no filename can end the header value early."""
    fallback = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in (filename or "download"))
    fallback = fallback.strip() or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename or 'download', safe='')}"


def safe_headers(*, filename: str, etag: Optional[str]) -> Dict[str, str]:
    headers = {
        "Content-Disposition": content_disposition(filename),
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
        "Cache-Control": "private, no-store",
    }
    if etag:
        headers["ETag"] = f'"{etag}"'
    return headers


def _etag_matches(header: Optional[str], etag: Optional[str]) -> bool:
    if not header or not etag:
        return False
    wanted = f'"{etag}"'
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*" or candidate == wanted or candidate == f"W/{wanted}":
            return True
    return False


class _OpenedStream(StreamingResponse):
    """A StreamingResponse that owns an open descriptor and closes it however
    the response ends (finished, client gone, cancelled)."""

    def __init__(self, fd: int, start: int, length: int, **kwargs) -> None:
        self._fd = fd
        super().__init__(self._chunks(start, length), **kwargs)

    async def _chunks(self, start: int, length: int) -> AsyncIterator[bytes]:
        offset, remaining = start, length
        try:
            while remaining > 0:
                chunk = await anyio.to_thread.run_sync(os.pread, self._fd, min(READ_BYTES, remaining), offset)
                if not chunk:
                    break
                offset += len(chunk)
                remaining -= len(chunk)
                yield chunk
        finally:
            self._close()

    def _close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._close()


def file_response(
    path: str,
    *,
    filename: str,
    etag: Optional[str],
    range_header: Optional[str] = None,
    if_none_match: Optional[str] = None,
    media_type: str = "application/octet-stream",
    missing: Optional[wire.FilesApiError] = None,
) -> Tuple[Response, int, Optional[Tuple[int, int]]]:
    """The response for `path`, plus the bytes it will send and the range.

    Raises `missing` (default `file_not_found`) when the path cannot be
    opened, and the 416 error for an unsatisfiable range."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        raise (missing or wire.file_not_found()) from None
    # EXACTLY ONE close per open, on every path. Until 2026-09-13 the 416 path
    # closed the descriptor and then the `except` closed the same NUMBER again
    # — which, in this shared worker-thread pool, can by then belong to another
    # request's socket, part file or download (review, reproduced). Ownership
    # passes to `_OpenedStream` only once it is constructed.
    owned = True
    try:
        size = os.fstat(fd).st_size
        headers = safe_headers(filename=filename, etag=etag)
        if _etag_matches(if_none_match, etag):
            return Response(status_code=304, headers=headers), 0, None
        wanted = parse_range(range_header, size)
        if wanted == UNSATISFIABLE:
            raise wire.range_not_satisfiable(size)
        if wanted is None:
            start, length, status = 0, size, 200
        else:
            start, end = wanted  # type: ignore[misc]
            length, status = end - start + 1, 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        headers["Content-Length"] = str(length)
        response = _OpenedStream(fd, start, length, status_code=status, headers=headers, media_type=media_type)
        owned = False
        return response, length, (None if wanted is None else wanted)  # type: ignore[return-value]
    finally:
        if owned:
            try:
                os.close(fd)
            except OSError:
                pass
