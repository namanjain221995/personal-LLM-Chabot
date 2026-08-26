"""Type mapping and typed-view generation.

The cast rules are load-bearing: a wrong one silently NULLs a whole column, a
missing one leaves an amount sorting as text, and a rule that fails mid-migration
can leave an object with no readable relation at all. All three failures are
invisible at runtime, so they are pinned here.

ALL_ORG_TYPES is the complete set of describe types this Salesforce org
actually uses (35,900 fields). Every one is exercised end to end -- mapping
function AND resulting DuckDB view column type.
"""

import duckdb
import pytest

from syncworker.typemap import (
    FieldSpec,
    UnknownSalesforceType,
    cast_expression,
    duckdb_type,
    plan_columns,
    summarize,
    unknown_types,
    validate_timezone,
)
from syncworker.views import (
    build_view_sql,
    existing_view_columns,
    promote_to_raw,
    refresh_view,
    view_timezone_matches,
)

#: (describe type, field count in this org, expected DuckDB type or None=VARCHAR)
#: Counts come from the org's own field-type census and are here as
#: documentation of scale, not as assertions.
ALL_ORG_TYPES = [
    ("reference", 6955, None),
    ("boolean", 6674, "BOOLEAN"),
    ("string", 5716, None),
    ("datetime", 5601, "TIMESTAMP"),
    ("picklist", 3791, None),
    ("id", 1852, None),
    ("textarea", 1297, None),
    ("int", 982, "BIGINT"),
    ("double", 823, "DECIMAL(18,2)"),
    ("anyType", 429, None),
    ("date", 425, "DATE"),
    ("url", 295, None),
    ("complexvalue", 263, None),
    ("currency", 242, "DECIMAL(18,2)"),
    ("phone", 124, None),
    ("email", 122, None),
    ("long", 109, "BIGINT"),
    ("address", 57, None),
    ("percent", 55, "DECIMAL(18,2)"),
    ("time", 34, "TIME"),
    ("multipicklist", 18, None),
    ("base64", 15, None),
    ("encryptedstring", 11, None),
    ("combobox", 8, None),
    ("datacategorygroupreference", 2, None),
]


def spec(name="F__c", type_="string", precision=None, scale=None):
    # describe types arrive in their API casing ("anyType"); FieldSpec
    # normalises, and every caller must go through it.
    return FieldSpec.from_describe(
        {"name": name, "type": type_, "precision": precision, "scale": scale}
    )


# --- every type this org actually uses -------------------------------------

@pytest.mark.parametrize("sf_type,count,expected", ALL_ORG_TYPES)
def test_every_org_type_maps(sf_type, count, expected):
    """No describe type in this org may raise; 265 fields once did."""
    assert duckdb_type(spec(type_=sf_type, precision=18, scale=2)) == expected


@pytest.mark.parametrize("sf_type,count,expected", ALL_ORG_TYPES)
def test_every_org_type_produces_the_expected_view_column(tmp_path, sf_type, count, expected):
    """The mapping is only half the contract -- the VIEW must report it too."""
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR, "F__c" VARCHAR)')
    specs = [spec("Id", "id"), spec("F__c", sf_type, 18, 2)]
    assert refresh_view(con, "T", specs) is True
    types = dict(existing_view_columns(con, "T"))
    assert types["F__c"] == (expected or "VARCHAR")
    con.close()


def test_the_org_type_list_is_complete():
    """A guard on the guard: 25 types, matching the org's census."""
    assert len(ALL_ORG_TYPES) == 25
    assert len({t for t, _, _ in ALL_ORG_TYPES}) == 25


# --- semantic correctness: what must stay text -----------------------------

@pytest.mark.parametrize(
    "sf_type",
    ["id", "reference", "string", "textarea", "picklist", "multipicklist",
     "email", "phone", "url", "combobox", "anyType", "complexvalue",
     "datacategorygroupreference"],
)
def test_text_types_are_left_alone(sf_type):
    assert duckdb_type(spec(type_=sf_type)) is None


def test_phone_stays_text_even_though_it_looks_numeric():
    # Leading zeros, '+', extensions and formatting are data, not noise.
    assert duckdb_type(spec("Phone", "phone")) is None


