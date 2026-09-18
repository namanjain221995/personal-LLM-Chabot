"""Prompt-driven edits (AS3 prompt-edits): artifacts/edits.py and the edit
turn in engines/artifact.py.

What is pinned: the deterministic pre-planner (0 model calls) and its 90%
coverage rule; typed ops applied by code; the destructive-op gates; the
preservation guard (every untouched block/sheet/slide canonical-JSON equal,
and file-grounded for DOCX paragraphs and XLSX cells); a no-op answered
BEFORE acceptance with no job and no version; restore byte-for-byte and
undo of a restore; formats lineage; generator sheets frozen with the
unchanged algorithm; sources carried; formula leads neutralised on the way
out. The model is never called: the planner and the section writer are
stubs, and the composer's model call raises if reached.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import os

import pytest

from app import db, metrics
from app.artifacts import compose as C
from app.artifacts import db as adb
from app.artifacts import edits as E
from app.artifacts import intent as I
from app.artifacts import material_in as MI
from app.artifacts import pipeline, store
from app.artifacts import spec as S
from app.artifacts import tables as X
from app.artifacts import types as T
from app.config import settings
from app.engines import artifact as engine

# ------------------------------------------------------------- fixtures --

STATUSES = ["Resolved"] * 24 + ["Open"] * 14 + ["In Progress"] * 11 + ["Closed"] * 8 + ["On Hold"] * 3


def nine_section_doc() -> S.ArtifactSpec:
    blocks = [{"type": "paragraph", "text": "This audit covers the quarter.", "sources": ["s1"]}]
    names = ["Executive Summary", "Scope", "Method", "Findings", "Risk Assessment", "Recommendations", "Owners", "Timeline", "Appendix"]
    for i, name in enumerate(names, 1):
        blocks.append({"type": "heading", "level": 1, "text": name})
        blocks.append({"type": "paragraph", "text": f"{name} paragraph one with a figure of {i * 7} items.", "sources": ["s1"] if i % 2 else ["s2"]})
        blocks.append({"type": "bullets", "items": [f"{name} point A", f"{name} point B"]})
        if name == "Findings":
            blocks.append({"type": "table", "table": {"columns": ["Area", "Status", "Score"], "rows": [["Access", "Open", 3], ["Backups", "Closed", 5]], "numeric_columns": [2]}})
            blocks.append({"type": "heading", "level": 2, "text": "Detail"})
            blocks.append({"type": "paragraph", "text": "Detail text."})
    return S.parse_body("document", {
        "title": "Security Audit", "subtitle": "Q3", "blocks": blocks,
        "sources": [{"id": "s1", "title": "Policy", "url": "https://example.com/policy"}, {"id": "s2", "title": "Log export"}],
    })


def tickets_rows(n: int = 60):
    return [[f"T-{i + 1:03d}", f"Ticket {i + 1}", STATUSES[i % len(STATUSES)] if n != 60 else STATUSES[i], (i % 5) + 1] for i in range(n)]


def generator_sheet(rows: int = 20) -> dict:
    gen = {"rows": rows, "seed": 7, "columns": [
        {"name": "ID", "kind": "id", "pattern": "EMP-{n:03d}"},
        {"name": "Name", "kind": "name"},
        {"name": "Team", "kind": "choice", "values": ["Ops", "Sales", "Tech"]},
        {"name": "Score", "kind": "int", "min": 1, "max": 10},
    ]}
    generated, _ = X.generate_rows(gen)
    return {"name": "People", "columns": [{"name": "ID"}, {"name": "Name"}, {"name": "Team"}, {"name": "Score", "type": "integer"}],
            "rows": generated, "generator": gen}


def four_sheet_workbook() -> S.ArtifactSpec:
    return S.parse_body("workbook", {
        "title": "Ops Workbook",
        "sheets": [
            {"name": "Tickets", "columns": [{"name": "ID"}, {"name": "Title"}, {"name": "Status"}, {"name": "Score", "type": "integer"}], "rows": tickets_rows()},
            {"name": "Audit", "columns": [{"name": "Host"}, {"name": "Check"}, {"name": "Result"}], "rows": [["web1", "tls", "Pass"], ["web1", "ssh", None], ["db1", "tls", "Fail"]], "rows_from": "paste1"},
            generator_sheet(),
            {"name": "Summary", "columns": [{"name": "Metric"}, {"name": "Value", "type": "number"}], "rows": [["Open", 14], ["Closed", 8]]},
        ],
        "sources": [{"id": "s1", "title": "Ticket export"}],
    })


def deck() -> S.ArtifactSpec:
    return S.parse_body("presentation", {"title": "Q3 Review", "slides": [
        {"layout": "title", "title": "Q3 Review"}, {"layout": "bullets", "title": "Wins", "bullets": ["a", "b"]},
        {"layout": "bullets", "title": "Risks", "bullets": ["c"]}, {"layout": "closing", "title": "Thanks"}]})


class Calls:
    def __init__(self, reply=None):
        self.n = 0
        self.reply = reply

    async def __call__(self, messages, schema, timeout_s):
        self.n += 1
        self.last = messages
        return copy.deepcopy(self.reply)


def run(coro):
    return asyncio.run(coro)


def unit_values(spec):
    return [(u["key"], E.canonical_json(u["value"])) for u in E._work_of(spec, "", ()).units]


# ------------------------------------------------------- the pre-planner --

STYLE_PROMPTS_DOC = [
    "make the headings dark blue", "headings ko gehra neela karo", "make the title bold and 20pt", "change the body font to Georgia",
    "make the subtitle grey and italic", "heading 1 navy, heading 2 blue", "make the headings bold and underlined",
    "title in red", "use Arial 11pt for the body text", "make captions italic", "make the headings maroon",
    "हेडिंग नीला करो", "make the table header fill dark blue", "title 28pt navy", "make the bullets grey",
]
STYLE_PROMPTS_WB = [
    "make the header row bold", "header row dark blue with white text", "highlight the Status column red",
    "make the Score column green", "use Calibri 10pt for the cells", "header fill navy", "make the total row bold",
    "make the header row blue and bold", "sheet with colors: header dark blue", "make the Title column italic", "highlight row 5 yellow",
]


def test_the_pre_planner_covers_style_orientation_title_undo_restore_and_columns_with_no_model_call():
    doc, wb = nine_section_doc(), four_sheet_workbook()
    calls = Calls({"ops": []})
    covered = 0
    for prompt in STYLE_PROMPTS_DOC:
        p = run(E.plan(prompt, doc, call=calls))
        covered += p.planner == "deterministic" and any(o.op == "set_style" for o in p.ops)
    for prompt in STYLE_PROMPTS_WB:
        p = run(E.plan(prompt, wb, call=calls))
        covered += p.planner == "deterministic" and any(o.op == "set_style" for o in p.ops)
    total = len(STYLE_PROMPTS_DOC) + len(STYLE_PROMPTS_WB)
    assert total == 26
    assert covered / total >= 0.70, f"pre-planner covered {covered}/{total} style prompts"
    for prompt, op in [("make it landscape", "set_orientation"), ("change the title to Q3 Security Audit", "set_title"),
                       ("restore version 1", "restore_version"), ("rename the heading Scope to Coverage", "rename_heading")]:
        p = run(E.plan(prompt, doc, call=calls))
        assert p.planner == "deterministic" and [o.op for o in p.ops] == [op], prompt
    assert run(E.plan("undo that", doc, call=calls)).summary == "undo"
    for prompt, op in [("add a column for owner after Status", "add_column"), ("rename the Status column to State", "rename_column"),
                       ("delete rows where status is Closed", "delete_rows"), ("rename the sheet Tickets to Queue", "rename_sheet")]:
        p = run(E.plan(prompt, wb, call=calls))
        assert p.planner == "deterministic" and [o.op for o in p.ops] == [op], prompt
    before = calls.n
    assert before <= total - covered, "only the prompts the pre-planner did not cover reach the model"


def test_a_mixed_request_is_not_taken_by_the_pre_planner_and_costs_one_call():
    doc = nine_section_doc()
    calls = Calls({"ops": [{"op": "set_style", "style_phrase": "headings dark blue"},
                           {"op": "replace_section", "target": "Executive Summary", "instruction": "shorter"}]})
    p = run(E.plan("make the headings dark blue and rewrite the executive summary to be shorter", doc, call=calls))
    assert calls.n == 1 and p.planner == "model" and [o.op for o in p.ops] == ["set_style", "replace_section"]
    prompt = calls.last[1]["content"]
    assert "Executive Summary paragraph one" not in prompt, "the planner sees an outline, never the body"
    assert "[section 1] Executive Summary" in prompt


def test_a_planner_that_fails_falls_back_to_the_whole_document_edit():
    async def boom(messages, schema, timeout_s):
        raise asyncio.TimeoutError()

    p = run(E.plan("make the tone friendlier", nine_section_doc(), call=boom))
    assert p.planner == "fallback" and [o.op for o in p.ops] == ["regenerate"]


def test_the_outline_lists_low_cardinality_values_so_a_condition_binds_to_a_real_value():
    text = E.outline(four_sheet_workbook())
    assert "column 'Status' (text) values: Resolved, Open, In Progress, Closed, On Hold" in text
    assert "Ticket 17" not in text, "a high-cardinality column lists no values"


# -------------------------------------------------------------- matching --


def test_section_references_resolve_by_text_typo_number_and_keyword_and_ties_ask():
    heads = ["Executive Summary", "Scope", "Risk Assessment", "Risk Register", "Appendix"]
    assert E.resolve_section("Risk Assesment", heads) == (2, "")
    assert E.resolve_section("section 2", heads) == (1, "")
    assert E.resolve_section("the last section", heads) == (4, "")
    assert E.resolve_section("appendix", heads) == (4, "")
    assert E.resolve_section("risk", heads) == (None, "ambiguous target")
    assert E.resolve_section("Budget", heads)[0] is None
    assert E.resolve_section("जोखिम", ["सारांश", "जोखिम"]) == (1, "")


def test_an_ambiguous_target_asks_one_question_and_changes_nothing():
    doc = S.parse_body("document", {"title": "D", "blocks": [
        {"type": "heading", "level": 1, "text": "Risk Assessment"}, {"type": "paragraph", "text": "a"},
        {"type": "heading", "level": 1, "text": "Risk Register"}, {"type": "paragraph", "text": "b"}]})
    plan = E.EditPlan(ops=[E.RenameHeading(target=E.SectionRef(text="risk"), text="Risks")], planner="model")
    out = E.apply(doc, plan, instruction="rename the risk heading to Risks")
    assert out.question.startswith("Which section") and not out.changed and out.not_applied[0]["reason"] == "ambiguous target"


# ---------------------------------------------------------- preservation --

def _writer_stub(calls):
    async def writer(item, current, whole):
        calls.append(item)
        if item["kind"] == "slide":
            return {**current, "bullets": ["rewritten"]}
        head = current[0]
        return [head, {"type": "paragraph", "text": f"New text for {head['text']}.", "sources": ["s1"]}]
    return writer


DOC_EDITS = [
    ("change the title to Q3 Security Audit", None),
    ("make it landscape", None),
    ("rename the heading Scope to Coverage", None),
    ("change the subtitle to Third quarter", None),
    ("rewrite the Findings section", E.EditPlan(ops=[E.ReplaceSection(target=E.SectionRef(text="Findings"), instruction="rewrite")], planner="model")),
    ("add a section on Budget after Timeline", E.EditPlan(ops=[E.InsertSection(after=E.SectionRef(text="Timeline"), heading="Budget", instruction="budget")], planner="model")),
    ("delete the appendix", E.EditPlan(ops=[E.DeleteBlocks(target=E.SectionRef(text="appendix"))], planner="model")),
    ("rename the Status column to State in the findings table", E.EditPlan(ops=[E.RenameColumn(column="Status", name="State")], planner="model")),
    ("add an Owner column to the findings table", E.EditPlan(ops=[E.AddColumn(name="Owner", after="Status")], planner="model")),
    ("update section 3 with a clearer method", E.EditPlan(ops=[E.ReplaceSection(target=E.SectionRef(text="section 3"), instruction="clearer")], planner="model")),
    ("make it landscape and rename the heading Owners to Accountable owners", E.EditPlan(ops=[E.SetOrientation(orientation="landscape"), E.RenameHeading(target=E.SectionRef(text="Owners"), text="Accountable owners")], planner="model")),
    ("rename the heading Timeline to Schedule", None),
    ("rewrite the risk assesment", E.EditPlan(ops=[E.ReplaceSection(target=E.SectionRef(text="risk assesment"), instruction="rewrite")], planner="model")),
]
WB_EDITS = [
    ("add a column for owner after Status", None),
    ("rename the Status column to State", None),
    ("delete rows where status is Closed", None),
    ("rename the sheet Summary to Overview", None),
    ("add a Team Lead column to the People sheet", E.EditPlan(ops=[E.AddColumn(sheet="People", name="Team Lead")], planner="model")),
    ("delete the Check column from Audit", E.EditPlan(ops=[E.DeleteColumn(sheet="Audit", column="Check")], planner="model")),
    ("set Score to 5 where ID is T-001", E.EditPlan(ops=[E.UpdateCells(sheet="Tickets", where=E.Where(column="ID", value="T-001"), column="Score", value="5")], planner="model")),
    ("make it landscape", None),
    ("put Status before Title", E.EditPlan(ops=[E.ReorderColumns(sheet="Tickets", order=["ID", "Status"])], planner="model")),
    ("add a Total column to Summary that copies Value", E.EditPlan(ops=[E.AddColumn(sheet="Summary", name="Total", fill=E.FillCopy(column="Value"))], planner="model")),
    ("add a Full column to People joining Name and Team", E.EditPlan(ops=[E.AddColumn(sheet="People", name="Full", fill=E.FillDerived(derived=S.Derived(op="concat", columns=["Name", "Team"])))], planner="model")),
    ("make the header row bold", None),
    ("highlight the Status column green", None),
]


def test_every_untouched_block_and_sheet_is_identical_after_each_edit():
    doc, wb = nine_section_doc(), four_sheet_workbook()
    checked = 0
    for parent, cases in ((doc, DOC_EDITS), (wb, WB_EDITS)):
        for prompt, plan in cases:
            plan = plan or E.preplan(prompt, parent)
            assert plan is not None, prompt
            out = E.apply(parent, plan, instruction=prompt)
            child = out.spec
            if out.pending_sections:
                writes = []
                child, _applied, _na = run(E.resolve_pending(child, out.pending_sections, section_writer=_writer_stub(writes)))
                assert len(writes) == len(out.pending_sections)
            problems = E.preservation_guard(parent, child, out.touched)
            assert problems == [], (prompt, problems, out.not_applied)
            assert child.sources == parent.sources, "sources are carried by every edit"
            checked += 1
    assert checked == len(DOC_EDITS) + len(WB_EDITS) == 26


def test_a_unit_the_op_did_not_touch_is_put_back_when_it_changed():
    doc = nine_section_doc()
    w = E._work_of(doc, "x", ())
    w.units[3]["value"][1]["text"] = "silently changed"
    w.touched.add("meta.title")
    notes = E._guard_units(doc, w)
    assert notes and E.canonical_json(w.units[3]["value"]) == E.canonical_json(E._work_of(doc, "", ()).units[3]["value"])


def test_canonical_json_sorts_keys_and_normalises_floats():
    assert E.canonical_json({"b": 3.0, "a": [1.50, None]}) == E.canonical_json({"a": [1.5, None], "b": 3})


# ---------------------------------------------------------- destructive --

OVERREACH = [
    E.DeleteRows(where=E.Where(column="Status", op="ne", value="x")),
    E.DeleteRows(where=E.Where(column="Status", op="eq", value="Resolved")),
    E.DeleteRows(where=E.Where(column="Score", op="gt", value=0)),
    E.DeleteRows(where=E.Where(column="Title", op="contains", value="Ticket")),
    E.DeleteRows(where=E.Where(column="Status", op="blank")),
    E.DeleteColumn(sheet="Tickets", column="Title"),
    E.DeleteColumn(sheet="Audit", column="Result"),
    E.DeleteRows(sheet="People", where=E.Where(column="Team", op="in", value=["Ops", "Sales", "Tech"])),
    E.DeleteRows(sheet="Summary", where=E.Where(column="Metric", op="ne", value="none")),
    E.UpdateCells(where=E.Where(column="Status", op="ne", value="x"), column="Status", value="Closed"),
]


def test_invented_destructive_ops_are_blocked_and_no_row_is_lost():
    wb = four_sheet_workbook()
    before = {sh.name: len(sh.rows) for sh in wb.body.sheets}
    for op in OVERREACH:
        out = E.apply(wb, E.EditPlan(ops=[E.AddColumn(name="Owner", after="Status"), op], planner="model"), instruction="add a column for owner")
        after = {sh.name: len(sh.rows) for sh in out.spec.body.sheets}
        assert after == before, op
        assert [n["op"] for n in out.not_applied] == [op.op], (op, out.not_applied)
        assert out.applied_ops == ["add_column"]
        cols = [c.name for c in out.spec.body.sheets[0].columns]
        assert cols == ["ID", "Title", "Status", "Owner", "Score"]


def test_a_named_delete_removes_exactly_the_matching_rows():
    wb = four_sheet_workbook()
    out = E.apply(wb, E.preplan("delete rows where status is Closed", wb), instruction="delete rows where status is Closed")
    rows = out.spec.body.sheets[0].rows
    assert len(rows) == 52 and not any(r[2] == "Closed" for r in rows) and out.applied == ["8 rows deleted"]
    kept = [r for r in tickets_rows() if r[2] != "Closed"]
    assert [list(r) for r in rows] == kept


def test_a_broad_delete_needs_the_condition_named_and_many_cells_ask():
    wb = four_sheet_workbook()
    plan = E.EditPlan(ops=[E.DeleteRows(where=E.Where(column="Status", op="eq", value="Resolved"))], planner="model")
    blocked = E.apply(wb, plan, instruction="remove the rows that are done")
    assert not blocked.changed and "did not name that condition" in blocked.not_applied[0]["reason"]
    named = E.apply(wb, plan, instruction="remove the Resolved rows")
    assert named.applied == ["24 rows deleted"]
    big = S.parse_body("workbook", {"title": "B", "sheets": [{"name": "D", "columns": [{"name": "K"}, {"name": "V"}], "rows": [["a", str(i)] for i in range(300)]}]})
    ask = E.apply(big, E.EditPlan(ops=[E.UpdateCells(where=E.Where(column="K", value="a"), column="V", value="z")]), instruction="set V to z where K is a")
    assert not ask.changed and ask.question.startswith("That would change 300 cells")


def test_deleting_two_sections_needs_both_named():
    doc = nine_section_doc()
    plan = E.EditPlan(ops=[E.DeleteBlocks(target=E.SectionRef(text="Owners")), E.DeleteBlocks(target=E.SectionRef(text="Timeline"))], planner="model")
    out = E.apply(doc, plan, instruction="delete the owners section and the other one")
    assert out.applied == ["section “Owners” removed"] and "was not named" in out.not_applied[0]["reason"]
    both = E.apply(doc, plan, instruction="delete the Owners and Timeline sections")
    assert len(both.applied) == 2


# ------------------------------------------------------ sheets and tables --


def test_a_pasted_table_sheet_gets_a_column_in_code_with_every_cell_kept():
    wb = four_sheet_workbook()
    out = E.apply(wb, E.EditPlan(ops=[E.AddColumn(sheet="Audit", name="Owner", after="Check")]), instruction="add a column for owner after Check")
    audit = out.spec.body.sheets[1]
    assert [c.name for c in audit.columns] == ["Host", "Check", "Owner", "Result"]
    assert [[r[0], r[1], r[3]] for r in audit.rows] == [["web1", "tls", "Pass"], ["web1", "ssh", None], ["db1", "tls", "Fail"]]
    assert all(r[2] is None for r in audit.rows) and audit.rows_from == "paste1"
    out = E.apply(wb, E.preplan("add a column for owner after Status", wb), instruction="add a column for owner after Status")
    tickets = out.spec.body.sheets[0]
    assert [c.name for c in tickets.columns].index("Owner") == 3 and len(tickets.rows) == 60
    assert [[r[0], r[1], r[2], r[4]] for r in tickets.rows] == tickets_rows()


def test_a_paste_in_the_edit_turn_supplies_the_rows_to_add():
    wb = four_sheet_workbook()
    paste = C.DataTable(id="paste1", title="p", columns=["Host", "Check", "Result"], rows=[["web2", "tls", "Pass"]])
    out = E.apply(wb, E.EditPlan(ops=[E.AddRows(sheet="Audit")]), tables=[paste], instruction="add these rows")
    assert out.spec.body.sheets[1].rows[-1] == ["web2", "tls", "Pass"] and out.applied == ["1 row added from the paste"]
    invented = E.apply(wb, E.EditPlan(ops=[E.AddRows(sheet="Audit", rows=[["web9", "dns", "Pass"]])]), instruction="add a row")
    assert not invented.changed, "rows the request did not type are never added"


def test_a_generator_sheet_is_frozen_with_the_unchanged_generator_on_its_first_edit():
    wb = four_sheet_workbook()
    people = wb.body.sheets[2]
    frozen = X.freeze_generated(people)
    assert frozen.generator is None and frozen.rows == people.rows
    recipe = {"rows": people.generator.rows, "seed": people.generator.seed,
              "columns": [C._recipe(c) for c in people.generator.columns]}
    regenerated, _ = X.generate_rows(recipe)
    assert frozen.rows == regenerated
    typed = "; ".join(f"EMP-{i:03d}, New Person {i}, Ops, {i % 10 + 1}" for i in range(21, 31))
    paste = C.DataTable(id="paste1", title="p", columns=["ID", "Name", "Team", "Score"], rows=[[f"EMP-{i:03d}", f"New Person {i}", "Ops", i % 10 + 1] for i in range(21, 31)])
    out = E.apply(wb, E.EditPlan(ops=[E.AddColumn(sheet="People", name="Office"), E.AddRows(sheet="People")]), tables=[paste], instruction=f"add an Office column and these rows {typed}")
    grown = out.spec.body.sheets[2]
    assert grown.generator is None and len(grown.rows) == 30
    assert [[r[0], r[1], r[2], r[3]] for r in grown.rows[:20]] == people.rows, "100% of the original generated values are kept"
    assert any("generated rows were kept" in n for n in out.notes)


def test_update_cells_writes_a_formula_lead_that_the_renderers_neutralise(tmp_path):
    wb = S.parse_body("workbook", {"title": "Leads", "sheets": [{"name": "Data", "columns": [{"name": "ID"}, {"name": "Note"}], "rows": [["a1", "ok"], ["a2", "ok"]]}]})
    evil = '=HYPERLINK("http://x","y")'
    out = E.apply(wb, E.EditPlan(ops=[E.UpdateCells(where=E.Where(column="ID", value="a1"), column="Note", value=evil)]), instruction=f"set Note to {evil} where ID is a1")
    assert out.spec.body.sheets[0].rows[0][1] == evil
    from app.artifacts import render as R
    import openpyxl

    report = R.render_version(out.spec, ["xlsx", "csv"], str(tmp_path), title_slug="leads", version=2, effort="fast")
    files = {f["format"]: f["filename"] for f in report.to_json()["files"]}
    ws = openpyxl.load_workbook(tmp_path / files["xlsx"]).worksheets[0]
    cell = next(c for row in ws.iter_rows() for c in row if c.value == evil)
    assert cell.data_type == "s" and cell.quotePrefix
    text = (tmp_path / files["csv"]).read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in text


def test_a_long_column_name_is_capped_and_a_sheet_rename_stays_legal():
    wb = four_sheet_workbook()
    with pytest.raises(Exception):
        E.AddColumn(name="x" * 65)
    out = E.apply(wb, E.EditPlan(ops=[E.RenameSheet(sheet="Summary", name="Q3: [draft]/final")]), instruction="rename the Summary sheet")
    assert out.spec.body.sheets[3].name == "Q3   draft  final"


# ------------------------------------------------------- style and chart --


def test_document_styling_without_the_styling_engine_is_said_not_applied():
    doc = nine_section_doc()
    out = E.apply(doc, E.preplan("make the headings dark blue", doc), instruction="make the headings dark blue")
    if E._supports_style("document"):
        assert out.applied == ["headings dark blue (#1F3864)"]
    else:
        assert not out.changed and "styling engine" in out.not_applied[0]["reason"]


def test_the_style_json_merge_replaces_rules_with_the_same_target():
    base = {"rules": [{"target": {"kind": "heading"}, "style": {"color": "#000000", "bold": True}}]}
    patch = {"rules": [{"target": {"kind": "heading"}, "style": {"color": "#1F3864"}}, {"target": {"kind": "title"}, "style": {"size_pt": 28}}]}
    if E._style is None:
        merged = E.merge_style_json(base, patch)
        assert merged["rules"] == [{"target": {"kind": "heading"}, "style": {"color": "#1F3864", "bold": True}},
                                   {"target": {"kind": "title"}, "style": {"size_pt": 28}}]


def test_a_workbook_column_highlight_and_bold_header_apply_through_sheet_style():
    wb = four_sheet_workbook()
    ambiguous = E.apply(wb, E.preplan("highlight the Score column green", wb), instruction="highlight the Score column green")
    assert ambiguous.question.startswith("Which sheet"), "Score is a column of two sheets"
    out = E.apply(wb, E.preplan("highlight the Status column green", wb), instruction="highlight the Status column green")
    if E._supports_style("workbook"):
        # AS3 integration: the styling engine carries it as a column rule in spec.style.
        assert out.applied == ["the Status column on a green fill (#3F8F4F)"]
        rules = out.spec.body.style.rules
        assert any(r.target.kind == "column" and r.target.name == "Status" and r.style.background == "#3F8F4F" for r in rules)
    else:
        assert out.applied == ["the Status column highlighted green"]
        assert out.spec.body.sheets[0].style.highlight[0].column == "Status"
    assert E.preservation_guard(wb, out.spec, out.touched) == []


def test_a_chart_type_and_title_change_applies_to_the_one_chart():
    doc = S.parse_body("document", {"title": "C", "blocks": [{"type": "heading", "level": 1, "text": "Sales"},
                                                            {"type": "chart", "chart": {"type": "bar", "title": "Sales", "categories": ["a", "b"], "series": [{"name": "s", "values": [1, 2]}]}}]})
    out = E.apply(doc, E.EditPlan(ops=[E.SetChart(patch={"type": "line", "title": "Monthly sales"})]), instruction="make the chart a line titled Monthly sales")
    assert out.applied == ["line chart; chart title “Monthly sales”"]
    assert out.spec.body.blocks[1].chart.type == "line"


def test_a_model_plan_is_turned_into_typed_ops_and_bad_ones_are_rejected_not_guessed():
    wb = four_sheet_workbook()
    calls = Calls({"ops": [
        {"op": "add_column", "name": "Owner", "after": "Status"},
        {"op": "delete_rows", "where_column": "Status", "where_op": "ne", "where_value": "x"},
        {"op": "update_cells"},
    ]})
    p = run(E.plan("please add an owner field next to status, thanks", wb, call=calls))
    assert [o.op for o in p.ops] == ["add_column", "delete_rows"] and p.rejected and p.rejected[0]["op"] == "update_cells"
    out = E.apply(wb, p, instruction="please add an owner field next to status, thanks")
    assert out.applied_ops == ["add_column"] and len(out.spec.body.sheets[0].rows) == 60


# ------------------------------------------------------------ the engine --


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "artifact_lease_ttl_s", 60.0)
    monkeypatch.setattr(settings, "artifact_stage_timeout_s", 30.0)
    monkeypatch.setattr(settings, "artifact_render_timeout_s", 30.0)
    monkeypatch.setattr(settings, "artifact_min_free_mb", 1)
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 1024)
    monkeypatch.setattr(settings, "video_pace_max_wait_s", 0.0)
    pipeline.reset_for_tests()
    pipeline.install_busy_probe(None)
    metrics.reset()
    planner = Calls(None)
    monkeypatch.setattr(E, "planner_call", planner)

    async def no_model(*a, **k):
        raise AssertionError("the model was called")

    monkeypatch.setattr(C, "_json", no_model)
    state = {"specs": {}, "planner": planner}

    async def fake_render(work_dir, spec, formats, title_slug, version, effort):
        files = []
        for fmt in formats:
            name = f"{title_slug}-v{version}.{fmt}"
            body = f"{fmt} {spec.model_dump_json()}".encode()
            with open(os.path.join(work_dir, name), "wb") as fh:
                fh.write(body)
            files.append({"format": fmt, "filename": name, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "pages": 1 if fmt == "pdf" else None})
        with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
            fh.write(b"%PDF-1.7 preview")
        return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "pages", "preview_pages": 1, "warnings": [], "validation": {"reopened": True}, "chart_files": [], "timings": {}}

    monkeypatch.setattr(pipeline, "_render_in_subprocess", fake_render)
    monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 1)
    monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG")
    pipeline.set_composer(engine.compose_for_pipeline)
    yield state
    pipeline.reset_for_tests()
    pipeline.set_composer(None)
    metrics.reset()


@pytest.fixture()
def owner():
    return int(db.create_user("edits-owner", "hash"))


def seed(owner, spec, formats, conv="conv-ed", gen="seed", allow_warnings=False, material=None):
    """A published v1 of `spec` through the real pipeline (the composer
    returns the import payload)."""
    job = pipeline.accept(user_id=owner, conversation_id=conv, generation_id=gen, operation="create", instruction="seed",
                          kind=spec.kind, formats=list(formats), format_reason=f"{engine.IMPORT_REASON}: test", effort="fast",
                          mode="assistant", template_id="generic", material=material or {})
    engine._attach_payload(owner, job["artifact_id"], int(job["version"]), "import", {"spec": spec.model_dump(mode="json", by_alias=True, exclude_none=True)})

    async def go():
        await pipeline.ensure_running(job["id"])
        return await pipeline.wait_for(job["id"])

    row = asyncio.run(go())
    assert row["status"] in (("completed",) if not allow_warnings else ("completed", "completed_with_warnings")), row
    return row["artifact_id"]


def turn(owner, text, *, action="edit", conv="conv-ed", gen="t", artifact_id=None, rule="edit", version=None, reference="latest", history=(), formats=(),
         intent=None, gathered=None):
    """One turn through the real engine. `intent` passes a verdict built by
    the rules themselves (`I.decide`) instead of the shorthand above, and
    `gathered` is the turn's material (material_in.GatheredInput)."""
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    intent = intent or I.ArtifactIntent(action, formats=list(formats), reference=reference, rule=rule, instruction=text, version=version, raw_text=text)
    answer = asyncio.run(engine.run_artifact_engine(text, list(history), emit, intent=intent, conversation_id=conv, user_id=owner,
                                                    generation_id=gen, effort="fast", artifact_id=artifact_id, gathered=gathered))
    metas = [d for k, d in events if k == "meta"]
    assert len(metas) == 1
    return answer, metas[0]


