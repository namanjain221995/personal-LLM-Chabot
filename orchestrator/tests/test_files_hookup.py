"""The Files API hooked into the running platform (files-hookup, 2026-09-13).

What the build waves proved in isolation — the tables, the routes, processing,
model input — this suite proves is CONNECTED: the migration is in the chain,
the settings are declared, the scopes exist, the application's body cap lets a
part through, the public router serves the fourteen routes with the real
dependencies, the lifespan starts and stops the workers, and a generating
request with a file part is lifted, authorised, resolved, planned and answered
through the real router. The openai-python end-to-end run against the whole
application lives beside the patch (scratchpad `e2e/run_e2e.sh`); these are
the in-process pins that keep each seam from coming undone.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import config as config_module, db, llm
from app.apifiles import schema, service
from app.apiplatform import projects, scopes
from app.config import settings
from app.publicapi import file_inputs, models, planning, registry, router as public_router
from tests.test_apifiles_vectors import write_pages
from tests.test_publicapi_routes import (  # noqa: F401 - fixtures
    TOKENS,
    WORKSPACE,
    _auth,
    _pepper,
    api,
    platform,
)

APP_ROOT = Path(__file__).resolve().parents[1] / "app"


# ----------------------------------------------------------- the migration --


def test_the_files_tables_are_migration_v37_and_the_schema_module_applies_the_same_text():
    versions = [version for version, _ddl in db._MIGRATIONS]
    assert versions[-1] == 37
    # V36 is the durable-generation migration (no-timeout T1); whether or not
    # it has landed, it must not be the files DDL.
    assert dict(db._MIGRATIONS).get(36) is not db.FILES_MIGRATION_SQL
    assert dict(db._MIGRATIONS)[37] is db.FILES_MIGRATION_SQL
    assert schema.MIGRATION_SQL is db.FILES_MIGRATION_SQL
    assert schema.SCHEMA_VERSION == 37
    with db.connection() as con:
        applied = {int(r["version"]) for r in con.execute("SELECT version FROM schema_migrations").fetchall()}
        tables = {
            r["table_name"]
            for r in con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
            ).fetchall()
        }
        lane = con.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'video_analyses' AND column_name = 'lane'"
        ).fetchone()
    assert 37 in applied
    assert set(schema.TABLES) <= tables
    assert lane is not None and "chat" in str(lane["column_default"])


def test_a_database_holding_the_superseded_api_files_shape_refuses_to_migrate():
    """The no-timeout design sketched an `api_files` without `blob_id`. If any
    migration still creates it, `CREATE TABLE IF NOT EXISTS` would keep it and
    every file route would fail at its first query — so V37 refuses instead."""
    with db.connection() as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS hookup_guard")
    try:
        with db.connection() as con:
            with pytest.raises(psycopg.errors.RaiseException, match="without blob_id"):
                with con.transaction():
                    con.execute("SET LOCAL search_path TO hookup_guard")
                    con.execute("CREATE TABLE api_files (id text PRIMARY KEY, status text)")
                    con.execute(db.FILES_MIGRATION_SQL)
    finally:
        with db.connection() as con:
            con.execute("DROP SCHEMA IF EXISTS hookup_guard CASCADE")


def test_the_chat_orphan_reaper_never_offers_an_api_lane_analysis():
    stale = "2000-01-01T00:00:00+00:00"
    chat = db.upsert_video_analysis("c" * 64, 10, "video/mp4", "chat.mp4")
    api_row = db.upsert_video_analysis("a" * 64, 10, "video/mp4", "api.mp4", lane="api")
    try:
        with db.connection() as con:
            con.execute("UPDATE video_analyses SET updated_at = %s WHERE id = ANY(%s)", (stale, [chat["id"], api_row["id"]]))
        assert chat["lane"] == "chat" and api_row["lane"] == "api"
        orphaned = {row["id"] for row in db.orphan_video_analyses(1)}
        assert chat["id"] in orphaned
        assert api_row["id"] not in orphaned
        # The lane is written only when the row is created.
        again = db.upsert_video_analysis("a" * 64, 10, "video/mp4", "api.mp4", lane="chat")
        assert again["lane"] == "api" and again["created"] is False
    finally:
        for row in (chat, api_row):
            db.delete_video_analysis(row["id"])


def test_an_unknown_video_lane_is_refused_before_any_write():
    with pytest.raises(ValueError, match="lane"):
        db.upsert_video_analysis("b" * 64, 10, "video/mp4", "x.mp4", lane="public")


# ------------------------------------------------------------ the settings --


_READER_LITERAL = re.compile(r'"(PUBLIC_API_(?:FILES|UPLOAD|INLINE)_[A-Z0-9_]+)"')


def _names_read_by_the_files_code() -> set:
    found = set()
    for folder in ("apifiles", "publicapi/files"):
        for path in (APP_ROOT / folder).rglob("*.py"):
            found.update(_READER_LITERAL.findall(path.read_text(encoding="utf-8")))
    for path in (APP_ROOT / "publicapi").glob("*.py"):
        found.update(n for n in _READER_LITERAL.findall(path.read_text(encoding="utf-8")) if "FILES" in n or "UPLOAD" in n)
    return found


def test_every_files_setting_a_reader_names_is_declared_once_in_config():
    declared = [env for env, *_rest in config_module.PUBLIC_API_FILES_SETTINGS]
    assert len(declared) == len(set(declared))
    assert _names_read_by_the_files_code() <= set(declared)
    for env, kind, default, why in config_module.PUBLIC_API_FILES_SETTINGS:
        assert kind in ("int", "float", "str") and why.strip(), env


def test_each_declared_default_is_the_default_its_reader_applies(monkeypatch):
    from app.apifiles import limits

    for env, *_rest in config_module.PUBLIC_API_FILES_SETTINGS:
        monkeypatch.delenv(env, raising=False)
    # The reader's answer with the attribute present equals its answer with the
    # attribute absent (its own literal default) — for every simple reader.
    pairs = {
        "PUBLIC_API_FILES_PART_MAX_BYTES": limits.part_max_bytes,
        "PUBLIC_API_FILES_MAX_BODY_BYTES": limits.max_body_bytes,
        "PUBLIC_API_FILES_MIN_FREE_GIB": limits.min_free_bytes,
        "PUBLIC_API_FILES_INLINE_MAX_TOKENS": limits.inline_max_tokens,
        "PUBLIC_API_FILES_SYNC_READY_WAIT_S": limits.sync_ready_wait_s,
        "PUBLIC_API_FILES_CPU_JOBS": limits.cpu_jobs,
        "PUBLIC_API_FILES_ASSEMBLY_RENEW_S": limits.assembly_renew_s,
        "PUBLIC_API_FILES_DIR": limits.files_dir,
    }
    with_attribute = {name: reader() for name, reader in pairs.items()}
    for env in pairs:
        monkeypatch.delattr(config_module.Settings, env.lower())
    without_attribute = {name: reader() for name, reader in pairs.items()}
    assert with_attribute == without_attribute
    assert service.sync_prepare_budget_s() == 45.0


def test_a_files_setting_follows_the_environment_at_call_time_and_an_assignment_wins_until_undone():
    name = "PUBLIC_API_FILES_CPU_JOBS"
    mp = pytest.MonkeyPatch()
    try:
        assert settings.public_api_files_cpu_jobs == 2
        mp.setenv(name, "5")
        assert settings.public_api_files_cpu_jobs == 5
        inner = pytest.MonkeyPatch()
        inner.setattr(settings, "public_api_files_cpu_jobs", 9)
        assert settings.public_api_files_cpu_jobs == 9
        inner.undo()
        # Undo assigns back the value it read (5, the environment's): that
        # must not pin 5 — the environment still decides.
        mp.setenv(name, "3")
        assert settings.public_api_files_cpu_jobs == 3
    finally:
        mp.undo()
    assert settings.public_api_files_cpu_jobs == 2
    # Nothing is left behind, and nothing ever sits under the attribute's own
    # name (a reader that bypasses the descriptor must never see a pin).
    assert "public_api_files_cpu_jobs" not in vars(settings)
    assert "public_api_files_cpu_jobs" not in vars(settings).get(config_module._PINS_KEY, {})


def test_an_assignment_made_before_the_environment_changed_does_not_outlive_the_change():
    """The order `monkeypatch.undo()` restores in — attributes first, then the
    environment — used to leave the attribute's value pinned for later tests."""
    name = "PUBLIC_API_FILES_PART_MAX_BYTES"
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv(name, "1024")
        mp.setattr(settings, "public_api_files_part_max_bytes", 2048)
        assert settings.public_api_files_part_max_bytes == 2048
        mp.setenv(name, "4096")
        assert settings.public_api_files_part_max_bytes == 4096
    finally:
        mp.undo()
    assert settings.public_api_files_part_max_bytes == 67_108_864


