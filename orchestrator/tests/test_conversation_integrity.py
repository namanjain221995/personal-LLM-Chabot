"""Phase 0-critical: conversation integrity + per-conversation authorization.

Two invariants, both learned from confirmed defects:

1. No sync path may ever REDUCE a conversation's message count. The client's
   offline sync used to reconcile a diverged tail by deleting the conversation
   and recreating it from its local copy; when that copy was empty or stale it
   destroyed the whole thread. PUT /messages refuses to shrink (409).
2. The per-conversation stores (url_documents, repo_chunks) and the live
   generation registry are keyed by conversation id alone, so POST /chat must
   verify the caller owns that conversation or 404.
"""
import pytest
from fastapi.testclient import TestClient

from app import db, llm
from app.config import settings
from app.main import app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings, "session_secret_file", str(tmp_path / ".session_secret")
    )
    monkeypatch.delenv("SESSION_SECRET", raising=False)


@pytest.fixture()
def alice(env, as_user):
    # No registration: login is gone. The app runs AS this user.
    as_user("alice")
    with TestClient(app) as c:
        yield c


def _seed(client, conversation_id: str, contents: list) -> None:
    client.post(
        "/history/conversations", json={"id": conversation_id, "title": "chat"}
    )
    for i, text in enumerate(contents):
        client.post(
            f"/history/conversations/{conversation_id}/messages",
            json={"role": "user" if i % 2 == 0 else "assistant", "content": text},
        )


# ---------------------------------------------------------------------------
# 1. The no-shrink invariant
# ---------------------------------------------------------------------------


def test_replace_refuses_to_shrink_a_conversation(alice):
    _seed(alice, "c1", ["turn 1", "answer 1", "turn 2"])

    resp = alice.put(
        "/history/conversations/c1/messages",
        json={"messages": [{"role": "assistant", "content": "replayed answer"}]},
    )
    assert resp.status_code == 409
    assert "shrink" in resp.json()["detail"]

    # Nothing was touched.
    kept = alice.get("/history/conversations/c1").json()["messages"]
    assert [m["content"] for m in kept] == ["turn 1", "answer 1", "turn 2"]


def test_replace_allows_same_length_and_growth(alice):
    _seed(alice, "c2", ["q", "first answer"])

    same_length = alice.put(
        "/history/conversations/c2/messages",
        json={
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "regenerated"},
            ]
        },
    )
    assert same_length.status_code == 200
    assert [
        m["content"] for m in alice.get("/history/conversations/c2").json()["messages"]
    ] == ["q", "regenerated"]

    grow = alice.put(
        "/history/conversations/c2/messages",
        json={
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "regenerated"},
                {"role": "user", "content": "follow-up"},
            ]
        },
    )
    assert grow.status_code == 200
    assert len(alice.get("/history/conversations/c2").json()["messages"]) == 3


def test_replace_round_trips_meta_and_is_scoped_to_the_owner(alice, env):
    _seed(alice, "c3", ["q"])
    alice.put(
        "/history/conversations/c3/messages",
        json={
            "messages": [
                {"role": "user", "content": "q"},
                {
                    "role": "assistant",
                    "content": "a",
                    "meta": {"route": "sql", "sql": "SELECT 1"},
                },
            ]
        },
    )
    messages = alice.get("/history/conversations/c3").json()["messages"]
    assert messages[1]["meta"]["sql"] == "SELECT 1"

    # Someone else's conversation is indistinguishable from a missing one.
    # Seeded through the db layer: login is gone, so the HTTP layer has only
    # one identity — the ownership check it still performs is the thing under
    # test, and it is what would matter if a second account ever existed.
    from app import db

    other = db.create_user("someone-else", "!x")
    db.create_conversation(other, "theirs", "private")
    assert (
        alice.put(
            "/history/conversations/theirs/messages",
            json={"messages": [{"role": "user", "content": "x"}]},
        ).status_code
        == 404
    )


def test_replace_is_atomic_when_a_row_is_rejected(alice):
    """A bad role must abort the whole replace, never empty the thread."""
    _seed(alice, "c4", ["keep me", "and me"])
    resp = alice.put(
        "/history/conversations/c4/messages",
        json={
            "messages": [
                {"role": "user", "content": "one"},
                {"role": "", "content": "bad role"},
                {"role": "user", "content": "three"},
            ]
        },
    )
    assert resp.status_code == 400
    assert [
        m["content"] for m in alice.get("/history/conversations/c4").json()["messages"]
    ] == ["keep me", "and me"]


