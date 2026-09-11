"""The composer (app/artifacts/compose.py) with the model stubbed.

Every path the budget can take is driven here: a clean Fast call, the single
repair after invalid JSON, the placeholder correction, the Think outline +
review + correction, an edit that carries the parent spec, the caps, the
visual review's request shape, and the intent classifier's confidence floor.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import llm
from app.artifacts import compose as C
from app.artifacts import spec as S
from app.artifacts import types as T


def _doc_json(**over):
    base = {
        "title": "Pricing Update",
        "template_id": "generic",
        "blocks": [
            {"type": "heading", "level": 1, "text": "Summary"},
            {"type": "paragraph", "text": "Team tier moves to $59.", "sources": ["s1"]},
        ],
        "sources": [{"id": "s1", "title": "Finance note"}],
    }
    base.update(over)
    return base


class _Model:
    """A scripted `llm.json_completion`: answers in order, records prompts."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, messages, *, json_schema=None, schema_name="", temperature=0.0, max_tokens=None, thinking=False, effort=None):
        self.calls.append({"messages": messages, "schema": schema_name, "thinking": thinking, "max_tokens": max_tokens, "effort": effort})
        if not self.answers:
            raise AssertionError("the model was called more times than the script allows")
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer)


def _req(effort="fast", **over):
    kw = dict(kind="document", formats=["docx", "pdf"], template_id="generic", effort=effort,
              material=C.Material(instruction="Write a short pricing update.", history_text="user: prices are changing", sources=[C.Source("s1", "Finance note", "Team tier to $59.")]))
    kw.update(over)
    return C.ComposeRequest(**kw)


def test_fast_is_one_call_thinking_off_and_the_spec_is_validated(monkeypatch):
    model = _Model([_doc_json()])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast")))
    assert result.spec.kind == "document" and result.spec.title == "Pricing Update"
    assert result.model_calls == 1 and result.corrections == 0
    call = model.calls[0]
    assert call["thinking"] is False and call["schema"] == "artifact_document"
    system = call["messages"][0]["content"]
    assert "never write HTML" in system or "never write" in system
    assert "[s1] Finance note" in call["messages"][1]["content"], "sources are offered by id"


def test_invalid_json_is_repaired_exactly_once_with_the_field_paths(monkeypatch):
    bad = _doc_json(blocks=[{"type": "hologram", "text": "x"}])
    model = _Model([bad, _doc_json()])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast")))
    assert result.spec.title == "Pricing Update" and result.corrections == 1
    repair_prompt = model.calls[1]["messages"][-1]["content"]
    assert "did not match the schema" in repair_prompt and "blocks" in repair_prompt


def test_two_invalid_answers_are_a_model_failure(monkeypatch):
    bad = _doc_json(blocks=[{"type": "hologram", "text": "x"}])
    monkeypatch.setattr(llm, "json_completion", _Model([bad, bad]))
    with pytest.raises(C.ComposeError) as exc:
        asyncio.run(C.compose(_req("fast")))
    assert exc.value.category == "model_failure"


def test_an_answer_cut_off_at_the_budget_says_so(monkeypatch):
    """The 2026-09-11 e2e run: a repair pass ran to max_tokens (180 s) and
    the person read "did not return the document as JSON". The finish
    reason the completion layer records tells the two apart."""
    truncated = '{"title": "Plans", "template_id": "tracker", "sheets": [{"name": "A", "columns": [{"name": "x"}], "rows": [["1"], ["2"'

    class _Cut(_Model):
        async def __call__(self, *a, **k):
            llm._set_finish_reason("length")
            return await super().__call__(*a, **k)

    monkeypatch.setattr(llm, "json_completion", _Cut([truncated]))
    llm.reset_finish_reason()
    with pytest.raises(C.ComposeError) as exc:
        asyncio.run(C.compose(_req("fast", kind="workbook", formats=["xlsx"], template_id="tracker")))
    assert exc.value.category == "model_failure" and "cut off" in str(exc.value)
    llm.reset_finish_reason()
    monkeypatch.setattr(llm, "json_completion", _Model(["not json at all"]))
    with pytest.raises(C.ComposeError) as exc:
        asyncio.run(C.compose(_req("fast")))
    assert "did not return the document as JSON" in str(exc.value)


def test_placeholders_get_one_correction_at_fast(monkeypatch):
    holey = _doc_json(blocks=[{"type": "paragraph", "text": "Lorem ipsum. [Insert chart]"}])
    model = _Model([holey, _doc_json()])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast")))
    assert result.corrections == 1 and result.warnings == []
    assert "placeholder" in model.calls[1]["messages"][-1]["content"].lower()


def test_a_placeholder_that_survives_the_budget_is_a_warning_not_a_loop(monkeypatch):
    holey = _doc_json(blocks=[{"type": "paragraph", "text": "TBD"}])
    model = _Model([holey, holey])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast")))
    assert result.corrections == 1 and len(model.calls) == 2
    assert result.warnings and "placeholder" in result.warnings[0]


