"""How results from several queries are merged into one source list.

The old `_collect_results` concatenated query by query and then head-sliced the
whole list to 10. With more than one query that silently discarded the later
ones — query 1 alone could fill the slice — so High issued six different
searches and answered from the first one. That is why "high" never read more
sources than "medium" no matter how many queries it ran.
"""
import asyncio

import pytest

from app.engines import search
from app.search.base import SearchResult, SearchUnavailableError


def result(url, title="t", snippet="s"):
    return SearchResult(title=title, url=url, snippet=snippet)


def fake_provider(monkeypatch, per_query, name="fake"):
    """per_query: {query -> [SearchResult]} or a list used for every query."""
    class P:
        def __init__(self):
            self.name = name
            self.calls = []

        async def search(self, q, max_results):
            self.calls.append(q)
            got = per_query[q] if isinstance(per_query, dict) else per_query
            if isinstance(got, Exception):
                raise got
            return got[:max_results]

    p = P()
    monkeypatch.setattr(search, "get_provider", lambda: p)
    search._cache.clear()
    return p


def collect(queries, effort="medium"):
    return asyncio.run(search._collect_results(queries, effort))


# ---------------------------------------------------------------------------
# Every query contributes
# ---------------------------------------------------------------------------


def test_later_queries_are_not_discarded_by_the_cap(monkeypatch):
    """THE BUG: query 1 filled the slice and queries 2-6 were thrown away."""
    fake_provider(monkeypatch, {
        "q1": [result(f"https://a{i}.test/p") for i in range(20)],
        "q2": [result(f"https://b{i}.test/p") for i in range(20)],
        "q3": [result(f"https://c{i}.test/p") for i in range(20)],
    })
    urls = [r.url for r in collect(["q1", "q2", "q3"], "medium")]
    assert any("a0" in u for u in urls)
    assert any(".test" in u and "b" in u.split("//")[1][0] for u in urls), urls
    assert any(u.startswith("https://c") for u in urls), "query 3 got nothing"


def test_merge_is_round_robin_so_each_angle_leads(monkeypatch):
    """Rank 1 of every query beats rank 2 of the first query."""
    fake_provider(monkeypatch, {
        "q1": [result("https://a.test/1"), result("https://a2.test/2")],
        "q2": [result("https://b.test/1"), result("https://b2.test/2")],
    })
    urls = [r.url for r in collect(["q1", "q2"], "medium")]
    assert urls[:2] == ["https://a.test/1", "https://b.test/1"]


def test_high_collects_more_than_medium_more_than_low(monkeypatch):
    many = [result(f"https://s{i}.test/p") for i in range(80)]
    fake_provider(monkeypatch, many)
    lo = len(collect(["q"], "low"))
    med = len(collect(["q"], "medium"))
    hi = len(collect(["q"], "high"))
    assert lo < med < hi
    assert hi == search.source_budget("high")


def test_low_was_not_cut_by_the_rework(monkeypatch):
    """Low read 10 sources before this change and must still read 10."""
    assert search.source_budget("low") == 10


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", [
    "https://www.a.test/page",
    "http://a.test/page",
    "https://a.test/page/",
    "https://a.test/page?utm_source=x&utm_medium=y",
    "https://a.test/page?fbclid=123",
])
def test_the_same_page_is_not_read_twice(monkeypatch, variant):
    fake_provider(monkeypatch, {
        "q1": [result("https://a.test/page")],
        "q2": [result(variant)],
    })
    assert len(collect(["q1", "q2"], "medium")) == 1


def test_genuinely_different_pages_are_both_kept(monkeypatch):
    fake_provider(monkeypatch, {
        "q1": [result("https://a.test/page?id=1")],
        "q2": [result("https://a.test/page?id=2")],
    })
    assert len(collect(["q1", "q2"], "medium")) == 2


# ---------------------------------------------------------------------------
# Domain diversity
# ---------------------------------------------------------------------------


