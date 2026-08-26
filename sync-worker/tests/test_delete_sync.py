"""Records deleted in Salesforce no longer live in the local copy forever.

Before this, the SystemModstamp incremental filter could never see a delete,
so DuckDB rows and RAG chunks for deleted records only ever accumulated. Two
complementary mechanisms now exist: incremental cycles ask the recycle bin
via /queryAll (best effort), and a FULL extract reconciles exactly — local
rows absent from the snapshot are dropped. `objects resync` forces the full
path on demand.
"""
import pandas as pd
import pytest

from syncworker import objects as ob
from syncworker.main import _purge_local, sync_object
from syncworker.sf_client import build_deleted_soql
from syncworker.storage import Store


def _df_rows(store, table):
    return store._con.execute(f'SELECT Id FROM "{table}" ORDER BY Id').fetchall()


# ---------------------------------------------------------------------------
# SOQL builder
# ---------------------------------------------------------------------------


def test_deleted_soql_targets_the_recycle_bin_window():
    soql = build_deleted_soql("Account", "2026-08-01T00:00:00Z")
    assert soql == (
        "SELECT Id FROM Account "
        "WHERE IsDeleted = true AND SystemModstamp > 2026-08-01T00:00:00Z"
    )


def test_deleted_soql_rejects_a_bad_watermark():
    with pytest.raises(ValueError):
        build_deleted_soql("Account", "2026-08-01' OR 1=1")


# ---------------------------------------------------------------------------
# Store: targeted deletes + full-extract reconciliation
# ---------------------------------------------------------------------------


def _seed(store):
    store.upsert(
        "Account",
        pd.DataFrame(
            [
                # SystemModstamp is present, as it is in every real warehouse
                # table: a configured column missing from the table is exactly
                # what triggers a full backfill extract (test_adoption_backfill).
                {"Id": "001A", "Name": "Acme", "SystemModstamp": "t1"},
                {"Id": "001B", "Name": "Globex", "SystemModstamp": "t1"},
                {"Id": "001C", "Name": "Initech", "SystemModstamp": "t1"},
            ]
        ),
    )


