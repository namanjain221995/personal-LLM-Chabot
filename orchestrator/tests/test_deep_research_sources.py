"""Deep Research reads on-topic sources from many sites, and keeps its rescue round.

Pinned here, each measured on 4810da0 before the fix:

* B7b — each search group asked `_collect_results` for the FETCH budget, so the
  reranker could only reorder what engine rank had already chosen; the pool
  it is meant to choose from (`candidate_budget`) was never requested. And a
  round whose every candidate the reranker scored as a different subject
  still read them all.
* B23b — link following capped two links per PAGE and nothing per SITE, so a
  vendor's pages linking to each other filled the run (the audit counted 11 of
  36 sources from one vendor).
* B23c — the verification round was gated on `budget_left()`, which is False
  after a source_cap stop, so the low-confidence runs never got the one extra
  round the verification pass exists to buy.
* B23a — report rule 7 said "Use the EVIDENCE STATUS table" and the model
  pasted the engine's table into the report.
* QA-web-dr-date — `state.today` was the UTC date. On this box (IST, UTC+05:30)
  that is yesterday between 00:00 and 05:30 local time.

Everything is offline: no vLLM, no SearXNG, no network, no database.
"""
import asyncio
import json
import re
from datetime import datetime, timezone

import pytest

from app import rerank
from app.config import settings
from app.engines import deep_research as dr
from app.engines import search
from app.engines.search import _Source
from app.freshness import Freshness, Verdict
from app.search.base import SearchResult


def _now():
    return datetime.now(timezone.utc)


def _emitter():
    events = []

    async def emit(kind, payload):
        events.append((kind, payload))

    return events, emit


def _state(question="q", subqs=("who leads it",), entities=()):
    st = dr.ResearchState(research_id="abc123def456", conversation_id="c1", question=question)
    st.subquestions = list(subqs)
    st.entities = list(entities)
    st.today = _now().date().isoformat()
    st.now_year = _now().year
    st.temporal = Verdict(Freshness.STATIC, 365 * 86400, "lexical:static")
    return st


def _src(st, url, text="a page about the subject. " * 20, *, links=(), authority=40):
    return dr._register(
        st,
        _Source(n=0, title=url, url=url, text=text, links=list(links),
                fetched_at=_now(), authority=authority, source_type="unknown"),
        "q",
    )


def _results(urls):
    return [SearchResult(title=f"Doc {i}", url=u, snippet=f"snippet {i}") for i, u in enumerate(urls, 1)]


async def _no_reranker(query, documents, **kw):
    raise rerank.RerankUnavailable("offline test")


