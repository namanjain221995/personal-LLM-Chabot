"""The Fast topical pre-check may say "no page can pass the gate" only when no
page can (second prover pass, 2026-09-13).

The pre-check that shipped first asked PostgreSQL's search_tsv. The prover
showed two pages the full gate grounds on and the pre-check rejected — an
accented question ("explain crème brûlée caramelising") and an answer past
search_tsv's 200,000-char cap — and on the 55432 test server to_tsvector also
keeps a file path and an e-mail address as single lexemes where the gate's
`_WORD` sees the words inside them. In every case Fast answered from weights
where HEAD cited the page. The pre-check now reads the gate's own tokens
(living_knowledge._page_vocabulary); these tests call the pre-check and the
full gate on the same seeded page and require that the pre-check never says
False when the gate accepts.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import time

import pytest

from app import db, freshness, web_index, web_memory
from app import living_knowledge as lk
from app.config import settings

FILLER = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
          "mike november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee zulu ").split()


def _filler(n: int) -> str:
    out, size, i = [], 0, 0
    while size < n:
        out.append(FILLER[i % len(FILLER)])
        size += len(out[-1]) + 1
        i += 1
    return " ".join(out)


#: (label, url, title, text, question, a string the grounding must carry)
PAGES = [
    ("accented question", "https://food.test/creme-brulee", "Crème brûlée: caramelising the sugar crust",
     ("To finish a crème brûlée, scatter a thin layer of sugar and caramelise it with a torch "
      "until it turns amber. The crust sets as it cools. ") * 6,
     "explain crème brûlée caramelising", "caramelise it with a torch"),
    ("answer past 200k chars", "https://archive.test/benchmarks-full", "Benchmark archive",
     _filler(205_000) + " " + ("Orion-9 scored 88.4 on the Kestrel reasoning suite, ahead of every other entry. ") * 8,
     "explain the orion-9 kestrel reasoning suite score", "scored 88.4"),
    ("words inside a file path", "https://ops.test/install", "Installing the service",
     ("Run /opt/zephyr/kestrel/bin/orionctl start after unpacking; /opt/zephyr/kestrel/bin/orionctl "
      "status confirms it. ") * 8,
     "explain zephyr kestrel orionctl", "orionctl start"),
    ("typographic apostrophe and ellipsis", "https://support.test/licensing", "Support contact",
     ("Nebulon’s Quasar… licences are renewed every spring by the regional office. ") * 10,
     "explain nebulon quasar licences", "renewed every spring"),
    ("control", "https://docs.test/badger-setts", "How badger setts breathe",
     ("Badger setts are ventilated by their many entrances; air enters the lower tunnels and "
      "leaves through the higher ones. ") * 6,
     "explain how badger setts are ventilated", "leaves through the higher"),
]


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
    monkeypatch.setattr(lk, "claims_for", lambda q, limit=3: [])
    yield
    lk._page_vocabulary.reset()


def _store(url, title, text):
    return db.upsert_web_page(url, url, url, title, text, "text/html", 200, hashlib.sha256(text.encode()).hexdigest())


def _dense_from_anywhere(monkeypatch):
    """A word-overlap stand-in for the vector index that returns the passage
    from ANYWHERE in the page, as a LanceDB chunk does (the prover's)."""

    def rows(query):
        q = set(web_memory._terms(query))
        with db.connection() as con:
            pages = con.execute("SELECT id, url, title, text FROM web_pages").fetchall()
        hits = []
        for r in pages:
            ratio = len(q & set(web_memory._terms(f"{r['title']} {r['text']}"))) / max(len(q), 1)
            if ratio >= 0.5:
                hits.append({"url": r["url"], "title": r["title"], "page_id": r["id"], "fetched_at": "",
                             "text": web_memory._best_window(r["text"], query), "score": 0.2 + (1 - ratio) * 0.6})
        return sorted(hits, key=lambda h: h["score"])

    async def dense(query, top_k=6, site_prefix="", **_):
        return (await db.run_in_thread(rows, query))[:top_k]

    monkeypatch.setattr(web_index, "retrieve", dense)


@pytest.mark.parametrize("label,url,title,text,question,want", PAGES, ids=[p[0] for p in PAGES])
def test_the_precheck_never_rejects_a_page_the_full_gate_grounds_on(monkeypatch, label, url, title, text, question, want):
    _dense_from_anywhere(monkeypatch)
    _store(url, title, text)
    out = lk.Prepared(verdict=freshness.static_timeless_task())
    asyncio.run(lk._topical(question, out, effort="think"))
    assert out.decision == "static_topical" and want in out.grounding, (label, out.decision)
    assert lk._topical_precheck(question) is not False, label


@pytest.mark.parametrize("label,url,title,text,question,want", PAGES, ids=[p[0] for p in PAGES])
def test_a_fast_turn_grounds_on_the_same_page_as_think(monkeypatch, label, url, title, text, question, want):
    _dense_from_anywhere(monkeypatch)
    _store(url, title, text)
    out = lk.Prepared(verdict=freshness.static_timeless_task())
    asyncio.run(lk._topical(question, out, effort="fast"))
    assert out.decision == "static_topical" and want in out.grounding, (label, out.decision, out.degraded)


def test_a_question_no_stored_page_can_answer_is_still_a_proven_miss():
    for _label, url, title, text, _q, _w in PAGES:
        _store(url, title, text)
    assert lk._topical_precheck("what is the boiling point of water at altitude") is False
    assert lk._topical_precheck("what is it?") is False  # no content word at all


def test_a_page_stored_after_the_vocabulary_was_built_is_seen_by_the_next_turn():
    _store("https://docs.test/rice", "Cooking rice", "Rinse the rice and simmer it for twelve minutes. " * 5)
    assert lk._topical_precheck("explain badger sett ventilation") is False
    _label, url, title, text, question, _w = PAGES[-1]
    _store(url, title, text)
    assert lk._topical_precheck(question) is True


def test_a_page_whose_text_changed_loses_the_old_words_once_it_is_reindexed():
    url = "https://docs.test/changing"
    _store(url, "Changing page", "zirconium lattice annealing notes " * 20)
    with db.connection() as con:
        con.execute("UPDATE web_pages SET indexed_at = now()")
    assert lk._topical_precheck("zirconium lattice annealing") is True
    _store(url, "Changing page", "tungsten filament drawing notes " * 20)
    # Re-fetched with a new hash: indexed_at is NULL until the vectors are
    # rebuilt, and the index may still serve the OLD chunks -> old words kept.
    assert lk._topical_precheck("zirconium lattice annealing") is True
    assert lk._topical_precheck("tungsten filament drawing") is True
    with db.connection() as con:
        con.execute("UPDATE web_pages SET indexed_at = now()")
    assert lk._topical_precheck("zirconium lattice annealing") is False
    assert lk._topical_precheck("tungsten filament drawing") is True


def test_a_turn_that_finds_the_vocabulary_being_built_is_told_could_not_tell():
    _store("https://docs.test/rice", "Cooking rice", "Rinse the rice and simmer it for twelve minutes. " * 5)
    assert lk._page_vocabulary._sync_lock.acquire(blocking=False)
    try:
        assert lk._topical_precheck("explain badger sett ventilation") is None
    finally:
        lk._page_vocabulary._sync_lock.release()
    assert lk._topical_precheck("explain badger sett ventilation") is False


def test_a_database_failure_is_could_not_tell(monkeypatch):
    def broken():
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(db, "connection", broken)
    assert lk._topical_precheck("explain badger sett ventilation") is None


def test_the_vocabulary_holds_exactly_the_gates_stems_even_when_a_page_is_tokenised_in_slices():
    rnd = random.Random(7)
    words = ["configured", "gpt-5.2", "crème", "zürich", "/usr/local/bin", "user@example.com", "3.14.5",
             "lenses", "the", "a", "running", "boxes", "v2.1", "foo_bar"] + FILLER
    text = " ".join(rnd.choice(words) + ("\n" if rnd.random() < 0.05 else "") for _ in range(60_000))
    assert len(text) > 3 * lk._VOCAB_SLICE_CHARS
    assert lk._page_stems("A Title", text) == set(web_memory._terms("A Title")) | set(web_memory._terms(text))


def test_the_bloom_filter_has_no_false_negatives():
    rnd = random.Random(11)
    stems = {"".join(rnd.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(rnd.randint(2, 12)))
             for _ in range(20_000)}
    bits, blob = lk._bloom(stems)
    state = lk._VocabState(fingerprint=(), pages={1: (None, bits, blob)}, matrices=lk._matrices({1: (None, bits, blob)}))
    sample = list(stems)[:2_000]
    for i in range(0, len(sample), 10):
        group = set(sample[i : i + 10])
        assert state.could_pass(group, len(group))


def test_tokenising_a_two_megabyte_page_yields_the_gil_between_slices(monkeypatch):
    """One `findall` over 2,367,944 chars holds the GIL for ~80 ms; the event
    loop must be able to run between slices."""
    sleeps = []
    real_sleep = time.sleep
    monkeypatch.setattr(lk.time, "sleep", lambda s: sleeps.append(s) or real_sleep(s))
    lk._page_stems(_filler(2_367_944))
    assert len(sleeps) >= 2_367_944 // lk._VOCAB_SLICE_CHARS - 1


def test_the_precheck_answers_for_a_two_thousand_page_corpus_in_milliseconds():
    """The live corpus is 2,209 servable pages (2026-09-07). The per-turn cost
    once the vocabulary is built is one fingerprint query and a Bloom probe."""
    rnd = random.Random(3)
    vocab = [f"w{i}" for i in range(30_000)]
    with db.connection() as con:
        for i in range(2_000):
            text = " ".join(rnd.choice(vocab) for _ in range(300))
            con.execute(
                "INSERT INTO web_pages (url_key, url, title, text, content_hash, domain, fetched_at, first_seen_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, now(), now())",
                (f"k{i}", f"https://p{i}.test/", f"page {i}", text, f"h{i}", f"p{i}.test"),
            )
    started = time.perf_counter()
    assert lk._topical_precheck("explain zzqq yyxx wwvv") is False  # builds
    build = time.perf_counter() - started
    timings = []
    for _ in range(20):
        started = time.perf_counter()
        lk._topical_precheck("explain zzqq yyxx wwvv")
        timings.append(time.perf_counter() - started)
    timings.sort()
    assert timings[10] < 0.05, (build, timings)
