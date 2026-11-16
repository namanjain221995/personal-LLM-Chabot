"""The term resolver in the catalog and the value grounding (docs/resolver_integration.md, steps (3) and (4)).

With the spelling help on (``catalog.resolver`` set), an object, field or value name that no exact tier knows is read
by the resolver: one ``corrected`` match is used inside a record tool call (the correction channel ``READ_AS`` is set
then) and stated in a note, in ``result["read_as"]`` and in ``QueryEvidence.read_as``; an ``ambiguous`` (or
``exact``, ``corrected`` outside a tool call) reading becomes a did-you-mean in the usual error (same codes); ``none``
keeps today's text. With the switch off (``SFA_RESOLVER=off``; on is the default since 2026-10-27) every result is
the old one; the old expectations stay in tests/test_engine_catalog.py and tests/test_engine_values.py, and the
switch-on twins of the ones that change are here.
Fake data only: tests/warehouse_fixture.py and a made-up word list; the live test prints and stores no name.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import os
import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import sf_assistant.engine.values as V  # noqa: E402
from sf_assistant import config as C  # noqa: E402
from sf_assistant.config import ResolverConfig, load_records_config  # noqa: E402
from sf_assistant.engine.catalog import READ_AS, RecordError, in_tool_call, no_read_as  # noqa: E402
from sf_assistant.engine.types import QueryEvidence  # noqa: E402
from sf_assistant.engine.values import ground_value  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")
WORDS = ("assigned scheduled complete completed interview interviews status the and how many records show list count "
         "open").split()


def _open(tmp: Path, on: bool):
    pytest.importorskip("duckdb")
    from warehouse_fixture import make_knowledge, make_warehouse

    from sf_assistant.tools.open_records import open_records

    words = tmp / "words.txt"
    words.write_text("\n".join(WORDS) + "\n", encoding="utf-8")
    wh, kn = make_warehouse(tmp), make_knowledge(tmp)
    rc = ResolverConfig(enabled=True, lexicon=(str(words),)) if on else ResolverConfig(enabled=False)
    return open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(kn), rc)


@pytest.fixture(scope="module")
def on(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV + tuple(C.RESOLVER_ENV_VARS.values()):
            mp.delenv(name, raising=False)
        t = _open(tmp_path_factory.mktemp("on"), True)
    assert t.catalog.resolver is not None, t.resolver_lines
    yield t
    t.close()


@pytest.fixture(scope="module")
def off(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV + tuple(C.RESOLVER_ENV_VARS.values()):
            mp.delenv(name, raising=False)
        t = _open(tmp_path_factory.mktemp("off"), False)
    assert t.catalog.resolver is None
    yield t
    t.close()


@contextlib.contextmanager
def in_call():
    """What RecordTools.call does around a call with the switch on: a fresh correction channel."""
    sink: list[dict] = []
    token = READ_AS.set(sink)
    try:
        yield sink
    finally:
        READ_AS.reset(token)


def _raises(fn, *args) -> RecordError:
    with pytest.raises(RecordError) as exc:
        fn(*args)
    return exc.value


def _f(cat, obj, fld):
    return cat.objects[obj].fields[fld]


# --------------------------------------------------------------------------------------------- switch off
def test_switch_off_nothing_is_read_as_even_inside_a_tool_call(off):
    cat = off.catalog
    sp = cat.objects["Interview__c"]
    with in_call() as sink:
        err = _raises(cat.resolve_object, "Interviw")
        assert err.code == "E1" and "Did you mean" not in str(err) and not hasattr(err, "did_you_mean")
        assert str(err).startswith("unknown object 'Interviw'. Closest: ")
        err = _raises(cat.field, sp, "Interview_Stauts__c")
        assert err.code == "E2" and str(err) == cat.unknown_field_text(sp, "Interview_Stauts__c")
        err = _raises(ground_value, cat, "Interview__c", _f(cat, "Interview__c", "Interview_Status__c"), "Completd")
        assert err.code == "E4" and " Close: " in str(err) and "Did you mean" not in str(err)
    assert sink == []


def test_switch_off_tool_results_and_evidence_carry_no_read_as(off):
    res = off.call("count_records", {"object": "Interviw"})
    assert not res["ok"] and res["error"].startswith("unknown object 'Interviw'. Closest: ")
    ev = off.evidence[-1]
    assert ev.error_code == "E1" and "_read_as" not in ev.__dict__ and "_did_you_mean" not in ev.__dict__
    res = off.call("count_records", {"object": "Interview__c"})
    assert res["ok"] and "read_as" not in res["result"]
    ev = off.evidence[-1]
    assert ev.read_as == [] and ev.did_you_mean == []
    # the spelling help adds no dataclass field: the evidence of a switch-off run is field for field the old one
    assert [f.name for f in dataclasses.fields(QueryEvidence)][-4:] == ["error_code", "fields_used", "not_found",
                                                                        "names_matched"]


def test_correct_names_never_reach_the_resolver(on, monkeypatch):
    """Every exact tier runs first: a correct object, field or value is resolved exactly as with the switch off."""
    cat = on.catalog
    seen: list[str] = []
    real = cat.resolver.resolve_term
    monkeypatch.setattr(cat.resolver, "resolve_term", lambda text, *a, **k: seen.append(str(text)) or real(text, *a, **k))
    cat._resolutions = {}
    sp = cat.objects["Interview__c"]
    with in_call() as sink:
        assert cat.resolve_object("interview__c").api == "Interview__c"
        assert cat.resolve_object("Employees").api == "Recruiter__c"
        assert cat.resolve_object("client interview").api == "Interview__c"
        assert cat.field(sp, "date of interview").api == "Date_of_Interview__c"
        assert cat.field(sp, "interview_status__C").api == "Interview_Status__c"
        g = ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Employment_Type__c"), "full-time")
        assert g.values == ["Full Time"] and g.read_as is None
        assert ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Interview_Outcome__c"),
                            "reference check").zero
    assert seen == [] and sink == []


# --------------------------------------------------------------------------------------------- (3) objects
def test_object_typo_is_used_only_inside_a_tool_call(on):
    cat = on.catalog
    assert not in_tool_call()
    err = _raises(cat.resolve_object, "Interviw")       # outside a tool call: a did-you-mean, never used on its own
    assert err.code == "E1" and str(err).startswith("unknown object 'Interviw'. Did you mean Interview__c? Closest: ")
    assert str(err).endswith("Call find_objects with the user's words.")
    assert err.did_you_mean == {"typed": "Interviw", "kind": "object", "suggestions": ["Interview__c"]}
    with in_call() as sink:
        assert in_tool_call()
        assert cat.resolve_object("Interviw").api == "Interview__c"
        assert cat.resolve_object("Interviw").api == "Interview__c"      # the same correction is kept once
    assert sink == [{"arg": None, "typed": "Interviw", "used": "Interview__c", "kind": "object",
                     "target": "Interview__c", "how": "closest object name",
                     "note": "object 'Interviw' read as 'Interview__c' (closest object name)"}]
    with in_call(), no_read_as():                        # a search query: never read as a name
        assert not in_tool_call()
        assert _raises(cat.resolve_object, "Interviw").code == "E1"


def test_flag_on_twin_of_the_interviewz_resolution(on):
    """Twin of tests/test_engine_catalog.py::test_resolution_and_suggestions ('Interviewz', switch off: E1)."""
    cat = on.catalog
    err = _raises(cat.resolve_object, "Interviewz")
    assert err.code == "E1" and "Interview__c" in str(err) and "find_objects" in str(err)
    assert "Did you mean Interview__c?" in str(err) and "Closest: " in str(err)
    with in_call():
        assert cat.resolve_object("Interviewz").api == "Interview__c"
    assert _raises(cat.resolve_object, "AccountHistory").code == "E1c"   # E1c comes before any reading
    assert _raises(cat.resolve_object, "Organization").code == "E1c"


def test_ambiguous_and_exact_readings_are_did_you_means_with_the_old_text(on):
    cat = on.catalog
    with in_call() as sink:
        err = _raises(cat.resolve_object, "Iternal Interview")      # one close spelling, too far for a correction
        assert err.code == "E1"
        assert str(err).startswith("unknown object 'Iternal Interview'. Did you mean Internal_Interview__c? "
                                   "Closest: ")
        assert str(err).endswith(". Call find_objects with the user's words.")
        assert err.did_you_mean["suggestions"] == ["Internal_Interview__c"]
        err = _raises(cat.resolve_object, "Programs Version")       # the resolver's wider normalising: offered only
        assert err.code == "E1" and "Did you mean Program_Version__c?" in str(err)
        err = _raises(cat.resolve_object, "Zzyzx Qwerty")           # no reading: today's text
        assert str(err).startswith("unknown object 'Zzyzx Qwerty'. Closest: ") and not hasattr(err, "did_you_mean")
    assert sink == []
    err = _raises(cat.resolve_object, "Employe")             # a label unlike the API name is shown with it
    assert err.code == "E1" and "Did you mean Recruiter__c (Employee)?" in str(err)


def test_object_typo_without_a_table_gives_e1b_and_a_sensitive_one_e1d(on):
    with in_call() as sink:
        err = _raises(on.catalog.resolve_object, "Training Mock Reslt")
    assert err.code == "E1b"
    assert str(err).startswith("object 'Training Mock Reslt' read as 'Training_Mock_Result__c' (closest object "
                               "name): Training_Mock_Result__c is documented in the knowledge but has no table")
    assert [e["used"] for e in sink] == ["Training_Mock_Result__c"]
    res = on.call("list_records", {"object": "Candidate Portal Credentail"})
    ev = on.evidence[-1]
    assert not res["ok"] and ev.error_code == "E1d"
    assert [e["used"] for e in ev.read_as] == ["Candidate_Portal_Credential__c"]
    res = on.call("count_records", {"object": "Candidate Portal Credentail",
                                    "filters": [{"field": "Usernam__c", "op": "eq", "value": "x"}]})
    assert not res["ok"] and on.evidence[-1].error_code == "E2c"


# --------------------------------------------------------------------------------------------- (3) fields
def test_field_typo_is_used_inside_a_tool_call_and_listed_first_outside(on):
    cat = on.catalog
    sp = cat.objects["Interview__c"]
    with in_call() as sink:
        assert cat.field(sp, "Interview_Stauts__c").api == "Interview_Status__c"
        assert cat.field(sp, "Intervew Status").api == "Interview_Status__c"
        assert cat.resolve_path(sp, "Recruter__c.Name").text == "Recruiter__c.Name"
    assert [(e["typed"], e["used"], e["target"], e["how"]) for e in sink] == [
        ("Interview_Stauts__c", "Interview_Status__c", "Interview__c.Interview_Status__c", "closest field name"),
        ("Intervew Status", "Interview_Status__c", "Interview__c.Interview_Status__c", "closest field name"),
        ("Recruter__c", "Recruiter__c", "Interview__c.Recruiter__c", "closest field name")]
    assert sink[0]["note"] == "field 'Interview_Stauts__c' read as 'Interview_Status__c' (closest field name of " \
                              "Interview__c)"
    err = _raises(cat.field, sp, "Interview_Stauts__c")
    assert err.code == "E2" and str(err).startswith("Interview__c has no field 'Interview_Stauts__c'. Closest: "
                                                    "Interview_Status__c (")
    assert "describe_fields" in str(err) and err.did_you_mean["suggestions"] == ["Interview_Status__c"]
    with in_call() as sink:                                 # ambiguous (words missing): listed first, never used
        err = _raises(cat.field, sp, "Bill Rate")
    assert sink == [] and err.code == "E2" and "Closest: Client_Bill_Rate__c (" in str(err)


def test_the_exact_api_name_of_another_objects_field_is_never_read_as_this_ones(on, monkeypatch):
    """The agent picked the wrong object: E2 keeps "is a field of B; check the object" and binds nothing, even when
    the name is a close spelling of a field of this object (the did-you-means still list it)."""
    cat = on.catalog
    sp = cat.objects["Interview__c"]
    other = next(o for o in cat.objects.values() if o.has_table and o.api != "Interview__c")
    twin = dataclasses.replace(sp.fields["Interview_Status__c"], api="Interview_Stauts__c")
    monkeypatch.setitem(other.fields, "Interview_Stauts__c", twin)
    with in_call() as sink:
        err = _raises(cat.field, sp, "Interview_Stauts__c")
    assert sink == [] and err.code == "E2"
    assert f"'Interview_Stauts__c' is a field of {other.api}; check the object." in str(err)
    assert err.did_you_mean["suggestions"] == ["Interview_Status__c"]
    with in_call() as sink:                                  # not another object's name: read as before
        assert cat.field(sp, "Interview_Statsu__c").api == "Interview_Status__c"
    assert [e["used"] for e in sink] == ["Interview_Status__c"]


def test_a_value_is_never_read_as_one_with_other_numbers(on, monkeypatch):
    """'01:55 PM' is no typo of '01:15 PM' (another time), nor 'H11B' of 'H1B': no binding, and the did-you-means
    keep the typed numbers ('9:30 AM' -> '09:30 AM' first); with none left E4 keeps today's Close: list."""
    from sf_assistant.knowledge.resolver import Candidate, Resolution

    cat = on.catalog
    sp = cat.objects["Interview__c"]
    f = sp.fields["Interview_Status__c"]
    stored = [("01:15 PM", 9), ("09:30 AM", 7), ("11:30 PM", 2)]
    entry = dataclasses.replace(cat.values_of("Interview__c", f.api), values=stored, truncated=False)
    monkeypatch.setitem(cat.values, ("Interview__c", f.api), entry)
    pre = "Interview__c.Interview_Status__c="

    def reading(decision, *vals):
        cands = tuple(Candidate("value", pre + v, v, "x", "phrase", 0.9, "Interview__c", "stored") for v in vals)
        return Resolution("x", decision, vals[0] if decision == "corrected" else None, vals, cands)

    readings = {"01:55 PM": reading("corrected", "01:15 PM"), "9:30 AM": reading("ambiguous", "11:30 PM", "01:15 PM"),
                "1:15 PM": reading("ambiguous", "11:30 PM")}
    monkeypatch.setattr(cat, "spelling", lambda text, kinds, *a, **k:
                        readings.get(str(text)) if tuple(kinds) == ("value",) else None)
    with in_call() as sink:
        err = _raises(V.ground_value, cat, "Interview__c", f, "01:55 PM")
    assert sink == [] and err.code == "E4" and "read as" not in str(err)
    assert "Did you mean" not in str(err) and " Close: " in str(err)      # the resolver's '01:15 PM' is left out
    err = _raises(V.ground_value, cat, "Interview__c", f, "9:30 AM")
    assert "Did you mean: '09:30 AM' (7)?" in str(err) and "11:30 PM' (2)?" not in str(err)
    err = _raises(V.ground_value, cat, "Interview__c", f, "1:15 PM")
    assert "Did you mean: '01:15 PM' (9)?" in str(err)
    assert V._numbers("09:30 AM") == V._numbers("9:30 AM") == [9, 30] and V._number_key("09:30 AM") == "9:30 am"


