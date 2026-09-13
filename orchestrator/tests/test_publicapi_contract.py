"""The public API's contract types: request rules, error envelope, SSE grammar,
model registry.

Offline and route-free. This wave ships no endpoint, so every assertion here is
about a VALUE the next wave will serve — which is the point: the rules of
CONTRACT §8, §9, §10 and §15 are pinned before anything can be built on a
misreading of them.

The tables in this file restate the contract rather than importing it. A test
that read `errors._CODES` to check `errors._CODES` would pass whatever the
table said; these numbers were typed out of CONTRACT §9 by hand so that
changing the table alone turns this suite red.
"""
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.config import settings
from app.publicapi import errors, events, models, registry


# ------------------------------------------------------- §8 the request --


def _body(**overrides):
    body = {"model": registry.TECHSARA_35B, "input": "Explain retrieval-augmented generation."}
    body.update(overrides)
    return body


def test_the_documented_request_body_validates_exactly_as_written():
    request = models.ResponsesRequest.model_validate(
        {
            "model": "techsara-35b",
            "input": "Explain retrieval-augmented generation.",
            "instructions": "Answer in British English.",
            "stream": True,
            "background": False,
            "max_output_tokens": 1000,
            "temperature": 0.2,
            "metadata": {"customer_request_id": "abc-123"},
        }
    )
    assert request.model == "techsara-35b"
    assert request.stream is True and request.background is False
    assert request.metadata == {"customer_request_id": "abc-123"}


def test_input_accepts_a_bare_string_or_a_list_of_typed_messages():
    string_form = models.ResponsesRequest.model_validate(_body())
    assert string_form.chat_messages() == [
        {"role": "user", "content": "Explain retrieval-augmented generation."}
    ]

    list_form = models.ResponsesRequest.model_validate(
        _body(
            input=[
                {"role": "system", "content": "Be terse."},
                {"role": "user", "content": "Why is the sky blue?"},
            ]
        )
    )
    assert [m.role for m in list_form.input] == ["system", "user"]


def test_instructions_lead_the_messages_the_engine_is_given():
    request = models.ResponsesRequest.model_validate(
        _body(
            instructions="Answer in British English.",
            input=[{"role": "user", "content": "Colour or color?"}],
        )
    )
    assert request.chat_messages() == [
        {"role": "system", "content": "Answer in British English."},
        {"role": "user", "content": "Colour or color?"},
    ]


def test_a_parameter_the_platform_cannot_honour_is_rejected_and_never_ignored():
    # Every one of these is a field an OpenAI-shaped client library will happily
    # send. CONTRACT §8: rejected, never silently ignored.
    for field, value in (
        ("top_p", 0.9),
        ("tools", []),
        ("tool_choice", "auto"),
        ("n", 2),
        ("seed", 7),
        ("logit_bias", {}),
        ("stream_options", {"include_usage": True}),
        ("store", True),
        ("previous_response_id", "resp_1"),
    ):
        with pytest.raises(errors.ApiError) as raised:
            models.parse_responses_request(_body(**{field: value}))
        assert raised.value.status == 400
        assert raised.value.code == "invalid_request_error"
        assert raised.value.param == field


def test_nothing_in_the_body_can_name_the_project_key_or_workspace():
    # CONTRACT §8: those come from the API key. `extra="forbid"` is what makes
    # that structural instead of a convention someone has to keep re-checking.
    for field in ("project_id", "workspace_id", "api_key", "key_id", "limits"):
        with pytest.raises(errors.ApiError) as raised:
            models.parse_responses_request(_body(**{field: "anything"}))
        assert raised.value.param == field


def test_an_empty_input_is_refused_in_both_of_its_shapes():
    for bad in ("", "   ", []):
        with pytest.raises(ValidationError):
            models.ResponsesRequest.model_validate(_body(input=bad))
    with pytest.raises(ValidationError):
        models.ResponsesRequest.model_validate(_body(input=[{"role": "user", "content": "  "}]))


def test_a_message_role_outside_the_three_named_ones_is_refused():
    for role in ("tool", "function", "developer", "admin"):
        with pytest.raises(ValidationError):
            models.ResponsesRequest.model_validate(
                _body(input=[{"role": role, "content": "hello"}])
            )


def test_message_content_must_be_a_string_because_v1_exposes_no_image_input():
    with pytest.raises(ValidationError):
        models.ResponsesRequest.model_validate(
            _body(
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_image", "image_url": "https://x/y.png"}],
                    }
                ]
            )
        )


def test_max_output_tokens_below_one_is_refused_and_above_the_ceiling_is_a_400():
    with pytest.raises(ValidationError):
        models.ResponsesRequest.model_validate(_body(max_output_tokens=0))

    request = models.ResponsesRequest.model_validate(_body(max_output_tokens=100_000))
    with pytest.raises(errors.ApiError) as raised:
        request.resolve_max_output_tokens(ceiling=8192, default=8192)
    assert raised.value.status == 400
    assert raised.value.param == "max_output_tokens"


def test_an_absent_max_output_tokens_takes_the_default_clamped_to_the_ceiling():
    request = models.ResponsesRequest.model_validate(_body())
    assert request.resolve_max_output_tokens(ceiling=8192, default=8192) == 8192
    # CONTRACT §12 clamps the DEFAULT; it never clamps a number the caller sent.
    assert request.resolve_max_output_tokens(ceiling=4096, default=8192) == 4096
    asked = models.ResponsesRequest.model_validate(_body(max_output_tokens=512))
    assert asked.resolve_max_output_tokens(ceiling=8192, default=8192) == 512


def test_temperature_outside_zero_to_two_is_refused():
    for bad in (-0.1, 2.1, 5):
        with pytest.raises(ValidationError):
            models.ResponsesRequest.model_validate(_body(temperature=bad))
    for good in (0.0, 1.0, 2.0):
        assert models.ResponsesRequest.model_validate(_body(temperature=good)).temperature == good


