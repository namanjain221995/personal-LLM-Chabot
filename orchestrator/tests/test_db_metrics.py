"""Database timing from the orchestrator's side (app/db_metrics.py, 2026-09-14).

Before this module the orchestrator exported no DB timing at all: pool
checkout wait, thread-pool queueing and query time could not be told apart in
production, so none of the owner's millisecond targets could be read off
Prometheus. These tests pin the series, the closed `site` vocabulary, and the
seam's invariants (never raises, never leaks a site onto a pooled connection,
never records the pool's own liveness check).
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pathlib
import re

import anyio
import psycopg
import pytest

from app import db, db_metrics, metrics

APP = pathlib.Path(db.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _clean_registry():
    metrics.reset()
    yield
    metrics.reset()


def _series(name: str) -> dict:
    return dict(metrics._hists.get(name, {}))


def _count(name: str, **labels: str) -> int:
    key = tuple(sorted(labels.items()))
    entry = metrics._hists.get(name, {}).get(key)
    return 0 if entry is None else entry[2]


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_every_named_call_site_exists_and_opens_a_transaction():
    """A renamed accessor must not silently fall back to its module's site."""
    stale = []
    for (module, function), _site in db_metrics._SITE_BY_FUNCTION.items():
        mod = importlib.import_module(module)
        fn = getattr(mod, function, None)
        if fn is None:
            stale.append(f"{module}.{function} (missing)")
            continue
        source = inspect.getsource(fn)
        if "connection()" not in source and "_on(con)" not in source:
            stale.append(f"{module}.{function} (no connection())")
    assert stale == []


@pytest.mark.parametrize(
    "module, function, site",
    [
        ("app.db", "list_messages", "history"),
        ("app.db", "add_message", "message_write"),
        ("app.db", "create_chat_request", "chat_request"),
        ("app.db", "recall_conversations", "recall"),
        ("app.db", "list_user_facts", "facts"),
        ("app.web_memory", "_lexical_candidates", "web_lexical"),
        ("app.web_memory", "_page_meta", "web_meta"),
        ("app.db", "api_key_by_public_id", "api_key"),
        ("app.db", "touch_api_key", "api_key"),
        ("app.apiplatform.quotas", "reserve", "api_usage"),
        ("app.publicapi.durable_store", "append", "durable_flush"),
        ("app.publicapi.durable_store", "due_for_resume", "durable_dispatch"),
        ("app.apifiles.queue", "claim_due_blobs", "files_claim"),
        ("app.db", "web_corpus_counts", "metrics_scrape"),
        ("app.health", "_read_work", "health"),
        ("app.authn.store", "get_session", "auth"),
        ("app.db", "share_for_conversation", "sharing"),
        ("app.db", "get_video_analysis", "video"),
        ("app.db", "something_nobody_wrote", "other"),
        ("tests.somewhere", "helper", "other"),
    ],
)
def test_hot_path_call_sites_have_their_own_site(module, function, site):
    assert db_metrics.site_for(module, function) == site


def test_every_site_the_resolver_can_return_is_in_the_closed_vocabulary():
    """Scan every function that opens `connection()` in app/ and resolve it."""
    produced = set()
    for path in APP.rglob("*.py"):
        source = path.read_text()
        if "connection()" not in source and "_on(con)" not in source:
            continue
        module = "app." + ".".join(path.relative_to(APP).with_suffix("").parts)
        module = re.sub(r"\.__init__$", "", module)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                produced.add(db_metrics.site_for(module, node.name))
    assert produced <= db_metrics.SITES
    # Low cardinality is the contract: two histograms of 17 series each.
    assert len(db_metrics.SITES) <= 45


def test_an_unknown_site_value_folds_to_other():
    metrics.observe(db_metrics.TRANSACTION, 0.001, site="SELECT * FROM users")
    assert list(_series(db_metrics.TRANSACTION)) == [(("site", "other"),)]


def test_db_histograms_have_edges_at_the_owner_targets():
    buckets = metrics._buckets_for(db_metrics.STATEMENT)
    for target in (0.002, 0.005, 0.02, 0.03, 0.05, 0.1):
        assert target in buckets
    assert metrics._buckets_for(db_metrics.POOL_WAIT) == buckets
    assert metrics._buckets_for(db_metrics.TRANSACTION) == buckets


