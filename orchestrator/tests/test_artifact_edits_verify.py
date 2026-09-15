"""Adversarial verifier cases for AS3 prompt-edits (40 cases, authored by
the verifier; no user content). Edits-module cases run without a model;
engine cases use the stub planner/renderer fixtures of test_artifact_edits;
render cases write real XLSX/CSV/DOCX/PDF files."""
from __future__ import annotations

import asyncio
import os
import re
import time

import pytest

from app.artifacts import compose as C
from app.artifacts import edits as E
from app.artifacts import spec as S
from app.artifacts import store

from app.engines.artifact import _attach_payload as _real_attach
from tests import test_artifact_edits as base
from tests.test_artifact_edits import counts, four_sheet_workbook, nine_section_doc, seed, spec_bytes, tickets_rows, turn

env = base.env
owner = base.owner

OUT = os.environ.get("AS3_VERIFY_OUT", "")


def ops_of(plan):
    return [o.model_dump() for o in plan.ops] if plan else None


def wb_one(rows, *, charts=None, extra_cols=(), totals=None):
    cols = [{"name": "ID"}, {"name": "Title"}, {"name": "Status"}, {"name": "Score", "type": "integer"}, *extra_cols]
    sheet = {"name": "Tickets", "columns": cols, "rows": rows}
    if charts:
        sheet["charts"] = charts
    if totals:
        sheet["totals"] = totals
    return S.parse_body("workbook", {"title": "Tickets", "sheets": [sheet]})


def rows_of(spec, i=0):
    return [list(r) for r in spec.body.sheets[i].rows]


def plan(*ops):
    return E.EditPlan(ops=list(ops), planner="model", model_calls=1)


# ---------------------------------------------------- 1-7 pre-planner traps --


@pytest.mark.parametrize("text", [
    "change the title to Q3 Review and make it landscape",
    "call it Quarterly Review, and make it landscape",
    "change the title to Q3 Review then delete the Appendix section",
])
def test_v01_03_a_compound_request_is_never_swallowed_into_the_title(text):
    p = E.preplan(text, nine_section_doc())
    titles = [o.title for o in (p.ops if p else []) if isinstance(o, E.SetTitle)]
    assert not any(re.search(r"\b(and|then|landscape|delete)\b", t, re.I) for t in titles), ops_of(p)


def test_v04_a_rename_column_stops_at_the_clause_boundary():
    p = E.preplan("rename Status to State and make the header bold", four_sheet_workbook())
    names = [o.name for o in (p.ops if p else []) if isinstance(o, E.RenameColumn)]
    assert all(n == "State" for n in names), ops_of(p)


def test_v05_a_sheet_rename_with_a_second_clause_is_not_a_column_called_tickets_sheet():
    p = E.preplan("rename the Tickets sheet to Queue and add a column for owner", four_sheet_workbook())
    assert not any(isinstance(o, E.RenameColumn) for o in (p.ops if p else [])), ops_of(p)


def test_v06_add_column_after_does_not_eat_the_next_clause():
    p = E.preplan("add owner column after status then delete rows where status is Closed", four_sheet_workbook())
    afters = [o.after for o in (p.ops if p else []) if isinstance(o, E.AddColumn)]
    assert all(a is None or a.lower() == "status" for a in afters), ops_of(p)


def test_v07_a_quoted_title_with_and_stays_deterministic():
    p = E.preplan('change the title to "Research and Development"', nine_section_doc())
    assert p is not None and [o.title for o in p.ops if isinstance(o, E.SetTitle)] == ["Research and Development"]


# ------------------------------------------------ 8-10 restore / undo words --


def test_v08_restore_words_inside_a_real_edit_are_not_a_byte_restore(env, owner):
    aid = seed(owner, nine_section_doc(), ["docx"])
    turn(owner, "change the title to Second", gen="v8a")
    env["planner"].reply = {"ops": [{"op": "replace_section", "target": "Scope", "instruction": "use the v1 numbers"}]}

    async def write_section(req, spec, item, current):
        return [current[0], {"type": "paragraph", "text": "Scope with the version 1 numbers."}]

    import app.artifacts.compose as CC
    orig = CC.write_section
    CC.write_section = write_section
    try:
        answer, meta = turn(owner, "use version 1 numbers in the Scope section", gen="v8b", rule="restore-version", version=1, reference="named")
    finally:
        CC.write_section = orig
    assert not answer.startswith("Restored"), answer
    assert spec_bytes(owner, aid, 3) != spec_bytes(owner, aid, 1)


