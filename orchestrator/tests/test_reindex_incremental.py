"""The incremental reindex driver: bounded, resumable, and honest about stopping.

`tools/reindex_incremental.py` drives `web_index.index_pending` — it must never
grow its own chunker, its own embedding call or its own checkpoint. This file
holds it to all three:

* **The checkpoint is `web_pages.chunk_version`, not a file.** Proven by
  stopping a run mid-queue and resuming it: the stamped pages stay stamped, the
  rest come back, and the LanceDB table does not gain a duplicate row. The tool
  opens exactly one file, `/proc/self/status`, and never for writing.
* **Every limit actually stops it**, and stopping on a limit is exit 0 while
  stopping for any other reason with work left is not. That distinction is the
  whole point: `index_pending` NEVER RAISES (it logs and returns 0), so a dead
  embedding service and a finished queue are indistinguishable from the return
  value alone. Only the queue depth tells them apart.
* **A batch that writes no chunks is not a stall** — thin pages are stamped and
  leave the queue with zero chunks written, and calling that a failure would
  exit non-zero on a healthy run.

No network and no embedding service: `llm.embed_texts` is a deterministic fake
where the real `index_pending` is exercised, and a stub replaces
`index_pending` entirely everywhere else. PostgreSQL is the suite's test
database; LanceDB writes go to tmp_path.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import signal
from typing import List, Optional

import pytest

from app import db, health, llm, web_index
from app.config import settings
from tools import reindex_incremental as ri

MEDIUM = ("A shorter page that still clears the chunk minimum. " * 12).strip()
LONG = ("The office holder is named in this paragraph. " * 80).strip()
THIN = "Too short to index."


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _page(url: str, text: str = MEDIUM) -> int:
    key = url.split("//", 1)[-1]
    return int(
        db.upsert_web_page(
            url_key=key, url=url, canonical_url=url, title="Page", text=text,
            content_type="text/html", fetch_status=200,
            content_hash=hashlib.sha256(text.encode()).hexdigest(),
        )["id"]
    )


def _row(page_id: int) -> dict:
    with db.connection() as con:
        return dict(
            con.execute(
                "SELECT id, indexed_at, chunk_version FROM web_pages WHERE id = %s",
                (page_id,),
            ).fetchone()
        )


def _run(argv: List[str], capsys):
    rc = ri.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _summary(out: str) -> dict:
    import json

    line = [l for l in out.splitlines() if l.startswith("summary-json ")]
    assert line, out
    return json.loads(line[-1][len("summary-json "):])


@pytest.fixture()
def fake_embed(monkeypatch):
    """The embedding service, replaced. Nothing in this file may reach it."""
    calls: List[int] = []

    async def embed(texts, **_kwargs):
        calls.append(len(texts))
        return [[float(len(t) % 7), 1.0, 0.5, 0.25] for t in texts]

    monkeypatch.setattr(llm, "embed_texts", embed)
    return calls


@pytest.fixture()
def stub_index_pending(monkeypatch):
    """A faithful stand-in: it drains the REAL queue without embedding anything.

    It takes the pages `index_pending` would have taken, stamps them the way
    `index_pending` stamps them, and reports a chunk count. What it leaves out
    is exactly the part these tests must not run — chunking, the embedding
    call, and the LanceDB write.
    """

    class Stub:
        def __init__(self) -> None:
            self.limits: List[int] = []
            self.chunks_per_page = 3
            self.stamp = True
            #: Calls that do nothing before the stub starts draining — the
            #: `IndexBusy` the live worker's lock produces, which is a queue
            #: rather than a fault.
            self.no_progress_calls = 0
            self.clock_step = 0.0
            self.clock = 0.0

        async def __call__(self, limit: int = 20, **_kwargs) -> int:
            self.limits.append(limit)
            self.clock += self.clock_step
            if self.no_progress_calls > 0:
                self.no_progress_calls -= 1
                return 0
            pages = db.get_unindexed_web_pages(
                limit, None, int(web_index.CHUNKER_VERSION)
            )
            if not self.stamp:
                return 0
            db.mark_web_pages_indexed(
                [int(p["id"]) for p in pages], int(web_index.CHUNKER_VERSION)
            )
            return len(pages) * self.chunks_per_page

    stub = Stub()
    monkeypatch.setattr(web_index, "index_pending", stub)
    return stub


@pytest.fixture()
def no_sleep(monkeypatch):
    """Record what the drain would have slept instead of sleeping it."""
    slept: List[float] = []
    real = asyncio.sleep

    async def fake(delay, *args, **kwargs):
        slept.append(float(delay))
        await real(0)

    monkeypatch.setattr(asyncio, "sleep", fake)
    return slept


# ---------------------------------------------------------------------------
# 1. the checkpoint already exists — verify it rather than invent one
# ---------------------------------------------------------------------------


def test_the_queue_is_the_checkpoint_and_a_stopped_run_resumes(fake_embed, tmp_path, monkeypatch):
    """Stop mid-queue, resume, and prove nothing was lost or duplicated.

    This is the claim the tool's docstring makes instead of shipping a progress
    file: `index_pending` stamps `chunk_version` only after the vectors are
    durable, and its write is delete-then-insert keyed by page_id, so a resumed
    run can only ever REPLACE a page's chunks.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "web"))
    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 1)
    ids = [_page(f"https://example.com/p{i}", LONG) for i in range(5)]

    first = asyncio.run(ri.drain(ri.Limits(batch_pages=5, pause_s=0)))
    assert first.stop_reason == "drained" and first.remaining == 0
    assert all(_row(i)["chunk_version"] == 1 for i in ids)

    # The chunker moves: every page is now owed a re-chunk.
    monkeypatch.setattr(web_index, "CHUNKER_VERSION", 2)
    assert sum(ri.queue_depth()) == 5

    # A run that stops early on its chunk budget: 2 pages, then the budget.
    partial = asyncio.run(
        ri.drain(ri.Limits(batch_pages=2, max_chunks=1, pause_s=0))
    )
    assert partial.stop_reason == "chunk-budget"
    assert partial.exit_code == 0, "a configured limit is not a failure"
    assert partial.remaining == 3 and partial.done == 2

    stamped = [i for i in ids if _row(i)["chunk_version"] == 2]
    assert len(stamped) == 2, "progress is durable in the row, batch by batch"
    assert sum(ri.queue_depth()) == 3

    # No second store: the only file the tool opens is /proc/self/status, and
    # nothing resembling a cursor was left beside the index.
    assert not [
        name for name in os.listdir(tmp_path)
        if "checkpoint" in name or "progress" in name or "cursor" in name
    ]

    # Resume — a fresh call is the resume, with no argument carried over.
    resumed = asyncio.run(ri.drain(ri.Limits(batch_pages=5, pause_s=0)))
    assert resumed.stop_reason == "drained" and resumed.remaining == 0
    assert resumed.backlog_start == 3, "it picked up exactly what was left"
    assert all(_row(i)["chunk_version"] == 2 for i in ids)

    # And the resumed run did not duplicate a single row.
    expected = sum(len(web_index.chunk_page(LONG)) for _ in ids)
    status = health._check_web_index()
    assert status["rows"] == expected, "a resumed page REPLACES its chunks"
    assert status["distinct_pages"] == 5


