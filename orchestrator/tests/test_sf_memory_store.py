"""The Salesforce conversation-memory store, `app/sf_memory_store.py`
(design 2026-11-25 §2, §3.1, §3.3, §3.4, §13, §14; tests of §16.2).

What these pin:
  * one immutable row per answer: a duplicate generation writes nothing and
    returns None, so only a real insert can spawn a summary fold (C27);
  * the read is always this user's rows of this user's chat, never an
    expired row, never another access profile's;
  * the branch key: answer hash, then the exact parent hash, then the
    question hash, else the newest row (C1);
  * the compare-and-set summary update only moves forward;
  * the truncate rule deletes only rows of answers that are gone (C26);
  * sizes are cut in code before the CHECKs see them, a state over 32 KB is
    stored as NULL, and no log line or error carries state, summary or plan
    text.

The table comes from migration V45, which the suite's session fixture
applies with every other migration (conftest `app_database`). The
`sf_query_state_table` fixture below only checks that it is there; the
per-test truncation of `conversations` and `users` empties it through its
cascading foreign keys.

All names, texts and ids here are made up.
"""
from __future__ import annotations

import hashlib
import logging
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app import db
from app import sf_memory_store as store
from app.sse import strict_json

_LOGGER = "app.sf_memory_store"
_PLANTED = "PLANTED-SF-TEXT-a06000000000099AAA"


@pytest.fixture(scope="module", autouse=True)
def sf_query_state_table(app_database):
    """The table migration V45 made. Nothing is created or dropped here: a
    test database without it is a migration failure, not a fixture's job."""
    with db.connection() as con:
        table = con.execute(
            "SELECT to_regclass('public.sf_query_state') AS t"
        ).fetchone()["t"]
    assert table is not None, "migration V45 did not create sf_query_state"
    assert db.schema_version() >= 45
    yield


def _h(text: str) -> str:
    """The toggle-off G1 normalisation the real helper uses. A local copy for
    the tests only: the store never hashes text itself."""
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()


def _user(name: str) -> int:
    row = db.get_user_by_username(name)
    if row is None:
        return db.create_user(name, "!p1-test")
    return int(row["id"])


def _chat(user_id: int, conversation_id: str) -> str:
    db.create_conversation(user_id, conversation_id, "made-up chat")
    return conversation_id


@pytest.fixture
def alice():
    return _user("p1-alice")


@pytest.fixture
def bob():
    return _user("p1-bob")


def _insert(user_id, conversation_id, generation_id, answer, **kw):
    kw.setdefault("contract", 2)
    return store.insert(
        user_id=user_id,
        conversation_id=conversation_id,
        generation_id=generation_id,
        answer_sha1=_h(answer),
        **kw,
    )


def _count(conversation_id=None) -> int:
    with db.connection() as con:
        if conversation_id is None:
            return con.execute("SELECT count(*) AS n FROM sf_query_state").fetchone()["n"]
        return con.execute(
            "SELECT count(*) AS n FROM sf_query_state WHERE conversation_id = %s",
            (conversation_id,),
        ).fetchone()["n"]


def _stored(row_id: int) -> dict:
    with db.connection() as con:
        return con.execute("SELECT * FROM sf_query_state WHERE id = %s", (row_id,)).fetchone()


# ------------------------------------------------------------------ DDL --


def test_ddl_builds_the_design_table_and_applies_twice():
    with db.connection() as con:
        con.execute(store.DDL)  # IF NOT EXISTS throughout: a second run is a no-op
        columns = {
            r["column_name"]: r["data_type"]
            for r in con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_name = 'sf_query_state'"
            ).fetchall()
        }
        fks = {
            (r["col"], r["ref"], r["deltype"])
            for r in con.execute(
                "SELECT a.attname AS col, con.confrelid::regclass::text AS ref, "
                "       con.confdeltype AS deltype "
                "  FROM pg_constraint con "
                "  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1] "
                " WHERE con.contype = 'f' AND con.conrelid = 'sf_query_state'::regclass"
            ).fetchall()
        }
        constraints = {
            r["conname"]
            for r in con.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid = 'sf_query_state'::regclass"
            ).fetchall()
        }
        indexes = {
            r["indexname"]
            for r in con.execute(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'sf_query_state'"
            ).fetchall()
        }
    assert set(columns) == {
        "id", "conversation_id", "user_id", "workspace_id", "access_profile",
        "generation_id", "answer_sha1", "parent_sha1", "question_sha1", "contract",
        "state", "plan_line", "summary", "summary_pairs", "path_pairs", "created_at",
        "summary_at", "expires_at",
    }
    assert columns["state"] == "jsonb"
    assert columns["contract"] == "smallint"
    # Both foreign keys cascade ('c'): the row lives and dies with its chat
    # and its user.
    assert fks == {("conversation_id", "conversations", "c"), ("user_id", "users", "c")}
    assert {
        "sf_query_state_generation",
        "sf_query_state_summary_len",
        "sf_query_state_plan_line_len",
    } <= constraints
    assert {
        "idx_sf_query_state_lookup",
        "idx_sf_query_state_user",
        "idx_sf_query_state_expiry",
    } <= indexes


