"""Pending-intent and conversation-state persistence.

The only place that mints clarification ids and resume tokens, and the only
place that turns a stored jsonb blob back into a validated model. Everything
above this module works with pydantic objects; everything below it works with
rows.

Two invariants live in the DATABASE rather than here, because they must survive
two requests racing (a click and a stale tab, a double-click, a retried fetch):

    one pending clarification per conversation  — a partial unique index
    the first response wins                     — UPDATE ... WHERE state='pending'

`db.py` owns both. This module is what makes them usable.
"""
from __future__ import annotations

import logging
import secrets
from typing import List, Optional, Sequence, Tuple

from ... import db
from .models import (
    AgentDecision,
    ClarificationDraft,
    ClarificationOption,
    ClarificationRequest,
    ClarificationResponse,
    ClarificationRound,
    ConversationSalesforceState,
    PendingIntent,
    fingerprint,
    new_id,
    utcnow_iso,
)

log = logging.getLogger(__name__)


class ClarificationRejected(ValueError):
    """A clarification response that must not be acted on."""


def _run(fn, *args, **kwargs):
    """db calls are blocking; engines are async. Callers await this."""
    return db.run_in_thread(fn, *args, **kwargs)


# ---------------------------------------------------------------------------
# Conversation state
# ---------------------------------------------------------------------------

async def load_state(conversation_id: str) -> ConversationSalesforceState:
    """This conversation's Salesforce context. Never raises: an unreadable or
    outdated row degrades to a blank state, which only costs a follow-up its
    inherited filters — much better than failing the request."""
    try:
        payload = await _run(db.get_sf_conversation_state, conversation_id)
    except Exception as exc:  # noqa: BLE001 — state is an optimisation
        log.warning("could not read Salesforce state for %s: %s", conversation_id, exc)
        return ConversationSalesforceState(conversation_id=conversation_id)
    if not payload:
        return ConversationSalesforceState(conversation_id=conversation_id)
    try:
        return ConversationSalesforceState.model_validate(
            {**payload, "conversation_id": conversation_id}
        )
    except Exception as exc:  # noqa: BLE001 — a shape change must not 500
        log.warning("discarding unreadable Salesforce state for %s: %s", conversation_id, exc)
        return ConversationSalesforceState(conversation_id=conversation_id)