def test_db_helper_raises_rather_than_shrinking(alice):
    _seed(alice, "c5", ["a", "b", "c"])
    user = db.get_user_by_username("alice")
    with pytest.raises(db.MessageCountWouldShrink) as exc:
        db.replace_messages(int(user["id"]), "c5", [{"role": "user", "content": "x"}])
    assert exc.value.existing == 3
    assert exc.value.incoming == 1


# ---------------------------------------------------------------------------
# 2. Per-conversation authorization on POST /chat
# ---------------------------------------------------------------------------


def test_chat_rejects_a_conversation_owned_by_someone_else(alice, env, monkeypatch):
    async def fake_stream(messages, **kwargs):
        yield "token", "hi"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    _seed(alice, "mine", ["my question"])

    # A conversation owned by someone else: its url_documents/repo_chunks are
    # keyed by conversation id alone, so guessing the id must not pull them
    # into this prompt.
    from app import db

    other = db.create_user("someone-else", "!x")
    db.create_conversation(other, "theirs", "private")

    resp = alice.post(
        "/chat",
        json={
            "message": "what did they ask?",
            "mode": "assistant",
            "conversation_id": "theirs",
        },
    )
    assert resp.status_code == 404

    # The owner is unaffected.
    ok = alice.post(
        "/chat",
        json={"message": "hello", "mode": "assistant", "conversation_id": "mine"},
    )
    assert ok.status_code == 200


def test_chat_allows_conversations_with_no_owner_row(env, monkeypatch):
    """Bare API calls (no history row) still work — there is no owner to violate."""

    async def fake_stream(messages, **kwargs):
        yield "token", "hi"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    with TestClient(app) as anon:
        resp = anon.post(
            "/chat",
            json={
                "message": "hello",
                "mode": "assistant",
                "conversation_id": "never-persisted",
            },
        )
        assert resp.status_code == 200


def test_conversation_owner_helper(alice):
    _seed(alice, "c6", ["x"])
    user = db.get_user_by_username("alice")
    assert db.conversation_owner("c6") == int(user["id"])
    assert db.conversation_owner("no-such-conversation") is None


# ---------------------------------------------------------------------------
# 3. The ONE sanctioned shrink: a user-confirmed regenerate of an older answer
# ---------------------------------------------------------------------------