def test_think_outlines_reviews_and_corrects_musts(monkeypatch):
    outline = {"title": "Pricing Update", "audience": "customers", "purpose": "announce", "sections": [{"heading": "Summary", "purpose": "the change", "elements": ["paragraphs"]}], "needs_current_facts": False, "assumptions": ["effective next month"]}
    review = {"ok": False, "issues": [{"where": "Summary", "problem": "does not state the effective date", "fix": "add it", "severity": "must"}]}
    fixed = _doc_json(blocks=[{"type": "heading", "level": 1, "text": "Summary"}, {"type": "paragraph", "text": "Team tier moves to $59 on 1 October.", "sources": ["s1"]}])
    model = _Model([outline, _doc_json(), review, fixed])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("think")))
    assert [c["schema"] for c in model.calls] == ["artifact_outline", "artifact_document", "artifact_review", "artifact_document"]
    assert all(c["thinking"] is True and c["effort"] == "think" for c in model.calls), "the completion layer sizes the thinking pool by effort"
    assert result.outline == outline and result.review == review and result.corrections == 1
    assert "1 October" in result.spec.body.blocks[1].text
    assert "Follow this outline" in model.calls[1]["messages"][0]["content"]
    assert "reviewer found these problems" in model.calls[3]["messages"][-1]["content"]


def test_a_clean_review_makes_no_correction(monkeypatch):
    outline = {"title": "x", "audience": "", "purpose": "", "sections": [], "needs_current_facts": False, "assumptions": []}
    model = _Model([outline, _doc_json(), {"ok": True, "issues": []}])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("think")))
    assert result.corrections == 0 and len(model.calls) == 3


def test_an_edit_carries_the_parent_spec_and_skips_the_outline(monkeypatch):
    parent = S.parse_body("document", _doc_json())
    edited = _doc_json(title="Pricing Update (short)")
    model = _Model([edited, {"ok": True, "issues": []}])
    monkeypatch.setattr(llm, "json_completion", model)
    req = _req("think", operation="edit", parent_spec=parent, instruction="Make it shorter.")
    result = asyncio.run(C.compose(req))
    assert result.spec.title == "Pricing Update (short)"
    assert model.calls[0]["schema"] == "artifact_document", "no outline call for an edit"
    assert "EDITING an existing document" in model.calls[0]["messages"][0]["content"]
    assert "Team tier moves to $59." in model.calls[0]["messages"][0]["content"], "the parent content travels with the edit"


def test_shorter_that_came_back_longer_is_corrected_once_then_warned(monkeypatch):
    """The e2e run of 2026-09-11: 'make the brief shorter' came back as two
    pages. A deterministic check every effort can afford: one correction
    naming the word counts, then a visible warning if the model still cannot."""
    parent = S.parse_body("document", _doc_json(blocks=[{"type": "paragraph", "text": "Team tier moves to fifty-nine dollars next month."}]))
    longer = _doc_json(blocks=[{"type": "paragraph", "text": "Team tier moves to fifty-nine dollars next month, " * 4 + "and this is a much longer paragraph than before."}])
    shorter = _doc_json(blocks=[{"type": "paragraph", "text": "Team tier: $59 next month."}])
    model = _Model([longer, shorter])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", operation="edit", parent_spec=parent, instruction="Make it shorter and add a warning.")))
    assert result.corrections == 1 and len(model.calls) == 2 and result.warnings == []
    assert "asked for a SHORTER document" in model.calls[1]["messages"][-1]["content"]
    assert "$59" in result.spec.body.blocks[0].text
    # Still longer after the correction: the version says so.
    model = _Model([longer, longer])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", operation="edit", parent_spec=parent, instruction="Condense this.")))
    assert result.corrections == 1 and any("shorter document but this version is longer" in w for w in result.warnings)
    # Not an edit, or not asking for less: no check, no call.
    model = _Model([longer])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", operation="edit", parent_spec=parent, instruction="Add a section on churn risk.")))
    assert result.corrections == 0 and len(model.calls) == 1


def test_figures_the_material_never_gave_are_a_warning_and_a_hint_to_the_reviewer(monkeypatch):
    invented = _doc_json(blocks=[{"type": "paragraph", "text": "Competitors charge between $55 and $65; 1,000 teams pay $59.", "sources": ["s1"]}])
    model = _Model([invented])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast")))
    assert result.corrections == 0, "Fast names them; it does not spend a call on them"
    assert any(w.startswith("figures not in the material") and "$55" in w and "1,000" in w and "$59" not in w for w in result.warnings), result.warnings
    # Think: the reviewer is told which figures to look at.
    outline = {"title": "x", "audience": "", "purpose": "", "sections": [], "needs_current_facts": False, "assumptions": []}
    model = _Model([outline, invented, {"ok": True, "issues": []}])
    monkeypatch.setattr(llm, "json_completion", model)
    asyncio.run(C.compose(_req("think")))
    review_prompt = model.calls[2]["messages"][-1]["content"]
    assert "appear nowhere in the material" in review_prompt and "$55" in review_prompt
    assert "never a placeholder, never a zero" in review_prompt, "the reviewer is told what the fix is, so the correction does not blank the figures"
    # The role prompt says what to do when a figure is missing.
    assert "never supply a plausible one" in model.calls[1]["messages"][0]["content"]


def _full_doc():
    return _doc_json(blocks=[
        {"type": "kpis", "items": [{"label": "Price", "value": "$59"}]},
        {"type": "heading", "level": 1, "text": "Objective"},
        {"type": "paragraph", "text": "Team tier moves to $59 to align with market rates and improve margin without hurting retention.", "sources": ["s1"]},
        {"type": "heading", "level": 1, "text": "Risks"},
        {"type": "bullets", "items": ["Churn among price-sensitive teams", "Support load during the transition", "Competitor response"]},
        {"type": "callout", "kind": "warning", "text": "Validate price sensitivity before the announcement goes out."},
    ])