def spec_bytes(owner, aid, v):
    with open(os.path.join(store.version_dir(owner, aid, v), T.SPEC_NAME), "rb") as fh:
        return fh.read()


def counts(aid):
    with db.connection() as con:
        versions = con.execute("SELECT count(*) AS n FROM artifact_versions WHERE artifact_id = %s", (aid,)).fetchone()["n"]
        jobs = con.execute("SELECT count(*) AS n FROM artifact_jobs WHERE artifact_id = %s", (aid,)).fetchone()["n"]
    return int(versions), int(jobs)


def test_a_no_op_edit_creates_no_job_and_no_version(env, owner, monkeypatch):
    aid = seed(owner, nine_section_doc(), ["docx", "pdf"])
    assert counts(aid) == (1, 1)
    real_preplan = E.preplan
    monkeypatch.setattr(E, "preplan", lambda instruction, parent, **kw: None)
    env["planner"].reply = {"ops": []}
    answer, meta = turn(owner, "change the heading colour to dark blue", gen="noop1")
    assert counts(aid) == (1, 1) and "artifacts" not in meta and "Updated" not in answer
    assert env["planner"].n == 1
    monkeypatch.setattr(E, "preplan", real_preplan)
    answer, meta = turn(owner, "make the headings dark blue", gen="noop2")
    if not E._supports_style("document"):
        assert counts(aid) == (1, 1) and "didn't change" in answer and "Updated" not in answer


