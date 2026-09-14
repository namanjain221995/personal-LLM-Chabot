"""A deterministic fake main engine and a fake engine controller (2026-09-13).

WHY. The durable runner's promises are about ORDER and IDENTITY — the text a
resumed answer ends with is byte-identical to one that was never interrupted,
no sequence number is reused, a poison prompt consumes at most two recoveries
— and a suite with no GPU can only prove them against an engine whose output
is a pure function of its input. So:

* `FakeMainEngine` replaces `llm.stream_chat_events`, accepts the T1 kwargs
  (`continue_final_message`, `on_dispatch`, `admission_patient`, ...), and
  emits token i as `w{i} `. A continuation counts the words already in the
  final assistant message and carries on from there, exactly as vLLM's
  `continue_final_message` extends a prefix. Hooks can make it hang, raise,
  or restart the fake controller's head when it sees a prompt.
* `FakeController` is a `liveness.EngineView` with the controller states the
  guard reads (READY/BUSY/WEDGED/RECOVERING/STARTING/DOWN/MONITORING_UNKNOWN,
  a cold-start DOWN on an old head, head_started_at, incident ids, the
  recovery budget and the engine load sample).
* `VirtualClock` makes an hour of silence a loop iteration.
"""
from __future__ import annotations

import asyncio
import dataclasses
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

SERVING = frozenset({"READY", "BUSY", "DEGRADED"})
BAD = frozenset({"WEDGED", "RECOVERING", "STARTING", "DOWN"})
STARTING_HEAD_AGE_S = 900.0
SAMPLE_EVERY_S = 15.0


class VirtualClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


@dataclass
class ControllerState:
    state: Optional[str] = "READY"  # None: stale/unreachable controller
    reason: str = ""
    head_started_at: Optional[float] = 0.0
    head_alive: bool = True
    incident_id: Optional[str] = None
    requests_running: Optional[int] = 1
    requests_waiting: Optional[int] = 0
    headroom: Optional[int] = 3
    #: How old the engine sample is (None: the view does not know, as with
    #: an engine_state that does not expose the sample's own timestamp).
    sample_age_s: Optional[float] = None


