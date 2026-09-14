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
from app.publicapi import capacity, endpoint_models, events, sidecars
from tests.publicapi_sidecar_support import (  # noqa: F401 - fixtures by name
    _pepper,
    api,
    api_port,
    assert_nothing_internal,
    auth,
    daily,
    engines_configured,
    install_embed_engine,
    no_backoff,
    platform,
    script_witness,
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
    monkeypatch.setattr(settings, "public_api_max_body_bytes", 256, raising=False)
    monkeypatch.setattr(settings, "public_api_max_pooling_body_bytes", 512, raising=False)
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
    # One read bound, the silence setting — never a per-request value (F048).
    assert set(engine.read_timeouts) == {endpoint_models.pooling_silence_s()}


def test_an_engine_that_refuses_connections_is_waited_for_and_the_request_completes_when_it_returns(api, platform, monkeypatch):
    """No clock: a restarting engine is waited out with backoff (it used to be
    an immediate 503), and the waits are not engine calls on the ledger."""
    slept = no_backoff(monkeypatch)
    refusals = {"left": 3}

    def restarting(request, body):
        if refusals["left"] > 0:
            refusals["left"] -= 1
            raise httpx.ConnectError("connection refused to embed-engine.internal:30003")
        return vllm_embeddings(body)

    engine = install_embed_engine(monkeypatch, restarting)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 200, response.text
    assert engine.calls == 4
    assert slept == [2.0, 4.0, 8.0]
    rows = usage_events("v1_embeddings")
    assert [(row["status"], row["meta"]["engine_calls"]) for row in rows] == [("ok", 1)]


def test_an_engine_unreachable_for_the_whole_down_grace_is_a_retryable_503_that_names_nothing_internal(api, platform, monkeypatch):
    monkeypatch.setattr(settings, "public_api_engine_down_grace_s", 5.0, raising=False)
    clock = {"now": 1000.0}
    monkeypatch.setattr(sidecars, "_clock", lambda: clock["now"])

    async def sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(sidecars, "_sleep", sleep)

    def down(request, body):
        raise httpx.ConnectError("connection refused to embed-engine.internal:30003")

    engine = install_embed_engine(monkeypatch, down)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert int(response.headers["Retry-After"]) >= 1
    assert response.headers.get("x-should-retry") != "false"
    assert_nothing_internal(response)
    # Backoff 2 s, then the 3 s left of the grace, then the refusal.
    assert engine.calls == 3
    # The request never reached the engine: counted, cancelled, no error row.
    assert usage_events("v1_embeddings") == []
    assert daily(platform["project"]["id"])["errors"] == 0


def test_a_silent_call_on_a_progressing_engine_is_sent_again_until_it_answers(api, monkeypatch):
    witness = script_witness(monkeypatch, [("progressing", 0)] * 5)
    silent = {"left": 5}

    async def slow_then_fine(request, body):
        if silent["left"] > 0:
            silent["left"] -= 1
            raise httpx.ReadTimeout("silent for the read bound")
        return vllm_embeddings(body)

    engine = install_embed_engine(monkeypatch, slow_then_fine)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 200, response.text
    assert engine.calls == 6
    assert witness.witness.asked == ["progressing"] * 5
    # ONE witness for the whole call, re-sends included: released between
    # re-sends, a lone request's restart history was thrown away (review
    # 2026-09-14, high).
    assert witness.acquired == witness.released == 1
    assert usage_events("v1_embeddings")[0]["meta"]["resends"] == {"progressing": 5}


@pytest.mark.parametrize(
    "verdict, sends",
    [("unknown", 1 + sidecars.UNKNOWN_RESENDS), ("lost", 1 + sidecars.LOST_RESENDS), ("stalled", 1)],
)
def test_a_silent_call_is_sent_again_as_often_as_its_witness_verdict_allows_then_a_retryable_503(api, platform, monkeypatch, verdict, sends):
    script_witness(monkeypatch, [(verdict, 0)] * 10)

    def silent(request, body):
        raise httpx.ReadTimeout("silent for the read bound")

    engine = install_embed_engine(monkeypatch, silent)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert response.headers.get("x-should-retry") != "false"
    assert engine.calls == sends
    rows = usage_events("v1_embeddings")
    assert [(row["status"], row["error_kind"]) for row in rows] == [("error", "model_unavailable")]


def test_a_second_engine_restart_during_one_call_is_a_503_that_tells_the_sdk_not_to_retry(api, monkeypatch):
    witness = script_witness(monkeypatch, [("restarted", 1), ("restarted", 1)])

    def crashes(request, body):
        raise httpx.RemoteProtocolError("server disconnected without sending a response")

    engine = install_embed_engine(monkeypatch, crashes)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 503
    assert response.headers["x-should-retry"] == "false"
    assert engine.calls == 2
    assert witness.witness.asked == ["restarted", "restarted"]


def test_one_engine_restart_during_a_call_is_survived_by_sending_its_inputs_again_one_at_a_time(api, monkeypatch):
    """A restart under a call of several inputs splits it: if one of them
    crashes the engine, the next restart names that one input."""
    script_witness(monkeypatch, [("restarted", 1)])
    crashed = {"once": True}

    def restarts_once(request, body):
        if crashed["once"]:
            crashed["once"] = False
            raise httpx.RemoteProtocolError("server disconnected without sending a response")
        return vllm_embeddings(body)

    engine = install_embed_engine(monkeypatch, restarts_once)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 200
    assert [body["input"] for body in engine.json_bodies()] == [
        ["first passage", "second passage"],
        ["first passage"],
        ["second passage"],
    ]
    assert usage_events("v1_embeddings")[0]["meta"]["resends"] == {"restarted": 1}


def test_a_connection_that_breaks_once_on_an_engine_that_answers_right_after_is_a_lost_resend_not_a_restart(api, monkeypatch):
    """A stale keep-alive connection breaks too. Without the engine refusing
    connections next (or its witness seeing a new process) it is not a
    restart: nothing is split and nothing is quarantined."""
    broke = {"once": True}

    def breaks_once(request, body):
        if broke["once"]:
            broke["once"] = False
            raise httpx.RemoteProtocolError("server disconnected without sending a response")
        return vllm_embeddings(body)

    engine = install_embed_engine(monkeypatch, breaks_once)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 200, response.text
    assert [len(body["input"]) for body in engine.json_bodies()] == [2, 2]
    assert usage_events("v1_embeddings")[0]["meta"]["resends"] == {"lost": 1}
    assert not sidecars._QUARANTINE.active(time.monotonic())


class _CrashingEngine:
    """An embedding engine that dies on any batch holding POISON: the
    connection breaks at once (no /metrics scrape can see it), the engine
    refuses connections for the next `down_sends` sends, then it is back."""

    def __init__(self, *, down_sends: int = 2, delay_s: float = 0.0):
        self.down_sends = down_sends
        self.delay_s = delay_s
        self.down_left = 0
        self.crashes = 0
        self.embedded: list = []

    async def __call__(self, request, body):
        import asyncio

        if self.down_left > 0:
            self.down_left -= 1
            raise httpx.ConnectError("connection refused")
        inputs = json.loads(body)["input"]
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if any("POISON" in text for text in inputs):
            self.crashes += 1
            self.down_left = self.down_sends
            raise httpx.RemoteProtocolError("server disconnected without sending a response")
        self.embedded.extend(inputs)
        return vllm_embeddings(body)


def test_an_input_that_crashes_the_engine_twice_after_the_commit_is_refused_before_the_status_line_of_the_sdk_retry(
    api_port, platform, monkeypatch
):
    """The review's scenario, end to end on a real socket with openai-python:
    the witness never starts (WITNESS_START_S stays 15 s), the commit window
    closes before the SECOND crash, so that refusal can only drop the
    connection. The SDK retries; the retry is refused before its status line
    with `x-should-retry: false`, which the SDK obeys. The engine went down
    twice in all, and the input's neighbours were embedded and are not
    quarantined."""
    import openai

    monkeypatch.setattr(settings, "public_api_sync_commit_s", 0.05, raising=False)
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.05)
    slept = no_backoff(monkeypatch)
    engine = _CrashingEngine(delay_s=0.2)
    install_embed_engine(monkeypatch, engine)
    requests_seen = []

    def on_request(request):
        requests_seen.append(request.url.path)

    client = openai.OpenAI(
        base_url=f"http://127.0.0.1:{api_port}/v1",
        api_key=auth()["Authorization"].split(" ", 1)[1],
        max_retries=2,
        http_client=httpx.Client(event_hooks={"request": [on_request]}),
    )

    with pytest.raises(openai.APIStatusError) as raised:
        client.embeddings.create(model="techsara-embed", input=["fine zero", "POISON one", "fine two"])

    assert raised.value.status_code == 503
    assert raised.value.response.headers["x-should-retry"] == "false"
    assert raised.value.body["param"] == "input.1"
    assert_nothing_internal(raised.value.response)
    assert engine.crashes == 2
    assert len(requests_seen) == 2  # the committed-then-dropped call and ONE retry
    assert engine.embedded == ["fine zero"]
    # One wait, for the engine to come back after the FIRST crash; the second
    # is refused at once instead of being waited out.
    assert slept == [2.0]
    rows = usage_events("v1_embeddings")
    assert [(row["status"], row["error_kind"]) for row in rows] == [("error", "model_unavailable")]

    # The input on its own is refused before any engine call...
    alone = httpx.post(
        f"http://127.0.0.1:{api_port}/v1/embeddings",
        json=_body(input="POISON one"),
        headers=auth(),
    )
    assert alone.status_code == 503 and alone.headers["x-should-retry"] == "false"
    assert alone.content[:1] == b"{"
    assert engine.crashes == 2
    # ...and its neighbours are not.
    neighbour = httpx.post(f"http://127.0.0.1:{api_port}/v1/embeddings", json=_body(input=["fine two"]), headers=auth())
    assert neighbour.status_code == 200, neighbour.text


