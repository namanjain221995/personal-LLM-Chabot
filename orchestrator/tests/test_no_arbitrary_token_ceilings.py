"""NOTHING CONSTANT DECIDES THE SIZE (owner, 2026-09-27).

    "Our ai decide it own What need ?? there is No token limit for docs and
     for sheet ?? or for any think ??"

The target state is not a bigger constant. It is that the size comes from the
REQUEST when the request names one, from the MODEL when it does not, and from
PHYSICS only where physics is the real bound — and that where a bound cannot be
removed it SAYS so instead of quietly shortening the work.

Three kinds of limit, kept apart on purpose:

  PHYSICS   the served window, the KV budget, what a call can decode inside the
            stage's wall clock. Budgeted, never removed.
  SAFETY    a wall-clock bound, a loop guard, an upload size. These protect the
            person; they are asserted here so a later change cannot quietly
            drop one while "removing a cap".
  ARBITRARY a number somebody picked that shortens an answer or a document.
            These are what this file is about.

Every measured figure quoted below was taken on 2026-09-27 against the running
container (`sf-local-ai-orchestrator-1`) and the live engine's own /tokenize.
"""
from __future__ import annotations

import pytest

from app.artifacts import compose as C
from app.artifacts import length as L
from app.artifacts import spec as S
from app.artifacts import types as T
from app.config import settings
from app.core import answer_sampling
from app.core import best_of


# --------------------------------------------- a request that names its shape


def test_a_named_slide_count_reaches_the_budget():
    """`_max_tokens_for` grew on `target.words` alone, and `parse_size` gives a
    presentation `words=0` by construction — so a slide count could not move a
    deck's budget at all. Measured before: 30 slides asked, 8,000 tokens given,
    in ONE call, with no per-slide write path anywhere in the module."""
    target = L.parse_size("Make a 30 slide deck on our platform", "presentation")
    assert target.slides == 30 and target.explicit is True
    budget = T.EFFORT_BUDGETS["fast"]
    tokens = C._max_tokens_for("presentation", "fast", target, budget=budget)
    assert tokens == 30 * C.TOKENS_PER_SLIDE == 18_000
    # And a deck nobody sized still gets exactly the floor it always had.
    plain = L.parse_size("make me a deck about onboarding", "presentation")
    assert C._max_tokens_for("presentation", "fast", plain, budget=budget) == 8_000


def test_a_named_workbook_shape_reaches_the_budget():
    """`parse_size` returned words=0, slides=0, explicit=False for EVERY
    workbook whatever the request said, so no wording could move a workbook off
    its base constant."""
    target = L.parse_size("Build a comprehensive workbook, 5000 rows, 8 sheets", "workbook")
    assert (target.rows, target.sheets, target.explicit) == (5_000, 8, True)
    want = C._wanted_tokens("workbook", "fast", target, budget=T.EFFORT_BUDGETS["fast"])
    assert want == 5_000 * 8 * C.TOKENS_PER_TYPED_ROW


def test_the_row_cap_and_the_token_budget_no_longer_contradict_each_other():
    """Fast advertised `max_rows_per_sheet = 2000` while its 12,000-token
    budget carried about 199 model-typed rows — measured, a 10x disagreement,
    and the sheet simply came back short. A workbook that named no shape is now
    budgeted for the rows its effort actually allows."""
    budget = T.EFFORT_BUDGETS["fast"]
    plain = L.parse_size("build me a workbook of our accounts", "workbook")
    want = C._wanted_tokens("workbook", "fast", plain, budget=budget)
    assert want == budget.max_rows_per_sheet * C.TOKENS_PER_TYPED_ROW
    assert want > 12_000, "the old base constant was the ceiling"


