"""Background responses — the row, the detached task, and the cancel flag.

CONTRACT-3 §14 in four sentences: the row is durable BEFORE any expensive
work, the caller gets 202 with its id, the work survives the client hanging
up, and cancellation is idempotent. Each of those is a promise about an ORDER
of operations, or about a reference that is deliberately not held — exactly
the kind of thing that passes review and then does not hold — so every one is
asserted here against real PostgreSQL and a real event loop.

WHY THE GENERATION IS A STUB, AND WHAT THAT DOES NOT LET US SKIP.
`streaming.Generation` needs an engine, and the suite runs with no vLLM, no
GPU and no network (`tests/conftest.py`). `background.set_generation_factory`
is the seam for that, and the stub deliberately has the SHAPE of
`streaming.Generation` — `stream()`, `aclose()`, `usage`, `error`,
`first_token_at` — because what this module is responsible for is the
ORCHESTRATION around one: what is written, when, in what order, and what
happens when the thing it is orchestrating fails halfway through. That the
production factory IS `streaming.Generation`, and therefore the same engine
path the synchronous route takes (CONTRACT-3 §11), is pinned by
`test_the_default_generation_is_the_one_the_synchronous_route_uses`.

The one property a stub cannot fake is ordering, so several tests below make
the stub BLOCK and then assert what the database says while the job is still
in the middle of its work.
"""
from __future__ import annotations

import asyncio
import secrets
import threading
import time
from dataclasses import dataclass

import pytest

from app import db
from app.apiplatform import quotas
from app.apiplatform.resolver import ApiCaller, CallerLimits
from app.apiplatform.webhooks import sender
from app.config import settings
from app.publicapi import background, errors, streaming
from app.publicapi.registry import declared_models

WORKSPACE = "ws-background"
MODEL = declared_models()[0]
MESSAGES = [{"role": "user", "content": "Explain retrieval-augmented generation."}]


@dataclass
class _Chunk:
    """The shape `streaming.Generation.stream()` yields."""

    kind: str
    text: str = ""


@pytest.fixture()
def limits_enforced(monkeypatch):
    """PUBLIC_API_ENFORCE_LIMITS=true for one test.

    The owner decision of 2026-09-13 made the public API unlimited by default,
    so every test below that pins a concurrency REFUSAL asks for enforcement
    explicitly: the enforcement code stays available to an operator, so it
    stays tested. The unlimited default has its own tests, which do not use
    this fixture.
    """
    monkeypatch.setattr(settings, "public_api_enforce_limits", True)


@pytest.fixture(autouse=True)
def _clean_module_state(monkeypatch):
    """Background jobs live in module globals because they outlive a request.

    Without this fixture a job a previous test left running would count
    against the next test's concurrency limit and `active_ids()` would carry
    a dead id — the same reason `tests/test_api_platform_keys.py` isolates the
    pepper cache.
    """
    monkeypatch.setattr(background, "_jobs", {})
    monkeypatch.setattr(background, "_tasks", set())
    # The concurrency counter is `quotas`' now — one per project, shared with
    # sync and stream (2026-09-13) — so that is the process state to clear.
    quotas.reset_concurrency()
    background.set_generation_factory(None)
    yield
    background.set_generation_factory(None)
    quotas.reset_concurrency()


@pytest.fixture()
def project():
    with db.connection() as con:
        con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", (WORKSPACE, "Background"))
    return db.create_api_project(WORKSPACE, "Background project", "live")


def _spec(**overrides) -> streaming.GenerationSpec:
    fields = {
        "response_id": "resp_" + secrets.token_hex(12),
        "model": MODEL.id,
        "messages": list(MESSAGES),
        "max_tokens": 256,
        "temperature": 0.2,
        "created_at": int(time.time()),
    }
    fields.update(overrides)
    return streaming.GenerationSpec(**fields)


def _generation(
    chunks=(("token", "hello"),),
    *,
    gate: asyncio.Event = None,
    entered: asyncio.Event = None,
    closed: list = None,
    specs: list = None,
    usage=None,
    error: BaseException = None,
    raises: BaseException = None,
):
    """A stub with `streaming.Generation`'s shape.

    `entered` is set the first time `stream()` runs, which is AFTER the job has
    written `status='in_progress'`. Several tests need that ordering to be a
    fact rather than a hope: `await asyncio.sleep(0)` yields the loop once,
    while the status write is a database round trip in a worker thread and
    takes several.
    """

    class _Fake:
        def __init__(self, spec):
            self.spec = spec
            self.usage = usage
            self.error = error
            self.first_token_at = None
            if specs is not None:
                specs.append(spec)

        async def stream(self):
            if entered is not None:
                entered.set()
            if raises is not None:
                raise raises
            for kind, text in chunks:
                if gate is not None:
                    await gate.wait()
                if self.first_token_at is None and kind == streaming.TOKEN_KIND:
                    self.first_token_at = time.monotonic()
                yield _Chunk(kind=kind, text=text)

        async def aclose(self):
            if closed is not None:
                closed.append(self.spec.response_id)

    return _Fake


