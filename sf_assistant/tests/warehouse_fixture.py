"""Synthetic warehouse + knowledge for the okrec tests (no real record data).

``make_warehouse(tmp_path)`` builds a DuckDB file shaped like the real warehouse: a ``raw`` schema of
VARCHAR tables and ``main`` views that ``TRY_CAST`` typed columns, plus ``main."Organization"`` and
``raw."_sync_meta"``. ``make_knowledge(tmp_path)`` writes minimal knowledge JSONs in the real schema.
All names are synthetic ("Recruiter A", "Candidate 1"; the people added for the lookup tests, "Delta Person" and
"Echo One", hold no digits so the hint tests can check that no digits appear). The counts the tests rely on are
listed in ``FACTS``.
"""

from __future__ import annotations

import json
from pathlib import Path

import duckdb


def sid(prefix: str, n: int) -> str:
    """18-character synthetic Salesforce Id."""
    return f"{prefix}{n:012d}AAA"


# Id prefixes per object.
P = {"Interview__c": "a0A", "Internal_Interview__c": "a0B", "Candidate_Training__c": "a0C", "Session__c": "a0D",
     "Program_Version__c": "a0E", "Program__c": "a0F", "Deliverable__c": "a0G", "Candidate_Portal_Credential__c": "a0H",
     "Recruiter__c": "a06", "Department__c": "a07", "Account": "001", "User": "005", "Group": "00G",
     "RecordType": "012", "Interview__History": "017", "Interview_Participant__c": "a0J", "Log_Entry__c": "a0K"}

REC_A, REC_B1, REC_B2, REC_C = (sid("a06", i) for i in (1, 2, 3, 4))
REC_D, REC_E1, REC_E2 = sid("a06", 5), sid("a06", 6), sid("a06", 7)
REC_F = sid("a06", 8)   # "Foxtrot Person": a linked login user plus a different, unlinked User with the same name
ORPHAN_REC = sid("a06", 99)
DEPT_1, DEPT_2 = sid("a07", 1), sid("a07", 2)
CAND_1, CAND_2, CLIENT = sid("001", 1), sid("001", 2), sid("001", 3)
ACC_A, ACC_E1, CAND_D = sid("001", 4), sid("001", 5), sid("001", 6)
RT_PERSON, RT_B2B, RT_INTERVIEW, RT_INITIAL = (sid("012", i) for i in (1, 2, 3, 4))
RT_RECRUITER = sid("012", 5)
USER_1, QUEUE_1 = sid("005", 1), sid("00G", 1)
USER_2, USER_3 = sid("005", 2), sid("005", 3)
USER_4, USER_5 = sid("005", 4), sid("005", 5)   # both "Foxtrot Person": USER_4 is REC_F's login, USER_5 is not linked
II = [sid("a0B", i) for i in range(1, 7)]
PV1, PV2 = sid("a0E", 1), sid("a0E", 2)
PROG_1, PROG_2 = sid("a0F", 1), sid("a0F", 2)
CT = [sid("a0C", i) for i in range(1, 5)]

#: Facts the tests assert (all computed from the rows below).
FACTS = {
    "interviews": 10,
    "outcome_offer_received": 2,
    "status_offer_received_legacy": 2,
    "ghosted": 2,
    "rejected": 1,
    "full_time": 6,
    "contract": 3,
    "recruiter_groups": {"Recruiter A": 4, "Recruiter B (#1)": 2, "Recruiter B (#2)": 1, "Recruiter C": 1,
                         "(unresolved id)": 1, "(blank)": 1},
    "interview_months_2026": {"2026-05": 1, "2026-06": 0, "2026-07": 1, "2026-08": 1, "2026-09": 3, "2026-10": 3},
    "interviews_without_date": 1,
    "internal": 6,
    "internal_completed": 2,
    "internal_avg_score": 25.17,
    "internal_scored": 3,
    "internal_created_this_month_ny": 3,
    "internal_created_last_month_ny": 2,
    "internal_created_this_month_utc": 4,
    "active_trainings_without_sessions": 1,
    "trainings_with_2_sessions": 1,
    "trainings_by_program": {"Program Alpha": 2, "Program Beta": 1, "(blank)": 1},
    "locked_by_program": {"Program Alpha": 2, "Program Beta": 1},
    "log_entries": 60,
    # Lookup disambiguation (RECORDS_SPEC §21.12): Internal_Interview__c rows by person and lookup.
    "delta_interviewer": 3, "delta_interviewer_by_status": {"Completed": 2, "Scheduled": 1},
    "delta_interviewer_by_type": {"Intake": 1, "Mock": 2},
    "delta_split_totals": {"Completed": 2, "Scheduled": 1},
    "delta_recruiter": 0, "delta_readiness": 1, "delta_any_role": 3,
    "delta_as_candidate": 2,
    "recruiter_a_recruiter": 2, "echo_one_interviewer": 1,
    "recruiter_b_interviewer_by_record": {"Recruiter B (#1)": 0, "Recruiter B (#2)": 1},
    # Internal_Interview__c.CreatedById: 2 records by Foxtrot Person's login (USER_4), 1 by the unlinked same-name USER_5
    "foxtrot_created_by_login": 2, "foxtrot_created_by_other_user": 1,
    "person_accounts": 3,
}

# (table, {column: duckdb type or None for VARCHAR}, rows as tuples in column order)
TS = "TIMESTAMP"
TABLES: list[tuple[str, dict[str, str | None], list[tuple]]] = []


def _t(name: str, cols: dict[str, str | None], rows: list[tuple]) -> None:
    TABLES.append((name, cols, rows))


