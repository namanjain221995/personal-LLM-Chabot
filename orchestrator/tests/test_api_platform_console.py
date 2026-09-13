"""The developer console API — `/admin/api/developers` (CONTRACT-3 §6, §17).

WHAT THESE TESTS ARE FOR. This router is the browser's door to credentials
that can spend the only GPUs in the building, so the properties pinned here are
the ones whose failure is a security incident rather than a bug report:

  * a member — someone with a perfectly valid session and no developer
    capability — gets 404 from EVERY route, so the console's existence is not
    disclosed (the assertion below enumerates the router, so a route added
    without a gate fails this file rather than shipping);
  * an administrator runs projects and keys but cannot decide which models the
    platform serves publicly, nor lift a project's ceiling;
  * workspace A's key is invisible, unlistable and unrevocable from workspace B;
  * the plaintext key exists in exactly one response body, once, for ever;
  * no response anywhere carries `key_hash` or a webhook signing secret — the
    assertion is on the SERIALISED JSON, not on a field name, because a leak
    that arrives nested inside another object is still a leak;
  * every mutation leaves an audit row naming the actor and the resource;
  * the playground needs no key, and when it streams it emits the PUBLIC event
    grammar — numbered from 1 by 1, exactly one terminal, usage only on it —
    rather than a console-only shape a developer's own client would never see.

Real PostgreSQL, real sessions, real router — the only fake is the model
itself, because a test that needs a GPU is a test nobody runs.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app import db
from app.apiplatform import console_api, keys as key_tools
from app.authn import store
from app.config import settings
from app.apiplatform import quotas
from app.publicapi import events as api_events
from app.publicapi import registry, streaming

WORKSPACE_B = "ws-console-beta"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def console_router_mounted():
    """Mount the console router on the real application.

    `app/main.py` is a single-owner file in this programme and the wiring
    engineer owns the `include_router` call; until it lands, the suite mounts
    the router itself so these tests run against the REAL app — the real
    session machinery, the real cross-site middleware, the real dependency
    resolution — rather than a toy FastAPI instance where a capability gate
    would prove nothing. Mounting is idempotent: once main.py carries the
    call, this fixture finds the path already present and does nothing.
    """
    from app.main import app

    mounted = {route.path for route in app.routes if hasattr(route, "path")}
    if "/admin/api/developers/overview" not in mounted:
        app.include_router(console_api.router)
    yield


@pytest.fixture(autouse=True)
def pepper_store():
    """The pepper wiring `app/main.py` performs at start-up, performed here.

    `key_digest` refuses to invent a pepper without somewhere to persist it —
    overwriting one would invalidate every stored `key_hash` at once — so a
    suite that mints keys has to bind the same insert-if-absent store the
    deployment does. The cache is dropped on both sides of the test because it
    is process-wide and the database is truncated between tests.
    """
    key_tools.reset_pepper_cache()
    key_tools.configure_pepper_store(
        lambda: db.get_platform_secret(key_tools.PEPPER_SECRET_NAME),
        lambda value: db.set_platform_secret(key_tools.PEPPER_SECRET_NAME, value),
    )
    yield
    key_tools.reset_pepper_cache()


@pytest.fixture()
def admin(login_client):
    """An ADMIN in the default workspace — the console's ordinary operator."""
    return login_client("devadmin", role="admin")


@pytest.fixture()
def root(login_client):
    """A SUPER_ADMIN — the only role that may toggle a model or a ceiling."""
    return login_client("devroot", role="super_admin")


@pytest.fixture()
def member(login_client):
    """A signed-in member with no developer capability at all."""
    return login_client("devmember")


