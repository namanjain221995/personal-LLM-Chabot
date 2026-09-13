"""`POST /v1/embeddings` over real HTTP, real keys and a real database.

The engine is an `httpx.MockTransport` behind a real `AsyncOpenAI` client
(tests/publicapi_sidecar_support.py); everything between the socket and that
transport is the production code path.
"""
from __future__ import annotations

import array
import base64
import json
import threading
import time

import httpx
import pytest

from app.config import settings
from app.publicapi import capacity, sidecars
from tests.publicapi_sidecar_support import (  # noqa: F401 - fixtures by name
    _pepper,
    api,
    assert_nothing_internal,
    auth,
    daily,
    engines_configured,
    install_embed_engine,
    platform,
    usage_events,
    vllm_embeddings,
)

URL = "/v1/embeddings"


def _body(**overrides):
    body = {"model": "techsara-embed", "input": ["first passage", "second passage"]}
    body.update(overrides)
    return body


# ------------------------------------------------------------- the answer --


def test_an_embedding_request_returns_one_vector_per_input_in_input_order(api, monkeypatch):
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    response = api.post(URL, json=_body(input=["a", "b", "c"]), headers=auth())

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["object"] == "list"
    assert payload["model"] == "techsara-embed"
    assert [item["index"] for item in payload["data"]] == [0, 1, 2]
    assert [item["embedding"][0] for item in payload["data"]] == [1.0, 2.0, 3.0]
    assert all(item["object"] == "embedding" for item in payload["data"])
    assert payload["usage"] == {"prompt_tokens": 6, "total_tokens": 6}
    assert response.headers["X-Request-Id"].startswith("req_")
    # The caller's text reaches the engine exactly as sent, under the served
    # model name — which never comes back out.
    assert engine.json_bodies()[0]["input"] == ["a", "b", "c"]
    assert_nothing_internal(response)


def test_the_text_is_embedded_as_sent_without_the_chat_apps_character_cap(api, monkeypatch):
    """`llm.embed_texts` clips at EMBED_INPUT_CHAR_CAP; a public caller billed
    by the token must get the embedding of the text they sent."""
    monkeypatch.setattr(settings, "embed_input_char_cap", 10)
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))
    long_text = "  leading spaces and " + "word " * 200

    response = api.post(URL, json=_body(input=long_text), headers=auth())

    assert response.status_code == 200
    assert engine.json_bodies()[0]["input"] == [long_text]


def test_a_missing_usage_block_is_null_and_never_zero(api, monkeypatch):
    install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body, usage=False))

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 200
    assert response.json()["usage"] is None


def test_base64_is_the_little_endian_float32_of_the_same_vector(api, monkeypatch):
    install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    floats = api.post(URL, json=_body(input="x"), headers=auth()).json()
    encoded = api.post(URL, json=_body(input="x", encoding_format="base64"), headers=auth()).json()

    raw = base64.b64decode(encoded["data"][0]["embedding"])
    decoded = array.array("f")
    decoded.frombytes(raw)
    assert list(decoded) == floats["data"][0]["embedding"]
    # Little-endian on the wire whatever the host: 1.0f is 00 00 80 3f.
    assert raw[:4] == b"\x00\x00\x80\x3f"


# ------------------------------------------------------ who may call it --


def test_a_request_without_a_key_is_a_401_and_never_reaches_the_engine(api, monkeypatch):
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    missing = api.post(URL, json=_body())
    unknown = api.post(URL, json=_body(), headers={"Authorization": "Bearer tsk_live_nope_nope"})
    api.cookies.set("ts_session", "a-perfectly-valid-looking-session")
    cookie_only = api.post(URL, json=_body())
    api.cookies.clear()

    for response in (missing, unknown, cookie_only):
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"
    assert missing.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert engine.calls == 0


def test_a_key_without_embeddings_write_is_a_403_naming_the_scope_and_nothing_it_holds(api, monkeypatch):
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    response = api.post(URL, json=_body(), headers=auth("narrow"))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert response.headers["WWW-Authenticate"] == 'Bearer error="insufficient_scope", scope="embeddings.write"'
    assert "responses.write" not in response.text + str(response.headers)
    assert engine.calls == 0