def test_a_shape_that_cannot_be_decoded_in_time_is_said_not_swallowed():
    """Where a bound genuinely cannot be removed, the defect is the silence.
    40,000 typed rows in ONE call is more decode time than the compose stage
    has, so the budget is clamped — and the clamp is reported."""
    target = L.parse_size("Build a workbook, 5000 rows, 8 sheets", "workbook")
    budget = T.EFFORT_BUDGETS["fast"]
    want, ceiling = C.one_call_shortfall("workbook", "fast", target, budget=budget)
    assert want > ceiling > 0, "the ask exceeds one call's decode budget"
    # What is actually SENT is always safe, whatever was asked for.
    assert C._max_tokens_for("workbook", "fast", target, budget=budget) == ceiling
    # And nothing is said when nothing was asked for.
    assert C.one_call_shortfall("workbook", "fast",
                               L.parse_size("a workbook", "workbook"), budget=budget) == (0, 0)


# ------------------------------------------------- the model decides the size


def test_the_outline_is_asked_how_long_each_section_needs_to_be():
    """The owner's "our AI decides what it needs", in the schema. Until today
    the ONLY translator between a request's shape and a word budget was
    `length.WORDS_PER_SECTION = 400`, read forwards to size a section and
    backwards to count them."""
    section = C._OUTLINE_SCHEMA["properties"]["sections"]["items"]
    assert "words" in section["properties"], "the model must be able to state the size"
    assert "words" in section["required"], "and must not be able to omit it"


def test_a_sections_budget_follows_the_plan_not_the_constant():
    """`_planned_words` reads the outline's own figure, and the even split by
    WORDS_PER_SECTION is only the fallback for a model that omitted it."""
    assert C._planned_words({"words": 1_800}, 400) == 1_800
    assert C._planned_words({}, 400) == 400, "the even split is the floor, not the rule"
    # The schema's own bounds hold, so nonsense cannot size a call.
    assert C._planned_words({"words": 99_999}, 400) == 6_000
    assert C._planned_words({"words": -5}, 400) == 400


def test_a_long_planned_section_is_no_longer_clamped_at_sixteen_thousand():
    """SECTION_MAX_TOKENS = 16_000 bound above 2,666 planned words — the
    ceiling that would bite first once the model may plan longer sections."""
    assert not hasattr(C, "SECTION_MAX_TOKENS"), "the flat clamp is gone"
    # A 2,700-word section — just past where the old 16,000 began to bind —
    # now gets what it needs.
    assert min(C.one_call_token_ceiling(), 2_700 * C.SECTION_TOKENS_PER_WORD) > 16_000
    # The only clamp left is physics: what one call can decode inside the
    # compose stage's wall clock, which moves with ARTIFACT_STAGE_TIMEOUT_S
    # rather than being a number somebody typed.
    assert C.one_call_token_ceiling() == max(
        12_000, int(C.DECODE_TOKENS_PER_S * C._stage_budget_s() * C.ONE_CALL_DEADLINE_FRACTION))


# ------------------------------------- four gates between a shape and a number


@pytest.mark.parametrize("instruction, expected, why", [
    ("Write a technical report on our platform. 1. Executive Summary 2. Architecture "
     "3. Hardware Layer 4. Security 5. Conclusion", 5,
     "a bare numbered list with NO colon heading above it was invisible"),
    ("Sections:\n1. Q3 2026 Revenue\n2. Data Model\n3. Top 10 Accounts", 3,
     "any digit in a name discarded it, and the minimum=2 rule then discarded the rest"),
    ("Requirements:\n1. How We Will Grow The Business Next Year\n2. Risks\n3. Roadmap", 3,
     "a seven-word heading was over the 1-5 word rule"),
])
def test_a_named_shape_is_no_longer_read_as_silence(instruction, expected, why):
    assert len(C.requested_sections(instruction)) == expected, why


def test_a_one_line_list_does_not_swallow_the_sentence_after_it():
    """The owner's request is ONE line with no newline anywhere in it, so its
    last item ran on into the next sentence: item 15 arrived as 'Conclusion Use
    professional Markdown', which put a wrong heading in the prompt AND made
    the coverage check report "Conclusion" missing from a document that has
    one."""
    names = C.requested_sections(
        "Requirements: 1. Executive Summary 2. Architecture Overview 3. Hardware Layer "
        "4. Security 5. Conclusion Use professional Markdown. Do not skip any section.")
    assert names[-1] == "Conclusion", names
    # A genuinely long last heading is NOT cut.
    assert C.requested_sections(
        "Requirements:\n1. Intro\n2. The Complete Regulatory Review Of Our Vendors"
    ) == ["Intro", "Complete Regulatory Review Of Our Vendors"]


