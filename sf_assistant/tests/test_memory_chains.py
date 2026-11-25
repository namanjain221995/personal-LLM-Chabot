"""Salesforce conversation memory, the chains (design v2 of 2026-11-25 §16.1, the critique's "Test gaps"; lane S4):
multi-step chats run model-free through the real ask service job with the state carried step to step, as the platform
carries it (``tests/chain_runner.py`` runs them; the chats are the JSON files in ``tests/chains/``).

For every chain:

- with ``SFA_MEMORY=on``, each step meets its expectations: the plan that ran (tools, model-form arguments, the
  enforced filters), the business-term contracts (candidates are Person Accounts, a placement is a client interview with
  the outcome Offer Received, support person performance is the interviews per ``Interview_Support_Person__c`` split
  by outcome with the offer rate), the reason and kind, the display flags, the clarifying text and its choices, and the
  next state;
- ``SFA_MEMORY=shadow`` answers byte for byte as ``SFA_MEMORY=off`` (C31): the same six result keys, the same model
  requests of the answer path, no resolver call.

A chain marked ``known_gap`` (``tests/chains/known_gaps.json``) holds the design's intent where the build does not meet
it yet; it runs as a strict xfail, so a fix shows up as a failure to update it.

The model is scripted per step; no model of any kind is called, and every name and value is made up.
"""
from __future__ import annotations

import copy
import json
import re
import types
from pathlib import Path

import pytest

import chain_runner as CR
import warehouse_fixture as F

ALL = CR.load_chains()
CHAINS = [c for c in ALL if not c.get("known_gap")]
GAPS = [c for c in ALL if c.get("known_gap")]
#: The chains design v2 §16.1 lists, T04 with the step stage-1 open point 19 adds ("relax, narrow, then visualize it").
DESIGN_IDS = ("C01", "C02", "C03", "C04", "C05", "C06", "C07", "C08", "U12", "P-A", "P-B", "H01", "H02", "H03", "H04",
              "T01", "T02", "T03", "T04", "T05", "K01", "G01", "N01", "M01", "X01", "R01", "V01", "S01", "A01", "CL01",
              "CL02", "CT01", "GR01")


# ------------------------------------------------------------------------------------------------ the chains
@pytest.mark.parametrize("chain", CHAINS, ids=[c["id"] for c in CHAINS])
def test_the_chain_runs_its_plan_with_memory_on(chain, tmp_path, monkeypatch):
    outs = CR.run_chain(chain, CR.ON, tmp_path, monkeypatch)
    CR.check_chain(chain, outs)


def _watch_the_m5_reading(monkeypatch) -> list:
    """Every call of the resolver's ``_Job._read_turns`` (the plan line of the last answer with each period named once,
    stage-2 review finding M5), which only ``SFA_MEMORY=on`` may reach."""
    from sf_assistant.agent import resolver

    seen: list = []
    real = resolver._Job._read_turns
    monkeypatch.setattr(resolver._Job, "_read_turns",
                        lambda self, standalone: seen.append(standalone) or real(self, standalone))
    return seen


def _watch_the_code_line(monkeypatch) -> list:
    """The ``code_line`` of every call of ``followup.rewrite`` that passes one (only the memory resolver does, with the
    model called: stage-3 review finding F1); the ask service's own rewrite passes none."""
    from sf_assistant.agent import followup

    seen: list = []
    real = followup.rewrite
    monkeypatch.setattr(followup, "rewrite", lambda question, turns, llm, **kw: (
        seen.append(kw["code_line"]) if "code_line" in kw else None) or real(question, turns, llm, **kw))
    return seen


@pytest.mark.parametrize("chain", CHAINS, ids=[c["id"] for c in CHAINS])
def test_shadow_answers_the_chain_byte_for_byte_as_off(chain, tmp_path, monkeypatch):
    seen = _watch_the_m5_reading(monkeypatch)
    code_lines = _watch_the_code_line(monkeypatch)
    off = CR.run_chain(chain, CR.OFF, tmp_path / "off", monkeypatch)
    shadow = CR.run_chain(chain, CR.SHADOW, tmp_path / "shadow", monkeypatch)
    CR.check_parity(chain, off, shadow)
    # M5 and F1 change only what SFA_MEMORY=on reads: off never calls the resolver, and shadow, which calls no model,
    # reads every turn as sent (resolver._Job._read) and passes no code_line
    assert seen == [] and code_lines == []


