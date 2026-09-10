"""Resumable, idempotent chunked uploads (V29, docs/upload-reliability/API.md).

What the 2026-09-09 incidents showed the rail could not do: say what had
already arrived after a reload, refuse a part whose body was cut short,
notice a missing FINAL part, or answer a retried `complete` with the same
result. Every test here goes through the real routes with a real session
cookie and the real database; only the document finaliser is stubbed, and
only where the test needs it to block or be counted.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time

import pytest
from starlette.requests import ClientDisconnect, Request

from app import db, metrics, uploads as up
from app.config import settings

MiB = 1024 * 1024


@pytest.fixture()
def alice(login_client):
    return login_client("alice")


@pytest.fixture()
def bob(login_client):
    return login_client("bob")


@pytest.fixture()
def conv(alice):
    resp = alice.post("/history/conversations", json={"id": "conv-resume", "title": "r"})
    assert resp.status_code == 200, resp.text
    return "conv-resume"


@pytest.fixture(autouse=True)
def _isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    # Every test starts with the sweep armed, not throttled by the previous one.
    monkeypatch.setattr(up, "_last_sweep_at", 0.0)


PAYLOAD = b"%PDF-1.4 fake but sniffable content for tests\n" * 100
HALF = len(PAYLOAD) // 2
PART_A, PART_B = PAYLOAD[:HALF], PAYLOAD[HALF:]


def _init(client, conv, *, filename="big.pdf", purpose="document", **declared):
    data = {"conversation_id": conv, "filename": filename, "purpose": purpose}
    data.update({k: str(v) for k, v in declared.items()})
    return client.post("/uploads/chunked/init", data=data)


def _upload_id(client, conv, **kw) -> str:
    resp = _init(client, conv, **kw)
    assert resp.status_code == 200, resp.text
    return resp.json()["upload_id"]


def _put(client, conv, upload_id, index, body, **headers):
    return client.put(
        f"/uploads/chunked/{conv}/{upload_id}/part/{index}", content=body, headers=headers
    )


def _parts_dir(conv, upload_id) -> str:
    return os.path.join(up.upload_root(conv, upload_id), up._PARTS_DIR)


def _listing(conv, upload_id) -> list:
    d = _parts_dir(conv, upload_id)
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


# ── init ────────────────────────────────────────────────────────────────────


def test_init_records_the_expectation_and_answers_the_contract(alice, conv):
    resp = _init(alice, conv, size=len(PAYLOAD), parts=2, part_size=HALF + 1)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["part_limit_bytes"] == up._PART_CAP and body["max_parts"] == up._MAX_PARTS
    assert body["accepted_parts"] == [] and body["bytes_received"] == 0
    assert body["expires_at"]
    row = db.get_upload_session(body["upload_id"])
    assert row["status"] == "uploading"
    assert row["expected_bytes"] == len(PAYLOAD)
    assert row["expected_parts"] == 2 and row["part_size"] == HALF + 1
    assert row["user_id"] == db.get_user_by_username("alice")["id"]
    assert row["conversation_id"] == conv
    assert os.path.isdir(_parts_dir(conv, body["upload_id"]))


def test_init_refuses_an_oversize_declaration_before_any_byte(alice, conv, monkeypatch):
    monkeypatch.setattr(settings, "upload_max_mb", 1)
    resp = _init(alice, conv, size=MiB + 1)
    assert resp.status_code == 413, resp.text
    assert db.list_upload_sessions(conv, db.get_user_by_username("alice")["id"]) == []
    assert not os.path.isdir(os.path.join(settings.workspace_dir, "uploads", conv))


def test_init_refuses_a_declaration_the_rail_cannot_honour(alice, conv):
    assert _init(alice, conv, parts=up._MAX_PARTS + 1).status_code == 400
    assert _init(alice, conv, part_size=up._PART_CAP + 1).status_code == 413
    # 100 bytes cannot be 2 parts of 10.
    assert _init(alice, conv, size=100, parts=2, part_size=10).status_code == 400


def test_init_applies_the_cap_of_the_purpose(alice, conv, monkeypatch):
    """A video is measured against VIDEO_MAX_UPLOAD_MB, a document against
    UPLOAD_MAX_MB — the same declared size passes one and fails the other."""
    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(settings, "upload_max_mb", 1)
    monkeypatch.setattr(settings, "video_max_upload_mb", 3)
    assert _init(alice, conv, size=2 * MiB).status_code == 413
    assert _init(alice, conv, filename="a.mp4", purpose="video", size=2 * MiB).status_code == 200
    resp = _init(alice, conv, filename="a.mp4", purpose="video", size=4 * MiB)
    assert resp.status_code == 413 and "video" in resp.json()["detail"]


# ── part ────────────────────────────────────────────────────────────────────


def test_a_part_interrupted_mid_body_is_not_a_part(alice, conv, caplog):
    """Review T-04, case two: a part interrupted mid-body used to stay on
    disk as a valid-looking part. Yesterday's reload: `ClientDisconnect` out
    of request.stream() became a 500 with a traceback, and the bytes that had
    landed kept the part's name. Now: quiet, nothing recorded, no `.tmp`."""
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, PART_A).status_code == 200

    async def cut_short(self):
        yield b"some of part one"
        raise ClientDisconnect()

    caplog.set_level("INFO")
    # Its own patch context: the shared `monkeypatch` also holds the
    # workspace redirect, and undoing that mid-test would move the goalposts.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Request, "stream", cut_short)
        resp = _put(alice, conv, upload_id, 1, PART_B)
    assert resp.status_code == 408, resp.text
    assert not [r for r in caplog.records if r.levelname == "ERROR"]
    assert _listing(conv, upload_id) == ["0"], "no accepted part, no .tmp"
    row = db.get_upload_session(upload_id)
    assert set(row["accepted_parts"]) == {"0"} and row["bytes_received"] == len(PART_A)


