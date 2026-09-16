"""A chart survives the whole pipeline, and a follow-up inherits its shape.

Production 2026-09-16: "visualise this table on pie chart" (the table was in
the assistant's own previous answer) came back as a Word file AND a PDF, and
the follow-ups that asked for another chart type made a second artifact with
new numbers. Four seams are pinned here:

  a) the INTENT's verdict decides the format, not a second reading of the text
     (formats.decide(chart_request=…) — engines/artifact.py passes it);
  b) the classifier may answer png/svg (intent.verdict_to_intent);
  c) the published version records its SHAPE (artifact_versions.deliverable,
     V39) so the gate can read it on the next turn;
  d) a follow-up that changes only the chart TYPE is an edit that re-renders
     from the binding the version already carries — 0 model calls.

"A report with a chart" stays a report throughout. Offline: the planner and
the composer are stubs and raise if the model is reached.
"""
from __future__ import annotations

import asyncio
import hashlib
import os

import pytest

from app import db, metrics
from app.artifacts import db as adb
from app.artifacts import deliverable as D
from app.artifacts import edits as E
from app.artifacts import formats as F
from app.artifacts import intent as I
from app.artifacts import intent_llm as IL
from app.artifacts import pipeline
from app.artifacts import spec as S
from app.artifacts import types as T
from app.config import settings

PIE_BINDING = {"table_id": "paste1", "x": "State", "y": ["Count (Approx)"], "agg": "sum"}


def chart_document(chart_type: str = "pie", title: str = "Records by state"):
    """The incident's deliverable: one chart, bound to the pasted table."""
    return S.parse_body("document", {"title": "Records by state", "blocks": [
        {"type": "chart", "chart": {
            "type": chart_type, "title": title, "data": dict(PIE_BINDING),
            "categories": ["Texas", "Missouri", "Illinois"],
            "series": [{"name": "Count", "values": [21.0, 11.0, 9.0]}],
        }},
    ]})


# --------------------------------------------- (a) the verdict, not the text --


def test_the_gates_verdict_decides_the_chart_deliverable():
    """formats.py's own chart words are the smaller set: measured on this
    branch they know neither "waterfall" nor a chart named as a noun with no
    verb, so both asks fell through to the document default — a Word file and
    a PDF for a one-line chart request."""
    for text in ("give me a waterfall showing revenue by quarter", "scatter of Salary vs Experience"):
        assert I.decide(text, has_assistant_answer=True).chart_request is True, text
        assert F.decide(text).formats == ["docx", "pdf"], text
        assert F.decide(text, chart_request=True).formats == ["png"], text
        assert F.decide(text, chart_request=True).kind == "document", text


def test_a_report_with_a_chart_is_still_a_report():
    """AS3/PR #74 behaviour: the verdict says "chart", the words name another
    deliverable, and the document wins — the chart lives inside it."""
    for text in ("write a Word report on Q3 with a pie chart", "a report with a pie chart",
                 "make a deck for the board with a bar chart"):
        d = F.decide(text, chart_request=True)
        assert d.formats not in (["png"], ["svg"]), text
        assert d.kind in ("document", "presentation") and d.formats == F.decide(text).formats, text


def test_a_verdict_of_no_chart_does_not_invent_an_image_but_a_named_one_still_wins():
    # A verdict of False is a NO-OP, not a veto: the words still decide. It
    # used to blank this png and hand back the incident's own docx+pdf, and
    # it bought nothing for it — a real negation ("don't put this in a pie
    # chart") is action='none' at the gate, so the engine never runs.
    assert F.decide("visualise this table on pie chart", chart_request=False).formats == ["png"]
    # A verdict of False adds NO chart where the words name none.
    assert F.decide("write up what we found", chart_request=False).formats == F.decide("write up what we found").formats
    # A format the person NAMED is theirs whatever the verdict says.
    assert F.decide("pie chart of this as an svg", chart_request=False).formats == ["svg"]
    assert F.decide("make a pie chart of this as png", chart_request=False).formats == ["png"]


