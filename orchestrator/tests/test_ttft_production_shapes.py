"""The production-sized TTFT measurement shape, and the proof it is real.

WHY THIS FILE EXISTS. The Fast-mode time-to-first-token work needed a seeded
account that costs what a production account costs before the engine is even
asked: `context_assembly_seconds` reads 167 ms on real traffic where the small
seeded shapes read 13-23 ms. This file is the definition of that account —
shape 7, 'heavy': saved facts at the MEMORY_MAX_FACTS ceiling (including
non-ASCII ones that reach the rendered facts block), ten stored documents, a
thread far longer than the compaction window and already compacted, and eight
earlier conversations whose messages are embedded so cross-chat recall has a
corpus to search.

THIS FILE STANDS ALONE. The measuring harness that consumed the builder lived
in a session scratch directory that has since been cleared, and nothing in the
repository imports this module. So the shape is not "the harness's fixture
kept honest by a test" any more — the pytest below IS the artefact, and it
passes with nothing but PostgreSQL: no GPU, no engine, no network (the
embedding sidecar is stubbed). Shape 6 'knowledge', which an earlier version of
this docstring described alongside shape 7, never had a builder in this file
and does not have one now; it is not part of this artefact.

WHY IT IS A TEST AND NOT A FIXTURE FILE. A shape that quietly stopped building
what it claims — because a writer changed, or a setting moved, or the app's own
compactor was read wrongly — would make every number measured against it wrong,
and wrong in the flattering direction, since every one of these costs is paid
per turn. So the shape is asserted against the row counts the app's own writers
actually produced, and, where the shape has to reproduce an app decision, it
CALLS the app rather than restating its arithmetic.

THE FOLD BOUNDARY IS THE APP'S, NOT OURS. `folded_turns` delegates to
`compaction.fold_boundary`. It used to compute `len(turns) // 2 - keep_recent`,
reading KEEP_RECENT_TURNS as a number of EXCHANGES where the app reads it as a
number of LIST ENTRIES, and the tests here pinned that wrong number: on this
shape the app folds 232 of 240 messages and the shape folded 224, so every
measurement carried eight extra verbatim messages (+1,760 chars) that
production would not have sent.

ONLY THE APP'S PUBLIC WRITERS. `build_heavy_account` calls
`db.create_conversation`, `db.add_user_fact`, `db.save_document`,
`db.add_message`, `db.save_summary`, `recall.index_folded` and
`memory_semantic.ensure_message_embeddings` — and nothing else. It issues no
SQL of its own, which `test_the_builder_writes_only_through_public_writers`
proves by walking its AST, not by searching its text: the text guard this
replaced could be walked straight past with `getattr(db, "conn" + "ection")`
and a lower-case `update`, which is why the guard now has a test of its own.
A shape assembled by direct INSERT would measure rows production could never
have written.

SYNTHETIC DATA ONLY. Every fact, document, message and conversation below is
invented: an imaginary maintenance rulebook for an imaginary site. No
production chat content, no real person, no real address.

KNOWN LIMIT, NOT FIXED. `build_heavy_account` is idempotent by check-then-
create around `db.create_conversation`, which is not atomic: two builders
running concurrently on one account can still both see the conversation
missing and race into the `conversations_pkey` UniqueViolation the builder's
docstring describes. Every caller so far is sequential, so this has never
fired; closing it would need a writer the app does not expose.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import inspect

import pytest

from app import compaction, db, facts, llm, memory_semantic, recall
from app.config import settings

# ---------------------------------------------------------------------------
# shape 7 — the heavy account
# ---------------------------------------------------------------------------

#: Ten stored documents, as an account that has worked through a project has.
HEAVY_DOCUMENTS = 10
#: 120 exchanges = 240 list entries. KEEP_RECENT_TURNS is read by
#: `compaction.fold_boundary` as list ENTRIES, so at 8 the app folds the first
#: 232 entries and leaves a live tail of 8 — four exchanges, not eight.
HEAVY_TURNS = 120
#: Earlier conversations, for cross-chat recall and keyword recall to search.
HEAVY_PRIOR_CONVERSATIONS = 8
#: Six exchanges in each of them.
PRIOR_TURNS = 6

#: The deployed values this shape is meant to reproduce, read from the deploy
#: root on 2026-09-27: `.env` sets KEEP_RECENT_TURNS=8 and sets MEMORY_MAX_FACTS
#: nowhere, and `.runtime/generated.env` sets neither, so the config defaults
#: (8 and 200) ARE production's numbers today. This is a recorded observation,
#: not an assertion: the suite cannot read the deployed environment. If
#: production ever sets MEMORY_MAX_FACTS, every test here would still pass while
#: the shape silently stopped matching production — so this note is what tells
#: the next reader to re-check it.
PRODUCTION_KEEP_RECENT_TURNS_AS_OBSERVED = 8
PRODUCTION_MEMORY_MAX_FACTS_AS_OBSERVED = 200

#: Non-ASCII saved facts. A saved-facts size cap proposed in this programme was
#: refused because it measured bytes and deleted exactly these for accounts
#: whose facts are not Latin-1; the shape carries them so the next attempt meets
#: them immediately.
#:
#: THE ORDER MATTERS TWICE. The two hardest probes come FIRST, so a smaller
#: MEMORY_MAX_FACTS truncates the easy ones and still tests the hard ones. And
#: these facts are written LAST by the builder — see `heavy_facts` — because
#: `db.list_user_facts` orders `updated_at DESC, id DESC` and
#: `facts.facts_block` stops at `_BLOCK_MAX_CHARS`, so the OLDEST facts are
#: precisely the ones that never reach the prompt. Written first, as they were,
#: all ten were dropped: the shape carried them in the database only.
NON_ASCII_FACTS = [
    "The user reads the 日本語 edition of the operations handbook, not the English one.",
    "The user prefers dates written as 2026年9月22日 in internal summaries.",
    "The user writes their name as Söderqvist and wants it spelled that way.",
    "The user's team in München reviews every release note before it ships.",
    "The user's second reviewer is Ángela and she reads Spanish drafts first.",
    "The user keeps the Ærø site's checklists in Danish and never translates them.",
    "The user signs off internal notes with «ок» when a draft is approved.",
    "The user's glossary renders the unit as μs, never as us or microseconds.",
    "The user asks for Ελληνικά headings to stay untranslated in the appendix.",
    "The user's archive path contains ünïcödé characters and must not be escaped.",
]

#: The two shapes that broke the refused saved-facts cap: a three-byte
#: character, and a date mixing ASCII digits with three-byte ideographs. They
#: must lead NON_ASCII_FACTS so no ceiling can leave them out.
HARDEST_NON_ASCII_PROBES = ("日本語", "2026年9月22日")

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

#: The provenance every fact of this shape carries. `app/identity.py` reads
#: `db.trusted_user_facts`, which filters on this value in SQL, so a shape that
#: lost it would change what the identity path sees with every test still green.
HEAVY_FACT_SOURCE = "stated"


def heavy_facts(ceiling: int) -> list:
    """`ceiling` distinct saved facts, IN THE ORDER THE SHAPE WRITES THEM.

    The non-ASCII facts come LAST, and that position is load-bearing rather
    than cosmetic: `db.add_user_fact` stamps `updated_at` per call,
    `db.list_user_facts` orders `updated_at DESC, id DESC`, and
    `facts.facts_block` stops adding lines at `_BLOCK_MAX_CHARS`. Last written
    is therefore first rendered. Written first — as the shape originally wrote
    them — all ten fell outside the 6,000-character block and the prompt the
    harness measured contained no non-ASCII fact at all.
    """
    if ceiling <= len(NON_ASCII_FACTS):
        return list(NON_ASCII_FACTS[:ceiling])
    out = []
    index = 0
    while len(out) < ceiling - len(NON_ASCII_FACTS):
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
    out.extend(NON_ASCII_FACTS)
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


def folded_turns(turns: list, keep_recent: int | None = None) -> list:
    """The part of `turns` a compacted thread has already folded away.

    This is the APP'S boundary, asked of the app: `compaction.fold_boundary`
    counts LIST ENTRIES and keeps `keep` of them, so on a 240-entry thread with
    KEEP_RECENT_TURNS=8 it folds 232 and leaves 8 verbatim. Do not reimplement
    that sum here. The version this replaced computed
    `turns[: max(0, len(turns) // 2 - keep_recent) * 2]`, which read the setting
    as exchanges, folded 224, and left the measurement carrying eight messages
    production would never have sent.

    `keep_recent` is passed straight through, so None means KEEP_RECENT_TURNS.
    """
    return list(turns[: compaction.fold_boundary(len(turns), 0, keep_recent)])


async def build_heavy_account(
    user_id: int, conversation_id: str, *, prior_conversation_ids: list
) -> dict:
    """Shape 7's stored state, written with the app's own public writers only.

    Returns what it wrote, so a caller can assert the shape instead of
    trusting it.

    IDEMPOTENT ON THE ACCOUNT, FRESH ON THE CONVERSATION. Every measured run
    needs its OWN conversation — five runs must be five identical first turns,
    not one conversation that grows under the measurement — so a caller calls
    this once per run, on one account.

    `create_conversation` RAISES on a duplicate id, so the earlier
    conversations must be created only when they are missing: the second run
    of a pass died on exactly that and left the account with no cross-chat
    history to measure. `add_user_fact` dedupes itself
    (ON CONFLICT (user_id, lower(fact)) DO NOTHING), so reading the stored
    facts first only saves 200 round trips; it is not what keeps the count
    right. It also means a second run does NOT restamp `updated_at`, so the
    facts keep the order the first run wrote them in — which is what puts the
    non-ASCII facts inside the rendered facts block. `add_message` does NOT
    dedupe, which is why the earlier conversations' turns are written only into
    an empty conversation.

    The check-then-create is not atomic; see this module's docstring.
    """
    ceiling = int(settings.memory_max_facts)
    stored = {row["fact"] for row in db.list_user_facts(user_id, ceiling * 2)}
    for fact in heavy_facts(ceiling):
        if fact not in stored:
            db.add_user_fact(
                user_id,
                fact,
                None,
                source=HEAVY_FACT_SOURCE,
                source_excerpt=fact,
            )

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
# the public-writer guard
# ---------------------------------------------------------------------------

#: The writers the shape is made of.
_SHAPE_WRITERS = frozenset(
    {
        "db.add_user_fact",
        "db.create_conversation",
        "db.add_message",
        "db.save_document",
        "db.save_summary",
        "recall.index_folded",
        "memory_semantic.ensure_message_embeddings",
    }
)
#: The readers that make the builder idempotent.
_SHAPE_READERS = frozenset(
    {
        "db.list_user_facts",
        "db.list_messages",
        "db.conversation_owner",
        "db.get_conversation_chunks",
    }
)
#: This module's own pure helpers, and the only builtins the builder needs.
_SHAPE_HELPERS = frozenset(
    {"heavy_facts", "heavy_document_text", "heavy_turns", "prior_turns", "folded_turns"}
)
_SAFE_BUILTINS = frozenset({"int", "len", "range", "enumerate", "str"})

#: Statement keywords that may not appear in any string the builder builds.
#: Matched case-insensitively, because "UPDATE " but not "update " is how the
#: text guard this replaced was walked past.
_FORBIDDEN_SQL = (
    "insert",
    "update",
    "delete",
    "truncate",
    "alter",
    "drop ",
    "copy ",
    "grant ",
)


def _dotted_name(node: ast.AST) -> str:
    """`db.add_user_fact` for an Attribute chain, else a name that is not one."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted_name(node.value)}.{node.attr}"
    # A call through a subscript, a call result or a lambda: deliberately
    # un-allowlistable, so it always fails.
    return f"<computed {type(node).__name__}>"


