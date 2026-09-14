"""The chat-turn and /v1 database fast path (dbperf2 track pg-chat-api, 2026-09-14).

Each test pins one change that removed round trips or synchronous commits
without touching durability:

* pooled sessions opt out of the server's idle_session_timeout (a killed idle
  connection cost psycopg_pool's 1 s backoff) and of JIT;
* the pool's liveness check no longer sends an empty query on every checkout,
  and still replaces a connection whose backend was terminated;
* `read_connection()` runs a single statement in autocommit and never leaks
  autocommit back into the pool;
* a /chat acceptance is ONE transaction: the row written `running` and the
  superseded parked rows cancelled together;
* a queued trace (root, checkpoints, close) commits in one or two
  transactions instead of one per row, and still lands row by row when a
  batch fails;
* `touch_api_key` writes at most once a minute per key, but always records a
  new address;
* keyword recall filters to the user before it reads message text, caps its
  keywords, and returns exactly what it returned before.
"""
from __future__ import annotations

import asyncio
import contextlib
import time

import psycopg
import pytest

from app import db
from app import main as app_main


# ------------------------------------------------------------------ pool --


def test_pooled_sessions_opt_out_of_the_idle_session_timeout_and_jit():
    with db.connection() as con:
        idle = con.execute("SHOW idle_session_timeout").fetchone()["idle_session_timeout"]
        jit = con.execute("SHOW jit").fetchone()["jit"]
    assert idle == "0"
    assert jit == "off"


def test_a_warm_checkout_does_not_send_the_empty_check_query(monkeypatch):
    """psycopg_pool's check_connection is `conn.execute("")`: one round trip
    (and one server-side commit) per checkout. Counted at the driver."""
    empty = []
    real_execute = psycopg.Connection.execute

    def spying(self, query, params=None, *args, **kwargs):
        if query == "":
            empty.append(1)
        return real_execute(self, query, params, *args, **kwargs)

    for _ in range(3):  # warm: the connection was just returned
        with db.connection() as con:
            con.execute("SELECT 1").fetchone()
    monkeypatch.setattr(psycopg.Connection, "execute", spying)
    for _ in range(20):
        with db.connection() as con:
            con.execute("SELECT 1").fetchone()
    monkeypatch.setattr(psycopg.Connection, "execute", real_execute)
    assert empty == []


def test_an_idle_connection_still_gets_the_full_check(monkeypatch):
    calls = []
    real = db.ConnectionPool.check_connection

    def counting(conn):
        calls.append(1)
        return real(conn)

    monkeypatch.setattr(db.ConnectionPool, "check_connection", staticmethod(counting))
    monkeypatch.setattr(db, "_POOL_FULL_CHECK_IDLE_S", 0.0)
    with db.connection() as con:
        con.execute("SELECT 1").fetchone()
    time.sleep(0.01)
    with db.connection() as con:
        con.execute("SELECT 1").fetchone()
    assert calls, "a connection idle past the threshold is checked with a round trip"


def test_a_terminated_backend_is_replaced_on_the_next_checkout():
    """What the liveness check exists for (`docker compose restart postgres`):
    the server ends the session under an idle pooled connection, and the next
    request still gets a working connection instead of an error."""
    pool = db.pool()
    with db.connection() as con:
        pids = {con.info.backend_pid}
    # Every idle connection in the pool, so whichever is handed out next is dead.
    with contextlib.ExitStack() as stack:
        held = [stack.enter_context(db.connection()) for _ in range(max(1, pool.get_stats().get("pool_size", 1)))]
        pids |= {c.info.backend_pid for c in held}
    killer = psycopg.connect(db.dsn(), autocommit=True)
    try:
        killer.execute("SELECT pg_terminate_backend(pid) FROM unnest(%s::int[]) AS pid", (sorted(pids),))
    finally:
        killer.close()
    time.sleep(0.2)  # the FATAL and the EOF reach the sockets
    with db.connection() as con:
        assert con.execute("SELECT 1 AS one").fetchone()["one"] == 1
        assert con.info.backend_pid not in pids


