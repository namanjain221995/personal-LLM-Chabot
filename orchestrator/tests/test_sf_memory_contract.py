"""Contract v2 of ask_salesforce across both halves, end to end and model-free.

Salesforce conversation memory, stage-2 integration (2026-11-25). The bridge
(engines/sfk_bridge.py with app/sf_memory.py) and the service
(sf_assistant/web/ask_mcp.py) each have their own tests against a fake of the
other side. This file and sf_assistant/tests/test_memory_contract.py pin both to
ONE wire record instead: sf_assistant/tests/fixtures/memory_contract_v2.json, a
made-up three-turn Salesforce chat (a records question, a follow-up the service
answers with a clarifying question, then the reply "2").

Here the chat runs through /chat with SF_ASSISTANT_MEMORY true. The fake service
on a real Unix socket answers each turn with the fixture's `reply` (which the
real service wrote from the fixture's `call`). Every answer is stored, its row
is written to the test PostgreSQL, and the next turn's lookup reads it back from
there, so the state the bridge sends has made the real jsonb round trip. The
arguments of every call must equal the fixture's `call`. The sf_assistant test
feeds the same arguments to the real service and checks that it accepts them
and answers with the fixture's `reply`. Each side also writes the limits, texts
and codes it uses into the fixture (`bridge` here, `service` there) and checks
the other side's.

With SF_MEMORY_CONTRACT_WRITE=1 the chat test writes `bridge` and the calls it
can capture into the fixture instead of comparing (the fixture's "regenerate"
says the order), and skips. No model call; every name and value is made up.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, sf_isolation, sf_memory, sf_memory_store
from app import main as app_main
from app.engines import sfk_bridge
from app.main import ChatRequest, app
from tests.test_sf_assistant_bridge import (  # noqa: F401 — fixtures and the fake service's wire
    _answers_in,
    _clean_registry,
    admin,
    memory_on,
    older_route,
    service,
    tool_reply,
)

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = REPO / "sf_assistant" / "tests" / "fixtures" / "memory_contract_v2.json"
WRITE = os.environ.get("SF_MEMORY_CONTRACT_WRITE") == "1"
CHAT = "conv-contract-v2"
QUESTIONS = ["how many interviews by status this year", "What about candidates?", "2"]
#: The keys of a contract-v2 call (design 2026-11-25 §4.1).
V2_ARGUMENTS = {"question", "recent_turns", "request_id", "contract", "state", "gap", "summary", "access_profile"}
V1_ARGUMENTS = {"question", "recent_turns", "request_id"}
#: The keys of a contract-v2 result (§4.2): the six of v1 and four more.
V2_RESULT = {"status", "answer", "kind", "data_ts", "objects", "charts", "contract", "state", "plan_line", "reason"}
#: The statuses whose answer the bridge shows (`_try_assistant`); any other leaves the turn to the older route.
SHOWN = ("answered", "refused", "unclear")
#: Never in meta: what a reply says for memory goes to `Outcome.sf_memory` only (C2).
MEMORY_KEYS = {"contract", "state", "plan_line", "reason"}
#: What a turn with no reply in the fixture yet is answered with while the fixture is written.
NO_REPLY = {"status": "failed", "answer": "", "kind": "none", "data_ts": None, "objects": [], "charts": []}


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _save(doc: dict) -> None:
    """The fixture's one format, the same in both writers: indent 2, UTF-8, a final newline."""
    FIXTURE.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _bridge_block() -> dict:
    """What this half uses on the wire: the v1 fallback's text, the statuses it
    reads, the fixed lines it knows, its limits on what it sends and stores, the
    reply keys memory keeps, and the code rule it reads `reason` and `kind` by."""
    return {
        "v1_arguments": sorted(V1_ARGUMENTS),
        "v1_refusal": sfk_bridge._ASK_V1_REFUSAL,
        "statuses": list(sfk_bridge._ASK_STATUSES),
        "shown_statuses": list(SHOWN),
        "fixed_lines": dict(sfk_bridge._ASK_FIXED_LINES),
        "refused_part_line": sfk_bridge._ASK_REFUSED_PART_LINE,
        "max_question": sfk_bridge._ASK_MAX_QUESTION,
        "v1_turns": sfk_bridge._ASK_MAX_TURNS,
        "max_turns": sf_memory.MAX_TURNS,
        "turn_max_chars": sf_memory.TURN_MAX_CHARS,
        "summary_max_chars": sf_memory.SUMMARY_MAX_CHARS,
        "max_gap": sf_memory.MAX_GAP,
        "contract": sf_memory.CONTRACT,
        "state_max_bytes": sf_memory_store.STATE_MAX_BYTES,
        "plan_line_max_chars": sf_memory_store.PLAN_LINE_MAX_CHARS,
        "store_summary_max_chars": sf_memory_store.SUMMARY_MAX_CHARS,
        "memory_reply_keys": sorted(sfk_bridge._memory_reply({})),
        "clarify_reason": "clarify",
        "code_max_chars": 32,
    }


