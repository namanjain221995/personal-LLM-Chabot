"""Who decides how long a document is.

THE OWNER, 2026-09-27: "Our ai decide it own What need ?? there is No token
limit for docs and for sheet ?? or for any think ??"

WHAT WAS THERE. A document's size came from `length.parse_size` — a size
WORD the person typed — or from `compose.target_for`, which multiplied the
sections a request NUMBERED by 400. Every other request was sized at zero,
and zero meant the prompt carried no size line at all and kept the effort's
own tone. Measured on the running container's own code (md5-identical to
origin/dev) with a scripted model, "Write a technical report on migrating
our monolith to microservices.":

    target                 0 words, explicit=False, phrase=''
    prompt section limit   at most 8 top-level sections
    tone line              "Be concise and concrete."   (Fast)
    document written       735 words across 6 sections, at fast, think AND max

AND THE ASYMMETRY. A size the PERSON typed bought the sectioned writer at
any effort; a size the product worked out from the fifteen sections they
numbered bought it only where `budget.outline_pass` was already true. The
same request, same words, measured on the same tree:

    fast    735 words, 6 of 15 sections, 9 reported missing, 2 model calls
    think   4,232 words, 15 of 15 sections, 17 model calls

The person who wrote out their whole table of contents and no word count
got the worse document, and which document they got depended on a dropdown.

WHAT IS THERE NOW. When nothing in the request decides the size, the MODEL
decides it: one plan call says how many sections the work has and how many
words each needs, and `size_from_plan` — pure, so every clamp below is
pinned without a model — sums those numbers. Code computes everything that
follows and nothing that precedes. The three paths that read
`target.explicit` no longer do.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app import llm
from app.artifacts import compose as C
from app.artifacts import length as L
from app.artifacts import spec as S
from app.artifacts import types as T

@pytest.fixture(autouse=True)
def _restore_the_model():
    """Several tests below install their stub with a plain assignment rather
    than monkeypatch, because they drive `compose` more than once. This puts
    the real `llm.json_completion` back either way, so a stub cannot leak
    into the file that runs after this one."""
    real = llm.json_completion
    yield
    llm.json_completion = real


NO_SHAPE = "Write a technical report on migrating our monolith to microservices."

FIFTEEN = ["Executive Summary", "Current Architecture", "Target Architecture", "Service Boundaries",
           "Data Strategy", "API Gateway", "Observability", "Deployment Pipeline", "Testing Strategy",
           "Security", "Migration Phases", "Risks and Mitigations", "Cost Model", "Team and Ownership",
           "Recommendations"]

#: What the model judges each part of this report needs. Not one number
#: repeated: an executive summary is not a migration plan, and the whole
#: point of asking is that it can say so.
PLAN = {"Executive Summary": 350, "Current Architecture": 900, "Target Architecture": 1_100,
        "Service Boundaries": 950, "Data Strategy": 1_000, "API Gateway": 700, "Observability": 750,
        "Deployment Pipeline": 800, "Testing Strategy": 750, "Security": 850, "Migration Phases": 1_200,
        "Risks and Mitigations": 900, "Cost Model": 700, "Team and Ownership": 500, "Recommendations": 600}
PLAN_TOTAL = sum(PLAN.values())          # 12,050

#: The same judgement inside the ceiling a bare one-line request buys at Fast
#: (`compose.size_bounds` → 8 sections, 8 x PLANNED_SECTION_CEILING = 5,400).
#: Used wherever a test needs the model's own numbers to survive untouched:
#: PLAN itself is 12,050 over fifteen sections, which is more than a request
#: of one sentence justifies at any effort, and it is CUT — out loud — by
#: `size_from_plan`.
FITS = {"Executive Summary": 350, "Current Architecture": 900, "Target Architecture": 1_100,
        "Service Boundaries": 950, "Data Strategy": 800, "API Gateway": 500, "Observability": 450,
        "Recommendations": 350}
FITS_TOTAL = sum(FITS.values())          # 5,400


class _Model:
    """A scripted `llm.json_completion` that records every prompt."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, messages, *, json_schema=None, schema_name="", temperature=0.0, max_tokens=None,
                       thinking=False, effort=None):
        self.calls.append({"schema": schema_name, "messages": messages, "max_tokens": max_tokens,
                           "plan_cap": ((json_schema or {}).get("properties", {})
                                        .get("sections", {}).get("maxItems"))})
        if not self.answers:
            raise AssertionError(f"the model was called more times than the script allows ({schema_name})")
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer)

    def named(self, schema):
        return [c for c in self.calls if c["schema"] == schema]