def test_an_assignment_never_shadows_the_attribute_for_code_that_reads_the_instance(monkeypatch):
    monkeypatch.setattr(settings, "public_api_files_inline_max_tokens", 7)
    assert settings.public_api_files_inline_max_tokens == 7
    assert "public_api_files_inline_max_tokens" not in vars(settings)


def test_a_garbage_files_setting_fails_start_up_not_the_first_upload(monkeypatch):
    monkeypatch.setenv("PUBLIC_API_FILES_PART_MAX_BYTES", "sixty-four")
    with pytest.raises(ValueError, match="PUBLIC_API_FILES_PART_MAX_BYTES"):
        config_module.validate_files_settings(settings)


# -------------------------------------------------------------- the scopes --


def test_the_two_file_scopes_carry_the_design_sentences_and_are_defaults():
    assert scopes.Scope("files.read") is scopes.Scope.FILES_READ
    assert scopes.Scope("files.write") is scopes.Scope.FILES_WRITE
    assert scopes.SCOPE_DESCRIPTIONS[scopes.Scope.FILES_READ] == "Read, download and use this project's files."
    assert scopes.SCOPE_DESCRIPTIONS[scopes.Scope.FILES_WRITE] == "Upload and delete this project's files."
    # Owner decision D1, the design's proposal; usage.read stays opt-in.
    assert {scopes.Scope.FILES_READ, scopes.Scope.FILES_WRITE} <= scopes.DEFAULT_SCOPES
    assert scopes.Scope.USAGE_READ not in scopes.DEFAULT_SCOPES
    assert len(scopes.ALL_SCOPES) == 9