def test_v09_a_plain_restore_still_restores_byte_for_byte(env, owner):
    aid = seed(owner, nine_section_doc(), ["docx"])
    turn(owner, "make it landscape", gen="v9a")
    answer, meta = turn(owner, "go back to version 1", gen="v9b", rule="restore-version", version=1, reference="named")
    assert answer.startswith("Restored") and spec_bytes(owner, aid, 3) == spec_bytes(owner, aid, 1)
    assert env["planner"].n == 0


def test_v10_a_hindi_undo_restores_the_parent(env, owner):
    aid = seed(owner, nine_section_doc(), ["docx"])
    turn(owner, "make it landscape", gen="v10a")
    answer, meta = turn(owner, "पहले जैसा करो", gen="v10b")
    assert meta["artifacts"][0]["version"] == 3 and spec_bytes(owner, aid, 3) == spec_bytes(owner, aid, 1)


# ---------------------------------------------- 11-13 multilingual / typos --


def test_v11_gujarati_and_hinglish_typo_style_requests_never_produce_a_wrong_op():
    wb = four_sheet_workbook()
    for text in ("હેડર રો બોલ્ડ કરો", "heading ka colour dark blu karo", "sheet me colors dalo"):
        p = E.preplan(text, wb)
        for o in (p.ops if p else []):
            assert isinstance(o, E.SetStyle), (text, ops_of(p))
            for r in o.patch.get("rules", []):
                assert set(r["style"]) <= {"bold", "color", "background"}, (text, ops_of(p))


def test_v12_document_fonts_before_the_styling_engine_are_said_not_applied_and_no_version(env, owner):
    aid = seed(owner, nine_section_doc(), ["docx", "pdf"])
    answer, meta = turn(owner, "make the title Georgia 28pt dark blue", gen="v12")
    if not E._supports_style("document"):
        assert counts(aid) == (1, 1) and "Updated" not in answer and "artifacts" not in meta


def test_v13_title_colour_words_after_a_title_change_do_not_style_the_title_when_headings_were_named():
    p = E.preplan("title should be Annual Plan and headings navy", nine_section_doc())
    for o in (p.ops if p else []):
        if isinstance(o, E.SetTitle):
            assert o.title == "Annual Plan", ops_of(p)
        if isinstance(o, E.SetStyle):
            assert all(r["target"]["kind"] == "heading" for r in o.patch["rules"]), ops_of(p)


# ----------------------------------------- 14-22 sections, ambiguity, repeat --


def test_v14_two_close_headings_ask_instead_of_guessing():
    doc = S.parse_body("document", {"title": "R", "blocks": [
        {"type": "heading", "level": 1, "text": "Risk Assessment"}, {"type": "paragraph", "text": "a"},
        {"type": "heading", "level": 1, "text": "Risk Register"}, {"type": "paragraph", "text": "b"}]})
    out = E.apply(doc, plan(E.RenameHeading(target=E.SectionRef(text="Risk"), text="Risks")), instruction="rename the risk heading to Risks")
    assert out.question and not out.applied_ops and E.canonical_json(out.spec) == E.canonical_json(doc)


def test_v15_five_repeated_edits_keep_every_untouched_section_identical(env, owner):
    parent = nine_section_doc()
    aid = seed(owner, parent, ["docx"])
    for i, text in enumerate(["change the title to One", "make it landscape", "rename the heading Scope to Coverage",
                              "change the title to Two", "make it portrait"], 2):
        answer, meta = turn(owner, text, gen=f"v15-{i}")
        assert meta["artifacts"][0]["version"] == i, answer
    child = store.read_spec(store.version_dir(owner, aid, 6))
    assert E.preservation_guard(parent, child, {"s2", "meta.title", "meta.orientation"}) == []
    assert child.body.orientation == "portrait" and child.title == "Two" and child.sources == parent.sources


def test_v16_remove_a_typo_in_a_section_never_deletes_the_section():
    doc = nine_section_doc()
    out = E.apply(doc, plan(E.DeleteBlocks(target=E.SectionRef(text="Scope"))), instruction="remove the typo in the Scope section")
    assert "Scope" in [b.text for b in out.spec.body.blocks if b.type == "heading"], out.applied