def assert_only_public_shape_writers(source: str) -> None:
    """Fail unless `source` reaches the database only through named writers.

    This walks the AST rather than searching the text. The text guard it
    replaced checked a regex over `db|recall|memory_semantic` attributes and a
    case-sensitive list of forbidden substrings, and `8 passed` with this
    inserted into the builder:

        _open = getattr(db, "conn" + "ection")
        with _open() as _h:
            getattr(_h, "exec" + "ute")(
                "update user_facts set source = null ...", (user_id,)
            )

    Every call here must name something in the allowlist, so `getattr` fails as
    a call, an imported alias fails as an unknown callee, and a computed callee
    fails as `<computed ...>`. `test_the_public_writer_guard_rejects_evasions`
    holds this function to that.
    """
    tree = ast.parse(source)
    allowed = _SHAPE_WRITERS | _SHAPE_READERS | _SHAPE_HELPERS | _SAFE_BUILTINS
    # Docstrings are prose, not SQL, and they are skipped BY NODE IDENTITY.
    # Comparing values instead would not work: `ast.get_docstring` cleans the
    # indentation, so no indented docstring would ever match, and the builder's
    # own docstring would then trip the keyword check on "updated_at".
    docstring_nodes = set()
    for holder in ast.walk(tree):
        body = getattr(holder, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(
            holder,
            (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        head = body[0]
        if (
            isinstance(head, ast.Expr)
            and isinstance(head.value, ast.Constant)
            and isinstance(head.value.value, str)
        ):
            docstring_nodes.add(id(head.value))

    called = set()
    attributes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise AssertionError(
                "the builder imports inside its own body, which can reach any "
                "module the allowlist does not name"
            )
        if isinstance(node, ast.Call):
            called.add(_dotted_name(node.func))
        if isinstance(node, ast.Attribute):
            root = _dotted_name(node)
            if root.split(".")[0] in ("db", "recall", "memory_semantic"):
                attributes.add(root)

    assert called <= allowed, (
        f"not a public shape writer: {sorted(called - allowed)}"
    )
    assert attributes <= (_SHAPE_WRITERS | _SHAPE_READERS), (
        "an app attribute is touched that is not an allowed writer or reader: "
        f"{sorted(attributes - (_SHAPE_WRITERS | _SHAPE_READERS))}"
    )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstring_nodes:
            continue
        lowered = node.value.lower()
        for forbidden in _FORBIDDEN_SQL:
            assert forbidden not in lowered, (
                f"the builder builds its own SQL: {forbidden!r} in {node.value!r}"
            )


# ---------------------------------------------------------------------------
# the shapes, asserted
# ---------------------------------------------------------------------------


def _vector_seed(text: str, position: int) -> int:
    """The stub embedder's seed: a real digest, not `hash()`.

    `hash(str)` is salted per process unless PYTHONHASHSEED is fixed, so a stub
    documented as deterministic produced different vectors on every run. No
    assertion depended on the values, which is exactly what made it a trap for
    whoever wrote the first assertion over a recalled ordering.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return ((digest[0] << 8) | digest[1]) % 977 + position


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
            seed = _vector_seed(text, position)
            out.append([float((seed + i) % 13) / 13.0 for i in range(dimension)])
        return out

    return _embed


@pytest.fixture()
def stub_embeddings(monkeypatch):
    monkeypatch.setattr(llm, "embed_texts", _fake_embedder())
    monkeypatch.setattr(llm, "embed_query", lambda *a, **k: _fake_embedder()([a[0]]))


def test_the_stub_embedder_is_deterministic_across_processes():
    """A pinned digest, so a salted-hash stub can never come back.

    These numbers are the SHA-256 of the two texts, not a recorded run: a
    process with a different PYTHONHASHSEED must reproduce them exactly.
    """
    assert _vector_seed("the fixed probe text", 0) == 205
    assert _vector_seed("the fixed probe text", 3) == 208
    assert _vector_seed("The user reads the 日本語 edition.", 0) == 914
    first = asyncio.run(_fake_embedder()(["a", "b"]))
    second = asyncio.run(_fake_embedder()(["a", "b"]))
    assert first == second
    assert first[0] != first[1], "the stub must separate distinct texts"


@pytest.mark.parametrize("ceiling", [1, 2, 9, 10, 11, 200])
def test_the_facts_reach_the_ceiling_and_keep_their_non_ascii(ceiling):
    facts_list = heavy_facts(ceiling)
    assert len(facts_list) == ceiling
    assert len(set(facts_list)) == ceiling, (
        "a duplicate fact is silently dropped on write"
    )
    expected_non_ascii = NON_ASCII_FACTS[: min(ceiling, len(NON_ASCII_FACTS))]
    non_ascii = [f for f in facts_list if any(ord(c) > 127 for c in f)]
    assert non_ascii == expected_non_ascii, (
        "the non-ASCII facts are not the ones the shape names, in order"
    )
    # They must be the LAST facts written, or list_user_facts' updated_at DESC
    # ordering puts them outside facts_block's character cap.
    assert facts_list[-len(expected_non_ascii):] == expected_non_ascii, (
        "the non-ASCII facts must be written last; written first they are the "
        "oldest rows and facts_block drops exactly them"
    )
    # The hard probes lead NON_ASCII_FACTS, so no ceiling can leave them out.
    for probe in HARDEST_NON_ASCII_PROBES[:ceiling]:
        assert any(probe in f for f in facts_list), (
            f"{probe!r} must survive a ceiling of {ceiling}"
        )
    if ceiling >= 2:
        assert any(len(f.encode("utf-8")) > len(f) + 4 for f in facts_list)


def test_the_hardest_probes_lead_the_non_ascii_list():
    """So a reorder cannot quietly make the small-ceiling cases vacuous."""
    for position, probe in enumerate(HARDEST_NON_ASCII_PROBES):
        assert probe in NON_ASCII_FACTS[position], (
            f"{probe!r} must be entry {position} of NON_ASCII_FACTS"
        )
    assert len(set(NON_ASCII_FACTS)) == len(NON_ASCII_FACTS)
    assert all(any(ord(c) > 127 for c in f) for f in NON_ASCII_FACTS)


def test_the_fold_boundary_is_the_apps_own_and_not_a_restatement():
    """`folded_turns` must agree with `compaction.fold_boundary` exactly.

    The shape used to compute `len(turns) // 2 - keep_recent` exchanges, which
    folds 224 of 240 entries where the app folds 232 — so every measurement
    carried eight extra verbatim messages. Several turn counts and several
    `keep` values, because agreeing at one point is how the old arithmetic
    looked right.
    """
    for exchanges in (1, 2, 4, 5, 8, 9, 20, HEAVY_TURNS):
        for keep in (1, 2, 3, 8, 16, 240, 1000):
            turns = heavy_turns(exchanges)
            expected = compaction.fold_boundary(len(turns), 0, keep)
            folded = folded_turns(turns, keep)
            assert len(folded) == expected, (
                f"{exchanges} exchanges, keep={keep}: shape folds "
                f"{len(folded)} entries, the app folds {expected}"
            )
            assert folded == turns[:expected]
            # What is NOT folded is exactly what compaction.assemble replays.
            replayed = [
                m
                for m in compaction.assemble(turns, HEAVY_SUMMARY, expected)
                if m.get("role") != "system"
            ]
            assert replayed == turns[expected:]
    # And with keep_recent omitted it is the deployed setting, not a default
    # baked in here.
    turns = heavy_turns()
    assert folded_turns(turns) == folded_turns(turns, int(settings.keep_recent_turns))


def test_the_thread_is_longer_than_the_compaction_window_and_is_folded():
    turns = heavy_turns()
    assert len(turns) == HEAVY_TURNS * 2
    keep = int(settings.keep_recent_turns)
    assert len(turns) > keep, "the thread must outrun the compaction window"
    folded = folded_turns(turns, keep)
    assert len(folded) == len(turns) - keep
    assert folded == turns[: len(folded)]
    # The live tail is `keep` LIST ENTRIES — not `keep` exchanges. At the
    # deployed KEEP_RECENT_TURNS=8 that is four exchanges.
    assert len(turns) - len(folded) == keep


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

    A direct INSERT would let a caller measure rows no writer could make — a
    fact with no source, a message with no embedding row queued — so the
    builder is held to the named public writers and to no SQL of its own.
    """
    assert_only_public_shape_writers(inspect.getsource(build_heavy_account))


def test_the_public_writer_guard_rejects_evasions():
    """The guard has to be tested, because the guard it replaced was not.

    Each source below passed the old text guard while writing raw SQL. The
    first is verbatim what I inserted into the builder to get `8 passed` on the
    unfixed branch.
    """
    evasions = {
        "getattr indirection with a lower-case statement": '''
async def build(user_id):
    _open = getattr(db, "conn" + "ection")
    with _open() as _h:
        getattr(_h, "exec" + "ute")(
            "update user_facts set source = null, source_excerpt = null"
            " where user_id = %s", (user_id,)
        )
''',
        "an attribute bound before it is called": '''
async def build(user_id):
    writer = db.connection
    with writer() as handle:
        handle.execute("delete from user_facts where user_id = %s", (user_id,))
''',
        "a driver imported inside the body": '''
async def build(user_id):
    import psycopg
    with psycopg.connect("") as con:
        con.execute("truncate user_facts")
''',
        "a callee computed from a mapping": '''
async def build(user_id):
    {"w": db.add_user_fact}["w"](user_id, "x", None)
''',
        "an unlisted writer of the right shape": '''
async def build(user_id):
    db.delete_user_facts(user_id)
''',
        "lower-case SQL in a string the builder assembles": '''
async def build(user_id):
    db.add_user_fact(user_id, "alter table user_facts drop column source", None)
''',
    }
    for label, source in evasions.items():
        try:
            assert_only_public_shape_writers(source)
        except AssertionError:
            continue
        raise AssertionError(f"the guard accepted a raw-SQL evasion: {label}")
    # And it still accepts the real thing, so it is not merely strict.
    assert_only_public_shape_writers(inspect.getsource(build_heavy_account))


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

    # Provenance, which the track spec names and no assertion used to pin:
    # app/identity.py reads db.trusted_user_facts, whose filter is in the SQL.
    assert {f["source"] for f in stored_facts} == {HEAVY_FACT_SOURCE}
    assert all(f["source_excerpt"] for f in stored_facts)
    assert all(f["trusted"] for f in stored_facts)
    trusted = db.trusted_user_facts(user_id, ceiling * 2)
    assert len(trusted) == ceiling, (
        "every fact of this shape must reach the identity path, which filters "
        f"on source in {db.TRUSTED_FACT_SOURCES}"
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
    # The app's boundary, asked of the app — not `(HEAVY_TURNS - keep) * 2`,
    # which is what this assertion pinned while the app folded eight more.
    assert summary["covers_through"] == compaction.fold_boundary(
        HEAVY_TURNS * 2, 0, int(settings.keep_recent_turns)
    )

    chunks = db.get_conversation_chunks(conversation_id)
    assert claimed["chunks"] >= claimed["folded_turns"], (
        "every folded turn must reach the recall index, or in-chat recall "
        "measures a thread shorter than the one the shape claims"
    )
    assert len(chunks) == claimed["chunks"]

    for prior_id in priors:
        assert len(db.list_messages(prior_id)) == PRIOR_TURNS * 2


def test_the_non_ascii_facts_reach_the_rendered_prompt(as_user, stub_embeddings):
    """Stored is not the same as sent, and only sent is measurable.

    On the unfixed shape this failed 10/10: the facts were written first, so
    `db.list_user_facts` (updated_at DESC) put them last and
    `facts.facts_block` stopped at `_BLOCK_MAX_CHARS` after 56 of 200 rows.
    The prompt the harness measured, and the prompt a byte-counting saved-facts
    cap would have been tested against, contained no non-ASCII fact at all.
    """
    user = as_user("ttft_heavy_block")
    user_id = int(user["id"])
    priors = [f"c-ttft-block-prior-{i}" for i in range(HEAVY_PRIOR_CONVERSATIONS)]
    asyncio.run(
        build_heavy_account(user_id, "c-ttft-block", prior_conversation_ids=priors)
    )

    ceiling = int(settings.memory_max_facts)
    stored = db.list_user_facts(user_id, ceiling)
    block = facts.facts_block(stored)
    assert block is not None
    missing = [f for f in NON_ASCII_FACTS if f not in block]
    assert missing == [], (
        f"{len(missing)} of {len(NON_ASCII_FACTS)} non-ASCII facts never reach "
        f"the prompt: facts_block renders {len(block.splitlines()) - 1} of "
        f"{len(stored)} rows within {facts._BLOCK_MAX_CHARS} characters, "
        "oldest first out"
    )
    # The block is genuinely capped — otherwise this test would pass for the
    # uninteresting reason that everything fits.
    assert len(block.splitlines()) - 1 < len(stored), (
        "the cap no longer bites on this shape, so it no longer proves the "
        "ordering; raise MEMORY_MAX_FACTS in the shape or re-read the cap"
    )
    # Provenance survives into the identity path's view of the same rows. The
    # order is REVERSED write order: `trusted_user_facts` sorts
    # `updated_at DESC, id DESC`, and the builder writes NON_ASCII_FACTS in list
    # order, so the last one written is the first one returned. Asserting the
    # exact reversed list — rather than a set — is deliberate: it pins that
    # these ten are the newest rows in the store, which is the whole reason they
    # survive `facts_block`'s cap.
    identity_facts = db.trusted_user_facts(user_id, len(NON_ASCII_FACTS))
    assert [f["fact"] for f in identity_facts] == list(reversed(NON_ASCII_FACTS)), (
        "the newest trusted facts must be the non-ASCII probes, newest first"
    )


def test_a_second_run_reuses_the_account_and_builds_its_own_conversation(
    as_user, stub_embeddings
):
    """A caller calls the builder once per measured run, on ONE account.

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
    # The second run must not restamp the facts: if it did, the non-ASCII rows
    # would stop being the newest and would leave the rendered block again.
    # Newest first, so reversed write order — see the rendered-prompt test.
    newest_trusted = db.trusted_user_facts(user_id, len(NON_ASCII_FACTS))
    assert [f["fact"] for f in newest_trusted] == list(reversed(NON_ASCII_FACTS))
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
