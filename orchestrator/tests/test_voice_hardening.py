"""Recording sessions, hardened before they ship (2026-09-29).

The security review of the voice-dictation server half
(scratchpad voice-security-review.md) and the follow-ups from the Recordings
page and the frontend verifiers found ways a stored, unlimited dictation
could be turned against the head node or against its own people. Each test
here names the finding it closes and fails on the code before the fix
(wip/backend-long-form-c32-6 at db51558f):

  * retranscription was unbounded and jumped the queue (review 2);
  * one member could fill the root filesystem that holds production
    Postgres: no arrival ceiling, no quota, a 20 GiB floor, parts accepted
    after the decoder died, mp3/aac bytes never checked (review 3);
  * decoded PCM was unbounded, written on the runner loop, with no free-space
    check, through a 23-demuxer probe, and a WAV header could buffer
    without limit (reviews 4 and 12);
  * a removed member's recordings were kept and reachable by nobody, and the
    maintenance loop only started on the first request (review 5);
  * turning voice input off locked a person out of their own recordings
    (review 7);
  * the lock map grew with every id anybody sent, long-polls were
    unbounded, and the JSON routes took text/plain (reviews 8, 11, 13);
  * eight long recordings refused every other dictation, a browser offline
    past the idle close lost its held audio, and 'engine' meant both busy and
    down (the follow-ups).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from app import asr, db, dictation
from app.config import settings
from tests import publicapi_fake_whisper as fw
from tests.test_voice_sessions import (  # noqa: F401 — `voice` is a fixture
    PART_BYTES,
    SR,
    _STREAMING_FFMPEG,
    all_segments,
    as_segments,
    create,
    dictate,
    parts_of,
    put,
    recording,
    sha,
    voice,
    wait_done,
)

GiB = 1024 ** 3


def uid(name: str) -> int:
    return int(db.get_user_by_username(name)["id"])


def row_of(sid: str) -> Dict[str, Any]:
    with db.connection() as con:
        return dict(con.execute("SELECT * FROM voice_sessions WHERE id = %s", (sid,)).fetchone())


def until(predicate, timeout: float = 15.0, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not met in time")


def install_ffmpeg(voice, monkeypatch, body: str) -> None:
    """A stand-in ffmpeg on PATH (the CI host has none)."""
    bin_dir = voice.tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    tool = bin_dir / "ffmpeg"
    tool.write_text(body.format(python=sys.executable))
    tool.chmod(0o755)
    monkeypatch.setenv("FAKE_STREAM_LOG", str(voice.tmp / "ffmpeg.jsonl"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")


_DYING_FFMPEG = r'''#!{python}
import sys
sys.stdin.buffer.read(1000)
sys.exit(1)
'''

_SILENT_FFMPEG = r'''#!{python}
import sys
while sys.stdin.buffer.read(65536):
    pass
'''

EBML = b"\x1a\x45\xdf\xa3"


def stereo_wav(mono: bytes) -> bytes:
    import numpy as np

    samples = np.frombuffer(mono[44:], dtype="<i2")
    stereo = np.repeat(samples, 2).tobytes()
    return (
        b"RIFF" + (36 + len(stereo)).to_bytes(4, "little") + b"WAVEfmt "
        + (16).to_bytes(4, "little") + (1).to_bytes(2, "little") + (2).to_bytes(2, "little")
        + SR.to_bytes(4, "little") + (SR * 4).to_bytes(4, "little") + (4).to_bytes(2, "little")
        + (16).to_bytes(2, "little") + b"data" + len(stereo).to_bytes(4, "little") + stereo
    )


def finish(client, sid: str, last: int):
    return client.post(f"/audio/sessions/{sid}/finish", json={"last_part": last, "ended_by": "person"})


def gaps_session(voice, client, monkeypatch, seconds: float = 30.0, seed: int = 7) -> str:
    """A finished dictation with engine gaps, ready to retranscribe."""
    from app.video import transcribe as video_transcribe

    monkeypatch.setattr(video_transcribe, "_backoff_s", lambda attempt: 0.01)
    _script, data = recording(voice.tmp, seconds, seed=seed)
    voice.fake.fail_status = 503
    sid, done, _ = dictate(client, data)
    voice.fake.fail_status = None
    assert done["outcome"] == "engine_unavailable", done
    return sid


# ----------------------------------------------- 1. retranscription (review 2) --


def test_retranscribe_counts_against_the_live_ceiling(voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid = gaps_session(voice, alice, monkeypatch)
    monkeypatch.setattr(settings, "voice_session_max_active", 1)
    assert create(login_client("bob")).status_code == 201  # one live recording
    r = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
    assert r.status_code == 503 and r.json()["reason"] == "capacity_full", r.text


def test_retranscribe_needs_the_free_space_a_recording_needs(voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid = gaps_session(voice, alice, monkeypatch)
    monkeypatch.setattr(settings, "voice_min_free_bytes", 1 << 62)
    r = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
    assert r.status_code == 507 and r.json()["reason"] == "storage_full", r.text


def test_one_retranscription_in_flight_per_person(voice, login_client, monkeypatch):
    alice = login_client("alice")
    first = gaps_session(voice, alice, monkeypatch, seed=7)
    second = gaps_session(voice, alice, monkeypatch, seed=9)
    voice.fake.latency_s = 1.0
    r = alice.post(f"/audio/sessions/{first}/retranscribe", json={"scope": "all"})
    assert r.status_code == 202, r.text
    busy = alice.post(f"/audio/sessions/{second}/retranscribe", json={"scope": "all"})
    assert busy.status_code == 409 and busy.json()["reason"] == "retranscribe_busy", busy.text
    assert busy.json()["session_id"] == first
    voice.fake.latency_s = 0.0
    assert wait_done(alice, first)["status"] == "done"
    assert alice.post(f"/audio/sessions/{second}/retranscribe", json={"scope": "all"}).status_code == 202


def test_retranscribe_is_rate_limited_per_hour(voice, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_retranscribe_per_hour", 2, raising=False)
    alice = login_client("alice")
    _script, data = recording(voice.tmp, 10.0)
    sid, _done, _ = dictate(alice, data)
    for _ in range(2):
        assert alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"}).status_code == 202
        wait_done(alice, sid)
    third = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
    assert third.status_code == 429 and third.json()["reason"] == "rate_limited", third.text


def test_retranscribe_windows_go_at_recording_priority_never_finish(voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid = gaps_session(voice, alice, monkeypatch)
    seen: List[bool] = []
    real = asr.transcribe_session_window

    async def spy(audio, *, filename, urgent=False, **kwargs):
        seen.append(bool(urgent() if callable(urgent) else urgent))
        return await real(audio, filename=filename, urgent=urgent, **kwargs)

    monkeypatch.setattr(asr, "transcribe_session_window", spy)
    assert alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"}).status_code == 202
    done = wait_done(alice, sid)
    assert done["outcome"] == "transcribed"
    assert seen and not any(seen), f"retranscription windows asked for FINISH priority: {seen}"


# ------------------------------------------------------- 2. disk (review 3) --


def test_a_part_arriving_faster_than_it_could_be_recorded_is_refused_too_fast(voice, login_client, monkeypatch):
    """16 kHz mono WAV is exactly 256 kb/s. With 10 s of head start a new
    session may hold 320,000 bytes; the third 160,000-byte part is ahead of
    the clock and is refused with a Retry-After, not stored."""
    monkeypatch.setattr(settings, "voice_max_bits_per_second", 256_000, raising=False)
    monkeypatch.setattr(settings, "voice_rate_slack_s", 10.0, raising=False)
    _script, data = recording(voice.tmp, 30.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    chunks = parts_of(data)
    assert put(alice, sid, 0, chunks[0]).status_code == 200
    assert put(alice, sid, 1, chunks[1][: PART_BYTES - 1000]).status_code == 200
    ahead = put(alice, sid, 2, chunks[2])
    assert ahead.status_code == 429 and ahead.json()["reason"] == "too_fast", ahead.text
    assert int(ahead.headers["retry-after"]) >= 1
    assert row_of(sid)["bytes_stored"] == PART_BYTES * 2 - 1000
    assert row_of(sid)["status"] == "recording", "a fast client is slowed, not cut off"


def test_a_person_over_their_quota_is_told_quota_full_and_the_recording_ends(voice, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_user_quota_bytes", 3 * PART_BYTES, raising=False)
    _script, data = recording(voice.tmp, 30.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    chunks = parts_of(data)
    for seq in range(3):
        assert put(alice, sid, seq, chunks[seq]).status_code == 200
    over = put(alice, sid, 3, chunks[3])
    assert over.status_code == 507 and over.json()["reason"] == "quota_full", over.text
    assert over.json()["quota_bytes"] == over.json()["used_bytes"] == 3 * PART_BYTES
    done = wait_done(alice, sid)
    assert done["ended_by"] == "quota_full" and done["outcome"] == "transcribed"
    again = create(alice)
    assert again.status_code == 507 and again.json()["reason"] == "quota_full"
    assert alice.delete(f"/audio/sessions/{sid}").status_code == 204
    assert create(alice).status_code == 201, "deleting a recording frees the quota"


def test_the_free_space_floor_defaults_to_the_files_api_watermark(monkeypatch):
    """/data/voice is on the head's root filesystem with production Postgres;
    the Files API stops at 250 GiB free there, and recordings must too."""
    from app import config

    monkeypatch.delenv("VOICE_MIN_FREE_BYTES", raising=False)
    assert config.Settings().voice_min_free_bytes >= 250 * GiB


@pytest.mark.parametrize("mime, opening", [("audio/mpeg", b"this is not an mp3 file"), ("audio/aac", b"nor is this aac audio!!")])
def test_every_declared_type_has_its_opening_checked(voice, login_client, mime, opening):
    alice = login_client("alice")
    sid = create(alice, mime=mime).json()["session_id"]
    r = put(alice, sid, 0, opening * 100, mime=mime)
    assert r.status_code == 415 and r.json()["reason"] == "unsupported_format", r.text


@pytest.mark.parametrize("ext, head, ok", [
    ("mp3", b"ID3\x04\x00\x00\x00\x00\x00\x00", True),
    ("mp3", b"\xff\xfb\x90\x64" + b"\x00" * 8, True),
    ("mp3", b"\xff\xf9\x90\x64" + b"\x00" * 8, False),  # an ADTS header is not MPEG audio layer III
    ("aac", b"\xff\xf1\x50\x80" + b"\x00" * 8, True),
    ("aac", b"ADIF" + b"\x00" * 8, True),
    ("aac", b"\xff\xfb\x90\x64" + b"\x00" * 8, False),
    ("3gp", b"\x00\x00\x00\x18ftyp3gp4", True),
    ("opus", b"OggS\x00\x02" + b"\x00" * 6, True),
])
def test_magic_bytes_per_type(ext, head, ok):
    assert dictation._magic_ok(ext, head) is ok


def test_parts_after_the_decoder_died_are_refused_session_closed_undecodable(voice, login_client, monkeypatch):
    install_ffmpeg(voice, monkeypatch, _DYING_FFMPEG)
    alice = login_client("alice")
    sid = create(alice, mime="audio/webm;codecs=opus").json()["session_id"]
    junk = EBML + os.urandom(100_000)
    assert put(alice, sid, 0, junk, mime="audio/webm").status_code == 200
    until(lambda: row_of(sid)["ended_by"] == "undecodable")
    late = put(alice, sid, 1, os.urandom(100_000), mime="audio/webm")
    assert late.status_code == 409 and late.json()["reason"] == "session_closed", late.text
    assert late.json()["ended_by"] == "undecodable"
    done = wait_done(alice, sid)
    assert done["status"] == "failed" and done["outcome"] == "undecodable"
    assert row_of(sid)["bytes_stored"] == len(junk), "nothing after the decoder died was stored"


def test_a_stream_that_decodes_to_nothing_stops_being_accepted(voice, login_client, monkeypatch):
    """Past progressive_after_bytes (512 KiB) with no audio out, the bytes
    are not the container they claim: a 4-byte header must not buy unlimited
    storage (review 3c)."""
    install_ffmpeg(voice, monkeypatch, _SILENT_FFMPEG)
    alice = login_client("alice")
    sid = create(alice, mime="audio/webm").json()["session_id"]
    parts = [EBML + os.urandom(199_996)] + [os.urandom(200_000) for _ in range(3)]
    for seq, part in enumerate(parts):
        assert put(alice, sid, seq, part, mime="audio/webm").status_code == 200
    until(lambda: row_of(sid)["ended_by"] == "undecodable", timeout=20)
    late = put(alice, sid, 4, os.urandom(200_000), mime="audio/webm")
    assert late.status_code == 409 and late.json()["ended_by"] == "undecodable", late.text
    done = wait_done(alice, sid)
    assert done["outcome"] == "undecodable"


# -------------------------------------------------- 3. PCM (reviews 4 and 12) --


def test_decoded_audio_longer_than_the_recording_has_existed_fails_undecodable(voice, login_client, monkeypatch):
    """60 s of audio decoded from a session that has existed for a second or
    two, with 10 s of slack: the decoder is stopped and the recording fails,
    instead of writing whatever length a crafted stream decodes to."""
    monkeypatch.setattr(settings, "voice_rate_slack_s", 10.0, raising=False)
    _script, data = recording(voice.tmp, 60.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    chunks = parts_of(data)
    for seq, part in enumerate(chunks):
        r = put(alice, sid, seq, part)
        if r.status_code == 409:
            assert r.json()["ended_by"] == "undecodable", r.text
            break
        assert r.status_code == 200, r.text
    else:
        assert finish(alice, sid, len(chunks) - 1).status_code in (200, 202)
    done = wait_done(alice, sid)
    assert done["status"] == "failed" and done["outcome"] == "undecodable", done
    assert done["error"]["reason"] == "undecodable"
    assert done["audio_ms"] <= 12_000 + 5_000, done["audio_ms"]


def test_a_part_that_races_the_server_closing_the_recording_is_told_session_closed(voice, login_client, monkeypatch):
    """The decoder's `_end_recording` (and the idle close) finish a session
    through `_finish_sync`, which does not take the part route's lock. A part
    whose status check ran before that close and whose guarded UPDATE ran
    after it matched nothing, and was answered 409 `part_conflict` ("another
    tab"), which the browser treats as fatal. The recording is closed, not
    contended: the answer is `session_closed` with its `ended_by`, the one the
    browser waits for the words on. Found as a load-dependent failure of the
    test above under Python 3.11 with three shards running (2026-09-29)."""
    _script, data = recording(voice.tmp, 12.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    real_append_line = dictation._append_line
    closed = []

    def close_first(path, payload):
        # Between the status check and the guarded UPDATE: exactly where the
        # decoder thread lands when it loses the race.
        if not closed and str(path).endswith("parts.jsonl"):
            closed.append(dictation._finish_sync(sid, None, "undecodable", None))
        return real_append_line(path, payload)

    monkeypatch.setattr(dictation, "_append_line", close_first)
    r = put(alice, sid, 0, parts_of(data)[0])
    assert closed, "the close did not run inside the part's write"
    assert r.status_code == 409, r.text
    assert r.json()["reason"] == "session_closed", r.text
    assert r.json()["ended_by"] == "undecodable", r.text
    assert row_of(sid)["next_part"] == 0, "a part the row never acknowledged is not counted"


def test_free_space_is_checked_while_decoding(voice, login_client, monkeypatch):
    monkeypatch.setattr(dictation, "_FREE_CHECK_BYTES", 64 * 1024, raising=False)
    monkeypatch.setattr(settings, "voice_min_free_bytes", 10 * GiB)
    def free(path=None):
        # Plenty for the part route; none left once the worker measures.
        if threading.current_thread().name.startswith(("voice-io", "voice-sessions")):
            return 0
        return 100 * GiB

    monkeypatch.setattr(dictation, "free_bytes", free)
    _script, data = recording(voice.tmp, 40.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    for seq, part in enumerate(parts_of(data)[:3]):
        assert put(alice, sid, seq, part).status_code == 200
    until(lambda: row_of(sid)["ended_by"] == "storage_full")
    late = put(alice, sid, 3, parts_of(data)[3])
    assert late.status_code == 409 and late.json()["ended_by"] == "storage_full"
    done = wait_done(alice, sid)
    assert done["error"]["reason"] == "storage_full", done


def test_pcm_is_written_off_the_runner_loop_in_batches(voice, login_client, monkeypatch):
    writes: List[tuple] = []
    real_fdopen = os.fdopen

    class Spy:
        def __init__(self, fh):
            self._fh = fh

        def write(self, data):
            writes.append((threading.current_thread().name, len(data)))
            return self._fh.write(data)

        def __getattr__(self, name):
            return getattr(self._fh, name)

    def fdopen(fd, mode="r", *args, **kwargs):
        fh = real_fdopen(fd, mode, *args, **kwargs)
        return Spy(fh) if mode == "wb" and kwargs.get("buffering") == 0 else fh

    monkeypatch.setattr(os, "fdopen", fdopen)
    _script, data = recording(voice.tmp, 90.0)
    alice = login_client("alice")
    _sid, done, _ = dictate(alice, data)
    assert done["outcome"] == "transcribed"
    assert writes, "the PCM was written"
    on_loop = [w for w in writes if w[0] == "voice-sessions"]
    assert not on_loop, f"{len(on_loop)} PCM writes ran on the runner loop"
    assert len(writes) <= 90 * SR * 2 // (256 * 1024) + 4, f"{len(writes)} writes for 90 s: not batched"


@pytest.mark.parametrize("ext, demuxer", [
    ("webm", "matroska"), ("mp4", "mov"), ("m4a", "mov"), ("3gp", "mov"),
    ("ogg", "ogg"), ("opus", "ogg"), ("wav", "wav"), ("mp3", "mp3"), ("flac", "flac"), ("aac", "aac"),
])
def test_the_decoder_is_pinned_to_the_declared_demuxer(ext, demuxer):
    argv = dictation._FfmpegStream(lambda pcm: None, ext).argv()
    before_input = argv[: argv.index("-i")]
    assert before_input[-2:] == ["-f", demuxer], before_input


def test_a_wav_header_is_never_buffered_past_64_kib():
    decoder = dictation._WavPassthrough(lambda pcm: None)
    header = b"RIFF" + (0).to_bytes(4, "little") + b"WAVE" + b"LIST" + (4 * 1024 * 1024).to_bytes(4, "little")

    async def feed() -> int:
        fed = 0
        await decoder.feed(header)
        with pytest.raises(dictation._Undecodable):
            while fed < 4 * 1024 * 1024:
                await decoder.feed(b"\x00" * 16384)
                fed += 16384
        return fed

    fed = asyncio.run(feed())
    assert fed <= 64 * 1024, fed
    assert len(decoder._head) <= 64 * 1024 + 16384


# ------------------------------------------ 4. account removal (review 5) --


def test_a_removed_members_recordings_stay_reachable_by_a_super_admin_audited(voice, login_client):
    _script, data = recording(voice.tmp, 12.0)
    alice = login_client("alice")
    sid, done, _ = dictate(alice, data)
    alice_id = uid("alice")
    root = login_client("root", role="super_admin")
    admin = login_client("adm", role="admin")
    assert root.delete(f"/admin/api/members/{alice_id}").status_code == 200

    listing = root.get(f"/admin/api/members/{alice_id}/voice")
    assert listing.status_code == 200, listing.text
    assert [s["session_id"] for s in listing.json()["sessions"]] == [sid]
    audio = root.get(f"/admin/api/members/{alice_id}/voice/{sid}/audio")
    assert audio.status_code == 200 and audio.content == data
    transcript = root.get(f"/admin/api/members/{alice_id}/voice/{sid}/transcript")
    assert transcript.status_code == 200 and transcript.json()["text"] == done["text"]
    removed = root.get("/admin/api/voice/removed-members")
    assert removed.status_code == 200
    [entry] = removed.json()["members"]
    assert (entry["user_id"], entry["recordings"], entry["bytes"]) == (alice_id, 1, len(data))
    for route in (f"/admin/api/members/{alice_id}/voice", f"/admin/api/members/{alice_id}/voice/{sid}/audio"):
        assert admin.get(route).status_code == 404, "an admin still cannot"
    assert admin.delete(f"/admin/api/members/{alice_id}/voice/{sid}").status_code == 404

    deleted = root.delete(f"/admin/api/members/{alice_id}/voice/{sid}")
    assert deleted.status_code == 204, deleted.text
    assert not os.path.exists(os.path.join(voice.root, str(alice_id), sid))
    with db.connection() as con:
        events = [dict(r) for r in con.execute(
            "SELECT * FROM audit_events WHERE action = 'admin_deleted_voice_recording'"
        ).fetchall()]
    assert len(events) == 1 and events[0]["resource_id"] == sid and events[0]["target_user_id"] == alice_id
    assert root.get(f"/admin/api/members/{alice_id}/voice").json()["sessions"] == []


def test_a_current_members_recording_is_theirs_to_delete_and_strangers_stay_404(voice, login_client):
    _script, data = recording(voice.tmp, 8.0)
    alice = login_client("alice")
    sid, _done, _ = dictate(alice, data)
    root = login_client("root", role="super_admin")
    current = root.delete(f"/admin/api/members/{uid('alice')}/voice/{sid}")
    assert current.status_code == 409 and current.json()["reason"] == "member_active"
    # A disabled account that was never removed from THIS workspace.
    from app.authn import store

    login_client("ghost")
    ghost_id = uid("ghost")
    with db.connection() as con:
        con.execute("DELETE FROM workspace_memberships WHERE user_id = %s", (ghost_id,))
    store.set_status(ghost_id, "disabled")
    assert root.get(f"/admin/api/members/{ghost_id}/voice").status_code == 404


def test_the_lifespan_starts_the_voice_maintenance_loop(monkeypatch):
    """Idle close, lease adoption, retention and the orphan sweep must run
    after a restart without waiting for somebody to touch the microphone."""
    from app import db as real_db, main as app_main, resources, shutdown_signals, web_worker

    class FakeDurable:
        def request_suspend(self, reason):
            return None

        async def start(self):
            return None

        async def suspend_all(self, reason):
            return None

        def stop(self):
            return None

    async def _noop():
        return None

    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(settings, "voice_sessions_enabled", True)
    monkeypatch.setattr(web_worker, "start", lambda: None)
    monkeypatch.setattr(web_worker, "stop", _noop)
    monkeypatch.setattr(resources, "raise_nofile", lambda: (1, 1))
    monkeypatch.setattr(app_main, "_durable_module", lambda: FakeDurable())
    monkeypatch.setattr(shutdown_signals, "install", lambda cb, loop=None: True)
    monkeypatch.setattr(real_db, "close_pool", lambda: None)
    dictation.RUNNER.stop_all()
    seen: Dict[str, bool] = {}

    def running() -> bool:
        m = dictation.RUNNER._maintenance
        return m is not None and not m.done()

    async def drive():
        async with app_main.lifespan(app_main.app):
            seen["serving"] = running()

    try:
        asyncio.run(drive())
    finally:
        dictation.RUNNER.stop_all()
    assert seen["serving"], "the maintenance loop was not started by the lifespan"


# ------------------------------------------------ 5. feature gate (review 7) --


def test_voice_input_off_still_lets_a_person_list_download_and_delete_their_recordings(voice, login_client):
    _script, data = recording(voice.tmp, 10.0)
    alice = login_client("alice")
    sid, _done, _ = dictate(alice, data)
    other, _d2, _ = dictate(alice, recording(voice.tmp, 8.0, seed=9)[1])
    root = login_client("root", role="super_admin")
    assert root.put(f"/admin/api/members/{uid('alice')}/access", json={"features": {"voice_input": False}}).status_code == 200

    listing = alice.get("/audio/sessions")
    assert listing.status_code == 200, listing.text
    assert {s["session_id"] for s in listing.json()["sessions"]} == {sid, other}
    assert alice.get(f"/audio/sessions/{sid}").status_code == 200
    assert alice.get(f"/audio/sessions/{sid}/audio").content == data
    assert alice.delete(f"/audio/sessions/{sid}").status_code == 204
    # What voice input gates: making and re-making recordings.
    assert create(alice).json()["reason"] == "voice_off"
    for method, route, body in (
        ("put", f"/audio/sessions/{other}/parts/0", None),
        ("post", f"/audio/sessions/{other}/finish", {}),
        ("post", f"/audio/sessions/{other}/retranscribe", {"scope": "all"}),
    ):
        if method == "put":
            r = put(alice, other, 0, b"RIFF")
        else:
            r = alice.post(route, json=body)
        assert r.status_code == 403 and r.json()["reason"] == "voice_off", (route, r.text)


# ---------------------------------------------------- 6. the list (follow-ups) --


def test_the_list_says_what_is_kept_and_for_how_long_and_takes_either_plus(voice, login_client, monkeypatch):
    alice = login_client("alice")
    first, _d1, _ = dictate(alice, recording(voice.tmp, 8.0, seed=3)[1])
    second, _d2, _ = dictate(alice, recording(voice.tmp, 8.0, seed=5)[1])
    monkeypatch.setattr(settings, "voice_retention_days", 30)
    with db.connection() as con:
        con.execute("UPDATE voice_sessions SET finished_at = now() - interval '31 days' WHERE id = %s", (first,))
    assert dictation.retention_sweep() == 1
    body = alice.get("/audio/sessions", params={"limit": 1}).json()
    assert body["retention_days"] == 30
    assert body["sessions"][0]["session_id"] == second and body["sessions"][0]["kept"] is True
    cursor = body["next_before"]
    assert "+00:00" in cursor
    raw = alice.get(f"/audio/sessions?limit=1&before={cursor}")  # the "+" arrives as a space
    encoded = alice.get("/audio/sessions", params={"limit": 1, "before": cursor})  # %2B
    for page in (raw, encoded):
        assert page.status_code == 200, page.text
        item = page.json()["sessions"][0]
        assert item["session_id"] == first and item["kept"] is False and item["preview"] is None


# ------------------------------------------------ 7. capacity for many users --


def test_many_recordings_at_once_are_all_accepted_and_stored(voice, login_client):
    """The old fleet-wide VOICE_SESSION_MAX_ACTIVE=8 refused the ninth person,
    even for 3 s, for as long as eight recordings with no length limit ran."""
    part = recording(voice.tmp, 5.0)[1]
    sessions = []
    for i in range(12):
        client = login_client(f"person{i}")
        r = create(client)
        assert r.status_code == 201, (i, r.text)
        sessions.append((client, r.json()["session_id"]))
    for client, sid in sessions:
        assert put(client, sid, 0, part).status_code == 200


class _FakeLive:
    def __init__(self, name: str, *, short: bool, recording: bool = True):
        self.id = name
        self.short = short
        self.recording = recording

    def is_short(self) -> bool:
        return self.short

    def decode_restricted(self) -> bool:
        return self.recording and not self.short


def test_the_decoder_gate_queues_beyond_its_cap_and_keeps_slots_for_short_sessions(monkeypatch):
    monkeypatch.setattr(settings, "voice_decoders_max", 3, raising=False)
    monkeypatch.setattr(settings, "voice_decoders_short_reserved", 1, raising=False)

    async def scenario():
        gate = dictation._DecoderGate()
        long1, long2, long3 = (_FakeLive(f"L{i}", short=False) for i in (1, 2, 3))
        short = _FakeLive("S", short=True)
        await gate.acquire(long1)
        await gate.acquire(long2)
        third = asyncio.ensure_future(gate.acquire(long3))
        await asyncio.sleep(0)
        assert not third.done(), "a third long recording waits: one slot is kept for short ones"
        await asyncio.wait_for(gate.acquire(short), 1.0)
        assert set(gate.holders) == {"L1", "L2", "S"}
        # The short session turns long while both long slots are held: it yields.
        short.short = False
        assert gate.must_yield(short) and not gate.must_yield(long1)
        gate.release(short)
        await asyncio.sleep(0)
        assert not third.done(), "still two long holders"
        finishing = _FakeLive("F", short=False, recording=False)  # pressed Stop: somebody waits
        await asyncio.wait_for(gate.acquire(finishing), 1.0)
        gate.release(finishing)
        gate.release(long1)
        await asyncio.wait_for(third, 1.0)
        assert set(gate.holders) == {"L2", "L3"}
        # A long recording waiting for a slot whose person presses Stop is
        # waited on now, and takes the reserved slot at once.
        long4 = _FakeLive("L4", short=False)
        fourth = asyncio.ensure_future(gate.acquire(long4))
        await asyncio.sleep(0)
        assert not fourth.done()
        long4.recording = False
        gate.regrant()
        await asyncio.wait_for(fourth, 1.0)
        assert set(gate.holders) == {"L2", "L3", "L4"}

    asyncio.run(scenario())


def test_a_recording_beyond_the_decoder_cap_is_stored_and_decodes_when_a_slot_frees(voice, login_client, monkeypatch):
    install_ffmpeg(voice, monkeypatch, _STREAMING_FFMPEG)
    monkeypatch.setattr(settings, "voice_decoders_max", 1, raising=False)
    monkeypatch.setattr(settings, "voice_decoders_short_reserved", 0, raising=False)
    _s1, mono_a = recording(voice.tmp, 30.0, seed=3)
    script_b, mono_b = recording(voice.tmp, 20.0, seed=5)
    a_parts = parts_of(stereo_wav(mono_a), 2 * PART_BYTES)
    b_parts = parts_of(stereo_wav(mono_b), 2 * PART_BYTES)
    alice, bob = login_client("alice"), login_client("bob")
    a = create(alice).json()["session_id"]
    for seq, part in enumerate(a_parts[:3]):
        assert put(alice, a, seq, part).status_code == 200
    until(lambda: alice.get(f"/audio/sessions/{a}").json()["audio_ms"] > 0)
    b = create(bob).json()["session_id"]
    for seq, part in enumerate(b_parts):
        assert put(bob, b, seq, part).status_code == 200, "stored even though no decoder is free"
    assert finish(bob, b, len(b_parts) - 1).status_code == 202
    time.sleep(1.5)
    waiting = bob.get(f"/audio/sessions/{b}").json()
    assert waiting["status"] == "finishing" and waiting["audio_ms"] == 0, waiting
    assert waiting["waiting_on"] == "engine" and waiting["bytes_stored"] == sum(map(len, b_parts))
    for seq, part in list(enumerate(a_parts))[3:]:
        assert put(alice, a, seq, part).status_code == 200
    assert finish(alice, a, len(a_parts) - 1).status_code == 202
    done_b = wait_done(bob, b)
    assert done_b["outcome"] == "transcribed" and done_b["audio_ms"] == 20_000
    assert fw.compare_to_script(as_segments(all_segments(bob, b)), script_b)["equal"]
    assert wait_done(alice, a)["outcome"] == "transcribed"


def test_the_engine_gate_serves_short_sessions_before_long_ones():
    order: List[str] = []

    async def scenario():
        gate = asr._SessionGate()
        await gate.acquire()
        waiting = []
        for name, urgent, short in (("long-finishing", True, False), ("short-recording", False, True), ("long-recording", False, False)):
            async def one(name=name, urgent=urgent, short=short):
                await gate.acquire(urgent=urgent, short=short)
                order.append(name)
                gate.release()
            waiting.append(asyncio.ensure_future(one()))
            await asyncio.sleep(0)
        gate.release()
        await asyncio.gather(*waiting)

    asyncio.run(scenario())
    assert order == ["short-recording", "long-finishing", "long-recording"]


# ---------------------------------------------- 8. offline past the idle close --


def _cut_in_a_pause(script: fw.Script, after_s: float) -> float:
    """A moment after `after_s` with no word spoken for 0.4 s either side."""
    words = sorted(script.words, key=lambda w: w.start_s)
    for prev, nxt in zip(words, words[1:]):
        if prev.end_s >= after_s and nxt.start_s - prev.end_s >= 1.0:
            return round((prev.end_s + nxt.start_s) / 2.0, 3)
    raise AssertionError("no pause in the script")


def test_audio_held_offline_past_the_idle_close_continues_in_a_linked_session(voice, login_client, monkeypatch):
    """The server idle-closes a recording after VOICE_SESSION_IDLE_S with no
    part; the browser still holds what it recorded meanwhile, which continues
    the SAME container stream. It opens a continuation, sends the held bytes
    as that session's parts, and every word comes back: the first session's
    words in its own transcript, the rest in the continuation's."""
    script, data = recording(voice.tmp, 70.0, seed=13)
    cut_s = _cut_in_a_pause(script, 30.0)
    cut = 44 + int(cut_s * SR) * 2
    alice = login_client("alice")
    first = create(alice).json()["session_id"]
    for seq, part in enumerate(parts_of(data[:cut])):
        assert put(alice, first, seq, part).status_code == 200
    monkeypatch.setattr(settings, "voice_session_idle_s", 0.0)
    time.sleep(0.05)
    dictation.RUNNER.submit(dictation.maintain_once()).result(10)
    closed = put(alice, first, 99, data[cut:cut + 1000])
    assert closed.status_code == 409 and closed.json()["ended_by"] == "idle"

    r = alice.post("/audio/sessions", json={
        "client_key": str(uuid.uuid4()), "mime_type": "audio/wav", "part_ms": 5000, "continues": first,
    })
    assert r.status_code == 201, r.text
    second = r.json()["session_id"]
    assert r.json()["continues_session_id"] == first
    rest = parts_of(data[cut:])
    for seq, part in enumerate(rest):
        assert put(alice, second, seq, part).status_code == 200, "a continuation may open mid-stream"
    assert finish(alice, second, len(rest) - 1).status_code == 202
    done_first = wait_done(alice, first)
    done_second = wait_done(alice, second)
    assert done_first["ended_by"] == "idle" and done_first["outcome"] == "transcribed"
    assert done_second["outcome"] == "transcribed", done_second
    assert abs(done_second["audio_ms"] - (70_000 - int(cut_s * 1000))) <= 5, done_second["audio_ms"]
    offset = done_first["audio_ms"] / 1000.0
    joined = as_segments(all_segments(alice, first)) + [
        SimpleNamespace(text=s.text, start_s=s.start_s + offset, end_s=s.end_s + offset)
        for s in as_segments(all_segments(alice, second))
    ]
    verdict = fw.compare_to_script(joined, script)
    assert verdict["equal"], verdict
    listing = {s["session_id"]: s for s in alice.get("/audio/sessions").json()["sessions"]}
    assert listing[second]["continues_session_id"] == first and listing[first]["continues_session_id"] is None


def test_a_part_stored_while_the_worker_claims_its_lease_is_not_cut_off(voice, login_client, monkeypatch):
    """The worker starts when the session is created and reads the row when
    it claims its lease. A part stored after that read and before the
    worker's start-up tail cut was truncated away (the worker trusted its
    stale copy of bytes_stored), and every later part was then refused with
    503 storage_unavailable, because the file was shorter than the row. Seen
    once under load in the voice suite (the continuation test above, whose
    held parts arrive right after create); here the claim is held open so the
    order is certain."""
    _script, data = recording(voice.tmp, 20.0, seed=5)
    parts = parts_of(data)
    claimed, proceed = threading.Event(), threading.Event()
    real_claim = dictation._claim_lease

    def slow_claim(session_id: str):
        row = real_claim(session_id)
        claimed.set()
        proceed.wait(15)
        return row

    monkeypatch.setattr(dictation, "_claim_lease", slow_claim)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    assert claimed.wait(15), "the worker never claimed its lease"
    assert put(alice, sid, 0, parts[0]).status_code == 200
    proceed.set()
    until(lambda: getattr(dictation.RUNNER.get(sid), "started", False))
    time.sleep(0.3)
    for seq, part in enumerate(parts[1:], start=1):
        r = put(alice, sid, seq, part)
        assert r.status_code == 200, (seq, r.status_code, r.text)
    assert os.path.getsize(dictation.source_path(row_of(sid))) == len(data)
    assert finish(alice, sid, len(parts) - 1).status_code == 202
    done = wait_done(alice, sid)
    assert done["outcome"] == "transcribed", done
    assert abs(done["audio_ms"] - 20_000) <= 5, done["audio_ms"]


def test_only_an_idle_closed_session_of_your_own_can_be_continued_and_only_once(voice, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_max_bits_per_second", 256_000, raising=False)
    monkeypatch.setattr(settings, "voice_rate_slack_s", 10.0, raising=False)
    alice, bob = login_client("alice"), login_client("bob")
    _script, data = recording(voice.tmp, 12.0)
    finished, _d, _ = dictate(alice, data[:PART_BYTES])  # ended_by person

    def cont(client, previous):
        return client.post("/audio/sessions", json={
            "client_key": str(uuid.uuid4()), "mime_type": "audio/wav", "continues": previous,
        })

    refused = cont(alice, finished)
    assert refused.status_code == 409 and refused.json()["reason"] == "not_continuable"
    assert cont(bob, finished).status_code == 404
    assert cont(alice, uuid.uuid4().hex).status_code == 404
    idle = create(alice).json()["session_id"]
    assert put(alice, idle, 0, data[:PART_BYTES]).status_code == 200
    with db.connection() as con:
        # Its last part arrived ten minutes ago; the browser held the rest.
        con.execute(
            "UPDATE voice_sessions SET created_at = now() - interval '11 minutes', "
            "audio_since = now() - interval '11 minutes', last_part_at = now() - interval '10 minutes' WHERE id = %s",
            (idle,),
        )
    monkeypatch.setattr(settings, "voice_session_idle_s", 60.0)
    dictation.RUNNER.submit(dictation.maintain_once()).result(10)
    wait_done(alice, idle)
    first = cont(alice, idle)
    assert first.status_code == 201, first.text
    again = cont(alice, idle)
    assert again.status_code == 409 and again.json()["reason"] == "already_continued"
    assert again.json()["session_id"] == first.json()["session_id"]
    # Ten minutes of held audio arrives at once, and is not "too fast": the
    # clock of a continuation starts at the last part the server acknowledged.
    held = os.urandom(8 * PART_BYTES)
    for seq in range(8):
        r = put(alice, first.json()["session_id"], seq, held[seq * PART_BYTES:(seq + 1) * PART_BYTES])
        assert r.status_code in (200, 409), r.text
        if r.status_code == 409:  # the random bytes were found undecodable: fine here
            assert r.json()["ended_by"] == "undecodable"
            break


# ------------------------------------------------- 9. engine state honesty --


def test_waiting_on_says_engine_unavailable_while_the_engine_is_down(voice, login_client, monkeypatch):
    from app.video import transcribe as video_transcribe

    monkeypatch.setattr(video_transcribe, "_backoff_s", lambda attempt: 1.5)
    voice.fake.fail_status = 503
    _script, data = recording(voice.tmp, 20.0)
    alice = login_client("alice")
    sid = create(alice).json()["session_id"]
    chunks = parts_of(data)
    for seq, part in enumerate(chunks):
        assert put(alice, sid, seq, part).status_code == 200
    finish(alice, sid, len(chunks) - 1)
    seen = set()
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline and "engine_unavailable" not in seen:
        seen.add(alice.get(f"/audio/sessions/{sid}").json()["waiting_on"])
        time.sleep(0.05)
    assert "engine_unavailable" in seen, seen


def test_the_engine_state_tells_busy_from_hung(monkeypatch):
    gate = asr._SessionGate()
    assert gate.engine_state() == "busy"
    gate.last_unavailable = time.monotonic()
    assert gate.engine_state() == "unavailable"
    gate.last_ok = time.monotonic() + 1
    assert gate.engine_state() == "busy"
    monkeypatch.setattr(settings, "voice_engine_stall_s", 5.0, raising=False)
    gate.inflight[1] = time.monotonic() - 6.0
    assert gate.engine_state() == "unavailable", "a window stuck in the engine means it is hung"


# ------------------------------------------ 10. reviews 8, 11 and 13 --


def test_the_lock_map_does_not_grow_with_ids_nobody_owns(voice, login_client):
    alice = login_client("alice")
    for i in range(50):
        assert alice.delete(f"/audio/sessions/{uuid.uuid4().hex}").status_code == 404
        assert alice.delete(f"/audio/sessions/{'x' * 2000}{i}").status_code == 404
    sid, _done, _ = dictate(alice, recording(voice.tmp, 6.0)[1])
    assert alice.delete(f"/audio/sessions/{sid}").status_code == 204
    locks = getattr(dictation, "_locks", None)
    if locks is None:
        locks = getattr(dictation, "_append_locks")
    assert len(locks) == 0, f"{len(locks)} locks kept after every request ended"


def test_at_most_two_long_polls_per_person(voice, login_client):
    clients = [login_client("alice") for _ in range(3)]
    sid = create(clients[0]).json()["session_id"]
    rev = clients[0].get(f"/audio/sessions/{sid}").json()["rev"]
    results: Dict[int, Any] = {}

    def poll(i):
        results[i] = clients[i].get(f"/audio/sessions/{sid}", params={"since_rev": rev + 1000, "wait_s": 4})

    threads = [threading.Thread(target=poll, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    time.sleep(1.0)
    third = clients[2].get(f"/audio/sessions/{sid}", params={"since_rev": rev + 1000, "wait_s": 4})
    for t in threads:
        t.join()
    assert third.status_code == 429 and third.json()["reason"] == "too_many_polls", third.text
    assert all(results[i].status_code == 200 for i in range(2))
    assert clients[2].get(f"/audio/sessions/{sid}", params={"wait_s": 0}).status_code == 200, "a plain read is not a poll"
    after = clients[2].get(f"/audio/sessions/{sid}", params={"since_rev": rev + 1000, "wait_s": 0.5})
    assert after.status_code == 200, "the count was given back"


def test_the_json_routes_refuse_a_body_that_is_not_declared_json(voice, login_client):
    alice = login_client("alice")
    body = json.dumps({"client_key": str(uuid.uuid4()), "mime_type": "audio/wav"})
    r = alice.post("/audio/sessions", content=body, headers={"content-type": "text/plain"})
    assert r.status_code == 415 and r.json()["reason"] == "bad_content_type", r.text
    sid = create(alice).json()["session_id"]
    stop = alice.post(f"/audio/sessions/{sid}/finish", content=json.dumps({"ended_by": "person"}), headers={"content-type": "text/plain"})
    assert stop.status_code == 415
    assert row_of(sid)["status"] == "recording", "a cross-site form cannot stop somebody's recording"
    again = alice.post(f"/audio/sessions/{sid}/retranscribe", content="{}", headers={"content-type": "application/x-www-form-urlencoded"})
    assert again.status_code == 415
