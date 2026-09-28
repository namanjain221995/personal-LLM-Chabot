"""The window scan finds the question's tokens without tokenising the page.

2026-09-28. `_term_positions` yielded EVERY token of a candidate page into
Python and rejected it there: on the live corpus (24 lexical rows, 5,916,296
chars of real page text) that was 2,852,163 `re.Match.group` calls and 229 ms
of a 277 ms `_merge_candidates` — the single biggest CPU item before a Fast
answer's first token, paid on every retrieval.

`_stem` only ever strips a SUFFIX, so a stem is a PREFIX of its word and a
token can only stem to a wanted term if it STARTS with that term. The scan now
asks the regex engine for those tokens only.

What is pinned here is EQUIVALENCE, not speed: the narrowed scan must return
the same dict as the plain `_WORD` scan for every text, including the cases a
prefix match gets wrong if the token boundary is not respected.
"""
from __future__ import annotations

import pytest

from app import web_memory


def _plain_term_positions(text: str, wanted: set) -> dict:
    """The pre-2026-09-28 implementation, verbatim: the reference answer."""
    pos: dict = {t: [] for t in wanted}
    if not wanted:
        return pos
    heads = {t[:2] for t in wanted}
    for m in web_memory._WORD.finditer(text.lower()):
        w = m.group(0)
        if len(w) < 2 or w[:2] not in heads or w in web_memory._STOP:
            continue
        hit = pos.get(web_memory._stem(w))
        if hit is not None:
            hit.append(m.start())
    return pos


#: Each case is (question, page text). The page texts carry the shapes a
#: prefix scan can get wrong: a wanted stem inside a longer run, after a
#: separator that `_WORD` swallows into the preceding token, at offset 0, in a
#: version string, and as an inflection the stemmer folds.
CASES = [
    ("what is the difference between a mutex and a semaphore",
     "A mutex differs from a semaphore. The semaphores differ; semaphore-like "
     "locks and mutex.lock and my_mutex and a.mutex and 12mutex and complexmutex."),
    ("configured release engine",
     "configure configured configures configuring reconfigured release released "
     "releases releasing engine engines enginex xengine engine.yaml engine-1 "
     "engine_room release.3.14.5 1990s"),
    ("mutex", "mutex"),
    ("mutex", "amutex mutex bmutex mutex"),
    ("kv cache memory gib",
     "CLUSTER_KV_CACHE_MEMORY_GIB is the kv cache budget. kv_cache_memory and "
     "the KV-CACHE row and memory.gib and cachememory."),
    ("orchestrator streaming",
     "the orchestrator streams; orchestrator's streaming. sub-orchestrator and "
     "orchestrator.py and ORCHESTRATOR and Orchestrator-2"),
    ("who is the CEO of Nvidia right now", "the ceo of nvidia is; nvidia.com; xnvidia; ceo-2"),
    ("tls certificate pinning", "TLS certificate pinning. tls1.3 tls-1.2 certificates pinned"),
    ("a b the of s", "a b the of s nothing here"),
    ("release", ""),
]


@pytest.mark.parametrize("question,text", CASES)
def test_narrowed_scan_matches_the_plain_word_scan(question: str, text: str) -> None:
    wanted = set(web_memory._terms(question))
    assert web_memory._term_positions(text, wanted) == _plain_term_positions(text, wanted)


def test_narrowed_scan_matches_on_real_page_shapes() -> None:
    """A page built the way an extracted page reads: prose, a markdown table,
    repeated headers, separators and mixed case, at a size that exercises the
    window scan rather than the short-circuit."""
    row = "| 12 | Qwen3.6-35B-A3B-NVFP4 | 82.7 | 6.50 | 2026-03-06 |\n"
    page = (
        "Mutex and Semaphore\n\n"
        "A MUTEX is owned; a semaphore counts. See mutex.lock() and my_mutex.\n"
        + "| Rank | Model | Reasoning score | Price | Released |\n| --- | --- | --- | --- | --- |\n"
        + row * 200
        + "\nThe semaphore-like counting lock differs from the mutex in ownership.\n"
        + "Filler prose about locking and ownership. " * 400
    )
    for question in ("what is the difference between a mutex and a semaphore",
                     "what is the reasoning score of Qwen3.6-35B-A3B-NVFP4",
                     "released price rank"):
        wanted = set(web_memory._terms(question))
        clean, _lines, _starts = web_memory._collapse_lines(page, " ")
        assert web_memory._term_positions(clean, wanted) == _plain_term_positions(clean, wanted)
        # and the window the scan feeds is the same window
        assert web_memory._best_window(page, question) == _best_window_plain(page, question)


def _best_window_plain(text: str, query: str, width: int = web_memory._WINDOW_CHARS) -> str:
    """`_best_window` driven by the reference scan, so the chosen window can be
    compared rather than only the positions it is derived from."""
    real = web_memory._term_positions
    web_memory._term_positions = _plain_term_positions  # type: ignore[assignment]
    try:
        return web_memory._best_window(text, query, width)
    finally:
        web_memory._term_positions = real  # type: ignore[assignment]


def test_the_scan_does_not_tokenise_the_whole_page(monkeypatch) -> None:
    """The point of the change, and what regresses if someone puts the plain
    scan back: `_term_positions` must not walk `_WORD` over the page.

    Before 2026-09-28 it did, and every one of a 3 MB page's ~470,000 tokens
    cost a Python-level `group()`/`len()`/set-lookup to be thrown away.
    """
    wanted = set(web_memory._terms("mutex semaphore"))  # uses the real _WORD
    page = "alpha beta gamma delta epsilon zeta eta theta iota kappa " * 2000

    class Refuses:
        def finditer(self, _text):
            raise AssertionError("_term_positions tokenised the whole page with _WORD")

    monkeypatch.setattr(web_memory, "_WORD", Refuses())
    assert web_memory._term_positions(page, wanted) == {t: [] for t in wanted}
