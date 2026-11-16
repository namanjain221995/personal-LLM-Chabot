"""Evaluation fixes of about_people (2026-10-22): the profile keeps the first filled values per record, and a record
joins a person only through that person's identity links; one that several listed people reach is nobody's own.

Model-free, on a copy of the people fixture (``warehouse_fixture.PEOPLE_TABLES`` / ``PEOPLE_KNOWLEDGE``; fake names
only, no digits) with:

- an employee field list longer than the old cut of 10: nine categorical fields and two dates after the status and the
  three lookups ("Ember Full" has every field filled, "Ember Gap" leaves a lookup and three categorical fields blank);
- two employees with the same name ("Charlie Twin") whose Recruiter_Contact__c point to one Recruiter-type account
  with one portal user (User.AccountId) that created two client interviews, and a third "Charlie Twin": a User that no
  identity link reaches;
- seven employees with the same name ("Golf Septet"): the first two point to one account, and only the first has no
  business reference (the one person left out of the listing);
- two employees with different names whose identity links reach one User (review of 2026-10-22): "Yankee Brook" with
  a Recruiter-type person account and its portal user, and "Zulu Arrow" whose Recruiter_User__c points to that portal
  user; the user created three client interviews: one with no recruiter, one of each employee (review of 2026-10-23);
- two employees with the same name ("Quebec Twin", review of 2026-10-23) whose Recruiter_Contact__c point to one
  person account (with a stored e-mail, phone and passport number) and its portal user (with a password change date),
  where the user is the account manager of one client interview and only #2 owns one: #2 ranks first.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import warehouse_fixture as F  # noqa: E402
from warehouse_fixture import _field, _with, sid  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")
DAY = "2025-01-01T00:00:00.000Z"

CATS = ("Band__c", "Desk__c", "Floor__c", "Grade__c", "Level__c", "Shift__c", "Track__c", "Wing__c", "Zone__c")
REC_EF, REC_EG = sid("a06", 60), sid("a06", 61)                    # Ember Full, Ember Gap
REC_CT1, REC_CT2 = sid("a06", 62), sid("a06", 63)                  # Charlie Twin (#1), (#2)
ACC_CT, USER_CT, USER_CT_OTHER = sid("001", 60), sid("005", 60), sid("005", 61)
REC_GS = [sid("a06", 70 + i) for i in range(7)]                    # Golf Septet (#1) ... (#7)
ACC_GS = sid("001", 61)
INT_CT = [sid("a0A", 60), sid("a0A", 61)]
INT_GS = [sid("a0A", 70 + i) for i in range(6)]
REC_YB, REC_ZA = sid("a06", 64), sid("a06", 65)                    # Yankee Brook, Zulu Arrow
ACC_YB, USER_YB, INT_YB = sid("001", 62), sid("005", 62), sid("a0A", 62)
INT_YB_OF = [sid("a0A", 63), sid("a0A", 64)]                       # the user's interviews of Yankee Brook, Zulu Arrow
REC_QT1, REC_QT2 = sid("a06", 66), sid("a06", 67)                  # Quebec Twin (#1), (#2)
ACC_QT, USER_QT, INT_QT = sid("001", 63), sid("005", 63), [sid("a0A", 65), sid("a0A", 66)]


def _tables() -> list:
    t = copy.deepcopy(F.PEOPLE_TABLES)
    cols = {c: None for c in CATS} | {"Date_of_Joining__c": "DATE", "Last_Review_Date__c": "DATE"}
    full = {c: f"{c[:-3]} one" for c in CATS}
    gap = {c: f"{c[:-3]} two" for c in CATS[:6]}                  # Track, Wing and Zone blank
    _with(t, "Recruiter__c", cols, [
        {"Id": REC_EF, "Name": "Ember Full", "CreatedDate": DAY, "Employment_Status__c": "Active Employee",
         "Department__c": F.DEPT_1, "Manager__c": F.REC_OP, "Assigned_Sr_Recruiter__c": F.REC_PQ, **full,
         "Date_of_Joining__c": "2024-03-04", "Last_Review_Date__c": "2026-01-15"},
        {"Id": REC_EG, "Name": "Ember Gap", "CreatedDate": DAY, "Employment_Status__c": "Active Employee",
         "Department__c": F.DEPT_1, "Assigned_Sr_Recruiter__c": F.REC_PQ, **gap, "Date_of_Joining__c": "2025-02-03"},
        {"Id": REC_CT1, "Name": "Charlie Twin", "CreatedDate": DAY, "Employment_Status__c": "Active Employee",
         "Recruiter_Contact__c": ACC_CT},
        {"Id": REC_CT2, "Name": "Charlie Twin", "CreatedDate": DAY, "Employment_Status__c": "Active Employee",
         "Recruiter_Contact__c": ACC_CT},
        *({"Id": rid, "Name": "Golf Septet", "CreatedDate": DAY, "Employment_Status__c": "Active Employee",
           "Recruiter_Contact__c": ACC_GS if i < 2 else None} for i, rid in enumerate(REC_GS)),
        {"Id": REC_YB, "Name": "Yankee Brook", "CreatedDate": DAY, "Employment_Status__c": "Active Employee",
         "Recruiter_Contact__c": ACC_YB},
        {"Id": REC_ZA, "Name": "Zulu Arrow", "CreatedDate": DAY, "Employment_Status__c": "Active Employee",
         "Recruiter_User__c": USER_YB},
        *({"Id": rid, "Name": "Quebec Twin", "CreatedDate": DAY, "Employment_Status__c": "Active Employee",
           "Recruiter_Contact__c": ACC_QT} for rid in (REC_QT1, REC_QT2))])
    _with(t, "Account", {}, [
        {"Id": ACC_CT, "Name": "Charlie Twin", "RecordTypeId": F.RT_RECRUITER, "CreatedDate": DAY,
         "IsPersonAccount": "true"},
        {"Id": ACC_GS, "Name": "Golf Septet", "RecordTypeId": F.RT_RECRUITER, "CreatedDate": DAY,
         "IsPersonAccount": "true"},
        {"Id": ACC_YB, "Name": "Yankee Brook", "RecordTypeId": F.RT_RECRUITER, "CreatedDate": DAY,
         "IsPersonAccount": "true"},
        {"Id": ACC_QT, "Name": "Quebec Twin", "RecordTypeId": F.RT_RECRUITER, "CreatedDate": DAY,
         "IsPersonAccount": "true", "PersonEmail": "qt@example.invalid", "Phone": "000-000-0063",
         "Passport_Number__c": "X0000063"}])
    _with(t, "User", {}, [
        {"Id": USER_CT, "Name": "Charlie Twin", "IsActive": "true", "AccountId": ACC_CT,
         "UserType": "PowerCustomerSuccess"},
        {"Id": USER_CT_OTHER, "Name": "Charlie Twin", "IsActive": "true", "UserType": "Standard"},
        {"Id": USER_YB, "Name": "Yankee Brook", "IsActive": "true", "AccountId": ACC_YB,
         "UserType": "PowerCustomerSuccess"},
        {"Id": USER_QT, "Name": "Quebec Twin", "IsActive": "true", "AccountId": ACC_QT,
         "UserType": "PowerCustomerSuccess", "LastPasswordChangeDate": "2026-01-02T03:04:05.000Z"}])
    # the portal user created two client interviews; Golf Septet (#2) ... (#7) own one client interview each, (#1) none
    _with(t, "Interview__c", {"Account_Manager__c": None}, [
        *({"Id": iid, "Name": f"I-CT-{'ab'[k]}", "IsDeleted": "false", "CreatedDate": DAY, "CreatedById": USER_CT,
           "Interview_Status__c": "Completed"} for k, iid in enumerate(INT_CT)),
        *({"Id": iid, "Name": f"I-GS-{'abcdef'[k]}", "IsDeleted": "false", "CreatedDate": DAY, "CreatedById": F.USER_1,
           "Interview_Status__c": "Completed", "Recruiter__c": REC_GS[k + 1]} for k, iid in enumerate(INT_GS)),
        {"Id": INT_YB, "Name": "I-YB-a", "IsDeleted": "false", "CreatedDate": DAY, "CreatedById": USER_YB,
         "Interview_Status__c": "Completed"},
        *({"Id": iid, "Name": f"I-YB-{'bc'[k]}", "IsDeleted": "false", "CreatedDate": DAY, "CreatedById": USER_YB,
           "Interview_Status__c": "Completed", "Recruiter__c": rec} for k, (iid, rec) in enumerate(zip(INT_YB_OF,
                                                                                                    (REC_YB, REC_ZA)))),
        # Quebec Twin: the shared portal user is the account manager of one interview, (#2) owns the other
        {"Id": INT_QT[0], "Name": "I-QT-a", "IsDeleted": "false", "CreatedDate": DAY, "CreatedById": F.USER_1,
         "Interview_Status__c": "Completed", "Account_Manager__c": USER_QT},
        {"Id": INT_QT[1], "Name": "I-QT-b", "IsDeleted": "false", "CreatedDate": DAY, "CreatedById": F.USER_1,
         "Interview_Status__c": "Completed", "Recruiter__c": REC_QT2}])
    return t


def _knowledge() -> list:
    k = copy.deepcopy(F.PEOPLE_KNOWLEDGE)
    rec = next(x for x in k if x["object"]["api_name"] == "Recruiter__c")
    rec["fields"] += [_field(c, c[:-3], "picklist", f"{c[:-3]} of the employee.",
                             values=[f"{c[:-3]} one", f"{c[:-3]} two"], roles=["categorical_attribute"]) for c in CATS]
    rec["fields"] += [_field("Date_of_Joining__c", "Date of Joining", "date", "Day the employee joined.",
                             roles=["temporal_attribute"]),
                      _field("Last_Review_Date__c", "Last Review Date", "date", "Day of the last review.",
                             roles=["temporal_attribute"])]
    return k


@pytest.fixture(scope="module")
def xtools(tmp_path_factory):
    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    tmp = tmp_path_factory.mktemp("people_fixes")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        wh = F.make_warehouse(tmp, tables=_tables())
        kn = F.make_knowledge(tmp, knowledge=_knowledge())
        t = open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(kn))
    yield t
    t.close()


def ok(tools, names, question=""):
    tools.begin_question(question or "who is " + " and ".join(names))
    out = tools.call("about_people", {"names": names})
    assert out["ok"], out["error"]
    return out["result"]


def _rec(r, obj, label):
    """The matched record of this object and label."""
    got = [m for m in r["matched_records"] if m["object"] == obj and m["label"] == label]
    assert len(got) == 1, (obj, label, [(m["object"], m["label"]) for m in r["matched_records"]])
    return got[0]


def _holder(r, obj, label):
    """The people items whose records list the record of this object and label (one person per record)."""
    ref = _rec(r, obj, label)["ref"]
    return [p for p in r["people"] if ref in p["records"]]


def _profile_keys(tools, fields):
    spec = tools.catalog.objects["Recruiter__c"]
    return [tools._profile_key(spec, f) for f in fields]


# ------------------------------------------------------------------------------------- fix 1: the profile fill
def test_profile_fields_are_every_candidate_in_rank_order(xtools):
    cat = xtools.catalog
    apis = [f.api for f in cat.profile_fields(cat.objects["Recruiter__c"])]
    # status, the lookups (most filled first), the categorical fields (most filled first), the dates: no cut at 10
    assert apis == ["Employment_Status__c", "Department__c", "Assigned_Sr_Recruiter__c", "Manager__c",
                    "Band__c", "Desk__c", "Floor__c", "Grade__c", "Level__c", "Shift__c", "Track__c", "Wing__c",
                    "Zone__c", "Date_of_Joining__c", "Last_Review_Date__c"]
    for hidden in ("Date_of_Birth__c", "Email__c", "Old_Level__c", "Recruiter_User__c", "Recruiter_Contact__c", "Name"):
        assert hidden not in apis


def test_a_blank_high_ranked_field_does_not_block_a_date(xtools):
    r = ok(xtools, ["Ember Gap"])
    prof = r["people"][0]["profile"]
    # Manager__c (a lookup) and Track, Wing and Zone (categorical) rank above the dates and are blank: no place taken
    assert prof["Date_of_Joining__c (Date of Joining)"] == "2025-02-03"
    assert not any(k.startswith(("Manager__c", "Track__c", "Wing__c", "Zone__c", "Last_Review_Date__c")) for k in prof)
    assert list(prof) == ["Employment_Status__c (Employment Status)", "Department__c (Department)",
                          "Assigned_Sr_Recruiter__c (Reporting to)", "Band__c (Band)", "Desk__c (Desk)",
                          "Floor__c (Floor)", "Grade__c (Grade)", "Level__c (Level)", "Shift__c (Shift)",
                          "Date_of_Joining__c (Date of Joining)"]
    # the old rule (the first 10 fields of the object, blank or not) had no date for anyone
    old = _profile_keys(xtools, xtools.catalog.profile_fields(xtools.catalog.objects["Recruiter__c"])[:10])
    assert "Date_of_Joining__c (Date of Joining)" not in old


def test_a_full_profile_keeps_at_most_twelve_and_the_first_ten_are_the_old_ones(xtools):
    from sf_assistant.tools.people import PROFILE_MAX_FILLED

    assert PROFILE_MAX_FILLED == 12
    r = ok(xtools, ["Ember Full"])
    prof = r["people"][0]["profile"]
    fields = xtools.catalog.profile_fields(xtools.catalog.objects["Recruiter__c"])
    assert len(fields) == 15 and len(prof) == PROFILE_MAX_FILLED
    assert list(prof) == _profile_keys(xtools, fields[:PROFILE_MAX_FILLED])   # every field filled: the rank order
    assert list(prof)[:10] == _profile_keys(xtools, fields[:10])              # the old 10 fields, unchanged
    assert prof["Department__c (Department)"] == "Dept One" and prof["Manager__c (Manager)"] == "Oscar Prime"
    assert prof["Wing__c (Wing)"] == "Wing one" and "Zone__c (Zone)" not in prof


def test_profile_values_past_the_core_yield_first_to_the_result_size_budget(xtools):
    from sf_assistant.tools.people import PROFILE_CORE_FIELDS, PROFILE_MAX_FILLED
    from sf_assistant.tools.record_base import _Call

    call = _Call(xtools, "about_people")
    got = xtools._profiles(call, [("Recruiter__c", REC_EF, "Ember Full", None),
                                  ("Recruiter__c", REC_EG, "Ember Gap", None)])
    full, gap = got[REC_EF], got[REC_EG]
    fields = xtools.catalog.profile_fields(xtools.catalog.objects["Recruiter__c"])
    late = {("full" if d is full else "gap"): keys for d, keys in call.profile_details}
    assert late == {"full": _profile_keys(xtools, fields[PROFILE_CORE_FIELDS:PROFILE_MAX_FILLED]),
                    "gap": ["Date_of_Joining__c (Date of Joining)"]}   # the fields past the first 10 of the list
    refs = [{"person": "P1", "record": "R1", "object": "Interview__c", "field": "Recruiter__c", "role": "recruiter",
             "records": 3}]
    sysrefs = [{"person": "P1", "record": "R1", "object": "Lead", "field": "CreatedById", "role": "record creator",
                "records": 2}]
    out = {"people": [{"ref": "P1", "profile": full}, {"ref": "P2", "profile": gap}], "references": refs,
           "system_references": sysrefs}

    def fixed(size: int) -> int:   # what the budget adds to the result's size (notes, 60 per person; reserve 0)
        return size + len(json.dumps(call.notes)) + 60 * 2

    # room for all but one value: the longest profile gives its last value past the core, nothing else goes
    call.max_chars = fixed(len(json.dumps(out))) - 10
    xtools._people_budget(call, out, 2, reserve=0)
    assert list(full)[-1] == "Track__c (Track)" and "Wing__c (Wing)" not in full
    assert len(full) == PROFILE_MAX_FILLED - 1 and gap["Date_of_Joining__c (Date of Joining)"] == "2025-02-03"
    assert out["references"] == refs and out["system_references"] == sysrefs and call.notes == []
    # room for the core values only: every value past the core goes, and no reference, system reference or note
    core = {"people": [{"ref": "P1", "profile": dict(list(full.items())[:PROFILE_CORE_FIELDS])},
                       {"ref": "P2", "profile": {k: v for k, v in gap.items() if not k.startswith("Date_of_Joining")}}],
            "references": refs, "system_references": sysrefs}
    call.max_chars = fixed(len(json.dumps(core)))
    xtools._people_budget(call, out, 2, reserve=0)
    assert out == core and call.notes == []


def _every_date(tables: list) -> list:
    """The tables with Date_of_Joining__c filled on every employee (as on most employees of the real data)."""
    out = []
    for name, cols, rows in tables:
        if name == "Recruiter__c":
            j = list(cols).index("Date_of_Joining__c")
            rows = [r[:j] + (r[j] or "2023-05-06",) + r[j + 1:] for r in rows]
        out.append((name, cols, rows))
    return out


@pytest.fixture(scope="module")
def dtools(tmp_path_factory):
    """The fixture of this module with the joining date filled on every employee."""
    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    tmp = tmp_path_factory.mktemp("people_fixes_dates")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        wh = F.make_warehouse(tmp, tables=_every_date(_tables()))
        kn = F.make_knowledge(tmp, knowledge=_knowledge())
        t = open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(kn))
    yield t
    t.close()


def test_a_later_field_filled_on_most_records_stays_in_the_core(dtools):
    """Date_of_Joining__c is the 14th field of the list, past the first 10, but filled on every employee: on a record
    with blank fields above it (Ember Gap: 10 filled values with the date) it is in the core, so the result-size
    budget never cuts it first; the later fields filled on few records (Track, Wing) are still the first cut."""
    from sf_assistant.tools.people import PROFILE_CORE_FIELDS, PROFILE_MAX_FILLED, _core_value
    from sf_assistant.tools.record_base import _Call

    spec = dtools.catalog.objects["Recruiter__c"]
    fields = dtools.catalog.profile_fields(spec)
    doj = next(f for f in fields if f.api == "Date_of_Joining__c")
    assert fields.index(doj) >= PROFILE_CORE_FIELDS and 2 * doj.filled >= spec.rows
    track = next(f for f in fields if f.api == "Track__c")
    assert fields.index(track) >= PROFILE_CORE_FIELDS and 2 * track.filled < spec.rows
    assert _core_value(fields.index(doj), PROFILE_CORE_FIELDS, doj, spec)
    assert not _core_value(fields.index(doj), PROFILE_CORE_FIELDS + 1, doj, spec)   # an 11th filled value: a detail
    assert not _core_value(fields.index(track), 5, track, spec)
    call = _Call(dtools, "about_people")
    got = dtools._profiles(call, [("Recruiter__c", REC_EF, "Ember Full", None),
                                  ("Recruiter__c", REC_EG, "Ember Gap", None)])
    full, gap = got[REC_EF], got[REC_EG]
    assert gap["Date_of_Joining__c (Date of Joining)"] == "2025-02-03" and len(gap) == PROFILE_CORE_FIELDS
    late = {("full" if d is full else "gap"): keys for d, keys in call.profile_details}
    assert late == {"full": _profile_keys(dtools, fields[PROFILE_CORE_FIELDS:PROFILE_MAX_FILLED])}   # gap: none
    out = {"people": [{"ref": "P1", "profile": full}, {"ref": "P2", "profile": gap}], "references": [],
           "system_references": []}
    core = {"people": [{"ref": "P1", "profile": dict(list(full.items())[:PROFILE_CORE_FIELDS])},
                       {"ref": "P2", "profile": dict(gap)}], "references": [], "system_references": []}
    call.max_chars = len(json.dumps(core)) + len(json.dumps(call.notes)) + 60 * 2
    dtools._people_budget(call, out, 2, reserve=0)
    assert out == core and call.notes == []   # Ember Full's two later values went; Ember Gap's date stayed


def test_profiles_of_the_people_fixture_are_unchanged(xtools):
    # nine or fewer filled values per record: the same profile as before (the Group B fixture's employees)
    r = ok(xtools, ["Romeo Stark"])
    assert r["people"][0]["profile"] == {"Employment_Status__c (Employment Status)": "Active Employee",
                                         "Department__c (Department)": "Dept One",
                                         "Assigned_Sr_Recruiter__c (Reporting to)": "Papa Quinn",
                                         "Manager__c (Manager)": "Oscar Prime"}


# ------------------------------------------------------------------------------------- fix 4: one person per record
def test_a_record_two_same_name_peoples_links_reach_is_nobodys_own(xtools):
    """Both employees' Recruiter_Contact__c point to one account (with its portal user): the data does not say whose
    they are (review of 2026-10-22), so neither employee holds them; they are listed once with shared_by, and the
    user's work counts once, as shared, never as #1's (the Id order) or #2's."""
    r = ok(xtools, ["Charlie Twin"])
    kinds = sorted((p["label"], p["kind"]) for p in r["people"])
    assert kinds == [("Charlie Twin (#1)", "employee"), ("Charlie Twin (#2)", "employee"),
                     ("Charlie Twin (#2)", "user")]
    acc, user = _rec(r, "Account", "Charlie Twin"), _rec(r, "User", "Charlie Twin (#1)")
    one = next(p for p in r["people"] if p["label"] == "Charlie Twin (#1)" and p["kind"] == "employee")
    two = next(p for p in r["people"] if p["label"] == "Charlie Twin (#2)" and p["kind"] == "employee")
    assert _holder(r, "Account", "Charlie Twin") == [] and _holder(r, "User", "Charlie Twin (#1)") == []
    for m in (acc, user):
        assert "person" not in m and m["shared_by"] == [one["ref"], two["ref"]]
        assert m["how"].count("Recruiter_Contact__c") == 2 or m["how"].count("portal user (") == 2   # both links
    assert one["records"] == [_rec(r, "Recruiter__c", "Charlie Twin (#1)")["ref"]]
    assert two["records"] == [_rec(r, "Recruiter__c", "Charlie Twin (#2)")["ref"]]
    for p in (one, two):
        assert "linked" not in p and "system_work" not in p
    assert user["system_work"] == ["Interview__c.CreatedById 2"]                 # on the shared record, once
    assert [(e.get("person"), e["shared_by"], e["record"]) for e in r["system_references"]] == [
        (None, [one["ref"], two["ref"]], user["ref"])]
    assert r["shared_records"] == {acc["ref"]: [one["ref"], two["ref"]], user["ref"]: [one["ref"], two["ref"]]}
    text = json.dumps(r["notes"])
    assert (f"{acc['ref']} (Account) and {user['ref']} (User) are linked to the records of both {one['ref']} and "
            f"{two['ref']}") in text
    assert ("the data does not say which of them uses them, so their references, system references and system work "
            f"(shared_by) count once, as shared, never as {one['ref']}'s or {two['ref']}'s own") in text
    assert "listed once, under" not in text and "not linked except through the records they share" in text
    # the second employee's portal-user link does reach a user (shared): no "has no linked user" note for it
    two_rec = _rec(r, "Recruiter__c", "Charlie Twin (#2)")["ref"]
    assert f"{two_rec} (Employee) has no linked user" not in text