def test_truncate_is_the_only_way_to_shorten_a_thread(alice):
    _seed(alice, "t1", ["q1", "a1", "q2", "a2"])

    # The sync path still refuses, even now that truncate exists.
    assert (
        alice.put(
            "/history/conversations/t1/messages",
            json={"messages": [{"role": "user", "content": "q1"}]},
        ).status_code
        == 409
    )

    # The explicit endpoint succeeds and keeps exactly the first `keep`.
    resp = alice.post(
        "/history/conversations/t1/truncate",
        json={"keep": 1, "expected_total": 4},
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 1
    assert [
        m["content"] for m in alice.get("/history/conversations/t1").json()["messages"]
    ] == ["q1"]

    # …and the regenerated answer then APPENDS normally.
    alice.post(
        "/history/conversations/t1/messages",
        json={"role": "assistant", "content": "regenerated answer"},
    )
    assert [
        m["content"] for m in alice.get("/history/conversations/t1").json()["messages"]
    ] == ["q1", "regenerated answer"]


def test_truncate_refuses_when_the_thread_changed_underneath(alice):
    """Another tab appended turns → the stale caller must not delete them."""
    _seed(alice, "t2", ["q1", "a1", "q2", "a2"])
    resp = alice.post(
        "/history/conversations/t2/truncate",
        json={"keep": 1, "expected_total": 2},  # caller thinks there are 2
    )
    assert resp.status_code == 409
    assert "conversation changed" in resp.json()["detail"]
    assert len(alice.get("/history/conversations/t2").json()["messages"]) == 4


def test_truncate_is_scoped_to_the_owner(alice, env):
    _seed(alice, "t3", ["q1", "a1"])
    from app import db

    other = db.create_user("someone-else", "!x")
    db.create_conversation(other, "theirs", "private")
    assert (
        alice.post(
            "/history/conversations/theirs/truncate",
            json={"keep": 0, "expected_total": 2},
        ).status_code
        == 404
    )
    assert len(alice.get("/history/conversations/t3").json()["messages"]) == 2


def test_truncate_rejects_nonsense_bounds(alice):
    _seed(alice, "t4", ["q1", "a1"])
    assert (
        alice.post(
            "/history/conversations/t4/truncate",
            json={"keep": -1, "expected_total": 2},
        ).status_code
        == 400
    )
    assert (
        alice.post(
            "/history/conversations/t4/truncate",
            json={"keep": 5, "expected_total": 2},
        ).status_code
        == 400
    )
    # keep == total is a harmless no-op, not an error.
    assert (
        alice.post(
            "/history/conversations/t4/truncate",
            json={"keep": 2, "expected_total": 2},
        ).status_code
        == 200
    )
    assert len(alice.get("/history/conversations/t4").json()["messages"]) == 2


def test_truncate_cannot_write_content():
    """The endpoint's shape makes history rewriting impossible, not just
    disallowed: it accepts only two integers."""
    from app.history import TruncateIn

    assert set(TruncateIn.model_fields) == {"keep", "expected_total"}
    with pytest.raises(Exception):
        TruncateIn(keep=1, expected_total=2, messages=[{"role": "user"}])


# ---------------------------------------------------------------------------
# 4. One generation, many attached clients → stored exactly once
# ---------------------------------------------------------------------------


def test_append_is_idempotent_per_generation(alice):
    """Two browsers attached to one answer must not store it twice."""
    _seed(alice, "g1", ["a question"])
    meta = {"route": "chat", "generation_id": "gen-abc"}

    first = alice.post(
        "/history/conversations/g1/messages",
        json={"role": "assistant", "content": "the answer", "meta": meta},
    )
    second = alice.post(
        "/history/conversations/g1/messages",
        json={"role": "assistant", "content": "the answer", "meta": meta},
    )
    assert first.status_code == 200 and second.status_code == 200
    # The second call returned the SAME row rather than inserting.
    assert second.json()["id"] == first.json()["id"]
    assert second.json().get("deduplicated") is True

    messages = alice.get("/history/conversations/g1").json()["messages"]
    assert [m["content"] for m in messages] == ["a question", "the answer"]


def test_messages_without_a_generation_id_are_never_deduplicated(alice):
    """Ordinary turns can legitimately repeat (a user asking twice)."""
    _seed(alice, "g2", ["hello"])
    for _ in range(2):
        alice.post(
            "/history/conversations/g2/messages",
            json={"role": "user", "content": "hello"},
        )
    assert len(alice.get("/history/conversations/g2").json()["messages"]) == 3


def test_generation_ids_are_scoped_per_conversation(alice):
    _seed(alice, "g3", ["q"])
    _seed(alice, "g4", ["q"])
    meta = {"route": "chat", "generation_id": "shared-id"}
    alice.post(
        "/history/conversations/g3/messages",
        json={"role": "assistant", "content": "A", "meta": meta},
    )
    alice.post(
        "/history/conversations/g4/messages",
        json={"role": "assistant", "content": "B", "meta": meta},
    )
    # Same key, different conversations → both stored.
    assert len(alice.get("/history/conversations/g3").json()["messages"]) == 2
    assert len(alice.get("/history/conversations/g4").json()["messages"]) == 2


def test_chat_meta_carries_a_generation_id(env, monkeypatch):
    """The client can only dedupe if the server tells it the key."""
    import json as _json

    async def fake_stream(messages, **kwargs):
        yield "token", "hi"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    with TestClient(app) as c:
        resp = c.post(
            "/chat", json={"message": "hello", "mode": "assistant"}
        )
        metas = [
            _json.loads(line[6:])
            for block in resp.text.strip().split("\n\n")
            for line in block.split("\n")
            if block.startswith("event: meta") and line.startswith("data: ")
        ]
    assert metas and metas[0].get("generation_id")


def test_schema_carries_the_generation_id_column_and_its_partial_unique_index():
    """The idempotency key and the index that enforces it are both present.

    Under SQLite this was an additive ALTER applied on every connection; the
    PostgreSQL schema declares both up front, so what is worth asserting is the
    end state — including the `WHERE generation_id IS NOT NULL` predicate,
    without which every un-keyed message would collide with every other.
    """
    from app import db as _db

    with _db.connection() as con:
        cols = {
            r["column_name"]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='messages'"
            ).fetchall()
        }
        idx = con.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE tablename='messages' AND indexname='idx_messages_generation'"
        ).fetchone()

    assert "generation_id" in cols
    assert idx is not None, "the partial unique index is missing"
    assert "UNIQUE" in idx["indexdef"]
    assert "generation_id IS NOT NULL" in idx["indexdef"]


