"""Files as model input — readiness when a referenced file is still
processing (design §5.2). A virtual clock replaces the 30 s wait: no test
here sleeps for real."""
from __future__ import annotations

import asyncio
from typing import List

import pytest

from app.apifiles import service
from app.publicapi import errors
from tests.test_publicapi_file_inputs import MAIN, PROJECT, file_id, request_id, responses_body, row


class VirtualTime:
    """`clock` and `sleep` for `service.prepare`; `on_sleep` lets a test move
    processing forward while the request waits."""

    def __init__(self, on_sleep=None) -> None:
        self.now = 1000.0
        self.sleeps: List[float] = []
        self.on_sleep = on_sleep

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(self)
        await asyncio.sleep(0)


def _lifted(*fids: str, part: str = "input_file"):
    return service.lift_file_parts(responses_body(*[{"type": part, "file_id": f} for f in fids]), dialect="responses")


def test_a_sync_request_waits_thirty_seconds_then_gets_409_file_not_ready_with_retry_after_five_for_documents():
    async def scenario():
        store = service.MemoryFileStore()
        fid = file_id()
        store.put(row(PROJECT, fid, status="processing", blob_stage="ocr", blob_progress={"percent": 40}))
        time = VirtualTime()
        with pytest.raises(errors.ApiError) as refused:
            await service.prepare(_lifted(fid), project_id=PROJECT, store=store, caps=MAIN, delivery="sync",
                                  request_id=request_id(), sleep=time.sleep, clock=time.clock)
        assert refused.value.status == 409 and refused.value.code == "file_not_ready"
        assert refused.value.headers() == {"Retry-After": "5"}
        assert refused.value.param == "input.0.content.0.file_id"
        assert time.now - 1000.0 == pytest.approx(30.0), "the whole readiness budget, and not a second more"
        assert max(time.sleeps) <= 1.0, "rows are re-read every second"

    asyncio.run(scenario())


def test_the_sync_retry_after_is_thirty_seconds_when_a_pending_file_is_audio_or_video():
    async def scenario():
        store = service.MemoryFileStore()
        doc, clip = file_id(), file_id()
        store.put(row(PROJECT, doc, status="processed"))
        store.put(row(PROJECT, clip, kind="video", status="processing", blob_mime_type="video/mp4"))
        time = VirtualTime()
        with pytest.raises(errors.ApiError) as refused:
            await service.prepare(_lifted(doc, clip), project_id=PROJECT, store=store, caps=MAIN, delivery="sync",
                                  request_id=request_id(), sleep=time.sleep, clock=time.clock)
        assert refused.value.headers() == {"Retry-After": "30"}
        assert refused.value.param == "input.0.content.1.file_id", "the part still waiting is named"
        # Still assembling from an upload: no blob, so no kind yet — the text-kind Retry-After.
        assembling = file_id()
        store.put(row(PROJECT, assembling, blob_id=None, assembling_upload_id="upload_" + "0" * 24,
                      blob_kind=None, blob_status=None, blob_mime_type=None))
        with pytest.raises(errors.ApiError) as still:
            await service.prepare(_lifted(assembling), project_id=PROJECT, store=store, caps=MAIN, delivery="sync",
                                  request_id=request_id(), sync_wait_s=0.0, sleep=time.sleep, clock=time.clock)
        assert still.value.code == "file_not_ready" and still.value.headers() == {"Retry-After": "5"}

    asyncio.run(scenario())


def test_a_sync_request_proceeds_the_moment_processing_finishes_inside_the_wait(tmp_path, monkeypatch):
    from app.config import settings

    async def scenario():
        monkeypatch.setattr(settings, "public_api_files_dir", str(tmp_path), raising=False)
        store = service.MemoryFileStore()
        fid = file_id()
        sha = "d" * 64
        store.put(row(PROJECT, fid, sha=sha, kind="text", status="queued"))
        from tests.test_apifiles_vectors import write_pages

        write_pages(service.derived_dir_for(PROJECT, sha), [{"page": 1, "text": "ready now"}])

        def advance(t: VirtualTime) -> None:
            if t.now - 1000.0 >= 7.0:
                store.update(PROJECT, fid, blob_status="processed")

        time = VirtualTime(on_sleep=advance)
        prepared = await service.prepare(_lifted(fid), project_id=PROJECT, store=store, caps=MAIN, delivery="sync",
                                         request_id=request_id(), sleep=time.sleep, clock=time.clock)
        assert prepared.files_wait_s == pytest.approx(7.0)
        assert prepared.usage_meta()["files_wait_s"] == pytest.approx(7.0)
        assert "ready now" in prepared.context.blocks[fid][0]["text"]

    asyncio.run(scenario())