@pytest.mark.parametrize("names", [["Yankee Brook", "Zulu Arrow"], ["Zulu Arrow", "Yankee Brook"]])
def test_a_record_two_names_links_reach_is_shared_in_either_order(xtools, names):
    """A portal user that one employee's person account and another employee's Recruiter_User__c reach: in either
    order of the names nobody holds it (the lane's rule gave it to the first name), its work counts once, as shared,
    and a "shared record" link joins the two people (no pair without links) and is their only link (review of
    2026-10-23: the user created an interview of each of them, and one order made it a creator link)."""
    r = ok(xtools, names, question=f"what is the relation between {names[0]} and {names[1]}")
    by_label = {p["label"]: p for p in r["people"]}
    yb, za = by_label["Yankee Brook"], by_label["Zulu Arrow"]
    user = _rec(r, "User", "Yankee Brook")
    first, second = sorted([yb["ref"], za["ref"]], key=lambda x: int(x[1:]))
    assert user["shared_by"] == [first, second] and "person" not in user
    assert user["ref"] not in yb["records"] + za["records"] and "system_work" not in yb and "system_work" not in za
    assert user["system_work"] == ["Interview__c.CreatedById 3"]
    assert [e["shared_by"] for e in r["system_references"]] == [[first, second]]
    assert r["shared_records"] == {user["ref"]: [first, second]}
    lk = [x for x in r["links"] if x["how"] == "shared record"]
    assert len(lk) == 1 and lk[0]["between"] == [first, second] and lk[0]["records"] == 1
    assert f"both link to User 'Yankee Brook' ({user['ref']})" in lk[0]["text"]
    assert r["links"] == lk and r["pairs_without_links"] == []