def _normal(call: dict, turn: int) -> dict:
    """A captured call as the fixture keeps it: the trace's request id, which
    differs on every run, becomes the placeholder `req-<turn>`."""
    assert isinstance(call.get("request_id"), str) and call["request_id"], "the bridge sent no request id"
    return {**call, "request_id": f"req-{turn}"}


def _stored_rows() -> list:
    with db.connection() as con:
        return con.execute("SELECT * FROM sf_query_state WHERE conversation_id = %s ORDER BY id", (CHAT,)).fetchall()


def _run_chat(fake, turns: list, owner_id: int) -> dict:
    """The chat through /chat, one POST a turn with the active path the browser
    sends (the stored answers). A turn with no reply in the fixture yet is
    answered "failed" (the older route then answers) and ends the run: only
    its call is kept."""
    path: list = []
    out: dict = {"calls": [], "v1": [], "rows": [], "answers": [], "stored": [], "lookups": []}
    with TestClient(app) as client:
        for index, turn in enumerate(turns):
            reply = turn.get("reply")
            fake.body = tool_reply(reply if reply is not None else NO_REPLY)
            messages = [*path, {"role": "user", "content": turn["question"]}]
            request = ChatRequest(messages=messages, mode="salesforce", conversation_id=CHAT)
            # The switch-off call of the same path, and what the lookup finds before this turn.
            out["v1"].append(sfk_bridge._recent_turns(request))
            found = asyncio.run(sf_memory.lookup(SimpleNamespace(user_id=owner_id), request))
            out["lookups"].append(None if found is None else
                                  {"outcome": found.outcome, "gap": found.gap, "pending": found.pending})
            before = len(fake.calls)
            resp = client.post("/chat", json={"messages": messages, "mode": "salesforce", "conversation_id": CHAT})
            assert resp.status_code == 200
            assert len(fake.calls) == before + 1, f"turn {index + 1}: {len(fake.calls) - before} calls"
            out["calls"].append(fake.calls[-1]["body"]["params"]["arguments"])
            if reply is None:
                break
            out["answers"].append(_answers_in(resp.text))
            rows = _stored_rows()
            assert len(rows) == index + 1, f"turn {index + 1}: {len(rows)} rows"
            out["rows"].append(rows[-1])
            stored = app_main._persisted_answer(CHAT, rows[-1]["generation_id"])
            assert stored is not None
            out["stored"].append(stored)
            path = [*messages, {"role": "assistant", "content": stored["content"]}]
    return out


def test_the_bridge_sends_the_fixtures_calls_and_reads_its_replies(
        service, admin, older_route, memory_on):  # noqa: F811 — fixtures imported above
    doc = _load()
    turns = doc["turns"]
    assert [t["question"] for t in turns] == QUESTIONS
    ran = _run_chat(service, turns, admin["id"])
    calls = [_normal(call, i + 1) for i, call in enumerate(ran["calls"])]

    if WRITE:
        doc["bridge"] = _bridge_block()
        for index, call in enumerate(calls):
            turns[index]["call"] = call
        _save(doc)
        pytest.skip(f"wrote the bridge block and {len(calls)} call(s) into {FIXTURE.name}")

    assert doc["bridge"] == _bridge_block(), "the bridge's limits, texts or codes changed: regenerate the fixture"
    assert all("call" in t and "reply" in t for t in turns), "the fixture is not complete: regenerate it"
    assert len(calls) == len(turns) and older_route == []
    for index, (call, turn) in enumerate(zip(calls, turns)):
        assert call == turn["call"], f"turn {index + 1}: the bridge sent other arguments than the fixture's"

    for index, (call, turn) in enumerate(zip(calls, turns)):
        reply = turn["reply"]
        # The call: contract v2 with every key, a null access profile, the limits held.
        assert set(call) == V2_ARGUMENTS and type(call["contract"]) is int and call["contract"] == 2
        assert call["access_profile"] is None and call["summary"] == ""   # the fold is off by default
        assert type(call["gap"]) is int and call["gap"] == 0
        assert call["question"] == turn["question"].strip()
        assert len(call["recent_turns"]) == 2 * index <= sf_memory.MAX_TURNS
        assert all(set(t) == {"role", "content"} and len(t["content"]) <= sf_memory.TURN_MAX_CHARS
                   for t in call["recent_turns"])
        # The window's last four turns are the switch-off call's (F1 floor, C31): shadow reads what it reads today.
        assert call["recent_turns"][-sf_memory.V1_TURNS:] == ran["v1"][index]
        # The state sent is the reply before it, after the store's jsonb round trip.
        assert call["state"] == (turns[index - 1]["reply"]["state"] if index else None)
        # The reply as the bridge read it: shown, stored with its state and plan line, nothing new in meta.
        assert set(reply) == V2_RESULT and reply["status"] in SHOWN and reply["contract"] == 2
        assert ran["answers"][index] == [reply["answer"]]
        stored = ran["stored"][index]
        assert stored["content"] == reply["answer"] and stored["meta"].get("engine") == "sf_assistant"
        assert not MEMORY_KEYS & set(stored["meta"]), "a memory key reached the stored meta"
        row = ran["rows"][index]
        assert row["contract"] == 2 and row["state"] == reply["state"] and row["plan_line"] == reply["plan_line"]
        assert row["path_pairs"] == index + 1
        assert row["question_sha1"] == sf_isolation.answer_key(turn["question"])
        assert row["answer_sha1"] == sf_isolation.answer_key(stored["content"])
        assert row["parent_sha1"] == (ran["rows"][index - 1]["answer_sha1"] if index else "")
        # The next turn's path ends with this answer as stored, which is what the service reads it from.
        if index + 1 < len(turns):
            assert calls[index + 1]["recent_turns"][-1] == {"role": "assistant", "content": stored["content"]}

    # Before each turn the lookup found: nothing, the records answer, then its clarifying question still pending.
    assert ran["lookups"] == [
        {"outcome": "none", "gap": 0, "pending": False},
        {"outcome": "found", "gap": 0, "pending": False},
        {"outcome": "found", "gap": 0, "pending": True},
    ]
    assert turns[1]["reply"]["reason"] == "clarify" and turns[1]["reply"]["state"]["pending"]


