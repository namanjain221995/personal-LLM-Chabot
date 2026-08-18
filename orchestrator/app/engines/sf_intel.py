"""Salesforce Intelligence Mode — the engine.

One pass over a request, in the order the directive lays out:

    normalize → resolve context → decide → resolve schema/entities →
    plan → validate → execute → paginate → calculate → verify → answer →
    update session state

Two things make this different from `engines/sql.py`, which it delegates to for
the warehouse path:

  1. It can STOP and ask. A clarification is persisted, streamed as metadata,
     and resumed later against the SAME intent — the original request is never
     retyped and never restarted.
  2. When it queries the org itself, the query is COMPILED from a validated
     structure (core/sf_intel/plan.py). The model chooses an object, some
     fields, an operator and a value; it never writes query text.

What this engine will not do: invent a record, present a tool failure as an
empty result, quote a number it did not compute, or answer a Salesforce question
from model memory.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from .. import llm
from ..config import settings
from ..core import org_brief
from ..core.sf_intel import budget as ctx_budget
from ..core.sf_intel import (
    interpret,
    phases,
    planner,
    resume,
    state as sf_state,
    tools,
)
from ..core.sf_intel.models import (
    AgentDecision,
    ClarificationRequest,
    ClarificationResponse,
    ConversationSalesforceState,
    PendingIntent,
    SalesforceQueryPlan,
    utcnow_iso,
)
from ..core.sf_intel.plan import PlanRejected
from ..core.sf_intel.prompts import ANSWER_SYSTEM, answer_user_message

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]

#: Rows shown to the answer model. The COUNTS come from calculate_result over
#: everything retrieved; this is an illustration, and the prompt says so.
ANSWER_SAMPLE_ROWS = 30

#: Rows put on `meta.data` for the table the user sees.
PREVIEW_ROWS = 200


@dataclass
class Outcome:
    """What the engine did, for main.py to act on."""

    #: True when this engine produced the whole answer (tokens + meta emitted).
    handled: bool = False
    answer: str = ""
    #: The request with clarification answers folded in. main.py uses this for
    #: the fallback chain so a resumed request reaches the engines RESOLVED.
    resolved_text: str = ""
    #: Set when a question is now waiting on the user.
    clarification: Optional[ClarificationRequest] = None
    #: Rides onto the final meta whoever emits it.
    meta_extras: Dict[str, Any] = field(default_factory=dict)


class _Phases:
    """Emits `status` events and remembers the last one for the final meta."""

    def __init__(self, emit: Emit, run_id: str) -> None:
        self._emit = emit
        self.run_id = run_id
        self.started_at = utcnow_iso()
        self.last: Dict[str, Any] = {}

    async def __call__(
        self,
        phase: str,
        *,
        record_count: Optional[int] = None,
        tool_name: str = "",
    ) -> None:
        payload = phases.status_payload(
            phase,
            run_id=self.run_id,
            started_at=self.started_at,
            record_count=record_count,
            tool_name=tool_name,
        )
        self.last = payload
        await self._emit("status", payload)


# ---------------------------------------------------------------------------
# Is this a request for data at all?
# ---------------------------------------------------------------------------

#: Greetings, thanks, and questions about the assistant itself. Kept
#: deliberately NARROW and anchored to the whole message: the cost of wrongly
#: skipping is that a real question loses its clarification, so anything with a
#: noun in it must fall through to the planner.
_CONVERSATIONAL_RE = re.compile(
    r"^(?:"
    r"(hi|hey|hello|yo|hiya|good (morning|afternoon|evening)|greetings)"
    r"|(thanks?|thank you|ta|cheers|nice|great|cool|ok|okay|got it|perfect|"
    r"sure|yes|no|yep|nope)"
    r"|(bye|goodbye|see you|later)"
    r"|(who are you|what are you|what can you do|what do you do|help|"
    r"how do (i|you) (use|work)|what('| i)s this)"
    r")\W*(there|everyone|all|again|mate|team)?\W*$",
    re.I,
)

#: A message this long is doing more than saying hello, whatever it opens with.
_CONVERSATIONAL_MAX_CHARS = 60


def is_conversational(text: str) -> bool:
    """True for a turn that is plainly not a request for Salesforce data."""
    stripped = (text or "").strip()
    if not stripped or len(stripped) > _CONVERSATIONAL_MAX_CHARS:
        return False
    return bool(_CONVERSATIONAL_RE.match(stripped))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run(
    text: str,
    history: Sequence[dict],
    emit: Emit,
    *,
    conversation_id: str,
    effort: str = "medium",
    model_choice: str = "smart",
    clarification_response: Optional[ClarificationResponse] = None,
    source_enabled: bool = True,
    use_planner: bool = True,
) -> Outcome:
    """Plan and, where possible, answer a Salesforce request.

    Returns `handled=False` when the request is not this engine's to answer
    (general chat, an unusable planner, the feature switched off) — main.py then
    runs its normal chain with `resolved_text`, so nothing is lost either way.

    `use_planner=False` runs the same pipeline with the deterministic detectors
    in `core/clarify.py` in place of the model call. That is what Salesforce
    Intelligence Mode's kill switch selects, and it is why there is exactly ONE
    clarification implementation now: with the planner off, a question is still
    a persisted `ClarificationRequest` with a resume token, a round budget, a
    repetition fingerprint and an idempotent answer. The previous fallback was a
    second, parallel implementation that rendered a different card, could not
    resume, could not survive a reload, and re-asked its own question forever
    when `CLARIFY_MODE=always` was set.
    """
    run_id = uuid.uuid4().hex[:12]
    phase = _Phases(emit, run_id)
    state = await sf_state.load_state(conversation_id)
    state.source_enabled = source_enabled
    # `meta` is trust metadata: it must say which planner actually decided this
    # turn. Reporting "intelligence" while the kill switch had the model off
    # would make the one flag that changes the answer invisible in the record
    # of how the answer was produced.
    mode_label = "intelligence" if use_planner else "deterministic"

    # ---- 0. is this even a request for data? --------------------------------
    # Checked BEFORE the first status event, and deliberately without a model
    # call. "Checking Salesforce fields" shown under a greeting is a fabricated
    # step — the labels are only honest if they describe work that is actually
    # happening — and running a planner over "thanks!" spends a second of the
    # user's time to conclude what a regex already knew.
    pending = await sf_state.get_pending(conversation_id)
    if (
        pending is None
        and clarification_response is None
        and is_conversational(text)
    ):
        return Outcome(handled=False, resolved_text=text)

    await phase("understanding")

    # ---- 1. an answer to a question we are already waiting on ----------------
    intent: Optional[PendingIntent] = None
    assumptions: List[str] = []
    duplicate = False

    # A response is handled even when nothing is pending any more. That is the
    # DUPLICATE case — a double-click, a retried fetch, a reconnect — and it is
    # the whole reason `apply_response` is idempotent: it returns the FIRST
    # answer and the intent it belongs to, so the original request resumes.
    # Skipping it here would turn the second click into a brand-new question
    # ("This quarter" asked on its own), which is exactly the loop this feature
    # exists to prevent. Found on a live run, 2026-08-11.
    if pending is None and clarification_response is not None:
        pending = await sf_state.get_clarification(
            clarification_response.clarification_id
        )

    if pending is not None:
        resumed = await _resume(
            pending,
            text,
            clarification_response=clarification_response,
            history=history,
            phase=phase,
            conversation_id=conversation_id,
        )
        if resumed.get("rejected"):
            await emit("token", {"text": resumed["message"]})
            await emit("meta", {"route": "clarify", "salesforce_mode": mode_label})
            return Outcome(handled=True, answer=resumed["message"])
        intent = resumed.get("intent")
        # A duplicate submission is not a new turn. Re-planning it can decide a
        # SECOND detail is now missing and ask again — so a double-click turns
        # into an interrogation. Seen on a live run (2026-08-11): the repeat of
        # "Tasks" was answered with "what status counts as pending?".
        duplicate = bool(resumed.get("already"))
        if resumed.get("new_topic"):
            intent = None  # the pending question was cancelled; start fresh

    # ---- 2. a fresh root intent, seeded from what this chat established -------
    if intent is None:
        await phase("resolving_context")
        intent = sf_state.new_intent(
            conversation_id,
            text=text,
            root_user_message_id=uuid.uuid4().hex[:12],
            carried_slots=state.carried_slots(),
        )
        await sf_state.save_intent(intent)
    state.active_intent_id = intent.intent_id

    resolved_text = intent.resolved_text()

    if not (
        settings.salesforce_contextual_clarification_enabled
        and settings.clarify_before_answering
    ):
        # Asking is switched off — by either flag. Resolving the request against
        # the conversation is NOT switched off, so the resolved text still goes
        # on: "what about tomorrow?" keeps working when questions are disabled,
        # which is the point of having two settings rather than one.
        await sf_state.save_state(state)
        return Outcome(handled=False, resolved_text=resolved_text)

    # ---- 3. read the request, then schema + entity resolution ----------------
    # WHAT THE PLANNER PLANS FOR is the resolved request, not the newest
    # message. On a resume the newest message is "Count of interviews" — an
    # answer, not a request — and planning for it on its own produced a decision
    # about a fragment: the original question survived only in whatever recent
    # turns happened to be included. `resolved_text` is the original WITH the
    # answers folded in, which is what every engine downstream already receives.
    planning_text = resolved_text if intent.clarification_history else text
    reading = interpret.read(planning_text)

    # The knowledge layers match on words. A three-word follow-up has none of
    # its subject's words in it, so grounding it on this turn alone hands the
    # planner an empty brief for a question the conversation has already
    # established the subject of.
    grounding = interpret.grounding_text(
        reading,
        original_request=intent.original_user_text,
        carried_slots=intent.resolved_slots,
        conversation_summary=state.last_query_summary,
        recent_turns=_recent(history, 6),
    )

    # ---- 4. the decision -----------------------------------------------------
    if use_planner:
        await phase("checking_schema")
        hinted = _objects_hinted_by(reading.text, state)
        schema_summary, _available = await tools.get_salesforce_schema(
            grounding, objects=hinted
        )
        candidates = await _entity_candidates(reading.text, hinted)
        decision = await planner.plan(
            user_text=planning_text,
            intent=intent,
            state=state,
            schema_summary=schema_summary,
            entity_candidates=candidates,
            recent_turns=_recent(history, 6),
            effort=effort,
            today=org_brief.business_today(),
            timezone_name=org_brief.BUSINESS_TIMEZONE,
            reading=reading,
            grounding_text=grounding,
        )
    else:
        # Schema and entity lookups exist to inform the model; with no model
        # call there is nothing to inform, and both are network round trips.
        candidates = []
        decision = planner.deterministic_decision(reading.text)
    decision = planner.enforce_policy(
        decision,
        intent,
        duplicate=duplicate,
        reading=reading,
        entity_candidates=candidates,
    )
    sf_state.decision_slots(decision, intent)
    assumptions.extend(decision.assumptions)
    await sf_state.save_intent(intent)
    log.info(
        "salesforce planner: run=%s action=%s reason=%s slots=%s",
        run_id,
        decision.action,
        decision.internal_reason_code,
        sorted(intent.resolved_slots),
    )

    if decision.action in ("DENY", "UNSUPPORTED"):
        message = _refusal(decision)
        await phase("failed")
        await emit("token", {"text": message})
        await emit("meta", {"route": "chat", "salesforce_mode": mode_label})
        await sf_state.complete_intent(intent, state, query_summary=text[:200])
        return Outcome(handled=True, answer=message)

    if decision.action == "ANSWER_GENERAL":
        # Not a Salesforce question after all. Hand it back rather than
        # answering it here — engines/chat.py already does this well.
        await sf_state.save_state(state)
        return Outcome(handled=False, resolved_text=resolved_text)

    # ---- 5. ask, if a question is genuinely worth asking ---------------------
    if decision.action == "ASK_CLARIFICATION" and decision.clarification_draft:
        await phase("clarifying")
        request = await sf_state.open_clarification(
            intent, decision.clarification_draft, run_id=run_id
        )
        if request is not None:
            state.pending_clarification_id = request.clarification_id
            await sf_state.save_state(state)
            message = _clarification_text(request)
            await emit("token", {"text": message})
            await emit(
                "meta",
                {
                    "route": "clarify",
                    "salesforce_mode": mode_label,
                    "clarification": request.wire(),
                    "status": phase.last,
                },
            )
            return Outcome(
                handled=True, answer=message, clarification=request,
                resolved_text=resolved_text,
            )
        # open_clarification refused (budget spent, already asked, or another
        # request won the race). Answering with a stated assumption is the
        # designed outcome, never a loop.
        assumptions.append(
            planner._assumption_for(  # noqa: SLF001 — one policy, one home
                decision.clarification_draft.slot,
                decision.clarification_draft.options,
            )
        )

    resolved_text = intent.resolved_text()

    # ---- 6. execute ----------------------------------------------------------
    plan_model = decision.structured_query_plan
    if plan_model is not None and _live_available():
        outcome = await _execute_live(
            plan_model,
            intent=intent,
            state=state,
            emit=emit,
            phase=phase,
            question=resolved_text,
            assumptions=assumptions,
            effort=effort,
        )
        if outcome is not None:
            return outcome

    # No usable plan, or no live connection: the warehouse engine answers, with
    # the RESOLVED request. Its own live fallback still applies.
    #
    # The intent is COMPLETED here, not merely saved. Handing the request on is
    # still finishing it as far as this engine is concerned, and `complete_intent`
    # is what rolls the object, metric, period and owner scope into the
    # conversation state. Without it, every deployment answering from the
    # warehouse — which is the common one — left `carried_slots()` empty
    # forever, so "what about tomorrow?" inherited nothing and arrived at the
    # planner as a bare question about a date. The follow-up behaviour this
    # engine exists for was live only when a live SOQL query happened to run.
    await sf_state.complete_intent(
        intent,
        state,
        # The plan names the object even when there was no live connection to
        # run it against, and that is the single most useful thing a follow-up
        # inherits. `complete_intent` falls back to the resolved `object` slot.
        objects=[plan_model.object_api_name] if plan_model is not None else (),
        query_summary=intent.original_user_text[:200],
    )
    return Outcome(
        handled=False,
        resolved_text=resolved_text,
        meta_extras=_meta_extras(intent, assumptions, phase),
    )


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

async def _resume(
    pending: ClarificationRequest,
    text: str,
    *,
    clarification_response: Optional[ClarificationResponse],
    history: Sequence[dict],
    phase: _Phases,
    conversation_id: str,
) -> Dict[str, Any]:
    """Apply an answer to the pending question, or cancel it for a new topic."""
    await phase("resolving_context")

    if clarification_response is not None:
        try:
            _request, intent, _value, already = await sf_state.apply_response(
                clarification_response
            )
        except sf_state.ClarificationRejected as exc:
            return {
                "rejected": True,
                "message": (
                    f"I could not apply that answer — {exc}. Ask again and I "
                    "will start from your question."
                ),
            }
        if already:
            log.info(
                "duplicate clarification response for %s — resuming with the "
                "first answer",
                clarification_response.clarification_id,
            )
        return {"intent": intent, "already": already}

    # Typed into the composer instead of clicking. Is it the answer?
    intent = await sf_state.load_intent(pending.intent_id)
    verdict = await resume.classify(
        pending,
        text,
        original_request=intent.original_user_text if intent else "",
        recent_summary=_summarize(history),
    )
    if verdict.verdict == "new_topic":
        await sf_state.cancel_pending(conversation_id)
        return {"new_topic": True}

    typed = ClarificationResponse(
        clarification_id=pending.clarification_id,
        conversation_id=conversation_id,
        client_message_id=f"typed-{uuid.uuid4().hex[:10]}",
        selected_option_ids=[verdict.option_id] if verdict.option_id else [],
        custom_text="" if verdict.option_id else verdict.value,
        skipped=verdict.skipped,
        resume_token=pending.resume_token,
    )
    try:
        _request, intent, _value, _already = await sf_state.apply_response(typed)
    except sf_state.ClarificationRejected as exc:
        log.info("typed clarification answer rejected: %s", exc)
        await sf_state.cancel_pending(conversation_id)
        return {"new_topic": True}
    return {"intent": intent}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def _live_available() -> bool:
    from ..core import salesforce

    return bool(settings.sf_live_enabled and salesforce.configured())


async def _execute_live(
    plan_model: SalesforceQueryPlan,
    *,
    intent: PendingIntent,
    state: ConversationSalesforceState,
    emit: Emit,
    phase: _Phases,
    question: str,
    assumptions: List[str],
    effort: str,
) -> Optional[Outcome]:
    """Compile, run and answer from the org. None → fall back to the warehouse."""
    await phase("querying_salesforce", tool_name="execute_salesforce_query_plan")

    async def on_page(count: int) -> None:
        await phase("retrieving_more_results", record_count=count)

    try:
        result = await tools.execute_salesforce_query_plan(plan_model, on_page=on_page)
    except PlanRejected as exc:
        # The plan named something that does not exist or is not permitted.
        # Falling back to the warehouse engine is honest; running a "repaired"
        # query nobody validated is not.
        log.info("query plan rejected: %s", exc)
        return None
    except tools.SalesforceToolError as exc:
        await phase("failed")
        message = (
            f"The Salesforce lookup failed ({exc}). I would rather tell you that "
            "than show you a number I did not read. Nothing was returned — this "
            "is not the same as finding no matching records."
        )
        await emit("token", {"text": message})
        await emit(
            "meta",
            {
                "route": "sql",
                "salesforce_mode": "intelligence",
                "salesforce_error": str(exc)[:300],
                "status": phase.last,
            },
        )
        return Outcome(handled=True, answer=message)

    await phase("analyzing_records", record_count=len(result.rows))
    await phase("calculating")
    group_by = plan_model.group_by[0] if plan_model.group_by else ""
    computed = tools.calculate_result(result, group_by=group_by)
    # The SOQL itself is withheld from the answer prompt, so WITHOUT this the
    # model cannot know the figures were already filtered — asked for locked
    # deliverables it answered "866 total records, but the data does not break
    # down by locked status" about a count that WAS the locked breakdown.
    population = _population_line(plan_model)
    if population:
        computed["population"] = population

    await phase("verifying")
    verification = _verify(result, computed)
    if verification:
        computed["verification_notes"] = verification

    await phase("drafting_answer")
    scope = _scope_line(intent)
    sample = result.rows[:ANSWER_SAMPLE_ROWS]
    messages = [
        {"role": "system", "content": ANSWER_SYSTEM + "\n\n" + org_brief.ANSWER_RULES},
        {
            "role": "user",
            "content": answer_user_message(
                question=question,
                scope=scope,
                source_label=f"Live Salesforce ({result.object_api_name})",
                freshness=f"queried at {result.queried_at} (live, not the synced copy)",
                sample_rows=sample,
                computed=computed,
                assumptions=assumptions,
                query_note=(
                    "The query has already run. Do not show it unless the user "
                    "asked to see it."
                ),
            ),
        },
    ]

    parts: List[str] = []
    max_tokens = (
        settings.model_high_max_output if effort == "high" else settings.model_max_output
    )
    async for kind, delta in llm.stream_chat_events(
        messages,
        model_choice="smart",
        effort=effort,
        temperature=0.1,
        max_tokens=min(max_tokens, 6000),
    ):
        # `reasoning` deltas are NOT forwarded: raw chain-of-thought must not
        # reach the UI, storage, or a log line for this route.
        if kind == "token":
            parts.append(delta)
            await emit("token", {"text": delta})
    answer = "".join(parts).strip()
    if not answer:
        answer = _fallback_answer(result, computed)
        await emit("token", {"text": answer})

    preview = result.rows[:PREVIEW_ROWS]
    meta: Dict[str, Any] = {
        "route": "sql",
        "salesforce_mode": "intelligence",
        "data": preview,
        "truncated": len(result.rows) > len(preview) or result.truncated,
        "salesforce_sources": {
            "source": "live",
            "objects": [result.object_api_name],
            "record_count": computed.get("record_count"),
            "query_timestamp": result.queried_at,
            "freshness": "live",
            "pages": result.pages,
            "truncated": result.truncated,
        },
        "status": {**phase.last, "phase": "completed"},
    }
    if assumptions:
        meta["assumptions"] = assumptions
    # The user asked to see the query, or policy allows it. Otherwise it stays
    # out of meta entirely — `meta.sql` is rendered in the proof drawer.
    if _wants_query(question):
        meta["sql"] = result.soql

    if preview and isinstance(preview[0], dict):
        from .sql import attach_chart

        columns = [
            c for c in preview[0] if not isinstance(preview[0][c], (dict, list))
        ]
        await attach_chart(
            meta,
            question,
            columns,
            [[row.get(c) for c in columns] for row in preview],
        )

    await emit("meta", meta)
    await phase("completed", record_count=computed.get("record_count"))

    await sf_state.complete_intent(
        intent,
        state,
        objects=[result.object_api_name],
        query_summary=intent.original_user_text[:200],
        result_metadata={
            "record_count": computed.get("record_count"),
            "queried_at": result.queried_at,
            "source": "live",
        },
    )
    return Outcome(handled=True, answer=answer)


def _verify(result: "tools.QueryResult", computed: Dict[str, Any]) -> List[str]:
    """Cheap cross-checks over what was actually returned.

    Not a second model pass — a second opinion from the same model on the same
    rows is not verification. These are the arithmetic identities that catch the
    failures this pipeline can actually have.
    """
    notes: List[str] = []
    total = computed.get("record_count")
    examined = computed.get("rows_examined", 0)
    if isinstance(total, int) and examined and total > examined:
        notes.append(
            f"{examined} of {total} matching records were retrieved; per-record "
            "breakdowns cover only those."
        )
    groups = computed.get("groups")
    if isinstance(groups, list) and groups:
        share = sum(g.get("share_percent", 0) for g in groups)
        if abs(share - 100.0) > 0.5:
            notes.append(
                "group shares do not total 100% — treat the breakdown as indicative."
            )
        counted = sum(g.get("count", 0) for g in groups)
        if counted != computed.get("group_denominator"):
            notes.append("group counts and their denominator disagree.")
    if result.truncated:
        notes.append(
            "the result was capped, so totals cover the records retrieved."
        )
    return notes


def _fallback_answer(result: "tools.QueryResult", computed: Dict[str, Any]) -> str:
    """An honest sentence when the model streamed nothing.

    Never leave a table with no words above it: the rows are real, and saying so
    plainly beats an empty bubble.
    """
    count = computed.get("record_count", len(result.rows))
    if not result.rows:
        return (
            f"No matching {result.object_api_name} records were found. The query "
            "ran successfully — this is an empty result, not a failure."
        )
    return (
        f"{count} matching {result.object_api_name} record(s), queried live at "
        f"{result.queried_at}. The records are in the table below."
    )


_SHOW_QUERY_RE = re.compile(
    r"\b(show|give|what'?s|display|see)\b.{0,24}\b(soql|query|sql)\b", re.I
)


def _wants_query(question: str) -> bool:
    return bool(_SHOW_QUERY_RE.search(question or ""))


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

_OBJECT_HINT_RE = re.compile(
    r"\b(opportunit(?:y|ies)|account?s?|contacts?|leads?|cases?|tasks?|events?|"
    r"campaigns?|quotes?|orders?|contracts?|[A-Z][A-Za-z0-9_]*__c)\b"
)

_HINT_TO_OBJECT = {
    "opportunity": "Opportunity",
    "opportunities": "Opportunity",
    "account": "Account",
    "accounts": "Account",
    "contact": "Contact",
    "contacts": "Contact",
    "lead": "Lead",
    "leads": "Lead",
    "case": "Case",
    "cases": "Case",
    "task": "Task",
    "tasks": "Task",
    "pipeline": "Opportunity",
}


def _objects_hinted_by(
    text: str, state: ConversationSalesforceState
) -> List[str]:
    """Objects worth describing for this request.

    The conversation's previous objects lead: a follow-up is far more likely to
    be about what was just discussed than about whatever noun it happens to
    contain.
    """
    out: List[str] = list(state.last_salesforce_objects[:2])
    lowered = (text or "").lower()
    if "pipeline" in lowered and "Opportunity" not in out:
        out.append("Opportunity")
    for match in _OBJECT_HINT_RE.finditer(text or ""):
        token = match.group(0)
        mapped = _HINT_TO_OBJECT.get(token.lower(), token if token.endswith("__c") else "")
        if mapped and mapped not in out:
            out.append(mapped)
    return out[:4]


#: A bare proper noun with no object named is the "which Acme?" shape.
_QUOTED_OR_CAPS_RE = re.compile(r"\"([^\"]{2,60})\"|\b([A-Z][A-Za-z0-9&.\-]{2,})\b")
_NOT_A_NAME = frozenset(
    {"Salesforce", "I", "Show", "What", "How", "Which", "Who", "List", "Give",
     "Find", "The", "My", "This", "Last", "Next", "All", "EMEA", "APAC", "AMER"}
)


async def _entity_candidates(text: str, objects: Sequence[str]) -> List[dict]:
    """Search for real records when the request names one but not which one."""
    if not _live_available():
        return []
    names: List[str] = []
    for match in _QUOTED_OR_CAPS_RE.finditer(text or ""):
        token = (match.group(1) or match.group(2) or "").strip()
        if token and token not in _NOT_A_NAME and token not in names:
            names.append(token)
    if not names:
        return []
    searchable = [o for o in objects if o in tools._DISPLAY_FIELDS] or ["Account"]
    found = await tools.search_salesforce_entities(names[0], objects=searchable)
    # Only worth surfacing when there is a genuine choice to make.
    return found if len(found) > 1 else []


def _recent(history: Sequence[dict], n: int) -> List[dict]:
    turns = [h for h in history if h.get("role") in ("user", "assistant")]
    return turns[-n:]


def _summarize(history: Sequence[dict]) -> str:
    return "\n".join(
        f"{t.get('role')}: {str(t.get('content') or '')[:200]}"
        for t in _recent(history, 4)
    )


def _population_line(plan) -> str:
    """The filters the query already applied, in plain words.

    This is what lets the answer say "866 deliverables WITH STATUS 'Locked'"
    instead of doubting whether the count was filtered at all.
    """
    parts = []
    for f in getattr(plan, "filters", None) or []:
        value = f.value if f.value is not None else ", ".join(f.values or [])
        parts.append(f"{f.field} {f.operator} {value!r}" if value != "" else f.field)
    if not parts:
        return ""
    return "these figures already apply the filters: " + "; ".join(parts)


def _scope_line(intent: PendingIntent) -> str:
    if not intent.resolved_slots:
        return ""
    return "; ".join(
        f"{slot.replace('_', ' ')}: {value}"
        for slot, value in intent.resolved_slots.items()
        if value
    )


def _clarification_text(request: ClarificationRequest) -> str:
    """The plain-text form, so the question works before any UI renders it.

    Also what gets stored in history: a message whose text is "…" and whose
    meaning lives entirely in metadata reads as a bug when the card is gone.
    """
    lines = [request.question, ""]
    for index, option in enumerate(request.options, start=1):
        line = f"**{index}.** {option.label}"
        if option.description:
            line += f" — {option.description}"
        lines.append(line)
    if request.allow_custom:
        lines.append(f"**{len(request.options) + 1}.** Something else — let me type it")
    return "\n".join(lines)


def _refusal(decision: AgentDecision) -> str:
    if decision.action == "DENY":
        return (
            "I can't do that from here. This assistant reads Salesforce; it does "
            "not change records, run administrative actions, or act on another "
            "system's behalf."
        )
    return (
        "I can't answer that from Salesforce. "
        + (decision.normalized_intent or "The request")
        + " needs something this connection does not expose — tell me which "
        "object or report you have in mind and I will try again."
    )


def _meta_extras(
    intent: PendingIntent, assumptions: Sequence[str], phase: _Phases
) -> Dict[str, Any]:
    extras: Dict[str, Any] = {"salesforce_mode": "intelligence"}
    if assumptions:
        extras["assumptions"] = list(assumptions)
    scope = _scope_line(intent)
    if scope:
        extras["salesforce_scope"] = scope
    if phase.last:
        extras["status"] = {**phase.last, "phase": "completed"}
    return extras


# ---------------------------------------------------------------------------
# Starter card
# ---------------------------------------------------------------------------

async def starter_options(conversation_id: str) -> Dict[str, Any]:
    """Context-aware options for the empty-composer starter card.

    Only offers what this connection can actually reach: an object the
    integration user cannot query never appears, because the list is filtered
    against the org's own describe rather than hardcoded.
    """
    state = await sf_state.load_state(conversation_id)
    options: List[Dict[str, str]] = []

    continuation = resume.continuation_label(state)
    if continuation:
        options.append(
            {
                "id": "continue",
                "label": continuation,
                "description": "Pick up the previous Salesforce task.",
                "prompt": f"Continue that analysis: {state.last_query_summary}",
            }
        )

    available: List[str] = []
    if _live_available():
        try:
            org_id = await _org_key()
            available = [o["name"] for o in await tools.list_objects(org_id)]
        except Exception as exc:  # noqa: BLE001 — a starter card is a nicety
            log.info("starter card could not list objects: %s", str(exc)[:160])

    catalogue = [
        ("find_record", ["Account", "Contact", "Lead"], "Find a record",
         "Look up a specific account, contact or lead.",
         "Find a record in Salesforce"),
        ("pipeline", ["Opportunity"], "Analyze opportunities or pipeline",
         "Open pipeline, stages, close dates and amounts.",
         "Show my open pipeline"),
        ("service", ["Case", "Task"], "Review cases, tasks or activity",
         "Open cases, follow-ups and recent activity.",
         "Show my open cases and tasks"),
        ("recent", [], "Summarize recent changes",
         "What has moved since the last time you looked.",
         "Summarize what changed in Salesforce recently"),
    ]
    for option_id, needs, label, description, prompt in catalogue:
        if needs and available and not any(n in available for n in needs):
            continue  # this org does not expose it to this user
        options.append(
            {
                "id": option_id,
                "label": label,
                "description": description,
                "prompt": prompt,
            }
        )

    pending = await sf_state.get_pending(conversation_id)
    return {
        "enabled": settings.salesforce_starter_card_enabled,
        "options": options[:5],
        "pending_clarification": pending.wire() if pending else None,
    }


async def _org_key() -> str:
    from ..core import salesforce

    return await salesforce.org_key()
