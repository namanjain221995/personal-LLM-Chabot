"""A copy of the people fixture with the shapes of the Group B people fixes (docs/people.md; no real record data).

``GROUPB_TABLES`` / ``GROUPB_KNOWLEDGE`` extend ``warehouse_fixture.PEOPLE_TABLES`` / ``PEOPLE_KNOWLEDGE`` (which stay
as they are: the agent and resolver tests read them) with:

- two Users with the same name ("Hotel Ivory", one inactive) that are linked to nobody; the inactive one owns a Lead;
- a user who owns candidate accounts and is the candidate owner on trainings of two candidates (distinct counts, the
  owner role on another object);
- a candidate whose converted Lead holds an intake interview, while the candidate's Account holds more references (the
  per-person reference share), and a mock with a blank interviewer that has an Interviewer attendee row;
- sessions of a candidate reached through the candidate's trainings, a training step and an attendee row (parent
  paths), next to the session's own candidate lookup;
- a candidate's assigned recruiter that copies the Marketing recruiter on only one of three candidates (a partial copy
  from a child object);
- a candidate with three trainings and three trainers (the other people in a link);
- a reporting line with its direction texts (from_source, from_target);
- review fixes (2026-10-21): a candidate-owner training row that points to no candidate (a related row with a blank
  lookup to the base), two candidates with the same name ("November Oak"), one with a session through its training
  and one with a direct session, and a second training of a trainer with fewer rows than the candidate.

All names are synthetic and hold no digits (the hint tests check that no digits appear).
"""

from __future__ import annotations

import copy
from pathlib import Path

import warehouse_fixture as F
from warehouse_fixture import _field, _obj, _with, sid

# Ids of the added records
USER_HI1, USER_HI2, USER_IJ = sid("005", 40), sid("005", 41), sid("005", 42)
LEAD_QL = sid("00Q", 40)
REC_GH, REC_MN = sid("a06", 40), sid("a06", 41)
CT_J1, CT_J2, CT_G, CT_M = sid("a0C", 40), sid("a0C", 41), sid("a0C", 42), sid("a0C", 43)
INT_J1, INT_J2 = sid("a0A", 40), sid("a0A", 41)
II_LEAD, II_BLANK = sid("a0B", 40), sid("a0B", 41)
MK = [sid("a0N", i) for i in range(1, 6)]
STEP_1 = sid("a0P", 1)
SES_DIRECT, SES_STEP = sid("a0D", 40), sid("a0D", 41)
ATT_CAND = sid("a0L", 40)
IA_1, IA_2 = sid("a0Q", 1), sid("a0Q", 2)
CT_BLANK, CT_N1, CT_G2 = sid("a0C", 44), sid("a0C", 45), sid("a0C", 46)
CAND_N1, CAND_N2 = sid("001", 40), sid("001", 41)
SES_N1, SES_N2 = sid("a0D", 42), sid("a0D", 43)

#: The people of the added records, by role in the tests.
GROUPB = {"same_name_users": "Hotel Ivory", "owner_user": "India Juno", "owned_lead": "Quebec Lark",
          "trainer_g": "Golf Harbor", "trainer_m": "Mike Nectar"}

#: Facts the tests assert (computed from the rows below).
GROUPB_FACTS = {
    "owner_trainings": 4, "owner_training_candidates": 2,        # Candidate_Training__c.Candidate_Owner__c = India Juno
    "owner_trainings_of_accounts": 3,                             # ... on rows that point to an Account (one is blank)
    "owner_accounts": 2,                                          # Account.OwnerId = India Juno
    "cand1_sessions_direct": 2, "cand1_sessions_training": 3, "cand1_sessions_step": 1, "cand1_sessions_attendee": 1,
    "cand1_sessions_any": 6,
    "kz_copy": 1, "kz_source_candidates": 3,                     # Account.Assigned_Recruiter__c vs Marketing__c
}

DAY = "2025-01-01T00:00:00.000Z"
TS = F.TS


