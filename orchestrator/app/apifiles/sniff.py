"""What a file IS, from its bytes — never from what the client said (design §4.1).

WHY NOT THE CLIENT'S TYPE. The parity capture of 2026-09-13 showed openai-python
sending `application/octet-stream` for every in-memory part and openai-node
sending it for every stream, while `POST /v1/uploads` requires a `mime_type`
that nothing checks. A type taken from the request is therefore wrong for most
real traffic, and a declared type is not evidence of what a parser will be
handed. The client's `mime_type`, part `Content-Type` and filename extension
are HINTS, used only to break ties between text formats.

WHAT IS READ. The first 64 KiB, the last 64 KiB (parquet's trailing `PAR1`, the
zip end-of-central-directory record) and, for a zip, its central directory —
names only, bounded at 16 MiB, parsed here rather than by `zipfile` so a
100 GiB upload with a hostile directory costs a bounded read. No member is ever
decompressed.

WHAT THIS DOES NOT DECIDE. For media containers the magic says "a media
container", not "audio" or "video": the `probe` stage runs `ffprobe` with the
hardened argument set (team B, design §6.4) and has the last word. `needs_probe`
says so; `kind` here is only the best guess a File object can show before that.

`#EXTM3U` and `ffconcat` are ALWAYS `unsupported`, whatever the name says: a
file whose content is a list of other inputs is not an input this API accepts
(design §4.1). The check runs on the bytes after any leading ID3v2 tags too,
because ffmpeg's probe skips those tags before it picks a demuxer (review,
2026-09-13, reproduced with ffmpeg 7.0.2).

`detect` IS TOTAL. Any exception inside classification means "not this
format" and yields `unknown`, never an exception: the caller is an upload
route (a 500 the SDKs retry) or the `assemble` stage (which, until 2026-09-13,
leaked a full-size assembled copy per attempt when a sniff raised).
"""
from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from typing import List, Optional, Sequence

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 64 * 1024
#: The central directory of a real DOCX/XLSX/PPTX is a few KiB; a 16 MiB bound
#: still admits ~150,000 member names and refuses a directory built to exhaust
#: memory.
MAX_CENTRAL_DIRECTORY_BYTES = 16 * 1024 * 1024
#: NUL bytes in the first 8 KiB mean binary (the `file`/git heuristic).
NUL_WINDOW = 8 * 1024

KINDS = (
    "unknown", "pdf", "document", "presentation", "spreadsheet", "tabular",
    "text", "html", "image", "audio", "video", "unsupported",
)

#: The fixed step ids of design §4.3, per kind. The SSE stream and the File
#: object use these names; team B's `jobs.processing_view` reads them from here.
STAGES_BY_KIND = {
    "pdf": ("sniff", "text", "ocr", "chunk", "index", "finalize"),
    "document": ("sniff", "text", "chunk", "index", "finalize"),
    "presentation": ("sniff", "text", "chunk", "index", "finalize"),
    "text": ("sniff", "text", "chunk", "index", "finalize"),
    "html": ("sniff", "text", "chunk", "index", "finalize"),
    "spreadsheet": ("sniff", "sheets", "profile", "chunk", "index", "finalize"),
    "tabular": ("sniff", "sheets", "profile", "chunk", "index", "finalize"),
    "image": ("sniff", "decode", "variants", "finalize"),
    "audio": ("sniff", "probe", "audio", "transcript", "fusion", "artifacts", "index", "finalize"),
    "video": ("sniff", "probe", "audio", "transcript", "frames", "ocr", "vision", "fusion", "artifacts", "index", "finalize"),
    "unsupported": ("sniff", "finalize"),
    "unknown": ("sniff", "finalize"),
}

#: The lane a kind is processed in (design §4.2).
MEDIA_KINDS = frozenset({"audio", "video"})

_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_MIME_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_OCTET = "application/octet-stream"

#: `core/archive.REFUSED_SUFFIXES`' macro rule: a macro-enabled container is
#: refused whether it says so in its name or only in its members.
_MACRO_SUFFIXES = (".xlsm", ".xlsb", ".pptm", ".docm", ".xltm", ".potm", ".dotm")


@dataclass(frozen=True)
class Detection:
    kind: str
    mime_type: str
    #: The suffix `derived/src.<ext>` gets (finding #2: openpyxl and DuckDB
    #: choose their reader by suffix). Empty for `unsupported`.
    ext: str
    needs_probe: bool = False
    #: A short machine reason, for logs and tests — never shown on the wire.
    reason: str = ""
    lane: str = "cpu"