def test_metadata_is_bounded_in_keys_key_length_value_length_and_type():
    assert models.MAX_METADATA_KEYS == 16
    with pytest.raises(ValidationError):
        models.ResponsesRequest.model_validate(
            _body(metadata={f"k{i}": "v" for i in range(17)})
        )
    with pytest.raises(ValidationError):
        models.ResponsesRequest.model_validate(_body(metadata={"k" * 65: "v"}))
    with pytest.raises(ValidationError):
        models.ResponsesRequest.model_validate(_body(metadata={"k": "v" * 513}))
    for not_a_string in (1, 1.5, True, None, ["a"], {"a": "b"}):
        with pytest.raises(ValidationError):
            models.ResponsesRequest.model_validate(_body(metadata={"k": not_a_string}))
    ok = models.ResponsesRequest.model_validate(
        _body(metadata={"k" * 64: "v" * 512, **{f"n{i}": "x" for i in range(15)}})
    )
    assert len(ok.metadata) == 16


def test_stream_and_background_may_not_both_be_true():
    with pytest.raises(errors.ApiError) as raised:
        models.parse_responses_request(_body(stream=True, background=True))
    assert raised.value.status == 400
    assert "stream" in raised.value.message and "background" in raised.value.message
    # Either one alone is ordinary.
    assert models.ResponsesRequest.model_validate(_body(stream=True)).stream is True
    assert models.ResponsesRequest.model_validate(_body(background=True)).background is True


def test_a_model_id_shaped_like_a_path_or_a_url_is_refused():
    for bad in ("../../etc/passwd", "http://vllm:30000/v1", "techsara 35b", "a" * 65, ""):
        with pytest.raises(ValidationError):
            models.ResponsesRequest.model_validate(_body(model=bad))


def test_the_reported_param_names_the_field_the_caller_actually_got_wrong():
    # `input` is a union, so one malformed message makes pydantic complain about
    # both branches. The caller needs the path into the body they sent, not the
    # branch they were not attempting.
    with pytest.raises(errors.ApiError) as raised:
        models.parse_responses_request(_body(input=[{"role": "user"}]))
    assert raised.value.param == "input.0.content"
    with pytest.raises(errors.ApiError) as raised:
        models.parse_responses_request({"input": "hello"})
    assert raised.value.param == "model"
    with pytest.raises(errors.ApiError) as raised:
        models.parse_responses_request(_body(metadata={"customer": 7}))
    assert raised.value.param == "metadata.customer"


def test_a_body_that_is_not_a_json_object_is_a_400_not_a_crash():
    for bad in ([], "hello", 3, None):
        with pytest.raises(errors.ApiError) as raised:
            models.parse_responses_request(bad)
        assert raised.value.status == 400


def test_the_rejection_message_never_echoes_the_value_the_caller_sent():
    # The body can contain the prompt, which CONTRACT §16 says we neither store
    # nor echo; pydantic's own rendering includes `input_value`.
    secret = "the-caller-s-private-prompt-text"
    with pytest.raises(errors.ApiError) as raised:
        models.parse_responses_request(_body(temperature=9.5, instructions=secret))
    assert secret not in raised.value.message
    assert "9.5" not in raised.value.message


def test_the_body_size_cap_is_a_mebibyte_and_is_read_from_settings(monkeypatch):
    assert models.max_body_bytes() == 1024 * 1024
    monkeypatch.setattr(settings, "public_api_max_body_bytes", 4096, raising=False)
    assert models.max_body_bytes() == 4096


# ------------------------------------------------ §9 the success shapes --


def test_the_documented_success_body_has_exactly_the_keys_of_the_contract():
    response = models.Response(
        id="resp_3f8a",
        created_at=1789200000,
        status="completed",
        model=registry.TECHSARA_35B,
        output=[models.OutputMessage.of("…")],
        usage=models.Usage(input_tokens=37, output_tokens=112, total_tokens=149),
    )
    wire = response.to_wire()
    assert list(wire) == ["id", "object", "created_at", "status", "model", "output", "usage"]
    assert wire["object"] == "response"
    assert wire["output"] == [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "…"}]}
    ]
    assert wire["usage"] == {"input_tokens": 37, "output_tokens": 112, "total_tokens": 149}


def test_usage_is_null_and_never_zero_when_the_engine_did_not_report_counts():
    # llm.get_usage() returns None for "not measured". A zero would be a lie and
    # an under-charge (CONTRACT §9).
    assert models.Usage.from_llm(None) is None
    assert models.Usage.from_llm({}) is None
    measured = models.Usage.from_llm({"prompt_tokens": 37, "completion_tokens": 112, "calls": 1})
    assert measured.total_tokens == 149

    unmeasured = models.Response(
        id="resp_1", created_at=1, status="completed", model=registry.TECHSARA_35B
    )
    assert unmeasured.to_wire()["usage"] is None


def test_a_response_status_outside_the_v34_check_constraint_is_refused():
    # SCHEMA-V34 api_responses.status: queued, in_progress, completed, failed,
    # cancelled. A status that cannot be stored must never be returned.
    for status in ("queued", "in_progress", "completed", "failed", "cancelled"):
        models.Response(id="resp_1", created_at=1, status=status, model=registry.TECHSARA_35B)
    for bad in ("running", "incomplete", "succeeded", "error"):
        with pytest.raises(ValidationError):
            models.Response(id="resp_1", created_at=1, status=bad, model=registry.TECHSARA_35B)


def test_the_error_field_appears_only_on_a_failed_response():
    failed = models.Response(
        id="resp_1",
        created_at=1,
        status="failed",
        model=registry.TECHSARA_35B,
        error=models.ResponseError(code="model_unavailable", message="The model is not available."),
    )
    assert failed.to_wire()["error"]["code"] == "model_unavailable"


# ------------------------------------------------- §9 the error envelope --

#: CONTRACT §9, typed out by hand: code → HTTP status.
CONTRACT_ERROR_STATUSES = {
    "invalid_request_error": 400,
    "invalid_api_key": 401,
    "insufficient_scope": 403,
    "origin_not_allowed": 403,
    "model_not_found": 404,
    "response_not_found": 404,
    "idempotency_conflict": 409,
    "request_too_large": 413,
    "context_length_exceeded": 400,
    "rate_limit_error": 429,
    "quota_exceeded": 429,
    "concurrency_limit_exceeded": 429,
    "model_recovering": 503,
    "model_unavailable": 503,
    "timeout": 504,
    "internal_error": 500,
}


