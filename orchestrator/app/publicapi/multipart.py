"""A bounded, streaming `multipart/form-data` reader for `/v1/audio/transcriptions`.

WHY NOT `request.form()` / `UploadFile` (2026-09-13). Starlette parses a
multipart body into `UploadFile`s backed by a `SpooledTemporaryFile` whose
rollover size is a class attribute fixed at 1 MB, in the container's temporary
directory, with no per-route cap: the form parser reads the whole body, and a
2 GB upload is 2 GB on disk before anything says no.

WHAT THIS DOES INSTEAD. The body is read chunk by chunk from
`request.stream()` and pushed straight into `python_multipart`'s callback
parser. Nothing keeps the raw chunks, and every cap is enforced WHILE reading:

* the whole body (`max_body_bytes`) — 413 `request_too_large`;
* each file part (`max_file_bytes`) — 413 `request_too_large`;
* each text field (`max_field_bytes`), the number of parts, and each part's
  header block — 400, because a 5 KB `language` field is a malformed request,
  not a big one.

WHERE A FILE PART GOES. With a `file_writer` (the transcription route, since
the no-timeout design of 2026-09-13: audio of any length, up to 89 MiB in one
request), the part's bytes are handed to it as they arrive — the route streams
them into a private file under the disk ledger (`disk_ledger.DiskSink`), so at
most one body chunk of audio is ever in this process's memory. Without one
(the default), the part lands in ONE `bytearray` — memory once, not once as a
body and again as a part — which the tests and small callers use.

A malformed body (no boundary, a truncated part, a part with no name) is a
400 with a fixed sentence: the parser's own message quotes offsets into the
caller's bytes, and CONTRACT §16 says we do not echo a body.

Pure: no FastAPI, no database, no settings. The route passes the caps in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Dict, List, Optional, Protocol, Tuple

from . import errors

try:  # python-multipart >= 0.0.13 ships the `python_multipart` import name
    from python_multipart.multipart import MultipartParser, parse_options_header
except ModuleNotFoundError:  # pragma: no cover - the older spelling
    from multipart.multipart import MultipartParser, parse_options_header  # type: ignore

#: One part's header block. Real clients send two short headers
#: (`Content-Disposition`, `Content-Type`); 4 KiB is generous, sits just under
#: python-multipart's own 4,224-byte header cap (newer releases; older ones
#: have none), and stops a body made of one endless header from growing
#: without bound whichever release is installed.
MAX_PART_HEADER_BYTES = 4 * 1024

#: How many parts a transcription form may have. The endpoint accepts five
#: fields plus a repeatable `timestamp_granularities[]`; sixteen leaves room and
#: still refuses a body of ten thousand empty parts (each a callback and a
#: dict entry).
DEFAULT_MAX_PARTS = 16

#: A text field (`model`, `language`, `response_format`) is a word, not a
#: document.
DEFAULT_MAX_FIELD_BYTES = 1024


@dataclass
class FilePart:
    """One uploaded file: held in memory once (`data`), or handed to a
    `FileWriter` as it arrived (`data` stays empty; `received` counts it)."""

    name: str
    filename: str
    content_type: str
    data: bytearray = field(default_factory=bytearray)
    received: int = 0
    streamed: bool = False

    @property
    def size(self) -> int:
        return self.received if self.streamed else len(self.data)


class FileWriter(Protocol):
    """Where a streamed file part goes. `start` is awaited once, before the
    part's first byte; `write` for every slice in order. Raising from either
    stops the read and propagates (a disk refusal is the route's answer)."""

    def start(self, part: FilePart) -> Awaitable[None]: ...

    def write(self, data: bytes) -> Awaitable[None]: ...


@dataclass
class FormData:
    """What the body said. `fields[name]` is a list because a field whose name
    ends in `[]` may repeat (`timestamp_granularities[]`); every other name is
    refused on a second appearance, since which of two `model` values counts is
    a guess this reader will not make."""

    fields: Dict[str, List[str]] = field(default_factory=dict)
    files: Dict[str, List[FilePart]] = field(default_factory=dict)
    body_bytes: int = 0

    def field_value(self, name: str) -> Optional[str]:
        values = self.fields.get(name)
        return values[0] if values else None

    def file(self, name: str) -> Optional[FilePart]:
        values = self.files.get(name)
        return values[0] if values else None


def _malformed(message: str = "The request body is not valid multipart/form-data.") -> errors.ApiError:
    return errors.invalid_request(message)


def boundary_of(content_type: Optional[str]) -> bytes:
    """The multipart boundary from a `Content-Type` header, or a 400.

    RFC 2046 §5.1.1 limits a boundary to 70 characters; a longer one is not a
    boundary any conforming client produced.
    """
    kind, options = parse_options_header(content_type or "")
    if kind.lower() != b"multipart/form-data":
        raise errors.invalid_request(
            "This endpoint accepts multipart/form-data only.", param="Content-Type"
        )
    boundary = options.get(b"boundary") or b""
    if not boundary or len(boundary) > 70:
        raise _malformed("The multipart/form-data body has no valid boundary.")
    return boundary


def _disposition(raw: bytes) -> Tuple[Optional[str], Optional[str]]:
    """(`name`, `filename`) from a `Content-Disposition` value."""
    kind, options = parse_options_header(raw)
    if kind.lower() != b"form-data":
        return None, None
    name = options.get(b"name")
    filename = options.get(b"filename")
    return (
        name.decode("utf-8", "replace") if name is not None else None,
        filename.decode("utf-8", "replace") if filename is not None else None,
    )


class _Collector:
    """The parser's callbacks, with every cap checked where the bytes arrive.

    A callback cannot usefully raise through `python_multipart` on every
    version (some wrap or swallow), so a refusal is recorded here and raised by
    the reading loop right after the `write` that caused it.
    """

    def __init__(
        self,
        *,
        max_file_bytes: int,
        max_field_bytes: int,
        max_parts: int,
        file_fields: frozenset,
        streaming: bool = False,
    ) -> None:
        self.form = FormData()
        #: Streaming mode: the file part's bytes of the chunk in hand, and a
        #: part whose `start` the reading loop has not yet awaited.
        self._streaming = streaming
        self.pending_start: Optional[FilePart] = None
        self.pending_bytes = bytearray()
        self.failure: Optional[errors.ApiError] = None
        self.ended = False
        self._max_file = int(max_file_bytes)
        self._max_field = int(max_field_bytes)
        self._max_parts = int(max_parts)
        self._file_fields = file_fields
        self._parts = 0
        self._header_bytes = 0
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._headers: Dict[str, bytes] = {}
        self._file: Optional[FilePart] = None
        self._field_name: Optional[str] = None
        self._field_value = bytearray()
        self._skip = False

    # -- helpers ----------------------------------------------------------

    def _fail(self, failure: errors.ApiError) -> None:
        if self.failure is None:
            self.failure = failure
        self._skip = True

    # -- callbacks --------------------------------------------------------

    def on_part_begin(self) -> None:
        self._parts += 1
        if self._parts > self._max_parts:
            self._fail(_malformed("The multipart/form-data body has too many parts."))
        self._header_bytes = 0
        self._headers = {}
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._file = None
        self._field_name = None
        self._field_value = bytearray()

    def _count_header(self, n: int) -> bool:
        self._header_bytes += n
        if self._header_bytes > MAX_PART_HEADER_BYTES:
            self._fail(_malformed("A multipart part header is too large."))
            return False
        return True

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        if self._skip or not self._count_header(end - start):
            return
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        if self._skip or not self._count_header(end - start):
            return
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        if self._skip:
            return
        self._headers[bytes(self._header_field).decode("latin-1").strip().lower()] = bytes(
            self._header_value
        ).strip()
        self._header_field = bytearray()
        self._header_value = bytearray()

    def on_headers_finished(self) -> None:
        if self._skip:
            return
        name, filename = _disposition(self._headers.get("content-disposition", b""))
        if not name:
            self._fail(_malformed("Every multipart part must be form-data with a name."))
            return
        if filename is not None or name in self._file_fields:
            if any(self.form.files.values()) or name in self.form.fields:
                self._fail(_malformed("Send exactly one file part."))
                return
            content_type = (
                self._headers.get("content-type", b"").decode("latin-1").split(";")[0].strip().lower()
            )
            self._file = FilePart(
                name=name, filename=filename or "", content_type=content_type, streamed=self._streaming
            )
            if self._streaming:
                self.pending_start = self._file
            return
        if (name in self.form.fields and not name.endswith("[]")) or name in self.form.files:
            self._fail(_malformed("A form field appears more than once."))
            return
        self._field_name = name

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._skip:
            return
        n = end - start
        if self._file is not None:
            if self._file.size + n > self._max_file:
                self._fail(errors.request_too_large(self._max_file))
                # Let go of what was collected: the refusal is decided, and a
                # large buffer held until the socket drains helps nobody.
                self._file.data = bytearray()
                self.pending_bytes = bytearray()
                return
            if self._streaming:
                self._file.received += n
                self.pending_bytes += data[start:end]
            else:
                self._file.data += data[start:end]
            return
        if self._field_name is not None:
            if len(self._field_value) + n > self._max_field:
                self._fail(
                    errors.invalid_request(
                        "A form field is longer than this endpoint accepts.",
                        param=self._field_name,
                    )
                )
                return
            self._field_value += data[start:end]

    def on_part_end(self) -> None:
        if self._skip:
            return
        if self._file is not None:
            self.form.files.setdefault(self._file.name, []).append(self._file)
            self._file = None
            return
        if self._field_name is not None:
            try:
                value = bytes(self._field_value).decode("utf-8")
            except UnicodeDecodeError:
                self._fail(
                    errors.invalid_request("A form field is not valid UTF-8.", param=self._field_name)
                )
                return
            self.form.fields.setdefault(self._field_name, []).append(value)
            self._field_name = None

    def on_end(self) -> None:
        self.ended = True

    def callbacks(self) -> dict:
        return {
            "on_part_begin": self.on_part_begin,
            "on_part_data": self.on_part_data,
            "on_part_end": self.on_part_end,
            "on_header_field": self.on_header_field,
            "on_header_value": self.on_header_value,
            "on_header_end": self.on_header_end,
            "on_headers_finished": self.on_headers_finished,
            "on_end": self.on_end,
        }


async def read_form(
    chunks: AsyncIterator[bytes],
    content_type: Optional[str],
    *,
    max_body_bytes: int,
    max_file_bytes: int,
    max_field_bytes: int = DEFAULT_MAX_FIELD_BYTES,
    max_parts: int = DEFAULT_MAX_PARTS,
    file_fields: frozenset = frozenset({"file"}),
    declared_length: Optional[str] = None,
    file_writer: Optional[FileWriter] = None,
) -> FormData:
    """Read and parse a `multipart/form-data` body under every cap, or raise.

    `declared_length` is the request's `Content-Length`: over the body cap it
    is refused before one byte is read (the cheap refusal). The bytes that
    actually arrive are counted too, because a chunked body declares nothing
    and a lying one declares whatever it likes.

    Once a cap is exceeded the reader STOPS pulling the body and raises; the
    server closes the connection on the rest, which is the only way to stop
    paying for an upload that has already been refused.
    """
    limit = int(max_body_bytes)
    if declared_length and declared_length.strip().isdigit() and int(declared_length) > limit:
        raise errors.request_too_large(limit)
    boundary = boundary_of(content_type)
    collector = _Collector(
        max_file_bytes=max_file_bytes,
        max_field_bytes=max_field_bytes,
        max_parts=max_parts,
        file_fields=file_fields,
        streaming=file_writer is not None,
    )
    parser = MultipartParser(boundary, collector.callbacks())
    total = 0
    async for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise errors.request_too_large(limit)
        try:
            parser.write(chunk)
        except errors.ApiError:
            raise
        except Exception:  # noqa: BLE001 - the parser's text quotes the body
            raise _malformed() from None
        if collector.failure is not None:
            raise collector.failure
        if file_writer is not None:
            await _hand_over(collector, file_writer)
    try:
        parser.finalize()
    except Exception:  # noqa: BLE001
        raise _malformed() from None
    if collector.failure is not None:
        raise collector.failure
    if file_writer is not None:
        await _hand_over(collector, file_writer)
    if not collector.ended:
        # The closing boundary never arrived: a truncated upload, which must
        # not be transcribed as if it were the whole recording.
        raise _malformed("The multipart/form-data body ended before its closing boundary.")
    collector.form.body_bytes = total
    return collector.form


async def _hand_over(collector: _Collector, writer: FileWriter) -> None:
    """The streamed part's news from the chunk just parsed, in order: its
    start (once), then its bytes. Synchronous parser callbacks cannot await,
    so they queue; this drains the queue between chunks."""
    if collector.pending_start is not None:
        part, collector.pending_start = collector.pending_start, None
        await writer.start(part)
    if collector.pending_bytes:
        data = bytes(collector.pending_bytes)
        collector.pending_bytes = bytearray()
        await writer.write(data)
