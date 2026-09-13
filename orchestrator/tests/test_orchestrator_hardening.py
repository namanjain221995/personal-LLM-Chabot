"""The five orchestrator security fixes of 2026-09-12, one section each.

Every test here is written against a finding of the read-only audit in
`docs/developer-platform/AUDIT.md` and fails on the tree as it stood before
this wave:

  F015          a 422 echoed the whole request body back to the caller;
  F016 / F033   no request body size limit existed anywhere in the process;
  F034          the synthetic conversation key `u<user id>-<session id>` shared
                a namespace with client-chosen conversation ids, so one account
                could pre-claim another's bare-call key;
  (main:3552)   the two Salesforce routes failed OPEN on an id whose owner
                could not be determined;
  F013/F026/    FastAPI served `/docs`, `/redoc` and `/openapi.json`
  F053/F076     unauthenticated, enumerating all 94 routes.

The style follows tests/test_authn_idor.py: two REAL logged-in sessions where
ownership is the point, and a name that is the sentence the test pins.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid

import httpx
import pytest
from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app import db
from app import main as app_main
from app import uploads as uploads_module
from app.config import settings
from app.main import app

#: A string that appears in a request body and must never appear in a
#: response. Distinctive on purpose: an `in` test against a short word would
#: pass by accident.
MARKER = "canary-8f1c2d-do-not-echo"


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


@pytest.fixture()
def alice(login_client):
    return login_client("alice")


@pytest.fixture()
def bob(login_client):
    return login_client("bob")


@pytest.fixture()
def probe_routes():
    """Two throwaway routes that RECORD whether they ran.

    The point of "refused before it is parsed" is that the handler never sees
    the body at all, and the only way to assert that is to have a handler that
    says whether it was entered. Added to the real application (so the real
    middleware stack is exercised) and removed again, because a route left
    behind would be visible to every other test in the session.
    """
    seen: list[str] = []

    async def default_probe(payload: dict) -> dict:
        seen.append("default")
        return {"ok": True}

    async def public_probe(payload: dict) -> dict:
        seen.append("v1")
        return {"ok": True}

    app.add_api_route("/__hardening_probe", default_probe, methods=["POST"])
    app.add_api_route("/v1/__hardening_probe", public_probe, methods=["POST"])
    added = app.router.routes[-2:]
    try:
        yield seen
    finally:
        for route in added:
            if route in app.router.routes:
                app.router.routes.remove(route)


# ---------------------------------------------------------------------------
# F015 — the 422 that echoed the request body
# ---------------------------------------------------------------------------


def test_a_422_from_chat_does_not_echo_the_request_body_back_to_the_caller(alice):
    """FastAPI's default handler serialises pydantic's `input` key, which for a
    body-level failure is the ENTIRE body — the conversation history and the
    base64 of every attachment, returned to whoever asked and written into
    every proxy log on the way back."""
    response = alice.post(
        "/chat",
        json={"message": MARKER, "effort": "not-an-effort", "session_id": "default"},
    )
    assert response.status_code == 422, response.text
    assert MARKER not in response.text

    # And the echo really was there to remove. Asserting only that the marker
    # is absent would pass vacuously against a handler that returned nothing at
    # all, so the two handlers are run side by side on the SAME error: FastAPI's
    # default serialises `input`, ours does not.
    request = Request({"type": "http", "method": "POST", "path": "/chat", "headers": []})
    error = RequestValidationError(
        [{"type": "value_error", "loc": ("body",), "msg": "bad", "input": {"message": MARKER}}]
    )
    default = asyncio.run(request_validation_exception_handler(request, error))
    assert MARKER in default.body.decode()
    ours = asyncio.run(app_main._validation_error_without_input_echo(request, error))
    assert MARKER not in ours.body.decode()


def test_a_422_still_names_the_field_that_was_wrong_and_why(alice):
    """Dropping the echo must not turn a useful 422 into an opaque one: a
    caller needs `loc` and `msg` to fix their request."""
    response = alice.post(
        "/chat",
        json={"message": "hello", "effort": "not-an-effort"},
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, list) and detail, response.text
    entry = detail[0]
    assert entry["loc"][-1] == "effort"
    assert entry["msg"]
    # The two keys that carried the value, and the one that fingerprints the
    # library version, are gone.
    assert "input" not in entry
    assert "ctx" not in entry
    assert "url" not in entry


def test_the_echo_is_closed_on_every_route_not_only_on_chat(alice):
    """Registered as an application-wide handler, so a route added tomorrow
    cannot reintroduce the echo by existing."""
    response = alice.post(
        "/chat/stop",
        json={"conversation_id": ["not", "a", "string", MARKER]},
    )
    assert response.status_code == 422, response.text
    assert MARKER not in response.text


# ---------------------------------------------------------------------------
# F016 / F033 — no body size limit
# ---------------------------------------------------------------------------


def test_a_declared_oversize_body_is_refused_with_413_before_the_route_runs(
    alice, probe_routes, monkeypatch
):
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "2048")
    response = alice.post(
        "/__hardening_probe", content=b"x" * 8192, headers={"content-type": "application/json"}
    )
    assert response.status_code == 413, response.text
    assert probe_routes == []  # the handler never ran
    assert "larger than" in response.json()["detail"].lower()


def test_a_body_with_no_declared_length_is_refused_by_the_counting_wrapper(
    alice, probe_routes, monkeypatch
):
    """`Content-Length` proves nothing: an HTTP/1.1 chunked body declares none,
    and a client is free to lie. The second door counts the frames as they
    arrive and refuses as soon as the total passes the cap."""
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "2048")

    async def chunks():
        for _ in range(8):
            yield b"x" * 1024

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/__hardening_probe",
                content=chunks(),
                headers={"content-type": "application/json"},
            )

    response = asyncio.run(send())
    # An async iterator body is sent chunked: there is no length to check.
    assert "content-length" not in {k.lower() for k in response.request.headers}
    assert response.status_code == 413, response.text
    assert probe_routes == []


def test_a_body_inside_the_cap_is_untouched(alice, probe_routes, monkeypatch):
    """The cap must not become a second, quieter outage."""
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "2048")
    response = alice.post("/__hardening_probe", json={"a": "b"})
    assert response.status_code == 200, response.text
    assert probe_routes == ["default"]


MIB = 1024 * 1024


def test_the_body_cap_is_one_mebibyte_everywhere_for_a_caller_without_a_session(
    monkeypatch,
):
    """2026-09-13, wave-2 verifier: ONE 128 MiB cap on every non-upload route
    let an anonymous caller spend ~830 MiB of RSS with a 32 MiB body, because
    FastAPI decodes a body before any auth dependency runs. Every family is now
    1 MiB until a session resolves, whatever route the caller names."""
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    for method, path in (
        ("POST", "/auth/login"),
        ("POST", "/chat"),
        ("POST", "/chat/stop"),
        ("POST", "/chat/compact"),
        ("PUT", "/history/conversations/abc/messages"),
        ("POST", "/audio/transcribe"),
        ("POST", "/uploads"),
        ("POST", "/uploads/chunked/init"),
        ("PUT", "/uploads/chunked/abc/0123/part/0"),
        ("PUT", "/admin/access"),
        ("POST", "/admin/api/developers/projects"),
    ):
        assert app_main.body_cap_for(method, path).anonymous == MIB, (method, path)


def test_only_the_routes_that_carry_large_bodies_get_a_larger_cap_once_signed_in(
    monkeypatch,
):
    """The table, read off the places that decide what a real client sends:
    /chat is frontend MAX_CHAT_BODY_BYTES, the thread sync is the proxy's
    MAX_PROXY_BODY_BYTES, the microphone is ASR_MAX_UPLOAD_BYTES, a single-shot
    upload is what `_stream_to_disk` keeps (UPLOAD_MAX_MB, for EVERY purpose —
    the old `max(upload, video)` ceiling was 20x anything the route accepted),
    and a chunked part is `_PART_CAP`."""
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    monkeypatch.delenv("CHAT_MAX_REQUEST_BODY_BYTES", raising=False)
    cap = app_main.body_cap_for
    assert cap("POST", "/chat").signed_in == 128 * MIB
    assert cap("POST", "/chat/compact").signed_in == 32 * MIB
    assert cap("PUT", "/history/conversations/c1/messages").signed_in == 32 * MIB
    assert cap("POST", "/history/conversations/c1/messages").signed_in == 32 * MIB
    assert cap("POST", "/audio/transcribe").signed_in == max(MIB, settings.asr_max_upload_bytes)
    assert cap("POST", "/uploads").signed_in == settings.upload_max_mb * MIB + MIB
    assert cap("POST", "/uploads").signed_in < uploads_module._cap_total("video")
    assert cap("PUT", "/uploads/chunked/c1/u1/part/3").signed_in == uploads_module._PART_CAP
    # Everything else stays at the default even when signed in — including the
    # wrong VERB on a large-body path, and the chunked init, which is a form of
    # a few short fields.
    for method, path in (
        ("PUT", "/chat"),
        ("POST", "/uploads/chunked/init"),
        ("POST", "/uploads/chunked/c1/u1/complete"),
        ("POST", "/history/conversations"),
        ("PUT", "/history/conversations/c1"),
        ("POST", "/auth/login"),
    ):
        assert cap(method, path).signed_in == MIB, (method, path)
    # And the upload ceiling tracks the setting rather than freezing a number.
    monkeypatch.setattr(settings, "upload_max_mb", 400)
    assert cap("POST", "/uploads").signed_in == 400 * MIB + MIB


def test_a_zero_or_garbage_cap_setting_never_removes_the_cap(monkeypatch):
    for value in ("0", "-5", "lots"):
        monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", value)
        assert app_main._max_request_body_bytes() == MIB, value


async def _drive_asgi(method: str, path: str, body_chunks, *, headers=()):
    """Call the REAL application stack directly and count what it PULLED.

    `pulled` is the number of body bytes the application took off the
    (simulated) socket. That — not the status code — is the RSS-relevant
    property: a body that is never read cannot be held or decoded."""
    pulled = 0
    chunks = iter(body_chunks)
    finished = False

    async def receive():
        nonlocal pulled, finished
        if finished:
            return {"type": "http.disconnect"}
        try:
            chunk = next(chunks)
        except StopIteration:
            finished = True
            return {"type": "http.request", "body": b"", "more_body": False}
        pulled += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    status: dict = {}
    body = bytearray()

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body") or b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return status.get("code"), bytes(body), pulled


def _repeat(chunk: bytes, total: int):
    sent = 0
    while sent < total:
        yield chunk
        sent += len(chunk)


def test_an_anonymous_two_mebibyte_body_to_a_small_cap_route_is_refused_before_it_is_parsed(
    monkeypatch, anonymous_mode
):
    """The verifier's recipe with no cookie, against /auth/login: chunked (no
    length to check up front) so the counting door is what decides. The
    application pulls at most the cap plus the one frame that crossed it —
    never the 2 MiB — and answers 413, not the 422 a parsed body would earn."""
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    frame = b"[" + b"{}," * (64 * 1024 // 3)
    code, body, pulled = asyncio.run(
        _drive_asgi(
            "POST",
            "/auth/login",
            _repeat(frame, 2 * MIB),
            headers=[("content-type", "application/json")],
        )
    )
    assert code == 413, body
    assert pulled <= MIB + len(frame), pulled

    # And the declared door refuses without pulling a byte at all.
    code, body, pulled = asyncio.run(
        _drive_asgi(
            "POST",
            "/auth/login",
            _repeat(frame, 2 * MIB),
            headers=[("content-type", "application/json"), ("content-length", str(2 * MIB))],
        )
    )
    assert code == 413, body
    assert pulled == 0


def test_a_body_that_lies_about_its_length_is_refused_when_the_count_passes_the_cap(
    monkeypatch, anonymous_mode
):
    """`Content-Length: 10` followed by two mebibytes: the declared door lets
    it through (10 bytes is fine), the count does not."""
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    frame = b"x" * (128 * 1024)
    code, _, pulled = asyncio.run(
        _drive_asgi(
            "POST",
            "/auth/login",
            _repeat(frame, 2 * MIB),
            headers=[("content-type", "application/json"), ("content-length", "10")],
        )
    )
    assert code == 413
    assert pulled <= MIB + len(frame)


def test_an_anonymous_32_mebibyte_chat_body_does_not_cost_the_process_its_size_in_memory(
    monkeypatch, anonymous_mode
):
    """The measured exploit: 32 MiB of `[{},{},…]` to /chat with no cookie took
    RSS from 158 to 989 MiB before the 422. Python's own allocator is traced
    here (tracemalloc), so the number is the application's allocations, not
    the test's. Before the per-family cap this peaked in the hundreds of MiB;
    now the body is refused after about one MiB."""
    import tracemalloc

    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    frame = b"{}," * (256 * 1024 // 3)

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        code, _, pulled = asyncio.run(
            _drive_asgi(
                "POST",
                "/chat",
                _repeat(frame, 32 * MIB),
                headers=[("content-type", "application/json")],
            )
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # 401 since 2026-09-13: /chat now establishes the principal before it
    # reads the body at all, so the anonymous body is never pulled — the
    # middleware's 413 is the backstop for routes that still parse first.
    assert code in (401, 413)
    assert pulled <= MIB + len(frame)
    assert peak < 16 * MIB, f"peak traced allocation {peak / MIB:.1f} MiB"


def test_a_forged_session_cookie_does_not_buy_the_large_chat_cap(monkeypatch, anonymous_mode):
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    frame = b"x" * (256 * 1024)
    code, _, pulled = asyncio.run(
        _drive_asgi(
            "POST",
            "/chat",
            _repeat(frame, 3 * MIB),
            headers=[
                ("content-type", "application/json"),
                ("cookie", f"{settings.auth_cookie_name}=not-a-real-session"),
            ],
        )
    )
    # 401 since 2026-09-13: /chat resolves the (forged) session before it
    # reads a byte, so it never gets as far as the cap. The middleware's own
    # verdict on the forged cookie is asserted directly below.
    assert code in (401, 413)
    assert pulled <= MIB + len(frame)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": [(b"cookie", f"{settings.auth_cookie_name}=not-a-real-session".encode())],
    }
    assert asyncio.run(app_main._carries_live_session(scope)) is False


def test_a_signed_in_chat_body_above_one_mebibyte_is_still_admitted(alice, monkeypatch):
    """The cap must not become a second, quieter outage: a signed-in person's
    /chat with inline attachments is megabytes. Two MiB of a JSON body that
    fails validation reaches the parser (422), which proves the middleware let
    it through; the chat cap itself still holds above that."""
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    big = {"message": "hi", "effort": "not-an-effort", "pad": "x" * (2 * MIB)}
    response = alice.post("/chat", json=big)
    assert response.status_code == 422, response.text[:300]

    monkeypatch.setenv("CHAT_MAX_REQUEST_BODY_BYTES", str(3 * MIB))
    bigger = {"message": "hi", "pad": "x" * (4 * MIB)}
    response = alice.post("/chat", json=bigger)
    assert response.status_code == 413, response.text[:300]


def test_a_signed_in_chunked_chat_body_is_admitted_past_one_mebibyte(alice, monkeypatch):
    """The lazy door: no declared length, so the session is resolved at the
    moment the count crosses 1 MiB, and the body carries on to the parser."""
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    cookie = alice.cookies.get(settings.auth_cookie_name)
    assert cookie
    payload = json.dumps({"message": "hi", "effort": "nope", "pad": "x" * (2 * MIB)}).encode()
    frames = [payload[i : i + 256 * 1024] for i in range(0, len(payload), 256 * 1024)]
    code, body, pulled = asyncio.run(
        _drive_asgi(
            "POST",
            "/chat",
            frames,
            headers=[
                ("content-type", "application/json"),
                ("cookie", f"{settings.auth_cookie_name}={cookie}"),
            ],
        )
    )
    assert code == 422, body[:300]
    assert pulled == len(payload)


def test_the_upload_rail_authenticates_before_it_reads_a_byte_of_the_form(anonymous_mode):
    """2026-09-13, wave-2 verifier: /uploads and /uploads/chunked/init declared
    `File(...)`/`Form(...)`, and FastAPI parses a declared body BEFORE resolving
    `require_user` — an anonymous caller could make the process spool ~4 GiB of
    temp disk per request (300 MiB measured before the 401). Sent under the
    1 MiB anonymous cap, so it is the ROUTE, not the middleware, being tested:
    the answer is 401 and not one byte of the body was pulled."""
    boundary = "----hardening"
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"conversation_id\"\r\n\r\nc1\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n"
        "Content-Type: text/plain\r\n\r\n"
    ).encode()
    frames = [head] + [b"x" * (128 * 1024)] * 6 + [f"\r\n--{boundary}--\r\n".encode()]
    for path in ("/uploads", "/uploads/chunked/init"):
        code, body, pulled = asyncio.run(
            _drive_asgi(
                "POST",
                path,
                frames,
                headers=[("content-type", f"multipart/form-data; boundary={boundary}")],
            )
        )
        assert code == 401, (path, body)
        assert pulled == 0, (path, pulled)


def test_a_signed_in_upload_still_parses_its_form(alice, tmp_path, monkeypatch):
    """Moving the parse after authentication must not cost the upload itself."""
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    response = alice.post(
        "/uploads",
        data={"conversation_id": "conv-upload-ok", "purpose": "document"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["bytes"] == 5

    init = alice.post(
        "/uploads/chunked/init",
        data={"conversation_id": "conv-upload-ok", "filename": "big.txt", "size": "5", "parts": "1", "part_size": "5"},
    )
    assert init.status_code == 200, init.text

    missing = alice.post("/uploads", data={"conversation_id": "conv-upload-ok"})
    assert missing.status_code == 422, missing.text
    not_a_number = alice.post(
        "/uploads/chunked/init",
        data={"conversation_id": "conv-upload-ok", "filename": "a", "size": "five"},
    )
    assert not_a_number.status_code == 422, not_a_number.text


def test_the_public_api_body_cap_is_the_contracts_one_mebibyte(probe_routes):
    """CONTRACT-3 §8/§12. Enforced here as well as in the router, so the cap
    holds however the public surface evolves."""
    assert app_main.body_limit_for_path("/v1/responses") == 1024 * 1024


def test_an_oversize_v1_body_is_refused_in_the_public_error_envelope(probe_routes):
    """A `/v1` caller is owed CONTRACT-3 §9's envelope, not FastAPI's
    `{"detail": …}` — an SDK parses one shape."""
    client = TestClient(app)
    response = client.post(
        "/v1/__hardening_probe",
        content=b"x" * (1024 * 1024 + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413, response.text
    body = response.json()
    assert body["error"]["code"] == "request_too_large"
    assert body["error"]["type"] == "invalid_request_error"
    assert probe_routes == []


def test_a_chunked_oversize_v1_body_also_gets_the_public_envelope(probe_routes):
    """The other door. A body with no declared length is refused from inside
    the route's own read, and that refusal used to surface as FastAPI's
    `400 There was an error parsing the body` — the wrong status, and a
    misleading one: the body was not malformed, it was too big."""

    async def chunks():
        for _ in range(3):
            yield b"x" * (512 * 1024)

    async def send() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            return await client.post(
                "/v1/__hardening_probe",
                content=chunks(),
                headers={"content-type": "application/json"},
            )

    response = asyncio.run(send())
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "request_too_large"
    assert probe_routes == []


def test_a_malformed_content_length_is_treated_as_absent_not_as_zero():
    """A header we cannot parse must fall through to the counting wrapper,
    never be trusted as a small number."""
    assert app_main._declared_content_length({"headers": [(b"content-length", b"abc")]}) is None
    assert app_main._declared_content_length({"headers": [(b"content-length", b"-1")]}) is None
    assert app_main._declared_content_length({"headers": []}) is None
    assert app_main._declared_content_length({"headers": [(b"content-length", b"17")]}) == 17


# ---------------------------------------------------------------------------
# F034 — the shared conversation-id namespace
# ---------------------------------------------------------------------------


def test_chat_refuses_a_conversation_id_shaped_like_another_accounts_bare_key(
    alice, bob
):
    """Bob sends nothing but a session id, so his work is keyed `u<bob>-…`.
    Alice must not be able to claim that key as an ordinary conversation."""
    victim_key = f"u{_uid('bob')}-default"
    response = alice.post(
        "/chat", json={"message": "hello", "conversation_id": victim_key}
    )
    assert response.status_code == 422, response.text
    assert db.conversation_owner(victim_key) is None


def test_the_reserved_shape_is_refused_even_for_the_callers_own_user_id(alice):
    """Shape-only by design: a rule with an exception is a rule somebody finds
    the edge of, and a caller who wants that conversation has a real id for
    it."""
    own_key = f"u{_uid('alice')}-default"
    response = alice.post("/chat", json={"message": "hi", "conversation_id": own_key})
    assert response.status_code == 422, response.text


def test_an_ordinary_conversation_id_beginning_with_u_is_still_accepted(alice):
    """The rule is `u<digits>-`, not "starts with u" — narrow enough that no
    real id is caught by it."""
    assert app_main._reject_synthetic_conversation_id("under-review") == "under-review"
    assert app_main._reject_synthetic_conversation_id("u-7-default") == "u-7-default"
    assert app_main._reject_synthetic_conversation_id(None) is None
    with pytest.raises(ValueError):
        app_main._reject_synthetic_conversation_id("u7-default")


def test_a_session_id_outside_the_conversation_id_alphabet_is_refused(alice):
    """`session_id` is half of the synthetic key, so it takes the conversation
    id's shape rule: before this it was an unbounded free string that named the
    generation registry, the per-conversation document store and the Salesforce
    state row."""
    response = alice.post(
        "/chat", json={"message": "hi", "session_id": "../../etc/passwd"}
    )
    assert response.status_code == 422, response.text
    response = alice.post("/chat", json={"message": "hi", "session_id": "x" * 65})
    assert response.status_code == 422, response.text


def test_stop_takes_the_same_two_rules_because_it_rebuilds_the_same_key(alice, bob):
    victim_key = f"u{_uid('bob')}-default"
    assert (
        alice.post("/chat/stop", json={"conversation_id": victim_key}).status_code
        == 422
    )
    assert (
        alice.post("/chat/stop", json={"session_id": "not a session"}).status_code == 422
    )


def test_the_upload_rail_refuses_the_reserved_shape_too(alice, bob):
    """The second door. /chat is not the only route that CLAIMS an id — the
    upload rail claims on first touch as well, so closing one and not the other
    would have left the hole open with documents already in it."""
    victim_key = f"u{_uid('bob')}-default"
    response = alice.post(
        "/uploads",
        data={"conversation_id": victim_key, "purpose": "document"},
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 422, response.text
    assert db.conversation_owner(victim_key) is None


def test_the_chunked_upload_rail_refuses_the_reserved_shape_too(alice, bob):
    victim_key = f"u{_uid('bob')}-default"
    response = alice.post(
        "/uploads/chunked/init",
        data={
            "conversation_id": victim_key,
            "upload_id": uuid.uuid4().hex,
            "filename": "notes.txt",
            "size": "5",
            "parts_total": "1",
            "part_size": str(64 * 1024 * 1024),
            "purpose": "document",
        },
    )
    assert response.status_code == 422, response.text
    assert db.conversation_owner(victim_key) is None


def test_history_refuses_to_create_a_conversation_with_the_reserved_shape(alice, bob):
    """The third claim door, closed at creation (2026-09-13). Until then POST
    /history/conversations accepted `u<bob>-default`, made alice its owner, and
    every ownership check downstream answered for her honestly."""
    victim_key = f"u{_uid('bob')}-default"
    response = alice.post(
        "/history/conversations", json={"id": victim_key, "title": "mine now"}
    )
    assert response.status_code == 400, response.text
    assert db.conversation_owner(victim_key) is None
    own = alice.post(
        "/history/conversations", json={"id": f"u{_uid('alice')}-default", "title": "x"}
    )
    assert own.status_code == 400, own.text
    ordinary = alice.post("/history/conversations", json={"id": "under-review", "title": "x"})
    assert ordinary.status_code == 200, ordinary.text


def _legacy_reserved_row_owned_by(username: str, victim_key: str) -> None:
    """A row of the reserved shape that predates the creation check — exactly
    what `POST /history/conversations` produced until 2026-09-13."""
    db.create_conversation(_uid(username), victim_key, "claimed before the fix")
    assert db.conversation_owner(victim_key) == _uid(username)


def test_the_upload_read_surface_refuses_the_reserved_shape_whoever_owns_the_row(
    alice, bob
):
    """The shape check on the read routes is LOAD-BEARING, and this proves it
    with something to leak (wave-2 verifier: three of four assertions used to
    404 with or without the check, because nothing was stored under the key).

    A bare /chat with an inline PDF stores the extracted text under
    `u<bob>-<session>` (engines/document.save_document). Alice owns a legacy
    row of that id. Seeded here: bob's document text, an upload row and an
    upload session — each of which the route would serve alice if the shape
    check were removed (200 with the text, 410, 200)."""
    victim_key = f"u{_uid('bob')}-default"
    _legacy_reserved_row_owned_by("alice", victim_key)
    db.save_document(victim_key, "document", MARKER, 1)
    upload_id = uuid.uuid4().hex
    db.save_upload(upload_id, victim_key, "bob.pdf", 10, "ready", None, "document")
    session_id = uuid.uuid4().hex
    db.create_upload_session(session_id, _uid("alice"), victim_key, "bob.pdf", "document")

    responses = [
        alice.get(f"/uploads/{victim_key}"),
        alice.get(f"/uploads/{victim_key}/document?name=document"),
        alice.get(f"/uploads/{victim_key}/{upload_id}/file"),
        alice.get(f"/uploads/chunked/{victim_key}/{session_id}"),
    ]
    for response in responses:
        assert response.status_code == 404, (response.request.url, response.text)
        assert MARKER not in response.text
        assert "bob.pdf" not in response.text

    # Non-vacuous: the same seeding under an ORDINARY id alice owns is served,
    # so the 404s above are the shape check and not an empty table.
    _legacy_reserved_row_owned_by("alice", "conv-alice-docs")
    db.save_document("conv-alice-docs", "document", MARKER, 1)
    served = alice.get("/uploads/conv-alice-docs/document?name=document")
    assert served.status_code == 200, served.text
    assert MARKER in served.text


# ---------------------------------------------------------------------------
# The Salesforce routes that failed open (main.py around 3552)
# ---------------------------------------------------------------------------


@pytest.fixture()
def salesforce_intelligence_on(monkeypatch):
    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", True)
    monkeypatch.setattr(settings, "sf_live_enabled", False)


def test_salesforce_context_does_not_serve_state_left_behind_by_a_deleted_account(
    alice, bob, salesforce_intelligence_on
):
    """`sf_conversation_state` has NO foreign key to `conversations`, so state
    can outlive the id that owns it.

    Deleting a CHAT is safe — `db.delete_conversation` clears the row through
    `_SIDE_TABLES`, checked below so this test says which path is the leak and
    which is not. Deleting an ACCOUNT is the leak: `conversations.user_id`
    cascades from `users`, the conversation row disappears with it, and nothing
    reaches the Salesforce state. The id then belongs to nobody, and the old
    check let an unowned id straight through — `starter_options` built the
    "Continue: …" option out of the departed colleague's Salesforce question, in
    plain text, for whoever asked next.

    This is the case that must NOT become a 404: an unclaimed id is also exactly
    what a brand-new chat looks like. So the card still comes back; what is gone
    is everything that was conversation state.
    """
    # The path that is NOT a leak, pinned so the distinction cannot rot.
    bob.post("/history/conversations", json={"id": "conv-gone", "title": "Bob's chat"})
    db.save_sf_conversation_state(
        "conv-gone",
        {"conversation_id": "conv-gone", "last_query_summary": "deleted properly"},
    )
    assert bob.delete("/history/conversations/conv-gone").status_code == 200
    assert db.get_sf_conversation_state("conv-gone") is None

    # The path that is: the row is orphaned with no conversation above it, which
    # is what a cascaded account deletion leaves behind.
    db.save_sf_conversation_state(
        "conv-orphan",
        {"conversation_id": "conv-orphan", "last_query_summary": MARKER},
    )
    assert db.conversation_owner("conv-orphan") is None
    # Non-vacuous: the state really is loadable, so what follows is the route
    # declining to serve it rather than there being nothing to serve.
    from app.core.sf_intel import state as sf_state

    assert asyncio.run(sf_state.load_state("conv-orphan")).last_query_summary == MARKER

    response = alice.get("/chat/salesforce/conv-orphan")
    assert response.status_code == 200, response.text
    body = response.json()
    assert MARKER not in json.dumps(body)
    assert not any(option["id"] == "continue" for option in body["options"])
    assert body["pending_clarification"] is None


def test_salesforce_context_does_not_serve_state_stored_under_a_bare_call_key(
    alice, bob, salesforce_intelligence_on
):
    """The other half: a BARE call (no conversation_id) parks its Salesforce
    state under the synthetic `u<user id>-<session id>`, which is not a
    conversation row either — so `GET /chat/salesforce/u7-default` read user 7's
    last question and pending clarification for anyone who asked. That shape is
    refused outright, because nothing legitimate ever asks for it."""
    victim_key = f"u{_uid('bob')}-default"
    db.save_sf_conversation_state(
        victim_key,
        {
            "conversation_id": victim_key,
            "last_query_summary": MARKER,
            "updated_at": "2026-09-12T00:00:00+00:00",
        },
    )

    response = alice.get(f"/chat/salesforce/{victim_key}")
    assert response.status_code == 404, response.text
    assert MARKER not in response.text


def test_salesforce_context_still_draws_the_starter_card_for_a_brand_new_chat(
    alice, salesforce_intelligence_on
):
    """The fix cannot be a 404: the card is drawn on the EMPTY composer of a
    new chat, whose id is deliberately unclaimed until the first message. The
    generic catalogue is a property of the org, not of the conversation, so it
    still comes back."""
    response = alice.get(f"/chat/salesforce/{uuid.uuid4().hex}")
    assert response.status_code == 200, response.text
    assert response.json()["options"], response.text


def test_salesforce_context_still_serves_the_owner_their_own_continuation(
    bob, salesforce_intelligence_on
):
    """The closing must not cost the owner the feature it exists for."""
    bob.post("/history/conversations", json={"id": "conv-bob", "title": "Bob's chat"})
    db.save_sf_conversation_state(
        "conv-bob",
        {
            "conversation_id": "conv-bob",
            "last_query_summary": "open pipeline this quarter",
            "updated_at": "2026-09-12T00:00:00+00:00",
        },
    )
    body = bob.get("/chat/salesforce/conv-bob").json()
    assert any(option["id"] == "continue" for option in body["options"]), body


def test_cancelling_a_clarification_under_an_unowned_id_is_refused(
    alice, bob, salesforce_intelligence_on
):
    """Cancel is a WRITE, and it failed open on exactly the same test — so
    anyone could dismiss anyone else's pending question, after which the next
    Salesforce turn in that chat silently stopped resuming it."""
    victim_key = f"u{_uid('bob')}-default"
    assert db.create_sf_clarification(
        clarification_id="clr-1",
        conversation_id=victim_key,
        intent_id="int-1",
        resume_token="tok-1",
        question_fingerprint="fp-1",
        payload={"header": "Mock count", "options": []},
    )

    response = alice.post(
        "/chat/salesforce/cancel", json={"conversation_id": victim_key}
    )
    assert response.status_code == 404, response.text
    still_pending = db.pending_sf_clarification(victim_key)
    assert still_pending is not None and still_pending["state"] == "pending"


def test_cancelling_a_clarification_under_an_orphaned_ordinary_id_is_refused(
    alice, bob, salesforce_intelligence_on
):
    """The fail-CLOSED rule itself, pinned on an id the shape check does not
    touch (wave-2 verifier: the old test used `u<bob>-default`, which the shape
    check 404s anyway, so reverting `owner != viewer` to the fail-open
    `owner is not None and owner != viewer` went unnoticed). `conv-orphan` is
    what a cascaded account deletion leaves: a clarification with no
    conversation above it. Nobody owns it, so nobody may dismiss it."""
    assert db.create_sf_clarification(
        clarification_id="clr-orphan",
        conversation_id="conv-orphan",
        intent_id="int-orphan",
        resume_token="tok-orphan",
        question_fingerprint="fp-orphan",
        payload={"header": "Mock count", "options": []},
    )
    assert db.conversation_owner("conv-orphan") is None

    response = alice.post(
        "/chat/salesforce/cancel", json={"conversation_id": "conv-orphan"}
    )
    assert response.status_code == 404, response.text
    still_pending = db.pending_sf_clarification("conv-orphan")
    assert still_pending is not None and still_pending["state"] == "pending"


def test_salesforce_state_under_a_reserved_key_is_refused_even_to_its_row_owner(
    alice, bob, salesforce_intelligence_on
):
    """The third claim door. `POST /history/conversations` accepted any
    well-shaped id, `u7-default` included, until 2026-09-13 — so a row created
    there before the fix names the attacker as owner, and the ownership check
    below would see `owner == viewer` and hand over user 7's Salesforce state
    honestly. The shape is therefore refused outright on both routes, whoever
    owns the row."""
    victim_key = f"u{_uid('bob')}-default"
    # history.py refuses the shape since 2026-09-13; a row claimed before that
    # still exists in a live database, so it is seeded directly.
    _legacy_reserved_row_owned_by("alice", victim_key)

    db.save_sf_conversation_state(
        victim_key,
        {
            "conversation_id": victim_key,
            "last_query_summary": MARKER,
            "updated_at": "2026-09-12T00:00:00+00:00",
        },
    )
    read = alice.get(f"/chat/salesforce/{victim_key}")
    assert read.status_code == 404, read.text
    assert MARKER not in read.text

    cancel = alice.post(
        "/chat/salesforce/cancel", json={"conversation_id": victim_key}
    )
    assert cancel.status_code == 404, cancel.text


def test_the_owner_can_still_cancel_their_own_pending_question(
    bob, salesforce_intelligence_on
):
    bob.post("/history/conversations", json={"id": "conv-bob", "title": "Bob's chat"})
    assert db.create_sf_clarification(
        clarification_id="clr-2",
        conversation_id="conv-bob",
        intent_id="int-2",
        resume_token="tok-2",
        question_fingerprint="fp-2",
        payload={"header": "Mock count", "options": []},
    )
    response = bob.post(
        "/chat/salesforce/cancel", json={"conversation_id": "conv-bob"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["cancelled"] >= 1
    assert db.pending_sf_clarification("conv-bob") is None


# ---------------------------------------------------------------------------
# F013 / F026 / F053 / F076 — the interactive schema
# ---------------------------------------------------------------------------


def test_fastapi_does_not_serve_its_own_schema(alice):
    """All 94 routes — the admin surface, the analytics console, the share
    governance routes — were enumerable without a credential on a port bound to
    every interface. Not mounted at all rather than mounted-and-refusing: a 403
    still confirms what the process is."""
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert alice.get(path).status_code == 404, path
    assert app.docs_url is None
    assert app.redoc_url is None
    assert app.openapi_url is None


def test_the_development_flag_brings_the_schema_back():
    """Off by default, available when a development box asks for it. Checked in
    a child interpreter because the three URLs are decided when the FastAPI
    object is constructed, which is import time."""
    env = dict(os.environ, ORCHESTRATOR_DEV_DOCS="1")
    result = subprocess.run(
        [sys.executable, "-c", "from app.main import app; print(app.docs_url, app.redoc_url, app.openapi_url)"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(app_main.__file__))),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr
    assert "/docs /redoc /openapi.json" in result.stdout, result.stdout


def test_the_flag_is_off_unless_it_is_explicitly_turned_on(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_DEV_DOCS", raising=False)
    monkeypatch.delattr(settings, "dev_docs_enabled", raising=False)
    assert app_main._dev_docs_enabled() is False
    for value in ("0", "false", "no", "off", "", "maybe"):
        monkeypatch.setenv("ORCHESTRATOR_DEV_DOCS", value)
        assert app_main._dev_docs_enabled() is False, value
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("ORCHESTRATOR_DEV_DOCS", value)
        assert app_main._dev_docs_enabled() is True, value


def test_a_setting_wins_over_the_environment_variable(monkeypatch):
    """`app/config.py` is a single-owner file in this programme, so the gate
    reads an attribute if the integration lead adds one and falls back to the
    environment until then."""
    monkeypatch.setenv("ORCHESTRATOR_DEV_DOCS", "1")
    monkeypatch.setattr(settings, "dev_docs_enabled", False, raising=False)
    assert app_main._dev_docs_enabled() is False


# ---------------------------------------------------------------------------
# 2026-09-13 (wave-3 re-verifier) — the 422 as a memory amplifier
# ---------------------------------------------------------------------------
#
# Dropping `input` from the 422 closed the echo, not the amplification: one
# entry per failing element meant a cookie-less 1 MiB body of `[{},…]` to
# /chat answered 33 MiB and cost +429 MiB of RSS, UNDER the 1 MiB anonymous
# cap; and a member's 32 MiB PUT to someone else's conversation was validated
# (~26 GB) before its 404. Every body below FITS inside its cap, so what is
# tested is the route and the handler, never the middleware.

#: One empty object per three bytes: the densest per-element error there is.
_EMPTY_OBJECTS = b"{}," * ((MIB - 64) // 3) + b"{}"


def _pathological(field: bytes, *, prefix: bytes = b'"message":"hi",') -> bytes:
    body = b"{" + prefix + b'"' + field + b'":[' + _EMPTY_OBJECTS + b"]}"
    assert len(body) <= MIB, len(body)
    return body


def _traced(fn):
    """(result, peak traced bytes) for `fn()` — Python allocations in every
    thread, so the TestClient's server side is counted too."""
    import tracemalloc

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        result = fn()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return result, peak