def test_every_code_in_the_contract_table_maps_to_the_status_it_documents():
    assert set(errors.error_codes()) == set(CONTRACT_ERROR_STATUSES)
    for code, status in CONTRACT_ERROR_STATUSES.items():
        assert errors.status_for(code) == status


def test_a_code_outside_the_table_cannot_be_constructed_or_looked_up():
    with pytest.raises(ValueError):
        errors.ApiError("teapot_error", "no", retry_after=1)
    with pytest.raises(ValueError):
        errors.status_for("teapot_error")


def test_the_envelope_is_the_same_five_keys_for_every_failure():
    for factory in (
        lambda: errors.invalid_request("bad", param="temperature"),
        errors.invalid_api_key,
        errors.insufficient_scope,
        errors.origin_not_allowed,
        errors.model_not_found,
        errors.response_not_found,
        errors.idempotency_conflict,
        lambda: errors.request_too_large(1048576),
        errors.context_length_exceeded,
        lambda: errors.rate_limit(30),
        lambda: errors.quota_exceeded(60),
        lambda: errors.concurrency_limit_exceeded(2),
        lambda: errors.model_recovering(15),
        errors.model_unavailable,
        errors.timeout,
        errors.internal_error,
    ):
        envelope = factory().envelope("req_abc")
        assert list(envelope) == ["error"]
        assert list(envelope["error"]) == ["message", "type", "code", "param", "request_id"]
        assert envelope["error"]["request_id"] == "req_abc"
        assert isinstance(envelope["error"]["message"], str) and envelope["error"]["message"]


def test_the_documented_authentication_envelope_is_reproduced_exactly():
    assert errors.invalid_api_key().envelope("req_1") == {
        "error": {
            "message": "The API key is invalid.",
            "type": "authentication_error",
            "code": "invalid_api_key",
            "param": None,
            "request_id": "req_1",
        }
    }


def test_a_missing_request_id_is_null_rather_than_an_empty_string():
    assert errors.internal_error().envelope("")["error"]["request_id"] is None


def test_a_429_or_503_cannot_be_raised_without_a_retry_after():
    for code in ("rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded",
                 "model_recovering", "model_unavailable"):
        with pytest.raises(ValueError):
            errors.ApiError(code, "throttled")
    assert errors.rate_limit(30).headers() == {"Retry-After": "30"}
    assert errors.model_recovering(15).headers() == {"Retry-After": "15"}
    assert errors.invalid_api_key().headers() == {}


def test_retry_after_is_a_whole_number_of_seconds_no_lower_than_one():
    assert errors.rate_limit(1.2).retry_after == 2
    assert errors.rate_limit(0).retry_after == 1
    assert errors.rate_limit(-5).retry_after == 1


def test_the_three_throttles_share_one_type_so_a_client_writes_one_retry_rule():
    assert errors.rate_limit(1).type == "rate_limit_error"
    assert errors.quota_exceeded(1).type == "rate_limit_error"
    assert errors.concurrency_limit_exceeded(1).type == "rate_limit_error"
    for code in ("rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded",
                 "model_recovering", "model_unavailable", "timeout"):
        assert code in errors.RETRYABLE_CODES
    assert "invalid_api_key" not in errors.RETRYABLE_CODES


def test_a_model_a_key_may_not_use_is_a_404_that_does_not_disclose_existence():
    # CONTRACT §4: never 403 — the two cases must be indistinguishable.
    assert errors.model_not_found("techsara-35b").status == 404
    assert errors.model_not_found("nonesuch").status == 404
    assert (
        errors.model_not_found("techsara-35b").message
        == "The model `techsara-35b` does not exist or you do not have access to it."
    )


def test_a_caller_supplied_identifier_is_only_echoed_when_it_is_plainly_safe():
    injected = "techsara-35b\nevent: error\ndata: {}"
    assert "\n" not in errors.model_not_found(injected).message
    assert "/etc/passwd" not in errors.response_not_found("../../etc/passwd").message


def test_a_raised_valueerror_with_an_internal_hostname_never_reaches_the_envelope():
    # The incident shape this rule exists for: an exception nobody wrote for a
    # caller carries the container name, the private address and the port.
    leaky = ValueError(
        "connection to server at \"vllm-router\" (172.18.0.4), port 30002 failed; "
        "check /data/lancedb and OPENAI_API_KEY=sk-local-abc123"
    )
    envelope = errors.envelope_from(leaky, request_id="req_9")["error"]
    body = json.dumps(envelope)
    for leak in ("vllm-router", "172.18.0.4", "30002", "/data/lancedb", "sk-local-abc123",
                 "OPENAI_API_KEY"):
        assert leak not in body
    assert envelope["code"] == "internal_error"
    assert envelope["type"] == "server_error"
    assert envelope["request_id"] == "req_9"


def test_an_unexpected_exception_becomes_internal_error_and_keeps_an_apierror_intact():
    assert errors.from_unexpected(KeyError("project_id")).code == "internal_error"
    assert errors.from_unexpected(RuntimeError("boom")).status == 500
    original = errors.rate_limit(30)
    assert errors.from_unexpected(original) is original


def test_the_scrubber_removes_what_the_contract_forbids_and_keeps_ordinary_prose():
    redacted = errors.redact(
        "failed at /app/main.py talking to http://vllm-embed:30003/v1 on 10.0.0.7 "
        "while running SELECT id FROM api_keys WHERE public_id = 'x'"
    )
    for leak in ("/app/main.py", "vllm-embed", "10.0.0.7", "api_keys", "SELECT"):
        assert leak not in redacted

    kept = "max_output_tokens must be between 1 and 8192 for this model."
    assert errors.redact(kept) == kept
    public = "See https://docs.techsarasolutions.com/api for the field list."
    assert errors.redact(public) == public


def test_a_traceback_pasted_into_a_message_is_removed_entirely():
    redacted = errors.redact(
        'Traceback (most recent call last):\n  File "/app/app/llm.py", line 756\n'
        "RuntimeError: engine died"
    )
    assert "llm.py" not in redacted and "RuntimeError" not in redacted


# ------------------------------------------------------- §10 the stream --


def _stream_of(emitter, *frames):
    return "".join(frames)