def _order_free(r):
    """An about_people result of two names without its P and R codes (they follow the order of the names): the links,
    each person's records, system work and reference totals, the references, the system references and the work and
    sharers of each shared record, by label."""
    lab = {p["ref"]: p["label"] for p in r["people"]}
    rec = {m["ref"]: (m["object"], m["label"]) for m in r["matched_records"]}

    def refs(key):
        return sorted((lab.get(e.get("person"), ""), tuple(sorted(lab[x] for x in e.get("shared_by") or [])),
                       e["object"], e["field"], e["records"]) for e in r[key])
    return {"links": sorted((x["how"], tuple(sorted(lab[b] for b in x["between"])), x["records"]) for x in r["links"]),
            "people": {p["label"]: (sorted(rec[x] for x in p["records"]), p.get("system_work"),
                                    p.get("no_business_references"), p.get("business_reference_total"))
                       for p in r["people"]},
            "references": refs("references"), "system_references": refs("system_references"),
            "shared": {rec[k]: (sorted(lab[x] for x in v),
                                next(m.get("system_work") for m in r["matched_records"] if m["ref"] == k))
                       for k, v in r["shared_records"].items()}}


def test_a_shared_users_rows_link_nobody_and_both_orders_agree(xtools):
    """Review of 2026-10-23: the portal user that both employees' identity links reach created a client interview of
    each of them. The link finder gave the user to the person the order of the names made its holder, so one order
    added "<the other> is `Recruiter__c` and <the holder> is `CreatedById` on 1 Interview records". The user is
    nobody's there too: in either order the shared-record link is the only link and the result is the same."""
    ab = ok(xtools, ["Yankee Brook", "Zulu Arrow"], question="what is the relation between Yankee Brook and Zulu Arrow")
    ba = ok(xtools, ["Zulu Arrow", "Yankee Brook"], question="what is the relation between Zulu Arrow and Yankee Brook")
    for r in (ab, ba):
        assert not any("CreatedById" in x["path"] or "CreatedById" in x["text"] for x in r["links"])
    assert _order_free(ab) == _order_free(ba)
    view = _order_free(ab)
    assert view["links"] == [("shared record", ("Yankee Brook", "Zulu Arrow"), 1)]
    # each employee's own client interview counts for that employee; the user's three creations for nobody
    assert view["references"] == [("Yankee Brook", (), "Interview__c", "Recruiter__c", 1),
                                  ("Zulu Arrow", (), "Interview__c", "Recruiter__c", 1)]
    assert view["system_references"] == [("", ("Yankee Brook", "Zulu Arrow"), "Interview__c", "CreatedById", 3)]
    assert view["shared"] == {("User", "Yankee Brook"): (["Yankee Brook", "Zulu Arrow"],
                                                         ["Interview__c.CreatedById 3"])}