def test_the_text_rules_are_unchanged_when_no_verdict_is_passed():
    """Every caller that has no verdict reads exactly what it read before."""
    for text in ("visualise this table on pie chart", "give me a waterfall showing revenue by quarter",
                 "write a Word report on Q3 with a pie chart", "Create a professional PDF about this.",
                 "Create a dataset of 500 customers"):
        assert F.decide(text, chart_request=None).formats == F.decide(text).formats, text


def test_a_classifier_verdict_of_no_chart_does_not_veto_the_texts_own_png():
    """The verifier's BLOCKER, 2026-09-16: the gate's verdict ADDS to this
    module's reading, it never REPLACES it. "can you put this in a pie chart"
    is rule-decided action='none' (rule 'no-request'), so _should_consult
    sends it to the classifier; a verdict of action='create' with
    chart_request=False used to blank the png the sentence's own words had
    produced, and the turn answered a one-line pie-chart ask with a Word file
    AND a PDF — the incident verbatim. Measured before the fix on this tree:
    202 of a 230-phrasing sweep (10 chart verbs x 23 chart nouns) lost the
    png to the veto."""
    for text in ("can you put this in a pie chart", "can you turn this into a bar graph",
                 "could you see this as a donut chart", "visualise this table on pie chart"):
        assert F.decide(text).formats == ["png"], text
        assert F.decide(text, chart_request=False).formats == F.decide(text).formats, text


def test_the_classifier_path_keeps_the_png_end_to_end():
    """The whole seam, not just the function: rules say 'none', the hook
    answers create/chart_request=False, and the png still survives."""
    text = "can you put this in a pie chart"
    rules = I.decide(text, has_assistant_answer=True)
    assert rules.action == "none" and rules.rule == "no-request", rules
    assert I._should_consult(rules, text) is True

    async def hook(_text, **_kw):
        return IL.IntentVerdict(action="create", target="answer", formats=[],
                                chart_request=False, confidence=0.9)

    out = asyncio.run(I.decide_with_hook(text, hook, has_assistant_answer=True))
    assert out.action == "create" and out.llm_used is True
    assert F.decide(text, chart_request=out.chart_request).formats == ["png"]


def test_a_chart_less_parent_sends_the_type_change_to_the_planner():
    """The pre-plan is only an answer when there IS a chart. On a chart-less
    document the old pre-plan emitted SetChart and _apply_chart raised "the
    file has no chart", so the turn's only outcome was a refusal — while the
    planner can simply ADD the chart. edits.plan() reaches preplan
    unconditionally, so the UI's "Edit with a prompt" on any artifact took
    this path."""
    plain = S.parse_body("document", {"title": "Records by state", "blocks": [
        {"type": "heading", "level": 1, "text": "Records by state"},
        {"type": "paragraph", "text": "Texas has the most records."},
    ]})
    for text in ("make it a bar chart instead", "as a donut chart", "now do the same as a line chart",
                 "make it a pie chart", "turn it into a heat map chart",
                 "show the chart as a waterfall chart"):
        assert E.preplan(text, plain) is None, text
    # ...and the parent that DOES hold a chart is untouched.
    assert E.preplan("make it a bar chart instead", chart_document()) is not None


def test_an_unreadable_stored_shape_never_raises():
    """main.py reads this on the chat streaming event loop with no guard
    (app/main.py: `_as3_deliverable.of_version(...).to_json()`), so a corrupt
    or foreign-written artifact_versions.deliverable row must be ignored, not
    break the turn. Measured before the fix: from_json({'charts': 'many'})
    raised ValueError: invalid literal for int() with base 10: 'many'."""
    assert D.from_json({"charts": "many"}) == D.Deliverable()
    assert D.from_json({"charts": None}).charts == 0
    assert D.from_json({"charts": 2.0}).charts == 2
    # A bare string is not a list of formats: it used to decode to ('p','n','g').
    assert D.from_json({"formats": "png"}).formats == ()
    assert D.from_json({"formats": ["png"]}).formats == ("png",)
    assert D.of_version({"deliverable": {"charts": "many"}, "formats": "png"}) == D.Deliverable()
    assert D.of_version({"deliverable": {"charts": "many"}, "formats": ["png"]}).formats == ("png",)


