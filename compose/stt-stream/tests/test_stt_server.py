"""The engine end to end through /v1/stream, with fake recognizers.

Audio is sent faster than real time here, as a reconnect's replay would be;
the rules that depend on time (forced finals, housekeeping renewals) are
driven by stream position, not wall time, so they behave the same.
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time

import numpy as np
import pytest
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from conftest import (AUTH, TOKEN, drain, finals, make_profile, make_settings, metrics_value, recv_json, running,
                      send_pcm, server, start, wait_ready, words_of)
from stt_fakes import TOKEN as TOKEN_SAMPLES
from stt_fakes import VALUE, WORDS, FakeRecognizer, fake_factory, positions, speech

LEAD = 2560  # the default 160 ms of silence in front of every stream


def connect(client, headers=AUTH, **kwargs):
    return client.websocket_connect("/v1/stream", headers=headers, **kwargs)


def start_message(**fields) -> str:
    message = {"type": "start", "sample_rate": 16000, "encoding": "pcm_s16le", "first_sample": 0,
               "first_u": 0, "mode": "dictation"}
    message.update(fields)
    return json.dumps(message)


def session(client, parts, *, flush=True, **start_fields):
    """Start, send the script's audio, flush, and return (ready, events, close code)."""
    with connect(client) as ws:
        ready = start(ws, **start_fields)
        send_pcm(ws, speech(parts))
        if flush:
            ws.send_text('{"type":"flush"}')
        events, code = drain(ws)
    return ready, events, code


def read_until(ws, predicate, timeout=10.0):
    seen = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = recv_json(ws, timeout)
        seen.append(event)
        if predicate(event):
            return seen
    raise AssertionError(f"never saw the event; got {seen}")


def client_streams(made):
    """Every stream the engine opened for clients (the warm-up stream has no
    language set), in creation order."""
    return [s for r in made for s in r.streams if s.options]


# -- the service surface -------------------------------------------------------

def test_health_names_profiles_models_and_capacity_but_never_a_path():
    settings = make_settings(profiles=(make_profile("fast", 160, 3), make_profile("wide", 560, 5, ("auto", "hi"))))
    with running(settings) as client:
        body = client.get("/health").json()
    assert body["ready"] is True and body["streams"] == 0 and body["capacity"] == 8
    assert body["model"] == "model-fast,model-wide" and body["error"] is None
    assert [(p["id"], p["chunk_ms"], p["max_streams"], p["active"], p["languages"]) for p in body["profiles"]] == [
        ("fast", 160, 3, 0, ["auto", "en", "hi"]), ("wide", 560, 5, 0, ["auto", "hi"])]
    assert "/models" not in json.dumps(body)


def test_the_upgrade_is_refused_before_accept_without_the_right_token():
    # A close BEFORE accept, which uvicorn answers with a bare 403. Not a
    # denial response: under websockets-sansio every one of those logged an
    # ERROR ("ASGI callable returned without completing handshake").
    with running() as client:
        for headers in ({}, {"authorization": "Bearer wrong-key-1-wrong-key-1"}, {"authorization": TOKEN}):
            with pytest.raises(WebSocketDisconnect) as denied:
                with connect(client, headers=headers):
                    pass
            assert not isinstance(denied.value, WebSocketDenialResponse)
            assert denied.value.code == 1008
        assert metrics_value(client, 'stt_stream_rejections_total{reason="unauthorized"}') == 3
        with connect(client, headers={"authorization": f"bearer {TOKEN}"}) as ws:
            assert start(ws)["type"] == "ready"


def test_an_engine_without_a_token_admits_anyone_which_is_for_tests_only():
    with running(make_settings(token="")) as client, connect(client, headers={}) as ws:
        assert start(ws)["profile"] == "fast"


def test_the_gateways_subprotocol_is_echoed():
    with running() as client, connect(client, subprotocols=["techsara.voice.v1"]) as ws:
        assert ws.accepted_subprotocol == "techsara.voice.v1"
        start(ws)


def test_ready_describes_the_stream_in_both_spellings_of_the_resume_point():
    with running() as client, connect(client) as ws:
        ready = start(ws, first_sample=320000, first_u=9)
    assert ready == {"type": "ready", "v": 1, "sample_rate": 16000, "frame_ms": 40, "max_frame_bytes": 16384,
                     "resume_from_sample": 320000, "next_u": 9, "first_sample": 320000, "first_u": 9,
                     "profile": "fast", "chunk_ms": 160, "language": "auto", "mode": "dictation"}


# -- refusals --------------------------------------------------------------------

@pytest.mark.parametrize("first", [
    b"\x00\x00",
    "not json",
    '{"type":"flush"}',
    start_message(sample_rate=8000),
    start_message(encoding="opus"),
    start_message(channels=2),
    start_message(first_sample=-1),
    start_message(first_u="3"),
    start_message(first_sample=True),
    start_message(mode="lecture"),
    start_message(mode=None),
    start_message(v=2),
    start_message(language="english"),
    start_message(frame_ms="40"),
    start_message(frame_ms=True),
    start_message(frame_ms=float("nan")),       # json.dumps writes NaN, which Python's json reads back
    start_message(frame_ms=float("inf")),
    start_message(frame_ms=10 ** 400),          # an int no float can hold: OverflowError, not a crash
    "[" * 5000 + "]" * 5000,                    # RecursionError in json, not ValueError
])
def test_a_bad_start_is_refused_with_4400(first):
    with running() as client:
        with connect(client) as ws:
            ws.send_bytes(first) if isinstance(first, bytes) else ws.send_text(first)
            events, code = drain(ws)
        assert metrics_value(client, 'stt_stream_rejections_total{reason="protocol"}') == 1
    assert code == 4400
    assert len(events) == 1 and events[0]["type"] == "error"
    assert (events[0]["code"], events[0]["retryable"]) == ("protocol", False)