def test_a_stream_numbers_itself_from_one_and_increases_by_exactly_one():
    emitter = events.SequencedEvents()
    response = models.Response(
        id="resp_1", created_at=1, status="in_progress", model=registry.TECHSARA_35B
    ).to_wire()
    frames = [emitter.created(response), emitter.in_progress(response)]
    frames += [emitter.output_text_delta(chunk) for chunk in ("Re", "trie", "val")]
    frames.append(emitter.output_text_done("Retrieval"))
    done = dict(response, status="completed")
    frames.append(emitter.completed(done))

    records = events.parse_frames("".join(frames))
    assert [r["event"] for r in records] == [
        "response.created",
        "response.in_progress",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.completed",
    ]
    assert [r["data"]["sequence_number"] for r in records] == [1, 2, 3, 4, 5, 6, 7]
    assert emitter.sequence_number == 7


def test_every_event_repeats_its_name_in_the_json_type_field():
    emitter = events.SequencedEvents()
    response = models.Response(
        id="resp_1", created_at=1, status="in_progress", model=registry.TECHSARA_35B
    ).to_wire()
    frames = "".join(
        [
            emitter.created(response),
            emitter.queued(response),
            emitter.in_progress(response),
            emitter.output_text_delta("hi"),
            emitter.output_text_done("hi"),
            emitter.completed(dict(response, status="completed")),
        ]
    )
    for record in events.parse_frames(frames):
        assert record["data"]["type"] == record["event"]


def test_a_heartbeat_is_a_comment_that_consumes_no_sequence_number():
    emitter = events.SequencedEvents()
    response = models.Response(
        id="resp_1", created_at=1, status="in_progress", model=registry.TECHSARA_35B
    ).to_wire()
    stream = emitter.created(response) + emitter.heartbeat() + emitter.output_text_delta("a")
    assert ": ping\n\n" in stream
    records = events.parse_frames(stream)
    assert [r["data"]["sequence_number"] for r in records] == [1, 2]
    # CONTRACT §10: at least every 15 s, whatever the chat app's interval is.
    assert events.HEARTBEAT_SECONDS <= 15.0


def test_exactly_one_terminal_event_is_ever_emitted():
    response = models.Response(
        id="resp_1", created_at=1, status="completed", model=registry.TECHSARA_35B
    ).to_wire()

    emitter = events.SequencedEvents()
    emitter.created(dict(response, status="in_progress"))
    emitter.completed(response)
    assert emitter.finished and emitter.terminal_event == "response.completed"
    # A second terminal is a programming error: two endings reach an SDK as a
    # response that succeeded and then failed.
    with pytest.raises(events.StreamProtocolError):
        emitter.error(errors.internal_error())
    with pytest.raises(events.StreamProtocolError):
        emitter.completed(response)


def test_no_event_of_any_kind_may_follow_the_terminal():
    emitter = events.SequencedEvents()
    emitter.created(
        models.Response(
            id="resp_1", created_at=1, status="in_progress", model=registry.TECHSARA_35B
        ).to_wire()
    )
    emitter.error(errors.model_unavailable())
    for attempt in (
        lambda: emitter.output_text_delta("late"),
        lambda: emitter.in_progress({"id": "resp_1", "status": "in_progress"}),
        lambda: emitter.output_text_done("late"),
    ):
        with pytest.raises(events.StreamProtocolError):
            attempt()


def test_usage_appears_only_on_the_terminal_event():
    emitter = events.SequencedEvents()
    in_progress = models.Response(
        id="resp_1", created_at=1, status="in_progress", model=registry.TECHSARA_35B
    ).to_wire()
    completed = models.Response(
        id="resp_1",
        created_at=1,
        status="completed",
        model=registry.TECHSARA_35B,
        usage=models.Usage(input_tokens=37, output_tokens=112, total_tokens=149),
    ).to_wire()

    frames = emitter.created(in_progress) + emitter.output_text_delta("x")
    frames += emitter.completed(completed)
    records = events.parse_frames(frames)
    assert records[0]["data"]["response"]["usage"] is None
    assert "usage" not in records[1]["data"]
    assert records[-1]["data"]["response"]["usage"]["total_tokens"] == 149

    # Attaching usage to a non-terminal is refused rather than written.
    other = events.SequencedEvents()
    with pytest.raises(events.StreamProtocolError):
        other.created(dict(in_progress, usage={"input_tokens": 1, "output_tokens": 1,
                                               "total_tokens": 2}))


def test_a_terminal_event_must_agree_with_the_status_it_carries():
    emitter = events.SequencedEvents()
    response = models.Response(
        id="resp_1", created_at=1, status="in_progress", model=registry.TECHSARA_35B
    ).to_wire()
    emitter.created(response)
    with pytest.raises(events.StreamProtocolError):
        emitter.completed(response)
    with pytest.raises(events.StreamProtocolError):
        emitter.failed(response)
    assert emitter.finished is False


def test_a_stream_opens_with_response_created_or_with_a_failure():
    with pytest.raises(events.StreamProtocolError):
        events.SequencedEvents().output_text_delta("no created yet")
    with pytest.raises(events.StreamProtocolError):
        events.SequencedEvents().in_progress({"id": "resp_1", "status": "in_progress"})
    # A request that dies between the 200 and the first lifecycle event has only
    # an in-band error left to send.
    first = events.parse_frames(events.SequencedEvents().error(errors.model_unavailable()))
    assert first[0]["event"] == "error" and first[0]["data"]["sequence_number"] == 1


def test_the_error_event_carries_the_five_fields_the_standard_requires():
    emitter = events.SequencedEvents()
    emitter.created(
        models.Response(
            id="resp_1", created_at=1, status="in_progress", model=registry.TECHSARA_35B
        ).to_wire()
    )
    record = events.parse_frames(emitter.error(errors.timeout(1800)))[0]
    assert record["event"] == "error"
    assert set(record["data"]) == {"type", "code", "message", "param", "sequence_number"}
    assert record["data"]["code"] == "timeout"
    assert record["data"]["sequence_number"] == 2


