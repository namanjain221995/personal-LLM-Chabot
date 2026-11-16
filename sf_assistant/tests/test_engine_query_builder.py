"""sf_assistant.engine.query_builder: exact SQL text and parameter order of the pure builders (synthetic catalog)."""

from __future__ import annotations

import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant.engine.catalog import RecordError  # noqa: E402
from sf_assistant.engine.periods import parse_period  # noqa: E402
from sf_assistant.engine.query_builder import (BuildCtx, Frag, Scope, count_sql, filter_frag, group_sql, join_frags,  # noqa: E402
                         percentage_sql, period_frag, related_frag, trend_sql)
from warehouse_fixture import open_fixture  # noqa: E402

NORM = "lower(trim(regexp_replace(replace(x.\"Name\", chr(160), ' '), '\\s+', ' ', 'g')))"
ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = open_fixture(tmp_path_factory.mktemp("q"))
    yield t
    t.close()


@pytest.fixture
def cat(tools):
    return tools.catalog


@pytest.fixture
def ctx(cat):
    return BuildCtx(cat, "America/New_York", dt.date(2026, 10, 13))


def test_frag_add_and_join_keep_param_order():
    f = Frag("a = ?", (1,)) + Frag(" AND b = ?", (2,))
    assert f == Frag("a = ? AND b = ?", (1, 2))
    j = join_frags(" OR ", [Frag("x = ?", ("x",)), Frag(""), Frag("y = ?", ("y",))])
    assert j == Frag("x = ? OR y = ?", ("x", "y"))


def test_count_with_filters_path_period_and_any_of(cat, ctx):
    sp = cat.objects["Interview__c"]
    sc = Scope(cat, sp)
    conds = [filter_frag(sc, {"field": "Interview_Outcome__c", "op": "eq", "value": "offer received"}, ctx).frag,
             filter_frag(sc, {"field": "Recruiter__c.Department__c.Name", "op": "eq", "value": "Dept One"}, ctx).frag,
             filter_frag(sc, {"field": "Client_Bill_Rate__c", "op": "gte", "value": "100"}, ctx).frag]
    pf, pa = period_frag(sc, cat.resolve_path(sp, "CreatedDate"), parse_period("this month", ctx.today), ctx)
    conds.append(pf)
    conds.append(Frag("(") + join_frags(" OR ", [
        filter_frag(sc, {"field": "Interview_Status__c", "op": "eq", "value": "Completed"}, ctx).frag,
        filter_frag(sc, {"field": "Employment_Type__c", "op": "eq", "value": "full-time"}, ctx).frag]) + Frag(")"))
    q = count_sql(sc, conds)
    # A path ending in .Name compares the last lookup with the normalised name (no join of the named object).
    assert q.sql == (
        'SELECT COUNT(*) AS n FROM main."Interview__c" AS t0 LEFT JOIN main."Recruiter__c" AS t1 ON t1."Id" = '
        't0."Recruiter__c" WHERE t0."Interview_Outcome__c" = ? AND t1."Department__c" IN (SELECT x."Id" FROM '
        'main."Department__c" AS x WHERE ' + NORM + ' = ?) AND t0."Client_Bill_Rate__c" >= ? AND '
        't0."CreatedDate" >= ? AND t0."CreatedDate" < ? AND (t0."Interview_Status__c" = ? OR '
        't0."Employment_Type__c" = ?)')
    assert q.params == ("Offer Received", "dept one", Decimal("100"), dt.datetime(2026, 10, 1, 4, 0),
                        dt.datetime(2026, 11, 1, 4, 0), "Completed", "Full Time")
    assert "LIMIT" not in q.sql
    assert pa["utc_from"] == "2026-10-01 04:00" and pa["from"] == "2026-10-01" and pa["to_exclusive"] == "2026-11-01"
    assert pa["to"] == "2026-10-31"  # last day inside the window, for answers without date arithmetic