def test_field_typos_keep_the_sensitivity_and_sync_checks(on):
    cat = on.catalog
    sp = cat.objects["Interview__c"]
    with in_call():
        err = _raises(cat.field, sp, "Old_Stats__c")          # documented, not synced: E2b with the reading stated
    assert err.code == "E2b"
    assert str(err) == ("field 'Old_Stats__c' read as 'Old_Status__c' (closest field name of Interview__c): "
                        "Interview__c.Old_Status__c is documented in the knowledge but is not synced to the warehouse, "
                        "so it cannot be queried. Use Interview_Status__c instead.")
    res = on.call("count_records", {"object": "Interview__c",
                                    "filters": [{"field": "Zoom_Pascode__c", "op": "eq", "value": "x"}]})
    ev = on.evidence[-1]
    assert not res["ok"] and ev.error_code == "E2c" and "Zoom_Passcode__c is a sensitive field" in res["error"]
    assert [e["used"] for e in ev.read_as] == ["Zoom_Passcode__c"]
    res = on.call("group_records", {"object": "Interview__c", "group_by": "Zoom_Pascode__c"})
    assert not res["ok"] and on.evidence[-1].error_code == "E2c"


def test_a_describe_fields_query_is_never_read_as_a_field(on):
    res = on.call("describe_fields", {"object": "Interview__c", "query": "Intervew Status"})
    assert res["ok"] and "read_as" not in res["result"]
    assert on.evidence[-1].read_as == []
    assert "Interview_Status__c" in [f["field"] for f in res["result"]["fields"]][:3]   # the search still finds it