def _plan(words_by_heading):
    return {"title": "Monolith to Microservices", "audience": "the platform team",
            "purpose": "how to migrate", "needs_current_facts": False, "assumptions": [],
            "sections": [{"heading": h, "purpose": f"what {h} covers", "elements": ["paragraphs"],
                          "words": w} for h, w in words_by_heading.items()]}


def _prose(heading, words):
    out = [{"type": "heading", "level": 1, "text": heading}]
    left = int(words)
    while left > 0:
        out.append({"type": "paragraph", "text": " ".join(["service"] * min(500, left))})
        left -= 500
    return out


def _section(heading, words):
    return {"blocks": _prose(heading, words)}


def _doc(words_by_heading):
    blocks = []
    for h, w in words_by_heading.items():
        blocks.extend(_prose(h, w))
    return {"title": "Monolith to Microservices", "template_id": "generic", "blocks": blocks}


def _req(instruction=NO_SHAPE, *, effort="fast", kind="document", operation="create", **material):
    return C.ComposeRequest(kind=kind, formats=["pdf", "docx"], template_id="generic", effort=effort,
                            operation=operation, instruction=instruction,
                            material=C.Material(instruction=instruction, **material))


def _system(model, schema):
    return model.named(schema)[0]["messages"][0]["content"]


def _headings(spec):
    return [b.text for b in spec.body.blocks
            if getattr(b, "type", "") == "heading" and getattr(b, "level", 0) == 1]


# ------------------------------------------- the pure half: size_from_plan --


def test_the_size_is_the_sum_of_the_models_own_per_section_numbers():
    decided, warnings = C.size_from_plan(_plan(PLAN))
    assert warnings == []
    assert decided.words == PLAN_TOTAL == 12_050, "code adds the model's numbers up; it invents none of them"
    assert decided.sections == 15
    assert decided.source == L.SOURCE_PLANNED
    assert decided.explicit is False, "the model is not the person"
    assert decided.phrase == "the 15 sections the model planned"
    # And the per-section target the writer uses is the plan's own number for
    # that section, not the total divided by the count.
    assert decided.section_count == 15
    assert L.section_words(decided.words, 15) == 803, "an even split says 803 for all fifteen"


def test_a_plan_with_no_sections_leaves_the_size_undecided_and_says_so():
    before = L.LengthTarget()
    decided, warnings = C.size_from_plan({"sections": []}, before)
    assert decided is before, "nothing was decided, so nothing changed"
    assert warnings == ["the model planned no sections, so the document was written at this effort level's "
                        "own size"]
    # A plan that is not a plan at all reads the same way.
    assert C.size_from_plan(None)[1] == warnings
    assert C.size_from_plan({"sections": [{"heading": "  ", "words": 400}]})[1] == warnings


def test_zero_words_is_clamped_and_the_clamp_is_on_the_card():
    """The absurd answer the owner's requirement names first. A silent clamp
    is the original defect, so the number code substituted is said out
    loud."""
    decided, warnings = C.size_from_plan(_plan({"One": 0, "Two": 0, "Three": 0}))
    assert decided.words == 3 * L.WORDS_PER_SECTION == 1_200
    assert warnings == ["the model planned 3 sections and no words for any of them, which is not a document; "
                        "it was sized at 1,200 words"]


def test_a_hundred_sections_is_clamped_to_what_one_file_holds_and_says_so():
    """The other absurd answer. 40 is `OUTLINE_MAX_SECTIONS`, which is the
    widest the outline schema itself can be — a bound on the file, not a
    size."""
    decided, warnings = C.size_from_plan(_plan({f"Section {i}": 300 for i in range(100)}))
    assert decided.sections == C.OUTLINE_MAX_SECTIONS == 40
    assert decided.words == 40 * 300 == 12_000, "only the sections that survive are counted"
    assert warnings == ["the model planned 100 sections, more than the 40 a single file can hold; "
                        "it was cut to 40"]


def test_a_plan_over_the_renderers_page_limit_is_clamped_and_says_so():
    decided, warnings = C.size_from_plan(_plan({f"Section {i}": 5_000 for i in range(8)}))
    assert decided.words == L.MAX_WORDS == 27_000
    assert warnings == [f"the model planned 40,000 words, more than the 27,000 ({T.MAX_PAGES} pages) "
                        "one file can hold; it was cut to 27,000"]


