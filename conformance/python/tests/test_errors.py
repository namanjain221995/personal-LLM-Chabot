"""The §9 error envelope for the refusals a client must tell apart."""
from __future__ import annotations

import uuid

import httpx
import openai
import pytest

from techsara_conformance import asserts

PONG = "Reply with the single word: pong"


def test_a_bad_key_is_401_invalid_api_key_through_the_sdk(make_client):
    client = make_client(api_key=asserts.FAKE_KEY)
    with pytest.raises(openai.AuthenticationError) as caught:
        client.models.list()
    error = asserts.sdk_error(caught.value, code="invalid_api_key")
    assert error["type"] == "authentication_error"
    assert caught.value.request_id == error["request_id"], "the SDK exposes X-Request-Id as request_id"


@pytest.mark.parametrize(
    "authorization",
    [None, "Bearer", "Basic dXNlcjpwYXNz", "Bearer not-a-techsara-key"],
    ids=["missing", "empty-bearer", "basic", "malformed"],
)
def test_every_unusable_credential_is_the_same_401_envelope(target, authorization):
    headers = {} if authorization is None else {"Authorization": authorization}
    response = httpx.get(f"{target.base_url}/models", headers=headers, timeout=30.0)
    asserts.envelope(response.status_code, response.json(), response.headers, code="invalid_api_key")


def test_a_key_without_the_scope_is_403_insufficient_scope_before_any_generation(make_client, target):
    if not target.limited_api_key:
        pytest.skip("no limited key: set TECHSARA_LIMITED_API_KEY to a key holding only models.read (tools/provision_key.py makes one)")
    limited = make_client(api_key=target.limited_api_key)
    assert limited.models.list().data, "the limited key must itself be valid (models.read)"
    with pytest.raises(openai.PermissionDeniedError) as caught:
        limited.responses.create(model=target.models["chat"], input=PONG, max_output_tokens=target.small_output_tokens)
    asserts.sdk_error(caught.value, code="insufficient_scope")
    with pytest.raises(openai.PermissionDeniedError) as caught:
        limited.chat.completions.create(
            model=target.models["chat"], messages=[{"role": "user", "content": PONG}], max_tokens=target.small_output_tokens
        )
    asserts.sdk_error(caught.value, code="insufficient_scope")


def test_an_unknown_model_is_404_model_not_found_on_both_generating_routes(client):
    with pytest.raises(openai.NotFoundError) as caught:
        client.responses.create(model="techsara-no-such-model", input=PONG, max_output_tokens=4)
    asserts.sdk_error(caught.value, code="model_not_found")
    with pytest.raises(openai.NotFoundError) as caught:
        client.chat.completions.create(
            model="techsara-no-such-model", messages=[{"role": "user", "content": PONG}], max_tokens=4
        )
    asserts.sdk_error(caught.value, code="model_not_found")


@pytest.mark.parametrize("internal", ["Qwen/Qwen3.6-35B-A3B-NVFP4", "Qwen3.6-35B-A3B-NVFP4", "main"])
def test_an_internal_model_or_engine_name_never_resolves_to_a_model(client, internal, note):
    """§15: internal names never leave and never resolve. A name with a `/`
    may be refused as malformed (400, param model) instead of unknown (404);
    either proves no engine was reached."""
    with pytest.raises((openai.NotFoundError, openai.BadRequestError)) as caught:
        client.responses.create(model=internal, input=PONG, max_output_tokens=4)
    exc = caught.value
    if isinstance(exc, openai.NotFoundError):
        asserts.sdk_error(exc, code="model_not_found")
    else:
        error = asserts.sdk_error(exc, code="invalid_request_error", param="model")
        if error["message"].startswith("Value error,"):
            note(f"400 message carries the validator's prefix: {error['message']!r}")


def test_the_same_idempotency_key_and_body_replays_the_original_without_a_second_generation(client, target):
    key = f"conformance-{uuid.uuid4()}"
    body = dict(model=target.models["chat"], input=PONG, max_output_tokens=target.small_output_tokens, temperature=0)
    first = client.responses.create(**body, extra_headers={"Idempotency-Key": key})
    second = client.responses.create(**body, extra_headers={"Idempotency-Key": key})
    assert second.id == first.id, "a replay returns the ORIGINAL response (§13)"
    assert second.status == first.status
    if first.usage is not None and second.usage is not None:
        assert second.usage.total_tokens == first.usage.total_tokens, "the replay reports the original's usage"


def test_the_same_idempotency_key_with_a_different_body_is_409_idempotency_conflict(client, target):
    key = f"conformance-{uuid.uuid4()}"
    client.responses.create(
        model=target.models["chat"], input=PONG, max_output_tokens=target.small_output_tokens, extra_headers={"Idempotency-Key": key}
    )
    with pytest.raises(openai.ConflictError) as caught:
        client.responses.create(
            model=target.models["chat"], input=PONG + " again", max_output_tokens=target.small_output_tokens,
            extra_headers={"Idempotency-Key": key},
        )
    asserts.sdk_error(caught.value, code="idempotency_conflict")


def test_idempotency_keys_are_scoped_per_endpoint(client, target):
    key = f"conformance-{uuid.uuid4()}"
    response = client.responses.create(
        model=target.models["chat"], input=PONG, max_output_tokens=target.small_output_tokens, extra_headers={"Idempotency-Key": key}
    )
    completion = client.chat.completions.create(
        model=target.models["chat"], messages=[{"role": "user", "content": PONG}], max_tokens=target.small_output_tokens,
        extra_headers={"Idempotency-Key": key},
    )
    assert response.status == "completed" and completion.choices, "(project, endpoint, key) scoping: no cross-endpoint 409 (§13)"
