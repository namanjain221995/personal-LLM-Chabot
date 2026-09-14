"""How `/v1` is WIRED into the application — not what it answers.

The public developer API's own behaviour belongs to `app/publicapi/`; this file
is about the four decisions `app/main.py` makes on its behalf, each of which is
a way the surface can be broken from outside the package that owns it:

  * `/v1` is exempt from `_reject_cross_site_writes`, and nothing else is
    (CONTRACT-3 §3.1);
  * the browser CORS allowlist was NOT widened to reach it, and the middleware
    that enforces it does not run for `/v1` at all, which is what lets the
    router answer a preflight the contract's way (CONTRACT-3 §3.2);
  * a `/v1` that will not import must not take the chat application down with
    it, and must say so loudly;
  * the platform's two background pieces — the API-key pepper store and the
    webhook delivery loop — are bound in the lifespan, and a failure in either
    is survivable.

`app/publicapi/router.py` and `app/apiplatform/webhooks/worker.py` belong to
other engineers in this wave; both had landed by the time this ran, and the
tests below exercise the real ones. They are nevertheless written not to
REQUIRE them: a test that needs a route under `/v1` adds a throwaway one and
removes it again, the lifespan tests stand a module in so the wiring is pinned
without a live delivery queue, and the mount test asserts the invariant that
holds whether the router is there or not. That is deliberate — this file has to
keep saying something true on the day one of those modules is mid-edit, which
is the day it matters most.
"""
from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

from app import db
from app import main as app_main
from app.config import settings
from app.main import app

#: A browser origin that is deliberately not one of ours — a developer's own
#: app, from the platform's point of view, and a hostile page from the chat
#: application's.
FOREIGN_ORIGIN = "https://someone-elses-app.example"


@pytest.fixture()
def alice(login_client):
    return login_client("alice")


@pytest.fixture()
def v1_probe():
    """A throwaway `POST /v1/__mount_probe`, removed again afterwards."""

    async def probe(payload: dict) -> dict:
        return {"ok": True}

    app.add_api_route("/v1/__mount_probe", probe, methods=["POST"])
    app.add_api_route("/v1beta/__mount_probe", probe, methods=["POST"])
    added = app.router.routes[-2:]
    try:
        yield
    finally:
        for route in added:
            if route in app.router.routes:
                app.router.routes.remove(route)


# ---------------------------------------------------------------------------
# CONTRACT-3 §3.1 — the CSRF exemption
# ---------------------------------------------------------------------------


def test_a_cross_origin_write_to_v1_is_not_refused_as_cross_site(v1_probe):
    """A developer's browser app sends its OWN `Origin` on every
    `POST /v1/responses`. `/v1` is key-authenticated and cookie-blind, so there
    is no ambient credential to ride and CSRF does not apply — leaving the
    middleware in would have refused the intended use before the key was even
    read."""
    client = TestClient(app)
    response = client.post(
        "/v1/__mount_probe", json={}, headers={"origin": FOREIGN_ORIGIN}
    )
    assert response.status_code == 200, response.text