@pytest.mark.parametrize(
    "path,body",
    [
        ("/chat", _pathological(b"images")),
        ("/chat/compact", _pathological(b"messages", prefix=b'"conversation_id":"c1",')),
    ],
    ids=["chat", "chat-compact"],
)
def test_an_anonymous_one_mebibyte_body_of_empty_objects_is_refused_unread_at_a_tiny_cost(
    path, body, monkeypatch, anonymous_mode
):
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    (code, response, pulled), peak = _traced(
        lambda: asyncio.run(
            _drive_asgi(
                "POST",
                path,
                [body],
                headers=[("content-type", "application/json"), ("content-length", str(len(body)))],
            )
        )
    )
    assert code == 401, (code, response[:200])
    assert pulled == 0, "the body of an anonymous caller was read"
    assert len(response) < 1024, len(response)
    assert peak < 2 * MIB, f"peak traced allocation {peak / MIB:.2f} MiB"


def test_a_signed_in_one_mebibyte_body_of_bad_elements_gets_a_small_422_at_a_small_cost(alice):
    """A member is allowed to send megabytes, so their bad body IS parsed —
    and must still cost kilobytes of answer and a bounded allocation, never
    one error per element. MARKER rides every element: never echoed."""
    element = b'{"x":"' + MARKER.encode() + b'"},'
    many = element * ((MIB - 128) // len(element))
    bodies = {
        "/chat": b'{"message":"hi","images":[' + many + b'{}]}',
        "/chat/compact": b'{"conversation_id":"c1","messages":[' + many + b'{}]}',
    }
    created = alice.post("/history/conversations", json={"id": "amp-own", "title": "t"})
    assert created.status_code == 200, created.text
    bodies["/history/conversations/amp-own/messages"] = b'{"messages":[' + many + b'{}]}'

    for path, body in bodies.items():
        method = alice.put if path.startswith("/history") else alice.post
        response, peak = _traced(
            lambda: method(path, content=body, headers={"content-type": "application/json"})
        )
        assert response.status_code == 422, (path, response.text[:300])
        assert len(response.content) < 16 * 1024, (path, len(response.content))
        assert MARKER not in response.text
        detail = response.json()["detail"]
        assert 1 <= len(detail) <= 20, (path, len(detail))
        assert detail[0]["loc"][0] == "body"
        # The body is ~1 MiB and is copied a few times on its way through the
        # client, the transport and the server; one error per element was
        # hundreds of MiB.
        assert peak < 24 * MIB, f"{path}: peak traced allocation {peak / MIB:.1f} MiB"


def test_a_route_fastapi_still_validates_returns_at_most_twenty_errors_and_the_total(
    alice, probe_routes
):
    """The handler bound is what holds for every route NOT moved onto the
    bounded reader. A throwaway `List[int]` route, 1 MiB of strings."""
    from typing import List

    async def list_probe(payload) -> dict:
        return {"n": len(payload)}

    # Set as objects: this module's `from __future__ import annotations` would
    # leave a string FastAPI cannot resolve (it would become a query param).
    list_probe.__annotations__ = {"payload": List[int], "return": dict}

    app.add_api_route("/__hardening_list_probe", list_probe, methods=["POST"])
    try:
        body = b"[" + b'"x",' * ((MIB - 16) // 4) + b'"x"]'
        response = alice.post(
            "/__hardening_list_probe", content=body, headers={"content-type": "application/json"}
        )
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", "") != "/__hardening_list_probe"
        ]
    assert response.status_code == 422
    assert len(response.content) < 16 * 1024, len(response.content)
    payload = response.json()
    assert len(payload["detail"]) == 20
    assert payload["errors_total"] > 200_000


def test_the_validation_handler_bounds_count_message_and_location_and_never_carries_input():
    request = Request({"type": "http", "method": "POST", "path": "/x", "headers": []})
    errors = [
        {
            "type": "value_error",
            "loc": ("body",) + tuple(f"part-{i}-" + "p" * 500 for i in range(50)),
            "msg": "m" * 50_000,
            "input": MARKER,
            "ctx": {"error": MARKER},
            "url": "https://errors.pydantic.dev/",
        }
    ] * 10_000
    response = asyncio.run(
        app_main._validation_error_without_input_echo(request, RequestValidationError(errors))
    )
    text = response.body.decode()
    payload = json.loads(text)
    assert len(text) < 32 * 1024, len(text)
    assert MARKER not in text and "pydantic" not in text
    assert payload["errors_total"] == 10_000
    assert len(payload["detail"]) == 20
    entry = payload["detail"][0]
    assert len(entry["msg"]) <= 200
    assert len(entry["loc"]) <= 8 and all(len(part) <= 64 for part in entry["loc"])
    assert set(entry) == {"type", "loc", "msg"}


@pytest.mark.parametrize("method", ["PUT", "POST"])
def test_a_member_cannot_make_the_server_parse_a_body_for_a_conversation_she_does_not_own(
    method, alice, bob, monkeypatch
):
    """Ownership before the body: 404 with not one byte pulled. Before the fix
    FastAPI read and validated the whole body, then `require_user` ran, then
    the 404 — ~26 GB of RSS at the 32 MiB cap."""
    monkeypatch.delenv("MAX_REQUEST_BODY_BYTES", raising=False)
    made = bob.post("/history/conversations", json={"id": "bobs-thread", "title": "t"})
    assert made.status_code == 200, made.text
    cookie = alice.cookies.get(settings.auth_cookie_name)
    body = _pathological(b"messages", prefix=b"")
    (code, response, pulled), peak = _traced(
        lambda: asyncio.run(
            _drive_asgi(
                method,
                "/history/conversations/bobs-thread/messages",
                [body],
                headers=[
                    ("content-type", "application/json"),
                    ("content-length", str(len(body))),
                    ("cookie", f"{settings.auth_cookie_name}={cookie}"),
                ],
            )
        )
    )
    assert code == 404, (code, response[:200])
    assert pulled == 0
    assert peak < 4 * MIB, f"peak traced allocation {peak / MIB:.2f} MiB"
    # And the owner's own sync still works, through the same reader.
    ok = bob.put(
        "/history/conversations/bobs-thread/messages",
        json={"messages": [{"role": "user", "content": "hello"}]},
    )
    assert ok.status_code == 200, ok.text
    appended = bob.post(
        "/history/conversations/bobs-thread/messages", json={"role": "assistant", "content": "hi"}
    )
    assert appended.status_code == 200, appended.text
    assert [m["content"] for m in bob.get("/history/conversations/bobs-thread").json()["messages"]] == [
        "hello",
        "hi",
    ]


def test_more_concurrent_unowned_syncs_than_the_pool_has_connections_all_finish(
    alice, bob, login_client
):
    """Real threads, real PostgreSQL. The ownership check takes one pooled
    connection and gives it back before the body is read, and the write takes
    another afterwards — never two at once. 32 concurrent requests (the pool
    is 16) interleaved with the owner's own valid syncs must all answer inside
    a hard deadline; a route that held a connection across the second checkout
    would stall every one of them until PoolTimeout."""
    from concurrent.futures import ThreadPoolExecutor, wait

    made = bob.post("/history/conversations", json={"id": "pool-thread", "title": "t"})
    assert made.status_code == 200, made.text
    alice_cookie = alice.cookies.get(settings.auth_cookie_name)
    bob_cookie = bob.cookies.get(settings.auth_cookie_name)
    body = _pathological(b"messages", prefix=b"")

    def intruder(_):
        client = TestClient(app, cookies={settings.auth_cookie_name: alice_cookie})
        return client.put(
            "/history/conversations/pool-thread/messages",
            content=body,
            headers={"content-type": "application/json"},
        ).status_code

    def owner(i):
        client = TestClient(app, cookies={settings.auth_cookie_name: bob_cookie})
        return client.put(
            "/history/conversations/pool-thread/messages",
            json={"messages": [{"role": "user", "content": f"m{j}"} for j in range(i + 1)]},
        ).status_code

    with ThreadPoolExecutor(max_workers=40) as pool:
        futures = [pool.submit(intruder, i) for i in range(32)] + [
            pool.submit(owner, i) for i in range(8)
        ]
        done, not_done = wait(futures, timeout=90)
        assert not not_done, f"{len(not_done)} request(s) still waiting after 90 s"
    codes = [f.result() for f in futures]
    assert codes[:32] == [404] * 32, codes[:32]
    # A shorter re-push after a longer one is a 409 by design; nothing else.
    assert set(codes[32:]) <= {200, 409}, codes[32:]


def test_every_list_in_the_large_body_models_stops_at_its_first_bad_element():
    """`fail_fast` is what keeps a SIGNED-IN bad body from costing one error
    per element (32 MiB of `[{},…]`: 11,184,811 errors and +12 GB without
    it). A list field added to these models without it fails here."""
    import typing

    from pydantic.types import FailFast

    from app import history as history_module

    def has_list(annotation) -> bool:
        if typing.get_origin(annotation) in (list, typing.List):
            return True
        return any(has_list(arg) for arg in typing.get_args(annotation))

    checked = []
    for model in (
        app_main.ChatRequest,
        app_main.CompactRequest,
        history_module.MessagesReplaceIn,
        history_module.MessageIn,
    ):
        for name, field in model.model_fields.items():
            if has_list(field.annotation):
                checked.append(f"{model.__name__}.{name}")
                assert any(
                    isinstance(meta, FailFast) and meta.fail_fast for meta in field.metadata
                ), f"{model.__name__}.{name} is a list without fail_fast"
    assert {"ChatRequest.messages", "ChatRequest.images", "MessagesReplaceIn.messages"} <= set(checked)


def test_the_bounded_reader_keeps_fastapis_strict_json_content_type_rule(alice):
    """Moving /chat off FastAPI's parser must not start accepting a text/plain
    body — that is a CORS-simple request a foreign page can send."""
    response = alice.post(
        "/chat", content=b'{"message":"hi"}', headers={"content-type": "text/plain"}
    )
    assert response.status_code == 422, response.text
    response = alice.post("/chat", content=b'{"message":', headers={"content-type": "application/json"})
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# 2026-09-13 — the dependency floors the upload rail needs
# ---------------------------------------------------------------------------


def _floor(requirement: str) -> tuple:
    import pathlib
    import re

    text = (pathlib.Path(__file__).resolve().parents[1] / "requirements.txt").read_text()
    match = re.search(rf"^{re.escape(requirement)}>=([0-9.]+)\s*$", text, re.MULTILINE)
    assert match, f"requirements.txt has no floor for {requirement}"
    return tuple(int(part) for part in match.group(1).split("."))


def test_requirements_pin_the_starlette_and_fastapi_that_request_form_max_part_size_needs():
    """`Request.form(max_part_size=…)` is Starlette 0.44.0 (0.40 only added it
    to the parser class), and FastAPI 0.115.7 is the first release that allows
    that Starlette. `fastapi>=0.115` could resolve starlette 0.39."""
    import inspect

    from starlette.requests import Request as StarletteRequest

    assert "max_part_size" in inspect.signature(StarletteRequest.form).parameters
    assert _floor("starlette") >= (0, 44, 0)
    assert _floor("fastapi") >= (0, 115, 7)
    assert _floor("pydantic") >= (2, 8)


def test_a_starlette_too_old_for_the_form_call_is_a_loud_error_not_a_400_for_every_upload(
    monkeypatch,
):
    async def old_form(self, **kwargs):
        raise TypeError("form() got an unexpected keyword argument 'max_part_size'")

    monkeypatch.setattr(Request, "form", old_form)
    request = Request({"type": "http", "method": "POST", "path": "/uploads", "headers": []})
    with pytest.raises(TypeError):
        asyncio.run(uploads_module._read_form(request, max_files=1))
