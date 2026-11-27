"""Chart labels under the contact rule with ``SFA_MEMORY=on`` (stage 3 of the Salesforce conversation memory, lane
charts: the first leftover of the stage-2 sf fix report).

``charts.make`` cleans every row label, series name and compare label (``agent.charts._Cleaner``): e-mail addresses
and phone numbers are replaced unless the question asks for contact details. A patch or a shortcut runs its plan
line, which repeats the typed values of earlier questions ("Phone Support"). So with ``SFA_MEMORY=on`` the chart step
reads the rule from the person's typed words (``MemoryRun.contact_ok``, the flag of stage-2 review fix PE1), as the
record tools and the answer's last drop already read it. With ``SFA_MEMORY=off`` and ``shadow`` no flag is passed,
and the chart step reads the question that runs, exactly as before.

The value under test is stored in a field that is no contact field (an employee's status). The record tools return
such a value whatever the contact flag says, so only the chart step decides whether it reaches a label.

The model is scripted per step; no model of any kind is called. Every name and value is made up (the memory fixture's
synthetic rows, ``example.invalid``, a 555 phone number).
"""
from __future__ import annotations

import base64
import json
from types import SimpleNamespace as NS

import pytest

import chain_runner as CR
from sf_assistant.agent import charts
from sf_assistant.engine.contact import EMAIL_SHOWN, PHONE_SHOWN, asks_for_contact

#: An employee status that holds contact details: no contact field, so the record tools return it as it is.
MAIL_STATUS = "Cover desk status-desk@example.invalid"
PHONE_STATUS = "Cover desk +1 555 010 0199"
VALUES = {"mail": (MAIL_STATUS, "status-desk@example.invalid", EMAIL_SHOWN),
          "phone": (PHONE_STATUS, "+1 555 010 0199", PHONE_SHOWN)}
SHOWN = NS(rows="The employees are counted.\n" + CR.SOURCE, chart="Shown.\n" + CR.SOURCE)


def _by_status(department: str) -> dict:
    return {"object": "Recruiter__c", "group_by": "Employment_Status__c",
            "filters": [{"field": "Department__c", "op": "eq", "value": department}]}


def _world(status: str, department: str = "") -> dict:
    """The memory fixture with Recruiter A's status set to ``status`` and, when given, Dept One renamed."""
    sets = [{"table": "Recruiter__c", "name": "Recruiter A", "field": "Employment_Status__c", "value": status}]
    if department:
        sets.append({"table": "Department__c", "name": "Dept One", "field": "Name", "value": department})
    return {"set": sets}


def chain_plan_line_word(status: str) -> dict:
    """A count per status for the "Phone Support" department, then "visualize it". The shortcut runs the plan line,
    which holds the typed department and so reads as a contact ask; the typed "visualize it" does not."""
    args = _by_status("Phone Support")
    return {"id": "CH-PLAN-WORD", "world": "w", "worlds": {"w": _world(status, "Phone Support")}, "steps": [
        {"q": "how many employees per status in the Phone Support department",
         "record": [{"name": "group_records", "args": args}, SHOWN.rows]},
        {"q": "visualize it", "record": [SHOWN.chart],
         "v1": {"rewrite": "Show the number of employees per status in the Phone Support department as a chart",
                "record": [{"name": "group_records", "args": args}, SHOWN.chart]}},
    ]}


def chain_typed_word(status: str) -> dict:
    """The plan-line-word chain with a follow-up whose typed words hold the department ("chart it for the Phone Support
    department"). Off and shadow accept the v1 rewrite (both read as a contact ask), so the chart step runs there with
    a question that holds a contact word."""
    args = _by_status("Phone Support")
    return {"id": "CH-TYPED-WORD", "world": "w", "worlds": {"w": _world(status, "Phone Support")}, "steps": [
        {"q": "how many employees per status in the Phone Support department",
         "record": [{"name": "group_records", "args": args}, SHOWN.rows]},
        {"q": "chart it for the Phone Support department", "record": [SHOWN.chart],
         "v1": {"rewrite": "chart how many employees per status for the Phone Support department",
                "record": [{"name": "group_records", "args": args}, SHOWN.chart]}},
    ]}