def test_a_shared_records_references_count_for_nobodys_ranking(xtools):
    """Review of 2026-10-23: the shared portal user of the two "Quebec Twin" employees is the account manager of one
    interview. That reference is nobody's, so it no longer ranks #1 (the Id order made #1 its holder) with #2, who
    owns an interview: #2 is listed first, #1 has no business reference, and the reference is listed once as shared."""
    r = ok(xtools, ["Quebec Twin"])
    emp = [p for p in r["people"] if p["kind"] == "employee"]
    assert [p["label"] for p in emp] == ["Quebec Twin (#2)", "Quebec Twin (#1)"]
    two, one = emp
    assert one.get("no_business_references") is True and "no_business_references" not in two
    user = _rec(r, "User", "Quebec Twin")
    assert user["shared_by"] == [two["ref"], one["ref"]] and user["ref"] not in one["records"] + two["records"]
    assert [(e.get("person"), e.get("shared_by"), e["field"], e["records"]) for e in r["references"]] == [
        (two["ref"], None, "Recruiter__c", 1), (None, [two["ref"], one["ref"]], "Account_Manager__c", 1)]


def test_a_shared_records_profile_is_shown_when_its_holder_is_not_detailed(xtools, monkeypatch):
    """Review of 2026-10-23: with one person detailed, only "Quebec Twin (#2)" (listed first) is; #1, which holds the
    shared account and user, is not. The shared user's profile is still read (shared_extra) and shown (a sharer is
    detailed), and no blocked or contact field of the account or the user (e-mail, phone, passport number, password
    change date) is in the result."""
    from sf_assistant.tools import people as PE

    monkeypatch.setattr(PE, "PEOPLE_DETAILED", 1)
    r = ok(xtools, ["Quebec Twin"])
    two, one = [p for p in r["people"] if p["kind"] == "employee"]
    assert two["label"] == "Quebec Twin (#2)" and "profile" in two
    assert "profile" not in one and one["business_reference_total"] == 0     # listed, not detailed
    user, acc = _rec(r, "User", "Quebec Twin"), _rec(r, "Account", "Quebec Twin")
    assert user["shared_by"] == acc["shared_by"] == [two["ref"], one["ref"]]
    assert user["profile"] == {"IsActive": True, "UserType": "PowerCustomerSuccess"}
    assert "profile" not in acc                                                 # only blocked or contact fields filled
    text = json.dumps(r)
    for value in ("qt@example.invalid", "000-000-0063", "X0000063", "2026-01-02", "LastPasswordChangeDate",
                  "PersonEmail", "Passport_Number__c"):
        assert value not in text, value


