"""The cached snapshot reader (core/warehouse.py, 2026-09-13)."""
from __future__ import annotations

import asyncio
import json
import os
import threading
import shutil

import duckdb
import pytest

from app import health
from app.config import settings
from app.core import warehouse
from app.core.schema_cache import SchemaCache
from app.engines import sql as sqleng


def _build(path: str, value: int) -> None:
    """A tiny warehouse in the sync worker's shape: raw tables, typed views."""
    tmp = path + ".build"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = duckdb.connect(tmp)
    con.execute("CREATE SCHEMA raw")
    con.execute('CREATE TABLE raw."Account" ("Id" VARCHAR, "Name" VARCHAR, "Amount" VARCHAR)')
    con.execute('INSERT INTO raw."Account" VALUES (?, ?, ?)', [f"001{value}", f"Person {value}", str(value)])
    con.execute(
        'CREATE VIEW main."Account" AS SELECT "Id", "Name", TRY_CAST("Amount" AS BIGINT) AS "Amount" FROM raw."Account"'
    )
    con.execute("CHECKPOINT")
    con.close()
    os.replace(tmp, path)


def _publish(snapshot: str, value: int) -> None:
    """storage.Store.publish_snapshot: copy, then an atomic os.replace."""
    staging = snapshot + ".src"
    _build(staging, value)
    shutil.copyfile(staging, snapshot + ".tmp")
    os.replace(snapshot + ".tmp", snapshot)


@pytest.fixture()
def snapshot(tmp_path, monkeypatch):
    live = str(tmp_path / "warehouse.duckdb")
    snap = str(tmp_path / "warehouse.read.duckdb")
    _build(live, 0)
    _publish(snap, 1)
    monkeypatch.setattr(settings, "_duckdb_live_path", live)
    monkeypatch.setattr(settings, "duckdb_snapshot_path", snap)
    monkeypatch.setattr(settings, "_prefer_snapshot", True)
    monkeypatch.delenv("WAREHOUSE_READER_CACHE", raising=False)
    warehouse.reset()
    yield snap
    warehouse.reset()


def _amount(cur) -> int:
    try:
        return cur.execute('SELECT "Amount" FROM "Account"').fetchone()[0]
    finally:
        cur.close()


def test_the_snapshot_is_opened_once_and_served_per_cursor(snapshot, monkeypatch):
    opened = []
    real = warehouse._open_generation
    monkeypatch.setattr(warehouse, "_open_generation", lambda p: opened.append(p) or real(p))
    assert settings.duckdb_path == snapshot
    for _ in range(5):
        cols, rows = sqleng._execute('SELECT "Name", "Amount" FROM "Account"', 10)
        assert rows == [["Person 1", 1]]
    assert opened == [snapshot]


def test_a_replaced_snapshot_is_read_on_the_next_call_and_inflight_cursors_finish(snapshot):
    before = warehouse.cursor(snapshot)
    _publish(snapshot, 2)
    assert _amount(warehouse.cursor(snapshot)) == 2
    # The cursor opened before the replace still works, on the data it started with.
    assert _amount(before) == 1


def test_the_live_file_is_never_held_open(snapshot, monkeypatch):
    live = settings._duckdb_live_path
    monkeypatch.setattr(settings, "_prefer_snapshot", False)
    assert settings.duckdb_path == live
    assert warehouse.cursor(live) is None
    assert sqleng._execute('SELECT "Amount" FROM "Account"', 10)[1] == [[0]]
    # Nothing in this process still holds the file: a writer gets in at once.
    writer = duckdb.connect(live)
    writer.execute('INSERT INTO raw."Account" VALUES (\'x\', \'y\', \'5\')')
    writer.close()


def test_a_cursor_cannot_reach_the_file_system_or_unlock_its_configuration(snapshot, tmp_path):
    outside = tmp_path / "secret.csv"
    outside.write_text("a\n1\n")
    cur = warehouse.cursor(snapshot)
    try:
        with pytest.raises(duckdb.PermissionException):
            cur.execute(f"SELECT * FROM read_csv_auto('{outside}')").fetchall()
        with pytest.raises(duckdb.Error):
            cur.execute("SET enable_external_access = true")
        with pytest.raises(duckdb.Error):
            cur.execute(f"ATTACH '{tmp_path / 'other.duckdb'}' AS other")
    finally:
        cur.close()


def test_the_schema_read_is_identical_through_the_cache(snapshot, monkeypatch):
    cached = SchemaCache._load(snapshot)
    monkeypatch.setenv("WAREHOUSE_READER_CACHE", "false")
    direct = SchemaCache._load(snapshot)
    assert cached == direct == {"Account": [("Id", "VARCHAR"), ("Name", "VARCHAR"), ("Amount", "BIGINT")]}


