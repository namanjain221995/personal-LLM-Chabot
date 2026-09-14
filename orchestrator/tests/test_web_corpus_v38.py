"""V38 (2026-09-14): the web corpus's hot reads made independent of its size.

What is pinned here, each against the measured cost it removes (dbperf2 track
pg-web-memory, production -c settings, a production-shaped synthetic corpus):

* the lexical stage ranks at most `_LEXICAL_RANK_BUDGET` pages per pass
  (it ranked every matching page: 352 ms for one question's OR pass, 3.9 s at
  10x), choosing them by how many question terms they carry;
* `web_corpus_state` is maintained by the database at commit, so the Fast
  turn's vocabulary fingerprint and the /metrics counts stop scanning
  web_pages (22.9 ms and 16.2 ms at 10x);
* `_bump_retrieval` writes `web_page_demand`, never the indexed page row
  (108-167 KB of WAL and GIN pending-list flushes up to 6.9 s per bump);
* `_page_meta`'s url lookup has an index (a Seq Scan before).
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from app import db, living_knowledge as lk, web_memory, web_worker
from app.config import settings


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    with db.connection() as con:
        con.execute("TRUNCATE web_pages RESTART IDENTITY CASCADE")
    web_memory.cache_clear()
    lk._page_vocabulary.reset()
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    yield
    lk._page_vocabulary.reset()


def _insert(key, title, text, *, domain=None, indexed=True, con=None):
    when = _now() - timedelta(days=1)
    domain = domain or f"{key}.example"
    sql = """INSERT INTO web_pages
               (url_key, url, title, text, fetched_at, first_seen_at, domain, content_hash, indexed_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"""
    params = (key, f"https://{domain}/{key}", title, text, when, when, domain,
              hashlib.sha256(text.encode()).hexdigest(), when if indexed else None)
    if con is not None:
        return int(con.execute(sql, params).fetchone()["id"])
    with db.connection() as c:
        return int(c.execute(sql, params).fetchone()["id"])


def _state():
    with db.connection() as con:
        row = con.execute("SELECT pages, generation FROM web_corpus_state WHERE id = 1").fetchone()
    return int(row["pages"]), int(row["generation"])


# ---------------------------------------------------------------------------
# Lexical stage: bounded ranking
# ---------------------------------------------------------------------------


def test_the_rank_budget_prefers_pages_carrying_more_question_terms_over_raw_frequency(monkeypatch):
    """Twenty-five pages repeat ONE question word hundreds of times (ts_rank_cd
    scores them highest); two pages carry three of the four words once. With
    every matching page ranked, the frequency pages filled all 24 slots and
    the two pages that are about the question never reached the scorer."""
    monkeypatch.setattr(web_memory, "_LEXICAL_RANK_BUDGET", 24)
    for i in range(25):
        _insert(f"freq{i}", f"Archive {i}", ("quokka " * 400) + " unrelated filler text", domain=f"f{i}.example")
    wanted = {
        _insert("about1", "Field notes", "The quokka colony on the island shelters under saltbush scrub.", domain="a.example"),
        _insert("about2", "Survey", "A quokka count across the island found saltbush habitat recovering.", domain="b.example"),
    }
    rows = web_memory._lexical_candidates("quokka island saltbush wombat", 24)
    ids = {int(r["id"]) for r in rows}
    assert wanted <= ids, sorted(ids)
    assert len(rows) == 24


def test_within_the_budget_every_matching_page_is_ranked_exactly(monkeypatch):
    """The exact path: a match set no larger than the budget is ranked by the
    same ts_rank_cd(…, 32) over the same query as before, so the order and
    the scores are unchanged."""
    monkeypatch.setattr(web_memory, "_LEXICAL_RANK_BUDGET", 150)
    for i in range(12):
        _insert(f"p{i}", f"Page {i}", ("kestrel " * (i + 1)) + "harrier", domain=f"d{i}.example")
    rows = web_memory._lexical_candidates("kestrel harrier osprey", 10)
    with db.connection() as con:
        expected = con.execute(
            """SELECT id, ts_rank_cd(search_tsv, websearch_to_tsquery('english', %s), 32) AS rank
                 FROM web_pages WHERE search_tsv @@ websearch_to_tsquery('english', %s)
                ORDER BY rank DESC, id LIMIT 10""",
            ("kestrel or harrier or osprey", "kestrel or harrier or osprey"),
        ).fetchall()
    assert [round(float(r["rank"]), 6) for r in rows] == [round(float(r["rank"]), 6) for r in expected]
    assert {int(r["id"]) for r in rows} == {int(r["id"]) for r in expected}


def test_a_quarantined_or_empty_page_never_enters_the_budget(monkeypatch):
    monkeypatch.setattr(web_memory, "_LEXICAL_RANK_BUDGET", 2)
    bad = _insert("bad", "Pangolin pangolin pangolin", "pangolin scales " * 50)
    empty = _insert("empty", "Pangolin", "")
    good = _insert("good", "Notes", "a pangolin was seen once")
    with db.connection() as con:
        con.execute("UPDATE web_pages SET quarantined_at = now() WHERE id = %s", (bad,))
    ids = [int(r["id"]) for r in web_memory._lexical_candidates("pangolin", 5)]
    assert ids == [good], (ids, bad, empty)


# ---------------------------------------------------------------------------
# Corpus generation maintained by the database
# ---------------------------------------------------------------------------


def test_the_state_row_counts_pages_through_insert_delete_and_truncate():
    assert _state()[0] == 0
    a = _insert("a", "A", "alpha text")
    _insert("b", "B", "beta text")
    assert _state()[0] == 2
    with db.connection() as con:
        con.execute("DELETE FROM web_pages WHERE id = %s", (a,))
    assert _state()[0] == 1
    before = _state()[1]
    with db.connection() as con:
        con.execute("TRUNCATE web_pages RESTART IDENTITY CASCADE")
    pages, generation = _state()
    assert pages == 0 and generation > before


def test_the_generation_moves_on_what_the_vocabulary_hashes_and_not_on_demand_or_freshness():
    url = "https://docs.example/page"
    first = db.upsert_web_page("docs.example/page", url, url, "Title", "one two three", "text/html", 200, "h1")
    page = first["id"]
    g0 = _state()[1]

    # Not a vocabulary change: a 304, a quarantine, a demand bump.
    db.touch_web_page_unchanged("docs.example/page")
    db.set_web_page_quarantine([page], quarantined=True)
    db.set_web_page_quarantine([page], quarantined=False)
    web_memory._bump_retrieval([page])
    assert _state()[1] == g0

    # Each of these is.
    with db.connection() as con:
        con.execute("UPDATE web_pages SET indexed_at = now() WHERE id = %s", (page,))
    g1 = _state()[1]
    assert g1 > g0
    with db.connection() as con:
        con.execute("UPDATE web_pages SET title = 'Other title' WHERE id = %s", (page,))
    g2 = _state()[1]
    assert g2 > g1
    db.upsert_web_page("docs.example/page", url, url, "Other title", "four five six seven", "text/html", 200, "h2")
    assert _state()[1] > g2


def test_one_transaction_moves_the_generation_once():
    g0 = _state()[1]
    with db.connection() as con:
        for i in range(5):
            _insert(f"bulk{i}", f"Bulk {i}", "bulk text", con=con)
        con.execute("UPDATE web_pages SET indexed_at = NULL")
    pages, generation = _state()
    assert pages == 5 and generation == g0 + 1


def test_no_reader_sees_the_new_generation_before_the_change_commits():
    """The bump is a DEFERRED trigger: it runs at commit. A generation visible
    before its rows would let a vocabulary be rebuilt from the old rows and
    then trusted under the new generation."""
    g0 = _state()[1]
    dsn = settings.app_database_url
    with psycopg.connect(dsn) as writer:
        writer.execute(
            "INSERT INTO web_pages (url_key, url, title, text, fetched_at, first_seen_at) "
            "VALUES ('pending', 'https://p.example/', 'P', 'pending text', now(), now())"
        )
        assert _state() == (0, g0)
        writer.commit()
    assert _state() == (1, g0 + 1)


def test_concurrent_writers_to_different_pages_both_commit():
    """Both writers take the state row at commit, last: no deadlock, both
    changes counted."""
    a = _insert("ca", "A", "alpha")
    b = _insert("cb", "B", "beta")
    g0 = _state()[1]
    dsn = settings.app_database_url
    barrier = threading.Barrier(2)
    errors = []

    def write(first, second, title):
        try:
            with psycopg.connect(dsn) as con:
                con.execute("UPDATE web_pages SET title = %s WHERE id = %s", (title, first))
                barrier.wait(timeout=10)
                con.execute("INSERT INTO web_pages (url_key, url, title, text, fetched_at, first_seen_at) "
                            "VALUES (%s, %s, 'n', 'new', now(), now())", (title, f"https://{title}.example/"))
                con.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(a, b, "t1")), threading.Thread(target=write, args=(b, a, "t2"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors
    pages, generation = _state()
    assert pages == 4 and generation == g0 + 2


def test_the_fast_turn_fingerprint_does_not_read_web_pages(monkeypatch):
    """Once the vocabulary is built, a turn issues one primary-key read of
    web_corpus_state — not the Seq Scan of web_pages it used to."""
    _insert("rice", "Cooking rice", "Rinse the rice and simmer it for twelve minutes. " * 5)
    assert lk._topical_precheck("explain badger sett ventilation") is False  # builds

    seen = []
    real = db.connection

    class Spy:
        def __init__(self, con):
            self._con = con

        def execute(self, sql, *args, **kwargs):
            seen.append(str(sql))
            return self._con.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._con, name)

    from contextlib import contextmanager

    @contextmanager
    def spying():
        with real() as con:
            yield Spy(con)

    monkeypatch.setattr(db, "connection", spying)
    assert lk._topical_precheck("explain badger sett ventilation") is False
    assert seen and all("web_pages" not in s for s in seen), seen


def test_a_page_written_by_another_process_rebuilds_the_vocabulary():
    """The generation is the database's, not this process's counter: a page
    inserted over a separate connection (another orchestrator, a script) is
    seen by the next turn."""
    _insert("rice", "Cooking rice", "Rinse the rice and simmer it for twelve minutes. " * 5)
    assert lk._topical_precheck("explain badger sett ventilation") is False
    with psycopg.connect(settings.app_database_url) as other:
        other.execute(
            "INSERT INTO web_pages (url_key, url, title, text, fetched_at, first_seen_at, content_hash, indexed_at) "
            "VALUES ('badger', 'https://docs.test/badger', 'How badger setts are ventilated', "
            "'Badger setts are ventilated by their many entrances.', now(), now(), 'hb', now())"
        )
    assert lk._topical_precheck("explain badger sett ventilation") is True


def test_corpus_counts_come_from_the_state_row_and_the_backlog_indexes():
    _insert("i1", "Indexed", "text one")
    _insert("p1", "Pending", "text two", indexed=False)
    due = _insert("d1", "Due", "text three")
    _insert("f1", "Future", "text four")
    with db.connection() as con:
        con.execute("UPDATE web_pages SET next_refresh_at = now() - interval '1 hour' WHERE id = %s", (due,))
        con.execute("UPDATE web_pages SET next_refresh_at = now() + interval '1 day' WHERE url_key = 'f1'")
    assert db.web_corpus_counts() == {"pages": 4, "pending": 1, "due": 1}
    assert "FROM web_corpus_state" in db._WEB_CORPUS_COUNTS_SQL


def test_rerunning_the_migration_keeps_the_state_exact():
    for i in range(3):
        _insert(f"r{i}", f"R {i}", "rerun text")
    g0 = _state()[1]
    with db.connection() as con:
        con.execute(db._MIGRATION_V38)
    pages, generation = _state()
    assert pages == 3 and generation > g0
    _insert("r3", "R 3", "rerun text")
    assert _state()[0] == 4


def test_the_migration_takes_its_strongest_lock_first_so_a_read_then_write_transaction_cannot_deadlock_it():
    """Review 2026-09-14. `SET COMPRESSION` needs ACCESS EXCLUSIVE; V38 used to
    take SHARE ROW EXCLUSIVE first and upgrade later. A transaction that had
    read web_pages and then wrote it while the migration waited formed a
    lock-upgrade cycle, and one side died with DeadlockDetected (reproduced on
    18: the migration waiting for AccessExclusiveLock, the writer for
    RowExclusiveLock). Taken first, the writer goes ahead and the migration
    waits for it."""
    page = _insert("dl", "Deadlock", "deadlock text")
    dsn = settings.app_database_url
    outcome = {}
    reader = psycopg.connect(dsn)
    try:
        reader.execute("SELECT count(*) FROM web_pages").fetchone()
        waiting = threading.Event()

        def migrate():
            try:
                with psycopg.connect(dsn) as con:
                    waiting.set()
                    con.execute(db._MIGRATION_V38)
                    con.commit()
                outcome["migration"] = "ok"
            except Exception as exc:  # noqa: BLE001
                outcome["migration"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=migrate)
        thread.start()
        waiting.wait(5)
        import time as _time

        _time.sleep(0.3)  # the migration is queued on web_pages by now
        try:
            reader.execute("UPDATE web_pages SET last_changed_at = now() WHERE id = %s", (page,))
            reader.commit()
            outcome["writer"] = "ok"
        except Exception as exc:  # noqa: BLE001
            reader.rollback()
            outcome["writer"] = f"{type(exc).__name__}: {exc}"
        thread.join(30)
    finally:
        reader.close()
    assert outcome == {"writer": "ok", "migration": "ok"}, outcome


# ---------------------------------------------------------------------------
# Retrieval demand off the indexed page row
# ---------------------------------------------------------------------------


def test_a_retrieval_bump_never_rewrites_the_page_row():
    page = _insert("demand", "Demand", "demand text")
    with db.connection() as con:
        before = con.execute("SELECT xmin::text AS x, ctid::text AS c FROM web_pages WHERE id = %s", (page,)).fetchone()
    web_memory._bump_retrieval([page, page])  # a duplicate id must not fail the statement
    web_memory._bump_retrieval([page, 987654])  # nor a page purged since it was retrieved
    with db.connection() as con:
        after = con.execute("SELECT xmin::text AS x, ctid::text AS c FROM web_pages WHERE id = %s", (page,)).fetchone()
        demand = con.execute("SELECT retrievals, retrieved_at FROM web_page_demand WHERE page_id = %s", (page,)).fetchone()
    assert (after["x"], after["c"]) == (before["x"], before["c"])
    assert int(demand["retrievals"]) == 2 and demand["retrieved_at"] is not None


def test_the_refresh_and_index_queues_order_by_base_plus_recorded_demand():
    low = _insert("low", "Low", "low text", indexed=False)
    high = _insert("high", "High", "high text", indexed=False)
    with db.connection() as con:
        con.execute("UPDATE web_pages SET retrieval_count = 2, next_refresh_at = now() - interval '1 hour'")
        con.execute("UPDATE web_pages SET retrieval_count = 3 WHERE id = %s", (low,))
    for _ in range(3):
        web_memory._bump_retrieval([high])
    due = web_worker._due_pages(10)
    assert [int(r["id"]) for r in due] == [high, low]
    assert {int(r["id"]): int(r["retrieval_count"]) for r in due} == {high: 5, low: 3}
    assert [p["id"] for p in db.get_unindexed_web_pages(limit=10)] == [high, low]


def test_a_purged_page_takes_its_demand_with_it():
    page = _insert("purge", "Purge", "purge text")
    web_memory._bump_retrieval([page])
    with db.connection() as con:
        con.execute("DELETE FROM web_pages WHERE id = %s", (page,))
        assert con.execute("SELECT count(*) AS n FROM web_page_demand").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# _page_meta by url
# ---------------------------------------------------------------------------


def test_page_meta_url_lookup_has_an_index():
    for i in range(50):
        _insert(f"m{i}", f"M {i}", "meta text")
    with db.connection() as con:
        con.execute("ANALYZE web_pages")
        con.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(
            r["QUERY PLAN"]
            for r in con.execute(
                "EXPLAIN SELECT id FROM web_pages WHERE url = ANY(%s) OR id = ANY(%s)",
                (["https://m1.example/m1", "https://m2.example/m2"], [3]),
            ).fetchall()
        )
    assert "idx_web_pages_url_hash" in plan, plan
    meta = web_memory._page_meta(["https://m1.example/m1"], [3])
    assert meta["https://m1.example/m1"]["id"] == 2 and meta["id:3"]["url"] == "https://m2.example/m2"
