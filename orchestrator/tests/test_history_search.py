"""V4-DESIGN §2: GET /history/search — the backend behind the search palette.

Covers the list the design calls for: title match, message match, both,
case-insensitivity, `%`/`_` treated literally, cross-user isolation, the empty
query, the limit cap, snippet windowing and ordering. All offline.
"""
import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_db_path", str(tmp_path / "app.sqlite3"))
    monkeypatch.setattr(settings, "session_secret_file", str(tmp_path / ".session_secret"))
    monkeypatch.delenv("SESSION_SECRET", raising=False)


@pytest.fixture()
def alice(env, as_user):
    # No registration: login is gone. The app runs AS this user.
    as_user("alice")
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def bob(env, as_user):
    # No registration: login is gone. The app runs AS this user.
    as_user("bob")
    with TestClient(app) as c:
        yield c


def _new(client, title: str) -> str:
    resp = client.post("/history/conversations", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["id"]


def _say(client, conversation_id: str, content: str, role: str = "user") -> None:
    resp = client.post(
        f"/history/conversations/{conversation_id}/messages",
        json={"role": role, "content": content},
    )
    assert resp.status_code == 200


def _search(client, q, **params) -> list:
    resp = client.get("/history/search", params={"q": q, **params})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"results"}
    return body["results"]


def _ids(client, q, **params) -> list:
    return [r["id"] for r in _search(client, q, **params)]


def _one(client, q, **params) -> dict:
    results = _search(client, q, **params)
    assert len(results) == 1, results
    return results[0]


# ---------------------------------------------------------------------------
# Auth + user scoping
# ---------------------------------------------------------------------------

def test_search_needs_no_credentials(env):
    """Login was removed — search is open to whoever can reach the port."""
    with TestClient(app) as anon:
        assert anon.get("/history/search", params={"q": "anything"}).status_code == 200
        assert anon.get("/history/search", params={"q": ""}).status_code == 200


def test_another_owners_rows_never_surface_in_search(alice):
    """Search is scoped by owner, seeded through the db layer since the HTTP
    layer no longer has a second identity to act as."""
    from app import db

    other = db.create_user("someone-else", "!x")
    db.create_conversation(other, "theirs", "Confidential merger notes")
    db.add_message(other, "theirs", "user", "the acquisition target is Northwind")

    assert _ids(alice, "merger") == []
    assert _ids(alice, "Northwind") == []
    assert _ids(alice, "confidential") == []

    # Alice's own matching chat still finds itself.
    mine = _new(alice, "Merger of my own")
    assert _ids(alice, "merger") == [mine]


# ---------------------------------------------------------------------------
# What matches: title, message, both
# ---------------------------------------------------------------------------

def test_title_match_has_no_snippet(alice):
    conv_id = _new(alice, "Q3 pipeline review")
    _say(alice, conv_id, "unrelated body text")

    row = _one(alice, "pipeline")
    assert row == {
        "id": conv_id,
        "title": "Q3 pipeline review",
        "updated_at": row["updated_at"],
        "pinned": False,
        "archived": False,
        "snippet": None,
        "matched_in": "title",
    }


def test_message_match_returns_a_snippet(alice):
    conv_id = _new(alice, "Untitled chat")
    _say(alice, conv_id, "what is the pipeline for next quarter?")

    row = _one(alice, "pipeline")
    assert row["id"] == conv_id
    assert row["title"] == "Untitled chat"
    assert row["matched_in"] == "message"
    assert row["snippet"] == "what is the pipeline for next quarter?"


def test_match_in_title_and_message_prefers_the_snippet(alice):
    """When both sides match, the row still carries the message context.

    `snippet is None` is exactly `matched_in == "title"` — the design's
    "null for title-only matches" — so a chat that matches on both reports
    "message" and hands the palette something to show.
    """
    conv_id = _new(alice, "pipeline planning")
    _say(alice, conv_id, "the pipeline looks healthy")

    row = _one(alice, "pipeline")
    assert row["id"] == conv_id
    assert row["matched_in"] == "message"
    assert row["snippet"] == "the pipeline looks healthy"