def test_no_start_in_time_is_refused():
    with running(make_settings(start_timeout_s=0.2)) as client, connect(client) as ws:
        events, code = drain(ws)
    assert code == 4400 and events[0]["code"] == "protocol"


def test_a_loading_engine_refuses_with_4503_worth_a_retry():
    gate = threading.Event()

    def slow(profile, settings):
        gate.wait(10)
        return FakeRecognizer(chunk_ms=profile.chunk_ms)

    with running(factory=slow, ready=False) as client:
        assert client.get("/health").json()["ready"] is False
        with connect(client) as ws:
            events, code = drain(ws)
        gate.set()
        wait_ready(client)
        assert metrics_value(client, 'stt_stream_rejections_total{reason="not_ready"}') == 1
    assert code == 4503
    assert (events[0]["code"], events[0]["retryable"]) == ("engine_unavailable", True)


def test_a_profile_that_cannot_load_says_so_without_its_path():
    def broken(profile, settings):
        raise OSError("/models/fast/encoder.int8.onnx: no such file")

    with running(factory=broken, ready=False) as client:
        deadline = time.monotonic() + 5
        while client.get("/health").json()["error"] is None and time.monotonic() < deadline:
            time.sleep(0.01)
        body = client.get("/health").json()
        assert metrics_value(client, "stt_stream_ready") == 0
    assert body["ready"] is False
    assert "fast" in body["error"] and "OSError" in body["error"] and "/models" not in body["error"]


def test_admission_takes_the_first_profile_with_room_for_the_language():
    # The deployed shape (build spec 8E): English to its own model first, the
    # multilingual one for auto and Hindi, each with a wide-chunk overflow.
    settings = make_settings(profiles=(make_profile("en-fast", 160, 1, ("en",)),
                                       make_profile("multi-fast", 160, 1, ("auto", "hi", "en")),
                                       make_profile("multi-wide", 560, 1, ("auto", "hi", "en"))))
    with running(settings) as client:
        with connect(client) as a, connect(client) as b, connect(client) as c:
            assert start(a, language="en")["profile"] == "en-fast"
            assert start(b, language="en")["profile"] == "multi-fast"
            assert start(c, language="hi")["profile"] == "multi-wide"
            for language in ("auto", "hi", "en"):
                with connect(client) as d:
                    d.send_text(start_message(language=language))
                    events, code = drain(d)
                assert code == 4429 and (events[0]["code"], events[0]["retryable"]) == ("capacity", True)
            with connect(client) as e:
                e.send_text(start_message(language="gu"))
                events, code = drain(e)
            assert code == 4400 and (events[0]["code"], events[0]["retryable"]) == ("unsupported_language", False)
            assert client.get("/health").json()["streams"] == 3
            assert metrics_value(client, 'stt_stream_active_streams{profile="multi-wide"}') == 1
        assert client.get("/health").json()["streams"] == 0
        with connect(client) as f:
            assert start(f)["profile"] == "multi-fast"  # "auto" skips the English-only profile
        assert metrics_value(client, 'stt_stream_rejections_total{reason="capacity"}') == 3
        assert metrics_value(client, 'stt_stream_rejections_total{reason="language"}') == 1


def test_the_requested_language_is_set_on_the_stream_in_the_allow_lists_spelling():
    made = []
    with running(factory=fake_factory(made=made)) as client, connect(client) as ws:
        assert start(ws, language="HI")["language"] == "hi"
        ws.send_text('{"type":"flush"}')
        drain(ws)
    assert [s.options.get("language") for s in client_streams(made)] == ["hi"]


def test_one_recognizer_per_profile_is_shared_by_its_decode_threads():
    made = []
    settings = make_settings(profiles=(make_profile("fast", 160, 8), make_profile("wide", 560, 8)), workers=3)
    with running(settings, fake_factory(made=made)) as client:
        assert len(made) == 2  # one per profile, not one per worker
        sockets = [connect(client) for _ in range(3)]
        for ws in sockets:
            ws.__enter__()
            start(ws)
            send_pcm(ws, speech(["alpha", 0.8]))
        for ws in sockets:
            ws.send_text('{"type":"flush"}')
            _, code = drain(ws)
            assert code == 1000
            ws.__exit__(None, None, None)
    streams = client_streams(made)
    # Three streams, plus the fresh one each renewal (after each final) puts
    # on the same slot and so the same worker.
    assert len(streams) >= 3 and all(s in made[0].streams for s in streams)
    threads = [s.decoded_by for s in streams]
    assert all(len(t) == 1 for t in threads)  # a stream is only ever decoded by its own worker
    assert len(set().union(*threads)) == 3    # and three streams went to three workers


def test_every_stream_starts_with_the_lead_silence_and_positions_do_not_count_it():
    made = []
    parts = ["alpha", "bravo", 0.8]  # speech from the very first sample
    with running(factory=fake_factory(made=made)) as client:
        _, events, code = session(client, parts, first_sample=16000)
    assert code == 1000
    stream = client_streams(made)[0]
    assert not stream.audio[:LEAD].any() and stream.audio[LEAD] != 0
    (final,) = finals(events)
    assert final["text"] == "alpha bravo" and final["start_sample"] == 16000
    # It ends in the silence after "bravo" (2 tokens), not at bravo's time.
    assert 16000 + 2 * TOKEN_SAMPLES <= final["end_sample"] <= 16000 + 2 * TOKEN_SAMPLES + 12800


def test_the_lead_silence_is_configurable_down_to_none():
    made = []
    with running(make_settings(lead_pad_ms=0), fake_factory(made=made)) as client:
        _, events, _ = session(client, [0.32, "alpha", 0.8])
    assert client_streams(made)[0].audio[:5120].tolist() == [0] * 5120
    assert finals(events)[0]["start_sample"] == 5120