def test_health_checks_the_cached_instance_and_the_switch_turns_it_off(snapshot, monkeypatch):
    assert health._check_duckdb(snapshot) == {"status": "ok"}
    assert warehouse._current is not None
    monkeypatch.setenv("WAREHOUSE_READER_CACHE", "false")
    assert warehouse.cursor(snapshot) is None
    assert health._check_duckdb(snapshot) == {"status": "ok"}


def test_an_unreadable_snapshot_falls_back_to_the_direct_error(snapshot):
    with open(snapshot, "wb") as fh:
        fh.write(b"not a duckdb file")
    warehouse.reset()
    assert warehouse.cursor(snapshot) is None
    assert health._check_duckdb(snapshot)["status"] == "error"


def test_sf_intel_renders_the_people_it_already_resolved(monkeypatch):
    from app.engines import sf_intel

    calls = []
    person = {"asked": "Jay Soni", "object": "Recruiter__c",
              "meaning": "staff (Recruiter__c) — interviews they RAN link through x",
              "matches": ["Jay Soni"]}
    monkeypatch.setattr(sqleng, "resolve_people", lambda q: calls.append(q) or [person])
    options, facts = asyncio.run(sf_intel._entity_candidates("how many interviews did Jay Soni run", []))
    assert options == [] and "Jay Soni" in facts
    assert len(calls) == 1, "the warehouse lookup must not run twice"


# ---------------------------------------------------------------------------
# Review additions (dbperf2 duckdb-datasets, 2026-09-14)
# ---------------------------------------------------------------------------


def test_a_generation_that_lost_its_catalog_is_reopened_not_abandoned(snapshot, monkeypatch):
    """lock_configuration does not stop DETACH from a cursor that never USEd
    the catalog. The reader must notice and re-attach, not fall back to the
    open-per-call path until the next snapshot."""
    opened = []
    real = warehouse._open_generation
    monkeypatch.setattr(warehouse, "_open_generation", lambda p: opened.append(p) or real(p))
    assert _amount(warehouse.cursor(snapshot)) == 1
    raw = warehouse._current.instance.cursor()
    raw.execute(f"DETACH {warehouse.CATALOG}")
    raw.close()
    assert _amount(warehouse.cursor(snapshot)) == 1
    assert opened == [snapshot, snapshot]


def _spill_dir(cur) -> str:
    try:
        return cur.execute("SELECT current_setting('temp_directory')").fetchone()[0]
    finally:
        cur.close()


def test_the_reader_spills_to_the_configured_temp_directory(snapshot, tmp_path, monkeypatch):
    spill = tmp_path / "spill"
    monkeypatch.setenv("WAREHOUSE_READER_TEMP_DIR", str(spill))
    used = _spill_dir(warehouse.cursor(snapshot))
    assert os.path.dirname(used) == str(spill)
    assert spill.is_dir()


def test_every_generation_spills_into_its_own_directory(snapshot, tmp_path, monkeypatch):
    """DuckDB's spill file names carry no per-instance part, and the instance
    that created the directory deletes it on close: two generations spilling
    into ONE directory at once segfaulted the process (2 of 2 on 1.5.5)."""
    monkeypatch.setenv("WAREHOUSE_READER_TEMP_DIR", str(tmp_path / "spill"))
    first = _spill_dir(warehouse.cursor(snapshot))
    _publish(snapshot, 2)
    second = _spill_dir(warehouse.cursor(snapshot))
    assert first != second
    assert os.path.dirname(first) == os.path.dirname(second) == str(tmp_path / "spill")


def test_nothing_a_statement_creates_outlives_its_cursor(snapshot, monkeypatch):
    """The old per-call read-only connection had no writable catalog at all.
    The cached instance must not keep one either, or what one request's
    statement stored would be visible to every later request in the process."""
    opened = []
    real = warehouse._open_generation
    monkeypatch.setattr(warehouse, "_open_generation", lambda p: opened.append(p) or real(p))
    cur = warehouse.cursor(snapshot)
    try:
        with pytest.raises(duckdb.Error):
            cur.execute("CREATE TABLE memory.main.leak AS SELECT 42 AS x")
        with pytest.raises(duckdb.Error):
            cur.execute("CREATE TABLE leak AS SELECT 42 AS x")
        # Still allowed by DuckDB with external access off, so the reader
        # must notice it on the next cursor and start a clean generation.
        cur.execute("ATTACH ':memory:' AS stash")
        cur.execute("CREATE TABLE stash.main.leak AS SELECT 42 AS x")
    finally:
        cur.close()
    nxt = warehouse.cursor(snapshot)
    try:
        with pytest.raises(duckdb.Error, match="stash"):
            nxt.execute("SELECT * FROM stash.main.leak").fetchall()
        assert nxt.execute('SELECT "Amount" FROM "Account"').fetchone()[0] == 1
    finally:
        nxt.close()
    assert opened == [snapshot, snapshot]