def test_every_foreign_key_leads_an_index():
    with db.connection() as con:
        unindexed = con.execute(
            "SELECT a.attname AS col FROM pg_constraint con "
            "  JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1] "
            " WHERE con.contype = 'f' AND con.conrelid = 'sf_query_state'::regclass "
            "   AND NOT EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid = con.conrelid "
            "                    AND i.indkey[0] = con.conkey[1])"
        ).fetchall()
    assert unindexed == []


# --------------------------------------------------------------- insert --


def test_insert_returns_the_row_and_a_duplicate_generation_writes_nothing(alice):
    conv = _chat(alice, "p1-chat-dedupe")
    first = _insert(
        alice, conv, "gen-1", "Interviews this month: shown as a chart.",
        question_sha1=_h("interviews this month"),
        state={"v": 1, "plan": {"calls": []}},
        plan_line="interviews per support person, 1-30 Nov 2026",
        path_pairs=1,
        workspace_id="ws-made-up",
    )
    assert first is not None and isinstance(first["id"], int)
    assert first["conversation_id"] == conv
    assert first["user_id"] == alice
    assert first["generation_id"] == "gen-1"
    assert first["contract"] == 2
    assert first["state"] == {"v": 1, "plan": {"calls": []}}
    assert first["workspace_id"] == "ws-made-up"
    assert first["access_profile"] is None
    assert first["expires_at"] is None
    assert first["created_at"] is not None

    # A second tab or a retried hook for the same generation: no row, no
    # fold, and the first row is not touched (rows are immutable).
    again = _insert(alice, conv, "gen-1", "a different text", state={"v": 9})
    assert again is None
    assert _count(conv) == 1
    assert _stored(first["id"])["state"] == {"v": 1, "plan": {"calls": []}}


def test_the_same_generation_in_another_chat_is_its_own_row(alice):
    one = _chat(alice, "p1-chat-gen-a")
    two = _chat(alice, "p1-chat-gen-b")
    assert _insert(alice, one, "gen-shared", "answer one") is not None
    assert _insert(alice, two, "gen-shared", "answer one") is not None
    assert _count() == 2


def test_insert_lands_only_in_a_chat_the_user_owns(alice, bob):
    bobs = _chat(bob, "p1-chat-of-bob")
    assert _insert(alice, bobs, "gen-x", "answer") is None
    assert _insert(alice, "p1-chat-that-does-not-exist", "gen-y", "answer") is None
    # A bare-API style key has no conversations row either.
    assert _insert(alice, f"u{alice}-p1-bare", "gen-z", "answer") is None
    assert _count() == 0


def test_a_state_over_32_kb_is_stored_as_null_and_one_at_the_cap_is_kept(alice, caplog):
    conv = _chat(alice, "p1-chat-cap")
    overhead = len(db._dumps_jsonb({"pad": ""}).encode("utf-8"))
    at_cap = {"pad": "x" * (store.STATE_MAX_BYTES - overhead)}
    over = {"pad": "x" * (store.STATE_MAX_BYTES - overhead + 1), "mark": _PLANTED}
    assert len(db._dumps_jsonb(at_cap).encode("utf-8")) == store.STATE_MAX_BYTES

    kept = _insert(alice, conv, "gen-cap", "answer at the cap", state=at_cap)
    assert kept["state"] == at_cap

    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    dropped = _insert(alice, conv, "gen-over", "answer over the cap", state=over)
    # The row still exists (the walk-back finds the answer), with no plan.
    assert dropped is not None and dropped["state"] is None
    assert any("over the cap" in r.getMessage() for r in caplog.records)
    assert all(_PLANTED not in r.getMessage() for r in caplog.records)


