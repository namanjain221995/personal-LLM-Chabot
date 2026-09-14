"""Resuming a Responses stream by id and sequence number (CONTRACT-3 §10.3).

WHY (2026-09-13, no-timeout design revision 2): a tunnel drop cuts every
in-flight connection, and no timer can prevent it. What makes a dropped stream
survivable is `GET /v1/responses/{id}?stream=true&starting_after=N`: the
events after N are replayed from the write-ahead log, then the generation is
followed live. These tests drop a stream on purpose and prove the resumed
events continue it exactly — contiguous sequence numbers, no duplicated or
missing text, one terminal event — through the openai-python calls the
documentation teaches.
"""
from __future__ import annotations

import openai
import pytest

from techsara_conformance import sse

RESUME = pytest.mark.feature("resumable_streams")

PROMPT = "Write the integers from 1 to 120 in words, one per line, with no other text."
TERMINALS = {"response.completed", "response.failed"}


def _start(client, target, *, tokens: int = 400, **extra):
    return client.responses.create(
        model=target.models["chat"], input=PROMPT, max_output_tokens=tokens, temperature=0, stream=True, **extra
    )


@RESUME
def test_a_stream_dropped_mid_answer_resumes_with_contiguous_events_and_the_same_text(make_client, target, note):
    client = make_client(timeout=None)
    stream = _start(client, target)
    seen = []
    with stream:
        for event in stream:
            seen.append(event)
            if sum(e.type == "response.output_text.delta" for e in seen) >= 5:
                break  # the connection closes here, mid-answer
    response_id = next(e.response.id for e in seen if e.type == "response.created")
    last = seen[-1].sequence_number

    resumed = list(client.responses.retrieve(response_id, stream=True, starting_after=last))
    assert resumed, "the resume stream carried no events"
    numbers = [e.sequence_number for e in seen] + [e.sequence_number for e in resumed]
    assert numbers == list(range(1, len(numbers) + 1)), f"not contiguous across the drop: {numbers[: last + 3]}…"
    assert resumed[-1].type in TERMINALS, resumed[-1].type
    assert sum(e.type in TERMINALS for e in resumed) == 1

    text = "".join(e.delta for e in [*seen, *resumed] if e.type == "response.output_text.delta")
    done = next(e for e in resumed if e.type == "response.output_text.done")
    assert text == done.text, "resumed deltas do not continue the text exactly"
    note(f"dropped after sequence {last}; {len(resumed)} events replayed and followed")


@RESUME
def test_a_finished_response_replays_after_n_and_closes_at_once(make_client, target):
    client = make_client(timeout=None)
    events = list(_start(client, target, tokens=target.small_output_tokens))
    response_id = events[0].response.id
    replay = list(client.responses.retrieve(response_id, stream=True, starting_after=2))
    assert [e.sequence_number for e in replay] == [e.sequence_number for e in events[2:]]
    assert replay[-1].type == "response.completed"
    full = list(client.responses.retrieve(response_id, stream=True))
    assert [e.type for e in full] == [e.type for e in events], "a replay without starting_after is the whole stream"


@RESUME
def test_starting_after_without_stream_is_a_400(client, target, raw):
    events = list(_start(client, target, tokens=target.small_output_tokens))
    response = raw.get(f"responses/{events[0].response.id}", params={"starting_after": 1})
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "invalid_request_error"


@RESUME
def test_a_store_false_response_cannot_be_replayed(make_client, target, raw):
    client = make_client(timeout=None)
    events = list(_start(client, target, tokens=target.small_output_tokens, store=False))
    response = raw.get(f"responses/{events[0].response.id}", params={"stream": "true", "starting_after": 0})
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["code"] == "invalid_request_error" and error["param"] == "stream", error


def test_a_resume_of_an_unknown_response_is_a_404_before_any_400_on_every_stack(raw):
    """No feature mark: a lookup that finds nothing is a 404 before and after
    the release — the release must not turn it into a 400 (§10.3 order)."""
    response = raw.get("responses/resp_000000000000000000000000", params={"starting_after": 1})
    assert response.status_code == 404, f"§10.3 checks the lookup before streamability: {response.status_code} {response.text}"


@RESUME
def test_a_key_that_did_not_create_the_response_gets_404(make_client, target):
    other = target.extra.get("second_api_key") or None
    import os

    other = other or os.environ.get("TECHSARA_SECOND_API_KEY")
    if not other:
        pytest.skip("needs TECHSARA_SECOND_API_KEY: a second key of the same project, not of the same service account")
    events = list(_start(make_client(timeout=None), target, tokens=target.small_output_tokens))
    with pytest.raises(openai.NotFoundError):
        list(make_client(api_key=other, timeout=None).responses.retrieve(events[0].response.id, stream=True))


@RESUME
def test_neither_the_stream_nor_its_replay_carries_id_or_retry_lines(raw, target):
    body = {"model": target.models["chat"], "input": PROMPT, "max_output_tokens": target.small_output_tokens, "stream": True}
    lines = []
    with raw.stream("POST", "responses", json=body) as response:
        assert response.status_code == 200
        lines.extend(response.iter_lines())
    transcript = sse.read(iter(lines))
    response_id = transcript.events[0].json()["response"]["id"]
    with raw.stream("GET", f"responses/{response_id}", params={"stream": "true", "starting_after": 0}) as replay:
        assert replay.status_code == 200
        assert replay.headers["content-type"].startswith("text/event-stream"), "the replay is a stream (§10.3)"
        lines.extend(replay.iter_lines())
    offenders = [line for line in lines if line.startswith(("id:", "retry:")) or line.startswith(": ts-seq")]
    assert not offenders, f"§10.2: never id:, retry: or internal ts-seq lines — {offenders[:3]}"


@RESUME
def test_the_sdk_stream_helper_resumes_from_a_response_id(make_client, target):
    client = make_client(timeout=None)
    events = list(_start(client, target, tokens=target.small_output_tokens))
    response_id = events[0].response.id
    with client.responses.stream(response_id=response_id, starting_after=3) as stream:
        helper_events = list(stream)
    assert helper_events and helper_events[0].sequence_number == 4
    assert stream.get_final_response().id == response_id
