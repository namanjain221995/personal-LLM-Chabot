"""Search accuracy: does the answer survive the trip to the prompt?

Findings S1-S6 and K2 from `docs/fast-web-research/FINDINGS.md`. Every case
here is derived from `tests/fixtures/web_eval/` — synthetic HTML whose expected
values were transcribed BY HAND from the markup (`cases.json`), not produced by
running this code. No database, no network, no model call: the search provider
and the LLM are fakes.

The failure these guard against is the dangerous one. The page is fetched, it
is cited in the panel, and the answer row has been sliced off the text handed
to the model — so the model correctly reports non-coverage and the user reads a
fully-cited "GPT-5.2 is not ranked".
"""
import asyncio
import json
from pathlib import Path

import pytest

from app import web_memory
from app.config import settings
from app.core import extract
from app.engines import search
from app.search.base import SearchResult

FIXTURES = Path(__file__).parent / "fixtures" / "web_eval"
CASES = {c["id"]: c for c in json.loads((FIXTURES / "cases.json").read_text())["cases"]}

QUESTION = "What is GPT-5.2's reasoning score on the BenchLM leaderboard?"


def page_text(name: str) -> str:
    """The fixture as the pipeline sees it — through the real extractor."""
    ext, _links = extract.extract_readable_and_links(
        "text/html", (FIXTURES / name).read_bytes(), f"https://benchlm.test/{name}"
    )
    return ext.text


# ---------------------------------------------------------------------------
# S1 + K2 — the answer row must reach the prompt at BOTH character tiers
# ---------------------------------------------------------------------------


def test_the_measured_position_of_the_answer_row_has_not_moved():
    """The premise of every case below (cases.json: exact-score-long-page).

    If the fixture is ever edited this is the assertion that says so, rather
    than the accuracy tests quietly passing for the wrong reason.
    """
    text = page_text("leaderboard_long.html")
    assert text.index("| 12 | GPT-5.2 | 82.7") > 19_000
    assert 20_000 < len(text) < 21_000


@pytest.mark.parametrize("budget", [8000, 2500])
def test_head_truncation_loses_the_answer_at_both_tiers(budget):
    """Baseline, kept as the control: this is what the code did until S1."""
    kept = extract.truncate_chars(page_text("leaderboard_long.html"), budget)
    assert "82.7" not in kept


@pytest.mark.parametrize("budget", [8000, 2500])
def test_query_centred_selection_keeps_the_answer_at_both_tiers(budget):
    """S1/C1 acceptance. 8,000 is Tier A, 2,500 is Tier B — on `max` effort
    Tier B is 50 of the 60 sources read."""
    kept = search._select_text(page_text("leaderboard_long.html"), QUESTION, budget)
    assert len(kept) <= budget
    assert "GPT-5.2" in kept
    assert "82.7" in kept


def test_the_selection_spends_no_more_of_the_prompt_than_the_head_slice_did():
    """Relevance may reorder the budget; it may not enlarge it."""
    text = page_text("leaderboard_long.html")
    for budget in (2500, 8000):
        assert len(search._select_text(text, QUESTION, budget)) <= budget


def test_a_page_shorter_than_the_budget_loses_nothing():
    """Selection must not become a second truncation for ordinary pages."""
    text = page_text("leaderboard.html")
    kept = search._select_text(text, QUESTION, 8000)
    assert len(text) < 8000
    assert "82.7" in kept and "76.1" in kept and "93.4" in kept
    assert "[…]" not in kept, "a whole page needs no elision marker"


def test_no_question_falls_back_to_the_head_slice():
    """A caller with nothing to centre on keeps exactly today's behaviour."""
    text = page_text("leaderboard_long.html")
    assert search._select_text(text, "", 2500) == extract.truncate_chars(text, 2500)


def test_a_window_opening_inside_a_table_carries_its_header(monkeypatch):
    """K2/K3: a window that starts at row 137 of a 200-row table is a grid of
    bare numbers — which column is the score and which is the price?"""
    rows = "\n".join(
        f"| {i} | Model-{i} | {50 + i % 40}.{i % 10} | {i % 9}.00 | 2026-03-01 |"
        for i in range(1, 201)
    )
    text = (
        "Preamble about the benchmark methodology. " * 40
        + "\n| Rank | Model | Reasoning score | Cost | Evaluated |\n"
        + "|---|---|---|---|---|\n"
        + rows
        + "\n| 201 | Quarry-9 | 71.4 | 3.00 | 2026-03-09 |\n"
    )
    got = web_memory.select_passages(text, "what is Quarry-9 scored", 1200)
    assert "Quarry-9" in got and "71.4" in got
    assert "| Rank | Model | Reasoning score |" in got, got


def test_the_repeated_header_no_longer_wins_the_whole_window():
    """K2/C1 as the knowledge inspector measured it: the header row contains
    every ordinary query term, so unweighted term-count scoring returned rows
    1-50 for a query whose answer is at rank 137."""
    rows = "\n".join(f"| {i} | Model-{i} | {90 - i * 0.1:.1f} |" for i in range(1, 201))
    text = "| Rank | Model | Reasoning score |\n|---|---|---|\n" + rows
    got = web_memory.select_passages(text, "Model-137 reasoning score", 1000)
    assert "Model-137" in got, got


# ---------------------------------------------------------------------------
# S4 — version and variant tokens must survive tokenisation
# ---------------------------------------------------------------------------


