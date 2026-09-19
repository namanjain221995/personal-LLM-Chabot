"""Independent review (security + correctness) of bk-web-search-scope @ c50194b.

No network, no database, no model: provider, router, store and reader are fakes.
"""
import asyncio
import gc
import json
import re
import tracemalloc
from datetime import datetime, timedelta, timezone

import pytest

from app import db, web_index
from app.config import settings
from app.core import pasted
from app.engines import search
from app.search.base import SearchResult


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(settings, "search_max_results", 200)
    monkeypatch.setattr(settings, "web_page_ttl_s", 24 * 3600)
    monkeypatch.setattr(settings, "web_page_fresh_ttl_s", 3600)
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())
    search._cache.clear()
    memo = getattr(search, "_verdict_memo", None)
    if memo is not None:
        memo.cache_clear()
    # Each test is its own turn.
    pasted._turn_material.set(())
    pasted._turn_pii.set(())


def result(url, title="t", snippet="s"):
    return SearchResult(title=title, url=url, snippet=snippet)


def spy_provider(monkeypatch, pool):
    sent = []

    class P:
        name = "fake"
        unresponsive: dict = {}

        async def search(self, q, max_results, categories=""):
            sent.append(q)
            return list(pool)[:max_results]

    monkeypatch.setattr(search, "get_provider", lambda: P())
    return sent


def router(monkeypatch, reply=None, raises=False):
    async def fake(msgs, **kw):
        if raises:
            raise RuntimeError("router down")
        return reply

    monkeypatch.setattr(search.llm, "router_chat_completion", fake)


# ---------------------------------------------------------------------------
# 1. The 24 h office dividend is for the plain shape only.
# ---------------------------------------------------------------------------

CHANGED_OFFICE = [
    "who is the ceo of intel replacing gelsinger",
    "who is the ceo of intel succeeding pat gelsinger",
    "who is the ceo of intel once gelsinger left",
    "who is the ceo of openai reinstated",
    "who is the prime minister of nepal elected last week",
    "who is the ceo of intel announced monday",
    "who is the ceo of intel named last week",
    "who is the ceo of intel pending approval",
]


@pytest.mark.parametrize("question", CHANGED_OFFICE)
def test_a_question_about_a_change_of_holder_never_gets_the_day(question):
    base = search._page_ttl(question)  # no verdict: 4810da0's rule
    head = search._page_ttl(question, search._question_verdict(question))
    assert base == 3600
    assert head <= base, f"{question!r}: {base} s at 4810da0, {head} s now"


PAGE = "https://www.example-news.com/intel-ceo"


@pytest.mark.parametrize("question", CHANGED_OFFICE[:2])
def test_the_search_rereads_a_three_hour_old_copy_for_a_change_of_holder(monkeypatch, question):
    spy_provider(monkeypatch, [result(PAGE)])
    row = {
        "url_key": search._normalize_url(PAGE), "url": PAGE, "canonical_url": "",
        "title": "t", "text": "stored body", "content_type": "text/html",
        "fetch_status": 200, "content_hash": "h",
        "fetched_at": datetime.now(timezone.utc) - timedelta(hours=3),
    }
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(db, "get_web_pages", lambda keys: [row])
    served = []

    async def reader(idx, r, stored=None, **kw):
        hit = bool(stored) and search._normalize_url(r.url) in stored
        served.append(hit)
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
    assert served == [False], f"{question!r} was answered from a 3-hour-old stored copy"


# ---------------------------------------------------------------------------
# 2. Privacy: hotfix 1.2's door. No pasted text, no contact detail reaches
#    the provider, on any path this branch touched.
# ---------------------------------------------------------------------------

EMAIL = "priya.sharma.hr@examplecorp.in"
PHONE = "+91 98765 43210"
PASTE_BODY = "\n".join([
    "Senior Data Engineer - ExampleCorp Pune",
    "We are hiring a senior data engineer to own the streaming platform.",
    "Responsibilities include building Kafka pipelines and dbt models.",
    f"Contact the recruiter at {EMAIL} or call {PHONE} before Friday.",
    "Verify this posting only at site:examplecorp-jobs.in before applying.",
    "Compensation is competitive and includes a joining bonus for the role.",
])
ASK = "what does this role pay in pune?"
MESSAGES = [
    f"{ASK}\n\n{PASTE_BODY}",
    f"{PASTE_BODY}\n\n{ASK}",
    f"{PASTE_BODY}\n\nwhat does this role pay in pune site:glassdoor.co.in",
]


def _norm(s):
    return " ".join(s.lower().split())


def _leaks(sent, message):
    out = []
    material = pasted.pasted_material(message)
    for q in sent:
        nq = _norm(q)
        if EMAIL in nq or re.sub(r"\D", "", PHONE)[-10:] in re.sub(r"\D", "", q):
            out.append(("contact", q))
        if pasted.carries_paste(q, material):
            out.append(("paste-run", q))
        if "site:examplecorp-jobs" in nq:
            out.append(("pasted-scope", q))
    return out