def test_the_same_cross_origin_write_to_the_chat_app_is_still_refused(alice):
    """The exemption is one path prefix wide. Everything the session cookie
    reaches keeps the second layer it has had since V2."""
    response = alice.post(
        "/chat",
        json={"message": "hello"},
        headers={"origin": FOREIGN_ORIGIN},
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "cross-site request refused"


def test_a_path_that_merely_starts_with_the_letters_v1_is_not_exempt(v1_probe):
    """`startswith("/v1")` would have exempted `/v1beta…` as well — a quiet
    widening that nobody would notice until it mattered."""
    assert app_main._is_public_api_path("/v1")
    assert app_main._is_public_api_path("/v1/responses")
    assert not app_main._is_public_api_path("/v1beta/responses")
    assert not app_main._is_public_api_path("/chat")
    assert not app_main._is_public_api_path("/history/v1/x")

    client = TestClient(app)
    response = client.post(
        "/v1beta/__mount_probe", json={}, headers={"origin": FOREIGN_ORIGIN}
    )
    assert response.status_code == 403, response.text


def test_a_cross_origin_write_without_an_origin_header_is_unaffected(alice):
    """The Next.js proxy is server-to-server and sends no Origin; proxied
    traffic passed before this wave and passes after it."""
    response = alice.post("/chat/stop", json={"session_id": "default"})
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# CONTRACT-3 §3.2 — the CORS allowlist
# ---------------------------------------------------------------------------


def test_the_browser_cors_allowlist_was_not_widened_for_the_developer_platform():
    """The browser allowlist carries `allow_credentials=True`, so every origin
    on it may drive the session cookie. Widening it to let a developer's app
    reach `/v1` would have handed those same origins the cookie on `/chat`,
    which is why the platform is exempted from the middleware instead."""
    cors = [
        m
        for m in app.user_middleware
        if getattr(m, "cls", None) is app_main.BrowserCorsExceptPublicApi
    ]
    assert len(cors) == 1
    options = cors[0].kwargs
    assert options["allow_origins"] == settings.cors_allow_origins
    assert options["allow_credentials"] is True
    assert FOREIGN_ORIGIN not in options["allow_origins"]


def test_a_preflight_for_v1_is_answered_by_the_router_not_by_browser_cors():
    """CONTRACT-3 §3.2, and the bug that made this class necessary.

    Starlette's CORSMiddleware sits outside the router and answers every
    preflight itself. With `/v1` inside it, `OPTIONS /v1/responses` from a
    developer's own origin was answered `400 Disallowed CORS origin` — carrying
    `access-control-allow-credentials: true`, the one header the contract says
    `/v1` must never send. Both are measured here: a developer's browser app
    could not make a single call, and the refusal leaked the header.
    """
    client = TestClient(app)
    response = client.options(
        "/v1/responses",
        headers={
            "origin": FOREIGN_ORIGIN,
            "access-control-request-method": "POST",
            "access-control-request-headers": "authorization,content-type",
        },
    )
    assert response.status_code == 204, response.text
    assert response.headers["access-control-allow-origin"] == FOREIGN_ORIGIN
    assert "access-control-allow-credentials" not in response.headers


def test_a_preflight_for_the_chat_app_is_still_decided_by_the_allowlist():
    """The exemption is one prefix wide: everything the session cookie reaches
    keeps the strict, credentialed CORS it has had since V2."""
    client = TestClient(app)
    foreign = client.options(
        "/chat",
        headers={"origin": FOREIGN_ORIGIN, "access-control-request-method": "POST"},
    )
    assert foreign.status_code == 400, foreign.text

    allowed = settings.cors_allow_origins[0]
    ours = client.options(
        "/chat",
        headers={"origin": allowed, "access-control-request-method": "POST"},
    )
    assert ours.status_code == 200
    assert ours.headers["access-control-allow-origin"] == allowed
    assert ours.headers.get("access-control-allow-credentials") == "true"


def test_a_foreign_origin_is_not_given_cors_permission_on_the_chat_app(alice):
    response = alice.get("/health", headers={"origin": FOREIGN_ORIGIN})
    assert response.headers.get("access-control-allow-origin") is None


def test_the_body_limit_sits_inside_cors_so_a_413_is_readable_by_a_browser(
    monkeypatch, v1_probe
):
    """Middleware order, outermost first: cross-site → CORS → body limit. If
    the limit were outermost its 413 would carry no CORS headers and a browser
    would see an opaque network error instead of a status it can act on."""
    names = [getattr(m, "cls", None).__name__ for m in app.user_middleware]
    assert names[1:] == ["BrowserCorsExceptPublicApi", "RequestBodySizeLimitMiddleware"]
    # _reject_cross_site_writes: a BaseHTTPMiddleware until main.py makes it
    # plain ASGI (see the abort test below, which says why it must).
    assert names[0] in ("BaseHTTPMiddleware", "RejectCrossSiteWrites")

    allowed = settings.cors_allow_origins[0]
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "512")
    client = TestClient(app)
    response = client.post(
        "/__does_not_exist",
        content=b"x" * 4096,
        headers={"origin": allowed, "content-type": "application/json"},
    )
    assert response.status_code == 413, response.text
    assert response.headers.get("access-control-allow-origin") == allowed


# ---------------------------------------------------------------------------
# The mount itself
# ---------------------------------------------------------------------------


_BROKEN_IMPORT_CHILD = r"""
import importlib.abc, sys

BROKEN = set(sys.argv[1:])


class Refuse(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in BROKEN:
            raise ImportError(f"simulated half-written module: {fullname}")
        return None


sys.meta_path.insert(0, Refuse())
from app import main as m

paths = set()


def walk(routes):
    for route in routes:
        p = getattr(route, "path", None)
        if p:
            paths.add(p)
        for attribute in ("original_router", "router", "app"):
            sub = getattr(route, attribute, None)
            if sub is not None and sub is not route:
                walk(getattr(sub, "routes", ()))


walk(m.app.routes)
print("PUBLIC", m.PUBLIC_API_MOUNTED, m.PUBLIC_API_MOUNT_ERROR)
print("CONSOLE", m.CONSOLE_API_MOUNTED, m.CONSOLE_API_MOUNT_ERROR)
print("HEALTH", "/health" in paths, "/chat" in paths)
print("V1", any(p.startswith("/v1") for p in paths))
print("DEVELOPERS", any(p.startswith("/admin/api/developers") for p in paths))
from fastapi.testclient import TestClient

r = TestClient(m.app).get("/v1/responses/x", headers={"origin": "https://dev.example"})
body = r.json()
print(
    "UNROUTED",
    r.status_code,
    sorted(body.get("error", {}).keys()) == ["code", "message", "param", "request_id", "type"],
    body.get("error", {}).get("request_id") == r.headers.get("x-request-id"),
    r.headers.get("access-control-allow-origin"),
    "access-control-allow-credentials" in r.headers,
)
"""


