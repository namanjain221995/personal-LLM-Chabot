"""F028, the ACCEPTING side: an invitation cannot hand over an account its
inviter does not outrank, whenever that invitation was issued.

The issuing side (`admin_api.create_invitation`, 2026-09-13) refuses to CREATE
such an invitation. But every invitation already sitting in
`workspace_invitations` was issued under the old rule, and accepting one set a
new password on the existing account, reactivated it, rewrote its membership
role and signed the acceptor in as that person. These tests plant invitations
straight into the table — exactly the shape a pre-fix row has, bypassing the
issuing-side check — and drive acceptance over HTTP.

Beside each refusal the legitimate re-invitation still works, and now also
kills the claimed account's old sessions and leaves its role alone.
"""
import hashlib
import secrets
from datetime import timedelta

from fastapi.testclient import TestClient

from app import db
from app.authn import passwords, store
from app.config import settings
from app.main import app

GOOD_PASSWORD = "a-long-enough-passphrase"
OLD_PASSWORD = "correct-horse-battery"  # conftest login_client's default


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def _plant_invitation(email: str, *, invited_by: int, role: str = "member") -> tuple:
    """An invitation row written the way the table held it before the
    issuing-side fix: no rank check ever ran. Returns (row, one-time token)."""
    token = secrets.token_urlsafe(32)
    row = store.create_invitation(
        workspace_id=store.default_workspace()["id"],
        email=email,
        name="",
        role=role,
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        invited_by=invited_by,
        ttl=timedelta(days=7),
    )
    return row, token


def _accept(token: str, password: str = GOOD_PASSWORD):
    # Deliberately NOT `with TestClient(app)`: the lifespan is not what these
    # tests are about. That the lifespan's identity baseline no longer turns a
    # REMOVED account back into a member is pinned on its own, below, in
    # test_a_restart_does_not_resurrect_a_removed_account_as_a_member.
    return TestClient(app).post(
        "/auth/invitations/accept",
        json={"token": token, "name": "Claimer", "password": password},
    )


def _invitation(invitation_id: str) -> dict:
    return store.get_invitation(invitation_id)


def _live_sessions(user_id: int) -> list:
    with db.connection() as con:
        return con.execute(
            "SELECT id, revoke_reason FROM auth_sessions "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        ).fetchall()


def _audit_rows(action: str) -> list:
    with db.connection() as con:
        return con.execute(
            "SELECT actor_user_id, target_user_id, resource_id, meta FROM audit_events "
            "WHERE action = %s ORDER BY id",
            (action,),
        ).fetchall()


def _password_still(user_id: int, password: str) -> bool:
    return passwords.verify(store.get_user(user_id)["password_hash"], password)[0]


# ---------------------------------------------------------------------------
# Refusals — each would have been a successful takeover before this fix
# ---------------------------------------------------------------------------


def test_an_invitation_issued_before_the_fix_cannot_claim_a_disabled_super_admin(
    login_client,
):
    login_client("adm", role="admin")
    login_client("boss", role="super_admin")
    boss_id = _uid("boss")
    store.set_status(boss_id, "disabled")
    inv, token = _plant_invitation("boss@test.local", invited_by=_uid("adm"))

    resp = _accept(token)

    # The same 404 as any dead link — the public accept page learns nothing.
    assert resp.status_code == 404
    assert resp.json()["detail"] == "This invitation is no longer valid."
    assert not resp.cookies.get(settings.auth_cookie_name)
    # The account is exactly as it was: disabled, super admin, old password.
    boss = store.get_user(boss_id)
    assert boss["status"] == "disabled"
    assert store.membership(boss_id)["role"] == "super_admin"
    assert _password_still(boss_id, OLD_PASSWORD)
    assert not _password_still(boss_id, GOOD_PASSWORD)
    # Nothing was burned or bound to the target.
    assert _invitation(inv["id"])["accepted_at"] is None
    assert _invitation(inv["id"])["accepted_user_id"] is None
    # And the attempt is on the record, attributed to the inviter.
    rows = _audit_rows("invitation_claim_refused")
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == _uid("adm")
    assert rows[0]["target_user_id"] == boss_id
    assert rows[0]["resource_id"] == inv["id"]
    assert rows[0]["meta"]["reason"] == "outranked_account"
    assert rows[0]["meta"]["target_role"] == "super_admin"
    assert _audit_rows("invitation_accepted") == []


def test_an_invitation_issued_before_the_fix_cannot_claim_a_removed_account_for_an_admin(
    login_client,
):
    admin = login_client("adm", role="admin")
    login_client("leaver")
    leaver_id = _uid("leaver")
    assert admin.delete(f"/admin/api/members/{leaver_id}").status_code == 200
    _, token = _plant_invitation("leaver@test.local", invited_by=_uid("adm"))

    assert _accept(token).status_code == 404
    assert store.get_user(leaver_id)["status"] == "disabled"
    assert store.membership(leaver_id) is None
    assert _password_still(leaver_id, OLD_PASSWORD)
    rows = _audit_rows("invitation_claim_refused")
    assert [r["meta"]["reason"] for r in rows] == ["former_member_unknown_rank"]


