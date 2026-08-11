"""Progress phases — what the UI is allowed to say while a request runs.

These are SUMMARIES of work that has actually started, not a narration of the
model's thinking and not a timer walking through plausible-sounding steps. Each
one is emitted at the moment the corresponding stage begins, so "Searching
Salesforce" means a query is in flight, and the animation stops when the
backend stops.

They ride on the EXISTING `status` SSE event, additively: the payload keeps its
`{"text": ...}` shape (every current client reads that and nothing else) and
gains `phase`, `run_id` and the optional counters. No new event name, so replay,
persistence and the frontend's allowlist are untouched.
"""
from __future__ import annotations

from typing import Optional

#: Ordered roughly as a request meets them. `reconnecting` and the two terminal
#: phases are out-of-band and may follow any of the others.
PHASES = (
    "understanding",
    "resolving_context",
    "checking_schema",
    "clarifying",
    "querying_salesforce",
    "retrieving_more_results",
    "analyzing_records",
    "calculating",
    "verifying",
    "drafting_answer",
    "reconnecting",
    "completed",
    "failed",
)

#: The default user-facing wording. Concise and factual — a label that promises
#: work the backend is not doing is worse than no label.
LABELS = {
    "understanding": "Understanding your request",
    "resolving_context": "Using this conversation's context",
    "checking_schema": "Checking Salesforce fields",
    "clarifying": "Checking one detail with you",
    "querying_salesforce": "Searching Salesforce",
    "retrieving_more_results": "Retrieving more results",
    "analyzing_records": "Analyzing records",
    "calculating": "Calculating the totals",
    "verifying": "Verifying the numbers",
    "drafting_answer": "Preparing the answer",
    "reconnecting": "Reconnecting",
    "completed": "Done",
    "failed": "That did not work",
}

#: Phases during which the UI shows an active indicator. `completed`/`failed`
#: are terminal, and `clarifying` hands over to the card rather than spinning.
ACTIVE_PHASES = frozenset(PHASES) - {"completed", "failed", "clarifying"}


def label_for(phase: str, *, record_count: Optional[int] = None) -> str:
    """The line the user reads. Counts are folded in where they are real.

    "Analyzing 42 records" is only allowed to say 42 when 42 records exist —
    the count comes from the query result, never from an estimate.
    """
    base = LABELS.get(phase, "Working")
    if record_count is None:
        return base
    if phase == "analyzing_records":
        return f"Analyzing {record_count:,} record{'' if record_count == 1 else 's'}"
    if phase == "retrieving_more_results":
        return f"Retrieved {record_count:,} record{'' if record_count == 1 else 's'} so far"
    return base


def status_payload(
    phase: str,
    *,
    run_id: str = "",
    started_at: str = "",
    record_count: Optional[int] = None,
    tool_name: str = "",
    label: str = "",
) -> dict:
    """The `status` event payload for one phase.

    `text` stays first-class and is what pre-existing clients render; the rest
    is additive metadata a newer client uses to drive the phase indicator.
    """
    if phase not in PHASES:
        raise ValueError(f"unknown phase {phase!r} (allowed: {', '.join(PHASES)})")
    payload = {
        "text": label or label_for(phase, record_count=record_count),
        "phase": phase,
    }
    if run_id:
        payload["run_id"] = run_id
    if started_at:
        payload["started_at"] = started_at
    if record_count is not None:
        payload["record_count"] = int(record_count)
    if tool_name:
        payload["tool_name"] = tool_name
    return payload
