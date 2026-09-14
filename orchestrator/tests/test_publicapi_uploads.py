"""`/v1/uploads` — create, parts (multipart and raw), complete, cancel (design §2.10–§2.15)."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os

import httpx
import pytest
from starlette.testclient import TestClient

from app import db
from app.apifiles import schema, storage
from tests.apifiles_test_support import (  # noqa: F401 - fixture by name
    StubAuth,
    assemble_all,
    build_app,
    headers_for,
    isolated_app_db,
    make_caller,
)


@pytest.fixture()
def api():
    caller = make_caller()
    usage: list = []
    app = build_app(StubAuth(caller), usage=usage)
    with TestClient(app) as client:
        yield client, caller, usage, app


def _upload(client, caller, size, **extra):
    body = {"bytes": size, "filename": "big.bin", "mime_type": "application/octet-stream", "purpose": "user_data", **extra}
    response = client.post("/v1/uploads", headers=headers_for(caller), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _post_part(client, caller, upload_id, data, **fields):
    return client.post(
        f"/v1/uploads/{upload_id}/parts",
        headers=headers_for(caller),
        files={"data": ("upload", data, "application/octet-stream")},
        data={k: str(v) for k, v in fields.items()},
    )


def _put_part(client, caller, upload_id, number, data, **headers):
    return client.put(f"/v1/uploads/{upload_id}/parts/{number}", headers={**headers_for(caller), **headers}, content=data)


def _complete(client, caller, upload_id, body=None):
    return client.post(f"/v1/uploads/{upload_id}/complete", headers=headers_for(caller), json=body if body is not None else {})


def test_create_answers_a_pending_upload_with_its_part_limits(api):
    client, caller, _usage, _app = api
    body = _upload(client, caller, 2 ** 31)
    assert body["object"] == "upload" and body["status"] == "pending" and body["file"] is None
    assert body["part_max_bytes"] == 67_108_864 and body["max_parts"] == 10_000 and body["bytes_received"] == 0
    assert body["expires_at"] - body["created_at"] == 86_400
    assert os.path.isdir(storage.parts_dir(body["id"]))


@pytest.mark.parametrize(
    "overrides,param",
    [
        ({"bytes": 107_374_182_401}, "bytes"),
        ({"bytes": -1}, "bytes"),
        ({"bytes": True}, "bytes"),
        ({"bytes": "10"}, "bytes"),
        ({"filename": ""}, "filename"),
        ({"mime_type": None}, "mime_type"),
        ({"purpose": "batch"}, "purpose"),
        ({"project_id": "proj_x"}, "project_id"),
        ({"expires_after": {"anchor": "created_at", "seconds": 10}}, "expires_after.seconds"),
        ({"expires_after": {"anchor": "created_at", "seconds": 3600, "x": 1}}, "x"),
    ],
)
def test_create_refuses_bad_fields_naming_them(api, overrides, param):
    client, caller, _usage, _app = api
    body = {"bytes": 10, "filename": "a", "mime_type": "text/plain", "purpose": "user_data", **overrides}
    body = {k: v for k, v in body.items() if v is not None}
    response = client.post("/v1/uploads", headers=headers_for(caller), json=body)
    assert response.status_code == 400
    assert response.json()["error"]["param"] == param


def test_an_upload_of_100_gib_is_accepted_and_the_message_above_it_says_the_ceiling(api):
    client, caller, _usage, _app = api
    assert _upload(client, caller, 107_374_182_400)["bytes"] == 107_374_182_400
    response = client.post("/v1/uploads", headers=headers_for(caller), json={"bytes": 107_374_182_401, "filename": "a", "mime_type": "x", "purpose": "user_data"})
    assert "100 GiB" in response.json()["error"]["message"]


def test_a_numbered_part_resent_keeps_its_id_and_replaces_its_bytes(api):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 10)
    first = _put_part(client, caller, upload["id"], 0, b"aaaaa").json()
    again = _put_part(client, caller, upload["id"], 0, b"bbbbb").json()
    via_post = _post_part(client, caller, upload["id"], b"ccccc", part_number=0).json()
    assert first["id"] == again["id"] == via_post["id"]
    assert open(storage.part_path(upload["id"], 0), "rb").read() == b"ccccc"
    state = client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()
    assert [p["part_number"] for p in state["parts"]] == [0] and state["bytes_received"] == 5
    assert os.listdir(storage.parts_dir(upload["id"])) == ["0"]


def test_sequential_parts_take_the_next_number_and_need_part_ids_at_complete(api):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 10)
    a = _post_part(client, caller, upload["id"], b"hello").json()
    b = _post_part(client, caller, upload["id"], b"world").json()
    assert (a["part_number"], b["part_number"]) == (0, 1)
    refused = _complete(client, caller, upload["id"])
    assert refused.status_code == 400 and refused.json()["error"]["param"] == "part_ids"
    assert client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()["status"] == "pending"
    done = _complete(client, caller, upload["id"], {"part_ids": [b["id"], a["id"]]})
    assert done.status_code == 200
    assemble_all(caller)
    file_id = done.json()["file"]["id"]
    assert client.get(f"/v1/files/{file_id}/content", headers=headers_for(caller)).content == b"worldhello"


@pytest.mark.parametrize("first_numbered", [True, False])
def test_numbered_and_sequential_parts_cannot_be_mixed(api, first_numbered):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 10)
    if first_numbered:
        assert _put_part(client, caller, upload["id"], 0, b"hello").status_code == 200
        mixed = _post_part(client, caller, upload["id"], b"world")
    else:
        assert _post_part(client, caller, upload["id"], b"hello").status_code == 200
        mixed = _put_part(client, caller, upload["id"], 1, b"world")
    assert mixed.status_code == 400 and mixed.json()["error"]["param"] == "part_number"
    assert "do not mix" in mixed.json()["error"]["message"]
    assert len(os.listdir(storage.parts_dir(upload["id"]))) == 1


def test_a_zero_length_part_is_a_400_on_both_routes_and_records_nothing(api):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 10)
    raw = _put_part(client, caller, upload["id"], 0, b"")
    form = _post_part(client, caller, upload["id"], b"")
    for response in (raw, form):
        assert response.status_code == 400 and response.json()["error"]["param"] == "data"
    assert client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()["parts"] == []
    assert os.listdir(storage.parts_dir(upload["id"])) == []


def test_a_lost_acknowledgement_on_a_small_upload_does_not_poison_it(api):
    """Finding #18: the round-1 orphan allowance (max(64 MiB, 10%)) made one
    retried sequential part on an upload ≤ 640 MiB a permanent failure. The
    budget is disk-based now, and `complete`'s byte sum decides."""
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 10)
    a = _post_part(client, caller, upload["id"], b"hello").json()
    lost = _post_part(client, caller, upload["id"], b"world").json()
    retried = _post_part(client, caller, upload["id"], b"world").json()
    assert lost["id"] != retried["id"]
    done = _complete(client, caller, upload["id"], {"part_ids": [a["id"], retried["id"]]})
    assert done.status_code == 200, done.text
    assemble_all(caller)
    assert client.get(f"/v1/files/{done.json()['file']['id']}/content", headers=headers_for(caller)).content == b"helloworld"


