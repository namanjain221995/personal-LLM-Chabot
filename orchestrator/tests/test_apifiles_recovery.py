"""A failed file recovers when its bytes are uploaded again, and the derived
routes tell a failed file from one still processing (gap 3, 2026-09-14).

Real handlers (stub auth), real Postgres, real runner and extraction child;
the embedder is an in-process stub that raises the openai SDK's own errors.
The verifier's two findings reproduced here: `files-embed-poisoned-reupload`
(the same bytes gave a new file that read `status: error` after 0.0 s, with
the engine back up) and `cross-project-1` (`GET …/derived` on a failed file
answered `409 file_not_ready`, "still being processed", with Retry-After).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest
from starlette.testclient import TestClient

from app import db, llm
from app.apifiles import derived, jobs, queue, schema
from tests import test_apifiles_jobs as J
from tests.apifiles_test_support import MULTIPART, StubAuth, assemble_all, build_app, headers_for, make_caller, multipart_body

files_schema = J.files_schema
isolated_app_db = J.isolated_app_db
ambient_identity = J.ambient_identity
files_root = J.files_root

TEXT = b"The escrow agent is Halden Trust, and the closing date is the third of March.\n" * 40


@pytest.fixture()
def api():
    caller = make_caller()
    app = build_app(StubAuth(caller), derived=derived)
    with TestClient(app) as client:
        yield client, caller


@pytest.fixture()
def woken():
    seen: List[str] = []
    queue.set_enqueue_hook(lambda blob: seen.append(str(blob["id"])))
    yield seen
    queue.set_enqueue_hook(None)


def _post(client, caller, data: bytes = TEXT, filename: str = "notes.txt") -> Dict[str, Any]:
    body = multipart_body([("purpose", "user_data"), ("file", filename, data, "text/plain")])
    response = client.post("/v1/files", headers={**headers_for(caller), "Content-Type": MULTIPART}, content=body)
    assert response.status_code == 200, response.text
    return response.json()


def _get(client, caller, file_id: str) -> Dict[str, Any]:
    return client.get(f"/v1/files/{file_id}", headers=headers_for(caller)).json()


def _blob_of(file_id: str) -> Dict[str, Any]:
    with db.connection() as con:
        row = con.execute("SELECT blob_id FROM api_files WHERE id = %s", (file_id,)).fetchone()
    return schema.get_api_file_blob(row["blob_id"])


def _server_error_embedder():
    """A batch gets HTTP 500 while the engine answers the one-input probe:
    evidence about the blob, so each run spends an attempt."""
    import openai

    httpx = llm._openai_httpx_module()

    async def embed(texts: Sequence[str]) -> Tuple[List[List[float]], Optional[int]]:
        if list(texts) == [jobs.PROBE_TEXT]:
            return [[1.0, 0.0]], 1
        request = httpx.Request("POST", "http://embed/v1/embeddings")
        raise openai.InternalServerError("boom", response=httpx.Response(500, request=request), body=None)

    return embed


async def _key_error_embedder(texts: Sequence[str]):
    raise KeyError("a bug in our own code")


def _fail_processing(code: str) -> None:
    """Drive the runner to a real failure with `code`."""
    if code == "processing_unavailable":
        outcomes = asyncio.run(J.runner(embed_documents=_server_error_embedder(), max_attempts=1).run_once())
    else:
        outcomes = asyncio.run(J.runner(embed_documents=_key_error_embedder).run_once())
    assert [(o.outcome, o.error_code) for o in outcomes] == [("failed", code)]


# ================================================================ recovery ==


@pytest.mark.parametrize("code", ["processing_unavailable", "internal_error"])
def test_uploading_the_same_bytes_again_re_queues_a_file_whose_processing_failed_and_both_files_then_process(api, woken, code):
    client, caller = api
    first = _post(client, caller)
    _fail_processing(code)
    failed = _get(client, caller, first["id"])
    assert failed["status"] == "error" and failed["processing"]["error"]["code"] == code
    failed_blob = _blob_of(first["id"])
    woken.clear()

    second = _post(client, caller)
    assert second["status"] == "uploaded", "the new file does not inherit the failure"
    assert second["processing"]["error"] is None and second["status_details"] is None
    blob = _blob_of(second["id"])
    assert blob["id"] == failed_blob["id"], "still one blob per (project, sha256)"
    assert (blob["status"], blob["error_code"], blob["attempt"], blob["not_before"]) == ("queued", None, 0, None)
    assert all(entry.get("status") != "failed" for entry in blob["stages"].values())
    assert blob["stages"]["chunk"]["status"] == "done", "finished stages are kept for the resumed run"
    assert "error_ceiling" not in blob["progress"] and "derived" not in blob["progress"]
    assert woken == [blob["id"]], "the runner is woken, not left to its poll"
    assert _get(client, caller, first["id"])["status"] == "uploaded", "the first file recovers with it"

    outcomes = asyncio.run(J.runner().run_once())
    assert [o.outcome for o in outcomes] == ["processed"]
    for file_id in (first["id"], second["id"]):
        assert _get(client, caller, file_id)["status"] == "processed"


def test_a_chunked_upload_of_the_same_bytes_re_queues_a_failed_blob_too(api, woken):
    client, caller = api
    first = _post(client, caller)
    _fail_processing("processing_unavailable")
    woken.clear()
    upload = client.post("/v1/uploads", headers=headers_for(caller), json={
        "bytes": len(TEXT), "filename": "notes.txt", "mime_type": "text/plain", "purpose": "user_data",
    }).json()
    half = len(TEXT) // 2
    for number, chunk in enumerate((TEXT[:half], TEXT[half:])):
        assert client.put(f"/v1/uploads/{upload['id']}/parts/{number}", headers=headers_for(caller), content=chunk).status_code == 200
    done = client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller), json={})
    assert done.status_code == 200, done.text
    assert [r.outcome for r in assemble_all(caller)] == ["attached"]
    blob = _blob_of(done.json()["file"]["id"])
    assert blob["id"] == _blob_of(first["id"])["id"]
    assert (blob["status"], blob["error_code"], blob["attempt"]) == ("queued", None, 0)
    assert woken == [blob["id"]]
    assert _get(client, caller, done.json()["file"]["id"])["status"] == "uploaded"


@pytest.mark.parametrize("code", ["unsupported_file", "file_corrupt", "file_too_complex"])
def test_bytes_whose_processing_ended_in_a_verdict_stay_failed_when_uploaded_again(api, code):
    client, caller = api
    first = _post(client, caller)
    J.set_blob(_blob_of(first["id"])["id"], "status = 'failed', error_code = %s, attempt = 0", code)
    second = _post(client, caller)
    assert second["status"] == "error" and second["processing"]["error"]["code"] == code
    assert _blob_of(second["id"])["status"] == "failed", "identical bytes would only fail identically"


def test_a_blob_failed_by_the_crash_loop_guard_is_not_re_queued_by_uploading_it_again(api):
    """Its runs kept taking the process down: a re-upload must not buy five more."""
    client, caller = api
    first = _post(client, caller)
    J.set_blob(
        _blob_of(first["id"])["id"],
        "status = 'failed', error_code = 'internal_error', progress = %s::jsonb",
        json.dumps({"crashes": 5}),
    )
    second = _post(client, caller)
    assert second["status"] == "error"
    assert _blob_of(second["id"])["status"] == "failed"


def test_simultaneous_re_uploads_of_a_failed_blob_all_join_it_and_re_queue_it_once(api, tmp_path):
    client, caller = api
    first = _post(client, caller)
    _fail_processing("internal_error")
    sha = hashlib.sha256(TEXT).hexdigest()
    barrier = threading.Barrier(6)
    results: List[Any] = []
    lock = threading.Lock()

    def upload(n: int) -> None:
        tmp = tmp_path / f"copy-{n}"
        tmp.write_bytes(TEXT)
        barrier.wait()
        try:
            got = queue.ingest_single(
                project_id=caller.project_id, workspace_id=caller.workspace_id, key_id=None, tmp_path=str(tmp),
                sha256=sha, bytes=len(TEXT), filename=f"copy-{n}.txt", purpose="user_data", expires_after_seconds=None,
            )
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            got = exc
        with lock:
            results.append(got)

    threads = [threading.Thread(target=upload, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [] and len(results) == 6, errors
    blob_ids = {str(r.blob["id"]) for r in results}
    assert blob_ids == {str(_blob_of(first["id"])["id"])}
    blob = _blob_of(first["id"])
    assert (blob["status"], blob["attempt"], blob["error_code"]) == ("queued", 0, None)


# ============================================================ derived routes ==


def test_the_derived_routes_of_a_file_whose_processing_failed_answer_400_with_its_own_sentence_and_no_retry(api):
    client, caller = api
    first = _post(client, caller)
    _fail_processing("processing_unavailable")
    sentence = _get(client, caller, first["id"])["status_details"]
    assert sentence.startswith("Processing could not reach a required service")
    for path in (f"/v1/files/{first['id']}/derived", f"/v1/files/{first['id']}/derived/text.txt"):
        response = client.get(path, headers=headers_for(caller))
        assert response.status_code == 400, (path, response.text)
        error = response.json()["error"]
        assert error["code"] == "invalid_request_error" and error["param"] == "file_id"
        assert error["message"] == "This file has no derived data: " + sentence
        assert "still being processed" not in error["message"]
        assert "retry-after" not in response.headers


def test_the_derived_routes_of_a_file_still_processing_keep_answering_409_file_not_ready_with_retry_after(api):
    client, caller = api
    first = _post(client, caller)  # queued, never run
    for path in (f"/v1/files/{first['id']}/derived", f"/v1/files/{first['id']}/derived/text.txt"):
        response = client.get(path, headers=headers_for(caller))
        assert response.status_code == 409, (path, response.text)
        assert response.json()["error"]["code"] == "file_not_ready"
        assert response.headers["retry-after"] == "5"


def test_the_derived_routes_of_an_upload_that_failed_its_checksum_answer_400_not_409(api):
    client, caller = api
    upload = client.post("/v1/uploads", headers=headers_for(caller), json={
        "bytes": 5, "filename": "x.txt", "mime_type": "text/plain", "purpose": "user_data",
    }).json()
    client.put(f"/v1/uploads/{upload['id']}/parts/0", headers=headers_for(caller), content=b"hello")
    done = client.post(f"/v1/uploads/{upload['id']}/complete", headers=headers_for(caller),
                       json={"sha256": hashlib.sha256(b"jello").hexdigest()})
    assert [r.outcome for r in assemble_all(caller)] == ["checksum_mismatch"]
    response = client.get(f"/v1/files/{done.json()['file']['id']}/derived", headers=headers_for(caller))
    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "This file has no derived data: The assembled bytes did not match the checksum you supplied."
    )


def test_the_derived_routes_of_a_processed_file_still_list_and_serve_its_text(api):
    client, caller = api
    first = _post(client, caller)
    assert [o.outcome for o in asyncio.run(J.runner().run_once())] == ["processed"]
    listed = client.get(f"/v1/files/{first['id']}/derived", headers=headers_for(caller))
    assert listed.status_code == 200 and "text.txt" in [n["name"] for n in listed.json()["data"]]
    text = client.get(f"/v1/files/{first['id']}/derived/text.txt", headers=headers_for(caller))
    assert text.status_code == 200 and b"Halden Trust" in text.content


@pytest.mark.parametrize("row, state", [
    ({"blob_id": "b", "blob_status": "processed", "blob_kind": "text"}, derived.READY),
    ({"blob_id": "b", "blob_status": "queued", "blob_kind": "unknown"}, derived.PENDING),
    ({"blob_id": "b", "blob_status": "processing", "blob_kind": "pdf"}, derived.PENDING),
    ({"blob_id": None, "assembling_upload_id": "upload_x"}, derived.PENDING),
    ({"blob_id": "b", "blob_status": "failed", "blob_kind": "pdf", "blob_error_code": "file_corrupt"}, derived.FAILED),
    ({"blob_id": "b", "blob_status": "processed", "blob_kind": "unsupported"}, derived.FAILED),
    ({"blob_id": None, "error_code": "checksum_mismatch"}, derived.FAILED),
])
def test_derived_readiness_is_ready_only_for_a_processed_blob_and_failed_for_every_ending_without_derived_data(row, state):
    assert derived.readiness(row).state == state