@pytest.mark.xfail(strict=True, reason="a known gap of the build (the chain's known_gap says which)")
@pytest.mark.parametrize("chain", GAPS, ids=[c["id"] for c in GAPS])
def test_a_known_gap_still_fails_as_the_design_means_it(chain, tmp_path, monkeypatch):
    CR.check_chain(chain, CR.run_chain(chain, CR.ON, tmp_path, monkeypatch))


def test_the_m5_reading_is_what_an_on_run_of_chain_b_reads(tmp_path, monkeypatch):
    """The control of the parity test's check: the same hook sees the on run of P-B ("and the placement rate?" after
    "plot it", read with the period named once), so an empty list in off and shadow means they never reached it."""
    seen = _watch_the_m5_reading(monkeypatch)
    outs = CR.run_chain(next(c for c in CHAINS if c["id"] == "P-B"), CR.ON, tmp_path, monkeypatch)
    assert seen == ["What is the placement rate each month in 2026?"] and outs[2].reply["reason"] == "new"


def test_the_code_line_is_what_an_on_run_after_chart_it_passes(tmp_path, monkeypatch):
    """The control of the parity test's ``code_line`` check: the on run of P-B-chart ("and the placement rate?" after
    "chart it", whose word stands in the plan line) reads that line as the line code wrote, so an empty list in off
    and shadow means they never passed it."""
    code_lines = _watch_the_code_line(monkeypatch)
    outs = CR.run_chain(next(c for c in CHAINS if c["id"] == "P-B-chart"), CR.ON, tmp_path, monkeypatch)
    assert code_lines == [True] and outs[2].reply["reason"] == "new"


# ------------------------------------------------------------------------------------------------ the chain files
def test_the_chain_files_hold_every_chain_of_the_design_once():
    ids = [c["id"] for c in ALL]
    assert len(ids) == len(set(ids)), ids
    assert set(DESIGN_IDS) <= {c["id"] for c in CHAINS}, set(DESIGN_IDS) - set(ids)
    assert len(CHAINS) >= len(DESIGN_IDS) and GAPS
    t04 = next(c for c in CHAINS if c["id"] == "T04")
    assert [s["q"] for s in t04["steps"]][-1] == "visualize it"            # relax, narrow, then visualize it


@pytest.mark.parametrize("chain", ALL, ids=[c["id"] for c in ALL])
def test_a_chain_uses_only_the_keys_the_runner_reads(chain):
    """A misspelt key would be ignored and the step would check less than it says."""
    assert set(chain) <= {"id", "title", "design", "world", "worlds", "steps", "file", "known_gap"}, set(chain)
    assert chain["title"] and chain["design"] and chain["steps"]
    worlds = chain.get("worlds") or {}
    for spec in worlds.values():
        assert set(spec) <= {"today", "drop_fields", "set"}, spec
    for i, step in enumerate(chain["steps"]):
        where = f"{chain['id']} step {i + 1}"
        assert set(step) <= CR.STEP_KEYS, (where, set(step) - CR.STEP_KEYS)
        assert isinstance(step["q"], str) and step["q"].strip(), where
        assert step.get("bridge") in (None, "fallback", "transparent"), where
        assert step.get("world", chain.get("world", "")) in ("", *worlds), where
        want = step.get("expect") or {}
        assert set(want) <= CR.EXPECT_KEYS, (where, set(want) - CR.EXPECT_KEYS)
        assert set(want.get("contracts") or ()) <= set(CR.CONTRACTS), where
        for ran in want.get("ran") or ():
            assert set(ran) <= CR.RAN_KEYS, (where, set(ran) - CR.RAN_KEYS)
        if isinstance(want.get("plan"), dict):
            assert set(want["plan"]) <= CR.PLAN_KEYS, (where, set(want["plan"]) - CR.PLAN_KEYS)
        if not step.get("bridge"):
            assert want, f"{where} expects nothing"
        for item in step.get("record") or ():
            assert isinstance(item, str) or set(item) == {"name", "args"}, where


def _texts(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _texts(v)
    elif isinstance(value, list):
        for v in value:
            yield from _texts(v)
    elif isinstance(value, str):
        yield value


def test_the_chains_hold_no_real_data():
    """Made-up names and values only: e-mail addresses at example.invalid, record Ids of the fixture's synthetic form
    (three characters, zeros, a number, AAA), no web address."""
    for chain in ALL:
        for text in _texts(chain):
            for mail in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text):
                assert mail.endswith("@example.invalid"), (chain["id"], mail)
            for rid in CR._ID_RE.findall(text):
                assert re.fullmatch(r"[0-9a-zA-Z]{3}0{6,}\d{1,6}AAA", rid), (chain["id"], rid)
            assert "http" not in text.lower() and "www." not in text.lower(), (chain["id"], text)