def test_a_pool_full_of_dead_connections_recovers_without_the_backoff_ladder():
    """`docker compose restart postgres` kills EVERY pooled connection at
    once. psycopg_pool sleeps 1 s, 2 s, 4 s ... after each failed check inside
    one getconn, so six dead idle connections used to turn the next request
    into a PoolTimeout after 10 s — on the pristine tree too (verifier,
    2026-09-14; the terminated-backend test above failed exactly so once
    earlier tests had grown the pool). The first failed check drains the
    pool, so the next request pays at most one backoff."""
    if not hasattr(db.ConnectionPool, "drain"):
        pytest.skip("psycopg_pool < 3.3 has no drain(); the pool's own backoff applies")
    db.pool()
    with contextlib.ExitStack() as stack:
        held = [stack.enter_context(db.connection()) for _ in range(8)]
        for c in held:
            c.execute("SELECT 1").fetchone()
        pids = sorted({c.info.backend_pid for c in held})
    killer = psycopg.connect(db.dsn(), autocommit=True)
    try:
        killer.execute("SELECT pg_terminate_backend(pid) FROM unnest(%s::int[]) AS pid", (pids,))
    finally:
        killer.close()
    time.sleep(0.2)
    t0 = time.monotonic()
    with db.connection() as con:
        assert con.execute("SELECT 1 AS one").fetchone()["one"] == 1
        assert con.info.backend_pid not in pids
    assert time.monotonic() - t0 < 3.0


def test_read_connection_is_autocommit_inside_and_never_leaks_it_to_the_pool():
    with db.read_connection() as con:
        assert con.autocommit is True
        con.execute("SELECT 1").fetchone()
    with pytest.raises(RuntimeError):
        with db.read_connection() as con:
            raise RuntimeError("inside the block")
    for _ in range(5):
        with db.connection() as con:
            assert con.autocommit is False


def test_hot_single_statement_reads_run_without_begin_and_commit(monkeypatch):
    """conversation_owner is on every /chat; it used to be BEGIN, SELECT,
    COMMIT on a pooled connection."""
    uid = db.create_user("reader", "hash")
    db.create_conversation(uid, "c-read", "t")
    seen = []
    real = db.read_connection

    @contextlib.contextmanager
    def spy():
        with real() as con:
            seen.append(con.autocommit)
            yield con

    monkeypatch.setattr(db, "read_connection", spy)
    assert db.conversation_owner("c-read") == uid
    assert seen == [True]


# --------------------------------------------------------- chat requests --


def test_acceptance_writes_running_and_supersedes_parked_rows_in_one_transaction(monkeypatch):
    uid = db.create_user("sender", "hash")
    db.create_conversation(uid, "c-acc", "t")
    for intent in ("parked-1", "parked-2", "held"):
        db.create_chat_request(intent, uid, "c-acc", "gen-" + intent, {"message": "old"})
        db.set_chat_request_status(intent, "queued")
    checkouts = []
    real = db.connection

    @contextlib.contextmanager
    def counting():
        checkouts.append(1)
        with real() as con:
            yield con

    monkeypatch.setattr(db, "connection", counting)
    row = db.create_chat_request(
        "new-send", uid, "c-acc", "gen-new", {"message": "new"},
        status="running", supersede_parked_except=["new-send", "held"],
    )
    monkeypatch.setattr(db, "connection", real)
    assert len(checkouts) == 1
    assert row["status"] == "running" and row["superseded"] == 2
    assert db.get_chat_request("parked-1")["status"] == "cancelled"
    assert db.get_chat_request("parked-1")["error"] == "replaced by a newer message"
    assert db.get_chat_request("parked-2")["status"] == "cancelled"
    assert db.get_chat_request("held")["status"] == "queued"


def test_a_known_intent_supersedes_nothing_and_a_new_row_is_only_accepted_or_running():
    uid = db.create_user("sender2", "hash")
    db.create_conversation(uid, "c-known", "t")
    db.create_chat_request("parked", uid, "c-known", "g0", {"message": "old"})
    db.set_chat_request_status("parked", "queued")
    assert db.create_chat_request("dup", uid, "c-known", "g1", {}) is not None
    again = db.create_chat_request("dup", uid, "c-known", "g2", {}, status="running", supersede_parked_except=["dup"])
    assert again is None
    assert db.get_chat_request("parked")["status"] == "queued"
    assert "superseded" not in db.create_chat_request("plain", uid, "c-known", "g3", {})
    with pytest.raises(ValueError):
        db.create_chat_request("bad", uid, "c-known", "g4", {}, status="completed")


