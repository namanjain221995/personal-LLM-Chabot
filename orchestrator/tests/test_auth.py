"""Real login is BACK (2026-09-01) — the /auth surface end to end.

This file used to pin the single-local-account era (no login endpoints, an
ambient account, ignored cookies). That era is over: opaque server-side
sessions ride an HttpOnly `ts_session` cookie, passwords are Argon2id, and
every failure an unauthenticated caller can see is deliberately generic.

Everything here goes through the REAL session machinery: `login_client`
performs an actual HTTP login and returns a cookie-carrying client, so
resolution, revocation, rolling renewal and throttling are exercised exactly
as production runs them. `anonymous_mode` turns off the ambient test shim for
the signed-out assertions.
"""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app import db
from app.authn import store
from app.config import settings
from app.main import app

PASSWORD = "correct-horse-battery"  # login_client's default


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def _sid(client: TestClient) -> str:
    """The server-side session id half of the client's cookie."""
    return client.cookies.get(settings.auth_cookie_name).partition(".")[0]


def _login(client: TestClient, email: str, password: str):
    return client.post("/auth/login", json={"email": email, "password": password})


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def test_login_sets_httponly_lax_cookie_and_returns_me_payload(login_client):
    login_client("alice")  # materializes the account with a real password
    with TestClient(app) as fresh:
        resp = _login(fresh, "alice@test.local", PASSWORD)
        assert resp.status_code == 200

        cookie = resp.headers["set-cookie"].lower()
        assert settings.auth_cookie_name.lower() + "=" in cookie
        assert "httponly" in cookie
        assert "samesite=lax" in cookie
        assert "path=/" in cookie

        body = resp.json()
        assert body["username"] == "alice"  # legacy key, still served
        assert body["user"] == {
            "id": _uid("alice"),
            "name": "alice",
            "email": "alice@test.local",
        }
        assert body["workspace"]["role"] == "member"
        assert body["workspace"]["name"] == settings.workspace_name
        assert body["capabilities"] == []  # members hold no admin caps

        # The cookie is a live session: /auth/me answers with the same shape.
        me = fresh.get("/auth/me")
        assert me.status_code == 200
        assert me.json() == body


def test_login_failures_are_indistinguishable(login_client):
    """Wrong password, unknown email and disabled account answer the SAME
    generic 401 — the login form is not an account-existence oracle."""
    login_client("bob")
    login_client("sleepy")
    store.set_status(_uid("sleepy"), "disabled")

    with TestClient(app) as c:
        wrong_password = _login(c, "bob@test.local", "not-the-password")
        unknown_email = _login(c, "nobody@test.local", PASSWORD)
        disabled_user = _login(c, "sleepy@test.local", PASSWORD)

    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401
    assert disabled_user.status_code == 401
    assert (
        wrong_password.json()
        == unknown_email.json()
        == disabled_user.json()
        == {"detail": "Incorrect email or password."}
    )


