"""Second hardening round on the recording-session server (2026-09-29).

Each test reproduces a defect an adversarial review found on f94fbcc0 and
fails there; the review's own tests were tests/test_voice_edges_attack.py in
worktree wf_ba7786d0-c32-12.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict

from app import db, dictation
from tests.test_voice_sessions import (  # noqa: F401  (voice is a fixture)
    create,
    parts_of,
    put,
    recording,
    sha,
    voice,
    wait_done,
)


def _source(voice, uid: int, sid: str) -> str:
    return os.path.join(voice.root, str(uid), sid, "source.wav")


# -- A. an acknowledged part must never be cut off the stored file ---------


def test_a_part_stored_between_lease_claim_and_restore_survives(voice, login_client, monkeypatch):
    """_Live.run reads bytes_stored at the lease claim; _restore used to cut
    source.<ext> to that stale number without the part lock, so a part
    stored in between was erased after its 200 and every later part got 503
    ("source is 0 bytes, the row acknowledges 160000")."""
    _script, data = recording(voice.tmp, 40.0, seed=3)
    alice = login_client("alice")
    uid = int(db.get_user_by_username("alice")["id"])
    chunks = parts_of(data)
    real_claim = dictation._claim_lease
    landed: Dict[str, Any] = {}

    def claim_then_a_part_lands(session_id):
        row = real_claim(session_id)
        if row is not None and "row" not in landed:
            landed["row"], _dup = dictation.append_part(uid, session_id, 0, chunks[0], sha(chunks[0]))
        return row

    monkeypatch.setattr(dictation, "_claim_lease", claim_then_a_part_lands)
    sid = create(alice).json()["session_id"]
    deadline = time.monotonic() + 10
    while "row" not in landed:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    time.sleep(1.0)
    acknowledged = int(landed["row"]["bytes_stored"])
    assert os.path.getsize(_source(voice, uid, sid)) == acknowledged
    assert put(alice, sid, 1, chunks[1]).status_code == 200
    r = alice.post(f"/audio/sessions/{sid}/finish", json={"last_part": 1, "ended_by": "person"})
    assert r.status_code in (200, 202), r.text
    done = wait_done(alice, sid, timeout=60)
    assert done["status"] == "done", done
    assert os.path.getsize(_source(voice, uid, sid)) == len(chunks[0]) + len(chunks[1])


def test_a_stored_file_shorter_than_its_row_ends_instead_of_spinning(voice, login_client):
    """If the file ever holds less than the row acknowledges, _ingest read an
    empty chunk, slept 0.2 s and looped for ever without re-reading the row:
    the session stayed 'finishing', held a VOICE_SESSION_MAX_ACTIVE slot and
    resumed the loop after every restart."""
    _script, data = recording(voice.tmp, 20.0, seed=5)
    alice = login_client("alice")
    uid = int(db.get_user_by_username("alice")["id"])
    chunks = parts_of(data)
    sid = create(alice).json()["session_id"]
    assert put(alice, sid, 0, chunks[0]).status_code == 200
    dictation.RUNNER.stop_all()
    # The loss the review produced, made directly: the file keeps the first
    # half of what its row acknowledges.
    with db.connection() as con:
        con.execute("UPDATE voice_sessions SET status = 'finishing', ended_by = 'person' WHERE id = %s", (sid,))
    with open(_source(voice, uid, sid), "r+b") as fh:
        fh.truncate(len(chunks[0]) // 2)
    dictation.RUNNER.ensure(sid)
    t0 = time.monotonic()
    done = wait_done(alice, sid, timeout=30)
    assert done["status"] in ("done", "failed"), done
    assert time.monotonic() - t0 < 20
    with db.connection() as con:
        live = con.execute(
            "SELECT count(*) AS n FROM voice_sessions WHERE id = %s AND status IN ('recording','finishing')", (sid,)
        ).fetchone()["n"]
    assert live == 0
