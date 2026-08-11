"""Turn a request into ONE validated AgentDecision.

Three attempts, in descending order of how strictly the shape is enforced, and
a deterministic floor underneath all of them:

    1. tool calling   the served model's own tool parser produces the arguments
    2. guided JSON    decoding is constrained to the schema
    3. one repair     the validation error is handed back verbatim
    4. fallback       the existing deterministic detectors in core/clarify.py

Step 4 is the important one. A planner that fails is not permitted to become a
planner that guesses: when nothing structured survives, the request either gets
the deterministic clarification this org already knows how to ask, or it goes to
the warehouse engine unchanged — never to an unvalidated query.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from ... import llm
from ...config import settings
from .models import (
    AgentDecision,
    ClarificationDraft,
    ClarificationOption,
    ConversationSalesforceState,
    PendingIntent,
)
from .prompts import PLANNER_SYSTEM, planner_user_message

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)

#: The planner's whole job is one call. Bounded so a model that decides to
#: reason at length cannot hold a chat open — the deterministic fallback is
#: always available and is far better than a stalled request.
PLANNER_MAX_TOKENS = 2400

#: The tool the planner is forced to call when tool calling is available. One
#: tool, one call: this is a decision, not a conversation.
DECISION_TOOL_NAME = "submit_plan"


def strip_reasoning(text: str) -> str:
    """Remove any thinking block a model emitted inline.

    Belt and braces: the served model streams reasoning on its own channel
    (`--reasoning-parser qwen3`), so this should never fire — but a runtime
    without that flag inlines `<think>…</think>` into the content, and raw
    chain-of-thought must not reach storage, the UI, or a log line.
    """
    return _THINK_RE.sub("", text or "").strip()


def extract_json_object(raw: str) -> Optional[dict]:
    """The first complete JSON object in a reply, or None.

    Brace-matching rather than a regex: a decision contains nested objects and
    strings with braces in them, and both the greedy and the lazy regex versions
    of this truncate real payloads.
    """
    text = strip_reasoning(raw)
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except ValueError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def decision_schema() -> dict:
    """The JSON schema guided decoding is constrained to."""
    return AgentDecision.model_json_schema()


def decision_tool() -> dict:
    """The OpenAI tool definition for the same schema."""
    return {
        "type": "function",
        "function": {
            "name": DECISION_TOOL_NAME,
            "description": (
                "Submit the routing decision for this Salesforce request. Call "
                "this exactly once. Do not answer in prose."
            ),
            "parameters": decision_schema(),
        },
    }


# ---------------------------------------------------------------------------
# The deterministic floor
# ---------------------------------------------------------------------------

#: Which normalized slot the existing detectors in core/clarify.py are asking
#: about. Those detectors were written for this org before slots existed, so the
#: mapping lives here rather than being retrofitted into their module.
_LEGACY_SLOT_BY_QUESTION = (
    ("which mocks", "object"),
    ("which interviews", "object"),
    ("over what period", "date_range"),
    ("read it right", "result_format"),
)


def _legacy_slot(question: str) -> str:
    lowered = (question or "").lower()
    for needle, slot in _LEGACY_SLOT_BY_QUESTION:
        if needle in lowered:
            return slot
    return "filter"


def deterministic_decision(user_text: str) -> AgentDecision:
    """The decision to use when nothing structured survived.

    Reuses `core/clarify.py`, which encodes real ambiguities measured against
    this org's data ("failed the mock" in a slot returned 7, 20 and 0 on three
    runs of one sentence). Falling back to detectors that were tuned on real
    wrong answers is a much better floor than falling back to "just run it".
    """
    from .. import clarify as legacy

    found = legacy.needs_clarification(user_text or "")
    if found is None:
        return AgentDecision(
            action="EXECUTE_SALESFORCE",
            normalized_intent=(user_text or "").strip()[:600],
            confidence=0.3,
            internal_reason_code="planner_unavailable",
        )
    slot = _legacy_slot(found.question)
    options = [
        ClarificationOption(
            id=f"opt{index + 1}",
            label=option.label[:120],
            description=(option.description or "")[:240],
            value=(option.resolves_to or option.label)[:400],
        )
        for index, option in enumerate(found.options[:4])
    ]
    while len(options) < 2:
        # A one-option question is not a question. The detectors always produce
        # at least two, but a future one that does not must not crash the model.
        options.append(
            ClarificationOption(
                id=f"opt{len(options) + 1}",
                label="Answer it as I asked",
                description="Do not narrow the scope.",
                value="Answer exactly as asked; do not narrow the scope.",
            )
        )
    return AgentDecision(
        action="ASK_CLARIFICATION",
        normalized_intent=(user_text or "").strip()[:600],
        confidence=0.4,
        missing_critical_slots=[slot],
        clarification_draft=ClarificationDraft(
            slot=slot,
            header="Salesforce",
            question=found.question[:280],
            options=options,
            allow_custom=True,
        ),
        internal_reason_code="deterministic_detector",
    )


# ---------------------------------------------------------------------------
# The planner call
# ---------------------------------------------------------------------------

async def _ask_model(
    messages: List[dict], *, thinking: bool, use_tools: bool
) -> Optional[dict]:
    """One planner call. Returns the raw decision dict, or None."""
    if use_tools:
        try:
            _text, calls = await llm.chat_with_tools(
                messages,
                tools=[decision_tool()],
                tool_choice={
                    "type": "function",
                    "function": {"name": DECISION_TOOL_NAME},
                },
                temperature=0.0,
                max_tokens=PLANNER_MAX_TOKENS,
                thinking=thinking,
            )
            for call in calls:
                if call.name == DECISION_TOOL_NAME and call.arguments:
                    return call.arguments
            log.info("planner tool call returned no arguments; using guided JSON")
        except Exception as exc:  # noqa: BLE001 — downgrade, never fail
            log.info(
                "tool calling unavailable for the planner (%s: %s); using guided JSON",
                type(exc).__name__,
                str(exc)[:160],
            )

    raw = await llm.json_completion(
        messages,
        json_schema=decision_schema(),
        schema_name="agent_decision",
        temperature=0.0,
        max_tokens=PLANNER_MAX_TOKENS,
        thinking=thinking,
    )
    return extract_json_object(raw)


async def plan(
    *,
    user_text: str,
    intent: PendingIntent,
    state: ConversationSalesforceState,
    schema_summary: str = "",
    entity_candidates: Optional[Sequence[dict]] = None,
    recent_turns: Sequence[dict] = (),
    effort: str = "medium",
    today: str = "",
    timezone_name: str = "UTC",
) -> AgentDecision:
    """The one decision this request runs on.

    Never raises: a planner failure degrades to `deterministic_decision`, which
    is why the caller can treat the return value as always usable.
    """
    from .. import org_brief

    history = [
        {
            "slot": round_.slot,
            "question": round_.question,
            "answer": round_.answer,
            "skipped": round_.skipped,
        }
        for round_ in intent.clarification_history
    ]
    user_message = planner_user_message(
        user_text=user_text,
        conversation_state=state.brief(),
        clarification_history=history,
        resolved_slots=intent.resolved_slots,
        schema_summary=schema_summary,
        today=today or org_brief.business_today(),
        timezone_name=timezone_name or org_brief.BUSINESS_TIMEZONE,
        effort=effort,
        recent_turns=recent_turns,
        entity_candidates=entity_candidates,
    )
    messages = [
        {"role": "system", "content": PLANNER_SYSTEM},
        {"role": "user", "content": user_message},
    ]
    # Fast and Low answer directly; the planner is a translation task and the
    # reasoning pass comes out of the same token budget as the decision.
    thinking = effort in ("medium", "high")
    use_tools = bool(
        settings.main_capabilities.supports_tool_calling
        and settings.salesforce_planner_tool_calling
    )

    try:
        payload = await _ask_model(messages, thinking=thinking, use_tools=use_tools)
    except Exception as exc:  # noqa: BLE001 — the model server is unreachable
        log.warning("planner call failed (%s: %s)", type(exc).__name__, str(exc)[:200])
        return deterministic_decision(user_text)

    if payload is None:
        log.info("planner produced no JSON object; repairing once")
        payload = await _repair(messages, "the reply contained no JSON object", thinking)
        if payload is None:
            return deterministic_decision(user_text)

    try:
        return AgentDecision.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — validation error, not a crash
        detail = str(exc)[:600]
        log.info("planner decision failed validation; repairing once: %s", detail)

    repaired = await _repair(messages, detail, thinking)
    if repaired is None:
        return deterministic_decision(user_text)
    try:
        return AgentDecision.model_validate(repaired)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "planner decision still invalid after one repair (%s); falling back",
            str(exc)[:200],
        )
        return deterministic_decision(user_text)


async def _repair(
    messages: List[dict], problem: str, thinking: bool
) -> Optional[dict]:
    """ONE constrained retry, carrying the exact validation error.

    Exactly one: a repair loop is a way to spend a minute of a user's time on a
    model that has already demonstrated it cannot produce the shape.
    """
    retry = list(messages) + [
        {
            "role": "user",
            "content": (
                "Your previous reply was rejected:\n"
                f"{problem}\n\n"
                "Reply again with ONLY a JSON object matching the AgentDecision "
                "schema. No prose, no code fence, no reasoning."
            ),
        }
    ]
    try:
        raw = await llm.json_completion(
            retry,
            json_schema=decision_schema(),
            schema_name="agent_decision",
            temperature=0.0,
            max_tokens=PLANNER_MAX_TOKENS,
            thinking=False,  # the repair is mechanical; thinking spends budget
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("planner repair call failed (%s)", str(exc)[:200])
        return None
    return extract_json_object(raw)


# ---------------------------------------------------------------------------
# Post-decision policy
# ---------------------------------------------------------------------------

def enforce_policy(
    decision: AgentDecision, intent: PendingIntent, *, duplicate: bool = False
) -> AgentDecision:
    """Downgrade a decision the clarification policy will not allow.

    The planner is told the rules, and mostly follows them. This is what makes
    them true: a request that has already spent its clarification budget, or
    whose question repeats one already asked, is answered with the safest
    reasonable interpretation and a stated assumption — never asked again.

    `duplicate` marks a repeated submission of an answer we have already
    resolved (a double-click, a retried fetch). It is not a new turn, so it must
    not be able to produce a new question: seen live on 2026-08-11, the repeat
    of "Tasks" was met with "what status counts as pending?", which reads as an
    interrogation for doing nothing but clicking twice.
    """
    if decision.action != "ASK_CLARIFICATION":
        return decision

    draft = decision.clarification_draft
    if draft is None:  # unreachable via validation, cheap to be sure
        return decision.model_copy(update={"action": "EXECUTE_SALESFORCE"})

    if duplicate:
        return decision.model_copy(
            update={
                "action": "EXECUTE_SALESFORCE",
                "clarification_draft": None,
                "assumptions": [
                    *decision.assumptions,
                    _assumption_for(draft.slot, draft.options),
                ],
                "internal_reason_code": "duplicate_submission",
            }
        )

    if intent.rounds_used >= settings.salesforce_max_clarification_rounds:
        return decision.model_copy(
            update={
                "action": "EXECUTE_SALESFORCE",
                "clarification_draft": None,
                "assumptions": [
                    *decision.assumptions,
                    _assumption_for(draft.slot, draft.options),
                ],
                "internal_reason_code": "clarification_budget_spent",
            }
        )

    from .models import fingerprint

    if intent.already_asked(fingerprint(draft.slot, draft.question)) or (
        draft.slot in intent.asked_slots()
    ):
        return decision.model_copy(
            update={
                "action": "EXECUTE_SALESFORCE",
                "clarification_draft": None,
                "assumptions": [
                    *decision.assumptions,
                    _assumption_for(draft.slot, draft.options),
                ],
                "internal_reason_code": "question_already_asked",
            }
        )

    if len(draft.options) < 2:
        # Fewer than two options is not a choice. Rather than showing a card
        # with one button, answer and say what was assumed.
        return decision.model_copy(
            update={
                "action": "EXECUTE_SALESFORCE",
                "clarification_draft": None,
                "assumptions": [
                    *decision.assumptions,
                    _assumption_for(draft.slot, draft.options),
                ],
                "internal_reason_code": "insufficient_options",
            }
        )
    return decision


_SAFE_DEFAULTS = {
    "date_range": "all records currently open, with no date filter",
    "owner_scope": "records you own",
    "region": "every region",
    "status": "every status",
    "result_format": "a short summary with the matching records",
    "grouping": "no grouping",
    "object": "the object named in the request",
    "metric": "a straight record count",
    "comparison_baseline": "no comparison",
    "record_identity": "every matching record",
    "filter": "no additional filter",
}


def _assumption_for(slot: str, options: Sequence[ClarificationOption]) -> str:
    """The assumption stated when a question is NOT asked.

    The first option a planner offers is its own best reading, so it is the
    honest default — and saying which one was taken is what lets the user
    correct it in one message instead of doubting the whole answer.
    """
    if options:
        return f"Assumed {slot.replace('_', ' ')}: {options[0].label}."
    return f"Assumed {slot.replace('_', ' ')}: {_SAFE_DEFAULTS.get(slot, 'the broadest safe reading')}."
