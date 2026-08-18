"""Intent resolution: understanding the request before deciding to ask about it.

The goal of the clarification system is NOT to ask more questions. It is to
understand the user, ask a good question only when a wrong guess would produce a
different number, and then finish the original request without being retyped.

Three layers are tested here, from the inside out:

  * `core/sf_intel/interpret.py` — the deterministic reading: spelling repaired
    against the org's own vocabulary, and the slots the sentence already settles;
  * `planner.enforce_policy` — the floor that downgrades a question the policy
    will not allow, INCLUDING one the model wanted to ask about something the
    user already said;
  * the HTTP surface — the scenarios end to end.

The model is stubbed throughout. What is under test is the machinery around it,
which is precisely the part that must hold when the model has a bad day.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.sf_intel import interpret, planner, state as sf_state, tools
from app.core.sf_intel.models import (
    AgentDecision,
    ClarificationDraft,
    ClarificationOption,
    ClarificationRequest,
    PendingIntent,
)
from app.engines import router as router_engine
from app.main import app

CONV = "conv-intent"


# ── Harness ──────────────────────────────────────────────────────────────────

def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        events.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return events


def _meta(events):
    metas = [d for e, d in events if e == "meta"]
    assert len(metas) == 1, f"exactly ONE meta per turn, got {len(metas)}"
    return metas[0]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    tools.clear_caches()
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", True)
    monkeypatch.setattr(settings, "salesforce_contextual_clarification_enabled", True)
    monkeypatch.setattr(settings, "clarify_mode", "ambiguous")
    monkeypatch.setattr(settings, "clarify_before_answering", True)
    monkeypatch.setattr(settings, "sf_live_enabled", False)

    async def no_schema(*_a, **_k):
        return "", []

    monkeypatch.setattr(tools, "get_salesforce_schema", no_schema)
    asyncio.run(sf_state.cancel_pending(CONV))
    yield
    asyncio.run(sf_state.cancel_pending(CONV))
    tools.clear_caches()


def _no_engines(monkeypatch):
    """Answering is not what these tests are about; keep the model out of it."""
    async def route(*_a, **_k):
        return "chat"

    monkeypatch.setattr(router_engine, "route_request", route)

    from app.engines import chat as chat_engine

    async def answer(message, history, emit, **_k):
        await emit("token", {"text": "12"})
        await emit("meta", {"route": "chat"})
        return "12"

    monkeypatch.setattr(chat_engine, "run_chat_engine", answer)


def _plan(monkeypatch, decision, *, seen=None):
    """Stub the planner, optionally recording what it was asked to plan for."""
    async def fake(**kwargs):
        if seen is not None:
            seen.append(kwargs)
        return decision

    monkeypatch.setattr(planner, "plan", fake)


def _ask_about(slot, question, options=("A", "B")):
    return AgentDecision(
        action="ASK_CLARIFICATION",
        normalized_intent="mocks",
        missing_critical_slots=[slot],
        clarification_draft=ClarificationDraft(
            slot=slot,
            header="Mock count",
            question=question,
            options=[
                ClarificationOption(id=f"o{i}", label=label)
                for i, label in enumerate(options)
            ],
        ),
    )


def _post(client, message, **extra):
    body = {"message": message, "conversation_id": CONV, "mode": "salesforce"}
    body.update(extra)
    return _parse_sse(client.post("/chat", json=body).text)


def _card(events):
    return _meta(events).get("clarification")


def _intent(text="How many advanced mock interviews are scheduled today?"):
    return PendingIntent(
        intent_id="int_1",
        conversation_id=CONV,
        root_user_message_id="m1",
        original_user_text=text,
    )


# ── Reading the request ──────────────────────────────────────────────────────

class TestReading:
    def test_domain_words_are_repaired_against_the_orgs_own_vocabulary(self):
        """Nobody maintains a list of misspellings: the spelling authority is
        the pack triggers, glossary and metric names already in the repo."""
        reading = interpret.read("how many advance mock scheddule todau?")
        assert "advanced" in reading.text
        assert "schedule" in reading.text
        assert "today" in reading.text
        assert reading.repaired

    def test_a_transposition_is_one_typo_not_two(self):
        """Levenshtein charges two edits for a swap, so a budget wide enough to
        catch "todya" would also turn unrelated words into each other."""
        assert "today" in interpret.read("how many mocks todya").text

    def test_names_are_never_corrected(self):
        """A capitalised word mid-sentence is a person, not a misspelling."""
        for text in ("Show mocks for Divya", "interviews owned by Naman"):
            assert interpret.read(text).text == text

    def test_ordinary_english_is_left_alone(self):
        for text in (
            "show me the top accounts",
            "list the active candidates",
            "what is the average amount per record",
        ):
            assert interpret.read(text).text == text

    def test_an_inflection_is_not_a_misspelling(self):
        """"offers" sits one edit from the vocabulary's "offer" and "scores"
        from "score". Rewriting them changed nothing — every matcher downstream
        stems its input — while filling the reading shown to the planner with
        corrections a reader would not recognise as corrections."""
        for text in (
            "how many offers were received",
            "show the evaluation scores for that interview",
            "how many sessions were rescheduled",
            "which recruiters placed the most candidates",
        ):
            assert interpret.read(text).repairs == (), text

    @pytest.mark.parametrize(
        "text",
        [
            "how many envelopes were voided",
            "unpaid invoices for dropped candidates",
            "what is the bill rate for that placement",
            "list every niche with no questions",
            "show the training deliverables locked last week",
            "bench candidates not marketed",
            "compare Q1 and Q2 placements",
            "who is the trainer for slot 128",
            "what percentage cleared the final mock",
            "show me the job requirements still open",
        ],
    )
    def test_a_correctly_spelled_request_is_never_rewritten(self, text):
        """A repair that "fixes" a correct word changes what the question means,
        which is far worse than a missed typo."""
        assert interpret.read(text).text == text

    def test_the_note_marks_the_users_words_as_authoritative(self):
        note = interpret.read("how many mocks scheddule todau").note()
        assert "scheddule" in note and "schedule" in note
        assert "authoritative" in note
        assert interpret.read("how many mocks today").note() == ""

    def test_a_stated_period_settles_the_period(self):
        for text in (
            "how many mocks today",
            "how many mocks tomorrow",
            "interviews this quarter",
            "candidates in 2026",
            "mocks between March and April",
            "how many mocks scheddule todau",
        ):
            assert "date_range" in interpret.satisfied_slots(interpret.read(text).text), text

    def test_a_question_with_no_period_settles_nothing_about_it(self):
        assert "date_range" not in interpret.satisfied_slots("how many mocks")

    def test_show_me_is_not_an_owner_scope(self):
        """"me" in "show me" is the recipient, not the owner. Reading it as a
        scope suppressed the one question that request actually needed."""
        assert "owner_scope" not in interpret.satisfied_slots("show me the top accounts")
        assert "owner_scope" in interpret.satisfied_slots("show my accounts")
        assert "owner_scope" in interpret.satisfied_slots("Divya's mocks")

    def test_the_metric_is_never_settled_by_the_sentence(self):
        """"How many advanced mocks today" genuinely does not say whether it
        means the interviews or the candidates who sit them, and that is the one
        question in this domain worth asking."""
        assert "metric" not in interpret.satisfied_slots("how many advanced mocks today")
        assert "object" not in interpret.satisfied_slots("how many interviews today")

    def test_a_follow_up_is_grounded_on_what_the_conversation_established(self):
        """"Tomorrow?" contains no trigger for any pack. Grounding it on this
        turn alone hands the planner an empty brief."""
        bare = interpret.grounding_text(interpret.read("Tomorrow?"))
        assert "mock" not in bare.lower()

        grounded = interpret.grounding_text(
            interpret.read("Tomorrow?"),
            original_request="how many advanced mocks today",
            recent_turns=[
                {"role": "user", "content": "how many advanced mocks today"},
                {"role": "assistant", "content": "12"},
            ],
        )
        assert "mock" in grounded.lower()
        # The assistant's prose is NOT borrowed — a paragraph of answer would
        # ground the follow-up in whatever it happened to mention.
        assert interpret.domain_knowledge(grounded)

    def test_the_planner_gets_the_org_brief_it_never_used_to_see(self):
        """The component that DECIDES whether to ask was the one component with
        no vocabulary. Asked about a mock it had no idea what a mock was."""
        knowledge = interpret.domain_knowledge("how many mocks in slot 128")
        assert knowledge
        assert "mock" in knowledge.lower() or "training" in knowledge.lower()


# ── The policy floor ─────────────────────────────────────────────────────────

class TestPolicy:
    def test_a_slot_the_sentence_settles_is_never_asked_about(self):
        """The single most damaging failure this feature has: asking "over what
        period?" about a request whose second word is "today"."""
        reading = interpret.read("how many advanced mocks today")
        decision = _ask_about("date_range", "Over what period?")
        out = planner.enforce_policy(decision, _intent(), reading=reading)
        assert out.action == "EXECUTE_SALESFORCE"
        assert out.internal_reason_code == "answered_by_the_request"
        assert out.assumptions, "an unasked question becomes a STATED assumption"

    def test_a_typo_does_not_reopen_a_settled_slot(self):
        reading = interpret.read("how many advance mock scheddule todau")
        out = planner.enforce_policy(
            _ask_about("date_range", "Over what period?"), _intent(), reading=reading
        )
        assert out.action == "EXECUTE_SALESFORCE"

    def test_a_genuinely_open_slot_is_still_asked_about(self):
        reading = interpret.read("how many advanced mocks today")
        out = planner.enforce_policy(
            _ask_about("metric", "Interviews or candidates?"), _intent(), reading=reading
        )
        assert out.action == "ASK_CLARIFICATION"

    def test_a_slot_the_user_already_answered_is_never_asked_about(self):
        intent = _intent()
        intent.resolved_slots["date_range"] = "THIS_QUARTER"
        out = planner.enforce_policy(
            _ask_about("date_range", "Over what period?"), intent,
            reading=interpret.read("how many mocks"),
        )
        assert out.internal_reason_code == "slot_already_resolved"

    def test_record_options_are_refused_unless_a_real_search_returned_them(self):
        """"Which record did you mean?" is the ONE question whose options are
        claims about data.

        Verified live on the 35B: asked "show mocks for John" with no org
        connection, the planner offered "John D.", "John S." and "John M." —
        three people who do not exist, presented as a list to pick from, at 0.95
        confidence. Every other slot's options are readings of the request and
        cost nothing if one is invented; a fabricated record is indistinguishable
        from a real one to the person clicking it.
        """
        decision = _ask_about(
            "record_identity", "Which John?", options=("John D.", "John S.")
        )
        reading = interpret.read("Show mocks for John.")

        refused = planner.enforce_policy(
            decision, _intent(), reading=reading, entity_candidates=[]
        )
        assert refused.action == "EXECUTE_SALESFORCE"
        assert refused.internal_reason_code == "unverified_record_options"

        # A search that DID find several people is exactly when to ask.
        allowed = planner.enforce_policy(
            decision,
            _intent(),
            reading=reading,
            entity_candidates=[{"name": "John D."}, {"name": "John S."}],
        )
        assert allowed.action == "ASK_CLARIFICATION"

    def test_every_other_slot_may_be_asked_about_without_a_search(self):
        """The guard is scoped to record identity. Readings of a request are not
        claims about data, so requiring a search for them would silence the
        questions this feature exists to ask."""
        for slot in ("metric", "object", "status", "grouping"):
            out = planner.enforce_policy(
                _ask_about(slot, "Which one?", options=("First", "Second")),
                _intent(),
                reading=interpret.read("how many mocks"),
                entity_candidates=[],
            )
            assert out.action == "ASK_CLARIFICATION", slot

    def test_two_options_that_say_the_same_thing_are_not_a_choice(self):
        decision = _ask_about(
            "metric", "Which?", options=("Scheduled interviews", "interviews scheduled")
        )
        out = planner.enforce_policy(
            decision, _intent(), reading=interpret.read("how many mocks")
        )
        assert out.action == "EXECUTE_SALESFORCE"
        assert out.internal_reason_code == "insufficient_options"

    def test_the_budget_ends_the_interrogation(self, monkeypatch):
        monkeypatch.setattr(settings, "salesforce_max_clarification_rounds", 2)
        intent = _intent()
        for i in range(2):
            intent.clarification_history.append(
                sf_state.ClarificationRound(
                    clarification_id=f"c{i}",
                    slot="filter",
                    question=f"q{i}",
                    question_fingerprint=f"f{i}",
                )
            )
        out = planner.enforce_policy(
            _ask_about("metric", "Which?"), intent,
            reading=interpret.read("how many mocks"),
        )
        assert out.internal_reason_code == "clarification_budget_spent"

    def test_a_double_click_cannot_produce_a_second_question(self):
        out = planner.enforce_policy(
            _ask_about("metric", "Which?"), _intent(), duplicate=True,
            reading=interpret.read("how many mocks"),
        )
        assert out.internal_reason_code == "duplicate_submission"

    def test_the_same_slot_is_never_asked_about_twice(self):
        intent = _intent()
        intent.clarification_history.append(
            sf_state.ClarificationRound(
                clarification_id="c1", slot="metric", question="Which count?",
                question_fingerprint="f1",
            )
        )
        # Different wording, same slot — the loop this guard exists to stop.
        out = planner.enforce_policy(
            _ask_about("metric", "Interviews or candidates?"), intent,
            reading=interpret.read("how many mocks"),
        )
        assert out.internal_reason_code == "question_already_asked"


