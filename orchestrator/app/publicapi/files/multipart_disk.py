"""A streaming `multipart/form-data` reader that writes the ONE file part to disk.

WHY NOT `request.form()` / `UploadFile` (design §10.1, "memory exhaustion via
buffered bodies"). Starlette spools any part over 1 MB to a
`SpooledTemporaryFile` in the container's /tmp (the writable image layer, not
the data volume) and parses the whole body before a handler can refuse it. And
`publicapi/multipart.py` is in-memory BY DESIGN (26 MiB audio). A 64 MiB part
through either costs 64 MiB of RSS or of container layer per request; eight
concurrent parts are half a gigabyte.

WHAT THIS DOES. The body is read chunk by chunk from `request.stream()` into
python-multipart's callback parser. The file part's bytes go straight to a
temporary file on the files volume, hashed (sha256, and md5 when asked) as they
pass; text fields are kept in memory under a small cap. Every cap is enforced
while reading — the whole body, the file part, each field, the field count —
and NEVER from `Content-Length` alone (a chunked body declares nothing, a lying
one declares whatever it likes). Field order does not matter: openai-node sends
the file first and openai-python last (parity capture, 2026-09-13).

On ANY failure — a cap, a malformed body, the client going away — the
temporary file is removed before the exception leaves `read_form_to_disk`.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from typing import AsyncIterator, Dict, Optional, Tuple

from . import wire

try:  # python-multipart >= 0.0.13 ships the `python_multipart` import name
    from python_multipart.multipart import MultipartParser, parse_options_header
except ModuleNotFoundError:  # pragma: no cover - the older spelling
    from multipart.multipart import MultipartParser, parse_options_header  # type: ignore

#: design §2.3: text fields ≤ 64 KiB each, at most 8 fields, exactly one file.
MAX_FIELD_BYTES = 64 * 1024
MAX_FIELDS = 8
MAX_PART_HEADER_BYTES = 4 * 1024


class ClientGone(Exception):
    """The body ended before its closing boundary because the client left."""


@dataclass
class DiskFile:
    name: str
    filename: Optional[str]
    content_type: str
    path: str
    bytes: int = 0
    sha256: str = ""
    md5: Optional[str] = None


@dataclass
class DiskForm:
    fields: Dict[str, str] = field(default_factory=dict)
    file: Optional[DiskFile] = None
    body_bytes: int = 0


def boundary_of(content_type: Optional[str]) -> bytes:
    kind, options = parse_options_header(content_type or "")
    if kind.lower() != b"multipart/form-data":
        raise wire.invalid_request("This endpoint accepts multipart/form-data only.", param="Content-Type")
    boundary = options.get(b"boundary") or b""
    if not boundary or len(boundary) > 70:
        raise wire.invalid_request("The multipart/form-data body has no valid boundary.")
    return boundary


def _malformed(message: str = "The request body is not valid multipart/form-data.") -> wire.FilesApiError:
    return wire.invalid_request(message)


class _DiskCollector:
    def __init__(
        self,
        *,
        file_field: str,
        tmp_path: str,
        max_file_bytes: int,
        too_large_message: Optional[str],
        want_md5: bool,
    ) -> None:
        self.form = DiskForm()
        self.failure: Optional[wire.FilesApiError] = None
        self.ended = False
        self._file_field = file_field
        self._tmp_path = tmp_path
        self._max_file = int(max_file_bytes)
        self._too_large_message = too_large_message
        self._want_md5 = want_md5
        self._parts = 0
        self._header_bytes = 0
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._headers: Dict[str, bytes] = {}
        self._field_name: Optional[str] = None
        self._field_value = bytearray()
        self._in_file = False
        self._handle = None
        self._sha = hashlib.sha256()
        self._md5 = hashlib.md5() if want_md5 else None
        self._skip = False

    def _fail(self, failure: wire.FilesApiError) -> None:
        if self.failure is None:
            self.failure = failure
        self._skip = True

    def close_file(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None

    def on_part_begin(self) -> None:
        self._parts += 1
        if self._parts > MAX_FIELDS + 1:
            self._fail(_malformed("The multipart/form-data body has too many parts."))
        self._header_bytes = 0
        self._headers = {}
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._field_name = None
        self._field_value = bytearray()
        self._in_file = False

    def _count_header(self, n: int) -> bool:
        self._header_bytes += n
        if self._header_bytes > MAX_PART_HEADER_BYTES:
            self._fail(_malformed("A multipart part header is too large."))
            return False
        return True

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        if not self._skip and self._count_header(end - start):
            self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        if not self._skip and self._count_header(end - start):
            self._header_value += data[start:end]

    def on_header_end(self) -> None:
        if self._skip:
            return
        self._headers[bytes(self._header_field).decode("latin-1").strip().lower()] = bytes(self._header_value).strip()
        self._header_field = bytearray()
        self._header_value = bytearray()

    def on_headers_finished(self) -> None:
        if self._skip:
            return
        kind, options = parse_options_header(self._headers.get("content-disposition", b""))
        name = options.get(b"name")
        if kind.lower() != b"form-data" or name is None:
            self._fail(_malformed("Every multipart part must be form-data with a name."))
            return
        name_text = name.decode("utf-8", "replace")
        filename = options.get(b"filename")
        if filename is not None or name_text == self._file_field:
            if name_text != self._file_field:
                self._fail(wire.invalid_request(f"Unexpected file field; send the file as `{self._file_field}`.", param=name_text))
                return
            if self.form.file is not None:
                self._fail(_malformed("Send exactly one file part."))
                return
            content_type = self._headers.get("content-type", b"").decode("latin-1").split(";")[0].strip().lower()
            try:
                self._handle = open(self._tmp_path, "wb")
            except OSError:
                self._fail(wire.storage_unavailable())
                return
            self.form.file = DiskFile(
                name=name_text,
                filename=filename.decode("utf-8", "replace") if filename is not None else None,
                content_type=content_type,
                path=self._tmp_path,
            )
            self._in_file = True
            return
        if name_text in self.form.fields:
            self._fail(_malformed("A form field appears more than once."))
            return
        self._field_name = name_text

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._skip:
            return
        n = end - start
        if self._in_file and self.form.file is not None:
            record = self.form.file
            if record.bytes + n > self._max_file:
                self._fail(wire.request_too_large(self._max_file, self._too_large_message))
                return
            chunk = memoryview(data)[start:end]
            self._sha.update(chunk)
            if self._md5 is not None:
                self._md5.update(chunk)
            self._handle.write(chunk)
            record.bytes += n
            return
        if self._field_name is not None:
            if len(self._field_value) + n > MAX_FIELD_BYTES:
                self._fail(wire.invalid_request("A form field is longer than this endpoint accepts.", param=self._field_name))
                return
            self._field_value += data[start:end]

    def on_part_end(self) -> None:
        if self._skip:
            return
        if self._in_file and self.form.file is not None:
            self.form.file.sha256 = self._sha.hexdigest()
            self.form.file.md5 = self._md5.hexdigest() if self._md5 is not None else None
            self._in_file = False
            return
        if self._field_name is not None:
            try:
                self.form.fields[self._field_name] = bytes(self._field_value).decode("utf-8")
            except UnicodeDecodeError:
                self._fail(wire.invalid_request("A form field is not valid UTF-8.", param=self._field_name))
                return
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


def _disconnect_types() -> Tuple[type, ...]:
    try:
        from starlette.requests import ClientDisconnect

        return (ClientDisconnect,)
    except Exception:  # pragma: no cover
        return ()


async def read_form_to_disk(
    chunks: AsyncIterator[bytes],
    content_type: Optional[str],
    *,
    tmp_path: str,
    file_field: str,
    max_body_bytes: int,
    max_file_bytes: int,
    declared_length: Optional[str] = None,
    too_large_message: Optional[str] = None,
    want_md5: bool = False,
) -> DiskForm:
    """Parse the body; the file part lands at `tmp_path` (fsynced). Raises a
    `FilesApiError` (400/413) or `ClientGone`, always after removing
    `tmp_path`. A body with no file part returns `form.file = None` and no
    file on disk — the caller decides whether that is a 400."""
    limit = int(max_body_bytes)
    declared = wire.ascii_int(declared_length)
    if declared is not None and declared > limit:
        raise wire.request_too_large(limit, too_large_message)
    boundary = boundary_of(content_type)
    os.makedirs(os.path.dirname(tmp_path), mode=0o750, exist_ok=True)
    collector = _DiskCollector(
        file_field=file_field,
        tmp_path=tmp_path,
        max_file_bytes=max_file_bytes,
        too_large_message=too_large_message,
        want_md5=want_md5,
    )
    parser = MultipartParser(boundary, collector.callbacks())
    total = 0
    disconnects = _disconnect_types()
    try:
        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    raise wire.request_too_large(limit, too_large_message)
                try:
                    parser.write(chunk)
                except wire.FilesApiError:
                    raise
                except Exception:  # noqa: BLE001 - the parser's text quotes the body
                    raise _malformed() from None
                if collector.failure is not None:
                    raise collector.failure
        except disconnects:
            raise ClientGone() from None
        try:
            parser.finalize()
        except Exception:  # noqa: BLE001
            raise _malformed() from None
        if collector.failure is not None:
            raise collector.failure
        if not collector.ended:
            raise _malformed("The multipart/form-data body ended before its closing boundary.")
        handle = collector._handle
        if handle is not None:
            handle.flush()
            await asyncio.to_thread(os.fsync, handle.fileno())
        collector.close_file()
    except BaseException:
        collector.close_file()
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    collector.form.body_bytes = total
    return collector.form
