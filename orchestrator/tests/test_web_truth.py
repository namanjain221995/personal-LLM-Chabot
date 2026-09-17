"""Web answers may only assert what a fetched source supports.

The platform audit (2026-09-17, area `web-search`) graded the ordinary search
route against what ChatGPT returns and found four ways a web answer could
state something no source said:

1. NOTHING ON THE ROUTE KNEW THE DATE. Asked "what is the latest iPhone
   model?" the answer read apple.com's own page — which said "Pre-order
   iPhone 18 Pro now" — and wrote that a device "became available in October
   2026", a month in the FUTURE, in the past tense. Deep Research has carried
   `state.today` since 2026-09-03; the search route never got it.
2. THE RERANKER COULD NOT DROP ANYTHING. `_collect_results` truncated to the
   fetch budget and `run_search_engine` then called
   `_rerank_results(asked, results, len(results))`, so `keep` was the whole
   input. Asked for Surat's weather, the answer cited two weather.com pages
   for Kolar, Karnataka (1,500 km away), "Electric current - Wikipedia" and a
   JEE physics worksheet, while weather.com/Surat sat unfetched in the pool.
3. THE REWRITER PINNED ITS OWN TRAINING CUTOFF. "what's new in vLLM v0.4.0",
   "Apple iPhone launch date 2024", "Intel CEO 2024" — three of eight cases,
   and the same shape in production `web_searches` rows.
4. `authority` WAS COMPUTED AND READ BY NOBODY, so three of four citations in
   the vLLM answer were SEO aggregators and docs.vllm.ai sat uncited at [12].

And the snippet rule was prompt-only: 6 of 15 sources in the weather turn
were pages that had 403'd, and the answer cited two of them for a temperature.

Every test here is derived from that report. No network, no database, no
model call: the provider, the reranker and the LLM are fakes.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.engines import search
from app.search.base import SearchResult


@pytest.fixture(autouse=True)
def generous_provider_cap(monkeypatch):
    """The per-provider cap is read from the environment and this repo's .env
    sets it to 10 — below the candidate pool these tests are about."""
    monkeypatch.setattr(settings, "search_max_results", 200)
    search._cache.clear()


def result(url, title="t", snippet="s"):
    return SearchResult(title=title, url=url, snippet=snippet)


def provider_returning(monkeypatch, results):
    class P:
        name = "fake"
        unresponsive: dict = {}

        async def search(self, q, max_results, categories=""):
            return list(results)[:max_results]

    monkeypatch.setattr(search, "get_provider", lambda: P())


def scorer(monkeypatch, fn):
    """`fn(query, document) -> float`, in place of the cross-encoder."""
    from app import rerank

    async def score(query, documents, **kw):
        return [fn(query, d) for d in documents]

    monkeypatch.setattr(rerank, "score", score)


# ---------------------------------------------------------------------------
# 1 — the answer prompt knows what day it is
# ---------------------------------------------------------------------------


def _iphone_sources():
    """The audit's case, as the prompt saw it. apple.com says PRE-ORDER."""
    return [
        search._Source(
            n=1,
            title="iPhone 18 Pro",
            url="https://www.apple.com/iphone/",
            text="Pre-order iPhone 18 Pro now.",
            published_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
            fetched_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
            source_type="official",
            authority=70,
        ),
        search._Source(
            n=2,
            title="Apple's iPhone 18 Pro will launch with the foldable",
            url="https://www.macrumors.com/2026/08/28/iphone-18-pro/",
            text="Apple's iPhone 18 Pro and Pro Max will launch with the "
            "foldable iPhone in September 2026.",
            published_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            fetched_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
            source_type="news",
            authority=40,
        ),
    ]


def test_the_answer_prompt_states_todays_date():
    """The one fact the whole route was missing."""
    system = search._answer_messages("q", _iphone_sources(), [])[0]["content"]
    assert search._today_iso() in system, system[:400]


def test_today_is_the_date_HERE_and_the_utc_reading_comes_with_it():
    """Found on the first live run of this change, at 00:54 IST: a UTC stamp
    put "today's weather for Surat" on 2026-09-17 while the person asking was
    already on the 18th. This box is UTC+05:30, so a UTC-only date is the
    wrong day for five and a half hours out of every twenty-four."""
    local = datetime.now().astimezone()
    assert search._today_iso() == local.date().isoformat()
    system = search._answer_messages("q", _iphone_sources(), [])[0]["content"]
    assert local.date().isoformat() in system
    assert "UTC" in system, "the UTC reading must travel with the local one"


