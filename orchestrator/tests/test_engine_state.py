"""app/engine_state.py — the engine controller's verdict, as this process sees it.

Offline: the controller is an httpx MockTransport, the clock is patched.
What is pinned (CONTRACT §8.1): a valid document becomes a snapshot and
the gauge; anything else — unreachable, non-200, malformed, a state name
that is not one of the nine, a stale read, a document that was already
old when it was read — is `unknown`, the gauge reads -1, and unknown NEVER
opens the breaker. That the READY event a queued generation sleeps on is
set and cleared by the verdict. And that /health carries the snapshot
without touching the network.
"""
from __future__ import annotations

import asyncio
import json
import time as _time

import httpx
import pytest

from app import breaker, engine_state, metrics
from app.config import settings


def _doc(state: str, **extra) -> dict:
    base = {
        "schema": 1,
        # Fresh: a document's own timestamp counts against freshness (v2).
        "generated_at": _time.time(),
        "state": state,
        "state_code": engine_state.STATE_CODES[state],
        "reason": "canary ok in 0.41s",
        "since": 1757629000.0,
        "primary_ready": state in ("READY", "BUSY", "DEGRADED"),
        "router_available": True,
        "incident": None,
        "recovery": {"in_progress": False, "step": "idle"},
        "signals": {
            "head_container": {"running": True, "health": "healthy", "restart_count": 1,
                               "started_at": _time.time() - 7200.0, "engine_process_alive": True},
            "engine": {"requests_running": 2, "requests_waiting": 0},
        },
    }
    base.update(extra)
    return base


class _Controller:
    """A fake `GET /state`: whatever `self.reply` says, or a transport error."""

    def __init__(self) -> None:
        self.reply = (200, _doc("READY"))
        self.error: Exception | None = None
        self.hits = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hits += 1
        if self.error is not None:
            raise self.error
        status, body = self.reply
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), timeout=2.0)


class _Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(engine_state.time, "monotonic", c.monotonic)
    return c


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    metrics.reset()
    engine_state.reset()
    breaker.reset()
    monkeypatch.setattr(settings, "engine_controller_url", "http://vllm:9838/state")
    yield
    engine_state.reset()
    breaker.reset()
    metrics.reset()


def _gauge() -> float:
    return metrics._gauges["llm_engine_state_code"][()]


async def _poll(controller: _Controller):
    async with controller.client() as client:
        return await engine_state.poll_once(client)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_accepts_the_contract_document():
    doc = _doc("RECOVERING", incident={"id": "20260911T221550Z", "category": "worker_rank_dead"},
               recovery={"in_progress": True, "step": "wait_load"}, generated_at=1757630000.0)
    snap = engine_state.parse_state_document(doc, observed_at=10.0, read_wall=1757630004.0)
    assert snap.state == "RECOVERING" and snap.state_code == 6
    assert snap.incident_id == "20260911T221550Z"
    assert snap.recovery_step == "wait_load"
    assert snap.primary_ready is False and snap.router_available is True
    assert snap.requests_running == 2 and snap.head_started_at == pytest.approx(doc["signals"]["head_container"]["started_at"])
    # Age = how long ago it was read + how old it already was at the read.
    assert snap.generated_lag_s == 4.0
    assert snap.age_s(now=25.0) == 19.0
    # The v1 name of code 7 is still understood (a mixed deploy).
    legacy = engine_state.parse_state_document(
        {**_doc("QUEUEING"), "state": "FALLBACK_ACTIVE"}, observed_at=0.0
    )
    assert legacy.state == "QUEUEING" and legacy.state_code == 7


@pytest.mark.parametrize(
    "doc",
    [
        "not an object",
        {},
        {**_doc("READY"), "state": "EXPLODED"},
        _doc("READY", state_code=8),  # code disagrees with the name
        _doc("READY", state_code="2"),
        _doc("READY", state_code=True),
    ],
)
def test_parse_rejects_what_it_cannot_decide_from(doc):
    with pytest.raises(ValueError):
        engine_state.parse_state_document(doc, observed_at=0.0)


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