def test_content_words_keep_a_version_token_whole():
    """`_WORD` + `len(w) > 1` reduced GPT-5.2 to ['gpt'] and 3.14.5 to ['14'],
    so a GPT-5 page and a GPT-5.2 page were lexically identical."""
    assert web_memory._content_words("GPT-5.2 elo") == ["gpt-5.2", "elo"]
    assert "3.14.5" in web_memory._content_words("what changed in 3.14.5")
    assert web_memory._terms("GPT-5.2") == ["gpt-5.2"]


def test_the_stemmer_is_held_off_tokens_carrying_digits_or_separators():
    """Suffix stripping is an English-inflection rule; a version is not
    English. '3.14.5' must not lose its trailing '5'."""
    assert web_memory._stem("3.14.5") == "3.14.5"
    assert web_memory._stem("oc-h1") == "oc-h1"
    assert web_memory._stem("1990s") == "1990s"
    # …and ordinary words still stem, exactly as test_knowledge_unified asserts.
    assert web_memory._stem("business") == "busines"
    assert web_memory._terms("configured engines") == ["configur", "engin"]


def test_a_variant_query_ranks_the_variant_page_first():
    """cases.json: followup-exact-variant. GPT-5 and GPT-5.2 must not collapse
    to the same retrieval key."""
    five = "GPT-5 scores 83.2 on the BenchLM reasoning leaderboard, ranked 11th."
    five_two = "GPT-5.2 scores 82.7 on the BenchLM reasoning leaderboard, ranked 12th."

    def overlap(query, text):
        q = set(web_memory._terms(query))
        return len(q & set(web_memory._terms(text)))

    assert overlap("GPT-5.2 score", five_two) > overlap("GPT-5.2 score", five)
    assert overlap("GPT-5 score", five) > overlap("GPT-5 score", five_two)


def test_the_window_picks_the_variant_row_not_its_sibling():
    """The conflation failure named in cases.json: 83.2 is the adjacent row."""
    got = search._select_text(page_text("leaderboard_long.html"), QUESTION, 2500)
    row = [ln for ln in got.splitlines() if "GPT-5.2" in ln and "|" in ln]
    assert row, got[-500:]
    assert "82.7" in row[0]


# ---------------------------------------------------------------------------
# S2 — a terse follow-up must be resolved for every consumer, not just SearXNG
# ---------------------------------------------------------------------------


def test_a_bare_pronoun_followup_tokenises_to_nothing_useful():
    """The premise: this is what the reranker and the store were handed."""
    assert web_memory._content_words("and its score?") == ["score"]
    # The ledger's own measurement: "but" is not in the stop list.
    assert web_memory._content_words("but what is its score?") == ["but", "score"]


@pytest.mark.parametrize(
    "message, query",
    [
        ("and its score?", "GPT-5.2 reasoning score BenchLM leaderboard"),
        ("what about 5.2?", "GPT-5.2 BenchLM reasoning score"),
    ],
)
def test_the_resolved_question_carries_the_referent(message, query):
    """S2 acceptance: the string that goes to rerank and to stored retrieval."""
    resolved = search.resolve_question(message, [query])
    assert "GPT-5.2" in resolved
    assert message in resolved, "the user's own words are kept"


def test_a_question_that_stands_on_its_own_is_left_alone():
    """Resolution must not rewrite an ordinary question into the rewriter's
    phrasing — the user asked what they asked."""
    assert search.resolve_question(QUESTION, ["something else entirely"]) == QUESTION


def test_resolution_is_a_no_op_when_the_rewrite_added_no_referent():
    assert search.resolve_question("and its score?", ["its score"]) == "and its score?"
    assert search.resolve_question("and its score?", []) == "and its score?"