_t("Interview__c", {
    "Id": None, "Name": None, "IsDeleted": "BOOLEAN", "OwnerId": None, "RecordTypeId": None, "CreatedDate": TS,
    "CreatedById": None, "LastModifiedDate": TS, "Interview_Status__c": None, "Interview_Outcome__c": None,
    "Employment_Type__c": None, "Date_of_Interview__c": "DATE", "Recruiter__c": None, "Candidate__c": None,
    "Client_Bill_Rate__c": "DECIMAL(18,2)", "Evaluation_Parameter__c": None, "StartTime_Text__c": None,
    "EndTime_Text__c": None, "Zoom_Passcode__c": None, "Support_Ref__c": None, "Interviewer_s_Name__c": None}, [
    # Id, Name, del, owner, RT, created(UTC), by, modified, status, outcome, emp, date, recruiter, candidate,
    # rate, eval, start, end, passcode, support_ref
    (sid("a0A", 1), "I-0001", "false", USER_1, RT_INTERVIEW, "2026-09-01T03:30:00.000Z", USER_1, "2026-09-02T00:00:00.000Z",
     "Completed", "Offer Received", "Full Time", "2026-09-05", REC_A, CAND_1, "100.00", "Communication;Coding",
     "09:00 AM", "10:00 AM", "zp-111", REC_B1, "Client Person"),
    (sid("a0A", 2), "I-0002", "false", QUEUE_1, RT_INTERVIEW, "2026-09-01T04:30:00.000Z", USER_1, "2026-09-02T00:00:00.000Z",
     "Completed", "Rejected", "Contract", "2026-09-10", REC_A, CAND_1, "120.50", "Coding", "11:00 AM", "10:30 AM",
     "zp-222", REC_B1, "Client Person"),
    (sid("a0A", 3), "I-0003", "false", USER_1, RT_INTERVIEW, "2026-10-01T12:00:00.000Z", USER_1, None,
     "Completed", "Ghosted", "Full Time", "2026-10-02", REC_B1, CAND_2, None, "Design", "01:00 PM", "02:00 PM", None, None, None),
    (sid("a0A", 4), "I-0004", "false", USER_1, RT_INITIAL, "2026-10-05T12:00:00.000Z", USER_1, None,
     "Scheduled", None, "Full Time", "2026-10-20", REC_B1, CAND_2, "90.00", None, None, None, None, None, None),
    (sid("a0A", 5), "I-0005", "false", USER_1, RT_INTERVIEW, "2026-08-10T12:00:00.000Z", USER_1, None,
     "Offer Received", "Offer Received", "Contract", "2026-08-15", REC_B2, CAND_1, None, None, None, None, None, None, None),
    (sid("a0A", 6), "I-0006", "false", USER_1, RT_INTERVIEW, "2026-07-01T12:00:00.000Z", USER_1, None,
     "Offer Received", None, "Full Time", "2026-07-01", ORPHAN_REC, CAND_2, None, None, None, None, None, None, None),
    (sid("a0A", 7), "I-0007", "false", USER_1, RT_INTERVIEW, "2026-10-13T15:00:00.000Z", USER_1, None,
     "Cancelled", None, None, "2026-10-13", None, None, None, None, None, None, None, None, None),
    (sid("a0A", 8), "I-0008", "false", USER_1, None, "2026-05-01T12:00:00.000Z", USER_1, None,
     "Completed", "Ghosted", "Full Time", "2026-05-20", REC_C, CAND_1, None, None, None, None, None, None, None),
    (sid("a0A", 9), "I-0009", "false", USER_1, RT_INTERVIEW, "2026-06-01T12:00:00.000Z", USER_1, None,
     "No Show", None, "Contract", None, REC_A, CAND_2, None, None, None, None, None, None, None),
    (sid("a0A", 10), "I-0010", "false", USER_1, RT_INTERVIEW, "2026-09-20T12:00:00.000Z", USER_1, None,
     "Completed", "Moved to Next Round", "Full Time", "2026-09-28", REC_A, CAND_1, None, None, None, None, None, None, None),
])

_t("Internal_Interview__c", {"Id": None, "Name": None, "CreatedDate": TS, "Status__c": None,
                             "Human_Total_Score__c": "DECIMAL(5,2)", "Interview_Type__c": None,
                             "Meeting_Passcode_IS__c": None, "Interviewer__c": None, "Recruiter__c": None,
                             "Interview_Support_Person__c": None, "Interview_Readiness_Person__c": None,
                             "Candidate__c": None, "CreatedById": None}, [
    # Id, Name, created, status, score, type, passcode, interviewer, recruiter, support, readiness, candidate, created by
    (II[0], "INT-0001", "2026-10-01T03:00:00.000Z", "Completed", "20.50", "Intake", None, REC_D, REC_A, None, REC_D, CAND_1,
     USER_4),
    (II[1], "INT-0002", "2026-10-01T05:00:00.000Z", "Completed", "30.00", "Mock", None, REC_D, None, None, None, CAND_D,
     USER_4),
    (II[2], "INT-0003", "2026-09-15T12:00:00.000Z", "Scheduled", None, "Mock", None, REC_D, REC_A, None, None, CAND_2,
     USER_5),
    (II[3], "INT-0004", "2026-10-10T12:00:00.000Z", "Unscheduled", None, "Intake", None, REC_E1, None, None, REC_E1,
     CAND_1, None),
    (II[4], "INT-0005", "2026-08-01T12:00:00.000Z", None, None, None, None, None, None, None, None, CAND_2, None),
    (II[5], "INT-0006", "2026-10-12T12:00:00.000Z", "Cancelled", "25.00", "Mock", None, REC_B2, REC_B1, REC_C, None,
     CAND_D, None),
])

