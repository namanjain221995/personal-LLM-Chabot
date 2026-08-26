"""The warehouse write lock is never held across network I/O.

A `Store.session()` holds DuckDB's file lock for its whole lifetime, and the
orchestrator's read-only queries cannot open the file at all while it does.
Measured with realistic Salesforce latency, a session that spanned the
extract took chat queries from 100% success at ~300 ms to 40% at ~4 s.

So sync_object must do every Salesforce and embedding call with no session
open. This test makes that a contract: each fake I/O method asserts that no
connection is pinned at the moment it is called.
"""
import pandas as pd

from syncworker.main import sync_object
from syncworker.storage import Store
from syncworker.typemap import FieldSpec


class Obj:
    name = "Account"
    fields = ("Id", "Name", "SystemModstamp")
    rag_fields = ("Name",)
    watermark_field = "SystemModstamp"


class Settings:
    parquet_dir = None
    sync_auto_fields = True
    sync_max_fields = 80
    sf_org_timezone = "UTC"


class LockWatchingClient:
    """Every Salesforce call records whether the write lock was held."""

    def __init__(self, store):
        self.store = store
        self.locked_during = []

    def _observe(self, call):
        self.locked_during.append((call, self.store._pinned is not None))

    def describe_fields(self, name):
        self._observe("describe_fields")
        return {"Id", "Name", "SystemModstamp", "IsDeleted"}

    def describe_field_types(self, name):
        self._observe("describe_field_types")
        return {"Id": "id", "Name": "string", "SystemModstamp": "datetime"}

    def describe_field_specs(self, name):
        self._observe("describe_field_specs")
        return []

    def soql_query(self, soql):
        self._observe("soql_query:first_page")
        yield [{"Id": "001A", "Name": "Acme v2", "SystemModstamp": "t2"}]
        self._observe("soql_query:next_page")
        yield [{"Id": "001B", "Name": "Globex v2", "SystemModstamp": "t2"}]

    def soql_query_all(self, soql):
        self._observe("soql_query_all")
        yield [{"Id": "001C"}]


class LockWatchingIndexer:
    """Every embedding call records whether the write lock was held."""

    def __init__(self, store):
        self.store = store
        self.locked_during = []

    def index_records(self, object_name, records, rag_fields):
        self.locked_during.append(("index_records", self.store._pinned is not None))
        return len(records)

    def delete_records(self, ids):
        self.locked_during.append(("delete_records", self.store._pinned is not None))
        return len(ids)


def test_no_salesforce_or_embedding_call_runs_under_the_write_lock(tmp_path):
    settings = Settings()
    settings.parquet_dir = str(tmp_path / "pq")
    store = Store(str(tmp_path / "wh.duckdb"))
    store.upsert("Account", pd.DataFrame([
        {"Id": "001A", "Name": "Acme", "SystemModstamp": "t1"},
        {"Id": "001C", "Name": "Initech", "SystemModstamp": "t1"},
    ]))
    store.set_watermark("Account", "2026-08-01T00:00:00Z")
    client = LockWatchingClient(store)
    indexer = LockWatchingIndexer(store)

    sync_object(Obj(), client, store, indexer, settings)

    # Every kind of call was actually exercised...
    seen = {name for name, _ in client.locked_during}
    assert {"describe_fields", "soql_query:first_page", "soql_query:next_page",
            "soql_query_all", "describe_field_specs"} <= seen
    assert "index_records" in {name for name, _ in indexer.locked_during}
    # ...and no network call happened while a session held the lock.
    assert [c for c in client.locked_during if c[1]] == []
    assert [c for c in indexer.locked_during if c[1] and c[0] == "index_records"] == []
    # The delete purge (recycle bin -> DuckDB + LanceDB) is the one indexer
    # call that legitimately runs inside the tail session; it is local disk,
    # not network, and only fires when Salesforce reported deletions.
    assert ("delete_records", True) in indexer.locked_during
    store.close()


def test_the_sync_still_did_its_job(tmp_path):
    """Scoping the lock must not have changed what lands in the warehouse."""
    settings = Settings()
    settings.parquet_dir = str(tmp_path / "pq")
    store = Store(str(tmp_path / "wh.duckdb"))
    store.upsert("Account", pd.DataFrame([
        {"Id": "001A", "Name": "Acme", "SystemModstamp": "t1"},
        {"Id": "001C", "Name": "Initech", "SystemModstamp": "t1"},
    ]))
    store.set_watermark("Account", "2026-08-01T00:00:00Z")

    total = sync_object(Obj(), LockWatchingClient(store), store,
                        LockWatchingIndexer(store), settings)

    assert total == 2
    rows = store._con.execute('SELECT Id, Name FROM "Account" ORDER BY Id').fetchall()
    assert rows == [("001A", "Acme v2"), ("001B", "Globex v2")]  # 001C purged
    assert store.get_watermark("Account") != "2026-08-01T00:00:00Z"
    store.close()


def test_a_quiet_object_opens_the_warehouse_exactly_twice(tmp_path):
    """The whole point, stated as a number: head session + tail session.

    The tail covers the watermark stamp AND the typed-view rebuild, which is
    why real field specs are returned here — with none, the view step is
    skipped and the test would pass without proving it shares the connection.
    (Plus one per upsert batch when there are rows, and one for the pending
    RAG read on objects with rag_fields — neither applies to a quiet object
    with no rag_fields.)
    """
    class Quiet(LockWatchingClient):
        def soql_query(self, soql):
            return iter(())

        def soql_query_all(self, soql):
            return iter(())

        def describe_field_specs(self, name):
            return [FieldSpec("Id", "id"), FieldSpec("Name", "string"),
                    FieldSpec("SystemModstamp", "datetime")]

    class NoRag(Obj):
        rag_fields = ()

    settings = Settings()
    settings.parquet_dir = str(tmp_path / "pq")
    store = Store(str(tmp_path / "wh.duckdb"))
    store.ensure_table("Account", ["Id", "Name", "SystemModstamp"])
    store.set_watermark("Account", "2026-08-01T00:00:00Z")
    before = store.connects

    sync_object(NoRag(), Quiet(store), store, None, settings)

    assert store.connects - before == 2
    # ...and the typed view really was built inside that second session.
    kind = store._con.execute(
        "SELECT table_type FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_name = 'Account'"
    ).fetchone()
    assert kind == ("VIEW",)
    store.close()
