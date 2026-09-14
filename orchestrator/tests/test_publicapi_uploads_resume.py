"""Resumable uploads (design §2.13, §7.3, acceptance A-1 at the orchestrator):
a cut part records nothing, the state is readable, a crash anywhere resumes.

`test_a_2_gib_upload_survives_a_dropped_connection_and_resumes` is the A-1
scenario against a REAL uvicorn on loopback with a relay that resets the TCP
connection 40 MiB into a 64 MiB part. It writes ~4 GiB under pytest's tmp dir
(parts + assembled copy) and takes about a minute; APIFILES_RESUME_BYTES
shrinks it for a quick local loop (the default is the design's 2 GiB).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import os
import random
import socket
import struct
import threading
import time

import httpx
import pytest
from starlette.testclient import TestClient

from app import db
from app.apifiles import queue, schema, storage, uploads_sweep
from tests.apifiles_test_support import (  # noqa: F401 - fixture by name
    StubAuth,
    asgi_call,
    assemble_all,
    build_app,
    headers_for,
    isolated_app_db,
    make_caller,
    multipart_body,
    peak_rss_mib,
    serve_process,
)

MIB = 1024 * 1024


@pytest.fixture()
def api():
    caller = make_caller()
    app = build_app(StubAuth(caller))
    with TestClient(app) as client:
        yield client, caller, app


def _upload(client, caller, size):
    response = client.post("/v1/uploads", headers=headers_for(caller),
                           json={"bytes": size, "filename": "big.bin", "mime_type": "application/octet-stream", "purpose": "user_data"})
    assert response.status_code == 200, response.text
    return response.json()


def _stray(upload_id):
    return sorted(os.listdir(storage.parts_dir(upload_id)))


def test_a_raw_part_cut_mid_body_is_408_and_records_nothing(api):
    client, caller, app = api
    upload = _upload(client, caller, 10 * MIB)
    assert client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(caller), content=b"a" * MIB).status_code == 200
    chunks = [b"x" * 65536] * 64
    result = asyncio.run(asgi_call(
        app, "PUT", f"/v1/uploads/{upload['id']}/parts/1",
        headers={**headers_for(caller), "content-length": str(4 * MIB)}, body_chunks=chunks, disconnect_after=20,
    ))
    assert result["status"] == 408 and b"incomplete_body" in result["body"]
    state = client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()
    assert [p["part_number"] for p in state["parts"]] == [0] and state["bytes_received"] == MIB
    assert _stray(upload["id"]) == ["0"]


def test_a_multipart_part_cut_mid_body_is_408_and_records_nothing(api):
    client, caller, app = api
    upload = _upload(client, caller, 10 * MIB)
    body = multipart_body([("part_number", "0"), ("data", "upload", os.urandom(2 * MIB), "application/octet-stream")])
    chunks = [body[i:i + 65536] for i in range(0, len(body), 65536)]
    result = asyncio.run(asgi_call(
        app, "POST", f"/v1/uploads/{upload['id']}/parts",
        headers={**headers_for(caller), "content-type": "multipart/form-data; boundary=testboundary7d1a"},
        body_chunks=chunks, disconnect_after=10,
    ))
    assert result["status"] == 408
    assert client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()["parts"] == []
    assert _stray(upload["id"]) == []


def test_the_resume_view_lists_every_accepted_part_and_a_resumed_upload_completes(api):
    client, caller, _app = api
    blobs = [os.urandom(1000) for _ in range(5)]
    upload = _upload(client, caller, 5000)
    for number in (0, 1, 3):
        client.put(f"/v1/uploads/{upload['id']}/parts/{number}", headers=headers_for(caller), content=blobs[number])
    state = client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()
    assert [p["part_number"] for p in state["parts"]] == [0, 1, 3]
    assert state["parts"][0]["sha256"] == hashlib.sha256(blobs[0]).hexdigest()
    assert state["bytes_received"] == 3000 and state["part_mode"] == "numbered" and state["error"] is None
    have = {p["part_number"] for p in state["parts"]}
    for number in range(5):
        if number not in have:
            client.put(f"/v1/uploads/{upload['id']}/parts/{number}", headers=headers_for(caller), content=blobs[number])
    done = client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={})
    assert done.status_code == 200
    assert [r.outcome for r in assemble_all(caller)] == ["attached"]
    assert client.get(f"/v1/files/{done.json()['file']['id']}/content", headers=headers_for(caller)).content == b"".join(blobs)


def test_a_stale_finalizing_upload_is_returned_to_pending_and_can_complete(api):
    client, caller, _app = api
    upload = _upload(client, caller, 5)
    client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(caller), content=b"hello")
    assert schema.try_begin_api_upload_finalize(caller.project_id, upload["id"])[0] == "won"   # ...and the process dies
    busy = client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={})
    assert busy.status_code == 409 and busy.headers["x-should-retry"] == "true" and busy.headers["retry-after"] == "2"
    with db.connection() as con:
        con.execute("UPDATE api_uploads SET updated_at = now() - interval '601 seconds' WHERE id = %s", (upload["id"],))
    report = uploads_sweep.run_once()
    assert report.finalizing_reset >= 1
    assert client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={}).status_code == 200


def test_the_expiry_slides_with_each_part_but_never_past_the_hard_cap(api):
    client, caller, _app = api
    upload = _upload(client, caller, 10)
    with db.connection() as con:
        con.execute("UPDATE api_uploads SET created_at = now() - interval '100 hours', "
                    "expires_at = now() + interval '1 hour' WHERE id = %s", (upload["id"],))
    client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(caller), content=b"hello")
    slid = client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()
    assert abs(slid["expires_at"] - (time.time() + 24 * 3600)) < 120, "last part + 24 h"
    with db.connection() as con:
        con.execute("UPDATE api_uploads SET created_at = now() - interval '167 hours' WHERE id = %s", (upload["id"],))
    client.put(f"/v1/uploads/{upload['id']}/parts/1", headers=headers_for(caller), content=b"world")
    capped = client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()
    assert abs(capped["expires_at"] - (time.time() + 3600)) < 120, "never past created + 7 d"


def test_an_expired_upload_refuses_parts_before_and_after_the_sweep_removes_them(api):
    client, caller, _app = api
    upload = _upload(client, caller, 10)
    client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(caller), content=b"hello")
    with db.connection() as con:
        con.execute("UPDATE api_uploads SET expires_at = now() - interval '1 second' WHERE id = %s", (upload["id"],))
    lapsed = client.put(f"/v1/uploads/{upload['id']}/parts/1", headers=headers_for(caller), content=b"world")
    assert lapsed.status_code == 409 and "expired" in lapsed.json()["error"]["message"]
    assert client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()["status"] == "expired"
    report = uploads_sweep.run_once()
    assert upload["id"] in report.expired
    assert not os.path.exists(storage.upload_dir(upload["id"]))
    assert client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={}).status_code == 409


def test_an_assembly_interrupted_by_a_crash_is_reclaimed_when_its_lease_lapses(api):
    client, caller, _app = api
    upload = _upload(client, caller, 10)
    client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(caller), content=b"hello")
    client.put(f"/v1/uploads/{upload['id']}/parts/1", headers=headers_for(caller), content=b"world")
    file_id = client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={}).json()["file"]["id"]
    crashed = queue.claim_assembly(owner="dead-host:1:x", project_ids=[caller.project_id])
    assert crashed is not None and crashed["id"] == upload["id"]
    assert queue.claim_assembly(project_ids=[caller.project_id]) is None, "a live lease is not stolen"
    assert _stray(upload["id"]) == ["0", "1"], "parts stay until the file has its blob"
    with db.connection() as con:
        con.execute("UPDATE api_uploads SET assembly_lease_expires_at = now() - interval '1 second' WHERE id = %s", (upload["id"],))
    assert [r.outcome for r in assemble_all(caller)] == ["attached"]
    assert client.get(f"/v1/files/{file_id}/content", headers=headers_for(caller)).content == b"helloworld"


def test_a_part_missing_from_disk_ends_the_file_in_error_after_the_attempts(api, monkeypatch):
    client, caller, _app = api
    monkeypatch.setenv("PUBLIC_API_FILES_ASSEMBLY_MAX_ATTEMPTS", "2")
    upload = _upload(client, caller, 5)
    client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(caller), content=b"hello")
    file_id = client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={}).json()["file"]["id"]
    os.unlink(storage.part_path(upload["id"], 0))
    outcomes = []
    for _ in range(3):
        outcomes += [r.outcome for r in assemble_all(caller)]
        with db.connection() as con:
            con.execute("UPDATE api_uploads SET assembly_lease_expires_at = NULL WHERE id = %s", (upload["id"],))
    assert outcomes == ["deferred", "deferred", "failed"]
    body = client.get(f"/v1/files/{file_id}", headers=headers_for(caller)).json()
    assert body["status"] == "error" and body["status_details"] == "The uploaded parts could not be assembled. Upload the file again."


def test_deleting_a_file_while_it_assembles_drops_its_parts(api):
    client, caller, _app = api
    upload = _upload(client, caller, 5)
    client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(caller), content=b"hello")
    file_id = client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={}).json()["file"]["id"]
    assert client.delete(f"/v1/files/{file_id}", headers=headers_for(caller)).status_code == 200
    deadline = time.monotonic() + 5
    while os.path.exists(storage.upload_dir(upload["id"])) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(storage.upload_dir(upload["id"]))
    assert assemble_all(caller) == []


def test_the_sweep_removes_old_leftovers_and_orphan_upload_directories_but_not_live_ones(api):
    client, caller, _app = api
    live = _upload(client, caller, 10)
    storage.ensure_dirs()
    old_single = os.path.join(storage.root(), storage.SINGLE_DIR, "dead.tmp")
    new_single = os.path.join(storage.root(), storage.SINGLE_DIR, "fresh.tmp")
    orphan = storage.upload_dir("upload_" + "9" * 24)
    stale_part_tmp = storage.part_path(live["id"], 3) + ".abc.tmp"
    for path in (old_single, new_single, stale_part_tmp, os.path.join(orphan, "parts", "0")):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "wb").close()
    two_days_ago = time.time() - 2 * 86400
    for path in (old_single, stale_part_tmp, os.path.join(orphan, "parts", "0"), os.path.join(orphan, "parts"), orphan):
        os.utime(path, (two_days_ago, two_days_ago))
    report = uploads_sweep.run_once()
    assert not os.path.exists(old_single) and os.path.exists(new_single)
    assert not os.path.exists(orphan) and report.orphan_upload_dirs_removed >= 1
    assert not os.path.exists(stale_part_tmp) and os.path.isdir(storage.parts_dir(live["id"]))


# ------------------------------------------------------ A-1 on loopback --


class ResettingRelay:
    """Accepts ONE connection, forwards `forward_bytes` of the client's bytes
    to the server, then resets both sides (SO_LINGER 0 → RST)."""

    def __init__(self, target_port: int, forward_bytes: int) -> None:
        self.target_port = target_port
        self.forward_bytes = forward_bytes
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.forwarded = 0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        client, _ = self.listener.accept()
        upstream = socket.create_connection(("127.0.0.1", self.target_port))
        try:
            while self.forwarded < self.forward_bytes:
                data = client.recv(min(MIB, self.forward_bytes - self.forwarded))
                if not data:
                    break
                upstream.sendall(data)
                self.forwarded += len(data)
        finally:
            for sock in (client, upstream, self.listener):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                except OSError:
                    pass
                sock.close()


def _part_bytes(number: int, total: int, part: int) -> bytes:
    size = min(part, total - number * part)
    return random.Random(0x5EED0000 + number).randbytes(size)


def test_a_2_gib_upload_survives_a_dropped_connection_and_resumes(tmp_path):
    total = int(os.environ.get("APIFILES_RESUME_BYTES") or 2 * 1024 ** 3)
    part = 64 * MIB
    parts = -(-total // part)
    drop_at = parts // 2
    timings = {}
    caller = make_caller()
    started = time.monotonic()
    whole_sha, whole_md5 = hashlib.sha256(), hashlib.md5()
    for number in range(parts):
        chunk = _part_bytes(number, total, part)
        whole_sha.update(chunk)
        whole_md5.update(chunk)
    timings["fixture_s"] = round(time.monotonic() - started, 1)
    with serve_process(caller) as (base, server_pid):
        port = int(base.rsplit(":", 1)[1])
        timings["server_rss_start_mib"] = round(peak_rss_mib(server_pid))
        headers = headers_for(caller)
        with httpx.Client(base_url=base, timeout=httpx.Timeout(300.0)) as client:
            upload = client.post("/v1/uploads", headers=headers, json={
                "bytes": total, "filename": "big.bin", "mime_type": "application/octet-stream", "purpose": "user_data"}).json()
            upload_id = upload["id"]
            t0 = time.monotonic()
            ids_by_number = {}
            for number in range(drop_at):
                chunk = _part_bytes(number, total, part)
                response = client.post(f"/v1/uploads/{upload_id}/parts", headers=headers,
                                       files={"data": ("upload", chunk, "application/octet-stream")},
                                       data={"part_number": str(number), "sha256": hashlib.sha256(chunk).hexdigest()})
                assert response.status_code == 200, response.text
                ids_by_number[number] = response.json()["id"]
            again = min(7, drop_at - 1)
            resent = client.post(f"/v1/uploads/{upload_id}/parts", headers=headers,
                                 files={"data": ("upload", _part_bytes(again, total, part), "application/octet-stream")},
                                 data={"part_number": str(again)}).json()
            assert resent["id"] == ids_by_number[again], "a re-sent part keeps its id"
            timings["first_half_s"] = round(time.monotonic() - t0, 1)

        # The drop: part `drop_at` through a relay that resets after 40 MiB.
        relay = ResettingRelay(port, forward_bytes=min(40 * MIB, (part * 5) // 8))
        doomed = _part_bytes(drop_at, total, part)
        with httpx.Client(base_url=f"http://127.0.0.1:{relay.port}", timeout=httpx.Timeout(60.0)) as dropped:
            with pytest.raises(httpx.TransportError):
                dropped.put(f"/v1/uploads/{upload_id}/parts/{drop_at}", headers=headers, content=doomed)
        relay.thread.join(timeout=10)
        assert relay.forwarded >= 30 * MIB
        deadline = time.monotonic() + 20
        while _stray(upload_id) != [str(n) for n in sorted(range(drop_at), key=str)] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert _stray(upload_id) == sorted(str(n) for n in range(drop_at)), "no partial part and no *.tmp remain"

        # A new process (a new client) resumes from the server's state.
        with httpx.Client(base_url=base, timeout=httpx.Timeout(300.0)) as client:
            state = client.get(f"/v1/uploads/{upload_id}", headers=headers).json()
            assert [p["part_number"] for p in state["parts"]] == list(range(drop_at))
            assert state["bytes_received"] == drop_at * part and state["status"] == "pending"
            have = {p["part_number"] for p in state["parts"]}

            def send(number: int) -> int:
                chunk = _part_bytes(number, total, part)
                response = client.put(f"/v1/uploads/{upload_id}/parts/{number}", content=chunk,
                                      headers={**headers, "X-Part-SHA256": hashlib.sha256(chunk).hexdigest()})
                assert response.status_code == 200, response.text
                return number

            t0 = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                assert sorted(pool.map(send, [n for n in range(parts) if n not in have])) == list(range(drop_at, parts))
            timings["second_half_s"] = round(time.monotonic() - t0, 1)

            t0 = time.monotonic()
            done = client.post(f"/v1/uploads/{upload_id}/complete", headers=headers, json={"md5": whole_md5.hexdigest()})
            timings["complete_ms"] = round((time.monotonic() - t0) * 1000)
            assert done.status_code == 200 and done.json()["status"] == "completed"
            file_id = done.json()["file"]["id"]
            t0 = time.monotonic()
            deadline = time.monotonic() + 600
            while True:
                body = client.get(f"/v1/files/{file_id}", headers=headers).json()
                if body["sha256"] or body["status"] == "error" or time.monotonic() > deadline:
                    break
                time.sleep(0.25)
            timings["assembly_s"] = round(time.monotonic() - t0, 1)
            assert body["status"] != "error", body
            assert body["bytes"] == total and body["sha256"] == whole_sha.hexdigest()

            first = client.get(f"/v1/files/{file_id}/content", headers={**headers, "Range": f"bytes=0-{MIB - 1}"})
            assert first.status_code == 206 and first.content == _part_bytes(0, total, part)[:MIB]
            last_part = _part_bytes(parts - 1, total, part)
            last = client.get(f"/v1/files/{file_id}/content", headers={**headers, "Range": f"bytes={total - MIB}-{total - 1}"})
            assert last.status_code == 206 and last.content == last_part[-MIB:]
            t0 = time.monotonic()
            streamed = hashlib.sha256()
            with client.stream("GET", f"/v1/files/{file_id}/content", headers=headers) as download:
                assert int(download.headers["content-length"]) == total
                for piece in download.iter_bytes():
                    streamed.update(piece)
            timings["download_s"] = round(time.monotonic() - t0, 1)
            assert streamed.hexdigest() == whole_sha.hexdigest()

            replay = client.post(f"/v1/uploads/{upload_id}/complete", headers=headers, json={"md5": whole_md5.hexdigest()})
            assert replay.status_code == 200 and replay.json()["id"] == upload_id and replay.json()["file"]["id"] == file_id
            late = client.post(f"/v1/uploads/{upload_id}/parts", headers=headers, files={"data": ("u", b"x", "application/octet-stream")})
            assert late.status_code == 409 and late.headers["x-should-retry"] == "false"
            assert client.delete(f"/v1/files/{file_id}", headers=headers).status_code == 200
            blob_dir = storage.blob_dir(caller.project_id, whole_sha.hexdigest())
            deadline = time.monotonic() + 30
            while os.path.exists(blob_dir) and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not os.path.exists(blob_dir)
            assert not os.path.exists(storage.upload_dir(upload_id))
        timings["server_rss_peak_mib"] = round(peak_rss_mib(server_pid))
    print("A-1 loopback measurements:", timings)
    # The design's pass bar for the orchestrator (A-1): RSS growth ≤ 256 MiB
    # across the whole upload, assembly and download.
    assert timings["server_rss_peak_mib"] - timings["server_rss_start_mib"] <= 256


# --------------------------------------------------- review fixes 2026-09-13 --


def _completed_upload(client, caller, chunks):
    upload = _upload(client, caller, sum(len(c) for c in chunks))
    for number, chunk in enumerate(chunks):
        response = client.put(f"/v1/uploads/{upload['id']}/parts/{number}", headers=headers_for(caller), content=chunk)
        assert response.status_code == 200, response.text
    done = client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={})
    assert done.status_code == 200, done.text
    return upload, done.json()["file"]["id"]


def _lease(upload_id):
    with db.connection() as con:
        return dict(con.execute(
            "SELECT assembly_lease_owner, assembly_lease_expires_at > now() AS held, assembly_attempts, "
            "       assembly_part_numbers FROM api_uploads WHERE id = %s",
            (upload_id,),
        ).fetchone())


def _slow_copy(monkeypatch, per_buffer_s):
    """Every 1 MiB buffer of `partfile.assemble` takes `per_buffer_s` (a slow
    disk). Returns an Event set once the copy is under way."""
    from app.core import partfile

    real = partfile.assemble
    started = threading.Event()

    def slow(*args, should_stop=None, **kwargs):
        def paced():
            started.set()
            time.sleep(per_buffer_s)
            return should_stop() if should_stop is not None else False

        return real(*args, should_stop=paced, **kwargs)

    monkeypatch.setattr(queue.partfile, "assemble", slow)
    return started


def test_stopping_the_runner_stops_the_copy_thread_and_hands_the_lease_back_at_once(api, monkeypatch):
    """Review MEDIUM (2026-09-13): `AssembleRunner.stop()` cancelled the task
    but the `to_thread` copy kept running (the probe held exit 8.01 s), kept
    its lease, and a restart re-copied from byte 0 only after the 90 s lease."""
    client, caller, _app = api
    chunks = [os.urandom(MIB) for _ in range(40)]
    upload, file_id = _completed_upload(client, caller, chunks)
    real_assemble = queue.partfile.assemble
    started = _slow_copy(monkeypatch, 0.05)  # 40 buffers → a 2 s copy

    async def run():
        runner = queue.AssembleRunner(concurrency=1, poll_s=0.2, project_ids=[caller.project_id])
        await runner.start()
        assert await asyncio.to_thread(started.wait, 10)
        await asyncio.sleep(0.2)
        began = time.monotonic()
        await runner.stop()
        return time.monotonic() - began, list(runner.completed)

    stopped_in, completed = asyncio.run(run())
    lease = _lease(upload["id"])
    assert stopped_in < 1.0, stopped_in
    assert [r.outcome for r in completed] == ["deferred"]
    assert lease["assembly_lease_owner"] is None and not lease["held"], "released, not left to lapse"
    assert lease["assembly_attempts"] == 0, "a shutdown is not the upload's fault"
    assert not [n for n in os.listdir(storage.upload_dir(upload["id"])) if n.startswith("assembled")]
    monkeypatch.setattr(queue.partfile, "assemble", real_assemble)
    assert [r.outcome for r in assemble_all(caller)] == ["attached"], "re-claimable at once"
    assert client.get(f"/v1/files/{file_id}/content", headers=headers_for(caller)).content == b"".join(chunks)


def test_each_claim_holds_its_own_lease_so_a_stale_claimant_of_the_same_process_can_neither_renew_nor_attach(api):
    """Review LOW (2026-09-13): every runner shared `OWNER`, so a claimant whose
    lease lapsed kept renewing while a second worker copied the same upload."""
    client, caller, _app = api
    upload, file_id = _completed_upload(client, caller, [b"hello", b"world"])
    first = queue.claim_assembly(project_ids=[caller.project_id], lease_s=0.001)
    time.sleep(0.05)
    second = queue.claim_assembly(project_ids=[caller.project_id])
    assert first["id"] == second["id"] == upload["id"]
    assert first["assembly_lease_owner"] != second["assembly_lease_owner"]
    assert first["assembly_lease_owner"].startswith(queue.OWNER + "#")
    assert queue.renew_assembly(upload["id"], 1, owner=first["assembly_lease_owner"]) is False
    assert queue.assemble_claimed(first).outcome == "abandoned"
    assert _stray(upload["id"]) == ["0", "1"], "the stale claimant touches nothing of the live claim"
    assert queue.assemble_claimed(second).outcome == "attached"
    assert client.get(f"/v1/files/{file_id}/content", headers=headers_for(caller)).content == b"helloworld"


def test_the_assembly_lease_is_renewed_on_a_timer_while_no_progress_tick_arrives(api, monkeypatch):
    """A copy that moves no 256 MiB boundary for longer than the lease (a slow
    disk, a final fsync of a huge file) keeps its lease; before the timer the
    same process could claim it a second time."""
    client, caller, _app = api
    monkeypatch.setenv("PUBLIC_API_FILES_ASSEMBLY_LEASE_S", "0.6")
    monkeypatch.setenv("PUBLIC_API_FILES_ASSEMBLY_RENEW_S", "0.15")
    upload, _file_id = _completed_upload(client, caller, [os.urandom(MIB)] * 30)
    started = _slow_copy(monkeypatch, 0.05)  # 1.5 s, no 256 MiB tick
    claimed = queue.claim_assembly(project_ids=[caller.project_id])
    outcome = {}
    worker = threading.Thread(target=lambda: outcome.update(r=queue.assemble_claimed(claimed)))
    worker.start()
    assert started.wait(10)
    time.sleep(1.0)  # well past the 0.6 s lease
    assert queue.claim_assembly(project_ids=[caller.project_id]) is None, "the lease is still held"
    worker.join(20)
    assert outcome["r"].outcome == "attached"


def test_a_part_left_on_disk_by_a_failed_retry_fails_the_file_instead_of_being_stitched_in(api, monkeypatch):
    """Review LOW (2026-09-13): part 0 accepted as AAA, a retry with BBB fails
    after its rename; the resume view showed sha(AAA) while disk held BBB, and
    the assembly attached BBB with no error."""
    from app.apifiles import ids as ids_mod

    client, caller, _app = api
    upload = _upload(client, caller, 3)
    url = f"/v1/uploads/{upload['id']}/parts/0"
    assert client.put(url, headers=headers_for(caller), content=b"AAA").status_code == 200

    def failing_part_id():
        raise RuntimeError("the row write fails after the rename")

    real_part_id = ids_mod.new_part_id
    monkeypatch.setattr(ids_mod, "new_part_id", failing_part_id)
    assert client.put(url, headers=headers_for(caller), content=b"BBB").status_code == 500
    monkeypatch.setattr(ids_mod, "new_part_id", real_part_id)
    with open(storage.part_path(upload["id"], 0), "rb") as fh:
        assert fh.read() == b"BBB", "the precondition the review found: disk and row disagree"
    view = client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()
    assert view["parts"][0]["sha256"] == hashlib.sha256(b"AAA").hexdigest()
    file_id = client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={}).json()["file"]["id"]
    assert [r.outcome for r in assemble_all(caller)] == ["failed"]
    body = client.get(f"/v1/files/{file_id}", headers=headers_for(caller)).json()
    assert body["status"] == "error"


def test_an_error_after_the_copy_removes_the_copy_and_releases_the_lease(api, monkeypatch):
    """Review MEDIUM (2026-09-13): anything raising between the copy and the
    attach (a sniff RecursionError then) left a full-size `assembled.*.tmp`
    per attempt and the lease held until it lapsed."""
    client, caller, _app = api
    upload, _file_id = _completed_upload(client, caller, [b"[" * 60000])

    def broken_attach(*args, **kwargs):
        raise RuntimeError("stands in for any failure after the copy")

    real_attach = queue.schema.attach_blob_to_assembling_file
    monkeypatch.setattr(queue.schema, "attach_blob_to_assembling_file", broken_attach)
    claimed = queue.claim_assembly(project_ids=[caller.project_id])
    with pytest.raises(RuntimeError):
        queue.assemble_claimed(claimed)
    assert not [n for n in os.listdir(storage.upload_dir(upload["id"])) if n.startswith("assembled")]
    assert _lease(upload["id"])["assembly_lease_owner"] is None
    monkeypatch.setattr(queue.schema, "attach_blob_to_assembling_file", real_attach)
    with db.connection() as con:
        con.execute("UPDATE api_uploads SET assembly_lease_expires_at = NULL WHERE id = %s", (upload["id"],))
    results = assemble_all(caller)
    assert [r.outcome for r in results] == ["attached"], "60,000 `[` assembles as text now"
    assert results[0].blob["mime_type"] == "text/plain"
