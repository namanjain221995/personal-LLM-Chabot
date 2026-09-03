"""Sharing a link with the workspace: what may enter the shared corpus.

Since 2026-09-03 a pasted link is written to the GLOBAL web store and its
site is crawled, so the URL itself became a security boundary: a pre-signed
S3 object, a `user:pass@` URL, an internal host, an OAuth callback — there the
URL is the credential, and the body fetched through it is private to whoever
held it. These tests pin (1) the pure decision in core/urls.check_shareable,
(2) the engine's use of it — a refused link is still read for the sharer's
own conversation, never stored for everyone, never crawled, logged by class
and counted, never logged by URL — (3) the V16 provenance written on the
shared row, and (4) the closing of the crawl-dedupe oracle: the "Indexing …"
line no longer reveals whether someone else in the workspace shared the site
first.

Fetch and the model are mocked; the database is the real test PostgreSQL.
"""
import asyncio
import logging

import pytest

from app import db, metrics, web_worker
from app.config import settings
from app.core import urls
from app.core.net import FetchResult
from app.engines import crawl
from app.engines import url as url_engine
from app.engines.search import _normalize_url

# ---------------------------------------------------------------------------
# core/urls.check_shareable — the pure decision
# ---------------------------------------------------------------------------

PRESIGNED_S3 = (
    "https://bucket.s3.eu-west-1.amazonaws.com/private/board-pack.pdf"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAEXAMPLE%2F20260903%2Feu-west-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260903T100000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
)
AZURE_SAS = (
    "https://acct.blob.core.windows.net/container/offer.docx"
    "?sv=2022-11-02&se=2026-09-04T00%3A00%3A00Z&sp=r&sig=abc%2Fdef%3D"
)
GCS_SIGNED = "https://storage.googleapis.com/b/o.csv?GoogleAccessId=x&Expires=1&Signature=abc"


@pytest.mark.parametrize(
    "url",
    [
        PRESIGNED_S3,
        AZURE_SAS,
        GCS_SIGNED,
        "https://drive.example/d/abc123?share=1",
        "https://drive.example/d/abc123?sharetoken=abc",
        "https://app.example/oauth/cb?code=4%2FxyZ&state=nonce",
        "https://api.example/v1/things?apikey=k",
        "https://api.example/v1/things?id=7&api_key=k",
        "https://x.example/p?TOKEN=abc",            # case-insensitive
        "https://x.example/p?Access-Token=abc",     # `-` folds to `_`
        "https://x.example/p?token",                # bare name, no value
        "https://x.example/p?id=1&oauth_token=abc", # prefix, mixed with an ordinary param
        "https://x.example/p?key%5B%5D=abc",        # percent-encoded `key[]`
        "https://x.example/p?%74oken=abc",          # percent-encoded name
        "https://x.example/login?session=abc",
        "https://x.example/reset?password=hunter2",
        # The OAuth implicit grant puts the token in the FRAGMENT.
        "https://app.example/cb#access_token=abc&token_type=bearer",
    ],
)
def test_credential_shaped_urls_are_refused(url):
    assert urls.shareable_url(url) is None
    assert urls.check_shareable(url).reason == "credential_query"


@pytest.mark.parametrize(
    "url, reason",
    [
        ("https://user:pass@files.example/report", "userinfo"),
        ("https://user@files.example/report", "userinfo"),
        ("https://@files.example/report", "userinfo"),
        ("https://files.example:pw@evil.example/", "userinfo"),
        ("http://10.0.0.1/admin", "ip_literal"),
        ("http://8.8.8.8/", "ip_literal"),           # public, but no domain identity
        ("https://[2001:db8::1]/", "ip_literal"),
        ("http://2130706433/", "ip_literal"),        # decimal 127.0.0.1
        ("http://0x7f000001/", "ip_literal"),        # hex 127.0.0.1
        ("http://0177.0.0.1/", "ip_literal"),        # octal
        ("http://localhost/", "internal_host"),
        ("http://localhost:80/", "internal_host"),   # host wins over the (default) port
        ("http://intranet/wiki", "internal_host"),   # single label
        ("https://git.corp.local/", "internal_host"),
        ("https://nas.home.arpa/", "internal_host"),
        ("https://api.example:8443/v1", "port"),
        ("http://dev.example:3000/", "port"),
        ("https://api.example:80/", "port"),         # 80 is not https's default
        ("ftp://files.example/a", "scheme"),
        ("file:///etc/passwd", "scheme"),
        ("javascript:alert(1)", "scheme"),
        ("", "unparseable"),
        ("https://", "unparseable"),
        ("https:///path-only", "unparseable"),
        ("http://[::1", "unparseable"),              # bad IPv6 literal
        ("https://x.example:99999/", "unparseable"), # port out of range
    ],
)
def test_untrusted_authorities_are_refused_by_class(url, reason):
    decision = urls.check_shareable(url)
    assert decision.url is None
    assert decision.reason == reason
    assert reason in urls.SHARE_REFUSAL_REASONS, "every class is a closed metric label"
    assert urls.shareable_url(url) is None


