#!/usr/bin/env python3
"""Write docs/prompts.md from the prompt files prompts/<id>.md (read through sf_assistant.prompts).

    .venv/bin/python scripts/gen_prompts_doc.py           # write docs/prompts.md
    .venv/bin/python scripts/gen_prompts_doc.py --check   # exit 1 when docs/prompts.md is not current

The page lists every prompt id with its version, the Python name and module that bind it, its placeholders, its size
and the sha256 of its text. Writing refuses a prompt whose text changed while its version stayed the same as on the
current page ("raise it when the text changes", sf_assistant.prompts): raise `version:` in the file, then run this
again. It reads only the prompt files: no knowledge file, no warehouse, no model.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

from sf_assistant import prompts as P

DOC = Path(__file__).resolve().parents[1] / "docs" / "prompts.md"
OLD_PREFIX = "notebooks/object_knowledge_eval/"
STAGE2_MARK = "inline text until stage 2"
NEW_MARK = "new in the term resolver wiring"   # texts the spelling help added (docs/resolver_integration.md)
GROUPB_MARK = "new in Group B"   # texts the Group B people fixes added (docs/people.md)
EVALFIX_MARK = "new in the evaluation fixes of 2026-10-22"   # texts those fixes added (docs/people.md)
COMPONENTS_MARK = "new in the flow components wiring"   # texts the flow components wiring added (docs/tools.md)
VALUES_MARK = "new in the value spots of 2026-10-28"   # texts the value spots added (docs/value_spots.md)
MERGE_MARK = "new in the lean-mode merge by code"   # the merge by code's label line (docs/runbook.md)
TABLES_MARK = "new in the code-built tables of 2026-10-29"   # the texts of the rows by code (docs/runbook.md)
CHARTS_MARK = "new in the charts of 2026-11-03"   # the note and the words of a chart (docs/runbook.md)
FOLLOWUP_MARK = "new in the ask service of 2026-11-04"   # the follow-up rewrite (docs/runbook.md)
#: a table row of the page: | n | `id` | version | ... | `sha12` |
_ROW_RE = re.compile(r"^\| \d+ \| `([a-z][a-z0-9_]*)` \| (\d+) \|.*\| `([0-9a-f]{12})` \|$")


def sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def fingerprint(prompts: dict) -> str:
    """One sha256 over every (id, version, text) in id order: changes when any prompt changes."""
    h = hashlib.sha256()
    for pid in sorted(prompts):
        p = prompts[pid]
        h.update(f"{pid}\0{p.version}\0".encode("utf-8") + p.text.encode("utf-8") + b"\0")
    return h.hexdigest()


def _code(value: str, table: bool = True) -> str:
    """``value`` as a markdown code span (``table``: safe inside a table cell, ``|`` escaped)."""
    value = value.replace("|", "\\|") if table else value
    ticks = max((len(m) for m in re.findall(r"`+", value)), default=0) + 1
    pad = " " if value.startswith("`") or value.endswith("`") else ""
    return f"{'`' * ticks}{pad}{value}{pad}{'`' * ticks}"


def _cell(value: str) -> str:
    return value.replace("|", "\\|")


def recorded(doc_text: str) -> dict[str, tuple[int, str]]:
    """``{id: (version, sha12)}`` of the rows of a prompts.md page."""
    out = {}
    for line in doc_text.splitlines():
        m = _ROW_RE.match(line)
        if m:
            out[m.group(1)] = (int(m.group(2)), m.group(3))
    return out


def version_problems(doc_text: str | None, prompts: dict) -> list[str]:
    """Prompts whose text differs from the page's while the version is the same (or lower)."""
    if not doc_text:
        return []
    problems = []
    for pid, (version, digest) in sorted(recorded(doc_text).items()):
        p = prompts.get(pid)
        if p is None:
            continue
        if p.version < version:
            problems.append(f"prompts/{pid}.md: version {p.version} is lower than {version} on {DOC.name}")
        elif p.version == version and sha12(p.text) != digest:
            problems.append(f"prompts/{pid}.md: the text changed but version is still {version}: raise it")
    return problems


def _origin(p) -> str:
    source = p.meta.get("source", "")
    return source[len(OLD_PREFIX):] if source.startswith(OLD_PREFIX) else source


