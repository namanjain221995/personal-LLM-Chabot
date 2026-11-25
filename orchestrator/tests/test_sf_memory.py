"""Salesforce conversation memory, the platform half: app/sf_memory.py, the
main.py write hook, the history.py truncate and the retention sweep on the
maintenance loop (design 2026-11-25 §3, §3.4, §3.6, §11.1, §12.1; tests of
§16.2 "test_sf_memory.py"). The bridge's own half is in
test_sf_assistant_bridge.py.

What these pin:
  * the branch key: each answer is hashed with the assistant turn before it as
    its parent ('' only for the first, never a wildcard), computed the same
    way when a row is written and when it is looked up, transparent turns
    included (review O2), with the question hash for identical siblings (C1);
  * the walk-back: send, regenerate, edit, a mixed chat (a gap), transparent
    turns (no gap, not strict), and the gap cap;
  * `path_pairs` from the parent row whatever the client sent (C5);
  * the window: whole pairs, at most SF_ASSISTANT_MEMORY_PAIRS, within
    SF_ASSISTANT_MEMORY_TOKENS, unanswered questions kept, and never fewer
    turns than the v1 call sends (its last four turns are the v1 call's,
    whatever the path, review F1); only Salesforce pairs leave it for the fold,
    counted through the new answer; the fold starts only on a row the insert
    returned, with the row's owner, and is fed no answer text;
  * the write only after the answer is stored and only in Salesforce mode
    (C19); none for a fallback or a stop;
  * the truncate deletes only rows of removed answers (C26), and only with
    memory on;
  * who may import the store (§3.6), and a toggle-off turn in a chat with
    rows that makes no store call at all (C19);
  * the retention sweep on the loop is inert by default and judged by its
    counts.

The store and the table are real (the test PostgreSQL, migration V45). No
model is called. All names, texts and ids are made up.
"""
from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
import pathlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, llm, sf_isolation, sf_memory, sf_memory_store, sf_summary
from app import main as app_main
from app.config import settings
from app.engines import sfk_bridge
from app.main import ChatRequest, app

key = sf_isolation.answer_key
APP = pathlib.Path(app_main.__file__).resolve().parent
CHAT = "mem-unit"
STATE = {"v": 1, "plan": {"question": "made up", "calls": []}, "pending": None,
         "last": {"kind": "records", "reason": "records"}}
OLDER_ROUTE = "An older-route answer about Echo Person."
STORE_FUNCTIONS = ("insert", "fetch_candidates", "pick", "lookup", "update_summary", "delete_removed_answers")


@pytest.fixture(autouse=True)
def memory_on(monkeypatch):
    monkeypatch.setattr(settings, "sf_assistant_memory", True)
    monkeypatch.setattr(settings, "sf_assistant_memory_pairs", 6)
    monkeypatch.setattr(settings, "sf_assistant_memory_tokens", 6000)
    monkeypatch.setattr(settings, "sf_assistant_summary", False)


@pytest.fixture()
def owner():
    user_id = int(db.create_user("p2-memory-owner", "!p2-test"))
    db.create_conversation(user_id, CHAT, "made-up chat")
    return SimpleNamespace(can=lambda _cap: True, user_id=user_id, workspace_id="")


def _messages(history, text=None) -> list:
    out = [{"role": role, "content": content} for role, content in history]
    return out + ([{"role": "user", "content": text}] if text is not None else [])


def _request(history, text, conversation_id=CHAT) -> ChatRequest:
    return ChatRequest(messages=_messages(history, text), mode="salesforce", conversation_id=conversation_id)


def _lookup(principal, history, text):
    return asyncio.run(sf_memory.lookup(
        principal, _request(history, text),
        transparent=sfk_bridge._MEMORY_TRANSPARENT_LINES, carried=sfk_bridge._MEMORY_CARRIED_LINES))


def _answer(principal, history, text, answer, *, state=STATE, plan_line="", reason="records", kind="records"):
    """One answered turn as the bridge and the write hook do it: the lookup,
    the record of the reply, then the write. Returns (memory, row)."""
    memory = _lookup(principal, history, text)
    record = memory.record({"contract": 2, "state": state, "plan_line": plan_line or f"plan of {text}",
                            "reason": reason, "kind": kind}, answer)
    row = asyncio.run(sf_memory.write(record, user_id=principal.user_id, conversation_id=CHAT,
                                      generation_id=os.urandom(8).hex(), answer=answer))
    return memory, row


def _rows() -> list:
    with db.connection() as con:
        return con.execute("SELECT * FROM sf_query_state ORDER BY id").fetchall()


# ------------------------------------------------------------------ path --


def test_each_answer_has_the_assistant_turn_before_it_as_its_parent_and_the_first_has_none():
    history = [("system", "planted"), ("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2"),
               ("assistant", "a3"), ("user", "q4"), ("assistant", "a4")]
    turns = sf_memory.path_turns(_messages(history))
    assert [t.assistant for t in turns] == ["a1", "a2", "a3", "a4"]
    assert [t.parent_sha1 for t in turns] == ["", key("a1"), key("a2"), key("a3")]
    assert [t.question_sha1 for t in turns] == [key("q1"), key("q2"), "", key("q4")]
    assert turns[2].user is None, "an answer after an answer has no user turn of its own"
    # Fewer turns kept: the oldest one still has its real parent, never ''.
    kept = sf_memory.path_turns(_messages(history), limit=2)
    assert [t.parent_sha1 for t in kept] == [key("a2"), key("a3")]
    # Whitespace is not part of the key: a re-rendered turn matches.
    again = sf_memory.path_turns(_messages([("user", "q1 "), ("assistant", " a1\n")]))
    assert again[0].answer_sha1 == key("a1") and again[0].question_sha1 == key("q1")