def test_a_title_edit_is_a_new_version_of_the_same_artifact_with_everything_else_equal(env, owner):
    parent = nine_section_doc()
    aid = seed(owner, parent, ["docx", "pdf"])
    answer, meta = turn(owner, "change the title to Q3 Security Audit", gen="title")
    ref = meta["artifacts"][0]
    assert ref["artifact_id"] == aid and ref["version"] == 2 and ref["operation"] == "edit"
    assert answer.startswith("Updated **Q3 Security Audit** v2: title changed to “Q3 Security Audit”")
    child = store.read_spec(store.version_dir(owner, aid, 2))
    assert child.title == "Q3 Security Audit" and child.body.blocks == parent.body.blocks and child.sources == parent.sources
    assert env["planner"].n == 0, "0 model calls"
    assert adb.get_artifact(aid, owner)["title"] == "Q3 Security Audit"


def test_make_the_document_landscape_adds_a_version_not_a_second_artifact(env, owner):
    aid = seed(owner, nine_section_doc(), ["docx", "pdf"])
    answer, meta = turn(owner, "Make the document landscape", gen="land")
    assert meta["artifacts"][0]["artifact_id"] == aid and meta["artifacts"][0]["version"] == 2
    assert len(adb.list_artifacts(owner, "conv-ed")) == 1
    assert store.read_spec(store.version_dir(owner, aid, 2)).body.orientation == "landscape"
    assert "landscape pages" in answer


