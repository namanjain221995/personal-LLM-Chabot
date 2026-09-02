"""The living knowledge layer — the production failure, pinned.

THE BUG (owner report, 2026-08-31). With web search ON, the platform found and
answered "who's vice president of india" correctly. In a NEW conversation with
search OFF and effort Fast, it answered from pretrained weights instead — while
19 pages already stored on the same machine said otherwise. The evidence was
stored, indexed and retrievable the whole time; `web_index.retrieve` was simply
never called outside the search engine, and nothing could have preferred the
current page over the obsolete one if it had been.

Fixture pages only, never the live internet: what is under test is the ranking
and the routing, which must be deterministic.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import db, web_index, web_memory
from app.config import settings
from app.freshness import Freshness, classify
from app.living_knowledge import prepare, today_iso
from app.web_memory import grounding_block, retrieve, staleness_note

OLD_PERSON = "Jagdeep Dhankhar"
NEW_PERSON = "C. P. Radhakrishnan"
QUESTION = "who's vice president of india"


def run(coro):
    """No pytest-asyncio in this suite — the repo drives coroutines directly."""
    return asyncio.run(coro)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _page(key, url, title, text, *, age_days, authority):
    when = _now() - timedelta(days=age_days)
    with db.connection() as con:
        con.execute(
            """INSERT INTO web_pages
                 (url_key, url, title, text, fetched_at, first_seen_at,
                  last_changed_at, domain, authority, indexed_at, content_hash)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (url_key) DO UPDATE
                 SET text = EXCLUDED.text,
                     fetched_at = EXCLUDED.fetched_at,
                     authority = EXCLUDED.authority""",
            (key, url, title, text, when, when, when,
             url.split("/")[2], authority, when, key),
        )


@pytest.fixture(autouse=True)
def _clean_corpus():
    with db.connection() as con:
        con.execute("TRUNCATE web_pages RESTART IDENTITY CASCADE")
    yield


@pytest.fixture()
def no_dense(monkeypatch):
    """Force the LEXICAL half of hybrid retrieval.

    LanceDB and the embedding service are absent here, and that is the point:
    the layer must still answer when vectors are cold — which is exactly the
    state a page is in the moment it is fetched.
    """

    async def _none(*_a, **_k):
        return []

    monkeypatch.setattr(web_index, "retrieve", _none)


def _seed_current(age_days: float = 2.0) -> None:
    _page(
        "vp/official",
        "https://vicepresidentofindia.nic.in/",
        "Vice President of India",
        f"{NEW_PERSON} is the Vice President of India, having assumed office in 2025.",
        age_days=age_days,
        authority=web_memory.AUTHORITY_OFFICIAL,
    )


def _seed_obsolete(age_days: float = 400.0) -> None:
    _page(
        "vp/blog",
        "https://examhub.example/list-of-vice-presidents",
        "Vice Presidents of India",
        f"{OLD_PERSON} is the Vice President of India. He assumed office in 2022.",
        age_days=age_days,
        authority=web_memory.AUTHORITY_LOW,
    )


# ── Scenario 1: the exact production failure ────────────────────────────────


def test_a_new_conversation_in_fast_mode_uses_the_stored_fresh_answer(no_dense):
    """The whole bug in one test: Fast, search OFF, brand-new conversation."""
    _seed_obsolete()
    _seed_current()

    prepared = run(
        prepare(
            QUESTION,
            effort="fast",
            mode="assistant",
            web_search_pref="off",
            allow_network=False,  # search is OFF: nothing may touch the network
        )
    )

    assert prepared.verdict is not None
    assert prepared.verdict.requirement is Freshness.RECENT
    assert prepared.grounding, "Fast mode must be grounded, not left to the weights"
    assert NEW_PERSON in prepared.grounding
    assert OLD_PERSON not in prepared.grounding, "the obsolete page must not be sent"
    assert not prepared.searched, "fresh local evidence must not trigger a lookup"
    assert any("vicepresidentofindia" in s["url"] for s in prepared.sources)
    assert f"Current date: {today_iso()}" in prepared.grounding


def test_the_obsolete_page_is_dropped_not_merely_outranked(no_dense):
    """Sending both names and hoping the model picks right is not a fix."""
    _seed_obsolete()
    _seed_current()
    verdict = run(classify(QUESTION, now_year=_now().year, allow_router=False))
    result = run(retrieve(QUESTION, level=verdict.requirement, top_k=5))

    kept = " ".join(e.text for e in result.evidence)
    dropped = " ".join(e.text for e in result.superseded)
    assert NEW_PERSON in kept
    assert OLD_PERSON not in kept
    assert OLD_PERSON in dropped


def test_a_timeless_question_costs_nothing(no_dense, monkeypatch):
    """STATIC questions must not pay for retrieval at all — with topical
    grounding OFF. (Since 2026-09-03 the default is ON: an indexed site is
    supposed to answer timeless questions about itself; that path is pinned
    by the two tests below, and this one keeps the zero-cost path honest.)"""
    monkeypatch.setattr(settings, "living_knowledge_topical", False)
    _seed_current()
    prepared = run(
        prepare(
            "What is photosynthesis?",
            effort="fast",
            mode="assistant",
            web_search_pref="off",
            allow_network=True,
        )
    )
    assert prepared.verdict.requirement is Freshness.STATIC
    assert prepared.grounding == ""
    assert prepared.retrieval is None, "no lookup should have happened"


def test_a_timeless_question_is_grounded_only_on_a_strong_local_match(monkeypatch):
    """Topical grounding: a page the platform read that closely matches a
    timeless question grounds the answer — that is what makes an indexed
    site a knowledge base. A weak match grounds nothing: ordinary chat must
    never drag in a loosely related page.

    The dense half is stubbed to a fixed near-hit for BOTH questions, so the
    difference between them is the lexical signal alone — the words of the
    question being on the page."""
    page_url = "https://docs.example.org/guide/photosynthesis-simulator-configuration"

    async def dense_hit(query, top_k=6, site_prefix=""):
        return [{
            "url": page_url,
            "title": "Photosynthesis simulator configuration",
            "text": "To configure the photosynthesis simulator set PHOTO_RATE in config.yaml.",
            "fetched_at": "",
            "score": 0.2,  # LanceDB distance: a close vector match
        }]

    monkeypatch.setattr(web_index, "retrieve", dense_hit)
    _page(
        "docs/config",
        page_url,
        "Photosynthesis simulator configuration",
        "To configure the photosynthesis simulator set PHOTO_RATE in config.yaml. "
        "The photosynthesis simulator reads config.yaml at startup.",
        age_days=30.0,
        authority=web_memory.AUTHORITY_REFERENCE,
    )
    strong = run(
        prepare(
            "explain how the photosynthesis simulator is configured",
            effort="fast",
            mode="assistant",
            web_search_pref="off",
            allow_network=False,
        )
    )
    assert strong.verdict.requirement is Freshness.STATIC
    assert strong.retrieval is not None, "the local corpus is consulted"
    assert "config.yaml" in strong.grounding
    assert strong.sources and "docs.example.org" in strong.sources[0]["url"]
    assert not strong.searched

    weak = run(
        prepare(
            "what is the boiling point of water?",
            effort="fast",
            mode="assistant",
            web_search_pref="off",
            allow_network=False,
        )
    )
    assert weak.grounding == "", "a loose match must not ground a timeless answer"


# ── Scenario 2: cached evidence has gone stale ──────────────────────────────


def test_stale_cache_is_not_treated_as_sufficient(no_dense):
    _seed_current(age_days=365)
    verdict = run(classify(QUESTION, now_year=_now().year, allow_router=False))
    result = run(retrieve(QUESTION, level=verdict.requirement, top_k=5))

    assert result.found, "it is still the best evidence available"
    assert not result.sufficient(verdict.max_age_seconds), "but not fresh enough"


def test_a_stale_cache_triggers_the_lightweight_lookup(monkeypatch, no_dense):
    """Fast mode may spend a LITTLE network when the corpus cannot answer."""
    _seed_current(age_days=365)
    calls = {}

    async def fake_fetch(question, *, max_queries=1, max_sources=2):
        calls["question"] = question
        calls["max_sources"] = max_sources
        _seed_current(age_days=0)  # the refreshed page lands in the store
        return 1

    import app.engines.search as search_mod

    monkeypatch.setattr(search_mod, "fetch_for_freshness", fake_fetch, raising=False)

    prepared = run(
        prepare(
            QUESTION,
            effort="fast",
            mode="assistant",
            web_search_pref="auto",
            allow_network=True,
        )
    )
    assert calls, "the lookup should have run"
    assert calls["max_sources"] <= 3, "the Fast fallback must stay small"
    assert prepared.searched
    assert NEW_PERSON in prepared.grounding


def test_think_mode_does_not_double_search(monkeypatch, no_dense):
    """think/max already get the full search engine; two lookups is one too many."""
    _seed_current(age_days=365)
    called = []

    async def fake_fetch(*_a, **_k):
        called.append(1)
        return 1

    import app.engines.search as search_mod

    monkeypatch.setattr(search_mod, "fetch_for_freshness", fake_fetch, raising=False)
    run(
        prepare(
            QUESTION,
            effort="think",
            mode="assistant",
            web_search_pref="auto",
            allow_network=True,
        )
    )
    assert not called, "the small lookup is a FAST-mode affordance only"


# ── Scenario 3: offline, with only stale evidence ───────────────────────────


def test_offline_answers_from_cache_but_says_how_old_it_is(no_dense):
    """Never present a cached fact as definitely current."""
    _seed_current(age_days=300)
    prepared = run(
        prepare(
            QUESTION,
            effort="fast",
            mode="assistant",
            web_search_pref="auto",
            allow_network=False,  # no internet
        )
    )
    assert prepared.grounding, "the cached page is still the best answer available"
    assert NEW_PERSON in prepared.grounding
    lowered = prepared.grounding.lower()
    assert "could not be refreshed" in lowered
    assert "may have changed" in lowered


def test_the_staleness_note_names_a_real_date(no_dense):
    _seed_current(age_days=120)
    verdict = run(classify(QUESTION, now_year=_now().year, allow_router=False))
    result = run(retrieve(QUESTION, level=verdict.requirement, top_k=5))
    note = staleness_note(result, verdict.max_age_seconds)
    assert (_now() - timedelta(days=120)).date().isoformat() in note
    assert "120 days ago" in note


# ── Scenario 4: the public corpus carries no private data ───────────────────


def test_public_evidence_is_shared_but_carries_no_user_data(no_dense):
    """User A's search warms the corpus for User B — page text only.

    web_pages is deliberately global: that is exactly why one person's search
    makes the next person's answer current. What must never cross is who
    asked, from which conversation, or anything they uploaded — and the table
    has no column through which it could.
    """
    _seed_current()
    verdict = run(classify(QUESTION, now_year=_now().year, allow_router=False))
    result = run(retrieve(QUESTION, level=verdict.requirement, top_k=5))
    assert result.found

    with db.connection() as con:
        cols = {
            r["column_name"]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'web_pages'"
            ).fetchall()
        }
    assert "user_id" not in cols
    assert "conversation_id" not in cols

    block = grounding_block(result, today_iso())
    for leak in ("user_id", "conversation_id", "session_id", "upload"):
        assert leak not in block.lower()


# ── Ranking behaviour ───────────────────────────────────────────────────────


def test_authority_breaks_ties_between_equally_fresh_sources(no_dense):
    """A content farm and a government site are not equal evidence."""
    _page(
        "tie/farm",
        "https://affairscloud.example/vp-india",
        "VP of India",
        f"{OLD_PERSON} is the Vice President of India.",
        age_days=1,
        authority=web_memory.AUTHORITY_LOW,
    )
    _page(
        "tie/gov",
        "https://vicepresidentofindia.nic.in/",
        "Vice President of India",
        f"{NEW_PERSON} is the Vice President of India.",
        age_days=1,
        authority=web_memory.AUTHORITY_OFFICIAL,
    )
    result = run(retrieve(QUESTION, level=Freshness.RECENT, top_k=5))
    assert NEW_PERSON in result.evidence[0].text


def test_lexical_matching_separates_entities_dense_search_confuses(no_dense):
    """VP of India and VP of the United States are near-identical vectors."""
    _page(
        "ent/us",
        "https://whitehouse.gov/vp",
        "Vice President of the United States",
        "The Vice President of the United States presides over the Senate.",
        age_days=1,
        authority=web_memory.AUTHORITY_OFFICIAL,
    )
    _page(
        "ent/in",
        "https://vicepresidentofindia.nic.in/",
        "Vice President of India",
        f"{NEW_PERSON} is the Vice President of India.",
        age_days=1,
        authority=web_memory.AUTHORITY_OFFICIAL,
    )
    result = run(retrieve(QUESTION, level=Freshness.RECENT, top_k=5))
    assert "india" in result.evidence[0].url.lower()


def test_genuine_disagreement_is_flagged_rather_than_resolved(no_dense):
    """Two fresh, authoritative, contradicting sources: say so, do not guess."""
    _page(
        "conflict/a",
        "https://en.wikipedia.org/wiki/Vice_President_of_India",
        "Vice President of India",
        f"{NEW_PERSON} is the Vice President of India.",
        age_days=1,
        authority=web_memory.AUTHORITY_REFERENCE,
    )
    _page(
        "conflict/b",
        "https://reuters.com/india-vp",
        "India names Vice President",
        "A different candidate is the Vice President of India, reports say.",
        age_days=2,
        authority=web_memory.AUTHORITY_REFERENCE,
    )
    result = run(retrieve(QUESTION, level=Freshness.RECENT, top_k=5))
    assert result.conflict
    assert "disagree" in grounding_block(result, today_iso()).lower()


def test_a_single_weak_passage_is_not_enough_to_override_the_model(no_dense):
    """One thin match must not be treated as established fact."""
    _page(
        "weak/1",
        "https://random.example/page",
        "Assorted notes",
        "India is a country in South Asia with a parliamentary system.",
        age_days=1,
        authority=web_memory.AUTHORITY_NEUTRAL,
    )
    verdict = run(classify(QUESTION, now_year=_now().year, allow_router=False))
    result = run(retrieve(QUESTION, level=verdict.requirement, top_k=5))
    assert not result.sufficient(verdict.max_age_seconds)


def test_the_evidence_block_stays_small_enough_for_fast_mode():
    """Grounding must not eat the context window (or the latency budget)."""
    from app.web_memory import Evidence, Retrieval

    big = Retrieval(query=QUESTION, freshness=Freshness.RECENT)
    big.evidence = [
        Evidence(
            url=f"https://example.com/{i}",
            title="T" * 80,
            text="x" * 40_000,
            domain="example.com",
            authority=50,
            fetched_at=_now(),
        )
        for i in range(6)
    ]
    block = grounding_block(big, today_iso())
    # 240k characters of evidence go in; a budgeted block (settings
    # LIVING_KNOWLEDGE_EVIDENCE_CHARS of passages plus the framing lines)
    # comes out. 900 chars used to be the whole budget — one paragraph —
    # which was not "a large amount of information from the site" (owner,
    # 2026-09-03); the ceiling is now a setting, and this pins that it holds.
    ceiling = settings.living_knowledge_evidence_chars + 1500
    assert len(block) <= ceiling, f"grounding block was {len(block)} chars (> {ceiling})"
    assert len(block) < 40_000, "one page must never be pasted whole"


def test_agreeing_pages_from_one_site_are_not_a_conflict(no_dense):
    """Corroboration is the opposite of conflict.

    REGRESSION (2026-09-01, found on the live corpus): five pages of
    vicepresidentofindia.nic.in all naming the same person tripped the
    conflict flag, and the model was told its sources disagreed. Hedging on a
    unanimous official answer is precisely the timidity this layer removes.
    """
    for i in range(4):
        _page(
            f"same/{i}",
            f"https://vicepresidentofindia.nic.in/page{i}",
            "Vice President of India",
            f"{NEW_PERSON} is the Vice President of India.",
            age_days=1 + i * 0.2,
            authority=web_memory.AUTHORITY_OFFICIAL,
        )
    result = run(retrieve(QUESTION, level=Freshness.RECENT, top_k=5))
    assert len(result.evidence) >= 2
    assert not result.conflict, "same-domain agreement is corroboration"
    assert "disagree" not in grounding_block(result, today_iso()).lower()