def _caller(project_row, *, max_concurrency: int = 4) -> ApiCaller:
    """The identity `background.start` takes its slot for. Hand-built, because
    what these tests exercise is the job's orchestration, not the resolver
    (which tests/test_api_platform_resolver.py covers); `key_id` is blank so the
    row carries no key, which the schema allows."""
    limits = CallerLimits(
        rpm=1000,
        input_tpm=10_000_000,
        output_tpm=10_000_000,
        max_concurrency=max_concurrency,
        daily_token_quota=10_000_000,
    )
    return ApiCaller(
        workspace_id=str(project_row.get("workspace_id") or WORKSPACE),
        project_id=str(project_row["id"]),
        service_account_id=None,
        key_id="",
        scopes=frozenset(),
        models=(),
        limits=limits,
        environment="live",
    )


async def _start(project_row, spec=None, *, max_concurrency: int = 4, **kwargs):
    return await background.start(
        spec if spec is not None else _spec(),
        caller=_caller(project_row, max_concurrency=max_concurrency),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# It is the synchronous route's engine path
# ---------------------------------------------------------------------------


def test_the_default_generation_is_the_one_the_synchronous_route_uses():
    """CONTRACT-3 §11: `/v1` has ONE path to the engine. A background job that
    built its own would drift from the synchronous one the first time either
    changed — and it is `streaming.Generation` that owns the admission lane,
    the `aclose()` rule and the usage ContextVar boundary."""
    background.set_generation_factory(None)

    assert background._generation_factory() is streaming.Generation


def test_the_generation_is_built_from_the_spec_the_caller_resolved(project):
    specs: list = []
    background.set_generation_factory(_generation(specs=specs))
    spec = _spec(max_tokens=128, temperature=0.9)

    async def scenario():
        await _start(project, spec)
        await background.drain(timeout=10)

    asyncio.run(scenario())

    assert specs == [spec]


# ---------------------------------------------------------------------------
# The row comes first
# ---------------------------------------------------------------------------


def test_the_response_row_is_durable_before_any_generation_starts(project):
    """The id in the 202 must name something that already exists. If this
    process died in the next millisecond, `GET /v1/responses/{id}` still has
    an answer."""
    specs: list = []
    background.set_generation_factory(_generation(specs=specs))

    async def scenario():
        row = await _start(project)
        # Nothing has been generated yet: the task does not run until this
        # coroutine next awaits.
        assert specs == []
        stored = await db.run_in_thread(db.get_api_response, row["id"], project["id"])
        await background.drain(timeout=10)
        return row, stored

    row, stored = asyncio.run(scenario())

    assert row["id"].startswith("resp_")
    assert row["status"] == "queued"
    assert row["background"] is True
    assert stored["id"] == row["id"]
    assert specs != []  # and it did run, afterwards


def test_a_row_the_router_already_claimed_is_used_rather_than_written_twice(project):
    """The router claims a row for EVERY mode before it dispatches, because
    `GET /v1/responses/{id}` is documented for all of them."""
    spec = _spec()
    claimed = db.create_api_response(
        project["id"], WORKSPACE, MODEL.id, "req_claimed",
        response_id=spec.response_id, background=True, metadata={"a": "b"},
    )
    background.set_generation_factory(_generation())

    async def scenario():
        row = await _start(project, spec)
        await background.drain(timeout=10)
        return row

    row = asyncio.run(scenario())

    assert row["id"] == claimed["id"]
    assert row["metadata"] == {"a": "b"}
    assert len(db.list_api_responses(project["id"])) == 1


def test_a_row_written_here_carries_the_metadata_and_the_projects_retention(project):
    background.set_generation_factory(_generation())

    async def scenario():
        row = await _start(
            project, metadata={"customer_request_id": "abc-123"},
            instructions_present=True, fingerprint="fp-1", request_id="req_9",
        )
        await background.drain(timeout=10)
        return row

    row = asyncio.run(scenario())

    assert row["metadata"] == {"customer_request_id": "abc-123"}
    assert row["instructions_present"] is True
    assert row["fingerprint"] == "fp-1"
    assert row["background"] is True
    # Retention comes from the project, not from the caller (SCHEMA-V34).
    assert row["expires_at"] is not None


def test_a_completed_job_records_its_text_its_usage_and_its_timings(project):
    background.set_generation_factory(
        _generation(
            [("reasoning", "thinking..."), ("token", "Hello "), ("token", "world.")],
            usage={"prompt_tokens": 37, "completion_tokens": 112},
        )
    )

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        return await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    final = asyncio.run(scenario())

    assert final["status"] == "completed"
    # The model's thinking is not the answer and never becomes `output_text`.
    assert final["output_text"] == "Hello world."
    assert final["input_tokens"] == 37 and final["output_tokens"] == 112
    assert final["ttft_ms"] is not None and final["duration_ms"] is not None
    assert final["started_at"] is not None and final["completed_at"] is not None


def test_token_counts_stay_null_when_the_engine_measured_nothing(project):
    """NULL means NOT MEASURED (SCHEMA-V34). A zero would be a lie and an
    under-charge."""
    background.set_generation_factory(_generation(usage=None))

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        return await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    final = asyncio.run(scenario())

    assert final["status"] == "completed"
    assert final["input_tokens"] is None and final["output_tokens"] is None


# ---------------------------------------------------------------------------
# It survives the client
# ---------------------------------------------------------------------------


def test_a_background_job_finishes_after_the_caller_has_gone_away(project):
    """Detached is the whole feature. Nothing in this test holds a reference
    to the task — `_tasks` does, which is what stops the garbage collector
    from taking a task mid-await (the 2026-09-10 upload finaliser bug)."""
    gate, entered = asyncio.Event(), asyncio.Event()
    background.set_generation_factory(
        _generation([("token", "late answer")], gate=gate, entered=entered)
    )

    async def scenario():
        row = await _start(project)
        # The "client" goes away here: the coroutine that started the job
        # returns, and only the module-level set still refers to the task.
        response_id = row["id"]
        del row
        await entered.wait()
        mid = await db.run_in_thread(db.get_api_response, response_id, project["id"])
        gate.set()
        await background.drain(timeout=10)
        return mid, await db.run_in_thread(db.get_api_response, response_id, project["id"])

    mid, final = asyncio.run(scenario())

    assert mid["status"] == "in_progress"
    assert mid["started_at"] is not None
    assert final["status"] == "completed"
    assert final["output_text"] == "late answer"


def test_the_task_is_held_strongly_while_it_runs_and_released_afterwards(project):
    gate, entered = asyncio.Event(), asyncio.Event()
    background.set_generation_factory(_generation(gate=gate, entered=entered))

    async def scenario():
        row = await _start(project)
        await entered.wait()
        held = len(background._tasks), background.active_ids()
        gate.set()
        await background.drain(timeout=10)
        await asyncio.sleep(0)
        return row, held

    row, (held_count, held_ids) = asyncio.run(scenario())

    assert held_count == 1 and held_ids == (row["id"],)
    assert background.active_ids() == ()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def test_a_running_job_stops_at_the_next_chunk_and_keeps_what_it_produced(project):
    """Nothing the model produced AFTER the cancel was requested is recorded,
    and everything it produced before it is."""
    first, second = asyncio.Event(), asyncio.Event()

    class _Fake:
        def __init__(self, spec):
            self.spec = spec
            self.usage = {"prompt_tokens": 5, "completion_tokens": 9}
            self.error = None
            self.first_token_at = time.monotonic()

        async def stream(self):
            yield _Chunk("token", "the part that was produced")
            first.set()
            # Parked until the test has cancelled, so the chunk below is
            # unambiguously one that arrived after the cancel.
            await second.wait()
            yield _Chunk("token", " and the part that never should be")

        async def aclose(self):
            return None

    background.set_generation_factory(_Fake)

    async def scenario():
        row = await _start(project)
        await first.wait()
        cancelled = await background.request_cancel(row["id"], project["id"])
        second.set()
        await background.drain(timeout=10)
        return cancelled, await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    cancelled, final = asyncio.run(scenario())

    assert cancelled["cancel_requested"] is True
    assert final["status"] == "cancelled"
    assert final["output_text"] == "the part that was produced"
    # A cancelled job still cost tokens, and they are still recorded.
    assert final["input_tokens"] == 5 and final["output_tokens"] == 9
    assert final["completed_at"] is not None


def test_cancelling_twice_is_the_same_answer_as_cancelling_once(project):
    background.set_generation_factory(_generation())

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        first = await background.request_cancel(row["id"], project["id"])
        second = await background.request_cancel(row["id"], project["id"])
        return first, second

    first, second = asyncio.run(scenario())

    assert first["status"] == second["status"] == "completed"
    assert first["completed_at"] == second["completed_at"]


def test_cancelling_a_finished_response_never_walks_it_back_to_cancelled(project):
    background.set_generation_factory(_generation([("token", "done")]))

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        after = await background.request_cancel(row["id"], project["id"])
        return after, await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    after, stored = asyncio.run(scenario())

    assert after["status"] == "completed"
    assert stored["status"] == "completed"
    assert stored["output_text"] == "done"
    # A terminal response is not even marked as having been asked to cancel.
    assert stored["cancel_requested"] is False


def test_a_cancel_that_arrives_before_the_model_is_asked_is_still_honoured(monkeypatch, project):
    """The cheapest thing to cancel must not be the one thing a cancel cannot
    stop: the flag is checked before the generation is built at all.

    The job is held at its very first write — the `in_progress` update — so
    the cancel provably lands before the model would have been asked. Without
    the hold this is a race the test would win most of the time and report as
    flaky the rest.
    """
    specs: list = []
    background.set_generation_factory(_generation(specs=specs))
    real_update = db.update_api_response
    reached, release = threading.Event(), threading.Event()

    def held_update(response_id, project_id, /, **fields):
        if fields.get("status") == "in_progress":
            reached.set()
            release.wait(10)
        return real_update(response_id, project_id, **fields)

    monkeypatch.setattr(db, "update_api_response", held_update)

    async def scenario():
        row = await _start(project)
        await asyncio.to_thread(reached.wait, 10)
        cancelled = await background.request_cancel(row["id"], project["id"])
        release.set()
        await background.drain(timeout=10)
        return cancelled, await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    cancelled, final = asyncio.run(scenario())

    assert cancelled["cancel_requested"] is True
    assert final["status"] == "cancelled"
    assert specs == []


def test_a_cancel_from_another_process_is_seen_by_the_poll(monkeypatch, project):
    """The in-memory event does not survive a restart, so the column is what
    makes a cancel work across processes. The poll interval is shortened here
    rather than waited out."""
    monkeypatch.setattr(background, "CANCEL_POLL_SECONDS", 0.0)
    seen = asyncio.Event()

    class _Fake:
        def __init__(self, spec):
            self.spec, self.usage, self.error = spec, None, None
            self.first_token_at = None

        async def stream(self):
            yield _Chunk("token", "first")
            seen.set()
            for _ in range(200):
                await asyncio.sleep(0.02)
                yield _Chunk("token", ".")

        async def aclose(self):
            return None

    background.set_generation_factory(_Fake)

    async def scenario():
        row = await _start(project)
        await seen.wait()
        # Written straight to the column, exactly as another orchestrator
        # would have written it — the in-memory event is never set.
        await db.run_in_thread(
            db.update_api_response, row["id"], project["id"], cancel_requested=True
        )
        await background.drain(timeout=10)
        return await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    final = asyncio.run(scenario())

    assert final["status"] == "cancelled"


def test_cancelling_a_row_no_process_is_running_closes_it_there_and_then(project):
    """A restart orphan has no task to see the flag. Setting only the flag
    would leave the caller polling `queued` until the sweep got to it, which
    is not what "cancel" means."""
    orphan = db.create_api_response(
        project["id"], WORKSPACE, MODEL.id, "req_orphan", background=True
    )
    db.update_api_response(orphan["id"], project["id"], status="in_progress")

    cancelled = asyncio.run(background.request_cancel(orphan["id"], project["id"]))

    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is True
    assert cancelled["completed_at"] is not None
    # And it stays cancelled when asked again.
    again = asyncio.run(background.request_cancel(orphan["id"], project["id"]))
    assert again["status"] == "cancelled"
    assert again["completed_at"] == cancelled["completed_at"]


def test_a_running_job_is_not_closed_from_outside_so_it_keeps_its_usage(project):
    """The complement of the test above: while a job IS running here, only the
    flag is written, because the job's own terminal write is the one that
    carries the text and the token counts."""
    gate, entered = asyncio.Event(), asyncio.Event()
    background.set_generation_factory(_generation(gate=gate, entered=entered))

    async def scenario():
        row = await _start(project)
        await entered.wait()
        cancelled = await background.request_cancel(row["id"], project["id"])
        gate.set()
        await background.drain(timeout=10)
        return cancelled

    cancelled = asyncio.run(scenario())

    assert cancelled["cancel_requested"] is True
    assert cancelled["status"] == "in_progress"


def test_a_cancel_reaches_a_job_whose_engine_has_gone_silent(project):
    """No token for minutes is the case where a between-chunks check would be
    useless. `Generation` keeps sending a heartbeat chunk (capped at 15 s by
    `events.HEARTBEAT_SECONDS`), and this loop consumes it precisely so the
    cancel is seen anyway. The stub emits heartbeats and nothing else."""
    silent = asyncio.Event()

    class _Fake:
        def __init__(self, spec):
            self.spec, self.usage, self.error = spec, None, None
            self.first_token_at = None

        async def stream(self):
            yield _Chunk("token", "the only token there will ever be")
            silent.set()
            for _ in range(500):
                await asyncio.sleep(0.01)
                yield _Chunk("heartbeat")

        async def aclose(self):
            return None

    background.set_generation_factory(_Fake)

    async def scenario():
        row = await _start(project)
        await silent.wait()
        await background.request_cancel(row["id"], project["id"])
        await background.drain(timeout=10)
        return await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    final = asyncio.run(scenario())

    assert final["status"] == "cancelled"
    assert final["output_text"] == "the only token there will ever be"


def test_cancelling_another_projects_response_reads_as_missing(project):
    other = db.create_api_project(WORKSPACE, "Someone else", "live")
    background.set_generation_factory(_generation())

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        return await background.request_cancel(row["id"], other["id"])

    assert asyncio.run(scenario()) is None


def test_cancelling_a_response_that_never_existed_reads_as_missing(project):
    assert asyncio.run(background.request_cancel("resp_does_not_exist", project["id"])) is None


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("limits_enforced")
def test_a_project_cannot_have_more_background_jobs_in_flight_than_its_limit(project):
    """CONTRACT-3 §12: the process-local gate. Without it one key's batch job
    would take every admission lane the chat app shares."""
    gate, entered = asyncio.Event(), asyncio.Event()
    background.set_generation_factory(_generation(gate=gate, entered=entered))

    async def scenario():
        await _start(project, max_concurrency=2)
        await _start(project, max_concurrency=2)
        await entered.wait()
        with pytest.raises(errors.ApiError) as raised:
            await _start(project, max_concurrency=2)
        gate.set()
        await background.drain(timeout=10)
        return raised.value

    error = asyncio.run(scenario())

    assert error.code == "concurrency_limit_exceeded"
    assert error.status == 429
    # CONTRACT-3 §9: every 429 carries Retry-After.
    assert int(error.headers()["Retry-After"]) >= 1


@pytest.mark.usefixtures("limits_enforced")
def test_a_refused_job_writes_no_row_at_all(project):
    gate, entered = asyncio.Event(), asyncio.Event()
    background.set_generation_factory(_generation(gate=gate, entered=entered))

    async def scenario():
        await _start(project, max_concurrency=1)
        await entered.wait()
        with pytest.raises(errors.ApiError):
            await _start(project, max_concurrency=1)
        rows = await db.run_in_thread(db.list_api_responses, project["id"])
        gate.set()
        await background.drain(timeout=10)
        return rows

    assert len(asyncio.run(scenario())) == 1


@pytest.mark.usefixtures("limits_enforced")
def test_a_row_the_router_claimed_is_closed_when_the_ceiling_refuses_it(project):
    """A row left `queued` with nobody running it is precisely what the
    restart sweep exists to clean up. Manufacturing one on a path that knows
    better would be sloppy."""
    gate, entered = asyncio.Event(), asyncio.Event()
    background.set_generation_factory(_generation(gate=gate, entered=entered))
    spec = _spec()
    db.create_api_response(
        project["id"], WORKSPACE, MODEL.id, "req_claimed",
        response_id=spec.response_id, background=True,
    )

    async def scenario():
        await _start(project, max_concurrency=1)
        await entered.wait()
        with pytest.raises(errors.ApiError):
            await _start(project, spec, max_concurrency=1)
        refused = await db.run_in_thread(db.get_api_response, spec.response_id, project["id"])
        gate.set()
        await background.drain(timeout=10)
        return refused

    refused = asyncio.run(scenario())

    assert refused["status"] == "failed"
    assert refused["error_code"] == "concurrency_limit_exceeded"


def test_a_finished_job_gives_its_slot_back(project):
    background.set_generation_factory(_generation())

    async def scenario():
        for _ in range(4):
            await _start(project, max_concurrency=1)
            await background.drain(timeout=10)
        return quotas.in_flight(_caller(project)), background.in_flight()

    assert asyncio.run(scenario()) == (0, 0)


def test_one_projects_jobs_do_not_count_against_another_projects_limit(project):
    other = db.create_api_project(WORKSPACE, "Neighbour", "live")
    gate, entered = asyncio.Event(), asyncio.Event()
    background.set_generation_factory(_generation(gate=gate, entered=entered))

    async def scenario():
        await _start(project, max_concurrency=1)
        await _start(other, max_concurrency=1)
        counts = quotas.in_flight(_caller(project)), quotas.in_flight(_caller(other))
        gate.set()
        await background.drain(timeout=10)
        return counts

    assert asyncio.run(scenario()) == (1, 1)


def test_a_slot_is_not_leaked_when_the_row_cannot_be_written(project):
    background.set_generation_factory(_generation())

    async def scenario():
        with pytest.raises(ValueError):
            await _start({"id": "proj_does_not_exist", "workspace_id": WORKSPACE})
        ghost = _caller({"id": "proj_does_not_exist", "workspace_id": WORKSPACE})
        return quotas.in_flight(ghost), background.in_flight()

    assert asyncio.run(scenario()) == (0, 0)


# ---------------------------------------------------------------------------
# Failure, cleanup and the restart sweep
# ---------------------------------------------------------------------------


def test_a_failed_job_records_a_public_error_and_never_the_traceback(project):
    """CONTRACT-3 §9: no response body may carry a traceback, SQL, a container
    name, an internal hostname or a private IP. The row that
    `GET /v1/responses/{id}` renders is a response body."""
    background.set_generation_factory(
        _generation(
            raises=RuntimeError(
                'connection to server at "postgres" (172.18.0.4), port 5432 failed'
            )
        )
    )

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        return await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    final = asyncio.run(scenario())

    assert final["status"] == "failed"
    assert final["error_code"] == "internal_error"
    assert "172.18.0.4" not in final["error_message"]
    assert "postgres" not in final["error_message"]
    assert "Traceback" not in final["error_message"]
    assert final["completed_at"] is not None


def test_an_engine_failure_keeps_the_retry_safe_code_the_streaming_path_gives_it(project):
    """`Generation` carries the engine's exception rather than raising it, and
    `streaming.engine_error` is what turns it into the CONTRACT §9 code a
    caller can act on. A flat `internal_error` would tell them to give up on a
    request that a retry would have served."""
    class QueuedForRecovery(RuntimeError):
        pass

    background.set_generation_factory(_generation(error=QueuedForRecovery("reloading")))

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        return await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    final = asyncio.run(scenario())

    assert final["status"] == "failed"
    assert final["error_code"] in ("model_recovering", "model_unavailable")


@pytest.mark.parametrize(
    "engine_state, expected",
    [(None, "model_recovering"), ("RECOVERING", "model_recovering"), ("DOWN", "model_unavailable")],
)
def test_the_engine_state_decides_which_retry_code_a_failed_job_records(
    monkeypatch, project, engine_state, expected
):
    """The test above accepts either code, so hard-coding `model_unavailable`
    in place of `streaming.engine_error` left it green (verifier mutation,
    2026-09-13). Pinning the controller's verdict makes the mapping itself the
    thing under test."""

    class QueuedForRecovery(RuntimeError):
        pass

    monkeypatch.setattr(streaming, "_engine_state_name", lambda: engine_state)
    background.set_generation_factory(_generation(error=QueuedForRecovery("reloading")))

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        return await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    assert asyncio.run(scenario())["error_code"] == expected


def test_a_full_admission_lane_is_recorded_as_the_model_at_capacity_not_a_limit_or_a_failure_of_ours(project):
    """The shared engine's lanes being full is capacity, not a per-caller
    limit (owner decision 2026-09-13): recorded as model_unavailable, which is
    retryable, never as a concurrency limit the API no longer enforces."""
    class AdmissionRejected(RuntimeError):
        pass

    background.set_generation_factory(_generation(error=AdmissionRejected("lanes full")))

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        return await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    assert asyncio.run(scenario())["error_code"] == "model_unavailable"


# ---------------------------------------------------------------------------
# One concurrency counter per project (2026-09-13)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("limits_enforced")
def test_a_background_job_is_refused_while_the_projects_streams_hold_every_slot(project):
    """The verifier's bypass: a key at its limit of streams could still start
    `max_concurrency` background jobs, because the two were counted in
    different dicts. Both now draw on `quotas.concurrency_slot`."""
    background.set_generation_factory(_generation())
    caller = _caller(project, max_concurrency=2)

    async def scenario():
        with quotas.concurrency_slot(caller, "stream"), quotas.concurrency_slot(caller, "sync"):
            with pytest.raises(errors.ApiError) as raised:
                await background.start(_spec(), caller=caller)
        rows = await db.run_in_thread(db.list_api_responses, project["id"])
        return raised.value, rows

    refusal, rows = asyncio.run(scenario())

    assert refusal.code == "concurrency_limit_exceeded"
    assert rows == []


@pytest.mark.usefixtures("limits_enforced")
def test_a_running_job_holds_its_projects_slot_until_it_has_finished(project):
    gate, entered = asyncio.Event(), asyncio.Event()
    background.set_generation_factory(_generation(gate=gate, entered=entered))
    caller = _caller(project, max_concurrency=1)

    async def scenario():
        await background.start(_spec(), caller=caller)
        await entered.wait()
        during = quotas.in_flight(caller, "background")
        with pytest.raises(errors.ApiError) as raised:
            with quotas.concurrency_slot(caller, "sync"):
                pass
        gate.set()
        await background.drain(timeout=10)
        await asyncio.sleep(0)
        return during, raised.value.code, quotas.in_flight(caller)

    assert asyncio.run(scenario()) == (1, "concurrency_limit_exceeded", 0)


def test_a_job_cancelled_before_its_first_step_still_gives_its_slot_back(project):
    """A task can be cancelled before it ever runs (a shutdown right after the
    202). Its own `finally` then never executes, so a slot released only
    there would be stranded — the release is a done-callback on the task."""
    background.set_generation_factory(_generation())
    caller = _caller(project, max_concurrency=1)

    async def scenario():
        await background.start(_spec(), caller=caller)
        for task in list(background._tasks):
            task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return quotas.in_flight(caller)

    assert asyncio.run(scenario()) == 0


def test_the_generation_is_always_closed_even_when_the_job_is_cancelled(project):
    """An abandoned generator holds its admission lane and its upstream HTTP
    response open until garbage collection (CONTRACT-3 §10), and
    `Generation.stream()`'s own `finally` does not run promptly when its
    consumer is abandoned — so the consumer asks."""
    closed: list = []
    first = asyncio.Event()

    class _Fake:
        def __init__(self, spec):
            self.spec, self.usage, self.error = spec, None, None
            self.first_token_at = None

        async def stream(self):
            yield _Chunk("token", "a")
            first.set()
            await asyncio.sleep(5)
            yield _Chunk("token", "b")

        async def aclose(self):
            closed.append(self.spec.response_id)

    background.set_generation_factory(_Fake)

    async def scenario():
        row = await _start(project)
        await first.wait()
        await background.request_cancel(row["id"], project["id"])
        # The stub is parked in `sleep(5)`, so the cancel is only seen when
        # the next chunk arrives — the documented limit. What is asserted here
        # is that the generation is closed either way.
        await background.drain(timeout=10)
        return row

    row = asyncio.run(scenario())

    assert closed == [row["id"]]


def test_the_generation_is_closed_when_the_job_fails(project):
    closed: list = []
    background.set_generation_factory(
        _generation(closed=closed, raises=RuntimeError("engine went away"))
    )

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        return row

    row = asyncio.run(scenario())

    assert closed == [row["id"]]


def test_a_restart_sweep_closes_background_rows_no_process_is_running(project):
    """Left alone, an `in_progress` row says so forever and a caller polls it
    forever — which is exactly what `research_runs` did until 2026-08-28."""
    orphan = db.create_api_response(
        project["id"], WORKSPACE, MODEL.id, "req_orphan", background=True
    )
    db.update_api_response(orphan["id"], project["id"], status="in_progress")
    synchronous = db.create_api_response(
        project["id"], WORKSPACE, MODEL.id, "req_sync", background=False
    )
    db.update_api_response(synchronous["id"], project["id"], status="in_progress")

    closed = asyncio.run(background.reconcile_interrupted(project["id"]))

    repaired = db.get_api_response(orphan["id"], project["id"])
    untouched = db.get_api_response(synchronous["id"], project["id"])

    assert closed == 1
    assert repaired["status"] == "failed"
    assert repaired["error_code"] == background.INTERRUPTED_CODE
    # A synchronous response died with the socket that was waiting on it;
    # nobody is polling for it and it is not this sweep's business.
    assert untouched["status"] == "in_progress"


def test_the_restart_sweep_leaves_a_job_this_process_is_still_running_alone(project):
    gate, entered = asyncio.Event(), asyncio.Event()
    background.set_generation_factory(_generation(gate=gate, entered=entered))

    async def scenario():
        row = await _start(project)
        await entered.wait()
        closed = await background.reconcile_interrupted(project["id"])
        mid = await db.run_in_thread(db.get_api_response, row["id"], project["id"])
        gate.set()
        await background.drain(timeout=10)
        return closed, mid, await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    closed, mid, final = asyncio.run(scenario())

    assert closed == 0
    assert mid["status"] == "in_progress"
    assert final["status"] == "completed"


# ---------------------------------------------------------------------------
# What happens afterwards
# ---------------------------------------------------------------------------


def test_the_routers_recorder_sees_the_outcome_and_cannot_break_the_response(project):
    """`on_finish` is the router's ledger write (CONTRACT-3 §16: exactly one
    usage row per request). A ledger failure must not turn a completed
    response into a failed one."""
    seen: list = []

    async def on_finish(outcome):
        seen.append(outcome)
        raise RuntimeError("the ledger is down")

    background.set_generation_factory(
        _generation([("token", "recorded")], usage={"prompt_tokens": 3, "completion_tokens": 4})
    )

    async def scenario():
        row = await _start(project, on_finish=on_finish)
        await background.drain(timeout=10)
        return await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    final = asyncio.run(scenario())

    assert len(seen) == 1
    assert seen[0].status == "completed"
    assert seen[0].text == "recorded"
    assert seen[0].usage == {"prompt_tokens": 3, "completion_tokens": 4}
    assert seen[0].duration_ms is not None
    # The text is still stored even though the recorder blew up, because that
    # write is this module's and not the hook's.
    assert final["output_text"] == "recorded"


def test_a_completed_job_queues_its_webhook_without_the_generated_text(project):
    db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://hooks.customer.example/hook",
        [sender.RESPONSE_COMPLETED], "whsec_" + "x" * 40,
    )
    background.set_generation_factory(
        _generation([("token", "the answer nobody outside may read")])
    )

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        return row

    row = asyncio.run(scenario())

    due = db.due_webhook_deliveries(limit=10)
    assert len(due) == 1
    assert due[0]["event_type"] == sender.RESPONSE_COMPLETED
    assert due[0]["response_id"] == row["id"]
    assert "the answer nobody outside may read" not in str(due[0]["payload"])


