"""sf_assistant.engine.catalog: the entity index of the value spots (Catalog._entity_index, 2026-10-28).

Every stored value a person could name in a question, in every queryable object and every field whose values a person
could name (owner 2026-10-28; rule R4), with the records that hold it: scope, keys, counts and stored spellings, cue
words, design and label phrases, compact keys, the person-derived fields the people path owns, the budgets and their
start-up line, and the catalog parts that must not change (the value index, the stat statements).

Model-free, on a small synthetic warehouse built here (fake names and values only; the person names are the synthetic
ones of the shared fixtures). The live twins (``OKEVAL_RECORDS_LIVE=1``) read their values from the snapshot at run
time and never write them into this file.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path

import pytest

from sf_assistant.config import DEFAULT_WAREHOUSE, KNOWLEDGE_DIR

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant.engine import catalog as C  # noqa: E402
from sf_assistant.engine import vocabulary as V  # noqa: E402
from sf_assistant.engine.catalog import (BLANK_VALUES, ENTITY_NAME_MAX_WORDS, Catalog, EntityEntry,  # noqa: E402
                                         entity_key, hide_embedded_secrets, normalize_value)
from sf_assistant.engine.warehouse import Warehouse  # noqa: E402
from warehouse_fixture import _field, _obj, make_knowledge, make_warehouse, sid  # noqa: E402

DAY = "2025-01-01T00:00:00.000Z"
TS = "TIMESTAMP"
RT_DEPT, RT_TEAM, RT_MASTER, RT_PERSON, RT_CLIENT = (sid("012", i) for i in range(1, 6))
#: A Name of 12 words and more than 60 characters: kept (Names keep ENTITY_NAME_MAX_CHARS / ENTITY_NAME_MAX_WORDS).
LONG_NAME = "Alpha Beta Gamma Delta Epsilon Zeta Eta Theta Iota Kappa Lambda Mu"
TOO_LONG_NAME = LONG_NAME + " Nu"                                     # 13 words: left out

TABLES = [
    ("Department__c", {"Id": None, "Name": None, "RecordTypeId": None, "Status__c": None, "CreatedDate": TS}, [
        (sid("a07", 1), "Zorvane", RT_DEPT, "Active", DAY),
        (sid("a07", 2), "Quillmore Unit", RT_TEAM, "Active", DAY),
        (sid("a07", 3), "Q&A Desk", RT_TEAM, "Inactive", DAY),
        (sid("a07", 4), "Vex Orbit", RT_DEPT, "Active", DAY),
        (sid("a07", 5), LONG_NAME, RT_DEPT, None, DAY),
        (sid("a07", 6), TOO_LONG_NAME, None, None, DAY),
        (sid("a07", 7), "Test", None, None, DAY),
        (sid("a07", 8), "Jan 2025", None, None, DAY),     # a cohort-like Name of a date shape: a record identity
        (sid("a07", 9), "2024", None, None, DAY),         # digits only, no code field: a number, never an entity
    ]),
    ("Company__c", {"Id": None, "Name": None}, [
        (sid("a0M", 1), "Brightloom"), (sid("a0M", 2), "BRIGHTLOOM"), (sid("a0M", 3), "Orbitek Labs"),
        (sid("a0M", 4), "Halo Grid"), (sid("a0M", 5), "Corvane Systems"), (sid("a0M", 6), "Quorvex"),
        (sid("a0M", 7), "Kilo Zephyr Partners")]),       # one of seven holds a person's name: still companies
    ("Meeting__c", {
        "Id": None, "Name": None, "CreatedDate": TS, "Company__c": None, "Company_Text__c": None, "Applicant__c": None,
        "Meeting_Type__c": None, "Skills__c": None, "Ticket_Number__c": None, "Batch_Number__c": None,
        "Slot_Text__c": None, "Misc_Text__c": None, "Ref_Code__c": None, "Description__c": None, "Venue_Url__c": None,
        "Join_Link__c": None, "External_Ref_Id__c": None, "Host_Email__c": None, "Zoom_Passcode__c": None,
        "Notes__c": None, "Meeting_Date__c": "DATE"}, [
        (sid("a0V", 1), "MT-0001", DAY, sid("a0M", 1), "Brightloom", "Kilo Zephyr", "Fishbowl Round", "Coding;Design",
         "000123", "7", "Jan 2020", "a0X000000000001AAA", "RC-1001", "Zorvane kickoff", "https://example.invalid/v",
         "https://example.invalid/j", "EXT-1", "host@example.invalid", "zp-1", "Quillmore notes", "2026-01-05"),
        (sid("a0V", 2), "MT-0002", DAY, sid("a0M", 1), "Brightloom", "Lima Nobel (ext)", "Fishbowl Round", "Coding",
         "000124", "7", "10 LPA", "someone@example.invalid", "free form ref", None, None, None, None, None, None, None,
         None),
        (sid("a0V", 3), "MT-0003", DAY, sid("a0M", 3), "Brightloom", "Romeo Stark", "Screening", None, "000125", "8",
         "Q1 2020", "https://example.invalid/x", None, None, None, None, None, None, None, None, None),
        (sid("a0V", 4), "MT-0004", DAY, None, "halogrid", "Panel Guest", "Screening", "Design", "000126", "9", "120k",
         "/srv/share/doc", None, None, None, None, None, None, None, None, None),
        (sid("a0V", 5), "MT-0005", DAY, None, None, None, None, None, None, None, "Batch 12", "12345", None, None, None,
         None, None, None, None, None, None),
        (sid("a0V", 6), "MT-0006", DAY, None, None, None, None, None, None, None, None, "2026-01-05", None, None,
         None, None, None, None, None, None, None),
        (sid("a0V", 7), "MT-0007", DAY, None, None, None, None, None, None, None, None, "x" * 61, None, None, None,
         None, None, None, None, None, None),
        (sid("a0V", 8), "MT-0008", DAY, None, None, None, None, None, None, None, None, None, None, None,
         None, None, None, None, None, None, None),
        (sid("a0V", 9), "MT-0009", DAY, None, None, None, None, None, None, None, None, None, None,
         None, None, None, None, None, None, None, None),
        (sid("a0V", 10), "MT-0010", DAY, None, None, None, None, None, None, None, None, "Orion Desk", None, None,
         None, None, None, None, None, None, None),
        (sid("a0V", 11), "MT-0011", DAY, None, None, None, None, None, None, None, None, "B.Tech/B.E", None, None,
         None, None, None, None, None, None, None),
    ]),
    # a text field one of whose values holds an embedded secret: left out whole, its plain values too (review PPI-2)
    ("Kiosk__c", {"Id": None, "Name": None, "Access_Text__c": None}, [
        (sid("a0K", 1), "Kiosk One", "join?pwd=abc123"), (sid("a0K", 2), "Kiosk Two", "Passcode: 998877"),
        (sid("a0K", 3), "Kiosk Three", "Zorquil Bay")]),
    ("Invoice__c", {"Id": None, "Name": None}, [(sid("a0W", 1), "00017"), (sid("a0W", 2), "00018"),
                                                (sid("a0W", 3), "INX-0003")]),
    ("Recruiter__c", {"Id": None, "Name": None, "Alias_Code__c": None}, [
        (sid("a06", 1), "Kilo Zephyr", "EMP-0001"), (sid("a06", 2), "Lima Nobel", "EMP-0002")]),
    ("User", {"Id": None, "Name": None, "IsActive": "BOOLEAN"}, [(sid("005", 1), "Romeo Stark", "true")]),
    ("Lead", {"Id": None, "Name": None, "Display__c": None, "Company": None}, [
        (sid("00Q", 1), "Sierra Tango", "Sierra Tango (Lead)", "Brightloom"),
        (sid("00Q", 2), "Amara Belle Cora Dane", "Amara Dane - new", "Orbitek")]),
    ("Contact", {"Id": None, "Name": None}, [(sid("003", 1), "Kilo Zephyr")]),
    ("Account", {"Id": None, "Name": None, "RecordTypeId": None, "IsPersonAccount": "BOOLEAN",
                 "Account_Code__c": None}, [
        (sid("001", 1), "Uniform Vale", RT_PERSON, "true", "ACC-0001"),
        (sid("001", 2), "Northwind Traders", RT_CLIENT, "false", "ACC-0002")]),
    ("Opportunity", {"Id": None, "Name": None}, [(sid("006", 1), "Uniform Vale Deal")]),
    ("Candidate_Portal_Credential__c", {"Id": None, "Name": None, "Status__c": None}, [
        (sid("a0H", 1), "CCN-0001", "Active")]),
    ("RecordType", {"Id": None, "Name": None, "DeveloperName": None, "SobjectType": None}, [
        (RT_DEPT, "Department", "Department", "Department__c"), (RT_TEAM, "Team", "Team", "Department__c"),
        (RT_MASTER, "Master", "Master", "Department__c"), (RT_PERSON, "Person Account", "PersonAccount", "Account"),
        (RT_CLIENT, "B2B Client", "B2B_Client", "Account")]),
]


def _knowledge() -> list[dict]:
    dept = _obj("Department__c", "Department", "Organisational Units", "One record is one department or team.", [
        _field("Status__c", "Status", "picklist", "Whether the unit is active.", values=["Active", "Inactive"]),
        # documented but not synced: its picklist values are still design vocabulary
        _field("Legacy_Tier__c", "Legacy Tier", "picklist", "Former tier.", values=["Platinum Tier"]),
    ], concepts=["department", "team", "organisational unit", "active", "the"])
    dept["record_types"] = [{"developer_name": "Team", "label": "Team"},
                            {"developer_name": "Master", "label": "Master"}]
    return [
        dept,
        _obj("Company__c", "Company", "Companies", "One record is one hiring company.", [],
             concepts=["company", "employer", "hiring company"]),
        _obj("Meeting__c", "Meeting", "Meetings", "One record is one meeting.", [
            _field("Name", "Meeting Number", "auto_number", "Meeting number."),
            _field("Company__c", "Company Record", "reference", "Company.", targets=["Company__c"]),
            _field("Company_Text__c", "Company", "string", "Company name as typed."),
            _field("Applicant__c", "Applicant", "string", "Who applied, as typed."),
            _field("Meeting_Type__c", "Meeting Type", "picklist", "Kind of meeting.",
                   values=["Fishbowl Round", "Screening"]),
            _field("Skills__c", "Skills", "multipicklist", "Skills covered.", values=["Coding", "Design"]),
            _field("Ticket_Number__c", "Ticket Number", "string", "Ticket of the meeting."),
            _field("Batch_Number__c", "Batch Number", "string", "Batch of the meeting."),
            _field("Slot_Text__c", "Slot", "string", "Slot as text."),
            _field("Misc_Text__c", "Misc", "string", "Anything."),
            _field("Ref_Code__c", "Reference Code", "string", "External reference.", roles=["identifier"]),
            _field("Description__c", "Description", "string", "Free text."),
            _field("Venue_Url__c", "Venue", "string", "Venue link."),
            _field("Join_Link__c", "Join Link", "url", "Join link."),
            _field("External_Ref_Id__c", "External Ref", "string", "External key."),
            _field("Host_Email__c", "Host Email", "email", "Host e-mail."),
            _field("Zoom_Passcode__c", "Zoom Passcode", "string", "Meeting passcode."),
            _field("Notes__c", "Notes", "long_text_area", "Notes."),
            _field("Meeting_Date__c", "Meeting Date", "date", "Day of the meeting."),
        ]),
        _obj("Kiosk__c", "Kiosk", "Kiosks", "One record is one kiosk.", [
            _field("Access_Text__c", "Access", "string", "Access note.")]),
        _obj("Invoice__c", "Invoice", "Invoices", "One record is one invoice.", [
            _field("Name", "Invoice Number", "auto_number", "Invoice number.")]),
        _obj("Recruiter__c", "Employee", "Employees", "One record is one employee.", [
            _field("Alias_Code__c", "Alias Code", "string", "Login alias.", roles=["identifier"])]),
        _obj("User", "User", "Users", "One record is one user.", []),
        _obj("Lead", "Lead", "Leads", "One record is one lead.", []),
        _obj("Contact", "Contact", "Contacts", "One record is one contact.", []),
        _obj("Account", "Account", "Accounts", "One record is one account.", [
            _field("Account_Code__c", "Account Code", "string", "Account code.", roles=["identifier"])]),
        _obj("Opportunity", "Opportunity", "Opportunities", "One record is one opportunity.", []),
        _obj("Candidate_Portal_Credential__c", "Candidate Portal Credential", "Candidate Portal Credentials",
             "One record is one portal login.", [
                 _field("Status__c", "Status", "picklist", "Login status.", values=["Active", "Disabled"])]),
    ]


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """(warehouse path, knowledge dir) of the synthetic warehouse."""
    d = tmp_path_factory.mktemp("entity")
    return str(make_warehouse(d, tables=TABLES)), str(make_knowledge(d, knowledge=_knowledge()))


def _build(built) -> Catalog:
    wh = Warehouse.open(built[0])
    try:
        return Catalog.build(wh, built[1])
    finally:
        wh.close()


@pytest.fixture(scope="module")
def cat(built) -> Catalog:
    return _build(built)


def _places(cat: Catalog, key: str) -> set[tuple[str, str, str]]:
    return {(e.obj, e.field, e.kind) for e in cat.entity_entries(key)}


# ----------------------------------------------------------------------------------------------- scope
def test_names_codes_text_picklists_and_record_types_are_indexed(cat):
    assert _places(cat, "zorvane") == {("Department__c", "Name", "name")}
    assert _places(cat, "quillmore unit") == {("Department__c", "Name", "name")}
    assert _places(cat, "northwind traders") == {("Account", "Name", "name")}          # a client company
    assert _places(cat, "brightloom") == {("Company__c", "Name", "name"), ("Meeting__c", "Company_Text__c", "text"),
                                          ("Lead", "Company", "text")}
    # codes: auto-number Names, digits-only Names of an auto-number field, identifier text of the code shape (a
    # unique ...Number text field: test_a_unique_number_field_gives_codes_only_on_enough_records)
    assert _places(cat, "mt 0001") == {("Meeting__c", "Name", "code")}
    assert _places(cat, "inx 0003") == {("Invoice__c", "Name", "code")}
    assert _places(cat, "17") == {("Invoice__c", "Name", "code")}
    assert [e.value for e in cat.entity_entries("17")] == ["00017"]
    assert _places(cat, "rc 1001") == {("Meeting__c", "Ref_Code__c", "code")}
    # picklists (multi-select values split) and record type names with records
    assert _places(cat, "fishbowl round") == {("Meeting__c", "Meeting_Type__c", "picklist")}
    assert _places(cat, "coding") == {("Meeting__c", "Skills__c", "picklist")}
    assert _places(cat, "team") == {("Department__c", "RecordTypeId", "record_type")}
    assert _places(cat, "person account") == {("Account", "RecordTypeId", "record_type")}
    assert not cat.entity_entries("master")                     # a record type without records


def test_people_sensitive_objects_and_fields_no_person_could_name_stay_out(cat):
    keys = set(cat.entities)
    # the Names of person objects, Contact and Opportunity belong to the people path; person accounts too
    for name in ("Kilo Zephyr", "Lima Nobel", "Romeo Stark", "Sierra Tango", "Amara Belle Cora Dane", "Uniform Vale",
                 "Uniform Vale Deal"):
        assert entity_key(name) not in keys, name
    objects = {o for o, _f in cat.entity_fields}
    assert not objects & {"Recruiter__c", "User", "Contact", "Opportunity", "Candidate_Portal_Credential__c",
                          "RecordType", "Group"}
    assert ("Account", "Name") in cat.entity_fields and ("Account", "Account_Code__c") not in cat.entity_fields
    assert "ccn 0001" not in keys and "emp 0001" not in keys and "acc 0001" not in keys
    meeting = {f for o, f in cat.entity_fields if o == "Meeting__c"}
    assert meeting == {"Name", "Company_Text__c", "Meeting_Type__c", "Skills__c", "Slot_Text__c", "Misc_Text__c",
                       "Ref_Code__c"}
    # free text, links, Ids, contact, blocked, long text, lookups and dates are never read
    for f in ("Description__c", "Venue_Url__c", "Join_Link__c", "External_Ref_Id__c", "Host_Email__c",
              "Zoom_Passcode__c", "Notes__c", "Company__c", "Meeting_Date__c", "Batch_Number__c", "Applicant__c"):
        assert f not in meeting, f
    for key in ("zorvane kickoff", "quillmore notes", "ext 1", "zp 1"):
        assert key not in keys, key
    # a ...Number field whose values repeat gives no codes
    assert not cat.entity_entries("7") and not cat.entity_entries("8")


def test_a_unique_number_field_gives_codes_only_on_enough_records(cat, built, monkeypatch):
    """A ...Number text field whose every record has its own value gives digits-only codes, keyed without leading
    zeros, once it is filled on ENTITY_NUMBER_MIN_FILLED records: on fewer, "every value is its own" proves nothing (a
    version number "1" on the one file of a small object; integration of the value spots, 2026-10-28)."""
    assert C.ENTITY_NUMBER_MIN_FILLED > 4 and not cat.entity_entries("123")   # Ticket_Number__c: 4 records
    assert ("Meeting__c", "Ticket_Number__c") not in cat.entity_fields
    monkeypatch.setattr(C, "ENTITY_NUMBER_MIN_FILLED", 4)
    four = _build(built)
    assert _places(four, "123") == {("Meeting__c", "Ticket_Number__c", "code")}
    assert [e.value for e in four.entity_entries("123")] == ["000123"]
    assert four.entity_fields[("Meeting__c", "Ticket_Number__c")]["kind"] == "code"


def test_value_rules(cat):
    misc = sorted(e.value for es in cat.entities.values() for e in es if e.field == "Misc_Text__c")
    assert misc == ["B.Tech/B.E", "Orion Desk"]                 # a slash name stays; everything else is no entity
    slots = [e.value for es in cat.entities.values() for e in es if e.field == "Slot_Text__c"]
    assert slots == ["Batch 12"]                                # dates and amounts written as text are filters
    names = {e.value for es in cat.entities.values() for e in es if (e.obj, e.field) == ("Department__c", "Name")}
    assert LONG_NAME in names and len(LONG_NAME) > V.MAX_VALUE_CHARS and len(LONG_NAME.split()) == 12
    assert TOO_LONG_NAME not in names and "2024" not in names
    assert "Jan 2025" in names and "Test" in names              # a date-shaped Name stays; blanks stay stored
    dropped = cat.entity_info["dropped"]
    for why in ("id_like", "mail_url_path", "number_date", "long", "secret", "date_amount", "not_code"):
        assert dropped.get(why), why
    for es in cat.entities.values():                           # never a secret, an e-mail address or a link
        for e in es:
            assert hide_embedded_secrets(e.value) == e.value and "@" not in e.value and "http" not in e.value
    assert cat.entity_max_words == ENTITY_NAME_MAX_WORDS == 12


def test_a_field_with_an_embedded_secret_is_left_out_whole(cat):
    """Review PPI-2 (phase 1, very high): one value with an embedded secret ("pwd=", a labelled passcode) leaves the
    whole field out of the index, as the value index does, so its unlabelled siblings never reach a spot or a hint;
    the field is listed by API name only."""
    assert ("Kiosk__c", "Access_Text__c") not in cat.entity_fields
    assert not cat.entity_entries("zorquil bay") and not cat.entity_entries("passcode 998877")
    assert cat.entity_info["secret_fields"] == ["Kiosk__c.Access_Text__c"]
    assert _places(cat, "kiosk one") == {("Kiosk__c", "Name", "name")}    # the object's other fields stay
    assert cat.entity_info["dropped"].get("secret")


def test_entity_key():
    assert entity_key("Q&A") == "q a"
    assert entity_key("Level 2-3 Drill") == "level 2 3 drill"
    assert entity_key("PILOT'S") == "pilot" and entity_key("the team’s lead") == "the team lead"
    assert entity_key("Zörvâne Désk") == entity_key("Zorvane Desk") == "zorvane desk"
    assert entity_key("Ｆｕｌｌ－Ｔｉｍｅ") == "full time" and entity_key("U.S. Office") == "us office"
    assert entity_key(None) == "" and entity_key(" ") == "" and entity_key(12) == "12"
    for text in ("Q&A Desk", "Vex-Orbit/Labs (East)", "it's 9:30 AM", "Full_Time — Contract", "A.B.C. 1-2"):
        ref = " ".join(re.findall(r"[a-z0-9]+", normalize_value(re.sub(r"['’]s\b", "", text, flags=re.I))))
        assert entity_key(text) == ref, text


def test_counts_and_stored_spellings(cat):
    es = cat.entity_entries("brightloom")
    company = sorted((e.value, e.records) for e in es if e.obj == "Company__c")
    assert company == [("BRIGHTLOOM", 1), ("Brightloom", 1)]   # every stored spelling of one field is kept
    assert [(e.value, e.records) for e in es if e.field == "Company_Text__c"] == [("Brightloom", 3)]
    assert [e.records for e in cat.entity_entries("coding")] == [2]          # records whose selection holds it
    assert [e.records for e in cat.entity_entries("team")] == [2]
    assert all(isinstance(e, EntityEntry) and not e.truncated for es in cat.entities.values() for e in es)
    assert cat.entity_fields[("Company__c", "Name")] == {"kind": "name", "distinct": 7, "truncated": False}
    with pytest.raises(AttributeError):        # frozen, slotted entries: shared read-only by every thread
        es[0].records = 9
    assert cat.entity_entries("no such value") == ()


def test_cue_words(cat):
    # label head, plural label head, one-word record type names (not Master), one-word concepts; no function word,
    # no blank value, no stored picklist value of the object ("active")
    assert cat.entity_cues("Department__c") == {"department", "units", "team"}
    assert cat.entity_cues("Company__c") == {"company", "companies", "employer"}
    assert cat.entity_cues("Meeting__c") == {"meeting", "meetings"}
    assert cat.entity_cues("No_Such__c") == frozenset()
    assert cat.field_cues("Meeting__c", "Company_Text__c") == {"company"}
    assert cat.field_cues("Meeting__c", "Slot_Text__c") == {"slot"}
    assert cat.field_cues("Meeting__c", "No_Such__c") == frozenset()


def test_design_and_label_phrases(cat):
    d = cat.design_phrases
    for phrase in ("department", "organisational units", "organisational unit", "team", "fishbowl round", "coding",
                   "platinum tier", "legacy tier", "company", "meeting type", "b2b client", "active"):
        assert phrase in d, phrase
    assert "zorvane" not in d and "brightloom" not in d and "" not in d
    assert {"meeting", "meetings", "company", "meeting type", "slot"} <= cat.label_phrases
    assert "fishbowl round" not in cat.label_phrases


def test_compact_keys(cat):
    assert cat.entity_compact["vexorbit"] == ("vex orbit",)
    assert cat.entity_compact["mt0001"] == ("mt 0001",)
    assert cat.entity_compact["qadesk"] == ("q a desk",)
    assert "halogrid" not in cat.entity_compact                # the compact form is a stored text value
    assert "zorvane" not in cat.entity_compact                 # one-word keys need no compact form
    assert "batch12" not in cat.entity_compact                 # text keys get none
    for compact, keys in cat.entity_compact.items():
        assert len(compact) >= 4 and all(" " in k and k.replace(" ", "") == compact for k in keys)


def test_person_derived_fields_are_left_to_the_people_path(cat, built, monkeypatch):
    assert set(cat.entity_info["person_fields"]) == {"Meeting__c.Applicant__c", "Lead.Display__c"}
    assert ("Lead", "Display__c") not in cat.entity_fields and ("Meeting__c", "Applicant__c") not in cat.entity_fields
    assert ("Company__c", "Name") in cat.entity_fields and ("Lead", "Company") in cat.entity_fields
    assert "panel guest" not in cat.entities
    # without person rows no field is left out
    monkeypatch.setattr(Catalog, "_entity_person_names", lambda self, deadline, info: frozenset())
    other = _build(built)
    assert other.entity_info["person_fields"] == [] and ("Meeting__c", "Applicant__c") in other.entity_fields


# ----------------------------------------------------------------------------------------------- budgets and cuts
def test_uncut_index_adds_no_start_up_line(cat):
    info = cat.entity_info
    assert cat.entity_lines() == [] and not info["cut_fields"] and not info["skipped_objects"] and not info["errors"]
    assert info["seconds"] <= C.ENTITY_BUILD_BUDGET_S and info["keys"] == len(cat.entities)
    assert info["entries"] == sum(len(v) for v in cat.entities.values())
    assert set(info["entries_by_kind"]) == {"name", "code", "text", "picklist", "record_type"}


def test_a_field_over_the_field_limit_keeps_its_most_frequent_values(built, monkeypatch):
    monkeypatch.setattr(C, "ENTITY_FIELD_MAX", 1)
    cat = _build(built)
    kept = [e for es in cat.entities.values() for e in es if (e.obj, e.field) == ("Meeting__c", "Company_Text__c")]
    assert [(e.value, e.records, e.truncated) for e in kept] == [("Brightloom", 3, True)]   # "halogrid" was cut
    assert cat.entity_fields[("Meeting__c", "Company_Text__c")]["truncated"] is True
    assert "Meeting__c.Company_Text__c" in cat.entity_info["cut_fields"]
    line = cat.entity_lines()
    assert len(line) == 1 and line[0].startswith("value index cut: ") and line[0].endswith(
        " fields, 0 objects skipped (field limit)")


def test_the_time_budget_skips_objects_and_says_so(built, monkeypatch):
    monkeypatch.setattr(C, "ENTITY_BUILD_BUDGET_S", 0.0)
    cat = _build(built)
    assert "Department__c" in cat.entity_info["skipped_objects"]
    assert not [e for es in cat.entities.values() for e in es if e.kind != "record_type"]
    assert cat.entity_lines() == [f"value index cut: 0 fields, {len(cat.entity_info['skipped_objects'])} objects "
                                  "skipped (time budget)"]


def test_the_record_session_lines_gain_the_cut_line_only_when_the_index_was_cut(built, monkeypatch):
    """The start-up lines of a record session (pipeline.open_record_session, the page's and the cli's): an uncut index
    adds nothing; a cut one adds its one line after the other lines (design amendment 22)."""
    import sf_assistant.pipeline as pipeline
    from sf_assistant import config as SC

    monkeypatch.setenv("SFA_RESOLVER", "off")
    cfg = SC.load_config(base_url="http://localhost:8000/v1")
    rcfg = SC.load_records_config(warehouse=built[0], today=dt.date(2026, 10, 13))

    def lines() -> list[str]:   # one session at a time; the snapshot's age changes from one open to the next
        session, out = pipeline.open_record_session(cfg, rcfg, built[1])
        assert session is not None, out
        session.close()
        return [re.sub(r"\d+\.\d h\b", "<age> h", x) for x in out if not x.startswith("note: made ")]

    uncut = lines()
    assert uncut and not any(x.startswith("value index cut: ") for x in uncut)
    monkeypatch.setattr(C, "ENTITY_BUILD_BUDGET_S", 0.0)
    cut = lines()
    assert cut[:-1] == uncut and cut[-1].startswith("value index cut: 0 fields, ") \
        and cut[-1].endswith(" objects skipped (time budget)")


def test_the_entry_limit_stops_the_index(built, monkeypatch):
    monkeypatch.setattr(C, "ENTITY_MAX_ENTRIES", 5)
    cat = _build(built)
    read = [e for es in cat.entities.values() for e in es if e.kind != "record_type"]
    assert 0 < len(read) <= 5
    assert "entry limit" in cat.entity_info["cut_reasons"] and cat.entity_lines()


def test_a_failing_statement_skips_its_object_and_never_fails_the_open(built, monkeypatch):
    real = Warehouse.run

    def run(self, sql, params=(), **kw):
        if "unnest([" in sql and '"Company__c"' in sql:
            raise RuntimeError("refused")
        return real(self, sql, params, **kw)

    monkeypatch.setattr(Warehouse, "run", run)
    cat = _build(built)
    assert cat.entity_info["errors"] >= 1 and "Company__c" in cat.entity_info["skipped_objects"]
    assert ("Company__c", "Name") not in cat.entity_fields and cat.entity_entries("zorvane")
    assert cat.entity_lines() == ["value index cut: 0 fields, 1 objects skipped (statement failed)"]


def test_the_value_index_and_the_stat_statements_do_not_change(cat, built, monkeypatch):
    monkeypatch.setattr(Catalog, "_entity_index", lambda self: None)
    plain = _build(built)
    assert plain.entities == {} and plain.entity_lines() == []
    assert {k: (v.values, v.blank, v.truncated, v.multi) for k, v in cat.values.items()} == \
        {k: (v.values, v.blank, v.truncated, v.multi) for k, v in plain.values.items()}
    assert cat.global_values == plain.global_values and cat._obj_values == plain._obj_values
    for api, spec in cat.objects.items():
        assert [st.sql for st in spec.stat_statements] == [st.sql for st in plain.objects[api].stat_statements], api
        assert not any("unnest([" in st.sql for st in spec.stat_statements)


# ----------------------------------------------------------------------------------- the real snapshot (live twin)
SNAPSHOT = Path(os.environ.get("SFA_LIVE_SNAPSHOT") or DEFAULT_WAREHOUSE)   # opened read-only
live = pytest.mark.skipif(os.environ.get("OKEVAL_RECORDS_LIVE") != "1" or not SNAPSHOT.is_file()
                          or not any(KNOWLEDGE_DIR.glob("*.json")),
                          reason="live: set OKEVAL_RECORDS_LIVE=1 with .private/okeval_run/warehouse.duckdb "
                                 "(or SFA_LIVE_SNAPSHOT) and the knowledge files present")
#: The person-derived fields of the 2026-10-13 snapshot the index must leave to the people path (design review,
#: amendment 1; API names only), and Name fields it must keep.
PERSON_DERIVED = [("Lead", "Header_Display__c"), ("Marketing__c", "Name"), ("Job_Submission__c", "Applicant_Name__c"),
                  ("Account", "Employee_Referral__c")]
KEPT_NAMES = [("Account", "Name"), ("Company__c", "Name"), ("Institute__c", "Name"), ("Department__c", "Name")]


@pytest.fixture(scope="module")
def live_tools():
    from sf_assistant.config import ResolverConfig, load_records_config
    from sf_assistant.tools.open_records import open_records

    t = open_records(load_records_config(warehouse=str(SNAPSHOT), today=dt.date(2026, 10, 13), tz="America/New_York"),
                     str(KNOWLEDGE_DIR), ResolverConfig(enabled=False))
    yield t
    t.close()


def _one(tools, sql: str) -> list:
    return tools.warehouse.run(sql, [], max_rows=5, timeout_s=30).rows


@live
def test_live_every_queryable_object_with_a_name_in_scope_has_entries(live_tools):
    cat = live_tools.catalog
    info = cat.entity_info
    assert not info["skipped_objects"] and not info["errors"] and cat.entity_lines() == []
    assert info["seconds"] <= C.ENTITY_BUILD_BUDGET_S
    person = set(info["person_fields"])
    missing = []
    for spec in cat.queryable():
        f = spec.fields.get("Name")
        if spec.sensitive or spec.rows == 0 or spec.api in C.EXTRA_OBJECTS or f is None or not f.has_data or \
                f.sensitivity or (spec.api in C._PERSON_NAMED and spec.api != "Account"):
            continue
        if (spec.api, "Name") not in cat.entity_fields and f"{spec.api}.Name" not in person:
            missing.append(spec.api)
    assert not missing
    for obj, fld in KEPT_NAMES:
        if obj in cat.objects and cat.objects[obj].has_table:
            assert (obj, fld) in cat.entity_fields, obj


@live
def test_live_no_entry_of_a_blocked_contact_or_sensitive_field_and_no_person_names(live_tools):
    cat = live_tools.catalog
    for es in cat.entities.values():
        for e in es:
            spec = cat.objects[e.obj]
            f = spec.fields[e.field]
            assert not spec.sensitive and e.obj not in C.EXTRA_OBJECTS, e.obj
            assert f.sensitivity is None and e.field.lower() not in cat.blocked_columns, (e.obj, e.field)
            assert hide_embedded_secrets(e.value) == e.value and "@" not in e.value, (e.obj, e.field)
            assert not (e.field == "Name" and e.obj in C._PERSON_NAMED and e.obj != "Account"), e.obj
    # Account Names: only the accounts that are no person (client companies)
    n = _one(live_tools, 'SELECT COUNT(DISTINCT trim(CAST(t0."Name" AS VARCHAR))) FROM main."Account" AS t0 '
                         'WHERE NOT COALESCE(t0."IsPersonAccount", false)')[0][0]
    assert cat.entity_fields[("Account", "Name")]["distinct"] <= n
    for obj, fld in PERSON_DERIVED:
        spec = cat.objects.get(obj)
        if spec is not None and fld in spec.fields and spec.fields[fld].has_data:
            assert (obj, fld) not in cat.entity_fields and f"{obj}.{fld}" in cat.entity_info["person_fields"], obj


@live
def test_live_named_records_of_the_owner_failures_are_indexed(live_tools):
    """The department most employees link to and the company most client interviews link to (read now, never written
    here) are entities, with their record counts; a case number is a code without its leading zeros."""
    cat = live_tools.catalog
    rows = _one(live_tools, 'SELECT d."Name", COUNT(*) AS n FROM main."Recruiter__c" AS r JOIN main."Department__c" '
                            'AS d ON d."Id" = r."Department__c" GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1')
    if rows:
        dept = [e for e in cat.entity_entries(entity_key(rows[0][0])) if (e.obj, e.field) == ("Department__c", "Name")]
        assert dept and sum(e.records for e in dept) >= 1
    if "company_new__c" in cat.objects["Interview__c"].fields and "Company__c" in cat.objects:
        rows = _one(live_tools, 'SELECT c."Name", COUNT(*) AS n FROM main."Interview__c" AS i JOIN main."Company__c" '
                                'AS c ON c."Id" = i."company_new__c" GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 1')
        key = entity_key(rows[0][0])
        n = sum(1 for (v,) in live_tools.warehouse.run('SELECT t0."Name" FROM main."Company__c" AS t0', [],
                                                       max_rows=100_000, timeout_s=30).rows if entity_key(v) == key)
        company = [e for e in cat.entity_entries(key) if (e.obj, e.field) == ("Company__c", "Name")]
        assert company and sum(e.records for e in company) == n
    case = cat.objects.get("Case")
    if case is not None and case.has_table and "CaseNumber" in case.fields and case.fields["CaseNumber"].has_data:
        num = _one(live_tools, 'SELECT t0."CaseNumber" FROM main."Case" AS t0 WHERE t0."CaseNumber" IS NOT NULL '
                               'ORDER BY 1 LIMIT 1')[0][0]
        assert ("Case", "CaseNumber", "code") in {(e.obj, e.field, e.kind)
                                                  for e in cat.entity_entries(str(num).lstrip("0") or "0")}
    long_names = [k for k, es in cat.entities.items() if 7 <= len(k.split()) <= 12
                  and any((e.obj, e.field) == ("Institute__c", "Name") for e in es)]
    if "Institute__c" in cat.objects and cat.objects["Institute__c"].has_table:
        assert long_names                       # Names keep up to 12 words (amendment 2)
    assert BLANK_VALUES >= {"none", "test", "n a"}
