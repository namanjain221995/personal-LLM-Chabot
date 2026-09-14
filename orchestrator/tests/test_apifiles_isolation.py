"""Cross-project isolation (design §7.2, acceptance A-4): the project is the
tenant, every accessor carries it, and a foreign id is indistinguishable from
one that never existed."""
from __future__ import annotations

import inspect

import pytest
from starlette.testclient import TestClient

from app import db
from app.apifiles import schema, storage
from tests.apifiles_test_support import (  # noqa: F401 - fixture by name
    MULTIPART,
    StubAuth,
    build_app,
    headers_for,
    isolated_app_db,
    make_caller,
    make_workspace,
    multipart_body,
)

SHA = "e" * 64


@pytest.fixture()
def tenants():
    workspace = make_workspace()
    owner = make_caller(workspace)
    neighbour = make_caller(workspace)       # same workspace, other project
    stranger = make_caller()                 # other workspace
    auth = StubAuth(owner, neighbour, stranger)
    app = build_app(auth)
    with TestClient(app) as client:
        yield client, owner, neighbour, stranger


def _upload_file(client, caller, data=b"%PDF-1.4 secret"):
    body = multipart_body([("purpose", "user_data"), ("file", "a.pdf", data, "application/pdf")])
    response = client.post("/v1/files", headers={**headers_for(caller), "Content-Type": MULTIPART}, content=body)
    assert response.status_code == 200, response.text
    return response.json()


def _owner_state(client, owner):
    file_body = _upload_file(client, owner)
    upload = client.post("/v1/uploads", headers=headers_for(owner),
                         json={"bytes": 10, "filename": "u.bin", "mime_type": "x", "purpose": "user_data"}).json()
    assert client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(owner), content=b"hello").status_code == 200
    return file_body["id"], upload["id"]


def _requests(file_id, upload_id):
    return [
        ("GET", f"/v1/files/{file_id}", {}),
        ("GET", f"/v1/files/{file_id}/content", {}),
        ("DELETE", f"/v1/files/{file_id}", {}),
        ("GET", f"/v1/uploads/{upload_id}", {}),
        ("POST", f"/v1/uploads/{upload_id}/parts", {"files": {"data": ("u", b"world", "application/octet-stream")}}),
        ("PUT", f"/v1/uploads/{upload_id}/parts/1", {"content": b"world"}),
        ("POST", f"/v1/uploads/{upload_id}/complete", {"json": {}}),
        ("POST", f"/v1/uploads/{upload_id}/cancel", {}),
    ]


def _error_without_request_id(response):
    body = response.json()
    body["error"].pop("request_id")
    return response.status_code, body


def test_every_route_answers_a_foreign_id_exactly_as_a_random_one(tenants):
    client, owner, neighbour, stranger = tenants
    file_id, upload_id = _owner_state(client, owner)
    random_file, random_upload = "file-" + "7" * 24, "upload_" + "7" * 24
    for intruder in (neighbour, stranger):
        foreign = _requests(file_id, upload_id)
        absent = _requests(random_file, random_upload)
        for (method, path, kwargs), (_m, random_path, random_kwargs) in zip(foreign, absent):
            seen = client.request(method, path, headers=headers_for(intruder), **kwargs)
            baseline = client.request(method, random_path, headers=headers_for(intruder), **random_kwargs)
            assert seen.status_code == 404, (method, path, seen.text)
            assert seen.json()["error"]["code"] in ("file_not_found", "upload_not_found")
            assert _error_without_request_id(seen) == _error_without_request_id(baseline), (method, path)
    # ...and nothing the intruders did touched the owner's objects.
    assert client.get(f"/v1/files/{file_id}/content", headers=headers_for(owner)).content == b"%PDF-1.4 secret"
    state = client.get(f"/v1/uploads/{upload_id}", headers=headers_for(owner)).json()
    assert state["status"] == "pending" and [p["part_number"] for p in state["parts"]] == [0]


def test_a_malformed_id_gets_the_same_404_as_a_well_formed_absent_one(tenants):
    client, owner, _neighbour, _stranger = tenants
    # (A path with `/` in it never reaches these handlers: the router answers it.)
    for bad, good in (("file-" + "1" * 23 + "g", "file-" + "1" * 24), ("file-XYZ", "file-" + "1" * 24), ("FILE-" + "1" * 24, "file-" + "1" * 24)):
        a = client.get(f"/v1/files/{bad}", headers=headers_for(owner))
        b = client.get(f"/v1/files/{good}", headers=headers_for(owner))
        assert a.status_code in (404,) and _error_without_request_id(a) == _error_without_request_id(b)


