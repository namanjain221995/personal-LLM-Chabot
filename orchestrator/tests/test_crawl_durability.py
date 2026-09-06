"""K12 + K8: an interrupted crawl resumes, and a page's "last changed" is real.

Two defects from the 2026-09-06 knowledge audit, both about state that was
being thrown away or invented.

K12 — the crawl frontier lived in two Python locals (`frontier` and
`_CrawlState.visited`), so a restart lost it. Progress appeared to survive
only because pages fresh in `web_pages` inside `web_page_ttl_s` are skipped;
that accident covers NOTHING for a link-walk crawl with no sitemap (it
restarted at the root) and nothing at all for any crawl resumed after 24h.
The tests below therefore run every durable-frontier case with `crawl._store`
stubbed out, so no page ever lands in the store: if a URL is not re-fetched,
the ONLY thing that can have prevented it is the frontier itself.

K8 — `last_changed_at` was stamped with `now` on a page's FIRST insert, and
V13's backfill set it to `fetched_at`. The column's documented meaning is
"when the content moved"; for 61% of the live corpus (1,338 of 2,208 rows,
measured 2026-09-06) it actually meant "when we first saw it".

Everything here runs against the isolated test database — no network, no live
crawl. `_fetch_page` is stubbed at the seam above `net.safe_fetch`, so the
SSRF guard, robots handling, byte caps and politeness delay are untouched.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.core import extract, robots
from app.engines import crawl
from app.engines.search import _normalize_url


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _durable_env(
    monkeypatch,
    *,
    sitemap,
    fetch_log,
    links_for=None,
    fail=(),
    cancel_at=None,
    concurrency=2,
):
    """Wire `_crawl_site` so only the frontier can prevent a re-fetch.

    `db.run_in_thread` stays REAL — the frontier writes to the real test
    database, which is the whole point. `_store` is replaced so nothing ever
    reaches `web_pages`, which removes the store-freshness shortcut that used
    to make a resume look like it worked.
    """
    stored: list = []

    async def fake_rules(url):
        return robots.RobotRules(allowed_all=True)

    async def fake_sitemap(root, rules, state):
        return list(sitemap)

    async def fake_fetch_page(url):
        fetch_log.append(url)
        if url == cancel_at:
            # A deploy, a stop click or a closed tab, landing mid-fetch.
            raise asyncio.CancelledError()
        if url in fail:
            raise RuntimeError("fetch failed")
        return (
            url,
            extract.Extracted(title="T", text="fetched body " * 40),
            list((links_for or {}).get(url, [])),
            "text/html",
        )

    def fake_store(*args, **kwargs):
        stored.append(args[0])

    monkeypatch.setattr(crawl.robots, "fetch_rules", fake_rules)
    monkeypatch.setattr(crawl, "_discover_sitemap", fake_sitemap)
    monkeypatch.setattr(crawl, "_fetch_page", fake_fetch_page)
    monkeypatch.setattr(crawl, "_store", fake_store)
    monkeypatch.setattr(crawl.settings, "web_crawl_delay_ms", 0)
    monkeypatch.setattr(crawl.settings, "web_crawl_concurrency", concurrency)
    return stored


#: `_crawl_site` treats a sitemap of fewer than 10 URLs as unusable and falls
#: back to link-walking, so every sitemap-mode case here needs at least that
#: many pages. Twelve keeps the batching (concurrency 2 -> batches of 4) easy
#: to reason about.
_SITEMAP_PAGES = 12


def _pages(root, n=_SITEMAP_PAGES):
    return [f"{root}p{i}" for i in range(n)]


def _run(root, crawl_id=None, *, max_pages=100, max_seconds=30.0):
    return asyncio.run(
        crawl._crawl_site(
            root, None, max_pages=max_pages, max_seconds=max_seconds,
            crawl_id=crawl_id,
        )
    )


def _open_crawl(root, conversation_id="conv-k12"):
    """A real web_crawls row, as both production entry points create."""
    _host, prefix = crawl._scope_of(root)
    return db.create_web_crawl(conversation_id, root, prefix), prefix


def _frontier_rows(scope_prefix):
    with db.connection() as con:
        rows = con.execute(
            "SELECT url_key, url, depth, state, outcome, failures, crawl_id "
            "FROM web_crawl_frontier WHERE scope_prefix = %s ORDER BY id",
            (scope_prefix,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# K12 — the acceptance case: kill a crawl mid-run, restart it
# ---------------------------------------------------------------------------


def test_an_interrupted_sitemap_crawl_resumes_where_it_stopped(monkeypatch):
    """The headline: run 2 fetches exactly what run 1 did not, and nothing else.

    Run 1 is capped by its page budget. Run 2 is a SEPARATE web_crawls row —
    which is what "continue crawling" actually creates — and must still find
    the queue run 1 left behind. That is why the frontier is keyed by the
    crawl's scope and not by a crawl id.
    """
    root = "https://docs.example.ai/en/"
    pages = _pages(root)

    log1: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log1)
    crawl_id_1, prefix = _open_crawl(root)
    state1, _found1, status1 = _run(root, crawl_id_1, max_pages=3)

    assert status1 == "capped", "the budget stopped it, so there is work left"
    assert 0 < len(log1) < len(pages)
    counts = db.crawl_frontier_counts(prefix)
    assert counts["total"] == len(pages)
    assert counts["visited"] == len(log1)
    assert counts["pending"] == len(pages) - len(log1)

    # --- the process dies here; nothing in memory survives ---

    log2: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log2)
    crawl_id_2, _prefix = _open_crawl(root)
    state2, _found2, status2 = _run(root, crawl_id_2)

    assert status2 == "done"
    # Not re-fetched: nothing run 1 read is read again.
    assert set(log1) & set(log2) == set()
    # Not skipped: between them the two runs covered the whole site.
    assert set(log1) | set(log2) == set(pages)
    # Not duplicated: no URL twice inside either run.
    assert len(log1) == len(set(log1))
    assert len(log2) == len(set(log2))
    assert state1.fetched + state2.fetched == len(pages)


def test_a_link_walk_crawl_resumes_mid_walk_instead_of_at_the_root(monkeypatch):
    """The case the store-freshness accident never covered at all.

    With no usable sitemap the crawler walks links from the root. Before K12
    the frontier was in memory, so a resume began at the root again and had to
    re-walk its way back to where it stopped. Here the root is fetched exactly
    once, across both runs.
    """
    root = "https://walk.example/docs/"
    kids = [f"{root}a", f"{root}b", f"{root}c"]
    grandkids = {f"{root}a": [f"{root}a1", f"{root}a2"]}
    links = {root: kids, **grandkids}

    log1: list = []
    _durable_env(monkeypatch, sitemap=[], fetch_log=log1, links_for=links)
    crawl_id_1, prefix = _open_crawl(root)
    _s1, _f1, status1 = _run(root, crawl_id_1, max_pages=1)

    assert status1 == "capped"
    assert log1 == [root]
    pending = {r["url"] for r in _frontier_rows(prefix) if r["state"] == "pending"}
    assert pending == set(kids), "the harvested links outlived the process"

    log2: list = []
    _durable_env(monkeypatch, sitemap=[], fetch_log=log2, links_for=links)
    crawl_id_2, _prefix = _open_crawl(root)
    _s2, _f2, status2 = _run(root, crawl_id_2)

    assert status2 == "done"
    assert root not in log2, "the resume restarted from the root"
    assert set(log2) == set(kids) | set(grandkids[f"{root}a"])
    assert len(log1) + len(log2) == len(set(log1) | set(log2))


def test_a_page_cut_off_mid_fetch_is_retried_not_skipped(monkeypatch):
    """An interruption may cost a repeat; it may never cost a page.

    A URL is settled AFTER it has been read, never when it is claimed. So a
    run killed mid-batch leaves that whole batch pending: the next run repeats
    at most `web_crawl_concurrency * 2` polite fetches, and cannot leave a
    hole. Marking a URL visited at claim time — which is what persisting the
    old in-memory shape would have done — turns exactly this case into a page
    that is never read at all.
    """
    root = "https://cut.example/docs/"
    pages = _pages(root)

    log1: list = []
    _durable_env(
        monkeypatch, sitemap=pages, fetch_log=log1, cancel_at=f"{root}p1",
        concurrency=2,
    )
    crawl_id_1, prefix = _open_crawl(root)
    with pytest.raises(asyncio.CancelledError):
        _run(root, crawl_id_1)

    rows = _frontier_rows(prefix)
    assert len(rows) == len(pages)
    assert all(r["state"] == "pending" for r in rows), (
        "the cancelled batch must stay pending — settling it would have "
        "recorded pages as read that were not"
    )

    log2: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log2)
    crawl_id_2, _prefix = _open_crawl(root)
    _s2, _f2, status2 = _run(root, crawl_id_2)

    assert status2 == "done"
    assert set(log2) == set(pages), "no page was skipped by the interruption"
    # The repeat is bounded by one batch, not by the whole crawl.
    assert len(set(log1) & set(log2)) <= crawl.settings.web_crawl_concurrency * 2


# ---------------------------------------------------------------------------
# K12 — campaign lifecycle: a finished crawl must not poison the next one
# ---------------------------------------------------------------------------


def test_a_finished_crawl_closes_its_campaign_so_the_site_can_be_crawled_again(
    monkeypatch,
):
    """Draining the frontier clears it.

    Without this the durability fix would be worse than the bug: every URL of
    a completed site would sit there marked 'visited' forever, and the next
    "index this site" would find nothing to do and report a crawl of zero
    pages.
    """
    root = "https://once.example/docs/"
    pages = _pages(root)

    log1: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log1)
    crawl_id_1, prefix = _open_crawl(root)
    _s1, _f1, status1 = _run(root, crawl_id_1)

    assert status1 == "done"
    assert set(log1) == set(pages)
    assert db.crawl_frontier_counts(prefix)["total"] == 0

    log2: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log2)
    crawl_id_2, _prefix = _open_crawl(root)
    _s2, _f2, status2 = _run(root, crawl_id_2)

    assert status2 == "done"
    assert set(log2) == set(pages), "a re-crawl of a finished site is not a no-op"


def test_an_abandoned_campaign_is_retired_before_a_new_crawl_seeds(monkeypatch):
    """A crawl capped and then never resumed must not block the site forever."""
    root = "https://stale.example/docs/"
    pages = _pages(root)
    _host, prefix = crawl._scope_of(root)

    db.add_crawl_frontier(
        prefix, [(_normalize_url(u), u, 0) for u in pages], None
    )
    db.mark_crawl_frontier(prefix, [_normalize_url(u) for u in pages], "fetched")
    long_ago = datetime.now(timezone.utc) - timedelta(
        seconds=crawl._FRONTIER_CAMPAIGN_MAX_AGE_S + 3600
    )
    with db.connection() as con:
        con.execute(
            "UPDATE web_crawl_frontier SET enqueued_at = %s, visited_at = %s "
            "WHERE scope_prefix = %s",
            (long_ago, long_ago, prefix),
        )

    log: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log)
    crawl_id, _prefix = _open_crawl(root)
    _s, _f, status = _run(root, crawl_id)

    assert status == "done"
    assert set(log) == set(pages), "the week-old campaign should not have counted"


def test_a_recent_campaign_is_not_retired(monkeypatch):
    """The age rule must not eat a crawl that was capped an hour ago."""
    root = "https://recent.example/docs/"
    pages = _pages(root)
    _host, prefix = crawl._scope_of(root)

    db.add_crawl_frontier(
        prefix, [(_normalize_url(u), u, 0) for u in pages], None
    )
    db.mark_crawl_frontier(prefix, [_normalize_url(pages[0])], "fetched")
    an_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    with db.connection() as con:
        con.execute(
            "UPDATE web_crawl_frontier SET enqueued_at = %s, visited_at = "
            "CASE WHEN visited_at IS NULL THEN NULL ELSE %s END "
            "WHERE scope_prefix = %s",
            (an_hour_ago, an_hour_ago, prefix),
        )

    log: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log)
    crawl_id, _prefix = _open_crawl(root)
    _s, _f, status = _run(root, crawl_id)

    assert status == "done"
    assert set(log) == set(pages[1:]), "the already-read page was re-fetched"


# ---------------------------------------------------------------------------
# K12 — failures are bounded in both directions
# ---------------------------------------------------------------------------


def test_a_failed_fetch_is_retried_by_the_next_run(monkeypatch):
    """A transient blip must not cost the page for the life of the campaign."""
    root = "https://flaky.example/docs/"
    pages = _pages(root)
    broken = pages[0]

    log1: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log1, fail={broken})
    crawl_id_1, prefix = _open_crawl(root)
    state1, _f1, _status1 = _run(root, crawl_id_1)

    assert state1.failed == 1
    row = [r for r in _frontier_rows(prefix) if r["url"] == broken][0]
    assert row["state"] == "pending" and row["failures"] == 1
    assert db.crawl_frontier_counts(prefix)["pending"] == 1, (
        "a campaign that still owes a retry is not closed"
    )

    log2: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log2)  # the blip is over
    crawl_id_2, _prefix = _open_crawl(root)
    _s2, _f2, _status2 = _run(root, crawl_id_2)

    assert log2 == [broken], "only the failed URL was retried"
    assert db.crawl_frontier_counts(prefix)["total"] == 0


def test_a_dead_url_is_retired_after_the_failure_cap():
    """And a permanently dead URL must not be re-attempted by every resume.

    Unit-level, because the bound is the interesting part: a URL is deferred
    while it has retries left and retired the moment it runs out.
    """
    scope = "dead.example/docs"
    db.add_crawl_frontier(scope, [("dead.example/docs/a", "https://dead.example/docs/a", 0)])

    assert db.defer_crawl_frontier(scope, ["dead.example/docs/a"], 2) == 0
    assert [r["url_key"] for r in db.take_crawl_frontier(scope, 5)] == [
        "dead.example/docs/a"
    ]

    assert db.defer_crawl_frontier(scope, ["dead.example/docs/a"], 2) == 1
    assert db.take_crawl_frontier(scope, 5) == []
    row = _frontier_rows(scope)[0]
    assert row["state"] == "visited" and row["outcome"] == "failed"
    assert row["failures"] == 2


# ---------------------------------------------------------------------------
# K12 — the frontier primitives
# ---------------------------------------------------------------------------


def test_re_seeding_a_campaign_never_resets_what_was_already_read():
    """Re-reading the same sitemap on a resume must be a no-op for read pages."""
    scope = "seed.example/docs"
    entries = [(f"seed.example/docs/p{i}", f"https://seed.example/docs/p{i}", 0)
               for i in range(4)]

    assert db.add_crawl_frontier(scope, entries) == 4
    db.mark_crawl_frontier(scope, [entries[0][0], entries[1][0]], "fetched")

    # The next run re-derives the identical sitemap.
    assert db.add_crawl_frontier(scope, entries) == 0
    counts = db.crawl_frontier_counts(scope)
    assert counts == {"pending": 2, "visited": 2, "total": 4}


def test_a_sitemap_listing_one_page_twice_does_not_abort_the_crawl():
    """ON CONFLICT cannot resolve two conflicting rows in ONE statement.

    A sitemap that lists the same URL twice is a site bug, not ours — but it
    would have taken the whole insert down with `command cannot affect row a
    second time`, and with it the crawl. The duplicate is dropped in Python.
    """
    scope = "dupe.example/docs"
    row = ("dupe.example/docs/a", "https://dupe.example/docs/a", 0)
    assert db.add_crawl_frontier(scope, [row, row, row]) == 1
    assert db.crawl_frontier_counts(scope)["total"] == 1


def test_an_absurdly_long_url_is_declined_rather_than_truncated():
    """A btree entry has a hard size limit, so an enormous URL cannot simply be
    stored. Truncating it would be worse than dropping it: the crawler settles
    a page by the key it computes from the URL, so a truncated key would never
    match and the row would sit pending for the life of the campaign."""
    scope = "long.example/docs"
    ok = ("long.example/docs/a", "https://long.example/docs/a", 0)
    huge_key = "long.example/docs/b?" + ("x=1&" * 800)
    assert len(huge_key.encode()) > db._MAX_FRONTIER_KEY_BYTES

    assert db.add_crawl_frontier(scope, [ok, (huge_key, "https://" + huge_key, 0)]) == 1
    assert [r["url_key"] for r in _frontier_rows(scope)] == ["long.example/docs/a"]


def test_the_frontier_cursor_hands_back_no_url_twice_within_a_run():
    """The claim query is not a claim, so ordering is what stops a repeat.

    Rows stay 'pending' until the page settles; without the (depth, id) cursor
    a deferred URL would be handed back on every single iteration and the loop
    would never end.
    """
    scope = "cursor.example/docs"
    db.add_crawl_frontier(
        scope,
        [(f"cursor.example/docs/p{i}", f"https://cursor.example/docs/p{i}", 0)
         for i in range(5)],
    )
    seen: list = []
    cursor = (-1, 0)
    while True:
        rows = db.take_crawl_frontier(scope, 2, cursor[0], cursor[1])
        if not rows:
            break
        cursor = (int(rows[-1]["depth"]), int(rows[-1]["id"]))
        seen.extend(r["url_key"] for r in rows)

    assert len(seen) == len(set(seen)) == 5
    # Nothing was settled, so a fresh run (cursor reset) sees all of them again.
    assert len(db.take_crawl_frontier(scope, 10, -1, 0)) == 5


def test_deeper_pages_are_read_after_shallower_ones():
    """Breadth-first, exactly as the in-memory list behaved."""
    scope = "bfs.example/docs"
    db.add_crawl_frontier(scope, [("bfs.example/docs/root", "https://bfs.example/docs/root", 0)])
    db.add_crawl_frontier(scope, [("bfs.example/docs/deep", "https://bfs.example/docs/deep", 2)])
    db.add_crawl_frontier(scope, [("bfs.example/docs/mid", "https://bfs.example/docs/mid", 1)])

    order = [r["url_key"] for r in db.take_crawl_frontier(scope, 10)]
    assert order == ["bfs.example/docs/root", "bfs.example/docs/mid", "bfs.example/docs/deep"]


def test_two_campaigns_on_one_host_do_not_share_a_frontier():
    """Scope, not host: /en/ and /de/ are separate crawls of the same site."""
    en, de = "multi.example/en", "multi.example/de"
    db.add_crawl_frontier(en, [("multi.example/en/a", "https://multi.example/en/a", 0)])
    db.add_crawl_frontier(de, [("multi.example/de/a", "https://multi.example/de/a", 0)])
    db.mark_crawl_frontier(en, ["multi.example/en/a"], "fetched")

    assert db.crawl_frontier_counts(en) == {"pending": 0, "visited": 1, "total": 1}
    assert db.crawl_frontier_counts(de) == {"pending": 1, "visited": 0, "total": 1}


def test_settling_is_idempotent_and_never_un_reads_a_page():
    scope = "settle.example/docs"
    db.add_crawl_frontier(scope, [("settle.example/docs/a", "https://settle.example/docs/a", 0)])
    assert db.mark_crawl_frontier(scope, ["settle.example/docs/a"], "fetched") == 1
    # Already visited: a second settle changes nothing, and cannot overwrite
    # the outcome that was recorded first.
    assert db.mark_crawl_frontier(scope, ["settle.example/docs/a"], "refused") == 0
    assert _frontier_rows(scope)[0]["outcome"] == "fetched"
    assert db.defer_crawl_frontier(scope, ["settle.example/docs/a"], 2) == 0
    assert _frontier_rows(scope)[0]["failures"] == 0


def test_a_crawl_records_which_run_read_each_url(monkeypatch):
    """`crawl_id` is provenance, and it must actually be filled in."""
    root = "https://prov.example/docs/"
    pages = _pages(root)
    log: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log)
    crawl_id, prefix = _open_crawl(root)
    # Cap it so the campaign is not closed and the rows survive to be read.
    _run(root, crawl_id, max_pages=1)
    rows = _frontier_rows(prefix)
    assert rows and all(r["crawl_id"] == crawl_id for r in rows)
    assert {r["outcome"] for r in rows if r["state"] == "visited"} == {"fetched"}


# ---------------------------------------------------------------------------
# K12 — the opportunistic path keeps its in-process queue
# ---------------------------------------------------------------------------


def test_the_post_search_expansion_writes_no_frontier_rows(monkeypatch):
    """Warming a few pages of whatever domain a search returned is not a
    campaign anyone will resume; one row per URL for each would fill the table
    with scopes nobody asked for. No crawl id, no persistence."""
    root = "https://expand.example/blog/post-1"
    pages = _pages("https://expand.example/blog/")
    _host, prefix = crawl._scope_of(root)
    log: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log)

    _s, _f, status = _run(root, None, max_pages=len(pages) + 4)

    assert status == "done" and set(log) == set(pages)
    assert db.crawl_frontier_counts(prefix)["total"] == 0
    with db.connection() as con:
        total = con.execute("SELECT count(*) AS n FROM web_crawl_frontier").fetchone()
    assert total["n"] == 0


def test_a_crawl_that_ends_exactly_on_its_budget_still_reports_done(monkeypatch):
    """Regression guard for the loop's reordering.

    The budget check now runs after the batch is taken rather than before, so
    that a run whose final batch exhausts both the frontier and the page
    budget still reports 'done'. Reporting 'capped' would tell the user to
    "continue crawling" a site with nothing left to read.
    """
    root = "https://exact.example/docs/"
    pages = _pages(root)
    log: list = []
    _durable_env(monkeypatch, sitemap=pages, fetch_log=log, concurrency=2)
    crawl_id, prefix = _open_crawl(root)

    # max_pages EQUALS the site: the last batch exhausts the budget and the
    # frontier in the same iteration.
    _s, _f, status = _run(root, crawl_id, max_pages=len(pages))

    assert status == "done"
    assert set(log) == set(pages)
    assert db.crawl_frontier_counts(prefix)["total"] == 0


# ---------------------------------------------------------------------------
# K8 — last_changed_at means "the content moved", and nothing else
# ---------------------------------------------------------------------------


def _page(url, text, **kw):
    import hashlib

    return db.upsert_web_page(
        url_key=_normalize_url(url),
        url=url,
        canonical_url=url,
        title="T",
        text=text,
        content_type="text/html",
        fetch_status=200,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        links=[],
        **kw,
    )


def _row(url):
    with db.connection() as con:
        return dict(
            con.execute(
                "SELECT first_seen_at, fetched_at, last_changed_at, fetch_count, "
                "content_hash FROM web_pages WHERE url_key = %s",
                (_normalize_url(url),),
            ).fetchone()
        )


def test_a_first_insert_claims_no_change_that_was_never_observed():
    """The K8 defect itself. A change is observed by comparing two fetches;
    a page seen once has nothing to compare against."""
    url = "https://k8.example/a"
    _page(url, "the original body " * 30)
    row = _row(url)

    assert row["last_changed_at"] is None
    assert row["fetch_count"] == 1
    assert row["first_seen_at"] == row["fetched_at"]


def test_a_page_whose_content_moves_moves_the_timestamp():
    url = "https://k8.example/b"
    _page(url, "the original body " * 30)
    assert _row(url)["last_changed_at"] is None

    result = _page(url, "a completely different body " * 30)
    row = _row(url)

    assert result["changed"] is True
    assert row["last_changed_at"] is not None
    assert row["last_changed_at"] == row["fetched_at"]
    assert row["fetch_count"] == 2


def test_a_refetch_with_identical_bytes_does_not_move_the_timestamp():
    """A page re-fetched daily with the same bytes must not look freshly
    authored every day — V13's stated reason for the column."""
    url = "https://k8.example/c"
    body = "stable body " * 40
    _page(url, body)
    _page(url, body + " and then it changed")  # a real change, to get a value
    changed_at = _row(url)["last_changed_at"]
    assert changed_at is not None

    result = _page(url, body + " and then it changed")  # identical bytes
    row = _row(url)

    assert result["changed"] is False
    assert row["last_changed_at"] == changed_at
    assert row["fetched_at"] > changed_at, "the freshness clock still advanced"
    assert row["fetch_count"] == 3