def test_a_correction_that_guts_the_document_is_not_applied(monkeypatch):
    """The Think brief of the 2026-09-11 e2e run came back from its review
    as one KPI row on an empty page. A correction is held against the draft
    it corrects; less than half the content means the draft stands."""
    outline = {"title": "x", "audience": "", "purpose": "", "sections": [], "needs_current_facts": False, "assumptions": []}
    review = {"ok": False, "issues": [{"where": "Objective", "problem": "no effective date", "fix": "add it", "severity": "must"}]}
    gutted = _doc_json(blocks=[{"type": "kpis", "items": [{"label": "Price", "value": "$59"}]}])
    model = _Model([outline, _full_doc(), review, gutted])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("think", template_id="brief")))
    assert len(result.spec.body.blocks) == 6, "the reviewed draft, not the gutted correction"
    assert result.corrections == 1 and any("dropped most of the content" in w for w in result.warnings)
    assert "WHOLE document with every section" in model.calls[3]["messages"][-1]["content"]
    # When the reviewer asked for less, halving is the fix, not a fault.
    review = {"ok": False, "issues": [{"where": "all", "problem": "far too long for a brief", "fix": "cut it to the essentials", "severity": "must"}]}
    half = _doc_json(blocks=_full_doc()["blocks"][:2] + [{"type": "paragraph", "text": "Team tier moves to $59."}])
    model = _Model([outline, _full_doc(), review, half])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("think", template_id="brief")))
    assert len(result.spec.body.blocks) == 3 and not any("dropped most" in w for w in result.warnings)
    # A correction that swaps figures for placeholders (the Think deck of
    # 2026-09-11: "[Verified Current Monthly Revenue]" in every tile) is
    # refused the same way.
    review = {"ok": False, "issues": [{"where": "kpis", "problem": "$59 is not in the material", "fix": "state the assumption", "severity": "must"}]}
    bracketed = _doc_json(blocks=_full_doc()["blocks"][1:] + [{"type": "kpis", "items": [{"label": "Price", "value": "[Verified Price]"}]}])
    model = _Model([outline, _full_doc(), review, bracketed])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("think", template_id="brief")))
    assert result.spec.body.blocks[0].type == "kpis" and result.spec.body.blocks[0].items[0].value == "$59"
    assert any("replaced content with placeholders" in w for w in result.warnings)


def test_a_hollow_first_draft_is_repaired_once(monkeypatch):
    hollow = _doc_json(blocks=[{"type": "kpis", "items": [{"label": "Price", "value": "$59"}]}])
    model = _Model([hollow, _full_doc()])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", template_id="brief")))
    assert len(result.spec.body.blocks) == 6 and result.corrections == 1 and result.warnings == []
    assert "incomplete" in model.calls[1]["messages"][-1]["content"]
    model = _Model([hollow, hollow])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", template_id="brief")))
    assert any(w.startswith("the document is thin") for w in result.warnings)


def test_the_template_is_the_requests_decision_not_the_models(monkeypatch):
    """The Think brief of 2026-09-11 came back `generic` from its correction
    and rendered as a report. The words decided `brief`; the model writes
    content."""
    model = _Model([_doc_json(template_id="executive_report")])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", template_id="brief")))
    assert result.spec.body.template_id == "brief"
    # A template the kind does not know is left to the model's default.
    model = _Model([_doc_json(template_id="sop")])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", template_id="ceo")))
    assert result.spec.body.template_id == "sop"


def test_caps_trim_a_deck_with_a_warning_instead_of_refusing(monkeypatch):
    slides = [{"layout": "bullets", "title": f"Slide {i}", "bullets": ["a"]} for i in range(15)]
    model = _Model([{"title": "Deck", "template_id": "generic", "slides": slides}])
    monkeypatch.setattr(llm, "json_completion", model)
    req = _req("fast", kind="presentation", formats=["pptx", "pdf"])
    result = asyncio.run(C.compose(req))
    assert len(result.spec.body.slides) == T.EFFORT_BUDGETS["fast"].max_slides
    assert any("trimmed" in w for w in result.warnings)


def test_progress_is_reported_in_words(monkeypatch):
    monkeypatch.setattr(llm, "json_completion", _Model([_doc_json()]))
    seen = []

    async def progress(pct, detail):
        seen.append((pct, detail))

    asyncio.run(C.compose(_req("fast"), progress=progress))
    assert [d for _, d in seen][0] == "writing" and seen[-1][1] == "content ready"


def test_visual_review_sends_images_with_thinking_off(monkeypatch):
    model = _Model([{"ok": False, "issues": [{"page": 2, "kind": "clipped_text", "problem": "table cut", "fix": "split it"}]}])
    monkeypatch.setattr(llm, "json_completion", model)
    verdict = asyncio.run(C.visual_review([b"\x89PNG-one", b"\x89PNG-two"], kind="document", title="Pricing"))
    assert verdict["issues"][0]["page"] == 2
    call = model.calls[0]
    assert call["thinking"] is False
    content = call["messages"][0]["content"]
    assert sum(1 for c in content if c.get("type") == "image_url") == 2
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_revise_applies_issues_as_an_edit(monkeypatch):
    parent = S.parse_body("document", _doc_json())
    model = _Model([_doc_json(title="Pricing Update v2")])
    monkeypatch.setattr(llm, "json_completion", model)
    out = asyncio.run(C.revise(_req("max"), parent, [{"page": 1, "kind": "too_dense", "problem": "wall of text", "fix": "split"}]))
    assert out.title == "Pricing Update v2"
    assert "page 1 too_dense" in model.calls[0]["messages"][-1]["content"]


