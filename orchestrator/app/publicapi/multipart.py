"""A bounded, in-memory `multipart/form-data` reader for `/v1/audio/transcriptions`.

WHY NOT `request.form()` / `UploadFile` (2026-09-13). Starlette parses a
multipart body into `UploadFile`s backed by a `SpooledTemporaryFile` whose
rollover size is a class attribute fixed at 1 MB, so every clip longer than a
minute or so is written to the container's disk before a handler sees a byte
of it. `app/audio_api.py` documents the promise this platform makes about
audio — it lives in this process's memory for the length of one call and
nowhere else — and a public endpoint must not be the place that promise
quietly stops being true. It also cannot be capped per route: the form parser
reads the whole body, and a 2 GB upload is 2 GB on disk before anything says
no.

WHAT THIS DOES INSTEAD. The body is read chunk by chunk from
`request.stream()` and pushed straight into `python_multipart`'s callback
parser. Nothing keeps the raw chunks: a file part's bytes are appended to ONE
`bytearray` as the parser hands out slices of the chunk in hand, so the audio
exists in memory once (not once as a body and again as a part), and every cap
is enforced WHILE reading:

* the whole body (`max_body_bytes`) — 413 `request_too_large`;
* each file part (`max_file_bytes`) — 413 `request_too_large`;
* each text field (`max_field_bytes`), the number of parts, and each part's
  header block — 400, because a 5 KB `language` field is a malformed request,
  not a big one.

A malformed body (no boundary, a truncated part, a part with no name) is a
400 with a fixed sentence: the parser's own message quotes offsets into the
caller's bytes, and CONTRACT §16 says we do not echo a body.

Pure: no FastAPI, no database, no settings. The route passes the caps in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, List, Optional, Tuple

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
    """One uploaded file, held in memory once."""

    name: str
    filename: str
    content_type: str
    data: bytearray = field(default_factory=bytearray)

    @property
    def size(self) -> int:
        return len(self.data)


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
    ) -> None:
        self.form = FormData()
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
            self._file = FilePart(name=name, filename=filename or "", content_type=content_type)
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
                # 25 MiB buffer held until the socket drains helps nobody.
                self._file.data = bytearray()
                return
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
    try:
        parser.finalize()
    except Exception:  # noqa: BLE001
        raise _malformed() from None
    if collector.failure is not None:
        raise collector.failure
    if not collector.ended:
        # The closing boundary never arrived: a truncated upload, which must
        # not be transcribed as if it were the whole recording.
        raise _malformed("The multipart/form-data body ended before its closing boundary.")
    collector.form.body_bytes = total
    return collector.form