def test_a_good_poll_publishes_the_state_and_the_gauge(clock):
    controller = _Controller()
    controller.reply = (200, _doc("BUSY"))
    snap = asyncio.run(_poll(controller))
    assert snap is not None and snap.state == "BUSY"
    assert engine_state.snapshot() is snap
    assert engine_state.unknown() is False
    assert _gauge() == 3
    assert controller.hits == 1
    shown = engine_state.describe()
    assert shown["state"] == "BUSY" and shown["state_code"] == 3
    assert shown["unknown"] is False and shown["age_s"] == 0.0
    assert "last_error" not in shown


@pytest.mark.parametrize(
    "reply",
    [
        (500, {"error": "boom"}),
        (200, "<html>not json</html>"),
        (200, {"state": "NOPE", "state_code": 99}),
        (200, {"state": "READY", "state_code": 5}),
    ],
)
def test_a_bad_answer_is_unknown_and_the_gauge_reads_minus_one(clock, reply):
    controller = _Controller()
    controller.reply = reply
    assert asyncio.run(_poll(controller)) is None
    assert engine_state.snapshot() is None
    assert engine_state.unknown() is True
    assert engine_state.external_open() is None
    assert _gauge() == -1
    assert engine_state.describe()["last_error"]


def test_an_unreachable_controller_is_unknown(clock):
    controller = _Controller()
    controller.error = httpx.ConnectError("connection refused")
    assert asyncio.run(_poll(controller)) is None
    assert engine_state.unknown() is True
    assert _gauge() == -1
    assert "ConnectError" in engine_state.describe()["last_error"]


def test_a_blank_url_polls_nothing(clock, monkeypatch):
    monkeypatch.setattr(settings, "engine_controller_url", "")
    controller = _Controller()
    assert asyncio.run(_poll(controller)) is None
    assert controller.hits == 0
    assert _gauge() == -1


def test_a_stale_snapshot_is_unknown_and_keeps_its_last_state_for_health(clock):
    controller = _Controller()
    # Stamped a hair ahead of the wall clock so the document's own age is
    # exactly zero at the read and only the read age is under test here.
    controller.reply = (200, _doc("RECOVERING", primary_ready=False, generated_at=_time.time() + 2.0))
    asyncio.run(_poll(controller))
    assert engine_state.external_open() == "RECOVERING"
    clock.now += engine_state.STALE_AFTER_S
    assert engine_state.unknown() is False  # exactly at the edge still counts
    clock.now += 0.1
    assert engine_state.unknown() is True
    assert engine_state.external_open() is None
    shown = engine_state.describe()
    assert shown["unknown"] is True and shown["state"] == "RECOVERING"
    assert shown["age_s"] == pytest.approx(15.1)
    # The controller comes back: a fresh read, and the poller re-publishes.
    controller.reply = (200, _doc("READY"))
    asyncio.run(_poll(controller))
    assert engine_state.unknown() is False and _gauge() == 2


def test_an_unreachable_controller_keeps_the_previous_snapshot_but_not_its_trust(clock):
    controller = _Controller()
    asyncio.run(_poll(controller))
    controller.error = httpx.ConnectError("refused")
    asyncio.run(_poll(controller))
    # The last good read is still shown (with its age), but the gauge says
    # unknown as soon as it is too old to act on.
    assert engine_state.snapshot() is not None
    assert engine_state.unknown() is False and _gauge() == 2
    clock.now += 20.0
    asyncio.run(_poll(controller))
    assert engine_state.unknown() is True and _gauge() == -1


