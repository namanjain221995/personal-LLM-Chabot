"""A processed blob's derived data: the download allowlist, readers for the
other teams, and the artifacts built from stage files (design §2.9, §13.2).

THE ALLOWLIST IS CLOSED (design §2.9). `GET /v1/files/{id}/derived/{name}`
serves only these names per kind, and resolves them to a path HERE, from ids
and digests that already matched their regexes (`storage.derived_dir`) plus a
name from the table below — never from the request's text:

    pdf, document, presentation, text, html   text.txt, pages.json
    spreadsheet, tabular                       profile.json, text.txt, sheet-<n>.csv (xlsx)
    image                                      image.png
    audio                                      transcript.txt/.srt/.vtt/.json, summary.md
    video                                      the audio names + screen_text.txt, screen_text.json

Audio and video names live in the video pipeline's analysis directory
(`video/store.artifacts_dir`), reached through the blob's `video_analysis_id`
row — the content hash is read from that row, not recomputed, so a change to
how API analyses are keyed cannot make this module serve another analysis.

`text.txt` IS PAGE-MARKED. Each page, section, slide or row block is preceded
by one marker line — `--- Page 12 ---`, `--- Section 3 ---`, `--- Slide 7 ---`,
`--- Rows 201-400 (Sales) ---` — the same `--- Page n ---` shape the chat app's
`core/pdf.render_pdf` gives its model, so a reader and a model see one format.
`pages.json` is `[{page, text, source}]` for the document kinds (§2.9).

BUILT AT `finalize`, FROM `pages.jsonl`, streamed: a 10,000-page table is read
line by line, so the parent never holds a whole document in memory.
"""
from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from . import chunks, storage
from .extractors import PAGES_NAME, PROFILE_NAME, atomic_write_bytes

TEXT_NAME = "text.txt"
PAGES_JSON_NAME = "pages.json"
IMAGE_NAME = "image.png"

TEXT_KINDS = ("pdf", "document", "presentation", "text", "html")
TABLE_KINDS = ("spreadsheet", "tabular")
MEDIA_KINDS = ("audio", "video")

_AUDIO_NAMES = ("transcript.txt", "transcript.srt", "transcript.vtt", "transcript.json", "summary.md")
_VIDEO_EXTRA = ("screen_text.txt", "screen_text.json")
_SHEET_RE = re.compile(r"^sheet-([1-9][0-9]{0,3})\.csv$")

CONTENT_TYPES = {
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".csv": "text/csv; charset=utf-8",
    ".vtt": "text/vtt",
    ".srt": "application/x-subrip",
    ".png": "image/png",
    ".jpg": "image/jpeg",
}

#: The marker word per extraction unit (the child's `facts.unit`).
_UNIT_WORDS = {"page": "Page", "section": "Section", "slide": "Slide", "rows": "Rows"}

#: detail → the image variant long edge (design §5.5); `original` → largest.
_DETAIL_EDGES = {"low": 896, "auto": 1600, "high": 2560, "original": 2560}


class NameNotFound(LookupError):
    """Not a derived name of this blob (unknown, wrong kind, or not built)."""


@dataclass(frozen=True)
class DerivedName:
    name: str
    bytes: int
    content_type: str

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "bytes": self.bytes, "content_type": self.content_type}


# ------------------------------------------------------------------ paths --


def derived_dir_for(blob_row: Mapping[str, Any]) -> str:
    """`<root>/<project_id>/<sha256>/derived` for a blob row (regex-checked)."""
    return storage.derived_dir(str(blob_row["project_id"]), str(blob_row["sha256"]))


def _kind(blob_row: Mapping[str, Any]) -> str:
    return str(blob_row.get("kind") or "unknown")