def test_a_stale_invitation_cannot_reset_the_password_of_an_account_that_is_active_again(
    login_client,
):
    root = login_client("root", role="super_admin")
    bob = login_client("bob")
    bob_id = _uid("bob")
    # Invited while deactivated, reactivated by an admin before the link was
    # used: accepting it now would silently replace a working password.
    _, token = _plant_invitation("bob@test.local", invited_by=_uid("root"))

    assert _accept(token).status_code == 404
    assert _password_still(bob_id, OLD_PASSWORD)
    assert bob.get("/auth/me").status_code == 200  # bob's own session untouched
    # The everyday "already a member" case is not an attack: no audit row.
    assert _audit_rows("invitation_claim_refused") == []
    assert root.get("/auth/me").status_code == 200


def test_an_inviter_who_has_since_been_deactivated_can_no_longer_claim_an_account_but_their_invite_to_a_new_address_still_works(
    login_client,
):
    login_client("root", role="super_admin")
    login_client("returner")
    returner_id = _uid("returner")
    store.set_status(returner_id, "disabled")
    root_id = _uid("root")
    _, claim_token = _plant_invitation("returner@test.local", invited_by=root_id)
    _, fresh_token = _plant_invitation("brand-new@example.com", invited_by=root_id)
    # The super admin who issued both links is deactivated before they are used.
    store.set_status(root_id, "disabled")

    assert _accept(claim_token).status_code == 404
    assert store.get_user(returner_id)["status"] == "disabled"
    assert _password_still(returner_id, OLD_PASSWORD)
    # No account is claimed by a brand-new address, so the inviter's rank is moot.
    assert _accept(fresh_token).status_code == 200


# ---------------------------------------------------------------------------
# The legitimate re-invitation — still works, and is now safe
# ---------------------------------------------------------------------------


def test_a_legitimate_reinvitation_signs_the_person_back_in_revokes_their_old_sessions_and_keeps_their_role(
    login_client,
):
    login_client("root", role="super_admin")
    # A deactivated ADMIN with a browser still holding a session from before.
    old_browser = login_client("deputy", role="admin")
    deputy_id = _uid("deputy")
    assert len(_live_sessions(deputy_id)) == 1
    store.set_status(deputy_id, "disabled")
    # Re-invited at the invitation form's default role, 'member'.
    inv, token = _plant_invitation("deputy@test.local", invited_by=_uid("root"))

    resp = _accept(token)

    assert resp.status_code == 200, resp.text
    assert store.get_user(deputy_id)["status"] == "active"
    assert _password_still(deputy_id, GOOD_PASSWORD)
    assert _invitation(inv["id"])["accepted_user_id"] == deputy_id
    # The role is NOT rewritten by the invitation: that was the silent
    # super-admin-to-member demotion F028 described. Role changes go through
    # POST /admin/api/members/{id}/role and its own audit row.
    assert store.membership(deputy_id)["role"] == "admin"
    assert resp.json()["workspace"]["role"] == "admin"
    # Only the session the acceptance just minted is alive; the browser signed
    # in before deactivation did not wake up with the reactivated account.
    live = _live_sessions(deputy_id)
    assert len(live) == 1
    assert old_browser.get("/auth/me").status_code == 401
    with db.connection() as con:
        old = con.execute(
            "SELECT revoke_reason FROM auth_sessions WHERE user_id = %s "
            "AND revoked_at IS NOT NULL",
            (deputy_id,),
        ).fetchall()
    assert [r["revoke_reason"] for r in old] == [store.REVOKE_PASSWORD_RESET]


def test_a_super_admin_reinviting_a_removed_member_creates_the_membership_at_the_invited_role(
    login_client,
):
    root = login_client("root", role="super_admin")
    login_client("leaver")
    leaver_id = _uid("leaver")
    assert root.delete(f"/admin/api/members/{leaver_id}").status_code == 200
    _, token = _plant_invitation("leaver@test.local", invited_by=_uid("root"), role="admin")

    resp = _accept(token)
    assert resp.status_code == 200, resp.text
    # No membership existed, so DO NOTHING still inserts the invited role.
    assert store.membership(leaver_id)["role"] == "admin"
    assert store.get_user(leaver_id)["status"] == "active"


def test_a_restart_does_not_resurrect_a_removed_account_as_a_member(login_client):
    """F028, the restart door (2026-09-13). Removing a member deletes the
    membership and disables the account; the lifespan's identity baseline used
    to give every membership-less user a fresh 'member' row on each start, so
    a removed super admin came back as a disabled member — which an admin
    outranks, re-opening the takeover on both sides. An ACTIVE account that
    predates memberships must still be adopted."""
    from app.authn import bootstrap

    workspace_id = store.ensure_workspace(settings.workspace_name)["id"]
    removed = db.create_user("removed-former-admin", passwords.hash_password(GOOD_PASSWORD))
    store.upsert_membership(workspace_id, int(removed), "super_admin")
    store.remove_membership(workspace_id, int(removed))
    store.set_status(int(removed), "disabled")

    legacy = db.create_user("legacy-active-orphan", passwords.hash_password(GOOD_PASSWORD))

    bootstrap.ensure_identity_baseline()

    assert store.membership(int(removed)) is None, \
        "a removed (disabled, membership-less) account must stay removed across a restart"
    adopted = store.membership(int(legacy))
    assert adopted is not None and adopted["role"] == "member", \
        "an active account that predates memberships is still adopted as a member"