def chain_typed_mail(status: str) -> dict:
    """A count per status for Dept One, then a chart "with their email". The typed words ask for e-mail addresses;
    the plan line ("... Department is Dept One, as a chart") does not."""
    args = _by_status("Dept One")
    return {"id": "CH-TYPED-MAIL", "world": "w", "worlds": {"w": _world(status)}, "steps": [
        {"q": "how many employees per status in the Dept One department",
         "record": [{"name": "group_records", "args": args}, SHOWN.rows]},
        {"q": "show it as a chart with their email",
         "resolve": {"kind": "patch", "standalone": "", "ops": [{"set_show": {"chart": True}}]},
         "record": [SHOWN.chart],
         "v1": {"rewrite": "Show the number of employees per status in the Dept One department as a chart with "
                           "their email",
                "record": [{"name": "group_records", "args": args}, SHOWN.chart]}},
    ]}


def chain_direct(status: str, question: str) -> dict:
    """One direct chart question (no state, no earlier turn)."""
    return {"id": "CH-DIRECT", "world": "w", "worlds": {"w": _world(status)}, "steps": [
        {"q": question, "record": [{"name": "group_records", "args": _by_status("Dept One")}, SHOWN.chart]}]}


DIRECT_MAIL = "bar chart of employees per status in the Dept One department with their email"
DIRECT_PLAIN = "bar chart of employees per status in the Dept One department"


@pytest.fixture
def made(monkeypatch):
    """Every ``charts.make`` call of the run: the question it got, its keyword arguments and the items it returned
    (with their pictures). The real function runs."""
    calls: list[dict] = []
    real = charts.make

    def spy(*args, **kwargs):
        got = real(*args, **kwargs)
        calls.append({"question": args[1], "kwargs": dict(kwargs), "items": got[1]})
        return got

    monkeypatch.setattr(charts, "make", spy)
    return calls


def _pictures(items: list[dict]) -> str:
    return " ".join(base64.b64decode(i["svg_b64"]).decode("utf-8") for i in items if i.get("svg_b64"))


def _seen(step: CR.StepOut, made_call: dict) -> str:
    """Everything a person sees of the step's charts: the reply's chart items and the pictures."""
    return json.dumps(step.reply["charts"], ensure_ascii=False) + " " + _pictures(made_call["items"])


def _group_result(step: CR.StepOut) -> str:
    ran = [e for e in step.evidence if e.tool == "group_records" and e.ok]
    assert len(ran) == 1, [(e.tool, e.ok) for e in step.evidence]
    return json.dumps(ran[0].model_result, ensure_ascii=False, default=str)


# ------------------------------------------------------------------------------------------------ the cleaner and make
QUESTIONS = ["bar chart", "bar chart of the e-mail address by status", "chart of their phone numbers by status",
             "number of Employees per Employment Status, Department is Phone Support, as a chart", "", None]


@pytest.mark.parametrize("question", QUESTIONS)
def test_without_a_flag_the_cleaner_reads_the_question_as_before(question):
    assert charts._Cleaner(question, []).asked is asks_for_contact(question)
    assert charts._Cleaner(question, [], contact_ok=None).asked is asks_for_contact(question)


@pytest.mark.parametrize("question", QUESTIONS)
def test_a_flag_given_overrules_the_question(question):
    assert charts._Cleaner(question, [], contact_ok=True).asked is True
    assert charts._Cleaner(question, [], contact_ok=False).asked is False
    kept = charts._Cleaner(question, [], contact_ok=True)(MAIL_STATUS)
    dropped = charts._Cleaner(question, [], contact_ok=False)(MAIL_STATUS)
    assert kept == MAIL_STATUS and dropped == f"Cover desk {EMAIL_SHOWN}"


def _rec():
    res = {"query_id": "q1", "object": "Recruiter__c", "grouped_by": "Employment_Status__c", "measure": "count",
           "groups": [{"group": "Active Employee", "value": 3}, {"group": MAIL_STATUS, "value": 2},
                      {"group": PHONE_STATUS, "value": 1}],
           "group_count": 3, "shown": 3, "unresolved_ids": 0, "ties_at_cutoff": [], "ties_at_cutoff_count": 0,
           "total_across_groups": 6, "source": "fake copy"}
    ev = NS(tool="group_records", ok=True, kind="data", object="Recruiter__c", model_result=res, query_id="q1",
            arguments={"object": "Recruiter__c", "group_by": "Employment_Status__c"}, statements=[], error=None)
    return NS(evidence=[ev], table_blocks={})