def test_one_site_cannot_dominate_the_result_set(monkeypatch):
    """30 sources that are really 3 sites is not deep research."""
    fake_provider(monkeypatch, {
        "q": [result(f"https://spam.test/p{i}") for i in range(30)]
             + [result(f"https://other{i}.test/p") for i in range(10)],
    })
    out = collect(["q"], "high")
    spam = [r for r in out if "spam.test" in r.url]
    assert len(spam) <= search._MAX_PER_DOMAIN["high"]


def test_subdomains_count_against_the_same_site(monkeypatch):
    """blog1.spam.test and blog2.spam.test are the same publisher."""
    fake_provider(monkeypatch, {
        "q": [result(f"https://blog{i}.spam.test/p") for i in range(20)]
             + [result(f"https://other{i}.test/p") for i in range(15)],
    })
    out = collect(["q"], "high")
    spam = [r for r in out if "spam.test" in r.url]
    assert len(spam) <= search._MAX_PER_DOMAIN["high"], [r.url for r in out]


def test_capped_pages_are_used_rather_than_wasted_when_results_are_thin(monkeypatch):
    """Better a 5th page from a good site than a short answer, when the other
    engines came back empty."""
    fake_provider(monkeypatch, {
        "q": [result(f"https://only.test/p{i}") for i in range(12)],
    })
    out = collect(["q"], "high")
    # Relaxed up to the floor — not to the full target, which would be 12 pages
    # of one site masquerading as deep research.
    assert len(out) == search._MIN_SOURCES


@pytest.mark.parametrize("url,expected", [
    ("https://www.example.com/a", "example.com"),
    ("https://a.b.example.com/x", "example.com"),
    ("https://sub.example.co.uk/x", "example.co.uk"),
])
def test_domain_extraction(url, expected):
    assert search._registrable_domain(url) == expected


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------


def test_one_dead_query_does_not_sink_the_others(monkeypatch):
    """Upstream engines suspend constantly; the other angles still have answers."""
    fake_provider(monkeypatch, {
        "q1": SearchUnavailableError("suspended"),
        "q2": [result("https://b.test/1")],
    })
    assert [r.url for r in collect(["q1", "q2"], "medium")] == ["https://b.test/1"]


def test_all_queries_dead_still_raises_so_the_fallback_runs(monkeypatch):
    fake_provider(monkeypatch, {"q1": SearchUnavailableError("suspended")})
    with pytest.raises(SearchUnavailableError):
        collect(["q1"], "medium")


def test_no_results_returns_empty_not_an_error(monkeypatch):
    fake_provider(monkeypatch, {"q": []})
    assert collect(["q"], "medium") == []


# ---------------------------------------------------------------------------
# Prompt size at scale
# ---------------------------------------------------------------------------


def test_the_long_tail_is_trimmed_so_the_prompt_stays_sane():
    """30 x 8000 chars is 240k of prefill for ONE step."""
    sources = [
        search._Source(n=i + 1, title="t", url=f"https://s{i}.test", text="x" * 8000)
        for i in range(30)
    ]
    search._apply_char_tiers(sources)
    assert len(sources[0].text) == 8000, "top-ranked keeps the full budget"
    # truncate_chars appends an ellipsis, hence the small allowance.
    assert len(sources[-1].text) <= search._TIER_B_CHARS + 8
    total = sum(len(s.text) for s in sources)
    assert total < 140_000, f"{total} chars is too much prefill"


def test_high_is_never_shallower_than_medium_on_the_pages_that_matter():
    """The top sources must not be thinned just because more were added."""
    assert search._TIER_A_SOURCES >= search.source_budget("medium") // 2


def test_the_answer_prompt_asks_for_breadth_and_disagreement():
    """More sources only help if the model is told to use them."""
    system = search._answer_messages("q", [], [])[0]["content"]
    assert "FULL" in system and "DISAGREE" in system