def test_the_resolved_question_reaches_rerank_the_store_and_the_prompt(monkeypatch):
    """The whole point of S2: `rewrite_queries` output used to stop at
    `_collect_results`. Records what each downstream consumer is handed."""
    history = CASES["followup-bare-pronoun"]["history"]
    seen = {}
    results = [SearchResult(title="BenchLM", url="https://benchlm.test/x", snippet="s")]

    async def fake_rewrite(message, hist, effort="medium"):
        return ["GPT-5.2 reasoning score BenchLM leaderboard"]

    async def fake_collect(queries, effort="medium", emit=None, categories="", degraded=None):
        return results

    async def fake_rerank(message, res, target):
        seen["rerank"] = message
        return res

    async def fake_fetch(res, message="", **kw):
        seen["fetch"] = message
        return [
            search._Source(
                n=1, title="BenchLM", url=res[0].url, text=page_text("leaderboard.html")
            )
        ]

    async def fake_memory(message, sources, budget=3):
        seen["memory"] = message
        return sources

    async def fake_stream(messages, **kwargs):
        seen["prompt"] = "\n".join(m["content"] for m in messages)
        for chunk in ("GPT-5.2 scores 82.7 [1].",):
            yield "token", chunk

    emitted = []

    async def emit(kind, data):
        emitted.append((kind, data))

    monkeypatch.setattr(search, "rewrite_queries", fake_rewrite)
    monkeypatch.setattr(search, "_collect_results", fake_collect)
    monkeypatch.setattr(search, "_rerank_results", fake_rerank)
    monkeypatch.setattr(search, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(search, "_memory_sources", fake_memory)
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(search.llm, "stream_chat_events", fake_stream)

    asyncio.run(search.run_search_engine("and its score?", history, emit, "think"))

    for consumer in ("rerank", "fetch", "memory"):
        assert "GPT-5.2" in seen[consumer], f"{consumer} got {seen[consumer]!r}"
    assert "Question: and its score? (GPT-5.2" in seen["prompt"]
    # The user's literal words stay in the conversation turns.
    assert "Which models are ranked on the BenchLM reasoning leaderboard?" in seen["prompt"]


# ---------------------------------------------------------------------------
# S3 — absence must be distinguishable from "not present"
# ---------------------------------------------------------------------------


def test_a_missing_entity_is_reported_as_a_coverage_gap():
    """cases.json: coverage-gap-not-absence. no_answer.html is methodology
    prose that names no model and carries no score."""
    src = search._Source(n=1, title="BenchLM methodology", url="https://b.test/m",
                         text=page_text("no_answer.html"))
    gap = search._coverage_gap("Is GPT-5.2 ranked on the BenchLM leaderboard?", [src])
    assert "gpt-5.2" in gap


def test_a_covered_entity_reports_no_gap():
    src = search._Source(n=1, title="BenchLM", url="https://b.test/l",
                         text=page_text("leaderboard.html"))
    assert search._coverage_gap(QUESTION, [src]) == []


def test_the_system_prompt_forbids_the_fabricated_negative():
    """S3 acceptance. The forbidden claims are listed in cases.json."""
    src = search._Source(n=1, title="BenchLM methodology", url="https://b.test/m",
                         text=page_text("no_answer.html"))
    system = search._answer_messages(
        "Is GPT-5.2 ranked on the BenchLM leaderboard?", [src], []
    )[0]["content"]
    assert "COVERAGE CHECK" in system
    assert "gpt-5.2" in system
    assert "DO NOT COVER" in system
    # The three claims cases.json lists as forbidden.
    assert "not ranked" in system and "not listed" in system
    assert "do not exist" in system
    # …and it must not order a blanket refusal: over-hedging an answerable
    # question is a different wrong answer, not a fix.
    assert "If the answer depends on one of them" in system


def test_no_coverage_note_when_every_term_is_covered():
    src = search._Source(n=1, title="BenchLM", url="https://b.test/l",
                         text=page_text("leaderboard.html"))
    system = search._answer_messages(QUESTION, [src], [])[0]["content"]
    assert "COVERAGE CHECK" not in system


def test_the_coverage_gap_is_recorded_in_meta(monkeypatch):
    """The honest signal has to leave the process, not just the prompt."""
    seen = {}

    async def fake_rewrite(message, hist, effort="medium"):
        return [message]

    async def fake_collect(queries, effort="medium", emit=None, categories="", degraded=None):
        return [SearchResult(title="m", url="https://b.test/m", snippet="s")]

    async def fake_rerank(message, res, target):
        return res

    async def fake_fetch(res, message="", **kw):
        return [search._Source(n=1, title="BenchLM methodology", url=res[0].url,
                               text=page_text("no_answer.html"))]

    async def fake_memory(message, sources, budget=3):
        return sources

    async def fake_stream(messages, **kwargs):
        yield "token", "The sources retrieved do not cover it."

    async def emit(kind, data):
        if kind == "meta":
            seen["meta"] = data

    monkeypatch.setattr(search, "rewrite_queries", fake_rewrite)
    monkeypatch.setattr(search, "_collect_results", fake_collect)
    monkeypatch.setattr(search, "_rerank_results", fake_rerank)
    monkeypatch.setattr(search, "_fetch_sources", fake_fetch)
    monkeypatch.setattr(search, "_memory_sources", fake_memory)
    monkeypatch.setattr(search, "_spawn", lambda coro: coro.close())
    monkeypatch.setattr(search.llm, "stream_chat_events", fake_stream)

    asyncio.run(
        search.run_search_engine(
            "Is GPT-5.2 ranked on the BenchLM leaderboard?", [], emit, "think"
        )
    )
    assert "gpt-5.2" in seen["meta"]["coverage_gap"]


# ---------------------------------------------------------------------------
# S5 — fetched, read and used are three different things
# ---------------------------------------------------------------------------


def test_a_failed_fetch_is_marked_not_rendered_as_a_read_page(monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("timeout")

    monkeypatch.setattr(search.net, "safe_fetch", boom)
    src = asyncio.run(
        search._fetch_source(
            1, SearchResult(title="T", url="https://x.test/p", snippet="a blurb")
        )
    )
    assert src.from_snippet is True
    assert _SNIPPET_IN(search._context_block([src]))


def _SNIPPET_IN(block: str) -> bool:
    return "SEARCH SNIPPET ONLY" in block


def test_a_read_page_carries_no_snippet_marker():
    src = search._Source(n=1, title="T", url="https://x.test/p", text="real body")
    assert not _SNIPPET_IN(search._context_block([src]))


def test_meta_separates_read_from_snippet_and_marks_what_was_cited():
    sources = [
        search._Source(n=1, title="A", url="https://a.test/1", text="body"),
        search._Source(n=2, title="B", url="https://b.test/2", text="blurb",
                       from_snippet=True),
        search._Source(n=3, title="C", url="https://c.test/3", text="body",
                       from_store=True),
    ]
    rows = search._meta_sources(sources, "As [1] and [3] show, the score is 82.7.")
    assert [r["read"] for r in rows] == [True, False, True]
    assert [r["cited"] for r in rows] == [True, False, True]
    assert [r["from_store"] for r in rows] == [False, False, True]
    # The panel's existing keys are untouched.
    assert rows[0]["domain"] == "a.test" and rows[0]["n"] == 1


def test_the_prompt_tells_the_model_a_snippet_is_not_evidence():
    system = search._answer_messages("q", [], [])[0]["content"]
    assert "SEARCH SNIPPET ONLY" in system


# ---------------------------------------------------------------------------
# S6 — a degraded search must not look like a thorough one
# ---------------------------------------------------------------------------


def _provider(monkeypatch, results, unresponsive=None, fail=()):
    from app.search import base

    class P:
        name = "fake"

        def __init__(self):
            self.unresponsive = dict(unresponsive or {})

        async def search(self, q, max_results, categories=""):
            if q in fail:
                raise base.SearchUnavailableError("engine down")
            return results

    p = P()
    monkeypatch.setattr(search, "get_provider", lambda: p)
    return p


def test_unresponsive_engines_are_reported_and_not_cached(monkeypatch):
    """`unresponsive_engines` had never been read. The last real query before
    the audit ran with wikipedia, duckduckgo and yandex all timed out."""
    search._cache.clear()
    results = [SearchResult(title="t", url="https://a.test/1", snippet="s")]
    _provider(monkeypatch, results, unresponsive={"q1": ["wikipedia", "yandex"]})
    degraded = {}
    got = asyncio.run(search._collect_results(["q1"], "think", None, "", degraded))
    assert got == results
    assert degraded["engines"] == ["wikipedia", "yandex"]
    assert search._cache == {}, "a degraded result must not be cached for 900 s"


def test_a_healthy_search_still_caches_and_reports_nothing(monkeypatch):
    search._cache.clear()
    results = [SearchResult(title="t", url="https://a.test/1", snippet="s")]
    _provider(monkeypatch, results)
    degraded = {}
    asyncio.run(search._collect_results(["q1"], "think", None, "", degraded))
    assert degraded == {}
    assert search._cache, "a clean result is still cached"


def test_a_partly_failed_fan_out_is_counted(monkeypatch):
    search._cache.clear()
    results = [SearchResult(title="t", url="https://a.test/1", snippet="s")]
    _provider(monkeypatch, results, fail=("q2",))
    degraded = {}
    got = asyncio.run(search._collect_results(["q1", "q2"], "think", None, "", degraded))
    assert got  # one live query is still an answer
    assert degraded["failed_queries"] == 1 and degraded["queries"] == 2


def test_the_degraded_status_line_names_engines_and_never_the_query():
    note = search._degraded_note(
        {"engines": ["wikipedia", "yandex"], "failed_queries": 1, "queries": 3}
    )
    assert "wikipedia" in note and "1 of 3" in note
    assert "?" not in note  # no question text smuggled into a status/log line


def _searxng_returning(monkeypatch, payload):
    """Drive the real provider against a canned JSON body — no socket."""
    from app.search import searxng

    class Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return payload

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None):
            return Resp()

    monkeypatch.setattr(searxng.httpx, "AsyncClient", Client)
    return searxng.SearxngProvider("https://searxng.invalid")


def test_searxng_records_unresponsive_engines_without_the_reason_text(monkeypatch):
    """The reason string is free text from an upstream; only the name is kept."""
    provider = _searxng_returning(
        monkeypatch,
        {
            "results": [{"url": "https://a.test/1", "title": "A", "content": "s"}],
            "unresponsive_engines": [["wikipedia", "timeout"], ["yandex", "CAPTCHA"]],
        },
    )
    got = asyncio.run(provider.search("q", 5))
    assert [r.url for r in got] == ["https://a.test/1"]
    assert provider.unresponsive == {"q": ["wikipedia", "yandex"]}
    assert "timeout" not in str(provider.unresponsive)


def test_searxng_reports_nothing_when_every_engine_answered(monkeypatch):
    provider = _searxng_returning(
        monkeypatch,
        {"results": [{"url": "https://a.test/1", "title": "A", "content": "s"}]},
    )
    asyncio.run(provider.search("q", 5))
    assert provider.unresponsive == {}


def test_searxng_tolerates_a_bare_string_engine_list(monkeypatch):
    """Older builds send names, not [name, reason] pairs."""
    provider = _searxng_returning(
        monkeypatch, {"results": [], "unresponsive_engines": ["mwmbl", "", None]}
    )
    asyncio.run(provider.search("q", 5))
    assert provider.unresponsive == {"q": ["mwmbl"]}


# ---------------------------------------------------------------------------
# The end-to-end shape: the fixture question, through the real selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fixture", ["leaderboard.html", "leaderboard_long.html", "leaderboard_cards.html"]
)
def test_the_answer_reaches_the_prompt_for_every_rendering(monkeypatch, fixture):
    """cases.json: exact-score-table / -long-page / -cards. Same facts, three
    renderings; the text handed to the model must contain 82.7 and not present
    83.2 (GPT-5's row) as the answer."""
    monkeypatch.setattr(settings, "search_source_char_budget", 8000)
    sources = [
        search._Source(n=i + 1, title="BenchLM", url=f"https://b.test/{i}",
                       text=page_text(fixture))
        for i in range(12)
    ]
    search._apply_char_tiers(sources, QUESTION)
    block = search._context_block(sources)
    assert "82.7" in block
    # Tier B (source 11+, cut to 2,500 chars) must keep it too — that tier is
    # 50 of 60 sources on `max` effort.
    assert "82.7" in sources[-1].text, sources[-1].text[:300]