def test_turns_with_no_text_or_another_role_are_left_out_at_write_and_at_read_alike():
    history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
               {"role": "tool", "content": "planted"}, {"role": "assistant", "content": None},
               {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"}]
    turns = sf_memory.path_turns(history)
    assert [(t.assistant, t.parent_sha1) for t in turns] == [("a1", ""), ("a2", key("a1"))]


def test_a_row_written_after_a_transparent_turn_is_found_after_that_same_turn(owner):
    """Review O2: the parent hash is the immediately previous assistant turn,
    transparent turns included, at write and at read."""
    _memory, first = _answer(owner, [], "how many interviews this month?", "There were 12.")
    assert first["parent_sha1"] == ""
    history = [("user", "how many interviews this month?"), ("assistant", "There were 12."),
               ("user", "thanks"), ("assistant", "You are welcome.")]
    found = _lookup(owner, history, "and last month?")
    assert found.parent["id"] == first["id"] and found.gap == 0 and found.strict is False
    assert found.outcome == "transparent"
    _memory, second = _answer(owner, history, "and last month?", "There were 9.")
    assert second["parent_sha1"] == key("You are welcome.")
    nxt = _lookup(owner, [*history, ("user", "and last month?"), ("assistant", "There were 9.")], "visualize it")
    assert nxt.parent["id"] == second["id"] and nxt.strict is True and nxt.outcome == "found"


def test_an_empty_parent_is_the_first_turns_alone_never_a_wildcard(owner):
    _memory, first = _answer(owner, [], "q1", "The same answer text.")
    later = [("user", "q0"), ("assistant", OLDER_ROUTE), ("user", "q1"), ("assistant", "The same answer text.")]
    memory = _lookup(owner, later, "visualize it")
    assert memory.parent is None and memory.outcome == "none"
    assert first["id"] not in [t.row["id"] for t in memory.turns if t.row]


# ------------------------------------------------------------- walk-back --


def test_send_regenerate_and_edit_each_find_the_right_parent(owner):
    _memory, a1 = _answer(owner, [], "q1", "a1 text")
    path = [("user", "q1"), ("assistant", "a1 text")]
    # Send.
    _memory, a2 = _answer(owner, path, "q2", "a2 text")
    assert a2["parent_sha1"] == key("a1 text") and a2["path_pairs"] == 2
    # Regenerate: the client sends the path up to the question, so the parent is a1 again.
    regen = _lookup(owner, path, "q2")
    assert regen.parent["id"] == a1["id"] and regen.strict
    _memory, a2b = _answer(owner, path, "q2", "a2 regenerated")
    assert (a2b["parent_sha1"], a2b["question_sha1"]) == (a2["parent_sha1"], a2["question_sha1"])
    assert _lookup(owner, [*path, ("user", "q2"), ("assistant", "a2 regenerated")], "q3").parent["id"] == a2b["id"]
    assert _lookup(owner, [*path, ("user", "q2"), ("assistant", "a2 text")], "q3").parent["id"] == a2["id"]
    # Edit: the path before the edited turn, then the edit.
    edited = _lookup(owner, path, "q2 edited")
    assert edited.parent["id"] == a1["id"]


def test_identical_sibling_answers_after_an_edit_are_told_apart_by_their_question(owner):
    path = [("user", "q1"), ("assistant", "a1 text")]
    _answer(owner, [], "q1", "a1 text")
    _memory, original = _answer(owner, path, "q2", "No records match.", plan_line="plan of the original")
    _memory, edited = _answer(owner, path, "q2 edited", "No records match.", plan_line="plan of the edit")
    on_original = _lookup(owner, [*path, ("user", "q2"), ("assistant", "No records match.")], "q3")
    on_edit = _lookup(owner, [*path, ("user", "q2 edited"), ("assistant", "No records match.")], "q3")
    assert on_original.parent["id"] == original["id"] and on_original.parent["matched"] == "question"
    assert on_edit.parent["id"] == edited["id"] and on_edit.parent["matched"] == "question"
    # A question that matches neither falls back to the newest (C1).
    other = _lookup(owner, [*path, ("assistant", "No records match.")], "q3")
    assert other.parent["id"] == edited["id"] and other.parent["matched"] == "newest"


def test_an_answer_with_no_row_in_between_is_a_gap(owner):
    _memory, a1 = _answer(owner, [], "q1", "a1 text")
    path = [("user", "q1"), ("assistant", "a1 text"), ("user", "write me a poem"), ("assistant", OLDER_ROUTE)]
    memory = _lookup(owner, path, "and by month?")
    assert memory.parent["id"] == a1["id"] and memory.gap == 1 and memory.strict is False
    assert memory.outcome == "gap"
    assert memory.arguments()["gap"] == 1 and memory.arguments()["state"] == STATE


@pytest.mark.parametrize(
    ("user", "answer"),
    [
        pytest.param("thanks", "Happy to help.", id="a polite word answered by the older route"),
        pytest.param("show the rest", sfk_bridge._ASK_NOTED_LINE, id="the noted line"),
        pytest.param("show the rest", sfk_bridge._ASK_AGAIN_LINE, id="the ask-again line"),
        *(pytest.param("plot this", reply, id=f"guard reply {n}") for n, reply in enumerate(sf_isolation.FIXED_REPLIES)),
    ],
)
def test_turns_that_read_no_data_are_stepped_over_without_a_gap(owner, user, answer):
    _memory, a1 = _answer(owner, [], "q1", "a1 text")
    path = [("user", "q1"), ("assistant", "a1 text"), ("user", user), ("assistant", answer)]
    memory = _lookup(owner, path, "plot this")
    assert memory.parent["id"] == a1["id"]
    assert memory.gap == 0 and memory.strict is False and memory.outcome == "transparent"


def test_the_retry_line_is_no_transparent_turn():
    assert sfk_bridge._ASK_RETRY_LINE not in sfk_bridge._MEMORY_TRANSPARENT_LINES
    assert sfk_bridge._MEMORY_CARRIED_LINES == (sfk_bridge._ASK_RETRY_LINE,)


@pytest.mark.parametrize(("misses", "found"), [(6, True), (7, False)])
def test_more_than_six_answers_with_no_row_end_the_walk(owner, misses, found):
    _memory, a1 = _answer(owner, [], "q1", "a1 text")
    path = [("user", "q1"), ("assistant", "a1 text")]
    for n in range(misses):
        path += [("user", f"other question {n}"), ("assistant", f"{OLDER_ROUTE} {n}")]
    memory = _lookup(owner, path, "and by month?")
    if found:
        assert memory.parent["id"] == a1["id"] and memory.gap == 6
    else:
        assert memory.parent is None and memory.arguments()["gap"] == 0 and memory.arguments()["state"] is None


def test_another_users_row_and_another_chat_are_never_read(owner):
    _answer(owner, [], "q1", "a1 text")
    other = SimpleNamespace(can=lambda _cap: True, user_id=int(db.create_user("p2-other", "!p2-test")))
    assert _lookup(other, [("user", "q1"), ("assistant", "a1 text")], "q2").parent is None
    request = _request([("user", "q1"), ("assistant", "a1 text")], "q2", conversation_id="mem-elsewhere")
    memory = asyncio.run(sf_memory.lookup(owner, request))
    assert memory.parent is None


# ------------------------------------------------------------ path_pairs --


def test_path_pairs_come_from_the_parent_row_whatever_the_client_sent(owner):
    sf_memory_store.insert(user_id=owner.user_id, conversation_id=CHAT, generation_id="g-parent",
                           answer_sha1=key("a5 text"), parent_sha1=key("a4 text"), question_sha1=key("q5"),
                           contract=2, state=STATE, path_pairs=5, summary="notes", summary_pairs=2)
    # A shortened path: the client sent only the last pairs, but the parent hash is still a4's.
    memory = _lookup(owner, [("user", "q4"), ("assistant", "a4 text"), ("user", "q5"), ("assistant", "a5 text")], "q6")
    assert memory.parent["path_pairs"] == 5
    assert memory.path_pairs() == 6
    record = memory.record({"contract": 2, "state": None, "plan_line": "", "reason": "new", "kind": "knowledge"}, "a6")
    assert (record["summary"], record["summary_pairs"], record["path_pairs"]) == ("notes", 2, 6)


def test_with_no_parent_row_path_pairs_count_the_rows_on_this_path(owner):
    _answer(owner, [], "q1", "a1 text")
    path = [("user", "q1"), ("assistant", "a1 text")]
    for n in range(7):
        path += [("user", f"other question {n}"), ("assistant", f"{OLDER_ROUTE} {n}")]
    memory = _lookup(owner, path, "q9")
    assert memory.parent is None and memory.path_pairs() == 2


# ---------------------------------------------------------------- window --


def _pairs(n: int, *, size: int = 10) -> list:
    return [{"user": f"q{i}", "assistant": f"a{i} " + "x" * size, "salesforce": True, "plan_line": f"plan {i}",
             "kind": "", "clarify": ""} for i in range(n)]


def test_the_window_is_at_most_the_set_pairs_newest_first_and_whole(monkeypatch):
    kept, left_out = sf_memory.window(_pairs(8))
    assert [p["user"] for p in kept] == ["q2", "q3", "q4", "q5", "q6", "q7"]
    assert [p["user"] for p in left_out] == ["q0", "q1"]
    turns = sf_memory.turns_for_service(kept)
    assert len(turns) == 12 and turns[0] == {"role": "user", "content": "q2"}
    assert [t["role"] for t in turns] == ["user", "assistant"] * 6
    monkeypatch.setattr(settings, "sf_assistant_memory_pairs", 3)
    assert [p["user"] for p in sf_memory.window(_pairs(8))[0]] == ["q5", "q6", "q7"]


def test_the_token_budget_drops_whole_pairs_from_the_oldest_end(monkeypatch):
    pairs = _pairs(4, size=2990)  # each answer cut to 2,000 characters: about 670 tokens a pair
    monkeypatch.setattr(settings, "sf_assistant_memory_tokens", 1500)
    kept, left_out = sf_memory.window(pairs)
    assert [p["user"] for p in kept] == ["q2", "q3"] and len(left_out) == 2
    # The first pair that does not fit ends the window: an older, shorter one is not taken instead.
    pairs[0]["assistant"] = "short"
    assert [p["user"] for p in sf_memory.window(pairs)[0]] == ["q2", "q3"]
    assert [p["user"] for p in sf_memory.window(_pairs(8, size=2990))[0]] == ["q6", "q7"]


@pytest.mark.parametrize("tokens", [1, 800], ids=["below one pair", "one pair"])
def test_whatever_the_budget_the_window_keeps_the_whole_pairs_of_the_v1_turns(monkeypatch, tokens):
    """Review F1: never fewer turns than the v1 call sends (the last four), so
    a service in shadow or off reads what it reads with the switch off. They
    are whole pairs: the budget only ever drops pairs beyond them."""
    pairs = _pairs(4, size=2990)
    monkeypatch.setattr(settings, "sf_assistant_memory_tokens", tokens)
    kept, left_out = sf_memory.window(pairs)
    assert [p["user"] for p in kept] == ["q2", "q3"] and left_out == pairs[:2]
    monkeypatch.setattr(settings, "sf_assistant_memory_pairs", 1)
    assert [p["user"] for p in sf_memory.window(pairs)[0]] == ["q2", "q3"]
    # A path shorter than the v1 window is sent whole.
    assert sf_memory.window(pairs[:1]) == (pairs[:1], [])


def test_each_turn_sent_is_cut_and_an_answer_with_no_user_turn_is_one_turn():
    pairs = [{"user": None, "assistant": "greeting " + "y" * 3000}, {"user": "z" * 2500, "assistant": "a"}]
    turns = sf_memory.turns_for_service(pairs)
    assert [t["role"] for t in turns] == ["assistant", "user", "assistant"]
    assert all(len(t["content"]) <= 2000 for t in turns)


def test_a_question_no_answer_followed_is_sent_with_its_pair_and_a_last_one_alone(owner):
    """Review F1: a send that failed or was stopped leaves two user turns in a
    row, or a last user turn with no answer. The v1 call sends them, so the
    window does too, in the pair of the answer after them or as a last pair
    with no answer; the branch hashes are those of the answered turns alone."""
    history = [("user", "how many interviews were there in October 2026?"),
               ("assistant", "There were 9 interviews in October 2026."),
               ("user", "how many placements were there in October 2026?")]
    memory = _lookup(owner, history, "and in November?")
    assert memory.recent_turns() == _messages(history)
    assert memory.pairs()[-1]["assistant"] is None and memory.pairs()[-1]["salesforce"] is False
    alone = _lookup(owner, history[:1], "and in November?")
    assert alone.recent_turns() == _messages(history[:1]) and alone.turns == []
    twice = [history[0], ("user", "made-up retry of it"), *history[1:]]
    memory = _lookup(owner, twice, "and in November?")
    assert memory.recent_turns() == _messages(twice)
    assert memory.turns[0].earlier == [history[0][1]] and memory.turns[0].user == "made-up retry of it"
    assert memory.turns[0].question_sha1 == key("made-up retry of it")
    assert memory.parent_sha1 == key(history[1][1])


def _random_text(rng, n: int) -> str:
    """Made-up text, sometimes long and sometimes not ASCII (more tokens a character)."""
    letters = rng.choice(["abcdefgh ", "ईऔकखगघ ", "Zürich Öl ", "☃\U0001F600 x"])
    return f"t{n} " + "".join(rng.choice(letters) for _ in range(rng.choice([3, 40, 900, 2600])))


def test_whatever_the_path_and_settings_the_window_ends_with_the_v1_calls_turns(owner, monkeypatch):
    """Review F1, the parity the rollout needs (design C31, §15.2 step 4):
    the last four turns of every v2 window are exactly the v1 call's
    `recent_turns`, the window is a run of the newest turns of the path, and
    it holds at most the contract's 12 turns. Paths of any shape: unanswered
    questions, two answers in a row, an answer first, long or non-ASCII text."""
    import random

    rng = random.Random(20261125)
    for n in range(120):
        roles = [rng.choice(["user", "user", "assistant"]) for _ in range(rng.randrange(0, 22))]
        history = [(role, _random_text(rng, i)) for i, role in enumerate(roles)]
        monkeypatch.setattr(settings, "sf_assistant_memory_pairs", rng.randrange(1, 7))
        monkeypatch.setattr(settings, "sf_assistant_memory_tokens", rng.choice([1, 300, 1500, 6000, 20000]))
        request = _request(history, "made-up follow-up")
        memory = asyncio.run(sf_memory.lookup(owner, request))
        v2 = memory.recent_turns()
        v1 = sfk_bridge._recent_turns(request)
        everything = [{"role": t["role"], "content": t["content"][:2000]} for t in request.history_messages]
        assert request.history_messages == _messages(history), "the path is the history as sent"
        assert v2[-4:] == v1, n
        assert len(v2) <= 12 and v2 == everything[len(everything) - len(v2):], n


def test_the_fold_window_is_the_next_calls_window(owner, monkeypatch):
    """The fold counts the window THROUGH the new answer (§11.1): built from
    `next_pairs`, it is exactly what the next call sends, an unanswered
    question before the new answer included (C20: the fold sees what the
    resolver saw)."""
    monkeypatch.setattr(settings, "sf_assistant_memory_pairs", 2)
    path, _rows = _chain(owner, 3)
    path += [("user", "made-up question that failed to send")]
    memory, row = _answer(owner, path, "question 3", "answer 3")
    record = memory.record({"contract": 2, "state": STATE, "plan_line": "plan 3", "reason": "records",
                            "kind": "records"}, "answer 3")
    after = [*path, ("user", "question 3"), ("assistant", "answer 3")]
    following = _lookup(owner, after, "question 4")
    kept, _left_out = sf_memory.window(sf_memory.next_pairs(record, row, "answer 3"))
    assert sf_memory.turns_for_service(kept) == following.recent_turns()
    assert following.recent_turns()[-3:] == _messages(after[-3:])


def test_the_floor_is_the_bridges_v1_window():
    assert sf_memory.V1_TURNS == sfk_bridge._ASK_MAX_TURNS
    assert sf_memory.MAX_TURNS == 12 == 2 * sf_memory.MAX_PAIRS


def test_the_window_settings_are_held_to_the_contract(monkeypatch):
    monkeypatch.setattr(settings, "sf_assistant_memory_pairs", 40)
    monkeypatch.setattr(settings, "sf_assistant_memory_tokens", 0)
    assert sf_memory._limits() == (6, 1)
    monkeypatch.setattr(settings, "sf_assistant_memory_pairs", "6")
    assert sf_memory._limits()[0] == 6


# ------------------------------------------------------------------ fold --


@pytest.fixture()
def folds(monkeypatch):
    monkeypatch.setattr(settings, "sf_assistant_summary", True)
    calls: list = []

    def spawn_fold(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(sf_summary, "spawn_fold", spawn_fold)
    return calls


def _chain(principal, n: int) -> tuple:
    """`n` answered Salesforce turns, each with its row: (path, rows)."""
    path: list = []
    rows: list = []
    for i in range(n):
        _memory, row = _answer(principal, path, f"question {i}", f"answer {i} with Echo Person 7",
                               plan_line=f"plan {i}")
        rows.append(row)
        path += [("user", f"question {i}"), ("assistant", f"answer {i} with Echo Person 7")]
    return path, rows


def test_a_pair_that_leaves_the_window_through_the_new_answer_is_folded_at_once(owner, folds):
    path, rows = _chain(owner, 6)
    assert folds == [], "six pairs fit the window through the sixth answer"
    _memory, row = _answer(owner, path, "question 6", "answer 6 with Echo Person 7", plan_line="plan 6")
    assert row["path_pairs"] == 7
    assert len(folds) == 1
    args, kwargs = folds[0]
    assert args[:4] == (CHAT, row["id"], row["summary"], row["summary_pairs"])
    assert args[4] == [{"question": "question 0", "plan_line": "plan 0", "kind": "", "clarify": ""}]
    assert kwargs == {"user_id": row["user_id"]}
    assert "Echo Person 7" not in json.dumps(args[4]), "no answer text reaches the fold"


def test_only_salesforce_pairs_are_folded_and_counted(owner, folds):
    """An assistant-mode answer of a mixed chat has no row: it takes room in the
    window but is neither counted nor folded (C20)."""
    path, _rows = _chain(owner, 1)
    path += [("user", "write me a poem"), ("assistant", OLDER_ROUTE)]
    for i in range(1, 7):
        _memory, row = _answer(owner, path, f"q{i}", f"a{i} text", plan_line=f"plan {i}")
        path += [("user", f"q{i}"), ("assistant", f"a{i} text")]
    assert row["path_pairs"] == 7, "the poem pair is no Salesforce pair"
    batches = [args[4] for args, _kwargs in folds]
    # q5 is the first answer whose window (poem, q1..q5) leaves the first pair out.
    assert batches == [[{"question": "question 0", "plan_line": "plan 0", "kind": "", "clarify": ""}]] * 2
    assert "write me a poem" not in json.dumps(batches) and OLDER_ROUTE not in json.dumps(batches)


def test_no_fold_with_the_summary_off_or_for_a_row_the_insert_did_not_return(owner, folds, monkeypatch):
    path, _rows = _chain(owner, 6)
    monkeypatch.setattr(settings, "sf_assistant_summary", False)
    _answer(owner, path, "question 6", "answer 6")
    assert folds == []
    monkeypatch.setattr(settings, "sf_assistant_summary", True)
    memory = _lookup(owner, path, "question 6")
    record = memory.record({"contract": 2, "state": STATE, "plan_line": "p", "reason": "records", "kind": "records"},
                           "answer 6 again")
    first = asyncio.run(sf_memory.write(record, user_id=owner.user_id, conversation_id=CHAT,
                                        generation_id="g-same", answer="answer 6 again"))
    assert first is not None and len(folds) == 1
    again = asyncio.run(sf_memory.write(record, user_id=owner.user_id, conversation_id=CHAT,
                                        generation_id="g-same", answer="answer 6 again"))
    assert again is None and len(folds) == 1, "a duplicate generation starts no second fold"


def test_kind_and_clarify_come_only_from_the_new_answers_reply_never_from_a_state(monkeypatch):
    """Open point 14: earlier pairs have no kind or clarify the bridge may read,
    and a stored state is never looked into for them. The new answer's own
    are kept in the record, but its pair is always in the window (the floor),
    so they reach no fold."""
    clarify = "Which ones do you mean?\n1. A\n2. B"
    record = {"question": "which ones?", "kind": "none", "clarify": clarify,
              "pairs": [{"user": f"q{i}", "assistant": f"a{i}", "salesforce": True, "plan_line": f"plan {i}",
                         "kind": "", "clarify": ""} for i in range(3)]}
    row = {"plan_line": "plan 3", "path_pairs": 4, "summary_pairs": 0}
    monkeypatch.setattr(settings, "sf_assistant_memory_tokens", 1)  # only the floor fits: q2 and the new pair
    due = sf_memory.fold_pairs(record, row, clarify)
    assert due == [{"question": "q0", "plan_line": "plan 0", "kind": "", "clarify": ""},
                   {"question": "q1", "plan_line": "plan 1", "kind": "", "clarify": ""}]
    assert clarify not in json.dumps(due)
    memory = sf_memory.Memory(question="q", question_sha1=key("q"), parent_sha1="",
                              turns=[sf_memory.Turn("q0", "a0", key("a0"), "", key("q0"),
                                                    row={"plan_line": "plan 0", "state": STATE})])
    assert memory.pairs()[0]["kind"] == "" and memory.pairs()[0]["clarify"] == ""


# ----------------------------------------------------------------- write --


def test_a_read_that_fails_raises_lookup_failed_carrying_no_text(owner, monkeypatch, caplog):
    """Review N1: a failed read is told apart from "no memory" (the bridge
    then records the trace outcome `error`). It is logged and counted once,
    with codes only, and the exception carries nothing of the row the
    database quoted."""
    import psycopg

    from app import metrics

    real = psycopg.Connection.execute

    def failing(self, query, *args, **kwargs):
        if "sf_query_state" in str(query):
            raise psycopg.errors.CheckViolation("DETAIL: Failing row contains (PLANTED-ROW, a06000000000099AAA)")
        return real(self, query, *args, **kwargs)

    error = (("event", "lookup_error"),)
    before = metrics._counters.get("sf_memory_events_total", {}).get(error, 0.0)
    monkeypatch.setattr(psycopg.Connection, "execute", failing)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(sf_memory.LookupFailed) as caught:
        _lookup(owner, [("user", "q1"), ("assistant", "a1 text")], "q2")
    assert str(caught.value) == "" and caught.value.__cause__ is None and caught.value.__suppress_context__
    assert metrics._counters["sf_memory_events_total"][error] == before + 1
    assert "lookup failed" in caplog.text
    assert "PLANTED" not in caplog.text and "a06000000000099AAA" not in caplog.text


def test_a_write_that_fails_is_logged_and_counted_and_never_raises(owner, monkeypatch, caplog):
    def fails(**_kwargs):
        raise sf_memory_store.StoreError("insert", "sqlstate 23503")

    monkeypatch.setattr(sf_memory_store, "insert", fails)
    memory = _lookup(owner, [], "q1")
    record = memory.record({"contract": 2, "state": {"marker": "PLANTED-STATE"}, "plan_line": "PLANTED-PLAN",
                            "reason": "records", "kind": "records"}, "a1")
    caplog.set_level(logging.DEBUG, logger="app.sf_memory")
    assert asyncio.run(sf_memory.write(record, user_id=owner.user_id, conversation_id=CHAT,
                                       generation_id="g1", answer="a1")) is None
    assert "sqlstate 23503" in caplog.text
    assert "PLANTED" not in caplog.text


def test_a_write_with_memory_off_does_nothing(owner, monkeypatch):
    memory = _lookup(owner, [], "q1")
    record = memory.record({"contract": 2, "state": STATE, "plan_line": "", "reason": "records", "kind": "records"},
                           "a1")
    monkeypatch.setattr(settings, "sf_assistant_memory", False)
    for name in STORE_FUNCTIONS:
        monkeypatch.setattr(sf_memory_store, name, lambda *a, **k: pytest.fail("the store was touched"))
    assert asyncio.run(sf_memory.write(record, user_id=owner.user_id, conversation_id=CHAT,
                                       generation_id="g1", answer="a1")) is None
    assert asyncio.run(sf_memory.lookup(owner, _request([], "q1"))) is None


def _gen(**fields) -> SimpleNamespace:
    base = {"persisted": True, "final_meta": {"engine": "sf_assistant", "route": "sql"}, "user_id": 7,
            "conversation_id": CHAT, "generation_id": "g-hook", "answer": "the stored answer, tail included"}
    return SimpleNamespace(**{**base, **fields})


@pytest.mark.parametrize(
    ("gen", "mode", "memory", "writes"),
    [
        pytest.param(_gen(), "salesforce", {"contract": 2}, True, id="stored, salesforce, sf_assistant"),
        pytest.param(_gen(persisted=False), "salesforce", {"contract": 2}, False, id="not stored"),
        pytest.param(_gen(), "assistant", {"contract": 2}, False, id="assistant mode"),
        pytest.param(_gen(final_meta={"route": "sql"}), "salesforce", {"contract": 2}, False, id="another engine"),
        pytest.param(_gen(final_meta=None), "salesforce", {"contract": 2}, False, id="no meta"),
        pytest.param(_gen(), "salesforce", None, False, id="no memory on the outcome"),
    ],
)
def test_the_write_hook_writes_only_a_stored_salesforce_answer_of_the_bridge(monkeypatch, gen, mode, memory, writes):
    seen: list = []

    async def write(record, **kwargs):
        seen.append((record, kwargs))

    monkeypatch.setattr(sf_memory, "write", write)
    outcome = SimpleNamespace(sf_memory=memory)
    asyncio.run(app_main._write_sf_memory(outcome, gen, SimpleNamespace(mode=mode),
                                          SimpleNamespace(workspace_id="ws-1")))
    assert bool(seen) is writes
    if writes:
        assert seen[0] == (memory, {"user_id": 7, "conversation_id": CHAT, "generation_id": "g-hook",
                                    "answer": "the stored answer, tail included", "workspace_id": "ws-1"})


def test_the_write_hook_never_raises(monkeypatch):
    async def write(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(sf_memory, "write", write)
    asyncio.run(app_main._write_sf_memory(SimpleNamespace(sf_memory={}), _gen(), SimpleNamespace(mode="salesforce"),
                                          None))


def test_main_calls_the_write_hook_right_after_the_answer_is_stored():
    import inspect

    source = inspect.getsource(app_main.chat)
    stored = source.index("await _store_answer(gen)\n")
    hook = source.index("await _write_sf_memory(_sf_memory_outcome, gen, request, principal)")
    completed = source.index('await _mark_chat_request(gen, "completed")', stored)
    assert stored < hook < completed
    assert source.count("_sf_memory_outcome = sf_outcome") == 1
    assigned = source.index("_sf_memory_outcome = sf_outcome")
    branch = source.rindex("elif sf_outcome is not None and sf_outcome.handled:", 0, assigned)
    assert source.index("answer = sf_outcome.answer", branch) < assigned


# -------------------------------------------------------------- truncate --


def _truncate_chat(user_id: int) -> str:
    conversation_id = "mem-truncate"
    db.create_conversation(user_id, conversation_id, "made-up chat")
    for role, content in (("user", "q1"), ("assistant", "a1 text"), ("user", "q2"), ("assistant", "a2 text")):
        db.add_message(user_id, conversation_id, role, content, None)
    for generation, answer in (("g1", "a1 text"), ("g2", "a2 text")):
        sf_memory_store.insert(user_id=user_id, conversation_id=conversation_id, generation_id=generation,
                               answer_sha1=key(answer), contract=2, state=STATE)
    return conversation_id


def test_a_truncate_deletes_only_the_rows_of_answers_it_removed(as_user):
    user_id = int(as_user("local")["id"])
    conversation_id = _truncate_chat(user_id)
    with TestClient(app) as client:
        resp = client.post(f"/history/conversations/{conversation_id}/truncate", json={"keep": 2, "expected_total": 4})
    assert resp.status_code == 200, resp.text
    assert [row["answer_sha1"] for row in _rows()] == [key("a1 text")]


def test_with_memory_off_a_truncate_leaves_the_table_alone(as_user, monkeypatch):
    user_id = int(as_user("local")["id"])
    conversation_id = _truncate_chat(user_id)
    monkeypatch.setattr(settings, "sf_assistant_memory", False)
    monkeypatch.setattr(sf_memory_store, "delete_removed_answers", lambda *a, **k: pytest.fail("store touched"))
    with TestClient(app) as client:
        resp = client.post(f"/history/conversations/{conversation_id}/truncate", json={"keep": 2, "expected_total": 4})
    assert resp.status_code == 200, resp.text
    assert len(_rows()) == 2


def test_a_truncate_whose_memory_trim_fails_still_succeeds(as_user, monkeypatch):
    user_id = int(as_user("local")["id"])
    conversation_id = _truncate_chat(user_id)

    def fails(*_a, **_k):
        raise sf_memory_store.StoreError("truncate", "sqlstate 57014")

    monkeypatch.setattr(sf_memory_store, "delete_removed_answers", fails)
    with TestClient(app) as client:
        resp = client.post(f"/history/conversations/{conversation_id}/truncate", json={"keep": 2, "expected_total": 4})
    assert resp.status_code == 200, resp.text
    assert len(db.list_messages(conversation_id)) == 2


def test_the_truncate_passes_the_guards_own_hash(as_user, monkeypatch):
    user_id = int(as_user("local")["id"])
    conversation_id = _truncate_chat(user_id)
    seen: list = []
    monkeypatch.setattr(sf_memory_store, "delete_removed_answers",
                        lambda uid, conv, *, answer_key: seen.append((uid, conv, answer_key)) or 0)
    with TestClient(app) as client:
        client.post(f"/history/conversations/{conversation_id}/truncate", json={"keep": 2, "expected_total": 4})
    assert seen == [(user_id, conversation_id, sf_isolation.answer_key)]


# ----------------------------------------------------- isolation (§3.6) --


def _imports(path: pathlib.Path) -> set:
    """The app modules `path` imports, by their last name, anywhere in the file."""
    names: set = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.rsplit(".", 1)[-1])
            if node.level or (node.module or "").split(".")[0] == "app":
                names.update(alias.name for alias in node.names)
    return names


def test_only_the_named_modules_import_the_store_or_the_memory():
    store, memory = set(), set()
    for path in APP.rglob("*.py"):
        name = str(path.relative_to(APP))
        imported = _imports(path)
        if "sf_memory_store" in imported and name != "sf_memory_store.py":
            store.add(name)
        if "sf_memory" in imported and name != "sf_memory.py":
            memory.add(name)
    assert store <= {"sf_memory.py", "sf_summary.py", "history.py", "retention.py"}, store
    assert memory == {"engines/sfk_bridge.py", "main.py"}, memory
    for name in ("sf_isolation.py", "engines/chat.py", "compaction.py",
                 *(str(p.relative_to(APP)) for p in APP.glob("memory*.py"))):
        imported = _imports(APP / name)
        assert not {"sf_memory", "sf_memory_store"} & imported, name


@pytest.fixture()
def quiet(monkeypatch):
    """No sidecar, router, extractor or web: only the main stream, faked."""
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(settings, "cross_chat_semantic_enabled", False)
    monkeypatch.setattr(settings, "search_enabled", False)
    captured: list = []

    async def fake_stream(messages, **_kwargs):
        captured.append(json.dumps(list(messages), ensure_ascii=False))
        yield "token", "ok"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    return captured


def test_a_toggle_off_turn_in_a_chat_with_memory_rows_makes_no_memory_call(as_user, quiet, monkeypatch):
    """C19: with memory and the summary on, a toggle-off turn reads no row,
    writes none and starts no fold, and the guard still hides the answer."""
    user_id = int(as_user("local", role="admin")["id"])
    conversation_id = "mem-toggle-off"
    marker = "ZQX7731 Echo Person had 14 placements"
    db.create_conversation(user_id, conversation_id, "made-up chat")
    db.add_message(user_id, conversation_id, "user", "how many placements did Echo Person have?", None)
    db.add_message(user_id, conversation_id, "assistant", marker,
                   {"mode": "salesforce", "route": "sql", "engine": "sf_assistant"})
    sf_memory_store.insert(user_id=user_id, conversation_id=conversation_id, generation_id="g-row",
                           answer_sha1=key(marker), contract=2, state=STATE, plan_line="placements per person")
    monkeypatch.setattr(settings, "sf_assistant_summary", True)
    monkeypatch.setattr(sfk_bridge, "ASSISTANT_ENABLED", True)
    calls: list = []

    def refuse(name):
        def raising(*_a, **_k):
            calls.append(name)
            raise AssertionError(name)
        return raising

    for name in STORE_FUNCTIONS:
        monkeypatch.setattr(sf_memory_store, name, refuse(name))
    for name in ("lookup", "write"):
        monkeypatch.setattr(sf_memory, name, refuse(f"sf_memory.{name}"))
    monkeypatch.setattr(sf_summary, "spawn_fold", refuse("spawn_fold"))
    body = {"messages": [{"role": "user", "content": "how many placements did Echo Person have?"},
                         {"role": "assistant", "content": marker},
                         {"role": "user", "content": "what are good interview questions for an engineer?"}],
            "mode": "assistant", "conversation_id": conversation_id, "effort": "fast", "web_search": "off"}
    with TestClient(app) as client:
        resp = client.post("/chat", json=body)
    assert resp.status_code == 200, resp.text
    assert calls == []
    assert len(quiet) == 1 and "ZQX7731" not in quiet[0]
    assert "placements per person" not in quiet[0]


# ------------------------------------------------------- retention sweep --


def test_the_retention_sweep_is_on_the_maintenance_loop():
    assert "run_chat_retention" in app_main._API_PLATFORM_SWEEPS
    assert asyncio.iscoroutinefunction(app_main.run_chat_retention)


def test_with_the_defaults_the_retention_sweep_touches_no_table(monkeypatch):
    from app import retention

    from app.config import Settings

    fresh = Settings()
    assert fresh.chat_retention_enabled is False and fresh.chat_retention_dry_run is True
    monkeypatch.setattr(settings, "chat_retention_enabled", fresh.chat_retention_enabled)
    monkeypatch.setattr(settings, "chat_retention_dry_run", fresh.chat_retention_dry_run)
    monkeypatch.setattr(retention, "_open", lambda: pytest.fail("the database was opened"))
    assert asyncio.run(app_main.run_chat_retention()) == {"status": "disabled"}


def test_the_sweep_runs_the_chat_retention_not_the_files_apis(monkeypatch):
    from app import retention
    from app.apifiles import retention as files_retention

    seen: list = []

    async def sweep():
        seen.append("chat")
        return {"status": "dry_run", "query_traces_old": 3, "capped": 0, "errors": 0}

    monkeypatch.setattr(retention, "sweep", sweep)
    if hasattr(files_retention, "sweep"):
        monkeypatch.setattr(files_retention, "sweep", lambda *a, **k: pytest.fail("the Files API's retention ran"))
    assert asyncio.run(app_main.run_chat_retention())["query_traces_old"] == 3
    assert seen == ["chat"]


def test_the_sweeps_work_is_judged_by_its_counts_never_by_its_status_word(monkeypatch, caplog):
    from app import retention

    assert app_main._retention_rows({"status": "disabled"}) == 0
    assert app_main._retention_rows({"status": "dry_run", "a": 0, "b": 0, "capped": 1, "errors": 2}) == 0
    assert app_main._retention_rows({"status": "deleted", "a": 4, "b": 1, "capped": 9, "errors": 0}) == 5
    assert app_main._retention_rows({"status": "deleted", "a": 4, "messages_stamped": 3, "errors": 0}) == 4
    results = iter([{"status": "dry_run", "a": 0, "capped": 0, "errors": 0},
                    {"status": "deleted", "a": 4, "capped": 0, "errors": 0}])

    async def sweep():
        return next(results)

    monkeypatch.setattr(retention, "sweep", sweep)
    caplog.set_level(logging.INFO, logger="app.main")
    asyncio.run(app_main.run_chat_retention())
    assert "chat retention" not in caplog.text, "a pass with nothing to do says nothing"
    asyncio.run(app_main.run_chat_retention())
    assert "chat retention: status=deleted rows=4 errors=0 capped=0" in caplog.text


def test_the_retention_sweep_never_raises(monkeypatch):
    from app import retention

    async def sweep():
        raise RuntimeError("boom")

    monkeypatch.setattr(retention, "sweep", sweep)
    assert asyncio.run(app_main.run_chat_retention()) is None

    async def odd():
        return ["not", "a", "dict"]

    monkeypatch.setattr(retention, "sweep", odd)
    assert asyncio.run(app_main.run_chat_retention()) is None


def test_the_tick_runs_every_sweep_in_order(monkeypatch):
    order: list = []
    for name in app_main._API_PLATFORM_SWEEPS:
        async def sweep(name=name):
            order.append(name)
        monkeypatch.setattr(app_main, name, sweep)
    asyncio.run(app_main._api_platform_maintenance_tick())
    assert order == ["run_api_platform_prune", "run_rotated_key_expiry", "run_chat_retention"]