def test_a_stream_waits_past_any_deadline_and_reports_each_stage_change_as_a_comment_once(tmp_path, monkeypatch):
    from app.config import settings

    async def scenario():
        monkeypatch.setattr(settings, "public_api_files_dir", str(tmp_path), raising=False)
        store = service.MemoryFileStore()
        fid = file_id()
        sha = "e" * 64
        store.put(row(PROJECT, fid, sha=sha, kind="text", status="queued", blob_stage="sniff"))
        from tests.test_apifiles_vectors import write_pages

        write_pages(service.derived_dir_for(PROJECT, sha), [{"page": 1, "text": "finally"}])
        script = {60: ("processing", "text", 10), 600: ("processing", "text", 10), 3600: ("processing", "index", 90), 7200: ("processed", "finalize", 100)}

        def advance(t: VirtualTime) -> None:
            elapsed = int(t.now - 1000.0)
            if elapsed in script:
                status, stage, percent = script[elapsed]
                store.update(PROJECT, fid, blob_status=status, blob_stage=stage, blob_progress={"percent": percent})

        time = VirtualTime(on_sleep=advance)
        comments: List[str] = []

        async def on_progress(text: str) -> None:
            comments.append(text)

        prepared = await service.prepare(_lifted(fid), project_id=PROJECT, store=store, caps=MAIN, delivery="stream",
                                         request_id=request_id(), on_progress=on_progress, sleep=time.sleep, clock=time.clock)
        assert time.now - 1000.0 == pytest.approx(7200.0), "two hours of processing, no deadline"
        assert comments == [f"file {fid} queued", f"file {fid} text 10%", f"file {fid} index 90%"]
        assert prepared.context.blocks[fid]

    asyncio.run(scenario())


def test_progress_comments_carry_only_closed_stage_names_never_text_from_the_stage_column():
    record = service.record_from_row(row(PROJECT, file_id(), status="processing",
                                         blob_stage="ocr engine http://10.0.0.7:8000 failed", blob_progress={"percent": 250}))
    text = service.progress_comment(record)
    assert text.endswith(" processing 100%") and "http" not in text and "10.0.0.7" not in text


def test_a_background_job_waits_with_no_deadline_and_stops_at_once_when_abandoned():
    async def scenario():
        store = service.MemoryFileStore()
        fid = file_id()
        store.put(row(PROJECT, fid, kind="video", status="processing"))
        abandon = asyncio.Event()

        def cancel_after_three_hours(t: VirtualTime) -> None:
            if t.now - 1000.0 >= 3 * 3600:
                abandon.set()

        time = VirtualTime(on_sleep=cancel_after_three_hours)
        with pytest.raises(asyncio.CancelledError):
            await service.prepare(_lifted(fid, part="input_video"), project_id=PROJECT, store=store, caps=MAIN,
                                  delivery="background", request_id=request_id(), abandon=abandon,
                                  sleep=time.sleep, clock=time.clock)
        assert time.now - 1000.0 == pytest.approx(3 * 3600), "no 409 however long it takes; the cancel ends it"

    asyncio.run(scenario())


def test_the_no_deadline_sync_wait_for_the_committed_json_response_waits_like_a_stream():
    async def scenario():
        store = service.MemoryFileStore()
        fid = file_id()
        store.put(row(PROJECT, fid, status="processing"))

        def finish(t: VirtualTime) -> None:
            if t.now - 1000.0 >= 120:
                store.update(PROJECT, fid, blob_status="failed", blob_error_code="file_corrupt")

        time = VirtualTime(on_sleep=finish)
        with pytest.raises(errors.ApiError) as refused:
            await service.prepare(_lifted(fid), project_id=PROJECT, store=store, caps=MAIN, delivery="sync",
                                  request_id=request_id(), sync_wait_s=service.NO_DEADLINE, sleep=time.sleep, clock=time.clock)
        assert refused.value.status == 400 and time.now - 1000.0 >= 120

    asyncio.run(scenario())


