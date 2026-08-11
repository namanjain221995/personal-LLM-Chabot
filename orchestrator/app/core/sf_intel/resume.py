"""Is this message the answer to the question we just asked, or a new subject?

Getting this wrong is the difference between an assistant that feels like it is
listening and one that has to be fought. Two failure modes, both bad:

  - treating an answer as a new question → the clarification is asked again, the
    original request is lost, and the user retypes it;
  - treating a new question as an answer → the user's actual question is folded
    into an unrelated intent as a "date range".

So: deterministic signals decide the clear cases (an option label, a bare
number, a slot-shaped phrase), and only the genuinely uncertain middle is put to
the model — with a schema, and with a bias toward ANSWER, because a pending
question is the strongest context there is.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from ... import llm
from .models import ClarificationRequest

log = logging.getLogger(__name__)

Verdict = Literal["answers_pending", "new_topic", "unclear"]

#: A reply longer than this is very unlikely to be an answer to "which period?"
#: and very likely to be a fresh request. Measured against the option labels
#: this app produces, which top out around 60 characters.
_LONG_REPLY_CHARS = 180

#: A reply that OPENS like a question needs far less length to be a new one.
#: It is only reached after the slot-shape check has already failed, so by this
#: point the reply does not look like an answer to what was asked — "Which
#: accounts have had no activity for six months, and who owns them?" is a
#: request, not a period.
_NEW_TOPIC_QUESTION_CHARS = 80

#: Phrases that announce a change of subject outright.
_NEW_TOPIC_RE = re.compile(
    r"\b(forget (that|it)|never ?mind|instead[, ]|new question|different question|"
    r"change of topic|actually,? (can|could|show|give|tell)|scrap that|stop that)\b",
    re.I,
)

#: A reply that is itself a question about something else.
_QUESTION_RE = re.compile(
    r"^(who|what|when|where|why|how|which|can you|could you|show|list|give me|"
    r"tell me|find|search|compare|explain)\b",
    re.I,
)

#: Slot-shaped answers. If the pending slot is `date_range` and the reply is
#: "last 90 days", it is an answer whatever else it looks like.
_SLOT_SHAPES = {
    "date_range": re.compile(
        r"\b(today|yesterday|this (week|month|quarter|year)|last (week|month|"
        r"quarter|year)|next (week|month|quarter|year)|last \d+ (days?|weeks?|"
        r"months?|quarters?|years?)|past \d+ (days?|weeks?|months?)|ytd|mtd|qtd|"
        r"all ?time|q[1-4]|fy\d{2,4}|\d{4}|since \w+|between .+ and .+|"
        r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4})\b",
        re.I,
    ),
    "owner_scope": re.compile(
        r"\b(mine|me|my (team|records|opportunities|accounts)|everyone|everybody|"
        r"all users|the whole team|my org|unassigned|team)\b",
        re.I,
    ),
    "region": re.compile(
        r"\b(emea|apac|amer|americas|north america|latam|na|uk|europe|asia|"
        r"india|us|usa|global|all regions|worldwide)\b",
        re.I,
    ),
    "status": re.compile(
        r"\b(open|closed|closed won|closed lost|won|lost|active|inactive|"
        r"pending|in progress|new|qualified|any status|all statuses|escalated)\b",
        re.I,
    ),
    "result_format": re.compile(
        r"\b(chart|graph|table|list|count|summary|records|numbers?|breakdown)\b",
        re.I,
    ),
    "grouping": re.compile(
        r"\b(by \w+|per \w+|grouped by|no grouping|don'?t group)\b", re.I
    ),
}

_SKIP_RE = re.compile(
    r"^(skip|skip it|no preference|doesn'?t matter|does not matter|whatever|"
    r"you decide|either|any|n/?a)\.?$",
    re.I,
)


@dataclass(frozen=True)
class Resolution:
    verdict: Verdict
    #: The normalized value when this is an answer. Empty for a skip.
    value: str = ""
    #: The option the reply matched, when it matched one.
    option_id: str = ""
    #: Deterministic signal name, or "model". For logs and tests, never the UI.
    reason: str = ""
    skipped: bool = False


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower()).strip()


def classify_deterministic(
    request: ClarificationRequest, text: str
) -> Optional[Resolution]:
    """The clear cases. None means "ask the model"."""
    reply = (text or "").strip()
    if not reply:
        return None

    # 1. An exact or leading match on an option label. The UI can send the label
    #    with its description appended, so a prefix match on the label counts.
    normalized = _normalize(reply)
    for option in request.options:
        label = _normalize(option.label)
        if not label:
            continue
        if normalized == label or normalized.startswith(label):
            return Resolution(
                "answers_pending",
                value=option.value,
                option_id=option.id,
                reason="option_label",
            )
        # …and the other way round, for a reply that abbreviates the label
        # ("this quarter" for "This quarter (Aug–Oct)").
        if len(normalized) >= 6 and label.startswith(normalized):
            return Resolution(
                "answers_pending",
                value=option.value,
                option_id=option.id,
                reason="option_label_prefix",
            )

    # 2. A bare number picking the nth option — the keyboard shortcut the card
    #    advertises, typed into the composer instead.
    if re.fullmatch(r"[1-9]\d?", reply):
        index = int(reply) - 1
        if 0 <= index < len(request.options):
            option = request.options[index]
            return Resolution(
                "answers_pending",
                value=option.value,
                option_id=option.id,
                reason="option_number",
            )

    # 3. An explicit pass.
    if _SKIP_RE.match(reply):
        return Resolution("answers_pending", value="", reason="skip", skipped=True)

    # 4. An explicit change of subject wins over everything below it.
    if _NEW_TOPIC_RE.search(reply):
        return Resolution("new_topic", reason="new_topic_phrase")

    # 5. A phrase shaped like the slot we asked about. "Last 90 days" is an
    #    answer to "which period?" no matter how it is punctuated.
    shape = _SLOT_SHAPES.get(request.slot)
    if shape is not None and shape.search(reply) and len(reply) <= _LONG_REPLY_CHARS:
        return Resolution("answers_pending", value=reply, reason="slot_shape")

    # 6. A reply that opens like a question and is too long to be an answer.
    #    Reached only after the slot-shape check above has already failed.
    if len(reply) > _NEW_TOPIC_QUESTION_CHARS and _QUESTION_RE.match(reply):
        return Resolution("new_topic", reason="long_question")

    # 7. Anything else long enough to be a request in its own right.
    if len(reply) > _LONG_REPLY_CHARS:
        return Resolution("new_topic", reason="too_long_to_be_an_answer")

    return None


_CLASSIFIER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["answers_pending", "new_topic"],
        },
        "normalized_value": {"type": "string"},
    },
    "required": ["verdict"],
    "additionalProperties": False,
}

_CLASSIFIER_SYSTEM = """\
You decide whether the user's newest message ANSWERS a question the assistant is
waiting on, or starts a NEW request.

