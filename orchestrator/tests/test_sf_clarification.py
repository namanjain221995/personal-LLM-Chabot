"""Asking once, remembering the answer, and resuming the SAME request.

The failure this whole feature exists to prevent is the loop: a question is
asked, the user answers, the answer is read as a brand-new question, and the
same card comes back. It happened three times in a row with the old
deterministic detector (see test_clarify.py). So most of what is tested here is
the machinery that makes a second identical question IMPOSSIBLE — the round
budget, the semantic fingerprint, the one-pending-per-conversation index, and
first-response-wins.
"""
import asyncio

import pytest

from app import db
from app.core.sf_intel import resume, state as sf_state
from app.core.sf_intel.models import (
    ClarificationDraft,
    ClarificationOption,
    ClarificationResponse,
    ConversationSalesforceState,
    fingerprint,
)

CONV = "conv-clarify"


def run(coro):
    return asyncio.run(coro)


def _draft(slot="date_range", question="Which period should I use?", n=3):
    labels = ["This month", "This quarter", "This year", "All open"]
    return ClarificationDraft(
        slot=slot,
        question=question,
        options=[
            ClarificationOption(id=f"o{i}", label=labels[i], value=labels[i].upper())
            for i in range(n)
        ],
    )


def _intent(text="show my pipeline", carried=None):
    intent = sf_state.new_intent(
        CONV, text=text, root_user_message_id="m1", carried_slots=carried
    )
    run(sf_state.save_intent(intent))
    return intent


# ── Asking ───────────────────────────────────────────────────────────────────

def test_a_clarification_is_persisted_and_can_be_restored_after_a_reload():
    """Nothing about a pending card lives in the generation buffer, which is
    per-process and dies with the answer it was streaming."""
    intent = _intent()
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    assert request is not None
    assert request.state == "pending"
    assert request.resume_token

    restored = run(sf_state.get_pending(CONV))
    assert restored is not None
    assert restored.clarification_id == request.clarification_id
    assert restored.question == request.question


def test_the_request_carries_everything_needed_to_resume_it():
    intent = _intent()
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    wire = request.wire()
    for key in (
        "clarification_id", "conversation_id", "run_id", "root_user_message_id",
        "intent_id", "slot", "options", "allow_custom", "round_number",
        "resume_token", "question_fingerprint", "state",
    ):
        assert key in wire, key


def test_only_one_question_can_be_pending_per_conversation():
    """Enforced by a partial unique index, so two racing sends cannot both
    insert and leave the user looking at two cards for one request."""
    first = _intent()
    assert run(sf_state.open_clarification(first, _draft(), run_id="r1")) is not None
    second = _intent("something else")
    assert run(sf_state.open_clarification(second, _draft(slot="region",
                                                          question="Which region?"),
                                           run_id="r2")) is None


def test_the_same_question_is_never_asked_twice_in_one_request():
    intent = _intent()
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    run(sf_state.apply_response(_answer(request, "o1")))
    # Same slot, different WORDING — the fingerprint is semantic, so this is
    # still recognised as the question we already asked.
    again = run(
        sf_state.open_clarification(
            intent, _draft(question="What time range do you want?"), run_id="r2"
        )
    )
    assert again is None


def test_the_fingerprint_ignores_wording_but_not_meaning():
    a = fingerprint("date_range", "Which period should I use?")
    b = fingerprint("date_range", "which period, should I use?")
    c = fingerprint("date_range", "Which owner should I use?")
    d = fingerprint("region", "Which period should I use?")
    assert a == b
    assert a != c
    assert a != d


def test_the_round_budget_stops_at_two(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "salesforce_max_clarification_rounds", 2)
    intent = _intent()

    first = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    run(sf_state.apply_response(_answer(first, "o1")))
    second = run(
        sf_state.open_clarification(
            intent, _draft(slot="region", question="Which region?"), run_id="r2"
        )
    )
    assert second is not None
    run(sf_state.apply_response(_answer(second, "o1")))

    third = run(
        sf_state.open_clarification(
            intent, _draft(slot="status", question="Which status?"), run_id="r3"
        )
    )
    assert third is None, "a third round is an interrogation, not help"


def test_a_slot_already_asked_about_is_not_asked_about_again():
    intent = _intent()
    first = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    run(sf_state.apply_response(_answer(first, "o1")))
    assert run(
        sf_state.open_clarification(
            intent,
            _draft(question="Actually, which dates exactly?"),
            run_id="r2",
        )
    ) is None


# ── Answering ────────────────────────────────────────────────────────────────