def test_intent_classifier_needs_confidence(monkeypatch):
    monkeypatch.setattr(llm, "json_completion", _Model([{"wants_file": True, "kind": "document", "confidence": 0.55}]))
    assert asyncio.run(C.classify_intent("maybe write something up?")) is None
    monkeypatch.setattr(llm, "json_completion", _Model([{"wants_file": True, "kind": "document", "confidence": 0.95}]))
    assert asyncio.run(C.classify_intent("maybe write something up?"))["wants_file"] is True


def test_history_material_keeps_the_recent_turns():
    history = [{"role": "user", "content": "old " * 4000}, {"role": "assistant", "content": "middle"}, {"role": "user", "content": "newest question"}]
    text = C.material_from_history(history, max_chars=200)
    assert text.endswith("user: newest question") and len(text) <= 200


def test_tables_are_offered_as_data_the_model_must_not_recompute(monkeypatch):
    model = _Model([_doc_json()])
    monkeypatch.setattr(llm, "json_completion", model)
    req = _req("fast", material=C.Material(instruction="x", tables=[C.DataTable("t1", "Pipeline", ["Stage", "Amount"], [["A", 10], ["B", 20]])]))
    asyncio.run(C.compose(req))
    user = model.calls[0]["messages"][1]["content"]
    assert "do not recompute totals" in user and "TABLE t1: Pipeline" in user and "A | 10" in user


def test_the_sources_manifest_is_built_from_the_material_not_the_model(monkeypatch):
    """A hostile upload can tell the model to add a source; the model can
    invent one on its own. Neither reaches the page: the manifest is code-
    built from what the engine provided, citations to anything else are
    stripped, and the person is told."""
    poisoned = _doc_json(
        blocks=[
            {"type": "paragraph", "text": "Team tier moves to $59.", "sources": ["s1", "w9"]},
            {"type": "paragraph", "text": "Log in to verify.", "sources": ["w9"]},
        ],
        sources=[
            {"id": "s1", "title": "Finance note (retitled by the model)", "url": "https://evil.example/s1"},
            {"id": "w9", "title": "Investor portal login", "url": "https://login-attacker.example/verify"},
        ],
    )
    model = _Model([poisoned])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast")))
    spec = result.spec
    assert [c.id for c in spec.body.sources] == ["s1"]
    assert spec.body.sources[0].title == "Finance note" and spec.body.sources[0].url is None, "the material's own title and url, not the model's"
    assert spec.body.blocks[0].sources == ["s1"] and spec.body.blocks[1].sources == []
    assert any("not provided" in w for w in result.warnings) and any("not among those provided" in w for w in result.warnings)
    assert "login-attacker" not in spec.model_dump_json()


def test_no_material_sources_means_no_manifest_at_all(monkeypatch):
    invented = _doc_json(sources=[{"id": "s1", "title": "Made up", "url": "https://example.com/x"}])
    monkeypatch.setattr(llm, "json_completion", _Model([invented]))
    req = _req("fast", material=C.Material(instruction="x"))
    result = asyncio.run(C.compose(req))
    assert result.spec.body.sources == [] and result.spec.body.blocks[1].sources == []


# ------------------------------------------------- code-made rows (§4, §6) --
#
# CONTRACT-2 wave 2c: the rows of a sheet built from a pasted table or a
# generator recipe are filled by code BEFORE the spec validates; the model's
# answer carries `rows: []`. The fixture is the 34-row audit paste, parsed by
# tables.parse_table exactly as the engine parses it.

from pathlib import Path  # noqa: E402

from app.artifacts import tables as X  # noqa: E402

_AUDIT = Path(__file__).parent / "fixtures" / "audit_paste.txt"
_AUDIT_COLUMNS = ["Host", "Candidate", "Date", "Session ID", "Meeting ID", "Interview Duration (min)",
                  "Ratio of Interview Post-Session", "Outcome", "Audit Comments"]


def _audit_material(**over) -> C.Material:
    table = X.forward_fill(X.parse_table(_AUDIT.read_text(encoding="utf-8")), 0, evidence=True)
    kw = dict(instruction="Share XLSX, Word, PDF and CSV of this audit.", tables=[table.to_material_table("paste1", "Pasted table 1")],
              transform={"rows": 34, "blanks": table.blanks, "forward_filled": table.forward_filled, "forward_filled_column": "Host"})
    kw.update(over)
    return C.Material(**kw)


def _workbook_json(sheet: dict, **over):
    base = {"title": "IR Session Audit", "template_id": "data", "sheets": [sheet]}
    base.update(over)
    return base


def _audit_sheet(**over):
    sheet = {"name": "Audit", "columns": [{"name": c} for c in _AUDIT_COLUMNS], "rows": [], "rows_from": "paste1"}
    sheet.update(over)
    return sheet


