"""sf_assistant.config and the RecordsStatus / SnapshotInfo lines of sf_assistant.engine.types."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

from sf_assistant.config import RecordsConfig, load_records_config  # noqa: E402
from sf_assistant.engine.types import RecordsStatus, SnapshotInfo  # noqa: E402
from sf_assistant.config import DEFAULT_WAREHOUSE, RECORDS_ENV_VARS  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in RECORDS_ENV_VARS.values():
        monkeypatch.delenv(name, raising=False)


def test_defaults():
    """max_steps is 12 (RECORDS_SPEC §21.10): the lookup nudges, E4h and find_references use rounds."""
    c = load_records_config()
    assert c == RecordsConfig()
    assert c.warehouse == str(DEFAULT_WAREHOUSE) and c.warehouse.endswith("okeval_run/warehouse.duckdb")
    assert (c.container, c.tz, c.today, c.timeout_s, c.call_budget_s, c.max_steps, c.max_age_h) == \
        ("sf-local-ai-orchestrator-1", None, None, 10.0, 20.0, 12, 24.0)


def test_env_then_overrides(monkeypatch):
    monkeypatch.setenv("OKEVAL_RECORDS_TZ", "Asia/Kolkata")
    monkeypatch.setenv("OKEVAL_RECORDS_TODAY", "2026-10-13")
    monkeypatch.setenv("OKEVAL_RECORDS_TIMEOUT_S", "5")
    monkeypatch.setenv("OKEVAL_RECORDS_MAX_STEPS", "15")
    monkeypatch.setenv("OKEVAL_RECORDS_MAX_AGE_H", "48")
    monkeypatch.setenv("OKEVAL_WAREHOUSE", "/tmp/x.duckdb")
    c = load_records_config()
    assert (c.tz, c.today, c.timeout_s, c.max_steps, c.max_age_h, c.warehouse) == \
        ("Asia/Kolkata", dt.date(2026, 10, 13), 5.0, 15, 48.0, "/tmp/x.duckdb")
    c = load_records_config(tz="America/New_York", today=None, warehouse=None)
    assert c.tz == "America/New_York" and c.today == dt.date(2026, 10, 13) and c.warehouse == "/tmp/x.duckdb"


@pytest.mark.parametrize("env,value,message", [
    ("OKEVAL_WAREHOUSE_CONTAINER", "bad name;rm", "OKEVAL_WAREHOUSE_CONTAINER is not a container name"),
    ("OKEVAL_RECORDS_TZ", "Mars/Base", "unknown time zone 'Mars/Base' in OKEVAL_RECORDS_TZ"),
    ("OKEVAL_RECORDS_TODAY", "13/10/2026", "OKEVAL_RECORDS_TODAY must be YYYY-MM-DD"),
    ("OKEVAL_RECORDS_TIMEOUT_S", "500", "OKEVAL_RECORDS_TIMEOUT_S must be a number between 1 and 120"),
    ("OKEVAL_RECORDS_MAX_STEPS", "1", "OKEVAL_RECORDS_MAX_STEPS must be an integer between 2 and 30"),
    ("OKEVAL_RECORDS_MAX_AGE_H", "0", "OKEVAL_RECORDS_MAX_AGE_H must be a number greater than 0"),
])
def test_invalid_env(monkeypatch, env, value, message):
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError) as exc:
        load_records_config()
    assert str(exc.value) == message


def test_unknown_override():
    with pytest.raises(TypeError):
        load_records_config(colour="red")


def _status(**kw) -> RecordsStatus:
    base = dict(snapshot_path="okeval_run/warehouse.duckdb", source="sf-local-ai-orchestrator-1:/data/warehouse.read.duckdb",
                copied_at_utc="2026-10-13 14:56 UTC", copied_at_from="file time", org_is_sandbox=True,
                origin="preprod SANDBOX copy", newest_sync_utc="2026-10-13 14:56 UTC", age_hours=6.2, stale=False,
                time_zone="America/New_York", time_zone_from="Organization.TimeZoneSidKey", today="2026-10-13",
                today_pinned=False, objects=60, duckdb_version="1.5.6")
    base.update(kw)
    return RecordsStatus(**base)


def test_status_line_and_warning():
    assert _status().line() == (
        "records: okeval_run/warehouse.duckdb | preprod SANDBOX copy | copied 2026-10-13 14:56 UTC (file time) | "
        "newest sync 2026-10-13 14:56 UTC (6.2 h ago) | today 2026-10-13 (America/New_York) | 60 objects")
    assert "copied 2026-10-13 14:56 UTC |" in _status(copied_at_from="meta").line()
    assert "today 2026-10-13 pinned (America/New_York)" in _status(today_pinned=True).line()
    assert _status().warning() is None
    w = _status(stale=True, age_hours=31.0).warning()
    assert w == ("WARNING records snapshot is 31.0 h old (newest sync 2026-10-13 14:56 UTC); run ./ask.sh "
                 "--refresh-warehouse")
    assert _status(org_is_sandbox=False, origin="PRODUCTION copy").warning() == \
        "WARNING this snapshot is a PRODUCTION copy (Organization.IsSandbox = false)"


def test_snapshot_info_line():
    info = SnapshotInfo(path=str(DEFAULT_WAREHOUSE), copied_at_utc="2026-10-14 09:01 UTC", bytes=428_617_728,
                        newest_sync_utc="2026-10-14 08:57 UTC", org_is_sandbox=True, duckdb_version="1.5.6")
    assert info.line() == ("snapshot refreshed: okeval_run/warehouse.duckdb (428.6 MB), copied 2026-10-14 09:01 UTC, "
                           "newest sync 2026-10-14 08:57 UTC, preprod SANDBOX")
