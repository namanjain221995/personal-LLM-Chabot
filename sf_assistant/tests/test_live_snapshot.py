"""Live tool test on the real local snapshot (no LLM). Opt-in: ``OKEVAL_RECORDS_LIVE=1``.

Calls every entry of ``questions/records_golden.json`` with ``RecordTools.call`` and checks the expected
numbers (pinned to the snapshot synced on 2026-10-13). Nothing is printed: results hold record data.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

from sf_assistant.config import DEFAULT_WAREHOUSE, KNOWLEDGE_DIR, PRIVATE_DIR, QUESTIONS_DIR

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

SNAPSHOT = Path(os.environ.get("SFA_LIVE_SNAPSHOT") or DEFAULT_WAREHOUSE)   # opened read-only
_GOLDEN_FILE = QUESTIONS_DIR / "records_golden.json"   # git-ignored (.private/questions)
GOLDEN = json.loads(_GOLDEN_FILE.read_text(encoding="utf-8")) if _GOLDEN_FILE.is_file() else []

pytestmark = pytest.mark.skipif(os.environ.get("OKEVAL_RECORDS_LIVE") != "1" or not SNAPSHOT.is_file()
                                or not _GOLDEN_FILE.is_file(),
                                reason="live record test: set OKEVAL_RECORDS_LIVE=1 with .private/okeval_run/warehouse.duckdb "
                                       "(or SFA_LIVE_SNAPSHOT) and .private/questions/records_golden.json present")


@pytest.fixture(scope="module")
def tools():
    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    t = open_records(load_records_config(warehouse=str(SNAPSHOT), today=dt.date(2026, 10, 13), tz="America/New_York"),
                     str(KNOWLEDGE_DIR))
    newest = t.status().newest_sync_utc or ""
    if not newest.startswith("2026-10-13"):
        t.close()
        pytest.skip("expectations are for the 2026-10-13 snapshot")
    yield t
    t.close()


def _numbers(result: dict) -> list[float]:
    text = json.dumps({k: v for k, v in result.items() if k != "query_id"})
    return [float(x) for x in re.findall(r"(?<![\w.])-?\d+(?:\.\d+)?", text)]


@pytest.mark.parametrize("g", GOLDEN, ids=[f"Q{g['n']}" for g in GOLDEN])
def test_golden(tools, g):
    out = tools.call(g["tool"], g["args"])
    assert out["ok"], out["error"]
    found = _numbers(out["result"])
    have = Counter(x for x in found if x == int(x))
    ints = Counter(float(e) for e in g["expect"] if float(e).is_integer())
    for value, n in ints.items():
        assert have[value] >= n, f"Q{g['n']}: expected {int(value)} x{n}"
    for e in g["expect"]:
        if not float(e).is_integer():
            assert any(abs(x - e) <= g["tol"] for x in found), f"Q{g['n']}: expected {e} ± {g['tol']}"
    for key, value in (g.get("check") or {}).items():
        assert out["result"].get(key) == value, f"Q{g['n']}: {key}"


def test_open_is_fast_and_blocks_the_known_columns(tools):
    from sf_assistant.engine.catalog import BLOCKED_COLUMN_RE, FIELD_SENSITIVITY
    from test_engine_catalog import REAL_BLOCKED, REAL_COLUMNS, REAL_SECRET_METADATA

    if not REAL_COLUMNS.is_file():
        pytest.skip("needs the git-ignored .private/real_columns.json")

    assert tools.catalog.build_seconds < 5
    blocked = {(o.api, f.api) for o in tools.catalog.queryable() for f in o.fields.values() if f.sensitivity == "blocked"}
    for obj, cols in REAL_BLOCKED.items():
        if obj in tools.catalog.objects and tools.catalog.objects[obj].has_table:
            for c in cols:
                if c in tools.catalog.objects[obj].fields:
                    assert ((obj, c) not in blocked) is (c in REAL_SECRET_METADATA.get(obj, ())), (obj, c)
    for obj, col in blocked:
        assert BLOCKED_COLUMN_RE.search(col) or tools.catalog.objects[obj].sensitive or \
            FIELD_SENSITIVITY.get((obj, col), ("",))[0] == "blocked"
    assert not any("question_bank" in c for c in tools.catalog.blocked_columns)


def test_no_readable_text_field_holds_mostly_contact_details(tools):
    """Review F2 of phase 1: a text field whose stored values are mostly e-mail addresses, phone numbers or LinkedIn
    profile links is a contact field (CONTACT_COLUMN_RE or FIELD_SENSITIVITY), whatever its name; a phone-shaped id
    that its field names so (a Zoom meeting id) is no phone number. Field names only in a failure."""
    from sf_assistant.engine.contact import EMAIL_RE, PHONE_CANDIDATE_RE, _is_phone

    id_name = re.compile(r"(?i)(?:meet|mett|webinar)\w*_id|_id(?:_\w+)?__c$|(?:^|_)id$")
    linkedin = re.compile(r"(?i)linkedin\.com/in/")
    shaped = []
    for spec in tools.catalog.queryable():
        for f in spec.fields.values():
            if f.kind not in ("text", "longtext", "name") or f.sensitivity or not f.has_data:
                continue
            st = tools.warehouse.run(f'SELECT DISTINCT CAST(x."{f.api}" AS VARCHAR) FROM main."{spec.api}" AS x '
                                     f'WHERE x."{f.api}" IS NOT NULL LIMIT 3000', [], max_rows=3000, timeout_s=60)
            vals = [str(r[0]).strip() for r in st.rows if r[0] is not None and str(r[0]).strip()]
            if len(vals) < 5:
                continue
            ne = sum(1 for v in vals if len(v) <= 120 and EMAIL_RE.search(v))
            nl = sum(1 for v in vals if linkedin.search(v))
            np_ = 0 if id_name.search(f.api) else sum(1 for v in vals if PHONE_CANDIDATE_RE.fullmatch(v) and _is_phone(v))
            if max(ne, nl, np_) * 2 >= len(vals):
                shaped.append(f"{spec.api}.{f.api}")
    assert shaped == []


# ----------------------------------------------------------------------------------- lookup disambiguation (§21.12)

def test_describe_fields_stays_small_and_lists_lookup_groups(tools):
    """Object and field names only: no record names or counts of people are read here."""
    for obj in ("Interview__c", "Account", "Recruiter__c", "User"):
        out = tools.call("describe_fields", {"object": obj})
        assert out["ok"], out["error"]
        r = out["result"]
        assert len(json.dumps(r)) <= 12_000, obj
        if obj != "User":                     # User has no knowledge file and only system lookups: no group
            assert r.get("lookup_groups"), obj
        spec = tools.catalog.objects[obj]
        undocumented = {f.api for f in spec.fields.values() if f.kind == "reference" and not f.documented and not f.system
                        and f.has_data and f.sensitivity != "blocked"}
        assert undocumented <= {f["field"] for f in r["fields"]}, obj
    acc = tools.call("describe_fields", {"object": "Account"})["result"]
    assert "Assigned_Recruiter__c" in {f["field"] for f in acc["fields"]}


def test_a_not_synced_lookup_written_as_a_path_is_e2b(tools):
    tools.begin_question()
    out = tools.call("count_records", {"object": "Interview_Evaluation__c",
                                       "filters": [{"field": "Evaluator__c.Name", "op": "eq", "value": "x"}]})
    assert out["ok"] is False and tools.evidence[-1].error_code == "E2b"
    assert "is not synced to the warehouse" in out["error"] and "Lookups to Employee that can be queried" in out["error"]


def test_almost_empty_lookup_and_identity_links(tools):
    cat = tools.catalog
    ii = cat.objects["Internal_Interview__c"]
    assert cat.almost_empty(ii, ii.fields["Interview_Support_Person__c"])
    assert "Interviewer__c" in [f.api for f in cat.same_target_lookups(ii, ii.fields["Recruiter__c"])]
    assert {(lk.col, lk.dst) for lk in cat.identity_links("Recruiter__c")} >= {("Recruiter_User__c", "User"),
                                                                             ("Recruiter_Contact__c", "Account")}
    assert any(f.unresolved > 0 for o in cat.queryable() for f in o.fields.values())


def test_golden_questions_get_no_people_hints(tools):
    questions = [ln.strip() for ln in (QUESTIONS_DIR / "records.txt").read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.lstrip().startswith("#")]
    assert len(questions) == 24
    for q in questions:
        tools.begin_question(q)
        h = tools.object_hints(q)
        assert "People named" not in h and "Other lookups" not in h, q


def test_a_bare_type_query_on_client_interviews_reaches_the_round_type_field(tools):
    """Follow-up round 1, Q18: describe_fields(Interview__c, query="type") ranks the object's own type field first and
    puts the field its meaning names for the type of a client round right after it; the incentive-type redirect gets
    no "says 'type'" mark. Field names only."""
    tools.begin_question()
    out = tools.call("describe_fields", {"object": "Interview__c", "query": "type"})
    assert out["ok"], out["error"]
    names = [f["field"] for f in out["result"]["fields"]]
    assert names[0] == "Interview_Type__c" and "Round_Info__c" in names[1:3], names
    notes = out["result"]["notes"]
    own = next(n for n in notes if n.startswith("Interview_Type__c is filled on only"))
    assert "Round_Info__c (filled" in own and "says 'type'" in own
    assert not any(n.startswith("Incentive_Type__c") and "says" in n for n in notes)
    assert out["result"]["fields"][0]["meaning"].startswith("Not the interview type in practice: the type of a client "
                                                            "round is Round_Info__c")


# ------------------------------------------------------------- about_people on the measured person questions (§22.11)
PEOPLE_EXPECTED = PRIVATE_DIR / "okeval_run" / "lookup_eval" / "people" / "expected.json"   # git-ignored: real names


def _people_question(qid: str) -> str:
    if not PEOPLE_EXPECTED.is_file():
        pytest.skip(".private/okeval_run/lookup_eval/people/expected.json is missing (local only)")
    for x in json.loads(PEOPLE_EXPECTED.read_text(encoding="utf-8")).get("questions") or []:
        if x.get("id") == qid:
            return str(x["q"])
    pytest.skip(f"{qid} is not in expected.json")


def _about(tools, qid: str) -> dict:
    """about_people with the names code spots in the question (the names are read at run time, never written here).
    Every assertion of the people tests checks a precomputed boolean and shows only the question id: a failure must
    never print names, labels, profile values or an error text (they are record data)."""
    import time as _time

    q = _people_question(qid)
    names = [d["words"] for d in tools.spot_people(q)]
    has_names = bool(names)
    assert has_names, qid
    tools.begin_question(q)
    t0 = _time.perf_counter()
    out = tools.call("about_people", {"names": names})
    took = round(_time.perf_counter() - t0, 2)
    ok = bool(out["ok"])
    assert ok, (qid, tools.evidence[-1].error_code)
    fast = took <= 1.5
    assert fast, (qid, took)
    from sf_assistant.tools.people import PEOPLE_LINKS_EXTRA_CHARS
    from sf_assistant.tools.record_base import MAX_RESULT_CHARS
    limit = MAX_RESULT_CHARS + (PEOPLE_LINKS_EXTRA_CHARS if len(names) >= 2 else 0)   # two or more names: links' room
    fits = len(json.dumps(out["result"])) <= limit
    assert fits, qid
    return out["result"]


def test_people_identity_p03(tools):
    r = _about(tools, "P03")
    p = r["people"][0]
    who = p["kind"] == "employee" and p.get("no_business_references") is True
    assert who, "P03"
    linked = [m for m in r["matched_records"] if m["person"] == p["ref"] and m["object"] == "User"]
    active = bool(linked) and any("IsActive" in (m.get("profile") or {}) for m in linked)
    assert active, "P03"


# P23: no direct link, but a link through a third person (a shared candidate, §22 review fix)
@pytest.mark.parametrize("qid,linked", [("P23", True), ("P24", True), ("P35", True), ("P38", True)])
def test_people_links_measured_pairs(tools, qid, linked):
    r = _about(tools, qid)
    shape = "links" in r and "links_checked" in r and r.get("partial") is False
    assert shape, qid
    found = bool(r["links"]) is linked
    assert found, (qid, [x["how"] for x in r["links"]])


def test_people_shared_department_manager_or_team_lead_p34(tools):
    r = _about(tools, "P34")
    shared = any(x["how"] == "shared parent" and re.search(r"Department__c|Manager__c|Team_Lead__c", x["path"])
                 for x in r["links"])
    assert shared, ("P34", [x["path"] for x in r["links"]])


# Quick fixes after round 2 (RECORDS_SPEC §22.17): the live interview status and Lead's text status
def test_status_fields_live(tools):
    cat = tools.catalog
    interview = tools._status_field(cat.objects["Interview__c"])
    assert interview is not None and interview.api == "Interview_Status__c" and not interview.legacy
    lead = tools._status_field(cat.objects["Lead"])
    assert lead is not None and lead.api == "Status" and len(tools.status_values(["Lead"])) >= 5
