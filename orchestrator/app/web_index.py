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

The index is DERIVED state. ``web_pages.indexed_at`` is the watermark:
NULL means "the index has not seen this text" (new page, or the content hash
changed on refetch), and indexing marks it. Deleting the whole directory is
always safe — the next pass rebuilds from PostgreSQL.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import List, Optional, Sequence

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
#: A single page contributes at most this many chunks. 24 measurably broke
#: the "complete local copy" promise — it kept 88% of a key 76K-char doc page
#: and 54% of the largest stored page. 64 covers ~180K chars; the embed cost
#: of the difference is ~200 ms per such page (measured 5 ms/chunk batched).
_MAX_CHUNKS_PER_PAGE = 64

_index_lock = asyncio.Lock()


def lancedb_web_dir() -> str:
    return settings.lancedb_web_dir


def chunk_page(text: str) -> List[str]:
    clean = (text or "").strip()
    if len(clean) < _MIN_CHUNK_CHARS:
        return []
    chunks: List[str] = []
    start = 0
    while start < len(clean) and len(chunks) < _MAX_CHUNKS_PER_PAGE:
        piece = clean[start : start + _CHUNK_CHARS]
        if len(piece) >= _MIN_CHUNK_CHARS:
            chunks.append(piece)
        if start + _CHUNK_CHARS >= len(clean):
            break
        start += _CHUNK_CHARS - _OVERLAP_CHARS
    return chunks


#: Recorded in the sidecar so a chunker change is visible (and a rebuild can
#: be forced) instead of silently mixing chunk shapes in one table.
CHUNKER_VERSION = 1


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
    os.makedirs(directory, exist_ok=True)
    import json
    import tempfile

    payload = {
        "table": TABLE,
        "model_id": settings.embed_model,
        "dimension": int(dimension),
        "schema_version": 1,
        # Additive keys; both sidecar readers ignore what they do not know.
        "chunker_version": CHUNKER_VERSION,
        "query_instruction": "qwen3-web-v1",
    }
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


def _open(create_dim: Optional[int] = None):
    """Connect + open (or create) the web_chunks table, sidecar-validated."""
    import lancedb  # lazy

    directory = lancedb_web_dir()
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


async def index_pending(limit: int = 20, page_ids: Optional[Sequence[int]] = None) -> int:
    """Embed and index pages the watermark says are new or changed.

    Delete-then-insert keyed by page_id, so a refreshed page replaces its old
    chunks instead of accumulating duplicates. Returns chunks written. Never
    raises — indexing is an enhancement running behind answered requests.
    `page_ids` limits the pass to those pages (the Fast lookup's two).
    """
    if not settings.web_memory_enabled:
        return 0
    async with _index_lock:  # one indexer at a time; the queue drains anyway
        try:
            pages = await db.run_in_thread(
                db.get_unindexed_web_pages, limit, list(page_ids) if page_ids else None
            )
            if not pages:
                return 0
            texts: List[str] = []
            rows_meta: List[dict] = []
            for page in pages:
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
                # Nothing chunkable (thin pages) — mark them so the queue drains.
                await db.run_in_thread(db.mark_web_pages_indexed, page_ids)
                return 0
            vectors = await _embed_batched(texts)
            rows = [
                {**meta, "vector": list(vec)}
                for meta, vec in zip(rows_meta, vectors)
            ]

            def _write() -> None:
                conn, table, _meta = _open(create_dim=len(rows[0]["vector"]))
                assert table is not None
                table.delete(
                    "page_id IN (" + ", ".join(str(i) for i in page_ids) + ")"
                )
                table.add(rows)

            await asyncio.to_thread(_write)
            await db.run_in_thread(db.mark_web_pages_indexed, page_ids)
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


async def retrieve(
    question: str,
    top_k: int = 6,
    site_prefix: str = "",
) -> List[dict]:
    """Chunks most relevant to `question` from previously read pages.

    Returns [{url, title, text, fetched_at, score}], best first — at most ONE
    chunk per URL. A raw top-k measurably collapsed to 6 chunks of the same
    page, which the per-URL dedupe downstream turned into a single source; the
    index over-fetches and groups so `top_k` means "distinct pages".

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

        def _search() -> List[dict]:
            conn, table, metadata = _open()
            if table is None:
                return []
            validate_query_dimension(metadata, vector, lancedb_web_dir())
            query = table.search(vector).limit(max(int(top_k) * 6, 24))
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
            return query.to_list()

        started = _time.perf_counter()
        hits = await asyncio.to_thread(_search)
        metrics.observe("knowledge_stage_seconds", _time.perf_counter() - started, stage="dense_scan")
        best_per_url: dict = {}
        for h in hits:
            distance = float(h.get("_distance", 0.0))
            if distance > MAX_DISTANCE:
                continue
            url = h.get("url", "")
            if url in best_per_url and best_per_url[url]["score"] <= distance:
                continue
            best_per_url[url] = {
                "url": url,
                "page_id": h.get("page_id"),
                "title": h.get("title", ""),
                "text": h.get("text", ""),
                "fetched_at": h.get("fetched_at", ""),
                "score": distance,
            }
        out = sorted(best_per_url.values(), key=lambda r: r["score"])
        return out[: int(top_k)]
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
    """
    global _cycles, _has_index
    out = {"healed": 0, "optimized": False, "indexed": False, "rows": 0}
    if not settings.web_memory_enabled:
        return out
    _cycles += 1

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

    every = max(1, int(settings.web_index_optimize_every))
    if force or _cycles % every == 0:
        try:
            await asyncio.to_thread(_optimize_table, table)
            out["optimized"] = True
        except Exception:  # noqa: BLE001
            log.debug("web index optimize failed", exc_info=True)

    if rows >= int(settings.web_index_ann_min_rows):
        _has_index = None
        if force or not _index_present(table):
            import math

            partitions = max(32, min(1024, int(math.sqrt(rows))))

            def _build():
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
