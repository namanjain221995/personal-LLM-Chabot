"""Files, chunked Uploads, resume, and files as model input.

Rewritten 2026-09-13 against the Files API as BUILT (the wire in
`orchestrator/app/publicapi/files/wire.py`, the handlers in `routes.py`, the
model-input facade in `apifiles/service.py`), not only the design:

* ids: `file-<24 hex>`, `upload_<24 hex>`, `part_<24 hex>`;
* File: OpenAI's keys plus `sha256`, `mime_type` and a `processing` object;
  `status` stays in {uploaded, processed, error} so `wait_for_processing`
  works unchanged;
* parts: an optional `part_number` (0-based) and `sha256` form field on
  `POST /uploads/{id}/parts`; a raw `PUT /uploads/{id}/parts/{n}` with
  `X-Part-SHA256` or `Content-Digest`; `GET /uploads/{id}` lists the parts
  the server holds;
* `complete` always answers 200 with a nested `file` and copies no bytes: a
  wrong `md5` or `sha256` surfaces later, as that file ending in `error` with
  `processing.error.code = checksum_mismatch`;
* a foreign project's id answers exactly like an id that never existed, on
  every file and upload route and as model input;
* scopes: each route needs exactly its one scope (`files.read` or
  `files.write`, `GET /uploads/{id}` included under `files.write`), checked
  BEFORE any lookup, so a real id and a random id get the same 403; a
  `file_id` in a prompt needs `files.read` on top of `responses.write`, while
  inline `file_data` needs no file scope;
* model input: `input_file` / `input_image` / `input_video` with a `file_id`,
  inline `file_data` and `file_context` on /v1/responses; the `file` part and
  `input_audio` on /v1/chat/completions. The built Responses dialect has no
  `input_audio` part: an uploaded recording reaches /v1/responses as
  `input_file` or `input_video`. A failed file, or a file of the wrong kind
  for its part, is a 400 naming that part's `file_id`.

HOW THE AUDIO TESTS KNOW THE RECORDING REACHED THE MODEL (2026-09-13, two
review findings). The fixture is a tone, not speech: a whisper no-speech gate
may return an empty transcript for it, so no test may depend on its words.
* An uploaded recording (`input_file` / `input_video` with a `file_id`): the
  model is asked how many seconds long it is, and must answer TONE_SECONDS.
  Only the recording's context block carries that number (its BEGIN line and
  its `Duration m:ss` header); the bare question must NOT get it. A prompt-
  token comparison was tried first and is not enough: with a `file_id` in the
  request the system addendum and a "(The file … is attached above.)"
  placeholder reach the prompt even when the block itself is dropped, so the
  count grows by ~160 tokens with nothing of the recording in it.
* `input_audio` on /v1/chat/completions: the clip adds nothing to the prompt
  but its transcript block (no addendum, no placeholder), so the prompt count
  with the clip must exceed the bare question's by at least
  MIN_MEDIA_BLOCK_TOKENS — the block's two DATA delimiter lines alone are more
  than that in any tokenizer, even with an empty transcript. That test needs
  `usage`.

Refusals made before a body is read (411, 413) come in the orchestrator's
form or the v1-gateway's edge form; set TECHSARA_FILES_EDGE_REFUSALS=origin or
=edge to pin the one this run's path must give (files_wire.pre_body_refusal).

Feature gates (techsara_conformance/features.py): `files` = POST /v1/files,
`uploads` = POST /v1/uploads, `uploads_resume` = GET /v1/uploads/{id},
`files_model_input` = the schema mentions `input_file`. A planned feature's
tests run as XFAIL(strict).

Keys beyond the main one, each optional (a test that needs a missing key
SKIPs with the reason):
* another project's key — TECHSARA_OTHER_PROJECT_API_KEY or
  `other_project_api_key` in the keys file (cross-project tests);
* the limited key of the SAME project (`models.read` only) —
  TECHSARA_LIMITED_API_KEY or `limited_api_key` (files and uploads scopes);
* a key of the SAME project holding `responses.write` but no `files.*` —
  TECHSARA_RESPONSES_ONLY_API_KEY or `responses_only_api_key` (model-input
  scope).

Proven by `selftest/files_selftest.py` against `selftest/files_local_target.py`
(the real handlers under uvicorn, stub auth, stub engines; its docstring says
what it stubs): all PASS as built, all XPASS(strict) when forced planned, and
each deliberate defect in `files_local_target.DEFECTS` fails exactly the tests
`files_selftest.EXPECTED` lists for it. Re-run it after any change here.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
import openai
import pytest

from techsara_conformance import files_wire as fw
from techsara_conformance import media, pacing

KIB = 1024
#: Small parts on purpose: the suite may be pointed at a shared stack, and the
#: server has no lower bound on a part size.
PART = 64 * KIB
POLL_S = float(os.environ.get("TECHSARA_FILES_POLL_S", "") or 2.0)
PROCESSING_TIMEOUT_S = float(os.environ.get("TECHSARA_FILES_PROCESSING_TIMEOUT_S", "") or 900.0)
#: The least an `input_audio` transcript block adds to a prompt: its two DATA
#: delimiter lines alone are more than this in any tokenizer (module docstring).
MIN_MEDIA_BLOCK_TOKENS = 8
#: The uploaded tone's length. Seven, not two: a model that guesses a short
#: clip's length should not pass by luck (module docstring).
TONE_SECONDS = 7
#: "7", "7 seconds", "0:07" — not "17" or "70".
TONE_SECONDS_RE = re.compile(r"(?<!\d)0?7(?!\d)")
CHECKSUM_SENTENCE = "The assembled bytes did not match the checksum you supplied."


# --------------------------------------------------------------- helpers --


def _tag() -> str:
    return uuid.uuid4().hex[:12]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _wait(client: Any, file_id: str) -> Dict[str, Any]:
    return fw.as_dict(client.files.wait_for_processing(file_id, poll_interval=POLL_S, max_wait_seconds=PROCESSING_TIMEOUT_S))


def _content(client: Any, file_id: str) -> bytes:
    return client.files.content(file_id).read()


def _get(client: Any, path: str) -> Dict[str, Any]:
    return client.get(path, cast_to=object)


def _delete_quietly(client: Any, file_id: Optional[str]) -> None:
    if not file_id:
        return
    try:
        client.files.delete(file_id)
    except openai.APIError:
        pass


def _cancel_quietly(client: Any, upload_id: Optional[str]) -> None:
    """Leave no pending upload behind a failed or skipped test (a completed
    one answers 409 here, which is fine)."""
    if not upload_id:
        return
    try:
        client.uploads.cancel(upload_id)
    except openai.APIError:
        pass


def _finish(client: Any, upload_id: str, file_id: Optional[str]) -> None:
    if file_id:
        _delete_quietly(client, file_id)
    else:
        _cancel_quietly(client, upload_id)


def _chunks(data: bytes, size: int = PART) -> List[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)]


def _extra_key(pytestconfig: pytest.Config, env_name: str, keys_name: str) -> Optional[str]:
    explicit = os.environ.get(env_name)
    if explicit:
        return explicit
    keys_file = pytestconfig.getoption("--keys-file") or os.environ.get("TECHSARA_KEYS_FILE")
    if keys_file:
        try:
            return json.loads(Path(keys_file).read_text(encoding="utf-8")).get(keys_name) or None
        except (OSError, ValueError):
            return None
    return None


def _require_other_key(pytestconfig: pytest.Config) -> str:
    key = _extra_key(pytestconfig, "TECHSARA_OTHER_PROJECT_API_KEY", "other_project_api_key")
    if not key:
        pytest.skip(
            "no key of another project: set TECHSARA_OTHER_PROJECT_API_KEY or `other_project_api_key` in the keys "
            "file (a second test project's key with the default scopes)"
        )
    return key


def _require_limited_key(target: Any) -> str:
    if not target.limited_api_key:
        pytest.skip("no limited key: set TECHSARA_LIMITED_API_KEY to a key of the same project holding only models.read "
                    "(tools/provision_key.py makes one)")
    return target.limited_api_key


def _require_responses_only_key(pytestconfig: pytest.Config) -> str:
    key = _extra_key(pytestconfig, "TECHSARA_RESPONSES_ONLY_API_KEY", "responses_only_api_key")
    if not key:
        pytest.skip(
            "no responses-only key: set TECHSARA_RESPONSES_ONLY_API_KEY or `responses_only_api_key` in the keys file "
            "(a key of the SAME project holding responses.write and no files.* scope)"
        )
    return key


def _status_error(call: Callable[[], Any]) -> openai.APIStatusError:
    with pytest.raises(openai.APIStatusError) as caught:
        call()
    return caught.value


def _random_file_id() -> str:
    return "file-" + uuid.uuid4().hex[:24]


def _random_upload_id() -> str:
    return "upload_" + uuid.uuid4().hex[:24]


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _upload_probes(caller: Any) -> Tuple[Tuple[str, Callable[[str], Any]], ...]:
    """Every route that names an upload id (routes 10–14), as `caller`."""
    return (
        ("retrieve", lambda uid: _get(caller, f"/uploads/{uid}")),
        ("parts.create", lambda uid: caller.uploads.parts.create(upload_id=uid, data=b"intruder")),
        ("put part", lambda uid: caller.put(f"/uploads/{uid}/parts/0", content=b"intruder", cast_to=object,
                                            options={"headers": {"Content-Type": "application/octet-stream"}})),
        ("complete", lambda uid: caller.uploads.complete(upload_id=uid, part_ids=[])),
        ("cancel", lambda uid: caller.uploads.cancel(uid)),
    )


def _input_tokens(usage: Any, key: str) -> int:
    assert usage is not None, (
        "usage is null: this test needs the engine's prompt count to prove the recording reached the model "
        "(module docstring)"
    )
    count = getattr(usage, key)
    assert isinstance(count, int) and count > 0, usage
    return count


class _DropsOnePart:
    """Cuts ONE part upload off mid-body, on the client's own transport.

    The `cut`-th `POST …/parts` attempt (0-based) sends half its bytes and then
    the connection dies — what a network drop mid-part looks like from the
    client. It also remembers the id `POST /uploads` returned, so a test can
    resume an upload the SDK helper never handed back."""

    def __init__(self, inner: Any, *, cut: int) -> None:
        import httpx2

        self._inner = inner
        self._cut = cut
        self._stream_base = httpx2.SyncByteStream
        self.parts_attempted = 0
        self.cuts = 0
        self.upload_ids: List[str] = []

    def handle_request(self, request: Any) -> Any:
        path = request.url.path
        if request.method == "POST" and path.endswith("/parts"):
            attempt = self.parts_attempted
            self.parts_attempted += 1
            if attempt == self._cut and self.cuts == 0:
                self.cuts += 1
                body = request.read()

                class HalfThenDrop(self._stream_base):  # type: ignore[misc, name-defined]
                    def __iter__(self):
                        yield body[: len(body) // 2]
                        raise ConnectionResetError("conformance: the network dropped mid-part")

                request.stream = HalfThenDrop()
        response = self._inner.handle_request(request)
        if request.method == "POST" and path.endswith("/uploads") and response.status_code == 200:
            response.read()
            self.upload_ids.append(json.loads(response.content)["id"])
        return response

    def close(self) -> None:
        self._inner.close()


def _dropping_client(target: Any, *, cut: int, max_retries: int):
    import httpx2

    dropper = _DropsOnePart(pacing.wrap(httpx2.HTTPTransport(), httpx2.BaseTransport), cut=cut)

    class Transport(httpx2.BaseTransport):
        def handle_request(self, request: Any) -> Any:
            return dropper.handle_request(request)

        def close(self) -> None:
            dropper.close()

    http_client = openai.DefaultHttpxClient(
        timeout=target.request_timeout_s,
        event_hooks={"request": [pacing.on_request], "response": [pacing.on_response]},
        transport=Transport(),
    )
    client = openai.OpenAI(
        base_url=target.base_url, api_key=target.api_key, max_retries=max_retries,
        timeout=target.request_timeout_s, http_client=http_client,
    )
    return client, dropper


# ------------------------------------------------------------------ files --


@pytest.mark.feature("files")
def test_a_small_text_file_round_trips_through_create_retrieve_list_content_and_delete(client):
    data = f"conformance {_tag()}\n".encode() * 64
    created = fw.assert_file(
        client.files.create(file=("notes.txt", data, "text/plain"), purpose="user_data"),
        bytes=len(data), filename="notes.txt", purpose="user_data",
    )
    try:
        assert created["expires_at"] is None and created["status"] in ("uploaded", "processed"), created
        got = fw.assert_file(client.files.retrieve(created["id"]), bytes=len(data), sha256=_sha(data))
        assert got["id"] == created["id"] and got["created_at"] == created["created_at"]
        assert created["id"] in {f.id for f in client.files.list(purpose="user_data", limit=100)}
        assert _content(client, created["id"]) == data
    finally:
        deleted = client.files.delete(created["id"])
    assert (deleted.id, deleted.deleted, deleted.object) == (created["id"], True, "file")
    with pytest.raises(openai.NotFoundError) as caught:
        client.files.retrieve(created["id"])
    fw.sdk_error(caught.value, code="file_not_found")
    with pytest.raises(openai.NotFoundError) as again:
        client.files.delete(created["id"])
    fw.sdk_error(again.value, code="file_not_found")


@pytest.mark.feature("files")
def test_wait_for_processing_returns_a_text_file_processed_with_its_kind_stages_and_facts(client):
    data = fw.text_payload(3 * KIB, _tag())
    created = fw.assert_file(client.files.create(file=("processed.txt", data, "text/plain"), purpose="user_data"))
    try:
        done = fw.assert_file(_wait(client, created["id"]), bytes=len(data), sha256=_sha(data))
        assert done["status"] == "processed" and done["status_details"] is None, done
        processing = done["processing"]
        assert processing["kind"] == "text" and processing["percent"] == 100, processing
        assert all(stage["status"] in ("done", "skipped") for stage in processing["stages"]), processing["stages"]
        assert isinstance(processing["facts"], dict) and done["mime_type"] == "text/plain", done
    finally:
        _delete_quietly(client, created["id"])


@pytest.mark.feature("files")
def test_expires_after_sets_expires_at_to_created_at_plus_the_seconds_given(client):
    created = fw.assert_file(client.files.create(
        file=("expiring.txt", b"expires in an hour\n", "text/plain"), purpose="user_data",
        expires_after={"anchor": "created_at", "seconds": 3600},
    ))
    try:
        assert created["expires_at"] - created["created_at"] == 3600, created
        with pytest.raises(openai.BadRequestError) as caught:
            client.files.create(file=("x.txt", b"x", "text/plain"), purpose="user_data",
                                expires_after={"anchor": "created_at", "seconds": 60})
        fw.sdk_error(caught.value, code="invalid_request_error", param="expires_after.seconds")
    finally:
        _delete_quietly(client, created["id"])


@pytest.mark.feature("files")
def test_a_purpose_this_platform_has_no_product_for_is_a_400_naming_purpose(client):
    with pytest.raises(openai.BadRequestError) as caught:
        client.files.create(file=("x.jsonl", b"{}\n", "application/jsonl"), purpose="fine-tune")
    fw.sdk_error(caught.value, code="invalid_request_error", param="purpose")


@pytest.mark.feature("files")
def test_list_pages_newest_first_with_limit_and_after_until_has_more_is_false(client):
    # Only this test's three files are asserted on: the project may hold other
    # callers' files, created before, between or after ours (review finding).
    tag = _tag()
    made: List[str] = []
    try:
        for n in range(3):
            made.append(client.files.create(file=(f"page-{n}.txt", f"{tag} {n}\n".encode(), "text/plain"),
                                            purpose="user_data").id)
        seen: List[str] = []
        newest_page_has_more: Optional[bool] = None
        after: Optional[str] = None
        for _ in range(200):
            page = client.files.list(limit=2, order="desc", **({"after": after} if after else {}))
            ids = [f.id for f in page.data]
            assert len(ids) <= 2, f"limit=2 answered {len(ids)} files"
            if made[2] in ids:
                newest_page_has_more = page.has_more
            seen += [i for i in ids if i in made]
            if len(seen) == 3 or not page.has_more or not ids:
                break
            after = ids[-1]
        assert seen == [made[2], made[1], made[0]], f"newest first, got {seen} for {made}"
        assert newest_page_has_more is True, "the page holding the newest file said there was nothing after it"
        older = [f.id for f in client.files.list(limit=2, order="desc", after=made[1]).data]
        assert made[0] in older and made[2] not in older and made[1] not in older, older
        newer = [f.id for f in client.files.list(limit=100, order="asc", after=made[0]).data]
        assert made[1] in newer and made[2] in newer and newer.index(made[1]) < newer.index(made[2]), newer
        assert made[0] not in newer, newer
        assert client.files.list(limit=10000, order="asc", after=made[2]).has_more is False
        with pytest.raises(openai.BadRequestError) as caught:
            client.files.list(limit=0)
        fw.sdk_error(caught.value, code="invalid_request_error", param="limit")
    finally:
        for file_id in made:
            _delete_quietly(client, file_id)


@pytest.mark.feature("files")
def test_content_serves_one_byte_range_as_206_answers_304_to_its_etag_and_416_past_the_end(client, raw):
    data = fw.text_payload(10 * KIB, _tag())
    file_id = client.files.create(file=("ranged.txt", data, "text/plain"), purpose="user_data").id
    try:
        whole = raw.get(f"files/{file_id}/content")
        assert whole.status_code == 200 and whole.content == data, whole.status_code
        headers = whole.headers
        assert headers.get("etag") == f'"{_sha(data)}"', headers.get("etag")
        assert headers.get("accept-ranges") == "bytes" and headers.get("x-content-type-options") == "nosniff", dict(headers)
        assert headers.get("content-disposition", "").startswith("attachment;"), headers.get("content-disposition")
        assert "sandbox" in headers.get("content-security-policy", "") and "no-store" in headers.get("cache-control", "")
        part = raw.get(f"files/{file_id}/content", headers={"Range": "bytes=100-1123"})
        assert part.status_code == 206 and part.content == data[100:1124], part.status_code
        assert part.headers.get("content-range") == f"bytes 100-1123/{len(data)}", part.headers.get("content-range")
        cached = raw.get(f"files/{file_id}/content", headers={"If-None-Match": f'"{_sha(data)}"'})
        assert cached.status_code == 304 and cached.content == b"", cached.status_code
        past = raw.get(f"files/{file_id}/content", headers={"Range": f"bytes={len(data)}-"})
        fw.envelope(past.status_code, past.json(), past.headers, code="invalid_request_error", param="Range")
        assert past.status_code == 416 and past.headers.get("content-range") == f"bytes */{len(data)}"
    finally:
        _delete_quietly(client, file_id)


@pytest.mark.feature("files")
def test_a_deleted_a_random_and_a_malformed_file_id_get_one_404_file_not_found_body(client):
    file_id = client.files.create(file=("gone.txt", b"soon gone\n", "text/plain"), purpose="user_data").id
    client.files.delete(file_id)
    bodies = []
    for probe in (file_id, _random_file_id(), "file-not-a-real-id"):
        exc = _status_error(lambda: client.files.retrieve(probe))
        error = fw.sdk_error(exc, code="file_not_found")
        assert probe not in json.dumps(error), "the id is echoed: a foreign id's body would then differ"
        bodies.append(fw.comparable(error))
    assert bodies[0] == bodies[1] == bodies[2], bodies


@pytest.mark.feature("files")
def test_an_idempotency_key_on_a_file_route_is_refused_with_400_naming_the_header(client):
    with pytest.raises(openai.BadRequestError) as caught:
        client.files.create(file=("k.txt", b"k\n", "text/plain"), purpose="user_data",
                            extra_headers={"Idempotency-Key": f"conformance-{_tag()}"})
    fw.sdk_error(caught.value, code="invalid_request_error", param="Idempotency-Key")


@pytest.mark.feature("files")
def test_another_projects_file_id_answers_exactly_like_a_file_id_that_never_existed(client, make_client, pytestconfig):
    other = make_client(api_key=_require_other_key(pytestconfig))  # before anything is created: a SKIP leaves nothing
    data = f"private to one project {_tag()}\n".encode()
    file_id = client.files.create(file=("private.txt", data, "text/plain"), purpose="user_data").id
    try:
        never = _random_file_id()
        for name, call in (
            ("retrieve", lambda fid: other.files.retrieve(fid)),
            ("content", lambda fid: other.files.content(fid)),
            ("delete", lambda fid: other.files.delete(fid)),
        ):
            foreign = fw.sdk_error(_status_error(lambda: call(file_id)), code="file_not_found")
            unknown = fw.sdk_error(_status_error(lambda: call(never)), code="file_not_found")
            assert fw.comparable(foreign) == fw.comparable(unknown), f"{name}: {foreign} != {unknown}"
        assert file_id not in {f.id for f in other.files.list(limit=10000).data}, "a foreign file is listed"
        assert _content(client, file_id) == data, "the owner lost the file to a foreign DELETE"
    finally:
        _delete_quietly(client, file_id)


@pytest.mark.feature("files")
def test_a_key_without_the_files_scopes_gets_the_same_403_for_a_real_and_a_random_file_id_on_every_file_route(client, make_client, target):
    limited = make_client(api_key=_require_limited_key(target))
    data = f"scoped {_tag()}\n".encode()
    file_id = client.files.create(file=("scoped.txt", data, "text/plain"), purpose="user_data").id
    try:
        fw.insufficient_scope(_status_error(lambda: limited.files.create(
            file=("x.txt", b"x\n", "text/plain"), purpose="user_data")), "files.write")
        fw.insufficient_scope(_status_error(lambda: limited.files.list()), "files.read")
        never = _random_file_id()
        for name, scope, call in (
            ("retrieve", "files.read", lambda fid: limited.files.retrieve(fid)),
            ("content", "files.read", lambda fid: limited.files.content(fid)),
            ("delete", "files.write", lambda fid: limited.files.delete(fid)),
        ):
            real = fw.insufficient_scope(_status_error(lambda: call(file_id)), scope)
            unknown = fw.insufficient_scope(_status_error(lambda: call(never)), scope)
            assert fw.comparable(real) == fw.comparable(unknown), f"{name}: a real id got another 403: {real} != {unknown}"
        assert _content(client, file_id) == data, "a refused DELETE removed the file"
    finally:
        _delete_quietly(client, file_id)


# ---------------------------------------------------------------- uploads --


@pytest.mark.feature("uploads")
def test_parts_carrying_their_sha256_complete_in_part_ids_order_with_md5_and_return_a_nested_file(client, raw):
    data = fw.text_payload(2 * PART + 10 * KIB, _tag())
    chunks = _chunks(data)
    upload = fw.assert_upload(
        client.uploads.create(bytes=len(data), filename="parts.txt", mime_type="text/plain", purpose="user_data"),
        status="pending", bytes=len(data),
    )
    file_id: Optional[str] = None
    try:
        assert upload["file"] is None and upload["filename"] == "parts.txt", upload
        ids: List[Optional[str]] = [None] * len(chunks)
        for index in (1, 2, 0):  # deliberately out of order
            part = fw.assert_part(
                client.uploads.parts.create(upload_id=upload["id"], data=chunks[index], extra_body={"sha256": _sha(chunks[index])}),
                upload_id=upload["id"], bytes=len(chunks[index]), sha256=_sha(chunks[index]),
            )
            ids[index] = part["id"]
        with pytest.raises(openai.BadRequestError) as caught:
            client.uploads.parts.create(upload_id=upload["id"], data=chunks[0], extra_body={"sha256": "0" * 64})
        fw.sdk_error(caught.value, code="checksum_mismatch", param="sha256", should_retry=False)

        done = fw.assert_upload(client.uploads.complete(upload_id=upload["id"], part_ids=ids, md5=_md5(data)), status="completed")
        nested = fw.assert_file(done["file"], bytes=len(data), filename="parts.txt", purpose="user_data")
        file_id = nested["id"]
        early = raw.get(f"files/{file_id}/content")
        if early.status_code != 200:  # the bytes may still be assembling: then it must say so, retryably
            fw.envelope(early.status_code, early.json(), early.headers, code="file_not_ready", should_retry=True)
            assert early.headers.get("retry-after"), "file_not_ready without Retry-After"
        finished = fw.assert_file(_wait(client, file_id), bytes=len(data), sha256=_sha(data))
        assert finished["status"] == "processed", finished
        assert _content(client, file_id) == data
    finally:
        _finish(client, upload["id"], file_id)


@pytest.mark.feature("uploads")
def test_the_sdk_upload_file_chunked_helper_uploads_a_path_and_bytes_and_wait_for_processing_reaches_processed(client, tmp_path):
    data = fw.text_payload(3 * PART + 1234, _tag())
    path = tmp_path / "helper.txt"
    path.write_bytes(data)
    uploads = []
    try:
        uploads.append((fw.assert_upload(
            client.uploads.upload_file_chunked(file=path, mime_type="text/plain", purpose="user_data", part_size=PART),
            status="completed", bytes=len(data),
        ), "helper.txt"))
        uploads.append((fw.assert_upload(
            client.uploads.upload_file_chunked(file=data, filename="memory.txt", bytes=len(data), mime_type="text/plain",
                                               purpose="user_data", part_size=PART, md5=_md5(data)),
            status="completed",
        ), "memory.txt"))
        for upload, name in uploads:
            nested = fw.assert_file(upload["file"], bytes=len(data), filename=name)
            done = fw.assert_file(_wait(client, nested["id"]), sha256=_sha(data))
            assert done["status"] == "processed", done
            assert _content(client, nested["id"]) == data
    finally:
        for upload, _name in uploads:
            _delete_quietly(client, (upload.get("file") or {}).get("id"))


@pytest.mark.feature("uploads")
def test_complete_with_parts_that_do_not_add_up_to_the_declared_bytes_is_400_and_the_upload_stays_open(client):
    data = fw.text_payload(PART, _tag())
    upload = client.uploads.create(bytes=len(data) + 1, filename="short.txt", mime_type="text/plain", purpose="user_data")
    file_id: Optional[str] = None
    try:
        first = client.uploads.parts.create(upload_id=upload.id, data=data)
        with pytest.raises(openai.BadRequestError) as caught:
            client.uploads.complete(upload_id=upload.id, part_ids=[first.id])
        fw.sdk_error(caught.value, code="invalid_request_error", param="part_ids")
        last = client.uploads.parts.create(upload_id=upload.id, data=b"\n")
        done = fw.assert_upload(client.uploads.complete(upload_id=upload.id, part_ids=[first.id, last.id]), status="completed")
        file_id = done["file"]["id"]
        assert done["file"]["bytes"] == len(data) + 1
    finally:
        _finish(client, upload.id, file_id)


@pytest.mark.feature("uploads")
def test_complete_with_a_wrong_md5_or_a_wrong_sha256_returns_the_file_which_then_ends_in_error_checksum_mismatch(client):
    for field, wrong in (("md5", "0" * 32), ("sha256", "0" * 64)):
        data = fw.text_payload(PART, _tag())
        upload = client.uploads.create(bytes=len(data), filename=f"{field}.txt", mime_type="text/plain", purpose="user_data")
        file_id: Optional[str] = None
        try:
            part = client.uploads.parts.create(upload_id=upload.id, data=data)
            done = fw.assert_upload(
                client.uploads.complete(upload_id=upload.id, part_ids=[part.id], extra_body={field: wrong}),
                status="completed",
            )
            file_id = fw.assert_file(done["file"], bytes=len(data))["id"]
            ended = fw.assert_file(_wait(client, file_id))
            assert ended["status"] == "error", f"a wrong {field} must fail the file, got {ended['status']}"
            assert (ended["processing"].get("error") or {}).get("code") == "checksum_mismatch", (field, ended["processing"])
            assert "checksum" in (ended["status_details"] or ""), ended["status_details"]
        finally:
            _finish(client, upload.id, file_id)


@pytest.mark.feature("uploads")
def test_a_replayed_complete_returns_the_same_upload_and_file_and_a_late_part_is_409_without_retry(client):
    data = fw.text_payload(PART // 2, _tag())
    upload = client.uploads.create(bytes=len(data), filename="replay.txt", mime_type="text/plain", purpose="user_data")
    file_id: Optional[str] = None
    try:
        part = client.uploads.parts.create(upload_id=upload.id, data=data)
        first = fw.assert_upload(client.uploads.complete(upload_id=upload.id, part_ids=[part.id]), status="completed")
        file_id = first["file"]["id"]
        again = fw.assert_upload(client.uploads.complete(upload_id=upload.id, part_ids=[part.id]), status="completed")
        assert (again["id"], again["file"]["id"]) == (first["id"], first["file"]["id"]), (first, again)
        with pytest.raises(openai.ConflictError) as caught:
            client.uploads.parts.create(upload_id=upload.id, data=b"late")
        fw.sdk_error(caught.value, code="upload_state_conflict", should_retry=False)
        with pytest.raises(openai.ConflictError) as cancel:
            client.uploads.cancel(upload.id)
        fw.sdk_error(cancel.value, code="upload_state_conflict")
    finally:
        _finish(client, upload.id, file_id)


@pytest.mark.feature("uploads")
def test_cancel_is_idempotent_and_a_cancelled_upload_refuses_parts_and_complete_with_409(client):
    upload = client.uploads.create(bytes=PART, filename="cancel.txt", mime_type="text/plain", purpose="user_data")
    try:
        part = client.uploads.parts.create(upload_id=upload.id, data=b"half of it\n")
        cancelled = fw.assert_upload(client.uploads.cancel(upload.id), status="cancelled")
        assert cancelled["file"] is None, cancelled
        fw.assert_upload(client.uploads.cancel(upload.id), status="cancelled")
        with pytest.raises(openai.ConflictError) as caught:
            client.uploads.parts.create(upload_id=upload.id, data=b"more")
        fw.sdk_error(caught.value, code="upload_state_conflict", should_retry=False)
        with pytest.raises(openai.ConflictError) as completing:
            client.uploads.complete(upload_id=upload.id, part_ids=[part.id])
        fw.sdk_error(completing.value, code="upload_state_conflict", should_retry=False)
    finally:
        _cancel_quietly(client, upload.id)


@pytest.mark.feature("uploads_resume")
@pytest.mark.feature("uploads")
def test_another_projects_upload_id_answers_retrieve_parts_put_complete_and_cancel_exactly_like_one_that_never_existed(client, make_client, pytestconfig):
    other = make_client(api_key=_require_other_key(pytestconfig))  # before anything is created: a SKIP leaves nothing
    data = fw.text_payload(KIB, _tag())
    upload = client.uploads.create(bytes=len(data), filename="mine.txt", mime_type="text/plain", purpose="user_data")
    file_id: Optional[str] = None
    try:
        # The owner's part goes in FIRST, so a foreign write has something to clobber.
        part = client.uploads.parts.create(upload_id=upload.id, data=data)
        never = _random_upload_id()
        for name, call in _upload_probes(other):
            foreign = fw.sdk_error(_status_error(lambda: call(upload.id)), code="upload_not_found")
            unknown = fw.sdk_error(_status_error(lambda: call(never)), code="upload_not_found")
            assert fw.comparable(foreign) == fw.comparable(unknown), f"{name}: {foreign} != {unknown}"
        done = fw.assert_upload(client.uploads.complete(upload_id=upload.id, part_ids=[part.id], md5=_md5(data)),
                                status="completed")
        file_id = done["file"]["id"]
        assert done["file"]["bytes"] == len(data), "the foreign calls changed the owner's upload"
        assert _wait(client, file_id)["status"] == "processed"
        assert _content(client, file_id) == data, "a foreign part write reached the owner's bytes"
    finally:
        _finish(client, upload.id, file_id)


@pytest.mark.feature("uploads_resume")
@pytest.mark.feature("uploads")
def test_a_key_without_files_write_gets_the_same_403_for_a_real_and_a_random_upload_id_on_every_upload_route(client, make_client, target):
    limited = make_client(api_key=_require_limited_key(target))
    data = fw.text_payload(KIB, _tag())
    upload = client.uploads.create(bytes=len(data), filename="scoped.txt", mime_type="text/plain", purpose="user_data")
    file_id: Optional[str] = None
    try:
        part = client.uploads.parts.create(upload_id=upload.id, data=data)
        fw.insufficient_scope(_status_error(lambda: limited.uploads.create(
            bytes=1, filename="x.txt", mime_type="text/plain", purpose="user_data")), "files.write")
        never = _random_upload_id()
        for name, call in _upload_probes(limited):
            real = fw.insufficient_scope(_status_error(lambda: call(upload.id)), "files.write")
            unknown = fw.insufficient_scope(_status_error(lambda: call(never)), "files.write")
            assert fw.comparable(real) == fw.comparable(unknown), f"{name}: a real id got another 403: {real} != {unknown}"
        done = fw.assert_upload(client.uploads.complete(upload_id=upload.id, part_ids=[part.id], md5=_md5(data)),
                                status="completed")
        file_id = done["file"]["id"]
        assert _wait(client, file_id)["status"] == "processed"
        assert _content(client, file_id) == data, "a refused call changed the owner's upload"
    finally:
        _finish(client, upload.id, file_id)


# ----------------------------------------------------------------- resume --


@pytest.mark.feature("uploads_resume")
def test_upload_file_chunked_rides_out_a_connection_dropped_mid_part_and_no_part_number_is_lost(target, client):
    data = fw.text_payload(3 * PART + 999, _tag())
    parts = len(_chunks(data))
    retrying, dropper = _dropping_client(target, cut=1, max_retries=2)
    upload: Optional[Dict[str, Any]] = None
    try:
        upload = fw.assert_upload(
            retrying.uploads.upload_file_chunked(file=data, filename="dropped.txt", bytes=len(data), mime_type="text/plain",
                                                 purpose="user_data", part_size=PART),
            status="completed",
        )
    finally:
        retrying.close()
        if upload is None:
            for upload_id in dropper.upload_ids:
                _cancel_quietly(client, upload_id)
    assert (dropper.cuts, dropper.parts_attempted) == (1, parts + 1), "the drop did not happen as arranged"
    file_id = upload["file"]["id"]
    try:
        numbers = [p["part_number"] for p in _get(client, f"/uploads/{upload['id']}")["parts"]]
        # Had the cut attempt been recorded, its retry would hold number 2 and
        # the kept parts would read 0, 2, 3, 4.
        assert numbers == list(range(parts)), f"part numbers {numbers}"
        assert _wait(client, file_id)["status"] == "processed"
        assert _content(client, file_id) == data
    finally:
        _delete_quietly(client, file_id)


@pytest.mark.feature("uploads_resume")
def test_a_chunked_upload_cut_by_a_dropped_connection_resumes_in_a_new_client_from_the_servers_part_list(target, make_client):
    data = fw.text_payload(3 * PART + 4321, _tag())
    chunks = _chunks(data)
    crashing, dropper = _dropping_client(target, cut=1, max_retries=0)
    try:
        with pytest.raises(openai.APIConnectionError):
            crashing.uploads.upload_file_chunked(file=data, filename="resumed.txt", bytes=len(data), mime_type="text/plain",
                                                 purpose="user_data", part_size=PART, md5=_md5(data))
    finally:
        crashing.close()  # the process "crashed": the helper never returned the upload
    assert dropper.cuts == 1 and len(dropper.upload_ids) == 1, (dropper.cuts, dropper.upload_ids)
    upload_id = dropper.upload_ids[0]

    fresh = make_client()
    file_id: Optional[str] = None
    try:
        state = fw.assert_upload(_get(fresh, f"/uploads/{upload_id}"), status="pending", bytes=len(data))
        held = state["parts"]
        assert [p["part_number"] for p in held] == [0], f"the server holds {held}: a cut part must record nothing"
        fw.assert_part(held[0], upload_id=upload_id, part_number=0, bytes=len(chunks[0]), sha256=_sha(chunks[0]))
        assert state["bytes_received"] == len(chunks[0]) and state["part_mode"] == "sequential", state
        part_ids = [held[0]["id"]]
        for chunk in chunks[1:]:
            part_ids.append(fw.assert_part(fresh.uploads.parts.create(upload_id=upload_id, data=chunk),
                                           upload_id=upload_id, sha256=_sha(chunk))["id"])
        done = fw.assert_upload(fresh.uploads.complete(upload_id=upload_id, part_ids=part_ids, md5=_md5(data)), status="completed")
        file_id = done["file"]["id"]
        assert _wait(fresh, file_id)["status"] == "processed"
        assert _content(fresh, file_id) == data
    finally:
        _finish(fresh, upload_id, file_id)


@pytest.mark.feature("uploads_resume")
def test_resending_a_part_number_replaces_that_part_under_the_same_part_id(client):
    data = fw.text_payload(PART, _tag())
    upload = client.uploads.create(bytes=len(data), filename="retry.txt", mime_type="text/plain", purpose="user_data")
    file_id: Optional[str] = None
    try:
        first = fw.assert_part(client.uploads.parts.create(upload_id=upload.id, data=fw.text_payload(PART, "stale"),
                                                           extra_body={"part_number": 0}), upload_id=upload.id, part_number=0)
        again = fw.assert_part(client.uploads.parts.create(upload_id=upload.id, data=data, extra_body={"part_number": 0}),
                               upload_id=upload.id, part_number=0, sha256=_sha(data))
        assert again["id"] == first["id"], "a lost-acknowledgement retry must not leave an orphan part"
        state = _get(client, f"/uploads/{upload.id}")
        assert [(p["id"], p["sha256"]) for p in state["parts"]] == [(first["id"], _sha(data))], state["parts"]
        with pytest.raises(openai.BadRequestError) as mixed:
            client.uploads.parts.create(upload_id=upload.id, data=b"unnumbered")
        fw.sdk_error(mixed.value, code="invalid_request_error", param="part_number")
        done = fw.assert_upload(client.uploads.complete(upload_id=upload.id, part_ids=[again["id"]], md5=_md5(data)), status="completed")
        file_id = done["file"]["id"]
        assert _wait(client, file_id)["status"] == "processed"
        assert _content(client, file_id) == data
    finally:
        _finish(client, upload.id, file_id)


@pytest.mark.feature("uploads_resume")
def test_raw_put_parts_with_a_part_digest_complete_without_part_ids_in_part_number_order(client):
    data = fw.text_payload(2 * PART + 77, _tag())
    chunks = _chunks(data)
    upload = client.uploads.create(bytes=len(data), filename="raw.txt", mime_type="text/plain", purpose="user_data")
    path = f"/uploads/{upload.id}/parts"
    file_id: Optional[str] = None
    try:
        for n in (2, 0):  # out of order, with the hex header
            fw.assert_part(
                client.put(f"{path}/{n}", content=chunks[n], cast_to=object, options={"headers": {
                    "Content-Type": "application/octet-stream", "X-Part-SHA256": _sha(chunks[n])}}),
                upload_id=upload.id, part_number=n, bytes=len(chunks[n]), sha256=_sha(chunks[n]),
            )
        with pytest.raises(openai.BadRequestError) as caught:
            client.put(f"{path}/1", content=chunks[1], cast_to=object, options={"headers": {
                "Content-Type": "application/octet-stream", "X-Part-SHA256": "f" * 64}})
        fw.sdk_error(caught.value, code="checksum_mismatch", param="sha256", should_retry=False)
        wrong_digest = base64.b64encode(hashlib.sha256(b"not these bytes").digest()).decode("ascii")
        with pytest.raises(openai.BadRequestError) as digest_caught:
            client.put(f"{path}/1", content=chunks[1], cast_to=object, options={"headers": {
                "Content-Type": "application/octet-stream", "Content-Digest": f"sha-256=:{wrong_digest}:"}})
        fw.sdk_error(digest_caught.value, code="checksum_mismatch", param="sha256", should_retry=False)
        assert [p["part_number"] for p in _get(client, f"/uploads/{upload.id}")["parts"]] == [0, 2], "a refused part was recorded"
        with pytest.raises(openai.BadRequestError) as gap:
            client.post(f"/uploads/{upload.id}/complete", body={}, cast_to=object)
        fw.sdk_error(gap.value, code="invalid_request_error", param="part_ids")
        digest = base64.b64encode(hashlib.sha256(chunks[1]).digest()).decode("ascii")
        fw.assert_part(
            client.put(f"{path}/1", content=chunks[1], cast_to=object, options={"headers": {
                "Content-Type": "application/octet-stream", "Content-Digest": f"sha-256=:{digest}:"}}),
            upload_id=upload.id, part_number=1, sha256=_sha(chunks[1]),
        )
        done = fw.assert_upload(client.post(f"/uploads/{upload.id}/complete", body={"sha256": _sha(data)}, cast_to=object),
                                status="completed")
        file_id = done["file"]["id"]
        assert _wait(client, file_id)["status"] == "processed"
        assert _content(client, file_id) == data
    finally:
        _finish(client, upload.id, file_id)


@pytest.mark.feature("uploads_resume")
def test_a_raw_part_sent_without_content_length_is_411_and_records_nothing(client, target):
    upload = client.uploads.create(bytes=KIB, filename="nolength.txt", mime_type="text/plain", purpose="user_data")
    try:
        def body():
            yield b"x" * KIB

        response = httpx.put(
            f"{target.base_url}/uploads/{upload.id}/parts/0", content=body(), timeout=60.0,
            headers={"Authorization": f"Bearer {target.api_key}", "Content-Type": "application/octet-stream"},
        )
        assert "content-length" not in response.request.headers, "httpx sent a length, so this proves nothing"
        # The orchestrator's form or the gateway's edge form, pinned by
        # TECHSARA_FILES_EDGE_REFUSALS when set (files_wire.pre_body_refusal).
        fw.length_required(response.status_code, response.json(), response.headers)
        assert _get(client, f"/uploads/{upload.id}")["parts"] == []
    finally:
        _cancel_quietly(client, upload.id)


@pytest.mark.feature("uploads_resume")
def test_a_raw_part_declaring_more_bytes_than_a_part_may_hold_is_413_before_any_byte_is_sent_and_records_nothing(client, target):
    import http.client
    from urllib.parse import urlsplit

    upload = client.uploads.create(bytes=KIB, filename="huge.txt", mime_type="text/plain", purpose="user_data")
    try:
        url = urlsplit(target.base_url)
        connection_class = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        connection = connection_class(url.hostname, url.port, timeout=30)
        try:
            # Headers only: 1 TiB declared, not one body byte sent. A handler
            # that reads before checking the declared length never answers:
            # the 30 s socket timeout turns that into a failure, not a hang.
            connection.putrequest("PUT", f"{url.path.rstrip('/')}/uploads/{upload.id}/parts/0")
            for name, value in (("Authorization", f"Bearer {target.api_key}"), ("Content-Type", "application/octet-stream"),
                                ("Content-Length", str(1 << 40))):
                connection.putheader(name, value)
            connection.endheaders()
            try:
                response = connection.getresponse()
            except OSError as exc:
                pytest.fail(f"the server did not refuse a part declaring 1 TiB before its body: {exc!r}")
            status, headers, body = response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()
        fw.part_too_large(status, json.loads(body), headers)
        assert _get(client, f"/uploads/{upload.id}")["parts"] == []
    finally:
        _cancel_quietly(client, upload.id)


# ---------------------------------------------------------- model input --


def _responses_file_input(question: str, part: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"role": "user", "content": [{"type": "input_text", "text": question}, part]}]


def _chat_file_input(question: str, part: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"role": "user", "content": [{"type": "text", "text": question}, part]}]


@pytest.mark.feature("files_model_input")
def test_an_uploaded_text_file_is_read_by_the_model_as_input_file_on_responses_in_auto_and_full_file_context(client, target):
    codeword = f"zebra-{uuid.uuid4().hex[:8]}"
    created = client.files.create(file=("memo.txt", f"The launch codeword is {codeword}.\n".encode(), "text/plain"),
                                  purpose="user_data")
    try:
        assert _wait(client, created.id)["status"] == "processed"
        question = "What is the launch codeword in the file? Reply with the codeword only."
        part = {"type": "input_file", "file_id": created.id}
        response = client.responses.create(model=target.models["chat"], input=_responses_file_input(question, part),
                                           max_output_tokens=32, temperature=0)
        assert response.status == "completed" and codeword in response.output_text, response.output_text
        full = client.responses.create(model=target.models["chat"], input=_responses_file_input(question, part),
                                       max_output_tokens=32, temperature=0, extra_body={"file_context": {"mode": "full"}})
        assert codeword in full.output_text, f"file_context full: {full.output_text!r}"
        with pytest.raises(openai.BadRequestError) as caught:
            client.responses.create(model=target.models["chat"], input=_responses_file_input(question, part),
                                    max_output_tokens=32, extra_body={"file_context": {"mode": "everything"}})
        fw.sdk_error(caught.value, code="invalid_request_error", param="file_context.mode")
    finally:
        _delete_quietly(client, created.id)


@pytest.mark.feature("files_model_input")
def test_an_uploaded_text_file_is_read_by_the_model_as_a_file_part_on_chat_completions(client, target):
    codeword = f"heron-{uuid.uuid4().hex[:8]}"
    created = client.files.create(file=("minutes.txt", f"The meeting codeword is {codeword}.\n".encode(), "text/plain"),
                                  purpose="user_data")
    try:
        assert _wait(client, created.id)["status"] == "processed"
        completion = client.chat.completions.create(
            model=target.models["chat"], max_tokens=32, temperature=0,
            messages=_chat_file_input("What is the meeting codeword in the file? Reply with the codeword only.",
                                      {"type": "file", "file": {"file_id": created.id}}),
        )
        content = completion.choices[0].message.content or ""
        assert codeword in content, content
    finally:
        _delete_quietly(client, created.id)


@pytest.mark.feature("files_model_input")
def test_inline_file_data_text_is_read_by_the_model_on_both_routes_without_an_upload(client, target):
    codeword = f"otter-{uuid.uuid4().hex[:8]}"
    data_url = _data_url(f"The inline codeword is {codeword}.\n".encode(), "text/plain")
    question = "What is the inline codeword in the file? Reply with the codeword only."
    response = client.responses.create(
        model=target.models["chat"], max_output_tokens=32, temperature=0,
        input=_responses_file_input(question, {"type": "input_file", "filename": "inline.txt", "file_data": data_url}),
    )
    assert response.status == "completed" and codeword in response.output_text, response.output_text
    completion = client.chat.completions.create(
        model=target.models["chat"], max_tokens=32, temperature=0,
        messages=_chat_file_input(question, {"type": "file", "file": {"filename": "inline.txt", "file_data": data_url}}),
    )
    assert codeword in (completion.choices[0].message.content or ""), completion.choices[0].message.content


@pytest.mark.feature("files_model_input")
def test_an_uploaded_png_is_seen_by_the_model_as_input_image_with_its_file_id(client, target):
    created = client.files.create(file=("red.png", media.solid_png(rgb=(220, 20, 20)), "image/png"), purpose="vision")
    try:
        done = _wait(client, created.id)
        assert done["status"] == "processed" and done["processing"]["kind"] == "image", done["processing"]
        response = client.responses.create(
            model=target.models["chat"],
            input=_responses_file_input("What single colour fills this image? Answer with one word.",
                                        {"type": "input_image", "file_id": created.id, "detail": "auto"}),
            max_output_tokens=target.small_output_tokens, temperature=0,
        )
        assert "red" in response.output_text.lower(), f"{target.models['chat']} answered {response.output_text!r}"
    finally:
        _delete_quietly(client, created.id)


@pytest.mark.feature("files_model_input")
def test_an_uploaded_wav_is_processed_as_audio_and_the_model_reads_its_length_from_it_as_input_file_and_input_video(client, target):
    created = client.files.create(file=("tone.wav", media.tone_wav(seconds=float(TONE_SECONDS)), "audio/wav"),
                                  purpose="user_data")
    try:
        done = _wait(client, created.id)
        assert done["status"] == "processed", (done["status"], done["status_details"])
        processing = done["processing"]
        assert processing["kind"] == "audio" and abs(float(processing["facts"].get("duration_s", 0)) - TONE_SECONDS) < 0.2, processing
        question = "How many seconds long is the recording in the attached file? Reply with the number only."
        bare = client.responses.create(model=target.models["chat"], max_output_tokens=32, temperature=0,
                                       input=[{"role": "user", "content": [{"type": "input_text", "text": question}]}])
        assert not TONE_SECONDS_RE.search(bare.output_text), (
            f"without the file the model already answered {bare.output_text!r}: the check below would prove nothing"
        )
        for part_type in ("input_file", "input_video"):
            response = client.responses.create(
                model=target.models["chat"], max_output_tokens=32, temperature=0,
                input=_responses_file_input(question, {"type": part_type, "file_id": created.id}),
            )
            assert response.status == "completed", response
            assert TONE_SECONDS_RE.search(response.output_text), (
                f"{part_type}: asked the length of a {TONE_SECONDS} s recording, {target.models['chat']} answered "
                f"{response.output_text!r}; the recording's block did not reach the model"
            )
    finally:
        _delete_quietly(client, created.id)


@pytest.mark.feature("files_model_input")
def test_input_audio_on_chat_completions_refuses_an_unknown_format_and_puts_a_wav_clips_transcript_into_the_prompt(client, target):
    clip = base64.b64encode(media.tone_wav(seconds=2.0)).decode("ascii")
    with pytest.raises(openai.BadRequestError) as caught:
        client.chat.completions.create(
            model=target.models["chat"], max_tokens=target.small_output_tokens,
            messages=[{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": clip, "format": "ogg"}}]}],
        )
    fw.sdk_error(caught.value, code="invalid_request_error", param="messages.0.content.0.input_audio.format")
    question = "Describe what can be heard in this recording in one sentence."
    bare = client.chat.completions.create(model=target.models["chat"], max_tokens=target.small_output_tokens,
                                          messages=[{"role": "user", "content": [{"type": "text", "text": question}]}])
    completion = client.chat.completions.create(
        model=target.models["chat"], max_tokens=target.small_output_tokens,
        messages=_chat_file_input(question, {"type": "input_audio", "input_audio": {"data": clip, "format": "wav"}}),
    )
    content = completion.choices[0].message.content
    assert isinstance(content, str) and content.strip(), completion
    grew = _input_tokens(completion.usage, "prompt_tokens") - _input_tokens(bare.usage, "prompt_tokens")
    assert grew >= MIN_MEDIA_BLOCK_TOKENS, (
        f"the prompt grew by {grew} tokens over the bare question; the clip's transcript did not reach the model"
    )


@pytest.mark.feature("uploads")
@pytest.mark.feature("files_model_input")
def test_a_failed_file_and_a_file_of_the_wrong_kind_are_refused_as_model_input_with_400_naming_the_file_id(client, target):
    data = fw.text_payload(KIB, _tag())
    upload = client.uploads.create(bytes=len(data), filename="broken.txt", mime_type="text/plain", purpose="user_data")
    failed_id: Optional[str] = None
    text_id: Optional[str] = None
    try:
        part = client.uploads.parts.create(upload_id=upload.id, data=data)
        failed_id = client.uploads.complete(upload_id=upload.id, part_ids=[part.id], md5="0" * 32).file.id
        ended = _wait(client, failed_id)
        assert ended["status"] == "error" and ended["processing"]["error"]["code"] == "checksum_mismatch", ended["processing"]
        text_id = client.files.create(file=("plain.txt", b"just some text\n", "text/plain"), purpose="user_data").id
        assert _wait(client, text_id)["status"] == "processed"

        refusals = (
            ("responses input_file", "input.0.content.1.file_id", lambda: client.responses.create(
                model=target.models["chat"], max_output_tokens=target.small_output_tokens,
                input=_responses_file_input("Summarise.", {"type": "input_file", "file_id": failed_id}))),
            ("chat file", "messages.0.content.1.file.file_id", lambda: client.chat.completions.create(
                model=target.models["chat"], max_tokens=target.small_output_tokens,
                messages=_chat_file_input("Summarise.", {"type": "file", "file": {"file_id": failed_id}}))),
        )
        for name, param, call in refusals:
            exc = _status_error(call)
            error = fw.sdk_error(exc, code="invalid_request_error", param=param)
            assert exc.status_code == 400 and error["message"] == CHECKSUM_SENTENCE, f"{name}: HTTP {exc.status_code} {error}"
        for part_type in ("input_image", "input_video"):
            exc = _status_error(lambda: client.responses.create(
                model=target.models["chat"], max_output_tokens=target.small_output_tokens,
                input=_responses_file_input("Describe it.", {"type": part_type, "file_id": text_id})))
            error = fw.sdk_error(exc, code="invalid_request_error", param="input.0.content.1.file_id")
            assert exc.status_code == 400 and part_type in error["message"], error
    finally:
        _delete_quietly(client, text_id)
        _finish(client, upload.id, failed_id)


@pytest.mark.feature("files_model_input")
def test_another_projects_file_id_as_model_input_is_the_same_404_as_an_id_that_never_existed_on_both_routes(client, make_client, target, pytestconfig):
    other = make_client(api_key=_require_other_key(pytestconfig))  # before anything is created: a SKIP leaves nothing
    created = client.files.create(file=("secret.txt", f"secret-{uuid.uuid4().hex[:8]}\n".encode(), "text/plain"),
                                  purpose="user_data")
    try:
        assert _wait(client, created.id)["status"] == "processed"
        for dialect, param, call in (
            ("responses", "input.0.content.1.file_id", lambda fid: other.responses.create(
                model=target.models["chat"], max_output_tokens=target.small_output_tokens,
                input=_responses_file_input("Repeat the file.", {"type": "input_file", "file_id": fid}))),
            ("chat.completions", "messages.0.content.1.file.file_id", lambda fid: other.chat.completions.create(
                model=target.models["chat"], max_tokens=target.small_output_tokens,
                messages=_chat_file_input("Repeat the file.", {"type": "file", "file": {"file_id": fid}}))),
        ):
            bodies = []
            for file_id in (created.id, _random_file_id()):
                exc = _status_error(lambda: call(file_id))
                bodies.append(fw.comparable(fw.sdk_error(exc, code="file_not_found", param=param)))
            assert bodies[0] == bodies[1], f"{dialect}: {bodies}"
    finally:
        _delete_quietly(client, created.id)


@pytest.mark.feature("files_model_input")
def test_a_key_without_files_read_gets_the_same_403_for_a_real_and_a_random_file_id_in_a_prompt_but_may_send_file_data(client, make_client, target, pytestconfig):
    narrow = make_client(api_key=_require_responses_only_key(pytestconfig))  # before anything is created
    created = client.files.create(file=("scoped-input.txt", f"kestrel-{uuid.uuid4().hex[:8]}\n".encode(), "text/plain"),
                                  purpose="user_data")
    try:
        assert _wait(client, created.id)["status"] == "processed"
        for dialect, call in (
            ("responses", lambda fid: narrow.responses.create(
                model=target.models["chat"], max_output_tokens=target.small_output_tokens,
                input=_responses_file_input("Repeat the file.", {"type": "input_file", "file_id": fid}))),
            ("chat.completions", lambda fid: narrow.chat.completions.create(
                model=target.models["chat"], max_tokens=target.small_output_tokens,
                messages=_chat_file_input("Repeat the file.", {"type": "file", "file": {"file_id": fid}}))),
        ):
            real = fw.insufficient_scope(_status_error(lambda: call(created.id)), "files.read")
            unknown = fw.insufficient_scope(_status_error(lambda: call(_random_file_id())), "files.read")
            assert fw.comparable(real) == fw.comparable(unknown), f"{dialect}: a real id got another 403: {real} != {unknown}"
        # Inline bytes are the caller's own: no file scope (service.required_scope).
        inline = narrow.responses.create(
            model=target.models["chat"], max_output_tokens=target.small_output_tokens,
            input=_responses_file_input("Summarise.", {"type": "input_file", "filename": "own.txt",
                                                       "file_data": _data_url(b"my own words\n", "text/plain")}),
        )
        assert inline.status == "completed", inline
    finally:
        _delete_quietly(client, created.id)


@pytest.mark.feature("files_model_input")
def test_input_file_with_a_file_url_is_refused_and_never_fetched(client, target):
    with pytest.raises(openai.BadRequestError) as caught:
        client.responses.create(
            model=target.models["chat"], max_output_tokens=target.small_output_tokens,
            input=_responses_file_input("Summarise.", {"type": "input_file", "file_url": "http://127.0.0.1:9/private.pdf"}),
        )
    fw.sdk_error(caught.value, code="invalid_request_error", param="input.0.content.1.file_url")
