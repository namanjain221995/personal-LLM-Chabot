"""The engine end to end, with the model and Salesforce replaced by doubles.

No credentials are available in this suite, so the planner's model call and the
org itself are stubbed — but everything BETWEEN them is the real code: the
prompt assembly, the decision validation, the policy that downgrades a repeated
question, the compiler, the deterministic calculation, and the shape of the SSE
events and the final meta.

What is deliberately asserted here rather than in a unit test: the things that
are only wrong in combination. A tool failure reported as "no records", a
reasoning block reaching the transcript, or a status phase claiming work that
never happened are all invisible until the pieces run together.
"""
import asyncio
import json

import pytest

from app import llm
from app.config import settings
from app.core.sf_intel import budget, phases, planner, state as sf_state, tools
from app.core.sf_intel.models import (
    AgentDecision,
    ClarificationDraft,
    ClarificationOption,
    ClarificationResponse,
    ConversationSalesforceState,
    PendingIntent,
)
from app.engines import sf_intel

CONV = "conv-engine"


def run(coro):
    return asyncio.run(coro)


class Recorder:
    """Collects the SSE events an engine emits."""

    def __init__(self):
        self.events = []

    async def emit(self, event, data):
        self.events.append((event, dict(data)))

    def kinds(self):
        return [e for e, _ in self.events]

    def text(self):
        return "".join(d["text"] for e, d in self.events if e == "token")

    def meta(self):
        metas = [d for e, d in self.events if e == "meta"]
        return metas[-1] if metas else {}

    def phases(self):
        return [d.get("phase") for e, d in self.events if e == "status"]


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """No network, no describe cache carried between tests, no live org."""
    tools.clear_caches()
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", True)
    monkeypatch.setattr(settings, "salesforce_contextual_clarification_enabled", True)
    monkeypatch.setattr(settings, "salesforce_planner_tool_calling", False)
    monkeypatch.setattr(settings, "sf_live_enabled", False)
    yield
    tools.clear_caches()


def stub_planner(monkeypatch, decision: AgentDecision):
    async def fake(*_a, **_k):
        return decision

    monkeypatch.setattr(planner, "plan", fake)


def stub_schema(monkeypatch, summary="Opportunity — fields: Id, Name"):
    async def fake(*_a, **_k):
        return summary, ["Opportunity"]

    monkeypatch.setattr(tools, "get_salesforce_schema", fake)


# ── Routing ──────────────────────────────────────────────────────────────────

def test_a_greeting_never_reaches_the_planner_or_claims_salesforce_work(monkeypatch):
    """"Checking Salesforce fields" shown under "hey there" is a fabricated
    step — the labels are only honest if they describe work that happened."""
    called = []

    async def fake(*_a, **_k):
        called.append(1)
        return AgentDecision(action="EXECUTE_SALESFORCE")

    monkeypatch.setattr(planner, "plan", fake)
    rec = Recorder()
    outcome = run(sf_intel.run("hey there", [], rec.emit, conversation_id=CONV))
    assert outcome.handled is False
    assert called == []
    assert rec.events == []


@pytest.mark.parametrize(
    "text", ["hi", "thanks!", "what can you do", "ok", "good morning"]
)
def test_conversational_turns_are_recognised(text):
    assert sf_intel.is_conversational(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "show my pipeline",
        "hi, how many opportunities closed last quarter?",
        "thanks — now show me the cases",
        "what can you do about the EMEA pipeline this quarter",
    ],
)
def test_a_real_request_is_never_mistaken_for_small_talk(text):
    assert sf_intel.is_conversational(text) is False


def test_a_clear_request_goes_straight_to_execution_with_no_question(monkeypatch):
    stub_schema(monkeypatch)
    stub_planner(
        monkeypatch,
        AgentDecision(
            action="EXECUTE_SALESFORCE",
            normalized_intent="open opportunities I own closing this quarter",
            resolved_slots={"object": "Opportunity", "status": "open"},
        ),
    )
    rec = Recorder()
    outcome = run(
        sf_intel.run(
            "Show open opportunities I own that close this quarter",
            [],
            rec.emit,
            conversation_id=CONV,
        )
    )
    assert outcome.handled is False, "no live org here — the warehouse engine answers"
    assert run(sf_state.get_pending(CONV)) is None, "nothing was asked"
    assert "clarifying" not in rec.phases()


def test_a_general_question_is_handed_back_untouched(monkeypatch):
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, AgentDecision(action="ANSWER_GENERAL"))
    rec = Recorder()
    outcome = run(
        sf_intel.run("what is a good discovery question?", [], rec.emit,
                     conversation_id=CONV)
    )
    assert outcome.handled is False
    assert rec.text() == ""


