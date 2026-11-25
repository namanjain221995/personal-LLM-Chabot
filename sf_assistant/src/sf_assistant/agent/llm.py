"""OpenAI-compatible chat client with tool calling, JSON mode and retries.

:class:`LLM` wraps ``openai.OpenAI().chat.completions.create`` (or any injected object with the same
shape, such as :class:`okeval.mock_llm.MockClient`) and normalises every response to a plain dict::

    {"content": str | None,
     "tool_calls": [{"id": str, "name": str, "arguments": dict}],
     "usage": {"prompt_tokens": int, "completion_tokens": int},
     "latency_s": float,
     "raw": <SDK response object>}

Retries: up to 3 attempts with exponential backoff on connection errors, timeouts, HTTP 429 and
5xx. Qwen ``<think>...</think>`` blocks are stripped from returned content.

Every call adds one model entry (seconds, tokens, finish reason, attempts; no text) to the timeline of the answer
being made, when one is open (:mod:`sf_assistant.agent.timeline`).

A forced tool call (prerequisite P2 of 2026-11-25): ``chat(..., tool_choice="required")`` sends that choice with the
tools (without ``tool_choice`` the request is byte for byte as before: ``"auto"`` with tools). A forced attempt the
server refuses (HTTP 400 or 422 that is not about ``response_format`` or ``temperature``, :func:`_refuses_choice`) is
sent again at once with ``"auto"``; when that one succeeds, forcing is off for :data:`FORCED_OFF_S` seconds
(``tool_choice`` is then sent as ``"auto"``), so a server that does not take it costs one failed attempt, not one per
question. ``forced_fallbacks`` counts those fallbacks. A 5xx, a 429, a timeout or a connection error of a forced call
takes the normal backoff retries with the forced choice kept, and leaves forcing on (review of 2026-11-25: the shared
server is often busy, and one busy reply must not turn the forced round into a plain nudge for ten minutes).
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from sf_assistant.agent import timeline
from sf_assistant.config import Config

__all__ = ["LLM", "parse_json_object", "parse_tool_arguments", "FORCED_OFF_S"]

#: Seconds forcing a tool call stays off after the server refused a forced call and its "auto" retry worked.
FORCED_OFF_S = 600.0

_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)

# Exception class names (openai SDK and httpx) that are worth retrying. Matched by name so this
# module imports cleanly even before ``openai`` is installed.
_RETRYABLE_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "InternalServerError",
    "RateLimitError",
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "ReadError",
    "RemoteProtocolError",
    "TimeoutException",
    "TimeoutError",
    "ConnectionError",
}


def _is_retryable(exc: BaseException) -> bool:
    """True for transient failures: connection problems, timeouts, HTTP 429 and HTTP 5xx."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status == 429 or status >= 500
    return any(cls.__name__ in _RETRYABLE_NAMES for cls in type(exc).__mro__)


def _refuses_choice(exc: BaseException, kwargs: dict[str, Any]) -> bool:
    """The server refused the request's forced ``tool_choice``: HTTP 400 or 422, unless the error is about
    ``response_format`` or ``temperature`` (their own fallbacks below handle those)."""
    status = getattr(exc, "status_code", None)
    if status not in (400, 422):
        return False
    text = str(exc)
    return not any(key in kwargs and key in text for key in ("response_format", "temperature"))


def _strip_thinking(text: str | None) -> str | None:
    """Remove ``<think>`` blocks (and a dangling unterminated one) from model output."""
    if text is None:
        return None
    text = _THINK_RE.sub("", text)
    if "<think>" in text.lower() and "</think>" not in text.lower():
        text = text[: text.lower().index("<think>")]
    return text.strip()


