"""The readers' snapshot: why it exists, and what it must guarantee.

DuckDB allows MANY READERS **or** ONE WRITER on a file. While this worker holds
the write lock, every reader is refused — measured at 41.4% of the orchestrator's
single-shot read opens failing, and each failure fell back to live Salesforce
whose 200-row cap then became the answer to "how many?" (the wrong counts in
TechSara_Fix_Report.pdf).

`publish_snapshot()` gives readers a file that has no writer at all.
"""
import os

import duckdb
import pandas as pd
import pytest

from syncworker.storage import Store


def _rows(store: Store, table: str) -> int:
    with store._connection() as con:  # noqa: SLF001 — asserting on the real file
        return con.execute(f'SELECT COUNT(*) FROM raw."{table}"').fetchone()[0]


def test_publish_snapshot_writes_a_sibling_readers_file(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.upsert("Acct", pd.DataFrame([{"Id": "a1", "Name": "one"}]))

    target = store.publish_snapshot()

    assert target == str(tmp_path / "wh.read.duckdb")
    assert os.path.exists(target)
    assert not os.path.exists(target + ".tmp")  # staging is always cleaned up


def test_a_reader_can_open_the_snapshot_while_the_writer_holds_the_warehouse(tmp_path):
    """The whole point: this is the open that used to fail 41% of the time."""
    store = Store(str(tmp_path / "wh.duckdb"))
    store.upsert("Acct", pd.DataFrame([{"Id": "a1", "Name": "one"}]))
    snapshot = store.publish_snapshot()

    with store.session():  # the worker now holds the write lock, as in a cycle
        with pytest.raises(duckdb.Error):
            duckdb.connect(str(tmp_path / "wh.duckdb"), read_only=True)
        reader = duckdb.connect(snapshot, read_only=True)
        try:
            assert reader.execute('SELECT COUNT(*) FROM raw."Acct"').fetchone()[0] == 1
        finally:
            reader.close()


def test_republishing_carries_new_rows(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.upsert("Acct", pd.DataFrame([{"Id": "a1", "Name": "one"}]))
    store.publish_snapshot()

    store.upsert("Acct", pd.DataFrame([{"Id": "a2", "Name": "two"}]))
    snapshot = store.publish_snapshot()

    assert _rows(store, "Acct") == 2
    reader = duckdb.connect(snapshot, read_only=True)
    try:
        assert reader.execute('SELECT COUNT(*) FROM raw."Acct"').fetchone()[0] == 2
    finally:
        reader.close()


def test_snapshot_path_is_derived_not_guessed():
    assert Store.snapshot_path("/data/warehouse.duckdb") == "/data/warehouse.read.duckdb"
    assert Store.snapshot_path("/data/warehouse") == "/data/warehouse.read.duckdb"
