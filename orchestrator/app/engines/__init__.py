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

# First-run state: the warehouse/vector store do not exist until the
# sync-worker completes its first Salesforce extract. Engines stream this
# as a NORMAL answer (not an error event) so the UI stays friendly.
NO_DATA_MESSAGE = (
    "There's no Salesforce data on this machine yet — the sync worker hasn't "
    "completed its first extract. Once it runs (it needs the AWS credentials "
    "and region in `.env`), ask me again and every answer will come straight "
    "from your synced Salesforce org."
)

# Diagrams: the UI renders ```mermaid blocks as real, zoomable, downloadable
# diagrams. The instruction is deliberately conservative — an earlier, more
# eager version made the model decorate ordinary answers with diagrams and
# invent giant, syntax-error-prone graphs with unreadable custom colors.
DIAGRAM_INSTRUCTION = (
    "\n\nDIAGRAMS: the interface renders ```mermaid code blocks as real "
    "diagrams the user can zoom and download. Include one ONLY when the user "
    "explicitly asks for a diagram/flowchart/visualization, or when you are "
    "explaining something genuinely complex (a system architecture, a "
    "multi-step process, entity relationships) where a picture is clearly "
    "easier to understand than prose. Ordinary questions, short answers and "
    "conversation must NOT contain a diagram. When you do draw one, follow "
    "ALL of these rules: at most ONE diagram per answer; keep it SMALL "
    "(under ~20 nodes — summarize, don't enumerate); prefer `flowchart TD` or "
    "`flowchart LR`; one statement per line; every label in double quotes "
    '(e.g. A["Login page"]) and short, with no parentheses, brackets, pipes '
    "or markdown inside labels; NEVER use style, classDef, linkStyle, click "
    "or %%{init}%% directives — the app applies its own theme and custom "
    "colors break dark mode. Never draw ASCII-art boxes. Right after the "
    "diagram, add one or two plain, simple sentences explaining what it "
    "shows so a non-technical reader can follow it."
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
    "(paths, credentials, versions). If the request is ambiguous, state the "
    "assumption you coded against in one line rather than asking and stopping. "
    "If you are unsure whether an API exists, say so instead of inventing one."
)