def _import_main_with_broken(*modules: str) -> str:
    import os
    import subprocess

    result = subprocess.run(
        [sys.executable, "-c", _BROKEN_IMPORT_CHILD, *modules],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(app_main.__file__))),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    return result.stdout


def test_a_public_router_that_will_not_import_does_not_take_the_app_down():
    """Nine engineers share this tree. An unguarded `include_router` would mean
    a half-written developer-platform module stops /chat, /auth, /history and
    the admin console from starting at all — so the import is guarded, and the
    guard records WHY rather than swallowing it.

    The import is made to FAIL, in a child interpreter (wave-2 verifier,
    2026-09-13: the old version only asserted `MOUNT_ERROR is None` with the
    router present, so deleting the try/except still passed)."""
    out = _import_main_with_broken("app.publicapi.router")
    assert "PUBLIC False ImportError" in out, out
    assert "HEALTH True True" in out, out
    assert "V1 False" in out, out
    # With no /v1 router at all, nothing in `app/publicapi/` can answer an
    # unrouted `/v1` path — main.py's HTTPException handler is what keeps
    # CONTRACT-3 §9's envelope, §7's request id and §3.2's CORS on it.
    assert "UNROUTED 404 True True https://dev.example False" in out, out
    # The console is its own guard: a broken /v1 does not unmount it.
    assert "CONSOLE True None" in out, out


def test_a_console_router_that_will_not_import_does_not_take_the_app_down():
    out = _import_main_with_broken("app.apiplatform.console_api")
    assert "CONSOLE False ImportError" in out, out
    assert "DEVELOPERS False" in out, out
    assert "HEALTH True True" in out, out
    # Not asserted either way for /v1: `app/apiplatform/__init__.py` imports
    # `console_api`, so a broken console module takes the whole package — and
    # the /v1 router that depends on it — down too. What matters here is that
    # the CHAT application still starts.


def test_the_running_process_mounted_both_developer_platform_routers():
    assert app_main.PUBLIC_API_MOUNTED and app_main.PUBLIC_API_MOUNT_ERROR is None
    assert app_main.CONSOLE_API_MOUNTED and app_main.CONSOLE_API_MOUNT_ERROR is None


def _app_paths() -> set[str]:
    """Every path the application will route, flattened.

    FastAPI 0.141 keeps an included router as a lazy `_IncludedRouter` object
    with no `path` of its own (the real router hangs off `original_router`), so
    a single pass over `app.routes` silently misses everything that was mounted
    rather than declared here — which is every route this file cares about.
    """
    found: set[str] = set()

    def walk(routes) -> None:
        for route in routes:
            path = getattr(route, "path", None)
            if path:
                found.add(path)
            for attribute in ("original_router", "router", "app"):
                sub = getattr(route, attribute, None)
                if sub is not None and sub is not route:
                    walk(getattr(sub, "routes", ()))

    walk(app.routes)
    return found


def test_the_mount_state_matches_whether_v1_routes_actually_exist():
    """The flag is not decoration: it says whether the surface is there, so a
    test and an operator can ask instead of inferring it from a 404."""
    mounted = {path for path in _app_paths() if path.startswith("/v1")}
    assert bool(mounted) == app_main.PUBLIC_API_MOUNTED


@pytest.mark.skipif(
    not app_main.PUBLIC_API_MOUNTED, reason="the /v1 router has not landed yet"
)
def test_the_mounted_surface_reads_the_key_and_never_the_session_cookie(alice):
    """CONTRACT-3 §1, and the reason the CSRF exemption above is safe. A route
    that accepted BOTH credentials would be a confused deputy: any page on the
    internet could drive it with a signed-in visitor's cookie. Alice holds a
    real session here and it buys her nothing."""
    anonymous = TestClient(app).post("/v1/responses", json={})
    assert anonymous.status_code == 401, anonymous.text
    assert anonymous.json()["error"]["code"] == "invalid_api_key"

    with_cookie = alice.post("/v1/responses", json={})
    assert with_cookie.status_code == 401, with_cookie.text
    assert with_cookie.json()["error"]["code"] == "invalid_api_key"


# ---------------------------------------------------------------------------
# Lifespan wiring: the pepper store and the webhook delivery loop
# ---------------------------------------------------------------------------


def _install_fake_webhook_worker(monkeypatch, *, start=None, stop=None):
    """Stand a module in at `app.apiplatform.webhooks.worker`.

    The real `worker.start()/stop()` drain a PostgreSQL queue and make outbound
    HTTPS requests, neither of which belongs in a test of the LIFESPAN. What is
    under test here is only the wiring — started on the way up, stopped on the
    way down, survivable when it throws — and a stand-in is the only way to
    observe those four facts directly. The real module is the one the
    application imports; it exposes exactly this pair (a synchronous `start`, an
    awaitable `stop`), which is why both spellings are accepted below.
    """
    calls: list[str] = []

    def default_start():
        calls.append("start")

    def default_stop():
        calls.append("stop")

    worker = types.ModuleType("app.apiplatform.webhooks.worker")
    worker.start = start or default_start
    worker.stop = stop or default_stop
    package = types.ModuleType("app.apiplatform.webhooks")
    package.worker = worker
    package.__path__ = []  # a package, so `from … import worker` resolves
    monkeypatch.setitem(sys.modules, "app.apiplatform.webhooks", package)
    monkeypatch.setitem(sys.modules, "app.apiplatform.webhooks.worker", worker)
    return calls


