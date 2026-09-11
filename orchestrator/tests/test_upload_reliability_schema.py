"""V29 — the durable state upload reliability rests on.

Three things the 2026-09-09 incidents lacked: a chunked upload session the
server remembers (so a reload can ask what arrived), a send intent the server
remembers (so an accepted request survives the process that accepted it), and
a lease on an analysis run (so a restart cannot start the same video twice).
These tests pin the accessor contract the upload rail, the chat endpoint and
the video pipeline build on. Real database; no mocks.
"""
from __future__ import annotations

import pytest

from app import db


@pytest.fixture()
def owner():
    user_id = int(db.create_user("v29-owner", "hash"))
    db.create_conversation(user_id, "conv-v29", "t")
    yield user_id
    db.delete_conversation(user_id, "conv-v29")


# ------------------------------------------------------------ sessions --


def test_upload_session_records_parts_once_each_and_replays_its_result(owner):
    s = db.create_upload_session("a" * 32, owner, "conv-v29", "big.mp4", "video", expected_bytes=300, expected_parts=3, part_size=100)
    assert s["status"] == "uploading" and s["bytes_received"] == 0 and s["accepted_parts"] == {}
    assert s["expires_at"] is not None

    db.record_upload_part("a" * 32, 0, 100, "h0")
    db.record_upload_part("a" * 32, 2, 100, "h2")
    # The same part again: replaced, not double-counted.
    s = db.record_upload_part("a" * 32, 0, 100, "h0b")
    assert s["bytes_received"] == 200
    assert set(s["accepted_parts"]) == {"0", "2"} and s["accepted_parts"]["0"]["sha256"] == "h0b"

    # Finalisation is a one-winner transition.
    assert db.try_begin_upload_finalize("a" * 32) == "uploading"
    assert db.try_begin_upload_finalize("a" * 32) == "finalizing"
    done = db.set_upload_session_status("a" * 32, "complete", result={"upload_id": "a" * 32, "files": 1})
    assert done["status"] == "complete" and done["completed_at"] is not None and done["result"]["files"] == 1
    # A retried complete after a lost acknowledgement sees the same answer.
    assert db.try_begin_upload_finalize("a" * 32) == "complete"
    assert db.get_upload_session("a" * 32)["result"] == {"upload_id": "a" * 32, "files": 1}
    assert db.try_begin_upload_finalize("f" * 32) is None


def test_expired_sessions_are_only_the_open_ones(owner):
    db.create_upload_session("b" * 32, owner, "conv-v29", "x.mp4", "video", ttl_hours=0)
    db.create_upload_session("c" * 32, owner, "conv-v29", "y.mp4", "video", ttl_hours=0)
    db.set_upload_session_status("c" * 32, "complete", result={})
    db.create_upload_session("d" * 32, owner, "conv-v29", "z.mp4", "video", ttl_hours=24)
    with db.connection() as con:
        con.execute("UPDATE upload_sessions SET expires_at = expires_at - interval '1 hour' WHERE id IN (%s, %s)", ("b" * 32, "c" * 32))
    ids = {s["id"] for s in db.expired_upload_sessions()}
    assert "b" * 32 in ids and "c" * 32 not in ids and "d" * 32 not in ids
    assert [s["id"] for s in db.list_upload_sessions("conv-v29", owner)] == ["b" * 32, "c" * 32, "d" * 32]


def test_upload_session_purpose_and_status_are_constrained(owner):
    with pytest.raises(Exception):
        db.create_upload_session("e" * 32, owner, "conv-v29", "x", "spreadsheet")
    db.create_upload_session("e" * 32, owner, "conv-v29", "x", "document")
    with pytest.raises(Exception):
        db.set_upload_session_status("e" * 32, "teleported")


# ------------------------------------------------------------ intents ---


def test_a_chat_request_is_recorded_once_per_intent_and_resumes_under_a_new_generation(owner):
    row = db.create_chat_request("intent-1", owner, "conv-v29", "gen-1", {"message": "what was decided?"})
    assert row is not None and row["status"] == "accepted" and row["attempt"] == 1
    # The same intent again (a double Send, a retried POST): NOT a new row.
    assert db.create_chat_request("intent-1", owner, "conv-v29", "gen-other", {"message": "x"}) is None
    assert db.get_chat_request("intent-1")["generation_id"] == "gen-1"
    assert db.latest_chat_request("conv-v29")["intent_id"] == "intent-1"

    assert db.set_chat_request_status("intent-1", "running")["finished_at"] is None
    # A restart: every open request is interrupted.
    assert db.interrupt_open_chat_requests() == 1
    assert db.get_chat_request("intent-1")["status"] == "interrupted"
    # Resumed: fresh generation, attempt 2, back to accepted.
    r = db.resume_chat_request("intent-1", "gen-2")
    assert r["generation_id"] == "gen-2" and r["attempt"] == 2 and r["status"] == "accepted"
    done = db.set_chat_request_status("intent-1", "completed")
    assert done["finished_at"] is not None
    # A finished request does not resume — and keeps its generation.
    again = db.resume_chat_request("intent-1", "gen-3")
    assert again["status"] == "completed" and again["generation_id"] == "gen-2"
    assert db.interrupt_open_chat_requests() == 0


def test_generation_ids_are_unique_across_intents(owner):
    assert db.create_chat_request("intent-2", owner, "conv-v29", "gen-shared", {}) is not None
    with pytest.raises(Exception):
        db.create_chat_request("intent-3", owner, "conv-v29", "gen-shared", {})