def _answer(request, option_id=None, custom="", skipped=False, token=None):
    return ClarificationResponse(
        clarification_id=request.clarification_id,
        conversation_id=request.conversation_id,
        client_message_id="click-1",
        selected_option_ids=[option_id] if option_id else [],
        custom_text=custom,
        skipped=skipped,
        resume_token=token if token is not None else request.resume_token,
    )


def test_choosing_an_option_resolves_the_slot_on_the_ORIGINAL_intent():
    intent = _intent("show my pipeline")
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    _req, resumed, value, already = run(sf_state.apply_response(_answer(request, "o1")))
    assert already is False
    assert value == "THIS QUARTER"
    assert resumed.resolved_slots["date_range"] == "THIS QUARTER"
    # The original request survives verbatim; the answer NARROWS it.
    assert resumed.original_user_text == "show my pipeline"
    assert "show my pipeline" in resumed.resolved_text()
    assert "THIS QUARTER" in resumed.resolved_text()


def test_custom_text_resolves_the_pending_slot():
    intent = _intent()
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    _req, resumed, value, _ = run(
        sf_state.apply_response(_answer(request, custom="last 90 days"))
    )
    assert value == "last 90 days"
    assert resumed.resolved_slots["date_range"] == "last 90 days"


def test_skipping_records_the_round_without_inventing_a_value():
    intent = _intent()
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    _req, resumed, value, _ = run(sf_state.apply_response(_answer(request, skipped=True)))
    assert value == ""
    assert "date_range" not in resumed.resolved_slots
    assert resumed.clarification_history[0].skipped is True


def test_a_duplicate_submission_returns_the_FIRST_answer():
    """A double-click, or a fetch retried after a timeout. The second one must
    not start a second generation, and must not change the recorded answer."""
    intent = _intent()
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    _r1, _i1, first_value, first_already = run(
        sf_state.apply_response(_answer(request, "o1"))
    )
    _r2, _i2, second_value, second_already = run(
        sf_state.apply_response(_answer(request, "o2"))
    )
    assert first_already is False
    assert second_already is True
    assert second_value == first_value, "the second click did not change what they said"


def test_an_unknown_clarification_id_is_rejected():
    with pytest.raises(sf_state.ClarificationRejected, match="no such clarification"):
        run(
            sf_state.apply_response(
                ClarificationResponse(
                    clarification_id="clr_nope",
                    conversation_id=CONV,
                    custom_text="whatever",
                )
            )
        )


def test_a_stale_resume_token_is_rejected():
    intent = _intent()
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    with pytest.raises(sf_state.ClarificationRejected, match="stale"):
        run(sf_state.apply_response(_answer(request, "o1", token="not-the-token")))


def test_an_option_that_was_never_offered_is_rejected():
    intent = _intent()
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    with pytest.raises(sf_state.ClarificationRejected, match="never offered"):
        run(sf_state.apply_response(_answer(request, "o99")))


def test_another_conversations_clarification_cannot_be_answered():
    intent = _intent()
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    response = _answer(request, "o1")
    response.conversation_id = "someone-elses-chat"
    with pytest.raises(sf_state.ClarificationRejected, match="another conversation"):
        run(sf_state.apply_response(response))


def test_a_cancelled_question_cannot_be_answered_later():
    intent = _intent()
    request = run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    run(sf_state.cancel_pending(CONV))
    with pytest.raises(sf_state.ClarificationRejected, match="cancelled"):
        run(sf_state.apply_response(_answer(request, "o1")))


def test_a_response_saying_nothing_is_refused_by_the_contract():
    with pytest.raises(ValueError, match="must select an option"):
        ClarificationResponse(clarification_id="clr_x", conversation_id=CONV)


def test_cancelling_clears_the_pending_question_and_its_intent():
    intent = _intent()
    run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    assert run(sf_state.cancel_pending(CONV)) == 1
    assert run(sf_state.get_pending(CONV)) is None
    assert db.latest_open_sf_intent(CONV) is None


def test_deleting_a_conversation_takes_its_pending_question_with_it(as_user):
    """An orphan would block the id from ever being reused: the partial unique
    index allows exactly one pending row per conversation."""
    user = as_user("owner")
    db.create_conversation(int(user["id"]), CONV, "Pipeline")
    intent = _intent()
    run(sf_state.open_clarification(intent, _draft(), run_id="r1"))
    assert db.delete_conversation(int(user["id"]), CONV) is True
    assert db.pending_sf_clarification(CONV) is None
    assert db.get_sf_conversation_state(CONV) in (None, {})


# ── Typed answers vs new topics ──────────────────────────────────────────────

def _pending():
    intent = _intent()
    return run(sf_state.open_clarification(intent, _draft(), run_id="r1"))


