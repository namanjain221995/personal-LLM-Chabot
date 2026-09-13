"""The eight `/v1` routes, over real HTTP, real keys and a real database.

WHAT THIS SUITE IS FOR. Every assertion here is a refusal the platform makes
its money on being right about: a key that may not use a model, a key without
a scope, another project's response, a browser origin outside the allowlist, a
body over the cap, a quota that has run out, and — the one that would be a
security incident rather than a bug — a session cookie being accepted as a
credential.

WHAT IS STUBBED, AND IT IS ONLY ONE THING. `llm.stream_chat_events`, because
no test should need a GPU. Everything else is the real article: PostgreSQL and
the V34 tables, `apiplatform/resolver.py` resolving a real minted key,
`apiplatform/quotas.py` counting real windows, `publicapi/background.py`
running a real detached job, the router, the error envelope and the SSE
framing. A tenancy predicate tested against a fake is not tested at all, and
the first draft of this file (2026-09-13, before the resolver landed) used a
double whose 401 masked the router's own — which is exactly the class of false
green these tests exist to prevent.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, llm
from app.apiplatform import keys as key_tools, projects, quotas, resolver
from app.apiplatform.scopes import DEFAULT_SCOPES, Scope
from app.config import settings
from app.publicapi import events, registry, router as public_router

WORKSPACE = "ws-public-api"
OTHER_WORKSPACE = "ws-public-api-other"

#: 32 characters, `keys.MIN_PEPPER_CHARS` exactly.
TEST_PEPPER = "routes-pepper-01234567890abcdefg"

#: Filled in by the `platform` fixture: the plaintext of each minted key, which
#: exists exactly once and only here.
TOKENS: Dict[str, str] = {}


@pytest.fixture()
def limits_enforced(monkeypatch):
    """PUBLIC_API_ENFORCE_LIMITS=true for one test.

    The owner decision of 2026-09-13 made the public API unlimited by default,
    so every test below that pins a rate, quota or concurrency REFUSAL (or the
    RateLimit fields) asks for enforcement explicitly: the enforcement code
    stays available to an operator, so it stays tested. The unlimited default
    has its own tests, which do not use this fixture.
    """
    monkeypatch.setattr(settings, "public_api_enforce_limits", True)


@pytest.fixture(autouse=True)
def _pepper(monkeypatch):
    """A configured pepper, and no cached one from a neighbouring test."""
    monkeypatch.setattr(settings, "api_key_pepper", TEST_PEPPER)
    key_tools.reset_pepper_cache()
    yield
    key_tools.reset_pepper_cache()


@pytest.fixture()
def platform(monkeypatch):
    """Two workspaces, two projects, three real keys.

    The smallest world in which "another tenant cannot see this" is a question
    that can be asked — and the keys are minted through `projects.create_key`,
    so the token a test presents is one the real resolver really verifies
    against a real HMAC digest.
    """
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", (WORKSPACE, "Acme"))
        con.execute(
            "INSERT INTO workspaces (id, name) VALUES (%s, %s)", (OTHER_WORKSPACE, "Rival")
        )
    project = projects.create_project(WORKSPACE, "Integration")
    other = projects.create_project(OTHER_WORKSPACE, "Their integration")

    live = projects.create_key(project["id"], WORKSPACE, "server", scopes=list(Scope))
    theirs = projects.create_key(other["id"], OTHER_WORKSPACE, "theirs", scopes=list(Scope))
    # A key that may read but not write — the narrowing CONTRACT §5 calls
    # "scopes are data, not roles": `responses.read` does not imply
    # `responses.write`, so this key can fetch a response and not create one.
    narrow = projects.create_key(
        project["id"],
        WORKSPACE,
        "reader",
        scopes=sorted(s.value for s in DEFAULT_SCOPES - {Scope.RESPONSES_WRITE}),
    )

    # A key whose model allowlist names something that is not the public
    # model. Narrowed at mint time on the key ROW, so what refuses the request
    # is the resolver's allowlist rather than a test's idea of one.
    wrong_model = projects.create_key(
        project["id"],
        WORKSPACE,
        "elsewhere",
        scopes=list(Scope),
        allowed_models=["some-other-model"],
    )

    TOKENS.clear()
    TOKENS.update(
        {
            "live": live.token,
            "other": theirs.token,
            "narrow": narrow.token,
            "wrongmodel": wrong_model.token,
        }
    )

    # Concurrency is process state, not request state (CONTRACT §12), so a
    # test that left a slot held would fail the next one for a reason that has
    # nothing to do with it.
    quotas.reset_concurrency()
    yield {
        "project": project,
        "other": other,
        "live": live,
        "narrow": narrow,
        "theirs": theirs,
    }
    quotas.reset_concurrency()
    TOKENS.clear()


@pytest.fixture()
def api(platform):
    """A bare app carrying ONLY the public router.

    Deliberately not `app.main.app`: mounting the whole application would test
    the chat app's middleware stack as well, and the point of these assertions
    is that `/v1` makes its own decisions rather than inheriting somebody
    else's. How it is WIRED into the application is pinned separately, in
    tests/test_publicapi_mount.py.
    """
    app = FastAPI()
    app.include_router(public_router.router)
    public_router.install_error_handlers(app)
    with TestClient(app) as client:
        yield client


class _Engine:
    """`llm.stream_chat_events`, stubbed."""

    def __init__(self, pieces: List[str], *, fail: Optional[BaseException] = None) -> None:
        self.pieces = pieces
        self.fail = fail
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        return self._run()

    async def _run(self):
        for piece in self.pieces:
            await asyncio.sleep(0)
            yield ("token", piece)
        if self.fail is not None:
            raise self.fail


@pytest.fixture()
def engine(monkeypatch):
    def install(pieces, *, fail=None, usage=None):
        fake = _Engine(pieces, fail=fail)
        monkeypatch.setattr(llm, "stream_chat_events", fake)
        monkeypatch.setattr(llm, "reset_usage", lambda: None)
        monkeypatch.setattr(llm, "get_usage", lambda: usage)
        return fake

    return install


def _auth(which: str = "live") -> Dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[which]}"}


def _caller(which: str = "live"):
    """The `ApiCaller` the router would resolve for this key — for the tests
    that have to talk to `quotas` directly."""
    return resolver.resolve_api_caller(f"Bearer {TOKENS[which]}")


def _body(**overrides) -> Dict[str, Any]:
    body: Dict[str, Any] = {"model": registry.TECHSARA_35B, "input": "Why is the sky blue?"}
    body.update(overrides)
    return body


#: Every route, as (method, path, body). Used by the tests that must hold for
#: ALL of them — a rule proved on one route is a rule the next route can be
#: written without.
ALL_ROUTES: Tuple[Tuple[str, str, Optional[Dict[str, Any]]], ...] = (
    ("get", "/v1/models", None),
    ("get", f"/v1/models/{registry.TECHSARA_35B}", None),
    ("post", "/v1/responses", {"model": registry.TECHSARA_35B, "input": "hi"}),
    ("get", "/v1/responses/resp_whatever", None),
    ("post", "/v1/responses/resp_whatever/cancel", None),
    (
        "post",
        "/v1/chat/completions",
        {"model": registry.TECHSARA_35B, "messages": [{"role": "user", "content": "hi"}]},
    ),
    ("get", "/v1/usage", None),
)


# ------------------------------------------------------- §1 credentials --


def test_no_route_accepts_a_session_cookie_as_a_credential(api, engine):
    engine(["ignored"])
    api.cookies.set("ts_session", "a-perfectly-valid-looking-session")

    for method, path, body in ALL_ROUTES:
        response = api.request(method, path, json=body)
        # CONTRACT §1: a route that accepted both credentials would be a
        # confused deputy — any page on the internet could drive it with a
        # signed-in visitor's cookie.
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "invalid_api_key", path


def test_the_router_refuses_a_cookie_before_the_resolver_is_ever_reached(
    platform, monkeypatch, engine
):
    """The cookie rule, pinned against the ROUTER rather than the resolver.

    The test above sends a cookie and gets a 401 — but so it would if the
    router had happily passed the cookie on, because `resolve_api_caller`
    would refuse it in its own right. That makes it blind to the mutation that
    matters (checked 2026-09-13: changing `resolve_caller` to fall back to
    `request.cookies.get("ts_session")` left it green).

    So here the resolver is made to accept ANYTHING it is handed. If the router
    reaches it at all with only a cookie present, the request succeeds — and
    that is the confused-deputy hole CONTRACT §1 forbids, so the assertion is
    that it 401s AND that the resolver was never called.
    """
    engine(["ok"])
    real = resolver.resolve_api_caller
    reached: List[str] = []

    def permissive(authorization_header=None, **_address):
        reached.append(str(authorization_header or ""))
        return real(f"Bearer {TOKENS['live']}")

    monkeypatch.setattr(resolver, "resolve_api_caller", permissive)

    app = FastAPI()
    app.include_router(public_router.router)
    public_router.install_error_handlers(app)
    with TestClient(app) as client:
        client.cookies.set("ts_session", "a-perfectly-valid-looking-session")
        refused = client.get("/v1/models")
        # The same permissive resolver DOES let a bearer request through, so
        # the 401 above is the cookie being ignored and not a refusal from
        # somewhere else.
        allowed = client.get("/v1/models", headers=_auth())

    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "invalid_api_key"
    assert len(reached) == 1  # only the bearer request got that far
    assert "a-perfectly-valid-looking-session" not in reached[0]
    assert allowed.status_code == 200


def test_a_missing_or_unknown_key_is_the_same_401_on_every_route(api, engine):
    engine(["ignored"])
    for method, path, body in ALL_ROUTES:
        missing = api.request(method, path, json=body)
        unknown = api.request(
            method, path, json=body, headers={"Authorization": "Bearer tsk_live_nope_nope"}
        )
        assert missing.status_code == unknown.status_code == 401, path
        # Byte for byte the same sentence: which of the five reasons it was is
        # exactly what a leaked-key scanner wants to learn.
        assert missing.json()["error"]["message"] == unknown.json()["error"]["message"]


def test_every_response_carries_a_request_id(api, engine):
    engine(["ok"])
    ok = api.get("/v1/models", headers=_auth())
    refused = api.get("/v1/models")

    assert ok.headers["X-Request-Id"].startswith("req_")
    assert refused.headers["X-Request-Id"].startswith("req_")
    assert refused.json()["error"]["request_id"] == refused.headers["X-Request-Id"]


def test_a_key_without_the_scope_is_refused_and_told_which_scope(api, engine):
    engine(["ok"])
    response = api.post("/v1/responses", json=_body(), headers=_auth("narrow"))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert response.json()["error"]["type"] == "permission_error"
    # RFC 6750: the challenge names what to ask for; the envelope stays ours.
    assert "responses.write" in response.headers["WWW-Authenticate"]


def test_a_scope_the_key_does_hold_is_never_named_in_the_refusal(api, engine):
    engine(["ok"])
    response = api.post("/v1/responses", json=_body(), headers=_auth("narrow"))

    # `models.read` and `responses.read` ARE held. Publishing the granted set
    # in a refusal tells an attacker what else the credential is good for.
    body = response.text + str(response.headers)
    assert "models.read" not in body
    assert "responses.read" not in body


# ---------------------------------------------------------- §7 models --


def test_a_valid_key_lists_the_models_it_may_use(api, engine):
    engine(["ok"])
    response = api.get("/v1/models", headers=_auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "list"
    assert [m["id"] for m in payload["data"]] == [registry.TECHSARA_35B]
    # The internal target never leaves the server: which checkpoint answers is
    # an operational detail, and publishing it reads out our upgrade schedule.
    assert "internal" not in payload["data"][0]
    assert "Qwen" not in response.text


def test_an_unknown_model_and_a_forbidden_one_are_the_same_404(api, platform, engine):
    engine(["ok"])
    unknown = api.get("/v1/models/does-not-exist", headers=_auth())

    forbidden = api.get(
        f"/v1/models/{registry.TECHSARA_35B}", headers=_auth("wrongmodel")
    )

    assert unknown.status_code == forbidden.status_code == 404
    assert unknown.json()["error"]["code"] == forbidden.json()["error"]["code"]
    assert forbidden.json()["error"]["code"] == "model_not_found"


def test_a_model_the_key_may_not_use_is_absent_from_the_list_as_well(
    api, platform, engine
):
    engine(["ok"])
    response = api.get("/v1/models", headers=_auth("wrongmodel"))

    assert response.json()["data"] == []


# ------------------------------------------------------- §8 validation --


def test_a_parameter_the_platform_cannot_honour_is_a_400_naming_the_field(api, engine):
    engine(["ok"])
    response = api.post("/v1/responses", json=_body(top_p=0.9), headers=_auth())

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request_error"
    assert response.json()["error"]["param"] == "top_p"


def test_a_body_over_the_cap_is_refused_before_it_is_parsed(api, engine, monkeypatch):
    engine(["ok"])
    monkeypatch.setattr("app.publicapi.models.max_body_bytes", lambda: 512)

    response = api.post("/v1/responses", json=_body(input="x" * 4096), headers=_auth())

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"


def test_a_body_that_is_not_json_never_echoes_the_decoder_s_view_of_it(api, engine):
    engine(["ok"])
    response = api.post(
        "/v1/responses",
        content=b'{"model": "techsara-35b", "input": "secret prompt"',
        headers={**_auth(), "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    # CONTRACT §16: the body is the prompt, and we do not echo one.
    assert "secret prompt" not in response.text


def test_stream_and_background_together_are_refused(api, engine):
    engine(["ok"])
    response = api.post(
        "/v1/responses", json=_body(stream=True, background=True), headers=_auth()
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request_error"


def test_a_temperature_outside_the_documented_range_is_refused(api, engine):
    engine(["ok"])
    response = api.post("/v1/responses", json=_body(temperature=3.0), headers=_auth())

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "temperature"


def test_max_output_tokens_above_the_model_ceiling_is_refused_not_clamped(api, engine):
    engine(["ok"])
    response = api.post(
        "/v1/responses", json=_body(max_output_tokens=10_000_000), headers=_auth()
    )

    assert response.status_code == 400
    # Silently clamping would bill for a truncated answer the caller believed
    # was complete (CONTRACT §8).
    assert response.json()["error"]["param"] == "max_output_tokens"


# ------------------------------------------------------ §9 the answer --


def test_a_synchronous_response_is_the_documented_envelope(api, engine):
    engine(["Because ", "of scattering."], usage={"prompt_tokens": 37, "completion_tokens": 112})
    response = api.post("/v1/responses", json=_body(), headers=_auth())

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["model"] == registry.TECHSARA_35B
    assert payload["output"][0]["content"][0]["text"] == "Because of scattering."
    assert payload["usage"] == {
        "input_tokens": 37,
        "output_tokens": 112,
        "total_tokens": 149,
    }
    assert payload["id"].startswith("resp_")


def test_usage_is_null_and_never_zero_when_the_engine_did_not_report(api, engine):
    engine(["hi"], usage=None)
    response = api.post("/v1/responses", json=_body(), headers=_auth())

    assert response.json()["usage"] is None


def test_a_recovering_engine_is_a_503_with_a_retry_after(api, engine, monkeypatch):
    class QueuedForRecovery(RuntimeError):
        pass

    QueuedForRecovery.__name__ = "QueuedForRecovery"
    monkeypatch.setattr(
        "app.publicapi.streaming._engine_state_name", lambda: "RECOVERING"
    )
    engine([], fail=QueuedForRecovery("the model is restarting"))

    response = api.post("/v1/responses", json=_body(), headers=_auth())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_recovering"
    assert int(response.headers["Retry-After"]) >= 1


def test_an_unexpected_failure_never_puts_its_text_on_the_wire(api, engine):
    engine(
        [],
        fail=RuntimeError(
            'connection to server at "vllm-router" (172.18.0.4), port 30002 failed; '
            "see /app/app/llm.py"
        ),
    )

    response = api.post("/v1/responses", json=_body(), headers=_auth())

    assert response.status_code == 500
    for forbidden in ("vllm-router", "172.18.0.4", "/app/", "Traceback"):
        assert forbidden not in response.text
    assert response.json()["error"]["code"] == "internal_error"


# ------------------------------------------------------ §10 streaming --


def test_a_streamed_response_is_the_documented_sse_lifecycle(api, engine):
    engine(["Blue ", "sky."], usage={"prompt_tokens": 5, "completion_tokens": 2})
    response = api.post("/v1/responses", json=_body(stream=True), headers=_auth())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["x-accel-buffering"] == "no"
    # A streamed body has no length; a proxy that sees one truncates at it.
    assert "content-length" not in {k.lower() for k in response.headers}

    records = events.parse_frames(response.text)
    assert [r["event"] for r in records][0] == "response.created"
    assert [r["event"] for r in records][-1] == "response.completed"
    assert [r["data"]["sequence_number"] for r in records] == list(
        range(1, len(records) + 1)
    )
    terminals = [r for r in records if r["event"] in events.TERMINAL_EVENTS]
    assert len(terminals) == 1


def test_a_streamed_generation_never_enters_the_chat_apps_live_registry(api, engine):
    engine(["a", "b"])
    from app import main as chat_app

    before = dict(chat_app._live_generations)
    api.post("/v1/responses", json=_body(stream=True), headers=_auth())

    # That registry is one entry per conversation and a second request under
    # the same key CANCELS the first. On this surface that would be a denial of
    # service a customer could inflict on themselves (CONTRACT §11).
    assert dict(chat_app._live_generations) == before


# ------------------------------------------------- §7 read and cancel --


def test_another_projects_response_reads_as_missing(api, engine):
    engine(["ok"])
    created = api.post("/v1/responses", json=_body(), headers=_auth()).json()

    mine = api.get(f"/v1/responses/{created['id']}", headers=_auth())
    theirs = api.get(f"/v1/responses/{created['id']}", headers=_auth("other"))

    assert mine.status_code == 200
    # 404, not 403: existence is not disclosed across a tenant boundary.
    assert theirs.status_code == 404
    assert theirs.json()["error"]["code"] == "response_not_found"


def test_cancel_is_idempotent_and_cannot_rewrite_a_finished_response(api, engine):
    engine(["ok"])
    created = api.post("/v1/responses", json=_body(), headers=_auth()).json()

    first = api.post(f"/v1/responses/{created['id']}/cancel", headers=_auth())
    second = api.post(f"/v1/responses/{created['id']}/cancel", headers=_auth())

    assert first.status_code == second.status_code == 200
    # It had already completed, so cancelling changed nothing — twice.
    assert first.json()["status"] == second.json()["status"] == "completed"


def test_cancelling_another_projects_response_is_the_same_404(api, engine):
    engine(["ok"])
    created = api.post("/v1/responses", json=_body(), headers=_auth()).json()

    response = api.post(f"/v1/responses/{created['id']}/cancel", headers=_auth("other"))

    assert response.status_code == 404


def test_a_background_response_is_durable_before_the_202_is_returned(api, engine, platform):
    engine(["slow ", "answer"])
    response = api.post("/v1/responses", json=_body(background=True), headers=_auth())

    assert response.status_code == 202
    payload = response.json()
    assert payload["status"] == "queued"
    # The promise is durable, not in-memory: a restart between the 202 and the
    # work must not lose the request (CONTRACT §14).
    row = db.get_api_response(payload["id"], platform["project"]["id"])
    assert row is not None and row["background"] is True


# ---------------------------------------------------------- §12 quotas --


@pytest.mark.usefixtures("limits_enforced")
def test_the_rate_limit_refuses_the_next_request_in_the_same_minute(
    api, platform, engine
):
    engine(["ok"])
    projects.update_project(platform["project"]["id"], WORKSPACE, rpm=1)

    first = api.post("/v1/responses", json=_body(), headers=_auth())
    second = api.post("/v1/responses", json=_body(), headers=_auth())

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limit_error"
    assert int(second.headers["Retry-After"]) >= 1
    # CONTRACT §12: the headers are on every response, not only the refusal.
    assert first.headers["RateLimit"].startswith('"requests";r=')
    assert "RateLimit-Policy" in first.headers


@pytest.mark.usefixtures("limits_enforced")
def test_the_rate_limit_headers_are_on_every_response_including_the_refusal(
    api, platform, engine
):
    """CONTRACT §12: `RateLimit` and `RateLimit-Policy` on every `/v1` response.

    Including the routes that do not generate anything, and including the 429
    — which is the response a client most needs them on, because it is how
    they learn the window is empty rather than inferring it from `Retry-After`.
    """
    engine(["ok"])
    # Two, because the read route SPENDS one now (2026-09-13: every
    # authenticated route is metered, not only the generating ones).
    projects.update_project(platform["project"]["id"], WORKSPACE, rpm=2)

    listed = api.get("/v1/models", headers=_auth())
    spent = api.post("/v1/responses", json=_body(), headers=_auth())
    refused = api.post("/v1/responses", json=_body(), headers=_auth())

    for response in (listed, spent, refused):
        assert response.headers["RateLimit"].startswith('"requests";r=')
        assert '"requests";q=2;w=60' in response.headers["RateLimit-Policy"]
    assert listed.headers["RateLimit"].startswith('"requests";r=1;')
    assert spent.status_code == 200
    assert refused.status_code == 429
    assert refused.headers["RateLimit"].startswith('"requests";r=0;')


@pytest.mark.usefixtures("limits_enforced")
def test_a_spent_daily_quota_is_a_429_quota_exceeded(api, platform, engine):
    engine(["ok"])
    projects.update_project(platform["project"]["id"], WORKSPACE, daily_token_quota=10)
    db.bump_usage_daily(platform["project"]["id"], input_tokens=8, output_tokens=8)

    response = api.post("/v1/responses", json=_body(), headers=_auth())

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota_exceeded"
    assert int(response.headers["Retry-After"]) >= 1


@pytest.mark.usefixtures("limits_enforced")
def test_the_quota_gate_runs_before_the_engine_is_ever_called(api, platform, engine):
    fake = engine(["ok"])
    projects.update_project(platform["project"]["id"], WORKSPACE, rpm=1)

    api.post("/v1/responses", json=_body(), headers=_auth())
    api.post("/v1/responses", json=_body(), headers=_auth())

    # The ten NORMAL admission lanes are SHARED with the chat application, so a
    # refused request must not have taken one (CONTRACT §11).
    assert fake.calls == 1


@pytest.mark.usefixtures("limits_enforced")
def test_a_streaming_request_over_the_concurrency_limit_is_a_429_not_a_broken_stream(
    api, platform, engine
):
    """The refusal has to happen before the 200 is committed.

    A streaming route returns before a single byte is written, so a slot taken
    inside the response body would refuse a request whose status line already
    said 200 — the caller would see a stream that died rather than
    `concurrency_limit_exceeded` with its `Retry-After`, and a client's retry
    logic keys on the status code it never received.
    """
    engine(["ok"])
    projects.update_project(platform["project"]["id"], WORKSPACE, max_concurrency=1)
    # One request already in flight for this key — taken through the real
    # counter, not by writing a private dictionary.
    held = quotas.concurrency_slot(_caller())
    held.__enter__()

    response = api.post("/v1/responses", json=_body(stream=True), headers=_auth())

    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "concurrency_limit_exceeded"
    assert int(response.headers["Retry-After"]) >= 1


def test_a_finished_stream_gives_its_concurrency_slot_back(api, platform, engine):
    engine(["ok"])
    projects.update_project(platform["project"]["id"], WORKSPACE, max_concurrency=1)

    first = api.post("/v1/responses", json=_body(stream=True), headers=_auth())
    second = api.post("/v1/responses", json=_body(stream=True), headers=_auth())

    assert first.status_code == second.status_code == 200
    # A slot that is never released turns a limit of four into a limit of four
    # for the lifetime of the process.
    assert quotas.in_flight(_caller()) == 0


# ------------------------------------------------------------- §3 CORS --


def test_the_preflight_answers_204_and_never_allows_credentials(api):
    response = api.options(
        "/v1/responses",
        headers={
            "Origin": "https://someone-elses-app.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Origin"] == "https://someone-elses-app.example"
    assert response.headers["Access-Control-Allow-Methods"] == "GET, POST, OPTIONS"
    assert "idempotency-key" in response.headers["Access-Control-Allow-Headers"]
    assert response.headers["Access-Control-Max-Age"] == "600"
    # The header that would let a browser attach a cookie. Its absence is what
    # makes the permissive preflight safe.
    assert "access-control-allow-credentials" not in {
        k.lower() for k in response.headers
    }


def test_an_origin_outside_the_projects_allowlist_is_refused(api, platform, engine):
    engine(["ok"])
    projects.update_project(
        platform["project"]["id"], WORKSPACE, allowed_origins=["https://app.customer.example"]
    )

    allowed = api.post(
        "/v1/responses",
        json=_body(),
        headers={**_auth(), "Origin": "https://app.customer.example"},
    )
    refused = api.post(
        "/v1/responses",
        json=_body(),
        headers={**_auth(), "Origin": "https://evil.example"},
    )

    assert allowed.status_code == 200
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "origin_not_allowed"
    # The rejected origin is attacker-controlled text and is not echoed back
    # into the message; the caller already knows what it sent.
    assert "evil.example" not in refused.json()["error"]["message"]


def test_a_server_side_caller_with_no_origin_is_unaffected_by_the_allowlist(
    api, platform, engine
):
    engine(["ok"])
    projects.update_project(
        platform["project"]["id"], WORKSPACE, allowed_origins=["https://app.customer.example"]
    )

    response = api.post("/v1/responses", json=_body(), headers=_auth())

    # An allowlist is a browser control. A key used from a server sends no
    # Origin, and refusing it would break every backend integration.
    assert response.status_code == 200


def test_no_successful_response_ever_allows_credentials(api, engine):
    engine(["ok"])
    response = api.get(
        "/v1/models", headers={**_auth(), "Origin": "https://app.customer.example"}
    )

    assert response.headers["Access-Control-Allow-Origin"] == "https://app.customer.example"
    assert "access-control-allow-credentials" not in {
        k.lower() for k in response.headers
    }
    assert response.headers["Vary"] == "Origin"


# ---------------------------------------------------------- §7 usage --


def test_usage_reads_this_projects_own_counters_only(api, platform, engine):
    engine(["ok"])
    db.bump_usage_daily(platform["project"]["id"], requests=3, input_tokens=100)
    db.bump_usage_daily(platform["other"]["id"], requests=99, input_tokens=999_999)

    response = api.get("/v1/usage", headers=_auth())

    assert response.status_code == 200
    rows = response.json()["data"]
    # 3 seeded + this very request: `GET /v1/usage` is metered like every
    # other authenticated route (2026-09-13), and the reservation is written
    # before the range is read.
    assert sum(r["requests"] for r in rows) == 4
    assert sum(r["input_tokens"] for r in rows) == 100


def test_an_unbounded_usage_range_is_refused(api, engine):
    engine(["ok"])
    response = api.get(
        "/v1/usage?start_date=2020-01-01&end_date=2026-12-31", headers=_auth()
    )

    # A client-supplied result size is the denial-of-service shape OWASP API4
    # names directly.
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request_error"


def test_a_malformed_usage_date_is_a_400_naming_the_parameter(api, engine):
    engine(["ok"])
    response = api.get("/v1/usage?start_date=last-tuesday", headers=_auth())

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "start_date"


# ------------------------------------------------ §7 chat completions --


def test_the_compatibility_shape_answers_in_its_own_dialect(api, engine):
    engine(["Hello."], usage={"prompt_tokens": 2, "completion_tokens": 3})
    response = api.post(
        "/v1/chat/completions",
        json={
            "model": registry.TECHSARA_35B,
            "messages": [{"role": "user", "content": "Say hello."}],
        },
        headers=_auth(),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"] == {"role": "assistant", "content": "Hello."}
    assert payload["usage"]["total_tokens"] == 5


def test_the_compatibility_shape_refuses_a_field_it_cannot_honour(api, engine):
    engine(["ok"])
    response = api.post(
        "/v1/chat/completions",
        json={
            "model": registry.TECHSARA_35B,
            "messages": [{"role": "user", "content": "hi"}],
            "logit_bias": {"1": 2},
        },
        headers=_auth(),
    )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "logit_bias"


def test_the_compatibility_stream_ends_with_the_done_sentinel(api, engine):
    engine(["Hi"])
    response = api.post(
        "/v1/chat/completions",
        json={
            "model": registry.TECHSARA_35B,
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers=_auth(),
    )

    assert response.status_code == 200
    assert response.text.endswith(events.DONE_SENTINEL)
    assert "event:" not in response.text


# --------------------------------------------------------- §13 replay --


def test_the_same_idempotency_key_and_body_does_not_run_the_model_twice(api, engine):
    fake = engine(["once"])
    headers = {**_auth(), "Idempotency-Key": "abc-123"}

    first = api.post("/v1/responses", json=_body(), headers=headers)
    second = api.post("/v1/responses", json=_body(), headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert fake.calls == 1


def test_the_same_idempotency_key_with_a_different_body_is_a_409(api, engine):
    engine(["once"])
    headers = {**_auth(), "Idempotency-Key": "abc-123"}

    api.post("/v1/responses", json=_body(), headers=headers)
    conflict = api.post("/v1/responses", json=_body(input="something else"), headers=headers)

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


# --------------------------------------------------------- §7 schema --


def test_the_public_schema_is_served_without_a_credential(api):
    response = api.get("/v1/openapi.json")

    assert response.status_code == 200
    document = response.json()
    assert document["openapi"].startswith("3.1")
    assert "bearerAuth" in document["components"]["securitySchemes"]


def test_the_public_schema_describes_no_internal_route(api):
    document = api.get("/v1/openapi.json").json()

    assert set(document["paths"]) == {
        "/v1/models",
        "/v1/models/{model}",
        "/v1/responses",
        "/v1/responses/{id}",
        "/v1/responses/{id}/cancel",
        "/v1/chat/completions",
        "/v1/usage",
        "/v1/openapi.json",
    }
    # Everything CONTRACT §7 deliberately does not expose. Each would need its
    # own product, scope and threat review.
    text = response_text = str(document)
    for internal in ("/chat", "/admin", "/history", "/uploads", "/analytics", "/reports"):
        assert f'"{internal}' not in text and f"'{internal}" not in response_text


def test_the_served_schema_is_the_one_the_ci_gate_checks(api):
    from app.publicapi import openapi as openapi_module

    assert api.get("/v1/openapi.json").json() == openapi_module.public_openapi()


# ======================================================================
# Wave 3 (2026-09-13): the verifier's confirmed defects, each pinned by a
# test that fails on the code it found them in.
# ======================================================================

import threading  # noqa: E402
import time  # noqa: E402

import httpx  # noqa: E402

from app.publicapi import background  # noqa: E402

#: Which scope each route in ALL_ROUTES requires (CONTRACT §7).
ROUTE_SCOPES: Dict[str, Scope] = {
    "/v1/models": Scope.MODELS_READ,
    f"/v1/models/{registry.TECHSARA_35B}": Scope.MODELS_READ,
    "/v1/responses": Scope.RESPONSES_WRITE,
    "/v1/responses/resp_whatever": Scope.RESPONSES_READ,
    "/v1/responses/resp_whatever/cancel": Scope.RESPONSES_WRITE,
    "/v1/chat/completions": Scope.RESPONSES_WRITE,
    "/v1/usage": Scope.USAGE_READ,
}


def _bare_app() -> FastAPI:
    app = FastAPI()
    app.include_router(public_router.router)
    return app


def _bearer(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _GatedEngine:
    """An engine that does not answer until the test opens a gate.

    A THREADING event polled from the loop, so the same gate works for a
    request served on TestClient's portal thread and for one served by an
    `httpx.ASGITransport` in the test's own loop."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.calls = 0

    def __call__(self, messages, **kwargs):
        self.calls += 1
        return self._run()

    async def _run(self):
        # Bounded, so a regression that over-admits FAILS its assertion in
        # seconds instead of hanging the suite on a request nobody releases.
        deadline = time.monotonic() + 10
        while not self.gate.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        yield ("token", "done")