def test_the_console_key_dialog_offers_the_same_file_scopes_with_the_same_sentences():
    frontend = APP_ROOT.parents[1] / "frontend" / "components" / "devplatform"
    types_ts = (frontend / "types.ts").read_text(encoding="utf-8")
    dialog = (frontend / "CreateKeyDialog.tsx").read_text(encoding="utf-8")
    for scope in (scopes.Scope.FILES_READ, scopes.Scope.FILES_WRITE):
        sentence = scopes.SCOPE_DESCRIPTIONS[scope].replace("'", "’")
        assert f"id: '{scope.value}'" in types_ts and sentence in types_ts
        assert f"'{scope.value}'," in dialog


# ----------------------------------------------------------- the body caps --


def test_the_application_lets_a_file_part_through_and_keeps_json_file_routes_at_one_mib():
    from app import main

    mib = 1024 * 1024
    assert main.body_cap_for("POST", "/v1/files").anonymous == 65 * mib
    assert main.body_cap_for("POST", "/v1/uploads/upload_abc/parts").anonymous == 65 * mib
    assert main.body_cap_for("PUT", "/v1/uploads/upload_abc/parts/3").anonymous == 64 * mib
    assert main.body_cap_for("POST", "/v1/uploads").anonymous == mib
    assert main.body_cap_for("POST", "/v1/uploads/upload_abc/complete").anonymous == mib
    assert main.body_cap_for("GET", "/v1/files/file-abc/content").anonymous == mib
    # A prefix match would hand 65 MiB to a JSON route.
    assert main.body_cap_for("POST", "/v1/files/extra").anonymous == mib
    assert main.body_cap_for("POST", "/v1/responses").anonymous == models.max_media_body_bytes()


