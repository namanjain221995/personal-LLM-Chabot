"""sf_assistant.tools.registry: the one definition of every tool, the tool lists the models get (equal to the notebook
folder's registries, JSON for JSON, in order) and docs/tools.md (current).

``tests/golden/tool_lists.json`` holds, for the notebook folder's okeval / okrec registries on fake data only, the tool
names, their value types and the sha256 of ``json.dumps(list_tools())`` and ``json.dumps(openai_tools())``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import warehouse_fixture as F  # noqa: E402
from prompt_changes import notebook_tools  # noqa: E402

from sf_assistant.agent.lean import LeanRegistry  # noqa: E402
from sf_assistant.knowledge.search import KnowledgeIndex  # noqa: E402
from sf_assistant.knowledge.store import ObjectKnowledge, knowledge_files  # noqa: E402
from sf_assistant.tools import registry as R  # noqa: E402
from sf_assistant.tools.knowledge_tools import MultiToolRegistry, ToolRegistry  # noqa: E402
from sf_assistant.tools.record_tools import RecordTools  # noqa: E402

GOLDEN = json.loads((ROOT / "tests" / "golden" / "tool_lists.json").read_text(encoding="utf-8"))
FIXTURE = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"


def _h(x) -> str:
    return hashlib.sha256(json.dumps(x, ensure_ascii=False).encode("utf-8")).hexdigest()


def _lists(reg) -> dict:
    return {"tools": [[k, type(v).__name__] for k, v in reg.tools.items()],
            "list_tools_sha256": _h(reg.list_tools()), "openai_tools_sha256": _h(reg.openai_tools())}


def _single(kn: ObjectKnowledge) -> ToolRegistry:
    return ToolRegistry(kn, KnowledgeIndex(kn.chunks()))


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    """The synthetic knowledge files of the fake warehouse fixture: (file names, ObjectKnowledge list)."""
    files = knowledge_files(str(F.make_knowledge(tmp_path_factory.mktemp("kn"))))
    return [Path(p).name for p in files], [ObjectKnowledge.load(p) for p in files]


@pytest.fixture(scope="module")
def fixture_knowledge():
    return ObjectKnowledge.load(str(FIXTURE))


# --------------------------------------------------------------------------------------------- equal to the notebook's
def test_knowledge_tool_lists_equal_the_notebook_registries(synthetic, fixture_knowledge):
    names, kns = synthetic
    single, multi = _single(fixture_knowledge), MultiToolRegistry(kns)
    assert _lists(single) == GOLDEN["single_fixture"]
    assert {kn.api_name: _lists(_single(kn)) for kn in kns} == GOLDEN["single_synthetic"]
    assert {"files": names, **_lists(multi)} == GOLDEN["multi_synthetic"]
    assert {"files": names[::-1], **_lists(MultiToolRegistry(kns[::-1]))} == GOLDEN["multi_synthetic_reversed"]
    assert _lists(MultiToolRegistry([fixture_knowledge])) == GOLDEN["multi_fixture"]
    assert _lists(LeanRegistry(single)) == GOLDEN["lean_single"]
    assert _lists(LeanRegistry(multi)) == GOLDEN["lean_multi"]
    assert _lists(LeanRegistry(multi, max_calls=10**6)) == GOLDEN["lean_multi_unbounded"]


def test_record_tool_list_equals_the_notebook_registry(tmp_path):
    rt = F.open_fixture(tmp_path)
    try:
        # the notebook's lists with the one deliberate change (placement fixes, 2026-11-16) taken out
        assert {"tools": [[k, type(v).__name__] for k, v in rt.tools.items()],
                "list_tools_sha256": _h(notebook_tools(rt.list_tools())),
                "openai_tools_sha256": _h(notebook_tools(rt.openai_tools()))} == GOLDEN["records"]
        assert _lists(rt) != GOLDEN["records"]   # ...and that change is there
        assert R.record_tool_list() == rt.list_tools()
        assert R.openai_tool_list(R.RECORD_TOOLS) == rt.openai_tools()
        for name, tool in rt.tools.items():   # the registry's own objects, bound to this instance
            assert tool.input_schema is R.SCHEMAS[name][1] and tool.description is R.SCHEMAS[name][0]
            assert tool.handler.__self__ is rt and tool.handler.__func__ is getattr(RecordTools, R.RECORD_HANDLERS[name])
    finally:
        rt.close()


# ------------------------------------------------------------------------------------ registry = the class surfaces
def test_registry_lists_equal_the_registries(synthetic, fixture_knowledge):
    _, kns = synthetic
    for kn in [fixture_knowledge, *kns]:
        reg = _single(kn)
        sections = reg.index.sections()
        assert R.knowledge_tool_list(kn.api_name, sections) == reg.list_tools()
        assert R.openai_tool_list(R.knowledge_tool_defs(kn.api_name, sections)) == reg.openai_tools()
    multi = MultiToolRegistry(kns)
    first = kns[0]
    assert R.multi_tool_list(sorted(kn.api_name for kn in kns), first.api_name,
                             KnowledgeIndex(first.chunks()).sections()) == multi.list_tools()
    assert R.mcp_tool_list(_single(first).tools.values()) == _single(first).list_tools()


def test_every_definition_names_a_method_of_its_registry():
    one = R.knowledge_tool_defs("X__c", ["fields"])
    assert [d.name for d in one] == list(dict.fromkeys(d.name for d in one)) and len(one) == 15
    assert all(callable(getattr(ToolRegistry, d.handler, None)) for d in one)
    own = R.multi_tool_defs(["A__c", "X__c"])
    assert [d.name for d in own] == ["list_objects", "search_all_knowledge", "find_links_to_object"]
    per = [R.multi_object_tool(t, ["A__c", "X__c"]) for t in R.mcp_tool_list(one)]
    assert all(callable(getattr(MultiToolRegistry, d.handler, None)) for d in own + per)
    assert not {d.name for d in own} & {d.name for d in per}
    assert all(d.input_schema["required"][0] == "object" and d.description.endswith(
        " Set `object` to the object API name.") for d in per)
    assert [d.name for d in R.RECORD_TOOLS] == list(R.TOOL_ORDER) and set(R.SCHEMAS) == set(R.TOOL_ORDER)
    assert set(R.RECORD_HANDLERS) == set(R.TOOL_ORDER)
    assert all(callable(getattr(RecordTools, d.handler, None)) for d in R.RECORD_TOOLS)
    assert all(t in R.TOOL_ORDER and a not in R.TOOL_ORDER for a, t in R.TOOL_ALIASES.items())


def test_bind_looks_up_the_handler_on_the_owner():
    class Owner:
        def _h(self, **kw):
            return {"result": kw}

    o = Owner()
    tool = R.ToolDef("t", "d", {"type": "object"}, "_h").bind(o)
    assert (tool.name, tool.description, tool.input_schema) == ("t", "d", {"type": "object"})
    assert tool.handler(a=1) == {"result": {"a": 1}} and tool.handler.__self__ is o


def test_registry_imports_no_engine_and_no_duckdb():
    code = ("import sys; import sf_assistant.tools.registry as r; r.tools_markdown(); r.record_tool_list(); "
            "print(sorted(m for m in sys.modules if m == 'duckdb' or m.startswith(('sf_assistant.engine', "
            "'sf_assistant.agent', 'sf_assistant.knowledge'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# ------------------------------------------------------------------------------------------------------------ docs
def test_docs_tools_md_is_current():
    doc = ROOT / "docs" / "tools.md"
    assert doc.read_text(encoding="utf-8") == R.tools_markdown(), "run: .venv/bin/python scripts/gen_tools_doc.py"


def test_doc_script_check_mode():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_tools_doc.py"), "--check"], capture_output=True,
                         text=True)
    assert out.returncode == 0, out.stderr


def test_docs_list_every_tool_once_per_set():
    text = R.tools_markdown()
    for d in R.RECORD_TOOLS:
        assert f"### `{d.name}`" in text and d.description in text
    assert text.count("### `get_field`") == 2 and text.count("### `list_objects`") == 1
    assert "`find_references` -> `about_people`" in text
    assert R.tools_markdown() == text   # deterministic: no date, time or knowledge file in it
