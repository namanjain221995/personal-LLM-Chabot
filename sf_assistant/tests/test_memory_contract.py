"""Contract v2 of ask_salesforce across both halves, end to end and model-free (Salesforce conversation memory,
stage-2 integration, 2026-11-25).

``web/ask_mcp.py`` and the platform's bridge (``orchestrator/app/engines/sfk_bridge.py`` with
``orchestrator/app/sf_memory.py``) each have their own tests against a fake of the other side. This file and
``orchestrator/tests/test_sf_memory_contract.py`` pin both to ONE wire record instead:
``tests/fixtures/memory_contract_v2.json``, a made-up three-turn Salesforce chat (a records question, a follow-up the
service answers with a clarifying question, then the reply "2").

The orchestrator test runs that chat through /chat with ``SF_ASSISTANT_MEMORY`` true and checks that the bridge sends
exactly the fixture's ``call`` of each turn (the state of the turn before read back from the test PostgreSQL, so after
the real jsonb round trip) and reads the fixture's ``reply``. Here every one of those calls goes to the real service
over its real transport (POST /mcp on a Unix socket with the bridge's headers and JSON-RPC body), with the real
pipeline, ``cli.ask``, record agent and record tools on the memory fixture and a scripted model
(``tests/chain_runner.py``'s ``World``), and the reply is read as the bridge reads it. With ``SFA_MEMORY=on`` each call
is accepted (no shape fault, no input problem, the stored state checked as usable) and answered with the fixture's
``reply`` (``state.made_at`` aside); with ``off`` and ``shadow`` the six v1 keys of the answers are byte for byte the
same, ``off`` answers as v1 (so the bridge stores no row) and shadow adds the four v2 keys. Each side also writes the
limits, texts and codes it uses into the fixture (``service`` here, ``bridge`` there) and checks the other side's.

With ``SFA_MEMORY_CONTRACT_WRITE=1`` the on-mode test writes ``service`` and the replies of the calls it finds into
the fixture instead of comparing (the fixture's "regenerate" says the order), and skips. No model of any kind is
called; every name and value is made up.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import chain_runner as CR   # puts web/ and tests/ on the path

import ask_mcp  # noqa: E402
import pipeline as pipeline_mod  # noqa: E402

from sf_assistant.agent import followup  # noqa: E402
from sf_assistant.agent import query_state as Q  # noqa: E402
from sf_assistant.config import MemoryConfig  # noqa: E402
from sf_assistant.server.auth import TokenStore, write_tokens  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "memory_contract_v2.json"
WRITE = os.environ.get("SFA_MEMORY_CONTRACT_WRITE") == "1"
QUESTIONS = ["how many interviews by status this year", "What about candidates?", "2"]
V1_KEYS, V2_KEYS = CR.V1_KEYS, CR.V2_KEYS
#: Every model role but the resolver's: off and shadow must make the same requests of these.
ANSWER_ROLES = tuple(r for r in CR.ROLES if r != "resolve")


def _load() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _save(doc: dict) -> None:
    """The fixture's one format, the same in both writers: indent 2, UTF-8, a final newline."""
    FIXTURE.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _statuses() -> list[str]:
    """Every status this service can send: the literal first argument of each ``_result(...)`` in ``ask_mcp.py`` and
    each status :func:`ask_mcp.decide` returns (read from the source, so a new status shows up here)."""
    tree = ast.parse(Path(ask_mcp.__file__).read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_result" and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            found.add(node.args[0].value)
        if isinstance(node, ast.FunctionDef) and node.name == "decide":
            for ret in ast.walk(node):
                if isinstance(ret, ast.Return) and isinstance(ret.value, ast.Tuple) \
                        and isinstance(ret.value.elts[0], ast.Constant):
                    found.add(ret.value.elts[0].value)
    return sorted(found)


def _plan_line_cap() -> int:
    """How many characters of a plan line the reply keeps (``_with_v2`` cuts it)."""
    return len(ask_mcp._with_v2(ask_mcp._result("answered"), None, "x" * 10_000, "records")["plan_line"])


def _service_block() -> dict:
    """What this half uses on the wire: its argument names, the refusal of an unknown key, every status, reason and
    answer kind it sends, its result keys and its limits and fixed texts."""
    return {
        "arguments": sorted(ask_mcp._ARGUMENTS),
        "v1_arguments": sorted(ask_mcp._V1_ARGUMENTS),
        "unknown_keys_error": ask_mcp.UNKNOWN_KEYS_ERROR,
        "contracts": list(ask_mcp.CONTRACTS),
        "statuses": _statuses(),
        "reasons": list(ask_mcp.REASONS),
        "kinds": sorted({*pipeline_mod.MODES, "none"}),
        "result_keys": sorted(V2_KEYS),
        "v1_result_keys": sorted(V1_KEYS),
        "fixed_lines": {"refused": ask_mcp.REFUSED_LINE, "unclear": ask_mcp.UNCLEAR_LINE},
        "refused_part_line": ask_mcp.REFUSED_PART_LINE,
        "max_question": ask_mcp.MAX_QUESTION,
        "max_turns": ask_mcp.MAX_TURNS,
        "max_turn_chars": ask_mcp.MAX_TURN_CHARS,
        "rewrite_turns": ask_mcp.REWRITE_TURNS,
        "max_summary": ask_mcp.MAX_SUMMARY,
        "max_gap": ask_mcp.MAX_GAP,
        "state_max_bytes": Q.MAX_STATE_BYTES,
        "plan_line_max_chars": _plan_line_cap(),
    }


# ------------------------------------------------------------------------------------------------ the bridge's reading
# Copies of what the bridge does with a reply (the two halves share no code): orchestrator/app/engines/sfk_bridge.py
# `_strict_json` and `_tool_reply`, and orchestrator/app/sf_memory.py `_code`. The orchestrator test pins the bridge's
# side of the same rules against this fixture.

def _strict_json(raw: Any) -> Any:
    def refuse(name: str) -> None:
        raise ValueError(f"{name} is not JSON")

    return json.loads(raw, parse_constant=refuse)


def bridge_reads(raw: bytes) -> tuple[dict | None, str | None]:
    """(the ``result``, None), or (None, the refusal's ``error``), as ``sfk_bridge._tool_reply`` reads a reply."""
    reply = _strict_json(raw)["result"]
    if reply.get("isError"):
        error = _strict_json(reply["content"][0]["text"]).get("error")
        return None, error if isinstance(error, str) else None
    envelope = _strict_json(reply["content"][0]["text"])
    if envelope.get("ok") is not True or not isinstance(envelope.get("result"), dict):
        error = envelope.get("error")
        return None, error if isinstance(error, str) else None
    return envelope["result"], None


def bridge_code(value: Any, max_chars: int) -> str:
    """A reply's ``reason`` or ``kind`` as the bridge keeps it (``sf_memory._code``), "" for anything else."""
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    return value if value and len(value) <= max_chars and value.replace("_", "").isalpha() else ""


def _norm(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _without_made_at(state: Any) -> Any:
    """A state with its ``made_at`` (the clock of the run that wrote it) left out, for comparing."""
    return {k: v for k, v in state.items() if k != "made_at"} if isinstance(state, dict) else state


def _reply_view(result: dict) -> dict:
    return {**result, "state": _without_made_at(result.get("state"))}


# ------------------------------------------------------------------------------------------------ the service's wire
class Wire:
    """The service of a ``chain_runner.World`` (the page's Pipeline, the real ``cli.ask``, record agent and record
    tools on the memory fixture, one scripted model client) behind its real transport, called as the bridge calls
    it: POST /mcp on a Unix socket, Host ``sf-assistant-ask``, the bearer token, ``Accept: application/json`` and one
    JSON-RPC ``tools/call`` with no initialize."""

    def __init__(self, tmp: Path, monkeypatch: Any, memory: MemoryConfig) -> None:
        folder = tmp / "world"
        folder.mkdir(parents=True)
        self.memory = memory
        self.world = CR.World(folder, monkeypatch, memory)
        self.world.service.executor.shutdown(wait=False)   # this test serves its own AskService over the same pipeline
        token_path = tmp / "ask_token.json"
        self.token = write_tokens(str(token_path), label="test")[0]
        self.service = ask_mcp.AskService(self.world.pipe, TokenStore(str(token_path)), self.world.log, memory=memory)
        self.log_dir = folder / "ask_logs"
        self.sock_dir = tempfile.mkdtemp(prefix="sfa-c2-")   # short: a socket's path has a limit of 107 bytes
        path = os.path.join(self.sock_dir, "ask.sock")
        self.sock = ask_mcp.listening_socket(path)
        self.server = ask_mcp.make_server(self.service)
        self.client = httpx.Client(transport=httpx.HTTPTransport(uds=path), base_url=f"http://{ask_mcp.ASK_HOST}",
                                   trust_env=False)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)
        self.thread.start()
        t0 = time.monotonic()
        while not self.server.started:
            assert time.monotonic() - t0 < 30, "uvicorn did not start"
            time.sleep(0.02)

    def ask(self, turn: dict) -> tuple[bytes, dict]:
        """One call of the fixture's turn, with the turn's model script for this mode: the raw reply body and the
        model requests made, per role."""
        resolve, rewrite, record = CR._script({"q": turn["question"], **turn["script"]}, self.memory)
        client = self.world.client
        client.resolve, client.rewrite, client.record, client.route = resolve, rewrite, record, []
        before = {k: len(v) for k, v in client.requests.items()}
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": ask_mcp.TOOL, "arguments": turn["call"]}},
                             headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                             timeout=120)
        assert r.status_code == 200, r.status_code
        return r.content, {k: client.requests[k][before[k]:] for k in client.requests}

    def asks(self) -> list[dict]:
        """The ask log's ``ask`` lines, oldest first."""
        return [json.loads(ln) for path in sorted(self.log_dir.glob("ask_log-*.jsonl"))
                for ln in path.read_text(encoding="utf-8").splitlines() if json.loads(ln).get("event") == "ask"]

    def stop(self) -> None:
        self.client.close()
        self.server.should_exit = True
        self.thread.join(10)
        self.service.close()
        shutil.rmtree(self.sock_dir, ignore_errors=True)