# --------------------------------------------------------------------------------------------- (4) values
def test_close_value_binds_inside_a_tool_call_with_its_note(on):
    cat = on.catalog
    f = _f(cat, "Interview__c", "Interview_Outcome__c")
    with in_call() as sink:
        g = ground_value(cat, "Interview__c", f, "Ofer Received")
    assert g.values == ["Offer Received"] and not g.zero and g.read_as == ("Ofer Received", "Offer Received")
    assert g.notes[0] == "'Ofer Received' read as 'Offer Received' (closest stored value of Interview_Outcome__c)"
    assert "'Offer Received' is also stored in Interview_Status__c (2 records, legacy value)" in g.notes
    assert sink == [{"arg": None, "typed": "Ofer Received", "used": "Offer Received", "kind": "value",
                     "target": "Interview__c.Interview_Outcome__c=Offer Received", "how": "closest stored value",
                     "note": g.notes[0]}]
    from sf_assistant.tools.record_tools import _ALSO_RE

    assert not _ALSO_RE.match(g.notes[0])


def test_flag_on_twin_of_the_e4_close_clause(on, off):
    """Twin of tests/test_engine_values.py::test_unknown_value_lists_stored_counts_and_cross_field_hint: a reading
    with suggestions replaces 'Close:' with 'Did you mean: ...?' and keeps every other section; 'Offer' (no clear
    reading, the resolver says none) keeps the old text exactly."""
    status_on, status_off = (_f(t.catalog, "Interview__c", "Interview_Status__c") for t in (on, off))
    with in_call():
        e_on = _raises(ground_value, on.catalog, "Interview__c", status_on, "Offer")
    e_off = _raises(ground_value, off.catalog, "Interview__c", status_off, "Offer")
    assert e_on.code == e_off.code == "E4" and str(e_on) == str(e_off)
    assert "Close: 'Offer Received' (2)" in str(e_on)
    outcome = _f(on.catalog, "Interview__c", "Interview_Outcome__c")
    err = _raises(ground_value, on.catalog, "Interview__c", outcome, "Ofer Received")   # corrected, outside a call
    msg = str(err)
    assert err.code == "E4" and " Close: " not in msg
    assert msg.startswith("'Ofer Received' is not a value of Interview__c.Interview_Outcome__c. Stored values "
                          "(records): Ghosted 2, Offer Received 2")
    assert "Knowledge values with no records: NA, Reference Check." in msg
    assert "Did you mean: 'Offer Received' (2)?" in msg and msg.endswith("Use one of them exactly, or op contains.")
    assert err.did_you_mean == {"typed": "Ofer Received", "kind": "value", "suggestions": ["Offer Received"]}
    with in_call() as sink:                                 # ambiguous (a word missing): a did-you-mean, not bound
        err = _raises(ground_value, on.catalog, "Interview__c", outcome, "Moved to Round")
    assert sink == [] and "Did you mean: 'Moved to Next Round' (1)?" in str(err)