@pytest.mark.parametrize(
    "url, expected, stripped",
    [
        # The ordinary case: a content-bearing query is kept exactly.
        ("https://news.example/article?id=123", "https://news.example/article?id=123", ()),
        # Tracking parameters go; the rest, in its original order and encoding, stays.
        (
            "https://news.example/article?id=123&utm_source=tw&utm_medium=x&fbclid=abc",
            "https://news.example/article?id=123",
            ("utm_source", "utm_medium", "fbclid"),
        ),
        ("https://x.example/?utm_source=a", "https://x.example/", ("utm_source",)),
        ("https://x.example/p?b=2&a=1", "https://x.example/p?b=2&a=1", ()),
        ("https://docs.example/p?q=a%20b&page=2#section-2", "https://docs.example/p?q=a%20b&page=2#section-2", ()),
        ("https://shop.example/docs/pricing", "https://shop.example/docs/pricing", ()),
        # `ref` selects content on many sites (a git ref, a catalogue number).
        ("https://git.example/repo/blob?ref=main", "https://git.example/repo/blob?ref=main", ()),
        # An explicit DEFAULT port is not a non-default port.
        ("https://api.example:443/v1", "https://api.example:443/v1", ()),
        ("http://api.example:80/v1", "http://api.example:80/v1", ()),
        # A plain fragment is not a credential.
        ("https://x.example/p#top", "https://x.example/p#top", ()),
    ],
)
def test_ordinary_links_are_kept_with_tracking_removed(url, expected, stripped):
    decision = urls.check_shareable(url)
    assert decision.url == expected
    assert decision.reason == ""
    assert decision.stripped == stripped
    assert urls.shareable_url(url) == expected


@pytest.mark.parametrize("junk", [None, 0, b"https://x.example/", "http://exa mple.com/", "https://x.example/%"])
def test_check_shareable_never_raises(junk):
    decision = urls.check_shareable(junk)  # type: ignore[arg-type]
    assert isinstance(decision, urls.ShareDecision)


# ---------------------------------------------------------------------------
# engines/url — the boundary in use
# ---------------------------------------------------------------------------


class Rec:
    def __init__(self):
        self.events = []

    async def emit(self, e, d):
        self.events.append((e, d))

    def of(self, k):
        return [d for e, d in self.events if e == k]

    def statuses(self):
        return [d["text"] for d in self.of("status")]


async def _fake_stream(messages, **kw):
    yield "token", "The page says pricing is $49 [1]."


@pytest.fixture(autouse=True)
def _sharing_on(monkeypatch):
    """Every flag on, the worker silent, the metric registry clean."""
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "web_share_crawl_enabled", True)
    monkeypatch.setattr(settings, "web_background_crawl_enabled", True)
    monkeypatch.setattr(web_worker, "kick", lambda: None)
    metrics.reset()
    yield
    metrics.reset()


