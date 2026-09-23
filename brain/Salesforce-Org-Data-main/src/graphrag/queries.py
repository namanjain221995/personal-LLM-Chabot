from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .models import Edge, Graph, Node


class QueryError(ValueError):
    """Raised when a graph query has invalid input."""


@dataclass(frozen=True, slots=True)
class QueryMatch:
    node: Node
    paths: tuple[tuple[str, ...], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"node": self.node.to_dict(), "paths": [list(path) for path in self.paths]}


def _index(graph: Graph) -> tuple[dict[str, Node], dict[str, list[Edge]], dict[str, list[Edge]]]:
    nodes = {node.id: node for node in graph.nodes}
    outgoing: dict[str, list[Edge]] = {}
    incoming: dict[str, list[Edge]] = {}
    for edge in graph.edges:
        outgoing.setdefault(edge.source, []).append(edge)
        incoming.setdefault(edge.target, []).append(edge)
    return nodes, outgoing, incoming


def _matches(node: Node, query: str) -> bool:
    normalized = query.strip()
    if not normalized:
        raise QueryError("query must not be empty")
    if normalized == node.id or normalized == node.name:
        return True
    if ":" in normalized:
        return normalized == f"{node.kind}:{node.name}"
    return False


def find_nodes(graph: Graph, query: str, kind: str | None = None) -> tuple[Node, ...]:
    """Find nodes by exact id/name, optionally constrained by node kind."""
    return tuple(
        node for node in graph.nodes
        if (kind is None or node.kind == kind) and _matches(node, query)
    )


def fields_for_object(graph: Graph, object_name: str) -> tuple[Node, ...]:
    """Return fields defined on an exact Salesforce object name."""
    matches = find_nodes(graph, object_name.strip(), "object")
    if not matches:
        return ()
    _, outgoing, _ = _index(graph)
    fields: dict[str, Node] = {}
    nodes = {node.id: node for node in graph.nodes}
    for match in matches:
        for edge in outgoing.get(match.id, ()):
            if edge.kind == "has_field" and nodes[edge.target].kind == "field":
                fields[edge.target] = nodes[edge.target]
    return tuple(fields[key] for key in sorted(fields))


def required_fields(graph: Graph, object_name: str | None = None) -> tuple[Node, ...]:
    """Return field nodes marked required, optionally limited to one object."""
    fields = (node for node in graph.nodes if node.kind == "field")
    if object_name is not None:
        normalized = object_name.strip()
        if not normalized:
            raise QueryError("object name must not be empty")
        fields = (node for node in fields if node.properties.get("object") == normalized)
    return tuple(
        sorted(
            (node for node in fields if node.properties.get("required") is True),
            key=lambda node: node.id,
        )
    )


def picklist_values(graph: Graph, field_name: str) -> tuple[Node, ...]:
    """Return options defined for an exact field API name."""
    normalized = field_name.strip()
    if not normalized:
        raise QueryError("field name must not be empty")
    field_id = normalized if normalized.startswith("field:") else f"field:{normalized}"
    nodes, outgoing, _ = _index(graph)
    if field_id not in nodes or nodes[field_id].kind != "field":
        return ()
    return tuple(
        sorted(
            (nodes[edge.target] for edge in outgoing.get(field_id, ())
             if edge.kind == "has_value" and nodes[edge.target].kind == "picklist_value"),
            key=lambda node: node.id,
        )
    )


def dependencies(
    graph: Graph,
    query: str,
    *,
    kind: str | None = None,
) -> tuple[QueryMatch, ...]:
    """Return direct source nodes that reference the matched target node(s)."""
    nodes, _, incoming = _index(graph)
    matches = find_nodes(graph, query, kind)
    results: dict[str, QueryMatch] = {}
    for target in matches:
        for edge in incoming.get(target.id, ()):
            source = nodes[edge.source]
            existing = results.get(source.id)
            path = (source.id, edge.kind, target.id)
            results[source.id] = QueryMatch(source, existing.paths + (path,) if existing else (path,))
    return tuple(results[key] for key in sorted(results))


def traverse_dependencies(
    graph: Graph,
    query: str,
    *,
    depth: int = 1,
    kind: str | None = None,
) -> tuple[QueryMatch, ...]:
    """Traverse reverse references from a target using a bounded depth."""
    if depth < 1:
        raise QueryError("depth must be at least 1")
    nodes, _, incoming = _index(graph)
    targets = find_nodes(graph, query, kind)
    queue = deque((node.id, (node.id,), 0) for node in targets)
    seen: set[tuple[str, int]] = set()
    paths: dict[str, list[tuple[str, ...]]] = {}
    while queue:
        current, path, distance = queue.popleft()
        if (current, distance) in seen:
            continue
        seen.add((current, distance))
        if distance:
            paths.setdefault(current, []).append(path)
        if distance >= depth:
            continue
        for edge in incoming.get(current, ()):
            queue.append((edge.source, path + (edge.kind, edge.source), distance + 1))
    return tuple(
        QueryMatch(nodes[node_id], tuple(paths[node_id]))
        for node_id in sorted(paths)
    )
