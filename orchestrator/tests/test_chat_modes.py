"""V2 modes (V2-DESIGN §3a), all offline:

- router "chat" class parsing + updated few-shots
- model=smart|fast resolution to MAIN_MODEL|ROUTER_MODEL
- gpt-oss "Reasoning: <effort>" system line (smart only)
- stream_chat_events yields reasoning/token pairs from vLLM deltas
- POST /chat mode=assistant bypasses the router and never touches DuckDB
- POST /chat mode=salesforce route=chat streams via the graph chat node
"""
import asyncio
import json
from types import SimpleNamespace

import duckdb
import pytest
from fastapi.testclient import TestClient

from app import db, llm
from app import main as app_main
from app.artifacts import lexicon
from app.config import settings
from app.engines import router as router_engine
from app.engines import search as search_engine
from app.engines import sfk_bridge
from app.engines.router import FEW_SHOTS, ROUTES, parse_route
from app.living_knowledge import Prepared
from app.main import app
from tests.test_fast_lane_route import wired  # noqa: F401
from tests.test_sf_assistant_bridge import (  # noqa: F401
    ANSWER,
    ASK_AGAIN_LINE,
    NEW_CHAT_LINE,
    PART_ANSWER,
    REFUSED_LINE,
    SHAPES,
    UNCLEAR_LINE,
    _body,
    _item,
    _sse,
    admin,
    older_route,
    result_of,
    service,
)

# ---------------------------------------------------------------------------
# Router "chat" class (§3a)
# ---------------------------------------------------------------------------


def test_chat_is_a_router_class():
    assert "chat" in ROUTES
    assert set(ROUTES) == {"sql", "rag", "vision", "report", "chat"}


def test_parse_route_chat():
    assert parse_route('{"route": "chat"}') == "chat"
    assert parse_route('```json\n{"route": "chat"}\n```') == "chat"
    assert parse_route('sure! {"route": "chat"} :)') == "chat"
    assert parse_route('{"route": "CHAT"}') == "chat"


def test_few_shots_include_hello_my_name_is_chat_example():
    chat_examples = [q for q, route in FEW_SHOTS if route == "chat"]
    assert any("hello my name is" in q.lower() for q in chat_examples)


def test_router_system_prompt_offers_chat():
    assert '"chat"' in router_engine._SYSTEM
    assert "sql|rag|vision|report|chat" in router_engine._SYSTEM


# ---------------------------------------------------------------------------
# Model choice resolution + effort system line (§3a)
# ---------------------------------------------------------------------------


def test_smart_resolves_to_main_model():
    base_url, _key, model = llm.resolve_model_choice("smart")
    assert base_url == settings.openai_base_url
    assert model == settings.llm_model
    assert llm.served_model_id("smart") == settings.llm_model


def test_fast_resolves_to_the_main_model_too():
    """Strict one-model mode (availability CONTRACT v2 §1): the router is a
    classifier and never writes a person's answer, so "fast" — a stored
    preference an old tab may still send — is the main model with the
    reasoning pass off, exactly what `wants_thinking` always made of it."""
    base_url, _key, model = llm.resolve_model_choice("fast")
    assert base_url == settings.openai_base_url
    assert model == settings.llm_model
    assert llm.served_model_id("fast") == settings.llm_model
    assert llm.capabilities_for_model_choice("fast") is settings.main_capabilities


def test_effort_is_expressed_as_thinking_not_a_system_line():
    """The gpt-oss "Reasoning: <effort>" line is gone.

    One model now serves both picker choices, so effort and the Smart/Fast
    switch mean exactly one thing: whether the reasoning pass runs. That is
    the real cost, and it is what the model actually honours.
    """
    messages = [{"role": "user", "content": "hi"}]
    assert llm.apply_reasoning_effort(messages, "high", "smart") == messages

    # Three levels on one model (2026-08-19 collapse): Think and Max
    # reason, Fast answers directly. Legacy names normalize as aliases.
    assert llm.wants_thinking("smart", "think") is True
    assert llm.wants_thinking("smart", "max") is True
    assert llm.wants_thinking("smart", "fast") is False
    assert llm.wants_thinking("smart", "high") is True    # alias -> think
    assert llm.wants_thinking("smart", "medium") is True  # alias -> think
    assert llm.wants_thinking("smart", "low") is False    # alias -> fast
    assert llm.REASONING_EFFORTS == ("fast", "think", "max")
    assert llm.normalize_effort("extra_high") == "max"


def test_thinking_body_is_the_chat_template_switch():
    assert llm.thinking_body(True) == {
        "chat_template_kwargs": {"enable_thinking": True}
    }
    assert llm.thinking_body(False) == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_effort_line_skipped_for_unknown_effort():
    messages = [{"role": "user", "content": "hi"}]
    assert llm.apply_reasoning_effort(messages, "extreme", "smart") == messages


# ---------------------------------------------------------------------------
# stream_chat_events: reasoning + token deltas (§3a)
# ---------------------------------------------------------------------------


class _FakeStreamClient:
    """OpenAI-compatible fake that records create() kwargs and streams the
    given (reasoning_content, content) delta pairs."""

    def __init__(self, recorder, deltas):
        self._recorder = recorder
        self._deltas = deltas
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self._recorder["chat_kwargs"] = kwargs

        async def gen():
            for reasoning, content in self._deltas:
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                reasoning_content=reasoning, content=content
                            )
                        )
                    ]
                )

        return gen()


def _collect_events(monkeypatch, deltas, **kwargs):
    rec = {}

    def fake_factory(base_url, api_key=None):
        rec["base_url"] = base_url
        return _FakeStreamClient(rec, deltas)

    monkeypatch.setattr(llm, "_client", fake_factory)

    async def run():
        return [pair async for pair in llm.stream_chat_events(
            [{"role": "user", "content": "hi"}], **kwargs
        )]

    return asyncio.run(run()), rec


def test_stream_chat_events_yields_reasoning_then_tokens(monkeypatch):
    events, rec = _collect_events(
        monkeypatch,
        [("thinking…", None), (None, "Hello"), (None, "!")],
        model_choice="smart",
        effort="low",
    )
    assert events == [("reasoning", "thinking…"), ("token", "Hello"), ("token", "!")]
    assert rec["base_url"] == settings.openai_base_url
    kwargs = rec["chat_kwargs"]
    assert kwargs["model"] == settings.llm_model
    assert kwargs["stream"] is True
    # Effort now rides in extra_body, not as a prepended system line, and the
    # user message is therefore first.
    assert kwargs["messages"][0] == {"role": "user", "content": "hi"}
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}, (
        "effort=low must switch the reasoning pass OFF"
    )