def test_a_part_over_one_mib_reaches_the_router_through_the_whole_application():
    """Unauthenticated on purpose: the router's 401 proves the middleware let
    the body through; before the hookup the middleware answered 413."""
    from app.main import app

    client = TestClient(app)
    response = client.put(
        "/v1/uploads/upload_" + "0" * 24 + "/parts/0",
        content=b"\x00" * (3 * 1024 * 1024),
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 401, response.text[:200]


# ------------------------------------------------------------ the mounting --


FOURTEEN = {
    "POST /v1/files", "GET /v1/files", "GET /v1/files/{file_id}", "GET /v1/files/{file_id}/content",
    "DELETE /v1/files/{file_id}", "GET /v1/files/{file_id}/events", "GET /v1/files/{file_id}/derived",
    "GET /v1/files/{file_id}/derived/{name}", "POST /v1/uploads", "GET /v1/uploads/{upload_id}",
    "POST /v1/uploads/{upload_id}/parts", "PUT /v1/uploads/{upload_id}/parts/{part_number}",
    "POST /v1/uploads/{upload_id}/complete", "POST /v1/uploads/{upload_id}/cancel",
}


def test_the_public_router_serves_all_fourteen_file_routes_through_its_own_route_class():
    assert public_router.FILES_MOUNTED is True and public_router.FILES_MOUNT_ERROR is None
    served = {}
    for route in public_router.router.routes:
        for method in getattr(route, "methods", None) or ():
            served[f"{method} {route.path}"] = route
    assert FOURTEEN <= set(served)
    assert all(isinstance(served[name], public_router.PublicRoute) for name in FOURTEEN)


def test_the_routes_carry_the_processing_half_the_docs_and_the_file_object_describe():
    from app.apifiles import derived, events, jobs, retention

    deps = public_router.FILES_DEPENDENCIES
    assert deps.processing_view is jobs.file_processing_view
    assert deps.derived is derived
    assert deps.events is events.stream_file_events
    assert deps.purge_blob is retention.purge_blob
    # The resolver, scope check and admission are the public router's own.
    assert deps.request_id is public_router.request_id
    assert deps.assembler is not None and callable(deps.assembler.kick)


def test_registering_the_files_again_adds_nothing():
    before = len(public_router.router.routes)
    public_router._register_files()
    assert len(public_router.router.routes) == before and public_router.FILES_MOUNTED


def test_a_files_package_that_will_not_import_leaves_the_rest_of_v1_serving(monkeypatch):
    import app.publicapi.files as files_package

    monkeypatch.delattr(files_package, "routes")
    monkeypatch.setitem(sys.modules, "app.publicapi.files.routes", None)
    public_router._register_files()
    assert public_router.FILES_MOUNTED is False
    assert public_router.FILES_MOUNT_ERROR in ("ImportError", "ModuleNotFoundError")
    monkeypatch.undo()
    public_router._register_files()
    assert public_router.FILES_MOUNTED is True


def test_the_preflight_lets_a_browser_send_raw_parts_deletes_and_range_requests(api):
    response = api.options(
        "/v1/uploads/upload_x/parts/0",
        headers={"Origin": "https://app.example", "Access-Control-Request-Method": "PUT"},
    )
    assert response.status_code == 204
    assert response.headers["Access-Control-Allow-Methods"] == "GET, POST, PUT, DELETE, OPTIONS"
    allowed = response.headers["Access-Control-Allow-Headers"]
    for header in ("content-digest", "x-part-sha256", "range", "if-none-match", "idempotency-key"):
        assert header in allowed
    exposed = response.headers["Access-Control-Expose-Headers"]
    for header in ("Content-Disposition", "Content-Range", "ETag", "x-should-retry"):
        assert header in exposed
    assert "access-control-allow-credentials" not in {k.lower() for k in response.headers}


def _key(platform, name, scope_values):
    return projects.create_key(platform["project"]["id"], WORKSPACE, name, scopes=scope_values).token


def test_a_file_route_asks_the_platforms_resolver_and_scopes_not_a_stub(api, platform, tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "api-files"))
    monkeypatch.setenv("PUBLIC_API_FILES_MIN_FREE_GIB", "0")
    reader = _key(platform, "reader", ["files.read"])
    writer = _key(platform, "writer", ["files.write", "files.read"])

    assert api.get("/v1/files").status_code == 401
    refused = api.post("/v1/files", headers={"Authorization": f"Bearer {reader}"},
                       files={"file": ("a.txt", b"hello", "text/plain")}, data={"purpose": "user_data"})
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "insufficient_scope"
    assert "files.write" in refused.json()["error"]["message"]

    created = api.post("/v1/files", headers={"Authorization": f"Bearer {writer}"},
                       files={"file": ("a.txt", b"hello files", "text/plain")}, data={"purpose": "user_data"})
    assert created.status_code == 200, created.text
    listed = api.get("/v1/files", headers={"Authorization": f"Bearer {reader}"}).json()
    assert [f["id"] for f in listed["data"]] == [created.json()["id"]]
    # The File object renders through the processing team's view, not the
    # ingest team's default (which reports percent null for a queued blob).
    from app.apifiles import jobs

    stored = schema.get_api_file(platform["project"]["id"], created.json()["id"])
    assert created.json()["processing"] == jobs.file_processing_view(stored)["processing"]
    assert created.json()["processing"]["stages"]
    # Another project's key sees the same 404 as a missing id.
    theirs = api.get(f"/v1/files/{created.json()['id']}", headers=_auth("other"))
    assert theirs.status_code == 404 and theirs.json()["error"]["code"] == "file_not_found"


