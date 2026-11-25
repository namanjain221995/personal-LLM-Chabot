"""Business terms enforced by code (business-term contracts of 2026-11-24): candidates are Person Accounts, placement is
an Offer Received client interview, support person performance is the interviews by Interview_Support_Person__c with
their outcome split and offer rate (``engine/term_contracts.py``, ``tools/term_enforce.py``, ``agent/term_notes.py``,
``prompts/term_contracts.md``, ``prompts/business_terms.md``).

The numbered tests follow the build design's test plan (1-40 and the a/b variants). Offline and model-free: the terms
fixture (``tests/terms_fixture.py``, synthetic names, made-up counts in ``TERMS_FACTS``) and a scripted fake of the
model's client. Each test sets the switches its subject depends on; the tool-level tests open the record tools with the
spelling help off unless they compare it on and off.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

pytest.importorskip("duckdb")

import sf_assistant.pipeline as pipeline  # noqa: E402
import terms_fixture as TF  # noqa: E402
import warehouse_fixture as F  # noqa: E402
from sf_assistant import config as C  # noqa: E402
from sf_assistant import prompts as P  # noqa: E402
from sf_assistant.agent import record_prompts as rprompts  # noqa: E402
from sf_assistant.agent import timeline  # noqa: E402
from sf_assistant.agent.llm import LLM  # noqa: E402
from sf_assistant.agent.merge import merge_scope_issues  # noqa: E402
from sf_assistant.agent.nudges import _filters_field  # noqa: E402
from sf_assistant.agent.record_session import RecordSession  # noqa: E402
from sf_assistant.agent.results import RecordAnswer  # noqa: E402
from sf_assistant.agent.term_notes import drop_unapplied_scope_claims, with_term_line  # noqa: E402
from sf_assistant.config import ResolverConfig, load_config, load_records_config  # noqa: E402
from sf_assistant.engine import business_terms as BT  # noqa: E402
from sf_assistant.engine import term_contracts as TC  # noqa: E402
from sf_assistant.engine.types import QueryEvidence  # noqa: E402
from sf_assistant.knowledge.store import knowledge_files  # noqa: E402
from sf_assistant.tools.open_records import open_records  # noqa: E402
from terms_fixture import TERMS_FACTS as FACTS  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")
KNOWLEDGE = ROOT / "knowledge" / "objects"
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."
PERSON = "RecordTypeId = 'Person Account'"
CAND_PERSON = "Candidate__c.RecordTypeId = 'Person Account'"
OFFER = [{"field": "Interview_Outcome__c", "op": "eq", "value": "Offer Received"}]
SUPPORT_GROUP = {"object": "Interview__c", "group_by": "Interview_Support_Person__c"}


def _open(tmp: Path, *, resolver: bool = False, person_type: bool = True):
    tmp.mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV + tuple(C.RESOLVER_ENV_VARS.values()):
            mp.delenv(name, raising=False)
        words = tmp / "words.txt"   # a made-up stand-in for the English word list (generic words only)
        words.write_text("\n".join("how many candidates interviews accounts per month the and of show list count "
                                   "support person performance".split()) + "\n", encoding="utf-8")
        rc = ResolverConfig(enabled=True, lexicon=(str(words),)) if resolver else ResolverConfig(enabled=False)
        return TF.open_terms_fixture(tmp, person_type=person_type, resolver=rc)


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    t = _open(tmp_path_factory.mktemp("terms"))
    yield t
    t.close()


@pytest.fixture(scope="module")
def tools_on(tmp_path_factory):
    t = _open(tmp_path_factory.mktemp("terms_on"), resolver=True)
    assert t.catalog.resolver is not None, t.resolver_lines
    yield t
    t.close()


def run(tools, question, name, args):
    """``begin_question(question)`` then one call: (result or None, error text or None, the call's evidence)."""
    tools.begin_question(question)
    out = tools.call(name, args)
    return out["result"], out["error"], tools.evidence[-1]


def ok(tools, question, name, args):
    res, err, ev = run(tools, question, name, args)
    assert err is None, err
    return res, ev


def refused(tools, question, name, args, code):
    res, err, ev = run(tools, question, name, args)
    assert res is None and ev.error_code == code, (err, ev.error_code)
    return err


def added(res) -> list[str]:
    return list((res.get("applied") or {}).get("term_filters") or [])


# ------------------------------------------------------------------------------------------------ reading the question
def keys(question, names=()):
    return set(TC.detect(question, names).keys)


def test_01_candidate_words_name_the_term():
    assert keys("how many candidates joined in October") == {"candidates"}
    assert "candidates" in keys("list the students of the batch") and "candidates" in keys("our job seekers")


@pytest.mark.parametrize("word", ["candiates", "canditates", "condidates", "cadidates", "candidtes", "studnets",
                                  "consultnts", "trainess", "applicnts"])
def test_02_misspellings_name_the_term(word):
    assert "candidates" in keys(f"how many {word} joined this month")


@pytest.mark.parametrize("question", ["kitne candidates ka interview hua", "is mahine kitne ummeedwar aaye",
                                      "kitne bacche placed hue"])
def test_03_hinglish_names_the_term(question):
    assert "candidates" in keys(question)


@pytest.mark.parametrize("question", ["how many accounts", "consultancy accounts", "candid feedback",
                                      "consulting revenue", "how many people work here"])
def test_04_other_words_name_no_term(question):
    assert "candidates" not in keys(question)


def test_05_overrides():
    names = ["B2B Client", "B2B_Client", "Recruiter", "Master"]
    # "client companies" are Company__c records, not Accounts: no override (v2, critique 2)
    t = TC.detect("candidates and client companies", names)
    assert not t.other_accounts and not t.all_types
    assert TC.detect("candidates including recruiter accounts", names).all_types
    assert TC.detect("all account types", names).all_types
    assert TC.detect("accounts of the Master record type", names).other_accounts
    assert TC.detect("how many B2B client accounts", names).other_accounts
    t = TC.detect("candidates per recruiter", names)
    assert not t.other_accounts and not t.all_types


@pytest.mark.parametrize("question, on", [
    ("support person performance", True), ("suport persn performance", True),
    ("support team ka performance kaisa hai", True), ("interview support wale", True),
    ("support tickets", False), ("customer support cases", False), ("support log", False)])
def test_06_support_words(question, on):
    assert ("support_performance" in keys(question)) is on


def test_06_session_and_performance_words():
    t = TC.detect("support sessions hosted")
    assert t.sessions and "support_performance" not in t.keys
    assert not TC.detect("top support persons by interviews").performance   # "top" alone is no performance word
    assert TC.detect("best support person by offer rate").performance


@pytest.mark.parametrize("question", ["how many trainers", "trainer list", "who trained them", "training sessions"])
def test_22i_trainers_are_no_candidates(question):
    assert "candidates" not in keys(question)


def test_22i_person_accounts_is_a_literal_count(tools):
    assert "candidates" not in keys("how many person accounts")
    res, _ev = ok(tools, "how many person accounts", "count_records",
                  {"object": "Account", "filters": [{"field": "IsPersonAccount", "op": "eq", "value": True}]})
    assert res["count"] == FACTS["flag_true"] and not added(res)


def test_22j_follow_up_and_raw_mcp_calls_are_unenforced(tools):
    """Accepted gaps (critique 21): a follow-up whose rewrite lost the word, and raw MCP calls (no question)."""
    assert TC.detect("and per month?").keys == frozenset()
    res, ev = ok(tools, "", "count_records", {"object": "Account"})
    assert res["count"] == FACTS["accounts"] and not added(res) and ev.enforced_arguments is None


# ------------------------------------------------------------------------------------------------ candidates
def test_07_account_count_gets_the_record_type(tools):
    res, ev = ok(tools, "how many candidates joined in October", "count_records", {"object": "Account"})
    assert res["count"] == FACTS["person_record_type"]
    sql = ev.statements[0].sql
    assert '"RecordTypeId" = ANY' in sql and "IsPersonAccount" not in sql   # one filter item (v2, critique 20)
    assert res["applied"]["filters"] == [PERSON] and added(res) == [PERSON]
    assert TC.text("candidates_note", FILTER=PERSON) in res["notes"]
    assert not any(n.startswith("includes all record types") for n in res["notes"])
    assert ev.arguments == {"object": "Account"}   # the model's arguments stay; what ran is beside them
    assert ev.enforced_arguments == {"object": "Account", "filters": [TC.CANDIDATE_PREDICATE]}


def test_08_without_the_word_nothing_changes(tools):
    res, ev = ok(tools, "how many accounts", "count_records", {"object": "Account"})
    assert res["count"] == FACTS["accounts"] and "applied" not in res and ev.enforced_arguments is None
    assert any(n.startswith("includes all record types") for n in res["notes"])


def test_09_a_question_about_other_accounts_keeps_its_filter(tools):
    args = {"object": "Account", "filters": [{"field": "RecordTypeId", "op": "eq", "value": "B2B_Client"}]}
    res, _ev = ok(tools, "how many B2B client accounts", "count_records", args)
    assert res["count"] == 1 and res["applied"]["filters"] == ["RecordTypeId = 'B2B Client'"] and not added(res)


def test_10_a_conflicting_record_type_is_refused(tools):
    args = {"object": "Account", "filters": [{"field": "RecordTypeId", "op": "eq", "value": "Recruiter"}]}
    err = refused(tools, "how many candidates", "count_records", args, "T1")
    assert err.startswith("Candidates are Person Accounts (owner's definition)") and "RecordTypeId = 'Recruiter'" in err
    args = {"object": "Account", "filters": [{"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"}]}
    res, ev = ok(tools, "how many candidates", "count_records", args)
    assert res["applied"]["filters"] == [PERSON] and not added(res) and ev.enforced_arguments is None


def test_11_distinct_candidates_of_interviews(tools):
    q = "how many candidates had interviews"
    res, ev = ok(tools, q, "count_records", {"object": "Interview__c", "count_distinct": "Candidate__c"})
    assert res["count"] == FACTS["interview_candidates_person"] and added(res) == [CAND_PERSON]
    assert 'LEFT JOIN main."Account" AS t1 ON t1."Id" = t0."Candidate__c"' in ev.statements[0].sql
    res, _ev = ok(tools, "", "count_records", {"object": "Interview__c", "count_distinct": "Candidate__c"})
    assert res["count"] == FACTS["interview_candidates_all"]   # the Recruiter-type account is one of them
    res, ev = ok(tools, q, "count_records", {"object": "Interview__c"})   # interviews are counted: not scoped
    assert res["count"] == FACTS["interviews"] and not added(res) and ev.enforced_arguments is None


def test_12_grouped_by_candidate(tools):
    res, _ev = ok(tools, "interviews per candidate", "group_records", {"object": "Interview__c", "group_by": "Candidate__c"})
    assert added(res) == [CAND_PERSON]
    groups = {g["group"]: g["value"] for g in res["groups"]}
    assert "(blank)" not in groups and "Recruiter A" not in groups
    assert sum(groups.values()) == FACTS["interviews_of_person_candidates"]


def test_13_only_the_candidate_lookup_is_scoped(tools):
    args = {"object": "Interview__c", "group_by": "Client_Account__c", "measure": "count_distinct",
            "measure_field": "Candidate__c"}
    res, _ev = ok(tools, "how many candidates per client account", "group_records", args)
    assert added(res) == [CAND_PERSON] and res["applied"]["filters"] == [CAND_PERSON]
    plain = {"object": "Interview__c", "group_by": "Client_Account__c"}
    res, _ev = ok(tools, "interviews of candidates per client account", "group_records", plain)
    assert not added(res)   # Client_Account__c is a lookup to Account that never holds the candidate


def test_14_lists(tools):
    res, _ev = ok(tools, "list candidates who joined this month", "list_records", {"object": "Account"})
    assert added(res) == [PERSON]
    args = {"object": "Interview__c", "fields": ["Name", "Candidate__c"]}
    res, _ev = ok(tools, "list the interviews of candidates", "list_records", args)
    assert added(res) == [CAND_PERSON] and res["total_count"] == FACTS["interviews_of_person_candidates"]


def test_15_a_filter_through_the_candidate(tools):
    args = {"object": "Interview__c", "filters": [{"field": "Candidate__c.Candidate_Status__c", "op": "eq",
                                                   "value": "Active"}]}
    res, _ev = ok(tools, "interviews of active candidates", "count_records", args)
    assert added(res) == [CAND_PERSON] and res["applied"]["filters"][-1] == CAND_PERSON


def test_16_a_named_candidate_is_not_scoped(tools):
    args = {"object": "Interview__c", "filters": [{"field": "Candidate__c", "op": "eq", "value": "Candidate 1"}]}
    res, ev = ok(tools, "interviews of candidate Candidate 1", "count_records", args)
    assert not added(res) and ev.enforced_arguments is None


def test_17_every_counting_tool_on_account(tools):
    q = "what share of candidates are active"
    res, _ev = ok(tools, q, "percentage_of_records", {"object": "Account", "part_filters": [
        {"field": "Candidate_Status__c", "op": "eq", "value": "Active"}]})
    assert res["applied"]["base_filters"] == [PERSON] and added(res) == [PERSON] and res["base"] == 5
    res, _ev = ok(tools, "new candidates per month", "trend_records",
                  {"object": "Account", "grain": "month", "date_field": "CreatedDate",
                   "period": "2024-12-01 to 2025-01-31"})
    assert added(res) == [PERSON] and res["total"] == FACTS["person_record_type"]
    res, _ev = ok(tools, "candidates with vs without a recruiter", "compare_records", {"object": "Account", "segments": [
        {"label": "with", "filters": [{"field": "Assigned_Recruiter__c", "op": "is_not_blank"}]},
        {"label": "without", "filters": [{"field": "Assigned_Recruiter__c", "op": "is_blank"}]}]})
    assert added(res) == [PERSON] and sum(s["value"] for s in res["segments"]) == FACTS["person_record_type"]
    res, _ev = ok(tools, "when was the first candidate created", "aggregate_records",
                  {"object": "Account", "op": "min", "field": "CreatedDate"})
    assert added(res) == [PERSON] and res["records_matched"] == FACTS["person_record_type"]
    res, _ev = ok(tools, "candidates with no interviews", "count_records", {"object": "Account", "related": [
        {"child_object": "Interview__c", "via_field": "Candidate__c", "exists": False}]})
    assert added(res) == [PERSON] and res["count"] == FACTS["person_record_type"] - 2


def test_18_other_objects_are_unchanged(tools):
    res, ev = ok(tools, "candidates per recruiter", "group_records", {"object": "Interview__c", "group_by": "Recruiter__c"})
    assert not added(res) and ev.enforced_arguments is None
    for obj in ("Recruiter__c", "Session__c"):
        res, ev = ok(tools, "how many candidates", "count_records", {"object": obj})
        assert not added(res) and ev.enforced_arguments is None


def test_19_no_question_no_change(tools):
    res, _ev = ok(tools, "", "count_records", {"object": "Account"})
    assert res["count"] == FACTS["accounts"] and not added(res)


def test_20_the_spelling_help_changes_nothing(tools, tools_on):
    for t in (tools, tools_on):
        res, _ev = ok(t, "interviews per candidate", "group_records", {"object": "Interview__c", "group_by": "Candidate__c"})
        assert res["applied"] == {"filters": [CAND_PERSON], "term_filters": [CAND_PERSON]}
        res, _ev = ok(t, "how many candidates", "count_records", {"object": "Account"})
        assert res["applied"] == {"filters": [PERSON], "term_filters": [PERSON]}


def test_21_base_fixture_and_the_flag_fallback(tmp_path):
    base = F.open_fixture(tmp_path / "base")   # no IsPersonAccount column
    try:
        res, _ev = ok(base, "how many candidates", "count_records", {"object": "Account"})
        assert res["applied"]["filters"] == [PERSON] and res["count"] == F.FACTS["person_accounts"]
    finally:
        base.close()
    flag = _open(tmp_path / "flag", person_type=False)   # the record type cannot be read: the flag, with its note
    try:
        res, _ev = ok(flag, "how many candidates", "count_records", {"object": "Account"})
        assert res["applied"]["filters"] == ["IsPersonAccount = true"] and res["count"] == FACTS["flag_true"]
        assert TC.text("candidates_flag_note", FILTER="IsPersonAccount = true") in res["notes"]
    finally:
        flag.close()


@pytest.mark.skipif(not (KNOWLEDGE / "Account.json").is_file(), reason="the knowledge files are git-ignored")
def test_22_candidate_lookups_of_the_knowledge():
    """The candidate lookup rule over the knowledge files: 19 lookups by name, the 7 other lookups to Account left
    out, and the .raw duplicate never loaded (critique 7)."""
    files = knowledge_files(str(KNOWLEDGE))
    assert (KNOWLEDGE / "Internal_Interview__c.raw.json").is_file() and not any(f.endswith(".raw.json") for f in files)
    found, others = set(), set()
    for path in files:
        k = json.loads(Path(path).read_text(encoding="utf-8"))
        obj = k["object"]["api_name"]
        for f in k.get("fields") or []:
            ref = f.get("reference") or {}
            targets = ref.get("target_objects") or []
            if targets != ["Account"]:
                continue
            step = TC.Step(obj, f["api_name"], "Account", str(ref.get("business_role") or ""))
            (found if TC.is_candidate_lookup(step) else others).add(f"{obj}.{f['api_name']}")
    names = ("Application__c", "Availability__c", "Background_Check__c", "CT_Session_Request__c",
             "Candidate_Portal_Credential__c", "Candidate_Training__c", "Education_History__c", "Internal_Interview__c",
             "Interview_Attendee__c", "Interview__c", "Invoice__c", "Marketing__c", "Pre_Enrolment_Request__c",
             "Resume__c", "Session_Attendee__c", "Session__c", "Training_Mock_Result__c")
    assert found == {f"{o}.Candidate__c" for o in names} | {"Quality_Finding__c.Related_Candidate__c",
                                                             "Onboarding__c.Account__c"}
    assert others == {"Interview__c.Client_Account__c", "Account.ParentId", "Account.Candidate_Referral__c",
                      "Recruiter__c.Recruiter_Contact__c", "Case.AccountId", "Opportunity.AccountId",
                      "Contact.AccountId"}


def test_22a_mixed_question_is_decided_per_call(tools):
    q = "how many candidates and how many B2B client accounts"
    res, _ev = ok(tools, q, "count_records", {"object": "Account"})
    assert res["count"] == FACTS["person_record_type"] and added(res) == [PERSON]
    args = {"object": "Account", "filters": [{"field": "RecordTypeId", "op": "eq", "value": "B2B Client"}]}
    res, _ev = ok(tools, q, "count_records", args)
    assert res["count"] == 1 and not added(res)


def test_22b_client_companies_are_no_override(tools):
    res, _ev = ok(tools, "candidates interviewing with client companies", "count_records", {"object": "Account"})
    assert added(res) == [PERSON]


def test_22c_record_type_contrasts(tools):
    res, _ev = ok(tools, "what share of accounts are candidates", "percentage_of_records", {"object": "Account",
                  "part_filters": [{"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"}]})
    assert res["part"] == FACTS["person_record_type"] and res["base"] == FACTS["accounts"] and not added(res)
    res, _ev = ok(tools, "candidates and the other accounts by record type", "group_records",
                  {"object": "Account", "group_by": "RecordTypeId"})
    assert not added(res) and res["group_count"] == 4
    res, err, ev = run(tools, "candidates vs recruiter accounts", "compare_records", {"object": "Account", "segments": [
        {"label": "candidates", "filters": [{"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"}]},
        {"label": "recruiter accounts", "filters": [{"field": "RecordTypeId", "op": "eq", "value": "Recruiter"}]}]})
    assert err is None and not added(res) and [s["value"] for s in res["segments"]] == [5, 2]


def test_22d_lead_and_contact(tools):
    err = refused(tools, "how many candidates", "count_records", {"object": "Lead"}, "T1")
    assert "Lead records are website applicants before conversion" in err
    refused(tools, "how many candidates", "count_records", {"object": "Contact"}, "T1")
    res, _ev = ok(tools, "how many leads", "count_records", {"object": "Lead"})
    assert res["count"] == 4
    refused(tools, "interviews per candidate", "group_records", {"object": "Interview__c", "group_by": "Lead__c"}, "T1")
    res, _ev = ok(tools, "interviews of candidates that came from a lead", "count_records", {
        "object": "Interview__c", "filters": [{"field": "Lead__c", "op": "is_not_blank"}]})
    assert res["count"] == 1


def test_22e_a_named_person_wins(tools):
    args = {"object": "Interview__c", "fields": ["Name", "Candidate__c"],
            "filters": [{"field": "Candidate__c", "op": "eq", "value": "Recruiter A"}]}
    res, _ev = ok(tools, "list the interviews of candidate Recruiter A", "list_records", args)
    assert not added(res) and res["total_count"] == 1   # a named non-candidate: its own records, never 0
    for flt in ({"field": "Name", "op": "eq", "value": "Recruiter A"}, {"field": "Id", "op": "in", "values": [F.ACC_A]}):
        res, _ev = ok(tools, "is Recruiter A a candidate", "count_records", {"object": "Account", "filters": [flt]})
        assert not added(res) and res["count"] == 1


@pytest.mark.parametrize("flt", [
    {"field": "RecordTypeId.DeveloperName", "op": "eq", "value": "PersonAccount"},
    {"field": "RecordTypeId", "op": "eq", "value": "Person Account"},
    {"field": "RecordTypeId", "op": "in", "values": ["PersonAccount"]}])
def test_22f_the_same_filter_in_another_form_is_not_added_twice(tools, flt):
    res, ev = ok(tools, "how many candidates", "count_records", {"object": "Account", "filters": [flt]})
    assert not added(res) and len(res["applied"]["filters"]) == 1 and ev.enforced_arguments is None


def test_22f_a_negated_person_type_is_refused(tools):
    args = {"object": "Account", "filters": [{"field": "RecordTypeId", "op": "ne", "value": "PersonAccount"}]}
    refused(tools, "how many candidates", "count_records", args, "T1")
    args = {"object": "Account", "filters": [{"field": "RecordTypeId", "op": "ne", "value": "B2B_Client"}]}
    res, _ev = ok(tools, "how many candidates", "count_records", args)   # neither: the candidate filter is added
    assert added(res) == [PERSON] and res["count"] == FACTS["person_record_type"]


def test_22g_or_groups(tools):
    mixed = {"object": "Interview__c", "any_of": [
        {"field": "Candidate__c.Candidate_Status__c", "op": "eq", "value": "Active"},
        {"field": "Interview_Status__c", "op": "eq", "value": "Cancelled"}]}
    res, _ev = ok(tools, "interviews of active candidates or cancelled", "count_records", mixed)
    assert not added(res) and TC.text("candidates_any_of", FIELD="Candidate__c") in res["notes"]
    same = {"object": "Interview__c", "any_of": [
        {"field": "Candidate__c.Candidate_Status__c", "op": "eq", "value": "Active"},
        {"field": "Candidate__c.Assigned_Recruiter__c", "op": "is_not_blank"}]}
    res, _ev = ok(tools, "interviews of active or assigned candidates", "count_records", same)
    assert added(res) == [CAND_PERSON]


def test_22h_the_join_cap_leaves_the_filter_out_with_a_note(tools):
    args = {"object": "Interview__c", "count_distinct": "Candidate__c", "filters": [
        {"field": "Recruiter__c.Department__c.Name", "op": "is_not_blank"},
        {"field": "OwnerId.Name", "op": "is_not_blank"},
        {"field": "RecordTypeId.DeveloperName", "op": "is_not_blank"},
        {"field": "Preferred_Support_Person__c.Name", "op": "is_not_blank"}]}
    res, ev = ok(tools, "", "count_records", args)   # the call alone is at the cap
    assert res["count"] >= 0
    res, ev = ok(tools, "how many candidates had such interviews", "count_records", args)
    assert not added(res) and TC.text("candidates_unapplied", FIELD="Candidate__c") in res["notes"]
    assert not any(n == TC.text("candidates_note", FILTER=CAND_PERSON) for n in res["notes"])
    assert ev.error_code is None and CAND_PERSON not in res["applied"]["filters"]


# ------------------------------------------------------------------------------------------------ placement
def test_23_placed_status_is_refused(tools):
    args = {"object": "Account", "filters": [{"field": "Candidate_Status__c", "op": "eq", "value": "Placed"}]}
    err = refused(tools, "how many candidates were placed", "count_records", args, "T2")
    assert "Interview_Outcome__c is 'Offer Received'" in err and '"value": "Offer Received"' in err


@pytest.mark.parametrize("question, values", [
    ("how many candidates have candidate status Placed", ["Placed"]),
    ("how many candidates with status Placed", ["Placed"]),
    ("how many candidates are in placed status", ["Placed"]),
    ("how many active or placed candidates", ["Active", "Placed"])])
def test_24_a_status_the_question_names_is_allowed(tools, question, values):
    assert "placement" in keys(question)
    flt = ({"field": "Candidate_Status__c", "op": "eq", "value": values[0]} if len(values) == 1 else
           {"field": "Candidate_Status__c", "op": "in", "values": values})
    res, _ev = ok(tools, question, "count_records", {"object": "Account", "filters": [flt]})
    assert added(res) == [PERSON] and res["count"] == (FACTS["placed_status_accounts"] if len(values) == 1 else 3)


def test_25_onboarding(tools):
    refused(tools, "placement rate this year", "count_records", {"object": "Onboarding__c"}, "T2")
    res, _ev = ok(tools, "onboarding completed for placed candidates", "count_records", {
        "object": "Onboarding__c", "filters": [{"field": "Status__c", "op": "eq", "value": "Completed"}]})
    assert res["count"] == 2


def test_26_placed_status_through_the_candidate(tools):
    args = {"object": "Interview__c", "filters": [{"field": "Candidate__c.Candidate_Status__c", "op": "eq",
                                                   "value": "Placed"}]}
    refused(tools, "how many interviews of placed candidates", "count_records", args, "T2")


def test_27_the_defined_call_runs_and_is_scoped(tools):
    args = {"object": "Interview__c", "filters": OFFER, "count_distinct": "Candidate__c"}
    res, _ev = ok(tools, "how many candidates were placed", "count_records", args)
    assert res["count"] == 1 and added(res) == [CAND_PERSON]


def test_27a_related_onboarding_and_status_breakdowns(tools):
    args = {"object": "Account", "related": [{"child_object": "Onboarding__c", "exists": True}]}
    refused(tools, "how many candidates were placed", "count_records", args, "T2")
    note = TC.text("placement_status_note")
    res, _ev = ok(tools, "how many candidates were placed, by status", "group_records",
                  {"object": "Account", "group_by": "Candidate_Status__c"})
    assert note in res["notes"] and added(res) == [PERSON]
    res, _ev = ok(tools, "how many candidates were placed", "get_field_values",
                  {"object": "Account", "field": "Candidate_Status__c"})
    assert note in res["notes"]


# ------------------------------------------------------------------------------------------------ support persons
def test_28_session_hosts_are_refused_with_the_call(tools):
    args = {"object": "Session__c", "group_by": "Host_User__c",
            "filters": [{"field": "Session_Role__c", "op": "eq", "value": "Support"}]}
    err = refused(tools, "support person performance", "group_records", args, "T3")
    assert '"group_by": "Interview_Support_Person__c"' in err and '"split_by": "Interview_Outcome__c"' in err


def test_29_session_words_allow_sessions(tools):
    res, _ev = ok(tools, "who hosted support sessions", "group_records", {"object": "Session__c", "group_by": "Host_User__c"})
    assert res["group_count"] >= 1


def test_30_support_performance_is_completed_with_the_offer_rate(tools):
    res, ev = ok(tools, "support person performance this year", "group_records", dict(SUPPORT_GROUP))
    assert ev.enforced_arguments == {**SUPPORT_GROUP, "split_by": "Interview_Outcome__c"}
    got = {g["group"]: (g["value"], g["offers"], g["offer_rate_pct"]) for g in res["groups"]}
    assert got == FACTS["support"]
    assert all("by" in g for g in res["groups"]) and res["offer_rate_overall_pct"] == FACTS["offer_rate_overall"]
    assert TC.text("support_note") in res["notes"]
    fixed = {**SUPPORT_GROUP, "filters": OFFER}   # the outcome is fixed: a rate would be 100%
    res, ev = ok(tools, "support person performance", "group_records", fixed)
    assert ev.enforced_arguments is None and "offer_rate_overall_pct" not in res
    res, ev = ok(tools, "top support persons by interviews", "group_records", dict(SUPPORT_GROUP))
    assert ev.enforced_arguments is None and "offers" not in res["groups"][0]


def test_30a_other_session_and_support_fields_are_refused(tools):
    q = "support person performance"
    refused(tools, q, "count_records", {"object": "Recruiter__c", "related": [
        {"child_object": "Session__c", "via_field": "Host_User__c", "exists": True}]}, "T3")
    refused(tools, q, "count_records", {"object": "Session_Attendee__c", "filters": [
        {"field": "Session__c.Session_Role__c", "op": "eq", "value": "Support"}]}, "T3")
    refused(tools, q, "group_records", {"object": "Session__c", "group_by": "Additional_Attendee_1__c"}, "T3")
    refused(tools, q, "group_records", {"object": "Interview__c", "group_by": "Preferred_Support_Person__c"}, "T3")
    refused(tools, q, "group_records", {"object": "Internal_Interview__c", "group_by": "Interview_Support_Person__c"}, "T3")
    assert "support_performance" not in keys("support log")
    view = NS(object=lambda name: name, path=lambda obj, text: TC.PathInfo(
        text, (TC.Step(obj, text, "Recruiter__c", "tl_preferred_support_person"),), text))
    p = TC.plan(TC.detect(q), "group_records", {"object": "Interview__c", "group_by": "varPreferred_support_person__c"},
                view)
    assert p.reject and p.reject[0] == "T3"


def test_31_counting_support_is_no_performance(tools):
    q = "how many interviews did each support person support"
    res, ev = ok(tools, q, "group_records", dict(SUPPORT_GROUP))
    assert ev.enforced_arguments is None and "offers" not in res["groups"][0]
    refused(tools, q, "group_records", {"object": "Session__c", "group_by": "Host_User__c"}, "T3")


def test_32_preferred_named_in_the_question(tools):
    args = {"object": "Interview__c", "group_by": "Preferred_Support_Person__c"}
    refused(tools, "support person performance", "group_records", args, "T3")
    res, _ev = ok(tools, "support person performance by preferred support person", "group_records", args)
    assert res["group_count"] == 1
    res, _ev = ok(tools, "support persons of mock interviews", "group_records",
                  {"object": "Internal_Interview__c", "group_by": "Interview_Support_Person__c"})
    assert res["group_count"] == 1


def test_33_no_rate_on_another_split(tools):
    res, _ev = ok(tools, "support person performance", "group_records", {**SUPPORT_GROUP, "split_by": "Lead__c"})
    assert "offer_rate_overall_pct" not in res and "offers" not in res["groups"][0]


def test_33_offer_rates_guard():
    groups = [{"group": "A", "value": 4, "by": {"Ghosted": 2, "(other)": 2}}]
    assert TC.offer_rates(groups, {"Ghosted": 2, "(other)": 2}) is False and "offers" not in groups[0]
    groups = [{"group": "A", "value": 3, "by": {"Offer Received": 1, "Ghosted": 2}}]
    assert TC.offer_rates(groups, {"Offer Received": 1, "Ghosted": 2}) == 33.3 and groups[0]["offer_rate_pct"] == 33.3


# ------------------------------------------------------------------------------------------------ the answer's notes
def _ev(applied=None, tool="count_records", ok_=True, obj="Account", result=None, **kw) -> QueryEvidence:
    return QueryEvidence(query_id="q1", tool=tool, kind="data", object=obj, arguments={"object": obj}, ok=ok_,
                         error=None, applied=dict(applied or {}), notes=[], statements=[], tables=[obj],
                         synced_at={obj: "2026-10-13 14:30 UTC"}, summary="", model_result=result or {"count": 9},
                         elapsed_ms=1.0, **kw)


def test_34_an_unapplied_scope_claim_is_cut_as_a_clause():
    ev, q = [_ev()], "how many candidates"
    text, removed = drop_unapplied_scope_claims("There are 1,234 candidates (Person Accounts only).\n" + SOURCE, ev, q)
    assert text == "There are 1,234 candidates.\n" + SOURCE and removed == ["Person Accounts only"]
    text, removed = drop_unapplied_scope_claims("Counted `Account` records, filtered to Person Accounts only: 9.", ev, q)
    assert text == "Counted `Account` records: 9." and removed
    text, _r = drop_unapplied_scope_claims("9 accounts (Person Accounts only, created this year).", ev, q)
    assert text == "9 accounts (created this year)."
    text, _r = drop_unapplied_scope_claims("9 accounts.\n- Person Accounts only\n" + SOURCE, ev, q)
    assert text == "9 accounts.\n" + SOURCE
    text, _r = drop_unapplied_scope_claims("9 accounts where `IsPersonAccount` = true.", ev, q)
    assert text == "9 accounts."
    text, _r = drop_unapplied_scope_claims("There are 9 candidates, Person Accounts only.", ev, q)
    assert text == "There are 9 candidates."
    backed = [_ev({"filters": [PERSON], "term_filters": [PERSON]})]
    same = "There are 5 candidates (Person Accounts only, `RecordTypeId` = 'Person Account')."
    assert drop_unapplied_scope_claims(same, backed) == (same, [])


def test_34_the_guard_never_empties_an_answer():
    answer = "Counted accounts where RecordTypeId = 'Batch 7'.\n" + SOURCE   # its only number is inside the claim
    text, removed = drop_unapplied_scope_claims(answer, [_ev()], "how many candidates")
    assert text == answer and removed == ["RecordTypeId = 'Batch 7'"]


def test_34_the_term_line():
    backed = [_ev({"filters": [CAND_PERSON], "term_filters": [CAND_PERSON]}, obj="Interview__c")]
    line = "Candidates counted: Person Accounts only (`Candidate__c.RecordTypeId = 'Person Account'`)."
    assert with_term_line("2 candidates had interviews.\n" + SOURCE, backed) == (
        "2 candidates had interviews.\n\n" + line + "\n" + SOURCE)
    assert with_term_line("2 candidates had interviews.", backed) == "2 candidates had interviews.\n\n" + line
    said = "2 candidates (Person Accounts) had interviews.\n" + SOURCE
    assert with_term_line(said, backed) == said and with_term_line("x", [_ev()]) == "x" and with_term_line("", backed) == ""


def test_34_merge_checks_the_person_account_claim():
    rec = RecordAnswer(question="q", answer="There are 9 accounts.\n" + SOURCE, no_records=False, used_data=True,
                       evidence=[_ev()])
    assert any("Person Accounts only" in i for i in merge_scope_issues("There are 9 accounts (Person Accounts only).",
                                                                        rec))
    assert any("IsPersonAccount" in i for i in merge_scope_issues("9 accounts with `IsPersonAccount` = true.", rec))
    rec.evidence = [_ev({"filters": [PERSON]}, fields_used=[("Account", "RecordTypeId")])]
    assert merge_scope_issues("There are 5 candidates (Person Accounts only).", rec) == []


def test_39_the_nudges_read_what_ran():
    e = _ev(obj="Interview__c")
    e.arguments = {"object": "Interview__c"}
    assert not _filters_field(e, "Candidate__c.RecordTypeId")
    e.enforced_arguments = {"object": "Interview__c", "filters": [{"field": "Candidate__c.RecordTypeId", "op": "eq",
                                                                    "value": "PersonAccount"}]}
    assert _filters_field(e, "Candidate__c.RecordTypeId")


# ------------------------------------------------------------------------------------------------ through the session
def _completion(content=None, calls=()):
    tcs = [NS(id=f"c{i}", function=NS(name=n, arguments=json.dumps(a))) for i, (n, a) in enumerate(calls)]
    msg = NS(content=content, tool_calls=tcs or None)
    return NS(choices=[NS(message=msg, finish_reason="tool_calls" if tcs else "stop")],
              usage=NS(prompt_tokens=100, completion_tokens=10))


class Model:
    """A fake OpenAI client: the route and names calls, the record agent's steps in order (a dict is one tool call, a
    list several, a str a reply; the last one repeats), the merge call and the knowledge steps."""

    def __init__(self, *steps, merged="", knowledge="`Account` holds the accounts."):
        self.steps, self.merged, self.knowledge = steps, merged, knowledge
        self.requests: dict[str, list] = {"record": [], "other": []}
        self._lock = threading.Lock()
        self.chat = NS(completions=NS(create=self._create))

    def _create(self, **kwargs):
        system = str(kwargs["messages"][0].get("content") or "")
        if system == rprompts.ROUTE_SYSTEM:
            return _completion("RECORDS")
        if system == rprompts.NAMES_SYSTEM:
            return _completion("NONE")
        if system.startswith(rprompts.COMBINE_SYSTEM):
            return _completion(self.merged)
        if system.startswith("You answer questions about Salesforce RECORDS"):
            with self._lock:
                n = len(self.requests["record"])
                self.requests["record"].append(kwargs)
            if not kwargs.get("tools"):
                return _completion("")
            step = self.steps[min(n, len(self.steps) - 1)]
            if isinstance(step, dict):
                return _completion(calls=[(step["name"], step["args"])])
            if isinstance(step, list):
                return _completion(calls=[(s["name"], s["args"]) for s in step])
            return _completion(step)
        if kwargs.get("tools") and not any(m.get("role") == "tool" for m in kwargs["messages"]):
            return _completion(calls=[("search_all_knowledge", {"query": "accounts"})])
        return _completion(self.knowledge)


def _cfg():
    return load_config(base_url="http://127.0.0.1:9/v1", api_key="test-key")   # never reached: the client is a fake


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("terms_world")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        mp.setenv("SFA_RESOLVER", "off")
        wh = F.make_warehouse(tmp, tables=TF.TERMS_TABLES)
        kn = F.make_knowledge(tmp, knowledge=TF.TERMS_KNOWLEDGE)
        settings = C.load_settings(records=False, components_dir=str(tmp / "none"))
        _c, _n, registry, system, _llm = pipeline.build(knowledge_files(str(kn)), settings)
        t = open_records(load_records_config(warehouse=str(wh), today=dt.date(2026, 10, 13)), str(kn),
                         ResolverConfig(enabled=False))
    status = t.status()
    t.status = lambda: status
    yield NS(tools=t, registry=registry, system=system)
    t.close()


@pytest.fixture(autouse=True)
def _switches(monkeypatch):
    monkeypatch.setenv("SFA_CHARTS", "on")
    monkeypatch.setenv("SFA_TABLES", "model")
    monkeypatch.setenv("SFA_MERGE", "code")
    monkeypatch.setenv("SFA_RESOLVER", "off")
    timeline.end()
    yield
    timeline.end()


def ask(world, model, question) -> RecordAnswer:
    cfg = _cfg()
    return RecordSession(world.tools, cfg, llm=LLM(cfg, client=model)).ask(question)


def answer(world, model, question):
    cfg = _cfg()
    llm = LLM(cfg, client=model)
    session = RecordSession(world.tools, cfg, llm=llm)
    return pipeline.answer(question, cfg, world.registry, world.system, llm, 1, session)


LINE = "Candidates counted: Person Accounts only (`RecordTypeId = 'Person Account'`)."
COUNT_ACCOUNT = {"name": "count_records", "args": {"object": "Account"}}


def test_34_the_session_cuts_the_claim_and_keeps_the_number(world):
    model = Model({"name": "count_records", "args": {"object": "Interview__c"}},
                  "There are 13 client interviews (Person Accounts only).\n" + SOURCE)
    rec = ask(world, model, "how many interviews did candidates have")   # interviews are counted: not scoped
    assert rec.answer == "There are 13 client interviews.\n" + SOURCE and rec.dropped_claims == ["Person Accounts only"]
    assert not rec.no_records


@pytest.mark.parametrize("tables", ["model", "code"])
def test_35_the_code_line_says_the_candidate_filter_once(world, monkeypatch, tables):
    monkeypatch.setenv("SFA_TABLES", tables)
    model = Model(COUNT_ACCOUNT, "There are 5 candidates in `Account`.\n" + SOURCE)
    rec = ask(world, model, "how many candidates are there")
    assert rec.answer == "There are 5 candidates in `Account`.\n\n" + LINE + "\n" + SOURCE
    assert rec.answer.count("Candidates counted:") == 1 and rec.evidence[0].applied["term_filters"] == [PERSON]


def test_36_one_line_for_several_calls(world):
    model = Model([COUNT_ACCOUNT, {"name": "count_records", "args": {"object": "Interview__c"}}],
                  "There are 5 candidates in `Account` and 13 client interviews.\n" + SOURCE)
    rec = ask(world, model, "how many candidates and how many interviews are there")
    assert rec.answer.count("Candidates counted:") == 1
    assert [e.model_result["count"] for e in rec.evidence] == [FACTS["person_record_type"], FACTS["interviews"]]
    assert rec.evidence[1].enforced_arguments is None


def test_37_a_refusal_then_the_defined_call(world):
    wrong = {"name": "count_records", "args": {"object": "Account", "filters": [
        {"field": "Candidate_Status__c", "op": "eq", "value": "Placed"}]}}
    right = {"name": "count_records", "args": {"object": "Interview__c", "filters": OFFER,
                                               "count_distinct": "Candidate__c"}}
    model = Model(wrong, right, "1 candidate was placed (a client interview with outcome Offer Received).\n" + SOURCE)
    rec = ask(world, model, "how many candidates were placed")
    assert [(e.tool, e.object, e.error_code) for e in rec.evidence] == [("count_records", "Account", "T2"),
                                                                        ("count_records", "Interview__c", None)]
    tool_msg = next(m for m in model.requests["record"][1]["messages"] if m.get("role") == "tool")
    assert json.loads(tool_msg["content"])["error"].startswith("Placement has one meaning here")
    assert rec.answer.startswith("1 candidate was placed") and "Candidate__c.RecordTypeId" in rec.answer


@pytest.mark.parametrize("merge_by", ["code", "model"])
def test_39_the_completed_support_call_is_charted_with_its_split(world, monkeypatch, merge_by):
    monkeypatch.setenv("SFA_MERGE", merge_by)
    draft = ("Recruiter C supported 2 client interviews (offer rate 50.0%) and Delta Person 4 (25.0%).\n" + SOURCE)
    model = Model({"name": "group_records", "args": dict(SUPPORT_GROUP)}, draft, merged=draft)
    o = answer(world, model, "show a bar chart of support person performance")
    (ev,) = o.rec.evidence
    assert ev.enforced_arguments["split_by"] == "Interview_Outcome__c" and "split_by" not in ev.arguments
    assert o.charts, o.status_lines
    keys_drawn = set(o.charts[0]["chart"].get("y_keys") or [])
    assert len(keys_drawn) > 1   # the outcome split (code's own split_by) is drawn, not one bar per group


@pytest.mark.parametrize("tables", ["model", "code"])
def test_40_the_merge_counted_line_states_the_added_filter(world, monkeypatch, tables):
    monkeypatch.setenv("SFA_TABLES", tables)
    monkeypatch.setenv("SFA_CHARTS", "off")
    group = {"name": "group_records", "args": {"object": "Interview__c", "group_by": "Candidate__c"}}
    draft = "Candidate 1 had 5 client interviews and Candidate 2 had 4.\n" + SOURCE
    o = answer(world, model := Model(group, draft), "how many interviews per candidate")
    assert model.requests["record"]
    counted = [ln for ln in o.final.split("\n") if ln.startswith("Counted:")]
    assert counted == [f"Counted: `Interview__c` records grouped by `Candidate__c`, where {CAND_PERSON}."], o.final
    assert "Candidates counted: Person Accounts only" in o.final   # both lines: business meaning and raw filters


# ------------------------------------------------------------------------------------------------ static
def test_the_prompt_lines_and_the_definitions():
    assert set(TC.TEXTS) == {"candidates_note", "candidates_flag_note", "candidates_unapplied", "candidates_any_of",
                             "candidates_blank", "candidates_reject", "leads_reject", "placement_reject",
                             "support_reject", "support_note", "support_rate_note", "support_blank_outcomes",
                             "support_rate_unapplied", "placement_status_note", "person_accounts_line",
                             "placement_date_reject"}
    assert "(IsPersonAccount = true)" not in BT.DEFINITIONS["candidates"] + TC.TEXTS["candidates_reject"]
    assert P.load("business_terms").version == 5 and P.load("term_contracts").version == 3
    assert "always dated by Date_of_Interview__c" in BT.DEFINITIONS["placement"]
    assert {"placement", "candidates", "support_performance"} <= set(BT.DEFINITIONS)
    assert "record type Person Account (a filter IsPersonAccount = true is not that" in BT.DEFINITIONS["candidates"]
    assert "Interview_Support_Person__c" in BT.DEFINITIONS["support_performance"]
    assert "never" in BT.DEFINITIONS["support_performance"].lower() and "Session_Role__c" in BT.DEFINITIONS[
        "support_performance"]


def test_hints_name_support_performance_but_not_candidates():
    assert BT.terms_in("support person performance this month") == ["support_performance"]
    assert BT.terms_in("how many candidates joined") == []   # code enforces it; the system prompt says it
    lines = BT.term_lines("support person performance")
    assert lines[0] == BT.TERMS_HEADER and lines[1] == "- " + BT.DEFINITIONS["support_performance"]
    assert ("Session__c", "Session_Role__c", "Support") in BT.not_the_term("support person performance")
    assert BT.terms_in("placement rate of support persons") == ["placement", "support_performance"]


def test_query_evidence_keeps_its_fields():
    """enforced_arguments is no dataclass field: the evidence of a call code did not change is field for field as
    before (the parity replays compare the fields)."""
    assert "enforced_arguments" not in [f.name for f in dataclasses.fields(QueryEvidence)]
    assert _ev().enforced_arguments is None


def test_the_contract_registry():
    assert [c.key for c in TC.CONTRACTS] == ["candidates", "placement", "support_performance"]
    assert [c.key for c in TC.CONTRACTS if c.hint] == ["placement", "support_performance"]
    assert TC.CANDIDATE_PREDICATE == {"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"}


# ------------------------------------------------------------------------------------------------ review fixes
# The three reviews of the build (escapes E*, regression R*, real data D*; 2026-11-24): one test per finding.
def test_review_E1_an_or_group_on_another_record_type_is_refused(tools):
    q = "how many candidates"
    for any_of in ([{"field": "RecordTypeId", "op": "eq", "value": "Recruiter"},
                    {"field": "RecordTypeId", "op": "eq", "value": "B2B_Client"}],
                   [{"field": "IsPersonAccount", "op": "eq", "value": False},
                    {"field": "Name", "op": "contains", "value": "a"}]):
        refused(tools, q, "count_records", {"object": "Account", "any_of": any_of}, "T1")
    mixed = {"object": "Account", "any_of": [{"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"},
                                             {"field": "Name", "op": "contains", "value": "a"}]}
    res, _ev = ok(tools, q, "count_records", mixed)   # one branch keeps other accounts: the filter is added
    assert added(res) == [PERSON]
    both = {"object": "Account", "any_of": [{"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"},
                                            {"field": "RecordTypeId", "op": "eq", "value": "Person Account"}]}
    res, _ev = ok(tools, q, "count_records", both)   # every branch keeps only candidates: nothing added
    assert not added(res) and res["count"] == FACTS["person_record_type"]
    res, _ev = ok(tools, "how many candidates, recruiter accounts and B2B client accounts", "count_records", {
        "object": "Account", "any_of": [{"field": "RecordTypeId", "op": "eq", "value": "Recruiter"},
                                        {"field": "RecordTypeId", "op": "eq", "value": "B2B_Client"}]})
    assert not added(res) and res["count"] == 3   # the question names the other types: the OR group stands


def test_review_E2_E15_records_with_no_candidate(tools):
    args = {"object": "Interview__c", "filters": [{"field": "Candidate__c", "op": "is_blank"}]}
    base, _ev = ok(tools, "", "count_records", args)
    res, _ev = ok(tools, "how many interviews have no candidate", "count_records", args)
    assert base["count"] > 0 and res["count"] == base["count"] and not added(res)
    res, _ev = ok(tools, "interviews per candidate, including those with no candidate", "group_records",
                  {"object": "Interview__c", "group_by": "Candidate__c"})
    assert "(blank)" in [g["group"] for g in res["groups"]] and not added(res)
    assert TC.text("candidates_blank", FIELD="Candidate__c") in res["notes"]
    assert not TC.detect("accounts with no candidate status").no_candidate


def test_review_E3_D2_D3_the_model_split_gets_the_offer_rate(tools):
    q = "support person performance this year"
    res, ev = ok(tools, q, "group_records", {**SUPPORT_GROUP, "split_by": "Interview_Outcome__c"})
    got = {g["group"]: (g["value"], g["offers"], g["offer_rate_pct"]) for g in res["groups"]}
    assert got == FACTS["support"] and res["offer_rate_overall_pct"] == FACTS["offer_rate_overall"]
    assert ev.enforced_arguments is None and TC.text("support_rate_note") in res["notes"]
    assert TC.text("support_note") not in res["notes"]   # code set no split_by here
    blank, total = TC.blank_outcomes(res["split_totals"])
    assert total == FACTS["interviews"] and blank / total >= TC.BLANK_SHARE_NOTE
    assert TC.text("support_blank_outcomes", BLANK=blank, TOTAL=total) in res["notes"]
    keep = {**SUPPORT_GROUP, "filters": [{"field": "Interview_Outcome__c", "op": "ne", "value": "Ghosted"}]}
    res, ev = ok(tools, q, "group_records", keep)   # leaves the offers in: the rate still means something
    assert ev.enforced_arguments["split_by"] == "Interview_Outcome__c" and "offer_rate_overall_pct" in res
    for flt in ({"field": "Interview_Outcome__c", "op": "ne", "value": "Offer Received"},
                {"field": "Interview_Outcome__c", "op": "in", "values": ["Offer Received", "Ghosted"]}):
        res, ev = ok(tools, q, "group_records", {**SUPPORT_GROUP, "filters": [flt]})
        assert ev.enforced_arguments is None and "offer_rate_overall_pct" not in res


def test_review_E4_a_record_type_pattern_is_read(tools):
    q = "how many candidates"
    for flt in ({"field": "RecordTypeId.Name", "op": "contains", "value": "Recruit"},
                {"field": "RecordTypeId.DeveloperName", "op": "starts_with", "value": "Recr"}):
        err = refused(tools, q, "count_records", {"object": "Account", "filters": [flt]}, "T1")
        assert "RecordTypeId." in err
    res, _ev = ok(tools, q, "count_records", {"object": "Account", "filters": [
        {"field": "RecordTypeId.Name", "op": "contains", "value": "Person"}]})
    assert not added(res) and res["count"] == FACTS["person_record_type"]


def test_review_E5_related_child_filters_are_checked(tools):
    placed = {"object": "Recruiter__c", "related": [{"child_object": "Interview__c", "via_field": "Recruiter__c",
                                                    "exists": True, "filters": [
                                                        {"field": "Candidate__c.Candidate_Status__c", "op": "eq",
                                                         "value": "Placed"}]}]}
    refused(tools, "how many recruiters placed candidates", "count_records", placed, "T2")
    session = {"object": "Recruiter__c", "related": [{"child_object": "Session_Attendee__c",
                                                     "via_field": "Attendee_Recruiter__c", "exists": True, "filters": [
                                                         {"field": "Session__c.Session_Role__c", "op": "eq",
                                                          "value": "Support"}]}]}
    refused(tools, "support person performance", "count_records", session, "T3")


def test_review_E6_D1_the_flag_filter_gets_the_record_type_too(tools):
    q = "how many candidates"
    flag = {"object": "Account", "filters": [{"field": "IsPersonAccount", "op": "eq", "value": True}]}
    res, ev = ok(tools, q, "count_records", flag)
    assert res["count"] == FACTS["person_record_type"] != FACTS["flag_true"] and added(res) == [PERSON]
    assert ev.enforced_arguments["filters"][-1] == TC.CANDIDATE_PREDICATE
    view = NS(object=lambda name: name, person_type=lambda obj: None, has_field=lambda obj, api: True,
              record_type=lambda obj, v: None, path=lambda obj, text: TC.PathInfo(text, (), text))
    p = TC.plan(TC.detect(q), "count_records", {**flag}, view)   # the fallback: the flag is the candidate filter
    assert not p.add and p.reject is None
    res, _ev = ok(tools, "how many person accounts", "count_records", flag)   # no term: the flag stays alone
    assert not added(res) and res["count"] == FACTS["flag_true"]
    assert any(n.startswith("IsPersonAccount is no record type") for n in res["notes"])


def test_review_E7_E8_D5_R8_question_words():
    assert "candidates" in keys("group interviews by Candidate__c this month")
    for q in ("kitne ummedwar aaye", "kitne bachon ka interview hua", "kitne umedwar placed hue"):
        assert "candidates" in keys(q), q
    assert {"candidates", "placement"} <= keys("kitne bacche placed hue")
    for word in ("candidacy", "candidacies", "supplicant", "supplicants", "strainers"):
        assert "candidates" not in keys(f"the {word} of the year"), word
    assert TC.CANDIDATES.names("how many canidates") and not TC.CANDIDATES.names("how many accounts")
    assert [f.name for f in dataclasses.fields(TC.PathInfo)] == ["text", "refs", "field"]


def test_review_E9_E10_E11_overrides_and_placed_values(tools):
    assert not TC.detect("how many candidates had interviews this month regardless of type").all_types
    assert TC.detect("how many candidates regardless of record type").all_types
    q = "what is the interview status of placed candidates"
    assert not TC.detect(q).placed_status
    placed = {"object": "Account", "filters": [{"field": "Candidate_Status__c", "op": "eq", "value": "Placed"}]}
    refused(tools, q, "count_records", placed, "T2")
    refused(tools, "how many candidates were placed", "count_records", {"object": "Account", "filters": [
        {"field": "Candidate_Status__c", "op": "starts_with", "value": "Plac"}]}, "T2")
    assert TC._placed_value("contains", "lace") and not TC._placed_value("eq", "Plac")


def test_review_E12_support_per_month_stays_uncompleted(tools):
    """Accepted gap (design scope: the group call only): a trend split by the support person gets no outcome split
    and no rate."""
    args = {"object": "Interview__c", "grain": "month", "date_field": "Date_of_Interview__c",
            "period": "2026-01-01 to 2026-12-31", "split_by": "Interview_Support_Person__c"}
    res, ev = ok(tools, "support person performance per month", "trend_records", args)
    assert ev.enforced_arguments is None and "offer_rate_overall_pct" not in res


def test_review_E13_E14_the_claim_and_the_line():
    lookup = _ev({"filters": [CAND_PERSON]}, obj="Interview__c")
    text, removed = drop_unapplied_scope_claims("There are 9 candidates (Person Accounts only).\n\n2 candidates had "
                                                "interviews.", [lookup, _ev()], "how many candidates")
    assert removed == ["Person Accounts only"] and text.startswith("There are 9 candidates.")
    same = "2 candidates had interviews (Person Accounts only)."
    assert drop_unapplied_scope_claims(same, [lookup], "how many candidates") == (same, [])   # every call scoped
    backed = [_ev({"filters": [PERSON], "term_filters": [PERSON]})]
    out = with_term_line("There are 5 candidates. Person accounts also include employees' own accounts.", backed)
    assert "Candidates counted:" in out
    said = "There are 5 candidates (Person Accounts only)."
    assert with_term_line(said, backed) == said


@pytest.mark.parametrize("answer", [
    "There are 9 accounts; RecordTypeId is not filtered.",
    "Only person accounts carry PersonEmail, so 4 of them have one.",
    "There are 9 accounts (not Person Accounts only: every record type).",
    "9 accounts by type (RecordTypeId = grouped)."])
@pytest.mark.parametrize("question", ["how many accounts", "how many candidates"])
def test_review_R1_words_that_state_no_scope_stay(answer, question):
    assert drop_unapplied_scope_claims(answer, [_ev()], question) == (answer, [])


def test_review_R1_no_check_on_other_answers():
    text = "There are 9 accounts (Person Accounts only)."
    assert drop_unapplied_scope_claims(text, [_ev()], "how many accounts") == (text, [])   # nothing to check against
    typed = [_ev({"filters": ["RecordTypeId = 'B2B Client'"]})]
    assert drop_unapplied_scope_claims(text, typed, "how many accounts")[1] == ["Person Accounts only"]


def test_review_R2_a_developer_name_or_an_id_backs_its_label():
    ev = [_ev({"filters": ["RecordTypeId = 'Vendor / Implementation Partner'"]})]
    for claim in ("RecordTypeId = 'Vendor_Implementation_Partner'", "RecordTypeId = Vendor_Implementation_Partner",
                  "RecordTypeId = '012000000000001AAA'"):
        text = f"There is 1 vendor account ({claim})."
        assert drop_unapplied_scope_claims(text, ev, "how many vendor accounts") == (text, []), claim


def test_review_R3_support_lookups_are_no_performance_measure(tools):
    q = "who is the support person of interview I-0001"
    args = {"object": "Interview__c", "fields": ["Name", "Interview_Support_Person__c", "Preferred_Support_Person__c"],
            "filters": [{"field": "Name", "op": "eq", "value": "I-0001"}]}
    res, _ev = ok(tools, q, "list_records", args)
    assert res["total_count"] == 1
    listed = {"object": "Interview__c", "fields": ["Name", "Interview_Support_Person__c", "Preferred_Support_Person__c"]}
    ok(tools, "list interviews per support person", "list_records", listed)   # a list without performance words
    refused(tools, "support person performance", "list_records", listed, "T3")
    refused(tools, "how many interviews per support person", "group_records",
            {"object": "Interview__c", "group_by": "Preferred_Support_Person__c"}, "T3")


def test_review_R4_a_record_type_the_question_names_in_the_plural(tools):
    args = {"object": "Account", "filters": [{"field": "RecordTypeId", "op": "eq", "value": "Recruiter"}]}
    for q in ("how many candidates and recruiters are there", "how many candidates and how many recruiter person accounts"):
        res, _ev = ok(tools, q, "count_records", args)
        assert res["count"] == 2 and not added(res), q
    refused(tools, "how many candidates per recruiter", "count_records", args, "T1")
    assert TC.detect("how many candidates and employee person accounts").other_accounts
    assert not TC.detect("candidates with masters degrees", ["Master"]).other_accounts


def test_review_R5_descriptive_words_are_no_condition():
    rec = RecordAnswer(question="q", answer="There are 9 interviews.\n" + SOURCE, no_records=False, used_data=True,
                       evidence=[_ev(obj="Interview__c")])
    assert merge_scope_issues("- The candidate lookup offers Person Accounts only.", rec) == []
    assert any("Person Accounts only" in i for i in merge_scope_issues("9 interviews, filtered to person accounts.",
                                                                        rec))


def test_review_R6_refusals_write_the_operator_as_the_applied_texts_do():
    assert TC._item_text(None, {"field": "RecordTypeId", "op": "in", "values": ["A", "B"]}) == "RecordTypeId in ('A', 'B')"
    assert TC._item_text(None, {"field": "IsPersonAccount", "op": "eq", "value": False}) == "IsPersonAccount = false"
    assert TC._item_text(None, {"field": "X__c", "op": "starts_with", "value": "Pl"}) == "X__c starts with 'Pl'"


def test_review_D4_contact_as_a_verb_is_no_contact_record(tools):
    refused(tools, "how many candidates did we contact this month", "count_records", {"object": "Contact"}, "T1")
    res, _ev = ok(tools, "how many contact records do candidates have", "count_records", {"object": "Contact"})
    assert res["count"] == 1


def test_review_D6_a_failed_call_says_no_filter_was_added(tools):
    res, err, ev = run(tools, "how many candidates had interviews", "count_records", {
        "object": "Interview__c", "count_distinct": "Candidate__c",
        "filters": [{"field": "No_Such_Field__c", "op": "eq", "value": "x"}]})
    assert err and ev.error_code and not any(n.startswith("candidates are Person Accounts") for n in ev.notes)


def test_review_D7_blank_outcomes_are_counted():
    assert TC.blank_outcomes({"Offer Received": 1, "(blank)": 3, "Ghosted": 1}) == (3, 5)
    assert TC.blank_outcomes(None) == (0, 0)
    assert TC.text("support_blank_outcomes", BLANK=3, TOTAL=5).startswith("3 of the 5 interviews counted")


# ------------------------------------------------------------------------------------------------ placement date rule
# Owner's decision of 2026-11-25 (review finding R2-4, owner item O1): placements are dated and trended by the interview
# date, Interview__c.Date_of_Interview__c. A placement call dated by another field is refused (T2) with the call to send.
PLACED_Q = "how many placements in August 2026"
RATE_Q = "what is the placement rate per month this year"
DATE_REJECT = "Placements are dated by the interview date"
AUG_BY = {"date_field": "CreatedDate", "period": "August 2026"}
AUG = {"date_field": "Date_of_Interview__c", "period": "August 2026"}
RATE_TREND = {"object": "Interview__c", "grain": "month", "period": "this year", "part_filters": OFFER}


@pytest.fixture(params=["tools", "tools_on"])
def both(request):
    """The record tools with the spelling help off and on (SFA_RESOLVER): the date rule is the same."""
    return request.getfixturevalue(request.param)


def test_placement_date_period_on_created_date_is_refused(both):
    args = {"object": "Interview__c", "filters": OFFER, **AUG_BY}
    err = refused(both, PLACED_Q, "count_records", args, "T2")
    assert err.startswith(DATE_REJECT) and "This call dates the interviews by date_field CreatedDate." in err
    assert '"date_field": "Date_of_Interview__c"' in err
    # what the refusal prevents: by CreatedDate the September offer (created 31 August, New York time) counts too
    res, _ev = ok(both, "", "count_records", args)
    assert res["count"] == 2
    # the record tools' own words for the created date are read as CreatedDate
    refused(both, PLACED_Q, "count_records", {**args, "date_field": "created"}, "T2")


def test_placement_date_condition_in_the_filters_is_refused(both):
    flt = {"field": "CreatedDate", "op": "gte", "value": "2026-08-01"}
    err = refused(both, PLACED_Q, "count_records", {"object": "Interview__c", "filters": OFFER + [flt]}, "T2")
    assert "This call dates the interviews by the filter CreatedDate >= '2026-08-01'." in err
    # an any_of branch, a percentage's base filter and a compare segment's filter are date conditions too
    refused(both, PLACED_Q, "count_records", {"object": "Interview__c", "filters": OFFER, "any_of": [
        flt, {"field": "Interview_Status__c", "op": "eq", "value": "Completed"}]}, "T2")
    refused(both, RATE_Q, "percentage_of_records", {"object": "Interview__c", "part_filters": OFFER,
                                                     "base_filters": [flt]}, "T2")
    # no period: blank checks and a comparison with another field date nothing
    for other in ({"field": "CreatedDate", "op": "is_not_blank"},
                  {"field": "CreatedDate", "op": "lte", "other_field": "LastModifiedDate"}):
        _res, ev = ok(both, PLACED_Q, "count_records", {"object": "Interview__c", "filters": OFFER + [other]})
        assert ev.enforced_arguments is None


def test_placement_monthly_trend_on_created_date_is_refused(both):
    err = refused(both, RATE_Q, "trend_records", {**RATE_TREND, "date_field": "CreatedDate"}, "T2")
    assert "date_field CreatedDate" in err and "the same grain and part_filters" in err
    # a trend of placements among the outcomes (split by the outcome), and one of offers only
    refused(both, "placements per month", "trend_records", {
        "object": "Interview__c", "grain": "month", "date_field": "CreatedDate", "period": "this year",
        "split_by": "Interview_Outcome__c"}, "T2")
    refused(both, "placements per month", "trend_records", {
        "object": "Interview__c", "grain": "month", "date_field": "LastModifiedDate", "period": "this year",
        "filters": OFFER}, "T2")
    # another way to write the outcome value still names the placement
    refused(both, "placements per month", "trend_records", {
        "object": "Interview__c", "grain": "month", "date_field": "CreatedDate", "period": "this year",
        "filters": [{"field": "Interview_Outcome__c", "op": "contains", "value": "offer"}]}, "T2")


def test_placement_rate_by_interview_month_runs_unchanged(both):
    for date_field in ("Date_of_Interview__c", "interview_date"):   # the API name and the temporal key
        res, ev = ok(both, RATE_Q, "trend_records", {**RATE_TREND, "date_field": date_field})
        assert ev.enforced_arguments is None and res["date_field"] == "Date_of_Interview__c"
        got = {b["bucket"]: (b["part"], b["base"], b["percent"]) for b in res["buckets"]}
        assert got["2026-08"] == (1, 1, 100.0) and got["2026-09"] == (1, 4, 25.0) and got["2026-10"] == (0, 3, 0.0)
    res, ev = ok(both, "placement rate in September 2026", "percentage_of_records", {
        "object": "Interview__c", "part_filters": OFFER, "date_field": "Date_of_Interview__c",
        "period": "September 2026"})
    assert ev.enforced_arguments is None and (res["part"], res["base"], res["percent"]) == (1, 4, 25.0)
    refused(both, "placement rate in September 2026", "percentage_of_records", {
        "object": "Interview__c", "part_filters": OFFER, "date_field": "CreatedDate", "period": "September 2026"},
        "T2")


def test_placement_count_by_the_interview_date_passes_unchanged(both):
    res, ev = ok(both, PLACED_Q, "count_records", {"object": "Interview__c", "filters": OFFER, **AUG})
    assert ev.enforced_arguments is None and res["count"] == 1
    assert res["applied"]["period"]["date_field"] == "Date_of_Interview__c"
    flt = {"field": "Date_of_Interview__c", "op": "gte", "value": "2026-09-01"}
    res, ev = ok(both, PLACED_Q, "count_records", {"object": "Interview__c", "filters": OFFER + [flt]})
    assert ev.enforced_arguments is None and res["count"] == 1
    # month on month with the segments' periods on the interview date
    segs = [{"label": "Aug", "period": "August 2026"}, {"label": "Sep", "period": "September 2026"}]
    res, _ev = ok(both, "placements in August vs September 2026", "compare_records", {
        "object": "Interview__c", "filters": OFFER, "date_field": "Date_of_Interview__c", "segments": segs})
    assert [s["value"] for s in res["segments"]] == [1, 1]
    refused(both, "placements in August vs September 2026", "compare_records", {
        "object": "Interview__c", "filters": OFFER, "date_field": "CreatedDate", "segments": segs}, "T2")


def test_placement_call_with_no_date_is_unaffected(both):
    res, ev = ok(both, "how many placements so far", "count_records", {"object": "Interview__c", "filters": OFFER})
    assert ev.enforced_arguments is None and res["count"] == 2
    # a date_field with no period dates nothing (list_records only sorts by it)
    res, ev = ok(both, "list the placements", "list_records", {"object": "Interview__c", "filters": OFFER,
                                                                 "date_field": "CreatedDate"})
    assert ev.enforced_arguments is None and res["total_count"] == 2


def test_every_dated_interview_call_of_a_placement_question_uses_the_interview_date(both):
    """The rate's denominator (all client interviews, by interview month) is part of the placement measure: in a
    question that names placement, every dated Interview__c call is held to Date_of_Interview__c, outcome filter or
    not. (Changed on purpose 2026-11-25: such a call by CreatedDate ran unchanged in the first cut of the rule.)"""
    by_created = {"object": "Interview__c", **AUG_BY}
    err = refused(both, PLACED_Q, "count_records", by_created, "T2")
    assert "This call dates the interviews by date_field CreatedDate." in err
    for flt in ({"field": "Interview_Outcome__c", "op": "eq", "value": "Rejected"},
                {"field": "Interview_Outcome__c", "op": "ne", "value": "Offer Received"}):
        refused(both, PLACED_Q, "count_records", {**by_created, "filters": [flt]}, "T2")
    refused(both, RATE_Q, "trend_records", {"object": "Interview__c", "grain": "month", "date_field": "CreatedDate",
                                            "period": "this year"}, "T2")
    refused(both, PLACED_Q, "count_records", {"object": "Interview__c", "filters": [
        {"field": "CreatedDate", "op": "lt", "value": "2026-09-01"}]}, "T2")
    # by the interview date, or with no date, the denominator runs unchanged
    res, ev = ok(both, PLACED_Q, "count_records", {"object": "Interview__c", **AUG})
    assert ev.enforced_arguments is None and res["count"] == 1
    res, ev = ok(both, RATE_Q, "count_records", {"object": "Interview__c"})
    assert ev.enforced_arguments is None and res["count"] == FACTS["interviews"]
    # a date of another linked record (the candidate's) is a condition, not the date of the count
    ok(both, PLACED_Q, "count_records", {"object": "Interview__c", "filters": [
        {"field": "Candidate__c.CreatedDate", "op": "gte", "value": "2025-01-01"}]})


def test_interview_calls_by_created_date_outside_a_placement_question_are_unchanged(both):
    by_created = {"object": "Interview__c", **AUG_BY}
    want, _ev = ok(both, "", "count_records", by_created)
    res, ev = ok(both, "how many interviews were created in August 2026", "count_records", by_created)
    assert ev.enforced_arguments is None and res["count"] == want["count"]
    # a question that names no placement, even with the offer filter: the contract does not run
    res, ev = ok(both, "how many interviews were created in August 2026 with an offer", "count_records",
                 {**by_created, "filters": OFFER})
    assert ev.enforced_arguments is None and res["count"] == 2


def test_placement_through_a_lookup_to_the_interview(both):
    """A placement measured from another object through its lookup to Interview__c (Interview_Participant__c rows of
    the terms fixture, created on another day than their interview) is dated by Interview__c.Date_of_Interview__c."""
    offer = [{"field": "Interview__c.Interview_Outcome__c", "op": "eq", "value": "Offer Received"}]
    args = {"object": "Interview_Participant__c", "filters": offer, **AUG_BY}
    res, _ev = ok(both, "", "count_records", args)
    assert res["count"] == 2   # by CreatedDate the September interview's participant counts in August too
    err = refused(both, PLACED_Q, "count_records", args, "T2")
    assert "dates the interviews by date_field CreatedDate." in err
    assert '"date_field": "Interview__c.Date_of_Interview__c"' in err
    res, ev = ok(both, PLACED_Q, "count_records", {**args, "date_field": "Interview__c.Date_of_Interview__c"})
    assert ev.enforced_arguments is None and res["count"] == 1
    # a date condition on the counted object, or on another date of the interview, is refused too
    refused(both, PLACED_Q, "count_records", {"object": "Interview_Participant__c", "filters": offer + [
        {"field": "CreatedDate", "op": "gte", "value": "2026-08-01"}]}, "T2")
    refused(both, PLACED_Q, "count_records", {"object": "Interview_Participant__c", "filters": offer + [
        {"field": "Interview__c.CreatedDate", "op": "gte", "value": "2026-08-01"}]}, "T2")
    # the same object measuring no placement, and the question naming none, are unchanged
    res, ev = ok(both, PLACED_Q, "count_records", {"object": "Interview_Participant__c", **AUG_BY})
    assert ev.enforced_arguments is None and res["count"] == 2
    ok(both, "how many participants of offer interviews joined in August 2026", "count_records", args)


def test_placement_date_on_a_related_interview_child(both):
    rel = {"child_object": "Interview__c", "via_field": "Recruiter__c", "exists": True, "filters": OFFER,
           "period": "September 2026"}
    q = "how many recruiters made placements in September 2026"
    err = refused(both, q, "count_records", {"object": "Recruiter__c", "related": [
        {**rel, "date_field": "CreatedDate"}]}, "T2")
    assert "date_field CreatedDate of the related Interview__c records" in err
    res, ev = ok(both, q, "count_records", {"object": "Recruiter__c", "related": [
        {**rel, "date_field": "Date_of_Interview__c"}]})
    assert ev.enforced_arguments is None and res["count"] == 1
    # a related child that measures no placement is unchanged
    ok(both, q, "count_records", {"object": "Recruiter__c", "related": [
        {**rel, "filters": [{"field": "Interview_Outcome__c", "op": "eq", "value": "Rejected"}],
         "date_field": "CreatedDate"}]})


def test_placement_date_rule_reads_plain_data():
    """The engine rule over a plain view (no catalog); a view without date_path applies no date rule."""

    class V:
        def object(self, name):
            return name if name in ("Interview__c", "Account") else None

        def path(self, obj, text):
            if obj != "Interview__c" or text not in ("CreatedDate", "Date_of_Interview__c", "Interview_Outcome__c"):
                return None
            return TC.PathInfo(text, (), text)

        def record_type(self, obj, value):
            return None

        def person_type(self, obj):
            return None

        def has_field(self, obj, api):
            return False

    class D(V):
        def date_path(self, obj, text):
            return self.path(obj, text) if text != "Interview_Outcome__c" else None

    terms = TC.detect(PLACED_Q)
    assert TC.plan(terms, "count_records", {"object": "Interview__c", "filters": OFFER, **AUG_BY}, V()).reject is None
    p = TC.plan(terms, "count_records", {"object": "Interview__c", "filters": OFFER, **AUG_BY}, D())
    assert p.reject == ("T2", TC.text("placement_date_reject", FIELD="date_field CreatedDate", DATE="Date_of_Interview__c"))
    assert TC.plan(terms, "count_records", {"object": "Interview__c", "filters": OFFER, **AUG}, D()).reject is None
    assert TC.plan(TC.detect("how many interviews in August 2026"), "count_records",
                   {"object": "Interview__c", "filters": OFFER, **AUG_BY}, D()).reject is None
    flt = {"field": "CreatedDate", "op": "lt", "value": "2026-09-01"}
    assert TC.plan(terms, "count_records", {"object": "Interview__c", "filters": OFFER + [flt]}, D()).reject == (
        "T2", TC.text("placement_date_reject", FIELD="the filter CreatedDate < '2026-09-01'",
                                 DATE="Date_of_Interview__c"))
    assert TC._offer_value("eq", "offer-received") and TC._offer_value("starts_with", "Off")
    assert not TC._offer_value("eq", "Offer") and not TC._offer_value("contains", "of")


@pytest.mark.parametrize("tables, merge_by, charts", [
    ("model", "code", "on"), ("code", "code", "on"), ("model", "model", "on"), ("code", "model", "off")])
def test_placement_date_refusal_then_the_interview_month(world, monkeypatch, tables, merge_by, charts):
    """Through the record session and the pipeline under each switch (SFA_TABLES, SFA_MERGE, SFA_CHARTS; SFA_RESOLVER
    off here, on in the tool-level tests): the CreatedDate trend is refused, the interview-month trend runs."""
    monkeypatch.setenv("SFA_TABLES", tables)
    monkeypatch.setenv("SFA_MERGE", merge_by)
    monkeypatch.setenv("SFA_CHARTS", charts)
    wrong = {"name": "trend_records", "args": {**RATE_TREND, "date_field": "CreatedDate"}}
    right = {"name": "trend_records", "args": {**RATE_TREND, "date_field": "Date_of_Interview__c"}}
    draft = "The placement rate per interview month is listed by month.\n" + SOURCE
    o = answer(world, model := Model(wrong, right, draft, merged=draft), RATE_Q)
    assert [(e.tool, e.object, e.error_code) for e in o.rec.evidence] == [("trend_records", "Interview__c", "T2"),
                                                                          ("trend_records", "Interview__c", None)]
    tool_msg = next(m for m in model.requests["record"][1]["messages"] if m.get("role") == "tool")
    assert json.loads(tool_msg["content"])["error"].startswith(DATE_REJECT)
    ran = o.rec.evidence[1]
    assert ran.enforced_arguments is None and ran.arguments["date_field"] == "Date_of_Interview__c"
    assert {b["bucket"]: b["part"] for b in ran.model_result["buckets"]}["2026-09"] == 1
    assert o.final.strip()