@pytest.fixture()
def gated(monkeypatch):
    fake = _GatedEngine()
    monkeypatch.setattr(llm, "stream_chat_events", fake)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    yield fake
    fake.gate.set()


async def _wait_until(predicate, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.02)


# ----------------------------------------------- the burst (race 1) --


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("post", "/v1/responses", {"model": registry.TECHSARA_35B, "input": "hi"}),
        ("get", "/v1/usage", None),
    ],
)
@pytest.mark.usefixtures("limits_enforced")
def test_twelve_simultaneous_requests_against_rpm_one_admit_exactly_one(
    platform, engine, monkeypatch, method, path, body
):
    """The verifier's probe, re-run through the router: 12 requests in the
    same instant against `rpm=1` were ALL admitted (12/12) because the gate
    read the window and bumped the counter in two steps.

    A barrier inside the gate makes the overlap a fact rather than a hope:
    all twelve are inside `quotas.reserve`, on twelve worker threads against
    the real PostgreSQL test database, before any of them may proceed.
    """
    engine(["ok"])
    projects.update_project(platform["project"]["id"], WORKSPACE, rpm=1)
    barrier = threading.Barrier(12, timeout=20)
    real_reserve = quotas.reserve

    def reserve_after_the_barrier(*args, **kwargs):
        barrier.wait()
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(quotas, "reserve", reserve_after_the_barrier)

    async def scenario():
        transport = httpx.ASGITransport(app=_bare_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            return await asyncio.gather(
                *(client.request(method, path, json=body, headers=_auth()) for _ in range(12))
            )

    responses = asyncio.run(scenario())
    statuses = sorted(r.status_code for r in responses)

    assert statuses.count(429) == 11, statuses
    assert len([s for s in statuses if s != 429]) == 1, statuses
    assert all(
        r.json()["error"]["code"] == "rate_limit_error" for r in responses if r.status_code == 429
    )


# ------------------------------- one counter per project (race 2) --


@pytest.mark.usefixtures("limits_enforced")
def test_twelve_simultaneous_streams_from_two_keys_share_the_projects_two_slots(
    platform, gated
):
    """`max_concurrency` is the PROJECT's (CONTRACT §12). It was counted per
    KEY for sync and stream, so each extra key the project minted added
    another full allowance of the shared NORMAL lanes.

    Twelve streams, six per key, all opened at once and all held open by an
    engine that will not answer: exactly two may be running.
    """
    project_id = platform["project"]["id"]
    projects.update_project(project_id, WORKSPACE, max_concurrency=2, rpm=1000)
    second = projects.create_key(project_id, WORKSPACE, "second", scopes=list(Scope))
    tokens = [TOKENS["live"], second.token] * 6
    caller = _caller()

    async def scenario():
        transport = httpx.ASGITransport(app=_bare_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test", timeout=60
        ) as client:
            tasks = [
                asyncio.ensure_future(
                    client.post("/v1/responses", json=_body(stream=True), headers=_bearer(t))
                )
                for t in tokens
            ]
            # Ten refusals come back at once; two streams stay open.
            await _wait_until(lambda: sum(t.done() for t in tasks) >= 10)
            await _wait_until(lambda: gated.calls >= 2)
            peak = quotas.in_flight(caller)
            await asyncio.sleep(0.2)  # give an over-admitted third every chance
            late_peak = quotas.in_flight(caller)
            gated.gate.set()
            responses = await asyncio.gather(*tasks)
        return peak, late_peak, responses

    peak, late_peak, responses = asyncio.run(scenario())
    statuses = [r.status_code for r in responses]

    assert statuses.count(200) == 2, statuses
    assert statuses.count(429) == 10, statuses
    assert peak == late_peak == 2
    assert gated.calls == 2
    assert quotas.in_flight(caller) == 0


@pytest.mark.usefixtures("limits_enforced")
def test_a_background_job_and_a_stream_draw_on_the_same_project_slots(
    api, platform, gated
):
    """Background jobs were counted in a SEPARATE per-project dict, so a
    project could hold its limit in streams and its limit again in background
    jobs. One counter now, and the job holds its slot for its whole life —
    not only until the 202."""
    project_id = platform["project"]["id"]
    projects.update_project(project_id, WORKSPACE, max_concurrency=1)
    second = projects.create_key(project_id, WORKSPACE, "second", scopes=list(Scope))

    queued = api.post("/v1/responses", json=_body(background=True), headers=_auth())
    assert queued.status_code == 202

    deadline = time.monotonic() + 20
    while gated.calls < 1 and time.monotonic() < deadline:
        time.sleep(0.02)
    stream = api.post("/v1/responses", json=_body(stream=True), headers=_bearer(second.token))
    sync = api.post("/v1/responses", json=_body(), headers=_bearer(second.token))
    another_job = api.post("/v1/responses", json=_body(background=True), headers=_auth())

    assert stream.status_code == 429
    assert stream.json()["error"]["code"] == "concurrency_limit_exceeded"
    assert sync.status_code == 429
    assert another_job.status_code == 429
    assert quotas.in_flight(_caller(), "background") == 1

    gated.gate.set()
    deadline = time.monotonic() + 20
    while quotas.in_flight(_caller()) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert quotas.in_flight(_caller()) == 0
    assert api.post("/v1/responses", json=_body(), headers=_auth()).status_code == 200


def test_the_router_has_exactly_one_background_implementation(api, platform, engine, monkeypatch):
    """A background request goes through `background.start` — with the
    caller, so the slot is the project's — and nothing else writes its row."""
    engine(["ok"])
    seen: List[Any] = []
    real_start = background.start

    async def spy(spec, **kwargs):
        seen.append(kwargs)
        return await real_start(spec, **kwargs)

    monkeypatch.setattr(background, "start", spy)
    response = api.post("/v1/responses", json=_body(background=True), headers=_auth())

    assert response.status_code == 202
    assert len(seen) == 1
    assert seen[0]["caller"].project_id == platform["project"]["id"]
    assert "max_concurrency" not in seen[0]
    rows = db.list_api_responses(platform["project"]["id"])
    assert [r["id"] for r in rows] == [response.json()["id"]]


# ---------------------------------------------- the slot leak (early hang-up) --


async def _post_and_hang_up(app, token: str, body: bytes, spec_version: str) -> List[dict]:
    """A raw ASGI POST from a client that is gone before the body is written.

    `2.3`: the disconnect is already waiting when the response starts, so
    Starlette's listener cancels the body before its first step. `2.4`: the
    socket write of the status line fails (a reset). In both, the SSE body
    generator never starts — which is exactly the case whose `finally` never
    ran and whose slot was never given back.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/responses",
        "raw_path": b"/v1/responses",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"api.test"),
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("198.51.100.20", 50123),
        "server": ("api.test", 80),
    }
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    sent: List[dict] = []

    async def receive():
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if spec_version == "2.4" and message["type"] == "http.response.start":
            raise OSError("connection reset by peer")

    try:
        await app(scope, receive, send)
    except Exception:  # noqa: BLE001 - the client is gone; what matters is the state left
        pass
    return sent


@pytest.mark.parametrize("spec_version", ["2.3", "2.4"])
def test_a_client_that_hangs_up_before_the_stream_starts_never_strands_a_slot(
    platform, engine, spec_version
):
    engine(["never", "sent"])
    project_id = platform["project"]["id"]
    projects.update_project(project_id, WORKSPACE, max_concurrency=1, rpm=1000)
    body = json_bytes(_body(stream=True))
    app = _bare_app()

    async def scenario():
        for _ in range(5):
            await _post_and_hang_up(app, TOKENS["live"], body, spec_version)
        held = quotas.in_flight(_caller())
        # The outcome of a stream cut off mid-flight is recorded by a SHIELDED
        # task that outlives the cancelled response (streaming._settle); in
        # production the loop keeps running, here we wait for it before the
        # loop closes.
        await _wait_until(
            lambda: "queued"
            not in {r["status"] for r in db.list_api_responses(project_id, limit=50)},
            timeout=15,
        )
        return held

    held = asyncio.run(scenario())

    # Five abandoned streams against a limit of one: with the leak, the first
    # one kept the slot and the limit was spent until a restart.
    assert held == 0
    with TestClient(app) as client:
        assert client.post("/v1/responses", json=_body(), headers=_auth()).status_code == 200
    # And none of the abandoned requests is left `queued` for a poller.
    statuses = {r["status"] for r in db.list_api_responses(project_id, limit=50)}
    assert "queued" not in statuses


def json_bytes(payload: Any) -> bytes:
    import json as _json

    return _json.dumps(payload).encode()


@pytest.mark.usefixtures("limits_enforced")
def test_a_stream_refused_by_the_ceiling_writes_no_row_and_frees_its_idempotency_key(
    api, platform, engine
):
    fake = engine(["ok"])
    projects.update_project(platform["project"]["id"], WORKSPACE, max_concurrency=1)
    headers = {**_auth(), "Idempotency-Key": "retry-me-after-the-429"}

    with quotas.concurrency_slot(_caller(), "background"):
        refused = api.post("/v1/responses", json=_body(stream=True), headers=headers)
    retried = api.post("/v1/responses", json=_body(stream=True), headers=headers)

    assert refused.status_code == 429
    assert refused.headers["content-type"].startswith("application/json")
    assert retried.status_code == 200
    assert fake.calls == 1
    assert len(db.list_api_responses(platform["project"]["id"])) == 1


# ------------------------------------------------ every route is metered --


@pytest.mark.parametrize("method, path, body", ALL_ROUTES, ids=[r[1] for r in ALL_ROUTES])
@pytest.mark.usefixtures("limits_enforced")
def test_every_authenticated_route_spends_one_request_of_the_rate_limit(
    api, platform, engine, method, path, body
):
    """Five of the seven routes only READ the window to fill in a header, so
    `GET /v1/usage` — a 93-day range query — could be called at any rate."""
    engine(["ok"])
    projects.update_project(platform["project"]["id"], WORKSPACE, rpm=1)

    first = api.request(method, path, json=body, headers=_auth())
    second = api.request(method, path, json=body, headers=_auth())

    assert first.status_code != 429, first.text
    assert second.status_code == 429, second.text
    assert second.json()["error"]["code"] == "rate_limit_error"
    assert int(second.headers["Retry-After"]) >= 1


@pytest.mark.parametrize(
    "path, bad",
    [
        ("/v1/responses", {"model": registry.TECHSARA_35B, "input": "hi", "top_p": 0.5}),
        ("/v1/responses", {"model": "no-such-model", "input": "hi"}),
        ("/v1/chat/completions", {"model": registry.TECHSARA_35B, "logit_bias": {}}),
    ],
)
@pytest.mark.usefixtures("limits_enforced")
def test_a_request_refused_by_validation_still_counts_against_the_rate_limit(
    api, platform, engine, path, bad
):
    """CONTRACT §4 puts the quota before request validation; a flood of
    malformed bodies — each costing a resolver read and, for a bad model, an
    overrides read — was never counted."""
    engine(["ok"])
    projects.update_project(platform["project"]["id"], WORKSPACE, rpm=1)

    refused = api.post(path, json=bad, headers=_auth())
    after = api.post("/v1/responses", json=_body(), headers=_auth())

    assert refused.status_code in (400, 404)
    assert after.status_code == 429


@pytest.mark.parametrize("method, path, body", ALL_ROUTES, ids=[r[1] for r in ALL_ROUTES])
def test_a_key_without_a_routes_scope_is_refused_on_that_route(
    api, platform, engine, method, path, body
):
    """Scope enforcement was tested on ONE route: deleting the scope check
    from `get_usage`, `get_response` and `cancel_response` left the suite
    green (verifier mutation)."""
    engine(["ok"])
    required = ROUTE_SCOPES[path]
    lacking = projects.create_key(
        platform["project"]["id"],
        WORKSPACE,
        f"without-{required.value}",
        scopes=sorted(s.value for s in Scope if s is not required),
    )

    response = api.request(method, path, json=body, headers=_bearer(lacking.token))

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "insufficient_scope"
    assert required.value in response.headers["WWW-Authenticate"]


# ---------------------------------------------- context_length_exceeded --


def test_an_input_over_the_models_window_is_a_400_before_admission(
    api, platform, engine, monkeypatch
):
    fake = engine(["never"])
    monkeypatch.setattr(settings, "model_max_context", 64)

    long_input = "sky " * 400
    responses_route = api.post("/v1/responses", json=_body(input=long_input), headers=_auth())
    chat_route = api.post(
        "/v1/chat/completions",
        json={
            "model": registry.TECHSARA_35B,
            "messages": [{"role": "user", "content": long_input}],
        },
        headers=_auth(),
    )

    for response in (responses_route, chat_route):
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "context_length_exceeded"
        assert response.json()["error"]["param"] == "input"
    assert fake.calls == 0
    assert db.list_api_responses(platform["project"]["id"]) == []


def test_the_projects_input_ceiling_narrows_the_models_and_zero_means_zero(
    api, platform, engine
):
    engine(["ok"])
    project_id = platform["project"]["id"]

    projects.update_project(project_id, WORKSPACE, max_input_tokens=20)
    long_enough = api.post("/v1/responses", json=_body(input="word " * 60), headers=_auth())
    short = api.post("/v1/responses", json=_body(input="hi"), headers=_auth())

    assert long_enough.status_code == 400
    assert long_enough.json()["error"]["code"] == "context_length_exceeded"
    assert short.status_code == 200

    # Zero means zero, only None inherits. Asserted on the router's own rule
    # with a caller that CARRIES 0: `resolver._ceiling` currently folds a
    # stored 0 into None before the router ever sees it (reported, not this
    # file's to change).
    import dataclasses

    caller = _caller()
    zero = dataclasses.replace(
        caller, limits=dataclasses.replace(caller.limits, max_input_tokens=0)
    )
    inherit = dataclasses.replace(
        caller, limits=dataclasses.replace(caller.limits, max_input_tokens=None)
    )
    refusal = public_router._context_limit_error(zero, registry.TECHSARA_35B, 8)
    assert refusal is not None and refusal.code == "context_length_exceeded"
    assert public_router._context_limit_error(inherit, registry.TECHSARA_35B, 8) is None


# ------------------------------------------------------ finish_reason --


@pytest.fixture()
def truncating_engine(monkeypatch):
    """An engine that reports `finish_reason: length`, the way `llm.py` does:
    by setting the ContextVar inside the generating task."""

    def fake(messages, **kwargs):
        async def run():
            yield ("token", "half an ans")
            llm._set_finish_reason("length")

        return run()

    monkeypatch.setattr(llm, "stream_chat_events", fake)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)


def test_a_truncated_answer_is_reported_as_length_on_both_chat_shapes(api, truncating_engine):
    body = {"model": registry.TECHSARA_35B, "messages": [{"role": "user", "content": "go"}]}

    sync = api.post("/v1/chat/completions", json=body, headers=_auth())
    stream = api.post("/v1/chat/completions", json={**body, "stream": True}, headers=_auth())

    assert sync.json()["choices"][0]["finish_reason"] == "length"
    reasons = [
        choice.get("finish_reason")
        for line in stream.text.splitlines()
        if line.startswith("data: {")
        for choice in __import__("json").loads(line[len("data: "):])["choices"]
    ]
    assert "length" in reasons
    assert "stop" not in reasons


# ------------------------------------------------- chat idempotent replay --


def test_a_chat_completions_replay_answers_in_the_chat_completion_shape(api, engine):
    fake = engine(["Hello."], usage={"prompt_tokens": 2, "completion_tokens": 3})
    headers = {**_auth(), "Idempotency-Key": "chat-retry-1"}
    body = {"model": registry.TECHSARA_35B, "messages": [{"role": "user", "content": "hi"}]}

    first = api.post("/v1/chat/completions", json=body, headers=headers)
    replay = api.post("/v1/chat/completions", json=body, headers=headers)

    assert first.status_code == replay.status_code == 200
    assert replay.json()["object"] == "chat.completion"
    assert replay.json()["id"] == first.json()["id"]
    assert "output" not in replay.json()
    assert replay.json()["usage"] == first.json()["usage"]
    assert fake.calls == 1


def test_a_replay_of_a_streamed_request_is_a_stream(api, engine):
    fake = engine(["Hi"])
    headers = {**_auth(), "Idempotency-Key": "stream-retry-1"}
    body = {
        "model": registry.TECHSARA_35B,
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }

    api.post("/v1/chat/completions", json=body, headers=headers)
    replay = api.post("/v1/chat/completions", json=body, headers=headers)
    responses_first = api.post(
        "/v1/responses", json=_body(stream=True), headers={**_auth(), "Idempotency-Key": "r-1"}
    )
    responses_replay = api.post(
        "/v1/responses", json=_body(stream=True), headers={**_auth(), "Idempotency-Key": "r-1"}
    )

    assert replay.headers["content-type"].startswith("text/event-stream")
    assert replay.text.endswith(events.DONE_SENTINEL)
    assert responses_first.status_code == 200
    assert responses_replay.headers["content-type"].startswith("text/event-stream")
    records = events.parse_frames(responses_replay.text)
    assert records[-1]["event"] == "response.completed"
    assert fake.calls == 2


def test_a_failed_attempt_does_not_poison_its_idempotency_key(api, engine, monkeypatch):
    monkeypatch.setattr("app.publicapi.streaming._engine_state_name", lambda: "RECOVERING")

    class QueuedForRecovery(RuntimeError):
        pass

    engine([], fail=QueuedForRecovery("reloading"))
    headers = {**_auth(), "Idempotency-Key": "after-a-503"}
    failed = api.post("/v1/responses", json=_body(), headers=headers)
    fake = engine(["back"])
    retried = api.post("/v1/responses", json=_body(), headers=headers)

    assert failed.status_code == 503
    assert retried.status_code == 200
    assert fake.calls == 1


# --------------------------------------------------- the envelope, everywhere --


def test_an_unknown_path_under_v1_is_the_contract_404_envelope(api):
    for method in ("get", "post", "put", "delete"):
        response = api.request(method, "/v1/nope/nothing-here", headers={"Origin": "https://x.example"})
        assert response.status_code == 404, (method, response.text)
        error = response.json()["error"]
        assert set(error) == {"message", "type", "code", "param", "request_id"}
        assert error["request_id"] == response.headers["X-Request-Id"]
        assert "detail" not in response.json()
        assert response.headers["Access-Control-Allow-Origin"] == "https://x.example"


def test_a_wrong_method_on_a_real_v1_route_is_a_405_envelope_naming_the_allowed(api):
    response = api.delete("/v1/models")

    assert response.status_code == 405
    assert response.json()["error"]["request_id"] == response.headers["X-Request-Id"]
    assert "GET" in response.headers["Allow"]


def test_the_catch_all_never_shadows_a_route_declared_after_it(platform):
    app = _bare_app()

    async def probe():
        return {"ok": True}

    app.add_api_route("/v1/declared-later", probe, methods=["POST"])
    with TestClient(app) as client:
        assert client.post("/v1/declared-later").json() == {"ok": True}


def test_the_mounted_application_answers_an_unknown_v1_path_in_the_envelope():
    from app import main as app_main

    if not app_main.PUBLIC_API_MOUNTED:
        pytest.skip("the /v1 router is not mounted in this process")
    with TestClient(app_main.app) as client:
        response = client.get("/v1/definitely-not-a-route")

    assert response.status_code == 404
    assert set(response.json()) == {"error"}


# ------------------------------------------------- the ip allowlist --


def test_a_forwarded_for_from_an_untrusted_peer_cannot_satisfy_the_ip_allowlist(
    platform, engine, monkeypatch
):
    """AUDIT F027/F058: production trusts proxy headers and publishes the
    orchestrator on 0.0.0.0:8080, and the router believed the FIRST
    `X-Forwarded-For` entry from any peer. A LAN client with a leaked key
    could name an allowlisted address and walk in."""
    engine(["ok"])
    monkeypatch.setattr(settings, "auth_trust_proxy_headers", True)
    monkeypatch.setattr(settings, "public_api_trusted_proxies", ("10.9.0.0/16",))
    projects.update_project(
        platform["project"]["id"], WORKSPACE, ip_allowlist=["203.0.113.7/32"]
    )
    app = _bare_app()
    forged_headers = {**_auth(), "X-Forwarded-For": "203.0.113.7"}

    with TestClient(app, client=("192.168.1.50", 40000)) as lan:
        forged = lan.get("/v1/models", headers=forged_headers)
    with TestClient(app, client=("10.9.0.3", 40000)) as proxy:
        relayed = proxy.get("/v1/models", headers=forged_headers)
        # Even through our proxy, only the hop the proxy appended counts.
        spoofed_left = proxy.get(
            "/v1/models", headers={**_auth(), "X-Forwarded-For": "203.0.113.7, 192.168.1.50"}
        )

    assert forged.status_code == 401
    assert relayed.status_code == 200
    assert spoofed_left.status_code == 401


def test_polling_a_background_response_a_restart_cut_off_does_not_poll_for_ever(
    api, platform, engine, monkeypatch
):
    from datetime import datetime, timedelta, timezone

    engine(["ok"])
    orphan = db.create_api_response(
        platform["project"]["id"], WORKSPACE, registry.TECHSARA_35B, "req_x", background=True
    )
    monkeypatch.setattr(
        background, "PROCESS_STARTED_AT", datetime.now(timezone.utc) + timedelta(minutes=1)
    )

    polled = api.get(f"/v1/responses/{orphan['id']}", headers=_auth())

    assert polled.status_code == 200
    assert polled.json()["status"] == "failed"
    assert polled.json()["error"]["code"] == "model_unavailable"


def test_a_stored_response_reads_back_with_its_real_creation_time(api, engine):
    """`db` returns timestamps as ISO strings; the renderer asked the string
    for `.timestamp()`, so every `GET /v1/responses/{id}`, background 202 and
    idempotent replay said `created_at: 0` (found 2026-09-13 while fixing the
    restart-orphan path)."""
    engine(["ok"])
    before = int(time.time()) - 5
    created = api.post("/v1/responses", json=_body(background=True), headers=_auth())
    read = api.get(f"/v1/responses/{created.json()['id']}", headers=_auth())

    assert created.json()["created_at"] >= before
    assert read.json()["created_at"] >= before


# ------------------------- wave-4: ceilings a caller cannot game down --
#
# Re-verifier, 2026-09-13: the hard input ceilings were decided on
# `context.estimate_messages` (3 ASCII chars per token). The Qwen pre-tokenizer
# isolates every digit, so `"7" * 2900` estimated at 974 tokens, passed a
# project `max_input_tokens=1000` with a 200 and reached the engine as ~2,900
# real tokens.


def test_a_digit_string_three_times_the_projects_input_ceiling_is_refused_before_the_engine(
    api, platform, engine
):
    fake = engine(["never"])
    projects.update_project(platform["project"]["id"], WORKSPACE, max_input_tokens=1000)

    digits = "7" * 2900
    responses_route = api.post("/v1/responses", json=_body(input=digits), headers=_auth())
    chat_route = api.post(
        "/v1/chat/completions",
        json={"model": registry.TECHSARA_35B, "messages": [{"role": "user", "content": digits}]},
        headers=_auth(),
    )

    for response in (responses_route, chat_route):
        assert response.status_code == 400, response.text
        error = response.json()["error"]
        assert error["code"] == "context_length_exceeded"
        # The refusal must not claim an exact count it never measured.
        assert "may be up to" in error["message"]
    assert fake.calls == 0
    assert db.list_api_responses(platform["project"]["id"]) == []


def test_a_digit_body_that_would_overflow_the_models_window_is_refused_on_its_byte_length(
    api, platform, engine, monkeypatch
):
    """The 1 MiB-against-1M-window probe, scaled down: 8,000 digits estimate
    at ~2,670 tokens, inside a 3,000-token window, and are ~8,000 real."""
    fake = engine(["never"])
    monkeypatch.setattr(settings, "model_max_context", 3000)

    response = api.post("/v1/responses", json=_body(input="7" * 8000), headers=_auth())

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "context_length_exceeded"
    assert fake.calls == 0


def test_the_soft_input_reservation_still_uses_the_estimate_and_not_the_byte_bound(
    api, platform, engine, monkeypatch
):
    """The trade-off is confined to the HARD ceiling: reserving the byte bound
    for input-TPM would spend 3-4x a prose prompt's budget until settlement."""
    engine(["ok"], usage={"prompt_tokens": 40, "completion_tokens": 1})
    seen: List[int] = []
    real_reserve = quotas.reserve

    def spy(*args, **kwargs):
        seen.append(int(kwargs.get("estimated_input_tokens") or 0))
        return real_reserve(*args, **kwargs)

    monkeypatch.setattr(quotas, "reserve", spy)
    prose = "The sky is blue because of Rayleigh scattering. " * 10

    response = api.post("/v1/responses", json=_body(input=prose), headers=_auth())

    assert response.status_code == 200, response.text
    from app import context as context_tools

    from app.publicapi import models as public_models

    messages = public_models.parse_responses_request(_body(input=prose)).chat_messages()
    assert seen == [context_tools.estimate_messages(messages)]
    assert seen[0] < context_tools.upper_bound_messages(messages)


# ------------------ wave-4: a sync generation cancelled after it ran --


class _RanThenHangs:
    """Generates one token, then waits — so a cancellation lands AFTER the
    engine has spent tokens. Bounded, so nothing here can hang the suite."""

    def __init__(self) -> None:
        self.calls = 0
        self.generated = threading.Event()

    def __call__(self, messages, **kwargs):
        self.calls += 1
        return self._run()

    async def _run(self):
        yield ("token", "partial ")
        self.generated.set()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        yield ("token", "never reached")


def test_a_sync_request_cancelled_after_the_engine_ran_is_charged_and_leaves_in_progress(
    platform, monkeypatch
):
    """Re-verifier, 2026-09-13: one `except BaseException` covered both the
    slot/row and `run_to_completion`, so a cancellation mid-generation
    returned the input estimate as "nothing ran", recorded no usage and left
    the row `in_progress` for ever. Real PostgreSQL; hard 60 s bound."""
    fake = _RanThenHangs()
    monkeypatch.setattr(llm, "stream_chat_events", fake)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: {"prompt_tokens": 11, "completion_tokens": 1})
    project_id = platform["project"]["id"]
    projects.update_project(project_id, WORKSPACE, rpm=1000)

    async def scenario():
        transport = httpx.ASGITransport(app=_bare_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test", timeout=60
        ) as client:
            task = asyncio.ensure_future(
                client.post("/v1/responses", json=_body(), headers=_auth())
            )
            await _wait_until(fake.generated.is_set, timeout=15)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await _wait_until(
                lambda: [r["status"] for r in db.list_api_responses(project_id)]
                not in ([], ["in_progress"]),
                timeout=15,
            )

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))

    rows = db.list_api_responses(project_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["input_tokens"] == 11
    assert rows[0]["output_tokens"] == 1
    assert quotas.in_flight(_caller()) == 0


@pytest.mark.usefixtures("limits_enforced")
def test_a_sync_request_refused_by_the_slot_frees_its_idempotency_key_and_writes_no_row(
    api, platform, engine
):
    """The other half of the split: before `run_to_completion` starts,
    nothing ran, and the pre-split behaviour must survive it."""
    fake = engine(["ok"])
    projects.update_project(platform["project"]["id"], WORKSPACE, max_concurrency=1)
    headers = {**_auth(), "Idempotency-Key": "sync-retry-after-the-429"}

    with quotas.concurrency_slot(_caller(), "background"):
        refused = api.post("/v1/responses", json=_body(), headers=headers)
    retried = api.post("/v1/responses", json=_body(), headers=headers)

    assert refused.status_code == 429, refused.text
    assert retried.status_code == 200, retried.text
    assert fake.calls == 1
    assert len(db.list_api_responses(platform["project"]["id"])) == 1


# -------------------------- wave-4: a database outage is a 503, not a 500 --


def _dead_database(*args, **kwargs):
    """A REAL connection failure: psycopg against a port nothing listens on,
    whose error text names an address — which must never reach the wire."""
    import psycopg

    psycopg.connect("postgresql://nobody@127.0.0.1:1/nothing", connect_timeout=2)
    raise AssertionError("port 1 accepted a connection")


@pytest.mark.parametrize("method, path, body", ALL_ROUTES, ids=[r[1] for r in ALL_ROUTES])
def test_a_quota_ledger_outage_refuses_every_route_as_a_retryable_503(
    api, platform, engine, monkeypatch, method, path, body
):
    fake = engine(["never"])
    monkeypatch.setattr(quotas, "reserve", _dead_database)

    response = api.request(method, path, json=body, headers=_auth())

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "model_unavailable"
    assert error["request_id"]
    assert int(response.headers["Retry-After"]) >= 1
    for forbidden in ("127.0.0.1", "psycopg", "Traceback", "nothing"):
        assert forbidden not in response.text
    assert fake.calls == 0


def test_a_key_that_cannot_be_looked_up_during_an_outage_is_a_503_not_a_401_or_500(
    api, platform, engine, monkeypatch
):
    monkeypatch.setattr(db, "api_key_by_public_id", _dead_database)

    response = api.get("/v1/models", headers=_auth())

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "model_unavailable"
    assert int(response.headers["Retry-After"]) >= 1
    assert "127.0.0.1" not in response.text


def test_a_bug_in_the_quota_gate_is_still_a_500_not_a_retry_loop(
    api, platform, engine, monkeypatch
):
    def broken(*args, **kwargs):
        raise TypeError("a programming error, not an outage")

    monkeypatch.setattr(quotas, "reserve", broken)

    response = api.get("/v1/models", headers=_auth())

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "programming error" not in response.text


def test_the_output_reservation_is_sized_from_the_request_not_the_platform_default(
    api, engine, monkeypatch
):
    """2026-09-13: both generating routes admitted every request with the
    platform default of 8,192 output tokens reserved, whatever the caller asked
    for, so a burst of short requests ran out of output_tpm after about seven
    and was refused until the first ones settled. The reservation must carry
    the request's own `max_output_tokens`, on /v1/responses and on the
    compatibility route alike; a request that names no budget still reserves
    the default (None)."""
    seen = []
    real_reserve = quotas.reserve

    def spy(caller, **kwargs):
        seen.append(kwargs.get("max_output_tokens"))
        return real_reserve(caller, **kwargs)

    monkeypatch.setattr(quotas, "reserve", spy)
    engine(["ok"], usage={"prompt_tokens": 3, "completion_tokens": 1})

    assert api.post("/v1/responses", json=_body(max_output_tokens=16), headers=_auth()).status_code == 200
    assert api.post("/v1/responses", json=_body(), headers=_auth()).status_code == 200
    chat = {"model": registry.TECHSARA_35B, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 24}
    assert api.post("/v1/chat/completions", json=chat, headers=_auth()).status_code == 200

    assert seen == [16, None, 24], seen


def test_the_bare_api_root_is_the_contract_404_and_never_a_redirect(api, engine):
    """2026-09-13, the end-to-end run: `GET /v1` answered `307 Location: /v1/`
    (Starlette's trailing-slash redirect, because the preflight catch-all
    matches `/v1/`), and the public edge — which refuses to follow an upstream
    redirect — turned that into a 500. The root of the API is a 404 in the
    documented envelope, for every method and with or without a key."""
    for method in ("get", "post", "delete"):
        response = getattr(api, method)("/v1", headers=_auth(), follow_redirects=False)
        assert response.status_code == 404, (method, response.status_code, response.headers.get("location"))
        assert "location" not in {k.lower() for k in response.headers}
        body = response.json()
        assert body["error"]["type"] == "invalid_request_error"
        assert body["error"]["request_id"]
    anonymous = api.get("/v1", follow_redirects=False)
    assert anonymous.status_code == 404 and "location" not in {k.lower() for k in anonymous.headers}


import json  # noqa: E402


# ======================================================================
# Unlimited by default (owner decision, 2026-09-13): PUBLIC_API_ENFORCE_LIMITS
# is false unless an operator sets it. These tests run on the shipped default
# and never ask for `limits_enforced`.
# ======================================================================


def _project_day(project_id: str) -> Dict[str, int]:
    with db.connection() as con:
        row = con.execute(
            "SELECT COALESCE(SUM(requests), 0) AS requests, "
            "       COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "       COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            "       COALESCE(SUM(rate_limited), 0) AS rate_limited "
            "  FROM api_usage_daily WHERE project_id = %s",
            (project_id,),
        ).fetchone()
    return {k: int(v) for k, v in row.items()}


def test_the_shipped_default_enforces_no_limits():
    """The suite's own settings are the shipped ones: nothing in the test
    environment turns the limits on, so every test without `limits_enforced`
    exercises the unlimited default."""
    assert settings.public_api_enforce_limits is False


def test_with_the_limits_off_seventy_requests_against_rpm_one_all_succeed_without_a_ratelimit_header(
    api, platform
):
    """The smoke check, in-process: 70 quick requests — above the old default
    of 60 a minute, against a project whose stored rpm is 1 — are all 200,
    none carries RateLimit or RateLimit-Policy, and all 70 are counted once."""
    project_id = platform["project"]["id"]
    projects.update_project(project_id, WORKSPACE, rpm=1)

    responses = [api.get("/v1/models", headers=_auth()) for _ in range(70)]

    assert [r.status_code for r in responses] == [200] * 70
    for response in responses:
        assert "RateLimit" not in response.headers
        assert "RateLimit-Policy" not in response.headers
        assert response.headers["X-Request-Id"]
    day = _project_day(project_id)
    assert day["requests"] == 70
    assert day["rate_limited"] == 0


def test_with_the_limits_off_a_spent_daily_quota_refuses_nothing_and_usage_is_still_recorded(
    api, platform, engine
):
    """A daily quota of 10 tokens already overspent, token rates of 1 a
    minute: generations still run, and each one's measured usage is written
    to the ledger exactly once."""
    engine(["ok"], usage={"prompt_tokens": 37, "completion_tokens": 112})
    project_id = platform["project"]["id"]
    projects.update_project(
        project_id, WORKSPACE, daily_token_quota=10, input_tpm=1, output_tpm=1
    )
    db.bump_usage_daily(project_id, input_tokens=8, output_tokens=8)

    statuses = [
        api.post("/v1/responses", json=_body(), headers=_auth()).status_code
        for _ in range(5)
    ]

    assert statuses == [200] * 5
    day = _project_day(project_id)
    assert day["requests"] == 5
    assert day["input_tokens"] == 8 + 5 * 37
    assert day["output_tokens"] == 8 + 5 * 112
    assert day["rate_limited"] == 0
    # And the per-person analytics half: one usage_events row per request.
    with db.connection() as con:
        events = con.execute(
            "SELECT count(*) AS n, COALESCE(SUM(input_tokens), 0) AS input_tokens "
            "  FROM usage_events WHERE workspace_id = %s AND mode = 'api'",
            (WORKSPACE,),
        ).fetchone()
    assert int(events["n"]) == 5
    assert int(events["input_tokens"]) == 5 * 37


def test_with_the_limits_off_simultaneous_streams_beyond_max_concurrency_all_run(
    platform, gated
):
    """Eight streams opened at once against max_concurrency=1, all held open
    by an engine that will not answer yet: every one reaches the engine,
    every one ends 200, and every slot comes back."""
    project_id = platform["project"]["id"]
    projects.update_project(project_id, WORKSPACE, max_concurrency=1, rpm=1)
    caller = _caller()

    async def scenario():
        transport = httpx.ASGITransport(app=_bare_app())
        async with httpx.AsyncClient(
            transport=transport, base_url="http://api.test", timeout=60
        ) as client:
            tasks = [
                asyncio.ensure_future(
                    client.post("/v1/responses", json=_body(stream=True), headers=_auth())
                )
                for _ in range(8)
            ]
            await _wait_until(lambda: gated.calls >= 8)
            peak = quotas.in_flight(caller)
            gated.gate.set()
            return peak, await asyncio.gather(*tasks)

    peak, responses = asyncio.run(scenario())

    assert [r.status_code for r in responses] == [200] * 8
    assert all("RateLimit" not in r.headers for r in responses)
    assert peak == 8
    assert gated.calls == 8
    assert quotas.in_flight(caller) == 0
    assert _project_day(project_id)["requests"] == 8


def test_with_the_limits_off_a_failure_carries_no_ratelimit_header_either(api, platform):
    """The refusal path builds its headers separately from the admission path
    (`_refusal_headers`); it must not advertise a limit either."""
    invalid = api.post("/v1/responses", json=_body(nonsense=True), headers=_auth())

    assert invalid.status_code == 400
    assert "RateLimit" not in invalid.headers
    assert "RateLimit-Policy" not in invalid.headers


def test_with_the_limits_off_a_recovering_engine_still_answers_503_with_retry_after(
    api, engine, monkeypatch
):
    """Retry-After on 503 model_recovering is the engine's, not the quota
    engine's, and the owner decision keeps it."""
    class QueuedForRecovery(RuntimeError):
        pass

    monkeypatch.setattr("app.publicapi.streaming._engine_state_name", lambda: "RECOVERING")
    engine([], fail=QueuedForRecovery("the model is restarting"))

    response = api.post("/v1/responses", json=_body(), headers=_auth())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_recovering"
    assert int(response.headers["Retry-After"]) >= 1
    assert "RateLimit" not in response.headers


def test_with_the_limits_off_the_body_cap_still_refuses_with_413(api, platform, monkeypatch):
    """A technical safety limit, kept: unlimited usage is not unlimited bytes."""
    monkeypatch.setattr(settings, "public_api_max_body_bytes", 4096, raising=False)

    response = api.post("/v1/responses", json=_body(input="x" * 8192), headers=_auth())

    assert response.status_code == 413
    assert "RateLimit" not in response.headers


def test_with_the_limits_off_a_full_engine_admission_lane_is_a_retryable_503_not_a_limit(
    api, platform, engine
):
    """The engine's shared admission queue is the one physical ceiling left:
    a refusal there is `503 model_unavailable` with Retry-After — the engine
    at capacity, never a 429 naming a concurrency limit the API does not
    enforce (owner decision 2026-09-13) — and it advertises no RateLimit."""
    class AdmissionRejected(RuntimeError):
        pass

    engine([], fail=AdmissionRejected("every lane is busy"))

    response = api.post("/v1/responses", json=_body(), headers=_auth())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_unavailable"
    assert "safe to retry" in response.json()["error"]["message"]
    assert int(response.headers["Retry-After"]) >= 1
    assert "RateLimit" not in response.headers


def test_the_served_schema_follows_the_switch(api, monkeypatch):
    """What `/v1/openapi.json` advertises is decided per request from the same
    switch the gate reads: no RateLimit header and no `quota_exceeded` when
    off; both back when on."""
    off = api.get("/v1/openapi.json").json()
    monkeypatch.setattr(settings, "public_api_enforce_limits", True)
    on = api.get("/v1/openapi.json").json()

    assert "RateLimit" not in json.dumps(off)
    assert "quota_exceeded" not in json.dumps(off["paths"])
    assert "RateLimit" in json.dumps(on)
    assert "quota_exceeded" in json.dumps(on["paths"])
