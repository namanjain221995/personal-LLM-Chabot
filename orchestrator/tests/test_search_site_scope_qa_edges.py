"""QA round 1 for web-search-scope: the seams the builder's file does not test.

Each test states whether it must pass on 4810da0 too ("must NOT change") or
is new behaviour. No network, no database, no model.
"""
import asyncio
import random
import time
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.config import settings
from app.engines import search
from app.search.base import SearchResult


@pytest.fixture(autouse=True)
def _cap(monkeypatch):
    monkeypatch.setattr(settings, "search_max_results", 20000)
    search._cache.clear()


def _r(url):
    return SearchResult(title="t", url=url, snippet="s")


def _provider(monkeypatch, by_query):
    class P:
        name = "fake"
        unresponsive: dict = {}

        async def search(self, q, max_results, categories=""):
            return list(by_query(q))[:max_results]

    monkeypatch.setattr(search, "get_provider", lambda: P())


ON = ["https://qdrant.tech/benchmarks/", "https://qdrant.tech/articles/x/",
      "https://api.qdrant.tech/ref"]
OFF = ["https://en.wikipedia.org/wiki/Documentation", "https://www.merriam-webster.com/d"]


def _pool():
    return [_r(u) for pair in zip(OFF + [None], ON) for u in pair if u]


def _collect(q, effort="think"):
    return [r.url for r in asyncio.run(search._collect_results([q], effort))]


# --------------------------------------------------------------------------
# 1. Operator spellings a person really types (NEW behaviour must hold)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query",
    [
        # Measured live 2026-09-18, 3/3 runs: SearXNG returned 6 qdrant.tech
        # pages for this exact string; the head keeps 0 of 16 and the turn
        # falls back to "No web results found".
        "does qdrant support 10 million vectors site:qdrant.tech?",
        "benchmarks site:qdrant.tech!",
        "benchmarks site:qdrant.tech…",        # pasted ellipsis
        "benchmarks site:qdrant.tech‏",        # RTL mark from an RTL UI
    ],
)
def test_trailing_punctuation_does_not_empty_a_site_query(monkeypatch, query):
    _provider(monkeypatch, lambda q: _pool())
    got = _collect(query)
    assert set(got) == set(ON), (query, got)


@pytest.mark.parametrize(
    "scope, url",
    [
        ("bücher.de", "https://xn--bcher-kva.de/katalog"),
        ("xn--bcher-kva.de", "https://bücher.de/katalog"),
        ("правительство.рф",
         "https://xn--80aealotwbjpid2k.xn--p1ai/news"),
    ],
)
def test_an_idn_site_matches_either_spelling_of_its_host(monkeypatch, scope, url):
    _provider(monkeypatch, lambda q: [_r(url), _r(OFF[0])])
    got = _collect(f"site:{scope} news")
    assert got == [url], got


# --------------------------------------------------------------------------
# 2. Hosts that must NOT pass as the site (security: must hold at head)
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "https://qdrant.tech@evil.example/benchmarks",       # userinfo trick
        "https://evil.example/?u=https://qdrant.tech/",     # URL in query string
        "https://evil.example/qdrant.tech/benchmarks",       # host in the path
        "https://qdrаnt.tech/benchmarks",               # Cyrillic a homograph
        "https://qdrant.tech.evil.example/",                 # suffix look-alike
        "http://[::1",                                        # malformed
        "",                                                   # empty
        "javascript:alert(1)//qdrant.tech",
    ],
)
def test_look_alike_and_malformed_hosts_never_pass_as_the_site(monkeypatch, url):
    _provider(monkeypatch, lambda q: [_r(url), _r(ON[0])])
    got = _collect("site:qdrant.tech benchmark")
    assert got == [ON[0]], got


# --------------------------------------------------------------------------
# 3. Scale, duplicates, cache, concurrency (must hold at head)
# --------------------------------------------------------------------------

def test_ten_thousand_results_are_scoped_fast(monkeypatch):
    pool = []
    for i in range(10_000):
        pool.append(_r(f"https://site{i}.test/p") if i % 2 else _r(f"https://qdrant.tech/p{i}"))
    _provider(monkeypatch, lambda q: pool)
    t = time.perf_counter()
    got = asyncio.run(search._collect_results(
        ["site:qdrant.tech x"], "max", candidates=10_000))
    took = time.perf_counter() - t
    assert got and all(search._on_site(r.url, ("qdrant.tech",)) for r in got)
    assert took < 2.0, took


def test_duplicate_scoped_queries_and_a_cache_hit_still_filter(monkeypatch):
    calls = []

    def by_query(q):
        calls.append(q)
        return _pool()

    _provider(monkeypatch, by_query)
    q = "site:qdrant.tech benchmark"
    first = _collect(q)
    second = _collect(q)                       # served from search._cache
    assert len(calls) == 1, "second call should be a cache hit"
    assert set(first) == set(second) == set(ON)
    both = [r.url for r in asyncio.run(search._collect_results([q, q], "think"))]
    assert set(both) == set(ON)
    # The cache still holds the UNFILTERED list: a later unscoped query with
    # the same text minus the operator is a different key and is unaffected.
    assert any(len(v[1]) == len(_pool()) for v in search._cache.values())


