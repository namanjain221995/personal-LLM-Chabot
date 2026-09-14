"""Byte-level primitives for resumable uploads: one part to disk, many parts
into one file. No HTTP, no database, no settings.

LIFTED FROM THE V29 CHAT RAIL (2026-09-13, Files API design §3.3). The
algorithm is `uploads.chunked_part` / `uploads._assemble`, which production has
run since 2026-09-10; the public Files API needs the same guarantees with
different framing, and two copies of "stream, hash, fsync, rename" would drift.
`uploads.py` is not switched onto this module in this wave (it is another
team's file); that delegation is an integration item gated on
`tests/test_uploads_resumable.py` and `tests/test_document_uploads.py`
passing unchanged (design §3.3).

THE GUARANTEES.

1. A part is written to `<final>.<nonce>.tmp`, hashed on the way in, fsynced
   off the event loop, and NOT renamed here. A body cut short, over its cap,
   refused by the caller's budget, or failing its declared digest leaves NO
   file: the temporary file is removed before the exception leaves this
   module. The caller re-reads its state row (a `complete` or `cancel` may have
   won while the body streamed) and only then calls `commit_part`.
2. `assemble` never exposes a short file: it writes `<dest>.assembling`,
   fsyncs, then renames. A crash mid-way leaves the `.assembling` file, never
   a `dest` a reader could mistake for the whole.
3. sha256 (and md5 when asked) of the assembled bytes come from the SAME pass
   that copies them — a 100 GiB assembly is one read of the parts, not two.

MEASURED (2026-09-13, this host): sha256 2,365 MiB/s on one core, md5 ~620
MiB/s, fsynced sequential write 260 MB/s; eight concurrent 64 MiB parts
through `request.stream()` + sha256 + fsync held 52 MiB RSS with an event-loop
p99 of 8.5 ms.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional, Sequence

#: The copy buffer. 1 MiB is V29's `_CHUNK`: large enough that hashing
#: releases the GIL for most of the time (hashlib drops it above 2,047 bytes),
#: small enough that a stop request is noticed within a millisecond.
COPY_BUFFER_BYTES = 1024 * 1024


class PartError(Exception):
    """Base of every refusal `write_part_stream` raises. The temporary file is
    already gone when any of these reaches the caller."""


class PartTooLarge(PartError):
    def __init__(self, cap_bytes: int) -> None:
        super().__init__(f"part exceeds {int(cap_bytes)} bytes")
        self.cap_bytes = int(cap_bytes)


class PartDigestMismatch(PartError):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__("part digest mismatch")
        self.expected = expected
        self.actual = actual


class PartIncomplete(PartError):
    """The client went away before the body ended (`ClientDisconnect`), or the
    body ended short of a declared length."""

    def __init__(self, bytes_written: int) -> None:
        super().__init__(f"body ended after {int(bytes_written)} bytes")
        self.bytes_written = int(bytes_written)


class AssemblyError(Exception):
    """A part file is missing or its size differs from what was recorded, or
    the caller asked the copy to stop. `dest.assembling` is removed."""


class AssemblyStopped(AssemblyError):
    pass


class PartChecksumMismatch(AssemblyError):
    """A part file's bytes on disk do not hash to the sha256 its row recorded.

    Found by review (2026-09-13): a failure AFTER a retried part's rename but
    before its row committed left the new bytes on disk under the old row, and
    assembly stitched them in silently. Permanent — retrying reads the same
    bytes — so the caller fails the file instead of deferring."""

    def __init__(self, index: int) -> None:
        super().__init__(f"part {int(index)} does not match its recorded sha256")
        self.index = int(index)


def _disconnect_types() -> tuple:
    try:
        from starlette.requests import ClientDisconnect

        return (ClientDisconnect,)
    except Exception:  # pragma: no cover - starlette is always installed here
        return ()


@dataclass(frozen=True)
class PartWritten:
    """A part on disk under its temporary name, not yet committed."""

    path: str
    bytes: int
    sha256: str
    md5: Optional[str] = None


def tmp_path_for(final_path: str) -> str:
    return f"{final_path}.{uuid.uuid4().hex[:16]}.tmp"


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


async def write_part_stream(
    stream: AsyncIterator[bytes],
    *,
    final_path: str,
    cap_bytes: int,
    live_budget: Callable[[int], None] = lambda n: None,
    declared_sha256: Optional[str] = None,
    expected_bytes: Optional[int] = None,
    want_md5: bool = False,
    tmp_path: Optional[str] = None,
) -> PartWritten:
    """Stream `stream` to `<final_path>.<nonce>.tmp` (or `tmp_path`).

    `live_budget(n)` is called with the running byte count after every chunk
    and may raise to refuse (a disk-based part budget, the watermark); its
    exception propagates after the temporary file is removed.
    `expected_bytes` (a `Content-Length`) turns a body that ends short into
    `PartIncomplete` rather than a smaller part.
    """
    cap = int(cap_bytes)
    tmp = tmp_path or tmp_path_for(final_path)
    os.makedirs(os.path.dirname(tmp) or ".", exist_ok=True)
    digest = hashlib.sha256()
    md5 = hashlib.md5() if want_md5 else None
    written = 0
    disconnects = _disconnect_types()
    try:
        with open(tmp, "wb") as out:
            try:
                async for chunk in stream:
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > cap:
                        raise PartTooLarge(cap)
                    live_budget(written)
                    digest.update(chunk)
                    if md5 is not None:
                        md5.update(chunk)
                    out.write(chunk)
            except disconnects:
                raise PartIncomplete(written) from None
            if expected_bytes is not None and written != int(expected_bytes):
                raise PartIncomplete(written)
            out.flush()
            # fsync in a thread: 64 MiB of dirty pages can take hundreds of
            # milliseconds to flush, which on the loop stalls every chat stream.
            await asyncio.to_thread(os.fsync, out.fileno())
    except BaseException:
        _unlink_quietly(tmp)
        raise
    actual = digest.hexdigest()
    if declared_sha256 is not None and actual != declared_sha256.lower():
        _unlink_quietly(tmp)
        raise PartDigestMismatch(declared_sha256.lower(), actual)
    return PartWritten(path=tmp, bytes=written, sha256=actual, md5=md5.hexdigest() if md5 else None)


def commit_part(written: PartWritten, final_path: str) -> None:
    """`os.replace(tmp, final)` then fsync the directory, so a part the
    database now lists survives a power cut. Replacing an existing part (a
    retried `part_number`) is atomic: a reader sees the old bytes or the new."""
    os.replace(written.path, final_path)
    directory = os.path.dirname(final_path) or "."
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def discard_part(written: PartWritten) -> None:
    _unlink_quietly(written.path)


@dataclass(frozen=True)
class Assembled:
    path: str
    bytes: int
    sha256: str
    md5: Optional[str]


def assemble(
    part_paths: Sequence[str],
    dest_path: str,
    *,
    want_md5: bool,
    expected_sizes: Optional[Sequence[int]] = None,
    expected_sha256s: Optional[Sequence[Optional[str]]] = None,
    on_progress: Optional[Callable[[int], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    progress_every_bytes: int = 256 * 1024 * 1024,
) -> Assembled:
    """Concatenate `part_paths` in order into `dest_path`. BLOCKING: call via
    `asyncio.to_thread` — the pre-V29 `complete` did this on the event loop and
    a 400 MB video stalled every other request (review F-02).

    `expected_sizes[i]` is the byte count the database recorded for part i; a
    part whose file is a different size is an `AssemblyError` (the rows are
    the truth, and a truncated part must not be stitched in as if whole).
    `expected_sha256s[i]` (when given and not None) is the digest the database
    recorded for part i; a part whose bytes hash differently raises
    `PartChecksumMismatch` (one extra sha256 per part in the same pass;
    measured 2026-09-13 on this host, 1 GiB from 16 x 64 MiB parts with the
    final fsync: 2.91 s → 2.83 s sha-only and 4.70 s → 4.77 s with md5 on the
    warm run — inside run-to-run noise, which the fsync dominates).
    `on_progress(bytes_done)` runs every `progress_every_bytes` and at the end.
    `should_stop()` is consulted after EVERY copy buffer (1 MiB) and aborts
    with `AssemblyStopped` (a deleted file, a lost lease, shutdown): it must be
    cheap — an Event check, never a query. Until 2026-09-13 it ran only every
    256 MiB, so a shutdown waited for up to 256 MiB of copying (review).
    """
    if expected_sizes is not None and len(expected_sizes) != len(part_paths):
        raise ValueError("expected_sizes must name one size per part")
    if expected_sha256s is not None and len(expected_sha256s) != len(part_paths):
        raise ValueError("expected_sha256s must name one digest per part")
    tmp = dest_path + ".assembling"
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    digest = hashlib.sha256()
    md5 = hashlib.md5() if want_md5 else None
    total = 0
    next_tick = int(progress_every_bytes)
    buffer = bytearray(COPY_BUFFER_BYTES)
    view = memoryview(buffer)
    try:
        with open(tmp, "wb") as out:
            for index, path in enumerate(part_paths):
                try:
                    handle = open(path, "rb")
                except FileNotFoundError:
                    raise AssemblyError(f"part {index} is missing on disk") from None
                with handle:
                    size = os.fstat(handle.fileno()).st_size
                    if expected_sizes is not None and size != int(expected_sizes[index]):
                        raise AssemblyError(f"part {index} has the wrong size on disk")
                    wanted = expected_sha256s[index] if expected_sha256s is not None else None
                    part_digest = hashlib.sha256() if wanted else None
                    while True:
                        n = handle.readinto(buffer)
                        if not n:
                            break
                        chunk = view[:n]
                        digest.update(chunk)
                        if md5 is not None:
                            md5.update(chunk)
                        if part_digest is not None:
                            part_digest.update(chunk)
                        out.write(chunk)
                        total += n
                        if should_stop is not None and should_stop():
                            raise AssemblyStopped("assembly was asked to stop")
                        if total >= next_tick:
                            next_tick = total + int(progress_every_bytes)
                            if on_progress is not None:
                                on_progress(total)
                    if part_digest is not None and part_digest.hexdigest() != str(wanted).lower():
                        raise PartChecksumMismatch(index)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, dest_path)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    if on_progress is not None:
        on_progress(total)
    return Assembled(
        path=dest_path,
        bytes=total,
        sha256=digest.hexdigest(),
        md5=md5.hexdigest() if md5 is not None else None,
    )