def test_a_moved_shared_records_references_count_for_its_new_holder(xtools, monkeypatch):
    """Review of 2026-10-23: a listed person's totals are those of its own records in the listing. With one person
    listed per name, "Quebec Twin (#1)", which holds the shared account and user, is not listed, so they are #2's
    own (one person per record): #2's business_reference_total counts its interview and the user's account-manager
    interview (2; its ranking total, without the shared user's reference, was 1)."""
    from sf_assistant.tools import people as PE

    monkeypatch.setattr(PE, "PEOPLE_LISTED", 1)
    monkeypatch.setattr(PE, "PEOPLE_DETAILED", 1)
    r = ok(xtools, ["Lima Nobel", "Quebec Twin"])
    two = next(p for p in r["people"] if p["label"] == "Quebec Twin (#2)")
    assert "Quebec Twin (#1)" not in [p["label"] for p in r["people"]]
    user = _rec(r, "User", "Quebec Twin")
    assert user.get("person") == two["ref"] and "shared_by" not in user and user["ref"] in two["records"]
    assert "profile" not in two and two["business_reference_total"] == 2


def test_a_shared_records_key_and_work_are_explained_and_are_details(xtools):
    """The keys note says what shared_by and a shared record's system work are, and facts_json over its size drops
    that work first, like a person's system_work, while the shared record and its system references stay."""
    from sf_assistant.agent.checks import facts_json

    r = ok(xtools, ["Charlie Twin"])
    note = next(n for n in r["notes"] if n.endswith("(never add these counts to each other or to references)"))
    assert "shared_by: the listed people whose records link to a shared record, which is nobody's own" in note
    assert "matched_records[].system_work: the records a shared record owns, created or last changed" in note
    full = facts_json(xtools.evidence, 10**9)
    assert "Interview__c.CreatedById 2" in full
    cut = json.loads(facts_json(xtools.evidence, len(full) - 1))[0]["result"]
    user = _rec(cut, "User", "Charlie Twin (#1)")
    assert "system_work" not in user and user["shared_by"] == _rec(r, "User", "Charlie Twin (#1)")["shared_by"]
    assert cut["system_references"] == [{k: v for k, v in e.items() if k != "who"} for e in r["system_references"]]
    assert "(never add these counts to each other or to references)" not in json.dumps(cut)   # the keys note: a detail