def test_v17_remove_blanks_from_a_column_never_deletes_the_column():
    wb = four_sheet_workbook()
    out = E.apply(wb, plan(E.DeleteColumn(sheet="Tickets", column="Score")), instruction="remove the blank values from the Score column")
    assert [c.name for c in out.spec.body.sheets[0].columns] == ["ID", "Title", "Status", "Score"], out.applied


def test_v17b_remove_a_value_from_a_column_never_deletes_the_rows():
    wb = wb_one(tickets_rows())
    out = E.apply(wb, plan(E.DeleteRows(where=E.Where(column="Status", value="Closed"))), instruction="remove Closed from the Status column")
    assert rows_of(out.spec) == tickets_rows() and out.question
    out = E.apply(wb, plan(E.DeleteRows(where=E.Where(column="Status", value="Closed"))), instruction="delete the closed tickets")
    assert len(rows_of(out.spec)) == 52


def test_v18_delete_the_appendix_section_deletes_exactly_it():
    doc = nine_section_doc()
    out = E.apply(doc, plan(E.DeleteBlocks(target=E.SectionRef(text="Appendix"))), instruction="delete the Appendix section")
    heads = [b.text for b in out.spec.body.blocks if b.type == "heading" and b.level == 1]
    assert "Appendix" not in heads and len(heads) == 8
    assert E.preservation_guard(doc, out.spec, out.touched) == []


def test_v19_delete_section_9_by_number():
    doc = nine_section_doc()
    out = E.apply(doc, plan(E.DeleteBlocks(target=E.SectionRef(text="section 9"))), instruction="delete section 9")
    assert [b.text for b in out.spec.body.blocks if b.type == "heading" and b.level == 1][-1] == "Timeline"


def test_v20_undo_on_a_first_version_says_there_is_nothing_to_go_back_to(env, owner):
    aid = seed(owner, nine_section_doc(), ["docx"])
    answer, meta = turn(owner, "undo", gen="v20")
    assert "no earlier version" in answer and counts(aid) == (1, 1)


def test_v21_insert_section_lands_after_its_anchor_and_the_rest_is_kept():
    doc = nine_section_doc()
    out = E.apply(doc, plan(E.InsertSection(after=E.SectionRef(text="Findings"), heading="Controls", instruction="list controls")), instruction="add a Controls section after Findings")
    heads = [b.text for b in out.spec.body.blocks if b.type == "heading" and b.level == 1]
    assert heads.index("Controls") == heads.index("Findings") + 1
    assert out.pending_sections and out.pending_sections[0]["mode"] == "insert"
    spec2, applied, na = asyncio.run(E.resolve_pending(out.spec, out.pending_sections, section_writer=_writer_para("Control list.")))
    assert applied and E.preservation_guard(doc, spec2, {"new1"}) == []


def _writer_para(text):
    async def w(item, current, whole):
        return [current[0], {"type": "paragraph", "text": text}]
    return w


def test_v22_a_fourth_section_rewrite_in_one_edit_is_not_applied():
    doc = nine_section_doc()
    ops = [E.ReplaceSection(target=E.SectionRef(text=n), instruction="shorter") for n in ("Scope", "Method", "Findings", "Timeline")]
    out = E.apply(doc, plan(*ops), instruction="shorten scope, method, findings and timeline")
    assert len(out.pending_sections) == 3 and any("at most 3" in n["reason"] for n in out.not_applied)


# ---------------------------------------------------- 23-26 cells and rows --


def test_v23_update_cells_coerces_a_typed_number_into_an_integer_column():
    wb = wb_one(tickets_rows())
    out = E.apply(wb, plan(E.UpdateCells(where=E.Where(column="ID", value="T-001"), column="Score", value="9")), instruction="set Score to 9 where ID is T-001")
    assert rows_of(out.spec)[0][3] == 9 and rows_of(out.spec)[1:] == tickets_rows()[1:]


def test_v24_an_unnamed_multi_row_update_is_refused():
    wb = wb_one(tickets_rows())
    out = E.apply(wb, plan(E.UpdateCells(where=E.Where(column="Status", value="Open"), column="Score", value="1")), instruction="set Score to 1 for those")
    assert rows_of(out.spec) == tickets_rows() and out.not_applied