@pytest.mark.parametrize("fields, sentence", [
    ({"blob_status": "failed", "blob_error_code": "file_corrupt"}, "The file could not be read; it may be damaged or encrypted."),
    ({"blob_status": "failed", "blob_error_code": "processing_unavailable"}, "Processing could not reach a required service"),
    ({"blob_status": "failed", "blob_error_code": None}, "Something went wrong while processing this file."),
    ({"blob_status": "processed", "blob_kind": "unsupported"}, "This file type cannot be used as model input."),
    ({"blob_id": None, "error_code": "checksum_mismatch", "blob_status": None}, "The assembled bytes did not match the checksum"),
])
def test_a_failed_or_unsupported_file_is_refused_at_once_with_its_fixed_sentence(fields, sentence):
    async def scenario():
        store = service.MemoryFileStore()
        fid = file_id()
        store.put(row(PROJECT, fid, **fields))
        time = VirtualTime()
        with pytest.raises(errors.ApiError) as refused:
            await service.prepare(_lifted(fid), project_id=PROJECT, store=store, caps=MAIN, delivery="stream",
                                  request_id=request_id(), sleep=time.sleep, clock=time.clock)
        assert refused.value.status == 400 and refused.value.code == "invalid_request_error"
        assert refused.value.message.startswith(sentence) and refused.value.param == "input.0.content.0.file_id"
        assert time.sleeps == [], "no waiting on a file that will never be ready"

    asyncio.run(scenario())


def test_a_file_deleted_while_the_request_waits_becomes_the_same_404_as_a_missing_one():
    async def scenario():
        from datetime import datetime, timezone

        store = service.MemoryFileStore()
        fid = file_id()
        store.put(row(PROJECT, fid, status="processing"))

        def delete(t: VirtualTime) -> None:
            if t.now - 1000.0 >= 3:
                store.update(PROJECT, fid, deleted_at=datetime.now(timezone.utc))

        time = VirtualTime(on_sleep=delete)
        with pytest.raises(errors.ApiError) as gone:
            await service.prepare(_lifted(fid), project_id=PROJECT, store=store, caps=MAIN, delivery="stream",
                                  request_id=request_id(), sleep=time.sleep, clock=time.clock)
        with pytest.raises(errors.ApiError) as missing:
            await service.prepare(_lifted(file_id()), project_id=PROJECT, store=store, caps=MAIN, delivery="stream",
                                  request_id=request_id(), sleep=time.sleep, clock=time.clock)
        assert gone.value.envelope("") == missing.value.envelope("") and gone.value.status == 404

    asyncio.run(scenario())


def test_a_real_wait_notices_a_cancel_inside_the_poll_interval_rather_than_after_it():
    async def scenario():
        import time as wall

        store = service.MemoryFileStore()
        fid = file_id()
        store.put(row(PROJECT, fid, status="processing"))
        abandon = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, abandon.set)
        started = wall.monotonic()
        with pytest.raises(asyncio.CancelledError):
            await service.prepare(_lifted(fid), project_id=PROJECT, store=store, caps=MAIN, delivery="background",
                                  request_id=request_id(), abandon=abandon)
        assert wall.monotonic() - started < 0.5, "the 1 s poll does not delay a cancel"

    asyncio.run(scenario())


# ------------------------------------- one deadline for a sync prepare --


def test_a_sync_readiness_wait_also_stops_at_the_requests_whole_prepare_budget():
    async def scenario():
        store = service.MemoryFileStore()
        fid = file_id()
        store.put(row(PROJECT, fid, status="processing"))
        time = VirtualTime()
        with pytest.raises(errors.ApiError) as refused:
            await service.prepare(_lifted(fid), project_id=PROJECT, store=store, caps=MAIN, delivery="sync",
                                  request_id=request_id(), sleep=time.sleep, clock=time.clock, sync_budget_s=12.0)
        assert refused.value.code == "file_not_ready"
        assert time.now - 1000.0 == pytest.approx(12.0), "the budget, not the 30 s readiness setting"
        assert service.sync_prepare_budget_s() == 45.0

    asyncio.run(scenario())


