"""Lexical windows are memoised by page content and question terms
(2026-09-29, track retrieval-does-less, B4).

Measured on production: a Fast lookup's readback re-ran the whole retrieval
seconds after the pre-fetch retrieval of the same question, and 105-167 ms of
its 273-337 ms (n=3) was the merge re-windowing the same full-text lexical rows
(4.7 MB of page text at p50), in the single retrieval CPU slot. A re-retrieval
after the router's verdict differed from the speculative guess repeats it too.

The memo is keyed by (page id, content_hash, text length, question terms,
width, keep_lines). `_best_window` reads the question only through
`set(_terms(question))`, so the key names everything the window depends on;
these tests pin that the memo is invisible (byte-identical windows over the
web_eval fixture pages and 200 questions) and that it actually saves the work.
"""
from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path

import pytest

from app import web_index, web_memory
from app.config import settings
from app.core import extract
from app.freshness import Freshness

WEB_EVAL = Path(__file__).parent / "fixtures" / "web_eval"


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    web_memory.cache_clear()
    for name, value in dict(
        web_memory_enabled=True, knowledge_rerank=False, knowledge_evidence_cache_ttl_s=0.0,
        knowledge_window_memo_entries=512,
    ).items():
        monkeypatch.setattr(settings, name, value, raising=False)
    yield
    web_memory.cache_clear()


def _long_row(i: int, content_hash: str = "", extra: str = "") -> dict:
    head = "Kestrel methodology notes on pricing and benchmarks. " * 120
    answer = f" The kestrel reasoning score for model K-{i} is {80 + i}.4 per the leaderboard. "
    tail = "Unrelated gardening prose about hedgerows and rainfall. " * 150
    return {
        "id": 10 + i, "url": f"https://memo{i}.example/page", "title": f"Memo page {i}",
        "text": head + answer + extra + tail, "domain": f"memo{i}.example", "authority": 40,
        "fetched_at": None, "published_at": None, "modified_at": None, "source_type": "",
        "origin": "search", "content_hash": content_hash,
    }


