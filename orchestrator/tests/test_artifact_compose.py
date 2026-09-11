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