# ---------------------------------------------------------------------------
# CLOSED (2026-09-07) — per-page retrieval diversity
#
# Everything above is about what happens to ONE page's text on its way to the
# prompt. This section is about which pages get there at all. The budget
# defect described below is FIXED; the within-page chunk-selection defect that
# was suspected behind it was measured against the real embedder and does not
# exist (see "SETTLED" at the end of this comment).
#
# THE MECHANISM. `web_index.retrieve` promises "at most ONE chunk per URL …
# the index over-fetches and groups so `top_k` means distinct pages". It keeps
# that promise by pulling `limit(max(top_k * 6, 24))` raw chunks and grouping
# afterwards. The budget is FIXED and the grouping happens after the cut, so
# the nearest-first scan spends it with no idea which page a chunk came from.
# A page holding more chunks than the budget can therefore take all of it, and
# "six distinct pages" becomes one — silently, because the caller gets a
# well-formed list that is simply short.
#
# WHY IT IS LIVE, NOT THEORETICAL (live corpus, read-only, 2026-09-07):
#   * 2,209 servable pages, 19,895 chunks, mean 9.0 per page — but 118 pages
#     (5.3%) hold >= 36, the entire budget at the top_k of 6 that
#     `crawl.site_hits_for` passes. 200 pages (9.1%) hold >= 24, the floor.
#   * The ten largest pages hold 12% of every chunk in the index; seven of
#     them sit at the 256-chunk cap.
#   * The page this was first noticed on is real and still stored:
#     www.cbinsights.com/research-unicorn-companies, 65 chunks.
#
# WHY "NEAR-IDENTICAL" IS RIGHT EVEN THOUGH THE DEDUPER CANNOT SEE IT. Those
# 65 chunks are 0% near-duplicate by `provenance.shingles`, so
# `web_memory._collapse_duplicates` will never fold them — correctly, since
# the company rows genuinely differ. They are near-identical to an EMBEDDING:
# 64 of the 65 are the same pipe table, and since CHUNKER_VERSION 2
# (`_carry_table_header`, added 2026-09-06 for finding K3) every one of them
# opens with the same 30-word header. The change that made a table chunk
# interpretable is the same change that made 64 of them look alike.
#
# WHERE IT HURTS MOST. `web_memory.retrieve` is hybrid, so its PostgreSQL half
# still surfaces other pages when the question's literal words are on them.
# `crawl.site_hits_for` has no lexical half at all — it renders
# `web_index.retrieve` straight into a cited answer — so on a crawled site
# with one oversized page, site Q&A answers from that page alone.
#
# NOT PROPOSED AS A FIX: any per-domain or per-source bonus. The remedy is a
# cap on how much of the over-fetch a SINGLE PAGE may occupy — a diversity
# constraint, not a judgement about whose page it is.
#
# SETTLED 2026-09-07 — the "within-page chunk selection" defect does not exist.
#
# For a day this file carried a second, strict-xfail finding: that with the
# budget fixed and all six pages returned, the chunk chosen FROM each sector
# page was the filler tail rather than the one carrying the figure. Its only
# evidence was `toy_embedder` below — which the fixture itself calls
# "deliberately NOT a similarity model" — so it was measured against the live
# Qwen3-Embedding-0.6B before anything was traded for it. All numbers below are
# squared L2 (what LanceDB returns as `_distance`) unless marked plain L2, and
# every run reproduced byte-identically.
#
#   1. THE PRODUCT PATH, END TO END. Same six pages, same production
#      `index_pending` / `retrieve(top_k=6)`, real embedder: 6 distinct pages,
#      and 2 of the 5 figures survive — so the assertion the xfail carried
#      (`>= 2`) PASSES on the real model. What the xfail recorded was the toy
#      reversing the two pages (robotics, climate) that the real embedder
#      decides in the figure's favour by 0.0022 and 0.0034 plain L2.
#
#   2. THE QUERY, NOT THE CHUNK, IS WHAT DECIDED IT. `BREADTH_QUESTION` is a
#      bag of six nouns, and the filler sentence on these pages is a near
#      verbatim restatement of it, so the embedder ranking the restatement
#      first is the embedder being right. Ask what a caller actually passes —
#      both callers hand `retrieve` the user's own question, `site_hits_for`
#      via `question` and `web_memory.retrieve` via `query` — and the result
#      inverts: "How much funding did unicorn startups raise in each
#      industry?" returns the figure chunk from all 5 sector pages, 5/5, with
#      the tracker demoted from first place to last (0.652 vs 0.384).
#
#   3. CONTROLS. Rewrite the sector page with ordinary varied prose instead of
#      one sentence repeated 40x and the figure chunk wins 5/5 under BOTH
#      phrasings, whether the figure sits in the head chunk (0.967-0.995 vs
#      the filler's 1.049-1.074 plain L2, keyword phrasing) or in the tail
#      chunk (1.011-1.051 vs 1.020-1.061). Position does not decide it.
#      Neither does length: trimming the figure chunk to the filler's 1,167
#      chars leaves the real question at 5/5 and does not rescue the keyword
#      soup (2/5 -> 0/5), so the effect is the query, not long-chunk dilution.
#
#   4. THERE WAS NO "FIRST CHUNK" BUG TO FIX EITHER. The proposed lever —
#      "keep the best-scoring chunk of a page rather than the first" — is
#      already what `web_index.retrieve` does: candidates arrive nearest-first
#      and the keep list is re-sorted by score and truncated. That invariant is
#      now pinned by `test_the_chunk_kept_for_a_page_is_its_nearest_one`
#      instead of being assumed.
#
#   5. AND THE OTHER LEVER BUYS NOTHING. Raising `max_chunks_per_page` from 1
#      to 2 was recorded as the fix that "makes this test pass at the cost of
#      halving the sources". It does not: `out` is still cut to `top_k` by
#      score, so the second chunk of each page is the one that gets cut, and
#      the figures are exactly the chunks that lose. Measured at top_k=6 on
#      this corpus, per_page 1 -> 2 -> 3:
#
#          toy  embedder: 6 -> 5 -> 4 distinct pages, 0/5 figures throughout
#          real embedder: 6 -> 4 -> 3 distinct pages, 2/5 -> 1/5 -> 1/5 figures
#
#      It costs diversity under both and buys evidence under neither. There is
#      no trade here to weigh; the knob is simply worse.
#
# SO: `max_chunks_per_page` stays at 1 and the diversity won above is not
# traded away. What the xfail described was a fixture artifact twice over — a
# bag-of-words embedder, and a page whose 78% boilerplate restates the query.
# ---------------------------------------------------------------------------