@pytest.fixture
def wire(tmp_path, monkeypatch):
    made: list[Wire] = []

    def build(memory: MemoryConfig) -> Wire:
        w = Wire(tmp_path / f"{memory.mode}{len(made)}", monkeypatch, memory)
        made.append(w)
        return w

    yield build
    for w in made:
        w.stop()


def _turns_with_calls(doc: dict) -> list[dict]:
    out = []
    for turn in doc["turns"]:
        if "call" not in turn:
            break
        out.append(turn)
    return out


def _check_shape(result: dict, bridge: dict) -> None:
    """A contract-v2 result as the bridge reads it: the ten keys, a status it shows, ``contract`` the integer 2, a
    state it can store, a plan line within its column, and a reason and kind it keeps as codes."""
    assert set(result) == V2_KEYS
    assert result["status"] in bridge["statuses"] and result["status"] in bridge["shown_statuses"]
    assert type(result["contract"]) is int and result["contract"] == bridge["contract"]
    state = result["state"]
    assert state is None or (isinstance(state, dict) and Q.state_bytes(state) <= bridge["state_max_bytes"])
    assert isinstance(result["plan_line"], str) and len(result["plan_line"]) <= bridge["plan_line_max_chars"]
    assert result["reason"] in ask_mcp.REASONS
    assert bridge_code(result["reason"], bridge["code_max_chars"]) == result["reason"]
    assert bridge_code(result["kind"], bridge["code_max_chars"]) == result["kind"]
    assert isinstance(result["answer"], str) and result["answer"].strip() == result["answer"] and result["answer"]


