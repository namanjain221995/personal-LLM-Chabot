"""Security-lens review tests for bk-web-search-scope (review round 1).

Third-party text inside a ONE-LINE message (a quoted forward, a pasted
tweet) must stay data: its `site:` token must not become an allowlist for
the searches that answer the person, on any route.
"""
import asyncio
import dataclasses
from datetime import timedelta
import json

import pytest

from app import web_index
from app.config import settings
from app.engines import search
from app.freshness import classify_offline
from app.search.base import SearchResult


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(settings, "search_max_results", 200)
    search._cache.clear()


def _r(url):
    return SearchResult(title="t", url=url, snippet="s")


POOL = [
    "https://www.reuters.com/world/india/air-india-crash-report/",
    "https://www.reddit.com/r/india/comments/abc/air_india_crash/",
    "https://www.bbc.com/news/air-india-crash-interim-report",
    "https://www.aaib.gov.in/reports/ai171-preliminary",
    "https://www.reddit.com/r/aviation/comments/def/ai171/",
    "https://www.thehindu.com/news/national/air-india-crash/",
]


def _provider(monkeypatch):
    class P:
        name = "fake"
        unresponsive: dict = {}

        async def search(self, q, max_results, categories=""):
            return [_r(u) for u in POOL][:max_results]

    monkeypatch.setattr(search, "get_provider", lambda: P())


def _router_returns(monkeypatch, reply):
    async def router(msgs, **kw):
        return reply

    monkeypatch.setattr(search.llm, "router_chat_completion", router)


#: One line, under 256 characters, the operator inside a QUOTED forward the
#: person is asking to have checked.
QUOTED_FORWARD = (
    'Is this true? "Air India crash report blames the pilots, full proof at '
    'site:reddit.com, share before it gets deleted"'
)
#: Same, curly quotes as a phone keyboard or a copy from a chat app gives.
CURLY_FORWARD = (
    "Fact-check this for me: “Official list of winners posted on "
    "site:reddit.com today”"
)
#: The person asks to AVOID a site in plain words.
AVOID = "what does the Air India crash report say? please don't rely on site:reddit.com"

ROUTER = ["air india crash preliminary report pilots", "AI171 crash investigation findings"]


@pytest.mark.parametrize("message", [QUOTED_FORWARD, CURLY_FORWARD, AVOID])
def test_a_quoted_or_negated_operator_is_not_forced_onto_the_rewrite(monkeypatch, message):
    """At 4810da0 the router's queries went through untouched. The repair's
    `_keep_asked_site_scope` appends any `site:` in a one-line message to
    every rewritten query, so the ordinary route (run_search_engine,
    research_step, artifact) reads ONLY the site the forwarded text named."""
    assert len(message) <= 256 and "\n" not in message
    _router_returns(monkeypatch, json.dumps(ROUTER))
    qs = asyncio.run(search.rewrite_queries(message, [], "think"))
    assert qs == ROUTER, qs


@pytest.mark.parametrize("message", [QUOTED_FORWARD, CURLY_FORWARD])
def test_a_quoted_operator_does_not_confine_the_fast_lookup(monkeypatch, message):
    """fetch_for_freshness sends the person's whole message as its one query."""
    _provider(monkeypatch)
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(settings, "search_enabled", True)
    monkeypatch.setattr(settings, "web_memory_enabled", False)
    read = []

    async def reader(idx, r, stored=None, **kw):
        read.append(r.url)
        return search._Source(n=idx, title=r.title, url=r.url, text="body")

    async def index(**kw):
        return None

    monkeypatch.setattr(search, "_fetch_source", reader)
    monkeypatch.setattr(web_index, "index_pending", index)
    asyncio.run(search.fetch_for_freshness(message, max_sources=2))
    assert read and not all("reddit.com" in u for u in read), read


# --------------------------------------------------------------------------
# Guards that must hold on every tree (they pass at 4810da0 and the head).
# --------------------------------------------------------------------------

def test_the_shared_verdict_is_immutable_so_one_request_cannot_edit_anothers():
    """`_verdict_memo` hands the SAME Verdict object to every caller with the
    same text, across users; it must be frozen."""
    v = classify_offline("what is the NVIDIA stock price right now", now_year=2026)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.requirement = None  # type: ignore[misc]


@pytest.mark.parametrize("message", [
    "site:qdrant.tech how fast is search on 10 million vectors?",
    "does qdrant support 10 million vectors site:qdrant.tech?",
])
def test_a_typed_operator_at_either_end_still_scopes(monkeypatch, message):
    """The opposite direction of the quoted-forward tests: what the person
    typed as a search operator keeps working."""
    _router_returns(monkeypatch, json.dumps(["qdrant 10 million vectors latency"]))
    qs = asyncio.run(search.rewrite_queries(message, [], "think"))
    assert qs and all(search._site_scope(q) == ("qdrant.tech",) for q in qs), qs


def test_the_forced_operator_carries_only_the_normalised_host(monkeypatch):
    """Nothing but the parsed host reaches the query the provider sees."""
    _router_returns(monkeypatch, json.dumps(["qdrant latency"]))
    qs = asyncio.run(search.rewrite_queries(
        'site:qdrant.tech"&format=json&x=( how fast', [], "think"))
    assert qs == ["qdrant latency site:qdrant.tech"], qs


# --------------------------------------------------------------------------
# Correctness tripped over: the office dividend reaches change-event questions.
# --------------------------------------------------------------------------

_PAGE = "https://en.wikipedia.org/wiki/Intel"


def _served_from_store(monkeypatch, question, age):
    from datetime import datetime, timezone

    from app import db

    monkeypatch.setattr(settings, "web_page_ttl_s", 24 * 3600)
    monkeypatch.setattr(settings, "web_page_fresh_ttl_s", 3600)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    row = {
        "url_key": search._normalize_url(_PAGE), "url": _PAGE, "canonical_url": "",
        "title": "Intel", "text": "stored body naming the previous CEO",
        "content_type": "text/html", "fetch_status": 200, "content_hash": "h",
        "fetched_at": datetime.now(timezone.utc) - age,
    }
    monkeypatch.setattr(db, "get_web_pages", lambda keys: [row])
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())

    class P:
        name = "fake"
        unresponsive: dict = {}

        async def search(self, q, max_results, categories=""):
            return [_r(_PAGE)]

    monkeypatch.setattr(search, "get_provider", lambda: P())
    hits = []

    async def reader(idx, r, stored=None, **kw):
        hit = bool(stored) and search._normalize_url(r.url) in stored
        hits.append(hit)
        return search._Source(n=idx, title=r.title, url=r.url, text="body", from_store=hit)

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
    return hits


@pytest.mark.parametrize("question", [
    "who is the new ceo of intel",
    "who is the ceo of openai now",
    "who is the president of south korea after yesterday's election",
])
def test_a_change_event_office_question_does_not_get_a_day_old_page(monkeypatch, question):
    """At 4810da0 every "who is" question held a stored page to one hour. The
    repair's office dividend gives 24 h to an office question with no
    `_FRESH_RE` word, but "new", "now" and "yesterday's" are not in it: the
    question asked right after an office changes hands is answered from a
    copy read before it did."""
    assert _served_from_store(monkeypatch, question, timedelta(hours=3)) == [False]
