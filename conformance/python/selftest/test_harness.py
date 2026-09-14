"""Offline checks of the suite's own machinery — no server, no key.

    python -m pytest selftest -p no:techsara_conformance.plugin

The conformance tests are only as honest as feature detection, the SSE
reader and the pacing parser; these pin each of them against fixed inputs
(2026-09-13)."""
from __future__ import annotations

import struct
import types
import wave
import io
import zlib

import pytest

from techsara_conformance import config, features, media, pacing, sse

OLD_DOC = {
    "paths": {
        "/v1/models": {"get": {"responses": {"200": {}, "429": {}}}},
        "/v1/responses": {"post": {"responses": {"409": {"description": "idempotency_conflict"}, "503": {"description": "model_recovering; model_unavailable"}}}},
    },
    "components": {"schemas": {"Model": {"properties": {"id": {}}}, "Response": {"properties": {"id": {}}}, "ChatCompletionRequest": {"properties": {"max_tokens": {}}}}},
}
NEW_DOC = {
    "paths": {
        "/v1/models": {"get": {"responses": {"200": {}}}},
        "/v1/responses": {"post": {"responses": {
            "409": {"description": "idempotency_conflict: ... still running (carries Retry-After)"},
            "503": {"description": "model_unavailable — down, or at capacity"},
        }}},
        "/v1/embeddings": {"post": {}},
        "/v1/rerank": {"post": {}},
        "/v1/audio/transcriptions": {"post": {}},
        "/v1/uploads/{upload_id}": {"get": {}},
    },
    "components": {"schemas": {
        "Model": {"properties": {"kind": {}}},
        "Response": {"properties": {"incomplete_details": {}}},
        "ChatCompletionRequest": {"properties": {"max_completion_tokens": {}}},
        "InputImage": {"properties": {"type": {"enum": ["input_image"]}}},
    }},
}


def test_an_eight_route_schema_marks_every_new_feature_planned():
    resolved = features.resolve(OLD_DOC, None, {})
    assert {r.state for r in resolved.values()} == {features.PLANNED}


def test_a_schema_that_declares_a_feature_marks_exactly_that_feature_built():
    resolved = features.resolve(NEW_DOC, None, {})
    built = {name for name, r in resolved.items() if r.state == features.BUILT}
    assert built == {
        "model_catalogue", "output_ceiling", "max_completion_tokens", "idempotency_in_flight_409", "limits_off",
        "capacity_gates", "image_input", "embeddings", "rerank", "audio_transcriptions", "uploads_resume",
    }


def test_an_unreadable_schema_marks_everything_planned_and_says_why():
    resolved = features.resolve(None, "ConnectError", {})
    assert all(r.state == features.PLANNED and "ConnectError" in r.evidence for r in resolved.values())


def test_overrides_win_over_detection_and_unknown_names_are_refused():
    overrides = features.parse_overrides(["embeddings=built", "rerank=planned,files=auto"])
    resolved = features.resolve(NEW_DOC, None, overrides)
    assert resolved["embeddings"].state == features.BUILT and "forced" in resolved["embeddings"].evidence
    assert resolved["rerank"].state == features.PLANNED
    with pytest.raises(ValueError):
        features.parse_overrides(["embedding=built"])
    with pytest.raises(ValueError):
        features.parse_overrides(["embeddings=maybe"])


def test_the_base_url_is_normalised_to_the_v1_form():
    for value in ("https://api.example.test", "https://api.example.test/", "https://api.example.test/v1", "https://api.example.test/v1/"):
        assert config.normalise_base_url(value) == "https://api.example.test/v1"


def test_the_sse_reader_keeps_event_names_multiline_data_and_heartbeat_comments():
    lines = iter(["event: response.created", 'data: {"type": "response.created",', 'data: "sequence_number": 1}', "", ": ping", "", "event: done", "data: [DONE]", ""])
    transcript = sse.read(lines)
    assert [e.name for e in transcript.events] == ["response.created", "done"]
    assert transcript.events[0].json() == {"type": "response.created", "sequence_number": 1}
    assert len(transcript.comments) == 1