def test_a_batch_that_writes_no_chunks_is_progress_not_a_stall(fake_embed, tmp_path, monkeypatch):
    """Thin pages are stamped with zero chunks written.

    `index_pending` returns 0 for them (its "nothing chunkable" branch) and
    they still leave the queue. Judging progress by the return value would report a stall
    — a non-zero exit — on a perfectly healthy drain.
    """
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "web"))
    ids = [_page(f"https://example.com/thin{i}", THIN) for i in range(4)]

    report = asyncio.run(ri.drain(ri.Limits(batch_pages=2, pause_s=0)))

    assert report.chunks == 0
    assert report.stop_reason == "drained" and report.remaining == 0
    assert report.exit_code == 0
    assert all(_row(i)["chunk_version"] == web_index.CHUNKER_VERSION for i in ids)
    assert fake_embed == [], "a thin page must not reach the embedding service"


def test_queue_depth_is_exactly_what_index_pending_would_select():
    """The two counters add up to the selection predicate, with no overlap.

    `unindexed` is `indexed_at IS NULL`; `stale` is `indexed_at IS NOT NULL AND
    chunk_version < V`; both require `text <> ''`. If that ever stops being a
    partition of `get_unindexed_web_pages`'s WHERE clause, "remaining" becomes
    a lie and the exit code with it.
    """
    fresh = _page("https://example.com/fresh")
    stale = _page("https://example.com/stale")
    db.mark_web_pages_indexed([stale], 0)
    settled = _page("https://example.com/settled")
    db.mark_web_pages_indexed([settled], int(web_index.CHUNKER_VERSION))
    _page("https://example.com/empty", "")

    unindexed, stale_pages = ri.queue_depth()
    assert (unindexed, stale_pages) == (1, 1)
    selected = db.get_unindexed_web_pages(
        100, None, int(web_index.CHUNKER_VERSION)
    )
    assert unindexed + stale_pages == len(selected)
    assert {int(r["id"]) for r in selected} == {fresh, stale}