def test_typing_an_option_label_is_read_as_choosing_it():
    request = _pending()
    verdict = resume.classify_deterministic(request, "This quarter")
    assert verdict.verdict == "answers_pending"
    assert verdict.option_id == "o1"


def test_typing_the_option_number_is_read_as_choosing_it():
    request = _pending()
    verdict = resume.classify_deterministic(request, "2")
    assert verdict.verdict == "answers_pending"
    assert verdict.option_id == "o1"


def test_a_slot_shaped_phrase_is_read_as_the_answer():
    """"Last 90 days" is an answer to "which period?" whatever else it is."""
    request = _pending()
    verdict = resume.classify_deterministic(request, "last 90 days")
    assert verdict.verdict == "answers_pending"
    assert verdict.value == "last 90 days"


def test_an_explicit_pass_is_a_skip_not_a_value():
    request = _pending()
    verdict = resume.classify_deterministic(request, "doesn't matter")
    assert verdict.skipped is True
    assert verdict.value == ""


def test_an_announced_change_of_subject_is_a_new_topic():
    request = _pending()
    verdict = resume.classify_deterministic(
        request, "never mind, show me open cases instead"
    )
    assert verdict.verdict == "new_topic"


def test_a_long_unrelated_question_is_a_new_topic():
    request = _pending()
    verdict = resume.classify_deterministic(
        request,
        "Which accounts in the manufacturing sector have had no activity at all "
        "for the past six months, and who owns each of them right now please?",
    )
    assert verdict.verdict == "new_topic"


def test_an_ambiguous_reply_is_left_to_the_model():
    """None here means "ask the classifier" — the deterministic layer is only
    allowed to decide the cases it is actually sure about."""
    request = _pending()
    assert resume.classify_deterministic(request, "the usual one") is None


def test_the_classifier_falls_back_to_answering_when_the_model_is_unreachable(
    monkeypatch,
):
    """Mistaking an answer for a new topic DESTROYS the original request;
    mistaking a new topic for an answer costs one visible turn."""
    from app import llm

    async def boom(*a, **k):
        raise RuntimeError("no model server")

    monkeypatch.setattr(llm, "json_completion", boom)
    request = _pending()
    verdict = run(resume.classify(request, "the usual one"))
    assert verdict.verdict == "answers_pending"


# ── Conversation state: what makes "what about EMEA?" work ───────────────────

def test_completing_an_intent_records_what_it_established():
    intent = _intent("open opportunities I own closing this quarter in North America")
    intent.resolved_slots = {
        "object": "Opportunity",
        "metric": "count",
        "date_range": "THIS_QUARTER",
        "owner_scope": "me",
        "region": "North America",
        "status": "open",
    }
    state = ConversationSalesforceState(conversation_id=CONV)
    run(
        sf_state.complete_intent(
            intent,
            state,
            objects=["Opportunity"],
            query_summary="open opps I own closing this quarter in NA",
            result_metadata={"record_count": 42},
        )
    )
    stored = run(sf_state.load_state(CONV))
    assert stored.last_salesforce_objects == ["Opportunity"]
    assert stored.last_date_range == "THIS_QUARTER"
    assert stored.last_owner_scope == "me"
    assert stored.last_filters["region"] == "North America"
    assert stored.last_result_metadata["record_count"] == 42


def test_a_follow_up_inherits_everything_except_what_it_changes():
    """"What about EMEA?" — object, status, owner scope and period all carry
    forward; only the region is new."""
    state = ConversationSalesforceState(
        conversation_id=CONV,
        last_salesforce_objects=["Opportunity"],
        last_metric="count",
        last_date_range="THIS_QUARTER",
        last_owner_scope="me",
        last_filters={"region": "North America", "status": "open"},
    )
    carried = state.carried_slots()
    assert carried["object"] == "Opportunity"
    assert carried["date_range"] == "THIS_QUARTER"
    assert carried["owner_scope"] == "me"
    assert carried["status"] == "open"

    follow_up = sf_state.new_intent(
        CONV, text="what about EMEA?", root_user_message_id="m2", carried_slots=carried
    )
    follow_up.resolved_slots["region"] = "EMEA"
    resolved = follow_up.resolved_text()
    assert "what about EMEA?" in resolved
    assert "EMEA" in resolved
    assert "THIS_QUARTER" in resolved
    assert "Opportunity" in resolved


def test_the_state_brief_stays_small_and_carries_no_record_payloads():
    state = ConversationSalesforceState(
        conversation_id=CONV,
        last_salesforce_objects=["Opportunity"],
        last_date_range="THIS_QUARTER",
        last_query_summary="open pipeline",
        last_result_metadata={"record_count": 42},
    )
    brief = state.brief()
    assert "Opportunity" in brief and "THIS_QUARTER" in brief
    assert len(brief) < 800, "the brief is a summary, not a transcript"