def test_pacing_waits_only_when_the_advertised_budget_is_nearly_spent(monkeypatch):
    slept = []
    monkeypatch.setattr(pacing.time, "sleep", lambda s: slept.append(s))
    pacing.reset_for_tests()
    pacing.on_response(types.SimpleNamespace(headers={"ratelimit": '"requests";r=40;t=30'}))
    pacing.on_request(None)
    assert slept == []
    pacing.on_response(types.SimpleNamespace(headers={"ratelimit": '"requests";r=1;t=7'}))
    pacing.on_request(None)
    assert len(slept) == 1 and 7.0 <= slept[0] <= 8.5
    pacing.on_response(types.SimpleNamespace(headers={}))
    pacing.on_request(None)
    assert len(slept) == 1, "no header (limits off) never waits"


def test_the_generated_png_is_a_valid_png_of_the_requested_size_and_colour():
    png = media.solid_png(width=4, height=3, rgb=(1, 2, 3))
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (4, 3)
    idat_len = struct.unpack(">I", png[33:37])[0]
    raw = zlib.decompress(png[41 : 41 + idat_len])
    assert raw == (b"\x00" + bytes((1, 2, 3)) * 4) * 3


def test_the_generated_wav_is_two_seconds_of_16k_mono_pcm():
    with wave.open(io.BytesIO(media.tone_wav(seconds=2.0)), "rb") as handle:
        assert (handle.getnchannels(), handle.getsampwidth(), handle.getframerate(), handle.getnframes()) == (1, 2, 16000, 32000)


def test_the_limit_patient_transport_waits_out_a_limit_429_but_never_an_idempotent_one(monkeypatch):
    import httpx

    slept = []
    monkeypatch.setattr(pacing.time, "sleep", lambda s: slept.append(s))
    answers = []

    def handler(request):
        answers.append(request.headers.get("idempotency-key"))
        if len(answers) in (1, 3):
            return httpx.Response(429, headers={"retry-after": "3"}, json={"error": {"code": "rate_limit_error"}})
        return httpx.Response(200, json={"ok": True})

    pacing.reset_for_tests()
    transport = pacing.wrap(httpx.MockTransport(handler), httpx.BaseTransport)
    with httpx.Client(transport=transport) as client:
        assert client.post("https://api.example.test/v1/x", json={}).status_code == 200
        assert slept == [3.0]
        response = client.post("https://api.example.test/v1/x", json={}, headers={"Idempotency-Key": "k"})
        assert response.status_code == 429, "a request with an Idempotency-Key sees its 429"


# ------------------------------------------------ 2026-09-13 review findings --


def test_a_limits_off_target_gets_no_patience_and_every_limit_it_shows_is_counted(monkeypatch):
    import httpx

    slept = []
    monkeypatch.setattr(pacing.time, "sleep", lambda s: slept.append(s))
    pacing.reset_for_tests()
    pacing.expect_limits_off()

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, headers={"ratelimit": '"requests";r=0;t=30'}, json={"data": []})
        return httpx.Response(429, headers={"retry-after": "1"}, json={"error": {"code": "rate_limit_error"}})

    transport = pacing.wrap(httpx.MockTransport(handler), httpx.BaseTransport)
    hooks = {"request": [pacing.on_request], "response": [pacing.on_response]}
    try:
        with httpx.Client(transport=transport, event_hooks=hooks) as client:
            assert client.get("https://api.example.test/v1/models").status_code == 200
            assert client.get("https://api.example.test/v1/models").status_code == 200, "r=0 would pace; limits off never does"
            assert client.post("https://api.example.test/v1/responses", json={}).status_code == 429, "the 429 reaches the test"
        assert slept == [], "no pacing sleep and no limit wait when limits are expected off"
        violations = pacing.limits_off_violations()
        assert any("1 HTTP 429" in v for v in violations), violations
        assert any("2 response(s) carried RateLimit" in v for v in violations), violations
    finally:
        pacing.reset_for_tests()


def test_a_limits_enforcing_target_still_counts_its_429s_while_they_are_waited_out(monkeypatch):
    import httpx

    monkeypatch.setattr(pacing.time, "sleep", lambda s: None)
    pacing.reset_for_tests()
    answers = []

    def handler(request):
        answers.append(1)
        if len(answers) == 1:
            return httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"code": "quota_exceeded"}})
        return httpx.Response(200, json={})

    with httpx.Client(transport=pacing.wrap(httpx.MockTransport(handler), httpx.BaseTransport)) as client:
        assert client.get("https://api.example.test/v1/models").status_code == 200
    assert pacing.limit_waits() == (1, 2.0)
    assert pacing.limits_off_violations(), "counted even when waited out, so a limits_off=built run cannot hide it"
    pacing.reset_for_tests()