import hashlib  # noqa: E402
import math  # noqa: E402
import os  # noqa: E402
import urllib.request  # noqa: E402

from app import db, llm, web_index  # noqa: E402

#: The axes the toy embedder projects onto. Bag-of-words over these terms,
#: L2-normalised. Deliberately NOT a similarity model: what is under test is
#: how a fixed candidate budget is SPENT, so the scores have to be arithmetic
#: anyone can check by hand rather than the output of a service.
_VOCAB = (
    "unicorn valuation billion company startup investors industry country "
    "healthcare robotics fintech climate defense raised funding"
).split()


def _toy_vector(text: str):
    counts = [float(text.lower().count(w)) for w in _VOCAB]
    norm = math.sqrt(sum(c * c for c in counts)) or 1.0
    return [c / norm for c in counts]


@pytest.fixture()
def toy_embedder(monkeypatch):
    async def embed_texts(texts, **_kw):
        return [_toy_vector(t) for t in texts]

    async def embed_query(text, **_kw):
        return _toy_vector(text)

    monkeypatch.setattr(llm, "embed_texts", embed_texts)
    monkeypatch.setattr(llm, "embed_query", embed_query)


#: Transcribed in SHAPE (not content) from the live CB Insights tracker: the
#: header the chunker now repeats into every chunk, then rows that differ only
#: in their names and numbers.
_TABLE_HEADER = (
    "| Company | Valuation ($B) | Date Joined | Country | City | Industry | "
    "Select Investors |\n|---|---|---|---|---|---|---|\n"
)
_ROW_SECTORS = ["Healthcare", "Robotics", "Fintech", "Climate", "Defense"]

