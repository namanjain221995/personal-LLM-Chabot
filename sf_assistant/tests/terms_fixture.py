"""A copy of the people fixture with the shapes of the business-term contracts (2026-11-24; no real record data).

``TERMS_TABLES`` / ``TERMS_KNOWLEDGE`` extend ``warehouse_fixture.PEOPLE_TABLES`` / ``PEOPLE_KNOWLEDGE`` (which stay as
they are) with:

- an Internal_Employee_Account record type and one such account (IsPersonAccount false), next to the people fixture's
  Recruiter accounts (IsPersonAccount true: the employees' own person accounts) and its B2B client (false);
- ``Account.Candidate_Status__c`` with 'Placed' on two candidates (not a placement);
- a client interview whose Candidate__c is a Recruiter-type account (not a candidate);
- ``Interview__c.Interview_Support_Person__c`` on 6 interviews of 2 support people with known outcomes, the preferred
  support person, a Lead lookup and the B2B client account (a lookup to Account that is never the candidate);
- ``Session__c.Session_Role__c`` ('Support' twice: the other reading of support) and an additional attendee lookup;
- ``Onboarding__c`` (completed onboardings of candidates) and a small ``Contact`` table;
- three ``Interview_Participant__c`` rows (a lookup to Interview__c) created on another day than their interview's
  date: the placement date rule through a lookup path.

``TERMS_FACTS`` holds the counts the tests assert (computed from the rows). All names are synthetic and hold no digits.
"""

from __future__ import annotations

import copy
from pathlib import Path

import warehouse_fixture as F
from warehouse_fixture import _field, _obj, _with, sid

RT_INTERNAL = sid("012", 60)
ACC_INT = sid("001", 60)
INT_RT = sid("a0A", 60)            # a client interview of a Recruiter-type account
ONB = [sid("a0R", i) for i in (1, 2, 3)]
CON_1 = sid("003", 1)
PART = [sid("a0J", i) for i in (1, 2, 3)]   # Interview_Participant__c

#: The two support people (Recruiter__c records of the base fixture) and their interviews.
SUPPORT = {"Recruiter C": ["I-0001", "I-0002"], "Delta  Person": ["I-0004", "I-0005", "I-0008", "I-0010"]}

TERMS_FACTS = {
    "accounts": 9, "person_record_type": 5, "flag_true": 7,
    "interviews": 13, "interview_candidates_all": 3, "interview_candidates_person": 2,
    "interviews_of_person_candidates": 9,   # Candidate__c on a Person Account (the blank and Recruiter ones drop out)
    # support person -> (interviews, offers, offer rate %); the blank group holds the other 7 interviews (no offer)
    "support": {"Recruiter C": (2, 1, 50.0), "Delta  Person": (4, 1, 25.0), "(blank)": (7, 0, 0.0)},
    "offer_rate_overall": 15.4,             # 2 offers of 13 interviews
    "placed_status_accounts": 2,
    "support_role_sessions": 2,
}


def _terms_tables() -> list:
    t = copy.deepcopy(F.PEOPLE_TABLES)
    day = "2025-01-01T00:00:00.000Z"
    _with(t, "RecordType", {}, [{"Id": RT_INTERNAL, "Name": "Internal Employee Account",
                                 "DeveloperName": "Internal_Employee_Account", "SobjectType": "Account",
                                 "IsActive": "true"}])
    _with(t, "Account", {"Candidate_Status__c": None}, [
        {"Id": ACC_INT, "Name": "Internal Desk", "RecordTypeId": RT_INTERNAL, "CreatedDate": day,
         "IsPersonAccount": "false"}])
    _set(t, "Account", "Candidate_Status__c", {F.CAND_1: "Placed", F.CAND_KQ: "Placed", F.CAND_2: "Active"})
    _with(t, "Interview__c", {"Interview_Support_Person__c": None, "Preferred_Support_Person__c": None,
                              "Lead__c": None, "Client_Account__c": None}, [
        {"Id": INT_RT, "Name": "I-0060", "IsDeleted": "false", "CreatedDate": day, "Interview_Status__c": "Completed",
         "Recruiter__c": F.REC_A, "Candidate__c": F.ACC_A, "Date_of_Interview__c": "2026-09-30"}])
    by_name = {f"I-{i:04d}": sid("a0A", i) for i in range(1, 11)}
    support = {by_name[n]: rec for rec, names in ((F.REC_C, SUPPORT["Recruiter C"]),
                                                  (F.REC_D, SUPPORT["Delta  Person"])) for n in names}
    _set(t, "Interview__c", "Interview_Support_Person__c", support)
    _set(t, "Interview__c", "Preferred_Support_Person__c", {by_name["I-0001"]: F.REC_E1, by_name["I-0003"]: F.REC_E1})
    _set(t, "Interview__c", "Lead__c", {by_name["I-0002"]: F.LEAD_KV})
    _set(t, "Interview__c", "Client_Account__c", {by_name["I-0001"]: F.CLIENT, by_name["I-0002"]: F.CLIENT})
    _with(t, "Session__c", {"Session_Role__c": None, "Additional_Attendee_1__c": None}, [])
    _set(t, "Session__c", "Session_Role__c", {sid("a0D", 1): "Support", sid("a0D", 2): "Support",
                                              sid("a0D", 3): "Interviewer"})
    _set(t, "Session__c", "Additional_Attendee_1__c", {sid("a0D", 1): F.REC_D})
    _with(t, "Onboarding__c", {"Id": None, "Name": None, "CreatedDate": F.TS, "Account__c": None, "Status__c": None}, [
        {"Id": ONB[0], "Name": "ONB-0001", "CreatedDate": day, "Account__c": F.CAND_1, "Status__c": "Completed"},
        {"Id": ONB[1], "Name": "ONB-0002", "CreatedDate": day, "Account__c": F.CAND_KQ, "Status__c": "Completed"},
        {"Id": ONB[2], "Name": "ONB-0003", "CreatedDate": day, "Account__c": F.CAND_2, "Status__c": "Started"}])
    _with(t, "Interview_Participant__c", {"CreatedDate": F.TS}, [   # created: Aug, Aug, Sep; interviews: Sep, Aug, Sep
        {"Id": PART[0], "Name": "IP-0001", "Interview__c": by_name["I-0001"], "CreatedDate": "2026-08-20T12:00:00.000Z"},
        {"Id": PART[1], "Name": "IP-0002", "Interview__c": by_name["I-0005"], "CreatedDate": "2026-08-12T12:00:00.000Z"},
        {"Id": PART[2], "Name": "IP-0003", "Interview__c": by_name["I-0002"], "CreatedDate": "2026-09-02T12:00:00.000Z"}])
    _with(t, "Contact", {"Id": None, "Name": None, "CreatedDate": F.TS, "AccountId": None}, [
        {"Id": CON_1, "Name": "Candidate 1", "CreatedDate": day, "AccountId": F.CAND_1}])
    return t


