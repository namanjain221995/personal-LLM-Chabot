"""Objects with zero Salesforce records still get (empty) warehouse tables.

Without this, a configured-but-empty object (Candidate_Email__c holds no
records yet) never appears in DuckDB, and every SQL question about it errors
with "table does not exist" instead of answering 0.
"""
import pandas as pd

from syncworker.storage import Store


def test_an_empty_object_gets_an_empty_table(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.ensure_table("Candidate_Email__c", ["Id", "Name", "SystemModstamp"])
    con = store._con
    assert con.execute('SELECT count(*) FROM "Candidate_Email__c"').fetchone()[0] == 0
    cols = [r[0] for r in con.execute('DESCRIBE "Candidate_Email__c"').fetchall()]
    assert cols == ["Id", "Name", "SystemModstamp"]
    store.close()


def test_ensure_table_never_touches_existing_data(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.upsert("Account", pd.DataFrame([{"Id": "1", "Name": "Acme"}]))
    store.ensure_table("Account", ["Id", "SomethingElse__c"])
    rows = store._con.execute('SELECT Id, Name FROM "Account"').fetchall()
    assert rows == [("1", "Acme")]
    store.close()


def test_a_grown_config_widens_an_empty_table(tmp_path):
    """When field-level security is loosened, an object that still has no
    rows must pick up the new columns anyway — otherwise a query against
    them errors instead of returning empty."""
    store = Store(str(tmp_path / "wh.duckdb"))
    store.ensure_table("Asset", ["Id", "SystemModstamp"])
    store.ensure_table("Asset", ["Id", "ProductFamily", "SystemModstamp"])
    cols = [r[0] for r in store._con.execute('DESCRIBE "Asset"').fetchall()]
    assert "ProductFamily" in cols
    assert store._con.execute('SELECT count(*) FROM "Asset"').fetchone()[0] == 0
    store.close()


def test_data_arriving_later_upserts_into_the_pre_created_table(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.ensure_table("Vendor__c", ["Id", "Name", "SystemModstamp"])
    store.upsert("Vendor__c", pd.DataFrame(
        [{"Id": "1", "Name": "Acme Staffing", "SystemModstamp": "t1"}]))
    rows = store._con.execute('SELECT Id, Name FROM "Vendor__c"').fetchall()
    assert rows == [("1", "Acme Staffing")]
    store.close()