def parse_tool_arguments(raw: Any) -> dict:
    """Decode a tool call's ``arguments`` string.

    Empty / missing arguments become ``{}``. Anything that is not a JSON object becomes
    ``{"_raw": <original string>}`` so the caller can see what the model produced.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    text = str(raw).strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": str(raw)}
    return value if isinstance(value, dict) else {"_raw": str(raw)}


def parse_json_object(text: str | None) -> dict:
    """Parse a JSON object from model text, tolerating code fences and surrounding prose.

    Raises:
        ValueError: when no JSON object can be decoded.
    """
    if not text or not text.strip():
        raise ValueError("empty response")
    candidate = _FENCE_RE.sub("", text.strip()).strip()
    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    # Fall back to the first decodable {...} object embedded in the text.
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", candidate):
        try:
            value, _ = decoder.raw_decode(candidate[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"no JSON object found in response: {text[:200]!r}")


class LLM:
    """Thin, retrying, normalising wrapper around an OpenAI-compatible chat completions client.

    Args:
        cfg: run configuration (endpoint, model, sampling, timeouts).
        client: optional pre-built client exposing ``.chat.completions.create(**kwargs)``.
            When omitted an ``openai.OpenAI`` client is created lazily from ``cfg``.
    """

    #: Total attempts per request (first try + retries).
    max_attempts: int = 3
    #: Seconds before the first retry; doubles each time. Set to 0 in tests.
    backoff_base_s: float = 1.0

    def __init__(self, cfg: Config, client: Any = None) -> None:
        self.cfg = cfg
        self._client = client
        self._lock = threading.Lock()
        #: Running token totals across every call made through this instance (thread-safe).
        self.total_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
        # Some servers reject response_format; remember that after the first 400 and stop sending.
        self._json_mode_supported = True
        # Reasoning models reject a non-default temperature; same treatment.
        self._temperature_supported = True
        # A forced tool call that failed and worked with "auto": no forcing until then (time.monotonic()).
        self._forced_off_until = 0.0
        #: How many forced calls fell back to "auto" (prerequisite P2; a log counter).
        self.forced_fallbacks = 0

    # ------------------------------------------------------------------ client
    @property
    def client(self) -> Any:
        """The underlying SDK client (created on first use)."""
        if self._client is None:
            from openai import OpenAI  # lazy: lets the package import without openai installed

            # SDK-level retries disabled: this class owns the retry policy.
            self._client = OpenAI(
                base_url=self.cfg.base_url,
                api_key=self.cfg.api_key or "EMPTY",
                timeout=self.cfg.timeout_s,
                max_retries=0,
            )
        return self._client

    @property
    def _is_openai_host(self) -> bool:
        return "api.openai.com" in (self.cfg.base_url or "")

    #: OpenAI model-name prefixes of reasoning models, which only accept the default temperature.
    REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5")

    def _sends_temperature(self, model: str) -> bool:
        """False for OpenAI reasoning models and after a server rejected ``temperature`` once."""
        if not self._temperature_supported:
            return False
        name = (model or "").lower().rsplit("/", 1)[-1]
        return not (self._is_openai_host and name.startswith(self.REASONING_MODEL_PREFIXES))

    # -------------------------------------------------------------------- chat
    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        json_mode: bool = False,
        model: str | None = None,
        max_tokens: int | None = None,
        tool_choice: str | None = None,
    ) -> dict:
        """Send one chat completion request and return the normalised result dict.

        Args:
            messages: OpenAI-format messages.
            tools: OpenAI ``tools=[{"type": "function", ...}]`` list; ``None`` disables tools.
            json_mode: request ``response_format={"type": "json_object"}``.
            model: override ``cfg.model`` (e.g. the judge model).
            max_tokens: override ``cfg.max_tokens``.
            tool_choice: with tools, the tool choice to send (``"required"`` forces a tool call); None or ``"auto"``
                sends ``"auto"`` as before. Ignored without tools, and sent as ``"auto"`` while forcing is off after
                the server refused a forced call (:meth:`forcing_off`).

        Raises:
            The last SDK exception when all attempts fail, or immediately for non-retryable
            errors (bad request, auth, not found).
        """
        kwargs: dict[str, Any] = {
            "model": model or self.cfg.model,
            "messages": messages,
        }
        if self._sends_temperature(kwargs["model"]):
            kwargs["temperature"] = self.cfg.temperature
        limit = max_tokens or self.cfg.max_tokens
        if self._is_openai_host:
            kwargs["max_completion_tokens"] = limit  # newer OpenAI models reject max_tokens
        else:
            kwargs["max_tokens"] = limit
            kwargs["seed"] = self.cfg.seed
            kwargs["extra_body"] = {
                "chat_template_kwargs": {"enable_thinking": bool(self.cfg.enable_thinking)}
            }
        forced = bool(tools) and tool_choice not in (None, "auto")
        choice = None
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
            if forced:
                choice = "auto_off" if self.forcing_off() else "required"
                if choice == "required":
                    kwargs["tool_choice"] = tool_choice
        if json_mode and self._json_mode_supported:
            kwargs["response_format"] = {"type": "json_object"}

        start = time.perf_counter()
        failed: list[dict] = []
        state: dict[str, Any] = {}
        try:
            response = self._create_with_retries(kwargs, failed, state)
        except Exception as exc:
            _note_call(start, messages, tools, limit, None, failed, exc, choice=choice)
            raise
        latency = time.perf_counter() - start
        _note_call(start, messages, tools, limit, response, failed,
                   choice="auto_fallback" if state.get("fell_back") else choice)
        out = self._normalise(response, latency)
        if choice is not None:
            out["tool_choice"] = "auto_fallback" if state.get("fell_back") else choice
        return out

    def forcing_off(self) -> bool:
        """The server refused a forced tool call, which then worked with "auto", less than :data:`FORCED_OFF_S`
        seconds ago."""
        return time.monotonic() < self._forced_off_until

    def _create_with_retries(self, kwargs: dict[str, Any], failed: list[dict] | None = None,
                             state: dict | None = None) -> Any:
        """Call the SDK with exponential backoff on transient failures. ``failed`` (optional) gets one entry per failed
        attempt for the timeline: the exception class, the HTTP status (or None) and the seconds into the call.

        A forced tool choice (anything but ``"auto"``) the server refuses (:func:`_refuses_choice`) is sent again at once
        with ``"auto"`` (``state["fell_back"]`` True); when that attempt works, forcing is off for :data:`FORCED_OFF_S`
        seconds. Any other failure of a forced call is retried as any call is, with the forced choice kept."""
        last_exc: BaseException | None = None
        t0 = time.perf_counter()
        fell_back = False
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.client.chat.completions.create(**kwargs)
                if fell_back:
                    with self._lock:
                        self._forced_off_until = time.monotonic() + FORCED_OFF_S
                        self.forced_fallbacks += 1
                return response
            except Exception as exc:  # noqa: BLE001 - classified below
                status = getattr(exc, "status_code", None)
                if failed is not None:
                    failed.append({"error": type(exc).__name__, "status": status if isinstance(status, int) else None,
                                   "after_s": round(time.perf_counter() - t0, 3)})
                # A forced tool call the server refused (400 / 422): once more at once with "auto".
                if kwargs.get("tool_choice") not in (None, "auto") and not fell_back and _refuses_choice(exc, kwargs):
                    fell_back = True
                    if state is not None:
                        state["fell_back"] = True
                    kwargs = {**kwargs, "tool_choice": "auto"}
                    continue
                # Server does not support JSON mode: drop it once and retry immediately.
                if status == 400 and "response_format" in kwargs and "response_format" in str(exc):
                    self._json_mode_supported = False
                    kwargs = {k: v for k, v in kwargs.items() if k != "response_format"}
                    continue
                # Model only accepts the default temperature (reasoning models): drop it and retry.
                if status == 400 and "temperature" in kwargs and "temperature" in str(exc):
                    self._temperature_supported = False
                    kwargs = {k: v for k, v in kwargs.items() if k != "temperature"}
                    continue
                if not _is_retryable(exc) or attempt == self.max_attempts:
                    raise
                last_exc = exc
                time.sleep(self.backoff_base_s * (2 ** (attempt - 1)))
        assert last_exc is not None  # pragma: no cover - loop always returns or raises
        raise last_exc

    def _normalise(self, response: Any, latency: float) -> dict:
        """Convert an SDK response into the harness's plain-dict shape and update totals."""
        choice = response.choices[0]
        message = choice.message
        tool_calls = []
        for tc in getattr(message, "tool_calls", None) or []:
            fn = tc.function
            tool_calls.append(
                {"id": tc.id, "name": fn.name, "arguments": parse_tool_arguments(fn.arguments)}
            )
        usage_obj = getattr(response, "usage", None)
        usage = {
            "prompt_tokens": int(getattr(usage_obj, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage_obj, "completion_tokens", 0) or 0),
        }
        with self._lock:
            self.total_usage["prompt_tokens"] += usage["prompt_tokens"]
            self.total_usage["completion_tokens"] += usage["completion_tokens"]
            self.total_usage["calls"] += 1
        return {
            "content": _strip_thinking(getattr(message, "content", None)),
            "tool_calls": tool_calls,
            "usage": usage,
            "latency_s": round(latency, 4),
            "raw": response,
        }

    # --------------------------------------------------------------- JSON mode
    def chat_json(
        self,
        messages: list[dict],
        *,
        model: str | None = None,
        schema_hint: str = "",
    ) -> dict:
        """Request a JSON object and parse it, with one repair retry.

        Args:
            messages: OpenAI-format messages (should mention JSON, as OpenAI requires).
            model: override the model (e.g. ``cfg.effective_judge_model``).
            schema_hint: short description of the expected shape, quoted in the repair prompt.

        Raises:
            ValueError: if the response is still not a JSON object after the repair retry.
        """
        first = self.chat(messages, json_mode=True, model=model)
        try:
            return parse_json_object(first["content"])
        except ValueError as exc:
            first_error = exc
        repair = "That was not valid JSON. Reply again with ONLY one valid JSON object"
        repair += f" matching this shape: {schema_hint}" if schema_hint else "."
        repair += " No prose, no markdown fences, no comments."
        retry_messages = list(messages) + [
            {"role": "assistant", "content": first["content"] or ""},
            {"role": "user", "content": repair},
        ]
        second = self.chat(retry_messages, json_mode=True, model=model)
        try:
            return parse_json_object(second["content"])
        except ValueError as exc:
            raise ValueError(
                f"model did not return valid JSON after one repair retry: {exc} "
                f"(first attempt: {first_error})"
            ) from exc

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def assistant_message(result: dict) -> dict:
        """Build the assistant message to append to history from a :meth:`chat` result.

        Tool-call arguments are re-serialised to JSON strings, as the OpenAI API expects.
        """
        msg: dict[str, Any] = {"role": "assistant", "content": result.get("content") or ""}
        if result.get("tool_calls"):
            msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False),
                    },
                }
                for tc in result["tool_calls"]
            ]
        return msg