def test_the_quarantine_of_a_crashing_input_ends_with_its_setting(api, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_POISON_QUARANTINE_S", "30")
    clock = {"now": 5000.0}
    monkeypatch.setattr(sidecars, "_clock", lambda: clock["now"])
    no_backoff(monkeypatch)
    engine = _CrashingEngine(down_sends=1)
    install_embed_engine(monkeypatch, engine)

    first = api.post(URL, json=_body(input="POISON"), headers=auth())
    assert first.status_code == 503 and first.headers["x-should-retry"] == "false"
    assert engine.crashes == 2

    clock["now"] += 29
    assert api.post(URL, json=_body(input="POISON"), headers=auth()).status_code == 503
    assert engine.crashes == 2
    clock["now"] += 2
    assert api.post(URL, json=_body(input="POISON"), headers=auth()).headers.get("x-should-retry") == "false"
    assert engine.crashes == 4  # sent again, crashed twice again, quarantined again


def test_the_real_witness_sees_the_engine_process_restart_under_a_silent_call(api, monkeypatch):
    """Wiring, not the decision table: the real `WitnessSampler` scraping a
    fake /metrics and the real `SidecarWitness` turn a process restart during
    each silent call into `restarted`, twice → x-should-retry false."""
    import asyncio
    import itertools

    from app.publicapi import liveness

    starts = itertools.count(1)
    roots = []

    async def fetch(root):
        roots.append(root)
        return (
            "vllm:num_requests_running 1\n"
            "vllm:num_requests_waiting 0\n"
            "vllm:prompt_tokens_total 10\n"
            f"process_start_time_seconds {next(starts)}\n"
        )

    monkeypatch.setattr(liveness.WitnessSampler, "SCRAPE_S", 0.01)
    sampler = liveness.WitnessSampler(fetch=fetch)
    monkeypatch.setattr(sidecars, "witness_sampler", lambda: sampler)
    monkeypatch.setattr(sidecars, "WITNESS_START_S", 0.0)

    async def silent(request, body):
        await asyncio.sleep(0.1)
        raise httpx.ReadTimeout("silent for the read bound")

    engine = install_embed_engine(monkeypatch, silent)

    response = api.post(URL, json=_body(), headers=auth())

    assert response.status_code == 503
    assert response.headers["x-should-retry"] == "false"
    assert engine.calls == 2
    assert set(roots) == {"http://embed-engine.internal:30003"}


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


def _gate_holder(api, monkeypatch, release):
    """An engine that holds its call until `release` is set, and a thread
    whose request takes the only `embed` slot with it."""
    import asyncio

    monkeypatch.setattr(settings, "public_api_embed_max_concurrent", 1, raising=False)

    async def slow_engine(request, body):
        # Bounded, so a broken gate fails this test instead of hanging it.
        for _ in range(1000):
            if release.is_set() or b"holder" not in body:
                break
            await asyncio.sleep(0.02)
        return vllm_embeddings(body)

    first: dict = {}

    def hold():
        first["response"] = api.post(URL, json=_body(input=["holder"]), headers=auth())

    return slow_engine, threading.Thread(target=hold), first


def _wait_for_in_flight(engine_name="embed", count=1):
    deadline = time.monotonic() + 5
    while capacity.snapshot().get(engine_name, {}).get("in_flight", 0) < count and time.monotonic() < deadline:
        time.sleep(0.02)
    assert capacity.snapshot()[engine_name]["in_flight"] >= count


def test_a_request_behind_a_full_gate_waits_past_the_old_budget_with_keepalive_bytes_and_completes(api, platform, monkeypatch):
    """The capacity rule of the no-timeout design: a queue is never a 503. The
    retired PUBLIC_API_GATE_WAIT_S is set tiny to prove nothing reads it; the
    waiting request commits to 200, writes spaces, then the object."""
    monkeypatch.setenv("PUBLIC_API_GATE_WAIT_S", "0.1")
    monkeypatch.setattr(settings, "public_api_sync_commit_s", 0.2, raising=False)
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.1)
    release = threading.Event()
    slow_engine, holder, first = _gate_holder(api, monkeypatch, release)
    install_embed_engine(monkeypatch, slow_engine)
    holder.start()
    _wait_for_in_flight()
    timer = threading.Timer(1.5, release.set)
    timer.start()

    started = time.monotonic()
    waited = api.post(URL, json=_body(input=["behind the holder"]), headers=auth())
    elapsed = time.monotonic() - started
    holder.join(timeout=10)
    timer.cancel()

    assert waited.status_code == 200, waited.text
    assert elapsed >= 1.2
    assert waited.content[:1] == b" " and waited.content.lstrip()[:1] == b"{"
    assert waited.headers["Cache-Control"] == "no-store, no-transform"
    assert json.loads(waited.content)["data"][0]["object"] == "embedding"
    assert first["response"].status_code == 200
    assert [row["status"] for row in usage_events("v1_embeddings")] == ["ok", "ok"]
    assert daily(platform["project"]["id"])["errors"] == 0