def test_listing_shows_only_the_callers_project(tenants):
    client, owner, neighbour, _stranger = tenants
    file_id, _upload_id = _owner_state(client, owner)
    assert file_id in [f["id"] for f in client.get("/v1/files", headers=headers_for(owner)).json()["data"]]
    assert client.get("/v1/files", headers=headers_for(neighbour)).json()["data"] == []


def test_the_same_bytes_in_another_project_start_from_scratch(tenants):
    client, owner, neighbour, _stranger = tenants
    mine = _upload_file(client, owner, b"identical bytes")
    theirs = _upload_file(client, neighbour, b"identical bytes")
    assert mine["id"] != theirs["id"] and mine["sha256"] == theirs["sha256"]
    with db.connection() as con:
        blobs = con.execute(
            "SELECT project_id, id, status FROM api_file_blobs WHERE sha256 = %s AND project_id = ANY(%s)",
            (mine["sha256"], [owner.project_id, neighbour.project_id]),
        ).fetchall()
    assert len({b["id"] for b in blobs}) == 2, "a blob is never shared across projects"
    with db.connection() as con:
        owners = con.execute(
            "SELECT f.id, f.project_id AS file_project, b.project_id AS blob_project FROM api_files f "
            "  JOIN api_file_blobs b ON b.id = f.blob_id WHERE f.id = ANY(%s)",
            ([mine["id"], theirs["id"]],),
        ).fetchall()
    assert len(owners) == 2 and all(o["file_project"] == o["blob_project"] for o in owners), \
        "each file references its own project's blob"
    assert all(b["status"] == "queued" for b in blobs), "no instant 'processed' from another tenant's work"
    assert theirs["processing"]["state"] == "queued"
    assert storage.api_video_hash(owner.project_id, mine["sha256"]) != storage.api_video_hash(neighbour.project_id, mine["sha256"])
    assert storage.original_path(owner.project_id, mine["sha256"]) != storage.original_path(neighbour.project_id, mine["sha256"])


def test_a_key_without_the_files_scopes_is_refused(tenants):
    client, owner, _neighbour, _stranger = tenants
    file_id, _upload_id = _owner_state(client, owner)
    blind = make_caller(scopes=("responses.write",))
    blind.project_id, blind.workspace_id = owner.project_id, owner.workspace_id
    client.app.state.files_deps.resolve_caller.__self__.add(blind)
    response = client.get(f"/v1/files/{file_id}", headers=headers_for(blind))
    assert response.status_code == 403 and response.json()["error"]["code"] == "insufficient_scope"


def _foreign_calls(foreign_project, file_id, upload_id):
    """Each §9.2 accessor that takes a project, called with the WRONG project."""
    return {
        "get_api_upload": lambda: schema.get_api_upload(foreign_project, upload_id),
        "list_api_upload_parts": lambda: schema.list_api_upload_parts(foreign_project, upload_id),
        "next_api_upload_part_number": lambda: schema.next_api_upload_part_number(foreign_project, upload_id),
        "upsert_api_upload_part": lambda: schema.upsert_api_upload_part(
            foreign_project, upload_id, part_number=5, bytes=1, sha256=SHA, mode="numbered", part_max_bytes=10,
            max_parts=10, upload_max_bytes=100, idle_ttl_s=60, max_ttl_s=60,
            before_commit=lambda n: pytest.fail("must not commit a foreign part"),
        ),
        "try_begin_api_upload_finalize": lambda: schema.try_begin_api_upload_finalize(foreign_project, upload_id),
        "return_api_upload_to_pending": lambda: schema.return_api_upload_to_pending(foreign_project, upload_id),
        "complete_api_upload": lambda: schema.complete_api_upload(
            foreign_project, upload_id, part_numbers=[0], expected_md5=None, expected_sha256=None,
            render_result=lambda u, f: pytest.fail("must not complete a foreign upload"),
        ),
        "finish_api_upload": lambda: schema.finish_api_upload(foreign_project, upload_id, status="failed"),
        "cancel_api_upload": lambda: schema.cancel_api_upload(foreign_project, upload_id),
        "create_api_file": lambda: schema.create_api_file(
            foreign_project, "ws", None, _blob_of(file_id), "x", "user_data", 1, "file", None
        ),
        "attach_blob_to_assembling_file": lambda: schema.attach_blob_to_assembling_file(
            foreign_project, file_id, upload_id, lease_owner="x", sha256=SHA, bytes=1, kind="text",
            mime_type="text/plain", lane="cpu", place_bytes=lambda r, c: pytest.fail("must not place bytes"),
        ),
        "fail_assembling_file": lambda: schema.fail_assembling_file(foreign_project, file_id, upload_id, "internal_error"),
        "get_api_file": lambda: schema.get_api_file(foreign_project, file_id),
        "get_api_files": lambda: schema.get_api_files(foreign_project, [file_id]),
        "list_api_files": lambda: schema.list_api_files(foreign_project, after=None, limit=10, order="desc", purpose=None)[0],
        "delete_api_file": lambda: schema.delete_api_file(foreign_project, file_id),
        "project_file_storage": lambda: {k: v for k, v in schema.project_file_storage(foreign_project).items() if v},
    }