def test_a_section_the_model_did_not_size_is_sized_by_code_and_named():
    plan = _plan({"One": 600, "Two": 600})
    plan["sections"].append({"heading": "Three", "purpose": "p", "elements": ["paragraphs"]})
    plan["sections"].append({"heading": "Four", "purpose": "p", "elements": ["paragraphs"], "words": "nonsense"})
    decided, warnings = C.size_from_plan(plan)
    assert decided.words == 1_200 + 2 * L.WORDS_PER_SECTION == 2_000
    assert warnings == ["the model did not say how long 2 of its 4 sections should be; "
                        "each was sized at 400 words"]
    # "0" is an answer and is read as one; a missing key is not.
    assert C._plan_words({"words": 0}) == 0 and C._plan_words({}) is None
    assert C._plan_words({"words": "1,200"}) == 1_200 and C._plan_words({"words": -5}) is None


def test_the_bound_the_request_justifies_is_read_off_the_request_alone():
    """`size_bounds` is the guard between a size the MODEL decided and a size
    an UPLOAD decided, and it is pure: the effort, the request's own target and
    the sections the request named, and nothing from the material.

    `PLANNED_SECTION_CEILING` is `length.MAX_WORDS // OUTLINE_MAX_SECTIONS`, so
    a request that justifies all forty sections is bounded at exactly the
    file's own limit and nothing tighter is invented."""
    fast, think, mx = (T.EFFORT_BUDGETS[e] for e in ("fast", "think", "max"))
    assert C.PLANNED_SECTION_CEILING == L.MAX_WORDS // C.OUTLINE_MAX_SECTIONS == 675
    assert C.size_bounds(fast) == (8, 5_400)
    assert C.size_bounds(think) == (12, 8_100)
    assert C.size_bounds(mx) == (16, 10_800)
    # The sections the request NAMED raise it; `caps_for` allows two over.
    assert C.size_bounds(fast, L.LengthTarget(), [f"S{i}" for i in range(15)]) == (17, 11_475)
    # A size the request already justified is never cut below itself.
    assert C.size_bounds(fast, L.LengthTarget(words=9_000, explicit=True))[1] >= 9_000
    # And the ceiling stops at the file's own.
    assert C.size_bounds(fast, L.LengthTarget(), [f"S{i}" for i in range(60)]) == (40, L.MAX_WORDS)


def test_a_plan_over_the_bound_is_cut_to_it_and_the_cut_is_named():
    """The pure half of the upload defence. Both cuts are separate sentences,
    because a 40-section plan cut to 8 and a 216,000-word plan cut to 5,400 are
    two different things to have happened."""
    plan = _plan({f"Section {i}": 27_000 for i in range(40)})
    decided, warnings = C.size_from_plan(plan, L.LengthTarget(), max_sections=8, max_words=5_400)
    assert decided.words == 5_400 and decided.sections == 8
    assert warnings == [
        "the model planned 40 sections, more than the 8 this request allows; it was cut to 8",
        "the model planned 216,000 words, more than the 5,400 this request allows; it was cut to 5,400"]
    # Passing no bound means the file's own limits, which is what every caller
    # that is not the deciding pass gets.
    assert C.size_from_plan(plan)[0].words == L.MAX_WORDS


def test_the_section_count_something_decided_replaces_the_arithmetic_one():
    """`caps_for` read `max(sections_for(words), target.sections)` for one day,
    so a plan CUT to eight sections was handed back the fourteen that
    `sections_for` reads out of its own 5,400-word ceiling — and the bound
    bought nothing, because the cost is one scoped write per section."""
    fast = T.EFFORT_BUDGETS["fast"]
    assert C.caps_for(fast, L.LengthTarget(words=5_400, sections=8))[0] == 8
    assert L.sections_for(5_400) == 14, "which is what it used to widen back to"
    # Nothing decided a count: the word target still implies one.
    assert C.caps_for(fast, L.LengthTarget(words=5_400))[0] == 14
    # And the effort's own floor still holds under both.
    assert C.caps_for(fast, L.LengthTarget(words=400, sections=1))[0] == 8


# ------------------------------------------------ who gets asked, and when --