# ── The scenarios, end to end ────────────────────────────────────────────────

class TestClearRequests:
    """No unnecessary clarification."""

    @pytest.mark.parametrize(
        "message",
        [
            "How many interviews today?",
            "How many candidates today?",
            "Show today's advanced mocks.",
            "how many advance mock scheddule todau?",
        ],
    )
    def test_a_clear_request_is_answered_even_if_the_model_wants_to_ask(
        self, monkeypatch, message
    ):
        """The model is stubbed to ask about the period on every one of these.
        The policy is what makes them go through anyway — a prompt can be
        ignored, a regex over the sentence cannot."""
        _no_engines(monkeypatch)
        _plan(monkeypatch, _ask_about("date_range", "Over what period?"))
        with TestClient(app) as client:
            events = _post(client, message)
        assert _card(events) is None
        assert asyncio.run(sf_state.get_pending(CONV)) is None


class TestGenuineAmbiguity:
    def test_a_materially_ambiguous_request_asks_one_good_question(self, monkeypatch):
        _no_engines(monkeypatch)
        _plan(
            monkeypatch,
            _ask_about(
                "metric",
                "Do you want the number of interviews or the number of candidates?",
                options=("Count of interviews", "Count of candidates"),
            ),
        )
        with TestClient(app) as client:
            events = _post(client, "How many advanced mocks today?")
        card = _card(events)
        assert card is not None
        assert card["header"] == "Mock count"
        assert 2 <= len(card["options"]) <= 4
        assert card["allow_custom"] is True
        assert card["resume_token"]
        # …and the phase indicator stops rather than spinning under a question.
        assert _meta(events)["route"] == "clarify"


