"""Salesforce Intelligence Mode over HTTP — the contract clients actually see.

The engine's own behaviour is covered in test_sf_intel_engine.py. What is tested
HERE is everything that only exists once a request is a request: the SSE frames
and their order, the single final meta, the resume field on POST /chat, the
restore and cancel endpoints, and the promise that switching the source off
stops Salesforce being touched at all.
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.sf_intel import planner, state as sf_state, tools
from app.core.sf_intel.models import (
    AgentDecision,
    ClarificationDraft,
    ClarificationOption,
)
from app.engines import router as router_engine
from app.main import app

CONV = "conv-api"


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
    assert len(metas) == 1, f"§10: exactly ONE meta per turn, got {len(metas)}"
    return metas[0]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    tools.clear_caches()
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", True)
    monkeypatch.setattr(settings, "salesforce_contextual_clarification_enabled", True)
    monkeypatch.setattr(settings, "sf_live_enabled", False)

    async def no_schema(*_a, **_k):
        return "", []

    monkeypatch.setattr(tools, "get_salesforce_schema", no_schema)
    yield
    tools.clear_caches()


def _ask(monkeypatch, question="Which period should I use for the pipeline?"):
    decision = AgentDecision(
        action="ASK_CLARIFICATION",
        normalized_intent="pipeline",
        missing_critical_slots=["date_range"],
        clarification_draft=ClarificationDraft(
            slot="date_range",
            question=question,
            options=[
                ClarificationOption(id="m", label="This month", value="THIS_MONTH"),
                ClarificationOption(id="q", label="This quarter", value="THIS_QUARTER"),
                ClarificationOption(id="y", label="This year", value="THIS_YEAR"),
            ],
        ),
    )

    async def fake(*_a, **_k):
        return decision

    monkeypatch.setattr(planner, "plan", fake)


def _execute(monkeypatch):
    async def fake(*_a, **_k):
        return AgentDecision(action="EXECUTE_SALESFORCE", normalized_intent="pipeline")

    monkeypatch.setattr(planner, "plan", fake)


def _no_engines(monkeypatch):
    """Answering is not what these tests are about; keep the model out of it."""
    async def route(*_a, **_k):
        return "chat"

    monkeypatch.setattr(router_engine, "route_request", route)

    from app.engines import chat as chat_engine

    async def answer(message, history, emit, **_k):
        await emit("token", {"text": "ok"})
        await emit("meta", {"route": "chat"})
        return "ok"

    monkeypatch.setattr(chat_engine, "run_chat_engine", answer)


# ── Asking over the wire ─────────────────────────────────────────────────────

def test_an_ambiguous_salesforce_request_streams_a_clarification(monkeypatch):
    _ask(monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"message": "Show my pipeline", "conversation_id": CONV,
                  "mode": "salesforce"},
        )
    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]
    assert kinds[-1] == "done"
    assert "status" in kinds, "the phase indicator has something real to show"

    meta = _meta(events)
    assert meta["route"] == "clarify"
    card = meta["clarification"]
    assert card["slot"] == "date_range"
    assert [o["id"] for o in card["options"]] == ["m", "q", "y"]
    # The v1 keys the frontend has always merged in are still there.
    assert meta["mode"] == "salesforce"
    assert meta["generation_id"]


def test_the_phases_streamed_are_real_ones_in_order(monkeypatch):
    _ask(monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"message": "Show my pipeline", "conversation_id": CONV,
                  "mode": "salesforce"},
        )
    statuses = [d for e, d in _parse_sse(resp.text) if e == "status"]
    assert statuses[0]["phase"] == "understanding"
    assert statuses[-1]["phase"] == "clarifying"
    for payload in statuses:
        # Backward compatible: every status still carries the plain `text` a
        # pre-existing client reads, and nothing else.
        assert isinstance(payload["text"], str) and payload["text"]
        assert payload["run_id"]


def test_a_replay_of_the_same_generation_yields_the_same_single_card(monkeypatch):
    """The event buffer is replayed verbatim on attach, so a reconnect rebuilds
    ONE card rather than appending a second."""
    _ask(monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"message": "Show my pipeline", "conversation_id": CONV,
                  "mode": "salesforce"},
        )
    events = _parse_sse(resp.text)
    cards = [d for e, d in events if e == "meta" and "clarification" in d]
    assert len(cards) == 1


# ── Answering over the wire ──────────────────────────────────────────────────

def _pending_card(client, monkeypatch):
    _ask(monkeypatch)
    resp = client.post(
        "/chat",
        json={"message": "Show my pipeline", "conversation_id": CONV,
              "mode": "salesforce"},
    )
    return _meta(_parse_sse(resp.text))["clarification"]


def test_answering_resumes_the_original_request(monkeypatch):
    _no_engines(monkeypatch)
    with TestClient(app) as client:
        card = _pending_card(client, monkeypatch)
        _execute(monkeypatch)
        resp = client.post(
            "/chat",
            json={
                "message": "This quarter",
                "conversation_id": CONV,
                "mode": "salesforce",
                "clarification": {
                    "clarification_id": card["clarification_id"],
                    "conversation_id": CONV,
                    "client_message_id": "click-1",
                    "selected_option_ids": ["q"],
                    "custom_text": "",
                    "skipped": False,
                    "resume_token": card["resume_token"],
                },
            },
        )
    assert resp.status_code == 200
    assert _meta(_parse_sse(resp.text))["route"] == "chat"
    assert asyncio.run(sf_state.get_pending(CONV)) is None


def test_a_skip_carries_no_text_of_its_own_and_is_still_valid(monkeypatch):
    _no_engines(monkeypatch)
    with TestClient(app) as client:
        card = _pending_card(client, monkeypatch)
        _execute(monkeypatch)
        resp = client.post(
            "/chat",
            json={
                "conversation_id": CONV,
                "mode": "salesforce",
                "clarification": {
                    "clarification_id": card["clarification_id"],
                    "conversation_id": CONV,
                    "client_message_id": "skip-1",
                    "selected_option_ids": [],
                    "custom_text": "",
                    "skipped": True,
                    "resume_token": card["resume_token"],
                },
            },
        )
    assert resp.status_code == 200, "a skip has no message, and must not 422"


def test_an_answer_is_not_swallowed_when_the_orchestrator_wants_the_agent(
    monkeypatch,
):
    """Auto-orchestration decides some requests deserve agent steps. A
    clarification answer is the second half of a request this server started,
    so that gate must not apply to it — found on a live run (2026-08-11), where
    the resumed half was routed to the agent and the resume was lost."""
    _no_engines(monkeypatch)

    with TestClient(app) as client:
        # Ask the question first, with orchestration behaving normally…
        card = _pending_card(client, monkeypatch)
        _execute(monkeypatch)

        # …then make the classifier want the agent for the ANSWER.
        from app.engines import orchestrate

        async def always_agent(*_a, **_k):
            return orchestrate.Plan(agent=True, search=False)

        monkeypatch.setattr(orchestrate, "decide", always_agent)

        called = {}

        async def agent_engine(text, _history, emit, **_k):
            called["text"] = text
            await emit("token", {"text": "ok"})
            await emit("meta", {"route": "agent"})
            return "ok"

        from app.engines import agent as agent_module

        monkeypatch.setattr(agent_module, "run_agent_engine", agent_engine)

        resp = client.post(
            "/chat",
            json={
                "message": "This quarter",
                "conversation_id": CONV,
                "mode": "salesforce",
                "clarification": {
                    "clarification_id": card["clarification_id"],
                    "conversation_id": CONV,
                    "client_message_id": "click-1",
                    "selected_option_ids": ["q"],
                    "custom_text": "",
                    "skipped": False,
                    "resume_token": card["resume_token"],
                },
            },
        )
    assert resp.status_code == 200
    # The agent may still ANSWER — escalation is orthogonal. What must never
    # happen is the agent seeing the bare answer as if it were a question: it
    # receives the ORIGINAL request with the answer folded in.
    assert "Show my pipeline" in called["text"]
    assert "THIS_QUARTER" in called["text"]
    assert called["text"] != "This quarter"
    assert asyncio.run(sf_state.get_pending(CONV)) is None


def test_a_forged_resume_token_is_refused_rather_than_answered(monkeypatch):
    _no_engines(monkeypatch)
    with TestClient(app) as client:
        card = _pending_card(client, monkeypatch)
        resp = client.post(
            "/chat",
            json={
                "message": "This quarter",
                "conversation_id": CONV,
                "mode": "salesforce",
                "clarification": {
                    "clarification_id": card["clarification_id"],
                    "conversation_id": CONV,
                    "client_message_id": "forged",
                    "selected_option_ids": ["q"],
                    "custom_text": "",
                    "skipped": False,
                    "resume_token": "not-the-token",
                },
            },
        )
    events = _parse_sse(resp.text)
    answer = "".join(d["text"] for e, d in events if e == "token")
    assert "could not apply that answer" in answer
    # The question is still open: a forged answer must not resolve it.
    assert asyncio.run(sf_state.get_pending(CONV)) is not None


def test_a_malformed_clarification_does_not_break_the_send(monkeypatch):
    """A client bug must cost a resume, not the whole message."""
    _no_engines(monkeypatch)
    _execute(monkeypatch)
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={
                "message": "This quarter",
                "conversation_id": CONV,
                "mode": "salesforce",
                "clarification": {"nonsense": True},
            },
        )
    assert resp.status_code == 200
    assert [e for e, _ in _parse_sse(resp.text)][-1] == "done"


# ── The source toggle ────────────────────────────────────────────────────────

def test_turning_the_source_off_touches_no_salesforce_code_at_all(monkeypatch):
    _no_engines(monkeypatch)

    async def forbidden(*_a, **_k):
        raise AssertionError("the planner must not run with the source off")

    monkeypatch.setattr(planner, "plan", forbidden)

    async def no_schema(*_a, **_k):
        raise AssertionError("no schema lookup with the source off")

    monkeypatch.setattr(tools, "get_salesforce_schema", no_schema)

    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"message": "Show my pipeline", "conversation_id": CONV,
                  "mode": "assistant"},
        )
    assert resp.status_code == 200
    assert _meta(_parse_sse(resp.text))["mode"] == "assistant"


def test_turning_the_source_off_cancels_a_question_that_was_waiting(monkeypatch):
    """Deterministic: the server stops waiting. Otherwise the next Salesforce
    message in this chat is read as an answer to a dismissed question."""
    _no_engines(monkeypatch)
    with TestClient(app) as client:
        _pending_card(client, monkeypatch)
        assert asyncio.run(sf_state.get_pending(CONV)) is not None
        client.post(
            "/chat",
            json={"message": "who are you?", "conversation_id": CONV,
                  "mode": "assistant"},
        )
    assert asyncio.run(sf_state.get_pending(CONV)) is None


def test_the_feature_flag_restores_the_previous_behaviour_exactly(monkeypatch):
    """With intelligence mode off the deterministic detector answers instead,
    and its legacy `meta.clarify` payload is what ships."""
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_mode", "ambiguous")
    monkeypatch.setattr(settings, "clarify_before_answering", True)

    async def forbidden(*_a, **_k):
        raise AssertionError("the planner must not run when the flag is off")

    monkeypatch.setattr(planner, "plan", forbidden)

    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={
                "message": "how many candidates failed the mock from slot 128",
                "conversation_id": CONV,
                "mode": "salesforce",
            },
        )
    meta = _meta(_parse_sse(resp.text))
    assert meta["route"] == "clarify"
    assert "clarify" in meta and "clarification" not in meta


# ── Restore + cancel endpoints ───────────────────────────────────────────────

def test_the_pending_question_can_be_fetched_after_a_reload(monkeypatch):
    with TestClient(app) as client:
        card = _pending_card(client, monkeypatch)
        body = client.get(f"/chat/salesforce/{CONV}").json()
    assert body["pending_clarification"]["clarification_id"] == card["clarification_id"]


def test_the_starter_card_is_served_with_options(monkeypatch):
    _execute(monkeypatch)
    with TestClient(app) as client:
        body = client.get("/chat/salesforce/fresh-conversation").json()
    assert body["enabled"] is True
    assert any(o["id"] == "pipeline" for o in body["options"])
    for option in body["options"]:
        assert option["prompt"], "an option that sends nothing is decoration"


def test_the_starter_card_disappears_with_the_feature_flag(monkeypatch):
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    with TestClient(app) as client:
        body = client.get(f"/chat/salesforce/{CONV}").json()
    assert body == {"enabled": False, "options": [], "pending_clarification": None}


def test_cancelling_clears_the_question(monkeypatch):
    with TestClient(app) as client:
        _pending_card(client, monkeypatch)
        body = client.post(
            "/chat/salesforce/cancel", json={"conversation_id": CONV}
        ).json()
        assert body["cancelled"] == 1
        after = client.get(f"/chat/salesforce/{CONV}").json()
    assert after["pending_clarification"] is None


def test_another_owners_conversation_is_not_disclosed(as_user, monkeypatch):
    from app import db

    owner = as_user("owner")
    db.create_conversation(int(owner["id"]), "private-conv", "Private")
    other = as_user("intruder")
    assert int(other["id"]) != int(owner["id"])

    with TestClient(app) as client:
        assert client.get("/chat/salesforce/private-conv").status_code == 404
        assert (
            client.post(
                "/chat/salesforce/cancel", json={"conversation_id": "private-conv"}
            ).status_code
            == 404
        )


# ── Health ───────────────────────────────────────────────────────────────────

def test_health_reports_the_effective_context_window(monkeypatch):
    """"262144 is in .env" and "262144 is what the server accepts" are not the
    same claim, and only one of them is useful."""
    from app import health

    async def probe(_client):
        return {
            "configured_max_model_len": 262144,
            "served_max_model_len": 262144,
            "status": "ok",
            "budget": {"max_input_tokens": 237568},
            "serving_flags": {"auto_tool_choice": True},
        }

    monkeypatch.setattr(health, "probe_context_window", probe)
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["context"]["served_max_model_len"] == 262144
    assert body["context"]["budget"]["max_input_tokens"] == 237568


def test_a_window_mismatch_is_reported_without_failing_the_service(monkeypatch):
    from app import health

    monkeypatch.setattr(settings, "model_max_context", 262144)

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "m", "max_model_len": 131072}]}

    class FakeClient:
        async def get(self, _url):
            return FakeResponse()

    result = asyncio.run(health.probe_context_window(FakeClient()))
    assert result["status"] == "degraded"
    assert "will be rejected" in result["detail"]


def test_the_agent_classifier_cannot_preempt_the_clarification_gate(monkeypatch):
    """Owner report 2026-08-11: a long analytical Salesforce question was
    classified as agent-worthy, which skipped Salesforce Intelligence Mode
    entirely — so it got neither the clarification gate nor the deterministic
    figures, and which card the user saw depended on an unrelated classifier.

    Resolving and asking are ROUTING; the agent is an execution strategy."""
    _no_engines(monkeypatch)
    _ask(monkeypatch)

    from app.engines import orchestrate

    async def always_agent(*_a, **_k):
        return orchestrate.Plan(agent=True, search=False)

    monkeypatch.setattr(orchestrate, "decide", always_agent)

    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={
                "message": (
                    "Give me the training details for slot 128, how many mocks "
                    "happened, how many cleared and failed, and the ratio."
                ),
                "conversation_id": "conv-agent-gate",
                "mode": "salesforce",
                "effort": "medium",
            },
        )
    meta = _meta(_parse_sse(resp.text))
    assert meta["route"] == "clarify"
    assert "clarification" in meta, "the typed card, not the legacy one"
    assert "clarify" not in meta


def test_the_agent_receives_the_resolved_request_when_it_does_run(monkeypatch):
    """Handing back is not giving up: the agent still runs, but on the request
    the planner resolved rather than the ambiguous original."""
    _no_engines(monkeypatch)
    _execute(monkeypatch)

    from app.engines import orchestrate

    async def always_agent(*_a, **_k):
        return orchestrate.Plan(agent=True, search=False)

    monkeypatch.setattr(orchestrate, "decide", always_agent)

    seen = {}

    async def agent_engine(text, _history, emit, **_k):
        seen["text"] = text
        await emit("token", {"text": "ok"})
        await emit("meta", {"route": "agent"})
        return "ok"

    from app.engines import agent as agent_module

    monkeypatch.setattr(agent_module, "run_agent_engine", agent_engine)

    with TestClient(app) as client:
        client.post(
            "/chat",
            json={"message": "how many open opportunities close this quarter",
                  "conversation_id": "conv-agent-resolved", "mode": "salesforce"},
        )
    assert "how many open opportunities" in seen["text"]