def _person_warehouse(path: str) -> None:
    """Every person object, and one human stored as staff AND as many
    lower-precedence rows with exactly the same spelling (equal similarity)."""
    con = duckdb.connect(path)
    con.execute("CREATE TABLE RecordType (Id VARCHAR, Name VARCHAR)")
    con.execute("INSERT INTO RecordType VALUES ('012A', 'Person Account')")
    con.execute("CREATE TABLE Recruiter__c (Name VARCHAR)")
    con.execute("CREATE TABLE Account (Name VARCHAR, RecordTypeId VARCHAR)")
    con.execute("CREATE TABLE Contact (Name VARCHAR)")
    con.execute("CREATE TABLE Lead (Name VARCHAR)")
    con.execute("INSERT INTO Lead SELECT 'Jayesh Prajapathi' FROM range(40)")
    con.execute("INSERT INTO Contact SELECT 'Jayesh Prajapathi' FROM range(40)")
    con.execute("INSERT INTO Account SELECT 'Jayesh Prajapathi', '012A' FROM range(40)")
    con.execute("INSERT INTO Recruiter__c VALUES ('Jayesh Prajapathi')")
    con.execute("CHECKPOINT")
    con.close()


def test_a_fuzzy_tie_resolves_to_the_same_object_as_the_exact_path_every_time(tmp_path):
    live = str(tmp_path / "people.duckdb")
    _person_warehouse(live)
    con = duckdb.connect(live, read_only=True)
    try:
        answers = {
            json.dumps(sqleng._fuzzy_person(con, "Jayesh Prajapati"), sort_keys=True)
            for _ in range(25)
        }
    finally:
        con.close()
    assert len(answers) == 1
    found = json.loads(answers.pop())
    # Staff first, as `_PERSON_SOURCES` orders the exact lookup.
    assert found["object"] == "Recruiter__c"
    assert found["fuzzy"] is True


def test_every_unmatched_name_is_scored_in_one_statement_and_keeps_its_place(tmp_path, monkeypatch):
    live = str(tmp_path / "people.duckdb")
    _person_warehouse(live)
    con = duckdb.connect(live)
    con.execute("INSERT INTO Contact VALUES ('Monica Challa'), ('Priya Venkataraman')")
    con.close()
    monkeypatch.setattr(settings, "duckdb_path", live)
    warehouse.reset()

    statements = []
    real_connect = sqleng._connect_warehouse

    class Counting:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=None):
            statements.append(sql)
            return self._inner.execute(sql, params)

        def close(self):
            self._inner.close()

    monkeypatch.setattr(sqleng, "_connect_warehouse", lambda d: Counting(real_connect(d)))
    # Two misspellings around one exact name.
    found = sqleng.resolve_people(
        "compare Jayesh Prajapati with Monica Challa and Priya Venkatraman"
    )
    assert [p["asked"] for p in found] == ["Jayesh Prajapati", "Monica Challa", "Priya Venkatraman"]
    assert [p["object"] for p in found] == ["Recruiter__c", "Contact", "Contact"]
    assert [p.get("fuzzy", False) for p in found] == [True, False, True]
    assert found[2]["matches"] == ["Priya Venkataraman"]
    assert sum("jaro_winkler_similarity" in s for s in statements) == 1


def test_ask_sql_resolves_people_off_the_event_loop(monkeypatch):
    from app import llm

    threads = []

    def lookup(question, *args, **kwargs):
        threads.append(threading.current_thread())
        return ""

    async def fake_completion(*args, **kwargs):
        return "SELECT 1"

    monkeypatch.setattr(sqleng, "who_these_people_are", lookup)
    monkeypatch.setattr(llm, "chat_completion", fake_completion)

    async def run():
        loop_thread = threading.current_thread()
        sql = await sqleng._ask_sql("how many interviews did Jay Soni run", "Account(Id VARCHAR)")
        return loop_thread, sql

    loop_thread, sql = asyncio.run(run())
    assert sql == "SELECT 1"
    assert threads and threads[0] is not loop_thread
