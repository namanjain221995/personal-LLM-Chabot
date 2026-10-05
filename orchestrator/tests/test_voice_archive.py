"""The voice archive (app/voice_archive.py, V43): finished recordings' audio
moves to the store on the worker's disk, and nothing a person can do with a
recording depends on where it is.

The owner asked (2026-09-30) for the stored audio to move off the head's
disk, which it shares with production Postgres, to the worker's 3.7 TB disk.
Only a finished recording's source file moves; the transcript and the list
stay here. These tests hold the move to what makes it safe:

  * the head's copy is deleted only after the store's copy was read back and
    hashed, on conditions re-checked in the same UPDATE, under the lock a
    restore takes too;
  * a retranscription, a continuation, playback and a download all work on a
    moved recording, and a store that is down refuses only what needs it,
    with a sentence that says nothing is lost;
  * a deleted recording is deleted on the store too, now or on a later pass;
  * the reconcile finds what the store lost and what nobody owns.

THE STORE is compose/voice-store/server.py itself, in-process through
httpx.ASGITransport, on the same fake network as the speech engine
(tests/publicapi_fake_whisper). Nothing here needs the worker.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import concurrent.futures
import contextlib
import errno
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import psycopg
import pytest
from starlette.requests import Request

from app import db, dictation, metrics, voice_archive
from app.config import settings
from tests.test_voice_sessions import (  # noqa: F401 - `voice` is a fixture
    PART_BYTES,
    SR,
    create,
    dictate,
    parts_of,
    put,
    recording,
    sha,
    voice,
    wait_done,
)

REPO = Path(__file__).resolve().parents[2]
STORE_HOST = "voice-store.internal"
#: Low-entropy on purpose: secret scanners flag random-looking literals.
TOKEN = "voice-archive-test-token-" + "a" * 40
#: "Due at once": a grace of MINUS five seconds, not 0. finished_at is written
#: with this host's clock (dictation._now) and the claim compares it with the
#: DATABASE's now(); the private test database runs on the worker, whose clock
#: trailed this host's by 12-33 ms (measured 2026-09-30), so with 0 a recording
#: finished a moment ago is still "in the future" there and nothing is claimed.
#: In production both run on one host, and the grace is a day.
DUE_NOW = -5.0


def _load_store_server():
    path = REPO / "compose" / "voice-store" / "server.py"
    spec = importlib.util.spec_from_file_location("voice_store_server_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


STORE = _load_store_server()


class StoreControl:
    """The store under test, and the switches a test flips on its network."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.app = STORE.create_app(STORE.Settings(root=str(root), tokens=(TOKEN,), min_free_bytes=0), background=False)
        self.store = self.app.state.store
        self.asgi = httpx.ASGITransport(app=self.app)
        self.down = False
        #: request -> a Response to answer instead of the store, or None.
        self.fail: Optional[Callable[[httpx.Request], Optional[httpx.Response]]] = None
        self.requests: List[Tuple[str, str]] = []

    def objects(self) -> List[Tuple[int, str]]:
        for _attempt in range(20):
            try:
                return sorted(
                    (int(p.parent.parent.name), p.parent.name) for p in self.root.glob("*/*/source.*")
                )
            except FileNotFoundError:
                time.sleep(0.01)  # a delete removed a folder mid-walk
        raise AssertionError("the store kept changing under the walk")

    def puts(self) -> int:
        return sum(1 for method, _path in self.requests if method == "PUT")


class _Routed(httpx.AsyncBaseTransport):
    def __init__(self, fleet: httpx.AsyncBaseTransport, control: StoreControl) -> None:
        self.fleet = fleet
        self.control = control

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != STORE_HOST:
            return await self.fleet.handle_async_request(request)
        control = self.control
        control.requests.append((request.method, request.url.path))
        if control.down:
            raise httpx.ConnectError("connection refused", request=request)
        if control.fail is not None:
            answer = control.fail(request)
            if answer is not None:
                await request.aread()
                return answer
        return await control.asgi.handle_async_request(request)


@pytest.fixture()
def archive(voice, monkeypatch, tmp_path):
    """`voice` (a deployment with recording sessions and a fake engine) plus
    the store, reachable at http://voice-store.internal:30011."""
    control = StoreControl(tmp_path / "worker-store")
    routed = _Routed(voice.fleet.transport(), control)
    real = httpx._client.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = routed
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    monkeypatch.setattr(settings, "voice_archive_url", f"http://{STORE_HOST}:30011")
    monkeypatch.setattr(settings, "voice_archive_token", TOKEN)
    monkeypatch.setattr(settings, "voice_archive_tls_cert_b64", "")
    # The tests run the passes themselves; the lifespan must not.
    monkeypatch.setattr(settings, "voice_archive_enabled", False)
    monkeypatch.setattr(settings, "voice_archive_after_s", DUE_NOW)
    monkeypatch.setattr(settings, "voice_archive_rate_bytes_per_s", 1 << 40)
    monkeypatch.setattr(settings, "voice_archive_batch", 20)
    # A restore that met a failing store pauses restores in this process; one
    # test's outage must not refuse the next test's restore.
    monkeypatch.setattr(voice_archive, "_restore_failed_at", None)
    yield control
    # No worker may still be writing when the next test's TRUNCATE runs: a
    # worker's V19 INSERT against that TRUNCATE is a deadlock.
    all_let_go()
    run(voice_archive.close_client())


def run(coro, timeout: float = 60.0):
    """On the archive's own loop, as production runs it."""
    return voice_archive.ARCHIVER.submit(coro).result(timeout)


def row_of(sid: str) -> Dict[str, Any]:
    with db.connection() as con:
        return dict(con.execute(f"SELECT {dictation._COLUMNS} FROM voice_sessions WHERE id = %s", (sid,)).fetchone())


def sql(statement: str, *params: Any) -> None:
    with db.connection() as con:
        con.execute(statement, params)


def uid_of(name: str) -> int:
    return int(db.get_user_by_username(name)["id"])


def let_go(sid: str, *, timeout: float = 30.0) -> None:
    """Until this process's session worker has let go of `sid`'s row.

    wait_done returns once the ROW says done, while the worker is still
    unwinding: it records the V19 attempt (an INSERT into
    voice_transcriptions), then clears its lease (an UPDATE of this row).
    A pass in between found the row locked and skipped it (the claim is FOR
    UPDATE SKIP LOCKED), and the next test's TRUNCATE could deadlock with
    that INSERT: about 1 full run of this file in 6 failed (review
    2026-10-01). The lease is cleared last, so a row with no lease and no
    worker is one nothing in this process still writes."""
    deadline = time.monotonic() + timeout
    while sid in dictation.RUNNER.live or row_of(sid)["lease_owner"] is not None:
        assert time.monotonic() < deadline, f"the session worker still holds {sid}"
        time.sleep(0.01)


def all_let_go(*, timeout: float = 60.0) -> None:
    """`let_go` for every row this process's session workers hold."""
    deadline = time.monotonic() + timeout
    while True:
        with db.connection() as con:
            held = [r["id"] for r in con.execute(
                "SELECT id FROM voice_sessions WHERE lease_owner = %s", (dictation._OWNER,)
            ).fetchall()]
        if not held and not dictation.RUNNER.live:
            return
        assert time.monotonic() < deadline, f"session workers still hold {held or list(dictation.RUNNER.live)}"
        time.sleep(0.01)


def settled(client, sid: str) -> Dict[str, Any]:
    """wait_done, and then until the worker has let go of the row."""
    done = wait_done(client, sid)
    let_go(sid)
    return done


def finished(voice_env, client, seconds: float = 8.0, *, seed: int = 7) -> Tuple[str, bytes]:
    _script, data = recording(voice_env.tmp, seconds, seed=seed)
    sid, done, _ = dictate(client, data)
    assert done["status"] == "done", done
    let_go(sid)
    return sid, data


def head_files(voice_env, name: str, sid: str) -> List[str]:
    return sorted(os.listdir(os.path.join(voice_env.root, str(uid_of(name)), sid)))


# ------------------------------------------------------------ the schema --


def test_v43_adds_the_archive_columns_with_every_existing_row_local_and_runs_twice_cleanly(voice, login_client):
    assert db.LATEST_SCHEMA_VERSION >= 43
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    row = row_of(sid)
    assert row["archive_state"] == "local" and row["archive_attempts"] == 0
    for column in ("archived_at", "head_released_at", "head_hold_until", "archive_next_at", "archive_error", "remote_purged_at"):
        assert row[column] is None, column
    with db.connection() as con:
        with con.transaction():
            con.execute(db._MIGRATION_V43)
            con.execute(db._MIGRATION_V43)
    with pytest.raises(Exception):
        sql("UPDATE voice_sessions SET archive_state = 'elsewhere' WHERE id = %s", sid)
    with db.connection() as con:
        indexes = {r["indexname"] for r in con.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'voice_sessions'"
        ).fetchall()}
    assert {"idx_voice_sessions_archive_due", "idx_voice_sessions_archive_copied", "idx_voice_sessions_archive_purge"} <= indexes


