"""`POST /v1/audio/transcriptions` over real HTTP, real keys and a real database.

The speech replicas are `publicapi_fake_whisper.FakeWhisper` — the speech
server's contract over a decodable synthetic recording — reached through
`sidecars._transport`, and the decoder is the ffmpeg stand-in that honours the
real argv. Everything between the socket and those stand-ins is production
code: the streaming multipart reader, the disk sink, the job registry and its
windows, the patient `asr` gate, the committed response, the ledger.

WHAT CHANGED ON 2026-09-14 (no-timeout design, sidecars_and_audio). The route
used to read the clip into memory, refuse it past 300 s, wait 30 s for the gate
and 240 s for the engine. These tests pin the replacement: any duration in
windows, the body streamed to disk, a gate with no deadline, a committed 200
with keep-alive bytes for a slow job, `stream`, `file_id`, and the gateway's
re-attach.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time

import httpx
import pytest
from fastapi import FastAPI

from app.apiplatform import projects
from app.apiplatform.scopes import Scope
from app.config import settings
from app.publicapi import audio_jobs, capacity, disk_ledger, endpoints, events
from app.publicapi import router as public_router
from app.publicapi import sidecars
from tests import publicapi_fake_whisper as fw
from tests.publicapi_sidecar_support import (  # noqa: F401 - fixtures by name
    ASR_URLS,
    OTHER_WORKSPACE,
    TOKENS,
    WORKSPACE,
    _pepper,
    api,
    assert_nothing_internal,
    auth,
    daily,
    engines_configured,
    platform,
    usage_events,
)

URL = "/v1/audio/transcriptions"
_SPEECH: dict = {}


def speech(tmp_dir: str, seconds: float, seed: int = 5):
    """(script, wav bytes) of a decodable synthetic recording, built once."""
    key = (seconds, seed)
    if key not in _SPEECH:
        script = fw.build_script(seconds, seed=seed)
        path = os.path.join(tmp_dir, f"speech-{int(seconds)}-{seed}.wav")
        fw.write_speech_wav(path, script, seed=seed * 13)
        with open(path, "rb") as fh:
            _SPEECH[key] = (script, fh.read())
    return _SPEECH[key]


@pytest.fixture(scope="module")
def speech_dir(tmp_path_factory):
    return str(tmp_path_factory.mktemp("speech"))


@pytest.fixture()
def fleet(engines_configured, tmp_path, monkeypatch):
    """Two fake replicas at the configured speech URLs, the fake decoder, and
    a job registry rooted in this test's directory."""
    tools = fw.install_fake_media_tools(str(tmp_path / "bin"))
    monkeypatch.setenv("FAKE_FFMPEG_LOG", str(tmp_path / "ffmpeg.jsonl"))
    monkeypatch.setenv("FAKE_FFMPEG_RUNDIR", str(tmp_path / "running"))
    monkeypatch.setenv("PUBLIC_API_ASR_HEALTH_POLL_S", "0.05")
    # The tmp root may share a device with the default Files store; the Files
    # watermark (250 GiB) is not what these tests are about (audio_windows).
    monkeypatch.setenv("PUBLIC_API_FILES_MIN_FREE_GIB", "0")
    monkeypatch.setattr(capacity, "YIELD_STEP_S", 0.02)
    disk_ledger.reset_for_tests()
    fake = fw.Fleet()
    assert fake.urls() == list(ASR_URLS)
    sidecars._transport = fake.transport()
    root = str(tmp_path / "asr")
    registry = audio_jobs.AudioJobs(
        root=root, decoder=audio_jobs.Decoder(ffmpeg=tools["ffmpeg"], ffprobe=tools["ffprobe"])
    )
    audio_jobs.reset_for_tests(registry)
    fake.root = root  # type: ignore[attr-defined]
    fake.jobs = registry  # type: ignore[attr-defined]
    yield fake
    audio_jobs.reset_for_tests(None)
    disk_ledger.reset_for_tests()