def test_trailing_blank_rows_of_a_code_made_sheet_are_left_out_with_a_note(monkeypatch):
    """A blank row at the END of the table is not one the XLSX can hold
    (openpyxl writes nothing for it, so the reopened sheet would count one
    row fewer than the spec and the CSV, and the render would be refused
    — reproduced in the 2026-09-12 review). Interior blank rows stay."""
    table = C.DataTable(id="paste1", title="t", columns=["Host", "Score"], rows=[["a", "1"], [None, None], ["b", "2"], [None, None], [None, ""]])
    material = C.Material(instruction="xlsx and csv of this", tables=[table])
    sheet = {"name": "Data", "columns": [{"name": "Host"}, {"name": "Score"}], "rows": [], "rows_from": "paste1"}
    model = _Model([_workbook_json(sheet)])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["xlsx", "csv"], template_id="data", material=material)))
    rows = result.spec.body.sheets[0].rows
    assert rows == [["a", "1"], [None, None], ["b", "2"]], "the interior blank row is the person's; the two trailing ones are not rows"
    assert any("2 blank rows at the end of the table were left out" in w for w in result.warnings), result.warnings


def test_rows_from_fills_the_pasted_rows_verbatim_before_validation(monkeypatch):
    """The model returns `rows_from: "paste1"` and no rows; the spec that
    validates carries all 34 rows, every blank cell blank (None), the
    forward-filled hosts, in the pasted order — and the result's
    transform says what was done."""
    model = _Model([_workbook_json(_audit_sheet())])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["xlsx", "csv", "docx", "pdf"], template_id="data", material=_audit_material())))
    sheet = result.spec.body.sheets[0]
    assert len(sheet.rows) == 34 and sheet.rows_from == "paste1" and result.model_calls == 1 and result.corrections == 0
    assert [c.name for c in sheet.columns] == _AUDIT_COLUMNS
    assert sheet.rows[0][:3] == ["Ravi Sharma", "Priya Nair", "2026-08-03"]
    assert sheet.rows[1][0] == "Ravi Sharma", "the host was forward-filled from the row above (evidence: a Host column, 25 blank continuation rows)"
    assert sheet.rows[2][2] is None and sheet.rows[8][2] is None and sheet.rows[8][3] is None, "blank dates and session ids stay blank"
    assert sum(1 for r in sheet.rows for c in r if c is None) == 19, "44 source blanks minus the 25 forward-filled hosts"
    assert result.transform["rows"] == 34 and result.transform["blanks"] == 19 and result.transform["forward_filled"] == 25
    assert "rewritten" not in result.transform
    # The prompt listed the table by id and shape and told the model never to retype rows.
    system = model.calls[0]["messages"][0]["content"]
    assert "TABLE paste1: 9 columns × 34 rows: Host, Candidate, Date" in system
    assert "rows_from" in system and "never retype" in system.lower()
    assert "rewrite: [{column, instruction}]" in system and "highlighted column" in system


def test_rows_from_with_the_wrong_column_count_takes_the_tables_columns_and_typed_rows_are_replaced(monkeypatch):
    sheet = _audit_sheet(columns=[{"name": "Host"}, {"name": "Candidate"}], rows=[["x", "y"]])
    monkeypatch.setattr(llm, "json_completion", _Model([_workbook_json(sheet)]))
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["csv"], template_id="data", material=_audit_material())))
    out = result.spec.body.sheets[0]
    assert [c.name for c in out.columns] == _AUDIT_COLUMNS and len(out.rows) == 34
    assert any("columns taken from the pasted table" in w for w in result.warnings)
    assert any("rows the model typed were replaced" in w for w in result.warnings)


def test_an_unknown_rows_from_id_is_repaired_once_naming_the_tables(monkeypatch):
    model = _Model([_workbook_json(_audit_sheet(rows_from="table_9")), _workbook_json(_audit_sheet())])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["csv"], template_id="data", material=_audit_material())))
    assert result.corrections == 1 and len(result.spec.body.sheets[0].rows) == 34
    repair = model.calls[1]["messages"][-1]["content"]
    assert "could not be filled" in repair and "'table_9' names no material table" in repair and "'paste1'" in repair
    # Twice wrong is a model failure, not a workbook with no rows.
    monkeypatch.setattr(llm, "json_completion", _Model([_workbook_json(_audit_sheet(rows_from="nope")), _workbook_json(_audit_sheet(rows_from="nope"))]))
    with pytest.raises(C.ComposeError) as exc:
        asyncio.run(C.compose(_req("fast", kind="workbook", formats=["csv"], template_id="data", material=_audit_material())))
    assert exc.value.category == "model_failure"


_CUSTOMER_COLUMNS = [{"name": "Customer ID"}, {"name": "Name"}, {"name": "Email"}, {"name": "City"}, {"name": "Plan"},
                     {"name": "Signup Date", "type": "date"}, {"name": "Seats", "type": "integer"}, {"name": "MRR", "type": "currency"}]


def _generator(rows=500, **over):
    gen = {"rows": rows, "seed": 7, "columns": [
        {"name": "Customer ID", "kind": "id", "pattern": "CUST-{n:05d}"},
        {"name": "Name", "kind": "name", "unique": True},
        {"name": "Email", "kind": "email"},
        {"name": "City", "kind": "choice", "values": ["Pune", "Mumbai", "Bengaluru", "Hyderabad"], "weights": [4, 3, 2, 1]},
        {"name": "Plan", "kind": "choice", "values": ["Free", "Team", "Enterprise"]},
        {"name": "Signup Date", "kind": "date", "start": "2025-01-01", "end": "2025-12-31"},
        {"name": "Seats", "kind": "int", "min": 1, "max": 250, "only_when": {"column": "Plan", "in": ["Team", "Enterprise"]}},
        {"name": "MRR", "kind": "float", "min": 0, "max": 5000, "decimals": 2},
    ]}
    gen.update(over)
    return gen