def test_the_part_budget_refuses_bytes_beyond_what_the_upload_could_ever_use(api, monkeypatch):
    client, caller, _usage, _app = api
    monkeypatch.setenv("PUBLIC_API_FILES_MAX_PARTS", "3")
    monkeypatch.setenv("PUBLIC_API_FILES_PART_MAX_BYTES", "8")
    upload = _upload(client, caller, 8)
    # budget = min(upload_max * 1.1, declared + (max_parts - accepted_parts) * part_max)
    # part 0: 8 <= 8 + 3*8; part 1: 16 <= 8 + 2*8; part 2: 24 > 8 + 1*8.
    for number in range(2):
        assert _put_part(client, caller, upload["id"], number, b"12345678").status_code == 200
    over = _put_part(client, caller, upload["id"], 2, b"12345678")
    assert over.status_code == 400 and over.json()["error"]["param"] == "data"
    assert "part budget" in over.json()["error"]["message"]
    assert sorted(os.listdir(storage.parts_dir(upload["id"]))) == ["0", "1"]
    # Replacing an accepted part does not count its old bytes twice.
    assert _put_part(client, caller, upload["id"], 1, b"87654321").status_code == 200


def test_a_part_digest_mismatch_is_refused_without_retry_and_records_nothing(api):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 5)
    bad = _put_part(client, caller, upload["id"], 0, b"hello", **{"X-Part-SHA256": hashlib.sha256(b"jello").hexdigest()})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "checksum_mismatch"
    assert bad.headers["x-should-retry"] == "false"
    bad_form = _post_part(client, caller, upload["id"], b"hello", sha256=hashlib.sha256(b"jello").hexdigest())
    assert bad_form.status_code == 400 and bad_form.json()["error"]["code"] == "checksum_mismatch"
    digest = base64.b64encode(hashlib.sha256(b"hello").digest()).decode()
    good = _put_part(client, caller, upload["id"], 0, b"hello", **{"Content-Digest": f"sha-256=:{digest}:"})
    assert good.status_code == 200 and good.json()["sha256"] == hashlib.sha256(b"hello").hexdigest()
    assert os.listdir(storage.parts_dir(upload["id"])) == ["0"]


