"""`/v1/files` routes 1–5 (design §2.3–§2.7) against the real handlers, stub auth."""
from __future__ import annotations

import asyncio
import hashlib
import os
import time

import pytest
from starlette.testclient import TestClient

from app import db
from app.apifiles import limits, storage
from tests.apifiles_test_support import (  # noqa: F401 - fixture by name
    MULTIPART,
    StubAuth,
    asgi_call,
    assemble_all,
    build_app,
    headers_for,
    isolated_app_db,
    make_caller,
    multipart_body,
)


@pytest.fixture()
def api():
    caller = make_caller()
    auth = StubAuth(caller)
    usage: list = []
    app = build_app(auth, usage=usage)
    with TestClient(app) as client:
        yield client, caller, usage, app


def _create(client, caller, data=b"%PDF-1.4 hello", filename="doc.pdf", fields=None, first=False):
    parts = [("purpose", "user_data")] + list(fields or [])
    file_part = ("file", filename, data, "application/octet-stream")
    body = multipart_body(([file_part] + parts) if first else (parts + [file_part]))
    return client.post("/v1/files", headers={**headers_for(caller), "Content-Type": MULTIPART}, content=body)


def _tree(root):
    out = []
    for base, _dirs, files in os.walk(root):
        for name in files:
            out.append(os.path.relpath(os.path.join(base, name), root))
    return sorted(out)


def test_a_file_is_accepted_with_the_file_part_first_or_last(api):
    client, caller, _usage, _app = api
    last = _create(client, caller)
    first = _create(client, caller, data=b"%PDF-1.4 other", first=True)
    for response in (last, first):
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["object"] == "file" and body["status"] == "uploaded" and body["purpose"] == "user_data"
        assert body["mime_type"] == "application/pdf", "sniffed from the bytes, not the octet-stream part type"
        assert body["processing"]["kind"] == "pdf" and body["processing"]["state"] == "queued"
    assert last.json()["sha256"] == hashlib.sha256(b"%PDF-1.4 hello").hexdigest()


def test_the_stored_original_is_the_bytes_and_nothing_is_left_in_single(api):
    client, caller, _usage, _app = api
    data = os.urandom(300_000)
    body = _create(client, caller, data=data, filename="blob.bin").json()
    original = storage.original_path(caller.project_id, body["sha256"])
    assert open(original, "rb").read() == data
    assert oct(os.stat(original).st_mode & 0o777) == "0o640"
    assert os.listdir(os.path.join(storage.root(), storage.SINGLE_DIR)) == []


def test_a_chunked_body_over_the_file_cap_is_refused_while_streaming_and_leaves_nothing(api, monkeypatch):
    client, caller, _usage, _app = api
    monkeypatch.setenv("PUBLIC_API_FILES_SINGLE_MAX_BYTES", "100000")
    body = multipart_body([("purpose", "user_data"), ("file", "big.bin", b"x" * 100_001, "application/octet-stream")])

    def chunks():
        for start in range(0, len(body), 8192):
            yield body[start:start + 8192]

    response = client.post("/v1/files", headers={**headers_for(caller), "Content-Type": MULTIPART}, content=chunks())
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert "/v1/uploads" in response.json()["error"]["message"]
    assert _tree(storage.root()) == []


def test_a_declared_length_over_the_body_cap_is_refused_before_reading(api):
    client, caller, _usage, _app = api
    response = client.post(
        "/v1/files",
        headers={**headers_for(caller), "Content-Type": MULTIPART, "Content-Length": str(limits.max_body_bytes() + 1)},
        content=b"",
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "fields,param",
    [
        ([("purpose", "batch")], "purpose"),
        ([("purpose", "user_data"), ("colour", "blue")], "colour"),
        ([("purpose", "user_data"), ("expires_after[anchor]", "created_at"), ("expires_after[seconds]", "60")], "expires_after.seconds"),
        ([("purpose", "user_data"), ("expires_after[seconds]", "7200")], "expires_after.anchor"),
    ],
)
def test_bad_form_fields_are_400s_naming_the_field(api, fields, param):
    client, caller, _usage, _app = api
    body = multipart_body(fields + [("file", "a.txt", b"hi", "text/plain")])
    response = client.post("/v1/files", headers={**headers_for(caller), "Content-Type": MULTIPART}, content=body)
    assert response.status_code == 400
    assert response.json()["error"]["param"] == param
    assert _tree(storage.root()) == []


