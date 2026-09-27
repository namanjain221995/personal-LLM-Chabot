"""Engines: router, sql, rag, vision, report (spec §8)."""

from typing import List, Sequence


def recent_turns(history: Sequence[dict], n: int) -> List[dict]:
    """The last `n` conversational turns, KEEPING every pinned system block.

    main.py prepends system messages to `history` — the cross-chat recall
    block and the content of pages/repos shared earlier in this chat. A plain
    `history[-6:]` silently sliced those off as soon as the conversation grew
    past the window, so the model stopped seeing context the user had already
    given it. System messages are kept regardless of age; only real turns are
    subject to the count.
    """
    items = list(history)
    system = [m for m in items if m.get("role") == "system"]
    turns = [m for m in items if m.get("role") != "system"]
    return system + turns[-n:] if n > 0 else system


def conversation_turns(history: Sequence[dict], n: int) -> List[dict]:
    """The last `n` real turns, WITHOUT the pinned system blocks.

    `recent_turns` keeps every system message on purpose — the chat engine
    needs the user's saved facts, cross-chat recall and the excerpts of pages
    and documents shared in this chat. A prompt whose OUTPUT LEAVES THE BOX
    must not see them: the search query rewriter turns its context into
    SearXNG queries, so a saved fact or a document excerpt in that prompt is a
    saved fact or a document excerpt on the wire to third-party engines; and
    the research planner, given the memory block, once listed the signed-in
    user's own name as an entity to research. Only what the user actually
    said and what the assistant answered is context for those. The same
    blocks still reach the ANSWER prompt, which stays on this machine.

    Deep Research keeps a private copy of this (`_conversation_turns`); the
    behaviour is identical.
    """
    return [m for m in recent_turns(history, n) if m.get("role") != "system"]

# First-run state: the warehouse/vector store do not exist until the
# sync-worker completes its first Salesforce extract. Engines stream this
# as a NORMAL answer (not an error event) so the UI stays friendly.
NO_DATA_MESSAGE = (
    "There's no Salesforce data on this machine yet — the sync worker hasn't "
    "completed its first extract. Once it runs (it needs the AWS credentials "
    "and region in `.env`), ask me again and every answer will come straight "
    "from your synced Salesforce org."
)

# Formatting: the chat UI renders every answer as Markdown, and the prompt
# never said so. Asked "be helpful, clear, and concise" and nothing else, the
# model answered in plain lines — no headings, no bold labels, no bullets —
# while the same question put to ChatGPT came back sectioned (owner report,
# 2026-09-17).
#
# The second half of the same report is a REWRITE: a person pasted a
# plain-text sample, pasted the source to rewrite, and asked for "the same
# format". Two things went wrong. The plain-text sample was copied literally,
# plain text and all, so the answer had no Markdown at all; and a section the
# source said nothing about was filled with invented content. In one synthetic
# reproduction the model answered with the SAMPLE's own content instead of the
# source's. So the rule names both sides explicitly: the sample decides the
# SHAPE, the source decides the CONTENT, and an empty section says so.
#
# Chat only (engines/chat.py, assistant mode). The Fast small-talk lane and
# the Salesforce chat prompt are short on purpose, and the search, url,
# document, rag and dataset prompts have formatting rules of their own.
FORMAT_INSTRUCTION = (
    "\n\nFORMAT: answers are rendered as Markdown. When the content has "
    "structure, show it: ## and ### headings for sections, - bullets for "
    "lists of items, **bold** for labels and key terms (for example "
    "**Location:** Austin, TX), and a Markdown table when you compare things "
    "or give rows of values. Leave a blank line between paragraphs, headings, "
    "lists and tables. A short conversational reply — a greeting, an "
    "acknowledgement, a one- or two-sentence answer — stays plain prose, with "
    "no headings.\n"
    # Hotfix 1.2 (P2, P4). "...and its level of detail" let a sample with two
    # "Must Have" lines cap a 21-requirement posting at 8-14 of 21 on Fast;
    # without it and with the keep-every-item sentence, 17-21 of 21 in 18
    # runs. The last sentence names the usual answers the model supplied
    # where the source was silent ("apply through the careers portal", "a
    # team of 14 developers" when the source said 14 people). "With all of
    # its details" and "taking them from the sample": a later live round kept
    # every item but shortened five, and copied the sample's "Email your CV".
    "REWRITES: when the user gives a sample, template or earlier answer and "
    "asks for the same format, the same shape or the same way, the SAMPLE "
    "decides the shape — its sections, their order and its labels — and the "
    "SOURCE they asked you to rewrite decides the content. Rewrite the "
    "source into that shape; never hand back the sample's own content as the "
    "answer. Keep EVERY item the source lists (every requirement, "
    "responsibility and benefit) with all of its details, every tool, name "
    "and number in it: the number of lines under a sample's section is never "
    "a limit on the rewrite. A plain-text sample still "
    "comes back as Markdown: its section names become headings, its 'Label: "
    "value' lines keep the label in bold, and its item lines become bullets. "
    "Fill every section from the source alone; where the source has nothing "
    "for a section, write 'Not specified' rather than inventing entries for "
    "it or taking them from the sample. Never add a detail the source does "
    "not state, however usual it is (how to apply, a team size, a salary), "
    "and keep every number with what it counts.\n"
    "Length follows the ask: when the user asks for something big or complete "
    "(a full report, every item, a detailed rewrite), write all of it instead "
    "of a summary."
)