_t("Recruiter__c", {"Id": None, "Name": None, "CreatedDate": TS, "Employment_Status__c": None, "Department__c": None,
                    "Email__c": None, "Recruiter_User__c": None, "Recruiter_Contact__c": None, "Portal_User__c": None,
                    "Team__c": None}, [
    # Team__c (a second lookup to Department__c): only Recruiter B (#1) has one, Dept Two (Dept One: 0 through it)
    (REC_A, "Recruiter A", "2025-01-01T00:00:00.000Z", "Active Employee", DEPT_1, "a@example.invalid", USER_1, ACC_A, None,
     None),
    (REC_B1, "Recruiter B", "2025-01-01T00:00:00.000Z", "Active Employee", DEPT_2, "b1@example.invalid", None, None, None,
     DEPT_2),
    (REC_B2, "Recruiter B", "2025-01-01T00:00:00.000Z", "Former Employee", DEPT_1, None, None, None, None, None),
    (REC_C, "Recruiter C", "2025-01-01T00:00:00.000Z", "Active Employee", None, None, None, None, None, None),
    (REC_D, "Delta  Person", "2025-01-01T00:00:00.000Z", "Active Employee", None, None, None, None, None, None),  # 2 spaces
    (REC_E1, "Echo One", "2025-01-01T00:00:00.000Z", "Active Employee", None, None, USER_3, ACC_E1, None, None),
    (REC_E2, "Echo Two", "2025-01-01T00:00:00.000Z", "Former Employee", None, None, None, None, None, None),
    (REC_F, "Foxtrot Person", "2025-01-01T00:00:00.000Z", "Active Employee", None, None, USER_4, None, None, None),
])

_t("Department__c", {"Id": None, "Name": None, "CreatedDate": TS}, [
    (DEPT_1, "Dept One", "2025-01-01T00:00:00.000Z"), (DEPT_2, "Dept Two", "2025-01-01T00:00:00.000Z")])

_t("Account", {"Id": None, "Name": None, "RecordTypeId": None, "CreatedDate": TS, "Passport_Number__c": None,
               "PersonEmail": None, "Phone": None}, [
    (CAND_1, "Candidate 1", RT_PERSON, "2025-01-01T00:00:00.000Z", "X0000001", "c1@example.invalid", "000-000-0001"),
    (CAND_2, "Candidate 2", RT_PERSON, "2025-01-01T00:00:00.000Z", None, "c2@example.invalid", None),
    (CLIENT, "Client Co", RT_B2B, "2025-01-01T00:00:00.000Z", None, None, None),
    (ACC_A, "Recruiter A", RT_RECRUITER, "2025-01-01T00:00:00.000Z", None, None, None),
    (ACC_E1, "Echo One", RT_RECRUITER, "2025-01-01T00:00:00.000Z", None, None, None),
    (CAND_D, "Delta\u00a0Person", RT_PERSON, "2025-01-01T00:00:00.000Z", None, None, None),  # a non-breaking space
])

_t("RecordType", {"Id": None, "Name": None, "DeveloperName": None, "SobjectType": None, "IsActive": "BOOLEAN"}, [
    (RT_PERSON, "Person Account", "PersonAccount", "Account", "true"),
    (RT_B2B, "B2B Client", "B2B_Client", "Account", "true"),
    (RT_INTERVIEW, "Interview", "Interview", "Interview__c", "true"),
    (RT_INITIAL, "Initial Call", "Initial_Call", "Interview__c", "true"),
    (RT_RECRUITER, "Recruiter", "Recruiter", "Account", "true"),
])

_t("User", {"Id": None, "Name": None, "IsActive": "BOOLEAN", "LastPasswordChangeDate": TS, "AccountId": None}, [
    (USER_1, "User One", "true", "2026-01-01T00:00:00.000Z", None),
    (USER_2, "Echo One", "true", "2026-01-01T00:00:00.000Z", ACC_E1),   # portal user of the employee's own account
    (USER_3, "Echo One", "true", "2026-01-01T00:00:00.000Z", None),     # standard user (Recruiter_User__c)
    (USER_4, "Foxtrot Person", "true", "2026-01-01T00:00:00.000Z", None),   # Foxtrot Person's login (Recruiter_User__c)
    (USER_5, "Foxtrot Person", "true", "2026-01-01T00:00:00.000Z", None),   # a different user with the same name
])
_t("Group", {"Id": None, "Name": None}, [(QUEUE_1, "Queue One")])

_t("Candidate_Training__c", {"Id": None, "Name": None, "CreatedDate": TS, "Status__c": None, "Program_Version__c": None,
                             "Candidate__c": None, "Program__c": None}, [
    (CT[0], "CT-0001", "2026-01-01T00:00:00.000Z", "Active", PV1, CAND_1, PROG_1),
    (CT[1], "CT-0002", "2026-01-01T00:00:00.000Z", "Active", PV2, CAND_2, PROG_2),
    (CT[2], "CT-0003", "2026-01-01T00:00:00.000Z", "Planned", PV1, CAND_1, None),   # the copy is blank here
    (CT[3], "CT-0004", "2026-01-01T00:00:00.000Z", "Completed", None, CAND_2, None),
])

_t("Session__c", {"Id": None, "Name": None, "CreatedDate": TS, "Candidate_Training__c": None, "Host_User__c": None,
                  "Co_Host__c": None, "Purpose__c": None, "Session_Date__c": "DATE"}, [
    (sid("a0D", 1), "SES-0001", "2026-09-01T00:00:00.000Z", CT[0], REC_A, REC_B1, "Training", "2026-09-01"),
    (sid("a0D", 2), "SES-0002", "2026-09-02T00:00:00.000Z", CT[0], REC_A, None, "Training", "2026-09-02"),
    (sid("a0D", 3), "SES-0003", "2026-09-03T00:00:00.000Z", CT[2], REC_C, None, "Mock", "2026-09-03"),
    (sid("a0D", 4), "SES-0004", "2026-09-04T00:00:00.000Z", None, None, None, "Training", "2026-09-04"),
])

_t("Program_Version__c", {"Id": None, "Name": None, "Program__c": None}, [(PV1, "PV-1", PROG_1), (PV2, "PV-2", PROG_2)])
_t("Program__c", {"Id": None, "Name": None}, [(PROG_1, "Program Alpha"), (PROG_2, "Program Beta")])

_t("Deliverable__c", {"Id": None, "Name": None, "CreatedDate": TS, "Status__c": None, "Candidate_Training__c": None,
                      "Approved_By__c": None}, [
    (sid("a0G", 1), "DEL-0001", "2026-09-01T00:00:00.000Z", "Locked", CT[0], None),
    (sid("a0G", 2), "DEL-0002", "2026-09-01T00:00:00.000Z", "Locked", CT[1], None),
    (sid("a0G", 3), "DEL-0003", "2026-09-01T00:00:00.000Z", "Open", CT[0], None),
    (sid("a0G", 4), "DEL-0004", "2026-09-01T00:00:00.000Z", "Locked", CT[2], None),
])