def test_the_webhook_delivery_loop_is_started_and_stopped_by_the_lifespan(
    monkeypatch,
):
    calls = _install_fake_webhook_worker(monkeypatch)
    with TestClient(app):
        assert calls == ["start"]
    assert calls == ["start", "stop"]


def test_a_webhook_worker_whose_start_is_a_coroutine_is_awaited(monkeypatch):
    """`start()` is synchronous in web_worker and continuity and a coroutine in
    the video and artifact pipelines. Accepting both means this wiring does not
    have to change the day the worker's author picks one."""
    calls: list[str] = []

    async def start():
        calls.append("start")

    async def stop():
        calls.append("stop")

    _install_fake_webhook_worker(monkeypatch, start=start, stop=stop)
    with TestClient(app):
        assert calls == ["start"]
    assert calls == ["start", "stop"]


def test_a_webhook_worker_that_throws_on_start_does_not_stop_the_application(
    monkeypatch, caplog
):
    def start():
        raise RuntimeError("queue unavailable")

    stopped: list[str] = []
    _install_fake_webhook_worker(
        monkeypatch, start=start, stop=lambda: stopped.append("stop")
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code in (200, 503)
    # Never stopped, because it never started — `stop()` on a loop that does
    # not exist is how a shutdown turns into a second incident.
    assert stopped == []


def test_a_webhook_worker_that_throws_on_stop_does_not_break_shutdown(monkeypatch):
    def stop():
        raise RuntimeError("already gone")

    _install_fake_webhook_worker(monkeypatch, stop=stop)
    with TestClient(app):
        pass  # the context manager exiting cleanly IS the assertion


def test_the_api_key_pepper_is_bound_to_the_durable_platform_secret_store():
    """CONTRACT-3 §5: `keys.py` refuses to invent a pepper on its own and must
    be told where one lives, once, at start-up. Without this wiring every key
    operation raises `PepperUnavailable`."""
    from app.apiplatform import keys as api_keys

    api_keys.reset_pepper_cache()
    with TestClient(app):
        pepper = api_keys.current_pepper()
        assert pepper
        stored = db.get_platform_secret(api_keys.PEPPER_SECRET_NAME)
    # Either the operator configured one (then nothing is stored) or the
    # platform generated one and it is DURABLE — a pepper that lived only in
    # memory would invalidate every key_hash on the next restart.
    if not getattr(settings, "api_key_pepper", ""):
        assert stored == pepper


def test_the_pepper_saver_can_never_overwrite_an_existing_pepper():
    """The single most destructive write in this platform: overwriting the
    pepper invalidates every stored `key_hash` at once, and the symptom is
    every customer's key answering 401 simultaneously. `db.set_platform_secret`
    is insert-if-absent and returns the STORED value, so a racing writer loses
    rather than corrupts."""
    from app.apiplatform import keys as api_keys

    api_keys.reset_pepper_cache()
    with TestClient(app):
        first = api_keys.current_pepper()
        second = db.set_platform_secret(api_keys.PEPPER_SECRET_NAME, "x" * 64)
        if not getattr(settings, "api_key_pepper", ""):
            assert second == first
            assert db.get_platform_secret(api_keys.PEPPER_SECRET_NAME) == first


# ---------------------------------------------------------------------------
# The developer console backend (CONTRACT-3 §2/§6/§17)
# ---------------------------------------------------------------------------


def test_the_developer_console_router_is_mounted_by_the_application_itself():
    """Wave 1's console suite mounts the router onto the app in an autouse
    fixture "until the wiring lands" — so every console test was green while
    `GET /admin/api/developers/overview` answered 404 in the running process.
    This file never imports that fixture: the path has to be there on its own."""
    assert "/admin/api/developers/overview" in _app_paths()


def test_the_console_answers_through_the_real_app_for_the_right_people_only(
    login_client, anonymous_mode
):
    """One route, through the whole stack: an admin reads it, a member is told
    it does not exist (404, CONTRACT-3 §6), and nobody signed out gets in."""
    admin = login_client("consoleadmin", role="admin")
    created = admin.post("/admin/api/developers/projects", json={"name": "Mounted"})
    assert created.status_code in (200, 201), created.text
    listed = admin.get("/admin/api/developers/projects")
    assert listed.status_code == 200, listed.text
    assert "Mounted" in listed.text

    member = login_client("consolemember")
    assert member.get("/admin/api/developers/projects").status_code == 404

    assert TestClient(app).get("/admin/api/developers/projects").status_code == 401


# ---------------------------------------------------------------------------
# /v1 responses that main.py renders itself: §7 X-Request-Id, §9, §3.2 CORS
# ---------------------------------------------------------------------------


def test_a_413_on_v1_carries_a_request_id_and_the_v1_cors_headers():
    """Wave-2 verifier: the 413 had no `X-Request-Id`, `request_id: null`, and
    no CORS headers (browser CORS skips `/v1`, and the router's `_decorate`
    never runs for a response the router did not produce) — a developer's
    browser SDK saw an opaque failure instead of `request_too_large`.

    Posted to `/v1/embeddings` since 2026-09-13, one byte over its cap —
    8 MiB since the no-timeout sidecar work (CONTRACT §8.4, 2026-09-14)."""
    from app.publicapi import endpoint_models

    client = TestClient(app)
    response = client.post(
        "/v1/embeddings",
        content=b"x" * (endpoint_models.max_pooling_body_bytes() + 1),
        headers={"content-type": "application/json", "origin": FOREIGN_ORIGIN},
    )
    assert response.status_code == 413, response.text
    request_id = response.headers.get("x-request-id")
    assert request_id and request_id.startswith("req_")
    body = response.json()
    assert body["error"]["code"] == "request_too_large"
    assert body["error"]["request_id"] == request_id
    assert response.headers["access-control-allow-origin"] == FOREIGN_ORIGIN
    assert "X-Request-Id" in response.headers["access-control-expose-headers"]
    assert "access-control-allow-credentials" not in response.headers


def _main_uses_the_public_per_route_caps() -> bool:
    from app.publicapi import models as public_models

    return all(
        app_main.body_cap_for("POST", path).signed_in == public_models.body_cap_for("POST", path)
        for path in ("/v1/responses", "/v1/chat/completions", "/v1/audio/transcriptions", "/v1/files")
    )


def _png_of(padding: int) -> bytes:
    import struct
    import zlib

    raw = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", 64, 64, 8, 2, 0, 0, 0)
    raw += struct.pack(">I", 13) + b"IHDR" + header + struct.pack(">I", zlib.crc32(b"IHDR" + header))
    pad = b"\x00" * padding
    return raw + struct.pack(">I", len(pad)) + b"tEXt" + pad + struct.pack(">I", zlib.crc32(b"tEXt" + pad))


def test_main_py_asks_the_public_per_route_table_for_every_v1_cap():
    """Integration 2026-09-13: `app.main.body_cap_for` delegates `/v1` to
    `publicapi.models.body_cap_for` — one table, the one the edge mirrors."""
    assert _main_uses_the_public_per_route_caps()
    for path in ("/v1/embeddings", "/v1/rerank"):
        assert app_main.body_cap_for("POST", path).signed_in == 8 * 1024 * 1024, path
    for method, path in (
        ("GET", "/v1/responses"),
        ("POST", "/v1/responses/resp_x/cancel"),
        ("GET", "/v1/models"),
    ):
        assert app_main.body_cap_for(method, path).signed_in == 1024 * 1024, (method, path)
    # Anonymous and signed-in are one number on /v1: it never reads a cookie.
    for path in ("/v1/responses", "/v1/audio/transcriptions"):
        cap = app_main.body_cap_for("POST", path)
        assert cap.anonymous == cap.signed_in and cap.family == "public-api"


def test_the_v1_caps_follow_the_public_settings_and_a_broken_table_falls_back_to_one_mib(monkeypatch):
    from app.publicapi import models as public_models

    monkeypatch.setattr(settings, "public_api_max_media_body_bytes", 8 * 1024 * 1024, raising=False)
    monkeypatch.setattr(settings, "public_api_max_audio_body_bytes", 12 * 1024 * 1024, raising=False)
    assert app_main.body_cap_for("POST", "/v1/chat/completions").signed_in == 8 * 1024 * 1024
    assert app_main.body_cap_for("POST", "/v1/audio/transcriptions").signed_in == 12 * 1024 * 1024

    def broken(method, path):
        raise RuntimeError("half-deployed sibling")

    monkeypatch.setattr(public_models, "body_cap_for", broken)
    assert app_main.body_cap_for("POST", "/v1/responses").signed_in == 1024 * 1024


def test_the_mounted_app_lets_an_image_request_and_an_audio_upload_over_one_mib_reach_the_router():
    """Adversarial review 2026-09-13: CONTRACT §8 advertises 20 MiB on the two
    generation routes and 26 MiB on transcriptions, the Next edge enforces
    those, and the router tests pass — but the application's middleware still
    capped every `/v1` path at 1 MiB, so a 2 MiB image request and every audio
    upload over 1 MiB were refused before the router saw them. Unauthenticated
    on purpose: past the middleware the router answers 401, which is the
    proof the body got through."""
    import base64

    client = TestClient(app)
    image = base64.b64encode(_png_of(1_600_000)).decode()
    body = {
        "model": "techsara-35b",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What is this?"},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{image}"},
                ],
            }
        ],
    }
    imaged = client.post("/v1/responses", json=body)
    audio = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", b"\x00" * (3 * 1024 * 1024), "audio/wav")},
        data={"model": "techsara-whisper"},
    )
    assert imaged.status_code == 401, imaged.text[:200]
    assert audio.status_code == 401, audio.text[:200]