def _fake_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pacing.time, "monotonic", lambda: now[0])

    def sleep(seconds):
        now[0] += seconds

    monkeypatch.setattr(pacing.time, "sleep", sleep)
    return now


def test_the_wire_clock_never_counts_a_pacing_sleep_as_stream_silence(monkeypatch):
    """The review's paced mock: RateLimit r=1;t=21 before an instant stream.
    The first long-stream test reported 22.2 s of silence; the wire clock
    starts at the response headers, after the sleep."""
    import httpx

    now = _fake_clock(monkeypatch)
    pacing.reset_for_tests()
    pacing.on_response(types.SimpleNamespace(headers={"ratelimit": '"requests";r=1;t=21'}))
    wire = sse.WireClock(clock=lambda: now[0])
    body = b"event: response.created\ndata: {}\n\nevent: response.completed\ndata: {}\n\n"

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=iter([body[:30], body[30:]]))

    transport = pacing.wrap(httpx.MockTransport(handler), httpx.BaseTransport, stream_base=httpx.SyncByteStream, wire=wire)
    hooks = {"request": [pacing.on_request], "response": [pacing.on_response]}
    try:
        with httpx.Client(transport=transport, event_hooks=hooks) as client:
            with client.stream("POST", "https://api.example.test/v1/responses", json={}) as response:
                chunks = list(response.iter_raw())
        assert b"".join(chunks) == body
        assert pacing.waited_seconds() >= 21, "the pacing sleep happened"
        assert wire.max_silence_s() == 0.0 and wire.pre_header_wait_s == 0.0, (wire.silences_s(), wire.pre_header_wait_s)
        assert len(wire.arrivals) == 2 and wire.ended_at is not None
    finally:
        pacing.reset_for_tests()


def test_the_wire_clock_measures_silence_from_the_headers_and_any_byte_resets_it():
    now = [0.0]
    wire = sse.WireClock(clock=lambda: now[0])
    wire.start(sent_at=-25.0)  # a 25 s capacity-gate wait before the status line
    now[0] = 10.0
    wire.chunk(b": pi")  # a heartbeat split across two chunks
    now[0] = 20.0
    wire.chunk(b"ng\n\n")
    now[0] = 41.0
    wire.chunk(b"event: response.completed\ndata: {}\n\n")
    now[0] = 41.5
    wire.end()
    assert wire.pre_header_wait_s == 25.0, "reported apart, never counted as silence"
    assert wire.silences_s() == [10.0, 10.0, 21.0, 0.5]
    assert wire.max_silence_s() == 21.0, "21 s with no byte is over 15 s + 5 s slack"
    assert wire.comment_lines == 1


def test_only_the_scopes_the_contract_names_are_required_and_unknown_scopes_skip_nothing():
    old_key = frozenset({"models.read", "responses.read", "responses.write"})
    assert features.missing_scopes("rerank", old_key) == ["rerank.write"]
    assert features.missing_scopes("embeddings", old_key) == ["embeddings.write"]
    assert features.missing_scopes("audio_transcriptions", old_key) == ["audio.write"]
    assert features.missing_scopes("image_input", old_key) == [], "input_image rides on responses.write"
    assert features.missing_scopes("files", old_key) == [], "Files scopes are not in CONTRACT-3 §7 yet"
    assert features.missing_scopes("rerank", None) == [], "a key whose scopes are unknown is not second-guessed"


def test_the_main_keys_scopes_come_from_the_source_that_supplied_the_key(tmp_path, monkeypatch):
    import json

    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"base_url": "https://api.example.test", "api_key": "file-key", "scopes": ["models.read"]}))

    class Options:
        def getoption(self, name):
            return {"--keys-file": str(keys)}.get(name)

    for name in ("TECHSARA_API_KEY", "TECHSARA_API_KEY_SCOPES", "TECHSARA_BASE_URL", "TECHSARA_KEYS_FILE"):
        monkeypatch.delenv(name, raising=False)
    target = config.load(Options())
    assert target.scopes == frozenset({"models.read"}) and "keys file" in target.scopes_source
    monkeypatch.setenv("TECHSARA_API_KEY", "env-key")
    target = config.load(Options())
    assert target.api_key == "env-key" and target.scopes is None, "the file's scopes belong to the file's key"
    monkeypatch.setenv("TECHSARA_API_KEY_SCOPES", "models.read, rerank.write")
    assert config.load(Options()).scopes == frozenset({"models.read", "rerank.write"})