def test_a_sha256_mismatch_is_422_and_records_nothing(alice, conv):
    upload_id = _upload_id(alice, conv)
    wrong = hashlib.sha256(b"not these bytes").hexdigest()
    resp = _put(alice, conv, upload_id, 0, PART_A, **{"X-Part-SHA256": wrong})
    assert resp.status_code == 422, resp.text
    assert _listing(conv, upload_id) == []
    assert db.get_upload_session(upload_id)["accepted_parts"] == {}
    # Malformed digest: refused before any byte is kept.
    assert _put(alice, conv, upload_id, 0, PART_A, **{"X-Part-SHA256": "zz"}).status_code == 422
    # The right digest is accepted and remembered with the part.
    right = hashlib.sha256(PART_A).hexdigest()
    resp = _put(alice, conv, upload_id, 0, PART_A, **{"X-Part-SHA256": right.upper()})
    assert resp.status_code == 200, resp.text
    assert db.get_upload_session(upload_id)["accepted_parts"]["0"]["sha256"] == right


def test_a_repeated_part_counts_once(alice, conv):
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, PART_A).status_code == 200
    resp = _put(alice, conv, upload_id, 0, PART_A)
    assert resp.status_code == 200
    assert resp.json() == {
        "received": len(PART_A), "accepted_parts": [0], "bytes_received": len(PART_A)
    }
    # Replaced, not appended: a re-sent part of a different length is the
    # new truth for that index and the total moves with it.
    resp = _put(alice, conv, upload_id, 0, b"xyz")
    assert resp.json()["bytes_received"] == 3
    assert _listing(conv, upload_id) == ["0"]


def test_parts_out_of_order_and_the_discovery_call(alice, conv):
    upload_id = _upload_id(alice, conv, size=len(PAYLOAD), parts=3, part_size=2000)
    assert _put(alice, conv, upload_id, 2, b"tail").status_code == 200
    resp = _put(alice, conv, upload_id, 0, PART_A)
    assert resp.json()["accepted_parts"] == [0, 2]
    resp = alice.get(f"/uploads/chunked/{conv}/{upload_id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["upload_id"] == upload_id and body["status"] == "uploading"
    assert body["filename"] == "big.pdf" and body["purpose"] == "document"
    assert body["expected_bytes"] == len(PAYLOAD)
    assert body["expected_parts"] == 3 and body["part_size"] == 2000
    assert body["accepted_parts"] == [0, 2]
    assert body["bytes_received"] == len(PART_A) + 4
    assert body["expires_at"] and body["result"] is None


def test_the_discovery_call_reports_what_the_disk_holds(alice, conv):
    """The row says a part is there; the workspace sweep took it. The disk
    wins, so a resume sends it again instead of assembling a hole."""
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, PART_A).status_code == 200
    assert _put(alice, conv, upload_id, 1, PART_B).status_code == 200
    os.unlink(os.path.join(_parts_dir(conv, upload_id), "0"))
    body = alice.get(f"/uploads/chunked/{conv}/{upload_id}").json()
    assert body["accepted_parts"] == [1] and body["bytes_received"] == len(PART_B)
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 409 and resp.json()["missing_parts"] == [0]