def test_exists_with_child_filters_and_min_count(cat, ctx):
    sc = Scope(cat, cat.objects["Candidate_Training__c"])
    rf, text, _, _o = related_frag(sc, {"child_object": "Session__c", "exists": True,
                                        "filters": [{"field": "Purpose__c", "op": "eq", "value": "training"}]}, 1, ctx)
    assert rf.sql == ('EXISTS (SELECT 1 FROM main."Session__c" AS c1 WHERE c1."Candidate_Training__c" = t0."Id" AND '
                      'c1."Purpose__c" = ?)')
    assert rf.params == ("Training",)
    assert text == "has a Session__c with Purpose__c = 'Training' (via Session__c.Candidate_Training__c)"
    rf, text, _, _o = related_frag(sc, {"child_object": "Session__c", "exists": False}, 2, ctx)
    assert rf.sql == 'NOT EXISTS (SELECT 1 FROM main."Session__c" AS c2 WHERE c2."Candidate_Training__c" = t0."Id")'
    rf, text, _, _o = related_frag(sc, {"child_object": "Session__c", "exists": True, "min_count": 2}, 1, ctx)
    assert rf.sql == '(SELECT COUNT(*) FROM main."Session__c" AS c1 WHERE c1."Candidate_Training__c" = t0."Id") >= ?'
    assert rf.params == (2,)


def test_child_with_two_lookups_needs_via_field(cat, ctx):
    sc = Scope(cat, cat.objects["Recruiter__c"])
    with pytest.raises(RecordError) as exc:
        related_frag(sc, {"child_object": "Session__c", "exists": True}, 1, ctx)
    assert exc.value.code == "E9b" and "Host_User__c" in str(exc.value) and "Co_Host__c" in str(exc.value)
    rf, _t, _n, _o = related_frag(sc, {"child_object": "Session__c", "exists": True, "via_field": "Co_Host__c"}, 1, ctx)
    assert 'c1."Co_Host__c" = t0."Id"' in rf.sql


def test_percentage_filter_params_come_before_where_params(cat, ctx):
    sc = Scope(cat, cat.objects["Internal_Interview__c"])
    part = filter_frag(sc, {"field": "Status__c", "op": "eq", "value": "Completed"}, ctx)
    base = filter_frag(sc, {"field": "Interview_Type__c", "op": "eq", "value": "Mock"}, ctx)
    q = percentage_sql(sc, [base.frag], part.frag)
    assert q.sql == ('SELECT COUNT(*) FILTER (WHERE t0."Status__c" = ?) AS part, COUNT(*) AS base FROM '
                     'main."Internal_Interview__c" AS t0 WHERE t0."Interview_Type__c" = ?')
    assert q.params == ("Completed", "Mock")


def test_group_by_lookup_uses_left_join_and_unresolved_label(cat):
    sp = cat.objects["Interview__c"]
    sc = Scope(cat, sp)
    g = sc.col(cat.resolve_path(sp, "Recruiter__c"), label=True)
    q = group_sql(sc, [], g, "count", None)
    assert q.sql == (
        'SELECT t1."Id" AS gid, COALESCE(t1."Name", CASE WHEN t0."Recruiter__c" IS NULL THEN \'(blank)\' WHEN t1."Id" '
        'IS NULL THEN \'(unresolved id)\' ELSE \'(no name)\' END) AS grp, COUNT(*) AS value FROM main."Interview__c" AS '
        't0 LEFT JOIN main."Recruiter__c" AS t1 ON t1."Id" = t0."Recruiter__c" GROUP BY 1, 2 ORDER BY value DESC NULLS '
        'LAST, grp ASC LIMIT 1001')