_t("Interview__History", {"Id": None, "IsDeleted": "BOOLEAN", "ParentId": None, "CreatedById": None, "CreatedDate": TS,
                          "Field": None, "DataType": None, "OldValue": None, "NewValue": None}, [
    (sid("017", 1), "false", sid("a0A", 1), USER_1, "2026-08-25T10:00:00.000Z", "created", "Text", None, None),
    (sid("017", 2), "false", sid("a0A", 1), USER_1, "2026-09-06T10:00:00.000Z", "Interview_Status__c", "DynamicEnum",
     "Scheduled", "Completed"),
    (sid("017", 3), "false", sid("a0A", 1), USER_1, "2026-09-06T11:00:00.000Z", "Recruiter__c", "EntityId", REC_B1, REC_A),
    (sid("017", 4), "false", sid("a0A", 1), USER_1, "2026-09-06T11:00:00.000Z", "Recruiter__c", "Text", "Recruiter B",
     "Recruiter A"),
    (sid("017", 5), "false", sid("a0A", 1), USER_1, "2026-09-07T11:00:00.000Z", "Zoom_Passcode__c", "Text", "zp-000",
     "zp-111"),
    (sid("017", 6), "false", sid("a0A", 2), USER_1, "2026-09-11T10:00:00.000Z", "Interview_Status__c", "DynamicEnum",
     "Scheduled", "Completed"),
])

_t("Candidate_Portal_Credential__c", {"Id": None, "Name": None, "CreatedDate": TS, "Username__c": None, "Password__c": None,
                                      "Status__c": None}, [
    (sid("a0H", 1), "CCN-0001", "2026-09-01T00:00:00.000Z", "user-one", "not-a-real-secret", "Active"),
    (sid("a0H", 2), "CCN-0002", "2026-09-02T00:00:00.000Z", "user-two", "not-a-real-secret", "Disabled"),
])

_t("Interview_Participant__c", {"Id": None, "Name": None, "Interview__c": None}, [])

_t("Log_Entry__c", {"Id": None, "Name": None, "CreatedDate": TS, "Message__c": None},
   [(sid("a0K", i), f"LOG-{i:04d}", f"2026-09-{(i % 28) + 1:02d}T00:00:00.000Z", "x" * 5000) for i in range(1, 61)])

_t("Organization", {"Id": None, "Name": None, "IsSandbox": "BOOLEAN", "TimeZoneSidKey": None,
                    "FiscalYearStartMonth": "BIGINT"}, [("00D000000000001AAA", "Synthetic Org", "true", "America/New_York", "1")])

SYNC = {name: "2026-10-13 14:00:00" for name, _c, _r in TABLES if name != "Organization"}
SYNC["Program__c"] = "2026-10-10 08:00:00"   # stale object (more than 24 h older than the newest sync)
SYNC["Interview__c"] = "2026-10-13 14:30:00"


def make_warehouse(tmp_path: Path, name: str = "warehouse.duckdb", tables: list | None = None) -> Path:
    """Build the synthetic warehouse file and return its path (``tables``: another table list, e.g. PEOPLE_TABLES)."""
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    path = Path(tmp_path) / name
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS raw")
    tables = TABLES if tables is None else tables
    sync = {t: SYNC.get(t, "2026-10-13 14:00:00") for t, _c, _r in tables if t != "Organization"}
    for table, cols, rows in tables:
        names = list(cols)
        con.execute(f'CREATE TABLE raw."{table}" (' + ", ".join(f'"{c}" VARCHAR' for c in names) + ")")
        if rows:
            con.executemany(f'INSERT INTO raw."{table}" VALUES (' + ", ".join("?" for _ in names) + ")",
                            [list(r) for r in rows])
        sel = ", ".join(f'TRY_CAST("{c}" AS {t}) AS "{c}"' if t else f'"{c}"' for c, t in cols.items())
        con.execute(f'CREATE VIEW main."{table}" AS SELECT {sel} FROM raw."{table}"')
    con.execute('CREATE TABLE raw."_sync_meta" (object_name VARCHAR, watermark VARCHAR, updated_at TIMESTAMP)')
    con.executemany('INSERT INTO raw."_sync_meta" VALUES (?, ?, ?)', [[k, None, v] for k, v in sync.items()])
    con.close()
    return path


# ------------------------------------------------------------------------------------- knowledge

def _field(api: str, label: str, typ: str, meaning: str, *, values: list | None = None, targets: list | None = None,
           roles: list | None = None, role: str | None = None, **extra) -> dict:
    f = {"api_name": api, "label": label, "type": typ, "business_meaning": meaning, "required": False,
         "calculated": False, "semantic": {"concept": meaning[:40], "roles": roles or []},
         "picklist": None, "reference": None}
    if values is not None:
        f["picklist"] = {"restricted": True, "values": [
            {"label": v[0], "value": v[0], "business_meaning": v[1]} if isinstance(v, tuple) else {"label": v, "value": v}
            for v in values]}
    if targets is not None:
        f["reference"] = {"target_objects": targets, "relationship_name": api.replace("__c", "s"),
                          "relationship_type": "lookup", "business_role": role or label.lower(),
                          "business_meaning": meaning}
    f.update(extra)
    return f


def _rel(api: str, targets: list, from_target: str, meaning: str = "") -> dict:
    """A relationships[] entry with direction_semantics.from_target (the reverse meaning of a lookup)."""
    return {"field": api, "type": "lookup", "target_objects": targets, "relationship_name": api.replace("__c", "s"),
            "business_meaning": meaning, "direction_semantics": {"from_source": meaning, "from_target": from_target},
            "cardinality": "many_to_one"}