def test_a_cancelled_job_queues_the_cancelled_event(project):
    db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://hooks.customer.example/hook",
        list(sender.SUBSCRIBABLE_EVENTS), "whsec_" + "x" * 40,
    )
    gate = asyncio.Event()
    background.set_generation_factory(_generation(gate=gate))

    async def scenario():
        row = await _start(project)
        # The gate holds the generation before its first chunk, so the cancel
        # cannot lose a race with a job that would otherwise have finished.
        await background.request_cancel(row["id"], project["id"])
        gate.set()
        await background.drain(timeout=10)

    asyncio.run(scenario())

    due = db.due_webhook_deliveries(limit=10)
    assert [row["event_type"] for row in due] == [sender.RESPONSE_CANCELLED]


def test_a_webhook_that_cannot_be_queued_does_not_fail_the_response(monkeypatch, project):
    """The last thing a job does must not be able to rewrite what it already
    recorded."""
    async def explode(*args, **kwargs):
        raise RuntimeError("the webhook table is on fire")

    monkeypatch.setattr(sender, "emit_response_event", explode)
    background.set_generation_factory(_generation([("token", "safe")]))

    async def scenario():
        row = await _start(project)
        await background.drain(timeout=10)
        return await db.run_in_thread(db.get_api_response, row["id"], project["id"])

    final = asyncio.run(scenario())

    assert final["status"] == "completed" and final["output_text"] == "safe"