def test_every_document_but_one_the_person_sized_is_planned():
    assert C.plans_size(_req(NO_SHAPE)) is True
    # A size the person typed is the person's decision, not ours to re-ask.
    assert C.plans_size(_req("write a 3,000 word report on the migration")) is False
    assert C.plans_size(_req("write a big report on the migration")) is False
    # "Short" is a decision too, and the same one.
    assert C.plans_size(_req("write a short brief on the migration")) is False
    # A size the PRODUCT worked out is a constant times a count, so it is a
    # FLOOR and the model still decides: fifteen numbered sections times
    # WORDS_PER_SECTION is 6,000, and the work may well need more.
    fifteen = _req("Write the platform report.\nRequirements:\n"
                   + "\n".join(f"{i}. {n}" for i, n in enumerate(FIFTEEN, 1)))
    assert C.target_for(fifteen).words == 6_000 and C.plans_size(fifteen) is True
    # A deck's length is its slide count; a sheet is as long as its rows.
    assert C.plans_size(_req(NO_SHAPE, kind="presentation")) is False
    assert C.plans_size(_req(NO_SHAPE, kind="workbook")) is False
    # An edit's words name a change, not a length.
    assert C.plans_size(_req("also add a chart", operation="edit")) is False


def test_a_size_the_product_worked_out_is_a_floor_the_plan_may_beat():
    floor = L.LengthTarget(words=6_000, phrase="the 15 sections the request named",
                           source=L.SOURCE_DERIVED)
    # The plan says the work needs more: the plan wins outright.
    over, warnings = C.size_from_plan(_plan(PLAN), floor)
    assert over.words == PLAN_TOTAL == 12_050 and over.source == L.SOURCE_PLANNED and warnings == []
    # The plan says less: the floor holds, and the card says which was kept.
    under, warnings = C.size_from_plan(_plan({h: 100 for h in FIFTEEN}), floor)
    assert under.words == 6_000 and under.source == L.SOURCE_DERIVED
    assert under.sections == 15, "the plan still decides the SHAPE"
    assert warnings == ["the model planned 1,500 words, under the 6,000 that the 15 sections the request "
                        "named asks for; the document was written to 6,000"]
    # The data-report floor reads the same way: 1,500 words over real rows.
    data = _req("write a report on the customers file",
                tables=[C.DataTable("t1", "customers", ["Country", "Spend"],
                                    [["India", 10], ["Germany", 20]] * 60)])
    assert C.target_for(data).words == L.DATA_REPORT_FLOOR == 1_500
    assert C.plans_size(data) is True


def test_the_deciding_call_is_not_told_to_be_concise_and_is_bounded_by_the_request():
    """What the pass that decides the size is allowed to see. Fast's tone line
    is a number that would decide the answer before the model did, so it is
    gone. The section CAP is not: it is `size_bounds`, read off the request
    alone, and stating it is what keeps the prompt and the rule the composer
    enforces the same number.

    For one day this read the renderer's 40 at every effort and for every
    request, which is how an upload came to ask for forty sections of 27,000
    words (see `test_text_inside_an_upload_cannot_decide_the_size`)."""
    model = _Model([_plan(PLAN)] + [_section(h, int(w * 0.7)) for h, w in PLAN.items()])
    llm.json_completion = model
    asyncio.run(C.compose(_req(NO_SHAPE)))
    system = _system(model, "artifact_outline")
    assert "Be concise and concrete." not in system
    assert "the length is yours to decide from the subject and the material" in system
    assert "Decide the size here." in system
    # A bare one-line request justifies Fast's own eight sections and no more,
    # and the ceiling those eight come to is stated rather than sprung.
    assert "at most 8 top-level sections" in system
    assert "keep that total at or under 5,400 words" in system
    assert model.named("artifact_outline")[0]["plan_cap"] == 8
    # No size is SUGGESTED anywhere in it: a suggested number is what the
    # model would anchor on. A ceiling is not a suggestion.
    assert "words" in system and "about 400 words" not in system and "Write about" not in system

    # THE SAME REQUEST WITH ITS SECTIONS NUMBERED raises the cap, which is the
    # case the renderer's 40 was reaching for — without letting an upload have
    # the same effect.
    fifteen = _req("Write the platform report.\nRequirements:\n"
                   + "\n".join(f"{i}. {n}" for i, n in enumerate(FIFTEEN, 1)))
    assert C.size_bounds(T.EFFORT_BUDGETS["fast"], C.target_for(fifteen),
                         C.requested_sections(fifteen.instruction)) == (17, 11_475)
    assert C.size_bounds(T.EFFORT_BUDGETS["fast"], C.target_for(_req(NO_SHAPE))) == (8, 5_400)
    assert C.size_bounds(T.EFFORT_BUDGETS["think"], C.target_for(_req(NO_SHAPE))) == (12, 8_100)
    assert C.size_bounds(T.EFFORT_BUDGETS["max"], C.target_for(_req(NO_SHAPE))) == (16, 10_800)


