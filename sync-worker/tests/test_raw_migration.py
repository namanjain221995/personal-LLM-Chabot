"""New worker code meets a warehouse built BEFORE the raw schema.

THE BUG THIS PINS. sync_object runs ensure_table at its head (under `USE raw`)
and builds the typed view only at its tail. On an unmigrated object, ensure_
table's CREATE TABLE IF NOT EXISTS made an EMPTY raw twin next to the populated
main table; the extract's rows landed in the twin; the tail's CREATE OR REPLACE
VIEW then failed ("existing object is of type Table"); and the orchestrator
kept reading the frozen main table forever. No error reached anyone.

Every write path now promotes first, and the promote leaves a passthrough view
so main.<obj> exists at every instant between head and tail.
"""
import duckdb
import pandas as pd

from syncworker.storage import Store
from syncworker.typemap import FieldSpec as F


def _old_layout(path):
    """A populated table and its watermark, both in main -- the pre-raw shape."""
    c = duckdb.connect(path)
    c.execute('CREATE TABLE main."Account" (Id VARCHAR, Name VARCHAR, SystemModstamp VARCHAR)')
    c.execute("""INSERT INTO main."Account" VALUES
        ('001A','Acme','t1'), ('001B','Globex','t1'), ('001C','Initech','t1')""")
    c.execute('CREATE TABLE main."_sync_meta" (object_name VARCHAR PRIMARY KEY, watermark VARCHAR, updated_at TIMESTAMP)')
    c.execute("INSERT INTO main._sync_meta VALUES ('Account', '2026-08-01T00:00:00Z', now())")
    c.close()


def _kind(con, schema, table):
    row = con.execute(
        "SELECT table_type FROM information_schema.tables WHERE table_schema=? AND table_name=?",
        [schema, table],
    ).fetchone()
    return row[0] if row else None


def test_ensure_table_migrates_a_populated_main_table_instead_of_shadowing_it(tmp_path):
    db = str(tmp_path / "wh.duckdb"); _old_layout(db)
    store = Store(db)
    with store.session():
        assert store.get_watermark("Account") == "2026-08-01T00:00:00Z"   # relocated, not lost
        assert store.ensure_table("Account", ["Id", "Name", "SystemModstamp"]) == []
    q = store._con
    assert _kind(q, "raw", "Account") == "BASE TABLE"
    assert q.execute('SELECT count(*) FROM raw."Account"').fetchone()[0] == 3   # the data moved
    assert _kind(q, "main", "Account") == "VIEW"                                # never absent
    assert q.execute('SELECT count(*) FROM main."Account"').fetchone()[0] == 3
    q.close(); store.close()


def test_the_sync_write_path_end_to_end_keeps_main_current(tmp_path):
    """Head session -> extract lands a row -> tail session builds the typed
    view. What the orchestrator reads must reflect the new row."""
    db = str(tmp_path / "wh.duckdb"); _old_layout(db)
    store = Store(db)
    with store.session():
        store.ensure_table("Account", ["Id", "Name", "SystemModstamp"])
    store.upsert("Account", pd.DataFrame([{"Id": "001A", "Name": "Acme v2", "SystemModstamp": "t2"}]))
    with store.session():
        assert store.refresh_typed_view(
            "Account", [F("Id", "id"), F("Name", "string"), F("SystemModstamp", "datetime")], "UTC"
        ) is True
    q = store._con
    assert _kind(q, "main", "Account") == "VIEW"
    assert q.execute('SELECT Name FROM main."Account" WHERE Id=\'001A\'').fetchone()[0] == "Acme v2"
    assert q.execute("SELECT data_type FROM information_schema.columns WHERE table_schema='main' AND table_name='Account' AND column_name='SystemModstamp'").fetchone()[0] == "TIMESTAMP"
    assert q.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='Account'").fetchone()[0] == 2  # one raw table + one main view, no twin
    q.close(); store.close()


def test_upsert_alone_also_migrates_first(tmp_path):
    """A write that bypasses ensure_table must not CTAS an empty twin either."""
    db = str(tmp_path / "wh.duckdb"); _old_layout(db)
    store = Store(db)
    store.upsert("Account", pd.DataFrame([{"Id": "001D", "Name": "Umbrella", "SystemModstamp": "t2"}]))
    q = store._con
    assert q.execute('SELECT count(*) FROM raw."Account"').fetchone()[0] == 4      # 3 moved + 1 new
    assert q.execute('SELECT count(*) FROM main."Account"').fetchone()[0] == 4
    q.close(); store.close()


def test_a_fresh_warehouse_is_unaffected(tmp_path):
    """No main table to migrate: promote is a no-op and nothing changes."""
    store = Store(str(tmp_path / "wh.duckdb"))
    assert store.ensure_table("Account", ["Id", "Name"]) == []
    q = store._con
    assert _kind(q, "raw", "Account") == "BASE TABLE"
    assert _kind(q, "main", "Account") is None   # no view until specs exist
    q.close(); store.close()


def test_the_passthrough_view_survives_a_column_being_added(tmp_path):
    """Between the head session (which may ADD COLUMN) and the tail (which
    rebuilds the typed view) the object must stay queryable."""
    db = str(tmp_path / "wh.duckdb"); _old_layout(db)
    store = Store(db)
    with store.session():
        added = store.ensure_table("Account", ["Id", "Name", "SystemModstamp", "Visa__c"])
    assert added == ["Visa__c"]
    q = store._con
    assert q.execute('SELECT count(*) FROM main."Account"').fetchone()[0] == 3   # no "contents altered"
    q.close(); store.close()