def test_an_unsupported_request_says_so_instead_of_guessing(monkeypatch):
    stub_schema(monkeypatch)
    stub_planner(
        monkeypatch,
        AgentDecision(action="DENY", internal_reason_code="write_requested"),
    )
    rec = Recorder()
    outcome = run(
        sf_intel.run("delete every closed lost opportunity", [], rec.emit,
                     conversation_id=CONV)
    )
    assert outcome.handled is True
    assert "does not change records" in outcome.answer
    assert "write_requested" not in rec.text(), "the reason code is internal"
    assert "write_requested" not in json.dumps(rec.meta())


# ── Asking ───────────────────────────────────────────────────────────────────

def _ask_decision(slot="date_range", question="Which period should I use?"):
    return AgentDecision(
        action="ASK_CLARIFICATION",
        normalized_intent="pipeline",
        missing_critical_slots=[slot],
        clarification_draft=ClarificationDraft(
            slot=slot,
            question=question,
            options=[
                ClarificationOption(id="m", label="This month", value="THIS_MONTH"),
                ClarificationOption(id="q", label="This quarter", value="THIS_QUARTER"),
                ClarificationOption(id="y", label="This year", value="THIS_YEAR"),
            ],
        ),
    )


def test_an_ambiguous_request_produces_a_validated_clarification(monkeypatch):
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _ask_decision())
    rec = Recorder()
    outcome = run(sf_intel.run("Show my pipeline", [], rec.emit, conversation_id=CONV))

    assert outcome.handled is True
    assert outcome.clarification is not None
    meta = rec.meta()
    assert meta["route"] == "clarify"
    card = meta["clarification"]
    assert card["question"] == "Which period should I use?"
    assert len(card["options"]) == 3
    assert card["allow_custom"] is True
    assert card["resume_token"]
    assert "clarifying" in rec.phases()
    # The plain-text form matters: it is what history stores and what a client
    # with no card renderer shows.
    assert "**1.**" in outcome.answer


def test_the_card_is_still_there_after_a_reload(monkeypatch):
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _ask_decision())
    run(sf_intel.run("Show my pipeline", [], Recorder().emit, conversation_id=CONV))

    restored = run(sf_intel.starter_options(CONV))
    assert restored["pending_clarification"] is not None
    assert (
        restored["pending_clarification"]["question"] == "Which period should I use?"
    )


def test_answering_resumes_the_original_request_rather_than_starting_a_new_one(
    monkeypatch,
):
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _ask_decision())
    first = Recorder()
    run(sf_intel.run("Show my pipeline", [], first.emit, conversation_id=CONV))
    card = first.meta()["clarification"]

    # The second turn: EXECUTE now that the slot is filled.
    stub_planner(
        monkeypatch,
        AgentDecision(action="EXECUTE_SALESFORCE", resolved_slots={}),
    )
    second = Recorder()
    outcome = run(
        sf_intel.run(
            "This quarter",
            [],
            second.emit,
            conversation_id=CONV,
            clarification_response=ClarificationResponse(
                clarification_id=card["clarification_id"],
                conversation_id=CONV,
                client_message_id="click-1",
                selected_option_ids=["q"],
                resume_token=card["resume_token"],
            ),
        )
    )
    # The ORIGINAL question is what runs, narrowed by the answer.
    assert "Show my pipeline" in outcome.resolved_text
    assert "THIS_QUARTER" in outcome.resolved_text
    assert run(sf_state.get_pending(CONV)) is None


