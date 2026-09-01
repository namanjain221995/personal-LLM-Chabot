"""RBAC on the admin surface (/admin/api/*).

Two properties under test, both via REAL logged-in sessions:

1. Capability gating answers 404 — never 403 — so the admin surface does not
   confirm its own existence to a member probing URLs.
2. Rank rules: an admin never manages an equal-or-higher role, and the
   workspace can never lose its last active super admin.
"""
from app import db
from app.authn import store
from app.config import settings


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


# ---------------------------------------------------------------------------
# Capability gating: who sees which endpoints
# ---------------------------------------------------------------------------


def test_member_sees_no_admin_surface_at_all(login_client):
    member = login_client("worker")
    assert member.get("/admin/api/overview").status_code == 404
    assert member.get("/admin/api/members").status_code == 404
    assert member.get("/admin/api/audit").status_code == 404


def test_admin_reads_members_but_not_audit_or_roles(login_client):
    admin = login_client("adm", role="admin")

    members = admin.get("/admin/api/members")
    assert members.status_code == 200
    assert any(m["email"] == "adm@test.local" for m in members.json()["members"])
    assert admin.get("/admin/api/overview").status_code == 200

    # No audit.read and no roles.manage → the endpoints do not exist for them.
    assert admin.get("/admin/api/audit").status_code == 404
    role_change = admin.post(
        f"/admin/api/members/{_uid('adm')}/role", json={"role": "member"}
    )
    assert role_change.status_code == 404


def test_super_admin_reaches_the_whole_surface(login_client):
    root = login_client("root", role="super_admin")

    overview = root.get("/admin/api/overview")
    assert overview.status_code == 200
    assert overview.json()["workspace"]["name"] == settings.workspace_name
    assert overview.json()["stats"]["active_members"] >= 1

    members = root.get("/admin/api/members")
    assert members.status_code == 200
    assert members.json()["total"] >= 1

    audit = root.get("/admin/api/audit")
    assert audit.status_code == 200
    assert any(e["action"] == "login_success" for e in audit.json()["events"])


# ---------------------------------------------------------------------------
# Rank rules: managing members
# ---------------------------------------------------------------------------


def test_admin_cannot_deactivate_or_remove_equal_or_higher_roles(login_client, as_user):
    admin = login_client("adm", role="admin")
    peer = as_user("peer", role="admin")
    boss = as_user("boss", role="super_admin")

    for target in (int(peer["id"]), int(boss["id"])):
        disable = admin.post(
            f"/admin/api/members/{target}/status", json={"disabled": True}
        )
        assert disable.status_code == 403
        assert admin.delete(f"/admin/api/members/{target}").status_code == 403

    # Sanity: the same admin CAN manage a plain member.
    member = as_user("worker")
    resp = admin.post(
        f"/admin/api/members/{int(member['id'])}/status", json={"disabled": True}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "status": "disabled"}


def test_disabling_a_member_kills_their_live_sessions(login_client):
    admin = login_client("adm", role="admin")
    victim = login_client("victim")
    assert victim.get("/auth/me").status_code == 200

    resp = admin.post(
        f"/admin/api/members/{_uid('victim')}/status", json={"disabled": True}
    )
    assert resp.status_code == 200
    # Deactivation is immediate: the live session dies with it, and a fresh
    # login gets the same generic 401 as a wrong password.
    assert victim.get("/auth/me").status_code == 401
    relogin = victim.post(
        "/auth/login",
        json={"email": "victim@test.local", "password": "correct-horse-battery"},
    )
    assert relogin.status_code == 401
    assert relogin.json() == {"detail": "Incorrect email or password."}


def test_the_last_active_super_admin_is_protected(login_client):
    """Demote/disable/remove the ONLY active super admin → 409 every time.

    Demotion trips the last-super-admin guard; disable and remove trip the
    self-guards — the actor holding the required capability IS the last super
    admin, and nobody else outranks them. Either way the workspace keeps its
    super admin, and the status code is the same 409.
    """
    root = login_client("root", role="super_admin")
    rid = _uid("root")

    demote = root.post(f"/admin/api/members/{rid}/role", json={"role": "member"})
    assert demote.status_code == 409

    disable = root.post(f"/admin/api/members/{rid}/status", json={"disabled": True})
    assert disable.status_code == 409

    remove = root.delete(f"/admin/api/members/{rid}")
    assert remove.status_code == 409

    # Still standing, still super_admin.
    detail = root.get(f"/admin/api/members/{rid}").json()["member"]
    assert detail["role"] == "super_admin"
    assert detail["status"] == "active"


def test_demotion_works_once_a_second_super_admin_exists(login_client, as_user):
    root = login_client("root", role="super_admin")
    second = as_user("successor", role="super_admin")

    resp = root.post(
        f"/admin/api/members/{int(second['id'])}/role", json={"role": "admin"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "role": "admin"}
    assert store.membership(int(second["id"]))["role"] == "admin"

    # And with the successor demoted, root is the last one again.
    self_demote = root.post(
        f"/admin/api/members/{_uid('root')}/role", json={"role": "admin"}
    )
    assert self_demote.status_code == 409
