"""POST /v1/embeddings through the SDK (CONTRACT-3 §8.4). Planned 2026-09-13."""
from __future__ import annotations

import math
import uuid

import openai
import pytest

from techsara_conformance import asserts

pytestmark = pytest.mark.feature("embeddings")
DIMENSIONS = 1024


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def test_the_sdk_default_request_round_trips_to_float_vectors_in_input_order(make_client, attempt_log, target):
    # openai-python sends encoding_format="base64" when the caller names none
    # and decodes it client-side, so THIS is the path every SDK user takes.
    client = make_client(log=attempt_log)
    inputs = ["The console rotates API keys.", "Webhooks are signed with HMAC-SHA256.", "The console rotates API keys."]
    result = client.embeddings.create(model=target.models["embed"], input=inputs)
    sent = attempt_log.requests[0]["headers"]
    assert "idempotency-key" not in {k.lower() for k in sent}, "the SDK sends no Idempotency-Key by default (the route refuses one)"
    assert result.object == "list" and result.model == target.models["embed"]
    assert [d.index for d in result.data] == [0, 1, 2]
    for item in result.data:
        assert item.object == "embedding"
        assert isinstance(item.embedding, list) and len(item.embedding) == DIMENSIONS, len(item.embedding)
    assert _cosine(result.data[0].embedding, result.data[2].embedding) > 0.999, "identical inputs embed identically"
    assert _cosine(result.data[0].embedding, result.data[1].embedding) < 0.99, "different inputs embed differently"
    assert result.usage is not None and result.usage.prompt_tokens > 0
    assert result.usage.total_tokens == result.usage.prompt_tokens


def test_float_encoding_returns_json_numbers(raw, target):
    response = raw.post("embeddings", json={"model": target.models["embed"], "input": "one string", "encoding_format": "float"})
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    vector = body["data"][0]["embedding"]
    assert isinstance(vector, list) and len(vector) == DIMENSIONS and all(isinstance(x, float) for x in vector[:8])
    assert response.headers.get("x-request-id")


def test_dimensions_is_refused_by_name_rather_than_ignored(client, target):
    with pytest.raises(openai.BadRequestError) as caught:
        client.embeddings.create(model=target.models["embed"], input="x", dimensions=256)
    asserts.sdk_error(caught.value, code="invalid_request_error", param="dimensions")


def test_token_array_input_is_refused(raw, target):
    response = raw.post("embeddings", json={"model": target.models["embed"], "input": [[1, 2, 3]]})
    asserts.envelope(response.status_code, response.json(), response.headers, code="invalid_request_error")


def test_an_idempotency_key_on_embeddings_is_refused_with_400(client, target):
    with pytest.raises(openai.BadRequestError) as caught:
        client.embeddings.create(model=target.models["embed"], input="x", extra_headers={"Idempotency-Key": f"conformance-{uuid.uuid4()}"})
    asserts.sdk_error(caught.value, code="invalid_request_error", param="Idempotency-Key")


def test_the_embedding_model_on_the_responses_endpoint_is_a_400_naming_model(client, target):
    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(model=target.models["embed"], input="x", max_output_tokens=4)
    error = asserts.sdk_error(caught.value, code="invalid_request_error", param="model")
    assert "/v1/responses" in error["message"], error["message"]


def test_a_chat_model_on_the_embeddings_endpoint_is_a_400_naming_model(client, target):
    with pytest.raises(openai.BadRequestError) as caught:
        client.embeddings.create(model=target.models["chat"], input="x")
    asserts.sdk_error(caught.value, code="invalid_request_error", param="model")