def test_typing_the_answer_into_the_composer_also_resumes(monkeypatch):
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _ask_decision())
    run(sf_intel.run("Show my pipeline", [], Recorder().emit, conversation_id=CONV))

    stub_planner(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"))
    rec = Recorder()
    outcome = run(
        sf_intel.run("last 90 days", [], rec.emit, conversation_id=CONV)
    )
    assert "Show my pipeline" in outcome.resolved_text
    assert "last 90 days" in outcome.resolved_text


def test_a_clear_new_topic_cancels_the_pending_question(monkeypatch):
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _ask_decision())
    run(sf_intel.run("Show my pipeline", [], Recorder().emit, conversation_id=CONV))

    stub_planner(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"))
    rec = Recorder()
    outcome = run(
        sf_intel.run(
            "never mind, show me open cases instead", [], rec.emit,
            conversation_id=CONV,
        )
    )
    assert run(sf_state.get_pending(CONV)) is None
    assert "Show my pipeline" not in outcome.resolved_text


def test_the_same_answer_sent_twice_still_resumes_the_original_request(
    monkeypatch,
):
    """The duplicate case, THROUGH THE ENGINE. By the second submission nothing
    is pending any more, so an engine that only looked at `get_pending` treated
    "Interviews" as a brand-new question and asked all over again — the exact
    loop this feature exists to prevent. Found on a live run, 2026-08-11."""
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _ask_decision())
    first = Recorder()
    run(sf_intel.run("Show my pipeline", [], first.emit, conversation_id=CONV))
    card = first.meta()["clarification"]

    answer = ClarificationResponse(
        clarification_id=card["clarification_id"],
        conversation_id=CONV,
        client_message_id="click-1",
        selected_option_ids=["q"],
        resume_token=card["resume_token"],
    )
    stub_planner(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"))
    run(sf_intel.run("This quarter", [], Recorder().emit, conversation_id=CONV,
                     clarification_response=answer))
    assert run(sf_state.get_pending(CONV)) is None

    second = Recorder()
    outcome = run(
        sf_intel.run("This quarter", [], second.emit, conversation_id=CONV,
                     clarification_response=answer)
    )
    assert "Show my pipeline" in outcome.resolved_text
    assert "THIS_QUARTER" in outcome.resolved_text
    assert "clarification" not in second.meta(), "it must not ask again"


def test_a_double_click_cannot_produce_a_NEW_question(monkeypatch):
    """Clicking twice is not a second turn. Re-planning the resumed request can
    decide another detail is missing and ask again — seen live on 2026-08-11,
    where repeating "Tasks" was met with "what status counts as pending?"."""
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _ask_decision())
    first = Recorder()
    run(sf_intel.run("Give me all pending items", [], first.emit, conversation_id=CONV))
    card = first.meta()["clarification"]

    answer = ClarificationResponse(
        clarification_id=card["clarification_id"],
        conversation_id=CONV,
        client_message_id="click-1",
        selected_option_ids=["q"],
        resume_token=card["resume_token"],
    )
    # The planner keeps wanting to ask about something else on every call.
    stub_planner(monkeypatch, _ask_decision(slot="status", question="Which status?"))
    run(sf_intel.run("This quarter", [], Recorder().emit, conversation_id=CONV,
                     clarification_response=answer))

    second = Recorder()
    outcome = run(
        sf_intel.run("This quarter", [], second.emit, conversation_id=CONV,
                     clarification_response=answer)
    )
    assert "clarification" not in second.meta()
    assert "clarifying" not in second.phases()
    assert outcome.meta_extras.get("assumptions"), "say what was assumed instead"


def test_a_response_naming_a_clarification_we_never_issued_is_ignored(monkeypatch):
    """A client describing a question we did not ask must not steer the turn."""
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"))
    rec = Recorder()
    outcome = run(
        sf_intel.run(
            "how many open cases",
            [],
            rec.emit,
            conversation_id=CONV,
            clarification_response=ClarificationResponse(
                clarification_id="clr_never_issued",
                conversation_id=CONV,
                custom_text="whatever",
            ),
        )
    )
    assert outcome.handled is False
    assert "how many open cases" in outcome.resolved_text


def test_an_invalid_clarification_answer_is_refused_not_silently_re_asked(
    monkeypatch,
):
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _ask_decision())
    first = Recorder()
    run(sf_intel.run("Show my pipeline", [], first.emit, conversation_id=CONV))
    card = first.meta()["clarification"]

    rec = Recorder()
    outcome = run(
        sf_intel.run(
            "This quarter",
            [],
            rec.emit,
            conversation_id=CONV,
            clarification_response=ClarificationResponse(
                clarification_id=card["clarification_id"],
                conversation_id=CONV,
                selected_option_ids=["not-an-option"],
                resume_token=card["resume_token"],
            ),
        )
    )
    assert outcome.handled is True
    assert "could not apply that answer" in outcome.answer


def test_the_same_question_is_downgraded_to_an_assumption_not_asked_again():
    """Policy, not prompt: a planner that ignores its instructions still cannot
    produce a second identical card."""
    intent = PendingIntent(
        intent_id="i1",
        conversation_id=CONV,
        root_user_message_id="m1",
        original_user_text="show my pipeline",
    )
    decision = _ask_decision()
    from app.core.sf_intel.models import ClarificationRound, fingerprint

    intent.clarification_history.append(
        ClarificationRound(
            clarification_id="clr_1",
            slot="date_range",
            question="Which period should I use?",
            question_fingerprint=fingerprint("date_range", "Which period should I use?"),
            answered=True,
        )
    )
    enforced = planner.enforce_policy(decision, intent)
    assert enforced.action == "EXECUTE_SALESFORCE"
    assert enforced.internal_reason_code == "question_already_asked"
    assert any("date range" in a for a in enforced.assumptions)


