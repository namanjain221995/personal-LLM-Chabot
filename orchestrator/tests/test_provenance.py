"""core/provenance: dates a page states, the class of source, and duplicates.

Pure functions, no I/O. These pin the three signals the research engine and
the living-knowledge layer now rank on — WHEN, WHAT KIND, and SAME REPORT? —
because a wrong answer in any of them is invisible downstream: a 2019
article would simply look fresh, a forum would look official, ten copies
would look like ten confirmations.

Since 2026-09-03 a fourth signal: WHO COULD HAVE WRITTEN IT. A page anyone
can create under a trusted domain must never inherit that domain's
authority or count as a first-hand source.
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
        # A hosted-blog platform is a tenant space — anyone gets a subdomain —
        # so it is community (user-generated), not blog, since 2026-09-03.
        ("https://someone.medium.com/post", "community"),
        ("https://vendor.example/blog/post", "blog"),
        ("https://en.wikipedia.org/wiki/Thing", "reference"),
        ("https://en.wikipedia.org/wiki/Talk:Thing", "community"),
        ("https://github.com/org/repo", "code"),
        ("https://paper.example/2026/05/story", "news"),
        ("https://vendor.example/whitepaper.pdf", "pdf"),
        ("https://vendor.example/", "unknown"),
    ],
)
def test_source_type_is_structural(url, expect):
    assert p.source_type(url) == expect


# ---------------------------------------------------------------------------
# User-generated content — shapes that mean "anyone can publish here"
#
# Security critique 2026-09-03: authority and "primary" were inherited from
# the registrable domain, so a page anyone can create under a trusted domain
# scored as a reference source (sites.google.com and
# techcommunity.microsoft.com both measured 70 before the fix). Each case
# below is a SHAPE; the hosts are only the best-known instance of each.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        # a host label that names a tenant space on somebody else's domain
        "https://sites.google.com/view/anyone/home",
        "https://gist.github.com/anyone/0123abcd",
        "https://answers.microsoft.com/en-us/windows/forum/all/x",
        "https://people.csail.mit.edu/anyone/",
        "https://users.rust-lang.org/t/help/1",
        "https://paste.debian.net/123456",
        # a host label that names a place where the public writes
        "https://groups.google.com/g/some-list/c/abc",
        "https://techcommunity.microsoft.com/t5/x/p/1",
        "https://discussions.apple.com/thread/255",
        "https://discuss.python.org/t/topic/1",
        "https://lists.apache.org/thread/xyz",
        # a tenant subdomain of a hosting platform
        "https://anyone.github.io/post/",
        "https://anyone.gitlab.io/",
        "https://anyone.medium.com/a-post-1",
        "https://anyone.substack.com/p/issue-1",
        "https://anyone.blogspot.com/2026/01/x.html",
        "https://anyone.blogspot.co.uk/2026/01/x.html",
        "https://anyone.wordpress.com/x",
        "https://anyone.tumblr.com/post/1",
        "https://anyone.netlify.app/",
        "https://anyone.vercel.app/docs/",
        "https://anyone.pages.dev/",
        "https://anyone.notion.site/Page-abc",
        "https://anyone.wixsite.com/mysite",
        "https://some-bucket.s3.amazonaws.com/index.html",
        "https://anyone.herokuapp.com/",
        "https://anyone.duckdns.org/",
        "https://abcd-1234.ngrok-free.app/",
        # the platform's apex publishes members too
        "https://medium.com/@anyone/a-post",
        "https://substack.com/@anyone",
        # a personal-page, handle, shared-document or list-archive path
        "https://cs.stanford.edu/~anyone/",
        "https://example.org/~anyone/paper.pdf",
        "https://docs.google.com/document/d/1AbC/edit",
        "https://docs.google.com/spreadsheets/d/1AbC/edit#gid=0",
        "https://drive.google.com/file/d/1AbC/view",
        "https://lists.example.org/pipermail/dev/2026-January/000001.html",
        # a wiki page that is a person or a conversation, not an article
        "https://en.wikipedia.org/wiki/User:Anyone",
        "https://en.wikipedia.org/wiki/User_talk:Anyone",
        "https://en.wikipedia.org/wiki/Talk:Some_article",
        "https://en.wikipedia.org/wiki/Draft:Some_article",
        "https://en.wikipedia.org/wiki/Wikipedia:Village_pump_(policy)",
        "https://en.wikipedia.org/wiki/User%3AAnyone",
        "https://en.wikipedia.org/w/index.php?title=Talk:Some_article&action=history",
        # a forum thread wherever it is hosted — a reference domain, a .gov
        "https://support.google.com/chrome/thread/123",
        "https://vendor.example/forum/topic/1",
        "https://forum.agency.gov/t/1",
        "https://agency.gov/forum/topic/1",
        "https://agency.gov/~anyone/",
        # the classes that were already community or social
        "https://www.reddit.com/r/x/comments/1",
        "https://stackoverflow.com/questions/1",
        "https://news.ycombinator.com/item?id=1",
        "https://x.com/anyone/status/1",
        "https://www.linkedin.com/company/anyone",
        "https://www.youtube.com/@anyone",
    ],
)
def test_ugc_shapes_mean_anyone_can_publish(url):
    assert p.is_ugc_host(url), url
    assert p.authority_cap(url) == p.UGC_AUTHORITY_CAP == 15


@pytest.mark.parametrize(
    "url",
    [
        # only the domain's owner publishes here
        "https://ministry.gov.uk/announcements/x",
        "https://cs.stanford.edu/paper.pdf",
        "https://docs.python.org/3/library/os.html",
        "https://docs.vendor.example/en/latest/guide/",
        "https://en.wikipedia.org/wiki/Some_article",
        "https://github.com/org/repo",
        "https://vendor.example/press-releases/2026/new-ceo",
        "https://vendor.example/",
        # a first-party blog host is not a hosted-blog platform
        "https://blog.vendor.example/post",
        # the project, not the .com tenants
        "https://wordpress.org/download/",
        # two labels: a site CALLED people/talk, not a tenant on somebody's domain
        "https://people.com/celebrity/x",
        "https://talk.example/",
        # the "user" in a path is a manual section, not a person
        "https://docs.vendor.example/user/guide/",
        # a company profile on a finance site is editorial
        "https://finance.example/profile/company/ACME",
        # a government host: no individual can obtain a hostname, and a
        # Q&A-shaped path is the agency's own FAQ
        "https://answers.usa.gov/x",
        "https://agency.gov/questions/1",
        "https://sites.agency.gov/x",
        "https://people.agency.gov/directory",
        # a project's own manual on its docs host — the tenant IS the project
        "https://project.readthedocs.io/en/latest/",
        "",
        "not a url",
    ],
)
def test_first_party_shapes_are_not_ugc(url):
    assert not p.is_ugc_host(url), url
    assert p.authority_cap(url) is None


def test_ugc_pages_are_classed_community_whatever_their_domain_says():
    # Each of these used to take the class of its trusted domain or path.
    assert p.source_type("https://sites.google.com/view/x/docs/") == "community"  # was docs
    assert p.source_type("https://someone.github.io/docs/") == "community"  # was docs
    assert p.source_type("https://gist.github.com/x/1") == "community"  # was code
    assert p.source_type("https://en.wikipedia.org/wiki/Talk:Thing") == "community"  # was reference
    assert p.source_type("https://cs.stanford.edu/~x/paper.pdf") == "community"  # was academic
    assert p.source_type("https://agency.gov/forum/topic/1") == "community"  # was official
    assert p.source_type("https://techcommunity.microsoft.com/t5/x/p/1") == "community"  # was unknown
    assert p.source_type("https://someone.medium.com/post") == "community"  # was blog
    # The finer classes survive where the shape is first-party.
    assert p.source_type("https://en.wikipedia.org/wiki/Thing") == "reference"
    assert p.source_type("https://cs.stanford.edu/paper.pdf") == "academic"
    assert p.source_type("https://agency.gov/questions/1") == "official"
    # Social is user-generated too, but keeps its own label.
    assert p.source_type("https://x.com/someone/status/1") == "social"
    assert p.source_type("https://www.youtube.com/@channel") == "social"


def test_ugc_cap_and_primary_floor_match_the_store_scale():
    """The cap is web_memory.AUTHORITY_LOW and the primary floor is
    web_memory.AUTHORITY_REFERENCE. core/ cannot import app/ (web_memory
    imports this module), so the numbers are duplicated and pinned here."""
    from app import web_memory

    assert p.UGC_AUTHORITY_CAP == web_memory.AUTHORITY_LOW
    assert p.PRIMARY_AUTHORITY_MIN == web_memory.AUTHORITY_REFERENCE


def test_the_store_applies_the_cap_to_a_trusted_domains_tenants():
    """The critique's reproduction: before the cap, sites.google.com and
    techcommunity.microsoft.com scored 70 through the google.com and
    microsoft.com reference entries (measured 2026-09-03)."""
    from app import web_memory

    assert web_memory.authority_of("https://sites.google.com/view/anyone") == 15
    assert web_memory.authority_of("https://techcommunity.microsoft.com/t5/x/p/1") == 15
    assert web_memory.authority_of("https://en.wikipedia.org/wiki/User:Anyone") == 15
    # ...and the owner's own pages on the same domains are untouched.
    assert web_memory.authority_of("https://en.wikipedia.org/wiki/Thing") == 70
    assert web_memory.authority_of("https://www.microsoft.com/en-us/x") == 70


# ---------------------------------------------------------------------------
# Primary — first-hand AND credentialed, and never user-generated
# ---------------------------------------------------------------------------


def test_primary_means_first_hand_and_credentialed():
    # BOTH: the class must publish first-hand AND either the suffix is the
    # credential (official, academic) or the host is reference-grade (70).
    assert p.is_primary("https://ministry.gov.uk/x", "official", 100)
    assert p.is_primary("https://cs.stanford.edu/paper.pdf", "academic", 80)
    assert p.is_primary("https://docs.vendor.example/x", "docs", 70)
    assert p.is_primary("https://vendor.example/press/x", "press", 70)
    # A /docs/ or /press/ path on a neutral (40) host is anyone's project
    # site or announcement page; both were primary before 2026-09-03.
    assert not p.is_primary("https://docs.vendor.example/x", "docs", 40)
    assert not p.is_primary("https://vendor.example/press/x", "press", 40)
    # Authority alone is not a class: a trusted domain's page of unknown
    # kind is a report until its shape says otherwise (was primary before).
    assert not p.is_primary("https://ref.example/x", "unknown", 70)
    assert not p.is_primary("https://ref.example/x", "reference", 100)
    assert not p.is_primary("https://someone.medium.com/x", "blog", 15)
    assert not p.is_primary("https://forum.example/t/1", "community", 90)


@pytest.mark.parametrize(
    "url, kind",
    [
        # Rows stored before the rule: class and authority were inherited
        # from the trusted domain, so the URL itself has to refuse.
        ("https://sites.google.com/view/anyone/docs/", "docs"),
        ("https://gist.github.com/anyone/1", "docs"),
        ("https://anyone.github.io/docs/", "docs"),
        ("https://answers.microsoft.com/en-us/x", "docs"),
        ("https://techcommunity.microsoft.com/t5/x/p/1", "press"),
        ("https://docs.google.com/document/d/1AbC/edit", "docs"),
        ("https://en.wikipedia.org/wiki/User:Anyone", "official"),
        ("https://cs.stanford.edu/~anyone/notes/", "academic"),
        ("https://agency.gov/forum/topic/1", "official"),
        ("https://anyone.substack.com/p/x", "press"),
    ],
)
def test_a_ugc_page_is_never_primary_whatever_it_was_stored_as(url, kind):
    assert not p.is_primary(url, kind, 100), url


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
