"""Which model each pipeline stage uses, resolved in one place.

Stages name a ROLE -- `main`, `embedding`, `optional_small` -- never an
endpoint. Every semantic stage that says `main` therefore reaches the same
deployment, and changing the main model is one setting rather than four that
can silently disagree.

Resolution order for a role, most specific first:
  1. the deployment's own environment (OPENAI_BASE_URL / MAIN_MODEL for
     `main`, the same variables the orchestrator reads)
  2. `models.<role>` in schema_config.yaml
  3. the defaults below, which are what the running stack serves (verified)

A stage may still be overridden explicitly -- SFK_EXTRACT_URL/MODEL for stage 1
predate this file and keep working -- but `validate()` reports any semantic
stage that no longer resolves to the main deployment, so a divergence is a
visible warning instead of a surprise in a trace.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, tuple[str, str]] = {
    "main": ("http://vllm:8000/v1", "Qwen/Qwen3.6-35B-A3B-NVFP4"),
    "embedding": ("http://vllm-embed:30003/v1", "Qwen/Qwen3-Embedding-0.6B"),
    "optional_small": ("http://vllm-router:30002/v1", "Qwen/Qwen3-VL-8B-Instruct-FP8"),
}

ENVIRONMENT: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "main": (("SFK_MAIN_URL", "OPENAI_BASE_URL"), ("SFK_MAIN_MODEL", "MAIN_MODEL")),
    "embedding": (("SFK_EMBED_URL", "EMBED_BASE_URL"), ("SFK_EMBED_MODEL", "EMBED_MODEL")),
    "optional_small": (("SFK_SMALL_URL", "ROUTER_BASE_URL"),
                       ("SFK_SMALL_MODEL", "ROUTER_MODEL")),
}

# Stage -> role, when the YAML does not say. Deterministic stages have none.
STAGE_DEFAULTS: dict[str, str | None] = {
    "intent": "main",
    "routing": "main",
    "discovery": "main",
    "discovery_rerank": "main",       # step-7 stage path only
    "linking": "main",
    "planning": "main",
    "record_query": None,
    "interpretation": "main",
    "answer": "main",
}

# Stages whose job is semantic judgement, and therefore must be on `main`
# unless someone deliberately says otherwise.
SEMANTIC_STAGES = ("intent", "routing", "discovery", "discovery_rerank", "linking",
                   "planning", "interpretation", "answer")

# The semantic-IR path's model stages, in pipeline order. The main model makes
# the semantic decision in every one of them that applies to a question; no
# retrieval confidence, cache or fast path stands in for it (spec §5, §6).
REQUIRED_MODEL_STAGES = ("intent", "routing", "discovery", "linking", "planning",
                         "interpretation", "answer")


def stage_required(stage: str) -> bool:
    """MAIN_MODEL_REQUIRED_FOR_<STAGE>, default true for every model stage.

    Setting one to false does not make the stage deterministic: it only stops
    the participation check from counting a stage that a deployment has
    deliberately not wired yet. The default for this project enforces all.
    """
    value = (os.environ.get(f"MAIN_MODEL_REQUIRED_FOR_{stage.upper()}") or "").strip().lower()
    if value in ("false", "0", "no", "off"):
        return False
    return stage in REQUIRED_MODEL_STAGES


@dataclass(frozen=True)
class ModelRole:
    role: str
    endpoint: str
    model: str
    source: str            # env | config | default | override

    def as_dict(self) -> dict[str, str]:
        return {"role": self.role, "endpoint": _safe(self.endpoint),
                "model": self.model, "source": self.source}


def _safe(url: str) -> str:
    """Host and port only -- never userinfo or a query string into a trace."""
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        if parts.scheme and parts.hostname:
            port = f":{parts.port}" if parts.port else ""
            return f"{parts.scheme}://{parts.hostname}{port}"
    except Exception:                                   # noqa: BLE001
        pass
    return str(url).split("?", 1)[0]


def _first_env(names: tuple[str, ...]) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _yaml(root: str | Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError:
        return {}
    path = Path(root) / "config" / "schema_config.yaml"
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def resolve_role(role: str, root: str | Path = "salesforce_knowledge") -> ModelRole:
    default_endpoint, default_model = DEFAULTS.get(role, DEFAULTS["main"])
    block = (_yaml(root).get("models") or {}).get(role) or {}
    url_vars, model_vars = ENVIRONMENT.get(role, ((), ()))
    endpoint, model = _first_env(url_vars), _first_env(model_vars)
    source = "env" if endpoint or model else ""
    if not endpoint and block.get("endpoint"):
        endpoint, source = str(block["endpoint"]), source or "config"
    if not model and block.get("name"):
        model, source = str(block["name"]), source or "config"
    return ModelRole(role=role, endpoint=endpoint or default_endpoint,
                     model=model or default_model, source=source or "default")


def stage_role(stage: str, root: str | Path = "salesforce_knowledge") -> str | None:
    stages = (_yaml(root).get("pipeline") or {})
    block = stages.get(stage) or {}
    if isinstance(block, dict) and "model_role" in block:
        return block["model_role"]
    return STAGE_DEFAULTS.get(stage)


def stage_model(stage: str, root: str | Path = "salesforce_knowledge"
                ) -> ModelRole | None:
    """The model a stage calls, honouring the legacy stage-1 overrides."""
    role = stage_role(stage, root)
    if role is None:
        return None
    resolved = resolve_role(role, root)
    if stage == "intent":
        url, model = _first_env(("SFK_EXTRACT_URL",)), _first_env(("SFK_EXTRACT_MODEL",))
        if url or model:
            return ModelRole(role=role, endpoint=url or resolved.endpoint,
                             model=model or resolved.model, source="override")
    return resolved


def validate(root: str | Path = "salesforce_knowledge") -> dict[str, Any]:
    """Every stage's model, and whether the semantic ones agree on `main`."""
    main = resolve_role("main", root)
    stages: dict[str, Any] = {}
    divergent: list[str] = []
    for stage in STAGE_DEFAULTS:
        resolved = stage_model(stage, root)
        stages[stage] = resolved.as_dict() if resolved else {"role": None,
                                                              "mode": "deterministic"}
        if stage in SEMANTIC_STAGES and resolved is not None and (
                resolved.endpoint.rstrip("/") != main.endpoint.rstrip("/")
                or resolved.model != main.model):
            divergent.append(stage)
    return {"main": main.as_dict(), "stages": stages,
            "divergent_semantic_stages": divergent,
            "ok": not divergent}


def probe(role: ModelRole, timeout: float = 5.0) -> tuple[bool, str]:
    """Is the endpoint serving the model it is configured for?"""
    try:
        import httpx
        response = httpx.get(f"{role.endpoint.rstrip('/')}/models", timeout=timeout)
        served = [m.get("id") for m in (response.json().get("data") or [])]
        roots = [m.get("root") for m in (response.json().get("data") or [])]
    except Exception as exc:                            # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if role.model in served or role.model in roots:
        return True, "serving"
    return False, f"serves {served}, not {role.model!r}"