def test_one_row_per_conversation_however_many_messages_match(alice):
    conv_id = _new(alice, "many hits")
    for _ in range(5):
        _say(alice, conv_id, "pipeline again")

    assert _ids(alice, "pipeline") == [conv_id]


def test_snippet_comes_from_the_first_matching_message(alice):
    conv_id = _new(alice, "ordering of hits")
    _say(alice, conv_id, "no hit here")
    _say(alice, conv_id, "FIRST pipeline mention")
    _say(alice, conv_id, "SECOND pipeline mention")

    assert _one(alice, "pipeline")["snippet"] == "FIRST pipeline mention"


def test_non_matching_conversations_are_absent(alice):
    _new(alice, "weather chat")
    conv_id = _new(alice, "pipeline chat")
    assert _ids(alice, "pipeline") == [conv_id]
    assert _ids(alice, "nothing-matches-this") == []


def test_archived_conversations_are_included_and_flagged(alice):
    conv_id = _new(alice, "archived pipeline chat")
    alice.put(f"/history/conversations/{conv_id}", json={"archived": True})

    row = _one(alice, "pipeline")
    assert row["id"] == conv_id
    assert row["archived"] is True


def test_pinned_flag_is_reported(alice):
    conv_id = _new(alice, "pinned pipeline chat")
    alice.put(f"/history/conversations/{conv_id}", json={"pinned": True})
    assert _one(alice, "pipeline")["pinned"] is True


# ---------------------------------------------------------------------------
# Case-insensitivity
# ---------------------------------------------------------------------------

def test_case_insensitive_on_titles_and_messages(alice):
    titled = _new(alice, "Quarterly PIPELINE Review")
    bodied = _new(alice, "plain")
    _say(alice, bodied, "Our PiPeLiNe is fine")

    for term in ("pipeline", "PIPELINE", "PiPeLiNe", "pIpElInE"):
        assert sorted(_ids(alice, term)) == sorted([titled, bodied])


def test_snippet_preserves_the_original_casing(alice):
    conv_id = _new(alice, "casing")
    _say(alice, conv_id, "The Pipeline Report is ready")
    assert _one(alice, "pipeline")["snippet"] == "The Pipeline Report is ready"


# ---------------------------------------------------------------------------
# LIKE wildcards and the escape character are literal text
# ---------------------------------------------------------------------------

def test_percent_is_a_literal_character_not_a_wildcard(alice):
    with_percent = _new(alice, "Closed 50% of deals")
    _new(alice, "no symbol here")
    plain = _new(alice, "plain body")
    _say(alice, plain, "growth was 12 percent")

    # "%" alone would match every row if it leaked through as a wildcard.
    assert _ids(alice, "%") == [with_percent]
    assert _ids(alice, "50%") == [with_percent]
    assert _ids(alice, "%%") == []


def test_underscore_is_a_literal_character_not_a_single_char_wildcard(alice):
    with_underscore = _new(alice, "table_name lookup")
    decoy = _new(alice, "tableXname lookup")

    assert _ids(alice, "table_name") == [with_underscore]
    assert _ids(alice, "_") == [with_underscore]
    # The decoy is only reachable by its literal text.
    assert _ids(alice, "tableXname") == [decoy]


def test_wildcards_inside_message_bodies_are_literal_too(alice):
    conv_id = _new(alice, "sql notes")
    _say(alice, conv_id, "we ran LIKE '%acme%' against the accounts table")
    other = _new(alice, "unrelated")
    _say(alice, other, "nothing special")

    assert _ids(alice, "%acme%") == [conv_id]
    assert _ids(alice, "%") == [conv_id]


def test_backslash_the_escape_character_is_literal(alice):
    """The escape char itself must be escaped, or a trailing backslash would
    corrupt the pattern (and SQLite would raise on a dangling escape)."""
    with_slash = _new(alice, "path C:\\reports\\out")
    _new(alice, "no slash at all")

    assert _ids(alice, "\\") == [with_slash]
    assert _ids(alice, "C:\\reports") == [with_slash]
    assert _ids(alice, "\\%") == []


