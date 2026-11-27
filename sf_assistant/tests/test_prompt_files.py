"""prompts/<id>.md and sf_assistant.prompts: the files parse, every file is bound by the code under the name its
header gives, and every bound text (and every render or fill of it) is byte for byte the notebook folder's, or, for
the prompts ``tests/prompt_changes.py`` lists (deliberate changes, version raised), the notebook text with exactly the
listed edits.

``tests/golden/prompts.json`` holds the sha256 of each prompt constant of notebooks/object_knowledge_eval (okeval,
okrec) and of renders with fixed fake inputs, made by the restructure exporter. When that folder is present,
:func:`test_prompts_equal_the_notebook_constants` also compares the texts with it directly (in a subprocess that
writes no .pyc into it).
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from prompt_changes import BY_NAME, PROMPT_CHANGES, apply, revert

from sf_assistant import prompts as P
from sf_assistant.agent import knowledge_agent as KA
from sf_assistant.agent import knowledge_prompts as KP
from sf_assistant.agent import record_prompts as RP

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "sf_assistant"
GOLDEN = json.loads((ROOT / "tests" / "golden" / "prompts.json").read_text(encoding="utf-8"))
OLD_DIR = Path(os.environ.get("SFA_OLD_DIR") or ROOT.parent / "notebooks" / "object_knowledge_eval")
SPECIAL = {"question_gen_system", "question_category_definitions", "combine_user"}


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Status:
    def __init__(self, origin, newest_sync_utc, today, time_zone):
        self.origin, self.newest_sync_utc, self.today, self.time_zone = origin, newest_sync_utc, today, time_zone


# ------------------------------------------------------------------------------------------------------ the files
def test_every_file_parses_and_is_well_formed():
    files = sorted(P.PROMPTS_DIR.glob("*.md"))
    # 52 prompt constants (stage 1) + 16 lines code writes (stage 2) + 3 texts of the spelling help (term resolver
    # wiring: spelling_hints, read_as_line, read_as_person_line) + 9 texts of the Group B people fixes (docs/people.md:
    # records_relation_word_message, records_direction_*, records_status_line; review fixes: records_paths_message,
    # records_child_copy_message, records_child_copy_held_note) + 4 of the evaluation fixes of 2026-10-22 (fix 5:
    # records_relation_lookup_message, _source_message, _unclear_message, _counted_note) + 1 of the flow components
    # wiring (stage 4 of the flow build, 2026-10-24: knowledge_components_note) + 2 of the value spots of 2026-10-28
    # (docs/value_spots.md: records_value_hints, records_value_no_records_message) + 2 of the lean-mode merge by code
    # (code_merge_label; step-2 fixes of 2026-10-30: code_merge_counted) + 4 of the code-built tables (latency plan
    # step 3: records_tables_note, combine_tables_note, records_tables_marker_message, records_table_words) + 2 of the
    # charts of 2026-11-03 (records_chart_note, records_chart_words) + 1 of the ask service of 2026-11-04
    # (followup_rewrite) + 1 of the placement fixes of 2026-11-16 (business_terms) + 1 of the business-term contracts
    # of 2026-11-24 (term_contracts) + 6 of the prerequisites of 2026-11-25 (small_talk, display_words,
    # records_display_note, records_tools_forced_message, records_all_failed_message, records_clarify) + 1 of the
    # Salesforce conversation memory of 2026-11-25 (followup_resolve: the follow-up resolver's prompt and the lines
    # code writes) + 2 of its stage 2 (plan_line_words: the plan line's words, moved from agent/query_state.py;
    # memory_exec: the lines of a re-run plan and the note of a question about the last answer) + 2 of development A
    # of 2026-11-27 (records_list_note: the record agent's note on a list question; answer_cut_line: the line of an
    # answer cut at its token limit); the last 41 are new, so the notebook golden does not cover them
    assert len(files) == 109
    for path in files:
        raw = path.read_bytes()
        assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf"), path.name
        p = P.load(path.stem)
        assert p.id == path.stem and p.version >= 1
        assert sorted(p.placeholders) == sorted(set(P.TOKEN_RE.findall(p.text)))
        assert p.meta.get("name") and p.meta.get("module", "").startswith("sf_assistant."), path.name
    assert P.ids() == [f.stem for f in files]
    assert set(P.versions()) == set(P.ids())


def test_loader_reads_each_file_once():
    first = P.load("agent_system")
    assert P.load("agent_system") is first and P.text("agent_system") is first.text


@pytest.mark.parametrize("data,message", [
    ("id: x\n---\nt\n", "must start with a '---'"),
    ("---\nid: x\nversion: 1\nplaceholders: \"\"\nt\n", "no closing '---'"),
    ("---\nid: y\nversion: 1\nplaceholders: \"\"\n---\nt\n", "must equal the file name"),
    ("---\nid: x\nversion: one\nplaceholders: \"\"\n---\nt\n", "version must be a positive integer"),
    ("---\nid: x\nversion: 1\nplaceholders: \"\"\n---\nhello {name}\n", "do not match the text's tokens"),
    ("---\nid: x\nversion: 1\nplaceholders: \"<<A>>\"\n---\nno token\n", "do not match the text's tokens"),
    ("---\nid: x\nversion: 1\ncolour: red\n---\nt\n", "unknown front matter line"),
    ("---\nid: x\nversion: 1\n---\nt", "must end with one newline"),
    ("---\r\nid: x\r\nversion: 1\r\n---\r\nt\r\n", "carriage return"),
    ("﻿---\nid: x\nversion: 1\n---\nt\n", "byte order mark"),
])
def test_loader_refuses_malformed_files(data, message):
    with pytest.raises(P.PromptError, match=re.escape(message)):
        P.parse(data, "x")


def test_text_keeps_edge_whitespace_and_drops_one_final_newline():
    p = P.parse("---\nid: x\nversion: 2\nplaceholders: \"<<A>> {b}\"\n---\n\n\n lead <<A>> {b}\n\n", "x")
    assert p.text == "\n\n lead <<A>> {b}\n" and p.version == 2 and p.placeholders == ("<<A>>", "{b}")
    assert RP.KNOWLEDGE_RECORDS_NOTE.startswith("\n\n") and RP.RECORDS_LEAD_ZERO_NOTE.startswith(" ")


def test_fill_is_one_pass_and_needs_every_value():
    assert P.fill("<<A>> and <<B>>", A="<<B>>", B="b") == "<<B>> and b"
    with pytest.raises(KeyError, match="<<B>>"):
        P.fill("<<A>> <<B>>", A="a")


# ------------------------------------------------------------------------------------------------- the bindings
def test_every_file_is_bound_under_its_header_name():
    for pid, p in P.all_prompts().items():
        module = importlib.import_module(p.meta["module"])
        value = getattr(module, p.meta["name"])
        if pid == "question_gen_system":
            assert value == P.fill(p.text, CATEGORY_BLOCK=KP._CATEGORY_BLOCK, TOOL_BLOCK=KP._TOOL_BLOCK)
        elif pid == "question_category_definitions":
            assert "\n".join(f"- {k}: {v}" for k, v in value.items()) == p.text
        elif pid == "combine_user":
            assert callable(value)
        else:
            assert value == p.text and type(value) is str, pid


def test_code_loads_only_existing_files_and_every_file_is_loaded():
    used: set[str] = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("text", "load")
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "_prompt_files"):
                assert len(node.args) == 1 and isinstance(node.args[0], ast.Constant), f"{path.name}:{node.lineno}"
                used.add(node.args[0].value)
    assert used == set(P.ids())


#: The lines code writes (stage 2: they were inline f-strings of okrec/session.py, now prompts/<id>.md filled in one
#: pass by agent/nudges.py and agent/record_session.py). Each entry: the bound name, and the OLD expression with the
#: placeholders as arguments (copied from okrec/session.py; a joined list is a str there, so ``j`` marks those).
_OLD_LINES = {
    "RECORDS_OTHER_LOOKUP_FITS_NOTE": lambda WORD, LOOKUPS: f" The question's word '{WORD}' fits " + LOOKUPS + ".",
    "RECORDS_UNFIT_ZERO_SPARSE_NOTE": lambda FIELD, ROLE, OBJECT:
        f" The lookup whose role fits it, {FIELD}{ROLE}, is filled on almost no {OBJECT} records.",
    "RECORDS_OTHER_LOOKUP_FIRM_SPARSE_NOTE": lambda FIELD, OBJECT: f" {FIELD} is filled on almost no {OBJECT} records.",
    "RECORDS_DATE_COVERAGE_MAIN": lambda MAIN, OBJECT: f"{MAIN} (the first of {OBJECT}'s date_fields)",
    "RECORDS_DATE_COVERAGE_MAIN_UNKNOWN": lambda OBJECT: f"the first of {OBJECT}'s date_fields in describe_fields",
    "RECORDS_NO_DATA_LINE": lambda FIELDS: (f"The data does not record this: {FIELDS} {'is'} blank on every record in "
                                            f"the warehouse, so a 0 through {'it'} is no answer."),
    "RECORDS_NO_DATA_LINE_SEVERAL": lambda FIELDS: (f"The data does not record this: {FIELDS} {'are'} blank on every "
                                                    f"record in the warehouse, so a 0 through {'them'} is no answer."),
    "RECORDS_FIELD_CHECK_UNSYNCED": lambda WORD, OBJECT, FIELD, ROLE, PROXY, QUERY_ID: (
        f"The question's word '{WORD}' fits {OBJECT}.{FIELD}{ROLE}, which is "
        "documented but not synced to the warehouse: the data does not record it. Record counts on "
        f"{OBJECT} go through {PROXY} instead (a proxy, RECORD FACTS query {QUERY_ID})."),
    "RECORDS_FIELD_CHECK_LOOKUP": lambda OBJECT, OTHER_FIELD, OTHER_ROLE, FIELD, ROLE, RECORDS, OTHER_RECORDS, QUERY_ID: (
        f"The question's verb fits {OBJECT}.{OTHER_FIELD}{OTHER_ROLE}, which the knowledge answer names; the record "
        f"count used {OBJECT}.{FIELD}{ROLE}: {RECORDS}. With the same conditions, "
        f"{OTHER_FIELD} has {OTHER_RECORDS} (RECORD FACTS, query {QUERY_ID})."),
    "RECORDS_FOOTER_LOOKUP_USED": lambda LOOKUPS: "Lookup used: " + LOOKUPS + ".",
    "RECORDS_FOOTER_SAME_PERSON": lambda OBJECT, OTHERS: (f"Same person through other lookups of {OBJECT}{''} "
                                                          "(overlapping, not to be added): " + OTHERS + "."),
    "RECORDS_FOOTER_SAME_PERSON_FOR": lambda OBJECT, NAMES, OTHERS: (
        f"Same person through other lookups of {OBJECT}{' for ' + NAMES} (overlapping, not to be "
        "added): " + OTHERS + "."),
    "RECORDS_FOOTER_WORD_FITS": lambda WORD, LOOKUPS: (f"The question's word '{WORD}' fits " + LOOKUPS
                                                       + " under the same conditions (overlapping, not to be added)."),
    "RECORDS_FOOTER_NOT_SYNCED": lambda OBJECT, FIELD, ROLE, WORD, PROXY: (
        f"Not in the warehouse: `{OBJECT}.{FIELD}`{ROLE}, which the question's word "
        f"'{WORD}' fits, is documented but not synced, so the data does not record it; counts "
        f"through {PROXY} are a proxy."),
    "RECORDS_SOURCE_LINE": lambda ORIGIN, WHEN: f"\nSource: {ORIGIN}, synced {WHEN}."[1:],
    "RECORDS_OTHER_PEOPLE_LINE": lambda WORDS, OTHERS: (f"Other stored people named '{WORDS}': " + OTHERS
                                                        + "; this answer does not cover them."),
}
#: Placeholders that hold a joined list (a str in the old code); every other one may be any value.
_JOINED = {"LOOKUPS", "OTHERS", "NAMES", "FIELDS"}


def test_code_written_lines_are_the_old_f_strings():
    import inspect

    files = {p.meta["name"]: p for p in P.all_prompts().values() if "inline text until stage 2" in p.meta.get("source", "")}
    assert sorted(files) == sorted(_OLD_LINES) and len(files) == 16
    values = ["v", "<<OBJECT>>", "<<FIELD>> <<ROLE>>", "{facts} {}", 'é — 漢字 "q"', "", "\\1 \\g<0> $1", " lead "]
    others = [None, 0, 12, 2.5, True]
    for name, old in _OLD_LINES.items():
        p = files[name]
        assert p.meta["module"] == "sf_assistant.agent.record_prompts" and getattr(RP, name) == p.text
        params = list(inspect.signature(old).parameters)
        assert sorted(f"<<{x}>>" for x in params) == sorted(p.placeholders), name
        for i, v in enumerate(values + others):
            kw = {x: (values[(i + k) % len(values)] if x in _JOINED else
                      (v if k == 0 else values[(i + 2 * k) % len(values)])) for k, x in enumerate(params)}
            if all(x in _JOINED for x in params) and not isinstance(v, str):
                continue
            assert P.fill(getattr(RP, name), **kw) == old(**kw), (name, kw)


def test_combine_user_message_layout_is_the_old_f_string():
    def old(question, knowledge_answer, record_answer, facts, field_check=None):
        check = "".join(f"\n- {line}" for line in field_check or [] if line)
        return (f"QUESTION:\n{question}\n\nKNOWLEDGE ANSWER:\n{knowledge_answer}\n\nRECORD ANSWER:\n{record_answer}"
                + (f"\n\nFIELD CHECK:{check}" if check else "") + f"\n\nRECORD FACTS:\n{facts}")

    for args in (("q", "k", "r", "f"), ("{facts}", "{question}", "{field_check}", "{record_answer}"),
                 ("\\1 \\g<0>", "$1", "é", '{"a": 1}'), (None, 1, 2.5, ["x"])):
        for fc in (None, [], ["a", ""], ["{facts}", "two"]):
            assert RP.combine_user_message(*args, field_check=fc) == old(*args, field_check=fc)


# --------------------------------------------------------------------------------------- equal to the notebook's
def test_constants_match_the_golden_hashes():
    by_module = {"sf_assistant.agent.knowledge_prompts": KP, "sf_assistant.agent.record_prompts": RP,
                 "sf_assistant.agent.knowledge_agent": KA}
    assert len(GOLDEN["constants"]) == 50
    for c in GOLDEN["constants"]:
        value = revert(c["id"], getattr(by_module[c["module"]], c["name"]))   # a listed change: its edits undone
        assert (len(value), _h(value)) == (c["chars"], c["sha256"]), c["name"]
        if c["id"] != "question_gen_system":
            assert _h(revert(c["id"], P.text(c["id"]))) == c["sha256"], c["id"]
    assert _h(json.dumps(list(KP.CATEGORY_DEFINITIONS.items()), ensure_ascii=False)) == GOLDEN["category_definitions_sha256"]
    assert KP.TEMPLATE_FIELDS == GOLDEN["template_fields"] and KP.TOOL_NAMES == GOLDEN["tool_names"]


def test_renders_and_fills_match_the_golden_hashes():
    for r in GOLDEN["renders"]:
        assert _h(KP.render(r["template"], **dict(r["kw"]))) == r["sha256"], r["template"]
    for r in GOLDEN["records_system"]:
        assert _h(revert("records_agent_system", RP.render_records_system(_Status(**r["status"])))) == r["sha256"]
    for r in GOLDEN["lean"]:
        m, names = r["max_tools"], r["names"]
        assert (_h(RP.render_lean_system(m, names)), _h(RP.lean_require_tools_message(m)),
                _h(RP.lean_budget_error(m))) == (r["system"], r["require"], r["budget"])
    for r in GOLDEN["combine"]:
        assert _h(RP.combine_user_message(*r["args"], field_check=r["field_check"])) == r["sha256"]
    names = {c["id"]: c["name"] for c in GOLDEN["constants"]}
    for r in GOLDEN["fills"]:
        out = revert(r["id"], getattr(RP, names[r["id"]]))
        for token, value in r["values"].items():
            out = out.replace(token, value)
        assert _h(out) == r["sha256"], r["id"]


_DUMP_OLD = r"""
import json, re, sys
sys.dont_write_bytecode = True
sys.path.insert(0, sys.argv[1])
import okeval.agent as a, okeval.multi as m, okeval.prompts as k, okrec.prompts as r
src = open(r.__file__, encoding="utf-8").read()
names = [n for n in re.findall(r"^([A-Z][A-Z0-9_]+) = ", src, re.M) if isinstance(getattr(r, n), str)]
out = {"okeval.prompts." + n: getattr(k, n) for n in k._TEMPLATE_NAMES}
out.update({"okeval.agent.REQUIRE_TOOLS_MESSAGE": a.REQUIRE_TOOLS_MESSAGE,
            "okeval.agent.FORCE_ANSWER_MESSAGE": a.FORCE_ANSWER_MESSAGE,
            "okeval.multi.MULTI_AGENT_SYSTEM": m.MULTI_AGENT_SYSTEM,
            "okeval.prompts.CATEGORY_DEFINITIONS": list(k.CATEGORY_DEFINITIONS.items())})