def test_a_shared_groups_references_are_cut_like_another_persons(xtools):
    """The result-size budget counts the references of a shared record (shared_by) as one group of their own
    (_ref_owner): without keep, the group with the most references gives its last that is not a record's top lookup
    first; with keep (the asked person), the group's last reference goes before any of the kept person's."""
    from sf_assistant.tools.people import _ref_owner, _ref_to_drop
    from sf_assistant.tools.record_base import _Call

    def ref(owner, rec, field, n):
        own = {"person": owner} if isinstance(owner, str) else {"shared_by": list(owner)}
        return {**own, "record": rec, "object": "Interview__c", "field": field, "role": "recruiter", "records": n,
                "meaning": "words of the lookup " * 12}

    def refs():
        return [ref("P1", "R1", "Alpha__c", 9), ref("P1", "R1", "Bravo__c", 7),
                ref(("P1", "P2"), "R3", "Charlie__c", 8), ref(("P1", "P2"), "R3", "Delta__c", 6),
                ref(("P1", "P2"), "R4", "Echo__c", 5)]

    assert [_ref_owner(e) for e in refs()][1:3] == ["P1", "shared P1+P2"]
    assert _ref_to_drop(refs(), {_ref_owner(e) for e in refs()}) == 3   # the group's Delta__c (R3's second lookup)
    for keep, gone in ((set(), "Delta__c"), ({"P1"}, "Echo__c")):
        call = _Call(xtools, "about_people")
        out = {"people": [{"ref": "P1"}, {"ref": "P2"}], "references": refs(), "system_references": []}
        after = {**out, "references": [e for e in refs() if e["field"] != gone]}
        call.max_chars = len(json.dumps(after)) + len(json.dumps(call.notes)) + 160 + 60 * 2   # room for one cut
        xtools._people_budget(call, out, 2, reserve=0, keep=keep)
        assert out == after, (keep, [e["field"] for e in out["references"]])
        assert call.notes == ["1 more references not shown (result size); call about_people with fewer names or "
                              "in_objects for them"]


