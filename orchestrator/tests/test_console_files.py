"""The developer console's Files routes — `/admin/api/developers/projects/{id}/…`
(files-hookup, 2026-09-13; Files design §14.1).

Real sessions, real capability gates, real tables, and the SAME upload handlers
`/v1/uploads` runs. What is pinned here is what the Files tab in the browser
depends on and what would be a security incident if it broke: a member sees
404, another workspace's project is 404, a write must be JSON, a busy
`complete` keeps its retry verdict, a delete is audited, and no route serves a
byte.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict

import pytest

from app import db
from app.apifiles import schema
from tests.test_api_platform_console import (  # noqa: F401 - fixtures
    _audit_rows,
    _make_project,
    admin,
    console_router_mounted,
    member,
    other_workspace,
    pepper_store,
)

BASE = "/admin/api/developers/projects"


@pytest.fixture(autouse=True)
def files_root(tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "api-files"))
    monkeypatch.setenv("PUBLIC_API_FILES_MIN_FREE_GIB", "0")
    yield


def _upload(client, project_id: str, data: bytes, filename: str = "notes.txt") -> Dict[str, Any]:
    created = client.post(
        f"{BASE}/{project_id}/uploads",
        json={"bytes": len(data), "filename": filename, "mime_type": "text/plain", "purpose": "user_data"},
    )
    assert created.status_code == 200, created.text
    upload = created.json()
    half = len(data) // 2
    for number, chunk in enumerate((data[:half], data[half:])):
        part = client.put(
            f"{BASE}/{project_id}/uploads/{upload['id']}/parts/{number}",
            content=chunk,
            headers={"content-type": "application/octet-stream", "x-part-sha256": hashlib.sha256(chunk).hexdigest()},
        )
        assert part.status_code == 200, part.text
    done = client.post(f"{BASE}/{project_id}/uploads/{upload['id']}/complete", json={})
    assert done.status_code == 200, done.text
    return done.json()


def test_an_admin_uploads_lists_reads_and_deletes_a_file_with_the_v1_objects_and_an_audit_row(admin):
    project = _make_project(admin, "Files console")
    data = b"The console upload goes through the same handlers.\n" * 400
    upload = _upload(admin, project["id"], data)
    assert upload["status"] == "completed" and upload["file"]["id"].startswith("file-")
    file_id = upload["file"]["id"]

    resumed = admin.get(f"{BASE}/{project['id']}/uploads/{upload['id']}")
    assert resumed.status_code == 200 and resumed.json()["file"]["id"] == file_id

    listed = admin.get(f"{BASE}/{project['id']}/files").json()
    assert listed["object"] == "list" and [f["id"] for f in listed["data"]] == [file_id]
    assert "derived_bytes" in listed["data"][0]
    got = admin.get(f"{BASE}/{project['id']}/files/{file_id}").json()
    assert got["id"] == file_id and got["bytes"] == len(data) and got["processing"] is not None

    stats = admin.get(f"{BASE}/{project['id']}/storage").json()
    assert stats["files"] == 1

    deleted = admin.delete(f"{BASE}/{project['id']}/files/{file_id}")
    assert deleted.status_code == 200 and deleted.json() == {"id": file_id, "object": "file", "deleted": True}
    assert admin.get(f"{BASE}/{project['id']}/files/{file_id}").status_code == 404
    rows = _audit_rows("api_file_deleted")
    assert rows and rows[-1]["resource_id"] == file_id


def test_a_member_is_refused_with_404_on_every_files_route(member):
    project = "proj_" + "0" * 24
    for method, path in (
        ("GET", f"{BASE}/{project}/files"),
        ("GET", f"{BASE}/{project}/files/file-{'0' * 24}"),
        ("DELETE", f"{BASE}/{project}/files/file-{'0' * 24}"),
        ("GET", f"{BASE}/{project}/files/file-{'0' * 24}/events"),
        ("GET", f"{BASE}/{project}/files/file-{'0' * 24}/derived"),
        ("GET", f"{BASE}/{project}/storage"),
        ("POST", f"{BASE}/{project}/uploads"),
        ("GET", f"{BASE}/{project}/uploads/upload_{'0' * 24}"),
        ("PUT", f"{BASE}/{project}/uploads/upload_{'0' * 24}/parts/0"),
        ("POST", f"{BASE}/{project}/uploads/upload_{'0' * 24}/complete"),
        ("POST", f"{BASE}/{project}/uploads/upload_{'0' * 24}/cancel"),
    ):
        assert member.request(method, path, json={}).status_code == 404, (method, path)


def test_another_workspaces_project_and_its_files_read_as_missing(admin, other_workspace):
    project = _make_project(admin, "Ours")
    upload = _upload(admin, project["id"], b"x" * 2048)
    file_id = upload["file"]["id"]
    assert other_workspace.get(f"{BASE}/{project['id']}/files").status_code == 404
    assert other_workspace.get(f"{BASE}/{project['id']}/files/{file_id}").status_code == 404
    assert other_workspace.delete(f"{BASE}/{project['id']}/files/{file_id}").status_code == 404
    assert admin.get(f"{BASE}/{project['id']}/files/{file_id}").status_code == 200


def test_creating_or_completing_an_upload_must_send_json(admin):
    project = _make_project(admin, "Json only")
    form = admin.post(f"{BASE}/{project['id']}/uploads", data={"bytes": "4", "filename": "a.txt"})
    assert form.status_code == 400
    assert form.json()["error"]["param"] == "Content-Type"
    upload = admin.post(
        f"{BASE}/{project['id']}/uploads",
        json={"bytes": 4, "filename": "a.txt", "mime_type": "text/plain", "purpose": "user_data"},
    ).json()
    empty = admin.post(f"{BASE}/{project['id']}/uploads/{upload['id']}/complete")
    assert empty.status_code == 400 and empty.json()["error"]["param"] == "Content-Type"


def test_a_busy_complete_reaches_the_browser_as_409_with_its_retry_verdict(admin, monkeypatch):
    project = _make_project(admin, "Busy")
    upload = admin.post(
        f"{BASE}/{project['id']}/uploads",
        json={"bytes": 4, "filename": "a.txt", "mime_type": "text/plain", "purpose": "user_data"},
    ).json()
    row = schema.get_api_upload(project["id"], upload["id"])
    monkeypatch.setattr(schema, "try_begin_api_upload_finalize", lambda *a, **k: ("busy", row))
    monkeypatch.setenv("PUBLIC_API_UPLOAD_COMPLETE_BUSY_WAIT_S", "0")
    busy = admin.post(f"{BASE}/{project['id']}/uploads/{upload['id']}/complete", json={})
    assert busy.status_code == 409
    assert busy.headers["x-should-retry"] == "true" and busy.headers["retry-after"] == "2"
    assert busy.json()["error"]["code"] == "upload_state_conflict"


def test_a_console_part_over_one_mib_reaches_the_handler_and_one_over_eight_is_refused(admin):
    project = _make_project(admin, "Parts")
    size = 3 * 1024 * 1024
    upload = admin.post(
        f"{BASE}/{project['id']}/uploads",
        json={"bytes": size, "filename": "big.bin", "mime_type": "application/octet-stream", "purpose": "user_data"},
    ).json()
    ok = admin.put(f"{BASE}/{project['id']}/uploads/{upload['id']}/parts/0", content=b"\x01" * size,
                   headers={"content-type": "application/octet-stream"})
    assert ok.status_code == 200, ok.text
    over = admin.put(f"{BASE}/{project['id']}/uploads/{upload['id']}/parts/1", content=b"\x01" * (8 * 1024 * 1024 + 1),
                     headers={"content-type": "application/octet-stream"})
    assert over.status_code == 413


def test_the_status_filter_pages_with_a_cursor_the_next_call_can_use(admin):
    project = _make_project(admin, "Paging")
    ids = [_upload(admin, project["id"], bytes([n]) * 1024, filename=f"f{n}.txt")["file"]["id"] for n in range(3)]
    first = admin.get(f"{BASE}/{project['id']}/files", params={"limit": 2}).json()
    assert len(first["data"]) == 2 and first["has_more"] is True
    second = admin.get(f"{BASE}/{project['id']}/files", params={"limit": 2, "after": first["last_id"]}).json()
    assert len(second["data"]) == 1 and second["has_more"] is False
    assert {f["id"] for f in first["data"] + second["data"]} == set(ids)
    none_processed = admin.get(f"{BASE}/{project['id']}/files", params={"status": "processed"}).json()
    assert none_processed["data"] == [] and none_processed["has_more"] is False
    assert admin.get(f"{BASE}/{project['id']}/files", params={"status": "exploded"}).status_code == 400


def test_no_console_route_serves_the_bytes_of_a_file_or_a_derived_output(admin):
    from app.apiplatform import console_api

    paths = {route.path for route in console_api.router.routes}
    assert not any(path.endswith("/content") for path in paths)
    assert not any("/derived/{" in path for path in paths)
    project = _make_project(admin, "No bytes")
    file_id = _upload(admin, project["id"], b"secret bytes" * 100)["file"]["id"]
    for path in (f"{BASE}/{project['id']}/files/{file_id}/content", f"{BASE}/{project['id']}/files/{file_id}/derived/text.txt"):
        response = admin.get(path)
        assert response.status_code in (404, 405) and b"secret bytes" not in response.content
    assert db is not None