def test_a_304_does_not_move_the_timestamp():
    """The conditional-request path already got this right; it must stay right
    now that the value can legitimately be NULL."""
    url = "https://k8.example/d"
    _page(url, "body " * 60)
    before = _row(url)

    db.touch_web_page_unchanged(_normalize_url(url), etag='W/"x"')
    after = _row(url)

    assert after["last_changed_at"] == before["last_changed_at"] is None
    assert after["fetched_at"] >= before["fetched_at"]
    assert after["fetch_count"] == 2


def test_a_change_observed_after_a_304_is_still_recorded():
    url = "https://k8.example/e"
    _page(url, "first body " * 40)
    db.touch_web_page_unchanged(_normalize_url(url))
    assert _row(url)["last_changed_at"] is None

    _page(url, "second body " * 40)
    assert _row(url)["last_changed_at"] is not None


# ---------------------------------------------------------------------------
# K8 — what V26 does, and does not, do to rows that already exist
# ---------------------------------------------------------------------------


def _legacy_row(url, *, fetch_count, first_seen_offset_s=0):
    """A row in the pre-fix shape: last_changed_at == fetched_at."""
    now = datetime.now(timezone.utc)
    seen = now - timedelta(seconds=first_seen_offset_s)
    with db.connection() as con:
        con.execute(
            "INSERT INTO web_pages (url_key, url, title, text, content_type, "
            "fetch_status, content_hash, fetch_count, first_seen_at, fetched_at, "
            "last_changed_at) VALUES (%s, %s, 'T', 'body', 'text/html', 200, "
            "'h', %s, %s, %s, %s)",
            (_normalize_url(url), url, fetch_count, seen, now, now),
        )