def test_a_batch_purpose_is_told_there_is_no_batch_product(api):
    client, caller, _usage, _app = api
    body = multipart_body([("purpose", "fine-tune"), ("file", "a.txt", b"hi", "text/plain")])
    response = client.post("/v1/files", headers={**headers_for(caller), "Content-Type": MULTIPART}, content=body)
    assert "user_data" in response.json()["error"]["message"]


def test_a_missing_file_part_or_a_non_multipart_body_is_a_400(api):
    client, caller, _usage, _app = api
    no_file = client.post("/v1/files", headers={**headers_for(caller), "Content-Type": MULTIPART},
                          content=multipart_body([("purpose", "user_data")]))
    assert no_file.status_code == 400 and no_file.json()["error"]["param"] == "file"
    as_json = client.post("/v1/files", headers=headers_for(caller), json={"purpose": "user_data"})
    assert as_json.status_code == 400 and as_json.json()["error"]["param"] == "Content-Type"


def test_a_client_that_leaves_mid_body_gets_408_and_leaves_nothing(api):
    _client, caller, usage, app = api
    body = multipart_body([("purpose", "user_data"), ("file", "a.bin", os.urandom(200_000), "application/octet-stream")])
    chunks = [body[i:i + 16_384] for i in range(0, len(body), 16_384)]
    result = asyncio.run(asgi_call(app, "POST", "/v1/files", headers={**headers_for(caller), "content-type": MULTIPART},
                                   body_chunks=chunks, disconnect_after=5))
    assert result["status"] == 408
    assert b"incomplete_body" in result["body"] and result["headers"]["x-should-retry"] == "true"
    assert _tree(storage.root()) == []
    assert usage[-1].error_code == "incomplete_body"


def test_the_filename_is_metadata_never_a_path(api):
    client, caller, _usage, _app = api
    body = _create(client, caller, data=b"hello", filename="../../etc/passwd").json()
    assert body["filename"] == "passwd"
    assert all(".." not in path and "passwd" not in path for path in _tree(storage.root()))


def test_identical_bytes_in_one_project_share_one_blob_and_survive_one_delete(api):
    client, caller, usage, _app = api
    first = _create(client, caller, data=b"same bytes", filename="a.txt").json()
    second = _create(client, caller, data=b"same bytes", filename="b.txt").json()
    assert first["id"] != second["id"] and first["sha256"] == second["sha256"]
    assert usage[-1].meta["deduplicated"] is True
    with db.connection() as con:
        blobs = con.execute("SELECT count(*) AS n FROM api_file_blobs WHERE project_id = %s", (caller.project_id,)).fetchone()["n"]
    assert blobs == 1
    headers = headers_for(caller)
    assert client.delete(f"/v1/files/{first['id']}", headers=headers).json() == {"id": first["id"], "object": "file", "deleted": True}
    assert client.get(f"/v1/files/{second['id']}/content", headers=headers).content == b"same bytes"
    assert client.delete(f"/v1/files/{second['id']}", headers=headers).status_code == 200
    blob_dir = storage.blob_dir(caller.project_id, first["sha256"])
    deadline = time.monotonic() + 5
    while os.path.exists(blob_dir) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not os.path.exists(blob_dir), "the last delete purges the bytes"


def test_the_same_bytes_can_be_uploaded_again_right_after_their_delete(api):
    client, caller, _usage, _app = api
    headers = headers_for(caller)
    first = _create(client, caller, data=b"again").json()
    assert client.delete(f"/v1/files/{first['id']}", headers=headers).status_code == 200
    again = _create(client, caller, data=b"again")
    assert again.status_code == 200, again.text
    assert client.get(f"/v1/files/{again.json()['id']}/content", headers=headers).content == b"again"


