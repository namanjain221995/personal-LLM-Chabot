"""The voice archive store keeps exactly what it was sent, or nothing.

Each test is one promise compose/voice-store/server.py makes to the
orchestrator (app/voice_archive.py), which deletes the head's copy of a
recording once this store holds it: a 201 means the bytes on disk are the
bytes declared, fsynced and linked into place; a refusal leaves nothing
behind; an object is never overwritten; reads honour Range and If-Range;
and every route under /v1 needs the token.
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import time

import pytest

from store_helpers import (
    AUTH, OTHER_TOKEN, SID, TOKEN, UID, files_under, leftovers, object_url, put, put_headers, server, sha,
)

DATA = bytes(range(256)) * 300  # 76,800 bytes: about 5 s of a browser recording


# ------------------------------------------------------------------ auth --


def test_every_route_under_v1_needs_the_token_and_health_does_not(store):
    client, _store, root = store
    for method, url in (
        ("PUT", object_url()), ("GET", object_url()), ("HEAD", object_url()),
        ("DELETE", f"/v1/recordings/{UID}/{SID}"), ("GET", "/v1/inventory"),
    ):
        for headers in ({}, {"authorization": "Bearer wrong-token-" + "c" * 40}, {"authorization": TOKEN}):
            response = client.request(method, url, headers={**headers, "x-content-sha256": sha(DATA)},
                                      content=DATA if method == "PUT" else None)
            assert response.status_code == 401, (method, url, headers, response.text)
    assert files_under(root) == [], "a refused upload writes nothing"
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_a_rotated_token_list_accepts_both_tokens_during_the_overlap(make_store):
    client, _store, _root = make_store(tokens=(TOKEN, OTHER_TOKEN))
    assert put(client, DATA).status_code == 201
    other = {"authorization": f"bearer {OTHER_TOKEN}"}
    assert client.get(object_url(), headers=other).content == DATA


def test_settings_refuse_to_start_without_a_long_token_and_the_bind_refuses_a_wildcard():
    with pytest.raises(SystemExit, match="VOICE_STORE_TOKENS"):
        server.Settings.from_env({})
    with pytest.raises(SystemExit, match="at least 32"):
        server.Settings.from_env({"VOICE_STORE_TOKENS": "short"})
    settings = server.Settings.from_env({"VOICE_STORE_TOKENS": f" {TOKEN} , {OTHER_TOKEN} "})
    assert settings.tokens == (TOKEN, OTHER_TOKEN)
    assert settings.min_free_bytes == 250 * 1024 ** 3, "the worker's floor is 250 GiB by default"
    for bind in ("", "0.0.0.0", "::", "[::]"):
        with pytest.raises(SystemExit):
            server._bind_address({"VOICE_STORE_BIND": bind})
    assert server._bind_address({"VOICE_STORE_BIND": "192.168.9.68"}) == "192.168.9.68"


# ------------------------------------------------------------ the names --


@pytest.mark.parametrize("uid,sid,name", [
    ("07", SID, "source.webm"),
    ("abc", SID, "source.webm"),
    ("-1", SID, "source.webm"),
    (UID, SID.upper(), "source.webm"),
    (UID, SID[:-1], "source.webm"),
    (UID, SID, "source.exe"),
    (UID, SID, "manifest.json"),
    (UID, SID, ".incoming-x"),
    (UID, SID, "source.webm.part"),
    (UID, "..", "source.webm"),
    (UID, "%2e%2e", "source.webm"),
])
def test_a_malformed_id_or_name_is_refused_and_nothing_is_written(store, uid, sid, name):
    client, _store, root = store
    response = client.put(f"/v1/recordings/{uid}/{sid}/{name}", content=DATA, headers=put_headers(DATA))
    assert response.status_code in (400, 404), response.text
    assert files_under(root) == []
    assert not any(p.name.startswith("source") for p in root.parent.rglob("*") if p.is_file())


def test_an_encoded_slash_or_traversal_never_leaves_the_store_root(store, tmp_path):
    client, _store, root = store
    for path in (
        f"/v1/recordings/{UID}/{SID}/..%2F..%2Fescape.webm",
        f"/v1/recordings/{UID}/%2e%2e%2f%2e%2e/source.webm",
        f"/v1/recordings/{UID}/{SID}/source.webm%00",
    ):
        response = client.put(path, content=DATA, headers=put_headers(DATA))
        assert response.status_code in (400, 404), (path, response.status_code)
    assert files_under(root) == []
    assert not (tmp_path / "escape.webm").exists()


# ------------------------------------------------------------- refusals --


def test_a_chunked_upload_is_refused_411_because_its_size_cannot_be_checked(store):
    client, _store, root = store
    response = client.put(object_url(), content=iter([DATA[:100], DATA[100:]]), headers=put_headers(DATA))
    assert response.status_code == 411 and response.json()["reason"] == "length_required"
    assert files_under(root) == []


def test_a_full_disk_is_refused_507_before_a_byte_of_the_body_is_read(make_store):
    client, store, root = make_store(min_free_bytes=1 << 62)
    reads = []
    original = server.Store._receive

    async def spy(self, *args, **kwargs):
        reads.append(args)
        return await original(self, *args, **kwargs)

    server.Store._receive = spy
    try:
        response = put(client, DATA)
    finally:
        server.Store._receive = original
    assert response.status_code == 507 and response.json()["reason"] == "storage_full"
    assert reads == [], "the body was never read"
    assert files_under(root) == []
    assert store.health_body()["reserved_bytes"] == 0


def test_the_floor_counts_the_uploads_already_in_flight(make_store, monkeypatch):
    client, store, _root = make_store(min_free_bytes=10_000_000)
    # A fixed free-space reading: a real disk moves by more than one upload
    # between two statvfs calls.
    monkeypatch.setattr(store, "free_bytes", lambda: 10_000_000 + 2 * len(DATA))
    store._admit(("9", "f" * 32), len(DATA) * 3 // 2, "/nonexistent")
    try:
        refused = put(client, DATA)
        assert refused.status_code == 507, "free minus reservations minus this upload is under the floor"
    finally:
        store._release(("9", "f" * 32), len(DATA) * 3 // 2)
    assert put(client, DATA).status_code == 201, "with nothing in flight the same upload fits"


def test_an_object_over_the_limit_is_refused_413(make_store):
    client, _store, root = make_store(max_object_bytes=1000)
    response = put(client, DATA)
    assert response.status_code == 413
    assert files_under(root) == []


def test_a_wrong_sha_or_length_is_422_and_leaves_nothing(store):
    client, _store, root = store
    wrong = put(client, DATA, headers={**put_headers(DATA), "x-content-sha256": sha(b"other")})
    assert wrong.status_code == 422 and wrong.json()["reason"] == "sha_mismatch"
    short = client.put(object_url(), content=DATA, headers={**put_headers(DATA), "content-length": str(len(DATA) + 10)})
    assert short.status_code == 422 and short.json()["reason"] == "length_mismatch"
    long = client.put(object_url(), content=DATA, headers={**put_headers(DATA), "content-length": str(len(DATA) - 10)})
    assert long.status_code == 422 and long.json()["reason"] == "length_mismatch"
    assert [p for p in files_under(root) if "source" in p] == []
    assert leftovers(root) == []


def test_a_malformed_sha_header_or_an_empty_body_is_a_bad_request(store):
    client, _store, root = store
    assert put(client, DATA, headers={**AUTH, "x-content-sha256": "ABC"}).status_code == 400
    assert put(client, b"", headers={**AUTH, "x-content-sha256": sha(b"")}).status_code == 400
    assert files_under(root) == []


def test_two_uploads_at_most_run_at_once_and_one_object_has_one_upload(make_store):
    client, store, _root = make_store(max_concurrent_puts=1)
    store._admit(("9", "e" * 32), 10, "/nonexistent")
    try:
        busy = put(client, DATA)
        assert busy.status_code == 503 and busy.json()["reason"] == "busy"
        assert busy.headers["retry-after"] == "5"
    finally:
        store._release(("9", "e" * 32), 10)
    client, store, _root = make_store(max_concurrent_puts=2)
    store._admit((UID, SID), 10, "/nonexistent")
    try:
        same = put(client, DATA)
        assert same.status_code == 503 and same.json()["reason"] == "in_progress"
    finally:
        store._release((UID, SID), 10)


# ------------------------------------------------------ what is kept --


def test_a_stored_recording_is_byte_exact_private_and_described_by_its_manifest(store):
    client, _store, root = store
    response = put(client, DATA, headers=put_headers(
        DATA, **{"x-recording-created-at": "2026-09-29T10:00:00+00:00", "x-recording-finished-at": "2026-09-29T10:05:00+00:00"},
    ))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["sha256"] == sha(DATA) and body["bytes"] == len(DATA) and body["stored"] == "new"
    folder = root / UID / SID
    assert (folder / "source.webm").read_bytes() == DATA
    assert stat.S_IMODE(os.stat(folder / "source.webm").st_mode) == 0o600
    for directory in (root, root / UID, folder):
        assert stat.S_IMODE(os.stat(directory).st_mode) == 0o700, directory
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["user_id"] == 7 and manifest["session_id"] == SID and manifest["name"] == "source.webm"
    assert manifest["mime"] == "audio/webm" and manifest["bytes"] == len(DATA) and manifest["sha256"] == sha(DATA)
    assert manifest["created_at"].startswith("2026-09-29T10:00") and manifest["stored_at"]
    assert stat.S_IMODE(os.stat(folder / "manifest.json").st_mode) == 0o600
    assert leftovers(root) == []


def test_the_same_recording_again_is_200_and_different_bytes_are_409_and_never_replace_it(store):
    client, _store, root = store
    assert put(client, DATA).status_code == 201
    again = put(client, DATA)
    assert again.status_code == 200 and again.json()["stored"] == "already"
    other = DATA[::-1]
    conflict = put(client, other)
    assert conflict.status_code == 409 and conflict.json()["reason"] == "conflict"
    other_ext = put(client, DATA, name="source.ogg")
    assert other_ext.status_code == 409
    assert (root / UID / SID / "source.webm").read_bytes() == DATA
    assert not (root / UID / SID / "source.ogg").exists()


def test_an_object_whose_manifest_was_lost_is_rehashed_not_replaced(store):
    client, _store, root = store
    assert put(client, DATA).status_code == 201
    (root / UID / SID / "manifest.json").unlink()
    assert put(client, DATA).status_code == 200
    manifest = json.loads((root / UID / SID / "manifest.json").read_text())
    assert manifest["sha256"] == sha(DATA) and manifest["rebuilt"] is True


def test_the_file_is_fsynced_before_it_is_linked_and_the_folder_after(store, monkeypatch):
    client, _store, _root = store
    events = []
    real_fsync, real_link, real_replace = os.fsync, os.link, os.replace

    def fsync(fd):
        events.append("dirsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync")
        return real_fsync(fd)

    def link(src, dst, *a, **k):
        events.append("link")
        return real_link(src, dst, *a, **k)

    def replace(src, dst, *a, **k):
        events.append("replace")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "link", link)
    monkeypatch.setattr(os, "replace", replace)
    assert put(client, DATA).status_code == 201
    assert events == ["fsync", "link", "dirsync", "fsync", "replace", "dirsync"], events


# --------------------------------------------------- uploads that die --


def _asgi_put(app, receive_messages, *, length: int, digest: str):
    """Drive the app directly: `receive_messages` is what the client sends."""
    sent = []
    queue = list(receive_messages)

    async def receive():
        if queue:
            item = queue.pop(0)
            if item == "hang":
                await asyncio.sleep(3600)
            return item
        await asyncio.sleep(3600)

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT",
        "scheme": "http", "path": object_url(), "raw_path": object_url().encode(), "query_string": b"",
        "root_path": "", "server": ("test", 80), "client": ("test", 1),
        "headers": [
            (b"authorization", f"Bearer {TOKEN}".encode()), (b"content-length", str(length).encode()),
            (b"x-content-sha256", digest.encode()),
        ],
    }

    asyncio.run(app(scope, receive, send))
    starts = [m for m in sent if m["type"] == "http.response.start"]
    return starts[0]["status"] if starts else None


def test_a_client_that_disconnects_mid_upload_leaves_no_object_and_no_temporary_file(make_store):
    _client, store, root = make_store()
    app = _client.app
    status = _asgi_put(
        app,
        [{"type": "http.request", "body": DATA[:1000], "more_body": True}, {"type": "http.disconnect"}],
        length=len(DATA), digest=sha(DATA),
    )
    assert status in (400, None)
    assert files_under(root) == [] or all(not p.endswith("source.webm") for p in files_under(root))
    assert leftovers(root) == []
    assert store.health_body()["puts_in_flight"] == 0 and store.health_body()["reserved_bytes"] == 0


def test_an_upload_that_stalls_is_abandoned_408_and_cleaned_up(make_store):
    _client, store, root = make_store(read_idle_timeout_s=0.2)
    status = _asgi_put(
        _client.app, [{"type": "http.request", "body": DATA[:1000], "more_body": True}, "hang"],
        length=len(DATA), digest=sha(DATA),
    )
    assert status == 408
    assert leftovers(root) == [] and not (root / UID / SID / "source.webm").exists()
    assert store.health_body()["puts_in_flight"] == 0


def test_the_sweep_removes_abandoned_temporary_files_but_not_an_upload_in_flight(store):
    _client, store_, root = store
    folder = root / UID / SID
    folder.mkdir(parents=True)
    stale = folder / ".incoming-stale"
    fresh = folder / ".incoming-fresh"
    live = folder / ".incoming-live"
    for path in (stale, fresh, live):
        path.write_bytes(b"x")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    os.utime(live, (old, old))
    store_._admit(("8", "d" * 32), 1, str(live))
    try:
        assert store_.sweep_once() == 1
    finally:
        store_._release(("8", "d" * 32), 1)
    assert not stale.exists() and fresh.exists() and live.exists()


# ------------------------------------------------------------- reading --


def test_range_suffix_if_range_and_416_are_answered_like_a_file(store):
    client, _store, _root = store
    assert put(client, DATA).status_code == 201
    full = client.get(object_url(), headers=AUTH)
    assert full.status_code == 200 and full.content == DATA
    assert full.headers["etag"] == f'"{sha(DATA)}"' and full.headers["accept-ranges"] == "bytes"
    assert full.headers["content-type"].startswith("audio/webm")
    part = client.get(object_url(), headers={**AUTH, "range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == DATA[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(DATA)}"
    tail = client.get(object_url(), headers={**AUTH, "range": "bytes=-10"})
    assert tail.status_code == 206 and tail.content == DATA[-10:]
    beyond = client.get(object_url(), headers={**AUTH, "range": f"bytes={len(DATA) + 5}-"})
    assert beyond.status_code == 416 and beyond.headers["content-range"] == f"bytes */{len(DATA)}"
    same = client.get(object_url(), headers={**AUTH, "range": "bytes=0-9", "if-range": f'"{sha(DATA)}"'})
    assert same.status_code == 206 and same.content == DATA[:10]
    stale = client.get(object_url(), headers={**AUTH, "range": "bytes=0-9", "if-range": '"not-this-one"'})
    assert stale.status_code == 200 and stale.content == DATA


def test_head_reports_the_size_and_sha_without_a_body(store):
    client, _store, _root = store
    assert put(client, DATA).status_code == 201
    head = client.head(object_url(), headers=AUTH)
    assert head.status_code == 200 and head.content == b""
    assert head.headers["content-length"] == str(len(DATA))
    assert head.headers["x-content-sha256"] == sha(DATA)
    assert client.head(object_url(sid="f" * 32), headers=AUTH).status_code == 404


def test_delete_is_always_204_and_removes_the_folder(store):
    client, store_, root = store
    assert put(client, DATA).status_code == 201
    assert client.delete(f"/v1/recordings/{UID}/{SID}", headers=AUTH).status_code == 204
    assert not (root / UID).exists(), "an emptied user folder goes too"
    assert client.get(object_url(), headers=AUTH).status_code == 404
    assert client.delete(f"/v1/recordings/{UID}/{SID}", headers=AUTH).status_code == 204, "idempotent"


def test_the_inventory_pages_in_numeric_user_order(store):
    client, _store, _root = store
    sids = [f"{i:032x}" for i in range(1, 4)]
    for uid in ("10", "2"):
        for sid in sids:
            assert put(client, DATA + sid.encode(), uid=uid, sid=sid).status_code == 201
    seen = []
    after = ""
    pages = 0
    while True:
        page = client.get("/v1/inventory", params={"after": after, "limit": 2}, headers=AUTH).json()
        pages += 1
        seen += [(o["user_id"], o["session_id"]) for o in page["objects"]]
        for item in page["objects"]:
            assert item["sha256"] == sha(DATA + item["session_id"].encode())
            assert item["bytes"] == len(DATA) + 32 and item["name"] == "source.webm" and item["stored_at"]
        if not page["next_after"]:
            break
        after = page["next_after"]
    assert seen == [(2, s) for s in sids] + [(10, s) for s in sids]
    assert pages == 3
    bad = client.get("/v1/inventory", params={"after": "../x"}, headers=AUTH)
    assert bad.status_code == 400


# ------------------------------------------------------- health, scrub --


def test_health_and_metrics_report_counts_free_space_and_bounded_labels(store):
    client, store_, _root = store
    store_.scan()
    assert put(client, DATA).status_code == 201
    health = client.get("/health").json()
    assert health["ready"] is True and health["objects"] == 1 and health["bytes"] == len(DATA)
    assert health["free_bytes"] > 0 and health["min_free_bytes"] == 0
    client.get(object_url(), headers=AUTH)
    client.get(object_url(sid="f" * 32), headers=AUTH)
    text = client.get("/metrics").text
    assert "voice_store_objects 1" in text
    assert 'voice_store_requests_total{op="put",code="201"} 1' in text
    assert 'voice_store_requests_total{op="get",code="404"} 1' in text
    labels = [line.split("{", 1)[1] for line in text.splitlines() if line.startswith("voice_store_requests_total{")]
    assert labels and all(SID not in label for label in labels), "no id ever becomes a label"


def test_the_scrub_finds_an_object_whose_bytes_changed_on_disk(store):
    client, store_, root = store
    assert put(client, DATA).status_code == 201
    assert put(client, DATA, sid="e" * 32).status_code == 201
    assert store_.scrub_once()["mismatches"] == 0
    path = root / UID / SID / "source.webm"
    damaged = bytearray(path.read_bytes())
    damaged[10] ^= 0xFF
    path.write_bytes(bytes(damaged))
    result = store_.scrub_once()
    assert result["checked"] == 2 and result["mismatches"] == 1
    assert "voice_store_scrub_mismatches 1" in client.get("/metrics").text