def _note_call(start: float, messages: list, tools: list | None, limit: int | None, response: Any,
               failed: list[dict], exc: BaseException | None = None, choice: str | None = None) -> None:
    """One model entry on the answer's timeline (:mod:`sf_assistant.agent.timeline`; nothing when none is open):
    ``round`` (1 + the assistant turns sent), ``after`` (what the call answers: ``question``, ``tools`` or ``nudge``),
    ``offered`` (tools offered), ``max`` (max tokens), ``tok_in``, ``tok_out``, ``finish``, ``tries``, ``failed`` (the
    failed attempts), ``error`` (the exception class when the call raised) and, for a forced tool call only,
    ``choice`` (``required``, ``auto_fallback`` when it went out again as "auto", ``auto_off`` while forcing is off).
    No message text and no error text: an error body can echo the request. Never raises."""
    try:
        roles = [m.get("role") if isinstance(m, dict) else getattr(m, "role", None) for m in messages or []]
        last = roles[-1] if roles else None
        entry: dict[str, Any] = {"round": 1 + roles.count("assistant"),
                                 "after": "tools" if last == "tool" else "question" if roles.count("user") <= 1
                                 else "nudge",
                                 "offered": len(tools or []), "max": limit}
        if response is not None:
            usage = getattr(response, "usage", None)
            entry["tok_in"] = int(getattr(usage, "prompt_tokens", 0) or 0)
            entry["tok_out"] = int(getattr(usage, "completion_tokens", 0) or 0)
            choices = getattr(response, "choices", None) or []
            entry["finish"] = str(getattr(choices[0], "finish_reason", "") or "") if choices else ""
        entry["tries"] = len(failed) + (0 if exc is not None else 1)
        if failed:
            entry["failed"] = [dict(f) for f in failed]
        if exc is not None:
            entry["error"] = type(exc).__name__
        if choice is not None:
            entry["choice"] = choice
        timeline.add("model", start, **entry)
    except Exception:  # noqa: BLE001 - logging never fails a call
        pass
