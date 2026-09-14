"""Context assembly, read concurrently, builds the SAME prompt (plan item 4).

2026-09-13: the dozen independent reads between MODE_RESOLVED and
CONTEXT_ASSEMBLED (facts, cross-chat recall, repo keys, stored pages,
documents, videos, uploads, in-conversation recall, the summary, artifacts)
used to be awaited one after another — p50 209 ms / p95 352 ms for 1-3
message histories. They now start together (main._ContextReads) while the
prompt blocks are still applied by the same sequential code.

The promise that makes that safe is byte-identity. The golden tests here drive
POST /chat for fixture conversations, capture the messages the engine was
handed, and compare their JSON bytes and the pinned meta keys (`meta.context`
— whose exact count runs behind the answer on the concurrent path,
compaction.prepare_deferred — plus `route`, `knowledge`, `sources`).

WHAT THEY COMPARE AGAINST (2026-09-13). The first version of this file ran
each scenario twice on the NEW code, CONTEXT_CONCURRENT_READS=false against
=true, and compared the two runs. That cannot see a change to the assembly
both paths share: the performance prover moved the cross-chat recall block
after history[0] in main.py and all 4 byte-identity tests still passed, while
a comparison against the pre-change tree's bytes failed. The reference is now
CHECKED IN: tests/fixtures/context_assembly_golden/ holds, per scenario, the
engine-bound messages JSON and the pinned meta captured from the PRE-CHANGE
orchestrator tree (git tree aefd714, commits 02b509f/82265d5, via
`git archive`), and BOTH flag states on the current tree must reproduce them
byte for byte. The living-knowledge scenarios pin the grounding block's
position (appended to the system prompt) and bytes, which the first version
never exercised (it ran with living_knowledge disabled); one of them serves
the topical hit at production's static-retrieval p50 (0.72 s), where the
first Fast budget (a flat 0.3 s deadline) dropped the grounding that the
pre-change tree waited for.

Proved by scratch mutations of a copy of the tree, 2026-09-13: the prover's
cross-chat reorder fails rich_fast and every living-knowledge scenario in both
flag states; grounding placed before the persona fails the living-knowledge
scenarios; a 1 us Fast topical deadline fails the Fast ones; a concurrent-only
dropped read fails only the `true` runs; a flag that never reaches the turn
fails every `true` run.

Recapturing is deliberate, never a way to make a failure go away: regenerate
only from a tree whose prompt is KNOWN right, with the recipe in
fixtures/context_assembly_golden/capture_from_baseline.py, and review the
fixture diff as the record of the prompt change.

Normalised, identically on the capture and the comparison, and nothing else:
  - today's date (UTC and local) anywhere in the messages -> "<TODAY>"; the
    grounding block opens with `Current date: <today>` (today_iso()).
  - meta.generation_id / trace_id / request_id are generated per turn and are
    not among the pinned meta keys.

Offline: the model stream, the router, the embedder, /tokenize and the web
memory retrieval are stubs; PostgreSQL is the suite's private test database.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import compaction, context, db, living_knowledge, llm, main, memory_semantic, recall
from app.config import settings
from app.engines import orchestrate
from app.engines import router as router_engine
from app.web_memory import Evidence, Retrieval


# ---------------------------------------------------------------------------
# Stubs shared by every scenario
# ---------------------------------------------------------------------------

#: Deterministic 3-dim vectors keyed by a substring, like test_recall's.
_VECTORS = {
    "badger": (1.0, 0.0, 0.0),
    "otter": (0.0, 1.0, 0.0),
    "orion": (0.7, 0.7, 0.0),
}


async def _fake_embed(texts, **_kwargs):
    out = []
    for text in texts:
        vec = [0.0, 0.0, 1.0]
        for key, value in _VECTORS.items():
            if key in (text or "").lower():
                vec = list(value)
                break
        out.append(vec)
    return out


async def _fake_count(base_url, model, messages):
    """A deterministic count far below every threshold, with a served window."""
    return sum(len(str(m.get("content") or "")) // 4 + 5 for m in messages) + 3, 1_000_000


async def _router_offline(*_args, **_kwargs):
    # The router hostnames do not resolve off the cluster, so every router
    # call in this file failed anyway; failing here makes that explicit and
    # guarantees no classification call leaves the process.
    raise ConnectionError("golden: the router is offline")


def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


@pytest.fixture()
def offline_turn(monkeypatch):
    """Everything around context assembly held still, so only it can differ."""
    captured: list = []

    async def fake_stream(messages, *, model_choice="smart", effort="medium", **_kwargs):
        captured.append(json.dumps(list(messages), ensure_ascii=False, sort_keys=False))
        yield ("token", "Noted.")

    async def no_store(_gen):
        # The answer is not persisted: a stored reply would become "another
        # conversation" that a later turn recalls.
        return None

    async def no_plan(message, history, effort):
        return orchestrate.Plan(agent=False, search=False)

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(llm, "embed_texts", _fake_embed)
    monkeypatch.setattr(llm, "router_chat_completion", _router_offline)
    monkeypatch.setattr(context, "count_tokens", _fake_count)
    monkeypatch.setattr(main, "_store_answer", no_store)
    monkeypatch.setattr(orchestrate, "decide", no_plan)
    monkeypatch.setattr(settings, "living_knowledge_enabled", False)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_before_answering", False)
    # The served window is known, as in any process past its first turn —
    # which is what lets the concurrent path defer the meter's count.
    base_url, _key, _model = llm.resolve_model_choice("smart")
    monkeypatch.setitem(context._window_cache, base_url, 1_000_000)
    llm.embed_cache_clear()
    # Process-wide recall caches (2026-09-13, memory_semantic's 60 s candidate
    # cache) outlive the per-test TRUNCATE; absent on the pre-change tree.
    invalidate = getattr(memory_semantic, "invalidate_message_embeddings", None)
    if invalidate is not None:
        invalidate()
    return captured


def _local_user() -> int:
    from tests.conftest import _materialize_test_user

    return int(_materialize_test_user("local")["id"])


def _rich_fixture() -> int:
    """A conversation with something in every block the prompt can carry."""
    uid = _local_user()
    db.create_conversation(uid, "golden-rich", "rich chat")
    db.create_conversation(uid, "golden-other", "the otter chat")
    db.add_user_fact(uid, "Prefers answers in British English")
    db.add_user_fact(uid, "Works on the ORION-7 badger survey")
    db.add_message(uid, "golden-other", "user", "The badger survey runs every March near the river.")
    db.add_message(uid, "golden-other", "assistant", "Noted: the badger survey is in March.")
    # Embed the other chat's messages now, so the background backfill finds
    # nothing to do in either run.
    asyncio.run(memory_semantic.ensure_message_embeddings(uid))
    db.save_url_document(
        "golden-rich", "https://example.org/badgers", "Badger habitats",
        "Badgers dig setts in woodland. " * 40,
    )
    db.save_document("golden-rich", "survey.pdf", "Badger counts by month. " * 60, 3)
    # A rolling summary plus folded chunks, so in-conversation recall answers.
    db.save_summary("golden-rich", "Earlier we planned the badger survey.", 2, 12)
    asyncio.run(
        recall.index_folded(
            "golden-rich",
            [
                {"role": "user", "content": "The badger survey codename is ORION-7, keep it."},
                {"role": "assistant", "content": "Understood, ORION-7 it is for the badgers."},
            ],
            0,
        )
    )
    return uid


_RICH_HISTORY = [
    {"role": "user", "content": "The badger survey codename is ORION-7, keep it."},
    {"role": "assistant", "content": "Understood, ORION-7 it is for the badgers."},
    {"role": "user", "content": "What did the PDF say about March?"},
    {"role": "assistant", "content": "It lists badger counts by month."},
    {"role": "user", "content": "And the habitat page?"},
    {"role": "assistant", "content": "Badgers dig setts in woodland."},
]

#: Every block the rich conversation must put in the prompt — otherwise
#: identity proves little.
_RICH_MARKERS = (
    "British English",  # saved facts
    "badger survey runs every March",  # cross-chat recall
    "Badger habitats",  # stored page
    "survey.pdf",  # uploaded document
    "Earlier we planned the badger survey.",  # rolling summary
    recall.RECALL_HEADER,  # in-conversation recall
)


# ---------------------------------------------------------------------------
# Living knowledge with a stubbed retrieval that returns a topical hit
# ---------------------------------------------------------------------------

#: Timeless by the deterministic rules ("explain", no recency word): neither
#: tree asks the router, and both reach living_knowledge._topical.
_LK_QUESTION = "Explain how badger setts are ventilated"
_LK_READ = datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc)
_LK_PAGE = (
    "Badger setts are ventilated by their many entrances. Air enters the lower "
    "tunnels and leaves through the higher ones, so a sett breathes without "
    "any chimney. Chambers sit off the main tunnels and are lined with dry "
    "bedding that the badgers air out on warm mornings. "
) * 6


def _lk_evidence() -> list:
    """Fresh objects per call: _topical filters `result.evidence` in place."""
    return [
        # Passes the topical gate (dense >= 0.35, lexical >= 0.34, score >= 0.4).
        Evidence(
            url="https://docs.example.org/setts/ventilation",
            title="How badger setts breathe",
            text=_LK_PAGE,
            domain="docs.example.org",
            authority=2,
            fetched_at=_LK_READ,
            published_at=datetime(2026, 3, 12, tzinfo=timezone.utc),
            source_type="docs",
            dense=0.71,
            lexical=0.67,
            recency=0.2,
            score=0.58,
        ),
        # Not a topical hit, but relevant (lexical >= 0.34): kept as [2].
        Evidence(
            url="https://wildlife.example.net/badger-facts",
            title="Badger facts",
            text="Badgers are nocturnal. Their setts can be centuries old and span many entrances. " * 4,
            domain="wildlife.example.net",
            authority=1,
            fetched_at=_LK_READ,
            dense=0.22,
            lexical=0.4,
            recency=0.1,
            score=0.31,
        ),
        # Neither: filtered out of the block and of meta.sources.
        Evidence(
            url="https://news.example.com/otter-week",
            title="Otter week",
            text="Otters were counted on the river this week. " * 4,
            domain="news.example.com",
            authority=0,
            fetched_at=_LK_READ,
            dense=0.1,
            lexical=0.05,
            recency=0.9,
            score=0.12,
        ),
    ]


#: How long the stubbed retrieval takes. Never instant: an instant stub
#: finishes in the loop step it is scheduled in, before any Fast deadline or
#: pre-check can race it — with KNOWLEDGE_FAST_TOPICAL_DEADLINE_S cut to 1 us
#: the Fast scenario still passed (2026-09-13, scratch mutation).
#:   - 50 ms: well inside the 0.3 s Fast deadline.
#:   - 0.72 s: production's static retrieval p50 (techsara_web_memory_seconds,
#:     n=462, 2026-09-13). The performance prover measured the first Fast
#:     budget dropping REAL topical hits at this latency (prepare_fast answer@5
#:     0.900 -> 0.300 at 0.6 s); the pre-change tree waited and grounded, so
#:     the grounded prompt is the golden at this latency too.
_LK_RETRIEVAL_S = 0.05
_LK_PRODUCTION_P50_S = 0.72


def _knowledge_on(monkeypatch, retrieval_s: float) -> None:
    async def retrieve(question, *, level, top_k=5, **_kwargs):
        await asyncio.sleep(retrieval_s)
        return Retrieval(query=question, freshness=level, evidence=_lk_evidence()[:top_k], newest_age=3600.0)

    monkeypatch.setattr(settings, "living_knowledge_enabled", True)
    monkeypatch.setattr(settings, "living_knowledge_topical", True)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(living_knowledge, "retrieve", retrieve)
    # The page is in the corpus too, so a PostgreSQL-side check of "could any
    # page pass the topical gate" (living_knowledge._topical_precheck,
    # 2026-09-13) sees the same world the stubbed retrieval describes.
    db.upsert_web_page(
        "docs.example.org/setts/ventilation",
        "https://docs.example.org/setts/ventilation",
        "https://docs.example.org/setts/ventilation",
        "How badger setts breathe",
        _LK_PAGE,
        "text/html",
        200,
        "golden-setts-ventilation",
        published_at=datetime(2026, 3, 12, tzinfo=timezone.utc),
        source_type="docs",
        authority=2,
    )


# ---------------------------------------------------------------------------
# Scenarios. Each setup seeds the database and returns the /chat body.
# ---------------------------------------------------------------------------


def _scenario_rich_fast(monkeypatch):
    _rich_fixture()
    body = {
        "mode": "assistant",
        "effort": "fast",
        "conversation_id": "golden-rich",
        "messages": [*_RICH_HISTORY, {"role": "user", "content": "When is the badger survey?"}],
    }
    return body


def _scenario_rich_think(monkeypatch):
    _rich_fixture()
    body = {
        "mode": "assistant",
        "effort": "think",
        "conversation_id": "golden-rich",
        "messages": [*_RICH_HISTORY, {"role": "user", "content": "Summarise the otter notes"}],
    }
    return body


def _scenario_first_message(monkeypatch):
    _local_user()
    body = {"mode": "assistant", "effort": "fast", "conversation_id": "golden-new", "message": "hello there"}
    return body


def _scenario_salesforce(monkeypatch):
    uid = _rich_fixture()
    db.create_conversation(uid, "golden-sf", "sf chat")

    async def route_chat(message, has_image=False, history=()):
        return "chat"

    monkeypatch.setattr(router_engine, "route_request", route_chat)
    body = {
        "mode": "salesforce",
        "conversation_id": "golden-sf",
        "messages": [{"role": "user", "content": "badger survey accounts please"}],
    }
    return body


def _lk_scenario(effort: str, retrieval_s: float = _LK_RETRIEVAL_S):
    def setup(monkeypatch):
        _rich_fixture()
        _knowledge_on(monkeypatch, retrieval_s)
        body = {
            "mode": "assistant",
            "effort": effort,
            "conversation_id": "golden-rich",
            "messages": [*_RICH_HISTORY, {"role": "user", "content": _LK_QUESTION}],
        }
        return body

    return setup


_LK_MARKERS = (
    *_RICH_MARKERS,
    "Reference material this platform has already read",  # topical_block
    "[1] How badger setts breathe",  # the topical hit
    "[2] Badger facts",  # relevant, kept; "Otter week" is filtered out
)

#: name -> (setup(monkeypatch) -> /chat body, markers its prompt must hold —
#: otherwise identity proves little). The fixture files are named after the
#: key; the capture script iterates the same dict.
SCENARIOS = {
    "rich_fast": (_scenario_rich_fast, _RICH_MARKERS),
    "rich_think": (_scenario_rich_think, ("British English",)),
    "first_message": (_scenario_first_message, ("hello there",)),
    # Keyword recall reached the Salesforce chat prompt.
    "salesforce": (_scenario_salesforce, ("badger survey is in March",)),
    "living_knowledge_topical_fast": (_lk_scenario("fast"), _LK_MARKERS),
    "living_knowledge_topical_fast_at_production_latency": (
        _lk_scenario("fast", _LK_PRODUCTION_P50_S),
        _LK_MARKERS,
    ),
    "living_knowledge_topical_think": (_lk_scenario("think"), _LK_MARKERS),
}


# ---------------------------------------------------------------------------
# Living knowledge with the REAL retrieval (second prover pass, 2026-09-13)
# ---------------------------------------------------------------------------
#
# The scenarios above stub living_knowledge.retrieve and make the router
# raise, so the Fast pre-check, the router skip, the Fast budget and
# web_memory's lexical text were never on the path: all 26 tests passed while
# a Fast turn lost the grounding of an accented page (the prover's
# test_zz_quality_prompt.py: HEAD system[0] 4,063 chars with the block, new
# 2,591 without). These run the real web_memory.retrieve, the real topical
# gate and pre-check over seeded pages; only the dense index (a word-overlap
# stand-in that returns a passage from ANYWHERE in the page, as a LanceDB
# chunk does, after production's 0.72 s p50), the router (STATIC after
# 0.2 s) and the model are stubs.

_REAL_DENSE_S = 0.72
_REAL_READ = _LK_READ
_REAL_FILLER = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
    "quebec romeo sierra tango uniform victor whiskey xray yankee zulu "
)
_REAL_EVAL_PAGES = {
    "leaderboard.html": "https://benchlm.test/leaderboard",
    "leaderboard_long.html": "https://benchlm.test/leaderboard-full",
    "leaderboard_cards.html": "https://benchlm.test/cards",
    "no_answer.html": "https://benchlm.test/methodology",
    "pricing_v2.html": "https://orbital.test/pricing",
    "hosting_costs.html": "https://nimbus.test/pricing",
}
_REAL_EXTRA = (
    ("https://food.test/creme-brulee", "Crème brûlée: caramelising the sugar crust",
     ("To finish a crème brûlée, scatter a thin layer of sugar and caramelise it with a torch "
      "until it turns amber. The crust sets as it cools. ") * 6),
    ("https://docs.test/badger-setts", "How badger setts breathe",
     ("Badger setts are ventilated by their many entrances; air enters the lower tunnels and "
      "leaves through the higher ones. ") * 6),
    ("https://archive.test/benchmarks-full", "Benchmark archive",
     (_REAL_FILLER * (205_000 // len(_REAL_FILLER) + 1))[:205_000]
     + " " + ("Orion-9 scored 88.4 on the Kestrel reasoning suite, ahead of every other entry. ") * 8),
)


def _real_dense_rows(query):
    from app import web_memory

    q = set(web_memory._terms(query))
    with db.connection() as con:
        rows = con.execute("SELECT id, url, title, text FROM web_pages ORDER BY id").fetchall()
    hits = []
    for r in rows:
        ratio = len(q & set(web_memory._terms((r["title"] or "") + " " + (r["text"] or "")))) / max(len(q), 1)
        if ratio >= 0.5:
            hits.append({"url": r["url"], "title": r["title"], "page_id": r["id"], "fetched_at": "",
                         "text": web_memory._best_window(r["text"] or "", query), "score": 0.2 + (1 - ratio) * 0.6})
    hits.sort(key=lambda h: (h["score"], h["page_id"]))
    return hits


def _real_knowledge_on(monkeypatch) -> None:
    import hashlib

    from app import freshness, web_index, web_memory
    from app.core import extract

    async def dense(query, top_k=6, site_prefix="", **_kwargs):
        await asyncio.sleep(_REAL_DENSE_S)
        return (await db.run_in_thread(_real_dense_rows, query))[:top_k]

    async def router(question):
        await asyncio.sleep(0.2)
        return freshness.Verdict(freshness.Freshness.STATIC, freshness._MAX_AGE[freshness.Freshness.STATIC], "router")

    monkeypatch.setattr(web_index, "retrieve", dense)
    monkeypatch.setattr(freshness, "_ask_router", router)
    monkeypatch.setattr(settings, "living_knowledge_enabled", True)
    monkeypatch.setattr(settings, "living_knowledge_topical", True)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "knowledge_rerank", False)
    monkeypatch.setattr(settings, "freshness_router_enabled", True)
    monkeypatch.setattr(settings, "knowledge_evidence_cache_ttl_s", 0)
    web_memory.cache_clear()
    vocabulary = getattr(living_knowledge, "_page_vocabulary", None)  # absent on the pre-change tree
    if vocabulary is not None:
        vocabulary.reset()
    fixtures = Path(__file__).parent / "fixtures" / "web_eval"
    pages = []
    for name, url in _REAL_EVAL_PAGES.items():
        ext = extract.extract_readable("text/html", (fixtures / name).read_bytes(), url)
        pages.append((url, ext.title or "", ext.text or ""))
    pages.extend(_REAL_EXTRA)
    for url, title, text in pages:
        db.upsert_web_page(url, url, url, title, text, "text/html", 200, hashlib.sha256(text.encode()).hexdigest())
    with db.connection() as con:
        # A fixed read date: the block and meta.sources carry it.
        con.execute("UPDATE web_pages SET fetched_at = %s, first_seen_at = %s", (_REAL_READ, _REAL_READ))


def _real_scenario(effort: str, question: str):
    def setup(monkeypatch):
        _rich_fixture()
        _real_knowledge_on(monkeypatch)
        return {
            "mode": "assistant",
            "effort": effort,
            "conversation_id": "golden-rich",
            "messages": [*_RICH_HISTORY, {"role": "user", "content": question}],
        }

    return setup


_REAL_BLOCK = "Reference material this platform has already read"
_REAL_EVAL_QUESTION = "Explain the BenchLM reasoning leaderboard methodology"
_REAL_ACCENTED_QUESTION = "explain crème brûlée caramelising"
_REAL_LONG_PAGE_QUESTION = "explain the orion-9 kestrel reasoning suite score"
_REAL_TASK_QUESTION = "write a short poem about how badger setts are ventilated"

SCENARIOS.update({
    "real_fast_eval_topical_hit": (_real_scenario("fast", _REAL_EVAL_QUESTION), (_REAL_BLOCK, "BenchLM")),
    "real_think_eval_topical_hit": (_real_scenario("think", _REAL_EVAL_QUESTION), (_REAL_BLOCK, "BenchLM")),
    "real_fast_accented_page": (_real_scenario("fast", _REAL_ACCENTED_QUESTION), (_REAL_BLOCK, "caramelise it with a torch")),
    "real_fast_passage_past_200k": (_real_scenario("fast", _REAL_LONG_PAGE_QUESTION), (_REAL_BLOCK, "scored 88.4")),
    "real_fast_timeless_task_with_a_matching_page": (
        _real_scenario("fast", _REAL_TASK_QUESTION), (_REAL_BLOCK, "leaves through the higher"),
    ),
})

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "context_assembly_golden"
#: The meta keys a prompt change can move. Everything else in meta is either
#: generated per turn (ids) or not decided by context assembly.
PINNED_META = ("context", "route", "knowledge", "sources")


def normalise_prompt(prompt: str) -> str:
    """The ONLY rewrite applied to the engine-bound bytes: today's date."""
    for today in {datetime.now(timezone.utc).date().isoformat(), date.today().isoformat()}:
        prompt = prompt.replace(today, "<TODAY>")
    return prompt


