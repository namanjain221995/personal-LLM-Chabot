"""Voice dictation, server side: the gate, the audio, the engine, the row.

Dictation is the one route on this platform that takes a MICROPHONE, and four
properties are what make that safe to leave switched on.

  1. THE GATE IS THE SERVER. The composer hides the microphone from an account
     that may not dictate, and hiding is a courtesy. A stale tab, a second
     browser and a curl command all arrive at POST /audio/transcribe anyway,
     so that is where signed-out is 401, feature-off is 403, and a deployment
     with no speech engine is 404.

  2. NOTHING REACHES THE GPU BY ACCIDENT. Format, duration and size are
     decided from the request alone, before a byte is handed to the engine —
     and the size cap is enforced WHILE READING, so a hostile body is refused
     rather than buffered. The engine shares hardware with the chat model, and
     a person waiting for an ANSWER must never queue behind someone's audio.

  3. THE ENGINE'S TROUBLE IS NOT THE PERSON'S. Busy, refused and unreachable
     each become a status the browser can act on and a sentence a person can
     read. Not one of them names the model, the engine's URL or the provider:
     a member has no reason to learn this platform's internals from a
     dictation box.

  4. THE ROW REMEMBERS THE ATTEMPT, NEVER THE WORDS. Every attempt writes one
     metadata row — the failures included, because an error rate computed from
     successes alone is not an error rate — and no column of that row holds a
     syllable of what was said.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import asr, audio_api, db
from app.config import settings

#: Deliberately distinctive, so the "nothing leaks" test is looking for
#: strings that could only have come from the server's own configuration.
ENGINE_URL = "http://asr-node.internal:30006/v1"
ENGINE_MODEL = "Qwen/Qwen3-ASR-1.7B-worker-build"
ENGINE_NAME = "qwen3_asr_worker"

#: What the fake engine hears. Chosen to be the sort of thing a person would
#: be appalled to find in a telemetry table.
SPOKEN = "the door code is four four one seven"

#: An EBML header and enough padding to clear _MIN_BYTES. The route never
#: decodes audio, so shape is all this needs.
WEBM = b"\x1a\x45\xdf\xa3" + b"\x00" * 8192

#: What a socket hands over at a time, for the fake body below.
_SLICE = 64 * 1024


class FakeEngine:
    """Qwen3-ASR's stand-in: records what it was asked, answers as instructed.

    Every test but the pure parsing ones goes through this, so a test failure
    is always about the orchestrator and never about a GPU being busy.
    """

    name = ENGINE_NAME
    model = ENGINE_MODEL

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.raises: BaseException | None = None
        #: Seconds this engine pretends to take, before it answers OR fails.
        #: Zero for most tests; the telemetry tests raise it, because an
        #: assertion that a measured wait is >= 0 would hold even if nothing
        #: were measured at all.
        self.delay = 0.0
        self.transcript = asr.Transcript(
            text=SPOKEN,
            language="English",
            language_code="en",
            provider=ENGINE_NAME,
            model=ENGINE_MODEL,
            engine_ms=41,
        )

    async def transcribe(self, audio, *, filename, content_type, language=""):
        self.calls.append(
            {
                "bytes": len(audio),
                "filename": filename,
                "content_type": content_type,
                "language": language,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.transcript

    async def health(self) -> bool:
        return True


@pytest.fixture()
def engine(monkeypatch):
    """A deployment WITH a speech engine, and clean admission control."""
    monkeypatch.setattr(settings, "asr_enabled", True)
    monkeypatch.setattr(settings, "asr_base_url", ENGINE_URL)
    monkeypatch.setattr(settings, "asr_model", ENGINE_MODEL)
    # The rate window and the pool are process-wide. Left alone, the twentieth
    # request of the file would 429 inside whichever test happened to be
    # twentieth, and the semaphore from the concurrency test would still hold
    # one permit against a dead event loop.
    audio_api.reset_for_tests()
    # conftest's truncation list predates V19. `users` CASCADE reaches this
    # table today, but a test that counts rows should not rest on that.
    with db.connection() as con:
        con.execute("TRUNCATE TABLE voice_transcriptions RESTART IDENTITY")
    fake = FakeEngine()
    asr.set_provider(fake)
    yield fake
    asr.set_provider(None)
    audio_api.reset_for_tests()


def _post(
    client,
    *,
    body: bytes = WEBM,
    content_type: str = "audio/webm",
    duration_ms: int = 4200,
    language: str = "auto",
):
    """One recording, exactly as the browser sends it: the audio IS the body.

    There is no multipart form and no filename — see the route's docstring for
    why. Everything beside the bytes is a query parameter.
    """
    return client.post(
        "/audio/transcribe",
        content=body,
        headers={"content-type": content_type},
        params={"duration_ms": str(duration_ms), "language": language},
    )


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def _rows() -> list[dict]:
    with db.connection() as con:
        return [
            dict(row)
            for row in con.execute(
                "SELECT * FROM voice_transcriptions ORDER BY id"
            ).fetchall()
        ]


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_a_signed_out_request_cannot_reach_the_microphone(engine, anonymous_mode):
    """An open ASR endpoint on a shared GPU is a free denial of service
    against the chat model, so this is 401 before anything else happens."""
    from app.main import app

    response = _post(TestClient(app))
    assert response.status_code == 401
    assert engine.calls == []


def test_a_member_whose_voice_input_is_off_is_refused_although_signed_in(
    engine, login_client
):
    """The composer hides the microphone for this account. Hiding is a
    courtesy: a stale tab still has the button, and audio still arrives."""
    root = login_client("root", role="super_admin")
    bob = login_client("bob")
    granted = _post(bob)
    assert granted.status_code == 200, granted.text

    revoked = root.put(
        f"/admin/api/members/{_uid('bob')}/access",
        json={"features": {"voice_input": False}},
    )
    assert revoked.status_code == 200, revoked.text

    response = _post(bob)
    assert response.status_code == 403
    assert "administrator" in response.json()["detail"]


def test_one_persons_revoked_microphone_does_not_close_anybody_elses(
    engine, login_client
):
    """A member override that leaked to the workspace would silently switch
    dictation off for everyone the next time one person misbehaved."""
    root = login_client("root", role="super_admin")
    bob = login_client("bob")
    carol = login_client("carol")
    root.put(
        f"/admin/api/members/{_uid('bob')}/access",
        json={"features": {"voice_input": False}},
    )

    assert _post(bob).status_code == 403
    assert _post(carol).status_code == 200


def test_a_deployment_without_a_speech_engine_answers_not_found(
    engine, login_client, monkeypatch
):
    """404, not 503: a deployment that never started scripts/asr.sh does not
    HAVE this feature, and 503 would promise it is coming back."""
    monkeypatch.setattr(settings, "asr_enabled", False)
    bob = login_client("bob")

    response = _post(bob)
    assert response.status_code == 404
    assert engine.calls == []


def test_the_feature_gate_answers_before_the_handler_learns_asr_is_off(
    engine, login_client, monkeypatch
):
    """Both switches off at once, which is what a deployment that never
    enabled voice actually looks like.

    require_voice is a DEPENDENCY and settings.asr_enabled is checked in the
    handler body, so the account-level refusal wins and the answer is 403 —
    not the 404 the ordering in the docstring might suggest. Pinned because a
    future reader who "fixes" this to 404 would be telling a member with no
    microphone permission that the route does not exist, and the two refusals
    mean different things to the composer.
    """
    root = login_client("root", role="super_admin")
    bob = login_client("bob")
    root.put(
        f"/admin/api/members/{_uid('bob')}/access",
        json={"features": {"voice_input": False}},
    )
    monkeypatch.setattr(settings, "asr_enabled", False)

    response = _post(bob)
    assert response.status_code == 403
    assert engine.calls == []


# ---------------------------------------------------------------------------
# The audio
# ---------------------------------------------------------------------------


def test_a_recording_comes_back_as_text_a_language_and_timings(engine, login_client):
    bob = login_client("bob")

    response = _post(bob, duration_ms=4200)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["text"] == SPOKEN
    assert body["language"] == "English"
    assert body["language_code"] == "en"
    # The console reports how long people speak and how long they wait; both
    # halves have to survive the route.
    assert body["duration_ms"] == 4200
    assert body["engine_ms"] == 41
    assert isinstance(body["processing_ms"], int)
    assert isinstance(body["upload_ms"], int)

    assert len(engine.calls) == 1
    assert engine.calls[0]["bytes"] == len(WEBM)


@pytest.mark.parametrize("content_type", ["application/pdf", "text/plain"])
def test_a_body_that_is_not_audio_is_refused_by_its_type(
    engine, login_client, content_type
):
    """The CONTENT TYPE is the only claim a client gets to make about these
    bytes, and it is checked before a decoder ever sees them."""
    bob = login_client("bob")

    response = _post(bob, content_type=content_type)
    assert response.status_code == 415
    assert content_type in response.json()["detail"]
    assert engine.calls == []


def test_a_tap_on_the_microphone_button_is_not_sent_to_a_gpu(engine, login_client):
    """A few hundred bytes is a mis-click, not speech. Transcribing it would
    spend a GPU slot to answer with an empty string."""
    bob = login_client("bob")

    response = _post(bob, body=b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
    assert response.status_code == 422
    assert "too short" in response.json()["detail"]
    assert engine.calls == []


def test_a_body_over_the_cap_is_refused_and_never_reaches_the_engine(
    engine, login_client, monkeypatch
):
    bob = login_client("bob")
    monkeypatch.setattr(settings, "asr_max_upload_bytes", 8 * 1024)

    response = _post(bob, body=b"\x00" * (64 * 1024))
    assert response.status_code == 413
    assert engine.calls == []


def test_the_size_cap_is_enforced_while_reading_not_after(monkeypatch):
    """The failure this prevents is an out-of-memory kill of the orchestrator.

    Reading the upload whole and THEN measuring it would let anyone with a
    session allocate a hundred megabytes per request, on the same container
    that streams everybody's answers. `_read_capped` is driven directly here
    because that is the only place the distinction is observable: an
    end-to-end 413 looks identical either way.
    """
    monkeypatch.setattr(settings, "asr_max_upload_bytes", 2 * 1024 * 1024)

    class HostileUpload:
        """A hundred megabytes, made up as it is read, counting what it gave.

        Shaped like a Request because that is what `_read_capped` consumes:
        the recording arrives as the request body, so there is no multipart
        parser between the socket and this loop to buffer it first.
        """

        def __init__(self, total: int) -> None:
            self.remaining = total
            self.served = 0

        async def stream(self):
            while self.remaining:
                n = min(_SLICE, self.remaining)
                self.remaining -= n
                self.served += n
                yield b"\x00" * n

    upload = HostileUpload(100 * 1024 * 1024)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(audio_api._read_capped(upload))

    assert raised.value.status_code == 413
    # One slice past the cap is what it takes to know the cap was passed;
    # anything approaching the full 100 MB means the body was buffered first.
    assert upload.served <= settings.asr_max_upload_bytes + _SLICE
    assert upload.remaining > 90 * 1024 * 1024


def test_a_recording_longer_than_the_ceiling_is_refused_before_any_gpu_work(
    engine, login_client, monkeypatch
):
    """The duration the browser reports is advisory, and it is used for
    exactly this: refusing an hour of audio without paying for an hour of
    audio first."""
    monkeypatch.setattr(settings, "asr_max_audio_seconds", 600)
    bob = login_client("bob")

    response = _post(bob, duration_ms=601_000)
    assert response.status_code == 413
    assert "minutes" in response.json()["detail"]
    assert engine.calls == []


@pytest.mark.parametrize(
    "content_type, expected_name",
    [("video/webm", "recording.webm"), ("video/mp4", "recording.mp4")],
)
def test_a_browsers_video_label_for_an_audio_only_recording_is_accepted(
    engine, login_client, content_type, expected_name
):
    """Chrome labels an audio-only MediaRecorder blob `video/webm` and Safari
    calls its own `video/mp4`. Refusing those on the word "video" would break
    dictation in both browsers while working perfectly in the test suite.

    The filename the engine's fallback endpoint needs is DERIVED from that
    content type here, not carried from the browser: nothing the client names
    travels into this process, so there is nothing named to end up in a log.
    """
    bob = login_client("bob")

    response = _post(bob, content_type=content_type)
    assert response.status_code == 200, response.text
    assert response.json()["text"] == SPOKEN
    assert engine.calls[0]["filename"] == expected_name


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure, status, phrase",
    [
        (asr.ASRBusy("every transcription slot is busy"), 503, "busy"),
        (asr.ASRRejected("unreadable container"), 422, "could not be transcribed"),
        (asr.ASRUnavailable(f"connection refused to {ENGINE_URL}"), 503, "try again"),
    ],
)
def test_an_engine_failure_becomes_a_status_and_a_sentence(
    engine, login_client, failure, status, phrase
):
    """Three different problems with three different answers, because the
    browser does different things with them — and none of them hands the
    person the exception the engine raised."""
    engine.raises = failure
    bob = login_client("bob")

    response = _post(bob)
    assert response.status_code == status
    detail = response.json()["detail"]
    assert phrase in detail.lower()
    assert detail.endswith(".")
    assert str(failure) not in detail
    assert ENGINE_URL not in detail


def test_a_stuck_client_is_rate_limited_after_its_share_of_a_minute(
    engine, login_client, monkeypatch
):
    """A retry loop in one tab must not be able to keep the ASR pool full."""
    monkeypatch.setattr(settings, "asr_rate_per_min", 3)
    bob = login_client("bob")

    assert [_post(bob).status_code for _ in range(3)] == [200, 200, 200]
    response = _post(bob)
    assert response.status_code == 429
    assert "Wait a moment" in response.json()["detail"]


def test_the_rate_limit_is_per_person_not_per_deployment(
    engine, login_client, monkeypatch
):
    """A shared counter would let one person's stuck tab silently mute the
    microphone for the whole workspace."""
    monkeypatch.setattr(settings, "asr_rate_per_min", 2)
    bob = login_client("bob")
    carol = login_client("carol")

    _post(bob), _post(bob)
    assert _post(bob).status_code == 429
    assert _post(carol).status_code == 200


def test_a_full_pool_refuses_rather_than_queueing_behind_work_nobody_can_see(
    engine, monkeypatch
):
    """The queue is short on purpose.

    Waiting indefinitely for a slot would leave a person watching a spinner
    with no way to tell that their recording is behind six other people's;
    ASRBusy becomes a 503 they can retry. Driven under asyncio directly
    because the second caller has to arrive while the first is still holding
    the only slot, which a synchronous TestClient cannot arrange.
    """
    monkeypatch.setattr(settings, "asr_max_concurrent", 1)
    monkeypatch.setattr(settings, "asr_queue_wait_s", 0.05)
    asr.POOL.reset_for_tests()

    class BlockingEngine(FakeEngine):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def transcribe(self, audio, *, filename, content_type, language=""):
            self.entered.set()
            await self.release.wait()
            return self.transcript

    async def scenario():
        blocking = BlockingEngine()
        asr.set_provider(blocking)
        holding = asyncio.create_task(
            asr.transcribe(WEBM, filename="a.webm", content_type="audio/webm")
        )
        await blocking.entered.wait()

        with pytest.raises(asr.ASRBusy):
            await asr.transcribe(WEBM, filename="b.webm", content_type="audio/webm")

        # And the slot is genuinely still occupied, not lost: the first caller
        # finishes normally once released.
        blocking.release.set()
        assert (await holding).text == SPOKEN

    asyncio.run(scenario())


def test_an_engine_failure_nobody_named_is_still_a_sentence_and_still_a_row(
    engine, login_client
):
    """The catch-all, and the reason `status = 'error'` exists in V19.

    The three ASR exceptions are the failures we thought of. A 200 whose body
    is an HTML proxy page, a reply shaped differently by a newer engine build,
    a bug in this client — any of those used to escape the route, and FastAPI
    answered 500: no sentence for the person holding the microphone, no row
    for the console, and an error rate that counted only the failures we had
    remembered to name.
    """
    engine.raises = ValueError("choices[0].message is not a dict")
    bob = login_client("bob")

    response = _post(bob)
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Transcription couldn't be completed. Please try again."
    )

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "error"


def test_an_engine_reply_this_client_cannot_read_is_engine_trouble(monkeypatch):
    """A 200 is not a promise about the shape of a body.

    An interstitial from a proxy in front of the worker parses as neither JSON
    nor a completion. Letting that raise out of the provider turns one
    misconfigured reverse proxy into 500s; it is the same trouble as a refused
    connection, so it is ASRUnavailable and the caller may retry.
    """
    import httpx

    def gateway_timeout(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>504 Gateway Time-out</html>")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(gateway_timeout), **kwargs
        ),
    )
    provider = asr.VLLMAudioProvider(base_url=ENGINE_URL, model=ENGINE_MODEL)

    with pytest.raises(asr.ASRUnavailable):
        asyncio.run(provider._chat(WEBM, "audio/webm", "auto"))


def test_a_failed_attempt_records_the_wait_the_person_actually_sat_through(
    engine, login_client
):
    """analytics.voice_totals averages processing_ms over EVERY attempt.

    The failure path used to write 0, which the column stores as NULL, so that
    average silently became an average of the successes — the one population
    whose latency was never the complaint. The eight seconds somebody waits
    for a full pool to give up is exactly the number the console is being
    asked about.
    """
    engine.delay = 0.03
    engine.raises = asr.ASRUnavailable("the worker is down")
    bob = login_client("bob")

    assert _post(bob).status_code == 503

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["processing_ms"] >= 10


def test_a_negative_duration_cannot_be_talked_into_the_console(engine, login_client):
    """`duration_ms` is a number a client typed into a query string.

    Stored unchecked it reaches sum() and avg() on the voice analytics page,
    where one curl can drag "minutes dictated this month" below zero — a
    figure no report can explain and no reader can distrust selectively.
    """
    bob = login_client("bob")

    response = _post(bob, duration_ms=-900_000)
    assert response.status_code == 200, response.text
    assert response.json()["duration_ms"] is None
    assert _rows()[0]["duration_ms"] is None


def test_a_recording_never_becomes_a_file_on_this_machine(engine, login_client):
    """The promise the whole feature is sold on, defended at the one place it
    was quietly untrue.

    Taking the audio as `UploadFile` hands the body to Starlette's multipart
    parser, which spools every part into a SpooledTemporaryFile whose max_size
    is a class attribute pinned at 1 MB — so any dictation past about ninety
    seconds was written to the container's disk before a line of this code
    ran. The recording is the request BODY now, so no parser stands between
    the socket and memory. This test fails the moment somebody puts one back.

    It reaches into starlette on purpose: a version that renames the symbol
    should break this and be re-verified, not skip silently.
    """
    import starlette.formparsers as formparsers

    spooled: list[int] = []
    real = formparsers.SpooledTemporaryFile

    def watched(*args, **kwargs):
        spooled.append(int(kwargs.get("max_size", 0)))
        return real(*args, **kwargs)

    formparsers.SpooledTemporaryFile = watched  # type: ignore[misc]
    try:
        bob = login_client("bob")
        # Comfortably past the 1 MB spool threshold: about three minutes of
        # Opus, which is an ordinary thing to dictate.
        response = _post(bob, body=b"\x1a\x45\xdf\xa3" + b"\x00" * (3 * 1024 * 1024))
    finally:
        formparsers.SpooledTemporaryFile = real  # type: ignore[misc]

    assert response.status_code == 200, response.text
    assert spooled == []


# ---------------------------------------------------------------------------
# The operational endpoint
# ---------------------------------------------------------------------------


def test_the_engine_health_endpoint_does_not_answer_a_member(engine, login_client):
    """/audio/health names the model and the queue depth.

    POST /transcribe goes to real trouble never to tell a member either, and a
    merely-signed-in gate here would have handed the same reconnaissance back
    through a second door. 404 rather than 403, like the rest of the admin
    surface, so the route does not confirm its own existence either.
    """
    bob = login_client("bob")

    response = bob.get("/audio/health")
    assert response.status_code == 404
    assert ENGINE_MODEL not in response.text


def test_an_administrator_gets_the_honest_operational_answer(engine, login_client):
    """The other half: somebody debugging a deployment needs one place that
    says whether the speech engine is actually answering."""
    root = login_client("root", role="super_admin")

    body = root.get("/audio/health").json()
    assert body["enabled"] is True
    assert body["ready"] is True
    assert body["model"] == ENGINE_MODEL


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------


def test_the_models_own_output_contract_is_split_into_language_and_words():
    assert asr.parse_chat_output("language English<asr_text>Hello there.") == (
        "English",
        "Hello there.",
    )


def test_a_reply_that_does_not_follow_the_contract_keeps_the_words_and_drops_the_claim():
    """A parser that fell back to "the first word is the language" would file
    a transcript beginning "Hello" under a language called Hello."""
    assert asr.parse_chat_output("Hello there.") == (None, "Hello there.")
    assert asr.parse_chat_output("") == (None, "")


def test_a_language_the_model_does_not_speak_is_none_rather_than_a_guess():
    """The console's language column is a label on a metric. Accepting
    whatever the engine emitted would let a mis-parse invent a language and
    grow the label set without bound."""
    assert asr.normalise_language("English") == "English"
    assert asr.normalise_language("english") == "English"
    assert asr.normalise_language("Cantonese.") == "Cantonese"
    assert asr.normalise_language("Klingon") is None
    assert asr.normalise_language("") is None


def test_a_transcript_never_tells_a_member_what_hardware_answered(
    engine, login_client
):
    """The reply is text and timings. A model id or an internal hostname in
    it is a free reconnaissance answer to anyone with a microphone."""
    bob = login_client("bob")

    response = _post(bob)
    assert response.status_code == 200, response.text
    body = response.text
    assert ENGINE_MODEL not in body
    assert ENGINE_URL not in body
    assert ENGINE_NAME not in body
    assert "asr-node.internal" not in body
    assert set(response.json()) == {
        "text",
        "language",
        "language_code",
        "duration_ms",
        "processing_ms",
        "upload_ms",
        "engine_ms",
    }


# ---------------------------------------------------------------------------
# Privacy and telemetry
# ---------------------------------------------------------------------------


def test_a_transcription_is_recorded_as_metadata_and_the_words_are_not(
    engine, login_client
):
    """The promise the feature is sold on. Nothing in this table can
    reconstruct a sentence anybody said, and the only way to keep that true is
    to look at every column of a real row rather than the ones we remember
    adding."""
    engine.delay = 0.03
    bob = login_client("bob")
    assert _post(bob, duration_ms=4200).status_code == 200

    rows = _rows()
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == _uid("bob")
    assert row["status"] == "ok"
    assert row["language"] == "English"
    assert row["duration_ms"] == 4200
    # The wait the person actually experienced — upload, queue and engine
    # together — is the number the console reports as latency.
    assert row["processing_ms"] >= 10
    assert row["degraded"] is False

    stored = " ".join(str(value).lower() for value in row.values())
    assert SPOKEN not in stored
    for word in ("door", "code", "seven", "four"):
        assert word not in stored


def test_a_failed_attempt_is_recorded_too_with_the_status_that_failed(
    engine, login_client
):
    """An error rate computed only from successes is not an error rate, and
    "dictation never works for me" is a claim the console has to be able to
    check against something."""
    engine.raises = asr.ASRUnavailable("the worker is down")
    bob = login_client("bob")

    assert _post(bob).status_code == 503

    rows = _rows()
    assert len(rows) == 1
    assert rows[0]["status"] == "unavailable"
    assert rows[0]["language"] is None
    assert rows[0]["user_id"] == _uid("bob")


def test_telemetry_that_cannot_write_is_still_not_an_exception(monkeypatch):
    """A missing row is a gap in a report. A raise here would be a failed
    transcription for a person who is waiting to speak, caused by the part of
    the feature that exists only to count things."""

    def broken_connection(*_a, **_k):
        raise RuntimeError("connection pool exhausted")

    monkeypatch.setattr(db, "connection", broken_connection)

    db.record_voice_transcription(
        user_id=1,
        duration_ms=4200,
        language="English",
        processing_ms=900,
        status="ok",
    )


def test_a_broken_telemetry_write_does_not_cost_the_person_their_transcript(
    engine, login_client, monkeypatch
):
    """The same guarantee one layer up, at the route: the recording still
    comes back as text even when the row cannot be written."""

    def explode(**_kwargs):
        raise RuntimeError("voice_transcriptions is unavailable")

    monkeypatch.setattr(db, "record_voice_transcription", explode)
    bob = login_client("bob")

    response = _post(bob)
    assert response.status_code == 200, response.text
    assert response.json()["text"] == SPOKEN
