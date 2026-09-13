"""`/v1` on techsara-35b against a FAKE OpenAI-compatible vLLM over real HTTP
— the contract findings of the adversarial review of 2026-09-13.

Everything but the engine is real: the router, the planner, Postgres,
`llm.stream_chat_events`, `context._fit` asking `/tokenize`, the admission
lanes. The fake behaves like vLLM in the three ways these tests depend on:
it refuses `prompt + max_tokens` over its window with vLLM's own 400 text,
it sends usage ONLY in the stream's last chunk, and `/tokenize` can be told to
fail (its 5 s timeout and transient failures land in the same fallback).
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app import context, db, usage as usage_ledger
from app.config import settings
from app.publicapi import capacity, events, models, openapi, planning, registry, streaming
from tests.test_publicapi_routes import TOKENS, _auth, _pepper, api, platform  # noqa: F401

CONTRACT = Path(__file__).resolve().parents[2] / "docs" / "developer-platform" / "CONTRACT.md"


class _Fake:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.window = 1_000_000
        self.tokenize_fails = False
        self.prompt_tokens = 37  # what /tokenize answers when it answers
        self.tokens = 3
        self.delay = 0.0
        self.chat: List[Dict[str, Any]] = []


FAKE = _Fake()


def _real_prompt_tokens(body: Dict[str, Any]) -> int:
    """Digits cost one token each (the Qwen pre-tokenizer isolates them);
    anything else at four characters per token; a small template overhead."""
    total = 3
    for message in body["messages"]:
        content = message["content"]
        text = content if isinstance(content, str) else "".join(p.get("text", "") for p in content)
        digits = sum(ch.isdigit() for ch in text)
        total += digits + (len(text) - digits + 3) // 4 + 5
    return total


async def _tokenize(request: Request):
    if FAKE.tokenize_fails:
        return JSONResponse({"error": "tokenizer busy"}, status_code=503)
    return JSONResponse({"count": FAKE.prompt_tokens, "max_model_len": FAKE.window, "tokens": []})


async def _chat(request: Request):
    body = await request.json()
    FAKE.chat.append({"max_tokens": body.get("max_tokens"), "stream_options": body.get("stream_options")})
    prompt = _real_prompt_tokens(body) if FAKE.tokenize_fails else FAKE.prompt_tokens
    max_tokens = int(body.get("max_tokens") or 16)
    if prompt + max_tokens > FAKE.window:
        message = (
            f"This model's maximum context length is {FAKE.window} tokens. However, you requested "
            f"{prompt + max_tokens} tokens ({prompt} in the messages, {max_tokens} in the completion). "
            "Please reduce the length of the messages or completion."
        )
        return JSONResponse(
            {"object": "error", "message": message, "type": "BadRequestError", "code": 400},
            status_code=400,
        )
    count = min(FAKE.tokens, max_tokens)
    finish = "length" if max_tokens <= FAKE.tokens else "stop"

    def frame(payload):
        return f"data: {json.dumps({'id': 'c', 'object': 'chat.completion.chunk', 'created': 0, 'model': body['model'], **payload})}\n\n"

    async def stream():
        for index in range(count):
            if FAKE.delay:
                await asyncio.sleep(FAKE.delay)
            yield frame({"choices": [{"index": 0, "delta": {"content": f"t{index} "}, "finish_reason": None}]})
        yield frame({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        # vLLM with include_usage: usage in the LAST chunk only.
        yield frame({"choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": count, "total_tokens": prompt + count}})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@pytest.fixture(scope="module")
def fake_vllm_url():
    app = Starlette(
        routes=[
            Route("/tokenize", _tokenize, methods=["POST"]),
            Route("/v1/chat/completions", _chat, methods=["POST"]),
        ]
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/v1"
    server.should_exit = True
    thread.join(5)


@pytest.fixture(autouse=True)
def _wired(fake_vllm_url, monkeypatch):
    monkeypatch.setattr(settings, "openai_base_url", fake_vllm_url)
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.setattr(settings, "gen_wall_clock_s", 4200.0)
    monkeypatch.setattr(settings, "context_safety_margin", 512)
    context._window_cache.clear()
    capacity.reset_for_tests()
    FAKE.reset()
    yield
    context._window_cache.clear()


def _await_terminal(api, response_id: str) -> Dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        got = api.get(f"/v1/responses/{response_id}", headers=_auth()).json()
        if got["status"] in ("completed", "failed", "cancelled"):
            return got
        time.sleep(0.1)
    raise AssertionError(f"{response_id} did not finish")


# ------------------------------------------ clamp, never refuse (§8.3) --


def test_when_tokenize_cannot_answer_an_underestimated_prompt_is_retried_on_its_byte_bound_not_refused(
    api, platform, monkeypatch
):
    """The review's probe: window 30,000; 20,000 digits (inside max_input_tokens
    on the byte bound, ~6,674 by the estimate); max_output_tokens 30,000.
    `_fit` fell back to the estimate, sent 22,814 and vLLM refused 20,008 +
    22,814 — the caller got `context_length_exceeded` blaming its input."""
    FAKE.window = 30_000
    FAKE.tokenize_fails = True
    monkeypatch.setattr(settings, "model_max_context", 30_000)
    digits = "7" * 20_000
    plan = planning.plan_generation(
        models.parse_responses_request({"model": "techsara-35b", "input": digits, "max_output_tokens": 30_000}),
        registry.resolve_public_model("techsara-35b"),
    )
    expected = 30_000 - plan.bounded_input_tokens - 512

    response = api.post(
        "/v1/responses",
        json={"model": "techsara-35b", "input": digits, "max_output_tokens": 30_000},
        headers=_auth(),
    )

    assert response.status_code == 200, response.text
    assert FAKE.chat[-1]["max_tokens"] == expected
    assert response.json()["max_output_tokens"] == expected
    assert response.json()["status"] == "completed"


def test_a_refusal_the_retry_cannot_fix_is_still_reported_once_and_not_looped(api, platform, monkeypatch):
    """The retry is ONE retry: an engine whose real window is smaller than the
    one configured refuses the retry too, and that refusal is the answer."""
    FAKE.tokenize_fails = True
    FAKE.window = 1_000  # the engine is not what settings say
    monkeypatch.setattr(settings, "model_max_context", 30_000)

    response = api.post(
        "/v1/responses",
        json={"model": "techsara-35b", "input": "7" * 5_000, "max_output_tokens": 29_000},
        headers=_auth(),
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "context_length_exceeded"
    # First attempt (sent twice by llm's include_usage fallback), then one retry
    # (sent twice again): never a third max_tokens value.
    assert len({call["max_tokens"] for call in FAKE.chat}) == 2


# ------------------------------------------- usage after a wall clock --


def test_a_wall_clock_stop_records_counted_usage_when_the_engine_sends_usage_only_at_the_end(
    api, platform, monkeypatch
):
    """CONTRACT §8.3 promised the partial output AND its usage; vLLM's usage
    chunk is the stream's last, and the wall clock closes the stream before
    it, so the row said `usage: null`. Scaled: the request's own wall clock is
    1 s (PUBLIC_API_GEN_WALL_CLOCK_S, the ceiling of every /v1 clock), six
    tokens 0.4 s apart — the guard fires on the third chunk.

    Scaled through the PUBLIC ceiling since the llm.py integration
    (2026-09-13): llm.stream_chat_events now enforces the per-request clock,
    so shrinking only GEN_WALL_CLOCK_S (a floor under the 900 s prefill
    allowance) no longer cuts a /v1 generation — which is the point."""
    monkeypatch.setattr(settings, "gen_wall_clock_s", 1.0)
    monkeypatch.setattr(settings, "public_api_gen_wall_clock_s", 1.0, raising=False)
    FAKE.tokens = 6
    FAKE.delay = 0.4
    rows: List[Dict[str, Any]] = []

    async def spy(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(usage_ledger, "record_async", spy)

    created = api.post(
        "/v1/responses", json={"model": "techsara-35b", "input": "Write long.", "background": True}, headers=_auth()
    )
    assert created.status_code == 202, created.text
    final = _await_terminal(api, created.json()["id"])

    assert final["status"] == "failed" and final["error"]["code"] == "timeout"
    text = final["output"][0]["content"][0]["text"]
    produced = len(text.split())
    assert produced >= 1
    assert final["usage"] == {
        "input_tokens": FAKE.prompt_tokens,  # the exact /tokenize count _fit took
        "output_tokens": produced,
        "total_tokens": FAKE.prompt_tokens + produced,
    }
    ledger = [row for row in rows if row.get("generation_id") == created.json()["id"]][-1]
    assert ledger["input_tokens"] == FAKE.prompt_tokens and ledger["output_tokens"] == produced
    assert ledger["meta"]["usage_source"] == streaming.USAGE_COUNTED_AT_STOP


def test_a_completed_answer_still_takes_its_usage_from_the_engine(api, platform, monkeypatch):
    rows: List[Dict[str, Any]] = []

    async def spy(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(usage_ledger, "record_async", spy)
    response = api.post("/v1/responses", json={"model": "techsara-35b", "input": "hi"}, headers=_auth())

    assert response.status_code == 200, response.text
    assert response.json()["usage"]["input_tokens"] == FAKE.prompt_tokens
    assert rows[-1]["meta"]["usage_source"] == "engine"


# ------------------------------------------ the 1M promise, told honestly --


def _max_output_description() -> str:
    return openapi.public_openapi()["components"]["schemas"]["ResponsesRequest"]["properties"][
        "max_output_tokens"
    ]["description"]


def test_the_openapi_document_says_the_million_is_not_live_exactly_while_llm_has_no_per_request_clock(
    monkeypatch,
):
    live = registry.per_request_wall_clock_live()
    assert ("NOT YET LIVE ON THIS DEPLOYMENT" in _max_output_description()) is (not live)

    monkeypatch.setattr(registry, "per_request_wall_clock_live", lambda: False)
    monkeypatch.setattr(settings, "gen_wall_clock_s", 4200.0)
    caveat = _max_output_description()
    assert "stopped after 4,200 seconds" in caveat
    assert "298,000-424,000 tokens" in caveat
    monkeypatch.setattr(registry, "per_request_wall_clock_live", lambda: True)
    assert "NOT YET LIVE" not in _max_output_description()


def test_the_liveness_answer_follows_llms_real_signature_and_its_source():
    import inspect

    from app import llm

    accepts = "wall_clock_s" in inspect.signature(llm.stream_chat_events).parameters
    assert registry.per_request_wall_clock_live() is accepts
    assert registry._source_accepts_wall_clock() is accepts


def _contract_section(start: str, end: str) -> str:
    text = CONTRACT.read_text("utf-8")
    return text[text.index(start): text.index(end, text.index(start))]


def test_contract_section_8_3_carries_the_not_live_caveat_exactly_while_it_is_true():
    section = _contract_section("### 8.3", "### 8.4")
    assert ("**Not yet live on this deployment:**" in section) is (
        not registry.per_request_wall_clock_live()
    )


def test_contract_section_9_says_the_applied_ceiling_may_exceed_the_planned_one_as_the_code_reports():
    """CONTRACT §9 said the terminal value is "≤ planned"; the review measured
    998,728 applied against 998,480 planned for 3,000 characters of prose."""
    section = _contract_section("## 9.", "## 10.")
    assert "(≤ planned)" not in section
    assert "may be **higher or lower**" in section
    applied = planning.applied_max_output_tokens(
        engine=registry.ENGINE_MAIN,
        requested=1_000_000,
        planned=998_480,
        context_window=1_000_000,
        reserve=512,
        usage={"prompt_tokens": 760},
    )
    assert applied == 998_728 > 998_480


def test_a_million_token_stream_is_not_cut_at_the_chat_apps_wall_clock(api, platform, monkeypatch):
    """Scaled: GEN_WALL_CLOCK_S = 1 s; the engine streams 8 tokens 0.4 s apart
    (3.2 s); a 1,000,000-token request's own clock is 20,900 s.

    Was a strict xfail until llm.stream_chat_events accepted `wall_clock_s`
    (integration 2026-09-13); tests/test_llm_per_request_wall_clock.py pins the
    llm.py side on an injected clock."""
    monkeypatch.setattr(settings, "gen_wall_clock_s", 1.0)
    FAKE.tokens = 8
    FAKE.delay = 0.4
    with api.stream(
        "POST",
        "/v1/responses",
        json={"model": "techsara-35b", "input": "Write long.", "max_output_tokens": 1_000_000, "stream": True},
        headers=_auth(),
    ) as response:
        text = "".join(response.iter_text())
    assert events.parse_frames(text)[-1]["event"] == "response.completed"
