"""Pages into citeable chunks, and the on-disk chunk table (2026-09-13).

WHAT A CHUNK IS. The extraction stage (team PROCESSING) writes
`derived/pages.jsonl`: one line per page, slide, docx section or row block,
`{"page": n, "text": …, "source": "text"|"ocr"}` (spreadsheets may add
`"rows": [a, b]` and `"sheet"`). This module cuts those pages into chunks of
at most PUBLIC_API_FILES_CHUNK_CHARS (1,500) characters with at most
PUBLIC_API_FILES_CHUNK_OVERLAP_CHARS (150) of overlap, and writes them to
`derived/chunks.jsonl` plus a fixed-width offset table `derived/chunks.idx`.

WHY PAGE-BOUNDED. Every chunk carries the page it came from, and a chunk never
spans two pages. A citation `[q3.pdf p.842]` can then be checked against the
exact pages the model was shown (citations.py): a chunk that straddled pages
841-842 would make "p.842" true for text that sat on 841.

WHY 1,500 CHARACTERS. It matches `video/index._SCREEN_CHUNK_CHARS`, and at the
3-characters-per-token estimate (`context.estimate_tokens`) it is ~500 tokens,
far under the embedding engine's 4,096-token input window (the public sidecar
refuses longer inputs rather than clipping them). The measured real report
averaged 1,756 characters per page (design §1.1), so a typical page is one or
two chunks: a 1,000-page PDF is ~1,300-2,000 rows, far under the 50,000-chunk
index ceiling.

WHY AN OFFSET TABLE. Retrieval needs the text of ~64 chunks out of up to
50,000. Parsing a 75 MB JSONL file per request to find them would put ~0.5 s
of CPU on every file question; `chunks.idx` (little-endian uint64 byte
offsets, one per line) makes each lookup a seek.

Everything on disk is written tmp + fsync + rename, so a crash never leaves a
short table that looks complete.
"""
from __future__ import annotations

import array
import json
import os
import secrets
import sys
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Tuple

PAGES_NAME = "pages.jsonl"
CHUNKS_NAME = "chunks.jsonl"
CHUNKS_INDEX_NAME = "chunks.idx"

#: Row block size the spreadsheet extractor uses when a page line carries no
#: explicit `rows` (design §4.4: "page = row block of 200 rows").
ROWS_PER_BLOCK = 200

MODALITY_TEXT = "text"
MODALITY_SPEECH = "speech"
MODALITY_SCREEN = "screen"
MODALITY_VISUAL = "visual"


def chunk_chars() -> int:
    """PUBLIC_API_FILES_CHUNK_CHARS (1,500): module docstring, WHY 1,500."""
    from ..publicapi.registry import setting_int

    return max(200, setting_int("PUBLIC_API_FILES_CHUNK_CHARS", 1500))


def chunk_overlap_chars() -> int:
    """PUBLIC_API_FILES_CHUNK_OVERLAP_CHARS (150): enough to keep a sentence
    that straddles a cut readable in both chunks, a tenth of the chunk."""
    from ..publicapi.registry import setting_int

    return max(0, setting_int("PUBLIC_API_FILES_CHUNK_OVERLAP_CHARS", 150))


@dataclass(frozen=True)
class Page:
    """One line of pages.jsonl."""

    page: int
    text: str
    source: str = "text"
    rows: Optional[Tuple[int, int]] = None
    sheet: Optional[str] = None

    def row_range(self) -> Tuple[int, int]:
        """Rows a block covers: the explicit `rows`, else the fixed block size."""
        if self.rows is not None:
            return self.rows
        first = (max(1, self.page) - 1) * ROWS_PER_BLOCK + 1
        return first, first + ROWS_PER_BLOCK - 1


@dataclass(frozen=True)
class Chunk:
    """One row of the chunk table. `page_start == page_end` for every text
    kind; media chunks carry `start_s`/`end_s` and page 0."""

    chunk_no: int
    page_start: int
    page_end: int
    char_start: int
    char_end: int
    text: str
    modality: str = MODALITY_TEXT
    start_s: Optional[float] = None
    end_s: Optional[float] = None
    rows: Optional[Tuple[int, int]] = None

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "chunk_no": self.chunk_no,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "text": self.text,
        }
        if self.modality != MODALITY_TEXT:
            out["modality"] = self.modality
        if self.start_s is not None:
            out["start_s"] = round(float(self.start_s), 3)
        if self.end_s is not None:
            out["end_s"] = round(float(self.end_s), 3)
        if self.rows is not None:
            out["rows"] = [int(self.rows[0]), int(self.rows[1])]
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Chunk":
        rows = data.get("rows")
        return cls(
            chunk_no=int(data["chunk_no"]),
            page_start=int(data.get("page_start", 0)),
            page_end=int(data.get("page_end", data.get("page_start", 0))),
            char_start=int(data.get("char_start", 0)),
            char_end=int(data.get("char_end", 0)),
            text=str(data.get("text") or ""),
            modality=str(data.get("modality") or MODALITY_TEXT),
            start_s=(float(data["start_s"]) if data.get("start_s") is not None else None),
            end_s=(float(data["end_s"]) if data.get("end_s") is not None else None),
            rows=(int(rows[0]), int(rows[1])) if isinstance(rows, (list, tuple)) and len(rows) == 2 else None,
        )


