"""When to accept a selection, escalate it, or refuse.

Refusing is a real outcome. A mapping nobody is confident in should surface as
AMBIGUOUS rather than become SOQL that runs and returns the wrong rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .config import Confidence
from .models import FailureCode, Selection


@dataclass
class Decision:
    selection: Selection
    accepted: bool
    escalated: bool
    code: FailureCode | None = None


def resolve(primary: Selection, escalate: Callable[[], Selection],
            thresholds: Confidence, *, ambiguous_code: FailureCode) -> Decision:
    """Accept, escalate once, or refuse.

    Escalation happens on low confidence OR on a primary that returned nothing
    -- an unreachable model should reach the fallback, not fail the request.
    """
    if primary.selected and primary.confidence >= thresholds.accept_small_model:
        return Decision(primary, accepted=True, escalated=False)

    fallback = escalate()
    fallback.escalated = True

    if fallback.selected and fallback.confidence >= thresholds.fallback_to_main_model:
        return Decision(fallback, accepted=True, escalated=True)

    # Keep whichever tried harder, so the trace shows what was considered.
    best = fallback if fallback.confidence >= primary.confidence else primary
    best.escalated = True
    return Decision(best, accepted=False, escalated=True, code=ambiguous_code)
