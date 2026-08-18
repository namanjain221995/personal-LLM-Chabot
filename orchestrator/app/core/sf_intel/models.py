"""Typed contracts for Salesforce Intelligence Mode.

Every structure the model produces is validated here before anything acts on
it. That is the whole point: control flow is decided by a schema, never by
reading prose. A planner that answers "I think you mean opportunities?" in a
sentence is a planner whose output we throw away and repair once, then fall
back from — see planner.py.

The frontend mirrors these shapes in `frontend/lib/clarification.ts`; the two
files are a contract and must be changed together.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _decode_nested(payload: Any, fields: tuple) -> Any:
    """JSON-decode named fields that arrived as strings.

    vLLM's `qwen3_xml` tool parser — the one this deployment runs — serialises a
    NESTED object argument as a JSON string rather than as a nested object.
    Verified live on 2026-08-11: `submit_plan` came back with `action` and
    `internal_reason_code` as proper values and `clarification_draft` as
    `'{"slot": "object", …}'`.

    That is a transport detail, not a model failure, and rejecting the whole
    decision over it would throw away a perfectly good plan and fall back to the
    deterministic detectors on every single turn. So it is normalised here,
    where the schema is — and only for fields that are DECLARED structured, so a
    string field whose value happens to look like JSON is never rewritten.
    """
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    for name in fields:
        value = out.get(name)
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text[0] not in "[{":
            continue
        try:
            out[name] = json.loads(text)
        except ValueError:
            # Leave it alone: validation will report the real problem rather
            # than a confusing one invented here.
            pass
    return out

# ---------------------------------------------------------------------------
# Slots
# ---------------------------------------------------------------------------

#: The only slot names a clarification may ask about. A free-form slot string
#: would defeat the repetition guard (question_fingerprint) and the resume
#: merge, both of which key on it, so the planner is held to this list.
SLOTS = (
    "object",            # which Salesforce object / business domain
    "record_identity",   # which specific record
    "metric",            # what is being measured
    "date_range",        # over what period
    "owner_scope",       # whose records
    "region",            # which region / business unit
    "status",            # which status or stage
    "comparison_baseline",  # compared against what
    "grouping",          # broken down by what
    "result_format",     # records, count, summary or chart
    "filter",            # another critical filter
)

#: Slots whose value is carried forward to the NEXT request in this
#: conversation ("what about EMEA?" keeps everything but region). Ordered for
#: stable rendering in the session summary.
CARRIED_SLOTS = (
    "object",
    "metric",
    "date_range",
    "owner_scope",
    "region",
    "status",
    "grouping",
    "filter",
    "result_format",
)

MAX_OPTIONS = 4
MIN_OPTIONS = 2

_WORD_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by do does for from how i in is it its me my of on or "
    "should show shall that the their them these this to us we what when which "
    "who whom why will with would you your".split()
)


def utcnow_iso() -> str:
    """UTC, ISO-8601, seconds precision — the timestamp format history uses."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


#: A Salesforce API name as it appears in prose: `Internal_Interview__c`,
#: `Candidate_Training__r`, `Final_Descion__c`. Matched with the suffix
#: REQUIRED, so an ordinary capitalised word is never touched.
_API_NAME_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*)__[cr]\b")

#: Standard objects and the field suffixes that give an internal name away in a
#: label. Only whole words, and only ones a business user would never say.
_INTERNAL_TOKEN_RE = re.compile(
    r"\b(RecordTypeId|RecordType\.\w+|\w+Id|SOQL|soql|API name|api_name)\b"
)


