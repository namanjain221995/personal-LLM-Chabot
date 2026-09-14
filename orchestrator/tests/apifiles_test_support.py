"""Shared fixtures and helpers for the Files API tests (2026-09-13).

WHY THESE TESTS OVERRIDE `isolated_app_db`. The suite-wide fixture TRUNCATEs
~60 tables before every test. On the shared throwaway Postgres (fsync ON as of
2026-09-13) that TRUNCATE is the slow part of a test by far, and these tests do
not need it: every test makes its OWN workspace and project with random ids,
and every Files API row hangs off a project, so tests cannot see each other's
rows. Importing `isolated_app_db` from here into a test module replaces the
conftest fixture for that module only (pytest resolves a fixture name to the
closest definition).

Point the run at a private database, e.g.
    TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/test_files_storage_uploads
"""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from app import db
from app.apifiles import schema
from app.config import settings
from app.publicapi import errors

_SCHEMA_READY = {"done": False}


@pytest.fixture()
def isolated_app_db(app_database, tmp_path, monkeypatch):
    """No TRUNCATE (see the module docstring); the files tree and the video
    tree are per test; the Files API tables exist."""
    monkeypatch.setattr(settings, "app_database_url", app_database)
    monkeypatch.setattr(settings, "session_secret_file", str(tmp_path / "appdb" / ".session_secret"))
    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "api-files"))
    # The 250 GiB production watermark is a fact about the box, not the test:
    # a CI runner has far less free, and every write would be refused.
    # Tests of the watermark itself patch `storage.free_bytes` against it.
    monkeypatch.setenv("PUBLIC_API_FILES_MIN_FREE_GIB", "0")
    if not _SCHEMA_READY["done"]:
        schema.ensure_schema()
        _SCHEMA_READY["done"] = True
    yield


def make_workspace() -> str:
    workspace_id = f"ws_files_{secrets.token_hex(8)}"
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", (workspace_id, workspace_id))
    return workspace_id


def make_caller(workspace_id: Optional[str] = None, *, scopes=("files.read", "files.write")) -> SimpleNamespace:
    """A real project row (the FKs need it) and a caller shaped like
    `ApiCaller` as far as the file routes read it."""
    workspace = workspace_id or make_workspace()
    project = db.create_api_project(workspace, f"files-{secrets.token_hex(6)}")
    return SimpleNamespace(
        project_id=project["id"],
        workspace_id=workspace,
        key_id=None,
        scopes=frozenset(scopes),
        token=f"tk_{secrets.token_hex(12)}",
    )


class StubAuth:
    """Bearer token → caller. Unknown or missing → the real 401 envelope."""

    def __init__(self, *callers: SimpleNamespace) -> None:
        self.by_token: Dict[str, SimpleNamespace] = {c.token: c for c in callers}
        self.admitted: List[str] = []

    def add(self, caller: SimpleNamespace) -> None:
        self.by_token[caller.token] = caller

    async def resolve(self, request) -> SimpleNamespace:
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        caller = self.by_token.get(token)
        if caller is None:
            raise errors.invalid_api_key()
        return caller

    async def authorize(self, request, caller, scope: str) -> None:
        if scope not in caller.scopes:
            raise errors.insufficient_scope(scope)

    async def admit(self, request, caller, kind: str) -> Any:
        self.admitted.append(kind)
        return None


def headers_for(caller: SimpleNamespace, **extra: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {caller.token}", **extra}


def build_app(auth: StubAuth, *, assembler=None, usage: Optional[list] = None, **deps_kwargs):
    from fastapi import FastAPI

    from app.publicapi.files import routes

    async def record(item) -> None:
        if usage is not None:
            usage.append(item)

    deps = routes.FilesDependencies(
        resolve_caller=auth.resolve,
        authorize=auth.authorize,
        admit=auth.admit,
        record_usage=record,
        assembler=assembler,
        **deps_kwargs,
    )
    app = FastAPI()
    app.include_router(routes.create_files_router(deps))
    app.state.files_deps = deps
    return app


def assemble_all(*callers) -> list:
    """Run every due assembly of `callers`' projects now, synchronously (no
    runner, deterministic). Scoped: tests share one database, and another
    test's completed-but-unassembled upload has no parts on this tmp_path."""
    from app.apifiles import queue

    project_ids = [c.project_id for c in callers] if callers else None
    if project_ids is None:
        raise ValueError("assemble_all needs the callers whose uploads to assemble")
    results = []
    while True:
        upload = queue.claim_assembly(project_ids=project_ids)
        if upload is None:
            return results
        results.append(queue.assemble_claimed(upload))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextlib.contextmanager
def serve(app, *, with_assembler: bool = True, project_ids=None) -> Iterator[str]:
    """Run `app` under a real uvicorn on 127.0.0.1 in a thread; yields the
    base URL. The assembly runner starts inside that server's loop."""
    import uvicorn

    from app.apifiles import queue

    port = free_port()
    runner = queue.AssembleRunner(concurrency=2, poll_s=0.5, project_ids=project_ids) if with_assembler else None
    if runner is not None:
        app.state.files_deps.assembler = runner
        app.router.on_startup.append(runner.start)
        app.router.on_shutdown.append(runner.stop)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on",
                            timeout_keep_alive=30)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)