# -- segmentation, end to end ------------------------------------------------------

def test_partials_replace_and_finals_commit_in_order():
    with running() as client, connect(client) as ws:
        start(ws)
        send_pcm(ws, speech(["alpha", "bravo", "charlie", "delta"]))
        seen = read_until(ws, lambda e: e["type"] == "partial" and e["text"] == "alpha bravo charlie")
        send_pcm(ws, speech([0.8, "echo", "foxtrot", 1.0]))
        ws.send_text('{"type":"flush"}')
        events, code = drain(ws)
    events = seen + events
    assert code == 1000 and events[-1] == {"type": "done"}
    assert [(f["u"], f["text"]) for f in finals(events)] == [(0, "alpha bravo charlie delta"), (1, "echo foxtrot")]
    partials = [e for e in events if e["type"] == "partial" and e["u"] == 0]
    assert partials and all("alpha bravo charlie delta".startswith(p["text"]) for p in partials)
    final_index = events.index(finals(events)[0])
    assert not [e for e in events[final_index + 1:] if e.get("u") == 0]
    speech_edges = [e["active"] for e in events if e["type"] == "speech"]
    assert speech_edges[:2] == [True, False]
    for event in events:
        if event["type"] in ("partial", "final"):
            assert isinstance(event["compute_ms"], int) and event["compute_ms"] >= 0
            assert 0 <= event["start_sample"] <= event["end_sample"]
        if event["type"] == "final":
            assert event["endpoint_ms"] == 600


@pytest.mark.parametrize("with_timestamps", [True, False])
def test_no_word_is_lost_across_many_utterances(with_timestamps):
    # Pauses of at least 0.8 s: an endpoint needs 0.6 s of silence as the
    # decoder's 160 ms steps see it, which a pause of 0.6 s + one step always
    # holds whatever the alignment (and every renewal shifts the alignment).
    rng = random.Random(7)
    parts, spoken, utterances = [0.32], [], []
    for _ in range(25):
        words = [rng.choice(WORDS[:-1]) for _ in range(rng.randint(1, 6))]
        parts += words + [rng.choice([0.8, 0.96, 1.12, 1.6, 2.72])]
        spoken += words
        utterances.append(words)
    with running(factory=fake_factory(with_timestamps=with_timestamps)) as client:
        _, events, code = session(client, parts, first_u=5, first_sample=320000)
    assert code == 1000
    fs = finals(events)
    assert [f["u"] for f in fs] == list(range(5, 5 + len(utterances)))
    assert [f["text"].split() for f in fs] == utterances
    assert words_of(events) == spoken
    starts = [f["start_sample"] for f in fs]
    assert starts == sorted(starts) and all(s >= 320000 for s in starts)
    ends = [f["end_sample"] for f in fs]
    assert all(e <= s for e, s in zip(ends, starts[1:]))  # utterances never overlap
    at = [p for _, p in positions(parts)] + [len(speech(parts))]
    index = 0
    for f, words in zip(fs, utterances):
        if with_timestamps:
            # Exact: the token times ARE the positions the client sent them at.
            assert f["start_sample"] == 320000 + at[index]
        # Every final ends in the silence after its last word, before the
        # next word: the point a reconnect resumes from.
        last = at[index + len(words) - 1]
        assert 320000 + last + TOKEN_SAMPLES <= f["end_sample"] <= 320000 + at[index + len(words)]
        index += len(words)


def test_flush_pads_silence_so_the_last_word_survives():
    # No silence after the last word: without the padding the fake, like the
    # real model, never decodes it (its right context never arrives).
    with running() as client:
        _, events, code = session(client, ["alpha", "bravo", "charlie"])
    assert code == 1000
    assert [f["text"] for f in finals(events)] == ["alpha bravo charlie"]
    assert events[-1] == {"type": "done"}


def test_without_the_padding_the_last_word_would_be_lost():
    """The control for the test above: the same script with padding switched off."""
    settings = make_settings(profiles=(make_profile("fast", 160, 4, flush_pad_ms=0),))
    with running(settings) as client:
        _, events, _ = session(client, ["alpha", "bravo", "charlie"])
    assert [f["text"] for f in finals(events)] == ["alpha bravo"]


def test_a_flush_with_nothing_said_is_just_done():
    with running() as client:
        _, events, code = session(client, [0.64])
    assert code == 1000 and events == [{"type": "done"}]


def test_long_speech_is_forced_out_without_losing_or_repeating_a_word():
    words = [WORDS[i % 26] for i in range(40)]  # 6.4 s with no pause
    with running(make_settings(max_utterance_s=2.0)) as client:
        _, events, code = session(client, words + [1.0])
        assert metrics_value(client, 'stt_stream_events_total{kind="final_forced"}') >= 2
    assert code == 1000
    fs = finals(events)
    assert words_of(events) == words
    forced = [f for f in fs if f["endpoint_ms"] == 0]
    assert len(forced) >= 2 and fs[-1]["endpoint_ms"] == 600
    for f in forced:
        assert (f["end_sample"] - f["start_sample"]) / 16000 <= 2.0 + 0.2


def wait_for(client, series, at_least, timeout=5.0):
    deadline = time.monotonic() + timeout
    while metrics_value(client, series) < at_least - 1e-6:  # a sum of float seconds may land a hair under
        assert time.monotonic() < deadline, f"{series} never reached {at_least}"
        time.sleep(0.01)


def wait_all_audio_in(client, *scripts, profile="fast"):
    """Until the engine has queued every sample of these scripts: each frame
    is counted right after it goes into its stream's inbox."""
    wait_for(client, f'stt_stream_audio_seconds_total{{profile="{profile}"}}',
             sum(len(speech(parts)) for parts in scripts) / 16000)