# ------------------------------------------------------------------------------------------------ the tests
def test_the_fixture_holds_a_made_up_three_turn_chat():
    doc = _load()
    assert [t["question"] for t in doc["turns"]] == QUESTIONS
    text = FIXTURE.read_text(encoding="utf-8")
    assert not [m for m in re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text) if not m.endswith("@example.invalid")]
    if WRITE:
        return
    for index, turn in enumerate(doc["turns"]):
        assert {"question", "script", "call", "reply"} <= set(turn), f"turn {index + 1} is not complete: regenerate"
        assert turn["call"]["request_id"] == f"req-{index + 1}"
        for state in (turn["call"]["state"], turn["reply"]["state"]):
            # a state never holds a record Id (design §10; chain_runner's invariant)
            assert not CR._ID_RE.search(json.dumps(state)), f"turn {index + 1}: a record Id in a state"


def test_the_service_block_is_this_services_and_agrees_with_the_bridges():
    doc = _load()
    if WRITE:
        pytest.skip("the on-mode test writes the service block")
    assert doc["service"] == _service_block(), "the service's limits, texts or codes changed: regenerate the fixture"
    bridge = doc["bridge"]
    assert bridge is not None, "the fixture has no bridge block: regenerate it"
    # the v1 fallback (C34): the bridge retries with the v1 keys on exactly this refusal
    assert bridge["v1_refusal"] == ask_mcp.UNKNOWN_KEYS_ERROR
    assert ask_mcp._bad_arguments({"question": "q", "made_up_key": 1}) == bridge["v1_refusal"]
    assert set(bridge["v1_arguments"]) == ask_mcp._V1_ARGUMENTS
    # what the bridge sends and stores, within what the service takes
    assert bridge["contract"] in ask_mcp.CONTRACTS
    assert bridge["max_question"] == ask_mcp.MAX_QUESTION
    assert bridge["v1_turns"] == ask_mcp.REWRITE_TURNS == followup.MAX_TURNS
    assert bridge["max_turns"] == ask_mcp.MAX_TURNS and bridge["turn_max_chars"] == ask_mcp.MAX_TURN_CHARS
    assert bridge["summary_max_chars"] == bridge["store_summary_max_chars"] == ask_mcp.MAX_SUMMARY
    assert bridge["max_gap"] == ask_mcp.MAX_GAP
    assert bridge["state_max_bytes"] == Q.MAX_STATE_BYTES
    assert bridge["plan_line_max_chars"] == _plan_line_cap() == Q.MAX_PLAN_LINE
    # what the service sends, within what the bridge reads
    assert set(_statuses()) <= set(bridge["statuses"])
    assert bridge["fixed_lines"] == {"refused": ask_mcp.REFUSED_LINE, "unclear": ask_mcp.UNCLEAR_LINE}
    assert bridge["refused_part_line"] == ask_mcp.REFUSED_PART_LINE
    assert set(bridge["memory_reply_keys"]) <= V2_KEYS
    assert bridge["clarify_reason"] in ask_mcp.REASONS
    for code in [*ask_mcp.REASONS, *_service_block()["kinds"]]:
        assert bridge_code(code, bridge["code_max_chars"]) == code, code