def test_an_over_length_input_behind_a_held_gate_is_a_real_400_within_a_second_without_a_commit(api, monkeypatch):
    """The length check runs before any gate and before the commit clock
    (CONTRACT §8.4): a 5,000-token input is refused at once, with its index,
    however long the queue in front of the engine."""
    monkeypatch.setattr(settings, "public_api_sync_commit_s", 0.05, raising=False)
    release = threading.Event()
    slow_engine, holder, first = _gate_holder(api, monkeypatch, release)

    def tokenize(prompt):
        return 5000 if prompt.startswith("long") else len(prompt.split()) + 1

    engine = install_embed_engine(monkeypatch, slow_engine, tokenize=tokenize)
    holder.start()
    _wait_for_in_flight()
    long_input = "long " + "x" * 5000  # 5,005 bytes: must be counted

    started = time.monotonic()
    refused = api.post(URL, json=_body(input=["short", long_input]), headers=auth())
    elapsed = time.monotonic() - started
    release.set()
    holder.join(timeout=10)

    assert elapsed < 1.0
    assert refused.status_code == 400
    assert refused.content[:1] == b"{"
    error = refused.json()["error"]
    assert (error["code"], error["param"]) == ("context_length_exceeded", "input.1")
    assert engine.tokenized == [long_input]
    assert [json.loads(body)["input"] for body in engine.bodies] == [["holder"]]