def test_the_bridge_reads_every_code_and_status_the_service_says_it_sends():
    """The other half's block (written by the sf_assistant test): every reason
    and kind is a code the bridge keeps, every status one it knows, and the
    limits and texts are the bridge's own."""
    svc = _load()["service"]
    assert svc is not None, "the fixture has no service block: regenerate it"
    for code in [*svc["reasons"], *svc["kinds"]]:
        assert sf_memory._code(code) == code, code
    assert sf_memory._code("a" * 32) == "a" * 32 and sf_memory._code("a" * 33) == ""
    assert "clarify" in svc["reasons"]
    assert set(svc["statuses"]) <= set(sfk_bridge._ASK_STATUSES)
    assert svc["unknown_keys_error"] == sfk_bridge._ASK_V1_REFUSAL
    assert set(svc["arguments"]) == V2_ARGUMENTS and set(svc["v1_arguments"]) == V1_ARGUMENTS
    assert set(svc["result_keys"]) == V2_RESULT and set(sfk_bridge._memory_reply({})) <= set(svc["result_keys"])
    assert svc["contracts"] == [1, sf_memory.CONTRACT]
    assert svc["fixed_lines"] == sfk_bridge._ASK_FIXED_LINES
    assert svc["refused_part_line"] == sfk_bridge._ASK_REFUSED_PART_LINE
    assert svc["max_question"] == sfk_bridge._ASK_MAX_QUESTION
    assert svc["max_turns"] == sf_memory.MAX_TURNS
    assert svc["max_turn_chars"] == sf_memory.TURN_MAX_CHARS == sfk_bridge._ASK_MAX_TURN_CHARS
    assert svc["rewrite_turns"] == sf_memory.V1_TURNS == sfk_bridge._ASK_MAX_TURNS
    assert svc["max_summary"] == sf_memory.SUMMARY_MAX_CHARS == sf_memory_store.SUMMARY_MAX_CHARS
    assert svc["max_gap"] == sf_memory.MAX_GAP
    assert svc["state_max_bytes"] == sf_memory_store.STATE_MAX_BYTES
    assert svc["plan_line_max_chars"] == sf_memory_store.PLAN_LINE_MAX_CHARS


def test_a_clarifying_reply_keeps_its_text_for_the_fold_by_the_reason_the_service_sends():
    """`Memory.record` keeps the answer as the clarify text only for the reason
    code `clarify`, the one the service sends with its clarifying question;
    a reply without `contract: 2` writes no row."""
    reply = _load()["turns"][1].get("reply")
    assert reply is not None, "the fixture is not complete: regenerate it"
    memory = sf_memory.Memory(question="q", question_sha1="", parent_sha1="")
    record = memory.record(sfk_bridge._memory_reply(reply), reply["answer"])
    assert record is not None and record["clarify"] == reply["answer"] and record["reason"] == "clarify"
    assert record["kind"] == reply["kind"] and record["state"] == reply["state"]
    assert record["plan_line"] == reply["plan_line"]
    v1 = {key: reply[key] for key in ("status", "answer", "kind", "data_ts", "objects", "charts")}
    assert memory.record(sfk_bridge._memory_reply(v1), reply["answer"]) is None