def test_a_twenty_mib_audio_upload_and_a_large_image_request_pass_the_middleware_and_the_caps_still_hold():
    """The integration's acceptance numbers through the mounted app: a 20 MiB
    audio upload (under the 26 MiB transcription cap) and an image request of
    about 6.5 MiB (under the 20 MiB generation cap) reach the router — 401,
    because they carry no key — while one byte over each cap is still the
    middleware's 413 in the public envelope."""
    import base64

    from app.publicapi import models as public_models

    client = TestClient(app)
    audio = client.post(
        "/v1/audio/transcriptions",
        files={"file": ("standup.wav", b"\x00" * (20 * 1024 * 1024), "audio/wav")},
        data={"model": "techsara-whisper"},
    )
    assert audio.status_code == 401, audio.text[:200]
    assert audio.json()["error"]["code"] != "request_too_large"

    image = base64.b64encode(_png_of(5 * 1024 * 1024)).decode()
    imaged = client.post(
        "/v1/chat/completions",
        json={
            "model": "techsara-8b-vision",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Read the slide."},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                    ],
                }
            ],
        },
    )
    assert imaged.status_code == 401, imaged.text[:200]

    for path, cap in (
        ("/v1/audio/transcriptions", public_models.max_audio_body_bytes()),
        ("/v1/responses", public_models.max_media_body_bytes()),
    ):
        over = client.post(path, content=b"x" * (cap + 1), headers={"content-type": "application/json"})
        assert over.status_code == 413, (path, over.text[:200])
        assert over.json()["error"]["code"] == "request_too_large"