def _det(kind: str, mime: str, ext: str, reason: str, *, needs_probe: bool = False) -> Detection:
    return Detection(
        kind=kind,
        mime_type=mime,
        ext=ext,
        needs_probe=needs_probe,
        reason=reason,
        lane="media" if kind in MEDIA_KINDS else "cpu",
    )


# ------------------------------------------------------------------ zip --


def _zip_member_names(read_at, size: int, tail: bytes) -> Optional[List[str]]:
    """Member names from the central directory, or None when the bytes are not
    a readable zip (or the directory is larger than the bound).

    `read_at(offset, length) -> bytes`. The end-of-central-directory record is
    in the last 65,557 bytes (22 + a comment of at most 65,535)."""
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0 or len(tail) - eocd < 22:
        return None
    tail_start = size - len(tail)
    (_sig, _disk, _cd_disk, _n_here, entries, cd_size, cd_offset, _clen) = struct.unpack(
        "<IHHHHIIH", tail[eocd : eocd + 22]
    )
    if cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF or entries == 0xFFFF:
        # zip64: the locator sits immediately before the EOCD record.
        loc = eocd - 20
        if loc < 0 or tail[loc : loc + 4] != b"PK\x06\x07":
            return None
        (_lsig, _ldisk, z64_offset, _ldisks) = struct.unpack("<IIQI", tail[loc : loc + 20])
        record = read_at(z64_offset, 56)
        if len(record) < 56 or record[:4] != b"PK\x06\x06":
            return None
        cd_size, cd_offset = struct.unpack("<QQ", record[40:56])
    if cd_size > MAX_CENTRAL_DIRECTORY_BYTES or cd_offset + cd_size > size:
        return None
    if cd_offset >= tail_start:
        directory = tail[cd_offset - tail_start : cd_offset - tail_start + cd_size]
    else:
        directory = read_at(cd_offset, cd_size)
    names: List[str] = []
    pos = 0
    while pos + 46 <= len(directory):
        if directory[pos : pos + 4] != b"PK\x01\x02":
            break
        name_len, extra_len, comment_len = struct.unpack("<HHH", directory[pos + 28 : pos + 34])
        start = pos + 46
        raw = directory[start : start + name_len]
        names.append(raw.decode("utf-8", "replace"))
        pos = start + name_len + extra_len + comment_len
    return names


def _classify_zip(names: Sequence[str], filename: str) -> Detection:
    lowered = {n.lower() for n in names}
    if filename.lower().endswith(_MACRO_SUFFIXES) or any(n.endswith("vbaproject.bin") for n in lowered):
        return _det("unsupported", _OCTET, "", "macro-container")
    if "word/document.xml" in lowered:
        return _det("document", _MIME_DOCX, "docx", "zip-docx")
    if "ppt/presentation.xml" in lowered:
        return _det("presentation", _MIME_PPTX, "pptx", "zip-pptx")
    if "xl/workbook.xml" in lowered:
        return _det("spreadsheet", _MIME_XLSX, "xlsx", "zip-xlsx")
    # Plain archives and OpenDocument are recorded gaps this wave (R14).
    return _det("unsupported", "application/zip", "", "zip-other")


# ---------------------------------------------------------------- media --


def _ftyp(head: bytes, filename: str) -> Optional[Detection]:
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    brand = head[8:12].decode("latin-1")
    compatible = head[16:64].decode("latin-1", "replace")
    if brand in ("heic", "heix", "hevc", "heim", "heis", "hevm", "hevs", "mif1", "msf1", "avif") or "heic" in compatible or "mif1" in compatible:
        # No pillow-heif in the image: HEIC/HEIF/AVIF are a recorded gap.
        return _det("unsupported", "image/heic", "", "heif")
    if brand in ("M4A ", "M4B ", "M4P ", "F4A ", "F4B "):
        return _det("audio", "audio/mp4", "m4a", "ftyp-audio", needs_probe=True)
    if brand == "qt  ":
        return _det("video", "video/quicktime", "mov", "ftyp-qt", needs_probe=True)
    if brand.startswith("3g"):
        return _det("video", "video/3gpp", "3gp", "ftyp-3gp", needs_probe=True)
    return _det("video", "video/mp4", "mp4", "ftyp-mp4", needs_probe=True)