# ------------------------------------------------------------------------------------------------ the runner itself
def test_stored_is_a_json_round_trip_that_reorders_every_objects_keys():
    state = {"v": 1, "plan": {"calls": [{"tool": "count_records", "args": {"object": "Account", "filters": []}}],
                              "age": 0}, "pending": None}
    got = CR.stored(state)
    assert got == state and got is not state
    assert list(got) == sorted(state, reverse=True) and list(got["plan"]) == ["calls", "age"]
    assert list(got["plan"]["calls"][0]["args"]) == ["object", "filters"]


def test_the_runner_carries_the_state_and_the_gap_as_the_bridge_does(tmp_path, monkeypatch):
    """G01 run with memory on: the second service call gets the first reply's state after the jsonb-like round trip,
    and the turn the older route answered counts as a gap of 1."""
    chain = next(c for c in CHAINS if c["id"] == "G01")
    outs = CR.run_chain(chain, CR.ON, tmp_path, monkeypatch)
    first, fallback, confirm = outs[0], outs[1], outs[2]
    assert fallback.out is None and fallback.sent == {"bridge": "fallback", "gap": 1}
    assert confirm.sent["state"] == first.reply["state"] and confirm.sent["gap"] == 1
    assert list(confirm.sent["state"]) == sorted(first.reply["state"], reverse=True)
    assert [t["role"] for t in confirm.sent["turns"]] == ["user", "assistant"] * 2
    assert confirm.sent["turns"][1]["content"] == first.reply["answer"]
    assert confirm.sent["turns"][3]["content"] == chain["steps"][1]["answer"]


def test_a_wrong_expectation_fails_the_chain(tmp_path, monkeypatch):
    """The checks are not vacuous: C08 with a wrong clarifying text, wrong term filters and contracts it does not
    meet fails, and the message names every miss with its step."""
    chain = copy.deepcopy(next(c for c in CHAINS if c["id"] == "C08"))
    chain["steps"][1]["expect"]["answer"] = "another text"
    chain["steps"][2]["expect"]["ran"][0]["term_filters"] = []
    chain["steps"][2]["expect"]["contracts"] = ["placement", "support"]
    outs = CR.run_chain(chain, CR.ON, tmp_path, monkeypatch)
    with pytest.raises(AssertionError) as err:
        CR.check_chain(chain, outs)
    text = str(err.value)
    assert "C08 step 2 'What about candidates?': answer" in text
    assert "C08 step 3 '1': group_records term_filters" in text
    assert "contract placement:" in text and "contract support:" in text


def _out(answer, memory=None, reply_keys=CR.V1_KEYS):
    reply = {k: None for k in reply_keys}
    reply["answer"] = answer
    return {"reply": reply, "memory": memory}


def test_the_parity_check_catches_an_answer_that_differs():
    step = {"q": "visualize it"}
    off = [CR.StepOut(0, step, _out("A"), {}, [], {"resolve": 0}, "", {r: [] for r in CR.ROLES})]
    same = [CR.StepOut(0, step, _out("A", {"mode": "shadow"}, CR.V2_KEYS), {}, [], {"resolve": 0}, "",
                       {r: [] for r in CR.ROLES})]
    CR.check_parity({"id": "X"}, off, same)
    other = copy.deepcopy(same)
    other[0].out["reply"]["answer"] = "B"
    with pytest.raises(AssertionError, match="shadow answered otherwise"):
        CR.check_parity({"id": "X"}, off, other)
    called = copy.deepcopy(same)
    called[0].calls["resolve"] = 1
    with pytest.raises(AssertionError, match="shadow called the resolver model"):
        CR.check_parity({"id": "X"}, off, called)
    asked = copy.deepcopy(same)
    asked[0].requests["record"] = [{"messages": []}]
    with pytest.raises(AssertionError, match="the record requests differ"):
        CR.check_parity({"id": "X"}, off, asked)


def _ev(tool, args, enforced=None, term_filters=None, result=None, ok=True):
    applied = {"term_filters": term_filters} if term_filters else {}
    return types.SimpleNamespace(tool=tool, ok=ok, arguments=args, enforced_arguments=enforced, applied=applied,
                                 model_result=result or {})