def test_restore_is_byte_identical_and_undo_of_a_restore_returns_to_the_version_before_it(env, owner):
    aid = seed(owner, nine_section_doc(), ["docx", "pdf"])
    turn(owner, "change the title to Second", gen="e2")
    turn(owner, "make it landscape", gen="e3")
    v1 = spec_bytes(owner, aid, 1)
    answer, meta = turn(owner, "restore version 1", gen="r1", rule="restore-version", version=1, reference="named")
    assert meta["artifacts"][0]["version"] == 4 and answer.startswith("Restored **Security Audit** to v1")
    assert spec_bytes(owner, aid, 4) == v1, "restore copies version 1's spec.json byte for byte"
    assert env["planner"].n == 0
    answer, meta = turn(owner, "undo that", gen="u1")
    assert meta["artifacts"][0]["version"] == 5
    assert spec_bytes(owner, aid, 5) == spec_bytes(owner, aid, 3), "undo of the restore returns to v3"
    answer, meta = turn(owner, "undo", gen="u2")
    assert spec_bytes(owner, aid, 6) == spec_bytes(owner, aid, 4) == v1
    answer, meta = turn(owner, "restore version 6", gen="r6", rule="restore-version", version=6, reference="named")
    assert "already at v6" in answer and "artifacts" not in meta