def test_close_knowledge_value_with_no_records_binds_through_the_zero_path(on):
    cat = on.catalog
    with in_call() as sink:
        g = ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Interview_Status__c"), "Resceduled")
    assert g.values == ["Rescheduled"] and g.zero
    assert g.notes[:2] == ["'Resceduled' read as 'Rescheduled' (closest picklist value of Interview_Status__c)",
                           "'Rescheduled' is a valid picklist value of Interview_Status__c; no stored records have it"]
    assert sink[0]["how"] == "closest picklist value"


def test_close_value_never_binds_with_a_truncated_index_or_for_a_person(on, monkeypatch):
    cat = on.catalog
    f = _f(cat, "Interview__c", "Interview_Status__c")
    entry = cat.values_of("Interview__c", "Interview_Status__c")
    monkeypatch.setattr(entry, "truncated", True)
    with in_call() as sink:
        assert ground_value(cat, "Interview__c", f, "Completd") is None      # may be stored beyond the index
    assert sink == []
    monkeypatch.setattr(entry, "truncated", False)
    monkeypatch.setattr(V, "_is_person_name", lambda _c, _t: True)
    with in_call() as sink:
        err = _raises(ground_value, cat, "Interview__c", f, "Completd")
    assert sink == [] and err.code == "E4" and "Did you mean: 'Completed' (5)?" in str(err)