def test_a_body_over_its_cap_on_a_json_only_public_route_is_still_refused_by_the_middleware():
    """The other half: embeddings and rerank carry 8 MiB of JSON (2,048 inputs,
    1,000 documents; CONTRACT §8.4/§8.5, 2026-09-14) and not a byte more, and
    the media and audio caps are not theirs."""
    from app.publicapi import endpoint_models

    client = TestClient(app)
    for path in ("/v1/embeddings", "/v1/rerank"):
        response = client.post(
            path,
            content=b"x" * (endpoint_models.max_pooling_body_bytes() + 1),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 413, (path, response.text[:200])


def test_a_chunked_oversize_body_on_a_public_route_is_a_413_not_a_500():
    """`PublicRoute` turns every exception it did not construct into a 500
    `internal_error` — including the middleware's refusal raised out of the
    body read. The middleware therefore replaces whatever response starts
    after the count crossed the cap. A 500 would tell an SDK to retry a
    request that can never succeed."""
    from fastapi import APIRouter
    import httpx
    import asyncio

    from app.publicapi.router import PublicRoute

    probe = APIRouter(prefix="/v1", route_class=PublicRoute)

    @probe.post("/__chunked_probe")
    async def chunked_probe(payload: dict) -> dict:
        return {"ok": True}

    before = list(app.router.routes)
    app.include_router(probe)
    try:
        async def chunks():
            for _ in range(3):
                yield b"x" * (512 * 1024)

        async def send():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                return await client.post(
                    "/v1/__chunked_probe",
                    content=chunks(),
                    headers={"content-type": "application/json", "origin": FOREIGN_ORIGIN},
                )

        response = asyncio.run(send())
    finally:
        app.router.routes[:] = before
    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "request_too_large"
    assert response.headers.get("x-request-id", "").startswith("req_")
    assert response.headers.get("access-control-allow-origin") == FOREIGN_ORIGIN


def test_an_unknown_v1_path_answers_in_the_public_error_envelope():
    """CONTRACT-3 §9 "Error, everywhere". The router's preflight catch-all
    made every unknown `/v1` path a `405 {"detail": "Method Not Allowed"}`
    with `allow: OPTIONS` — FastAPI's shape, no request id, no CORS."""
    client = TestClient(app)
    for method in ("GET", "POST"):
        response = client.request(
            method, "/v1/does-not-exist", headers={"origin": FOREIGN_ORIGIN}
        )
        assert response.status_code == 404, (method, response.text)
        body = response.json()
        assert set(body) == {"error"}
        assert set(body["error"]) == {"message", "type", "code", "param", "request_id"}
        assert body["error"]["request_id"] == response.headers["x-request-id"]
        assert response.headers["access-control-allow-origin"] == FOREIGN_ORIGIN
        assert "access-control-allow-credentials" not in response.headers


def test_a_real_method_mismatch_on_v1_is_a_405_in_the_envelope():
    response = TestClient(app).delete("/v1/models")
    assert response.status_code == 405, response.text
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]
    assert "GET" in response.headers.get("allow", "")


def test_the_chat_app_keeps_fastapis_own_error_shape():
    client = TestClient(app)
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "x-request-id" not in response.headers


# ---------------------------------------------------------------------------
# Lifespan: the real webhook worker, and platform retention
# ---------------------------------------------------------------------------