def _set(tables: list, name: str, col: str, by_id: dict) -> None:
    """Set ``col`` of the rows of table ``name`` whose Id is a key of ``by_id``."""
    for i, (t, cols, rows) in enumerate(tables):
        if t != name:
            continue
        j = list(cols).index(col)
        tables[i] = (t, cols, [tuple(by_id[r[0]] if k == j and r[0] in by_id else v for k, v in enumerate(r))
                               for r in rows])
        return


def _terms_knowledge() -> list:
    k = copy.deepcopy(F.PEOPLE_KNOWLEDGE)
    by = {x["object"]["api_name"]: x for x in k}
    by["Account"]["fields"] += [
        _field("Candidate_Status__c", "Candidate Status", "picklist", "Where the candidate stands.",
               values=["Active", "Placed", "On Hold"], roles=["business_status"])]
    by["Interview__c"]["fields"] += [
        _field("Interview_Support_Person__c", "Interview Support Person", "reference",
               "Employee who supports the candidate in the client interview.", targets=["Recruiter__c"],
               role="interview_support_person"),
        _field("Preferred_Support_Person__c", "Preferred Support Person", "reference",
               "Support employee the candidate prefers.", targets=["Recruiter__c"], role="preferred_support_person"),
        _field("Lead__c", "Lead", "reference", "Lead of an applicant before conversion.", targets=["Lead"],
               role="applicant_lead"),
        _field("Client_Account__c", "Client Account", "reference", "B2B client account of the interview.",
               targets=["Account"], role="client_account")]
    by["Session__c"]["fields"] += [
        _field("Session_Role__c", "Session Role", "picklist", "Role of the session's host.",
               values=["Interviewer", "Support"], roles=["categorical_attribute"]),
        _field("Additional_Attendee_1__c", "Additional Attendee 1", "reference", "Another employee in the session.",
               targets=["Recruiter__c"], role="additional_attendee")]
    k.append(_obj("Onboarding__c", "Onboarding", "Onboardings", "One record is one onboarding of a candidate.", [
        _field("Account__c", "Account", "reference", "Candidate being onboarded.", targets=["Account"],
               role="candidate"),
        _field("Status__c", "Status", "picklist", "Onboarding status.", values=["Started", "Completed"],
               roles=["business_status"])]))
    k.append(_obj("Contact", "Contact", "Contacts", "One record is one contact (mirrors a person account).", [
        _field("AccountId", "Account", "reference", "Account of the contact.", targets=["Account"],
               role="parent_account")]))
    return k


TERMS_TABLES = _terms_tables()
TERMS_KNOWLEDGE = _terms_knowledge()


def _no_person_type(tables: list) -> list:
    """``tables`` without the Person Account record type (the flag fallback: only IsPersonAccount can be read)."""
    t = copy.deepcopy(tables)
    for i, (name, cols, rows) in enumerate(t):
        if name == "RecordType":
            t[i] = (name, cols, [r for r in rows if r[0] != F.RT_PERSON])
    return t


def open_terms_fixture(tmp_path: Path, *, person_type: bool = True, resolver=None, **overrides):
    """``open_records`` over TERMS_TABLES and TERMS_KNOWLEDGE (today pinned to 2026-10-13). ``person_type=False``:
    without the Person Account record type. ``resolver``: a ``config.ResolverConfig`` (None: the default)."""
    import datetime as dt

    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    wh = F.make_warehouse(tmp_path, tables=TERMS_TABLES if person_type else _no_person_type(TERMS_TABLES))
    kn = F.make_knowledge(tmp_path, knowledge=TERMS_KNOWLEDGE)
    opts = {"warehouse": str(wh), "today": dt.date(2026, 10, 13)}
    opts.update(overrides)
    args = (load_records_config(**opts), str(kn)) + ((resolver,) if resolver is not None else ())
    return open_records(*args)
