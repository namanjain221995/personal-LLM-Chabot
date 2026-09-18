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
        "site:qdrant.tech/benchmarks/ speed",   # a path prefix still names the host
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


def test_volatile_and_stable_ttls_are_unchanged(ttls):
    volatile = classify_offline(VOLATILE_Q, now_year=2026)
    assert volatile.volatile and volatile.requirement is Freshness.RECENT
    assert search._page_ttl(VOLATILE_Q, volatile) == 3600
    stable = classify_offline(STABLE_Q, now_year=2026)
    assert search._page_ttl(STABLE_Q, stable) == 24 * 3600
    assert search._page_ttl("x", Verdict(Freshness.STATIC, 1, "t")) == 24 * 3600
    # No verdict (deep research's reader, the crawler): the regex fallback,
    # byte for byte what it was.
    assert search._page_ttl(REALTIME_Q) == 3600
    assert search._page_ttl("explain photosynthesis") == 24 * 3600