def test_v26_clears_only_the_rows_that_provably_never_observed_a_change():
    """The predicate is a proof, not a heuristic.

    A page fetched exactly once has no second observation to compare against,
    so whatever wrote its `last_changed_at` wrote a default. A page fetched
    more than once is left alone — some of those are V13 backfill survivors
    (invented) and some genuinely changed on their most recent fetch, because
    `upsert_web_page` writes both timestamps from one `now`. The two are not
    distinguishable from the data, and "probably invented" is not a reason to
    destroy a value that may be real.
    """
    _legacy_row("https://v26.example/once", fetch_count=1)
    _legacy_row("https://v26.example/twice", fetch_count=2, first_seen_offset_s=86400)

    with db.connection() as con:
        con.execute(db._MIGRATION_V26)

    assert _row("https://v26.example/once")["last_changed_at"] is None
    twice = _row("https://v26.example/twice")
    assert twice["last_changed_at"] == twice["fetched_at"]


def test_v26_is_idempotent():
    _legacy_row("https://v26.example/idem", fetch_count=1)
    with db.connection() as con:
        con.execute(db._MIGRATION_V26)
    first = _row("https://v26.example/idem")
    assert first["last_changed_at"] is None

    with db.connection() as con:
        # Re-running matches nothing: the rows it cleared no longer satisfy
        # `last_changed_at = fetched_at`.
        assert con.execute(db._MIGRATION_V26).rowcount == 0
    assert _row("https://v26.example/idem") == first