class TestContinuation:
    """Original request → clarification → user choice → the ORIGINAL task
    continues, without being retyped and without restarting."""

    def test_the_answer_resumes_the_original_request(self, monkeypatch):
        _no_engines(monkeypatch)
        _plan(
            monkeypatch,
            _ask_about("metric", "Interviews or candidates?",
                       options=("Count of interviews", "Count of candidates")),
        )
        original = "How many advanced mock interviews are scheduled today?"
        with TestClient(app) as client:
            card = _card(_post(client, original))
            assert card is not None

            seen: list = []
            _plan(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"), seen=seen)
            events = _post(
                client,
                "Count of interviews",
                clarification={
                    "clarification_id": card["clarification_id"],
                    "conversation_id": CONV,
                    "client_message_id": "clr-1",
                    "selected_option_ids": [card["options"][0]["id"]],
                    "custom_text": "",
                    "skipped": False,
                    "resume_token": card["resume_token"],
                },
            )

        # THE POINT: the planner plans for the original request with the answer
        # folded in — not for the two words the user clicked. Planning for the
        # fragment is how a resume silently became a new, tiny question.
        assert seen, "the planner ran for the continuation"
        planned = seen[-1]["user_text"]
        assert "advanced mock interviews" in planned
        assert "Count of interviews" in planned
        assert "(Clarified:" in planned
        # No second card, and nothing left pending.
        assert _card(events) is None
        assert asyncio.run(sf_state.get_pending(CONV)) is None

    def test_a_custom_answer_resolves_and_continues(self, monkeypatch):
        _no_engines(monkeypatch)
        _plan(monkeypatch, _ask_about("metric", "Interviews or candidates?"))
        with TestClient(app) as client:
            card = _card(_post(client, "How many advanced mocks today?"))
            seen: list = []
            _plan(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"), seen=seen)
            _post(
                client,
                "just the ones Divya ran",
                clarification={
                    "clarification_id": card["clarification_id"],
                    "conversation_id": CONV,
                    "client_message_id": "clr-custom",
                    "selected_option_ids": [],
                    "custom_text": "just the ones Divya ran",
                    "skipped": False,
                    "resume_token": card["resume_token"],
                },
            )
        planned = seen[-1]["user_text"]
        assert "advanced mocks" in planned
        assert "Divya" in planned

    def test_a_double_click_runs_the_continuation_once(self, monkeypatch):
        """The same answer submitted twice — a double click, a retried fetch, a
        reconnect. The second must return the FIRST answer, not open a second
        question about a second missing detail."""
        _no_engines(monkeypatch)
        _plan(monkeypatch, _ask_about("metric", "Interviews or candidates?"))
        with TestClient(app) as client:
            card = _card(_post(client, "How many advanced mocks today?"))
            answer = {
                "clarification_id": card["clarification_id"],
                "conversation_id": CONV,
                "client_message_id": "clr-same",
                "selected_option_ids": [card["options"][0]["id"]],
                "custom_text": "",
                "skipped": False,
                "resume_token": card["resume_token"],
            }
            _plan(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"))
            first = _post(client, "Count of interviews", clarification=answer)

            # The repeat is not a new turn. Even with the planner now demanding
            # a second detail, it must not ask: seen live, the repeat of "Tasks"
            # was met with "what status counts as pending?", which reads as an
            # interrogation for doing nothing but clicking twice.
            _plan(monkeypatch, _ask_about("filter", "Which niche?"))
            second = _post(client, "Count of interviews", clarification=answer)
        assert _card(first) is None
        assert _card(second) is None
        assert asyncio.run(sf_state.get_pending(CONV)) is None

    def test_a_skip_is_an_answer_not_a_disappearance(self, monkeypatch):
        """A card that merely vanished would leave the question pending, and
        only one may be pending per conversation — so it would block every
        later question in the chat."""
        _no_engines(monkeypatch)
        _plan(monkeypatch, _ask_about("metric", "Interviews or candidates?"))
        with TestClient(app) as client:
            card = _card(_post(client, "How many advanced mocks today?"))
            _plan(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"))
            _post(
                client,
                "",
                clarification={
                    "clarification_id": card["clarification_id"],
                    "conversation_id": CONV,
                    "client_message_id": "clr-skip",
                    "selected_option_ids": [],
                    "custom_text": "",
                    "skipped": True,
                    "resume_token": card["resume_token"],
                },
            )
        assert asyncio.run(sf_state.get_pending(CONV)) is None


class TestLoops:
    def test_the_same_question_cannot_come_back(self, monkeypatch):
        """The planner is stubbed to ask the IDENTICAL question forever. It gets
        asked exactly once."""
        _no_engines(monkeypatch)
        _plan(monkeypatch, _ask_about("metric", "Interviews or candidates?"))
        with TestClient(app) as client:
            card = _card(_post(client, "How many advanced mocks today?"))
            assert card is not None
            events = _post(
                client,
                "Count of interviews",
                clarification={
                    "clarification_id": card["clarification_id"],
                    "conversation_id": CONV,
                    "client_message_id": "clr-loop",
                    "selected_option_ids": [card["options"][0]["id"]],
                    "custom_text": "",
                    "skipped": False,
                    "resume_token": card["resume_token"],
                },
            )
        assert _card(events) is None, "the second round asked the same thing again"

    def test_an_answer_is_never_rejected_for_omitting_what_it_was_given(self):
        """The resume token and the conversation id were only checked WHEN
        PRESENT, so a response with the fields left out skipped both — which is
        the one shape an attacker would choose."""
        from app.core.sf_intel.models import ClarificationResponse

        request = ClarificationRequest(
            clarification_id="clr_x", conversation_id=CONV, run_id="r",
            root_user_message_id="m", intent_id="i", question="Which?",
            slot="metric",
            options=[
                ClarificationOption(id="a", label="A"),
                ClarificationOption(id="b", label="B"),
            ],
            resume_token="secret", question_fingerprint="fp",
        )
        asyncio.run(sf_state._run(  # noqa: SLF001 — the row is the fixture
            sf_state.db.create_sf_clarification,
            clarification_id=request.clarification_id,
            conversation_id=CONV,
            intent_id="i",
            resume_token="secret",
            question_fingerprint="fp",
            payload=request.wire(),
        ))
        naked = ClarificationResponse(
            clarification_id="clr_x", selected_option_ids=["a"]
        )
        with pytest.raises(sf_state.ClarificationRejected):
            asyncio.run(sf_state.apply_response(naked))


class TestFollowUpContext:
    def test_a_completed_request_leaves_its_scope_for_the_next_turn(
        self, monkeypatch
    ):
        """"What about tomorrow?" can only inherit a subject if the previous
        turn recorded one. Carry-forward used to happen ONLY when a live SOQL
        query ran, so on a warehouse deployment — the common one — every
        follow-up arrived with nothing."""
        _no_engines(monkeypatch)
        _plan(
            monkeypatch,
            AgentDecision(
                action="EXECUTE_SALESFORCE",
                resolved_slots={
                    "object": "Internal_Interview__c",
                    "metric": "count of interviews",
                    "date_range": "TODAY",
                },
            ),
        )
        with TestClient(app) as client:
            _post(client, "How many advanced mocks today?")

        state = asyncio.run(sf_state.load_state(CONV))
        carried = state.carried_slots()
        assert carried.get("object")
        assert carried.get("metric") == "count of interviews"
        assert state.last_query_summary

    def test_the_follow_up_is_seeded_with_them(self, monkeypatch):
        _no_engines(monkeypatch)
        _plan(
            monkeypatch,
            AgentDecision(
                action="EXECUTE_SALESFORCE",
                resolved_slots={
                    "object": "Internal_Interview__c",
                    "metric": "count of interviews",
                    "date_range": "TODAY",
                },
            ),
        )
        with TestClient(app) as client:
            _post(client, "How many advanced mocks today?")
            seen: list = []
            _plan(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"), seen=seen)
            _post(client, "What about tomorrow?")

        intent = seen[-1]["intent"]
        assert intent.resolved_slots.get("metric") == "count of interviews"
        assert intent.resolved_slots.get("object")
        # …and the planner is told what the conversation already established.
        assert "Internal_Interview__c" in seen[-1]["state"].brief()

    def test_a_bare_follow_up_is_not_mistaken_for_small_talk(self):
        """"Tomorrow?" must reach the planner. The conversational shortcut is
        what decides that, and a request it swallows loses its whole turn."""
        from app.engines.sf_intel import is_conversational

        assert not is_conversational("Tomorrow?")
        assert not is_conversational("Only Divya's")
        assert is_conversational("thanks!")
        assert is_conversational("hello there")


class TestToolFailure:
    def test_a_failed_lookup_after_a_clarification_keeps_the_conversation(
        self, monkeypatch
    ):
        """A tool failure is reported as a failure — never as an empty result —
        and it does not strand the question or the intent."""
        _no_engines(monkeypatch)
        _plan(monkeypatch, _ask_about("metric", "Interviews or candidates?"))
        with TestClient(app) as client:
            card = _card(_post(client, "How many advanced mocks today?"))

            from app.engines import sql as sql_engine

            async def boom(message, history, emit, **_k):
                text = "The Salesforce lookup failed. Nothing was returned."
                await emit("token", {"text": text})
                await emit("meta", {"route": "sql", "salesforce_error": "boom"})
                return text

            monkeypatch.setattr(router_engine, "route_request", lambda *_a, **_k: _sql())
            monkeypatch.setattr(sql_engine, "run_sql_engine", boom)
            _plan(monkeypatch, AgentDecision(action="EXECUTE_SALESFORCE"))
            events = _post(
                client,
                "Count of interviews",
                clarification={
                    "clarification_id": card["clarification_id"],
                    "conversation_id": CONV,
                    "client_message_id": "clr-fail",
                    "selected_option_ids": [card["options"][0]["id"]],
                    "custom_text": "",
                    "skipped": False,
                    "resume_token": card["resume_token"],
                },
            )
        kinds = [e for e, _ in events]
        assert kinds[-1] == "done", "a tool failure is an ANSWER, not a stream error"
        # The clarification was consumed, so the next question is not blocked.
        assert asyncio.run(sf_state.get_pending(CONV)) is None

    def test_a_malformed_answer_keeps_the_question_open(self, monkeypatch):
        """Silently re-reading an unreadable answer as a new question cancelled
        the pending one AND lost the request behind it."""
        _no_engines(monkeypatch)
        _plan(monkeypatch, _ask_about("metric", "Interviews or candidates?"))
        with TestClient(app) as client:
            card = _card(_post(client, "How many advanced mocks today?"))
            events = _post(
                client,
                "Count of interviews",
                clarification={"clarification_id": card["clarification_id"]},
            )
        assert _meta(events)["route"] == "clarify"
        still = asyncio.run(sf_state.get_pending(CONV))
        assert still is not None and still.clarification_id == card["clarification_id"]


async def _sql():
    return "sql"