def test_the_contract_checks_read_what_ran():
    acc = {"object": "Account", "group_by": "Candidate_Status__c"}
    person = {"field": "RecordTypeId", "op": "eq", "value": "PersonAccount"}
    scoped = _ev("group_records", acc, {**acc, "filters": [person]}, ["RecordTypeId = 'Person Account'"])
    assert CR._contract_problem("candidates", [scoped]) == ""
    assert "without the Person Account scope" in CR._contract_problem("candidates", [_ev("group_records", acc)])
    carried = _ev("list_records", {"object": "Interview__c",
                                   "filters": [dict(person, field="Candidate__c.RecordTypeId")]})
    assert CR._contract_problem("candidates", [carried]) == ""                  # a followed set's own path filter
    offer = {"field": "Interview_Outcome__c", "op": "eq", "value": "Offer Received"}
    counted = _ev("count_records", {"object": "Interview__c", "filters": [offer]})
    assert CR._contract_problem("placement", [counted]) == ""
    placed = {"object": "Account", "filters": [{"field": "Candidate_Status__c", "op": "eq", "value": "Placed"}]}
    assert "not Interview__c" in CR._contract_problem("placement", [_ev("count_records", placed)])
    sup = {"object": "Interview__c", "group_by": "Interview_Support_Person__c"}
    done = _ev("group_records", sup, {**sup, "split_by": "Interview_Outcome__c"},
               result={"offer_rate_overall_pct": 20.0})
    assert CR._contract_problem("support", [done]) == ""
    assert "no outcome split or offer rate" in CR._contract_problem("support", [_ev("group_records", sup)])
    assert CR._contract_problem("support", []) == "no data call ran"
    assert CR._contract_problem("candidates", [_ev("count_records", acc, ok=False)]) == "no data call ran"


def test_the_v1_script_of_a_step_follows_from_its_on_script():
    step = {"q": "visualize it", "record": ["Drawn."],
            "expect": {"ran": [{"tool": "group_records", "args": {"object": "Account", "group_by": "Name"}},
                               {"tool": "count_records", "args_has": {"object": "Account"}}]}}
    rewrite, record = CR._v1_script(step, [json.dumps({"kind": "patch", "standalone": "Visualize the accounts",
                                                       "ops": []})])
    assert rewrite == "Visualize the accounts"
    assert record == [{"name": "group_records", "args": {"object": "Account", "group_by": "Name"}}, "Drawn."]
    assert CR._v1_script({"q": "thanks"}, []) == ("thanks", [])
    assert CR._v1_script({"q": "x", "v1": {"rewrite": "y", "record": ["z"]}}, []) == ("y", ["z"])


# ------------------------------------------------------------------------------------------------ the fixture
def test_the_memory_fixture_adds_only_the_region_to_the_terms_fixture():
    import terms_fixture as TF

    tables, base = F.memory_tables(), TF.TERMS_TABLES
    assert [t for t, _c, _r in tables] == [t for t, _c, _r in base]
    for (name, cols, rows), (_n, bcols, brows) in zip(tables, base):
        if name != "Interview__c":
            assert (cols, rows) == (bcols, brows), name
            continue
        assert list(cols) == [*bcols, "Region__c"] and [r[:-1] for r in rows] == brows
        at = list(cols).index("Name")
        regions = [r[-1] for r in rows]
        assert {r[at]: r[-1] for r in rows if r[-1]} == F.MEMORY_REGION_OF
        assert set(filter(None, regions)) == set(F.MEMORY_REGIONS)
        counts = {k: regions.count(v) for k, v in (("north_america", "North America"), ("europe", "Europe"),
                                                    ("asia_pacific", "Asia Pacific"), ("no_region", None))}
        assert counts == F.MEMORY_FACTS
    kn = {k["object"]["api_name"]: k for k in F.memory_knowledge()}
    region = [f for f in kn["Interview__c"]["fields"] if f["api_name"] == "Region__c"]
    assert len(region) == 1 and [v["value"] for v in region[0]["picklist"]["values"]] == list(F.MEMORY_REGIONS)
    assert TF.TERMS_TABLES == base and all(c != "Region__c" for t, cols, _r in base for c in cols)   # left as it was