def test_the_backstop_renewals_happen_only_inside_long_silences_and_lose_nothing():
    # Safe-point renewals off: only the text backstop (renew_chars) renews,
    # after the backstop's 1.5 s hold.
    made = []
    renewals = 'stt_stream_renewals_total{profile="fast"}'
    parts = [0.32]
    settings = make_settings(renew_chars=40, renew_silence_s=0)
    with running(settings, fake_factory(made=made)) as client, connect(client) as ws:
        start(ws, first_sample=16000)
        send_pcm(ws, speech([0.32]))
        for i in range(12):
            # Short pauses (an endpoint, but shorter than the renewal hold)
            # between most utterances, a long one every fourth. After a long
            # one the decoder catches up inside the silence and renews there.
            long_pause = i % 4 == 3
            segment = [WORDS[(3 * i + k) % 26] for k in range(3)] + [2.72 if long_pause else 0.8]
            parts += segment
            send_pcm(ws, speech(segment))
            if long_pause:
                wait_for(client, renewals, (i + 1) // 4)
        segment = ["alpha", "bravo"]  # speech right up to the flush, on a renewed stream
        parts += segment
        send_pcm(ws, speech(segment))
        ws.send_text('{"type":"flush"}')
        events, code = drain(ws)
        assert metrics_value(client, renewals) == 3
    assert code == 1000
    spoken = [p for p in parts if isinstance(p, str)]
    assert words_of(events) == spoken
    streams = client_streams(made)
    assert len(streams) == 4 and made[0].resets == 0
    for stream in streams[1:]:
        # A renewed stream: the lead silence, then a seed that starts in
        # silence (never inside a word that was already committed).
        assert not stream.audio[:LEAD + 3200].any()
    # The time base survives every renewal: positions stay exact after them.
    at = [p for _, p in positions(parts)]
    index = 0
    for f in finals(events):
        assert f["start_sample"] == 16000 + at[index]
        index += len(f["text"].split())


def test_a_word_already_arriving_at_the_renewal_is_decoded_from_the_seed():
    # The decoder is held back while a long silence and the first word after
    # it queue up, so it reaches the live edge -- and renews -- with that word
    # still undecoded in the old stream's tail. The word is too soft for the
    # renewal's level check to hear (a loud one holds the renewal back: see
    # the next test), so the seed must carry it over.
    made = []
    parts = [0.32, "alpha", "bravo", "charlie", 2.56, "hush"]
    with running(factory=fake_factory(made=made)) as client, connect(client) as ws:
        start(ws)
        made[0].gate = threading.Event()
        send_pcm(ws, speech(parts))
        # Every frame is in the stream's inbox before the decoder may start,
        # so it cannot catch up (and renew) before it reaches "delta".
        wait_all_audio_in(client, parts)
        made[0].gate.set()
        wait_for(client, 'stt_stream_renewals_total{profile="fast"}', 1)
        renewed = client_streams(made)[1]
        tail = renewed.audio[-TOKEN_SAMPLES:]
        assert not renewed.audio[:LEAD].any() and (tail == tail[0]).all() and tail[0] != 0
        send_pcm(ws, speech(["echo", 0.8]))
        ws.send_text('{"type":"flush"}')
        events, code = drain(ws)
    assert code == 1000
    assert [f["text"] for f in finals(events)] == ["alpha bravo charlie", "hush echo"]
    at = dict(positions(parts + ["echo"]))
    assert finals(events)[1]["start_sample"] == at["hush"]


def test_a_renewal_waits_for_the_next_pause_when_the_next_word_has_begun():
    # The same backlog with a LOUD word in the undecoded tail: the recognizer
    # still says "endpoint, nothing pending" (the word's first token is not
    # out yet), but the level check hears the onset, so the renewal is left
    # for the next pause instead of making the fresh stream re-decode 2 s of
    # seed before that word can show.
    made = []
    renewals = 'stt_stream_renewals_total{profile="fast"}'
    parts = [0.32, "alpha", "bravo", "charlie", 2.56, "delta"]
    with running(factory=fake_factory(made=made)) as client, connect(client) as ws:
        start(ws)
        made[0].gate = threading.Event()
        send_pcm(ws, speech(parts))
        wait_all_audio_in(client, parts)
        made[0].gate.set()
        read_until(ws, lambda e: e["type"] == "final")
        stream = client_streams(made)[0]
        deadline = time.monotonic() + 5
        while made[0].is_ready(stream):  # the decoder reaches the live edge...
            assert time.monotonic() < deadline
            time.sleep(0.01)
        time.sleep(0.1)  # ...and decides there
        assert metrics_value(client, renewals) == 0
        send_pcm(ws, speech(["echo", 2.72]))
        wait_for(client, renewals, 1)  # the next pause, a quiet one, renews
        ws.send_text('{"type":"flush"}')
        events, code = drain(ws)
    assert code == 1000
    assert [f["text"] for f in finals(events)] == ["delta echo"]
    renewed = client_streams(made)[1]
    assert VALUE["delta"] not in renewed.audio and VALUE["echo"] not in renewed.audio


def test_a_replayed_backlog_never_renews_before_the_decoder_catches_up():
    # 30 s of short utterances and long pauses arrive at once, as a
    # reconnect's replay does: whatever the renewals do, nothing is lost.
    made = []
    parts = [0.32]
    for i in range(10):
        parts += [WORDS[(2 * i + k) % 26] for k in range(2)] + [2.72]
    with running(make_settings(renew_chars=5), fake_factory(made=made)) as client:
        _, events, code = session(client, parts)
    assert code == 1000
    assert words_of(events) == [p for p in parts if isinstance(p, str)]
    at = [p for _, p in positions(parts)]
    assert [f["start_sample"] for f in finals(events)] == at[::2]


def test_punctuation_after_an_endpoint_leads_the_next_utterance():
    # Kept, not deleted: the consumer joins ". charlie, delta" to "alpha
    # bravo" without a space.
    with running() as client:
        _, events, _ = session(client, ["alpha", "bravo", 0.8, ".", 0.8, "charlie", ",", "delta", 0.8])
    assert [f["text"] for f in finals(events)] == ["alpha bravo", ". charlie, delta"]


def test_word_pieces_join_into_one_word():
    with running() as client:
        _, events, _ = session(client, ["alpha", "sau", "ce", 0.8])
    assert [f["text"] for f in finals(events)] == ["alpha sauce"]


def test_a_wide_profile_decodes_several_words_per_step():
    settings = make_settings(profiles=(make_profile("wide", 560, 4),))
    with running(settings) as client:
        _, events, code = session(client, [0.32, "alpha", "bravo", "charlie", "delta", 1.12, "echo", 0.8])
    assert code == 1000
    assert [f["text"] for f in finals(events)] == ["alpha bravo charlie delta", "echo"]


# -- limits ----------------------------------------------------------------------------

def test_no_audio_for_the_idle_limit_closes_4408():
    with running(make_settings(idle_s=0.3)) as client:
        with connect(client) as ws:
            start(ws)
            events, code = drain(ws)
        assert metrics_value(client, 'stt_stream_streams_total{profile="fast",outcome="idle"}') == 1
    assert code == 4408 and (events[-1]["code"], events[-1]["retryable"]) == ("idle", True)


@pytest.mark.parametrize("frame,code,error", [
    (b"\x00" * (server.MAX_FRAME_BYTES + 2), 4413, "frame_too_large"),
    (b"\x00" * 641, 4400, "protocol"),
    (b"", 4400, "protocol"),
])
def test_frame_limits(frame, code, error):
    with running() as client, connect(client) as ws:
        start(ws)
        ws.send_bytes(frame)
        events, closed = drain(ws)
    assert closed == code and events[-1]["code"] == error and events[-1]["retryable"] is False


def test_the_largest_frame_is_accepted():
    with running() as client, connect(client) as ws:
        start(ws)
        ws.send_bytes(speech(["alpha"] * 3 + [0.32]).tobytes()[:server.MAX_FRAME_BYTES])
        ws.send_text('{"type":"flush"}')
        events, code = drain(ws)
    assert code == 1000 and [f["text"] for f in finals(events)] == ["alpha alpha alpha"]


def test_audio_faster_than_a_resume_allows_is_refused():
    with running(make_settings(resume_max_s=0.0)) as client:
        with connect(client) as ws:
            start(ws)
            send_pcm(ws, np.zeros(16000 * 6, dtype=np.int16))  # 6 s at once; the ceiling is ~5 s
            events, code = drain(ws)
        assert metrics_value(client, 'stt_stream_streams_total{profile="fast",outcome="rate_limited"}') == 1
    assert code == 4429 and (events[-1]["code"], events[-1]["retryable"]) == ("rate_limited", True)


def test_audio_after_flush_is_a_protocol_error():
    with running() as client, connect(client) as ws:
        start(ws)
        ws.send_text('{"type":"flush"}')
        ws.send_bytes(b"\x00\x00")
        events, code = drain(ws)
    # Either the flush completed first (done, 1000) or the frame broke it.
    assert (code, events[-1]["type"]) in ((1000, "done"), (4400, "error"))


def test_unknown_messages_are_protocol_errors_and_pings_are_answered():
    with running() as client, connect(client) as ws:
        start(ws)
        ws.send_text('{"type":"ping","t":123.5}')
        assert recv_json(ws) == {"type": "pong", "t": 123.5}
        ws.send_text('{"type":"client_stats","partial_ms":[1,2]}')  # ignored
        ws.send_text('{"type":"shout"}')
        events, code = drain(ws)
    assert code == 4400 and events[-1]["code"] == "protocol"


def test_a_decode_failure_closes_the_stream_4500_and_the_worker_lives_on():
    made = []
    with running(factory=fake_factory(made=made)) as client:
        for recognizer in made:
            recognizer.fail_decode = True
        with connect(client) as ws:
            start(ws)
            send_pcm(ws, speech(["alpha", "bravo", 0.8]))
            events, code = drain(ws)
        assert code == 4500 and (events[-1]["code"], events[-1]["retryable"]) == ("internal", True)
        for recognizer in made:
            recognizer.fail_decode = False
        _, events, code = session(client, ["charlie", 0.8])
        assert code == 1000 and [f["text"] for f in finals(events)] == ["charlie"]
        assert metrics_value(client, 'stt_stream_streams_total{profile="fast",outcome="error"}') == 1


def test_a_client_that_leaves_frees_its_place():
    with running(make_settings(profiles=(make_profile("fast", 160, 1),))) as client:
        with connect(client) as ws:
            start(ws)
            send_pcm(ws, speech(["alpha", "bravo"]))
        assert client.get("/health").json()["streams"] == 0
        with connect(client) as ws:
            assert start(ws)["profile"] == "fast"
        assert metrics_value(client, 'stt_stream_streams_total{profile="fast",outcome="disconnected"}') == 2


# -- what is exported and what is logged --------------------------------------------------

def test_metrics_text_uses_closed_label_sets_only():
    with running() as client:
        session(client, ["alpha", "bravo", 0.8])
        text = client.get("/metrics").text
        assert metrics_value(client, 'stt_stream_streams_total{profile="fast",outcome="completed"}') == 1
        assert metrics_value(client, 'stt_stream_events_total{kind="final"}') == 1
        assert metrics_value(client, 'stt_stream_events_total{kind="done"}') == 1
        assert metrics_value(client, 'stt_stream_audio_seconds_total{profile="fast"}') == pytest.approx(0.32 + 0.8)
        # Every rejection reason, "other" included, is a series from the start.
        for reason in ("unauthorized", "not_ready", "protocol", "language", "capacity", "other"):
            assert metrics_value(client, f'stt_stream_rejections_total{{reason="{reason}"}}') == 0
    allowed = {
        "profile": {"fast"}, "outcome": set(server.OUTCOMES), "reason": set(server.REJECT_REASONS),
        "kind": set(server.EVENT_KINDS), "le": None,
    }
    types = {}
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split()
            types[name] = kind
            continue
        if line.startswith("#") or not line:
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        assert name.startswith("stt_stream_")
        if "{" in line:
            labels = line.split("{", 1)[1].split("}", 1)[0]
            for pair in labels.split(","):
                key, value = pair.split("=", 1)
                assert key in allowed, line
                if allowed[key] is not None:
                    assert value.strip('"') in allowed[key], line
    assert types["stt_stream_decode_step_seconds"] == "histogram"
    assert types["stt_stream_batch_size"] == "histogram"
    assert types["stt_stream_streams_total"] == "counter"
    assert types["stt_stream_renewals_total"] == "counter"
    assert types["stt_stream_active_streams"] == "gauge"
    buckets = [float(line.rsplit(" ", 1)[1]) for line in text.splitlines()
               if line.startswith('stt_stream_decode_step_seconds_bucket{profile="fast"')]
    assert buckets == sorted(buckets) and buckets[-1] >= 1


def test_no_transcript_audio_or_token_reaches_the_logs(caplog):
    with caplog.at_level(logging.DEBUG):
        with running() as client:
            _, events, _ = session(client, ["secretword", "alpha", 0.8])
            with pytest.raises(WebSocketDisconnect):
                with connect(client, headers={"authorization": "Bearer wrong-key-1-wrong-key-1"}):
                    pass
    assert [f["text"] for f in finals(events)] == ["secretword alpha"]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "stream closed" in logged
    for secret in ("secretword", "alpha", TOKEN, "wrong-key-1"):
        assert secret not in logged


# -- the resume point (review finding M1) ---------------------------------------------

def test_a_reconnect_that_resumes_from_the_last_finals_end_repeats_no_word():
    # The browser keeps the last 60 s of PCM and, after a drop, resumes from
    # committedUntil = the largest end_sample of the finals it saw
    # (frontend/lib/voiceLive.ts resumePoint). The socket drops 0.32 s into
    # the second utterance, after the first one's final.
    parts = [0.32, "alpha", "bravo", "charlie", 0.8, "delta", "echo", "foxtrot", 0.8]
    pcm = speech(parts)
    cut = len(speech(parts[:6]))
    with running() as client:
        with connect(client) as ws:
            start(ws)
            send_pcm(ws, pcm[:cut])
            first = finals(read_until(ws, lambda e: e["type"] == "final"))
        resume = max(f["end_sample"] for f in first)
        with connect(client) as ws:
            start(ws, first_sample=resume, first_u=first[-1]["u"] + 1)
            send_pcm(ws, pcm[resume:])
            ws.send_text('{"type":"flush"}')
            events, code = drain(ws)
    second = finals(events)
    assert code == 1000
    heard = [w for f in first + second for w in f["text"].split()]
    assert heard == ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    assert second[0]["u"] == 1 and second[0]["start_sample"] == dict(positions(parts))["delta"]


# -- a Stop is not held behind a neighbour (review finding M2) ------------------------

def test_a_stop_gets_done_while_a_neighbours_replay_is_still_decoding():
    # Two streams on ONE decode worker. X replays a 60 s backlog, as a
    # reconnect does; Y says two words and stops. The decoder is then given
    # 40 steps: enough for all of Y, a tenth of X. Y's last final and `done`
    # must arrive inside those 40 steps -- the gateway waits 5 s for `done`,
    # and under overload the decode loop never runs dry at all.
    made = []
    with running(make_settings(workers=1), fake_factory(made=made)) as client:
        recognizer = made[0]
        recognizer.permits = threading.Semaphore(0)
        backlog = []
        while len(speech(backlog)) < 60 * 16000:
            backlog += ["alpha", "bravo", "charlie", 0.8]
        y_parts = [0.32, "delta", "echo", 0.32]
        with connect(client) as x, connect(client) as y:
            start(x, first_sample=16000 * 600)
            start(y)
            send_pcm(x, speech(backlog), frame=8192)
            send_pcm(y, speech(y_parts))
            y.send_text('{"type":"flush"}')
            y.send_text('{"type":"ping","t":1}')
            assert recv_json(y) == {"type": "pong", "t": 1}  # the flush is in Y's inbox
            wait_all_audio_in(client, backlog, y_parts)
            before = len(recognizer.batches)
            recognizer.permits.release(40)
            events = read_until(y, lambda e: e["type"] == "done", timeout=5)
            assert [f["text"] for f in finals(events)] == ["delta echo"]
            assert len(recognizer.batches) - before <= 40  # X's backlog is nowhere near decoded
            recognizer.permits.release(100_000)
            x.send_text('{"type":"flush"}')
            x_events, code = drain(x, timeout=30)
    assert code == 1000 and words_of(x_events) == [w for w in backlog if isinstance(w, str)]


# -- the engine's decode budget (review finding M3) -------------------------------------

def test_admission_refuses_beyond_the_engines_decode_budget():
    # fast (160 ms) costs 2 units, wide (560 ms) 1, and the budget is 5: two
    # fast streams and one wide one fill it although both profiles have
    # slots left. A nearly spent budget sends a stream to the cheap profile.
    settings = make_settings(profiles=(make_profile("fast", 160, 4), make_profile("wide", 560, 8)), max_cost=5)
    with running(settings) as client:
        assert client.get("/health").json()["capacity"] == 5
        with connect(client) as a, connect(client) as b, connect(client) as c:
            assert start(a)["profile"] == "fast"
            assert start(b)["profile"] == "fast"
            assert start(c)["profile"] == "wide"
            with connect(client) as d:
                d.send_text(start_message())
                events, code = drain(d)
            assert code == 4429 and (events[0]["code"], events[0]["retryable"]) == ("capacity", True)
            body = client.get("/health").json()
            assert body["streams"] == 3 and body["cost"] == {"budget": 5, "in_use": 5}
            assert [p["cost"] for p in body["profiles"]] == [2, 1]
            assert metrics_value(client, "stt_stream_cost_in_use_units") == 5
            assert metrics_value(client, "stt_stream_cost_budget_units") == 5
        # Every place is handed back when its stream closes.
        assert client.get("/health").json()["cost"]["in_use"] == 0
        with connect(client) as e:
            assert start(e)["profile"] == "fast"
        assert metrics_value(client, 'stt_stream_rejections_total{reason="capacity"}') == 1


# -- renewal at every safe point, the default (spec section 11) ---------------------------

def test_renewing_right_after_every_final_loses_nothing():
    # The most aggressive setting (0.6 s: AT every endpoint) puts a fresh
    # stream's seam after every utterance, so it tests the seam hardest.
    made = []
    renewals = 'stt_stream_renewals_total{profile="fast"}'
    parts, seen = [0.32], []
    with running(make_settings(renew_silence_s=0.6), fake_factory(made=made)) as client, connect(client) as ws:
        start(ws, first_sample=16000, language="hi")
        send_pcm(ws, speech([0.32]))
        for i in range(6):
            segment = [WORDS[(2 * i + k) % 26] for k in range(2)] + [1.12]
            parts += segment
            send_pcm(ws, speech(segment))
            seen += finals(read_until(ws, lambda e: e["type"] == "final"))
            wait_for(client, renewals, i + 1)  # right after the final, in its silence
        ws.send_text('{"type":"flush"}')
        events, code = drain(ws)
    assert code == 1000 and not finals(events)
    spoken = [p for p in parts if isinstance(p, str)]
    assert [f["text"].split() for f in seen] == [spoken[k:k + 2] for k in range(0, 12, 2)]
    at = [p for _, p in positions(parts)]
    assert [f["start_sample"] for f in seen] == [16000 + at[k] for k in range(0, 12, 2)]
    streams = client_streams(made)
    assert len(streams) == 7 and made[0].resets == 0
    for index, stream in enumerate(streams[1:]):
        # A fresh stream gets the language again, the lead silence, and a
        # seed from where the last final ended: in its silence, so the first
        # sound in it is the NEXT utterance's first word, never a committed one.
        assert stream.options == {"language": "hi"}
        assert not stream.audio[:LEAD + 4800].any()
        voiced = np.flatnonzero(stream.audio)
        if index + 1 < 6:
            assert stream.audio[voiced[0]] == VALUE[spoken[2 * (index + 1)]]


def test_by_default_a_stream_renews_after_a_pause_between_thoughts_not_inside_one():
    # Settings.renew_silence_s: 2.0 s of silence since the last token. Short
    # pauses (1.12 s) end utterances without a renewal; a long one renews.
    renewals = 'stt_stream_renewals_total{profile="fast"}'
    parts, seen = [0.32], []
    with running() as client, connect(client) as ws:
        start(ws)
        send_pcm(ws, speech([0.32]))
        for i, pause in enumerate([1.12, 1.12, 2.72, 1.12]):
            segment = [WORDS[(2 * i + k) % 26] for k in range(2)] + [pause]
            parts += segment
            send_pcm(ws, speech(segment))
            seen += finals(read_until(ws, lambda e: e["type"] == "final"))
            if pause > 2:
                wait_for(client, renewals, 1)
            else:
                assert metrics_value(client, renewals) == (1 if i > 2 else 0)
        ws.send_text('{"type":"flush"}')
        rest, code = drain(ws)
        assert metrics_value(client, renewals) == 1
    assert code == 1000 and not finals(rest)
    assert [w for f in seen for w in f["text"].split()] == [p for p in parts if isinstance(p, str)]


def test_renewal_at_every_final_keeps_a_recognizers_rule_3_from_latching():
    # Scaled down: a recognizer whose rule 3 fires 6 s into a stream (the
    # real one counts from the stream's creation and nothing resets it). A
    # stream that reached it would hold the endpoint for good: no endpoint
    # finals after it. Renewing at every final restarts the stream's clock.
    renewals = 'stt_stream_renewals_total{profile="fast"}'
    settings = make_settings(max_utterance_s=10.0, renew_silence_s=0.6)
    with running(settings, fake_factory(rule3_s=6.0)) as client, connect(client) as ws:
        start(ws)
        seen = []
        for i in range(10):  # 10 x 1.6 s: rule 3 would latch in the fourth
            segment = [WORDS[(3 * i + k) % 26] for k in range(3)] + [1.12]
            send_pcm(ws, speech(segment))
            seen += finals(read_until(ws, lambda e: e["type"] == "final", timeout=3))
            wait_for(client, renewals, i + 1)
        ws.send_text('{"type":"flush"}')
        rest, code = drain(ws)
    assert code == 1000 and not finals(rest)
    assert [(f["u"], f["endpoint_ms"], len(f["text"].split())) for f in seen] == [(i, 600, 3) for i in range(10)]


# -- start.mode "meeting" (spec section 11) -----------------------------------------------

def test_a_meeting_stream_holds_an_utterance_through_a_pause_that_ends_one_in_dictation():
    parts = [0.32, "alpha", "bravo", 0.64, "charlie", "delta", 1.6]
    with running() as client:
        _, dictation, _ = session(client, parts)
        ready, meeting, code = session(client, parts, mode="meeting")
    assert ready["mode"] == "meeting" and code == 1000
    assert [f["text"] for f in finals(dictation)] == ["alpha bravo", "charlie delta"]
    assert [(f["text"], f["endpoint_ms"]) for f in finals(meeting)] == [("alpha bravo charlie delta", 900)]


def test_a_meeting_stream_forces_a_long_utterance_out_at_its_own_cap():
    words = [WORDS[i % 26] for i in range(40)]  # 6.4 s with no pause
    settings = make_settings(max_utterance_s=30.0, meeting_max_utterance_s=2.0)
    with running(settings) as client:
        _, events, code = session(client, words + [1.6], mode="meeting")
        _, plain, _ = session(client, words + [1.6])
    assert code == 1000 and words_of(events) == words
    forced = [f for f in finals(events) if f["endpoint_ms"] == 0]
    assert len(forced) >= 2 and all((f["end_sample"] - f["start_sample"]) / 16000 <= 2.2 for f in forced)
    assert finals(events)[-1]["endpoint_ms"] == 900
    assert [f["endpoint_ms"] for f in finals(plain)] == [600]  # dictation's cap is its own 30 s


# -- robustness (review finding L3) ---------------------------------------------------------

@pytest.mark.parametrize("text", ["[" * 5000 + "]" * 5000, '{"a":' * 3000 + "1" + "}" * 3000])
def test_deeply_nested_json_after_start_is_a_protocol_error(text):
    with running() as client, connect(client) as ws:
        start(ws)
        ws.send_text(text)
        events, code = drain(ws)
    assert code == 4400 and (events[-1]["code"], events[-1]["retryable"]) == ("protocol", False)


@pytest.mark.parametrize("stamp", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_a_ping_must_carry_a_finite_number(stamp):
    with running() as client, connect(client) as ws:
        start(ws)
        ws.send_text('{"type":"ping","t":%s}' % stamp)
        events, code = drain(ws)
    assert code == 4400 and events[-1]["code"] == "protocol"
    assert not [e for e in events if e["type"] == "pong"]


def test_an_event_that_cannot_be_encoded_fails_the_stream_and_says_so(monkeypatch, caplog):
    encode = server._dumps

    def broken(event):
        if event.get("type") == "partial":
            raise ValueError("Out of range float values are not JSON compliant")
        return encode(event)

    monkeypatch.setattr(server, "_dumps", broken)
    with caplog.at_level(logging.DEBUG), running() as client, connect(client) as ws:
        start(ws)
        send_pcm(ws, speech(["secretword", "alpha", 0.8]))
        events, code = drain(ws, timeout=5)
    assert code == 4500 and (events[-1]["code"], events[-1]["retryable"]) == ("internal", True)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "a partial event could not be encoded (ValueError)" in logged
    assert "secretword" not in logged


# -- the message ceiling (spec section 11) ---------------------------------------------------

def test_a_flood_of_tiny_frames_is_refused_by_the_message_ceiling():
    # One sample a frame: the SAMPLE ceiling never notices them. With no
    # resume allowance the message ceiling is (elapsed + 5 s) x 50 a second.
    with running(make_settings(resume_max_s=0.0)) as client:
        with connect(client) as ws:
            start(ws)
            for _ in range(1000):
                ws.send_bytes(b"\x00\x00")
            ws.send_text('{"type":"flush"}')
            events, code = drain(ws)
        assert metrics_value(client, 'stt_stream_streams_total{profile="fast",outcome="rate_limited"}') == 1
    assert code == 4429 and (events[-1]["code"], events[-1]["retryable"]) == ("rate_limited", True)


def test_a_flood_of_pings_is_refused_by_the_same_ceiling():
    with running(make_settings(resume_max_s=0.0)) as client, connect(client) as ws:
        start(ws)
        for i in range(1000):
            ws.send_text('{"type":"ping","t":%d}' % i)
        ws.send_text('{"type":"flush"}')
        events, code = drain(ws)
    assert code == 4429 and events[-1]["code"] == "rate_limited"
    assert 0 < len([e for e in events if e["type"] == "pong"]) < 1000


def test_a_shorter_frame_buys_more_messages_and_a_longer_one_no_fewer():
    assert (server.message_rate(40), server.message_rate(20), server.message_rate(10)) == (50, 100, 200)
    assert (server.message_rate(5), server.message_rate(100), server.message_rate(-3)) == (200, 50, 200)
    with running(make_settings(resume_max_s=0.0)) as client, connect(client) as ws:
        start(ws, frame_ms=10)  # 1,000 messages at the start instead of 250
        for _ in range(600):
            ws.send_bytes(b"\x00\x00")
        ws.send_text('{"type":"flush"}')
        events, code = drain(ws)
    assert code == 1000 and events[-1] == {"type": "done"}


# -- the compute counter (spec section 11) ----------------------------------------------------

def test_every_stream_of_a_decode_call_is_charged_the_whole_call():
    metrics = server.Metrics(["p"])
    metrics.decode_step("p", 0.25, 3)
    metrics.decode_step("p", 0.5, 1)
    values = dict(line.rsplit(" ", 1) for line in metrics.render([]).splitlines() if not line.startswith("#"))
    assert float(values['stt_stream_compute_seconds_total{profile="p"}']) == 0.25 * 3 + 0.5
    assert float(values['stt_stream_decode_step_seconds_sum{profile="p"}']) == 0.75
    assert float(values['stt_stream_batch_size_sum{profile="p"}']) == 4
