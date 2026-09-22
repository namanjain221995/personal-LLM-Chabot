"""The production-sized TTFT measurement shapes, and the proof they are real.

WHY THIS FILE EXISTS. The Fast-mode time-to-first-token harness measures a
real POST /chat against seeded accounts. Its first four shapes are honest but
small, and they do not reproduce what a production account costs before the
engine is even asked: `context_assembly_seconds` reads 167 ms on real traffic
where those shapes read 13-23 ms. Two heavier shapes close that gap:

  shape 6 'knowledge'  an ordinary account, but measured against a real
                       indexed corpus of public technical documentation, so
                       `knowledge_stage_seconds{lexical,dense_scan,embed}`
                       has something to scan. The corpus is built by crawling
                       through the platform's own crawl path; nothing here
                       writes a page or a vector directly.

  shape 7 'heavy'      the account that makes context assembly read like
                       production: saved facts at the MEMORY_MAX_FACTS
                       ceiling (including non-ASCII ones), ten stored
                       documents, a thread far longer than the compaction
                       window and already compacted, and eight earlier
                       conversations whose messages are embedded so cross-chat
                       recall has a corpus to search.

THE SHAPES LIVE HERE, NOT IN THE HARNESS. The builders below are the single
definition; the harness imports them. That is the whole point of the file: a
shape that quietly stopped building what it claims — because a writer changed,
or a setting moved — would make every number measured against it wrong, and
wrong in the flattering direction, since every one of these costs is paid per
turn. So the shapes are asserted against the row counts the app's own writers
actually produced.

ONLY THE APP'S PUBLIC WRITERS. `build_heavy_account` calls
`db.create_conversation`, `db.add_user_fact`, `db.save_document`,
`db.add_message`, `db.save_summary`, `recall.index_folded` and
`memory_semantic.ensure_message_embeddings` — and nothing else. It issues no
SQL of its own, which `test_the_builder_writes_only_through_public_writers`
proves by reading its source. A shape assembled by direct INSERT would measure
rows production could never have written.

SYNTHETIC DATA ONLY. Every fact, document, message and conversation below is
invented: an imaginary maintenance rulebook for an imaginary site. No
production chat content, no real person, no real address.
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from app import db, llm, memory_semantic, recall
from app.config import settings

# ---------------------------------------------------------------------------
# shape 7 — the heavy account
# ---------------------------------------------------------------------------

#: Ten stored documents, as an account that has worked through a project has.
HEAVY_DOCUMENTS = 10
#: 120 exchanges. KEEP_RECENT_TURNS is 8, so 112 of them are already folded.
HEAVY_TURNS = 120
#: Earlier conversations, for cross-chat recall and keyword recall to search.
HEAVY_PRIOR_CONVERSATIONS = 8
#: Six exchanges in each of them.
PRIOR_TURNS = 6

#: Non-ASCII saved facts, first in the list so no shape can be built without
#: them. A saved-facts size cap proposed in this programme was refused because
#: it measured bytes and deleted exactly these for accounts whose facts are not
#: Latin-1; the shape carries them so the next attempt meets them immediately.
NON_ASCII_FACTS = [
    "The user writes their name as Söderqvist and wants it spelled that way.",
    "The user's team in München reviews every release note before it ships.",
    "The user prefers dates written as 2026年9月22日 in internal summaries.",
    "The user's second reviewer is Ángela and she reads Spanish drafts first.",
    "The user keeps the Ærø site's checklists in Danish and never translates them.",
    "The user signs off internal notes with «ок» when a draft is approved.",
    "The user's glossary renders the unit as μs, never as us or microseconds.",
    "The user asks for Ελληνικά headings to stay untranslated in the appendix.",
    "The user's archive path contains ünïcödé characters and must not be escaped.",
    "The user reads the 日本語 edition of the operations handbook, not the English one.",
]

_FACT_TEMPLATES = (
    "The user prefers {thing} in every {place}.",
    "The user's {role} asks for {thing} before a change reaches {place}.",
    "The user keeps {thing} out of {place} unless a reviewer asks for it.",
    "The user reviews {thing} on the first working day of the month in {place}.",
    "The user records {thing} against the {role} who approved it in {place}.",
)
_THINGS = (
    "the rollback note", "the unit column", "the owner field", "the change window",
    "the dependency list", "the test evidence", "the approval trail",
    "the capacity forecast", "the retention setting", "the escalation contact",
    "the data classification", "the maintenance interval", "the spare part code",
    "the calibration record", "the acceptance threshold", "the sampling rate",
)
_PLACES = (
    "the weekly report", "the release checklist", "the handover document",
    "the incident review", "the service catalogue", "the vendor summary",
    "the audit pack", "the runbook",
)
_ROLES = ("reviewer", "duty engineer", "service owner", "quality lead")

_DOCUMENT_TOPICS = (
    "pump overhaul intervals", "chiller refrigerant handling",
    "compressor valve wear limits", "isolation valve leak testing",
    "vibration trending and alarm bands", "lubricant sampling and analysis",
    "electrical insulation resistance testing", "pressure relief device certification",
    "spare parts holding and reorder points", "commissioning and baseline records",
)
DOCUMENT_FILENAMES = (
    "pump-overhaul-intervals.pdf", "chiller-refrigerant-handling.pdf",
    "compressor-valve-wear-limits.pdf", "isolation-valve-leak-testing.pdf",
    "vibration-trending-and-alarm-bands.pdf", "lubricant-sampling-and-analysis.pdf",
    "insulation-resistance-testing.pdf", "relief-device-certification.pdf",
    "spare-parts-holding.pdf", "commissioning-baseline-records.pdf",
)

_SENTENCES = (
    "Each asset carries a service class that fixes its inspection interval.",
    "A deferred inspection is recorded with the reason and the new due date.",
    "The duty engineer countersigns every form before it leaves the site.",
    "A replacement part is booked against the asset, not against the work order.",
    "The annual reliability review reads the whole year of forms at once.",
    "A calibration outside tolerance opens a nonconformance the same day.",
    "Spare stock below the reorder point raises a purchase request automatically.",
    "The commissioning record is the baseline every later measurement is read against.",
    "A vibration reading is logged with the probe position and the load at the time.",
    "An asset moved between sites keeps its history and gets a new location code.",
    "The maintenance window is agreed with operations a fortnight ahead.",
    "Any change to an interval needs the service owner's written approval.",
)

_ASSET_KINDS = ("pump", "chiller", "compressor", "valve", "gearbox", "fan")

_PRIOR_TOPICS = (
    "reorder points for imported spares", "the calibration certificate archive",
    "the handover checklist after a night shift", "vibration alarm bands on the fan deck",
    "who signs a deferred inspection", "the lubricant sampling schedule",
    "the commissioning baseline for the new chiller", "the audit pack's evidence list",
)

#: What a rolling summary of the folded part of the thread says.
HEAVY_SUMMARY = (
    "Earlier in this conversation the user worked through the Ashfield "
    "maintenance rulebook turn by turn: inspection intervals by service class "
    "for the pump, chiller, compressor, valve, gearbox and fan, which AM-series "
    "form records each inspection, who countersigns it, how a deferred "
    "inspection is recorded, and how every reading reaches the annual "
    "reliability review alongside the previous two years."
)


def heavy_facts(ceiling: int) -> list:
    """`ceiling` distinct saved facts, the first ten of them non-ASCII."""
    out = list(NON_ASCII_FACTS[:ceiling])
    index = 0
    while len(out) < ceiling:
        template = _FACT_TEMPLATES[index % len(_FACT_TEMPLATES)]
        out.append(
            template.format(
                thing=_THINGS[index % len(_THINGS)],
                place=_PLACES[(index // len(_THINGS)) % len(_PLACES)],
                role=_ROLES[index % len(_ROLES)],
            )
            + f" (rule {index + 1})"
        )
        index += 1
    return out


def heavy_document_text(index: int, target_chars: int = 40000) -> str:
    """Deterministic English prose of a realistic extracted-PDF length."""
    topic = _DOCUMENT_TOPICS[index % len(_DOCUMENT_TOPICS)]
    out = [f"Section 1. Overview of {topic}.\n"]
    paragraph = 0
    section = 1
    while sum(len(piece) for piece in out) < target_chars:
        paragraph += 1
        if paragraph % 12 == 0:
            section += 1
            out.append(f"\nSection {section}. Further notes on {topic}.\n")
        out.append(
            f"{_SENTENCES[paragraph % len(_SENTENCES)]} "
            f"({topic}, paragraph {paragraph})\n"
        )
    return "".join(out)


def heavy_turns(count: int = HEAVY_TURNS) -> list:
    """`count` exchanges of a plausible long working conversation."""
    turns = []
    for i in range(1, count + 1):
        kind = _ASSET_KINDS[i % len(_ASSET_KINDS)]
        turns.append(
            {
                "role": "user",
                "content": (
                    f"Turn {i}: in the Ashfield maintenance rulebook, what "
                    f"interval applies to the {kind} in service class "
                    f"{i % 7 + 1}, which inspection form records it, and who "
                    f"has to countersign the result before it is filed?"
                ),
            }
        )
        turns.append(
            {
                "role": "assistant",
                "content": (
                    f"Answer {i}: service class {i % 7 + 1} puts the {kind} on "
                    f"a {i % 3 + 2}-month interval. The inspection is recorded "
                    f"on form AM-{100 + i}, countersigned by the duty engineer, "
                    f"and the reading is carried into the annual reliability "
                    f"review together with the previous two years."
                ),
            }
        )
    return turns


def prior_turns(index: int) -> list:
    """One earlier conversation of the account: six exchanges on its own topic."""
    topic = _PRIOR_TOPICS[index % len(_PRIOR_TOPICS)]
    turns = []
    for i in range(1, PRIOR_TURNS + 1):
        turns.append(
            {
                "role": "user",
                "content": (
                    f"About {topic}: question {i} — what did we settle on, and why?"
                ),
            }
        )
        turns.append(
            {
                "role": "assistant",
                "content": (
                    f"On {topic} we settled point {i}: the service owner "
                    f"decides, the duty engineer records it on the AM form, and "
                    f"the result is read at the annual review."
                ),
            }
        )
    return turns


def folded_turns(turns: list, keep_recent: int) -> list:
    """The part of `turns` a compacted thread has already folded away."""
    return turns[: max(0, len(turns) // 2 - keep_recent) * 2]


async def build_heavy_account(
    user_id: int, conversation_id: str, *, prior_conversation_ids: list
) -> dict:
    """Shape 7's stored state, written with the app's own public writers only.

    Returns what it wrote, so a caller can assert the shape instead of
    trusting it.

    IDEMPOTENT ON THE ACCOUNT, FRESH ON THE CONVERSATION. Every measured run
    needs its OWN conversation — five runs must be five identical first turns,
    not one conversation that grows under the measurement — so the harness
    calls this once per run, on one account.

    `create_conversation` RAISES on a duplicate id, so the earlier
    conversations must be created only when they are missing: the second run
    of a pass died on exactly that and left the account with no cross-chat
    history to measure. `add_user_fact` dedupes itself
    (ON CONFLICT (user_id, lower(fact)) DO NOTHING), so reading the stored
    facts first only saves 200 round trips; it is not what keeps the count
    right. `add_message` does NOT dedupe, which is why the earlier
    conversations' turns are written only into an empty conversation.
    """
    ceiling = int(settings.memory_max_facts)
    stored = {row["fact"] for row in db.list_user_facts(user_id, ceiling * 2)}
    for fact in heavy_facts(ceiling):
        if fact not in stored:
            db.add_user_fact(user_id, fact, None, source="stated", source_excerpt=fact)

    for index, prior_id in enumerate(prior_conversation_ids):
        if db.conversation_owner(prior_id) is None:
            db.create_conversation(user_id, prior_id, f"earlier conversation {index}")
        if not db.list_messages(prior_id):
            for turn in prior_turns(index):
                db.add_message(user_id, prior_id, turn["role"], turn["content"])

    if db.conversation_owner(conversation_id) is None:
        db.create_conversation(user_id, conversation_id, "the measured conversation")
    for index, filename in enumerate(DOCUMENT_FILENAMES[:HEAVY_DOCUMENTS]):
        db.save_document(
            conversation_id, filename, heavy_document_text(index), total_pages=22
        )

    turns = heavy_turns(HEAVY_TURNS)
    if not db.list_messages(conversation_id):
        for turn in turns:
            db.add_message(user_id, conversation_id, turn["role"], turn["content"])

    folded = folded_turns(turns, int(settings.keep_recent_turns))
    db.save_summary(
        conversation_id, HEAVY_SUMMARY, covers_through=len(folded), token_estimate=5200
    )
    chunks = len(db.get_conversation_chunks(conversation_id))
    if not chunks:
        chunks = await recall.index_folded(conversation_id, folded, first_ordinal=0)

    embedded = 0
    for _ in range(200):  # the backfill is batched; run it to completion
        wrote = await memory_semantic.ensure_message_embeddings(user_id)
        embedded += wrote
        if wrote == 0:
            break

    return {
        "facts": ceiling,
        "documents": HEAVY_DOCUMENTS,
        "messages": len(turns),
        "folded_turns": len(folded),
        "chunks": chunks,
        "messages_embedded": embedded,
    }


# ---------------------------------------------------------------------------
# the shapes, asserted
# ---------------------------------------------------------------------------


def _fake_embedder(dimension: int = 8):
    """A deterministic stand-in for the embedding sidecar.

    The suite runs with no GPU and no network. What these tests assert is that
    every text the builder hands the embedding path comes back with a stored
    vector — which a fixed-dimension stub proves exactly as well as the real
    model, and without a model call.
    """

    async def _embed(texts, *_args, **_kwargs):
        out = []
        for position, text in enumerate(texts):
            seed = (hash(text) % 977) + position
            out.append([float((seed + i) % 13) / 13.0 for i in range(dimension)])
        return out

    return _embed


@pytest.fixture()
def stub_embeddings(monkeypatch):
    monkeypatch.setattr(llm, "embed_texts", _fake_embedder())
    monkeypatch.setattr(llm, "embed_query", lambda *a, **k: _fake_embedder()([a[0]]))


def test_the_facts_reach_the_ceiling_and_keep_their_non_ascii():
    ceiling = int(settings.memory_max_facts)
    facts = heavy_facts(ceiling)
    assert len(facts) == ceiling
    assert len(set(facts)) == ceiling, "a duplicate fact is silently dropped on write"
    non_ascii = [f for f in facts if any(ord(c) > 127 for c in f)]
    assert len(non_ascii) == min(len(NON_ASCII_FACTS), ceiling)
    # The specific shapes that broke the refused saved-facts cap: a character
    # outside Latin-1, and one whose UTF-8 encoding is three bytes wide.
    assert any("日本語" in f for f in facts)
    assert any(len(f.encode("utf-8")) > len(f) + 4 for f in facts)


def test_the_thread_is_longer_than_the_compaction_window_and_is_folded():
    turns = heavy_turns()
    assert len(turns) == HEAVY_TURNS * 2
    keep = int(settings.keep_recent_turns)
    folded = folded_turns(turns, keep)
    assert len(folded) == (HEAVY_TURNS - keep) * 2
    assert folded == turns[: len(folded)]
    # What is NOT folded is exactly the live tail the assembler still reads.
    assert len(turns) - len(folded) == keep * 2


def test_the_documents_are_a_realistic_extracted_pdf_length():
    assert len(DOCUMENT_FILENAMES) >= HEAVY_DOCUMENTS
    assert len(set(DOCUMENT_FILENAMES)) == len(DOCUMENT_FILENAMES), (
        "save_document upserts on (conversation_id, filename), so a repeated "
        "filename would store nine documents where the shape claims ten"
    )
    for index in range(HEAVY_DOCUMENTS):
        text = heavy_document_text(index)
        assert 40000 <= len(text) < 40400
        assert heavy_document_text(index) == text, "the builder is not deterministic"
    assert len({heavy_document_text(i)[:200] for i in range(HEAVY_DOCUMENTS)}) == (
        HEAVY_DOCUMENTS
    ), "ten copies of one document is not ten documents"


def test_the_earlier_conversations_are_eight_distinct_topics():
    assert len(_PRIOR_TOPICS) >= HEAVY_PRIOR_CONVERSATIONS
    openings = {prior_turns(i)[0]["content"] for i in range(HEAVY_PRIOR_CONVERSATIONS)}
    assert len(openings) == HEAVY_PRIOR_CONVERSATIONS
    for index in range(HEAVY_PRIOR_CONVERSATIONS):
        assert len(prior_turns(index)) == PRIOR_TURNS * 2


def test_the_builder_writes_only_through_public_writers():
    """The shape must be what production's own code would have produced.

    A direct INSERT would let the harness measure rows no writer could make
    — a fact with no source, a message with no embedding row queued — so the
    builder is held to the named public writers and to no SQL of its own.
    """
    source = inspect.getsource(build_heavy_account)
    allowed = {
        # the writers the shape is made of
        "db.add_user_fact",
        "db.create_conversation",
        "db.add_message",
        "db.save_document",
        "db.save_summary",
        "recall.index_folded",
        "memory_semantic.ensure_message_embeddings",
        # the readers that make it idempotent
        "db.list_user_facts",
        "db.list_messages",
        "db.conversation_owner",
        "db.get_conversation_chunks",
    }
    called = set(re.findall(r"\b(?:db|recall|memory_semantic)\.[a-z_]+", source))
    assert called <= allowed, f"not a public shape writer: {sorted(called - allowed)}"
    for forbidden in ("INSERT", "UPDATE ", "DELETE", "connection(", "execute("):
        assert forbidden not in source, f"the builder issues its own SQL: {forbidden}"


def test_the_heavy_account_writes_the_rows_it_claims(as_user, stub_embeddings):
    """End to end, against the app's real writers and a real database."""
    user = as_user("ttft_heavy_shape")
    user_id = int(user["id"])
    conversation_id = "c-ttft-heavy-shape"
    priors = [f"c-ttft-heavy-prior-{i}" for i in range(HEAVY_PRIOR_CONVERSATIONS)]

    claimed = asyncio.run(
        build_heavy_account(user_id, conversation_id, prior_conversation_ids=priors)
    )

    ceiling = int(settings.memory_max_facts)
    stored_facts = db.list_user_facts(user_id, ceiling * 2)
    assert len(stored_facts) == ceiling == claimed["facts"]
    assert [f for f in stored_facts if any(ord(c) > 127 for c in f["fact"])], (
        "the non-ASCII facts did not survive the round trip through the writer"
    )

    documents = db.get_documents(conversation_id)
    assert len(documents) == HEAVY_DOCUMENTS == claimed["documents"]
    assert all(len(d["text"]) >= 40000 for d in documents)
    assert len({d["filename"] for d in documents}) == HEAVY_DOCUMENTS

    messages = db.list_messages(conversation_id)
    assert len(messages) == HEAVY_TURNS * 2 == claimed["messages"]

    summary = db.get_summary(conversation_id)
    assert summary is not None
    assert summary["covers_through"] == claimed["folded_turns"]
    assert summary["covers_through"] == (
        HEAVY_TURNS - int(settings.keep_recent_turns)
    ) * 2

    chunks = db.get_conversation_chunks(conversation_id)
    assert claimed["chunks"] >= claimed["folded_turns"], (
        "every folded turn must reach the recall index, or in-chat recall "
        "measures a thread shorter than the one the shape claims"
    )
    assert len(chunks) == claimed["chunks"]

    for prior_id in priors:
        assert len(db.list_messages(prior_id)) == PRIOR_TURNS * 2