def test_login_failure_lands_in_the_audit_trail(login_client):
    login_client("bob")
    with TestClient(app) as c:
        assert _login(c, "bob@test.local", "not-the-password").status_code == 401

    with db.connection() as con:
        rows = con.execute(
            "SELECT meta, target_user_id FROM audit_events WHERE action = 'login_failure'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["meta"]["email"] == "bob@test.local"
    assert rows[0]["target_user_id"] == _uid("bob")


def test_repeated_failures_throttle_to_429(login_client):
    login_client("marge")
    with TestClient(app) as c:
        for _ in range(settings.auth_login_max_fails):
            assert _login(c, "marge@test.local", "wrong-every-time").status_code == 401
        # Locked now — even the CORRECT password is refused until the lock
        # expires, and the answer names no reason beyond "too many attempts".
        locked = _login(c, "marge@test.local", PASSWORD)
    assert locked.status_code == 429
    assert "Too many attempts" in locked.json()["detail"]


# ---------------------------------------------------------------------------
# Logout + session lifetime
# ---------------------------------------------------------------------------


def test_logout_revokes_the_session_server_side(login_client):
    client = login_client("carol")
    old_cookie = client.cookies.get(settings.auth_cookie_name)
    assert client.get("/auth/me").status_code == 200

    resp = client.post("/auth/logout")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # Replaying the old cookie must fail: revocation is server-side state,
    # not merely the browser forgetting the cookie.
    client.cookies.set(settings.auth_cookie_name, old_cookie)
    assert client.get("/auth/me").status_code == 401


def test_logout_is_safe_when_signed_out(anonymous_mode):
    with TestClient(app) as c:
        resp = c.post("/auth/logout")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_expired_session_is_rejected(login_client):
    client = login_client("henry")
    with db.connection() as con:
        con.execute(
            "UPDATE auth_sessions SET expires_at = now() - interval '1 hour' "
            "WHERE id = %s",
            (_sid(client),),
        )
    assert client.get("/auth/me").status_code == 401


def test_revoked_session_reuse_is_rejected(login_client):
    client = login_client("ivan")
    assert store.revoke_session(_sid(client)) is True
    assert client.get("/auth/me").status_code == 401


def test_rolling_renewal_pushes_expiry_forward(login_client):
    """A request on a session quiet for >5 minutes rolls expires_at forward
    (persistent login) and stamps last_seen_at."""
    client = login_client("iris")
    sid = _sid(client)
    with db.connection() as con:
        con.execute(
            "UPDATE auth_sessions SET last_seen_at = now() - interval '10 minutes', "
            "expires_at = now() + interval '1 day' WHERE id = %s",
            (sid,),
        )
        before = con.execute(
            "SELECT expires_at FROM auth_sessions WHERE id = %s", (sid,)
        ).fetchone()["expires_at"]

    assert client.get("/auth/me").status_code == 200

    with db.connection() as con:
        row = con.execute(
            "SELECT expires_at, last_seen_at, absolute_expires_at "
            "FROM auth_sessions WHERE id = %s",
            (sid,),
        ).fetchone()
    assert row["expires_at"] > before
    assert row["expires_at"] <= row["absolute_expires_at"]  # never past ceiling
    assert row["last_seen_at"] > datetime.now(timezone.utc) - timedelta(seconds=60)


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


def test_password_change_rules_and_other_session_revocation(login_client):
    client = login_client("dave")
    other = login_client("dave")  # a second live session, same account

    wrong_current = client.post(
        "/auth/password",
        json={"current_password": "not-it-at-all", "new_password": "a-perfectly-fine-one"},
    )
    assert wrong_current.status_code == 403

    weak_new = client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": "short"},
    )
    assert weak_new.status_code == 422
    assert "characters" in weak_new.json()["detail"]

    assert other.get("/auth/me").status_code == 200
    resp = client.post(
        "/auth/password",
        json={"current_password": PASSWORD, "new_password": "a-brand-new-passphrase"},
    )
    assert resp.status_code == 200
    # Everyone ELSE holding this account is signed out; this session stays.
    assert client.get("/auth/me").status_code == 200
    assert other.get("/auth/me").status_code == 401

    # Only the new password logs in now.
    with TestClient(app) as fresh:
        assert _login(fresh, "dave@test.local", PASSWORD).status_code == 401
        assert _login(fresh, "dave@test.local", "a-brand-new-passphrase").status_code == 200


# ---------------------------------------------------------------------------
# Session listing + targeted revocation
# ---------------------------------------------------------------------------


def test_sessions_list_marks_current_and_revokes_one(login_client):
    first = login_client("erin")
    second = login_client("erin")

    sessions = second.get("/auth/sessions").json()["sessions"]
    assert len(sessions) == 2
    current = [s for s in sessions if s["current"]]
    assert len(current) == 1
    assert current[0]["id"] == _sid(second)

    # A session id that is not yours (or does not exist) is a 404.
    made_up = second.post("/auth/sessions/revoke", json={"session_id": "nope"})
    assert made_up.status_code == 404

    other_id = next(s["id"] for s in sessions if not s["current"])
    resp = second.post("/auth/sessions/revoke", json={"session_id": other_id})
    assert resp.json() == {"revoked": 1}
    assert first.get("/auth/me").status_code == 401
    assert second.get("/auth/me").status_code == 200


def test_revoke_others_spares_only_the_current_session(login_client):
    a1 = login_client("gina")
    a2 = login_client("gina")
    a3 = login_client("gina")

    resp = a3.post("/auth/sessions/revoke", json={"others": True})
    assert resp.json() == {"revoked": 2}
    assert a1.get("/auth/me").status_code == 401
    assert a2.get("/auth/me").status_code == 401
    assert a3.get("/auth/me").status_code == 200


def test_cannot_revoke_another_users_session(login_client):
    alice = login_client("alice")
    bob = login_client("bob")
    resp = alice.post("/auth/sessions/revoke", json={"session_id": _sid(bob)})
    assert resp.status_code == 404
    assert bob.get("/auth/me").status_code == 200


# ---------------------------------------------------------------------------
# Stored credentials
# ---------------------------------------------------------------------------


def test_stored_hash_is_argon2id_never_plaintext(login_client):
    login_client("jane")
    stored = db.get_user_by_username("jane")["password_hash"]
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


