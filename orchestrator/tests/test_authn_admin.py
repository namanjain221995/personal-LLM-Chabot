"""The audited admin content viewer + the audit log itself.

Reading a member's conversation through /admin/api is legitimate oversight —
but every such read MUST leave an audit event naming who read what. The audit
log is super-admin-only, filters by action, and paginates by keyset
(before_id), never by offset.
"""
import pytest

from app import db


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


@pytest.fixture()
def bob_with_chat(login_client):
    """A member with one conversation of two messages."""
    bob = login_client("bob")
    resp = bob.post(
        "/history/conversations", json={"id": "conv-b", "title": "Bob's chat"}
    )
    assert resp.status_code == 200, resp.text
    for role, content in (
        ("user", "quarterly numbers?"),
        ("assistant", "Here they are."),
    ):
        resp = bob.post(
            "/history/conversations/conv-b/messages",
            json={"role": role, "content": content},
        )
        assert resp.status_code == 200, resp.text
    return bob


def test_super_admin_conversation_view_returns_messages_and_audits(
    login_client, bob_with_chat
):
    root = login_client("root", role="super_admin")
    bid = _uid("bob")

    resp = root.get(f"/admin/api/members/{bid}/conversations/conv-b")
    assert resp.status_code == 200
    data = resp.json()
    assert data["conversation"]["id"] == "conv-b"
    assert data["conversation"]["title"] == "Bob's chat"
    assert [(m["role"], m["content"]) for m in data["messages"]] == [
        ("user", "quarterly numbers?"),
        ("assistant", "Here they are."),
    ]
    # The contract shape: every message carries id + a real timestamp.
    assert all(m["id"] and m["created_at"] for m in data["messages"])

    # The read left a trail: actor, target and resource all named.
    events = root.get(
        "/admin/api/audit", params={"action": "admin_viewed_conversation"}
    ).json()["events"]
    assert len(events) == 1
    event = events[0]
    assert event["actor"]["id"] == _uid("root")
    assert event["actor"]["email"] == "root@test.local"
    assert event["target"]["id"] == bid
    assert event["resource_type"] == "conversation"
    assert event["resource_id"] == "conv-b"


def test_plain_admin_can_view_member_conversations_and_is_audited(
    login_client, bob_with_chat
):
    admin = login_client("adm", role="admin")
    bid = _uid("bob")

    resp = admin.get(f"/admin/api/members/{bid}/conversations/conv-b")
    assert resp.status_code == 200
    assert len(resp.json()["messages"]) == 2

    # The admin cannot read the audit log, but the event exists all the same.
    assert admin.get("/admin/api/audit").status_code == 404
    with db.connection() as con:
        rows = con.execute(
            "SELECT actor_user_id, target_user_id, resource_id FROM audit_events "
            "WHERE action = 'admin_viewed_conversation'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == _uid("adm")
    assert rows[0]["target_user_id"] == bid
    assert rows[0]["resource_id"] == "conv-b"


def test_member_cannot_use_the_admin_viewer(login_client, bob_with_chat):
    snoop = login_client("snoop")
    bid = _uid("bob")
    assert snoop.get(f"/admin/api/members/{bid}/conversations").status_code == 404
    assert (
        snoop.get(f"/admin/api/members/{bid}/conversations/conv-b").status_code == 404
    )
    # And no audit event pretends a view happened.
    with db.connection() as con:
        row = con.execute(
            "SELECT count(*) AS n FROM audit_events "
            "WHERE action = 'admin_viewed_conversation'"
        ).fetchone()
    assert row["n"] == 0


def test_audit_filters_by_action_and_paginates_by_before_id(
    login_client, bob_with_chat
):
    root = login_client("root", role="super_admin")
    bid = _uid("bob")
    for _ in range(3):  # three separate audited reads
        assert (
            root.get(f"/admin/api/members/{bid}/conversations/conv-b").status_code
            == 200
        )

    page1 = root.get(
        "/admin/api/audit",
        params={"action": "admin_viewed_conversation", "limit": 2},
    ).json()
    assert [e["action"] for e in page1["events"]] == ["admin_viewed_conversation"] * 2
    assert page1["next_before_id"] == page1["events"][-1]["id"]

    page2 = root.get(
        "/admin/api/audit",
        params={
            "action": "admin_viewed_conversation",
            "limit": 2,
            "before_id": page1["next_before_id"],
        },
    ).json()
    assert [e["action"] for e in page2["events"]] == ["admin_viewed_conversation"]

    ids = [e["id"] for e in page1["events"] + page2["events"]]
    assert ids == sorted(ids, reverse=True)  # newest first, keyset-ordered
    assert len(set(ids)) == 3  # no overlap between pages

    # The filter really filters: the unfiltered log holds other actions too.
    unfiltered = root.get("/admin/api/audit").json()["events"]
    assert any(e["action"] == "login_success" for e in unfiltered)


def test_invitation_creation_is_audited(login_client):
    root = login_client("root", role="super_admin")
    created = root.post(
        "/admin/api/invitations",
        json={"email": "fresh@example.com", "name": "Fresh", "role": "member"},
    )
    assert created.status_code == 200

    events = root.get(
        "/admin/api/audit", params={"action": "user_invited"}
    ).json()["events"]
    assert len(events) == 1
    event = events[0]
    assert event["actor"]["id"] == _uid("root")
    assert event["resource_type"] == "invitation"
    assert event["resource_id"] == created.json()["id"]
    assert event["meta"] == {"email": "fresh@example.com", "role": "member"}
