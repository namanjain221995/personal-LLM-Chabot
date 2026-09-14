"""An engine outage never fails a file; a patient gate never defers one
(gap 3 of the no-timeout + Files release, 2026-09-14).

The runner, the database and the extraction child are REAL. The embedding
engine is a real HTTP server on loopback speaking the OpenAI embeddings wire
(or no server at all, for "down"), reached through the production embedder —
`sidecars._embed_call` and the openai SDK — so the exceptions the runner
classifies are the ones production raises, not stand-ins. That is the gap the
verifier found: the old test raised `jobs.Deferred` itself, and the SDK's
`APIConnectionError` failed the file `internal_error` in production.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import socket
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest

from app import llm
from app.apifiles import events, jobs, outage, storage, vectors
from app.config import settings
from app.publicapi import capacity, sidecars
from tests import test_apifiles_jobs as J

# The jobs module's fixtures: one schema pass, the four Files tables emptied
# per test (the claim sweep takes ANY project's due blob), a per-test files root.
files_schema = J.files_schema
isolated_app_db = J.isolated_app_db
ambient_identity = J.ambient_identity
files_root = J.files_root

#: ~10,000 characters of plain text: several chunks, packed into more than one
#: engine call by the embed budget (up to 1,502 weight per chunk against 8,192).
TEXT = "".join(f"Paragraph {n}: the quarterly figures moved as the board expected. " * 3 + "\n" for n in range(50)).encode()


@pytest.fixture(autouse=True)
def fresh_gates(monkeypatch):
    capacity.reset_for_tests()
    events.reset_for_tests()
    monkeypatch.setattr(settings, "llm_max_retries", 0)  # the runner, not the SDK, decides retries
    monkeypatch.setattr(settings, "embed_model", "stub-embed")
    yield
    capacity.reset_for_tests()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class EmbedEngine:
    """An OpenAI-compatible `/v1/embeddings` on loopback.

    `batch` / `probe`: what a document batch and the one-input probe get —
    `ok`, `500`, `503`, `slow` (answers after `slow_s`), or `die` (the
    connection is dropped with the call outstanding and the server stops
    listening, like a crashed engine). `ok_batches`: document batches that
    succeed before `batch` applies."""

    def __init__(self, *, batch: str = "ok", probe: str = "ok", ok_batches: int = 0, slow_s: float = 2.0) -> None:
        self.batch, self.probe, self.ok_batches, self.slow_s = batch, probe, ok_batches, slow_s
        self.port = _free_port()
        self.calls: List[List[str]] = []
        self.server: Optional[asyncio.AbstractServer] = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def start(self) -> "EmbedEngine":
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", self.port)
        return self

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            self.server = None

    def _mode(self, inputs: List[str]) -> str:
        if inputs == [jobs.PROBE_TEXT]:
            return self.probe
        documents = sum(1 for call in self.calls if call != [jobs.PROBE_TEXT])
        return "ok" if documents <= self.ok_batches else self.batch

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    head = await reader.readuntil(b"\r\n\r\n")
                except (asyncio.IncompleteReadError, ConnectionError):
                    return
                length = int(re.search(rb"(?i)content-length:\s*(\d+)", head).group(1))
                inputs = list(json.loads(await reader.readexactly(length))["input"])
                self.calls.append(inputs)
                mode = self._mode(inputs)
                if mode == "die":
                    await self.stop()
                    writer.transport.abort()
                    return
                if mode == "slow":
                    await asyncio.sleep(self.slow_s)
                    mode = "ok"
                if mode == "ok":
                    body = json.dumps({
                        "object": "list", "model": "stub-embed",
                        "data": [{"object": "embedding", "index": i, "embedding": [1.0, float(len(t) % 7), 0.5]} for i, t in enumerate(inputs)],
                        "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
                    }).encode()
                    status = b"200 OK"
                else:
                    body = json.dumps({"error": {"message": "engine failure", "type": "server_error"}}).encode()
                    status = {
                        "500": b"500 Internal Server Error", "503": b"503 Service Unavailable",
                        "401": b"401 Unauthorized", "404": b"404 Not Found",
                    }[mode]
                writer.write(b"HTTP/1.1 " + status + b"\r\ncontent-type: application/json\r\ncontent-length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
        finally:
            with contextlib.suppress(Exception):
                writer.close()


def production_runner(**kwargs: Any) -> jobs.JobRunner:
    """The runner with the PRODUCTION embedder (`embed_documents=None`)."""
    kwargs.setdefault("embed_documents", None)
    return J.runner(**kwargs)


def _not_before_in(row: Dict[str, Any]) -> float:
    return (row["not_before"] - datetime.now(timezone.utc)).total_seconds()


async def _run_once_against(engine: Optional[EmbedEngine], monkeypatch, **runner_kwargs: Any) -> List[jobs.RunOutcome]:
    if engine is None:
        monkeypatch.setattr(settings, "embed_base_url", f"http://127.0.0.1:{_free_port()}/v1")  # nothing listens
    else:
        monkeypatch.setattr(settings, "embed_base_url", engine.base_url)
    return await production_runner(**runner_kwargs).run_once()


# =================================================================== the rule ==


def _request():
    return llm._openai_httpx_module().Request("POST", "http://127.0.0.1:9/v1/embeddings")


def _wrapped(sdk_exc: BaseException, cause: BaseException) -> BaseException:
    sdk_exc.__cause__ = cause
    return sdk_exc


def _cases():
    import openai

    httpx = llm._openai_httpx_module()
    req = _request()
    return {
        "refused": _wrapped(openai.APIConnectionError(request=req), httpx.ConnectError("[Errno 111] Connection refused")),
        "connect-timeout": _wrapped(openai.APITimeoutError(request=req), httpx.ConnectTimeout("timed out")),
        "read-timeout": _wrapped(openai.APITimeoutError(request=req), httpx.ReadTimeout("timed out")),
        "503": openai.InternalServerError("unavailable", response=httpx.Response(503, request=req), body=None),
        "429": openai.RateLimitError("busy", response=httpx.Response(429, request=req), body=None),
        "500": openai.InternalServerError("boom", response=httpx.Response(500, request=req), body=None),
        "broke-mid-response": _wrapped(
            openai.APIConnectionError(request=req), httpx.RemoteProtocolError("Server disconnected without sending a response.")
        ),
        "400": openai.BadRequestError("too long", response=httpx.Response(400, request=req), body=None),
        "401": openai.AuthenticationError("bad key", response=httpx.Response(401, request=req), body=None),
        "403": openai.PermissionDeniedError("forbidden", response=httpx.Response(403, request=req), body=None),
        "404": openai.NotFoundError(
            "The model `techsara-embed` does not exist.", response=httpx.Response(404, request=req), body=None
        ),
        "unparseable-200": openai.APIResponseValidationError(response=httpx.Response(200, request=req), body="<html>"),
    }


@pytest.mark.parametrize("case, kind", [
    ("refused", outage.KIND_CONNECT),
    ("connect-timeout", outage.KIND_TIMEOUT),
    ("read-timeout", outage.KIND_TIMEOUT),
    ("503", outage.KIND_CONNECT),
    ("429", outage.KIND_CONNECT),
    ("500", outage.KIND_STATUS),
    ("broke-mid-response", outage.KIND_BROKEN),
    ("400", outage.KIND_REFUSED),
    ("401", outage.KIND_REJECTED),
    ("403", outage.KIND_REJECTED),
    ("404", outage.KIND_REJECTED),
    ("unparseable-200", outage.KIND_UNEXPECTED),
])
def test_the_openai_sdk_errors_of_an_engine_are_classified_by_what_the_engine_did(case, kind):
    assert outage.classify(_cases()[case]) == kind


@pytest.mark.parametrize("exc", [KeyError("rows"), TypeError("NoneType"), ValueError("bad"), AttributeError("x")])
def test_a_bug_in_our_own_code_is_never_classified_as_an_engine_failure(exc):
    assert outage.classify(exc) == outage.KIND_NOT_ENGINE


def _raised_while_handling(inner: BaseException, outer: BaseException, *, from_none: bool = False) -> BaseException:
    try:
        try:
            raise inner
        except BaseException:
            if from_none:
                raise outer from None
            raise outer
    except BaseException as caught:  # noqa: BLE001 - the chained exception is the point
        return caught


def test_our_bug_raised_while_handling_an_engine_error_is_still_our_bug():
    """Review finding (2026-09-14): `_chain` walked `__context__`, so a
    `KeyError` raised inside `except httpx.ConnectError` classified as an
    engine outage and was retried for ever without failing."""
    httpx = llm._openai_httpx_module()
    refused = httpx.ConnectError("[Errno 111] Connection refused")
    assert outage.classify(_raised_while_handling(refused, KeyError("vectors"))) == outage.KIND_NOT_ENGINE
    assert outage.classify(_raised_while_handling(refused, TypeError("NoneType"))) == outage.KIND_NOT_ENGINE


def test_a_context_cut_with_from_none_is_not_walked():
    import openai

    httpx = llm._openai_httpx_module()
    refused = httpx.ConnectError("[Errno 111] Connection refused")
    outer = openai.APIError("wrapped", request=_request(), body=None)
    assert outage.classify(_raised_while_handling(refused, outer)) == outage.KIND_CONNECT, "an implicit context is evidence"
    assert outage.classify(_raised_while_handling(refused, outer, from_none=True)) == outage.KIND_UNEXPECTED


@pytest.mark.parametrize("exc", [TimeoutError("ours"), ConnectionRefusedError(111, "refused"), ConnectionError("pool")])
def test_a_bare_builtin_timeout_or_connection_error_is_not_classified_as_an_engine_outage(exc):
    """The SDK wraps every transport failure in its own error: a bare builtin
    came from our side of the call and must not be retried without a limit."""
    assert outage.classify(exc) == outage.KIND_NOT_ENGINE


def test_a_model_unavailable_wrapper_is_classified_by_the_engine_error_it_carries():
    from app.resilience import ModelUnavailable

    refused = _cases()["refused"]
    assert outage.classify(ModelUnavailable("http://embed/v1", 30.0, 3, refused)) == outage.KIND_CONNECT
    assert outage.classify(ModelUnavailable("http://embed/v1", 30.0, 3, _cases()["404"])) == outage.KIND_REJECTED


def test_a_probe_the_engine_answered_with_a_4xx_proves_it_is_serving():
    assert outage.probe_verdict(_cases()["404"]) == outage.PROBE_SERVING
    assert outage.probe_verdict(_cases()["400"]) == outage.PROBE_SERVING
    assert outage.probe_verdict(_cases()["refused"]) == outage.PROBE_DOWN
    assert outage.probe_verdict(_cases()["read-timeout"]) == outage.PROBE_UNKNOWN


def test_a_misconfigured_engine_or_an_unexplained_engine_error_spends_attempts():
    for kind in (outage.KIND_REJECTED, outage.KIND_UNEXPECTED):
        assert not outage.needs_probe(kind), kind
        assert outage.counts_attempt(kind, probe=None, progressed=False), kind
        assert not outage.counts_attempt(kind, probe=None, progressed=True), "a run that made progress is not a loop"


def test_an_attempt_is_spent_only_on_evidence_about_the_blob_never_for_an_engine_that_is_down_or_unknown():
    count = outage.counts_attempt
    for kind in (outage.KIND_CONNECT, outage.KIND_TIMEOUT):
        assert not count(kind, probe=None, progressed=False), kind
    assert not count(outage.KIND_STATUS, probe=outage.PROBE_DOWN, progressed=False)
    assert not count(outage.KIND_STATUS, probe=outage.PROBE_UNKNOWN, progressed=False)
    assert not count(outage.KIND_BROKEN, probe=outage.PROBE_UNKNOWN, progressed=False)
    assert count(outage.KIND_STATUS, probe=outage.PROBE_SERVING, progressed=False)
    assert count(outage.KIND_BROKEN, probe=outage.PROBE_SERVING, progressed=False)
    assert count(outage.KIND_BROKEN, probe=outage.PROBE_DOWN, progressed=False), "the engine died under this batch"
    assert count(outage.KIND_REFUSED, probe=None, progressed=False)
    for kind in (outage.KIND_STATUS, outage.KIND_BROKEN, outage.KIND_REFUSED):
        assert not count(kind, probe=outage.PROBE_SERVING, progressed=True), "a run that made progress is not a loop"


def test_the_outage_backoff_doubles_from_fifteen_seconds_and_stops_at_the_retry_delay():
    assert [outage.backoff_s(n, 300.0) for n in range(7)] == [15.0, 30.0, 60.0, 120.0, 240.0, 300.0, 300.0]
    assert outage.backoff_s(10_000, 300.0) == 300.0
    assert outage.backoff_s(3, 0.0) == 0.0


# =============================================================== the runner ==


def test_a_file_indexed_while_the_embedding_engine_is_down_is_never_failed_and_processes_once_it_is_back(tmp_path, monkeypatch, caplog):
    """The verifier's files-embed-down scenario: nothing listens on the embed
    port. Before 2026-09-14 the file read `status: error, internal_error`
    after one run."""
    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)

    first = asyncio.run(_run_once_against(None, monkeypatch))
    assert [(o.outcome, o.error_code) for o in first] == [("deferred", "outage")]
    after = J.blob(row["id"])
    assert after["status"] == "queued" and after["attempt"] == 0 and after["error_code"] is None
    assert 5.0 < _not_before_in(after) <= 15.0, "the first outage retry is due in 15 s, not 300"
    assert after["stages"]["chunk"]["status"] == "done", "finished stages are kept"
    assert jobs.processing_view(after)["status"] == "uploaded", "the File object never says error"

    # The outage outlasts the old five-attempt budget several times over.
    delays = []
    caplog.set_level("INFO", logger=jobs.log.name)
    for _ in range(8):
        J.set_blob(row["id"], "not_before = now() - interval '1 second'")
        outcomes = asyncio.run(_run_once_against(None, monkeypatch))
        assert [o.outcome for o in outcomes] == ["deferred"]
        current = J.blob(row["id"])
        delays.append(round(_not_before_in(current) / 15.0))
        assert current["status"] == "queued" and current["attempt"] == 0 and current["error_code"] is None
    assert delays == [2, 4, 8, 16, 20, 20, 20, 20], "backoff 30, 60, 120, 240, then the 300 s cap"
    deferrals = [r for r in caplog.records if "deferred at" in r.getMessage() and row["id"] in r.getMessage()]
    assert [r.levelname for r in deferrals] == ["INFO"] * 4 + ["WARNING"] * 4, "a long outage is visible"
    assert "6 in a row since " in deferrals[4].getMessage()
    since = J.blob(row["id"])["progress"]["outage_since"]
    assert all(f"since {since}" in r.getMessage() for r in deferrals), "the streak keeps the time it began"

    async def back_up() -> List[jobs.RunOutcome]:
        engine = await EmbedEngine().start()
        try:
            J.set_blob(row["id"], "not_before = now() - interval '1 second'")
            return await _run_once_against(engine, monkeypatch)
        finally:
            await engine.stop()

    done = asyncio.run(back_up())
    assert [o.outcome for o in done] == ["processed"]
    final = J.blob(row["id"])
    assert final["status"] == "processed" and final["facts"]["chunks_indexed"] == final["facts"]["chunks"] > 5
    assert "outage_retries" not in final["progress"], "the next outage backs off from 15 s again"
    assert "outage_since" not in final["progress"]


@pytest.mark.parametrize("batch", ["503", "slow"])
def test_a_503_or_an_engine_too_slow_to_answer_defers_the_file_without_spending_an_attempt(batch, tmp_path, monkeypatch):
    monkeypatch.setattr(sidecars, "EMBED_READ_TIMEOUT_S", 0.3)
    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)

    async def scenario() -> List[jobs.RunOutcome]:
        engine = await EmbedEngine(batch=batch, slow_s=3.0).start()
        try:
            return await _run_once_against(engine, monkeypatch)
        finally:
            await engine.stop()

    outcomes = asyncio.run(scenario())
    assert [(o.outcome, o.error_code) for o in outcomes] == [("deferred", "outage")]
    after = J.blob(row["id"])
    assert after["status"] == "queued" and after["attempt"] == 0 and after["error_code"] is None


def test_a_500_while_the_engine_answers_a_probe_spends_attempts_and_the_fifth_fails_processing_unavailable(tmp_path, monkeypatch):
    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)

    async def scenario(runs: int) -> List[List[jobs.RunOutcome]]:
        engine = await EmbedEngine(batch="500", probe="ok").start()
        results = []
        try:
            for _ in range(runs):
                J.set_blob(row["id"], "not_before = now() - interval '1 second'")
                results.append(await _run_once_against(engine, monkeypatch))
        finally:
            await engine.stop()
        return results

    first = asyncio.run(scenario(1))
    assert [(o.outcome, o.error_code) for o in first[0]] == [("deferred", None)]
    after = J.blob(row["id"])
    assert after["attempt"] == 1 and 250 < _not_before_in(after) <= 300, "a counted deferral keeps the 300 s schedule"
    rest = asyncio.run(scenario(4))
    assert [o.outcome for o in rest[-1]] == ["failed"]
    final = J.blob(row["id"])
    assert final["status"] == "failed" and final["error_code"] == "processing_unavailable"


@pytest.mark.parametrize("answer", ["404", "401"])
def test_an_engine_that_refuses_us_spends_attempts_and_the_fifth_fails_processing_unavailable(answer, tmp_path, monkeypatch):
    """Review finding (2026-09-14): a wrong EMBED model name (404) or a bad key
    (401) classified as `broken` with an `unknown` probe — 12 runs, 24 engine
    calls, the file queued with attempt 0 and nobody told."""
    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)

    async def scenario(runs: int) -> Tuple[List[List[jobs.RunOutcome]], List[List[str]]]:
        engine = await EmbedEngine(batch=answer, probe=answer).start()
        results = []
        try:
            for _ in range(runs):
                J.set_blob(row["id"], "not_before = now() - interval '1 second'")
                results.append(await _run_once_against(engine, monkeypatch))
        finally:
            await engine.stop()
        return results, list(engine.calls)

    results, calls = asyncio.run(scenario(5))
    assert [[(o.outcome, o.error_code) for o in run] for run in results] == [[("deferred", None)]] * 4 + [
        [("failed", "processing_unavailable")]
    ]
    assert all(call != [jobs.PROBE_TEXT] for call in calls), "a refusal is not ambiguous: no probe is spent on it"
    final = J.blob(row["id"])
    assert final["status"] == "failed" and final["error_code"] == "processing_unavailable"


def test_a_500_while_the_probe_cannot_reach_the_engine_is_an_outage_and_spends_nothing(tmp_path, monkeypatch):
    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)

    async def scenario() -> List[jobs.RunOutcome]:
        engine = await EmbedEngine(batch="500", probe="die").start()
        try:
            return await _run_once_against(engine, monkeypatch)
        finally:
            await engine.stop()

    outcomes = asyncio.run(scenario())
    assert [(o.outcome, o.error_code) for o in outcomes] == [("deferred", "outage")]
    assert J.blob(row["id"])["attempt"] == 0


def test_an_engine_that_dies_under_a_batch_before_any_vector_is_written_spends_an_attempt(tmp_path, monkeypatch):
    """The poison-input bound: the connection breaks with THIS blob's call
    outstanding and the engine is gone right after. Uncounted, a file that
    crashes the shared embed engine would crash it again every few minutes."""
    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)

    async def scenario() -> List[jobs.RunOutcome]:
        engine = await EmbedEngine(batch="die").start()
        try:
            return await _run_once_against(engine, monkeypatch)
        finally:
            await engine.stop()

    outcomes = asyncio.run(scenario())
    assert [(o.outcome, o.error_code) for o in outcomes] == [("deferred", None)]
    assert J.blob(row["id"])["attempt"] == 1


def test_a_run_that_wrote_vectors_before_the_engine_died_spends_nothing_and_the_next_run_resumes_at_the_next_row(tmp_path, monkeypatch):
    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)
    derived = storage.derived_dir(tenant["project_id"], row["sha256"])

    async def crash_after_one_batch() -> tuple:
        engine = await EmbedEngine(batch="die", ok_batches=1).start()
        try:
            return await _run_once_against(engine, monkeypatch), engine.calls
        finally:
            await engine.stop()

    outcomes, crashed_calls = asyncio.run(crash_after_one_batch())
    assert [(o.outcome, o.error_code) for o in outcomes] == [("deferred", "outage")]
    assert J.blob(row["id"])["attempt"] == 0
    written = len(crashed_calls[0])
    assert os.path.getsize(os.path.join(derived, vectors.VECTORS_NAME)) == written * 3 * 4, "the first batch's rows are kept"

    async def resume() -> tuple:
        engine = await EmbedEngine().start()
        try:
            J.set_blob(row["id"], "not_before = now() - interval '1 second'")
            return await _run_once_against(engine, monkeypatch), engine.calls
        finally:
            await engine.stop()

    done, calls = asyncio.run(resume())
    assert [o.outcome for o in done] == ["processed"]
    total = J.blob(row["id"])["facts"]["chunks_indexed"]
    assert sum(len(c) for c in calls) == total - written > 0, "only the rows without a vector were embedded again"


def test_a_job_abandoned_while_its_probe_waits_at_the_gate_stops_instead_of_failing_internal_error(tmp_path):
    import openai

    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)
    httpx = llm._openai_httpx_module()

    async def embed(texts):
        if list(texts) == [jobs.PROBE_TEXT]:
            raise capacity.Abandoned()  # the lease went while the probe queued
        request = httpx.Request("POST", "http://embed/v1/embeddings")
        raise openai.InternalServerError("boom", response=httpx.Response(500, request=request), body=None)

    outcomes = asyncio.run(J.runner(embed_documents=embed).run_once())
    assert [o.outcome for o in outcomes] == ["stopped"]
    after = J.blob(row["id"])
    assert after["status"] != "failed" and after["error_code"] is None


def test_a_bug_in_the_index_stage_still_fails_internal_error_instead_of_being_retried_for_ever(tmp_path):
    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)

    async def buggy(texts):
        raise KeyError("embedding")

    outcomes = asyncio.run(J.runner(embed_documents=buggy).run_once())
    assert [(o.outcome, o.error_code) for o in outcomes] == [("failed", "internal_error")]
    assert J.blob(row["id"])["status"] == "failed"


# ============================================================= patient gates ==


@contextlib.asynccontextmanager
async def _gate_full(gate: str):
    """Every unit of a public gate held by the test (weight 0: the count, not
    the KV budget, is what is full)."""
    config = capacity._config(gate)
    async with contextlib.AsyncExitStack() as stack:
        for _ in range(config.max_concurrent):
            await stack.enter_async_context(capacity.hold(gate, wait_s=None))
        yield


def test_a_processing_job_waits_at_a_full_embed_gate_past_the_background_wait_setting_and_then_indexes(tmp_path, monkeypatch):
    """Before 2026-09-14 each embed call gave up after
    PUBLIC_API_BACKGROUND_GATE_WAIT_S and deferred the whole blob 300 s for a
    gate that was only busy."""
    monkeypatch.setenv("PUBLIC_API_BACKGROUND_GATE_WAIT_S", "0.2")
    assert capacity.background_wait_s() == 0.2
    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)

    async def scenario() -> tuple:
        engine = await EmbedEngine().start()
        monkeypatch.setattr(settings, "embed_base_url", engine.base_url)
        r = production_runner(renew_s=0.2)
        try:
            async with _gate_full(capacity.GATE_EMBED):
                tasks = await r._claim_and_spawn()
                await asyncio.sleep(1.5)  # 7x the old per-call limit
                waiting = (tasks[0].done(), J.blob(row["id"])["status"], list(engine.calls))
            outcome = await asyncio.wait_for(tasks[0], timeout=30)
        finally:
            await engine.stop()
        return waiting, outcome

    waiting, outcome = asyncio.run(scenario())
    assert waiting == (False, "processing", []), "still waiting at the gate: not deferred, nothing sent"
    assert outcome.outcome == "processed"
    assert J.blob(row["id"])["progress"]["waited_for_capacity_s"] >= 1.0


def test_a_job_waiting_at_a_full_embed_gate_stops_when_another_process_marks_its_blob_deleting(tmp_path, monkeypatch):
    tenant = J.new_project()
    row = J.insert_blob(tenant, TEXT)
    monkeypatch.setattr(settings, "embed_base_url", f"http://127.0.0.1:{_free_port()}/v1")

    async def scenario() -> jobs.RunOutcome:
        r = production_runner()
        async with _gate_full(capacity.GATE_EMBED):
            tasks = await r._claim_and_spawn()
            for _ in range(200):  # until the job reaches the gate
                if J.blob(row["id"])["stage"] == "index":
                    break
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)
            J.set_blob(row["id"], "status = 'deleting'")  # a DELETE served by another process
            await r._renew_once()  # the renewal that finds the lease gone
            return await asyncio.wait_for(tasks[0], timeout=10)

    outcome = asyncio.run(scenario())
    assert (outcome.outcome, outcome.error_code) == ("stopped", "deleting")
    assert J.blob(row["id"])["status"] == "deleting", "nothing was written over the delete"


def test_the_ocr_gate_of_a_processing_job_has_no_clock_and_ends_only_when_the_job_is_abandoned(monkeypatch):
    from app.apifiles import ocr_pages

    monkeypatch.setenv("PUBLIC_API_BACKGROUND_GATE_WAIT_S", "0.1")

    async def scenario() -> tuple:
        abandon = asyncio.Event()

        async def enter() -> None:
            async with ocr_pages.default_gate(abandon=abandon):
                pass

        async with _gate_full(capacity.GATE_OCR):
            task = asyncio.ensure_future(enter())
            await asyncio.sleep(1.0)
            still_waiting = not task.done()
            abandon.set()
            try:
                await asyncio.wait_for(task, timeout=5)
            except capacity.Abandoned:
                return still_waiting, "abandoned"
            return still_waiting, "entered"

    assert asyncio.run(scenario()) == (True, "abandoned")
