"""GET /v1/models and /v1/models/{model} through the SDK (CONTRACT-3 §7, §15)."""
from __future__ import annotations

import openai
import pytest

from techsara_conformance import asserts

KINDS = {"chat", "embedding", "rerank", "transcription"}
CAPABILITY_FLAGS = {"chat", "streaming", "vision", "tools", "embeddings", "rerank", "audio_transcription", "ocr", "background"}


def test_the_model_list_is_an_openai_list_that_includes_the_chat_model(make_client, target):
    client = make_client()
    raw = client.models.with_raw_response.list()
    assert raw.headers.get("x-request-id"), "every response carries X-Request-Id (§7)"
    page = raw.parse()
    ids = [m.id for m in page.data]
    assert ids, "the list is empty"
    assert target.models["chat"] in ids, f"{target.models['chat']} not in {ids}"
    for model in page.data:
        assert model.object == "model"
        assert model.owned_by == "techsara"
        assert model.id.startswith("techsara-"), f"an internal name leaked into the list: {model.id!r}"


def test_retrieving_a_listed_model_returns_the_same_object(client, target):
    listed = {m.id: m for m in client.models.list().data}
    model = client.models.retrieve(target.models["chat"])
    assert model.id == target.models["chat"]
    assert model.object == "model"
    assert model.model_dump() == listed[model.id].model_dump(), "retrieve and list disagree about the same model"


def test_retrieving_an_unknown_model_is_a_404_model_not_found_envelope(client):
    with pytest.raises(openai.NotFoundError) as caught:
        client.models.retrieve("techsara-no-such-model")
    asserts.sdk_error(caught.value, code="model_not_found")


@pytest.mark.feature("model_catalogue")
def test_every_listed_model_declares_kind_capabilities_endpoints_and_ceilings(client):
    for model in client.models.list().data:
        extra = model.model_dump()
        assert extra.get("kind") in KINDS, f"{model.id}: kind {extra.get('kind')!r}"
        capabilities = extra.get("capabilities") or {}
        assert CAPABILITY_FLAGS <= set(capabilities), f"{model.id}: capabilities missing {CAPABILITY_FLAGS - set(capabilities)}"
        assert capabilities["tools"] is False, f"{model.id}: tools is false for every model (§15)"
        assert isinstance(extra.get("endpoints"), list) and extra["endpoints"], f"{model.id}: no endpoints"
        for key in ("context_window", "max_input_tokens", "max_output_tokens", "default_max_output_tokens"):
            assert key in extra, f"{model.id}: {key} missing (null is allowed, absent is not)"
            assert extra[key] is None or (isinstance(extra[key], int) and extra[key] > 0), f"{model.id}: {key}={extra[key]!r}"
        assert isinstance(extra.get("limits"), dict), f"{model.id}: limits must be an object"


@pytest.mark.feature("model_catalogue")
def test_all_six_public_models_are_listed_with_their_kinds(client, target):
    listed = {m.id: m.model_dump() for m in client.models.list().data}
    expected = {
        target.models["chat"]: "chat",
        target.models["vision"]: "chat",
        target.models["ocr"]: "chat",
        target.models["embed"]: "embedding",
        target.models["rerank"]: "rerank",
        target.models["whisper"]: "transcription",
    }
    missing = sorted(set(expected) - set(listed))
    assert not missing, f"not listed: {missing} (listed: {sorted(listed)})"
    for model_id, kind in expected.items():
        assert listed[model_id].get("kind") == kind, f"{model_id}: kind {listed[model_id].get('kind')!r}, expected {kind}"


@pytest.mark.feature("output_ceiling")
def test_the_chat_model_advertises_the_one_million_token_output_ceiling(client, target):
    model = client.models.retrieve(target.models["chat"]).model_dump()
    assert model.get("max_output_tokens") == target.expected_chat_max_output_tokens, (
        f"max_output_tokens {model.get('max_output_tokens')!r}, CONTRACT-3 §8.3 promises "
        f"{target.expected_chat_max_output_tokens} (TECHSARA_EXPECT_CHAT_MAX_OUTPUT_TOKENS)"
    )
    assert model.get("default_max_output_tokens") == 8192, f"default {model.get('default_max_output_tokens')!r}"


@pytest.mark.feature("limits_off")
def test_no_ratelimit_headers_are_sent_when_limits_are_off(client):
    raw = client.models.with_raw_response.list()
    present = [h for h in ("ratelimit", "ratelimit-policy") if h in raw.headers]
    assert not present, f"limits are off by owner decision (§12.1) but the response carries {present}"