_SERVER_PROGRAM = r"""
import json, os, sys
sys.path.insert(0, os.getcwd())
from types import SimpleNamespace
from app.config import settings
settings.app_database_url = os.environ["APIFILES_SERVER_DSN"]
settings.video_data_dir = os.environ["APIFILES_SERVER_VIDEO_DIR"]
import uvicorn
from app.apifiles import queue
from tests.apifiles_test_support import StubAuth, build_app
callers = [SimpleNamespace(**{**c, "scopes": frozenset(c["scopes"])}) for c in json.loads(os.environ["APIFILES_SERVER_CALLERS"])]
app = build_app(StubAuth(*callers))
runner = queue.AssembleRunner(concurrency=2, poll_s=0.5, project_ids=[c.project_id for c in callers])
app.state.files_deps.assembler = runner
app.router.on_startup.append(runner.start)
app.router.on_shutdown.append(runner.stop)
uvicorn.run(app, host="127.0.0.1", port=int(os.environ["APIFILES_SERVER_PORT"]), log_level="warning")
"""


@contextlib.contextmanager
def serve_process(*callers: SimpleNamespace) -> Iterator[Tuple[str, int]]:
    """The same app in a SEPARATE uvicorn process (so its memory can be
    measured apart from the client's); yields `(base_url, pid)`."""
    import json
    import os
    import subprocess
    import sys

    port = free_port()
    env = {
        **os.environ,
        "APIFILES_SERVER_DSN": settings.app_database_url,
        "APIFILES_SERVER_VIDEO_DIR": str(settings.video_data_dir),
        "APIFILES_SERVER_PORT": str(port),
        "APIFILES_SERVER_CALLERS": json.dumps([
            {"project_id": c.project_id, "workspace_id": c.workspace_id, "key_id": c.key_id,
             "scopes": sorted(c.scopes), "token": c.token}
            for c in callers
        ]),
    }
    process = subprocess.Popen([sys.executable, "-c", _SERVER_PROGRAM], cwd=os.getcwd(), env=env)
    deadline = time.monotonic() + 60
    while True:
        if process.poll() is not None:
            raise RuntimeError(f"the files test server exited with {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            if time.monotonic() > deadline:
                process.kill()
                raise RuntimeError("the files test server did not start")
            time.sleep(0.1)
    try:
        yield f"http://127.0.0.1:{port}", process.pid
    finally:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def peak_rss_mib(pid: int) -> float:
    """VmHWM — the peak resident set of a live process, in MiB."""
    with open(f"/proc/{pid}/status") as status:
        for line in status:
            if line.startswith("VmHWM:"):
                return int(line.split()[1]) / 1024
    return 0.0


async def asgi_call(app, method: str, path: str, *, headers: Dict[str, str], body_chunks: List[bytes],
                    disconnect_after: Optional[int] = None, query: str = "") -> Dict[str, Any]:
    """Drive the ASGI app directly. With `disconnect_after=n`, the receive
    channel delivers n body chunks and then `http.disconnect` — the exact event
    uvicorn delivers when a client's socket closes mid-body."""
    raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "headers": raw_headers, "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 80), "root_path": "",
    }
    chunks = list(body_chunks)
    sent = {"n": 0}
    messages: List[dict] = []
    response_done = asyncio.Event()

    async def receive() -> dict:
        if disconnect_after is not None and sent["n"] >= disconnect_after:
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}
        if sent["n"] < len(chunks):
            index = sent["n"]
            sent["n"] += 1
            return {"type": "http.request", "body": chunks[index], "more_body": index < len(chunks) - 1}
        await response_done.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body"):
            response_done.set()

    await app(scope, receive, send)
    start = next((m for m in messages if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return {
        "status": None if start is None else start["status"],
        "headers": {} if start is None else {k.decode(): v.decode() for k, v in start["headers"]},
        "body": body,
    }


def multipart_body(fields: List[tuple], boundary: str = "testboundary7d1a") -> bytes:
    """`fields`: (name, value) for text, (name, filename, bytes, content_type) for a file."""
    out = bytearray()
    for item in fields:
        out += f"--{boundary}\r\n".encode()
        if len(item) == 2:
            name, value = item
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode() + str(value).encode() + b"\r\n"
        else:
            name, filename, data, content_type = item
            out += (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode()
            out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out)


MULTIPART = "multipart/form-data; boundary=testboundary7d1a"
