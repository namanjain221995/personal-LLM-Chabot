"""Deterministic candidate retrieval and scoring.

No model runs here. Everything this module produces is derived from the runtime
schema by rules, which is what makes it testable on its own and what makes the
`retrieval_score` on a candidate mean something a reranker can be checked
against.

The scoring is additive across independent evidence: a field whose label
matches AND whose picklist contains the user's value is a better candidate than
one with either alone, and the score should say so. But the bands are gapped so
no accumulation of weak lexical evidence reaches the score of one exact match.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from .config import SchemaLinkingConfig, Weights
from .models import Evidence, FieldCandidate, ObjectCandidate, RelationshipCandidate

_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SUFFIX = re.compile(r"__(c|mdt|e|b|x|kav|r)$")

# Values whose shape implies a data type, for the §14 compatibility evidence.
_BOOLEAN_WORDS = {"true", "false", "yes", "no"}
_DATE_WORDS = {"today", "tomorrow", "yesterday", "now", "this week", "last week",
               "this month", "last month", "this year", "last year"}
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}")

DATE_TYPES = {"date", "datetime", "time"}
NUMERIC_TYPES = {"number", "currency", "percent", "double", "int", "summary"}
TEXT_TYPES = {"text", "textarea", "longtextarea", "string", "email", "phone",
              "url", "picklist", "multiselectpicklist", "html"}
BOOLEAN_TYPES = {"checkbox", "boolean"}


def tokens(name: str) -> set[str]:
    """Words inside an identifier: Scheduled_Date__c -> {scheduled, date}."""
    bare = _SUFFIX.sub("", name or "")
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", _CAMEL.sub(" ", bare)) if w}


def normalise(text: Any) -> str:
    return str(text or "").strip().lower()


def expected_types(value: Any, operator: str | None = None) -> set[str]:
    """Data types a value's shape implies. Evidence, never a veto.

    A date filter makes Date fields likelier, but "Scheduled_Date__c" beating
    "CreatedDate" is a semantic judgement -- type only narrows the field.
    """
    if value is None:
        return set()
    if isinstance(value, bool):
        return BOOLEAN_TYPES
    if isinstance(value, (int, float)):
        return NUMERIC_TYPES
    text = normalise(value)
    if text in _BOOLEAN_WORDS:
        return BOOLEAN_TYPES
    if text in _DATE_WORDS or _DATE_PATTERN.match(text):
        return DATE_TYPES
    if operator in {"greater_than", "less_than", "between"}:
        return NUMERIC_TYPES | DATE_TYPES
    return TEXT_TYPES


class CandidateRetriever:
    """Builds scored candidates from the runtime schema."""

    def __init__(self, schema_service: Any, config: SchemaLinkingConfig) -> None:
        self.schema = schema_service
        self.config = config

    @property
    def weights(self) -> Weights:
        return self.config.weights

    # -- objects ----------------------------------------------------------
    def object_candidates(self, term: str, *,
                          related_entities: Sequence[str] = (),
                          limit: int | None = None) -> list[ObjectCandidate]:
        limit = limit or self.config.candidate_limits.objects
        term_normalised = normalise(term)
        if not term_normalised:
            return []

        scored: dict[str, ObjectCandidate] = {}

        def ensure(api_name: str) -> ObjectCandidate | None:
            if api_name in scored:
                return scored[api_name]
            entry = self.schema.cache.catalog_entry(api_name)
            if entry is None:
                row = self.schema.repo.get_object(api_name)
                if row is None:
                    return None
                entry = {"api_name": api_name, "label": row.get("label"),
                         "description": row.get("description"),
                         "field_count": row.get("field_count", 0),
                         "is_hot_object": row.get("is_hot_object", 0),
                         "aliases": None}
            candidate = ObjectCandidate(
                api_name=api_name, label=entry.get("label"),
                description=entry.get("description"),
                aliases=[a for a in (entry.get("aliases") or "").split("|") if a],
                field_count=int(entry.get("field_count") or 0),
                is_hot_object=bool(entry.get("is_hot_object")))
            scored[api_name] = candidate
            return candidate

        def award(api_name: str, weight: float, evidence: Evidence) -> None:
            candidate = ensure(api_name)
            if candidate is None:
                return
            if evidence.value not in candidate.evidence:
                candidate.evidence.append(evidence.value)
                candidate.retrieval_score += weight

        connection = self.schema.db.connection

        for row in connection.execute(
                "SELECT api_name FROM objects WHERE api_name = ? COLLATE NOCASE",
                (term,)):
            award(row[0], self.weights.exact_api, Evidence.EXACT_API)
        for row in connection.execute(
                "SELECT api_name FROM objects WHERE label = ? COLLATE NOCASE"
                " OR plural_label = ? COLLATE NOCASE", (term, term)):
            award(row[0], self.weights.exact_label, Evidence.EXACT_LABEL)
        for row in connection.execute(
                "SELECT object_api_name, is_manual FROM object_aliases"
                " WHERE alias = ? COLLATE NOCASE", (term_normalised,)):
            manual = bool(row[1])
            award(row[0],
                  self.weights.manual_alias if manual else self.weights.auto_alias,
                  Evidence.MANUAL_ALIAS if manual else Evidence.AUTO_ALIAS)

        for position, entry in enumerate(
                self.schema.search_objects(term, limit * 2)):
            if entry.get("matched_by") == "fts":
                award(entry["api_name"],
                      max(0.1, self.weights.fts - position * 0.01), Evidence.FTS)

        # An object that relates to something else the question named is more
        # likely the subject than one that stands alone.
        for name in related_entities:
            for related in self.object_candidates_by_name(normalise(name)):
                for rel in self.schema.repo.get_child_relationships(related):
                    if rel["child_object"] in scored:
                        award(rel["child_object"], self.weights.relationship,
                              Evidence.RELATIONSHIP)
                for rel in self.schema.repo.get_relationships(related):
                    if rel["target_object"] in scored:
                        award(rel["target_object"], self.weights.relationship,
                              Evidence.RELATIONSHIP)

        for candidate in scored.values():
            if not candidate.aliases:
                candidate.aliases = self.schema.repo.get_object_aliases(
                    candidate.api_name)[:6]
            candidate.related_objects = sorted({
                r["target_object"] for r in
                self.schema.repo.get_relationships(candidate.api_name)})[:8]
            candidate.retrieval_score = round(candidate.retrieval_score, 4)

        ranked = sorted(scored.values(),
                        key=lambda c: (-c.retrieval_score, c.api_name))
        return ranked[:limit]

    def object_candidates_by_name(self, term: str) -> list[str]:
        """Exact-ish object names for a term. Used for relationship evidence."""
        connection = self.schema.db.connection
        found = [r[0] for r in connection.execute(
            "SELECT api_name FROM objects WHERE api_name = ? COLLATE NOCASE"
            " OR label = ? COLLATE NOCASE OR plural_label = ? COLLATE NOCASE",
            (term, term, term))]
        found += [r[0] for r in connection.execute(
            "SELECT object_api_name FROM object_aliases WHERE alias = ? COLLATE NOCASE",
            (term,))]
        return list(dict.fromkeys(found))

    # -- fields -----------------------------------------------------------
    def field_candidates(self, object_api_name: str, concept: str, *,
                         value: Any = None, operator: str | None = None,
                         limit: int | None = None) -> list[FieldCandidate]:
        """Candidates for one concept, searched WITHIN one object.

        Scoped deliberately: a global search over 4,592 fields returns the same
        name on forty objects and buries the one that belongs to the object
        already selected.
        """
        limit = limit or self.config.candidate_limits.fields
        concept_normalised = normalise(concept)
        if not concept_normalised:
            return []

        scored: dict[str, FieldCandidate] = {}
        concept_tokens = tokens(concept)
        wanted_types = expected_types(value, operator)
        value_normalised = normalise(value) if value is not None else None

        def ensure(api_name: str) -> FieldCandidate | None:
            if api_name in scored:
                return scored[api_name]
            row = self.schema.repo.get_field(object_api_name, api_name)
            if row is None:
                return None
            candidate = FieldCandidate(
                object_api_name=object_api_name, api_name=api_name,
                label=row.get("label"), description=row.get("description"),
                data_type=row.get("data_type"),
                reference_to=row.get("reference_to") or [],
                relationship_name=row.get("relationship_name"),
                picklist_values=[v["value"] for v in
                                 self.schema.repo.get_picklist_values(
                                     object_api_name, api_name)],
                aliases=self.schema.repo.get_field_aliases(
                    object_api_name, api_name)[:6])
            scored[api_name] = candidate
            return candidate

        def award(api_name: str, weight: float, evidence: Evidence) -> None:
            candidate = ensure(api_name)
            if candidate is None:
                return
            if evidence.value not in candidate.evidence:
                candidate.evidence.append(evidence.value)
                candidate.retrieval_score += weight

        connection = self.schema.db.connection

        for row in connection.execute(
                "SELECT api_name FROM fields WHERE object_api_name=?"
                " AND api_name = ? COLLATE NOCASE", (object_api_name, concept)):
            award(row[0], self.weights.exact_api, Evidence.EXACT_API)
        for row in connection.execute(
                "SELECT api_name FROM fields WHERE object_api_name=?"
                " AND label = ? COLLATE NOCASE", (object_api_name, concept)):
            award(row[0], self.weights.exact_label, Evidence.EXACT_LABEL)
        for row in connection.execute(
                "SELECT field_api_name, is_manual FROM field_aliases"
                " WHERE object_api_name=? AND alias = ? COLLATE NOCASE",
                (object_api_name, concept_normalised)):
            manual = bool(row[1])
            award(row[0],
                  self.weights.manual_alias if manual else self.weights.auto_alias,
                  Evidence.MANUAL_ALIAS if manual else Evidence.AUTO_ALIAS)

        # A hand-reviewed multi-word alias whose words all appear in the
        # concept: "offer received status" contains "offer status". Exact
        # matching missed it, and the business meaning then reached the model
        # only as a note -- which lost to a literal label match on
        # Interview_Status__c. Manual aliases only: they are few and reviewed,
        # so containment cannot flood the candidate set the way it would over
        # 5,000 generated ones.
        for row in connection.execute(
                "SELECT field_api_name, alias FROM field_aliases"
                " WHERE object_api_name=? AND is_manual = 1", (object_api_name,)):
            alias_tokens = set(normalise(row[1]).split())
            if len(alias_tokens) >= 2 and alias_tokens <= set(concept_normalised.split()) \
                    and normalise(row[1]) != concept_normalised:
                award(row[0], self.weights.manual_alias * 0.9, Evidence.MANUAL_ALIAS)

        for position, entry in enumerate(
                self.schema.search_fields(concept, object_api_name, limit * 2)):
            if entry.get("matched_by") == "fts":
                award(entry["api_name"],
                      max(0.1, self.weights.fts - position * 0.01), Evidence.FTS)

        # Name-token overlap: "status" reaching Mock_Status__c even when the
        # label is "Mock Status" and no exact match fired.
        for row in connection.execute(
                "SELECT api_name, label FROM fields WHERE object_api_name=?",
                (object_api_name,)):
            overlap = concept_tokens & (tokens(row[0]) | tokens(row[1] or ""))
            if overlap:
                award(row[0], self.weights.name_token * (len(overlap) / max(
                    1, len(concept_tokens))), Evidence.NAME_TOKEN)

        # Picklist-value evidence: the strongest deterministic signal there is.
        # If the user said "unassigned" and exactly one field on this object has
        # that value, the field is all but decided.
        if isinstance(value, (list, tuple)) and value:
            # A set ("Rescheduled or Cancelled") is evidence only for a field
            # that holds EVERY member. Stringified whole, it matched nothing
            # and the choice fell to a lookalike field. Seen live.
            members: dict[str, list[str]] = {}
            for item in value:
                for row in connection.execute(
                        "SELECT field_api_name, value FROM picklist_values"
                        " WHERE object_api_name=? AND value = ? COLLATE NOCASE",
                        (object_api_name, str(item))):
                    members.setdefault(row[0], []).append(row[1])
            for api_name, found in members.items():
                if len(found) == len(value):
                    award(api_name, self.weights.picklist_value, Evidence.PICKLIST_VALUE)
                    candidate = scored.get(api_name)
                    if candidate:
                        candidate.matched_picklist_value = found
        elif value_normalised:
            for row in connection.execute(
                    "SELECT field_api_name, value FROM picklist_values"
                    " WHERE object_api_name=? AND value = ? COLLATE NOCASE",
                    (object_api_name, str(value))):
                award(row[0], self.weights.picklist_value, Evidence.PICKLIST_VALUE)
                candidate = scored.get(row[0])
                if candidate:
                    candidate.matched_picklist_value = row[1]

        if wanted_types:
            for candidate in list(scored.values()):
                if normalise(candidate.data_type) in wanted_types:
                    award(candidate.api_name, self.weights.data_type,
                          Evidence.DATA_TYPE)

        for candidate in scored.values():
            candidate.retrieval_score = round(candidate.retrieval_score, 4)
        ranked = sorted(scored.values(),
                        key=lambda c: (-c.retrieval_score, c.api_name))
        return ranked[:limit]

    # -- relationships ----------------------------------------------------
    @staticmethod
    def traversal_name(field_api_name: str, relationship_name: str | None) -> str:
        """The name SOQL uses to walk PARENT-ward across this field.

        For a custom field it is the API name with __c swapped for __r. The
        stored `relationship_name` is the CHILD-side name -- on
        Internal_Interview__c.Candidate__c it reads 'Internal_Interviews',
        which is how Account refers back to its interviews and is invalid in
        the parent direction. Getting these two confused produces SOQL that
        parses and returns the wrong thing.
        """
        if field_api_name.endswith("__c"):
            return field_api_name[:-3] + "__r"
        # Standard reference fields: OwnerId -> Owner, AccountId -> Account.
        if field_api_name.endswith("Id") and len(field_api_name) > 2:
            return field_api_name[:-2]
        return relationship_name or field_api_name

    def relationship_candidates(self, object_api_name: str, term: str, *,
                                limit: int | None = None
                                ) -> list[RelationshipCandidate]:
        limit = limit or self.config.candidate_limits.relationships
        term_normalised = normalise(term)
        term_tokens = tokens(term)
        candidates: list[RelationshipCandidate] = []

        for row in self.schema.repo.get_relationships(object_api_name):
            field_name = row["source_field"]
            candidate = RelationshipCandidate(
                source_object=object_api_name,
                source_field=field_name,
                target_object=row["target_object"],
                traversal_name=self.traversal_name(field_name,
                                                   row.get("relationship_name")),
                child_relationship_name=row.get("relationship_name"),
                relationship_type=row.get("relationship_type"))

            field = self.schema.repo.get_field(object_api_name, field_name)
            label = (field or {}).get("label")
            if normalise(field_name) == term_normalised or normalise(label) == term_normalised:
                candidate.retrieval_score += self.weights.exact_api
                candidate.evidence.append(Evidence.EXACT_API.value)
            overlap = term_tokens & (tokens(field_name) | tokens(label or ""))
            if overlap:
                candidate.retrieval_score += self.weights.name_token * (
                    len(overlap) / max(1, len(term_tokens)))
                candidate.evidence.append(Evidence.NAME_TOKEN.value)
            if term_tokens & tokens(row["target_object"]):
                candidate.retrieval_score += self.weights.relationship
                candidate.evidence.append(Evidence.RELATIONSHIP.value)
            for alias in self.schema.repo.get_field_aliases(object_api_name, field_name):
                if alias == term_normalised:
                    candidate.retrieval_score += self.weights.auto_alias
                    candidate.evidence.append(Evidence.AUTO_ALIAS.value)
                    break
            # "candidate" reaching Interview__c.Candidate__c through the fact
            # that Account IS the candidate object -- a hand-reviewed alias on
            # the target says so even when the field is named differently.
            if term_normalised in self.schema.repo.get_object_aliases(
                    row["target_object"]):
                candidate.retrieval_score += self.weights.manual_alias * 0.5
                candidate.evidence.append(Evidence.MANUAL_ALIAS.value)

            if candidate.evidence:
                candidate.retrieval_score = round(candidate.retrieval_score, 4)
                candidates.append(candidate)

        candidates.sort(key=lambda c: (-c.retrieval_score, c.source_field))
        return candidates[:limit]
