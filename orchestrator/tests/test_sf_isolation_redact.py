"""Toggle-off isolation: the history rewrite (app/sf_isolation.py, design_v2 G1/G2).

With the Salesforce toggle off, an earlier Salesforce answer, an answer written
from a prompt that held one (tainted), a stored answer with no mode, and a
quote of any of them must not reach the prompt. The state comes from stored
rows; `redact` is pure.
"""
from __future__ import annotations

import pytest

from app import db, sf_isolation as S

SF_ANSWER = (
    "Ravi Kumar joined on 3 March 2025 as a senior recruiter in the Pune office and "
    "closed 14 placements last quarter with Acme Corp and Globex."
)
CLEAN = "Here is a general overview of how recruitment pipelines usually work."
TAINTED = "As the earlier answer showed, Ravi Kumar closed 14 placements; here is a chart of them."


def _row(i, content, *, sf=False, mode="assistant", stamped=False):
    return {"id": i, "content": content, "is_sf": sf, "mode": "salesforce" if sf else mode, "stamped": stamped}


def _state(*rows):
    return S.build_state(rows)


def test_a_salesforce_pair_becomes_two_placeholders_and_keeps_its_roles():
    state = _state(_row(1, CLEAN), _row(3, SF_ANSWER, sf=True))
    history = [
        {"role": "user", "content": "explain pipelines"},
        {"role": "assistant", "content": CLEAN},
        {"role": "user", "content": "who closed the most placements?"},
        {"role": "assistant", "content": SF_ANSWER},
    ]
    out, hidden = S.redact(history, state)
    assert [m["role"] for m in out] == [m["role"] for m in history]
    assert out[0] == history[0] and out[1] == history[1]
    assert out[2]["content"] == S.USER_PLACEHOLDER
    assert out[3]["content"] == S.SF_ANSWER_PLACEHOLDER
    assert hidden.sf == 1 and hidden.users == 1 and hidden.last_answer_hidden
    assert history[3]["content"] == SF_ANSWER, "the input list is not mutated"


def test_a_chat_with_no_salesforce_answer_comes_back_as_the_same_object():
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": CLEAN}]
    out, hidden = S.redact(history, _state(_row(1, CLEAN)))
    assert out is history
    assert hidden.count == 0 and not hidden.last_answer_hidden
    out, _ = S.redact(history, None)
    assert out is history


def test_an_unmatched_assistant_turn_is_hidden_only_in_a_salesforce_holding_chat():
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "an answer nobody stored"}]
    out, _ = S.redact(history, _state(_row(1, CLEAN)))
    assert out is history
    out, hidden = S.redact(history, _state(_row(1, CLEAN), _row(2, SF_ANSWER, sf=True)))
    assert out[1]["content"] == S.TAINTED_PLACEHOLDER
    assert out[0]["content"] == "q", "the user turn before a tainted answer is the person's own"
    assert hidden.tainted == 1 and hidden.last_answer_hidden


def test_whitespace_only_differences_still_match():
    state = _state(_row(1, CLEAN), _row(2, SF_ANSWER, sf=True))
    history = [
        {"role": "assistant", "content": "  " + CLEAN.replace(" ", "\n", 3) + "\n"},
        {"role": "assistant", "content": SF_ANSWER.replace(" ", "   ")},
    ]
    out, hidden = S.redact(history, state)
    assert out[0]["content"] == history[0]["content"]
    assert out[1]["content"] == S.SF_ANSWER_PLACEHOLDER
    assert hidden.sf == 1 and hidden.tainted == 0


def test_last_answer_hidden_follows_the_last_assistant_turn():
    state = _state(_row(1, SF_ANSWER, sf=True), _row(2, CLEAN, stamped=True))
    out, hidden = S.redact(
        [{"role": "assistant", "content": SF_ANSWER}, {"role": "user", "content": "x"}, {"role": "assistant", "content": CLEAN}],
        state,
    )
    assert not hidden.last_answer_hidden
    out, hidden = S.redact([{"role": "assistant", "content": CLEAN}, {"role": "assistant", "content": SF_ANSWER}], state)
    assert hidden.last_answer_hidden


def test_system_blocks_are_untouched():
    state = _state(_row(1, SF_ANSWER, sf=True))
    system = {"role": "system", "content": SF_ANSWER}
    out, _ = S.redact([system, {"role": "assistant", "content": SF_ANSWER}], state)
    assert out[0] == system


def test_rows_of_another_branch_still_match():
    """Every stored row of the chat counts, whatever branch the client shows."""
    branch_a = "Branch A: the pipeline for Globex holds 12 open requisitions in Q3."
    state = _state(_row(5, SF_ANSWER, sf=True), _row(9, branch_a, sf=True), _row(12, CLEAN, stamped=True))
    out, hidden = S.redact([{"role": "assistant", "content": branch_a}], state)
    assert out[0]["content"] == S.SF_ANSWER_PLACEHOLDER and hidden.sf == 1


