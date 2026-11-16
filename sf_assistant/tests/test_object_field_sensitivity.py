"""Field sensitivity of the Recruitment KB and Availability Calendar objects (2026-10-23), as phase 1 sets it (owner,
2026-10-26: the first users are admins; only the very-high fields stay blocked).

A knowledge file is what makes an object queryable by the record tools, so the new files put employee leaves and the
Zoom Recording Portal logs into reach. Since phase 1 the leave reasons and the leave document are readable like any
other field; ``FIELD_SENSITIVITY`` keeps the Zoom audit's event details (they hold hashed verification codes:
blocked as a credential) and the access log's details (contact data: shown only when the question asks for contact
details), and names three contact fields whose values are e-mail addresses or LinkedIn profile links (review F2). The
column patterns still block access keys, verification codes, integrity hashes and code signatures and treat IP,
user-agent, device, WhatsApp and contact-number fields as contact data. Synthetic warehouse and knowledge only: the values are
markers (ZQ-...) that no other fixture row holds.
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
from sf_assistant.engine.catalog import (BLOCKED_COLUMN_RE, CONTACT_COLUMN_RE, FIELD_SENSITIVITY,  # noqa: E402
                                         blocked_text)
from sf_assistant.engine.vocabulary import BLOCKED_RE  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")
LEAVE = [F.sid("a0X", i) for i in (1, 2, 3)]
LOG = [F.sid("a0Y", i) for i in (1, 2)]
JOB = F.sid("a0Z", 1)
AUDIT = F.sid("a0W", 1)
#: The leave values phase 1 shows again.
LEAVE_MARKERS = ("ZQ-LEAVE-REASON", "ZQ-CANCEL-REASON", "ZQ-REJECT-REASON", "ZQ-DOC-LINK")
#: The access-log values that are contact data.
LOG_MARKERS = ("ZQ-DETAILS", "ZQ-BROWSER", "ZQ-IP")
DAY = "2026-10-01T09:00:00.000Z"
NOT_ASKED = "How many portal events did each user have?"
ASKED = "Which IP addresses and browsers did the portal users log in from?"


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    tables = copy.deepcopy(F.TABLES)
    F._with(tables, "Employee_Leave__c", {
        "Id": None, "Name": None, "CreatedDate": F.TS, "Employee__c": None, "Leave_Type__c": None, "Status__c": None,
        "Start_Date__c": "DATE", "Leave_Reason__c": None, "Cancel_Reason__c": None, "Rejection_Reason__c": None,
        "Supporting_Document_Link__c": None}, [
        {"Id": LEAVE[0], "Name": "A-0001", "CreatedDate": DAY, "Employee__c": F.REC_A, "Leave_Type__c": "Sick",
         "Status__c": "Approved", "Start_Date__c": "2026-10-02", "Leave_Reason__c": "ZQ-LEAVE-REASON-1",
         "Supporting_Document_Link__c": "ZQ-DOC-LINK-1"},
        {"Id": LEAVE[1], "Name": "A-0002", "CreatedDate": DAY, "Employee__c": F.REC_A, "Leave_Type__c": "PTO",
         "Status__c": "Cancelled", "Start_Date__c": "2026-10-05", "Leave_Reason__c": "ZQ-LEAVE-REASON-2",
         "Cancel_Reason__c": "ZQ-CANCEL-REASON-1"},
        {"Id": LEAVE[2], "Name": "A-0003", "CreatedDate": DAY, "Employee__c": F.REC_C, "Leave_Type__c": "PTO",
         "Status__c": "Rejected", "Start_Date__c": "2026-10-07", "Leave_Reason__c": "ZQ-LEAVE-REASON-3",
         "Rejection_Reason__c": "ZQ-REJECT-REASON-1"}])
    F._with(tables, "Zoom_Recording_Access_Log__c", {
        "Id": None, "Name": None, "CreatedDate": F.TS, "User__c": None, "Event_Type__c": None, "Error_Message__c": None,
        "User_Agent__c": None, "IP_Address__c": None}, [
        {"Id": LOG[0], "Name": "LOG-0000001", "CreatedDate": DAY, "User__c": F.USER_1, "Event_Type__c": "Login_Success",
         "Error_Message__c": "ZQ-DETAILS-1", "User_Agent__c": "ZQ-BROWSER-1", "IP_Address__c": "ZQ-IP-1"},
        {"Id": LOG[1], "Name": "LOG-0000002", "CreatedDate": DAY, "User__c": F.USER_1, "Event_Type__c": "Portal_Access",
         "Error_Message__c": "ZQ-DETAILS-2"}])
    F._with(tables, "Zoom_Portal_Access_Audit__c", {
        "Id": None, "Name": None, "CreatedDate": F.TS, "Event_Type__c": None, "Event_Details__c": None}, [
        {"Id": AUDIT, "Name": "AUD-0000001", "CreatedDate": DAY, "Event_Type__c": "Code_Verified",
         "Event_Details__c": "ZQ-AUDIT-HASH-1"}])
    F._with(tables, "Job_Submission__c", {"Id": None, "Name": None, "CreatedDate": F.TS, "Rejection_Reason__c": None}, [
        {"Id": JOB, "Name": "JS-0001", "CreatedDate": DAY, "Rejection_Reason__c": "ZQ-JOB-REJECT-1"}])
    knowledge = copy.deepcopy(F.KNOWLEDGE)
    knowledge.append(F._obj("Employee_Leave__c", "Employee Leave", "Employee Leaves", "One record is one leave.", [
        F._field("Name", "Leave Number", "auto_number", "Leave number."),
        F._field("Employee__c", "Employee", "reference", "Employee on leave.", targets=["Recruiter__c"],
                 role="employee_on_leave"),
        F._field("Leave_Type__c", "Leave Type", "picklist", "Kind of leave.", values=["Sick", "PTO"]),
        F._field("Status__c", "Status", "picklist", "Status of the leave.", values=["Approved", "Cancelled", "Rejected"]),
        F._field("Start_Date__c", "Start Date", "date", "First day of the leave."),
        F._field("Leave_Reason__c", "Leave Reason", "long_text_area", "Reason typed by the applicant."),
        F._field("Cancel_Reason__c", "Cancel Reason", "text_area", "Reason typed at cancel."),
        F._field("Rejection_Reason__c", "Rejection Reason", "text_area", "Reason the manager gave."),
        F._field("Supporting_Document_Link__c", "Supporting Document Link", "url", "Link to a supporting document.")],
        relationships=[F._rel("Employee__c", ["Recruiter__c"], "leaves of this employee")]))
    knowledge.append(F._obj("Zoom_Recording_Access_Log__c", "Zoom Recording Access Log", "Zoom Recording Access Logs",
                            "One record is one logged portal event.", [
        F._field("Name", "Log Number", "auto_number", "Log number."),
        F._field("User__c", "User", "reference", "User who did the action.", targets=["User"], role="acting_user"),
        F._field("Event_Type__c", "Event Type", "picklist", "Kind of portal event.",
                 values=["Login_Success", "Portal_Access"]),
        F._field("Error_Message__c", "Error Message", "text_area", "Event details despite the label."),
        F._field("User_Agent__c", "User Agent", "text", "Browser of the last login."),
        F._field("IP_Address__c", "IP Address", "text", "IP address of the last login.")],
        relationships=[F._rel("User__c", ["User"], "portal events of this user")]))
    knowledge.append(F._obj("Zoom_Portal_Access_Audit__c", "Zoom Portal Access Audit", "Zoom Portal Access Audits",
                            "One record is one audited portal action.", [
        F._field("Name", "Audit Number", "auto_number", "Audit number."),
        F._field("Event_Type__c", "Event Type", "picklist", "What happened.", values=["Code_Verified"]),
        F._field("Event_Details__c", "Event Details", "long_text_area", "Details, with hashed verification codes.")]))
    knowledge.append(F._obj("Job_Submission__c", "Job Submission", "Job Submissions", "One record is one submission.", [
        F._field("Name", "Submission Number", "auto_number", "Submission number."),
        F._field("Rejection_Reason__c", "Rejection Reason", "text_area", "Why the client rejected the submission.")]))
    root = tmp_path_factory.mktemp("field_sensitivity")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = open_records(load_records_config(warehouse=str(F.make_warehouse(root, tables=tables)),
                                             today=dt.date(2026, 10, 13)),
                         str(F.make_knowledge(root, knowledge=knowledge)))
    yield t
    t.close()


def _call(t, name, args, question: str = NOT_ASKED) -> tuple[dict, str | None, str]:
    t.begin_question(question)
    out = t.call(name, args)
    return out, (t.evidence[-1].error_code if t.evidence else None), json.dumps(out)


def test_credential_and_contact_patterns_of_2026_10_23():
    for c in ("AWS_Access_Key__c", "AWS_Secret_Key__c", "Verification_Code__c", "Integrity_Hash__c", "Signature__c"):
        assert BLOCKED_COLUMN_RE.search(c) and BLOCKED_RE.search(c), c
    for c in ("dfsle__HasEnabledSendForSignature__c", "Signature", "StayInTouchSignature", "Question_Hash__c",
              "Department_Code__c", "Code_Attempts__c", "Code_Expires_At__c"):
        assert not BLOCKED_COLUMN_RE.search(c), c
    for c in ("User_Agent__c", "IP_Address__c", "Verified_IP__c", "Target_IP__c", "Performed_By_IP__c", "Actor_IP__c",
              "Verified_Device__c", "Performed_By_Device__c", "Target_Device__c", "WhatsApp_Number__c",
              "Whatsapp_No__c", "Contact_Number_WhatsApp__c", "Emergency_Contact_Number__c", "ContactNumber__c"):
        assert CONTACT_COLUMN_RE.search(c) and BLOCKED_RE.search(c), c
    for c in ("Ship_To__c", "VIP_Level__c", "Relationship__c", "Error_Message__c", "Assignment_Error_Message__c",
              "DeviceType", "Rejection_Reason__c", "Leave_Reason__c", "Cancel_Reason__c", "Supporting_Document_Link__c",
              "Contact__c", "Point_of_Contact__c", "Contact_Type__c", "Number_of_Contacts__c", "EmailEncodingKey"):
        assert not CONTACT_COLUMN_RE.search(c) and not BLOCKED_COLUMN_RE.search(c), c


def test_phase1_object_scoped_entries():
    assert FIELD_SENSITIVITY == {
        ("Zoom_Portal_Access_Audit__c", "Event_Details__c"): ("blocked", "credential, passcode or identity document"),
        ("Zoom_Recording_Access_Log__c", "Error_Message__c"): ("contact", ""),
        ("User", "Username"): ("contact", ""), ("Onboarding__c", "Professional_Profiles__c"): ("contact", ""),
        ("Job_Submission__c", "Unique_Submission_Key__c"): ("contact", "")}


def test_object_scoped_sensitivity_in_the_catalog(tools):
    cat = tools.catalog
    leave = cat.objects["Employee_Leave__c"].fields
    for c in ("Leave_Reason__c", "Cancel_Reason__c", "Rejection_Reason__c", "Supporting_Document_Link__c",
              "Leave_Type__c", "Start_Date__c"):
        assert leave[c].sensitivity is None and ("employee_leave__c", c.lower()) not in cat.blocked_fields, c
        assert leave[c].filled, c   # counted at open again
    audit = cat.objects["Zoom_Portal_Access_Audit__c"].fields
    assert audit["Event_Details__c"].sensitivity == "blocked" and audit["Event_Type__c"].sensitivity is None
    assert ("zoom_portal_access_audit__c", "event_details__c") in cat.blocked_fields
    assert "event_details__c" not in cat.blocked_columns          # object-scoped: never blocked in another table
    log = cat.objects["Zoom_Recording_Access_Log__c"].fields
    assert log["Error_Message__c"].sensitivity == "contact" and log["User_Agent__c"].sensitivity == "contact"
    assert log["IP_Address__c"].sensitivity == "contact" and log["Event_Type__c"].sensitivity is None
    assert cat.objects["Job_Submission__c"].fields["Rejection_Reason__c"].sensitivity is None
    with pytest.raises(Exception):
        tools.warehouse.run('SELECT t0."Event_Details__c" FROM main."Zoom_Portal_Access_Audit__c" AS t0', [],
                            max_rows=1, timeout_s=10)


def test_blocked_text_of_the_audit_details():
    assert blocked_text("Zoom_Portal_Access_Audit__c", "Event_Details__c").startswith(
        "Zoom_Portal_Access_Audit__c.Event_Details__c is a sensitive field (credential, passcode or identity "
        "document); it cannot be read")
    assert "(credential, passcode or identity document)" in blocked_text("Job_Submission__c", "Rejection_Reason__c")


@pytest.mark.parametrize("name,args,marker", [
    ("list_records", {"object": "Employee_Leave__c", "fields": ["Employee__c", "Leave_Reason__c"]}, "ZQ-LEAVE-REASON-1"),
    ("count_records", {"object": "Employee_Leave__c", "filters": [{"field": "Cancel_Reason__c", "op": "is_not_blank"}]},
     None),
    ("group_records", {"object": "Employee_Leave__c", "group_by": "Rejection_Reason__c"}, "ZQ-REJECT-REASON-1"),
    ("get_field_values", {"object": "Employee_Leave__c", "field": "Supporting_Document_Link__c"}, "ZQ-DOC-LINK-1"),
    ("list_records", {"object": "Employee_Leave__c", "fields": ["Name", "Cancel_Reason__c"],
                      "filters": [{"field": "Leave_Reason__c", "op": "contains", "value": "ZQ"}]}, "ZQ-CANCEL-REASON-1"),
])
def test_record_tools_read_the_leave_reasons(tools, name, args, marker):
    for question in (NOT_ASKED, ""):     # no contact field: the same with or without a question
        out, code, text = _call(tools, name, args, question)
        assert out["ok"], (name, code, out["error"])
        assert marker is None or marker in text, name
    if name == "count_records":
        assert out["result"]["count"] == 1


def test_every_leave_value_is_readable(tools):
    out, code, text = _call(tools, "list_records", {"object": "Employee_Leave__c", "fields": [
        "Leave_Type__c", "Leave_Reason__c", "Cancel_Reason__c", "Rejection_Reason__c", "Supporting_Document_Link__c"]})
    assert out["ok"], out["error"]
    assert all(m in text for m in LEAVE_MARKERS)
    assert not [n for n in out["result"].get("notes") or [] if "left out" in n]   # a link is no contact detail


def test_the_audit_details_stay_blocked_even_when_contact_details_are_asked(tools):
    for question in (NOT_ASKED, ASKED):
        for name, args in (("list_records", {"object": "Zoom_Portal_Access_Audit__c", "fields": ["Event_Details__c"]}),
                           ("count_records", {"object": "Zoom_Portal_Access_Audit__c",
                                              "filters": [{"field": "Event_Details__c", "op": "is_not_blank"}]}),
                           ("get_field_values", {"object": "Zoom_Portal_Access_Audit__c", "field": "Event_Details__c"})):
            out, code, text = _call(tools, name, args, question)
            assert out["ok"] is False and code == "E2c", (name, question)
            assert "ZQ-AUDIT-HASH" not in text


def test_a_look_alike_on_another_object_stays_readable(tools):
    out, code, text = _call(tools, "list_records", {"object": "Job_Submission__c", "fields": ["Rejection_Reason__c"]})
    assert out["ok"], out["error"]
    assert "ZQ-JOB-REJECT-1" in text


def test_access_log_details_are_contact_data(tools):
    gated = (("group_records", {"object": "Zoom_Recording_Access_Log__c", "group_by": "Error_Message__c"}),
             ("group_records", {"object": "Zoom_Recording_Access_Log__c", "group_by": "User_Agent__c"}),
             ("get_field_values", {"object": "Zoom_Recording_Access_Log__c", "field": "IP_Address__c"}))
    for name, args in gated:                       # not asked: refused, and the refusal never says it was asked
        out, code, text = _call(tools, name, args)
        assert out["ok"] is False and code == "E2d", (name, args)
        assert "does not ask for contact details" in out["error"] and "asked for them" not in out["error"]
        assert not [m for m in LOG_MARKERS if m in text]
    for name, args in gated:                       # asked: shown
        out, code, text = _call(tools, name, args, ASKED)
        assert out["ok"], (name, code, out["error"])
        assert [m for m in LOG_MARKERS if m in text], name
    out, code, text = _call(tools, "list_records", {"object": "Zoom_Recording_Access_Log__c",
                                                    "fields": ["User__c", "Event_Type__c"]})
    assert out["ok"], out["error"]
    assert not [m for m in LOG_MARKERS if m in text]
