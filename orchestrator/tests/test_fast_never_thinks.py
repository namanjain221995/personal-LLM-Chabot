"""FAST NEVER THINKS (owner rule, 2026-09-17).

A turn the person asked for at Fast must not spend one token thinking, on any
path it reaches. Effort used to be a per-CALL argument, so the rule held only
where a caller remembered to pass it — and most callers underneath the answer
(a search fallback, a repo Q&A, a compaction summary, the route classifier's
main-model fallback, a chart question, an agent step) pass nothing, which
means `enable_thinking` true, because `wants_thinking`'s default effort is
"think".

This file pins the rule at the ONE place every main-model request passes:
`llm.mark_fast_turn` / `llm.fast_turn`, read inside `llm.reasoning_extra_body`
and by every entry point's sizing. Each test below fails on dev.

Two kinds of assertion:

- the chokepoint — with a Fast turn marked, every function that talks to the
  main model sends `enable_thinking` false and asks for no thinking budget,
  whatever effort, `thinking=` flag or answer plan the caller passed;
- the call sites — the calls a Fast turn actually makes now name their own
  thinking decision, so `meta` and the trace tell the truth about a Think turn
  instead of relying on the floor underneath them.

`test_think_and_max_still_think` is the guard in the other direction: nothing
here may turn a chosen reasoning pass off.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, llm, main, summarize
from app.config import settings
from app.engines import agent, repo, router, search, sql
from tests.conftest import _materialize_test_user

# A pasted job description: prose full of numbers, years and requirement
# lists. PR #71's lexicon read it as a "measurement" problem and opened a
# thinking grant on it (owner report, 2026-09-17) — the exact shape the rule
# has to survive. Synthetic; the only contact details are example.com / 555.
JOB_DESCRIPTION = """Senior Platform Engineer — Bengaluru (hybrid, 3 days on site)

About the role
We are hiring 2 engineers for the platform team. You will own a fleet of 40+
services behind a 99.95% availability target, with a p95 latency budget of
250 ms and an error budget of 0.05% measured over a rolling 28 days.

Requirements
- 5+ years building production services, at least 3 of them in Python
- Experience operating PostgreSQL 14 or later at 2 TB and above
- Comfortable reasoning about a ratio of 3:1 between read and write traffic
- A degree, or 7 years of equivalent experience

