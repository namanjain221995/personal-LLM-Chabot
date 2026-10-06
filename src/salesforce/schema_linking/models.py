"""Records the schema-linking pipeline passes between its stages.

The intent and the grounded plan are kept apart on purpose. The intent holds
what the user said -- "mock interview", "status", "candidate email" -- and the
plan holds what Salesforce actually calls those things. Overwriting the first
with the second would make a wrong mapping untraceable: you could no longer
tell whether the question was misread or the concept was mislinked.
"""
from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from enum import Enum
from typing import Any


class FailureCode(str, Enum):
    """Why linking stopped. Never a silent guess."""

    NO_OBJECT_CANDIDATES = "NO_OBJECT_CANDIDATES"
    AMBIGUOUS_OBJECT = "AMBIGUOUS_OBJECT"
    OBJECT_VERIFICATION_FAILED = "OBJECT_VERIFICATION_FAILED"

    NO_FIELD_CANDIDATES = "NO_FIELD_CANDIDATES"
    AMBIGUOUS_FIELD = "AMBIGUOUS_FIELD"
    FIELD_VERIFICATION_FAILED = "FIELD_VERIFICATION_FAILED"

    NO_RELATIONSHIP_PATH = "NO_RELATIONSHIP_PATH"
    AMBIGUOUS_RELATIONSHIP = "AMBIGUOUS_RELATIONSHIP"
    RELATIONSHIP_VERIFICATION_FAILED = "RELATIONSHIP_VERIFICATION_FAILED"

    SMALL_MODEL_LOW_CONFIDENCE = "SMALL_MODEL_LOW_CONFIDENCE"
    MAIN_MODEL_LOW_CONFIDENCE = "MAIN_MODEL_LOW_CONFIDENCE"

    SCHEMA_LINKING_FAILED = "SCHEMA_LINKING_FAILED"


class Evidence(str, Enum):
    """Why a candidate was retrieved. Deterministic, never model-authored."""

    EXACT_API = "exact_api_match"
    EXACT_LABEL = "exact_label_match"
    MANUAL_ALIAS = "manual_alias_match"
    AUTO_ALIAS = "auto_alias_match"
    FTS = "fts_match"
    DESCRIPTION = "description_match"
    RELATIONSHIP = "relationship_match"
    PICKLIST_VALUE = "picklist_value_match"
    DATA_TYPE = "data_type_match"
    NAME_TOKEN = "name_token_match"


@dataclass
class ObjectCandidate:
    api_name: str
    label: str | None = None
    description: str | None = None
    aliases: list[str] = dataclass_field(default_factory=list)
    related_objects: list[str] = dataclass_field(default_factory=list)
    retrieval_score: float = 0.0
    evidence: list[str] = dataclass_field(default_factory=list)
    field_count: int = 0
    is_hot_object: bool = False

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def for_model(self) -> dict[str, Any]:
        """Only what a reranking decision needs. No credentials, no full schema."""
        return {"api_name": self.api_name, "label": self.label,
                "description": (self.description or "")[:300],
                "aliases": self.aliases[:6],
                "related_objects": self.related_objects[:8],
                "evidence": self.evidence}


@dataclass
class FieldCandidate:
    object_api_name: str
    api_name: str
    label: str | None = None
    description: str | None = None
    data_type: str | None = None
    picklist_values: list[str] = dataclass_field(default_factory=list)
    reference_to: list[str] = dataclass_field(default_factory=list)
    relationship_name: str | None = None
    aliases: list[str] = dataclass_field(default_factory=list)
    retrieval_score: float = 0.0
    evidence: list[str] = dataclass_field(default_factory=list)
    # Set when a picklist value matched the filter value the user gave.
    matched_picklist_value: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def for_model(self) -> dict[str, Any]:
        return {"api_name": self.api_name, "label": self.label,
                "description": (self.description or "")[:200],
                "data_type": self.data_type,
                "picklist_values": self.picklist_values[:12],
                "reference_to": self.reference_to,
                "evidence": self.evidence,
                "matched_picklist_value": self.matched_picklist_value}


@dataclass
class RelationshipCandidate:
    source_object: str
    source_field: str
    target_object: str
    # The name used to traverse PARENT-ward in SOQL. Derived from the field
    # name (Candidate__c -> Candidate__r), NOT the relationship_name column,
    # which holds the child-side name (Account -> Internal_Interviews).
    traversal_name: str = ""
    child_relationship_name: str | None = None
    relationship_type: str | None = None
    retrieval_score: float = 0.0
    evidence: list[str] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def for_model(self) -> dict[str, Any]:
        return {"source_field": self.source_field,
                "relationship_name": self.traversal_name,
                "target_object": self.target_object,
                "relationship_type": self.relationship_type,
                "evidence": self.evidence}


@dataclass
class Selection:
    """One model decision, after validation."""
    selected: str | None
    confidence: float
    reason_codes: list[str] = dataclass_field(default_factory=list)
    model: str = ""
    escalated: bool = False
    duration_ms: int = 0
    resolved_value: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class EntityMapping:
    business_entity: str
    salesforce_object: str
    confidence: float
    role: str = "primary_entity"
    source_field: str | None = None
    relationship_name: str | None = None
    target_object: str | None = None
    reason_codes: list[str] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class FilterMapping:
    business_concept: str
    field: str
    operator: str
    value: Any
    confidence: float
    object_api_name: str = ""
    data_type: str | None = None
    reason_codes: list[str] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class AttributeMapping:
    business_attribute: str
    field_path: str
    target_object: str
    target_field: str
    confidence: float
    reason_codes: list[str] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class GroundedSchemaPlan:
    """The only schema input a later SOQL generator is allowed to read.

    Every identifier here has been verified against the runtime schema. Nothing
    a model named survives into this structure without existing in the org.
    """
    primary_object: str | None = None
    objects: list[str] = dataclass_field(default_factory=list)
    entity_mappings: list[EntityMapping] = dataclass_field(default_factory=list)
    filter_mappings: list[FilterMapping] = dataclass_field(default_factory=list)
    requested_attribute_mappings: list[AttributeMapping] = dataclass_field(
        default_factory=list)
    schema_grounded: bool = False
    requires_fallback: bool = False
    failures: list[dict[str, Any]] = dataclass_field(default_factory=list)
    trace: list[Any] = dataclass_field(default_factory=list)
    duration_ms: int = 0
    # How the plan was decided: linker mode, whether the main model was
    # called, which model, how long. Empty for the offline linker.
    semantic: dict[str, Any] = dataclass_field(default_factory=dict)

    def fail(self, code: FailureCode, detail: str, **context: Any) -> None:
        self.failures.append({"code": code.value, "detail": detail, **context})
        self.schema_grounded = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "primary_object": self.primary_object,
            "objects": self.objects,
            "entity_mappings": [m.as_dict() for m in self.entity_mappings],
            "filter_mappings": [m.as_dict() for m in self.filter_mappings],
            "requested_attribute_mappings": [
                m.as_dict() for m in self.requested_attribute_mappings],
            "schema_grounded": self.schema_grounded,
            "requires_fallback": self.requires_fallback,
            "failures": self.failures,
            "duration_ms": self.duration_ms,
            "semantic": self.semantic,
        }
