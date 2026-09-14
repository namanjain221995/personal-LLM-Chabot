"""A local target for the Files / Uploads conformance tests, built from the
real handlers (2026-09-13).

    cd orchestrator
    TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/test_files_conformance \
      .venv/bin/python ../conformance/python/selftest/files_local_target.py \
        --port 18471 --data-dir /tmp/files-target \
        --keys-out /tmp/files-target/keys.json --env-out /tmp/files-target/node.env

WHY THIS EXISTS. The Files API was built as new modules that the running /v1
router does not mount yet, so no deployment can prove the conformance tests
pass. This process serves the SAME handlers a deployment will serve —
`publicapi.files.routes.create_files_router` — under uvicorn on loopback, the
way the storage team's SDK parity test does, and the two SDK suites run
against it unchanged. It is a harness, not a deployment: read the list below
before trusting a green run for anything the harness stubs.

WHAT IS REAL
* every /v1/files and /v1/uploads handler, the streaming multipart parser,
  part files, assembly (`JobRunner` in the cpu lane claims assemblies), the
  Postgres rows (a private `test_*` database), the byte store on disk;
* processing of text and image kinds: the real `apifiles.jobs.JobRunner`
  stages (sniff, extraction child, chunk, index, decode, variants, finalize)
  and the real `jobs.file_processing_view` on the wire;
* model input: `apifiles.service.lift_file_parts` / `prepare` /
  `splice_messages` / `annotate` — the facade the /v1 router will call —
  against the real `SqlFileStore`.

WHAT IS STUBBED (and why)
* authentication: a Bearer token → caller table (`StubAuth` shape), two
  projects in two workspaces so the cross-project tests have a foreign key,
  and two NARROW keys of the main project for the scope tests: one holding
  only `models.read` (the suites' existing limited key) and one holding only
  `responses.read` + `responses.write`. Scopes are checked the way the
  handlers and the router integration note order them: the route's own scope
  first, then `files.read` for any `file_id` BEFORE a file is resolved. That
  ORDER on the two generating routes is this harness's code (`_prepared`), not
  built code: the model-input 403 test proves what `service.required_scope`
  returns, and publicapi/router.py must make the same two calls in the same
  order before `service.prepare` for the test to pass there. The real
  `router.resolve_caller` needs the `files.*` scopes that
  `apiplatform/scopes.py` does not have yet;
* the embedding engine used by the index stage: a deterministic hash;
* the media pipeline (ffmpeg + whisper): `media.start_media_analysis` is
  replaced by a stand-in that writes the analysis row and a one-segment
  transcript, exactly as `tests/test_apifiles_jobs.py` does. The jobs stages
  around it (probe relay, index over the transcript, finalize) are real;
* the model behind /v1/responses and /v1/chat/completions: a function that
  answers ONLY from the messages `splice_messages` produced — a `word-<hex>`
  codeword it finds in their text (the question's own words and file ids
  excluded), the colour of the centre pixel of an image part, or, asked how
  many seconds long a recording is, the `Duration m:ss` header line of a
  recording's context block. It cannot see the caller's upload any other way,
  so a correct answer proves the file reached the model input. Its `usage` counts the SPLICED messages (4 chars a
  token, 85 an image), as an engine's prompt count would: a request whose file
  or audio block was dropped reports the same count as the bare question;
* the transcriber for `input_audio`: returns a fixed sentence and the WAV's
  duration.

`--defect NAME` serves the handlers with one deliberate defect (`DEFECTS` at
the bottom); `files_selftest.py` uses it to prove each suite catches it.

ONE TARGET PER DATABASE. The job runners claim due blobs of every project in
the database, so a second target on the same database would process (and
fail) this one's blobs; the process holds an advisory lock and refuses to
start beside another.

/v1/openapi.json is FastAPI's schema of exactly these routes, with the
content-part names the model-input routes accept, so the suites' feature
detection sees what this process really serves.
"""
# No `from __future__ import annotations`: FastAPI must resolve the route
# signatures (`Request`) at import time, and they are imported inside build_app.
import argparse
import asyncio
import base64
import io
import json
import os
import re
import secrets
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

