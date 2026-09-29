"""Live dictation's WebSocket gateway (app/voice_live.py, 2026-09-29).

While a person dictates, the browser streams 16 kHz PCM to
/audio/sessions/{id}/live and gets back a partial hypothesis within a few
hundred milliseconds and a final one per utterance. A WebSocket passes every
middleware this app has untouched, so these tests hold the handler itself to
what those would have done, and to the build spec's protocol:

  * Origin is checked BEFORE accept (a bare 403), because a WebSocket answer
    is readable cross-origin and would otherwise hand a page elsewhere
    somebody's transcript; X-Forwarded-Host counts only from our own proxy;
  * every later refusal is accepted, explained in one `error` and closed with
    its code: signed out 4401, voice off 4403, no live path / not yours /
    stopped 4404, too many 4429, bad protocol 4400, frame too large 4413;
  * one stream per recording (the newest wins, 4409), a stream ceiling, an
    arrival ceiling, an idle close, and a re-check of the sign-in, the
    feature and the recording while the stream runs;
  * partials replace, finals commit in order, flush ends with done and 1000,
    and a resumed stream continues the sample and utterance numbering;
  * an engine that is down, silent, full or dies mid-stream is 4503 / 4429,
    never a hang, and a failing one is stood down;
  * metrics use closed labels and start at zero, and no transcript text,
    cookie or token reaches the logs.

THE ENGINE is FakeEngine below, injected through voice_live.connect_engine
(the seam) and speaking the spec's section-3 protocol: it hears a frame of
non-zero samples as one word, a silent frame as the pause that ends an
utterance, and a frame opening with 32767 as its own crash.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import anyio
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from app import db, dictation, metrics, voice_live
from app.authn import proxy_trust, store
from app.config import settings
from app.main import app
from tests.test_voice_sessions import create, voice  # noqa: F401 — `voice` is a fixture

SUBPROTOCOL = "techsara.voice.v1"
SAME_ORIGIN = "http://testserver"
#: 40 ms frames, as the browser's worklet posts them.
SPEECH = (1000).to_bytes(2, "little", signed=True) * 640
SILENCE = bytes(1280)
CRASH = (32767).to_bytes(2, "little", signed=True) * 640
ENGINE_URL = "ws://engine-a.test:30009"
ENGINE_TOKEN = "test-token-1"


# ------------------------------------------------------------ the engine --

_CLOSED = object()


class FakeEngine:
    """compose/stt-stream/server.py's contract, in process. `mode` is what it
    does with a start: ready, capacity, unsupported_language, refuse (the
    connection itself fails), hang (never answers) or garbage."""

    def __init__(self) -> None:
        self.mode = "ready"
        self.flush_answer = True
        self.urls: List[str] = []
        self.tokens: List[str] = []
        self.starts: List[Dict[str, Any]] = []
        self.streams: List["FakeStream"] = []

    async def connect(self, url: str, token: str, timeout_s: float) -> "FakeStream":
        self.urls.append(url)
        self.tokens.append(token)
        if self.mode == "refuse":
            raise ConnectionRefusedError("the fake engine is down")
        stream = FakeStream(self)
        self.streams.append(stream)
        return stream


class FakeStream:
    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        self.outbox: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.position = 0
        self.u = 0
        self.words: List[str] = []
        self.utterance_start = 0

    def _emit(self, event: Dict[str, Any]) -> None:
        self.outbox.put_nowait(json.dumps(event))

    async def send(self, data: Any) -> None:
        if self.closed:
            raise ConnectionError("the fake engine closed this stream")
        if isinstance(data, str):
            message = json.loads(data)
            if message["type"] == "start":
                self.engine.starts.append(message)
                self.position, self.u = message["first_sample"], message["first_u"]
                mode = self.engine.mode
                if mode == "ready":
                    # Internals a browser must never see.
                    self._emit({"type": "ready", "v": 1, "profile": "multi-fast", "chunk_ms": 160,
                                "model": "nemotron-secret", "first_sample": self.position})
                elif mode in ("capacity", "unsupported_language"):
                    self._emit({"type": "error", "code": mode, "message": "no", "retryable": mode == "capacity"})
                    self.outbox.put_nowait(_CLOSED)
                elif mode == "garbage":
                    self._emit({"type": "hello"})
            elif message["type"] == "flush":
                self._final()
                if self.engine.flush_answer:
                    self._emit({"type": "done"})
            return
        start = self.position
        self.position += len(data) // 2
        if int.from_bytes(data[:2], "little", signed=True) == 32767:
            self.closed = True
            self.outbox.put_nowait(_CLOSED)
            return
        if any(data):
            if not self.words:
                self.utterance_start = start
            self.words.append(f"kiwi{len(self.words) + 1}")
            self._emit({"type": "partial", "u": self.u, "text": " ".join(self.words),
                        "start_sample": self.utterance_start, "end_sample": self.position, "compute_ms": 3})
        else:
            self._final()

    def _final(self) -> None:
        if not self.words:
            return
        self._emit({"type": "final", "u": self.u, "text": " ".join(self.words),
                    "start_sample": self.utterance_start, "end_sample": self.position,
                    "compute_ms": 3, "endpoint_ms": 600})
        self.u += 1
        self.words = []

    async def recv(self) -> str:
        item = await self.outbox.get()
        if item is _CLOSED:
            raise ConnectionError("the fake engine went away")
        return item

    async def close(self) -> None:
        self.closed = True
        self.outbox.put_nowait(_CLOSED)


@pytest.fixture()
def live(voice, monkeypatch):
    """A deployment with recording sessions and one live engine (the fake)."""
    engine = FakeEngine()
    monkeypatch.setattr(voice_live, "connect_engine", engine.connect)
    monkeypatch.setattr(settings, "voice_live_enabled", True)
    monkeypatch.setattr(settings, "voice_live_engine_urls", (ENGINE_URL,))
    monkeypatch.setattr(settings, "voice_live_engine_token", ENGINE_TOKEN)
    monkeypatch.setattr(settings, "voice_live_languages", ("auto", "en", "hi"))
    voice_live.reset_for_tests()
    metrics.reset()
    yield SimpleNamespace(engine=engine)
    voice_live.reset_for_tests()


# --------------------------------------------------------------- helpers --


def socket(client: TestClient, sid: str, *, origin: Optional[str] = SAME_ORIGIN,
           headers: Optional[Dict[str, str]] = None):
    sent = {"origin": origin} if origin else {}
    sent.update(headers or {})
    return client.websocket_connect(f"/audio/sessions/{sid}/live", subprotocols=[SUBPROTOCOL], headers=sent)


def start(**fields: Any) -> Dict[str, Any]:
    message = {"type": "start", "v": 1, "encoding": "pcm_s16le", "sample_rate": 16000, "channels": 1,
               "frame_ms": 40, "source": "mic", "resume_from_sample": 0, "next_u": 0, "clock_offset_ms": 12}
    message.update(fields)
    return {key: value for key, value in message.items() if value is not None}


def _next(ws, timeout: float = 10.0) -> Dict[str, Any]:
    """The next message the server sent. Bounded: the session's own
    receive() would wait for ever on a server that says nothing."""
    async def receive():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    return ws.portal.call(receive)


def event(ws, timeout: float = 10.0) -> Dict[str, Any]:
    message = _next(ws, timeout)
    assert message["type"] == "websocket.send", message
    return json.loads(message["text"])


def close_code(ws, timeout: float = 10.0) -> int:
    message = _next(ws, timeout)
    assert message["type"] == "websocket.close", message
    return message["code"]


def refused(ws, code: str, close: int) -> Dict[str, Any]:
    error = event(ws)
    assert error["type"] == "error" and error["code"] == code, error
    assert set(error) == {"type", "code", "message", "retryable"}
    assert close_code(ws) == close
    return error


def assert_forbidden(client: TestClient, sid: str, **options: Any) -> None:
    """Refused before accept: a close before accept, which is how ASGI says
    403 (uvicorn answers it with a 403 and no body)."""
    with pytest.raises(WebSocketDisconnect) as refusal:
        with socket(client, sid, **options):
            pass
    assert not isinstance(refusal.value, WebSocketDenialResponse)
    assert refusal.value.code == 1008


def ready(ws, **fields: Any) -> Dict[str, Any]:
    ws.send_json(start(**fields))
    answer = event(ws)
    assert answer["type"] == "ready", answer
    return answer


def session(client: TestClient) -> str:
    response = create(client)
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def settle(timeout: float = 5.0) -> None:
    """Every stream has ended and been accounted for."""
    deadline = time.monotonic() + timeout
    while voice_live.REGISTRY.active():
        assert time.monotonic() < deadline, "a live stream did not end"
        time.sleep(0.01)


def counter(name: str, **labels: str) -> float:
    return metrics._counters.get(name, {}).get(metrics._clean(labels, name), 0.0)


def observations(name: str, **labels: str) -> int:
    entry = metrics._hists.get(name, {}).get(metrics._clean(labels, name))
    return entry[2] if entry else 0


def uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


# ---------------------------------------------------------------- Origin --


def test_a_foreign_origin_is_refused_with_a_bare_403_before_accept(live, login_client):
    alice = login_client("alice")
    sid = session(alice)
    for origin in ("https://evil.example", "null", "http://testserver.evil.example", "http://testserver:8080", None):
        assert_forbidden(alice, sid, origin=origin)
    assert live.engine.urls == [], "nothing reached the engine"
    assert counter("voice_stream_rejections_total", reason="origin") == 5


def test_an_origin_listed_in_cors_allow_origins_is_accepted(live, login_client, monkeypatch):
    monkeypatch.setattr(settings, "cors_allow_origins", ["https://app.example"])
    alice = login_client("alice")
    sid = session(alice)
    with socket(alice, sid, origin="https://app.example") as ws:
        assert ws.accepted_subprotocol == SUBPROTOCOL
        ready(ws)


def test_x_forwarded_host_counts_only_when_our_own_proxy_sent_it(live, login_client, monkeypatch):
    """The relay forwards the browser's Host as X-Forwarded-Host (the socket
    reaches this process as orchestrator:8080). Believed from the frontend,
    ignored from anyone else, who could otherwise name any host at all."""
    proxy_trust.reset_caches()
    monkeypatch.delenv("AUTH_TRUSTED_PROXIES", raising=False)
    monkeypatch.setattr(settings, "public_api_trusted_proxies", ("10.9.0.0/16",))
    monkeypatch.setattr(proxy_trust, "resolve_host", lambda host: frozenset())
    monkeypatch.setattr(proxy_trust, "read_local_gateways", lambda: frozenset())
    try:
        alice = login_client("alice")
        sid = session(alice)
        relayed = {"host": "orchestrator:8080", "x-forwarded-host": "ai.example.com"}
        assert_forbidden(alice, sid, origin="https://ai.example.com", headers=relayed)
        frontend = TestClient(app, client=("10.9.0.6", 41000), cookies=alice.cookies)
        with socket(frontend, sid, origin="https://ai.example.com", headers=relayed) as ws:
            ready(ws)
        assert_forbidden(frontend, sid, origin="https://ai.example.com:8443", headers=relayed)
    finally:
        proxy_trust.reset_caches()


# ------------------------------------------------------- the handshake --


def test_a_signed_out_socket_is_accepted_then_closed_4401(live, login_client, anonymous_mode):
    sid = session(login_client("alice"))
    with socket(TestClient(app), sid) as ws:
        assert refused(ws, "signed_out", 4401)["retryable"] is False
    assert counter("voice_stream_rejections_total", reason="signed_out") == 1


def test_a_member_whose_voice_input_is_off_is_closed_4403(live, login_client):
    root = login_client("root", role="super_admin")
    bob = login_client("bob")
    sid = session(bob)
    root.put(f"/admin/api/members/{uid('bob')}/access", json={"features": {"voice_input": False}})
    with socket(bob, sid) as ws:
        refused(ws, "voice_off", 4403)


def test_without_an_engine_the_live_path_is_neither_offered_nor_served(live, login_client, monkeypatch):
    offered = create(login_client("alice")).json()["config"]["live"]
    assert offered == {"path": "/api/audio/sessions/{id}/live", "path_template": "/api/audio/sessions/{id}/live",
                       "sample_rate": 16000, "frame_ms": 40, "resume_max_s": 60}
    monkeypatch.setattr(settings, "voice_live_engine_urls", ())
    bob = login_client("bob")
    response = create(bob)
    assert response.json()["config"]["live"] is None
    with socket(bob, response.json()["session_id"]) as ws:
        refused(ws, "live_unavailable", 4404)
    assert live.engine.urls == []


def test_someone_elses_recording_and_an_unknown_one_get_the_same_404(live, login_client):
    sid = session(login_client("alice"))
    bob = login_client("bob")
    answers = []
    for target in (sid, "0" * 32, "not-a-session-id"):
        with socket(bob, target) as ws:
            answers.append(refused(ws, "not_found", 4404))
    assert answers[0] == answers[1] == answers[2]
    assert answers[0]["message"] == dictation._not_found().detail


def test_a_recording_that_has_stopped_is_closed_4404_session_closed(live, login_client):
    alice = login_client("alice")
    sid = session(alice)
    assert alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": None, "ended_by": "person"}).status_code == 202
    with socket(alice, sid) as ws:
        refused(ws, "session_closed", 4404)


def test_connections_per_minute_are_limited_4429(live, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_connects_per_min", 1)
    alice = login_client("alice")
    sid = session(alice)
    with socket(alice, sid) as ws:
        ready(ws)
    settle()
    with socket(alice, sid) as ws:
        assert refused(ws, "rate_limited", 4429)["retryable"] is True


def test_the_stream_ceiling_refuses_a_new_recording_but_not_a_reconnect(live, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_max_streams", 1)
    alice, bob = login_client("alice"), login_client("bob")
    alices, bobs = session(alice), session(bob)
    with socket(alice, alices) as first:
        ready(first)
        with socket(bob, bobs) as ws:
            assert refused(ws, "capacity", 4429)["retryable"] is True
        with socket(alice, alices) as again:
            ready(again)
            refused(first, "superseded", 4409)
    assert counter("voice_stream_rejections_total", reason="capacity") == 1


def test_a_second_connection_for_a_recording_supersedes_the_first_4409(live, login_client):
    alice = login_client("alice")
    sid = session(alice)
    with socket(alice, sid) as old:
        ready(old)
        with socket(alice, sid) as new:
            ready(new, resume_from_sample=640, next_u=0)
            error = refused(old, "superseded", 4409)
            assert error["retryable"] is False
            new.send_bytes(SPEECH)
            assert event(new)["type"] == "partial"
    settle()
    assert counter("voice_stream_sessions_total", outcome="superseded") == 1


# ------------------------------------------------------- the protocol --


@pytest.mark.parametrize(
    "first",
    [
        "not json at all",
        json.dumps({"type": "flush"}),
        json.dumps(start(sample_rate=44100)),
        json.dumps(start(encoding="opus")),
        json.dumps(start(channels=2)),
        json.dumps(start(v=2)),
        json.dumps(start(resume_from_sample=-1)),
        json.dumps(start(next_u="3")),
        json.dumps(start(resume_from_sample=True)),
        json.dumps(start(clock_offset_ms="soon")),
        "[" * 5000,
    ],
)
def test_a_bad_start_is_closed_4400(live, login_client, first):
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ws.send_text(first)
        refused(ws, "protocol", 4400)
    assert live.engine.starts == []


def test_audio_before_the_start_is_a_protocol_error(live, login_client):
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ws.send_bytes(SPEECH)
        refused(ws, "protocol", 4400)


def test_the_language_is_checked_and_passed_to_the_engine_unchanged(live, login_client):
    alice = login_client("alice")
    sid = session(alice)
    with socket(alice, sid) as ws:
        ready(ws, language="hi")
    settle()
    with socket(alice, sid) as ws:
        ready(ws)
    settle()
    assert [s["language"] for s in live.engine.starts] == ["hi", "auto"]
    for language in ("gu", "EN", "english-united-kingdom", 7):
        with socket(alice, sid) as ws:
            ws.send_json(start(language=language))
            refused(ws, "unsupported_language", 4400)
    assert len(live.engine.starts) == 2, "a refused language never reaches the engine"


def test_a_frame_over_the_limit_is_closed_4413(live, login_client):
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        ws.send_bytes(bytes(16384 + 2))
        assert refused(ws, "frame_too_large", 4413)["retryable"] is False


def test_an_odd_length_frame_is_closed_4400(live, login_client):
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        ws.send_bytes(b"\x01\x02\x03")
        refused(ws, "protocol", 4400)


def test_audio_faster_than_the_clock_plus_the_resume_allowance_is_closed_4429(live, login_client, monkeypatch):
    """At most (wall seconds since start + VOICE_LIVE_RESUME_MAX_S + 5) x
    16,000 samples: a reconnect may replay its ring at once, a microphone
    cannot outrun time. With no resume allowance, 5 s of head start."""
    monkeypatch.setattr(settings, "voice_live_resume_max_s", 0.0)
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        for _ in range(20):  # 20 x 8,192 samples: 10 s of audio, far ahead of the clock
            ws.send_bytes(bytes(16384))
        assert refused(ws, "rate_limited", 4429)["retryable"] is True
    settle()
    assert counter("voice_stream_rejections_total", reason="rate_limited") == 1
    assert counter("voice_stream_sessions_total", outcome="rejected") == 1


def test_partials_replace_finals_commit_and_flush_ends_with_done(live, login_client):
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        assert ws.accepted_subprotocol == SUBPROTOCOL
        assert ready(ws) == {"type": "ready", "v": 1, "sample_rate": 16000, "frame_ms": 40,
                             "max_frame_bytes": 16384, "resume_from_sample": 0, "next_u": 0}
        ws.send_bytes(SPEECH)
        assert event(ws) == {"type": "partial", "u": 0, "text": "kiwi1", "start_sample": 0, "end_sample": 640}
        ws.send_bytes(SPEECH)
        # The WHOLE hypothesis again, never an increment; no engine timings.
        assert event(ws) == {"type": "partial", "u": 0, "text": "kiwi1 kiwi2", "start_sample": 0, "end_sample": 1280}
        ws.send_json({"type": "ping", "t": 1234.5})
        assert event(ws) == {"type": "pong", "t": 1234.5}
        ws.send_bytes(SILENCE)
        assert event(ws) == {"type": "final", "u": 0, "text": "kiwi1 kiwi2", "start_sample": 0, "end_sample": 1920}
        ws.send_bytes(SPEECH)
        assert event(ws)["u"] == 1
        ws.send_json({"type": "flush"})
        assert event(ws) == {"type": "final", "u": 1, "text": "kiwi1", "start_sample": 1920, "end_sample": 2560}
        assert event(ws) == {"type": "done"}
        assert close_code(ws) == 1000
    settle()
    assert live.engine.urls == [ENGINE_URL + "/v1/stream"]
    assert live.engine.tokens == [ENGINE_TOKEN]
    assert live.engine.starts == [{"type": "start", "sample_rate": 16000, "encoding": "pcm_s16le", "first_sample": 0,
                                   "first_u": 0, "mode": "dictation", "language": "auto"}]
    assert live.engine.streams[0].closed, "the engine's stream was closed with the browser's"
    assert counter("voice_stream_sessions_started_total") == 1
    assert counter("voice_stream_sessions_total", outcome="completed") == 1
    assert counter("voice_stream_utterances_total") == 2
    assert counter("voice_stream_audio_received_seconds_total") == pytest.approx(4 * 640 / 16000)
    assert observations("voice_stream_first_partial_seconds") == 2
    assert observations("voice_stream_final_seconds") == 2
    assert observations("voice_stream_event_lag_seconds", event="partial") == 3
    assert observations("voice_stream_event_lag_seconds", event="final") == 2


def test_a_flush_the_engine_never_answers_still_ends_with_done_and_1000(live, login_client, monkeypatch):
    monkeypatch.setattr(voice_live, "FLUSH_WAIT_S", 0.2)
    live.engine.flush_answer = False
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        ws.send_json({"type": "flush"})
        assert event(ws) == {"type": "done"}
        assert close_code(ws) == 1000
    settle()
    assert counter("voice_stream_errors_total", reason="engine_timeout") == 1
    assert counter("voice_stream_sessions_total", outcome="completed") == 1


def test_a_resumed_stream_continues_the_sample_and_utterance_numbering(live, login_client):
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        answer = ready(ws, resume_from_sample=32000, next_u=3)
        assert (answer["resume_from_sample"], answer["next_u"]) == (32000, 3)
        ws.send_bytes(SPEECH)
        assert event(ws) == {"type": "partial", "u": 3, "text": "kiwi1", "start_sample": 32000, "end_sample": 32640}
    assert live.engine.starts[0]["first_sample"] == 32000 and live.engine.starts[0]["first_u"] == 3


def test_client_stats_are_bounded_clamped_and_rate_limited(live, login_client):
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        ws.send_json({"type": "client_stats", "partial_ms": [-5, 250, 1e12, "x", True] + [100] * 100,
                      "final_ms": [700]})
        ws.send_json({"type": "client_stats", "partial_ms": [100], "final_ms": [100]})  # too soon: ignored
        ws.send_json({"type": "ping", "t": 1})
        assert event(ws)["type"] == "pong"
    # The first 64 entries only; the string and the bool among them skipped.
    assert observations("voice_stream_client_e2e_seconds", event="partial") == 64 - 2
    assert observations("voice_stream_client_e2e_seconds", event="final") == 1
    _counts, total, _n = metrics._hists["voice_stream_client_e2e_seconds"][(("event", "partial"),)]
    assert total == pytest.approx(0.0 + 0.25 + 60.0 + 59 * 0.1), "-5 ms counts as 0, 1e12 ms as 60 s"


# ------------------------------------------------------------ the engine --


def test_an_engine_that_cannot_be_reached_is_4503_and_stood_down(live, login_client):
    live.engine.mode = "refuse"
    alice = login_client("alice")
    sid = session(alice)
    with socket(alice, sid) as ws:
        ws.send_json(start())
        assert refused(ws, "engine_unavailable", 4503)["retryable"] is True
    settle()
    (engine,) = voice_live.health()["engines"]
    assert engine["healthy"] is False and 10 <= engine["stood_down_s"] <= 20, "the first 20 s of the ladder"
    with socket(alice, sid) as ws:
        ws.send_json(start())
        refused(ws, "engine_unavailable", 4503)
    assert len(live.engine.urls) == 1, "an engine standing down is not tried"
    settle()
    assert counter("voice_stream_sessions_total", outcome="engine_unavailable") == 2


def test_an_engine_that_never_answers_times_out_as_4503(live, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_engine_connect_s", 0.3)
    live.engine.mode = "hang"
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ws.send_json(start())
        refused(ws, "engine_unavailable", 4503)
    settle()
    assert counter("voice_stream_errors_total", reason="engine_timeout") == 1
    assert counter("voice_stream_errors_total", reason="engine_unavailable") == 0


def test_a_full_engine_is_capacity_and_is_not_stood_down(live, login_client):
    live.engine.mode = "capacity"
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ws.send_json(start())
        refused(ws, "capacity", 4429)
    settle()
    assert voice_live.health()["engines"][0]["healthy"] is True


def test_an_engine_that_dies_mid_stream_is_4503(live, login_client):
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        ws.send_bytes(SPEECH)
        assert event(ws)["type"] == "partial"
        ws.send_bytes(CRASH)
        refused(ws, "engine_unavailable", 4503)
    settle()
    assert counter("voice_stream_sessions_total", outcome="engine_unavailable") == 1


# ---------------------------------------------------- while it streams --


def test_signing_out_elsewhere_ends_the_stream_4401(live, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_revalidate_s", 0.2)
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        store.revoke_user_sessions(uid("alice"), reason=store.REVOKE_ADMIN)
        refused(ws, "signed_out", 4401)


def test_turning_voice_off_ends_the_stream_4403(live, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_revalidate_s", 0.2)
    root = login_client("root", role="super_admin")
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        root.put(f"/admin/api/members/{uid('alice')}/access", json={"features": {"voice_input": False}})
        refused(ws, "voice_off", 4403)


def test_a_recording_stopped_elsewhere_ends_the_stream_4404(live, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_revalidate_s", 0.2)
    alice = login_client("alice")
    sid = session(alice)
    with socket(alice, sid) as ws:
        ready(ws)
        alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": None, "ended_by": "person"})
        refused(ws, "session_closed", 4404)


def test_a_socket_that_sends_no_audio_is_closed_idle_4408(live, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_idle_s", 0.3)
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        assert refused(ws, "idle", 4408)["retryable"] is True
    settle()
    assert counter("voice_stream_sessions_total", outcome="idle") == 1


def test_a_browser_that_closes_is_counted_and_its_engine_stream_closed(live, login_client):
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        ws.send_bytes(SPEECH)
        event(ws)
        ws.close(1000)
        settle()
    assert counter("voice_stream_sessions_total", outcome="client_closed") == 1
    assert live.engine.streams[0].closed
    assert voice_live.health()["engines"][0]["active"] == 0


# ------------------------------------------------- what it tells anyone --


def test_metrics_start_at_zero_and_keep_closed_labels(live, login_client):
    text = metrics.render()
    for line in (
        "voice_stream_sessions_active 0",
        "voice_stream_sessions_started_total 0",
        'voice_stream_sessions_total{outcome="superseded"} 0',
        'voice_stream_rejections_total{reason="origin"} 0',
        'voice_stream_errors_total{reason="engine_timeout"} 0',
        "voice_stream_audio_received_seconds_total 0",
        "voice_stream_utterances_total 0",
    ):
        assert line in text.splitlines(), line
    assert metrics._buckets_for("voice_stream_final_seconds") == (
        0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 30.0,
    )
    for name in ("voice_stream_first_partial_seconds", "voice_stream_event_lag_seconds",
                 "voice_stream_client_e2e_seconds"):
        assert metrics._buckets_for(name) == metrics._STREAM_BUCKETS
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        assert "voice_stream_sessions_active 1" in metrics.render().splitlines()
    # A session id or a stray value can never become a series.
    metrics.inc_by("voice_stream_rejections_total", 1, reason="user-42", session_id="0" * 32)
    metrics.observe("voice_stream_event_lag_seconds", 0.1, event="typing", user_id="7")
    assert (("reason", "other"),) in metrics._counters["voice_stream_rejections_total"]
    assert (("event", "other"),) in metrics._hists["voice_stream_event_lag_seconds"]
    assert "0" * 32 not in metrics.render() and 'user_id="7"' not in metrics.render()


def test_the_blank_values_compose_passes_mean_the_defaults(monkeypatch):
    """compose.yaml hands every VOICE_LIVE_* key over as `${KEY:-}`, so a
    deployment that sets none of them gets blanks, which must read as the
    defaults. An empty language list would refuse every stream."""
    from app.config import Settings

    for key in ("VOICE_LIVE_ENABLED", "VOICE_LIVE_ENGINE_URLS", "VOICE_LIVE_MAX_STREAMS",
                "VOICE_LIVE_CONNECTS_PER_MIN", "VOICE_LIVE_IDLE_S", "VOICE_LIVE_REVALIDATE_S",
                "VOICE_LIVE_MAX_FRAME_BYTES", "VOICE_LIVE_RESUME_MAX_S", "VOICE_LIVE_ENGINE_CONNECT_S",
                "VOICE_LIVE_LANGUAGES"):
        monkeypatch.setenv(key, "")
    fresh = Settings()
    assert fresh.voice_live_enabled is True
    assert fresh.voice_live_engine_urls == ()
    assert fresh.voice_live_languages == ("auto", "en", "hi")
    assert (fresh.voice_live_max_streams, fresh.voice_live_connects_per_min) == (64, 30)
    assert (fresh.voice_live_idle_s, fresh.voice_live_revalidate_s) == (120.0, 60.0)
    assert (fresh.voice_live_max_frame_bytes, fresh.voice_live_resume_max_s) == (16384, 60.0)
    assert fresh.voice_live_engine_connect_s == 5.0
    monkeypatch.setenv("VOICE_LIVE_LANGUAGES", " EN, hi ,en,, a-very-long-language-tag ")
    assert Settings().voice_live_languages == ("en", "hi")


def test_an_engine_address_names_the_stream_endpoint():
    assert voice_live._stream_url("ws://192.168.9.68:30009") == "ws://192.168.9.68:30009/v1/stream"
    assert voice_live._stream_url("http://engine.test:30009/") == "ws://engine.test:30009/v1/stream"
    assert voice_live._stream_url("https://engine.test") == "wss://engine.test/v1/stream"
    assert voice_live._stream_url("ws://engine.test:1/custom/path") == "ws://engine.test:1/custom/path"


def test_inc_by_ignores_amounts_a_counter_cannot_take(live):
    metrics.inc_by("voice_stream_audio_received_seconds_total", 1.5)
    for bad in (-1.0, float("nan"), float("inf")):
        metrics.inc_by("voice_stream_audio_received_seconds_total", bad)
    assert counter("voice_stream_audio_received_seconds_total") == 1.5


def test_no_transcript_text_cookie_or_token_reaches_the_logs(live, login_client, caplog):
    caplog.set_level(logging.DEBUG)
    alice = login_client("alice")
    with socket(alice, session(alice)) as ws:
        ready(ws)
        for frame in (SPEECH, SPEECH, SILENCE):
            ws.send_bytes(frame)
        while event(ws)["type"] != "final":
            pass
        ws.send_json({"type": "flush"})
        assert event(ws) == {"type": "done"}
    settle()
    assert "live dictation stream closed" in caplog.text
    assert "kiwi" not in caplog.text
    assert alice.cookies.get(settings.auth_cookie_name) not in caplog.text
    assert ENGINE_TOKEN not in caplog.text


def test_the_admin_health_answer_numbers_engines_and_never_names_them(live, login_client):
    root = login_client("root", role="super_admin")
    response = root.get("/audio/health")
    assert response.json()["live"] == {
        "configured": True, "active": 0,
        "engines": [{"index": 0, "healthy": True, "active": 0, "stood_down_s": 0}],
    }
    assert "engine-a.test" not in response.text