def test_deleting_the_conversation_takes_its_sessions_and_intents(owner):
    user_id = int(db.create_user("v29-other", "hash"))
    db.create_conversation(user_id, "conv-v29-gone", "t")
    db.create_upload_session("1" * 32, user_id, "conv-v29-gone", "x.mp4", "video")
    db.create_chat_request("intent-gone", user_id, "conv-v29-gone", "gen-gone", {})
    assert db.delete_conversation(user_id, "conv-v29-gone") is True
    assert db.get_upload_session("1" * 32) is None
    assert db.get_chat_request("intent-gone") is None


# ------------------------------------------------------------- leases ---


def test_a_live_lease_is_not_requeued_and_an_expired_one_is():
    row = db.upsert_video_analysis("9" * 64, 10, "video/mp4", "clip.mp4")
    aid = row["id"]
    try:
        db.update_video_analysis(aid, status="running")
        assert db.claim_video_lease(aid, "proc-A", ttl_s=60) is True
        assert db.claim_video_lease(aid, "proc-B", ttl_s=60) is False, "a live lease belongs to A"
        assert db.claim_video_lease(aid, "proc-A", ttl_s=60) is True, "the owner renews"
        assert db.requeue_interrupted_video_analyses() == 0, "a heartbeating run is not interrupted"
        with db.connection() as con:
            con.execute("UPDATE video_analyses SET lease_expires_at = lease_expires_at - interval '1 day' WHERE id = %s", (aid,))
        assert db.claim_video_lease(aid, "proc-B", ttl_s=60) is True, "an expired lease can be taken"
        db.release_video_lease(aid, "proc-B")
        db.update_video_analysis(aid, status="running")
        assert db.requeue_interrupted_video_analyses() == 1, "no lease at all = interrupted"
        assert db.get_video_analysis(aid)["status"] == "queued"
    finally:
        db.delete_video_analysis(aid)


# --------------------------------------------------- conditional replace ---


def test_a_stale_tab_cannot_overwrite_a_newer_thread(login_client):
    """RC-4: the whole-thread replace was unconditional. With the
    conversation's updated_at the tab last saw, a same-length overwrite
    of a server-persisted answer is refused with the server's value."""
    client = login_client("v29-replace")
    conv = "conv-v29-replace"
    r = client.post("/history/conversations", json={"id": conv, "title": "t"})
    assert r.status_code in (200, 201), r.text
    r = client.post(f"/history/conversations/{conv}/messages", json={"role": "user", "content": "q1"})
    assert r.status_code in (200, 201), r.text
    seen = client.get(f"/history/conversations/{conv}").json()["updated_at"]

    # Unchanged since: the replace goes through.
    body = {"messages": [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1 (tab)"}], "expected_updated_at": seen}
    r = client.put(f"/history/conversations/{conv}/messages", json=body)
    assert r.status_code == 200, r.text
    later = client.get(f"/history/conversations/{conv}").json()["updated_at"]

    # Meanwhile the server persisted a better answer (a different tab, or the
    # server itself). A tab still holding `seen` must not overwrite it.
    r = client.post(f"/history/conversations/{conv}/messages", json={"role": "user", "content": "q2"})
    assert r.status_code in (200, 201)
    stale = {"messages": [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1 (stale)"}, {"role": "user", "content": "q2 (stale)"}], "expected_updated_at": later}
    r = client.put(f"/history/conversations/{conv}/messages", json=stale)
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == "conversation changed" and r.json()["updated_at"] and r.json()["messages"] == 3
    kept = [m["content"] for m in client.get(f"/history/conversations/{conv}").json()["messages"]]
    assert kept == ["q1", "a1 (tab)", "q2"], "nothing was written"

    # Without the field: the old unconditional behaviour, still never shrinking.
    r = client.put(f"/history/conversations/{conv}/messages", json={"messages": stale["messages"]})
    assert r.status_code == 200
    r = client.put(f"/history/conversations/{conv}/messages", json={"messages": stale["messages"][:1]})
    assert r.status_code == 409 and "shrink" in r.json()["detail"]


# --------------------------------------------------- idempotent create ---


def test_creating_the_same_conversation_twice_is_not_an_error_for_its_owner(login_client):
    """The client calls create to ENSURE a conversation exists before pushing
    its thread, then swallows the 409 — so the healthy path was answering
    with an error the browser logged to the console next to real failures
    (seen on production 2026-09-11). Someone else's id is still a conflict."""
    alice = login_client("conv-owner")
    first = alice.post("/history/conversations", json={"id": "conv-idem", "title": "First"})
    assert first.status_code in (200, 201), first.text

    # Renamed after creation: an ensure-exists call must not undo that.
    # (PUT renames; POST .../title asks the server to GENERATE one.)
    assert alice.put("/history/conversations/conv-idem", json={"title": "Renamed"}).status_code == 200

    again = alice.post("/history/conversations", json={"id": "conv-idem", "title": "Second"})
    assert again.status_code == 200, again.text
    assert again.json()["id"] == "conv-idem"
    assert again.json()["title"] == "Renamed", "an ensure-exists call must not retitle"

    # Another account asking for the same id is a real conflict.
    bob = login_client("conv-other")
    clash = bob.post("/history/conversations", json={"id": "conv-idem", "title": "Mine"})
    assert clash.status_code == 409, clash.text
    assert alice.get("/history/conversations/conv-idem").json()["title"] == "Renamed"