def test_the_round_budget_downgrades_to_a_stated_assumption(monkeypatch):
    monkeypatch.setattr(settings, "salesforce_max_clarification_rounds", 1)
    from app.core.sf_intel.models import ClarificationRound

    intent = PendingIntent(
        intent_id="i1",
        conversation_id=CONV,
        root_user_message_id="m1",
        original_user_text="show my pipeline",
    )
    intent.clarification_history.append(
        ClarificationRound(
            clarification_id="c",
            slot="object",
            question="Which object?",
            question_fingerprint="f",
            answered=True,
        )
    )
    enforced = planner.enforce_policy(_ask_decision(), intent)
    assert enforced.action == "EXECUTE_SALESFORCE"
    assert enforced.assumptions, "an assumption taken silently is a wrong answer"


def test_a_one_option_question_is_never_shown():
    intent = PendingIntent(
        intent_id="i1", conversation_id=CONV, root_user_message_id="m1",
        original_user_text="x",
    )
    decision = AgentDecision(
        action="ASK_CLARIFICATION",
        clarification_draft=ClarificationDraft(
            slot="region",
            question="Which region?",
            options=[ClarificationOption(id="a", label="EMEA")],
        ),
    )
    enforced = planner.enforce_policy(decision, intent)
    assert enforced.action == "EXECUTE_SALESFORCE"


def test_clarification_can_be_switched_off_without_losing_context(monkeypatch):
    monkeypatch.setattr(
        settings, "salesforce_contextual_clarification_enabled", False
    )
    rec = Recorder()
    outcome = run(
        sf_intel.run("Show my pipeline", [], rec.emit, conversation_id=CONV)
    )
    assert outcome.handled is False
    assert run(sf_state.get_pending(CONV)) is None
    assert "clarifying" not in rec.phases()


# ── The live path ────────────────────────────────────────────────────────────

OPP_DESCRIBE = {
    "name": "Opportunity",
    "label": "Opportunity",
    "queryable": True,
    "fields": [
        {"name": "Id", "type": "id", "label": "Id"},
        {"name": "Name", "type": "string", "label": "Name"},
        {"name": "StageName", "type": "picklist", "label": "Stage"},
        {"name": "Amount", "type": "currency", "label": "Amount"},
        {"name": "CloseDate", "type": "date", "label": "Close Date"},
    ],
}


def _live(monkeypatch, *, pages, total=None, fail=False):
    """Point the tool layer at a fake org."""
    from app.core import salesforce

    monkeypatch.setattr(settings, "sf_live_enabled", True)
    monkeypatch.setattr(salesforce, "configured", lambda: True)

    async def org_key():
        return "orgtest"

    async def describe_object(name):
        if name != "Opportunity":
            raise RuntimeError("no such object")
        return OPP_DESCRIBE

    ran = {}

    async def run_soql_page(soql):
        if fail:
            raise salesforce.SalesforceUnavailable("Salesforce rejected the query")
        ran["soql"] = soql
        first = pages[0]
        return {
            "soql": soql,
            "rows": first,
            "total_size": total if total is not None else len(first),
            "done": len(pages) == 1,
            "next_records_url": (
                "/services/data/v61.0/query/01g000" if len(pages) > 1 else ""
            ),
        }

    async def query_more(_url):
        return {
            "rows": pages[1],
            "total_size": total,
            "done": True,
            "next_records_url": "",
        }

    monkeypatch.setattr(salesforce, "org_key", org_key)
    monkeypatch.setattr(salesforce, "describe_object", describe_object)
    monkeypatch.setattr(salesforce, "run_soql_page", run_soql_page)
    monkeypatch.setattr(salesforce, "query_more", query_more)
    return ran


def _stream(tokens):
    async def fake(*_a, **_k):
        for t in tokens:
            yield "token", t

    return fake


def _execute_decision(**plan_kwargs):
    from app.core.sf_intel.models import SalesforceQueryPlan

    plan_kwargs.setdefault("object_api_name", "Opportunity")
    plan_kwargs.setdefault("select_fields", ["Name", "StageName", "Amount"])
    return AgentDecision(
        action="EXECUTE_SALESFORCE",
        normalized_intent="open pipeline",
        resolved_slots={"object": "Opportunity", "date_range": "THIS_QUARTER"},
        structured_query_plan=SalesforceQueryPlan(**plan_kwargs),
    )


def test_a_live_answer_streams_with_provenance_and_computed_numbers(monkeypatch):
    rows = [
        {"Id": "1", "Name": "A", "StageName": "Won", "Amount": "100"},
        {"Id": "2", "Name": "B", "StageName": "Lost", "Amount": "300"},
    ]
    _live(monkeypatch, pages=[rows], total=2)
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _execute_decision())
    monkeypatch.setattr(llm, "stream_chat_events", _stream(["2 open ", "deals."]))

    rec = Recorder()
    outcome = run(
        sf_intel.run("Show my pipeline this quarter", [], rec.emit,
                     conversation_id=CONV)
    )
    assert outcome.handled is True
    assert outcome.answer == "2 open deals."
    meta = rec.meta()
    assert meta["route"] == "sql"
    assert meta["salesforce_sources"]["source"] == "live"
    assert meta["salesforce_sources"]["objects"] == ["Opportunity"]
    assert meta["salesforce_sources"]["record_count"] == 2
    assert meta["salesforce_sources"]["query_timestamp"]
    assert len(meta["data"]) == 2
    # Real phases, in order, each emitted when its stage began.
    assert rec.phases()[:1] == ["understanding"]
    for expected in (
        "querying_salesforce", "analyzing_records", "calculating",
        "verifying", "drafting_answer", "completed",
    ):
        assert expected in rec.phases(), expected


