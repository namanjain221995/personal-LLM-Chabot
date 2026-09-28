"""Recording sessions: chunked, stored dictation with no length limit (2026-09-29).

The owner's complaint was a long dictation answered with "That recording
wasn't clear enough to transcribe. Try again, closer to the microphone." The
measurement found why: the speech engine judges only the FIRST 30 s of a clip
against its silence gate, so a recording that opens with a pause came back
empty whole (the owner's own 181,427 ms dictation of 2026-09-24, empty in
2,175 ms). These tests hold the replacement to the four things the owner then
asked for, in their words "record ... and store ... as it give transcript as
long as i talk More then 1 hr":

  * a recording whose first 30 s are quiet returns every word said after it,
    and every engine request goes with the gate OFF;
  * the recording is STORED, byte for byte, 0600, and only its owner (and a
    super admin, audited) can read it;
  * the transcript grows while the recording is still being made;
  * two hours transcribe word for word, with no ceiling anywhere and memory
    flat in duration.

THE ENGINE is tests/publicapi_fake_whisper.FakeWhisper: the speech server's
contract, including the first-30-s gate and one clip at a time, over a
synthetic recording whose words it can actually hear. It is reached through an
in-process transport. The recordings are 16 kHz mono WAV because the CI host
has no ffmpeg, and a WAV of exactly that shape is decoded without one
(dictation._WavPassthrough).
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

import httpx
import numpy as np
import pytest

from app import asr, audio_api, db, dictation
from app.config import settings
from tests import publicapi_fake_whisper as fw

SR = 16000
#: 5 s of 16 kHz mono PCM: the part the browser sends at the default timeslice.
PART_BYTES = 5 * SR * 2


# ------------------------------------------------------------ recordings --


def shifted(script: fw.Script, by_s: float) -> fw.Script:
    """The same speech, starting `by_s` later: a quiet opening."""
    words = [fw.Word(w.position, w.vocab, round(w.start_s + by_s, 4), round(w.end_s + by_s, 4)) for w in script.words]
    return fw.Script(seconds=script.seconds + by_s, words=words, continuous=[(a + by_s, b + by_s) for a, b in script.continuous])


def pink_noise(samples: int, dbfs: float, seed: int = 5) -> np.ndarray:
    """Pink (1/f) noise at `dbfs` RMS, as float in [-1, 1]."""
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(samples)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(samples, 1.0 / SR)
    freqs[0] = freqs[1]
    pink = np.fft.irfft(spectrum / np.sqrt(freqs), n=samples)
    pink /= np.sqrt(np.mean(pink * pink))
    return pink * (10 ** (dbfs / 20.0))


def recording(tmp_path, speech_s: float, *, quiet_s: float = 0.0, opening: str = "room", seed: int = 7) -> Tuple[fw.Script, bytes]:
    """(script, WAV bytes): `quiet_s` of `opening` (room = the generator's
    -59 dBFS floor, digital = zeros, pink = pink noise at -50 dBFS), then
    `speech_s` of synthetic speech."""
    script = fw.build_script(speech_s, seed=seed)
    if quiet_s:
        script = shifted(script, quiet_s)
    path = str(tmp_path / f"rec-{speech_s}-{quiet_s}-{opening}-{seed}.wav")
    fw.write_speech_wav(path, script, seed=seed * 13)
    with open(path, "rb") as fh:
        data = bytearray(fh.read())
    if quiet_s and opening != "room":
        n = int(quiet_s * SR)
        if opening == "digital":
            quiet = np.zeros(n, dtype="<i2")
        else:
            quiet = np.clip(pink_noise(n, -50.0) * 32767.0, -32768, 32767).astype("<i2")
        data[44:44 + 2 * n] = quiet.tobytes()
    return script, bytes(data)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------- fixtures --


@pytest.fixture()
def voice(tmp_path, monkeypatch):
    """A deployment with recording sessions, a one-replica fake engine that
    records whether each request asked for its silence gate, and clean state."""
    fleet = fw.Fleet(names=("asr-worker",))
    fake = fleet["asr-worker"]
    gate_flags: List[bool] = []
    original = fake._transcribe

    async def recorded(file, language, response_format, no_speech_check):
        gate_flags.append(bool(no_speech_check))
        return await original(file, language, response_format, no_speech_check)

    fake._transcribe = recorded
    real_client = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = fleet.transport()
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(settings, "asr_base_urls", fleet.urls())
    monkeypatch.setattr(settings, "voice_sessions_enabled", True)
    monkeypatch.setattr(settings, "voice_data_dir", str(tmp_path / "voice"))
    monkeypatch.setattr(settings, "voice_min_free_bytes", 0)
    monkeypatch.setattr(settings, "voice_retention_days", 0)
    # The suite uploads far faster than anyone speaks; the limits have their own test.
    monkeypatch.setattr(settings, "voice_part_per_min", 1_000_000)
    monkeypatch.setattr(settings, "voice_session_create_per_min", 1_000)
    audio_api.reset_for_tests()
    with db.connection() as con:
        con.execute("TRUNCATE TABLE voice_sessions, voice_transcriptions RESTART IDENTITY")
    provider = asr.RoutedProvider([
        asr.VLLMAudioProvider(base_url=url, model="whisper-test", name="whisper") for url in fleet.urls()
    ])
    asr.set_provider(provider)
    yield SimpleNamespace(fleet=fleet, fake=fake, gate_flags=gate_flags, root=str(tmp_path / "voice"), tmp=tmp_path)
    audio_api.reset_for_tests()
    asr.set_provider(None)


def create(client, *, mime: str = "audio/wav", client_key: Optional[str] = None):
    return client.post("/audio/sessions", json={"client_key": client_key or str(uuid.uuid4()), "mime_type": mime, "part_ms": 5000})


def put(client, sid: str, seq: int, part: bytes, *, mime: str = "audio/wav", cursor: int = 0, digest: Optional[str] = None):
    return client.put(
        f"/audio/sessions/{sid}/parts/{seq}",
        params={"cursor": cursor},
        content=part,
        headers={"content-type": mime, "x-part-sha256": digest or sha(part)},
    )


def parts_of(data: bytes, size: int = PART_BYTES) -> List[bytes]:
    return [data[i:i + size] for i in range(0, len(data), size)]


def iter_parts(data: bytes, size: int = PART_BYTES):
    """One part at a time: a two-hour list of slices would be 230 MB of the
    TEST's memory inside the measurement of the server's."""
    for i in range(0, len(data), size):
        yield data[i:i + size]


def wait_done(client, sid: str, *, timeout: float = 180.0) -> Dict[str, Any]:
    deadline = time.monotonic() + timeout
    rev = -1
    while time.monotonic() < deadline:
        r = client.get(f"/audio/sessions/{sid}", params={"since_rev": rev, "wait_s": 5})
        assert r.status_code == 200, r.text
        body = r.json()
        if body["status"] in ("done", "failed"):
            return body
        rev = body["rev"]
    raise AssertionError(f"session {sid} did not finish in {timeout}s")


def dictate(client, data: bytes, *, mime: str = "audio/wav", size: int = PART_BYTES) -> Tuple[str, Dict[str, Any], List[int]]:
    """Record a whole WAV as parts, press Stop, wait for the transcript."""
    r = create(client, mime=mime)
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    statuses = []
    for seq, part in enumerate(iter_parts(data, size)):
        resp = put(client, sid, seq, part, mime=mime)
        statuses.append(resp.status_code)
        assert resp.status_code == 200, resp.text
    r = client.post(f"/audio/sessions/{sid}/finish", json={"last_part": len(statuses) - 1, "ended_by": "person", "duration_ms": 0})
    assert r.status_code == 202, r.text
    return sid, wait_done(client, sid), statuses


def all_segments(client, sid: str) -> List[Dict[str, Any]]:
    return client.get(f"/audio/sessions/{sid}", params={"cursor": 0}).json()["segments"]


def as_segments(items: List[Dict[str, Any]]):
    return [SimpleNamespace(text=s["text"], start_s=s["start_ms"] / 1000.0, end_s=s["end_ms"] / 1000.0) for s in items]


# ------------------------------------------------- the owner's recording --


@pytest.mark.parametrize("opening", ["digital", "pink"])
def test_a_recording_whose_first_30_seconds_are_quiet_returns_every_word(voice, login_client, opening):
    """THE OWNER'S ERROR, and the proof it is gone.

    150 s: 30 s of quiet (digital silence, or pink noise at -50 dBFS), then
    120 s of speech. The legacy path sends it whole; the engine judges the
    quiet opening, empties the clip, and at 150 s (> the 120 s retry bound)
    nobody asks again: empty, 'unclear', the microphone sentence. The session
    sends only what voice-activity detection found speech in, with the gate
    off, and must return every word, at its time.
    """
    script, data = recording(voice.tmp, 120.0, quiet_s=30.0, opening=opening)
    alice = login_client("alice")

    legacy = alice.post(
        "/audio/transcribe", content=data, headers={"content-type": "audio/wav"},
        params={"duration_ms": "150000", "language": "auto"},
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["text"] == "" and legacy.json()["confidence"] == "unclear", legacy.json()
    legacy_calls = len(voice.gate_flags)
    assert voice.gate_flags == [True], "the legacy path asks for the gate, and was not retried"

    sid, done, _ = dictate(alice, data)
    assert done["status"] == "done" and done["outcome"] == "transcribed", done
    verdict = fw.compare_to_script(as_segments(all_segments(alice, sid)), script)
    assert verdict["equal"], verdict
    assert verdict["max_start_error_s"] <= 0.25 and verdict["max_end_error_s"] <= 0.25, verdict
    assert done["text"].split()[0].lower().strip(".") == script.words[0].text
    print(json.dumps({"opening": opening, "words": verdict["words"], "windows": len(voice.fake.calls) - legacy_calls, "clip_s": [round(c["seconds"], 1) for c in voice.fake.calls[legacy_calls:]], "first_start": all_segments(alice, sid)[0]["start_ms"], "outcome": done["outcome"], "speech_ms": done["speech_ms"], "audio_ms": done["audio_ms"]}))
    session_calls = voice.gate_flags[legacy_calls:]
    assert session_calls and not any(session_calls), "every session window goes with the gate OFF"
    # Nothing from the quiet opening was sent: the first clip starts on speech.
    assert all(call["seconds"] <= settings.voice_session_window_max_s + 0.01 for call in voice.fake.calls[legacy_calls:])


def test_the_transcript_grows_while_the_recording_is_still_being_made(voice, login_client):
    """Progressive: segments arrive on the part responses and the long-poll
    BEFORE Stop, append-only, and the cursor only moves forward."""
    script, data = recording(voice.tmp, 90.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    held: List[Dict[str, Any]] = []
    cursor = 0
    chunks = parts_of(data)
    for seq, part in enumerate(chunks[: len(chunks) * 2 // 3]):
        body = put(alice, sid, seq, part, cursor=cursor).json()
        assert body["cursor"] >= cursor
        held.extend(body["segments"])
        cursor = body["cursor"]
    deadline = time.monotonic() + 30
    rev = -1
    body = {"status": "recording"}
    while not held and time.monotonic() < deadline:
        body = alice.get(f"/audio/sessions/{sid}", params={"cursor": cursor, "since_rev": rev, "wait_s": 2}).json()
        held.extend(body["segments"])
        cursor, rev = body["cursor"], body["rev"]
    assert held, "text must appear while the person is still talking"
    assert body["status"] == "recording"
    assert [s["i"] for s in held] == list(range(len(held)))
    for seq, part in list(enumerate(chunks))[len(chunks) * 2 // 3:]:
        assert put(alice, sid, seq, part).status_code == 200
    alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": len(chunks) - 1, "ended_by": "person"})
    done = wait_done(alice, sid)
    final = all_segments(alice, sid)
    assert final[: len(held)] == held, "released text is never taken back"
    assert fw.compare_to_script(as_segments(final), script)["equal"]
    assert done["backlog_ms"] == 0 and done["transcribed_ms"] == done["audio_ms"] == 90_000


def test_the_recording_is_stored_byte_for_byte_private_and_downloadable_by_its_owner_only(voice, login_client):
    _script, data = recording(voice.tmp, 20.0)
    alice = login_client("alice")
    bob = login_client("bob")
    sid, done, _ = dictate(alice, data)
    uid = int(db.get_user_by_username("alice")["id"])
    folder = os.path.join(voice.root, str(uid), sid)
    source = os.path.join(folder, "source.wav")
    with open(source, "rb") as fh:
        assert fh.read() == data, "stored exactly as the browser encoded it"
    assert stat.S_IMODE(os.stat(source).st_mode) == 0o600
    for directory in (voice.root, os.path.dirname(folder), folder):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700, directory
    for name in os.listdir(folder):
        assert stat.S_IMODE(os.stat(os.path.join(folder, name)).st_mode) == 0o600, name
    assert "audio.pcm" not in os.listdir(folder), "the derived PCM goes when the session ends"
    assert set(os.listdir(folder)) >= {
        "source.wav", "parts.jsonl", "plan.jsonl", "results.jsonl", "transcript.json", "transcript.txt",
    }
    with open(os.path.join(folder, "transcript.txt"), encoding="utf-8") as fh:
        assert fh.read().strip() == done["text"]
    with db.connection() as con:
        row = con.execute("SELECT source_sha256, bytes_stored FROM voice_sessions WHERE id = %s", (sid,)).fetchone()
    assert row["source_sha256"] == sha(data) and row["bytes_stored"] == len(data)
    assert done["stored"] == {"kept": True, "retention_days": 0, "delete_after": None, "bytes": len(data)}

    got = alice.get(f"/audio/sessions/{sid}/audio")
    assert got.status_code == 200 and got.content == data
    assert got.headers["cache-control"] == "no-store"
    assert got.headers["x-recording-complete"] == "true"
    assert got.headers["content-disposition"].startswith("attachment;")
    ranged = alice.get(f"/audio/sessions/{sid}/audio", headers={"range": "bytes=0-99"})
    assert ranged.status_code == 206 and ranged.content == data[:100]
    for route in (f"/audio/sessions/{sid}/audio", f"/audio/sessions/{sid}"):
        stranger = bob.get(route)
        assert stranger.status_code == 404 and stranger.json()["reason"] == "not_found"
    listing = alice.get("/audio/sessions").json()["sessions"]
    assert [s["session_id"] for s in listing] == [sid] and listing[0]["preview"]
    assert bob.get("/audio/sessions").json()["sessions"] == []


def test_a_finished_session_writes_one_v19_row_so_the_console_keeps_counting(voice, login_client):
    _script, data = recording(voice.tmp, 15.0)
    alice = login_client("alice")
    _sid, done, _ = dictate(alice, data)
    with db.connection() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM voice_transcriptions").fetchall()]
    assert len(rows) == 1
    assert rows[0]["status"] == "ok" and rows[0]["duration_ms"] == done["audio_ms"] == 15_000
    assert rows[0]["language"] == "English"


def test_create_is_idempotent_and_one_person_has_one_recording_at_a_time(voice, login_client):
    alice = login_client("alice")
    key = str(uuid.uuid4())
    first = create(alice, client_key=key)
    assert first.status_code == 201
    body = first.json()
    assert body["config"]["part_ms"] == 5000
    assert body["config"]["part_limit_bytes"] == 1024 * 1024
    assert body["config"]["long_poll_max_s"] == 25
    sid = body["session_id"]
    assert put(alice, sid, 0, b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 100).status_code == 200
    again = create(alice, client_key=key)
    assert again.status_code == 200 and again.json()["session_id"] == sid
    assert again.json()["next_part"] == 1, "a reloaded tab resumes where the server is"
    other = create(alice)
    assert other.status_code == 409
    assert other.json()["reason"] == "session_active" and other.json()["session_id"] == sid
    assert create(login_client("bob")).status_code == 201, "another person is not affected"


@pytest.mark.parametrize(
    "setup, payload, status, reason",
    [
        ({}, {"client_key": "not-a-uuid", "mime_type": "audio/webm"}, 400, "bad_request"),
        ({}, {"client_key": str(uuid.uuid4()), "mime_type": "text/plain"}, 415, "unsupported_format"),
        ({"voice_sessions_enabled": False}, None, 404, "sessions_off"),
        ({"asr_enabled": False}, None, 404, "voice_unavailable"),
        ({"voice_session_max_active": 1}, None, 503, "capacity_full"),
        ({"voice_min_free_bytes": 1 << 62}, None, 507, "storage_full"),
    ],
)
def test_create_refusals_are_flat_and_closed(voice, login_client, monkeypatch, setup, payload, status, reason):
    if setup.get("voice_session_max_active") == 1:
        monkeypatch.setattr(settings, "voice_session_max_active", 1)
        assert create(login_client("carol")).status_code == 201
    for key, value in setup.items():
        monkeypatch.setattr(settings, key, value)
    alice = login_client("alice")
    body = payload or {"client_key": str(uuid.uuid4()), "mime_type": "audio/webm;codecs=opus"}
    r = alice.post("/audio/sessions", json=body)
    assert r.status_code == status, r.text
    assert r.json()["reason"] == reason and isinstance(r.json()["detail"], str)


def test_a_member_whose_voice_input_is_off_gets_voice_off(voice, login_client):
    root = login_client("root", role="super_admin")
    bob = login_client("bob")
    uid = int(db.get_user_by_username("bob")["id"])
    root.put(f"/admin/api/members/{uid}/access", json={"features": {"voice_input": False}})
    r = create(bob)
    assert r.status_code == 403 and r.json()["reason"] == "voice_off"


def test_the_part_protocol_says_what_happened_and_never_stores_a_part_twice(voice, login_client, monkeypatch):
    _script, data = recording(voice.tmp, 12.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    chunks = parts_of(data)
    assert put(alice, sid, 0, chunks[0]).status_code == 200
    replay = put(alice, sid, 0, chunks[0])
    assert replay.status_code == 200 and replay.json()["duplicate"] is True
    ahead = put(alice, sid, 2, chunks[2])
    assert ahead.status_code == 409 and ahead.json()["reason"] == "out_of_order"
    assert ahead.json()["next_part"] == 1
    conflict = put(alice, sid, 0, chunks[1])
    assert conflict.status_code == 409 and conflict.json()["reason"] == "part_conflict"
    corrupt = put(alice, sid, 1, chunks[1], digest=sha(b"something else"))
    assert corrupt.status_code == 422 and corrupt.json()["reason"] == "part_corrupt"
    assert put(alice, sid, 1, chunks[1], digest="abc").json()["reason"] == "bad_request"
    monkeypatch.setattr(settings, "voice_part_max_bytes", 64 * 1024)
    big = put(alice, sid, 1, chunks[1])
    assert big.status_code == 413 and big.json()["reason"] == "part_too_large"
    monkeypatch.setattr(settings, "voice_part_max_bytes", 1024 * 1024)
    assert put(alice, sid, 1, chunks[1]).status_code == 200
    missing = alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": 2, "ended_by": "person"})
    assert missing.status_code == 409 and missing.json()["reason"] == "parts_missing"
    assert missing.json()["next_part"] == 2
    assert put(alice, sid, 2, chunks[2]).status_code == 200
    first = alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": 2, "ended_by": "person"})
    assert first.status_code == 202
    repeat = alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": 2, "ended_by": "person"})
    assert repeat.status_code == 200
    closed = put(alice, sid, 3, b"late")
    assert closed.status_code == 409 and closed.json()["reason"] == "session_closed"
    wait_done(alice, sid)
    uid = int(db.get_user_by_username("alice")["id"])
    with open(os.path.join(voice.root, str(uid), sid, "source.wav"), "rb") as fh:
        assert fh.read() == data[: 3 * PART_BYTES]


def test_a_first_part_that_is_not_the_declared_format_is_refused_and_nothing_kept(voice, login_client):
    alice = login_client("alice")
    sid = create(alice, mime="audio/webm;codecs=opus").json()["session_id"]
    r = put(alice, sid, 0, b"RIFF\x00\x00\x00\x00WAVEnot webm at all", mime="audio/webm")
    assert r.status_code == 415 and r.json()["reason"] == "unsupported_format"
    assert alice.get(f"/audio/sessions/{sid}").status_code == 404
    uid = int(db.get_user_by_username("alice")["id"])
    assert not os.path.exists(os.path.join(voice.root, str(uid), sid))


def test_lost_part_responses_are_replays_and_the_stored_audio_is_identical(voice, login_client):
    """One response in ten 'lost': the browser sends that part again; the
    server recognises it, and the recording is byte-identical to a clean run."""
    script, data = recording(voice.tmp, 60.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    chunks = parts_of(data)
    duplicates = 0
    for seq, part in enumerate(chunks):
        assert put(alice, sid, seq, part).status_code == 200
        if seq % 10 == 9:
            again = put(alice, sid, seq, part)
            assert again.status_code == 200
            duplicates += again.json()["duplicate"]
    assert duplicates == len(chunks) // 10
    alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": len(chunks) - 1, "ended_by": "person"})
    done = wait_done(alice, sid)
    uid = int(db.get_user_by_username("alice")["id"])
    with open(os.path.join(voice.root, str(uid), sid, "source.wav"), "rb") as fh:
        assert fh.read() == data
    assert done["outcome"] == "transcribed"
    assert fw.compare_to_script(as_segments(all_segments(alice, sid)), script)["equal"]


def test_a_process_that_dies_mid_session_is_replaced_and_nothing_is_lost(voice, login_client):
    """Kill the worker mid-recording (no finish, no cleanup), leave an
    unacknowledged tail on the file as a crash between write and row would,
    let the lease lapse: the next request adopts the session, the recording
    continues, and every word comes back."""
    script, data = recording(voice.tmp, 100.0, seed=11)
    alice = login_client("alice")
    uid = int(db.get_user_by_username("alice")["id"])
    sid = create(alice).json()["session_id"]
    chunks = parts_of(data)
    half = len(chunks) // 2
    for seq, part in enumerate(chunks[:half]):
        assert put(alice, sid, seq, part).status_code == 200
    results = os.path.join(voice.root, str(uid), sid, "results.jsonl")
    deadline = time.monotonic() + 30
    while not os.path.exists(results):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    dictation.RUNNER.stop_all()  # the process dies
    source = os.path.join(voice.root, str(uid), sid, "source.wav")
    with open(source, "ab") as fh:
        fh.write(b"\x00" * 1234)  # written, never acknowledged
    with db.connection() as con:
        con.execute("UPDATE voice_sessions SET lease_expires_at = now() - interval '1 minute' WHERE id = %s", (sid,))
    for seq, part in list(enumerate(chunks))[half:]:
        assert put(alice, sid, seq, part).status_code == 200, seq
    alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": len(chunks) - 1, "ended_by": "person"})
    done = wait_done(alice, sid)
    assert done["outcome"] == "transcribed", done
    with open(source, "rb") as fh:
        assert fh.read() == data, "the unacknowledged tail was cut before the next part"
    verdict = fw.compare_to_script(as_segments(all_segments(alice, sid)), script)
    assert verdict["equal"], verdict


def test_an_engine_outage_is_a_gap_the_person_can_retranscribe_from_the_stored_audio(voice, login_client, monkeypatch):
    from app.video import transcribe as video_transcribe

    monkeypatch.setattr(video_transcribe, "_backoff_s", lambda attempt: 0.01)
    script, data = recording(voice.tmp, 90.0)
    alice = login_client("alice")
    voice.fake.fail_status = 503
    voice.fake.fail_after_calls = 3  # a few windows succeed, then the engine is down
    sid, done, _ = dictate(alice, data)
    assert done["status"] == "done" and done["outcome"] == "transcribed_with_gaps", done
    assert done["gaps"] and all(g["reason"] == "engine_unavailable" for g in done["gaps"])
    voice.fake.fail_status = None
    r = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "gaps"})
    assert r.status_code == 202, r.text
    again = wait_done(alice, sid)
    assert again["outcome"] == "transcribed" and again["gaps"] == []
    assert fw.compare_to_script(as_segments(all_segments(alice, sid)), script)["equal"]
    nothing = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "gaps"})
    assert nothing.status_code == 200, "no gaps left: nothing to do"


def test_every_window_failing_is_engine_unavailable_and_the_audio_is_kept(voice, login_client, monkeypatch):
    from app.video import transcribe as video_transcribe

    monkeypatch.setattr(video_transcribe, "_backoff_s", lambda attempt: 0.01)
    _script, data = recording(voice.tmp, 20.0)
    alice = login_client("alice")
    voice.fake.fail_status = 503
    sid, done, _ = dictate(alice, data)
    assert done["status"] == "failed" and done["outcome"] == "engine_unavailable"
    assert done["error"]["reason"] == "engine_unavailable"
    assert alice.get(f"/audio/sessions/{sid}/audio").content == data


def test_a_silent_recording_is_no_speech_and_nothing_reaches_the_engine(voice, login_client):
    alice = login_client("alice")
    silent = fw._wav_header(40 * SR * 2) + bytes(40 * SR * 2)
    _sid, done, _ = dictate(alice, silent)
    assert done["outcome"] == "no_speech" and done["speech_ms"] == 0
    assert voice.fake.calls == [], "silence costs the GPU nothing"


def test_discarding_deletes_the_recording_and_it_is_gone_for_good(voice, login_client):
    _script, data = recording(voice.tmp, 12.0)
    alice = login_client("alice")
    sid, _done, _ = dictate(alice, data)
    uid = int(db.get_user_by_username("alice")["id"])
    folder = os.path.join(voice.root, str(uid), sid)
    assert login_client("bob").delete(f"/audio/sessions/{sid}").status_code == 404
    assert os.path.isdir(folder)
    assert alice.delete(f"/audio/sessions/{sid}").status_code == 204
    assert not os.path.exists(folder)
    assert alice.get(f"/audio/sessions/{sid}").status_code == 404
    assert alice.get(f"/audio/sessions/{sid}/audio").status_code == 404
    assert alice.delete(f"/audio/sessions/{sid}").status_code == 204, "idempotent"
    assert alice.get("/audio/sessions").json()["sessions"] == []


def test_discarding_while_recording_stops_the_worker_and_keeps_nothing(voice, login_client):
    _script, data = recording(voice.tmp, 40.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    for seq, part in enumerate(parts_of(data)[:5]):
        put(alice, sid, seq, part)
    assert alice.delete(f"/audio/sessions/{sid}").status_code == 204
    assert dictation.RUNNER.wait_idle(sid, 10.0)
    uid = int(db.get_user_by_username("alice")["id"])
    time.sleep(0.2)
    assert not os.path.exists(os.path.join(voice.root, str(uid), sid))
    with db.connection() as con:
        assert con.execute("SELECT status FROM voice_sessions WHERE id = %s", (sid,)).fetchone()["status"] == "cancelled"
    assert create(alice).status_code == 201, "the person can record again at once"


def test_retention_removes_old_recordings_and_orphaned_folders_and_keeps_a_tombstone(voice, login_client, monkeypatch):
    _script, data = recording(voice.tmp, 10.0)
    alice = login_client("alice")
    sid, _done, _ = dictate(alice, data)
    uid = int(db.get_user_by_username("alice")["id"])
    orphan = os.path.join(voice.root, str(uid), uuid.uuid4().hex)
    os.makedirs(orphan)
    old = time.time() - 2 * 86400
    os.utime(orphan, (old, old))
    assert dictation.retention_sweep() == 1, "VOICE_RETENTION_DAYS=0 keeps recordings; only the orphan goes"
    assert not os.path.exists(orphan)
    assert alice.get(f"/audio/sessions/{sid}/audio").status_code == 200
    monkeypatch.setattr(settings, "voice_retention_days", 1)
    with db.connection() as con:
        con.execute("UPDATE voice_sessions SET finished_at = now() - interval '2 days' WHERE id = %s", (sid,))
    assert dictation.retention_sweep() == 1
    assert not os.path.exists(os.path.join(voice.root, str(uid), sid))
    gone = alice.get(f"/audio/sessions/{sid}/audio")
    assert gone.status_code == 410 and gone.json()["reason"] == "audio_deleted"
    assert alice.get(f"/audio/sessions/{sid}").status_code == 404
    again = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
    assert again.status_code == 410 and again.json()["reason"] == "audio_deleted"


def test_a_recording_left_idle_is_closed_and_everything_received_is_transcribed(voice, login_client, monkeypatch):
    script, data = recording(voice.tmp, 30.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    for seq, part in enumerate(parts_of(data)):
        put(alice, sid, seq, part)
    monkeypatch.setattr(settings, "voice_session_idle_s", 0.0)
    time.sleep(0.05)
    dictation.RUNNER.submit(dictation.maintain_once()).result(10)
    done = wait_done(alice, sid)
    assert done["ended_by"] == "idle" and done["outcome"] == "transcribed"
    assert fw.compare_to_script(as_segments(all_segments(alice, sid)), script)["equal"]
    closed = put(alice, sid, 99, b"late")
    assert closed.status_code == 409 and closed.json()["ended_by"] == "idle"


# ----------------------------------------------------------- oversight --


def _audit(action: str) -> List[Dict[str, Any]]:
    with db.connection() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM audit_events WHERE action = %s ORDER BY id", (action,)
        ).fetchall()]


def test_only_a_super_admin_can_read_another_members_recordings_and_every_read_is_audited(voice, login_client):
    _script, data = recording(voice.tmp, 12.0)
    alice = login_client("alice")
    sid, done, _ = dictate(alice, data)
    uid = int(db.get_user_by_username("alice")["id"])
    root = login_client("root", role="super_admin")
    admin = login_client("adm", role="admin")
    bob = login_client("bob")

    listing = root.get(f"/admin/api/members/{uid}/voice")
    assert listing.status_code == 200, listing.text
    assert [s["session_id"] for s in listing.json()["sessions"]] == [sid]
    assert "preview" not in listing.json()["sessions"][0], "the list carries no words"
    audio = root.get(f"/admin/api/members/{uid}/voice/{sid}/audio")
    assert audio.status_code == 200 and audio.content == data
    transcript = root.get(f"/admin/api/members/{uid}/voice/{sid}/transcript")
    assert transcript.status_code == 200 and transcript.json()["text"] == done["text"]

    assert len(_audit("admin_listed_voice_recordings")) == 1
    downloads = _audit("admin_downloaded_voice_recording")
    reads = _audit("admin_read_voice_transcript")
    assert len(downloads) == 1 and downloads[0]["resource_id"] == sid and downloads[0]["target_user_id"] == uid
    assert len(reads) == 1 and reads[0]["resource_type"] == "voice_session"

    for client in (admin, bob):
        for route in (
            f"/admin/api/members/{uid}/voice",
            f"/admin/api/members/{uid}/voice/{sid}/audio",
            f"/admin/api/members/{uid}/voice/{sid}/transcript",
        ):
            assert client.get(route).status_code == 404, (route,)
    bob_id = int(db.get_user_by_username("bob")["id"])
    assert root.get(f"/admin/api/members/{bob_id}/voice/{sid}/audio").status_code == 404, "ownership is re-derived"
    root_id = int(db.get_user_by_username("root")["id"])
    root.get(f"/admin/api/members/{root_id}/voice")
    assert len(_audit("admin_listed_voice_recordings")) == 1, "reading your own writes nothing"


# ------------------------------------------------------------- fairness --


def test_a_short_dictation_during_a_long_session_waits_for_one_window_not_the_recording(voice, login_client):
    """The policy the contract names: at most ONE session window decodes at a
    time fleet-wide, a session has at most one window in the gate or the
    engine, and a session whose person pressed Stop goes first. So a 6 s
    dictation that starts during somebody's 10-minute backlog waits behind the
    window already decoding, not behind the recording."""
    voice.fake.latency_s = 0.25
    long_script, long_data = recording(voice.tmp, 600.0, seed=3)
    short_script, short_data = recording(voice.tmp, 6.0, seed=5)
    alice = login_client("alice")
    bob = login_client("bob")
    a = create(alice).json()["session_id"]
    chunks = parts_of(long_data)
    for seq, part in enumerate(chunks):
        assert put(alice, a, seq, part).status_code == 200
    alice.post(f"/audio/sessions/{a}/finish", json={"last_part": len(chunks) - 1, "ended_by": "person"})
    deadline = time.monotonic() + 60
    while len(voice.fake.calls) < 3:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    b = create(bob).json()["session_id"]
    bchunks = parts_of(short_data)
    for seq, part in enumerate(bchunks):
        put(bob, b, seq, part)
    started = time.monotonic()
    calls_before = len(voice.fake.calls)
    bob.post(f"/audio/sessions/{b}/finish", json={"last_part": len(bchunks) - 1, "ended_by": "person"})
    short = wait_done(bob, b)
    long_state = alice.get(f"/audio/sessions/{a}").json()
    bob_id = int(db.get_user_by_username("bob")["id"])
    b_windows = [w for w in dictation._read_lines(os.path.join(voice.root, str(bob_id), b, "plan.jsonl")) if "start_s" in w]
    b_seconds = sorted(round(w["end_s"] - w["start_s"], 2) for w in b_windows)
    after = voice.fake.calls[calls_before:]
    mine = [i for i, c in enumerate(after) if round(c["seconds"], 2) in b_seconds]
    assert len(mine) == len(b_windows) == 1, (b_seconds, [round(c["seconds"], 2) for c in after])
    ahead_of_it = mine[0]  # engine calls that STARTED after Stop and before the short window
    with db.connection() as con:
        row = con.execute("SELECT finish_requested_at, finished_at FROM voice_sessions WHERE id = %s", (b,)).fetchone()
    server_wait_s = (row["finished_at"] - row["finish_requested_at"]).total_seconds()
    print(json.dumps({
        "engine_latency_s": voice.fake.latency_s,
        "short_server_wait_s": round(server_wait_s, 3),
        "short_window_started_after_stop_s": round(after[mine[0]]["started"] - started, 3),
        "engine_calls_started_ahead_of_it": ahead_of_it,
        "long_status_when_short_done": long_state["status"],
        "long_backlog_ms_when_short_done": long_state["backlog_ms"],
    }))
    assert short["outcome"] == "transcribed"
    assert fw.compare_to_script(as_segments(all_segments(bob, b)), short_script)["equal"]
    assert long_state["status"] == "finishing" and long_state["backlog_ms"] > 60_000, "the long session was still decoding"
    assert ahead_of_it <= 1, f"the short dictation waited behind {ahead_of_it} newly started windows"
    done = wait_done(alice, a, timeout=300)
    assert fw.compare_to_script(as_segments(all_segments(alice, a)), long_script)["equal"]
    assert voice.fleet.peak_in_flight == 1, "one session window at a time, fleet-wide"
    assert done["outcome"] == "transcribed"


def test_the_session_gate_serves_stopped_sessions_first_and_never_leaks_a_slot():
    import asyncio

    order: List[str] = []

    async def scenario():
        gate = asr._SessionGate()
        await gate.acquire()  # something is decoding
        waiting = []
        for name, urgent in (("live-1", False), ("stopped", True), ("live-2", False)):
            async def one(name=name, urgent=urgent):
                await gate.acquire(urgent=urgent)
                order.append(name)
                gate.release()
            waiting.append(asyncio.ensure_future(one()))
            await asyncio.sleep(0)
        cancelled = asyncio.ensure_future(gate.acquire())
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.sleep(0)
        gate.release()
        await asyncio.gather(*waiting)
        return gate.active, gate.waiting

    active, waiting = asyncio.run(scenario())
    assert order == ["stopped", "live-1", "live-2"]
    assert (active, waiting) == (0, 0)


# ------------------------------------------------------------- the plan --


def _plan_all(pcm: bytes, *, part_s: float = 5.0, finish: bool = True):
    planner = dictation.Planner()
    committed = []
    marks = []
    step = int(part_s * SR) * 2
    for i in range(0, len(pcm), step):
        planner.push(pcm[i:i + step])
        got = planner.plan(finishing=False)
        committed.extend(got)
        marks.extend([(planner.decoded_s, w) for w in got])
    if finish:
        committed.extend(planner.plan(finishing=True))
    return planner, committed, marks


def test_the_planner_sends_nothing_for_a_quiet_opening_and_opens_on_speech(voice):
    _script, data = recording(voice.tmp, 40.0, quiet_s=45.0, opening="digital")
    _planner, windows, _ = _plan_all(data[44:])
    assert windows and windows[0]["start_s"] >= 45.0 - 0.3
    assert all(w["end_s"] - w["start_s"] <= settings.voice_session_window_max_s + 1e-6 for w in windows)


def test_continuous_speech_is_cut_with_an_overlap_while_it_is_still_being_spoken(voice):
    script = fw.build_script(400.0, seed=7)
    block = max(script.continuous, key=lambda b: b[1] - b[0])
    assert block[1] - block[0] > 90
    path = str(voice.tmp / "continuous.wav")
    fw.write_speech_wav(path, script, seed=91)
    with open(path, "rb") as fh:
        pcm = fh.read()[44:]
    _planner, windows, marks = _plan_all(pcm)
    pieces = [w for w in windows if w["overlaps_previous"]]
    assert pieces, "a long stretch without a pause was split"
    for w in pieces:
        assert w["end_s"] - w["start_s"] <= settings.voice_session_window_max_s + 1e-6
    early = [(t, w) for t, w in marks if w["next_overlaps"] and w["end_s"] < block[1] - 10 and t < block[1]]
    assert early, "pieces commit before the speaker pauses"
    for i, w in enumerate(windows[1:], start=1):
        if w["overlaps_previous"]:
            assert w["start_s"] < windows[i - 1]["end_s"], "an overlapping piece starts inside the one before"
            assert windows[i - 1]["next_overlaps"] and windows[i - 1]["next_start_s"] == w["start_s"]


def test_the_rate_limits_answer_rate_limited(voice, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_part_per_min", 2)
    monkeypatch.setattr(settings, "voice_session_create_per_min", 1)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    part = b"RIFF" + b"\x00" * 4 + b"WAVE" + b"\x00" * 100
    assert put(alice, sid, 0, part).status_code == 200
    assert put(alice, sid, 1, b"\x00" * 64).status_code == 200
    limited = put(alice, sid, 2, b"\x00" * 64)
    assert limited.status_code == 429 and limited.json()["reason"] == "rate_limited"
    alice.delete(f"/audio/sessions/{sid}")
    again = create(alice)
    assert again.status_code == 429 and again.json()["reason"] == "rate_limited"


# ------------------------------------------------------- the stress case --


class AnonSampler:
    """Peak RssAnon of this process above a baseline, sampled every 10 ms
    (the method of tests/test_publicapi_audio_windows.py's two-hour test)."""

    def __init__(self) -> None:
        self.baseline = fw.rss_anon_bytes()
        self.peak = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.peak = max(self.peak, fw.rss_anon_bytes() - self.baseline)
            time.sleep(0.01)

    def __enter__(self) -> "AnonSampler":
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()


def test_two_hours_transcribe_word_for_word_with_no_ceiling_and_flat_memory(voice, login_client):
    """The owner's stress case: 7,200 s of speech, 1,440 parts of 5 s.

    Pass: every word once, in order, at its time (no word lost or doubled at
    a seam); every part answered 200 (no 413, no 504, no timeout anywhere);
    outcome 'transcribed'; and the process's anonymous memory after two hours
    within 16 MiB of what ten minutes needed. Nothing holds the recording in
    memory: the parts go to disk as they arrive, the PCM is a file, and one
    window of at most 30 s is in flight at a time.
    """
    warm_script, warm_data = recording(voice.tmp, 600.0, seed=3)
    long_script, long_data = recording(voice.tmp, 7200.0, seed=7)
    assert len(long_data) == 7200 * SR * 2 + 44
    alice = login_client("alice")
    started = time.monotonic()
    with AnonSampler() as sampler:
        warm_sid, warm_done, _ = dictate(alice, warm_data)
        warm_peak = sampler.peak
        sid, done, statuses = dictate(alice, long_data)
        long_peak = sampler.peak
    wall_s = time.monotonic() - started
    segments = all_segments(alice, sid)
    verdict = fw.compare_to_script(as_segments(segments), long_script)
    with db.connection() as con:
        row = dict(con.execute("SELECT * FROM voice_sessions WHERE id = %s", (sid,)).fetchone())
    uid = int(db.get_user_by_username("alice")["id"])
    with open(os.path.join(voice.root, str(uid), sid, "transcript.json"), encoding="utf-8") as fh:
        report = json.load(fh)["report"]
    print(json.dumps({
        "parts": len(statuses), "words": verdict["words"], "equal": verdict["equal"],
        "max_start_error_s": verdict.get("max_start_error_s"), "max_end_error_s": verdict.get("max_end_error_s"),
        "windows": row["windows_total"], "longest_clip_s": max(c["seconds"] for c in voice.fake.calls),
        "stitcher": report["stitcher"], "detector": report["detector"],
        "anon_peak_mib_10min": round(warm_peak / 2**20, 1), "anon_peak_mib_2h": round(long_peak / 2**20, 1),
        "wall_s": round(wall_s, 1),
    }))
    assert fw.compare_to_script(as_segments(all_segments(alice, warm_sid)), warm_script)["equal"]
    assert statuses == [200] * -(-len(long_data) // PART_BYTES)
    assert done["status"] == "done" and done["outcome"] == "transcribed", done
    assert done["audio_ms"] == 7_200_000
    assert verdict["equal"], json.dumps(verdict)
    assert verdict["duplicates"] == 0 and verdict["missing"] == 0
    assert verdict["words"] == len(long_script.words) > 10_000
    assert verdict["max_start_error_s"] <= 0.25 and verdict["max_end_error_s"] <= 0.25, verdict
    assert report["stitcher"]["seams_aligned"] >= 10, "continuous speech really was split with overlaps"
    assert all(call["seconds"] <= settings.voice_session_window_max_s + 0.01 for call in voice.fake.calls)
    assert voice.fleet.peak_in_flight == 1
    assert not any(voice.gate_flags), "no window of two hours asked for the engine's silence gate"
    assert long_peak - warm_peak < 16 * 1024 * 1024, (warm_peak, long_peak)


# ------------------------------------------------------------ the decoder --

_STREAMING_FFMPEG = r'''#!{python}
"""ffmpeg stand-in for the session decoder: the argv dictation._FfmpegStream
builds (allowlists, pipe:0 in, pipe:1 out, aresample=async=1), 16 kHz 16-bit
stereo WAV on stdin, mono s16le on stdout AS IT ARRIVES."""
import json, os, sys
args = sys.argv[1:]
log = os.environ["FAKE_STREAM_LOG"]
with open(log, "a") as fh:
    fh.write(json.dumps({{"pid": os.getpid(), "argv": args}}) + "\n")
before = args[: args.index("-i")]
assert "-protocol_whitelist" in before and "-format_whitelist" in before, "allowlists"
assert args[args.index("-i") + 1] == "pipe:0" and args[-1] == "pipe:1"
assert "aresample=async=1" in args
stream, out = sys.stdin.buffer, sys.stdout.buffer
head = stream.read(12)
while True:
    chunk = stream.read(8)
    tag, size = chunk[:4], int.from_bytes(chunk[4:8], "little")
    if tag == b"data":
        break
    stream.read(size + (size % 2))
import numpy as np
while True:
    block = stream.read(4 * 1600)
    if not block:
        break
    block = block[: len(block) // 4 * 4]
    pairs = np.frombuffer(block, dtype="<i2").reshape(-1, 2).astype(np.int32)
    out.write((pairs.sum(axis=1) // 2).astype("<i2").tobytes())
    out.flush()
'''


def test_a_format_that_needs_the_decoder_streams_through_one_ffmpeg_while_recording(voice, login_client, monkeypatch):
    """Anything but 16 kHz mono WAV goes through ONE ffmpeg for the life of the
    session, fed the parts as they arrive: the text still grows before Stop.
    (The CI host has no ffmpeg, so a stand-in honouring the argv plays it.)"""
    import sys as _sys

    bin_dir = voice.tmp / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "ffmpeg"
    tool.write_text(_STREAMING_FFMPEG.format(python=_sys.executable))
    tool.chmod(0o755)
    log = voice.tmp / "ffmpeg.jsonl"
    monkeypatch.setenv("FAKE_STREAM_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    script, mono = recording(voice.tmp, 60.0)
    samples = np.frombuffer(mono[44:], dtype="<i2")
    stereo = np.repeat(samples, 2).tobytes()
    header = (
        b"RIFF" + (36 + len(stereo)).to_bytes(4, "little") + b"WAVEfmt "
        + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (2).to_bytes(2, "little")
        + SR.to_bytes(4, "little") + (SR * 4).to_bytes(4, "little") + (4).to_bytes(2, "little")
        + (16).to_bytes(2, "little") + b"data" + len(stereo).to_bytes(4, "little")
    )
    data = header + stereo
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    chunks = parts_of(data, 2 * PART_BYTES)  # 5 s of stereo
    early: List[Dict[str, Any]] = []
    for seq, part in enumerate(chunks):
        body = put(alice, sid, seq, part).json()
        early.extend(body["segments"])
        if seq == len(chunks) - 2:
            deadline = time.monotonic() + 20
            while not early and time.monotonic() < deadline:
                early.extend(alice.get(f"/audio/sessions/{sid}", params={"wait_s": 1, "since_rev": body["rev"]}).json()["segments"])
    assert early, "the decoder streams: text before Stop"
    alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": len(chunks) - 1, "ended_by": "person"})
    done = wait_done(alice, sid)
    assert done["outcome"] == "transcribed" and done["audio_ms"] == 60_000
    assert fw.compare_to_script(as_segments(all_segments(alice, sid)), script)["equal"]
    runs = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(runs) == 1, "one decoder for the whole session"


@pytest.mark.skipif(dictation._FfmpegStream.binary() is None, reason="needs a real ffmpeg (the CI host has none)")
def test_a_real_browser_container_decodes_as_it_arrives(voice, login_client, monkeypatch):
    """With a real ffmpeg: the same speech as WebM/Opus, the container
    Chrome's MediaRecorder produces, uploaded in 5 s parts."""
    import subprocess

    script, wav = recording(voice.tmp, 60.0)
    src = voice.tmp / "speech.wav"
    src.write_bytes(wav)
    webm = voice.tmp / "speech.webm"
    subprocess.run(
        [dictation._FfmpegStream.binary(), "-nostdin", "-loglevel", "error", "-y", "-i", str(src),
         "-c:a", "libopus", "-b:a", "128k", "-f", "webm", str(webm)],
        check=True,
    )
    data = webm.read_bytes()
    alice = login_client("alice")
    sid, done, _ = dictate(alice, data, mime="audio/webm;codecs=opus", size=80_000)
    assert done["outcome"] == "transcribed", done
    assert abs(done["audio_ms"] - 60_000) <= 100
    verdict = fw.compare_to_script(as_segments(all_segments(alice, sid)), script)
    assert verdict["missing"] == 0 and verdict["duplicates"] == 0, verdict


class _SparseEngine:
    """Answers every window with five words spread over the whole clip, as
    whisper answered a slowly read chapter title on the live worker
    (2026-09-29): one 12.52 s segment, 0.40 words/s, no_speech_prob 0.0."""

    name = "whisper"
    model = "whisper-test"

    def __init__(self, no_speech_prob):
        self.no_speech_prob = no_speech_prob

    async def transcribe_window(self, audio, *, filename, content_type, timeout_s=None):
        seconds = (len(audio) - 44) / (2 * SR)
        segments = ({"start": 0.0, "end": seconds, "text": "Chapter three, the stockbroker's clerk.", "language": "en"},)
        transcript = asr.TranscriptSegments(
            text=segments[0]["text"], language="English", language_code="en", provider="whisper",
            model="whisper-test", engine_ms=5, segments=segments,
        )
        return asr.WindowReply(transcript, self.no_speech_prob)

    async def health(self):
        return True


@pytest.mark.parametrize("no_speech_prob, kept", [(0.0, True), (0.9, False)])
def test_slow_real_speech_is_kept_when_the_engine_heard_speech(voice, login_client, no_speech_prob, kept):
    """A window is judged the way dictation judges a clip: its words stand when
    the engine itself would have let it through (no_speech_prob at or under
    0.6), and must be as dense as speech only when it would have gated it."""
    asr.set_provider(_SparseEngine(no_speech_prob))
    _script, data = recording(voice.tmp, 12.0)
    alice = login_client("alice")
    sid, done, _ = dictate(alice, data)
    if kept:
        assert done["outcome"] == "transcribed" and "stockbroker's clerk" in done["text"], done
        assert done["gaps"] == []
    else:
        assert done["outcome"] == "no_words", done
        assert done["gaps"] and all(g["reason"] == "dropped_as_noise" for g in done["gaps"])
        uid = int(db.get_user_by_username("alice")["id"])
        lines = dictation._read_lines(os.path.join(voice.root, str(uid), sid, "results.jsonl"))
        assert all(line["dropped_segments"] for line in lines), "what was dropped is kept for checking"