def test_v26_leaves_a_genuinely_recorded_change_alone():
    """A real change, recorded a moment after the fetch that produced it."""
    url = "https://v26.example/real"
    _page(url, "one " * 60)
    _page(url, "two " * 60)
    with db.connection() as con:
        con.execute(
            "UPDATE web_pages SET last_changed_at = fetched_at - interval '1 hour' "
            "WHERE url_key = %s",
            (_normalize_url(url),),
        )
    before = _row(url)
    with db.connection() as con:
        con.execute(db._MIGRATION_V26)
    assert _row(url)["last_changed_at"] == before["last_changed_at"]


# ---------------------------------------------------------------------------
# Migrations: identifiers, convergence, idempotence
# ---------------------------------------------------------------------------


def test_no_migration_identifier_is_used_twice():
    """`init_schema` skips any version already recorded, so a duplicate number
    is silently skipped in production: it reports success and applies nothing.
    Identifier 21 was orphaned exactly that way once already."""
    versions = [version for version, _ddl in db._MIGRATIONS]
    assert len(versions) == len(set(versions))
    assert versions == sorted(versions)
    assert db.LATEST_SCHEMA_VERSION == max(versions)
    # This phase's own two, named explicitly: reusing either would be the
    # silent-skip failure above, not a test failure.
    assert {25, 26} <= set(versions)