def test_the_code_people_answer_names_the_records_same_name_people_share(xtools):
    from sf_assistant.agent.people_answer import people_answer

    r = ok(xtools, ["Charlie Twin"])
    one = next(p for p in r["people"] if p["label"] == "Charlie Twin (#1)" and p["kind"] == "employee")
    two = next(p for p in r["people"] if p["label"] == "Charlie Twin (#2)" and p["kind"] == "employee")
    text = people_answer(r)
    assert ("People listed with the same name are not linked by their records, except through the records they "
            f"share: Charlie Twin (#1) ({one['ref']}) and Charlie Twin (#2) ({two['ref']}) link to Account 'Charlie "
            "Twin' and User 'Charlie Twin (#1)' (the data does not say which of them uses them); the data does not "
            "record whether they are one human.") in text
    assert "are not linked by their records: the data" not in text and "listed under" not in text
    # same-name people that share nothing: the line as before
    r = ok(xtools, ["Golf Septet"])
    assert "shared_records" not in r and r.get("same_name")
    assert ("People listed with the same name are not linked by their records: the data does not record whether "
            "they are one human.") in people_answer(r)
    # people of two names: the shared-record link, no "no stored record links" line
    r = ok(xtools, ["Yankee Brook", "Zulu Arrow"])
    text = people_answer(r)
    assert "both link to User 'Yankee Brook'" in text and "No stored record links" not in text