async def save_state(state: ConversationSalesforceState) -> None:
    state.updated_at = utcnow_iso()
    try:
        await _run(
            db.save_sf_conversation_state,
            state.conversation_id,
            state.model_dump(mode="json"),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not save Salesforce state for %s: %s", state.conversation_id, exc)


# ---------------------------------------------------------------------------
# Intents
# ---------------------------------------------------------------------------

async def load_intent(intent_id: str) -> Optional[PendingIntent]:
    row = await _run(db.get_sf_intent, intent_id)
    if not row:
        return None
    try:
        return PendingIntent.model_validate(row["payload"])
    except Exception as exc:  # noqa: BLE001
        log.warning("discarding unreadable intent %s: %s", intent_id, exc)
        return None


async def load_open_intent(conversation_id: str) -> Optional[PendingIntent]:
    row = await _run(db.latest_open_sf_intent, conversation_id)
    if not row:
        return None
    try:
        return PendingIntent.model_validate(row["payload"])
    except Exception as exc:  # noqa: BLE001
        log.warning("discarding unreadable open intent for %s: %s", conversation_id, exc)
        return None


async def save_intent(intent: PendingIntent) -> None:
    intent.updated_at = utcnow_iso()
    await _run(
        db.save_sf_intent,
        intent.intent_id,
        intent.conversation_id,
        intent.status,
        intent.model_dump(mode="json"),
    )


def new_intent(
    conversation_id: str,
    *,
    text: str,
    root_user_message_id: str,
    carried_slots: Optional[dict] = None,
) -> PendingIntent:
    """A fresh root intent, pre-seeded with what the conversation established.

    Seeding is what makes "what about EMEA?" work: the follow-up starts with the
    previous object, metric, period and owner scope already resolved, and the
    planner only has to change the one thing the user actually said.
    """
    return PendingIntent(
        intent_id=new_id("int"),
        conversation_id=conversation_id,
        root_user_message_id=root_user_message_id,
        original_user_text=text,
        resolved_slots=dict(carried_slots or {}),
    )


# ---------------------------------------------------------------------------
# Clarifications
# ---------------------------------------------------------------------------

def _normalize_options(
    draft: ClarificationDraft,
) -> List[ClarificationOption]:
    """Options with stable ids, deduped, capped, in the order the planner gave.

    Ids matter: the client sends one back, and an option whose id changes
    between the card being rendered and the answer arriving cannot be resolved.
    A planner that returns duplicate or empty ids gets deterministic ones here
    rather than having its whole decision thrown away.
    """
    out: List[ClarificationOption] = []
    seen = set()
    for index, option in enumerate(draft.options):
        raw = (option.id or "").strip() or f"opt{index + 1}"
        candidate = raw
        suffix = 2
        while candidate in seen:
            candidate = f"{raw}-{suffix}"
            suffix += 1
        seen.add(candidate)
        out.append(option.model_copy(update={"id": candidate}))
    return out


async def open_clarification(
    intent: PendingIntent,
    draft: ClarificationDraft,
    *,
    run_id: str,
) -> Optional[ClarificationRequest]:
    """Persist and return the question to ask, or None when we must not ask.

    Returns None — meaning "answer with an assumption instead" — when the same
    question has already been asked in this intent, when the round budget is
    spent, or when another request won the race to ask. Every one of those is a
    normal outcome, not an error: the alternative is a loop, and a loop is the
    single worst thing a clarifying assistant can do.
    """
    from ...config import settings

    print_slot = draft.slot
    mark = fingerprint(print_slot, draft.question)
    if intent.already_asked(mark) or print_slot in intent.asked_slots():
        log.info(
            "not asking about %s again for intent %s", print_slot, intent.intent_id
        )
        return None
    if intent.rounds_used >= settings.salesforce_max_clarification_rounds:
        log.info(
            "clarification budget spent for intent %s (%d rounds)",
            intent.intent_id,
            intent.rounds_used,
        )
        return None

    options = _normalize_options(draft)
    request = ClarificationRequest(
        clarification_id=new_id("clr"),
        conversation_id=intent.conversation_id,
        run_id=run_id,
        root_user_message_id=intent.root_user_message_id,
        intent_id=intent.intent_id,
        header=draft.header or "Salesforce",
        question=draft.question,
        slot=print_slot,
        options=options,
        allow_custom=bool(draft.allow_custom and settings.salesforce_allow_custom_clarification),
        custom_placeholder=draft.custom_placeholder or _placeholder_for(print_slot),
        multi_select=_allows_multiple(print_slot, draft),
        round_number=intent.rounds_used + 1,
        resume_token=secrets.token_urlsafe(24),
        question_fingerprint=mark,
    )

    created = await _run(
        db.create_sf_clarification,
        clarification_id=request.clarification_id,
        conversation_id=request.conversation_id,
        intent_id=request.intent_id,
        resume_token=request.resume_token,
        question_fingerprint=request.question_fingerprint,
        payload=request.wire(),
    )
    if not created:
        # Another send is already asking this conversation something. Two cards
        # for one request is worse than answering this one with an assumption.
        log.info(
            "a clarification is already pending for %s; not asking a second",
            request.conversation_id,
        )
        return None

    intent.status = "awaiting_clarification"
    intent.missing_slots = [print_slot]
    intent.clarification_history.append(
        ClarificationRound(
            clarification_id=request.clarification_id,
            slot=print_slot,
            question=request.question,
            question_fingerprint=request.question_fingerprint,
            round_number=request.round_number,
        )
    )
    await save_intent(intent)
    return request


#: Slots where picking two answers is INCOHERENT rather than merely unusual.
#: A result can only be presented one way, so "a chart AND a table AND a count"
#: is not an answer to "how should I show this?" — everything else genuinely
#: can take several: two objects, two regions, two statuses, two records to
#: compare, or two periods to compare across.
EXCLUSIVE_SLOTS = frozenset({"result_format"})


def _allows_multiple(slot: str, draft: ClarificationDraft) -> bool:
    """Whether this card lets the user tick more than one box.

    Multi-select is the DEFAULT (owner request, 2026-08-11). The single-answer
    card was actively wrong for the questions this org gets: asked which object
    holds payment and invoice data, "Invoice__c" and "Payment__c" is the honest
    answer, and a radio group forced a choice between two things the user needed
    together.

    But the default is not a mandate. Some questions are alternative readings of
    ONE number — "do you mean the interviews or the candidates who sit them?" —
    and there ticking both is not a richer answer, it is a contradiction. The
    planner is told to set multi_select=false for those and is now believed:
    before this, the flag overrode its judgement globally and every such card
    shipped as a checkbox group whose second tick made the question unanswerable.

    Precedence: an exclusive SLOT always wins (the planner cannot force multi on
    one), then the planner's explicit choice, then the deployment default.
    """
    from ...config import settings

    if slot in EXCLUSIVE_SLOTS:
        return False
    if draft.multi_select is not None:
        return bool(draft.multi_select)
    return bool(settings.salesforce_multi_select_clarification)


_PLACEHOLDERS = {
    "date_range": "Enter another date range…",
    "object": "Name the object or area…",
    "record_identity": "Name the record…",
    "metric": "Describe the measure…",
    "owner_scope": "Whose records?",
    "region": "Which region?",
    "status": "Which status?",
    "grouping": "Group by…",
    "result_format": "How should I present it?",
    "comparison_baseline": "Compared against…",
    "filter": "Add the filter…",
}


def _placeholder_for(slot: str) -> str:
    return _PLACEHOLDERS.get(slot, "Tell me what you meant…")


async def get_pending(conversation_id: str) -> Optional[ClarificationRequest]:
    """The open question for this conversation, restored from the database.

    This is what makes a card survive a reload: nothing about it lives in the
    generation buffer, so the browser can be closed and reopened and the same
    question is still there, still resumable.
    """
    row = await _run(db.pending_sf_clarification, conversation_id)
    if not row:
        return None
    try:
        return ClarificationRequest.model_validate(row["payload"])
    except Exception as exc:  # noqa: BLE001
        log.warning("discarding unreadable clarification for %s: %s", conversation_id, exc)
        return None


async def get_clarification(clarification_id: str) -> Optional[ClarificationRequest]:
    """One clarification by id, whatever state it is in.

    Distinct from `get_pending`, which only returns an OPEN question. This is
    what a duplicate response needs: the second click refers to a clarification
    that is already answered, and the request it belongs to must still be
    resumable — otherwise the repeat becomes a brand-new question.
    """
    row = await _run(db.get_sf_clarification, clarification_id)
    if not row:
        return None
    try:
        return ClarificationRequest.model_validate(row["payload"])
    except Exception as exc:  # noqa: BLE001
        log.warning("discarding unreadable clarification %s: %s", clarification_id, exc)
        return None


def answer_text(request: ClarificationRequest, response: ClarificationResponse) -> str:
    """The normalized value the answer resolves the slot to.

    Options and custom text are COMBINED, not one-or-the-other. On a
    multi-select card someone can tick "Invoice__c" and "Payment__c" and also
    type "…and anything linked to a renewal"; dropping the typed half because
    boxes were ticked would silently discard the more specific instruction.

    A skip resolves to nothing, and the caller states its assumption instead.
    """
    if response.skipped:
        return ""
    by_id = {option.id: option for option in request.options}
    chosen = [by_id[i].value for i in response.selected_option_ids if i in by_id]
    typed = response.custom_text.strip()
    parts = [*chosen, typed] if typed else chosen
    return ", ".join(p for p in parts if p)


async def apply_response(
    response: ClarificationResponse,
) -> Tuple[ClarificationRequest, PendingIntent, str, bool]:
    """Resolve a clarification and merge the answer into its intent.

    → (request, intent, resolved_value, was_already_resolved)

    Raises ClarificationRejected for an unknown id, a mismatched resume token, a
    conversation that does not own it, or an option id that was never offered —
    every one of which means the client is describing a question we did not ask.
    """
    row = await _run(db.get_sf_clarification, response.clarification_id)
    if not row:
        raise ClarificationRejected("no such clarification")

    try:
        request = ClarificationRequest.model_validate(row["payload"])
    except Exception as exc:  # noqa: BLE001
        raise ClarificationRejected("this clarification can no longer be read") from exc

    # Both checks are unconditional. Guarding them behind "if the client sent
    # one" made them opt-in: a response with the fields simply omitted skipped
    # straight past both, which is the one shape an attacker would choose. The
    # server minted the token and the conversation id; a client resuming a
    # question it was actually shown always has them.
    if response.conversation_id != request.conversation_id:
        raise ClarificationRejected("this clarification belongs to another conversation")
    if response.resume_token != row["resume_token"]:
        raise ClarificationRejected("stale clarification — it has been replaced")
    if row["state"] == "cancelled":
        raise ClarificationRejected("this question was cancelled")

    offered = {option.id for option in request.options}
    unknown = [i for i in response.selected_option_ids if i not in offered]
    if unknown:
        raise ClarificationRejected(f"option {unknown[0]!r} was never offered")
    if not request.multi_select and len(response.selected_option_ids) > 1:
        raise ClarificationRejected("this question takes a single answer")

    state = "skipped" if response.skipped else "answered"
    stored, already = await _run(
        db.resolve_sf_clarification,
        response.clarification_id,
        state=state,
        response=response.model_dump(mode="json"),
        client_message_id=response.client_message_id or None,
    )
    if stored is None:
        raise ClarificationRejected("no such clarification")

    # A duplicate submission must return the FIRST answer, not this one — the
    # user's second click did not change what they said.
    effective = response
    if already and stored.get("response"):
        try:
            effective = ClarificationResponse.model_validate(stored["response"])
        except Exception:  # noqa: BLE001 — fall back to what we were sent
            effective = response

    intent = await load_intent(request.intent_id)
    if intent is None:
        raise ClarificationRejected("the request this question belonged to is gone")

    value = answer_text(request, effective)
    for round_ in intent.clarification_history:
        if round_.clarification_id == request.clarification_id:
            round_.answered = not effective.skipped
            round_.skipped = effective.skipped
            round_.answer = value
    if value:
        intent.resolved_slots[request.slot] = value
    intent.missing_slots = [s for s in intent.missing_slots if s != request.slot]
    intent.status = "open"
    if not already:
        await save_intent(intent)
    return request, intent, value, already


async def cancel_pending(conversation_id: str) -> int:
    """Cancel this conversation's open question — the source was switched off,
    the topic clearly changed, or the conversation is going away."""
    cancelled = await _run(db.cancel_sf_clarifications, conversation_id)
    if cancelled:
        await _run(db.close_sf_intents, conversation_id, "cancelled")
    return cancelled


async def complete_intent(
    intent: PendingIntent,
    state: ConversationSalesforceState,
    *,
    objects: Sequence[str] = (),
    entities: Sequence[str] = (),
    query_summary: str = "",
    result_metadata: Optional[dict] = None,
) -> None:
    """Mark an intent done and roll what it established into the conversation.

    This is the write that makes the NEXT turn cheap: the follow-up reads these
    fields instead of re-deriving the object, period and owner scope from a
    transcript that may already have been compacted away.
    """
    intent.status = "completed"
    await save_intent(intent)

    slots = intent.resolved_slots
    if objects:
        state.last_salesforce_objects = list(dict.fromkeys(objects))[:5]
    elif slots.get("object"):
        state.last_salesforce_objects = [slots["object"]]
    if entities:
        state.last_entities = list(dict.fromkeys(entities))[:10]
    state.last_metric = slots.get("metric", state.last_metric)
    state.last_date_range = slots.get("date_range", state.last_date_range)
    state.last_owner_scope = slots.get("owner_scope", state.last_owner_scope)
    state.last_grouping = slots.get("grouping", state.last_grouping)
    carried = {
        key: value
        for key, value in slots.items()
        if key in ("region", "status", "filter", "result_format", "comparison_baseline")
        and value
    }
    if carried:
        state.last_filters = {**state.last_filters, **carried}
    state.last_completed_intent = intent.intent_id
    state.active_intent_id = None
    state.pending_clarification_id = None
    if query_summary:
        state.last_query_summary = query_summary[:400]
    if result_metadata is not None:
        state.last_result_metadata = result_metadata
    await save_state(state)


def decision_slots(decision: AgentDecision, intent: PendingIntent) -> None:
    """Fold a planner decision's resolved slots into the intent, in place.

    The intent wins on conflict: a slot the USER answered in a previous round is
    a fact, and a planner that re-derives it differently on the next turn must
    not silently overwrite what they said.
    """
    answered = {r.slot for r in intent.clarification_history if r.answered}
    for slot, value in decision.resolved_slots.items():
        if slot in answered:
            continue
        intent.resolved_slots[slot] = value
    if decision.normalized_intent:
        intent.normalized_intent = decision.normalized_intent
    intent.missing_slots = list(decision.missing_critical_slots)