def test_a_raw_part_without_content_length_is_411_and_over_the_cap_is_413(api, monkeypatch):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 5)

    def chunked():
        yield b"hel"
        yield b"lo"

    missing = client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(caller), content=chunked())
    assert missing.status_code == 411 and missing.json()["error"]["param"] == "Content-Length"
    monkeypatch.setenv("PUBLIC_API_FILES_PART_MAX_BYTES", "4")
    over = _put_part(client, caller, upload["id"], 0, b"hello")
    assert over.status_code == 413
    bad_number = _put_part(client, caller, upload["id"], 10_000, b"hi")
    assert bad_number.status_code == 400 and "0-based" in bad_number.json()["error"]["message"]


def test_complete_without_part_ids_needs_contiguous_numbered_parts_and_names_the_gap(api):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 15)
    _put_part(client, caller, upload["id"], 0, b"aaaaa")
    _put_part(client, caller, upload["id"], 2, b"ccccc")
    _put_part(client, caller, upload["id"], 3, b"ddddd")
    gap = _complete(client, caller, upload["id"])
    assert gap.status_code == 400 and gap.json()["error"]["message"] == "Part 1 is missing."
    _put_part(client, caller, upload["id"], 1, b"bbbbb")
    short = _complete(client, caller, upload["id"])
    assert short.status_code == 400
    assert short.json()["error"]["message"] == "The listed parts hold 20 bytes; the upload declared 15."
    state = client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()
    assert state["status"] == "pending", "a refused complete returns the upload to pending"


def test_complete_always_answers_200_with_a_nested_file_before_any_byte_is_copied(api):
    """Finding #1. The file exists at once, in stage `assemble`; the bytes
    arrive when the stage runs, and `wait_for_processing(up.file.id)` works."""
    client, caller, _usage, _app = api
    data = os.urandom(3 * 1024 * 1024)
    upload = _upload(client, caller, len(data))
    for number, start in enumerate(range(0, len(data), 1024 * 1024)):
        assert _put_part(client, caller, upload["id"], number, data[start:start + 1024 * 1024]).status_code == 200
    done = _complete(client, caller, upload["id"], {"sha256": hashlib.sha256(data).hexdigest()})
    assert done.status_code == 200
    body = done.json()
    assert body["status"] == "completed" and body["file"]["object"] == "file"
    nested = body["file"]
    assert nested["status"] == "uploaded" and nested["processing"]["stage"] == "assemble" and nested["sha256"] is None
    file_id = nested["id"]
    before = client.get(f"/v1/files/{file_id}", headers=headers_for(caller)).json()
    assert before["processing"]["stage"] == "assemble" and before["bytes"] == len(data)
    not_yet = client.get(f"/v1/files/{file_id}/content", headers=headers_for(caller))
    assert not_yet.status_code == 409 and not_yet.json()["error"]["code"] == "file_not_ready"
    assert "retry-after" in not_yet.headers
    results = assemble_all(caller)
    assert [r.outcome for r in results] == ["attached"]
    after = client.get(f"/v1/files/{file_id}", headers=headers_for(caller)).json()
    assert after["sha256"] == hashlib.sha256(data).hexdigest() and after["processing"]["stage"] == "sniff"
    assert client.get(f"/v1/files/{file_id}/content", headers=headers_for(caller)).content == data
    assert not os.path.exists(storage.upload_dir(upload["id"])), "parts are removed once the file has its blob"


@pytest.mark.parametrize("field", ["md5", "sha256"])
def test_a_supplied_checksum_that_does_not_match_fails_the_file_not_the_complete(api, field):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 5)
    _put_part(client, caller, upload["id"], 0, b"hello")
    wrong = {"md5": hashlib.md5(b"jello").hexdigest(), "sha256": hashlib.sha256(b"jello").hexdigest()}[field]
    done = _complete(client, caller, upload["id"], {field: wrong})
    assert done.status_code == 200 and done.json()["status"] == "completed"
    assert [r.outcome for r in assemble_all(caller)] == ["checksum_mismatch"]
    failed = client.get(f"/v1/files/{done.json()['file']['id']}", headers=headers_for(caller)).json()
    assert failed["status"] == "error"
    assert failed["status_details"] == "The assembled bytes did not match the checksum you supplied."
    assert client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller)).json()["status"] == "completed"
    assert not os.path.exists(storage.upload_dir(upload["id"]))