def test_the_code_people_answer_names_a_record_people_of_one_name_with_different_names_share():
    """Review of 2026-10-23: two people of one asked name ("kilo") with different names whose records link to one user:
    the line says the data does not tell which of them uses it (_shared_lines; the same-name line does not cover
    people of different names), and neither person's sentence lists the user."""
    from sf_assistant.agent.people_answer import people_answer

    def person(ref, label, rec):
        return {"ref": ref, "asked": "kilo", "label": label, "kind": "employee", "match": "first name",
                "records": [rec], "no_business_references": True}

    def record(ref, label, obj, label_of, **who):
        return {"ref": ref, **who, "label": label, "object": obj, "object_label": label_of, "how": "name"}
    res = {"asked": ["kilo"], "people": [person("P1", "Kilo Alder", "R1"), person("P2", "Kilo Birch", "R3")],
           "matched_records": [record("R1", "Kilo Alder", "Recruiter__c", "Employee", person="P1"),
                               record("R2", "Kilo Alder", "User", "User", shared_by=["P1", "P2"]),
                               record("R3", "Kilo Birch", "Recruiter__c", "Employee", person="P2")],
           "references": [], "system_references": [], "shared_records": {"R2": ["P1", "P2"]}}
    text = people_answer(res)
    assert ("Kilo Alder (P1) and Kilo Birch (P2) link to User 'Kilo Alder' (the data does not say which of them uses "
            "it).") in text
    sentences = [ln for ln in text.split("\n") if ln.startswith(("Kilo Alder (P1) is", "Kilo Birch (P2) is"))]
    assert len(sentences) == 2 and not any("Linked records" in ln or "User" in ln for ln in sentences)
    assert "People listed with the same name" not in text


def test_the_code_people_answer_gives_the_profile_core(xtools):
    """Ten profile items per person (review of 2026-10-22: five left the joining date out when five filled values came
    before it)."""
    from sf_assistant.agent.people_answer import PROFILE_ITEMS, people_answer
    from sf_assistant.tools.people import PROFILE_CORE_FIELDS

    assert PROFILE_ITEMS == PROFILE_CORE_FIELDS == 10
    r = ok(xtools, ["Ember Gap"])
    assert list(r["people"][0]["profile"])[-1] == "Date_of_Joining__c (Date of Joining)"   # the 10th value
    assert "Date of Joining: 2025-02-03" in people_answer(r)


def test_a_same_name_user_no_identity_link_reaches_is_its_own_person(xtools):
    r = ok(xtools, ["Charlie Twin"])
    other = _rec(r, "User", "Charlie Twin (#2)")
    owners = _holder(r, "User", "Charlie Twin (#2)")
    assert [(p["kind"], p["records"]) for p in owners] == [("user", [other["ref"]])]   # never attached by its name
    assert other["how"] == "name"
    for p in r["people"]:
        if p["kind"] == "employee":
            assert other["ref"] not in p["records"]


def test_a_shared_record_goes_to_the_first_listed_person_when_its_holder_is_not_listed(xtools):
    r = ok(xtools, ["Golf Septet"])
    assert len(r["people"]) == 6 and r["more_people"]["Golf Septet"]["count"] == 1
    assert "Golf Septet (#1)" not in [p["label"] for p in r["people"]]        # no business reference: not listed
    acc = _rec(r, "Account", "Golf Septet")
    assert [p["label"] for p in _holder(r, "Account", "Golf Septet")] == ["Golf Septet (#2)"]
    assert "Recruiter_Contact__c" in acc["how"] and "shared_records" not in r


def test_people_with_records_of_their_own_are_unchanged(xtools):
    # one employee, its login user: the records, links and notes as before
    r = ok(xtools, ["Lima Nobel"])
    assert [(p["kind"], len(p["records"])) for p in r["people"]] == [("employee", 2)]
    assert "shared_records" not in r and not any("listed once" in n for n in r["notes"])