def test_delete_is_200_once_then_404_and_the_tombstone_is_invisible(api):
    client, caller, _usage, _app = api
    headers = headers_for(caller)
    created = _create(client, caller).json()
    assert client.delete(f"/v1/files/{created['id']}", headers=headers).status_code == 200
    for method, path in (("DELETE", ""), ("GET", ""), ("GET", "/content")):
        response = client.request(method, f"/v1/files/{created['id']}{path}", headers=headers)
        assert response.status_code == 404 and response.json()["error"]["code"] == "file_not_found"
    assert created["id"] not in [f["id"] for f in client.get("/v1/files", headers=headers).json()["data"]]
    with db.connection() as con:
        tomb = con.execute("SELECT blob_id, deleted_at, filename FROM api_files WHERE id = %s", (created["id"],)).fetchone()
    assert tomb["blob_id"] is None and tomb["deleted_at"] is not None and tomb["filename"] == "doc.pdf"


def test_list_paginates_by_keyset_in_both_orders(api):
    client, caller, _usage, _app = api
    headers = headers_for(caller)
    made = [_create(client, caller, data=f"file {i}".encode(), filename=f"{i}.txt").json()["id"] for i in range(5)]
    page = client.get("/v1/files", headers=headers, params={"limit": 2}).json()
    assert page["object"] == "list" and page["has_more"] is True
    assert [f["id"] for f in page["data"]] == made[::-1][:2]
    assert page["first_id"] == made[4] and page["last_id"] == made[3]
    seen = []
    after = None
    while True:
        params = {"limit": 2, "order": "asc", **({"after": after} if after else {})}
        page = client.get("/v1/files", headers=headers, params=params).json()
        seen += [f["id"] for f in page["data"]]
        if not page["has_more"]:
            break
        after = page["last_id"]
    assert seen == made


def test_deleting_the_last_item_of_a_page_before_the_next_request_keeps_the_loop_working(api):
    """Finding #8: the SDK paginator sends `after=<last id>`, and a cleanup
    loop has already deleted it."""
    client, caller, _usage, _app = api
    headers = headers_for(caller)
    made = [_create(client, caller, data=f"n{i}".encode()).json()["id"] for i in range(4)]
    deleted = []
    after = None
    while True:
        page = client.get("/v1/files", headers=headers, params={"limit": 1, **({"after": after} if after else {})})
        assert page.status_code == 200, page.text
        body = page.json()
        for item in body["data"]:
            assert client.delete(f"/v1/files/{item['id']}", headers=headers).status_code == 200
            deleted.append(item["id"])
        if not body["has_more"]:
            break
        after = body["last_id"]
    assert sorted(deleted) == sorted(made)


def test_an_unknown_cursor_is_one_400_whether_malformed_absent_or_another_projects(api):
    client, caller, _usage, _app = api
    other = make_caller()
    foreign = _create(TestClient(build_app(StubAuth(other))), other).json()["id"]
    bodies = []
    for after in ("file-" + "0" * 24, "garbage", foreign):
        response = client.get("/v1/files", headers=headers_for(caller), params={"after": after})
        assert response.status_code == 400
        error = response.json()["error"]
        error.pop("request_id")
        bodies.append(error)
    assert bodies[0] == bodies[1] == bodies[2]


def test_list_refuses_unknown_parameters_and_bad_limits(api):
    client, caller, _usage, _app = api
    headers = headers_for(caller)
    assert client.get("/v1/files", headers=headers, params={"limit": 0}).status_code == 400
    assert client.get("/v1/files", headers=headers, params={"limit": 10_001}).status_code == 400
    assert client.get("/v1/files", headers=headers, params={"order": "sideways"}).status_code == 400
    assert client.get("/v1/files", headers=headers, params={"project_id": "x"}).status_code == 400
    assert client.get("/v1/files", headers=headers, params={"purpose": "batch"}).json()["data"] == []


