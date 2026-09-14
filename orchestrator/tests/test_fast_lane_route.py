"""POST /chat through the Fast small-talk lane (app/fast_lane.py), offline.

The lane turn must reach none of the pre-passes a greeting cannot use — the
freshness router, the knowledge pre-pass, retrieval, rerank, cross-chat
recall, in-conversation recall, compaction — must carry no sources, and must
still be stored and traced. A turn that only LOOKS like a greeting (typed
after an unanswered question, or with a question attached) must take the
full path exactly as before.
"""
from __future__ import annotations

import json
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app import (
    compaction,
    db,
    fast_lane,
    freshness,
    living_knowledge,
    llm,
    main,
    memory_semantic,
    metrics,
    recall,
    rerank,
    web_index,
    web_memory,
)
from app.config import settings
from app.engines import CODE_INSTRUCTION, DIAGRAM_INSTRUCTION
from tests.conftest import _materialize_test_user


def _parse_sse(body: str):
    events = []
    for block in body.split("\n\n"):
        lines = [line for line in block.split("\n") if line and not line.startswith(":")]
        if len(lines) >= 2 and lines[0].startswith("event: "):
            events.append((lines[0][7:], json.loads(lines[1][6:])))
    return events


@pytest.fixture
def wired(monkeypatch):
    """Every network boundary stubbed, every pre-pass counted."""
    calls: Counter = Counter()
    prompts: list = []
    stream_kwargs: list = []

    monkeypatch.setattr(settings, "living_knowledge_enabled", True)
    monkeypatch.setattr(settings, "web_memory_enabled", True)
    monkeypatch.setattr(settings, "freshness_router_enabled", True)
    monkeypatch.setattr(settings, "fact_extraction_enabled", False)
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", False)
    monkeypatch.setattr(settings, "clarify_before_answering", False)

    async def stream_chat_events(messages, **kwargs):
        prompts.append(list(messages))
        stream_kwargs.append(dict(kwargs))
        for piece in ("Hello", "! How can I help?"):
            yield ("token", piece)

    async def router_chat_completion(messages, **kwargs):
        calls["router_chat_completion"] += 1
        return "STATIC"

    async def embed_query(text, **kwargs):
        return [0.1] * 8

    async def embed_texts(texts, **kwargs):
        return [[0.1] * 8 for _ in texts]

    async def dense(query, top_k=6, site_prefix="", **kwargs):
        calls["web_index.retrieve"] += 1
        return []

    monkeypatch.setattr(llm, "stream_chat_events", stream_chat_events)
    monkeypatch.setattr(llm, "router_chat_completion", router_chat_completion)
    monkeypatch.setattr(llm, "embed_query", embed_query)
    monkeypatch.setattr(llm, "embed_texts", embed_texts)
    monkeypatch.setattr(web_index, "retrieve", dense)

    def spy(module, name, key):
        real = getattr(module, name)

        async def wrapped(*args, **kwargs):
            calls[key] += 1
            return await real(*args, **kwargs)

        monkeypatch.setattr(module, name, wrapped)

    spy(freshness, "_ask_router", "freshness._ask_router")
    spy(web_memory, "retrieve", "web_memory.retrieve")
    spy(living_knowledge, "retrieve", "living_knowledge.retrieve")
    spy(living_knowledge, "prepare", "living_knowledge.prepare")
    spy(memory_semantic, "cross_chat_block", "memory_semantic.cross_chat_block")
    spy(compaction, "prepare_deferred", "compaction.prepare_deferred")

    async def score(query, documents, **kwargs):
        calls["rerank.score"] += 1
        return [0.0 for _ in documents]

    monkeypatch.setattr(rerank, "score", score)

    real_block = recall.retrieve_block

    def retrieve_block(*args, **kwargs):
        calls["recall.retrieve_block"] += 1
        return real_block(*args, **kwargs)

    monkeypatch.setattr(recall, "retrieve_block", retrieve_block)
    return {"calls": calls, "prompts": prompts, "stream_kwargs": stream_kwargs}


_PRE_PASSES = (
    "router_chat_completion",
    "freshness._ask_router",
    "living_knowledge.prepare",
    "web_memory.retrieve",
    "living_knowledge.retrieve",
    "web_index.retrieve",
    "rerank.score",
    "memory_semantic.cross_chat_block",
    "recall.retrieve_block",
    "compaction.prepare_deferred",
)


def _send(client, conversation_id: str, messages: list, **extra):
    body = {
        "mode": "assistant",
        "effort": "fast",
        "model": "smart",
        "web_search": "auto",
        "conversation_id": conversation_id,
        "session_id": conversation_id,
        "messages": messages,
        **extra,
    }
    resp = client.post("/chat", json=body)
    assert resp.status_code == 200, resp.text
    events = _parse_sse(resp.text)
    assert events[-1][0] == "done", events[-3:]
    metas = [data for kind, data in events if kind == "meta" and "route" in data]
    return events, metas[-1]