def _content_type(name: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def video_row(blob_row: Mapping[str, Any]) -> Optional[dict]:
    """The `video_analyses` row an audio/video blob points at, or None."""
    analysis_id = blob_row.get("video_analysis_id")
    if not analysis_id:
        return None
    from .. import db

    return db.get_video_analysis(int(analysis_id))


def _media_dir(blob_row: Mapping[str, Any]) -> Optional[str]:
    row = video_row(blob_row)
    if not row or not row.get("content_hash"):
        return None
    from ..video import store

    return store.artifacts_dir(str(row["content_hash"]))


def allowed_names(blob_row: Mapping[str, Any]) -> Tuple[str, ...]:
    """The fixed names for this blob's kind (sheet CSVs are matched by regex)."""
    kind = _kind(blob_row)
    if kind in TEXT_KINDS:
        return (TEXT_NAME, PAGES_JSON_NAME)
    if kind in TABLE_KINDS:
        return (PROFILE_NAME, TEXT_NAME)
    if kind == "image":
        return (IMAGE_NAME,)
    if kind == "audio":
        return _AUDIO_NAMES
    if kind == "video":
        return _AUDIO_NAMES + _VIDEO_EXTRA
    return ()


def _resolve(blob_row: Mapping[str, Any], name: str) -> Optional[str]:
    """The on-disk path for an allowed name, or None. Never joins a name that
    is not in the allowlist (or matching the sheet regex for xlsx)."""
    kind = _kind(blob_row)
    if not isinstance(name, str):
        return None
    if name in allowed_names(blob_row):
        if kind in MEDIA_KINDS:
            base = _media_dir(blob_row)
            return os.path.join(base, name) if base else None
        return os.path.join(derived_dir_for(blob_row), name)
    if kind == "spreadsheet" and _SHEET_RE.fullmatch(name):
        return os.path.join(derived_dir_for(blob_row), name)
    return None


def list_names(blob_row: Mapping[str, Any]) -> List[dict]:
    """`[{name, bytes, content_type}]` for the names that exist. Empty until
    the blob is processed (a half-built directory is never advertised)."""
    if str(blob_row.get("status") or "") != "processed":
        return []
    out: List[DerivedName] = []
    names = list(allowed_names(blob_row))
    if _kind(blob_row) == "spreadsheet":
        try:
            sheets = sorted(
                (e.name for e in os.scandir(derived_dir_for(blob_row)) if _SHEET_RE.fullmatch(e.name)),
                key=lambda n: int(_SHEET_RE.fullmatch(n).group(1)),  # type: ignore[union-attr]
            )
        except OSError:
            sheets = []
        names.extend(sheets)
    for name in names:
        path = _resolve(blob_row, name)
        if not path:
            continue
        size = _regular_size(path)
        if size is None:
            continue
        out.append(DerivedName(name, int(size), _content_type(name)))
    return [n.to_json() for n in out]


def open_name(blob_row: Mapping[str, Any], name: str) -> Tuple[str, str, int]:
    """(path, content type, bytes) for a derived download; `NameNotFound`
    for anything not on the allowlist, not built, or before processing."""
    if str(blob_row.get("status") or "") != "processed":
        raise NameNotFound(name)
    path = _resolve(blob_row, name)
    if not path:
        raise NameNotFound(name)
    size = _regular_size(path)
    if size is None:
        raise NameNotFound(name)
    return path, _content_type(name), int(size)


def _regular_size(path: str) -> Optional[int]:
    """The size of a REGULAR file at `path` itself, never through a link.

    Derived files are written by the sandboxed extraction child, which cannot
    create symlinks (Landlock, 2026-09-13); a download still refuses one, so a
    link planted by any other means can never serve a file outside the blob."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return int(info.st_size) if stat.S_ISREG(info.st_mode) else None


# ---------------------------------------------------------------- readers --


def read_pages(blob_row: Mapping[str, Any]) -> Iterator[chunks.Page]:
    """The page table (team INDEX's `chunks.Page` records), streamed."""
    return chunks.read_pages(os.path.join(derived_dir_for(blob_row), PAGES_NAME))


def read_chunks(blob_row: Mapping[str, Any]) -> Iterator[chunks.Chunk]:
    return chunks.read_chunks(derived_dir_for(blob_row))


def _facts(blob_row: Mapping[str, Any]) -> Mapping[str, Any]:
    facts = blob_row.get("facts") or {}
    if isinstance(facts, str):
        try:
            facts = json.loads(facts)
        except ValueError:
            facts = {}
    return facts if isinstance(facts, Mapping) else {}


def estimated_text_tokens(blob_row: Mapping[str, Any]) -> int:
    """`facts.estimated_tokens` as measured by the extraction child with the
    chat app's `context.estimate_tokens` (0 when the kind has no text)."""
    try:
        return max(0, int(_facts(blob_row).get("estimated_tokens") or 0))
    except (TypeError, ValueError):
        return 0


def image_variant(blob_row: Mapping[str, Any], detail: str) -> Tuple[str, str]:
    """(path, mime) of the variant for `detail`: the largest stored variant at
    or below the detail's long edge, falling back to the smallest one."""
    derived = derived_dir_for(blob_row)
    wanted = _DETAIL_EDGES.get(str(detail or "auto").lower(), 1600)
    candidates: List[Tuple[int, str]] = []
    try:
        for entry in os.scandir(derived):
            match = re.fullmatch(r"image_(896|1600|2560)\.(png|jpg)", entry.name)
            if match:
                candidates.append((int(match.group(1)), entry.name))
    except OSError:
        pass
    if not candidates:
        raise NameNotFound("image variant")
    candidates.sort()
    fitting = [c for c in candidates if c[0] <= wanted]
    edge, name = (fitting[-1] if fitting else candidates[0])
    return os.path.join(derived, name), _content_type(name)


# --------------------------------------------------------------- building --


def _marker(unit: str, record: Mapping[str, Any]) -> str:
    word = _UNIT_WORDS.get(unit, "Page")
    rows = record.get("rows")
    if unit == "rows" and isinstance(rows, (list, tuple)) and len(rows) == 2:
        sheet = record.get("sheet")
        suffix = f" ({sheet})" if sheet else ""
        return f"--- Rows {int(rows[0])}-{int(rows[1])}{suffix} ---"
    return f"--- {word} {int(record.get('page') or 0)} ---"


def build_text_artifacts(derived_dir: str, *, kind: str, unit: str = "page") -> List[str]:
    """`text.txt` (page-marked) for text and table kinds, plus `pages.json`
    for the document kinds. Streamed from pages.jsonl; written atomically.
    Returns the names written. Blocking: call it in a thread."""
    source = os.path.join(derived_dir, PAGES_NAME)
    if not os.path.exists(source):
        return []
    from .extractors import AtomicTextWriter

    written: List[str] = []
    text_out = AtomicTextWriter(os.path.join(derived_dir, TEXT_NAME))
    pages_out = AtomicTextWriter(os.path.join(derived_dir, PAGES_JSON_NAME)) if kind in TEXT_KINDS else None
    try:
        first = True
        if pages_out is not None:
            pages_out.write("[")
        with open(source, "r", encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                record = json.loads(line)
                text = str(record.get("text") or "")
                if not first:
                    text_out.write("\n\n")
                text_out.write(_marker(unit, record) + "\n" + text)
                if pages_out is not None:
                    item = {"page": int(record.get("page") or 0), "text": text, "source": str(record.get("source") or "text")}
                    pages_out.write(("" if first else ",") + json.dumps(item, ensure_ascii=False))
                first = False
        if not first:
            text_out.write("\n")
        text_out.commit()
        written.append(TEXT_NAME)
        if pages_out is not None:
            pages_out.write("]")
            pages_out.commit()
            written.append(PAGES_JSON_NAME)
    except BaseException:
        text_out.abort()
        if pages_out is not None:
            pages_out.abort()
        raise
    return written


def write_bytes(derived_dir: str, name: str, payload: bytes) -> None:
    """Atomic write of one named artifact (names come from this module)."""
    atomic_write_bytes(os.path.join(derived_dir, name), payload)


def derived_bytes(blob_row: Mapping[str, Any]) -> int:
    """Bytes under the blob's `derived/` (hard link `src.<ext>` excluded —
    it is the original's bytes, already counted in `api_file_blobs.bytes`)
    plus the analysis directory for audio/video, excluding its source link."""
    total = 0
    derived = derived_dir_for(blob_row)
    for base, _dirs, files in os.walk(derived, followlinks=False):
        for name in files:
            if base == derived and name.startswith("src."):
                continue
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                pass
    row = video_row(blob_row) if _kind(blob_row) in MEDIA_KINDS else None
    if row and row.get("content_hash"):
        from ..video import store

        analysis = store.analysis_dir(str(row["content_hash"]))
        for base, _dirs, files in os.walk(analysis, followlinks=False):
            for name in files:
                if base == analysis and name.startswith("source."):
                    continue
                try:
                    total += os.lstat(os.path.join(base, name)).st_size
                except OSError:
                    pass
    return total