#: The question a person asks of a corpus like this one: broad, and answerable
#: only by several sources at once. Every term is on the tracker's header row,
#: which is why the tracker is a legitimate top hit — the complaint is not
#: that it ranks first, it is that it ranks first SIXTY TIMES.
BREADTH_QUESTION = "unicorn company valuation billion industry investors"

#: Each sector page states one figure that appears nowhere else in the corpus.
_SECTORS = {
    "healthcare": "Healthcare unicorn startups raised 12.4 billion dollars.",
    "robotics": "Robotics unicorn startups raised 7.1 billion dollars.",
    "fintech": "Fintech unicorn startups raised 9.8 billion dollars.",
    "climate": "Climate unicorn startups raised 4.2 billion dollars.",
    "defense": "Defense unicorn startups raised 6.6 billion dollars.",
}


def _tracker_page(rows: int = 4000) -> str:
    body = "".join(
        f"| Unicorn-{i:04d} | ${1 + i % 40} billion | 1/{1 + i % 28}/2026 | "
        f"United States | City-{i % 50} | {_ROW_SECTORS[i % 5]} | "
        f"Investors-{i % 60} |\n"
        for i in range(rows)
    )
    return (
        "The Complete List Of Unicorn Companies. A unicorn company is a "
        "private startup with a valuation over $1 billion.\n" + _TABLE_HEADER + body
    )


def _sector_page(sector: str, answer: str) -> str:
    return (
        f"{sector} unicorn companies in 2026. {answer}\n"
        + f"The {sector} sector's unicorn startups and their investors, by "
        f"valuation in billion dollar terms. " * 40
    )


def _store_page(url: str, text: str, title: str) -> int:
    return int(
        db.upsert_web_page(
            url_key=url.replace("https://", ""), url=url, canonical_url=url,
            title=title, text=text, content_type="text/html", fetch_status=200,
            content_hash=hashlib.sha1(text.encode("utf-8")).hexdigest(),
            origin="crawl",
        )["id"]
    )


def _seed_crowded_corpus() -> dict:
    """One oversized tracker page and five ordinary pages, all indexed through
    the production indexer."""
    pages = {
        "tracker": _store_page(
            "https://tracker.example/unicorn-companies", _tracker_page(), "Unicorns"
        )
    }
    for sector, answer in _SECTORS.items():
        pages[sector] = _store_page(
            f"https://{sector}.example/unicorns", _sector_page(sector, answer), sector
        )
    written = asyncio.run(web_index.index_pending(limit=50))
    assert written > 0, "nothing was indexed; the assertions below prove nothing"
    return pages


def _pages_returned(hits, pages: dict) -> list:
    by_id = {v: k for k, v in pages.items()}
    return [by_id[int(h["page_id"])] for h in hits]


