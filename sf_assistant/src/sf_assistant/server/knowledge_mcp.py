"""Optional: expose the ToolRegistry as a real MCP server.

``build_fastmcp(registry)`` registers every registry tool on an MCP server with the same
name, description and input schema; each tool returns ``json.dumps(registry.call(...))``.

The ``mcp`` package is imported lazily inside :func:`build_fastmcp`, so this module imports
cleanly when ``mcp`` is not installed. Both SDK generations are supported:

* ``mcp`` 1.x: ``from mcp.server.fastmcp import FastMCP``
* ``mcp`` 2.x: FastMCP was renamed ``MCPServer`` (``from mcp.server.mcpserver import MCPServer``)

Run as a stdio server (e.g. for Claude Desktop / MCP Inspector)::

    python -m sf_assistant.server.knowledge_mcp knowledge/objects/<Object>.json
"""

from __future__ import annotations

import inspect
import json
import sys
from typing import TYPE_CHECKING, Annotated, Any, Callable, Literal

if TYPE_CHECKING:  # pragma: no cover
    from sf_assistant.tools.knowledge_tools import ToolRegistry

__all__ = ["build_fastmcp", "load_fastmcp_class", "main"]

_JSON_TO_PY: dict[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool}


def load_fastmcp_class() -> type:
    """Return the installed SDK's FastMCP-compatible server class (lazy import).

    Raises ``ImportError`` with an install hint when ``mcp`` is missing.
    """
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore  # mcp 1.x

        return FastMCP
    except ImportError:
        pass
    try:
        from mcp.server.mcpserver import MCPServer  # type: ignore  # mcp 2.x

        return MCPServer
    except ImportError as exc:
        raise ImportError("The optional 'mcp' package is required: pip install mcp") from exc


def _describe(ptype: Any, description: str) -> Any:
    """Attach a description via ``Annotated[..., pydantic.Field]`` (pydantic ships with mcp)."""
    try:
        from pydantic import Field  # lazy: only needed when building the server
    except ImportError:  # pragma: no cover - mcp always depends on pydantic
        return ptype
    return Annotated[ptype, Field(description=description)]


def _make_tool_fn(registry: "ToolRegistry", name: str, schema: dict) -> Callable[..., str]:
    """Build a function whose signature mirrors ``schema`` so the SDK derives the same inputSchema."""
    props: dict[str, dict] = schema.get("properties", {})
    required = set(schema.get("required", []))

    def tool_fn(**kwargs: Any) -> str:
        args = {k: v for k, v in kwargs.items() if v is not None}
        return json.dumps(registry.call(name, args), ensure_ascii=False)

    params = []
    annotations: dict[str, Any] = {"return": str}
    # Required parameters first (Python signature rules), all keyword-only anyway.
    for pname in sorted(props, key=lambda p: p not in required):
        spec = props[pname]
        ptype: Any = _JSON_TO_PY.get(spec.get("type", "string"), str)
        if spec.get("enum"):
            ptype = Literal[tuple(spec["enum"])]  # type: ignore[valid-type]
        if spec.get("description"):
            ptype = _describe(ptype, spec["description"])
        if pname in required:
            ann: Any = ptype
            default: Any = inspect.Parameter.empty
        else:
            ann = ptype | None
            default = None
        params.append(inspect.Parameter(pname, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=ann))
        annotations[pname] = ann
    tool_fn.__signature__ = inspect.Signature(params, return_annotation=str)  # type: ignore[attr-defined]
    tool_fn.__annotations__ = annotations
    tool_fn.__name__ = name
    tool_fn.__qualname__ = name
    return tool_fn


def build_fastmcp(registry: "ToolRegistry", name: str | None = None) -> Any:
    """Create a FastMCP (mcp 1.x) / MCPServer (mcp 2.x) instance exposing every registry tool.

    Args:
        registry: the :class:`sf_assistant.tools.knowledge_tools.ToolRegistry` to expose.
        name: server name; defaults to ``"<Object> knowledge"``.

    Returns:
        The server instance; call ``.run()`` (stdio) or use its async ``list_tools`` /
        ``call_tool`` methods directly.
    """
    server_cls = load_fastmcp_class()
    server = server_cls(name or f"{registry.knowledge.api_name} knowledge")
    for tool in registry.tools.values():
        fn = _make_tool_fn(registry, tool.name, tool.input_schema)
        server.add_tool(fn, name=tool.name, description=tool.description)
    return server


def main(argv: list[str] | None = None) -> None:
    """Serve a knowledge file over stdio: ``python -m sf_assistant.server.knowledge_mcp <knowledge.json>``."""
    from sf_assistant.knowledge.search import KnowledgeIndex
    from sf_assistant.knowledge.store import ObjectKnowledge
    from sf_assistant.tools.knowledge_tools import ToolRegistry

    args = sys.argv[1:] if argv is None else argv
    path = args[0] if args else "knowledge/Internal_Interview__c.json"
    knowledge = ObjectKnowledge.load(path)
    registry = ToolRegistry(knowledge, KnowledgeIndex(knowledge.chunks()))
    build_fastmcp(registry).run()


if __name__ == "__main__":  # pragma: no cover
    main()