def test_no_index_predicate_carries_a_version_literal():
    """V21's `WHERE extract_version < 2` stopped finding the rows it existed
    for the moment the constant moved, and V22 exists solely to undo it. The
    new work must not repeat it."""
    import re

    for version, ddl in db._MIGRATIONS:
        if version <= 21:
            continue  # restored history; V22 already replaced the offender
        for statement in re.findall(r"CREATE\s+(?:UNIQUE\s+)?INDEX.*?;", ddl, re.S | re.I):
            assert " WHERE " not in statement.upper() or "_version" not in statement, (
                f"V{version} indexes on a version predicate: {statement}"
            )


def _schema_fingerprint(con):
    """Everything a schema comparison must see: columns with their types,
    defaults and generation expressions, every index definition, and every
    constraint definition."""
    columns = con.execute(
        "SELECT table_name, column_name, data_type, is_nullable, column_default, "
        "is_generated, generation_expression FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, column_name"
    ).fetchall()
    indexes = con.execute(
        "SELECT tablename, indexname, indexdef FROM pg_indexes "
        "WHERE schemaname = 'public' ORDER BY tablename, indexname"
    ).fetchall()
    constraints = con.execute(
        "SELECT conrelid::regclass::text AS tbl, conname, "
        "pg_get_constraintdef(oid) AS def FROM pg_constraint "
        "WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
    ).fetchall()
    return (
        [tuple(dict(r).values()) for r in columns],
        [tuple(dict(r).values()) for r in indexes],
        [tuple(dict(r).values()) for r in constraints],
    )


