"""Org Knowledge Base build (2026-10-27; integration of 2026-10-28): the people tools never count records of the
person-account mirror (Contact) and of the lead-conversion by-product (Opportunity) as a person's references, work or
links (``engine.catalog.PEOPLE_SCAN_SKIP``), a name search reads them only when the call names them, both objects
stay readable by the other record tools, a B2B field never takes a place in a person's profile
(``engine.catalog.PROFILE_DROP_ROLES``), and neither does a field of ``engine.catalog.PROFILE_SKIP_FIELDS`` (fixer step
of 2026-10-28).

Model-free, on a copy of the people fixture (``warehouse_fixture.PEOPLE_TABLES`` / ``PEOPLE_KNOWLEDGE``; fake names
only) with the person contact and the conversion opportunity of the converted candidate "Juliet Converse", both owned
and created by the user "Lima Nobel" (who no business record references), and two filled B2B fields on the candidate
account "Kilo Quill" next to a filled categorical field and a filled standard ``Rating`` (categorical, but in
``PROFILE_SKIP_FIELDS``).
"""

from __future__ import annotations

import copy
import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import warehouse_fixture as F  # noqa: E402
from warehouse_fixture import _field, _obj, _with, sid  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")
DAY = "2025-01-01T00:00:00.000Z"
CON_JC, OPP_JC = sid("003", 1), sid("006", 1)


def _tables() -> list:
    t = copy.deepcopy(F.PEOPLE_TABLES)
    _with(t, "Contact", {"Id": None, "Name": None, "CreatedDate": F.TS, "AccountId": None, "OwnerId": None,
                         "CreatedById": None}, [
        {"Id": CON_JC, "Name": "Juliet Converse", "CreatedDate": DAY, "AccountId": F.CAND_CONV, "OwnerId": F.USER_LN,
         "CreatedById": F.USER_LN}])
    _with(t, "Opportunity", {"Id": None, "Name": None, "CreatedDate": F.TS, "AccountId": None, "OwnerId": None,
                             "CreatedById": None, "StageName": None}, [
        {"Id": OPP_JC, "Name": "Juliet Converse", "CreatedDate": DAY, "AccountId": F.CAND_CONV, "OwnerId": F.USER_LN,
         "CreatedById": F.USER_LN, "StageName": "Prospecting"}])
    _with(t, "Account", {"Plan__c": None, "Client_Type__c": None, "Account_Manager__c": None, "Rating": None}, [])
    for i, (name, cols, rows) in enumerate(t):
        if name == "Account":
            keys = list(cols)
            fill = {"Plan__c": "Plan One", "Client_Type__c": "Direct Client", "Account_Manager__c": F.USER_1,
                    "Rating": "Warm"}
            t[i] = (name, cols, [tuple(fill[k] if k in fill and r[0] == F.CAND_KQ else v for k, v in zip(keys, r))
                                 for r in rows])
    return t


def _knowledge() -> list:
    k = copy.deepcopy(F.PEOPLE_KNOWLEDGE)
    acc = next(x for x in k if x["object"]["api_name"] == "Account")
    acc["fields"] += [
        _field("Plan__c", "Plan", "picklist", "Plan the candidate bought.", values=["Plan One", "Plan Two"],
               roles=["categorical_attribute"]),
        _field("Client_Type__c", "Client Type", "picklist", "B2B: kind of client company.",
               values=["Direct Client", "Vendor"], roles=["categorical_attribute", "b2b_attribute"]),
        _field("Account_Manager__c", "Account Manager", "reference", "B2B: account manager.",
               targets=["User"], role="b2b_account_manager", roles=["relationship", "b2b_attribute"]),
        _field("Rating", "Account Rating", "picklist", "Unused standard rating copied from the converted lead.",
               values=["Hot", "Warm", "Cold"], roles=["categorical_attribute", "score"])]
    k.append(_obj("Contact", "Contact", "Contacts", "One record is one contact (the person contact of a person account).", [
        _field("AccountId", "Account", "reference", "Account of the contact.", targets=["Account"], role="parent_account"),
        _field("OwnerId", "Owner", "reference", "Owner of the contact.", targets=["User"], role="record_owner")]))
    k.append(_obj("Opportunity", "Opportunity", "Opportunities", "One record is one opportunity (unused).", [
        _field("AccountId", "Account", "reference", "Account of the opportunity.", targets=["Account"],
               role="account"),
        _field("OwnerId", "Owner", "reference", "Owner of the opportunity.", targets=["User"], role="record_owner"),
        _field("StageName", "Stage", "picklist", "Stage of the opportunity.", values=["Prospecting"],
               roles=["business_status"])]))
    return k


