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
import time
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from app import rerank
from app.config import settings
from app.core.org_brief import BUSINESS_TIMEZONE
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


@pytest.fixture
def utc_process(monkeypatch):
    """The production container's clock: no TZ set, /etc/localtime -> UTC
    (QA 2026-09-18). This host runs IST, which hid the defect."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_today_is_the_business_date_with_utc_beside_it(monkeypatch, utc_process):
    """Under the container's UTC clock the stamp read '... UTC (... UTC)' and
    its date was yesterday for the person between 00:00 and 05:30 IST — at
    a0a9b6f and with the process-local stamp alike. It is the business
    zone's (`org_brief.BUSINESS_TIMEZONE`) date and time now, with UTC
    beside it, whatever zone the process runs in."""
    _wire(monkeypatch, collect=lambda q: _results([f"https://s{i}.example/a" for i in range(4)]))
    events, emit = _emitter()
    business_date = datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date().isoformat()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    today = _run_meta(events)["today"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} IST \(\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\)", today), today
    assert today.startswith(business_date), f"{today!r} is not the business date {business_date}"


def test_the_night_window_is_the_person_s_today(monkeypatch, utc_process):
    """00:10 IST on 1 March is 18:40 UTC on 28 February: the window where the
    container's calendar is a day behind the person's. The clock is stopped
    there (a future date, so the real clock cannot stand in for it): the run
    is stamped with the person's date, a claim dated that day is kept, and
    the next day is still a future date. Measured at a0a9b6f under TZ=UTC at
    00:55 IST on 2026-09-19: `_parse_as_of('2026-09-19')` was None."""
    night = datetime(2027, 3, 1, 0, 10, tzinfo=ZoneInfo(BUSINESS_TIMEZONE))
    monkeypatch.setattr(dr, "_clock", lambda: night, raising=False)
    assert dr._parse_as_of("2027-03-01") == date(2027, 3, 1), "the person's today was refused as a future date"
    assert dr._parse_as_of("2027-03-02") is None
    _wire(monkeypatch, collect=lambda q: _results([f"https://s{i}.example/a" for i in range(4)]))
    events, emit = _emitter()
    asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert _run_meta(events)["today"] == "2027-03-01 00:10 IST (2027-02-28 18:40 UTC)"


def test_the_prompts_carry_the_stamp_the_run_was_given():
    st = _state()
    st.today = "2026-09-18 00:54 IST (2026-09-17 19:24 UTC)"
    assert "Current date: 2026-09-18 00:54 IST (2026-09-17 19:24 UTC)" in dr._temporal_note(st)
    _src(st, "https://a.example/x")
    assert "2026-09-17 19:24 UTC" in dr._report_messages(st, [])[0]["content"]


# ---------------------------------------------------------------------------
# B7b, repair (QA 2026-09-18). The floor scored the pool against the RAW
# message. A follow-up asked with Deep Research on ("Tell me more") has no
# subject of its own; the planner rebuilds it from history into the
# subquestions, but the reranker never saw them. Live reranker, 33 real
# SearXNG results for the planner's JWST query: "Tell me more" alone kept 0 of
# 33 (max 0.023, spread 0.0228, so not flagged degenerate); the same text plus
# the planner's subquestions kept 30 of 33. The fake below has that shape.
# ---------------------------------------------------------------------------

_JWST_WORDS = {"jwst", "galaxies", "early", "universe", "webb", "bright"}


async def _live_shaped_score(query, documents, **kw):
    q = set(re.findall(r"[a-z]+", query.lower()))
    out = []
    for i, d in enumerate(documents):
        hits = len(q & _JWST_WORDS & set(re.findall(r"[a-z]+", d.lower())))
        out.append(0.99 if hits >= 2 else 0.001 * (i % 24))
    if len(out) >= rerank.DEGENERATE_MIN_N and max(out) - min(out) < rerank.DEGENERATE_BAND:
        raise rerank.RerankUnavailable("degenerate")
    return out


_JWST_DOCS = [
    SearchResult(title=f"JWST early galaxies too bright {i}", url=f"https://s{i}.example/jwst",
                 snippet="Webb telescope observations of early universe galaxies")
    for i in range(30)
]


def _plan_json(subquestion, entities=("JWST",)):
    async def fake(messages, **kw):
        name = kw.get("schema_name")
        if name == "research_plan":
            return json.dumps({"subquestions": [subquestion], "queries": ["planner query"],
                               "entities": list(entities)})
        if name == "research_claims":
            return json.dumps({"claims": []})
        if name == "research_verify":
            return json.dumps({"verdicts": []})
        return json.dumps({"sufficient": True, "missing": [], "followup_queries": []})

    return fake


def test_qa_a_followup_research_question_still_reads_sources(monkeypatch):
    """QA's reproduction. 4810da0: 30 sources read. a0a9b6f: 0, and the
    person was told the search provider returned nothing usable."""
    history = [
        {"role": "user", "content": "What has JWST revealed about galaxies in the early universe?"},
        {"role": "assistant", "content": "JWST found unexpectedly bright early galaxies."},
    ]
    _wire(monkeypatch, collect=lambda q: list(_JWST_DOCS))
    monkeypatch.setattr(dr.llm, "json_completion", _plan_json(
        "What explanations exist for the bright early galaxies JWST found?"))
    monkeypatch.setattr(rerank, "score", _live_shaped_score)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 600.0)
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("Tell me more", history, emit, conversation_id="c1"))
    sources = ([p for k, p in events if k == "meta"] or [{}])[-1].get("sources") or []
    assert len(sources) >= 5, f"{len(sources)} source(s) read for a resolvable follow-up: {out[:160]!r}"


def test_the_reranker_scores_the_plan_not_only_the_message(monkeypatch):
    st = _state(question="Tell me more", subqs=("What explanations exist for the bright early galaxies?",))
    seen = []

    async def recording(query, documents, **kw):
        seen.append(query)
        return [0.9] * len(documents)

    monkeypatch.setattr(rerank, "score", recording)
    asyncio.run(dr._rank_candidates(st, _results(["https://a.example/x"])))
    assert seen == ["Tell me more\nWhat explanations exist for the bright early galaxies?"]


_PASTED_POSTING = "\n".join(
    [
        "Senior Data Engineer - Claims Platform",
        "Acme Mutual Insurance Private Limited",
        "Location Pune, hybrid, in office Monday and Wednesday at the Baner campus",
        "Experience 7 to 10 years",
        "Employment type full time, permanent",
        "about acme",
        "acme mutual settles motor and health claims for 2.3 million policy holders across "
        "Maharashtra and Karnataka, and every claim passes through the platform this team owns.",
        "about the role",
        "you will report to the head of claims data engineering and lead the rebuild of our "
        "fraud scoring pipeline on the streaming stack we adopted last year.",
        "requirements",
        "7+ years building batch and streaming data pipelines in Python and SQL",
        "hands on experience with Kafka, Flink or Spark Structured Streaming",
        "has run a dbt project with more than 400 models in production",
        "benefits",
        "family health insurance of 6 lakh rupees and a learning budget of 50,000 rupees a year",
    ]
)


@pytest.mark.parametrize("where", ["search", "links"])
def test_a_paste_does_not_crowd_the_plan_out_of_the_relevance_query(monkeypatch, where):
    """A Deep Research message that carries a paste: the floor judges pages
    against the person's own question and the plan, not the pasted text.

    The reranker reads the first 600 characters of its query
    (`rerank.MAX_QUERY_CHARS`), so the whole message went first and the
    subquestions never arrived: live on 2026-09-19, 40 real SearXNG results
    for a salary question under a pasted job posting, the floor kept 12
    scored against the message and 22 against the question plus the plan."""
    question = "What do senior data engineers earn in Pune in 2026, and how does that compare with Bengaluru?"
    subq = "What is the typical salary range for a senior data engineer in Bengaluru in 2026?"
    st = _state(question=f"{_PASTED_POSTING}\n\n{question}", subqs=(subq,))
    seen = []

    async def recording(query, documents, **kw):
        seen.append(rerank.format_query(query))
        return [0.9] * len(documents)

    monkeypatch.setattr(rerank, "score", recording)
    if where == "search":
        asyncio.run(dr._rank_candidates(st, _results(["https://a.example/x"])))
    else:
        page = _Source(n=0, url="https://b.example/pay", title="Pay", text="salaries in Pune")
        asyncio.run(dr._on_topic_pages(st, [page]))
    assert len(seen) == 1
    sent = seen[0]
    assert question in sent, f"the person's question did not reach the reranker: {sent!r}"
    assert subq in sent, f"the plan was cut off behind the paste: {sent!r}"
    assert "Baner campus" not in sent and "Claims Platform" not in sent, f"pasted text scored against: {sent!r}"


def test_a_run_whose_results_were_all_off_topic_does_not_blame_the_provider(monkeypatch):
    """The search DID return results; the floor turned every one down. At
    a0a9b6f the person was told "the search provider returned nothing
    usable" — false, and the wrong advice (rephrase, use Web Search)."""
    _wire(monkeypatch, collect=lambda q: list(_JWST_DOCS))
    monkeypatch.setattr(dr.llm, "json_completion", _plan_json("Who won the 1998 chess olympiad?", ()))
    monkeypatch.setattr(rerank, "score", _live_shaped_score)
    monkeypatch.setattr(settings, "deep_research_timeout_s", 600.0)
    closed = {}
    monkeypatch.setattr(dr.db, "finish_research_run",
                        lambda run_id, status, *a, **k: closed.update(status=status))
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("Who won the 1998 chess olympiad?", [], emit,
                                                  conversation_id="c1"))
    assert "search provider returned nothing usable" not in out, out
    assert "none of them was about this question" in out, out
    assert closed["status"] == "failed"