@pytest.fixture
def schema_build(app_database, monkeypatch):
    """Build the schema from scratch in a companion database and fingerprint it.

    Deliberately NOT a database per build. `DROP DATABASE` forces an immediate
    checkpoint, and on this shared server — with several suites writing at once
    — one drop was measured at 52 s (2026-09-07). Wiping and recreating the
    `public` schema of one reused database is the same clean sheet for a
    fraction of a second, and it leaves behind exactly one clearly-named
    `*_test` database, the way conftest's own session database does.
    """
    import psycopg

    from app.config import settings

    base, _, _name = app_database.rpartition("/")
    name = "crawl_durability_test"
    dsn = f"{base}/{name}"
    with psycopg.connect(f"{base}/postgres", autocommit=True, connect_timeout=5) as admin:
        exists = admin.execute(
            "SELECT 1 AS ok FROM pg_database WHERE datname = %s", (name,)
        ).fetchone()
        if not exists:
            admin.execute(
                f'CREATE DATABASE "{name}" TEMPLATE template0 '
                "LC_COLLATE 'C' LC_CTYPE 'C' ENCODING 'UTF8'"
            )

    def build(up_to=None):
        """→ (fingerprint, applied versions). `up_to` stops the first pass at
        that migration, then a second pass upgrades — which is exactly what a
        deployed database at V24 goes through."""
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as con:
            con.execute("DROP SCHEMA public CASCADE")
            con.execute("CREATE SCHEMA public")
        monkeypatch.setattr(settings, "app_database_url", dsn)
        db.close_pool()
        try:
            if up_to is not None:
                original = db._MIGRATIONS
                monkeypatch.setattr(
                    db, "_MIGRATIONS", tuple(m for m in original if m[0] <= up_to)
                )
                db.init_schema()
                assert db.schema_version() == up_to
                monkeypatch.setattr(db, "_MIGRATIONS", original)
            db.init_schema()
            with db.connection() as con:
                fingerprint = _schema_fingerprint(con)
                versions = [
                    int(r["version"])
                    for r in con.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    ).fetchall()
                ]
        finally:
            db.close_pool()
            monkeypatch.setattr(settings, "app_database_url", app_database)
            db.close_pool()
        return fingerprint, versions

    yield build

    # Leave the (empty) database, drop its contents: see the docstring.
    with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as con:
        con.execute("DROP SCHEMA IF EXISTS public CASCADE")
        con.execute("CREATE SCHEMA public")