# ---------------------------------------------------------------------------
# The seam, against the real test database
# ---------------------------------------------------------------------------


def test_an_accessor_records_pool_wait_transaction_and_statement_under_its_site():
    assert db.list_messages("no-such-conversation") == []
    assert _count(db_metrics.TRANSACTION, site="history") == 1
    assert _count(db_metrics.STATEMENT, site="history") == 1
    assert _count(db_metrics.POOL_WAIT) == 1


def test_read_connection_is_timed_under_its_callers_site():
    """The single-statement autocommit checkout (pg-chat-api, 2026-09-14) is
    the seam for most chat-turn reads; it must record like `connection()`
    and resolve the caller, not `read_connection`, as the site."""
    assert db.conversation_owner("no-such-conversation") is None
    assert _count(db_metrics.TRANSACTION, site="conversation") == 1
    assert _count(db_metrics.STATEMENT, site="conversation") == 1
    assert _count(db_metrics.POOL_WAIT) == 1
    assert _count(db_metrics.TRANSACTION, site="other") == 0


def test_writers_behind_on_keep_their_site_and_the_trace_batch_has_one():
    db.finish_query_trace("no-such-trace", "completed", 1)
    assert _count(db_metrics.TRANSACTION, site="query_trace") == 1
    db.write_query_trace_batch(
        [(db.finish_query_trace, ("no-such-trace", "completed", 1), {})]
    )
    assert _count(db_metrics.TRANSACTION, site="query_trace") == 2
    assert _count(db_metrics.STATEMENT, site="query_trace") == 3  # LOCK + UPDATE, UPDATE
    assert _count(db_metrics.TRANSACTION, site="other") == 0


def test_statements_are_counted_one_per_round_trip():
    with db.connection() as con:  # this test module: site "other"
        for _ in range(3):
            con.execute("SELECT 1").fetchone()
        con.cursor().executemany("SELECT %s", [(1,), (2,)])
    assert _count(db_metrics.STATEMENT, site="other") == 4
    assert _count(db_metrics.TRANSACTION, site="other") == 1


def test_the_site_is_unbound_before_the_connection_goes_back_to_the_pool():
    with db.connection() as con:
        assert con._techsara_site == "other"
        held = con
    assert held._techsara_site is None
    # Outside a checkout the cursor records nothing (the pool's liveness check
    # and reset run here).
    before = _count(db_metrics.STATEMENT, site="other")
    held.execute("SELECT 1")
    held.rollback()
    assert _count(db_metrics.STATEMENT, site="other") == before


def test_a_failing_transaction_is_counted_and_still_raises():
    with pytest.raises(psycopg.errors.UndefinedTable):
        with db.connection() as con:
            con.execute("SELECT * FROM table_that_does_not_exist")
    assert metrics._counters[db_metrics.TRANSACTION_ERRORS][(("site", "other"),)] == 1
    assert _count(db_metrics.TRANSACTION, site="other") == 1
    # and the pool is healthy afterwards
    with db.connection() as con:
        assert con.execute("SELECT 1 AS x").fetchone()["x"] == 1


def test_a_nested_accessor_gets_its_own_site():
    with db.connection():
        db.list_messages("nested")
    assert _count(db_metrics.TRANSACTION, site="history") == 1
    assert _count(db_metrics.TRANSACTION, site="other") == 1


def test_rows_still_come_back_as_dicts_and_server_cursors_still_work():
    with db.connection() as con:
        assert isinstance(con.cursor(), db_metrics.TimedCursor)
        assert con.execute("SELECT 2 AS two").fetchone() == {"two": 2}
        with con.cursor(name="obs_named") as cur:
            cur.execute("SELECT generate_series(1, 3) AS n")
            assert [r["n"] for r in cur.fetchall()] == [1, 2, 3]