def test_group_plain_field_and_three_hop_path(cat):
    sp = cat.objects["Internal_Interview__c"]
    sc = Scope(cat, sp)
    g = sc.col(cat.resolve_path(sp, "Status__c"), label=True)
    assert group_sql(sc, [], g, "count", None).sql == (
        'SELECT COALESCE(CAST(t0."Status__c" AS VARCHAR), \'(blank)\') AS grp, COUNT(*) AS value FROM '
        'main."Internal_Interview__c" AS t0 GROUP BY 1 ORDER BY value DESC NULLS LAST, grp ASC LIMIT 1001')
    dl = cat.objects["Deliverable__c"]
    sc = Scope(cat, dl)
    g = sc.col(cat.resolve_path(dl, "Candidate_Training__c.Program_Version__c.Program__c"), label=True)
    sql = group_sql(sc, [], g, "count", None).sql
    assert sql.count("LEFT JOIN") == 3 and 'AS t3 ON t3."Id" = t2."Program__c"' in sql
    assert sql.startswith('SELECT t3."Id" AS gid, COALESCE(t3."Name", CASE WHEN t0."Candidate_Training__c" IS NULL')


def test_shared_join_prefix_is_joined_once(cat, ctx):
    sp = cat.objects["Interview__c"]
    sc = Scope(cat, sp)
    filter_frag(sc, {"field": "Recruiter__c.Name", "op": "eq", "value": "Recruiter A"}, ctx)
    filter_frag(sc, {"field": "Recruiter__c.Employment_Status__c", "op": "eq", "value": "Active Employee"}, ctx)
    assert sc.from_sql().count("LEFT JOIN") == 1


def test_split_by_and_min_value(cat):
    sp = cat.objects["Interview__c"]
    sc = Scope(cat, sp)
    g = sc.col(cat.resolve_path(sp, "Interview_Status__c"), label=True)
    s = sc.col(cat.resolve_path(sp, "Employment_Type__c"), label=True)
    q = group_sql(sc, [], g, "count", None, split=s, having=Frag("COUNT(*) >= ?", (Decimal("2"),)))
    assert q.sql == ('SELECT COALESCE(CAST(t0."Interview_Status__c" AS VARCHAR), \'(blank)\') AS grp, '
                     'COALESCE(CAST(t0."Employment_Type__c" AS VARCHAR), \'(blank)\') AS split, COUNT(*) AS value FROM '
                     'main."Interview__c" AS t0 GROUP BY 1, 2 HAVING COUNT(*) >= ? ORDER BY grp ASC, split ASC LIMIT 1001')
    assert q.params == (Decimal("2"),)


def test_timestamp_trend_bucket_binds_the_time_zone(cat, ctx):
    sp = cat.objects["Interview__c"]
    sc = Scope(cat, sp)
    q = trend_sql(sc, [Frag('t0."Interview_Status__c" = ?', ("Completed",))], sc.col(cat.resolve_path(sp, "CreatedDate")),
                  "month", ctx, "count", None)
    assert q.sql == ('SELECT CAST(date_trunc(\'month\', (t0."CreatedDate" AT TIME ZONE \'UTC\') AT TIME ZONE ?) AS DATE) '
                     'AS bucket, COUNT(*) AS value FROM main."Interview__c" AS t0 WHERE t0."Interview_Status__c" = ? '
                     'GROUP BY 1 ORDER BY 1')
    assert q.params == ("America/New_York", "Completed")
    q = trend_sql(sc, [], sc.col(cat.resolve_path(sp, "Date_of_Interview__c")), "week", ctx, "count", None)
    assert q.sql.startswith('SELECT CAST(date_trunc(\'week\', t0."Date_of_Interview__c") AS DATE) AS bucket') and not q.params