def test_a_second_run_reuses_the_account_and_builds_its_own_conversation(
    as_user, stub_embeddings
):
    """The harness calls the builder once per measured run, on ONE account.

    A second call must not double the facts, must not re-add the earlier
    conversations' messages, and must not fail on the conversation ids that
    already exist — and it must still build a fresh measured conversation, or
    run 2 would measure a thread that run 1 had already grown.
    """
    user = as_user("ttft_heavy_twice")
    user_id = int(user["id"])
    priors = [f"c-ttft-twice-prior-{i}" for i in range(HEAVY_PRIOR_CONVERSATIONS)]

    first = asyncio.run(
        build_heavy_account(user_id, "c-ttft-twice-0", prior_conversation_ids=priors)
    )
    second = asyncio.run(
        build_heavy_account(user_id, "c-ttft-twice-1", prior_conversation_ids=priors)
    )

    # Everything but the embedding backfill is the same shape twice.
    assert {k: v for k, v in second.items() if k != "messages_embedded"} == {
        k: v for k, v in first.items() if k != "messages_embedded"
    }
    # The backfill is the one number that MUST differ: the first call embeds
    # the earlier conversations as well as the measured one, the second only
    # the new measured conversation — the earlier ones are already embedded.
    assert first["messages_embedded"] == (
        HEAVY_TURNS * 2 + HEAVY_PRIOR_CONVERSATIONS * PRIOR_TURNS * 2
    )
    assert second["messages_embedded"] == HEAVY_TURNS * 2

    ceiling = int(settings.memory_max_facts)
    assert len(db.list_user_facts(user_id, ceiling * 3)) == ceiling
    for prior_id in priors:
        assert len(db.list_messages(prior_id)) == PRIOR_TURNS * 2
    for measured in ("c-ttft-twice-0", "c-ttft-twice-1"):
        assert len(db.list_messages(measured)) == HEAVY_TURNS * 2
        assert len(db.get_documents(measured)) == HEAVY_DOCUMENTS
        assert len(db.get_conversation_chunks(measured)) == first["chunks"]
    assert db.messages_missing_embeddings(user_id, settings.embed_model, 64) == []


def test_every_stored_message_of_the_heavy_account_is_embedded(
    as_user, stub_embeddings
):
    """Cross-chat recall searches embeddings, not rows.

    An account whose messages are stored but not embedded measures a cheap
    cross-chat read and a shape that does not exist in production, where the
    backfill has long since run.
    """
    user = as_user("ttft_heavy_embed")
    user_id = int(user["id"])
    priors = [f"c-ttft-embed-prior-{i}" for i in range(HEAVY_PRIOR_CONVERSATIONS)]

    asyncio.run(
        build_heavy_account(user_id, "c-ttft-embed", prior_conversation_ids=priors)
    )

    remaining = db.messages_missing_embeddings(
        user_id, settings.embed_model, 64
    )
    assert remaining == [], (
        f"{len(remaining)} stored message(s) have no embedding; cross-chat "
        "recall would search a smaller corpus than the shape claims"
    )