def test_an_unreadable_stored_state_degrades_instead_of_failing_the_request():
    db.save_sf_conversation_state(CONV, {"last_filters": "not-a-dict"})
    state = run(sf_state.load_state(CONV))
    assert state.conversation_id == CONV
    assert state.last_filters == {}


def test_an_answer_the_user_gave_is_never_overwritten_by_the_planner():
    """A slot the USER answered is a fact. A planner that re-derives it
    differently on the next turn must not silently change what they said."""
    from app.core.sf_intel.models import AgentDecision, ClarificationRound

    intent = _intent()
    intent.resolved_slots["date_range"] = "THIS_QUARTER"
    intent.clarification_history.append(
        ClarificationRound(
            clarification_id="clr_1",
            slot="date_range",
            question="Which period?",
            question_fingerprint="f",
            answer="THIS_QUARTER",
            answered=True,
        )
    )
    decision = AgentDecision(
        action="EXECUTE_SALESFORCE",
        resolved_slots={"date_range": "THIS_YEAR", "region": "EMEA"},
    )
    sf_state.decision_slots(decision, intent)
    assert intent.resolved_slots["date_range"] == "THIS_QUARTER"
    assert intent.resolved_slots["region"] == "EMEA"


# ── Several answers to one question ──────────────────────────────────────────
# Owner request 2026-08-11. Asked which object holds payment AND invoice data,
# "Invoice__c" and "Payment__c" is the honest answer — a single-answer card
# forced a choice between two things the user needed together.

def test_most_questions_accept_several_answers():
    intent = _intent()
    request = run(
        sf_state.open_clarification(
            intent, _draft(slot="object", question="Which object?"), run_id="r1"
        )
    )
    assert request.multi_select is True


def test_a_genuinely_exclusive_slot_stays_single_answer():
    """A result is presented ONE way; "a chart and a table and a count" is not
    an answer to "how should I show this?"."""
    intent = _intent()
    request = run(
        sf_state.open_clarification(
            intent,
            _draft(slot="result_format", question="How should I present it?"),
            run_id="r1",
        )
    )
    assert request.multi_select is False
    assert "result_format" in sf_state.EXCLUSIVE_SLOTS


def test_multi_select_can_be_switched_off_wholesale(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "salesforce_multi_select_clarification", False)
    intent = _intent()
    request = run(
        sf_state.open_clarification(
            intent, _draft(slot="object", question="Which object?"), run_id="r1"
        )
    )
    assert request.multi_select is False


def test_several_options_resolve_the_slot_to_all_of_them():
    intent = _intent()
    request = run(
        sf_state.open_clarification(
            intent, _draft(slot="object", question="Which object?"), run_id="r1"
        )
    )
    response = ClarificationResponse(
        clarification_id=request.clarification_id,
        conversation_id=CONV,
        client_message_id="click-1",
        selected_option_ids=["o0", "o2"],
        resume_token=request.resume_token,
    )
    _req, resumed, value, _already = run(sf_state.apply_response(response))
    assert value == "THIS MONTH, THIS YEAR"
    assert resumed.resolved_slots["object"] == "THIS MONTH, THIS YEAR"


def test_ticked_options_and_typed_text_are_COMBINED_not_one_or_the_other():
    """Dropping the typed half because boxes were ticked silently discards the
    more specific half of what the user said."""
    intent = _intent()
    request = run(
        sf_state.open_clarification(
            intent, _draft(slot="object", question="Which object?"), run_id="r1"
        )
    )
    response = ClarificationResponse(
        clarification_id=request.clarification_id,
        conversation_id=CONV,
        client_message_id="click-1",
        selected_option_ids=["o0"],
        custom_text="and anything linked to a renewal",
        resume_token=request.resume_token,
    )
    _req, _resumed, value, _already = run(sf_state.apply_response(response))
    assert value == "THIS MONTH, and anything linked to a renewal"


def test_a_single_answer_card_still_refuses_two_answers():
    intent = _intent()
    request = run(
        sf_state.open_clarification(
            intent,
            _draft(slot="result_format", question="How should I present it?"),
            run_id="r1",
        )
    )
    response = ClarificationResponse(
        clarification_id=request.clarification_id,
        conversation_id=CONV,
        selected_option_ids=["o0", "o1"],
        resume_token=request.resume_token,
    )
    with pytest.raises(sf_state.ClarificationRejected, match="single answer"):
        run(sf_state.apply_response(response))