def test_a_pre_guard_answer_after_the_first_salesforce_answer_is_tainted_unless_stamped():
    """C1: an assistant-mode answer written after a Salesforce answer, before
    the guard, may repeat it. With the guard's stamp it is clean; before the
    first Salesforce answer it is clean."""
    before = "An answer from before any Salesforce question."
    rows = (_row(1, before), _row(3, SF_ANSWER, sf=True), _row(5, TAINTED), _row(7, CLEAN, stamped=True))
    history = [
        {"role": "assistant", "content": before},
        {"role": "user", "content": "plot that"},
        {"role": "assistant", "content": TAINTED},
        {"role": "user", "content": "tell me about pipelines"},
        {"role": "assistant", "content": CLEAN},
    ]
    out, hidden = S.redact(history, _state(*rows))
    assert out[0]["content"] == before
    assert out[1]["content"] == "plot that", "the user turn before a tainted answer is kept"
    assert out[2]["content"] == S.TAINTED_PLACEHOLDER
    assert out[4]["content"] == CLEAN
    assert hidden.tainted == 1 and hidden.sf == 0


def test_a_mode_less_row_in_a_salesforce_holding_chat_is_hidden():
    """C2: a stopped answer has no mode: it is never counted clean."""
    stopped = "Partial answer that stopped before its meta."
    rows = (_row(1, stopped, mode=None), _row(2, SF_ANSWER, sf=True))
    out, hidden = S.redact([{"role": "assistant", "content": stopped}], _state(*rows))
    assert out[0]["content"] == S.TAINTED_PLACEHOLDER and hidden.tainted == 1


def _quote(text: str, follow: str = "what does this mean?", origin: str = "assistant") -> str:
    quoted = "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))
    return f"Selected context from a previous {origin} message:\n\n{quoted}\n\nUser follow-up:\n\n{follow}"


def test_a_selected_context_quote_of_a_salesforce_answer_is_replaced_and_the_follow_up_kept():
    state = _state(_row(1, CLEAN), _row(3, SF_ANSWER, sf=True))
    quoted = _quote("joined on 3 March 2025 as a senior recruiter", "draft an email to him")
    out, hidden = S.redact([{"role": "user", "content": quoted}, {"role": "assistant", "content": CLEAN}], state)
    assert out[0]["content"].startswith(S.QUOTE_PLACEHOLDER)
    assert out[0]["content"].endswith("draft an email to him")
    assert "senior recruiter" not in out[0]["content"]
    assert hidden.quotes_removed == 1
    assert S.quote_check(quoted, state) is True


def test_a_quote_of_an_assistant_mode_answer_is_kept():
    state = _state(_row(1, CLEAN), _row(3, SF_ANSWER, sf=True))
    quoted = _quote("a general overview of how recruitment pipelines usually work")
    out, hidden = S.redact([{"role": "user", "content": quoted}], state)
    assert out[0]["content"] == quoted and hidden.quotes_removed == 0
    assert S.quote_check(quoted, state) is False


def test_rendered_table_text_still_matches():
    table = "| Name | Placements |\n|---|---|\n| Ravi Kumar | 14 |\n| Asha Rao | 9 |"
    state = _state(_row(1, table, sf=True))
    rendered = "Name\tPlacements\nRavi Kumar\t14\nAsha Rao\t9"
    assert S.quote_check(_quote(rendered), state) is True


def test_a_short_quote_counts_when_it_appears_in_a_hidden_answer():
    state = _state(_row(1, "Ravi joined Acme in March.", stamped=True), _row(2, SF_ANSWER, sf=True))
    assert S.quote_check(_quote("Ravi Kumar joined"), state) is True
    assert S.quote_check(_quote("Ravi joined Acme"), state) is False, "clean text is kept"


def test_a_quote_of_neither_is_kept_out_of_a_salesforce_holding_chat():
    """Review F3: an allow-list. The person selects RENDERED text, so a quote
    that matches no clean answer and no question of theirs is removed."""
    state = _state(_row(1, CLEAN), _row(2, SF_ANSWER, sf=True))
    assert S.quote_check(_quote("text that is nowhere in this chat at all, word for word"), state) is True
    assert S.quote_check(_quote("text nowhere"), state) is True


def test_a_quote_that_cannot_be_checked_is_removed_in_a_salesforce_holding_chat():
    failed = S.ChatState(has_salesforce=True, read_failed=True)
    assert S.quote_check(_quote("anything at all"), failed) is True
    assert S.quote_check("no quote here", failed) is False


