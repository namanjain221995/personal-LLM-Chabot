"""An SDK retry of a request that is still running joins it (CONTRACT-3 §13).

WHY (2026-09-13, no-timeout design revision 2): openai-python retries a
timed-out call with the same body and `x-stainless-retry-count: 1` — and before
this release every such retry started a second, separately billed generation.
Now a retry with count ≥ 1, the same key and a byte-identical body attaches to
the run the first attempt left orphaned. The test gives the SDK a read timeout
shorter than the API's 15 s commit, so the first attempt times out before its
first byte, and proves there was ONE generation: the retry returns, and the
project's output-token usage grew by that one answer, not two.
"""
from __future__ import annotations

import time
import uuid

import httpx
import pytest


LONG_PROMPT = "Write the integers from 1 to 2000 in words, one per line, with no other text."


def _usage_output_tokens(target) -> int:
    headers = {"Authorization": f"Bearer {target.api_key}"}
    response = httpx.get(f"{target.base_url}/usage", headers=headers, timeout=30)
    if response.status_code == 403:
        pytest.skip("the key lacks usage.read (opt-in scope), so one generation cannot be told from two")
    response.raise_for_status()
    body = response.json()
    total = 0
    for row in body.get("data", []):
        total += int(row.get("output_tokens") or 0)
    return total


@pytest.mark.long
@pytest.mark.feature("no_timeouts")
def test_an_sdk_retry_after_a_read_timeout_attaches_to_the_running_generation(make_client, target, attempt_log, note):
    before = _usage_output_tokens(target)
    # Shorter than PUBLIC_API_SYNC_COMMIT_S (15 s): the first attempt times out
    # before the API writes its first byte; the SDK retries with count 1.
    client = make_client(timeout=httpx.Timeout(10.0, connect=5.0), max_retries=4, log=attempt_log)
    started = time.monotonic()
    tokens = max(1500, target.long_output_tokens)
    response = client.responses.create(
        model=target.models["chat"], input=LONG_PROMPT, max_output_tokens=tokens, temperature=0
    )
    elapsed = time.monotonic() - started
    retries = [r for r in attempt_log.requests if r["headers"].get("x-stainless-retry-count") not in (None, "0")]
    assert response.status == "completed", response.status
    if not retries:
        pytest.skip(f"the call finished in {elapsed:.1f} s without an SDK retry; raise TECHSARA_LONG_OUTPUT_TOKENS")

    produced = response.usage.output_tokens
    deadline = time.monotonic() + 60
    grown = 0
    while time.monotonic() < deadline:
        grown = _usage_output_tokens(target) - before
        if grown >= produced:
            break
        time.sleep(2)
    assert grown < 2 * produced, (
        f"usage grew by {grown} output tokens for one {produced}-token answer after {len(retries)} SDK retr(ies): "
        "the retry started a second generation instead of attaching"
    )
    note(f"{len(attempt_log.requests)} SDK attempts, {len(retries)} marked retries, one generation of {produced} tokens in {elapsed:.0f} s")


def test_a_retry_count_of_zero_with_the_same_body_is_a_new_request(make_client, target):
    """No feature mark: true on every stack, and the release must keep it true —
    implicit attach needs the SDK's retry mark."""
    client = make_client()
    body = dict(model=target.models["chat"], input=f"Reply with the word ok. {uuid.uuid4()}", max_output_tokens=target.small_output_tokens)
    first = client.responses.create(**body)
    second = client.responses.create(**body, extra_headers={"x-stainless-retry-count": "0"})
    assert first.id != second.id, "without a retry mark an identical body is a new request"


def test_a_retry_mark_with_a_different_body_launches_fresh(make_client, target):
    """No feature mark: an attach needs a byte-identical body on every stack."""
    client = make_client()
    first = client.responses.create(model=target.models["chat"], input=f"Reply ok. {uuid.uuid4()}", max_output_tokens=target.small_output_tokens)
    second = client.responses.create(
        model=target.models["chat"], input=f"Reply ok. {uuid.uuid4()}", max_output_tokens=target.small_output_tokens,
        extra_headers={"x-stainless-retry-count": "1"},
    )
    assert first.id != second.id