def test_ne_includes_blanks_and_like_escaping(cat, ctx):
    sp = cat.objects["Interview__c"]
    sc = Scope(cat, sp)
    out = filter_frag(sc, {"field": "Interview_Outcome__c", "op": "ne", "value": "Ghosted"}, ctx)
    assert out.frag.sql == '(t0."Interview_Outcome__c" IS NULL OR t0."Interview_Outcome__c" <> ?)'
    assert out.text == "Interview_Outcome__c <> 'Ghosted' (blank included)"
    assert any("blank" in n for n in out.notes)
    out = filter_frag(sc, {"field": "StartTime_Text__c", "op": "contains", "value": "5%_\\"}, ctx)
    assert out.frag.sql == 't0."StartTime_Text__c" ILIKE ? ESCAPE \'\\\''
    assert out.frag.params == ("%5\\%\\_\\\\%",)
    out = filter_frag(sc, {"field": "StartTime_Text__c", "op": "starts_with", "value": "09"}, ctx)
    assert out.frag.params == ("09%",)


def test_multipicklist_boolean_reference_and_record_type(cat, ctx):
    sp = cat.objects["Interview__c"]
    sc = Scope(cat, sp)
    out = filter_frag(sc, {"field": "Evaluation_Parameter__c", "op": "contains", "value": "coding"}, ctx)
    assert out.frag == Frag("list_contains(string_split(t0.\"Evaluation_Parameter__c\", ';'), ?)", ("Coding",))
    out = filter_frag(sc, {"field": "Evaluation_Parameter__c", "op": "in", "values": ["Coding", "Design"]}, ctx)
    assert out.frag.sql == "list_has_any(string_split(t0.\"Evaluation_Parameter__c\", ';'), ?)"
    out = filter_frag(sc, {"field": "Recruiter__c", "op": "eq", "value": "Recruiter A"}, ctx)
    assert out.frag == Frag('t0."Recruiter__c" IN (SELECT x."Id" FROM main."Recruiter__c" AS x WHERE ' + NORM + ' = ?)',
                            ("recruiter a",))
    assert out.text == "Recruiter__c.Name = 'Recruiter A'"
    out = filter_frag(sc, {"field": "Recruiter__c", "op": "eq", "value": "a06000000000001AAA"}, ctx)
    assert out.frag == Frag('t0."Recruiter__c" = ?', ("a06000000000001AAA",))
    out = filter_frag(sc, {"field": "RecordTypeId", "op": "eq", "value": "initial call"}, ctx)
    assert out.frag == Frag('t0."RecordTypeId" = ANY(?)', (["012000000000004AAA"],))
    assert out.text == "RecordTypeId = 'Initial Call'"
    ii = cat.objects["Internal_Interview__c"]
    out = filter_frag(Scope(cat, ii), {"field": "Human_Total_Score__c", "op": "gt", "value": "25%"}, ctx)
    assert out.frag == Frag('t0."Human_Total_Score__c" > ?', (Decimal("25"),))


# ----------------------------------------------------------------------------------- name grounding (RECORDS_SPEC §21.4)

def test_a_lookup_and_its_name_path_build_the_same_filter(cat, ctx):
    from sf_assistant.engine.query_builder import name_norm_sql

    assert name_norm_sql('x."Name"') == NORM
    ii = cat.objects["Internal_Interview__c"]
    a = filter_frag(Scope(cat, ii), {"field": "Interviewer__c", "op": "eq", "value": "Delta  Person"}, ctx)
    sc = Scope(cat, ii)
    b = filter_frag(sc, {"field": "Interviewer__c.Name", "op": "eq", "value": "Delta  Person"}, ctx)
    assert a.frag == b.frag == Frag('t0."Interviewer__c" IN (SELECT x."Id" FROM main."Recruiter__c" AS x WHERE ' + NORM
                                    + ' = ?)', ("delta person",))
    assert a.text == b.text == "Interviewer__c.Name = 'Delta  Person'"
    assert sc.from_sql() == 'main."Internal_Interview__c" AS t0'      # no join of the named object
    chk = b.checks[0]
    assert (chk.target, chk.op, chk.field.api, chk.obj, chk.on_base, chk.names) == (
        "Recruiter__c", "eq", "Interviewer__c", "Internal_Interview__c", True, ["Delta  Person"])
    assert chk.frag is b.frag
    out = filter_frag(Scope(cat, ii), {"field": "Interviewer__c", "op": "in", "values": ["Delta Person", "Echo One"]}, ctx)
    assert out.frag.params == (["delta person", "echo one"],) and "= ANY(?)" in out.frag.sql
    out = filter_frag(Scope(cat, ii), {"field": "Interviewer__c", "op": "contains", "value": "Delta"}, ctx)
    assert out.checks[0].pattern == "%Delta%" and "ILIKE ?" in out.frag.sql