# ------------------------------------------------------------------ pages --


def page_from_json(data: Mapping[str, Any]) -> Page:
    rows = data.get("rows")
    return Page(
        page=int(data["page"]),
        text=str(data.get("text") or ""),
        source=str(data.get("source") or "text"),
        rows=(int(rows[0]), int(rows[1])) if isinstance(rows, (list, tuple)) and len(rows) == 2 else None,
        sheet=(str(data["sheet"]) if data.get("sheet") is not None else None),
    )


def read_pages(path: str) -> Iterator[Page]:
    """Stream pages.jsonl. A malformed line raises ValueError naming the line
    NUMBER only (never the path or the text): this message is internal, but
    the rule of never carrying a path is cheaper to keep everywhere."""
    with open(path, "r", encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                yield page_from_json(json.loads(line))
            except (ValueError, KeyError, TypeError):
                raise ValueError(f"pages table line {number} is malformed") from None


# --------------------------------------------------------------- chunking --


def _break_before(text: str, start: int, end: int, floor: int) -> int:
    """The best cut in text[floor:end]: a paragraph, then a line, then a
    sentence, then any space. `end` itself when there is none (a 1,500-char
    token with no whitespace is cut where it is)."""
    for needle, width in (("\n\n", 2), ("\n", 1), (". ", 2), (" ", 1)):
        found = text.rfind(needle, floor, end)
        if found != -1 and found + width > start:
            return found + width
    return end


def split_page(text: str, *, max_chars: int, overlap: int) -> List[Tuple[int, int]]:
    """Character spans [start, end) covering `text`, each ≤ max_chars, with
    ≤ overlap characters shared by neighbours, cut at whitespace when there
    is any. A whitespace-only page has no spans."""
    length = len(text)
    if not text.strip():
        return []
    if length <= max_chars:
        return [(0, length)]
    overlap = max(0, min(overlap, max_chars // 3))
    spans: List[Tuple[int, int]] = []
    start = 0
    while start < length:
        hard_end = min(length, start + max_chars)
        if hard_end >= length:
            end = length
        else:
            end = _break_before(text, start, hard_end, start + max_chars // 2)
        spans.append((start, end))
        if end >= length:
            break
        next_start = max(start + 1, end - overlap)
        if overlap:
            # Begin the next chunk at a word, not in the middle of one.
            space = text.find(" ", next_start, end)
            if space != -1 and space + 1 < end:
                next_start = space + 1
        start = next_start
    return spans


def chunk_pages(
    pages: Iterable[Page], *, max_chars: Optional[int] = None, overlap: Optional[int] = None
) -> Iterator[Chunk]:
    """Pages → page-bounded chunks, numbered from 0 in document order."""
    size = chunk_chars() if max_chars is None else int(max_chars)
    shared = chunk_overlap_chars() if overlap is None else int(overlap)
    number = 0
    for page in pages:
        for start, end in split_page(page.text, max_chars=size, overlap=shared):
            yield Chunk(
                chunk_no=number,
                page_start=page.page,
                page_end=page.page,
                char_start=start,
                char_end=end,
                text=page.text[start:end],
                rows=page.rows if page.rows is not None else None,
            )
            number += 1


def chunk_media(transcript: Optional[Mapping[str, Any]], screen: Optional[Mapping[str, Any]]) -> List[Chunk]:
    """An analysis's transcript.json + screen.json → chunks, through the chat
    pipeline's own `video.index.build_chunks` (speech ≤ 45 s / 700 chars,
    screen ≤ 1,500 chars), so a video question on /v1 retrieves over exactly
    the units the chat app retrieves over."""
    from ..video.index import build_chunks
    from ..video.types import OcrSpan, Segment

    segments = [Segment.from_json(s) for s in ((transcript or {}).get("segments") or []) if isinstance(s, Mapping)]
    spans = [OcrSpan.from_json(s) for s in ((screen or {}).get("spans") or []) if isinstance(s, Mapping)]
    out: List[Chunk] = []
    for row in build_chunks(segments, spans):
        text = str(row.get("text") or "")
        out.append(
            Chunk(
                chunk_no=len(out),
                page_start=0,
                page_end=0,
                char_start=0,
                char_end=len(text),
                text=text,
                modality=str(row.get("modality") or MODALITY_SPEECH),
                start_s=float(row.get("start_s") or 0.0),
                end_s=float(row.get("end_s") or 0.0),
            )
        )
    return out


# ----------------------------------------------------------------- on disk --


def fsync_dir(directory: str) -> None:
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


def atomic_write_bytes(path: str, payload: bytes) -> None:
    """tmp + fsync + rename (+ directory fsync): a reader sees the old file
    or the whole new one, never a prefix."""
    directory = os.path.dirname(path) or "."
    tmp = f"{path}.{secrets.token_hex(6)}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(directory)


def write_chunks(chunks: Iterable[Chunk], derived_dir: str) -> int:
    """Write chunks.jsonl and chunks.idx atomically; returns the row count.

    The offset table is renamed into place AFTER the table it indexes, and a
    reader only trusts an index whose entry count matches the table (it
    rebuilds offsets otherwise), so a crash between the two renames costs a
    scan, never a wrong row."""
    os.makedirs(derived_dir, exist_ok=True)
    table_path = os.path.join(derived_dir, CHUNKS_NAME)
    index_path = os.path.join(derived_dir, CHUNKS_INDEX_NAME)
    offsets = array.array("Q")
    tmp = f"{table_path}.{secrets.token_hex(6)}.tmp"
    count = 0
    try:
        with open(tmp, "wb") as fh:
            position = 0
            for expected, chunk in enumerate(chunks):
                if chunk.chunk_no != expected:
                    raise ValueError("chunks must be numbered 0..n-1 in order")
                line = (json.dumps(chunk.to_json(), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                offsets.append(position)
                fh.write(line)
                position += len(line)
                count += 1
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, table_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if sys.byteorder != "little":  # pragma: no cover - every host we run on is little-endian
        offsets.byteswap()
    atomic_write_bytes(index_path, offsets.tobytes())
    fsync_dir(derived_dir)
    return count


class ChunkTable:
    """Random access to chunks.jsonl by chunk number. Not thread-safe; open
    one per request inside `asyncio.to_thread`."""

    def __init__(self, derived_dir: str) -> None:
        self.path = os.path.join(derived_dir, CHUNKS_NAME)
        self._fh = open(self.path, "rb")
        self._offsets = self._load_offsets(os.path.join(derived_dir, CHUNKS_INDEX_NAME))

    def _load_offsets(self, index_path: str) -> "array.array[int]":
        offsets = array.array("Q")
        try:
            with open(index_path, "rb") as fh:
                raw = fh.read()
            if len(raw) % 8 == 0:
                offsets.frombytes(raw)
                if sys.byteorder != "little":  # pragma: no cover
                    offsets.byteswap()
        except OSError:
            offsets = array.array("Q")
        if offsets and self._index_agrees(offsets):
            return offsets
        return self._scan()

    def _index_agrees(self, offsets: "array.array[int]") -> bool:
        size = os.fstat(self._fh.fileno()).st_size
        last = offsets[-1]
        if last >= size:
            return False
        self._fh.seek(last)
        tail = self._fh.readline()
        return bool(tail) and self._fh.tell() == size

    def _scan(self) -> "array.array[int]":
        offsets = array.array("Q")
        self._fh.seek(0)
        position = 0
        for line in self._fh:
            if line.strip():
                offsets.append(position)
            position += len(line)
        return offsets

    def __len__(self) -> int:
        return len(self._offsets)

    def get(self, chunk_no: int) -> Optional[Chunk]:
        if chunk_no < 0 or chunk_no >= len(self._offsets):
            return None
        self._fh.seek(self._offsets[chunk_no])
        line = self._fh.readline()
        try:
            chunk = Chunk.from_json(json.loads(line))
        except (ValueError, KeyError, TypeError):
            return None
        # A row whose number is not the one asked for would put another
        # chunk's text (and page) behind a vector hit: nothing, rather than
        # the wrong excerpt and a citation to the wrong page.
        return chunk if chunk.chunk_no == int(chunk_no) else None

    def __iter__(self) -> Iterator[Chunk]:
        self._fh.seek(0)
        for line in self._fh:
            if line.strip():
                yield Chunk.from_json(json.loads(line))

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "ChunkTable":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def read_chunks(derived_dir: str) -> Iterator[Chunk]:
    with ChunkTable(derived_dir) as table:
        yield from table


def build_text_chunks(derived_dir: str) -> int:
    """The processing stage's one call for a text kind: pages.jsonl →
    chunks.jsonl + chunks.idx. Blocking; run it in a thread."""
    return write_chunks(chunk_pages(read_pages(os.path.join(derived_dir, PAGES_NAME))), derived_dir)