def test_generator_fills_exactly_500_rows_from_a_scripted_recipe(monkeypatch):
    """"500 sample customers": the model's answer is a schema and a recipe
    with rows: []; code makes exactly 500 validated rows — unique ids and
    names, blank Seats on the Free plan, dates in the window — and the
    generator stays on the sheet as provenance."""
    sheet = {"name": "Customers", "columns": _CUSTOMER_COLUMNS, "rows": [], "generator": _generator()}
    model = _Model([_workbook_json(sheet, title="Sample customers")])
    monkeypatch.setattr(llm, "json_completion", model)
    material = C.Material(instruction="Create a CSV of 500 sample customers.", row_count=500)
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["csv"], template_id="data", material=material)))
    out = result.spec.body.sheets[0]
    assert len(out.rows) == 500 and out.generator is not None and out.generator.rows == 500
    assert all(len(r) == 8 for r in out.rows)
    ids = [r[0] for r in out.rows]
    assert ids[0] == "CUST-00001" and ids[-1] == "CUST-00500" and len(set(ids)) == 500
    assert len({r[1] for r in out.rows}) == 500 and all("@example." in r[2] for r in out.rows)
    assert all((r[6] is None) == (r[4] == "Free") for r in out.rows), "only_when: seats only on paid plans"
    assert all("2025-01-01" <= r[5] <= "2025-12-31" for r in out.rows)
    assert result.transform == {"generated": 500} and result.model_calls == 1 and result.warnings == []
    assert "exactly 500 rows" in model.calls[0]["messages"][0]["content"] and "rows MUST be 500" in model.calls[0]["messages"][0]["content"]
    assert "figures not in the material" not in " ".join(result.warnings), "generated numbers are code's, not figures the model invented"


def test_generator_row_count_follows_the_request_and_recipes_follow_the_columns(monkeypatch):
    """The model forgot the count (rows: 100) and wrote the recipes in
    another order: the person's 500 wins with a note, the recipes are put
    in the sheet's column order, and the cells land under their headers."""
    gen = _generator(rows=100)
    gen["columns"] = list(reversed(gen["columns"]))
    sheet = {"name": "Customers", "columns": _CUSTOMER_COLUMNS, "rows": [], "generator": gen}
    monkeypatch.setattr(llm, "json_completion", _Model([_workbook_json(sheet)]))
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["csv"], template_id="data", material=C.Material(instruction="500 rows please", row_count=500))))
    out = result.spec.body.sheets[0]
    assert len(out.rows) == 500 and out.rows[0][0].startswith("CUST-") and isinstance(out.rows[0][7], float)
    assert any("were set to the 500 that were asked for" in w for w in result.warnings)


def test_a_generator_that_does_not_match_the_columns_is_repaired_once(monkeypatch):
    bad = _generator()
    bad["columns"] = bad["columns"][:-1]   # no recipe for MRR
    sheet = {"name": "Customers", "columns": _CUSTOMER_COLUMNS, "rows": [], "generator": bad}
    good = {"name": "Customers", "columns": _CUSTOMER_COLUMNS, "rows": [], "generator": _generator(rows=20)}
    model = _Model([_workbook_json(sheet), _workbook_json(good)])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["csv"], template_id="data", material=C.Material(instruction="20 rows"))))
    assert result.corrections == 1 and len(result.spec.body.sheets[0].rows) == 20
    assert "no recipe for column 'MRR'" in model.calls[1]["messages"][-1]["content"]


def test_a_pasted_table_is_not_cut_by_the_effort_cap(monkeypatch):
    """A 2,500-row pasted table at Fast (cap 2,000 for typed rows) is the
    person's table: code-made rows are held only to the hard ceiling."""
    columns = ["Id", "Value"]
    table = C.DataTable("paste1", "Pasted table 1", columns, [[str(i), i] for i in range(2500)])
    sheet = {"name": "Data", "columns": [{"name": "Id"}, {"name": "Value", "type": "integer"}], "rows": [], "rows_from": "paste1"}
    monkeypatch.setattr(llm, "json_completion", _Model([_workbook_json(sheet)]))
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["csv"], template_id="data", material=C.Material(instruction="x", tables=[table]))))
    assert len(result.spec.body.sheets[0].rows) == 2500 and not any("was cut" in w for w in result.warnings)


# ---------------------------------------------------------------- rewrite --


def _rewrite_reply(batch_messages, transform):
    """A scripted rewrite answer built from the batch the composer sent."""
    cells = json.loads(batch_messages[-1]["content"].split("Cells:\n", 1)[1])
    return {"rewrites": [{"row": c["row"], "text": transform(c["row"], c["text"])} for c in cells]}


