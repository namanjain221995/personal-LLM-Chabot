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

from app import llm
from app.config import settings
from app.engines import router as router_engine
from app.engines.router import FEW_SHOTS, ROUTES, parse_route
from app.main import app

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


def test_fast_resolves_to_router_model():
    base_url, _key, model = llm.resolve_model_choice("fast")
    assert base_url == settings.router_base_url
    assert model == settings.router_model
    assert llm.served_model_id("fast") == settings.router_model


def test_effort_is_expressed_as_thinking_not_a_system_line():
    """The gpt-oss "Reasoning: <effort>" line is gone.

    One model now serves both picker choices, so effort and the Smart/Fast
    switch mean exactly one thing: whether the reasoning pass runs. That is
    the real cost, and it is what the model actually honours.
    """
    messages = [{"role": "user", "content": "hi"}]
    assert llm.apply_reasoning_effort(messages, "high", "smart") == messages

    # Four levels on one model: the top two think, the bottom two answer directly.
    assert llm.wants_thinking("smart", "high") is True
    assert llm.wants_thinking("smart", "medium") is True
    assert llm.wants_thinking("smart", "low") is False
    assert llm.wants_thinking("smart", "fast") is False
    assert "fast" in llm.REASONING_EFFORTS and "low" in llm.REASONING_EFFORTS


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


def test_stream_chat_events_fast_model_no_effort_line(monkeypatch):
    events, rec = _collect_events(
        monkeypatch, [(None, "hey")], model_choice="fast", effort="high"
    )
    assert events == [("token", "hey")]
    assert rec["base_url"] == settings.router_base_url
    kwargs = rec["chat_kwargs"]
    assert kwargs["model"] == settings.router_model
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


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
    assert meta == {
        "route": "chat",
        "mode": "assistant",
        "model": settings.llm_model,
        "effort": "medium",
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
    assert events["meta"]["model"] == settings.router_model
    assert events["meta"]["effort"] == "low"
    assert rec["model_choice"] == "fast"
    assert rec["effort"] == "low"


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
        resp = client.post("/chat", json={"message": "hey there"})
    events = _parse_sse(resp.text)
    kinds = [e for e, _ in events]
    assert kinds == ["token", "meta", "done"]
    meta = dict(events[1][1])
    assert meta.pop("generation_id")
    assert meta == {
        "route": "chat",
        "mode": "salesforce",
        "model": settings.llm_model,
        "effort": "medium",
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
            "/chat", json={"message": "top accounts", "model": "fast", "effort": "high"}
        )
    assert resp.status_code == 200
    meta = dict(_parse_sse(resp.text))["meta"]
    assert meta["model"] == settings.llm_model      # the model that answered
    # The request asked for "fast" + "high"; the data engines are pinned to the
    # main model and report it honestly. (One model serves every path now, so
    # this is about the REPORTED id being the serving one, not the picker's.)
    assert meta["effort"] == "medium"                # picker effort not applied


def test_vision_route_meta_reports_vision_model(monkeypatch):
    """The vision route is served by the vision model; it has no reasoning
    effort, so meta carries the vision model id and omits effort (V2 §2)."""
    from app.engines import vision as vision_engine

    async def route_vision(message, has_image=False, history=()):
        return "vision"

    async def fake_vision_engine(message, image_base64, history, emit):
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
    assert "effort" not in meta


def test_agent_meta_reports_smart_model_and_applied_effort(monkeypatch):
    """Agent synthesis is pinned to the smart model (§3b) but honors the
    requested effort — meta must reflect exactly that."""
    from app.engines import agent as agent_engine

    async def fake_agent_engine(
        message, history, emit, *, effort="medium", salesforce=True, web=True
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
    assert meta["effort"] == "high"             # effort IS applied to synthesis


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
