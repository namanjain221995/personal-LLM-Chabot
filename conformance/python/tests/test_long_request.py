"""timeout=None: a request that runs as long as its output needs.

WHY (owner decision 2026-09-13: no timeouts, max output 1,000,000 tokens).
The SDK's default is a 600 s timeout; a developer generating a long answer
passes `timeout=None`. What the SERVER owes that developer (CONTRACT-3 §8.3,
§10): the request is not cut short of its own wall clock, a stream never
goes silent for more than the 15 s heartbeat, and the terminal event carries
usage. Size the run with --long-output-tokens (default 1,024: about ten
seconds of decode, cheap enough for any stack). A release run on a quiet
stack should use something like 60,000, which outlasts the SDK's default
600 s timeout; the default is small only so the suite is safe to point at an
engine people are using.
"""
from __future__ import annotations

import time

import httpx
import openai
import pytest

from techsara_conformance import sse

import json


def counting_prompt(tokens: int) -> str:
    """A task whose honest answer is LONGER than the budget: every integer up
    to max(1000, budget), one per line, is at least two tokens a line. The
    first prompt, "count upward and never stop", ended on its own at 292
    tokens on 2026-09-13 (the model stopped at a round number), which tests
    the model's patience, not the server."""
    return f"Write every integer from 1 to {max(1000, tokens)} in ascending order, one per line, with no other text."


HEARTBEAT_S = 15.0
SLACK_S = 5.0


@pytest.mark.long
def test_a_long_stream_with_the_sdk_timeout_disabled_runs_to_its_budget_and_is_never_silent_on_the_wire(make_client, target, note):
    # WHY THE WIRE AND NOT SDK EVENTS (2026-09-13, review finding): the first
    # version started its clock before `create()` and timed SDK events. A
    # 22 s client-side pacing sleep then read as 22 s of silence on a server
    # that streamed everything at once, and a server heartbeating through a
    # long prefill or an admission-lane wait (§10: up to 600 s, inside the
    # body) would have FAILED, because the SDK drops `: ping` comments. The
    # WireClock times every byte inside the SDK's transport, from the moment
    # the response headers arrive; the wait before the headers (a capacity
    # gate may hold a request up to 30 s there, §10) is reported, not asserted.
    wire = sse.WireClock()
    client = make_client(timeout=None, wire=wire)
    tokens = target.long_output_tokens
    started = time.monotonic()
    events = []
    stream = client.responses.create(model=target.models["chat"], input=counting_prompt(tokens), max_output_tokens=tokens, stream=True, temperature=0)
    with stream:
        for event in stream:
            events.append(event)
    elapsed = time.monotonic() - started
    terminal = events[-1]
    assert terminal.type == "response.completed", f"terminal {terminal.type}: {getattr(terminal, 'response', None)}"
    usage = terminal.response.usage
    assert usage is not None, "the terminal event carries usage (§10)"
    assert usage.output_tokens <= tokens
    assert usage.output_tokens >= tokens // 2, (
        f"asked for a list longer than its {tokens}-token budget, the model produced {usage.output_tokens}: "
        "the stream ended far short of its budget"
    )
    assert wire.headers_at is not None and wire.arrivals, "the transport never saw the event-stream body"
    silence = wire.max_silence_s()
    assert silence <= HEARTBEAT_S + SLACK_S, (
        f"{silence:.1f} s with no byte on the wire after the response headers; CONTRACT-3 §10 sends a `: ping` "
        f"at least every {HEARTBEAT_S:g} s for the whole life of the stream (heartbeat comment lines seen: {wire.comment_lines})"
    )
    streamed_s = (wire.ended_at or time.monotonic()) - wire.headers_at
    note(
        f"{usage.output_tokens} output tokens in {elapsed:.1f} s, {streamed_s:.1f} s of it after the headers "
        f"({usage.output_tokens / max(streamed_s, 0.001):.0f} tok/s); "
        f"wire: {wire.pre_header_wait_s:.1f} s before headers (not asserted), max silence {silence:.1f} s, "
        f"{wire.comment_lines} heartbeat comment line(s), {wire.bytes} bytes"
    )


@pytest.mark.long
def test_a_long_synchronous_request_with_timeout_none_completes_with_its_usage(make_client, target, note):
    client = make_client(timeout=None)
    tokens = target.long_output_tokens
    started = time.monotonic()
    response = client.responses.create(model=target.models["chat"], input=counting_prompt(tokens), max_output_tokens=tokens, temperature=0)
    elapsed = time.monotonic() - started
    assert response.status == "completed", f"{response.status}: {response.error!r}"
    assert response.usage is not None and tokens // 2 <= response.usage.output_tokens <= tokens, response.usage
    note(
        f"sync {response.usage.output_tokens} tokens in {elapsed:.1f} s; through a proxy with a 100 s origin timeout "
        "a synchronous request this long must be stream or background (§8.3)"
    )


# --------------------------------------------------------------------------
# 2026-09-13, no-timeout design revision 2: the byte invariant (CONTRACT-3
# §10.1). A synchronous request no longer has to finish before a proxy's
# first-byte timeout: after PUBLIC_API_SYNC_COMMIT_S (15 s) the API commits a
# 200 and writes a space every 15 s until the JSON object is ready.

COMMIT_S = 15.0


@pytest.mark.long
@pytest.mark.feature("no_timeouts")
def test_a_synchronous_request_longer_than_the_commit_sends_its_first_byte_within_15_s_and_spaces_until_the_body(raw, target, note):
    tokens = max(3000, target.long_output_tokens)
    body = {"model": target.models["chat"], "input": counting_prompt(tokens), "max_output_tokens": tokens, "temperature": 0}
    started = time.monotonic()
    arrivals = []
    chunks = []
    with raw.stream("POST", "responses", json=body, timeout=httpx.Timeout(30.0, read=None)) as response:
        headers_at = time.monotonic()
        for chunk in response.iter_bytes():
            arrivals.append(time.monotonic())
            chunks.append(chunk)
    ended = time.monotonic()
    payload = b"".join(chunks)
    elapsed = ended - started
    if elapsed <= COMMIT_S:
        pytest.skip(f"answered in {elapsed:.1f} s, inside the commit interval; raise TECHSARA_LONG_OUTPUT_TOKENS")
    assert response.status_code == 200
    assert arrivals and arrivals[0] - started <= COMMIT_S + SLACK_S, (
        f"first body byte after {arrivals[0] - started:.1f} s; §10.1 owes one within {COMMIT_S:g} s"
    )
    gaps = [b - a for a, b in zip([headers_at, *arrivals], [*arrivals, ended])]
    assert max(gaps) <= HEARTBEAT_S + SLACK_S, f"{max(gaps):.1f} s without a byte"
    assert payload[:1] == b" ", "a committed body starts with the keepalive space"
    parsed = json.loads(payload)
    assert parsed["object"] == "response" and parsed["status"] in ("completed", "failed"), parsed.get("status")
    note(f"{elapsed:.0f} s synchronous call; first byte after {arrivals[0] - started:.1f} s; {len(payload) - len(payload.lstrip())} leading spaces")


@pytest.mark.long
@pytest.mark.feature("no_timeouts")
def test_openai_python_with_its_defaults_parses_a_committed_synchronous_body(make_client, target):
    tokens = max(3000, target.long_output_tokens)
    client = make_client(timeout=openai.Timeout(600.0, connect=5.0))
    response = client.responses.create(
        model=target.models["chat"], input=counting_prompt(tokens), max_output_tokens=tokens, temperature=0
    )
    assert response.status == "completed", f"{response.status}: {response.error!r}"