#: What a database whose V43 is not this one looks like, after the released
#: V43 ran: the voice archive's draft (V43 recorded, no owner table, the old
#: purge index), and the dropped meeting-transcripts branch's V43 (V43
#: recorded, none of the archive's columns: that branch made voice_sessions.kind).
_FOREIGN_V43 = {
    "draft": (
        "DROP TABLE voice_archive_owner; DROP INDEX idx_voice_sessions_archive_purge; "
        "CREATE INDEX idx_voice_sessions_archive_purge ON voice_sessions (audio_deleted_at) "
        "WHERE audio_deleted_at IS NOT NULL AND archive_state <> 'local' AND remote_purged_at IS NULL;",
        "a draft of the voice archive's V43",
    ),
    "another_branch": (
        "DROP TABLE voice_archive_owner; ALTER TABLE voice_sessions DROP CONSTRAINT voice_sessions_archive_state, "
        "DROP COLUMN archive_state, DROP COLUMN archived_at, DROP COLUMN head_released_at, "
        "DROP COLUMN head_hold_until, DROP COLUMN archive_attempts, DROP COLUMN archive_next_at, "
        "DROP COLUMN archive_error, DROP COLUMN remote_purged_at; "
        "ALTER TABLE voice_sessions ADD COLUMN kind text NOT NULL DEFAULT 'dictation';",
        "another branch's migration numbered 43",
    ),
}


@pytest.mark.parametrize("shape", sorted(_FOREIGN_V43))
def test_a_database_whose_v43_is_not_this_one_is_refused_at_start_up_with_what_to_do(monkeypatch, shape):
    """V43 was edited in place before release (the owner table and the purge
    index's predicate came after a draft had run on test databases), and the
    dropped meeting-transcripts branches numbered a different migration 43.
    The runner keeps no checksum: such a database said "V43", started
    cleanly, and then every store call that names the owner raised
    UndefinedTable (review 2026-10-01; ten such test databases found).
    Start-up now refuses it, says which it is and what to do, and doing that
    brings it to this V43."""
    leftover, diagnosis = _FOREIGN_V43[shape]
    first = db.dsn()
    other = _empty_database(f"v43_{shape}")
    try:
        monkeypatch.setattr(settings, "app_database_url", other)
        db.init_schema()
        db.init_schema()  # this V43, applied before this start-up: nothing to say
        with db.connection() as con:
            con.execute(leftover)
        with pytest.raises(RuntimeError) as refused:
            db.init_schema()
        message = str(refused.value)
        assert diagnosis in message and "drop it" in message and db.V43_REPAIR in message, message
        with db.connection() as con:
            assert con.execute("SELECT to_regclass('voice_archive_owner') AS t").fetchone()["t"] is None
            con.execute(db.V43_REPAIR)  # what the message says to run
        db.init_schema()
        with db.connection() as con:
            assert con.execute("SELECT max(version) AS v FROM schema_migrations").fetchone()["v"] == db.LATEST_SCHEMA_VERSION
            assert con.execute("SELECT to_regclass('voice_archive_owner') AS t").fetchone()["t"] is not None
            purge = con.execute(
                "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_voice_sessions_archive_purge'"
            ).fetchone()["indexdef"]
            columns = {r["column_name"] for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'voice_sessions'"
            ).fetchall()}
        assert "archive_state" not in purge, purge
        assert {"archive_state", "archived_at", "head_released_at", "head_hold_until", "archive_attempts",
                "archive_next_at", "archive_error", "remote_purged_at"} <= columns
        db.init_schema()
    finally:
        monkeypatch.setattr(settings, "app_database_url", first)
        db.close_pool()
        _drop_database(other)


def test_the_store_accepts_exactly_the_extensions_dictation_stores():
    tree = ast.parse((REPO / "compose" / "voice-store" / "server.py").read_text(encoding="utf-8"))
    literal = next(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "EXTENSIONS" for t in node.targets)
    )
    store_extensions = set(ast.literal_eval(literal.args[0]))
    assert store_extensions == set(dictation._EXTENSIONS.values())


def test_the_pooled_client_gives_up_an_idle_connection_before_the_store_closes_it(monkeypatch):
    """A kept-alive connection that the store closes just as the client reuses
    it fails a request with nothing wrong: a person seeking in a moved
    recording is told the archive "isn't answering" (503). httpx keeps an idle
    connection 5 s by default and uvicorn closes one after timeout_keep_alive,
    which the store set to 5 s too: equal, the worst case. The client must let
    an idle connection go well before the store does."""
    tree = ast.parse((REPO / "compose" / "voice-store" / "server.py").read_text(encoding="utf-8"))
    run_call = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run"
        and getattr(node.func.value, "id", None) == "uvicorn"
    )
    keep_alive = {k.arg: k.value for k in run_call.keywords}["timeout_keep_alive"]
    assert isinstance(keep_alive, ast.Name) and keep_alive.id == "KEEP_ALIVE_TIMEOUT_S", (
        "the store's keep-alive is the constant this test compares with"
    )

    monkeypatch.setattr(settings, "voice_archive_url", f"http://{STORE_HOST}:30011")
    monkeypatch.setattr(settings, "voice_archive_token", TOKEN)
    monkeypatch.setattr(settings, "voice_archive_tls_cert_b64", "")

    async def pooled_expiry() -> Optional[float]:
        client = await voice_archive._client()
        try:
            return client._transport._pool._keepalive_expiry
        finally:
            await voice_archive.close_client()

    expiry = asyncio.run(pooled_expiry())
    assert expiry is not None and expiry <= STORE.KEEP_ALIVE_TIMEOUT_S / 2, (expiry, STORE.KEEP_ALIVE_TIMEOUT_S)


def test_every_archive_metric_label_is_a_closed_set_the_module_agrees_with():
    closed = metrics._LABELS_BY_METRIC
    assert closed["voice_archive_errors_total"]["reason"] == set(voice_archive.ERROR_REASONS)
    assert closed["voice_archive_proxy_total"]["result"] == set(voice_archive.PROXY_RESULTS)
    assert closed["voice_archive_restored_total"]["result"] == set(voice_archive.RESTORE_RESULTS)
    assert closed["voice_archive_reconcile_total"]["result"] == set(voice_archive.RECONCILE_RESULTS)
    metrics.inc("voice_archive_errors_total", reason=uuid.uuid4().hex)
    assert 'voice_archive_errors_total{reason="other"}' in metrics.render()


# --------------------------------------------------------------- moving --