def test_the_decided_size_reaches_the_prompt_the_document_and_the_card():
    """The owner's own prompt, end to end. Before: 735 words, 6 sections,
    "Be concise and concrete.", "at most 8 top-level sections"."""
    model = _Model([_plan(FITS)] + [_section(h, int(w * 0.7)) for h, w in FITS.items()])
    llm.json_completion = model
    result = asyncio.run(C.compose(_req(NO_SHAPE)))

    system = _system(model, "artifact_section_write")
    assert "Be concise and concrete." not in system
    assert ("Write about 5,400 words in 8 top-level sections, about 675 words of real prose in each"
            in system)
    assert len(_headings(result.spec)) == 8
    assert len(S.text_of(result.spec).split()) == 3_795
    note = next(w for w in result.warnings if w.startswith(C.LONG_DOCUMENT_NOTE))
    assert "the 8 sections the model planned" in note and "5,400 words" in note
    assert "written in 8 sections" in note

    # EACH SECTION IS ASKED FOR ITS OWN PLANNED LENGTH, not the average — the
    # numbers still add up to the decided total, so the plan's shape is the
    # shape that gets written.
    asked = [c["messages"][-1]["content"] for c in model.named("artifact_section_write")]
    assert "Write about 350 words in this section." in asked[0], "the executive summary"
    assert "Write about 1,100 words in this section." in asked[2], "the target architecture"


def test_a_plan_bigger_than_the_request_justifies_is_cut_out_loud_and_in_the_writes():
    """PLAN is 12,050 words over fifteen sections for a request of one
    sentence. `size_bounds` says a bare ask at Fast buys eight sections and
    5,400 words, so the plan is cut to that — and the CUT reaches the writes,
    which is the half that was missing for a day: the card said 5,400 while
    every scoped write was asked for the plan's own number."""
    # Each scripted section writes the 675 it is asked for, so no extension
    # pass runs and the call count is one per section and nothing else.
    model = _Model([_plan(PLAN)] + [_section(h, 675) for h in PLAN])
    llm.json_completion = model
    result = asyncio.run(C.compose(_req(NO_SHAPE)))
    asked = [c["messages"][-1]["content"] for c in model.named("artifact_section_write")]
    assert len(asked) == 8, "one scoped write per section the request bought, and no more"
    assert all("Write about 675 words in this section." in a for a in asked), asked
    # 6,550 is the first EIGHT sections of PLAN: the section cut comes first and
    # only the sections that survive are counted, as
    # `test_a_hundred_sections_is_clamped_to_what_one_file_holds_and_says_so`
    # pins. Both cuts are on the card.
    assert ["the model planned 15 sections, more than the 8 this request allows; it was cut to 8",
            "the model planned 6,550 words, more than the 5,400 this request allows; "
            "it was cut to 5,400"] == [w for w in result.warnings if w.startswith("the model planned")]


def test_the_size_is_the_same_at_every_effort_level():
    """The size is a property of the work, not of a dropdown. Before this
    change the SAME request produced 735 words at Fast and 4,232 at Think.

    The effort sets one thing about the size and only one: the CEILING, which
    is the cost the person chose (`size_bounds`). Inside it the answer is the
    model's and it is the same at all three."""
    written = {}
    for effort in ("fast", "think", "max"):
        answers = [_plan(FITS)] + [_section(h, int(w * 0.7)) for h, w in FITS.items()]
        answers.append({"ok": True, "issues": []})          # Think and Max review
        model = _Model(answers)
        llm.json_completion = model
        result = asyncio.run(C.compose(_req(NO_SHAPE, effort=effort)))
        written[effort] = (len(_headings(result.spec)), len(S.text_of(result.spec).split()))
    assert written == {"fast": (8, 3_795), "think": (8, 3_795), "max": (8, 3_795)}, written

    # And the ceiling is the one number the effort still moves. A plan of
    # twenty sections is over all three, so each is cut to the sections its own
    # effort bought — one scoped write each, which is the cost the person chose.
    twenty = {f"Part {i}": 600 for i in range(1, 21)}            # 12,000 words
    ceilings = {}
    for effort in ("fast", "think", "max"):
        answers = [_plan(twenty)] + [_section(h, 675) for h in twenty]
        answers.append({"ok": True, "issues": []})
        model = _Model(answers)
        llm.json_completion = model
        asyncio.run(C.compose(_req(NO_SHAPE, effort=effort)))
        ceilings[effort] = len(model.named("artifact_section_write"))
    assert ceilings == {"fast": 8, "think": 12, "max": 16}, ceilings