def test_v25_invented_rows_from_the_planner_are_refused():
    wb = wb_one(tickets_rows())
    out = E.apply(wb, plan(E.AddRows(rows=[["T-999", "Invented ticket", "Open", 4]])), instruction="add a row for the new ticket")
    assert len(out.spec.body.sheets[0].rows) == 60 and out.not_applied


def test_v26_a_5000_row_sheet_takes_a_column_and_a_named_delete_fast_and_keeps_every_cell():
    big = [[f"T-{i:05d}", f"Ticket {i}", ("Closed" if i % 10 == 0 else "Open"), i % 7] for i in range(5000)]
    wb = wb_one(big)
    t0 = time.perf_counter()
    out = E.apply(wb, plan(E.AddColumn(name="Owner", after="Status"), E.DeleteRows(where=E.Where(column="Status", value="Closed"))),
                  instruction="add a column for owner after Status and delete rows where Status is Closed")
    took = time.perf_counter() - t0
    rows = rows_of(out.spec)
    assert len(rows) == 4500 and all(r[3] is None for r in rows)
    assert [[r[0], r[1], r[2], r[4]] for r in rows] == [r for r in big if r[2] != "Closed"]
    assert took < 10, took


# ------------------------------------------------------------ 27-31 charts --


def _chart_wb():
    rows = [["T-1", "a", "Open", 3], ["T-2", "b", "Closed", 5], ["T-3", "c", "Open", 2], ["T-4", "d", "Closed", 7]]
    return wb_one(rows, charts=[{"type": "bar", "title": "Scores", "categories": ["ID"], "series": [{"name": "Score"}]}])


def test_v27_a_sheet_chart_follows_the_rows_after_a_delete():
    wb = _chart_wb()
    assert wb.body.sheets[0].charts[0].categories == ["T-1", "T-2", "T-3", "T-4"]
    out = E.apply(wb, plan(E.DeleteRows(where=E.Where(column="Status", value="Closed"))), instruction="delete rows where Status is Closed")
    ch = out.spec.body.sheets[0].charts[0]
    remaining = rows_of(out.spec)
    assert ch.categories == [r[0] for r in remaining] and ch.series[0].values == [float(r[3]) for r in remaining]


def test_v28_a_sheet_chart_follows_an_update_and_loses_a_deleted_series_column():
    wb = _chart_wb()
    out = E.apply(wb, plan(E.UpdateCells(where=E.Where(column="ID", value="T-3"), column="Score", value="9")), instruction="set Score to 9 where ID is T-3")
    assert out.spec.body.sheets[0].charts[0].series[0].values == [3.0, 5.0, 9.0, 7.0]
    out2 = E.apply(wb, plan(E.DeleteColumn(column="Score")), instruction="delete the Score column")
    sh = out2.spec.body.sheets[0]
    assert all(s.name != "Score" for c in sh.charts for s in c.series), "a chart must not keep data for a column that is gone"


def test_v29_a_chart_patch_carrying_data_is_refused():
    wb = _chart_wb()
    out = E.apply(wb, plan(E.SetChart(patch={"type": "line", "series": [{"name": "Score", "values": [9, 9, 9, 9]}]})), instruction="make it a line chart")
    assert out.spec.body.sheets[0].charts[0].series[0].values == [3.0, 5.0, 2.0, 7.0]


def test_v30_an_unsupported_chart_type_is_said_not_applied():
    out = E.apply(_chart_wb(), plan(E.SetChart(patch={"type": "scatter"})), instruction="make it a scatter plot")
    if E._chart_spec is None:
        assert out.not_applied and "charts engine" in out.not_applied[0]["reason"]


def test_v31_a_pie_needs_one_series_and_a_bar_to_line_change_keeps_the_values():
    rows = [["T-1", "a", "Open", 3, 1], ["T-2", "b", "Closed", 5, 2]]
    wb = wb_one(rows, extra_cols=[{"name": "Effort", "type": "integer"}],
                charts=[{"type": "bar", "title": "S", "categories": ["ID"], "series": [{"name": "Score"}, {"name": "Effort"}]}])
    out = E.apply(wb, plan(E.SetChart(patch={"type": "pie"})), instruction="make it a pie")
    assert out.not_applied and out.spec.body.sheets[0].charts[0].type == "bar"
    out = E.apply(wb, plan(E.SetChart(patch={"type": "line", "title": "Trend"})), instruction="make it a line chart titled Trend")
    ch = out.spec.body.sheets[0].charts[0]
    assert ch.type == "line" and ch.title == "Trend" and ch.series[0].values == [3.0, 5.0] and ch.series[1].values == [1.0, 2.0]