def _share(monkeypatch, url: str, conversation_id: str, *, landed: str = None, user_id=None) -> Rec:
    """Run the URL engine for one pasted link with fetch + model mocked.
    `landed` is the URL the (mocked) fetch ends on after redirects."""

    async def fake_fetch(u, **kw):
        return FetchResult(landed or u, 200, "text/html", b"<h1>Pricing</h1><p>$49/mo</p>")

    monkeypatch.setattr(url_engine.net, "safe_fetch", fake_fetch)
    monkeypatch.setattr(
        url_engine.extract, "extract_readable",
        lambda ct, b, u: url_engine.extract.Extracted(title="Pricing Page", text="Pro plan is $49/mo. " * 20),
    )
    monkeypatch.setattr(url_engine.llm, "stream_chat_events", _fake_stream)
    rec = Rec()
    asyncio.run(url_engine.run_url_engine(
        "summarize this", [url], conversation_id, [], rec.emit, user_id=user_id
    ))
    return rec


def _spies(monkeypatch):
    """Refuse-path assertions: neither the global store nor the crawl queue
    may be reached. Both are patched where the engine looks them up."""
    stored, queued = [], []
    monkeypatch.setattr(db, "upsert_web_page", lambda *a, **k: stored.append((a, k)))

    async def spy_enqueue(*a, **k):
        queued.append((a, k))
        return 1

    monkeypatch.setattr(crawl, "enqueue_site_crawl", spy_enqueue)
    return stored, queued


def test_a_presigned_link_is_read_for_the_sharer_but_never_shared(monkeypatch, caplog):
    stored, queued = _spies(monkeypatch)
    caplog.set_level(logging.INFO, logger="app.engines.url")
    rec = _share(monkeypatch, PRESIGNED_S3, "c1")

    # The sharer's own conversation is served exactly as before: the page is
    # in url_documents and is the cited source of the answer.
    assert db.get_url_document_urls("c1") == {PRESIGNED_S3}
    meta = rec.of("meta")[-1]
    assert meta["route"] == "url" and meta["sources"][0]["url"] == PRESIGNED_S3
    # …and nobody else ever sees it: no global row, no crawl, no "Indexing".
    assert stored == [] and queued == []
    assert db.web_crawl_queue_counts() == {"queued": 0, "running": 0}
    assert not any("Indexing" in t for t in rec.statuses())
    assert "site_crawl" not in meta
    # The sharer is told, without the URL echoed back.
    private = [t for t in rec.statuses() if "Kept this link private" in t]
    assert private and "amazonaws" not in private[0] and "X-Amz" not in private[0]
    assert "credential" in private[0]
    # Logged by class, never by URL — the log is where a signature must not go.
    msgs = [r.getMessage() for r in caplog.records if r.name == "app.engines.url"]
    assert any("reason=credential_query" in m and "where=pasted" in m for m in msgs)
    assert not any("amazonaws" in m or "X-Amz" in m for m in msgs)
    # Counted, by class.
    assert 'knowledge_share_refused_total{reason="credential_query",where="pasted"} 1' in metrics.render()


@pytest.mark.parametrize(
    "url, reason",
    [
        ("https://user:pass@files.example/report", "userinfo"),
        ("http://8.8.8.8/status", "ip_literal"),
        ("https://api.example:8443/v1/export", "port"),
    ],
)
def test_other_refusal_classes_never_reach_the_store_or_the_queue(monkeypatch, url, reason):
    stored, queued = _spies(monkeypatch)
    rec = _share(monkeypatch, url, "c2")
    assert db.get_url_document_urls("c2") == {url}, "the sharer still gets the page"
    assert stored == [] and queued == []
    assert not any("Indexing" in t for t in rec.statuses())
    assert f'knowledge_share_refused_total{{reason="{reason}",where="pasted"}} 1' in metrics.render()


def test_a_redirect_onto_a_signed_url_is_refused_too(monkeypatch):
    """A short link that 302s to a pre-signed object is the same leak with one
    hop: the pasted URL is clean, the body is private."""
    stored, queued = _spies(monkeypatch)
    rec = _share(monkeypatch, "https://short.example/r/abc", "c3", landed=PRESIGNED_S3)
    assert db.get_url_document_urls("c3") == {"https://short.example/r/abc"}
    assert stored == [] and queued == []
    assert not any("Indexing" in t for t in rec.statuses())
    assert 'knowledge_share_refused_total{reason="credential_query",where="final"} 1' in metrics.render()


