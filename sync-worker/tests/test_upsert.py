import pandas as pd

from syncworker.storage import Store, normalize_records


def _df(rows):
    return pd.DataFrame(rows)


def test_first_upsert_creates_table(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    n = store.upsert(
        "Account",
        _df(
            [
                {"Id": "001A", "Name": "Acme", "SystemModstamp": "2026-07-01T00:00:00Z"},
                {"Id": "001B", "Name": "Globex", "SystemModstamp": "2026-07-02T00:00:00Z"},
            ]
        ),
    )
    assert n == 2
    rows = store._con.execute('SELECT Id, Name FROM "Account" ORDER BY Id').fetchall()
    assert rows == [("001A", "Acme"), ("001B", "Globex")]
    store.close()


def test_upsert_replaces_changed_rows_without_duplicating(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.upsert(
        "Account",
        _df(
            [
                {"Id": "001A", "Name": "Acme", "SystemModstamp": "t1"},
                {"Id": "001B", "Name": "Globex", "SystemModstamp": "t1"},
            ]
        ),
    )
    # second batch: 001B changed, 001C new
    store.upsert(
        "Account",
        _df(
            [
                {"Id": "001B", "Name": "Globex Renamed", "SystemModstamp": "t2"},
                {"Id": "001C", "Name": "Initech", "SystemModstamp": "t2"},
            ]
        ),
    )
    rows = store._con.execute(
        'SELECT Id, Name, SystemModstamp FROM "Account" ORDER BY Id'
    ).fetchall()
    assert rows == [
        ("001A", "Acme", "t1"),
        ("001B", "Globex Renamed", "t2"),
        ("001C", "Initech", "t2"),
    ]
    # no duplicated Ids
    dupes = store._con.execute(
        'SELECT Id FROM "Account" GROUP BY Id HAVING count(*) > 1'
    ).fetchall()
    assert dupes == []
    store.close()


def test_upsert_dedupes_ids_within_one_batch(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.upsert(
        "Contact",
        _df(
            [
                {"Id": "003A", "Email": "old@x.com"},
                {"Id": "003A", "Email": "new@x.com"},
            ]
        ),
    )
    rows = store._con.execute('SELECT Id, Email FROM "Contact"').fetchall()
    assert rows == [("003A", "new@x.com")]
    store.close()


def test_upsert_empty_batch_is_noop(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    assert store.upsert("Account", pd.DataFrame()) == 0
    store.close()


def test_upsert_handles_new_column_appearing_later(tmp_path):
    store = Store(str(tmp_path / "wh.duckdb"))
    store.upsert("Lead", _df([{"Id": "00QA", "Company": "Acme"}]))
    store.upsert("Lead", _df([{"Id": "00QB", "Company": "Globex", "Rating": "Hot"}]))
    rows = store._con.execute(
        'SELECT Id, Company, Rating FROM "Lead" ORDER BY Id'
    ).fetchall()
    assert rows == [("00QA", "Acme", None), ("00QB", "Globex", "Hot")]
    store.close()


def test_normalize_records_makes_values_string_or_none():
    records = normalize_records(
        [
            {"Id": "001A", "IsClosed": True, "Amount": 1200.5, "Note": None, "Blank": ""},
        ]
    )
    assert records == [
        {"Id": "001A", "IsClosed": "true", "Amount": "1200.5", "Note": None, "Blank": None}
    ]


def test_all_null_first_batch_does_not_wedge_the_column(tmp_path):
    """Live incident (Interview__c.Employment_Type__c): an all-None column in
    the CREATE TABLE batch let DuckDB resolve the NULL type to INTEGER, and
    the first real value ('Full Time') then failed EVERY cycle. Staging is
    now pinned to string dtype, and mistyped columns are healed in place."""
    store = Store(str(tmp_path / "wh.duckdb"))
    store.upsert(
        "Interview__c",
        _df([{"Id": "a0A1", "Employment_Type__c": None, "SystemModstamp": "t1"}]),
    )
    # Would raise ConversionException before the fix:
    store.upsert(
        "Interview__c",
        _df([{"Id": "a0A2", "Employment_Type__c": "Full Time", "SystemModstamp": "t2"}]),
    )
    rows = store._con.execute(
        'SELECT Id, Employment_Type__c FROM "Interview__c" ORDER BY Id'
    ).fetchall()
    assert rows == [("a0A1", None), ("a0A2", "Full Time")]
    store.close()


def test_a_pre_existing_mistyped_column_is_healed(tmp_path):
    """Tables damaged by the old NULL-type inference heal on the next batch."""
    store = Store(str(tmp_path / "wh.duckdb"))
    store._con.execute(
        'CREATE TABLE "Interview__c" (Id VARCHAR, Employment_Type__c INTEGER)'
    )
    store._con.execute('INSERT INTO "Interview__c" VALUES (\'a0A1\', NULL)')

    store.upsert(
        "Interview__c",
        _df([{"Id": "a0A2", "Employment_Type__c": "Full Time"}]),
    )

    types = dict(
        (r[0], r[1])
        for r in store._con.execute('DESCRIBE "Interview__c"').fetchall()
    )
    assert types["Employment_Type__c"] == "VARCHAR"
    rows = store._con.execute(
        'SELECT Id, Employment_Type__c FROM "Interview__c" ORDER BY Id'
    ).fetchall()
    assert rows == [("a0A1", None), ("a0A2", "Full Time")]
    store.close()