def test_a_delta_event_carries_exactly_the_field_names_the_sdks_read():
    emitter = events.SequencedEvents(item_id="msg_abc")
    emitter.created(
        models.Response(
            id="resp_1", created_at=1, status="in_progress", model=registry.TECHSARA_35B
        ).to_wire()
    )
    record = events.parse_frames(emitter.output_text_delta("Hi"))[0]
    assert record["data"] == {
        "type": "response.output_text.delta",
        "sequence_number": 2,
        "item_id": "msg_abc",
        "output_index": 0,
        "content_index": 0,
        "delta": "Hi",
    }


def test_a_payload_cannot_renumber_the_stream_or_mislabel_its_own_event():
    emitter = events.SequencedEvents()
    frame = emitter.created(
        {"id": "resp_1", "status": "queued", "type": "response.completed",
         "sequence_number": 99}
    )
    record = events.parse_frames(frame)[0]
    assert record["data"]["type"] == "response.created"
    assert record["data"]["sequence_number"] == 1


def test_an_unknown_event_name_cannot_be_framed():
    emitter = events.SequencedEvents()
    with pytest.raises(events.StreamProtocolError):
        emitter._frame("response.output_item.added", {})
    assert events.TERMINAL_EVENTS == ("response.completed", "response.failed", "error")


def test_every_frame_ends_with_a_blank_line_and_holds_no_raw_newline():
    # Closing a stream without the trailing blank line silently discards the
    # last event — which is the one carrying usage.
    emitter = events.SequencedEvents()
    frame = emitter.created({"id": "resp_1", "status": "queued", "model": "a\nb"})
    assert frame.endswith("\n\n")
    data_line = [line for line in frame.split("\n") if line.startswith("data: ")]
    assert len(data_line) == 1
    assert json.loads(data_line[0][len("data: "):])["response"]["model"] == "a\nb"


# --------------------------------- §10 the Chat Completions compatibility --


def test_a_chat_completions_stream_ends_with_the_literal_done_line():
    chunks = events.ChatCompletionChunks(
        completion_id="chatcmpl_1", model=registry.TECHSARA_35B, created=1789200000
    )
    stream = chunks.delta("Hello") + chunks.delta(" there") + chunks.stop() + chunks.done()
    assert stream.endswith("data: [DONE]\n\n")
    assert events.DONE_SENTINEL == "data: [DONE]\n\n"
    # No `event:` lines on this surface — the chunks are anonymous.
    assert "event: " not in stream


def test_the_usage_chunk_has_empty_choices_and_every_other_chunk_has_null_usage():
    chunks = events.ChatCompletionChunks(
        completion_id="chatcmpl_1", model=registry.TECHSARA_35B, include_usage=True
    )
    stream = (
        chunks.delta("Hi")
        + chunks.stop()
        + chunks.usage_chunk({"prompt_tokens": 37, "completion_tokens": 112, "total_tokens": 149})
        + chunks.done()
    )
    bodies = [
        json.loads(line[len("data: "):])
        for line in stream.split("\n\n")
        if line.startswith("data: ") and not line.endswith("[DONE]")
    ]
    assert [b["usage"] for b in bodies[:-1]] == [None, None]
    assert bodies[-1]["choices"] == []
    assert bodies[-1]["usage"]["total_tokens"] == 149
    assert all(b["object"] == "chat.completion.chunk" for b in bodies)
    assert bodies[0]["choices"][0]["delta"] == {"role": "assistant", "content": "Hi"}
    assert bodies[1]["choices"][0]["finish_reason"] == "stop"


def test_a_usage_chunk_is_refused_when_the_caller_did_not_ask_for_usage():
    chunks = events.ChatCompletionChunks(completion_id="c", model="m", include_usage=False)
    chunks.delta("Hi")
    chunks.stop()
    with pytest.raises(events.StreamProtocolError):
        chunks.usage_chunk({"total_tokens": 1})


def test_nothing_may_be_framed_after_done_and_done_happens_once():
    chunks = events.ChatCompletionChunks(completion_id="c", model="m", include_usage=True)
    chunks.delta("Hi")
    chunks.stop()
    chunks.done()
    for attempt in (
        lambda: chunks.delta("late"),
        lambda: chunks.usage_chunk({"total_tokens": 1}),
        chunks.done,
    ):
        with pytest.raises(events.StreamProtocolError):
            attempt()


def test_a_delta_after_the_finish_reason_chunk_is_refused():
    chunks = events.ChatCompletionChunks(completion_id="c", model="m")
    chunks.delta("Hi")
    chunks.stop()
    with pytest.raises(events.StreamProtocolError):
        chunks.delta("more")
    with pytest.raises(events.StreamProtocolError):
        chunks.stop()


# ----------------------------------------------------- §15 the registry --


def test_the_registry_declares_exactly_one_public_model_today():
    declared = registry.declared_models()
    assert [m.id for m in declared] == ["techsara-35b"]
    assert registry.PUBLIC_MODEL_IDS == ("techsara-35b",)
    model = declared[0]
    assert model.chat is True and model.streaming is True
    # Verified 2026-09-09: the main model IS a vision-language model.
    assert model.vision is True
    # CONTRACT §7 exposes neither tools nor embeddings on /v1.
    assert model.tools is False and model.embeddings is False
    assert model.status == "available"


def test_the_internal_target_is_read_at_call_time_and_never_captured_at_import(monkeypatch):
    # The main model has been swapped under a running deployment; a constant
    # captured at import would keep naming the old checkpoint.
    assert registry.declared_models()[0].internal == settings.llm_model
    monkeypatch.setattr(settings, "llm_model", "nvidia/Qwen4-99B-NVFP4")
    assert registry.declared_models()[0].internal == "nvidia/Qwen4-99B-NVFP4"
    assert registry.resolve_public_model("techsara-35b").internal == "nvidia/Qwen4-99B-NVFP4"


def test_the_public_rendering_never_names_the_checkpoint_behind_the_model():
    wire = registry.declared_models()[0].to_wire()
    assert "internal" not in wire
    assert settings.llm_model not in json.dumps(wire)
    assert wire["id"] == "techsara-35b" and wire["object"] == "model"
    assert set(wire["capabilities"]) == {"chat", "streaming", "vision", "tools", "embeddings"}


