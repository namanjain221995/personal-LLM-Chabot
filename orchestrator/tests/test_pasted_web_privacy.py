"""Pasted text never reaches a web search (hotfix 1.2, P6).

THE LEAK. A person pasted a ~10,000-character job description, one line of
their own ("... change it in the same way") and a sample. The freshness rule
read "report to the head of care technology" inside the paste as an
office-holder question, and the Fast pre-pass called the search provider with
the ENTIRE paste as the query - the person's pasted text, sent to third-party
engines through SearXNG (measured on production main 4e7cf8e: one query of
9,961 characters, verdict lexical:office).

THE BAR. No 40-character run of pasted material reaches a search provider's
`search()`, on any path that builds a web query from message text. One spy
test per path: every test below hands the real engine code a paste, lets the
query be built, and records what the provider was asked. The model calls are
stubbed to the WORST case - a query rewriter that copies whatever it was shown
- so the test proves what the code allows, not what a model happened to do.

Offline: a spy provider, no SearXNG, no engine. The /chat tests need the test
database like every other TestClient test.
"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app import llm
from app.config import settings
from app.engines import search as search_engine
from app.search.base import SearchResult

# A synthetic posting (no real company), shaped like the report: plain
# "Label value" lines, an office-holder phrase the freshness rule fires on
# ("head of"), then the person's one line, then a plain-lines sample.
POSTING = "\n".join(
    [
        "Senior Data Engineer - Claims Platform",
        "Orbiton Mutual Insurance Private Limited",
        "Location Pune, hybrid, in office Monday and Wednesday at the Baner campus",
        "Experience 7 to 10 years",
        "Employment type full time, permanent",
        "about orbiton",
        "orbiton mutual settles motor and health claims for 2.3 million policy holders across "
        "Maharashtra and Karnataka, and every claim passes through the platform this team owns.",
        "about the role",
        "you will report to the head of claims data engineering and lead the rebuild of our "
        "fraud scoring pipeline on the streaming stack we adopted last year.",
        "requirements",
        "7+ years building batch and streaming data pipelines in Python and SQL",
        "hands on experience with Kafka, Flink or Spark Structured Streaming",
        "has run a dbt project with more than 400 models in production",
        "can design a lakehouse table layout for slowly changing dimensions",
        "benefits",
        "family health insurance of 6 lakh rupees and a learning budget of 50,000 rupees a year",
    ]
)
ASK = "this is the requirement, use the sample format below and change it in the same way"
SAMPLE = "\n".join(
    [
        "sample format",
        "Job Title: Data Engineer",
        "Company: Example Retail Labs",
        "Location: Surat (Hybrid)",
        "Key Skills",
        "Python, SQL",
        "Must Have",
        "5+ years of data engineering.",
    ]
)
#: The reported shape: material, the person's line, material.
REWRITE = f"{POSTING}\n\n{ASK}\n\n{SAMPLE}"
#: A paste with a real question of the person's own at the end: the web may be
#: asked, but only that question.
QUESTION = "who is the current chief executive of Orbiton Mutual today?"
ASKED = f"{POSTING}\n\n{QUESTION}"

RUN = 40


def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def _leaks(queries, *materials):
    """Queries that carry a RUN-character substring of any material, compared
    both as sent and whitespace/case-normalised."""
    bad = []
    for q in queries:
        for m in materials:
            raw_hit = any(q[i : i + RUN] in m for i in range(max(0, len(q) - RUN + 1)))
            nq, nm = _norm(q), _norm(m)
            norm_hit = any(nq[i : i + RUN] in nm for i in range(max(0, len(nq) - RUN + 1)))
            if raw_hit or norm_hit:
                bad.append(q[:80])
                break
    return bad


class _SpyProvider:
    """Records every query a search provider is asked, answers one result."""

    def __init__(self):
        # A fresh name per test: the query cache is keyed by provider name,
        # and a cached query would never reach the spy.
        self.name = f"spy-{uuid.uuid4().hex[:8]}"
        self.unresponsive = {}
        self.queries = []

    async def search(self, query, max_results=10, categories=""):
        self.queries.append(query)
        return [
            SearchResult(
                title=f"Result {len(self.queries)}",
                url=f"https://example.org/r{len(self.queries)}",
                snippet="a snippet",
            )
        ]


def _copying_router(seen: list):
    """The WORST-case query rewriter: it copies everything it was shown (the
    earlier turns and the message) into the queries, whole and as slices.
    Every other router call (the orchestration classifier, the should-search
    yes/no) answers so the turn reaches a search."""

    async def fake(messages, **kwargs):
        system = str(messages[0].get("content", "")) if messages else ""
        shown = "\n".join(str(m.get("content", "")) for m in messages[1:])
        seen.append(shown)
        if "route a user's request" in system:
            return json.dumps({"agent": False, "search": True})
        if "web-search queries" in system:
            last = str(messages[-1].get("content", ""))
            return json.dumps([shown, shown[:120], last[:120], "claims platform data engineer"])
        return "yes"

    return fake


async def _no_stream(messages, **kwargs):
    yield ("token", "An answer.")


@pytest.fixture
def spy(monkeypatch):
    provider = _SpyProvider()
    monkeypatch.setattr(settings, "search_enabled", True)
    monkeypatch.setattr(search_engine, "get_provider", lambda: provider)

    async def no_fetch(results, question="", **kwargs):
        return []

    monkeypatch.setattr(search_engine, "_fetch_sources", no_fetch)
    monkeypatch.setattr(search_engine, "_spawn", lambda coro: coro.close())
    return provider


def _materials():
    return (POSTING, SAMPLE)


# ---------------------------------------------------------------------------
# 1. living_knowledge: the Fast freshness lookup (the reported path)
# ---------------------------------------------------------------------------


def _stale_store(monkeypatch):
    """The local store has nothing, so a time-sensitive verdict reaches the
    Fast lookup - exactly the production turn."""
    from app import living_knowledge as lk
    from app.freshness import Freshness
    from app.web_memory import Retrieval

    async def empty(question, *, level=Freshness.RECENT, **kwargs):
        return Retrieval(query=question, freshness=level)

    monkeypatch.setattr(lk, "retrieve", empty)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "freshness_fast_lookup", True)
    monkeypatch.setattr(settings, "freshness_router_enabled", False)
    monkeypatch.setattr(settings, "living_knowledge_topical", False)
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 0.0, raising=False)
    return lk


def test_the_fast_freshness_lookup_never_searches_a_pasted_rewrite(monkeypatch, spy):
    lk = _stale_store(monkeypatch)
    asyncio.run(
        lk.prepare(REWRITE, effort="fast", mode="assistant", web_search_pref="auto", allow_network=True)
    )
    assert spy.queries == [], "a rewrite of pasted text has nothing to look up on the web"


def test_the_fast_freshness_lookup_for_a_question_about_a_paste_sends_only_the_question(monkeypatch, spy):
    lk = _stale_store(monkeypatch)
    asyncio.run(
        lk.prepare(ASKED, effort="fast", mode="assistant", web_search_pref="auto", allow_network=True)
    )
    # The question the person typed under the paste is what the web is asked.
    assert spy.queries == [QUESTION]
    assert _leaks(spy.queries, *_materials()) == []


# ---------------------------------------------------------------------------
# 2. The search engine (auto-decided or forced web search)
# ---------------------------------------------------------------------------


def test_the_search_engine_builds_its_queries_from_the_persons_words(monkeypatch, spy):
    seen: list = []
    monkeypatch.setattr(llm, "router_chat_completion", _copying_router(seen))
    monkeypatch.setattr(llm, "stream_chat_events", _no_stream)

    async def emit(kind, data):
        return None

    asyncio.run(search_engine.run_search_engine(ASKED, [], emit, "think"))
    assert spy.queries, "the person asked a question; the web is searched for it"
    assert _leaks(spy.queries, *_materials()) == []
    # The rewriter is never shown the paste, so it cannot paraphrase it either.
    assert _leaks(seen, *_materials()) == []


def test_an_earlier_turns_paste_is_not_rewritten_into_this_turns_queries(monkeypatch, spy):
    seen: list = []
    monkeypatch.setattr(llm, "router_chat_completion", _copying_router(seen))
    monkeypatch.setattr(llm, "stream_chat_events", _no_stream)

    async def emit(kind, data):
        return None

    history = [
        {"role": "user", "content": REWRITE},
        {"role": "assistant", "content": "Done."},
    ]
    asyncio.run(search_engine.run_search_engine(QUESTION, history, emit, "think"))
    assert _leaks(spy.queries, *_materials()) == []
    assert _leaks(seen, *_materials()) == []


def test_the_last_door_drops_a_query_that_carries_the_turns_paste(monkeypatch, spy):
    """Whatever a caller builds, `_collect_results` is the one door to the
    provider: a query carrying the turn's paste does not pass it."""
    from app.core import pasted

    async def turn():
        # Marked inside the task, as main.py does: the mark is scoped to
        # this turn's context and never leaks into another test.
        pasted.mark_turn(REWRITE)
        return await search_engine._collect_results(
            [POSTING[200:300], "claims data engineer pune"], "fast"
        )

    found = asyncio.run(turn())
    assert spy.queries == ["claims data engineer pune"]
    assert found