def test_pattern_helper_escapes_every_special_character():
    assert db.like_contains_pattern("plain") == "%plain%"
    assert db.like_contains_pattern("50%") == "%50\\%%"
    assert db.like_contains_pattern("a_b") == "%a\\_b%"
    assert db.like_contains_pattern("c\\d") == "%c\\\\d%"
    # The escape char is doubled first, so an already-escaped-looking input
    # cannot smuggle a live wildcard through.
    assert db.like_contains_pattern("\\%") == "%\\\\\\%%"


# ---------------------------------------------------------------------------
# Empty query / query length
# ---------------------------------------------------------------------------

def test_empty_and_whitespace_queries_return_no_results_without_erroring(alice):
    conv_id = _new(alice, "pipeline review")
    _say(alice, conv_id, "content")

    for term in ("", "   ", "\t", "\n  "):
        assert _search(alice, term) == []

    # A missing q behaves the same way.
    resp = alice.get("/history/search")
    assert resp.status_code == 200
    assert resp.json() == {"results": []}


def test_query_is_trimmed_before_matching(alice):
    conv_id = _new(alice, "pipeline review")
    assert _ids(alice, "  pipeline  ") == [conv_id]


def test_query_length_is_bounded(alice):
    _new(alice, "x" * 150)
    assert alice.get("/history/search", params={"q": "x" * 100}).status_code == 200
    over = alice.get("/history/search", params={"q": "x" * 101})
    assert over.status_code == 400
    # Trimming happens first: padding does not push a legal query over the line.
    padded = alice.get("/history/search", params={"q": "  " + "x" * 100 + "  "})
    assert padded.status_code == 200


# ---------------------------------------------------------------------------
# limit: default 50, hard cap 100
# ---------------------------------------------------------------------------

def _seed_many(client, count: int, prefix: str = "pipeline chat") -> None:
    """Seed `count` matching conversations through the db layer (the HTTP
    route is exercised everywhere else; this keeps the cap test quick)."""
    user_id = int(db.get_user_by_username("alice")["id"])
    for i in range(count):
        db.create_conversation(user_id, f"seed-{i:04d}", f"{prefix} {i}")


def test_limit_defaults_to_50(alice):
    _seed_many(alice, 60)
    assert len(_search(alice, "pipeline")) == 50


def test_limit_is_honoured_below_the_cap(alice):
    _seed_many(alice, 60)
    assert len(_search(alice, "pipeline", limit=7)) == 7
    assert len(_search(alice, "pipeline", limit=1)) == 1


def test_limit_is_clamped_to_the_hard_cap_of_100(alice):
    _seed_many(alice, 105)
    assert len(_search(alice, "pipeline", limit=100)) == 100
    # Over the cap is clamped, not rejected.
    assert len(_search(alice, "pipeline", limit=500)) == 100
    assert len(_search(alice, "pipeline", limit=100000)) == 100


def test_non_numeric_limit_is_rejected(alice):
    assert alice.get(
        "/history/search", params={"q": "pipeline", "limit": "lots"}
    ).status_code == 422


# ---------------------------------------------------------------------------
# Snippet windowing
# ---------------------------------------------------------------------------

def test_short_message_is_returned_whole_without_ellipsis(alice):
    conv_id = _new(alice, "short")
    _say(alice, conv_id, "short pipeline note")
    snippet = _one(alice, "pipeline")["snippet"]
    assert snippet == "short pipeline note"
    assert "…" not in snippet


def test_long_message_is_windowed_around_the_hit(alice):
    conv_id = _new(alice, "long")
    content = "a" * 200 + " pipeline " + "b" * 200
    _say(alice, conv_id, content)

    snippet = _one(alice, "pipeline")["snippet"]
    assert "pipeline" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    # ~120 characters of context plus the two ellipsis markers.
    assert len(snippet.strip("…")) == 120
    assert len(snippet) < len(content)
    # Centered: roughly as much context before the hit as after.
    before, _, after = snippet.strip("…").partition("pipeline")
    assert abs(len(before) - len(after)) <= 2