def test_the_limits_come_from_settings_rather_than_a_number_copied_from_the_docs(monkeypatch):
    model = registry.declared_models()[0]
    assert model.max_input_tokens == settings.model_max_context
    assert model.max_output_tokens == settings.model_max_output
    monkeypatch.setattr(settings, "model_max_context", 1_000_000)
    monkeypatch.setattr(settings, "model_max_output", 4096)
    refreshed = registry.declared_models()[0]
    assert refreshed.max_input_tokens == 1_000_000
    assert refreshed.max_output_tokens == 4096
    assert registry.default_max_output_tokens() == 4096


def test_an_internal_engine_can_never_be_registered_as_a_public_model():
    for target in (
        settings.router_base_url,
        settings.router_model,
        settings.embed_base_url,
        settings.embed_model,
        settings.ocr_base_url,
        settings.ocr_model,
        settings.agent_base_url,
    ):
        with pytest.raises(registry.InternalTargetError):
            registry.guard_internal_target(target)
        with pytest.raises(registry.InternalTargetError):
            registry.PublicModel(
                id="sneaky",
                internal=target,
                chat=True,
                streaming=True,
                vision=False,
                tools=False,
                embeddings=False,
                max_input_tokens=1024,
                max_output_tokens=1024,
            )
    with pytest.raises(registry.InternalTargetError):
        registry.guard_internal_target("")


def test_the_reranker_is_guarded_when_the_deployment_configures_one(monkeypatch):
    # RERANK_BASE_URL is blank by default (the reranker may run in process), so
    # the guard is exercised with one configured, as production has it.
    monkeypatch.setattr(settings, "rerank_base_url", "http://vllm-reranker:30005/v1")
    with pytest.raises(registry.InternalTargetError):
        registry.guard_internal_target("http://vllm-reranker:30005/v1")
    with pytest.raises(registry.InternalTargetError):
        registry.guard_internal_target(settings.rerank_model)


def test_a_database_override_may_disable_a_model_and_never_add_one():
    assert [m.id for m in registry.public_models({"techsara-35b": False})] == []
    assert registry.resolve_public_model("techsara-35b", overrides={"techsara-35b": False}) is None

    # A row for an id the code does not declare is ignored: the database cannot
    # expose a model (CONTRACT §15, SCHEMA-V34 public_models).
    widened = registry.public_models({"vllm-router": True, "internal-ocr": True})
    assert [m.id for m in widened] == ["techsara-35b"]
    assert registry.resolve_public_model("internal-ocr", overrides={"internal-ocr": True}) is None
    assert registry.resolve_public_model("vllm-router") is None


def test_override_rows_are_read_as_the_database_returns_them():
    rows = [{"id": "techsara-35b", "enabled": False}, {"id": "ghost", "enabled": True}]
    assert registry.public_models(rows) == ()
    assert [m.id for m in registry.public_models([{"id": "techsara-35b", "enabled": True}])] == [
        "techsara-35b"
    ]


def test_a_key_allowlist_narrows_the_catalogue_and_an_empty_one_does_not():
    assert registry.resolve_public_model("techsara-35b", allowed=[]).id == "techsara-35b"
    assert registry.resolve_public_model("techsara-35b", allowed=None).id == "techsara-35b"
    assert registry.resolve_public_model("techsara-35b", allowed=["techsara-35b"]) is not None
    assert registry.resolve_public_model("techsara-35b", allowed=["something-else"]) is None
    assert registry.resolve_public_model("", allowed=None) is None
    assert registry.resolve_public_model("  techsara-35b  ") is not None


# ------------------------------------------------ the verifier's residuals --
#
# Everything below was added on 2026-09-13 in answer to the independent
# verifier's read of wave 1. Each block names the mutation that used to survive
# this suite: the point of a test is not that the code is right today, it is
# that a change which made it wrong would be caught.


def test_the_scrubber_removes_every_kind_of_thing_the_contract_forbids():
    # The wave-1 suite exercised ONE clause of `_redact_url` (a host with no
    # dot) and none of the env-assignment rule, so deleting three of five
    # clauses left all 63 tests green. One assertion per rule, so a deletion is
    # a red test rather than a quiet regression.
    cases = {
        "env value": "boom API_KEY_PEPPER=s3cr3tvalue",
        "credentials in a URL": "see https://user:pw@db.example.com/x",
        "an internal suffix": "probe https://postgres.svc/health",
        "a private IP host": "dial https://10.0.0.7/x",
        "localhost": "dial http://localhost:8000/x",
        "a compose service name": "dial http://vllm-embed:30003/v1",
        "a private IP on its own": "the peer was 172.18.0.4",
        "a container path": "opening /app/app/llm.py failed",
        "a statement": "SELECT secret FROM api_keys WHERE id = 1",
        "a traceback": "Traceback (most recent call last):\n  File x\nValueError: y",
    }
    for what, message in cases.items():
        assert "[redacted]" in errors.redact(message), what

    for what, leak in (
        ("the pepper", "s3cr3tvalue"),
        ("the password", "user:pw"),
        ("the private address", "10.0.0.7"),
        ("the container path", "/app/app/llm.py"),
        ("the column list", "secret FROM api_keys"),
    ):
        assert leak not in errors.redact(cases_value(cases, leak)), what

    # …and ordinary prose with a public link survives untouched, or the
    # scrubber would make every message useless.
    kept = "See https://docs.techsarasolutions.com/api for the retry table."
    assert errors.redact(kept) == kept


def cases_value(cases, leak):
    """The one message that carries `leak`. A helper so the assertion above
    reads as a table rather than as an index."""
    return next(message for message in cases.values() if leak in message)


def test_the_scrubber_removes_a_presented_api_key():
    # CONTRACT §5: the secret is never stored, logged, returned or displayed.
    # Nothing echoes a key today — `invalid_api_key()` is one fixed sentence —
    # so this is defence in depth for the waves that put key material beside
    # these code paths.
    for token in (
        "tsk_live_0123456789abcdef_AbCdEfGh",
        "tsk_test_0123456789abcdef_AbCdEfGh",
    ):
        scrubbed = errors.redact(f"key {token} was rejected")
        assert token not in scrubbed
        assert "[redacted]" in scrubbed


