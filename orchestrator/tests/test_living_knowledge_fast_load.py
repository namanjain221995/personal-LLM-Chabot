"""Fast topical grounding under CONCURRENT load, at the shipped defaults
(second prover pass, 2026-09-13).

The prover's conc_eval at a 0.72 s retrieval with 24 lexical pages of 200,000
chars: HEAD grounded 24/24 at c=8 and 48/48 at c=16; the Fast budget of the
first fix grounded 8/24 and 15/48, every loss `topical_hit_budget` (the 1.5 s
wall-clock bound, measured from turn start, fired while turns queued for CPU
and connections) or `topical_deadline` (the 0.3 s bound fired before the
pre-check itself had answered). The offline eval ran one turn at a time and
could not see it. Both bounds are now off by default: a Fast turn skips the
retrieval only on a proven miss.

These tests do NOT pin KNOWLEDGE_FAST_TOPICAL_DEADLINE_S or
KNOWLEDGE_FAST_TOPICAL_HIT_BUDGET_S: they prove what the shipped defaults do.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import time

import pytest

from app import db, web_index, web_memory
from app import living_knowledge as lk
from app.config import settings

DOCS_URL = "https://docs.example.org/guide/photosynthesis-simulator-configuration"
DOCS_TITLE = "Photosynthesis simulator configuration"
DOCS_TEXT = (
    "To configure the photosynthesis simulator set PHOTO_RATE in config.yaml. "
    "The photosynthesis simulator reads config.yaml at startup."
)
STRONG = "explain how the photosynthesis simulator is configured"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with db.connection() as con:
        con.execute("TRUNCATE web_pages RESTART IDENTITY CASCADE")
    web_memory.cache_clear()
    lk._page_vocabulary.reset()
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 0.0)
    monkeypatch.setattr(settings, "knowledge_rerank", False)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "living_knowledge_topical", True)
    monkeypatch.setattr(settings, "freshness_router_enabled", True)
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    yield
    lk._page_vocabulary.reset()


def _seed(monkeypatch, *, dense_delay: float, distractors: int = 8, distractor_chars: int = 200_000):
    rnd = random.Random(5)
    words = "photosynthesis simulator configured reasoning leaderboard".split()
    filler = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima".split()
    for k in range(distractors):
        parts, size = [], 0
        while size < distractor_chars:
            w = rnd.choice(words) if rnd.random() < 0.02 else rnd.choice(filler)
            parts.append(w)
            size += len(w) + 1
        text = " ".join(parts)
        url = f"https://distract{k}.test/archive"
        db.upsert_web_page(url, url, url, f"Archive {k}", text, "text/html", 200, hashlib.sha256(text.encode()).hexdigest())
    db.upsert_web_page(DOCS_URL, DOCS_URL, DOCS_URL, DOCS_TITLE, DOCS_TEXT, "text/html", 200,
                       hashlib.sha256(DOCS_TEXT.encode()).hexdigest())

    async def dense_hit(query, top_k=6, site_prefix="", **_):
        await asyncio.sleep(dense_delay)
        return [{"url": DOCS_URL, "title": DOCS_TITLE, "text": DOCS_TEXT, "fetched_at": "", "score": 0.2}]

    monkeypatch.setattr(web_index, "retrieve", dense_hit)


def _fast(question):
    return lk.prepare(question, effort="fast", mode="assistant", web_search_pref="off", allow_network=False)


def test_the_shipped_defaults_put_no_wall_clock_bound_on_fast_topical_grounding():
    assert lk.fast_topical_deadline_s() <= 0
    assert lk.fast_topical_hit_budget_s() <= 0
    assert lk.fast_topical_precheck() is True


def test_eight_concurrent_fast_turns_keep_every_hit_the_precheck_clears(monkeypatch):
    """Retrieval slower than the old 1.5 s hit budget, as it is under load, and
    eight turns at once over 200,000-char lexical pages."""
    _seed(monkeypatch, dense_delay=1.6)
    assert lk._topical_precheck(STRONG) is True  # the vocabulary is warm

    async def burst():
        return await asyncio.gather(*(_fast(STRONG) for _ in range(8)))

    results = asyncio.run(burst())
    lost = [(p.decision, p.degraded) for p in results if p.decision != "static_topical"]
    assert lost == [], lost
    assert all("config.yaml" in p.grounding for p in results)


def test_a_precheck_that_answers_after_300_ms_under_load_still_keeps_the_hit(monkeypatch):
    """The prover's c=8 at 0.14 s: 6 of 8 losses were `topical_deadline`, the
    pre-check had not answered within 0.3 s. It is waited for now."""
    _seed(monkeypatch, dense_delay=0.5, distractors=2)
    real = lk._topical_precheck

    def slow(question):
        time.sleep(0.45)
        return real(question)

    monkeypatch.setattr(lk, "_topical_precheck", slow)

    async def burst():
        return await asyncio.gather(*(_fast(STRONG) for _ in range(8)))

    results = asyncio.run(burst())
    assert [p.degraded for p in results if p.degraded] == []
    assert all(p.decision == "static_topical" for p in results)


def test_a_proven_miss_under_the_same_load_still_skips_the_slow_retrieval(monkeypatch):
    _seed(monkeypatch, dense_delay=3.0, distractors=2)
    assert lk._topical_precheck("what is the boiling point of water at altitude") is False

    async def burst():
        started = time.perf_counter()
        out = await asyncio.gather(*(_fast("what is the boiling point of water at altitude") for _ in range(8)))
        return out, time.perf_counter() - started

    results, elapsed = asyncio.run(burst())
    assert all(p.decision == "static_model" and p.degraded == "" for p in results)
    assert elapsed < 2.0, elapsed
