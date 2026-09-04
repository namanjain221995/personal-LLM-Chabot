"""Conversation sharing.

Four properties, and the tests are grouped by them:

1. ONLY THE OWNER DECIDES. Hiding the button is not a control; every create,
   update and revoke is checked server-side, and a stranger gets 404 rather
   than 403 so they do not learn the conversation exists.

2. WHAT IS PUBLISHED IS AN ALLOWLIST. The snapshot is built by naming what
   goes in, so a meta key added next month is private by default. The test
   here asserts that a published message carries ONLY the named keys — which
   is how it catches leaks nobody has thought of yet.

3. PROVENANCE DECIDES, NOT PROSE. A Salesforce answer is blocked because it
   came from Salesforce — a fact recorded when it was generated — not because
   a model judged the text sensitive.

4. THE PUBLIC ROUTE TELLS A STRANGER NOTHING. Malformed, unknown, revoked,
   expired and wrong-workspace all answer identically.

`anonymous_mode` is load-bearing in every public test: without it conftest
hands a cookie-less request an ambient signed-in member, and a test that
"opens the link without logging in" would silently be logged in.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from app import db, share_api, sharing
from app.main import app


@pytest.fixture(autouse=True)
def _clean_rate_limits():
    share_api.reset_for_tests()
    yield
    share_api.reset_for_tests()


@pytest.fixture()
def public(anonymous_mode):
    """A browser with no session at all — the stranger holding the link."""
    return TestClient(app)


def _uid(username: str) -> int:
    return int(db.get_user_by_username(username)["id"])


def _say(cid: str, role: str, content: str, meta: Optional[dict] = None) -> int:
    with db.connection() as con:
        row = con.execute(
            "INSERT INTO messages (conversation_id, role, content, meta, created_at) "
            "VALUES (%s, %s, %s, %s, now()) RETURNING id",
            (cid, role, content, db._json_param(meta or {})),
        ).fetchone()
    return int(row["id"])


def _conversation(owner: str, cid: str, title: str = "A conversation") -> str:
    db.create_conversation(_uid(owner), cid, title)
    _say(cid, "user", "hello")
    _say(cid, "assistant", "hi", {"route": "chat"})
    return cid


def _create(client: TestClient, cid: str, **body):
    payload = {"visibility": "public", "expiry": "7d"}
    payload.update(body)
    return client.post(f"/conversations/{cid}/share", json=payload)


# ---------------------------------------------------------------------------
# 1. Only the owner decides
# ---------------------------------------------------------------------------


def test_the_owner_can_create_read_update_and_revoke(login_client):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-1")

    state = owner.get(f"/conversations/{cid}/share").json()
    assert state["share"] is None
    assert state["policy"]["public_allowed"] is True

    created = _create(owner, cid)
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["token"], "the token is returned exactly once, at creation"
    assert body["share"]["visibility"] == "public"

    assert owner.get(f"/conversations/{cid}/share").json()["share"] is not None
    assert owner.patch(f"/conversations/{cid}/share", json={"expiry": "30d"}).status_code == 200
    assert owner.delete(f"/conversations/{cid}/share").json()["revoked"] is True
    assert owner.get(f"/conversations/{cid}/share").json()["share"] is None


def test_the_owners_view_never_returns_the_token(login_client):
    """It is stored as a hash, so this is not a redaction — there is nothing
    to return. The test pins it so a future convenience cannot add one."""
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-tok")
    token = _create(owner, cid).json()["token"]
    public_id, secret = token.split(".", 1)
    state = owner.get(f"/conversations/{cid}/share").json()
    assert secret not in str(state)
    assert state["share"]["url"].endswith(public_id)


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("post", "", {"visibility": "public", "expiry": "7d"}),
        ("post", "/refresh", {}),
        ("patch", "", {"expiry": "7d"}),
        ("delete", "", None),
        ("get", "", None),
    ],
)
def test_another_member_cannot_touch_it(login_client, method, suffix, body):
    """404, not 403: a stranger must not learn the conversation exists."""
    owner = login_client("olive")
    intruder = login_client("mallory")
    cid = _conversation("olive", "conv-share-2")
    _create(owner, cid)

    call = getattr(intruder, method)
    kwargs = {"json": body} if body is not None else {}
    assert call(f"/conversations/{cid}/share{suffix}", **kwargs).status_code == 404


def test_an_admin_is_not_an_owner(login_client):
    """Administering the workspace is not the same as owning a conversation.
    Publishing someone else's chat is the owner's decision alone."""
    owner = login_client("olive")
    admin = login_client("adele", role="admin")
    cid = _conversation("olive", "conv-share-admin")
    _create(owner, cid)
    assert _create(admin, cid).status_code == 404
    assert admin.delete(f"/conversations/{cid}/share").status_code == 404


def test_a_signed_out_request_cannot_manage_a_share(public, login_client):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-3")
    _create(owner, cid)
    assert public.delete(f"/conversations/{cid}/share").status_code in (401, 404)
    assert public.get(f"/conversations/{cid}/share").status_code in (401, 404)


def test_two_clicks_produce_one_link(login_client):
    """The partial unique index decides, not a prior SELECT. Otherwise an
    owner ends up with two live links and can revoke only one of them."""
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-4")
    first, second = _create(owner, cid), _create(owner, cid)
    assert first.json()["token"]
    # The loser returns the winner's share and NO token — that token belongs
    # to the request that created it.
    assert second.json()["token"] is None
    with db.connection() as con:
        n = con.execute(
            "SELECT count(*) c FROM conversation_shares "
            "WHERE conversation_id=%s AND status='active'",
            (cid,),
        ).fetchone()["c"]
    assert n == 1


def test_creating_links_is_rate_limited(login_client, monkeypatch):
    monkeypatch.setattr(share_api.settings, "share_create_rate_per_hour", 2)
    owner = login_client("olive")
    codes = []
    for i in range(3):
        cid = _conversation("olive", f"conv-share-rate-{i}")
        codes.append(_create(owner, cid).status_code)
    assert codes == [200, 200, 429]


# ---------------------------------------------------------------------------
# 2. What is published
# ---------------------------------------------------------------------------


def test_the_snapshot_contains_only_what_it_names(login_client, public):
    owner = login_client("olive")
    cid = "conv-share-5"
    db.create_conversation(_uid("olive"), cid, "Titled")
    _say(cid, "user", "a question")
    _say(cid, "assistant", "an answer", {
        "route": "search",
        "sources": [{"n": 1, "title": "T", "url": "https://ok.test/a", "domain": "ok.test"}],
        "reasoning": "hidden chain of thought",
        "generation_id": "gen-123",
        "model": "internal-model-name",
        "effort": "high",
        "steps": [{"tool": "search", "detail": "internal plan"}],
        "branch": {"parent": "abc"},
    })
    token = _create(owner, cid).json()["token"]

    body = public.get(f"/public/shares/{token}").json()
    blob = str(body)
    for private in ("hidden chain of thought", "gen-123", "internal-model-name",
                    "internal plan", "branch"):
        assert private not in blob, f"{private} reached the public payload"

    message = body["snapshot"]["messages"][1]
    # The allowlist, asserted as an allowlist: anything new here is a failure.
    assert set(message) <= {"role", "content", "sources", "route"}
    assert set(message["sources"][0]) == {"n", "title", "url", "domain"}
    assert message["sources"][0]["url"] == "https://ok.test/a"


def test_the_snapshot_drops_internal_urls_from_its_sources(login_client, public):
    owner = login_client("olive")
    cid = "conv-share-internal"
    db.create_conversation(_uid("olive"), cid, "T")
    _say(cid, "user", "q")
    _say(cid, "assistant", "a", {
        "route": "search",
        "sources": [
            {"n": 1, "title": "int", "url": "http://orchestrator:8000/x", "domain": "orchestrator"},
            {"n": 2, "title": "lan", "url": "http://192.168.9.68/y", "domain": "192.168.9.68"},
            {"n": 3, "title": "real", "url": "https://ok.test/z", "domain": "ok.test"},
        ],
    })
    token = _create(owner, cid).json()["token"]
    sources = public.get(f"/public/shares/{token}").json()["snapshot"]["messages"][1]["sources"]
    assert [s["url"] for s in sources] == ["https://ok.test/z"]


def test_the_owners_identity_is_hidden_unless_they_ask(login_client, public):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-owner")
    token = _create(owner, cid).json()["token"]
    snapshot = public.get(f"/public/shares/{token}").json()["snapshot"]
    assert "owner_name" not in snapshot

    owner.delete(f"/conversations/{cid}/share")
    token = _create(owner, cid, show_owner_name=True).json()["token"]
    snapshot = public.get(f"/public/shares/{token}").json()["snapshot"]
    assert snapshot["owner_name"] == "olive"
    # A display name, never the address the account was registered with.
    assert "@test.local" not in str(snapshot)


def test_an_unfinished_turn_is_not_published(login_client, public):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-partial")
    _say(cid, "assistant", "   ")  # an empty turn, mid-stream
    token = _create(owner, cid).json()["token"]
    messages = public.get(f"/public/shares/{token}").json()["snapshot"]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# 3. Provenance decides
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "meta,expected",
    [
        ({"route": "sql"}, "Salesforce"),
        ({"route": "dataset"}, "dataset"),
        ({"route": "rag"}, "documents"),
        ({"route": "chat", "salesforce_sources": [{"id": "001"}]}, "Salesforce"),
        ({"route": "agent", "citations": [{"record_id": "001", "object": "Account"}]}, "Salesforce"),
        ({"route": "chat", "document": {"name": "x.pdf"}}, "document"),
        ({"route": "chat", "attachments": [{"name": "x.png"}]}, "uploaded files"),
        ({"route": "chat", "report_files": [{"filename": "x.csv"}]}, "generated files"),
        ({"route": "chat", "code_sources": [{"path": "a.py"}]}, "private repository"),
        ({"route": "chat", "data": [{"a": 1}]}, "result set"),
    ],
)
def test_private_provenance_blocks_a_public_link(login_client, meta, expected):
    owner = login_client("olive")
    cid = f"conv-prov-{abs(hash(str(sorted(meta.items())))) % 100000}"
    db.create_conversation(_uid("olive"), cid, "T")
    _say(cid, "user", "q")
    _say(cid, "assistant", "a", meta)
    resp = _create(owner, cid)
    assert resp.status_code == 422, resp.text
    assert expected.lower() in resp.json()["detail"].lower()


def test_a_pasted_credential_blocks_a_public_link(login_client):
    owner = login_client("olive")
    cid = "conv-share-secret"
    db.create_conversation(_uid("olive"), cid, "T")
    _say(cid, "user", "deploy with AKIAIOSFODNN7EXAMPLE please")
    _say(cid, "assistant", "ok", {"route": "chat"})
    resp = _create(owner, cid)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "AWS access key" in detail
    assert "AKIA" not in detail, "the matched secret must never be echoed back"


def test_a_credential_is_not_written_to_the_audit_trail(login_client):
    owner = login_client("olive")
    cid = "conv-share-secret-audit"
    db.create_conversation(_uid("olive"), cid, "T")
    _say(cid, "user", "token is ghp_" + "a" * 36)
    _say(cid, "assistant", "ok", {"route": "chat"})
    _create(owner, cid)
    with db.connection() as con:
        rows = con.execute("SELECT action, meta FROM audit_events").fetchall()
    assert any(r["action"] == "conversation_share_blocked_by_policy" for r in rows)
    assert "ghp_" not in str([dict(r) for r in rows])


def test_an_empty_conversation_cannot_be_shared(login_client):
    owner = login_client("olive")
    db.create_conversation(_uid("olive"), "conv-share-empty", "Empty")
    resp = _create(owner, "conv-share-empty")
    assert resp.status_code == 422
    assert "nothing to share" in resp.json()["detail"]


def test_republishing_makes_a_new_version_on_the_same_link(login_client, public):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-6")
    token = _create(owner, cid).json()["token"]
    assert len(public.get(f"/public/shares/{token}").json()["snapshot"]["messages"]) == 2

    _say(cid, "user", "a later question")
    # Not published — that is the entire point of a snapshot.
    assert len(public.get(f"/public/shares/{token}").json()["snapshot"]["messages"]) == 2
    assert owner.get(f"/conversations/{cid}/share").json()["unshared_messages"] == 1

    assert owner.post(f"/conversations/{cid}/share/refresh", json={}).status_code == 200
    assert len(public.get(f"/public/shares/{token}").json()["snapshot"]["messages"]) == 3
    assert owner.get(f"/conversations/{cid}/share").json()["share"]["version"] == 2


def test_new_private_content_leaves_the_old_snapshot_live(login_client, public):
    """The failure to avoid is publishing something nobody reviewed because an
    earlier version happened to be fine."""
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-7")
    token = _create(owner, cid).json()["token"]
    _say(cid, "assistant", "crm rows", {"route": "sql"})

    resp = owner.post(f"/conversations/{cid}/share/refresh", json={})
    assert resp.status_code == 422
    assert "still shows the earlier version" in resp.json()["detail"]

    still = public.get(f"/public/shares/{token}")
    assert still.status_code == 200
    assert len(still.json()["snapshot"]["messages"]) == 2
    assert "crm rows" not in str(still.json())


# ---------------------------------------------------------------------------
# 4. The public route
# ---------------------------------------------------------------------------


def test_a_public_link_opens_with_no_session(public, login_client):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-8")
    token = _create(owner, cid).json()["token"]
    resp = public.get(f"/public/shares/{token}")
    assert resp.status_code == 200
    assert resp.json()["snapshot"]["messages"]
    assert resp.headers["cache-control"] == "private, no-store"
    assert "noindex" in resp.headers["x-robots-tag"]
    assert resp.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    "mangle",
    [
        lambda good: "not-a-token",
        lambda good: good.split(".")[0],                    # the id alone
        lambda good: good.split(".")[0] + ".wrongsecret",   # right id, wrong secret
        lambda good: "f" * 32 + ".anything",                # an id that never existed
        lambda good: good.replace(".", "", 1),              # no separator
        lambda good: good[:-1],                             # one byte short
        lambda good: good + "x",                            # one byte long
    ],
)
def test_every_bad_token_answers_identically(public, login_client, mangle):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-9")
    good = _create(owner, cid).json()["token"]
    resp = public.get(f"/public/shares/{mangle(good)}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == share_api._GONE


def test_a_revoked_link_stops_working_immediately(public, login_client):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-10")
    token = _create(owner, cid).json()["token"]
    assert public.get(f"/public/shares/{token}").status_code == 200
    owner.delete(f"/conversations/{cid}/share")
    resp = public.get(f"/public/shares/{token}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == share_api._GONE
    # Revoking twice is not an error, and does not resurrect anything.
    assert owner.delete(f"/conversations/{cid}/share").json()["revoked"] is False
    assert public.get(f"/public/shares/{token}").status_code == 404


def test_expiry_is_enforced_by_the_server(public, login_client):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-11")
    token = _create(owner, cid, expiry="24h").json()["token"]
    with db.connection() as con:
        con.execute(
            "UPDATE conversation_shares SET expires_at = %s WHERE conversation_id = %s",
            (datetime.now(timezone.utc) - timedelta(minutes=1), cid),
        )
    resp = public.get(f"/public/shares/{token}")
    assert resp.status_code == 404
    assert resp.json()["detail"] == share_api._GONE


def test_expiry_choices_are_checked_against_policy(login_client):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-expiry")
    # "never" is off by default: a link that never expires is a decision an
    # administrator makes, not one every author makes by accident.
    assert _create(owner, cid, expiry="never").status_code == 422
    assert _create(owner, cid, expiry="10 years").status_code == 422
    assert "never" not in owner.get(f"/conversations/{cid}/share").json()["expiry_choices"]


def test_a_workspace_link_requires_a_session(public, login_client):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-12")
    token = _create(owner, cid, visibility="workspace").json()["token"]
    resp = public.get(f"/public/shares/{token}")
    assert resp.status_code == 404
    # Identical to a wrong token: which links exist is not a stranger's business.
    assert resp.json()["detail"] == share_api._GONE
    assert owner.get(f"/public/shares/{token}").status_code == 200


def test_a_view_is_counted_without_the_page_depending_on_it(public, login_client):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-13")
    token = _create(owner, cid).json()["token"]
    for _ in range(3):
        public.get(f"/public/shares/{token}")
    state = owner.get(f"/conversations/{cid}/share").json()["share"]
    assert state["view_count"] == 3
    assert state["last_viewed_at"]


def test_the_public_route_is_rate_limited_per_link(public, login_client, monkeypatch):
    monkeypatch.setattr(share_api.settings, "share_view_rate_per_minute", 3)
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-flood")
    token = _create(owner, cid).json()["token"]
    codes = [public.get(f"/public/shares/{token}").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


# ---------------------------------------------------------------------------
# The token itself
# ---------------------------------------------------------------------------


def test_the_secret_is_never_stored(login_client):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-14")
    token = _create(owner, cid).json()["token"]
    secret = token.split(".", 1)[1]
    with db.connection() as con:
        row = con.execute(
            "SELECT * FROM conversation_shares WHERE conversation_id = %s", (cid,)
        ).fetchone()
    assert secret not in str(dict(row)), "a database dump must not yield a working link"


def test_a_token_is_not_guessable_from_another():
    ids = {sharing.mint_token()[0] for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 32 for i in ids)
    # 16 random bytes each: sequential ids and counters are not in the shape.
    ordered = sorted(int(i, 16) for i in ids)
    assert all(b - a > 1 for a, b in zip(ordered, ordered[1:]))


def test_redaction_keeps_only_the_addressable_half():
    _pid, token, _h = sharing.mint_token()
    redacted = sharing.redact(token)
    assert token.split(".", 1)[1] not in redacted
    assert redacted.endswith(".<redacted>")
    assert sharing.redact("garbage") == "<malformed>"


def test_the_audit_trail_never_carries_a_working_link(login_client):
    owner = login_client("olive")
    cid = _conversation("olive", "conv-share-15")
    token = _create(owner, cid).json()["token"]
    secret = token.split(".", 1)[1]
    owner.patch(f"/conversations/{cid}/share", json={"expiry": "30d"})
    owner.post(f"/conversations/{cid}/share/refresh", json={})
    owner.delete(f"/conversations/{cid}/share")

    with db.connection() as con:
        rows = [
            dict(r)
            for r in con.execute(
                "SELECT action, meta FROM audit_events "
                "WHERE action LIKE 'conversation_share%' ORDER BY id"
            ).fetchall()
        ]
    assert [r["action"] for r in rows] == [
        "conversation_share_created",
        "conversation_share_expiration_changed",
        "conversation_share_updated",
        "conversation_share_revoked",
    ]
    assert secret not in str(rows)


# ---------------------------------------------------------------------------
# Governance — /admin/api/shares/*, SUPER_ADMIN only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["member", "admin"])
def test_the_governance_surface_does_not_exist_for_anyone_else(login_client, role):
    """404, not 403: an admin typing the URL learns neither that the surface
    exists nor that they were refused."""
    who = login_client(f"nosy-{role}", role=role)
    assert who.get("/admin/api/shares").status_code == 404
    assert who.get("/admin/api/shares/policy").status_code == 404
    assert who.patch("/admin/api/shares/policy", json={"public_enabled": False}).status_code == 404
    assert who.delete("/admin/api/shares/1").status_code == 404


def test_a_super_admin_sees_every_link_but_no_conversation(login_client):
    owner = login_client("olive")
    root = login_client("root", role="super_admin")
    cid = _conversation("olive", "conv-gov-1")
    token = _create(owner, cid).json()["token"]

    body = root.get("/admin/api/shares").json()
    assert body["summary"]["active"] == 1 and body["summary"]["public"] == 1
    row = body["shares"][0]
    assert row["conversation_id"] == cid
    assert row["author"]["name"] == "olive"
    # The addressable half, never a working link — and never the messages.
    assert row["public_id"] == token.split(".")[0]
    assert token.split(".", 1)[1] not in str(body)
    assert "hello" not in str(body) and "snapshot" not in str(body)


def test_a_super_admin_can_revoke_someone_elses_link(login_client, public):
    owner = login_client("olive")
    root = login_client("root", role="super_admin")
    cid = _conversation("olive", "conv-gov-2")
    token = _create(owner, cid).json()["token"]
    share_id = root.get("/admin/api/shares").json()["shares"][0]["id"]

    assert public.get(f"/public/shares/{token}").status_code == 200
    assert root.delete(f"/admin/api/shares/{share_id}").json()["revoked"] is True
    assert public.get(f"/public/shares/{token}").status_code == 404
    # The author sees it is gone, and can publish a fresh one.
    assert owner.get(f"/conversations/{cid}/share").json()["share"] is None

    with db.connection() as con:
        actions = [
            r["action"]
            for r in con.execute("SELECT action FROM audit_events").fetchall()
        ]
    assert "conversation_share_revoked_by_admin" in actions


def test_revoking_a_share_from_another_workspace_is_a_404(login_client):
    root = login_client("root", role="super_admin")
    assert root.delete("/admin/api/shares/99999").status_code == 404


def test_policy_is_a_ceiling_the_author_cannot_publish_past(login_client):
    owner = login_client("olive")
    root = login_client("root", role="super_admin")
    cid = _conversation("olive", "conv-gov-3")

    assert root.patch(
        "/admin/api/shares/policy", json={"public_enabled": False}
    ).json()["policy"]["public_enabled"] is False

    resp = _create(owner, cid)
    assert resp.status_code == 422
    assert "administrator" in resp.json()["detail"]
    # A workspace link is a different decision and is still allowed.
    assert _create(owner, cid, visibility="workspace").status_code == 200


def test_tightening_the_policy_does_not_silently_break_live_links(login_client, public):
    """An administrator planning for the future has not necessarily decided to
    break the links people sent to customers this morning. They have to say so."""
    owner = login_client("olive")
    root = login_client("root", role="super_admin")
    cid = _conversation("olive", "conv-gov-4")
    token = _create(owner, cid).json()["token"]

    root.patch("/admin/api/shares/policy", json={"public_enabled": False})
    assert public.get(f"/public/shares/{token}").status_code == 200

    done = root.patch(
        "/admin/api/shares/policy",
        json={"public_enabled": False, "revoke_existing_public": True},
    ).json()
    assert done["revoked"] == 1
    assert public.get(f"/public/shares/{token}").status_code == 404


def test_the_policy_caps_how_long_a_link_may_live(login_client):
    owner = login_client("olive")
    root = login_client("root", role="super_admin")
    cid = _conversation("olive", "conv-gov-5")
    root.patch("/admin/api/shares/policy", json={"max_days": 7})
    assert _create(owner, cid, expiry="30d").status_code == 422
    assert _create(owner, cid, expiry="7d").status_code == 200