def test_a_leaky_message_is_scrubbed_in_the_mid_stream_frame_as_well():
    # CONTRACT §9: the envelope applies "everywhere, including mid-stream".
    # Changing `stream_payload` to skip `redact()` used to leave 63 tests green.
    leaky = errors.internal_error(
        "at /app/x talking to http://vllm-router:30002 on 172.18.0.4"
    )
    emitter = events.SequencedEvents()
    frame = emitter.error(leaky)

    for forbidden in ("/app/x", "http://vllm-router:30002", "172.18.0.4"):
        assert forbidden not in frame
        assert forbidden not in json.dumps(leaky.envelope("req_1"))
        assert forbidden not in json.dumps(leaky.stream_payload(1))


def test_a_failed_responses_error_message_is_scrubbed_on_its_way_to_the_wire():
    # `Response.to_wire()` is what `GET /v1/responses/{id}` returns after a
    # failure and what `response.failed` carries. It used to dump the message
    # verbatim.
    response = models.Response(
        id="resp_1",
        created_at=1,
        status="failed",
        model=registry.TECHSARA_35B,
        error=models.ResponseError(
            code="internal_error",
            message='connection to "vllm-router" (172.18.0.4), port 30002; see /app/app/llm.py',
        ),
    )
    wire = response.to_wire()
    frame = events.SequencedEvents().failed(wire)

    for forbidden in ("172.18.0.4", "/app/app/llm.py"):
        assert forbidden not in json.dumps(wire)
        assert forbidden not in frame


def test_an_unexpected_exception_loses_even_the_container_name_it_named():
    # The scrubber cannot recognise a bare container name — `vllm-router` reads
    # as an ordinary word — which is exactly why `from_unexpected` DISCARDS the
    # text instead of filtering it. This is the whole chain, end to end.
    raised = ValueError(
        'connection to server at "vllm-router" (172.18.0.4), port 30002 failed'
    )
    failure = errors.from_unexpected(raised, request_id="req_1")
    response = models.Response(
        id="resp_1",
        created_at=1,
        status="failed",
        model=registry.TECHSARA_35B,
        error=models.ResponseError(code=failure.code, message=failure.message),
    )

    body = json.dumps(response.to_wire()) + json.dumps(failure.envelope("req_1"))
    for forbidden in ("vllm-router", "172.18.0.4", "30002"):
        assert forbidden not in body


def test_a_caller_supplied_field_name_is_never_echoed_into_the_envelope():
    # With `extra="forbid"`, pydantic puts the caller's OWN key in `loc` and
    # `_param_from_loc` turned it into `param`. This body used to put a
    # filesystem path and a private IP into an error envelope.
    hostile = 'x\nevent: error\ndata: {"pwn":1}\n\n/etc/passwd 10.0.0.7'
    with pytest.raises(errors.ApiError) as raised:
        models.parse_responses_request(
            {"model": registry.TECHSARA_35B, "input": "hi", hostile: 1}
        )

    envelope = raised.value.envelope("req_1")
    assert envelope["error"]["param"] is None
    assert "/etc/passwd" not in json.dumps(envelope)
    assert "10.0.0.7" not in json.dumps(envelope)
    # The caller still learns what to fix.
    assert "not permitted" in envelope["error"]["message"].lower()


def test_a_param_that_names_a_real_field_still_reaches_the_caller():
    # The validation above must not be a blanket "never echo": a plain field
    # path is exactly what a caller needs to find the mistake in their body.
    with pytest.raises(errors.ApiError) as raised:
        models.parse_responses_request(
            {"model": registry.TECHSARA_35B, "input": "hi", "top_p": 0.9}
        )
    assert raised.value.param == "top_p"

    with pytest.raises(errors.ApiError) as nested:
        models.parse_responses_request(
            {"model": registry.TECHSARA_35B, "input": "hi", "metadata": {"a": 1}}
        )
    assert nested.value.param == "metadata.a"


def test_a_near_miss_of_an_internal_engine_url_is_refused_too():
    # The guard used to compare `_clean`ed STRINGS, so every one of these —
    # the same engine, spelled slightly differently — was ALLOWED. The OCR move
    # to the worker (2026-09-09) is precisely this shape: one env var away from
    # the main model, and `http://192.168.9.68:30007/v1` is the same target as
    # `192.168.9.68:30007`.
    router_url = settings.router_base_url  # e.g. http://vllm-router:30002/v1
    host = router_url.split("://", 1)[1].split("/", 1)[0]
    bare_host = host.split(":", 1)[0]

    for near_miss in (
        router_url,
        router_url.rstrip("/v1"),
        f"http://{host}",
        f"https://{host}/v1",
        host,
        bare_host,
        f"{router_url}/",
        router_url.upper(),
    ):
        with pytest.raises(registry.InternalTargetError):
            registry.guard_internal_target(near_miss)


def test_the_guard_still_permits_the_main_model_and_an_ordinary_public_id():
    # A guard that refused everything would be just as useless as one that
    # refused nothing, and `declared_models()` must keep building.
    assert registry.guard_internal_target(settings.llm_model) == settings.llm_model
    assert registry.guard_internal_target("techsara-35b") == "techsara-35b"
    assert len(registry.declared_models()) == 1


def test_the_lifecycle_can_only_move_forward():
    # The only ordering rule used to be "an opener first, nothing after the
    # terminal". Everything in between was unchecked, so a route could renumber
    # or contradict what a client had already rendered.
    opening = models.Response(
        id="resp_1", created_at=1, status="queued", model=registry.TECHSARA_35B
    ).to_wire()

    second_created = events.SequencedEvents()
    second_created.created(opening)
    second_created.in_progress(dict(opening, status="in_progress"))
    with pytest.raises(events.StreamProtocolError):
        second_created.created(opening)

    queued_after_deltas = events.SequencedEvents()
    queued_after_deltas.created(opening)
    queued_after_deltas.in_progress(dict(opening, status="in_progress"))
    queued_after_deltas.output_text_delta("half an ")
    with pytest.raises(events.StreamProtocolError):
        queued_after_deltas.queued(opening)

    delta_after_done = events.SequencedEvents()
    delta_after_done.created(opening)
    delta_after_done.in_progress(dict(opening, status="in_progress"))
    delta_after_done.output_text_delta("all of it")
    delta_after_done.output_text_done("all of it")
    with pytest.raises(events.StreamProtocolError):
        delta_after_done.output_text_delta("more")


