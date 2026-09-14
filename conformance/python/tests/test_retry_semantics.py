"""What the official SDK's automatic retries do against this server.

openai-python retries 408, 409, 429 and every 5xx twice by default, obeys
`x-should-retry: true|false`, waits `Retry-After` when it is at most 120 s
and does NOT retry at all when it is longer (openai/_base_client.py,
`_should_retry` / `_calculate_retry_timeout`, SDK 3.13.0). So the server's
status codes and headers ARE its retry policy for every SDK user; these tests
watch the wire (an httpx event hook on the SDK's own transport) to see it.
"""
from __future__ import annotations

import threading
import time
import uuid

import httpx
import openai
import pytest

from techsara_conformance import asserts

SDK_MAX_RETRY_AFTER_S = 120
COUNT = "Count from 1 to 300, separated by spaces."


def _retry_after(headers) -> float:
    value = headers.get("retry-after")
    assert value is not None, f"no Retry-After header: {sorted(headers)}"
    seconds = float(value)
    assert 1 <= seconds <= SDK_MAX_RETRY_AFTER_S, (
        f"Retry-After {value!r}: the SDK only honours 1..{SDK_MAX_RETRY_AFTER_S} s and gives up above it"
    )
    return seconds


def test_the_sdk_does_not_retry_a_401(make_client, attempt_log):
    client = make_client(api_key=asserts.FAKE_KEY, max_retries=2, log=attempt_log)
    with pytest.raises(openai.AuthenticationError):
        client.models.list()
    assert len(attempt_log.requests) == 1, f"{len(attempt_log.requests)} attempts for a 401"
    assert attempt_log.responses[0]["headers"].get("x-should-retry") != "true"


def test_a_conflicting_idempotency_key_stays_a_409_on_every_sdk_retry_and_generates_nothing_twice(make_client, attempt_log, target, note):
    key = f"conformance-{uuid.uuid4()}"
    plain = make_client()
    original = plain.responses.create(
        model=target.models["chat"], input="Reply with the single word: pong", max_output_tokens=target.small_output_tokens,
        extra_headers={"Idempotency-Key": key},
    )
    retrying = make_client(max_retries=2, log=attempt_log)
    with pytest.raises(openai.ConflictError) as caught:
        retrying.responses.create(
            model=target.models["chat"], input="A different body.", max_output_tokens=target.small_output_tokens,
            extra_headers={"Idempotency-Key": key},
        )
    asserts.sdk_error(caught.value, code="idempotency_conflict")
    statuses = [r["status"] for r in attempt_log.responses]
    assert set(statuses) == {409}, statuses
    should_retry = attempt_log.responses[0]["headers"].get("x-should-retry")
    note(
        f"SDK attempts for a permanent 409: {len(statuses)} "
        f"(x-should-retry={should_retry!r}; 'false' would save {len(statuses) - 1} round trip(s))"
    )
    assert plain.responses.retrieve(original.id).status == "completed", "the original is untouched"


@pytest.mark.feature("idempotency_in_flight_409")
def test_a_retry_while_the_first_request_runs_is_409_with_retry_after_and_the_sdk_collects_the_original(make_client, attempt_log, target, note):
    key = f"conformance-{uuid.uuid4()}"
    body = dict(model=target.models["chat"], input=COUNT, max_output_tokens=200, temperature=0)
    first_result: dict = {}

    def run_first() -> None:
        try:
            first_result["response"] = make_client().responses.create(**body, extra_headers={"Idempotency-Key": key})
        except Exception as exc:  # noqa: BLE001 — reported by the main thread
            first_result["error"] = exc

    worker = threading.Thread(target=run_first, daemon=True)
    worker.start()
    time.sleep(0.4)  # the claim is written before generation starts
    retrying = make_client(max_retries=5, log=attempt_log)
    second = retrying.responses.create(**body, extra_headers={"Idempotency-Key": key})
    worker.join(timeout=target.request_timeout_s)
    assert "error" not in first_result, f"the first request failed: {first_result.get('error')!r}"

    first_attempt = attempt_log.responses[0]
    first_error = (first_attempt["body"] or {}).get("error", {}) or {}
    # The message and param are printed too (2026-09-13, review): on a stack
    # that enforces usage limits, a genuine rate-limit 429 carries the same
    # code as the pre-2026-09-13 "still running" 429, and only the message
    # ("A request with this Idempotency-Key is still running.") and
    # param Idempotency-Key tell them apart. KNOWN RACE: the fixed 0.4 s head
    # start assumes the worker's request claims the key first; with remote
    # latency the main thread can claim first and see 200 (a false FAIL).
    assert first_attempt["status"] == 409, (
        f"first SDK attempt while the original ran: HTTP {first_attempt['status']} {first_error.get('code')} "
        f"message={first_error.get('message')!r} param={first_error.get('param')!r} (§13 says 409 idempotency_conflict)"
    )
    assert (first_attempt["body"] or {}).get("error", {}).get("code") == "idempotency_conflict"
    waited = _retry_after(first_attempt["headers"])
    gap = attempt_log.requests[1]["t"] - attempt_log.requests[0]["t"]
    assert gap >= waited * 0.9, f"the SDK retried after {gap:.2f} s, before Retry-After {waited} s"
    assert attempt_log.responses[-1]["status"] == 200
    assert second.id == first_result["response"].id, "the retry collected the ORIGINAL response, not a second generation"
    note(f"in-flight retry: {len(attempt_log.requests)} SDK attempts, Retry-After {waited:g} s")


@pytest.mark.capacity_probe
@pytest.mark.feature("capacity_gates")
def test_a_full_capacity_gate_is_503_model_unavailable_with_retry_after_before_the_status_line(make_client, target, note):
    """Fills `main.long` (one public request whose input + planned output is
    over 131,072 tokens) with one stream, then sends a second. §12.3: the
    second waits PUBLIC_API_GATE_WAIT_S (30 s) BEFORE the status line and is
    refused with a real HTTP 503 + Retry-After. Costs ~35 s of decode on the
    main engine, which is why it only runs with --capacity-probe."""
    body = {
        "model": target.models["chat"],
        "input": "Count upward from 1, one number per line, and never stop.",
        "max_output_tokens": 200_000,
        "stream": True,
    }
    holding = threading.Event()
    release = threading.Event()
    problems: list = []

    def hold_the_gate() -> None:
        try:
            with httpx.Client(timeout=None) as c:
                with c.stream(
                    "POST", f"{target.base_url}/responses", json=body,
                    headers={"Authorization": f"Bearer {target.api_key}"},
                ) as response:
                    if response.status_code != 200:
                        problems.append(f"holder got HTTP {response.status_code}: {response.read()[:200]!r}")
                        return
                    for line in response.iter_lines():
                        if "response.output_text.delta" in line:
                            holding.set()
                        if release.is_set():
                            return
        except Exception as exc:  # noqa: BLE001
            problems.append(repr(exc))
        finally:
            holding.set()

    holder = threading.Thread(target=hold_the_gate, daemon=True)
    holder.start()
    try:
        assert holding.wait(120), "the holder never started generating"
        assert not problems, problems
        started = time.monotonic()
        with pytest.raises(openai.InternalServerError) as caught:
            make_client(max_retries=0).responses.create(**body)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(timeout=60)
    exc = caught.value
    assert exc.status_code == 503
    error = asserts.sdk_error(exc, code="model_unavailable")
    assert "capacity" in error["message"].lower(), error["message"]
    _retry_after(exc.response.headers)
    note(f"503 after {elapsed:.1f} s gate wait, Retry-After {exc.response.headers.get('retry-after')}")
