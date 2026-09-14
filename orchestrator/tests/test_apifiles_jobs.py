"""The processing runner against a real Postgres, real extraction children and
stub engines (design §4.2-§4.4, §13.4 PROCESSING).

Database: the conftest session database (a PRIVATE one per TEST_DATABASE_URL)
plus `schema.ensure_schema()` once. The conftest's per-test TRUNCATE of ~60
tables is replaced here by a DELETE of the four Files API tables only: the
test Postgres runs with fsync on, and TRUNCATE's per-table file swap is the
slow path there, while these tests touch a handful of rows each.

Engines are stubs: the embedder is a deterministic function, the OCR reader
and gate are in-process fakes. The extraction child is REAL.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from app import db
from app.apifiles import events, extract_worker, ids, jobs, queue, schema, sniff, storage
from app.config import settings
from tests.test_apifiles_extractors import build_pdf, make_docx, page_marker_jpeg


# --- database: one private schema pass, four-table cleanup per test --------
@pytest.fixture(scope="module", autouse=True)
def files_schema(app_database):
    schema.ensure_schema()
    yield


@pytest.fixture
def isolated_app_db(app_database, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_database_url", app_database)
    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    with db.connection() as con:
        for table in ("api_upload_parts", "api_files", "api_uploads", "api_file_blobs"):
            con.execute(f"DELETE FROM {table}")
    yield


@pytest.fixture
def ambient_identity():
    yield None


@pytest.fixture(autouse=True)
def files_root(tmp_path, monkeypatch):
    root = tmp_path / "api-files"
    root.mkdir()
    monkeypatch.setenv("PUBLIC_API_FILES_DIR", str(root))
    events.reset_for_tests()
    yield str(root)


def run(coro):
    return asyncio.run(coro)


# ================================================================ helpers ==


def new_project(name: str = "p") -> Dict[str, str]:
    workspace_id = "ws_" + uuid.uuid4().hex[:12]
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", (workspace_id, name))
    project = db.create_api_project(workspace_id, f"{name}-{uuid.uuid4().hex[:6]}", "live")
    return {"project_id": project["id"], "workspace_id": workspace_id}


def insert_blob(
    tenant: Dict[str, str],
    payload: bytes,
    *,
    kind: str = "unknown",
    lane: str = "cpu",
    status: str = "queued",
    filename: str = "upload.bin",
    progress: Optional[dict] = None,
) -> Dict[str, Any]:
    sha = hashlib.sha256(payload).hexdigest()
    original = storage.original_path(tenant["project_id"], sha)
    os.makedirs(os.path.dirname(original), exist_ok=True)
    with open(original, "wb") as fh:
        fh.write(payload)
    blob_id = ids.new_blob_id()
    with db.connection() as con:
        con.execute(
            "INSERT INTO api_file_blobs (id, project_id, workspace_id, sha256, bytes, kind, lane, status, progress) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (blob_id, tenant["project_id"], tenant["workspace_id"], sha, len(payload), kind, lane, status,
             json.dumps(progress or {})),
        )
        con.execute(
            "INSERT INTO api_files (id, project_id, workspace_id, blob_id, filename, purpose, bytes) "
            "VALUES (%s, %s, %s, %s, %s, 'user_data', %s)",
            (ids.new_file_id(), tenant["project_id"], tenant["workspace_id"], blob_id, filename, len(payload)),
        )
    return schema.get_api_file_blob(blob_id)


def blob(blob_id: str) -> Dict[str, Any]:
    return schema.get_api_file_blob(blob_id)


def set_blob(blob_id: str, sql_set: str, *params: Any) -> None:
    with db.connection() as con:
        con.execute(f"UPDATE api_file_blobs SET {sql_set} WHERE id = %s", (*params, blob_id))


async def stub_embed(texts: Sequence[str]) -> Tuple[List[List[float]], Optional[int]]:
    vectors = []
    for text in texts:
        digest = hashlib.sha256(text.encode()).digest()
        vectors.append([b / 255.0 + 0.01 for b in digest[:8]])
    return vectors, sum(len(t) // 3 for t in texts)


def runner(lane: str = "cpu", **kwargs: Any) -> jobs.JobRunner:
    kwargs.setdefault("run_assemblies", False)
    kwargs.setdefault("emit_webhooks", False)
    kwargs.setdefault("record_usage", False)
    kwargs.setdefault("embed_documents", stub_embed)
    kwargs.setdefault("owner", f"test:{uuid.uuid4().hex[:8]}")
    return jobs.JobRunner(lane, **kwargs)


def docx_bytes(tmp_path) -> bytes:
    path = tmp_path / "doc.docx"
    make_docx(str(path))
    return path.read_bytes()


# ============================================================ claim/lease ==


def test_a_sweep_claims_at_most_one_blob_per_project_so_one_tenant_cannot_starve_another(tmp_path):
    busy, quiet_a, quiet_b = new_project("busy"), new_project("qa"), new_project("qb")
    for n in range(3):
        insert_blob(busy, f"busy {n}".encode())
    insert_blob(quiet_a, b"quiet a")
    insert_blob(quiet_b, b"quiet b")
    gate = asyncio.Event()
    claimed: List[str] = []

    async def hold(ctx):
        claimed.append(ctx.project_id)
        await gate.wait()
        return jobs.StageResult()

    async def scenario():
        r = runner(concurrency=10, stages={"sniff": hold})
        tasks = await r._claim_and_spawn()
        await asyncio.sleep(0.2)
        first_sweep = sorted(claimed)
        gate.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return first_sweep

    first = run(scenario())
    assert sorted(first) == sorted([busy["project_id"], quiet_a["project_id"], quiet_b["project_id"]])


def test_a_lost_lease_stops_the_old_job_at_its_next_unit_and_it_never_writes_over_the_new_holder(tmp_path):
    tenant = new_project()
    row = insert_blob(tenant, b"hello lease")
    async def looping(ctx):
        while True:
            await ctx.checkpoint()
            await asyncio.sleep(0.05)

    async def scenario():
        old = runner(stages={"sniff": looping}, renew_s=0.1, lease_s=30)
        tasks = await old._claim_and_spawn()
        await asyncio.sleep(0.3)
        # The lease lapses (a GC pause, a partition) and another process claims it.
        set_blob(row["id"], "lease_expires_at = now() - interval '1 second'")
        new_owner = "other:process"
        taken = queue.claim_due_blobs("cpu", 5, owner=new_owner, lease_s=30)
        assert [t["id"] for t in taken] == [row["id"]]
        await old.start()  # the renewal loop notices the loss
        result = await asyncio.wait_for(tasks[0], timeout=10)
        await old.stop()
        return result

    result = run(scenario())
    assert result.outcome == "stopped" and result.error_code == "lease_lost"
    after = blob(row["id"])
    assert after["lease_owner"] == "other:process" and after["status"] == "processing"
    assert jobs._update_held(row["id"], "test:someone-else", stage="text") is None


def test_a_sweep_that_finds_this_runners_own_lapsed_lease_never_starts_a_second_job_for_that_blob(tmp_path, monkeypatch):
    # Review finding, 2026-09-13: a DB blip longer than the lease's slack let
    # the claim sweep re-claim a running blob under the same owner — two
    # stage copies, one untracked, so a DELETE's cancel missed it.
    tenant = new_project()
    row = insert_blob(tenant, b"hello world\n" * 10)
    started: List[int] = []
    renewals: List[List[str]] = []
    real_renew = queue.renew_blob_leases

    def counting_renew(blob_ids, **kwargs):
        renewals.append(list(blob_ids))
        return real_renew(blob_ids, **kwargs)

    monkeypatch.setattr(queue, "renew_blob_leases", counting_renew)

    async def scenario():
        release = asyncio.Event()

        async def slow_sniff(ctx):
            started.append(1)
            await release.wait()
            return await jobs.stage_sniff(ctx)

        r = runner(concurrency=2, lease_s=1.0, renew_s=3600.0, stages={"sniff": slow_sniff})
        first = await r._claim_and_spawn()
        await asyncio.sleep(1.3)  # the lease lapses; no renewal ran (renew_s is an hour)
        second = await r._claim_and_spawn()
        await asyncio.sleep(0.3)
        tracked = list(r._jobs)
        release.set()
        results = await asyncio.gather(*first, *second, return_exceptions=True)
        return first, second, tracked, results, r

    first, second, tracked, results, r = run(scenario())
    assert len(first) == 1 and second == [] and started == [1]
    assert tracked == [row["id"]] and r._jobs == {}
    assert [getattr(x, "outcome", x) for x in results] == ["processed"]
    assert renewals == []  # the id check, not a renewal, prevented the duplicate

    async def overdue():
        release = asyncio.Event()

        async def slow_sniff(ctx):
            await release.wait()
            return jobs.StageResult()

        other = insert_blob(new_project("second"), b"renew me before sweeping\n")
        r2 = runner(concurrency=2, lease_s=30.0, renew_s=0.5, stages={"sniff": slow_sniff})
        tasks = await r2._claim_and_spawn()
        await asyncio.sleep(0.7)  # a renewal is overdue and the loop is not running
        await r2._claim_and_spawn()
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        return other

    other = run(overdue())
    assert [other["id"]] in renewals  # renewed BEFORE the sweep, not after


def test_a_text_document_runs_every_stage_in_a_child_and_ends_processed_with_its_derived_files(tmp_path):
    tenant = new_project()
    row = insert_blob(tenant, docx_bytes(tmp_path))
    outcomes = run(runner().run_once())
    assert [o.outcome for o in outcomes] == ["processed"]
    after = blob(row["id"])
    assert after["status"] == "processed" and after["kind"] == "document" and after["lease_owner"] is None
    facts = after["facts"]
    assert facts["sections"] >= 2 and facts["chunks"] >= facts["sections"] and facts["chunks_indexed"] == facts["chunks"]
    assert after["derived_bytes"] > 0
    derived = storage.derived_dir(tenant["project_id"], after["sha256"])
    for name in ("pages.jsonl", "chunks.jsonl", "vectors.f32", "index.json", "text.txt", "pages.json"):
        assert os.path.exists(os.path.join(derived, name)), name
    assert sorted(os.listdir(os.path.join(derived, "stages"))) == [f"{s}.json" for s in sorted(sniff.STAGES_BY_KIND["document"])]
    with open(os.path.join(derived, "text.txt")) as fh:
        assert fh.read().startswith("--- Section 1 ---\nQuarterly review")
    view = jobs.processing_view(after)
    assert view["status"] == "processed" and view["status_details"] is None
    processing = view["processing"]
    assert processing["state"] == "processed" and processing["percent"] == 100
    assert [s["name"] for s in processing["stages"]] == list(sniff.STAGES_BY_KIND["document"])
    assert all(s["status"] == "done" for s in processing["stages"])
    assert processing["derived"] == ["text.txt", "pages.json"]
    assert set(processing["facts"]) <= {"sections", "chars", "estimated_tokens", "chunks", "chunks_indexed", "index_truncated"}


def test_a_scanned_pdf_reads_its_thin_pages_through_the_ocr_gate_and_cites_them_as_ocr(tmp_path):
    pages = [("text", [f"Line {i} of an ordinary text page with plenty of words on it." for i in range(6)]) for _ in range(3)]
    pages.insert(1, ("image", (page_marker_jpeg(2), (400, 566))))
    path = tmp_path / "scan.pdf"
    build_pdf(str(path), pages)
    tenant = new_project()
    row = insert_blob(tenant, path.read_bytes())
    gate_uses: List[str] = []

    class Read:
        status = "ok"
        text = "Escrow agent: Halden Trust."

    async def reader(images):
        assert images[0].startswith("data:image/png;base64,")
        return [Read()]

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def gate():
        gate_uses.append("ocr")
        yield

    outcomes = run(runner(ocr_reader=reader, ocr_gate=gate).run_once())
    assert [o.outcome for o in outcomes] == ["processed"]
    after = blob(row["id"])
    assert after["facts"]["pages"] == 4 and after["facts"]["ocr_pages"] == 1 and gate_uses == ["ocr"]
    derived = storage.derived_dir(tenant["project_id"], after["sha256"])
    with open(os.path.join(derived, "pages.json")) as fh:
        page_two = json.load(fh)[1]
    assert page_two == {"page": 2, "text": "Escrow agent: Halden Trust.", "source": "ocr"}


def test_an_ocr_engine_still_unreachable_on_the_blobs_last_attempt_ends_processed_with_its_text_layers(tmp_path):
    pages = [("text", [f"Line {i} of an ordinary text page with plenty of words on it." for i in range(6)]) for _ in range(2)]
    pages.insert(1, ("image", (page_marker_jpeg(2), (400, 566))))
    path = tmp_path / "scan.pdf"
    build_pdf(str(path), pages)
    tenant = new_project()
    row = insert_blob(tenant, path.read_bytes())

    class Down:
        status = "failed"
        text = ""
        error = "APIConnectionError: Connection error."

    async def reader(images):
        return [Down()]

    first = run(runner(ocr_reader=reader, retry_delay_s=0).run_once())
    assert [o.outcome for o in first] == ["deferred"]  # not the last attempt: wait for the engine
    set_blob(row["id"], "attempt = 4, not_before = now() - interval '1 second'")
    last = run(runner(ocr_reader=reader, retry_delay_s=0).run_once())
    assert [o.outcome for o in last] == ["processed"]
    after = blob(row["id"])
    assert after["status"] == "processed" and after["facts"]["ocr_failed_pages"] == 1
    assert after["facts"]["pages"] == 3 and after["facts"]["ocr_pages"] == 0


def test_a_restart_resumes_at_the_first_stage_without_a_marker_and_never_re_runs_a_finished_one(tmp_path, monkeypatch):
    tenant = new_project()
    row = insert_blob(tenant, docx_bytes(tmp_path))
    child_ops: List[str] = []
    real_run = extract_worker.run

    async def counting_run(spec, **kwargs):
        child_ops.append(spec.op)
        return await real_run(spec, **kwargs)

    monkeypatch.setattr(extract_worker, "run", counting_run)
    reached_chunk = asyncio.Event()

    async def hang(ctx):
        reached_chunk.set()
        await asyncio.Event().wait()

    async def crash_mid_chunk():
        first = runner(stages={"chunk": hang})

        async def die_without_cleanup(ctx, reason):  # a process that dies writes nothing
            return jobs.RunOutcome(ctx.blob_id, "stopped", reason)

        first._stopped = die_without_cleanup  # type: ignore[assignment]
        tasks = await first._claim_and_spawn()
        await asyncio.wait_for(reached_chunk.wait(), timeout=30)
        tasks[0].cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    run(crash_mid_chunk())
    crashed = blob(row["id"])
    assert crashed["status"] == "processing" and crashed["stages"]["text"]["status"] == "done"
    text_ms = crashed["stages"]["text"]["ms"]
    assert child_ops == ["extract"]
    set_blob(row["id"], "lease_expires_at = now() - interval '1 second'")

    outcomes = run(runner().run_once())
    assert [o.outcome for o in outcomes] == ["processed"]
    assert child_ops == ["extract"]  # the text stage was NOT run again
    after = blob(row["id"])
    assert after["stages"]["text"]["ms"] == text_ms
    assert after["progress"]["crashes"] == 1 and "running_owner" not in after["progress"]


def test_an_unavailable_engine_defers_with_backoff_and_the_fifth_deferral_fails_processing_unavailable(tmp_path):
    tenant = new_project()
    row = insert_blob(tenant, b"plain text that will be indexed\n" * 20)

    async def engine_down(ctx):
        raise jobs.Deferred("embedding engine down")

    first = run(runner(stages={"index": engine_down}).run_once())
    assert [o.outcome for o in first] == ["deferred"]
    after = blob(row["id"])
    assert after["status"] == "queued" and after["attempt"] == 1 and after["lease_owner"] is None
    assert after["stages"]["chunk"]["status"] == "done"
    delay = (after["not_before"] - datetime.now(timezone.utc)).total_seconds()
    assert 250 < delay <= 300
    assert run(runner(stages={"index": engine_down}).run_once()) == []  # not due yet

    set_blob(row["id"], "not_before = now() - interval '1 second'")
    for expected_attempt in range(2, 6):
        outcome = run(runner(stages={"index": engine_down}, retry_delay_s=0).run_once())
        assert len(outcome) == 1
        assert blob(row["id"])["attempt"] == expected_attempt
    final = blob(row["id"])
    assert final["status"] == "failed" and final["error_code"] == "processing_unavailable"
    assert outcome[0].outcome == "failed"
    view = jobs.processing_view(final)
    assert view["status"] == "error"
    assert view["status_details"].startswith("Processing could not reach a required service")


def test_a_blob_marked_deleting_stops_its_job_at_the_next_unit_and_nothing_more_is_written(tmp_path):
    tenant = new_project()
    row = insert_blob(tenant, b"soon deleted text\n" * 10)
    ticks: List[int] = []

    async def slow_text(ctx):
        for n in range(200):
            await ctx.checkpoint()
            ticks.append(n)
            await asyncio.sleep(0.05)
        return jobs.StageResult()

    async def scenario():
        r = runner(stages={"text": slow_text})
        tasks = await r._claim_and_spawn()
        while len(ticks) < 3:
            await asyncio.sleep(0.02)
        schema.update_api_file_blob(row["id"], status="deleting")
        return await asyncio.wait_for(tasks[0], timeout=10)

    started = time.monotonic()
    result = run(scenario())
    assert result.outcome == "stopped" and result.error_code == "deleting"
    assert time.monotonic() - started < 5 and len(ticks) < 60
    after = blob(row["id"])
    assert after["status"] == "deleting" and "text" not in {k for k, v in after["stages"].items() if v.get("status") == "done"}


def test_a_shutdown_releases_the_blob_to_queued_without_counting_an_attempt(tmp_path):
    tenant = new_project()
    row = insert_blob(tenant, b"shutdown text\n" * 10)
    started = asyncio.Event()

    async def forever(ctx):
        started.set()
        await asyncio.Event().wait()

    async def scenario():
        r = runner(stages={"text": forever})
        await r.start()
        r.kick()
        await asyncio.wait_for(started.wait(), timeout=20)
        await r.stop()

    run(scenario())
    after = blob(row["id"])
    assert after["status"] == "queued" and after["attempt"] == 0 and after["lease_owner"] is None
    assert after["stages"]["sniff"]["status"] == "done" and "running_owner" not in after["progress"]
    assert [o.outcome for o in run(runner().run_once())] == ["processed"]


def test_an_unsupported_file_ends_as_an_error_with_the_fixed_sentence_and_stays_downloadable(tmp_path):
    tenant = new_project()
    row = insert_blob(tenant, b"\x00\x01\x02binary" * 100)
    outcomes = run(runner().run_once())
    assert [(o.outcome, o.error_code) for o in outcomes] == [("failed", "unsupported_file")]
    after = blob(row["id"])
    view = jobs.processing_view(after)
    assert view["status"] == "error"
    assert view["status_details"] == "This file type cannot be used as model input."
    assert view["processing"]["error"]["code"] == "unsupported_file"
    assert os.path.exists(storage.original_path(tenant["project_id"], after["sha256"]))


def test_a_damaged_pdf_fails_file_corrupt_and_a_too_long_one_names_its_ceiling(tmp_path, monkeypatch):
    tenant = new_project()
    bad = insert_blob(tenant, b"%PDF-1.7\nthis is not really a pdf at all" * 3)
    assert [(o.outcome, o.error_code) for o in run(runner().run_once())] == [("failed", "file_corrupt")]
    pages = [("text", ["page"]) for _ in range(30)]
    path = tmp_path / "long.pdf"
    build_pdf(str(path), pages)
    monkeypatch.setenv("PUBLIC_API_FILES_PDF_MAX_PAGES", "20")
    long = insert_blob(tenant, path.read_bytes())
    assert [(o.outcome, o.error_code) for o in run(runner().run_once())] == [("failed", "file_too_complex")]
    view = jobs.processing_view(blob(long["id"]))
    assert view["status_details"] == "The file exceeds a processing ceiling: more than 20 pages."
    assert blob(bad["id"])["stages"]["text"]["status"] == "failed"


def test_media_bytes_sniffed_in_the_cpu_lane_move_to_the_media_lane_instead_of_being_processed_there(tmp_path):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    tenant = new_project()
    row = insert_blob(tenant, buf.getvalue())
    outcomes = run(runner().run_once())
    assert [o.outcome for o in outcomes] == ["moved"]
    after = blob(row["id"])
    assert after["lane"] == "media" and after["status"] == "queued" and after["kind"] == "audio"
    assert run(runner().run_once()) == []  # the cpu lane never claims it again


def test_a_blob_whose_runs_keep_dying_fails_internal_error_instead_of_being_reclaimed_forever(tmp_path):
    tenant = new_project()
    row = insert_blob(tenant, b"crash loop\n", progress={"running_owner": "dead:1:aa", "crashes": 4})
    outcomes = run(runner().run_once())
    assert [(o.outcome, o.error_code) for o in outcomes] == [("failed", "internal_error")]
    after = blob(row["id"])
    assert after["status"] == "failed" and after["progress"]["crashes"] == 5


def test_the_processing_view_never_names_an_engine_a_url_or_a_path_whatever_the_row_holds(tmp_path):
    poison_values = [
        str(getattr(settings, name))
        for name in dir(settings)
        if name.endswith("_model") and isinstance(getattr(settings, name, None), str) and getattr(settings, name)
    ]
    assert poison_values, "the deployment names at least one model"
    poison = " ".join(poison_values)
    row = {
        "id": "blob_" + "9" * 24,
        "project_id": "proj_" + "9" * 24,
        "sha256": "9" * 64,
        "kind": "video",
        "status": "failed",
        "stage": "vision",
        "error_code": "file_too_complex",
        "stages": {
            "vision": {"status": "running", "percent": 40, "detail": f"{poison} at http://vllm-router:8000/v1"},
            "ocr": {"status": "failed", "detail": "OCR engine http://techsara-ocr:30004 said no"},
        },
        "progress": {
            "stage": "vision",
            "detail": poison,
            "error_ceiling": f"longer than 4 hours via http://internal/{poison}",
            "running_owner": "host:123:abc",
        },
        "facts": {
            "duration_s": 12.5,
            "language": f"en {poison}",
            "format": "http://x",
            "engine": poison,
            "width": "1920 http://x",
            "chunks": 3,
        },
        "started_at": datetime.now(timezone.utc) - timedelta(minutes=3),
        "processed_at": datetime.now(timezone.utc),
    }
    view = jobs.processing_view(row)
    wire = json.dumps(view)
    for value in poison_values:
        assert value not in wire
    assert "http" not in wire and "/data/" not in wire and "host:123" not in wire
    assert view["processing"]["facts"] == {"duration_s": 12.5, "chunks": 3}
    assert view["status_details"] == "The file exceeds a processing ceiling: a processing limit."


def test_a_file_still_assembling_reports_the_assemble_stage_with_its_upload_percent():
    view = jobs.processing_view(None, file_row={"id": "file-" + "1" * 24, "bytes": 1000, "assembly_bytes_done": 250, "blob_id": None})
    assert view["status"] == "uploaded"
    assert (view["processing"]["stage"], view["processing"]["step"], view["processing"]["percent"]) == ("assemble", 0, 25)
    failed = jobs.processing_view(None, file_row={"blob_id": None, "error_code": "checksum_mismatch", "bytes": 10})
    assert failed["status"] == "error"
    assert failed["status_details"] == "The assembled bytes did not match the checksum you supplied."


def test_waiting_for_a_blob_wakes_on_local_progress_and_on_another_processes_row_change(tmp_path, monkeypatch):
    tenant = new_project()
    row = insert_blob(tenant, b"wait for me\n")
    monkeypatch.setattr(events, "POLL_S", 0.2)
    seen: List[dict] = []

    async def on_progress(view):
        seen.append(view)

    async def scenario():
        waiter = asyncio.ensure_future(jobs.wait_until_terminal(row["id"], on_progress=on_progress, timeout_s=None))
        await asyncio.sleep(0.3)
        assert not waiter.done()
        # Another process finishes the blob: only the poll can see it.
        schema.update_api_file_blob(row["id"], status="processed", processed_at=datetime.now(timezone.utc))
        return await asyncio.wait_for(waiter, timeout=5)

    done = run(scenario())
    assert done["status"] == "processed" and seen and seen[0]["state"] == "queued"
    timed = run(jobs.wait_until_terminal(insert_blob(tenant, b"never done\n")["id"], timeout_s=0.3))
    assert timed["status"] == "queued"


# ============================================================ derived data ==


def test_derived_downloads_are_a_closed_allowlist_resolved_from_the_row_never_from_the_name(tmp_path):
    from app.apifiles import derived
    from tests.test_apifiles_extractors import make_xlsx

    path = tmp_path / "book.xlsx"
    make_xlsx(str(path))
    tenant = new_project()
    row = insert_blob(tenant, path.read_bytes())
    assert [o.outcome for o in run(runner().run_once())] == ["processed"]
    after = blob(row["id"])
    assert after["kind"] == "spreadsheet"
    names = [n["name"] for n in derived.list_names(after)]
    assert names == ["profile.json", "text.txt", "sheet-1.csv", "sheet-2.csv"]
    file_path, content_type, size = derived.open_name(after, "sheet-2.csv")
    assert content_type == "text/csv; charset=utf-8" and size == os.path.getsize(file_path)
    assert file_path == os.path.join(storage.derived_dir(tenant["project_id"], after["sha256"]), "sheet-2.csv")
    for hostile in ("../original", "pages.jsonl", "src.xlsx", "stages/sniff.json", "sheet-01.csv", "sheet-1.csv/../../original",
                    "pages.json", "image.png", "transcript.vtt", "", "/etc/passwd"):
        with pytest.raises(derived.NameNotFound):
            derived.open_name(after, hostile)
    with pytest.raises(derived.NameNotFound):
        derived.open_name({**after, "status": "processing"}, "profile.json")
    assert derived.list_names({**after, "status": "processing"}) == []
    # The joined file row (what the routes hold) gives the same view as the blob row.
    file_row = schema.get_api_file(tenant["project_id"], schema.live_files_for_blob(row["id"])[0]["id"])
    assert jobs.file_processing_view(file_row) == jobs.processing_view(after)
    with open(os.path.join(storage.derived_dir(tenant["project_id"], after["sha256"]), "text.txt")) as fh:
        assert fh.read().startswith("--- Rows 1-200 (Sales) ---\nregion\tunits\tsecret_note\n")
    # An allow-listed name that is a symlink is never served or listed.
    planted = os.path.join(storage.derived_dir(tenant["project_id"], after["sha256"]), "sheet-2.csv")
    os.unlink(planted)
    os.symlink("/etc/passwd", planted)
    with pytest.raises(derived.NameNotFound):
        derived.open_name(after, "sheet-2.csv")
    assert "sheet-2.csv" not in [n["name"] for n in derived.list_names(after)]


def test_an_image_is_decoded_and_given_its_variants_in_the_cpu_lane(tmp_path):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (1800, 900), (10, 20, 30, 128)).save(buf, format="PNG")
    tenant = new_project()
    row = insert_blob(tenant, buf.getvalue())
    assert [o.outcome for o in run(runner().run_once())] == ["processed"]
    after = blob(row["id"])
    view = jobs.processing_view(after)["processing"]
    assert view["kind"] == "image" and view["facts"] == {"width": 1800, "height": 900, "format": "PNG"}
    assert view["derived"] == ["image.png"]
    from app.apifiles import derived

    assert derived.image_variant(after, "low")[0].endswith("image_896.png")
    assert derived.image_variant(after, "high")[0].endswith("image_1600.png")


def test_starting_the_module_installs_the_enqueue_hook_so_a_new_blob_runs_without_waiting_for_a_poll(tmp_path):
    tenant = new_project()

    async def scenario():
        await jobs.start(poll_s=60, run_assemblies=False, emit_webhooks=False, record_usage=False, embed_documents=stub_embed)
        try:
            await asyncio.sleep(0.3)  # the first sweep finds nothing and parks for 60 s
            row = insert_blob(tenant, b"arrives after start\n" * 5)
            queue._notify_enqueued(row)  # what ingest does after it commits a blob
            for _ in range(200):
                if blob(row["id"])["status"] == "processed":
                    return row
                await asyncio.sleep(0.05)
            raise AssertionError("the kick did not wake the lane")
        finally:
            await jobs.stop()

    run(scenario())
    assert queue._enqueue_hook is None


# ============================================================== media lane ==


def test_an_audio_blob_follows_its_api_lane_analysis_relays_bare_stage_names_and_indexes_the_transcript(tmp_path, monkeypatch):
    """The pipeline itself is team MEDIA's and needs ffmpeg and whisper; here
    `media.start_media_analysis` is stubbed to do what it does to the rows,
    and the pipeline's progress events — with an engine-naming `detail`, as
    the real pipeline sends — are published on its real bus."""
    from app.apifiles import derived, media
    from app.video import media as vmedia
    from app.video import pipeline, store

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    tenant = new_project()
    row = insert_blob(tenant, buf.getvalue(), kind="audio", lane="media")
    content_hash = storage.api_video_hash(tenant["project_id"], row["sha256"])
    with db.connection() as con:
        con.execute("DELETE FROM video_analyses WHERE content_hash = %s", (content_hash,))
    analysis = db.upsert_video_analysis(content_hash, len(buf.getvalue()), "audio/wav", "media.wav")
    os.makedirs(store.analysis_dir(content_hash), exist_ok=True)
    store.write_json(
        store.stage_path(content_hash, "transcript.json"),
        {"segments": [{"start": 0.0, "end": 4.0, "text": "the deployment window moves to Thursday the ninth"},
                      {"start": 50.0, "end": 55.0, "text": "checkpoint bravo nine zero eight two"}]},
    )
    os.makedirs(store.artifacts_dir(content_hash), exist_ok=True)
    with open(os.path.join(store.artifacts_dir(content_hash), "transcript.txt"), "w") as fh:
        fh.write("t" * 5000)
    engine_detail = f"{settings.ocr_model} at http://vllm-ocr:8000/v1"

    async def fake_start(**kwargs):
        schema.update_api_file_blob(kwargs["blob_id"], video_analysis_id=int(analysis["id"]), kind="audio")

        async def pipeline_runs():
            await asyncio.sleep(0.2)
            pipeline._publish(int(analysis["id"]), {"stage": "transcript", "status": "running", "percent": 40, "detail": engine_detail})
            await asyncio.sleep(0.2)
            db.update_video_analysis(int(analysis["id"]), status="done", language="en")
            pipeline._publish(int(analysis["id"]), {"stage": "_done", "status": "done", "detail": engine_detail})

        asyncio.ensure_future(pipeline_runs())
        probe = vmedia.Probe(duration_s=65.5, has_video=False, has_audio=True, width=0, height=0, fps=0.0,
                             video_codec="", audio_codec="pcm_s16le", container="wav", bytes=len(buf.getvalue()))
        return media.MediaStart(analysis_id=int(analysis["id"]), content_hash=content_hash, kind="audio", probe=probe)

    monkeypatch.setattr(media, "start_media_analysis", fake_start)
    monkeypatch.setattr(jobs, "MEDIA_POLL_S", 0.1)

    async def no_real_pipeline(analysis_id):  # no ffmpeg or whisper in this suite
        return True

    monkeypatch.setattr(pipeline, "ensure_running", no_real_pipeline)
    seen: List[dict] = []

    async def scenario():
        with events.subscription(row["id"]) as bus:
            outcomes = await runner("media").run_once()
            while not bus.empty():
                seen.append(bus.get_nowait())
        return outcomes

    outcomes = run(scenario())
    after = blob(row["id"])
    assert [o.outcome for o in outcomes] == ["processed"], (after["error_code"], after["stages"], after["kind"])
    view = jobs.processing_view(after)
    wire = json.dumps(view) + json.dumps(seen)
    assert settings.ocr_model not in wire and "http" not in wire
    processing = view["processing"]
    assert [s["name"] for s in processing["stages"]] == list(sniff.STAGES_BY_KIND["audio"])
    assert processing["facts"]["duration_s"] == 65.5 and processing["facts"]["language"] == "en"
    assert processing["facts"]["has_video"] is False and processing["facts"]["chunks_indexed"] >= 1
    assert after["stages"]["probe"]["status"] == "done" and after["stages"]["fusion"]["status"] == "done"
    assert any(e.get("stage") == "transcript" and e.get("percent") == 40 for e in seen)
    table = list(derived.read_chunks(after))
    assert any("Thursday" in c.text for c in table) and all(c.modality == "speech" for c in table)
    # Processed in ONE run, the file still lists its analysis's derived names
    # and counts their bytes (review finding, 2026-09-13: derived was []).
    assert "transcript.txt" in processing["derived"]
    assert after["derived_bytes"] == derived.derived_bytes(after) and after["derived_bytes"] >= 5000


def test_an_xlsx_zip_bomb_uploaded_as_a_file_fails_too_complex_in_seconds_and_writes_no_sheet(tmp_path):
    from tests.test_apifiles_extractors import zip_bomb

    bomb = tmp_path / "bomb.xlsx"
    zip_bomb(str(bomb), {"xl/workbook.xml": b"<workbook/>", "[Content_Types].xml": b"<Types/>"},
             "xl/worksheets/sheet1.xml", 512 * 1024 * 1024)
    tenant = new_project()
    row = insert_blob(tenant, bomb.read_bytes())
    started = time.monotonic()
    outcomes = run(runner().run_once())
    elapsed = time.monotonic() - started
    assert [(o.outcome, o.error_code) for o in outcomes] == [("failed", "file_too_complex")]
    after = blob(row["id"])
    assert after["kind"] == "spreadsheet" and after["stages"]["sheets"]["status"] == "failed"
    derived_dir = storage.derived_dir(tenant["project_id"], after["sha256"])
    assert not [n for n in os.listdir(derived_dir) if n.startswith("sheet-")]
    assert jobs.processing_view(after)["status_details"] == (
        "The file exceeds a processing ceiling: it expands beyond the decompression ceiling."
    )
    print(f"{bomb.stat().st_size:,} B xlsx declaring 512 MiB refused in {elapsed:.2f}s")
    assert elapsed < 15
