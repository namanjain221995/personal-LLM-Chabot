"""Background responses: create, poll, cancel (CONTRACT-3 §14)."""
from __future__ import annotations

import time

TERMINAL = {"completed", "failed", "cancelled"}


def _poll(client, response_id: str, timeout_s: float, interval_s: float = 2.0):
    deadline = time.monotonic() + timeout_s
    seen = []
    while True:
        response = client.responses.retrieve(response_id)
        if not seen or seen[-1] != response.status:
            seen.append(response.status)
        if response.status in TERMINAL:
            return response, seen
        assert time.monotonic() < deadline, f"{response_id} still {response.status} after {timeout_s:.0f} s (statuses {seen})"
        time.sleep(interval_s)


def test_a_background_response_is_accepted_with_202_then_completes_when_polled(client, target):
    raw = client.responses.with_raw_response.create(
        model=target.models["chat"],
        input="Reply with the single word: pong",
        max_output_tokens=target.small_output_tokens,
        background=True,
    )
    assert raw.http_response.status_code == 202, f"background create answers 202 (§14), got {raw.http_response.status_code}"
    accepted = raw.parse()
    assert accepted.id.startswith("resp_")
    assert accepted.status in ("queued", "in_progress"), accepted.status
    assert accepted.usage is None, "no usage before the work has run"
    finished, statuses = _poll(client, accepted.id, target.background_poll_timeout_s)
    assert finished.status == "completed", f"statuses {statuses}, error {finished.error!r}"
    assert finished.output_text.strip(), "a background response keeps its output for retrieval"
    assert finished.usage is not None and finished.usage.output_tokens > 0


def test_cancelling_a_running_background_response_ends_it_cancelled_and_repeating_the_cancel_is_harmless(client, target):
    accepted = client.responses.create(
        model=target.models["chat"],
        input="Count from 1 to 500, one number per line.",
        max_output_tokens=256,
        background=True,
    )
    first = client.responses.cancel(accepted.id)
    assert first.id == accepted.id
    finished, statuses = _poll(client, accepted.id, target.background_poll_timeout_s, interval_s=1.0)
    assert finished.status == "cancelled", (
        f"cancel sent straight after the 202 should win against a 256-token job; statuses {statuses}"
    )
    again = client.responses.cancel(accepted.id)
    assert again.status == "cancelled", "a repeated cancel returns the same answer, not an error (§14)"


def test_cancelling_a_finished_response_does_not_walk_it_back(client, target):
    created = client.responses.create(
        model=target.models["chat"], input="Reply with the single word: pong", max_output_tokens=target.small_output_tokens
    )
    after = client.responses.cancel(created.id)
    assert after.status == "completed", f"a completed response can never become cancelled, got {after.status}"