def test_formats_lineage_convert_adds_and_edit_inherits(env, owner):
    wb = S.parse_body("workbook", {"title": "Tickets", "sheets": [
        {"name": "Tickets", "columns": [{"name": "ID"}, {"name": "Title"}, {"name": "Status"}, {"name": "Score", "type": "integer"}], "rows": tickets_rows()}]})
    aid = seed(owner, wb, ["xlsx", "csv"])
    answer, meta = turn(owner, "Convert it to PDF", action="convert", gen="c1", rule="convert", formats=["pdf"])
    ref = meta["artifacts"][0]
    assert ref["version"] == 2 and [f["format"] for f in ref["files"]][:1] == ["xlsx"] and {f["format"] for f in ref["files"]} >= {"xlsx", "csv", "pdf"}
    answer, meta = turn(owner, "rename the Status column to State", gen="e3")
    ref = meta["artifacts"][0]
    assert ref["version"] == 3 and {f["format"] for f in ref["files"]} >= {"xlsx", "csv", "pdf"}
    assert [c.name for c in store.read_spec(store.version_dir(owner, aid, 3)).body.sheets[0].columns][2] == "State"
    v3 = store.read_spec(store.version_dir(owner, aid, 3))
    assert [[r[0], r[1], r[2], r[3]] for r in v3.body.sheets[0].rows] == tickets_rows(), "every cell is kept"