def humanize(text: str) -> str:
    """Strip implementation vocabulary out of text a user will read.

    Option labels are the one place a planner reliably leaks the schema:
    "Internal_Interview__c rows" is a perfectly good answer to which object to
    query and a terrible thing to show someone who asked about mock interviews.
    The machine-facing half of an option — `value` — keeps the API name, which
    is where it belongs and where the query planner reads it from.

    Deterministic and reversible-looking rather than clever: `Foo_Bar__c`
    becomes "foo bar", so the sentence around it still reads.
    """
    cleaned = _API_NAME_RE.sub(
        lambda m: m.group(1).replace("_", " ").strip().lower(), text or ""
    )
    cleaned = _INTERNAL_TOKEN_RE.sub("", cleaned)
    # Collapse whatever the substitutions left behind — a removed token can
    # strand a double space or a space before punctuation.
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    return cleaned.strip()


def fingerprint(slot: str, question: str) -> str:
    """A SEMANTIC fingerprint for "have we already asked this?".

    Wording drifts between rounds — "Over what period?" then "Which time range
    should I use?" — so comparing the question strings lets the same question be
    asked twice with different words, which is exactly the loop this exists to
    stop. The slot dominates (it is what an answer actually fills), and the
    content words are folded in so two genuinely different questions about the
    same slot are still distinguishable.
    """
    words = sorted(
        w for w in _WORD_RE.findall((question or "").lower())
        if w not in _STOPWORDS and len(w) > 2
    )
    digest = hashlib.sha256(
        f"{slot}|{' '.join(words)}".encode("utf-8")
    ).hexdigest()
    return digest[:32]


# ---------------------------------------------------------------------------
# Clarification
# ---------------------------------------------------------------------------

