"""V10 semantic cross-chat recall: embedding backfill, meaning-based search,
and the merged semantic+keyword block — against real SQL, with llm.embed_texts
monkeypatched to a deterministic fake (test_recall.py style)."""
import asyncio

import pytest

from app import db, llm, memory_semantic
from app.config import settings


def fake_embedder(mapping, default=(0.0, 0.0, 1.0)):
    """Deterministic 3-dim embeddings keyed by substring match."""

    async def embed(texts, **kwargs):
        out = []
        for text in texts:
            vec = list(default)
            for key, v in mapping.items():
                if key in (text or "").lower():
                    vec = list(v)
                    break
            out.append(vec)
        return out

    return embed


_MAPPING = {
    "ceo": (1.0, 0.0, 0.0),
    "company": (1.0, 0.0, 0.0),  # semantically near "ceo", shares no keyword
    "sourdough": (0.0, 1.0, 0.0),
}


@pytest.fixture()
def corpus(monkeypatch):
    """alice with a CEO answer + a cooking chat; bob with a CEO secret."""
    monkeypatch.setattr(llm, "embed_texts", fake_embedder(_MAPPING))
    uid = db.create_user("alice", "hash")
    db.create_conversation(uid, "c1", "Leadership")
    db.add_message(uid, "c1", "user", "Who is the CEO of TechSara please tell")
    db.add_message(
        uid, "c1", "assistant", "The CEO of TechSara is Sahil Patel."
    )
    db.create_conversation(uid, "c2", "Cooking")
    db.add_message(uid, "c2", "user", "How do I bake sourdough bread at home?")
    other = db.create_user("bob", "hash")
    db.create_conversation(other, "c9", "Bob private")
    db.add_message(other, "c9", "user", "Our CEO bonus is 999999 dollars")
    return uid


def _backfill(uid):
    return asyncio.run(memory_semantic.ensure_message_embeddings(uid))


def test_backfill_embeds_once_and_only_this_user(corpus):
    assert _backfill(corpus) == 3  # alice's 3 messages
    assert _backfill(corpus) == 0  # idempotent
    rows = db.fetch_message_embeddings(corpus, settings.embed_model, None)
    assert len(rows) == 3
    assert all("999999" not in r["content"] for r in rows)


def test_semantic_hits_find_meaning_not_keywords(corpus):
    _backfill(corpus)
    # "who runs the company" shares NO content word with the stored answer —
    # keyword recall returns nothing, semantic recall must find it.
    from app.memory_recall import keywords

    hits = asyncio.run(
        memory_semantic.semantic_hits(corpus, "who runs the company?", "new-conv")
    )
    assert hits, "semantic recall found nothing"
    assert "Sahil Patel" in hits[0]["snippet"]
    assert hits[0]["role"] == "assistant"
    kw_hits = db.recall_conversations(
        corpus, keywords("who runs the company?"), "new-conv", 3
    )
    assert all("Sahil Patel" not in (h["snippet"] or "") for h in kw_hits)


def test_semantic_hits_exclude_current_conversation_and_noise(corpus):
    _backfill(corpus)
    hits = asyncio.run(
        memory_semantic.semantic_hits(corpus, "who is the ceo?", "c1")
    )
    assert all(h["conversation_id"] != "c1" for h in hits)
    # the cooking chat is orthogonal to the query → below the score floor
    hits = asyncio.run(
        memory_semantic.semantic_hits(corpus, "who is the ceo?", "c2")
    )
    assert all("sourdough" not in h["snippet"].lower() for h in hits)


def test_semantic_hits_scoped_to_user(corpus):
    _backfill(corpus)
    bob_id = corpus + 1
    assert asyncio.run(memory_semantic.ensure_message_embeddings(bob_id)) == 1
    hits = asyncio.run(
        memory_semantic.semantic_hits(corpus, "who is the ceo?", "new-conv")
    )
    assert all("999999" not in h["snippet"] for h in hits)


def test_semantic_hits_drop_echo_of_the_query(corpus):
    _backfill(corpus)
    # c1 stores the literal question; asking it again from a new conversation
    # must NOT hand the model its own question back as a "memory".
    hits = asyncio.run(
        memory_semantic.semantic_hits(
            corpus, "Who is the CEO of TechSara please tell", "new-conv"
        )
    )
    assert all(h["snippet"] != "Who is the CEO of TechSara please tell" for h in hits)


def test_cross_chat_block_merges_and_attributes(corpus):
    _backfill(corpus)
    block = asyncio.run(
        memory_semantic.cross_chat_block(corpus, "who runs the company?", "new-conv")
    )
    assert block is not None
    assert "Sahil Patel" in block
    assert "(you answered)" in block


def test_recall_disabled_and_embedder_down_fail_soft(corpus, monkeypatch):
    _backfill(corpus)

    async def boom(texts, **kwargs):
        raise RuntimeError("embed service down")

    monkeypatch.setattr(llm, "embed_texts", boom)
    assert asyncio.run(memory_semantic.semantic_hits(corpus, "ceo?", None)) == []
    assert asyncio.run(memory_semantic.ensure_message_embeddings(corpus)) == 0

    monkeypatch.setattr(settings, "cross_chat_semantic_enabled", False)
    assert asyncio.run(memory_semantic.semantic_hits(corpus, "ceo?", None)) == []


def test_model_swap_reembeds_under_new_id(corpus, monkeypatch):
    """V7 regression: after an EMBED_MODEL swap the backfill must store fresh
    vectors under the new model id instead of silently conflicting forever."""
    _backfill(corpus)
    monkeypatch.setattr(settings, "embed_model", "Qwen/Some-New-Embedder")
    assert _backfill(corpus) == 3  # re-embedded, not wedged
    assert _backfill(corpus) == 0  # and only once
    rows = db.fetch_message_embeddings(corpus, "Qwen/Some-New-Embedder", None)
    assert len(rows) == 3


def test_store_reports_real_insert_count(corpus):
    _backfill(corpus)
    rows = db.fetch_message_embeddings(corpus, settings.embed_model, None)
    dup = [
        {
            "message_id": rows[0]["message_id"],
            "conversation_id": rows[0]["conversation_id"],
            "embedding": rows[0]["embedding"],
        }
    ]
    assert (
        db.store_message_embeddings(corpus, settings.embed_model, 3, dup) == 0
    )