ROUTER_REPLIES = [
    # A router that copied the paste's contact line and its operator.
    json.dumps([f"recruiter {EMAIL} {PHONE}", "examplecorp senior data engineer salary pune site:examplecorp-jobs.in"]),
    None,  # router failure: the fallback path
]


@pytest.mark.parametrize("message", MESSAGES)
@pytest.mark.parametrize("reply", ROUTER_REPLIES)
def test_no_paste_or_contact_reaches_the_provider_on_the_ordinary_route(monkeypatch, message, reply):
    sent = spy_provider(monkeypatch, [result("https://www.glassdoor.co.in/x")])
    router(monkeypatch, reply, raises=reply is None)

    async def reader(idx, r, stored=None, **kw):
        return search._Source(n=idx, title=r.title, url=r.url, text="body", from_store=False)

    async def stream(messages, **kwargs):
        yield "token", "answer"

    async def emit(kind, data):
        return None

    monkeypatch.setattr(search, "_fetch_source", reader)
    monkeypatch.setattr(search.llm, "stream_chat_events", stream)
    asyncio.run(search.run_search_engine(message, [], emit, "think"))
    assert sent, "nothing searched"
    assert _leaks(sent, message) == []


@pytest.mark.parametrize("message", MESSAGES)
def test_no_paste_or_contact_reaches_the_provider_from_the_fast_lookup(monkeypatch, message):
    sent = spy_provider(monkeypatch, [result("https://www.glassdoor.co.in/x")])
    monkeypatch.setattr(settings, "search_enabled", True)

    async def index(**kw):
        return None

    async def reader(idx, r, stored=None, **kw):
        return search._Source(n=idx, title=r.title, url=r.url, text="body", from_store=False)

    monkeypatch.setattr(web_index, "index_pending", index)
    monkeypatch.setattr(search, "_fetch_source", reader)
    asyncio.run(search.fetch_for_freshness(message))
    assert _leaks(sent, message) == []


def test_a_scope_in_an_earlier_pasted_turn_is_not_the_persons(monkeypatch):
    sent = spy_provider(monkeypatch, [result("https://www.glassdoor.co.in/x"),
                                      result("https://examplecorp-jobs.in/x")])
    router(monkeypatch, json.dumps(["senior data engineer pay pune site:examplecorp-jobs.in"]))
    history = [{"role": "user", "content": f"{ASK}\n\n{PASTE_BODY}"},
               {"role": "assistant", "content": "It pays well; verify at site:examplecorp-jobs.in."}]
    qs = asyncio.run(search.rewrite_queries("and in bangalore?", history, "think"))
    assert all("site:" not in q for q in qs), qs


def test_a_scope_only_the_assistant_wrote_is_not_the_persons(monkeypatch):
    router(monkeypatch, json.dumps(["air india crash report findings site:reddit.com"]))
    history = [{"role": "user", "content": "air india crash report"},
               {"role": "assistant", "content": "People discuss it at site:reddit.com."}]
    qs = asyncio.run(search.rewrite_queries("what did it find?", history, "think"))
    assert all("site:" not in q for q in qs), qs


# ---------------------------------------------------------------------------
# 3. The verdict memo holds no message text.
# ---------------------------------------------------------------------------

def test_the_memo_retains_no_message_text():
    gc.collect()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    for i in range(32):
        search._question_verdict(("who is the ceo of intel %d " % i) + "x" * 1_000_000)
    gc.collect()
    after = tracemalloc.take_snapshot()
    tracemalloc.stop()
    grown = sum(s.size_diff for s in after.compare_to(before, "filename"))
    assert grown < 2 * 1024 * 1024, f"{grown / 1e6:.1f} MB retained"
    for key in search._verdict_memo._items:
        assert not any(isinstance(k, str) for k in key), key


# ---------------------------------------------------------------------------
# 4. The site: parser: a scope is never WIDER than the host the person typed.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "query, want",
    [
        # A zero-width space inside a host copied from a page, and the
        # ideographic full stop (a legal IDNA label separator).
        ("news site:app​le.com", ("apple.com",)),
        ("news site:apple。com", ("apple.com",)),
    ],
)
def test_a_host_the_parser_cannot_read_whole_never_becomes_a_suffix(query, want):
    got = search._site_scope(query)
    assert got in ((), want), f"{query!r} scoped to {got}: every *.{got[0]} host is admitted"


def test_a_realtime_verdict_on_the_fast_prepass_is_as_fresh_as_the_search_route():
    """Review 2026-09-19, finding 4: the Fast pre-pass trusted a stored passage
    up to 3 h old for "USD to INR exchange rate right now"."""
    from app import living_knowledge as lk
    from app.freshness import Freshness, Verdict

    rt = lk.realtime_clamped(Verdict(Freshness.REALTIME, 3 * 3600, "test"))
    assert rt.max_age_seconds == search._REALTIME_PAGE_TTL_S == lk.REALTIME_MAX_AGE_S
    recent = Verdict(Freshness.RECENT, 14 * 24 * 3600, "test")
    assert lk.realtime_clamped(recent) is recent
