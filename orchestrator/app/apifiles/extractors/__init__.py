"""Per-kind extractors for the public Files API (design §4.4, 2026-09-13).

WHERE THIS CODE RUNS. Only inside `app.apifiles.extract_worker`, a child
process with its own rlimits and no core dumps. Nothing in this package is
imported by the orchestrator's request path: a hostile PDF, zip or image may
crash or balloon a parser, and it must do that to a child under RLIMIT_AS,
not to the process that serves chat on the memory the model engines share.

WHAT EVERY EXTRACTOR PRODUCES. One `derived/pages.jsonl`, one line per page,
docx section, slide or spreadsheet row block:

    {"page": n, "text": "...", "source": "text"}          pdf / docx / pptx / text / html
    {"page": n, "text": "...", "source": "text",
     "sheet": "Sales", "rows": [201, 400]}                 xlsx / csv / tsv / ndjson row blocks

— the exact shape `apifiles/chunks.py` (team INDEX) reads, so chunking needs
no adapter. Plus a facts dict (the child prints it as its one JSON line) and,
per kind, the extra artifacts named in the design's derived allowlist
(`profile.json`, `sheet-<n>.csv`, image variants, `thin_pages.json`).

WHY THE CHAT APP'S CODE IS IMPORTED, NOT COPIED. `core/docx.extract_docx_text`,
`core/archive.check_zip_container`, `core/profile.profile_excel` /
`profile_tabular`, `core/extract.extract_readable` and `context.estimate_tokens`
are the same functions the chat surface runs; a fix to one reaches both. Only
the PDF text layer is new code, because `core/pdf.extract_pdf_pages` takes the
whole file as base64 in memory and holds the process-wide PDFIUM_LOCK — the
design's reason for a path-based reader in a child (§1.2, §4.4).

ERRORS ARE A CLOSED VOCABULARY (§4.6). An extractor raises `FileCorrupt`,
`FileTooComplex` or `Unsupported`; the ceiling phrase of `FileTooComplex` is
always a string THIS package wrote ("more than 10,000 pages"), never a
parser's exception text, because `status_details` reaches the caller.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

PAGES_NAME = "pages.jsonl"
THIN_PAGES_NAME = "thin_pages.json"
PROFILE_NAME = "profile.json"
SOURCE_PREFIX = "src."

#: Docx and text "pages" are sections of at most this many characters, cut at
#: a paragraph (docx) or line (text) boundary. 3,000 is the design's number
#: (§4.4): two 1,500-char chunks, so a `§n` citation names a span a reader can
#: find in one screen.
SECTION_CHARS = 3000

#: Spreadsheet and CSV text blocks: 200 rows each, header repeated (§4.4).
ROWS_PER_BLOCK = 200

#: The fixed sentences of `processing.error` (§4.6). `{ceiling}` is filled
#: only from `FileTooComplex.ceiling`, which this package writes.
ERROR_SENTENCES: Dict[str, str] = {
    "unsupported_file": "This file type cannot be used as model input.",
    "file_corrupt": "The file could not be read; it may be damaged or encrypted.",
    "file_too_complex": "The file exceeds a processing ceiling: {ceiling}.",
    "processing_unavailable": (
        "Processing could not reach a required service after several attempts. "
        "Upload the file again later."
    ),
    "internal_error": "Something went wrong while processing this file.",
}
ERROR_CODES = tuple(ERROR_SENTENCES)


class ExtractError(Exception):
    """A terminal verdict about the FILE (not about an engine)."""

    code = "internal_error"

    def __init__(self, ceiling: str = "") -> None:
        super().__init__(self.code)
        self.ceiling = str(ceiling or "")

    @property
    def sentence(self) -> str:
        return sentence_for(self.code, self.ceiling)

    def to_json(self) -> Dict[str, str]:
        return {"code": self.code, "ceiling": self.ceiling}


class FileCorrupt(ExtractError):
    code = "file_corrupt"


class FileTooComplex(ExtractError):
    code = "file_too_complex"


class Unsupported(ExtractError):
    code = "unsupported_file"


def sentence_for(code: str, ceiling: str = "") -> str:
    """The caller-facing sentence for an error code; unknown codes read as
    `internal_error` rather than leaking the code string."""
    template = ERROR_SENTENCES.get(code) or ERROR_SENTENCES["internal_error"]
    if "{ceiling}" in template:
        return template.format(ceiling=ceiling or "a processing limit")
    return template


def error_from_json(data: Mapping[str, Any]) -> ExtractError:
    code = str(data.get("code") or "internal_error")
    cls = {c.code: c for c in (FileCorrupt, FileTooComplex, Unsupported)}.get(code, ExtractError)
    return cls(str(data.get("ceiling") or ""))


@dataclass(frozen=True)
class Spec:
    """What the parent tells the child: which op, on which directory, under
    which ceilings. JSON on argv — paths and numbers only, never content."""

    op: str
    kind: str
    derived_dir: str
    source: str = ""
    caps: Dict[str, Any] = field(default_factory=dict)
    args: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "op": self.op,
                "kind": self.kind,
                "derived_dir": self.derived_dir,
                "source": self.source,
                "caps": self.caps,
                "args": self.args,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> "Spec":
        data = json.loads(raw)
        return cls(
            op=str(data["op"]),
            kind=str(data.get("kind") or ""),
            derived_dir=str(data["derived_dir"]),
            source=str(data.get("source") or ""),
            caps=dict(data.get("caps") or {}),
            args=dict(data.get("args") or {}),
        )

    def cap(self, name: str, default: Any = None) -> Any:
        value = self.caps.get(name)
        return default if value is None else value


# ------------------------------------------------------------- the source --


def find_source(derived_dir: str) -> Optional[str]:
    """`derived/src.<ext>`, the suffixed hard link the sniff stage creates.

    Every extractor opens THIS, never the extensionless `original` (design
    finding #2, measured 2026-09-13): openpyxl raises InvalidFileException on
    a suffixless path, and DuckDB chooses read_parquet / read_json_auto /
    read_csv_auto by suffix."""
    try:
        for entry in sorted(os.scandir(derived_dir), key=lambda e: e.name):
            if entry.name.startswith(SOURCE_PREFIX) and entry.is_file(follow_symlinks=False):
                return entry.path
    except OSError:
        return None
    return None


def source_suffix(path: str) -> str:
    return os.path.splitext(path or "")[1].lower()


# ------------------------------------------------------------ byte caps --


def size_phrase(limit_bytes: int) -> str:
    """"larger than 1,024 MiB" — or KiB/bytes below 1 MiB, where integer MiB
    read "larger than 0 MiB" (review finding, 2026-09-13)."""
    limit = max(0, int(limit_bytes))
    if limit >= 1024 * 1024:
        return f"larger than {limit // (1024 * 1024):,} MiB"
    if limit >= 1024:
        return f"larger than {limit // 1024:,} KiB"
    return f"larger than {limit:,} bytes"


def check_source_size(spec: "Spec", default: int, *, what: str = "") -> int:
    """The source's size, or `FileTooComplex` naming the byte ceiling. Every
    extractor calls this BEFORE opening the file, so an oversized input is a
    verdict in milliseconds instead of a parser run until a ceiling kills it."""
    size = os.path.getsize(spec.source)
    limit = int(spec.cap("bytes", default))
    if size > limit:
        raise FileTooComplex(size_phrase(limit) + (f" {what}" if what else ""))
    return size


# ------------------------------------------------------------ atomic I/O --


def _fsync_dir(directory: str) -> None:
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


def _tmp_for(path: str) -> str:
    return f"{path}.{secrets.token_hex(6)}.tmp"


def atomic_write_bytes(path: str, payload: bytes) -> None:
    """tmp + fsync + rename: a reader sees the old file or the whole new one.

    The directory is NOT created: `derived/` exists before any stage runs, and
    a writer that recreated it after a DELETE's rmtree would resurrect a
    purged blob's directory (the retention sweep would then have to find it)."""
    tmp = _tmp_for(path)
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        _unlink_quiet(tmp)
        raise
    _fsync_dir(os.path.dirname(path) or ".")


def atomic_write_json(path: str, payload: Any) -> None:
    atomic_write_bytes(path, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


class AtomicTextWriter:
    """A streamed text file that appears under its name only when complete."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._tmp = _tmp_for(path)
        self._fh = open(self._tmp, "w", encoding="utf-8", newline="")
        self.bytes_hint = 0

    def write(self, text: str) -> None:
        self._fh.write(text)

    def commit(self) -> None:
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        os.replace(self._tmp, self.path)
        _fsync_dir(os.path.dirname(self.path) or ".")

    def abort(self) -> None:
        try:
            self._fh.close()
        finally:
            _unlink_quiet(self._tmp)

    def __enter__(self) -> "AtomicTextWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.abort()


class PagesWriter:
    """`derived/pages.jsonl`, one JSON line per page, written atomically.

    Counts what the facts need as it goes (pages, characters, estimated
    tokens, pages with a usable text layer), so no stage re-reads the table."""

    def __init__(self, derived_dir: str, *, thin_below: int = 0) -> None:
        self._out = AtomicTextWriter(os.path.join(derived_dir, PAGES_NAME))
        self.pages = 0
        self.chars = 0
        self.estimated_tokens = 0
        self.text_pages = 0
        self.thin: List[int] = []
        self._thin_below = int(thin_below)
        from ...context import estimate_tokens  # the chat app's estimate, one formula for both

        self._estimate = estimate_tokens

    def add(
        self,
        page: int,
        text: str,
        *,
        source: str = "text",
        sheet: Optional[str] = None,
        rows: Optional[Sequence[int]] = None,
    ) -> None:
        record: Dict[str, Any] = {"page": int(page), "text": text, "source": source}
        if sheet is not None:
            record["sheet"] = sheet
        if rows is not None:
            record["rows"] = [int(rows[0]), int(rows[1])]
        self._out.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.pages += 1
        self.chars += len(text)
        if text:
            self.estimated_tokens += self._estimate(text)
        if self._thin_below and len(text) < self._thin_below:
            self.thin.append(int(page))
        elif text:
            self.text_pages += 1

    def commit(self) -> None:
        self._out.commit()

    def abort(self) -> None:
        self._out.abort()

    def __enter__(self) -> "PagesWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.abort()


def iter_page_records(derived_dir: str) -> Iterator[Dict[str, Any]]:
    """The raw JSON records of pages.jsonl (for stages that rewrite it)."""
    with open(os.path.join(derived_dir, PAGES_NAME), "r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


# ------------------------------------------------------------ splitting --


_LEADING_SPACE = re.compile(r"\s*")


def split_sections(blocks: Iterator[str], *, max_chars: int = SECTION_CHARS, joiner: str = "\n") -> Iterator[str]:
    """Group text blocks (paragraphs or lines) into sections ≤ `max_chars`.

    A block is never split unless it alone is longer than a section; then it
    is cut at the last space inside the window, or hard at the window when a
    run of 3,000 characters has no space (a base64 blob pasted into a doc).

    LINEAR IN THE BLOCK'S LENGTH (review finding, 2026-09-13). The cut used
    to be `block = block[cut:].lstrip()`, a copy of the whole remainder per
    3,000-character section: one-line files measured 0.17 s at 4 MiB, 3.26 s
    at 16 MiB and 15.02 s at 32 MiB (≈4.7× per doubling, hours at 1 GiB —
    past the text kind's CPU ceiling, holding a CPU-lane slot all the while).
    Now a position walks the block and only each section is copied."""
    current: List[str] = []
    size = 0
    for block in blocks:
        if not block:
            continue
        if len(block) > max_chars:
            if current:
                yield joiner.join(current)
                current, size = [], 0
            pos, end = 0, len(block)
            while end - pos > max_chars:
                cut = block.rfind(" ", pos + max_chars // 2, pos + max_chars)
                cut = cut if cut > pos else pos + max_chars
                piece = block[pos:cut].rstrip()
                if piece:
                    yield piece
                pos = _LEADING_SPACE.match(block, cut).end()
            block = block[pos:]
            if not block:
                continue
        extra = len(block) + (len(joiner) if current else 0)
        if current and size + extra > max_chars:
            yield joiner.join(current)
            current, size = [], 0
            extra = len(block)
        current.append(block)
        size += extra
    if current:
        yield joiner.join(current)


__all__ = [
    "AtomicTextWriter",
    "ERROR_CODES",
    "ERROR_SENTENCES",
    "ExtractError",
    "FileCorrupt",
    "FileTooComplex",
    "PAGES_NAME",
    "PROFILE_NAME",
    "PagesWriter",
    "ROWS_PER_BLOCK",
    "SECTION_CHARS",
    "Spec",
    "THIN_PAGES_NAME",
    "Unsupported",
    "atomic_write_bytes",
    "atomic_write_json",
    "check_source_size",
    "error_from_json",
    "find_source",
    "iter_page_records",
    "sentence_for",
    "size_phrase",
    "source_suffix",
    "split_sections",
]