# ------------------------------------------ (b) the classifier may say chart --


def test_the_classifier_may_answer_with_a_chart():
    """intent_llm has offered png/svg since the AS3 integration and its prompt
    names "PNG/SVG charts"; verdict_to_intent dropped them at the exit, so the
    one escape hatch for a chart ask the rules cannot read could never make
    one."""
    assert "png" in IL.FORMATS and "svg" in IL.FORMATS
    rules = I.decide("kuch aisa banao jisme ye saaf dikh jaye", has_assistant_answer=True)
    for fmt in ("png", "svg"):
        verdict = IL.IntentVerdict(action="create", formats=[fmt], target="conversation", confidence=0.9)
        out = I.verdict_to_intent(verdict, rules, has_artifacts=False, has_assistant_answer=True)
        assert out is not None and out.formats == [fmt], fmt
        # png and svg exist in this product only as chart images.
        assert out.chart_request is True, fmt
    # A document format is still a document format, and a format this build
    # cannot write is still dropped.
    verdict = IL.IntentVerdict(action="create", formats=["docx", "zip"], target="conversation", confidence=0.9)
    assert I.verdict_to_intent(verdict, rules, has_artifacts=False, has_assistant_answer=True).formats == ["docx"]


# -------------------------------------------------- (c) the recorded shape --


def test_the_shape_of_a_published_chart_is_its_type_and_its_binding():
    shape = D.of_spec(chart_document(), kind="document", formats=["png"])
    assert (shape.kind, shape.formats, shape.charts) == ("document", ("png",), 1)
    assert shape.chart_type == "pie" and shape.chart_title == "Records by state"
    assert shape.binding["table_id"] == "paste1" and shape.binding["y"] == ["Count (Approx)"]
    assert shape.is_chart and shape.has_chart
    # No values: they are recomputed from the binding every time the spec is
    # resolved, and a copy here would be a second truth that ages.
    assert "categories" not in shape.to_json() and "series" not in shape.to_json()
    assert D.from_json(shape.to_json()) == shape


def test_a_report_that_holds_a_chart_is_not_a_chart_deliverable():
    doc = S.parse_body("document", {"title": "Q3", "blocks": [
        {"type": "paragraph", "text": "Revenue grew."},
        {"type": "chart", "chart": {"type": "bar", "title": "Revenue", "data": dict(PIE_BINDING)}},
    ]})
    shape = D.of_spec(doc, kind="document", formats=["docx", "pdf"])
    assert shape.has_chart and not shape.is_chart
    assert shape.chart_type == "bar"


def test_two_charts_carry_no_binding_because_the_same_names_neither():
    doc = S.parse_body("document", {"title": "Q3", "blocks": [
        {"type": "chart", "chart": {"type": "bar", "title": "A", "data": dict(PIE_BINDING)}},
        {"type": "chart", "chart": {"type": "line", "title": "B", "data": dict(PIE_BINDING)}},
    ]})
    shape = D.of_spec(doc, kind="document", formats=["docx", "pdf"])
    assert shape.charts == 2 and shape.binding is None and shape.chart_type == ""


def test_a_version_published_before_v39_still_says_what_it_is():
    row = {"deliverable": {}, "files": [{"format": "png"}], "formats": ["png"], "kind": "document"}
    assert D.of_version(row).is_chart
    assert D.of_version({"deliverable": {}, "files": [], "formats": ["docx", "pdf"]}).is_chart is False
    assert D.of_version(None) == D.Deliverable() and D.from_json("not a shape") == D.Deliverable()


# ------------------------------------------------- (d) the follow-up shapes --


CHART_SHAPE = D.Deliverable(kind="document", formats=("png",), charts=1, chart_type="pie", binding=dict(PIE_BINDING)).to_json()
REPORT_SHAPE = D.Deliverable(kind="document", formats=("docx", "pdf"), charts=1, chart_type="bar", binding=dict(PIE_BINDING)).to_json()
NO_CHART = D.Deliverable(kind="document", formats=("docx", "pdf"), charts=0).to_json()
FOLLOW_UP = dict(has_artifacts=True, has_assistant_answer=True, last_turn_is_artifact=True)