def test_the_query_is_not_shown_unless_it_was_asked_for(monkeypatch):
    rows = [{"Id": "1", "Name": "A", "StageName": "Won", "Amount": "100"}]
    _live(monkeypatch, pages=[rows], total=1)
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _execute_decision())
    monkeypatch.setattr(llm, "stream_chat_events", _stream(["ok"]))

    rec = Recorder()
    run(sf_intel.run("Show my pipeline", [], rec.emit, conversation_id=CONV))
    assert "sql" not in rec.meta()

    rec2 = Recorder()
    run(sf_intel.run("show me the SOQL for my pipeline", [], rec2.emit,
                     conversation_id="conv-2"))
    assert "sql" in rec2.meta()


def test_pagination_merges_every_page_and_counts_the_true_total(monkeypatch):
    page1 = [{"Id": str(i), "StageName": "Won", "Amount": "10"} for i in range(3)]
    page2 = [{"Id": str(i), "StageName": "Lost", "Amount": "10"} for i in range(3, 5)]
    _live(monkeypatch, pages=[page1, page2], total=5)
    stub_schema(monkeypatch)
    stub_planner(
        monkeypatch,
        _execute_decision(
            select_fields=[],
            aggregate_functions=["count"],
            group_by=["StageName"],
            result_mode="aggregate",
        ),
    )
    monkeypatch.setattr(llm, "stream_chat_events", _stream(["done"]))

    rec = Recorder()
    run(sf_intel.run("break my pipeline down by stage", [], rec.emit,
                     conversation_id=CONV))
    assert len(rec.meta()["data"]) == 5
    assert rec.meta()["salesforce_sources"]["pages"] == 2
    assert "retrieving_more_results" in rec.phases()


def test_a_tool_failure_is_never_reported_as_zero_matching_records(monkeypatch):
    """The two are completely different facts, and conflating them is how an
    outage becomes "you have no open opportunities"."""
    _live(monkeypatch, pages=[[]], fail=True)
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _execute_decision())

    rec = Recorder()
    outcome = run(
        sf_intel.run("Show my pipeline", [], rec.emit, conversation_id=CONV)
    )
    assert outcome.handled is True
    assert "failed" in outcome.answer
    assert "not the same as finding no matching records" in outcome.answer
    assert rec.meta().get("salesforce_error")
    assert "failed" in rec.phases()


def test_an_empty_result_says_so_plainly(monkeypatch):
    _live(monkeypatch, pages=[[]], total=0)
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _execute_decision())
    monkeypatch.setattr(llm, "stream_chat_events", _stream([]))

    rec = Recorder()
    outcome = run(sf_intel.run("Show my pipeline", [], rec.emit, conversation_id=CONV))
    assert "No matching Opportunity records were found" in outcome.answer
    assert "empty result, not a failure" in outcome.answer


def test_a_plan_the_compiler_refuses_falls_back_instead_of_running_anything(
    monkeypatch,
):
    _live(monkeypatch, pages=[[]])
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _execute_decision(select_fields=["SecretMargin__c"]))

    rec = Recorder()
    outcome = run(sf_intel.run("Show my pipeline", [], rec.emit, conversation_id=CONV))
    assert outcome.handled is False, "the warehouse engine answers instead"
    assert not any(e == "meta" for e, _ in rec.events)


def test_reasoning_deltas_never_reach_the_transcript(monkeypatch):
    """Raw chain-of-thought must not reach the UI, storage, or a log line."""
    rows = [{"Id": "1", "Name": "A", "StageName": "Won", "Amount": "1"}]
    _live(monkeypatch, pages=[rows], total=1)
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _execute_decision())

    async def with_reasoning(*_a, **_k):
        yield "reasoning", "First I will consider whether the user means…"
        yield "token", "One open deal."

    monkeypatch.setattr(llm, "stream_chat_events", with_reasoning)

    rec = Recorder()
    outcome = run(sf_intel.run("Show my pipeline", [], rec.emit, conversation_id=CONV))
    assert outcome.answer == "One open deal."
    assert "reasoning" not in rec.kinds()
    assert "First I will consider" not in json.dumps(rec.events)