def test_a_stage_may_be_skipped_and_a_failure_may_interrupt_anything():
    # Skipping is legitimate — a request refused before admission never reaches
    # `in_progress`, and a generation that produced no text has no delta — so
    # the rule is "forward only", not "every stage".
    opening = models.Response(
        id="resp_1", created_at=1, status="queued", model=registry.TECHSARA_35B
    ).to_wire()

    emitter = events.SequencedEvents()
    emitter.created(opening)
    emitter.output_text_done("")
    emitter.completed(dict(opening, status="completed"))
    assert emitter.terminal_event == "response.completed"

    interrupted = events.SequencedEvents()
    interrupted.created(opening)
    interrupted.in_progress(dict(opening, status="in_progress"))
    interrupted.output_text_delta("half")
    interrupted.error(errors.model_unavailable(30))
    assert interrupted.terminal_event == "error"


def test_the_compatibility_dialect_has_its_own_error_frame():
    # Without one, a route serving this surface had to hand-roll the single
    # thing CONTRACT §9 says must be identical on both.
    chunks = events.ChatCompletionChunks(completion_id="chatcmpl_1", model="techsara-35b")
    frame = chunks.error_chunk(
        errors.internal_error("at /app/x on 10.0.0.7")
    )

    assert frame.startswith("data: ")
    payload = json.loads(frame[len("data: ") :].strip())
    assert payload["error"]["code"] == "internal_error"
    assert payload["error"]["type"] == "server_error"
    assert payload["choices"] == [] and payload["usage"] is None
    # Same scrubbing as everywhere else.
    assert "/app/x" not in frame and "10.0.0.7" not in frame
    # It does not end the stream: a client library waits for the sentinel.
    assert chunks.finished is False
    assert chunks.done() == events.DONE_SENTINEL


# ---------------------------------------------------------------------------
# The public document follows PUBLIC_API_ENFORCE_LIMITS (owner decision,
# 2026-09-13: unlimited by default)
# ---------------------------------------------------------------------------


def _operations(document):
    for path, item in document["paths"].items():
        for method, operation in item.items():
            yield f"{method.upper()} {path}", operation


def test_with_the_limits_off_the_public_document_advertises_no_usage_limit(monkeypatch):
    """A client generator turns every documented 429 and RateLimit header into
    throttling code. With the limits off: no RateLimit header anywhere, no
    limit code anywhere, and no 429 on ANY route — the server sends none. The
    two refusals that remain on the generating routes are documented as what
    they are: a 409 whose Retry-After marks a still-running Idempotency-Key,
    and a 503 that names the engine at capacity."""
    from app.publicapi.openapi import public_openapi

    monkeypatch.setattr(settings, "public_api_enforce_limits", False)
    document = public_openapi()
    text = json.dumps(document)
    paths = json.dumps(document["paths"])

    assert "RateLimit" not in text
    for code in ("quota_exceeded", "rate_limit_error", "concurrency_limit_exceeded"):
        assert code not in paths, code
    generating = {"POST /v1/responses", "POST /v1/chat/completions"}
    for name, operation in _operations(document):
        responses = operation["responses"]
        assert "429" not in responses, name
        if name in generating:
            assert "still running" in responses["409"]["description"], name
            assert responses["409"]["headers"]["Retry-After"]["required"] is False
            assert "at capacity" in responses["503"]["description"], name
            assert "Retry-After" in responses["503"]["headers"]
    assert "no request, token-per-minute, daily or concurrency limits" in (
        document["info"]["description"]
    )


def test_with_the_limits_on_the_public_document_is_what_it_was_before_the_switch(monkeypatch):
    """Enforced, the document keeps every limit it advertised: RateLimit on
    the success responses, `rate_limit_error` on every authenticated route and
    `quota_exceeded` / `concurrency_limit_exceeded` on the generating ones."""
    from app.publicapi.openapi import public_openapi

    monkeypatch.setattr(settings, "public_api_enforce_limits", True)
    document = public_openapi()

    for name, operation in _operations(document):
        responses = operation["responses"]
        if name == "GET /v1/openapi.json":
            continue
        assert "RateLimit" in responses["200"]["headers"], name
        assert "rate_limit_error" in responses["429"]["description"], name
    create = document["paths"]["/v1/responses"]["post"]["responses"]["429"]["description"]
    assert "quota_exceeded" in create and "concurrency_limit_exceeded" in create
    assert "limits: usage is recorded" not in document["info"]["description"]


def test_both_forms_of_the_public_document_are_valid_openapi_3_1(monkeypatch):
    """The CI contract gate validates whichever document the default builds;
    both must pass its structural checks and publish the same operations."""
    import importlib.util
    from pathlib import Path

    from app.publicapi.openapi import public_openapi

    gate_path = Path(__file__).resolve().parents[2] / ".github/workflows/scripts/api_contract.py"
    if not gate_path.is_file():  # an image built from orchestrator/ alone
        pytest.skip(f"the CI contract gate is not in this checkout ({gate_path})")
    spec = importlib.util.spec_from_file_location("api_contract_gate", gate_path)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)

    shapes = {}
    for enforced in (False, True):
        monkeypatch.setattr(settings, "public_api_enforce_limits", enforced)
        document = public_openapi()
        assert gate.validate_openapi_31(document) == [], enforced
        shapes[enforced] = gate.document_operations(document)
    assert shapes[False] == shapes[True]


def test_a_full_engine_and_an_outstanding_idempotent_request_are_not_limit_errors():
    """Owner decision 2026-09-13: no rate, token, quota or concurrency limit on
    /v1. The two refusals that remain are physical facts, and they must not be
    spelled as limits: the shared engine at capacity is a retryable 503, and a
    repeat of a still-running Idempotency-Key is a 409."""
    from app.publicapi import router as public_router, streaming

    class AdmissionRejected(RuntimeError):
        pass

    capacity = streaming.engine_error(AdmissionRejected("lanes full"))
    assert (capacity.code, capacity.status) == ("model_unavailable", 503)
    assert int(capacity.headers()["Retry-After"]) >= 1

    running = public_router._still_running()
    assert (running.code, running.status) == ("idempotency_conflict", 409)
    assert "still running" in running.message and int(running.headers()["Retry-After"]) >= 1