def test_a_finished_recording_moves_after_its_grace_and_only_its_audio_leaves_the_head(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid, data = finished(voice, alice)
    monkeypatch.setattr(settings, "voice_archive_after_s", 3600.0)
    assert run(voice_archive.archive_once()) == {}, "inside its grace nothing moves"
    assert archive.objects() == [] and row_of(sid)["archive_state"] == "local"

    monkeypatch.setattr(settings, "voice_archive_after_s", DUE_NOW)
    stats = run(voice_archive.archive_once())
    assert stats == {"archived": 1}, stats
    row = row_of(sid)
    assert row["archive_state"] == "archived" and row["archived_at"] and row["head_released_at"]
    assert archive.objects() == [(uid_of("alice"), sid)]
    stored = archive.root / str(uid_of("alice")) / sid / "source.wav"
    assert stored.read_bytes() == data and row["source_sha256"] == sha(data)
    names = head_files(voice, "alice", sid)
    assert "source.wav" not in names, "the head's copy is released"
    assert {"transcript.json", "transcript.txt", "parts.jsonl", "plan.jsonl", "results.jsonl"} <= set(names)
    state = alice.get(f"/audio/sessions/{sid}").json()
    assert state["stored"]["archived"] is True and state["stored"]["kept"] is True and state["text"]
    listing = alice.get("/audio/sessions").json()["sessions"]
    assert listing[0]["archived"] is True and listing[0]["preview"]
    assert dictation.stored_bytes(uid_of("alice")) == len(data), "the quota does not care where the audio is"
    assert run(voice_archive.archive_once()) == {}, "nothing is copied twice"
    assert archive.puts() == 1


def test_only_finished_kept_idle_recordings_with_audio_are_candidates(archive, voice, login_client):
    alice = login_client("alice")
    eligible, _ = finished(voice, alice, seed=1)
    others = {}
    for label, statement in (
        ("deleted", "UPDATE voice_sessions SET audio_deleted_at = now() WHERE id = %s"),
        ("retranscribing", "UPDATE voice_sessions SET retranscribe = 'all' WHERE id = %s"),
        ("empty", "UPDATE voice_sessions SET bytes_stored = 0 WHERE id = %s"),
        ("backing_off", "UPDATE voice_sessions SET archive_next_at = now() + interval '1 hour' WHERE id = %s"),
        ("cancelled", "UPDATE voice_sessions SET status = 'cancelled' WHERE id = %s"),
    ):
        sid, _ = finished(voice, alice, seed=len(others) + 2)
        sql(statement, sid)
        others[label] = sid
    claimed = run(db.run_in_thread(voice_archive._claim, 20, DUE_NOW))
    assert [r["id"] for r in claimed] == [eligible]
    assert run(db.run_in_thread(voice_archive._claim, 20, DUE_NOW)) == [], "a claim is a lease another pass skips"
    for sid in others.values():
        assert row_of(sid)["archive_state"] == "local"


def test_a_row_another_mover_holds_is_skipped_not_waited_for(archive, voice, login_client):
    alice = login_client("alice")
    first, _ = finished(voice, alice, seed=3)
    second, _ = finished(voice, alice, seed=4)
    import psycopg

    other = psycopg.connect(db.dsn())
    try:
        other.execute("SELECT id FROM voice_sessions WHERE id = %s FOR UPDATE", (first,))
        started = time.monotonic()
        claimed = run(db.run_in_thread(voice_archive._claim, 20, DUE_NOW))
        assert time.monotonic() - started < 5
        assert [r["id"] for r in claimed] == [second]
    finally:
        other.rollback()
        other.close()


def test_a_copy_is_read_back_before_the_head_file_goes(archive, voice, login_client):
    """The store answered 201 but kept different bytes: the read-back catches
    it, the head keeps the only good copy, and the recording backs off."""
    alice = login_client("alice")
    sid, data = finished(voice, alice)

    def corrupt_get(request: httpx.Request) -> Optional[httpx.Response]:
        if request.method == "GET" and "/v1/recordings/" in request.url.path:
            return httpx.Response(200, content=b"\x00" * len(data))
        return None

    archive.fail = corrupt_get
    stats = run(voice_archive.archive_once())
    assert stats.get("errors") == 1
    row = row_of(sid)
    assert row["archive_state"] == "local" and row["archive_error"] == "remote_sha_mismatch"
    assert "source.wav" in head_files(voice, "alice", sid)
    archive.fail = None
    sql("UPDATE voice_sessions SET archive_next_at = NULL WHERE id = %s", sid)
    assert run(voice_archive.archive_once()) == {"archived": 1}
    assert row_of(sid)["archive_attempts"] == 0 and row_of(sid)["archive_error"] is None


def test_a_head_file_that_no_longer_matches_its_sha_is_never_copied(archive, voice, login_client):
    alice = login_client("alice")
    sid, data = finished(voice, alice)
    path = dictation.source_path(row_of(sid))
    with open(path, "r+b") as fh:
        fh.seek(100)
        fh.write(b"\xff\xfe")
    assert run(voice_archive.archive_once()) == {"local_sha_mismatch": 1}
    assert archive.puts() == 0 and row_of(sid)["archive_error"] == "local_sha_mismatch"


def test_failures_back_off_per_recording_and_a_refused_token_stops_the_pass(archive, voice, login_client):
    alice = login_client("alice")
    first, _ = finished(voice, alice, seed=5)
    second, _ = finished(voice, alice, seed=6)
    archive.fail = lambda request: httpx.Response(500) if request.method == "PUT" else None
    stats = run(voice_archive.archive_once())
    assert stats["errors"] == 1, "a 5xx is the store's failure: the rest of the pass waits"
    failed = [row_of(s) for s in (first, second) if row_of(s)["archive_attempts"]]
    assert len(failed) == 1 and failed[0]["archive_error"] == "http_5xx"
    with db.connection() as con:
        delay = con.execute(
            "SELECT EXTRACT(EPOCH FROM archive_next_at - now()) AS s FROM voice_sessions WHERE id = %s", (failed[0]["id"],)
        ).fetchone()["s"]
    assert 45 <= float(delay) <= 75, "first retry after 60 s +/-20%"
    sql("UPDATE voice_sessions SET archive_next_at = NULL")
    run(voice_archive.archive_once())
    with db.connection() as con:
        second_delay = con.execute(
            "SELECT EXTRACT(EPOCH FROM archive_next_at - now()) AS s FROM voice_sessions WHERE archive_attempts = 2"
        ).fetchone()["s"]
    assert 90 <= float(second_delay) <= 150, "then 120 s"
    sql("UPDATE voice_sessions SET archive_next_at = NULL, archive_attempts = 0, archive_error = NULL")
    archive.fail = lambda request: httpx.Response(401) if request.method == "PUT" else None
    before = archive.puts()
    stats = run(voice_archive.archive_once())
    assert archive.puts() - before == 1, "a refused token stops the pass at the first recording"
    assert "voice_archive_errors_total{reason=\"auth\"}" in metrics.render()


def test_with_the_archive_unconfigured_nothing_reaches_any_store(voice, login_client, monkeypatch):
    monkeypatch.setattr(settings, "voice_archive_url", "")
    monkeypatch.setattr(settings, "voice_archive_token", "")
    alice = login_client("alice")
    sid, data = finished(voice, alice)
    assert run(voice_archive.archive_once()) == {}
    assert voice_archive.forget_remote(uid_of("alice"), sid) is None
    assert voice_archive.ARCHIVER.start() is False
    assert alice.get(f"/audio/sessions/{sid}/audio").content == data


def test_a_full_store_is_not_written_and_the_recording_waits(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    archive.store.settings = STORE.Settings(root=str(archive.root), tokens=(TOKEN,), min_free_bytes=1 << 62)
    stats = run(voice_archive.archive_once())
    assert stats.get("errors") == 1 and archive.puts() == 0, "the free-space check in /health stops it before a PUT"
    assert row_of(sid)["archive_error"] == "storage_full" and row_of(sid)["archive_state"] == "local"


# ---------------------------------------------------------------- reads --


def test_playing_a_moved_recording_streams_it_from_the_store_with_ranges(archive, voice, login_client):
    alice = login_client("alice")
    sid, data = finished(voice, alice)
    run(voice_archive.archive_once())
    got = alice.get(f"/audio/sessions/{sid}/audio")
    assert got.status_code == 200 and got.content == data
    assert got.headers["content-type"].startswith("audio/wav")
    assert got.headers["cache-control"] == "no-store" and got.headers["x-recording-complete"] == "true"
    assert got.headers["content-disposition"].startswith('attachment; filename="recording-')
    assert got.headers["content-length"] == str(len(data))
    ranged = alice.get(f"/audio/sessions/{sid}/audio", headers={"range": "bytes=100-199"})
    assert ranged.status_code == 206 and ranged.content == data[100:200]
    assert ranged.headers["content-range"] == f"bytes 100-199/{len(data)}"
    beyond = alice.get(f"/audio/sessions/{sid}/audio", headers={"range": f"bytes={len(data) + 10}-"})
    assert beyond.status_code == 416 and beyond.headers["content-range"] == f"bytes */{len(data)}"
    bob = login_client("bob")
    before = len(archive.requests)
    assert bob.get(f"/audio/sessions/{sid}/audio").status_code == 404
    assert len(archive.requests) == before, "ownership is checked before anything reaches the store"


def test_a_store_that_is_down_refuses_playback_only_and_says_nothing_is_lost(archive, voice, login_client):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    archive.down = True
    refused = alice.get(f"/audio/sessions/{sid}/audio")
    assert refused.status_code == 503 and refused.headers["retry-after"] == "30"
    body = refused.json()
    assert body["reason"] == "archive_unavailable" and "Nothing is lost" in body["detail"]
    assert alice.get("/audio/sessions").status_code == 200
    state = alice.get(f"/audio/sessions/{sid}")
    assert state.status_code == 200 and state.json()["text"]
    assert run(voice_archive.archive_once()) == {"store_down": 1}


def test_a_moved_recording_the_store_lost_is_410_audio_missing_and_flagged(archive, voice, login_client):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    shutil.rmtree(archive.root / str(uid_of("alice")) / sid)
    gone = alice.get(f"/audio/sessions/{sid}/audio")
    assert gone.status_code == 410 and gone.json()["reason"] == "audio_missing"
    assert row_of(sid)["archive_error"] == "remote_missing"
    assert 'voice_archive_proxy_total{result="missing"}' in metrics.render()


def test_a_super_admin_hears_a_moved_recording_and_only_served_audio_is_audited(archive, voice, login_client):
    alice = login_client("alice")
    sid, data = finished(voice, alice)
    run(voice_archive.archive_once())
    root = login_client("root", role="super_admin")
    url = f"/admin/api/members/{uid_of('alice')}/voice/{sid}/audio"

    def audits() -> int:
        with db.connection() as con:
            return con.execute(
                "SELECT count(*) AS n FROM audit_events WHERE action = 'admin_downloaded_voice_recording'"
            ).fetchone()["n"]

    archive.down = True
    assert root.get(url).status_code == 503
    assert audits() == 0, "a refusal serves no audio and is not a download"
    archive.down = False
    served = root.get(url)
    assert served.status_code == 200 and served.content == data
    assert audits() == 1


# ------------------------------------------------------------ restoring --


def test_retranscribing_a_moved_recording_brings_it_back_and_it_moves_again_without_a_second_upload(archive, voice, login_client):
    alice = login_client("alice")
    sid, data = finished(voice, alice)
    run(voice_archive.archive_once())
    assert row_of(sid)["archive_state"] == "archived"
    r = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
    assert r.status_code == 202, r.text
    done = settled(alice, sid)
    assert done["outcome"] == "transcribed", done
    row = row_of(sid)
    assert row["archive_state"] == "copied" and row["head_released_at"] is None and row["head_hold_until"]
    assert row["source_sha256"] == sha(data)
    with open(dictation.source_path(row), "rb") as fh:
        assert fh.read() == data
    assert run(voice_archive.archive_once()) == {}, "held on the head for VOICE_ARCHIVE_HOLD_S"
    sql("UPDATE voice_sessions SET head_hold_until = now() - interval '1 second' WHERE id = %s", sid)
    assert run(voice_archive.archive_once()) == {"released": 1}
    assert row_of(sid)["archive_state"] == "archived"
    assert archive.puts() == 1, "the bytes never changed, so they were never sent again"


def test_retranscribing_while_the_store_is_down_changes_nothing(archive, voice, login_client):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    before = row_of(sid)
    archive.down = True
    r = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
    assert r.status_code == 503 and r.json()["reason"] == "archive_unavailable"
    after = row_of(sid)
    assert after["status"] == "done" and after["archive_state"] == "archived" and after["retranscribe"] is None
    assert after["rev"] == before["rev"]


def test_bringing_a_recording_back_is_refused_below_the_heads_free_space_floor(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    monkeypatch.setattr(settings, "voice_min_free_bytes", 1 << 62)
    with pytest.raises(dictation.SessionError) as refused:
        run(voice_archive.ensure_local(row_of(sid)))
    assert refused.value.status == 507 and refused.value.reason == "storage_full"
    assert row_of(sid)["archive_state"] == "archived"
    assert not any(n.startswith(".restore-") for n in head_files(voice, "alice", sid))


def test_a_restore_never_brings_back_a_recording_that_was_discarded(archive, voice, login_client):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    row = row_of(sid)
    assert alice.delete(f"/audio/sessions/{sid}").status_code == 204
    with pytest.raises(dictation.SessionError) as refused:
        run(voice_archive.ensure_local(row))
    assert refused.value.reason == "audio_deleted"
    assert not os.path.exists(dictation.session_dir(row["user_id"], sid)), "the folder was not recreated"


# ---------------------------------------------------------------- races --


def _copied(voice_env, client) -> Tuple[str, bytes]:
    """A recording on both the head and the store, free to be released."""
    sid, data = finished(voice_env, client)
    real_release = voice_archive.release

    async def no_release(row):
        return False

    voice_archive.release = no_release
    try:
        run(voice_archive.archive_once())
    finally:
        voice_archive.release = real_release
    assert row_of(sid)["archive_state"] == "copied"
    return sid, data


def test_release_and_a_retranscription_never_leave_the_decoder_without_its_file(archive, voice, login_client, monkeypatch):
    """Both orders, forced: the restore holds the lock while the release
    tries (the release then finds the hold), and the release holds the lock
    while the restore waits (the restore then brings the file back)."""
    alice = login_client("alice")

    # 1. The retranscription's ensure_local has the lock and is setting its hold.
    sid, data = _copied(voice, alice)
    holding, go = threading.Event(), threading.Event()
    real_hold = voice_archive._set_hold

    def slow_hold(session_id, hold_s):
        holding.set()
        go.wait(15)
        return real_hold(session_id, hold_s)

    monkeypatch.setattr(voice_archive, "_set_hold", slow_hold)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        request = pool.submit(lambda: alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"}))
        assert holding.wait(15)
        release = asyncio.run_coroutine_threadsafe(voice_archive.release(row_of(sid)), voice_archive.ARCHIVER.loop())
        time.sleep(0.3)
        assert not release.done(), "the release waits for the lock"
        go.set()
        assert release.result(15) is False, "then finds the hold and leaves the file"
        assert request.result(15).status_code == 202
    monkeypatch.setattr(voice_archive, "_set_hold", real_hold)
    assert settled(alice, sid)["outcome"] == "transcribed"

    # 2. The release has the lock and is between its UPDATE and its unlink.
    sid2, data2 = _copied(voice, alice)
    unlinking, go2 = threading.Event(), threading.Event()
    real_unlink = voice_archive._unlink_and_sync

    def slow_unlink(path, directory):
        unlinking.set()
        go2.wait(15)
        return real_unlink(path, directory)

    monkeypatch.setattr(voice_archive, "_unlink_and_sync", slow_unlink)
    release2 = asyncio.run_coroutine_threadsafe(voice_archive.release(row_of(sid2)), voice_archive.ARCHIVER.loop())
    assert unlinking.wait(15)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        request = pool.submit(lambda: alice.post(f"/audio/sessions/{sid2}/retranscribe", json={"scope": "all"}))
        time.sleep(0.3)
        assert not request.done(), "the retranscription waits for the lock"
        go2.set()
        assert release2.result(15) is True
        assert request.result(30).status_code == 202
    done = settled(alice, sid2)
    assert done["outcome"] == "transcribed", done
    with open(dictation.source_path(row_of(sid2)), "rb") as fh:
        assert fh.read() == data2, "brought back byte for byte"


def test_a_recording_discarded_while_it_is_copied_is_deleted_on_the_store_too(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    real_read_back = voice_archive.read_back

    async def discard_then_read_back(row, size, digest):
        await db.run_in_thread(dictation.discard, uid_of("alice"), sid)
        return await real_read_back(row, size, digest)

    monkeypatch.setattr(voice_archive, "read_back", discard_then_read_back)
    assert run(voice_archive.archive_once()) == {"deleted_meanwhile": 1}
    assert archive.objects() == []
    row = row_of(sid)
    assert row["status"] == "cancelled" and row["archive_state"] == "local" and row["remote_purged_at"]


def test_a_mover_that_died_after_the_upload_finishes_on_the_next_pass(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    real_mark = voice_archive._mark_copied
    calls = {"n": 0}

    def dies_once(session_id, digest):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the process died here")
        return real_mark(session_id, digest)

    monkeypatch.setattr(voice_archive, "_mark_copied", dies_once)
    with pytest.raises(RuntimeError):
        run(voice_archive.archive_once())
    assert archive.objects() == [(uid_of("alice"), sid)] and row_of(sid)["archive_state"] == "local"
    sql("UPDATE voice_sessions SET archive_next_at = NULL WHERE id = %s", sid)
    assert run(voice_archive.archive_once()) == {"archived": 1}
    assert [m for m, p in archive.requests if m == "PUT"] == ["PUT", "PUT"], "the second PUT answered 'already there'"


def test_a_release_that_died_before_its_unlink_leaves_a_file_the_sweep_removes(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid, data = finished(voice, alice)
    real_unlink = voice_archive._unlink_and_sync

    def dies(path, directory):
        raise RuntimeError("the process died here")

    monkeypatch.setattr(voice_archive, "_unlink_and_sync", dies)
    with pytest.raises(RuntimeError):
        run(voice_archive.archive_once())
    monkeypatch.setattr(voice_archive, "_unlink_and_sync", real_unlink)
    assert row_of(sid)["archive_state"] == "archived" and "source.wav" in head_files(voice, "alice", sid)
    assert alice.get(f"/audio/sessions/{sid}/audio").content == data, "the leftover still plays"
    assert run(voice_archive.head_sweep_once()) == 1
    assert "source.wav" not in head_files(voice, "alice", sid)
    assert alice.get(f"/audio/sessions/{sid}/audio").content == data, "now from the store"


# ------------------------------------------------------- continuations --


def _cut_in_a_pause(script, after_s: float) -> float:
    words = sorted(script.words, key=lambda w: w.start_s)
    for prev, nxt in zip(words, words[1:]):
        if prev.end_s >= after_s and nxt.start_s - prev.end_s >= 1.0:
            return round((prev.end_s + nxt.start_s) / 2.0, 3)
    raise AssertionError("no pause in the script")


def _idle_closed_then_moved(voice_env, client, monkeypatch):
    script, data = recording(voice_env.tmp, 40.0, seed=13)
    cut = 44 + int(_cut_in_a_pause(script, 15.0) * SR) * 2
    first = create(client).json()["session_id"]
    for seq, part in enumerate(parts_of(data[:cut])):
        assert put(client, first, seq, part).status_code == 200
    monkeypatch.setattr(settings, "voice_session_idle_s", 0.0)
    time.sleep(0.05)
    dictation.RUNNER.submit(dictation.maintain_once()).result(10)
    assert settled(client, first)["ended_by"] == "idle"
    monkeypatch.setattr(settings, "voice_session_idle_s", 600.0)
    run(voice_archive.archive_once())
    assert row_of(first)["archive_state"] == "archived"
    return first, data, cut


def _continue(client, first: str, rest: bytes) -> str:
    r = client.post("/audio/sessions", json={
        "client_key": str(uuid.uuid4()), "mime_type": "audio/wav", "part_ms": 5000, "continues": first,
    })
    assert r.status_code == 201, r.text
    second = r.json()["session_id"]
    parts = parts_of(rest)
    for seq, part in enumerate(parts):
        assert put(client, second, seq, part).status_code == 200
    r = client.post(f"/audio/sessions/{second}/finish", json={"last_part": len(parts) - 1, "ended_by": "person"})
    assert r.status_code == 202, r.text
    return second


def test_a_continuation_of_a_moved_recording_brings_it_back_and_keeps_it_while_it_decodes(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    first, data, cut = _idle_closed_then_moved(voice, alice, monkeypatch)
    # WAV is decoded without ffmpeg only from its header: the rest of the
    # stream must come after the first recording's bytes, so this continuation
    # needs them back on the head.
    second = _continue(alice, first, data[cut:])
    done = settled(alice, second)
    assert done["outcome"] == "transcribed", done
    assert abs(done["audio_ms"] - (40_000 - (cut - 44) * 1000 // (SR * 2))) <= 5
    assert row_of(first)["archive_state"] == "copied", "brought back, and held"
    # While a continuation is live, everything it continues is protected.
    sql("UPDATE voice_sessions SET head_hold_until = NULL WHERE id = %s", first)
    # Live again, under a lease another process holds (so this process's
    # maintenance does not adopt it while the test looks).
    sql(
        "UPDATE voice_sessions SET status = 'finishing', lease_owner = 'another-process', "
        "lease_expires_at = now() + interval '1 hour' WHERE id = %s", second,
    )
    assert run(voice_archive.release(row_of(first))) is False
    sql("UPDATE voice_sessions SET status = 'done', lease_owner = NULL, lease_expires_at = NULL WHERE id = %s", second)
    assert run(voice_archive.release(row_of(first))) is True


def test_a_continuation_that_read_its_recording_as_copied_survives_a_release_that_raced_the_read(
    archive, voice, login_client, monkeypatch,
):
    # A release's UPDATE that began before the continuation's row was
    # committed does not see it in the live chain, so it can still release
    # the file right after the decoder read the row as 'copied'. The decoder
    # must re-read under the release's lock (ensure_local), not trust that
    # read: here the stale read is forced, and the release has already
    # happened (the row says archived, the head file is gone).
    alice = login_client("alice")
    first, data, cut = _idle_closed_then_moved(voice, alice, monkeypatch)
    assert not os.path.exists(dictation.source_path(row_of(first)))
    r = alice.post("/audio/sessions", json={
        "client_key": str(uuid.uuid4()), "mime_type": "audio/wav", "part_ms": 5000, "continues": first,
    })
    assert r.status_code == 201, r.text
    second = r.json()["session_id"]
    real_row = dictation._row
    stale = {"left": 1}

    def racing_row(session_id):
        found = real_row(session_id)
        if session_id == first and found is not None and stale["left"]:
            stale["left"] -= 1
            return {**found, "archive_state": "copied", "head_released_at": None}
        return found

    monkeypatch.setattr(dictation, "_row", racing_row)
    parts = parts_of(data[cut:])
    for seq, part in enumerate(parts):
        assert put(alice, second, seq, part).status_code == 200
    r = alice.post(f"/audio/sessions/{second}/finish", json={"last_part": len(parts) - 1, "ended_by": "person"})
    assert r.status_code == 202, r.text
    done = settled(alice, second)
    assert stale["left"] == 0, "the decoder read the recording it continues"
    assert done["outcome"] == "transcribed", done
    assert abs(done["audio_ms"] - (40_000 - (cut - 44) * 1000 // (SR * 2))) <= 5
    assert row_of(first)["archive_state"] == "copied", "brought back, and held"


def test_a_continuation_waits_for_the_archive_instead_of_failing(archive, voice, login_client, monkeypatch):
    monkeypatch.setattr(dictation, "_ARCHIVE_RETRY_S", 0.2)
    # ...and a failed restore pauses restores that much, not 30 s.
    monkeypatch.setattr(voice_archive, "_RESTORE_PAUSE_S", 0.2)
    alice = login_client("alice")
    first, data, cut = _idle_closed_then_moved(voice, alice, monkeypatch)
    archive.down = True
    second = _continue(alice, first, data[cut:])
    deadline = time.monotonic() + 15
    seen = None
    while time.monotonic() < deadline:
        seen = alice.get(f"/audio/sessions/{second}").json()
        if seen["waiting_on"] == "archive":
            break
        time.sleep(0.1)
    assert seen and seen["waiting_on"] == "archive" and seen["status"] == "finishing", seen
    archive.down = False
    done = settled(alice, second)
    assert done["outcome"] == "transcribed", done


# -------------------------------------------------------------- deletes --


def test_deleting_a_moved_recording_deletes_the_stores_copy(archive, voice, login_client):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    assert archive.objects()
    assert alice.delete(f"/audio/sessions/{sid}").status_code == 204
    deadline = time.monotonic() + 10
    while archive.objects() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert archive.objects() == [], "asked for at once, without waiting on the request"
    deadline = time.monotonic() + 10
    while row_of(sid)["remote_purged_at"] is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert row_of(sid)["remote_purged_at"] is not None


def test_a_delete_the_store_missed_is_purged_when_it_is_back(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    archive.down = True
    assert alice.delete(f"/audio/sessions/{sid}").status_code == 204, "the person's delete never waits for the store"
    time.sleep(0.3)
    assert run(voice_archive.archive_once()) == {"store_down": 1}
    assert 'voice_archive_purge_pending 1' in metrics.render()
    archive.down = False
    stats = run(voice_archive.archive_once())
    assert stats.get("purged") == 1 and archive.objects() == []
    assert 'voice_archive_purge_pending 0' in metrics.render()


def _gauge(name: str) -> Optional[float]:
    for line in metrics.render().splitlines():
        if line.startswith(name + " "):
            return float(line.split()[1])
    return None


def test_a_recording_deleted_during_its_copy_is_purged_by_the_next_pass_when_the_stores_delete_missed(
    archive, voice, login_client, monkeypatch,
):
    """The copy was verified, then the person deleted the recording, and the
    store's DELETE failed once: the row stays 'local'. The purge step (and
    voice_archive_purge_pending) looked only at rows that were not 'local', so
    the person's deleted voice stayed on the worker until the daily reconcile
    (review 2026-09-30)."""
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    uid = uid_of("alice")
    real_read_back = voice_archive.read_back

    async def discard_then_read_back(row, size, digest):
        await db.run_in_thread(dictation.discard, uid, sid)
        return await real_read_back(row, size, digest)

    monkeypatch.setattr(voice_archive, "read_back", discard_then_read_back)
    blips = {"n": 0}

    def one_failed_delete(request: httpx.Request) -> Optional[httpx.Response]:
        if request.method == "DELETE" and blips["n"] == 0:
            blips["n"] += 1
            return httpx.Response(503, json={"reason": "busy"})
        return None

    archive.fail = one_failed_delete
    assert run(voice_archive.archive_once()) == {"deleted_meanwhile": 1}
    assert blips["n"] == 1 and archive.objects() == [(uid, sid)], "the store's DELETE missed"
    archive.fail = None
    assert row_of(sid)["archive_state"] == "local" and row_of(sid)["remote_purged_at"] is None
    assert _gauge("voice_archive_purge_pending") == 1.0, "and the gauge says so"
    assert run(voice_archive.archive_once()) == {"purged": 1}
    assert archive.objects() == [] and row_of(sid)["remote_purged_at"] is not None
    assert _gauge("voice_archive_purge_pending") == 0.0


def test_a_recording_retranscribed_during_its_copy_then_deleted_leaves_no_copy_on_the_store(
    archive, voice, login_client, monkeypatch,
):
    """No store failure at all: 'busy_meanwhile' keeps the verified copy on
    the store while the row stays 'local'; the person then deletes it."""
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    uid = uid_of("alice")
    real_read_back = voice_archive.read_back

    async def retranscribe_then_read_back(row, size, digest):
        r = await asyncio.to_thread(lambda: alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"}))
        assert r.status_code == 202, r.text
        return await real_read_back(row, size, digest)

    monkeypatch.setattr(voice_archive, "read_back", retranscribe_then_read_back)
    assert run(voice_archive.archive_once()) == {"busy_meanwhile": 1}
    monkeypatch.setattr(voice_archive, "read_back", real_read_back)
    settled(alice, sid)
    assert archive.objects() == [(uid, sid)] and row_of(sid)["archive_state"] == "local"
    assert alice.delete(f"/audio/sessions/{sid}").status_code == 204
    assert run(voice_archive.archive_once()) == {"purged": 1}
    assert archive.objects() == [], "the deleted recording's verified copy is gone from the worker"


def test_the_purge_is_keyed_on_the_delete_in_the_index_too():
    ddl = " ".join(db._MIGRATION_V43.split())
    predicate = ddl.split("CREATE INDEX IF NOT EXISTS idx_voice_sessions_archive_purge", 1)[1].split(";", 1)[0]
    assert "WHERE audio_deleted_at IS NOT NULL AND remote_purged_at IS NULL" in predicate
    assert "archive_state" not in predicate


def test_retention_deletes_the_stores_copy_as_well(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    monkeypatch.setattr(settings, "voice_retention_days", 1)
    sql("UPDATE voice_sessions SET finished_at = now() - interval '2 days' WHERE id = %s", sid)
    assert dictation.retention_sweep() == 1
    assert run(voice_archive.archive_once()).get("purged") == 1
    assert archive.objects() == []


# ------------------------------------------------------------ reconcile --


def _iso_days_ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _plant(control: "StoreControl", uid: int, *, owner: Optional[str], days: float) -> str:
    """An object on the store that no row of this database knows."""
    sid = uuid.uuid4().hex
    folder = control.root / str(uid) / sid
    folder.mkdir(parents=True)
    (folder / "source.wav").write_bytes(b"RIFF")
    manifest = {"name": "source.wav", "sha256": "0" * 64, "bytes": 4, "stored_at": _iso_days_ago(days)}
    if owner is not None:
        manifest["owner"] = owner
    (folder / "manifest.json").write_text(json.dumps(manifest))
    return sid


def _changes(control: "StoreControl", since: int) -> List[Tuple[str, str]]:
    """Every call after `since` that could change or remove an object."""
    return [(m, p) for m, p in control.requests[since:] if m in ("PUT", "POST", "DELETE")]


def test_every_object_names_the_deployment_whose_database_stored_it(archive, voice, login_client):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    me = run(voice_archive.deployment_owner())
    assert len(me) == 32 and int(me, 16) >= 0
    with db.connection() as con:
        assert con.execute("SELECT owner FROM voice_archive_owner").fetchall() == [{"owner": me}]
    manifest = json.loads((archive.root / str(uid_of("alice")) / sid / "manifest.json").read_text())
    assert manifest["owner"] == me


def test_the_reconcile_sets_aside_only_its_own_old_orphans_and_deletes_only_what_a_row_says_was_deleted(
    archive, voice, login_client, capsys,
):
    alice = login_client("alice")
    uid = uid_of("alice")
    kept, _ = finished(voice, alice, seed=21)
    lost, _ = finished(voice, alice, seed=22)
    cancelled, _ = finished(voice, alice, seed=23)
    repaired, _ = finished(voice, alice, seed=24)
    run(voice_archive.archive_once())
    me = run(voice_archive.deployment_owner())
    # No row of this database knows these: two of ours, 20 days and 2 days
    # old; another deployment's; and one stored before owners were recorded.
    old_orphan = _plant(archive, uid, owner=me, days=20)
    young_orphan = _plant(archive, uid, owner=me, days=2)
    theirs = _plant(archive, uid, owner="b" * 32, days=400)
    nobodys = _plant(archive, uid, owner=None, days=400)
    shutil.rmtree(archive.root / str(uid) / lost)
    sql("UPDATE voice_sessions SET status = 'cancelled', audio_deleted_at = now(), archive_state = 'local' WHERE id = %s", cancelled)
    sql("UPDATE voice_sessions SET archive_state = 'local', head_released_at = NULL WHERE id = %s", repaired)
    sql("UPDATE voice_sessions SET archived_at = now() - interval '1 hour'")
    before = len(archive.requests)
    stats = run(voice_archive.reconcile_once())
    assert stats == {
        "orphan_quarantined": 1, "orphan_waiting": 1, "other_owner": 1, "unowned": 1,
        "deleted_row_purged": 1, "repaired": 1, "remote_missing": 1, "foreign": 0,
    }
    assert sorted(_changes(archive, before)) == [
        ("DELETE", f"/v1/recordings/{uid}/{cancelled}"),
        ("POST", f"/v1/recordings/{uid}/{old_orphan}/quarantine"),
    ], "the tombstone's copy is deleted; the old orphan is set aside; nothing else is touched"
    assert row_of(lost)["archive_error"] == "remote_missing"
    assert row_of(repaired)["archive_state"] == "archived"
    assert row_of(kept)["archive_state"] == "archived" and row_of(kept)["archive_error"] is None
    assert {sid for _u, sid in archive.objects()} == {kept, repaired, young_orphan, theirs, nobodys}
    aside = archive.root / ".quarantine" / str(uid) / old_orphan
    assert (aside / "source.wav").read_bytes() == b"RIFF", "set aside whole, not deleted"
    assert [(o["session_id"], o["owner"]) for o in run(voice_archive.quarantined_objects())] == [(old_orphan, me)]

    # The operator's commands. The purge keeps what was set aside recently.
    assert voice_archive.main(["quarantine-purge"]) == 0
    assert json.loads(capsys.readouterr().out) == {"purged": 0, "kept": 1, "failed": 0}
    assert voice_archive.main(["quarantine-restore", f"{uid}/{old_orphan}"]) == 0
    capsys.readouterr()
    assert (archive.root / str(uid) / old_orphan / "source.wav").read_bytes() == b"RIFF"
    run(voice_archive.quarantine_object(uid, old_orphan))
    (aside / "quarantine.json").write_text(json.dumps({"quarantined_at": _iso_days_ago(40)}))
    assert voice_archive.main(["quarantine-purge"]) == 0
    assert json.loads(capsys.readouterr().out) == {"purged": 1, "kept": 0, "failed": 0}
    assert not aside.exists()
    assert {sid for _u, sid in archive.objects()} == {kept, repaired, young_orphan, theirs, nobodys}


def _empty_database(suffix: str) -> str:
    """A second, empty database on the test server (another deployment's)."""
    base, _, name = db.dsn().rpartition("/")
    second = f"{name.split('?', 1)[0]}_{suffix}_test"
    with psycopg.connect(f"{base}/postgres", autocommit=True, connect_timeout=5) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{second}" WITH (FORCE)')
        admin.execute(f"""CREATE DATABASE "{second}" TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C' ENCODING 'UTF8'""")
    return f"{base}/{second}"


def _drop_database(dsn: str) -> None:
    base, _, name = dsn.rpartition("/")
    with psycopg.connect(f"{base}/postgres", autocommit=True, connect_timeout=5) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_a_second_orchestrator_on_its_own_database_with_the_same_store_settings_deletes_nothing(
    archive, voice, login_client, monkeypatch,
):
    """THE REVIEW'S HIGH (2026-09-30). An e2e stack, a candidate or a
    developer's orchestrator given production's VOICE_ARCHIVE_URL, token and
    certificate has its OWN, empty database. Its daily reconcile used to
    delete every stored object its database had no row for, and after the
    head copy's release the store holds production's ONLY copy: measured
    {'orphan_deleted': 1}, then 410 audio_missing on production's playback.
    Now the second deployment is another owner: its reconcile, its mover and
    its purge touch nothing of production's, however old."""
    alice = login_client("alice")
    sid, data = finished(voice, alice)
    assert run(voice_archive.archive_once()) == {"archived": 1}
    uid = uid_of("alice")
    production = run(voice_archive.deployment_owner())
    manifest = archive.root / str(uid) / sid / "manifest.json"
    body = json.loads(manifest.read_text())
    body["stored_at"] = _iso_days_ago(400)  # past every grace there is
    manifest.write_text(json.dumps(body))

    first = db.dsn()
    second = _empty_database("second_deployment")
    try:
        monkeypatch.setattr(settings, "app_database_url", second)
        db.init_schema()
        other = run(voice_archive.deployment_owner())
        before = len(archive.requests)
        stats = run(voice_archive.reconcile_once())
        passed = run(voice_archive.archive_once())
        changes = _changes(archive, before)
    finally:
        monkeypatch.setattr(settings, "app_database_url", first)
        db.close_pool()
        _drop_database(second)
        voice_archive._OWNERS.pop(second, None)
    assert other != production, "another database is another owner"
    assert changes == [], f"the second deployment changed the store: {changes}"
    assert stats["other_owner"] == 1 and stats["orphan_quarantined"] == 0, stats
    assert passed == {}, passed
    assert archive.objects() == [(uid, sid)]
    back = alice.get(f"/audio/sessions/{sid}/audio")
    assert back.status_code == 200 and back.content == data, "production still plays its recording"
    # The store would refuse it anyway: another owner cannot delete it.
    with pytest.raises(voice_archive.StoreError) as refused:
        run(_as_owner(other, voice_archive.delete_object(uid, sid)))
    assert refused.value.reason == "conflict" and refused.value.status == 409
    assert archive.objects() == [(uid, sid)]


async def _as_owner(owner: str, coro):
    """Run `coro` speaking for `owner` on the store."""
    key = db.dsn()
    real = voice_archive._OWNERS.get(key)
    voice_archive._OWNERS[key] = owner
    try:
        return await coro
    finally:
        if real is None:
            voice_archive._OWNERS.pop(key, None)
        else:
            voice_archive._OWNERS[key] = real


def test_a_recording_another_owner_stored_is_never_called_missing_and_never_deleted(
    archive, voice, login_client, caplog,
):
    """Review 2026-10-01. The reconcile skipped another owner's object before
    it noted the object as present, so a row of ours whose object another
    owner holds was flagged remote_missing: after a change of the owner row
    (or in a copy of production's database made a deployment of its own)
    every archived recording was, and VoiceArchiveCopiesMissing fired. The
    object is there and plays; it is not missing. And nothing here deletes
    it, not even for a row here that says it was deleted."""
    alice = login_client("alice")
    uid = uid_of("alice")
    plays, data = finished(voice, alice, seed=41)
    flagged, _ = finished(voice, alice, seed=42)
    tombstone, _ = finished(voice, alice, seed=43)
    assert run(voice_archive.archive_once()) == {"archived": 3}
    theirs = "c" * 32
    for sid in (plays, flagged, tombstone):
        manifest = archive.root / str(uid) / sid / "manifest.json"
        manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "owner": theirs}))
    sql("UPDATE voice_sessions SET archived_at = now() - interval '1 hour'")
    sql("UPDATE voice_sessions SET archive_error = 'remote_missing' WHERE id = %s", flagged)  # an earlier flag
    sql("UPDATE voice_sessions SET status = 'cancelled', audio_deleted_at = now() WHERE id = %s", tombstone)
    missing_errors = _gauge('voice_archive_errors_total{reason="remote_missing"}') or 0.0
    before = len(archive.requests)
    stats = run(voice_archive.reconcile_once())
    assert stats["other_owner"] == 3 and stats["remote_missing"] == 0, stats
    assert _changes(archive, before) == [], "nothing of another owner's is deleted or set aside, tombstone or not"
    assert row_of(plays)["archive_error"] is None
    assert row_of(flagged)["archive_error"] is None, "there after all: the earlier flag is cleared"
    assert _gauge("voice_archive_remote_missing") == 0.0
    assert (_gauge('voice_archive_errors_total{reason="remote_missing"}') or 0.0) == missing_errors
    assert "is not on the store" not in caplog.text
    assert "3 recording(s) this database has rows for are stored under another owner" in caplog.text
    got = alice.get(f"/audio/sessions/{plays}/audio")
    assert got.status_code == 200 and got.content == data, "and it plays"
    # The purge asks the store for the tombstone's copy; the store refuses another owner's.
    assert run(voice_archive.archive_once()) == {"errors": 1}
    assert row_of(tombstone)["remote_purged_at"] is None
    assert {sid for _u, sid in archive.objects()} == {plays, flagged, tombstone}


def test_a_copy_whose_row_vanished_is_kept_because_only_a_tombstone_deletes(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    real_read_back = voice_archive.read_back

    async def row_deleted_by_hand(row, size, digest):
        await db.run_in_thread(sql, "DELETE FROM voice_sessions WHERE id = %s", sid)
        return await real_read_back(row, size, digest)

    monkeypatch.setattr(voice_archive, "read_back", row_deleted_by_hand)
    assert run(voice_archive.archive_once()) == {"deleted_meanwhile": 1}
    assert archive.objects() == [(uid_of("alice"), sid)], "no row is not a tombstone"
    assert not [p for m, p in archive.requests if m == "DELETE"]


# --------------------------------------------------------------- rollback --


def test_recall_all_brings_every_recording_back_and_marks_it_local(archive, voice, login_client):
    alice = login_client("alice")
    first, data1 = finished(voice, alice, seed=31)
    second, data2 = finished(voice, alice, seed=32)
    run(voice_archive.archive_once())
    assert {row_of(first)["archive_state"], row_of(second)["archive_state"]} == {"archived"}
    stats = run(voice_archive.recall_all())
    assert stats == {"recalled": 2, "failed": 0, "remote_deleted": 2}
    for sid, data in ((first, data1), (second, data2)):
        row = row_of(sid)
        assert row["archive_state"] == "local" and row["archived_at"] is None
        with open(dictation.source_path(row), "rb") as fh:
            assert fh.read() == data
    assert archive.objects() == []


# ------------------------------------------------- reads, the fix round --


@pytest.mark.parametrize("rng", ["bytes=abc", "bytes=5-2", "items=0-1"])
def test_a_malformed_range_on_a_moved_recording_gets_the_heads_answer_not_an_outage(archive, voice, login_client, rng):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    local = alice.get(f"/audio/sessions/{sid}/audio", headers={"range": rng})
    run(voice_archive.archive_once())
    moved = alice.get(f"/audio/sessions/{sid}/audio", headers={"range": rng})
    assert moved.status_code == local.status_code == 400, (local.status_code, moved.status_code, moved.text[:160])
    assert 'voice_archive_errors_total{reason="http_4xx"}' not in metrics.render()
    assert 'voice_archive_proxy_total{result="bad_request"}' in metrics.render()


def test_a_multi_range_on_a_moved_recording_keeps_its_multipart_type(archive, voice, login_client):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    local = alice.get(f"/audio/sessions/{sid}/audio", headers={"range": "bytes=0-0,10-19"})
    run(voice_archive.archive_once())
    moved = alice.get(f"/audio/sessions/{sid}/audio", headers={"range": "bytes=0-0,10-19"})
    assert local.headers["content-type"].startswith("multipart/byteranges")
    assert moved.status_code == 206 and moved.headers["content-type"].startswith("multipart/byteranges")


def test_a_read_that_saw_the_row_before_the_move_is_served_from_the_store_not_told_it_was_deleted(
    archive, voice, login_client, monkeypatch,
):
    alice = login_client("alice")
    sid, data = finished(voice, alice)
    before_the_move = row_of(sid)
    run(voice_archive.archive_once())
    real = dictation._owned_row

    def stale(session_id, user_id):
        return dict(before_the_move) if session_id == sid else real(session_id, user_id)

    monkeypatch.setattr(dictation, "_owned_row", stale)
    got = alice.get(f"/audio/sessions/{sid}/audio")
    assert got.status_code == 200 and got.content == data, got.text[:160]


def test_retranscribe_refusals_while_the_store_is_down_do_not_use_up_the_hourly_retries(
    archive, voice, login_client, monkeypatch,
):
    monkeypatch.setattr(settings, "voice_retranscribe_per_hour", 5)  # the production default
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    archive.down = True
    for attempt in range(5):
        r = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
        assert r.status_code == 503 and r.json()["reason"] == "archive_unavailable", (attempt, r.text[:200])
    archive.down = False
    # The refusals' Retry-After (30 s) has passed.
    monkeypatch.setattr(voice_archive, "_restore_failed_at", time.monotonic() - voice_archive._RESTORE_PAUSE_S)
    r = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
    assert r.status_code == 202, r.text[:200]
    settled(alice, sid)


def test_while_the_store_is_failing_try_again_is_refused_at_once_and_only_one_attempt_reaches_it(
    archive, voice, login_client, monkeypatch,
):
    """Review 2026-10-01. Retranscriptions are counted against the hourly
    limit only once their audio is here (the fix above), and then nothing
    capped attempts on a failing store: every "Try again" ran a full restore
    inside the person's lock, on a thread of the shared worker pool, for up
    to 32 s against a hung store (12 at once: 12 store attempts, 12.3 s with
    a 1 s failure). Now the first failure pauses restores for the 30 s its
    Retry-After names, and the rest are refused at once without the store."""
    monkeypatch.setattr(settings, "voice_retranscribe_per_hour", 1)
    alice = login_client("alice")
    sid, data = finished(voice, alice)
    assert run(voice_archive.archive_once()) == {"archived": 1}
    real_download = voice_archive.download_to
    attempts: List[str] = []
    in_store, give_up = threading.Event(), threading.Event()

    async def hung_store(row, dest, size, digest):
        attempts.append(row["id"])
        in_store.set()
        await asyncio.to_thread(give_up.wait, 30)
        raise voice_archive.StoreError("timeout", "the store did not answer within the read timeout")

    monkeypatch.setattr(voice_archive, "download_to", hung_store)

    def try_again() -> httpx.Response:
        return alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})

    with concurrent.futures.ThreadPoolExecutor(12) as pool:
        presses = [pool.submit(try_again) for _ in range(12)]
        assert in_store.wait(15), "the first press reached the store"
        give_up.set()  # the store's read timeout
        answers = [p.result(60) for p in presses]
    assert [(a.status_code, a.json()["reason"]) for a in answers] == [(503, "archive_unavailable")] * 12
    assert {a.json()["retry_after_s"] for a in answers} == {30}
    assert attempts == [sid], f"one attempt reached the store, not {len(attempts)}"
    refused = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
    assert refused.status_code == 503 and attempts == [sid], "inside the 30 s, still without the store"
    row = row_of(sid)
    assert row["archive_state"] == "archived" and row["status"] == "done" and row["retranscribe"] is None

    # The store answers again, and the 30 s have passed: the next press
    # restores it, and the refusals did not use up the hour (one allowed).
    monkeypatch.setattr(voice_archive, "download_to", real_download)
    monkeypatch.setattr(voice_archive, "_restore_failed_at", time.monotonic() - voice_archive._RESTORE_PAUSE_S)
    r = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
    assert r.status_code == 202, r.text[:200]
    assert voice_archive._restore_failed_at is None, "a restore that got its bytes ends the pause"
    assert settled(alice, sid)["outcome"] == "transcribed"
    with open(dictation.source_path(row_of(sid)), "rb") as fh:
        assert fh.read() == data


def test_one_unreadable_head_file_does_not_stop_every_other_recording_moving(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    first, _ = finished(voice, alice, seconds=4.0, seed=201)  # the oldest: claimed first
    others = [finished(voice, alice, seconds=4.0, seed=202 + i)[0] for i in range(3)]
    real_hash = voice_archive._hash_file

    def unreadable(path):
        if first in path:
            raise OSError(errno.EIO, "Input/output error", path)
        return real_hash(path)

    monkeypatch.setattr(voice_archive, "_hash_file", unreadable)
    assert run(voice_archive.archive_once()) == {"local_unreadable": 1, "archived": 3}
    assert {row_of(s)["archive_state"] for s in others} == {"archived"}
    row = row_of(first)
    assert row["archive_state"] == "local" and row["archive_error"] == "local_unreadable" and row["archive_attempts"] == 1
    assert 'voice_archive_errors_total{reason="local_unreadable"}' in metrics.render()


def test_a_head_file_that_fails_while_the_put_reads_it_backs_off_that_recording_only(
    archive, voice, login_client, monkeypatch,
):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)

    async def failing_body(path, rate):
        raise OSError(errno.EIO, "Input/output error", path)
        yield b""  # pragma: no cover - an async generator

    monkeypatch.setattr(voice_archive, "_paced_file", failing_body)
    # Answered without the store: the transport reads the body, as a real one does.
    archive.fail = lambda request: httpx.Response(201) if request.method == "PUT" else None
    assert run(voice_archive.archive_once()) == {"local_unreadable": 1}
    assert row_of(sid)["archive_error"] == "local_unreadable" and row_of(sid)["archive_state"] == "local"


def test_the_heads_disk_filling_during_a_restore_is_507_and_leaves_nothing_behind(archive, voice, login_client, monkeypatch):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    calls = {"n": 0}

    def fills_up(fd, data):
        calls["n"] += 1
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(voice_archive, "_write_all", fills_up)
    r = alice.post(f"/audio/sessions/{sid}/retranscribe", json={"scope": "all"})
    assert calls["n"] >= 1
    assert r.status_code == 507 and r.json()["reason"] == "storage_full", (r.status_code, r.text[:200])
    row = row_of(sid)
    assert row["archive_state"] == "archived" and row["status"] == "done"
    assert not [n for n in head_files(voice, "alice", sid) if n.startswith((".restore-", "source."))]


def test_recall_all_refuses_while_the_mover_would_move_everything_back_out(archive, voice, login_client, monkeypatch, capsys):
    alice = login_client("alice")
    sid, _data = finished(voice, alice)
    run(voice_archive.archive_once())
    monkeypatch.setattr(settings, "voice_archive_enabled", True)
    assert voice_archive.main(["recall-all"]) == 2
    assert "VOICE_ARCHIVE_ENABLED" in capsys.readouterr().err
    assert row_of(sid)["archive_state"] == "archived", "nothing was recalled"
    monkeypatch.setattr(settings, "voice_archive_enabled", False)
    assert voice_archive.main(["recall-all"]) == 0
    assert row_of(sid)["archive_state"] == "local"


# ------------------------------------------------- the pool, real HTTP --


def _store_server(root: Path, *, limit: int = 256, keep_alive: int = STORE.KEEP_ALIVE_TIMEOUT_S) -> Tuple[Any, threading.Thread, int]:
    """The store's app on a real port. By default its own concurrency limit
    is set well above any client pool here, so what is measured is the
    CLIENT's pool."""
    import uvicorn

    app = STORE.create_app(STORE.Settings(root=str(root), tokens=(TOKEN,), min_free_bytes=0), background=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error", lifespan="off", http="h11", loop="asyncio",
        limit_concurrency=limit, timeout_keep_alive=keep_alive,
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    return server, thread, port


def test_playback_is_not_capped_at_eight_listeners_and_a_full_pool_says_busy_not_down(tmp_path, monkeypatch):
    """Every proxied playback holds one pooled connection for its whole
    stream. With 8 per loop the ninth listener waited out the pool and was
    told the archive "isn't answering" (review 2026-09-30, real Chromium).
    Real HTTP here: the store's own server, the module's own client."""
    root = tmp_path / "store-root"
    sid = uuid.uuid4().hex
    folder = root / "7" / sid
    folder.mkdir(parents=True)
    data = os.urandom(256 * 1024)
    (folder / "source.webm").write_bytes(data)
    (folder / "manifest.json").write_text(json.dumps({"name": "source.webm", "sha256": sha(data), "bytes": len(data)}))
    server, thread, port = _store_server(root)
    monkeypatch.setattr(settings, "voice_archive_url", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(settings, "voice_archive_token", TOKEN)
    monkeypatch.setattr(settings, "voice_archive_tls_cert_b64", "")
    row = {"id": sid, "user_id": 7, "ext": "webm", "mime_type": "audio/webm", "status": "done", "archive_state": "archived"}

    def request(rng: str) -> Request:
        return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [(b"range", rng.encode())]})

    async def scenario() -> Dict[str, Any]:
        held: List[Any] = []
        seen: Dict[str, Any] = {}
        try:
            async with asyncio.timeout(20):
                for _ in range(16):  # sixteen players, each holding its stream
                    held.append(await voice_archive._proxy(row, request("bytes=0-"), "r.webm", {}))
                started = time.monotonic()
                probe = await voice_archive._proxy(row, request("bytes=0-0"), "r.webm", {})
                seen["seventeenth"] = (probe.status_code, time.monotonic() - started)
                await probe.background()
                while len(held) < voice_archive._MAX_CONNECTIONS:
                    held.append(await voice_archive._proxy(row, request("bytes=0-"), "r.webm", {}))
                started = time.monotonic()
                full = await voice_archive._proxy(row, request("bytes=0-0"), "r.webm", {})
                seen["full"] = (full.status_code, json.loads(full.body), full.headers.get("retry-after"), time.monotonic() - started)
            seen["codes"] = sorted({r.status_code for r in held})
            return seen
        finally:
            for response in held:
                await response.background()
            await voice_archive.close_client()

    try:
        seen = asyncio.run(scenario())
    finally:
        server.should_exit = True
        thread.join(10)
    assert seen["codes"] == [206]
    status, elapsed = seen["seventeenth"]
    assert status == 206 and elapsed < 2, seen
    status, body, retry_after, waited = seen["full"]
    assert status == 503 and body["reason"] == "archive_busy" and retry_after == "5", seen
    assert waited < voice_archive._POOL_TIMEOUT_S + 3, "told promptly, not after the read timeout"


class _LoopThread:
    """An event loop on its own thread, as the orchestrator's request loop
    and the archive's loop each are: each gets its own pooled client."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout: float = 30.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


def _until(condition: Callable[[], bool], what: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, what
        time.sleep(0.01)


def test_the_store_serves_a_full_pool_from_both_loops_and_past_its_own_limit_a_player_hears_busy(tmp_path, monkeypatch):
    """Review 2026-10-01. uvicorn answers 503 once len(connections) >=
    limit_concurrency, so the store's 64 served 63: with the request loop's
    pool and the archive loop's both full (32 + 32), the 64th stream got the
    store's own 503, and the person was told the archive "isn't answering".
    Real uvicorn at the limit server.main() gives it, the module's own
    clients on two event loops, held streams as players hold them."""
    tree = ast.parse((REPO / "compose" / "voice-store" / "server.py").read_text(encoding="utf-8"))
    run_call = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "run"
        and getattr(node.func.value, "id", None) == "uvicorn"
    )
    limit = {k.arg: k.value for k in run_call.keywords}["limit_concurrency"]
    assert isinstance(limit, ast.Name) and limit.id == "LIMIT_CONCURRENCY", "the limit this test runs the store at"
    pools = 2 * voice_archive._MAX_CONNECTIONS

    root = tmp_path / "store-root"
    sid = uuid.uuid4().hex
    folder = root / "7" / sid
    folder.mkdir(parents=True)
    data = os.urandom(256 * 1024)
    (folder / "source.webm").write_bytes(data)
    (folder / "manifest.json").write_text(json.dumps({"name": "source.webm", "sha256": sha(data), "bytes": len(data)}))
    # A long keep-alive: a held stream here is a connection the store counts
    # whether or not its handler has finished writing this short body.
    server, thread, port = _store_server(root, limit=STORE.LIMIT_CONCURRENCY, keep_alive=120)
    monkeypatch.setattr(settings, "voice_archive_url", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(settings, "voice_archive_token", TOKEN)
    monkeypatch.setattr(settings, "voice_archive_tls_cert_b64", "")
    row = {"id": sid, "user_id": 7, "ext": "webm", "mime_type": "audio/webm", "status": "done", "archive_state": "archived"}

    def request(rng: str) -> Request:
        return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [(b"range", rng.encode())]})

    async def fill(held: List[Any]) -> List[int]:
        while len(held) < voice_archive._MAX_CONNECTIONS:
            held.append(await voice_archive._proxy(row, request("bytes=0-"), "r.webm", {}))
        return sorted({r.status_code for r in held})

    async def let_them_go(held: List[Any]) -> None:
        for response in held:
            if response.background is not None:
                await response.background()
        await voice_archive.close_client()

    async def one_more() -> Tuple[int, Dict[str, Any], Optional[str]]:
        try:
            refused = await voice_archive._proxy(row, request("bytes=0-0"), "r.webm", {})
            return refused.status_code, json.loads(refused.body), refused.headers.get("retry-after")
        finally:
            await voice_archive.close_client()

    loops = {"requests": _LoopThread(), "archive": _LoopThread()}
    held: Dict[str, List[Any]] = {name: [] for name in loops}
    idle: List[socket.socket] = []
    try:
        assert loops["requests"].run(fill(held["requests"])) == [206]
        assert loops["archive"].run(fill(held["archive"])) == [206], "the second full pool fits too"
        _until(lambda: len(server.server_state.connections) == pools, "the store counts both pools")
        assert STORE.LIMIT_CONCURRENCY - 1 > pools, "room left over for the rest of what talks to the store"
        # Up to one below the store's own limit with connections that say nothing...
        while len(idle) < STORE.LIMIT_CONCURRENCY - 1 - pools:
            idle.append(socket.create_connection(("127.0.0.1", port), timeout=5))
        _until(lambda: len(server.server_state.connections) == STORE.LIMIT_CONCURRENCY - 1, "the store counts them")
        # ...and one more, from a third loop whose pool has room: the store refuses it itself.
        status, body, retry_after = asyncio.run(one_more())
        assert status == 503 and body["reason"] == "archive_busy" and retry_after == "5", (status, body)
    finally:
        for sock in idle:
            sock.close()
        for name, loop in loops.items():
            with contextlib.suppress(Exception):
                loop.run(let_them_go(held[name]))
            loop.close()
        server.should_exit = True
        thread.join(10)


# -------------------------------------------------------------- the pin --


def _openssl() -> Optional[str]:
    return shutil.which("openssl")


def _certificate(directory: Path, name: str) -> Tuple[Path, Path]:
    key, cert = directory / f"{name}.key", directory / f"{name}.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
         "-keyout", str(key), "-out", str(cert), "-days", "2", "-subj", f"/CN={name}",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True, timeout=60,
    )
    return key, cert


@pytest.mark.skipif(_openssl() is None, reason="the openssl command is needed to make a certificate")
def test_the_store_is_trusted_by_its_pinned_certificate_and_nothing_else(tmp_path, monkeypatch):
    import uvicorn

    key, cert = _certificate(tmp_path, "store")
    _other_key, other_cert = _certificate(tmp_path, "impostor")
    root = tmp_path / "store-root"
    root.mkdir()
    app = STORE.create_app(STORE.Settings(root=str(root), tokens=(TOKEN,), min_free_bytes=0), background=False)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, ssl_certfile=str(cert), ssl_keyfile=str(key), log_level="error", lifespan="off",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        monkeypatch.setattr(settings, "voice_archive_url", f"https://127.0.0.1:{port}")
        monkeypatch.setattr(settings, "voice_archive_token", TOKEN)

        async def health():
            try:
                return await voice_archive.store_health()
            finally:
                await voice_archive.close_client()

        monkeypatch.setattr(settings, "voice_archive_tls_cert_b64", base64.b64encode(cert.read_bytes()).decode())
        assert asyncio.run(health())["ready"] is True
        monkeypatch.setattr(settings, "voice_archive_tls_cert_b64", base64.b64encode(other_cert.read_bytes()).decode())
        with pytest.raises(voice_archive.StoreError) as wrong:
            asyncio.run(health())
        assert wrong.value.reason == "tls"
        monkeypatch.setattr(settings, "voice_archive_tls_cert_b64", "")
        with pytest.raises(voice_archive.StoreError) as unpinned:
            asyncio.run(health())
        assert unpinned.value.reason == "tls", "https without a pinned certificate is refused, not trusted"
    finally:
        server.should_exit = True
        thread.join(10)