def test_a_run_the_provider_failed_still_says_so(monkeypatch):
    """What must NOT change: no results at all is still the provider's."""
    _wire(monkeypatch, collect=lambda q: [])
    monkeypatch.setattr(settings, "deep_research_timeout_s", 600.0)
    events, emit = _emitter()
    out = asyncio.run(dr.run_deep_research_engine("q", [], emit, conversation_id="c1"))
    assert "the search provider returned nothing usable" in out, out


# ---------------------------------------------------------------------------
# B23b, repair (QA 2026-09-18). The run-wide cap keyed on
# `search._registrable_domain`, which reads any <=3-letter label before a
# <=3-letter TLD as a public suffix: rocm.docs.amd.com -> docs.amd.com,
# community.amd.com -> community.amd.com, spam1.abc.io -> spam1.abc.io. A
# vendor with a short name got a fresh "site" per subdomain, and so would an
# SEO farm on a short domain.
# ---------------------------------------------------------------------------


def test_the_site_key_is_the_registrable_domain():
    cases = {
        "https://rocm.docs.amd.com/x": "amd.com",
        "https://community.amd.com/t": "amd.com",
        "https://www.amd.com/": "amd.com",
        "https://developer.ibm.com/a": "ibm.com",
        "https://research.ibm.com/b": "ibm.com",
        "https://docs.x.ai/": "x.ai",
        "https://spam1.abc.io/": "abc.io",
        "https://spam2.abc.io/": "abc.io",
        # Real two-label public suffixes are still one site each.
        "https://news.bbc.co.uk/1": "bbc.co.uk",
        "https://www.abc.net.au/news": "abc.net.au",
        "https://a.b.example.co.uk/": "example.co.uk",
        "https://shop.example.com.br/": "example.com.br",
        "https://acme.co/": "acme.co",
        "https://docs.python.org/3/": "python.org",
    }
    assert {u: dr._site_of(u) for u in cases} == cases