# ---------------------------------------------------------------------------
# The breaker's external input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["STARTING", "WEDGED", "RECOVERING", "DOWN"])
def test_bad_states_open_the_main_breaker_at_once(clock, state):
    controller = _Controller()
    # STARTING opens when the head really is starting (a young container);
    # the controller-restart shape has its own test below.
    head = {"running": True, "started_at": _time.time() - 20.0, "engine_process_alive": True}
    controller.reply = (200, _doc(state, signals={"head_container": head}))
    asyncio.run(_poll(controller))
    assert engine_state.external_open() == state
    main = breaker.get("main")
    assert main.state == breaker.OPEN
    assert main.describe()["held_open_by"] == state
    assert breaker.main_allows() is False


@pytest.mark.parametrize("state", ["MONITORING_UNKNOWN", "READY", "BUSY", "DEGRADED", "QUEUEING"])
def test_other_states_and_unknown_never_open(clock, state):
    controller = _Controller()
    controller.reply = (200, _doc(state))
    asyncio.run(_poll(controller))
    assert engine_state.external_open() is None
    assert breaker.get("main").state == breaker.CLOSED
    # And a controller that vanishes afterwards changes nothing.
    controller.error = httpx.ConnectError("gone")
    clock.now += 60.0
    asyncio.run(_poll(controller))
    assert engine_state.unknown() is True
    assert breaker.get("main").state == breaker.CLOSED


def test_recovering_then_ready_releases_the_hold_into_the_cooldown(clock, monkeypatch):
    monkeypatch.setattr(settings, "llm_breaker_cooldown_s", 10.0)
    controller = _Controller()
    controller.reply = (200, _doc("RECOVERING"))
    asyncio.run(_poll(controller))
    main = breaker.get("main")
    assert main.state == breaker.OPEN
    clock.now += 300.0
    asyncio.run(_poll(controller))
    assert main.state == breaker.OPEN  # held for the whole reload
    controller.reply = (200, _doc("READY"))
    asyncio.run(_poll(controller))
    clock.now += 10.0
    assert main.state == breaker.HALF_OPEN
    assert main.acquire() is not None  # the one canary


# ---------------------------------------------------------------------------
# The poller lifecycle and /health
# ---------------------------------------------------------------------------


def test_start_and_stop_are_idempotent_and_off_when_disabled(monkeypatch):
    async def run():
        monkeypatch.setattr(settings, "engine_state_poll_s", 0.0)
        engine_state.start()
        assert engine_state._task is None
        monkeypatch.setattr(settings, "engine_state_poll_s", 5.0)
        monkeypatch.setattr(settings, "engine_controller_url", "")
        engine_state.start()
        assert engine_state._task is None
        monkeypatch.setattr(settings, "engine_controller_url", "http://127.0.0.1:9/state")
        engine_state.start()
        first = engine_state._task
        assert first is not None
        engine_state.start()
        assert engine_state._task is first
        await engine_state.stop()
        assert engine_state._task is None
        await engine_state.stop()

    asyncio.run(run())