def test_multipicklist_and_text_of_another_object_through_a_path(on):
    cat = on.catalog
    with in_call() as sink:
        g = ground_value(cat, "Interview__c", _f(cat, "Interview__c", "Evaluation_Parameter__c"), "Codng")
        assert g.values == ["Coding"]
        g = ground_value(cat, "Recruiter__c", _f(cat, "Recruiter__c", "Employment_Status__c"), "Activ Employee",
                         "Recruiter__c.Employment_Status__c")
        assert g.values == ["Active Employee"]
    assert g.notes[0] == ("'Activ Employee' read as 'Active Employee' (closest stored value of "
                          "Recruiter__c.Employment_Status__c)")
    assert [e["target"] for e in sink] == ["Interview__c.Evaluation_Parameter__c=Coding",
                                           "Recruiter__c.Employment_Status__c=Active Employee"]


# --------------------------------------------------------------------------------------------- end to end
def test_count_with_typos_in_object_field_and_value(on):
    res = on.call("count_records", {"object": "Interviw", "filters": [
        {"field": "Interview_Stauts__c", "op": "eq", "value": "Completd"}]})
    assert res["ok"], res["error"]
    r = res["result"]
    assert r["count"] == 5 and r["applied"]["filters"] == ["Interview_Status__c = 'Completed'"]
    assert r["read_as"] == [
        {"arg": "object", "typed": "Interviw", "used": "Interview__c", "kind": "object", "target": "Interview__c",
         "how": "closest object name"},
        {"arg": "filters[0].field", "typed": "Interview_Stauts__c", "used": "Interview_Status__c", "kind": "field",
         "target": "Interview__c.Interview_Status__c", "how": "closest field name"},
        {"arg": "filters[0].value", "typed": "Completd", "used": "Completed", "kind": "value",
         "target": "Interview__c.Interview_Status__c=Completed", "how": "closest stored value"}]
    assert r["notes"][:3] == ["object 'Interviw' read as 'Interview__c' (closest object name)",
                              "field 'Interview_Stauts__c' read as 'Interview_Status__c' (closest field name of "
                              "Interview__c)",
                              "'Completd' read as 'Completed' (closest stored value of Interview_Status__c)"]
    assert list(r)[-2:] == ["read_as", "source"]
    ev = on.evidence[-1]
    assert ev.ok and ev.read_as == r["read_as"] and ev.did_you_mean == []
    assert not in_tool_call()                               # the channel is reset after the call