def _retrieval_file(tmp_path, monkeypatch):
    from app.apifiles import chunks
    from app.config import settings
    from tests.test_apifiles_vectors import HashEmbedder, write_pages

    monkeypatch.setattr(settings, "public_api_files_dir", str(tmp_path), raising=False)
    store = service.MemoryFileStore()
    fid = file_id()
    sha = "e" * 64
    store.put(row(PROJECT, fid, sha=sha, kind="text", facts={"estimated_tokens": 400}))
    derived = service.derived_dir_for(PROJECT, sha)
    write_pages(derived, [{"page": p, "text": f"Section {p} talks about the forecast budget for region {p}."} for p in range(1, 40)])
    chunks.build_text_chunks(derived)
    embedder = HashEmbedder()
    body = responses_body({"type": "input_file", "file_id": fid}, text="What is the forecast budget for region 7?")
    body["file_context"] = {"mode": "retrieval"}
    return store, fid, derived, embedder, service.lift_file_parts(body, dialect="responses")


def test_a_slow_embed_or_rerank_on_a_sync_request_is_abandoned_at_the_deadline_and_retrieval_still_answers(tmp_path, monkeypatch):
    import time as real_time

    from app.apifiles import vectors

    async def scenario():
        store, fid, derived, embedder, lifted = _retrieval_file(tmp_path, monkeypatch)
        await vectors.build_index(derived, embed_documents=embedder.documents)
        calls: List[str] = []

        async def slow_embed(question):
            calls.append("embed")
            await asyncio.sleep(30)

        async def slow_rerank(question, documents):
            calls.append("rerank")
            await asyncio.sleep(30)

        monkeypatch.setattr(service, "MIN_OPTIONAL_S", 0.05)
        started = real_time.monotonic()
        prepared = await service.prepare(lifted, project_id=PROJECT, store=store, caps=MAIN, delivery="sync",
                                         request_id=request_id(), sync_budget_s=0.6,
                                         engines=service.Engines(embed_query=slow_embed, rerank=slow_rerank))
        elapsed = real_time.monotonic() - started
        assert elapsed < 3.0, elapsed
        meta = prepared.usage_meta()
        assert meta["retrieval"] == "lexical" and calls[0] == "embed"
        assert "region 7" in prepared.context.blocks[fid][0]["text"]
        # With time left the rerank is attempted and abandoned at the deadline.
        calls.clear()
        started = real_time.monotonic()
        prepared = await service.prepare(lifted, project_id=PROJECT, store=store, caps=MAIN, delivery="sync",
                                         request_id=request_id(), sync_budget_s=0.6,
                                         engines=service.Engines(embed_query=embedder.query, rerank=slow_rerank))
        assert real_time.monotonic() - started < 3.0
        assert calls == ["rerank"] and prepared.usage_meta()["rerank"] == "skipped"
        # A stream has no deadline: the same slow engines are simply awaited.
        async def slightly_slow_embed(question):
            await asyncio.sleep(0.8)
            return await embedder.query(question)

        streamed = await service.prepare(lifted, project_id=PROJECT, store=store, caps=MAIN, delivery="stream",
                                         request_id=request_id(), sync_budget_s=0.6,
                                         engines=service.Engines(embed_query=slightly_slow_embed))
        assert streamed.usage_meta()["retrieval"] == "vector"

    asyncio.run(scenario())


def test_the_committed_json_response_lifts_the_prepare_deadline_along_with_the_readiness_wait(tmp_path, monkeypatch):
    from app.apifiles import vectors

    async def scenario():
        store, fid, derived, embedder, lifted = _retrieval_file(tmp_path, monkeypatch)
        await vectors.build_index(derived, embed_documents=embedder.documents)
        monkeypatch.setattr(service, "sync_prepare_budget_s", lambda: 0.2)

        async def slightly_slow_embed(question):
            await asyncio.sleep(0.5)
            return await embedder.query(question)

        engines = service.Engines(embed_query=slightly_slow_embed)
        bounded = await service.prepare(lifted, project_id=PROJECT, store=store, caps=MAIN, delivery="sync",
                                        request_id=request_id(), engines=engines)
        assert bounded.usage_meta()["retrieval"] == "lexical", "the setting's 0.2 s budget applied"
        committed = await service.prepare(lifted, project_id=PROJECT, store=store, caps=MAIN, delivery="sync",
                                          request_id=request_id(), engines=engines, sync_wait_s=service.NO_DEADLINE)
        assert committed.usage_meta()["retrieval"] == "vector"

    asyncio.run(scenario())