# ---------------------------------------------------------------------------
# 3. The agent's web step
# ---------------------------------------------------------------------------


def test_an_agent_web_step_never_searches_the_paste(monkeypatch, spy):
    from app.engines import agent

    seen: list = []
    monkeypatch.setattr(llm, "router_chat_completion", _copying_router(seen))
    monkeypatch.setattr(llm, "stream_chat_events", _no_stream)

    async def planner(messages, **kwargs):
        # The planner hands the web step the whole message: what
        # `ensure_web_step` does when the person forced the web on.
        return json.dumps(
            {"steps": [{"id": 1, "title": "Research", "kind": "web", "input": ASKED}]}
        )

    monkeypatch.setattr(llm, "chat_completion", planner)

    async def emit(kind, data):
        return None

    asyncio.run(
        agent.run_agent_engine(
            ASKED, [], emit, effort="think", salesforce=False, web=True, web_forced=True
        )
    )
    assert spy.queries
    assert _leaks(spy.queries, *_materials()) == []


# ---------------------------------------------------------------------------
# 4. Deep Research (the /deep-research slash command lands here)
# ---------------------------------------------------------------------------


def _wire_research(monkeypatch, plan):
    from app.engines import deep_research as dr

    async def fake_json(messages, **kw):
        if kw.get("schema_name") == "research_plan":
            return plan if isinstance(plan, str) else json.dumps(plan)
        return json.dumps({"sufficient": True, "missing": [], "followup_queries": []})

    async def fake_rerank(message, res, target, **kw):
        return res

    async def fake_fetch(res, message="", **kw):
        return []

    monkeypatch.setattr(dr.llm, "json_completion", fake_json)
    monkeypatch.setattr(dr.llm, "stream_chat_events", _no_stream)
    monkeypatch.setattr(dr, "_rerank_results", fake_rerank)
    monkeypatch.setattr(dr, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(dr, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(dr.db, "create_research_run", lambda *a, **k: 1)
    monkeypatch.setattr(dr.db, "finish_research_run", lambda *a, **k: None)
    return dr


def test_deep_research_drops_a_planned_query_that_copies_the_paste(monkeypatch, spy):
    dr = _wire_research(
        monkeypatch,
        {
            "subquestions": ["who leads the company"],
            "queries": [POSTING[150:260], "Orbiton Mutual chief executive"],
        },
    )

    async def emit(kind, data):
        return None

    asyncio.run(dr.run_deep_research_engine(ASKED, [], emit, effort="fast", conversation_id="c1"))
    assert "Orbiton Mutual chief executive" in spy.queries
    assert _leaks(spy.queries, *_materials()) == []


def test_deep_research_falls_back_to_the_persons_words_not_the_paste(monkeypatch, spy):
    dr = _wire_research(monkeypatch, "not json at all")

    async def emit(kind, data):
        return None

    asyncio.run(dr.run_deep_research_engine(ASKED, [], emit, effort="fast", conversation_id="c1"))
    assert _leaks(spy.queries, *_materials()) == []


# ---------------------------------------------------------------------------
# 5. The artifact composer's web research
# ---------------------------------------------------------------------------


def test_artifact_web_research_never_searches_the_paste(monkeypatch, spy):
    from app.engines import artifact

    seen: list = []
    monkeypatch.setattr(llm, "router_chat_completion", _copying_router(seen))

    async def emit(kind, data):
        return None

    asyncio.run(
        artifact._web_sources(
            f"make a one-page brief of this posting as a document\n\n{POSTING}\n\n{QUESTION}",
            [],
            effort="max",
            user_id=1,
            conversation_id="c1",
            emit=emit,
        )
    )
    assert _leaks(spy.queries, *_materials()) == []


# ---------------------------------------------------------------------------
# 6. Through /chat: orchestration, the slash commands, and the reported turn
# ---------------------------------------------------------------------------


def _chat(monkeypatch, spy, body):
    from app.main import app

    seen: list = []
    monkeypatch.setattr(llm, "router_chat_completion", _copying_router(seen))
    monkeypatch.setattr(llm, "stream_chat_events", _no_stream)
    monkeypatch.setattr(settings, "deep_research_enabled", True)
    with TestClient(app) as client:
        resp = client.post("/chat", json={"mode": "assistant", **body})
    assert resp.status_code == 200
    return resp.text


def test_orchestration_auto_search_on_a_paste_sends_only_the_persons_words(monkeypatch, spy):
    _chat(monkeypatch, spy, {"message": ASKED, "effort": "think", "web_search": "auto"})
    assert spy.queries, "the orchestration call asked for a search"
    assert _leaks(spy.queries, *_materials()) == []


def test_the_search_slash_command_over_a_paste_never_searches_the_paste(monkeypatch, spy):
    # `/search <text>` is sent as web_search="on" (frontend/lib/slashCommands.ts).
    _chat(monkeypatch, spy, {"message": REWRITE, "effort": "fast", "web_search": "on"})
    assert _leaks(spy.queries, *_materials()) == []


def test_the_deep_research_slash_command_over_a_paste_never_searches_the_paste(monkeypatch, spy):
    # `/deep-research <text>` is sent as deep_research=true.
    _wire_research(
        monkeypatch,
        {"subquestions": ["a"], "queries": [POSTING[300:420], "claims data engineering pune"]},
    )
    _chat(
        monkeypatch,
        spy,
        {"message": ASKED, "effort": "think", "web_search": "auto", "deep_research": True},
    )
    assert "claims data engineering pune" in spy.queries
    assert _leaks(spy.queries, *_materials()) == []


def test_the_reported_fast_rewrite_turn_makes_no_web_query(monkeypatch, spy):
    _stale_store(monkeypatch)
    _chat(monkeypatch, spy, {"message": REWRITE, "effort": "fast", "web_search": "auto"})
    assert spy.queries == []