# ---------------------------------------------------------------------------
# The restart orphan, repaired where its poller reads it (2026-09-13)
# ---------------------------------------------------------------------------


def test_a_background_row_a_dead_process_left_open_is_closed_when_it_is_read(
    monkeypatch, project
):
    """`reconcile_interrupted` has no production caller, so a job cut off by a
    deploy stayed `in_progress` and its poller polled for ever."""
    from datetime import datetime, timedelta, timezone

    orphan = db.create_api_response(
        project["id"], WORKSPACE, MODEL.id, "req_orphan", background=True
    )
    db.update_api_response(orphan["id"], project["id"], status="in_progress")
    monkeypatch.setattr(
        background, "PROCESS_STARTED_AT", datetime.now(timezone.utc) + timedelta(minutes=1)
    )

    repaired = asyncio.run(
        background.repair_if_orphaned(db.get_api_response(orphan["id"], project["id"]))
    )

    assert repaired["status"] == "failed"
    assert repaired["error_code"] == background.INTERRUPTED_CODE


def test_a_row_created_after_this_process_started_is_never_treated_as_an_orphan(
    monkeypatch, project
):
    """It may be a sibling process's job during a blue/green overlap."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(
        background, "PROCESS_STARTED_AT", datetime.now(timezone.utc) - timedelta(minutes=1)
    )
    row = db.create_api_response(
        project["id"], WORKSPACE, MODEL.id, "req_sibling", background=True
    )

    untouched = asyncio.run(background.repair_if_orphaned(row))

    assert untouched["status"] == "queued"
    assert db.get_api_response(row["id"], project["id"])["status"] == "queued"


# ---------------------------------------------------------------------------
# Unlimited by default (owner decision, 2026-09-13)
# ---------------------------------------------------------------------------


def test_with_the_limits_off_jobs_beyond_the_projects_ceiling_all_run_and_give_their_slots_back(
    project,
):
    """Six background jobs held open at once in a project whose ceiling is one
    — the shape that refuses the second job when enforced. With
    PUBLIC_API_ENFORCE_LIMITS off every job is accepted, all six are counted
    in flight while they run, a sync request beside them still gets a slot,
    and every slot comes back when they finish."""
    assert settings.public_api_enforce_limits is False
    gate, entered = asyncio.Event(), asyncio.Event()
    background.set_generation_factory(_generation(gate=gate, entered=entered))
    caller = _caller(project, max_concurrency=1)

    async def scenario():
        rows = [await background.start(_spec(), caller=caller) for _ in range(6)]
        await entered.wait()
        during = quotas.in_flight(caller, "background")
        with quotas.concurrency_slot(caller, "sync"):
            beside = quotas.in_flight(caller)
        gate.set()
        await background.drain(timeout=10)
        await asyncio.sleep(0)
        stored = await db.run_in_thread(db.list_api_responses, project["id"])
        return rows, during, beside, quotas.in_flight(caller), stored

    rows, during, beside, after, stored = asyncio.run(scenario())

    assert len({row["id"] for row in rows}) == 6
    assert during == 6
    assert beside == 7
    assert after == 0
    assert sorted(str(row["status"]) for row in stored) == ["completed"] * 6