def test_concurrent_turns_with_different_scopes_do_not_bleed(monkeypatch):
    milvus = ["https://milvus.io/docs/bench", "https://blog.milvus.io/b"]
    _provider(monkeypatch, lambda q: _pool() + [_r(u) for u in milvus])

    async def both():
        return await asyncio.gather(
            search._collect_results(["site:qdrant.tech bench"], "think"),
            search._collect_results(["site:milvus.io bench"], "think"),
            search._collect_results(["vector bench"], "think"),
        )

    a, b, c = asyncio.run(both())
    assert {r.url for r in a} == set(ON)
    assert {r.url for r in b} == set(milvus)
    assert {r.url for r in c} >= set(OFF), "the unscoped turn lost results"


# --------------------------------------------------------------------------
# 4. What must NOT change: queries with no inclusion operator merge exactly as
#    4810da0 did (differential against the filter switched off).
# --------------------------------------------------------------------------

_NOT_OPS = ["website:qdrant.tech", "-site:qdrant.tech", "campsite: tips",
            "parasite:host", "site: ", "\"site:qdrant.tech\"", "موقع site",
            "mysite:qdrant.tech", "https://x.test/site:qdrant.tech"]


def test_no_inclusion_operator_is_byte_identical_to_the_old_merge(monkeypatch):
    rng = random.Random(20260918)
    hosts = ["qdrant.tech", "en.wikipedia.org", "a.test", "b.test", "c.test",
             "api.qdrant.tech", "notqdrant.tech", "www.d.test"]
    for case in range(300):
        pools = {}
        qs = []
        for j in range(rng.randint(1, 4)):
            q = f"q{case}-{j} " + rng.choice(_NOT_OPS) + " " + rng.choice(["", "שלום", "x"])
            qs.append(q)
            pools[q] = [_r(f"https://{rng.choice(hosts)}/p{rng.randint(0, 30)}")
                        for _ in range(rng.randint(0, 25))]
        _provider(monkeypatch, lambda q: pools[q])
        effort = rng.choice(["fast", "medium", "think", "max"])
        cand = rng.choice([None, 40])
        search._cache.clear()
        new = asyncio.run(search._collect_results(qs, effort, candidates=cand))
        search._cache.clear()
        real = search._site_scope
        monkeypatch.setattr(search, "_site_scope", lambda q: ())
        old = asyncio.run(search._collect_results(qs, effort, candidates=cand))
        monkeypatch.setattr(search, "_site_scope", real)
        assert [r.url for r in new] == [r.url for r in old], qs


# --------------------------------------------------------------------------
# 5. TTL: what must NOT change. A question 4810da0 already treated as fresh
#    (its wording names today / news / an update) must not start being served
#    a 3-hour-old stored page because a verdict now reaches the store.
# --------------------------------------------------------------------------

PAGE = "https://scores.example.com/today"


@pytest.mark.parametrize(
    "question",
    [
        "Who won the match today?",
        "what's happening in Delhi today",
        "Any news on the Air India crash investigation?",
        "update on the Microsoft layoffs",
        "election results 2026",
        # Both from the repo's own freshness/web-truth fixtures.
        "how much is gold per gram",
        "What is GPT-5.2's reasoning score on the BenchLM leaderboard?",
    ],
)
def test_a_today_or_news_question_does_not_reuse_a_three_hour_old_page(
    monkeypatch, question
):
    monkeypatch.setattr(settings, "web_page_ttl_s", 24 * 3600)
    monkeypatch.setattr(settings, "web_page_fresh_ttl_s", 3600)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    row = {
        "url_key": search._normalize_url(PAGE), "url": PAGE, "canonical_url": "",
        "title": "T", "text": "stored body", "content_type": "text/html",
        "fetch_status": 200, "content_hash": "h",
        "fetched_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }
    monkeypatch.setattr(db, "get_web_pages", lambda keys: [row])
    _provider(monkeypatch, lambda q: [_r(PAGE)])
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())
    served = []

    async def reader(idx, r, stored=None, **kw):
        served.append(bool(stored) and search._normalize_url(r.url) in stored)
        return search._Source(n=idx, title=r.title, url=r.url, text="body")

    async def rewrite(message, hist, effort="medium"):
        return [message]

    async def memory(message, srcs, budget=3):
        return srcs

    async def stream(messages, **kwargs):
        yield "token", "answer [1]"

    async def emit(kind, data):
        return None

    monkeypatch.setattr(search, "_fetch_source", reader)
    monkeypatch.setattr(search, "rewrite_queries", rewrite)
    monkeypatch.setattr(search, "_memory_sources", memory)
    monkeypatch.setattr(search.llm, "stream_chat_events", stream)
    asyncio.run(search.run_search_engine(question, [], emit, "think"))
    assert served == [False], f"{question!r} was answered from a 3-hour-old stored page"


# --------------------------------------------------------------------------
# 6. The verdict helper never costs the search.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [None, "", "\x00" * 10, "‮" * 100])
def test_question_verdict_never_raises(bad):
    search._question_verdict(bad)