@pytest.mark.parametrize("text", [
    "make it a bar chart instead",
    "now do the same as a line chart",
    "same thing but as a bar chart",
    "as a donut chart",
    "make it a horizontal bar chart instead",
])
def test_a_type_change_after_a_chart_edits_that_chart(text):
    intent = I.decide(text, last_deliverable=CHART_SHAPE, **FOLLOW_UP)
    assert (intent.action, intent.rule, intent.reference) == ("edit", "edit-chart-type", "latest"), text
    assert intent.chart_request is True


def test_the_same_words_over_a_report_edit_the_report_not_a_new_file():
    intent = I.decide("show the chart as a line graph", last_deliverable=REPORT_SHAPE, **FOLLOW_UP)
    assert intent.action == "edit" and intent.rule == "edit-chart-type"


@pytest.mark.parametrize("text,shape", [
    # Its own data named: a new chart, not a change to the last one.
    ("visualise this table on pie chart", CHART_SHAPE),
    ("draw a bar chart of this table", CHART_SHAPE),
    ("give me a pie chart of the new numbers", CHART_SHAPE),
    # Nothing chart-shaped was made: there is nothing to inherit.
    ("make it a bar chart instead", NO_CHART),
    ("make it a bar chart instead", None),
])
def test_a_new_chart_is_still_a_new_chart(text, shape):
    intent = I.decide(text, last_deliverable=shape, **FOLLOW_UP)
    assert intent.action == "create" and intent.rule != "edit-chart-type", (text, intent.rule)


def test_the_other_follow_up_shapes_are_untouched_by_the_chart_rule():
    assert I.decide("make it a docx", last_deliverable=CHART_SHAPE, **FOLLOW_UP).action == "convert"
    assert I.decide("make the headings dark blue", last_deliverable=CHART_SHAPE, **FOLLOW_UP).rule == "edit-style"
    assert I.decide("undo that", last_deliverable=CHART_SHAPE, **FOLLOW_UP).rule == "restore-version"


def test_an_unreadable_shape_is_no_shape_and_never_raises():
    assert I.decide("make it a bar chart instead", last_deliverable=object(), **FOLLOW_UP).action == "create"
    assert I.decide("make it a bar chart instead", last_deliverable=CHART_SHAPE, **FOLLOW_UP).action == "edit"


# ------------------------------- (d) the type change costs no model call -----


@pytest.mark.parametrize("text,expected", [
    ("make it a bar chart instead", "bar"),
    ("now do the same as a line chart", "line"),
    ("as a donut chart", "donut"),
    ("redo the chart as a waterfall chart", "waterfall"),
    ("make it a horizontal bar chart", "horizontal_bar"),
    ("make it a heat map chart", "heatmap"),
])
def test_a_type_change_is_planned_deterministically(text, expected):
    async def no_planner(messages, schema, timeout_s):
        raise AssertionError("the planner must not be called for a type change")

    plan = asyncio.run(E.plan(text, chart_document(), call=no_planner))
    assert plan.planner == "deterministic" and plan.model_calls == 0, text
    assert [type(o).__name__ for o in plan.ops] == ["SetChart"]
    assert plan.ops[0].patch == {"type": expected}


@pytest.mark.parametrize("text", [
    "add a bar chart of revenue by month",
    "make the bar chart title red",
    "make it a bar chart instead and sort it descending",
    "put a pie chart on slide 3",
])
def test_more_than_a_type_change_still_goes_to_the_planner(text):
    plan = E.preplan(text, chart_document())
    assert plan is None or [type(o).__name__ for o in plan.ops] != ["SetChart"], text


def test_the_type_change_re_renders_from_the_same_binding():
    """chart_spec.apply_patch clears the computed categories and series, so
    chart_data re-runs the binding the parent version carries; the model is
    never asked for the numbers a second time."""
    parent = chart_document()
    plan = E.preplan("make it a bar chart instead", parent)
    out = E.apply(parent, plan, instruction="make it a bar chart instead")
    chart = out.spec.body.blocks[0].chart
    assert chart.type == "bar"
    assert chart.data.model_dump(mode="json")["table_id"] == "paste1"
    assert chart.data.x == "State" and chart.data.y == ["Count (Approx)"] and chart.data.agg == "sum"
    # Cleared, so code recomputes them — never carried over from the pie.
    assert chart.categories == [] and chart.series == []
    assert out.applied == ["bar chart"]