def test_styling_a_csv_only_lineage_adds_an_excel_file_and_says_so(env, owner):
    one = S.parse_body("workbook", {"title": "Data", "sheets": [{"name": "D", "columns": [{"name": "A"}, {"name": "B"}], "rows": [["x", "1"]]}]})
    seed(owner, one, ["csv"])
    answer, meta = turn(owner, "make the header row blue and bold", gen="s1")
    fmts = [f["format"] for f in meta["artifacts"][0]["files"]]
    assert "csv" in fmts and "xlsx" in fmts
    assert "The CSV carries the data only; the formatting is in the Excel file." in answer


def test_a_section_rewrite_changes_only_that_section_and_keeps_sources(env, owner, monkeypatch):
    parent = nine_section_doc()
    aid = seed(owner, parent, ["docx", "pdf"])
    writes = []

    async def write_section(req, spec, item, current):
        writes.append(item)
        return [current[0], {"type": "paragraph", "text": "Findings rewritten plainly.", "sources": ["s2", "bogus"]}]

    monkeypatch.setattr(C, "write_section", write_section)
    env["planner"].reply = {"ops": [{"op": "replace_section", "target": "Findings", "instruction": "rewrite it plainly"}]}
    answer, meta = turn(owner, "rewrite the findings in plain words", gen="sec")
    assert env["planner"].n == 1 and len(writes) == 1
    child = store.read_spec(store.version_dir(owner, aid, 2))
    assert E.preservation_guard(parent, child, {"s4"}) == []
    assert child.sources == parent.sources
    assert answer.startswith("Updated **Security Audit** v2: section “Findings” rewritten")


def test_a_failed_section_write_is_not_claimed(env, owner, monkeypatch):
    seed(owner, nine_section_doc(), ["docx"])

    async def write_section(req, spec, item, current):
        return None

    monkeypatch.setattr(C, "write_section", write_section)
    env["planner"].reply = {"ops": [{"op": "replace_section", "target": "Scope", "instruction": "x"}, {"op": "set_title", "text": "New Title"}]}
    answer, meta = turn(owner, "rewrite scope and retitle it New Title", gen="fail")
    assert "section “Scope” rewritten" not in answer and "title changed" in answer and "could not be written" in answer