def test_usage_reports_this_projects_storage_as_accounted_not_metered(api, platform, tmp_path, monkeypatch):
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "api-files"))
    monkeypatch.setenv("PUBLIC_API_FILES_MIN_FREE_GIB", "0")
    writer = _key(platform, "writer", ["files.write", "usage.read"])
    api.post("/v1/files", headers={"Authorization": f"Bearer {writer}"},
             files={"file": ("a.txt", b"twelve bytes", "text/plain")}, data={"purpose": "user_data"})
    body = api.get("/v1/usage", headers={"Authorization": f"Bearer {writer}"}).json()
    assert body["storage"]["files"] == 1 and body["storage"]["bytes"] == 12
    monkeypatch.setattr(public_router, "FILES_MOUNTED", False)
    assert "storage" not in api.get("/v1/usage", headers={"Authorization": f"Bearer {writer}"}).json()


# ------------------------------------------------------------- the lifespan --


class _Worker:
    def __init__(self, log: List[str], name: str, *, fail_start: bool = False) -> None:
        self.log, self.name, self.fail_start = log, name, fail_start

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("boom")
        self.log.append(f"start {self.name}")

    async def stop(self) -> None:
        self.log.append(f"stop {self.name}")


def _install_workers(monkeypatch, log, *, retention_fails=False):
    from app.apifiles import jobs, retention, uploads_sweep

    for module, name, fails in ((jobs, "jobs", False), (retention, "retention", retention_fails), (uploads_sweep, "sweep", False)):
        worker = _Worker(log, name, fail_start=fails)
        monkeypatch.setattr(module, "start", worker.start)
        monkeypatch.setattr(module, "stop", worker.stop)


def test_the_lifespan_starts_processing_retention_and_the_sweep_and_stops_them_in_reverse(tmp_path, monkeypatch):
    from app import main

    log: List[str] = []
    _install_workers(monkeypatch, log)
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "root"))
    started = asyncio.run(main._start_files_workers())
    assert (tmp_path / "root" / "_uploads").is_dir()
    asyncio.run(main._stop_files_workers(started))
    assert log == ["start jobs", "start retention", "start sweep", "stop sweep", "stop retention", "stop jobs"]


def test_a_worker_that_will_not_start_does_not_stop_the_others_or_the_application(tmp_path, monkeypatch):
    from app import main

    log: List[str] = []
    _install_workers(monkeypatch, log, retention_fails=True)
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "root"))
    started = asyncio.run(main._start_files_workers())
    asyncio.run(main._stop_files_workers(started))
    assert log == ["start jobs", "start sweep", "stop sweep", "stop jobs"]


def test_an_unusable_storage_root_starts_no_file_worker(tmp_path, monkeypatch):
    from app import main

    log: List[str] = []
    _install_workers(monkeypatch, log)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(blocker / "api-files"))
    assert asyncio.run(main._start_files_workers()) is None
    assert log == []


def test_the_real_application_lifespan_runs_the_file_workers_while_it_serves(tmp_path, monkeypatch):
    from app.apifiles import jobs, retention, uploads_sweep
    from app.main import app

    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "api-files"))
    with TestClient(app):
        assert set(jobs._runners) == {"cpu", "media"}
        assert retention._task is not None and not retention._task.done()
        assert uploads_sweep._task is not None and not uploads_sweep._task.done()
    assert jobs._runners == {} and retention._task is None and uploads_sweep._task is None


def test_children_of_the_orchestrator_start_with_core_dumps_disabled():
    code = (
        "import resource\n"
        "from app import main\n"
        "assert main._limit_core_dumps_for_children()\n"
        "print(resource.getrlimit(resource.RLIMIT_CORE))\n"
        "import subprocess\n"
        "print(subprocess.check_output(['sh', '-c', 'ulimit -c']).decode().strip())\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=str(APP_ROOT.parent), capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": str(APP_ROOT.parent)}, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    lines = out.stdout.strip().splitlines()
    assert lines[-2] == "(1, 1)"
    # `ulimit -c` counts 512-byte blocks: one byte is reported as 0 blocks.
    assert lines[-1] in ("0", "1")


# ------------------------------------------------------------- the planner --


def _model(model_id=registry.TECHSARA_35B):
    return registry.resolve_public_model(model_id, allowed=None, overrides=None)


def _request(**kwargs):
    body = {"model": registry.TECHSARA_35B, "input": [{"role": "user", "content": [{"type": "input_text", "text": "(attached file)"}]}]}
    body.update(kwargs)
    return models.parse_responses_request(body)


def test_a_pending_file_request_to_the_ocr_model_is_not_refused_before_its_image_file_resolves():
    ocr = _model(registry.TECHSARA_OCR)
    request = _request(model=ocr.id)
    with pytest.raises(Exception, match="exactly one image"):
        planning.plan_generation(request, ocr)
    plan = planning.plan_generation(request, ocr, files=planning.FILES_PENDING)
    assert plan.image_count == 0