def _obj(api: str, label: str, plural: str, grain: str, fields: list[dict], *, children: list | None = None,
         temporal: dict | None = None, concepts: list | None = None, relationships: list | None = None) -> dict:
    return {
        "object": {"api_name": api, "label": label, "plural_label": plural, "custom": True,
                   "description": f"{label}: {grain}", "business_domain": ["Synthetic"],
                   "business_semantics": {"primary_concept": label.lower(), "concepts": concepts or [label.lower()]},
                   "record_grain": {"description": grain},
                   "discovery_summary": {"one_line": f"{api} holds one {label.lower()} per record.",
                                         "key_concepts": concepts or [label.lower()], "key_relationships": []}},
        "record_types": [], "fields": fields, "relationships": relationships or [],
        "child_relationships": children or [], "field_dependencies": [], "temporal_semantics": temporal or {},
        "validation_rules": [], "automation": {"flows": [], "apex": []}, "business_process_usage": [],
        "provenance": {"source_environment": "synthetic"},
    }


KNOWLEDGE: list[dict] = [
    _obj("Interview__c", "Interview", "Interviews", "One record is one client interview round.", [
        _field("Name", "Interview Number", "auto_number", "Interview number."),
        _field("Interview_Status__c", "Interview Status", "picklist", "Lifecycle status of the client interview.",
               values=[("Scheduled", "Booked and upcoming."), ("Completed", "The interview took place."),
                       ("Rescheduled", "Moved."), ("Cancelled", "Called off."), ("No Show", "Candidate did not join.")],
               roles=["business_status"]),
        _field("Interview_Outcome__c", "Interview Outcome", "picklist", "Result of the client interview.",
               values=[("Moved to Next Round", "Next round."), ("Rejected", "Not selected."),
                       ("Offer Received", "The company made an offer."), ("Ghosted", "No reply after 14 days."),
                       ("NA", "Not applicable."), ("Reference Check", "References requested.")], roles=["outcome"]),
        _field("Employment_Type__c", "Employment Type", "picklist", "Employment type of the position.",
               values=["Full-Time", "Contract"]),
        _field("Date_of_Interview__c", "Date of Interview", "date", "Calendar date of the client interview."),
        _field("Recruiter__c", "Recruiter", "reference", "Employee who owns the interview.", targets=["Recruiter__c"],
               role="recruiter"),
        _field("Candidate__c", "Candidate", "reference", "Candidate person account.", targets=["Account"]),
        _field("Client_Bill_Rate__c", "Client Bill Rate", "currency", "Hourly bill rate."),
        _field("Interviewer_s_Name__c", "Interviewer's Name", "string", "Name of the client's interviewer."),
        _field("Evaluation_Parameter__c", "Evaluation Parameter", "multipicklist", "Skills evaluated.",
               values=["Communication", "Coding", "Design"]),
        _field("StartTime_Text__c", "Start Time Text", "string", "Start time as text."),
        _field("EndTime_Text__c", "End Time Text", "string", "End time as text."),
        _field("Zoom_Passcode__c", "Zoom Passcode", "string", "Meeting passcode."),
        _field("Account_Manager__c", "Account Manager", "reference", "B2B account manager.", targets=["User"]),
        _field("Old_Status__c", "Old Status", "picklist", "Former status field.", values=["Open"],
               replaced_by="Interview_Status__c", legacy=True),
    ], temporal={"interview_date": {"field": "Date_of_Interview__c", "business_meaning": "Day the client interview happens."},
                 "created_at": {"field": "CreatedDate", "business_meaning": "When the record was created."}},
        concepts=["client interview", "interview round"],
        relationships=[_rel("Recruiter__c", ["Recruiter__c"], "client interviews this employee recruits for")]),
    _obj("Internal_Interview__c", "Internal Interview", "Internal Interviews", "One record is one internal mock interview.", [
        _field("Name", "Internal Interview Number", "auto_number", "Number."),
        _field("Status__c", "Status", "picklist", "Status of the mock interview.",
               values=["Scheduled", "Completed", "Cancelled", "Unscheduled"], roles=["business_status"]),
        _field("Human_Total_Score__c", "Human Total Score", "number", "Total score given by the interviewer.", scale=2),
        _field("Interview_Type__c", "Interview Type", "picklist", "Kind of mock interview.", values=["Intake", "Mock"]),
        _field("Meeting_Passcode_IS__c", "Zoom Passcode", "string", "Meeting passcode."),
        _field("Interviewer__c", "Interviewer", "reference", "Employee conducting the interview.",
               targets=["Recruiter__c"], role="interviewer"),
        _field("Recruiter__c", "Recruiter", "reference", "Recruiter responsible for the candidate.",
               targets=["Recruiter__c"], role="recruiter"),
        _field("Interview_Support_Person__c", "Interview Support Person", "reference", "Support employee of the mock.",
               targets=["Recruiter__c"], role="support_employee", legacy=True),
        _field("Candidate__c", "Candidate", "reference", "Candidate being interviewed.", targets=["Account"],
               role="candidate"),
        _field("Final_Decision_By__c", "Final Decision By", "reference", "Employee who made the final decision.",
               targets=["Recruiter__c"], role="final_decision_maker"),
    ], temporal={"created_at": {"field": "CreatedDate", "business_meaning": "When the mock interview was created."}},
        concepts=["mock interview", "internal interview"],
        relationships=[_rel("Interviewer__c", ["Recruiter__c"], "interviews this employee conducts"),
                       _rel("Recruiter__c", ["Recruiter__c"], "mocks for this recruiter's candidates"),
                       _rel("Interview_Support_Person__c", ["Recruiter__c"], "mocks this employee supports"),
                       _rel("Candidate__c", ["Account"], "interviews of this candidate")]),
    _obj("Recruiter__c", "Employee", "Employees", "One record is one employee.", [
        _field("Employment_Status__c", "Employment Status", "picklist", "Whether the employee is active.",
               values=["Active Employee", "Former Employee"], roles=["business_status"]),
        _field("Department__c", "Department", "reference", "Department of the employee.", targets=["Department__c"]),
        _field("Team__c", "Team", "reference", "Team unit of the employee (a department record).",
               targets=["Department__c"], role="team_unit"),
        _field("Email__c", "Email", "email", "Work e-mail."),
        _field("Recruiter_User__c", "Recruiter User", "reference", "Salesforce user of the employee.", targets=["User"],
               role="employee_login_user"),
        _field("Recruiter_Contact__c", "Recruiter Contact", "reference", "The employee's own person account.",
               targets=["Account"], role="employee_person_account"),
        _field("Portal_User__c", "Portal User", "reference", "Portal login of the employee.", targets=["User"],
               role="employee_portal_login_user"),
    ], concepts=["employee", "recruiter", "trainer"]),
    _obj("Department__c", "Department", "Departments", "One record is one department.", []),
    _obj("Candidate_Training__c", "Candidate Training", "Candidate Trainings", "One record is one training enrolment.", [
        _field("Status__c", "Status", "picklist", "Training status.", values=["Active", "Planned", "Completed"],
               roles=["business_status"]),
        _field("Program_Version__c", "Program Version", "reference", "Program version.", targets=["Program_Version__c"]),
        _field("Candidate__c", "Candidate", "reference", "Candidate.", targets=["Account"]),
        _field("Program__c", "Program", "reference", "Program of the enrolment.", targets=["Program__c"]),
    ], children=[{"child_object": "Session__c", "field": "Candidate_Training__c", "relationship_name": "Sessions",
                  "cascade_delete": False, "business_meaning": "Sessions of the training.", "direction_semantics": {}}],
        concepts=["training", "enrolment"]),
    _obj("Session__c", "Session", "Sessions", "One record is one session.", [
        _field("Candidate_Training__c", "Candidate Training", "reference", "Training.", targets=["Candidate_Training__c"]),
        _field("Host_User__c", "Session Host User", "reference", "Employee hosting.", targets=["Recruiter__c"],
               role="host_employee"),
        _field("Co_Host__c", "Co Host", "reference", "Second employee.", targets=["Recruiter__c"], role="co_host"),
        _field("Purpose__c", "Purpose", "picklist", "Why the session runs.", values=["Training", "Mock"]),
        _field("Session_Date__c", "Session Date", "date", "Day of the session."),
    ], temporal={"session_date": {"field": "Session_Date__c", "business_meaning": "Day of the session."}},
        relationships=[_rel("Host_User__c", ["Recruiter__c"], "sessions the employee hosts")]),
    _obj("Program_Version__c", "Program Version", "Program Versions", "One record is one program version.", [
        _field("Program__c", "Program", "reference", "Program.", targets=["Program__c"])]),
    _obj("Program__c", "Program", "Programs", "One record is one training program.", []),
    _obj("Deliverable__c", "Deliverable", "Deliverables", "One record is one deliverable.", [
        _field("Status__c", "Status", "picklist", "Deliverable status.", values=["Locked", "Open"], roles=["business_status"]),
        _field("Candidate_Training__c", "Candidate Training", "reference", "Training.", targets=["Candidate_Training__c"]),
        _field("Approved_By__c", "Approved By", "reference", "Employee who approved the deliverable.",
               targets=["Recruiter__c"], role="approver"),
    ]),
    _obj("Candidate_Portal_Credential__c", "Candidate Portal Credential", "Candidate Portal Credentials",
         "One record is one portal login.", [
             _field("Username__c", "Username", "string", "Login name."),
             _field("Password__c", "Password", "string", "Password hash."),
             _field("Status__c", "Status", "picklist", "Login status.", values=["Active", "Disabled"]),
         ]),
    _obj("Interview_Participant__c", "Interview Participant", "Interview Participants", "One record is one participant.", [
        _field("Interview__c", "Interview", "reference", "Interview.", targets=["Interview__c"])]),
    _obj("Training_Mock_Result__c", "Training Mock Result", "Training Mock Results", "One record is one result.", []),
    _obj("Log_Entry__c", "Log Entry", "Log Entries", "One record is one log line.", [
        _field("Message__c", "Message", "string", "Log text.")]),
]

