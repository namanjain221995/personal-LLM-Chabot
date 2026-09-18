"""A record question the router handed to "chat" goes to the SQL engine.

QA, 2026-09-18: "Does the interview record for Priya exist and when was it
last updated?" forced onto the chat class answered "I cannot access
Salesforce data" 3 of 3 times, and 2 of 3 also sent the person to "check the
'Interview Records' object in your Salesforce org directly" — both forbidden
by SALESFORCE_ASSISTANT_SYSTEM's own rule, because graph._chat_node never
looked at the message. The live router classifies that exact message as sql
today, so this is the net under a router miss, not the main path.

The net must not catch ordinary questions: the chat class in Salesforce mode
is where every general question lands (mode-and-tone sweep), and a general
question answered by the SQL engine is a worse failure than a record question
answered by the assistant. Hence the near-miss set below, built from the
standard objects whose names are everyday words (task, event, case, lead).

Offline: the dictionary is a synthetic export, and both engines are stubs.
"""
from __future__ import annotations

import asyncio

import pytest

from app import graph
from app.core import sf_dictionary as sd

ROWS = [
    ("Interview__c", "Interview", "Interview_Status__c", "Interview Status", "picklist"),
    ("Interview__c", "Interview", "Candidate__c", "Candidate", "reference"),
    ("Interview__c", "Interview", "LastModifiedDate", "Last Modified Date", "datetime"),
    ("Candidate__c", "Candidate", "Placed_Date__c", "Placed Date", "date"),
    ("Account", "Account", "AnnualRevenue", "Annual Revenue", "currency"),
    ("Contact", "Contact", "Email", "Email", "email"),
    ("Opportunity", "Opportunity", "StageName", "Stage", "picklist"),
    ("Lead", "Lead", "Status", "Lead Status", "picklist"),
    ("Case", "Case", "Status", "Status", "picklist"),
    ("CaseStatus", "Case Status", "MasterLabel", "Master Label", "string"),
    ("Task", "Task", "Subject", "Subject", "string"),
    ("Event", "Event", "Subject", "Subject", "string"),
    ("UserProvMockTarget", "User Prov Mock Target", "Name", "Name", "string"),
]

PRIYA = "Does the interview record for Priya exist and when was it last updated?"

#: The ten ordinary asks the mode-and-tone QA ran in both modes (index vs
#: materialized view, ISO-8601 parser, interview questions, flowchart,
#: remote-first, 45 -> 25 days, translation, capital, pick-one, LinkedIn).
QA_TEN = [
    "What's the difference between a database index and a materialized view, and when should I use each?",
    "Write a Python function that parses an ISO-8601 timestamp and returns a timezone-aware datetime.",
    "Give me ten good interview questions for a senior backend engineer.",
    "Draw a flowchart of our candidate interview process, from application to offer.",
    "What are the pros and cons of running a remote-first company?",
    "Our time-to-hire is 45 days. How can we get it down to 25?",
    "Translate 'Welcome to the team, we are glad you joined us' into Hindi.",
    "What is the capital of Australia?",
    "Kubernetes or Docker Swarm for a five-person team? Pick one.",
    "Write a LinkedIn post announcing that we are hiring two senior recruiters.",
]

#: General questions that DO carry a data verb and an object's name — the
#: shapes a looser rule would have pulled into SQL.
NEAR_MISSES = [
    "List ten interview questions for our senior backend role.",
    "Show me a flowchart of our interview process.",
    "How many interview rounds should a startup run?",
    "How many tasks should a two-week sprint have?",
    "List the tasks for a Kubernetes migration.",
    "Show me how to create a report of open opportunities.",
    "What's the status of our hiring plan?",
    "How many users does Slack have?",
    "List 5 events for team building this year.",
    "Can you show me an example of a case study for our website?",
    "How many leads should we expect from a trade show?",
    "Count the vowels in the word interview.",
]

#: Record questions a router miss could hand to the chat class.
RECORD_QUESTIONS = [
    PRIYA,
    "How many candidates did we place last month?",
    "How many interviews are scheduled for today?",
    "Can you show me our open opportunities?",
    "List all leads created this week.",
    "When was the Acme account last updated?",
    "What is the status of the interview for Priya Sharma?",
    "Does a contact record exist for Rahul Mehta?",
]