def test_the_real_webhook_delivery_loop_runs_while_the_app_runs():
    """The stand-in tests above pin the wiring; this one pins that the module
    the application actually imports is the one that starts."""
    from app.apiplatform.webhooks import worker

    with TestClient(app):
        assert worker.running() is worker.enabled()
    assert worker.running() is False


def test_platform_retention_is_scheduled_by_the_lifespan_on_the_maintenance_cadence(
    monkeypatch,
):
    """`db.prune_api_platform` existed from wave 1 and nothing called it, so
    idempotency claims, minute counters, expired responses and delivered
    webhooks grew without bound. The lifespan now runs it on the artifact
    maintenance cadence and cancels it on the way down."""
    import threading
    import time

    ran = threading.Event()
    calls: list[int] = []

    def fake_prune(**kwargs):
        calls.append(1)
        ran.set()
        return {"api_idempotency": 0}

    monkeypatch.setattr(db, "prune_api_platform", fake_prune)
    monkeypatch.setattr(app_main, "_API_PLATFORM_PRUNE_FIRST_DELAY_S", 0.0)
    with TestClient(app) as client:
        assert ran.wait(10), "the prune never ran"
        tasks = client.portal.call(
            lambda: __import__("asyncio").all_tasks()
        )
        names = {task.get_name() for task in tasks}
        assert "api-platform-prune" in names
    count = len(calls)
    time.sleep(0.2)
    assert len(calls) == count  # cancelled with the lifespan

    monkeypatch.setattr(settings, "artifact_maintenance_interval_s", 1800.0)
    assert app_main._api_platform_prune_interval_s() == 1800.0
    monkeypatch.setattr(settings, "artifact_maintenance_interval_s", 5.0)
    assert app_main._api_platform_prune_interval_s() == 60.0


def test_a_retention_pass_prunes_the_real_tables_and_never_raises(monkeypatch):
    import asyncio

    removed = asyncio.run(app_main.run_api_platform_prune())
    assert removed is not None
    assert {"api_idempotency", "api_usage_minute", "api_responses", "api_webhook_deliveries"} <= set(removed)

    def broken(**kwargs):
        raise RuntimeError("database away")

    monkeypatch.setattr(db, "prune_api_platform", broken)
    assert asyncio.run(app_main.run_api_platform_prune()) is None


# ---------------------------------------------------------------------------
# The maintenance loop must outlive anything a sweep does (2026-09-13)
# ---------------------------------------------------------------------------


async def _run_loop_until(predicate, *, timeout_s: float = 15.0):
    """Run the REAL `_api_platform_prune_loop` until `predicate()` holds or
    the deadline passes; returns (held, loop_still_running). A hard deadline,
    so a loop that died — or never ticks — fails fast instead of hanging."""
    import asyncio
    import time

    task = asyncio.get_running_loop().create_task(app_main._api_platform_prune_loop())
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline and not task.done() and not predicate():
            await asyncio.sleep(0.01)
        return predicate(), not task.done()
    finally:
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 — cancelled, or the crash under test
            pass


def test_the_retention_loop_survives_a_prune_that_returns_something_other_than_a_dict(
    monkeypatch,
):
    """The wave-3 re-verifier's probe: `db.prune_api_platform` returning None.
    `any(removed.values())` sat outside the try, so the AttributeError ended
    the loop after one pass, silently, for the life of the process."""
    import asyncio

    calls: list[int] = []

    def odd_prune(**kwargs):
        calls.append(1)
        return None

    monkeypatch.setattr(db, "prune_api_platform", odd_prune)
    monkeypatch.setattr(db, "expire_rotated_keys", lambda **kwargs: [])
    monkeypatch.setattr(app_main, "_API_PLATFORM_PRUNE_FIRST_DELAY_S", 0.0)
    monkeypatch.setattr(app_main, "_api_platform_prune_interval_s", lambda: 0.0)

    held, running = asyncio.run(_run_loop_until(lambda: len(calls) >= 3))
    assert held, f"the loop stopped after {len(calls)} prune pass(es)"
    assert running
    assert asyncio.run(app_main.run_api_platform_prune()) is None


def test_the_loop_survives_a_sweep_that_raises_despite_its_contract_and_runs_the_next_one(
    monkeypatch,
):
    """Belt and braces: the sweeps are written never to raise, and the tick
    does not rely on it. A broken prune must not starve the key expiry, and
    an unreadable interval must not end the loop either."""
    import asyncio

    expiries: list[int] = []

    async def exploding_prune():
        raise RuntimeError("a sweep that broke its own contract")

    async def counting_expiry():
        expiries.append(1)
        return 0

    intervals = iter([ValueError("interval unreadable")] + [0.0] * 1000)

    def interval():
        value = next(intervals)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(app_main, "run_api_platform_prune", exploding_prune)
    monkeypatch.setattr(app_main, "run_rotated_key_expiry", counting_expiry)
    monkeypatch.setattr(app_main, "_API_PLATFORM_PRUNE_FIRST_DELAY_S", 0.0)
    monkeypatch.setattr(app_main, "_api_platform_prune_interval_s", interval)
    # The unreadable interval falls back to 30 minutes; shorten the sleep that
    # follows so the test sees the next tick.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(app_main.asyncio, "sleep", lambda s: real_sleep(min(s, 0.01)))

    held, running = asyncio.run(_run_loop_until(lambda: len(expiries) >= 3))
    assert held, f"key expiry ran {len(expiries)} time(s)"
    assert running