def test_the_service_accepts_each_call_the_bridge_sent_and_answers_it_as_the_bridge_reads_it(wire):
    doc = _load()
    turns = _turns_with_calls(doc)
    assert turns, "the fixture holds no call: run the orchestrator test with SF_MEMORY_CONTRACT_WRITE=1 first"
    bridge = doc["bridge"]
    w = wire(MemoryConfig(mode="on"))
    results = []
    for index, turn in enumerate(turns):
        call = turn["call"]
        # the validator: no shape fault, and no v2 value the service would set aside
        assert ask_mcp._bad_arguments(call) is None
        assert ask_mcp._Request(call).problems == []
        raw, made = w.ask(turn)
        result, error = bridge_reads(raw)
        assert error is None and result is not None, f"turn {index + 1}: refused: {error}"
        _check_shape(result, bridge)
        line = w.asks()[-1]
        assert line["status"] == result["status"] and line["request_id"] == call["request_id"]
        memory = line["memory"]
        assert memory["state_in"] == ("none" if call["state"] is None else "ok"), memory
        assert "input" not in memory and memory["gap"] == call["gap"] and memory["mode"] == "on"
        results.append((result, made))

    if WRITE:
        for turn, (result, _made) in zip(turns, results):
            # a reply equal but for its clock is kept as it was, so the calls captured from it stay true
            if "reply" not in turn or _reply_view(turn["reply"]) != _reply_view(result):
                turn["reply"] = result
        doc["service"] = _service_block()
        _save(doc)
        pytest.skip(f"wrote the service block and {len(results)} repl(ies) into {FIXTURE.name}")

    assert len(results) == len(QUESTIONS)
    for index, (turn, (result, _made)) in enumerate(zip(turns, results)):
        assert _reply_view(result) == _reply_view(turn["reply"]), f"turn {index + 1}: another reply than the fixture's"
        if index + 1 < len(turns):
            # the bridge sent back the state it got, after its store's jsonb round trip (keys in another order)
            assert _without_made_at(turns[index + 1]["call"]["state"]) == _without_made_at(result["state"])
    (first, _), (clarify, made_2), (picked, made_3) = results
    # 1: a records question with no state: a new plan
    assert first["reason"] == "new" and first["kind"] in ("merged", "records") and first["state"]["plan"]
    # 2: the resolver's clarifying question, its choices pending, its text as sent (the next turn finds it)
    assert clarify["reason"] == "clarify" and clarify["kind"] == "none" and len(made_2["resolve"]) == 1
    pending = clarify["state"]["pending"]
    assert pending and [c["n"] for c in pending["choices"]] == [1, 2, 3]
    last_assistant = [t for t in turns[2]["call"]["recent_turns"] if t["role"] == "assistant"][-1]["content"]
    assert _norm(pending["asked"]) in _norm(last_assistant)
    # 3: "2" picks the second choice by code (no model call of the resolver) and runs it as a question of its own
    assert picked["reason"] == "new" and made_3["resolve"] == [] and picked["state"]["pending"] is None
    assert picked["answer"].startswith("Read as: `" + pending["choices"][1]["question"])
    main = next(c for c in picked["state"]["plan"]["calls"] if c["role"] == "main")
    assert main["args"]["measure"] == "count_distinct" and main["args"]["measure_field"] == "Candidate__c"


