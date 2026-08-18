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
from . import interpret
from .interpret import Reading
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

#: Which normalized slot each detector in core/clarify.py is asking about.
#: Those detectors were written for this org before slots existed, so the
#: mapping lives here rather than being retrofitted into their module.
_LEGACY_SLOT_BY_QUESTION = (
    ("which mocks", "object"),
    ("which interviews", "object"),
    ("over what period", "date_range"),
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

    A detector that finds nothing means EXECUTE, not "ask something generic".
    There is no worse moment to invent a question than the one where the
    component that decides what to ask has just failed.
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
    if len(options) < 2:
        # A one-option question is not a question. The detectors always produce
        # at least two; one that stops doing so must degrade to answering, not
        # to a card with a single button.
        return AgentDecision(
            action="EXECUTE_SALESFORCE",
            normalized_intent=(user_text or "").strip()[:600],
            confidence=0.3,
            internal_reason_code="insufficient_options",
        )
    return AgentDecision(
        action="ASK_CLARIFICATION",
        normalized_intent=(user_text or "").strip()[:600],
        confidence=0.4,
        missing_critical_slots=[slot],
        clarification_draft=ClarificationDraft(
            slot=slot,
            header=found.header or "Salesforce",
            question=found.question[:280],
            options=options,
            allow_custom=True,
            # These are alternative readings of ONE number. Ticking two of them
            # is incoherent, and the detectors know that about their own
            # questions in a way a general default cannot.
            multi_select=False,
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
    reading: Optional[Reading] = None,
    grounding_text: str = "",
) -> AgentDecision:
    """The one decision this request runs on.

    Never raises: a planner failure degrades to `deterministic_decision`, which
    is why the caller can treat the return value as always usable.

    `reading` carries the deterministic pass over the request — the spelling
    repairs and the slots the sentence already settles. `grounding_text` is what
    the knowledge layers match on, which is the request PLUS what this
    conversation established, so a follow-up of three words is grounded as well
    as the request it follows.
    """
    from .. import org_brief

    read = reading if reading is not None else interpret.read(user_text)
    history = [
        {
            "slot": round_.slot,
            "question": round_.question,
            "answer": round_.answer,
            "skipped": round_.skipped,
        }
        for round_ in intent.clarification_history
    ]
    # Slots the SENTENCE settles, plus the ones already answered, are closed.
    # Telling the planner which they are is cheaper and far more reliable than
    # hoping it re-derives the same list from the prose.
    settled = sorted(set(read.satisfied) | set(intent.resolved_slots))
    original = intent.original_user_text.strip()
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
        original_request=original if original != (user_text or "").strip() else "",
        domain_knowledge=interpret.domain_knowledge(grounding_text or read.text),
        reading_note=read.note(),
        settled_slots=settled,
        ask_bias=settings.clarify_mode == "always",
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

def _answer_instead(
    decision: AgentDecision, draft: ClarificationDraft, reason: str
) -> AgentDecision:
    """Turn a question we must not ask into an answer that says what it assumed."""
    return decision.model_copy(
        update={
            "action": "EXECUTE_SALESFORCE",
            "clarification_draft": None,
            "assumptions": [
                *decision.assumptions,
                _assumption_for(draft.slot, draft.options),
            ],
            "internal_reason_code": reason,
        }
    )


def _distinct_options(draft: ClarificationDraft) -> bool:
    """At least two options a user could tell apart.

    A planner under pressure produces "Scheduled interviews" and "Interviews
    scheduled" — two rows, one choice. Comparing on the normalized label is
    enough to catch it, and the alternative is a card that cannot be answered
    correctly because both answers mean the same thing.
    """
    def key(label: str) -> str:
        return " ".join(sorted(re.sub(r"[^a-z0-9 ]+", " ", label.lower()).split()))

    return len({key(o.label) for o in draft.options}) >= 2


def enforce_policy(
    decision: AgentDecision,
    intent: PendingIntent,
    *,
    duplicate: bool = False,
    reading: Optional[Reading] = None,
    entity_candidates: Optional[Sequence[dict]] = None,
) -> AgentDecision:
    """Downgrade a decision the clarification policy will not allow.

    The planner is told the rules, and mostly follows them. This is what makes
    them true: a request that has already spent its clarification budget, whose
    question repeats one already asked, or whose answer is sitting in the
    sentence the user typed, is answered with the safest reasonable
    interpretation and a stated assumption — never asked again.

    `duplicate` marks a repeated submission of an answer we have already
    resolved (a double-click, a retried fetch). It is not a new turn, so it must
    not be able to produce a new question: seen live on 2026-08-11, the repeat
    of "Tasks" was met with "what status counts as pending?", which reads as an
    interrogation for doing nothing but clicking twice.

    `reading` is the deterministic pass over the request. It is what closes the
    single most damaging failure this feature has: asking "over what period?"
    about a request whose second word is "today". A model can be told not to do
    that, and mostly will not; a regex over the sentence means it CANNOT.
    """
    if decision.action != "ASK_CLARIFICATION":
        return decision

    draft = decision.clarification_draft
    if draft is None:  # unreachable via validation, cheap to be sure
        return decision.model_copy(update={"action": "EXECUTE_SALESFORCE"})

    if duplicate:
        return _answer_instead(decision, draft, "duplicate_submission")

    if intent.rounds_used >= settings.salesforce_max_clarification_rounds:
        return _answer_instead(decision, draft, "clarification_budget_spent")

    from .models import fingerprint

    if intent.already_asked(fingerprint(draft.slot, draft.question)) or (
        draft.slot in intent.asked_slots()
    ):
        return _answer_instead(decision, draft, "question_already_asked")

    # The user already told us. Answering their own words back at them is the
    # behaviour that makes a clarifying assistant feel like a form to fill in.
    if intent.resolved_slots.get(draft.slot):
        return _answer_instead(decision, draft, "slot_already_resolved")
    if reading is not None and draft.slot in reading.satisfied:
        return _answer_instead(decision, draft, "answered_by_the_request")

    # "Which record did you mean?" is the ONE question whose options are claims
    # about data. Asked "show mocks for John" with no live connection, the
    # planner offered "John D.", "John S." and "John M." — three people who do
    # not exist, presented as a list to pick from, on a 0.95 confidence. Every
    # other slot's options are readings of the request and cost nothing if the
    # model invents one; this slot's options are records, and a fabricated
    # record is indistinguishable from a real one to the person clicking it.
    # So it is allowed only when a real search actually returned candidates.
    if draft.slot == "record_identity" and not entity_candidates:
        return _answer_instead(decision, draft, "unverified_record_options")

    if len(draft.options) < 2 or not _distinct_options(draft):
        # Fewer than two DISTINGUISHABLE options is not a choice. Rather than
        # showing a card that cannot be answered, answer and say what was
        # assumed.
        return _answer_instead(decision, draft, "insufficient_options")
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
