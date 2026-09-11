"""video/pipeline.py — a model outage defers the job, it does not strand it.

Before 2026-09-11 a required stage that could not reach the engine stamped
the row `failed`, and nothing re-ran a failed row (the drain only lists
`queued`). The sweep the incident report describes lost 10 of 101 jobs that
way. Now: a required stage that gives up waiting (ModelUnavailable /
ASRUnavailable) puts the row back in the queue with its finished stages
kept, bounded by VIDEO_MAX_ATTEMPTS; the drain leaves a freshly deferred row
alone for VIDEO_RETRY_DELAY_S; an optional stage fails soft; an OCR outage is
no longer cached as "no readable text".
"""
from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
import openai

from app import asr, db, resilience
from app.config import settings


def _seed(tmp_path, monkeypatch, content_hash: str):
    from app.video import store

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    os.makedirs(store.analysis_dir(content_hash), exist_ok=True)
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 64)
    store.adopt_source(content_hash, str(src), "clip.mp4")
    return db.upsert_video_analysis(content_hash, 64, "video/mp4", "clip.mp4")


def _unavailable() -> resilience.ModelUnavailable:
    last = openai.APIConnectionError(request=httpx.Request("POST", "http://vllm:8000/v1"))
    return resilience.ModelUnavailable("http://vllm:8000/v1", 1200.0, 7, last)


def _fns(pipeline, ran, overrides):
    from app.video import store

    def make(name, status="done"):
        async def run(ctx, progress):
            ran.append(name)
            # A real stage writes its output file; the resume's cache check
            # is "status done AND the file exists".
            output = pipeline._OUTPUTS.get(name)
            if output and status == "done":
                store.write_json(store.stage_path(ctx.content_hash, output), {"stub": name})
            return pipeline._StageResult(status, name)
        return run

    fns = {s: make(s) for s in pipeline.STAGES}
    fns.update(overrides)
    return fns


def test_a_required_stage_that_cannot_reach_the_model_defers_the_job(monkeypatch, tmp_path):
    from app.video import pipeline

    row = _seed(tmp_path, monkeypatch, "f" * 64)
    ran: list[str] = []

    async def fusion_down(ctx, progress):
        ran.append("fusion")
        raise _unavailable()

    monkeypatch.setattr(pipeline, "_STAGE_FNS", _fns(pipeline, ran, {"fusion": fusion_down}))
    windows: list[float] = []
    real_window = pipeline.recovery_window

    def spy(seconds):
        windows.append(seconds)
        return real_window(seconds)

    monkeypatch.setattr(pipeline, "recovery_window", spy)
    asyncio.run(pipeline._run(row["id"]))
    fresh = db.get_video_analysis(row["id"])
    # Back in the queue, not failed; the finished stages are kept; the
    # deferred stage is NOT stamped done so it re-runs next time.
    assert fresh["status"] == "queued"
    assert fresh["error"].startswith(pipeline.DEFERRED_MARK)
    assert "attempt 1" in fresh["error"]
    assert fresh["attempt"] == 1
    assert fresh["finished_at"] is None
    assert fresh["stages"]["transcript"]["status"] == "done"
    assert fresh["stages"]["fusion"]["status"] == "deferred"
    assert "index" not in ran and "artifacts" not in ran
    # The job ran under the LONG window: a background job waits out a reload.
    assert windows == [settings.llm_recovery_window_s]
    db.delete_video_analysis(row["id"])


def test_a_deferred_job_resumes_from_its_finished_stages(monkeypatch, tmp_path):
    from app.video import pipeline

    row = _seed(tmp_path, monkeypatch, "a" * 64)
    ran: list[str] = []
    down = {"fusion": True}

    async def fusion(ctx, progress):
        ran.append("fusion")
        if down["fusion"]:
            raise _unavailable()
        return pipeline._StageResult("done", "fused")

    monkeypatch.setattr(pipeline, "_STAGE_FNS", _fns(pipeline, ran, {"fusion": fusion}))
    asyncio.run(pipeline._run(row["id"]))
    assert db.get_video_analysis(row["id"])["status"] == "queued"
    first_pass = list(ran)
    ran.clear()
    down["fusion"] = False
    asyncio.run(pipeline._run(row["id"]))
    fresh = db.get_video_analysis(row["id"])
    assert fresh["status"] == "done" and fresh["error"] == ""
    assert fresh["attempt"] == 2
    # Only the deferred stage and what follows it ran the second time; the
    # prelude and the two branches came from the earlier run's files.
    assert "probe" in first_pass and "transcript" in first_pass
    assert ran == ["fusion", "index", "artifacts"]
    db.delete_video_analysis(row["id"])