def test_a_long_input_the_engine_counts_as_fitting_is_sent_and_weighs_its_real_count(api, monkeypatch):
    """Bytes over the window do not make an input too long: the engine's own
    count decides, and the gate is charged that count, not the byte bound."""
    weights = []
    real_hold = capacity.hold

    def recording_hold(gate, **kwargs):
        weights.append(kwargs["weight_tokens"])
        return real_hold(gate, **kwargs)

    monkeypatch.setattr(capacity, "hold", recording_hold)
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body), tokenize=lambda prompt: 1200)
    text = "word " * 1000  # 5,000 bytes

    response = api.post(URL, json=_body(input=text), headers=auth())

    assert response.status_code == 200
    assert engine.tokenized == [text]
    assert weights == [1200]


def test_an_input_tokenize_could_not_count_is_counted_again_before_any_engine_call(api, monkeypatch):
    """Never skipped: an engine whose /tokenize is unreachable at the check
    is asked again (waiting like any call) before the input is embedded."""
    slept = no_backoff(monkeypatch)
    answers = iter([httpx.Response(503), httpx.Response(503), 900])
    engine = install_embed_engine(
        monkeypatch, lambda request, body: vllm_embeddings(body), tokenize=lambda prompt: next(answers)
    )
    text = "word " * 1000

    response = api.post(URL, json=_body(input=text), headers=auth())

    assert response.status_code == 200, response.text
    assert len(engine.tokenized) == 3
    assert slept == [2.0]
    assert engine.calls == 1


def test_two_thousand_and_forty_eight_inputs_in_a_body_over_the_json_rule_are_one_request_answered_in_order(api, monkeypatch):
    """2,048 passages of about 1 KB each: a 2 MB body, over the 1 MiB JSON
    rule of the other routes and inside the 8 MiB pooling cap (CONTRACT §8.4)."""
    engine = install_embed_engine(monkeypatch, lambda request, body: vllm_embeddings(body, dims=1))
    inputs = [f"passage {i} " + "x" * 1000 for i in range(2048)]
    assert len(json.dumps(_body(input=inputs))) > 2_000_000

    response = api.post(URL, json=_body(input=inputs), headers=auth())

    assert response.status_code == 200, response.text
    assert engine.tokenized == []  # every input's byte bound fits the window
    data = response.json()["data"]
    assert [item["index"] for item in data] == list(range(2048))
    assert engine.calls == 256  # 16 inputs of ~1,012 bytes exceed the 8,192 budget: 8 per call
    too_many = api.post(URL, json=_body(input=inputs + ["one more"]), headers=auth())
    assert too_many.status_code == 400
    assert "at most 2048" in too_many.json()["error"]["message"]


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