def _wire(monkeypatch, *, collect, verify=None, claims=None):
    """The loop's outside world, stubbed. Returns the collect-call log."""
    verify = verify or {"verdicts": []}
    claims = claims or {"claims": []}

    async def fake_json_completion(messages, **kw):
        name = kw.get("schema_name")
        if name == "research_plan":
            return json.dumps({"subquestions": ["who leads it"], "queries": ["q1"], "entities": []})
        if name == "research_claims":
            return json.dumps(claims)
        if name == "research_verify":
            return json.dumps(verify)
        return json.dumps({"sufficient": True, "missing": [], "followup_queries": []})

    collected = []

    async def recording_collect(queries, effort="medium", emit=None, categories="", **kw):
        collected.append({"queries": list(queries), "effort": effort, **kw})
        return collect(queries)

    async def fake_fetch(res, message=""):
        return [
            _Source(n=i, title=r.title, url=r.url,
                    text=f"body {i}. The board confirmed that Person A leads it. " * 15,
                    authority=100 if "gov.example" in r.url else 40,
                    source_type="official" if "gov.example" in r.url else "news",
                    fetched_at=_now())
            for i, r in enumerate(res, 1)
        ]

    async def fake_stream(messages, **kw):
        for piece in ("Report ", "on it [1]."):
            yield ("token", piece)

    monkeypatch.setattr(dr.llm, "json_completion", fake_json_completion)
    monkeypatch.setattr(dr.llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(dr, "_collect_results", recording_collect)
    monkeypatch.setattr(rerank, "score", _no_reranker)
    monkeypatch.setattr(dr, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(dr, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(dr.db, "create_research_run", lambda *a, **k: 1)
    monkeypatch.setattr(dr.db, "finish_research_run", lambda *a, **k: None)
    monkeypatch.setattr(dr, "_persist_claims", lambda state: asyncio.sleep(0))
    monkeypatch.setattr(settings, "deep_research_min_sources", 1)
    monkeypatch.setattr(settings, "deep_research_background_crawl", False)
    return collected


def _run_meta(events):
    return [p for k, p in events if k == "meta"][-1]["research_run"]


# ---------------------------------------------------------------------------
# B7b — the reranker chooses from a pool, and off-topic candidates are not read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", ["think", "fast", "max"])
def test_each_search_group_asks_for_the_reranker_s_candidate_pool(monkeypatch, effort):
    collected = _wire(monkeypatch, collect=lambda q: _results([f"https://s{i}.example/a" for i in range(4)]))
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, effort=effort, conversation_id="c1"))
    assert collected, "no search ran"
    assert all(c.get("candidates") == search.candidate_budget(effort) for c in collected), collected


def test_sub_floor_candidates_are_dropped(monkeypatch):
    st = _state(question="what is the capital of France")
    res = _results([f"https://site{i}.example/p" for i in range(6)])
    scores = {"now": [0.99, 0.001, 0.98, 0.002, 0.97, 0.0]}

    async def fake_score(query, documents, **kw):
        return scores["now"][: len(documents)]

    monkeypatch.setattr(rerank, "score", fake_score)
    kept = asyncio.run(dr._rank_candidates(st, res))
    assert {r.url for r in kept} == {res[0].url, res[2].url, res[4].url}

    # A round where the reranker judged EVERY candidate a different subject
    # reads none of them. At 4810da0 all six were kept ("never a gate") and
    # the round spent its fetches and its extraction call on them.
    scores["now"] = [0.01, 0.001, 0.02, 0.002, 0.03, 0.0]
    assert asyncio.run(dr._rank_candidates(st, res)) == []


def test_among_on_topic_pages_the_first_hand_one_leads(monkeypatch):
    """The reranker is near-binary (0.95-1.0 for every on-topic page, measured
    live), so between two pages that both answer the question a 0.01 relevance
    gap is noise and authority decides. Holds before and after the floor
    change; pinned because the order now comes from the scores themselves."""
    st = _state(question="what does the regulator require")
    res = _results(["https://someblog.example/post", "https://www.regulator.gov/rules"])

    async def fake_score(query, documents, **kw):
        return [0.999, 0.990]

    monkeypatch.setattr(rerank, "score", fake_score)
    ranked = asyncio.run(dr._rank_candidates(st, res))
    assert ranked[0].url == "https://www.regulator.gov/rules"


def test_without_a_reranker_nothing_is_dropped(monkeypatch):
    """No scores, no floor: an unavailable reranker is an upgrade missing,
    never a gate — the candidates keep engine order."""
    st = _state()
    res = _results([f"https://site{i}.example/p" for i in range(6)])
    monkeypatch.setattr(rerank, "score", _no_reranker)
    kept = asyncio.run(dr._rank_candidates(st, res))
    assert [r.url for r in kept] == [r.url for r in res]


def test_one_site_cannot_fill_a_round_while_alternatives_exist(monkeypatch):
    """The 5x pool widens `_collect_results`' own per-domain cap with it (3 ->
    15 per site at Think), so the read-set cap is re-applied here, in
    `domain_cap(effort)` — over-cap pages go to the back of the queue: read
    only when the round has nothing else."""
    st = _state()
    vendor = [f"https://vendor.com/p{i}" for i in range(3)] + [
        f"https://www.vendor.com/q{i}" for i in range(2)] + ["https://shop.vendor.com/r"]
    others = [f"https://site{i}.example/x" for i in range(4)]
    monkeypatch.setattr(rerank, "score", _no_reranker)
    ranked = asyncio.run(dr._rank_candidates(st, _results(vendor + others)))
    head = [r.url for r in ranked[: 3 + len(others)]]
    assert sum(1 for u in head if search._registrable_domain(u) == "vendor.com") == search.domain_cap("think")
    assert set(others) <= set(head)
    assert len(ranked) == len(vendor) + len(others), "a capped page was dropped, not deferred"


def test_the_write_behind_gets_the_read_set_not_the_pool(monkeypatch):
    """`_persist_and_index` logs the search and crawls the domains of what it
    is handed. With a 5x pool that must be what was READ, as on the search
    route — not every candidate the reranker turned down."""
    pool = [f"https://s{i}.example/a" for i in range(30)]
    _wire(monkeypatch, collect=lambda q: _results(pool))
    handed = []

    def recorder(question, queries, results, *rest):
        handed.append([r.url for r in results])
        return asyncio.sleep(0)

    monkeypatch.setattr(dr, "_persist_and_index", recorder)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert handed
    assert len(handed[0]) <= settings.deep_research_sources_per_iteration, len(handed[0])
    assert len(handed[0]) < len(pool)


# ---------------------------------------------------------------------------
# B23b — one site cannot take over the run through its links
# ---------------------------------------------------------------------------


def _vendor_run():
    st = _state(question="what does Vendor ship", subqs=("what vendor ships",), entities=("Vendor",))
    _src(st, "https://docs.vendor.com/a")
    _src(st, "https://www.vendor.com/b")
    pages = [
        _src(st, f"https://news{i}.example/story", links=[
            f"https://vendor.com/product-{i}",
            f"https://blog.vendor.com/post-{i}",
            f"https://other{i}.example/vendor-report",
        ])
        for i in range(3)
    ]
    return st, pages


@pytest.mark.parametrize("effort", ["think", "max", "fast"])
def test_link_following_never_pushes_a_site_past_its_cap(effort):
    st, pages = _vendor_run()
    picks = dr._candidate_links(st, pages, 10, effort)
    already = sum(1 for s in st.sources if search._registrable_domain(s.url) == "vendor.com")
    followed = [link for link, _src in picks if search._registrable_domain(link) == "vendor.com"]
    assert already + len(followed) <= max(already, search.domain_cap(effort)), followed
    # The other sites the pages cite are still followed.
    assert any("other" in link for link, _src in picks)


def test_the_site_cap_counts_every_page_already_read_from_it():
    st, pages = _vendor_run()
    _src(st, "https://vendor.com/c")  # three read: the Think cap is spent
    picks = dr._candidate_links(st, pages, 10)  # the default effort is Think
    assert not [link for link, _src in picks if search._registrable_domain(link) == "vendor.com"]


# ---------------------------------------------------------------------------
# B23c — the verification round survives a source_cap stop
# ---------------------------------------------------------------------------


def test_the_verification_round_runs_after_a_source_cap_stop(monkeypatch):
    def collect(queries):
        if any(q.startswith("verify") for q in queries):
            return _results(["https://org.gov.example/official"])
        return _results([f"https://s{i}.example/a" for i in range(4)])

    claims = {"claims": [{"subquestion": 1, "claim": "Person A leads it", "value": "Person A",
                          "source": 1, "as_of": "2024-01-01", "status": "current"}]}
    verify = {"verdicts": [{"subquestion": 1, "enough_evidence": False, "primary_source_opened": False,
                            "confidence": 0.3, "verification_queries": ["verify: official leadership page"]}]}
    _wire(monkeypatch, collect=collect, verify=verify, claims=claims)
    monkeypatch.setattr(settings, "deep_research_max_sources", 4)
    monkeypatch.setattr(settings, "deep_research_verify", True)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 600.0)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("who leads it", [], emit, conversation_id="c1"))
    run = _run_meta(events)
    assert run["stop_reason"] == "source_cap"
    assert run["verification_rounds"] == 1, "a low-confidence run lost its verification round"
    # It read past the cap, but only by the verification reserve.
    assert 4 < run["sources_found"] <= 4 + dr._VERIFY_SOURCE_RESERVE
    assert any("gov.example" in s["url"] for s in [p for k, p in events if k == "meta"][-1]["sources"])