def _post(api, audio: bytes, headers=None, *, fields=None, content_type="audio/wav", filename="clip.wav"):
    data = {"model": "techsara-whisper"}
    data.update(fields or {})
    return api.post(
        URL,
        data=data,
        files={"file": (filename, audio, content_type)},
        headers=headers if headers is not None else auth(),
    )


def windows(fake: fw.Fleet):
    return [call for call in fake.calls if "seconds" in call]


def _words(text: str):
    return fw.normalized_words(text)


# ------------------------------------------------------------- the answer --


def test_a_recording_is_transcribed_as_json_with_duration_usage_and_nothing_internal(api, fleet, speech_dir):
    script, audio = speech(speech_dir, 20)

    response = _post(api, audio)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"text", "usage"}
    assert _words(payload["text"]) == [fw.norm_word(w.text) for w in script.words]
    assert payload["usage"] == {"type": "duration", "seconds": 20}
    assert_nothing_internal(response)


def test_text_format_is_a_plain_text_body(api, fleet, speech_dir):
    script, audio = speech(speech_dir, 20)

    response = _post(api, audio, fields={"response_format": "text"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert _words(response.text) == [fw.norm_word(w.text) for w in script.words]


def test_verbose_json_names_the_language_and_times_segments_in_the_recording(api, fleet, speech_dir):
    script, audio = speech(speech_dir, 20)

    response = _post(api, audio, fields={"response_format": "verbose_json", "timestamp_granularities[]": "segment"})

    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) == {"task", "language", "duration", "text", "segments", "usage"}
    assert payload["task"] == "transcribe" and payload["language"] == "english"
    assert payload["duration"] == 20.0
    assert [segment["id"] for segment in payload["segments"]] == list(range(len(payload["segments"])))
    assert payload["segments"][-1]["end"] <= 20.0
    assert "processing_ms" not in response.text and "no_speech_prob" not in response.text


def test_a_forced_language_reaches_the_engine_on_every_window(api, fleet, speech_dir):
    _, audio = speech(speech_dir, 20)

    response = _post(api, audio, fields={"language": "HI"})

    assert response.status_code == 200
    assert {call["language"] for call in windows(fleet)} == {"hi"}


def test_public_windows_go_to_the_replica_dictation_reaches_for_last(api, fleet, speech_dir):
    _, audio = speech(speech_dir, 20)

    _post(api, audio)

    assert fleet["asr-worker"].calls and not fleet["asr-head"].calls


def test_audio_longer_than_the_old_five_minute_limit_is_transcribed_in_windows_of_at_most_ninety_seconds(api, fleet, speech_dir, platform):
    script, audio = speech(speech_dir, 400)

    response = _post(api, audio)

    assert response.status_code == 200, response.text
    assert _words(response.json()["text"]) == [fw.norm_word(w.text) for w in script.words]
    assert response.json()["usage"]["seconds"] == 400
    sent = windows(fleet)
    assert len(sent) >= 5 and max(call["seconds"] for call in sent) <= 93.0
    row = usage_events("v1_audio_transcriptions")[0]
    assert row["meta"]["audio_seconds"] == 400 and row["meta"]["windows"] == len(sent)


def test_the_upload_streams_to_disk_and_is_never_spooled_by_starlette_or_left_behind(api, fleet, speech_dir, monkeypatch):
    """Starlette's UploadFile spools a part to an unbounded temporary file; this
    route streams it into a 0600 file under the disk ledger and the job removes
    it after decoding."""

    # The fake speech server in front of the transport is itself a FastAPI app
    # that parses its upload with UploadFile, so the refusal is scoped to the
    # orchestrator's host: only OUR route must never build a form.
    import starlette.requests

    real_form = starlette.requests.Request.form

    def form(self, *args, **kwargs):
        if self.url.hostname == "testserver":
            raise AssertionError("the transcription route parsed its body with request.form()")
        return real_form(self, *args, **kwargs)

    monkeypatch.setattr(starlette.requests.Request, "form", form)
    modes = []
    real_commit = disk_ledger.DiskSink._commit

    def recording_commit(self, final_path):
        real_commit(self, final_path)
        modes.append(os.stat(final_path).st_mode & 0o777)

    monkeypatch.setattr(disk_ledger.DiskSink, "_commit", recording_commit)
    _, audio = speech(speech_dir, 20)

    response = _post(api, audio)

    assert response.status_code == 200, response.text
    assert modes == [0o600]
    incoming = os.path.join(fleet.root, "incoming")
    assert os.listdir(incoming) == []


def test_stream_true_sends_a_ping_first_then_text_deltas_and_one_done_event(api, fleet, speech_dir):
    script, audio = speech(speech_dir, 200)

    response = _post(api, audio, fields={"stream": "true"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = response.text.splitlines()
    assert lines[0] == ": ping"
    assert not any(line.startswith(("id:", "retry:", "event:")) for line in lines)
    assert not any(line.startswith(": ts-seq") for line in lines)
    payloads = [json.loads(line[len("data: "):]) for line in lines if line.startswith("data: ")]
    assert payloads[-1]["type"] == "transcript.text.done"
    deltas = [p for p in payloads[:-1] if p["type"] == "transcript.text.delta"]
    assert len(deltas) == len(payloads) - 1 >= 2
    assert "".join(p["delta"] for p in deltas) == payloads[-1]["text"]
    assert _words(payloads[-1]["text"]) == [fw.norm_word(w.text) for w in script.words]


def test_a_slow_transcription_commits_to_200_with_keepalive_spaces_then_the_object(api, fleet, speech_dir, monkeypatch):
    monkeypatch.setattr(settings, "public_api_sync_commit_s", 0.1, raising=False)
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.1)
    for replica in fleet.replicas.values():
        replica.latency_s = 0.4
    _, audio = speech(speech_dir, 20)

    response = _post(api, audio, fields={"response_format": "text"})

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, no-transform"
    assert response.headers["content-type"].startswith("text/plain")
    assert response.content.startswith(b"  ")
    assert response.text.strip()


# ------------------------------------------------------ who may call it --


def test_a_request_without_a_key_is_a_401_and_never_reaches_the_engine(api, fleet, speech_dir):
    _, audio = speech(speech_dir, 20)

    response = _post(api, audio, headers={})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert fleet.calls == []


def test_a_key_without_audio_write_is_a_403_naming_the_scope(api, fleet, speech_dir):
    _, audio = speech(speech_dir, 20)

    response = _post(api, audio, headers=auth("narrow"))

    assert response.status_code == 403
    assert response.headers["WWW-Authenticate"] == 'Bearer error="insufficient_scope", scope="audio.write"'
    assert fleet.calls == []


def test_a_deployment_without_speech_does_not_offer_the_model(api, fleet, speech_dir, monkeypatch):
    monkeypatch.setattr(settings, "asr_enabled", False)
    _, audio = speech(speech_dir, 20)

    response = _post(api, audio)

    assert response.status_code == 404
    assert fleet.calls == []


# ------------------------------------------------------------ the body --


def test_a_file_over_the_audio_cap_is_a_413_while_reading_and_leaves_no_file(api, fleet, monkeypatch):
    monkeypatch.setattr(settings, "public_api_max_audio_bytes", 4096, raising=False)

    response = _post(api, b"RIFF" + b"\x00" * 9000)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert fleet.calls == []
    incoming = os.path.join(fleet.root, "incoming")
    assert not os.path.isdir(incoming) or os.listdir(incoming) == []


def test_a_declared_body_over_the_body_cap_is_refused_before_it_is_read(api, fleet, monkeypatch):
    monkeypatch.setattr(settings, "public_api_max_audio_body_bytes", 1024, raising=False)

    response = _post(api, b"RIFF" + b"\x00" * 4000)

    assert response.status_code == 413
    assert fleet.calls == []


@pytest.mark.parametrize(
    "fields, file, param",
    [
        ({"prompt": "names: Sara"}, ("a.wav", b"RIFF1234", "audio/wav"), "prompt"),
        ({"temperature": "0.2"}, ("a.wav", b"RIFF1234", "audio/wav"), "temperature"),
        ({"response_format": "srt"}, ("a.wav", b"RIFF1234", "audio/wav"), "response_format"),
        ({"language": "klingon"}, ("a.wav", b"RIFF1234", "audio/wav"), "language"),
        ({"stream": "maybe"}, ("a.wav", b"RIFF1234", "audio/wav"), "stream"),
        ({}, ("a.txt", b"hello", "text/plain"), "file"),
        ({}, ("a.wav", b"", "audio/wav"), "file"),
        ({}, None, "file"),
        ({"file_id": "file-" + "a" * 24}, ("a.wav", b"RIFF1234", "audio/wav"), "file_id"),
    ],
)
def test_a_form_the_engine_cannot_honour_is_a_400_naming_the_field(api, fleet, fields, file, param):
    data = {"model": "techsara-whisper", **fields}
    if file is None:
        response = api.post(URL, files=[(name, (None, value)) for name, value in data.items()], headers=auth())
    else:
        response = api.post(URL, data=data, files={"file": file}, headers=auth())

    assert response.status_code == 400, response.text
    assert response.json()["error"]["param"] == param
    assert fleet.calls == []
    incoming = os.path.join(fleet.root, "incoming")
    assert not os.path.isdir(incoming) or os.listdir(incoming) == []


def test_a_json_body_names_a_file_id_or_is_a_400(api, fleet):
    missing = api.post(URL, json={"model": "techsara-whisper"}, headers=auth())
    extra = api.post(URL, json={"model": "techsara-whisper", "file_id": "file-x", "prompt": "hi"}, headers=auth())

    assert missing.status_code == 400 and missing.json()["error"]["param"] == "file_id"
    assert extra.status_code == 400 and extra.json()["error"]["param"] == "prompt"


def test_an_idempotency_key_is_refused_before_the_upload_is_read(api, fleet, speech_dir):
    _, audio = speech(speech_dir, 20)

    response = _post(api, audio, headers={**auth(), "Idempotency-Key": "k-1"})

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "Idempotency-Key"
    assert fleet.calls == []


def test_undecodable_audio_is_a_400_with_a_fixed_sentence_never_the_decoder_output(api, fleet):
    response = _post(api, b"\x01\x02" * 5000)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["message"] == "The audio could not be decoded. Send a supported audio file."
    assert error["param"] == "file"
    assert "/" not in error["message"]
    assert fleet.calls == []


# ---------------------------------------------------------- the engine --


def test_every_replica_refusing_connections_is_waited_out_and_the_transcript_completes(api, fleet, speech_dir, monkeypatch):
    """No clock: both replicas down at first (a restart) is a wait with
    backoff, not the old immediate 503."""
    fleet["asr-head"].down = True
    fleet["asr-worker"].down = True
    timer = threading.Timer(1.2, lambda: [setattr(r, "down", False) for r in fleet.replicas.values()])
    timer.start()
    script, audio = speech(speech_dir, 20)

    response = _post(api, audio)
    timer.cancel()

    assert response.status_code == 200, response.text
    assert _words(response.json()["text"]) == [fw.norm_word(w.text) for w in script.words]


def test_replicas_down_for_the_whole_grace_are_a_retryable_503_that_names_no_host(api, fleet, speech_dir, platform, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_ASR_UNAVAILABLE_GRACE_S", "0")
    for replica in fleet.replicas.values():
        replica.down = True
    _, audio = speech(speech_dir, 20)

    response = _post(api, audio)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert int(response.headers["Retry-After"]) >= 1
    assert_nothing_internal(response)
    rows = usage_events("v1_audio_transcriptions")
    assert [(row["status"], row["input_tokens"], row["output_tokens"]) for row in rows] == [("error", None, None)]


def test_the_speech_gate_is_taken_fleet_wide_with_no_deadline_and_yields_to_chat(api, fleet, speech_dir, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_GATE_WAIT_S", "0.01")
    taken = []
    real_hold = capacity.hold

    def recording_hold(gate, **kwargs):
        taken.append((gate, kwargs.get("wait_s", "absent"), kwargs.get("yield_to_chat")))
        return real_hold(gate, **kwargs)

    monkeypatch.setattr(capacity, "hold", recording_hold)
    _, audio = speech(speech_dir, 20)

    response = _post(api, audio)

    assert response.status_code == 200
    assert taken and set(taken) == {("asr", None, True)}


def test_a_window_waits_behind_a_full_speech_gate_past_the_old_budget_instead_of_a_503(api, fleet, speech_dir, monkeypatch):
    """The fleet-wide gate held by another public job for longer than the
    retired PUBLIC_API_GATE_WAIT_S: the request waits and completes."""
    monkeypatch.setenv("PUBLIC_API_GATE_WAIT_S", "0.05")
    monkeypatch.setattr(settings, "public_api_sync_commit_s", 0.1, raising=False)
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0.1)
    fleet["asr-worker"].latency_s = 1.0
    _, first = speech(speech_dir, 20)
    _, second = speech(speech_dir, 20, seed=9)
    results = {}

    def one(name, audio):
        results[name] = _post(api, audio)

    threads = [threading.Thread(target=one, args=("a", first)), threading.Thread(target=one, args=("b", second))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert results["a"].status_code == results["b"].status_code == 200
    assert fleet.peak_in_flight == 1


# ------------------------------------------------------------ the disk --


def test_a_decode_the_disk_cannot_hold_is_a_503_retry_after_60_before_any_decoder_starts(api, fleet, speech_dir, platform, monkeypatch):
    _, audio = speech(speech_dir, 20)
    real_check = disk_ledger.DiskLedger.check

    def refuse_the_decode(self, nbytes, *, purpose="check"):
        if purpose == "asr-preflight":
            raise disk_ledger.DiskFull(nbytes, 0, purpose)
        return real_check(self, nbytes, purpose=purpose)

    monkeypatch.setattr(disk_ledger.DiskLedger, "check", refuse_the_decode)

    response = _post(api, audio)

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "60"
    assert fleet.calls == []
    assert fw.read_tool_log(os.environ["FAKE_FFMPEG_LOG"]) == []
    assert usage_events("v1_audio_transcriptions") == []


# ------------------------------------------------------------ metering --


def test_a_transcription_is_one_usage_row_with_seconds_and_no_invented_tokens(api, fleet, speech_dir, platform):
    _, audio = speech(speech_dir, 20)

    response = _post(api, audio, fields={"language": "en"})

    assert response.status_code == 200
    rows = usage_events("v1_audio_transcriptions")
    assert len(rows) == 1
    row = rows[0]
    assert (row["status"], row["model"], row["input_tokens"], row["output_tokens"]) == (
        "ok",
        "techsara-whisper",
        None,
        None,
    )
    assert row["generation_id"].startswith("asr_")
    assert row["meta"]["audio_seconds"] == 20
    assert row["meta"]["language_forced"] is True
    assert row["meta"]["response_format"] == "json"
    assert daily(platform["project"]["id"]) == {
        "requests": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "errors": 0,
    }


# ------------------------------------------------------------- file_id --


def _stored_file(project_id: str, audio: bytes, tmp_path, monkeypatch, *, kind="audio", mime="audio/wav", workspace=WORKSPACE):
    import hashlib

    from app.apifiles import schema, storage

    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "files"))
    sha = hashlib.sha256(audio).hexdigest()

    def place(row, created):
        path = storage.original_path(project_id, sha)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(audio)

    row, _blob, _created = schema.create_api_file_with_blob(
        project_id, workspace, None, sha256=sha, bytes=len(audio), kind=kind, mime_type=mime, lane="media",
        filename="meeting.wav", purpose="user_data", origin="file", expires_after_seconds=None, place_bytes=place,
    )
    return row["id"], storage.original_path(project_id, sha)


def test_a_file_id_of_this_project_is_transcribed_in_place_and_left_there(api, fleet, speech_dir, platform, tmp_path, monkeypatch):
    script, audio = speech(speech_dir, 20)
    file_id, path = _stored_file(platform["project"]["id"], audio, tmp_path, monkeypatch)

    by_json = api.post(URL, json={"model": "techsara-whisper", "file_id": file_id}, headers=auth())
    by_form = api.post(
        URL, files=[("model", (None, "techsara-whisper")), ("file_id", (None, file_id))], headers=auth()
    )

    assert by_json.status_code == 200, by_json.text
    assert _words(by_json.json()["text"]) == [fw.norm_word(w.text) for w in script.words]
    assert by_form.status_code == 200 and by_form.json() == by_json.json()
    assert os.path.exists(path)


def test_a_file_id_needs_files_read_and_another_projects_id_is_the_same_404_as_none(api, fleet, speech_dir, platform, tmp_path, monkeypatch):
    _, audio = speech(speech_dir, 20)
    theirs, _path = _stored_file(platform["other"]["id"], audio, tmp_path, monkeypatch, workspace=OTHER_WORKSPACE)
    no_files_read = projects.create_key(
        platform["project"]["id"], WORKSPACE, "audio-only", scopes=[Scope.AUDIO_WRITE.value]
    )

    foreign = api.post(URL, json={"model": "techsara-whisper", "file_id": theirs}, headers=auth())
    absent = api.post(URL, json={"model": "techsara-whisper", "file_id": "file-" + "0" * 24}, headers=auth())
    malformed = api.post(URL, json={"model": "techsara-whisper", "file_id": "not-an-id"}, headers=auth())
    unscoped = api.post(
        URL, json={"model": "techsara-whisper", "file_id": theirs},
        headers={"Authorization": f"Bearer {no_files_read.token}"},
    )

    assert foreign.status_code == absent.status_code == malformed.status_code == 404
    assert foreign.json()["error"]["message"] == absent.json()["error"]["message"] == malformed.json()["error"]["message"]
    assert unscoped.status_code == 403
    assert fleet.calls == []


def test_a_file_id_that_is_not_audio_or_video_is_a_400(api, fleet, platform, tmp_path, monkeypatch):
    file_id, _ = _stored_file(platform["project"]["id"], b"%PDF-1.7 not audio", tmp_path, monkeypatch, kind="pdf", mime="application/pdf")

    response = api.post(URL, json={"model": "techsara-whisper", "file_id": file_id}, headers=auth())

    assert response.status_code == 400 and response.json()["error"]["param"] == "file_id"


# -------------------------------------------------------- the gateway --


def _app():
    endpoints.register(public_router.router)
    app = FastAPI()
    app.include_router(public_router.router)
    return app


def test_a_trusted_gateway_reattaches_to_a_running_stream_from_where_it_left_off(fleet, speech_dir, platform, monkeypatch):
    """`X-TechSara-Run: job:<key>-<format>` names the job; the gateway's empty
    re-POST with Attach-Job and Resume-After N continues at data frame N+1
    with `: ts-seq` markers, and is neither counted nor metered again."""
    monkeypatch.setenv("PUBLIC_API_GATEWAY_PEERS", "10.231.231.2")
    for replica in fleet.replicas.values():
        replica.latency_s = 0.3
    script, audio = speech(speech_dir, 200)
    app = _app()

    async def scenario():
        transport = httpx.ASGITransport(app=app, client=("10.231.231.2", 40000))
        async with httpx.AsyncClient(transport=transport, base_url="http://orchestrator") as client:
            first = asyncio.ensure_future(
                client.post(
                    URL,
                    data={"model": "techsara-whisper", "stream": "true"},
                    files={"file": ("a.wav", audio, "audio/wav")},
                    headers={**auth(), "X-TechSara-Attempt": "attempt-0001-abcd"},
                )
            )
            deadline = time.monotonic() + 30
            job = None
            while time.monotonic() < deadline:
                running = list(fleet.jobs._running.values())
                if running and len([e for e in running[0]._events if e.type == "delta"]) >= 1:
                    job = running[0]
                    break
                await asyncio.sleep(0.02)
            assert job is not None
            again = await client.post(
                URL,
                content=b"",
                headers={
                    **auth(),
                    "X-TechSara-Attempt": "attempt-0001-abcd",
                    "X-TechSara-Attach-Job": f"{job.key}-json",
                    "X-TechSara-Resume-After": "1",
                },
            )
            return await first, again, job

    first, again, job = asyncio.run(scenario())

    assert first.status_code == again.status_code == 200
    assert first.headers["X-TechSara-Run"] == again.headers["X-TechSara-Run"] == f"job:{job.key}-json"

    def data_frames(text):
        frames, numbers = [], []
        for block in text.split("\n\n"):
            if block.startswith("data: "):
                frames.append(json.loads(block[len("data: "):]))
            elif block.startswith(": ts-seq="):
                numbers.append(int(block.split("=", 1)[1]))
        return frames, numbers

    original, original_numbers = data_frames(first.text)
    resumed, resumed_numbers = data_frames(again.text)
    assert original_numbers == list(range(1, len(original) + 1))
    assert resumed == original[1:]
    assert resumed_numbers == list(range(2, len(original) + 1))
    assert _words(resumed[-1]["text"]) == [fw.norm_word(w.text) for w in script.words]
    assert len(usage_events("v1_audio_transcriptions")) == 1
    assert daily(platform["project"]["id"])["requests"] == 1


def test_attach_headers_from_an_untrusted_peer_are_ignored_and_another_projects_job_is_a_404(fleet, speech_dir, platform, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_GATEWAY_PEERS", "10.231.231.2")
    _, audio = speech(speech_dir, 20)
    app = _app()

    async def scenario():
        trusted = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("10.231.231.2", 1)), base_url="http://o")
        stranger = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 1)), base_url="http://o")
        async with trusted, stranger:
            done = await trusted.post(
                URL, data={"model": "techsara-whisper"}, files={"file": ("a.wav", audio, "audio/wav")},
                headers={**auth(), "X-TechSara-Attempt": "attempt-0002-abcd"},
            )
            run = done.headers["X-TechSara-Run"][len("job:"):]
            attach = {"X-TechSara-Attempt": "attempt-0002-abcd", "X-TechSara-Attach-Job": run}
            by_stranger = await stranger.post(URL, content=b"", headers={**auth(), **attach})
            by_other_project = await trusted.post(URL, content=b"", headers={"Authorization": f"Bearer {TOKENS['other']}", **attach})
            by_owner = await trusted.post(URL, content=b"", headers={**auth(), **attach})
            return done, by_stranger, by_other_project, by_owner

    done, by_stranger, by_other_project, by_owner = asyncio.run(scenario())

    assert done.status_code == 200
    # From a stranger the headers do not exist: an empty body is just malformed.
    assert by_stranger.status_code == 400 and "X-TechSara-Run" not in by_stranger.headers
    assert by_other_project.status_code == 404
    assert by_owner.status_code == 200 and by_owner.json() == done.json()