def test_a_two_hop_name_path_and_related_filters_carry_name_checks(cat, ctx):
    sc = Scope(cat, cat.objects["Session__c"])
    out = filter_frag(sc, {"field": "Candidate_Training__c.Program_Version__c.Name", "op": "eq", "value": "PV-1"}, ctx)
    assert out.frag.sql.startswith('t1."Program_Version__c" IN (SELECT x."Id" FROM main."Program_Version__c" AS x')
    chk = out.checks[0]
    assert chk.on_base is False and chk.field.api == "Program_Version__c" and chk.obj == "Candidate_Training__c"
    assert out.text == "Candidate_Training__c.Program_Version__c.Name = 'PV-1'"
    sc = Scope(cat, cat.objects["Candidate_Training__c"])
    rf, text, _n, outs = related_frag(sc, {"child_object": "Session__c", "exists": True,
                                           "filters": [{"field": "Host_User__c", "op": "eq", "value": "Recruiter A"}]},
                                      1, ctx)
    assert len(outs) == 1 and outs[0].checks[0].field.api == "Host_User__c" and outs[0].checks[0].target == "Recruiter__c"
    assert "c1.\"Host_User__c\" IN (SELECT" in rf.sql


def test_numbered_names_bind_the_record_id(tools, cat):
    from warehouse_fixture import REC_B2

    ctx = BuildCtx(cat, "America/New_York", dt.date(2026, 10, 13),
                   names=lambda objs, norm: tools._name_rows(None, objs, norm))
    sc = Scope(cat, cat.objects["Internal_Interview__c"])
    out = filter_frag(sc, {"field": "Interviewer__c", "op": "eq", "value": "Recruiter B (#2)"}, ctx)
    assert out.frag == Frag('t0."Interviewer__c" = ?', (REC_B2,))
    assert out.text == "Interviewer__c.Name = 'Recruiter B (#2)'" and out.checks[0].picked
    out = filter_frag(sc, {"field": "Interviewer__c", "op": "in", "values": ["Recruiter B (#2)", "Delta Person"]}, ctx)
    assert out.frag.sql.startswith('(t0."Interviewer__c" = ? OR t0."Interviewer__c" IN (SELECT')
    with pytest.raises(RecordError) as exc:
        filter_frag(sc, {"field": "Interviewer__c", "op": "eq", "value": "Recruiter B (#3)"}, ctx)
    assert exc.value.code == "E4h" and str(exc.value) == (
        "'Recruiter B' has 2 Employee (Recruiter__c) records (#1 Active Employee, #2 Former Employee); there is no #3.")


def test_a_base_name_filter_is_normalised_and_checked(cat, ctx):
    sc = Scope(cat, cat.objects["Recruiter__c"])
    out = filter_frag(sc, {"field": "Name", "op": "eq", "value": "Delta Person"}, ctx)
    assert out.frag == Frag("lower(trim(regexp_replace(replace(t0.\"Name\", chr(160), ' '), '\\s+', ' ', 'g'))) = ?",
                            ("delta person",))
    chk = out.checks[0]
    assert chk.field is None and chk.target == "Recruiter__c" and out.text == "Name = 'Delta Person'"
    out = filter_frag(sc, {"field": "Name", "op": "contains", "value": "Delta"}, ctx)
    assert not out.checks