def test_a_key_allowlisted_to_another_model_gets_the_same_404_as_no_such_model(api, monkeypatch):
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    forbidden = api.post(URL, json=_body(), headers=auth("chatonly"))
    unknown = api.post(URL, json=_body(model="techsara-nothing"), headers=auth())

    assert forbidden.status_code == unknown.status_code == 404
    assert forbidden.json()["error"]["code"] == "model_not_found"
    assert engine.calls == 0


def test_a_chat_model_on_the_embeddings_endpoint_is_a_400_naming_model(api, monkeypatch):
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    response = api.post(URL, json=_body(model="techsara-35b"), headers=auth())

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["param"]) == ("invalid_request_error", "model")
    assert error["message"] == "The model `techsara-35b` does not support /v1/embeddings."
    assert engine.calls == 0


def test_an_unconfigured_embedding_engine_is_not_a_model_anyone_can_address(api, monkeypatch):
    monkeypatch.setattr(settings, "embed_base_url", "")
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 404
    assert engine.calls == 0


# ------------------------------------------------------------ the body --


def test_a_body_over_the_cap_is_a_413_and_the_engine_is_never_called(api, monkeypatch):
    monkeypatch.setattr(settings, "public_api_max_body_bytes", 512, raising=False)
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    response = api.post(URL, json=_body(input="x" * 4096), headers=auth())

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert engine.calls == 0


@pytest.mark.parametrize(
    "overrides, param",
    [
        ({"dimensions": 256}, "dimensions"),
        ({"user": "someone"}, "user"),
        ({"input": [[1, 2, 3]]}, "input"),
        ({"input": [101, 102]}, "input"),
        ({"input": ["fine", "   "]}, "input.1"),
        ({"encoding_format": "int8"}, "encoding_format"),
    ],
)
def test_a_parameter_the_engine_cannot_honour_is_refused_by_name(api, monkeypatch, overrides, param):
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    response = api.post(URL, json=_body(**overrides), headers=auth())

    assert response.status_code == 400
    assert response.json()["error"]["param"] == param
    assert engine.calls == 0


def test_more_inputs_than_the_published_maximum_is_a_400(api, monkeypatch):
    monkeypatch.setattr(settings, "public_api_embed_max_inputs", 3, raising=False)
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    response = api.post(URL, json=_body(input=["x"] * 4), headers=auth())

    assert response.status_code == 400
    assert "at most 3" in response.json()["error"]["message"]
    assert engine.calls == 0


def test_an_idempotency_key_is_refused_rather_than_silently_ignored(api, monkeypatch):
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    response = api.post(URL, json=_body(), headers={**auth(), "Idempotency-Key": "abc"})

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "Idempotency-Key"
    assert engine.calls == 0


# -------------------------------------------------------- the engine --


def test_inputs_are_packed_into_engine_calls_of_sixteen_each_holding_the_embed_gate(api, monkeypatch):
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))
    gates = []
    real_hold = capacity.hold

    def recording_hold(gate, **kwargs):
        gates.append((gate, kwargs["weight_tokens"]))
        return real_hold(gate, **kwargs)

    monkeypatch.setattr(capacity, "hold", recording_hold)

    response = api.post(URL, json=_body(input=[f"text {i}" for i in range(40)]), headers=auth())

    assert response.status_code == 200
    assert [len(body["input"]) for body in engine.json_bodies()] == [16, 16, 8]
    assert [gate for gate, _ in gates] == ["embed", "embed", "embed"]
    assert all(weight > 0 for _, weight in gates)
    assert [item["embedding"][0] for item in response.json()["data"]][:17] == [
        float(i + 1) for i in range(16)
    ] + [1.0]
    assert response.json()["usage"]["prompt_tokens"] == 40 * 3
    # One fixed read timeout, never a per-request value (F048).
    assert set(engine.read_timeouts) == {sidecars.EMBED_READ_TIMEOUT_S}


def test_an_unreachable_engine_is_a_503_with_retry_after_that_names_nothing_internal(api, platform, monkeypatch):
    def down(request, body):
        raise httpx.ConnectError("connection refused to embed-engine.internal:30003")

    install_embed_engine(monkeypatch, down)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert int(response.headers["Retry-After"]) >= 1
    assert_nothing_internal(response)
    # The engine was called and failed: one request, one error, one row.
    rows = usage_events("v1_embeddings")
    assert [(row["status"], row["error_kind"]) for row in rows] == [("error", "model_unavailable")]
    assert daily(platform["project"]["id"])["errors"] == 1