@pytest.fixture(autouse=True)
def dictionary(tmp_path):
    path = tmp_path / "dict.json"
    sd.save(sd.build_from_rows(ROWS), str(path))
    sd._cache = None
    sd.load(str(path))
    yield
    sd._cache = None


def _node(monkeypatch, message: str) -> list:
    """Run graph._chat_node with both engines stubbed; return which ran."""
    ran = []

    async def fake_sql(msg, history, emit, **kwargs):
        ran.append(("sql", msg, list(history)))
        await emit("meta", {"route": "sql"})
        return "sql answer"

    async def fake_chat(msg, history, emit, **kwargs):
        ran.append(("chat", msg, kwargs.get("mode")))
        await emit("meta", {"route": "chat"})
        return "chat answer"

    import app.engines.chat as chat_mod
    import app.engines.sql as sql_mod

    monkeypatch.setattr(sql_mod, "run_sql_engine", fake_sql)
    monkeypatch.setattr(chat_mod, "run_chat_engine", fake_chat)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    history = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]
    state = {"message": message, "history": history, "emit": emit, "model_choice": "smart", "effort": "fast"}
    out = asyncio.run(graph._chat_node(state))
    assert out["answer"] in ("sql answer", "chat answer")
    assert [k for k, _ in events] == ["meta"], "exactly one engine answers, once"
    return ran


def test_the_priya_record_question_is_dispatched_to_the_sql_engine(monkeypatch):
    ran = _node(monkeypatch, PRIYA)
    assert [r[0] for r in ran] == ["sql"]
    assert ran[0][1] == PRIYA
    # The engine gets the conversation, as the sql node gives it.
    assert ran[0][2] == [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]


@pytest.mark.parametrize("message", RECORD_QUESTIONS)
def test_a_record_question_naming_a_synced_object_goes_to_sql(monkeypatch, message):
    assert [r[0] for r in _node(monkeypatch, message)] == ["sql"]


@pytest.mark.parametrize("message", QA_TEN + ["hi", "thanks!"])
def test_the_ordinary_salesforce_mode_asks_stay_on_chat(monkeypatch, message):
    ran = _node(monkeypatch, message)
    assert [r[0] for r in ran] == ["chat"]
    assert ran[0][2] == "salesforce"


@pytest.mark.parametrize("message", NEAR_MISSES)
def test_a_general_question_with_a_data_verb_and_an_object_word_stays_on_chat(monkeypatch, message):
    assert [r[0] for r in _node(monkeypatch, message)] == ["chat"]


def test_without_a_dictionary_nothing_is_dispatched(monkeypatch, tmp_path):
    """No org export loaded means no object can be named: the node behaves
    exactly as it did."""
    sd._cache = None
    sd.load(str(tmp_path / "missing.json"))
    assert [r[0] for r in _node(monkeypatch, PRIYA)] == ["chat"]


def test_setup_objects_sharing_the_name_do_not_crowd_out_the_synced_one(monkeypatch):
    """relevant_objects keeps four objects by default. The export also lists
    setup objects named after interviews (FlowInterview, FlowInterviewLog...)
    and each field labelled "Interview ..." scores on the question, so four of
    them outrank Interview__c and the one synced object would be dropped."""
    rows = list(ROWS)
    for api in ("FlowInterview", "FlowInterviewLog", "FlowInterviewLogEntry", "FlowInterviewSetup", "FlowInterviewStage"):
        label = " ".join(api.replace("Flow", "Flow ").replace("Interview", "Interview ").split())
        rows += [(api, label, f"Interview_Field_{k}", f"Interview Field {k}", "string") for k in range(10)]
    sd._cache = sd.build_from_rows(rows)
    assert "Interview__c" not in [o["api"] for o in sd.relevant_objects(PRIYA)]
    assert [r[0] for r in _node(monkeypatch, PRIYA)] == ["sql"]


def test_a_setup_object_is_not_a_synced_record(monkeypatch):
    """UserProvMockTarget is in the export but not in the warehouse."""
    message = "How many user prov mock target records exist in our org?"
    assert [r[0] for r in _node(monkeypatch, message)] == ["chat"]