def test_a_legacy_model_value_does_not_veto_the_chosen_effort(monkeypatch):
    """The legacy `model: "fast"` names WEIGHTS, and it selects none.

    Until 2026-09-27 this test asserted the opposite — that model_choice
    "fast" switched the reasoning pass off whatever effort said — which is
    the entanglement itself written down. `llm.resolve_model_choice` serves
    the SAME weights for every choice (asserted just above), so a stored
    preference no control can clear may not quietly spend the level the
    person picked. Effort decides; the choice only still resolves the model id.
    """
    events, rec = _collect_events(
        monkeypatch, [(None, "hey")], model_choice="fast", effort="high"
    )
    assert events == [("token", "hey")]
    # One model serves every picker choice (CONTRACT v2 §1).
    assert rec["base_url"] == settings.openai_base_url
    kwargs = rec["chat_kwargs"]
    assert kwargs["model"] == settings.llm_model
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}, (
        "effort=high (alias -> think) must reason, whatever `model` says"
    )
    # ...and Fast is still Fast on the same legacy value: it is the EFFORT
    # that switches the reasoning pass, at every choice.
    _, rec_fast = _collect_events(
        monkeypatch, [(None, "hey")], model_choice="fast", effort="fast"
    )
    assert rec_fast["chat_kwargs"]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_thinking_is_decided_by_effort_at_every_model_choice():
    """GUARD (2026-09-27). `wants_thinking` used to return False for every
    choice but "smart". Revert that line and this fails on "fast"."""
    for choice in (*llm.MODEL_CHOICES, "", "gpt-4o", "router"):
        assert llm.wants_thinking(choice, "max") is True, choice
        assert llm.wants_thinking(choice, "think") is True, choice
        assert llm.wants_thinking(choice, "extra_high") is True, choice  # alias
        assert llm.wants_thinking(choice, "fast") is False, choice
        assert llm.wants_thinking(choice, "low") is False, choice  # alias


def test_the_answer_stream_refuses_any_engine_but_the_main_model(monkeypatch):
    """The guard where the answer is produced: a base URL that is not the
    main model's — the router's, say — is refused before a stream opens."""
    monkeypatch.setattr(
        llm, "resolve_model_choice",
        lambda choice: (settings.router_base_url, llm.LOCAL_API_KEY, settings.router_model),
    )
    with pytest.raises(llm.AnswerEngineViolation):
        _collect_events(monkeypatch, [(None, "hey")], model_choice="smart", effort="fast")


# ---------------------------------------------------------------------------
# POST /chat (SSE) — assistant bypass + salesforce chat route
# ---------------------------------------------------------------------------


def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        event = lines[0][len("event: "):]
        data = json.loads(lines[1][len("data: "):])
        events.append((event, data))
    return events


def _fake_stream_chat_events(recorder, pairs):
    async def fake(messages, *, model_choice="smart", effort="medium", **kwargs):
        recorder["messages"] = list(messages)
        recorder["model_choice"] = model_choice
        recorder["effort"] = effort
        for pair in pairs:
            yield pair

    return fake


def _forbid_router_and_duckdb(monkeypatch):
    async def no_router(*a, **k):
        raise AssertionError("router must not run")

    def no_duckdb(*a, **k):
        raise AssertionError("DuckDB must not be touched")

    monkeypatch.setattr(router_engine, "route_request", no_router)
    monkeypatch.setattr(duckdb, "connect", no_duckdb)


def test_assistant_mode_bypasses_router_and_duckdb(monkeypatch):
    _forbid_router_and_duckdb(monkeypatch)
    rec = {}
    monkeypatch.setattr(
        llm,
        "stream_chat_events",
        _fake_stream_chat_events(
            rec, [("reasoning", "hmm"), ("token", "Hi "), ("token", "there!")]
        ),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"message": "hello my name is Naman", "mode": "assistant"},
        )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]
    assert kinds == ["reasoning", "token", "token", "meta", "done"]
    meta = dict(events[kinds.index("meta")][1])
    # Every answer carries a generation id (the dedupe key for clients
    # attached to the same detached generation); the rest of the contract is
    # unchanged.
    assert meta.pop("generation_id")
    # The query-tracing branch (merged 2026-09-12) puts the trace and request
    # ids on every meta, like generation_id: identities, not the contract
    # under test here.
    assert meta.pop("trace_id") and meta.pop("request_id")
    # The knowledge pre-pass reports how it served the turn (ADR-0001 D12:
    # `decision`, plus `degraded` when the judge was missing). Its VALUE
    # depends on the offline corpus and the classifier, so only its shape is
    # part of this contract.
    knowledge = meta.pop("knowledge", {})
    assert set(knowledge) <= {"decision", "degraded", "freshness", "from_local_memory"}
    assert meta == {
        "route": "chat",
        "mode": "assistant",
        "model": settings.llm_model,
        "effort": "think",
        # Toggle-off isolation (2026-11-25): written under the guard.
        "sf_isolation": 1,
    }
    # No Salesforce claims: assistant system prompt, user message last.
    assert rec["model_choice"] == "smart"
    assert rec["messages"][-1] == {"role": "user", "content": "hello my name is Naman"}


def test_assistant_mode_fast_model_reports_router_model(monkeypatch):
    _forbid_router_and_duckdb(monkeypatch)
    rec = {}
    monkeypatch.setattr(
        llm, "stream_chat_events", _fake_stream_chat_events(rec, [("token", "yo")])
    )

    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"message": "hi", "mode": "assistant", "model": "fast", "effort": "low"},
        )
    events = dict(_parse_sse(resp.text))
    assert events["meta"]["model"] == settings.llm_model  # one model, whatever the picker said
    # Legacy "low" normalizes at the API boundary; meta reports canonical.
    assert events["meta"]["effort"] == "fast"
    assert rec["model_choice"] == "fast"
    assert rec["effort"] == "fast"


def test_salesforce_mode_chat_route_streams_via_graph(monkeypatch):
    async def route_chat(message, has_image=False, history=()):
        return "chat"

    monkeypatch.setattr(router_engine, "route_request", route_chat)

    def no_duckdb(*a, **k):
        raise AssertionError("DuckDB must not be touched on the chat route")

    monkeypatch.setattr(duckdb, "connect", no_duckdb)

    rec = {}
    monkeypatch.setattr(
        llm, "stream_chat_events", _fake_stream_chat_events(rec, [("token", "Hello!")])
    )

    with TestClient(app) as client:
        resp = client.post("/chat", json={"message": "hey there", "mode": "salesforce"})
    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]
    assert kinds == ["token", "meta", "done"]
    meta = dict(events[1][1])
    assert meta.pop("generation_id")
    # The query-tracing branch (merged 2026-09-12) puts the trace and request
    # ids on every meta, like generation_id: identities, not the contract
    # under test here.
    assert meta.pop("trace_id") and meta.pop("request_id")
    assert meta == {
        "route": "chat",
        "mode": "salesforce",
        "model": settings.llm_model,
        "effort": "think",
    }
    # The prompt used to tell users to toggle Salesforce OFF, which the
    # model generalised into "I don't have access to your Salesforce
    # org" — said to someone whose org it had been querying.
    system = rec["messages"][0]["content"]
    assert "you DO have Salesforce access" in system
    assert "toggle" not in system.lower()


# ---------------------------------------------------------------------------
# V2 §2 meta truthfulness — model/effort report what actually served the
# answer, not the request's picker, on routes that ignore the picker
# ---------------------------------------------------------------------------


