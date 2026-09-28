"""Think and Max start the knowledge pre-pass BESIDE decide() (2026-09-29).

WHAT WAS MEASURED. At Think and Max `orchestrate.decide` is a router round
trip — live traces, REQUEST_RECEIVED to MODE_RESOLVED, p50 555/655 ms and
p95 1,057/1,324 ms (Think/Max, 14 days) — and the knowledge pre-pass was
dispatched only after it returned, although the only dispatch gate that
reads the plan is "the agent is not answering". So the first reasoning text
waited for both, in series.

Pinned here, through the real /chat handler with every engine stubbed:
  * with decide() taking 500 ms and the pre-pass 400 ms, the pre-pass starts
    before decide() returns;
  * when decide() picks the agent, the pre-pass is cancelled unread;
  * a Think turn whose plan wants a search still gets the pre-pass verdict
    exactly as before: a confident local answer cancels the search, and a
    stale one lets it run, spending one slot of the search window;
  * CHAT_PREPASS_BESIDE_DECIDE=false restores the old order;
  * Fast keeps its order (its decide() makes no router call).
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import living_knowledge, main
from app.config import settings
from app.engines import agent as agent_engine
from app.engines import orchestrate
from app.engines import search as search_engine
from app.engines.orchestrate import Plan
from app.living_knowledge import Prepared
from tests.conftest import _materialize_test_user
from tests.test_fast_lane_route import _send, wired  # noqa: F401


@pytest.fixture
def think(monkeypatch, wired):  # noqa: F811
    monkeypatch.setattr(settings, "search_enabled", True)
    monkeypatch.setattr(settings, "search_rate_per_min", 10)
    monkeypatch.setattr(search_engine, "_rate", {})
    return {"key": str(_materialize_test_user("local")["id"]), "wired": wired}


def _stub_decide(monkeypatch, plan: Plan, seconds: float, marks: dict) -> None:
    async def decide(message, history, effort):
        marks["decide_started"] = time.perf_counter()
        await asyncio.sleep(seconds)
        marks["decide_returned"] = time.perf_counter()
        return plan

    monkeypatch.setattr(orchestrate, "decide", decide)


def test_at_think_the_prepass_starts_before_decide_returns(think, monkeypatch):
    marks: dict = {}
    _stub_decide(monkeypatch, Plan(agent=False, search=False), 0.5, marks)

    async def prepare(question, *, effort, allow_network, emit=None, **kw):
        marks["prepare_started"] = time.perf_counter()
        marks["effort"] = effort
        await asyncio.sleep(0.4)
        marks["prepare_returned"] = time.perf_counter()
        return Prepared(decision="static_model")

    monkeypatch.setattr(living_knowledge, "prepare", prepare)

    with TestClient(main.app) as client:
        events, meta = _send(
            client, "think-conv-1", [{"role": "user", "content": "explain how a balance sheet works"}],
            effort="think",
        )

    assert marks["effort"] == "think"
    assert marks["prepare_started"] < marks["decide_returned"], (
        "the pre-pass waited for decide(): started "
        f"{(marks['prepare_started'] - marks['decide_returned']) * 1000:.0f} ms after it returned"
    )
    # Overlapped, the pre-pass (400 ms) is done by the time decide (500 ms) is.
    assert marks["prepare_returned"] <= marks["decide_returned"] + 0.05
    assert meta["knowledge"]["decision"] == "static_model"
    assert "".join(d["text"] for k, d in events if k == "token") == "Hello! How can I help?"


def test_when_decide_picks_the_agent_the_prepass_is_cancelled_unread(think, monkeypatch):
    marks: dict = {}
    _stub_decide(monkeypatch, Plan(agent=True, search=False), 0.2, marks)

    async def prepare(question, *, allow_network, emit=None, **kw):
        marks["prepare_started"] = time.perf_counter()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            marks["prepare_cancelled"] = time.perf_counter()
            raise
        return Prepared(decision="local")  # pragma: no cover - never reached

    monkeypatch.setattr(living_knowledge, "prepare", prepare)

    async def run_agent_engine(message, history, emit, **kwargs):
        marks["agent_ran"] = True
        await emit("token", {"text": "planned"})
        await emit("meta", {"route": "agent", "effort": kwargs.get("effort")})
        return "planned"

    monkeypatch.setattr(agent_engine, "run_agent_engine", run_agent_engine)

    with TestClient(main.app) as client:
        events, meta = _send(
            client, "think-conv-2", [{"role": "user", "content": "plan a three-week onboarding programme"}],
            effort="think",
        )

    assert marks.get("agent_ran") is True
    assert meta["route"] == "agent"
    assert "prepare_started" in marks, "the pre-pass was not started beside decide()"
    assert marks["prepare_cancelled"] >= marks["decide_returned"], "cancelled before the plan was known"
    assert "knowledge" not in meta or not meta["knowledge"].get("decision")


class _LocalFirst(Prepared):
    @property
    def local_first(self) -> bool:  # the store answers with confidence
        return True


def test_a_think_turn_with_an_auto_search_is_answered_locally_when_the_store_is_confident(think, monkeypatch):
    marks: dict = {}
    _stub_decide(monkeypatch, Plan(agent=False, search=True), 0.05, marks)
    searched: list = []

    async def run_search_engine(*args, **kwargs):
        searched.append(args)
        return "searched"

    monkeypatch.setattr(search_engine, "run_search_engine", run_search_engine)
    allow: list = []

    async def prepare(question, *, allow_network, emit=None, **kw):
        allow.append(allow_network)
        return _LocalFirst(
            grounding="STORED-GROUNDING-7",
            decision="local",
            sources=[{"n": 1, "title": "Stored page", "url": "https://example.com/p"}],
        )

    monkeypatch.setattr(living_knowledge, "prepare", prepare)

    with TestClient(main.app) as client:
        events, meta = _send(
            client, "think-conv-3", [{"role": "user", "content": "who is the current CEO of Example Corp?"}],
            effort="think",
        )

    assert searched == [], "local-first did not cancel the auto search"
    statuses = [d.get("text") for k, d in events if k == "status"]
    assert "Answering from stored knowledge…" in statuses, statuses
    assert meta["knowledge"]["decision"] == "local"
    assert meta["auto"]["local_first"] is True and meta["auto"]["search"] is False
    prompt = think["wired"]["prompts"][-1]
    assert any("STORED-GROUNDING-7" in str(m.get("content")) for m in prompt)
    # The search the plan wanted took its slot when it was decided, as the
    # per-turn slot did before; nothing else took one.
    assert len(search_engine._rate.get(think["key"], [])) == 1
    # Dispatched before the plan was known, the pre-pass is offered the
    # network it would get with no search planned; at Think that only changes
    # whether a stale result is labelled escalate_search, and escalation is
    # gated on `not want_search`. Not pinned here: this test is the guard
    # that the ANSWER is the same either way.
    assert len(allow) == 1


def test_a_think_turn_with_an_auto_search_still_searches_when_the_store_is_stale(think, monkeypatch):
    marks: dict = {}
    _stub_decide(monkeypatch, Plan(agent=False, search=True), 0.05, marks)
    searched: list = []

    async def run_search_engine(message, history, emit, *args, **kwargs):
        searched.append(message)
        await emit("token", {"text": "from the web"})
        await emit("meta", {"route": "search", "effort": "think"})
        return "from the web"

    monkeypatch.setattr(search_engine, "run_search_engine", run_search_engine)

    async def prepare(question, *, allow_network, emit=None, **kw):
        return Prepared(grounding="OLD", decision="stale_offline")

    monkeypatch.setattr(living_knowledge, "prepare", prepare)

    with TestClient(main.app) as client:
        events, meta = _send(
            client, "think-conv-4", [{"role": "user", "content": "who won the match yesterday?"}],
            effort="think",
        )

    assert len(searched) == 1
    assert meta["route"] == "search"
    statuses = [d.get("text") for k, d in events if k == "status"]
    assert not any("rate limit" in (s or "") for s in statuses), statuses
    assert len(search_engine._rate.get(think["key"], [])) == 1


def test_with_the_setting_off_think_keeps_the_old_order(think, monkeypatch):
    monkeypatch.setattr(settings, "chat_prepass_beside_decide", False)
    marks: dict = {}
    _stub_decide(monkeypatch, Plan(agent=False, search=False), 0.2, marks)

    async def prepare(question, *, allow_network, emit=None, **kw):
        marks["prepare_started"] = time.perf_counter()
        return Prepared(decision="static_model")

    monkeypatch.setattr(living_knowledge, "prepare", prepare)

    with TestClient(main.app) as client:
        _send(client, "think-conv-5", [{"role": "user", "content": "explain double-entry bookkeeping"}], effort="think")

    assert marks["prepare_started"] >= marks["decide_returned"]


def test_fast_keeps_its_order(think, monkeypatch):
    marks: dict = {}
    _stub_decide(monkeypatch, Plan(agent=False, search=False), 0.1, marks)

    async def prepare(question, *, allow_network, emit=None, **kw):
        marks["prepare_started"] = time.perf_counter()
        return Prepared(decision="static_model")

    monkeypatch.setattr(living_knowledge, "prepare", prepare)

    with TestClient(main.app) as client:
        _send(client, "fast-conv-6", [{"role": "user", "content": "explain double-entry bookkeeping"}])

    assert marks["prepare_started"] >= marks["decide_returned"]
