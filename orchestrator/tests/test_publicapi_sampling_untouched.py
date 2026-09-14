"""The /v1 API never receives chat's sampling plan (answer-quality C6/C10).

- publicapi/streaming.py never names `answer_plan` among the keywords it
  forwards to llm.stream_chat_events, so no server-chosen top_p, top_k, min_p
  or presence_penalty can ride on a /v1 request;
- a /v1 generation at temperature 0.2, through the REAL stream_chat_events and
  a fake engine, reaches the engine with temperature 0.2 and none of them;
- a /v1 body with top_p is still refused (a 400 with param 'top_p').
"""
from __future__ import annotations

import ast
import asyncio
import inspect

import pytest

from app import llm
from app.publicapi import errors, models, registry, streaming
from tests.test_llm_public_stream_kwargs import world  # noqa: F401 — fixture


def _forwarded_keyword_names() -> set:
    tree = ast.parse(inspect.getsource(streaming))
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_accepted_keywords":
            for arg in node.args[1:]:
                if isinstance(arg, ast.Tuple):
                    names.update(elt.value for elt in arg.elts if isinstance(elt, ast.Constant))
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            if isinstance(node.value, ast.Name) and node.value.id in ("kwargs", "sidecar_kwargs"):
                names.add(node.slice.value)
    return names


def test_streaming_never_forwards_answer_plan():
    names = _forwarded_keyword_names()
    assert "wall_clock_s" in names  # the parse found the real tuple
    assert "answer_plan" not in names
    assert "answer_guard" not in names
    assert "answer_plan" not in inspect.getsource(streaming)


def test_a_v1_generation_keeps_the_callers_temperature_and_nothing_else(world):
    spec = streaming.GenerationSpec(
        response_id="resp_sampling",
        model="techsara-35b",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=64,
        temperature=0.2,
        created_at=1789200000,
        item_id="msg_fixed",
    )

    async def drain():
        return "".join([frame async for frame in streaming.responses_sse(spec)])

    asyncio.run(drain())
    assert world.engine.calls, "the real stream_chat_events never reached the engine"
    sent = world.engine.calls[0]
    assert sent["temperature"] == 0.2
    for key in ("top_p", "presence_penalty", "frequency_penalty", "seed"):
        assert key not in sent
    for key in ("top_k", "min_p", "repetition_penalty"):
        assert key not in (sent.get("extra_body") or {})


def test_top_p_is_still_refused_on_v1():
    with pytest.raises(errors.ApiError) as raised:
        models.parse_responses_request({"model": registry.TECHSARA_35B, "input": "hi", "top_p": 0.9})
    assert raised.value.param == "top_p"


def test_llm_signature_exposes_answer_plan_keyword_only():
    parameter = inspect.signature(llm.stream_chat_events).parameters["answer_plan"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is None