def test_sql_route_meta_reports_serving_model_not_picker(monkeypatch):
    """model=fast/effort=high on a data question: the sql engine answers with
    the MAIN model at default effort (spec §8), and meta must say so (V2 §2)."""
    from app.engines import sql as sql_engine

    async def route_sql(message, has_image=False, history=()):
        return "sql"

    async def fake_sql_engine(message, history, emit):
        await emit("token", {"text": "Two rows."})
        await emit("meta", {"route": "sql", "sql": "SELECT 1", "data": [], "truncated": False})
        return "Two rows."

    monkeypatch.setattr(router_engine, "route_request", route_sql)
    monkeypatch.setattr(sql_engine, "run_sql_engine", fake_sql_engine)

    with TestClient(app) as client:
        resp = client.post(
            "/chat", json={"message": "top accounts", "mode": "salesforce", "model": "fast", "effort": "high"}
        )
    assert resp.status_code == 200
    meta = dict(_parse_sse(resp.text))["meta"]
    assert meta["model"] == settings.llm_model      # the model that answered
    # The request asked for "fast" + "high"; the data engines are pinned to the
    # main model and report it honestly. (One model serves every path now, so
    # this is about the REPORTED id being the serving one, not the picker's.)
    assert meta["effort"] == "think"                 # picker effort not applied


def test_vision_route_meta_reports_vision_model_and_applied_effort(monkeypatch):
    """The vision route is served by the vision model — and since 2026-08-28
    it DOES honour a reasoning effort (it streams through
    `stream_chat_events`), so meta carries both the vision model id and the
    level the request actually ran at (V2 §2)."""
    from app.engines import vision as vision_engine

    async def route_vision(message, has_image=False, history=()):
        return "vision"

    async def fake_vision_engine(
        message, image_base64, history, emit, *, effort="think", max_tokens=None,
        conversation_id=None, total_pictures=0,
    ):
        await emit("token", {"text": "An invoice."})
        await emit("meta", {"route": "vision"})
        return "An invoice."

    monkeypatch.setattr(router_engine, "route_request", route_vision)
    monkeypatch.setattr(vision_engine, "run_vision_engine", fake_vision_engine)

    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"message": "read this", "image": "aGk=", "model": "fast", "effort": "high"},
        )
    meta = dict(_parse_sse(resp.text))["meta"]
    assert meta["model"] == settings.vision_model
    # Legacy "high" normalizes at the boundary; meta reports the canonical
    # level that was applied, never the raw wire value.
    assert meta["effort"] == "think"


def test_agent_meta_reports_smart_model_and_applied_effort(monkeypatch):
    """Agent synthesis is pinned to the smart model (§3b) but honors the
    requested effort — meta must reflect exactly that."""
    from app.engines import agent as agent_engine

    async def fake_agent_engine(
        message, history, emit, *, effort="medium", salesforce=True, web=True,
        web_forced=False, user_id=None, conversation_id="",
    ):
        await emit("token", {"text": "done"})
        await emit("meta", {"route": "agent", "steps": []})
        return "done"

    monkeypatch.setattr(agent_engine, "run_agent_engine", fake_agent_engine)

    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"message": "deep dive", "agent": True, "model": "fast", "effort": "high"},
        )
    meta = dict(_parse_sse(resp.text))["meta"]
    assert meta["model"] == settings.llm_model  # §3b: synthesis uses smart
    # Legacy "high" normalizes at the boundary; meta reports the canonical
    # level that was actually applied to synthesis.
    assert meta["effort"] == "think"


def test_salesforce_mode_default_unchanged_router_still_runs(monkeypatch):
    """V1 behavior preserved: default mode still consults the router."""
    calls = {}

    async def route_sql(message, has_image=False, history=()):
        calls["routed"] = message
        return "chat"  # cheapest terminal node for this offline test

    monkeypatch.setattr(router_engine, "route_request", route_sql)
    monkeypatch.setattr(
        llm, "stream_chat_events", _fake_stream_chat_events({}, [("token", "ok")])
    )

    with TestClient(app) as client:
        resp = client.post("/chat", json={"message": "top accounts"})
    assert resp.status_code == 200
    assert calls["routed"] == "top accounts"


# ---------------------------------------------------------------------------
# sf_assistant: which turns reach it, what is stored, and the web block
# ---------------------------------------------------------------------------
#
# The bridge's own behaviour is in test_sf_assistant_bridge.py. These are the
# places main.py decides around it.


def _reached(service, older_route, client, text: str) -> str:
    """Which system this question went to, and that it went to exactly one."""
    service.calls.clear()
    older_route.clear()
    resp = client.post("/chat", json=_body("gate-conv", text))
    assert resp.status_code == 200
    assert len(service.calls) + len(older_route) == 1, text
    return "bridge" if service.calls else "older route"


@pytest.mark.parametrize(
    "text",
    [
        # Each of these carries a file word: the switch-off rule skips the bridge
        # for all of them, and an admin would lose every chart.
        "chart of applications by month",
        "bar chart of interviews by status",
        "who reports to the sales manager",
        "how many reports did the team submit",
        "how many candidates uploaded their documents",
        "which resume file is pending for open cases",
        # And these carry none.
        "how many interviews happened last month",
        "and his joining date?",
    ],
)
def test_chart_and_noun_questions_reach_the_bridge(service, admin, older_route, text):
    with TestClient(app) as client:
        assert _reached(service, older_route, client, text) == "bridge"


@pytest.mark.parametrize(
    "text",
    [
        "export this list to excel",
        "export this chart to pdf",
        "chart of interviews by status as an excel file",
        "give me a pdf of this month's joiners",
        "show the pie chart in a pdf report",
    ],
)
def test_a_real_file_request_goes_to_the_older_path(service, admin, older_route, text):
    assert lexicon.file_signal(text), "not the case under test: no file word"
    with TestClient(app) as client:
        assert _reached(service, older_route, client, text) == "older route"


def test_a_member_with_a_file_word_never_reaches_the_bridge(service, as_user, older_route):
    as_user("sfa-member")
    with TestClient(app) as client:
        assert _reached(service, older_route, client, "chart of applications by month") == "older route"


def test_with_the_switch_off_a_file_word_skips_the_bridge_as_it_always_did(admin, older_route, monkeypatch):
    seen: list = []

    async def try_answer(text, **kwargs):
        seen.append(text)
        return None

    monkeypatch.setattr(sfk_bridge, "ASSISTANT_ENABLED", False)
    monkeypatch.setattr(sfk_bridge, "try_answer", try_answer)
    with TestClient(app) as client:
        client.post("/chat", json=_body("gate-off", "chart of applications by month"))
        assert seen == []
        client.post("/chat", json=_body("gate-off", "how many interviews happened last month"))
        assert seen == ["how many interviews happened last month"]


