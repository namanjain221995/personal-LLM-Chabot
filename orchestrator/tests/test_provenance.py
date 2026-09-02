"""core/provenance: dates a page states, the class of source, and duplicates.

Pure functions, no I/O. These pin the three signals the research engine and
the living-knowledge layer now rank on — WHEN, WHAT KIND, and SAME REPORT? —
because a wrong answer in any of them is invisible downstream: a 2019
article would simply look fresh, a forum would look official, ten copies
would look like ten confirmations.
"""
from datetime import datetime, timezone

import pytest

from app.core import provenance as p


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expect",
    [
        ("2026-03-12", datetime(2026, 3, 12, tzinfo=timezone.utc)),
        ("2026-03-12T09:30:00Z", datetime(2026, 3, 12, 9, 30, tzinfo=timezone.utc)),
        ("2026-03-12T11:30:00+02:00", datetime(2026, 3, 12, 9, 30, tzinfo=timezone.utc)),
        ("Wed, 02 Sep 2026 18:00:00 GMT", datetime(2026, 9, 2, 18, 0, tzinfo=timezone.utc)),
        ("", None),
        ("not a date", None),
        (None, None),
    ],
)
def test_parse_date_accepts_the_shapes_pages_emit(raw, expect):
    assert p.parse_date(raw) == expect


def test_page_dates_reads_published_and_modified_from_metadata():
    pytest.importorskip("htmldate")
    html = (
        "<html><head><title>Acme names new CEO</title>"
        "<meta property='article:published_time' content='2026-03-12T09:30:00Z'>"
        "<meta property='article:modified_time' content='2026-04-01T10:00:00Z'>"
        "</head><body><article><p>" + "Acme appointed a new chief executive. " * 40
        + "</p></article></body></html>"
    )
    dates = p.page_dates(html, "https://acme.example/press/ceo")
    assert dates.published and dates.published.date().isoformat() == "2026-03-12"
    assert dates.modified and dates.modified.date().isoformat() == "2026-04-01"


def test_page_dates_never_invents_a_date_and_uses_last_modified_as_fallback():
    html = "<html><body><p>" + "No dates anywhere in this page. " * 30 + "</p></body></html>"
    bare = p.page_dates(html, "https://x.example/p")
    assert bare.published is None
    with_header = p.page_dates(
        html, "https://x.example/p", {"last-modified": "Tue, 01 Sep 2026 10:00:00 GMT"}
    )
    assert with_header.published is None, "a header is not a publication date"
    assert with_header.modified and with_header.modified.date().isoformat() == "2026-09-01"


def test_future_and_prehistoric_dates_are_rejected():
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    assert p._plausible(datetime(2031, 1, 1, tzinfo=timezone.utc), now) is None
    assert p._plausible(datetime(1970, 1, 1, tzinfo=timezone.utc), now) is None
    assert p._plausible(datetime(2026, 9, 1, tzinfo=timezone.utc), now) is not None


def test_effective_time_prefers_the_content_date_over_the_read_time():
    published = datetime(2019, 5, 1, tzinfo=timezone.utc)
    fetched = datetime(2026, 9, 2, tzinfo=timezone.utc)
    # A 2019 article fetched this morning is still a 2019 article.
    assert p.effective_time(published, None, fetched) == published
    # ...unless the page says it was updated since.
    modified = datetime(2026, 8, 30, tzinfo=timezone.utc)
    assert p.effective_time(published, modified, fetched) == modified
    # A page with no dates of its own falls back to the read time.
    assert p.effective_time(None, None, fetched) == fetched


# ---------------------------------------------------------------------------
# Source type — structural, never a named site preferred for an answer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, expect",
    [
        ("https://ministry.gov.uk/announcements/x", "official"),
        ("https://agency.nic.in/report.pdf", "official"),
        ("https://cs.stanford.edu/paper.pdf", "academic"),
        ("https://docs.vendor.example/en/latest/guide/", "docs"),
        ("https://vendor.example/documentation/api", "docs"),
        ("https://vendor.example/press-releases/2026/new-ceo", "press"),
        ("https://vendor.example/newsroom/announcement", "press"),
        ("https://www.reddit.com/r/x/comments/1", "community"),
        ("https://forum.vendor.example/t/help/12", "community"),
        ("https://x.com/someone/status/1", "social"),
        ("https://someone.medium.com/post", "blog"),
        ("https://vendor.example/blog/post", "blog"),
        ("https://en.wikipedia.org/wiki/Thing", "reference"),
        ("https://github.com/org/repo", "code"),
        ("https://paper.example/2026/05/story", "news"),
        ("https://vendor.example/whitepaper.pdf", "pdf"),
        ("https://vendor.example/", "unknown"),
    ],
)
def test_source_type_is_structural(url, expect):
    assert p.source_type(url) == expect


def test_primary_means_first_hand():
    assert p.is_primary("https://ministry.gov.uk/x", "official", 100)
    assert p.is_primary("https://docs.vendor.example/x", "docs", 40)
    assert p.is_primary("https://vendor.example/press/x", "press", 40)
    assert not p.is_primary("https://someone.medium.com/x", "blog", 15)
    assert not p.is_primary("https://forum.example/t/1", "community", 90)
    # A high cached authority (reference site) counts even without a class.
    assert p.is_primary("https://ref.example/x", "unknown", 70)


# ---------------------------------------------------------------------------
# Near-duplicates — ten syndicated copies are ONE source
# ---------------------------------------------------------------------------


_STORY = (
    "The company announced on Tuesday that its board had appointed a new chief "
    "executive, effective the first of next month, following a six-month search "
    "led by an external firm. The outgoing chief executive will remain as an "
    "adviser through the end of the year, the company said in a statement. "
) * 6


def test_identical_and_trimmed_copies_are_duplicates():
    a = p.shingles(_STORY)
    b = p.shingles("Breaking: " + _STORY)  # a copy with a different lead-in
    trimmed = p.shingles(_STORY[: len(_STORY) // 2])  # a copy that cut the tail
    assert p.near_duplicate(a, b)
    assert p.near_duplicate(a, trimmed), "containment catches the trimmed copy"
    assert p.jaccard(a, a) == 1.0


def test_different_reports_are_not_duplicates():
    other = (
        "Quarterly revenue rose twelve percent on stronger demand in the "
        "enterprise segment, the company reported, while margins narrowed "
        "because of higher component costs and currency effects. "
    ) * 6
    assert not p.near_duplicate(p.shingles(_STORY), p.shingles(other))
    assert p.shingles("too short") == frozenset()


def test_title_key_strips_the_site_suffix():
    assert p.title_key("Acme names new CEO | Reuters") == p.title_key("Acme names new CEO - AP News")