def test_the_plan_is_not_paid_for_twice():
    """At Think and Max an outline call was already budgeted. The deciding
    pass IS that call: it planned these sections against this material, and
    planning them again would be the same call twice."""
    for effort in ("think", "max"):
        answers = [_plan(PLAN)] + [_section(h, int(w * 0.7)) for h, w in PLAN.items()]
        answers.append({"ok": True, "issues": []})
        model = _Model(answers)
        llm.json_completion = model
        asyncio.run(C.compose(_req(NO_SHAPE, effort=effort)))
        assert len(model.named("artifact_outline")) == 1, f"{effort}: {[c['schema'] for c in model.calls]}"

    # A document the model sizes under SECTIONED_WRITER_WORDS takes the
    # single-call path, and reuses the plan there too.
    model = _Model([_plan({"Note": 200, "What changes": 200}), _doc({"Note": 140, "What changes": 140}),
                    {"ok": True, "issues": []}])
    llm.json_completion = model
    asyncio.run(C.compose(_req("write a note about the price change", effort="think")))
    assert [c["schema"] for c in model.calls] == ["artifact_outline", "artifact_document", "artifact_review"]
    assert ("Write about 400 words in 2 top-level sections, about 200 words of real prose in each"
            in _system(model, "artifact_document"))


def test_a_plan_that_cannot_be_produced_is_not_a_failed_job():
    """The size stays where it was, the document is still written, and the
    card says the size was not decided."""
    async def broken(messages, *, json_schema=None, schema_name="", **kw):
        if schema_name == "artifact_outline":
            return "not json at all"
        return json.dumps(_doc({"Note": 300}))

    llm.json_completion = broken
    result = asyncio.run(C.compose(_req(NO_SHAPE)))
    assert _headings(result.spec) == ["Note"]
    assert ("the size of this document could not be planned, so it was written at this effort level's own size"
            in result.warnings)


# ---------------------------------------- the asymmetry, in all three places --


def test_a_size_the_product_decided_buys_the_sectioned_writer(monkeypatch):
    """`sectioned` read `(target.explicit or budget.outline_pass)`. The
    model's 12,050 words are not explicit, and Fast has no outline pass, so
    before today this request would have been one whole-document call."""
    model = _Model([_plan(FITS)] + [_section(h, int(w * 0.7)) for h, w in FITS.items()])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req(NO_SHAPE)))
    assert [c["schema"] for c in model.calls] == ["artifact_outline"] + ["artifact_section_write"] * 8
    assert any(w.startswith(C.LONG_DOCUMENT_NOTE) for w in result.warnings)


def test_a_short_draft_is_repaired_whoever_decided_the_size(monkeypatch):
    """The SHORT_DRAFT_FRACTION gate read `target.explicit`, so a document
    that came in at a third of a size the PRODUCT decided got no repair pass
    while one a third short of a typed size did."""
    small = {"One": 700, "Two": 700, "Three": 700}      # 2,100: under SECTIONED_WRITER_WORDS
    model = _Model([_plan(small), _doc({"One": 100, "Two": 100, "Three": 100}),
                    _doc({"One": 600, "Two": 600, "Three": 600})])
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req(NO_SHAPE)))
    assert [c["schema"] for c in model.calls] == ["artifact_outline"] + ["artifact_document"] * 2, \
        [c["schema"] for c in model.calls]
    grow = model.calls[-1]["messages"][-1]["content"]
    # And it does not tell the person they asked for 2,100 words.
    assert "the plan for it came to about 2,100" in grow
    assert "asked for" not in grow.split("Write the WHOLE document")[0]
    assert len(S.text_of(result.spec).split()) == 1_806


