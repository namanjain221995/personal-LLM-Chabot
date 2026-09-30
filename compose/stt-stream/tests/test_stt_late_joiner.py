"""A stream that connects WHILE its decode worker is replaying a
neighbour's backlog. The fixer's M2 test opens both streams before the backlog
arrives; here Y is admitted after X's backlog is already decoding."""
import threading
import time

from conftest import AUTH, drain, finals, make_settings, recv_json, running, send_pcm, start, words_of
from stt_fakes import fake_factory, speech


def connect(client):
    return client.websocket_connect("/v1/stream", headers=AUTH)


def read_until(ws, stop, timeout=10.0):
    got = []
    deadline = time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        assert left > 0, f"never saw the event; got {[e['type'] for e in got]}"
        event = recv_json(ws, timeout=left)
        got.append(event)
        if stop(event):
            return got


def metric(client, series):
    for line in client.get("/metrics").text.splitlines():
        if line.startswith(series + " "):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def test_a_stream_admitted_during_a_neighbours_replay_gets_done_in_time():
    made = []
    with running(make_settings(workers=1), fake_factory(made=made)) as client:
        recognizer = made[0]
        recognizer.permits = threading.Semaphore(0)
        backlog = []
        while len(speech(backlog)) < 60 * 16000:
            backlog += ["alpha", "bravo", "charlie", 0.8]
        with connect(client) as x:
            start(x, first_sample=16000 * 600)
            send_pcm(x, speech(backlog), frame=8192)
            # the worker is now INSIDE its decode loop on X's backlog (blocked on a permit)
            deadline = time.monotonic() + 5
            while metric(client, 'stt_stream_audio_seconds_total{profile="fast"}') < len(speech(backlog)) / 16000 - 1e-6:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            already = len(recognizer.batches)
            recognizer.permits.release(5)
            # Wait for those 5 steps to be taken, so the count below starts
            # after them (under load they could land after it: 64 > 60).
            deadline = time.monotonic() + 5
            while len(recognizer.batches) < already + 5:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            with connect(client) as y:          # admitted during the replay
                start(y)
                y_parts = [0.32, "delta", "echo", 0.32]
                send_pcm(y, speech(y_parts))
                y.send_text('{"type":"flush"}')
                y.send_text('{"type":"ping","t":1}')
                assert recv_json(y) == {"type": "pong", "t": 1}
                before = len(recognizer.batches)
                recognizer.permits.release(60)   # 60 more steps: all of Y, a sixth of X
                events = read_until(y, lambda e: e["type"] in ("done", "error"), timeout=5)
                assert events[-1]["type"] == "done", events[-1]
                assert [f["text"] for f in finals(events)] == ["delta echo"]
                assert len(recognizer.batches) - before <= 60
            recognizer.permits.release(100_000)
            x.send_text('{"type":"flush"}')
            x_events, code = drain(x, timeout=30)
    assert code == 1000 and words_of(x_events) == [w for w in backlog if isinstance(w, str)]