def test_unusable_hash_can_never_log_in(as_user):
    """Accounts with the '!' sentinel (pre-bootstrap local, un-accepted
    invitee) fail every login — including the stored sentinel itself."""
    as_user("ghost")  # materialized with an unusable "!"-prefixed hash
    store.set_credentials(_uid("ghost"), email="ghost@test.local")
    assert db.get_user_by_username("ghost")["password_hash"].startswith("!")

    with TestClient(app) as c:
        for candidate in ("!test-ambient", "!", "anything-at-all"):
            assert _login(c, "ghost@test.local", candidate).status_code == 401


# ---------------------------------------------------------------------------
# Public vs. authenticated surface
# ---------------------------------------------------------------------------


def test_reports_require_auth(anonymous_mode):
    with TestClient(app) as c:
        assert c.get("/reports").status_code == 401
        assert c.get("/reports/q3.pdf").status_code == 401


def test_health_stays_public(anonymous_mode, monkeypatch):
    from app import main as app_main

    async def fake_checks():
        return {"status": "ok", "checks": {}}

    monkeypatch.setattr(app_main, "check_dependencies", fake_checks)
    with TestClient(app) as c:
        resp = c.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Why a session ended (2026-09-03): the browser that held the cookie is told
# ---------------------------------------------------------------------------


def _me_401(client: TestClient):
    resp = client.get("/auth/me")
    assert resp.status_code == 401
    return resp, resp.json()["detail"]


def test_a_removed_member_is_told_and_given_admin_contacts(login_client):
    """Remove Carol as the super admin; Carol's next /auth/me must SAY so —
    code, plain sentence, workspace and who to contact — and clear the dead
    cookie so the edge gate stops bouncing her into an app that only 401s."""
    admin = login_client("root", role="super_admin")
    carol = login_client("carol")
    cid = _uid("carol")
    assert admin.delete(f"/admin/api/members/{cid}").status_code == 200

    resp, body = _me_401(carol)
    assert body["code"] == "account_removed"
    assert "removed" in body["detail"].lower()
    assert body["workspace"] == settings.workspace_name
    emails = [c["email"] for c in body["contact"]]
    assert "root@test.local" in emails
    assert "carol@test.local" not in emails
    assert body["ended_at"]
    cleared = resp.headers.get("set-cookie", "").lower()
    assert settings.auth_cookie_name.lower() + "=" in cleared and "max-age=0" in cleared


def test_a_deactivated_member_is_told_it_was_deactivated(login_client):
    admin = login_client("root", role="super_admin")
    dave = login_client("dave")
    did = _uid("dave")
    assert admin.post(f"/admin/api/members/{did}/status", json={"disabled": True}).status_code == 200
    _resp, body = _me_401(dave)
    assert body["code"] == "account_disabled"
    assert "deactivated" in body["detail"].lower()
    assert body["contact"], "a deactivated member is told who can reactivate them"


def test_logout_and_expiry_are_distinguished_from_removal(login_client):
    eve = login_client("eve")
    dead = eve.cookies.get(settings.auth_cookie_name)
    eve.post("/auth/logout")
    # Logout clears the client's cookie; put the dead one back to simulate
    # another tab that still carries it.
    eve.cookies.set(settings.auth_cookie_name, dead)
    _resp, body = _me_401(eve)
    assert body["code"] == "session_revoked"
    assert "contact" not in body

    frank = login_client("frank")
    sid = _sid(frank)
    with db.connection() as con:
        con.execute(
            "UPDATE auth_sessions SET expires_at = now() - interval '1 minute' WHERE id = %s",
            (sid,),
        )
    _resp, body = _me_401(frank)
    assert body["code"] == "session_expired"
    assert "contact" not in body


def test_a_forged_or_absent_cookie_learns_nothing(anonymous_mode):
    with TestClient(app) as c:
        resp = c.get("/auth/me")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Sign in required."
        c.cookies.set(settings.auth_cookie_name, "0" * 32 + ".not-a-real-secret")
        resp = c.get("/auth/me")
        assert resp.status_code == 401
        body = resp.json()["detail"]
        assert body["code"] == "signed_out"
        assert "contact" not in body and "workspace" not in body


def test_login_stays_generic_for_a_removed_member(login_client):
    """The removed page knows because the COOKIE proved who they were; the
    login form has no such proof and must not become an oracle."""
    admin = login_client("root", role="super_admin")
    login_client("gone")
    gid = _uid("gone")
    assert admin.delete(f"/admin/api/members/{gid}").status_code == 200
    with TestClient(app) as fresh:
        resp = _login(fresh, "gone@test.local", PASSWORD)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Incorrect email or password."


def test_revoke_reasons_are_recorded(login_client):
    grace = login_client("grace")
    sid = _sid(grace)
    grace.post("/auth/logout")
    with db.connection() as con:
        row = con.execute("SELECT revoke_reason FROM auth_sessions WHERE id = %s", (sid,)).fetchone()
    assert row["revoke_reason"] == store.REVOKE_LOGOUT