def test_more_than_twenty_named_sections_survive():
    """`found[:20]` threw away names the outline schema (which widens to 40)
    could carry, and cost the derived target 400 words each."""
    names = [f"Chapter {w}" for w in (
        "Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India",
        "Juliett", "Kilo", "Lima", "Mike", "November", "Oscar", "Papa", "Quebec", "Romeo",
        "Sierra", "Tango", "Uniform", "Victor", "Whiskey", "Xray", "Yankee")]
    instruction = "Requirements:\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1))
    assert len(C.requested_sections(instruction)) == 25
    assert C.MAX_REQUESTED_SECTIONS == 40


def test_the_limits_sentence_never_tells_the_model_to_contradict_the_person():
    """On the owner's recorded run the system prompt said "at most 8 top-level
    sections" against his fifteen. A cap enforced by instructing the model to
    disobey the person is the weakest layer there is."""
    req = C.ComposeRequest(kind="document", formats=["pdf"], template_id="generic", effort="fast",
                           instruction="x", material=C.Material(instruction="x"))
    named = C._material_messages(req, budget=T.EFFORT_BUDGETS["fast"], target=None,
                                 requested=["Alpha", "Beta", "Gamma"])[0]["content"]
    assert "Limits: at most" not in named, "the person's own sections are not a ceiling"
    assert "top-level sections" in named and "room for" in named
    assert "none of them may be dropped or merged" in named
    # With no sections named the sentence is exactly what it always was.
    plain = C._material_messages(req, budget=T.EFFORT_BUDGETS["fast"], target=None)[0]["content"]
    assert "Limits: at most" in plain


# ------------------------------------------- a structure, widened not ignored


def test_the_block_ceiling_is_derived_from_the_page_ceiling():
    """A flat 400 was 3.5x tighter than the 60 pages and 27,000 words the
    product advertises, so neither advertised ceiling could be reached — and it
    was enforced by DELETING whole sections from the end of the document.
    Measured binding on a real stored file at exactly 400 blocks / 8,044 words."""
    assert T.MAX_DOCUMENT_BLOCKS == 1_500
    field = S.DocumentSpec.model_fields["blocks"]
    assert any(getattr(m, "max_length", None) == T.MAX_DOCUMENT_BLOCKS for m in field.metadata)
    # The derivation: pages x words-a-page / words-a-block, with headroom.
    assert T.MAX_DOCUMENT_BLOCKS >= (T.MAX_PAGES * L.WORDS_PER_PAGE) // T.WORDS_PER_BLOCK


def test_every_page_the_renderer_will_make_can_be_previewed():
    """MAX_PREVIEW_PAGES = 40 against MAX_PAGES = 60 meant twenty pages of a
    document the product will happily make could not be previewed — which is
    how a long document LOOKS short to the person who asked for it."""
    assert T.MAX_PREVIEW_PAGES == T.MAX_PAGES


def test_the_prompt_and_the_document_table_agree_on_one_row_number():
    """`rows[:40]` nested inside a 200-row slice: two numbers for the same
    thing, and the tighter one always won."""
    assert C.PROMPT_TABLE_ROWS == T.MAX_TABLE_ROWS


# --------------------------------------------------- the chat path's own shape


def test_the_chat_path_derives_a_target_from_a_named_shape():
    """`requested_words` is a regex for an explicit "N words", so fifteen
    numbered sections returned None and the WHOLE length mechanism was off: no
    section plan on the first call, no word gauge on continuations, and the
    short-normal-stop extension could never fire."""
    shape = ("Write a report on our platform. 1. Executive Summary 2. Architecture Overview "
             "3. Hardware Layer 4. AI Inference Layer 5. Backend Architecture 6. Frontend "
             "Architecture 7. Database Architecture 8. RAG Pipeline 9. Authentication "
             "10. Security 11. Monitoring 12. Scaling 13. Recovery 14. Performance 15. Conclusion")
    assert answer_sampling.requested_words(shape) is None, "this is why it was needed"
    assert answer_sampling.requested_shape_words(shape) == 15 * L.WORDS_PER_SECTION == 6_000
    # An explicit count still wins, and a request for LESS is never grown.
    assert answer_sampling.requested_words("Write a 10,000-word report") == 10_000
    assert answer_sampling.requested_shape_words(
        "Give me a brief summary: 1. Intro 2. Risks 3. Next steps") is None