def test_completing_a_live_answer_updates_the_session_state(monkeypatch):
    rows = [{"Id": "1", "Name": "A", "StageName": "Won", "Amount": "1"}]
    _live(monkeypatch, pages=[rows], total=1)
    stub_schema(monkeypatch)
    stub_planner(monkeypatch, _execute_decision())
    monkeypatch.setattr(llm, "stream_chat_events", _stream(["ok"]))

    run(sf_intel.run("Show my pipeline this quarter", [], Recorder().emit,
                     conversation_id=CONV))
    state = run(sf_state.load_state(CONV))
    assert state.last_salesforce_objects == ["Opportunity"]
    assert state.last_date_range == "THIS_QUARTER"
    assert state.last_result_metadata["record_count"] == 1


# ── The planner's own robustness ─────────────────────────────────────────────

def test_a_planner_that_returns_prose_falls_back_to_the_deterministic_detector(
    monkeypatch,
):
    async def prose(*_a, **_k):
        return "I think you probably mean opportunities, shall I look?"

    monkeypatch.setattr(llm, "json_completion", prose)
    decision = run(
        planner.plan(
            user_text="how may candidates completed the training from slot 128 "
                      "and how many failed the mock",
            intent=PendingIntent(
                intent_id="i", conversation_id=CONV, root_user_message_id="m",
                original_user_text="x",
            ),
            state=ConversationSalesforceState(conversation_id=CONV),
        )
    )
    assert decision.action == "ASK_CLARIFICATION"
    assert decision.internal_reason_code == "deterministic_detector"
    assert len(decision.clarification_draft.options) >= 2


def test_an_invalid_decision_is_repaired_once_then_accepted(monkeypatch):
    replies = [
        json.dumps({"action": "NOT_A_REAL_ACTION"}),
        json.dumps({"action": "EXECUTE_SALESFORCE", "normalized_intent": "ok"}),
    ]

    async def two_tries(*_a, **_k):
        return replies.pop(0)

    monkeypatch.setattr(llm, "json_completion", two_tries)
    decision = run(
        planner.plan(
            user_text="show my pipeline",
            intent=PendingIntent(
                intent_id="i", conversation_id=CONV, root_user_message_id="m",
                original_user_text="x",
            ),
            state=ConversationSalesforceState(conversation_id=CONV),
        )
    )
    assert decision.action == "EXECUTE_SALESFORCE"
    assert replies == [], "exactly one repair, not a loop"


def test_a_planner_that_never_produces_the_shape_does_not_loop(monkeypatch):
    calls = []

    async def always_bad(*_a, **_k):
        calls.append(1)
        return "{}"

    monkeypatch.setattr(llm, "json_completion", always_bad)
    decision = run(
        planner.plan(
            user_text="how many accounts",
            intent=PendingIntent(
                intent_id="i", conversation_id=CONV, root_user_message_id="m",
                original_user_text="x",
            ),
            state=ConversationSalesforceState(conversation_id=CONV),
        )
    )
    assert len(calls) <= 2, "one attempt plus one repair"
    assert decision.action in ("ASK_CLARIFICATION", "EXECUTE_SALESFORCE")


def test_a_thinking_block_is_stripped_from_a_planner_reply():
    raw = '<think>let me consider…</think>\n```json\n{"action": "DENY"}\n```'
    assert planner.extract_json_object(raw) == {"action": "DENY"}
    assert "consider" not in planner.strip_reasoning(raw)


def test_nested_json_survives_extraction():
    raw = '{"action":"ASK_CLARIFICATION","clarification_draft":{"question":"a{b}c"}}'
    parsed = planner.extract_json_object(raw)
    assert parsed["clarification_draft"]["question"] == "a{b}c"


def test_a_nested_argument_serialised_as_a_string_by_the_tool_parser_is_decoded():
    """vLLM's `qwen3_xml` parser — the one this deployment runs — serialises a
    NESTED tool argument as a JSON string. Verified live on 2026-08-11:
    `submit_plan` returned `action` as a value and `clarification_draft` as
    '{"slot": "object", …}'. Rejecting the decision over a transport detail
    would fall back to the deterministic detectors on every single turn."""
    decision = AgentDecision.model_validate(
        {
            "action": "ASK_CLARIFICATION",
            "clarification_draft": json.dumps(
                {
                    "slot": "object",
                    "question": "Which object represents your pipeline?",
                    "options": [
                        {"id": "o", "label": "Opportunities", "value": "Opportunity"},
                        {"id": "q", "label": "Quotes", "value": "Quote"},
                    ],
                }
            ),
            "resolved_slots": json.dumps({"metric": "count"}),
            "assumptions": json.dumps(["assumed open only"]),
        }
    )
    assert decision.clarification_draft.slot == "object"
    assert len(decision.clarification_draft.options) == 2
    assert decision.resolved_slots == {"metric": "count"}
    assert decision.assumptions == ["assumed open only"]