def test_the_prompt_carries_the_time_of_day_not_only_the_date():
    """A spot price is minutes old. The audit's gold answer "gave a date but
    no time of day", which reads as true for the whole day."""
    system = search._answer_messages("q", _iphone_sources(), [])[0]["content"]
    assert f"{datetime.now().astimezone():%H:%M}" in system, system[:200]


def test_the_prompt_forbids_writing_a_future_date_in_the_past_tense():
    """"The iPhone Duo became available in October 2026" was written on
    2026-09-17. No source said "became available", and October had not
    happened."""
    system = search._answer_messages("q", _iphone_sources(), [])[0]["content"].lower()
    assert "later than today" in system
    assert "past tense" in system
    for word in ("released", "shipped", "launched", "available"):
        assert word in system, f"{word!r} is one of the words that was invented"


def test_the_prompt_requires_a_source_for_every_factual_claim():
    system = search._answer_messages("q", _iphone_sources(), [])[0]["content"].lower()
    assert "every factual claim" in system
    assert "do not make it" in system
    assert "uncertainty" in system


def test_a_sources_own_dates_reach_the_prompt():
    """The `_Source` has carried published/modified/fetched since 2026-09-03
    and `_context_block` rendered title and URL alone, so a page from 2026 and
    a page from 2019 were indistinguishable."""
    block = search._context_block(_iphone_sources())
    assert "published 2026-09-09" in block
    assert "published 2026-08-28" in block
    assert "read 2026-09-17" in block
    assert "official" in block


def test_an_undated_page_says_so_rather_than_saying_nothing():
    """Silence reads as "recent". The SEO rewrites that outranked first-party
    sources are exactly the pages that carry no date."""
    s = search._Source(n=1, title="t", url="https://whatsnew.fyi/x", text="body")
    assert "undated" in search._context_block([s])


# ---------------------------------------------------------------------------
# 2 — the reranker gets a candidate pool, and may drop what is in it
# ---------------------------------------------------------------------------

#: The audit's production pool for "what is today's weather forecast for
#: Surat, Gujarat?": engine rank put Kolar and the electric-current pages in
#: the fetch budget and left weather.com/Surat outside it.
JUNK = [
    ("https://weather.com/en-IN/weather/today/l/Kolar+Karnataka", "Kolar, Karnataka Weather"),
    ("https://www.accuweather.com/en/in/kolar/current-weather", "Kolar Current Weather"),
    ("https://en.wikipedia.org/wiki/Electric_current", "Electric current - Wikipedia"),
    ("https://www.electricalvolt.com/electric-current/", "Electric Current - Definition, Symbol, Formula"),
    ("https://www.allen.in/current-electricity", "Current Electricity: Solved Examples"),
    ("https://www.tripadvisor.in/Attractions-Surat", "THE 30 BEST Places to Visit in Surat"),
]
GOOD = [
    ("https://weather.com/en-IN/weather/today/l/Surat+Gujarat", "Surat, Gujarat Weather Forecast"),
    ("https://www.imd.gov.in/surat", "Surat weather forecast today"),
    ("https://www.ndtv.com/weather/surat", "Surat weather today forecast"),
]


def _surat_pool():
    """Junk at the good engine ranks, the real pages far down — as measured."""
    pool = [result(u, title=t, snippet=t) for u, t in JUNK]
    pool += [result(f"https://filler{i}.test/x", title=f"filler {i}", snippet="f")
             for i in range(20)]
    pool += [result(u, title=t, snippet=t) for u, t in GOOD]
    return pool


def _surat_scorer(query, document):
    """What the real reranker gave this exact pool (audit pool_probe_weather)."""
    d = document.lower()
    if "surat" in d and ("weather" in d or "forecast" in d):
        return 0.9995
    if "surat" in d:
        return 0.0045          # the TripAdvisor page
    return 0.0002


ASKED = "what is today's weather forecast for Surat, Gujarat?"


def test_the_reranker_is_handed_a_pool_and_not_the_fetch_budget(monkeypatch):
    """`keep = len(results)` meant nothing could ever be dropped."""
    provider_returning(monkeypatch, _surat_pool())
    scorer(monkeypatch, _surat_scorer)
    seen = {}

    real_rerank = search._rerank_results

    async def watched(message, results, target, per_domain=0):
        seen["candidates"] = len(results)
        seen["target"] = target
        return await real_rerank(message, results, target, per_domain)

    monkeypatch.setattr(search, "_rerank_results", watched)
    pool = asyncio.run(
        search._collect_results(
            [ASKED], "think", None, candidates=search.candidate_budget("think")
        )
    )
    kept = asyncio.run(
        watched(ASKED, pool, search.source_budget("think"), search.domain_cap("think"))
    )
    assert seen["candidates"] > search.source_budget("think"), (
        "the reranker was handed the fetch budget, so it could only reorder"
    )
    assert seen["target"] == search.source_budget("think")
    assert len(kept) < seen["candidates"], "a pool the reranker cannot cut is not a pool"