# Candidate_Training__c.Program__c copies Program_Version__c.Program__c (a mirror lookup, Catalog._mirrors).
next(k for k in KNOWLEDGE if k["object"]["api_name"] == "Candidate_Training__c")["field_dependencies"] = [
    {"field": "Program__c", "depends_on": ["Program_Version__c.Program__c"], "kind": "business_rule",
     "meaning": "The training's Program must equal the version's Program."}]


def make_knowledge(tmp_path: Path, name: str = "knowledge", knowledge: list | None = None) -> Path:
    """Write the synthetic knowledge files and return the folder (``knowledge``: another list, e.g. PEOPLE_KNOWLEDGE)."""
    d = Path(tmp_path) / name
    d.mkdir(parents=True, exist_ok=True)
    for kn in (KNOWLEDGE if knowledge is None else knowledge):
        (d / f"{kn['object']['api_name']}.json").write_text(json.dumps(kn, indent=1), encoding="utf-8")
    return d


def open_fixture(tmp_path: Path, **overrides):
    """``open_records`` on the fixture with today pinned to 2026-10-13 (America/New_York by org default)."""
    import datetime as dt

    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    wh = make_warehouse(tmp_path)
    kn = make_knowledge(tmp_path)
    opts = {"warehouse": str(wh), "today": dt.date(2026, 10, 13)}
    opts.update(overrides)
    return open_records(load_records_config(**opts), str(kn))


# ------------------------------------------------------------------------------------- people (RECORDS_SPEC §22.11)
# A larger copy of the fixture for the questions about named people: leads (one converted to a candidate account),
# one first name held by an employee, a candidate and a lead, a lead named only with catalog words, an employee no
# business record references (with a linked active user), a reporting line, a training with its session and an
# attendee row, two interviews that share only a company, and a documented date of birth. All names are synthetic.
LEAD_KV, LEAD_CONV, LEAD_WORDS = sid("00Q", 1), sid("00Q", 2), sid("00Q", 3)
REC_KZ, REC_LN, REC_OP, REC_PQ, REC_RS = (sid("a06", i) for i in (20, 21, 22, 23, 24))
REC_ST, REC_UV, REC_VW, REC_WY = (sid("a06", i) for i in (25, 26, 27, 28))
REC_XZ = sid("a06", 29)
LEAD_SIX = sid("00Q", 4)
DEPT_3 = sid("a07", 3)
CAND_KQ, CAND_CONV = sid("001", 20), sid("001", 21)
USER_LN = sid("005", 20)
CT_P = sid("a0C", 20)
SES_P = sid("a0D", 20)
ATT_P = sid("a0L", 1)
COMP_1 = sid("a0M", 1)
INT_C1, INT_C2 = sid("a0A", 20), sid("a0A", 21)