out.update({"okrec.prompts." + n: getattr(r, n) for n in names})
sys.stdout.write(json.dumps(out))
"""


@pytest.mark.skipif(not (OLD_DIR / "okrec" / "prompts.py").is_file(), reason="notebook folder not present")
def test_prompts_equal_the_notebook_constants():
    out = subprocess.run([sys.executable, "-I", "-B", "-c", _DUMP_OLD, str(OLD_DIR)], capture_output=True, text=True,
                         check=True, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, encoding="utf-8")
    old = json.loads(out.stdout)
    mods = {"okeval.prompts": KP, "okeval.agent": KA, "okeval.multi": KP, "okrec.prompts": RP}
    for key, value in old.items():
        mod, name = key.rsplit(".", 1)
        new = getattr(mods[mod], name)
        if name == "CATEGORY_DEFINITIONS":
            assert [list(i) for i in new.items()] == value
        else:
            assert new == (apply(BY_NAME[name], value) if name in BY_NAME else value), key
    assert len(old) == 53   # 50 prompt texts, CATEGORY_DEFINITIONS, and okrec's NO_RECORDS / RECORDS words


def test_listed_prompt_changes_are_the_only_ones_and_raise_the_version():
    """A changed notebook prompt is listed in tests/prompt_changes.py with its edits and its raised version; every
    edit applies exactly once, and reverting it gives the golden hash (also without the notebook folder)."""
    golden = {c["id"]: c for c in GOLDEN["constants"]}
    for pid, change in PROMPT_CHANGES.items():
        p = P.load(pid)
        assert p.version == change["version"] > 1 and p.meta["name"] == change["name"], pid
        assert _h(revert(pid, p.text)) == golden[pid]["sha256"] and apply(pid, revert(pid, p.text)) == p.text, pid
        assert change["why"] and change["edits"], pid
    for pid, c in golden.items():   # every other notebook constant is unchanged, version 1
        if pid not in PROMPT_CHANGES and pid != "question_gen_system":
            assert P.load(pid).version == 1 and _h(P.text(pid)) == c["sha256"], pid