# ---------------------------------------------------------------------------
# 2. the limits
# ---------------------------------------------------------------------------


def test_batch_size_is_the_limit_passed_to_index_pending(stub_index_pending, no_sleep):
    for i in range(7):
        _page(f"https://example.com/b{i}")

    report = asyncio.run(ri.drain(ri.Limits(batch_pages=3, pause_s=0)))

    assert stub_index_pending.limits == [3, 3, 3]
    assert report.batches == 3 and report.remaining == 0
    assert report.chunks == 21


def test_the_wall_clock_stops_the_run_and_exits_zero(stub_index_pending, no_sleep):
    for i in range(20):
        _page(f"https://example.com/w{i}")
    stub_index_pending.clock_step = 30.0

    report = asyncio.run(
        ri.drain(
            ri.Limits(batch_pages=2, max_wall_clock_s=60.0, pause_s=0),
            clock=lambda: stub_index_pending.clock,
        )
    )

    assert report.stop_reason == "wall-clock"
    assert report.batches == 2, "checked BETWEEN batches — never mid-batch"
    assert report.remaining == 16
    assert report.exit_code == 0


def test_the_chunk_budget_stops_the_run_and_exits_zero(stub_index_pending, no_sleep):
    for i in range(20):
        _page(f"https://example.com/c{i}")
    stub_index_pending.chunks_per_page = 10

    report = asyncio.run(
        ri.drain(ri.Limits(batch_pages=2, max_chunks=25, pause_s=0))
    )

    assert report.stop_reason == "chunk-budget"
    assert report.batches == 2 and report.chunks == 40, (
        "the budget is checked BETWEEN batches: 20 chunks was under it, so a "
        "second batch ran and overshot to 40 rather than abandoning its work"
    )
    assert report.remaining == 16
    assert report.exit_code == 0


def test_the_pause_runs_between_batches_and_not_after_the_last(stub_index_pending, no_sleep):
    """The flag exists so a long drain cannot monopolise the embedding service
    interactive chat shares — but pausing after the final batch would only make
    the operator wait."""
    for i in range(6):
        _page(f"https://example.com/p{i}")

    report = asyncio.run(ri.drain(ri.Limits(batch_pages=2, pause_s=1.5)))

    assert report.batches == 3
    assert no_sleep == [1.5, 1.5], no_sleep


def test_zero_pause_sleeps_not_at_all(stub_index_pending, no_sleep):
    for i in range(4):
        _page(f"https://example.com/z{i}")

    asyncio.run(ri.drain(ri.Limits(batch_pages=2, pause_s=0)))

    assert no_sleep == []


# ---------------------------------------------------------------------------
# 3. stopping for a reason that is NOT a configured limit
# ---------------------------------------------------------------------------


def test_a_stall_is_detected_and_exits_non_zero(stub_index_pending, no_sleep):
    """The failure `index_pending` cannot report.

    It catches everything and returns 0 (its closing `except Exception`), so a broken
    embedding service leaves the queue full and the driver none the wiser
    unless it watches the queue depth. Three no-progress batches, then stop.
    """
    for i in range(10):
        _page(f"https://example.com/s{i}")
    stub_index_pending.stamp = False  # writes nothing, queue never moves

    report = asyncio.run(ri.drain(ri.Limits(batch_pages=2, pause_s=0)))

    assert report.stop_reason == "stalled"
    assert report.batches == ri._STALL_BATCHES
    assert report.remaining == 10 and report.done == 0
    assert report.exit_code == ri.EXIT_STALLED
    assert no_sleep == [ri._STALL_BACKOFF_S] * (ri._STALL_BATCHES - 1)