@pytest.mark.parametrize(("status", "line"), [("refused", REFUSED_LINE), ("unclear", UNCLEAR_LINE)])
@pytest.mark.parametrize("sent", ["its own text", ""])
def test_a_fixed_line_on_a_file_word_turn_is_not_held_or_replaced(service, admin, older_route, monkeypatch, status, line, sent):
    """The route is "sql" on these too. "chat" or "clarify" would send the turn
    to the hold at the file gate, and the artifact branch could then replace it."""
    from app.engines import artifact as artifact_engine

    async def no_artifact(*args, **kwargs):
        raise AssertionError("the file gate replaced the answer")

    monkeypatch.setattr(artifact_engine, "run_artifact_engine", no_artifact)
    text = "chart of interviews by status"
    assert lexicon.file_signal(text), "not the case under test: no file word"
    service.answers(status, answer=line if sent else "", kind="none", data_ts=None, objects=())
    with TestClient(app) as client:
        resp = client.post("/chat", json=_body("gate-held", text))
    assert older_route == []
    events = _sse(resp.text)
    assert [d["text"] for k, d in events if k == "token"] == [line]
    final = [d for k, d in events if k == "meta"][-1]
    assert final["route"] == "sql" and final["engine"] == "sf_assistant"
    assert events[-1][0] == "done"


def test_the_stored_answer_carries_the_engine_and_the_chart_and_no_sql(service, admin, older_route):
    chart, rows = SHAPES["bar"]
    service.answers(charts=[_item(chart, rows, subtitle=["Period: 2026"], notes=["A note."])])
    with TestClient(app) as client:
        resp = client.post("/chat", json=_body("gate-store", intent_id="int-sfa-1"))
    assert resp.status_code == 200
    row = db.get_chat_request("int-sfa-1")
    assert row["status"] == "completed"
    stored = app_main._persisted_answer("gate-store", row["generation_id"])
    assert stored["content"] == ANSWER
    meta = stored["meta"]
    assert meta["engine"] == "sf_assistant"
    assert meta["route"] == "sql"
    assert "sql" not in meta
    assert "chart_data" not in meta
    assert meta["chart"] == chart
    assert meta["data"] == rows
    assert meta["salesforce_sources"]["source"] == "synced"
    assert meta["salesforce_scope"] == "Period: 2026"
    assert meta["assumptions"] == ["A note."]
    # What the web block reads, from what was really stored.
    assert db.conversation_has_engine("gate-store", "sf_assistant") is True
    assert db.conversation_has_engine("gate-other", "sf_assistant") is False
    # The attempt ledger names it too, where a viewer's PUT cannot replace it.
    with db.connection() as con:
        usage = con.execute(
            "SELECT meta FROM usage_events WHERE generation_id = %s", (row["generation_id"],)
        ).fetchone()
    assert usage["meta"]["engine"] == "sf_assistant"
    # With no `sql`, a thumbs-up teaches nobody's prompt anything.
    with db.connection() as con:
        con.execute("UPDATE messages SET feedback = 'up' WHERE conversation_id = 'gate-store' AND role = 'assistant'")
    assert db.list_confirmed_sql_examples() == []


#: Nothing in it asks for the web by itself, so a False below is the block's doing.
ASSISTANT_QUESTION = "what is a balance sheet?"


def _hold_an_answer(user: dict, conversation_id: str) -> None:
    db.create_conversation(int(user["id"]), conversation_id, "A chat")
    db.add_message(int(user["id"]), conversation_id, "user", "how many interviews?")
    db.add_message(
        int(user["id"]), conversation_id, "assistant", "Echo Person had 7.", {"route": "sql", "engine": "sf_assistant"}
    )


@pytest.fixture()
def web_open(wired, monkeypatch):  # noqa: F811
    """Search on, and a spy on what the pre-pass and the search engine are given."""
    monkeypatch.setattr(settings, "search_enabled", True)
    monkeypatch.setattr(settings, "search_rate_per_min", 1000)
    monkeypatch.setattr(search_engine, "_rate", {})
    seen = {"prepare": [], "search": []}

    async def prepare(question, *, allow_network, **kwargs):
        seen["prepare"].append(allow_network)
        return Prepared(decision="static_model")

    async def run_search_engine(text, history, emit, effort="fast", **kwargs):
        seen["search"].append(text)
        await emit("token", {"text": "From the web."})
        await emit("meta", {"route": "search"})
        return "From the web."

    from app import living_knowledge

    monkeypatch.setattr(living_knowledge, "prepare", prepare)
    monkeypatch.setattr(search_engine, "run_search_engine", run_search_engine)
    return seen


