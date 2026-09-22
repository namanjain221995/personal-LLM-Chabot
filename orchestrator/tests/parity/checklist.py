"""The checklist the owner's prompt actually asks for, made machine-checkable.

PROVENANCE. Every check below is traced to a clause of the owner's report
request of 2026-09-22, quoted verbatim in `clause`; the request itself is
`prompt.txt` beside this file, 914 characters. A check with clause="" is NOT
in the prompt -- it comes from the owner's separate complaint about diagrams
-- and is scored separately so no one can claim the prompt asked for it.

The conversation's own identifier is deliberately NOT recorded anywhere in
this package. This repository is public, and a conversation id is a live
handle to a real person's private conversation on a public-facing
deployment, in the same class as the artifact and file identifiers that
`test_no_identifier_shaped_literals_in_runs` keeps out of `runs/`. The date
and the prompt text are the provenance; they are reproducible and they
identify nobody.

THE BAR IS NOT "COPY CHATGPT". ChatGPT's answer (3,419 words, 15 H2, 57 H3,
32 fences, 62 table rows, 7 blockquotes) is ONE reference point, used to
place the floors somewhere defensible, not to set a target. Floors are
deliberately BELOW ChatGPT on every axis: the question is whether a request
was honoured, not whether two answers match.

ONE SCORER, BOTH ROUTES. A chat answer is Markdown. A file is an
ArtifactSpec. `normalise.py` turns a DocumentSpec into the Markdown it is
equivalent to, so the same checklist judges the chat route and the file
route and the comparison is fair.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


#: The 15 sections the prompt names, verbatim and in the order given.
REQUIRED_SECTIONS: List[str] = [
    "Executive Summary",
    "Architecture Overview",
    "Hardware Layer",
    "AI Inference Layer",
    "Backend Architecture",
    "Frontend Architecture",
    "Database Architecture",
    "RAG Pipeline",
    "Authentication and Authorization",
    "Security",
    "Monitoring",
    "Scaling Strategy",
    "Failure Recovery",
    "Performance Optimization",
    "Conclusion",
]

#: The title the prompt asks for. NOTE the EN DASH (U+2013), not a hyphen.
REQUIRED_TITLE = "Enterprise Local AI Platform – Technical Overview"

#: The context items the prompt lists. A report that never mentions one of
#: these did not read the brief.
CONTEXT_ITEMS: List[str] = [
    "DGX Spark", "Qwen", "vLLM", "PostgreSQL", "FastAPI",
    "Next.js", "Redis", "RAG", "web search", "speech-to-text",
    "text-to-speech", "300",
]

# ----------------------------------------------------------------- floors --
#
# Each floor is justified here so a builder can argue with the NUMBER rather
# than guess at the intent. "ChatGPT" figures are measured from
# backups/chatgptoutput.txt (3,419 words).

#: Words of body per section before a section counts as "substantive".
#: MEASURED, not guessed: with this file's prose counter (headings, code
#: fences and diagram fences excluded) the reference's thinnest section is
#: "Authentication and Authorization" at 102 words and its fattest is
#: "Performance Optimization" at 296; the mean is 165. Our production run
#: averages 70 and its thinnest section is 50. The floor is set at 100 --
#: BELOW every section the reference wrote, so the bar can never be called
#: "match ChatGPT", and above 13 of our 15.
SECTION_WORD_FLOOR = 100

#: Total words, on the SAME prose counter as the per-section floor. The
#: reference is 3,419 words by `wc -w`, but 2,475 by this counter (the
#: difference is 73 headings, 5 diagram fences, 3 code fences and the fence
#: markers themselves). Quoting 3,419 here and measuring 2,475 would be a
#: floor no honest answer could clear, so the floor is 2,000: below the
#: reference's own prose, and nearly double our production run's 1,057.
#: The upper bound exists so "pad it out" is not a winning strategy.
TOTAL_WORDS_MIN = 2_000
TOTAL_WORDS_MAX = 8_000

#: "Use headings" -- the report names 15 sections, so 15 headings is the
#: least an answer that used a heading per section can show. Named here
#: rather than left as a literal in score.py so that every floor lives in
#: one file and `test_every_floor_is_below_the_reference` can see it: the
#: reference writes 73.
HEADINGS_MIN = 15

#: "subheadings" is plural and the report has 15 sections. ChatGPT gave 57
#: subheadings across all 15. Requiring 8 of 15 to carry at least one is
#: about a seventh of that.
SECTIONS_WITH_SUBHEADING_MIN = 8

#: "tables" (plural). ChatGPT: 62 table rows. Three tables in a 15-section
#: infrastructure report is the least that satisfies a plural.
TABLES_MIN = 3

#: "code blocks" (plural). ChatGPT: 32 fenced blocks.
CODE_BLOCKS_MIN = 3

#: "warnings, notes". Two kinds named, so at least one of each, or two
#: callouts with at least one warning.
CALLOUTS_MIN = 2
WARNINGS_MIN = 1

#: "bullet points", "numbered steps" -- plural, so more than one each.
BULLET_LISTS_MIN = 3
NUMBERED_LISTS_MIN = 1

#: "bold text". A single bold run satisfies the literal ask; requiring 10
#: across 2,500+ words is what "use bold text where appropriate" means.
BOLD_RUNS_MIN = 10

#: "and recommendations where appropriate". Counted as mentions of
#: `recommend*` rather than as a section, because the request says "where
#: appropriate" and not "a recommendations section". Named here for the same
#: reason as HEADINGS_MIN; the reference writes 6.
RECOMMENDATION_MENTIONS_MIN = 3

#: "Do not repeat information unnecessarily." Two paragraphs that share
#: this fraction of their 8-word shingles are the same paragraph twice.
REPEAT_SHINGLE_OVERLAP = 0.60

#: How many of the 12 context items must appear.
CONTEXT_ITEMS_MIN = 10

#: NOT FROM THE PROMPT -- from the owner's complaint that our diagrams are
#: missing and colourless. Scored in the `extra` group.
DIAGRAMS_MIN = 1

#: How many of a diagram's nodes must carry a role class before the diagram
#: counts as role-tagged. One: a diagram in which a single node is marked as
#: a store or a model is already a diagram the renderer can colour, and
#: demanding all of them would fail a correct drawing that has one plain box.
DIAGRAM_ROLED_NODES_MIN = 1


@dataclass
class Check:
    """One machine-checkable requirement."""

    id: str
    clause: str          # the prompt text this comes from; "" = not in the prompt
    description: str
    group: str = "prompt"  # "prompt" | "extra"


CHECKS: List[Check] = [
    Check("title", 'Create a professional technical report titled: "Enterprise Local AI Platform – Technical Overview"',
          "The title appears, with the en dash the request used."),
    Check("sections_present", "Requirements: 1. Executive Summary ... 15. Conclusion / Do not skip any section.",
          "All 15 named sections appear as top-level headings."),
    Check("sections_in_order", "Requirements: 1. ... 15. ...",
          "The 15 sections appear in the order the request numbered them."),
    Check("sections_substantive", "Do not skip any section.",
          f"Every section carries at least {SECTION_WORD_FLOOR} words of body."),
    Check("total_words", "professional technical report / Do not skip any section.",
          f"Total body is between {TOTAL_WORDS_MIN:,} and {TOTAL_WORDS_MAX:,} words."),
    Check("headings", "Use headings",
          f"At least {HEADINGS_MIN} headings are used for structure."),
    Check("subheadings", "subheadings",
          f"At least {SECTIONS_WITH_SUBHEADING_MIN} of the 15 sections carry a subheading."),
    Check("tables", "tables",
          f"At least {TABLES_MIN} tables."),
    Check("bullets", "bullet points",
          f"At least {BULLET_LISTS_MIN} bullet lists."),
    Check("numbered", "numbered steps",
          f"At least {NUMBERED_LISTS_MIN} numbered list."),
    Check("bold", "bold text",
          f"At least {BOLD_RUNS_MIN} bold runs."),
    Check("code_blocks", "code blocks",
          f"At least {CODE_BLOCKS_MIN} fenced code blocks."),
    Check("callouts", "warnings, notes",
          f"At least {CALLOUTS_MIN} callouts, of which at least {WARNINGS_MIN} is a warning."),
    Check("recommendations", "and recommendations where appropriate",
          f"At least {RECOMMENDATION_MENTIONS_MIN} recommendations are present."),
    Check("no_repeats", "Do not repeat information unnecessarily.",
          "No duplicated heading and no near-duplicate paragraph."),
    Check("context_used", "Context: - 10 NVIDIA DGX Spark systems - ... - 300 enterprise users",
          f"At least {CONTEXT_ITEMS_MIN} of the {len(CONTEXT_ITEMS)} context items are used."),
    Check("markdown_clean", "Use professional Markdown.",
          "No literal Markdown syntax left as visible text (e.g. '**' shown to the reader)."),
    Check("diagrams", "", "At least one architecture diagram.", group="extra"),
    # RENAMED from `diagram_colour` when this harness moved into the
    # repository, and REDEFINED -- see score.py's DIAGRAM_ROLE_RE for the
    # argument. In short: a Markdown scorer cannot see colour, it can only
    # see the directives that ask for it, and this release's own prompt
    # forbids the model to write those directives while its sanitiser strips
    # them. A check that is unpassable by construction is not a check. What
    # the scorer CAN see, and what the renderer actually colours from, is a
    # per-node role class.
    Check("diagram_roles", "",
          "Diagram nodes carry a role class the renderer can colour from.",
          group="extra"),
]

CHECKS_BY_ID = {c.id: c for c in CHECKS}


@dataclass
class Result:
    check_id: str
    passed: bool
    observed: str
    group: str = "prompt"


@dataclass
class Score:
    label: str
    results: List[Result] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def prompt_results(self) -> List[Result]:
        return [r for r in self.results if r.group == "prompt"]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.prompt_results if r.passed)

    @property
    def total(self) -> int:
        return len(self.prompt_results)

    def failures(self) -> List[Result]:
        return [r for r in self.results if not r.passed]