def test_salesforce_ids_are_never_numeric():
    # 18-character alphanumerics. reference/id cover ~8,800 fields here.
    for sf_type in ("id", "reference"):
        assert duckdb_type(spec("Candidate__c", sf_type)) is None


def test_anytype_is_polymorphic_so_it_stays_text():
    # One row can hold a number, the next a date. Any cast NULLs the rest.
    assert duckdb_type(spec("Value", "anyType")) is None


def test_complexvalue_is_not_parsed_into_a_struct():
    # REST and Bulk serialise it differently; neither format is guaranteed.
    assert duckdb_type(spec("Blob__c", "complexvalue")) is None


@pytest.mark.parametrize("sf_type", ["base64", "encryptedstring", "address", "location"])
def test_never_synced_types_are_not_given_a_type(sf_type):
    # These never reach the warehouse (compound breaks Bulk CSV; encrypted
    # fields are credentials). If one ever did, text is the safe landing.
    assert duckdb_type(spec(type_=sf_type)) is None


# --- numeric width ---------------------------------------------------------

def test_whole_numbers_become_bigint_not_decimal():
    assert duckdb_type(spec("Cycles__c", "double", 18, 0)) == "BIGINT"


@pytest.mark.parametrize(
    "precision,scale,expected",
    [(15, 6, "DECIMAL(15,6)"), (18, 15, "DECIMAL(18,15)"),
     (18, 4, "DECIMAL(18,4)"), (5, 2, "DECIMAL(5,2)"), (3, 1, "DECIMAL(3,1)")],
)
def test_declared_scale_is_honoured_not_flattened_to_two(precision, scale, expected):
    # This org has 66 columns at 15 decimals and 64 at 6. A blanket
    # DECIMAL(18,2) would silently truncate every one of them.
    assert duckdb_type(spec("N__c", "double", precision, scale)) == expected


def test_currency_keeps_at_least_two_decimals():
    # 7 Currency fields here declare scale 0. If Salesforce ever returns
    # '65.50' a scale-0 column would round it away without a word.
    assert duckdb_type(spec("Rate__c", "currency", 18, 0)) == "DECIMAL(18,2)"
    assert duckdb_type(spec("Total__c", "currency", 18, 2)) == "DECIMAL(18,2)"


def test_percent_uses_declared_scale_and_is_not_rescaled():
    # Salesforce stores the DISPLAYED value: 12.5 means 12.5%, not 0.125.
    assert duckdb_type(spec("Agreed__c", "percent", 5, 2)) == "DECIMAL(5,2)"


def test_missing_precision_and_scale_do_not_crash():
    # describe omits them for some fields; a None must not become DECIMAL(None).
    assert duckdb_type(spec("N__c", "double", None, None)) == "BIGINT"
    assert duckdb_type(spec("C__c", "currency", None, None)) == "DECIMAL(18,2)"


def test_precision_never_exceeds_duckdbs_limit():
    assert duckdb_type(spec("Wide__c", "double", 99, 4)) == "DECIMAL(38,4)"


def test_precision_is_widened_when_it_would_be_below_scale():
    assert duckdb_type(spec("Odd__c", "double", 1, 6)) == "DECIMAL(7,6)"


def test_int_and_long_are_both_bigint():
    assert duckdb_type(spec("N", "int")) == "BIGINT"
    assert duckdb_type(spec("N", "long")) == "BIGINT"


# --- unknown types fail at column granularity, never object granularity ----

def test_an_unmapped_type_raises_from_the_strict_primitive():
    with pytest.raises(UnknownSalesforceType):
        duckdb_type(spec("Mystery__c", "quantumtype"))


def test_but_cast_expression_falls_back_to_text():
    # One unanticipated type must cost that column its type, not the object
    # its view -- which previously left the object unreachable entirely.
    assert cast_expression(spec("Mystery__c", "quantumtype")) == '"Mystery__c"'


def test_unknown_types_are_reported_so_the_fallback_is_not_silent():
    found = unknown_types([spec("A", "id"), spec("B__c", "quantumtype")])
    assert found == {"quantumtype": ["B__c"]}


