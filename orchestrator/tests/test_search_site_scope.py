"""A `site:` search stays on its site, and a realtime question never reads an
hour-old stored page.

SITE SCOPE (backlog B7a). One read-only query to the production SearXNG on
2026-09-18, 'site:qdrant.tech documentation performance benchmark 10 million
vectors', came back with SIX of its top twelve results off the site: bing
ignored the operator and answered the word "documentation" (en.wikipedia.org,
scribe.com, geeksforgeeks.org, lisedunetwork.com, merriam-webster.com,
herothemes.com) while yandex honoured it at ranks 2, 4, 6, 8, 10 and 12.
search.py had no `site:` handling, so `_collect_results` merged those six into
the pool by engine rank. Deep research's rescue for an unresolved subquestion
IS a `site:` query (`deep_research._synthetic_followups`), and the Fast-mode
freshness lookup runs through the same merge.

REALTIME TTL (backlog QA-web-6). `_page_ttl` has honoured a freshness verdict
since ADR-0001, but no caller of `_fetch_sources` passed one, so every search
fell back to re-matching `_FRESH_RE`: a REALTIME question ("stock price right
now") and a merely volatile one shared WEB_PAGE_FRESH_TTL_S = 3600, and a
stored page read 59 minutes ago answered "right now".

No network, no database, no model: the provider, the page store and the
reader are fakes.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import db, web_index
from app.config import settings
from app.engines import search
from app.freshness import Freshness, Verdict, classify_offline
from app.search.base import SearchResult


@pytest.fixture(autouse=True)
def generous_provider_cap(monkeypatch):
    """The per-provider cap comes from the environment; these tests are about
    what the merge keeps, not about how many the provider was asked for."""
    monkeypatch.setattr(settings, "search_max_results", 200)
    search._cache.clear()


def result(url, title="t", snippet="s"):
    return SearchResult(title=title, url=url, snippet=snippet)


def provider(monkeypatch, by_query):
    """`by_query(q) -> [SearchResult]`, standing in for SearXNG."""

    class P:
        name = "fake"
        unresponsive: dict = {}

        async def search(self, q, max_results, categories=""):
            return list(by_query(q))[:max_results]

    monkeypatch.setattr(search, "get_provider", lambda: P())


# ---------------------------------------------------------------------------
# 1 — a site: query keeps only that site
# ---------------------------------------------------------------------------

QDRANT_Q = "site:qdrant.tech documentation performance benchmark 10 million vectors"

#: The production result list for QDRANT_Q, in rank order, as measured on
#: 2026-09-18 — plus a subdomain and two look-alike hosts the operator must
#: not be fooled by.
OFF_SITE = [
    "https://en.wikipedia.org/wiki/Documentation",
    "https://scribe.com/library/what-is-documentation",
    "https://www.geeksforgeeks.org/software-engineering/overview-software-documentation/",
    "https://www.lisedunetwork.com/documentation/",
    "https://www.merriam-webster.com/dictionary/documentation",
    "https://herothemes.com/blog/how-to-write-documentation/",
    "https://qdrant.tech.example.com/benchmarks/",
    "https://notqdrant.tech/benchmarks/",
]
ON_SITE = [
    "https://qdrant.tech/blog/case-study-sprinklr/",
    "https://qdrant.tech/",
    "https://qdrant.tech/benchmarks/",
    "https://qdrant.tech/articles/memory-consumption/",
    "https://qdrant.tech/benchmarks/single-node-speed-benchmark/",
    "https://qdrant.tech/course/essentials/day-2/collection-tuning-demo/",
    "https://api.qdrant.tech/api-reference",
]


def _qdrant_pool():
    """Interleaved the way SearXNG merged bing and yandex: off-site first."""
    pool = []
    for i in range(max(len(OFF_SITE), len(ON_SITE))):
        if i < len(OFF_SITE):
            pool.append(result(OFF_SITE[i]))
        if i < len(ON_SITE):
            pool.append(result(ON_SITE[i]))
    return pool


def test_a_site_query_keeps_only_that_site_and_its_subdomains(monkeypatch):
    provider(monkeypatch, lambda q: _qdrant_pool())
    got = asyncio.run(search._collect_results([QDRANT_Q], "think"))
    urls = [r.url for r in got]
    off = [u for u in urls if u not in ON_SITE]
    assert not off, f"{len(off)} off-site results survived a site: query: {off}"
    assert set(urls) == set(ON_SITE), "an on-site result was lost"
    assert "https://api.qdrant.tech/api-reference" in urls, "a subdomain IS the site"


def test_two_site_operators_keep_both_sites(monkeypatch):
    milvus = [result("https://milvus.io/docs/benchmark.md"),
              result("https://blog.milvus.io/vector-db-bench")]
    provider(monkeypatch, lambda q: _qdrant_pool() + milvus)
    got = asyncio.run(search._collect_results(
        ["vector database benchmark site:qdrant.tech OR site:milvus.io"], "think"
    ))
    urls = {r.url for r in got}
    assert urls <= set(ON_SITE) | {r.url for r in milvus}, sorted(urls)
    # Both sites survive. (Not every page: the per-domain diversity cap still
    # applies inside the scope, as it always has.)
    assert {r.url for r in milvus} <= urls, sorted(urls)
    assert urls & set(ON_SITE), sorted(urls)


def test_a_query_without_an_operator_is_unchanged(monkeypatch):
    """The ordinary route: every result the engines return is still eligible,
    in the order it was before."""
    pool = [result(f"https://site{i}.test/page") for i in range(10)]
    provider(monkeypatch, lambda q: pool)
    got = asyncio.run(search._collect_results(["vector database benchmark"], "think"))
    assert [r.url for r in got] == [r.url for r in pool]
    # And the mixed qdrant pool with the operator taken out keeps its
    # off-site results: only the operator scopes a search.
    search._cache.clear()
    provider(monkeypatch, lambda q: _qdrant_pool())
    got = asyncio.run(search._collect_results(
        ["qdrant documentation performance benchmark 10 million vectors"], "think"
    ))
    assert "https://en.wikipedia.org/wiki/Documentation" in [r.url for r in got]


def test_a_site_query_with_no_match_returns_nothing(monkeypatch):
    """An empty set is the honest answer: every caller already handles []
    (run_search_engine falls back with a notice, deep research moves on)."""
    provider(monkeypatch, lambda q: [result(u) for u in OFF_SITE])
    assert asyncio.run(search._collect_results([QDRANT_Q], "think")) == []


def test_the_operator_scopes_its_own_query_not_its_neighbours(monkeypatch):
    """Deep research and the rewriter send several queries in one merge; a
    `site:` on one of them must not empty the others."""
    general = [result("https://weaviate.io/blog/ann-benchmark"),
               result("https://ann-benchmarks.com/")]

    def by_query(q):
        return _qdrant_pool() if "site:" in q else general

    provider(monkeypatch, by_query)
    got = asyncio.run(search._collect_results(
        [QDRANT_Q, "approximate nearest neighbour benchmark"], "think"
    ))
    urls = {r.url for r in got}
    assert {r.url for r in general} <= urls, "the unscoped query lost its results"
    assert not urls & set(OFF_SITE), "the scoped query leaked"


def test_the_research_panel_shows_what_the_scope_kept(monkeypatch):
    provider(monkeypatch, lambda q: _qdrant_pool())
    shown = []

    async def emit(kind, data):
        if kind == "research" and data.get("phase") == "query":
            shown.extend(r["url"] for r in data["results"])

    asyncio.run(search._collect_results([QDRANT_Q], "think", emit))
    assert shown and not set(shown) & set(OFF_SITE), shown


@pytest.mark.parametrize(
    "query",
    [
        "Site:QDRANT.tech benchmark",           # operators are case-insensitive
        "site:www.qdrant.tech benchmark",       # www. is the same site
        # "site:qdrant.tech/benchmarks/ speed" used to sit here, pinning that
        # a path prefix admits the whole host. QA round 1 (2026-09-18) called
        # that over-admission a defect; the path is honoured now, see
        # test_a_path_prefix_keeps_only_that_path below.
        "site:https://qdrant.tech benchmark",   # pasted with its scheme
        "site:*.qdrant.tech benchmark",         # the wildcard form
        "benchmark (site:qdrant.tech)",
    ],
)
def test_the_operator_spellings_people_type(monkeypatch, query):
    provider(monkeypatch, lambda q: _qdrant_pool())
    urls = {r.url for r in asyncio.run(search._collect_results([query], "think"))}
    assert urls == set(ON_SITE), (query, sorted(urls))


@pytest.mark.parametrize(
    "query",
    [
        "is this website:qdrant.tech legit",   # not an operator, a word
        "python tutorial -site:qdrant.tech",   # an EXCLUSION is not an inclusion
        "what does site: mean in a search",    # nothing after the colon
    ],
)
def test_text_that_is_not_an_inclusion_operator_scopes_nothing(monkeypatch, query):
    provider(monkeypatch, lambda q: _qdrant_pool())
    got = asyncio.run(search._collect_results([query], "think"))
    assert "https://en.wikipedia.org/wiki/Documentation" in [r.url for r in got]


# ---------------------------------------------------------------------------
# 2 — the freshness verdict reaches the page store; REALTIME has its own TTL
# ---------------------------------------------------------------------------

PAGE = "https://quotes.example.com/nvda"
REALTIME_Q = "what is the NVIDIA stock price right now?"
VOLATILE_Q = "latest vllm release notes"
STABLE_Q = "who is the ceo of acme robotics"


@pytest.fixture
def ttls(monkeypatch):
    monkeypatch.setattr(settings, "web_page_ttl_s", 24 * 3600)
    monkeypatch.setattr(settings, "web_page_fresh_ttl_s", 3600)


def _stored_page_aged(monkeypatch, age):
    """One page in the store, read `age` ago."""
    row = {
        "url_key": search._normalize_url(PAGE), "url": PAGE, "canonical_url": "",
        "title": "NVDA", "text": "stored body", "content_type": "text/html",
        "fetch_status": 200, "content_hash": "h",
        "fetched_at": datetime.now(timezone.utc) - age,
    }
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(db, "get_web_pages", lambda keys: [row])


def _watch_the_store(monkeypatch):
    """Record the verdict each `_stored_pages` call got, and whether the page
    was served from the store instead of the network."""
    seen = {"verdicts": [], "from_store": []}
    real = search._stored_pages

    async def spy(results, message, verdict=None):
        seen["verdicts"].append(verdict)
        return await real(results, message, verdict=verdict)

    async def reader(idx, r, stored=None, **kw):
        hit = bool(stored) and search._normalize_url(r.url) in stored
        seen["from_store"].append(hit)
        return search._Source(n=idx, title=r.title, url=r.url, text="body",
                              from_store=hit)

    monkeypatch.setattr(search, "_stored_pages", spy)
    monkeypatch.setattr(search, "_fetch_source", reader)
    return seen


def _run_search_engine(monkeypatch, question):
    async def rewrite(message, hist, effort="medium"):
        return [message]

    async def memory(message, srcs, budget=3):
        return srcs

    async def stream(messages, **kwargs):
        yield "token", "answer [1]"

    async def emit(kind, data):
        return None

    monkeypatch.setattr(search, "rewrite_queries", rewrite)
    monkeypatch.setattr(search, "_memory_sources", memory)
    monkeypatch.setattr(search.llm, "stream_chat_events", stream)
    asyncio.run(search.run_search_engine(question, [], emit, "think"))


def _research_step(monkeypatch, question):
    async def rewrite(message, hist, effort="medium"):
        return [message]

    async def memory(message, srcs, budget=3):
        return srcs

    async def complete(messages, **kwargs):
        return "answer [1]"

    monkeypatch.setattr(search, "rewrite_queries", rewrite)
    monkeypatch.setattr(search, "_memory_sources", memory)
    monkeypatch.setattr(search.llm, "chat_completion", complete)
    asyncio.run(search.research_step(question, [], "think"))


def _fetch_for_freshness(monkeypatch, question):
    async def index(**kw):
        return None

    monkeypatch.setattr(settings, "search_enabled", True)
    monkeypatch.setattr(web_index, "index_pending", index)
    asyncio.run(search.fetch_for_freshness(question))


CALL_SITES = {
    "run_search_engine": _run_search_engine,
    "research_step": _research_step,
    "fetch_for_freshness": _fetch_for_freshness,
}


def _through(monkeypatch, call_site, question, age):
    provider(monkeypatch, lambda q: [result(PAGE, title="NVDA quote")])
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())
    _stored_page_aged(monkeypatch, age)
    seen = _watch_the_store(monkeypatch)
    CALL_SITES[call_site](monkeypatch, question)
    assert len(seen["verdicts"]) == 1, seen
    return seen["verdicts"][0], seen["from_store"]


@pytest.mark.parametrize("call_site", sorted(CALL_SITES))
def test_a_realtime_verdict_reaches_the_store_and_a_ten_minute_old_page_is_stale(
    monkeypatch, ttls, call_site
):
    """The reproduction: at 4810da0 no caller passed a verdict, `_FRESH_RE`
    matched "right now", and the 3600 s TTL served a ten-minute-old copy of
    a stock quote as the answer to "right now"."""
    verdict, from_store = _through(
        monkeypatch, call_site, REALTIME_Q, timedelta(minutes=10)
    )
    assert verdict is not None, f"{call_site} read the store without a verdict"
    assert verdict.requirement is Freshness.REALTIME
    assert search._page_ttl(REALTIME_Q, verdict) == search._REALTIME_PAGE_TTL_S
    assert from_store == [False], "a ten-minute-old quote answered 'right now'"


@pytest.mark.parametrize("call_site", sorted(CALL_SITES))
def test_a_realtime_question_still_reuses_a_page_read_a_minute_ago(
    monkeypatch, ttls, call_site
):
    """Short, not zero: a regenerate right after the answer reads nothing new."""
    _verdict, from_store = _through(
        monkeypatch, call_site, REALTIME_Q, timedelta(minutes=1)
    )
    assert from_store == [True]


@pytest.mark.parametrize("call_site", sorted(CALL_SITES))
def test_a_volatile_question_keeps_the_hour(monkeypatch, ttls, call_site):
    verdict, from_store = _through(
        monkeypatch, call_site, VOLATILE_Q, timedelta(minutes=10)
    )
    assert verdict.volatile and verdict.requirement is not Freshness.REALTIME
    assert from_store == [True], "the volatile TTL is still 3600 s"


@pytest.mark.parametrize("call_site", sorted(CALL_SITES))
def test_a_months_stable_question_is_served_its_three_hour_old_page(
    monkeypatch, ttls, call_site
):
    """The dividend `_page_ttl` was written for and never paid: "who is"
    trips `_FRESH_RE`, so without the verdict an office-holder question threw
    away a three-hour-old copy of the page that answers it."""
    verdict, from_store = _through(
        monkeypatch, call_site, STABLE_Q, timedelta(hours=3)
    )
    assert verdict.requirement is Freshness.RECENT and not verdict.volatile
    assert from_store == [True]


def test_the_realtime_ttl_is_short_and_never_longer_than_the_volatile_one(
    monkeypatch, ttls
):
    realtime = classify_offline(REALTIME_Q, now_year=2026)
    assert realtime.requirement is Freshness.REALTIME
    assert search._page_ttl(REALTIME_Q, realtime) == search._REALTIME_PAGE_TTL_S
    assert search._REALTIME_PAGE_TTL_S < settings.web_page_fresh_ttl_s
    # An operator who shortened the volatile TTL below it is not overruled.
    monkeypatch.setattr(settings, "web_page_fresh_ttl_s", 60)
    assert search._page_ttl(REALTIME_Q, realtime) == 60


def test_the_volatile_ttl_and_the_office_dividend(ttls):
    """Renamed from test_volatile_and_stable_ttls_are_unchanged: QA round 1
    pointed out that STABLE_Q got 3600 s at 4810da0 (no caller passed a
    verdict and "who is" trips `_FRESH_RE`), so 24 h here is a deliberate
    change, not an unchanged value. It is the one lengthening the verdict
    may make; test_a_verdict_never_lengthens_a_ttl_the_wording_shortened
    pins everything else."""
    volatile = classify_offline(VOLATILE_Q, now_year=2026)
    assert volatile.volatile and volatile.requirement is Freshness.RECENT
    assert search._page_ttl(VOLATILE_Q, volatile) == 3600
    stable = classify_offline(STABLE_Q, now_year=2026)
    assert search._page_ttl(STABLE_Q) == 3600, "4810da0's TTL for it"
    assert search._page_ttl(STABLE_Q, stable) == 24 * 3600
    assert search._page_ttl("x", Verdict(Freshness.STATIC, 1, "t")) == 24 * 3600
    # No verdict (deep research's reader, the crawler): the regex fallback,
    # byte for byte what it was.
    assert search._page_ttl(REALTIME_Q) == 3600
    assert search._page_ttl("explain photosynthesis") == 24 * 3600


# ---------------------------------------------------------------------------
# 3 — QA round 1 repairs (2026-09-18)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        # QA reproductions: 4810da0 held every one of these to 3600 s through
        # `_FRESH_RE`; at a039169 the RECENT verdict gave them 24 h.
        "who won the match today",
        "news about the air india crash",
        "any update on the israel ceasefire",
        "top headlines this week",
        # STATIC because "what is" matches the timeless shapes, yet a score.
        "what is the score",
        # An office-holder question that ALSO carries a word about time: the
        # office dividend is for the phrasing, not for the day word.
        "who is the ceo of intel today",
        "any update on the prime minister's resignation",
        "news about the ceo of acme robotics",
    ],
)
def test_a_verdict_never_lengthens_a_ttl_the_wording_shortened(ttls, question):
    verdict = classify_offline(question, now_year=2026)
    assert search._page_ttl(question) == 3600, "the fixture must be one 4810da0 shortened"
    assert search._page_ttl(question, verdict) <= 3600, (question, verdict)


def test_a_path_prefix_keeps_only_that_path(monkeypatch):
    """QA round 1: 'site:qdrant.tech/benchmarks' admitted the whole host. The
    prefix is matched on a segment boundary and without case."""
    extra = [result("https://qdrant.tech/benchmarks-old/"),
             result("https://qdrant.tech/Benchmarks/filtered-search/")]
    provider(monkeypatch, lambda q: _qdrant_pool() + extra)
    want = {
        "https://qdrant.tech/benchmarks/",
        "https://qdrant.tech/benchmarks/single-node-speed-benchmark/",
        "https://qdrant.tech/Benchmarks/filtered-search/",
    }
    for q in ("site:qdrant.tech/benchmarks/ speed",
              "site:qdrant.tech/benchmarks speed",
              "speed site:https://qdrant.tech/benchmarks?"):
        search._cache.clear()
        urls = {r.url for r in asyncio.run(search._collect_results([q], "think"))}
        assert urls == want, (q, sorted(urls))


def test_a_malformed_result_url_does_not_abort_an_unscoped_search(monkeypatch):
    """Pre-existing at 4810da0 (QA round 1, finding 11): 'https://[::1/broken'
    raised ValueError('Invalid IPv6 URL') out of `_registrable_domain`, so one
    bad URL from any engine aborted the whole search."""
    provider(monkeypatch, lambda q: [result("https://[::1/broken"),
                                     result("https://ok.test/a")])
    shown = []

    async def emit(kind, data):
        if kind == "research" and data.get("phase") == "query":
            shown.extend(r["url"] for r in data["results"])

    got = asyncio.run(search._collect_results(["plain query"], "think", emit))
    assert "https://ok.test/a" in [r.url for r in got]
    assert "https://ok.test/a" in shown


def test_a_long_rescue_query_keeps_its_scope_and_a_long_message_gets_none(monkeypatch):
    """Deep research's rescue query OPENS with its operator and may run past
    the one-line size; a long message with an operator in its middle is a
    message, and a pasted operator in it must not scope the search."""
    subq = ("what did the vendor's own published benchmark report for "
            "filtered search latency at ten million vectors ") * 3
    assert len(subq) > search._SITE_SCOPE_MAX_CHARS
    provider(monkeypatch, lambda q: _qdrant_pool())
    rescue = f"site:qdrant.tech {subq}"
    urls = {r.url for r in asyncio.run(search._collect_results([rescue], "think"))}
    assert urls == set(ON_SITE)
    search._cache.clear()
    message = f"{subq} and cross-check it on site:qdrant.tech first"
    urls = [r.url for r in asyncio.run(search._collect_results([message], "think"))]
    assert "https://en.wikipedia.org/wiki/Documentation" in urls


def test_the_fast_lookup_keeps_a_typed_scope_with_its_question_mark(monkeypatch):
    """QA round 1, finding 2, through the caller: fetch_for_freshness sends the
    person's words as the query, so 'site:qdrant.tech?' reached the filter
    with its '?' and the lookup read nothing (8 results at 4810da0, 0 at
    a039169, live)."""
    provider(monkeypatch, lambda q: _qdrant_pool())
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(settings, "web_memory_enabled", False)
    read = []

    async def reader(idx, r, stored=None, **kw):
        read.append(r.url)
        return search._Source(n=idx, title=r.title, url=r.url, text="body")

    async def index(**kw):
        return None

    monkeypatch.setattr(search, "_fetch_source", reader)
    monkeypatch.setattr(settings, "search_enabled", True)
    monkeypatch.setattr(web_index, "index_pending", index)
    n = asyncio.run(search.fetch_for_freshness("latest qdrant release site:qdrant.tech?"))
    assert n >= 1 and read and all(u in ON_SITE for u in read), read


def _router_returns(monkeypatch, reply):
    async def router(msgs, **kw):
        return reply

    monkeypatch.setattr(search.llm, "router_chat_completion", router)


def test_the_rewrite_keeps_the_persons_typed_site_operator(monkeypatch):
    """QA round 1, finding 9, 3 of 3 live runs: the router rewrote
    'site:qdrant.tech how fast is search on 10 million vectors?' into three
    unscoped queries, so the ordinary route never kept a typed scope."""
    _router_returns(monkeypatch, json.dumps([
        "qdrant search latency 10 million vectors",
        "qdrant benchmark site:qdrant.tech",
        "vector search speed",
    ]))
    qs = asyncio.run(search.rewrite_queries(
        "site:qdrant.tech how fast is search on 10 million vectors?", [], "think"))
    assert qs == [
        "qdrant search latency 10 million vectors site:qdrant.tech",
        "qdrant benchmark site:qdrant.tech",
        "vector search speed site:qdrant.tech",
    ], qs
    assert all(search._site_scope(q) == ("qdrant.tech",) for q in qs)


def test_the_rewrite_adds_no_operator_the_person_did_not_type(monkeypatch):
    reply = json.dumps(["air india crash investigation news", "air india crash report"])
    _router_returns(monkeypatch, reply)
    # A pasted block's operator is not the person's.
    pasted = ("Summarise this forwarded note and tell me today's news on it.\n"
              "---\nCross-check it on site:reddit.com first.\n---")
    assert asyncio.run(search.rewrite_queries(pasted, [], "think")) == json.loads(reply)
    # No operator at all: the router's queries, untouched.
    assert asyncio.run(search.rewrite_queries(
        "air india crash investigation", [], "think")) == json.loads(reply)


def _evidence(url, *, age, text="the NVIDIA share price quote"):
    from app.web_memory import Evidence

    return Evidence(
        url=url, title="Quote", text=text, domain=url.split("/")[2], authority=40,
        fetched_at=datetime.now(timezone.utc) - age, lexical=1.0,
    )


def _memory_with(monkeypatch, evidence):
    from app import web_memory

    monkeypatch.setattr(settings, "web_memory_enabled", True)

    async def retrieve(question, *, level, top_k, **kw):
        r = web_memory.Retrieval(query=question, freshness=level)
        r.evidence = list(evidence)
        return r

    monkeypatch.setattr(web_memory, "retrieve", retrieve)


def test_a_realtime_answer_gets_no_stored_passage_older_than_its_ttl(monkeypatch, ttls):
    """QA round 1, finding 4: a passage read 2 h earlier was appended to the
    sources for 'NVIDIA stock price right now', labelled only with its read
    DATE, so the model could not tell it was two hours old."""
    _memory_with(monkeypatch, [
        _evidence("https://old.example/nvda", age=timedelta(hours=2)),
        _evidence("https://new.example/nvda", age=timedelta(minutes=1)),
    ])
    out = asyncio.run(search._memory_sources(REALTIME_Q, []))
    assert [s.url for s in out] == ["https://new.example/nvda"]


def test_a_non_realtime_answer_still_gets_its_dated_passages(monkeypatch, ttls):
    """What must not change: the age cut is for REALTIME only."""
    _memory_with(monkeypatch, [
        _evidence("https://old.example/vllm", age=timedelta(hours=2),
                  text="the vllm release notes"),
    ])
    out = asyncio.run(search._memory_sources(VOLATILE_Q, []))
    assert [s.url for s in out] == ["https://old.example/vllm"]


def test_one_search_classifies_its_question_once(monkeypatch, ttls):
    """QA round 1, finding 5: the page TTL and `_memory_sources` each ran
    classify_offline on the same text, and its timeless-shape pattern is
    quadratic on a pathological line (13.3 s at 400 KB)."""
    from app import freshness

    calls = []
    real = freshness.classify_offline

    def counting(question, *, now_year):
        calls.append(question)
        return real(question, now_year=now_year)

    monkeypatch.setattr(freshness, "classify_offline", counting)
    monkeypatch.setattr(search, "classify_offline", counting)
    _memory_with(monkeypatch, [])
    provider(monkeypatch, lambda q: [result(PAGE, title="NVDA quote")])
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(db, "get_web_pages", lambda keys: [])

    async def reader(idx, r, stored=None, **kw):
        return search._Source(n=idx, title=r.title, url=r.url, text="body")

    async def rewrite(message, hist, effort="medium"):
        return [message]

    async def stream(messages, **kwargs):
        yield "token", "answer [1]"

    async def emit(kind, data):
        return None

    monkeypatch.setattr(search, "_fetch_source", reader)
    monkeypatch.setattr(search, "rewrite_queries", rewrite)
    monkeypatch.setattr(search.llm, "stream_chat_events", stream)
    q = "what is the NVIDIA stock price right now, counted once?"
    asyncio.run(search.run_search_engine(q, [], emit, "think"))
    assert calls == [q], calls


@pytest.mark.parametrize("brk", ["\\n", "\\r\\n", "\\u2028", "\\u0085"])
def test_an_operator_after_any_line_break_is_pasted_text_not_a_scope(monkeypatch, brk):
    """QA round 1, finding 8: a paste arrives inline in the message the Fast
    lookup sends as its query. Any line break str recognises marks it."""
    brk = brk.encode("ascii").decode("unicode_escape")
    provider(monkeypatch, lambda q: _qdrant_pool())
    message = f"summarise this note{brk}cross-check it on site:qdrant.tech first"
    assert search._site_scope(message) == ()
    urls = [r.url for r in asyncio.run(search._collect_results([message], "think"))]
    assert "https://en.wikipedia.org/wiki/Documentation" in urls
