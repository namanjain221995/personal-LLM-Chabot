"""Feature access (V17): which TOOLS a person may use.

Two halves, both pinned here. The RESOLUTION rules are pure functions
(`app/authn/features.py`) — layered defaults, dependencies that cannot
dangle, a super admin nobody can lock out. The GATE is what /chat and the
upload routes do with the answer, and the rule there is: the client is never
trusted, a removed tool is downgraded with a notice rather than erroring,
and bytes are refused outright at the door they actually arrive through.
"""
from __future__ import annotations

import pytest

from app import db
from app.authn import features as fa


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_a_fresh_workspace_grants_every_tool():
    assert fa.resolve(role="member") == {key: True for key in fa.IDS}


def test_layers_override_in_order():
    """Built-in < workspace default < member override."""
    resolved = fa.resolve(
        role="member",
        workspace_defaults={"salesforce": False, "web_search": False},
        member_overrides={"web_search": True},
    )
    assert resolved["salesforce"] is False  # workspace default stands
    assert resolved["web_search"] is True  # member override wins
    assert resolved["attachments"] is True  # untouched built-in


def test_a_dependency_cannot_dangle():
    """Live Salesforce without Salesforce, or deep research without the web,
    would be a toggle that cannot do anything."""
    resolved = fa.resolve(role="member", workspace_defaults={"salesforce": False})
    assert resolved["salesforce_live"] is False
    resolved = fa.resolve(role="member", member_overrides={"web_search": False})
    assert resolved["deep_research"] is False


def test_a_super_admin_cannot_be_locked_out():
    resolved = fa.resolve(
        role="super_admin",
        workspace_defaults={key: False for key in fa.IDS},
        member_overrides={key: False for key in fa.IDS},
    )
    assert all(resolved.values())


def test_unknown_and_non_boolean_keys_are_dropped_not_stored():
    assert fa.clean({"web_search": True, "nope": True, "salesforce": "yes"}) == {
        "web_search": True
    }
    assert fa.clean(None) == {} and fa.clean("nonsense") == {}


# ---------------------------------------------------------------------------
# The chat gate
# ---------------------------------------------------------------------------


def test_the_gate_downgrades_every_blocked_tool_and_names_them():
    gate = fa.enforce_chat(
        fa.resolve(role="member", workspace_defaults={key: False for key in fa.IDS}),
        mode="salesforce",
        web_search="on",
        deep_research=True,
        sf_live=True,
    )
    assert gate.mode == "assistant"
    assert gate.web_search == "off"
    assert gate.deep_research is False and gate.sf_live is False
    assert gate.changed
    notice = fa.blocked_notice(gate.blocked)
    assert "Salesforce" in notice and "Web search" in notice


def test_auto_search_is_blocked_too_not_just_a_forced_one():
    """"auto" would still send the question to a search provider."""
    gate = fa.enforce_chat(
        fa.resolve(role="member", workspace_defaults={"web_search": False}),
        mode="assistant",
        web_search="auto",
        deep_research=False,
        sf_live=False,
    )
    assert gate.web_search == "off" and gate.blocked


def test_live_salesforce_alone_can_be_removed_leaving_the_synced_copy():
    gate = fa.enforce_chat(
        fa.resolve(role="member", workspace_defaults={"salesforce_live": False}),
        mode="salesforce",
        web_search="off",
        deep_research=False,
        sf_live=True,
    )
    assert gate.mode == "salesforce"  # the synced copy still works
    assert gate.sf_live is False
    assert gate.blocked == ("Live Salesforce",)


def test_a_full_access_turn_is_untouched_and_says_nothing():
    gate = fa.enforce_chat(
        fa.resolve(role="member"),
        mode="salesforce",
        web_search="on",
        deep_research=True,
        sf_live=True,
    )
    assert not gate.changed and fa.blocked_notice(gate.blocked) == ""


# ---------------------------------------------------------------------------
# The admin surface + the enforced doors
# ---------------------------------------------------------------------------