def test_a_transient_no_progress_batch_is_tolerated(stub_index_pending, no_sleep):
    """`index_pending` swallows `IndexBusy` when the live refresh worker holds
    the cross-process write lock past its 20 s wait. That is a queue, not a
    fault, so a no-progress batch below the stall threshold must not end the
    run — the drain backs off and tries again."""
    for i in range(6):
        _page(f"https://example.com/t{i}")
    stub_index_pending.no_progress_calls = ri._STALL_BATCHES - 1

    report = asyncio.run(ri.drain(ri.Limits(batch_pages=2, pause_s=0)))

    assert report.stop_reason == "drained" and report.remaining == 0
    assert report.batches == (ri._STALL_BATCHES - 1) + 3, "it recovered"
    assert no_sleep == [ri._STALL_BACKOFF_S] * (ri._STALL_BATCHES - 1)


def test_an_exception_from_index_pending_still_reports_and_exits_non_zero(
    monkeypatch, no_sleep, capsys
):
    """`index_pending` is documented never to raise. If it ever does, the run
    must still print its report: the peak-RSS reading exists nowhere but inside
    this process, and a traceback would cost a whole second run to get it."""
    _page("https://example.com/boom")

    async def boom(**_kwargs):
        raise RuntimeError("lance manifest is locked")

    monkeypatch.setattr(web_index, "index_pending", boom)

    rc, out, err = _run(["--batch-pages", "2", "--pause-s", "0"], capsys)

    assert rc == ri.EXIT_STALLED
    assert "FAILED with 1 page(s) left" in err and "lance manifest" in err
    assert "peak RSS" in out
    summary = _summary(out)
    assert summary["stop_reason"] == "failed"
    assert summary["error"].startswith("RuntimeError:")
    assert summary["remaining"] == 1


def test_an_interrupt_stops_after_the_current_batch_and_exits_non_zero(
    stub_index_pending, no_sleep
):
    for i in range(10):
        _page(f"https://example.com/i{i}")
    state = {"signum": None}

    def should_stop():
        return state["signum"]

    def on_batch(_line):
        state["signum"] = int(signal.SIGTERM)

    report = asyncio.run(
        ri.drain(
            ri.Limits(batch_pages=2, pause_s=0),
            should_stop=should_stop,
            on_batch=on_batch,
        )
    )

    assert report.stop_reason == "interrupted"
    assert report.batches == 1, "the batch in flight is always finished"
    assert report.remaining == 8
    assert report.exit_code == 128 + int(signal.SIGTERM)
    assert report.exit_code != 0


# ---------------------------------------------------------------------------
# 4. the CLI
# ---------------------------------------------------------------------------


def test_dry_run_reports_the_queue_and_embeds_nothing(monkeypatch, capsys):
    for i in range(5):
        _page(f"https://example.com/d{i}")
    stale = _page("https://example.com/old")
    db.mark_web_pages_indexed([stale], 0)

    async def explode(*_a, **_k):  # pragma: no cover - must never be called
        raise AssertionError("--dry-run must not index anything")

    monkeypatch.setattr(web_index, "index_pending", explode)

    rc, out, _err = _run(["--dry-run", "--batch-pages", "2"], capsys)

    assert rc == 0
    assert "queue: 6 page(s)" in out
    assert "new or changed text (indexed_at IS NULL): 5" in out
    assert f"stale chunks (chunk_version < {web_index.CHUNKER_VERSION}):    1" in out
    assert "at --batch-pages 2: 3 batch(es)" in out
    assert "nothing was embedded" in out
    assert "peak RSS" in out


def test_the_cli_refuses_the_salesforce_directory(monkeypatch, tmp_path, capsys):
    """`LANCEDB_WEB_DIR` is an environment variable and a typo in it points
    every write in `web_index` — deletes included — at the CRM corpus."""
    monkeypatch.setattr(settings, "lancedb_dir", str(tmp_path / "crm"))
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path / "crm" / "web"))

    async def explode(*_a, **_k):  # pragma: no cover - must never be called
        raise AssertionError("refused runs must not index anything")

    monkeypatch.setattr(web_index, "index_pending", explode)

    rc, _out, err = _run([], capsys)

    assert rc == ri.EXIT_REFUSED
    assert "REFUSED" in err and "SALESFORCE" in err

    # The parent direction too: a web directory that swallows the CRM corpus.
    monkeypatch.setattr(settings, "lancedb_web_dir", str(tmp_path))
    rc, _out, err = _run(["--dry-run"], capsys)
    assert rc == ri.EXIT_REFUSED and "REFUSED" in err


