"""Answer-generation settings, read from the same YAML as the rest.

Endpoint and model name come from the environment the orchestrator already
sets -- OPENAI_BASE_URL and MAIN_MODEL -- rather than being declared again
here. Two places to configure one endpoint is how a deployment ends up talking
to a model nobody meant it to.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path


@dataclass
class FreshnessPolicy:
    """When the answer mentions how old the data is.

    The model is always told the real age. This decides only whether it is
    asked to say it, which is a presentation choice -- so a deployment can
    stay quiet about a 3-minute-old replica without the model ever being led
    to believe the data is live.
    """

    show_when_fresh: bool = False
    show_when_warning: bool = True
    show_when_critical: bool = True

    def should_show(self, status: str) -> bool:
        return {"fresh": self.show_when_fresh,
                "stale": self.show_when_warning,
                "very_stale": self.show_when_critical,
                "unknown": self.show_when_warning,
                # Schema and sync-state answers do not come from the records,
                # so the records' age says nothing about them.
                "not_applicable": False}.get(status, True)


@dataclass
class AnswerConfig:
    model_role: str = "main"
    endpoint: str = ""
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 3000
    timeout_seconds: float = 120.0
    # A reasoning model spends its whole budget thinking and returns content
    # None unless this is off. Measured on the 35B: 7.9 s and no answer with
    # thinking on, 610 ms with it off.
    enable_thinking: bool = False
    max_rows_to_model: int = 50
    max_context_characters: int = 30_000
    max_regenerations: int = 1
    freshness: FreshnessPolicy = dataclass_field(default_factory=FreshnessPolicy)

    def _role(self):
        try:
            from model_roles import resolve_role
        except ImportError:
            return None
        return resolve_role(self.model_role or "main")

    def resolved_endpoint(self) -> str:
        """Explicit setting, else the role -- the same main every stage uses."""
        if self.endpoint:
            return self.endpoint
        role = self._role()
        return role.endpoint if role else (
            os.environ.get("OPENAI_BASE_URL") or "http://vllm:8000/v1")

    def resolved_model(self) -> str:
        if self.model:
            return self.model
        role = self._role()
        return role.model if role else (
            os.environ.get("MAIN_MODEL") or "Qwen/Qwen3.6-35B-A3B-NVFP4")


def load_answer_config(root: str | Path = "salesforce_knowledge",
                       ) -> AnswerConfig:
    try:
        import yaml
    except ImportError:
        return AnswerConfig()
    path = Path(root) / "config" / "schema_config.yaml"
    if not path.is_file():
        return AnswerConfig()
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}
           ).get("answer_generation") or {}
    fresh = raw.get("freshness") or {}
    return AnswerConfig(
        model_role=str(raw.get("model_role", "main")),
        endpoint=str(raw.get("endpoint", "") or ""),
        model=str(raw.get("model", "") or ""),
        temperature=float(raw.get("temperature", 0.0)),
        max_tokens=int(raw.get("max_tokens", 3000)),
        timeout_seconds=float(raw.get("timeout_seconds", 120.0)),
        enable_thinking=bool(raw.get("enable_thinking", False)),
        max_rows_to_model=int(raw.get("max_rows_to_model", 50)),
        max_context_characters=int(raw.get("max_context_characters", 30_000)),
        max_regenerations=int(raw.get("max_regenerations", 1)),
        freshness=FreshnessPolicy(
            show_when_fresh=bool(fresh.get("show_when_fresh", False)),
            show_when_warning=bool(fresh.get("show_when_warning", True)),
            show_when_critical=bool(fresh.get("show_when_critical", True))))
