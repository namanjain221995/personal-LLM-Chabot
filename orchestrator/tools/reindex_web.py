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
"""
from __future__ import annotations

import argparse
import asyncio
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
from app.embedding_index import load_metadata  # noqa: E402

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


def iter_pages(limit: Optional[int] = None) -> Iterator[dict]:
    """Every page with text, in id order, in bounded batches.

    Keyset pagination (``id > last``) rather than OFFSET: OFFSET re-scans
    everything it skips, so the last batch of a 100k-page store would cost
    as much as the whole table. One short transaction per batch keeps the
    statement under ``APP_DB_STATEMENT_TIMEOUT_MS`` however large the store.
    """
    last = 0
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


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


async def build(
    out_dir: str,
    *,
    limit: Optional[int] = None,
    progress_every: int = 100,
    log=print,
) -> BuildReport:
    """Embed every indexable page into a NEW LanceDB directory and validate it.

    Refuses the live directory (that is ``reset-watermark``'s job) and any
    directory that already has content (this tool never deletes). Raises on
    an embedding or write failure — a partial directory is left in place for
    inspection and the operator picks a fresh ``--out``.
    """
    started = datetime.now(timezone.utc)
    report = BuildReport(directory=out_dir, started_at=started.isoformat(), limited=limit is not None)
    if _same_path(out_dir, settings.lancedb_web_dir):
        raise SystemExit(
            f"refusing to build into the LIVE index directory {out_dir!r}. "
            "A rebuild in place is `reset-watermark`; a swap needs a new --out."
        )
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise SystemExit(
            f"refusing to build into non-empty {out_dir!r}: this tool never "
            "deletes an index. Choose a new --out (the default is timestamped)."
        )

    t0 = time.perf_counter()
    pending_texts: List[str] = []
    pending_meta: List[dict] = []
    rows_buffer: List[dict] = []
    table = None

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

    with _pointed_at(out_dir):
        for page in iter_pages(limit=limit):
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
        return report
    validate(report)
    return report


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
            "  4. Pages re-fetched WHILE this build ran were indexed into the OLD directory only.",
            "     Queue them for the worker (it re-embeds into the new one):",
            f"       python -m tools.reindex_web reset-watermark --since {report.started_at} --yes",
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
# reset-watermark
# ---------------------------------------------------------------------------


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
                "AND text <> '' AND fetched_at >= %s",
                (since,),
            ).fetchone()
    return int(row["n"])


def reset_watermark(since: Optional[datetime] = None) -> int:
    """NULL ``indexed_at`` so the worker re-embeds pages into the live directory.

    Without ``since`` this is ``db.reset_web_index_watermark`` — every page.
    With it, only pages fetched at or after that instant: the "changed during
    the build" set after a build-alongside swap, which is a handful of pages
    rather than the whole store.
    """
    if since is None:
        return int(db.reset_web_index_watermark())
    with db.connection() as con:
        cur = con.execute(
            "UPDATE web_pages SET indexed_at = NULL WHERE indexed_at IS NOT NULL "
            "AND text <> '' AND fetched_at >= %s",
            (since,),
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
    r = sub.add_parser("reset-watermark", help="queue pages for the worker to re-embed into the LIVE directory")
    r.add_argument("--since", default=None, help="only pages fetched at/after this ISO-8601 instant (default: every page)")
    r.add_argument("--yes", action="store_true", help="apply; without it the count is printed and nothing changes")
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

    out_dir = args.out or _default_out()
    print(f"building web index: {out_dir}")
    print(f"  source: PostgreSQL web_pages (text <> ''), live index: {settings.lancedb_web_dir}")
    print(f"  model: {settings.embed_model} via {settings.embed_base_url}, batch {EMBED_BATCH}")
    report = asyncio.run(build(out_dir, limit=args.limit, progress_every=args.progress_every))
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
