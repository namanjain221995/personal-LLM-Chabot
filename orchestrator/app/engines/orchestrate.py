"""Auto-orchestration (2026-07-28): the model decides how hard to work.

The Agent toggle is gone from the UI. Instead, before answering, a cheap
non-thinking classification call looks at the request and decides whether it
needs:

  * AGENT   — break the task into steps, run them, then synthesize
              (multi-part work: "build X", "compare A/B/C", "audit …");
  * SEARCH  — fetch live web sources
              (anything current, factual-external, or explicitly asked for).

What each effort level is allowed to do:

    fast    → nothing. Answer directly, no reasoning pass, no tools.
    low     → web search if the question needs it. Still no reasoning, no agent.
    medium  → agent steps and/or web search, as the request warrants.
    high    → same as medium; the reasoning pass simply runs longer.

Other limits:
- an explicit user choice always wins: if the client still sends agent=true or
  web_search=on/off, that is honoured and this never runs;
- any failure returns "do neither", so a classification hiccup degrades to a
  normal answer instead of breaking the turn.

The decision runs with thinking OFF and a tiny token budget, so it costs a
fraction of a second even on the 35B.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Sequence

from .. import llm
from ..config import settings

_JSON_RE = re.compile(r"\{.*\}", re.S)

_SYSTEM = (
    "You route a user's request to the right amount of work. Answer with ONLY "
    "a JSON object, no prose:\n"
    '{"agent": true|false, "search": true|false}\n\n'
    "agent = true when the request needs SEVERAL steps to answer well: "
    "building or designing something, comparing multiple things, auditing or "
    "reviewing, planning a project, or any task with clearly separable parts.\n"
    "agent = false for a single question, a definition, a lookup, small talk, "
    "a rewrite, or anything answerable in one pass.\n\n"
    "search = true when the answer depends on information from the live web: "
    "current events, today's prices or news, product/version specifics, "
    "anything the user says to look up, or facts likely to have changed "
    "recently.\n"
    "search = false for general knowledge, reasoning, maths, code, writing, "
    "or questions about data the user already provided."
)

_FEW_SHOTS = [
    ("hello, my name is Naman", {"agent": False, "search": False}),
    ("what is a CRM?", {"agent": False, "search": False}),
    ("who won the election yesterday?", {"agent": False, "search": True}),
    (
        "build me a full onboarding plan: research the market, draft the "
        "phases, and list the risks",
        {"agent": True, "search": True},
    ),
    (
        "compare our top 3 accounts and write a summary for each",
        {"agent": True, "search": False},
    ),
    ("rewrite this paragraph to be shorter", {"agent": False, "search": False}),
]

# The opening of a request is enough to classify it.
_INPUT_CAP = 2000


@dataclass
class Plan:
    agent: bool
    search: bool
    auto: bool = True  # False when the user chose explicitly


def _messages(message: str, history: Sequence[dict]) -> list:
    shots: list = []
    for text, answer in _FEW_SHOTS:
        shots.append({"role": "user", "content": text})
        shots.append({"role": "assistant", "content": json.dumps(answer)})
    return [
        {"role": "system", "content": _SYSTEM},
        *shots,
        # Only the last couple of turns matter for "is this a big task?".
        *[m for m in list(history)[-2:] if m.get("role") != "system"],
        {"role": "user", "content": (message or "")[:_INPUT_CAP]},
    ]


def parse_plan(raw: str) -> Plan:
    """Parse the classifier's JSON; anything unreadable means 'do neither'."""
    match = _JSON_RE.search(raw or "")
    if not match:
        return Plan(agent=False, search=False)
    try:
        data = json.loads(match.group(0))
    except Exception:
        return Plan(agent=False, search=False)
    return Plan(
        agent=data.get("agent") is True,
        search=data.get("search") is True,
    )


#: Which tools each effort level may use.
ALLOWED = {
    "fast": {"agent": False, "search": False},
    "think": {"agent": True, "search": True},
    "max": {"agent": True, "search": True},
}


def allowances(effort: str) -> dict:
    """What this effort level is permitted to do (unknown → treat as medium)."""
    return ALLOWED.get(llm.normalize_effort(effort), ALLOWED["think"])


async def decide(message: str, history: Sequence[dict], effort: str) -> Plan:
    """Decide whether this request deserves agent steps and/or web search.

    The classifier only ever narrows what the effort level already permits —
    it can never escalate beyond it, so "Fast" stays fast no matter what the
    model thinks of the question.
    """
    allowed = allowances(effort)
    if not (allowed["agent"] or allowed["search"]) or not (message or "").strip():
        return Plan(agent=False, search=False)
    try:
        raw = await llm.router_chat_completion(
            _messages(message, history), temperature=0.0, max_tokens=40
        )
        plan = parse_plan(raw)
    except Exception:
        # A classification failure must never cost the user their answer.
        return Plan(agent=False, search=False)
    agent = plan.agent and allowed["agent"]
    search = plan.search and allowed["search"]
    # High must never do LESS than Medium. The classifier is a small model and
    # its agent/search call varies run to run: the same research question came
    # back as {agent, search} at Medium and {search} at High, so the level the
    # user picked for hard work answered with a one-shot search while the level
    # below it planned. Anything worth searching for at High is worth planning,
    # so the two travel together there. A question needing no tools at all is
    # still answered directly — High does not mean "always slow".
    if llm.normalize_effort(effort) == "max" and search:
        agent = True
    return Plan(agent=agent, search=search)


def describe(plan: Plan) -> str:
    """Short label for the UI, so auto-escalation is never invisible."""
    if plan.agent and plan.search:
        return "Planning steps and searching the web"
    if plan.agent:
        return "Planning the steps for this task"
    if plan.search:
        return "Searching the web"
    return ""
