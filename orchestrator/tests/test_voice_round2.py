"""Second hardening round on the recording-session server (2026-09-29).

Each test reproduces a defect an adversarial review found on f94fbcc0 and
fails there; the review's own tests were tests/test_voice_edges_attack.py in
worktree wf_ba7786d0-c32-12.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any, Dict, List

import httpx
import pytest

from app import asr, db, dictation
from app.config import settings
from tests import publicapi_fake_whisper as fw
from tests.test_voice_sessions import (  # noqa: F401  (voice is a fixture)
    all_segments,
    as_segments,
    create,
    parts_of,
    put,
    recording,
    sha,
    voice,
    wait_done,
)


def _source(voice, uid: int, sid: str) -> str:
    return os.path.join(voice.root, str(uid), sid, "source.wav")


# -- A. an acknowledged part must never be cut off the stored file ---------


def test_a_part_stored_between_lease_claim_and_restore_survives(voice, login_client, monkeypatch):
    """_Live.run reads bytes_stored at the lease claim; _restore used to cut
    source.<ext> to that stale number without the part lock, so a part
    stored in between was erased after its 200 and every later part got 503
    ("source is 0 bytes, the row acknowledges 160000")."""
    _script, data = recording(voice.tmp, 40.0, seed=3)
    alice = login_client("alice")
    uid = int(db.get_user_by_username("alice")["id"])
    chunks = parts_of(data)
    real_claim = dictation._claim_lease
    landed: Dict[str, Any] = {}

    def claim_then_a_part_lands(session_id):
        row = real_claim(session_id)
        if row is not None and "row" not in landed:
            landed["row"], _dup = dictation.append_part(uid, session_id, 0, chunks[0], sha(chunks[0]))
        return row

    monkeypatch.setattr(dictation, "_claim_lease", claim_then_a_part_lands)
    sid = create(alice).json()["session_id"]
    deadline = time.monotonic() + 10
    while "row" not in landed:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    time.sleep(1.0)
    acknowledged = int(landed["row"]["bytes_stored"])
    assert os.path.getsize(_source(voice, uid, sid)) == acknowledged
    assert put(alice, sid, 1, chunks[1]).status_code == 200
    r = alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": 1, "ended_by": "person"})
    assert r.status_code in (200, 202), r.text
    done = wait_done(alice, sid, timeout=60)
    assert done["status"] == "done", done
    assert os.path.getsize(_source(voice, uid, sid)) == len(chunks[0]) + len(chunks[1])


def test_a_stored_file_shorter_than_its_row_ends_instead_of_spinning(voice, login_client):
    """If the file ever holds less than the row acknowledges, _ingest read an
    empty chunk, slept 0.2 s and looped for ever without re-reading the row:
    the session stayed 'finishing', held a VOICE_SESSION_MAX_ACTIVE slot and
    resumed the loop after every restart."""
    _script, data = recording(voice.tmp, 20.0, seed=5)
    alice = login_client("alice")
    uid = int(db.get_user_by_username("alice")["id"])
    chunks = parts_of(data)
    sid = create(alice).json()["session_id"]
    assert put(alice, sid, 0, chunks[0]).status_code == 200
    dictation.RUNNER.stop_all()
    # The loss the review produced, made directly: the file keeps the first
    # half of what its row acknowledges.
    with db.connection() as con:
        con.execute("UPDATE voice_sessions SET status = 'finishing', ended_by = 'person' WHERE id = %s", (sid,))
    with open(_source(voice, uid, sid), "r+b") as fh:
        fh.truncate(len(chunks[0]) // 2)
    dictation.RUNNER.ensure(sid)
    t0 = time.monotonic()
    done = wait_done(alice, sid, timeout=30)
    assert done["status"] in ("done", "failed"), done
    assert time.monotonic() - t0 < 20
    with db.connection() as con:
        live = con.execute(
            "SELECT count(*) AS n FROM voice_sessions WHERE id = %s AND status IN ('recording','finishing')", (sid,)
        ).fetchone()["n"]
    assert live == 0


# -- C. a decoder that dies part way ---------------------------------------


def _dying_decoder(monkeypatch, *, after_bytes: int, times: int) -> Dict[str, int]:
    """The WAV passthrough stands in for ffmpeg: the first `times` decoders
    stop (as a killed ffmpeg does: `dead`, no more PCM) after `after_bytes`."""
    real_feed = dictation._WavPassthrough.feed
    died = {"n": 0}

    async def feed(self, data):
        if getattr(self, "_doomed", None) is None:
            self._doomed = died["n"] < times
            self._seen = 0
        if self._doomed and self._seen + len(data) > after_bytes:
            if not self.dead:
                died["n"] += 1
            self.dead = True
            return
        self._seen += len(data)
        await real_feed(self, data)

    monkeypatch.setattr(dictation._WavPassthrough, "feed", feed)
    return died


def _record_90s(voice, alice, monkeypatch):
    """The whole recording is stored before its worker starts, so a decoder
    that closes the recording cannot refuse the later parts."""
    real = dictation._Runner._ensure_in_loop
    hold = {"on": True}
    monkeypatch.setattr(
        dictation._Runner, "_ensure_in_loop", lambda self, sid: None if hold["on"] else real(self, sid)
    )
    script, data = recording(voice.tmp, 90.0, seed=12)
    sid = create(alice).json()["session_id"]
    chunks = parts_of(data)
    for seq, part in enumerate(chunks):
        assert put(alice, sid, seq, part).status_code == 200
    r = alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": len(chunks) - 1, "ended_by": "person"})
    assert r.status_code in (200, 202), r.text
    hold["on"] = False
    dictation.RUNNER.ensure(sid)
    return script, sid, len(data)


def test_a_decoder_killed_part_way_is_started_again_and_nothing_is_lost(voice, login_client, monkeypatch):
    """ffmpeg killed after a third of a 90 s recording (the OOM killer, a
    crash) was reported 'transcribed' with gaps [] while about two thirds of
    the words were missing. A decoder death says nothing about the audio: it
    is started again from byte 0 and the whole recording is transcribed."""
    died = _dying_decoder(monkeypatch, after_bytes=960_000, times=1)
    alice = login_client("alice")
    script, sid, _size = _record_90s(voice, alice, monkeypatch)
    done = wait_done(alice, sid, timeout=120)
    assert died["n"] == 1
    verdict = fw.compare_to_script(as_segments(all_segments(alice, sid)), script)
    assert done["outcome"] == "transcribed", done
    assert verdict["missing"] == 0, verdict
    assert done["audio_ms"] >= 89_000


def test_a_decoder_that_keeps_dying_reports_the_unread_rest_as_a_gap(voice, login_client, monkeypatch):
    """A decoder that stops at the same place every time: what it could not
    read is a gap, and the outcome is not 'transcribed'."""
    died = _dying_decoder(monkeypatch, after_bytes=960_000, times=99)
    alice = login_client("alice")
    _script, sid, _size = _record_90s(voice, alice, monkeypatch)
    done = wait_done(alice, sid, timeout=120)
    assert died["n"] >= 2  # started again, and stopped again
    assert done["outcome"] == "transcribed_with_gaps", done
    unread = [g for g in done["gaps"] if g["reason"] == "undecodable"]
    assert len(unread) == 1, done["gaps"]
    assert unread[0]["start_ms"] <= 31_000
    assert unread[0]["end_ms"] >= 80_000, unread
    assert (done["error"] or {}).get("reason") == "undecodable"
    # "Retranscribe the gaps" is not a no-op for it.
    r = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "gaps"})
    assert r.status_code == 202, r.text


# -- B. a timed-out window is never sent again; a hung engine opens a breaker


class _RealEngineTransport(httpx.AsyncBaseTransport):
    """The fleet's transport, plus what the real speech server does and the
    in-process ASGI one does not: after a client read timeout the server
    keeps decoding the clip it already has (asr.ASRTimeout)."""

    def __init__(self, fleet: fw.Fleet) -> None:
        self.fleet = fleet
        self.zombies: List[asyncio.Task] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        name = (request.url.host or "").split(".")[0]
        fake = self.fleet.replicas.get(name)
        if fake is None or fake.down:
            raise httpx.ConnectError("connection refused", request=request)
        read = (request.extensions.get("timeout") or {}).get("read")
        await request.aread()
        task = asyncio.ensure_future(fake.transport.handle_async_request(request))
        done, _ = await asyncio.wait({task}, timeout=read)
        if not done:
            self.zombies.append(task)  # the engine keeps decoding it
            raise httpx.ReadTimeout("read timed out", request=request)
        response = task.result()
        await response.aread()
        return response


@pytest.fixture()
def two_replicas(voice, monkeypatch):
    fleet = fw.Fleet(names=("asr-worker", "asr-head"))  # production order
    transport = _RealEngineTransport(fleet)
    base = httpx._client.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return base(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(settings, "asr_base_urls", fleet.urls())
    asr.set_provider(asr.RoutedProvider([
        asr.VLLMAudioProvider(base_url=url, model="whisper-test", name="whisper") for url in fleet.urls()
    ]))
    yield fleet, transport
    dictation.RUNNER.stop_all()
    asr.SESSION_GATE.reset_for_tests()  # an open breaker must not reach the next test


def _store_and_finish(alice, data):
    sid = create(alice).json()["session_id"]
    chunks = parts_of(data)
    for seq, part in enumerate(chunks):
        assert put(alice, sid, seq, part).status_code == 200
    r = alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": len(chunks) - 1, "ended_by": "person"})
    assert r.status_code in (200, 202), r.text
    return sid


def test_a_timed_out_window_is_not_sent_again(voice, two_replicas, login_client, monkeypatch):
    """_Live._transcribe caught ASRTimeout as ASRUnavailable and re-sent the
    window, which went to the OTHER replica while the first still decoded
    it. Scaled: timeout 1 s, the slow replica 3 s."""
    from app.video import transcribe as video_transcribe

    fleet, _transport = two_replicas
    monkeypatch.setattr(video_transcribe, "_backoff_s", lambda attempt: 0.05)
    monkeypatch.setattr(settings, "voice_session_window_timeout_s", 1.0)
    fleet["asr-worker"].latency_s = 3.0
    _script, data = recording(voice.tmp, 40.0, seed=4)
    alice = login_client("alice")
    sid = _store_and_finish(alice, data)
    done = wait_done(alice, sid, timeout=60)
    time.sleep(3.5)  # let the abandoned decode finish
    sent: Dict[int, List[str]] = {}
    for name, fake in fleet.replicas.items():
        for call in fake.calls:
            sent.setdefault(call["bytes"], []).append(name)
    assert not {k: v for k, v in sent.items() if len(v) > 1}, sent
    assert done["outcome"] == "transcribed_with_gaps", done
    assert [g["reason"] for g in done["gaps"]].count("engine_timeout") == 1, done["gaps"]


def test_a_hung_engine_opens_the_breaker_instead_of_costing_every_window_a_timeout(
    voice, two_replicas, login_client, monkeypatch,
):
    """Both replicas accept the clip and never answer. Each window cost
    _WINDOW_ATTEMPTS x VOICE_SESSION_WINDOW_TIMEOUT_S (1.99 s per window at a
    0.5 s timeout, about 480 s at the production 120 s, 20 h for an hour).
    Now two timeouts open the breaker and the rest are gaps at once."""
    from app.video import transcribe as video_transcribe

    fleet, _transport = two_replicas
    monkeypatch.setattr(video_transcribe, "_backoff_s", lambda attempt: 0.0)
    monkeypatch.setattr(settings, "voice_session_window_timeout_s", 0.5)
    for fake in fleet.replicas.values():
        fake.hang_calls = set(range(1, 10_000))
    _script, data = recording(voice.tmp, 120.0, seed=6)
    alice = login_client("alice")
    t0 = time.monotonic()
    sid = _store_and_finish(alice, data)
    done = wait_done(alice, sid, timeout=120)
    elapsed = time.monotonic() - t0
    uid = int(db.get_user_by_username("alice")["id"])
    windows = [w for w in dictation._read_lines(os.path.join(voice.root, str(uid), sid, "plan.jsonl")) if "start_s" in w]
    calls = sum(len(f.calls) for f in fleet.replicas.values())
    assert len(windows) >= 4
    assert calls == asr._BREAKER_TIMEOUTS, (calls, len(windows))
    assert elapsed / len(windows) < 2 * 0.5, elapsed
    assert done["status"] == "failed" and done["outcome"] == "engine_unavailable", done
    assert asr.SESSION_GATE.engine_state() == "unavailable"


# -- E. VOICE_SESSION_MAX_ACTIVE holds under simultaneous creates -----------


def test_simultaneous_creates_cannot_exceed_voice_session_max_active(voice, login_client, monkeypatch):
    """The count and the insert were two transactions: with max_active=1, two
    simultaneous creates were both admitted in 23 of 25 trials."""
    monkeypatch.setattr(settings, "voice_session_max_active", 1)
    clients = [login_client(f"user{i}") for i in range(2)]
    over = 0
    for _trial in range(15):
        with db.connection() as con:
            con.execute("UPDATE voice_sessions SET status = 'cancelled' WHERE status IN ('recording','finishing')")
        barrier = threading.Barrier(2)
        codes = [0, 0]

        def go(i):
            barrier.wait()
            codes[i] = create(clients[i]).status_code

        threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(codes) in ([201, 503], [201, 201]), codes
        over += codes == [201, 201]
    assert over == 0


def test_a_retried_create_is_answered_even_at_the_ceiling(voice, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_session_max_active", 1)
    alice = login_client("alice")
    key = "5b0e3c8e-2f7a-4c55-9d1e-3f1a2b4c5d6e"
    first = create(alice, client_key=key)
    assert first.status_code == 201, first.text
    again = create(alice, client_key=key)
    assert again.status_code in (200, 201), again.text
    assert again.json()["session_id"] == first.json()["session_id"]
    assert create(login_client("bob")).status_code == 503


# -- D. noise windows: asked with the gate, judged per VAD-speech second ----


class _RealEngine:
    """What compose/whisper/server.py answers, measured live 2026-09-29: with
    the gate OFF no_speech_prob is 0.0 for everything; with it ON it is the
    real number (0.0525 for pink noise at -30 dBFS, which passes the gate).
    Every window here is room noise that decodes as a stock phrase."""

    name = "whisper"
    model = "whisper-test"

    def __init__(self, *, gated_nsp: float = 0.0525, gate_empties: bool = False, words: str = "Thank you for watching."):
        self.gated_nsp = gated_nsp
        self.gate_empties = gate_empties
        self.words = words
        self.asked: List[bool] = []

    async def transcribe_window(self, audio, *, filename, content_type, timeout_s=None, no_speech_check=False):
        self.asked.append(bool(no_speech_check))
        seconds = (len(audio) - 44) / (2 * 16000)
        nsp = self.gated_nsp if no_speech_check else 0.0
        if no_speech_check and self.gate_empties:
            segments = ()
        else:
            segments = ({"start": 0.0, "end": min(seconds, 29.98), "text": self.words, "language": "en"},)
        transcript = asr.TranscriptSegments(
            text=" ".join(s["text"] for s in segments), language="English", language_code="en",
            provider="whisper", model="whisper-test", engine_ms=5, segments=segments,
        )
        return asr.WindowReply(transcript, nsp)

    async def health(self):
        return True


def test_a_noise_window_that_decodes_as_a_stock_phrase_is_dropped(voice, login_client):
    """Live, window 108 of an hour (11 s of pink noise) came back "Thank you
    for watching." and stayed in the transcript: the session asked with the
    gate off, where the engine reports no_speech_prob 0.0, so no rule fired."""
    engine = _RealEngine()
    asr.set_provider(engine)
    _script, data = recording(voice.tmp, 40.0, seed=31)
    alice = login_client("alice")
    sid = _store_and_finish(alice, data)
    done = wait_done(alice, sid, timeout=60)
    assert "watching" not in (done["text"] or "").lower(), done
    assert done["gaps"] and all(g["reason"] == "dropped_as_noise" for g in done["gaps"]), done
    assert engine.asked and engine.asked[0] is True, "each window is asked WITH the gate first"


def test_a_window_the_gate_empties_is_asked_again_and_dense_speech_is_kept(voice, login_client):
    """The first-30-s gate emptied real dictation that opened quietly
    (2026-09-24); a gated window VAD heard speech in is asked again without
    the gate, and words as dense as speech stand."""
    words = " ".join(["alpha bravo charlie delta echo foxtrot golf hotel"] * 12) + "."
    engine = _RealEngine(gated_nsp=0.83, gate_empties=True, words=words)
    asr.set_provider(engine)
    _script, data = recording(voice.tmp, 40.0, seed=32)
    alice = login_client("alice")
    sid = _store_and_finish(alice, data)
    done = wait_done(alice, sid, timeout=60)
    assert True in engine.asked and False in engine.asked, engine.asked
    assert done["outcome"] == "transcribed", done
    assert "foxtrot" in done["text"]