#: `pelican-153150` and `zebra-1a2b3c4d` alike; `file-<24 hex>` ids are not codewords.
CODEWORD_RE = re.compile(r"\b(?!file-)[a-z]+-[0-9a-f]{6,}\b")
#: The header line `context._load_media` prints for a recording ("Duration 0:07, other").
DURATION_RE = re.compile(r"\bDuration (?:(\d+):)?(\d+):(\d{2})\b")
TRANSCRIPT_SENTENCE = "A steady tone plays; nobody speaks."


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    here = Path(__file__).resolve()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--orchestrator", default=str(here.parents[3] / "orchestrator"))
    parser.add_argument("--port", type=int, default=18471)
    parser.add_argument("--data-dir", required=True, help="scratch directory for the byte store and video tree")
    parser.add_argument("--keys-out", required=True, help="the Python suite's --keys-file (written 0600)")
    parser.add_argument("--env-out", required=True, help="the Node suite's TECHSARA_ENV_FILE (written 0600)")
    parser.add_argument("--dsn", default=os.environ.get("TEST_DATABASE_URL", ""))
    parser.add_argument("--defect", default="none", choices=sorted(["none", *DEFECTS]),
                        help="serve the handlers with ONE deliberate defect (files_selftest.py proves the suites catch it)")
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    if not args.dsn:
        raise SystemExit("set TEST_DATABASE_URL (or --dsn) to a private test_* database")
    data = Path(args.data_dir).resolve()
    data.mkdir(parents=True, exist_ok=True)
    # Settings the handlers read at call time. The free-space floor defaults to
    # 250 GiB (one maximum assembly); this harness stores kilobytes.
    os.environ["PUBLIC_API_FILES_DIR"] = str(data / "api-files")
    os.environ["PUBLIC_API_FILES_MIN_FREE_GIB"] = "1"
    os.environ["VIDEO_DATA_DIR"] = str(data / "video")
    os.environ.setdefault("TEST_DATABASE_URL", args.dsn)
    sys.path.insert(0, args.orchestrator)
    os.chdir(args.orchestrator)

    from tests.conftest import _assert_safe_test_dsn, _ensure_database  # the suite's own guards

    dsn = _assert_safe_test_dsn(args.dsn)
    _ensure_database(dsn)

    from app import db
    from app.config import settings

    settings.app_database_url = dsn
    settings.video_data_dir = str(data / "video")
    settings.lancedb_dir = str(data / "lancedb")
    settings.lancedb_web_dir = str(data / "lancedb-web")
    settings.lancedb_video_dir = str(data / "lancedb-video")
    settings.session_secret_file = str(data / "session_secret")
    db.close_pool()
    db.init_schema()
    _hold_database(dsn)

    from app.apifiles import schema

    schema.ensure_schema()
    main_caller = _make_caller(db, scopes=("files.read", "files.write", "responses.read", "responses.write"))
    other_caller = _make_caller(db, scopes=("files.read", "files.write", "responses.read", "responses.write"))
    # Same project as the main key: a scope check that ran AFTER the lookup
    # would then answer 200/404, not 403 (a separate project would hide that).
    narrow_caller = _make_caller(db, scopes=("models.read",), project_of=main_caller)
    responses_only_caller = _make_caller(db, scopes=("responses.read", "responses.write"), project_of=main_caller)
    base_url = f"http://127.0.0.1:{args.port}/v1"
    _write_secret(args.keys_out, json.dumps({
        "base_url": base_url,
        "api_key": main_caller.token,
        "scopes": sorted(main_caller.scopes),
        "project_id": main_caller.project_id,
        "other_project_api_key": other_caller.token,
        "limited_api_key": narrow_caller.token,
        "limited_scopes": sorted(narrow_caller.scopes),
        "responses_only_api_key": responses_only_caller.token,
    }, indent=2))
    _write_secret(args.env_out, "\n".join([
        f"TECHSARA_BASE_URL={base_url}",
        f"TECHSARA_API_KEY={main_caller.token}",
        f"TECHSARA_API_KEY_SCOPES={' '.join(sorted(main_caller.scopes))}",
        f"TECHSARA_API_KEY_OTHER_PROJECT={other_caller.token}",
        f"TECHSARA_API_KEY_NARROW={narrow_caller.token}",
        f"TECHSARA_API_KEY_RESPONSES_ONLY={responses_only_caller.token}",
        "CONFORMANCE_MIN_INTERVAL_MS=0",
        "",
    ]))

    DEFECTS.get(args.defect, (None, lambda: None))[1]()
    app = build_app(main_caller, other_caller, narrow_caller, responses_only_caller)
    import uvicorn

    print(f"files local target: {base_url} (projects {main_caller.project_id}, {other_caller.project_id})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning", timeout_keep_alive=30)


