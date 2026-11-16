"""sf_assistant.engine.catalog: knowledge joined with the warehouse schema (synthetic warehouse; real knowledge metadata once)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

from sf_assistant.config import DEFAULT_WAREHOUSE, KNOWLEDGE_DIR, PRIVATE_DIR

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant.engine.catalog import (BLOCKED_COLUMN_RE, CONTACT_COLUMN_RE, Catalog,  # noqa: E402
                          RecordError, blocked_text)
from sf_assistant.engine.warehouse import Warehouse  # noqa: E402
from warehouse_fixture import make_warehouse, open_fixture  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")

#: Column names for the pattern tests: synthetic, in the shapes of real Salesforce fields. The @live tests compare the
#: patterns with a real snapshot and need that snapshot's own names: .private/real_columns.json, git-ignored, replaces
#: these three lists when it is there.
REAL_COLUMNS = PRIVATE_DIR / "real_columns.json"
REAL_BLOCKED = {
    "Object_A__c": ["AccountNumber", "XX_Bank_Name__c", "Sample_Last4_SSN__c", "Sample_Passport_Number__c",
                    "XX_Sample_Identification_Number__c", "Sample_Bank_Account_Last4__c"],
    "Object_B__c": ["Sample_Credential__c", "Session_Id__c", "Sample_Passcode__c", "Sample_Password_Note__c",
                    "Token__c", "Public_Token__c"],
    "User": ["LastPasswordChangeDate", "PasswordExpirationDate"],
}
REAL_SECRET_METADATA = {"User": ["LastPasswordChangeDate", "PasswordExpirationDate"]}
REAL_SPECIAL_CATEGORY = {
    "Object_A__c": ["PersonBirthdate", "Date_of_Birth__c", "Alt_Date_Of_Birth__c", "Sample_Race_Ethnicity__c",
                    "Sample_Disability_Status__c", "Sample_Military_Status__c", "Sample_Criminal_Record__c"],
    "Contact": ["Birthdate"],
}
if REAL_COLUMNS.is_file():
    globals().update(json.loads(REAL_COLUMNS.read_text(encoding="utf-8")))
#: The pattern the special-category fields were blocked by until phase 1 (tests only: what it took must stay readable).
SPECIAL_CATEGORY_RE = re.compile(r"(birth_?(date|day)|date_?of_?birth|(^|_)dob(_|$)|(^|_)race(_|$)|ethnicit|disabilit"
                                 r"|militar|veteran|criminal|conviction)", re.IGNORECASE)
#: Real column names that must stay queryable (the last rows: snapshot names close to the special-category words).
REAL_NOT_BLOCKED = ["Question_Bank__c", "Question__c", "Section__c", "Session_Window__c", "Candidate__c",
                    "Interview_Type__c", "Status__c", "Sample_Visa_Status__c", "PersonHasOptedOutOfEmail",
                    "PersonEmailBouncedDate", "Human_Total_Score__c", "Sample_Payment_Status__c",
                    "Notes__c", "Recruiter__c", "Host_User__c",
                    "StackTrace", "PermissionsTraceXdsQueries", "IsNavPersonalizationDisabled",
                    "UserPreferencesDisableAllFeedsEmail", "OptionsReputationRecordConversationsDisabled",
                    "Background_Check_Status__c", "Before_Background_Check__c", "Ref_1_Designation__c",
                    "Designation__c", "AgeInDays", "TaxEffectiveDate", "ProcessExceptionNumber"]


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = open_fixture(tmp_path_factory.mktemp("c"))
    yield t
    t.close()


@pytest.fixture
def cat(tools) -> Catalog:
    return tools.catalog


def test_queryable_set_and_no_table_objects(cat):
    q = sorted(s.api for s in cat.queryable())
    assert q == ["Account", "Candidate_Portal_Credential__c", "Candidate_Training__c", "Deliverable__c", "Department__c",
                 "Group", "Internal_Interview__c", "Interview_Participant__c", "Interview__c", "Log_Entry__c",
                 "Program_Version__c", "Program__c", "RecordType", "Recruiter__c", "Session__c", "User"]
    assert cat.objects["Training_Mock_Result__c"].has_table is False
    with pytest.raises(RecordError) as exc:
        cat.resolve_object("Training_Mock_Result__c")
    assert exc.value.code == "E1b" and "no table in the warehouse" in str(exc.value)
    assert cat.objects["Account"].documented is False and cat.objects["Interview__c"].documented is True
    assert ("main", "Interview__History") in cat.allowed and ("main", "Organization") not in cat.allowed


def test_system_fields_and_kinds(cat):
    f = cat.objects["Interview__c"].fields
    assert f["Id"].kind == "id" and f["Id"].meaning == "record Id"
    assert f["OwnerId"].kind == "reference" and f["OwnerId"].ref_targets == ["User", "Group"]
    assert f["RecordTypeId"].ref_targets == ["RecordType"] and f["CreatedById"].ref_targets == ["User"]
    assert f["CreatedDate"].kind == "datetime" and f["CreatedDate"].meaning == "when the record was created"
    assert f["Date_of_Interview__c"].kind == "date"
    assert f["Interview_Status__c"].kind == "picklist" and f["Evaluation_Parameter__c"].kind == "multipicklist"
    assert f["Client_Bill_Rate__c"].kind == "currency" and f["IsDeleted"].kind == "boolean"
    assert f["Recruiter__c"].kind == "reference" and f["Recruiter__c"].ref_targets == ["Recruiter__c"]
    assert f["Name"].auto_number is True
    assert cat.objects["Internal_Interview__c"].fields["Human_Total_Score__c"].kind == "number"
    assert cat.objects["Account"].fields["Name"].kind == "name"
    assert f["Date_of_Interview__c"].temporal_keys[0][0] == "interview_date"


def test_prefix_detection_of_undocumented_lookup(cat):
    f = cat.objects["Interview__c"].fields["Support_Ref__c"]
    assert f.documented is False and f.kind == "reference" and f.ref_targets == ["Recruiter__c"]
    assert "detected from the Id prefix" in f.meaning


def test_documented_not_synced(cat):
    ns = cat.objects["Interview__c"].not_synced
    assert set(ns) == {"Account_Manager__c", "Old_Status__c"}
    with pytest.raises(RecordError) as exc:
        cat.field(cat.objects["Interview__c"], "Old_Status__c")
    assert exc.value.code == "E2b" and "Use Interview_Status__c instead." in str(exc.value)
    with pytest.raises(RecordError) as exc:
        cat.field(cat.objects["Interview__c"], "Account Manager")
    assert exc.value.code == "E2b"


def test_children_index(cat):
    kids = {(c.child, c.via) for c in cat.objects["Recruiter__c"].children}
    assert {("Interview__c", "Recruiter__c"), ("Interview__c", "Support_Ref__c"), ("Session__c", "Host_User__c"),
            ("Session__c", "Co_Host__c")} <= kids
    assert ("Session__c", "Candidate_Training__c") in {(c.child, c.via) for c in cat.objects["Candidate_Training__c"].children}


def test_sensitivity_classification(cat):
    acc = cat.objects["Account"].fields
    assert acc["Passport_Number__c"].sensitivity == "blocked"
    assert acc["PersonEmail"].sensitivity == "contact" and acc["Phone"].sensitivity == "contact"
    assert cat.objects["Recruiter__c"].fields["Email__c"].sensitivity == "contact"
    cred = cat.objects["Candidate_Portal_Credential__c"].fields
    assert cred["Password__c"].sensitivity == "blocked" and cred["Username__c"].sensitivity == "blocked"
    assert cred["Name"].sensitivity is None and cred["Status__c"].sensitivity is None
    assert "zoom_passcode__c" in cat.blocked_columns and "passport_number__c" in cat.blocked_columns
    # a date named after a password holds no secret (review F8): readable, and no blocked column name
    assert cat.objects["User"].fields["LastPasswordChangeDate"].sensitivity is None
    assert "lastpasswordchangedate" not in cat.blocked_columns


def test_blocked_and_contact_patterns_on_real_column_names():
    for obj, cols in REAL_BLOCKED.items():
        for c in cols:
            assert BLOCKED_COLUMN_RE.search(c), f"{obj}.{c} must be blocked"
    for c in REAL_NOT_BLOCKED:
        assert not BLOCKED_COLUMN_RE.search(c), f"{c} must not be blocked"
    assert not BLOCKED_COLUMN_RE.search("Question_Bank__c")
    for obj, cols in REAL_SPECIAL_CATEGORY.items():   # phase 1: readable
        for c in cols:
            assert not BLOCKED_COLUMN_RE.search(c) and SPECIAL_CATEGORY_RE.search(c), f"{obj}.{c}"
    for obj, cols in REAL_BLOCKED.items():            # no very-high column is a special-category one
        for c in cols:
            assert not SPECIAL_CATEGORY_RE.search(c), f"{obj}.{c}"
    for c in ("PersonEmail", "Phone", "MobilePhone", "BillingStreet", "MailingPostalCode", "LinkedIn_Profile__c"):
        assert CONTACT_COLUMN_RE.search(c)
    # the session-id and identification-number words of phase 1 take no look-alike
    for c in ("MessagingSessionId", "Session__c", "Session_Window__c", "Session_Identifier__c",
              "DS_ACH_Identification_Type__c", "DS_ACH_Issuing_Authority__c", "Identification_Status__c"):
        assert not BLOCKED_COLUMN_RE.search(c), c
    for c in ("Session_Id__c", "SessionId", "ZVC__Session_ID__c", "Session_Key__c", "National_Identification_Number__c"):
        assert BLOCKED_COLUMN_RE.search(c), c


def test_resolution_and_suggestions(cat):
    assert cat.resolve_object("interview__c").api == "Interview__c"
    assert cat.resolve_object("Employees").api == "Recruiter__c"
    assert cat.resolve_object("client interview").api == "Interview__c"
    with pytest.raises(RecordError) as exc:
        cat.resolve_object("Interviewz")
    assert exc.value.code == "E1" and "Interview__c" in str(exc.value) and "find_objects" in str(exc.value)
    with pytest.raises(RecordError) as exc:
        cat.resolve_object("AccountHistory")
    assert exc.value.code == "E1c"
    sp = cat.objects["Interview__c"]
    assert cat.field(sp, "date of interview").api == "Date_of_Interview__c"
    assert cat.field(sp, "interview_status__C").api == "Interview_Status__c"
    assert cat.field(sp, "Interview Outcome").api == "Interview_Outcome__c"
    with pytest.raises(RecordError) as exc:
        cat.field(sp, "Outcome_Status")
    assert exc.value.code == "E2" and "Interview_Outcome__c" in str(exc.value) and "describe_fields" in str(exc.value)


def test_path_aliases_and_errors(cat):
    sp = cat.objects["Interview__c"]
    ref = cat.resolve_path(sp, "Recruiter__r.Name")
    assert ref.text == "Recruiter__c.Name" and ref.notes and ref.obj == "Recruiter__c"
    ref = cat.resolve_path(sp, "Owner.Name")
    assert ref.text == "OwnerId.Name"
    ref = cat.resolve_path(sp, "RecordType.Name")
    assert ref.obj == "RecordType"
    ref = cat.resolve_path(sp, "Interview__c.Interview_Status__c")
    assert ref.text == "Interview_Status__c" and not ref.hops
    with pytest.raises(RecordError) as exc:
        cat.resolve_path(sp, "Interview_Status__c.Name")
    assert exc.value.code == "E3" and "Lookup fields of Interview__c" in str(exc.value)
    with pytest.raises(RecordError) as exc:
        cat.resolve_path(cat.objects["Candidate_Training__c"], "Session__c.Name")
    assert exc.value.code == "E3c" and "related=" in str(exc.value)
    with pytest.raises(RecordError) as exc:
        cat.resolve_path(sp, "Recruiter__c.Department__c.Name.X.Y")
    assert exc.value.code == "E3b"


def test_record_types_and_number_prefixes(cat):
    rts = {r["developer_name"]: r["records"] for r in cat.objects["Interview__c"].record_types}
    assert rts == {"Interview": 8, "Initial_Call": 1, None: 1}
    assert "Interview__c" in cat.number_prefixes["i"]


def test_value_index(cat):
    e = cat.values_of("Interview__c", "Interview_Outcome__c")
    assert dict(e.values)["Ghosted"] == 2 and e.blank == 4 and e.statement.cached
    assert cat.values_of("Interview__c", "Evaluation_Parameter__c").multi
    assert cat.values_of("Interview__c", "Zoom_Passcode__c") is None
    assert ("Interview__c", "Interview_Outcome__c", "Offer Received", 2) in cat.global_values["offer received"]


def test_entity_index_of_the_fixture(cat):
    """The entity index (value spots, 2026-10-28; tests/test_entity_index.py has the rules): the fixture's Names,
    codes and picklists are entities; people, blocked and sensitive data are not; nothing was cut, so no start-up
    line; no index statement is kept in stat_statements."""
    assert {(e.obj, e.field, e.kind) for e in cat.entity_entries("dept one")} == {("Department__c", "Name", "name")}
    assert {(e.obj, e.field, e.kind) for e in cat.entity_entries("i 0001")} == {("Interview__c", "Name", "code")}
    assert [(e.kind, e.records) for e in cat.entity_entries("ghosted")] == [("picklist", 2)]
    assert [(e.field, e.records) for e in cat.entity_entries("initial call")] == [("RecordTypeId", 1)]
    objects = {o for o, _f in cat.entity_fields}
    assert not objects & {"Candidate_Portal_Credential__c", "RecordType", "Group"}
    # people's Names are the people path's (the fixture's Account has no IsPersonAccount flag: no Account Name at all);
    # their picklists and record types are design vocabulary like any other
    assert not {(o, f) for o, f in cat.entity_fields if f == "Name"} & {("Recruiter__c", "Name"), ("User", "Name"),
                                                                        ("Account", "Name")}
    assert ("Recruiter__c", "Employment_Status__c") in cat.entity_fields
    assert ("Account", "RecordTypeId") in cat.entity_fields
    assert "recruiter a" not in cat.entities and "candidate 1" not in cat.entities
    assert ("Interview__c", "Zoom_Passcode__c") not in cat.entity_fields and "zp 111" not in cat.entities
    assert ("Log_Entry__c", "Message__c") not in cat.entity_fields
    assert cat.entity_lines() == [] and cat.entity_cues("Department__c") >= {"department"}
    assert not any("unnest([" in st.sql for o in cat.objects.values() for st in o.stat_statements)


@pytest.mark.skipif(not any(KNOWLEDGE_DIR.glob("*.json")),
                    reason="the git-ignored knowledge files knowledge/objects/*.json are not there")
def test_all_real_knowledge_files_load_against_the_synthetic_warehouse(tmp_path):
    wh = Warehouse.open(str(make_warehouse(tmp_path)))
    try:
        cat = Catalog.build(wh, str(KNOWLEDGE_DIR))
        assert cat.objects["Interview__c"].documented
        assert cat.objects["Interview__c"].has_table
        assert len(cat.objects) > 40
        assert not cat.load_errors or all(":" in e for e in cat.load_errors)
    finally:
        wh.close()


# ----------------------------------------------------------------------------------- lookup roles (RECORDS_SPEC §21.3)

def test_lookup_roles_meanings_and_unresolved_targets(cat):
    ii = cat.objects["Internal_Interview__c"].fields
    f = ii["Interviewer__c"]
    assert (f.ref_role, f.from_target) == ("interviewer", "interviews this employee conducts")
    assert f.ref_meaning == "Employee conducting the interview."
    assert f.role_text() == "interviewer: Employee conducting the interview."
    assert ii["Interview_Support_Person__c"].role == "support employee" and ii["Interview_Support_Person__c"].legacy
    assert ii["Interview_Readiness_Person__c"].ref_role == "undocumented"
    assert ii["Interview_Readiness_Person__c"].role_text() == "undocumented"
    assert cat.objects["Interview__c"].fields["Recruiter__c"].unresolved == 1      # the orphan row
    assert cat.objects["Interview__c"].fields["Candidate__c"].unresolved == 0
    cb = cat.objects["Interview__c"].fields["CreatedById"]
    assert (cb.ref_role, cb.concept, cb.from_target) == ("record creator", "user who created the record",
                                                         "records this user created")
    assert cat.objects["Interview__c"].fields["OwnerId"].role == "record owner"
    ns = cat.objects["Internal_Interview__c"].not_synced["Final_Decision_By__c"]
    assert ns["targets"] == ["Recruiter__c"] and ns["role"] == "final_decision_maker"
    assert any(st.sql.count("NOT IN (SELECT") for st in cat.objects["Interview__c"].stat_statements)


def test_same_target_lookups_and_lookup_groups(cat):
    ii = cat.objects["Internal_Interview__c"]
    sibs = [g.api for g in cat.same_target_lookups(ii, ii.fields["Interviewer__c"])]
    assert sibs == ["Recruiter__c", "Interview_Readiness_Person__c", "Interview_Support_Person__c"]  # filled descending
    iv = cat.objects["Interview__c"]
    assert [g.api for g in cat.same_target_lookups(iv, iv.fields["Recruiter__c"])] == ["Support_Ref__c"]
    assert [g.api for g in cat.same_target_lookups(iv, iv.fields["CreatedById"])] == ["OwnerId"]  # system with system
    groups = dict(cat.lookup_groups(ii))
    items = groups["Recruiter__c"]
    assert [g.api for g in items if not isinstance(g, str)] == ["Interviewer__c", "Recruiter__c",
                                                                 "Interview_Readiness_Person__c",
                                                                 "Interview_Support_Person__c"]
    assert items[-1] == "Final_Decision_By__c"          # documented, not synced, listed last
    f = ii.fields["Interview_Support_Person__c"]
    saved = f.filled
    try:
        f.filled = 0                                    # an empty lookup is left out
        assert "Interview_Support_Person__c" not in [getattr(g, "api", g) for g in dict(cat.lookup_groups(ii))["Recruiter__c"]]
    finally:
        f.filled = saved
    assert cat.not_synced_lookups(ii, "Recruiter__c") == ["Final_Decision_By__c"]
    assert set(cat.person_lookups(ii)) == {"Recruiter__c", "Account"}


def test_role_words(cat):
    from sf_assistant.engine.catalog import FieldSpec

    ii = cat.objects["Internal_Interview__c"]
    assert cat.role_words(ii, ii.fields["Interviewer__c"]) == ["interviewer", "conducts"]
    assert cat.role_words(ii, ii.fields["Interview_Readiness_Person__c"]) == ["interview", "readiness", "person"]
    assert cat.role_words(cat.objects["Interview__c"], cat.objects["Interview__c"].fields["CreatedById"]) == ["record", "creator"]
    ct = cat.objects["Candidate_Training__c"]
    only_objects = FieldSpec(api="Candidate__c", label="Candidate", kb_type="reference", db_type="VARCHAR", kind="reference",
                             meaning="", ref_targets=["Account"], documented=True, ref_role="",
                             from_target="trainings of the candidate", filled=4)
    assert cat.role_words(ct, only_objects) == []      # a from_target made only of object words has no action words


def test_role_fits_and_question_cues(cat):
    from sf_assistant.engine.catalog import FieldSpec

    ii = cat.objects["Internal_Interview__c"]
    q = "how many interviews has delta taken"
    assert cat.role_fits(q, ii, ii.fields["Interviewer__c"], "employee") == "taken"
    assert cat.role_fits(q, ii, ii.fields["Recruiter__c"], "employee") is None
    assert cat.role_fits(q, ii, ii.fields["Candidate__c"], "candidate") == "taken"
    assert cat.role_fits(q, ii, ii.fields["Candidate__c"], "employee") is None
    assert cat.role_fits(q, ii, ii.fields["Interviewer__c"], "other") is None
    rows = ii.rows
    try:
        ii.rows = 1000                                  # 3 of 1,000 rows: almost empty, never marked
        assert cat.almost_empty(ii, ii.fields["Interviewer__c"])
        assert cat.role_fits(q, ii, ii.fields["Interviewer__c"], "employee") is None
    finally:
        ii.rows = rows
    looks_like = FieldSpec(api="Lead_Person__c", label="Interviewer", kb_type="reference", db_type="VARCHAR",
                           kind="reference", meaning="interviewer of the mock", ref_targets=["Recruiter__c"],
                           documented=True, ref_role="lead_person", concept="interviewer", from_target="mocks of this lead",
                           filled=3)
    assert cat.role_fits(q, ii, looks_like, "employee") is None  # label and concept are never role words
    assert cat.question_cues("how many mock interviews has delta person taken", "employee") == ["taken"]
    assert cat.question_cues("how many interviews per recruiter", "employee") == ["recruiter"]
    assert cat.question_cues("how many interviews in total", "employee") == []
    assert cat.question_cues("who hosted and supported", "other") == []


def test_person_kind_names_and_identity_links(cat):
    from sf_assistant.engine.catalog import norm_name

    assert cat.person_kind("Recruiter__c") == "employee" and cat.person_kind("User") == "employee"
    assert cat.person_kind("Account", "Person Account") == "candidate"
    assert cat.person_kind("Account", "Recruiter") == "other" and cat.person_kind("Interview__c") == "other"
    assert norm_name("  Delta \xa0 Person ") == "delta person" and norm_name("Delta\u00a0Person") == "delta person"
    links = {(lk.col, lk.dst, lk.dst_col): lk.label for lk in cat.identity_links("Recruiter__c")}
    assert links == {("Recruiter_User__c", "User", "Id"): "Recruiter_User__c (employee login user)",
                     ("Recruiter_Contact__c", "Account", "Id"): "Recruiter_Contact__c (employee person account)",
                     ("Recruiter_Contact__c", "User", "AccountId"): "portal user (User.AccountId = Recruiter_Contact__c)",
                     ("Portal_User__c", "User", "Id"): "Portal_User__c (employee portal login user)"}
    assert {lk.src for lk in cat.linked_from("User")} == {"Recruiter__c"}
    assert cat.objects["User"].fields["AccountId"].ref_role == "undocumented"   # found from the Id prefix, not a link


def test_e2b_for_a_documented_lookup_that_is_not_synced(cat):
    ii = cat.objects["Internal_Interview__c"]
    with pytest.raises(RecordError) as exc:
        cat.field(ii, "Final_Decision_By__c")
    e = str(exc.value)
    assert exc.value.code == "E2b"
    assert e.startswith("Internal_Interview__c.Final_Decision_By__c (final decision maker) is documented in the knowledge "
                        "but is not synced to the warehouse, so it cannot be queried; say it is not recorded in the "
                        "warehouse, never 0. Lookups to Employee that can be queried")
    assert "Interviewer__c (interviewer)" in e
    with pytest.raises(RecordError) as exc:                       # the path form keeps E2b (not E3)
        cat.resolve_path(ii, "Final_Decision_By__c.Name")
    assert exc.value.code == "E2b" and "is not synced" in str(exc.value)
    with pytest.raises(RecordError) as exc:                       # a real non-lookup hop is still E3
        cat.resolve_path(ii, "Status__c.Name")
    assert exc.value.code == "E3"


def test_object_note_of_the_employee_object(cat):
    assert "pick the lookup whose role fits the question, not the one named Recruiter__c" in cat.objects["Recruiter__c"].note


def test_sparse_fits_marks_an_almost_empty_lookup_of_the_asked_role(cat):
    ii = cat.objects["Internal_Interview__c"]
    q = "how many mock interviews has delta supported"
    sup = ii.fields["Interview_Support_Person__c"]
    assert cat.sparse_fits(q, ii, sup, "employee") is None          # 1 of 6 rows: not almost empty, role_fits marks it
    assert cat.role_fits(q, ii, sup, "employee") == "supported"
    rows = ii.rows
    try:
        ii.rows = 1000                                              # 1 of 1,000 rows: almost empty
        assert cat.role_fits(q, ii, sup, "employee") is None
        assert cat.sparse_fits(q, ii, sup, "employee") == "supported"
        assert cat.sparse_fits(q, ii, sup, "other") is None
        assert cat.sparse_fits("how many mock interviews has delta taken", ii, sup, "employee") is None
        assert cat.sparse_fits(q, ii, ii.fields["Interviewer__c"], "employee") is None   # filled: not sparse
    finally:
        ii.rows = rows


def test_mirror_lookups_come_from_path_field_dependencies(cat):
    ct = cat.objects["Candidate_Training__c"]
    assert ct.fields["Program__c"].mirror_of == "Program_Version__c.Program__c"
    assert ct.fields["Program__c"].mirror_rule == "The training's Program must equal the version's Program."
    assert all(not f.mirror_of for o in cat.objects.values() for f in o.fields.values()
               if (o.api, f.api) != ("Candidate_Training__c", "Program__c"))


# ------------------------------ special-category personal fields: blocked 2026-10-19 (RECORDS_SPEC §22.18), readable
# again since phase 1 (owner, 2026-10-26: the first users are admins; only the very-high fields stay blocked). A copy of
# the people fixture with a candidate account, a lead and account history rows that hold special-category values. The
# values are synthetic markers (ZQ-..., 1977-...) that no other fixture row holds, so a tool result shows them by their
# text. Lima Nobel's documented date of birth (1990-01-01) is on Recruiter__c.
ACC_N, LEAD_N = "001000000000040AAA", "00Q000000000040AAA"
PERSONAL_FIXTURE = {
    "Account": ["PersonBirthdate", "Alt_Date_Of_Birth__c", "Sample_Race_Ethnicity__c", "Sample_Disability_Status__c",
                "Sample_Military_Status__c", "Sample_Criminal_Record__c"],
    "Lead": ["Sample_Criminal_Record__c"],
    "Recruiter__c": ["Date_of_Birth__c"],
}


@pytest.fixture(scope="module")
def stools(tmp_path_factory):
    import copy
    import datetime as dt

    import warehouse_fixture as F
    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    tables = copy.deepcopy(F.PEOPLE_TABLES)
    day = "2025-01-01T00:00:00.000Z"
    F._with(tables, "Account", {"PersonBirthdate": "DATE", "Alt_Date_Of_Birth__c": "DATE", "Sample_Race_Ethnicity__c": None,
                                "Sample_Disability_Status__c": None, "Sample_Military_Status__c": None,
                                "Sample_Criminal_Record__c": None}, [
        {"Id": ACC_N, "Name": "November Marker", "RecordTypeId": F.RT_PERSON, "CreatedDate": day,
         "IsPersonAccount": "true", "Assigned_Recruiter__c": F.REC_LN, "PersonBirthdate": "1977-07-07",
         "Alt_Date_Of_Birth__c": "1977-07-08", "Sample_Race_Ethnicity__c": "ZQ-RACE-1", "Sample_Disability_Status__c": "ZQ-DISAB-1",
         "Sample_Military_Status__c": "ZQ-MIL-1", "Sample_Criminal_Record__c": "ZQ-CRIM-1"}])
    F._with(tables, "Lead", {"Sample_Criminal_Record__c": None}, [
        {"Id": LEAD_N, "Name": "November Lead", "CreatedDate": day, "Status": "Open", "IsConverted": "false",
         "OwnerId": F.USER_1, "Sample_Criminal_Record__c": "ZQ-LEADCRIM-1"}])
    F._with(tables, "Interview__c", {}, [
        {"Id": F.sid("a0A", 40), "Name": "I-0040", "IsDeleted": "false", "CreatedDate": day,
         "Interview_Status__c": "Completed", "Recruiter__c": F.REC_LN, "Candidate__c": ACC_N}])
    hist = ("017000000000040AAA", "017000000000041AAA", "017000000000042AAA")
    F._with(tables, "AccountHistory", {"Id": None, "IsDeleted": "BOOLEAN", "AccountId": None, "CreatedById": None,
                                       "CreatedDate": "TIMESTAMP", "Field": None, "DataType": None, "OldValue": None,
                                       "NewValue": None}, [
        {"Id": hist[0], "IsDeleted": "false", "AccountId": ACC_N, "CreatedById": F.USER_1,
         "CreatedDate": "2026-09-01T10:00:00.000Z", "Field": "Sample_Race_Ethnicity__c", "DataType": "Text",
         "OldValue": "ZQ-OLDRACE-1", "NewValue": "ZQ-RACE-1"},
        {"Id": hist[1], "IsDeleted": "false", "AccountId": ACC_N, "CreatedById": F.USER_1,
         "CreatedDate": "2026-09-02T10:00:00.000Z", "Field": "PersonBirthdate", "DataType": "Date",
         "OldValue": "1977-01-01", "NewValue": "1977-07-07"},
        {"Id": hist[2], "IsDeleted": "false", "AccountId": ACC_N, "CreatedById": F.USER_1,
         "CreatedDate": "2026-09-03T10:00:00.000Z", "Field": "Name", "DataType": "Text", "OldValue": "November Old",
         "NewValue": "November Marker"}])
    knowledge = copy.deepcopy(F.PEOPLE_KNOWLEDGE)
    by = {x["object"]["api_name"]: x for x in knowledge}
    by["Account"]["fields"] += [
        F._field("Sample_Race_Ethnicity__c", "Race/Ethnicity", "picklist", "Race or ethnicity the candidate declared.",
                 values=["KB Option"], roles=["categorical_attribute"]),
        F._field("Sample_Criminal_Record__c", "Criminal Record Declaration", "picklist",
                 "Whether the candidate declared a criminal record.", values=["KB Option"],
                 roles=["categorical_attribute"])]
    by["Lead"]["fields"] += [F._field("Sample_Criminal_Record__c", "Criminal Record Declaration", "picklist",
                                      "Whether the lead declared a criminal record.", values=["KB Option"],
                                      roles=["categorical_attribute"])]
    root = tmp_path_factory.mktemp("personal")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = open_records(load_records_config(warehouse=str(F.make_warehouse(root, tables=tables)),
                                             today=dt.date(2026, 10, 13)),
                         str(F.make_knowledge(root, knowledge=knowledge)))
    yield t
    t.close()


def _call(t, name, args) -> tuple[dict, str | None, str]:
    t.begin_question()
    out = t.call(name, args)
    return out, (t.evidence[-1].error_code if t.evidence else None), json.dumps(out)


def test_special_category_names_are_not_blocked_and_look_alikes_never_were():
    for c in ("Date_of_Birth__c", "Alt_Date_Of_Birth__c", "PersonBirthdate", "Birthdate", "Birth_Date__c", "BirthDay__c",
              "DOB__c", "Candidate_DOB__c", "Sample_Race_Ethnicity__c", "Race__c", "Ethnicity__c", "Sample_Disability_Status__c",
              "Has_Disabilities__c", "Sample_Military_Status__c", "Veteran_Status__c", "Sample_Criminal_Record__c",
              "Criminal_Conviction__c", "Conviction_Details__c"):
        assert SPECIAL_CATEGORY_RE.search(c) and not BLOCKED_COLUMN_RE.search(c), c
    for c in ("Grace_Period__c", "Trace_Id__c", "Embrace__c", "Terrace__c", "Bracelet__c", "Is_Disabled__c",
              "Disable_Emails__c", "Rebirth_Program__c", "Adobe_Sign_Id__c", "Dobson__c", "Ethics_Score__c",
              "Date_of_Joining__c", "Birth_Country__c", "Gender__c", "Age__c"):
        assert not BLOCKED_COLUMN_RE.search(c), c


def test_blocked_text_of_a_very_high_field():
    e = blocked_text("Account", "Passport_Number__c")
    assert e.startswith("Account.Passport_Number__c is a sensitive field (credential, passcode or identity document); "
                        "it cannot be read, filtered, grouped or shown.")
    assert "special-category" not in e


def _personal_calls() -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    fx = lambda f, op="is_not_blank": {"field": f, "op": op}  # noqa: E731
    for obj, cols in PERSONAL_FIXTURE.items():
        for c in cols:
            out += [("list_records", {"object": obj, "fields": [c]}),
                    ("list_records", {"object": obj, "order_by": c}),
                    ("list_records", {"object": obj, "filters": [fx(c)]}),
                    ("get_field_values", {"object": obj, "field": c}),
                    ("get_field_values", {"object": obj, "field": c, "contains": "ZQ"}),
                    ("group_records", {"object": obj, "group_by": c}),
                    ("group_records", {"object": obj, "group_by": "OwnerId" if obj == "Lead" else "Name", "split_by": c}),
                    ("count_records", {"object": obj, "filters": [fx(c)]}),
                    ("count_records", {"object": obj, "filters": [{"field": c, "op": "eq", "value": "ZQ-RACE-1"}]}),
                    ("count_records", {"object": obj, "count_distinct": c}),
                    ("count_records", {"object": obj, "any_of": [fx(c), fx("Name")]}),
                    ("percentage_of_records", {"object": obj, "part_filters": [fx(c)]}),
                    ("compare_records", {"object": obj, "segments": [{"label": "a", "filters": [fx(c, "is_blank")]},
                                                                     {"label": "b", "filters": [fx(c)]}]})]
            if obj == "Account":                    # the fixture's only history table
                out += [("get_field_history", {"object": obj, "field": c}),
                        ("get_field_history", {"object": obj, "field": c, "changed_to": "ZQ-RACE-1"})]
    for obj, c in (("Account", "PersonBirthdate"), ("Account", "Alt_Date_Of_Birth__c"), ("Recruiter__c", "Date_of_Birth__c")):
        out += [("aggregate_records", {"object": obj, "op": "min", "field": c}),
                ("count_records", {"object": obj, "date_field": c, "period": "last 60 years"}),
                ("trend_records", {"object": obj, "grain": "year", "date_field": c, "period": "last 60 years"})]
    # lookup paths, one and two hops, and child-record conditions
    for path in ("Candidate__c.Sample_Race_Ethnicity__c", "Candidate__r.PersonBirthdate", "Candidate__c.Sample_Criminal_Record__c",
                 "Recruiter__c.Date_of_Birth__c", "Recruiter__r.Date_of_Birth__c",
                 "Candidate__c.Assigned_Recruiter__c.Date_of_Birth__c"):
        out += [("list_records", {"object": "Interview__c", "fields": [path]}),
                ("list_records", {"object": "Interview__c", "order_by": path}),
                ("get_field_values", {"object": "Interview__c", "field": path}),
                ("group_records", {"object": "Interview__c", "group_by": path}),
                ("count_records", {"object": "Interview__c", "filters": [fx(path)]})]
    out += [("list_records", {"object": "Internal_Interview__c", "fields": ["Interviewer__c.Date_of_Birth__c"]}),
            ("count_records", {"object": "Recruiter__c", "related": [
                {"child_object": "Account", "via_field": "Assigned_Recruiter__c", "exists": True,
                 "filters": [fx("Sample_Race_Ethnicity__c")]}]})]
    return out


def test_personal_fields_are_readable_in_the_catalog(stools):
    cat = stools.catalog
    for obj, cols in PERSONAL_FIXTURE.items():
        for c in cols:
            f = cat.objects[obj].fields[c]
            assert f.sensitivity is None and c.lower() not in cat.blocked_columns, (obj, c)
            assert (obj.lower(), c.lower()) not in cat.blocked_fields and f.filled, (obj, c)
    assert cat.values_of("Account", "Sample_Race_Ethnicity__c") is not None   # a documented picklist: value-indexed again
    st = stools.warehouse.run('SELECT t0."Sample_Race_Ethnicity__c" FROM main."Account" AS t0 WHERE t0."Sample_Race_Ethnicity__c" '
                              "IS NOT NULL", [], max_rows=5, timeout_s=10)
    assert st.rows == [["ZQ-RACE-1"]]


def test_record_tools_read_personal_fields_directly_and_through_lookups(stools):
    calls = _personal_calls()
    assert len(calls) > 120
    for name, args in calls:
        out, code, text = _call(stools, name, args)
        assert code != "E2c" and "sensitive field" not in (out["error"] or ""), (name, args, code, out["error"])
        assert "special-category" not in text, (name, args)
    for obj, c, marker in (("Account", "Sample_Race_Ethnicity__c", "ZQ-RACE-1"), ("Account", "PersonBirthdate", "1977-07-07"),
                           ("Account", "Sample_Criminal_Record__c", "ZQ-CRIM-1"),
                           ("Lead", "Sample_Criminal_Record__c", "ZQ-LEADCRIM-1"),
                           ("Recruiter__c", "Date_of_Birth__c", "1990-01-01")):
        out, code, text = _call(stools, "list_records", {"object": obj, "fields": [c], "limit": 50})
        assert out["ok"] and marker in text, (obj, c, code, out["error"])
    out, code, text = _call(stools, "list_records", {"object": "Interview__c", "fields": ["Candidate__c.Sample_Race_Ethnicity__c"],
                                                     "limit": 50})
    assert out["ok"] and "ZQ-RACE-1" in text, (code, out["error"])
    out, code, text = _call(stools, "group_records", {"object": "Account", "group_by": "Sample_Military_Status__c"})
    assert out["ok"] and "ZQ-MIL-1" in text, (code, out["error"])


def test_history_and_describe_show_personal_fields(stools):
    out, code, text = _call(stools, "get_field_history", {"object": "Account", "record": "November Marker"})
    assert out["ok"], (code, out["error"])
    assert {x["field"] for x in out["result"]["tracked_fields"]} == {"Name", "Sample_Race_Ethnicity__c", "PersonBirthdate"}
    assert "ZQ-OLDRACE-1" in text and "ZQ-RACE-1" in text
    d = _call(stools, "describe_fields", {"object": "Account"})[0]["result"]
    assert set(PERSONAL_FIXTURE["Account"]) <= {f["field"] for f in d["fields"]}
    assert not (d.get("not_queryable") or {}).get("sensitive") or \
        not set(d["not_queryable"]["sensitive"]) & set(PERSONAL_FIXTURE["Account"])


# ---------------------------------------------------------------- the same on the real snapshot (OKEVAL_RECORDS_LIVE=1)
SNAPSHOT = Path(os.environ.get("SFA_LIVE_SNAPSHOT") or DEFAULT_WAREHOUSE)   # opened read-only
live = pytest.mark.skipif(os.environ.get("OKEVAL_RECORDS_LIVE") != "1" or not SNAPSHOT.is_file()
                          or not REAL_COLUMNS.is_file(),
                          reason="live: set OKEVAL_RECORDS_LIVE=1 with .private/okeval_run/warehouse.duckdb "
                                 "(or SFA_LIVE_SNAPSHOT) present")


@live
def test_blocked_and_special_category_patterns_on_every_snapshot_column():
    """Column names only (information_schema, read-only): the old special-category pattern takes exactly
    REAL_SPECIAL_CATEGORY (main and raw), and BLOCKED_COLUMN_RE takes none of them."""
    import duckdb

    con = duckdb.connect(str(SNAPSHOT), read_only=True)
    try:
        rows = con.execute("SELECT table_schema, table_name, column_name FROM information_schema.columns").fetchall()
    finally:
        con.close()
    assert len(rows) > 30_000
    hits = {(s, t, c) for s, t, c in rows if SPECIAL_CATEGORY_RE.search(c)}
    want = {(s, t, c) for s in ("main", "raw") for t, cols in REAL_SPECIAL_CATEGORY.items() for c in cols}
    assert hits == want
    assert not [x for x in hits if BLOCKED_COLUMN_RE.search(x[2])]


@live
@pytest.mark.skipif(not any(KNOWLEDGE_DIR.glob("*.json")),
                    reason="the git-ignored knowledge files knowledge/objects/*.json are not there")
def test_record_tools_read_personal_fields_on_the_snapshot():
    """Every special-category field of a queryable object, directly and through every one-hop lookup path to it, is
    readable (phase 1). Only names and error codes are compared: a failure never shows record data."""
    import datetime as dt

    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    t = open_records(load_records_config(warehouse=str(SNAPSHOT), today=dt.date(2026, 10, 13), tz="America/New_York"),
                     str(KNOWLEDGE_DIR))
    try:
        cat = t.catalog
        queryable = {o.api for o in cat.queryable()}
        held = {o: [c for c in cols if c in cat.objects[o].fields] for o, cols in REAL_SPECIAL_CATEGORY.items()
                if o in queryable}
        assert set(held) == set(REAL_SPECIAL_CATEGORY)
        # the pattern blocks exactly the very-high columns of REAL_BLOCKED on the queryable objects (phase 1)
        by_pattern = {o.api: sorted(f.api for f in o.fields.values() if BLOCKED_COLUMN_RE.search(f.api))
                      for o in cat.queryable()}
        assert {o: c for o, c in by_pattern.items() if c} == {o: sorted(c) for o, c in REAL_BLOCKED.items()}
        calls: list[tuple[str, dict]] = []
        for obj, cols in held.items():
            for c in cols:
                assert cat.objects[obj].fields[c].sensitivity is None and c.lower() not in cat.blocked_columns
                calls += [("list_records", {"object": obj, "fields": [c]}),
                          ("count_records", {"object": obj, "filters": [{"field": c, "op": "is_not_blank"}]})]
        for o in cat.queryable():
            if o.sensitive:
                continue
            for g in o.fields.values():
                if g.kind == "reference" and g.sensitivity != "blocked":
                    for tgt in g.ref_targets:
                        for c in held.get(tgt, []):
                            calls.append(("list_records", {"object": o.api, "fields": [f"{g.api}.{c}"], "limit": 1}))
        assert len(calls) > 150
        failed = []
        for name, args in calls:
            t.begin_question()
            out = t.call(name, args)
            code = t.evidence[-1].error_code if t.evidence else None
            if not out["ok"] or code is not None:
                failed.append((name, args.get("object"), args.get("fields") or args.get("filters"), code))
        assert not failed
    finally:
        t.close()