def test_each_value_of_an_in_list_is_grounded_on_its_own(on):
    res = on.call("count_records", {"object": "Internal Intervew", "filters": [
        {"field": "Stauts__c", "op": "in", "values": ["Completd", "Sheduled"]}]})
    assert res["ok"], res["error"]
    r = res["result"]
    assert r["count"] == 3 and r["applied"]["filters"] == ["Status__c in ('Completed', 'Scheduled')"]
    assert [(e["arg"], e["used"]) for e in r["read_as"]] == [
        ("object", "Internal_Interview__c"), ("filters[0].field", "Status__c"),
        ("filters[0].values[0]", "Completed"), ("filters[0].values[1]", "Scheduled")]


def test_failed_calls_keep_their_codes_and_record_the_did_you_mean(on):
    cases = [("count_records", {"object": "Iternal Interview"}, "E1", "object"),
             ("count_records", {"object": "Training Mock Reslt"}, "E1b", None),
             ("count_records", {"object": "Interview__c", "filters": [
                 {"field": "Interview_Outcome__c", "op": "eq", "value": "Moved to Round"}]}, "E4", "value"),
             ("count_records", {"object": "Interview__c", "filters": [
                 {"field": "Old_Stats__c", "op": "eq", "value": "Open"}]}, "E2b", None)]
    for name, args, code, dym in cases:
        res = on.call(name, args)
        ev = on.evidence[-1]
        assert not res["ok"] and ev.error_code == code, (args, res["error"])
        assert [d["kind"] for d in ev.did_you_mean] == ([dym] if dym else []), args


