"""The official SDKs against the real handlers (design §3.4, §13.4).

openai-python and openai-node run in SUBPROCESSES against a real uvicorn
serving `create_files_router` with stub auth, on loopback. Nothing is mocked
between the SDK and the handlers: the SDK builds its multipart bodies, its
paginator sends `after`, `upload_file_chunked` sends 64 MiB parts, and the
File object it parses is the one the handler rendered.

WHERE THE SDKS COME FROM. They are not dependencies of the orchestrator, so
the test needs a directory with both installed, named by
TECHSARA_SDK_PARITY_DIR:

    <dir>/venv/bin/python          with openai==3.13.0
    <dir>/node/node_modules/openai openai@6.49.0

Without it the test is skipped WITH that reason (never silently passed).

PROCESSING IS A STUB HERE. Team B's jobs do not exist in this wave, so a
queue hook marks each new blob `processed` the moment its bytes land — enough
for `files.wait_for_processing` / `waitForProcessing` to see a terminal status
and prove the waiters work on `upload_file_chunked(...).file.id` (finding #1).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

from app.apifiles import queue, schema
from tests.apifiles_test_support import (  # noqa: F401 - fixture by name
    StubAuth,
    build_app,
    isolated_app_db,
    make_caller,
    serve,
)

SDK_DIR = os.environ.get("TECHSARA_SDK_PARITY_DIR", "")
PYTHON = os.path.join(SDK_DIR, "venv", "bin", "python") if SDK_DIR else ""
NODE_MODULES = os.path.join(SDK_DIR, "node", "node_modules") if SDK_DIR else ""

pytestmark = pytest.mark.skipif(
    not (SDK_DIR and os.path.exists(PYTHON) and os.path.isdir(os.path.join(NODE_MODULES, "openai"))),
    reason="set TECHSARA_SDK_PARITY_DIR to a directory with venv/ (openai==3.13.0) and node/node_modules/openai (6.49.0)",
)

MIB = 1024 * 1024

PY_SCRIPT = r'''
import hashlib, json, os, pathlib, time
import openai
from openai import OpenAI

MIB = 1024 * 1024
work = pathlib.Path(os.environ["WORK"])
client = OpenAI(base_url=os.environ["BASE"], api_key=os.environ["KEY"], max_retries=0)
out = {"sdk": openai.__version__}

doc = work / "doc.pdf"
doc.write_bytes(b"%PDF-1.4 " + bytes(range(256)) * 400)
created = client.files.create(file=doc, purpose="user_data", expires_after={"anchor": "created_at", "seconds": 3600})
out["create"] = {"id": created.id, "status": created.status, "bytes": created.bytes, "filename": created.filename,
                 "expires_in": created.expires_at - created.created_at, "mime_type": getattr(created, "mime_type", None)}
second = client.files.create(file=("notes.txt", b"hello from python", "text/plain"), purpose="assistants")

out["list_ids"] = [f.id for f in client.files.list(limit=1)]          # auto-pagination, one per page
out["list_asc_first"] = client.files.list(order="asc", limit=10).data[0].id
got = client.files.retrieve(created.id)
out["retrieve"] = {"filename": got.filename, "bytes": got.bytes, "sha256": getattr(got, "sha256", None)}
out["content_sha256"] = hashlib.sha256(client.files.content(created.id).read()).hexdigest()
with client.files.with_streaming_response.content(created.id) as response:
    response.stream_to_file(work / "download.bin")
out["stream_sha256"] = hashlib.sha256((work / "download.bin").read_bytes()).hexdigest()

big = work / "big.bin"
with open(big, "wb") as fh:
    for i in range(70):
        fh.write(hashlib.shake_256(i.to_bytes(4, "big")).digest(MIB))
big_sha = hashlib.sha256(big.read_bytes()).hexdigest()
t0 = time.monotonic()
up = client.uploads.upload_file_chunked(file=big, mime_type="application/octet-stream", purpose="user_data")
out["chunked"] = {"status": up.status, "file_id": up.file.id if up.file else None, "seconds": round(time.monotonic() - t0, 2)}
waited = client.files.wait_for_processing(up.file.id, poll_interval=0.2, max_wait_seconds=120)
out["chunked"]["waited_status"] = waited.status
out["chunked"]["sha256_matches"] = getattr(waited, "sha256", None) == big_sha
out["chunked"]["bytes"] = waited.bytes

small = b"in-memory chunked " * 1000
small_up = client.uploads.upload_file_chunked(file=small, filename="mem.txt", bytes=len(small), mime_type="text/plain",
                                              purpose="user_data", part_size=4096, md5=hashlib.md5(small).hexdigest())
done_small = client.files.wait_for_processing(small_up.file.id, poll_interval=0.2, max_wait_seconds=120)
out["in_memory_chunked"] = {"status": done_small.status, "content_ok": client.files.content(small_up.file.id).read() == small}

upload = client.uploads.create(bytes=10, filename="parts.txt", mime_type="text/plain", purpose="user_data")
p1 = client.uploads.parts.create(upload_id=upload.id, data=b"hello")
p2 = client.uploads.parts.create(upload_id=upload.id, data=b"world")
done = client.uploads.complete(upload_id=upload.id, part_ids=[p2.id, p1.id], md5=hashlib.md5(b"worldhello").hexdigest())
out["manual"] = {"status": done.status, "file": bool(done.file)}
ready = client.files.wait_for_processing(done.file.id, poll_interval=0.2, max_wait_seconds=60)
out["manual"]["content"] = client.files.content(ready.id).read().decode()

resumable = client.post("/uploads", body={"bytes": 6, "filename": "r.bin", "mime_type": "application/octet-stream", "purpose": "user_data"}, cast_to=dict)
for n, chunk in enumerate([b"abc", b"def"]):
    client.put(f"/uploads/{resumable['id']}/parts/{n}", content=chunk, cast_to=dict,
               options={"headers": {"X-Part-SHA256": hashlib.sha256(chunk).hexdigest(), "Content-Type": "application/octet-stream"}})
state = client.get(f"/uploads/{resumable['id']}", cast_to=dict)
out["resume_view_parts"] = [p["part_number"] for p in state["parts"]]
finished = client.post(f"/uploads/{resumable['id']}/complete", body={"sha256": hashlib.sha256(b"abcdef").hexdigest()}, cast_to=dict)
out["raw_complete_file"] = bool(finished["file"]["id"])

to_cancel = client.uploads.create(bytes=5, filename="c.txt", mime_type="text/plain", purpose="user_data")
out["cancel"] = client.uploads.cancel(to_cancel.id).status

deleted = client.files.delete(created.id)
out["delete"] = {"id": deleted.id, "deleted": deleted.deleted}
try:
    client.files.retrieve(created.id)
    out["after_delete"] = "found"
except openai.NotFoundError as exc:
    out["after_delete"] = exc.body.get("code") if isinstance(exc.body, dict) else str(exc.body)
try:
    client.uploads.parts.create(upload_id=upload.id, data=b"late")
    out["late_part"] = "accepted"
except openai.ConflictError as exc:
    out["late_part"] = [exc.status_code, exc.response.headers.get("x-should-retry")]
out["second_id"] = second.id
print(json.dumps(out))
'''

NODE_SCRIPT = r'''
const OpenAI = require('openai');
const { toFile, NotFoundError } = require('openai');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

(async () => {
  const MIB = 1024 * 1024;
  const work = process.env.WORK;
  const client = new OpenAI({ baseURL: process.env.BASE, apiKey: process.env.KEY, maxRetries: 0 });
  const out = { sdk: require('openai/version').VERSION };
  const sha = (buf) => crypto.createHash('sha256').update(buf).digest('hex');

  const docPath = path.join(work, 'node-doc.txt');
  fs.writeFileSync(docPath, 'node stream body '.repeat(5000));
  const created = await client.files.create({
    file: fs.createReadStream(docPath), purpose: 'user_data', expires_after: { anchor: 'created_at', seconds: 7200 },
  });
  out.create = { id: created.id, status: created.status, bytes: created.bytes, filename: created.filename,
                 expires_in: created.expires_at - created.created_at };
  const second = await client.files.create({ file: await toFile(Buffer.from('hello from node'), 'b.txt'), purpose: 'user_data' });

  const ids = [];
  for await (const f of client.files.list({ limit: 1 })) ids.push(f.id);
  out.list_ids = ids;
  const got = await client.files.retrieve(created.id);
  out.retrieve = { filename: got.filename, bytes: got.bytes, sha256: got.sha256 };
  const content = Buffer.from(await (await client.files.content(created.id)).arrayBuffer());
  out.content_sha256 = sha(content);

  const total = 20 * MIB + 12345;
  const big = Buffer.alloc(total);
  for (let o = 0; o < total; o += 32) big.writeUInt32LE((o * 2654435761) >>> 0, Math.min(o, total - 4));
  const bigSha = sha(big);
  const PART = 8 * MIB;
  const up = await client.uploads.create({ bytes: total, filename: 'node-big.bin', mime_type: 'application/octet-stream', purpose: 'user_data' });
  const partIds = [];
  for (let o = 0; o < total; o += PART) {
    const part = await client.uploads.parts.create(up.id, { data: await toFile(big.subarray(o, Math.min(o + PART, total)), 'part') });
    partIds.push(part.id);
  }
  const md5 = crypto.createHash('md5').update(big).digest('hex');
  const done = await client.uploads.complete(up.id, { part_ids: partIds, md5 });
  out.chunked = { status: done.status, file_id: done.file && done.file.id, parts: partIds.length };
  const waited = await client.files.waitForProcessing(done.file.id, { pollInterval: 200, maxWait: 120000 });
  out.chunked.waited_status = waited.status;
  out.chunked.sha256_matches = waited.sha256 === bigSha;

  const raw = await client.uploads.create({ bytes: total, filename: 'node-raw.bin', mime_type: 'application/octet-stream', purpose: 'user_data' });
  for (let n = 0, o = 0; o < total; n++, o += PART) {
    const buf = big.subarray(o, Math.min(o + PART, total));
    await client.put(`/uploads/${raw.id}/parts/${n}`, { body: buf, headers: { 'Content-Type': 'application/octet-stream', 'X-Part-SHA256': sha(buf) } });
  }
  const rawDone = await client.uploads.complete(raw.id, {});
  const rawReady = await client.files.waitForProcessing(rawDone.file.id, { pollInterval: 200, maxWait: 120000 });
  const rawBytes = Buffer.from(await (await client.files.content(rawReady.id)).arrayBuffer());
  out.raw_put = { status: rawReady.status, content_ok: sha(rawBytes) === bigSha };

  const cancelled = await client.uploads.cancel((await client.uploads.create({ bytes: 3, filename: 'c', mime_type: 'text/plain', purpose: 'user_data' })).id);
  out.cancel = cancelled.status;
  const deleted = await client.files.delete(created.id);
  out.delete = { id: deleted.id, deleted: deleted.deleted };
  try {
    await client.files.retrieve(created.id);
    out.after_delete = 'found';
  } catch (err) {
    out.after_delete = err instanceof NotFoundError ? err.error && err.error.code : String(err);
  }
  out.second_id = second.id;
  console.log(JSON.stringify(out));
})().catch((err) => { console.error(err); process.exit(1); });
'''


@pytest.fixture()
def server():
    caller = make_caller()
    marked = []

    def stub_processor(blob):
        # Stands in for team B: the blob is processed the moment it exists.
        schema.update_api_file_blob(blob["id"], status="processed", stage="finalize")
        marked.append(blob["id"])

    queue.set_enqueue_hook(stub_processor)
    try:
        with serve(build_app(StubAuth(caller)), project_ids=[caller.project_id]) as base:
            yield base, caller, marked
    finally:
        queue.set_enqueue_hook(None)


def _run(command, *, env, cwd, timeout=300):
    completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    assert completed.returncode == 0, f"stdout:\n{completed.stdout[-4000:]}\nstderr:\n{completed.stderr[-4000:]}"
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_openai_python_creates_lists_retrieves_downloads_uploads_in_chunks_and_deletes(server, tmp_path):
    base, caller, marked = server
    script = tmp_path / "parity.py"
    script.write_text(PY_SCRIPT)
    env = {**os.environ, "BASE": f"{base}/v1", "KEY": caller.token, "WORK": str(tmp_path)}
    out = _run([PYTHON, str(script)], env=env, cwd=str(tmp_path))
    print("openai-python result:", json.dumps(out))
    doc = b"%PDF-1.4 " + bytes(range(256)) * 400
    assert out["sdk"] == "3.13.0"
    assert out["create"] == {"id": out["create"]["id"], "status": "uploaded", "bytes": len(doc), "filename": "doc.pdf",
                             "expires_in": 3600, "mime_type": "application/pdf"}
    assert out["create"]["id"] in out["list_ids"] and out["second_id"] in out["list_ids"]
    assert out["retrieve"]["sha256"] == hashlib.sha256(doc).hexdigest() == out["content_sha256"] == out["stream_sha256"]
    assert out["chunked"]["status"] == "completed" and out["chunked"]["file_id"]
    assert out["chunked"]["waited_status"] == "processed" and out["chunked"]["sha256_matches"] is True
    assert out["chunked"]["bytes"] == 70 * MIB
    assert out["in_memory_chunked"] == {"status": "processed", "content_ok": True}
    assert out["manual"] == {"status": "completed", "file": True, "content": "worldhello"}
    assert out["resume_view_parts"] == [0, 1] and out["raw_complete_file"] is True
    assert out["cancel"] == "cancelled"
    assert out["delete"] == {"id": out["create"]["id"], "deleted": True}
    assert out["after_delete"] == "file_not_found"
    assert out["late_part"] == [409, "false"]
    assert marked, "the stub processor saw the blobs"


def test_openai_node_creates_lists_retrieves_downloads_uploads_in_parts_and_deletes(server, tmp_path):
    base, caller, _marked = server
    script = tmp_path / "parity.cjs"
    script.write_text(NODE_SCRIPT)
    env = {**os.environ, "BASE": f"{base}/v1", "KEY": caller.token, "WORK": str(tmp_path), "NODE_PATH": NODE_MODULES}
    out = _run(["node", str(script)], env=env, cwd=str(tmp_path))
    print("openai-node result:", json.dumps(out))
    assert out["sdk"] == "6.49.0"
    body = ("node stream body " * 5000).encode()
    assert out["create"]["status"] == "uploaded" and out["create"]["bytes"] == len(body)
    assert out["create"]["filename"] == "node-doc.txt" and out["create"]["expires_in"] == 7200
    assert out["create"]["id"] in out["list_ids"] and out["second_id"] in out["list_ids"]
    assert out["retrieve"]["sha256"] == hashlib.sha256(body).hexdigest() == out["content_sha256"]
    assert out["chunked"]["status"] == "completed" and out["chunked"]["parts"] == 3
    assert out["chunked"]["waited_status"] == "processed" and out["chunked"]["sha256_matches"] is True
    assert out["raw_put"] == {"status": "processed", "content_ok": True}
    assert out["cancel"] == "cancelled"
    assert out["delete"] == {"id": out["create"]["id"], "deleted": True}
    assert out["after_delete"] == "file_not_found"