# --------------------------------------------------- the turn, end to end ----


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "artifact_lease_ttl_s", 60.0)
    monkeypatch.setattr(settings, "artifact_stage_timeout_s", 30.0)
    monkeypatch.setattr(settings, "artifact_render_timeout_s", 30.0)
    monkeypatch.setattr(settings, "artifact_min_free_mb", 1)
    monkeypatch.setattr(settings, "artifact_user_quota_mb", 1024)
    pipeline.reset_for_tests()
    pipeline.install_busy_probe(None)
    metrics.reset()
    yield
    pipeline.reset_for_tests()
    pipeline.set_composer(None)


@pytest.fixture()
def owner():
    return int(db.create_user("chart-deliverable-owner", "hash"))


def _install(monkeypatch, seen):
    async def stub_composer(ctx):
        seen.append({"kind": ctx.kind, "formats": list(ctx.formats), "operation": ctx.operation})
        await ctx.progress_stage("intent", "done", "")
        await ctx.progress_stage("gather", "done", "")
        await ctx.progress_stage("outline", "skipped", "")
        return chart_document()

    async def fake_render(work_dir, spec, formats, title_slug, version, effort):
        files = []
        for fmt in formats:
            name = f"{title_slug}-v{version}.{fmt}"
            body = f"{fmt} of {spec.title}".encode() * 8
            with open(os.path.join(work_dir, name), "wb") as fh:
                fh.write(body)
            files.append({"format": fmt, "filename": name, "size": len(body),
                          "sha256": hashlib.sha256(body).hexdigest(), "pages": None})
        with open(os.path.join(work_dir, T.PREVIEW_PDF_NAME), "wb") as fh:
            fh.write(b"%PDF-1.7 preview")
        return {"files": files, "preview_pdf": T.PREVIEW_PDF_NAME, "preview_kind": "pages", "preview_pages": 1,
                "warnings": [], "validation": {"reopened": True}, "chart_files": [], "timings": {}}

    pipeline.set_composer(stub_composer)
    monkeypatch.setattr(pipeline, "_render_in_subprocess", fake_render)
    monkeypatch.setattr(pipeline, "_page_count", lambda pdf: 1)
    monkeypatch.setattr(pipeline, "_rasterise_page", lambda pdf, page, width: b"\x89PNG")


def test_a_chart_ask_delivers_a_chart_and_the_version_records_its_shape(owner, monkeypatch):
    from app.engines import artifact as engine

    seen, events = [], []
    _install(monkeypatch, seen)

    async def emit(kind, data):
        events.append((kind, data))

    text = "give me a waterfall showing revenue by quarter"
    intent = I.decide(text, has_assistant_answer=True)
    assert intent.chart_request is True, "the gate saw the chart"
    asyncio.run(engine.run_artifact_engine(text, [{"role": "assistant", "content": "Q1 120, Q2 140"}], emit,
                                           intent=intent, conversation_id="conv-chart", user_id=owner,
                                           generation_id="gen-chart", effort="fast", mode="assistant"))
    meta = [d for k, d in events if k == "meta"][0]
    ref = meta["artifacts"][0]
    # The deliverable is the chart, not a Word file and a PDF nobody asked for.
    assert [f["format"] for f in ref["files"]] == ["png"]
    assert seen[0]["formats"] == ["png"]

    row = adb.get_version(ref["artifact_id"], 1, owner)
    shape = D.of_version(row)
    assert shape.is_chart and shape.chart_type == "pie" and shape.charts == 1
    assert shape.binding["table_id"] == "paste1" and shape.binding["x"] == "State"
    # And the gate reads that shape on the next turn.
    follow_up = I.decide("make it a bar chart instead", has_artifacts=True, has_assistant_answer=True,
                         last_turn_is_artifact=True, last_deliverable=shape.to_json())
    assert follow_up.action == "edit" and follow_up.rule == "edit-chart-type"
