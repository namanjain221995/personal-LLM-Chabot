"""Deep Research: the iterative loop, its budgets, and citation integrity.

Web Search answers in one pass; Deep Research plans, searches, reads, audits
what is missing, searches again, and only then writes. These tests pin the
parts that are easy to get quietly wrong:

* the loop TERMINATES — on sufficiency, on the iteration cap, and when the
  auditor asks for follow-ups that were already run;
* a citation the model invented is REMOVED. The search engine's only defence
  is a sentence in the prompt and the frontend strips [n] before rendering,
  so an invented [99] there is invisible rather than caught. A report is a
  document people quote, so here it is checked against the registry;
* the engine never raises into the request — a dead search provider, a
  malformed plan and a failed fetch all degrade to an honest answer;
* the dispatch reaches it at all: `orchestrate.decide()` classifies exactly
  this multi-part phrasing as agent=true, so a Deep Research request placed
  below the agent branch would be silently eaten by the planner.

Everything is offline: no vLLM, no SearXNG, no network.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.engines import deep_research as dr
from app.main import app
from app.search.base import SearchResult, SearchUnavailableError


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emitter():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    return events, emit


def _results(n, host="example.com"):
    return [
        SearchResult(
            title=f"Doc {i}", url=f"https://{host}/p{i}", snippet=f"snippet {i}"
        )
        for i in range(1, n + 1)
    ]


def _sources(n, host="example.com"):
    from app.engines.search import _Source

    return [
        _Source(n=i, title=f"Doc {i}", url=f"https://{host}/p{i}", text=f"body {i} " * 30)
        for i in range(1, n + 1)
    ]


def _wire(monkeypatch, *, plan=None, gap=None, results=None, sources=None, report="Report [1]."):
    """Stub every outside dependency of the loop with canned answers."""
    plan = plan or {"subquestions": ["a", "b"], "queries": ["q1", "q2"]}
    gaps = list(gap or [{"sufficient": True, "missing": [], "followup_queries": []}])

    async def fake_json_completion(messages, **kw):
        name = kw.get("schema_name")
        if name == "research_plan":
            return json.dumps(plan)
        return json.dumps(gaps.pop(0) if len(gaps) > 1 else gaps[0])

    async def fake_collect(queries, effort="medium", emit=None, categories=""):
        return list(results if results is not None else _results(4))

    async def fake_rerank(message, res, target):
        return res

    async def fake_fetch(res, message=""):
        pool = sources if sources is not None else _sources(len(res))
        # One source per result, exactly like the real _fetch_sources.
        return list(pool[: len(res)])

    async def fake_stream(messages, **kw):
        # Streamed in pieces, like the real one — a single-chunk fake would
        # hide a regression back to buffering.
        for piece in (report[i : i + 8] for i in range(0, len(report), 8)):
            yield ("token", piece)

    monkeypatch.setattr(dr.llm, "json_completion", fake_json_completion)
    monkeypatch.setattr(dr.llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(dr, "_collect_results", fake_collect)
    monkeypatch.setattr(dr, "_rerank_results", fake_rerank)
    monkeypatch.setattr(dr, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(dr, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(dr.db, "create_research_run", lambda *a, **k: 1)
    monkeypatch.setattr(dr.db, "finish_research_run", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# Citation integrity — the reason this engine exists rather than a long search
# ---------------------------------------------------------------------------


def test_a_fabricated_citation_is_removed():
    report, invalid = dr.validate_citations(
        "Real claim [1]. Invented claim [99]. Another real one [2].", 2
    )
    assert invalid == [99]
    assert "[99]" not in report
    assert "[1]" in report and "[2]" in report


def test_valid_citations_survive_untouched():
    text = "A [1] B [2] C [3]."
    report, invalid = dr.validate_citations(text, 3)
    assert invalid == []
    assert report == text


def test_zero_and_out_of_range_are_both_invalid():
    report, invalid = dr.validate_citations("[0] and [1] and [4]", 3)
    assert sorted(invalid) == [0, 4]
    assert "[1]" in report


def test_the_report_only_ever_cites_real_sources(monkeypatch):
    # The model writes [1] and [7]; only two sources were gathered, so [7]
    # must not reach the user and must not appear in the cited count.
    _wire(monkeypatch, sources=_sources(2), report="Claim [1] and claim [7].")
    events, emit = _emitter()
    out = asyncio.run(
        dr.run_deep_research_engine("q", [], emit, effort="fast", conversation_id="c1")
    )
    assert "[7]" not in out
    assert "[1]" in out
    meta = [p for k, p in events if k == "meta"][-1]
    assert meta["research_run"]["invalid_citations_removed"] == 1
    assert meta["research_run"]["sources_cited"] == 1


def test_every_meta_source_maps_to_a_fetched_page(monkeypatch):
    _wire(monkeypatch, sources=_sources(3))
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    meta = [p for k, p in events if k == "meta"][-1]
    assert [s["n"] for s in meta["sources"]] == [1, 2, 3]
    assert all(s["url"].startswith("https://") for s in meta["sources"])
    assert all(s["domain"] for s in meta["sources"])


# ---------------------------------------------------------------------------
# Termination — a loop that cannot stop is the failure mode that matters
# ---------------------------------------------------------------------------


def test_stops_as_soon_as_the_evidence_is_sufficient(monkeypatch):
    _wire(monkeypatch, gap=[{"sufficient": True, "missing": [], "followup_queries": []}])
    monkeypatch.setattr(settings, "deep_research_min_sources", 1)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    meta = [p for k, p in events if k == "meta"][-1]
    assert meta["research_run"]["iterations"] == 1


def test_never_exceeds_the_iteration_cap(monkeypatch):
    # An auditor that is never satisfied and always asks for something new.
    calls = {"n": 0}

    async def never_enough(messages, **kw):
        if kw.get("schema_name") == "research_plan":
            return json.dumps({"subquestions": ["a"], "queries": ["q0"]})
        calls["n"] += 1
        return json.dumps(
            {
                "sufficient": False,
                "missing": ["more"],
                "followup_queries": [f"follow-{calls['n']}"],
            }
        )

    _wire(monkeypatch)
    monkeypatch.setattr(dr.llm, "json_completion", never_enough)
    monkeypatch.setattr(settings, "deep_research_max_iterations", 3)
    monkeypatch.setattr(settings, "deep_research_min_sources", 1)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    meta = [p for k, p in events if k == "meta"][-1]
    assert meta["research_run"]["iterations"] == 3


def test_stops_when_the_auditor_only_repeats_queries_already_run(monkeypatch):
    async def repeats(messages, **kw):
        if kw.get("schema_name") == "research_plan":
            return json.dumps({"subquestions": ["a"], "queries": ["q1"]})
        # Asking again for exactly what was already searched must not loop.
        return json.dumps(
            {"sufficient": False, "missing": ["x"], "followup_queries": ["q1"]}
        )

    _wire(monkeypatch)
    monkeypatch.setattr(dr.llm, "json_completion", repeats)
    monkeypatch.setattr(settings, "deep_research_min_sources", 1)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    meta = [p for k, p in events if k == "meta"][-1]
    assert meta["research_run"]["iterations"] == 1


def test_the_source_cap_is_respected(monkeypatch):
    _wire(monkeypatch, results=_results(50), sources=_sources(50))
    monkeypatch.setattr(settings, "deep_research_max_sources", 8)
    monkeypatch.setattr(settings, "deep_research_sources_per_iteration", 8)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    meta = [p for k, p in events if k == "meta"][-1]
    assert len(meta["sources"]) <= 8


# ---------------------------------------------------------------------------
# Degradation — nothing here may raise into the request
# ---------------------------------------------------------------------------


def test_a_dead_search_provider_answers_honestly(monkeypatch):
    _wire(monkeypatch)

    async def dead(*a, **k):
        raise SearchUnavailableError("searxng down")

    monkeypatch.setattr(dr, "_collect_results", dead)
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert "could not gather" in out.lower()
    meta = [p for k, p in events if k == "meta"][-1]
    assert meta["sources"] == []
    # Nothing was invented to fill the gap.
    assert "[1]" not in out


def test_a_malformed_plan_falls_back_to_the_raw_question(monkeypatch):
    _wire(monkeypatch)

    async def garbage(messages, **kw):
        if kw.get("schema_name") == "research_plan":
            return "not json at all"
        return json.dumps({"sufficient": True, "missing": [], "followup_queries": []})

    monkeypatch.setattr(dr.llm, "json_completion", garbage)
    events, emit = _emitter()
    out = asyncio.run(
        dr.run_deep_research_engine("what is X", [], emit, conversation_id="c1")
    )
    assert out  # still answered
    meta = [p for k, p in events if k == "meta"][-1]
    assert meta["research_run"]["queries"] == ["what is X"]


def test_a_failing_fetch_round_does_not_end_the_run(monkeypatch):
    _wire(monkeypatch)

    async def boom(res, message=""):
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(dr, "_fetch_sources", boom)
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert "could not gather" in out.lower()


def test_pages_already_read_are_not_fetched_twice(monkeypatch):
    seen = []

    async def recording_fetch(res, message=""):
        seen.append([r.url for r in res])
        return _sources(len(res))

    _wire(monkeypatch, gap=[
        {"sufficient": False, "missing": ["m"], "followup_queries": ["q-follow"]},
        {"sufficient": True, "missing": [], "followup_queries": []},
    ])
    monkeypatch.setattr(dr, "_fetch_sources", recording_fetch)
    monkeypatch.setattr(settings, "deep_research_min_sources", 1)
    monkeypatch.setattr(settings, "deep_research_max_iterations", 3)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    # The second round searched again but every URL was already read, so it
    # must not have re-fetched any of them.
    assert len(seen) >= 1
    if len(seen) > 1:
        assert not (set(seen[0]) & set(seen[1]))


# ---------------------------------------------------------------------------
# Category routing — the untapped SearXNG pools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, expected",
    [
        ("which papers evaluate this approach", "science"),
        ("state of the art survey", "science"),
        # NOT "it": that pool is github/stackoverflow/mdn/Docker Hub, and a
        # live run brought back 9 Docker Hub image pages out of 23 sources.
        ("how to install the python package", ""),
        ("vllm memory usage", ""),
        ("who is the mayor of Paris", ""),
        ("", ""),
    ],
)
def test_route_category(query, expected):
    assert dr.route_category(query) == expected


def test_each_routed_group_is_searched_in_its_own_pool(monkeypatch):
    calls = []

    async def recording_collect(queries, effort="medium", emit=None, categories=""):
        calls.append((tuple(queries), categories))
        return _results(2)

    _wire(
        monkeypatch,
        plan={
            "subquestions": ["a"],
            "queries": ["which papers evaluate this", "who is the mayor"],
        },
    )
    monkeypatch.setattr(dr, "_collect_results", recording_collect)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    routed = {cat for _q, cat in calls}
    assert routed == {"science", ""}


# ---------------------------------------------------------------------------
# Concurrency — one run at a time per process
# ---------------------------------------------------------------------------


def test_a_second_concurrent_run_is_refused_not_starved(monkeypatch):
    _wire(monkeypatch)

    async def scenario():
        async with dr._RUN_LOCK:
            events, emit = _emitter()
            out = await dr.run_deep_research_engine(
                "q", [], emit, conversation_id="c1"
            )
            return out, events

    out, events = asyncio.run(scenario())
    assert "already in progress" in out
    meta = [p for k, p in events if k == "meta"][-1]
    assert meta["route"] == "deep_research" and meta["sources"] == []


# ---------------------------------------------------------------------------
# SSE contract — only legal event names, exactly one meta
# ---------------------------------------------------------------------------


def test_emits_only_legal_sse_events_and_one_meta(monkeypatch):
    from app.sse import ALL_EVENTS

    _wire(monkeypatch)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    kinds = [k for k, _ in events]
    # sse_event() RAISES on an unknown name, and that raise happens inside the
    # response generator — it would kill the stream with no error frame.
    assert set(kinds) <= set(ALL_EVENTS), set(kinds) - set(ALL_EVENTS)
    assert kinds.count("meta") == 1
    assert "step" in kinds and "status" in kinds


def test_the_pipeline_reports_its_stages_as_steps(monkeypatch):
    _wire(monkeypatch)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    steps = [p for k, p in events if k == "step"]
    titles = [s["title"] for s in steps]
    assert any("Plan" in t for t in titles)
    assert any("report" in t.lower() for t in titles)
    # Every started step is also finished, so the UI never leaves a spinner on.
    running = {s["id"] for s in steps if s["status"] == "running"}
    done = {s["id"] for s in steps if s["status"] == "done"}
    assert running == done


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_research_run_lifecycle_is_recorded():
    run_id = db.create_research_run("conv-dr-1", None, "what is X", "rid-1")
    assert run_id > 0
    db.finish_research_run(
        run_id,
        "done",
        2,
        5,
        9,
        4,
        "The report [1].",
        [{"n": 1, "title": "T", "url": "https://a.example/x", "domain": "a.example"}],
    )
    runs = db.get_research_runs("conv-dr-1")
    assert len(runs) == 1
    row = runs[0]
    assert row["status"] == "done"
    assert row["iterations"] == 2 and row["sources_found"] == 9
    assert row["sources_cited"] == 4
    assert row["sources"][0]["url"] == "https://a.example/x"


def test_deleting_a_conversation_removes_its_research_runs():
    # research_runs has no FK to conversations (a bare API call has no
    # conversations row), so it must be cleaned by hand via _SIDE_TABLES —
    # otherwise a deleted conversation leaves its question text behind.
    assert "research_runs" in db._SIDE_TABLES


# ---------------------------------------------------------------------------
# Dispatch — the pill must actually reach the engine
# ---------------------------------------------------------------------------


def _sse(body):
    out = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def test_deep_research_outranks_the_agent_planner(monkeypatch):
    """orchestrate.decide() classifies research phrasing as agent=true and at
    effort max FORCES it, so a lower branch would never run."""
    called = {}

    async def fake_engine(text, history, emit, **kw):
        called["text"] = text
        called["effort"] = kw.get("effort")
        await emit("token", {"text": "report"})
        await emit("meta", {"route": "deep_research", "sources": []})
        return "report"

    async def fake_agent(*a, **k):  # must NOT be reached
        called["agent"] = True
        return "agent answer"

    monkeypatch.setattr(settings, "search_enabled", True)
    monkeypatch.setattr(settings, "deep_research_enabled", True)
    from app.engines import deep_research as dre
    from app.engines import agent as agent_engine

    monkeypatch.setattr(dre, "run_deep_research_engine", fake_engine)
    monkeypatch.setattr(agent_engine, "run_agent_engine", fake_agent)

    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={
                "message": "Compare these frameworks and recommend one",
                "mode": "assistant",
                "deep_research": True,
                "web_search": "on",
                "effort": "max",
            },
        )
    assert resp.status_code == 200
    events = dict(_sse(resp.text))
    assert called.get("agent") is None, "the agent engine swallowed the request"
    assert called["text"].startswith("Compare these frameworks")
    assert events["meta"]["route"] == "deep_research"


def test_deep_research_is_explicit_only(monkeypatch):
    """Nothing may infer it: it costs minutes and the whole search budget."""
    called = {}

    async def fake_engine(*a, **k):
        called["hit"] = True
        return "report"

    from app.engines import deep_research as dre

    monkeypatch.setattr(dre, "run_deep_research_engine", fake_engine)
    monkeypatch.setattr(settings, "search_enabled", True)
    with TestClient(app) as client:
        client.post(
            "/chat",
            json={
                "message": "Research the state of the art and compare options",
                "mode": "assistant",
                "web_search": "on",
                "effort": "max",
            },
        )
    assert "hit" not in called


def test_without_a_search_provider_it_degrades_instead_of_pretending(monkeypatch):
    called = {}

    async def fake_engine(*a, **k):
        called["hit"] = True
        return "report"

    from app.engines import deep_research as dre

    monkeypatch.setattr(dre, "run_deep_research_engine", fake_engine)
    monkeypatch.setattr(settings, "search_enabled", False)
    with TestClient(app) as client:
        resp = client.post(
            "/chat",
            json={
                "message": "Research this thoroughly",
                "mode": "assistant",
                "deep_research": True,
                "effort": "fast",
            },
        )
    assert resp.status_code == 200
    assert "hit" not in called
    statuses = [d.get("text", "") for k, d in _sse(resp.text) if k == "status"]
    assert any("unavailable" in s.lower() for s in statuses)


def test_the_report_is_streamed_not_delivered_in_one_lump(monkeypatch):
    """A 6,349-character report arrived as ONE token event in the first live
    run, so the user watched a thinking indicator for ~40 s and then the whole
    document appeared at once. The engine buffers for citation validation, but
    it must forward each delta as it arrives."""
    _wire(monkeypatch, report="A fairly long report body [1] with several sentences in it.")
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    tokens = [p for k, p in events if k == "token"]
    assert len(tokens) > 1, "the report was not streamed"
    assert "".join(t["text"] for t in tokens).startswith("A fairly long report")
