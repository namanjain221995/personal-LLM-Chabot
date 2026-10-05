"""The one way a pipeline stage asks the main model for a decision.

Every semantic stage -- intent, routing, discovery, linking, planning,
interpretation, answer -- posts through `StageModel.call` and gets back a
`ModelCall`: which stage, which model, whether it was really called, how long
it took, how many tokens, how many candidates it was shown, what it chose,
whether its output was usable and how many retries that took. The records are
what prove, per question, that the main model made each decision (spec §44).

Failure policy (§52): an unreachable model is `unavailable`; output that is
still unusable after the one structured retry is `invalid`. Neither is ever
replaced by a deterministic decision here -- the caller fails the question.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field as dataclass_field
from typing import Any, Callable

log = logging.getLogger(__name__)

UNAVAILABLE = "unavailable"
INVALID = "invalid"


@dataclass
class ModelCall:
    stage: str
    model_role: str = "main"
    model: str = ""
    endpoint: str = ""
    model_called: bool = False
    ok: bool = False
    duration_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    candidate_count: int | None = None
    selected: Any = None
    valid: bool = False
    retries: int = 0
    failure: str = ""                 # "" | unavailable | invalid
    error: str = ""
    # Two adjacent stages answered by one call (§42): the second stage's record
    # names the stage whose call carried it.
    combined_with: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class StageLog:
    """Per-request list of model calls. Passed down, never shared between requests."""

    def __init__(self) -> None:
        self.calls: list[ModelCall] = []

    def add(self, call: ModelCall) -> ModelCall:
        self.calls.append(call)
        return call

    def combined(self, stage: str, carrier: ModelCall, *, selected: Any = None,
                 valid: bool | None = None) -> ModelCall:
        """Record `stage` as decided by the same call that carried `carrier.stage`."""
        return self.add(ModelCall(
            stage=stage, model_role=carrier.model_role, model=carrier.model,
            endpoint=carrier.endpoint, model_called=carrier.model_called,
            ok=carrier.ok if valid is None else (carrier.ok and valid),
            duration_ms=0, selected=selected,
            valid=carrier.valid if valid is None else valid,
            retries=carrier.retries, failure=carrier.failure, error=carrier.error,
            combined_with=carrier.stage))

    def by_stage(self) -> dict[str, ModelCall]:
        """The last record per stage (a two-pass stage keeps its final pass)."""
        out: dict[str, ModelCall] = {}
        for call in self.calls:
            out[call.stage] = call
        return out


class StageModel:
    def __init__(self, endpoint: str, model: str, *, role: str = "main",
                 timeout: float = 60.0, client: Any = None) -> None:
        self.endpoint, self.model, self.role = endpoint, model, role
        self.timeout = timeout
        self.client = client

    def call(self, stage: str, messages: list[dict[str, str]], *,
             log_to: StageLog | None = None,
             validate: Callable[[Any], str] | None = None,
             soft: Callable[[Any], str] | None = None,
             max_tokens: int = 1200, candidate_count: int | None = None,
             retry_hint: str = "Return the JSON again, minified, with every required key."
             ) -> tuple[Any, ModelCall]:
        """One decision, with one structured retry. Never raises.

        `validate(payload)` returns "" when the payload is usable, else the reason,
        which the retry message repeats to the model verbatim. `soft(payload)`
        names a shape problem worth one retry that a later model stage can still
        settle: it triggers the retry but never fails the stage.
        """
        record = ModelCall(stage=stage, model_role=self.role, model=self.model,
                           endpoint=_safe(self.endpoint), candidate_count=candidate_count)
        if log_to is not None:
            log_to.add(record)
        started = time.perf_counter()
        payload, problem = self._once(messages, record, max_tokens)
        if problem == UNAVAILABLE:
            record.duration_ms = _ms(started)
            return None, record
        reason = problem or (validate(payload) if validate else "") or \
            (soft(payload) if soft else "")
        if reason:
            record.retries = 1
            retry = list(messages) + [
                {"role": "assistant",
                 "content": json.dumps(payload) if payload is not None else ""},
                {"role": "user", "content": f"That answer was unusable ({reason}). {retry_hint}"}]
            payload, problem = self._once(retry, record, max_tokens)
            if problem == UNAVAILABLE:
                record.duration_ms = _ms(started)
                return None, record
            reason = problem or (validate(payload) if validate else "")
        record.duration_ms = _ms(started)
        if reason:
            record.failure, record.error = INVALID, reason
            return None, record
        record.ok = record.valid = True
        return payload, record

    def _once(self, messages: list[dict[str, str]], record: ModelCall,
              max_tokens: int) -> tuple[Any, str]:
        body = {"model": self.model, "messages": messages, "temperature": 0,
                "max_tokens": max_tokens, "response_format": {"type": "json_object"},
                "chat_template_kwargs": {"enable_thinking": False}}
        url = f"{self.endpoint.rstrip('/')}/chat/completions"
        record.model_called = True
        try:
            client = self.client if self.client is not None else _client()
            response = client.post(url, json=body, timeout=self.timeout)
        except Exception as exc:                        # noqa: BLE001
            record.failure, record.error = UNAVAILABLE, f"unreachable: {type(exc).__name__}"
            return None, UNAVAILABLE
        status = getattr(response, "status_code", 200)
        if status != 200:
            record.failure, record.error = UNAVAILABLE, f"http_{status}"
            return None, UNAVAILABLE
        try:
            envelope = response.json()
            choice = envelope["choices"][0]
            content = choice["message"]["content"]
            usage = envelope.get("usage") or {}
        except Exception as exc:                        # noqa: BLE001
            record.failure, record.error = UNAVAILABLE, f"malformed_envelope: {type(exc).__name__}"
            return None, UNAVAILABLE
        record.prompt_tokens += int(usage.get("prompt_tokens") or 0)
        record.completion_tokens += int(usage.get("completion_tokens") or 0)
        if not content:
            return None, f"empty output (finish={choice.get('finish_reason')})"
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return None, f"not JSON (finish={choice.get('finish_reason')})"
        if not isinstance(payload, dict):
            return None, "not a JSON object"
        return payload, ""


def _ms(mark: float) -> int:
    return int((time.perf_counter() - mark) * 1000)


_HTTP = None


def _client():
    """One pooled HTTP client for the process: no socket per stage."""
    global _HTTP
    if _HTTP is None:
        import httpx
        _HTTP = httpx.Client(limits=httpx.Limits(max_keepalive_connections=16,
                                                 max_connections=32))
    return _HTTP


def _safe(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        if parts.scheme and parts.hostname:
            return f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    except ValueError:
        pass
    return str(url).split("?", 1)[0]