def test_open_memory_fixture_groups_by_the_region(tmp_path):
    tools = F.open_memory_fixture(tmp_path)
    try:
        tools.begin_question("how many interviews per region")
        out = tools.call("group_records", {"object": "Interview__c", "group_by": "Region__c"})
        assert out["ok"], out
        got = {g["group"]: g["value"] for g in out["result"]["groups"]}
        assert got == {"North America": 4, "Europe": 3, "Asia Pacific": 1, "(blank)": 5}
    finally:
        tools.close()


# ------------------------------------------------------------------------------------------------ stage-2 review fixes
# The findings of the stage-2 review of 2026-11-25 (correctness M2, M3; privacy PE1, PE2, PE3, PE5), each pinned on the
# memory fixture through the real job. Made-up names and values only (the fixture's synthetic rows, example.invalid).
_BY_STATUS_THIS_MONTH = {"object": "Interview__c", "group_by": "Interview_Status__c",
                         "date_field": "Date_of_Interview__c", "period": "this month"}


def test_a_compare_on_a_grouping_keeps_this_period_as_the_main_call(tmp_path, monkeypatch):
    """M2: "Compare with previous month" on a grouping adds the previous month as a side call; the next state keeps
    this month as the main call, "only top 2" ranks this month, and "visualize it" draws this month, as its Read-as
    line says (the seeded calls run with the main call last, and a seeded main call stays the main call)."""
    chain = {"id": "RV-M2", "steps": [
        {"q": "how many interviews by status this month",
         "record": [{"name": "group_records", "args": _BY_STATUS_THIS_MONTH},
                    "The interviews are shown.\n" + CR.SOURCE]},
        {"q": "Compare with previous month",
         "resolve": {"kind": "patch", "standalone": "", "ops": [{"compare_period": {"phrase": "previous month"}}]},
         "record": ["Both months are shown.\n" + CR.SOURCE]},
        {"q": "only top 2", "record": ["The top 2 are shown.\n" + CR.SOURCE]},
        {"q": "visualize it", "record": ["Shown.\n" + CR.SOURCE]},
    ]}
    outs = CR.run_chain(chain, CR.ON, tmp_path, monkeypatch)
    two, three, four = outs[1], outs[2], outs[3]
    assert two.reply["reason"] == "patch", two.out.get("memory")
    roles = {c["args"]["period"]: c["role"] for c in two.reply["state"]["plan"]["calls"]}
    assert roles == {"this month": "main", "previous month": "side"}
    assert two.reply["plan_line"].startswith("number of Interviews per Interview Status, this month (2026-10-01 to")
    assert [e.arguments.get("period") for e in two.evidence] == ["previous month", "this month"]   # main call last
    tops = {e.arguments.get("period"): e.arguments.get("top") for e in three.evidence}
    assert tops == {"this month": 2, "previous month": None}, tops
    assert four.reply["reason"] == "shortcut" and len(four.reply["charts"]) == 1
    assert four.reply["charts"][0]["subtitle"] == ["Period: 2026-10-01 to 2026-10-31 (by Date of Interview)"]
    assert four.reply["plan_line"].startswith("number of Interviews per Interview Status, this month (2026-10-01 to "
                                              "2026-10-31), top 2, as a chart")


_FOR_CLIENT = [{"field": "Client_Account__c", "op": "eq", "value": "Client Co"}]
_PER_RECRUITER = {"object": "Interview__c", "group_by": "Recruiter__c", "filters": _FOR_CLIENT,
                  "date_field": "Date_of_Interview__c", "period": "this year"}
_PER_OUTCOME = {"object": "Interview__c", "group_by": "Interview_Outcome__c", "filters": _FOR_CLIENT,
                "date_field": "Date_of_Interview__c", "period": "this year"}


