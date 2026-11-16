"""docs/prompts.md is current (scripts/gen_prompts_doc.py), lists every prompt file, and the script refuses a text
change without a version change. Reads only prompts/*.md: no model, no warehouse, no private data."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from sf_assistant import prompts as P

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "gen_prompts_doc.py"
DOC = ROOT / "docs" / "prompts.md"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_prompts_doc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


G = _gen()


def test_docs_prompts_md_is_current():
    assert DOC.read_text(encoding="utf-8") == G.prompts_markdown(), "run: .venv/bin/python scripts/gen_prompts_doc.py"


def test_check_mode_exits_zero_when_current():
    # PYTHONPATH: this tree's package, also when the venv's editable install names another copy of the folder
    out = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, cwd=ROOT,
                         env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(ROOT / "src")})
    assert out.returncode == 0, out.stderr


def test_every_prompt_has_one_row_with_its_version_and_hash():
    rows = G.recorded(DOC.read_text(encoding="utf-8"))
    prompts = P.all_prompts()
    assert sorted(rows) == P.ids()
    for pid, p in prompts.items():
        assert rows[pid] == (p.version, G.sha12(p.text)), pid
    assert G.fingerprint(prompts) in DOC.read_text(encoding="utf-8")


def _fake(version: int, text: str):
    return P.Prompt(id="x_prompt", version=version, text=text, placeholders=(), meta={
        "name": "X_PROMPT", "module": "sf_assistant.agent.record_prompts", "source": "fake"})


def test_a_changed_text_needs_a_raised_version():
    page = G.prompts_markdown({"x_prompt": _fake(1, "old text")})
    assert G.recorded(page) == {"x_prompt": (1, G.sha12("old text"))}
    assert G.version_problems(page, {"x_prompt": _fake(1, "old text")}) == []
    assert G.version_problems(page, {"x_prompt": _fake(2, "new text")}) == []
    assert G.version_problems(None, {"x_prompt": _fake(1, "new text")}) == []
    assert G.version_problems(page, {}) == []                       # a removed prompt is no problem
    same = G.version_problems(page, {"x_prompt": _fake(1, "new text")})
    assert len(same) == 1 and "raise it" in same[0]
    lower = G.version_problems(G.prompts_markdown({"x_prompt": _fake(3, "t")}), {"x_prompt": _fake(2, "t")})
    assert len(lower) == 1 and "lower" in lower[0]


def test_table_cells_survive_pipes_backticks_and_angle_tokens():
    assert G._code("a|b") == "`a\\|b`"
    assert G._code("x `y` z") == "``x `y` z``"
    assert G._code("`edge`") == "`` `edge` ``"
    page = G.prompts_markdown({"x_prompt": P.Prompt(id="x_prompt", version=1, text="<<A>> | {b}",
                                                    placeholders=("<<A>>", "{b}"), meta={"name": "X", "module": "m"})})
    row = next(line for line in page.splitlines() if line.startswith("| 1 |"))
    assert "`<<A>>`" in row and "`{b}`" in row and G.recorded(page)["x_prompt"][0] == 1