# ---------------------------------------------------------------- traces --


def _flow(recorder, conversation_id, user_id):
    async def go():
        await recorder.start(
            conversation_id=conversation_id, user_id=user_id, workspace_id="ws-1",
            question="q", requested_mode="assistant",
        )
        for stage in ("REQUEST_RECEIVED", "MODE_RESOLVED", "CONTEXT_ASSEMBLED", "RETRIEVAL", "RERANK", "GENERATION"):
            await recorder.event(stage, component="t", details={"n": 1}, duration_ms=3)
            await asyncio.sleep(0.002)
        await recorder.finish("ok", route="chat", resolved_mode="assistant")
        await recorder.flush()

    asyncio.run(go())


def test_a_queued_trace_commits_in_at_most_two_transactions(monkeypatch):
    uid = db.create_user("tracer2", "hash")
    db.create_conversation(uid, "c-trace", "t")
    checkouts = []
    real = db.connection

    @contextlib.contextmanager
    def counting():
        checkouts.append(1)
        with real() as con:
            yield con

    monkeypatch.setattr(db, "connection", counting)
    recorder = app_main._QueuedTraceRecorder("gen-trace-batch", versions={"application": "t"})
    _flow(recorder, "c-trace", uid)
    monkeypatch.setattr(db, "connection", real)
    assert 1 <= len(checkouts) <= 2, f"{len(checkouts)} transactions for one trace"
    trace = db.get_query_trace("gen-trace-batch", uid)
    assert trace["final_status"] == "ok" and trace["selected_route"] == "chat"
    assert [e["sequence_number"] for e in trace["events"]] == [1, 2, 3, 4, 5, 6]
    assert [e["stage"] for e in trace["events"]][:2] == ["REQUEST_RECEIVED", "MODE_RESOLVED"]


def test_a_failed_trace_batch_is_retried_row_by_row(monkeypatch):
    uid = db.create_user("tracer3", "hash")
    db.create_conversation(uid, "c-trace-3", "t")

    def broken(_writes):
        raise psycopg.OperationalError("batch lost")

    monkeypatch.setattr(db, "write_query_trace_batch", broken)
    recorder = app_main._QueuedTraceRecorder("gen-trace-retry", versions={"application": "t"})
    _flow(recorder, "c-trace-3", uid)
    trace = db.get_query_trace("gen-trace-retry", uid)
    assert trace is not None and trace["final_status"] == "ok"
    assert len(trace["events"]) == 6


def test_the_trace_batch_refuses_anything_but_trace_writers():
    with pytest.raises(ValueError):
        db.write_query_trace_batch([(db.delete_conversation, (1, "c"), {})])


# ---------------------------------------------------------- api key touch --


@pytest.fixture()
def api_key():
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES ('ws-touch', 'Touch')")
    owner = int(db.create_user("touch-owner", "hash"))
    project = db.create_api_project("ws-touch", "p", "live", created_by=owner)
    account = db.create_service_account(project["id"], "ws-touch", "svc")
    return db.create_api_key(
        project["id"], "ws-touch", "k", "pubtouch00000001", "digest", "ab12",
        service_account_id=account["id"],
    )


def _last_used(key):
    row = db.api_key_by_public_id(key["public_id"])
    return row["last_used_at"], row["last_used_ip"], row["service_account"]["last_used_at"]


def test_touch_api_key_writes_once_a_minute_per_key(api_key, monkeypatch):
    db.touch_api_key(api_key["id"], "203.0.113.7")
    first = _last_used(api_key)
    assert first[0] is not None and first[1] == "203.0.113.7" and first[2] is not None
    writes = []
    real = db.connection

    @contextlib.contextmanager
    def counting():
        writes.append(1)
        with real() as con:
            yield con

    monkeypatch.setattr(db, "connection", counting)
    for _ in range(10):
        db.touch_api_key(api_key["id"], "203.0.113.7")
        db.touch_api_key(api_key["id"])  # no address never erases one, never writes
    monkeypatch.setattr(db, "connection", real)
    assert writes == []
    assert _last_used(api_key) == first