def prompts_markdown(prompts: dict | None = None) -> str:
    prompts = P.all_prompts() if prompts is None else prompts
    written = {pid for pid, p in prompts.items() if STAGE2_MARK in p.meta.get("source", "")}
    added = {pid for pid, p in prompts.items() if p.meta.get("source", "").startswith(NEW_MARK)}
    groupb = {pid for pid, p in prompts.items() if p.meta.get("source", "").startswith(GROUPB_MARK)}
    evalfix = {pid for pid, p in prompts.items() if p.meta.get("source", "").startswith(EVALFIX_MARK)}
    components = {pid for pid, p in prompts.items() if p.meta.get("source", "").startswith(COMPONENTS_MARK)}
    values = {pid for pid, p in prompts.items() if p.meta.get("source", "").startswith(VALUES_MARK)}
    merge = {pid for pid, p in prompts.items() if p.meta.get("source", "").startswith(MERGE_MARK)}
    tables = {pid for pid, p in prompts.items() if p.meta.get("source", "").startswith(TABLES_MARK)}
    charts = {pid for pid, p in prompts.items() if p.meta.get("source", "").startswith(CHARTS_MARK)}
    followups = {pid for pid, p in prompts.items() if p.meta.get("source", "").startswith(FOLLOWUP_MARK)}
    changed = sorted(pid for pid, p in prompts.items() if p.version > 1
                     and pid not in written | added | groupb | evalfix | components | values | merge | tables | charts
                     | followups)
    constants = (len(prompts) - len(written) - len(added) - len(groupb) - len(evalfix) - len(components)
                 - len(values) - len(merge) - len(tables) - len(charts)
                 - len(followups))   # moved from the notebook folder in stage 1
    modules: dict[str, list[str]] = {}
    for pid, p in prompts.items():
        modules.setdefault(p.meta.get("module", "(no module)"), []).append(pid)
    lines = [
        "# Prompts",
        "",
        "Generated by `scripts/gen_prompts_doc.py` from `prompts/*.md`. Do not edit this file by hand: change the "
        "prompt file (and raise its `version`), then run `.venv/bin/python scripts/gen_prompts_doc.py`. "
        "`tests/test_prompts_doc.py` fails when this file is not current, and the script refuses to write it when a "
        "text changed while its version stayed the same.",
        "",
        "Every text the models get from a prompt constant, and every line code writes into a nudge, the FIELD CHECK, "
        "the lookup footer, the Source line or the other-people line, is one file `prompts/<id>.md`: a front matter "
        "block (`id` = the file name, `version`, `name` and `module` = the Python name that binds the text and its "
        "module, `source`, `placeholders`, sometimes `fill`) and then the exact text. `sf_assistant.prompts` loads "
        "and checks each file once per process (id, integer version, placeholders = the text's `{name}` and "
        "`<<NAME>>` tokens, LF only, no byte order mark); the module binds the text at import to the same name the "
        "code used before the files existed. `{name}` tokens are filled by `knowledge_prompts.render` or "
        "`combine_user_message`, `<<NAME>>` tokens by `prompts.fill` (one pass: a value is never filled again). "
        "`tests/test_prompt_files.py` checks every file, its binding, that every text is byte for byte the notebook "
        "folder's (a deliberately changed one: the notebook text with exactly the edits `tests/prompt_changes.py` "
        "lists, version raised) and that every code-written line equals the old f-string.",
        "",
        f"{len(prompts)} files: "
        f"{constants} "
        f"prompt constants (moved from `{OLD_PREFIX}` in stage 1" + (f"; changed since, version raised: "
                                                                     f"{', '.join(f'`{pid}`' for pid in changed)}"
                                                                     if changed else "")
        + ")" + (", " if added or groupb or evalfix or components or values or merge or tables or charts or followups
                 else " and ")
        + f"{len(written)} lines "
        "code writes (inline f-strings until stage 2)"
        + ((", " if groupb or evalfix or components or values or merge or tables or charts or followups else " and ")
           + f"{len(added)} texts of the spelling help (new in the term resolver wiring, "
           "`docs/resolver_integration.md`)" if added else "")
        + ((", " if evalfix or components or values or merge or tables or charts or followups else " and ")
           + f"{len(groupb)} texts of the Group B people fixes (`docs/people.md`)" if groupb else "")
        + ((", " if components or values or merge or tables or charts or followups else " and ")
           + f"{len(evalfix)} texts of the evaluation fixes of 2026-10-22 (fix 5, `docs/people.md`)" if evalfix else "")
        + ((", " if values or merge or tables or charts or followups else " and ") + f"{len(components)} text of the "
           "flow components wiring (the knowledge agent's components note, `docs/tools.md`)" if components else "")
        + ((", " if merge or tables or charts or followups else " and ") + f"{len(values)} texts of the value spots of "
           "2026-10-28 (the records a question names: the record agent's hint section and its NO_RECORDS pushback, "
           "`docs/value_spots.md`)" if values else "")
        + ((", " if tables or charts or followups else " and ") + f"{len(merge)} texts of the lean-mode merge by code "
           "(its label line before the field-meaning bullets after a list and the line that says what was counted, "
           "`docs/runbook.md`)"
           if merge else "")
        + ((", " if charts or followups else " and ") + f"{len(tables)} texts of the code-built tables (latency plan "
           "step 3: the record agent's note, the merge call's note, the table-line message and the words of the rows, "
           "`docs/runbook.md`)" if tables else "")
        + ((", " if followups else " and ") + f"{len(charts)} texts of the charts of 2026-11-03 (the record agent's "
           "note on a chart question and the words code writes on a chart and in its reason line, `docs/runbook.md`)"
           if charts else "")
        + (f" and {len(followups)} text of the ask service of 2026-11-04 (the follow-up rewrite, `docs/runbook.md`)"
           if followups else "")
        + ".",
        "",
        f"Prompt set fingerprint (sha256 over every id, version and text, in id order): `{fingerprint(prompts)}`. "
        "`sf_assistant.prompts.versions()` gives `{id: version}` for answer metadata.",
        "",
        "Columns: **v** = `version`; **placeholders** = the tokens the text holds; **size** = lines / characters of "
        "the text; **sha256** = the first 12 hex digits of the sha256 of the text (UTF-8); **origin** = `source` "
        f"without the `{OLD_PREFIX}` prefix.",
        "",
        "| Module | Files |",
        "|---|---|",
    ]
    for module in sorted(modules):
        lines.append(f"| `{module}` | {len(modules[module])} |")
    n = 0
    fills = []
    for module in sorted(modules):
        lines += ["", f"## `{module}`", "",
                  "| # | id | v | bound name | placeholders | size | origin | sha256 |",
                  "|---|---|---|---|---|---|---|---|"]
        for pid in sorted(modules[module]):
            n += 1
            p = prompts[pid]
            tokens = " ".join(_code(t) for t in p.placeholders) or "-"
            size = f"{p.text.count(chr(10)) + 1 if p.text else 0} / {len(p.text):,}"
            lines.append(f"| {n} | `{pid}` | {p.version} | `{p.meta.get('name', '')}` | {tokens} | {size} | "
                         f"{_cell(_origin(p))} | `{sha12(p.text)}` |")
            if p.meta.get("fill"):
                fills.append((pid, p.meta["fill"]))
    if fills:
        lines += ["", "## How the special files are filled", "",
                  "Most texts are bound as they are. These say how their text is used (the `fill` header):", ""]
        lines += [f"- `{pid}`: {_code(text, table=False)}" for pid, text in fills]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="only check that docs/prompts.md is current")
    a = ap.parse_args(argv)
    prompts = P.all_prompts()
    text = prompts_markdown(prompts)
    current = DOC.read_text(encoding="utf-8") if DOC.is_file() else None
    if a.check:
        if current != text:
            print(f"{DOC} is not current: run scripts/gen_prompts_doc.py", file=sys.stderr)
            return 1
        print(f"{DOC} is current")
        return 0
    problems = version_problems(current, prompts)
    if problems:
        print("not written:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    if current == text:
        print(f"{DOC} is already current")
        return 0
    DOC.parent.mkdir(parents=True, exist_ok=True)
    DOC.write_text(text, encoding="utf-8", newline="\n")
    print(f"wrote {DOC} ({len(prompts)} prompts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