@pytest.mark.parametrize("memory", ["on", "shadow"])
def test_a_new_question_that_carries_an_earlier_typed_name_stores_its_own_plan(tmp_path, monkeypatch, memory):
    """M3: "what about by outcome?" read as a new question that carries "Client Co", which the person typed in the
    question before. The value screen counts the user turns the bridge sent as typed, so the state holds the outcome
    query that ran (age 0), and its plan line names it: never the per-recruiter query of the answer before."""
    second = {"q": "what about by outcome?",
              "record": [{"name": "group_records", "args": _PER_OUTCOME}, "The outcomes are shown.\n" + CR.SOURCE]}
    if memory == "on":
        second["resolve"] = {"kind": "new", "standalone": "How many interviews for Client Co this year by outcome?",
                             "ops": []}
    else:
        second["rewrite"] = "How many interviews for Client Co this year by outcome?"
    chain = {"id": "RV-M3", "steps": [
        {"q": "how many interviews per recruiter for Client Co this year",
         "record": [{"name": "group_records", "args": _PER_RECRUITER}, "The recruiters are shown.\n" + CR.SOURCE]},
        second]}
    outs = CR.run_chain(chain, CR.ON if memory == "on" else CR.SHADOW, tmp_path, monkeypatch)
    two = outs[1]
    assert [e.arguments for e in two.evidence] == [_PER_OUTCOME]
    plan = two.reply["state"]["plan"]
    assert [c["args"] for c in plan["calls"]] == [_PER_OUTCOME] and plan["age"] == 0
    assert "Interview Outcome" in two.reply["plan_line"] and "Recruiter" not in two.reply["plan_line"]
    assert "what about by outcome?" in plan["terms"]["text"]
    assert "Client Co this year" not in plan["terms"]["text"]   # the turns count for the screen only


def test_an_answer_whose_every_call_was_screened_out_stores_no_plan(tmp_path, monkeypatch):
    """M3, the first part: when the value screen leaves out every call the answer ran (here a name and a date the
    record agent read in a result), the state holds no plan, and never the plan of the answer before."""
    w = CR.World(tmp_path, monkeypatch, CR.ON, {})
    try:
        w.client.record = [{"name": "group_records", "args": _BY_STATUS_THIS_MONTH}, "Shown.\n" + CR.SOURCE]
        one = w.job("how many interviews by status this month", [], state=None, gap=0)
        assert one["reply"]["state"]["plan"] is not None
        turns = [{"role": "user", "content": "how many interviews by status this month"},
                 {"role": "assistant", "content": one["reply"]["answer"]}]
        look = {"object": "Recruiter__c", "fields": ["Name", "Date_of_Birth__c"],
                "filters": [{"field": "Name", "op": "eq", "value": "Lima Nobel"}]}
        older = {"object": "Recruiter__c",
                 "filters": [{"field": "Date_of_Birth__c", "op": "lt", "value": "1990-01-01"}]}
        w.client.resolve = [json.dumps({"kind": "new", "standalone": "How many employees are older than the oldest "
                                        "recruiter?", "ops": []})]
        w.client.record = [{"name": "list_records", "args": look}, {"name": "count_records", "args": older},
                           "No one is older.\n" + CR.SOURCE]
        two = w.job("how many employees are older than the oldest recruiter?", turns, state=CR.stored(
            one["reply"]["state"]), gap=0)
        assert two["reply"]["status"] == "answered" and two["reply"]["reason"] == "new", two
        assert {e.tool for e in w.tools.evidence if e.ok} >= {"list_records", "count_records"}
        assert two["reply"]["state"] is not None and two["reply"]["state"]["plan"] is None
        assert two["reply"]["plan_line"] == ""
    finally:
        w.close()


_EMAIL = "a@example.invalid"   # Recruiter A's work e-mail in the fixture (department "Dept One")


@pytest.mark.parametrize("department", ["Phone Support", "WhatsApp Desk"])
def test_a_contact_word_in_a_typed_value_never_opens_the_contact_fields(tmp_path, monkeypatch, department):
    """PE1: a list with e-mail addresses the person asked for, then "top 3", which asks for none. The shortcut runs
    the plan line, which holds the typed department with a contact word; the contact rule is read from "top 3", so
    the e-mail column is left out and an address in the draft is replaced."""
    from sf_assistant.engine.contact import asks_for_contact

    w = CR.World(tmp_path, monkeypatch, CR.ON, {"set": [{"table": "Department__c", "name": "Dept One",
                                                          "field": "Name", "value": department}]})
    try:
        ask = f"list the employees of the {department} department with their email"
        args = {"object": "Recruiter__c", "fields": ["Name", "Email__c"],
                "filters": [{"field": "Department__c", "op": "eq", "value": department}]}
        w.client.record = [{"name": "list_records", "args": args}, "The employees are listed.\n" + CR.SOURCE]
        one = w.job(ask, [], state=None, gap=0)
        assert _EMAIL in str([e.model_result for e in w.tools.evidence if e.tool == "list_records"][0])
        turns = [{"role": "user", "content": ask}, {"role": "assistant", "content": one["reply"]["answer"]}]
        w.client.record = [f"Recruiter A: {_EMAIL}\n" + CR.SOURCE]
        two = w.job("top 3", turns, state=CR.stored(one["reply"]["state"]), gap=0)
        assert two["reply"]["reason"] == "shortcut", two
        assert asks_for_contact(two["question_used"]) is True        # the plan line reads as a contact ask
        assert w.tools._contact_ok is False                          # the typed "top 3" does not
        ev = [e for e in w.tools.evidence if e.tool == "list_records"]
        assert ev and ev[0].ok and _EMAIL not in str(ev[0].model_result)
        assert _EMAIL not in two["reply"]["answer"]
    finally:
        w.close()