def test_an_engine_timeout_is_a_504(api, monkeypatch):
    def slow(request, body):
        raise httpx.ReadTimeout("timed out")

    install_embed_engine(monkeypatch, slow)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "timeout"


def test_an_over_length_input_is_named_by_its_index_after_the_batch_is_refused(api, monkeypatch):
    def engine(request, body):
        payload = json.loads(body)
        if any(text.startswith("too long") for text in payload["input"]):
            return httpx.Response(
                400,
                json={"object": "error", "message": "This model's maximum context length is 4096 tokens. However, you requested 9000 tokens"},
            )
        return vllm_embeddings(body)

    recorded = install_embed_engine(monkeypatch, engine)

    response = api.post(URL, json=_body(input=["ok 0", "ok 1", "too long 2", "ok 3"]), headers=auth())

    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["param"]) == ("context_length_exceeded", "input.2")
    assert "4096" in error["message"] and "9000" not in error["message"]
    # The batch, then the inputs one at a time until the offender.
    assert [len(body["input"]) for body in recorded.json_bodies()] == [4, 1, 1, 1]


def test_a_full_engine_gate_is_a_503_at_capacity_after_the_bounded_wait_not_a_429(api, platform, monkeypatch):
    monkeypatch.setattr(settings, "public_api_embed_max_concurrent", 1, raising=False)
    monkeypatch.setattr(settings, "public_api_gate_wait_s", 0.2, raising=False)
    release = threading.Event()

    async def slow_engine(request, body):
        import asyncio

        # Bounded, so a broken gate fails this test instead of hanging it.
        for _ in range(250):
            if release.is_set():
                break
            await asyncio.sleep(0.02)
        return vllm_embeddings(body)

    install_embed_engine(monkeypatch, slow_engine)
    first: dict = {}

    def hold_the_gate():
        first["response"] = api.post(URL, json=_body(), headers=auth())

    holder = threading.Thread(target=hold_the_gate)
    holder.start()
    deadline = time.monotonic() + 5
    while capacity.snapshot().get("embed", {}).get("in_flight", 0) < 1 and time.monotonic() < deadline:
        time.sleep(0.02)

    refused = api.post(URL, json=_body(), headers=auth())
    release.set()
    holder.join(timeout=10)

    assert refused.status_code == 503
    error = refused.json()["error"]
    assert error["code"] == "model_unavailable" and "at capacity" in error["message"]
    assert int(refused.headers["Retry-After"]) >= 1
    assert first["response"].status_code == 200
    # Nothing ran for the refused request: counted, settled as cancelled, not
    # an error and not a usage_events row.
    assert daily(platform["project"]["id"])["requests"] == 2
    assert daily(platform["project"]["id"])["errors"] == 0
    assert len(usage_events("v1_embeddings")) == 1


# ------------------------------------------------------------ metering --


def test_a_completed_request_is_metered_exactly_once_with_the_engine_count(api, platform, monkeypatch):
    install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    response = api.post(URL, json=_body(input=["one two", "three"]), headers=auth())

    assert response.status_code == 200
    rows = usage_events("v1_embeddings")
    assert len(rows) == 1
    row = rows[0]
    assert (row["model"], row["mode"], row["status"]) == ("techsara-embed", "api", "ok")
    assert (row["input_tokens"], row["output_tokens"]) == (5, 0)
    assert row["generation_id"].startswith("emb_")
    assert row["meta"]["inputs"] == 2 and row["meta"]["engine_calls"] == 1
    assert row["meta"]["request_id"] == response.headers["X-Request-Id"]
    counters = daily(platform["project"]["id"])
    assert counters == {"requests": 1, "input_tokens": 5, "output_tokens": 0, "errors": 0}


def test_a_refused_body_still_counts_as_a_request_but_spends_no_tokens(api, platform, monkeypatch):
    install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body))

    api.post(URL, json=_body(dimensions=3), headers=auth())

    assert daily(platform["project"]["id"]) == {
        "requests": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "errors": 0,
    }
    assert usage_events("v1_embeddings") == []
