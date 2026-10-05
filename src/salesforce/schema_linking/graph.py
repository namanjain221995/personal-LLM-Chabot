"""The org's relationships as a graph, and path search over it (§18).

Node: a Salesforce object. Edge: a lookup or master-detail field. Every edge
is walkable both ways:

    parent  Interview__c --Recruiter__c--> Recruiter__c     (a join to one row)
    child   Account <--Candidate__c-- Interview__c           (many rows: EXISTS)

Paths are discovered from the runtime schema, never written down. Shortest
first; among paths of equal length, the one whose field labels share the most
words with the question wins -- "manages" is how Account_Manager__c beats five
other lookups that also reach a User.

Platform audit lookups (CreatedById, LastModifiedById) are not edges: they
reach User from every object and would make "shortest path" meaningless.
"""
from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field as dataclass_field
from typing import Any

AUDIT_FIELDS = {"CreatedById", "LastModifiedById", "OwnerId"}


@dataclass(frozen=True)
class Hop:
    source: str          # object the hop starts from
    field: str           # the lookup field (always on the CHILD side)
    target: str          # object the hop reaches
    direction: str       # parent | child
    label: str = ""

    def describe(self) -> str:
        arrow = "->" if self.direction == "parent" else "<-"
        return f"{self.source} {arrow}[{self.field}] {self.target}"


@dataclass
class Path:
    hops: list[Hop] = dataclass_field(default_factory=list)
    score: float = 0.0

    @property
    def target(self) -> str | None:
        return self.hops[-1].target if self.hops else None

    @property
    def parent_only(self) -> bool:
        return all(h.direction == "parent" for h in self.hops)

    def describe(self) -> str:
        return "  ".join(h.describe() for h in self.hops)

    def as_dict(self) -> dict[str, Any]:
        return {"hops": [h.__dict__ for h in self.hops], "score": self.score}


def _stem(word: str) -> str:
    """Plural to singular, nothing more: "recruiters" ~ "Recruiter",
    "candidates" ~ "Candidate", "activities" ~ "Activity". Anything cleverer
    ("candidat", "recruit") stops labels from matching the words they are."""
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _tokens(text: str) -> set[str]:
    words = re.split(r"[^a-z0-9]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text or "").lower())
    return {_stem(w) for w in words if len(w) > 2 and w not in {"the", "and", "for", "with"}}


class RelationshipGraph:
    def __init__(self, schema_service: Any) -> None:
        self.schema = schema_service
        self._parents: dict[str, list[Hop]] = {}
        self._children: dict[str, list[Hop]] = {}
        self._load()

    def _load(self) -> None:
        connection = self.schema.db.connection
        labels = {(o, f): l for o, f, l in connection.execute(
            "SELECT object_api_name, api_name, label FROM fields")}
        for source, field, target in connection.execute(
                "SELECT source_object, source_field, target_object FROM relationships"):
            if field in AUDIT_FIELDS or not target:
                continue
            label = labels.get((source, field)) or field
            self._parents.setdefault(source, []).append(
                Hop(source, field, target, "parent", label))
            self._children.setdefault(target, []).append(
                Hop(target, field, source, "child", label))

    def parents(self, obj: str) -> list[Hop]:
        return list(self._parents.get(obj, []))

    def children(self, obj: str) -> list[Hop]:
        return list(self._children.get(obj, []))

    def paths(self, start: str, goal: str, *, max_hops: int = 3,
              allow_child: bool = True, via: list[str] | None = None,
              context: str = "", limit: int = 12) -> list[Path]:
        """Every simple path start -> goal up to max_hops, best first.

        `via` names objects the path must pass through in order -- the
        question's intermediate entities ("the job requirement associated with
        the submission"). A path that skips them answers a different question.
        """
        if start == goal:
            return [Path()]
        words = _tokens(context)
        found: list[Path] = []
        queue: deque[list[Hop]] = deque([[]])
        while queue:
            hops = queue.popleft()
            here = hops[-1].target if hops else start
            if len(hops) >= max_hops:
                continue
            edges = self.parents(here) + (self.children(here) if allow_child else [])
            for hop in edges:
                if hop.target == start or any(h.target == hop.target for h in hops):
                    continue
                path = hops + [hop]
                if hop.target == goal:
                    found.append(Path(path))
                else:
                    queue.append(path)
        if via:
            found = [p for p in found if _passes(p, via)]
        # Words that only repeat the starting object's own name cannot tell its
        # lookups apart: every lookup on Interview__c is "Interview something".
        evidence = words - _tokens(start)
        for path in found:
            overlap = sum(len(evidence & _tokens(h.label)) for h in path.hops)
            parents = sum(1 for h in path.hops if h.direction == "parent")
            # Shorter first; then label evidence; then parent hops (a parent
            # join is one row, a child join is many -- prefer the former).
            path.score = round(-len(path.hops) + 0.3 * overlap + 0.05 * parents, 4)
        found.sort(key=lambda p: (-p.score, p.describe()))
        return found[:limit]

    def lookups_from(self, obj: str) -> list[Hop]:
        """All parent hops out of an object: options when no path fits."""
        return self.parents(obj)


def _passes(path: Path, via: list[str]) -> bool:
    visited = [h.target for h in path.hops]
    position = 0
    for obj in via:
        try:
            position = visited.index(obj, position) + 1
        except ValueError:
            return False
    return True
