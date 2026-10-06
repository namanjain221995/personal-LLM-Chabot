"""The main model, over the endpoint the orchestrator already configures.

One client for the main role. `graphrag.extract` is the other model caller in
this pipeline and is deliberately not merged with this one: it fills the router
role, runs the small model by default, lives in a different source tree and is
mounted into a different container. Merging them would mean a shared import
across that boundary in exchange for a dozen lines.

Endpoint and model name are read from OPENAI_BASE_URL and MAIN_MODEL, the same
variables `orchestrator/app/config.py` reads. Nothing here declares a second
endpoint.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .config import AnswerConfig

log = logging.getLogger(__name__)


class ModelError(RuntimeError):
    pass


class MainModelClient:
    def __init__(self, config: AnswerConfig) -> None:
        self.config = config
        self.endpoint = config.resolved_endpoint()
        self.model = config.resolved_model()

    def complete_json(self, messages: list[dict[str, str]],
                      ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """One call. Returns the parsed JSON object and call metadata.

        json_object rather than a grammar. Measured on this model: constrained
        decoding collapsed intent accuracy from 11/12 to 1/12 on the same
        cases, and the same collapse in an answer would read as confident,
        well-formed and wrong.
        """
        try:
            import httpx
        except ImportError as exc:
            raise ModelError("answer generation needs httpx") from exc

        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {
                "enable_thinking": self.config.enable_thinking},
        }
        url = f"{self.endpoint.rstrip('/')}/chat/completions"
        started = time.perf_counter()
        meta: dict[str, Any] = {"model": self.model, "endpoint": self.endpoint,
                                "duration_ms": 0, "completion_tokens": 0,
                                "finish_reason": None}
        try:
            with httpx.Client() as client:
                response = client.post(url, json=body,
                                       timeout=self.config.timeout_seconds)
        except Exception as exc:                        # noqa: BLE001
            meta["duration_ms"] = int((time.perf_counter() - started) * 1000)
            raise ModelError(f"{type(exc).__name__}: {exc}") from exc

        meta["duration_ms"] = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            raise ModelError(
                f"HTTP {response.status_code}: {response.text[:200]}")
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"].get("content")
            meta["finish_reason"] = choice.get("finish_reason")
            meta["completion_tokens"] = int(
                (payload.get("usage") or {}).get("completion_tokens") or 0)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelError(f"malformed response envelope: {exc}") from exc

        if not content:
            # A reasoning model with thinking left on spends the whole budget
            # before writing anything and returns None here.
            raise ModelError(
                f"the model returned no content (finish_reason="
                f"{meta['finish_reason']})")
        try:
            return json.loads(content), meta
        except json.JSONDecodeError:
            return None, meta