def test_an_unknown_type_does_not_prevent_the_object_being_typed(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Amt__c" VARCHAR, "Weird__c" VARCHAR)')
    con.execute("""INSERT INTO main."T" VALUES ('27000','x')""")
    specs = [spec("Amt__c", "currency", 18, 2), spec("Weird__c", "quantumtype")]
    assert refresh_view(con, "T", specs) is True
    types = dict(existing_view_columns(con, "T"))
    assert types["Amt__c"] == "DECIMAL(18,2)"
    assert types["Weird__c"] == "VARCHAR"
    assert con.execute('SELECT count(*) FROM "T"').fetchone()[0] == 1
    con.close()


# --- cast expressions ------------------------------------------------------

def test_typed_column_uses_try_cast_never_cast():
    out = cast_expression(spec("Amount__c", "currency", 18, 2))
    assert out == 'TRY_CAST("Amount__c" AS DECIMAL(18,2)) AS "Amount__c"'
    assert "TRY_CAST" in out and " CAST(" not in out


def test_identifier_quotes_are_escaped():
    assert '"od""d"' in cast_expression(spec('od"d', "date"))


# --- timezone --------------------------------------------------------------

def test_datetime_converts_out_of_utc_into_the_org_zone():
    out = cast_expression(spec("CreatedDate", "datetime"), "America/New_York")
    assert "AT TIME ZONE 'UTC'" in out
    assert "AT TIME ZONE 'America/New_York'" in out


def test_date_and_time_are_never_shifted():
    # Salesforce Date/Time carry no instant; converting them invents an error.
    for sf_type in ("date", "time"):
        assert "AT TIME ZONE" not in cast_expression(spec("D__c", sf_type), "America/New_York")


def test_utc_is_a_no_op_not_a_round_trip():
    # The default must leave the value byte-identical to what Salesforce sent.
    assert "AT TIME ZONE" not in cast_expression(spec("C", "datetime"), "UTC")
    assert "AT TIME ZONE" not in cast_expression(spec("C", "datetime"), None)


def test_numeric_columns_are_untouched_by_timezone():
    out = cast_expression(spec("Amt__c", "currency", 18, 2), "America/New_York")
    assert "AT TIME ZONE" not in out


@pytest.mark.parametrize("bad", ["", "  ", "'; DROP TABLE x --", "Asia/Kolkata; --", "1Bad"])
def test_a_timezone_that_is_not_a_zone_name_is_refused(bad):
    # The value is interpolated into SQL, so it is validated, not trusted.
    with pytest.raises(ValueError):
        validate_timezone(bad)


def test_real_zone_names_pass():
    for good in ("UTC", "America/New_York", "Asia/Kolkata", "Etc/GMT+5"):
        assert validate_timezone(good) == good


def test_utc_timestamps_are_not_shifted_by_the_default(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR, "CreatedDate" VARCHAR)')
    con.execute("""INSERT INTO main."T" VALUES ('a','2026-08-17T21:00:00.000+0000')""")
    refresh_view(con, "T", [spec("Id", "id"), spec("CreatedDate", "datetime")], "UTC")
    assert str(con.execute('SELECT CreatedDate FROM "T"').fetchone()[0]) == "2026-08-17 21:00:00"
    con.close()


def test_org_timezone_shifts_the_value_and_survives_dst(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR, "CreatedDate" VARCHAR)')
    con.execute("""INSERT INTO main."T" VALUES
        ('a','2026-08-17T21:00:00.000+0000'), ('b','2026-01-17T21:00:00.000+0000')""")
    refresh_view(con, "T", [spec("Id", "id"), spec("CreatedDate", "datetime")],
                 "America/New_York")
    rows = dict(con.execute('SELECT Id, CreatedDate FROM "T"').fetchall())
    assert str(rows["a"]) == "2026-08-17 17:00:00"   # EDT, UTC-4
    assert str(rows["b"]) == "2026-01-17 16:00:00"   # EST, UTC-5
    con.close()


# --- migration safety ------------------------------------------------------

def test_a_failing_view_does_not_leave_the_object_unreachable(tmp_path, monkeypatch):
    """The bug this guards: promote moved the table, the view then failed, and
    main.<obj> did not exist at all -- rows safe in raw, unreachable to every
    query. Promote and CREATE VIEW must be one transaction."""
    import syncworker.views as views

    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR, "Amt__c" VARCHAR)')
    con.execute("""INSERT INTO main."T" VALUES ('a','27000')""")
    monkeypatch.setattr(
        views, "build_view_sql",
        lambda *a, **k: 'CREATE OR REPLACE VIEW main."T" AS SELECT nope FROM raw."T";',
    )
    assert refresh_view(con, "T", [spec("Id", "id"), spec("Amt__c", "currency", 18, 2)]) is False
    # rolled back: still queryable, and no half-migrated raw table left behind
    assert con.execute('SELECT count(*) FROM main."T"').fetchone()[0] == 1
    assert con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='raw' AND table_name='T'"
    ).fetchone()[0] == 0
    con.close()