_DOB = "1990-01-01"
_LOOK = {"object": "Recruiter__c", "fields": ["Name", "Date_of_Birth__c"],
         "filters": [{"field": "Name", "op": "eq", "value": "Lima Nobel"}]}


@pytest.mark.parametrize("memory, count", [
    ("shadow", {"object": "Recruiter__c", "filters": [{"field": "Date_of_Birth__c", "op": "gt", "value": _DOB}]}),
    ("on", {"object": "Recruiter__c", "filters": [{"field": "Date_of_Birth__c", "op": "gt", "value": _DOB}]}),
    ("shadow", {"object": "Recruiter__c", "date_field": "Date_of_Birth__c", "period": f"{_DOB} to 2026-10-13"}),
    ("on", {"object": "Recruiter__c", "date_field": "Date_of_Birth__c", "period": f"since {_DOB}"}),
], ids=["shadow-filter", "on-filter", "shadow-period", "on-since"])
def test_a_date_the_record_agent_read_in_a_result_is_not_kept(tmp_path, monkeypatch, memory, count):
    """PE2: "born after Lima Nobel": the record agent reads her date of birth in a result and counts with it, as a
    filter or as the call's period. Nobody typed the date, so the call is left out of the state and the plan line."""
    w = CR.World(tmp_path, monkeypatch, CR.ON if memory == "on" else CR.SHADOW, {})
    try:
        ask = "how many employees were born after Lima Nobel?"
        w.client.record = [{"name": "list_records", "args": _LOOK}, {"name": "count_records", "args": count},
                           "No employee was born after Lima Nobel.\n" + CR.SOURCE]
        one = w.job(ask, [], state=None, gap=0)
        assert _DOB in json.dumps([e.model_result for e in w.tools.evidence if e.tool == "list_records"],
                                  default=str)
        assert [e.ok for e in w.tools.evidence if e.tool == "count_records"] == [True]
        reply = one["reply"]
        assert reply["status"] == "answered", one
        assert _DOB not in json.dumps(reply["state"]) and _DOB not in reply["plan_line"]
        assert [c["tool"] for c in reply["state"]["plan"]["calls"]] == ["list_records"]   # the typed name's lookup
    finally:
        w.close()


@pytest.mark.parametrize("question, args", [
    ("how many interviews since 2026-10-01",
     {"object": "Interview__c", "filters": [{"field": "Date_of_Interview__c", "op": "gte", "value": "2026-10-01"}]}),
    ("how many interviews this month", {"object": "Interview__c", "filters": [
        {"field": "Date_of_Interview__c", "op": "gte", "value": "2026-10-01"},
        {"field": "Date_of_Interview__c", "op": "lt", "value": "2026-11-01"}]}),
    ("how many interviews this month",
     {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "2026-10-01 to 2026-10-31"}),
    ("how many interviews last month and this month",
     {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "2026-09-01 to 2026-10-31"}),
    ("how many interviews pichle mahine",
     {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "last month"}),
    ("how many interviews this year",
     {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "2026"}),
    ("how many interviews per recruiter",
     {"object": "Interview__c", "date_field": "Date_of_Interview__c", "period": "last 12 months"}),
], ids=["typed-date", "this-month-bounds", "this-month-range", "two-months-span", "hinglish", "same-range",
        "relative-default"])
def test_a_date_of_a_period_the_person_typed_is_kept(tmp_path, monkeypatch, question, args):
    """PE2, the other side: a date the person typed, the bounds or the range of a period the typed words name
    (Hinglish too, and other words for the same dates), and relative words with no date of their own (the chart
    note's default "last 12 months") stay in the plan."""
    w = CR.World(tmp_path, monkeypatch, CR.ON, {})
    try:
        w.client.record = [{"name": "count_records", "args": args}, "Counted.\n" + CR.SOURCE]
        one = w.job(question, [], state=None, gap=0)
        assert [e.ok for e in w.tools.evidence if e.tool == "count_records"] == [True], one
        plan = one["reply"]["state"]["plan"]
        assert plan is not None and plan["calls"][0]["args"] == args
    finally:
        w.close()