def test_a_query_plan_serialised_the_same_way_is_decoded_too():
    decision = AgentDecision.model_validate(
        {
            "action": "EXECUTE_SALESFORCE",
            "structured_query_plan": json.dumps(
                {
                    "object_api_name": "Opportunity",
                    "select_fields": ["Name", "Amount"],
                    "filters": [
                        {"field": "IsClosed", "operator": "eq", "value": "false"}
                    ],
                    "result_mode": "records",
                }
            ),
        }
    )
    plan = decision.structured_query_plan
    assert plan.object_api_name == "Opportunity"
    assert plan.select_fields == ["Name", "Amount"]
    assert plan.filters[0].field == "IsClosed"


def test_a_string_field_that_merely_looks_like_json_is_left_alone():
    """The decode is scoped to fields DECLARED structured, so a question that
    happens to contain braces is never rewritten."""
    decision = AgentDecision.model_validate(
        {
            "action": "ASK_CLARIFICATION",
            "normalized_intent": '{"not": "a decoded object"}',
            "clarification_draft": {
                "slot": "region",
                "question": "Which region?",
                "options": [
                    {"id": "a", "label": "EMEA"},
                    {"id": "b", "label": "AMER"},
                ],
            },
        }
    )
    assert decision.normalized_intent == '{"not": "a decoded object"}'


def test_an_undecodable_string_still_reports_the_real_validation_error():
    with pytest.raises(Exception) as excinfo:
        AgentDecision.model_validate(
            {"action": "ASK_CLARIFICATION", "clarification_draft": "{not json"}
        )
    assert "clarification_draft" in str(excinfo.value)


def test_the_planner_never_receives_a_slot_it_cannot_use():
    """An invented slot is DROPPED, not fatal: it must not cost the user the
    four slots the planner got right."""
    decision = AgentDecision(
        action="EXECUTE_SALESFORCE",
        resolved_slots={"region": "EMEA", "urgency": "high", "date_range": ""},
    )
    assert decision.resolved_slots == {"region": "EMEA"}


# ── Progress phases ──────────────────────────────────────────────────────────

def test_every_phase_has_a_concise_label():
    for phase in phases.PHASES:
        label = phases.label_for(phase)
        assert label and len(label) < 40, phase


def test_a_record_count_is_only_stated_when_it_is_real():
    assert phases.label_for("analyzing_records") == "Analyzing records"
    assert phases.label_for("analyzing_records", record_count=42) == "Analyzing 42 records"
    assert phases.label_for("analyzing_records", record_count=1) == "Analyzing 1 record"


def test_the_status_payload_keeps_its_backward_compatible_text_field():
    """Older clients read `text` and nothing else; the phase is additive."""
    payload = phases.status_payload("querying_salesforce", run_id="r1")
    assert payload["text"] == "Searching Salesforce"
    assert payload["phase"] == "querying_salesforce"
    assert payload["run_id"] == "r1"


def test_an_unknown_phase_is_refused_at_the_emitter():
    with pytest.raises(ValueError, match="unknown phase"):
        phases.status_payload("making_things_up")


# ── Context budgeting ────────────────────────────────────────────────────────

def test_the_documented_budget_holds_at_262144(monkeypatch):
    monkeypatch.setattr(settings, "model_max_context", 262144)
    monkeypatch.setattr(settings, "model_high_max_output", 16384)
    monkeypatch.setattr(settings, "main_model_context_safety_margin", 8192)
    plan = budget.budget_for("high")
    assert plan.window == 262144
    assert plan.reserved_output == 16384
    assert plan.safety_margin == 8192
    assert plan.max_input_tokens == 237568


def test_the_builder_drops_the_lowest_priority_blocks_first():
    blocks = [
        budget.Block(budget.P_SYSTEM, "system", "S" * 100, label="system"),
        budget.Block(budget.P_REQUEST, "user", "Q" * 100, label="request"),
        budget.Block(budget.P_RECORDS, "user", "R" * 100_000, label="records"),
        budget.Block(budget.P_OLDER_TURNS, "user", "O" * 100_000, label="older"),
    ]
    built = run(
        budget.build(blocks, budget=budget.ContextBudget(4096, 1024, 256))
    )
    kept = [m["content"][0] for m in built.messages]
    assert "S" in kept and "Q" in kept
    assert "older" in built.dropped
    assert "records" in built.dropped