_ANSWERED = [
    {"role": "user", "content": "How do I lock the header row in a spreadsheet?"},
    {"role": "assistant", "content": "Select the row below it and choose View > Freeze panes."},
]


def test_a_greeting_takes_the_lane_and_touches_no_pre_pass(wired):
    metrics.reset()
    with TestClient(main.app) as client:
        events, meta = _send(client, "lane-conv-1", [*_ANSWERED, {"role": "user", "content": "hi ??"}])
        trace = client.get(f"/chat/trace/{meta['trace_id']}")

    calls = wired["calls"]
    for name in _PRE_PASSES:
        assert calls[name] == 0, (name, dict(calls))
    assert "".join(d["text"] for k, d in events if k == "token") == "Hello! How can I help?"
    assert not meta.get("sources")
    assert meta["provenance"] == {"source": "model", "retrieved_source_count": 0, "cited_source_count": 0}
    # Compaction (and so the context meter's count) is skipped; the web UI's
    # latestUsage() keeps showing the previous reply's reading.
    assert "context" not in meta
    assert meta["knowledge"]["decision"] == "small_talk_lane"
    assert meta["route"] == "chat" and meta["effort"] == "fast"

    # The prompt: persona, no diagram/code rules, the answered exchange, the greeting.
    (prompt,) = wired["prompts"]
    system = prompt[0]["content"]
    assert DIAGRAM_INSTRUCTION not in system and CODE_INSTRUCTION not in system
    assert "mermaid" not in system and "```python" not in system
    assert [m["role"] for m in prompt] == ["system", "user", "assistant", "user"]
    assert prompt[-1] == {"role": "user", "content": "hi ??"}
    (kwargs,) = wired["stream_kwargs"]
    assert kwargs["max_tokens"] == 1024
    assert kwargs["effort"] == "fast" and llm.wants_thinking(kwargs["model_choice"], kwargs["effort"]) is False

    # Stored like every answer.
    assert db.list_messages("lane-conv-1")[-1]["content"] == "Hello! How can I help?"

    # Traced: the usual stages plus the lane's decision.
    assert trace.status_code == 200, trace.text
    rows = trace.json()["events"]
    stages = [row["stage"] for row in rows]
    assert stages[:3] == ["REQUEST_RECEIVED", "MODE_RESOLVED", "CONTEXT_ASSEMBLED"]
    assert "RESPONSE_GENERATED" in stages
    lane_row = next(row for row in rows if row["stage"] == "FAST_LANE")
    assert lane_row["details"]["entered"] is True
    assert lane_row["details"]["category"] == "greeting"
    assert lane_row["details"]["veto"] == "none"
    assert lane_row["details"] == {"entered": True, "category": "greeting", "veto": "none"}
    # PROVENANCE_RECORDED still fires from emit(meta): the model, no sources.
    provenance_row = next(row for row in rows if row["stage"] == "PROVENANCE_RECORDED")
    assert provenance_row["details"]["source"] == "model"
    assert provenance_row["details"]["retrieved_source_count"] == 0
    assert stages.index("FAST_LANE") < stages.index("RESPONSE_GENERATED")

    counters = metrics._counters["fast_lane_total"]
    assert counters[(("category", "greeting"), ("result", "entered"), ("veto", "none"))] == 1
    prepare_hist = metrics._hists["knowledge_prepare_seconds"]
    assert (("decision", "small_talk_lane"), ("effort", "fast"), ("outcome", "skipped")) in prepare_hist


def test_the_lane_keeps_saved_facts_but_no_other_context_block(wired, monkeypatch):
    from app.facts import FACTS_HEADER

    user = _materialize_test_user("local")
    db.add_user_fact(int(user["id"]), "Prefers to be called Sam")
    with TestClient(main.app) as client:
        _send(client, "lane-conv-facts", [{"role": "user", "content": "good morning"}])
    (prompt,) = wired["prompts"]
    assert FACTS_HEADER in prompt[0]["content"] and "Prefers to be called Sam" in prompt[0]["content"]
    assert sum(1 for m in prompt if m["role"] == "system") == 1


def test_a_greeting_after_an_unanswered_live_question_takes_the_full_path(wired):
    history = [{"role": "user", "content": "what is the gold price today?"}, {"role": "user", "content": "hi ??"}]
    with TestClient(main.app) as client:
        _events, meta = _send(client, "lane-conv-2", history)
    calls = wired["calls"]
    assert calls["living_knowledge.prepare"] == 1
    assert calls["memory_semantic.cross_chat_block"] == 1
    assert (meta.get("knowledge") or {}).get("decision") != "small_talk_lane"


