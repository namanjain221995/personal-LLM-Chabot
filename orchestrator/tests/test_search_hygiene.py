"""Search hygiene: nothing private leaves the box, and every lookup is owned.

main.py pins the user's saved facts, the cross-chat recall block and the
excerpts of pages/documents shared in this chat to `history` as system
messages. `recent_turns` keeps them on purpose — the answer prompt needs
them. The search query rewriter used the same helper, so those blocks were
handed to the query model and could come back out as SearXNG queries
(security review 2026-09-03). These tests pin the three fixes from that
review: system-stripped turns on the query path, attribution of the
Fast-mode freshness lookup, and a warm-cache TTL that follows the freshness
verdict instead of re-matching a second regex.

All I/O mocked; no network, no model.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app import db, llm, web_index
from app.config import settings
from app.engines import conversation_turns, recent_turns
from app.engines import search
from app.freshness import Freshness, Verdict, classify_offline
from app.search.base import SearchResult

# Three kinds of private text, each distinctive enough that a substring check
# is unambiguous. Shaped like the real blocks (facts.facts_block, the
# cross-chat recall block, the uploaded-document block in main.py).
FACT = "works at Acme Robotics and reports to Priya Venkatesan"
RECALL = 'From "salary talk" (you answered): your offer was 240k base'
DOC = "Confidential term sheet: Series B at a 90M pre-money valuation"
PRIVATE = (FACT, RECALL, DOC)


def _history_with_private_blocks():
    """What `history` looks like by the time an engine sees it."""
    return [
        {
            "role": "system",
            "content": "Durable facts this user has told you in past "
            "conversations:\n- " + FACT,
        },
        {
            "role": "system",
            "content": "Relevant context from the user's other conversations:\n- "
            + RECALL,
        },
        {
            "role": "system",
            "content": "Documents the user uploaded earlier in this chat:\n"
            "[1] termsheet.pdf\n" + DOC,
        },
        {"role": "user", "content": "what does vLLM do"},
        {"role": "assistant", "content": "vLLM is an inference server."},
        {"role": "user", "content": "and continuous batching?"},
        {"role": "assistant", "content": "It schedules requests token by token."},
    ]


def _flatten(msgs):
    return "\n".join(str(m.get("content") or "") for m in msgs)


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


def test_conversation_turns_drops_system_blocks_and_keeps_the_last_n():
    history = _history_with_private_blocks()
    turns = conversation_turns(history, 2)
    assert [m["role"] for m in turns] == ["user", "assistant"]
    assert turns[0]["content"] == "and continuous batching?"
    # The contrast that makes this a separate helper: recent_turns PINS them.
    assert sum(m["role"] == "system" for m in recent_turns(history, 2)) == 3


def test_conversation_turns_with_zero_is_empty():
    # recent_turns(history, 0) is "only the system blocks"; stripped, nothing.
    assert conversation_turns(_history_with_private_blocks(), 0) == []


# ---------------------------------------------------------------------------
# The query rewriter: the prompt that becomes outbound queries
# ---------------------------------------------------------------------------


def test_rewriter_prompt_carries_no_private_context(monkeypatch):
    seen = {}

    async def capture(messages, **kwargs):
        seen["messages"] = messages
        return '["vllm continuous batching throughput"]'

    monkeypatch.setattr(llm, "router_chat_completion", capture)
    out = asyncio.run(
        search.rewrite_queries("how fast is it?", _history_with_private_blocks(), "think")
    )
    assert out == ["vllm continuous batching throughput"]

    prompt = _flatten(seen["messages"])
    for secret in PRIVATE:
        assert secret not in prompt, secret
    # Stripping must not blind the rewriter to the conversation: the
    # follow-up "how fast is it?" only makes sense with the prior turns.
    assert "continuous batching" in prompt
    assert seen["messages"][-1] == {"role": "user", "content": "how fast is it?"}
    # Exactly one system message — the rewriter's own instruction.
    assert [m["role"] for m in seen["messages"]].count("system") == 1


# ---------------------------------------------------------------------------
# Auto-mode decision
# ---------------------------------------------------------------------------


def test_should_search_prompt_carries_no_private_context(monkeypatch):
    seen = {}

    async def capture(messages, **kwargs):
        seen["messages"] = messages
        return "no"

    monkeypatch.setattr(llm, "router_chat_completion", capture)
    # Wording the regex heuristic does NOT settle, so the model is consulted.
    question = "and is that still true?"
    assert search._FRESH_RE.search(question) is None
    assert (
        asyncio.run(search.should_search(question, _history_with_private_blocks()))
        is False
    )
    prompt = _flatten(seen["messages"])
    for secret in PRIVATE:
        assert secret not in prompt, secret
    assert "continuous batching" in prompt
    assert seen["messages"][-1] == {"role": "user", "content": question}


def test_should_search_without_history_is_the_old_two_message_prompt(monkeypatch):
    """main.py still calls it with the message alone; that path is unchanged."""
    seen = {}

    async def capture(messages, **kwargs):
        seen["messages"] = messages
        return "yes"

    monkeypatch.setattr(llm, "router_chat_completion", capture)
    assert asyncio.run(search.should_search("is that still true?")) is True
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]


# ---------------------------------------------------------------------------
# Fast-mode freshness lookup: attributed, not anonymous
# ---------------------------------------------------------------------------


def _seed_freshness_pipeline(monkeypatch, logged):
    """A provider, a reader and a store that record instead of doing I/O."""
    monkeypatch.setattr(settings, "search_enabled", True)
    results = [
        SearchResult(title="A", url="https://a.example/x", snippet="sa"),
        SearchResult(title="B", url="https://b.example/y", snippet="sb"),
        SearchResult(title="A2", url="https://a.example/z", snippet="sa2"),
    ]

    async def fake_collect(queries, effort="medium", emit=None, categories=""):
        return results

    async def fake_fetch_sources(picked, message="", **attribution):
        # The reader now carries user_id/conversation_id (V16 introducer) and
        # the freshness verdict; this fake records nothing about them.
        return [
            search._Source(n=i + 1, title=r.title, url=r.url, text="body")
            for i, r in enumerate(picked)
        ]

    async def fake_index():
        return None

    async def fake_run_in_thread(fn, *args):
        logged.append((fn, args))

    monkeypatch.setattr(search, "_collect_results", fake_collect)
    monkeypatch.setattr(search, "_fetch_sources", fake_fetch_sources)
    monkeypatch.setattr(web_index, "index_pending", fake_index)
    monkeypatch.setattr(db, "run_in_thread", fake_run_in_thread)


async def _run_and_drain(coro):
    out = await coro
    # The log is written behind the answer (_spawn); wait for it to land.
    pending = list(search._BACKGROUND_TASKS)
    if pending:
        await asyncio.gather(*pending)
    return out


def _log_calls(logged):
    return [args for fn, args in logged if fn is search._log_search_background]


def test_fetch_for_freshness_attributes_the_lookup(monkeypatch):
    logged = []
    _seed_freshness_pipeline(monkeypatch, logged)
    n = asyncio.run(
        _run_and_drain(
            search.fetch_for_freshness(
                "who is the ceo of acme",
                max_sources=2,
                user_id=42,
                conversation_id="conv-7",
            )
        )
    )
    assert n == 2
    (call,) = _log_calls(logged)
    message, queries, picked, effort, user_id, conversation_id = call
    assert (user_id, conversation_id) == (42, "conv-7")
    assert message == "who is the ceo of acme" and queries == [message]
    assert effort == "fast"
    # One page per registrable domain, capped at max_sources — unchanged.
    assert [r.url for r in picked] == ["https://a.example/x", "https://b.example/y"]


def test_fetch_for_freshness_defaults_stay_anonymous(monkeypatch):
    """A caller without ids (the current living_knowledge path) still logs."""
    logged = []
    _seed_freshness_pipeline(monkeypatch, logged)
    asyncio.run(_run_and_drain(search.fetch_for_freshness("latest vllm release")))
    (call,) = _log_calls(logged)
    assert call[4:] == (None, "")


# ---------------------------------------------------------------------------
# Warm-cache TTL from the verdict
# ---------------------------------------------------------------------------


def test_page_ttl_follows_the_verdict_not_the_regex(monkeypatch):
    monkeypatch.setattr(settings, "web_page_ttl_s", 24 * 3600)
    monkeypatch.setattr(settings, "web_page_fresh_ttl_s", 3600)
    # "who is" trips _FRESH_RE, but an office-holder question is RECENT and
    # NOT volatile: its answer is stable for months, so a stored page is too.
    q = "who is the ceo of acme robotics"
    assert search._FRESH_RE.search(q), "fixture must be one the regex would shorten"
    stable = classify_offline(q, now_year=2026)
    assert stable.requirement is Freshness.RECENT and not stable.volatile
    assert search._page_ttl(q, stable) == 24 * 3600
    # No verdict: the regex fallback, exactly as before.
    assert search._page_ttl(q) == 3600
    # A volatile verdict shortens it whatever the wording says.
    hot = classify_offline("latest vllm release", now_year=2026)
    assert hot.volatile
    assert search._page_ttl("latest vllm release", hot) == 3600
    # Hand-built verdicts on neutral wording: the flag alone decides.
    neutral = "tell me about the page"
    assert search._page_ttl(neutral, Verdict(Freshness.RECENT, 86400, "t", volatile=True)) == 3600
    assert search._page_ttl(neutral, Verdict(Freshness.STATIC, 86400, "t")) == 24 * 3600


def test_stored_pages_use_the_verdict_ttl(monkeypatch):
    key = search._normalize_url("https://example.com/page")
    three_hours = datetime.now(timezone.utc) - timedelta(hours=3)
    row = {
        "url_key": key, "url": "https://example.com/page", "canonical_url": "",
        "title": "T", "text": "body", "content_type": "text/html",
        "fetch_status": 200, "content_hash": "h", "fetched_at": three_hours,
    }
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "web_page_ttl_s", 24 * 3600)
    monkeypatch.setattr(settings, "web_page_fresh_ttl_s", 3600)
    monkeypatch.setattr(db, "get_web_pages", lambda keys: [row])
    results = [SearchResult(title="T", url="https://example.com/page", snippet="s")]

    q = "who is the ceo of acme robotics"
    stable = classify_offline(q, now_year=2026)
    # The verdict says months-stable: the 3-hour copy is served warm.
    assert key in asyncio.run(search._stored_pages(results, q, verdict=stable))
    # Same wording, no verdict: "who is" -> 1 h -> stale, goes to the network.
    assert asyncio.run(search._stored_pages(results, q)) == {}
    # A volatile verdict: stale, whatever the regex thinks.
    hot = classify_offline("latest release notes", now_year=2026)
    assert asyncio.run(search._stored_pages(results, "latest release notes", verdict=hot)) == {}
