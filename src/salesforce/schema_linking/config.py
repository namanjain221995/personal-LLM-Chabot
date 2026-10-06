"""Schema-linking configuration. Every weight and threshold comes from YAML."""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any


@dataclass
class CandidateLimits:
    objects: int = 10
    fields: int = 10
    relationships: int = 5


@dataclass
class Weights:
    """Retrieval weights, gapped so certainty outranks similarity.

    An exact API-name match is a fact; an FTS hit is a guess. Keeping the bands
    apart stops a pile of weak lexical hits outscoring one exact match.
    """
    exact_api: float = 1.0
    exact_label: float = 0.95
    manual_alias: float = 0.95
    auto_alias: float = 0.80
    fts: float = 0.65
    relationship: float = 0.55
    picklist_value: float = 0.90
    data_type: float = 0.60
    name_token: float = 0.45
    description: float = 0.40


@dataclass
class Confidence:
    accept_small_model: float = 0.85
    fallback_to_main_model: float = 0.60


@dataclass
class Traversal:
    max_depth: int = 2
    max_related_objects_per_step: int = 10


@dataclass
class ModelEndpoint:
    endpoint: str = "http://vllm:8000/v1"
    name: str = "Qwen/Qwen3.6-35B-A3B-NVFP4"


@dataclass
class LinkingModels:
    primary: ModelEndpoint = dataclass_field(default_factory=ModelEndpoint)
    fallback: ModelEndpoint = dataclass_field(default_factory=ModelEndpoint)
    timeout_seconds: float = 60.0
    max_tokens: int = 300


@dataclass
class SchemaLinkingConfig:
    candidate_limits: CandidateLimits = dataclass_field(default_factory=CandidateLimits)
    weights: Weights = dataclass_field(default_factory=Weights)
    confidence: Confidence = dataclass_field(default_factory=Confidence)
    traversal: Traversal = dataclass_field(default_factory=Traversal)
    models: LinkingModels = dataclass_field(default_factory=LinkingModels)
    # Step 7. How far the top retrieval candidate must lead the next one for
    # the fast path to accept it without asking the model.
    fast_path_margin: float = 0.25
    # The date field a period applies to when the question names none
    # ("background checks in May 2026"). A platform field every object has.
    default_date_field: str = "CreatedDate"


def load_schema_linking_config(root: str | Path = "salesforce_knowledge"
                               ) -> SchemaLinkingConfig:
    try:
        import yaml
    except ImportError:
        return SchemaLinkingConfig()

    path = Path(root) / "config" / "schema_config.yaml"
    if not path.is_file():
        # Not an error -- tests build linkers from defaults -- but never
        # silent: a deployment that lands here has lost every setting in the
        # YAML, and the symptom (truncated model answers) points elsewhere.
        import logging
        logging.getLogger(__name__).warning(
            "schema linking config not found at %s; using built-in defaults",
            path.resolve())
        return SchemaLinkingConfig()
    raw = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}
           ).get("schema_linking") or {}

    limits = raw.get("candidate_limits") or {}
    weights = raw.get("weights") or {}
    confidence = raw.get("confidence") or {}
    traversal = raw.get("relationship_traversal") or {}
    models = raw.get("models") or {}

    def endpoint(key: str) -> ModelEndpoint:
        """A tier names a ROLE (`role: main`) or pins an endpoint explicitly.

        Naming the role is the default and the point: the linker then reaches
        exactly the deployment every other semantic stage reaches, resolved in
        model_roles, instead of a copy of its URL that can go stale.
        """
        block = models.get(key) or {}
        if block.get("endpoint") or block.get("name"):
            default = ModelEndpoint()
            return ModelEndpoint(endpoint=str(block.get("endpoint", default.endpoint)),
                                 name=str(block.get("name", default.name)))
        try:
            from model_roles import resolve_role
        except ImportError:
            return ModelEndpoint()
        resolved = resolve_role(str(block.get("role") or "main"), root)
        return ModelEndpoint(endpoint=resolved.endpoint, name=resolved.model)

    return SchemaLinkingConfig(
        candidate_limits=CandidateLimits(
            objects=int(limits.get("objects", 10)),
            fields=int(limits.get("fields", 10)),
            relationships=int(limits.get("relationships", 5))),
        weights=Weights(**{k: float(v) for k, v in weights.items()
                           if k in Weights.__dataclass_fields__}),
        confidence=Confidence(
            accept_small_model=float(confidence.get("accept_small_model", 0.85)),
            fallback_to_main_model=float(confidence.get("fallback_to_main_model", 0.60))),
        traversal=Traversal(
            max_depth=int(traversal.get("max_depth", 2)),
            max_related_objects_per_step=int(
                traversal.get("max_related_objects_per_step", 10))),
        models=LinkingModels(
            primary=endpoint("primary"), fallback=endpoint("fallback"),
            timeout_seconds=float(models.get("timeout_seconds", 60)),
            max_tokens=int(models.get("max_tokens", 300))),
        fast_path_margin=float((raw.get("semantic") or {}).get("fast_path_margin", 0.25)),
        default_date_field=str((raw.get("temporal") or {}).get("default_date_field",
                                                                "CreatedDate"))
    )