def _media(head: bytes, filename: str) -> Optional[Detection]:
    found = _ftyp(head, filename)
    if found is not None:
        return found
    if head[:4] == b"\x1a\x45\xdf\xa3":
        webm = b"webm" in head[:64]
        return _det("video", "video/webm" if webm else "video/x-matroska", "webm" if webm else "mkv", "ebml", needs_probe=True)
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return _det("video", "video/x-msvideo", "avi", "riff-avi", needs_probe=True)
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return _det("audio", "audio/wav", "wav", "riff-wave", needs_probe=True)
    if head[:4] == b"OggS":
        video = b"\x80theora" in head[:512]
        return _det("video" if video else "audio", "video/ogg" if video else "audio/ogg", "ogv" if video else "ogg", "ogg", needs_probe=True)
    if head[:4] == b"fLaC":
        return _det("audio", "audio/flac", "flac", "flac", needs_probe=True)
    if len(head) >= 377 and head[0] == 0x47 and head[188] == 0x47 and head[376] == 0x47:
        return _det("video", "video/mp2t", "ts", "mpeg-ts", needs_probe=True)
    if head[:3] == b"ID3":
        return _det("audio", "audio/mpeg", "mp3", "id3", needs_probe=True)
    if len(head) >= 2 and head[0] == 0xFF:
        second = head[1]
        if second in (0xF1, 0xF9):
            return _det("audio", "audio/aac", "aac", "adts", needs_probe=True)
        # An MPEG audio frame header: sync, a non-reserved layer, and a third
        # byte whose bitrate index is neither free (0) nor bad (15) and whose
        # sample-rate index is not reserved (3). Without the third byte
        # `\xff\xfe` — the UTF-16 byte-order mark — reads as MP3.
        if (second & 0xE0) == 0xE0 and (second & 0x06) != 0 and len(head) >= 3:
            third = head[2]
            if (third >> 4) not in (0, 15) and ((third >> 2) & 3) != 3:
                return _det("audio", "audio/mpeg", "mp3", "mpeg-frame", needs_probe=True)
    return None


# ---------------------------------------------------------------- image --


def _image(head: bytes) -> Optional[Detection]:
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return _det("image", "image/png", "png", "png")
    if head[:3] == b"\xff\xd8\xff":
        return _det("image", "image/jpeg", "jpg", "jpeg")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return _det("image", "image/webp", "webp", "webp")
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return _det("image", "image/gif", "gif", "gif")
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return _det("image", "image/tiff", "tiff", "tiff")
    if head[:2] == b"BM" and len(head) >= 26:
        # BMP: a 14-byte file header whose DIB header size is one of the known
        # sizes; "BM" alone starts too much ordinary text.
        (dib,) = struct.unpack("<I", head[14:18])
        if dib in (12, 40, 52, 56, 64, 108, 124):
            return _det("image", "image/bmp", "bmp", "bmp")
    return None


# ----------------------------------------------------------------- text --


def _decode_text(head: bytes, truncated: bool) -> Optional[str]:
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return head.decode("utf-16")
        except UnicodeDecodeError:
            if truncated:
                try:
                    return head[: len(head) - (len(head) % 2)].decode("utf-16", "strict")
                except UnicodeDecodeError:
                    return None
            return None
    data = head[3:] if head.startswith(b"\xef\xbb\xbf") else head
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        # A window cut through a multi-byte character is still UTF-8.
        if truncated and exc.start >= len(data) - 3:
            try:
                return data[: exc.start].decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None


def _suffix(filename: str) -> str:
    return os.path.splitext(filename or "")[1].lower()


def _complete_lines(text: str, truncated: bool) -> List[str]:
    """Non-empty lines, without the last one when the 64 KiB window cut it."""
    raw = text.splitlines()
    if truncated and len(raw) > 1:
        raw = raw[:-1]
    return [line for line in raw if line.strip()]


def _delimited(text: str, truncated: bool) -> Optional[str]:
    """',' / '\\t' / ';' / '|' when ≥ 90% of the first 50 non-empty lines share
    one field count ≥ 2 (design §4.1)."""
    lines = _complete_lines(text, truncated)[:50]
    if len(lines) < 2:
        return None
    for delimiter in (",", "\t", ";", "|"):
        counts = [line.count(delimiter) + 1 for line in lines]
        common = max(set(counts), key=counts.count)
        if common >= 2 and counts.count(common) >= 0.9 * len(counts):
            return delimiter
    return None