def test_a_derived_chat_target_can_only_lengthen_an_answer():
    """`_TARGET_HIGH = 1.40` cuts an answer at 140% of target. That is right
    for a number the person typed and wrong for one the product inferred, so a
    derived target is a FLOOR: it may extend a short answer and may never cut
    one."""
    from app import continuation
    typed = continuation._WordGauge(1_000)
    derived = continuation._WordGauge(1_000, floor_only=True)
    assert typed.high == 1_400
    assert derived.high == float("inf"), "a number nobody typed must not shorten an answer"


# ------------------------------------------------ the input side of the same ask


def test_one_document_budget_derived_from_the_window():
    """Three bare literals in two files decided how much of the person's own
    uploaded document the model may see: 48,000 on the UPLOAD turn, a bare
    `8000` inline in main.py on every later turn, and 6,000 for a shared page.
    Measured: an 84-page, 428,122-character file stored WHOLE in Postgres
    handed the model 1.87% of itself on a follow-up."""
    from app.engines import document as D
    assert settings.document_context_chars >= 48_000
    assert D.DOC_CONTEXT_CHARS == max(48_000, settings.document_context_chars)
    # Derived from the served window, not picked.
    assert settings.document_context_chars == max(
        48_000, int(settings.model_max_context * settings.document_context_window_fraction * 3.0))


def test_the_compaction_ceiling_is_derived_from_the_window():
    """Measured: a 60-message chat at 78,909 tokens was folded so the model saw
    13,560 tokens — 17.2% of what the person wrote — with 912,387 tokens of
    window sitting unused. Of 868 production conversations ZERO exceed the
    949,915-token verified needle depth."""
    assert settings.context_compact_max_tokens == max(
        40_000, int(settings.model_max_context * settings.context_compact_window_fraction))
    assert settings.context_compact_max_tokens > 40_000 or settings.model_max_context <= 80_000


def test_chat_and_the_api_read_one_ocr_page_budget():
    """40 in chat against 1,000 for the same file through the public API — same
    sidecar, same worker Spark, two numbers picked independently, and the chat
    user got the smaller one."""
    from app.engines import document as D
    assert D.OCR_PAGE_BUDGET == settings.public_api_files_ocr_page_budget


def test_a_text_upload_is_stored_whole():
    """The ONE irreversible cap on the input path. `[:400_000]` cut the text
    before it reached Postgres, so the remainder is gone unless the person
    uploads again — and one real stored document sits at exactly 400,000
    characters, which is that line's fingerprint. Every other cap here only
    shortens a prompt."""
    import inspect
    from app.engines import document as D
    src = inspect.getsource(D)
    assert 'errors="replace")[:400_000]' not in src
    assert "_INGEST_TEXT_SOFT_CHARS" in src, "kept as a log threshold, not a cut"


# --------------------------------------------------------------------- SAFETY
#
# Asserted so a later change cannot drop one of these while "removing a cap".
# The owner asked for no token limits, not for a product that can hang forever.


def test_the_wall_clock_bounds_stay_and_keep_their_invariant():
    """GEN_WALL_CLOCK_S is the only thing that kills a wedged generation, and
    this cluster wedges for real — /health stays green while the engine is
    dead. It never binds on length: at the 87.8 tok/s measured today, 4,200 s
    allows ~368,000 tokens in one call. LLM_REQUEST_TIMEOUT must never be
    shorter, or the HTTP client kills a generation the application still
    permits and the SDK silently re-runs it."""
    assert settings.gen_wall_clock_s > 0
    assert settings.llm_request_timeout >= settings.gen_wall_clock_s