Compensation: 32,00,000 to 45,00,000 INR per year, reviewed every 12 months.
Apply to careers@example.com or call +1-555-0142.
"""


# ---------------------------------------------------------------------------
# A fake engine at the client boundary, plus a recorder for every send
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_fast_turn_leaks():
    """A ContextVar set in a test body outlives it; clear it both ways."""
    llm.mark_fast_turn(False)
    yield
    llm.mark_fast_turn(False)


def _chunk(content=None, reasoning=None, finish=None):
    delta = SimpleNamespace(reasoning=reasoning, content=content, model_extra=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=None
    )


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        return None


def _message(content="ok"):
    msg = SimpleNamespace(
        content=content, reasoning=None, model_extra=None, tool_calls=[]
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=None
    )


@pytest.fixture()
def sends(monkeypatch):
    """Record every request that reaches the main model.

    Patched at `_primary_send` / `_open_stream` — the module's own choke point
    — so the recording is of what would go on the wire, whichever entry point
    built it.
    """
    recorded: list = []

    async def passthrough(messages, *, base_url, model, requested_max_tokens=None, **kw):
        return list(messages), requested_max_tokens or 8192

    monkeypatch.setattr(llm.context, "fit_request", passthrough)
    monkeypatch.setattr(llm, "_openai_client", lambda *a, **kw: object())
    monkeypatch.setattr(llm, "_client", lambda *a, **kw: object())

    async def primary_send(client, request, **kw):
        recorded.append(dict(request))
        return _message()

    async def open_stream(client, request, **send):
        recorded.append(dict(request))
        return _FakeStream([_chunk(content="ok")])

    monkeypatch.setattr(llm, "_primary_send", primary_send)
    monkeypatch.setattr(llm, "_open_stream", open_stream)
    return recorded


def _thinking(request) -> bool:
    """`enable_thinking` as this request would send it."""
    return request["extra_body"]["chat_template_kwargs"]["enable_thinking"]


async def _drain(gen):
    return [item async for item in gen]


# ---------------------------------------------------------------------------
# 1. The chokepoint
# ---------------------------------------------------------------------------


def test_reasoning_extra_body_is_off_while_a_fast_turn_is_marked():
    caps = settings.main_capabilities
    assert llm.reasoning_extra_body(caps, True) == {
        "chat_template_kwargs": {"enable_thinking": True}
    }
    llm.mark_fast_turn(True)
    assert llm.fast_turn() is True
    assert llm.reasoning_extra_body(caps, True) == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    llm.mark_fast_turn(False)
    assert llm.fast_turn() is False
    assert _thinking({"extra_body": llm.reasoning_extra_body(caps, True)}) is True


def test_every_main_model_send_is_thinking_off_on_a_fast_turn(sends):
    """Every entry point, each asked for thinking as loudly as it can be."""

    async def scenario():
        llm.mark_fast_turn(True)
        await _drain(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="medium", max_tokens=1200,
        ))
        await llm.chat_completion([{"role": "user", "content": "q"}], max_tokens=1200, thinking=True)
        await _drain(llm.stream_chat_completion(
            [{"role": "user", "content": "q"}], max_tokens=1200, thinking=True,
        ))
        await llm.chat_completion_with_reasoning(
            [{"role": "user", "content": "q"}], max_tokens=1200, effort="high",
        )
        await llm.json_completion(
            [{"role": "user", "content": "q"}], max_tokens=1200, thinking=True, effort="max",
        )
        await llm.chat_with_tools(
            [{"role": "user", "content": "q"}],
            tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}],
            max_tokens=1200, thinking=True, effort="max",
        )

    asyncio.run(scenario())
    assert len(sends) == 6, sends
    for request in sends:
        assert _thinking(request) is False, request
        # No thinking budget was added, and the request was NOT floored at
        # MAX_OUTPUT_TOKENS — the floor exists to leave room for a reasoning
        # pass that is not happening.
        assert request["max_tokens"] == 1200, request
        assert request["max_tokens"] < settings.max_output_tokens
        assert "thinking_token_budget" not in request["extra_body"]["chat_template_kwargs"]


def test_an_answer_plan_cannot_turn_thinking_on_during_a_fast_turn(sends):
    plan = SimpleNamespace(enable_thinking=True, sampling={"top_p": 0.9})

    async def scenario():
        llm.mark_fast_turn(True)
        await _drain(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="think", max_tokens=1200, answer_plan=plan,
        ))

    asyncio.run(scenario())
    (request,) = sends
    assert _thinking(request) is False
    assert request["max_tokens"] == 1200


def test_think_and_max_still_think(sends):
    """The guard in the other direction: no Fast turn marked, nothing changes."""

    async def scenario():
        await _drain(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="think", max_tokens=1200,
        ))
        await _drain(llm.stream_chat_events(
            [{"role": "user", "content": "q"}], effort="max", max_tokens=1200,
        ))
        await llm.chat_completion([{"role": "user", "content": "q"}], max_tokens=1200, thinking=True)
        await llm.chat_completion_with_reasoning(
            [{"role": "user", "content": "q"}], max_tokens=1200, effort="high",
        )

    asyncio.run(scenario())
    assert [_thinking(r) for r in sends] == [True, True, True, True]
    # The unbounded-mode floor still applies to a thinking call.
    assert sends[0]["max_tokens"] == settings.max_output_tokens
    assert sends[3]["max_tokens"] == settings.max_output_tokens


# ---------------------------------------------------------------------------
# 2. The call sites a Fast turn reaches
# ---------------------------------------------------------------------------


def test_a_fast_search_fallback_is_thinking_off_and_says_search_unavailable(sends):
    """F07: the worker stack cannot reach SearXNG, so every search turn lands
    in `_fallback` — which ran at "think" whatever the person chose."""
    events: list = []

    async def emit(kind, data):
        events.append((kind, data))

    async def unavailable(*a, **kw):
        raise search.SearchUnavailableError("no searx")

    async def rewrite(message, history, effort):
        return [message]

    search_module = search

    async def scenario(effort):
        return await search_module.run_search_engine(
            "who won yesterday?", [], emit, effort=effort,
        )

    import unittest.mock as _mock

    with _mock.patch.object(search, "rewrite_queries", rewrite), \
         _mock.patch.object(search, "_collect_results", unavailable):
        asyncio.run(scenario("fast"))
        fast_request = sends[-1]
        events_fast = list(events)
        events.clear()
        sends.clear()
        asyncio.run(scenario("think"))
        think_request = sends[-1]

    assert _thinking(fast_request) is False
    assert _thinking(think_request) is True
    meta = [d for k, d in events_fast if k == "meta"][-1]
    assert meta["search_unavailable"] is True
    assert not any(k == "reasoning" for k, _ in events_fast)


def test_a_fast_repo_turn_is_thinking_off(sends, monkeypatch):
    events: list = []

    async def emit(kind, data):
        events.append((kind, data))

    async def run_in_thread(fn, *args, **kwargs):
        if fn is db.search_repo_chunks:
            return [{"path": "a.py", "start_line": 1, "end_line": 4, "text": "def f(): pass"}]
        return None

    monkeypatch.setattr(repo.db, "run_in_thread", run_in_thread)

    asyncio.run(repo.run_repo_engine("what does f do?", None, "conv", [], emit, "fast"))
    assert _thinking(sends[-1]) is False
    sends.clear()
    asyncio.run(repo.run_repo_engine("what does f do?", None, "conv", [], emit, "think"))
    assert _thinking(sends[-1]) is True


def test_compaction_summaries_never_think(sends):
    """A fold is a copying task. Thinking draws from the SAME budget as the
    summary, so a thinking fold can return nothing and silently drop the turns
    it was compacting — at every effort, not only at Fast."""
    turns = [{"role": "user", "content": "we chose Postgres 18"},
             {"role": "assistant", "content": "noted"}]
    asyncio.run(summarize.summarize("", turns))
    asyncio.run(summarize.condense("earlier: we chose Postgres 18"))
    assert [_thinking(r) for r in sends] == [False, False]


def test_the_route_classifier_fallback_and_the_sql_chart_call_never_think(sends, monkeypatch):
    """Both are small structured answers with a tight ceiling: a reasoning pass
    eats the whole ceiling and the caller reads the empty result as a
    decision."""

    async def router_dead(*a, **kw):
        raise RuntimeError("router endpoint down")

    monkeypatch.setattr(llm, "router_chat_completion", router_dead)
    asyncio.run(router.route_request("count my open opportunities"))
    classifier_request = sends[-1]
    assert _thinking(classifier_request) is False
    assert classifier_request["max_tokens"] == 50

    sends.clear()
    asyncio.run(sql._ask_chart_model([{"role": "user", "content": "chart it"}]))
    assert _thinking(sends[-1]) is False


def test_agent_steps_follow_effort(sends):
    """The planner and the step calls name the turn's level instead of
    inheriting chat_completion's thinking-on default."""
    from app.engines.agent import PlanStep, _run_step_impl, make_plan

    async def plan_at(effort):
        sends.clear()
        await make_plan("build me a launch plan", [], salesforce=False, effort=effort)
        return _thinking(sends[-1])

    async def step_at(effort):
        sends.clear()
        await _run_step_impl(
            PlanStep(id=1, title="t", kind="llm", input="do the thing"),
            [], False, effort,
        )
        return _thinking(sends[-1])

    assert asyncio.run(plan_at("fast")) is False
    assert asyncio.run(plan_at("think")) is True
    assert asyncio.run(step_at("fast")) is False
    assert asyncio.run(step_at("think")) is True


