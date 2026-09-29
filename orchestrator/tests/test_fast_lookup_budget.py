"""The Fast lookup's budget keeps the pages that landed.

MEASURED 2026-09-29 (Fast, component harness on the live stack): 3 of 10
lookups ran to the 8.0 s deadline (knowledge_fast_lookup_seconds
outcome=deadline, 8,005-8,007 ms) and returned NOTHING, so those turns answered
at 8.8-9.9 s from stale local passages. `_fast_lookup`'s asyncio.timeout was
the only bound and it cancelled the whole fetch, including pages already
read and stored; FETCH_TIMEOUT_MS (8 s) was as long as the whole budget, and
`index_pending` had its own fixed 6 s inside it.

Now `fetch_for_freshness` takes what is left of the budget: the provider
search, each page read, the store writes and `index_pending` are all bounded
by it, the pages that landed are counted and read back, and `_fast_lookup`'s
timeout is only a backstop. The 8 s deadline itself is unchanged.

The budgets here are SET SMALL (1 s, 0.5 s) rather than waited out.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from app import db, web_index
from app import living_knowledge as lk
from app.config import settings
from app.engines import search
from app.freshness import Freshness, Verdict
from app.search.base import SearchResult
from app.web_memory import Evidence, Retrieval

VERDICT = Verdict(Freshness.REALTIME, 300, "lexical:realtime", volatile=True)
QUESTION = "usd to inr exchange rate right now"
A = "https://a.example.org/rate"
B = "https://b.example.net/rate"


class _Web:
    """A provider, a reader and an index that do no I/O, timed by the test."""

    def __init__(self, monkeypatch, *, delays=None, index_s=0.0, search_s=0.0, snippet=(),
                 store_s=0.0):
        self.delays = {A: 0.2, B: None} if delays is None else delays  # None = hangs
        self.read_cancelled = []
        self.index_cancelled = False
        self.stages = []
        self.readbacks = []
        results = [SearchResult(title="A", url=A, snippet="sa"), SearchResult(title="B", url=B, snippet="sb")]

        async def collect(queries, effort="medium", emit=None, categories=""):
            await asyncio.sleep(search_s)
            return results

        async def reader(idx, r, stored=None, *, landed=None, **kw):
            delay = self.delays.get(r.url)
            try:
                if delay is None:
                    await asyncio.Event().wait()
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                self.read_cancelled.append(r.url)
                raise
            if r.url in snippet:
                return search._Source(n=idx, title=r.title, url=r.url, text=r.snippet, from_snippet=True)
            if landed is not None:
                landed[r.url] = asyncio.ensure_future(asyncio.sleep(store_s))
            return search._Source(n=idx, title=r.title, url=r.url, text="body")

        async def index(**kw):
            try:
                await asyncio.sleep(index_s)
            except asyncio.CancelledError:
                self.index_cancelled = True
                raise

        async def readback(q, *, level=Freshness.RECENT, **kw):
            self.readbacks.append(kw.get("use_cache"))
            return Retrieval(query=q, freshness=level, newest_age=1.0, evidence=[Evidence(
                url=A, title="A", text="1 USD = 83.2 INR", domain="a.example.org", authority=50,
                fetched_at=None, lexical=0.9)])

        def stage(seconds, *, stage, outcome):
            self.stages.append((stage, outcome, seconds))

        monkeypatch.setattr(settings, "search_enabled", True)
        monkeypatch.setattr(settings, "web_memory_enabled", False)
        monkeypatch.setattr(settings, "freshness_fast_lookup", True)
        monkeypatch.setattr(settings, "index_pending_timeout_ms", 6000)
        monkeypatch.setattr(settings, "freshness_fast_second_source_grace_s", 0.0, raising=False)
        monkeypatch.setattr(search, "_collect_results", collect)
        monkeypatch.setattr(search, "_fetch_source", reader)
        monkeypatch.setattr(web_index, "index_pending", index)
        monkeypatch.setattr(search, "_log_search_background", lambda *a: None)
        monkeypatch.setattr(search.metrics, "knowledge_fast_lookup", stage)
        monkeypatch.setattr(lk.metrics, "knowledge_fast_lookup", stage)
        monkeypatch.setattr(lk, "retrieve", readback)

    def outcome(self, name):
        return [o for s, o, _ in self.stages if s == name]


def _lookup(budget: float):
    async def go():
        started = time.perf_counter()
        async with asyncio.timeout(budget + 3.0):  # never hangs the suite
            out = await lk._fast_lookup(QUESTION, VERDICT)
        return out, time.perf_counter() - started

    return asyncio.run(go())


def test_a_page_that_landed_is_read_back_when_the_other_hangs(monkeypatch):
    """dev: the 1 s deadline cancels everything and the lookup returns None."""
    web = _Web(monkeypatch)
    monkeypatch.setattr(settings, "freshness_fast_deadline_s", 1.0)
    out, took = _lookup(1.0)
    assert out is not None and out.found, "the page that landed at 0.2 s is kept"
    assert web.readbacks == [False], "and read back, bypassing the cache"
    assert web.read_cancelled == [B]
    assert took < 1.0 + lk.FAST_BACKSTOP_S
    assert web.outcome("page") == ["deadline"]
    assert web.outcome("fetch") == ["ok"] and web.outcome("readback") == ["ok"]


def test_fetch_for_freshness_counts_the_page_that_landed(monkeypatch):
    _Web(monkeypatch)

    async def go():
        return await search.fetch_for_freshness(QUESTION, max_sources=2, budget_s=0.6)

    assert asyncio.run(go()) == 1


def test_a_slow_index_is_cut_at_what_is_left_not_at_six_seconds(monkeypatch):
    """dev: index_pending keeps its 6 s inside the 0.5 s deadline, the
    deadline fires first and the lookup returns None."""
    web = _Web(monkeypatch, delays={A: 0.0, B: 0.0}, index_s=5.0)
    monkeypatch.setattr(settings, "freshness_fast_deadline_s", 0.5)
    out, took = _lookup(0.5)
    assert out is not None and web.readbacks == [False]
    assert web.index_cancelled
    assert took < 0.5 + lk.FAST_BACKSTOP_S
    assert web.outcome("index") == ["deadline"]


def test_a_budget_already_spent_skips_the_index_and_keeps_the_pages(monkeypatch):
    web = _Web(monkeypatch, delays={A: 0.3, B: 0.3})

    async def go():
        return await search.fetch_for_freshness(QUESTION, max_sources=2, budget_s=0.3)

    assert asyncio.run(go()) in (0, 1, 2)  # whatever landed by 0.3 s
    assert web.outcome("index") == ["deadline"] and not web.index_cancelled


def test_a_hanging_provider_is_cut_at_the_budget(monkeypatch):
    web = _Web(monkeypatch, search_s=5.0)

    async def go():
        started = time.perf_counter()
        n = await search.fetch_for_freshness(QUESTION, budget_s=0.3)
        return n, time.perf_counter() - started

    n, took = asyncio.run(go())
    assert n == 0 and took < 1.0
    assert web.outcome("search") == ["deadline"]


def test_a_snippet_is_not_a_stored_page(monkeypatch):
    """A page that fell back to the provider's blurb is not in the store, so
    there is nothing new to read back (dev counted it and re-read the same
    stale passages as a "fast_lookup")."""
    _Web(monkeypatch, delays={A: 0.0, B: 0.0}, snippet=(A, B))

    async def go():
        return await search.fetch_for_freshness(QUESTION, budget_s=1.0)

    assert asyncio.run(go()) == 0


def test_a_store_write_that_did_not_land_is_not_counted(monkeypatch):
    _Web(monkeypatch, delays={A: 0.0, B: 0.0}, store_s=2.0)

    async def go():
        return await search.fetch_for_freshness(QUESTION, budget_s=0.3)

    assert asyncio.run(go()) == 0


def test_every_sub_stage_is_timed(monkeypatch):
    web = _Web(monkeypatch, delays={A: 0.0, B: 0.05})

    async def go():
        return await search.fetch_for_freshness(QUESTION, budget_s=2.0)

    assert asyncio.run(go()) == 2
    assert [s for s, _, _ in web.stages] == ["search", "second_source_lag", "page", "index"]
    assert web.outcome("search") == ["ok"] and web.outcome("page") == ["ok"] and web.outcome("index") == ["ok"]
    (lag,) = [sec for s, _, sec in web.stages if s == "second_source_lag"]
    assert 0.02 < lag < 1.0


def test_the_second_source_grace_is_off_by_default_and_opt_in(monkeypatch):
    assert float(getattr(settings, "freshness_fast_second_source_grace_s", 0.0)) == 0.0
    web = _Web(monkeypatch, delays={A: 0.05, B: None})
    monkeypatch.setattr(settings, "freshness_fast_second_source_grace_s", 0.1, raising=False)

    async def go():
        started = time.perf_counter()
        n = await search.fetch_for_freshness(QUESTION, budget_s=5.0)
        return n, time.perf_counter() - started

    n, took = asyncio.run(go())
    assert n == 1 and took < 1.0, "one page and the grace, not the whole budget"
    assert web.outcome("page") == ["ok"]
    assert web.outcome("second_source_lag") == ["deadline"]


def test_no_budget_keeps_the_per_stage_bounds_only(monkeypatch):
    _Web(monkeypatch, delays={A: 0.0, B: 0.01})

    async def go():
        return await search.fetch_for_freshness(QUESTION)

    assert asyncio.run(go()) == 2


def test_the_fetch_timeout_is_only_a_backstop(monkeypatch):
    """A fetch that ignores its budget is still cut, at budget + backstop."""
    monkeypatch.setattr(settings, "freshness_fast_lookup", True)
    monkeypatch.setattr(settings, "freshness_fast_deadline_s", 0.2)

    async def stuck(query, **kw):
        assert kw["budget_s"] == 0.2
        await asyncio.Event().wait()

    monkeypatch.setattr(search, "fetch_for_freshness", stuck)
    out, took = _lookup(0.2)
    assert out is None
    assert 0.2 + lk.FAST_BACKSTOP_S - 0.05 < took < 0.2 + lk.FAST_BACKSTOP_S + 0.5


@pytest.mark.parametrize("n_pages", [1, 2])
def test_a_page_served_from_the_store_counts(monkeypatch, n_pages):
    _Web(monkeypatch, delays={A: 0.0, B: 0.0})

    async def from_store(idx, r, stored=None, **kw):
        return search._Source(n=idx, title=r.title, url=r.url, text="stored", from_store=True)

    monkeypatch.setattr(search, "_fetch_source", from_store)

    async def go():
        return await search.fetch_for_freshness(QUESTION, max_sources=n_pages, budget_s=1.0)

    assert asyncio.run(go()) == n_pages


def test_the_log_still_names_every_page_picked(monkeypatch):
    web = _Web(monkeypatch)
    logged = []
    monkeypatch.setattr(search, "_log_search_background", lambda *a: logged.append(a))
    monkeypatch.setattr(db, "run_in_thread", _run_now)

    async def go():
        n = await search.fetch_for_freshness(QUESTION, user_id=7, conversation_id="c1", budget_s=0.5)
        await asyncio.gather(*list(search._BACKGROUND_TASKS))
        return n

    assert asyncio.run(go()) == 1
    (call,) = logged
    assert [r.url for r in call[2]] == [A, B] and call[4:] == (7, "c1")
    assert web.read_cancelled == [B]


async def _run_now(fn, *args):
    return fn(*args)


@pytest.mark.parametrize("stage", ["search", "page", "index", "second_source_lag"])
def test_the_sub_stages_reach_the_scrape_under_their_own_names(stage):
    # Measured in the container on 2026-09-29: every sub-stage above came out
    # of /metrics as stage="other", because the metric's closed label
    # vocabulary knew only "fetch" and "readback". The split the stage timing
    # exists for was then invisible to anyone reading the scrape.
    from app import metrics

    metrics.knowledge_fast_lookup(0.01, stage=stage, outcome="ok")
    rendered = metrics.render()
    assert f'knowledge_fast_lookup_seconds_count{{outcome="ok",stage="{stage}"}}' in rendered