def test_a_metrics_failure_never_breaks_a_query(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(metrics, "observe", boom)
    monkeypatch.setattr(metrics, "observe_many", boom)
    monkeypatch.setattr(metrics, "inc", boom)
    monkeypatch.setattr(db_metrics, "_caller_site", boom)
    assert db.list_messages("still-works") == []
    with pytest.raises(psycopg.errors.UndefinedTable):
        with db.connection() as con:
            con.execute("SELECT * FROM table_that_does_not_exist")

    async def threaded():
        return await db.run_in_thread(db.list_messages, "still-works")

    assert anyio.run(threaded) == []


def test_observe_many_matches_observe_exactly():
    for seconds in (0.0, 0.0005, 0.00051, 0.002, 0.0299, 0.03, 7.0, 11.0):
        metrics.observe(db_metrics.STATEMENT, seconds, site="history")
    via_observe = metrics._hists[db_metrics.STATEMENT][(("site", "history"),)]
    metrics.reset()
    metrics.observe_many(
        (db_metrics.STATEMENT, (("site", "history"),), s)
        for s in (0.0, 0.0005, 0.00051, 0.002, 0.0299, 0.03, 7.0, 11.0)
    )
    assert metrics._hists[db_metrics.STATEMENT][(("site", "history"),)] == via_observe


def test_run_in_thread_records_the_thread_wait():
    async def main():
        return await db.run_in_thread(db.list_messages, "threaded")

    assert anyio.run(main) == []
    assert _count(db_metrics.THREAD_WAIT) == 1
    assert _count(db_metrics.TRANSACTION, site="history") == 1


def test_pool_gauges_are_rendered_from_the_open_pool_only():
    db.list_messages("warm")  # the pool is open
    text = metrics.render()
    assert re.search(r"^techsara_db_pool_max \d+$", text, re.M)
    assert re.search(r"^techsara_db_pool_size \d+$", text, re.M)
    assert re.search(r"^techsara_db_pool_requests_waiting 0$", text, re.M)
    assert "# TYPE techsara_db_statement_seconds histogram" in text
    assert 'techsara_db_statement_seconds_bucket{site="history",le="0.005"}' in text


def test_pool_gauges_never_open_a_pool(monkeypatch):
    monkeypatch.setattr(db, "_pool", None)
    opened = []
    monkeypatch.setattr(db, "pool", lambda: opened.append(1))
    metrics.render()
    assert opened == []
    assert "techsara_db_pool_size" not in metrics.render()


def test_a_failing_collector_does_not_break_the_scrape(monkeypatch):
    def broken():
        raise RuntimeError("no")

    monkeypatch.setattr(metrics, "_collectors", [broken])
    metrics.inc("techsara_freshness_auto_search_total", result="ok")
    assert "techsara_freshness_auto_search_total" in metrics.render()


def test_a_checkout_that_times_out_is_counted_with_its_wait(monkeypatch):
    from psycopg_pool import PoolTimeout

    class _Refusing:
        def connection(self):
            class _CM:
                def __enter__(self):
                    raise PoolTimeout("couldn't get a connection after 0.01 sec")

                def __exit__(self, *exc):
                    return False

            return _CM()

    monkeypatch.setattr(db, "pool", lambda: _Refusing())
    with pytest.raises(PoolTimeout):
        db.list_messages("never")
    assert metrics._counters[db_metrics.CHECKOUT_ERRORS][(("site", "history"),)] == 1
    assert _count(db_metrics.POOL_WAIT) == 1
    assert _count(db_metrics.TRANSACTION, site="history") == 0


def test_the_files_store_indirection_is_attributed_to_files():
    assert db_metrics.site_for("app.apifiles.service", "_query") == "files"


def test_a_long_statement_loop_is_recorded_in_bounded_chunks():
    """A per-row loop inside one transaction must not grow the pending list
    without bound nor record it all under one registry lock at commit."""
    n = db_metrics._PENDING_FLUSH_AT * 3 + 7
    with db.connection() as con:  # this test module: site "other"
        cur = con.cursor()
        for _ in range(n):
            cur.execute("SELECT 1")
            assert len(con._techsara_pending) < db_metrics._PENDING_FLUSH_AT
        assert _count(db_metrics.STATEMENT, site="other") == db_metrics._PENDING_FLUSH_AT * 3
    assert _count(db_metrics.STATEMENT, site="other") == n
    assert _count(db_metrics.TRANSACTION, site="other") == 1