#: The CLOSED role vocabulary a diagram may attach to a node.
#:
#: Shared verbatim with the frontend: `DIAGRAM_ROLES` in
#: frontend/lib/mermaidTheme.ts turns each name into the `classDef` that paints
#: it, and frontend/app/globals.css holds the paints as `--ts-diagram-<role>-*`
#: for both themes. A diagram names a ROLE and never a colour, which is the
#: whole reason a role can be allowed where a hex cannot: a name is a
#: vocabulary we can validate on both sides, a hex is not.
#:
#: Adding a fifth name here without a paint on the other side would render as
#: an uncoloured node, so tests/test_diagram_role_vocabulary.py asserts this
#: tuple against the frontend's list and its CSS tokens.
DIAGRAM_ROLES = ("service", "store", "model", "external")

# Diagrams: the UI renders ```mermaid blocks as real, zoomable, downloadable
# diagrams, and the document renderer now draws the same source as a coloured
# figure inside a .docx and a .pdf (artifacts/render/diagrams.py). The
# instruction stays deliberately conservative — an earlier, more eager version
# made the model decorate ordinary answers with diagrams and invent giant,
# syntax-error-prone graphs with unreadable custom colors.
#
# WHAT THE BAN ON style/classDef BECAME: a ROLE. The model names what a node
# IS, from the closed list above, and the renderers pick the colour — which is
# what makes a coloured diagram possible without letting a model choose an
# unreadable pair. Both halves of that colour were already built and could not
# meet: the frontend has carried the four-colour palette since 2026-09-22 and
# this block banned `classDef` while never mentioning the `:::` form, so every
# chat diagram painted on one default node fill, which is the owner's report.
#
# THE SIZE IS A TEST, AND IT WAS RAISED ONCE, DELIBERATELY. This string is
# concatenated at ELEVEN call sites (chat.py x2, rag.py, repo.py x2, agent.py,
# url.py x2, dataset.py, document.py, search.py), on the chat path, at EVERY
# effort, with no gate — so a character added here is prefill on every Fast
# turn in nine engines. The one-diagram cap, the "ordinary questions get none"
# rule and the ~20-node legibility cap are therefore all kept; the
# three-diagram allowance for a DOCUMENT lives on the artifact path, which
# never imports this string.
#
# Measured 2026-09-27 by importing the string on each tree:
#
#   origin/dev (1f80aa3, 4164bb8)          1,082 CHARACTERS, 1,086 UTF-8 bytes
#   fix/document-vocabulary-r2             1,082 characters, 1,088 bytes
#   fix/diagram-roles                      1,848 characters, 1,858 bytes
#   naive merge, diagram-roles' wording    1,822 characters, 1,832 bytes
#   integ/diagram-group (7f16f4b7)         1,549 characters, 1,553 bytes
#   THIS FILE                              1,602 characters, 1,606 bytes
#
# The two branches wrote the same fix twice. This is ONE wording carrying every
# rule from both: fix/diagram-roles' ban on a role outside `flowchart`/`graph`
# (outside them `:::` is a parse error and the reader gets NO diagram, measured
# by its author in mermaid 11.17 — strictly worse than grey, which is why that
# wording was chosen over the shorter one), its colour-value ban and its two
# accessibility rules, plus fix/document-vocabulary-r2's compression of the
# surrounding prose. +520 characters over origin/dev is what could not be
# removed while keeping the type ban: 71 of them are the seven diagram-type
# names the ban has to name to be concrete, and the rest is the role list, its
# glosses, the "do not invent a name" rule and the two accessibility rules.
# tests/test_diagram_instruction_budget.py pins both units at the numbers
# above and reads this comment, so neither can drift back.
#
# THE LAST 53 CHARACTERS ARE origin/dev's OWN PROSE, PUT BACK. The 1,549
# reconciliation dropped three fragments that no test held, which is why the
# loss was silent. Each changes what the model is told, so each is back and
# pinned: "inside labels" (+13), without which the ban on brackets and pipes
# reads globally and forbids the `A["Payments API"]` this very string then
# requires; "custom colours break dark mode" (+32), the REASON the directive
# ban exists, which the compression had replaced with what happens next;
# and "prefer" over "use" for `flowchart TD`/`LR` (+4), because "use" makes
# the flowchart mandatory four sentences before this string explains what to
# do inside seven other diagram types. Two further fragments stay dropped and
# are listed in the budget test so the account is complete: the second worked
# example `(e.g. A["Login page"])`, and the adjective in "plain, SIMPLE".
#
# THE TOKEN COST, MEASURED 2026-09-27 — this is the unit prefill is charged
# in. Pinned engine tokenizer, on CPU, no GPU touched (tokenizers 0.23.2 over
# Model/repos/nvidia--Qwen3.6-35B-A3B-NVFP4--491c2f1ea524/tokenizer.json):
#
#   origin/dev 232 tokens; document-vocabulary-r2 240; diagram-roles 415;
#   integ/diagram-group 356; THIS FILE 365.
#
# So +133 tokens per prompt over origin/dev, at every effort, in nine engines.
# Against the twelve context_assembly_golden fixtures — 41,496 tokens on
# origin/dev, mean 3,458 per prompt, 43,092 here, exactly +133 on each of the
# twelve — that is 3.85% of the mean prompt. The +183 / 5.3% figure carried
# through the release is fix/diagram-roles' 1,848-character wording, NOT this
# one. The old note here claimed "+8 tokens per prompt / 30,832 -> 30,928
# across the twelve golden fixtures"; that claim stays withdrawn, and the
# numbers above replace it with first-hand ones.
#
# THE CHAT UI IS UNAFFECTED, CHECKED RATHER THAN ASSUMED. `A["x"]:::role`
# reaches the browser's mermaid, which has no `classDef` for these names.
# mermaid 11.17.0 `setClass` (dist/chunks/mermaid.core/chunk-RHFEMEQ7.mjs:423)
# only pushes the name onto the node's class list; it never looks the class
# up and never raises, so an undefined class is a CSS class nothing styles.
# The frontend's own classDefs (fix/diagram-roles) are what paint them.
DIAGRAM_INSTRUCTION = (
    "\n\n"
    "DIAGRAMS: ```mermaid blocks render as zoomable, downloadable "
    "diagrams. Include one ONLY when the user explicitly asks for a "
    "diagram/flowchart/visualization, or when a picture is easier to "
    "follow than prose for something genuinely complex (a system "
    "architecture, a multi-step process, entity relationships). "
    "Ordinary questions, short answers and conversation must NOT "
    "contain a diagram. When you do, follow ALL of these rules: at most "
    "ONE diagram per answer; keep it SMALL (under ~20 nodes — "
    "summarize, don't enumerate); prefer `flowchart TD` or `flowchart "
    "LR`; one statement per line; every label in double quotes and "
    "short, with no parentheses, brackets, pipes or markdown inside "
    "labels; NEVER use style, classDef, linkStyle, click or %%{init}%% "
    "directives and never write a colour of your own (no hex, no rgb(), "
    "no colour name): custom colours break dark mode, and the app "
    "paints a ROLE instead. In a `flowchart`/`graph` you MAY give a "
    "node ONE role from this CLOSED list, written with `:::`: service "
    "(code), store (data), model (an AI model), external (a person or "
    'outside system) — A["Payments API"]:::service. Tag only the nodes '
    "one fits; an invented name paints nothing. Put NO role in any "
    "other type (sequenceDiagram, erDiagram, pie, journey, timeline, "
    "mindmap, gitGraph): there `:::` is a syntax error and the diagram "
    "fails to draw. The LABEL carries the meaning: two nodes must never "
    "differ by colour alone, so it reads for someone who cannot see "
    "colour. Never draw ASCII-art boxes. Right after it, add one or two "
    "plain sentences saying what it shows so a non-technical reader can "
    "follow it."
)