def test_off_and_shadow_answer_the_bridges_calls_alike_and_off_answers_them_as_v1(wire):
    doc = _load()
    turns = _turns_with_calls(doc)
    if WRITE or len(turns) < len(QUESTIONS):
        pytest.skip("the fixture is being written")
    bridge = doc["bridge"]
    off, shadow = wire(MemoryConfig(mode="off")), wire(MemoryConfig(mode="shadow"))
    for index, turn in enumerate(turns):
        raw_off, made_off = off.ask(turn)
        raw_shadow, made_shadow = shadow.ask(turn)
        got_off, error_off = bridge_reads(raw_off)
        got_shadow, error_shadow = bridge_reads(raw_shadow)
        assert error_off is None and error_shadow is None and got_off is not None and got_shadow is not None
        # off answers a v2 call as v1: no contract, so the bridge's record gives no row
        assert set(got_off) == V1_KEYS and "contract" not in got_off
        # shadow: the same answer byte for byte, plus the four keys of contract v2
        _check_shape(got_shadow, bridge)
        assert {k: got_shadow[k] for k in V1_KEYS} == got_off, f"turn {index + 1}: shadow answered otherwise"
        assert made_shadow["resolve"] == [], "shadow called the resolver's model"
        for role in ANSWER_ROLES:
            assert made_shadow[role] == made_off[role], f"turn {index + 1}: the {role} requests differ"
        memory = shadow.asks()[-1]["memory"]
        assert memory["mode"] == "shadow" and memory["state_in"] == ("none" if turn["call"]["state"] is None
                                                                     else "ok"), memory
        assert "memory" not in off.asks()[-1]
