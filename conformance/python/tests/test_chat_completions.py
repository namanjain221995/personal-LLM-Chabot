"""POST /v1/chat/completions through the SDK (CONTRACT-3 §8.2)."""
from __future__ import annotations

import openai
import pytest

from techsara_conformance import asserts

MESSAGES = [{"role": "user", "content": "Reply with the single word: pong"}]
COUNTING = [{"role": "user", "content": "Count from 1 to 200, separated by spaces."}]


def _assert_completion(completion, target, *, limit: int) -> None:
    assert completion.object == "chat.completion"
    assert completion.id.startswith("chatcmpl"), completion.id
    assert completion.model == target.models["chat"]
    assert len(completion.choices) == 1
    choice = completion.choices[0]
    assert choice.index == 0
    assert choice.message.role == "assistant"
    assert choice.finish_reason in ("stop", "length")
    usage = completion.usage
    assert usage is not None and usage.prompt_tokens > 0
    assert 0 < usage.completion_tokens <= limit, f"completion_tokens {usage.completion_tokens} over the requested {limit}"
    assert usage.total_tokens == usage.prompt_tokens + usage.completion_tokens


def test_a_synchronous_chat_completion_with_max_tokens(client, target):
    completion = client.chat.completions.create(
        model=target.models["chat"], messages=MESSAGES, max_tokens=target.small_output_tokens, temperature=0
    )
    _assert_completion(completion, target, limit=target.small_output_tokens)
    assert completion.choices[0].message.content.strip()


def test_max_tokens_is_a_hard_bound_and_reports_finish_reason_length(client, target):
    completion = client.chat.completions.create(model=target.models["chat"], messages=COUNTING, max_tokens=5, temperature=0)
    _assert_completion(completion, target, limit=5)
    assert completion.choices[0].finish_reason == "length"


def test_a_streamed_chat_completion_with_max_tokens_ends_with_one_finish_and_a_usage_chunk(client, target):
    stream = client.chat.completions.create(
        model=target.models["chat"],
        messages=MESSAGES,
        max_tokens=target.small_output_tokens,
        stream=True,
        stream_options={"include_usage": True},
    )
    with stream:
        chunks = list(stream)
    assert chunks, "no chunks"
    assert {c.object for c in chunks} == {"chat.completion.chunk"}
    assert len({c.id for c in chunks}) == 1, "one completion id across the stream"
    finishes = [ch.finish_reason for c in chunks for ch in c.choices if ch.finish_reason]
    assert len(finishes) == 1 and finishes[0] in ("stop", "length"), finishes
    text = "".join(ch.delta.content or "" for c in chunks for ch in c.choices)
    assert text.strip(), "no streamed content"
    usage_chunks = [c for c in chunks if c.usage is not None]
    assert len(usage_chunks) == 1 and usage_chunks[0] is chunks[-1], "include_usage adds ONE final usage chunk"
    assert usage_chunks[0].choices == [], "the usage chunk has an empty choices list (OpenAI shape)"
    assert 0 < usage_chunks[0].usage.completion_tokens <= target.small_output_tokens


def test_a_streamed_chat_completion_without_include_usage_sends_no_usage(client, target):
    stream = client.chat.completions.create(
        model=target.models["chat"], messages=MESSAGES, max_tokens=target.small_output_tokens, stream=True
    )
    with stream:
        chunks = list(stream)
    assert all(c.usage is None for c in chunks)


def test_an_unsupported_chat_field_is_refused_by_name(client, target):
    with pytest.raises(openai.BadRequestError) as caught:
        client.chat.completions.create(
            model=target.models["chat"], messages=MESSAGES, max_tokens=4, tools=[{"type": "function", "function": {"name": "f", "parameters": {}}}]
        )
    error = asserts.sdk_error(caught.value, code="invalid_request_error")
    assert error["param"] == "tools" or "tools" in error["message"], error


@pytest.mark.feature("max_completion_tokens")
def test_max_completion_tokens_bounds_a_synchronous_completion(client, target):
    completion = client.chat.completions.create(
        model=target.models["chat"], messages=COUNTING, max_completion_tokens=5, temperature=0
    )
    _assert_completion(completion, target, limit=5)
    assert completion.choices[0].finish_reason == "length"


@pytest.mark.feature("max_completion_tokens")
def test_max_completion_tokens_bounds_a_streamed_completion(client, target):
    stream = client.chat.completions.create(
        model=target.models["chat"],
        messages=COUNTING,
        max_completion_tokens=5,
        stream=True,
        stream_options={"include_usage": True},
    )
    with stream:
        chunks = list(stream)
    finishes = [ch.finish_reason for c in chunks for ch in c.choices if ch.finish_reason]
    assert finishes == ["length"], finishes
    assert chunks[-1].usage is not None and chunks[-1].usage.completion_tokens <= 5


@pytest.mark.feature("max_completion_tokens")
def test_sending_both_max_tokens_and_max_completion_tokens_is_refused_though_each_alone_is_accepted(client, target):
    # Each alone first: a server that refuses max_completion_tokens as an
    # unknown field would otherwise "pass" the both-at-once refusal below
    # for the wrong reason (observed on the 2026-09-13 e2e build).
    alone = client.chat.completions.create(model=target.models["chat"], messages=MESSAGES, max_completion_tokens=4)
    assert alone.choices, "max_completion_tokens alone is accepted"
    with pytest.raises(openai.BadRequestError) as caught:
        client.chat.completions.create(model=target.models["chat"], messages=MESSAGES, max_tokens=4, max_completion_tokens=4)
    asserts.sdk_error(caught.value, code="invalid_request_error")


@pytest.mark.feature("output_ceiling")
def test_a_chat_completion_reports_the_applied_max_output_tokens_extension(client, target):
    completion = client.chat.completions.create(model=target.models["chat"], messages=COUNTING, max_tokens=5, temperature=0)
    applied = completion.model_dump().get("max_output_tokens")
    assert applied == 5, f"top-level max_output_tokens extension (§9): {applied!r}"