def test_delete_ids_removes_only_the_named_rows(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    removed = store.delete_ids("Account", ["001B"])
    assert removed == 1
    assert _df_rows(store, "Account") == [("001A",), ("001C",)]
    store.close()


def test_delete_ids_survives_missing_table_and_empty_list(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    assert store.delete_ids("Nowhere", ["001A"]) == 0
    _seed(store)
    assert store.delete_ids("Account", []) == 0
    store.close()


def test_reconcile_full_drops_rows_absent_from_the_snapshot(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    removed = store.reconcile_full("Account", {"001A", "001C"})
    assert removed == ["001B"]
    assert _df_rows(store, "Account") == [("001A",), ("001C",)]
    store.close()


def test_clear_watermark_forces_the_next_full_extract(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.set_watermark("Account", "2026-08-01T00:00:00Z")
    store.clear_watermark("Account")
    assert store.get_watermark("Account") is None
    store.close()


# ---------------------------------------------------------------------------
# RAG purge
# ---------------------------------------------------------------------------


class FakeTable:
    def __init__(self):
        self.predicates = []

    def delete(self, predicate):
        self.predicates.append(predicate)


def _indexer_with(table):
    from syncworker.rag_index import RagIndexer

    indexer = RagIndexer.__new__(RagIndexer)
    indexer._open_table_if_exists = lambda: table
    return indexer


def test_rag_purge_validates_ids_before_building_the_predicate():
    table = FakeTable()
    indexer = _indexer_with(table)
    n = indexer.delete_records(["001xx000003DGbY", "nope' OR 1=1", ""])
    assert n == 1
    assert table.predicates == ["record_id IN ('001xx000003DGbY')"]


def test_rag_purge_without_a_table_is_a_no_op():
    indexer = _indexer_with(None)
    assert indexer.delete_records(["001xx000003DGbY"]) == 0


# ---------------------------------------------------------------------------
# sync_object integration: incremental purge + full reconcile
# ---------------------------------------------------------------------------


class Obj:
    name = "Account"
    fields = ("Id", "Name", "SystemModstamp")
    rag_fields = ()
    watermark_field = "SystemModstamp"


class Settings:
    parquet_dir = None  # set per test
    sync_auto_fields = False
    sync_max_fields = 80


class IncrementalClient:
    """One changed record; 001B sits soft-deleted in the recycle bin."""

    def describe_fields(self, name):
        return {"Id", "Name", "SystemModstamp", "IsDeleted"}

    def soql_query(self, soql):
        yield [{"Id": "001A", "Name": "Acme v2", "SystemModstamp": "t2"}]

    def soql_query_all(self, soql):
        assert "IsDeleted = true" in soql
        yield [{"Id": "001B"}]


class FullClient:
    """Full snapshot no longer contains 001C."""

    def describe_fields(self, name):
        return {"Id", "Name", "SystemModstamp"}

    def bulk_query(self, soql):
        yield [
            {"Id": "001A", "Name": "Acme", "SystemModstamp": "t1"},
            {"Id": "001B", "Name": "Globex", "SystemModstamp": "t1"},
        ]


def test_incremental_cycle_purges_recycle_bin_deletes(tmp_path):
    settings = Settings()
    settings.parquet_dir = str(tmp_path / "pq")
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    store.set_watermark("Account", "2026-08-01T00:00:00Z")

    sync_object(Obj(), IncrementalClient(), store, None, settings)

    assert _df_rows(store, "Account") == [("001A",), ("001C",)]
    store.close()


def test_full_extract_reconciles_exactly(tmp_path):
    settings = Settings()
    settings.parquet_dir = str(tmp_path / "pq")
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)  # 001A, 001B, 001C — no watermark → full mode

    sync_object(Obj(), FullClient(), store, None, settings)

    assert _df_rows(store, "Account") == [("001A",), ("001B",)]
    store.close()


def test_a_failed_delete_pass_does_not_block_the_sync(tmp_path):
    class BrokenDeletes(IncrementalClient):
        def soql_query_all(self, soql):
            raise RuntimeError("queryAll forbidden for this user")

    settings = Settings()
    settings.parquet_dir = str(tmp_path / "pq")
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    store.set_watermark("Account", "2026-08-01T00:00:00Z")

    total = sync_object(Obj(), BrokenDeletes(), store, None, settings)

    assert total == 1  # the data sync itself succeeded
    assert store.get_watermark("Account") is not None  # watermark advanced
    store.close()


def test_purge_local_reports_rag_only_when_store_already_reconciled():
    table = FakeTable()
    _purge_local("Account", ["001xx000003DGbY"], _indexer_with(table),
                 source="full_reconcile")
    assert table.predicates  # RAG purged even with store=None


# ---------------------------------------------------------------------------
# CLI: resync
# ---------------------------------------------------------------------------


def test_resync_clears_the_watermark_via_the_cli(tmp_path):
    db = str(tmp_path / "wh.duckdb")
    store = Store(db)
    store.set_watermark("Account", "2026-08-01T00:00:00Z")
    store.close()

    rc = ob.main(["resync", "Account", "--duckdb", db])

    assert rc == 0
    store = Store(db)
    assert store.get_watermark("Account") is None
    store.close()


def test_resync_on_an_unsynced_object_is_informative_not_fatal(tmp_path):
    db = str(tmp_path / "wh.duckdb")
    Store(db).close()
    assert ob.main(["resync", "Never_Synced__c", "--duckdb", db]) == 0


def test_objects_without_isdeleted_skip_the_recycle_bin_pass(tmp_path):
    """User (and other setup objects) have no IsDeleted field — asking
    queryAll about them is a guaranteed INVALID_FIELD every cycle."""

    class NoIsDeletedClient(IncrementalClient):
        def describe_fields(self, name):
            return {"Id", "Name", "SystemModstamp"}  # no IsDeleted

        def soql_query_all(self, soql):
            raise AssertionError("queryAll must not run without IsDeleted")

    class HasIsDeleted(IncrementalClient):
        def describe_fields(self, name):
            return {"Id", "Name", "SystemModstamp", "IsDeleted"}

    settings = Settings()
    settings.parquet_dir = str(tmp_path / "pq")
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    store.set_watermark("Account", "2026-08-01T00:00:00Z")

    sync_object(Obj(), NoIsDeletedClient(), store, None, settings)  # no boom
    sync_object(Obj(), HasIsDeleted(), store, None, settings)  # purge runs
    assert _df_rows(store, "Account") == [("001A",), ("001C",)]
    store.close()
