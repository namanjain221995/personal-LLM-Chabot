"""Scope invariants (ADR-0001 D7, §5) — cross-user, proven at the store.

Two real users, every private store seeded for one of them, every read
attempted as the other. The filters live INSIDE the queries (a `user_id`
or `conversation_id` predicate, an ownership check before a per-conversation
read), so these tests exercise the accessors the engines call, not a
post-filter that could be forgotten.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app import db, knowledge, web_memory
from app.config import settings
from app.freshness import Freshness
from app.recall import pack_vector
from app.web_memory import Evidence, Retrieval


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def two_users(as_user):
    alice = as_user("scope_alice")
    bob = as_user("scope_bob")
    return int(alice["id"]), int(bob["id"])


def _conversation(user_id: int, cid: str) -> str:
    try:
        db.create_conversation(user_id, cid, "seed")
    except db.IntegrityError:
        pass
    return cid


def test_user_scope_facts_and_recall_never_cross(two_users):
    alice, bob = two_users
    db.add_user_fact(alice, "alice prefers metric units")
    cid = _conversation(alice, "scope-conv-a1")
    msg = db.add_message(alice, cid, "user", "our office moved to Frisco")
    assert msg is not None
    db.store_message_embeddings(
        alice, settings.embed_model, 2,
        [{"message_id": int(msg["id"]), "conversation_id": cid, "embedding": pack_vector([0.5, 0.5])}],
    )
    assert db.add_message(bob, cid, "user", "sneak") is None  # not bob's conversation
    assert [f["fact"] for f in db.list_user_facts(alice, 10)] == ["alice prefers metric units"]
    assert db.list_user_facts(bob, 10) == []
    assert db.fetch_message_embeddings(bob, settings.embed_model, None, 100) == []
    assert db.recall_conversations(bob, ["frisco"], None, 3) == []


def test_conversation_scope_documents_and_urls_are_owner_only(two_users):
    alice, bob = two_users
    cid = _conversation(alice, "scope-conv-a2")
    db.save_document(cid, "plan.pdf", "confidential plan text", 1)
    db.save_url_document(cid, "https://intranet.example/page", "Intranet", "internal page text")
    # The accessors are keyed by conversation id; the OWNER check is what
    # every caller performs first (main.py /chat, uploads._own). Both halves
    # are asserted: the owner resolves to alice, and a request as bob is
    # refused before any read.
    assert db.conversation_owner(cid) == alice
    assert db.conversation_owner(cid) != bob
    from app import uploads

    with pytest.raises(HTTPException) as refused:
        run(uploads._own(cid, {"id": bob}))
    assert refused.value.status_code == 404
    run(uploads._own(cid, {"id": alice}))  # the owner passes


def test_an_upload_claims_an_unowned_conversation_for_the_uploader(two_users):
    """Pre-seeding closed: whoever uploads first OWNS the id, so the next
    /chat sender cannot inherit their files."""
    alice, bob = two_users
    from app import uploads

    run(uploads._own("scope-fresh-id", {"id": alice}))
    assert db.conversation_owner("scope-fresh-id") == alice
    with pytest.raises(HTTPException):
        run(uploads._own("scope-fresh-id", {"id": bob}))
    with pytest.raises(HTTPException) as bad:
        run(uploads._own("not a valid id!", {"id": alice}))
    assert bad.value.status_code == 422


def test_public_evidence_is_shared_and_carries_no_user_data(two_users):
    """The web corpus is one store for everyone — by design — and nothing in
    a row identifies who asked. The provenance columns record who
    INTRODUCED a page (for purge and trust), never who read it."""
    with db.connection() as con:
        cols = {
            r["column_name"]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'web_pages'"
            ).fetchall()
        }
    assert "user_id" not in cols and "conversation_id" not in cols
    assert {"origin", "introduced_by_user_id", "quarantined_at"} <= cols


def test_the_public_pipeline_refuses_non_public_evidence(monkeypatch):
    async def fake(question, *, level, top_k, **kw):
        r = Retrieval(query=question, freshness=level)
        r.evidence = [Evidence(url="u", title="t", text="x", domain="d", authority=1, fetched_at=None, scope="user")]
        return r

    monkeypatch.setattr(web_memory, "retrieve", fake)
    with pytest.raises(RuntimeError):
        run(knowledge.retrieve("q", level=Freshness.RECENT, top_k=5))
    assert knowledge.cacheable("public") and not knowledge.cacheable("user")