def test_a_correction_cannot_gut_a_document_the_sectioned_writer_built(monkeypatch):
    """WHICH DRAFTS THE 0.9 FLOOR PROTECTS. `CORRECTION_KEEP_FRACTION` refuses
    a correction that keeps less than 90% of the draft it replaces, and it
    reads `(target.explicit or sectioned)` — a size the person NAMED, or a
    draft the sectioned writer built one call at a time.

    For one day it read `target.words` alone. Every created document has a
    planned size now, so that put the 0.9 floor on every correction in the
    product, and a placeholder fix that came back 15% shorter was refused with
    the placeholder still in the file (see
    `test_a_correction_that_removes_a_placeholder_is_still_applied`). A draft
    that cost one call is not the draft that floor was written for: the guard
    there is `_worse`, which refuses anything under half.

    The candidate below keeps every section and a bit over half the words, so
    `_worse` accepts it and only the keep-fraction floor can refuse it."""
    # A PLANNED size over SECTIONED_WRITER_WORDS: nine calls behind the draft.
    planned = {f"Part {i}": 500 for i in range(1, 7)}            # 3,000 words, 6 sections
    answers = [_plan(planned)] + [_section(h, 500) for h in planned]
    answers += [{"ok": False, "issues": [{"where": "all", "problem": "thin", "fix": "more",
                                          "severity": "must"}]}]
    answers += [_doc({h: 275 for h in planned})]                 # 1,650 of 3,000: 55%
    model = _Model(answers)
    monkeypatch.setattr(llm, "json_completion", model)
    result = asyncio.run(C.compose(_req(NO_SHAPE, effort="think")))
    assert _headings(result.spec) == list(planned), "the draft survives the correction"
    cut = next(w for w in result.warnings if "would have cut the document" in w)
    assert "3,000 words this document was planned at" in cut, cut
    assert "words that were asked for" not in cut, "he never asked for 3,000"


def test_a_single_call_drafts_correction_is_held_to_worse_and_not_to_the_floor(monkeypatch):
    """The other side of the same rule. A planned size UNDER
    SECTIONED_WRITER_WORDS is one whole-document call, so a correction that
    keeps 55% of it is applied — and one that keeps under half is still
    refused, by `_worse`, which is the guard that was always there."""
    small = {"One": 700, "Two": 700, "Three": 700}               # 2,100 words
    kept = _Model([_plan(small), _doc({"One": 600, "Two": 600, "Three": 600}),
                   {"ok": False, "issues": [{"where": "all", "problem": "thin", "fix": "more",
                                             "severity": "must"}]},
                   _doc({"One": 330, "Two": 330, "Three": 330})])
    monkeypatch.setattr(llm, "json_completion", kept)
    result = asyncio.run(C.compose(_req(NO_SHAPE, effort="think")))
    assert len(S.text_of(result.spec).split()) == 996, "the correction was applied"
    assert not [w for w in result.warnings if "would have cut the document" in w], result.warnings

    gutted = _Model([_plan(small), _doc({"One": 600, "Two": 600, "Three": 600}),
                     {"ok": False, "issues": [{"where": "all", "problem": "thin", "fix": "more",
                                               "severity": "must"}]},
                     _doc({"One": 100})])
    monkeypatch.setattr(llm, "json_completion", gutted)
    result = asyncio.run(C.compose(_req(NO_SHAPE, effort="think")))
    assert _headings(result.spec) == ["One", "Two", "Three"], "the draft survives"


# -------------------------------------------------------------- the tone line --


def test_the_tone_line_follows_the_decided_size_not_the_effort_level():
    # No size decided yet, and not being decided either: the effort's own
    # adjective, which is the only place it still lives.
    plain = _req(NO_SHAPE)
    assert C._size_line(L.LengthTarget()) == ""
    assert "Be concise and concrete." in C._material_messages(
        plain, budget=T.EFFORT_BUDGETS["fast"], target=L.LengthTarget())[0]["content"]

    # A size, from anywhere: the sentence is the number and the shape.
    for target in (L.LengthTarget(words=1_200, explicit=True, phrase="1200 words", source=L.SOURCE_ASKED),
                   L.LengthTarget(words=1_200, sections=4, source=L.SOURCE_PLANNED),
                   L.LengthTarget(words=1_200, source=L.SOURCE_DERIVED)):
        line = C._size_line(target)
        assert line.startswith("Write about 1,200 words in ") and "words of real prose in each" in line

    # The plan's own section count, not the arithmetic one: 1,200 words is
    # sections_for() == 3, and a plan of four sections is four.
    assert "in 4 top-level sections, about 300 words" in C._size_line(
        L.LengthTarget(words=1_200, sections=4, source=L.SOURCE_PLANNED))
    assert "in 3 top-level sections, about 400 words" in C._size_line(L.LengthTarget(words=1_200))

    # "Short" is a decided size, said in the person's own words instead of
    # an effort adjective.
    short = C.target_for(_req("write a short brief on the migration"))
    assert short.words == 0 and short.explicit is True
    assert C._size_line(short) == ('The request asks for a short file (“short”). Write it tight: '
                                   'no padding, no repetition, and nothing the request did not ask for.')

    # A deck and a workbook have no word target and keep the effort's tone.
    deck = C.target_for(_req(NO_SHAPE, kind="presentation"))
    assert C._size_line(deck) == ""

    # A DOCUMENT request that names a slide count also comes back words=0,
    # explicit=True — with "10 slides" as the phrase. That is not a request
    # for a short report.
    slides = C.target_for(_req("write a report on the migration and a 10 slide deck"))
    assert slides.words == 0 and slides.explicit is True and slides.phrase == "10 slide"
    assert C._size_line(slides) == "", "a slide count is not a shrink word"