def test_file_text_counts_at_the_engines_count_so_a_100k_token_inline_file_stays_out_of_main_long():
    """Senior fix 2026-09-14: the engine's exact count, not the estimate (a
    digit-dense file games the estimate down) and not the byte bound (prose
    at ~3 bytes a token would all go to `main.long`). Without a count the
    byte bound decides, as it does for typed text."""
    model = _model()
    request = _request()
    file_text = "x" * 300_000  # 300,000 bytes the engine counts at 100,000
    spliced = [{"role": "user", "content": file_text}]
    files = planning.FileInputs(messages=spliced, tokens=100_000, bounded_tokens=300_000, images=0, measured_tokens=100_000)
    plan = planning.plan_generation(request, model, files=files)
    assert plan.messages == spliced
    assert plan.estimated_input_tokens >= 100_000
    # 131,072: admission's LONG threshold (no main gate is sized by the prompt
    # since PR #65, merged 2026-09-14).
    assert plan.footprint_tokens < 131_072
    assert plan.gate_engine != "main.long"
    assert plan.bounded_input_tokens >= 100_000
    uncounted = planning.FileInputs(messages=spliced, tokens=100_000, bounded_tokens=300_000, images=0)
    assert uncounted.ceiling_tokens() == 300_000
    try:
        fallback = planning.plan_generation(request, model, files=uncounted)
    except Exception as refused:  # a window smaller than 300,000 refuses it
        assert getattr(refused, "code", "") == "context_length_exceeded"
    else:
        assert fallback.bounded_input_tokens >= 300_000


def test_file_images_count_against_the_models_image_limit_once_resolved():
    model = _model()
    files = planning.FileInputs(messages=[{"role": "user", "content": "x"}], tokens=10, images=int(model.max_images) + 1)
    with pytest.raises(Exception, match="at most"):
        planning.plan_generation(_request(), model, files=files)


def test_a_full_mode_file_over_the_input_ceiling_is_the_context_length_400():
    model = _model()
    files = planning.FileInputs(messages=[{"role": "user", "content": "x"}], tokens=int(model.max_input_tokens) + 1)
    with pytest.raises(Exception) as refused:
        planning.plan_generation(_request(), model, files=files)
    assert getattr(refused.value, "code", "") == "context_length_exceeded"


# ------------------------------------------------- a generation with a file --


class _CapturingEngine:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.messages: List[List[Dict[str, Any]]] = []

    def __call__(self, messages, **kwargs):
        self.messages.append(messages)
        return self._run()

    async def _run(self):
        for piece in (self.answer[: len(self.answer) // 2], self.answer[len(self.answer) // 2:]):
            await asyncio.sleep(0)
            yield ("token", piece)


@pytest.fixture()
def files_world(platform, tmp_path, monkeypatch):
    """A processed text file of the platform's project in a memory store the
    router's `SqlFileStore()` returns, and a capturing engine."""
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "api-files"))
    monkeypatch.setattr(settings, "public_api_files_ready_poll_s", 0.05)
    project_id = platform["project"]["id"]
    store = service.MemoryFileStore()
    fid = "file-" + secrets.token_hex(12)
    sha = secrets.token_hex(32)
    store.put({
        "id": fid, "project_id": project_id, "blob_id": "blob_" + secrets.token_hex(12), "assembling_upload_id": None,
        "error_code": None, "filename": "memo.txt", "bytes": 64, "expires_at": None, "deleted_at": None,
        "blob_sha256": sha, "blob_kind": "text", "blob_mime_type": "text/plain", "blob_status": "processed",
        "blob_stage": "finalize", "blob_progress": {}, "blob_facts": {}, "blob_error_code": None,
        "blob_video_analysis_id": None,
    })
    write_pages(service.derived_dir_for(project_id, sha), [{"page": 1, "text": "The launch code is AZURE-42."}])
    monkeypatch.setattr(service, "SqlFileStore", lambda *a, **k: store)
    engine = _CapturingEngine("The code is AZURE-42 [memo.txt §1].")
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: {"prompt_tokens": 40, "completion_tokens": 9})
    return {"store": store, "file_id": fid, "engine": engine, "project_id": project_id}


def _file_body(fid, **extra):
    body = {"model": registry.TECHSARA_35B, "input": [{"role": "user", "content": [
        {"type": "input_file", "file_id": fid}, {"type": "input_text", "text": "What is the launch code?"}]}]}
    body.update(extra)
    return body


def _engine_text(messages) -> str:
    out = []
    for m in messages:
        c = m.get("content")
        out.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in c))
    return "\n".join(out)