# ------------------------------------------------- 32-37 render + injection --


def _render(spec, formats, tmp_path, slug):
    from app.artifacts import render as R
    report = R.render_version(spec, formats, str(tmp_path), title_slug=slug, version=2, effort="fast")
    files = {f["format"]: tmp_path / f["filename"] for f in report.to_json()["files"]}
    if OUT:
        import shutil
        for p in files.values():
            shutil.copy(p, os.path.join(OUT, p.name))
    return files


def test_v32_a_formula_lead_column_name_is_neutralised_in_xlsx_and_csv(tmp_path):
    import openpyxl
    evil = "=cmd|' /C calc'!A0"
    wb = wb_one(tickets_rows()[:5])
    out = E.apply(wb, plan(E.RenameColumn(column="Title", name=evil)), instruction=f"rename Title to {evil}")
    files = _render(out.spec, ["xlsx", "csv"], tmp_path, "v32")
    ws = openpyxl.load_workbook(files["xlsx"]).worksheets[0]
    cells = [c for row in ws.iter_rows() for c in row if c.value == evil]
    assert cells and all(c.data_type == "s" and c.quotePrefix for c in cells)
    assert "'=cmd" in files["csv"].read_text(encoding="utf-8-sig")


def test_v33_a_formula_lead_constant_fill_is_neutralised(tmp_path):
    import openpyxl
    wb = wb_one(tickets_rows()[:5])
    out = E.apply(wb, plan(E.AddColumn(name="Note", fill=E.FillConstant(value="=1+1"))), instruction="add a column Note filled with =1+1")
    assert [r[4] for r in rows_of(out.spec)] == ["=1+1"] * 5
    files = _render(out.spec, ["xlsx", "csv"], tmp_path, "v33")
    ws = openpyxl.load_workbook(files["xlsx"]).worksheets[0]
    cells = [c for row in ws.iter_rows() for c in row if c.value == "=1+1"]
    assert len(cells) == 5 and all(c.quotePrefix and c.data_type == "s" for c in cells)
    assert files["csv"].read_text(encoding="utf-8-sig").count("'=1+1") == 5


def test_v34_a_formula_lead_title_on_a_workbook_is_not_a_formula(tmp_path):
    import openpyxl
    wb = wb_one(tickets_rows()[:3])
    out = E.apply(wb, plan(E.SetTitle(title="=HYPERLINK(\"http://x\",\"y\")")), instruction='change the title to =HYPERLINK("http://x","y")')
    files = _render(out.spec, ["xlsx"], tmp_path, "v34")
    book = openpyxl.load_workbook(files["xlsx"])
    for ws in book.worksheets:
        for row in ws.iter_rows():
            for c in row:
                assert c.data_type != "f", (ws.title, c.coordinate, c.value)


def test_v35_pasted_rows_with_formula_leads_are_added_and_neutralised(tmp_path):
    import openpyxl
    wb = wb_one(tickets_rows()[:3])
    from app.engines import artifact as engine
    tables_, _t, _n = engine._pasted_tables("add these rows\n\nID\tTitle\tStatus\tScore\nT-900\t@SUM(A1)\tOpen\t2\nT-901\tok\tClosed\t3\n", [])
    out = E.apply(wb, plan(E.AddRows(rows=[])), tables=tables_, instruction="add these rows")
    assert rows_of(out.spec)[-2][:3] == ["T-900", "@SUM(A1)", "Open"], (out.not_applied, tables_)
    files = _render(out.spec, ["xlsx"], tmp_path, "v35")
    ws = openpyxl.load_workbook(files["xlsx"]).worksheets[0]
    cell = next(c for row in ws.iter_rows() for c in row if c.value == "@SUM(A1)")
    assert cell.quotePrefix