def test_read_as_survives_the_payload_cap(on):
    big = {"query_id": "q1", "object": "Interview__c", "read_as": [{"arg": "object", "typed": "Interviw"}],
           "rows": [{"x": "y" * 50} for _ in range(400)], "extra": "z" * 20_000, "source": "s"}
    fit = on._fit(big, 3_000)
    assert fit["read_as"] == big["read_as"] and "extra" not in fit


def test_correct_calls_give_the_same_results_with_the_switch_on(on, off):
    calls = [("count_records", {"object": "Interview__c", "filters": [
                 {"field": "Interview_Status__c", "op": "eq", "value": "Completed"}]}),
             ("group_records", {"object": "Interview__c", "group_by": "Interview_Outcome__c"}),
             ("get_field_values", {"object": "Internal_Interview__c", "field": "Status__c"}),
             ("describe_fields", {"object": "Interview__c", "query": "status"}),
             ("count_records", {"object": "Interview__c", "filters": [
                 {"field": "Interview_Status__c", "op": "eq", "value": "Offer"}]})]
    for t in (on, off):
        t.begin_question("")
    for name, args in calls:
        a, b = on.call(name, args), off.call(name, args)
        assert a == b, (name, args)


# ------------------------------------------------------------------------ step (2) on the catalog's BM25 indexes
def test_catalog_search_indexes_read_misspelt_words_only_with_the_switch_on(on, off):
    """Catalog.attach_resolver gives the object index and every field index the expander (metadata kinds):
    find_objects states what its search read (``read_as``, a search map, never an answer-line correction); off,
    none of this."""
    assert off.catalog._object_index.expand is None
    assert off.catalog.objects["Interview__c"].field_index().expand is None
    assert off.catalog.search_read_as("internl intervew") == {}
    res = off.call("find_objects", {"query": "internl intervew"})
    assert res["ok"] and "read_as" not in res["result"] and res["result"]["objects"] == []
    assert off.catalog.objects["Interview__c"].field_index().search("intervew outcme", k=1) == []

    assert on.catalog._object_index.expand is not None
    res = on.call("find_objects", {"query": "internl intervew"})
    assert res["ok"] and res["result"]["read_as"] == {"internl intervew": "internal interview"}
    assert res["result"]["objects"][0]["object"] == "Internal_Interview__c"
    assert on.evidence[-1].read_as == []   # a search word is no tool argument
    hits = on.catalog.objects["Interview__c"].field_index().search("intervew outcme", k=1)
    assert [h["id"] for h in hits] == ["field:Interview_Outcome__c"]


