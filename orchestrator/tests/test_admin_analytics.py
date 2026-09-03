"""Workspace usage analytics, and the Pending-invites list that used to lie.

The analytics endpoint answers "who is using this, and which tools" over a
window. Two properties matter more than the totals: a member who used
NOTHING still appears (that is usually the question being asked), and the
window includes today (a report that ends at midnight reads as "nobody used
it today").
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import db


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def _seed(user: int, conversation: str, turns, *, days_ago: float = 0.0) -> None:
    """A conversation with (role, content, route) turns, stamped in the past."""
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.create_conversation(user, conversation, "seeded")
    with db.connection() as con:
        for role, content, route in turns:
            con.execute(
                """INSERT INTO messages (conversation_id, role, content, meta, created_at)
                   VALUES (%s, %s, %s, %s, %s)""",
                (
                    conversation,
                    role,
                    content,
                    db._json_param({"route": route} if route else None),
                    when,
                ),
            )


@pytest.fixture()
def workspace_with_usage(login_client):
    root = login_client("root", role="super_admin")
    login_client("bob")
    login_client("carol")
    _seed(
        _uid("bob"),
        "conv-usage-1",
        [
            ("user", "who is the ceo?", None),
            ("assistant", "…", "search"),
            ("user", "and the report?", None),
            ("assistant", "…", "deep_research"),
        ],
    )
    _seed(
        _uid("bob"),
        "conv-usage-old",
        [("user", "ancient", None), ("assistant", "…", "chat")],
        days_ago=200,
    )
    return root


def test_analytics_counts_tools_and_keeps_silent_members(workspace_with_usage):
    root = workspace_with_usage
    data = root.get("/admin/api/analytics", params={"range": "7d"}).json()

    assert data["range"]["key"] == "7d" and data["range"]["days"] == 7
    summary = data["summary"]
    assert summary["messages"] == 2 and summary["answers"] == 2
    assert summary["web_search"] == 1 and summary["deep_research"] == 1
    assert summary["tool_runs"] == 2
    assert summary["active_users"] == 1
    # Seats do not move with the window.
    assert summary["members"] >= 3

    by_name = {m["name"]: m for m in data["members"]}
    assert by_name["bob"]["messages"] == 2 and by_name["bob"]["web_search"] == 1
    # carol used nothing and is STILL listed — "who is not using it" is the
    # question this table is usually opened to answer.
    assert by_name["carol"]["messages"] == 0
    assert by_name["carol"]["tool_runs"] == 0


def test_a_longer_window_reaches_further_back(workspace_with_usage):
    root = workspace_with_usage
    week = root.get("/admin/api/analytics", params={"range": "7d"}).json()
    year = root.get("/admin/api/analytics", params={"range": "12m"}).json()
    assert week["summary"]["messages"] == 2
    assert year["summary"]["messages"] == 3  # the 200-day-old turn joins


def test_the_daily_series_covers_every_day_including_today(workspace_with_usage):
    data = workspace_with_usage.get(
        "/admin/api/analytics", params={"range": "7d"}
    ).json()
    days = [d["day"] for d in data["daily"]]
    assert len(days) == 7 and days == sorted(days)
    today = datetime.now(timezone.utc).date().isoformat()
    assert days[-1] == today
    assert sum(d["messages"] for d in data["daily"]) == 2


def test_export_is_csv_and_is_audited(workspace_with_usage):
    root = workspace_with_usage
    resp = root.get("/admin/api/analytics/export", params={"range": "7d"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in resp.headers["content-disposition"]
    body = resp.text.splitlines()
    assert body[0].startswith("name,email,role,status")
    assert any(line.startswith("bob,") for line in body[1:])

    events = root.get(
        "/admin/api/audit", params={"action": "analytics_exported"}
    ).json()["events"]
    assert len(events) == 1 and events[0]["meta"]["range"] == "7d"


def test_a_member_cannot_read_analytics(login_client):
    bob = login_client("bob")
    assert bob.get("/admin/api/analytics").status_code == 404
    assert bob.get("/admin/api/analytics/export").status_code == 404


# ---------------------------------------------------------------------------
# Pending invites
# ---------------------------------------------------------------------------


def test_the_pending_filter_returns_only_pending_invitations(login_client):
    """The Members tab says "Pending invites" and used to list every
    invitation ever sent — nine rows reading "Accepted" under a heading
    promising one pending (owner report, 2026-09-03)."""
    root = login_client("root", role="super_admin")
    for email in ("one@x.example", "two@x.example"):
        assert (
            root.post("/admin/api/invitations", json={"email": email, "role": "member"}).status_code
            == 200
        )
    invites = root.get("/admin/api/invitations").json()["invitations"]
    assert len(invites) == 2
    # By email, not by position: the list is newest-first.
    doomed = next(i for i in invites if i["email"] == "one@x.example")
    root.post(f"/admin/api/invitations/{doomed['id']}/revoke")

    pending = root.get(
        "/admin/api/invitations", params={"status": "pending"}
    ).json()["invitations"]
    assert [i["email"] for i in pending] == ["two@x.example"]

    revoked = root.get(
        "/admin/api/invitations", params={"status": "revoked"}
    ).json()["invitations"]
    assert [i["email"] for i in revoked] == ["one@x.example"]

    # No filter still returns the whole history, for the Invitations page.
    assert len(root.get("/admin/api/invitations").json()["invitations"]) == 2