def test_the_loop_guard_is_what_stops_a_runaway_not_a_length_cap():
    """The owner's own precedent: PR #71's 8k/64k Fast caps were a regression
    and PR #73 removed them. Runaway repetition is stopped by shape, not by
    length — which is why Max now streams through the guard as well."""
    import inspect
    from app.core import answer_guard
    from app.engines import chat
    assert hasattr(answer_guard, "AnswerGuard") and hasattr(answer_guard, "repetition_allowance")
    src = inspect.getsource(chat.run_chat_engine)
    guard_at = src.index("guard = answer_guard.AnswerGuard")
    best_of_at = src.index("best_of.generate_candidates")
    assert guard_at < best_of_at, "a Max answer that may continue must pass the loop guard"


def test_the_continuation_backstops_stay():
    """None of these binds — 1,000,000 tokens at the measured rate is about
    3.2 h against a 6 h deadline — but each bounds a loop that makes tiny
    forward progress forever."""
    from app import continuation
    assert settings.continuation_deadline_s > 0
    assert settings.continuation_max_segments > 1
    assert settings.continuation_min_segment_tokens > 0
    assert continuation._MIN_PROGRESS_CHARS > 0


def test_the_sectioned_writer_keeps_its_deadline_and_its_reserve():
    """A wedged engine holds /health green while generation is dead, so without
    a wall clock a job waits forever. The 0.75 fraction and the 60 s reserve
    are what let a nine-of-fourteen-section document be validated and PUBLISHED
    instead of thrown away — they protect work, they do not shorten it."""
    assert 0 < C.SECTION_DEADLINE_FRACTION < 1
    assert C.SECTION_RESERVE_S > 0
    assert C.SECTION_EXTEND_MAX > 0
    assert C.CORRECTION_KEEP_FRACTION > 0.5, "a correction may not halve a many-call draft"


def test_the_prompt_injection_boundary_stays():
    """These are not size caps. A cell or a pasted heading is DATA; making it
    an instruction is how attacker-shaped phrases would end up beside the
    composer's own anti-fabrication rules."""
    assert C.DATA_FENCE_OPEN and C.DATA_FENCE_CLOSE and C.DATA_FENCE_RULE
    assert C._SECTION_SCAN_CHARS > 0, "requested_sections runs on the single event loop"
    assert C._LIST_BLOCK_CHARS > 0


def test_the_section_scan_bound_limits_cpu_and_not_the_document():
    """The bound must stay — this repository has stalled the event loop twice
    on a multi-megabyte message — but hitting it must not silently mean "no
    size was asked for". A table of contents in the first 4,000 characters is
    read whatever follows it."""
    names = ["Executive Summary", "Architecture Overview", "Hardware Layer", "Security",
             "Monitoring", "Scaling Strategy", "Failure Recovery", "Conclusion"]
    toc = "Requirements:\n" + "\n".join(f"{i}. {n}" for i, n in enumerate(names, 1))
    huge = toc + "\n\n" + ("filler words that go on and on " * 5_000)
    assert len(huge) > C._SECTION_SCAN_CHARS * 5
    assert len(C.requested_sections(huge)) == 8


def test_the_upload_and_render_bounds_stay():
    """Upload and render-resource bounds, not answer lengths."""
    assert T.MAX_FILE_BYTES > 0 and T.MAX_ZIP_BYTES > 0
    assert T.MAX_ROWS_PER_SHEET > 0 and T.MAX_SHEETS > 0 and T.MAX_SLIDES > 0


def test_a_code_made_sheet_is_still_held_only_to_the_hard_ceiling():
    """The one place in the surface that already distinguished "the person's
    data" from "a token bill", and the pattern the rest now follows."""
    import inspect
    src = inspect.getsource(C._enforce_caps)
    assert "rows_are_code_made" in src
    assert "T.MAX_ROWS_PER_SHEET" in src


def test_the_judge_floor_still_holds_for_a_short_answer():
    """The window follows the candidates now, but nothing shrank."""
    assert best_of._JUDGE_ANSWER_CHARS == 4_000
    assert best_of._JUDGE_PROMPT_CHARS > best_of._JUDGE_ANSWER_CHARS * 3