def test_the_cli_refuses_when_web_memory_is_disabled(monkeypatch, capsys):
    """Without this, `index_pending`'s early return would be reported as a
    stall — a fault — for a deliberate configuration."""
    monkeypatch.setattr(settings, "web_memory_enabled", False)

    rc, _out, err = _run([], capsys)

    assert rc == ri.EXIT_REFUSED
    assert "WEB_MEMORY_ENABLED" in err


def test_the_cli_prints_resumable_progress_and_peak_rss(stub_index_pending, no_sleep, capsys):
    for i in range(6):
        _page(f"https://example.com/cli{i}")

    rc, out, _err = _run(
        ["--batch-pages", "2", "--max-chunks", "7", "--pause-s", "0"], capsys
    )

    assert rc == 0
    assert "batch   1 chunks+6" in out
    assert "left=" in out and "elapsed=" in out
    assert "progress: done=4 remaining=2 of 6 page(s) at the start" in out
    assert "re-run the same command to continue" in out
    assert "peak RSS" in out and "VmHWM" in out

    summary = _summary(out)
    assert summary["stop_reason"] == "chunk-budget"
    assert summary["remaining"] == 2 and summary["done"] == 4
    assert summary["exit_code"] == 0
    assert summary["chunker_version"] == int(web_index.CHUNKER_VERSION)
    assert isinstance(summary["peak_rss_kib"], int) and summary["peak_rss_kib"] > 0


def test_the_cli_exits_one_when_it_stalls(stub_index_pending, no_sleep, capsys):
    for i in range(4):
        _page(f"https://example.com/stall{i}")
    stub_index_pending.stamp = False

    rc, out, err = _run(["--batch-pages", "2", "--pause-s", "0"], capsys)

    assert rc == ri.EXIT_STALLED
    assert "no progress" in out
    assert "STALLED" in err
    assert _summary(out)["exit_code"] == ri.EXIT_STALLED


def test_the_cli_exits_zero_on_an_empty_queue(stub_index_pending, no_sleep, capsys):
    rc, out, _err = _run([], capsys)

    assert rc == 0
    assert stub_index_pending.limits == [], "an empty queue costs no batch"
    assert "the queue is empty" in out
    assert _summary(out)["backlog_start"] == 0


def test_a_bad_limit_is_refused_before_anything_runs(capsys):
    for flag in ("--batch-pages", "--max-chunks"):
        with pytest.raises(SystemExit) as exc:
            ri.main([flag, "0"])
        assert exc.value.code == ri.EXIT_REFUSED
    with pytest.raises(SystemExit) as exc:
        ri.main(["--pause-s", "-1"])
    assert exc.value.code == ri.EXIT_REFUSED


# ---------------------------------------------------------------------------
# 5. it drives the real code and keeps no copy of it
# ---------------------------------------------------------------------------


def test_it_calls_the_real_index_pending_and_reimplements_nothing():
    """A second chunker or a second embedding call would drift from the worker
    silently — the index would hold two shapes and nothing would say so."""
    import ast

    tree = ast.parse(open(ri.__file__, encoding="utf-8").read())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                called.add(func.attr)
            elif isinstance(func, ast.Name):
                called.add(func.id)

    assert "index_pending" in called
    assert ri.web_index.index_pending is web_index.index_pending
    for forbidden in ("chunk_page", "embed_texts", "_embed_batched", "delete_pages"):
        assert forbidden not in called, f"{forbidden} belongs to web_index, not here"


def test_the_tool_writes_no_file_of_its_own():
    """The claim "the queue IS the checkpoint" in one assertion: every `open()`
    in the tool is the read of /proc/self/status that reports peak RSS."""
    import ast

    tree = ast.parse(open(ri.__file__, encoding="utf-8").read())
    opens = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
    ]
    assert len(opens) == 1
    only = opens[0]
    assert isinstance(only.args[0], ast.Constant)
    assert only.args[0].value == "/proc/self/status"
    assert not [k for k in only.keywords if k.arg == "mode"]


def test_peak_rss_is_read_from_vmhwm():
    peak = ri.peak_rss_kib()
    current = ri.current_rss_kib()
    assert isinstance(peak, int) and peak > 0
    assert isinstance(current, int) and current > 0
    assert peak >= current, "VmHWM is a high-water mark"
