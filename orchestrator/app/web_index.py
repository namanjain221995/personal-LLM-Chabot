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

from . import db, llm
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


def _write_sidecar_once(directory: str) -> None:
    """The one-table-per-directory contract, same shape as the main corpus."""
    path = metadata_path(directory)
    if os.path.exists(path):
        return
    os.makedirs(directory, exist_ok=True)
    import json

    payload = {
        "table": TABLE,
        "model_id": settings.embed_model,
        "dimension": 0,  # filled on first write below
        "schema_version": 1,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


def _open(create_dim: Optional[int] = None):
    """Connect + open (or create) the web_chunks table, sidecar-validated."""
    import lancedb  # lazy

    directory = lancedb_web_dir()
    os.makedirs(directory, exist_ok=True)
    conn = lancedb.connect(directory)
    if TABLE not in conn.table_names():
        if create_dim is None:
            return conn, None, None
        _write_sidecar_once(directory)
        import json

        path = metadata_path(directory)
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not payload.get("dimension"):
            payload["dimension"] = int(create_dim)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
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


async def index_pending(limit: int = 20) -> int:
    """Embed and index pages the watermark says are new or changed.

    Delete-then-insert keyed by page_id, so a refreshed page replaces its old
    chunks instead of accumulating duplicates. Returns chunks written. Never
    raises — indexing is an enhancement running behind answered requests.
    """
    if not settings.web_memory_enabled:
        return 0
    async with _index_lock:  # one indexer at a time; the queue drains anyway
        try:
            pages = await db.run_in_thread(db.get_unindexed_web_pages, limit)
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
            vectors = await llm.embed_texts(texts)
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
        vectors = await llm.embed_texts([question])
        if not vectors:
            return []

        def _search() -> List[dict]:
            conn, table, metadata = _open()
            if table is None:
                return []
            validate_query_dimension(metadata, vectors[0], lancedb_web_dir())
            query = table.search(vectors[0]).limit(max(int(top_k) * 6, 24))
            if site_prefix:
                # scope_prefix is normalized (scheme dropped, "www." stripped)
                # but the url column stores the ORIGINAL URL — a crawl of a
                # www-canonical site stores "https://www.…", which a bare-host
                # LIKE can never match, so site Q&A silently never fired for
                # those sites (review round, 2026-08-30). Match both spellings
                # under both schemes.
                safe = site_prefix.replace("'", "''")
                alts = " OR ".join(
                    f"url LIKE '{scheme}://{host_form}%'"
                    for scheme in ("https", "http")
                    for host_form in (safe, f"www.{safe}")
                )
                query = query.where(alts)
            return query.to_list()

        hits = await asyncio.to_thread(_search)
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
                "title": h.get("title", ""),
                "text": h.get("text", ""),
                "fetched_at": h.get("fetched_at", ""),
                "score": distance,
            }
        out = sorted(best_per_url.values(), key=lambda r: r["score"])
        return out[: int(top_k)]
    except Exception:  # noqa: BLE001
        log.debug("web recall unavailable", exc_info=True)
        return []


def optimize() -> None:
    """Compact the table after a big write burst (a crawl's delete+add
    batches measurably cost +27% scan time and 2x disk until compacted)."""
    try:
        conn, table, _meta = _open()
        if table is not None:
            table.optimize()
    except Exception:  # noqa: BLE001
        log.debug("web index optimize skipped", exc_info=True)