def test_a_fresh_install_and_an_upgrade_from_v24_converge(schema_build):
    """Both paths must land on an identical schema.

    The engagement has been bitten here before: a migration applied to
    production by code that was later reverted left the live database one
    version ahead of the source, and a column-level diff missed the index that
    migration also created. So this compares columns, indexes AND constraints,
    not just column names.
    """
    # 24 is the version production was on when this work started; the point of
    # the test is the upgrade path a deployed database actually takes.
    fresh, fresh_versions = schema_build()
    upgraded, upgraded_versions = schema_build(up_to=24)

    expected = [version for version, _ddl in db._MIGRATIONS]
    assert fresh_versions == upgraded_versions == expected
    fresh_cols, fresh_idx, fresh_cons = fresh
    up_cols, up_idx, up_cons = upgraded
    assert fresh_cols == up_cols
    assert fresh_idx == up_idx
    assert fresh_cons == up_cons


def test_repeated_initialisation_applies_nothing_twice(schema_build):
    """A migrated database must be left completely alone by another run."""
    before, versions = schema_build()
    assert versions == [version for version, _ddl in db._MIGRATIONS]

    # ...and on the database the rest of the suite is using.
    for _ in range(3):
        db.init_schema()
    with db.connection() as con:
        duplicated = con.execute(
            "SELECT version FROM schema_migrations "
            "GROUP BY version HAVING count(*) > 1"
        ).fetchall()
        total = con.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()
    assert duplicated == []
    assert total["n"] == db.LATEST_SCHEMA_VERSION

    after, versions_again = schema_build()
    assert versions_again == versions
    assert after == before


def test_the_frontier_table_is_reachable_by_the_suites_truncation(app_database):
    """Test isolation, proved rather than assumed.

    conftest truncates a hardcoded list of tables that does not name
    `web_crawl_frontier`. It survives only because the table has a real
    foreign key to `web_crawls`, and TRUNCATE ... CASCADE reaches every
    referencing table. If that FK is ever dropped, campaigns will leak between
    tests and this fails first.
    """
    with db.connection() as con:
        referencing = con.execute(
            "SELECT conrelid::regclass::text AS tbl FROM pg_constraint "
            "WHERE contype = 'f' AND confrelid = 'web_crawls'::regclass"
        ).fetchall()
    assert "web_crawl_frontier" in {r["tbl"] for r in referencing}
