"""The retrieval pre-pass off the event loop (2026-09-13, plan item 3a/3b).

Measured before this change: the lexical stage p50 0.223 s / p95 0.984 s
(knowledge_stage_seconds), the lexical SQL carried the FULL text of every
matching page (up to 2,367,944 chars) and computed ts_rank_cd twice per row,
and `_best_window`, `_score` and `_collapse_duplicates` ran on the event loop,
so every in-flight stream waited behind them (8 concurrent Fast: TTFT 11.7 s,
2.3 s with the pre-pass off, 2026-09-05).

What is pinned here: the loop keeps ticking while a page set is windowed; the
statement reads text once, capped at the prefix search_tsv indexes, with the
rank computed once; and the rows and the evidence are the same as the old
statement produced on the web_eval fixtures.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db, llm, rerank, web_index, web_memory
from app.config import settings
from app.freshness import Freshness

FIXTURES = Path(__file__).parent / "fixtures" / "web_eval"

#: The statement exactly as it stood before 2026-09-13 (web_memory.py @ HEAD
#: 2fd37f3), kept here as the reference the new one must agree with.
_OLD_SQL = """WITH ranked AS (
               SELECT id, url, title, text, domain, authority, fetched_at,
                      published_at, modified_at, source_type, origin,
                      ts_rank_cd(search_tsv, websearch_to_tsquery('english', %s), 32) AS rank,
                      row_number() OVER (
                        PARTITION BY domain
                        ORDER BY ts_rank_cd(search_tsv, websearch_to_tsquery('english', %s), 32) DESC
                      ) AS dn
                 FROM web_pages
                WHERE search_tsv @@ websearch_to_tsquery('english', %s)
                  AND text <> ''
                  AND quarantined_at IS NULL
             )
             SELECT * FROM ranked WHERE dn <= 3 ORDER BY rank DESC LIMIT %s"""


def _old_lexical_candidates(query: str, limit: int):
    """The pre-change `_lexical_candidates`, verbatim apart from the SQL name."""
    words = web_memory._content_words(query)[:12]
    if not words:
        return []
    plain = web_memory.lexical_query(words)
    any_of = " or ".join(web_memory.lexical_query([w]) for w in words)
    with db.connection() as con:
        rows = list(con.execute(_OLD_SQL, (plain, plain, plain, limit)).fetchall())
        if len(rows) < limit:
            seen = {r["id"] for r in rows}
            for r in con.execute(_OLD_SQL, (any_of, any_of, any_of, limit)).fetchall():
                if r["id"] not in seen:
                    rows.append(r)
                    seen.add(r["id"])
        return rows[:limit]


def _now():
    return datetime.now(timezone.utc)


def _text_of(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def _page(key, url, title, text, *, days=1.0):
    when = _now() - timedelta(days=days)
    with db.connection() as con:
        con.execute(
            """INSERT INTO web_pages
                 (url_key, url, title, text, fetched_at, first_seen_at, last_changed_at,
                  domain, authority, indexed_at, content_hash, origin)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 40, %s, %s, 'search')""",
            (key, url, title, text, when, when, when, url.split("/")[2], when, key),
        )


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with db.connection() as con:
        con.execute("TRUNCATE web_pages RESTART IDENTITY CASCADE")
    web_memory.cache_clear()
    llm.embed_cache_clear()
    rerank.reset_for_tests()
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    # The cross-encoder is a separate service with its own tests; here the
    # hybrid order is what is compared.
    monkeypatch.setattr(settings, "knowledge_rerank", False)

    async def no_dense(query, top_k=10, **kw):
        return []

    monkeypatch.setattr(web_index, "retrieve", no_dense)
    yield
    rerank.reset_for_tests()


def _seed_fixture_corpus():
    """The web_eval fixture pages, four of them on one domain so the
    three-per-domain cut is exercised, plus two big pages: one of 2.3 MB whose
    question terms all sit inside the indexed 200k prefix, and one of 222k whose
    answer sits just inside it."""
    for i, path in enumerate(sorted(FIXTURES.glob("*.html"))):
        domain = "bench.example" if i < 4 else f"site{i}.example"
        text = _text_of(path.read_text(encoding="utf-8"))
        _page(f"fx{i}", f"https://{domain}/{path.stem}", path.stem.replace("_", " ").title(), text, days=1 + i)
    head = ("Methodology notes on how every model score and price is measured. " * 40)
    answer = " The reasoning score leaderboard lists GPT-5.2 with a score of 82.7 and pricing per million tokens. "
    filler = "Unrelated archive paragraph about gardening and weather patterns. " * 34_000  # ~2.3 MB
    big = head + ("x " * 30_000) + answer + ("y " * 30_000) + filler
    assert len(big) > 2_000_000
    _page("big", "https://huge.example/archive", "Archive", big, days=2)
    mid = head + ("z " * 90_000) + answer + ("w " * 20_000)
    assert len(mid) > 200_000 and mid.index("82.7") < 200_000
    _page("mid", "https://mid.example/page", "Mid page", mid, days=3)


_QUESTIONS = [
    "GPT-5.2 reasoning score leaderboard",
    "what is the price per million tokens",
    "hosting costs per month",
    "benchlm leaderboard methodology",
    "model pricing",
]


def test_the_lexical_statement_computes_the_rank_once_and_reads_the_full_text():
    """The text cap was reverted on 2026-09-13 (second prover pass): it cost
    Think and Max the passage past 200,000 chars."""
    source = inspect.getsource(web_memory._lexical_candidates)
    assert source.count("ts_rank_cd(search_tsv") == 1
    assert "left(p.text" not in source
    assert "SELECT p.id, p.url, p.title, p.text," in source


def test_the_lexical_rows_match_the_old_statement_text_and_all():
    _seed_fixture_corpus()
    for q in _QUESTIONS:
        for limit in (2, 5, 24):
            old = _old_lexical_candidates(q, limit)
            new = web_memory._lexical_candidates(q, limit)
            assert old, f"fixture corpus finds nothing for {q!r}; the comparison would be empty"

            def shape(rows):
                return sorted(
                    (-float(r["rank"]), int(r["id"]), int(r["dn"]), r["url"], r["title"], r["domain"],
                     r["authority"], r["fetched_at"], r["origin"])
                    for r in rows
                )

            assert shape(new) == shape(old), q
            # Same order: AND rows first, then OR rows, each ranked (ties may
            # fall either way, as they always could).
            assert [float(r["rank"]) for r in new] == [float(r["rank"]) for r in old]
            old_text = {r["id"]: r["text"] for r in old}
            for r in new:
                assert r["text"] == old_text[r["id"]]


def test_retrieve_returns_the_same_evidence_as_with_the_old_statement(monkeypatch):
    _seed_fixture_corpus()

    def evidence(q):
        web_memory.cache_clear()
        r = asyncio.run(web_memory.retrieve(q, level=Freshness.STATIC, top_k=5, use_cache=False))
        rows = [(e.url, e.title, e.text, round(e.score, 6), e.lexical, e.dense, e.page_id) for e in r.evidence]
        # The prompt blocks the model reads, byte for byte.
        return rows, web_memory.grounding_block(r, "2026-09-13"), web_memory.topical_block(r, "2026-09-13")

    new = {q: evidence(q) for q in _QUESTIONS}
    monkeypatch.setattr(web_memory, "_lexical_candidates", _old_lexical_candidates)
    old = {q: evidence(q) for q in _QUESTIONS}
    assert any(v[0] for v in new.values()) and any(v[1] for v in new.values())
    assert new == old
    # The 2.3 MB page's answer passage is still the window chosen.
    big = [row for rows, _g, _t in new.values() for row in rows if row[0] == "https://huge.example/archive"]
    assert big and any("82.7" in row[2] for row in big)


def test_a_window_past_the_indexed_prefix_is_still_found():
    """A page matched on its first 200,000 chars (here, by its title) is
    windowed over its WHOLE text, as it was before 2026-09-13: the passage the
    question asks about may lie past the prefix `search_tsv` indexes."""
    _seed_fixture_corpus()
    beyond = ("filler words here " * 12_000) + " zebracorn quartzite marker "
    assert len(beyond) > 200_000
    _page("beyond", "https://beyond.example/p", "Zebracorn", beyond)
    rows = web_memory._lexical_candidates("zebracorn quartzite", 5)
    row = next(r for r in rows if r["url"] == "https://beyond.example/p")  # found by its title
    assert len(row["text"]) == len(beyond)
    assert "quartzite" in web_memory._best_window(row["text"], "zebracorn quartzite")


def test_a_leaderboard_row_past_200k_chars_stays_in_the_evidence_at_every_level():
    """The prover's adv_longpage.py as a test: a 249,196-char leaderboard whose
    asked-for row sits at char 240,154, the vector half unavailable. With the
    text capped at 200,000 chars the row left the evidence (lexical 0.33 ->
    0.17) at STATIC and RECENT alike, so at Think and Max too."""
    rows, i = [], 0
    while sum(len(r) + 1 for r in rows) < 240_000:
        rows.append(f"| Model-{i:05d} | reasoning {40 + (i % 50)}.{i % 10} | coding {30 + (i % 60)}.{i % 7} |")
        i += 1
    rows.append("| Zephyr-77 | reasoning 91.3 | coding 88.0 |")
    rows += [f"| Tail-{j:04d} | reasoning 20.{j % 10} | coding 10.{j % 7} |" for j in range(200)]
    text = (
        "BenchLM full leaderboard. Reasoning and coding scores for every ranked model.\n"
        "| Model | Reasoning | Coding |\n|---|---|---|\n" + "\n".join(rows)
    )
    assert text.index("Zephyr-77") > 200_000
    _page("archive", "https://benchlm.test/leaderboard-archive", "BenchLM full leaderboard", text)
    for level in (Freshness.STATIC, Freshness.RECENT):
        web_memory.cache_clear()
        r = asyncio.run(web_memory.retrieve("zephyr-77 reasoning score", level=level, top_k=5, use_cache=False))
        assert any("Zephyr-77 | reasoning 91.3" in e.text for e in r.evidence), (level, [e.lexical for e in r.evidence])


def test_windowing_a_large_page_set_does_not_stall_the_event_loop(monkeypatch):
    """24 lexical rows of 200,000 chars dense with question terms: the merge
    costs tens of milliseconds of pure Python. A heartbeat task on the loop
    must keep ticking while it runs, which it cannot do if the windowing runs
    inline (that is how it ran until 2026-09-13)."""
    q = "reasoning score leaderboard pricing tokens"
    unit = "the reasoning model has a score on the leaderboard and pricing in tokens " \
           "while unrelated prose about gardening follows for a while here. "
    rows = [
        {
            "id": i + 1,
            "url": f"https://load{i}.example/p",
            "title": f"Load {i}",
            "text": (unit * (200_000 // len(unit) + 1))[:200_000],
            "domain": f"load{i}.example",
            "authority": 40,
            "fetched_at": _now(),
            "published_at": None,
            "modified_at": None,
            "source_type": "",
            "origin": "search",
        }
        for i in range(24)
    ]
    started = time.perf_counter()
    for row in rows:
        web_memory._best_window(row["text"], q)
    inline_cost = time.perf_counter() - started
    assert inline_cost > 0.05, f"the load is too light to prove anything ({inline_cost * 1000:.0f} ms)"

    monkeypatch.setattr(web_memory, "_lexical_candidates", lambda query, limit: rows)

    async def scenario():
        gaps = []
        stop = asyncio.Event()

        async def heartbeat():
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.002)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.01)
        result = await web_memory.retrieve(q, level=Freshness.RECENT, top_k=5, use_cache=False)
        stop.set()
        await beat
        return result, max(gaps)

    result, worst_gap = asyncio.run(scenario())
    assert result.evidence  # identical pages collapse to one; the point is the loop
    # Inline, the loop would be blocked for the whole merge (the worst gap is
    # at least `inline_cost`). In a thread it only yields the GIL at the 5 ms
    # switch interval.
    assert worst_gap < inline_cost / 2, (
        f"event loop stalled {worst_gap * 1000:.1f} ms while windowing "
        f"(inline cost {inline_cost * 1000:.1f} ms)"
    )


def test_scoring_and_duplicate_collapse_run_in_a_worker_thread(monkeypatch):
    _seed_fixture_corpus()
    loop_thread = {}
    seen = {}
    real_rank = web_memory._rank_candidates
    real_merge = web_memory._merge_candidates

    import threading

    def rank(*a, **k):
        seen["rank"] = threading.get_ident()
        return real_rank(*a, **k)

    def merge(*a, **k):
        seen["merge"] = threading.get_ident()
        return real_merge(*a, **k)

    monkeypatch.setattr(web_memory, "_rank_candidates", rank)
    monkeypatch.setattr(web_memory, "_merge_candidates", merge)

    async def go():
        loop_thread["id"] = threading.get_ident()
        return await web_memory.retrieve("model pricing", level=Freshness.RECENT, top_k=3, use_cache=False)

    out = asyncio.run(go())
    assert out.evidence
    assert seen["rank"] != loop_thread["id"]
    assert seen["merge"] != loop_thread["id"]