@pytest.mark.parametrize("question", [DIRECT_PLAIN, DIRECT_MAIL, "bar chart of their phone numbers by status"])
def test_make_without_a_flag_is_make_with_the_flag_of_its_question(question):
    """``contact_ok`` None (what off and shadow pass: nothing) gives what the question gives, picture included."""
    ask = charts.chart_ask(question)
    before = charts.make(ask, question, "There are 6.", "records", "lean", _rec(), None, "2026-10-13")
    assert before[1] and before[3] == ""
    assert charts.make(ask, question, "There are 6.", "records", "lean", _rec(), None, "2026-10-13",
                       contact_ok=None) == before
    assert charts.make(ask, question, "There are 6.", "records", "lean", _rec(), None, "2026-10-13",
                       contact_ok=asks_for_contact(question)) == before


def test_make_with_a_flag_reads_the_flag_not_the_question():
    ask = charts.chart_ask(DIRECT_MAIL)
    _f, items, _l, error = charts.make(ask, DIRECT_MAIL, "t", "records", "lean", _rec(), None, None, contact_ok=False)
    assert error == "" and items
    seen = json.dumps(items) + _pictures(items)
    assert "status-desk@example.invalid" not in seen and "555 010 0199" not in seen
    assert EMAIL_SHOWN in seen and PHONE_SHOWN in seen
    ask = charts.chart_ask(DIRECT_PLAIN)
    _f, items, _l, error = charts.make(ask, DIRECT_PLAIN, "t", "records", "lean", _rec(), None, None, contact_ok=True)
    seen = json.dumps(items) + _pictures(items)
    assert error == "" and "status-desk@example.invalid" in seen and "+1 555 010 0199" in seen


def test_the_mcp_chart_never_shows_contact_details():
    """``for_tool`` (an MCP envelope's chart) passes no question and no flag: contact details never show."""
    item = charts.for_tool("group_records", _rec().evidence[0].model_result, {}, None, None)
    assert "status-desk@example.invalid" not in json.dumps(item) and EMAIL_SHOWN in json.dumps(item)


# ------------------------------------------------------------------------------------------------ SFA_MEMORY=on
@pytest.mark.parametrize("kind", sorted(VALUES))
def test_a_contact_word_in_the_plan_line_never_keeps_contact_details_in_a_chart_label(tmp_path, monkeypatch, made,
                                                                                      kind):
    """The shortcut "visualize it" runs the plan line "... Department is Phone Support, as a chart", which reads as a
    contact ask; the typed words do not. The status value with an e-mail address or a phone number reaches the chart
    step (no contact field, so the record tools return it) and its label is cleaned."""
    status, raw, shown = VALUES[kind]
    outs = CR.run_chain(chain_plan_line_word(status), CR.ON, tmp_path, monkeypatch)
    two = outs[1]
    assert two.reply["reason"] == "shortcut", two.out
    assert asks_for_contact(two.out["question_used"]) is True          # the plan line reads as a contact ask
    assert asks_for_contact("visualize it") is False                   # the typed words do not
    assert raw in _group_result(two)                                   # the value reached the chart step
    assert made[-1]["question"] == two.out["question_used"]
    assert made[-1]["kwargs"] == {"contact_ok": False}
    assert len(two.reply["charts"]) == 1
    seen = _seen(two, made[-1])
    assert raw not in seen and shown in seen
    assert raw not in two.reply["answer"]


@pytest.mark.parametrize("kind", sorted(VALUES))
def test_typed_words_that_ask_for_contact_details_keep_them_in_a_chart_label(tmp_path, monkeypatch, made, kind):
    """The other side: "show it as a chart with their email" asks for contact details, the plan line it runs does
    not. The label keeps the stored value, as it does for a direct question that asks for them."""
    status, raw, shown = VALUES[kind]
    outs = CR.run_chain(chain_typed_mail(status), CR.ON, tmp_path, monkeypatch)
    two = outs[1]
    assert two.reply["reason"] in ("patch", "shortcut"), two.out
    assert two.reply["plan_line"] and asks_for_contact(two.reply["plan_line"]) is False
    assert made[-1]["kwargs"] == {"contact_ok": True}
    assert len(two.reply["charts"]) == 1
    seen = _seen(two, made[-1])
    assert raw in seen and shown not in seen


