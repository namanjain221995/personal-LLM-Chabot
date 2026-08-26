"""A column that just appeared in the warehouse is backfilled, not left NULL.

THE BUG THIS FIXES. When a field is adopted from Salesforce (or added to the
YAML config, or made visible by loosened field-level security), ensure_table
adds the column — NULL for every existing row. An incremental cycle then
fetches only records modified since the watermark, so on a 50,000-row object
the new column is populated for the handful edited in the last five minutes
and stays NULL for the rest. Nothing ever revisits them: the watermark has
already moved past. "How many candidates have X" then answers off ~0.06% of
the data, with no error to suggest anything is wrong.

THE TRIGGER IS THE SCHEMA, NOT ADOPTION. `adopt_new_fields` compares describe
against the YAML config, which is never rewritten — so an adopted field is
re-adopted every cycle, and "did we adopt something?" would force a full
extract every five minutes forever. "Did ensure_table add a column?" fires
exactly once. test_a_second_cycle_with_the_same_client_and_config_is_
incremental pins that.

CRASH SAFETY. The stored watermark is cleared as well as the local one, so a
backfill that fails partway is retried in full next cycle instead of leaving
the column half populated behind a watermark that says "all done".
"""
import pandas as pd
import pytest

from syncworker.main import sync_object
from syncworker.storage import Store

WATERMARK = "2026-08-01T00:00:00Z"


class Obj:
    name = "Candidate__c"
    fields = ("Id", "Name", "SystemModstamp")
    rag_fields = ()
    watermark_field = "SystemModstamp"


class Settings:
    parquet_dir = None  # set per test
    sync_auto_fields = True
    sync_max_fields = 80
    sf_org_timezone = "UTC"


class AdoptingClient:
    """Salesforce grew a Visa_Status__c since the config was written.

    Records which extraction path was taken, because that is the whole
    question: full (backfills every row) or incremental (does not).
    """

    def __init__(self):
        self.paths = []

    def describe_fields(self, name):
        return {"Id", "Name", "SystemModstamp", "Visa_Status__c", "IsDeleted"}

    def describe_field_types(self, name):
        return {
            "Id": "id",
            "Name": "string",
            "SystemModstamp": "datetime",
            "Visa_Status__c": "picklist",
        }

    def describe_field_specs(self, name):
        return []

    def bulk_query(self, soql):
        self.paths.append("full")
        assert "Visa_Status__c" in soql
        yield [
            {"Id": "a01", "Name": "Asha", "SystemModstamp": "t1",
             "Visa_Status__c": "H1B"},
            {"Id": "a02", "Name": "Ben", "SystemModstamp": "t1",
             "Visa_Status__c": "H1B"},
            {"Id": "a03", "Name": "Chen", "SystemModstamp": "t1",
             "Visa_Status__c": "GC"},
        ]

    def soql_query(self, soql):
        self.paths.append("incremental")
        yield [
            {"Id": "a03", "Name": "Chen", "SystemModstamp": "t2",
             "Visa_Status__c": "GC"},
        ]

    def soql_query_all(self, soql):
        return iter(())


class TwoFieldClient(AdoptingClient):
    def describe_fields(self, name):
        return super().describe_fields(name) | {"Skill__c"}

    def describe_field_types(self, name):
        return {**super().describe_field_types(name), "Skill__c": "string"}

    def bulk_query(self, soql):
        assert "Skill__c" in soql
        for batch in super().bulk_query(soql):
            yield [{**r, "Skill__c": "py"} for r in batch]


class FailsMidExtract(AdoptingClient):
    """The full extract dies after its first page — a dropped connection."""

    def bulk_query(self, soql):
        self.paths.append("full")
        yield [{"Id": "a01", "Name": "Asha", "SystemModstamp": "t1",
                "Visa_Status__c": "H1B"}]
        raise ConnectionError("salesforce hung up")


def _seed(store, with_column=False):
    """Three candidates already in the warehouse."""
    rows = [
        {"Id": "a01", "Name": "Asha", "SystemModstamp": "t1"},
        {"Id": "a02", "Name": "Ben", "SystemModstamp": "t1"},
        {"Id": "a03", "Name": "Chen", "SystemModstamp": "t1"},
    ]
    if with_column:
        rows = [{**r, "Visa_Status__c": None} for r in rows]
    store.upsert("Candidate__c", pd.DataFrame(rows))
    store.set_watermark("Candidate__c", WATERMARK)


def _settings(tmp_path):
    s = Settings()
    s.parquet_dir = str(tmp_path / "pq")
    return s