# Code: the UI renders fenced blocks with syntax highlighting and a copy
# button, so code belongs in a fence with a language tag. The rules below are
# the difference between a snippet that looks right and one that RUNS.
CODE_INSTRUCTION = (
    "\n\nCODE: put every piece of code in a fenced block tagged with its "
    "language (```python, ```sql, ```ts, ```bash). Write code that runs as "
    "given: include the imports it needs, use exact library and API names, and "
    "handle the error cases that matter. Prefer one complete, working file "
    "over fragments the reader has to assemble. Do not add line numbers, and "
    "do not fill the code with narration — a comment earns its place by "
    "explaining WHY, not by restating the line below it. After the block, "
    "briefly say how to run it and call out anything the user must change "
    "(paths, credentials, versions). If a request FOR CODE is ambiguous, state "
    "the assumption you coded against in one line rather than asking and "
    "stopping. If you are unsure whether an API exists, say so instead of "
    "inventing one.\n"
    # Scoped, 2026-09-08. This block is attached to EVERY assistant turn, and
    # the ambiguity clause above used to read "if the request is ambiguous,
    # state the assumption you coded against" — unconditionally. Inside a
    # French lesson, "how to translate" is ambiguous, and the model did
    # exactly as instructed: it assumed the programming reading and answered
    # with googletrans. The clause is about how to handle an ambiguous CODING
    # request; it is not a licence to read every ambiguous question as one.
    "This section governs HOW to present code when code is what was asked "
    "for. It is not a reason to answer a non-programming question with a "
    "program: if the conversation is about something else, stay in it."
)