@pytest.mark.parametrize("memory", ["off", "shadow", "on"])
@pytest.mark.parametrize("question, kept", [(DIRECT_MAIL, True), (DIRECT_PLAIN, False)], ids=["asks", "plain"])
def test_a_direct_question_keeps_contact_details_in_a_label_only_when_it_asks(tmp_path, monkeypatch, made, memory,
                                                                             question, kept):
    """A direct question reads the same in every mode: kept when it asks for e-mail addresses, cleaned otherwise.
    On mode passes the flag of the typed words; off and shadow pass none."""
    mode = {"off": CR.OFF, "shadow": CR.SHADOW, "on": CR.ON}[memory]
    one = CR.run_chain(chain_direct(MAIL_STATUS, question), mode, tmp_path, monkeypatch)[0]
    assert one.reply["status"] == "answered" and len(one.reply["charts"]) == 1, one.out
    assert made[-1]["kwargs"] == ({"contact_ok": kept} if memory == "on" else {})
    seen = _seen(one, made[-1])
    assert ("status-desk@example.invalid" in seen) is kept and (EMAIL_SHOWN in seen) is not kept


# ------------------------------------------------------------------------------------------------ off, shadow and v1
@pytest.mark.parametrize("memory", ["off", "shadow", "on"])
def test_a_v1_call_passes_no_flag_in_any_mode(tmp_path, monkeypatch, made, memory):
    """A contract-1 call (the platform's default bridge) has no memory part in any mode, even ``SFA_MEMORY=on``: the
    chart step gets no flag and reads the question, as before."""
    mode = {"off": CR.OFF, "shadow": CR.SHADOW, "on": CR.ON}[memory]
    w = CR.World(tmp_path, monkeypatch, mode, _world(MAIL_STATUS))
    try:
        for question, kept in ((DIRECT_MAIL, True), (DIRECT_PLAIN, False)):
            w.client.record = [{"name": "group_records", "args": _by_status("Dept One")}, SHOWN.chart]
            out = w.service._job(question, [])
            reply = w.service._final(out)
            assert set(reply) == CR.V1_KEYS and len(reply["charts"]) == 1, out
            assert made[-1]["kwargs"] == {} and made[-1]["question"] == question
            seen = json.dumps(reply["charts"]) + _pictures(made[-1]["items"])
            assert ("status-desk@example.invalid" in seen) is kept
    finally:
        w.close()


@pytest.mark.parametrize("build", [chain_plan_line_word, chain_typed_word, chain_typed_mail],
                         ids=["plan-line-word", "typed-word", "typed-mail"])
def test_shadow_answers_the_chains_as_off_and_the_chart_step_gets_no_flag(tmp_path, monkeypatch, made, build):
    """Off and shadow are as before: shadow answers byte for byte as off (the chain runner's parity check: the six
    result keys and every model request). What the second step gives is pinned as f90cd21b gives it.

    - plan-line-word: the rewrite check refuses a rewrite that adds the contact word "Phone" the typed "visualize it"
      lacks. No chart is made, so this case pins only that refusal; the flag and picture checks below compare empty
      lists for it.
    - typed-word: the typed "chart it for the Phone Support department" holds the contact word itself, so the rewrite
      is accepted and the chart step runs in off and shadow with a question that reads as a contact ask. It gets no
      flag, reads that question as before this lane, and the label keeps the value.
    - typed-mail: the rewrite of "... with their email" asks for e-mail addresses; the chart step gets no flag and the
      label keeps the value.

    In the two typed cases the chart step runs once per mode, gets no flag, and makes equal pictures off and in
    shadow."""
    chain = build(MAIL_STATUS)
    off = CR.run_chain(chain, CR.OFF, tmp_path / "off", monkeypatch)
    off_made = list(made)
    shadow = CR.run_chain(chain, CR.SHADOW, tmp_path / "shadow", monkeypatch)
    shadow_made = made[len(off_made):]
    CR.check_parity(chain, off, shadow)
    assert all(c["kwargs"] == {} for c in made)
    assert [(c["question"], c["items"]) for c in off_made] == [(c["question"], c["items"]) for c in shadow_made]
    two = off[1]
    if build is chain_plan_line_word:
        assert two.out["reason"] == "rewrite_contact_changed" and two.reply["status"] == "unclear", two.out
        assert not two.reply["charts"] and not off_made and not shadow_made
        return
    assert len(off_made) == len(shadow_made) == 1, (off_made, shadow_made)
    assert len(two.reply["charts"]) == 1 and asks_for_contact(off_made[-1]["question"]) is True
    assert off_made[-1]["question"] == chain["steps"][1]["v1"]["rewrite"]
    assert "status-desk@example.invalid" in _seen(two, off_made[-1])
    assert "status-desk@example.invalid" in _seen(shadow[1], shadow_made[-1])
    if build is chain_typed_word:
        assert asks_for_contact(chain["steps"][1]["q"]) is True and "Phone" in off_made[-1]["question"]