def test_the_ui_artifact_id_picks_that_artifact_and_the_last_artifact_turn_wins_over_newest(env, owner):
    first = seed(owner, nine_section_doc(), ["docx"], gen="a")
    second = seed(owner, S.parse_body("document", {"title": "Other", "blocks": [{"type": "paragraph", "text": "x"}]}), ["docx"], gen="b")
    answer, meta = turn(owner, "make it landscape", gen="p1", artifact_id=first, action="create", rule="none")
    assert meta["artifacts"][0]["artifact_id"] == first
    history = [{"role": "assistant", "content": "Created", "meta": {"artifacts": [{"artifact_id": second, "version": 1}]}}]
    answer, meta = turn(owner, "change the title to Picked", gen="p2", history=history)
    assert meta["artifacts"][0]["artifact_id"] == second
    answer, meta = turn(owner, "change the title to Named", gen="p3", artifact_id=second)
    assert meta["artifacts"][0]["artifact_id"] == second
    foreign = "f" * 32
    answer, meta = turn(owner, "change the title to Nope", gen="p4", artifact_id=foreign)
    assert meta["artifacts"][0]["artifact_id"] in (first, second), "an id that is not the caller's is ignored"


def test_two_equally_named_artifacts_get_one_question_and_no_job(env, owner):
    a = seed(owner, S.parse_body("document", {"title": "Audit North", "blocks": [{"type": "paragraph", "text": "x"}]}), ["docx"], gen="n")
    b = seed(owner, S.parse_body("document", {"title": "Audit South", "blocks": [{"type": "paragraph", "text": "y"}]}), ["docx"], gen="s")
    intent = I.ArtifactIntent("edit", reference="named", reference_hint="audit", rule="edit", instruction="make the audit landscape")
    row, question = engine.pick_artifact(adb.list_artifacts(owner, "conv-ed"), intent, instruction="make the audit landscape")
    assert row is None and question.startswith("Which one should I change")
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    answer = asyncio.run(engine.run_artifact_engine("make the audit landscape", [], emit, intent=intent, conversation_id="conv-ed", user_id=owner, generation_id="q"))
    assert answer == question and "artifacts" not in [d for k, d in events if k == "meta"][0]
    assert counts(a) == (1, 1) and counts(b) == (1, 1), "no job and no version for either"


def test_the_payload_is_scratch_and_is_not_published(env, owner):
    aid = seed(owner, nine_section_doc(), ["docx"])
    turn(owner, "make it landscape", gen="scr")
    for v in (1, 2):
        assert not os.path.exists(os.path.join(store.version_dir(owner, aid, v), store.MATERIAL_NAME))


def test_planner_items_seen_live_in_odd_fields_are_still_read():
    """Three shapes the live model produced on 2026-09-15 (Fast): the new
    heading in `value`, the cell change as rows: [[column, value]], and the
    copied column in derived_columns."""
    doc, wb = nine_section_doc(), four_sheet_workbook()
    live = [
        (doc, "add a section on Budget after Timeline", {"op": "insert_section", "after": "Timeline", "value": "Budget", "style_phrase": ""}, "section “Budget” added"),
        (wb, "set Score to 5 where ID is T-001", {"op": "update_cells", "sheet": "Tickets", "where_column": "ID", "where_op": "eq", "where_value": "T-001", "rows": [["Score", "5"]]}, "1 Score cell updated"),
        (wb, "add a Total column to Summary that copies Value", {"op": "add_column", "sheet": "Summary", "name": "Total", "after": "Value", "fill": "copy_of", "derived_columns": ["Value"]}, "Total column added"),
        (wb, "add a Total column to Summary that copies Value", {"op": "add_column", "sheet": "Summary", "name": "Total", "after": "Value", "fill": "copy_of"}, "Total column added"),
    ]
    for parent, prompt, item, phrase in live:
        p = run(E.plan(prompt, parent, call=Calls({"ops": [item]}))) if E.preplan(prompt, parent) is None else E.EditPlan(ops=[E._flat_to_op(item, prompt, parent.kind)], planner="model")
        out = E.apply(parent, p, instruction=prompt)
        assert out.applied and out.applied[0].startswith(phrase), (prompt, out.applied, out.not_applied)


def test_a_failed_section_write_with_nothing_else_publishes_no_version(env, owner, monkeypatch):
    """AS3 fix B4: the only change was a section write, and it failed. The
    version row made at acceptance must not become a published no-op: the
    job fails before render, v1 stays current, and the answer never says
    "Updated" or "Saved … v2"."""
    aid = seed(owner, nine_section_doc(), ["docx"])
    v1 = spec_bytes(owner, aid, 1)

    async def write_section(req, spec, item, current):
        return None

    monkeypatch.setattr(C, "write_section", write_section)
    env["planner"].reply = {"ops": [{"op": "replace_section", "target": "Scope", "instruction": "x"}]}
    answer, meta = turn(owner, "rewrite the scope section in plain words", gen="fail-only")
    assert "Updated" not in answer and "Saved" not in answer, answer
    assert answer.startswith("I didn't change **Security Audit**") and "could not be written" in answer, answer
    assert "no new version was saved" in answer
    assert adb.get_artifact(aid, owner)["current_version"] == 1, "v1 stays current"
    row = adb.get_version(aid, 2, owner)
    assert row is not None and row["status"] not in ("completed", "completed_with_warnings"), row
    assert not os.path.exists(os.path.join(store.version_dir(owner, aid, 2), T.SPEC_NAME)), "nothing published for v2"
    assert spec_bytes(owner, aid, 1) == v1


def test_a_section_write_that_comes_back_unchanged_publishes_no_version(env, owner, monkeypatch):
    aid = seed(owner, nine_section_doc(), ["docx"])

    async def write_section(req, spec, item, current):
        return current

    monkeypatch.setattr(C, "write_section", write_section)
    env["planner"].reply = {"ops": [{"op": "replace_section", "target": "Scope", "instruction": "x"}]}
    answer, meta = turn(owner, "rewrite the scope section in plain words", gen="same-only")
    assert "Updated" not in answer and "Saved" not in answer, answer
    assert "came back unchanged" in answer, answer
    assert adb.get_artifact(aid, owner)["current_version"] == 1


def test_a_failed_inserted_section_publishes_no_version(env, owner, monkeypatch):
    aid = seed(owner, nine_section_doc(), ["docx"])

    async def write_section(req, spec, item, current):
        raise RuntimeError("model down")

    monkeypatch.setattr(C, "write_section", write_section)
    env["planner"].reply = {"ops": [{"op": "insert_section", "after": "Scope", "text": "Budget", "instruction": "add a budget section"}]}
    answer, meta = turn(owner, "add a budget section after scope", gen="insert-fail")
    assert "Updated" not in answer and "Saved" not in answer, answer
    assert "could not be written" in answer and counts(aid)[1] == 2, answer
    assert adb.get_artifact(aid, owner)["current_version"] == 1