def test_correct_search_words_never_reach_the_catalog_expander(on, off, monkeypatch):
    seen: list = []
    real = on.catalog._object_index.expand
    monkeypatch.setattr(on.catalog._object_index, "expand", lambda q, unknown: seen.append(q) or real(q, unknown))
    for t in (on, off):
        t.begin_question("")
    for q in ("interview", "internal interview", "candidate training"):
        a, b = on.call("find_objects", {"query": q}), off.call("find_objects", {"query": q})
        assert a == b, q
    assert seen == []


def test_a_correct_word_a_small_index_lacks_is_never_read_as_another(on):
    """The catalog's indexes are small, so many right words of a question are in none of their chunks: such a word
    (here 'complete', in the word list; the field index only has 'completed') is searched as typed, while a misspelt
    one is read."""
    sp = on.catalog.objects["Interview__c"]
    assert "complete" not in sp.field_index().words and on.catalog.resolver.is_known_word("complete")
    assert sp.field_index().search_read_as("complete status", k=2)[1] == {}
    assert sp.field_index().search_read_as("complet status", k=2)[1] == {"complet": "completed"}


def test_attach_resolver_reaches_field_indexes_built_before_it(on):
    cat = on.catalog
    r = cat.resolver
    idx = cat.objects["Internal_Interview__c"].field_index()
    try:
        cat.attach_resolver(None)
        assert cat._object_index.expand is None and idx.expand is None and cat.search_read_as("intervew") == {}
    finally:
        cat.attach_resolver(r)
    assert idx.expand is not None and cat._object_index.expand is not None


# --------------------------------------------------------------------------------------------- live (opt-in)
_LIVE = Path(os.environ.get("SFA_LIVE_SNAPSHOT") or C.DEFAULT_WAREHOUSE)


@pytest.mark.skipif(os.environ.get("OKEVAL_RECORDS_LIVE") != "1" or not _LIVE.is_file()
                    or not Path("/usr/share/dict/american-english").is_file(),
                    reason="live: set OKEVAL_RECORDS_LIVE=1 with the local snapshot and the system word lists")
def test_live_object_label_typos_never_bind_another_object():
    """On the real snapshot (counts only, nothing printed): one letter dropped from each queryable object's label, given
    to count_records, either counts that object (stated in read_as) or fails with an object error; it never counts
    another object."""
    from sf_assistant.tools.open_records import open_records

    t = open_records(load_records_config(warehouse=str(_LIVE), today=dt.date(2026, 10, 13), tz="America/New_York"),
                     str(C.KNOWLEDGE_DIR), ResolverConfig(enabled=True))
    rng = random.Random(7)
    used = wrong = 0
    try:
        assert t.catalog.resolver is not None, t.resolver_lines
        specs = [s for s in t.catalog.queryable() if len(s.label) >= 7 and s.label.isascii()]
        for spec in specs:
            i = rng.choice([j for j in range(1, len(spec.label) - 1) if spec.label[j].isalpha()])
            typo = spec.label[:i] + spec.label[i + 1:]
            if typo.lower() in {s.label.lower() for s in t.catalog.objects.values()}:
                continue
            res = t.call("count_records", {"object": typo})
            ev = t.evidence[-1]
            if res["ok"]:
                used += ev.object == spec.api
                wrong += ev.object != spec.api
                assert [e["kind"] for e in ev.read_as] == ["object"]
            else:
                assert ev.error_code in ("E1", "E1b", "E1c", "E1d"), ev.error_code
    finally:
        t.close()
    assert wrong == 0 and used > len(specs) // 2