class FakeController:
    """The controller's /state document as the guard sees it."""

    def __init__(self, clock: Callable[[], float], **fields: Any) -> None:
        self.clock = clock
        self.doc = ControllerState(**fields)
        self._chunk_at: Optional[float] = None
        self._serving_at: Optional[float] = None
        self.head_restarts = 0

    def set(self, **fields: Any) -> None:
        self.doc = dataclasses.replace(self.doc, **fields)

    def restart_head(self, now: Optional[float] = None) -> None:
        moment = self.clock() if now is None else now
        self.head_restarts += 1
        self.set(head_started_at=moment, incident_id=f"inc-{self.head_restarts}")

    # -- the EngineView protocol --

    def _unknown(self) -> bool:
        return self.doc.state is None

    def proven_not_serving(self, now: float) -> Optional[str]:
        doc = self.doc
        if self._unknown() or doc.state not in BAD:
            return None
        head_age = None if doc.head_started_at is None else now - doc.head_started_at
        old_alive_head = doc.head_alive and head_age is not None and head_age > STARTING_HEAD_AGE_S
        if doc.state == "STARTING" and old_alive_head and not doc.incident_id:
            return None  # the controller's own cold start
        if doc.state == "DOWN" and doc.reason.startswith("cold start timeout") and old_alive_head and not doc.incident_id:
            return None
        return doc.state

    def serving(self, now: float) -> Optional[bool]:
        if self._unknown() or self.doc.state == "MONITORING_UNKNOWN":
            return None
        verdict = self.doc.state in SERVING
        if verdict:
            self._serving_at = now
        return verdict

    def engine_load(self, now: float) -> Optional[Dict[str, float]]:
        if self._unknown() or self.doc.requests_running is None:
            return None
        load = {
            "requests_running": float(self.doc.requests_running),
            "requests_waiting": float(self.doc.requests_waiting or 0),
            "age_s": 0.0,
        }
        if self.doc.sample_age_s is not None:
            load["sample_age_s"] = float(self.doc.sample_age_s)
        return load

    def head_started_at(self, now: float) -> Optional[float]:
        return None if self._unknown() else self.doc.head_started_at

    def incident_id(self, now: float) -> Optional[str]:
        return None if self._unknown() else self.doc.incident_id

    def sample_at(self, now: float) -> Optional[float]:
        if self._unknown():
            return None
        return float(int(now // SAMPLE_EVERY_S) * SAMPLE_EVERY_S)

    def serving_evidence_at(self, now: float) -> Optional[float]:
        self.serving(now)
        known = [t for t in (self._chunk_at, self._serving_at) if t is not None]
        return max(known) if known else None

    def recovery_headroom(self, now: float) -> Optional[int]:
        return None if self._unknown() else self.doc.headroom

    def note_chunk(self, now: float) -> None:
        self._chunk_at = now


def word(i: int) -> str:
    return f"w{i} "


def expected_text(n: int) -> str:
    return "".join(word(i) for i in range(n))


@dataclass
class EngineCall:
    messages: List[Dict[str, Any]]
    kwargs: Dict[str, Any]
    start_index: int
    tokens_sent: int = 0
    closed: bool = False


class FakeMainEngine:
    """`llm.stream_chat_events` with deterministic output and fault hooks.

    `answer_tokens`: how long a complete answer is. `delay_s`: pause before
    each token. `before_token(engine, call, index)`: an awaitable hook run
    before each token; it may sleep (a silent engine), raise (a connection
    error) or restart the controller's head.
    """

    def __init__(
        self,
        *,
        answer_tokens: int = 40,
        delay_s: float = 0.0,
        before_token: Optional[Callable[["FakeMainEngine", EngineCall, int], Any]] = None,
        report_usage: bool = True,
        prompt_tokens: int = 17,
        respect_max_tokens: bool = True,
    ) -> None:
        self.respect_max_tokens = bool(respect_max_tokens)
        self.answer_tokens = int(answer_tokens)
        self.delay_s = float(delay_s)
        self.before_token = before_token
        self.report_usage = report_usage
        self.prompt_tokens = int(prompt_tokens)
        self.calls: List[EngineCall] = []

    def install(self, monkeypatch: Any) -> "FakeMainEngine":
        from app import llm

        monkeypatch.setattr(llm, "stream_chat_events", self)
        return self

    @property
    def open_streams(self) -> int:
        return sum(1 for call in self.calls if not call.closed)

    def __call__(
        self,
        messages: Any,
        *,
        model_choice: str = "smart",
        effort: str = "fast",
        temperature: float = 0.2,
        max_tokens: Optional[int] = None,
        wall_clock_s: Any = None,
        wall_clock_marker: bool = True,
        read_timeout_s: Any = None,
        continue_final_message: bool = False,
        admission_patient: bool = False,
        on_dispatch: Optional[Callable[[], None]] = None,
        admission_run_id: Optional[str] = None,
    ) -> Any:
        from app import llm

        messages = [dict(m) for m in messages]
        start = 0
        if continue_final_message and messages and messages[-1].get("role") == "assistant":
            start = len(str(messages[-1].get("content") or "").split())
        call = EngineCall(
            messages=messages,
            kwargs=dict(
                max_tokens=max_tokens, continue_final_message=continue_final_message,
                admission_patient=admission_patient, wall_clock_s=wall_clock_s,
                read_timeout_s=read_timeout_s, admission_run_id=admission_run_id,
            ),
            start_index=start,
        )
        self.calls.append(call)
        engine = self

        async def run() -> Any:
            try:
                if on_dispatch is not None:
                    on_dispatch()
                limit = engine.answer_tokens
                if max_tokens is not None and engine.respect_max_tokens:
                    limit = min(limit, start + int(max_tokens))
                index = start
                while index < limit:
                    if engine.before_token is not None:
                        result = engine.before_token(engine, call, index)
                        if asyncio.iscoroutine(result):
                            await result
                    if engine.delay_s:
                        await asyncio.sleep(engine.delay_s)
                    call.tokens_sent += 1
                    yield ("token", word(index))
                    index += 1
                llm._finish_reason.set("stop" if index >= engine.answer_tokens else "length")
                if engine.report_usage:
                    llm._record_usage(engine.prompt_tokens + start, index - start)
            finally:
                call.closed = True

        return run()


@dataclass
class FakeSidecarEngine:
    """`engines.stream_chat` for router/OCR runs, deterministic like the main one."""

    answer_tokens: int = 12
    delay_s: float = 0.0
    #: The FIRST call goes silent after this many tokens (a lost request).
    hang_first_after: Optional[int] = None
    calls: List[Dict[str, Any]] = field(default_factory=list)

    def install(self, monkeypatch: Any) -> "FakeSidecarEngine":
        from app.publicapi import engines

        monkeypatch.setattr(engines, "stream_chat", self)
        monkeypatch.setattr(
            engines, "target",
            lambda key: engines.EngineTarget(key=key, base_url=f"http://fake-{key}:8000/v1", model=f"fake-{key}"),
        )
        return self

    def __call__(self, resolved: Any, messages: Any, *, max_tokens: int, temperature: float,
                 continue_final_message: bool = False, on_dispatch: Any = None) -> Any:
        from app import llm

        messages = [dict(m) for m in messages]
        start = 0
        if continue_final_message and messages and messages[-1].get("role") == "assistant":
            start = len(str(messages[-1].get("content") or "").split())
        self.calls.append({"engine": resolved.key, "messages": messages, "start": start})
        engine = self

        first_call = len(self.calls) == 1

        async def run() -> Any:
            if on_dispatch is not None:
                on_dispatch()
            index = start
            while index < min(engine.answer_tokens, start + int(max_tokens)):
                if first_call and engine.hang_first_after is not None and index >= engine.hang_first_after:
                    await asyncio.sleep(3600)
                if engine.delay_s:
                    await asyncio.sleep(engine.delay_s)
                yield ("token", word(index))
                index += 1
            llm._finish_reason.set("stop")
            llm._record_usage(9, index - start)

        return run()


# ------------------------------------------------------------- tenants --


@dataclass
class Tenant:
    workspace_id: str
    project: Dict[str, Any]
    keys: List[Dict[str, Any]]
    service_account: Optional[Dict[str, Any]] = None

    def caller(self, index: int = 0) -> Any:
        from app.publicapi import durable

        key = self.keys[index]
        return durable.Caller(
            project_id=self.project["id"], workspace_id=self.workspace_id, key_id=key["id"],
            service_account_id=key.get("service_account_id"),
        )


def make_tenant(
    workspace_id: str = "ws-durable", *, keys: int = 1, service_account_keys: int = 0, name: str = "Durable"
) -> Tenant:
    """A workspace, a project and real api_keys rows (the creator rule and
    the authorisation shim read them)."""
    import secrets

    from app import db

    with db.connection() as con:
        con.execute(
            "INSERT INTO workspaces (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
            (workspace_id, name),
        )
    project = db.create_api_project(workspace_id, f"{name} {secrets.token_hex(3)}", "live")
    rows: List[Dict[str, Any]] = []
    account = None
    if service_account_keys:
        account = db.create_service_account(project["id"], workspace_id, "svc")
    for index in range(keys + service_account_keys):
        rows.append(db.create_api_key(
            project["id"], workspace_id, f"key{index}", f"pub{secrets.token_hex(6)}", "digest", "abcd",
            service_account_id=(account["id"] if account is not None and index >= keys else None),
        ))
    return Tenant(workspace_id=workspace_id, project=project, keys=rows, service_account=account)


def spec(response_id: Optional[str] = None, **overrides: Any) -> Any:
    import secrets
    import time as _time

    from app.publicapi import streaming

    fields: Dict[str, Any] = dict(
        response_id=response_id or f"resp_{secrets.token_hex(12)}",
        model="techsara-35b",
        messages=[{"role": "user", "content": "Count for me."}],
        max_tokens=100_000,
        temperature=0.0,
        created_at=int(_time.time()),
        engine="main",
        requested_max_output_tokens=100_000,
        planned_max_output_tokens=100_000,
        context_window=1_000_000,
        context_reserve=512,
        estimated_input_tokens=10,
        bounded_input_tokens=40,
    )
    fields.update(overrides)
    return streaming.GenerationSpec(**fields)


def set_setting(monkeypatch: Any, name: str, value: Any) -> None:
    """Set a PUBLIC_API_* setting for one test in BOTH places it can be read:
    the environment (registry's fallback) and the `settings` attribute, which
    wins when config.py declares it — as T1's config does, reading the
    environment once at import, where a later `setenv` would change nothing."""
    from app.config import settings

    text = str(value)
    monkeypatch.setenv(name, text)
    typed: Any = value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "false"):
            typed = lowered == "true"
        else:
            try:
                typed = int(value)
            except ValueError:
                try:
                    typed = float(value)
                except ValueError:
                    typed = value
    monkeypatch.setattr(settings, name.lower(), typed, raising=False)


def fast_durable_settings(monkeypatch: Any, **extra: Any) -> None:
    """Scale every durable/liveness interval for a test run."""
    from app.publicapi import durable, liveness

    values = {
        "PUBLIC_API_EVENT_FLUSH_S": "0.02",
        "PUBLIC_API_LEASE_HEARTBEAT_S": "3600",
        "PUBLIC_API_LEASE_TTL_S": "60",
        "PUBLIC_API_FOLLOWER_POLL_S": "0.05",
        "PUBLIC_API_SUSPENDED_CLAIM_POLL_S": "0.05",
        "PUBLIC_API_LAPSED_SWEEP_S": "3600",
        "PUBLIC_API_RESUME_STAGGER_S": "0",
    }
    values.update({k: str(v) for k, v in extra.items()})
    for key, value in values.items():
        set_setting(monkeypatch, key, value)
    monkeypatch.setattr(durable, "heartbeat_s", lambda: 0.05)
    monkeypatch.setattr(liveness, "NOT_BAD_POLL_S", 0.02)


async def collect(handle: Any, after: int = 0, *, heartbeat: float = 0.05, limit_s: float = 20.0) -> List[Any]:
    """Every record a follower receives, until the run ends."""
    from app.publicapi import durable

    records: List[Any] = []

    async def run() -> None:
        async for item in handle.follow(after, heartbeat=heartbeat):
            if item is durable.HEARTBEAT:
                continue
            records.append(item)

    await asyncio.wait_for(run(), limit_s)
    return records


__all__ = [
    "ControllerState",
    "EngineCall",
    "FakeController",
    "FakeMainEngine",
    "FakeSidecarEngine",
    "Tenant",
    "VirtualClock",
    "collect",
    "fast_durable_settings",
    "make_tenant",
    "set_setting",
    "spec",
    "expected_text",
    "word",
]