def _json_lines(text: str, truncated: bool) -> bool:
    lines = _complete_lines(text, truncated)[:20]
    if not lines:
        return False
    for line in lines:
        try:
            if not isinstance(json.loads(line), dict):
                return False
        except (ValueError, RecursionError):
            # 60,000 `[` on one line raises RecursionError, not ValueError
            # (Python 3.12.3, review 2026-09-13): it is simply not JSON lines.
            return False
    return True


def _text_kind(text: str, filename: str, mime_hint: str, truncated: bool) -> Detection:
    stripped = text.lstrip("﻿ \t\r\n")
    lowered = stripped[:512].lower()
    suffix = _suffix(filename)
    hint = (mime_hint or "").lower().split(";")[0].strip()
    if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
        return _det("html", "text/html", "html", "html")
    if suffix in (".csv", ".tsv") or hint in ("text/csv", "text/tab-separated-values"):
        tsv = suffix == ".tsv" or hint == "text/tab-separated-values"
        return _det("tabular", "text/tab-separated-values" if tsv else "text/csv", "tsv" if tsv else "csv", "csv-hint")
    if suffix in (".jsonl", ".ndjson") or hint in ("application/x-ndjson", "application/jsonl"):
        if _json_lines(stripped, truncated):
            return _det("tabular", "application/x-ndjson", "jsonl", "ndjson")
    if stripped.startswith("[") and stripped[1:].lstrip().startswith("{"):
        # A top-level array of objects: DuckDB profiles it as rows.
        return _det("tabular", "application/json", "json", "json-array")
    if stripped.startswith("{") and (suffix == ".json" or hint == "application/json"):
        # A JSON object (config/spec) is text: a one-struct-row profile is
        # useless (finding #9, measured).
        return _det("text", "application/json", "json", "json-object")
    if suffix not in (".json", ".jsonl", ".ndjson") and _json_lines(stripped, truncated) and len(_complete_lines(stripped, truncated)) >= 2:
        return _det("tabular", "application/x-ndjson", "jsonl", "ndjson-sniffed")
    delimiter = _delimited(stripped, truncated)
    if delimiter is not None and suffix not in (".md", ".txt", ".py", ".js", ".ts", ".html", ".htm"):
        tsv = delimiter == "\t"
        return _det("tabular", "text/tab-separated-values" if tsv else "text/csv", "tsv" if tsv else "csv", f"delimited:{delimiter!r}")
    if suffix in (".md", ".markdown") or hint == "text/markdown":
        return _det("text", "text/markdown", "md", "markdown")
    return _det("text", "text/plain", "txt", "text")


# ----------------------------------------------------------------- entry --


#: ID3v2 tags an input may stack before its first real byte. ffmpeg loops over
#: consecutive tags; a bound keeps a file of nothing but tag headers cheap.
_MAX_ID3_TAGS = 16


def _id3v2_length(header: bytes) -> Optional[int]:
    """The full length of the ID3v2 tag whose 10-byte header is `header`
    (header + 28-bit synchsafe size + 10 more with the footer flag), or None
    when `header` is not one."""
    if len(header) < 10 or header[:3] != b"ID3" or header[3] == 0xFF or header[4] == 0xFF:
        return None
    size_bytes = header[6:10]
    if any(b & 0x80 for b in size_bytes):
        return None
    size = (size_bytes[0] << 21) | (size_bytes[1] << 14) | (size_bytes[2] << 7) | size_bytes[3]
    return 10 + size + (10 if header[5] & 0x10 else 0)


def _id3v2_end(read_at, size: int) -> int:
    """The offset of the first byte after the leading ID3v2 tags of a file,
    the way ffmpeg's probe skips them before choosing a demuxer."""
    offset = 0
    for _ in range(_MAX_ID3_TAGS):
        length = _id3v2_length(read_at(offset, 10) if offset + 10 <= size else b"")
        if length is None:
            return offset
        offset += length
    return offset


def _skip_id3v2(head: bytes) -> bytes:
    """`head` after its leading ID3v2 tags (empty when they run past it)."""
    offset = _id3v2_end(lambda at, n: head[at:at + n], len(head))
    return head[offset:]


def _is_playlist(data: bytes) -> bool:
    probe = data.lstrip(b"\xef\xbb\xbf \t\r\n")
    return probe[:7] == b"#EXTM3U" or probe[:8] == b"ffconcat" or probe[:7] == b"#EXT-X-"