def test_a_confident_run_at_the_source_cap_does_not_spend_the_reserve(monkeypatch):
    verify = {"verdicts": [{"subquestion": 1, "enough_evidence": True, "primary_source_opened": True,
                            "confidence": 0.95}]}
    claims = {"claims": [{"subquestion": 1, "claim": "Person A leads it", "value": "Person A",
                          "source": n, "as_of": "2026-08-01", "status": "current"} for n in (1, 2, 3)]}
    _wire(monkeypatch, collect=lambda q: _results([f"https://s{i}.example/a" for i in range(4)]),
          verify=verify, claims=claims)
    monkeypatch.setattr(settings, "deep_research_max_sources", 4)
    monkeypatch.setattr(settings, "deep_research_verify", True)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 600.0)
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("who leads it", [], emit, conversation_id="c1"))
    run = _run_meta(events)
    assert run["verification_rounds"] == 0
    assert run["sources_found"] == 4


# ---------------------------------------------------------------------------
# B23a — the evidence table is the writer's input, never the reader's text
# ---------------------------------------------------------------------------


def test_the_report_prompt_forbids_reproducing_the_evidence_table():
    st = _state()
    _src(st, "https://a.example/x", authority=70)
    system = dr._report_messages(st, [])[0]["content"]
    clause = next((line for line in system.splitlines() if "EVIDENCE STATUS" in line and "never" in line), "")
    assert clause, "no rule keeps the EVIDENCE STATUS table out of the report"
    assert re.search(r"never (quote|reproduce)", clause), clause
    # ...while the table itself still reaches the writer.
    assert "EVIDENCE STATUS" in dr._report_messages(st, [])[-1]["content"]


# ---------------------------------------------------------------------------
# QA-web-dr-date — today is the person's today, with UTC beside it
# ---------------------------------------------------------------------------


def test_today_is_the_local_date_with_utc_beside_it(monkeypatch):
    _wire(monkeypatch, collect=lambda q: _results([f"https://s{i}.example/a" for i in range(4)]))
    events, emit = _emitter()
    local_date = datetime.now().astimezone().date().isoformat()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    today = _run_meta(events)["today"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} .+ \(\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\)", today), today
    assert today.startswith(local_date), f"{today!r} is not the local date {local_date}"


def test_the_prompts_carry_the_stamp_the_run_was_given():
    st = _state()
    st.today = "2026-09-18 00:54 IST (2026-09-17 19:24 UTC)"
    assert "Current date: 2026-09-18 00:54 IST (2026-09-17 19:24 UTC)" in dr._temporal_note(st)
    _src(st, "https://a.example/x")
    assert "2026-09-17 19:24 UTC" in dr._report_messages(st, [])[0]["content"]
