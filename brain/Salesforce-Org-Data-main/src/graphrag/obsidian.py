from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

from .models import Graph, Node


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    cleaned = re.sub(r'[\\/:*?"<>|#^\[\]]+', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    if len(cleaned) > 120:
        digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
        cleaned = f"{cleaned[:109].rstrip()}_{digest}"
    return cleaned or "unnamed"


def _note_paths(graph: Graph) -> dict[str, str]:
    paths: dict[str, str] = {}
    used: set[str] = set()
    for node in sorted(graph.nodes, key=lambda item: item.id):
        base = f"{_safe_name(node.kind)}/{_safe_name(node.name)}"
        path = base
        suffix = 2
        while path in used:
            path = f"{base} ({suffix})"
            suffix += 1
        used.add(path)
        paths[node.id] = path
    return paths


def _link(node_id: str, paths: dict[str, str]) -> str:
    return f"[[{paths[node_id]}]]"


def _properties(node: Node) -> list[str]:
    lines = []
    for key, value in sorted(node.properties.items()):
        rendered = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
        lines.append(f"- **{key}**: `{rendered}`")
    return lines


def export_obsidian(graph: Graph, output: str | Path) -> int:
    """Write an Obsidian-compatible Markdown vault from a graph artifact."""
    root = Path(output)
    root.mkdir(parents=True, exist_ok=True)
    paths = _note_paths(graph)
    nodes = {node.id: node for node in graph.nodes}
    outgoing: dict[str, list] = {node.id: [] for node in graph.nodes}
    incoming: dict[str, list] = {node.id: [] for node in graph.nodes}
    for edge in graph.edges:
        if edge.source in outgoing and edge.target in nodes:
            outgoing[edge.source].append(edge)
            incoming[edge.target].append(edge)

    index_lines = [
        "# Salesforce Metadata Graph",
        "",
        "This vault was generated from `graph.jsonl`. Notes are generated; do not edit them as a source of truth.",
        "",
        f"- Nodes: {len(graph.nodes)}",
        f"- Edges: {len(graph.edges)}",
        "",
        "## Browse by type",
        "",
    ]
    for kind in sorted({node.kind for node in graph.nodes}):
        index_lines.append(f"- {kind}: {sum(node.kind == kind for node in graph.nodes)}")
    (root / "README.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

    for node in graph.nodes:
        note_path = root / f"{paths[node.id]}.md"
        note_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"# {node.name}", "", f"- **Kind**: `{node.kind}`", f"- **ID**: `{node.id}`"]
        if node.path:
            lines.append(f"- **Source**: `{node.path}`")
        if node.properties:
            lines.extend(["", "## Properties", ""])
            lines.extend(_properties(node))
        if outgoing[node.id]:
            lines.extend(["", "## Outgoing relationships", ""])
            for edge in sorted(outgoing[node.id], key=lambda item: (item.kind, item.target)):
                lines.append(f"- **{edge.kind}** → {_link(edge.target, paths)}")
        if incoming[node.id]:
            lines.extend(["", "## Incoming relationships", ""])
            for edge in sorted(incoming[node.id], key=lambda item: (item.kind, item.source)):
                lines.append(f"- {_link(edge.source, paths)} **{edge.kind}** → this note")
        note_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(graph.nodes)