_GUARD: List[Any] = []


def _hold_database(dsn: str) -> None:
    """One target per database. WHY (2026-09-13, observed): the job runners
    claim due blobs of EVERY project in the database, and a second target on
    the same database with another --data-dir claimed the first one's blobs,
    looked for their bytes under its own directory and failed them — a flaky
    `wait_for_processing` that was the harness, not the handlers."""
    import psycopg

    guard = psycopg.connect(dsn, autocommit=True)
    held = guard.execute("SELECT pg_try_advisory_lock(hashtext('techsara-files-local-target'))").fetchone()[0]
    if not held:
        raise SystemExit(
            "another files_local_target already serves this database; its job runners would claim this "
            "target's blobs. Stop it, or point TEST_DATABASE_URL at another test_* database."
        )
    _GUARD.append(guard)  # the lock lives as long as this connection, i.e. the process


def _write_secret(path: str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _make_caller(db: Any, *, scopes: Tuple[str, ...], project_of: Optional[SimpleNamespace] = None) -> SimpleNamespace:
    if project_of is not None:
        return SimpleNamespace(
            project_id=project_of.project_id, workspace_id=project_of.workspace_id, key_id=None,
            scopes=frozenset(scopes), token=f"tk_local_{secrets.token_hex(16)}",
        )
    workspace_id = f"ws_conformance_{secrets.token_hex(8)}"
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", (workspace_id, workspace_id))
    project = db.create_api_project(workspace_id, f"files-conformance-{secrets.token_hex(4)}")
    return SimpleNamespace(
        project_id=project["id"], workspace_id=workspace_id, key_id=None,
        scopes=frozenset(scopes), token=f"tk_local_{secrets.token_hex(16)}",
    )


# ------------------------------------------------------------------ stubs --


async def stub_embed(texts):
    import hashlib

    vectors = []
    for text in texts:
        digest = hashlib.sha256(text.encode()).digest()
        vectors.append([b / 255.0 + 0.01 for b in digest[:16]])
    return vectors, sum(max(1, len(t) // 4) for t in texts)


def install_media_stand_in() -> None:
    """`media.start_media_analysis` without ffmpeg or whisper: the analysis row,
    one transcript segment and the pipeline's `_done` event, as the jobs suite's
    audio test does. The duration is read from the WAV header."""
    from app import db
    from app.apifiles import inline, jobs, media, schema, storage
    from app.video import media as vmedia
    from app.video import pipeline, store

    async def start(**kwargs: Any) -> Any:
        with open(kwargs["original_path"], "rb") as handle:
            raw = handle.read()
        seconds = inline.wav_seconds(raw) or 1.0
        content_hash = storage.api_video_hash(kwargs["project_id"], kwargs["sha256"])
        analysis = db.upsert_video_analysis(content_hash, len(raw), kwargs.get("media_type") or "audio/wav", kwargs["filename"])
        os.makedirs(store.analysis_dir(content_hash), exist_ok=True)
        store.write_json(store.stage_path(content_hash, "transcript.json"),
                         {"segments": [{"start": 0.0, "end": float(seconds), "text": TRANSCRIPT_SENTENCE}]})
        os.makedirs(store.artifacts_dir(content_hash), exist_ok=True)
        with open(os.path.join(store.artifacts_dir(content_hash), "transcript.txt"), "w", encoding="utf-8") as handle:
            handle.write(TRANSCRIPT_SENTENCE + "\n")
        schema.update_api_file_blob(kwargs["blob_id"], video_analysis_id=int(analysis["id"]), kind="audio")

        async def finish() -> None:
            await asyncio.sleep(0.2)
            db.update_video_analysis(int(analysis["id"]), status="done", language="en", duration_ms=int(seconds * 1000))
            pipeline._publish(int(analysis["id"]), {"stage": "_done", "status": "done"})

        asyncio.ensure_future(finish())
        probe = vmedia.Probe(duration_s=float(seconds), has_video=False, has_audio=True, width=0, height=0, fps=0.0,
                             video_codec="", audio_codec="pcm_s16le", container="wav", bytes=len(raw))
        return media.MediaStart(analysis_id=int(analysis["id"]), content_hash=content_hash, kind="audio", probe=probe)

    async def no_pipeline(_analysis_id: int) -> bool:
        return True

    media.start_media_analysis = start
    pipeline.ensure_running = no_pipeline
    jobs.MEDIA_POLL_S = 0.2


def _colour_of(data_url: str) -> Optional[str]:
    try:
        from PIL import Image

        payload = base64.b64decode(data_url.split(",", 1)[1])
        image = Image.open(io.BytesIO(payload)).convert("RGB")
        r, g, b = image.getpixel((image.width // 2, image.height // 2))
    except Exception:  # noqa: BLE001 - a part the stub cannot read has no colour
        return None
    named = {"red": (220, 20, 20), "green": (20, 180, 40), "blue": (20, 40, 220),
             "white": (250, 250, 250), "black": (5, 5, 5), "yellow": (240, 220, 20)}
    return min(named, key=lambda n: sum((a - c) ** 2 for a, c in zip((r, g, b), named[n])))


def stub_model(messages: List[Dict[str, Any]], question: str) -> str:
    """Answers from the spliced messages only (module docstring)."""
    texts: List[str] = []
    images: List[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        for part in content or []:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
            url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) else None
            if isinstance(url, str):
                images.append(url)
    lowered = question.lower()
    if images and ("colour" in lowered or "color" in lowered):
        colour = _colour_of(images[-1])
        return colour or "unknown"
    if "how many seconds" in lowered:
        for text in texts:
            match = DURATION_RE.search(text)
            if match:
                hours, minutes, secs = (int(g or 0) for g in match.groups())
                return str(hours * 3600 + minutes * 60 + secs)
        return "I cannot tell how long it is."
    asked = set(CODEWORD_RE.findall(question))
    found = [word for text in texts for word in CODEWORD_RE.findall(text) if word not in asked]
    if found:
        return found[-1]
    if any(TRANSCRIPT_SENTENCE in text for text in texts):
        return "The recording is a steady tone with no speech."
    return "I found nothing to answer from."


def prompt_tokens(messages: List[Dict[str, Any]]) -> int:
    """What an engine would count for these messages, roughly: 4 characters a
    token for text, 85 for an image. Only the spliced messages count, so a
    dropped file or audio block shows up as a smaller number."""
    count = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            count += len(content) // 4 + 1
            continue
        for part in content or []:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                count += len(part["text"]) // 4 + 1
            elif part.get("type") == "image_url":
                count += 85
    return max(1, count)


# -------------------------------------------------------------------- app --


def build_app(*callers: SimpleNamespace):
    from fastapi import APIRouter, FastAPI, Request
    from fastapi.responses import JSONResponse

    from app.apifiles import context as file_context
    from app.apifiles import jobs, queue, service
    from app.publicapi import errors, models
    from app.publicapi.files import routes

    by_token = {c.token: c for c in callers}

    async def resolve(request: Request) -> SimpleNamespace:
        header = request.headers.get("authorization") or ""
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        caller = by_token.get(token)
        if caller is None:
            raise errors.invalid_api_key()
        return caller

    async def authorize(_request: Request, caller: SimpleNamespace, scope: str) -> None:
        if scope not in caller.scopes:
            raise errors.insufficient_scope(scope)

    install_media_stand_in()
    cpu = jobs.JobRunner("cpu", concurrency=2, poll_s=0.5, renew_s=10.0, run_assemblies=True,
                         embed_documents=stub_embed, emit_webhooks=False, record_usage=False)
    media_lane = jobs.JobRunner("media", concurrency=1, poll_s=0.5, renew_s=10.0,
                                embed_documents=stub_embed, emit_webhooks=False, record_usage=False)
    deps = routes.FilesDependencies(
        resolve_caller=resolve, authorize=authorize,
        processing_view=jobs.file_processing_view, assembler=cpu,
    )

    app = FastAPI(title="TechSara Files API — local conformance target", openapi_url="/v1/openapi.json",
                  docs_url=None, redoc_url=None)
    app.include_router(routes.create_files_router(deps))

    async def start_runners() -> None:
        jobs._runners["cpu"] = cpu
        jobs._runners["media"] = media_lane
        queue.set_enqueue_hook(jobs.enqueue)
        await cpu.start()
        await media_lane.start()

    async def stop_runners() -> None:
        queue.set_enqueue_hook(None)
        await cpu.stop()
        await media_lane.stop()

    app.router.on_startup.append(start_runners)
    app.router.on_shutdown.append(stop_runners)

    engines = service.Engines(transcriber=_stub_transcriber, analysis_loader=service.db_analysis_loader)
    caps = file_context.ModelCaps(model_id="local", vision=True, max_images=16, context_window=262_144,
                                  max_input_tokens=262_144, planned_output_tokens=64)
    generation = APIRouter(prefix="/v1", route_class=routes.FilesRoute)
    part_schema = {"requestBody": {"content": {"application/json": {"schema": {
        "type": "object",
        "description": "Content part types this target accepts on user messages.",
        "properties": {"content_part_types": {"type": "string", "enum": [
            "input_text", "input_file", "input_image", "input_video", "text", "file", "input_audio"]}},
    }}}}}

    async def _prepared(request: Request, dialect: str) -> Tuple[Any, Any, Any]:
        caller = await resolve(request)
        await authorize(request, caller, "responses.write")  # the route's own scope first
        try:
            payload = json.loads(await request.body())
        except ValueError:
            raise errors.invalid_request("The request body is not valid JSON.") from None
        lifted = service.lift_file_parts(payload, dialect=dialect)
        scope = service.required_scope(lifted)
        if scope:  # before prepare: a key that may not read files learns nothing about ids
            await authorize(request, caller, scope)
        request_id = getattr(request.state, "public_request_id", "") or f"req_{uuid.uuid4().hex}"
        prepared = await service.prepare(lifted, project_id=caller.project_id, store=service.SqlFileStore(),
                                         caps=caps, delivery="sync", request_id=request_id, engines=engines)
        return caller, lifted, prepared

    @generation.post("/responses", operation_id="createResponse", openapi_extra=part_schema, response_class=JSONResponse)
    async def create_response(request: Request):
        _caller, lifted, prepared = await _prepared(request, service.DIALECT_RESPONSES)
        try:
            messages = service.splice_messages(models.parse_responses_request(lifted.payload).chat_messages(), prepared)
            text = stub_model(messages, lifted.question)
            annotated = service.annotate(text, prepared)
            prompt = prompt_tokens(messages)
        finally:
            prepared.cleanup()
        now = int(time.time())
        body = {
            "id": f"resp_{uuid.uuid4().hex[:24]}", "object": "response", "created_at": now, "status": "completed",
            "model": str(lifted.payload.get("model") or "local"), "error": None, "incomplete_details": None,
            "output": [{"type": "message", "id": f"msg_{uuid.uuid4().hex[:24]}", "status": "completed", "role": "assistant",
                        "content": [{"type": "output_text", "text": text,
                                     "annotations": list(getattr(annotated, "annotations", []) or [])}]}],
            "usage": {"input_tokens": prompt, "output_tokens": max(1, len(text) // 4),
                      "total_tokens": prompt + max(1, len(text) // 4)},
        }
        return JSONResponse(body)

    @generation.post("/chat/completions", operation_id="createChatCompletion", openapi_extra=part_schema,
                     response_class=JSONResponse)
    async def create_chat_completion(request: Request):
        from app.publicapi import router as public_router

        _caller, lifted, prepared = await _prepared(request, service.DIALECT_CHAT)
        try:
            request_model, _ = public_router._from_chat_completions(lifted.payload)
            messages = service.splice_messages(request_model.chat_messages(), prepared)
            text = stub_model(messages, lifted.question)
            prompt = prompt_tokens(messages)
        finally:
            prepared.cleanup()
        return JSONResponse({
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion", "created": int(time.time()),
            "model": str(lifted.payload.get("model") or "local"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": prompt, "completion_tokens": max(1, len(text) // 4),
                      "total_tokens": prompt + max(1, len(text) // 4)},
        })

    app.include_router(generation)
    return app


async def _stub_transcriber(raw: bytes, content_type: str):
    from app.apifiles import inline

    return TRANSCRIPT_SENTENCE, inline.wav_seconds(raw)


# ---------------------------------------------------------------- defects --
#
# Each defect breaks ONE promise the conformance tests make, in the running
# process only. `files_selftest.py` starts the target with each and checks
# that exactly the tests that guard that promise fail, in both SDK suites — a
# test that stays green against its own defect proves nothing.


def _defect_cross_project_file_lookup() -> None:
    from app import db
    from app.apifiles import schema

    real = schema.get_api_file

    def by_id_only(project_id: str, file_id: str):
        with db.connection() as con:
            row = con.execute("SELECT project_id FROM api_files WHERE id = %s", (file_id,)).fetchone()
        return real(row["project_id"] if row else project_id, file_id)

    schema.get_api_file = by_id_only


def _defect_part_sha256_ignored() -> None:
    from app.publicapi.files import routes

    routes._sha256_field = lambda raw, *, param: None


def _defect_state_conflict_says_retry() -> None:
    from app.publicapi.files import wire

    real = wire.upload_state_conflict
    wire.upload_state_conflict = lambda message, *, retry_after=None, should_retry=False: real(
        message, retry_after=retry_after, should_retry=True)


def _defect_splice_drops_files() -> None:
    from app.apifiles import service

    service.splice_messages = lambda messages, model_input: [dict(m) for m in messages]


def _defect_md5_ignored() -> None:
    from app.apifiles import schema

    real = schema.complete_api_upload

    def without_md5(*args, **kwargs):
        kwargs["expected_md5"] = None
        return real(*args, **kwargs)

    schema.complete_api_upload = without_md5


def _defect_range_ignored() -> None:
    from app.publicapi.files import content

    content.parse_range = lambda header, size: None


def _defect_cancel_not_idempotent() -> None:
    from app.apifiles import schema

    real = schema.cancel_api_upload

    def once(project_id: str, upload_id: str):
        state, row = real(project_id, upload_id)
        return ("conflict", row) if state == "already" else (state, row)

    schema.cancel_api_upload = once


def _defect_complete_not_replayable() -> None:
    from app.apifiles import schema

    real = schema.try_begin_api_upload_finalize

    def no_replay(project_id: str, upload_id: str):
        state, row = real(project_id, upload_id)
        return ("conflict", row) if state == "completed" else (state, row)

    schema.try_begin_api_upload_finalize = no_replay


def _defect_list_order_ignored() -> None:
    from app.apifiles import schema

    real = schema.list_api_files
    schema.list_api_files = lambda project_id, **kwargs: real(project_id, **{**kwargs, "order": "asc"})


def _defect_resume_view_lists_no_parts() -> None:
    """GET /uploads/{id} answers without the parts the server holds."""
    from app.publicapi.files import wire

    real = wire.upload_object
    wire.upload_object = lambda row, *, file=None, parts=None: real(row, file=file, parts=None if parts is None else [])


def _defect_upload_lookup_ignores_project() -> None:
    """An upload is looked up by id alone (GET /uploads/{id}, PUT parts and
    every route that reads the upload row through `_upload_row`)."""
    from app import db
    from app.apifiles import schema

    real = schema.get_api_upload

    def by_id_only(project_id: str, upload_id: str):
        with db.connection() as con:
            row = con.execute("SELECT project_id FROM api_uploads WHERE id = %s", (upload_id,)).fetchone()
        return real(row["project_id"] if row else project_id, upload_id)

    schema.get_api_upload = by_id_only


def _defect_model_input_ignores_project() -> None:
    """The model-input store resolves a file id in whichever project owns it."""
    from app import db
    from app.apifiles import service

    real_query = service.SqlFileStore._query

    def any_project(self, project_id, file_ids):
        with db.connection() as con:
            owners = con.execute("SELECT DISTINCT project_id FROM api_files WHERE id = ANY(%s)",
                                 ([i for i in file_ids if "\x00" not in i],)).fetchall()
        rows = []
        for owner in owners or [{"project_id": project_id}]:
            for row in real_query(self, owner["project_id"], file_ids):
                rows.append({**row, "project_id": project_id})
        return rows

    service.SqlFileStore._query = any_project


def _defect_complete_sha256_ignored() -> None:
    from app.apifiles import schema

    real = schema.complete_api_upload

    def without_sha256(*args, **kwargs):
        kwargs["expected_sha256"] = None
        return real(*args, **kwargs)

    schema.complete_api_upload = without_sha256


def _defect_input_audio_transcript_dropped() -> None:
    """`input_audio` is accepted and transcribed, but its block never reaches the messages."""
    from app.apifiles import service

    real = service.splice_messages

    def splice(messages, model_input):
        model_input.audio_blocks.clear()
        return real(messages, model_input)

    service.splice_messages = splice


def _defect_files_routes_skip_scope() -> None:
    """The /v1/files and /v1/uploads handlers never check a scope."""
    import dataclasses

    from app.publicapi.files import routes

    real = routes.create_files_router

    async def allow(_request, _caller, _scope) -> None:
        return None

    routes.create_files_router = lambda deps, **kwargs: real(dataclasses.replace(deps, authorize=allow), **kwargs)


def _defect_model_input_skips_files_read() -> None:
    """A `file_id` in a prompt needs no files.read."""
    from app.apifiles import service

    service.required_scope = lambda lifted: None


def _defect_failed_files_reach_the_model() -> None:
    """A file that ended in error is used as model input anyway."""
    import dataclasses

    from app.apifiles import service

    real = service.record_from_row

    def lenient(row, *, now=None):
        record = real(row, now=now)
        if record is not None and record.state == "failed":
            return dataclasses.replace(record, state="processed", error_code=None)
        return record

    service.record_from_row = lenient


def _defect_part_kind_unchecked() -> None:
    """input_image / input_video accept a file of any kind."""
    from app.apifiles import service

    service._check_part_kind = lambda ref, record: None


def _defect_media_blocks_dropped() -> None:
    """An audio or video `file_id`'s context block never reaches the messages,
    while the system addendum and the "(The file … is attached above.)"
    placeholder still do (2026-09-13 review probe). The prompt still grows by
    ~160 tokens over the bare question, so a token-growth check misses it."""
    from app.apifiles import context as file_context
    from app.apifiles import service

    real = service.prepare

    async def prepare(*args, **kwargs):
        prepared = await real(*args, **kwargs)
        for file_id, record in prepared.files.items():
            if record.kind in file_context.KINDS_MEDIA:
                prepared.context.blocks.pop(file_id, None)
        return prepared

    service.prepare = prepare


DEFECTS = {
    "cross_project_file_lookup": ("a file is looked up by id alone, not by project", _defect_cross_project_file_lookup),
    "part_sha256_ignored": ("a part's declared sha256 is never compared", _defect_part_sha256_ignored),
    "state_conflict_says_retry": ("upload_state_conflict tells the SDKs to retry", _defect_state_conflict_says_retry),
    "splice_drops_files": ("file blocks never reach the model messages", _defect_splice_drops_files),
    "md5_ignored": ("complete never checks the md5 it was given", _defect_md5_ignored),
    "range_ignored": ("content ignores Range", _defect_range_ignored),
    "cancel_not_idempotent": ("a second cancel is a 409", _defect_cancel_not_idempotent),
    "complete_not_replayable": ("a second complete is a 409", _defect_complete_not_replayable),
    "list_order_ignored": ("list always answers oldest first", _defect_list_order_ignored),
    "resume_view_lists_no_parts": ("GET /uploads/{id} lists no parts", _defect_resume_view_lists_no_parts),
    "upload_lookup_ignores_project": ("an upload is looked up by id alone, not by project", _defect_upload_lookup_ignores_project),
    "model_input_ignores_project": ("a file_id in a prompt resolves in any project", _defect_model_input_ignores_project),
    "complete_sha256_ignored": ("complete never checks the sha256 it was given", _defect_complete_sha256_ignored),
    "input_audio_transcript_dropped": ("an input_audio transcript never reaches the messages", _defect_input_audio_transcript_dropped),
    "files_routes_skip_scope": ("the files and uploads routes check no scope", _defect_files_routes_skip_scope),
    "model_input_skips_files_read": ("a file_id in a prompt needs no files.read", _defect_model_input_skips_files_read),
    "failed_files_reach_the_model": ("a file that ended in error is used as model input", _defect_failed_files_reach_the_model),
    "part_kind_unchecked": ("input_image and input_video accept any kind of file", _defect_part_kind_unchecked),
    "media_blocks_dropped": ("a recording's block is dropped but the file scaffolding stays", _defect_media_blocks_dropped),
}


if __name__ == "__main__":
    main()