def _counting_best_window(monkeypatch):
    calls = {"n": 0}
    real = web_memory._best_window

    def counted(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(web_memory, "_best_window", counted)
    return calls


def _retrieve_with(monkeypatch, rows, question="kestrel reasoning score leaderboard"):
    async def no_dense(query, top_k=6, site_prefix=""):
        return []

    monkeypatch.setattr(web_index, "retrieve", no_dense)
    monkeypatch.setattr(web_memory, "_lexical_candidates", lambda q, limit: [dict(r) for r in rows])
    monkeypatch.setattr(web_memory, "_page_meta", lambda urls, ids=(): {})
    result = asyncio.run(web_memory.retrieve(question, level=Freshness.RECENT, top_k=5, use_cache=False))
    return [(e.url, e.text) for e in result.evidence]


def test_a_second_retrieve_of_the_same_question_windows_nothing(monkeypatch):
    rows = [_long_row(i, content_hash=f"sha-{i}") for i in range(6)]
    calls = _counting_best_window(monkeypatch)
    first = _retrieve_with(monkeypatch, rows)
    assert calls["n"] == 6
    calls["n"] = 0
    second = _retrieve_with(monkeypatch, rows)
    assert calls["n"] == 0, f"the readback re-windowed {calls['n']} rows"
    assert second == first and any("kestrel reasoning score for model K-" in text for _url, text in first)
    # Another spelling of the same question terms is the same window.
    calls["n"] = 0
    assert _retrieve_with(monkeypatch, rows, "Leaderboard: what is the KESTREL reasoning score?") == first
    assert calls["n"] == 0


def test_a_changed_content_hash_recomputes_that_page_only(monkeypatch):
    rows = [_long_row(i, content_hash=f"sha-{i}") for i in range(4)]
    calls = _counting_best_window(monkeypatch)
    _retrieve_with(monkeypatch, rows)
    # The page was refetched and moved on: new text, new hash, same id.
    rows[2] = _long_row(2, content_hash="sha-2-v2")
    rows[2]["text"] = rows[2]["text"].replace("is 82.4", "is 91.9")
    calls["n"] = 0
    after = dict(_retrieve_with(monkeypatch, rows))
    assert calls["n"] == 1
    assert "K-2 is 91.9" in after["https://memo2.example/page"]


def test_the_same_hash_with_different_text_is_not_served_the_old_window(monkeypatch):
    """Belt and braces: a writer that stored new text without re-hashing it
    (none does today) still gets a fresh window when the length moved."""
    rows = [_long_row(0, content_hash="sha-0")]
    _retrieve_with(monkeypatch, rows)
    rows[0]["text"] = rows[0]["text"].replace("is 80.4", "is 80.4 (revised to 77.1)")
    after = dict(_retrieve_with(monkeypatch, rows))
    assert "revised to 77.1" in after["https://memo0.example/page"]


def test_rows_without_a_content_hash_are_never_memoised(monkeypatch):
    rows = [_long_row(i) for i in range(3)]
    calls = _counting_best_window(monkeypatch)
    _retrieve_with(monkeypatch, rows)
    _retrieve_with(monkeypatch, rows)
    assert calls["n"] == 6
    assert len(web_memory._window_memo) == 0


def test_the_memo_switched_off_computes_every_window(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_window_memo_entries", 0)
    rows = [_long_row(i, content_hash=f"sha-{i}") for i in range(3)]
    calls = _counting_best_window(monkeypatch)
    _retrieve_with(monkeypatch, rows)
    _retrieve_with(monkeypatch, rows)
    assert calls["n"] == 6
    assert len(web_memory._window_memo) == 0


def test_the_memo_is_bounded_and_evicts_the_least_recently_used(monkeypatch):
    monkeypatch.setattr(settings, "knowledge_window_memo_entries", 3)
    rows = [_long_row(i, content_hash=f"sha-{i}") for i in range(5)]
    terms = frozenset(web_memory._terms("kestrel score"))
    for r in rows:
        web_memory._lexical_window(r, "kestrel score", terms)
    assert len(web_memory._window_memo) == 3
    calls = _counting_best_window(monkeypatch)
    web_memory._lexical_window(rows[4], "kestrel score", terms)  # newest: kept
    assert calls["n"] == 0
    web_memory._lexical_window(rows[0], "kestrel score", terms)  # oldest: evicted
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Invisible: byte-identical windows over the web_eval pages x 200 questions
# ---------------------------------------------------------------------------


def _fixture_texts():
    texts = []
    for path in sorted(WEB_EVAL.glob("*.html")):
        ext, _links = extract.extract_readable_and_links("text/html", path.read_bytes(), f"https://x/{path.name}")
        texts.append((path.stem, ext.title or path.stem, ext.text or ""))
    # The fixture pages are short, and a page under one window is returned
    # whole whatever the question. Concatenations make pages several windows
    # long, so the question actually chooses the window.
    rng = random.Random(7)
    for k in range(8):
        picks = [texts[rng.randrange(len(texts))] for _ in range(rng.randint(4, 9))]
        texts.append((f"combo{k}", f"Combined {k}", "\n\n".join(t for _s, _t, t in picks)))
    return texts


def _questions(texts, n=200):
    vocab = sorted({w for _s, _t, t in texts for w in re.findall(r"[a-z0-9][a-z0-9.\-]+", t.lower())
                    if len(w) > 2})
    rng = random.Random(20260929)
    shapes = ["{}", "what is the {}", "{}?", "Tell me about {} please", "{} now", "latest {}"]
    out = []
    while len(out) < n:
        words = rng.sample(vocab, rng.randint(1, 5))
        base = " ".join(words)
        out.append(rng.choice(shapes).format(base))
        # Same terms, another spelling: reordered, upper-cased, stopwords.
        if len(out) < n:
            out.append(rng.choice(shapes).format(" ".join(reversed(words)).upper()))
    return out


def test_windows_are_byte_identical_with_the_memo_on_and_off():
    texts = _fixture_texts()
    questions = _questions(texts)
    assert len(questions) == 200
    long_pages = [t for _s, _t, t in texts if len(" ".join(t.split())) > web_memory._WINDOW_CHARS]
    assert len(long_pages) >= 6, "premise: pages the question has to choose a window in"
    rows = [
        {"id": i + 1, "url": f"https://eval{i}.example/{stem}", "title": title, "text": text,
         "domain": f"eval{i}.example", "authority": 40, "fetched_at": None, "published_at": None,
         "modified_at": None, "source_type": "", "origin": "search", "content_hash": f"h{i}-{len(text)}"}
        for i, (stem, title, text) in enumerate(texts)
    ]
    hits_before = web_memory.metrics._counters.get("knowledge_window_memo_total", {}).get(
        web_memory.metrics._clean({"result": "hit"}, "knowledge_window_memo_total"), 0.0)
    compared = 0
    for q in questions:
        terms = frozenset(web_memory._terms(q))
        for row in rows:
            memo = web_memory._lexical_window(row, q, terms)
            plain = web_memory._best_window(row["text"], q)
            assert memo == plain, (q, row["url"])
            compared += 1
        # The whole merge, too: memo on against memo off.
        on = web_memory._merge_candidates(q, [], rows)
        settings_value = settings.knowledge_window_memo_entries
        try:
            settings.knowledge_window_memo_entries = 0
            off = web_memory._merge_candidates(q, [], rows)
        finally:
            settings.knowledge_window_memo_entries = settings_value
        assert {k: e.text for k, e in on.items()} == {k: e.text for k, e in off.items()}, q
    hits = web_memory.metrics._counters.get("knowledge_window_memo_total", {}).get(
        web_memory.metrics._clean({"result": "hit"}, "knowledge_window_memo_total"), 0.0) - hits_before
    assert compared == 200 * len(rows)
    # Premise: the memo was actually read, including across different
    # spellings of one question (the reversed, upper-cased variants).
    assert hits >= 200 * len(rows)
