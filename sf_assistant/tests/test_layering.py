"""Package boundary and import direction (was test_okrec_boundary.py, plus the layer check of test_import_layers.py).

- no duckdb on light imports, no imports from the other pipeline (src/, orchestrator, ...) or the notebook (okeval, okrec);
- imports go one way: config/prompts < knowledge < engine < tools < agent < evaluation/server < pipeline < cli < __main__ < web (the test
  page in web/, outside the package). Every import of every package module (top level, inside functions, under
  TYPE_CHECKING) points to the same or a lower layer, so knowledge never imports engine, engine never imports tools;
- no import cycle among the imports that run at import time (top level, not in functions, not under TYPE_CHECKING);
- the knowledge side (what came from okeval) never imports record code (was: okeval never imports okrec).

The check is static (ast), so it also sees imports on code paths the other tests do not run.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "sf_assistant"
WEB = ROOT / "web"

#: first part after ``sf_assistant.`` -> layer; a module may import only its own or a lower layer
LAYER = {"config": 0, "prompts": 0, "knowledge": 1, "engine": 2, "tools": 3, "agent": 4, "evaluation": 5, "server": 5,
         "pipeline": 6, "cli": 7, "__main__": 8}
NOTEBOOK = ("okeval", "okrec")

#: modules that came from okeval (the knowledge side, also used by the notebook): they never import record code
#: (knowledge/resolver.py and knowledge/terms.py: the term resolver and the knowledge part of its vocabulary, moved
#: down from engine/ by decision A1 of docs/resolver_integration.md, so the knowledge side can use them;
#: knowledge/components.py and tools/component_tools.py: the flow components and their tools, stage 4 of the flow
#: build, 2026-10-24)
KNOWLEDGE_SIDE = ["knowledge/store.py", "knowledge/search.py", "knowledge/resolver.py", "knowledge/terms.py",
                  "knowledge/components.py", "tools/registry.py", "tools/component_tools.py", "tools/knowledge_tools.py",
                  "agent/knowledge_agent.py", "agent/knowledge_prompts.py", "agent/llm.py", "agent/mock_llm.py",
                  "agent/question.py", "evaluation/questions.py", "evaluation/grading.py", "evaluation/report.py",
                  "server/knowledge_mcp.py"]
#: neither side: they wire both sides together (or are the package root / a layer's empty ``__init__``);
#: agent/timeline.py (latency logging, 2026-10-29) is neither: the answer's timeline both sides add entries to
WIRING = {"sf_assistant", "sf_assistant.config", "sf_assistant.prompts", "sf_assistant.pipeline", "sf_assistant.cli",
          "sf_assistant.__main__", "sf_assistant.agent.timeline"}

#: modules a ``--no-records`` run or a records-off page may import without loading duckdb (split modules included)
LIGHT = ["sf_assistant.config", "sf_assistant.engine.types", "sf_assistant.tools.open_records",
         "sf_assistant.knowledge.resolver", "sf_assistant.knowledge.terms", "sf_assistant.engine.vocabulary",
         "sf_assistant.tools.record_tools", "sf_assistant.tools.registry", "sf_assistant.tools.record_base",
         "sf_assistant.tools.discovery", "sf_assistant.tools.records", "sf_assistant.tools.people",
         "sf_assistant.tools.names", "sf_assistant.tools.lookups", "sf_assistant.tools.meta",
         "sf_assistant.agent.record_session", "sf_assistant.agent.checks", "sf_assistant.agent.results",
         "sf_assistant.agent.nudges", "sf_assistant.agent.triage", "sf_assistant.agent.people_answer",
         "sf_assistant.agent.merge", "sf_assistant.agent.tables", "sf_assistant.agent.charts"]


# ------------------------------------------------------------------------------------------------- static import graph
def _modules() -> dict[str, tuple[Path, list[str]]]:
    """``sf_assistant.x.y`` -> (file, package parts used to resolve relative imports)."""
    out = {}
    for path in sorted(SRC.rglob("*.py")):
        parts = ["sf_assistant", *path.relative_to(SRC).with_suffix("").parts]
        if path.name == "__init__.py":
            parts = parts[:-1]
            out[".".join(parts)] = (path, parts)
        else:
            out[".".join(parts)] = (path, parts[:-1])
    return out


MODULES = _modules()


def _is_type_checking(node: ast.AST) -> bool:
    t = node.test if isinstance(node, ast.If) else None
    return (isinstance(t, ast.Name) and t.id == "TYPE_CHECKING") or (isinstance(t, ast.Attribute) and t.attr == "TYPE_CHECKING")


def _targets(node: ast.Import | ast.ImportFrom, package: list[str]) -> list[str]:
    """Absolute module names an import statement loads (``from pkg import mod`` also loads ``pkg.mod``)."""
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if node.level:
        base = package[: len(package) - node.level + 1]
        module = ".".join(base + ([node.module] if node.module else []))
    else:
        module = node.module or ""
    return [module] + [f"{module}.{a.name}" for a in node.names if f"{module}.{a.name}" in MODULES]


def _imports(path: Path, package: list[str], runtime_only: bool = False) -> list[tuple[str, int]]:
    """(target, line) for every import; ``runtime_only``: just those that run when the module is imported."""
    found: list[tuple[str, int]] = []

    def visit(nodes):
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                found.extend((t, node.lineno) for t in _targets(node, package))
            elif runtime_only and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            elif runtime_only and _is_type_checking(node):
                visit(node.orelse)
            else:
                visit(ast.iter_child_nodes(node))

    visit(ast.parse(path.read_text(encoding="utf-8")).body)
    return found


def _layer(module: str) -> int | None:
    parts = module.split(".")
    return LAYER.get(parts[1]) if len(parts) > 1 else 0


# ------------------------------------------------------------------------------------------------------------- tests
def test_light_imports_do_not_load_duckdb():
    code = ("import sys; " + "; ".join(f"import {m}" for m in LIGHT) + "; "
            "from sf_assistant.engine.types import RecordsUnavailable; from sf_assistant.config import load_records_config; "
            "print('duckdb' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_package_does_not_import_the_other_pipeline():
    pat = re.compile(r"^\s*(from|import)\s+(src|orchestrator|salesforce_knowledge|brain)(\.|\s|$)", re.M)
    for p in SRC.rglob("*.py"):
        assert not pat.search(p.read_text(encoding="utf-8")), p.name


def test_every_package_module_has_a_layer():
    missing = sorted(m for m in MODULES if _layer(m) is None)
    assert not missing, f"add these to LAYER (tests/test_layering.py): {missing}"


def test_imports_point_down_the_layers():
    bad = []
    for me, (path, package) in MODULES.items():
        for target, line in _imports(path, package):
            if target.split(".")[0] in NOTEBOOK:
                bad.append(f"{me}:{line} -> {target} (notebook package)")
            elif target.startswith("sf_assistant.") and (_layer(target) is None or _layer(target) > _layer(me)):
                bad.append(f"{me}:{line} -> {target}")
    assert not bad, bad


def test_package_never_imports_the_web_page():
    """web/ (server, codes, pipeline) is the top layer: it imports the package, never the other way round."""
    web_names = {"web"} | {p.stem for p in WEB.glob("*.py")}
    bad = [f"{me}:{line} -> {target}" for me, (path, package) in MODULES.items()
           for target, line in _imports(path, package) if target.split(".")[0] in web_names]
    assert not bad, bad


def test_no_import_cycles_at_import_time():
    graph: dict[str, set[str]] = {m: set() for m in MODULES}
    for me, (path, package) in MODULES.items():
        for target, _ in _imports(path, package, runtime_only=True):
            parts = target.split(".")
            # importing a.b.c also runs a/__init__ and a/b/__init__
            graph[me].update(c for c in (".".join(parts[:i]) for i in range(1, len(parts) + 1)) if c in MODULES and c != me)
    state: dict[str, int] = {}   # 1 = on the current path, 2 = done
    cycles: list[list[str]] = []

    def walk(m: str, path: list[str]) -> None:
        state[m] = 1
        for n in sorted(graph[m]):
            if state.get(n) == 1:
                cycles.append(path[path.index(n):] + [n])
            elif n not in state:
                walk(n, path + [n])
        state[m] = 2

    for m in sorted(graph):
        if m not in state:
            walk(m, [m])
    assert not cycles, cycles


def test_knowledge_side_never_imports_record_code():
    knowledge = {".".join(["sf_assistant", *Path(rel).with_suffix("").parts]) for rel in KNOWLEDGE_SIDE}
    assert knowledge <= set(MODULES), sorted(knowledge - set(MODULES))
    record = {m for m, (path, _) in MODULES.items()
              if m not in knowledge and m not in WIRING and path.name != "__init__.py"}
    assert {"sf_assistant.engine.catalog", "sf_assistant.tools.record_base", "sf_assistant.agent.merge"} <= record
    bad = [f"{me}:{line} -> {target}" for me in sorted(knowledge)
           for target, line in _imports(*MODULES[me])
           if target in record or target.split(".")[0] in NOTEBOOK or target.startswith("sf_assistant.engine")]
    assert not bad, bad


#: what reads a record session's objects by duck typing (no import needed): the catalog, the evidence, the triage
_RECORD_SESSION_RE = re.compile(r"\.(catalog|evidence|triage)\b|question_people")


def test_knowledge_side_never_reads_record_session_objects():
    """The rule in substance, not only in imports (review 2026-10-21): the knowledge side never reaches into a record
    session (``session.tools.catalog``, ``rec.evidence``, ``rec.triage``, ``question_people()``); that wiring lives in
    ``sf_assistant.pipeline`` (``question_spelling``, ``spelling_pairs``, ``spelling_final``)."""
    bad = [f"{rel}:{no}" for rel in KNOWLEDGE_SIDE
           for no, line in enumerate((SRC / rel).read_text(encoding="utf-8").splitlines(), 1)
           if _RECORD_SESSION_RE.search(line)]
    assert not bad, bad