#: The people of the larger fixture, by role in the tests.
PEOPLE = {"shared_first": "Kilo", "employee_kilo": "Kilo Zephyr", "candidate_kilo": "Kilo Quill",
          "lead_kilo": "Kilo Vortex", "converted_lead": "Juliet Converse", "words_lead": "Session Purpose",
          "unreferenced": "Lima Nobel", "manager": "Oscar Prime", "senior": "Papa Quinn", "junior": "Romeo Stark",
          "trainer": "Sierra Tango", "attendee": "Uniform Vale", "company_a": "Victor Wren",
          "company_b": "Whiskey Yarrow", "three_words": "Xavier Yale Zorn", "six_words": "Amara Belle Cora Dane Elise Faye"}


def _with(tables: list, name: str, extra_cols: dict, rows: list[dict]) -> None:
    """Add columns (blank on the existing rows) and rows (dicts by column) to one table of ``tables``."""
    for i, (t, cols, old) in enumerate(tables):
        if t != name:
            continue
        new_cols = dict(cols)
        new_cols.update(extra_cols)
        keys = list(new_cols)
        new_rows = [tuple(r) + (None,) * (len(keys) - len(r)) for r in old]
        new_rows += [tuple(r.get(k) for k in keys) for r in rows]
        tables[i] = (t, new_cols, new_rows)
        return
    tables.append((name, dict(extra_cols), [tuple(r.get(k) for k in extra_cols) for r in rows]))


def _people_tables() -> list:
    import copy as _copy
    t = _copy.deepcopy(TABLES)
    day = "2025-01-01T00:00:00.000Z"
    _with(t, "Recruiter__c", {"Manager__c": None, "Assigned_Sr_Recruiter__c": None, "Date_of_Birth__c": "DATE",
                              "Old_Level__c": None}, [
        {"Id": REC_KZ, "Name": "Kilo Zephyr", "CreatedDate": day, "Employment_Status__c": "Active Employee",
         "Old_Level__c": "Senior"},
        {"Id": REC_XZ, "Name": "Xavier Yale Zorn", "CreatedDate": day, "Employment_Status__c": "Active Employee"},
        {"Id": REC_LN, "Name": "Lima Nobel", "CreatedDate": day, "Employment_Status__c": "Active Employee",
         "Recruiter_User__c": USER_LN, "Date_of_Birth__c": "1990-01-01"},
        {"Id": REC_OP, "Name": "Oscar Prime", "CreatedDate": day, "Employment_Status__c": "Active Employee"},
        {"Id": REC_PQ, "Name": "Papa Quinn", "CreatedDate": day, "Employment_Status__c": "Active Employee",
         "Department__c": DEPT_1, "Manager__c": REC_OP},
        {"Id": REC_RS, "Name": "Romeo Stark", "CreatedDate": day, "Employment_Status__c": "Active Employee",
         "Department__c": DEPT_1, "Manager__c": REC_OP, "Assigned_Sr_Recruiter__c": REC_PQ},
        {"Id": REC_ST, "Name": "Sierra Tango", "CreatedDate": day, "Employment_Status__c": "Active Employee"},
        {"Id": REC_UV, "Name": "Uniform Vale", "CreatedDate": day, "Employment_Status__c": "Active Employee"},
        {"Id": REC_VW, "Name": "Victor Wren", "CreatedDate": day, "Employment_Status__c": "Active Employee"},
        {"Id": REC_WY, "Name": "Whiskey Yarrow", "CreatedDate": day, "Employment_Status__c": "Active Employee"}])
    # Dept One has a head (a department is an activity record that holds people) and a parent department (a column
    # named like its own table: every column of a statement is alias-qualified, so the guard passes it)
    _with(t, "Department__c", {"Head__c": None, "Department__c": None}, [
        {"Id": DEPT_3, "Name": "Dept Three", "CreatedDate": day}])
    for i, (name, cols, rows) in enumerate(t):
        if name == "Department__c":
            keys = list(cols)
            fix = {"Head__c": REC_OP, "Department__c": DEPT_3}
            t[i] = (name, cols, [tuple(fix[k] if k in fix and r[0] == DEPT_1 else v for k, v in zip(keys, r))
                                 for r in rows])
    _with(t, "User", {"UserType": None, "LastLoginDate": TS}, [
        {"Id": USER_LN, "Name": "Lima Nobel", "IsActive": "true", "UserType": "Standard",
         "LastLoginDate": "2026-10-01T09:00:00.000Z"}])
    _with(t, "Account", {"Assigned_Recruiter__c": None, "IsPersonAccount": "BOOLEAN"}, [
        {"Id": CAND_KQ, "Name": "Kilo Quill", "RecordTypeId": RT_PERSON, "CreatedDate": day,
         "Assigned_Recruiter__c": REC_KZ, "IsPersonAccount": "true"},
        {"Id": CAND_CONV, "Name": "Juliet Converse", "RecordTypeId": RT_PERSON, "CreatedDate": day,
         "IsPersonAccount": "true"}])
    for i, (name, cols, rows) in enumerate(t):   # the standard flag: every account but the client company is a person
        if name == "Account":
            keys = list(cols)
            t[i] = (name, cols, [tuple(("false" if r[0] == CLIENT else "true") if k == "IsPersonAccount" else v
                                       for k, v in zip(keys, r)) for r in rows])
    _with(t, "Lead", {"Id": None, "Name": None, "CreatedDate": TS, "Status": None, "IsConverted": "BOOLEAN",
                      "ConvertedAccountId": None, "ConvertedDate": "DATE", "LeadSource": None, "OwnerId": None,
                      "CreatedById": None}, [
        {"Id": LEAD_KV, "Name": "Kilo Vortex", "CreatedDate": day, "Status": "Open", "IsConverted": "false",
         "OwnerId": USER_1, "CreatedById": USER_LN},
        {"Id": LEAD_SIX, "Name": "Amara Belle Cora Dane Elise Faye", "CreatedDate": day, "Status": "Open",
         "IsConverted": "false", "OwnerId": USER_1},
        {"Id": LEAD_CONV, "Name": "Juliet Converse", "CreatedDate": day, "Status": "Converted", "IsConverted": "true",
         "ConvertedAccountId": CAND_CONV, "ConvertedDate": "2026-05-06", "LeadSource": "Web", "OwnerId": USER_1},
        {"Id": LEAD_WORDS, "Name": "Session Purpose", "CreatedDate": day, "Status": "Open", "IsConverted": "false",
         "OwnerId": USER_1}])
    _with(t, "Candidate_Training__c", {"Assigned_Trainer__c": None}, [
        {"Id": CT_P, "Name": "CT-0020", "CreatedDate": day, "Status__c": "Active", "Candidate__c": CAND_KQ,
         "Assigned_Trainer__c": REC_ST}])
    _with(t, "Session__c", {}, [
        {"Id": SES_P, "Name": "SES-0020", "CreatedDate": day, "Candidate_Training__c": CT_P, "Host_User__c": REC_ST,
         "Purpose__c": "Training", "Session_Date__c": "2026-09-20"}])
    _with(t, "Session_Attendee__c", {"Id": None, "Name": None, "CreatedDate": TS, "Session__c": None,
                                     "Attendee_Recruiter__c": None}, [
        {"Id": ATT_P, "Name": "SA-0001", "CreatedDate": day, "Session__c": SES_P, "Attendee_Recruiter__c": REC_UV}])
    _with(t, "Company__c", {"Id": None, "Name": None}, [{"Id": COMP_1, "Name": "Company One"}])
    _with(t, "Interview__c", {"Company__c": None}, [
        {"Id": INT_C1, "Name": "I-0020", "IsDeleted": "false", "CreatedDate": day, "Interview_Status__c": "Completed",
         "Recruiter__c": REC_VW, "Company__c": COMP_1},
        {"Id": INT_C2, "Name": "I-0021", "IsDeleted": "false", "CreatedDate": day, "Interview_Status__c": "Completed",
         "Recruiter__c": REC_WY, "Company__c": COMP_1}])
    return t