def test_a_sync_response_with_input_file_sends_the_file_text_and_returns_a_file_citation(api, files_world):
    fid = files_world["file_id"]
    response = api.post("/v1/responses", headers=_auth(), json=_file_body(fid))
    assert response.status_code == 200, response.text
    sent = _engine_text(files_world["engine"].messages[-1])
    assert "<<<BEGIN FILE " + fid in sent and "AZURE-42" in sent
    assert "(attached file)" not in sent
    part = response.json()["output"][0]["content"][0]
    assert part["text"] == "The code is AZURE-42 [memo.txt §1]."
    # `index` is the UTF-16 offset of the marker in the text; `page` the
    # section the model was shown (a text file's unit is §).
    assert part["annotations"] == [
        {"type": "file_citation", "file_id": fid, "filename": "memo.txt", "index": part["text"].index("[memo.txt"), "page": 1}
    ]


def test_chat_completions_with_a_file_part_carries_the_annotations_on_the_message(api, files_world):
    fid = files_world["file_id"]
    body = {"model": registry.TECHSARA_35B, "messages": [{"role": "user", "content": [
        {"type": "file", "file": {"file_id": fid}}, {"type": "text", "text": "What is the launch code?"}]}]}
    response = api.post("/v1/chat/completions", headers=_auth(), json=body)
    assert response.status_code == 200, response.text
    message = response.json()["choices"][0]["message"]
    assert "AZURE-42" in message["content"]
    assert message["annotations"][0]["file_id"] == fid


def test_a_key_without_files_read_is_refused_before_the_file_is_looked_up(api, platform, files_world):
    narrow = _key(platform, "writer only", ["responses.write", "models.read"])
    lookups = files_world["store"].queries
    before = len(lookups)
    response = api.post("/v1/responses", headers={"Authorization": f"Bearer {narrow}"}, json=_file_body(files_world["file_id"]))
    assert response.status_code == 403 and response.json()["error"]["code"] == "insufficient_scope"
    assert len(lookups) == before, "the 403 came before any file lookup"
    assert files_world["engine"].messages == []


def test_another_projects_file_id_is_the_same_404_as_a_missing_one_and_nothing_runs(api, files_world):
    theirs = api.post("/v1/responses", headers=_auth("other"), json=_file_body(files_world["file_id"]))
    missing = api.post("/v1/responses", headers=_auth(), json=_file_body("file-" + "0" * 24))
    for response in (theirs, missing):
        assert response.status_code == 404 and response.json()["error"]["code"] == "file_not_found"
    strip = lambda body: {k: v for k, v in body["error"].items() if k != "request_id"}  # noqa: E731
    assert strip(theirs.json()) == strip(missing.json())
    assert files_world["engine"].messages == []


def test_a_stream_waits_for_a_processing_file_with_progress_comments_then_answers(api, files_world, monkeypatch):
    store, fid, project_id = files_world["store"], files_world["file_id"], files_world["project_id"]
    store.update(project_id, fid, blob_status="processing", blob_stage="index", blob_progress={"percent": 40})

    def finish_later():
        time.sleep(0.6)
        store.update(project_id, fid, blob_status="processed", blob_stage="finalize", blob_progress={"percent": 100})

    threading.Thread(target=finish_later, daemon=True).start()
    with api.stream("POST", "/v1/responses", headers=_auth(), json=_file_body(fid, stream=True)) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    assert f": file {fid} index 40%" in text
    assert text.index(f": file {fid}") < text.index("event: response.created")
    assert "event: response.completed" in text and "AZURE-42" in text
    assert "AZURE-42" in _engine_text(files_world["engine"].messages[-1])


def test_a_file_that_fails_while_a_stream_waits_ends_in_response_failed_not_a_broken_stream(api, files_world):
    store, fid, project_id = files_world["store"], files_world["file_id"], files_world["project_id"]
    store.update(project_id, fid, blob_status="processing", blob_stage="text", blob_progress={})

    def fail_later():
        time.sleep(0.4)
        store.update(project_id, fid, blob_status="failed", blob_error_code="file_corrupt")

    threading.Thread(target=fail_later, daemon=True).start()
    with api.stream("POST", "/v1/responses", headers=_auth(), json=_file_body(fid, stream=True)) as response:
        assert response.status_code == 200
        text = "".join(response.iter_text())
    frames = [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]
    assert [frame["type"] for frame in frames] == ["response.created", "response.failed"]
    assert frames[-1]["response"]["error"]["message"] == service.FAILURE_SENTENCES["file_corrupt"]
    assert files_world["engine"].messages == []