def test_the_section_cap_never_trims_what_the_plan_decided():
    """`caps_for` promises what `_enforce_caps` allows, and a plan of more
    sections than the effort's floor raises both."""
    fast = T.EFFORT_BUDGETS["fast"]                                   # max_sections 8
    assert C.caps_for(fast, L.LengthTarget(words=900, sections=12))[0] >= 12
    assert C.caps_for(fast, L.LengthTarget(words=12_050, sections=15))[0] >= 15
    spec = S.parse_body("document", _doc({h: 20 for h in FIFTEEN}))
    assert C._enforce_caps(spec, fast, (), target=L.LengthTarget(words=12_050, sections=15)) == []
    assert len([b for b in spec.body.blocks if b.type == "heading" and b.level == 1]) == 15


# ------------------------------------------- the stage the person can read --


def test_the_outline_stage_says_running_whenever_the_composer_plans(monkeypatch):
    """The engine decided this stage from `budget.outline_pass`, so a Fast
    document whose size is planned would have shown "outline: skipped — Fast
    goes straight to writing" over a real plan call, and the stage would
    have stayed skipped for the whole job because the `detail == "writing"`
    hand-off was gated on the same flag."""
    from app.engines import artifact as E

    async def fake_compose(req, *, progress=None):
        if progress is not None:
            await progress(30.0, "writing")
        return C.ComposeResult(spec=S.parse_body("document", _doc({"One": 20})))

    monkeypatch.setattr(C, "compose", fake_compose)

    def run(instruction, effort, operation="create", kind="document"):
        stages = []

        class _Ctx:
            material = {"instruction": instruction}
            job = {}
            parent_spec = None
            budget = T.EFFORT_BUDGETS[effort]

            async def progress_stage(self, name, state, detail=""):
                stages.append((name, state, detail))

            async def progress(self, pct, detail):
                pass

            def warn(self, message):
                pass

        ctx = _Ctx()
        ctx.instruction, ctx.effort, ctx.operation, ctx.kind = instruction, effort, operation, kind
        ctx.formats, ctx.template_id = ["pdf"], "generic"
        asyncio.run(E._compose_for_pipeline_inner(ctx))
        return [s for s in stages if s[0] == "outline"]

    # A Fast document nobody sized: planned, so the stage runs and finishes.
    assert run(NO_SHAPE, "fast") == [("outline", "running", ""), ("outline", "done", "")]
    # A Fast document the PERSON sized: no plan call, and the old sentence.
    assert run("write a 900 word brief on the migration", "fast") == [
        ("outline", "skipped", "Fast goes straight to writing")]
    # A deck at Fast is not planned either, and reads the same.
    assert run(NO_SHAPE, "fast", kind="presentation") == [
        ("outline", "skipped", "Fast goes straight to writing")]
    # Think still outlines whatever the size is.
    assert run("write a 900 word brief on the migration", "think") == [
        ("outline", "running", ""), ("outline", "done", "")]
    # An edit keeps its own sentence.
    assert run("also add a chart", "fast", operation="edit") == [
        ("outline", "skipped", "an edit keeps the structure")]


def test_a_failed_plan_is_not_outlined_again(monkeypatch):
    """The plan IS the outline pass. When it fails at Think, the composer
    writes from no outline rather than making a second attempt at the call
    that just failed."""
    calls = []

    async def broken(messages, *, json_schema=None, schema_name="", **kw):
        calls.append(schema_name)
        if schema_name == "artifact_outline":
            return "not json at all"
        if schema_name == "artifact_review":
            return json.dumps({"ok": True, "issues": []})
        return json.dumps(_doc({"Note": 300}))

    monkeypatch.setattr(llm, "json_completion", broken)
    result = asyncio.run(C.compose(_req(NO_SHAPE, effort="think")))
    assert calls.count("artifact_outline") == 1, calls
    assert ("the size of this document could not be planned, so it was written at this effort level's own size"
            in result.warnings), result.warnings
