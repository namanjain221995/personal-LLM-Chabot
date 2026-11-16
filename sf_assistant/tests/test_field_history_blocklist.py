"""get_field_history leaves out history rows of a blocked field that is no column of the parent table (a deleted,
renamed or unsynced field): their OldValue / NewValue would show the secret (2026-10-20 review). Since phase 1
(2026-10-26) only the very-high fields are blocked, so the rows of a date of birth are shown. Fake data only."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant.config import load_records_config  # noqa: E402
from sf_assistant.tools.open_records import open_records  # noqa: E402
from warehouse_fixture import USER_1, make_knowledge, make_warehouse, open_fixture, sid  # noqa: E402

OLD_SECRET, NEW_SECRET = "old-pw-not-real", "new-pw-not-real"


def _tools(tmp_path: Path, extra_rows: list[tuple]):
    wh = make_warehouse(tmp_path)
    con = duckdb.connect(str(wh))
    con.executemany('INSERT INTO raw."Interview__History" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [list(r) for r in extra_rows])
    con.close()
    return open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(make_knowledge(tmp_path)))


def test_history_rows_of_a_blocked_field_that_is_no_column_are_left_out(tmp_path):
    rows = [(sid("017", 90), "false", sid("a0A", 1), USER_1, "2026-09-08T10:00:00.000Z", "Old_Password__c", "Text",
             OLD_SECRET, NEW_SECRET),
            (sid("017", 91), "false", sid("a0A", 2), USER_1, "2026-09-09T10:00:00.000Z", "Date_of_Birth__c", "Date",
             "1990-01-01", "1991-02-02")]
    tools = _tools(tmp_path, rows)
    try:
        tools.begin_question("")
        calls = [{"object": "Interview__c"}, {"object": "Interview__c", "record": "I-0001"},
                 {"object": "Interview__c", "count_by": "new_value"}, {"object": "Interview__c", "count_by": "field"},
                 {"object": "Interview__c", "count_by": "month"}]
        for args in calls:
            out = tools.call("get_field_history", dict(args))
            text = json.dumps(out, default=str)
            assert out["ok"], (args, out)
            for secret in (OLD_SECRET, NEW_SECRET, "Old_Password__c"):
                assert secret not in text, (args, secret)
        out = tools.call("get_field_history", {"object": "Interview__c"})["result"]
        assert {t["field"] for t in out["tracked_fields"]} == {"record created", "Interview_Status__c", "Recruiter__c",
                                                                "Date_of_Birth__c"}   # phase 1: no longer blocked
        assert out["total_count"] == 5      # the fixture's rows without the passcode and EntityId, the password row
        assert "1991-02-02" in json.dumps(out)   # the date of birth row is shown
    finally:
        tools.close()


def test_without_such_rows_the_statements_stay_the_same(tmp_path):
    """The extra check only adds a statement when the history table holds such a row (parity with the notebook)."""
    tools = open_fixture(tmp_path)
    try:
        tools.begin_question("")
        tools.call("get_field_history", {"object": "Interview__c"})
        statements = [st.sql for ev in tools.evidence for st in ev.statements]
        tracked = [s for s in statements if s.startswith('SELECT h."Field" AS field, COUNT(*) AS changes')]
        assert len(statements) == 2 and len(tracked) == 1
    finally:
        tools.close()