@pytest.fixture(scope="module")
def stools(tmp_path_factory):
    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    tmp = tmp_path_factory.mktemp("people_scan_skip")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        wh = F.make_warehouse(tmp, tables=_tables())
        kn = F.make_knowledge(tmp, knowledge=_knowledge())
        t = open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(kn))
    yield t
    t.close()


def ok(tools, name, args, question=""):
    tools.begin_question(question)
    out = tools.call(name, args)
    assert out["ok"], out["error"]
    return out["result"]


def _objects_named(r) -> set[str]:
    """Every object that a reference, a system reference, a person's system work or more_references names."""
    out = {e.get("object") for key in ("references", "system_references") for e in r.get(key) or []}
    for p in r.get("people") or []:
        for line in (p.get("system_work") or []) + (p.get("more_references") or []):
            out.add(str(line).split(".", 1)[0])
    return out


def test_the_two_objects_are_skipped_by_the_people_tools_only(stools):
    from sf_assistant.engine.catalog import PEOPLE_SCAN_SKIP

    assert PEOPLE_SCAN_SKIP == {"Contact", "Opportunity"}
    for obj in ("Contact", "Opportunity"):   # every other record tool still reads them
        assert stools.catalog.objects[obj].queryable
        assert ok(stools, "count_records", {"object": obj})["count"] == 1
    r = ok(stools, "count_records", {"object": "Contact", "filters": [{"field": "AccountId", "op": "eq",
                                                                         "value": "Juliet Converse"}]})
    assert r["count"] == 1


def test_the_person_contact_and_the_conversion_opportunity_are_no_references(stools):
    r = ok(stools, "about_people", {"names": ["Juliet Converse"]})
    assert [(p["label"], p["kind"]) for p in r["people"]] == [("Juliet Converse", "candidate")]
    assert not {"Contact", "Opportunity"} & _objects_named(r)
    assert r["people"][0].get("no_business_references") is True


def test_the_owner_of_both_rows_keeps_no_business_references_and_no_work_on_them(stools):
    r = ok(stools, "about_people", {"names": ["Lima Nobel"]})
    p = r["people"][0]
    assert p["kind"] == "employee" and p["no_business_references"] is True
    assert not {"Contact", "Opportunity"} & _objects_named(r)


def test_the_link_finder_reads_neither_object(stools):
    r = ok(stools, "about_people", {"names": ["Juliet Converse", "Lima Nobel"]})
    paths = [x.get("path", "") for x in r.get("links") or []]
    assert not [x for x in paths if x.startswith(("Contact.", "Opportunity."))]
    assert "Contact" not in r.get("links_checked", "") and "Opportunity" not in r.get("links_checked", "")


def test_a_b2b_field_never_takes_a_profile_place(stools):
    cat = stools.catalog
    apis = [f.api for f in cat.profile_fields(cat.objects["Account"])]
    assert "Plan__c" in apis and "Client_Type__c" not in apis and "Account_Manager__c" not in apis
    r = ok(stools, "about_people", {"names": ["Kilo Quill"]})
    prof = r["people"][0]["profile"]
    assert any(k.startswith("Plan__c") for k in prof)
    assert not any(k.startswith(("Client_Type__c", "Account_Manager__c")) for k in prof)


def test_a_name_search_reads_both_objects_only_when_the_call_names_them(stools):
    r = ok(stools, "search_records", {"name": "Juliet Converse"})
    found = {m["object"] for m in r["matches"]}
    assert "Account" in found and not {"Contact", "Opportunity"} & found
    for obj in ("Contact", "Opportunity"):   # named in the call: searched
        r = ok(stools, "search_records", {"name": "Juliet Converse", "objects": [obj]})
        assert [m["object"] for m in r["matches"]] == [obj]


def test_a_profile_skip_field_never_takes_a_profile_place(stools):
    from sf_assistant.engine.catalog import PROFILE_SKIP_FIELDS

    assert PROFILE_SKIP_FIELDS == {"Account": {"AccountSource", "Industry", "PersonLeadSource", "Rating", "Type"}}
    cat = stools.catalog
    rating = cat.objects["Account"].fields["Rating"]
    assert rating.has_data and "categorical_attribute" in rating.roles   # admitted by its roles ...
    apis = [f.api for f in cat.profile_fields(cat.objects["Account"])]
    assert "Plan__c" in apis and "Rating" not in apis                    # ... but skipped by name
    prof = ok(stools, "about_people", {"names": ["Kilo Quill"]})["people"][0]["profile"]
    assert any(k.startswith("Plan__c") for k in prof) and not any(k.startswith("Rating") for k in prof)
    r = ok(stools, "list_records", {"object": "Account", "fields": ["Name", "Rating"],
                                    "filters": [{"field": "Rating", "op": "eq", "value": "Warm"}]})
    assert r["total_count"] == 1 and "Rating" in str(r["columns"])            # still read when asked