def test_a_greeting_after_a_failed_send_takes_the_full_path_even_with_its_partial_text(wired):
    """The browser sends a died answer back as plain text; the previous send's
    durable status is what tells the lane."""
    user = _materialize_test_user("local")
    uid = int(user["id"])
    db.create_conversation(uid, "lane-conv-3", "chat")
    db.create_chat_request("intent-prev-1", uid, "lane-conv-3", "gen-prev-1", {"text": "gold"})
    db.set_chat_request_status("intent-prev-1", "failed", error="engine died")
    history = [
        {"role": "user", "content": "what is the gold price today?"},
        {"role": "assistant", "content": "The gold price today is"},
        {"role": "user", "content": "hello?"},
    ]
    with TestClient(main.app) as client:
        _send(client, "lane-conv-3", history, intent_id="intent-now-1")
    assert wired["calls"]["living_knowledge.prepare"] == 1


def test_a_greeting_after_a_completed_send_still_takes_the_lane(wired):
    user = _materialize_test_user("local")
    uid = int(user["id"])
    db.create_conversation(uid, "lane-conv-4", "chat")
    db.create_chat_request("intent-prev-2", uid, "lane-conv-4", "gen-prev-2", {"text": "q"})
    db.set_chat_request_status("intent-prev-2", "completed")
    with TestClient(main.app) as client:
        _events, meta = _send(
            client, "lane-conv-4", [*_ANSWERED, {"role": "user", "content": "thanks"}], intent_id="intent-now-2"
        )
    assert wired["calls"]["living_knowledge.prepare"] == 0
    assert meta["knowledge"]["decision"] == "small_talk_lane"


def test_thanks_after_an_offer_takes_the_full_path(wired):
    """A pleasantry answering an offer is its acceptance: the full path, with
    its code rules and uncapped answer, not the lane's short prompt."""
    history = [
        {"role": "user", "content": "I need a python script that parses web server logs"},
        {"role": "assistant", "content": "Plan: regex per line, count with Counter. Want me to write the full script?"},
        {"role": "user", "content": "thanks"},
    ]
    with TestClient(main.app) as client:
        _events, meta = _send(client, "lane-conv-offer", history)
    assert wired["calls"]["living_knowledge.prepare"] == 1
    assert (meta.get("knowledge") or {}).get("decision") != "small_talk_lane"
    (prompt,) = wired["prompts"]
    assert CODE_INSTRUCTION in prompt[0]["content"]


def test_thanks_with_a_question_attached_takes_the_full_path(wired):
    with TestClient(main.app) as client:
        _send(
            client,
            "lane-conv-5",
            [*_ANSWERED, {"role": "user", "content": "thanks, and who won the match yesterday?"}],
        )
    calls = wired["calls"]
    assert calls["living_knowledge.prepare"] == 1
    assert calls["living_knowledge.retrieve"] + calls["web_memory.retrieve"] >= 1


def test_a_greeting_with_an_undecided_question_reaches_the_router(wired):
    with TestClient(main.app) as client:
        _send(client, "lane-conv-6", [*_ANSWERED, {"role": "user", "content": "hello, is the market open"}])
    calls = wired["calls"]
    assert calls["living_knowledge.prepare"] == 1
    assert calls["freshness._ask_router"] == 1
    assert calls["living_knowledge.retrieve"] + calls["web_memory.retrieve"] >= 1


def test_the_kill_switch_restores_the_full_path(wired, monkeypatch):
    monkeypatch.setenv("FAST_LANE_ENABLED", "false")
    with TestClient(main.app) as client:
        _events, meta = _send(client, "lane-conv-7", [*_ANSWERED, {"role": "user", "content": "hi ??"}])
    calls = wired["calls"]
    assert calls["living_knowledge.prepare"] == 1
    assert calls["memory_semantic.cross_chat_block"] == 1
    assert (meta.get("knowledge") or {}).get("decision") != "small_talk_lane"
    (prompt,) = wired["prompts"]
    assert DIAGRAM_INSTRUCTION in prompt[0]["content"]


def test_think_effort_greetings_are_not_in_the_lane(wired):
    with TestClient(main.app) as client:
        _send(
            client,
            "lane-conv-8",
            [*_ANSWERED, {"role": "user", "content": "hello"}],
            effort="think",
        )
    assert wired["calls"]["living_knowledge.prepare"] == 1


def test_no_knowledge_task_is_left_for_the_later_guards_to_handle(wired, monkeypatch):
    """The lane leaves `knowledge_task` None; the escalation block, the chat
    branch and the post-answer cancel must all cope with that."""
    import asyncio

    started: list = []
    real = asyncio.ensure_future

    def watch(coro_or_future, *args, **kwargs):
        name = getattr(getattr(coro_or_future, "cr_code", None), "co_name", "")
        started.append(name)
        return real(coro_or_future, *args, **kwargs)

    monkeypatch.setattr(main.asyncio, "ensure_future", watch)
    with TestClient(main.app) as client:
        events, meta = _send(client, "lane-conv-9", [{"role": "user", "content": "bye"}])
    assert "_prepare_knowledge" not in started
    assert meta["knowledge"]["decision"] == "small_talk_lane"
    assert not [kind for kind, _ in events if kind == "error"]
    assert fast_lane.FAST_LANE_FACTS_WAIT_S == 0.15