def test_admin_sets_workspace_defaults_and_a_member_override(login_client):
    root = login_client("root", role="super_admin")
    login_client("bob")  # materialise the member
    bid = _uid("bob")

    catalog = root.get("/admin/api/access").json()
    assert [f["id"] for f in catalog["catalog"]] == list(fa.IDS)
    assert catalog["resolved"]["salesforce"] is True

    resp = root.put("/admin/api/access", json={"features": {"salesforce": False}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolved"]["salesforce"] is False
    # The dependency follows the parent down.
    assert resp.json()["resolved"]["salesforce_live"] is False

    member = root.get(f"/admin/api/members/{bid}/access").json()
    assert member["resolved"]["salesforce"] is False
    assert member["overrides"] == {}

    # One member gets it back.
    resp = root.put(
        f"/admin/api/members/{bid}/access", json={"features": {"salesforce": True}}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolved"]["salesforce"] is True

    events = root.get(
        "/admin/api/audit", params={"action": "member_access_changed"}
    ).json()["events"]
    assert len(events) == 1 and events[0]["target"]["id"] == bid


def test_a_member_cannot_read_or_change_access(login_client):
    bob = login_client("bob")
    assert bob.get("/admin/api/access").status_code == 404
    assert bob.put("/admin/api/access", json={"features": {}}).status_code == 404
    assert (
        bob.put(f"/admin/api/members/{_uid('bob')}/access", json={"features": {}}).status_code
        == 404
    )


def test_me_carries_the_resolved_features(login_client):
    root = login_client("root", role="super_admin")
    bob = login_client("bob")
    root.put("/admin/api/access", json={"features": {"deep_research": False}})

    me = bob.get("/auth/me").json()
    assert me["features"]["deep_research"] is False
    assert me["features"]["attachments"] is True
    # The super admin's own map is unconditional.
    assert root.get("/auth/me").json()["features"]["deep_research"] is True


def test_chat_downgrades_a_blocked_tool_and_says_so_once(login_client, monkeypatch):
    """The composer is not the gate: a request that still ASKS for Salesforce
    is answered as an ordinary assistant turn, with one status line — not a
    403 in the middle of a conversation."""
    import duckdb

    from app import llm
    from app.engines import router as router_engine

    root = login_client("root", role="super_admin")
    bob = login_client("bob")
    root.put(
        "/admin/api/access",
        json={"features": {"salesforce": False, "web_search": False}},
    )

    async def no_router(*a, **k):
        raise AssertionError("Salesforce routing must not run for a blocked account")

    def no_duckdb(*a, **k):
        raise AssertionError("the warehouse must not be touched")

    def fake_stream(*a, **k):
        async def gen():
            yield "token", "Answered without it."

        return gen()

    monkeypatch.setattr(router_engine, "route_request", no_router)
    monkeypatch.setattr(duckdb, "connect", no_duckdb)
    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)

    resp = bob.post(
        "/chat",
        json={
            "message": "what are my open opportunities?",
            "mode": "salesforce",
            "web_search": "on",
            "deep_research": True,
        },
    )
    assert resp.status_code == 200
    body = resp.text
    assert "Salesforce" in body and "turned off for your account" in body
    # One notice, not one per blocked tool.
    assert body.count("turned off for your account") == 1
    # And the answer still came.
    assert "Answered without it." in body


def test_uploads_are_refused_when_attachments_are_off(login_client, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    root = login_client("root", role="super_admin")
    bob = login_client("bob")
    root.put("/admin/api/access", json={"features": {"attachments": False}})

    resp = bob.post(
        "/uploads",
        files={"file": ("notes.csv", b"a,b\n1,2\n", "text/csv")},
        data={"conversation_id": "conv-gate", "purpose": "dataset"},
    )
    assert resp.status_code == 403
    assert "administrator" in resp.json()["detail"]

    # And the door stays open for everyone else.
    root.put("/admin/api/access", json={"features": {"attachments": True}})
    resp = bob.post(
        "/uploads",
        files={"file": ("notes.csv", b"a,b\n1,2\n", "text/csv")},
        data={"conversation_id": "conv-gate", "purpose": "dataset"},
    )
    assert resp.status_code == 200, resp.text