def test_a_matching_md5_is_accepted(api):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 5)
    _put_part(client, caller, upload["id"], 0, b"hello")
    done = _complete(client, caller, upload["id"], {"md5": hashlib.md5(b"hello").hexdigest()})
    assert [r.outcome for r in assemble_all(caller)] == ["attached"]
    assert client.get(f"/v1/files/{done.json()['file']['id']}", headers=headers_for(caller)).json()["status"] == "uploaded"


def test_concurrent_completes_both_answer_the_same_upload_and_file(api):
    _client, caller, _usage, app = api
    upload = _upload(_client, caller, 5)
    _put_part(_client, caller, upload["id"], 0, b"hello")

    async def race():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            calls = [client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={}) for _ in range(4)]
            return await asyncio.gather(*calls)

    responses = asyncio.run(race())
    assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
    assert len({r.json()["file"]["id"] for r in responses}) == 1
    replay = _complete(_client, caller, upload["id"])
    assert replay.status_code == 200 and replay.json()["file"]["id"] == responses[0].json()["file"]["id"]
    with db.connection() as con:
        files = con.execute("SELECT count(*) AS n FROM api_files WHERE project_id = %s", (caller.project_id,)).fetchone()["n"]
    assert files == 1


def test_parts_after_complete_are_409_that_the_sdks_will_not_retry(api):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 5)
    _put_part(client, caller, upload["id"], 0, b"hello")
    assert _complete(client, caller, upload["id"]).status_code == 200
    for response in (_put_part(client, caller, upload["id"], 1, b"x"), _post_part(client, caller, upload["id"], b"x")):
        assert response.status_code == 409 and response.json()["error"]["code"] == "upload_state_conflict"
        assert response.headers["x-should-retry"] == "false"


def test_cancel_removes_the_parts_is_idempotent_and_refuses_a_completed_upload(api):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 5)
    _put_part(client, caller, upload["id"], 0, b"hello")
    first = client.post(f"/v1/uploads/{upload['id']}/cancel", headers=headers_for(caller))
    assert first.status_code == 200 and first.json()["status"] == "cancelled"
    assert not os.path.exists(storage.upload_dir(upload["id"]))
    assert client.post(f"/v1/uploads/{upload['id']}/cancel", headers=headers_for(caller)).json()["status"] == "cancelled"
    assert _put_part(client, caller, upload["id"], 0, b"hello").status_code == 409
    assert _complete(client, caller, upload["id"]).status_code == 409
    done = _upload(client, caller, 5)
    _put_part(client, caller, done["id"], 0, b"hello")
    _complete(client, caller, done["id"])
    refused = client.post(f"/v1/uploads/{done['id']}/cancel", headers=headers_for(caller))
    assert refused.status_code == 409 and refused.headers["x-should-retry"] == "false"


def test_a_part_that_finishes_streaming_after_cancel_is_discarded(api):
    """The status is re-read under the row lock after the body: a cancel that
    won while the part streamed leaves no file and no row."""
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 5)
    real = schema.upsert_api_upload_part

    def cancel_first(*args, **kwargs):
        schema.cancel_api_upload(caller.project_id, upload["id"])
        return real(*args, **kwargs)

    schema.upsert_api_upload_part = cancel_first
    try:
        response = _put_part(client, caller, upload["id"], 0, b"hello")
    finally:
        schema.upsert_api_upload_part = real
    assert response.status_code == 409
    assert os.listdir(storage.parts_dir(upload["id"])) == []


def test_an_empty_upload_completes_into_an_empty_file(api):
    client, caller, _usage, _app = api
    upload = _upload(client, caller, 0)
    done = _complete(client, caller, upload["id"])
    assert done.status_code == 200
    assert [r.outcome for r in assemble_all(caller)] == ["attached"]
    body = client.get(f"/v1/files/{done.json()['file']['id']}", headers=headers_for(caller)).json()
    assert body["bytes"] == 0 and body["sha256"] == hashlib.sha256(b"").hexdigest()


def test_upload_routes_write_one_usage_record_each(api):
    client, caller, usage, _app = api
    upload = _upload(client, caller, 5)
    _put_part(client, caller, upload["id"], 0, b"hello")
    client.get(f"/v1/uploads/{upload['id']}", headers=headers_for(caller))
    _complete(client, caller, upload["id"])
    assert [u.route for u in usage] == ["v1_uploads_create", "v1_uploads_part", "v1_uploads_get", "v1_uploads_complete"]
    assert usage[1].meta == {"part_number": 0, "bytes": 5, "route": "raw"}
    assert all(u.generation_id == upload["id"] for u in usage)