def test_hit_near_the_end_still_yields_a_full_width_window(alice):
    conv_id = _new(alice, "tail")
    content = "x" * 200 + "pipeline"
    _say(alice, conv_id, content)

    snippet = _one(alice, "pipeline")["snippet"]
    assert snippet.endswith("pipeline")
    assert not snippet.endswith("…")  # nothing was cut off the end
    assert snippet.startswith("…")
    assert len(snippet.lstrip("…")) == 120


def test_hit_at_the_start_has_no_leading_ellipsis(alice):
    conv_id = _new(alice, "head")
    content = "pipeline " + "z" * 300
    _say(alice, conv_id, content)

    snippet = _one(alice, "pipeline")["snippet"]
    assert snippet.startswith("pipeline")
    assert snippet.endswith("…")


def test_snippet_window_helper_directly():
    assert db.snippet_window("tiny", "tiny") == "tiny"
    windowed = db.snippet_window("a" * 500 + "hit" + "b" * 500, "hit")
    assert "hit" in windowed
    assert len(windowed.strip("…")) == 120
    # A needle the LIKE matched but Python cannot locate degrades to the head
    # of the message rather than blowing up.
    fallback = db.snippet_window("q" * 300, "absent")
    assert fallback.startswith("q") and fallback.endswith("…")
    assert len(fallback.rstrip("…")) == 120


# ---------------------------------------------------------------------------
# Ordering: pinned DESC, updated_at DESC
# ---------------------------------------------------------------------------

def test_results_are_pinned_first_then_most_recent(alice):
    first = _new(alice, "pipeline one")
    second = _new(alice, "pipeline two")
    third = _new(alice, "pipeline three")

    # Newest-first to start, matching the sidebar listing.
    assert _ids(alice, "pipeline") == [third, second, first]

    alice.put(f"/history/conversations/{first}", json={"pinned": True})
    assert _ids(alice, "pipeline") == [first, third, second]

    alice.put(f"/history/conversations/{second}", json={"pinned": True})
    assert _ids(alice, "pipeline") == [second, first, third]

    # Activity re-sorts within the pinned group only.
    _say(alice, first, "ping")
    assert _ids(alice, "pipeline") == [first, second, third]
    _say(alice, third, "ping")
    assert _ids(alice, "pipeline") == [first, second, third]


def test_archived_rows_sort_alongside_active_ones(alice):
    active = _new(alice, "pipeline active")
    archived = _new(alice, "pipeline archived")
    alice.put(f"/history/conversations/{archived}", json={"archived": True})
    alice.put(f"/history/conversations/{archived}", json={"pinned": True})

    # Pinned wins regardless of archived state; both are present.
    assert _ids(alice, "pipeline") == [archived, active]


def test_limit_keeps_the_highest_ranked_rows(alice):
    oldest = _new(alice, "pipeline oldest")
    _new(alice, "pipeline middle")
    newest = _new(alice, "pipeline newest")
    alice.put(f"/history/conversations/{oldest}", json={"pinned": True})

    assert _ids(alice, "pipeline", limit=2) == [oldest, newest]


# ---------------------------------------------------------------------------
# The rest of /history is untouched
# ---------------------------------------------------------------------------

def test_search_route_does_not_shadow_conversation_routes(alice):
    conv_id = _new(alice, "still reachable")
    assert alice.get(f"/history/conversations/{conv_id}").status_code == 200
    assert [c["id"] for c in alice.get("/history/conversations").json()] == [conv_id]
    # A conversation literally named "search" is still addressable.
    named = alice.post(
        "/history/conversations", json={"id": "search", "title": "search"}
    )
    assert named.status_code == 200
    assert alice.get("/history/conversations/search").json()["title"] == "search"