def test_an_ordinary_link_is_shared_with_provenance_and_without_tracking(monkeypatch):
    pasted = "https://news.example/article?id=123&utm_source=tw&fbclid=abc"
    rec = _share(monkeypatch, pasted, "c9", user_id=7)

    # The sharer's memory keeps the link as pasted…
    assert db.get_url_document_urls("c9") == {pasted}
    # …the shared row keeps the content-bearing query only, with V16 provenance.
    with db.connection() as con:
        row = con.execute(
            "SELECT url, canonical_url, origin, introduced_by_user_id, "
            "introduced_in_conversation_id FROM web_pages WHERE url_key = %s",
            (_normalize_url(pasted),),
        ).fetchone()
    assert row is not None
    assert row["url"] == "https://news.example/article?id=123"
    assert row["canonical_url"] == "https://news.example/article?id=123"
    assert row["origin"] == "share"
    assert row["introduced_by_user_id"] == 7
    assert row["introduced_in_conversation_id"] == "c9"
    # Crawl queued for the clean URL; the user told; meta carries THIS job.
    assert db.web_crawl_queue_counts()["queued"] == 1
    job = db.get_web_crawl(rec.of("meta")[-1]["site_crawl"][0]["job_id"])
    assert job["root_url"] == "https://news.example/article?id=123"
    assert any("Indexing news.example in the background" in t for t in rec.statuses())
    assert 'knowledge_share_refused_total' not in metrics.render()


def test_the_introducer_is_null_until_main_threads_the_user(monkeypatch):
    """main.py does not pass user_id to run_url_engine yet (its neighbours
    do). The column must then be NULL, not a guess."""
    seen = {}
    monkeypatch.setattr(db, "upsert_web_page", lambda *a, **k: seen.update(k))
    _share(monkeypatch, "https://news.example/a", "c4")
    assert seen["origin"] == "share"
    assert seen["introduced_by_user_id"] is None
    assert seen["introduced_in_conversation_id"] == "c4"
    _share(monkeypatch, "https://news.example/b", "c5", user_id=42)
    assert seen["introduced_by_user_id"] == 42 and seen["introduced_in_conversation_id"] == "c5"


def test_the_indexing_line_does_not_reveal_who_shared_the_site_first(monkeypatch):
    """THE ORACLE. The crawl queue dedups by site across every member for
    24 h. When the "Indexing …" line appeared only for a NEW job, its absence
    told a member that someone else had shared the site recently. Now both
    members see the identical line; only meta.site_crawl — a fact about the
    request's own job — differs."""
    first = _share(monkeypatch, "https://shop.example/docs/pricing", "alice-c1")
    second = _share(monkeypatch, "https://shop.example/docs/pricing", "bob-c1")
    assert db.web_crawl_queue_counts()["queued"] == 1, "the second share was deduped"

    line_a = [t for t in first.statuses() if t.startswith("Indexing")]
    line_b = [t for t in second.statuses() if t.startswith("Indexing")]
    assert line_a and line_b and line_a == line_b
    assert "shop.example" in line_a[0]
    # Only the request that created the job carries it in meta.
    assert first.of("meta")[-1]["site_crawl"][0]["status"] == "queued"
    assert "site_crawl" not in second.of("meta")[-1]


def test_a_deduped_share_is_announced_but_not_claimed(monkeypatch):
    """Same contract, isolated from the database's dedupe rule: enqueue says
    'nothing new' and the engine must still announce, but not claim, a job."""

    async def already_known(*a, **k):
        return None

    monkeypatch.setattr(crawl, "enqueue_site_crawl", already_known)
    rec = _share(monkeypatch, "https://shop.example/docs/faq", "c6")
    assert any("Indexing shop.example in the background" in t for t in rec.statuses())
    assert "site_crawl" not in rec.of("meta")[-1]


def test_with_the_crawl_feature_off_nothing_is_announced(monkeypatch):
    """A None from enqueue_site_crawl also means 'feature off'; that is not
    an indexing statement and must not be dressed as one."""
    monkeypatch.setattr(settings, "web_background_crawl_enabled", False)
    rec = _share(monkeypatch, "https://shop.example/docs/pricing", "c7")
    assert db.get_web_pages([_normalize_url("https://shop.example/docs/pricing")]), "still stored"
    assert not any("Indexing" in t for t in rec.statuses())
    assert "site_crawl" not in rec.of("meta")[-1]