def test_the_cap_counts_bytes_not_characters(alice):
    conv = _chat(alice, "p1-chat-bytes")
    # Two bytes per character: under the cap in characters, over it in bytes.
    wide = {"pad": "é" * (store.STATE_MAX_BYTES // 2)}
    assert len(db._dumps_jsonb(wide)) < store.STATE_MAX_BYTES
    row = _insert(alice, conv, "gen-wide", "answer", state=wide)
    assert row["state"] is None


@pytest.mark.parametrize("bad", [["a", "list"], "a string", 42, True])
def test_a_state_that_is_not_an_object_is_stored_as_null(alice, bad):
    conv = _chat(alice, "p1-chat-shape")
    row = _insert(alice, conv, "gen-shape", "answer", state=bad)
    assert row is not None and row["state"] is None


def test_plan_line_and_summary_are_cut_in_code_before_the_checks(alice):
    conv = _chat(alice, "p1-chat-cut")
    row = _insert(
        alice, conv, "gen-cut", "answer",
        plan_line="p" * 700,
        summary="s" * 9000,
        summary_pairs=1,
        path_pairs=2,
    )
    assert len(row["plan_line"]) == store.PLAN_LINE_MAX_CHARS == 600
    assert len(row["summary"]) == store.SUMMARY_MAX_CHARS == 8000

    # NUL (which PostgreSQL text refuses) is removed, a lone surrogate (which
    # UTF-8 cannot carry) becomes '?', and the row is still written.
    odd = _insert(alice, conv, "gen-odd", "answer two", plan_line="top\x005 \ud800 rows")
    assert odd["plan_line"] == "top5 ? rows"


def test_a_state_is_kept_through_the_jsonb_round_trip(alice):
    conv = _chat(alice, "p1-chat-jsonb")
    state = {
        "v": 1,
        "plan": {"terms": {"text": "made-up words", "keys": ["candidates"]}, "age": 0},
        "pending": None,
        "last": {"reason": "records", "kind": "records"},
    }
    _insert(alice, conv, "gen-json", "answer", state=state)
    got = store.lookup(alice, conv, answer_sha1=_h("answer"))
    # jsonb reorders keys on storage; the value is what matters.
    assert got["state"] == state


def test_a_typed_backslash_u0000_in_a_state_is_stored_exactly(alice):
    """Review F1. The six typed characters `\\u0000` are text, not a NUL: at
    the end of a value they used to make the JSON invalid (the row was lost)
    and inside one they came back changed ("C:\\u0000temp" as "C:<TAB>emp").
    A real NUL code point, which jsonb cannot hold, is removed from values
    and keys alike, and the row is always written."""
    conv = _chat(alice, "p1-chat-escape")
    at_end = "folder C:\\u0000"
    inside = "folder C:\\u0000temp"
    state = {
        "v": 1,
        "terms": {"keys": ["candidates"], "text": at_end},
        "filters": [{"value": inside}, "a\\u0000b"],
        "odd\x00key": "real\x00nul",
    }
    row = _insert(alice, conv, "gen-escape", "answer escape", state=state)
    assert row is not None
    expected = {
        "v": 1,
        "terms": {"keys": ["candidates"], "text": at_end},
        "filters": [{"value": inside}, "a\\u0000b"],
        "oddkey": "realnul",
    }
    assert row["state"] == expected
    assert _stored(row["id"])["state"] == expected
    got = store.lookup(alice, conv, answer_sha1=_h("answer escape"))
    assert got["state"]["terms"]["text"] == at_end
    assert got["state"]["filters"][0]["value"] == inside


def test_the_state_cap_is_measured_on_the_exact_text_stored(alice):
    """The 32 KB rule reads the text that is stored. Each typed `\\u0000`
    is seven characters there (the backslash is escaped). The old house
    helper removed six of them per occurrence, so a state over the cap
    passed; since stage 2 `db._dumps_jsonb` writes the exact text too (it
    removes NUL from the object, open point 16)."""
    conv = _chat(alice, "p1-chat-escape-cap")
    unit = "\\u0000"
    overhead = len(db._dumps_jsonb({"pad": ""}).encode("utf-8"))
    copies = (store.STATE_MAX_BYTES - overhead) // 7 + 1  # 7 stored bytes each
    state = {"pad": unit * copies}
    assert db._dumps_jsonb(state) == strict_json(state)
    assert len(db._dumps_jsonb(state).encode("utf-8")) > store.STATE_MAX_BYTES
    row = _insert(alice, conv, "gen-escape-cap", "answer", state=state)
    assert row is not None and row["state"] is None


def test_the_parent_summary_fields_are_copied_at_insert(alice):
    conv = _chat(alice, "p1-chat-copy")
    folded_at = datetime(2026, 11, 20, 9, 30, tzinfo=timezone.utc)
    row = _insert(
        alice, conv, "gen-copy", "answer",
        summary="earlier notes about the made-up objective",
        summary_pairs=2,
        summary_at=folded_at.isoformat(),
        path_pairs=9,
    )
    assert row["summary"] == "earlier notes about the made-up objective"
    assert row["summary_pairs"] == 2
    assert row["summary_at"] == folded_at
    assert row["path_pairs"] == 9


@pytest.mark.parametrize(
    "overrides, name",
    [
        ({"answer_sha1": "the whole answer text, 42 records"}, "answer_sha1"),
        ({"answer_sha1": ""}, "answer_sha1"),
        ({"parent_sha1": "previous answer text"}, "parent_sha1"),
        ({"question_sha1": "ABCDEF" + "0" * 34}, "question_sha1"),
        ({"user_id": True}, "user_id"),
        ({"user_id": 0}, "user_id"),
        ({"generation_id": ""}, "generation_id"),
        ({"contract": 0}, "contract"),
        ({"summary_pairs": -1}, "summary_pairs"),
        ({"path_pairs": "3"}, "path_pairs"),
        ({"expires_at": datetime(2027, 1, 1)}, "expires_at"),
    ],
)
def test_bad_arguments_are_refused_without_echoing_them(alice, overrides, name):
    conv = _chat(alice, "p1-chat-args")
    args = dict(
        user_id=alice, conversation_id=conv, generation_id="gen-args",
        answer_sha1=_h("answer"), contract=2,
    )
    args.update(overrides)
    with pytest.raises(store.InvalidArgument) as err:
        store.insert(**args)
    assert err.value.name == name
    assert isinstance(err.value, ValueError) and isinstance(err.value, store.StoreError)
    for value in overrides.values():
        if isinstance(value, str) and value:
            assert value not in str(err.value)
    assert _count() == 0


# --------------------------------------------------------------- lookup --


def test_lookup_by_answer_parent_and_question_hash(alice):
    conv = _chat(alice, "p1-chat-path")
    a1, a2 = "Interviews by support person: chart.", "Same for last month: chart."
    first = _insert(alice, conv, "gen-a1", a1, question_sha1=_h("interviews by support person"))
    second = _insert(
        alice, conv, "gen-a2", a2, parent_sha1=_h(a1), question_sha1=_h("same for last month")
    )

    got = store.lookup(alice, conv, answer_sha1=_h(a1), question_sha1=_h("interviews by support person"))
    assert got["id"] == first["id"] and got["matched"] == "question"
    got = store.lookup(
        alice, conv, answer_sha1=_h(a2), parent_sha1=_h(a1), question_sha1=_h("same for last month")
    )
    assert got["id"] == second["id"] and got["matched"] == "question"

    # The parent hash is an exact match: '' means "first assistant turn",
    # never "any parent".
    assert store.lookup(alice, conv, answer_sha1=_h(a2)) is None
    assert store.lookup(alice, conv, answer_sha1=_h(a1), parent_sha1=_h(a2)) is None
    # An answer with no row.
    assert store.lookup(alice, conv, answer_sha1=_h("never stored")) is None


def test_identical_sibling_answers_are_told_apart_by_the_question(alice):
    """After an edit both answers share the parent and the text (C1)."""
    conv = _chat(alice, "p1-chat-siblings")
    parent, same = "Placements this quarter: 3.", "No records match."
    _insert(alice, conv, "gen-p", parent)
    older = _insert(
        alice, conv, "gen-s1", same, parent_sha1=_h(parent),
        question_sha1=_h("only for the made-up region"),
    )
    newer = _insert(
        alice, conv, "gen-s2", same, parent_sha1=_h(parent),
        question_sha1=_h("only for the other made-up region"),
    )
    with db.connection() as con:  # make the order explicit, not a clock race
        con.execute(
            "UPDATE sf_query_state SET created_at = created_at - interval '1 minute' WHERE id = %s",
            (older["id"],),
        )

    got = store.lookup(
        alice, conv, answer_sha1=_h(same), parent_sha1=_h(parent),
        question_sha1=_h("only for the made-up region"),
    )
    assert got["id"] == older["id"] and got["matched"] == "question"
    got = store.lookup(
        alice, conv, answer_sha1=_h(same), parent_sha1=_h(parent),
        question_sha1=_h("only for the other made-up region"),
    )
    assert got["id"] == newer["id"] and got["matched"] == "question"
    # A question hash that matches neither: the newest candidate.
    got = store.lookup(
        alice, conv, answer_sha1=_h(same), parent_sha1=_h(parent),
        question_sha1=_h("a question typed some other way"),
    )
    assert got["id"] == newer["id"] and got["matched"] == "newest"


def test_another_users_row_is_never_returned(alice, bob):
    mine = _chat(alice, "p1-chat-alice")
    theirs = _chat(bob, "p1-chat-bob")
    text = "Open positions by client: chart."
    _insert(bob, theirs, "gen-b", text)
    # A row filed under Bob inside Alice's chat cannot come from `insert`;
    # plant one by hand, as a forged or buggy write would.
    with db.connection() as con:
        con.execute(
            "INSERT INTO sf_query_state (conversation_id, user_id, generation_id, "
            "answer_sha1, contract, state) VALUES (%s, %s, 'gen-forged', %s, 2, %s)",
            (mine, bob, _h(text), db._json_param({"v": 1, "mark": "bob"})),
        )

    assert store.lookup(alice, mine, answer_sha1=_h(text)) is None
    assert store.lookup(alice, theirs, answer_sha1=_h(text)) is None
    assert store.fetch_candidates(alice, theirs, [_h(text)]) == []
    # Bob reaches his own row in his own chat, and not the forged one in
    # Alice's chat: the chat must be his as well.
    assert store.lookup(bob, theirs, answer_sha1=_h(text))["generation_id"] == "gen-b"
    assert store.fetch_candidates(bob, mine, [_h(text)]) == []


def test_a_reclaimed_chat_id_starts_without_state(alice, bob):
    conv = _chat(alice, "p1-chat-reclaimed")
    _insert(alice, conv, "gen-old", "an earlier answer")
    assert db.delete_conversation(alice, conv)
    _chat(bob, conv)  # conversation ids are client-chosen
    assert store.lookup(bob, conv, answer_sha1=_h("an earlier answer")) is None
    assert _count(conv) == 0


def test_an_expired_row_is_not_returned(alice):
    conv = _chat(alice, "p1-chat-expiry")
    now = datetime.now(timezone.utc)
    _insert(alice, conv, "gen-past", "expired answer", expires_at=now - timedelta(days=1))
    _insert(alice, conv, "gen-future", "living answer", expires_at=now + timedelta(days=1))
    _insert(alice, conv, "gen-none", "answer with no expiry")
    assert store.lookup(alice, conv, answer_sha1=_h("expired answer")) is None
    assert store.lookup(alice, conv, answer_sha1=_h("living answer")) is not None
    assert store.lookup(alice, conv, answer_sha1=_h("answer with no expiry")) is not None


def test_an_access_profile_mismatch_is_dropped(alice):
    conv = _chat(alice, "p1-chat-profile")
    _insert(alice, conv, "gen-prof", "profiled answer", access_profile="profile-made-up")
    _insert(alice, conv, "gen-plain", "plain answer")
    assert store.lookup(alice, conv, answer_sha1=_h("profiled answer")) is None
    assert store.lookup(
        alice, conv, answer_sha1=_h("profiled answer"), access_profile="profile-made-up"
    ) is not None
    assert store.lookup(
        alice, conv, answer_sha1=_h("profiled answer"), access_profile="profile-other"
    ) is None
    assert store.lookup(
        alice, conv, answer_sha1=_h("plain answer"), access_profile="profile-made-up"
    ) is None
    # '' is "no profile", the same as None.
    assert store.lookup(alice, conv, answer_sha1=_h("plain answer"), access_profile="") is not None


def test_fetch_is_newest_first_capped_and_one_read_serves_a_walk_back(alice):
    conv = _chat(alice, "p1-chat-order")
    ids = [_insert(alice, conv, f"gen-{n}", "repeated fixed line")["id"] for n in range(3)]
    other = _insert(alice, conv, "gen-other", "another answer")["id"]
    with db.connection() as con:
        # ids[0] newest by clock; ids[1] and ids[2] tie, so id DESC decides.
        con.execute(
            "UPDATE sf_query_state SET created_at = %s WHERE id = %s",
            (datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc), ids[0]),
        )
        con.execute(
            "UPDATE sf_query_state SET created_at = %s WHERE id = ANY(%s)",
            (datetime(2026, 11, 25, 9, 0, tzinfo=timezone.utc), [ids[1], ids[2], other]),
        )
    rows = store.fetch_candidates(
        alice, conv, [_h("repeated fixed line"), _h("another answer"), _h("repeated fixed line")]
    )
    assert [r["id"] for r in rows] == [ids[0], other, ids[2], ids[1]]
    capped = store.fetch_candidates(alice, conv, [_h("repeated fixed line")], limit=2)
    assert [r["id"] for r in capped] == [ids[0], ids[2]]
    # The walk-back picks per turn from the one result.
    assert store.pick(rows, answer_sha1=_h("another answer"))["id"] == other
    assert store.pick(rows, answer_sha1=_h("repeated fixed line"))["id"] == ids[0]
    assert store.fetch_candidates(alice, conv, []) == []


def test_pick_is_pure_and_strict_on_parent_and_profile():
    a, p, q = _h("answer"), _h("parent"), _h("question")
    rows = [
        {"id": 3, "answer_sha1": a, "parent_sha1": "", "question_sha1": q, "access_profile": None},
        {"id": 2, "answer_sha1": a, "parent_sha1": p, "question_sha1": "", "access_profile": "x"},
        {"id": 1, "answer_sha1": a, "parent_sha1": p, "question_sha1": "", "access_profile": None},
    ]
    assert store.pick(rows, answer_sha1=a, parent_sha1=p, question_sha1=q)["id"] == 1
    assert store.pick(rows, answer_sha1=a, parent_sha1=p, access_profile="x")["id"] == 2
    assert store.pick(rows, answer_sha1=a, question_sha1=q)["id"] == 3
    assert store.pick(rows, answer_sha1=_h("other")) is None
    assert "matched" not in rows[0]  # the input rows are not changed


# -------------------------------------------------------------- summary --


def test_the_summary_update_is_compare_and_set_and_only_moves_forward(alice, bob):
    conv = _chat(alice, "p1-chat-fold")
    row = _insert(alice, conv, "gen-fold", "answer", path_pairs=5)
    rid = row["id"]
    assert row["summary_at"] is None

    assert store.update_summary(
        conv, rid, expected_pairs=0, summary="objective: made-up", summary_pairs=2
    )
    stored = _stored(rid)
    assert (stored["summary"], stored["summary_pairs"]) == ("objective: made-up", 2)
    assert stored["summary_at"] is not None

    # A late fold that started from the old count loses; the newer one stays.
    assert not store.update_summary(conv, rid, expected_pairs=0, summary="late", summary_pairs=3)
    # A fold never stands still or moves back, and never covers more pairs
    # than the path holds.
    assert not store.update_summary(conv, rid, expected_pairs=2, summary="same", summary_pairs=2)
    assert not store.update_summary(conv, rid, expected_pairs=2, summary="past", summary_pairs=6)
    # The row id alone is not enough: the chat (and the user, when given)
    # must match too.
    other = _chat(alice, "p1-chat-fold-other")
    assert not store.update_summary(other, rid, expected_pairs=2, summary="x", summary_pairs=3)
    assert not store.update_summary(
        conv, rid, expected_pairs=2, summary="x", summary_pairs=3, user_id=bob
    )
    assert _stored(rid)["summary"] == "objective: made-up"

    # Cut in code to 8,000 characters before the CHECK sees it.
    assert store.update_summary(
        conv, rid, expected_pairs=2, summary="n" * 9000, summary_pairs=5, user_id=alice
    )
    stored = _stored(rid)
    assert len(stored["summary"]) == 8000 and stored["summary_pairs"] == 5


def test_the_summary_update_never_writes_a_row_filed_under_someone_else(alice, bob):
    """Review DS-5. Like every read, the update needs the chat to belong to
    the row's user, so a forged row (Bob's, inside Alice's chat) is never
    written even when no user is named."""
    conv = _chat(alice, "p1-chat-fold-forged")
    with db.connection() as con:
        forged = con.execute(
            "INSERT INTO sf_query_state (conversation_id, user_id, generation_id, "
            "answer_sha1, contract, path_pairs) VALUES (%s, %s, 'gen-forged', %s, 2, 5) "
            "RETURNING id",
            (conv, bob, _h("made-up forged answer")),
        ).fetchone()["id"]
    for user in (None, bob, alice):
        assert not store.update_summary(
            conv, forged, expected_pairs=0, summary="made-up", summary_pairs=1, user_id=user
        )
    assert _stored(forged)["summary"] == ""


def test_the_fold_adapter_names_the_owner_to_the_store(alice, bob):
    """Review DS-5 through the fold's own adapter, against the real store:
    the owner it is given is a filter, so a wrong one writes nothing."""
    from app import sf_summary

    conv = _chat(alice, "p1-chat-fold-owner")
    row = _insert(alice, conv, "gen-fold-owner", "answer", path_pairs=3)
    assert not sf_summary._store_summary(
        conv, row["id"], expected_pairs=0, summary="made-up notes", summary_pairs=1, user_id=bob
    )
    assert _stored(row["id"])["summary"] == ""
    assert sf_summary._store_summary(
        conv, row["id"], expected_pairs=0, summary="made-up notes", summary_pairs=1, user_id=alice
    )
    assert _stored(row["id"])["summary"] == "made-up notes"


# ------------------------------------------------------------- truncate --


def test_truncate_deletes_only_rows_of_removed_answers(alice):
    conv = _chat(alice, "p1-chat-truncate")
    turns = [
        ("Interviews this month?", "Interviews this month: 7 shown."),
        ("By month instead", "Interviews by month: chart."),
        ("Only placements", "Placements by month: chart."),
    ]
    parent = ""
    ids = []
    for n, (question, answer) in enumerate(turns):
        db.add_message(alice, conv, "user", question)
        db.add_message(alice, conv, "assistant", answer)
        ids.append(
            _insert(
                alice, conv, f"gen-t{n}", answer,
                parent_sha1=parent, question_sha1=_h(question),
            )["id"]
        )
        parent = _h(answer)
    elsewhere = _chat(alice, "p1-chat-untouched")
    _insert(alice, elsewhere, "gen-e", turns[2][1])

    # Keep the first pair only: the second and third answers are gone.
    db.truncate_messages(alice, conv, 2, 6)
    deleted = store.delete_removed_answers(alice, conv, answer_key=_h)
    assert deleted == 2
    with db.connection() as con:
        left = [r["id"] for r in con.execute(
            "SELECT id FROM sf_query_state WHERE conversation_id = %s", (conv,)
        ).fetchall()]
    assert left == [ids[0]]
    assert _count(elsewhere) == 1
    # A second run has nothing left to delete.
    assert store.delete_removed_answers(alice, conv, answer_key=_h) == 0


def test_truncate_matches_answers_with_the_shared_normalisation(alice):
    conv = _chat(alice, "p1-chat-spaces")
    db.add_message(alice, conv, "user", "made-up question")
    db.add_message(alice, conv, "assistant", "Records:\n\n  three   shown.\n")
    _insert(alice, conv, "gen-sp", "Records: three shown.")
    assert store.delete_removed_answers(alice, conv, answer_key=_h) == 0
    assert _count(conv) == 1


def test_truncate_of_a_chat_that_is_not_the_users_deletes_nothing(alice, bob):
    conv = _chat(alice, "p1-chat-not-bobs")
    _insert(alice, conv, "gen-n", "answer with no message")
    assert store.delete_removed_answers(bob, conv, answer_key=_h) == 0
    assert store.delete_removed_answers(bob, "p1-chat-missing", answer_key=_h) == 0
    assert _count(conv) == 1
    # Alice's own truncate of a chat with no assistant message left clears it.
    assert store.delete_removed_answers(alice, conv, answer_key=_h) == 1


# ------------------------------------------------------- with the chat --


def test_rows_go_with_their_chat_and_with_their_user(alice, bob):
    mine = _chat(alice, "p1-chat-delete")
    theirs = _chat(bob, "p1-chat-delete-bob")
    _insert(alice, mine, "gen-d1", "answer")
    _insert(bob, theirs, "gen-d2", "answer")
    assert db.delete_conversation(alice, mine)
    assert _count(mine) == 0
    with db.connection() as con:
        con.execute("DELETE FROM users WHERE id = %s", (bob,))
    assert _count() == 0


# ------------------------------------------------- failures and privacy --


def test_a_database_failure_raises_a_store_error_that_carries_no_text(alice, monkeypatch, caplog):
    """A real CHECK failure: psycopg's DETAIL quotes the whole failing row,
    plan text included. Neither the error nor the log may carry it."""
    conv = _chat(alice, "p1-chat-check")
    monkeypatch.setattr(store, "PLAN_LINE_MAX_CHARS", 700)  # let a long line past the cut
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    with pytest.raises(store.StoreError) as err:
        _insert(alice, conv, "gen-check", "answer", plan_line=_PLANTED + "p" * 650,
                state={"mark": _PLANTED}, summary=_PLANTED)
    assert err.value.op == "insert"
    assert err.value.reason == "sqlstate 23514"  # check_violation
    shown = "".join(traceback.format_exception(err.value))
    assert _PLANTED not in shown
    assert err.value.__cause__ is None and err.value.__suppress_context__
    assert all(_PLANTED not in r.getMessage() for r in caplog.records)
    assert any("sqlstate=23514" in r.getMessage() for r in caplog.records)
    assert _count(conv) == 0


@pytest.mark.parametrize("call", ["insert", "fetch", "update", "truncate"])
def test_every_operation_turns_a_lost_database_into_a_store_error(alice, monkeypatch, call):
    conv = _chat(alice, "p1-chat-down")
    row = _insert(alice, conv, "gen-down", "answer", path_pairs=3)

    @contextmanager
    def down():
        raise psycopg.OperationalError(f"server closed the connection: {_PLANTED}")
        yield  # pragma: no cover

    monkeypatch.setattr(db, "connection", down)
    monkeypatch.setattr(db, "read_connection", down)
    actions = {
        "insert": lambda: _insert(alice, conv, "gen-down-2", "answer two"),
        "fetch": lambda: store.fetch_candidates(alice, conv, [_h("answer")]),
        "update": lambda: store.update_summary(
            conv, row["id"], expected_pairs=0, summary="s", summary_pairs=1
        ),
        "truncate": lambda: store.delete_removed_answers(alice, conv, answer_key=_h),
    }
    with pytest.raises(store.StoreError) as err:
        actions[call]()
    assert not isinstance(err.value, store.InvalidArgument)
    assert err.value.reason == "sqlstate OperationalError"
    assert _PLANTED not in "".join(traceback.format_exception(err.value))


def test_no_log_line_holds_state_summary_or_plan_text(alice, caplog):
    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    conv = _chat(alice, "p1-chat-logs")
    db.add_message(alice, conv, "user", f"question {_PLANTED}")
    db.add_message(alice, conv, "assistant", f"answer {_PLANTED}")
    row = _insert(
        alice, conv, "gen-log", f"answer {_PLANTED}",
        question_sha1=_h(f"question {_PLANTED}"),
        state={"plan": {"question": _PLANTED}}, plan_line=_PLANTED, summary=_PLANTED,
        summary_pairs=0, path_pairs=4,
    )
    _insert(alice, conv, "gen-log", f"answer {_PLANTED}")  # duplicate
    _insert(alice, conv, "gen-big", "big", state={"pad": _PLANTED * 2000})  # over the cap
    _insert(alice, conv, "gen-list", "list", state=[_PLANTED])  # not an object
    store.lookup(alice, conv, answer_sha1=_h(f"answer {_PLANTED}"))
    store.update_summary(conv, row["id"], expected_pairs=0, summary=_PLANTED, summary_pairs=1)
    store.update_summary(conv, row["id"], expected_pairs=0, summary=_PLANTED, summary_pairs=2)
    store.update_summary(conv, row["id"], expected_pairs=1, summary=_PLANTED, summary_pairs=1)
    db.truncate_messages(alice, conv, 0, 2)
    store.delete_removed_answers(alice, conv, answer_key=_h)

    assert caplog.records, "the operations above log ids and sizes"
    for record in caplog.records:
        assert _PLANTED not in record.getMessage()
        assert record.exc_info is None