def test_the_oversized_page_really_does_outnumber_the_over_fetch_budget(toy_embedder):
    """The premise, as arithmetic rather than assumption.

    If the tracker ever stopped exceeding `max(top_k * 6, 24)`, the xfails
    below would start passing for a reason that has nothing to do with
    diversity — which is the quiet way a regression test stops testing.
    """
    pages = _seed_crowded_corpus()
    _conn, table, _meta = web_index._open()
    per_page: dict = {}
    for row in table.search().limit(10_000).to_list():
        per_page[int(row["page_id"])] = per_page.get(int(row["page_id"]), 0) + 1

    tracker = per_page[pages["tracker"]]
    assert tracker > max(6 * 6, 24), (
        f"the tracker holds {tracker} chunks, which no longer exceeds the "
        "over-fetch budget this section is about"
    )
    # …and nothing else is oversized, so the budget has only one claimant.
    assert all(per_page[pages[s]] <= 4 for s in _SECTORS)


def test_one_oversized_page_must_not_take_every_slot_in_dense_recall(toy_embedder):
    """`top_k` is documented as "distinct pages". With a tracker page in the
    corpus it means one page, whatever the caller asked for."""
    pages = _seed_crowded_corpus()
    got = _pages_returned(
        asyncio.run(web_index.retrieve(BREADTH_QUESTION, top_k=6)), pages
    )
    assert len(set(got)) >= 3, (
        f"dense recall returned {len(set(got))} distinct page(s): {got}. The "
        "over-fetch was spent entirely on one page's chunks before the "
        "per-URL grouping ever ran."
    )


def test_the_crowding_costs_evidence_not_merely_a_source_count(toy_embedder):
    """What the user actually loses — sources are the means, evidence is the end.

    Five pages each state one figure stated nowhere else. The question is a
    breadth question — it is about the industries — so a good answer needs
    several of them. When the tracker took every slot, not one of the five
    figures was in the retrieved text, and the model was grounded on a table of
    company names and left to supply the numbers itself. That is the
    fabrication shape this whole file exists to prevent, arrived at from the
    retrieval end instead of the truncation end.

    WHAT THIS CAN AND CANNOT ASSERT. Until 2026-09-07 this ran the whole
    property — "at least two of the five figures come back" — under
    `toy_embedder`, and failed as a strict xfail. It was the wrong instrument
    for the second half of the claim: a bag-of-words projection cannot rank a
    stated fact above boilerplate when the query is itself a bag of the same
    generic nouns, and this file's own comment calls it "deliberately NOT a
    similarity model". Any fixture shape that lets the toy prefer the figure
    chunk pushes its distances past `web_index.MAX_DISTANCE`, so `retrieve`
    returns nothing at all — the toy cannot be made to answer this question.

    So the halves are asserted where each can be honest. Here, offline: the
    figures are IN the index and every page gets a slot, which is what makes
    any remaining loss a RANKING loss rather than an indexing or budget one.
    The ranking half runs against the model the product actually uses, at the
    end of this file (`..._real_embedder_returns_the_chunk_carrying_the_figure`)
    — 5 of 5 figures, measured. Neither half is a strict xfail any more,
    because neither is open.
    """
    pages = _seed_crowded_corpus()

    _conn, table, _meta = web_index._open()
    indexed = "\n".join(row["text"] for row in table.search().limit(100_000).to_list())
    missing = [s for s, answer in _SECTORS.items() if answer not in indexed]
    assert not missing, (
        f"the chunker never put {missing} into the index, so no amount of "
        "ranking could return those figures"
    )

    hits = asyncio.run(web_index.retrieve(BREADTH_QUESTION, top_k=6))
    got = _pages_returned(hits, pages)
    assert len(set(got)) == len(pages), (
        f"dense recall reached {len(set(got))} of {len(pages)} pages: {got}. "
        "Each of the five figures is stated on exactly one page, so a page "
        "that never gets a slot is a figure the model has to invent."
    )


def test_the_chunk_kept_for_a_page_is_its_nearest_one(toy_embedder):
    """The within-page half of the promise, as an invariant rather than a hope.

    `retrieve` returns ONE chunk per page, so which chunk it keeps decides
    whether the page's evidence reaches the prompt at all. The guarantee the
    product can make is that the kept chunk is the page's NEAREST chunk to the
    query — not its first, not the one the ANN scan happened to surface first
    in a later round. Whether the nearest chunk is also the most useful one is
    the embedding model's job, and was measured separately (section comment,
    "SETTLED"): on ordinary prose the real embedder puts the fact-bearing chunk
    first 5/5, head or tail.

    Checked by recomputing every chunk's distance from the stored vectors, so
    it fails if a future change ever keeps a chunk by arrival order.
    """
    _seed_crowded_corpus()
    hits = asyncio.run(web_index.retrieve(BREADTH_QUESTION, top_k=6))
    assert hits, "nothing retrieved; the assertion below would prove nothing"

    query_vector = _toy_vector(BREADTH_QUESTION)
    _conn, table, _meta = web_index._open()
    by_page: dict = {}
    for row in table.search().limit(100_000).to_list():
        by_page.setdefault(int(row["page_id"]), []).append(row)

    for hit in hits:
        chunks = by_page[int(hit["page_id"])]
        # LanceDB's `l2` is the SQUARED euclidean distance — the same number
        # `_distance` reports, which is what `MAX_DISTANCE` is calibrated in.
        scored = sorted(
            (
                (sum((a - b) ** 2 for a, b in zip(row["vector"], query_vector)), row)
                for row in chunks
            ),
            key=lambda pair: pair[0],
        )
        nearest_distance, nearest = scored[0]
        assert hit["text"] == nearest["text"], (
            f"{hit['url']} has {len(chunks)} chunk(s); retrieve kept the one "
            f"at distance {hit['score']:.5f} but its nearest is "
            f"{nearest_distance:.5f}. One chunk per page only carries the "
            f"page's evidence if it is the page's best chunk."
        )
        assert hit["score"] == pytest.approx(nearest_distance, abs=1e-5)