def test_touch_api_key_records_a_new_address_at_once(api_key):
    db.touch_api_key(api_key["id"], "203.0.113.7")
    db.touch_api_key(api_key["id"], "198.51.100.23")
    assert _last_used(api_key)[1] == "198.51.100.23"


def test_touch_api_key_throttles_across_processes_through_the_row(api_key):
    """Another process's write inside the interval is honoured by the WHERE
    clause even when this process has never touched the key."""
    db.touch_api_key(api_key["id"], "203.0.113.7")
    before = _last_used(api_key)
    db._api_key_touched.clear()  # a second process: no local memory
    db.touch_api_key(api_key["id"], "203.0.113.7")
    assert _last_used(api_key) == before


def test_touch_api_key_writes_again_after_the_interval(api_key, monkeypatch):
    db.touch_api_key(api_key["id"], "203.0.113.7")
    before = _last_used(api_key)[0]
    monkeypatch.setattr(db, "API_KEY_TOUCH_INTERVAL_S", 0.0)
    time.sleep(0.01)
    db.touch_api_key(api_key["id"], "203.0.113.7")
    assert _last_used(api_key)[0] > before


# ---------------------------------------------------------------- recall --


@pytest.fixture()
def recall_world():
    uid = db.create_user("alice-r", "hash")
    other = db.create_user("bob-r", "hash")
    db.create_conversation(uid, "r-cur", "current")
    db.create_conversation(uid, "r-1", "Zephyr planning")
    db.add_message(uid, "r-1", "user", "Zephyr LAUNCH is on September 30.")
    db.add_message(uid, "r-1", "assistant", "Noted: the zephyr launch, Sept 30, Budget 250000.")
    db.add_message(uid, "r-1", "user", "and the budget owner is Priya")
    db.create_conversation(uid, "r-2", "Bread")
    db.add_message(uid, "r-2", "user", "How do I bake sourdough? budget is small")
    db.create_conversation(other, "r-9", "Bob")
    for _ in range(30):
        db.add_message(other, "r-9", "user", "zephyr launch budget sourdough priya " * 20)
    return uid


def test_recall_ranks_and_picks_snippets_case_insensitively_for_this_user_only(recall_world):
    hits = db.recall_conversations(recall_world, ["zephyr", "launch", "budget"], "r-cur", 3)
    assert [h["id"] for h in hits] == ["r-1", "r-2"]
    # the message matching the most keywords, newest among ties
    assert hits[0]["snippet"].startswith("Noted: the zephyr launch")
    assert hits[0]["role"] == "assistant"
    assert "bob" not in " ".join(h["snippet"].lower() for h in hits)


def test_recall_snippet_matches_exactly_what_ilike_matches_for_non_ascii_keywords():
    """The snippet pass lowercases in PostgreSQL, never in Python: under the
    C collation lower() folds ASCII only, so a Python-lowered 'école' would
    miss the 'ÉCOLE' message that the ILIKE ranking pass matched, and the hit
    would come back without its snippet (verifier finding 2026-09-14)."""
    uid = db.create_user("unicode-r", "hash")
    db.create_conversation(uid, "u-cur", "current")
    db.create_conversation(uid, "u-1", "Visit")
    db.add_message(uid, "u-1", "user", "notes from the ÉCOLE visit and the CAFÉ")
    hits = db.recall_conversations(uid, ["ÉCOLE", "CAFÉ"], "u-cur", 3)
    assert [h["id"] for h in hits] == ["u-1"]
    assert hits[0]["snippet"].startswith("notes from the ÉCOLE visit")


def test_recall_caps_keywords_to_the_longest():
    kept = db._recall_keywords(["a1", "bbbb", "cc", "dddddd", "eee", "ffffffff", "gg", "hhhhh"])
    assert len(kept) == db.RECALL_MAX_KEYWORDS == 6
    # the longest six, ties broken by position, in the order given
    assert kept == ["a1", "bbbb", "dddddd", "eee", "ffffffff", "hhhhh"]
    assert db._recall_keywords(["x", "x", "", "y"]) == ["x", "y"]