def _three_letter_vendor_run(effort):
    st = _state(question="what does AMD ship for ROCm", subqs=("what AMD ships",), entities=("AMD",))
    for i in range(search.domain_cap(effort)):
        _src(st, f"https://www.amd.com/page-{i}")
    pages = [
        _src(st, f"https://news{i}.example/story", links=[
            f"https://rocm.docs.amd.com/guide-{i}",
            f"https://community.amd.com/thread-{i}",
            f"https://other{i}.example/amd-report",
        ])
        for i in range(3)
    ]
    return st, pages


@pytest.mark.parametrize("effort", ["fast", "think", "max"])
def test_link_cap_holds_for_a_vendor_with_a_three_letter_name(effort):
    """QA2's reproduction: amd.com already read up to the cap, three news
    pages linking into its subdomains. At a0a9b6f 2 (Fast), 3 (Think) and 3
    (Max) more amd.com pages were followed."""
    st, pages = _three_letter_vendor_run(effort)
    picks = dr._candidate_links(st, pages, 10, effort)
    amd = [link for link, _s in picks if re.search(r"(^|\.)amd\.com$", link.split("/")[2])]
    assert not amd, f"cap {search.domain_cap(effort)} already spent on amd.com, still followed {amd}"
    assert any("other" in link for link, _s in picks)


def test_a_short_domain_cannot_lead_a_round_through_its_subdomains(monkeypatch):
    """The same key in the search-result order: subdomains of one short
    domain share one site's slots."""
    st = _state()
    farm = [f"https://spam{i}.abc.io/best-answer" for i in range(6)]
    others = [f"https://site{i}.example/x" for i in range(4)]
    monkeypatch.setattr(rerank, "score", _no_reranker)
    ranked = asyncio.run(dr._rank_candidates(st, _results(farm + others)))
    head = [r.url for r in ranked[: search.domain_cap("think") + len(others)]]
    assert sum(1 for u in head if u.endswith(".abc.io/best-answer")) == search.domain_cap("think"), head
    assert set(others) <= set(head)


