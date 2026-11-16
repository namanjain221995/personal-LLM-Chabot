"""The term resolver's wiring through ``pipeline.answer``, steps (1) and (6) (docs/resolver_integration.md): the
knowledge step gets the question's spelling block, the final answer gets the "Read as" line (never ``body``), and
the JSONL keeps the user's words plus the spelling rows.

These tests need the hook in ``pipeline.answer`` (``question_spelling``; ``Outcome.spelling``) and are skipped until
it is there. With the switch off (the first test) the outcome is exactly the one without the spelling help. Every
test sets the switch itself. Fake data only: tests/warehouse_fixture.py, tests/fixtures/Internal_Interview__c.json,
a made-up word list and a fake model.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant import config as C  # noqa: E402
from sf_assistant import pipeline  # noqa: E402
from sf_assistant.config import ResolverConfig, load_config, load_records_config  # noqa: E402

pytestmark = pytest.mark.skipif(not any(f.name == "spelling" for f in dataclasses.fields(pipeline.Outcome)),
                                reason="pipeline.answer has no spelling-help hook yet (question_spelling)")

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H") + \
    tuple(C.RESOLVER_ENV_VARS.values())
WORDS = ("a about all and are by count did does end ended for has have how is many me of offer records show take "
         "taken tell the to was were what when where which who why with work mean means used list").split()
SOURCE = "Source: preprod sandbox copy, synced 2026-10-13 14:30 UTC."
OFFER_ARGS = {"object": "Interview__c", "filters": [{"field": "Interview_Outcome__c", "op": "eq",
                                                      "value": "Offer Received"}]}
RECORD_ANSWER = "2 client interviews ended with an offer (`Interview_Outcome__c` = 'Offer Received').\n" + SOURCE
MERGED_ANSWER = "2 client interviews ended with an offer: `Interview_Outcome__c` = 'Offer Received'.\n" + SOURCE
TYPO_Q = "how many interviews ended with Ofer Recieved"
CLEAN_Q = "how many interviews ended with Offer Received"


#: A local port nothing listens on: the record session accepts it (local), and no client could reach a model there.
NO_MODEL_URL = "http://127.0.0.1:9/v1"


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    """Every model call goes to :class:`FakeLLM`; a real client fails the test instead of calling anything."""
    from sf_assistant.agent import llm as L

    def refuse(self, *a, **kw):
        raise AssertionError("a real model client was called")

    monkeypatch.setattr(L.LLM, "chat", refuse)
    monkeypatch.setattr(L.LLM, "chat_json", refuse)


class FakeLLM:
    """One fake for every role, routed by the system prompt (as tests/test_pipeline_cli.py's)."""

    def __init__(self):
        self.knowledge_users: list[str] = []
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    def chat(self, messages, tools=None, **_):
        system = str(messages[0].get("content") or "")
        n = sum(1 for m in messages if m.get("role") == "tool")
        usage = {"prompt_tokens": 5, "completion_tokens": 1}
        if system.startswith("Decide whether answering"):
            return {"content": "RECORDS", "tool_calls": [], "usage": usage}
        if system.startswith("List the names of people"):
            return {"content": "NONE", "tool_calls": [], "usage": usage}
        if system.startswith("You write the ONE final answer"):
            return {"content": MERGED_ANSWER, "tool_calls": [], "usage": usage}
        if system.startswith("You answer questions about Salesforce RECORDS"):
            if n == 0:
                return {"content": None, "tool_calls": [{"id": "r0", "name": "count_records", "arguments": OFFER_ARGS}],
                        "usage": usage}
            return {"content": RECORD_ANSWER, "tool_calls": [], "usage": usage}
        self.knowledge_users.append(str(messages[1].get("content")))
        names = [t["function"]["name"] for t in tools or []]
        if names and n == 0:
            tool = "search_all_knowledge" if "search_all_knowledge" in names else names[0]
            call = {"id": "k0", "name": tool, "arguments": {"query": "offer", "k": 3}}
            return {"content": None, "tool_calls": [call], "usage": usage}
        return {"content": "`Interview_Outcome__c` = 'Offer Received' marks an offer.", "tool_calls": [],
                "usage": usage}


def _setup(base: Path, on: bool, llm: "FakeLLM", records: bool = True):
    pytest.importorskip("duckdb")
    import warehouse_fixture as F
    from sf_assistant.agent.record_session import RecordSession
    from sf_assistant.tools.open_records import open_records

    base.mkdir(parents=True, exist_ok=True)
    words = base / "words.txt"
    words.write_text("\n".join(WORDS) + "\n", encoding="utf-8")
    rc = ResolverConfig(enabled=on, lexicon=(str(words),))
    kn = F.make_knowledge(base)
    settings = dataclasses.replace(C.load_settings(records=False), resolver=rc)
    cfg = load_config(base_url=NO_MODEL_URL, api_key="test-key", max_tool_steps=6)
    _cfg, _objs, registry, system, _llm = pipeline.build(sorted(str(p) for p in kn.glob("*.json")), settings)
    session = None
    if records:
        tools = open_records(load_records_config(warehouse=str(F.make_warehouse(base)), today=dt.date(2026, 10, 13)),
                             str(kn), rc)
        session = RecordSession(tools, cfg, llm=llm, max_steps=6)
    return cfg, registry, system, session


def _answer(base, text, on, records=True, **kw):
    llm = FakeLLM()
    cfg, registry, system, session = _setup(base, on, llm, records)
    try:
        o = pipeline.answer(text, cfg, registry, system, llm, 1, session, **kw)
        row = pipeline.result_row(o, session)
    finally:
        if session is not None:
            session.tools.close()
    return o, row, llm


def test_switch_off_answer_has_no_block_no_line_no_spelling_key(tmp_path):
    o, row, llm = _answer(tmp_path, TYPO_Q, on=False)
    assert o.spelling is None and o.footed is None
    assert all(u == TYPO_Q for u in llm.knowledge_users) and llm.knowledge_users
    assert "Read as:" not in o.final and "spelling" not in row["jsonl"] and "read_as_line" not in row["jsonl"]
    assert row["jsonl"]["lookup_footer"] == (o.final != o.body)


def test_switch_on_merged_answer_states_the_correction_in_final_only(tmp_path):
    o, row, llm = _answer(tmp_path, TYPO_Q, on=True, lean_knowledge=False)
    assert o.kind == "merged", (o.kind, o.m)
    block = o.spelling.block
    assert "- 'Ofer Recieved' -> Offer Received (value of Interview__c.Interview_Outcome__c)" in block
    assert llm.knowledge_users and all(u == TYPO_Q + "\n\n" + block for u in llm.knowledge_users)
    assert o.q.question == TYPO_Q and o.text == TYPO_Q and row["jsonl"]["question"] == TYPO_Q
    assert o.final == o.body.replace("\n" + SOURCE, "\nRead as: 'Ofer Recieved' as Offer Received.\n" + SOURCE)
    assert "Read as" not in o.body and o.final.endswith(SOURCE)
    assert row["jsonl"]["spelling"] == [{"typed": "Ofer Recieved", "decision": "corrected", "used": "Offer Received",
                                         "options": []}]
    assert row["jsonl"]["read_as_line"] is True and row["jsonl"]["lookup_footer"] is False
    lean, _row, lean_llm = _answer(tmp_path / "lean", TYPO_Q, on=True)   # the lean knowledge step gets the block too
    assert lean.mode in ("lean", "lean+full") and lean_llm.knowledge_users[0] == TYPO_Q + "\n\n" + block


def test_switch_on_records_off_knowledge_answer_gets_the_line(tmp_path):
    o, row, llm = _answer(tmp_path, "what does Ofer Recieved mean", on=True, records=False)
    assert o.kind == "knowledge" and o.spelling.records == []
    assert llm.knowledge_users[0].startswith("what does Ofer Recieved mean\n\nSpelling (read by code")
    assert o.final.endswith("\nRead as: 'Ofer Recieved' as Offer Received.") and "Read as" not in o.body


def test_switch_on_a_correct_question_is_answered_as_with_the_switch_off(tmp_path):
    on, row_on, llm_on = _answer(tmp_path / "on", CLEAN_Q, on=True)
    off, row_off, llm_off = _answer(tmp_path / "off", CLEAN_Q, on=False)
    assert on.spelling.knowledge == [] and on.spelling.records == []
    assert (on.final, on.body, on.kind) == (off.final, off.body, off.kind)
    assert llm_on.knowledge_users == llm_off.knowledge_users == [CLEAN_Q] * len(llm_off.knowledge_users)
    assert row_on["jsonl"]["spelling"] == [] and row_on["jsonl"]["read_as_line"] is False