def test_rewrite_column_is_rewritten_in_batches_by_row_id_and_unfaithful_replies_keep_the_original(monkeypatch):
    """The comment column is sent in batches of 40 (34 rows → one call,
    thinking off, its own schema), joined back by row id. A reply that
    loses the timestamp keeps the original with a warning naming the row;
    one that introduces a figure is refused the same way; one row is
    left out of the answer and keeps its original too."""
    sheet = _audit_sheet(rewrite=[{"column": "Audit Comments", "instruction": "concise professional audit comment"}])

    class _Scripted(_Model):
        async def __call__(self, messages, **kw):
            self.calls.append({"messages": messages, "schema": kw.get("schema_name"), "thinking": kw.get("thinking"), "max_tokens": kw.get("max_tokens"), "effort": kw.get("effort")})
            if kw.get("schema_name") == "artifact_rewrite":
                def fix(row, text):
                    if row == 0:
                        return "Confident candidate; answered every question. At 00:12:30 the host asked about system design; good explanation."
                    if row == 1:
                        return "Weak on basics; the host repeated a question twice."          # drops 00:05:10
                    if row == 2:
                        return "Very good communication; ratio 0.95; praised at 00:40:02."    # invents 0.95
                    return text.capitalize()
                reply = _rewrite_reply(messages, fix)
                reply["rewrites"] = [r for r in reply["rewrites"] if r["row"] != 3]           # row 4 left out
                return json.dumps(reply)
            return json.dumps(self.answers.pop(0))

    model = _Scripted([_workbook_json(sheet)])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["xlsx", "csv"], template_id="data", material=_audit_material())))
    rows = result.spec.body.sheets[0].rows
    assert len(rows) == 34 and result.model_calls == 2, "one compose call + one rewrite batch"
    rewrite_call = model.calls[1]
    assert rewrite_call["schema"] == "artifact_rewrite" and rewrite_call["thinking"] is False and 600 <= rewrite_call["max_tokens"] <= 12_000
    assert "concise professional audit comment" in rewrite_call["messages"][0]["content"]
    assert "keep every timestamp" in rewrite_call["messages"][0]["content"].lower()
    assert rows[0][8].startswith("Confident candidate") and "00:12:30" in rows[0][8]
    assert rows[1][8] == "weak on basics. host had to repeat questn twice (00:05:10). no follow up", "the timestamp went missing: original kept"
    assert rows[2][8].startswith("very good comunication"), "a figure the original did not have: original kept"
    assert rows[3][8].startswith("session cut short"), "no reply for the row: original kept"
    assert rows[4][8] == "Solid. minor grammer issues but technically strong"
    assert rows[0][:8] == ["Ravi Sharma", "Priya Nair", "2026-08-03", "S-1041", "MTG-77812", "42", "0.81", "Selected"], "every other cell untouched"
    assert result.transform["rewritten"] == 31 and result.transform["kept_original"] == 3 and result.transform["rewrite_columns"] == ["Audit Comments"]
    assert any("row 2: kept the original (timestamp 00:05:10 missing)" in w for w in result.warnings)
    assert any("row 3 kept the original (the rewrite changed a figure)" in w for w in result.warnings)
    assert any("2 row(s) had no rewrite and keep the original: rows 3, 4" in w for w in result.warnings), "the refused figure row and the row left out"


def test_rewrite_runs_in_forty_row_batches_and_a_failed_batch_keeps_its_originals(monkeypatch):
    rows = [[f"c{i}", f"comment number {i} at 00:0{i % 10}:00"] for i in range(90)]
    table = C.DataTable("paste1", "Pasted table 1", ["Candidate", "Comment"], rows)
    sheet = {"name": "Data", "columns": [{"name": "Candidate"}, {"name": "Comment"}], "rows": [], "rows_from": "paste1",
             "rewrite": [{"column": "comment", "instruction": "tidy"}]}
    calls = {"n": 0}

    class _Flaky(_Model):
        async def __call__(self, messages, **kw):
            self.calls.append({"messages": messages, "schema": kw.get("schema_name")})
            if kw.get("schema_name") == "artifact_rewrite":
                calls["n"] += 1
                if calls["n"] == 2:
                    return "not json"
                return json.dumps(_rewrite_reply(messages, lambda r, t: t.upper()))
            return json.dumps(self.answers.pop(0))

    monkeypatch.setattr(llm, "json_completion", _Flaky([_workbook_json(sheet)]))
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["csv"], template_id="data", material=C.Material(instruction="x", tables=[table]))))
    out = result.spec.body.sheets[0].rows
    assert calls["n"] == 3 and result.model_calls == 4
    assert out[0][1] == "COMMENT NUMBER 0 AT 00:00:00" and out[39][1].isupper()
    assert out[40][1] == "comment number 40 at 00:00:00" and out[79][1] == "comment number 79 at 00:09:00", "the failed batch kept every original"
    assert out[80][1].isupper()
    assert result.transform["rewritten"] == 50 and result.transform["kept_original"] == 40
    assert any("40 rows could not be rewritten (the model call failed)" in w for w in result.warnings)


# --------------------------------------------------------------- coverage --


def test_requested_sections_are_parsed_from_include_with_and_sections_lists():
    assert C.requested_sections("Create a PDF report on the audit. Include an executive summary, key risks, the roadmap and next steps.") == ["executive summary", "key risks", "roadmap", "next steps"]
    assert C.requested_sections("Write a proposal covering the need, approach and pricing; sections: timeline, team.") == ["timeline", "team", "need", "approach", "pricing"]
    assert C.requested_sections("Make a deck with 3 slides and our logo") == []
    assert C.requested_sections("Turn this into an Excel tracker with the three plans and a total row.") == [], "'with' needs a list of sections, not table parts"
    assert C.requested_sections("Include a risks section.") == ["risks"]
    assert C.requested_sections("") == [] and C.requested_sections("Create a brief.") == []