def test_a_sync_transcription_past_the_deadline_is_a_retryable_503_and_an_inline_extraction_a_400():
    import base64
    import time as real_time

    from tests.test_publicapi_file_inputs import _wav

    async def scenario():
        async def slow_whisper(raw, content_type):
            await asyncio.sleep(30)

        clip = base64.b64encode(_wav(3.0)).decode()
        body = {"model": "m", "messages": [{"role": "user", "content": [
            {"type": "text", "text": "q"}, {"type": "input_audio", "input_audio": {"data": clip, "format": "wav"}}]}]}
        started = real_time.monotonic()
        with pytest.raises(errors.ApiError) as late:
            await service.prepare(service.lift_file_parts(body, dialect="chat"), project_id=PROJECT,
                                  store=service.MemoryFileStore(), caps=MAIN, delivery="sync", request_id=request_id(),
                                  sync_budget_s=0.4, engines=service.Engines(transcriber=slow_whisper))
        assert real_time.monotonic() - started < 3.0
        assert late.value.status == 503 and late.value.code == "model_unavailable"
        assert late.value.headers() == {"Retry-After": "30"} and "safe to retry" in late.value.message

    asyncio.run(scenario())


def test_a_slow_inline_extraction_on_a_sync_request_is_a_400_toward_files_or_streaming(tmp_path, monkeypatch):
    import base64

    from app.config import settings

    async def scenario():
        monkeypatch.setattr(settings, "public_api_files_dir", str(tmp_path), raising=False)

        async def slow_extractor(source, kind, derived, max_ocr, strict):
            await asyncio.sleep(30)

        body = responses_body({"type": "input_file", "file_data": base64.b64encode(b"%PDF-1.7 big").decode(), "filename": "a.pdf"})
        rid = request_id()
        with pytest.raises(errors.ApiError) as slow:
            await service.prepare(service.lift_file_parts(body, dialect="responses"), project_id=PROJECT,
                                  store=service.MemoryFileStore(), caps=MAIN, delivery="sync", request_id=rid,
                                  sync_budget_s=0.3, engines=service.Engines(extractor=slow_extractor))
        assert slow.value.status == 400 and "too long to read in a synchronous request" in slow.value.message
        assert slow.value.param == "input.0.content.0.file_data"
        assert not (tmp_path / "_inline" / rid).exists()

    asyncio.run(scenario())


def test_a_sync_request_may_carry_one_clips_worth_of_input_audio_and_is_refused_before_any_transcription():
    import base64

    from tests.test_publicapi_file_inputs import _wav

    async def scenario():
        heard: List[int] = []

        async def whisper(raw, content_type):
            heard.append(len(raw))
            return "hello", None

        mp3 = base64.b64encode(b"ID3" + b"\x00" * 64).decode()
        two_mp3 = {"model": "m", "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": mp3, "format": "mp3"}},
            {"type": "input_audio", "input_audio": {"data": mp3, "format": "mp3"}}]}]}
        with pytest.raises(errors.ApiError) as refused:
            await service.prepare(service.lift_file_parts(two_mp3, dialect="chat"), project_id=PROJECT,
                                  store=service.MemoryFileStore(), caps=MAIN, delivery="sync", request_id=request_id(),
                                  engines=service.Engines(transcriber=whisper))
        assert refused.value.status == 400 and refused.value.param == "messages.0.content.1.input_audio"
        assert "stream" in refused.value.message and heard == [], "no clip reached the engine"
        streamed = await service.prepare(service.lift_file_parts(two_mp3, dialect="chat"), project_id=PROJECT,
                                         store=service.MemoryFileStore(), caps=MAIN, delivery="stream",
                                         request_id=request_id(), engines=service.Engines(transcriber=whisper))
        assert len(heard) == 2 and len(streamed.audio_blocks) == 2
        wavs = {"model": "m", "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(_wav(100.0)).decode(), "format": "wav"}},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(_wav(150.0)).decode(), "format": "wav"}}]}]}
        both = await service.prepare(service.lift_file_parts(wavs, dialect="chat"), project_id=PROJECT,
                                     store=service.MemoryFileStore(), caps=MAIN, delivery="sync", request_id=request_id(),
                                     engines=service.Engines(transcriber=whisper))
        assert len(both.audio_blocks) == 2, "250 s of WAV whose headers say so fits one clip's budget"

    asyncio.run(scenario())