def test_refresh_is_idempotent_and_preserves_rows(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR, "Amt__c" VARCHAR)')
    con.execute("""INSERT INTO main."T" VALUES ('a','27000'),('b','999')""")
    specs = [spec("Id", "id"), spec("Amt__c", "currency", 18, 2)]
    for _ in range(3):
        assert refresh_view(con, "T", specs) is True
    assert con.execute('SELECT count(*) FROM "T"').fetchone()[0] == 2
    assert con.execute('SELECT count(*) FROM raw."T"').fetchone()[0] == 2
    con.close()


def test_a_new_field_appears_in_the_view_on_the_next_refresh(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR)')
    refresh_view(con, "T", [spec("Id", "id")])
    con.execute('ALTER TABLE raw."T" ADD COLUMN "Amt__c" VARCHAR')
    refresh_view(con, "T", [spec("Id", "id"), spec("Amt__c", "currency", 18, 2)])
    assert dict(existing_view_columns(con, "T"))["Amt__c"] == "DECIMAL(18,2)"
    con.close()


def test_a_changed_salesforce_type_retypes_the_column(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("F__c" VARCHAR)')
    refresh_view(con, "T", [spec("F__c", "string")])
    assert dict(existing_view_columns(con, "T"))["F__c"] == "VARCHAR"
    refresh_view(con, "T", [spec("F__c", "currency", 18, 2)])
    assert dict(existing_view_columns(con, "T"))["F__c"] == "DECIMAL(18,2)"
    con.close()


def test_a_changed_scale_retypes_the_column(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("F__c" VARCHAR)')
    refresh_view(con, "T", [spec("F__c", "double", 18, 2)])
    assert dict(existing_view_columns(con, "T"))["F__c"] == "DECIMAL(18,2)"
    refresh_view(con, "T", [spec("F__c", "double", 15, 6)])
    assert dict(existing_view_columns(con, "T"))["F__c"] == "DECIMAL(15,6)"
    con.close()


def test_promote_to_raw_is_idempotent(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR)')
    assert promote_to_raw(con, "T") is True
    assert promote_to_raw(con, "T") is False   # already there, no-op
    con.close()


def test_raw_keeps_the_original_string_when_the_cast_fails(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR, "Amt__c" VARCHAR)')
    con.execute("""INSERT INTO main."T" VALUES ('a','$1,234.56')""")
    refresh_view(con, "T", [spec("Id", "id"), spec("Amt__c", "currency", 18, 2)])
    assert con.execute('SELECT Amt__c FROM main."T"').fetchone()[0] is None
    assert con.execute('SELECT Amt__c FROM raw."T"').fetchone()[0] == "$1,234.56"
    con.close()


# --- skip-if-unchanged -----------------------------------------------------

def _count_rebuilds(monkeypatch):
    """Record every time refresh_view decides to emit a CREATE OR REPLACE VIEW.

    build_view_sql is the decision point: refresh_view only reaches it after
    the staleness check says a rebuild is needed. (DuckDB connections do not
    allow patching `execute`, so the SQL cannot be spied on directly.)"""
    import syncworker.views as views

    calls = []
    original = views.build_view_sql

    def counting(*args, **kwargs):
        calls.append(args[0] if args else None)
        return original(*args, **kwargs)

    monkeypatch.setattr(views, "build_view_sql", counting)
    return calls


def test_an_unchanged_view_is_not_rebuilt(tmp_path, monkeypatch):
    """~1,000 CREATE OR REPLACE VIEW per cycle, each taking the write lock the
    orchestrator's reads compete for, is worth avoiding when nothing changed."""
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR, "Amt__c" VARCHAR)')
    specs = [spec("Id", "id"), spec("Amt__c", "currency", 18, 2)]
    refresh_view(con, "T", specs)

    rebuilds = _count_rebuilds(monkeypatch)
    assert refresh_view(con, "T", specs) is True
    assert rebuilds == []
    con.close()


def test_force_rebuilds_even_when_unchanged(tmp_path, monkeypatch):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR)')
    specs = [spec("Id", "id")]
    refresh_view(con, "T", specs)

    rebuilds = _count_rebuilds(monkeypatch)
    assert refresh_view(con, "T", specs, force=True) is True
    assert rebuilds == ["T"]
    con.close()


