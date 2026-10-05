"""Check every identifier a model produced against the runtime schema.

Two independent gates, and both matter:

  membership  the model must pick FROM the candidate list it was given. A name
              that was not offered is an invention, however plausible it looks.
  existence   the picked name must exist in the runtime schema. The candidate
              list is built from that schema, so this should never fail -- and
              that is exactly why it is checked: a failure here means a bug,
              and finding it now beats emitting SOQL against an object the org
              does not have.

Nothing reaches the grounded plan without passing both.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from .models import FailureCode

# Salesforce identifiers are letters, digits and underscores. Anything else is
# either a hallucination or an injection attempt.
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


@dataclass
class VerificationResult:
    ok: bool
    value: str | None = None
    code: FailureCode | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.ok


def _valid_identifier(name: str) -> bool:
    return bool(name) and bool(_IDENTIFIER.match(name)) and len(name) <= 80


class SchemaVerifier:
    def __init__(self, schema_service: Any) -> None:
        self.schema = schema_service

    def verify_object(self, selected: str | None,
                      candidates: Sequence[Any]) -> VerificationResult:
        offered = {c.api_name for c in candidates}
        if not selected:
            return VerificationResult(False, code=FailureCode.OBJECT_VERIFICATION_FAILED,
                                      detail="model returned no object")
        if not _valid_identifier(selected):
            return VerificationResult(
                False, code=FailureCode.OBJECT_VERIFICATION_FAILED,
                detail=f"{selected!r} is not a Salesforce identifier")
        if selected not in offered:
            return VerificationResult(
                False, code=FailureCode.OBJECT_VERIFICATION_FAILED,
                detail=f"{selected!r} was not among the candidates offered")
        if self.schema.repo.get_object(selected) is None:
            return VerificationResult(
                False, code=FailureCode.OBJECT_VERIFICATION_FAILED,
                detail=f"{selected!r} is not in the runtime schema")
        return VerificationResult(True, value=selected)

    def verify_field(self, object_api_name: str, selected: str | None,
                     candidates: Sequence[Any]) -> VerificationResult:
        offered = {c.api_name for c in candidates}
        if not selected:
            return VerificationResult(False, code=FailureCode.FIELD_VERIFICATION_FAILED,
                                      detail="model returned no field")
        if not _valid_identifier(selected):
            return VerificationResult(
                False, code=FailureCode.FIELD_VERIFICATION_FAILED,
                detail=f"{selected!r} is not a Salesforce identifier")
        if selected not in offered:
            return VerificationResult(
                False, code=FailureCode.FIELD_VERIFICATION_FAILED,
                detail=f"{selected!r} was not among the candidates offered")
        if self.schema.repo.get_field(object_api_name, selected) is None:
            return VerificationResult(
                False, code=FailureCode.FIELD_VERIFICATION_FAILED,
                detail=f"{object_api_name}.{selected!r} is not in the runtime schema")
        return VerificationResult(True, value=selected)

    def verify_relationship(self, object_api_name: str, selected_field: str | None,
                            candidates: Sequence[Any]) -> VerificationResult:
        offered = {c.source_field for c in candidates}
        if not selected_field:
            return VerificationResult(
                False, code=FailureCode.RELATIONSHIP_VERIFICATION_FAILED,
                detail="model returned no relationship field")
        if selected_field not in offered:
            return VerificationResult(
                False, code=FailureCode.RELATIONSHIP_VERIFICATION_FAILED,
                detail=f"{selected_field!r} was not among the candidates offered")
        field = self.schema.repo.get_field(object_api_name, selected_field)
        if field is None or not field.get("reference_to"):
            return VerificationResult(
                False, code=FailureCode.RELATIONSHIP_VERIFICATION_FAILED,
                detail=f"{object_api_name}.{selected_field} is not a reference field")
        return VerificationResult(True, value=selected_field)

    def verify_picklist_value(self, object_api_name: str, field_api_name: str,
                              value: Any) -> str | None:
        """The stored spelling of a picklist value, or None.

        Returns the value AS SALESFORCE STORES IT: a filter written with the
        user's casing matches nothing, so "unassigned" must become
        "Unassigned" before it reaches a query.
        """
        if value is None:
            return None
        wanted = str(value).strip().lower()
        for row in self.schema.repo.get_picklist_values(object_api_name,
                                                        field_api_name):
            if str(row["value"]).strip().lower() == wanted:
                return row["value"]
        return None

    def verify_field_path(self, root_object: str, traversal_name: str,
                          target_object: str,
                          target_field: str) -> VerificationResult:
        """A relationship path, end to end, before it can be written into SOQL."""
        if self.schema.repo.get_object(target_object) is None:
            return VerificationResult(
                False, code=FailureCode.RELATIONSHIP_VERIFICATION_FAILED,
                detail=f"target object {target_object!r} is not in the runtime schema")
        if self.schema.repo.get_field(target_object, target_field) is None:
            return VerificationResult(
                False, code=FailureCode.FIELD_VERIFICATION_FAILED,
                detail=f"{target_object}.{target_field} is not in the runtime schema")
        return VerificationResult(True, value=f"{traversal_name}.{target_field}")