@pytest.fixture()
def other_workspace(login_client, admin):
    """An admin of a SECOND workspace.

    The order matters: `admin` logs in first, which creates the default
    workspace, so the workspace inserted here is the newer one and
    `store.membership` (oldest workspace first) keeps resolving each user to
    the single membership they hold.
    """
    client = login_client("betaadmin", role="admin")
    user_id = int(db.get_user_by_username("betaadmin")["id"])
    default = store.default_workspace()
    with db.connection() as con:
        con.execute(
            "INSERT INTO workspaces (id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (WORKSPACE_B, "Beta"),
        )
    store.remove_membership(default["id"], user_id)
    store.upsert_membership(WORKSPACE_B, user_id, "admin")
    return client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_project(client, name: str = "Billing integration") -> Dict[str, Any]:
    response = client.post("/admin/api/developers/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["project"]


def _make_key(client, project_id: str, name: str = "server") -> Dict[str, Any]:
    response = client.post(
        f"/admin/api/developers/projects/{project_id}/keys", json={"name": name}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _audit_rows(action: str) -> List[Dict[str, Any]]:
    with db.connection() as con:
        return [
            dict(row)
            for row in con.execute(
                "SELECT actor_user_id, action, resource_type, resource_id, meta "
                "FROM audit_events WHERE action = %s ORDER BY id",
                (action,),
            ).fetchall()
        ]


def _sample_paths() -> List[Tuple[str, str]]:
    """(method, path) for every route the router declares, ids filled in.

    Built from `console_api.router` rather than from a hand-written list, so a
    route added to the module without a capability gate is caught here instead
    of in production.
    """
    fillers = {
        "{project_id}": "proj_000000000000000000000000",
        "{key_id}": "key_000000000000000000000000",
        "{endpoint_id}": "whe_000000000000000000000000",
        "{model_id}": registry.PUBLIC_MODEL_IDS[0],
    }
    out: List[Tuple[str, str]] = []
    for route in console_api.router.routes:
        path = route.path
        for token, value in fillers.items():
            path = path.replace(token, value)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            out.append((method, path))
    return out


class FakeModel:
    """A scripted stand-in for `llm.stream_chat_events` + `llm.get_usage`.

    The same shape `tests/test_continuation.py` installs: the contract with
    `llm.py` is that the stream yields (kind, delta) pairs and that usage is
    read afterwards, and this implements exactly that pair — so a change to
    either side breaks these tests rather than production.
    """

    def __init__(self, chunks: Optional[List[Tuple[str, str]]] = None, usage: Optional[dict] = None):
        self.chunks = chunks or [("reasoning", "hmm"), ("token", "Retrieval"), ("token", "-augmented.")]
        self._usage = usage if usage is not None else {"prompt_tokens": 11, "completion_tokens": 5, "calls": 1}
        self.calls: List[Dict[str, Any]] = []

    def stream(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})

        async def _gen():
            for kind, delta in self.chunks:
                yield kind, delta

        return _gen()

    def usage(self):
        return self._usage


@pytest.fixture()
def fake_model(monkeypatch):
    def _install(**kwargs) -> FakeModel:
        from app import llm

        fake = FakeModel(**kwargs)
        monkeypatch.setattr(llm, "stream_chat_events", fake.stream)
        monkeypatch.setattr(llm, "get_usage", fake.usage)
        return fake

    return _install


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_every_console_route_refuses_a_signed_in_member_with_404(member):
    """Not 403, and not 401 — 404, from every route, without exception.

    A 403 would confirm that a developer console exists at this path and that
    this person is simply not allowed in; 404 says nothing. The list comes
    from the router itself, so this is a statement about the surface rather
    than about the handlers somebody remembered to check.
    """
    checked = 0
    for method, path in _sample_paths():
        response = member.request(method, path, json={})
        assert response.status_code == 404, f"{method} {path} answered {response.status_code}"
        assert "developer" not in response.text.lower()
        checked += 1
    assert checked == 24, f"the router declares {checked} operations; update this pin"


def test_an_anonymous_caller_is_refused_before_any_capability_is_considered(
    anonymous_mode, console_router_mounted
):
    """No session at all is a 401: there is nobody to check a capability for."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    assert client.get("/admin/api/developers/overview").status_code == 401
    assert client.post("/admin/api/developers/projects", json={"name": "x"}).status_code == 401


@pytest.mark.parametrize("enforced", [True, False], ids=["enforced", "unlimited"])
def test_an_admin_may_run_projects_and_keys_but_not_models_or_limits(
    admin, root, monkeypatch, enforced
):
    """The capability split of CONTRACT-3 §6, asserted from both sides.

    An admin runs the workspace's projects; deciding which models this
    platform serves to the internet, and lifting the ceilings that protect the
    chat application's admission lanes, stay with the super admin.

    In BOTH modes of PUBLIC_API_ENFORCE_LIMITS (owner decision 2026-09-13):
    the capability split does not change, but what the ceilings READ as does —
    the stored number when enforced, null beside `enforced: false` when not.
    """
    monkeypatch.setattr(settings, "public_api_enforce_limits", enforced)
    project = _make_project(admin)
    assert _make_key(admin, project["id"])["key"]["status"] == "active"

    # Refused — and refused as "there is no such thing", not "you may not".
    assert (
        admin.put(
            f"/admin/api/developers/models/{registry.PUBLIC_MODEL_IDS[0]}",
            json={"enabled": False},
        ).status_code
        == 404
    )
    assert (
        admin.put(
            f"/admin/api/developers/projects/{project['id']}/limits",
            json={"rpm": 100000},
        ).status_code
        == 404
    )
    # The admin may READ the ceilings — they explain a 429 — and is told
    # plainly that they cannot move them.
    limits = admin.get(f"/admin/api/developers/projects/{project['id']}/limits")
    assert limits.status_code == 200
    assert limits.json()["can_manage"] is False
    assert limits.json()["limits"]["rpm"] == (60 if enforced else None)
    assert limits.json()["limits"]["enforced"] is enforced
    assert limits.json()["limits_enforced"] is enforced

    # The super admin can do both.
    assert (
        root.put(
            f"/admin/api/developers/projects/{project['id']}/limits", json={"rpm": 120}
        ).status_code
        == 200
    )
    assert root.get(f"/admin/api/developers/projects/{project['id']}/limits").json()[
        "limits"
    ]["rpm"] == (120 if enforced else None)
    # Stored either way, for the day enforcement is switched on.
    assert db.get_api_project(project["id"], store.default_workspace()["id"])["rpm"] == 120
    toggled = root.put(
        f"/admin/api/developers/models/{registry.PUBLIC_MODEL_IDS[0]}",
        json={"enabled": False},
    )
    assert toggled.status_code == 200
    assert toggled.json()["model"]["enabled"] is False


def test_a_project_body_cannot_carry_a_workspace_or_a_limit(admin):
    """`extra="forbid"` is a tenancy guard as well as a validation rule.

    A body naming a workspace is refused outright rather than quietly ignored,
    and a body naming `rpm` cannot raise a ceiling through the project route
    that is not gated on `api.limits.manage`.
    """
    for body in (
        {"name": "Sneaky", "workspace_id": WORKSPACE_B},
        {"name": "Sneaky", "rpm": 100000},
    ):
        response = admin.post("/admin/api/developers/projects", json=body)
        assert response.status_code == 422, response.text
    assert admin.get("/admin/api/developers/projects").json()["projects"] == []


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


def test_a_key_created_in_one_workspace_is_invisible_in_another(admin, other_workspace):
    """The tenancy predicate is in the WHERE clause, not in an `if` afterwards.

    Workspace B's administrator is a legitimate administrator — of workspace B.
    Against workspace A's ids every route answers 404, and a revoke aimed at
    A's key changes nothing.
    """
    project = _make_project(admin, "Alpha billing")
    created = _make_key(admin, project["id"])
    key_id = created["key"]["id"]

    # Nothing of A's appears in B's listings.
    assert other_workspace.get("/admin/api/developers/projects").json()["projects"] == []
    # And nothing of A's is reachable by id.
    for method, path in (
        ("GET", f"/admin/api/developers/projects/{project['id']}"),
        ("GET", f"/admin/api/developers/projects/{project['id']}/keys"),
        ("GET", f"/admin/api/developers/projects/{project['id']}/logs"),
        ("GET", f"/admin/api/developers/projects/{project['id']}/limits"),
        ("POST", f"/admin/api/developers/projects/{project['id']}/keys"),
        ("POST", f"/admin/api/developers/projects/{project['id']}/keys/{key_id}/revoke"),
        ("PATCH", f"/admin/api/developers/projects/{project['id']}"),
    ):
        response = other_workspace.request(method, path, json={"name": "Taken over"})
        assert response.status_code == 404, f"{method} {path} answered {response.status_code}"

    # The key is untouched, and A can still see it.
    with db.connection() as con:
        row = con.execute("SELECT status FROM api_keys WHERE id = %s", (key_id,)).fetchone()
    assert row["status"] == "active"
    assert [k["id"] for k in admin.get(
        f"/admin/api/developers/projects/{project['id']}/keys"
    ).json()["keys"]] == [key_id]


def test_usage_and_overview_count_only_the_callers_own_workspace(admin, other_workspace):
    """Two tenants, two ledgers. A total that included a neighbour's traffic
    would be a billing disclosure as well as a wrong number."""
    mine = _make_project(admin, "Mine")
    theirs = _make_project(other_workspace, "Theirs")
    db.bump_usage_daily(mine["id"], requests=3, input_tokens=100, output_tokens=40)
    db.bump_usage_daily(theirs["id"], requests=9, input_tokens=900, output_tokens=900)

    totals = admin.get("/admin/api/developers/usage").json()
    assert totals["totals"]["requests"] == 3
    assert totals["totals"]["input_tokens"] == 100
    assert totals["totals"]["total_tokens"] == 140
    assert [p["id"] for p in totals["projects"]] == [mine["id"]]

    stats = admin.get("/admin/api/developers/overview").json()["stats"]
    assert stats["projects"] == 1
    assert stats["today"]["requests"] == 3


# ---------------------------------------------------------------------------
# Keys and secrets
# ---------------------------------------------------------------------------


def test_the_plaintext_key_is_returned_exactly_once_and_never_listed_again(admin):
    """The show-once flow of CONTRACT-3 §5.

    What is stored is `HMAC-SHA256(pepper, secret)`, so there is no path back
    to the plaintext for anyone — this asserts that the surface behaves the
    way the storage already forces it to, and that nothing helpfully echoes
    the token into a later listing.
    """
    project = _make_project(admin)
    created = _make_key(admin, project["id"])
    token = created["secret"]
    assert token.startswith("tsk_live_")
    parsed = key_tools.split_key(token)
    assert parsed is not None and parsed.public_id == created["key"]["public_id"]

    # Every later read of the same key, and the project detail that embeds it.
    for path in (
        f"/admin/api/developers/projects/{project['id']}/keys",
        f"/admin/api/developers/projects/{project['id']}",
        "/admin/api/developers/projects",
        "/admin/api/developers/overview",
    ):
        body = admin.get(path).text
        assert token not in body
        assert parsed.secret not in body

    listed = admin.get(
        f"/admin/api/developers/projects/{project['id']}/keys"
    ).json()["keys"]
    assert [k["id"] for k in listed] == [created["key"]["id"]]
    assert listed[0]["last_four"] == created["key"]["last_four"]
    assert "secret" not in listed[0]


def test_no_console_response_carries_a_key_hash_or_a_webhook_secret(admin):
    """Asserted on the SERIALISED JSON of every readable response.

    A field-name check would miss a digest that arrives nested inside another
    object, or under a different name; the values themselves are read out of
    the database and searched for in the bytes the console actually sends.
    """
    project = _make_project(admin)
    created = _make_key(admin, project["id"])
    webhook = admin.post(
        f"/admin/api/developers/projects/{project['id']}/webhooks",
        json={"url": "https://hooks.example.com/techsara", "events": ["response.completed"]},
    )
    assert webhook.status_code == 200, webhook.text

    with db.connection() as con:
        key_hash = con.execute(
            "SELECT key_hash FROM api_keys WHERE id = %s", (created["key"]["id"],)
        ).fetchone()["key_hash"]
        secret = con.execute(
            "SELECT secret FROM api_webhook_endpoints WHERE project_id = %s",
            (project["id"],),
        ).fetchone()["secret"]
    assert key_hash and secret  # the server really does hold both
    # The signing secret is shown ONCE, in the create response, exactly like a
    # key's plaintext — and only as the top-level `secret`, never inside the
    # endpoint object that listings reuse.
    assert webhook.json()["secret"] == secret
    assert secret not in json.dumps(webhook.json()["webhook"])
    endpoint_id = webhook.json()["webhook"]["id"]

    bodies = [json.dumps(created["key"])]
    for path in (
        "/admin/api/developers/overview",
        "/admin/api/developers/settings",
        "/admin/api/developers/projects",
        f"/admin/api/developers/projects/{project['id']}",
        f"/admin/api/developers/projects/{project['id']}/keys",
        f"/admin/api/developers/projects/{project['id']}/webhooks",
        f"/admin/api/developers/projects/{project['id']}/webhooks/{endpoint_id}/deliveries",
        f"/admin/api/developers/projects/{project['id']}/logs",
        "/admin/api/developers/models",
        "/admin/api/developers/usage",
    ):
        response = admin.get(path)
        assert response.status_code == 200, f"{path}: {response.text}"
        bodies.append(response.text)

    for body in bodies:
        assert key_hash not in body
        assert secret not in body
        assert "key_hash" not in body
        assert "whsec_" not in body


def test_rotating_a_key_with_no_overlap_mints_a_replacement_and_revokes_the_original(admin):
    """`overlap_hours: 0` is the compromise case: create-then-revoke, in that
    order.

    The replacement is durable before the original stops working, because the
    other order leaves a window in which the project has no working key at
    all. The new token is shown once, exactly like a first mint.
    """
    project = _make_project(admin)
    created = _make_key(admin, project["id"], name="server")
    original = created["key"]

    rotated = admin.post(
        f"/admin/api/developers/projects/{project['id']}/keys/{original['id']}/rotate",
        json={"overlap_hours": 0},
    )
    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body["secret"] != created["secret"]
    assert body["key"]["id"] != original["id"]
    assert body["key"]["rotated_from"] == original["public_id"]
    assert body["key"]["name"] == "server"
    assert body["revoked"]["id"] == original["id"]
    assert body["revoked"]["status"] == "revoked"

    # A second rotation of the now-revoked key is a 409, not a second mint.
    assert (
        admin.post(
            f"/admin/api/developers/projects/{project['id']}/keys/{original['id']}/rotate",
            json={},
        ).status_code
        == 409
    )
    statuses = {
        k["id"]: k["status"]
        for k in admin.get(
            f"/admin/api/developers/projects/{project['id']}/keys"
        ).json()["keys"]
    }
    assert statuses == {original["id"]: "revoked", body["key"]["id"]: "active"}


def test_revoking_a_key_is_idempotent_and_keeps_the_first_actor(admin):
    project = _make_project(admin)
    key_id = _make_key(admin, project["id"])["key"]["id"]
    first = admin.post(
        f"/admin/api/developers/projects/{project['id']}/keys/{key_id}/revoke"
    )
    assert first.status_code == 200
    assert first.json()["key"]["status"] == "revoked"
    again = admin.post(
        f"/admin/api/developers/projects/{project['id']}/keys/{key_id}/revoke"
    )
    assert again.status_code == 200
    assert again.json()["key"]["revoked_at"] == first.json()["key"]["revoked_at"]


def test_a_key_cannot_be_revoked_or_rotated_through_a_sibling_projects_path(admin):
    """A 404 must not be a mutation in disguise.

    `db.revoke_api_key` is scoped by WORKSPACE, not by project, so a handler
    that revoked first and checked the project afterwards would destroy a
    working credential and then tell the caller nothing was there. Both key
    routes resolve the key inside the project before they write anything.
    """
    first = _make_project(admin, "First")
    second = _make_project(admin, "Second")
    victim = _make_key(admin, second["id"], name="live-traffic")["key"]

    for path in (
        f"/admin/api/developers/projects/{first['id']}/keys/{victim['id']}/revoke",
        f"/admin/api/developers/projects/{first['id']}/keys/{victim['id']}/rotate",
    ):
        assert admin.post(path, json={}).status_code == 404, path

    with db.connection() as con:
        rows = con.execute("SELECT id, status FROM api_keys").fetchall()
    assert [(r["id"], r["status"]) for r in rows] == [(victim["id"], "active")]


def test_the_audit_row_is_written_for_create_rotate_and_revoke(admin):
    """Every mutation names the actor, the resource type and the resource id.

    The audit log is how a leaked key is traced back to the person who minted
    it, so it records `public_id` and `last_four` — both safe to log by
    CONTRACT-3 §5 — and nothing else about the credential.
    """
    actor = int(db.get_user_by_username("devadmin")["id"])
    project = _make_project(admin)
    created = _make_key(admin, project["id"])
    rotated = admin.post(
        f"/admin/api/developers/projects/{project['id']}/keys/{created['key']['id']}/rotate",
        json={},
    ).json()
    assert (
        admin.post(
            f"/admin/api/developers/projects/{project['id']}/keys/{rotated['key']['id']}/revoke"
        ).status_code
        == 200
    )

    project_events = _audit_rows("api_project_created")
    assert len(project_events) == 1
    assert project_events[0]["resource_type"] == "api_project"
    assert project_events[0]["resource_id"] == project["id"]

    for action, resource_id in (
        ("api_key_created", created["key"]["id"]),
        ("api_key_rotated", rotated["key"]["id"]),
        ("api_key_revoked", rotated["key"]["id"]),
    ):
        events = _audit_rows(action)
        assert len(events) == 1, action
        event = events[0]
        assert event["actor_user_id"] == actor
        assert event["resource_type"] == "api_key"
        assert event["resource_id"] == resource_id
        # Nothing secret travels into the audit log either.
        rendered = json.dumps(event["meta"])
        assert created["secret"] not in rendered
        assert rotated["secret"] not in rendered

    assert _audit_rows("api_key_created")[0]["meta"]["last_four"] == created["key"]["last_four"]


def test_a_key_cannot_be_minted_for_a_disabled_project(admin):
    project = _make_project(admin)
    assert (
        admin.patch(
            f"/admin/api/developers/projects/{project['id']}", json={"status": "disabled"}
        ).status_code
        == 200
    )
    response = admin.post(
        f"/admin/api/developers/projects/{project['id']}/keys", json={"name": "late"}
    )
    assert response.status_code == 409
    assert db.list_api_keys(project["id"], store.default_workspace()["id"]) == []


def test_an_unknown_scope_is_a_422_and_mints_nothing(admin):
    """The caller's REQUEST is wrong, not their credential — so 422, never 403."""
    project = _make_project(admin)
    response = admin.post(
        f"/admin/api/developers/projects/{project['id']}/keys",
        json={"name": "typo", "scopes": ["responses.writes"]},
    )
    assert response.status_code == 422
    assert db.list_api_keys(project["id"], store.default_workspace()["id"]) == []


# ---------------------------------------------------------------------------
# Projects, models, logs
# ---------------------------------------------------------------------------


def test_a_project_name_is_unique_within_a_workspace_and_free_across_them(
    admin, other_workspace
):
    _make_project(admin, "Shared name")
    duplicate = admin.post("/admin/api/developers/projects", json={"name": "shared NAME"})
    assert duplicate.status_code == 409
    # The other tenant is unaffected: uniqueness is per workspace.
    assert other_workspace.post(
        "/admin/api/developers/projects", json={"name": "Shared name"}
    ).status_code == 200


def test_a_project_allowlist_refuses_a_wildcard_origin_and_an_unknown_model(admin):
    project = _make_project(admin)
    wildcard = admin.patch(
        f"/admin/api/developers/projects/{project['id']}", json={"allowed_origins": ["*"]}
    )
    assert wildcard.status_code == 422
    typo = admin.patch(
        f"/admin/api/developers/projects/{project['id']}",
        json={"allowed_models": ["techsara-35"]},
    )
    assert typo.status_code == 422
    good = admin.patch(
        f"/admin/api/developers/projects/{project['id']}",
        json={
            "allowed_origins": ["https://app.example.com"],
            "allowed_models": [registry.PUBLIC_MODEL_IDS[0]],
        },
    )
    assert good.status_code == 200
    assert good.json()["project"]["allowed_origins"] == ["https://app.example.com"]


def test_the_model_list_shows_what_the_code_declares_and_the_database_narrowing(
    admin, root
):
    """The database may only take a model AWAY (CONTRACT-3 §15).

    A `public_models` row naming an id the code does not declare cannot appear
    in this list at all, because there is nothing for it to be a flag on.
    """
    db.set_public_model_enabled(
        store.default_workspace()["id"], "a-model-nobody-declared", True
    )
    before = admin.get("/admin/api/developers/models").json()
    assert [m["id"] for m in before["models"]] == list(registry.PUBLIC_MODEL_IDS)
    assert before["models"][0]["enabled"] is True
    assert before["can_manage"] is False
    assert "internal" not in json.dumps(before)

    assert (
        root.put(
            f"/admin/api/developers/models/{registry.PUBLIC_MODEL_IDS[0]}",
            json={"enabled": False},
        ).status_code
        == 200
    )
    after = root.get("/admin/api/developers/models").json()
    assert after["models"][0]["enabled"] is False
    assert after["can_manage"] is True
    # An id the code does not declare is a 404 and writes no row.
    assert root.put(
        "/admin/api/developers/models/a-model-nobody-declared", json={"enabled": False}
    ).status_code == 404


def test_request_logs_carry_metadata_but_never_the_prompt_or_the_generated_text(admin):
    """CONTRACT-3 §16: request logs are metadata only.

    `error_message` is absent for the same reason as `output_text`: it is an
    engine-produced string that can carry an upstream URL, a container name or
    a traceback fragment, all three of which CONTRACT-3 §9 forbids on any wire.
    """
    workspace = store.default_workspace()["id"]
    project = _make_project(admin)
    created = _make_key(admin, project["id"])
    response = db.create_api_response(
        project["id"],
        workspace,
        registry.PUBLIC_MODEL_IDS[0],
        "req_abc123",
        key_id=created["key"]["id"],
        metadata={"customer_request_id": "abc-123"},
    )
    db.update_api_response(
        response["id"],
        project["id"],
        status="failed",
        output_text="the model's answer about quarterly numbers",
        error_code="model_unavailable",
        error_message="connection refused to http://10.0.0.7:8000/v1",
        input_tokens=37,
        output_tokens=112,
    )

    logs = admin.get(f"/admin/api/developers/projects/{project['id']}/logs")
    assert logs.status_code == 200
    body = logs.text
    assert "quarterly numbers" not in body
    assert "10.0.0.7" not in body
    assert "error_message" not in body
    line = logs.json()["requests"][0]
    assert line["id"] == response["id"]
    assert line["request_id"] == "req_abc123"
    assert line["error_code"] == "model_unavailable"
    assert line["input_tokens"] == 37
    assert line["metadata"] == {"customer_request_id": "abc-123"}
    assert line["key"]["last_four"] == created["key"]["last_four"]


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


def test_a_webhook_endpoint_is_created_updated_tested_and_deleted_with_audit(admin):
    project = _make_project(admin)
    created = admin.post(
        f"/admin/api/developers/projects/{project['id']}/webhooks",
        json={
            "url": "https://hooks.example.com/techsara",
            "events": ["response.completed", "response.failed"],
        },
    )
    assert created.status_code == 200, created.text
    endpoint = created.json()["webhook"]
    # The endpoint SAYS it holds a secret; the value travels once, beside it.
    assert endpoint["has_secret"] is True
    assert endpoint["rotation_in_progress"] is False
    with db.connection() as con:
        stored = con.execute(
            "SELECT secret FROM api_webhook_endpoints WHERE id = %s", (endpoint["id"],)
        ).fetchone()["secret"]
    assert stored and created.json()["secret"] == stored
    listed = admin.get(f"/admin/api/developers/projects/{project['id']}/webhooks")
    assert stored not in listed.text
    assert listed.json()["webhooks"][0]["has_secret"] is True

    updated = admin.patch(
        f"/admin/api/developers/projects/{project['id']}/webhooks/{endpoint['id']}",
        json={"events": ["response.completed"]},
    )
    assert updated.status_code == 200
    assert updated.json()["webhook"]["events"] == ["response.completed"]

    # A test delivery is QUEUED, never sent from the request thread: the SSRF
    # check belongs to the delivery path, against the resolved address.
    drill = admin.post(
        f"/admin/api/developers/projects/{project['id']}/webhooks/{endpoint['id']}/test"
    )
    assert drill.status_code == 200
    assert drill.json()["queued"] is True
    with db.connection() as con:
        row = con.execute(
            "SELECT event_type, status, payload FROM api_webhook_deliveries "
            "WHERE endpoint_id = %s",
            (endpoint["id"],),
        ).fetchone()
    assert row["event_type"] == console_api.WEBHOOK_TEST_EVENT
    assert row["status"] == "pending"
    assert row["payload"]["data"]["endpoint_id"] == endpoint["id"]

    assert admin.delete(
        f"/admin/api/developers/projects/{project['id']}/webhooks/{endpoint['id']}"
    ).status_code == 200
    assert admin.get(
        f"/admin/api/developers/projects/{project['id']}/webhooks"
    ).json()["webhooks"] == []

    for action in (
        "api_webhook_created",
        "api_webhook_updated",
        "api_webhook_test_sent",
        "api_webhook_deleted",
    ):
        events = _audit_rows(action)
        assert len(events) == 1, action
        assert events[0]["resource_type"] == "api_webhook_endpoint"
        assert events[0]["resource_id"] == endpoint["id"]


def test_a_webhook_url_must_be_https_and_carry_no_credentials(admin):
    project = _make_project(admin)
    for url in (
        "http://hooks.example.com/techsara",
        "https://user:pass@hooks.example.com/techsara",
        "ftp://hooks.example.com/techsara",
    ):
        response = admin.post(
            f"/admin/api/developers/projects/{project['id']}/webhooks",
            json={"url": url, "events": ["response.completed"]},
        )
        assert response.status_code == 422, url
    assert db.list_webhook_endpoints(project["id"], store.default_workspace()["id"]) == []


def test_a_webhook_cannot_subscribe_to_an_event_this_platform_never_emits(admin):
    project = _make_project(admin)
    response = admin.post(
        f"/admin/api/developers/projects/{project['id']}/webhooks",
        json={"url": "https://hooks.example.com/x", "events": ["response.started"]},
    )
    assert response.status_code == 422
    empty = admin.post(
        f"/admin/api/developers/projects/{project['id']}/webhooks",
        json={"url": "https://hooks.example.com/x", "events": []},
    )
    assert empty.status_code == 422


def test_a_console_request_cannot_choose_a_webhook_signing_secret(admin):
    """The accessor's allow-list carries `secret` because rotation needs it;
    this handler's does not. A caller-chosen signing secret would make every
    signature forgeable by whoever watched the request."""
    project = _make_project(admin)
    endpoint = admin.post(
        f"/admin/api/developers/projects/{project['id']}/webhooks",
        json={"url": "https://hooks.example.com/x", "events": ["response.completed"]},
    ).json()["webhook"]
    response = admin.patch(
        f"/admin/api/developers/projects/{project['id']}/webhooks/{endpoint['id']}",
        json={"secret": "whsec_attacker_chosen"},
    )
    assert response.status_code == 422
    with db.connection() as con:
        stored = con.execute(
            "SELECT secret FROM api_webhook_endpoints WHERE id = %s", (endpoint["id"],)
        ).fetchone()["secret"]
    assert stored != "whsec_attacker_chosen"


# ---------------------------------------------------------------------------
# The playground
# ---------------------------------------------------------------------------


def test_the_playground_runs_without_a_key_and_returns_the_public_response_shape(
    admin, fake_model
):
    """The console's "try it" box asks for no credential at all.

    A playground that demanded a pasted key would teach people to mint
    long-lived credentials into a browser. This one is authenticated by the
    session and `api.console.access`, and the body it returns is CONTRACT-3
    §9's — the same shape the customer's own code will parse.
    """
    fake = fake_model()
    response = admin.post(
        "/admin/api/developers/playground/execute",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "Explain RAG."},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["model"] == registry.PUBLIC_MODEL_IDS[0]
    # The reasoning delta is consumed and dropped; only answer text is output.
    assert body["output"][0]["content"][0]["text"] == "Retrieval-augmented."
    assert "hmm" not in response.text
    assert body["usage"] == {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16}
    assert response.headers["X-Request-Id"].startswith("req_")

    # It reached the model through the public API's own entry point, with an
    # explicit effort and a resolved output ceiling.
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["messages"] == [{"role": "user", "content": "Explain RAG."}]
    # The SAME effort `/v1` uses: the console shows what a customer gets.
    assert call["effort"] == streaming.PUBLIC_EFFORT
    assert call["max_tokens"] == console_api.FALLBACK_MAX_OUTPUT_TOKENS

    # No key was created, consulted or needed.
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM api_keys").fetchone()["n"] == 0
        ledger = con.execute(
            "SELECT route, status, mode, meta FROM usage_events"
        ).fetchall()
    assert len(ledger) == 1
    assert ledger[0]["route"] == console_api.PLAYGROUND_ROUTE
    assert ledger[0]["status"] == "ok"
    assert ledger[0]["meta"]["public_model"] == registry.PUBLIC_MODEL_IDS[0]


def test_the_playground_refuses_a_parameter_the_public_api_cannot_honour(
    admin, fake_model
):
    """Same validation as `/v1`, same envelope, same refusal to guess.

    `top_p` is the shape every OpenAI-flavoured client library attaches, and a
    console that accepted and dropped it would teach a developer their knob
    works before their own integration proves otherwise.
    """
    fake = fake_model()
    response = admin.post(
        "/admin/api/developers/playground/execute",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi", "top_p": 0.3},
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "invalid_request_error"
    assert error["request_id"].startswith("req_")

    background = admin.post(
        "/admin/api/developers/playground/execute",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi", "background": True},
    )
    assert background.status_code == 400
    assert background.json()["error"]["param"] == "background"
    # Nothing was generated for either refusal.
    assert fake.calls == []


def test_the_playground_resolves_models_through_the_same_narrowing_as_v1(
    admin, root, fake_model
):
    """A model the database disabled is a 404 here too — never a 403, and
    never a quietly-served answer from a model an operator turned off."""
    fake = fake_model()
    assert (
        root.put(
            f"/admin/api/developers/models/{registry.PUBLIC_MODEL_IDS[0]}",
            json={"enabled": False},
        ).status_code
        == 200
    )
    response = admin.post(
        "/admin/api/developers/playground/execute",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    assert fake.calls == []

    unknown = admin.post(
        "/admin/api/developers/playground/execute",
        json={"model": "gpt-5.2", "input": "hi"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "model_not_found"


def test_the_playground_honours_a_projects_model_allowlist(admin, fake_model):
    """`project_id` is a QUERY parameter and contributes only an allowlist —
    nothing in a body may select a tenant (CONTRACT-3 §8)."""
    fake = fake_model()
    project = _make_project(admin)
    assert admin.patch(
        f"/admin/api/developers/projects/{project['id']}",
        json={"allowed_models": [registry.PUBLIC_MODEL_IDS[0]]},
    ).status_code == 200
    allowed = admin.post(
        f"/admin/api/developers/playground/execute?project_id={project['id']}",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"},
    )
    assert allowed.status_code == 200
    # A project in another workspace is not selectable at all.
    foreign = admin.post(
        "/admin/api/developers/playground/execute?project_id=proj_does_not_exist",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"},
    )
    assert foreign.status_code == 404
    assert len(fake.calls) == 1


def test_a_playground_failure_returns_the_error_envelope_and_no_internal_detail(
    admin, monkeypatch
):
    """An unexpected exception is recorded, not echoed (CONTRACT-3 §9).

    No traceback, no path, no hostname — and the failed run still lands in the
    usage ledger, because a run that burned GPU and then failed is exactly the
    kind the analytics console must not lose.
    """
    from app import llm

    def boom(*args, **kwargs):
        # An async generator, like the real `llm.stream_chat_events`: the
        # failure surfaces on first iteration, which is where the engine
        # raises it.
        async def _gen():
            raise RuntimeError("vllm-head at http://10.0.0.7:8000 refused the connection")
            yield  # pragma: no cover

        return _gen()

    monkeypatch.setattr(llm, "stream_chat_events", boom)
    response = admin.post(
        "/admin/api/developers/playground/execute",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"},
    )
    assert response.status_code == 500
    assert "10.0.0.7" not in response.text
    assert "RuntimeError" not in response.text
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["type"] == "server_error"
    with db.connection() as con:
        row = con.execute("SELECT route, status, error_kind FROM usage_events").fetchone()
    assert row["route"] == console_api.PLAYGROUND_ROUTE
    assert row["status"] == "error"
    assert row["error_kind"] == "internal_error"


def test_the_playground_writes_no_api_response_row_and_no_conversation(admin, fake_model):
    """A console experiment is not a project's API traffic.

    If it wrote an `api_responses` row, a project's request log would mix its
    customers' calls with its administrator's experiments, and CONTRACT-3 §8's
    promise that the API never writes to anyone's conversation history would
    need an asterisk.
    """
    fake_model()
    project = _make_project(admin)
    assert admin.post(
        f"/admin/api/developers/playground/execute?project_id={project['id']}",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"},
    ).status_code == 200
    with db.connection() as con:
        responses = con.execute("SELECT count(*) AS n FROM api_responses").fetchone()["n"]
        messages = con.execute("SELECT count(*) AS n FROM messages").fetchone()["n"]
    assert responses == 0
    assert messages == 0
    assert admin.get(
        f"/admin/api/developers/projects/{project['id']}/logs"
    ).json()["requests"] == []


def test_a_streamed_playground_run_follows_the_public_event_grammar(admin, fake_model):
    """`stream: true` emits CONTRACT-3 §10 — not a console-only shape.

    Same event names, numbering from 1 by exactly 1, usage only on the
    terminal, and exactly one terminal. A console that invented its own frames
    would show a developer something their own client will never receive.
    """
    fake_model()
    response = admin.post(
        "/admin/api/developers/playground/execute",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "Explain RAG.", "stream": True},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store, no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["X-Request-Id"].startswith("req_")

    frames = api_events.parse_frames(response.text)
    names = [frame["event"] for frame in frames]
    assert names == [
        "response.created",
        "response.in_progress",
        "response.output_text.delta",
        "response.output_text.delta",
        "response.output_text.done",
        "response.completed",
    ]
    assert [frame["data"]["sequence_number"] for frame in frames] == [1, 2, 3, 4, 5, 6]
    assert len([n for n in names if n in api_events.TERMINAL_EVENTS]) == 1
    # The reasoning delta never reached the wire.
    assert "hmm" not in response.text
    assert "".join(
        frame["data"]["delta"] for frame in frames if frame["event"].endswith("delta")
    ) == "Retrieval-augmented."
    # usage is null until the terminal, and real on it.
    assert all(
        frame["data"]["response"]["usage"] is None
        for frame in frames
        if "response" in frame["data"] and frame["event"] != "response.completed"
    )
    assert frames[-1]["data"]["response"]["usage"] == {
        "input_tokens": 11,
        "output_tokens": 5,
        "total_tokens": 16,
    }
    assert frames[-1]["data"]["response"]["status"] == "completed"

    with db.connection() as con:
        row = con.execute("SELECT route, status, meta FROM usage_events").fetchone()
    assert row["route"] == console_api.PLAYGROUND_ROUTE
    assert row["status"] == "ok"
    assert row["meta"]["streamed"] is True


def test_a_streamed_failure_ends_in_one_response_failed_frame_with_no_internal_detail(
    admin, monkeypatch
):
    """A generation that dies mid-stream ends the stream ONCE, with the code
    and nothing else — no traceback, no hostname (CONTRACT-3 §9).

    `response.failed`, not the out-of-band `error` frame: the generation had
    started, so CONTRACT-3 §10's terminal carries the response object — the
    frame `/v1` sends, which the console's own pump never emitted before
    2026-09-13."""
    from app import llm

    def boom(*args, **kwargs):
        async def _gen():
            yield "token", "Retr"
            raise RuntimeError("vllm-head at http://10.0.0.7:8000 died")

        return _gen()

    monkeypatch.setattr(llm, "stream_chat_events", boom)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    response = admin.post(
        "/admin/api/developers/playground/execute",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi", "stream": True},
    )
    assert response.status_code == 200  # the headers were already sent
    assert "10.0.0.7" not in response.text
    assert "RuntimeError" not in response.text
    frames = api_events.parse_frames(response.text)
    names = [frame["event"] for frame in frames]
    assert names[-1] == "response.failed"
    assert len([n for n in names if n in api_events.TERMINAL_EVENTS]) == 1
    assert frames[-1]["data"]["response"]["status"] == "failed"
    assert frames[-1]["data"]["response"]["error"]["code"] == "internal_error"
    # The partial text still reached the caller before the failure.
    assert any(frame["event"].endswith("delta") for frame in frames)
    with db.connection() as con:
        row = con.execute("SELECT status, error_kind FROM usage_events").fetchone()
    assert (row["status"], row["error_kind"]) == ("error", "internal_error")


def test_a_streamed_run_heartbeats_while_the_engine_is_still_thinking(
    admin, monkeypatch
):
    """The silence that kills a connection is the one BEFORE the first token.

    The heartbeat window is shortened for the test and the first delta is made
    to arrive late; the stream must carry a comment frame in the gap, and the
    comment must not disturb the numbering of the real events.
    """
    from app import llm
    from app.publicapi import events as events_mod

    monkeypatch.setattr(events_mod, "HEARTBEAT_SECONDS", 0.05)

    def slow(*args, **kwargs):
        async def _gen():
            import asyncio

            await asyncio.sleep(0.2)
            yield "token", "late"

        return _gen()

    monkeypatch.setattr(llm, "stream_chat_events", slow)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    response = admin.post(
        "/admin/api/developers/playground/execute",
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi", "stream": True},
    )
    assert ": ping" in response.text
    frames = api_events.parse_frames(response.text)
    assert [frame["data"]["sequence_number"] for frame in frames] == [1, 2, 3, 4, 5]
    assert frames[-1]["event"] == "response.completed"
    # Not measured is null, never zero.
    assert frames[-1]["data"]["response"]["usage"] is None


# ---------------------------------------------------------------------------
# The 2026-09-13 adversarial review
# ---------------------------------------------------------------------------

PLAYGROUND = "/admin/api/developers/playground/execute"


@pytest.fixture(autouse=True)
def _no_leaked_slots():
    """In-process concurrency slots are process state; a test that leaked one
    would fail the NEXT test for a reason that has nothing to do with it."""
    quotas.reset_concurrency()
    yield
    quotas.reset_concurrency()


def _concurrently(count: int, fn):
    """Run `fn(index)` on `count` threads released at the same instant; return
    the results in index order. Real threads, real HTTP through the app, real
    PostgreSQL — the shape in which the review proved each race."""
    import threading

    barrier = threading.Barrier(count)
    results: List[Any] = [None] * count

    def run(index: int) -> None:
        barrier.wait()
        try:
            results[index] = fn(index)
        except Exception as exc:  # noqa: BLE001 — surfaced by the assertion
            results[index] = exc

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return results


class GatedModel(FakeModel):
    """A fake engine that holds every generation open until the test lets go —
    across event loops, because each TestClient request runs on its own."""

    def __init__(self):
        import threading

        super().__init__(chunks=[("token", "held")])
        self.gate = threading.Event()
        self.started = 0

    def stream(self, messages, **kwargs):
        self.calls.append({"messages": list(messages), **kwargs})
        gate = self.gate

        async def _gen():
            import asyncio

            self.started += 1
            while not gate.is_set():
                await asyncio.sleep(0.01)
            yield "token", "held"

        return _gen()


# ---- (a) the body cap counts bytes --------------------------------------------


def _big_prompt_body(size: int) -> bytes:
    return json.dumps(
        {"model": registry.PUBLIC_MODEL_IDS[0], "input": "x" * size}
    ).encode()


#: The playground's cap in these tests. Below the application-wide 1 MiB body
#: middleware on purpose: the handler's own count is what is under test, and a
#: body the middleware refuses first would prove the middleware, not the fix.
SMALL_CAP = 64 * 1024


def test_a_chunked_playground_body_over_the_cap_is_refused_before_the_model(
    admin, fake_model, monkeypatch
):
    """Transfer-Encoding: chunked declares no length; the review put 3 MiB
    into the model this way."""
    fake = fake_model()
    monkeypatch.setattr(console_api.api_models, "max_body_bytes", lambda: SMALL_CAP)
    body = _big_prompt_body(200 * 1024)

    def chunks():
        for start in range(0, len(body), 65536):
            yield body[start : start + 65536]

    response = admin.post(
        PLAYGROUND, content=chunks(), headers={"content-type": "application/json"}
    )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "request_too_large"
    assert fake.calls == []


def test_a_playground_body_with_a_lying_content_length_is_refused_by_what_arrived(
    admin, fake_model, monkeypatch
):
    fake = fake_model()
    monkeypatch.setattr(console_api.api_models, "max_body_bytes", lambda: SMALL_CAP)
    body = _big_prompt_body(200 * 1024)

    response = admin.post(
        PLAYGROUND,
        content=body,
        headers={"content-type": "application/json", "content-length": "10"},
    )

    assert response.status_code == 413, response.text
    assert response.json()["error"]["code"] == "request_too_large"
    assert fake.calls == []


def test_a_non_numeric_content_length_is_a_400_not_a_500():
    """The review's `Content-Length: abc` was an unhandled ValueError. Driven
    at the reader directly: an HTTP client refuses to send that header."""

    class _Req:
        headers = {"content-length": "abc"}

        async def stream(self):  # pragma: no cover — never reached
            yield b"{}"

    import asyncio

    with pytest.raises(console_api.api_errors.ApiError) as refused:
        asyncio.run(console_api._read_capped_body(_Req(), 1024))
    assert refused.value.status == 400


def test_a_playground_body_at_most_the_cap_is_accepted(admin, fake_model, monkeypatch):
    fake = fake_model()
    monkeypatch.setattr(console_api.api_models, "max_body_bytes", lambda: 4096)
    body = _big_prompt_body(100)

    def chunks():
        yield body[:10]
        yield body[10:]

    response = admin.post(
        PLAYGROUND, content=chunks(), headers={"content-type": "application/json"}
    )

    assert response.status_code == 200, response.text
    assert len(fake.calls) == 1


# ---- (b) the playground goes through the quota engine -------------------------


@pytest.mark.usefixtures("limits_enforced")
def test_concurrent_playground_runs_are_held_to_the_workspace_allowance(admin, monkeypatch):
    """Twelve concurrent runs, each held open by the engine: exactly the
    allowance's `max_concurrency` get a lane, every other one is a 429 with a
    Retry-After — and the model is invoked only for the admitted ones.
    Before 2026-09-13 all twelve reached the shared admission lanes."""
    from app import llm

    fake = GatedModel()
    monkeypatch.setattr(llm, "stream_chat_events", fake.stream)
    monkeypatch.setattr(llm, "get_usage", fake.usage)
    monkeypatch.setenv("API_PLAYGROUND_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("API_PLAYGROUND_RPM", "1000")
    # Output tokens are RESERVED at admission since the quotas wave-3 fix, and
    # twelve runs' ceilings exceed the default 30k output_tpm: widened so this
    # test measures the concurrency cap and only the concurrency cap.
    monkeypatch.setenv("API_PLAYGROUND_OUTPUT_TPM", "10000000")
    monkeypatch.setenv("API_PLAYGROUND_DAILY_TOKEN_QUOTA", "100000000")
    refused = []

    def run(index):
        response = admin.post(
            PLAYGROUND, json={"model": registry.PUBLIC_MODEL_IDS[0], "input": f"q{index}"}
        )
        if response.status_code == 429:
            refused.append(response)
        return response

    import threading
    import time as _time

    def release_when_the_rest_are_refused():
        deadline = _time.monotonic() + 30
        while len(refused) < 10 and _time.monotonic() < deadline:
            _time.sleep(0.02)
        fake.gate.set()

    releaser = threading.Thread(target=release_when_the_rest_are_refused)
    releaser.start()
    try:
        results = _concurrently(12, run)
    finally:
        fake.gate.set()
        releaser.join()

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 200] + [429] * 10, statuses
    for response in results:
        if response.status_code == 429:
            assert response.json()["error"]["code"] == "concurrency_limit_exceeded"
            assert int(response.headers["Retry-After"]) >= 1
    assert len(fake.calls) == 2


@pytest.mark.usefixtures("limits_enforced")
def test_concurrent_playground_runs_cannot_exceed_the_allowance_rate_limit(
    admin, fake_model, monkeypatch
):
    """The per-minute window, raced: twelve simultaneous first runs in a
    workspace against rpm=5 admit exactly five. This is `quotas.reserve`'s
    advisory lock doing its job through the playground path — and the twelve
    simultaneous first runs also race to create the allowance row."""
    fake = fake_model()
    monkeypatch.setenv("API_PLAYGROUND_RPM", "5")
    monkeypatch.setenv("API_PLAYGROUND_MAX_CONCURRENCY", "50")

    results = _concurrently(
        12,
        lambda index: admin.post(
            PLAYGROUND, json={"model": registry.PUBLIC_MODEL_IDS[0], "input": f"q{index}"}
        ),
    )

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200] * 5 + [429] * 7, statuses
    assert all(
        r.json()["error"]["code"] == "rate_limit_error" for r in results if r.status_code == 429
    )
    assert len(fake.calls) == 5
    workspace_id = store.default_workspace()["id"]
    with db.connection() as con:
        requests = con.execute(
            "SELECT COALESCE(SUM(requests), 0) AS n FROM api_usage_minute WHERE project_id = %s",
            (console_api.playground_project_id(workspace_id),),
        ).fetchone()["n"]
        projects = con.execute(
            "SELECT count(*) AS n FROM api_projects WHERE id = %s",
            (console_api.playground_project_id(workspace_id),),
        ).fetchone()["n"]
    assert requests == 5
    assert projects == 1


@pytest.mark.usefixtures("limits_enforced")
def test_a_playground_allowance_of_zero_means_no_runs_at_all(admin, fake_model, monkeypatch):
    fake = fake_model()
    monkeypatch.setenv("API_PLAYGROUND_RPM", "0")

    response = admin.post(PLAYGROUND, json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_error"
    assert fake.calls == []


@pytest.mark.usefixtures("limits_enforced")
def test_the_playground_allowance_is_per_workspace(admin, other_workspace, fake_model, monkeypatch):
    fake_model()
    monkeypatch.setenv("API_PLAYGROUND_RPM", "1")
    body = {"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"}

    assert admin.post(PLAYGROUND, json=body).status_code == 200
    assert admin.post(PLAYGROUND, json=body).status_code == 429
    # Workspace B's allowance is untouched by workspace A's spend.
    assert other_workspace.post(PLAYGROUND, json=body).status_code == 200


def test_the_playground_allowance_row_is_invisible_to_every_project_route(admin, fake_model):
    """The allowance is a quota ledger, not a project: never listed, never
    addressable, never given a key — so `/v1` can never reach it."""
    fake_model()
    assert admin.post(
        PLAYGROUND, json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"}
    ).status_code == 200
    hidden = console_api.playground_project_id(store.default_workspace()["id"])

    assert admin.get("/admin/api/developers/projects").json()["projects"] == []
    assert admin.get("/admin/api/developers/overview").json()["stats"]["projects"] == 0
    assert admin.get("/admin/api/developers/usage").json()["projects"] == []
    assert admin.get(f"/admin/api/developers/projects/{hidden}").status_code == 404
    assert admin.post(
        f"/admin/api/developers/projects/{hidden}/keys", json={"name": "sneaky"}
    ).status_code == 404
    assert admin.post(
        f"/admin/api/developers/projects/{hidden}/webhooks",
        json={"url": "https://hooks.example.com/x", "events": ["response.completed"]},
    ).status_code == 404
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM api_keys").fetchone()["n"] == 0


def test_a_streamed_playground_slot_comes_back_even_if_the_body_never_starts():
    """A client that leaves before the first byte never starts the body
    generator, so a release in its `finally` never runs. The response object's
    own `__call__` releases too; a plain StreamingResponse would strand it."""
    import asyncio
    import contextlib

    from fastapi.responses import StreamingResponse

    from app.apiplatform.resolver import ApiCaller, CallerLimits

    caller = ApiCaller(
        workspace_id="ws", project_id="proj_slot_test", service_account_id=None,
        key_id="k", scopes=frozenset(), models=(), environment="live",
        limits=CallerLimits(rpm=10, input_tpm=10, output_tpm=10, max_concurrency=5,
                            daily_token_quota=10),
    )

    def held_slot():
        stack = contextlib.ExitStack()
        stack.enter_context(quotas.concurrency_slot(caller, "stream"))
        return console_api._Slot(stack)

    async def frames(slot):
        try:
            yield "data: never\n\n"
        finally:
            slot.release()

    async def gone_before_the_status_line(message):
        raise OSError("client went away")

    async def receive():
        return {"type": "http.disconnect"}

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}

    plain_slot = held_slot()
    plain = StreamingResponse(frames(plain_slot))
    with contextlib.suppress(Exception):
        asyncio.run(plain(scope, receive, gone_before_the_status_line))
    assert quotas.in_flight(caller) == 1  # the leak the subclass exists for
    plain_slot.release()

    slot = held_slot()
    response = console_api._SlotStreamingResponse(frames(slot), slot=slot)
    with contextlib.suppress(Exception):
        asyncio.run(response(scope, receive, gone_before_the_status_line))
    assert quotas.in_flight(caller) == 0


@pytest.mark.usefixtures("limits_enforced")
def test_a_streamed_playground_run_over_the_allowance_is_a_429_status_not_a_dead_stream(
    admin, fake_model, monkeypatch
):
    fake = fake_model()
    monkeypatch.setenv("API_PLAYGROUND_MAX_CONCURRENCY", "0")

    response = admin.post(
        PLAYGROUND,
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi", "stream": True},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "concurrency_limit_exceeded"
    assert fake.calls == []


def test_an_admission_refusal_in_the_playground_is_the_v1_503_not_a_500(admin, monkeypatch):
    """One pump since 2026-09-13: the engine refusals map to the §9 codes
    `/v1` uses, with a Retry-After, instead of the console's own 500 — and a
    full admission lane is the engine at capacity (503 model_unavailable),
    not a concurrency limit."""
    from app import llm

    class AdmissionRejected(Exception):
        pass

    def refused(*args, **kwargs):
        async def _gen():
            raise AdmissionRejected("all NORMAL lanes busy")
            yield  # pragma: no cover

        return _gen()

    monkeypatch.setattr(llm, "stream_chat_events", refused)
    monkeypatch.setattr(llm, "get_usage", lambda: None)

    response = admin.post(PLAYGROUND, json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"})

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "model_unavailable"
    assert "Retry-After" in response.headers
    assert "NORMAL" not in response.text


@pytest.mark.usefixtures("limits_enforced")
def test_a_playground_run_carries_the_ratelimit_headers(admin, fake_model):
    fake_model()
    response = admin.post(PLAYGROUND, json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"})
    assert response.status_code == 200
    assert "RateLimit" in response.headers
    assert "RateLimit-Policy" in response.headers


# ---- (c) webhook URLs are displayed and audited redacted -----------------------


def test_a_webhook_url_token_never_reaches_a_listing_or_the_audit_log(admin):
    project = _make_project(admin)
    url = "https://hooks.example.com/cb?token=s3cr3t-query-token"
    created = admin.post(
        f"/admin/api/developers/projects/{project['id']}/webhooks",
        json={"url": url, "events": ["response.completed"]},
    )
    assert created.status_code == 200, created.text
    endpoint_id = created.json()["webhook"]["id"]
    moved = admin.patch(
        f"/admin/api/developers/projects/{project['id']}/webhooks/{endpoint_id}",
        json={"url": "https://user:pw@hooks.example.com/v2?sig=another-s3cr3t"},
    )
    # Credentials in userinfo are still refused outright at configuration.
    assert moved.status_code == 422
    moved = admin.patch(
        f"/admin/api/developers/projects/{project['id']}/webhooks/{endpoint_id}",
        json={"url": "https://hooks.example.com/v2?sig=another-s3cr3t"},
    )
    assert moved.status_code == 200

    listing = admin.get(f"/admin/api/developers/projects/{project['id']}/webhooks")
    deliveries = admin.get(
        f"/admin/api/developers/projects/{project['id']}/webhooks/{endpoint_id}/deliveries"
    )
    audit_meta = json.dumps(
        [row["meta"] for row in _audit_rows("api_webhook_created") + _audit_rows("api_webhook_updated")]
    )
    for text in (created.text, moved.text, listing.text, deliveries.text, audit_meta):
        assert "s3cr3t-query-token" not in text
        assert "another-s3cr3t" not in text
    assert listing.json()["webhooks"][0]["url"] == "https://hooks.example.com/v2?<redacted>"
    assert "https://hooks.example.com/v2?<redacted>" in audit_meta
    # Stored intact: the delivery has to reach the real URL.
    with db.connection() as con:
        stored = con.execute(
            "SELECT url FROM api_webhook_endpoints WHERE id = %s", (endpoint_id,)
        ).fetchone()["url"]
    assert stored == "https://hooks.example.com/v2?sig=another-s3cr3t"


# ---- (d) the secret flags read the columns the database returns ----------------


def test_the_console_reports_an_open_rotation_and_a_lapsed_one_honestly(admin):
    from datetime import datetime, timedelta, timezone

    project = _make_project(admin)
    endpoint_id = admin.post(
        f"/admin/api/developers/projects/{project['id']}/webhooks",
        json={"url": "https://hooks.example.com/x", "events": ["response.completed"]},
    ).json()["webhook"]["id"]
    workspace_id = store.default_workspace()["id"]

    def listed():
        return admin.get(
            f"/admin/api/developers/projects/{project['id']}/webhooks"
        ).json()["webhooks"][0]

    assert (listed()["has_secret"], listed()["rotation_in_progress"]) == (True, False)
    db.update_webhook_endpoint(
        endpoint_id, workspace_id, previous_secret="whsec_old-one-000000000000",
        previous_secret_expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    assert listed()["rotation_in_progress"] is True
    db.update_webhook_endpoint(
        endpoint_id, workspace_id,
        previous_secret_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    assert listed()["rotation_in_progress"] is False
    assert "whsec_old-one" not in admin.get(
        f"/admin/api/developers/projects/{project['id']}/webhooks"
    ).text


# ---- (e) model toggles are per workspace ---------------------------------------


def test_a_super_admin_disabling_a_model_affects_only_their_own_workspace(
    root, other_workspace, fake_model
):
    fake_model()
    model_id = registry.PUBLIC_MODEL_IDS[0]
    assert root.put(
        f"/admin/api/developers/models/{model_id}", json={"enabled": False}
    ).status_code == 200

    mine = root.get("/admin/api/developers/models").json()["models"][0]
    theirs = other_workspace.get("/admin/api/developers/models").json()["models"][0]
    assert (mine["enabled"], theirs["enabled"]) == (False, True)
    assert root.post(PLAYGROUND, json={"model": model_id, "input": "hi"}).status_code == 404
    assert other_workspace.post(PLAYGROUND, json={"model": model_id, "input": "hi"}).status_code == 200
    assert db.public_model_overrides(WORKSPACE_B, [model_id]) == {}
    assert db.public_model_overrides(store.default_workspace()["id"], [model_id]) == {
        model_id: False
    }


# ---- (f) the two routes the console already calls ------------------------------


def test_the_settings_route_describes_the_platform_without_disclosing_it(admin, member):
    response = admin.get("/admin/api/developers/settings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api"]["base_path"] == "/v1"
    assert body["api"]["key_prefixes"] == {"live": "tsk_live_", "test": "tsk_test_"}
    assert body["playground"]["allowance"] == console_api.playground_allowance()
    assert body["webhooks"]["events"] == list(console_api.WEBHOOK_EVENTS)
    assert body["capabilities"]["webhooks_manage"] is True
    for forbidden in ("http://", "10.0.", "pepper", "whsec_", "vllm", "postgres"):
        assert forbidden not in response.text.lower()
    assert member.get("/admin/api/developers/settings").status_code == 404


def test_the_delivery_history_route_lists_an_endpoints_attempts_within_its_workspace(
    admin, other_workspace
):
    project = _make_project(admin)
    endpoint_id = admin.post(
        f"/admin/api/developers/projects/{project['id']}/webhooks",
        json={"url": "https://hooks.example.com/x?t=tok", "events": ["response.completed"]},
    ).json()["webhook"]["id"]
    for _ in range(3):
        assert admin.post(
            f"/admin/api/developers/projects/{project['id']}/webhooks/{endpoint_id}/test"
        ).json()["queued"] is True
    with db.connection() as con:
        con.execute(
            "UPDATE api_webhook_deliveries SET error = %s WHERE endpoint_id = %s",
            ("delivery failed: 502 from https://hooks.example.com/x?t=tok", endpoint_id),
        )
    path = f"/admin/api/developers/projects/{project['id']}/webhooks/{endpoint_id}/deliveries"

    page = admin.get(path)
    assert page.status_code == 200, page.text
    rows = page.json()["deliveries"]
    assert len(rows) == 3
    assert {row["event_type"] for row in rows} == {"webhook.test"}
    assert all("payload" not in row for row in rows)
    assert "t=tok" not in page.text
    assert len(admin.get(path, params={"limit": 2}).json()["deliveries"]) == 2
    assert len(admin.get(path, params={"limit": 2, "offset": 2}).json()["deliveries"]) == 1
    # Bounded server-side, and another workspace cannot read it.
    assert admin.get(path, params={"limit": 1000}).status_code == 422
    assert admin.get(path, params={"offset": 10**7}).status_code == 422
    assert admin.get(path, params={"limit": 0}).status_code == 422
    assert other_workspace.get(path).status_code == 404


# ---- (g) every list parameter is bounded ---------------------------------------


def test_every_integer_query_parameter_on_the_console_router_is_bounded_both_ways():
    """Structural, so a list route added later without bounds fails here."""
    from fastapi.params import Query as QueryParam

    unbounded = []
    checked = 0
    for route in console_api.router.routes:
        for param in route.dependant.query_params:
            info = param.field_info
            if param.field_info.annotation is not int:
                continue
            checked += 1
            ge = next((m.ge for m in info.metadata if getattr(m, "ge", None) is not None), None)
            le = next((m.le for m in info.metadata if getattr(m, "le", None) is not None), None)
            if not isinstance(info, QueryParam) or ge is None or le is None:
                unbounded.append(f"{route.path}:{param.name}")
    assert checked >= 4
    assert unbounded == []


def test_a_request_log_page_larger_than_the_bound_is_refused(admin):
    project = _make_project(admin)
    path = f"/admin/api/developers/projects/{project['id']}/logs"
    assert admin.get(path, params={"limit": console_api.MAX_LOG_LIMIT}).status_code == 200
    assert admin.get(path, params={"limit": console_api.MAX_LOG_LIMIT + 1}).status_code == 422


# ---- rotation, endpoint cap: the races, raced ----------------------------------


def test_twelve_concurrent_rotations_of_one_key_mint_exactly_one_replacement(admin):
    project = _make_project(admin)
    original = _make_key(admin, project["id"], name="server")["key"]
    path = f"/admin/api/developers/projects/{project['id']}/keys/{original['id']}/rotate"

    results = _concurrently(12, lambda index: admin.post(path, json={}))

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200] + [409] * 11, statuses
    with db.connection() as con:
        keys = con.execute(
            "SELECT id, status, rotated_from, rotation_expires_at FROM api_keys ORDER BY created_at"
        ).fetchall()
    assert len(keys) == 2
    assert len(_audit_rows("api_key_rotated")) == 1


def test_a_default_rotation_keeps_the_old_key_working_for_the_overlap(admin):
    from datetime import datetime, timedelta, timezone

    project = _make_project(admin)
    original = _make_key(admin, project["id"], name="server")["key"]
    before = datetime.now(timezone.utc)

    rotated = admin.post(
        f"/admin/api/developers/projects/{project['id']}/keys/{original['id']}/rotate",
        json={"overlap_hours": 24},
    )

    assert rotated.status_code == 200, rotated.text
    body = rotated.json()
    assert body["revoked"] is None
    assert body["previous"]["id"] == original["id"]
    assert body["previous"]["status"] == "active"
    expires = datetime.fromisoformat(body["previous"]["rotation_expires_at"])
    assert timedelta(hours=23, minutes=59) < expires - before < timedelta(hours=24, minutes=1)
    assert body["key"]["rotated_from"] == original["public_id"]
    # Already rotating: a second rotation is refused rather than a third key.
    assert admin.post(
        f"/admin/api/developers/projects/{project['id']}/keys/{original['id']}/rotate",
        json={},
    ).status_code == 409
    assert admin.post(
        f"/admin/api/developers/projects/{project['id']}/keys/{original['id']}/rotate",
        json={"overlap_hours": 24 * 31},
    ).status_code == 422


def test_concurrent_webhook_creates_cannot_exceed_the_per_project_cap(admin):
    project = _make_project(admin)
    path = f"/admin/api/developers/projects/{project['id']}/webhooks"

    results = _concurrently(
        12,
        lambda index: admin.post(
            path, json={"url": f"https://h{index}.example.com/x", "events": ["response.completed"]}
        ),
    )

    statuses = sorted(r.status_code for r in results)
    cap = console_api.MAX_WEBHOOK_ENDPOINTS_PER_PROJECT
    assert statuses == [200] * cap + [409] * (12 - cap), statuses
    assert len(admin.get(path).json()["webhooks"]) == cap


# ---- the residual test gaps ---------------------------------------------------


def test_the_playground_refuses_a_model_outside_the_projects_allowlist(
    admin, fake_model, monkeypatch
):
    """The allowlist test used to send only an ALLOWED model with one model
    declared, so ignoring the allowlist passed. Two declared models, one
    allowed: the other is a 404 and never reaches the engine."""
    import dataclasses

    real = registry.declared_models()[0]
    second = dataclasses.replace(real, id="techsara-second")
    monkeypatch.setattr(registry, "declared_models", lambda: (real, second))
    fake = fake_model()
    project = _make_project(admin)
    assert admin.patch(
        f"/admin/api/developers/projects/{project['id']}",
        json={"allowed_models": [real.id]},
    ).status_code == 200

    excluded = admin.post(
        f"{PLAYGROUND}?project_id={project['id']}", json={"model": second.id, "input": "hi"}
    )
    included = admin.post(
        f"{PLAYGROUND}?project_id={project['id']}", json={"model": real.id, "input": "hi"}
    )
    unrestricted = admin.post(PLAYGROUND, json={"model": second.id, "input": "hi"})

    assert excluded.status_code == 404
    assert excluded.json()["error"]["code"] == "model_not_found"
    assert included.status_code == 200
    assert unrestricted.status_code == 200
    assert [call["messages"][0]["content"] for call in fake.calls] == ["hi", "hi"]


def test_an_ip_allowlist_entry_that_is_a_hostname_is_refused(admin):
    project = _make_project(admin)
    path = f"/admin/api/developers/projects/{project['id']}"
    assert admin.patch(path, json={"ip_allowlist": ["evil.example.com"]}).status_code == 422
    assert admin.patch(path, json={"ip_allowlist": ["203.0.113.0/24", "2001:db8::1"]}).status_code == 200


def test_project_metadata_is_held_to_the_public_api_bounds(admin):
    project = _make_project(admin)
    path = f"/admin/api/developers/projects/{project['id']}"
    too_many = {f"k{index}": "v" for index in range(17)}
    assert admin.patch(path, json={"metadata": too_many}).status_code == 422
    assert admin.patch(path, json={"metadata": {"k": "v" * 513}}).status_code == 422
    assert admin.patch(path, json={"metadata": {"k" * 65: "v"}}).status_code == 422
    assert admin.patch(path, json={"metadata": {"k": "v" * 512}}).status_code == 200


def test_a_playground_output_ceiling_above_the_model_is_a_400(admin, fake_model):
    fake = fake_model()
    response = admin.post(
        PLAYGROUND,
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi", "max_output_tokens": 10**9},
    )
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "max_output_tokens"
    assert fake.calls == []


def test_the_overview_withholds_counts_the_reader_could_not_read_elsewhere(admin, monkeypatch):
    from app.authn.principal import Principal
    from app.authn.rbac import Cap

    _make_project(admin)
    real_can = Principal.can
    withheld = {Cap.API_PROJECTS_READ, Cap.API_USAGE_READ}
    monkeypatch.setattr(Principal, "can", lambda self, cap: cap not in withheld and real_can(self, cap))

    stats = admin.get("/admin/api/developers/overview").json()["stats"]

    assert stats["projects"] is None and stats["keys"] is None and stats["today"] is None
    assert isinstance(stats["models"], int)


def test_a_customer_project_cannot_hide_itself_by_claiming_the_playground_mark(admin):
    project = _make_project(admin)
    assert admin.patch(
        f"/admin/api/developers/projects/{project['id']}",
        json={"metadata": {"system": console_api.PLAYGROUND_SYSTEM_MARK}},
    ).status_code == 200

    assert [p["id"] for p in admin.get("/admin/api/developers/projects").json()["projects"]] == [
        project["id"]
    ]
    assert admin.get(f"/admin/api/developers/projects/{project['id']}").status_code == 200


# ---- wave 4: the pool, the refused reservation, the squatted name, the fan-out -


def _race_with_deadline(jobs, deadline_s: float):
    """Run every zero-argument job on its own daemon thread, released together,
    and FAIL (never hang) if any is still running after `deadline_s`.

    The pool deadlock this guards against does not raise on its own: every
    thread waits for a connection none of them will release, until PoolTimeout
    — and a regression that also lengthened the pool timeout would hang CI.
    Daemon threads, so a wedged one cannot keep the interpreter alive."""
    import threading
    import time as _time

    barrier = threading.Barrier(len(jobs))
    results: List[Any] = [None] * len(jobs)

    def run(index: int) -> None:
        barrier.wait()
        try:
            results[index] = jobs[index]()
        except Exception as exc:  # noqa: BLE001 — surfaced by the assertion
            results[index] = exc

    threads = [
        threading.Thread(target=run, args=(index,), daemon=True) for index in range(len(jobs))
    ]
    started = _time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=max(0.0, deadline_s - (_time.monotonic() - started)))
    stuck = [index for index, thread in enumerate(threads) if thread.is_alive()]
    if stuck:
        pytest.fail(f"{len(stuck)} of {len(jobs)} requests still running after {deadline_s}s")
    return results, _time.monotonic() - started


@pytest.fixture()
def small_pool(monkeypatch):
    """The app's ONE shared psycopg pool, shrunk to 4 with a 5 s checkout
    timeout, so "more concurrent requests than the pool holds" is 24 threads
    rather than a hundred. Rebuilt on both sides: `db.pool()` reads the
    settings only when it opens."""
    from app.config import settings

    db.close_pool()
    monkeypatch.setattr(settings, "app_db_pool_max", 4)
    monkeypatch.setattr(settings, "app_db_pool_min", 1)
    monkeypatch.setattr(settings, "app_db_pool_timeout", 5.0)
    yield 4
    db.close_pool()


def test_rotations_and_webhook_creates_beyond_the_pool_size_neither_deadlock_nor_starve_the_pool(
    admin, small_pool
):
    """THE 2026-09-13 WAVE-3 DEADLOCK. rotate_key and create_webhook held
    `pg_advisory_xact_lock` on one pooled connection while the accessors inside
    checked out a SECOND one. With more waiters than the pool holds, every
    connection belonged to a request blocked on the lock, the holder could
    never get its second, and the whole app — chat included — stalled until
    PoolTimeout turned them into 500s. Twelve rotations of one key and twelve
    creates on one project, against a pool of four: every request must get an
    honest answer (one rotation, the cap's worth of endpoints, 409 for the
    rest), no 500, well inside the deadline."""
    project = _make_project(admin)
    original = _make_key(admin, project["id"], name="server")["key"]
    rotate = f"/admin/api/developers/projects/{project['id']}/keys/{original['id']}/rotate"
    hooks = f"/admin/api/developers/projects/{project['id']}/webhooks"

    jobs = [lambda: admin.post(rotate, json={}) for _ in range(12)] + [
        (lambda index=index: admin.post(
            hooks, json={"url": f"https://h{index}.example.com/x", "events": ["response.completed"]}
        ))
        for index in range(12)
    ]
    results, elapsed = _race_with_deadline(jobs, deadline_s=45.0)

    assert not [r for r in results if isinstance(r, Exception)], results
    rotations = sorted(r.status_code for r in results[:12])
    creates = sorted(r.status_code for r in results[12:])
    cap = console_api.MAX_WEBHOOK_ENDPOINTS_PER_PROJECT
    assert rotations == [200] + [409] * 11, rotations
    assert creates == [200] * cap + [409] * (12 - cap), creates
    # Well under the 5 s checkout timeout per request: nobody waited it out.
    assert elapsed < 30.0, elapsed
    with db.connection() as con:
        live = con.execute(
            "SELECT count(*) AS n FROM api_keys WHERE project_id = %s", (project["id"],)
        ).fetchone()["n"]
        endpoints = con.execute(
            "SELECT count(*) AS n FROM api_webhook_endpoints WHERE project_id = %s",
            (project["id"],),
        ).fetchone()["n"]
    assert live == 2
    assert endpoints == cap
    assert len(_audit_rows("api_key_rotated")) == 1


def _playground_ledger(workspace_id: str) -> Dict[str, int]:
    project_id = console_api.playground_project_id(workspace_id)
    with db.connection() as con:
        daily = con.execute(
            "SELECT COALESCE(sum(input_tokens + output_tokens), 0) AS n FROM api_usage_daily "
            "WHERE project_id = %s",
            (project_id,),
        ).fetchone()["n"]
        minute = con.execute(
            "SELECT COALESCE(sum(input_tokens + output_tokens), 0) AS n FROM api_usage_minute "
            "WHERE project_id = %s",
            (project_id,),
        ).fetchone()["n"]
    return {"daily": int(daily), "minute": int(minute)}


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.usefixtures("limits_enforced")
def test_a_playground_run_refused_a_concurrency_slot_gives_its_input_estimate_back(
    admin, fake_model, monkeypatch, stream
):
    """Wave-3 re-verify: the refusal returned 429 but left the ~66k-token
    ESTIMATE `reserve` had charged in both ledgers, so a console user could
    burn the workspace's shared input TPM and daily quota without the engine
    ever running. `/v1` gives it back on the same path; so must this."""
    fake = fake_model()
    monkeypatch.setenv("API_PLAYGROUND_MAX_CONCURRENCY", "0")
    monkeypatch.setenv("API_PLAYGROUND_RPM", "1000")
    big = "word " * 4000

    response = admin.post(
        PLAYGROUND,
        json={"model": registry.PUBLIC_MODEL_IDS[0], "input": big, "stream": stream},
    )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "concurrency_limit_exceeded"
    assert fake.calls == []
    assert _playground_ledger(store.default_workspace()["id"]) == {"daily": 0, "minute": 0}


def test_a_streamed_playground_reservation_is_given_back_when_the_body_never_starts(admin):
    """A client gone before the first byte never starts the body generator, so
    `on_finish` never runs and nothing settles the reservation. The response
    object settles it (nothing ran, so the estimate comes back)."""
    import asyncio
    import contextlib

    workspace_id = store.default_workspace()["id"]
    row = console_api._ensure_playground_project(workspace_id)
    from app.apiplatform.resolver import ApiCaller, CallerLimits

    caller = ApiCaller(
        workspace_id=workspace_id, project_id=row["id"], service_account_id=None,
        key_id="playground_user_1", scopes=frozenset(), models=(), environment="live",
        limits=CallerLimits(rpm=100, input_tpm=100_000, output_tpm=100_000,
                            max_concurrency=5, daily_token_quota=1_000_000),
    )
    reservation = quotas.reserve(
        caller, kind="stream", estimated_input_tokens=5000, max_output_tokens=700
    )
    assert _playground_ledger(workspace_id)["daily"] == 5700
    stack = contextlib.ExitStack()
    stack.enter_context(quotas.concurrency_slot(caller, "stream"))
    slot = console_api._Slot(stack)

    async def frames():
        yield "data: never\n\n"

    async def gone_before_the_status_line(message):
        raise OSError("client went away")

    async def receive():
        return {"type": "http.disconnect"}

    response = console_api._SlotStreamingResponse(
        frames(), slot=slot, settle=console_api._Settlement(caller, reservation)
    )
    with contextlib.suppress(Exception):
        asyncio.run(response({"type": "http", "asgi": {"spec_version": "2.4"}},
                             receive, gone_before_the_status_line))

    assert quotas.in_flight(caller) == 0
    assert _playground_ledger(workspace_id) == {"daily": 0, "minute": 0}


def test_customer_projects_holding_every_predictable_playground_name_cannot_break_the_playground(
    admin, fake_model
):
    """Wave-3 re-verify: the allowance row was created under 'Console
    playground' or 'Console playground <last 8 of its id>' — and the id is a
    sha256 of the workspace id /overview returns — so an admin who took both
    names made every playground run in the workspace a 500."""
    fake_model()
    workspace_id = store.default_workspace()["id"]
    hidden = console_api.playground_project_id(workspace_id)
    _make_project(admin, console_api.PLAYGROUND_PROJECT_NAME)
    _make_project(admin, f"{console_api.PLAYGROUND_PROJECT_NAME} {hidden[-8:]}")

    response = admin.post(PLAYGROUND, json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"})

    assert response.status_code == 200, response.text
    names = sorted(p["name"] for p in admin.get("/admin/api/developers/projects").json()["projects"])
    assert names == sorted(
        [console_api.PLAYGROUND_PROJECT_NAME, f"{console_api.PLAYGROUND_PROJECT_NAME} {hidden[-8:]}"]
    )
    # Still identified by the id only this module mints, and still hidden.
    assert db.get_api_project(hidden, workspace_id) is not None
    assert admin.get(f"/admin/api/developers/projects/{hidden}").status_code == 404


def _checkouts_during(monkeypatch, call) -> int:
    """How many pooled-connection checkouts `call()` makes through
    `db.connection` — the door every accessor uses."""
    import contextlib

    real = db.connection
    count = {"n": 0}

    @contextlib.contextmanager
    def counting():
        count["n"] += 1
        with real() as con:
            yield con

    monkeypatch.setattr(db, "connection", counting)
    try:
        call()
    finally:
        monkeypatch.setattr(db, "connection", real)
    return count["n"]


def test_the_overview_and_usage_panels_cost_the_same_queries_for_one_project_or_many(
    admin, monkeypatch
):
    """Wave-3 re-verify: both panels looped `list_api_keys` and
    `read_usage_daily` once per project, so the landing page cost two queries
    per project and project creation is uncapped. One aggregate per panel: the
    checkout count must not move with the number of projects — and the numbers
    must still be right."""
    first = _make_project(admin, "p0")
    _make_key(admin, first["id"])
    db.bump_usage_daily(first["id"], requests=1, input_tokens=10, output_tokens=5)

    def load():
        assert admin.get("/admin/api/developers/overview").status_code == 200
        assert admin.get("/admin/api/developers/usage").status_code == 200

    load()  # warm the model-override and session paths once
    one = _checkouts_during(monkeypatch, load)

    for index in range(1, 7):
        project = _make_project(admin, f"p{index}")
        made = _make_key(admin, project["id"])
        db.bump_usage_daily(project["id"], requests=2, input_tokens=20, output_tokens=7, errors=1)
        if index == 3:
            admin.post(
                f"/admin/api/developers/projects/{project['id']}/keys/{made['key']['id']}/revoke"
            )
    admin.patch(f"/admin/api/developers/projects/{first['id']}", json={"status": "disabled"})
    many = _checkouts_during(monkeypatch, load)

    assert many == one, (one, many)
    stats = admin.get("/admin/api/developers/overview").json()["stats"]
    assert stats["projects"] == 7 and stats["active_projects"] == 6
    assert stats["keys"] == 7 and stats["active_keys"] == 6
    assert stats["today"]["requests"] == 13
    assert stats["today"]["input_tokens"] == 130
    assert stats["today"]["output_tokens"] == 47
    assert stats["today"]["errors"] == 6
    usage = admin.get("/admin/api/developers/usage").json()
    assert usage["totals"]["requests"] == 13 and usage["totals"]["total_tokens"] == 177
    per_project = {p["name"]: p for p in usage["projects"]}
    assert set(per_project) == {f"p{index}" for index in range(7)}
    assert per_project["p0"]["input_tokens"] == 10 and per_project["p5"]["errors"] == 1
    only = admin.get("/admin/api/developers/usage", params={"project_id": first["id"]}).json()
    assert [p["id"] for p in only["projects"]] == [first["id"]]
    assert only["totals"]["requests"] == 1



# ---------------------------------------------------------------------------
# Unlimited by default (owner decision, 2026-09-13)
# ---------------------------------------------------------------------------


def test_with_the_limits_off_the_limits_payload_says_unlimited_rather_than_a_number(
    admin, root
):
    """A console that shows `rpm: 60` beside an API that admits the 61st
    request shows a number nothing enforces. With the switch off the five
    usage limits are null beside `enforced: false`, on the project, the
    project list and the limits route; the two per-request token ceilings are
    technical limits and still read as stored."""
    project = _make_project(admin)
    root.put(
        f"/admin/api/developers/projects/{project['id']}/limits",
        json={"rpm": 120, "max_output_tokens": 2048},
    )

    read = admin.get(f"/admin/api/developers/projects/{project['id']}/limits").json()
    listed = admin.get("/admin/api/developers/projects").json()["projects"]
    mine = next(p for p in listed if p["id"] == project["id"])

    for limits in (read["limits"], mine["limits"]):
        assert limits["enforced"] is False
        for field in ("rpm", "input_tpm", "output_tpm", "max_concurrency", "daily_token_quota"):
            assert limits[field] is None, field
        assert limits["max_output_tokens"] == 2048
    assert read["limits_enforced"] is False


def test_with_the_limits_off_a_limit_change_is_audited_with_the_stored_numbers(root):
    """The audit trail records what a change moved from and to, whether or not
    anything enforces it — never a `from: null` invented by the display rule."""
    project = _make_project(root)

    response = root.put(
        f"/admin/api/developers/projects/{project['id']}/limits", json={"rpm": 120}
    )

    assert response.status_code == 200
    assert response.json()["limits"]["rpm"] is None
    changed = _audit_rows("api_limits_changed")[-1]["meta"]["changed"]
    assert changed == {"rpm": {"from": 60, "to": 120}}


def test_the_overview_says_whether_limits_are_enforced(admin, monkeypatch):
    assert admin.get("/admin/api/developers/overview").json()["stats"]["limits_enforced"] is False
    monkeypatch.setattr(settings, "public_api_enforce_limits", True)
    assert admin.get("/admin/api/developers/overview").json()["stats"]["limits_enforced"] is True


def test_with_the_limits_off_concurrent_playground_runs_beyond_the_allowance_all_run(
    admin, monkeypatch
):
    """The enforced test's twelve held-open runs against an allowance of two
    concurrent and five a minute: every one reaches the engine, every one is
    200 without a RateLimit field, and each is counted once in the
    allowance's ledger."""
    from app import llm

    fake = GatedModel()
    monkeypatch.setattr(llm, "stream_chat_events", fake.stream)
    monkeypatch.setattr(llm, "get_usage", fake.usage)
    monkeypatch.setenv("API_PLAYGROUND_MAX_CONCURRENCY", "2")
    monkeypatch.setenv("API_PLAYGROUND_RPM", "5")
    monkeypatch.setenv("API_PLAYGROUND_OUTPUT_TPM", "1")
    monkeypatch.setenv("API_PLAYGROUND_DAILY_TOKEN_QUOTA", "1")

    import threading
    import time as _time

    def release_when_every_run_is_in_the_engine():
        deadline = _time.monotonic() + 30
        while fake.started < 12 and _time.monotonic() < deadline:
            _time.sleep(0.02)
        fake.gate.set()

    releaser = threading.Thread(target=release_when_every_run_is_in_the_engine)
    releaser.start()
    try:
        results = _concurrently(
            12,
            lambda index: admin.post(
                PLAYGROUND, json={"model": registry.PUBLIC_MODEL_IDS[0], "input": f"q{index}"}
            ),
        )
    finally:
        fake.gate.set()
        releaser.join()

    assert [r.status_code for r in results] == [200] * 12, [r.status_code for r in results]
    for response in results:
        assert "RateLimit" not in response.headers
        assert "RateLimit-Policy" not in response.headers
    assert fake.started == 12
    workspace_id = store.default_workspace()["id"]
    with db.connection() as con:
        day = con.execute(
            "SELECT COALESCE(SUM(requests), 0) AS requests, "
            "       COALESCE(SUM(rate_limited), 0) AS rate_limited "
            "  FROM api_usage_daily WHERE project_id = %s",
            (console_api.playground_project_id(workspace_id),),
        ).fetchone()
    assert int(day["requests"]) == 12
    assert int(day["rate_limited"]) == 0


def test_with_the_limits_off_a_playground_allowance_of_zero_still_runs(
    admin, fake_model, monkeypatch
):
    """The enforced mode's "zero means no runs at all" is a usage limit, and
    the owner removed usage limits: with the switch off the run happens."""
    fake = fake_model()
    monkeypatch.setenv("API_PLAYGROUND_RPM", "0")
    monkeypatch.setenv("API_PLAYGROUND_MAX_CONCURRENCY", "0")

    response = admin.post(PLAYGROUND, json={"model": registry.PUBLIC_MODEL_IDS[0], "input": "hi"})

    assert response.status_code == 200, response.text
    assert "RateLimit" not in response.headers
    assert len(fake.calls) == 1