Return only JSON: {"verdict": "answers_pending" | "new_topic",
"normalized_value": "<the answer, normalized>"}.

The assistant asked about one specific missing detail. A message that supplies
that detail — in any wording, including a partial or negative one ("not this
quarter, last quarter") — answers it. A message about a different subject,
object, or task starts a new request.

When it could plausibly be either, choose answers_pending: a pending question is
strong context, and treating an answer as a new request loses the user's
original question entirely.

normalized_value is the answer expressed plainly (for example "last 90 days",
"EMEA", "closed won"). Leave it empty for new_topic.
"""


async def classify(
    request: ClarificationRequest,
    text: str,
    *,
    original_request: str = "",
    recent_summary: str = "",
) -> Resolution:
    """Deterministic signals first, the model only for the uncertain middle.

    Never raises: a classifier that cannot be reached falls back to
    `answers_pending`, which is the safer error. Mistaking an answer for a new
    topic destroys the original request; mistaking a new topic for an answer
    costs one turn and is visible immediately.
    """
    decided = classify_deterministic(request, text)
    if decided is not None:
        return decided

    payload = {
        "pending_question": request.question,
        "expected_detail": request.slot,
        "options_offered": [o.label for o in request.options],
        "original_request": original_request or "",
        "recent_conversation": recent_summary[:1500] if recent_summary else "",
        "latest_user_message": (text or "")[:1500],
    }
    try:
        raw = await llm.json_completion(
            [
                {"role": "system", "content": _CLASSIFIER_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(payload, ensure_ascii=False),
                },
            ],
            json_schema=_CLASSIFIER_SCHEMA,
            schema_name="topic_shift",
            temperature=0.0,
            max_tokens=200,
            thinking=False,
        )
    except Exception as exc:  # noqa: BLE001
        log.info("topic-shift classifier unavailable (%s)", str(exc)[:160])
        return Resolution("answers_pending", value=(text or "").strip(), reason="fallback")

    from .planner import extract_json_object

    parsed = extract_json_object(raw) or {}
    verdict = str(parsed.get("verdict") or "").strip()
    if verdict == "new_topic":
        return Resolution("new_topic", reason="model")
    value = str(parsed.get("normalized_value") or "").strip() or (text or "").strip()
    return Resolution("answers_pending", value=value, reason="model")


# ---------------------------------------------------------------------------
# Starter-card context
# ---------------------------------------------------------------------------

def continuation_label(state) -> str:
    """"Continue the previous Salesforce task" — but only when there is one.

    Returns "" when the conversation has nothing to continue, which is what the
    starter card checks before offering it as the first option.
    """
    summary = (getattr(state, "last_query_summary", "") or "").strip()
    if not summary:
        return ""
    clipped = summary if len(summary) <= 60 else summary[:57].rstrip() + "…"
    return f"Continue: {clipped}"