def test_part_index_is_bounded_by_the_declaration(alice, conv):
    upload_id = _upload_id(alice, conv, parts=2)
    assert _put(alice, conv, upload_id, 2, b"x").status_code == 400
    assert _put(alice, conv, upload_id, up._MAX_PARTS, b"x").status_code == 400
    assert _put(alice, conv, upload_id, 1, b"x").status_code == 200


def test_the_running_total_is_capped_by_purpose(alice, conv, monkeypatch):
    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(settings, "upload_max_mb", 1)
    monkeypatch.setattr(settings, "video_max_upload_mb", 3)
    big = b"\x00" * (MiB + MiB // 2)  # 1.5 MiB

    doc = _upload_id(alice, conv)
    resp = _put(alice, conv, doc, 0, big)
    assert resp.status_code == 413 and "1 MB" in resp.json()["detail"]
    assert _listing(conv, doc) == []

    vid = _upload_id(alice, conv, filename="a.mp4", purpose="video")
    assert _put(alice, conv, vid, 0, big).status_code == 200
    assert _put(alice, conv, vid, 1, big).status_code == 200, "3 MiB is the video cap"
    resp = _put(alice, conv, vid, 2, big)
    assert resp.status_code == 413 and "video" in resp.json()["detail"]
    assert _listing(conv, vid) == ["0", "1"]
    # Replacing an index counts once: re-sending part 1 is not 4.5 MiB.
    assert _put(alice, conv, vid, 1, big).status_code == 200


# ── complete ────────────────────────────────────────────────────────────────


def test_complete_reassembles_byte_for_byte_and_stores_the_result(alice, conv):
    upload_id = _upload_id(alice, conv, size=len(PAYLOAD), parts=2, part_size=len(PART_A))
    assert _put(alice, conv, upload_id, 0, PART_A).status_code == 200
    assert _put(alice, conv, upload_id, 1, PART_B).status_code == 200
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["upload_id"] == upload_id and body["bytes"] == len(PAYLOAD)
    root = up.upload_root(conv, upload_id)
    with open(os.path.join(root, "_original", "big.pdf"), "rb") as fh:
        assert fh.read() == PAYLOAD
    assert not os.path.isdir(os.path.join(root, up._PARTS_DIR)), "parts are gone once assembled"
    row = db.get_upload_session(upload_id)
    assert row["status"] == "complete" and row["result"] == body and row["completed_at"]
    status = alice.get(f"/uploads/chunked/{conv}/{upload_id}").json()
    assert status["status"] == "complete" and status["result"] == body
    assert status["accepted_parts"] == [0, 1] and status["bytes_received"] == len(PAYLOAD)


def test_a_missing_final_part_is_a_refusal_not_a_shorter_file(alice, conv):
    """Review T-04, case one: THE case the old rail got wrong. Parts 0 and 1
    of 3 are contiguous, so it assembled two thirds of the file and called
    it done. With `parts` declared at init the hole at the end is visible."""
    upload_id = _upload_id(alice, conv, parts=3)
    assert _put(alice, conv, upload_id, 0, PART_A).status_code == 200
    assert _put(alice, conv, upload_id, 1, PART_B).status_code == 200
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 409, resp.text
    assert resp.json() == {
        "detail": "parts are missing", "missing_parts": [2], "accepted_parts": [0, 1]
    }
    # Not terminal: the row is back to uploading, the parts are still there,
    # and sending the last piece finishes the job.
    assert db.get_upload_session(upload_id)["status"] == "uploading"
    assert _listing(conv, upload_id) == ["0", "1"]
    assert _put(alice, conv, upload_id, 2, b"tail").status_code == 200
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 200, resp.text
    assert resp.json()["bytes"] == len(PAYLOAD) + 4


def test_a_hole_in_the_middle_is_listed(alice, conv):
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, b"aa").status_code == 200
    assert _put(alice, conv, upload_id, 2, b"cc").status_code == 200
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 409
    assert resp.json()["missing_parts"] == [1] and resp.json()["accepted_parts"] == [0, 2]