def _groupb_tables() -> list:
    t = copy.deepcopy(F.PEOPLE_TABLES)
    _with(t, "User", {}, [
        {"Id": USER_HI1, "Name": "Hotel Ivory", "IsActive": "true", "UserType": "Standard"},
        {"Id": USER_HI2, "Name": "Hotel Ivory", "IsActive": "false", "UserType": "Standard"},
        {"Id": USER_IJ, "Name": "India Juno", "IsActive": "true", "UserType": "Standard"}])
    _with(t, "Lead", {}, [
        {"Id": LEAD_QL, "Name": "Quebec Lark", "CreatedDate": DAY, "Status": "Open", "IsConverted": "false",
         "OwnerId": USER_HI2}])
    _with(t, "Recruiter__c", {}, [
        {"Id": REC_GH, "Name": "Golf Harbor", "CreatedDate": DAY, "Employment_Status__c": "Active Employee"},
        {"Id": REC_MN, "Name": "Mike Nectar", "CreatedDate": DAY, "Employment_Status__c": "Active Employee"}])
    # Account.OwnerId: India Juno owns Candidate 1 and Kilo Quill
    _with(t, "Account", {"OwnerId": None}, [])
    _set(t, "Account", "OwnerId", {F.CAND_1: USER_IJ, F.CAND_KQ: USER_IJ, F.CAND_2: F.USER_1})
    # Candidate_Training__c.Candidate_Owner__c: India Juno on 3 trainings of Candidate 1 (2) and Candidate 2 (1)
    _with(t, "Candidate_Training__c", {"Candidate_Owner__c": None}, [
        {"Id": CT_J1, "Name": "CT-0040", "CreatedDate": DAY, "Status__c": "Active", "Candidate__c": F.CAND_CONV},
        {"Id": CT_J2, "Name": "CT-0041", "CreatedDate": DAY, "Status__c": "Planned", "Candidate__c": F.CAND_CONV},
        {"Id": CT_G, "Name": "CT-0042", "CreatedDate": DAY, "Status__c": "Completed", "Candidate__c": F.CAND_KQ,
         "Assigned_Trainer__c": REC_GH},
        {"Id": CT_M, "Name": "CT-0043", "CreatedDate": DAY, "Status__c": "Planned", "Candidate__c": F.CAND_KQ,
         "Assigned_Trainer__c": REC_MN}])
    _set(t, "Candidate_Training__c", "Candidate_Owner__c", {F.CT[0]: USER_IJ, F.CT[1]: USER_IJ, F.CT[2]: USER_IJ})
    # review fixes: an owner row of no candidate; the training of one of two same-name candidates; a second training of
    # Golf Harbor (2 rows, fewer than Kilo Quill's 3)
    _with(t, "Candidate_Training__c", {}, [
        {"Id": CT_BLANK, "Name": "CT-0044", "CreatedDate": DAY, "Status__c": "Planned", "Candidate_Owner__c": USER_IJ},
        {"Id": CT_N1, "Name": "CT-0045", "CreatedDate": DAY, "Status__c": "Active", "Candidate__c": CAND_N1},
        {"Id": CT_G2, "Name": "CT-0046", "CreatedDate": DAY, "Status__c": "Active", "Candidate__c": F.CAND_2,
         "Assigned_Trainer__c": REC_GH}])
    _with(t, "Account", {}, [
        {"Id": CAND_N1, "Name": "November Oak", "RecordTypeId": F.RT_PERSON, "CreatedDate": DAY, "IsPersonAccount": "true"},
        {"Id": CAND_N2, "Name": "November Oak", "RecordTypeId": F.RT_PERSON, "CreatedDate": DAY,
         "IsPersonAccount": "true"}])
    # client interviews of the converted candidate (2) and the intake mock of its Lead (1)
    _with(t, "Interview__c", {}, [
        {"Id": INT_J1, "Name": "I-0040", "IsDeleted": "false", "CreatedDate": DAY, "Interview_Status__c": "Completed",
         "Recruiter__c": F.REC_A, "Candidate__c": F.CAND_CONV},
        {"Id": INT_J2, "Name": "I-0041", "IsDeleted": "false", "CreatedDate": DAY, "Interview_Status__c": "Scheduled",
         "Recruiter__c": F.REC_A, "Candidate__c": F.CAND_CONV}])
    _with(t, "Internal_Interview__c", {"Lead__c": None}, [
        {"Id": II_LEAD, "Name": "INT-0040", "CreatedDate": DAY, "Status__c": "Completed", "Interview_Type__c": "Intake",
         "Interviewer__c": F.REC_D, "Lead__c": F.LEAD_CONV},
        {"Id": II_BLANK, "Name": "INT-0041", "CreatedDate": DAY, "Status__c": "Scheduled", "Interview_Type__c": "Mock",
         "Candidate__c": F.CAND_CONV}])
    _with(t, "Interview_Attendee__c", {"Id": None, "Name": None, "CreatedDate": TS, "Internal_Interview__c": None,
                                       "Attendee_Recruiter__c": None, "Role__c": None}, [
        {"Id": IA_1, "Name": "IA-0001", "CreatedDate": DAY, "Internal_Interview__c": II_BLANK,
         "Attendee_Recruiter__c": F.REC_E1, "Role__c": "Interviewer"},
        {"Id": IA_2, "Name": "IA-0002", "CreatedDate": DAY, "Internal_Interview__c": II_BLANK,
         "Attendee_Recruiter__c": F.REC_C, "Role__c": "Observer"}])
    # Marketing__c: Kilo Zephyr markets Kilo Quill, Candidate 1 and Candidate 2; Recruiter C markets Juliet Converse
    _with(t, "Marketing__c", {"Id": None, "Name": None, "CreatedDate": TS, "Candidate__c": None, "Recruiter__c": None}, [
        {"Id": MK[0], "Name": "MK-0001", "CreatedDate": DAY, "Candidate__c": F.CAND_KQ, "Recruiter__c": F.REC_KZ},
        {"Id": MK[1], "Name": "MK-0002", "CreatedDate": DAY, "Candidate__c": F.CAND_1, "Recruiter__c": F.REC_KZ},
        {"Id": MK[2], "Name": "MK-0003", "CreatedDate": DAY, "Candidate__c": F.CAND_2, "Recruiter__c": F.REC_KZ},
        {"Id": MK[3], "Name": "MK-0004", "CreatedDate": DAY, "Candidate__c": F.CAND_CONV, "Recruiter__c": F.REC_C},
        {"Id": MK[4], "Name": "MK-0005", "CreatedDate": DAY, "Candidate__c": F.CAND_CONV, "Recruiter__c": F.REC_C}])
    # sessions of Candidate 1: its own lookup (SES-0001, SES-0040), its trainings (SES-0001..3), a step of a training
    # (SES-0041) and an attendee row (SES-0004)
    _with(t, "Training_Step__c", {"Id": None, "Name": None, "CreatedDate": TS, "Candidate_Training__c": None}, [
        {"Id": STEP_1, "Name": "STEP-0001", "CreatedDate": DAY, "Candidate_Training__c": F.CT[0]}])
    _with(t, "Session__c", {"Candidate__c": None, "Training_Step__c": None}, [
        {"Id": SES_DIRECT, "Name": "SES-0040", "CreatedDate": DAY, "Candidate__c": F.CAND_1, "Purpose__c": "Mock",
         "Session_Date__c": "2026-09-21"},
        {"Id": SES_STEP, "Name": "SES-0041", "CreatedDate": DAY, "Training_Step__c": STEP_1, "Purpose__c": "Training",
         "Session_Date__c": "2026-09-22"}])
    _set(t, "Session__c", "Candidate__c", {sid("a0D", 1): F.CAND_1})
    _with(t, "Session__c", {}, [
        {"Id": SES_N1, "Name": "SES-0042", "CreatedDate": DAY, "Candidate_Training__c": CT_N1, "Purpose__c": "Training",
         "Session_Date__c": "2026-09-23"},
        {"Id": SES_N2, "Name": "SES-0043", "CreatedDate": DAY, "Candidate__c": CAND_N2, "Purpose__c": "Mock",
         "Session_Date__c": "2026-09-24"}])
    _with(t, "Session_Attendee__c", {"Candidate__c": None, "Role__c": None}, [
        {"Id": ATT_CAND, "Name": "SA-0040", "CreatedDate": DAY, "Session__c": sid("a0D", 4), "Candidate__c": F.CAND_1,
         "Role__c": "Candidate"}])
    _set(t, "Session_Attendee__c", "Role__c", {F.ATT_P: "Employee"})
    return t