def test_content_carries_the_safe_headers_and_honours_range_and_etag(api):
    client, caller, usage, _app = api
    headers = headers_for(caller)
    data = os.urandom(5000)
    created = _create(client, caller, data=data, filename='<script> "evil".html').json()
    url = f"/v1/files/{created['id']}/content"
    full = client.get(url, headers={**headers, "Accept": "application/binary"})
    assert full.status_code == 200 and full.content == data
    assert full.headers["content-type"] == "application/octet-stream"
    assert full.headers["content-length"] == "5000"
    assert full.headers["x-content-type-options"] == "nosniff"
    assert full.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert full.headers["cache-control"] == "private, no-store"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.headers["etag"] == f'"{created["sha256"]}"'
    disposition = full.headers["content-disposition"]
    assert disposition.startswith("attachment; ") and '"evil"' not in disposition and "filename*=UTF-8''" in disposition
    part = client.get(url, headers={**headers, "Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == data[100:200]
    assert part.headers["content-range"] == "bytes 100-199/5000" and part.headers["content-length"] == "100"
    tail = client.get(url, headers={**headers, "Range": "bytes=-10"})
    assert tail.status_code == 206 and tail.content == data[-10:]
    several = client.get(url, headers={**headers, "Range": "bytes=0-1,5-6"})
    assert several.status_code == 200 and several.content == data
    beyond = client.get(url, headers={**headers, "Range": "bytes=5000-"})
    assert beyond.status_code == 416 and beyond.headers["content-range"] == "bytes */5000"
    assert beyond.json()["error"]["code"] == "invalid_request_error"
    cached = client.get(url, headers={**headers, "If-None-Match": f'"{created["sha256"]}"'})
    assert cached.status_code == 304 and cached.content == b""
    assert usage[-5].meta["range"] == "100-199"


def test_a_file_with_an_expiry_disappears_when_it_passes(api):
    client, caller, _usage, _app = api
    headers = headers_for(caller)
    created = _create(client, caller, fields=[("expires_after[anchor]", "created_at"), ("expires_after[seconds]", "3600")]).json()
    assert created["expires_at"] == created["created_at"] + 3600
    with db.connection() as con:
        con.execute("UPDATE api_files SET expires_at = now() - interval '1 second' WHERE id = %s", (created["id"],))
    assert client.get(f"/v1/files/{created['id']}", headers=headers).status_code == 404
    assert client.delete(f"/v1/files/{created['id']}", headers=headers).status_code == 404


def test_an_idempotency_key_is_refused_naming_the_header(api):
    client, caller, _usage, _app = api
    response = client.get("/v1/files", headers={**headers_for(caller), "Idempotency-Key": "k1"})
    assert response.status_code == 400 and response.json()["error"]["param"] == "Idempotency-Key"


def test_scope_and_credential_are_checked_before_any_body_byte(api):
    client, caller, _usage, app = api
    reader = make_caller(scopes=("files.read",))
    app.state.files_deps.resolve_caller.__self__.add(reader)
    body = multipart_body([("purpose", "user_data"), ("file", "a.txt", b"hi", "text/plain")])
    denied = client.post("/v1/files", headers={**headers_for(reader), "Content-Type": MULTIPART}, content=body)
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "insufficient_scope"
    anonymous = client.post("/v1/files", headers={"Content-Type": MULTIPART}, content=body)
    assert anonymous.status_code == 401
    assert client.get("/v1/files", headers=headers_for(reader)).status_code == 200
    assert _tree(storage.root()) == []


def test_every_request_writes_exactly_one_usage_record_with_its_route(api):
    client, caller, usage, _app = api
    headers = headers_for(caller)
    created = _create(client, caller).json()
    client.get("/v1/files", headers=headers)
    client.get(f"/v1/files/{created['id']}", headers=headers)
    client.get(f"/v1/files/{created['id']}/content", headers=headers)
    client.delete(f"/v1/files/{created['id']}", headers=headers)
    assert [u.route for u in usage] == ["v1_files_create", "v1_files_list", "v1_files_get", "v1_files_content", "v1_files_delete"]
    assert usage[0].generation_id == created["id"] and usage[1].generation_id.startswith("lst_")
    assert all(u.status == "ok" for u in usage)


@pytest.mark.parametrize(
    "case",
    ["file_over_cap", "body_over_cap", "malformed", "no_closing_boundary", "client_gone"],
)
def test_the_disk_multipart_reader_removes_its_temporary_file_on_every_failure(tmp_path, case):
    from starlette.requests import ClientDisconnect

    from app.publicapi.files import multipart_disk, wire

    target = str(tmp_path / "incoming.tmp")
    body = multipart_body([("purpose", "user_data"), ("file", "a.bin", b"x" * 50_000, "application/octet-stream")])
    caps = {"max_body_bytes": 1_000_000, "max_file_bytes": 1_000_000}
    if case == "file_over_cap":
        caps["max_file_bytes"] = 10_000
    elif case == "body_over_cap":
        caps["max_body_bytes"] = 20_000
    elif case == "malformed":
        body = body[:200] + b"\r\n--wrongboundary\r\ngarbage" + body[200:]
    elif case == "no_closing_boundary":
        body = body[: body.rfind(b"--testboundary7d1a--")]

    async def chunks():
        for index, start in enumerate(range(0, len(body), 4096)):
            if case == "client_gone" and index == 5:
                raise ClientDisconnect()
            yield body[start:start + 4096]

    async def read():
        return await multipart_disk.read_form_to_disk(chunks(), MULTIPART, tmp_path=target, file_field="file", **caps)

    expected = multipart_disk.ClientGone if case == "client_gone" else wire.FilesApiError
    with pytest.raises(expected):
        asyncio.run(read())
    assert not os.path.exists(target)


def test_the_disk_multipart_reader_hashes_the_file_it_writes(tmp_path):
    from app.publicapi.files import multipart_disk

    data = os.urandom(123_457)
    body = multipart_body([("file", "a.bin", data, "application/octet-stream"), ("purpose", "user_data")])
    target = str(tmp_path / "incoming.tmp")

    async def chunks():
        for start in range(0, len(body), 1000):
            yield body[start:start + 1000]

    form = asyncio.run(multipart_disk.read_form_to_disk(
        chunks(), MULTIPART, tmp_path=target, file_field="file", max_body_bytes=10 ** 6, max_file_bytes=10 ** 6, want_md5=True))
    assert form.fields == {"purpose": "user_data"}
    assert (form.file.bytes, form.file.sha256, form.file.md5) == (len(data), hashlib.sha256(data).hexdigest(), hashlib.md5(data).hexdigest())
    assert open(target, "rb").read() == data


def test_the_transport_body_caps_cover_exactly_the_three_streaming_routes():
    from app.publicapi.files import routes

    assert routes.body_cap_for("POST", "/v1/files") == 68_157_440
    assert routes.body_cap_for("POST", "/v1/uploads/upload_abc/parts") == 68_157_440
    assert routes.body_cap_for("PUT", "/v1/uploads/upload_abc/parts/3") == 67_108_864
    for method, path in (("POST", "/v1/uploads"), ("POST", "/v1/uploads/u/complete"), ("GET", "/v1/files"),
                         ("PUT", "/v1/files"), ("POST", "/v1/files/x"), ("POST", "/v1/responses")):
        assert routes.body_cap_for(method, path) is None


def test_the_routes_render_through_the_public_route_class_unchanged():
    """Integration only registers: `FilesApiError` is an `ApiError`, so the
    real `/v1` `PublicRoute` renders the new codes with its envelope,
    `X-Request-Id` and the `x-should-retry` / `Retry-After` headers."""
    from fastapi import APIRouter, FastAPI

    from app.publicapi import router as public_router
    from app.publicapi.files import routes

    caller = make_caller()
    auth = StubAuth(caller)
    deps = routes.FilesDependencies(resolve_caller=auth.resolve, authorize=auth.authorize)
    router = APIRouter(prefix="/v1", route_class=public_router.PublicRoute)
    assert routes.register(router, deps) == 11
    assert routes.register(router, deps) == 0, "idempotent"
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    missing = client.get("/v1/files/file-" + "0" * 24, headers={**headers_for(caller), "Origin": "https://dev.example"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "file_not_found"
    assert missing.headers["x-request-id"].startswith("req_") and missing.headers["x-should-retry"] == "false"
    assert missing.headers["access-control-allow-origin"] == "https://dev.example"
    upload = client.post("/v1/uploads", headers=headers_for(caller),
                         json={"bytes": 1, "filename": "a", "mime_type": "x", "purpose": "user_data"}).json()
    busy = client.post(f"/v1/uploads/{upload['id']}/cancel", headers=headers_for(caller))
    assert busy.status_code == 200 and busy.json()["status"] == "cancelled"
    conflict = client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(caller), content=b"x")
    assert conflict.status_code == 409 and conflict.headers["x-should-retry"] == "false"


# --------------------------------------------------- review fixes 2026-09-13 --


def _held_purge(inner):
    """A purge whose FIRST call (the DELETE's) waits for `release`; later calls
    run at once. Counts calls, so a second purge of one blob is visible."""
    import threading

    release = threading.Event()
    calls = {"n": 0}

    async def purge(blob):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.to_thread(release.wait, 30)
        return await inner(blob)

    return purge, release, calls


def _drain_background(client, app):
    deps = app.state.files_deps
    client.portal.call(lambda: asyncio.gather(*list(deps.background_tasks), return_exceptions=True))


def test_a_reupload_during_a_slow_purge_gets_a_retryable_503_and_never_loses_the_new_bytes(monkeypatch):
    """Review HIGH (2026-09-13): the re-upload used to run a SECOND purge inline;
    whichever purge finished last rmtree'd `<project>/<sha256>/`, which by then
    held the NEW file's bytes (GET file 200, GET content 404)."""
    from app.publicapi.files import routes

    monkeypatch.setenv("PUBLIC_API_FILES_PURGE_WAIT_S", "0.3")
    caller = make_caller()
    purge, release, calls = _held_purge(routes.purge_blob_local)
    app = build_app(StubAuth(caller), purge_blob=purge)
    data = b"%PDF-1.4 " + os.urandom(4096)
    with TestClient(app) as client:
        first = _create(client, caller, data=data).json()
        assert client.delete(f"/v1/files/{first['id']}", headers=headers_for(caller)).json()["deleted"] is True
        busy = _create(client, caller, data=data)
        assert busy.status_code == 503 and busy.json()["error"]["code"] == "storage_unavailable"
        assert busy.headers["x-should-retry"] == "true", "a purge in flight is transient, unlike a full disk"
        assert int(busy.headers["retry-after"]) >= 1
        assert calls["n"] == 1, "the re-upload waited on the DELETE's purge instead of starting its own"
        assert os.listdir(os.path.join(storage.root(), storage.SINGLE_DIR)) == [], "the refused body is discarded"
        release.set()
        _drain_background(client, app)
        again = _create(client, caller, data=data)
        assert again.status_code == 200, again.text
        second = again.json()
        _drain_background(client, app)
        assert calls["n"] == 1
        assert client.get(f"/v1/files/{second['id']}/content", headers=headers_for(caller)).content == data


def test_a_reupload_that_the_purge_finishes_under_is_accepted_on_the_first_try(monkeypatch):
    import threading

    from app.publicapi.files import routes

    monkeypatch.setenv("PUBLIC_API_FILES_PURGE_WAIT_S", "10")
    caller = make_caller()
    purge, release, calls = _held_purge(routes.purge_blob_local)
    app = build_app(StubAuth(caller), purge_blob=purge)
    data = b"%PDF-1.4 " + os.urandom(2048)
    with TestClient(app) as client:
        first = _create(client, caller, data=data).json()
        client.delete(f"/v1/files/{first['id']}", headers=headers_for(caller))
        threading.Timer(0.3, release.set).start()
        started = time.monotonic()
        again = _create(client, caller, data=data)
        waited = time.monotonic() - started
        assert again.status_code == 200, again.text
        _drain_background(client, app)
        assert calls["n"] == 1 and 0.2 < waited < 5
        assert client.get(f"/v1/files/{again.json()['id']}/content", headers=headers_for(caller)).content == data


def test_a_stale_second_purge_of_a_retired_blob_touches_nothing_of_the_newer_blob_with_the_same_bytes():
    """The purge is keyed by blob id under the row's lock, not by the
    content-keyed path: a purge that runs late (another process, retention's
    reconciliation, a DELETE's task) finds no row and leaves the path alone."""
    from app.apifiles import schema
    from app.publicapi.files import routes

    async def keep(blob):
        return None

    caller = make_caller()
    app = build_app(StubAuth(caller), purge_blob=keep)
    data = b"%PDF-1.4 " + os.urandom(1024)
    with TestClient(app) as client:
        first = _create(client, caller, data=data).json()
        client.delete(f"/v1/files/{first['id']}", headers=headers_for(caller))
        with db.connection() as con:
            old = dict(con.execute(
                "SELECT * FROM api_file_blobs WHERE project_id = %s AND sha256 = %s", (caller.project_id, first["sha256"])
            ).fetchone())
        assert old["status"] == "deleting"
        assert routes.purge_blob_bytes(old["id"]) is True
        assert not os.path.exists(storage.blob_dir(caller.project_id, old["sha256"]))
        assert os.listdir(storage.trash_root()) == [], "the trash entry is removed after the commit"
        again = _create(client, caller, data=data).json()
        assert again["id"] != first["id"]
        # A stale purge of the OLD blob, by id and by row, after the new blob exists.
        assert routes.purge_blob_bytes(old["id"]) is False
        client.portal.call(routes.purge_blob_local, old)
        assert schema.get_api_file_blob(old["id"]) is None
        assert client.get(f"/v1/files/{again['id']}/content", headers=headers_for(caller)).content == data


def test_a_reupload_blocked_on_the_purge_transaction_creates_a_fresh_blob_instead_of_a_500():
    """`_lock_or_create_blob`'s INSERT sees the doomed row (DO NOTHING), its
    SELECT … FOR SHARE waits on the purge's lock and then finds the row gone.
    That was `RuntimeError("blob row vanished")` — a 500 — before the retry."""
    import threading

    from app.apifiles import queue, schema

    async def keep(blob):
        return None

    caller = make_caller()
    app = build_app(StubAuth(caller), purge_blob=keep)
    data = b"%PDF-1.4 " + os.urandom(1024)
    with TestClient(app) as client:
        first = _create(client, caller, data=data).json()
        client.delete(f"/v1/files/{first['id']}", headers=headers_for(caller))
    with db.connection() as con:
        old_id = con.execute(
            "SELECT id FROM api_file_blobs WHERE project_id = %s AND sha256 = %s", (caller.project_id, first["sha256"])
        ).fetchone()["id"]

    locked, release = threading.Event(), threading.Event()

    def slow_move(row):
        locked.set()
        release.wait(20)
        return storage.move_blob_dir_aside(row)

    purge_result = {}
    purger = threading.Thread(target=lambda: purge_result.update(r=schema.retire_deleting_blob(old_id, move_aside=slow_move)))
    purger.start()
    assert locked.wait(10)
    storage.ensure_dirs()
    tmp = storage.single_tmp()
    with open(tmp, "wb") as fh:
        fh.write(data)
    ingest_result = {}

    def ingest():
        try:
            ingest_result["r"] = queue.ingest_single(
                project_id=caller.project_id, workspace_id=caller.workspace_id, key_id=None, tmp_path=tmp,
                sha256=first["sha256"], bytes=len(data), filename="doc.pdf", purpose="user_data",
                expires_after_seconds=None,
            )
        except BaseException as exc:  # noqa: BLE001 - asserted below
            ingest_result["e"] = exc

    uploader = threading.Thread(target=ingest)
    uploader.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db.connection() as con:
            waiting = con.execute(
                "SELECT count(*) AS n FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND query ILIKE '%%api_file_blobs%%FOR SHARE%%'"
            ).fetchone()["n"]
        if waiting:
            break
        time.sleep(0.05)
    assert waiting, "the re-upload is blocked on the purge's row lock"
    release.set()
    purger.join(20)
    uploader.join(20)
    assert "e" not in ingest_result, repr(ingest_result.get("e"))
    fresh = ingest_result["r"]
    assert fresh.created and fresh.blob["id"] != old_id
    storage.remove_trash(purge_result["r"][1])
    with open(storage.original_path(caller.project_id, first["sha256"]), "rb") as fh:
        assert fh.read() == data


def test_deeply_nested_json_is_text_on_create_and_a_400_as_a_request_body(api):
    """Review MEDIUM (2026-09-13): RecursionError is neither ValueError nor
    OSError; it escaped the sniff (500 on POST /v1/files) and `_read_json`."""
    client, caller, _usage, _app = api
    created = _create(client, caller, data=b"[" * 60000, filename="notes.txt")
    assert created.status_code == 200, created.text
    assert created.json()["mime_type"] == "text/plain"
    nested = client.post("/v1/uploads", headers={**headers_for(caller), "Content-Type": "application/json"}, content=b"[" * 100000)
    assert nested.status_code == 400 and nested.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("spelling", ["²", "٣", "１"])
def test_a_number_written_with_non_ascii_digits_is_a_400_not_a_500(spelling):
    caller = make_caller()
    app = build_app(StubAuth(caller))
    with TestClient(app, raise_server_exceptions=False) as client:
        upload = client.post("/v1/uploads", headers=headers_for(caller),
                             json={"bytes": 3, "filename": "a.bin", "mime_type": "application/octet-stream", "purpose": "user_data"}).json()
        from urllib.parse import quote

        put = client.put(f"/v1/uploads/{upload['id']}/parts/{quote(spelling)}",
                         headers={**headers_for(caller), "Content-Type": "application/octet-stream"}, content=b"abc")
        body = multipart_body([("part_number", spelling), ("data", "blob", b"abc", "application/octet-stream")])
        post = client.post(f"/v1/uploads/{upload['id']}/parts", headers={**headers_for(caller), "Content-Type": MULTIPART}, content=body)
        listed = client.get("/v1/files", headers=headers_for(caller), params={"limit": spelling})
    for response in (put, post, listed):
        assert response.status_code == 400, (response.status_code, response.text)
        assert response.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize("field", ["filename", "mime_type"])
def test_a_lone_surrogate_in_upload_metadata_is_a_400_not_a_500(field):
    caller = make_caller()
    app = build_app(StubAuth(caller))
    values = {"filename": '"a.bin"', "mime_type": '"text/plain"'}
    values[field] = '"a\\ud800b"'
    raw = ('{"bytes": 3, "purpose": "user_data", "filename": %s, "mime_type": %s}' % (values["filename"], values["mime_type"])).encode()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/v1/uploads", headers={**headers_for(caller), "Content-Type": "application/json"}, content=raw)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["param"] == field


def _count_closes(monkeypatch, content):
    closes: list = []
    real_close = os.close

    def spy(fd):
        closes.append(fd)
        return real_close(fd)

    monkeypatch.setattr(content.os, "close", spy)
    return closes


def test_an_unsatisfiable_range_closes_its_descriptor_exactly_once(tmp_path, monkeypatch):
    """Review MEDIUM (2026-09-13): the 416 path closed the fd, then the except
    closed the same number again — by then possibly another request's."""
    from app.publicapi.files import content, wire

    path = tmp_path / "f"
    path.write_bytes(b"abc")
    closes = _count_closes(monkeypatch, content)
    with pytest.raises(wire.FilesApiError) as refused:
        content.file_response(str(path), filename="f", etag="e", range_header="bytes=10-20")
    assert refused.value.status == 416
    assert len(closes) == 1
    response, _sent, _span = content.file_response(str(path), filename="f", etag="e", if_none_match='"e"')
    assert response.status_code == 304 and len(closes) == 2
    response, sent, _span = content.file_response(str(path), filename="f", etag="e")
    assert sent == 3 and len(closes) == 2, "a 200 hands its descriptor to the stream"
    response._close()
    assert len(closes) == 3


def test_with_the_retention_purge_plugged_in_a_reupload_waits_for_it_and_keeps_the_new_bytes(monkeypatch):
    """The same race with team B's `retention.purge_blob` as the dependency:
    its job cancellation is the seconds-long window the review measured."""
    import threading

    from app.apifiles import jobs, retention

    monkeypatch.setenv("PUBLIC_API_FILES_PURGE_WAIT_S", "0.3")
    release, cancels = threading.Event(), {"n": 0}

    async def slow_cancel(blob_id):
        cancels["n"] += 1
        await asyncio.to_thread(release.wait, 30)

    monkeypatch.setattr(jobs, "cancel_blob", slow_cancel)
    caller = make_caller()
    app = build_app(StubAuth(caller), purge_blob=retention.purge_blob)
    data = b"%PDF-1.4 " + os.urandom(4096)
    with TestClient(app) as client:
        first = _create(client, caller, data=data).json()
        client.delete(f"/v1/files/{first['id']}", headers=headers_for(caller))
        busy = _create(client, caller, data=data)
        assert busy.status_code == 503 and busy.headers["x-should-retry"] == "true"
        assert cancels["n"] == 1, "no second purge was started"
        release.set()
        _drain_background(client, app)
        again = _create(client, caller, data=data)
        assert again.status_code == 200, again.text
        _drain_background(client, app)
        assert client.get(f"/v1/files/{again.json()['id']}/content", headers=headers_for(caller)).content == data