def test_a_declared_part_size_checks_the_shape_without_a_part_count(alice, conv):
    """No `parts`, but `part_size`: every part but the last must be exactly
    that size, and the last at most. A part that is not is the one to resend."""
    upload_id = _upload_id(alice, conv, part_size=10)
    assert _put(alice, conv, upload_id, 0, b"0123456789").status_code == 200
    assert _put(alice, conv, upload_id, 1, b"short").status_code == 200
    assert _put(alice, conv, upload_id, 2, b"0123456789x").status_code == 200
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 409
    assert resp.json()["missing_parts"] == [1, 2]
    assert _put(alice, conv, upload_id, 1, b"abcdefghij").status_code == 200
    assert _put(alice, conv, upload_id, 2, b"tail").status_code == 200
    assert alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete").status_code == 200


def test_a_wrong_total_is_refused_when_size_was_declared(alice, conv):
    upload_id = _upload_id(alice, conv, size=len(PAYLOAD))
    assert _put(alice, conv, upload_id, 0, PART_A).status_code == 200
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["bytes_received"] == len(PART_A) and body["accepted_parts"] == [0]
    assert str(len(PAYLOAD)) in body["detail"]
    assert db.get_upload_session(upload_id)["status"] == "uploading"


def test_complete_with_no_parts_is_400(alice, conv):
    upload_id = _upload_id(alice, conv)
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 400
    assert db.get_upload_session(upload_id)["status"] == "uploading"


def test_complete_twice_answers_from_the_stored_result(alice, conv, monkeypatch):
    calls = []
    real = up._finalise_document

    async def counted(*args, **kwargs):
        calls.append(1)
        return await real(*args, **kwargs)

    monkeypatch.setattr(up, "_finalise_document", counted)
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, PAYLOAD).status_code == 200
    first = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    second = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert first.status_code == 200 == second.status_code
    assert first.json() == second.json()
    assert len(calls) == 1, "the second call replays; it does not finalise again"
    # And a part after completion is 409: the bytes belong to the conversation.
    assert _put(alice, conv, upload_id, 1, b"x").status_code == 409


def test_two_concurrent_completes_finalise_once(alice, login_client, conv, monkeypatch):
    """One caller wins the row; the other waits on it and returns the same
    body. The finaliser is held open on a gate so the second request is
    provably inside the wait, not merely second in line."""
    gate = threading.Event()
    calls = []
    real = up._finalise_document

    async def held(*args, **kwargs):
        calls.append(1)
        await asyncio.to_thread(gate.wait, 15)
        return await real(*args, **kwargs)

    monkeypatch.setattr(up, "_finalise_document", held)
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, PAYLOAD).status_code == 200
    alice_again = login_client("alice")
    url = f"/uploads/chunked/{conv}/{upload_id}/complete"
    results: dict = {}

    def run(name, client):
        results[name] = client.post(url)

    first = threading.Thread(target=run, args=("first", alice))
    first.start()
    try:
        for _ in range(250):
            if db.get_upload_session(upload_id)["status"] == "finalizing":
                break
            time.sleep(0.02)
        else:
            pytest.fail("the first complete never took the row")
        second = threading.Thread(target=run, args=("second", alice_again))
        second.start()
        time.sleep(0.7)
        assert "second" not in results, "the second caller waits for the winner"
        assert len(calls) == 1
    finally:
        gate.set()
    first.join(20)
    second.join(20)
    assert results["first"].status_code == 200, results["first"].text
    assert results["second"].status_code == 200, results["second"].text
    assert results["first"].json() == results["second"].json()
    assert len(calls) == 1, "finalisation ran exactly once"
    assert db.get_upload_session(upload_id)["status"] == "complete"