def test_a_read_failure_that_fails_closed_hides_every_assistant_turn():
    failed = S.ChatState(has_salesforce=True, read_failed=True, announce=True)
    out, hidden = S.redact([{"role": "user", "content": "q"}, {"role": "assistant", "content": CLEAN}], failed)
    assert out[0]["content"] == "q"
    assert out[1]["content"] == S.TAINTED_PLACEHOLDER


def test_the_status_line_is_due_until_a_guarded_answer_follows_the_last_salesforce_answer():
    assert _state(_row(1, CLEAN, stamped=True), _row(2, SF_ANSWER, sf=True)).announce is True
    assert _state(_row(2, SF_ANSWER, sf=True), _row(3, CLEAN, stamped=True)).announce is False
    assert _state(_row(2, SF_ANSWER, sf=True), _row(3, TAINTED)).announce is True


# ---------------------------------------------------------------------------
# The state, read from stored rows (db.salesforce_chat_state)
# ---------------------------------------------------------------------------


@pytest.fixture()
def owner():
    uid = db.create_user("iso-owner", "hash")
    db.create_conversation(uid, "iso-chat", "Mixed chat")
    return uid


def test_the_state_reads_every_signal_and_is_scoped_to_the_owner(owner):
    db.add_message(owner, "iso-chat", "user", "general question")
    db.add_message(owner, "iso-chat", "assistant", CLEAN, {"mode": "assistant"})
    db.add_message(owner, "iso-chat", "user", "sf question")
    db.add_message(owner, "iso-chat", "assistant", SF_ANSWER, {"mode": "salesforce"})
    db.add_message(owner, "iso-chat", "assistant", "engine-only stamp", {"engine": "sf_assistant"})
    db.add_message(owner, "iso-chat", "assistant", "scope-only key", {"salesforce_scope": {"x": 1}})
    db.add_message(owner, "iso-chat", "assistant", "a guarded answer", {"mode": "assistant", "sf_isolation": 1})
    rows = db.salesforce_chat_state(owner, "iso-chat")
    by_content = {r["content"]: r for r in rows}
    assert by_content[SF_ANSWER]["is_sf"] and by_content["engine-only stamp"]["is_sf"]
    assert by_content["scope-only key"]["is_sf"]
    assert not by_content[CLEAN]["is_sf"] and by_content[CLEAN]["mode"] == "assistant"
    assert by_content["a guarded answer"]["stamped"]
    assert by_content["general question"]["role"] == "user" and not by_content["general question"]["is_sf_question"]
    assert by_content["sf question"]["is_sf_question"], "the question right before a Salesforce answer"
    assert all(r["holding"] for r in rows)
    other = db.create_user("iso-other", "hash")
    assert db.salesforce_chat_state(other, "iso-chat") == []


def test_a_chat_with_no_salesforce_answer_returns_no_rows(owner):
    db.add_message(owner, "iso-chat", "assistant", CLEAN, {"mode": "assistant"})
    assert db.salesforce_chat_state(owner, "iso-chat") == []


def test_a_row_whose_generation_asked_for_salesforce_counts_without_a_mode(owner):
    """C2: the server-written request decides for an answer stopped before its meta."""
    db.create_chat_request("int-sf", owner, "iso-chat", "gen-sf", {"message": "q", "mode": "salesforce"})
    db.add_message(owner, "iso-chat", "assistant", "stopped half answer", {"generation_id": "gen-sf"})
    db.create_chat_request("int-as", owner, "iso-chat", "gen-as", {"message": "q", "mode": "assistant"})
    db.add_message(owner, "iso-chat", "assistant", "assistant half answer", {"generation_id": "gen-as"})
    rows = {r["content"]: r for r in db.salesforce_chat_state(owner, "iso-chat")}
    assert rows["stopped half answer"]["is_sf"] is True
    assert rows["assistant half answer"]["is_sf"] is False


def test_a_row_whose_trace_asked_for_salesforce_counts_without_a_mode(owner):
    # A trace's id IS its generation's id (main.py _QueuedTraceRecorder).
    db.start_query_trace("trace-sf", "iso-chat", owner, "", "q", "salesforce")
    db.add_message(owner, "iso-chat", "assistant", "traced answer", {"generation_id": "trace-sf", "trace_id": "trace-sf"})
    rows = {r["content"]: r for r in db.salesforce_chat_state(owner, "iso-chat")}
    assert rows["traced answer"]["is_sf"] is True


def test_load_state_fails_closed_only_for_a_principal_with_salesforce(monkeypatch):
    import asyncio

    def broken(*_a, **_k):
        raise RuntimeError("database down")

    monkeypatch.setattr(db, "salesforce_chat_state", broken)
    closed = asyncio.run(S.load_state(1, "x", fail_closed=True))
    opened = asyncio.run(S.load_state(1, "x", fail_closed=False))
    assert closed.has_salesforce and closed.read_failed and closed.announce
    assert not opened.has_salesforce and opened.read_failed