def test_pinned_blocks_are_never_dropped_however_tight_the_budget():
    """Answering without the question you were asked is not a smaller failure
    than answering slowly."""
    blocks = [
        budget.Block(budget.P_SYSTEM, "system", "S" * 50_000, label="system"),
        budget.Block(budget.P_REQUEST, "user", "Q" * 50_000, label="request"),
        budget.Block(budget.P_PENDING, "user", "P" * 50_000, label="pending"),
    ]
    built = run(budget.build(blocks, budget=budget.ContextBudget(1000, 256, 64)))
    assert built.dropped == []
    assert len(built.messages) == 3


def test_blocks_are_ordered_by_priority_not_by_insertion():
    blocks = [
        budget.Block(budget.P_RECORDS, "user", "records"),
        budget.Block(budget.P_SYSTEM, "system", "instructions"),
        budget.Block(budget.P_REQUEST, "user", "the question"),
    ]
    built = run(budget.build(blocks, budget=budget.ContextBudget(100_000, 1024, 256)))
    assert [m["content"] for m in built.messages] == [
        "instructions", "the question", "records",
    ]


def test_an_empty_block_is_skipped_rather_than_sent_as_whitespace():
    blocks = [
        budget.Block(budget.P_SYSTEM, "system", "instructions"),
        budget.Block(budget.P_SALESFORCE_STATE, "user", "   "),
    ]
    built = run(budget.build(blocks, budget=budget.ContextBudget(100_000, 1024, 256)))
    assert len(built.messages) == 1


# ── Ungrouped aggregates: the value IS the answer ────────────────────────────
# "how many deliverables are locked?" ran SELECT COUNT(Id) ... and was answered
# `1` while the org held 866: Salesforce returns ONE synthetic row for an
# ungrouped aggregate, calculate_result published record_count=1 as an
# authoritative figure, and the answer model quoted it. These pin the fix.


def _aggregate_result(rows, columns, result_mode="count", total_size=1):
    return tools.QueryResult(
        soql="SELECT COUNT(Id) FROM Deliverable__c",
        object_api_name="Deliverable__c",
        rows=rows,
        total_size=total_size,
        pages=1,
        truncated=False,
        result_mode=result_mode,
        queried_at="2026-08-17T00:00:00+00:00",
        columns=columns,
    )


def test_an_ungrouped_count_reports_the_count_value_not_the_row_count():
    result = _aggregate_result([{"expr0": 866}], ("COUNT(Id)",))
    computed = tools.calculate_result(result)
    assert computed["record_count"] == 866
    assert computed["aggregate_values"] == {"COUNT(Id)": 866}
    # rows_examined=1 was the misleading co-figure; it must not survive.
    assert "rows_examined" not in computed


def test_a_single_aliased_aggregate_still_finds_its_value():
    result = _aggregate_result([{"total": 866}], ("COUNT(Id)",))
    computed = tools.calculate_result(result)
    assert computed["record_count"] == 866


def test_an_ungrouped_sum_never_claims_a_record_count():
    result = _aggregate_result(
        [{"expr0": 123456.78}], ("SUM(Payment_Amount__c)",), result_mode="aggregate"
    )
    computed = tools.calculate_result(result)
    assert "record_count" not in computed
    assert computed["aggregate_values"] == {"SUM(Payment_Amount__c)": 123456.78}
    assert "not queried" in computed["matching_record_count"]


def test_grouped_aggregates_keep_the_group_row_semantics():
    result = _aggregate_result(
        [{"Status__c": "Locked", "expr0": 866}, {"Status__c": "Active", "expr0": 107}],
        ("Status__c", "COUNT(Id)"),
        result_mode="aggregate",
        total_size=2,
    )
    computed = tools.calculate_result(result, group_by="Status__c")
    # Two GROUPS returned; the per-group counts live in the groups breakdown.
    assert computed["record_count"] == 2
    assert "aggregate_values" not in computed


def test_a_records_mode_result_is_untouched_by_the_aggregate_path():
    result = _aggregate_result(
        [{"Id": "a1", "Name": "x"}], ("Id", "Name"), result_mode="records"
    )
    computed = tools.calculate_result(result)
    assert computed["record_count"] == 1
    assert "aggregate_values" not in computed


def test_the_population_line_states_the_filters_already_applied():
    from app.core.sf_intel.models import QueryFilter, SalesforceQueryPlan

    plan = SalesforceQueryPlan(
        object_api_name="Deliverable__c",
        result_mode="count",
        filters=[QueryFilter(field="Status__c", operator="eq", value="Locked")],
    )
    line = sf_intel._population_line(plan)
    assert "Status__c" in line and "Locked" in line
    assert "already apply" in line


def test_a_plan_without_filters_adds_no_population_noise():
    from app.core.sf_intel.models import SalesforceQueryPlan

    plan = SalesforceQueryPlan(object_api_name="Deliverable__c", result_mode="count")
    assert sf_intel._population_line(plan) == ""
