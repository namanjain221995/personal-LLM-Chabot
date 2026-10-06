"""Model-backed selection among retrieved candidates.

The interface takes candidates and returns a choice. It never asks a model
what something is called -- the names come from the runtime schema and are
verified afterwards -- only which of several verified names the user meant.

Two tiers, primary then fallback on low confidence. Which model fills each is
configuration: measured on this hardware the 35B is both faster and more
accurate than the deployed 8B, so both default to it, and the escalation path
works unchanged the day a better small model exists.
"""
from __future__ import annotations

import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Sequence

from .config import ModelEndpoint, SchemaLinkingConfig
from .models import Selection
from .prompts import (FIELD_SYSTEM, OBJECT_SYSTEM, RELATIONSHIP_SYSTEM,
                      field_prompt, object_prompt, relationship_prompt)

log = logging.getLogger(__name__)


class SchemaLinkingModel(ABC):
    """What schema linking needs from a model. No provider leaks past this."""

    name: str = "unknown"

    @abstractmethod
    def select_object(self, business_entity: str, role: str,
                      candidates: Sequence[Any], **context: Any) -> Selection: ...

    @abstractmethod
    def select_field(self, concept: str, value: Any, operator: str | None,
                     object_api_name: str,
                     candidates: Sequence[Any]) -> Selection: ...

    @abstractmethod
    def select_relationship(self, business_entity: str, object_api_name: str,
                            candidates: Sequence[Any]) -> Selection: ...


def _clamp(value: Any) -> float:
    """Confidence outside 0-1 is a malformed answer, not a strong opinion."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


class LLMSchemaLinkingModel(SchemaLinkingModel):
    """An OpenAI-compatible endpoint, used with thinking disabled.

    Thinking off is not a preference: the main model is a reasoning model and
    with it on spends the whole token budget thinking and returns no content at
    all. Measured, a selection takes ~600 ms off and ~8 s on, with no answer.
    """

    def __init__(self, endpoint: ModelEndpoint, *, timeout: float = 60.0,
                 max_tokens: int = 300) -> None:
        self.endpoint = endpoint
        self.name = endpoint.name
        self.timeout = timeout
        self.max_tokens = max_tokens

    def _ask(self, system: str, user: str) -> tuple[dict[str, Any] | None, int]:
        try:
            import httpx
        except ImportError:
            log.error("schema-linking model needs httpx")
            return None, 0
        started = time.perf_counter()
        body = {
            "model": self.endpoint.name,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "chat_template_kwargs": {"enable_thinking": False},
        }
        try:
            with httpx.Client() as client:
                response = client.post(
                    f"{self.endpoint.endpoint.rstrip('/')}/chat/completions",
                    json=body, timeout=self.timeout)
        except Exception as exc:
            log.warning("schema-linking model unreachable: %s", exc)
            return None, int((time.perf_counter() - started) * 1000)
        duration = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            log.warning("schema-linking model HTTP %s", response.status_code)
            return None, duration
        try:
            content = response.json()["choices"][0]["message"]["content"]
            return json.loads(content), duration
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            log.warning("schema-linking model returned unusable content: %s", exc)
            return None, duration

    def select_object(self, business_entity: str, role: str,
                      candidates: Sequence[Any], **context: Any) -> Selection:
        payload, duration = self._ask(
            OBJECT_SYSTEM,
            object_prompt(business_entity, role, candidates,
                          context.get("filters"), context.get("requested_attributes", ())))
        if payload is None:
            return Selection(selected=None, confidence=0.0, model=self.name,
                             duration_ms=duration, reason_codes=["model_unavailable"])
        return Selection(
            selected=payload.get("selected_object"),
            confidence=_clamp(payload.get("confidence")),
            reason_codes=[str(r) for r in (payload.get("reason_codes") or [])][:6],
            model=self.name, duration_ms=duration)

    def select_field(self, concept: str, value: Any, operator: str | None,
                     object_api_name: str, candidates: Sequence[Any]) -> Selection:
        payload, duration = self._ask(
            FIELD_SYSTEM,
            field_prompt(concept, value, operator, object_api_name, candidates))
        if payload is None:
            return Selection(selected=None, confidence=0.0, model=self.name,
                             duration_ms=duration, reason_codes=["model_unavailable"])
        return Selection(
            selected=payload.get("selected_field"),
            confidence=_clamp(payload.get("confidence")),
            reason_codes=[str(r) for r in (payload.get("reason_codes") or [])][:6],
            resolved_value=payload.get("resolved_value"),
            model=self.name, duration_ms=duration)

    def select_relationship(self, business_entity: str, object_api_name: str,
                            candidates: Sequence[Any]) -> Selection:
        payload, duration = self._ask(
            RELATIONSHIP_SYSTEM,
            relationship_prompt(business_entity, object_api_name, candidates))
        if payload is None:
            return Selection(selected=None, confidence=0.0, model=self.name,
                             duration_ms=duration, reason_codes=["model_unavailable"])
        return Selection(
            selected=payload.get("selected_field"),
            confidence=_clamp(payload.get("confidence")),
            reason_codes=[str(r) for r in (payload.get("reason_codes") or [])][:6],
            model=self.name, duration_ms=duration)


class RetrievalOnlyModel(SchemaLinkingModel):
    """Takes the top retrieval candidate. No network.

    Not a stub: it is the baseline the model tiers must beat. Running the whole
    pipeline with this measures how much the model actually contributes, and it
    keeps every test that is not about model behaviour offline and fast.
    """

    name = "retrieval_only"

    @staticmethod
    def _top(candidates: Sequence[Any], attribute: str) -> Selection:
        if not candidates:
            return Selection(selected=None, confidence=0.0,
                             model=RetrievalOnlyModel.name)
        best = candidates[0]
        runner_up = candidates[1].retrieval_score if len(candidates) > 1 else 0.0
        # Confidence from the margin: a clear winner is trusted, a near-tie is
        # not, which is what makes escalation trigger where it should.
        margin = best.retrieval_score - runner_up
        confidence = min(0.99, 0.5 + margin / 2) if best.retrieval_score else 0.0
        return Selection(selected=getattr(best, attribute),
                         confidence=round(confidence, 4),
                         reason_codes=list(best.evidence)[:6],
                         model=RetrievalOnlyModel.name)

    def select_object(self, business_entity: str, role: str,
                      candidates: Sequence[Any], **context: Any) -> Selection:
        return self._top(candidates, "api_name")

    def select_field(self, concept: str, value: Any, operator: str | None,
                     object_api_name: str, candidates: Sequence[Any]) -> Selection:
        selection = self._top(candidates, "api_name")
        if candidates and getattr(candidates[0], "matched_picklist_value", None):
            selection.resolved_value = candidates[0].matched_picklist_value
        return selection

    def select_relationship(self, business_entity: str, object_api_name: str,
                            candidates: Sequence[Any]) -> Selection:
        return self._top(candidates, "source_field")


def build_models(config: SchemaLinkingConfig, *, offline: bool = False
                 ) -> tuple[SchemaLinkingModel, SchemaLinkingModel]:
    if offline:
        model = RetrievalOnlyModel()
        return model, model
    return (LLMSchemaLinkingModel(config.models.primary,
                                  timeout=config.models.timeout_seconds,
                                  max_tokens=config.models.max_tokens),
            LLMSchemaLinkingModel(config.models.fallback,
                                  timeout=config.models.timeout_seconds,
                                  max_tokens=config.models.max_tokens))