def _ask_the_assistant(client, conversation_id: str, **fields):
    resp = client.post(
        "/chat",
        json={
            "message": ASSISTANT_QUESTION,
            "mode": "assistant",
            "effort": "fast",
            "web_search": "auto",
            "conversation_id": conversation_id,
            "session_id": conversation_id,
            **fields,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp


@pytest.mark.parametrize("switch", [False, True], ids=["switch off", "switch on"])
def test_a_chat_holding_an_answer_gets_no_web_at_any_door(admin, web_open, monkeypatch, switch):
    """Not tied to the switch: the row outlives a switch-off, and so must this."""
    monkeypatch.setattr(sfk_bridge, "ASSISTANT_ENABLED", switch)
    _hold_an_answer(admin, "web-held")
    with TestClient(app) as client:
        _ask_the_assistant(client, "web-held")
        assert web_open["prepare"] == [False]
        # A forced search is the strongest ask there is.
        _ask_the_assistant(client, "web-held", web_search="on")
    assert web_open["search"] == []


def test_a_chat_with_no_such_answer_keeps_the_web(admin, web_open):
    with TestClient(app) as client:
        _ask_the_assistant(client, "web-clean")
        assert web_open["prepare"] == [True]
        _ask_the_assistant(client, "web-clean", web_search="on")
    assert web_open["search"] == [ASSISTANT_QUESTION]


def test_when_the_read_fails_the_web_stays_closed_for_those_with_the_capability(admin, web_open, monkeypatch):
    def broken(conversation_id, engine):
        raise RuntimeError("database down")

    monkeypatch.setattr(db, "conversation_has_engine", broken)
    with TestClient(app) as client:
        _ask_the_assistant(client, "web-broken")
        assert web_open["prepare"] == [False]
        _ask_the_assistant(client, "web-broken", web_search="on")
    assert web_open["search"] == []


def test_a_members_turn_reads_too_and_keeps_the_web_in_a_chat_with_no_such_answer(as_user, web_open, monkeypatch):
    reads: list = []
    real = db.conversation_has_engine
    monkeypatch.setattr(db, "conversation_has_engine", lambda *args: reads.append(args) or real(*args))
    as_user("sfa-member")
    with TestClient(app) as client:
        _ask_the_assistant(client, "web-member")
        _ask_the_assistant(client, "web-member", web_search="on")
    assert reads == [("web-member", "sf_assistant")] * 2
    assert web_open["prepare"] == [True]
    assert web_open["search"] == [ASSISTANT_QUESTION]


def test_a_person_who_lost_the_capability_gets_no_web_in_a_chat_holding_an_answer(as_user, web_open):
    """An admin who has since become a member still holds the chat, with the name in
    its history."""
    _hold_an_answer(as_user("sfa-former-admin"), "web-former")
    with TestClient(app) as client:
        _ask_the_assistant(client, "web-former")
        assert web_open["prepare"] == [False]
        resp = _ask_the_assistant(client, "web-former", web_search="on")
    assert web_open["search"] == []
    assert WEB_OFF_LINE in _status_lines(resp)


def test_when_the_read_fails_a_person_without_the_capability_keeps_the_web(as_user, web_open, monkeypatch):
    """A database fault must not take web search from everyone who never could hold
    such an answer. The price: a former admin's turn during that fault is open too."""

    def broken(conversation_id, engine):
        raise RuntimeError("database down")

    monkeypatch.setattr(db, "conversation_has_engine", broken)
    as_user("sfa-member")
    with TestClient(app) as client:
        _ask_the_assistant(client, "web-member-broken")
        assert web_open["prepare"] == [True]
        resp = _ask_the_assistant(client, "web-member-broken", web_search="on")
    assert web_open["search"] == [ASSISTANT_QUESTION]
    assert WEB_OFF_LINE not in _status_lines(resp)


def test_salesforce_mode_does_no_read_because_it_has_no_web_to_close(admin, older_route, monkeypatch):
    reads: list = []
    monkeypatch.setattr(db, "conversation_has_engine", lambda *args: reads.append(args) or True)
    with TestClient(app) as client:
        client.post("/chat", json=_body("web-sf"))
    assert reads == []


def test_the_pre_pass_of_the_assistant_branch_is_closed_too(admin, web_open, monkeypatch):
    """The call in the assistant branch itself, reached when no earlier gate
    started a pre-pass. It never read the flag, because until now the flag was
    always true there."""
    seen: list = []

    async def prepare_knowledge(request, text, *, allow_network, **kwargs):
        seen.append(allow_network)
        return Prepared()

    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(app_main, "_prepare_knowledge", prepare_knowledge)
    _hold_an_answer(admin, "web-branch-held")
    with TestClient(app) as client:
        _ask_the_assistant(client, "web-branch-held")
        assert seen == [False]
        _ask_the_assistant(client, "web-branch-clean")
    assert seen == [False, True]


def test_the_agent_and_deep_research_get_no_web_either(admin, web_open, monkeypatch):
    from app.engines import agent as agent_engine

    seen: list = []

    async def fake_agent_engine(message, history, emit, *, web=True, web_forced=False, **kwargs):
        seen.append(web)
        await emit("token", {"text": "done"})
        await emit("meta", {"route": "agent", "steps": []})
        return "done"

    monkeypatch.setattr(agent_engine, "run_agent_engine", fake_agent_engine)
    monkeypatch.setattr(settings, "deep_research_enabled", True)
    _hold_an_answer(admin, "web-agent")
    with TestClient(app) as client:
        _ask_the_assistant(client, "web-agent", agent=True, web_search="on")
        assert seen == [False]
        resp = _ask_the_assistant(client, "web-agent", deep_research=True)
    assert "Deep Research is unavailable here" in resp.text


# ---------------------------------------------------------------------------
# sf_assistant: file turns stay with the older system
# ---------------------------------------------------------------------------
#
# The service's answer is final, so a file turn it takes makes no file. These
# turns only ask for a file given the context the artifact gate reads, and most
# of them hold no file word, so they never reached the rule while it read the
# words alone.

#: A substantial answer, then the turn.
RECORDS_THREAD = [
    {"role": "user", "content": "list the interviews last month"},
    {"role": "assistant", "content": "| Name | Count |\n|---|---|\n| Echo Person | 7 |\n| Delta Person | 3 |\n\n" * 5},
]
#: A file the platform made, then the turn: the engine's own sentence is a file card.
FILE_THREAD = [
    {"role": "user", "content": "make a deck of interviews"},
    {"role": "assistant", "content": "Created **Interviews report** as a deck."},
]


def _thread_turn(client, conversation_id: str, thread: list, text: str, **fields):
    resp = client.post(
        "/chat",
        json={
            "messages": [*thread, {"role": "user", "content": text}],
            "mode": "salesforce",
            "conversation_id": conversation_id,
            "effort": "fast",
            **fields,
        },
    )
    assert resp.status_code == 200, resp.text
    return resp


def _went_to(service, older_route, client, thread: list, text: str, **fields) -> str:
    service.calls.clear()
    older_route.clear()
    _thread_turn(client, "file-conv", thread, text, **fields)
    assert len(service.calls) + len(older_route) == 1, text
    return "bridge" if service.calls else "older route"


@pytest.fixture()
def a_file_in_the_chat(monkeypatch):
    """One published artifact in this chat, and no model: the gate's classifier
    hook is a model call, so the rules alone decide."""
    from app.artifacts import intent_llm
    from app.engines import artifact as artifact_engine

    async def no_verdict(*args, **kwargs):
        return None

    monkeypatch.setattr(intent_llm, "classify", no_verdict)
    monkeypatch.setattr(
        artifact_engine.adb,
        "list_artifacts",
        lambda viewer, conversation_id: [
            {"id": "art-1", "title": "Interviews report", "current": {"status": "completed", "version": 1}}
        ],
    )


@pytest.fixture()
def the_file_path(monkeypatch):
    """The older route as it is for a turn it hands back, and a spy on the engine
    that makes the file."""
    from app.artifacts import intent_llm
    from app.engines import artifact as artifact_engine
    from app.engines import sf_intel

    async def no_verdict(*args, **kwargs):
        return None

    async def run(text, history, emit, **kwargs):
        return sf_intel.Outcome(handled=False, resolved_text=text)

    made: list = []

    async def run_artifact_engine(text, history, emit, **kwargs):
        made.append(text)
        await emit("token", {"text": "Here is your file."})
        await emit("meta", {"route": "artifact", "artifacts": []})
        return "Here is your file."

    monkeypatch.setattr(intent_llm, "classify", no_verdict)
    monkeypatch.setattr(sf_intel, "run", run)
    monkeypatch.setattr(artifact_engine, "run_artifact_engine", run_artifact_engine)
    return made


FILE_FOLLOW_UPS = [
    "also as Word",
    "also as a word doc",
    "pdf please",
    "in excel",
    "download this",
    "csv",
    "xlsx",
    "export this",
    "export it",
    "export that table",
    "make slide 4 shorter",
]
FILE_EDITS = [
    "add a slide about joiners",
    "update that file",
    "edit the report",
    "change it to a pie chart",
    "what does this sheet have?",
    "what is in the sheet you made?",
]


@pytest.mark.parametrize("text", FILE_FOLLOW_UPS)
def test_a_file_follow_up_goes_to_the_older_path(service, admin, older_route, text):
    with TestClient(app) as client:
        assert _went_to(service, older_route, client, RECORDS_THREAD, text) == "older route"


@pytest.mark.parametrize("text", [t for t in FILE_FOLLOW_UPS if t != "make slide 4 shorter"])
def test_a_file_follow_up_ends_in_a_file_as_it_does_with_the_switch_off(service, admin, the_file_path, text):
    with TestClient(app) as client:
        _thread_turn(client, "file-made", RECORDS_THREAD, text)
    assert service.calls == []
    assert the_file_path == [text]


@pytest.mark.parametrize("text", FILE_EDITS)
@pytest.mark.parametrize(
    "thread",
    [pytest.param(FILE_THREAD, id="right after the file"), pytest.param([*FILE_THREAD, *RECORDS_THREAD], id="an answer since")],
)
def test_a_turn_about_a_file_in_the_chat_goes_to_the_older_path(service, admin, older_route, a_file_in_the_chat, thread, text):
    with TestClient(app) as client:
        assert _went_to(service, older_route, client, thread, text) == "older route"


@pytest.mark.parametrize("text", ["list them", "summarize this", "explain this", "what does this have?"])
def test_right_after_a_file_a_bare_question_is_about_that_file(service, admin, older_route, a_file_in_the_chat, text):
    with TestClient(app) as client:
        assert _went_to(service, older_route, client, FILE_THREAD, text) == "older route"


def test_the_edit_box_goes_to_the_older_path(service, admin, older_route, a_file_in_the_chat):
    with TestClient(app) as client:
        assert (
            _went_to(service, older_route, client, FILE_THREAD, "make the second heading blue", artifact_id="art-1")
            == "older route"
        )


@pytest.mark.parametrize(
    "text",
    [
        "list them",
        "show them",
        "summarize this",
        "explain this",
        "what does this have?",
        "how many of these are closed?",
        "and for last month?",
        "what about Echo Person?",
        "show this as a table",
        "break this down by month",
        "give me more details",
        "which report does he have",
        "how many candidates uploaded their documents",
        "make it a pie chart",
        "show it as a bar chart",
    ],
)
@pytest.mark.parametrize("file_earlier", [False, True], ids=["no file", "a file made earlier"])
def test_an_ordinary_follow_up_stays_with_the_bridge(
    service, admin, older_route, a_file_in_the_chat, monkeypatch, file_earlier, text
):
    """The last turn is a records answer, so these are about that answer. In a
    chat holding a file the rules also read the first few as about the file."""
    from app.engines import artifact as artifact_engine

    if not file_earlier:
        monkeypatch.setattr(artifact_engine.adb, "list_artifacts", lambda viewer, conversation_id: [])
    thread = [*FILE_THREAD, *RECORDS_THREAD] if file_earlier else RECORDS_THREAD
    with TestClient(app) as client:
        assert _went_to(service, older_route, client, thread, text) == "bridge"


def test_a_question_about_an_earlier_upload_goes_to_the_older_path(service, admin, older_route, monkeypatch):
    """The service cannot read uploads. A document rides on every later turn, so
    only the words can say which turns are about it."""
    monkeypatch.setattr(
        db,
        "get_documents",
        lambda conversation_id: [{"filename": "policy.pdf", "total_pages": 2, "text": "The notice period is 60 days."}],
    )
    with TestClient(app) as client:
        assert (
            _went_to(service, older_route, client, [], "what does that pdf say about the notice period?")
            == "older route"
        )
        assert _went_to(service, older_route, client, [], "how many interviews happened last month") == "bridge"


def test_with_no_document_that_question_is_the_bridges(service, admin, older_route):
    with TestClient(app) as client:
        assert _went_to(service, older_route, client, [], "what does that pdf say about the notice period?") == "bridge"


BLOCKED_THREAD = [{"role": "user", "content": "what is Echo Person's passport number?"}]
FILE_ASKS = ["pdf please", "export this", "download this", "export this list to excel"]


@pytest.fixture()
def older_and_file(the_file_path, monkeypatch):
    """Spies on the two things a file request could start, with the older route
    handing the turn back as it does for a file."""
    from app.engines import sf_intel

    older: list = []

    async def run(text, history, emit, **kwargs):
        older.append(text)
        return sf_intel.Outcome(handled=False, resolved_text=text)

    monkeypatch.setattr(sf_intel, "run", run)
    return older, the_file_path


@pytest.mark.parametrize(
    ("last_answer", "line"),
    [(REFUSED_LINE, NEW_CHAT_LINE), (PART_ANSWER, NEW_CHAT_LINE), (UNCLEAR_LINE, ASK_AGAIN_LINE)],
    ids=["refused", "refused under real text", "unclear"],
)
@pytest.mark.parametrize("text", FILE_ASKS)
def test_right_after_a_refusal_a_file_request_is_told_there_is_none_and_nothing_runs(
    service, admin, older_and_file, last_answer, line, text
):
    """The file path starts with the older route, which would read the blocked
    question in its history, and the service can make no file: it would answer in
    text, with no word about the file. After an unclear line alone, asking again
    works, so that line says so and not "new chat"."""
    with TestClient(app) as client:
        resp = _thread_turn(client, "file-stop", [*BLOCKED_THREAD, {"role": "assistant", "content": last_answer}], text)
    assert older_and_file == ([], [])
    assert service.calls == []
    assert [d["text"] for k, d in _sse(resp.text) if k == "token"] == [line]


@pytest.mark.parametrize("text", FILE_ASKS)
@pytest.mark.parametrize("role", ["admin", "member"], ids=["admin", "former admin"])
def test_a_file_request_in_a_chat_holding_a_stored_stop_line_is_told_the_same(service, as_user, older_and_file, role, text):
    """The second ask: the client's last answer holds no line, and it was this turn
    that went to the file path with the refused question in its history."""
    user = as_user(f"sfa-{role}", role=role)
    db.create_conversation(int(user["id"]), f"file-stored-{role}", "A chat")
    db.add_message(int(user["id"]), f"file-stored-{role}", "user", BLOCKED_THREAD[0]["content"])
    db.add_message(int(user["id"]), f"file-stored-{role}", "assistant", PART_ANSWER, {"route": "sql", "engine": "sf_assistant"})
    thread = [*BLOCKED_THREAD, {"role": "assistant", "content": PART_ANSWER}, {"role": "user", "content": "how many interviews?"},
              {"role": "assistant", "content": ANSWER}]
    with TestClient(app) as client:
        resp = _thread_turn(client, f"file-stored-{role}", thread, text)
    assert older_and_file == ([], [])
    assert service.calls == []
    assert [d["text"] for k, d in _sse(resp.text) if k == "token"] == [NEW_CHAT_LINE]


@pytest.mark.parametrize("stored", [False, True], ids=["the client's turns", "a stored row"])
@pytest.mark.parametrize("text", FILE_ASKS)
def test_an_unclear_line_closes_a_file_request_on_the_very_next_turn_only(service, admin, older_and_file, stored, text):
    """It holds nothing back, so it must not close file exports for the rest of the chat."""
    older, made = older_and_file
    conversation_id = f"file-unclear-{stored}"
    if stored:
        db.create_conversation(int(admin["id"]), conversation_id, "A chat")
        db.add_message(int(admin["id"]), conversation_id, "user", BLOCKED_THREAD[0]["content"])
        db.add_message(int(admin["id"]), conversation_id, "assistant", UNCLEAR_LINE, {"route": "sql", "engine": "sf_assistant"})
        thread = [*BLOCKED_THREAD, {"role": "assistant", "content": ANSWER}]
    else:
        thread = [*BLOCKED_THREAD, {"role": "assistant", "content": UNCLEAR_LINE}, {"role": "user", "content": "how many interviews?"},
                  {"role": "assistant", "content": ANSWER}]
    with TestClient(app) as client:
        resp = _thread_turn(client, conversation_id, thread, text)
    assert made == [text]
    assert service.calls == []
    assert NEW_CHAT_LINE not in resp.text
    assert ASK_AGAIN_LINE not in resp.text


@pytest.mark.parametrize("text", FILE_ASKS)
def test_a_refusal_before_an_unclear_line_still_closes_file_requests(service, admin, older_and_file, text):
    thread = [*BLOCKED_THREAD, {"role": "assistant", "content": PART_ANSWER}, {"role": "user", "content": "and his company?"},
              {"role": "assistant", "content": UNCLEAR_LINE}, {"role": "user", "content": "how many interviews?"},
              {"role": "assistant", "content": ANSWER}]
    with TestClient(app) as client:
        resp = _thread_turn(client, "file-refused-unclear", thread, text)
    assert older_and_file == ([], [])
    assert service.calls == []
    assert [d["text"] for k, d in _sse(resp.text) if k == "token"] == [NEW_CHAT_LINE]


@pytest.mark.parametrize(("refused", "made_it"), [(True, False), (False, True)], ids=["after a refusal", "no refusal"])
def test_a_question_about_our_file_that_skips_the_gate_stays_out_of_the_file_branch_after_a_refusal(
    service, admin, older_and_file, a_file_in_the_chat, monkeypatch, refused, made_it
):
    """`answers_from_spec` is the file branch's other door, and a repo follow-up skips the gate."""
    older, made = older_and_file
    monkeypatch.setattr(db, "get_repo_keys", lambda conversation_id: ["example-org/example-repo"])
    thread = [*BLOCKED_THREAD, {"role": "assistant", "content": REFUSED_LINE if refused else ANSWER}, *FILE_THREAD]
    with TestClient(app) as client:
        resp = _thread_turn(client, "spec-skip", thread, "what does this sheet have?")
    assert older == []
    assert made == ([] if refused else ["what does this sheet have?"])
    assert (NEW_CHAT_LINE in resp.text) is refused


@pytest.mark.parametrize("text", ["show it as a bar chart", "make it a pie chart", "and for last month?"])
def test_after_a_refusal_a_chart_or_a_question_still_goes_to_the_service(service, admin, older_route, text):
    with TestClient(app) as client:
        thread = [*BLOCKED_THREAD, {"role": "assistant", "content": PART_ANSWER}]
        assert _went_to(service, older_route, client, thread, text) == "bridge"


@pytest.mark.parametrize("who", ["member", "switch off"])
def test_the_file_rule_runs_for_nobody_else(service, as_user, older_route, monkeypatch, who):
    """Only the switch and the capability open this: "export this" holds no file
    word, so for everyone else it reaches `try_answer` as it always did."""
    seen: list = []

    async def try_answer(text, **kwargs):
        seen.append(text)
        return None

    def not_for_this_person(*args, **kwargs):
        raise AssertionError("the rule ran")

    if who == "member":
        as_user("sfa-member")
    else:
        as_user("sfa-admin", role="admin")
        monkeypatch.setattr(sfk_bridge, "ASSISTANT_ENABLED", False)
    monkeypatch.setattr(sfk_bridge, "try_answer", try_answer)
    monkeypatch.setattr(app_main, "_sf_assistant_leaves_it_to_the_files", not_for_this_person)
    with TestClient(app) as client:
        _thread_turn(client, "file-other", RECORDS_THREAD, "export this")
    assert seen == ["export this"]


def test_the_artifact_list_is_read_once_on_a_fallback_turn(service, admin, older_route, a_file_in_the_chat, monkeypatch):
    from app.engines import artifact as artifact_engine
    from app.engines import sf_intel

    listed: list = []

    async def hands_back(text, history, emit, **kwargs):
        return sf_intel.Outcome(handled=False, resolved_text=text)

    monkeypatch.setattr(sf_intel, "run", hands_back)
    monkeypatch.setattr(artifact_engine.adb, "list_artifacts", lambda viewer, conversation_id: listed.append(1) or [])
    service.answers("failed", answer="", kind="none", data_ts=None, objects=())
    with TestClient(app) as client:
        _thread_turn(client, "file-once", [], "how many interviews happened last month")
    assert len(service.calls) == 1
    assert len(listed) == 1


# ---------------------------------------------------------------------------
# sf_assistant: what else could carry an answer to the web
# ---------------------------------------------------------------------------


def _status_lines(resp) -> list:
    return [data["text"] for event, data in _sse(resp.text) if event == "status"]


WEB_OFF_LINE = "This chat may hold Salesforce answers, so web search stays off here. Start a new chat to search."


@pytest.mark.parametrize("effort", ["fast", "think", "max"])
@pytest.mark.parametrize("switch", [False, True], ids=["switch off", "switch on"])
def test_a_forced_search_in_a_held_chat_is_refused_out_loud(admin, web_open, monkeypatch, switch, effort):
    """The pill and the footer still say a search goes out. Deep Research and the
    rate limit both say why they did not search."""
    monkeypatch.setattr(sfk_bridge, "ASSISTANT_ENABLED", switch)
    _hold_an_answer(admin, "web-said")
    with TestClient(app) as client:
        resp = _ask_the_assistant(client, "web-said", web_search="on", effort=effort)
    assert web_open["search"] == []
    assert _status_lines(resp).count(WEB_OFF_LINE) == 1


def test_a_failed_read_says_so_too(admin, web_open, monkeypatch):
    def broken(conversation_id, engine):
        raise RuntimeError("database down")

    monkeypatch.setattr(db, "conversation_has_engine", broken)
    with TestClient(app) as client:
        resp = _ask_the_assistant(client, "web-said-broken", web_search="on")
    assert web_open["search"] == []
    assert _status_lines(resp).count(WEB_OFF_LINE) == 1


def test_only_a_forced_search_in_a_held_chat_is_told(admin, web_open, monkeypatch):
    _hold_an_answer(admin, "web-auto")
    with TestClient(app) as client:
        # Nobody asked for the web here, so there is nothing to refuse.
        assert WEB_OFF_LINE not in _status_lines(_ask_the_assistant(client, "web-auto"))
        # And nothing to refuse in a chat with no such answer.
        assert WEB_OFF_LINE not in _status_lines(_ask_the_assistant(client, "web-open", web_search="on"))
        # Nor where search is off for everyone.
        monkeypatch.setattr(settings, "search_enabled", False)
        assert WEB_OFF_LINE not in _status_lines(_ask_the_assistant(client, "web-auto", web_search="on"))


def test_a_greeting_on_the_fast_lane_does_no_read(admin, web_open, monkeypatch):
    """The lane never searches, so the read cannot change its answer, and it must
    add nothing to a greeting's latency."""
    reads: list = []
    monkeypatch.setattr(db, "conversation_has_engine", lambda *args: reads.append(args) or True)
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={"message": "hi", "mode": "assistant", "effort": "fast", "web_search": "auto", "conversation_id": "lane"},
        )
    assert resp.status_code == 200
    meta = [data for event, data in _sse(resp.text) if event == "meta"][-1]
    assert (meta.get("knowledge") or {}).get("decision") == "small_talk_lane"
    assert reads == []


def _web_history(monkeypatch) -> list:
    """Spy on the search engine's history, for a turn that reaches it."""
    seen: list = []

    async def run_search_engine(text, history, emit, effort="fast", **kwargs):
        seen.append(list(history))
        await emit("token", {"text": "From the web."})
        await emit("meta", {"route": "search"})
        return "From the web."

    monkeypatch.setattr(search_engine, "run_search_engine", run_search_engine)
    return seen


def test_an_answer_stays_out_of_the_in_process_memory(service, admin, older_route, monkeypatch):
    """This memory is read when a client sends no turns, and it holds no stamp."""
    kept: list = []
    monkeypatch.setattr(app_main.memory, "add_exchange", lambda session, text, answer, **_mode: kept.append(answer))
    with TestClient(app) as client:
        client.post("/chat", json=_body("memory-answer"))
        assert kept == []
        # Another engine's answer is kept as it always was.
        monkeypatch.setattr(sfk_bridge, "ASSISTANT_ENABLED", False)
        client.post("/chat", json=_body("memory-older"))
    assert len(kept) == 1


def test_an_emptied_chat_does_not_get_the_answer_back_from_memory(service, admin, older_route, web_open, monkeypatch):
    """Truncating to nothing removes the stamp with the row, so the web opens. What
    must not survive is the answer itself, in the history of that search."""
    history = _web_history(monkeypatch)
    # `wired` closes the Salesforce block, which is the door to the bridge.
    monkeypatch.setattr(settings, "clarify_before_answering", True)
    service.answers(answer="Delta Person had 7 interviews.")
    with TestClient(app) as client:
        assert client.post("/chat", json=_body("trunc", "how many interviews?", session_id="trunc")).status_code == 200
        assert len(service.calls) == 1, "not the case under test: the service did not answer"
        with db.connection() as con:
            total = con.execute("SELECT count(*) AS n FROM messages WHERE conversation_id = 'trunc'").fetchone()["n"]
        assert client.post("/history/conversations/trunc/truncate", json={"keep": 0, "expected_total": total}).status_code == 200
        _ask_the_assistant(client, "trunc", web_search="on")
    assert history, "not the case under test: the search engine did not run"
    assert "Delta Person" not in json.dumps(history)


def test_a_whole_thread_put_keeps_the_stamp(admin, web_open):
    _hold_an_answer(admin, "put-kept")
    with TestClient(app) as client:
        stored = client.get("/history/conversations/put-kept").json()["messages"]
        rows = [
            {"role": m["role"], "content": m["content"], "meta": {k: v for k, v in (m.get("meta") or {}).items() if k != "engine"}}
            for m in stored
        ]
        assert client.put("/history/conversations/put-kept/messages", json={"messages": rows}).status_code == 200
        assert db.conversation_has_engine("put-kept", "sf_assistant") is True
        _ask_the_assistant(client, "put-kept", web_search="on")
    assert web_open["search"] == []


def test_a_put_stamps_nothing_else_and_the_rows_it_appends_read_as_sent(admin):
    user_id = int(admin["id"])
    db.create_conversation(user_id, "put-others", "A chat")
    db.add_message(user_id, "put-others", "user", "a question")
    db.add_message(user_id, "put-others", "assistant", "an answer", {"route": "chat", "generation_id": "g1"})
    db.replace_messages(
        user_id,
        "put-others",
        [
            {"role": "user", "content": "a question"},
            {"role": "assistant", "content": "an answer", "meta": {"route": "chat", "generation_id": "g1"}},
            {"role": "user", "content": "another"},
        ],
    )
    assert db.conversation_has_engine("put-others", "sf_assistant") is False


def _two_chats(user: dict, name: str = "Echo Person") -> None:
    """A chat holding an sf_assistant answer, and one holding an older answer."""
    user_id = int(user["id"])
    db.create_conversation(user_id, "chat-sfa", "A chat")
    db.add_message(user_id, "chat-sfa", "user", "how many interviews did the candidate have?")
    db.add_message(
        user_id, "chat-sfa", "assistant", f"The candidate {name} had 7 interviews.", {"route": "sql", "engine": "sf_assistant"}
    )
    db.create_conversation(user_id, "chat-older", "Another chat")
    db.add_message(user_id, "chat-older", "assistant", "The older route counted 3 interviews.", {"route": "sql"})


def test_keyword_recall_leaves_out_an_sf_assistant_answer(admin):
    _two_chats(admin)
    hits = db.recall_conversations(int(admin["id"]), ["interviews"], "chat-now", 3)
    snippets = {hit["id"]: hit["snippet"] for hit in hits}
    # Its own question stays: those are the person's own words.
    assert snippets["chat-sfa"] == "how many interviews did the candidate have?"
    assert snippets["chat-older"] == "The older route counted 3 interviews."


def test_a_chat_with_nothing_but_such_an_answer_is_not_recalled(admin):
    user_id = int(admin["id"])
    db.create_conversation(user_id, "chat-bare", "A chat")
    db.add_message(user_id, "chat-bare", "assistant", "Echo Person had 7 interviews.", {"route": "sql", "engine": "sf_assistant"})
    assert db.recall_conversations(user_id, ["interviews"], "chat-now", 3) == []


def test_semantic_recall_never_gets_a_vector_for_such_an_answer(admin):
    _two_chats(admin)
    missing = db.messages_missing_embeddings(int(admin["id"]), "a-model")
    assert "The older route counted 3 interviews." in [row["content"] for row in missing]
    assert not [row for row in missing if "Echo Person" in row["content"]]


def test_the_agent_planner_never_sees_such_an_answer_from_another_chat(admin, wired, monkeypatch):  # noqa: F811
    """The chat this turn is in holds no answer, so the web stays open, and the
    agent planner's prompt keeps pinned blocks. A name recalled into it could go
    into a public query."""
    monkeypatch.setattr(settings, "search_enabled", True)
    monkeypatch.setattr(settings, "search_rate_per_min", 1000)
    monkeypatch.setattr(search_engine, "_rate", {})
    prompts: list = []
    queries: list = []

    async def collect(asked, *args, **kwargs):
        queries.extend(asked)
        return []

    async def chat_completion(messages, **kwargs):
        prompts.append(messages)
        # What a planner could write: it copies a name only if its prompt has one.
        said = " ".join(str(m.get("content")) for m in messages if m.get("role") != "user")
        target = "Echo Person" if "Echo Person" in said else "the candidate"
        return json.dumps(
            {"steps": [{"id": 1, "title": "Find the company", "kind": "web", "input": f"{target} current company"}]}
        )

    monkeypatch.setattr(search_engine, "_collect_results", collect)
    monkeypatch.setattr(llm, "chat_completion", chat_completion)
    _two_chats(admin)
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={
                "message": "find on the web what company the candidate we discussed earlier works at",
                "mode": "assistant",
                "effort": "think",
                "web_search": "on",
                "agent": True,
                "conversation_id": "chat-new",
                "session_id": "chat-new",
            },
        )
    assert resp.status_code == 200, resp.text
    assert db.conversation_has_engine("chat-new", "sf_assistant") is False
    assert prompts, "not the case under test: the planner did not run"
    assert "Echo Person" not in json.dumps(prompts)
    assert queries, "not the case under test: the web step did not run"
    assert not [query for query in queries if "Echo Person" in query]