def test_a_rejected_finalisation_is_terminal_and_replays(alice, conv, monkeypatch):
    """The assembled total is measured against the purpose's cap at
    complete as well (the cap was lowered under a session here); the refusal
    is remembered and a retried complete gets the same 413, not a 404."""
    monkeypatch.setattr(settings, "upload_max_mb", 3)
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, b"\x00" * MiB).status_code == 200
    assert _put(alice, conv, upload_id, 1, b"\x00" * MiB).status_code == 200
    monkeypatch.setattr(settings, "upload_max_mb", 1)
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 413, resp.text
    row = db.get_upload_session(upload_id)
    assert row["status"] == "rejected" and "1 MB" in row["error"]
    assert _listing(conv, upload_id) == [], "a rejected session keeps no bytes"
    again = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert again.status_code == 413 and again.json() == resp.json()
    status = alice.get(f"/uploads/chunked/{conv}/{upload_id}").json()
    assert status["status"] == "rejected" and status["error"] == resp.json()["detail"]
    assert status["result"] is None
    assert _put(alice, conv, upload_id, 2, b"x").status_code == 404


def test_the_video_cap_applies_at_complete(alice, conv, monkeypatch):
    monkeypatch.setattr(settings, "video_analysis_enabled", True)
    monkeypatch.setattr(settings, "upload_max_mb", 1)
    monkeypatch.setattr(settings, "video_max_upload_mb", 3)
    upload_id = _upload_id(alice, conv, filename="a.mp4", purpose="video")
    assert _put(alice, conv, upload_id, 0, b"\x00" * (2 * MiB)).status_code == 200
    monkeypatch.setattr(settings, "video_max_upload_mb", 1)
    resp = alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete")
    assert resp.status_code == 413 and "video" in resp.json()["detail"]
    assert db.get_upload_session(upload_id)["status"] == "rejected"


# ── cancel ──────────────────────────────────────────────────────────────────


def test_cancel_reclaims_the_parts_and_is_idempotent(alice, conv):
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, PART_A).status_code == 200
    resp = alice.delete(f"/uploads/chunked/{conv}/{upload_id}")
    assert resp.status_code == 204, resp.text
    assert not os.path.isdir(_parts_dir(conv, upload_id))
    assert db.get_upload_session(upload_id)["status"] == "cancelled"
    assert alice.delete(f"/uploads/chunked/{conv}/{upload_id}").status_code == 204
    assert alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete").status_code == 404
    assert _put(alice, conv, upload_id, 1, PART_B).status_code == 404
    assert alice.get(f"/uploads/chunked/{conv}/{upload_id}").json()["status"] == "cancelled"


def test_cancel_is_refused_once_the_bytes_belong_to_the_conversation(alice, conv):
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, PAYLOAD).status_code == 200
    assert alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete").status_code == 200
    assert alice.delete(f"/uploads/chunked/{conv}/{upload_id}").status_code == 409
    root = up.upload_root(conv, upload_id)
    assert os.path.isfile(os.path.join(root, "_original", "big.pdf"))


# ── ownership ───────────────────────────────────────────────────────────────


def test_another_user_sees_404_everywhere_even_knowing_both_ids(alice, bob, conv):
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, PART_A).status_code == 200
    base = f"/uploads/chunked/{conv}/{upload_id}"
    assert bob.put(f"{base}/part/1", content=PART_B).status_code == 404
    assert bob.get(base).status_code == 404
    assert bob.post(f"{base}/complete").status_code == 404
    assert bob.delete(base).status_code == 404
    # Nothing moved: alice's session is exactly as she left it.
    row = db.get_upload_session(upload_id)
    assert row["status"] == "uploading" and set(row["accepted_parts"]) == {"0"}
    # Bob's own conversation cannot borrow alice's session id either.
    assert bob.post("/history/conversations", json={"id": "conv-bob", "title": "b"}).status_code == 200
    assert bob.get(f"/uploads/chunked/conv-bob/{upload_id}").status_code == 404
    assert bob.put(f"/uploads/chunked/conv-bob/{upload_id}/part/1", content=b"x").status_code == 404


def test_a_forged_or_malformed_id_is_404(alice, conv):
    forged = "f" * 32
    assert alice.get(f"/uploads/chunked/{conv}/{forged}").status_code == 404
    assert alice.get(f"/uploads/chunked/{conv}/not-hex").status_code == 404
    assert _put(alice, conv, forged, 0, b"x").status_code == 404
    assert alice.delete(f"/uploads/chunked/{conv}/{forged}").status_code == 404
    assert not os.path.isdir(up.upload_root(conv, forged))


# ── expiry ──────────────────────────────────────────────────────────────────


