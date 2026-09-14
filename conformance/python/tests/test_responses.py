"""POST /v1/responses, synchronous, through the SDK (CONTRACT-3 §8.1, §8.3, §9)."""
from __future__ import annotations

import openai
import pytest

from techsara_conformance import asserts

PONG = "Reply with the single word: pong"


def _usage_is_consistent(usage) -> None:
    assert usage is not None, "usage is null: allowed only when the engine did not report counts (§9), and it does here"
    assert usage.input_tokens > 0 and usage.output_tokens > 0
    assert usage.total_tokens == usage.input_tokens + usage.output_tokens


def test_a_synchronous_response_completes_with_text_and_usage(client, target):
    raw = client.responses.with_raw_response.create(
        model=target.models["chat"], input=PONG, max_output_tokens=target.small_output_tokens, temperature=0
    )
    assert raw.http_response.status_code == 200
    assert raw.headers.get("x-request-id")
    response = raw.parse()
    assert response.id.startswith("resp_"), response.id
    assert response.object == "response"
    assert response.status == "completed"
    assert response.model == target.models["chat"]
    assert response.output_text.strip(), "no output text"
    assert [item.type for item in response.output] == ["message"]
    assert response.output[0].role == "assistant"
    _usage_is_consistent(response.usage)


def test_instructions_and_a_message_list_are_accepted_as_input(client, target):
    response = client.responses.create(
        model=target.models["chat"],
        instructions="You answer with one lowercase word.",
        input=[
            {"role": "user", "content": "Say hello."},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": PONG},
        ],
        max_output_tokens=target.small_output_tokens,
        temperature=0,
    )
    assert response.status == "completed"
    assert response.output_text.strip()


def test_a_response_can_be_read_back_by_id_from_the_same_project(client, target):
    created = client.responses.create(model=target.models["chat"], input=PONG, max_output_tokens=target.small_output_tokens)
    fetched = client.responses.retrieve(created.id)
    assert fetched.id == created.id
    assert fetched.status == "completed"
    assert fetched.model == created.model
    # Output text of a synchronous request is not stored by default (§16), so
    # only the status, model and usage are compared.
    if fetched.usage is not None and created.usage is not None:
        assert fetched.usage.total_tokens == created.usage.total_tokens


def test_reading_an_unknown_response_id_is_a_404_response_not_found(client):
    with pytest.raises(openai.NotFoundError) as caught:
        client.responses.retrieve("resp_000000000000000000000000")
    asserts.sdk_error(caught.value, code="response_not_found")


def test_a_parameter_the_platform_cannot_honour_is_refused_by_name(client, target):
    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(
            model=target.models["chat"],
            input=PONG,
            max_output_tokens=target.small_output_tokens,
            extra_body={"frobnicate": True},
        )
    error = asserts.sdk_error(caught.value, code="invalid_request_error")
    assert error["param"] == "frobnicate" or "frobnicate" in error["message"], error


def test_stream_and_background_together_are_refused(client, target):
    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(
            model=target.models["chat"], input=PONG, max_output_tokens=target.small_output_tokens, stream=True, background=True
        )
    asserts.sdk_error(caught.value, code="invalid_request_error")


@pytest.mark.feature("output_ceiling")
def test_max_output_tokens_one_above_the_advertised_ceiling_is_a_400_naming_it(client, target):
    # WHY THE GUARD (2026-09-13, review finding): this was a CORE test sending
    # 1,000,001, and it passed on the e2e build because that build refuses
    # everything above 8,192 — it proved nothing about the 1,000,000 boundary.
    # The refusal counts only when the model advertises exactly that ceiling;
    # its acceptance is the next test.
    ceiling = target.expected_chat_max_output_tokens
    advertised = client.models.retrieve(target.models["chat"]).model_dump().get("max_output_tokens")
    assert advertised == ceiling, (
        f"{target.models['chat']} advertises max_output_tokens {advertised!r}, not {ceiling}: "
        f"a refusal of {ceiling + 1} would prove nothing about the ceiling"
    )
    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(model=target.models["chat"], input=PONG, max_output_tokens=ceiling + 1)
    asserts.sdk_error(caught.value, code="invalid_request_error", param="max_output_tokens")


@pytest.mark.main_long_gate
@pytest.mark.feature("output_ceiling")
def test_max_output_tokens_of_one_million_is_accepted_and_clamped_not_refused(client, target):
    # The model stops after one word, so this costs a handful of tokens: what
    # is under test is the ADMISSION of a 1,000,000 ceiling (§8.3 step 3).
    #
    # SIDE EFFECT (2026-09-13, review finding): input + planned output is over
    # PUBLIC_API_MAIN_LONG_FOOTPRINT_TOKENS (131,072), so for as long as this
    # request runs it holds `main.long`, the ONE public long slot shared by
    # every project (§12.3). A customer's long request arriving meanwhile
    # waits; if one is already running, this request waits up to 30 s and is
    # refused 503 "at capacity" — correct server behaviour, so it is a SKIP,
    # not a FAIL. Deselect with -m "not main_long_gate" where that matters.
    try:
        response = client.responses.create(
            model=target.models["chat"], input=PONG, max_output_tokens=target.expected_chat_max_output_tokens, temperature=0
        )
    except openai.APIStatusError as exc:
        body = exc.body if isinstance(exc.body, dict) else {}
        error = body.get("error", body) if isinstance(body, dict) else {}
        if exc.status_code == 503 and exc.code == "model_unavailable" and "capacity" in str(error.get("message", "")).lower():
            pytest.skip(
                "main.long gate at capacity: another public long request holds the only slot "
                f"(Retry-After {exc.response.headers.get('retry-after')!r}); the admission of the ceiling was not observed"
            )
        raise
    assert response.status == "completed"
    wire = response.model_dump()
    applied = wire.get("max_output_tokens")
    assert isinstance(applied, int) and 1 <= applied <= 1_000_000, f"applied max_output_tokens {applied!r}"
    assert "incomplete_details" in wire and wire["incomplete_details"] is None, wire.get("incomplete_details")


@pytest.mark.feature("output_ceiling")
def test_hitting_the_applied_ceiling_reports_incomplete_details_and_stays_completed(client, target):
    response = client.responses.create(
        model=target.models["chat"],
        input="Count from 1 to 200, separated by spaces.",
        max_output_tokens=5,
        temperature=0,
    )
    wire = response.model_dump()
    assert response.status == "completed", "status stays completed; the V34 CHECK has no 'incomplete' (§9)"
    assert wire.get("incomplete_details") == {"reason": "max_output_tokens"}, wire.get("incomplete_details")
    assert wire.get("max_output_tokens") == 5, f"applied {wire.get('max_output_tokens')!r}"
    assert response.usage is not None and response.usage.output_tokens <= 5
