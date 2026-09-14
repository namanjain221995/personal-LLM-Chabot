"""The audited admin content viewer + the audit log itself.

Reading a member's conversation through /admin/api is legitimate oversight —
but every such read MUST leave an audit event naming who read what. The audit
log is super-admin-only, filters by action, and paginates by keyset
(before_id), never by offset.

The LIST reads (conversations, uploads, reports, sessions) and the member
detail's usage counts are inspection too: each read of ANOTHER member writes
one event carrying paging and counts only, an identical repeat within
`READ_AUDIT_COALESCE_S` is folded into it, and reading yourself writes none.
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
            "WHERE action IN ('admin_viewed_conversation', 'admin_listed_conversations')"
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


LIST_ACTIONS = (
    "admin_listed_conversations",
    "admin_listed_uploads",
    "admin_listed_reports",
    "admin_listed_sessions",
    "admin_viewed_member_stats",
)


def _events(action):
    with db.connection() as con:
        return con.execute(
            "SELECT actor_user_id, target_user_id, resource_type, resource_id, meta "
            "FROM audit_events WHERE action = %s ORDER BY id",
            (action,),
        ).fetchall()


def test_list_and_stats_reads_of_another_member_are_audited_with_paging_and_counts_only(
    login_client, bob_with_chat
):
    root = login_client("root", role="super_admin")
    bid = _uid("bob")
    rid = _uid("root")

    listed = root.get(
        f"/admin/api/members/{bid}/conversations", params={"limit": 1, "offset": 0}
    )
    assert listed.json()["conversations"][0]["title"] == "Bob's chat"
    assert root.get(f"/admin/api/members/{bid}/uploads").status_code == 200
    assert root.get(f"/admin/api/members/{bid}/reports").status_code == 200
    assert len(root.get(f"/admin/api/members/{bid}/sessions").json()["sessions"]) == 1
    assert root.get(f"/admin/api/members/{bid}").json()["stats"] is not None

    expected_meta = {
        "admin_listed_conversations": {"offset": 0, "limit": 1, "returned": 1, "total": 1},
        "admin_listed_uploads": {"offset": 0, "limit": 50, "returned": 0, "total": 0},
        "admin_listed_reports": {"returned": 0},
        "admin_listed_sessions": {"returned": 1},
        "admin_viewed_member_stats": None,
    }
    for action in LIST_ACTIONS:
        rows = _events(action)
        assert len(rows) == 1, action
        assert rows[0]["actor_user_id"] == rid, action
        assert rows[0]["target_user_id"] == bid, action
        assert rows[0]["resource_type"] is None and rows[0]["resource_id"] is None
        assert rows[0]["meta"] == expected_meta[action], action
    # Never the content itself: no title reaches the log.
    with db.connection() as con:
        dump = con.execute(
            "SELECT string_agg(meta::text, ' ') AS m FROM audit_events"
        ).fetchone()["m"]
    assert "Bob's chat" not in (dump or "")

    # The super admin's audit page shows them like any other event.
    events = root.get(
        "/admin/api/audit", params={"action": "admin_listed_conversations"}
    ).json()["events"]
    assert [e["target"]["id"] for e in events] == [bid]
    assert events[0]["meta"] == expected_meta["admin_listed_conversations"]


def test_repeating_the_same_list_read_is_folded_but_another_page_is_not(
    login_client, bob_with_chat
):
    root = login_client("root", role="super_admin")
    bid = _uid("bob")
    page = f"/admin/api/members/{bid}/conversations"

    # A re-render / retry / tab switch back: same page, same result.
    for _ in range(5):
        assert root.get(page, params={"limit": 1, "offset": 0}).status_code == 200
        assert root.get(f"/admin/api/members/{bid}").status_code == 200
    assert len(_events("admin_listed_conversations")) == 1
    assert len(_events("admin_viewed_member_stats")) == 1

    # Paging on is a different read and always leaves its own row.
    assert root.get(page, params={"limit": 1, "offset": 1}).status_code == 200
    metas = [r["meta"] for r in _events("admin_listed_conversations")]
    assert metas == [
        {"offset": 0, "limit": 1, "returned": 1, "total": 1},
        {"offset": 1, "limit": 1, "returned": 0, "total": 1},
    ]

    # A changed result is a different read too: Bob starts a second chat.
    assert bob_with_chat.post(
        "/history/conversations", json={"id": "conv-b2", "title": "Second"}
    ).status_code == 200
    assert root.get(page, params={"limit": 1, "offset": 0}).status_code == 200
    assert len(_events("admin_listed_conversations")) == 3

    # Once the window has passed, the identical read is recorded again.
    with db.connection() as con:
        con.execute(
            "UPDATE audit_events SET created_at = created_at - interval '10 minutes' "
            "WHERE action IN ('admin_listed_conversations', 'admin_viewed_member_stats')"
        )
    assert root.get(page, params={"limit": 1, "offset": 0}).status_code == 200
    assert root.get(f"/admin/api/members/{bid}").status_code == 200
    assert len(_events("admin_listed_conversations")) == 4
    assert len(_events("admin_viewed_member_stats")) == 2

    # Single-item reads are never folded: each open is its own event.
    for _ in range(2):
        assert root.get(f"{page}/conv-b").status_code == 200
    assert len(_events("admin_viewed_conversation")) == 2


def test_reading_your_own_lists_and_stats_writes_no_event(login_client):
    root = login_client("root", role="super_admin")
    adm = login_client("adm", role="admin")
    for client, name in ((root, "root"), (adm, "adm")):
        uid = _uid(name)
        for suffix in ("/conversations", "/uploads", "/reports", "/sessions", ""):
            resp = client.get(f"/admin/api/members/{uid}{suffix}")
            assert resp.status_code == 200, (name, suffix)
    for action in LIST_ACTIONS:
        assert _events(action) == [], action


def test_coalescing_look_back_is_bounded_and_handles_null_keys(login_client):
    """The fold looks at the actor's newest AUDIT_COALESCE_LOOKBACK_ROWS rows
    only (an index walk, never the actor's whole history), treats NULL
    workspace/target/meta as equal keys, and past the bound it over-records
    rather than losing the read."""
    from app.authn import store

    login_client("root", role="super_admin")
    rid = _uid("root")

    def write():
        return store.record_audit(
            workspace_id=None,
            actor_user_id=rid,
            action="probe_fold",
            target_user_id=None,
            meta=None,
            coalesce_seconds=60,
        )

    assert write() is True
    assert write() is False
    assert write() is False
    assert len(_events("probe_fold")) == 1

    for i in range(store.AUDIT_COALESCE_LOOKBACK_ROWS):
        store.record_audit(
            workspace_id=None, actor_user_id=rid, action="probe_noise", meta={"i": i}
        )
    # The first event is now beyond the look-back: the repeat is recorded.
    assert write() is True
    assert len(_events("probe_fold")) == 2
