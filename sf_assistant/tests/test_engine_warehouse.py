"""sf_assistant.engine.warehouse: read-only connection, locked settings, timeouts, caps and JSON-safe cells."""

from __future__ import annotations

import datetime as dt
import math
import os
import stat
import sys
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant.engine.types import RecordsUnavailable  # noqa: E402
from sf_assistant.engine.warehouse import Warehouse, WarehouseError, json_cell  # noqa: E402
from warehouse_fixture import make_warehouse  # noqa: E402

ALLOWED = frozenset({("main", "Interview__c"), ("main", "Log_Entry__c"), ("raw", "_sync_meta")})


@pytest.fixture(scope="module")
def wh(tmp_path_factory):
    path = make_warehouse(tmp_path_factory.mktemp("wh"))
    os.chmod(path, 0o644)
    w = Warehouse.open(str(path), timeout_s=5)
    w.set_policy(ALLOWED, frozenset({"zoom_passcode__c"}))
    yield w
    w.close()


def test_open_makes_file_private_and_reads_schema(wh):
    assert wh.made_private is True
    assert stat.S_IMODE(os.stat(wh.path).st_mode) == 0o600
    assert wh.columns()["Interview__c"]["Date_of_Interview__c"] == "DATE"
    assert wh.columns()["Interview__c"]["CreatedDate"] == "TIMESTAMP"
    assert wh.sync_times()["Interview__c"] == dt.datetime(2026, 10, 13, 14, 30)
    assert wh.org() == {"IsSandbox": True, "TimeZoneSidKey": "America/New_York", "FiscalYearStartMonth": 1}


def test_read_only_and_locked(wh):
    cur = wh._con.cursor()
    with pytest.raises(duckdb.Error):
        cur.execute("CREATE TABLE x (a INTEGER)")
    with pytest.raises(duckdb.Error):
        cur.execute("SELECT * FROM read_csv('/etc/passwd')")
    with pytest.raises(duckdb.Error):
        cur.execute("ATTACH ':memory:' AS other")
    with pytest.raises(duckdb.Error):
        cur.execute("INSTALL httpfs")
    with pytest.raises(duckdb.Error):
        wh._con.execute("SET threads=1")
    assert cur.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "UTC"


def test_timeout_helper_interrupts_and_connection_stays_usable(wh):
    cur = wh._con.cursor()
    with pytest.raises(duckdb.InterruptException):
        Warehouse._execute(cur, "SELECT count(*) FROM range(100000000000)", [], 0.2)
    st = wh.run('SELECT COUNT(*) AS n FROM main."Interview__c" AS t0')
    assert st.rows == [[10]]


def test_run_turns_interrupt_into_warehouse_error(wh, monkeypatch):
    def boom(cur, sql, params, timeout_s):
        raise duckdb.InterruptException("INTERRUPT Error: Interrupted!")
    monkeypatch.setattr(Warehouse, "_execute", staticmethod(boom))
    with pytest.raises(WarehouseError, match=r"query stopped after 3 s") as exc:
        wh.run('SELECT COUNT(*) AS n FROM main."Interview__c" AS t0', timeout_s=3)
    assert exc.value.kind == "timeout"


def test_run_guard_and_duckdb_errors(wh):
    with pytest.raises(WarehouseError) as exc:
        wh.run('SELECT t0."Id" FROM main."Account" AS t0')
    assert exc.value.kind == "guard" and "not allowed" in exc.value.detail
    with pytest.raises(WarehouseError) as exc:
        wh.run('SELECT t0."Zoom_Passcode__c" FROM main."Interview__c" AS t0')
    assert exc.value.kind == "guard"
    with pytest.raises(WarehouseError) as exc:
        wh.run('SELECT t0."NoSuchColumn" FROM main."Interview__c" AS t0')
    assert exc.value.kind == "error" and len(str(exc.value)) <= 340


def test_row_and_byte_caps(wh):
    st = wh.run('SELECT t0."Name" FROM main."Interview__c" AS t0 ORDER BY 1', max_rows=3)
    assert st.row_count == 3 and st.truncated is True
    st = wh.run('SELECT t0."Message__c" FROM main."Log_Entry__c" AS t0', max_rows=100, max_bytes=12_000)
    assert st.truncated is True and st.row_count == 2
    st = wh.run('SELECT COUNT(*) AS n FROM main."Interview__c" AS t0 WHERE t0."Interview_Outcome__c" = ?', ["Ghosted"])
    assert st.rows == [[2]] and st.params == ["Ghosted"] and st.columns == ["n"] and not st.truncated


def test_introspect_takes_constants_only(wh):
    with pytest.raises(WarehouseError):
        wh.introspect('SELECT t0."Id" FROM main."Account" AS t0')


def test_json_cells():
    assert json_cell(Decimal("5.00")) == 5.0 and isinstance(json_cell(Decimal("5.00")), float)
    assert json_cell(Decimal("7")) == 7 and isinstance(json_cell(Decimal("7")), int)
    assert json_cell(dt.date(2026, 10, 13)) == "2026-10-13"
    assert json_cell(dt.datetime(2026, 10, 13, 4, 5, 6)) == "2026-10-13 04:05:06"
    assert json_cell(dt.time(8, 30)) == "08:30:00"
    assert json_cell(math.nan) is None and json_cell(math.inf) is None
    assert json_cell(b"\x00\x01") == "<binary>"
    assert json_cell([Decimal("1.5"), dt.date(2026, 1, 1)]) == [1.5, "2026-01-01"]


def test_missing_file_and_forbidden_path(tmp_path):
    with pytest.raises(RecordsUnavailable, match="no warehouse snapshot"):
        Warehouse.open(str(tmp_path / "none.duckdb"))
    with pytest.raises(RecordsUnavailable, match="refusing"):
        Warehouse.open("/data/warehouse.duckdb")


def test_invalid_file(tmp_path):
    p = tmp_path / "bad.duckdb"
    p.write_bytes(b"garbage" * 1000)
    with pytest.raises(RecordsUnavailable, match="invalid warehouse snapshot"):
        Warehouse.open(str(p))