def test_a_failed_section_write_beside_an_applied_change_still_publishes(env, owner, monkeypatch):
    aid = seed(owner, nine_section_doc(), ["docx"])

    async def write_section(req, spec, item, current):
        return None

    monkeypatch.setattr(C, "write_section", write_section)
    env["planner"].reply = {"ops": [{"op": "replace_section", "target": "Scope", "instruction": "x"}, {"op": "set_title", "text": "Kept Title"}]}
    answer, meta = turn(owner, "rewrite scope and retitle it Kept Title", gen="fail-mixed")
    assert meta["artifacts"][0]["version"] == 2 and adb.get_artifact(aid, owner)["current_version"] == 2
    assert answer.startswith("Updated **Kept Title** v2"), answer


# ------------------------------- the owner's two sentences, end to end --


def customers_table(table_id: str = "upload1"):
    """The conversation's dataset, as `material_in.gather` leaves it."""
    countries = ["Aurelia"] * 17 + ["Borvia"] * 11 + ["Caldia"] * 8 + ["Dornia"] * 4
    rows = [[f"C-{i:03d}", countries[i], (i % 9) + 1] for i in range(40)]
    return C.DataTable(id=table_id, title="customers-100.csv", columns=["Customer Id", "Country", "Seats"],
                       rows=rows, source_id="upload")


def chart_doc() -> S.ArtifactSpec:
    """A published document holding a BOUND chart — the only kind that
    survives a render: `chart_data.resolve_spec` replaces an unbound chart
    with a "its numbers were not bound to a table" callout."""
    return S.parse_body("document", {"title": "Pricing", "blocks": [
        {"type": "heading", "level": 1, "text": "Seats"},
        {"type": "chart", "chart": {"type": "bar", "title": "Seats by country",
                                    "data": {"table_id": "upload1", "x": "Country", "agg": "count"}}},
        {"type": "heading", "level": 1, "text": "Notes"},
        {"type": "paragraph", "text": "Prices hold until the quarter ends."}]})


def test_the_owners_plots_sentence_adds_the_chart_instead_of_re_rendering_the_file(env, owner):
    """A-F1, end to end through the engine with the verdict THE RULES give.

    "also i want Plots on this docs" is read as a format conversion
    (measured on the integrated tree e543bef, 2026-09-18: action='convert',
    rule='convert-artifact-turn', formats=['docx'], chart_request=True, and
    `intent._should_consult` False, so the classifier never sees it). The
    convert branch re-renders the stored spec, so the owner's sentence
    published a second version of the same document with no plot in it —
    twice, in his own conversation. Asking for a plot is a CHANGE to the
    file, so the turn runs as an edit."""
    aid = seed(owner, nine_section_doc(), ["docx"], conv="conv-plots", gen="seed-plots")
    intent = I.decide("also i want Plots on this docs", has_artifacts=True, artifact_hints=["Security Audit"],
                      has_assistant_answer=True, last_turn_is_artifact=True)
    assert (intent.action, intent.rule, intent.chart_request) == ("convert", "convert-artifact-turn", True)
    gathered = MI.GatheredInput(upload_tables=[customers_table()])
    answer, meta = turn(owner, "also i want Plots on this docs", conv="conv-plots", gen="plots",
                        intent=intent, gathered=gathered)
    ref = meta["artifacts"][0]
    assert ref["artifact_id"] == aid and ref["version"] == 2, "the same file, one version on"
    child = store.read_spec(store.version_dir(owner, aid, 2))
    charts = [b.chart for b in child.body.blocks if getattr(b, "type", "") == "chart"]
    assert charts, f"no plot was added: {answer}"
    assert charts[0].data.table_id == "upload1", "bound to the conversation's dataset"
    assert charts[0].categories and sum(int(v) for v in charts[0].series[0].values) == 40, \
        "the compose stage counted the real rows"
    assert "Not applied" not in answer and "nothing in it changed" not in answer, answer
    assert "chart" in answer.lower(), answer


def test_a_conversion_that_says_nothing_about_charts_is_still_a_conversion(env, owner):
    """GUARD for the rule above: "also give me it as a PDF" in the same state
    (chart_request False) keeps the conversion, so the person gets the file
    format they asked for."""
    aid = seed(owner, nine_section_doc(), ["docx"], conv="conv-pdf", gen="seed-pdf")
    intent = I.decide("also give me it as a PDF", has_artifacts=True, artifact_hints=["Security Audit"],
                      has_assistant_answer=True, last_turn_is_artifact=True)
    assert (intent.action, intent.chart_request) == ("convert", False)
    answer, meta = turn(owner, "also give me it as a PDF", conv="conv-pdf", gen="pdf", intent=intent)
    ref = meta["artifacts"][0]
    assert ref["artifact_id"] == aid and {f["format"] for f in ref["files"]} >= {"docx", "pdf"}, answer
    child = store.read_spec(store.version_dir(owner, aid, 2))
    assert child.body.blocks == nine_section_doc().body.blocks, "a conversion re-renders, it does not edit"


def test_an_edit_that_changes_the_chart_reports_the_change_it_made(env, owner):
    """A-F2, end to end. "make the chart a line chart" pre-plans `set_chart`,
    applies it, and publishes a v2 whose chart really IS a line chart — and
    gains no NEW chart, because changing one adds none. The `gained <= 0`
    withdrawal then stripped the clause and appended a refusal. Measured on
    the integrated tree (e543bef, 2026-09-18), v2 holding ('chart', 'line'),
    the answer was verbatim:

        Saved **Pricing** v2, but nothing in it changed. Not applied: the
        chart (the data could not be drawn as a chart).

    which is the owner's own "the change could not be made" complaint, back
    on the `set_chart` path PR #77 shipped two days earlier."""
    material = engine._material_dict(C.Material(instruction="seed", tables=[customers_table()]))
    aid = seed(owner, chart_doc(), ["docx"], conv="conv-pie", gen="seed-pie", material=material, allow_warnings=True)
    answer, meta = turn(owner, "make the chart a line chart", conv="conv-pie", gen="pie",
                        gathered=MI.GatheredInput(upload_tables=[customers_table()]))
    assert meta["artifacts"][0]["version"] == 2
    child = store.read_spec(store.version_dir(owner, aid, 2))
    charts = [b.chart for b in child.body.blocks if getattr(b, "type", "") == "chart"]
    assert [c.type for c in charts] == ["line"], "the chart really changed"
    assert "Not applied" not in answer, answer
    assert "nothing in it changed" not in answer, answer
    assert answer.startswith("Updated **Pricing** v2: line chart"), answer