def test_health_carries_the_snapshot_and_the_breakers_without_probing(clock, monkeypatch):
    from app import health

    controller = _Controller()
    controller.reply = (200, _doc("WEDGED"))
    asyncio.run(_poll(controller))
    hits_before = controller.hits

    async def endpoint_ok(client, base_url):
        return {"status": "ok"}

    async def optional_disabled(client):
        return {"status": "disabled", "detail": "disabled by configuration"}

    monkeypatch.setattr(health, "_probe_vllm", endpoint_ok)
    monkeypatch.setattr(health, "_probe_ocr", optional_disabled)
    monkeypatch.setattr(health, "_probe_reranker", optional_disabled)
    monkeypatch.setattr(health, "_check_duckdb", lambda path: {"status": "ok"})
    monkeypatch.setattr(health, "_check_app_db", lambda: {"status": "ok"})
    monkeypatch.setattr(health, "_check_embedding_index", lambda: {"status": "empty"})
    report = asyncio.run(health.check_dependencies())
    # The view rides on the main model's entry and never moves `status`:
    # an OPEN breaker is the orchestrator protecting itself, not a reason
    # to restart the orchestrator.
    assert report["status"] == "ok"
    assert report["checks"]["vllm"]["status"] == "ok"
    engine = report["checks"]["vllm"]["engine"]
    assert engine["controller"]["state"] == "WEDGED"
    assert engine["breakers"]["main"]["state"] == "OPEN"
    assert engine["breakers"]["main"]["held_open_by"] == "WEDGED"
    assert set(engine["breakers"]) == {"main"}
    assert "fallback" not in engine and engine["answer_engine"] == "main"
    # One-model mode: the queue and the lanes ride beside the breaker.
    assert engine["queue"]["queued_generations"] == 0
    assert engine["queue"]["max_wait_s"] == settings.llm_queue_max_wait_s
    assert engine["admission"]["normal"]["capacity"] == settings.admission_normal_max
    assert engine["admission"]["long"]["capacity"] == settings.admission_long_max
    assert "engine" not in report["checks"]["vllm-embed"]
    assert controller.hits == hits_before, "/health reads memory, never the controller"
    json.dumps(report["checks"]["vllm"]["engine"])  # serialisable as it stands


# ---------------------------------------------------------------------------
# v2: freshness from the document's own timestamp
# ---------------------------------------------------------------------------


def test_a_document_that_was_already_old_when_read_is_unknown(clock):
    """A controller whose poll loop hung keeps serving the last document it
    built. Its `generated_at` says so: the read succeeds, the verdict is
    unknown, the gauge reads -1, and a bad state in it opens nothing."""
    controller = _Controller()
    controller.reply = (200, _doc("RECOVERING", generated_at=_time.time() - 60.0))
    snap = asyncio.run(_poll(controller))
    assert snap is not None and snap.generated_lag_s == pytest.approx(60.0, abs=2.0)
    assert engine_state.unknown() is True
    assert engine_state.external_open() is None
    assert breaker.get("main").state == breaker.CLOSED
    assert _gauge() == -1
    assert "old at read" in engine_state.describe()["last_error"]
    # A document with no timestamp at all is never fresh.
    controller.reply = (200, {**_doc("DOWN"), "generated_at": None})
    asyncio.run(_poll(controller))
    assert engine_state.unknown() is True and breaker.get("main").state == breaker.CLOSED
    # And a fresh one is trusted again.
    controller.reply = (200, _doc("DOWN"))
    asyncio.run(_poll(controller))
    assert engine_state.unknown() is False and breaker.get("main").state == breaker.OPEN


def test_staleness_follows_the_poll_interval(clock, monkeypatch):
    """ENGINE_STATE_POLL_S=20 must not make every snapshot stale before the
    next read (review finding engine_state.py:67)."""
    monkeypatch.setattr(settings, "engine_state_poll_s", 20.0)
    assert engine_state.stale_after_s() == 60.0
    controller = _Controller()
    controller.reply = (200, _doc("RECOVERING"))
    asyncio.run(_poll(controller))
    clock.now += 45.0
    assert engine_state.unknown() is False and engine_state.external_open() == "RECOVERING"
    clock.now += 20.0
    assert engine_state.unknown() is True
    monkeypatch.setattr(settings, "engine_state_poll_s", 5.0)
    assert engine_state.stale_after_s() == 15.0


# ---------------------------------------------------------------------------
# v2: STARTING opens only when the head really is starting
# ---------------------------------------------------------------------------


def test_starting_from_a_controller_restart_does_not_open(clock):
    """The controller reports STARTING for a tick after ITS OWN restart while
    the head has served for hours (review finding engine_state.py:63)."""
    controller = _Controller()
    controller.reply = (200, _doc("STARTING", primary_ready=False))  # head started 2 h ago
    asyncio.run(_poll(controller))
    assert engine_state.external_open() is None
    assert breaker.get("main").state == breaker.CLOSED
    assert engine_state.serving() is False  # not proven either: a queued caller keeps waiting


