"""Vector index over stored web pages (V8 web-search memory, 2026-08-30).

Every page the search engine fetches is persisted in PostgreSQL
(``web_pages`` — the durable, rebuildable source of truth). This module keeps
a LanceDB table of embedded chunks over that store so a later question can
pull relevant paragraphs from pages read days ago, in any conversation,
without a network fetch.

WHY A SEPARATE LANCEDB DIRECTORY. The Salesforce corpus lives in
``settings.lancedb_dir`` and its sidecar pins exactly one table
(embedding_index.py refuses a second); more importantly, the RAG engine
renders every hit from that table as a Salesforce record citation, so web
rows mixed in would surface as if they came from the CRM. Web chunks get
their own directory with their own sidecar — same embedding service, same
1024-dim vectors, zero blast radius. Measured on this deployment: an
855-token chunk embeds in ~15 ms warm (~5 ms/chunk batched), and a flat scan
runs at ~1.9 µs/row, so a young table answers in tens of milliseconds.

The index is DERIVED state, tracked by TWO watermarks that answer different
questions (V24, 2026-09-06):

* ``web_pages.indexed_at`` — has the index seen this TEXT? NULL means no (a
  new page, or the content hash changed on a refetch).
* ``web_pages.chunk_version`` — were those vectors built by the CURRENT
  chunker? A page whose text never changes still needs new vectors when
  ``chunk_page`` changes shape, and ``indexed_at`` can never say so.

The second is a purely LOCAL repair. ``web_pages.text`` is retained, so a
re-chunk needs no fetch, no ETag and no remote freshness: a page can be
confirmed unchanged upstream by a 304 and still be overdue for reprocessing
here. Keeping the two apart is why a 304 costs nothing yet never hides a page
from ``index_pending``.

Deleting the whole directory is always safe — the next pass rebuilds from
PostgreSQL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from contextlib import contextmanager
from typing import Dict, List, Optional, Sequence, Set

from . import db, llm, metrics
from .config import settings
from .embedding_index import (
    EmbeddingIndexMetadata,
    metadata_path,
    open_compatible_table,
    validate_query_dimension,
)

log = logging.getLogger(__name__)

TABLE = "web_chunks"

#: ~800 tokens at the 4-chars/token heuristic the sync-worker chunker uses,
#: with a 100-token overlap so a fact split across a boundary survives on one
#: side. Character-based on purpose: no tokenizer dependency in the request
#: path.
_CHUNK_CHARS = 3200
_OVERLAP_CHARS = 400
_MIN_CHUNK_CHARS = 200
#: A single page contributes at most this many chunks. 24 measurably broke the
#: "complete local copy" promise — it kept 88% of a key 76K-char doc page and
#: 54% of the largest stored page. 64 covered ~180K chars and still cut 59 live
#: pages, so V27 (2026-09-07) raised it to 256; the one-off drain cost 5,523
#: chunks in 232.5 s and 15.9 MB of vectors, and `chunk_page` stops on text
#: length rather than on the cap, so no page that already fitted was re-chunked.
_MAX_CHUNKS_PER_PAGE = 256

#: K10: how much of ONE page the chunk cap can actually reach.
#:
#: The window advances by (chunk - overlap), so the ceiling is (cap - 1)
#: STRIDES plus one full chunk — at the cap of 256 that is 255 x 2,800 + 3,200
#: = 717,200 chars, NOT 256 x 3,200 = 819,200, which is what the cap looks
#: like until the overlap is accounted for. Keep this DERIVED: every attempt
#: to quote the number instead of computing it has gone stale on the next cap
#: change, in this comment and in the tests that pinned it.
#:
#: Everything past the ceiling used to be dropped with no log line and no
#: counter, so a half-indexed page was indistinguishable from a fully indexed
#: one — in `web_pages`, in the logs and on /metrics alike. `index_pending` now
#: counts `web_index_page_truncated_total` and names the URL and the shortfall
#: (covered by tests/test_reprocessing.py). The cap is deliberately left where
#: it is rather than raised to swallow the remaining tail: measured on the live
#: corpus 2026-09-07, 7 of 2,209 pages are still over the ceiling and they lose
#: 4,563,338 chars between them, but the largest is a 2.37M-char SEC archive —
#: no cap short of "no cap" reaches that, and the COVERAGE-DECISION measurement
#: found the newly covered band indexed but only weakly RETRIEVABLE (~1 in 5
#: for prose, ~0 for big tables), so more coverage is not more answers. What
#: matters is that the loss is VISIBLE.
INDEXED_CHARS_PER_PAGE = (_MAX_CHUNKS_PER_PAGE - 1) * (
    _CHUNK_CHARS - _OVERLAP_CHARS
) + _CHUNK_CHARS

_index_lock = asyncio.Lock()

#: A pipe row as trafilatura writes it: `| a | b |`, and the `|---|---|` rule
#: under the header.
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")
#: A carried header is a repair, not a payload: anything longer than this is
#: not a header row.
_MAX_CARRIED_HEADER_CHARS = 400


def lancedb_web_dir() -> str:
    return settings.lancedb_web_dir


def unindexed_chars(text: str) -> int:
    """Characters of `text` the chunk cap will drop (0 when the page fits).

    K10's observability hook. The chunker stays a pure function; the caller
    knows which URL it is holding, so it is the caller that logs and counts.
    """
    clean = (text or "").strip()
    return max(0, len(clean) - INDEXED_CHARS_PER_PAGE)


def _pipe_table_runs(text: str) -> List[dict]:
    """Header blocks of every pipe table in `text`, with its character span.

    → [{"start", "end", "block"}], one per run of pipe rows that looks like a
    table (a `|---|` rule under the header, or a consistent column count).
    """
    runs: List[List[tuple]] = []
    current: List[tuple] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if _TABLE_ROW_RE.match(line) and stripped.count("|") >= 2:
            current.append((offset, line))
        elif current:
            runs.append(current)
            current = []
        offset += len(line)
    if current:
        runs.append(current)

    out: List[dict] = []
    for rows in runs:
        if len(rows) < 2:
            continue
        has_rule = bool(_TABLE_RULE_RE.match(rows[1][1]))
        even_columns = len({r[1].count("|") for r in rows}) == 1
        if not has_rule and not (len(rows) >= 3 and even_columns):
            continue  # pipes in prose or in code, not a table
        header = [rows[0][1].strip()]
        if has_rule:
            header.append(rows[1][1].strip())
        block = "\n".join(header)
        if len(block) > _MAX_CARRIED_HEADER_CHARS:
            continue
        out.append(
            {
                "start": rows[0][0],
                "end": rows[-1][0] + len(rows[-1][1]),
                "block": block,
            }
        )
    return out


def _carry_table_header(piece: str, start: int, runs: List[dict]) -> str:
    """Repeat the table header when a chunk boundary lands inside the table.

    K3/C1: the header goes in chunk 0 and the rows in chunks 1..n, so every
    later chunk retrieves as bare numbers with no column meaning — the
    retrieved evidence for "what is X's score" is a row of digits whose
    columns the model has to guess.
    """
    for run in runs:
        if run["start"] <= start < run["end"]:
            block = run["block"]
            if block and block not in piece:
                return block + "\n" + piece
            break
    return piece


def chunk_page(text: str) -> List[str]:
    clean = (text or "").strip()
    if len(clean) < _MIN_CHUNK_CHARS:
        return []
    runs = _pipe_table_runs(clean)
    chunks: List[str] = []
    start = 0
    while start < len(clean) and len(chunks) < _MAX_CHUNKS_PER_PAGE:
        piece = clean[start : start + _CHUNK_CHARS]
        if len(piece) >= _MIN_CHUNK_CHARS:
            chunks.append(_carry_table_header(piece, start, runs) if runs else piece)
        if start + _CHUNK_CHARS >= len(clean):
            break
        start += _CHUNK_CHARS - _OVERLAP_CHARS
    return chunks


#: WHICH CHUNKER produced a page's vectors. Recorded per page in
#: ``web_pages.chunk_version`` (migration V24) and in the sidecar, so a chunker
#: change is visible and repairable instead of silently mixing chunk shapes in
#: one table.
#:
#:   1  the original fixed 3,200/400 window.
#:   2  (2026-09-06) the same window, plus ``_carry_table_header``: a chunk that
#:      starts inside a pipe table now repeats that table's header row, because
#:      the header landed in chunk 0 and the answer row in chunk 2, so the
#:      retrieved evidence for "what is X's score" was a line of bare digits
#:      with no column meaning (findings C1/K3).
#:
#: The shape changed on 2026-09-06 while this constant stayed at 1, which made
#: it a lie: every page indexed before that day holds header-less chunks and
#: nothing could tell them apart from the repaired ones. Bumping it is what
#: turns ``get_unindexed_web_pages``/``mark_web_pages_indexed`` into an
#: incremental repair — no re-fetch, no operator, the stored text is enough.
#:
#: BUMP THIS whenever `chunk_page` can produce different text for the same
#: input. Do NOT bump it for a change that only affects which pages are
#: selected or how they are embedded; those are not chunk shape.
#:   3  (2026-09-07) the same window and header carry, but the per-page cap
#:      rose 64 -> 256 chunks (179,600 -> 717,200 characters). Measured: only
#:      59 of 2,063 indexable pages exceeded the old ceiling, but 86.4% of them
#:      held facts in the dropped tail that are absent from the indexed region
#:      (60/60 sampled probes verified). Chosen over 512/1024 because the
#:      marginal chunks above 256 come from the deepest tails of the very
#:      largest pages -- highest retrieval-skew risk, lowest measured value --
#:      and 256 had the lowest measured ms/chunk (21.35) and 507 MB peak RSS.
#:      A page that fits under the OLD ceiling chunks byte-identically at
#:      either cap, so V27 stamps those as version 3 without re-embedding.
CHUNKER_VERSION = 3


def _write_sidecar(directory: str, dimension: int) -> None:
    """The one-table-per-directory contract, same shape as the main corpus.

    Written ONCE, atomically, with the dimension known. The previous
    two-step write (dimension 0, then rewrite) left a sidecar every reader
    rejects if the process died between the steps — and recall then
    returned nothing at DEBUG level while the indexer retried forever.
    """
    path = metadata_path(directory)
    if os.path.exists(path):
        return
    payload = {
        "table": TABLE,
        "model_id": settings.embed_model,
        "dimension": int(dimension),
        "schema_version": 1,
        # Additive keys; both sidecar readers ignore what they do not know.
        "chunker_version": CHUNKER_VERSION,
        "query_instruction": "qwen3-web-v1",
    }
    _atomic_write_json(path, payload)


def _atomic_write_json(path: str, payload: dict) -> None:
    """Replace `path` with `payload`, or leave the old file completely intact."""
    import json
    import tempfile

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".sidecar-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def sidecar_chunker_version(directory: str) -> Optional[int]:
    """What the sidecar in `directory` says produced its chunks.

    None means there is no readable sidecar at all (no index yet). **0 means
    the sidecar exists and does not say** — which is the LIVE production case,
    not a hypothetical: `/data/lancedb-web/_techsara_embedding_index.json` was
    written on 2026-08-30, before `chunker_version` was a key, and reads
    exactly `{"table", "model_id", "dimension", "schema_version"}`. Returning
    None for that would have made the sidecar unfixable on the one deployment
    that matters. 0 is the same "unknown/legacy" value `web_pages.chunk_version`
    uses, and it is below every real version, so it can be advanced.
    """
    import json

    try:
        with open(metadata_path(directory), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return int(payload.get("chunker_version", 0))
    except (TypeError, ValueError):
        return 0


def _advance_sidecar_chunker_version(directory: str) -> bool:
    """Move the sidecar's `chunker_version` up once the repair is complete.

    THE BUG THIS FIXES. `_write_sidecar` returns early when the file already
    exists — correct, because model_id and dimension must never move under a
    live table — and `chunker_version` was written by the same one-shot. So on
    every deployment that already has an index (i.e. production), bumping
    CHUNKER_VERSION left the sidecar frozen at the old number for ever, while
    the table filled up with new-shaped chunks. `/health` reads that key
    verbatim, and health.py's own comment promises "a chunker bump is visible
    here first" — after a bump it was visible NOWHERE. Measured on a scratch
    directory before this existed: code at 2, table holding chunker-2 rows,
    sidecar and /health still reporting 1.

    WHY IT WAITS FOR THE BACKLOG TO DRAIN. Half-way through an incremental
    repair the table genuinely holds both shapes, and the sidecar is per-INDEX.
    Writing 2 while chunker-1 rows are still in there would trade a stale
    number for a false one. `get_unindexed_web_pages(limit=1,
    chunk_version=CHUNKER_VERSION)` is the exact predicate `index_pending`
    drains, so an empty result means every stored page's vectors were built by
    this chunker — and only then does the sidecar say so.

    Never on a request path: `maintain()` is the worker's own cycle.
    """
    current = sidecar_chunker_version(directory)
    if current is None or current >= CHUNKER_VERSION:
        return False
    import json

    try:  # re-read rather than reuse: model_id/dimension are not ours to move
        with open(metadata_path(directory), encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return False
    payload["chunker_version"] = CHUNKER_VERSION
    _atomic_write_json(metadata_path(directory), payload)
    log.info(
        "web index: sidecar chunker_version %s -> %s (every stored page "
        "re-chunked)", current, CHUNKER_VERSION,
    )
    return True


#: Name of the cross-PROCESS lock guarding one LanceDB directory. It sits
#: BESIDE the directory, not inside it, so it can be taken before the directory
#: exists and so it never makes an "empty target" look non-empty to
#: `tools.reindex_web`, which refuses to build into a directory with content.
def _lock_path(directory: str) -> str:
    absolute = os.path.abspath(directory).rstrip("/")
    return os.path.join(
        os.path.dirname(absolute), "." + os.path.basename(absolute) + ".write.lock"
    )


class IndexBusy(RuntimeError):
    """Another PROCESS is writing this index directory."""


@contextmanager
def write_lock(directory: str, *, wait_s: float = 0.0):
    """Exclusive, cross-process write access to one LanceDB directory.

    `_index_lock` is an `asyncio.Lock`: it serialises the indexer against the
    search path's write-behind INSIDE this process and knows nothing about a
    second one. The second one is real — `tools/reindex_web.py` is documented
    to run as `docker exec … python -m tools.reindex_web`, which is a separate
    interpreter against the same `/data` mount, and a clustered deployment can
    have two orchestrators on one volume. Lance commits a manifest optimistically
    and has no commit lock on a plain filesystem, so two writers are a corrupt
    table, not a slow one.

    `flock` is the right primitive here rather than a lock row or a PID file:
    the kernel releases it when the holder dies, so a killed rebuild cannot
    strand the worker. `wait_s = 0` means "fail immediately" (an operator gets
    told, rather than hanging); the indexer passes a small budget because its
    work is deferrable.
    """
    import fcntl

    path = _lock_path(directory)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        handle = open(path, "a+")
    except OSError as exc:
        # Loud, not silent, and NOT "carry on unlocked". The lock file sits in
        # the index directory's parent (`/data` in this deployment — verified
        # writable by the orchestrator, which runs as root). If a future
        # deployment cannot create it, refusing to write is the safe failure:
        # writing anyway is how two processes corrupt one Lance manifest.
        raise IndexBusy(
            f"cannot take the web index write lock at {path}: {exc}. "
            "The index directory's parent must be writable."
        ) from exc
    deadline = time.monotonic() + max(0.0, wait_s)
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise IndexBusy(
                        f"another process is writing the web index at {directory!r} "
                        f"(lock {path}); refusing to write concurrently"
                    ) from None
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _open(create_dim: Optional[int] = None):
    """Connect + open (or create) the web_chunks table, sidecar-validated."""
    import lancedb  # lazy

    directory = lancedb_web_dir()
    assert_not_salesforce(directory)
    os.makedirs(directory, exist_ok=True)
    conn = lancedb.connect(directory)
    if TABLE not in conn.table_names():
        if create_dim is None:
            return conn, None, None
        _write_sidecar(directory, int(create_dim))
        table = conn.create_table(
            TABLE,
            data=[
                {
                    "page_id": 0,
                    "url": "",
                    "title": "",
                    "text": "",
                    "fetched_at": "",
                    "vector": [0.0] * int(create_dim),
                }
            ],
        )
        table.delete("page_id = 0")
        metadata = EmbeddingIndexMetadata(
            table=TABLE,
            model_id=settings.embed_model,
            dimension=int(create_dim),
            schema_version=1,
        )
        return conn, table, metadata
    table, metadata = open_compatible_table(
        conn, directory, TABLE, settings.embed_model
    )
    return conn, table, metadata


def _within(child: str, parent: str) -> bool:
    """True when `child` IS `parent` or lives underneath it."""
    child_abs = os.path.abspath(child).rstrip("/")
    parent_abs = os.path.abspath(parent).rstrip("/")
    return child_abs == parent_abs or child_abs.startswith(parent_abs + os.sep)


def assert_not_salesforce(directory: str) -> None:
    """Refuse a web-index path that is, contains, or sits inside the CRM corpus.

    ``settings.lancedb_dir`` holds the SALESFORCE vectors in a different table.
    ``embedding_index`` pins exactly one table per directory in its sidecar, so
    a web operation pointed at that directory does not merely add rows — it
    makes the sidecar and the table set disagree and the RAG engine's index
    stops opening. ``tools.reindex_web`` has carried this check for its ``--out``
    since it was written; it belongs on the LIVE directory too, because
    ``LANCEDB_WEB_DIR`` is an environment variable and a typo in it points every
    write in this module, DELETES INCLUDED, at the CRM corpus.

    Both directions are tested so neither a subdirectory of the CRM corpus nor
    a parent that swallows it gets through.
    """
    salesforce = settings.lancedb_dir
    if _within(directory, salesforce) or _within(salesforce, directory):
        raise RuntimeError(
            f"refusing to use {directory!r} as the web index: it overlaps the "
            f"SALESFORCE index directory {salesforce!r} (LANCEDB_DIR, table "
            f"{settings.lancedb_table!r}). The web index is a separate "
            f"directory with its own sidecar and table {TABLE!r}."
        )


#: Page ids per DELETE predicate. The predicate is a literal IN list, so this
#: bounds the SQL string a very large purge builds.
_DELETE_BATCH = 500


def delete_pages(page_ids: Sequence[int]) -> dict:
    """Remove every chunk of these pages from the live web index.

    The other half of a purge (K7). ``web_pages`` is the source of truth and a
    delete there is a transaction; the vectors are derived state in a separate
    store, and a purge that skipped this step used to leave chunks that
    ``crawl.site_hits_for`` rendered straight into an answer — a citation to a
    page that had been deleted, with no row left to date it or name it.

    ``_servable_page_ids`` is now the second line of defence and refuses to
    serve such a chunk, but it is a filter, not a cleanup: only this call (or a
    ``tools.reindex_web`` rebuild) takes the rows out of the table.

    Delete-by-predicate is what the indexer itself does before re-adding a
    page, so this is a write the table already sees routinely. Returns
    ``{"status", "removed", "rows"}``; the caller decides what a failure means.
    """
    ids = [int(i) for i in page_ids]
    if not ids:
        return {"status": "skipped", "removed": 0, "rows": 0}
    assert_not_salesforce(lancedb_web_dir())
    _conn, table, _meta = _open()
    if table is None:
        return {"status": "empty", "removed": 0, "rows": 0}
    before = int(table.count_rows())
    for start in range(0, len(ids), _DELETE_BATCH):
        batch = ids[start : start + _DELETE_BATCH]
        table.delete("page_id IN (" + ", ".join(str(i) for i in batch) + ")")
    after = int(table.count_rows())
    return {"status": "ok", "removed": before - after, "rows": after}


#: Texts per embedding request. The whole backlog used to go in ONE request
#: (up to 2,560 chunks), which the sidecar answered in tens of seconds and
#: which a query embedding then queued behind.
_EMBED_BATCH = 64


async def _embed_batched(texts: List[str]) -> List[List[float]]:
    out: List[List[float]] = []
    for start in range(0, len(texts), _EMBED_BATCH):
        out.extend(await llm.embed_texts(texts[start : start + _EMBED_BATCH], kind="index"))
        await asyncio.sleep(0)  # let a query embedding in between batches
    return out


async def index_pending(
    limit: int = 20,
    page_ids: Optional[Sequence[int]] = None,
    *,
    repair_stale_chunks: bool = True,
) -> int:
    """Embed and index pages whose vectors are missing, stale, or mis-shaped.

    Delete-then-insert keyed by page_id, so a refreshed page replaces its old
    chunks instead of accumulating duplicates. Returns chunks written. Never
    raises — indexing is an enhancement running behind answered requests.
    `page_ids` limits the pass to those pages (the Fast lookup's two).

    TWO KINDS OF WORK, and V24 is what makes them distinguishable:

    * `indexed_at IS NULL` — no vectors for this text (new page, or the content
      hash moved on a re-fetch). Somebody may be waiting on it, so it is served
      first.
    * `chunk_version < CHUNKER_VERSION` — vectors exist and were built by an
      older chunker. **This is a purely LOCAL repair**: `web_pages.text` is
      retained, so it needs no fetch, no network and no remote freshness. A
      page whose text never changes used to keep obsolete chunks for ever,
      because `indexed_at` only ever answered "were vectors built", never "were
      they built by the current chunker".

    Passing CHUNKER_VERSION to BOTH db calls is load-bearing in opposite
    directions: omit it from the select and the repair never happens, omit it
    from the stamp and every repaired page is re-selected on the next pass, for
    ever.

    `repair_stale_chunks=False` restricts the pass to genuinely un-vectored
    pages. The Fast lookup indexes synchronously — the caller is about to read
    the corpus back — and must not inherit a whole-corpus repair backlog inside
    a user's deadline. It still STAMPS the current version on what it writes:
    the chunker that ran really was this one.
    """
    if not settings.web_memory_enabled:
        return 0
    select_version = CHUNKER_VERSION if repair_stale_chunks else 0
    async with _index_lock:  # one indexer at a time; the queue drains anyway
        try:
            pages = await db.run_in_thread(
                db.get_unindexed_web_pages,
                limit,
                list(page_ids) if page_ids else None,
                select_version,
            )
            if not pages:
                return 0
            texts: List[str] = []
            rows_meta: List[dict] = []
            for page in pages:
                dropped = unindexed_chars(page["text"] or "")
                if dropped:
                    # K10: a page too big for the chunk cap used to be indexed
                    # to 179,600 chars and stored as if it were complete.
                    metrics.inc(
                        "web_index_page_truncated_total",
                        "pages whose tail the chunk cap left out of the index",
                    )
                    log.warning(
                        "web index: %s exceeds the per-page chunk ceiling; "
                        "%d of %d char(s) not indexed",
                        (page.get("url") or "")[:160],
                        dropped,
                        len((page["text"] or "").strip()),
                    )
                for piece in chunk_page(page["text"]):
                    texts.append(piece)
                    rows_meta.append(
                        {
                            "page_id": int(page["id"]),
                            "url": page["url"],
                            "title": page["title"] or "",
                            "text": piece,
                            "fetched_at": str(page["fetched_at"]),
                        }
                    )
            page_ids = [int(p["id"]) for p in pages]
            if not texts:
                # Nothing chunkable (thin pages) — mark them so the queue
                # drains. Stamped with the current chunker like any other pass:
                # a page with no chunks cannot have stale ones.
                await db.run_in_thread(
                    db.mark_web_pages_indexed, page_ids, CHUNKER_VERSION
                )
                return 0
            vectors = await _embed_batched(texts)
            rows = [
                {**meta, "vector": list(vec)}
                for meta, vec in zip(rows_meta, vectors)
            ]

            def _write() -> None:
                # The delete+add pair is the only place this process replaces
                # rows another writer may be touching, so it is what the
                # cross-process lock has to cover. A short wait, then give up:
                # the queue is durable and the next cycle retries.
                with write_lock(lancedb_web_dir(), wait_s=20.0):
                    conn, table, _meta = _open(create_dim=len(rows[0]["vector"]))
                    assert table is not None
                    table.delete(
                        "page_id IN (" + ", ".join(str(i) for i in page_ids) + ")"
                    )
                    table.add(rows)

            await asyncio.to_thread(_write)
            # Only after the vectors are durable: a crash between the two
            # re-does the page, which is free; the reverse loses it silently.
            await db.run_in_thread(
                db.mark_web_pages_indexed, page_ids, CHUNKER_VERSION
            )
            log.info(
                "web index: %d chunk(s) from %d page(s)", len(rows), len(pages)
            )
            return len(rows)
        except Exception:  # noqa: BLE001 — background enhancement, never fatal
            log.warning("web indexing failed; will retry next pass", exc_info=True)
            return 0


#: L2 distance above which a hit is noise, not memory. Measured on this
#: index: genuinely relevant hits scored 0.36-0.66 and junk started at 1.19 —
#: without a floor, a big crawled site fills every weak-match query's slots.
MAX_DISTANCE = 1.0


def _servable_page_ids(page_ids: Sequence[int]) -> set:
    """Which of these pages PostgreSQL still says may be served.

    A page id survives if its row exists and is not quarantined. Everything
    else is an ORPHAN CHUNK: the vectors of a page that was purged, or one an
    operator pulled out of retrieval.

    WHY THIS IS NOT SOMEBODY ELSE'S JOB. ``web_memory.retrieve`` re-reads every
    hit from ``web_pages`` a moment later and does drop quarantined rows — but
    it is not the only caller. ``crawl.site_hits_for`` calls this module
    directly and renders the chunk text into the answer with no PostgreSQL
    round trip at all, so on the site-Q&A path a quarantined page has always
    still been answered from and a purged one still cited. The check belongs
    where the rows come from.

    Measured 2026-09-06 against a 400-row table: 0.40 ms for 36 ids, 0.46 ms
    for 24 — one indexed primary-key lookup, run in a worker thread like every
    other database call on this path, so it is not on the event loop and not in
    front of the first token.

    (This reaches for ``db.connection()`` rather than a named accessor because
    ``db.py`` is owned elsewhere this cycle; the query wants to move into it as
    ``db.servable_web_page_ids(ids) -> set[int]``.)
    """
    ids = sorted({int(i) for i in page_ids if i})
    if not ids:
        return set()
    with db.connection() as con:
        rows = con.execute(
            "SELECT id FROM web_pages WHERE id = ANY(%s) AND quarantined_at IS NULL",
            (ids,),
        ).fetchall()
    return {int(r["id"]) for r in rows}


#: How many ANN scans one `retrieve` call may run. Round 1 is the scan this
#: function always did; each later round repeats it with the pages already
#: found excluded, and only when the previous rounds came back with fewer
#: distinct pages than the caller asked for. Three is the measured knee: on
#: the live corpus round 1 (limit 36) returned 1 distinct page in 3.6 ms and
#: round 2, excluding it, returned all 5 others in 5.1 ms.
_RETRIEVE_MAX_ROUNDS = 3


async def retrieve(
    question: str,
    top_k: int = 6,
    site_prefix: str = "",
    max_chunks_per_page: int = 1,
) -> List[dict]:
    """Chunks most relevant to `question` from previously read pages.

    Returns [{url, title, text, fetched_at, score}], best first, with at most
    `max_chunks_per_page` chunks per URL (1 — one chunk per source — is what
    every caller wants today). A raw top-k measurably collapsed to 6 chunks of
    the same page, which the per-URL dedupe downstream turned into a single
    source, so this groups by page and `top_k` means "distinct pages".

    WHY GROUPING ALONE WAS NOT ENOUGH — the diversity promise above was a
    BUDGET bug, not a ranking one. The over-fetch was a single
    `limit(max(top_k * 6, 24))`, so the cut happened inside the database: the
    nearest-first scan spent the whole budget with no idea which page a chunk
    came from, and one large page could take all of it. Measured on the live
    corpus 2026-09-07: 2,209 servable pages / 19,895 chunks, mean 9.0 chunks a
    page — but 118 pages (5.3%) hold >= 36, which is the ENTIRE budget at the
    `top_k=6` that `crawl.site_hits_for` passes, and 200 (9.1%) hold >= 24.
    Seven pages sit at the 256-chunk cap. Reproduced: a corpus with one
    oversized page returned 1 distinct source instead of 6, at every top_k.

    Raising the over-fetch is NOT the fix — any fixed global budget is
    swampable by a page that holds more chunks than the budget, and seven live
    pages do. So the budget is made PAGE-AWARE instead: when a round comes
    back with fewer distinct pages than asked for, the scan runs again with
    the pages already found excluded (`page_id NOT IN (…)`, the same `.where()`
    prefilter already used for `site_prefix`), up to `_RETRIEVE_MAX_ROUNDS`.
    No score is adjusted and no source is promoted — every hit is still ranked
    by distance alone; only the set of candidates the scan is allowed to
    consider changes.

    `site_prefix` scopes the search to one crawled site (normalized-URL
    prefix) — the follow-up Q&A path. Empty on any failure or before anything
    is indexed: recall is an enhancement, never a precondition for answering.
    """
    if not settings.web_memory_enabled or not (question or "").strip():
        return []
    try:
        import time as _time

        started = _time.perf_counter()
        # One cached, bounded query embedding per turn (llm.embed_query),
        # with the model's query instruction: Qwen3-Embedding is asymmetric
        # and the documents were indexed plain, so only the query changes.
        vector = await llm.embed_query(question, instruction=llm.QUERY_INSTRUCTION)
        metrics.observe("knowledge_stage_seconds", _time.perf_counter() - started, stage="embed")
        if not vector:
            return []

        #: One round's raw-chunk budget. It is still a global cut inside the
        #: database — what changed is that a round that spends it all on one
        #: page is now followed by another round that cannot see that page.
        candidates = max(int(top_k) * 6, 24)

        def _search(exclude_page_ids: Sequence[int] = ()) -> List[dict]:
            conn, table, metadata = _open()
            if table is None:
                return []
            validate_query_dimension(metadata, vector, lancedb_web_dir())
            query = table.search(vector).limit(candidates)
            if settings.knowledge_ann_bypass:
                # Reader-side rollback for an ANN index: no data change.
                try:
                    query = query.bypass_vector_index()
                except Exception:  # noqa: BLE001
                    pass
            elif _index_present(table):
                # ANN (ADR-0001 D8): 50 probes measured recall@10 = 0.995 at
                # 9 ms on a 90k-row copy, versus 150 ms for the flat scan.
                try:
                    query = query.nprobes(max(1, int(settings.web_index_nprobes))).refine_factor(2)
                except Exception:  # noqa: BLE001 — older client: flat is fine
                    pass
            if site_prefix:
                # scope_prefix is normalized (scheme dropped, "www." stripped)
                # but the url column stores the ORIGINAL URL — a crawl of a
                # www-canonical site stores "https://www.…", which a bare-host
                # LIKE can never match, so site Q&A silently never fired for
                # those sites (review round, 2026-08-30). Match both spellings
                # under both schemes. `%` and `_` in a real path are LIKE
                # wildcards; escape them so a prefix means a prefix.
                safe = _like_literal(site_prefix)
                alts = " OR ".join(
                    f"url LIKE '{scheme}://{host_form}%' ESCAPE '\\'"
                    for scheme in ("https", "http")
                    for host_form in (safe, f"www.{safe}")
                )
                query = query.where(alts)
            if exclude_page_ids:
                # lancedb 0.37 ANDs repeated where() calls and prefilters by
                # default, so this composes with `site_prefix` above and cuts
                # the pages BEFORE the vector scan spends its budget on them.
                query = query.where(
                    "page_id NOT IN ("
                    + ", ".join(str(int(i)) for i in exclude_page_ids)
                    + ")"
                )
            return query.to_list()

        per_page = max(1, int(max_chunks_per_page))
        wanted_pages = max(1, -(-int(top_k) // per_page))  # ceil
        # Every page id this call has already decided about — kept whether the
        # page was usable or not, so a later round neither re-ranks it nor
        # spends its budget on an orphan a second time.
        decided: Set[int] = set()
        chunks_by_url: Dict[str, List[dict]] = {}

        for _round in range(_RETRIEVE_MAX_ROUNDS):
            started = _time.perf_counter()
            hits = await asyncio.to_thread(_search, sorted(decided))
            metrics.observe(
                "knowledge_stage_seconds", _time.perf_counter() - started, stage="dense_scan"
            )
            near = [h for h in hits if float(h.get("_distance", 0.0)) <= MAX_DISTANCE]
            if not near:
                break
            # The index is DERIVED state and can outlive its rows. Ask
            # PostgreSQL which of these pages may still be served before any of
            # them reaches a caller: a purged page has no row, a quarantined
            # one is deliberately out of retrieval, and neither is visible in
            # the LanceDB row itself. Asked AFTER the distance floor, so only
            # the hits that could be returned are looked up.
            started = _time.perf_counter()
            servable = await db.run_in_thread(
                _servable_page_ids, [h.get("page_id") for h in near]
            )
            metrics.observe(
                "knowledge_stage_seconds", _time.perf_counter() - started, stage="servable"
            )
            fresh = False
            for h in near:
                distance = float(h.get("_distance", 0.0))
                page_id = h.get("page_id")
                if page_id:
                    if int(page_id) not in decided:
                        fresh = True
                    decided.add(int(page_id))
                    if int(page_id) not in servable:
                        metrics.inc("web_index_orphan_chunks_total")
                        continue
                url = h.get("url", "")
                kept = chunks_by_url.setdefault(url, [])
                if len(kept) >= per_page and kept[-1]["score"] <= distance:
                    continue
                kept.append(
                    {
                        "url": url,
                        "page_id": page_id,
                        "title": h.get("title", ""),
                        "text": h.get("text", ""),
                        "fetched_at": h.get("fetched_at", ""),
                        "score": distance,
                    }
                )
                kept.sort(key=lambda r: r["score"])
                del kept[per_page:]
            if len(chunks_by_url) >= wanted_pages:
                break
            # A round that turned up no page this call had not already seen
            # cannot be improved on by repeating it (a chunk with no page_id
            # cannot be excluded, so it would come back for ever).
            if not fresh:
                break

        out = sorted(
            (row for rows in chunks_by_url.values() for row in rows),
            key=lambda r: r["score"],
        )[: int(top_k)]
        if len({r["url"] for r in out}) < wanted_pages:
            # The caller gets a well-formed list that is merely SHORT on
            # sources, which is exactly why the budget bug stayed invisible.
            metrics.inc(
                "web_index_distinct_pages_short_total",
                "retrievals that returned fewer distinct pages than asked for",
            )
        return out
    except llm.EmbedUnavailable as exc:
        metrics.inc("knowledge_degraded_total", reason="embed_" + ("busy" if "busy" in str(exc) else "error"))
        log.debug("web recall: embedding unavailable (%s)", exc)
        return []
    except Exception:  # noqa: BLE001
        log.debug("web recall unavailable", exc_info=True)
        return []


def _like_literal(value: str) -> str:
    """A string safe inside a single-quoted LIKE pattern with ESCAPE '\\'."""
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace("'", "''")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def optimize() -> None:
    """Compact the table after a big write burst (a crawl's delete+add
    batches measurably cost +27% scan time and 2x disk until compacted)."""
    try:
        conn, table, _meta = _open()
        if table is not None:
            _optimize_table(table)
    except Exception:  # noqa: BLE001
        log.debug("web index optimize skipped", exc_info=True)


# ---------------------------------------------------------------------------
# Maintenance (ADR-0001 D8): compaction, the ANN index, self-heal
# ---------------------------------------------------------------------------

_has_index: Optional[bool] = None
_cycles = 0


def _index_names(table) -> List[str]:
    try:
        return [str(getattr(i, "name", i)) for i in table.list_indices()]
    except Exception:  # noqa: BLE001
        return []


def _index_present(table) -> bool:
    """Cached per process; refreshed by maintain()."""
    global _has_index
    if _has_index is None:
        _has_index = any("vector" in n.lower() or "idx" in n.lower() for n in _index_names(table))
    return bool(_has_index)


def _optimize_table(table) -> None:
    """Compact fragments and prune old versions.

    Every delete+add batch leaves a fragment and a version behind (measured
    on this deployment: 419 versions / 209 fragments for 9k rows, and a
    Salesforce table with 137k versions holding 3.1 GB for 444 MB of live
    data). An hour's grace keeps any in-flight reader on the version it
    opened.
    """
    from datetime import timedelta

    try:
        table.optimize(cleanup_older_than=timedelta(hours=1))
    except TypeError:  # older client without the keyword
        table.optimize()


async def maintain(*, force: bool = False) -> dict:
    """Never on a request path. Called by the worker once per cycle:

    - SELF-HEAL: pages marked indexed while the table is missing or empty
      (a deleted LanceDB directory) get their watermark reset so the next
      pass rebuilds them — the docstring's "deleting the directory is safe"
      promise, now actually kept.
    - COMPACTION every `web_index_optimize_every` cycles.
    - ANN INDEX (IVF_FLAT, sqrt(rows) partitions) once the table crosses
      `web_index_ann_min_rows`; rebuilt when forced (a chunker change).
    - CHUNKER VERSION: reports how many stored pages still hold chunks from an
      older chunker, and advances the sidecar's `chunker_version` once that
      count reaches zero. Both are observability, not work — the repair itself
      is `index_pending`, which needs no fetch.
    """
    global _cycles, _has_index
    out = {
        "healed": 0, "optimized": False, "indexed": False, "rows": 0,
        "stale_chunk_pages": 0, "sidecar_advanced": False,
    }
    if not settings.web_memory_enabled:
        return out
    _cycles += 1
    try:
        out["stale_chunk_pages"] = int(
            await db.run_in_thread(db.count_stale_chunk_pages, CHUNKER_VERSION)
        )
    except Exception:  # noqa: BLE001 — a count is never worth a cycle
        log.debug("could not count stale-chunker pages", exc_info=True)

    def _state():
        conn, table, _meta = _open()
        return table, (table.count_rows() if table is not None else 0)

    try:
        table, rows = await asyncio.to_thread(_state)
    except Exception:  # noqa: BLE001
        log.debug("web index maintenance: cannot open table", exc_info=True)
        return out
    out["rows"] = rows
    if table is None or rows == 0:
        try:
            healed = await db.run_in_thread(db.reset_web_index_watermark)
        except Exception:  # noqa: BLE001
            healed = 0
        if healed:
            log.warning("web index empty; %d page(s) queued for re-indexing", healed)
        out["healed"] = int(healed or 0)
        _has_index = None
        return out

    # The sidecar is per-INDEX, so it may only claim the new chunker once NO
    # stored page still needs re-chunking — `get_unindexed_web_pages` with the
    # current version is exactly the queue `index_pending` drains, so an empty
    # answer is the proof. Mid-repair the table really does hold both shapes
    # and the old number is the honest one.
    try:
        outstanding = await db.run_in_thread(
            db.get_unindexed_web_pages, 1, None, CHUNKER_VERSION
        )
        if not outstanding:
            out["sidecar_advanced"] = await asyncio.to_thread(
                _advance_sidecar_chunker_version, lancedb_web_dir()
            )
    except Exception:  # noqa: BLE001
        log.debug("could not advance the sidecar chunker version", exc_info=True)

    every = max(1, int(settings.web_index_optimize_every))
    if force or _cycles % every == 0:
        # Compaction rewrites fragments and prunes versions, so it is a writer
        # like any other and takes the cross-process lock.
        def _compact() -> None:
            with write_lock(lancedb_web_dir(), wait_s=10.0):
                _optimize_table(table)

        try:
            await asyncio.to_thread(_compact)
            out["optimized"] = True
        except IndexBusy:
            log.debug("web index optimize skipped: another process is writing")
        except Exception:  # noqa: BLE001
            log.debug("web index optimize failed", exc_info=True)

    if rows >= int(settings.web_index_ann_min_rows):
        _has_index = None
        if force or not _index_present(table):
            import math

            partitions = max(32, min(1024, int(math.sqrt(rows))))

            def _build():
                with write_lock(lancedb_web_dir(), wait_s=10.0):
                    table.create_index(
                        metric="l2",
                        vector_column_name="vector",
                        index_type="IVF_FLAT",
                        num_partitions=partitions,
                        replace=True,
                    )

            try:
                started = asyncio.get_running_loop().time()
                await asyncio.to_thread(_build)
                _has_index = True
                out["indexed"] = True
                log.info(
                    "web index: IVF_FLAT index built over %d rows (%d partitions) in %.1fs",
                    rows, partitions, asyncio.get_running_loop().time() - started,
                )
            except Exception:  # noqa: BLE001
                log.warning("web index: ANN index build failed; flat scan continues", exc_info=True)
    return out