def test_a_changed_schema_does_rebuild(tmp_path, monkeypatch):
    """The other half of the contract: skipping must not mean never."""
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR, "Amt__c" VARCHAR)')
    refresh_view(con, "T", [spec("Id", "id"), spec("Amt__c", "string")])

    rebuilds = _count_rebuilds(monkeypatch)
    refresh_view(con, "T", [spec("Id", "id"), spec("Amt__c", "currency", 18, 2)])
    assert rebuilds == ["T"]
    con.close()


def test_a_timezone_change_is_not_skipped(tmp_path):
    """Types do not change when the zone does, so a types-only staleness check
    would silently ignore a new SF_ORG_TIMEZONE."""
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("Id" VARCHAR, "CreatedDate" VARCHAR)')
    con.execute("""INSERT INTO main."T" VALUES ('a','2026-08-17T21:00:00.000+0000')""")
    specs = [spec("Id", "id"), spec("CreatedDate", "datetime")]
    refresh_view(con, "T", specs, "UTC")
    assert str(con.execute('SELECT CreatedDate FROM "T"').fetchone()[0]) == "2026-08-17 21:00:00"
    refresh_view(con, "T", specs, "America/New_York")
    assert str(con.execute('SELECT CreatedDate FROM "T"').fetchone()[0]) == "2026-08-17 17:00:00"
    con.close()


def test_view_timezone_matches_detects_both_directions(tmp_path):
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.execute('CREATE TABLE main."T" ("CreatedDate" VARCHAR)')
    specs = [spec("CreatedDate", "datetime")]
    refresh_view(con, "T", specs, "UTC")
    assert view_timezone_matches(con, "T", "UTC") is True
    assert view_timezone_matches(con, "T", "America/New_York") is False
    refresh_view(con, "T", specs, "America/New_York")
    assert view_timezone_matches(con, "T", "America/New_York") is True
    assert view_timezone_matches(con, "T", "UTC") is False
    con.close()


# --- planning and view SQL -------------------------------------------------

def test_columns_absent_from_the_table_are_skipped():
    # describe reports fields the sync never stored (field-level security,
    # the SYNC_MAX_FIELDS ceiling). Selecting one is a binder error.
    specs = [spec("Id", "id"), spec("Ghost__c", "currency", 18, 2)]
    assert plan_columns(specs, present={"Id"}) == ['"Id"']


def test_duplicate_fields_are_emitted_once():
    assert plan_columns([spec("Id", "id"), spec("Id", "id")]) == ['"Id"']


def test_view_sql_carries_through_columns_describe_did_not_mention():
    # A field adopted between describes must not vanish from the view.
    sql = build_view_sql("Acc", [spec("Id", "id")], present={"Id", "Extra__c"})
    assert '"Extra__c"' in sql
    assert sql.startswith('CREATE OR REPLACE VIEW "main"."Acc"')
    assert 'FROM "raw"."Acc"' in sql


def test_view_sql_is_none_when_there_is_nothing_to_select():
    assert build_view_sql("Acc", [], present=set()) is None


def test_a_missing_object_is_reported_not_raised(tmp_path):
    # A Salesforce object that 404s (deleted upstream) must not stop a cycle.
    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    assert refresh_view(con, "Nonexistent__c", [spec("Id", "id")]) is False
    con.close()


def test_summarize_counts_targets():
    counts = summarize([spec("Id", "id"), spec("A__c", "currency", 18, 2),
                        spec("B__c", "boolean")])
    assert counts == {"VARCHAR": 1, "DECIMAL(18,2)": 1, "BOOLEAN": 1}