def test_a_stream_naming_a_missing_file_is_a_404_before_the_status_line(api, files_world):
    response = api.post("/v1/responses", headers=_auth(), json=_file_body("file-" + "0" * 24, stream=True))
    assert response.status_code == 404 and response.json()["error"]["code"] == "file_not_found"


def test_a_sync_request_on_a_file_still_processing_waits_inside_the_committed_response_and_answers(api, files_world):
    """No deadline since the committed JSON response (no-timeout wire): the
    synchronous request waits for the file instead of a 409, and answers."""
    store, fid, project_id = files_world["store"], files_world["file_id"], files_world["project_id"]
    store.update(project_id, fid, blob_status="processing")

    def finish_later():
        time.sleep(0.6)
        store.update(project_id, fid, blob_status="processed", blob_stage="finalize")

    threading.Thread(target=finish_later, daemon=True).start()
    response = api.post("/v1/responses", headers=_auth(), json=_file_body(fid))
    assert response.status_code == 200, response.text
    assert "AZURE-42" in response.json()["output"][0]["content"][0]["text"]
    assert response.json()["output"][0]["content"][0]["annotations"][0]["file_id"] == fid


def test_the_usage_row_names_the_file_and_the_citations(api, files_world, monkeypatch):
    from app import usage as usage_ledger

    recorded: List[Dict[str, Any]] = []

    async def capture(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(usage_ledger, "record_async", capture)
    response = api.post("/v1/responses", headers=_auth(), json=_file_body(files_world["file_id"]))
    assert response.status_code == 200
    meta = next(r["meta"] for r in recorded if r["route"] == "v1_responses")
    assert meta["file_ids"] == [files_world["file_id"]]
    assert meta["citations"] == 1 and meta["citations_unresolved"] == 0
    assert meta["file_context_mode"]


def test_a_request_without_file_parts_never_imports_the_files_package_path(api, platform, monkeypatch):
    calls: List[str] = []
    real = file_inputs._service

    def spy():
        calls.append("service")
        return real()

    monkeypatch.setattr(file_inputs, "_service", spy)
    monkeypatch.setattr(llm, "stream_chat_events", _CapturingEngine("hi"))
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    response = api.post("/v1/responses", headers=_auth(), json={"model": registry.TECHSARA_35B, "input": "hello"})
    assert response.status_code == 200
    # One lift (a body with no file parts comes back untouched), nothing more.
    assert calls == ["service"]


def test_inline_file_bytes_are_removed_when_the_request_fails_before_generating(api, platform, tmp_path, monkeypatch):
    import base64

    from app.publicapi import errors as api_errors

    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "api-files"))

    async def refuse(*args, **kwargs):
        raise api_errors.model_unavailable()

    monkeypatch.setattr(public_router, "_claim_row", refuse)
    engine = _CapturingEngine("never")
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    csv = "region,units\n" + "\n".join(f"north,{i}" for i in range(50))
    body = {"model": registry.TECHSARA_35B, "input": [{"role": "user", "content": [
        {"type": "input_file", "file_data": "data:text/csv;base64," + base64.b64encode(csv.encode()).decode(), "filename": "units.csv"},
        {"type": "input_text", "text": "How many rows?"}]}]}
    response = api.post("/v1/responses", headers=_auth(), json=body)
    assert response.status_code == 503
    inline_root = tmp_path / "api-files" / "_inline"
    assert not inline_root.exists() or list(inline_root.iterdir()) == []
    assert engine.messages == []


def test_inline_file_bytes_are_removed_after_a_sync_answer(api, platform, tmp_path, monkeypatch):
    import base64

    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(tmp_path / "api-files"))
    engine = _CapturingEngine("There are 50 rows.")
    monkeypatch.setattr(llm, "stream_chat_events", engine)
    monkeypatch.setattr(llm, "reset_usage", lambda: None)
    monkeypatch.setattr(llm, "get_usage", lambda: None)
    csv = "region,units\n" + "\n".join(f"north,{i}" for i in range(50))
    body = {"model": registry.TECHSARA_35B, "input": [{"role": "user", "content": [
        {"type": "input_file", "file_data": "data:text/csv;base64," + base64.b64encode(csv.encode()).decode(), "filename": "units.csv"},
        {"type": "input_text", "text": "How many rows?"}]}]}
    response = api.post("/v1/responses", headers=_auth(), json=body)
    assert response.status_code == 200, response.text
    assert "north,49" in _engine_text(engine.messages[-1])
    inline_root = tmp_path / "api-files" / "_inline"
    assert not inline_root.exists() or list(inline_root.iterdir()) == []