def meta_bytes(meta: dict) -> str:
    return json.dumps(meta, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def golden_turn(monkeypatch, captured: list, name: str, run: str):
    """Run one scenario through POST /chat: (normalised prompt bytes, pinned meta)."""
    setup, _markers = SCENARIOS[name]
    body = setup(monkeypatch)
    llm.embed_cache_clear()
    compaction._pending_notice.clear()
    captured.clear()
    with TestClient(main.app) as client:
        # A session of its own per run: with no conversation history the
        # worker falls back to the in-process memory of the session, and an
        # earlier test's exchange must not become this one's history.
        # (session_id allows 64 characters, so the scenario goes in by index.)
        session = f"golden-{list(SCENARIOS).index(name)}-{run}"
        resp = client.post("/chat", json={**body, "session_id": session})
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "done", kinds
    meta = dict(next(data for kind, data in events if kind == "meta"))
    assert len(captured) == 1, "the engine was called once"
    return normalise_prompt(captured[0]), {key: meta[key] for key in PINNED_META if key in meta}


def assert_markers(name: str, prompt: str) -> None:
    for marker in SCENARIOS[name][1]:
        assert marker in prompt, f"{name}: the seeded context is missing from the prompt: {marker!r}"


def _pretty(prompt: str) -> list:
    return json.dumps(json.loads(prompt), indent=1, ensure_ascii=False).splitlines()


# ---------------------------------------------------------------------------
# Golden: the pre-change prompt, byte for byte, with the reads on and off
# ---------------------------------------------------------------------------


def test_every_scenario_has_a_baseline_fixture_holding_its_seeded_context_and_no_fixture_is_orphaned():
    expected = {f"{name}.{part}.json" for name in SCENARIOS for part in ("messages", "meta")}
    present = {p.name for p in GOLDEN_DIR.glob("*.json") if p.name != "MANIFEST.json"}
    assert present == expected
    for name in SCENARIOS:
        assert_markers(name, (GOLDEN_DIR / f"{name}.messages.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("concurrent_reads", ["false", "true"])
@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_the_engine_is_handed_the_pre_change_prompt_byte_for_byte_with_the_reads_concurrent_or_not(
    scenario, concurrent_reads, offline_turn, monkeypatch
):
    monkeypatch.setenv("CONTEXT_CONCURRENT_READS", concurrent_reads)
    # These goldens pin the FULL path's prompt; "hello there" at Fast would
    # take the small-talk lane (app/fast_lane.py, tests/test_fast_lane_*.py).
    monkeypatch.setenv("FAST_LANE_ENABLED", "false")
    # main.py prefers the Settings attribute since 2026-09-13; pin both.
    monkeypatch.setattr(settings, "context_concurrent_reads", concurrent_reads == "true", raising=False)
    # The flag must really reach the turn, or the "true" run is the "false"
    # run twice and proves nothing about the concurrent path.
    started: list = []
    real_start = main._ContextReads.start

    def spying_start(self, key, factory):
        if self.concurrent:
            started.append(key)
        return real_start(self, key, factory)

    monkeypatch.setattr(main._ContextReads, "start", spying_start)

    prompt, meta = golden_turn(monkeypatch, offline_turn, scenario, concurrent_reads)

    expected_prompt = (GOLDEN_DIR / f"{scenario}.messages.json").read_text(encoding="utf-8")
    expected_meta = (GOLDEN_DIR / f"{scenario}.meta.json").read_text(encoding="utf-8")
    if prompt + "\n" != expected_prompt:
        diff = "\n".join(
            list(
                difflib.unified_diff(
                    _pretty(expected_prompt), _pretty(prompt), "baseline", "current", lineterm="", n=2
                )
            )[:80]
        )
        pytest.fail(f"{scenario}: the engine-bound messages differ from the pre-change tree:\n{diff}")
    assert meta_bytes(meta) == expected_meta
    if concurrent_reads == "true":
        assert started, "CONTEXT_CONCURRENT_READS=true started no read concurrently"
    else:
        assert not started, started


# ---------------------------------------------------------------------------
# The reads really run together
# ---------------------------------------------------------------------------


def test_the_context_reads_run_at_the_same_time_rather_than_one_after_another(
    offline_turn, monkeypatch
):
    """Two reads that can only finish TOGETHER: each waits at a barrier for
    the other. One-at-a-time, the first read waits alone until the barrier
    times out and the turn fails; started together, both pass it."""
    _rich_fixture()
    monkeypatch.setenv("CONTEXT_CONCURRENT_READS", "true")
    barrier = threading.Barrier(2, timeout=3)
    real_facts, real_docs = db.list_user_facts, db.get_documents

    def facts(*args, **kwargs):
        barrier.wait()
        return real_facts(*args, **kwargs)

    def documents(*args, **kwargs):
        barrier.wait()
        return real_docs(*args, **kwargs)

    monkeypatch.setattr(db, "list_user_facts", facts)
    monkeypatch.setattr(db, "get_documents", documents)
    body = {
        "mode": "assistant",
        "effort": "fast",
        "conversation_id": "golden-rich",
        "messages": [*_RICH_HISTORY, {"role": "user", "content": "When is the badger survey?"}],
    }
    with TestClient(main.app) as client:
        resp = client.post("/chat", json=body)
    kinds = [kind for kind, _ in _parse_sse(resp.text)]
    assert kinds[-1] == "done", resp.text
    assert "survey.pdf" in offline_turn[0] and "British English" in offline_turn[0]


def test_a_read_the_turn_never_collects_is_cancelled_without_an_unretrieved_exception(caplog):
    async def scenario():
        reads = main._ContextReads(True)

        async def boom():
            raise RuntimeError("the database hiccuped")

        async def slow():
            await asyncio.sleep(10)

        reads.start("failed", boom)
        reads.start("never", slow)
        await asyncio.sleep(0.01)
        reads.close()
        await asyncio.sleep(0)
        # A read that was never started falls back to reading inline.
        assert await reads.get("inline", lambda: asyncio.sleep(0, result="inline")) == "inline"

    asyncio.run(scenario())
    assert "never retrieved" not in caplog.text


def test_an_exception_from_a_started_read_surfaces_at_its_call_site():
    async def scenario():
        reads = main._ContextReads(True)

        async def boom():
            raise LookupError("no such conversation")

        reads.start("facts", boom)
        with pytest.raises(LookupError):
            await reads.get("facts", boom)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# The meter's count behind the answer (compaction.prepare_deferred)
# ---------------------------------------------------------------------------


def _counting(calls: list, window: int = 1_000_000):
    async def count(base_url, model, messages):
        calls.append(len(messages))
        return sum(len(str(m.get("content") or "")) // 4 + 5 for m in messages) + 3, window

    return count


def test_prepare_deferred_returns_the_prompt_before_the_count_and_the_same_info_after_it(monkeypatch):
    uid = _local_user()
    db.create_conversation(uid, "defer-1", "d")
    db.save_summary("defer-1", "We spoke about otters.", 1, 5)
    calls: list = []
    monkeypatch.setattr(context, "count_tokens", _counting(calls))
    monkeypatch.setitem(context._window_cache, "http://x/v1", 1_000_000)
    history = [{"role": "user", "content": "otters?"}, {"role": "assistant", "content": "yes"}]

    async def scenario():
        expected_history, expected_info = await compaction.prepare(
            "defer-1", history, "more otters", base_url="http://x/v1", model="m"
        )
        calls.clear()
        got_history, info, pending = await compaction.prepare_deferred(
            "defer-1", history, "more otters", base_url="http://x/v1", model="m"
        )
        assert info is None and pending is not None
        assert got_history == expected_history
        assert await pending == expected_info
        return expected_info

    info = asyncio.run(scenario())
    assert list(info) == [
        "tokens_used", "usable_budget", "window", "reserved_output", "fraction", "summarized_turns",
    ]
    assert calls == [3], "exactly one count, made behind the prompt"


@pytest.mark.parametrize(
    "case",
    ["no_window_yet", "near_the_absolute_cap", "multimodal"],
)
def test_prepare_deferred_counts_first_whenever_the_bound_cannot_rule_compaction_out(monkeypatch, case):
    uid = _local_user()
    db.create_conversation(uid, "defer-2", "d")
    calls: list = []
    monkeypatch.setattr(context, "count_tokens", _counting(calls))
    if case != "no_window_yet":
        monkeypatch.setitem(context._window_cache, "http://x/v1", 1_000_000)
    else:
        monkeypatch.setattr(context, "_window_cache", {})
    history = [{"role": "user", "content": "hi"}]
    if case == "near_the_absolute_cap":
        monkeypatch.setattr(settings, "context_compact_max_tokens", 100)
        history = [{"role": "user", "content": "x" * 400}]
    if case == "multimodal":
        history = [{"role": "user", "content": [{"type": "text", "text": "look"}]}]

    async def scenario():
        return await compaction.prepare_deferred(
            "defer-2", history, "next", base_url="http://x/v1", model="m"
        )

    _history, info, pending = asyncio.run(scenario())
    assert pending is None and info is not None
    assert calls, "the count ran before the prompt was returned"


# ---------------------------------------------------------------------------
# One query embedding per question per turn
# ---------------------------------------------------------------------------


def test_two_concurrent_embeddings_of_the_same_question_make_one_sidecar_call(monkeypatch):
    calls: list = []

    async def embed(texts, **_kwargs):
        calls.append(list(texts))
        await asyncio.sleep(0.05)
        return [[0.1, 0.2, 0.3]]

    monkeypatch.setattr(llm, "embed_texts", embed)
    llm.embed_cache_clear()

    async def scenario():
        return await asyncio.gather(
            llm.embed_query("when is the badger survey?"),
            llm.embed_query("when is the  badger survey?"),
        )

    first, second = asyncio.run(scenario())
    assert first == second == [0.1, 0.2, 0.3]
    assert len(calls) == 1


def test_a_cancelled_embedding_caller_does_not_fail_the_one_that_joined_it(monkeypatch):
    async def embed(texts, **_kwargs):
        await asyncio.sleep(0.05)
        return [[1.0, 0.0]]

    monkeypatch.setattr(llm, "embed_texts", embed)
    llm.embed_cache_clear()

    async def scenario():
        leader = asyncio.ensure_future(llm.embed_query("otters"))
        await asyncio.sleep(0.01)
        joiner = asyncio.ensure_future(llm.embed_query("otters"))
        await asyncio.sleep(0.01)
        leader.cancel()
        return await joiner

    assert asyncio.run(scenario()) == [1.0, 0.0]


def test_in_conversation_recall_uses_the_cached_query_embedding(monkeypatch):
    uid = _local_user()
    db.create_conversation(uid, "recall-cache", "r")
    monkeypatch.setattr(llm, "embed_texts", _fake_embed)
    asyncio.run(
        recall.index_folded(
            "recall-cache", [{"role": "user", "content": "The badger codename is ORION-7."}], 0
        )
    )
    calls: list = []

    async def counting_embed(texts, **kwargs):
        calls.append(list(texts))
        return await _fake_embed(texts, **kwargs)

    monkeypatch.setattr(llm, "embed_texts", counting_embed)
    llm.embed_cache_clear()

    async def scenario():
        # Cross-chat recall embeds the question first in the same turn.
        await llm.embed_query("badger codename?")
        return await recall.retrieve_block("recall-cache", "badger codename?", effort="fast")

    block = asyncio.run(scenario())
    assert block and "ORION-7" in block
    assert len(calls) == 1, "recall re-embedded a question the turn had already embedded"


# ---------------------------------------------------------------------------
# /tokenize's token-id list is parsed off the event loop when it is large
# ---------------------------------------------------------------------------


def test_a_large_tokenize_body_is_parsed_in_a_worker_thread(monkeypatch):
    import httpx

    big = {"count": 50_000, "max_model_len": 1_000_000, "tokens": list(range(50_000))}
    small = {"count": 5, "max_model_len": 1_000_000, "tokens": [1, 2, 3, 4, 5]}
    threads: list = []
    real_json = httpx.Response.json

    def spying_json(self, **kwargs):
        threads.append(threading.current_thread() is threading.main_thread())
        return real_json(self, **kwargs)

    monkeypatch.setattr(httpx.Response, "json", spying_json)

    class _Tokenize:
        is_closed = False

        def __init__(self, payload):
            self.payload = payload

        async def post(self, url, json):
            return httpx.Response(200, json=self.payload, request=httpx.Request("POST", url))

    for payload in (small, big):
        monkeypatch.setattr(context, "_tokenize_client", lambda p=payload: _Tokenize(p))
        count, window = asyncio.run(
            context.count_tokens("http://x/v1", "m", [{"role": "user", "content": "hi"}])
        )
        assert count == payload["count"] and window == 1_000_000
    assert threads == [True, False], "small inline on the loop, large in a thread"


# ---------------------------------------------------------------------------
# The same real-retrieval grounding, eight Fast turns at once (2026-09-13)
# ---------------------------------------------------------------------------


def test_eight_concurrent_fast_turns_ground_exactly_as_the_pre_change_single_turn(offline_turn, monkeypatch):
    """The prover's conc_eval found the first Fast budget dropping hits only
    under concurrency (8/24 at c=8), which no single-turn golden can see. Each
    of eight concurrent Fast turns must carry, byte for byte, the grounding
    block the pre-change tree put in the single-turn prompt."""
    _real_knowledge_on(monkeypatch)
    fixture = json.loads((GOLDEN_DIR / "real_fast_eval_topical_hit.messages.json").read_text(encoding="utf-8"))
    system = fixture[0]["content"]
    assert _REAL_BLOCK in system

    async def burst():
        return await asyncio.gather(*(
            living_knowledge.prepare(
                _REAL_EVAL_QUESTION, effort="fast", mode="assistant", web_search_pref="auto", allow_network=False
            )
            for _ in range(8)
        ))

    results = asyncio.run(burst())
    for prepared in results:
        assert prepared.decision == "static_topical", (prepared.decision, prepared.degraded)
        assert normalise_prompt(prepared.grounding) in system