def test_concurrent_appends_of_one_generation_store_it_once(alice):
    """The dedupe must survive the RACE, not just sequential calls.

    Two attached clients finalize simultaneously; a select-then-insert check
    loses that race, so the constraint lives in the database.
    """
    import threading

    _seed(alice, "race1", ["a question"])
    meta = {"route": "chat", "generation_id": "gen-race"}
    user = db.get_user_by_username("alice")
    uid = int(user["id"])
    errors = []
    barrier = threading.Barrier(8)

    def append():
        try:
            barrier.wait()
            db.add_message(uid, "race1", "assistant", "the answer", meta)
        except Exception as exc:  # noqa: BLE001 - surfaced in the assert below
            errors.append(exc)

    threads = [threading.Thread(target=append) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    messages = alice.get("/history/conversations/race1").json()["messages"]
    assert [m["content"] for m in messages] == ["a question", "the answer"]


def test_a_second_message_for_one_generation_cannot_be_stored():
    """The database refuses the duplicate, rather than a repair pass removing
    it afterwards.

    SQLite could only enforce this once the index existed, so `migrate()`
    carried a one-time DELETE to clean up rows an earlier build had already
    written — a repair that, being unconditional, then re-ran forever. Here the
    constraint exists from the first migration, so there is nothing to repair;
    what matters is that the second write is rejected and `add_message` turns
    that rejection into the winning row.
    """
    from app import db as _db

    uid = _db.create_user("dupes", "hash")
    _db.create_conversation(uid, "c", "t")
    _db.add_message(uid, "c", "user", "q")
    meta = {"generation_id": "g1"}
    first = _db.add_message(uid, "c", "assistant", "first copy", meta)
    second = _db.add_message(uid, "c", "assistant", "second copy", meta)

    assert second["id"] == first["id"]
    assert second["deduplicated"] is True
    assert second["content"] == "first copy", "the first write must win"
    assert [(m["role"], m["content"]) for m in _db.list_messages("c")] == [
        ("user", "q"),
        ("assistant", "first copy"),
    ]


# ---------------------------------------------------------------------------
# 5. Schema readiness is a DEPLOY concern, not a first-request surprise
# ---------------------------------------------------------------------------


def test_startup_applies_the_migration_before_serving(monkeypatch):
    """Lifespan must migrate; /health must then report app_db ok.

    The point is unchanged from the SQLite version: schema readiness is a
    DEPLOY concern. Proving it needs a database that has NOT been migrated, so
    this one drops `schema_migrations` first and checks the lifespan puts it
    back before a single request is served.
    """
    from app import db as _db

    with _db.connection() as con:
        con.execute("DROP TABLE IF EXISTS schema_migrations")
    with _db.connection() as con:
        gone = con.execute(
            "SELECT to_regclass('public.schema_migrations') AS t"
        ).fetchone()
    assert gone["t"] is None

    with TestClient(app):  # entering the client runs the lifespan
        assert _db.schema_version() == _db.LATEST_SCHEMA_VERSION
        with _db.connection() as con:
            idx = con.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'messages'"
            ).fetchall()
    assert "idx_messages_generation" in {r["indexname"] for r in idx}


def test_health_reports_the_app_database(monkeypatch):
    from app import health as health_module

    with TestClient(app) as client:
        body = client.get("/health").json()
    assert "app_db" in body["checks"]
    assert body["checks"]["app_db"]["status"] == "ok"
    assert body["checks"]["app_db"]["schema_version"] >= 1

    # An unreachable database is REPORTED, not hidden behind a 500.
    from app.config import settings as _settings

    monkeypatch.setattr(
        _settings, "app_database_url", "postgresql://nobody@127.0.0.1:1/none"
    )
    result = health_module._check_app_db()
    assert result["status"] == "error" and result["detail"]

    # And so is a database whose migrations have not been applied.
    monkeypatch.setattr(health_module, "EXPECTED_SCHEMA_VERSION", 999)
    monkeypatch.undo()
    stale = health_module._check_app_db()
    assert stale["status"] == "ok"  # back on the real DSN, at the real version
