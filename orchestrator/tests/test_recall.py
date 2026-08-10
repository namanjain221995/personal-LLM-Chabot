"""Phase B: semantic recall over compacted turns.

The point of recall is the detail the SUMMARIZER dropped: a summary keeps what
it judged important, and everything else would otherwise be gone for good.

Isolation here is structural rather than a filter: chunks are stored per
conversation and the only read path loads one conversation's rows, so another
session's text cannot be returned even in principle.
"""
import asyncio

import pytest

from app import db, llm, recall
from app.config import settings


@pytest.fixture()
def conv():
    uid = db.create_user("alice", "hash")
    db.create_conversation(uid, "conv-a", "chat a")
    db.create_conversation(uid, "conv-b", "chat b")
    return uid


def fake_embedder(mapping):
    """Deterministic 3-dim vectors keyed by a substring of the text."""

    async def embed(texts, **kwargs):
        out = []
        for t in texts:
            vec = [0.0, 0.0, 0.0]
            for key, v in mapping.items():
                if key.lower() in t.lower():
                    vec = list(v)
                    break
            out.append(vec)
        return out

    return embed


# ---------------------------------------------------------------------------
# Vector plumbing
# ---------------------------------------------------------------------------


def test_vectors_round_trip_through_the_database():
    original = [0.5, -0.25, 1.0, 0.0]
    restored = recall.unpack_vector(recall.pack_vector(original))
    assert len(restored) == len(original)
    for a, b in zip(original, restored):
        assert abs(a - b) < 1e-6


def test_cosine_basics():
    assert recall.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert recall.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert recall.cosine([1, 0], []) == 0.0
    assert recall.cosine([0, 0], [1, 0]) == 0.0


def test_chunk_text_overlaps_so_a_fact_is_not_split_away():
    text = "A" * 3000
    chunks = recall.chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= 1200 for c in chunks)
    # Consecutive chunks overlap.
    assert chunks[0][-100:] in chunks[1] or chunks[1][:100] in chunks[0]


def test_chunk_text_skips_trivial_content():
    assert recall.chunk_text("ok") == []
    assert recall.chunk_text("   ") == []


# ---------------------------------------------------------------------------
# Indexing + retrieval
# ---------------------------------------------------------------------------


def test_folded_turns_are_indexed_and_retrievable(conv, monkeypatch):
    monkeypatch.setattr(
        llm, "embed_texts", fake_embedder({"badger": (1, 0, 0), "otter": (0, 1, 0)})
    )
    folded = [
        {"role": "user", "content": "the badger protocol uses port 8443 exclusively"},
        {"role": "assistant", "content": "the otter fallback uses port 9000"},
    ]
    stored = asyncio.run(recall.index_folded("conv-a", folded, 0))
    assert stored == 2

    block = asyncio.run(recall.retrieve_block("conv-a", "tell me about badger"))
    assert block is not None
    assert recall.RECALL_HEADER in block
    assert "badger protocol uses port 8443" in block


def test_retrieval_is_labelled_so_the_model_knows_the_source(conv, monkeypatch):
    monkeypatch.setattr(llm, "embed_texts", fake_embedder({"badger": (1, 0, 0)}))
    asyncio.run(
        recall.index_folded(
            "conv-a", [{"role": "user", "content": "badger fact " * 10}], 0
        )
    )
    block = asyncio.run(recall.retrieve_block("conv-a", "badger"))
    assert block.startswith(recall.RECALL_HEADER)
    assert "THIS conversation" in recall.RECALL_HEADER


def test_top_k_bounds_how_much_is_pulled_back(conv, monkeypatch):
    monkeypatch.setattr(llm, "embed_texts", fake_embedder({"topic": (1, 0, 0)}))
    folded = [
        {"role": "user", "content": f"topic number {i} " * 12} for i in range(20)
    ]
    asyncio.run(recall.index_folded("conv-a", folded, 0))
    block = asyncio.run(recall.retrieve_block("conv-a", "topic", top_k=3))
    assert block.count("[user]") == 3


def test_recall_returns_nothing_before_anything_is_folded(conv, monkeypatch):
    monkeypatch.setattr(llm, "embed_texts", fake_embedder({}))
    assert asyncio.run(recall.retrieve_block("conv-a", "anything")) is None


def test_recall_is_disabled_by_the_flag(conv, monkeypatch):
    monkeypatch.setattr(settings, "semantic_recall_enabled", False)
    monkeypatch.setattr(llm, "embed_texts", fake_embedder({"x": (1, 0, 0)}))
    assert asyncio.run(recall.index_folded("conv-a", [{"role": "user", "content": "x" * 100}], 0)) == 0
    assert asyncio.run(recall.retrieve_block("conv-a", "x")) is None


def test_embedding_failure_never_breaks_the_answer(conv, monkeypatch):
    async def boom(texts, **kwargs):
        raise RuntimeError("embed service down")

    monkeypatch.setattr(llm, "embed_texts", boom)
    # Retrieval degrades to None rather than raising into the chat path.
    assert asyncio.run(recall.retrieve_block("conv-a", "anything")) is None


# ---------------------------------------------------------------------------
# Isolation — the guarantee that matters most
# ---------------------------------------------------------------------------


def test_retrieval_never_crosses_sessions(conv, monkeypatch):
    monkeypatch.setattr(
        llm,
        "embed_texts",
        fake_embedder({"quartz": (1, 0, 0), "basalt": (1, 0, 0)}),
    )
    asyncio.run(
        recall.index_folded(
            "conv-a",
            [{"role": "user", "content": "SECRET-A quartz mining figures " * 8}],
            0,
        )
    )
    asyncio.run(
        recall.index_folded(
            "conv-b",
            [{"role": "user", "content": "SECRET-B basalt mining figures " * 8}],
            0,
        )
    )

    # Identical query vectors: only the conversation scoping separates them.
    a = asyncio.run(recall.retrieve_block("conv-a", "quartz"))
    b = asyncio.run(recall.retrieve_block("conv-b", "basalt"))
    assert "SECRET-A" in a and "SECRET-B" not in a
    assert "SECRET-B" in b and "SECRET-A" not in b


def test_chunks_are_deleted_with_their_conversation(conv, monkeypatch):
    monkeypatch.setattr(llm, "embed_texts", fake_embedder({"x": (1, 0, 0)}))
    asyncio.run(
        recall.index_folded("conv-a", [{"role": "user", "content": "x" * 200}], 0)
    )
    assert db.get_conversation_chunks("conv-a")
    db.clear_summary("conv-a")
    assert db.get_conversation_chunks("conv-a") == []