def test_a_whole_search_reads_the_pages_that_answer_the_question(monkeypatch):
    """End to end, through the real `_collect_results` and `_rerank_results`.

    This is the audit case with nothing stubbed but the provider, the
    cross-encoder and the model: the pool the engines return is what was
    measured, the scores are what the real reranker gave that pool, and the
    assertion is on the URLs the fetch stage is asked to open.
    """
    provider_returning(monkeypatch, _surat_pool())
    scorer(monkeypatch, _surat_scorer)
    asked_for: list = []

    async def fake_fetch(res, message="", **kw):
        asked_for.extend(r.url for r in res)
        return [
            search._Source(n=i + 1, title=r.title, url=r.url, text="page text",
                           fetched_at=datetime(2026, 9, 17, tzinfo=timezone.utc))
            for i, r in enumerate(res)
        ]

    async def fake_rewrite(message, hist, effort="medium"):
        return [message]

    async def fake_memory(message, srcs, budget=3):
        return srcs

    async def fake_stream(messages, **kwargs):
        yield "token", "It is 30 C in Surat [1]."

    async def emit(kind, data):
        return None

    monkeypatch.setattr(search, "rewrite_queries", fake_rewrite)
    monkeypatch.setattr(search, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(search, "_memory_sources", fake_memory)
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(search.llm, "stream_chat_events", fake_stream)

    asyncio.run(search.run_search_engine(ASKED, [], emit, "think"))

    for junk_url, title in JUNK:
        assert junk_url not in asked_for, f"{title!r} was read for a weather question"
    assert "https://weather.com/en-IN/weather/today/l/Surat+Gujarat" in asked_for, (
        "the page that answers the question was in the pool and never fetched"
    )


def test_the_reranker_drops_junk_even_when_asked_to_keep_everything(monkeypatch):
    """`len(results)` was the production `target` at both call sites, which is
    why the comment above it ("best 15 of the pool replaces first 15 by engine
    rank") described something the code did not do. The floor holds whatever
    `keep` says."""
    scorer(monkeypatch, _surat_scorer)
    pool = _surat_pool()
    kept = asyncio.run(search._rerank_results(ASKED, pool, len(pool)))
    urls = [r.url for r in kept]
    assert "https://en.wikipedia.org/wiki/Electric_current" not in urls
    assert "https://www.allen.in/current-electricity" not in urls
    assert "https://weather.com/en-IN/weather/today/l/Surat+Gujarat" in urls


def test_the_junk_that_was_cited_for_a_weather_question_is_dropped(monkeypatch):
    """The reproduction. Every source the audit named must be gone, and the
    weather.com page production never fetched must be in."""
    scorer(monkeypatch, _surat_scorer)
    kept = asyncio.run(
        search._rerank_results(
            ASKED, _surat_pool(), search.source_budget("think"),
            per_domain=search.domain_cap("think"),
        )
    )
    urls = [r.url for r in kept]
    for junk_url, _title in JUNK:
        assert junk_url not in urls, f"{junk_url} is keyword noise and was read"
    assert "https://weather.com/en-IN/weather/today/l/Surat+Gujarat" in urls
    assert all(r.url.startswith("https://") for r in kept)


def test_the_floor_is_relevance_alone_and_authority_cannot_lift_a_junk_page(monkeypatch):
    """en.wikipedia.org scores 70 authority and `reference` class — the
    highest prior in the pool — and scored 0.0002 for this question."""
    scorer(monkeypatch, _surat_scorer)
    kept = asyncio.run(
        search._rerank_results(ASKED, _surat_pool(), 15, per_domain=3)
    )
    assert "https://en.wikipedia.org/wiki/Electric_current" not in [r.url for r in kept]


def test_a_batch_in_which_nothing_scores_is_still_answered(monkeypatch):
    """Reranking is an upgrade, never a gate: a weak batch must not become an
    empty one, or a niche question turns into "couldn't read the sources"."""
    scorer(monkeypatch, lambda q, d: 0.001)
    pool = [result(f"https://x{i}.test/a", title=f"t{i}") for i in range(10)]
    kept = asyncio.run(search._rerank_results("q", pool, 5))
    assert len(kept) == 5


def test_the_read_set_keeps_its_domain_cap_after_the_pool_widens(monkeypatch):
    """`_collect_results` relaxes the per-domain cap to BUILD the pool, so the
    cap has to be re-applied where the read set is chosen — otherwise one site
    supplies the whole answer."""
    pool = [result(f"https://one.test/{i}", title=f"weather forecast {i}",
                   snippet="surat weather forecast") for i in range(12)]
    pool += [result(f"https://other{i}.test/a", title=f"weather forecast {i}",
                    snippet="surat weather forecast") for i in range(12)]
    scorer(monkeypatch, lambda q, d: 0.9)
    kept = asyncio.run(search._rerank_results(ASKED, pool, 15, per_domain=3))
    from_one = [r for r in kept if r.url.startswith("https://one.test/")]
    assert len(from_one) <= 3, [r.url for r in kept]


# ---------------------------------------------------------------------------
# 3 — authority decides between two pages that both answer the question
# ---------------------------------------------------------------------------


def test_the_projects_own_pages_beat_the_sites_that_rewrite_them(monkeypatch):
    """The vLLM answer cited patchletter.com, whatsnew.fyi and traceary.com,
    and left docs.vllm.ai at [12]. All four are neutral-authority hosts, so
    only the page CLASS can separate them."""
    pool = [
        result("https://patchletter.com/vllm", title="vLLM latest release notes"),
        result("https://whatsnew.fyi/vllm", title="vLLM latest release notes"),
        result("https://traceary.com/vllm", title="vLLM latest release notes"),
        result("https://docs.vllm.ai/en/latest/", title="vLLM latest release notes"),
        result("https://github.com/vllm-project/vllm/releases",
               title="vLLM latest release notes"),
    ]
    # Identical topical relevance: the cross-encoder cannot tell them apart,
    # which is exactly the state the audit found.
    scorer(monkeypatch, lambda q, d: 0.9)
    kept = asyncio.run(search._rerank_results("latest vLLM release", pool, 2))
    urls = {r.url for r in kept}
    assert "https://docs.vllm.ai/en/latest/" in urls, urls
    assert "https://github.com/vllm-project/vllm/releases" in urls, urls


def test_a_blog_host_is_finally_scored_as_one():
    """`\\.blog$` was anchored to the end of the whole URL and therefore never
    matched anything; the audit's comparison answer was written out of
    jamesm.blog and aibytes.blog while nvidia.com sat uncited."""
    from app import web_memory

    assert web_memory.authority_of("https://jamesm.blog/dgx-spark-vs-m3-ultra") == 15
    assert web_memory.authority_of("https://aibytes.blog/post") == 15


def test_a_city_called_seoul_is_not_an_seo_farm():
    """Bare `seo` matched three letters anywhere in a URL. It never mattered
    while nothing read the score; it matters now that ranking does."""
    from app import web_memory

    assert web_memory.authority_of("https://www.timeanddate.com/weather/south-korea/seoul") == 40
    assert web_memory.authority_of("https://example.com/seo-services") == 15


# ---------------------------------------------------------------------------
# 4 — the rewriter may not pin a year the person never gave
# ---------------------------------------------------------------------------


def _rewrites(monkeypatch, queries):
    import json

    async def router(msgs, **kw):
        _rewrites.system = msgs[0]["content"]
        return json.dumps(queries)

    monkeypatch.setattr(search.llm, "router_chat_completion", router)


def test_the_training_cutoff_year_is_taken_out_of_a_live_search(monkeypatch):
    """The audit's three cases, verbatim."""
    _rewrites(monkeypatch, [
        "latest iPhone model released by Apple",
        "current iPhone model available for purchase",
        "Apple iPhone launch date 2024",
    ])
    got = asyncio.run(search.rewrite_queries("what is the latest iPhone model?", []))
    assert "Apple iPhone launch date" in got
    assert not any("2024" in q for q in got), got


def test_a_remembered_version_number_is_taken_out_too(monkeypatch):
    _rewrites(monkeypatch, [
        "vLLM latest version release notes",
        "vLLM changelog most recent update",
        "what's new in vLLM v0.4.0",
    ])
    got = asyncio.run(search.rewrite_queries(
        "what is the latest version of vLLM and what changed in it?", []
    ))
    assert "what's new in vLLM" in got
    assert not any("0.4.0" in q for q in got), got


def test_a_year_the_person_asked_for_is_kept(monkeypatch):
    """This only removes what the CONVERSATION never contained."""
    _rewrites(monkeypatch, ["Indian general election 2024 result"])
    got = asyncio.run(search.rewrite_queries(
        "who won the 2024 Indian general election?", []
    ))
    assert got == ["Indian general election 2024 result"]


def test_a_version_named_two_turns_ago_is_kept(monkeypatch):
    """A terse follow-up carries its subject in the history, not the message."""
    _rewrites(monkeypatch, ["Python 3.13 free-threading status"])
    history = [{"role": "user", "content": "what is new in Python 3.13?"}]
    got = asyncio.run(search.rewrite_queries("and the free-threaded build?", history))
    assert got == ["Python 3.13 free-threading status"]


def test_a_product_name_that_contains_digits_is_not_a_pin():
    """S2 depends on this: "GPT-5.2 reasoning score" must survive intact, or
    the follow-up resolution it feeds resolves to nothing."""
    got = search._strip_unasked_pins(
        "and its score?", [], ["GPT-5.2 reasoning score BenchLM leaderboard"]
    )
    assert got == ["GPT-5.2 reasoning score BenchLM leaderboard"]


def test_stripping_a_pin_never_leaves_a_query_too_short_to_search():
    got = search._strip_unasked_pins("what happened?", [], ["news 2024"])
    assert got == ["news 2024"], "two words is the floor; a bare 'news' is not a search"


def test_two_rewrites_that_collapse_into_one_do_not_spend_two_slots():
    got = search._strip_unasked_pins(
        "who is the CEO of Intel?", [], ["Intel CEO 2024", "Intel CEO 2025"]
    )
    assert got == ["Intel CEO"]


def test_the_rewriter_prompt_says_what_today_is(monkeypatch):
    _rewrites(monkeypatch, ["q"])
    asyncio.run(search.rewrite_queries("anything", []))
    assert search._today_iso() in _rewrites.system
    assert "unless the user gave one" in _rewrites.system


# ---------------------------------------------------------------------------
# 5 — a page nobody could open is not evidence
# ---------------------------------------------------------------------------


def _mixed_sources(read, snippets):
    out = [
        search._Source(n=i + 1, title=f"read {i}", url=f"https://r{i}.test/a",
                       text="a page that was read")
        for i in range(read)
    ]
    out += [
        search._Source(n=read + i + 1, title=f"blurb {i}",
                       url=f"https://s{i}.test/a", text="one line from the engine",
                       from_snippet=True)
        for i in range(snippets)
    ]
    return out


def test_a_page_that_403d_is_not_offered_as_evidence_when_others_were_read():
    """The audit's weather turn cited [2] — a timeanddate.com page that had
    403'd — for "current temperatures hovering around 30°C"."""
    kept = search._drop_unread_sources(_mixed_sources(read=12, snippets=6))
    assert len(kept) == 12
    assert all(not s.from_snippet for s in kept)
    assert [s.n for s in kept] == list(range(1, 13)), "[n] indexes this list"


def test_a_thin_result_set_keeps_its_pointers():
    """Below `_MIN_SOURCES` read pages a blurb is worth having, labelled."""
    sources = _mixed_sources(read=3, snippets=2)
    kept = search._drop_unread_sources(sources)
    assert len(kept) == 5
    assert search._SNIPPET_LABEL in search._context_block(kept)


def test_a_set_with_nothing_to_drop_is_returned_unchanged():
    sources = _mixed_sources(read=12, snippets=0)
    assert search._drop_unread_sources(sources) is sources


def test_the_read_count_stops_counting_pages_nobody_opened(monkeypatch):
    """The panel said "Web · 15 · 15 read" for a turn in which six pages had
    403'd; `len(sources)` counted the blurbs."""
    sources = _mixed_sources(read=3, snippets=2)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    async def fake_rewrite(message, hist, effort="medium"):
        return ["q"]

    async def fake_collect(queries, effort="medium", emit=None, categories="",
                           degraded=None, candidates=None):
        return [result("https://r0.test/a")]

    async def fake_rerank(message, res, target, per_domain=0):
        return res

    async def fake_fetch(res, message="", **kw):
        return sources

    async def fake_memory(message, srcs, budget=3):
        return srcs

    async def fake_stream(messages, **kwargs):
        yield "token", "answer [1]"

    monkeypatch.setattr(search, "rewrite_queries", fake_rewrite)
    monkeypatch.setattr(search, "_collect_results", fake_collect)
    monkeypatch.setattr(search, "_rerank_results", fake_rerank)
    monkeypatch.setattr(search, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(search, "_memory_sources", fake_memory)
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(search.llm, "stream_chat_events", fake_stream)

    asyncio.run(search.run_search_engine("q", [], emit, "think"))

    read = [d for k, d in events if k == "research" and d.get("phase") == "read"]
    assert read and read[0]["count"] == 3, read