class ClarificationOption(BaseModel):
    """One selectable answer. `value` is what gets merged into resolved_slots."""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=240)
    #: Normalized value applied to the missing slot. Never rendered as such —
    #: the label is what the user reads.
    value: str = Field(default="", max_length=400)
    #: Safe display metadata only (account city, industry, owner name…). Ids and
    #: anything sensitive are deliberately NOT put here; see engines/sf_intel.py.
    metadata: Dict[str, str] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _machine_readable(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
            raise ValueError("option id must be machine-readable [A-Za-z0-9._-]")
        return value

    @model_validator(mode="after")
    def _value_defaults_to_label(self) -> "ClarificationOption":
        # ORDER MATTERS: `value` inherits the label BEFORE the label is
        # cleaned, so an option whose only content is an object name still
        # tells the query planner which object — while the user reads "internal
        # interview" rather than "Internal_Interview__c".
        if not self.value.strip():
            object.__setattr__(self, "value", self.label)
        object.__setattr__(self, "label", humanize(self.label) or self.label)
        object.__setattr__(self, "description", humanize(self.description))
        return self


class ClarificationRequest(BaseModel):
    """The one question we are asking, and everything needed to resume after it.

    `resume_token` is a server-generated opaque value stored alongside the row;
    a response that does not present it is rejected. It is NOT a substitute for
    ownership checks (main.py already scopes the conversation to its owner) —
    it is what stops a stale card in an old tab from resuming an intent that has
    since been replaced.
    """

    model_config = ConfigDict(extra="ignore")

    clarification_id: str
    conversation_id: str
    run_id: str
    root_user_message_id: str
    intent_id: str
    source: Literal["salesforce"] = "salesforce"
    header: str = Field(default="Salesforce", max_length=60)
    question: str = Field(min_length=1, max_length=280)
    slot: str
    options: List[ClarificationOption]
    allow_custom: bool = True
    custom_placeholder: str = Field(default="Tell me what you meant…", max_length=120)
    multi_select: bool = False
    round_number: int = Field(default=1, ge=1)
    created_at: str = Field(default_factory=utcnow_iso)
    state: Literal["pending", "answered", "skipped", "cancelled"] = "pending"
    resume_token: str
    question_fingerprint: str

    @field_validator("slot")
    @classmethod
    def _known_slot(cls, value: str) -> str:
        slot = (value or "").strip().lower()
        if slot not in SLOTS:
            raise ValueError(f"unknown slot {value!r} (allowed: {', '.join(SLOTS)})")
        return slot

    @field_validator("question", "header")
    @classmethod
    def _reads_as_english(cls, value: str) -> str:
        return humanize(value) or value

    @field_validator("options")
    @classmethod
    def _bounded_unique_options(
        cls, value: List[ClarificationOption]
    ) -> List[ClarificationOption]:
        if not (MIN_OPTIONS <= len(value) <= MAX_OPTIONS):
            raise ValueError(
                f"a clarification needs {MIN_OPTIONS}-{MAX_OPTIONS} options, got {len(value)}"
            )
        ids = [o.id for o in value]
        if len(set(ids)) != len(ids):
            raise ValueError("option ids must be unique within a clarification")
        return value

    def wire(self) -> dict:
        """The payload that rides on `meta.clarification`.

        `resume_token` IS included: the client must send it back, and it is
        worthless to anyone who cannot already reach this conversation.
        """
        return self.model_dump(mode="json")


class ClarificationResponse(BaseModel):
    """What the client sends back. Idempotent on (clarification_id, client_message_id)."""

    model_config = ConfigDict(extra="ignore")

    clarification_id: str = Field(min_length=1, max_length=80)
    conversation_id: str = Field(default="", max_length=200)
    #: Client-generated, stable across retries of the SAME click. Double-clicking
    #: an option reuses it, so the second submission returns the first result
    #: instead of starting a second run.
    client_message_id: str = Field(default="", max_length=80)
    selected_option_ids: List[str] = Field(default_factory=list)
    custom_text: str = Field(default="", max_length=2000)
    skipped: bool = False
    resume_token: str = Field(default="", max_length=80)
    submitted_at: str = Field(default_factory=utcnow_iso)

    @model_validator(mode="after")
    def _says_something(self) -> "ClarificationResponse":
        if not self.skipped and not self.selected_option_ids and not self.custom_text.strip():
            raise ValueError(
                "a clarification response must select an option, carry custom "
                "text, or be marked skipped"
            )
        return self


# ---------------------------------------------------------------------------
# Intent + conversation state
# ---------------------------------------------------------------------------

class ClarificationRound(BaseModel):
    """One completed question/answer pair, kept so we never repeat ourselves."""

    model_config = ConfigDict(extra="ignore")

    clarification_id: str
    slot: str
    question: str
    question_fingerprint: str
    answer: str = ""
    answered: bool = False
    skipped: bool = False
    round_number: int = 1


class PendingIntent(BaseModel):
    """The request being resolved, which survives one or two clarifications."""

    model_config = ConfigDict(extra="ignore")

    intent_id: str
    conversation_id: str
    root_user_message_id: str
    original_user_text: str
    source_mode: Literal["salesforce", "assistant"] = "salesforce"
    normalized_intent: str = ""
    resolved_slots: Dict[str, str] = Field(default_factory=dict)
    missing_slots: List[str] = Field(default_factory=list)
    clarification_history: List[ClarificationRound] = Field(default_factory=list)
    query_plan_draft: Optional[Dict[str, Any]] = None
    status: Literal["open", "awaiting_clarification", "completed", "cancelled"] = "open"
    created_at: str = Field(default_factory=utcnow_iso)
    updated_at: str = Field(default_factory=utcnow_iso)

    @property
    def rounds_used(self) -> int:
        return len(self.clarification_history)

    def already_asked(self, question_fingerprint: str) -> bool:
        return any(
            r.question_fingerprint == question_fingerprint
            for r in self.clarification_history
        )

    def asked_slots(self) -> List[str]:
        return [r.slot for r in self.clarification_history]

    def resolved_text(self) -> str:
        """The original request with the answers folded in, as ONE instruction.

        This is what the execution engines receive. Keeping the original text
        verbatim matters — the answers narrow it, they do not replace it, and a
        rewritten question is how "closing this quarter in North America" turns
        into a query about EMEA only.
        """
        if not self.resolved_slots:
            return self.original_user_text
        scope = "; ".join(
            f"{slot.replace('_', ' ')}: {value}"
            for slot, value in self.resolved_slots.items()
            if value
        )
        if not scope:
            return self.original_user_text
        return f"{self.original_user_text}\n\n(Clarified: {scope})"


class ConversationSalesforceState(BaseModel):
    """What this conversation has established about Salesforce so far.

    This is what makes "what about EMEA?" answerable: everything except the
    region carries forward from `last_*`, and only the region changes.
    """

    model_config = ConfigDict(extra="ignore")

    conversation_id: str
    source_enabled: bool = True
    active_intent_id: Optional[str] = None
    pending_clarification_id: Optional[str] = None
    last_completed_intent: Optional[str] = None
    last_salesforce_objects: List[str] = Field(default_factory=list)
    last_entities: List[str] = Field(default_factory=list)
    last_filters: Dict[str, str] = Field(default_factory=dict)
    last_date_range: str = ""
    last_owner_scope: str = ""
    last_grouping: str = ""
    last_metric: str = ""
    last_query_summary: str = ""
    last_result_metadata: Dict[str, Any] = Field(default_factory=dict)
    compact_session_summary: str = ""
    updated_at: str = Field(default_factory=utcnow_iso)

    def carried_slots(self) -> Dict[str, str]:
        """Slot values a follow-up inherits unless it changes them."""
        carried: Dict[str, str] = {}
        if self.last_salesforce_objects:
            carried["object"] = ", ".join(self.last_salesforce_objects[:3])
        if self.last_metric:
            carried["metric"] = self.last_metric
        if self.last_date_range:
            carried["date_range"] = self.last_date_range
        if self.last_owner_scope:
            carried["owner_scope"] = self.last_owner_scope
        if self.last_grouping:
            carried["grouping"] = self.last_grouping
        for key, value in self.last_filters.items():
            if key in SLOTS and value and key not in carried:
                carried[key] = value
        return carried

    def brief(self) -> str:
        """A compact, model-facing summary of the Salesforce context so far.

        Deliberately small — the whole point of the state table is that a
        follow-up does not need the transcript to be understood.
        """
        if not (self.last_query_summary or self.last_salesforce_objects):
            return ""
        lines = ["Salesforce context established earlier in this conversation:"]
        if self.last_salesforce_objects:
            lines.append(f"- objects: {', '.join(self.last_salesforce_objects[:5])}")
        if self.last_metric:
            lines.append(f"- metric: {self.last_metric}")
        if self.last_date_range:
            lines.append(f"- date range: {self.last_date_range}")
        if self.last_owner_scope:
            lines.append(f"- owner scope: {self.last_owner_scope}")
        if self.last_grouping:
            lines.append(f"- grouped by: {self.last_grouping}")
        for key, value in list(self.last_filters.items())[:6]:
            lines.append(f"- {key.replace('_', ' ')}: {value}")
        if self.last_query_summary:
            lines.append(f"- last request: {self.last_query_summary}")
        if self.last_result_metadata.get("record_count") is not None:
            lines.append(
                f"- last result: {self.last_result_metadata['record_count']} record(s)"
            )
        if self.compact_session_summary:
            lines.append(f"- session summary: {self.compact_session_summary}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Query plan
# ---------------------------------------------------------------------------

#: Filter operators the compiler will emit. Anything else is refused rather
#: than passed through — an operator allowlist is cheaper to reason about than
#: an operator denylist, and SOQL has more syntax than a denylist can cover.
FILTER_OPERATORS = (
    "eq", "ne", "lt", "lte", "gt", "gte",
    "like", "starts_with", "ends_with", "contains",
    "in", "not_in", "is_null", "is_not_null",
)

AGGREGATE_FUNCTIONS = ("count", "count_distinct", "sum", "avg", "min", "max")

RESULT_MODES = ("records", "aggregate", "count", "comparison", "timeline")


class QueryFilter(BaseModel):
    """One WHERE predicate, in pieces the compiler can escape independently.

    The model never writes `WHERE ...`. It names a field, an operator from the
    allowlist, and a value; escaping and quoting happen in plan.py against the
    field's real Salesforce type.
    """

    model_config = ConfigDict(extra="ignore")

    field: str = Field(min_length=1, max_length=120)
    operator: str = "eq"
    value: Optional[str] = None
    values: List[str] = Field(default_factory=list)
    #: Salesforce date literals (TODAY, THIS_QUARTER, LAST_N_DAYS:30) are
    #: keywords, not quoted strings, and must be marked as such by the planner.
    is_date_literal: bool = False

    @field_validator("operator")
    @classmethod
    def _known_operator(cls, value: str) -> str:
        op = (value or "").strip().lower()
        if op not in FILTER_OPERATORS:
            raise ValueError(
                f"unsupported filter operator {value!r} "
                f"(allowed: {', '.join(FILTER_OPERATORS)})"
            )
        return op

    @model_validator(mode="after")
    def _operand_shape(self) -> "QueryFilter":
        if self.operator in ("in", "not_in"):
            if not self.values:
                raise ValueError(f"{self.operator} needs a non-empty `values` list")
        elif self.operator in ("is_null", "is_not_null"):
            pass  # no operand
        elif self.value is None or self.value == "":
            raise ValueError(f"{self.operator} needs a `value`")
        return self


class OrderBy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    field: str = Field(min_length=1, max_length=120)
    direction: Literal["asc", "desc"] = "asc"


class SalesforceQueryPlan(BaseModel):
    """A structured request for records. Compiled to SOQL by plan.py — NEVER by
    the model, and never by string-formatting user text into a clause."""

    model_config = ConfigDict(extra="ignore")

    object_api_name: str = Field(min_length=1, max_length=120)
    select_fields: List[str] = Field(default_factory=list)
    aggregate_functions: List[str] = Field(default_factory=list)
    filters: List[QueryFilter] = Field(default_factory=list)
    #: Parent traversals (`Account.Owner.Name`). Depth is bounded in plan.py.
    relationship_paths: List[str] = Field(default_factory=list)
    group_by: List[str] = Field(default_factory=list)
    having: List[QueryFilter] = Field(default_factory=list)
    order_by: List[OrderBy] = Field(default_factory=list)
    limit: int = Field(default=200, ge=1, le=2000)
    offset: int = Field(default=0, ge=0, le=2000)
    search_terms: List[str] = Field(default_factory=list)
    result_mode: str = "records"

    @model_validator(mode="before")
    @classmethod
    def _decode_tool_call_strings(cls, payload: Any) -> Any:
        return _decode_nested(
            payload,
            (
                "select_fields",
                "aggregate_functions",
                "filters",
                "relationship_paths",
                "group_by",
                "having",
                "order_by",
                "search_terms",
            ),
        )

    @field_validator("result_mode")
    @classmethod
    def _known_result_mode(cls, value: str) -> str:
        mode = (value or "records").strip().lower()
        if mode not in RESULT_MODES:
            raise ValueError(
                f"unknown result_mode {value!r} (allowed: {', '.join(RESULT_MODES)})"
            )
        return mode

    @field_validator("aggregate_functions")
    @classmethod
    def _known_aggregates(cls, value: List[str]) -> List[str]:
        out = []
        for item in value:
            spec = (item or "").strip()
            name = spec.split("(", 1)[0].strip().lower()
            if name not in AGGREGATE_FUNCTIONS:
                raise ValueError(
                    f"unsupported aggregate {item!r} "
                    f"(allowed: {', '.join(AGGREGATE_FUNCTIONS)})"
                )
            out.append(spec)
        return out


# ---------------------------------------------------------------------------
# Planner decision
# ---------------------------------------------------------------------------

ACTIONS = (
    "EXECUTE_SALESFORCE",
    "ASK_CLARIFICATION",
    "ANSWER_GENERAL",
    "UNSUPPORTED",
    "DENY",
)


class ClarificationDraft(BaseModel):
    """What the planner proposes to ask. Promoted to a ClarificationRequest by
    state.py, which is the only thing allowed to mint ids and resume tokens."""

    model_config = ConfigDict(extra="ignore")

    slot: str
    header: str = Field(default="Salesforce", max_length=60)
    question: str = Field(min_length=1, max_length=280)
    options: List[ClarificationOption] = Field(default_factory=list)
    allow_custom: bool = True
    custom_placeholder: str = Field(default="", max_length=120)
    #: TRI-STATE, and deliberately so. `None` means the planner expressed no
    #: view and the deployment default applies; `False` is an explicit "ticking
    #: two of these is incoherent" that must be honoured. Collapsing the two
    #: into one boolean is what let a global default override the planner on
    #: every card, including the ones whose options were alternative readings
    #: of a single number.
    multi_select: Optional[bool] = None

    @model_validator(mode="before")
    @classmethod
    def _decode_tool_call_strings(cls, payload: Any) -> Any:
        return _decode_nested(payload, ("options",))

    @field_validator("slot")
    @classmethod
    def _known_slot(cls, value: str) -> str:
        slot = (value or "").strip().lower()
        if slot not in SLOTS:
            raise ValueError(f"unknown slot {value!r}")
        return slot

    @field_validator("question", "header")
    @classmethod
    def _reads_as_english(cls, value: str) -> str:
        return humanize(value) or value

    @field_validator("options")
    @classmethod
    def _bounded(cls, value: List[ClarificationOption]) -> List[ClarificationOption]:
        if len(value) > MAX_OPTIONS:
            return value[:MAX_OPTIONS]
        return value


class AgentDecision(BaseModel):
    """The planner's ONLY output. Validated before anything acts on it."""

    model_config = ConfigDict(extra="ignore")

    action: str
    normalized_intent: str = Field(default="", max_length=600)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    resolved_slots: Dict[str, str] = Field(default_factory=dict)
    missing_critical_slots: List[str] = Field(default_factory=list)
    assumptions: List[str] = Field(default_factory=list)
    clarification_draft: Optional[ClarificationDraft] = None
    structured_query_plan: Optional[SalesforceQueryPlan] = None
    #: A short categorical diagnostic. NEVER shown to the user and never a
    #: place to put reasoning — see the planner prompt.
    internal_reason_code: str = Field(default="", max_length=64)

    @model_validator(mode="before")
    @classmethod
    def _decode_tool_call_strings(cls, payload: Any) -> Any:
        return _decode_nested(
            payload,
            (
                "clarification_draft",
                "structured_query_plan",
                "resolved_slots",
                "missing_critical_slots",
                "assumptions",
            ),
        )

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        action = (value or "").strip().upper()
        if action not in ACTIONS:
            raise ValueError(f"unknown action {value!r} (allowed: {', '.join(ACTIONS)})")
        return action

    @field_validator("resolved_slots")
    @classmethod
    def _drop_unknown_slots(cls, value: Dict[str, str]) -> Dict[str, str]:
        # Dropped rather than rejected: a planner that invents "urgency" should
        # not cost the user the four slots it got right.
        return {
            k.strip().lower(): str(v)
            for k, v in (value or {}).items()
            if k.strip().lower() in SLOTS and str(v).strip()
        }

    @field_validator("missing_critical_slots")
    @classmethod
    def _known_missing(cls, value: List[str]) -> List[str]:
        return [s.strip().lower() for s in value or [] if s.strip().lower() in SLOTS]

    @model_validator(mode="after")
    def _action_carries_its_payload(self) -> "AgentDecision":
        if self.action == "ASK_CLARIFICATION" and self.clarification_draft is None:
            raise ValueError("ASK_CLARIFICATION requires a clarification_draft")
        return self