def _filled(store, column="Visa_Status__c"):
    return store._con.execute(
        f'SELECT count(*) FROM "Candidate__c" WHERE "{column}" IS NOT NULL'
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# the fix
# ---------------------------------------------------------------------------


def test_a_new_column_forces_a_full_extract_that_cycle(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    client = AdoptingClient()

    sync_object(Obj(), client, store, None, _settings(tmp_path))

    assert client.paths == ["full"]
    store.close()


def test_the_new_column_is_populated_for_every_row(tmp_path):
    """The user-visible outcome: a count over the new field is right."""
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)

    sync_object(Obj(), AdoptingClient(), store, None, _settings(tmp_path))

    assert _filled(store) == 3  # not 1 — every row, not just the edited one
    h1b = store._con.execute(
        'SELECT count(*) FROM "Candidate__c" WHERE "Visa_Status__c" = \'H1B\''
    ).fetchone()[0]
    assert h1b == 2
    store.close()


def test_several_new_columns_cost_one_full_extract(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    client = TwoFieldClient()

    sync_object(Obj(), client, store, None, _settings(tmp_path))

    assert client.paths == ["full"]
    assert _filled(store, "Visa_Status__c") == 3
    assert _filled(store, "Skill__c") == 3
    store.close()


def test_a_field_added_to_the_yaml_config_is_backfilled_too(tmp_path):
    """Not only auto-adoption: the trigger is the column appearing, however
    it got into the field list."""
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    settings = _settings(tmp_path)
    settings.sync_auto_fields = False

    class WidenedConfig(Obj):
        fields = ("Id", "Name", "SystemModstamp", "Visa_Status__c")

    client = AdoptingClient()
    sync_object(WidenedConfig(), client, store, None, settings)

    assert client.paths == ["full"]
    assert _filled(store) == 3
    store.close()


# ---------------------------------------------------------------------------
# once, and only once
# ---------------------------------------------------------------------------


def test_a_second_cycle_with_the_same_client_and_config_is_incremental(tmp_path):
    """THE regression guard. Adoption is stateless — the field is re-adopted
    every cycle — so anything keyed on adoption would go full forever. The
    column exists after cycle one, so the schema-based trigger stays quiet."""
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    settings = _settings(tmp_path)
    client = AdoptingClient()

    sync_object(Obj(), client, store, None, settings)
    sync_object(Obj(), client, store, None, settings)
    sync_object(Obj(), client, store, None, settings)

    assert client.paths == ["full", "incremental", "incremental"]
    store.close()


def test_an_already_present_column_is_never_a_trigger(tmp_path):
    """Also shows the original bug in miniature: an incremental cycle over a
    NULL column fills only the row that changed."""
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store, with_column=True)
    client = AdoptingClient()

    sync_object(Obj(), client, store, None, _settings(tmp_path))

    assert client.paths == ["incremental"]
    assert _filled(store) == 1  # a03 only — a01/a02 stay NULL
    store.close()


def test_the_watermark_is_restamped_after_a_successful_backfill(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)

    sync_object(Obj(), AdoptingClient(), store, None, _settings(tmp_path))

    assert store.get_watermark("Candidate__c") not in (None, WATERMARK)
    store.close()


# ---------------------------------------------------------------------------
# crash safety
# ---------------------------------------------------------------------------


def test_a_backfill_that_fails_partway_is_retried_in_full_next_cycle(tmp_path):
    """Cycle 1 adds the column and dies mid-extract with one of three rows
    filled. If the watermark survived that, cycle 2 would be incremental and
    the other two rows would be NULL for good."""
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    settings = _settings(tmp_path)

    with pytest.raises(ConnectionError):
        sync_object(Obj(), FailsMidExtract(), store, None, settings)
    assert _filled(store) == 1
    assert store.get_watermark("Candidate__c") is None  # cleared, not restamped

    client = AdoptingClient()
    sync_object(Obj(), client, store, None, settings)

    assert client.paths == ["full"]
    assert _filled(store) == 3
    assert store.get_watermark("Candidate__c") is not None
    store.close()


# ---------------------------------------------------------------------------
# it must not fire when there is nothing to backfill
# ---------------------------------------------------------------------------


def test_first_sync_of_a_new_table_is_simply_full(tmp_path):
    """No stored watermark, table created fresh: full anyway, nothing to
    clear, and the watermark is stamped afterwards like any first sync."""
    store = Store(str(tmp_path / "wh.duckdb"))
    client = AdoptingClient()

    sync_object(Obj(), client, store, None, _settings(tmp_path))

    assert client.paths == ["full"]
    assert store.get_watermark("Candidate__c") is not None
    store.close()


def test_an_empty_object_gaining_a_column_is_harmless(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.ensure_table("Candidate__c", ["Id", "Name", "SystemModstamp"])
    store.set_watermark("Candidate__c", WATERMARK)

    class Empty(AdoptingClient):
        def bulk_query(self, soql):
            self.paths.append("full")
            return iter(())

    client = Empty()
    sync_object(Obj(), client, store, None, _settings(tmp_path))

    assert client.paths == ["full"]
    assert store.get_watermark("Candidate__c") is not None
    store.close()


def test_an_object_with_no_watermark_field_is_unaffected(tmp_path):
    """Objects that always run full extracts have no watermark to drop."""
    store = Store(str(tmp_path / "wh.duckdb"))
    _seed(store)
    client = AdoptingClient()

    class NoWatermark(Obj):
        watermark_field = None

    sync_object(NoWatermark(), client, store, None, _settings(tmp_path))

    assert client.paths == ["full"]
    store.close()