@pytest.mark.parametrize(
    "extra",
    [
        {"incident": {"id": "20260912T000000Z", "category": "head_engine_dead"}},
        {"signals": {"head_container": {"running": True, "started_at": _time.time() - 30.0, "engine_process_alive": True}}},
        {"signals": {"head_container": {"running": False, "started_at": _time.time() - 7200.0}}},
        {"signals": {"head_container": {"running": True, "started_at": _time.time() - 7200.0, "engine_process_alive": False}}},
        {"signals": {}},  # nothing to qualify it with: taken at its word
    ],
)
def test_a_real_starting_opens(clock, extra):
    controller = _Controller()
    controller.reply = (200, _doc("STARTING", primary_ready=False, **extra))
    asyncio.run(_poll(controller))
    assert engine_state.external_open() == "STARTING"
    assert breaker.get("main").state == breaker.OPEN


# ---------------------------------------------------------------------------
# v2: the READY event and the edge listeners
# ---------------------------------------------------------------------------


def test_the_ready_event_follows_the_verdict():
    # No fake clock here: the `clock` fixture patches time.monotonic for the
    # whole process, which freezes the event loop's own timers, and this
    # test waits on real ones (wait_ready's timeout).
    controller = _Controller()
    fired: list = []
    engine_state.on_ready(lambda: fired.append(1))

    async def run():
        event = engine_state.ready_event()
        assert not event.is_set(), "no verdict yet"
        controller.reply = (200, _doc("RECOVERING"))
        await _poll(controller)
        assert not event.is_set() and fired == []
        assert await engine_state.wait_ready(0.01) is False
        controller.reply = (200, _doc("READY"))
        waiter = asyncio.create_task(engine_state.wait_ready(5.0))
        await asyncio.sleep(0)
        await _poll(controller)
        assert event.is_set() and await waiter is True
        assert fired == [1], "the edge fired once"
        controller.reply = (200, _doc("BUSY"))
        await _poll(controller)
        assert event.is_set() and fired == [1], "READY → BUSY is not an edge"
        controller.reply = (200, _doc("MONITORING_UNKNOWN"))
        await _poll(controller)
        assert event.is_set(), "cannot-see changes nothing"
        controller.reply = (200, _doc("WEDGED"))
        await _poll(controller)
        assert not event.is_set()
        controller.reply = (200, _doc("DEGRADED"))
        await _poll(controller)
        assert event.is_set() and fired == [1, 1], "DEGRADED still serves: a second edge"
        # An unreachable controller leaves the event where it was.
        controller.error = httpx.ConnectError("gone")
        await _poll(controller)
        assert event.is_set()

    asyncio.run(run())


def test_engine_load_is_the_controllers_sample(clock):
    controller = _Controller()
    controller.reply = (200, _doc("BUSY", signals={"engine": {"requests_running": 7, "requests_waiting": 3}}))
    asyncio.run(_poll(controller))
    load = engine_state.engine_load()
    assert load["requests_running"] == 7.0 and load["requests_waiting"] == 3.0
    controller.reply = (200, _doc("READY", signals={}))
    asyncio.run(_poll(controller))
    assert engine_state.engine_load() is None, "no sample in the document"
    controller.reply = (200, _doc("BUSY"))
    asyncio.run(_poll(controller))
    assert engine_state.engine_load() is not None
    clock.now += 60.0
    assert engine_state.engine_load() is None, "a stale sample is no sample"


def test_controller_url_accepts_the_scripts_base_url_shape():
    from app.config import _controller_state_url

    assert _controller_state_url("http://127.0.0.1:9838") == "http://127.0.0.1:9838/state"
    assert _controller_state_url("http://127.0.0.1:9838/") == "http://127.0.0.1:9838/state"
    assert _controller_state_url("http://vllm:9838/state") == "http://vllm:9838/state"
    assert _controller_state_url("") == ""