# ---------------------------------------------------------------------------
# 3. The route: the worker marks the turn, and meta tells the truth
# ---------------------------------------------------------------------------


def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        lines = [ln for ln in block.split("\n") if ln and not ln.startswith(":")]
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


@pytest.fixture()
def offline_chat(monkeypatch, sends):
    """POST /chat with every network boundary stubbed, the main model still
    reached through `llm`'s own choke point so `sends` sees the real requests."""
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "web_memory_enabled", False)
    monkeypatch.setattr(settings, "freshness_router_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_before_answering", False)
    monkeypatch.setenv("FAST_LANE_ENABLED", "false")

    async def router_chat_completion(messages, **kwargs):
        return json.dumps({"route": "chat"})

    async def embed_query(text, **kwargs):
        return [0.1] * 8

    async def embed_texts(texts, **kwargs):
        return [[0.1] * 8 for _ in texts]

    monkeypatch.setattr(llm, "router_chat_completion", router_chat_completion)
    monkeypatch.setattr(llm, "embed_query", embed_query)
    monkeypatch.setattr(llm, "embed_texts", embed_texts)
    _materialize_test_user("local")
    return sends


def _post(client, conversation_id: str, text: str, effort: str, mode: str = "assistant"):
    resp = client.post("/chat", json={
        "mode": mode,
        "effort": effort,
        "model": "smart",
        "web_search": "off",
        "conversation_id": conversation_id,
        "session_id": conversation_id,
        "messages": [{"role": "user", "content": text}],
    })
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    metas = [d for k, d in events if k == "meta" and "route" in d]
    return events, (metas[-1] if metas else {})


def test_the_chat_worker_marks_fast_turns(offline_chat):
    """The owner's shape: a pasted job description at Fast. PR #71's lexicon
    read it as a measurement problem and opened a thinking grant."""
    with TestClient(main.app) as client:
        events, meta = _post(client, "fast-never-1", JOB_DESCRIPTION, "fast")

    assert offline_chat, "the turn never reached the main model"
    for request in offline_chat:
        assert _thinking(request) is False, request
    assert not any(kind == "reasoning" for kind, _ in events)
    assert meta.get("effort") == "fast"


def test_a_think_turn_still_thinks_through_the_route(offline_chat):
    with TestClient(main.app) as client:
        _events, meta = _post(client, "fast-never-2", JOB_DESCRIPTION, "think")
    assert offline_chat
    assert any(_thinking(request) for request in offline_chat), offline_chat
    assert meta.get("effort") == "think"


def test_salesforce_meta_reports_the_turn_effort(offline_chat, monkeypatch):
    """meta is trust metadata: the sql/rag/report routes reported a hardcoded
    'think' whatever the person chose, which on a Fast turn was simply false."""
    events: list = []

    async def fake_rag(message, history, emit):
        await emit("token", {"text": "42 records"})
        await emit("meta", {"route": "rag", "citations": []})
        return "42 records"

    monkeypatch.setattr("app.engines.rag.run_rag_engine", fake_rag)

    async def router_chat_completion(messages, **kwargs):
        return json.dumps({"route": "rag"})

    monkeypatch.setattr(llm, "router_chat_completion", router_chat_completion)

    with TestClient(main.app) as client:
        events, meta = _post(
            client, "fast-never-3", "how many accounts do we have?", "fast",
            mode="salesforce",
        )
    assert meta["route"] == "rag"
    assert meta["effort"] == "fast"