def test_rotated_out_keys_are_revoked_by_the_scheduled_loop_against_the_real_database(
    monkeypatch,
):
    """`db.expire_rotated_keys` was tested and NEVER SCHEDULED: the resolver
    refused a key past its overlap, but the console showed it live forever.
    The real loop, the real sweep, the real tables: a key whose own deadline
    has passed is revoked on the first tick, and a key without one is not.

    `db.prune_api_platform` also calls the sweep at its END (the db owner's
    fix of the same day), so the prune is made to fail here: a retention
    statement that times out must not take the revocation record with it,
    which is why the loop schedules the sweep on its own as well."""
    import asyncio
    from datetime import datetime, timedelta, timezone

    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", ("ws-loop-a", "Loop"))
    owner = int(db.create_user("loop-owner", "hash"))
    project = db.create_api_project("ws-loop-a", "Loop project", "live", created_by=owner)

    def key(public_id: str, name: str) -> dict:
        return db.create_api_key(
            project["id"], "ws-loop-a", name, public_id,
            "hmac-digest-never-a-plaintext-key", "ab12",
        )

    old = key("publoop000000001", "rotated-out")
    live = key("publoop000000002", "current")
    db.update_api_key_rotation(
        old["id"], "ws-loop-a", datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    def prune_that_times_out(**kwargs):
        raise RuntimeError("canceling statement due to statement timeout")

    monkeypatch.setattr(db, "prune_api_platform", prune_that_times_out)
    monkeypatch.setattr(app_main, "_API_PLATFORM_PRUNE_FIRST_DELAY_S", 0.0)
    monkeypatch.setattr(app_main, "_api_platform_prune_interval_s", lambda: 0.05)

    def revoked() -> bool:
        return db.api_key_by_public_id(old["public_id"])["status"] == "revoked"

    held, running = asyncio.run(_run_loop_until(revoked))
    assert held, "the rotated-out key was never revoked by the loop"
    assert running
    assert db.api_key_by_public_id(live["public_id"])["status"] == "active"


# ---------------------------------------------------------------------------
# A /v1 response that fails mid-body must reach the caller as a failure
# ---------------------------------------------------------------------------


# Assembler, 2026-09-14: the strict xfail markers that waited for the main.py
# integration are removed; both integrations are in this tree.
@pytest.mark.parametrize("kind", ["committed-abort", "sse"])
def test_a_v1_response_that_fails_mid_body_drops_the_connection_through_the_whole_app(kind):
    """Adversarial review of T3-wire, 2026-09-14. Starlette's BaseHTTPMiddleware
    re-streams the body through a memory stream, and when the app raises
    mid-body it ends that stream CLEANLY and re-raises only after the outer
    response is complete. Measured on uvicorn: without it a committed abort
    and an SSE generator raising mid-body both give the client
    `httpx.RemoteProtocolError`; with it both give a complete 200 (a
    whitespace-only JSON body, a truncated stream) and no error — so an SDK
    parses a success instead of retrying, the gateway cannot see an
    incomplete read, and T4's `failure_mode="abort"` and T2's `suspend_all`
    reader aborts are silently turned into answers. Through the REAL
    `app.main:app` stack, on a real socket: an in-process client buffers the
    body and would prove nothing."""
    import asyncio

    import httpx
    from fastapi.responses import StreamingResponse

    from app.publicapi import keepalive
    from tests.test_publicapi_sync_commit import serve

    async def committed_abort():
        async def work():
            await asyncio.sleep(0.3)
            raise RuntimeError("the engine went away after the commit")

        return keepalive.CommittedJSONResponse(
            work, failure_mode=keepalive.FAILURE_ABORT, commit_s=0.05, heartbeat_s=0.05
        )

    async def sse():
        async def frames():
            yield "event: response.created\ndata: {}\n\n"
            await asyncio.sleep(0.2)
            raise RuntimeError("the reader was suspended")

        return StreamingResponse(frames(), media_type="text/event-stream")

    endpoint = committed_abort if kind == "committed-abort" else sse
    path = f"/v1/__mount_abort_{kind.replace('-', '_')}"
    app.add_api_route(path, endpoint, methods=["GET"])
    added = app.router.routes[-1]
    try:
        with serve(app) as port:
            received = b""
            error = None
            with httpx.Client(timeout=10) as client:
                with client.stream("GET", f"http://127.0.0.1:{port}{path}") as response:
                    assert response.status_code == 200
                    try:
                        for chunk in response.iter_raw():
                            received += chunk
                    except httpx.HTTPError as exc:
                        error = exc
    finally:
        if added in app.router.routes:
            app.router.routes.remove(added)
    assert isinstance(error, httpx.RemoteProtocolError), (error, received)
