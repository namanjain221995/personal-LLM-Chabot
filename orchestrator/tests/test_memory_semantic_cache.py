"""Cross-chat recall candidates are cached per user (2026-09-13, plan item 3e).

Every assistant turn re-read the newest 500 message_embeddings rows with their
full message content (754 kB of content for the owner) and normalised every
one on the event loop. What is pinned: a second turn inside the TTL reads no
rows, and answers exactly as the uncached path does; the backfill writing new
vectors invalidates; a deleted conversation is never recalled from the cache;
the TTL expires; the exclusion is part of the key; the ranking runs off the
loop.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from app import db, llm, memory_semantic
from app.config import settings

from tests.test_memory_semantic import _MAPPING, fake_embedder


class _Clock:
    def __init__(self):
        self.now = 5000.0

    def monotonic(self):
        return self.now


@pytest.fixture()
def corpus(monkeypatch):
    monkeypatch.setattr(llm, "embed_texts", fake_embedder(_MAPPING))

    async def embed_query(text, **kw):
        return (await fake_embedder(_MAPPING)([text]))[0]

    monkeypatch.setattr(llm, "embed_query", embed_query)
    monkeypatch.setattr(settings, "cross_chat_semantic_enabled", True)
    monkeypatch.setattr(settings, "embed_model", "test-embedder")
    monkeypatch.setattr(settings, "embed_base_url", "http://embed.test/v1")
    monkeypatch.setattr(memory_semantic, "CROSS_CHAT_EMBEDDINGS_CACHE_S", 60.0)
    # memory_semantic prefers the Settings attribute since 2026-09-13; pin both.
    monkeypatch.setattr(settings, "cross_chat_embeddings_cache_s", 60.0, raising=False)
    clock = _Clock()
    monkeypatch.setattr(memory_semantic, "time", clock)
    memory_semantic.invalidate_message_embeddings()

    uid = db.create_user("alice", "hash")
    db.create_conversation(uid, "c1", "Leadership")
    db.add_message(uid, "c1", "user", "Who is the CEO of TechSara please tell")
    db.add_message(uid, "c1", "assistant", "The CEO of TechSara is Sahil Patel.")
    db.create_conversation(uid, "c2", "Cooking")
    db.add_message(uid, "c2", "user", "How do I bake sourdough bread at home?")
    assert asyncio.run(memory_semantic.ensure_message_embeddings(uid)) == 3

    reads = {"n": 0}
    real = db.fetch_message_embeddings

    def counting(*a, **k):
        reads["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(db, "fetch_message_embeddings", counting)
    yield uid, reads, clock
    memory_semantic.invalidate_message_embeddings()


def _hits(uid, q="who runs the company?", exclude="new-conv"):
    return asyncio.run(memory_semantic.semantic_hits(uid, q, exclude))


def test_a_second_turn_inside_the_ttl_reads_no_rows_and_answers_the_same(corpus):
    uid, reads, clock = corpus
    first = _hits(uid)
    clock.now += 59.0
    second = _hits(uid)
    assert reads["n"] == 1
    assert first == second
    assert first and "Sahil Patel" in first[0]["snippet"]


def test_cached_answers_equal_the_uncached_path(corpus, monkeypatch):
    uid, _reads, _clock = corpus
    questions = [
        ("who runs the company?", "new-conv"),
        ("who is the ceo?", "c1"),
        ("who is the ceo?", "c2"),
        ("Who is the CEO of TechSara please tell", "new-conv"),  # the echo filter
        ("sourdough", None),
    ]

    def blocks():
        return [asyncio.run(memory_semantic.cross_chat_block(uid, q, ex)) for q, ex in questions]

    cached = [_hits(uid, q, ex) for q, ex in questions]
    cached_again = [_hits(uid, q, ex) for q, ex in questions]
    cached_blocks = blocks()
    monkeypatch.setattr(memory_semantic, "CROSS_CHAT_EMBEDDINGS_CACHE_S", 0.0)
    monkeypatch.setattr(settings, "cross_chat_embeddings_cache_s", 0.0, raising=False)
    uncached = [_hits(uid, q, ex) for q, ex in questions]
    assert cached == cached_again == uncached
    # The recall block the prompt carries, byte for byte.
    assert any(cached_blocks) and cached_blocks == blocks()


def test_new_embeddings_invalidate_the_cache(corpus):
    uid, reads, _clock = corpus
    assert all("Priya" not in h["snippet"] for h in _hits(uid))
    db.create_conversation(uid, "c3", "Board")
    db.add_message(uid, "c3", "assistant", "The company is now run by Priya Rao as CEO.")
    assert asyncio.run(memory_semantic.ensure_message_embeddings(uid)) == 1
    after = _hits(uid)
    assert reads["n"] == 2
    assert any("Priya Rao" in h["snippet"] for h in after)


def test_a_write_that_bypasses_the_backfill_is_still_seen(corpus):
    """Any writer of message_embeddings moves the fingerprint (count and
    newest id), so a vector stored by another path is not hidden for 60 s."""
    uid, reads, _clock = corpus
    _hits(uid)
    db.create_conversation(uid, "c4", "Other")
    mid = db.add_message(uid, "c4", "assistant", "Our company CEO changed last week.")
    pending = db.messages_missing_embeddings(uid, settings.embed_model, 10)
    from app.recall import pack_vector

    db.store_message_embeddings(
        uid, settings.embed_model, 3,
        [{"message_id": m["id"], "conversation_id": m["conversation_id"], "embedding": pack_vector([1.0, 0.0, 0.0])}
         for m in pending],
    )
    assert pending and mid is not None
    after = _hits(uid)
    assert reads["n"] == 2
    assert any("changed last week" in h["snippet"] for h in after)


def test_a_deleted_conversation_is_never_recalled_from_the_cache(corpus):
    uid, reads, _clock = corpus
    assert any("Sahil Patel" in h["snippet"] for h in _hits(uid))
    assert db.delete_conversation(uid, "c1")
    after = _hits(uid)
    assert reads["n"] == 2
    assert all("Sahil Patel" not in h["snippet"] for h in after)


def test_the_ttl_expires(corpus):
    uid, reads, clock = corpus
    _hits(uid)
    clock.now += 60.0
    _hits(uid)
    assert reads["n"] == 2


def test_the_excluded_conversation_is_part_of_the_key(corpus):
    uid, reads, _clock = corpus
    everything = _hits(uid, "who is the ceo?", "new-conv")
    without_c1 = _hits(uid, "who is the ceo?", "c1")
    assert reads["n"] == 2
    assert any(h["conversation_id"] == "c1" for h in everything)
    assert all(h["conversation_id"] != "c1" for h in without_c1)


def test_the_cache_is_per_user(corpus):
    uid, _reads, _clock = corpus
    _hits(uid)
    bob = db.create_user("bob", "hash")
    db.create_conversation(bob, "c9", "Bob private")
    db.add_message(bob, "c9", "user", "Our CEO bonus is 999999 dollars")
    assert asyncio.run(memory_semantic.ensure_message_embeddings(bob)) == 1
    assert all("999999" not in h["snippet"] for h in _hits(uid, "who is the ceo?"))
    assert any("999999" in h["snippet"] for h in _hits(bob, "who is the ceo?"))


def test_ranking_runs_off_the_event_loop(corpus, monkeypatch):
    uid, _reads, _clock = corpus
    seen = {}
    real = memory_semantic._rank_candidates

    def rank(*a, **k):
        seen["thread"] = threading.get_ident()
        return real(*a, **k)

    monkeypatch.setattr(memory_semantic, "_rank_candidates", rank)

    async def go():
        seen["loop"] = threading.get_ident()
        return await memory_semantic.semantic_hits(uid, "who runs the company?", "new-conv")

    assert asyncio.run(go())
    assert seen["thread"] != seen["loop"]


@pytest.mark.parametrize("raw, expected", [(None, 60.0), ("", 60.0), ("0", 0.0), ("15", 15.0)])
def test_the_tunable_parses_like_config_float(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("CROSS_CHAT_EMBEDDINGS_CACHE_S", raising=False)
    else:
        monkeypatch.setenv("CROSS_CHAT_EMBEDDINGS_CACHE_S", raw)
    assert memory_semantic._env_float("CROSS_CHAT_EMBEDDINGS_CACHE_S", 60.0) == expected


# ── second prover pass (2026-09-13) ───────────────────────────────────────────


def test_the_next_turn_of_the_same_conversation_reuses_the_rows_after_the_backfill_stored_the_last_one(corpus):
    """The cache never hit for someone chatting: every turn's background
    backfill stored the previous turn's vectors — in the conversation the
    turn excludes — and the user-wide fingerprint moved (fast_path_bench
    followup, instrumented: 0 hits, 17 misses)."""
    uid, reads, clock = corpus
    db.create_conversation(uid, "chat-now", "Now")
    db.add_message(uid, "chat-now", "user", "who runs the company?")
    first = asyncio.run(memory_semantic.cross_chat_block(uid, "who runs the company?", "chat-now"))
    # The turn's answer lands, and the backfill stores both new vectors.
    db.add_message(uid, "chat-now", "assistant", "Sahil Patel runs TechSara.")
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    clock.now += 5.0
    db.add_message(uid, "chat-now", "user", "and who is the CEO?")
    second = asyncio.run(memory_semantic.cross_chat_block(uid, "who runs the company?", "chat-now"))
    assert reads["n"] == 1, "the second turn re-read every candidate row"
    assert first == second and first


def test_an_invalidation_during_a_fetch_is_not_overwritten_by_that_fetch(corpus, monkeypatch):
    """recall_cache_race.py as a test: the rename commits and its route
    invalidates WHILE the first read is in flight."""
    uid, reads, _clock = corpus
    real = db.fetch_message_embeddings
    renamed = {"done": False}

    def fetch_during_rename(*a, **k):
        rows = real(*a, **k)
        if not renamed["done"]:
            renamed["done"] = True
            with db.connection() as con:
                con.execute("UPDATE conversations SET title = %s WHERE id = %s", ("Board of directors", "c1"))
            memory_semantic.invalidate_message_embeddings(uid)
        return rows

    monkeypatch.setattr(db, "fetch_message_embeddings", fetch_during_rename)
    first = _hits(uid)
    assert first and first[0]["title"] == "Leadership"  # read before the rename
    second = _hits(uid)
    assert second[0]["title"] == "Board of directors", "the pre-rename rows were cached after the invalidation"
