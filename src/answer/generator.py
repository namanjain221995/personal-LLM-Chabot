"""Main model in, GroundedAnswer out.

Parsing is strict about shape and forgiving about spelling: a model that
returns a bare string where a list was asked for has still answered, and
rejecting it would send a correct answer to the fallback. A model that returns
nothing usable has not, and that is an error.
"""
from __future__ import annotations

import logging
from typing import Any

from .client import MainModelClient
from .models import AnswerDetail, GroundedAnswer

log = logging.getLogger(__name__)

ANSWER_TYPES = ("record_list", "count", "aggregate", "comparison", "empty",
                "summary")


def parse_answer(payload: Any) -> GroundedAnswer | None:
    if not isinstance(payload, dict):
        return None
    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None

    answer_type = str(payload.get("answer_type") or "summary").strip().lower()
    if answer_type not in ANSWER_TYPES:
        answer_type = "summary"

    details: list[AnswerDetail] = []
    for item in payload.get("details") or []:
        if isinstance(item, dict):
            text = item.get("text")
        else:
            text = item
        if isinstance(text, str) and text.strip():
            details.append(AnswerDetail(text.strip()))

    notes = [n.strip() for n in (payload.get("notes") or [])
             if isinstance(n, str) and n.strip()]

    freshness_note = payload.get("freshness_note")
    if not isinstance(freshness_note, str) or not freshness_note.strip():
        freshness_note = None
    else:
        freshness_note = freshness_note.strip()

    return GroundedAnswer(answer_type=answer_type, summary=summary.strip(),
                          details=details, notes=notes,
                          freshness_note=freshness_note,
                          interpretation=parse_interpretation(payload.get("interpretation")))


def parse_interpretation(raw: Any) -> dict[str, Any] | None:
    """The result interpretation, or None when the model gave none usable."""
    if not isinstance(raw, dict):
        return None

    def strings(value: Any) -> list[str]:
        items = value if isinstance(value, list) else [value] if value else []
        return [str(v).strip()[:200] for v in items if str(v or "").strip()][:20]

    answer_type = str(raw.get("answer_type") or "").strip().lower()
    primary = strings(raw.get("primary_facts"))
    if not answer_type or not primary:
        return None
    relation = raw.get("relation")
    return {"answer_type": answer_type[:40], "primary_facts": primary,
            "important_context": strings(raw.get("important_context")),
            "relation": str(relation)[:300] if relation else None}


def generate(client: MainModelClient, messages: list[dict[str, str]],
             ) -> tuple[GroundedAnswer | None, dict[str, Any], str]:
    """One generation attempt. Never raises; the caller decides what a failure means."""
    try:
        payload, meta = client.complete_json(messages)
    except Exception as exc:                            # noqa: BLE001
        # Every exception, not only ModelError. This layer exists to keep a
        # bad model answer away from the user; an exception escaping it would
        # take down the fallback along with the answer.
        log.warning("main model call failed: %s", exc)
        return None, {"model": getattr(client, "model", ""),
                      "endpoint": getattr(client, "endpoint", ""),
                      "duration_ms": 0}, f"{type(exc).__name__}: {exc}"
    answer = parse_answer(payload)
    if answer is None:
        finish = meta.get("finish_reason")
        why = ("the answer was cut off at the token limit" if finish == "length"
               else "the model returned no usable answer object")
        return None, meta, f"{why} (finish_reason={finish})"
    return answer, meta, ""