def test_deferral_is_bounded_by_video_max_attempts(monkeypatch, tmp_path):
    from app.video import pipeline

    monkeypatch.setattr(settings, "video_max_attempts", 2)
    row = _seed(tmp_path, monkeypatch, "b" * 64)
    ran: list[str] = []

    async def fusion_down(ctx, progress):
        ran.append("fusion")
        raise _unavailable()

    monkeypatch.setattr(pipeline, "_STAGE_FNS", _fns(pipeline, ran, {"fusion": fusion_down}))
    asyncio.run(pipeline._run(row["id"]))
    assert db.get_video_analysis(row["id"])["status"] == "queued"
    asyncio.run(pipeline._run(row["id"]))
    fresh = db.get_video_analysis(row["id"])
    assert fresh["status"] == "failed"
    assert "gave up after 2 attempts" in fresh["error"]
    db.delete_video_analysis(row["id"])


def test_an_asr_outage_on_the_transcript_stage_defers_too(monkeypatch, tmp_path):
    from app.video import pipeline

    row = _seed(tmp_path, monkeypatch, "d" * 64)
    ran: list[str] = []

    async def transcript_down(ctx, progress):
        ran.append("transcript")
        raise asr.ASRUnavailable("both engines standing down")

    monkeypatch.setattr(pipeline, "_STAGE_FNS", _fns(pipeline, ran, {"transcript": transcript_down}))
    asyncio.run(pipeline._run(row["id"]))
    fresh = db.get_video_analysis(row["id"])
    assert fresh["status"] == "queued" and fresh["error"].startswith(pipeline.DEFERRED_MARK)
    # The screen branch still ran to the end: its files are kept for the resume.
    assert {"frames", "ocr", "vision"} <= set(ran)
    db.delete_video_analysis(row["id"])


def test_an_optional_stage_that_cannot_reach_its_engine_fails_soft(monkeypatch, tmp_path):
    from app.video import pipeline

    row = _seed(tmp_path, monkeypatch, "0" * 64)
    ran: list[str] = []

    async def vision_down(ctx, progress):
        ran.append("vision")
        raise _unavailable()

    monkeypatch.setattr(pipeline, "_STAGE_FNS", _fns(pipeline, ran, {"vision": vision_down}))
    asyncio.run(pipeline._run(row["id"]))
    fresh = db.get_video_analysis(row["id"])
    assert fresh["status"] == "done"
    assert fresh["stages"]["vision"]["status"] == "failed"
    assert "fusion" in ran and "artifacts" in ran
    db.delete_video_analysis(row["id"])


def test_drain_leaves_a_freshly_deferred_row_alone_but_not_a_new_one(monkeypatch, tmp_path):
    from app.video import pipeline

    monkeypatch.setattr(settings, "video_retry_delay_s", 300.0)
    now = datetime.now(timezone.utc)
    just_deferred = {"error": f"{pipeline.DEFERRED_MARK} (attempt 1): x", "updated_at": now.isoformat()}
    old_deferral = {"error": f"{pipeline.DEFERRED_MARK} (attempt 1): x", "updated_at": (now - timedelta(seconds=301)).isoformat()}
    fresh_upload = {"error": "", "updated_at": now.isoformat()}
    assert pipeline._deferred_too_recently(just_deferred) is True
    assert pipeline._deferred_too_recently(old_deferral) is False
    assert pipeline._deferred_too_recently(fresh_upload) is False

    started: list[int] = []

    async def fake_ensure(analysis_id):
        started.append(analysis_id)
        return True

    monkeypatch.setattr(pipeline, "ensure_running", fake_ensure)
    monkeypatch.setattr(
        db, "list_video_analyses",
        lambda status, limit=20: [{"id": 1, **just_deferred}, {"id": 2, **old_deferral}, {"id": 3, **fresh_upload}],
    )
    assert asyncio.run(pipeline.drain_queue()) == 2
    assert started == [2, 3]


def test_an_ocr_outage_is_not_cached_as_no_readable_text(monkeypatch, tmp_path):
    from app.video import pipeline, screen, store

    monkeypatch.setattr(settings, "video_data_dir", str(tmp_path / "video"))
    monkeypatch.setattr(settings, "ocr_enabled", True)
    monkeypatch.setattr(settings, "video_ocr_enabled", True)
    content_hash = "9" * 64
    os.makedirs(store.analysis_dir(content_hash), exist_ok=True)

    class Kept:
        def __init__(self, i):
            self.index, self.t_s, self.end_s, self.path = i, float(i), float(i) + 1.0, f"/nowhere/{i}.png"

    monkeypatch.setattr(pipeline, "_kept_frames", lambda ctx: [Kept(0), Kept(1)])

    async def unreachable(frames, *, progress=None):
        return screen.ScreenRead(["", ""], [], {"status": "unavailable", "detail": "APIConnectionError: connection refused", "unread": 2})

    monkeypatch.setattr(screen, "read_frames", unreachable)
    ctx = pipeline._Ctx(row={}, content_hash=content_hash, source=str(tmp_path / "clip.mp4"), counts={})

    async def quiet(percent, detail):
        return None

    out = asyncio.run(pipeline._stage_ocr(ctx, quiet))
    assert out.status == "failed" and "unavailable" in out.detail
    # No ocr.json was written: the next run reads the frames again.
    assert not os.path.exists(store.stage_path(content_hash, "ocr.json"))