def _blob_of(file_id):
    with db.connection() as con:
        return con.execute("SELECT blob_id FROM api_files WHERE id = %s", (file_id,)).fetchone()["blob_id"]


def test_every_project_scoped_accessor_returns_nothing_for_a_foreign_project(tenants):
    client, owner, neighbour, _stranger = tenants
    file_id, upload_id = _owner_state(client, owner)
    calls = _foreign_calls(neighbour.project_id, file_id, upload_id)
    assert set(calls) == set(schema.PROJECT_SCOPED_ACCESSORS), "every listed accessor is exercised, and only those"
    for name in schema.PROJECT_SCOPED_ACCESSORS:
        parameters = list(inspect.signature(getattr(schema, name)).parameters)
        assert parameters[0] == "project_id", f"{name} must take project_id first"
        result = calls[name]()
        empty = result in (None, [], {}) or (isinstance(result, tuple) and result[0] == "gone") or getattr(result, "state", None) == "gone"
        assert empty, (name, result)
    assert client.get(f"/v1/files/{file_id}/content", headers=headers_for(owner)).content == b"%PDF-1.4 secret"
    state = client.get(f"/v1/uploads/{upload_id}", headers=headers_for(owner)).json()
    assert state["status"] == "pending" and state["bytes_received"] == 5


class _Derived:
    def list_names(self, blob_row):
        return [{"name": "text.txt", "bytes": 5, "content_type": "text/plain"}]

    def open_name(self, blob_row, name):
        if name != "text.txt":
            raise KeyError(name)
        return storage.original_path(blob_row["project_id"], blob_row["sha256"]), "text/plain; charset=utf-8", 5


def test_the_derived_and_events_routes_refuse_a_foreign_id_like_a_random_one():
    """Routes 6–8 are registered only when team B's providers are passed; the
    lookup in front of them is the same project-scoped one."""
    from fastapi.responses import PlainTextResponse

    owner, intruder = make_caller(), make_caller()

    async def events(request, caller, row):
        return PlainTextResponse("event: file.processed\n\n")

    app = build_app(StubAuth(owner, intruder), derived=_Derived(), events=events)
    client = TestClient(app)
    file_id = _upload_file(client, owner, b"hello")["id"]
    with db.connection() as con:
        con.execute("UPDATE api_file_blobs SET status = 'processed' WHERE project_id = %s", (owner.project_id,))
    assert client.get(f"/v1/files/{file_id}/derived", headers=headers_for(owner)).json()["data"][0]["name"] == "text.txt"
    assert client.get(f"/v1/files/{file_id}/derived/text.txt", headers=headers_for(owner)).content == b"hello"
    assert client.get(f"/v1/files/{file_id}/events", headers=headers_for(owner)).status_code == 200
    not_listed = client.get(f"/v1/files/{file_id}/derived/secret.txt", headers=headers_for(owner))
    assert not_listed.status_code == 404 and not_listed.json()["error"]["param"] == "name"
    random_id = "file-" + "5" * 24
    for suffix in ("/derived", "/derived/text.txt", "/events"):
        foreign = client.get(f"/v1/files/{file_id}{suffix}", headers=headers_for(intruder))
        absent = client.get(f"/v1/files/{random_id}{suffix}", headers=headers_for(intruder))
        assert foreign.status_code == 404 and _error_without_request_id(foreign) == _error_without_request_id(absent)