def test_v36_a_red_status_highlight_fills_only_the_status_column(tmp_path):
    import openpyxl
    wb = wb_one(tickets_rows()[:6])
    p = E.preplan("highlight the Status column red", wb)
    assert p is not None
    out = E.apply(wb, p, instruction="highlight the Status column red")
    assert out.applied_ops == ["set_style"], out.not_applied
    files = _render(out.spec, ["xlsx"], tmp_path, "v36")
    ws = openpyxl.load_workbook(files["xlsx"]).worksheets[0]
    header_row = next(i for i, row in enumerate(ws.iter_rows(values_only=True), 1) if row and "Status" in row)
    col = [c.value for c in ws[header_row]].index("Status") + 1
    body = header_row + 1
    fill = ws.cell(body, col).fill.fgColor.rgb
    other = ws.cell(body, 1).fill.fgColor.rgb
    assert fill not in (None, "00000000") and fill != other, (fill, other)


def test_v37_landscape_reaches_the_docx_section_and_the_pdf_pages(tmp_path):
    import docx
    import pypdfium2
    doc = nine_section_doc()
    out = E.apply(doc, E.preplan("make it landscape", doc), instruction="make it landscape")
    files = _render(out.spec, ["docx", "pdf"], tmp_path, "v37")
    sec = docx.Document(str(files["docx"])).sections[0]
    assert sec.page_width > sec.page_height
    pdf = pypdfium2.PdfDocument(str(files["pdf"]))
    try:
        sizes = [pdf[i].get_size() for i in range(len(pdf))]
    finally:
        pdf.close()
    assert sizes and all(w > h for w, h in sizes), sizes
    (tmp_path / "p").mkdir()
    parent_files = _render(doc, ["docx"], tmp_path / "p", "v37p")
    a = [p.text for p in docx.Document(str(files["docx"])).paragraphs]
    b = [p.text for p in docx.Document(str(parent_files["docx"])).paragraphs]
    assert a == b, "only the orientation changed"


# --------------------------------------------- 38-40 blanks, numbers, race --


def test_v38_delete_rows_where_a_value_is_blank_removes_exactly_the_blank_row():
    wb = four_sheet_workbook()
    out = E.apply(wb, plan(E.DeleteRows(sheet="Audit", where=E.Where(column="Result", op="blank"))), instruction="delete rows where Result is blank")
    assert [r for r in out.spec.body.sheets[1].rows] == [["web1", "tls", "Pass"], ["db1", "tls", "Fail"]]
    assert E.preservation_guard(wb, out.spec, out.touched) == []


def test_v39_a_numeric_condition_deletes_by_value_not_text_and_totals_follow_a_column_insert():
    rows = tickets_rows()
    wb = wb_one(rows, totals=[{"column": "Score", "fn": "sum"}])
    out = E.apply(wb, plan(E.DeleteRows(where=E.Where(column="Score", op="lt", value=2))), instruction="delete rows where Score is less than 2")
    assert all(r[3] >= 2 for r in rows_of(out.spec)) and len(rows_of(out.spec)) == len([r for r in rows if r[3] >= 2])
    out2 = E.apply(wb, plan(E.AddColumn(name="Owner", after="ID")), instruction="add a column for owner after ID")
    t = out2.spec.body.sheets[0].totals[0]
    col = t.column if isinstance(t.column, int) else [c.name for c in out2.spec.body.sheets[0].columns].index(t.column)
    assert out2.spec.body.sheets[0].columns[col].name == "Score"


def test_v40_a_restore_job_whose_payload_is_missing_still_restores_byte_for_byte(env, owner, monkeypatch):
    from app.engines import artifact as engine
    aid = seed(owner, nine_section_doc(), ["docx"])
    turn(owner, "make it landscape", gen="v40a")
    monkeypatch.setattr(engine, "_attach_payload", lambda *a, **k: None if a[3] == "edit" else _real_attach(*a, **k))
    monkeypatch.setattr(engine, "_PAYLOAD_WAIT_S", 0.2)
    answer, meta = turn(owner, "restore version 1", gen="v40b", rule="restore-version", version=1, reference="named")
    assert spec_bytes(owner, aid, 3) == spec_bytes(owner, aid, 1), answer



def test_v41_background_is_still_a_section_a_request_can_name():
    assert C.requested_sections("write a report with background, findings and recommendations") == ["background", "findings", "recommendations"]
    assert C.requested_sections("create a doc with an introduction, background and conclusion") == ["introduction", "background", "conclusion"]
    assert C.requested_sections("give it in docs in a standard and classy format with headings in dark blue, landscape, background colour light grey") == []
