"""The compact structure the main model is shown.

Small on purpose. Everything the question does not need is left out, and the
query's plumbing -- SQL, table aliases, physical column names -- is never in
here at all. A model shown `t0.Interview_Outcome__c` will write it back to the
user; a model shown "interview outcome" will not.

Two kinds of truncation live side by side and must never be conflated. The
database cut rows because the query asked for a limit; this layer cuts rows
because the context has a budget. An answer that confuses them tells a user
more records may exist when none do, or the reverse.
"""
from __future__ import annotations

import json
from typing import Any

from .config import AnswerConfig, FreshnessPolicy
from .models import AnswerContext, InterpretedResult, ResultType


def build_context(interpreted: InterpretedResult, config: AnswerConfig,
                  ) -> AnswerContext:
    payload: dict[str, Any] = {
        "question": interpreted.user_question,
        "result_type": interpreted.result_type.value,
        "verified_facts": _verified_facts(interpreted),
    }
    if interpreted.primary_entity:
        payload["business_entity"] = interpreted.primary_entity
    if interpreted.applied_filters:
        # What the query searched for. Supplied so an empty answer can say
        # what found nothing, which is the only useful thing it can say.
        payload["applied_filters"] = interpreted.applied_filters

    rows_sent = 0
    context_truncated = False

    if interpreted.records:
        limit = max(0, config.max_rows_to_model)
        shown = interpreted.records[:limit]
        rows_sent = len(shown)
        context_truncated = len(shown) < len(interpreted.records)
        payload["records"] = shown
    if interpreted.groups:
        # Groups are rows too: 2,744 of them overflowed the answer's token
        # limit. Same cap as records; the aggregates stay complete.
        limit = max(0, config.max_rows_to_model)
        payload["groups"] = interpreted.groups[:limit]
        if len(interpreted.groups) > limit:
            rows_sent = limit
            context_truncated = True
    if interpreted.aggregates:
        payload["aggregates"] = interpreted.aggregates
    if interpreted.derived_facts:
        payload["derived_facts"] = interpreted.derived_facts

    if context_truncated:
        # Said explicitly, because the model must not describe rows it was
        # never shown. Aggregates in verified_facts were computed over the
        # whole result and remain safe to state.
        payload["model_context"] = {
            "rows_shown": rows_sent,
            "rows_in_result": interpreted.returned_row_count,
            "model_context_truncated": True,
        }

    payload["freshness"] = _freshness_block(interpreted, config.freshness)

    text = json.dumps(payload, sort_keys=True, default=str)
    context = AnswerContext(
        payload=payload,
        rows_sent_to_model=rows_sent,
        database_returned_rows=interpreted.returned_row_count,
        model_context_truncated=context_truncated,
        database_result_truncated=interpreted.truncated,
        characters=len(text))

    if context.characters > config.max_context_characters and rows_sent:
        _shrink(context, interpreted, config)
    return context


def _verified_facts(interpreted: InterpretedResult) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "returned_records": interpreted.returned_row_count,
        "truncated": interpreted.truncated,
    }
    # Absent rather than null when unknown: a key holding None invites the
    # model to say "unknown total", and the instruction is already to say
    # nothing it was not given.
    if interpreted.total_count is not None:
        facts["matching_records"] = interpreted.total_count
    if interpreted.result_type is ResultType.COUNT:
        facts["matching_record_count"] = interpreted.aggregates.get(
            "matching_record_count", 0)
        facts.pop("returned_records", None)
    return facts


def _freshness_block(interpreted: InterpretedResult,
                     policy: FreshnessPolicy) -> dict[str, Any]:
    """The real sync state, plus whether the answer should mention it.

    Always supplied even when it must not be shown: the model needs to know
    the data is a synchronised copy so it does not describe it as live, which
    is a separate thing from being asked to quote an age.
    """
    freshness = interpreted.freshness
    return {"status": freshness.status,
            "age_minutes": freshness.age_minutes,
            "last_successful_sync": freshness.last_successful_sync,
            "source": "synchronised copy of Salesforce",
            "include_in_answer": policy.should_show(freshness.status)}


def _shrink(context: AnswerContext, interpreted: InterpretedResult,
            config: AnswerConfig) -> None:
    """Drop display rows until the context fits its character budget.

    Rows go, facts stay. The aggregates were computed over the whole result,
    so a shorter context is still a correct one -- just a less illustrated
    one.
    """
    rows = list(context.payload.get("records") or [])
    while rows and context.characters > config.max_context_characters:
        rows = rows[: max(1, len(rows) // 2)]
        context.payload["records"] = rows
        context.payload["model_context"] = {
            "rows_shown": len(rows),
            "rows_in_result": interpreted.returned_row_count,
            "model_context_truncated": True,
        }
        context.characters = len(json.dumps(context.payload, sort_keys=True,
                                            default=str))
        if len(rows) == 1:
            break
    context.rows_sent_to_model = len(rows)
    context.model_context_truncated = (
        context.rows_sent_to_model < interpreted.returned_row_count)