# ---------------------------------------------------------------------------
# B23b, search rounds (QA 2026-09-18). `_rank_candidates` counted each site
# from zero every round, so every round gave one site `cap` more lead slots:
# live at Fast (cap 2), doc.rust-lang.org supplied 6 of 36 sources and
# postgresql.org 10, all through search.
# ---------------------------------------------------------------------------


def test_a_search_round_counts_the_pages_already_read_from_a_site(monkeypatch):
    st = _state(question="what does AMD ship for ROCm")
    for i in range(search.domain_cap("think")):  # the Think cap, already spent
        _src(st, f"https://rocm.docs.amd.com/page-{i}")
    amd = ["https://www.amd.com/en/products/rocm", "https://community.amd.com/t/rocm-7"]
    others = [f"https://site{i}.example/rocm" for i in range(3)]
    monkeypatch.setattr(rerank, "score", _no_reranker)
    ranked = [r.url for r in asyncio.run(dr._rank_candidates(st, _results(amd + others)))]
    assert ranked[: len(others)] == others, f"amd.com led again past its run-wide cap: {ranked}"
    assert ranked[len(others):] == amd, "a capped page was dropped, not deferred"


def test_a_site_under_its_run_wide_cap_still_leads(monkeypatch):
    """The count is the pages read, no more: one page already read leaves
    Think two lead slots (a0a9b6f gave it three, counting from zero)."""
    st = _state()
    _src(st, "https://vendor.com/read")
    vendor = [f"https://vendor.com/p{i}" for i in range(3)]
    others = [f"https://site{i}.example/x" for i in range(2)]
    monkeypatch.setattr(rerank, "score", _no_reranker)
    ranked = [r.url for r in asyncio.run(dr._rank_candidates(st, _results(vendor + others)))]
    assert ranked == vendor[:2] + others + vendor[2:], ranked


# ---------------------------------------------------------------------------
# B7b, followed links (QA2 2026-09-18). A link has no snippet, so the floor
# never saw it: a battery-price run followed links to a uranium price
# forecast, an 18650 pack calculator and a product page; none was cited, and
# each took a slot of the source cap or the verification reserve.
# ---------------------------------------------------------------------------


def _link_round(monkeypatch, scorer):
    st = _state(question="how fast are lithium-ion battery pack prices falling",
                subqs=("What is the average battery pack price per kWh?",))
    page = _src(st, "https://news.example/battery-prices", links=[
        "https://bnef.example/battery-pack-prices-2026",
        "https://investingnews.example/uranium-price-forecasts",
    ])
    texts = {
        "https://bnef.example/battery-pack-prices-2026":
            "Battery pack prices fell to $108/kWh in 2025, the survey of pack prices found. " * 8,
        "https://investingnews.example/uranium-price-forecasts":
            "Uranium spot prices and what analysts expect for the nuclear fuel market. " * 8,
    }

    async def fake_fetch(res, message=""):
        return [_Source(n=i, title=r.url, url=r.url, text=texts[r.url], fetched_at=_now(), authority=40)
                for i, r in enumerate(res, 1)]

    monkeypatch.setattr(dr, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(rerank, "score", scorer)
    monkeypatch.setattr(settings, "deep_research_links_per_round", 6)
    stats = dr.RoundStats(iteration=1, label="search", queries=[])
    added = asyncio.run(dr._follow_links(st, [page], "think", None, stats))
    return st, [s.url for s in added]


def test_a_followed_page_about_something_else_takes_no_source_slot(monkeypatch):
    seen = []

    async def by_topic(query, documents, **kw):
        seen.append(query)
        return [0.99 if "battery pack" in d.lower() else 0.0004 for d in documents]

    st, added = _link_round(monkeypatch, by_topic)
    assert added == ["https://bnef.example/battery-pack-prices-2026"], added
    assert "https://investingnews.example/uranium-price-forecasts" not in [s.url for s in st.sources]
    # Scored against the plan, like the search candidates.
    assert seen == ["how fast are lithium-ion battery pack prices falling\n"
                    "What is the average battery pack price per kWh?"]


def test_without_a_reranker_every_followed_page_is_kept(monkeypatch):
    st, added = _link_round(monkeypatch, _no_reranker)
    assert len(added) == 2, added