def test_a_month_the_record_agent_chose_is_not_kept(tmp_path, monkeypatch):
    """PE2: a month the person never named (the agent read "the busiest month" in a result) is left out like a date."""
    w = CR.World(tmp_path, monkeypatch, CR.ON, {})
    try:
        args = {"object": "Interview__c", "group_by": "Recruiter__c", "date_field": "Date_of_Interview__c",
                "period": "September 2026"}
        w.client.record = [{"name": "group_records", "args": args}, "Shown.\n" + CR.SOURCE]
        one = w.job("how many interviews per recruiter in the busiest month", [], state=None, gap=0)
        assert [e.ok for e in w.tools.evidence if e.tool == "group_records"] == [True], one
        assert one["reply"]["state"]["plan"] is None and "September" not in one["reply"]["plan_line"]
    finally:
        w.close()


_LINK = "https://profiles.example.invalid/person-a"
_STREET = "Example Street, Sample Town"


@pytest.mark.parametrize("first, kept", [
    ("list the employees with their email", False),
    ("list the employees", True),
], ids=["asked-contact-then", "asked-none-then"])
def test_a_question_about_the_last_answer_gets_no_contact_detail_it_does_not_ask_for(tmp_path, monkeypatch, first,
                                                                                    kept):
    """PE3: the record agent gets the last answer's text for a question about it only under the contact rule: not at
    all when that answer's question asked for contact details (a link or an address may be in it as well as an
    e-mail), and without e-mail addresses and phone numbers otherwise."""
    w = CR.World(tmp_path, monkeypatch, CR.ON, {})
    try:
        fields = ["Name", "Email__c"] if kept is False else ["Name"]
        w.client.record = [{"name": "list_records", "args": {"object": "Recruiter__c", "fields": fields}},
                           f"Recruiter A: {_EMAIL}, {_LINK}, {_STREET}\nRecruiter B: none.\n" + CR.SOURCE]
        one = w.job(first, [], state=None, gap=0)
        turns = [{"role": "user", "content": first}, {"role": "assistant", "content": one["reply"]["answer"]}]
        q = "Why is Recruiter A listed before Recruiter B?"
        w.client.resolve = [json.dumps({"kind": "about_answer", "standalone": q, "ops": []})]
        echo = f"Recruiter A ({_EMAIL}, {_LINK}, {_STREET}) comes first by name.\n" + CR.SOURCE
        w.client.record = [echo, echo, echo]
        two = w.job(q, turns, state=CR.stored(one["reply"]["state"]), gap=0)
        assert two["reply"]["reason"] == "about_answer", two
        user = w.client.requests["record"][-1]["messages"][1]["content"]
        assert _EMAIL not in user
        assert (_LINK in user and _STREET in user and "Recruiter B: none." in user) is kept
        assert (_STREET in user) is kept
        assert _EMAIL not in two["reply"]["answer"]
    finally:
        w.close()


def test_the_resolvers_raw_reply_never_reaches_the_ask_log(tmp_path, monkeypatch):
    """PE5 / L3: an unreadable resolver reply that quotes the summary ends unclear; the log line holds the resolver's
    codes and the reply's length, never the reply (its prompt held the summary, the plan and the turns)."""
    import asyncio

    mark = "SUMMARYMARK the person tracks the Example region"
    w = CR.World(tmp_path, monkeypatch, CR.ON, {})
    try:
        turns = [{"role": "user", "content": "how many interviews this month"},
                 {"role": "assistant", "content": "There were 3 interviews this month."}]
        w.client.resolve = [f"Here is the summary you gave me: {mark}"]
        reply = asyncio.run(w.service.ask({"question": "and for them?", "recent_turns": turns, "request_id": "rv-1",
                                           "contract": 2, "state": None, "gap": 0, "summary": mark}))
        assert reply["status"] == "unclear"
        assert "SUMMARYMARK" in json.dumps(w.client.requests["resolve"][-1]["messages"])
        text = "".join(p.read_text() for p in Path(w.log.path).glob("ask_log-*.jsonl"))
        assert "SUMMARYMARK" not in text
        line = [json.loads(x) for x in text.splitlines() if x.strip() and '"rv-1"' in x][-1]
        assert "rewrite_reply" not in line and line["memory"]["resolver"]["reply_chars"] > 0
    finally:
        w.close()