def detect(
    head: bytes,
    *,
    tail: bytes = b"",
    size: Optional[int] = None,
    filename: str = "",
    mime_hint: str = "",
    zip_names: Optional[Sequence[str]] = None,
) -> Detection:
    """Classify from bytes already in hand. `zip_names` is the central
    directory's member list when the caller could read it (see `detect_path`);
    a zip without it is `unsupported` rather than guessed. Never raises."""
    try:
        return _detect(head, tail=tail, size=size, filename=filename, mime_hint=mime_hint, zip_names=zip_names)
    except Exception:  # noqa: BLE001 - total by contract (module docstring)
        return _det("unknown", _OCTET, "", "sniff-error")


def _detect(
    head: bytes,
    *,
    tail: bytes,
    size: Optional[int],
    filename: str,
    mime_hint: str,
    zip_names: Optional[Sequence[str]],
) -> Detection:
    total = len(head) if size is None else int(size)
    if _is_playlist(head) or (head[:3] == b"ID3" and _is_playlist(_skip_id3v2(head))):
        return _det("unsupported", _OCTET, "", "playlist")
    if b"%PDF-" in head[:1024]:
        return _det("pdf", "application/pdf", "pdf", "pdf")
    if head[:4] == b"PK\x03\x04" or head[:4] == b"PK\x05\x06":
        if zip_names is None:
            return _det("unsupported", "application/zip", "", "zip-unreadable")
        return _classify_zip(zip_names, filename)
    if head[:4] == b"PAR1" and (tail[-4:] == b"PAR1" or (total <= len(head) and head[-4:] == b"PAR1")):
        return _det("tabular", "application/vnd.apache.parquet", "parquet", "parquet")
    image = _image(head)
    if image is not None:
        return image
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        bom_text = _decode_text(head, truncated=total > len(head))
        if bom_text is not None:
            return _text_kind(bom_text, filename, mime_hint, total > len(head))
    media = _media(head, filename)
    if media is not None:
        return media
    if b"\x00" in head[:NUL_WINDOW] and not head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return _det("unsupported", _OCTET, "", "binary")
    if total == 0:
        return _det("text", "text/plain", "txt", "empty")
    truncated = total > len(head)
    text = _decode_text(head, truncated=truncated)
    if text is None:
        return _det("unsupported", _OCTET, "", "not-text")
    return _text_kind(text, filename, mime_hint, truncated)


def detect_path(path: str, *, filename: str = "", mime_hint: str = "") -> Detection:
    """Classify a file on disk: 64 KiB head, 64 KiB tail (extended to the zip
    EOCD search window), and a bounded central directory read for zips."""
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        head = fh.read(HEAD_BYTES)
        tail_len = min(size, max(TAIL_BYTES, 65_557))
        fh.seek(size - tail_len)
        tail = fh.read(tail_len)

        def read_at(offset: int, length: int) -> bytes:
            if offset < 0 or length < 0 or offset + length > size:
                return b""
            fh.seek(offset)
            return fh.read(length)

        names = None
        if head[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
            try:
                names = _zip_member_names(read_at, size, tail)
            except (struct.error, ValueError):
                names = None
        if head[:3] == b"ID3":
            # A tag larger than the 64 KiB head (cover art is often bigger)
            # hides what follows it from `detect`; read those bytes directly.
            after = _id3v2_end(read_at, size)
            if after >= len(head) and _is_playlist(read_at(after, min(4096, max(0, size - after)))):
                return _det("unsupported", _OCTET, "", "playlist")
        return detect(head, tail=tail, size=size, filename=filename, mime_hint=mime_hint, zip_names=names)


def link_source(original: str, derived: str, ext: str) -> Optional[str]:
    """`derived/src.<ext>` as a HARD LINK to `original` (finding #2): every
    extractor opens the suffixed name, because openpyxl raises
    `InvalidFileException` on an extensionless path and DuckDB picks its
    reader by suffix. A hard link costs no bytes and no copy time. Idempotent:
    an existing link to the same inode is kept. Returns the path, or None for
    a kind with no extension (unsupported)."""
    if not ext or not ext.isalnum() or len(ext) > 8:
        return None
    os.makedirs(derived, mode=0o750, exist_ok=True)
    target = os.path.join(derived, f"src.{ext}")
    try:
        os.link(original, target)
    except FileExistsError:
        if os.stat(target).st_ino != os.stat(original).st_ino:
            os.unlink(target)
            os.link(original, target)
    return target