def _age(upload_id: str) -> None:
    with db.connection() as con:
        con.execute(
            "UPDATE upload_sessions SET expires_at = now() - interval '1 hour' WHERE id = %s",
            (upload_id,),
        )


def test_the_sweep_reclaims_only_expired_open_sessions(alice, conv):
    stale = _upload_id(alice, conv)
    live = _upload_id(alice, conv)
    done = _upload_id(alice, conv)
    busy = _upload_id(alice, conv)
    for upload_id in (stale, live, done, busy):
        assert _put(alice, conv, upload_id, 0, PAYLOAD).status_code == 200
    assert alice.post(f"/uploads/chunked/{conv}/{done}/complete").status_code == 200
    assert db.try_begin_upload_finalize(busy) == "uploading"  # somebody's assembly
    for upload_id in (stale, done, busy):
        _age(upload_id)

    assert up.sweep_expired_upload_sessions() == 1

    assert db.get_upload_session(stale)["status"] == "expired"
    assert not os.path.isdir(_parts_dir(conv, stale))
    assert alice.get(f"/uploads/chunked/{conv}/{stale}").status_code == 404
    assert _put(alice, conv, stale, 1, b"x").status_code == 404
    assert alice.post(f"/uploads/chunked/{conv}/{stale}/complete").status_code == 404

    assert db.get_upload_session(live)["status"] == "uploading"
    assert _listing(conv, live) == ["0"]
    assert db.get_upload_session(done)["status"] == "complete"
    assert os.path.isfile(os.path.join(up.upload_root(conv, done), "_original", "big.pdf"))
    assert db.get_upload_session(busy)["status"] == "finalizing"
    assert _listing(conv, busy) == ["0"], "a finalizing session is never touched"


def test_the_sweep_rides_on_the_next_upload(alice, conv):
    stale = _upload_id(alice, conv)
    assert _put(alice, conv, stale, 0, PART_A).status_code == 200
    _age(stale)
    up._last_sweep_at = 0.0
    _upload_id(alice, conv)  # any init is a chance to sweep
    assert db.get_upload_session(stale)["status"] == "expired"
    assert not os.path.isdir(_parts_dir(conv, stale))


def test_removing_a_session_takes_only_its_own_parts_directory(alice, conv):
    """The one function the sweep, cancel and complete all delete through:
    exactly `upload_root(conv, id)/_parts`, never `_original`, never a
    neighbour, and nothing a hostile conversation id could point outside
    the uploads tree."""
    upload_id = _upload_id(alice, conv)
    other = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, PART_A).status_code == 200
    assert _put(alice, conv, other, 0, PART_A).status_code == 200
    root = up.upload_root(conv, upload_id)
    original = os.path.join(root, "_original", "big.pdf")
    os.makedirs(os.path.dirname(original))
    with open(original, "wb") as fh:
        fh.write(b"kept")
    up.remove_session_parts(conv, upload_id)
    assert not os.path.isdir(os.path.join(root, up._PARTS_DIR))
    assert os.path.isfile(original), "_original is not a session's to remove"
    assert _listing(conv, other) == ["0"], "the neighbour is untouched"
    # A traversal-shaped conversation id and a forged upload id reach nothing.
    up.remove_session_parts("../../" + conv, upload_id)
    up.remove_session_parts(conv, "not-hex")
    assert _listing(conv, other) == ["0"]
    assert os.path.isdir(settings.workspace_dir)


# ── metrics ─────────────────────────────────────────────────────────────────


def test_the_session_and_part_metrics_are_emitted(alice, conv):
    metrics.reset()
    upload_id = _upload_id(alice, conv)
    assert _put(alice, conv, upload_id, 0, PART_A).status_code == 200
    assert _put(alice, conv, upload_id, 1, PART_B).status_code == 200
    assert alice.post(f"/uploads/chunked/{conv}/{upload_id}/complete").status_code == 200
    cancelled = _upload_id(alice, conv)
    assert alice.delete(f"/uploads/chunked/{conv}/{cancelled}").status_code == 204
    text = metrics.render()
    assert 'upload_session_total{purpose="document",result="complete"} 1' in text
    assert 'upload_session_total{purpose="document",result="cancelled"} 1' in text
    assert f'upload_part_bytes_total{{purpose="document"}} {len(PAYLOAD)}' in text
    assert 'upload_finalize_seconds_count{purpose="document"} 1' in text