def test_the_crowding_is_a_budget_bug_not_a_ranking_one(toy_embedder):
    """The control that names the cause, and the evidence for the fix.

    Raise ONLY the raw candidate budget and every page comes back: no change
    to scoring, no per-domain rule, nothing that privileges one source over
    another. The ranking was never wrong — the budget ran out.

    It is also why the fix cannot be "over-fetch more". A page may hold 256
    chunks (the cap), and seven live pages do, so any fixed global budget is
    swampable by a large enough page. The budget has to be spent per page.
    """
    pages = _seed_crowded_corpus()
    # Until 2026-09-07 this pinned the crowded-out state (`== 1`) as the
    # premise for the fix below. The fix landed, so asserting the defect still
    # happens would now fail for the best possible reason. What the control is
    # actually for -- that the RANKING was never wrong -- is the `roomy` half,
    # which is unchanged and still the evidence that no scoring was touched.
    roomy = _pages_returned(
        asyncio.run(web_index.retrieve(BREADTH_QUESTION, top_k=200)), pages
    )
    assert len(set(roomy)) == len(pages), (
        f"with a budget larger than the tracker, dense recall finds "
        f"{len(set(roomy))} of {len(pages)} pages: {roomy}"
    )


# ---------------------------------------------------------------------------
# The same corpus against the REAL embedder — opt in, never in CI
#
# This is the instrument the "SETTLED" note above was written from, kept
# runnable so the next person does not have to rebuild it to check the claim:
#
#     EMBED_EVAL_BASE_URL=http://127.0.0.1:8003/v1 \
#         pytest tests/test_search_accuracy_eval.py -k real_embedder -s
#
# Unset by default. Everything else in this file is offline by construction and
# stays that way; a measurement that needs a GPU sidecar cannot be a gate.
# ---------------------------------------------------------------------------

#: The question phrasing that matters: BOTH callers of `web_index.retrieve`
#: pass the user's own words — `crawl.site_hits_for(question)` and
#: `web_memory.retrieve(query)`. Nothing in production hands it the noun bag
#: that `BREADTH_QUESTION` is.
NATURAL_BREADTH_QUESTION = (
    "How much funding did unicorn startups raise in each industry?"
)

REAL_EMBED_BASE_URL = os.environ.get("EMBED_EVAL_BASE_URL", "").strip()
REAL_EMBED_MODEL = os.environ.get("EMBED_EVAL_MODEL", "Qwen/Qwen3-Embedding-0.6B")


def _embed_over_http(texts):
    """One OpenAI-compatible /embeddings call, vectors in input order."""
    request = urllib.request.Request(
        REAL_EMBED_BASE_URL.rstrip("/") + "/embeddings",
        data=json.dumps({"model": REAL_EMBED_MODEL, "input": list(texts)}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.load(response)
    rows = sorted(payload["data"], key=lambda row: row["index"])
    return [row["embedding"] for row in rows]


@pytest.fixture()
def real_embedder(monkeypatch):
    if not REAL_EMBED_BASE_URL:
        pytest.skip("set EMBED_EVAL_BASE_URL to run against the real embedder")

    async def embed_texts(texts, **_kw):
        return _embed_over_http(texts)

    async def embed_query(text, *, instruction=None, **_kw):
        # Qwen3-Embedding is asymmetric: documents are indexed plain and only
        # the query carries the instruction, exactly as `retrieve` calls it.
        return _embed_over_http([(instruction or "") + text])[0]

    monkeypatch.setattr(llm, "embed_texts", embed_texts)
    monkeypatch.setattr(llm, "embed_query", embed_query)


def test_the_real_embedder_returns_the_chunk_carrying_the_figure(real_embedder):
    """The measurement that closed the within-page finding.

    Asked as a person asks it, every sector page gives up its figure and the
    tracker — first by a wide margin under the noun bag — falls to last. Run
    2026-09-07 against Qwen3-Embedding-0.6B, squared L2, reproducible to the
    digit across runs:

        fintech    0.57177 FIGURE(9.8)    healthcare 0.62288 FIGURE(12.4)
        defense    0.61304 FIGURE(6.6)    climate    0.62645 FIGURE(4.2)
        robotics   0.62244 FIGURE(7.1)    tracker    0.65186
    """
    pages = _seed_crowded_corpus()
    hits = asyncio.run(web_index.retrieve(NATURAL_BREADTH_QUESTION, top_k=6))
    got = _pages_returned(hits, pages)
    assert len(set(got)) == len(pages), f"expected all six pages, got {got}"

    retrieved = "\n".join(h.get("text", "") for h in hits)
    found = [s for s, answer in _SECTORS.items() if answer.split()[-3] in retrieved]
    assert len(found) == len(_SECTORS), (
        f"only {len(found)} of the five per-sector figures survived retrieval "
        f"({found}) under the real embedder. Returned: {got}"
    )