def _people_knowledge() -> list:
    import copy as _copy
    k = _copy.deepcopy(KNOWLEDGE)
    by = {x["object"]["api_name"]: x for x in k}
    by["Recruiter__c"]["fields"] += [
        _field("Manager__c", "Manager", "reference", "Manager of the employee.", targets=["Recruiter__c"],
               role="manager", roles=["relationship", "hierarchy_attribute"]),
        _field("Assigned_Sr_Recruiter__c", "Reporting to", "reference", "Senior recruiter the employee reports to.",
               targets=["Recruiter__c"], role="reporting_senior_recruiter", roles=["relationship", "hierarchy_attribute"]),
        _field("Date_of_Birth__c", "Date of Birth", "date", "Birth date of the employee.",
               roles=["temporal_attribute", "contact_attribute", "security_attribute"]),
        _field("Old_Level__c", "Old Level", "picklist", "Former level field.", values=["Senior", "Junior"],
               roles=["categorical_attribute"], legacy=True)]
    for f in by["Recruiter__c"]["fields"]:
        if f["api_name"] == "Department__c":
            f["semantic"]["roles"] = ["relationship", "categorical_attribute"]
    by["Department__c"]["fields"] += [_field("Head__c", "Head", "reference", "Employee who heads the department.",
                                             targets=["Recruiter__c"], role="department_head"),
                                      _field("Department__c", "Parent Department", "reference", "Parent department.",
                                             targets=["Department__c"])]
    k.append(_obj("Account", "Account", "Accounts", "One record is one account (candidates are person accounts).", [
        _field("Assigned_Recruiter__c", "Assigned Recruiter", "reference", "Recruiter assigned to the candidate.",
               targets=["Recruiter__c"], role="assigned_recruiter", roles=["relationship", "assignment_status"])]))
    by["Candidate_Training__c"]["fields"] += [_field("Assigned_Trainer__c", "Assigned Trainer", "reference",
                                                     "Employee who trains the candidate.", targets=["Recruiter__c"],
                                                     role="trainer_employee")]
    by["Interview__c"]["fields"] += [_field("Company__c", "Company", "reference", "Hiring company.",
                                            targets=["Company__c"])]
    k.append(_obj("Lead", "Lead", "Leads", "One record is one lead.", [
        _field("OwnerId", "Owner", "reference", "Owner of the lead.", targets=["User"], role="lead_owner")]))
    k.append(_obj("Session_Attendee__c", "Session Attendee", "Session Attendees", "One record is one attendee row.", [
        _field("Session__c", "Session", "reference", "Session.", targets=["Session__c"]),
        _field("Attendee_Recruiter__c", "Attendee Recruiter", "reference", "Employee on the attendee row.",
               targets=["Recruiter__c"], role="attending_employee")]))
    k.append(_obj("Company__c", "Company", "Companies", "One record is one hiring company.", []))
    return k


PEOPLE_TABLES = _people_tables()
PEOPLE_KNOWLEDGE = _people_knowledge()


def open_people_fixture(tmp_path: Path, **overrides):
    """``open_fixture`` over the larger fixture of the questions about named people (PEOPLE_TABLES, PEOPLE_KNOWLEDGE)."""
    import datetime as dt

    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    wh = make_warehouse(tmp_path, tables=PEOPLE_TABLES)
    kn = make_knowledge(tmp_path, knowledge=PEOPLE_KNOWLEDGE)
    opts = {"warehouse": str(wh), "today": dt.date(2026, 10, 13)}
    opts.update(overrides)
    return open_records(load_records_config(**opts), str(kn))
