"""Invitations — the only account-creation path (no public signup).

The token appears exactly once (in `accept_path`); the database keeps only
its hash. Expired, used, revoked and never-existed tokens are all the same
404. Acceptance is single-use and transactional: account + membership +
session in one step.
"""
from fastapi.testclient import TestClient

from app import db
from app.authn import store
from app.config import settings
from app.main import app

GOOD_PASSWORD = "a-long-enough-passphrase"


def _invite(client, email, *, role="member", name=""):
    """Create an invitation; returns (response body, the one-time token)."""
    resp = client.post(
        "/admin/api/invitations", json={"email": email, "name": name, "role": role}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    token = body["accept_path"].partition("token=")[2]
    assert token
    return body, token


def _accept(token, *, name="Newcomer", password=GOOD_PASSWORD):
    with TestClient(app) as c:
        resp = c.post(
            "/auth/invitations/accept",
            json={"token": token, "name": name, "password": password},
        )
    return resp


def test_create_returns_the_token_exactly_once(login_client):
    root = login_client("root", role="super_admin")
    body, token = _invite(root, "newcomer@example.com")

    assert body["accept_path"] == f"/accept-invite?token={token}"
    assert body["email"] == "newcomer@example.com"
    assert body["role"] == "member"

    # Neither the listing nor the database ever shows the token again.
    listing = root.get("/admin/api/invitations")
    assert listing.status_code == 200
    assert token not in listing.text
    with db.connection() as con:
        row = con.execute(
            "SELECT token_hash FROM workspace_invitations WHERE id = %s",
            (body["id"],),
        ).fetchone()
    assert row["token_hash"] != token


def test_invitation_info_feeds_the_accept_page(login_client):
    root = login_client("root", role="super_admin")
    _, token = _invite(root, "newcomer@example.com", name="New Colleague")

    with TestClient(app) as c:
        resp = c.get(f"/auth/invitations/{token}")
    assert resp.status_code == 200
    info = resp.json()
    assert info["email"] == "newcomer@example.com"
    assert info["name"] == "New Colleague"
    assert info["role"] == "member"
    assert info["workspace_name"] == settings.workspace_name
    assert info["expires_at"]


def test_accept_creates_account_membership_and_session(login_client):
    root = login_client("root", role="super_admin")
    _, token = _invite(root, "joiner@example.com")

    with TestClient(app) as c:
        resp = c.post(
            "/auth/invitations/accept",
            json={"token": token, "name": "Joiner", "password": GOOD_PASSWORD},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user"]["email"] == "joiner@example.com"
        assert body["user"]["name"] == "Joiner"
        assert body["workspace"]["role"] == "member"
        # Auto-login: the response set a cookie that resolves to a session.
        assert c.cookies.get(settings.auth_cookie_name)
        assert c.get("/auth/me").status_code == 200

    user = store.get_user_by_email("joiner@example.com")
    assert user["status"] == "active"
    assert user["password_hash"].startswith("$argon2id$")
    assert store.membership(int(user["id"]))["role"] == "member"

    # And the credentials work for an ordinary login afterwards.
    with TestClient(app) as fresh:
        relogin = fresh.post(
            "/auth/login",
            json={"email": "joiner@example.com", "password": GOOD_PASSWORD},
        )
    assert relogin.status_code == 200


def test_second_accept_of_the_same_token_404s(login_client):
    root = login_client("root", role="super_admin")
    _, token = _invite(root, "once@example.com")
    assert _accept(token).status_code == 200
    assert _accept(token).status_code == 404


def test_expired_invitation_404s(login_client):
    root = login_client("root", role="super_admin")
    body, token = _invite(root, "late@example.com")
    with db.connection() as con:
        con.execute(
            "UPDATE workspace_invitations SET expires_at = now() - interval '1 hour' "
            "WHERE id = %s",
            (body["id"],),
        )
    with TestClient(app) as c:
        assert c.get(f"/auth/invitations/{token}").status_code == 404
    assert _accept(token).status_code == 404


def test_revoked_invitation_404s(login_client):
    root = login_client("root", role="super_admin")
    body, token = _invite(root, "uninvited@example.com")
    revoke = root.post(f"/admin/api/invitations/{body['id']}/revoke")
    assert revoke.status_code == 200
    assert revoke.json() == {"ok": True}

    with TestClient(app) as c:
        assert c.get(f"/auth/invitations/{token}").status_code == 404
    assert _accept(token).status_code == 404


def test_reinvite_supersedes_the_earlier_pending_invitation(login_client):
    root = login_client("root", role="super_admin")
    first, token1 = _invite(root, "twice@example.com")
    second, token2 = _invite(root, "twice@example.com")

    # Exactly one live token per address: the first is dead, the second works.
    with TestClient(app) as c:
        assert c.get(f"/auth/invitations/{token1}").status_code == 404
        assert c.get(f"/auth/invitations/{token2}").status_code == 200
    rows = {
        i["id"]: i for i in root.get("/admin/api/invitations").json()["invitations"]
    }
    assert rows[first["id"]]["revoked_at"] is not None
    assert rows[second["id"]]["revoked_at"] is None
    assert _accept(token2).status_code == 200


def test_admin_invites_members_only_super_admin_invites_admins(login_client):
    admin = login_client("adm", role="admin")
    refused = admin.post(
        "/admin/api/invitations",
        json={"email": "peer@example.com", "name": "", "role": "admin"},
    )
    assert refused.status_code == 403
    _invite(admin, "colleague@example.com", role="member")  # allowed

    root = login_client("root", role="super_admin")
    _, token = _invite(root, "deputy@example.com", role="admin")
    accepted = _accept(token)
    assert accepted.status_code == 200
    assert accepted.json()["workspace"]["role"] == "admin"
    assert "members.read" in accepted.json()["capabilities"]


def test_weak_password_on_accept_is_422_and_the_invite_survives(login_client):
    root = login_client("root", role="super_admin")
    _, token = _invite(root, "careful@example.com")
    weak = _accept(token, password="short")
    assert weak.status_code == 422
    assert "characters" in weak.json()["detail"]
    # The token was not burned by the failed attempt.
    assert _accept(token).status_code == 200


def test_inviting_an_active_member_409s(login_client):
    root = login_client("root", role="super_admin")
    login_client("bob")  # bob@test.local is an active member
    resp = root.post(
        "/admin/api/invitations",
        json={"email": "bob@test.local", "name": "", "role": "member"},
    )
    assert resp.status_code == 409