def _draft(headings):
    blocks = []
    for h in headings:
        blocks.append({"type": "heading", "level": 1, "text": h})
        blocks.append({"type": "paragraph", "text": f"Content of {h} with the $59 price.", "sources": ["s1"]})
    return _doc_json(blocks=blocks)


def test_missing_requested_sections_get_one_correction_naming_them_at_every_effort(monkeypatch):
    """CONTRACT-2 §11: a draft without "risks" and "roadmap" is corrected
    ONCE, the correction names exactly the missing sections, and this
    happens at Fast even after its one budgeted correction was spent."""
    instruction = "Create a PDF report on the pricing change. Include an executive summary, risks, the roadmap and next steps."
    partial = _draft(["Executive Summary", "Next Steps"])
    holey = _draft(["Executive Summary", "Next Steps"])
    holey["blocks"][1]["text"] = "TBD"
    full = _draft(["Executive Summary", "Key Risks", "Roadmap for FY27", "Next Steps"])
    model = _Model([holey, partial, full])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", instruction=instruction, material=C.Material(instruction=instruction, sources=[C.Source("s1", "Finance note", "Team tier to $59.")]))))
    assert result.corrections == 2 and len(model.calls) == 3, "the placeholder correction, then the coverage correction — past Fast's budget of one"
    prompt = model.calls[2]["messages"][-1]["content"]
    assert "does not have: risks, roadmap" in prompt and "executive summary" not in prompt.lower().split("does not have")[1]
    assert [b.text for b in result.spec.body.blocks if b.type == "heading"] == ["Executive Summary", "Key Risks", "Roadmap for FY27", "Next Steps"]
    assert not any("requested sections" in w for w in result.warnings)
    # Still missing after the one correction: a warning, no loop.
    model = _Model([partial, partial])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", instruction=instruction, material=C.Material(instruction=instruction, sources=[C.Source("s1", "Finance note", "Team tier to $59.")]))))
    assert len(model.calls) == 2 and any(w == "requested sections not found in the document: risks, roadmap" for w in result.warnings)
    # Every section present: no correction at all.
    model = _Model([full])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", instruction=instruction, material=C.Material(instruction=instruction, sources=[C.Source("s1", "Finance note", "Team tier to $59.")]))))
    assert len(model.calls) == 1 and result.corrections == 0


def test_section_cap_never_falls_below_the_requested_sections_plus_two():
    spec = S.parse_body("document", _draft([f"Section {i}" for i in range(10)]))
    budget = T.EFFORT_BUDGETS["fast"]           # max_sections 8
    assert any("10 top-level sections" in w for w in C._enforce_caps(spec, budget))
    assert C._enforce_caps(spec, budget, requested=[f"section {i}" for i in range(8)]) == [], "8 requested + 2 = a floor of 10"


def test_an_edit_of_a_code_made_workbook_reads_the_parent_without_its_rows(monkeypatch):
    """C6 (discovery of 2026-09-12): the edit prompt used to embed the
    parent JSON with all 500 generated rows, and the model retyped them
    past Fast's ceiling. The parent travels with rows: [] for a copied or
    generated sheet; the model answers the same way; code fills again."""
    table = X.forward_fill(X.parse_table(_AUDIT.read_text(encoding="utf-8")), 0)
    material = C.Material(instruction="Rename the sheet to Sessions.", tables=[table.to_material_table("paste1")])
    parent = S.parse_body("workbook", {"title": "Audit", "template_id": "data", "sheets": [_audit_sheet(rows=[list(r) for r in table.rows])]})
    assert len(parent.body.sheets[0].rows) == 34
    model = _Model([{"title": "Audit", "template_id": "data", "sheets": [_audit_sheet(name="Sessions")]}])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req("fast", kind="workbook", formats=["csv"], template_id="data", material=material,
                                        operation="edit", parent_spec=parent, instruction="Rename the sheet to Sessions.")))
    system = model.calls[0]["messages"][0]["content"]
    assert '"rows_from": "paste1"' in system and system.count("MTG-") == 0, "the parent's copied rows are not in the edit prompt"
    assert "EDITING an existing document" in system
    sheet = result.spec.body.sheets[0]
    assert sheet.name == "Sessions" and len(sheet.rows) == 34 and sheet.rows[8][3] is None
    # A generated parent likewise; a typed workbook still travels whole.
    gen_sheet = {"name": "Customers", "columns": _CUSTOMER_COLUMNS, "rows": [], "generator": _generator(rows=20)}
    raw = {"title": "S", "template_id": "data", "sheets": [gen_sheet]}
    assert C._fill_code_made_rows(raw, C.ComposeRequest(kind="workbook", formats=["csv"], template_id="data", effort="fast", material=C.Material(instruction="x")), []) == []
    generated = S.parse_body("workbook", raw)
    assert len(generated.body.sheets[0].rows) == 20
    assert '"rows": []' in C.body_json_for_prompt(generated) and "CUST-00001" not in C.body_json_for_prompt(generated)
    typed = S.parse_body("workbook", {"title": "w", "sheets": [{"name": "A", "columns": [{"name": "x"}], "rows": [["typed"]]}]})
    assert "typed" in C.body_json_for_prompt(typed) and C.body_json_for_prompt(typed) == typed.body.model_dump_json()
    doc = S.parse_body("document", _doc_json())
    assert C.body_json_for_prompt(doc) == doc.body.model_dump_json()