def _set(tables: list, name: str, col: str, by_id: dict) -> None:
    """Set ``col`` of the rows of table ``name`` whose Id is a key of ``by_id``."""
    for i, (t, cols, rows) in enumerate(tables):
        if t != name:
            continue
        keys = list(cols)
        j = keys.index(col)
        tables[i] = (t, cols, [tuple(by_id.get(r[0], v) if k == j and r[0] in by_id else v for k, v in enumerate(r))
                               for r in rows])
        return


def _groupb_knowledge() -> list:
    k = copy.deepcopy(F.PEOPLE_KNOWLEDGE)
    by = {x["object"]["api_name"]: x for x in k}
    rec = by["Recruiter__c"]
    rec["relationships"] = list(rec.get("relationships") or []) + [
        {"field": "Assigned_Sr_Recruiter__c", "type": "lookup", "target_objects": ["Recruiter__c"],
         "relationship_name": "Juniors", "business_meaning": "Reporting line.", "cardinality": "many_to_one",
         "direction_semantics": {"from_source": "senior recruiter this employee reports to",
                                 "from_target": "employees reporting to this senior recruiter"}}]
    acc = by["Account"]
    acc["field_dependencies"] = [
        {"field": "Assigned_Recruiter__c", "depends_on": ["Marketing__c.Recruiter__c", "Marketing__c.Candidate__c"],
         "kind": "flow", "meaning": "A flow copies the Marketing record's recruiter to its candidate (no back-fill)."}]
    by["Candidate_Training__c"]["fields"] += [
        _field("Candidate_Owner__c", "Candidate Owner", "reference", "User who owned the candidate at enrolment.",
               targets=["User"], role="candidate_owner")]
    by["Internal_Interview__c"]["fields"] += [
        _field("Lead__c", "Lead", "reference", "Lead interviewed in an intake interview.", targets=["Lead"],
               role="lead_candidate")]
    by["Session__c"]["fields"] += [
        _field("Candidate__c", "Candidate", "reference", "Candidate of the session (legacy; the training is the path).",
               targets=["Account"], role="candidate", legacy=True),
        _field("Training_Step__c", "Training Step", "reference", "Training step the session delivers.",
               targets=["Training_Step__c"], role="training_step")]
    by["Session_Attendee__c"]["fields"] += [
        _field("Candidate__c", "Candidate", "reference", "Candidate on the attendee row.", targets=["Account"],
               role="candidate"),
        _field("Role__c", "Role", "picklist", "Role of the person on the attendee row.",
               values=["Host", "Candidate", "Employee"], roles=["categorical_attribute"])]
    k.append(_obj("Interview_Attendee__c", "Interview Attendee", "Interview Attendees", "One record is one attendee row "
                  "of an internal interview.", [
                      _field("Internal_Interview__c", "Internal Interview", "reference", "The interview.",
                             targets=["Internal_Interview__c"], role="parent_interview"),
                      _field("Attendee_Recruiter__c", "Attendee Recruiter", "reference", "Employee on the row.",
                             targets=["Recruiter__c"], role="invited_employee"),
                      _field("Role__c", "Role", "picklist", "Role of the attendee.",
                             values=["Interviewer", "Observer"], roles=["categorical_attribute"])]))
    k.append(_obj("Marketing__c", "Marketing", "Marketing", "One record is one marketing assignment of a candidate.", [
        _field("Candidate__c", "Candidate", "reference", "Candidate being marketed.", targets=["Account"],
               role="candidate"),
        _field("Recruiter__c", "Recruiter", "reference", "Marketing recruiter of the candidate.",
               targets=["Recruiter__c"], role="marketing_recruiter")]))
    k.append(_obj("Training_Step__c", "Training Step", "Training Steps", "One record is one step of a training.", [
        _field("Candidate_Training__c", "Candidate Training", "reference", "Training of the step.",
               targets=["Candidate_Training__c"], role="candidate_training")]))
    return k


GROUPB_TABLES = _groupb_tables()
GROUPB_KNOWLEDGE = _groupb_knowledge()


def open_groupb_fixture(tmp_path: Path, **overrides):
    """``open_records`` over GROUPB_TABLES and GROUPB_KNOWLEDGE (today pinned to 2026-10-13)."""
    import datetime as dt

    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    wh = F.make_warehouse(tmp_path, tables=GROUPB_TABLES)
    kn = F.make_knowledge(tmp_path, knowledge=GROUPB_KNOWLEDGE)
    opts = {"warehouse": str(wh), "today": dt.date(2026, 10, 13)}
    opts.update(overrides)
    return open_records(load_records_config(**opts), str(kn))