def test_recall_filters_to_the_user_before_reading_message_text(recall_world):
    """The plan must carry an index condition that limits messages to this
    user's conversations — the trigram index alone has no user dimension."""
    captured = []
    real = db.read_connection

    @contextlib.contextmanager
    def capture():
        with real() as con:
            original = con.execute

            def execute(sql, params=None, **kw):
                captured.append((sql, params))
                return original(sql, params, **kw)

            con.execute = execute  # type: ignore[method-assign]
            try:
                yield con
            finally:
                del con.execute

    import unittest.mock as mock

    with mock.patch.object(db, "read_connection", capture):
        db.recall_conversations(recall_world, ["zephyr"], "r-cur", 3)
    sql, params = captured[-1]
    with db.connection() as con:
        con.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(r["QUERY PLAN"] for r in con.execute("EXPLAIN " + sql, params).fetchall())
    assert "idx_messages_conversation" in plan
    assert "conversation_id = ANY" in plan, plan


# ------------------------------------------------ embedding backfill read --


def _capture_sql(monkeypatch):
    captured = []
    real = db.read_connection

    @contextlib.contextmanager
    def capture():
        with real() as con:
            original = con.execute

            def execute(sql, params=None, **kw):
                captured.append((sql, params))
                return original(sql, params, **kw)

            con.execute = execute  # type: ignore[method-assign]
            try:
                yield con
            finally:
                del con.execute

    monkeypatch.setattr(db, "read_connection", capture)
    return captured


def test_missing_embeddings_are_the_newest_long_enough_rows_of_this_user_only():
    uid = db.create_user("backfill", "hash")
    other = db.create_user("backfill-other", "hash")
    db.create_conversation(uid, "b-1", "t")
    db.create_conversation(uid, "b-2", "t")
    db.create_conversation(other, "b-9", "t")
    ids = []
    for i in range(6):
        ids.append(db.add_message(uid, "b-1" if i % 2 else "b-2", "user", f"message number {i} long enough")["id"])
    db.add_message(uid, "b-1", "user", "short")  # under min_chars
    db.add_message(other, "b-9", "user", "someone else's long enough message")
    db.store_message_embeddings(uid, "m1", 2, [{"message_id": ids[5], "conversation_id": "b-1", "embedding": b"\0" * 8}])
    rows = db.messages_missing_embeddings(uid, "m1", limit=3)
    assert [r["id"] for r in rows] == [ids[4], ids[3], ids[2]]
    assert [r["id"] for r in db.messages_missing_embeddings(uid, "m1")] == [ids[4], ids[3], ids[2], ids[1], ids[0]]


def test_missing_embeddings_count_characters_not_bytes_and_skip_detoasting_long_rows(monkeypatch):
    """`length` (characters) is what the filter means; `octet_length` only
    decides the rows where the byte count settles it without detoasting."""
    uid = db.create_user("backfill-bytes", "hash")
    db.create_conversation(uid, "b-bytes", "t")
    four_byte_short = db.add_message(uid, "b-bytes", "user", "\U0001F600" * 5)["id"]  # 5 chars, 20 bytes
    three_byte_ok = db.add_message(uid, "b-bytes", "user", "\u20ac" * 15)["id"]  # 15 chars, 45 bytes
    ascii_short = db.add_message(uid, "b-bytes", "user", "fourteen chars")["id"]  # 14
    ascii_ok = db.add_message(uid, "b-bytes", "user", "fifteen chars!!")["id"]  # 15
    captured = _capture_sql(monkeypatch)
    got = [r["id"] for r in db.messages_missing_embeddings(uid, "m1")]
    assert got == [ascii_ok, three_byte_ok]
    assert four_byte_short not in got and ascii_short not in got
    sql = " ".join(captured[-1][0].split())
    assert "octet_length(m.content) >= %s AND (octet_length(m.content) >= 4 * %s OR length(m.content) >= %s)" in sql
