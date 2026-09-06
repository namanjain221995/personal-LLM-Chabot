"""Rebuild the web vector index from PostgreSQL — build alongside, then swap.

Runs inside the orchestrator container (``/app``), where the data volume,
the embedding service and the database are reachable:

    docker exec -it sf-local-ai-orchestrator-1 python -m tools.reindex_web build
    docker exec -it sf-local-ai-orchestrator-1 python -m tools.reindex_web build \\
        --out /data/lancedb-web.20260903T1200Z
    docker exec -it sf-local-ai-orchestrator-1 python -m tools.reindex_web \\
        reset-watermark [--since 2026-09-03T12:00:00+00:00] --yes

WHY A NEW DIRECTORY (ADR-0001 D8, §6). The index is DERIVED state: every
row is a chunk of ``web_pages.text`` plus its vector, so PostgreSQL can
always regenerate it. But "delete the directory and let the worker refill
it" is not free: the worker embeds 40 pages per cycle, so a 1,300-page store
spends ~30 cycles with a partial index, and every Fast answer in that window
sees an incomplete dense half. Building into a NEW directory keeps the old
one serving until the new one is complete and validated; the swap is one
env change (``LANCEDB_WEB_DIR``) and the rollback is the same change in
reverse. The old directory is never touched by this tool.

WHAT "VALIDATED" MEANS. Before the tool prints swap instructions it re-opens
the new directory the way the app does (sidecar checked against the
configured model and the table's vector width) and asserts that the number
of distinct ``page_id`` values equals the number of indexable pages it read,
and that the row count equals the chunks it embedded. A page that lost its
chunks to a failed batch is therefore a hard failure, not a quiet gap.

The chunker (``web_index.chunk_page``), the embedding call
(``llm.embed_texts``, batches of 64 — the live indexer's batch size) and the
table schema (``web_index._open``) are the live code paths, not copies, so
the rebuilt index cannot drift from what the worker would have written.

Pages are indexed regardless of ``quarantined_at``: quarantine is enforced
in the retrieval queries (``web_memory``), and lifting it must not require a
reindex. ``reset-watermark`` is the in-place alternative — it NULLs
``indexed_at`` so the worker re-embeds into the LIVE directory — for the
case where a second directory is not wanted (a small store, or no room).

WHAT THIS TOOL MAY TOUCH (2026-09-06). Exactly one directory —
``LANCEDB_WEB_DIR`` or a new one given with ``--out`` — and exactly one table
inside it, ``web_index.TABLE`` (``web_chunks``). It refuses ``LANCEDB_DIR``
and anything under it: that is the SALESFORCE corpus, a different table
(``LANCEDB_TABLE``, default ``chunks``) whose sidecar pins one table per
directory, so building web chunks there would make the CRM index
unopenable. Conversation vectors are not in LanceDB at all — they are rows in
PostgreSQL ``conversation_chunks`` — and this tool has no statement that names
them. The only rows it writes in PostgreSQL are ``web_pages.indexed_at`` and
``web_pages.chunk_version``.

INTERRUPTION AND CONCURRENCY.

* A build never touches the live index, so an interrupted one loses no serving
  capability. ``--resume`` continues into the same directory: it drops the
  highest ``page_id`` present (the only page a partial flush can have split)
  and restarts from there, so a resumed run cannot duplicate a row.
* One LanceDB directory, one writer. ``web_index._index_lock`` is an
  ``asyncio.Lock`` and cannot see a second PROCESS, which is exactly what
  ``docker exec … python -m tools.reindex_web`` is. Both the worker's
  ``index_pending`` and this tool take ``web_index.write_lock`` — an ``flock``
  beside the directory, released by the kernel if the holder dies.
* Pages re-indexed by the worker DURING a long build went into the OLD
  directory only, so the swap would silently revert them. ``adopt`` (run once,
  after the swap) queues that window for the worker and only then records
  which pages the new index actually holds.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

if os.path.isdir("/app"):
    sys.path.insert(0, "/app")

from app import db, llm, web_index  # noqa: E402
from app.config import settings  # noqa: E402
from app.embedding_index import load_metadata, vector_dimension  # noqa: E402

#: Chunks per embedding request. Identical to web_index._EMBED_BATCH on
#: purpose: the sidecar answered 64 in ~320 ms warm (5 ms/chunk), and larger
#: requests measurably queued a user's query embedding behind them.
EMBED_BATCH = 64

#: Pages fetched per PostgreSQL round trip (keyset on id). A page's text is
#: up to ~180K chars (64 chunks x 3,200), so 200 pages is at most ~36 MB in
#: flight — bounded regardless of how large the store grows.
PAGE_BATCH = 200

#: Rows buffered before one LanceDB ``add``. Each add is one fragment and one
#: version; the live index accumulated 418 versions from small adds, and the
#: closing ``optimize`` here compacts whatever is left.
ADD_BATCH = 1024

#: Written into the built directory so `adopt` knows, without the operator
#: retyping anything, WHEN the build started (the window whose newer revisions
#: the swap would otherwise revert) and WHICH chunker produced it.
MANIFEST_FILENAME = ".reindex-manifest.json"


@dataclass
class BuildReport:
    directory: str
    started_at: str
    pages_total: int = 0
    pages_indexed: int = 0
    pages_thin: int = 0
    chunks: int = 0
    dimension: int = 0
    rows: int = 0
    distinct_pages: int = 0
    seconds: float = 0.0
    embed_seconds: float = 0.0
    limited: bool = False
    problems: List[str] = field(default_factory=list)

    @property
    def validated(self) -> bool:
        return not self.problems


# ---------------------------------------------------------------------------
# Reading the store
# ---------------------------------------------------------------------------


def iter_pages(limit: Optional[int] = None, after: int = 0) -> Iterator[dict]:
    """Every page with text, in id order, in bounded batches.

    Keyset pagination (``id > last``) rather than OFFSET: OFFSET re-scans
    everything it skips, so the last batch of a 100k-page store would cost
    as much as the whole table. One short transaction per batch keeps the
    statement under ``APP_DB_STATEMENT_TIMEOUT_MS`` however large the store.

    ``after`` is what makes a resume cheap: the same keyset cursor, started
    where the interrupted run's durable rows end.
    """
    last = int(after)
    yielded = 0
    while True:
        want = PAGE_BATCH if limit is None else min(PAGE_BATCH, limit - yielded)
        if want <= 0:
            return
        with db.connection() as con:
            rows = con.execute(
                "SELECT id, url, title, text, fetched_at FROM web_pages "
                "WHERE id > %s AND text <> '' ORDER BY id LIMIT %s",
                (last, want),
            ).fetchall()
        if not rows:
            return
        for row in rows:
            yield dict(row)
            yielded += 1
        last = int(rows[-1]["id"])


@contextmanager
def _pointed_at(directory: str) -> Iterator[None]:
    """Run ``web_index`` against ``directory`` instead of the live one.

    ``web_index._open`` reads ``settings.lancedb_web_dir``; swapping the
    setting for the duration of the build is what lets this tool reuse the
    live table-creation and sidecar code instead of re-typing the schema —
    a copy is exactly how a rebuilt index ends up one column different from
    the one the worker writes. Restored on every exit path.
    """
    previous = settings.lancedb_web_dir
    settings.lancedb_web_dir = directory
    try:
        yield
    finally:
        settings.lancedb_web_dir = previous


def _same_path(a: str, b: str) -> bool:
    return os.path.abspath(a).rstrip("/") == os.path.abspath(b).rstrip("/")


def _within(child: str, parent: str) -> bool:
    """True when `child` IS `parent` or lives underneath it."""
    child_abs = os.path.abspath(child).rstrip("/")
    parent_abs = os.path.abspath(parent).rstrip("/")
    return child_abs == parent_abs or child_abs.startswith(parent_abs + os.sep)


def assert_not_salesforce(directory: str) -> None:
    """Refuse any path that is, contains, or lives inside the CRM corpus.

    ``LANCEDB_DIR`` holds the Salesforce vectors in table ``LANCEDB_TABLE``
    (default ``chunks``). ``embedding_index`` pins exactly ONE table per
    directory in its sidecar, so creating ``web_chunks`` there does not merely
    add a table — it makes the sidecar and the table set disagree, and the RAG
    engine's index stops opening. Nothing about ``--out`` is validated by the
    filesystem: an empty or absent ``/data/lancedb`` on a box whose CRM sync
    has not run yet would have been accepted by the "must be empty" check
    alone. The containment test runs in BOTH directions so neither a
    subdirectory of the CRM corpus nor a parent that swallows it gets through.
    """
    salesforce = settings.lancedb_dir
    if _within(directory, salesforce) or _within(salesforce, directory):
        raise SystemExit(
            f"refusing {directory!r}: it overlaps the SALESFORCE index directory "
            f"{salesforce!r} (LANCEDB_DIR, table {settings.lancedb_table!r}). "
            "This tool only ever writes the public-web index "
            f"({settings.lancedb_web_dir!r}, table {web_index.TABLE!r}). "
            "Choose an --out outside the Salesforce corpus."
        )


def manifest_path(directory: str) -> str:
    return os.path.join(directory, MANIFEST_FILENAME)


def read_manifest(directory: str) -> Optional[Dict[str, Any]]:
    """The build manifest in `directory`, or None when there is not one."""
    try:
        with open(manifest_path(directory), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_manifest(directory: str, data: Dict[str, Any]) -> None:
    """Atomically, via the same helper the sidecar uses."""
    web_index._atomic_write_json(manifest_path(directory), data)


def _table_page_ids(directory: str) -> List[int]:
    """Distinct ``page_id`` values physically present in `directory`'s table.

    The only evidence this tool accepts that a page is represented in an
    index — never a counter it kept, never a page it believes it wrote.
    """
    import lancedb  # lazy
    import pyarrow.compute as pc

    conn = lancedb.connect(directory)
    if web_index.TABLE not in conn.table_names():
        return []
    table = conn.open_table(web_index.TABLE)
    rows = int(table.count_rows())
    if not rows:
        return []
    ids = table.search().select(["page_id"]).limit(rows).to_arrow()
    return sorted(int(v) for v in pc.unique(ids["page_id"]).to_pylist())


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


async def build(
    out_dir: str,
    *,
    limit: Optional[int] = None,
    progress_every: int = 100,
    resume: bool = False,
    log=print,
) -> BuildReport:
    """Embed every indexable page into a NEW LanceDB directory and validate it.

    Refuses the live directory (that is ``reset-watermark``'s job), the
    Salesforce corpus, and any directory that already has content — unless
    ``resume=True``, which continues an interrupted build in place. Raises on
    an embedding or write failure; the partial directory is left for
    inspection and can be resumed or discarded.

    WHY A RESUME IS SAFE HERE, when "resume" is usually where corruption
    comes from. Pages are read in ``id`` order and their rows are appended in
    that order, so everything below the highest ``page_id`` in the table is
    complete; only that highest page can have been split by a partial flush.
    Deleting exactly that page and restarting from it therefore cannot
    duplicate a row and cannot skip one — and the watermark is read back from
    the TABLE, not from a counter this process kept, so a manifest that is
    stale or a crash between two writes changes nothing.
    """
    started = datetime.now(timezone.utc)
    report = BuildReport(directory=out_dir, started_at=started.isoformat(), limited=limit is not None)
    if _same_path(out_dir, settings.lancedb_web_dir):
        raise SystemExit(
            f"refusing to build into the LIVE index directory {out_dir!r}. "
            "A rebuild in place is `reset-watermark`; a swap needs a new --out."
        )
    assert_not_salesforce(out_dir)
    existing = read_manifest(out_dir)
    if os.path.isdir(out_dir) and os.listdir(out_dir) and not resume:
        hint = (
            " It holds an interrupted build of this tool — re-run with --resume "
            "to continue it, or choose a fresh --out to start over."
            if existing and not existing.get("complete")
            else " Choose a new --out (the default is timestamped)."
        )
        raise SystemExit(
            f"refusing to build into non-empty {out_dir!r}: this tool never "
            "deletes an index." + hint
        )

    t0 = time.perf_counter()
    pending_texts: List[str] = []
    pending_meta: List[dict] = []
    rows_buffer: List[dict] = []
    table = None
    resume_after = 0

    if resume:
        if limit is not None:
            raise SystemExit("--resume and --limit are mutually exclusive: a smoke test is not a build to finish.")
        if existing is None:
            raise SystemExit(
                f"--resume needs a build manifest in {out_dir!r} and there is none. "
                "That directory was not written by this tool; refusing to touch it."
            )
        if existing.get("complete"):
            raise SystemExit(
                f"the build in {out_dir!r} already completed "
                f"({existing.get('chunks')} chunks); nothing to resume."
            )
        if int(existing.get("chunker_version") or 0) != int(web_index.CHUNKER_VERSION):
            raise SystemExit(
                f"the interrupted build used chunker {existing.get('chunker_version')} "
                f"and this code is chunker {web_index.CHUNKER_VERSION}. Resuming would "
                "mix two chunk shapes in one table; start a fresh --out."
            )
        # Keep the ORIGINAL start: `adopt`'s window has to cover the whole
        # build, including the attempt that was interrupted.
        report.started_at = str(existing.get("started_at") or report.started_at)

    def _flush_rows() -> None:
        nonlocal table
        if not rows_buffer:
            return
        if table is None:
            _conn, table, _meta = web_index._open(create_dim=len(rows_buffer[0]["vector"]))
            assert table is not None
        table.add(list(rows_buffer))
        rows_buffer.clear()

    async def _embed_pending() -> None:
        if not pending_texts:
            return
        t_embed = time.perf_counter()
        vectors = await llm.embed_texts(list(pending_texts), kind="index")
        report.embed_seconds += time.perf_counter() - t_embed
        if len(vectors) != len(pending_texts):
            raise RuntimeError(
                f"embedding service returned {len(vectors)} vectors for "
                f"{len(pending_texts)} texts"
            )
        for meta, vec in zip(pending_meta, vectors):
            if report.dimension == 0:
                report.dimension = len(vec)
            elif len(vec) != report.dimension:
                raise RuntimeError(
                    f"embedding dimension changed mid-build: {len(vec)} != {report.dimension}"
                )
            rows_buffer.append({**meta, "vector": list(vec)})
        report.chunks += len(pending_texts)
        pending_texts.clear()
        pending_meta.clear()
        if len(rows_buffer) >= ADD_BATCH:
            await asyncio.to_thread(_flush_rows)

    def _snapshot(complete: bool) -> None:
        _write_manifest(
            out_dir,
            {
                "tool": "reindex_web",
                "started_at": report.started_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "chunker_version": int(web_index.CHUNKER_VERSION),
                "model_id": settings.embed_model,
                "table": web_index.TABLE,
                "dimension": int(report.dimension),
                "pages_total": int(report.pages_total),
                "pages_indexed": int(report.pages_indexed),
                "pages_thin": int(report.pages_thin),
                "chunks": int(report.chunks),
                "limited": bool(report.limited),
                "complete": bool(complete),
            },
        )

    # ONE writer per directory, across processes. Held for the whole build: a
    # second `reindex_web` (or an orchestrator that has already been swapped
    # onto this directory) writing the same Lance manifest is a corrupt table,
    # not a slow one, and `_index_lock` cannot see another interpreter.
    with web_index.write_lock(out_dir, wait_s=0.0), _pointed_at(out_dir):
        if resume:
            resume_after = await asyncio.to_thread(_rewind_partial_build, out_dir, report)
            log(
                f"  resuming after page id {resume_after}: "
                f"{report.chunks} chunk(s) over {report.pages_indexed} page(s) already durable"
            )
        _snapshot(complete=False)
        for page in iter_pages(limit=limit, after=resume_after):
            report.pages_total += 1
            pieces = web_index.chunk_page(page["text"])
            if not pieces:
                report.pages_thin += 1
                continue
            report.pages_indexed += 1
            for piece in pieces:
                pending_texts.append(piece)
                pending_meta.append(
                    {
                        "page_id": int(page["id"]),
                        "url": page["url"],
                        "title": page["title"] or "",
                        "text": piece,
                        "fetched_at": str(page["fetched_at"]),
                    }
                )
                if len(pending_texts) >= EMBED_BATCH:
                    await _embed_pending()
            if progress_every and report.pages_total % progress_every == 0:
                elapsed = time.perf_counter() - t0
                per_chunk = (report.embed_seconds / report.chunks * 1000) if report.chunks else 0.0
                log(
                    f"  pages={report.pages_total} indexed={report.pages_indexed} "
                    f"thin={report.pages_thin} chunks={report.chunks} "
                    f"elapsed={elapsed:.0f}s embed={per_chunk:.1f} ms/chunk"
                )
                _snapshot(complete=False)
        await _embed_pending()
        await asyncio.to_thread(_flush_rows)
        if table is not None:
            # One compaction at the end: the adds above left one fragment
            # each, and a fragmented table scans measurably slower (+27% on
            # the live index before its first optimize).
            await asyncio.to_thread(web_index._optimize_table, table)

    report.seconds = time.perf_counter() - t0
    if report.pages_indexed == 0:
        report.problems.append("no indexable page (every page shorter than the chunk minimum)")
        _snapshot(complete=False)
        return report
    validate(report)
    # `complete` is what stops `--resume` restarting a finished build and what
    # lets `adopt` trust the window. Only a VALIDATED build earns it.
    _snapshot(complete=report.validated and not report.limited)
    return report


def _rewind_partial_build(out_dir: str, report: BuildReport) -> int:
    """Make an interrupted build's directory a consistent prefix; return its end.

    Everything is read back from the table rather than from the manifest,
    because the manifest is written by the same process that may have died
    between two writes. The highest ``page_id`` present is the only page a
    partial flush can have split, so it is deleted and re-done; every lower id
    is complete by the id ordering of the build.
    """
    import lancedb  # lazy
    import pyarrow.compute as pc

    conn = lancedb.connect(out_dir)
    if web_index.TABLE not in conn.table_names():
        return 0  # died before the first flush; a fresh start over an empty dir
    table = conn.open_table(web_index.TABLE)
    if int(table.count_rows()) == 0:
        return 0
    ids = table.search().select(["page_id"]).limit(int(table.count_rows())).to_arrow()
    highest = int(pc.max(ids["page_id"]).as_py())
    table.delete(f"page_id >= {highest}")

    rows = int(table.count_rows())
    report.chunks = rows
    report.dimension = int(vector_dimension(table))
    if rows:
        kept = table.search().select(["page_id"]).limit(rows).to_arrow()
        report.pages_indexed = int(len(pc.unique(kept["page_id"])))
    else:
        report.pages_indexed = 0
    with db.connection() as con:
        total = int(
            con.execute(
                "SELECT count(*) AS n FROM web_pages WHERE text <> '' AND id < %s",
                (highest,),
            ).fetchone()["n"]
        )
    report.pages_total = total
    # Every page below the watermark was either indexed or too thin; a page
    # purged since the interrupted run can make this negative, hence the clamp.
    report.pages_thin = max(0, total - report.pages_indexed)
    return max(0, highest - 1)


def validate(report: BuildReport) -> BuildReport:
    """Re-open the new directory exactly as the app would and check counts.

    Distinct ``page_id`` == pages indexed is the invariant that matters: a
    missing page is a page the platform has read but can no longer recall.
    """
    import lancedb  # lazy
    import pyarrow.compute as pc

    directory = report.directory
    metadata = None
    try:
        metadata = load_metadata(directory)
    except Exception as exc:  # noqa: BLE001 — reported, not raised
        report.problems.append(f"sidecar unreadable: {exc}")
    if metadata is None:
        if not report.problems:
            report.problems.append("sidecar missing")
    else:
        if metadata.model_id != settings.embed_model:
            report.problems.append(
                f"sidecar model {metadata.model_id!r} != configured {settings.embed_model!r}"
            )
        if metadata.dimension != report.dimension:
            report.problems.append(
                f"sidecar dimension {metadata.dimension} != embedded {report.dimension}"
            )
        if metadata.table != web_index.TABLE:
            report.problems.append(f"sidecar table {metadata.table!r} != {web_index.TABLE!r}")
    try:
        conn = lancedb.connect(directory)
        if web_index.TABLE not in conn.table_names():
            report.problems.append(f"table {web_index.TABLE!r} was not created")
            return report
        table = conn.open_table(web_index.TABLE)
        report.rows = int(table.count_rows())
        if report.rows:
            ids = table.search().select(["page_id"]).limit(report.rows).to_arrow()
            report.distinct_pages = len(pc.unique(ids["page_id"]))
    except Exception as exc:  # noqa: BLE001
        report.problems.append(f"cannot open the new table: {type(exc).__name__}: {exc}")
        return report
    if report.rows != report.chunks:
        report.problems.append(f"rows {report.rows} != chunks embedded {report.chunks}")
    if report.distinct_pages != report.pages_indexed:
        report.problems.append(
            f"distinct page_id {report.distinct_pages} != indexable pages {report.pages_indexed}"
        )
    return report


def swap_instructions(report: BuildReport) -> str:
    """The exact env change, and its reverse. Printed only for a validated,
    complete (un-limited) build."""
    live = settings.lancedb_web_dir
    return "\n".join(
        [
            "",
            "To switch the orchestrator to the new index:",
            f"  1. In .env set:   LANCEDB_WEB_DIR={report.directory}",
            "     (the key is documented in .env.example; the orchestrator reads .env via env_file)",
            "  2. Recreate the orchestrator so it picks the value up:   ./techsara up",
            "  3. Check GET /health -> web_index.directory == the new path and "
            f"web_index.rows == {report.rows} (distinct_pages {report.distinct_pages})",
            "  4. Finish the swap — ONE command, AFTER the orchestrator is on the new path:",
            "       python -m tools.reindex_web adopt --yes",
            "     It re-queues every page the worker re-indexed WHILE this build ran (their",
            "     newer chunks went to the old directory only, so the swap would revert them),",
            f"     then records chunk_version={web_index.CHUNKER_VERSION} for the pages this index",
            "     demonstrably holds — without which the worker re-chunks the whole corpus",
            "     from scratch to produce byte-identical vectors.",
            "",
            "Rollback (no data is lost either way — the old directory was not touched):",
            f"  set LANCEDB_WEB_DIR={live} in .env, then ./techsara up",
            "",
            f"Do not delete {live} until the new index has served real questions.",
            "The worker builds the ANN index on its first maintenance cycle if the table "
            f"is over WEB_INDEX_ANN_MIN_ROWS ({settings.web_index_ann_min_rows}).",
        ]
    )


# ---------------------------------------------------------------------------
# adopt — the post-swap reconciliation
# ---------------------------------------------------------------------------


def adopt(directory: str, *, apply: bool) -> Dict[str, int]:
    """Reconcile PostgreSQL with an index that has just become LIVE.

    Two writes, and the ORDER between them is the whole correctness argument.

    1. QUEUE THE BUILD WINDOW. A page the refresh worker re-read or re-chunked
       while the build was running was written to the OLD directory; the new
       one holds whatever that page looked like when the build walked past it.
       Swapping therefore reverts it — silently, because every count still
       matches. Every page with `indexed_at` or `fetched_at` at/after the build
       started has its watermark cleared, so the worker rebuilds it here.

       `indexed_at` matters as much as `fetched_at` now: a V24 re-chunk moves
       only `indexed_at`, so a `fetched_at`-only window would miss exactly the
       repairs this phase introduced.

    2. RECORD WHAT THE INDEX ACTUALLY HOLDS. The pages are stamped with the
       chunker that built them, so the worker does not immediately re-chunk the
       entire corpus to produce byte-identical vectors — 2,208 pages of
       embedding for nothing, which is what happens without this.

       The page ids come from the TABLE (`_table_page_ids`), never from a
       counter, and `indexed_at IS NOT NULL` excludes everything step 1 just
       queued. So nothing is claimed for a page whose vectors are not
       physically there, and nothing that needs rebuilding is marked done.

    Refuses to run against anything but the configured live directory: the
    stamp is a statement about what the app is SERVING, and making it before
    the swap would tell the worker the repair is finished when it has not
    started.
    """
    live = settings.lancedb_web_dir
    assert_not_salesforce(live)
    manifest = read_manifest(live)
    if manifest is None:
        raise SystemExit(
            f"no build manifest in the live index directory {live!r}: it was not "
            "built by this tool, so there is no build window to reconcile. "
            "(Run `adopt` AFTER setting LANCEDB_WEB_DIR to the new directory and "
            "recreating the orchestrator.)"
        )
    if not _same_path(directory, live):
        raise SystemExit(
            f"refusing to adopt {directory!r}: the live index directory is {live!r}. "
            "Swap first, then adopt."
        )
    if not manifest.get("complete"):
        raise SystemExit(
            f"the build in {live!r} never completed or never validated; refusing to "
            "adopt a partial index. Finish it with `build --out … --resume`."
        )
    if int(manifest.get("chunker_version") or 0) != int(web_index.CHUNKER_VERSION):
        raise SystemExit(
            f"that index was built by chunker {manifest.get('chunker_version')} and this "
            f"code is chunker {web_index.CHUNKER_VERSION}; adopting it would record a "
            "chunk shape the code no longer produces. Rebuild."
        )
    since = _parse_since(str(manifest["started_at"]))
    page_ids = _table_page_ids(live)

    out = {
        "window_pages": count_watermark_reset(since),
        "table_pages": len(page_ids),
        "queued": 0,
        "stamped": 0,
    }
    if not apply:
        return out
    out["queued"] = reset_watermark(since)
    out["stamped"] = _stamp_chunk_version(page_ids)
    return out


def _stamp_chunk_version(page_ids: List[int]) -> int:
    """Record `CHUNKER_VERSION` for pages the live index demonstrably holds.

    `indexed_at IS NOT NULL` is the guard that makes this safe next to a
    freshly cleared watermark: a page queued for re-indexing is skipped, and
    `web_index.index_pending` stamps it when it actually rebuilds it.
    `GREATEST` mirrors `db.mark_web_pages_indexed` — a version never moves
    down, or a page would re-enter the repair queue for ever.
    """
    if not page_ids:
        return 0
    changed = 0
    version = int(web_index.CHUNKER_VERSION)
    for start in range(0, len(page_ids), 5000):
        batch = [int(i) for i in page_ids[start : start + 5000]]
        with db.connection() as con:
            cur = con.execute(
                "UPDATE web_pages SET chunk_version = GREATEST(chunk_version, %s) "
                "WHERE id = ANY(%s) AND indexed_at IS NOT NULL AND chunk_version < %s",
                (version, batch, version),
            )
            changed += int(cur.rowcount or 0)
    return changed


# ---------------------------------------------------------------------------
# reset-watermark
# ---------------------------------------------------------------------------


#: The "moved while the build ran" window. BOTH timestamps, because they
#: answer different questions and only one of them existed before V24:
#: `fetched_at` moves when the page was re-downloaded, `indexed_at` when its
#: vectors were rebuilt — and a stale-chunker repair rebuilds vectors from
#: stored text without any fetch at all. A `fetched_at`-only window therefore
#: misses precisely the pages this phase taught the worker to repair, and the
#: swap would revert them with every count still adding up.
_WINDOW_SQL = "(indexed_at >= %s OR fetched_at >= %s)"


def count_watermark_reset(since: Optional[datetime] = None) -> int:
    """How many pages ``reset_watermark`` would queue. Printed before mutating."""
    with db.connection() as con:
        if since is None:
            row = con.execute(
                "SELECT count(*) AS n FROM web_pages WHERE indexed_at IS NOT NULL AND text <> ''"
            ).fetchone()
        else:
            row = con.execute(
                "SELECT count(*) AS n FROM web_pages WHERE indexed_at IS NOT NULL "
                f"AND text <> '' AND {_WINDOW_SQL}",
                (since, since),
            ).fetchone()
    return int(row["n"])


def reset_watermark(since: Optional[datetime] = None) -> int:
    """NULL ``indexed_at`` so the worker re-embeds pages into the live directory.

    Without ``since`` this is ``db.reset_web_index_watermark`` — every page.
    With it, only pages re-fetched OR re-indexed at/after that instant: the
    "moved during the build" set after a build-alongside swap, which is a
    handful of pages rather than the whole store.
    """
    if since is None:
        return int(db.reset_web_index_watermark())
    with db.connection() as con:
        cur = con.execute(
            "UPDATE web_pages SET indexed_at = NULL WHERE indexed_at IS NOT NULL "
            f"AND text <> '' AND {_WINDOW_SQL}",
            (since, since),
        )
        return int(cur.rowcount or 0)


def _parse_since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _default_out() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{settings.lancedb_web_dir.rstrip('/')}.{stamp}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tools.reindex_web",
        description="Rebuild the web vector index from PostgreSQL (build alongside, then swap).",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="embed every page with text into a NEW directory and validate it")
    b.add_argument("--out", default=None, help="new index directory (default: <LANCEDB_WEB_DIR>.<utc stamp>)")
    b.add_argument("--limit", type=int, default=None, help="smoke test: only the first N pages by id (never swap such a build)")
    b.add_argument("--progress-every", type=int, default=100, help="print a progress line every N pages")
    b.add_argument("--resume", action="store_true", help="continue an interrupted build in the same --out")
    r = sub.add_parser("reset-watermark", help="queue pages for the worker to re-embed into the LIVE directory")
    r.add_argument("--since", default=None, help="only pages re-fetched or re-indexed at/after this ISO-8601 instant (default: every page)")
    r.add_argument("--yes", action="store_true", help="apply; without it the count is printed and nothing changes")
    a = sub.add_parser("adopt", help="after the swap: re-queue the build window, record which chunker the live index holds")
    a.add_argument("--yes", action="store_true", help="apply; without it the counts are printed and nothing changes")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # The documented flag form `--reset-watermark` is the subcommand.
    argv = ["reset-watermark" if a == "--reset-watermark" else a for a in argv]
    args = _parser().parse_args(argv)

    if args.command == "reset-watermark":
        since = _parse_since(args.since) if args.since else None
        scope = f"fetched at/after {since.isoformat()}" if since else "every page"
        n = count_watermark_reset(since)
        print(f"pages that would be queued for re-indexing ({scope}): {n}")
        if not args.yes:
            print("nothing changed — re-run with --yes to apply")
            return 0
        done = reset_watermark(since)
        print(f"queued {done} page(s); the worker re-embeds 40 pages per cycle "
              f"(WEB_WORKER_INTERVAL_S={settings.web_worker_interval_s}) into {settings.lancedb_web_dir}")
        return 0

    if args.command == "adopt":
        live = settings.lancedb_web_dir
        counts = adopt(live, apply=args.yes)
        print(f"adopting the live web index: {live}")
        print(
            f"  pages the index holds: {counts['table_pages']}; "
            f"pages that moved during the build (to re-queue): {counts['window_pages']}"
        )
        if not args.yes:
            print("nothing changed — re-run with --yes to apply")
            return 0
        print(
            f"queued {counts['queued']} page(s) for re-indexing; "
            f"recorded chunk_version={web_index.CHUNKER_VERSION} on {counts['stamped']} page(s)"
        )
        return 0

    out_dir = args.out or _default_out()
    print(f"{'resuming' if args.resume else 'building'} web index: {out_dir}")
    print(f"  source: PostgreSQL web_pages (text <> ''), live index: {settings.lancedb_web_dir}")
    print(f"  model: {settings.embed_model} via {settings.embed_base_url}, batch {EMBED_BATCH}")
    print(f"  chunker: v{web_index.CHUNKER_VERSION}")
    try:
        report = asyncio.run(
            build(out_dir, limit=args.limit, progress_every=args.progress_every, resume=args.resume)
        )
    except web_index.IndexBusy as exc:
        # A traceback here reads like a crash; it is a queue, not a fault.
        print(f"REFUSED: {exc}", file=sys.stderr)
        print("Wait for the other writer to finish, or pick a different --out.", file=sys.stderr)
        return 2
    print(
        f"done in {report.seconds:.1f}s: pages={report.pages_total} indexed={report.pages_indexed} "
        f"thin={report.pages_thin} chunks={report.chunks} dim={report.dimension} "
        f"rows={report.rows} distinct_pages={report.distinct_pages} "
        f"embed={report.embed_seconds:.1f}s"
    )
    if not report.validated:
        print("VALIDATION FAILED — do not switch to this directory:")
        for problem in report.problems:
            print(f"  - {problem}")
        return 1
    print("validated: distinct page_id == indexable pages, rows == chunks, sidecar matches the model")
    if report.limited:
        print(f"this was a --limit smoke test ({report.pages_total} pages); not a swap candidate")
        return 0
    print(swap_instructions(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
