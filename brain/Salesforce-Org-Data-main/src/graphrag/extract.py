"""Read a question's business intent with a small model, under a strict schema.

The lexical resolver can only find vocabulary someone wrote down. It scans
every n-gram of a sentence, which is why "recipe for chocolate cake" once
reached BatchJob.IsDebugRecipeDeleted: nothing told it which words were the
subject and which were furniture. This stage says what the question is ABOUT,
so the resolver grounds two or three named concepts instead of every phrase.

What it does NOT do is name Salesforce components. `business_entities` holds
the user's own words -- "mock interview", "candidate" -- and the lexicon maps
those to API names afterwards. Keeping extraction and grounding apart is what
lets the evaluator score them separately, and it stops a model inventing an
object that does not exist in the org.

Decoding is grammar-constrained against intent.schema.json, so an enum cannot
be violated: an invalid `action` is unreachable rather than merely discouraged.
Where the server does not support that, weaker modes are tried in turn and the
result is validated in Python regardless -- a malformed extraction is discarded,
never passed downstream.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any

# Measured 2026-09-25 over 12 cases, one per distinct dataset intent:
#
#                              intent  req_type  primary_entity   p50
#   8B  json_object             9/12    11/12       0/12        5,396 ms
#   8B  json_schema  think off  2/12    10/12       4/12       12,249 ms
#   35B json_schema  think off  1/12    10/12       7/12        2,520 ms
#   35B json_object  think off 11/12     3/12      12/12        2,024 ms
#
# The 35B wins on intent, on entity roles and on latency at once. Its A3B is a
# mixture of experts, so only a fraction of the parameters activate per token.
DEFAULT_ENDPOINT = "http://vllm:8000/v1"
DEFAULT_MODEL = "Qwen/Qwen3.6-35B-A3B-NVFP4"
DEFAULT_TIMEOUT = 30.0

# The schema is a reviewable file, not a string inside a prompt: it is the
# contract between this stage and everything downstream, and it is versioned
# with the code that depends on it.
SCHEMA_PATH = Path(__file__).with_name("intent.schema.json")
SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

# Short on purpose. Latency is linear in output length, and every token spent
# restating the schema is a token the grammar already guarantees.
def _enum_block() -> str:
    """The allowed values, spelled out.

    json_object promises valid JSON and nothing about values, so the enums have
    to be stated. Under json_schema the grammar enforced them instead -- and
    that turned out to cost far more than it bought: same model, same cases,
    intent fell from 11/12 to 1/12 because constraining the tokens pushed the
    model toward whichever value the grammar reached first.
    """
    props = SCHEMA["properties"]
    role = props["business_entities"]["items"]["properties"]["role"]["enum"]
    operator = props["filters"]["items"]["properties"]["operator"]["enum"]
    return (
        "\n\nReturn exactly these keys: request_type, intent, action, "
        "business_entities (name, role), filters (concept, operator, value), "
        "requested_attributes (entity, attribute), temporal "
        "(expression, concept) or null, return_mode, metadata_types, "
        "requires_schema_discovery, requires_record_query, "
        "requires_metadata_context."
        "\nreturn_mode MUST be one of: records, count, aggregate, "
        "single_record, exists, or null."
        "\nThe three requires_* keys are booleans:"
        "\n  requires_schema_discovery: true when the Salesforce object or "
        "field behind a business word is not yet known"
        "\n  requires_record_query: true when actual records must be read"
        "\n  requires_metadata_context: true when org configuration "
        "(flows, rules, permissions, layouts) must be read"
        "\nrequest_type MUST be exactly one of: " + " | ".join(props["request_type"]["enum"]) +
        "\nintent MUST be exactly one of: " + ", ".join(props["intent"]["enum"]) +
        "\naction MUST be exactly one of: " + ", ".join(props["action"]["enum"]) +
        "\nrole MUST be exactly one of: " + ", ".join(role) +
        "\noperator MUST be exactly one of: " + ", ".join(operator))


SYSTEM_PROMPT = """Read a Salesforce question and return its business intent.

Use the user's OWN words for entity names. Never invent Salesforce API names.
Split compound phrases: "candidate Person Accounts" is two entities.

Rules:
- "how many X"                     -> count / DATA
- "which fields ... on X"          -> inspect / METADATA
- "which flow/rule does X"         -> explain / METADATA
- spans two or more objects        -> cross_object_record_list
- asks for passwords/tokens/keys   -> refuse_sensitive_data
- too vague to answer as asked     -> clarification_required

Semantic roles -- keep these apart, they are different things:
- A business ENTITY is a KIND of record: employee, candidate, interview,
  background check. It is never a specific person, company or record.
- A specific person, company or record NAME is a VALUE. Put it in filters as
  {"concept": "name", "operator": "equals", "value": "<the name>"} and name
  its kind as the entity. "Is Jayesh Prajapati available for X" -> entity
  "employee", filter name = "Jayesh Prajapati".
- A date or period (today, tomorrow, last month, May 2026, 2025, this week)
  is TEMPORAL: {"expression": "<as the user said it>", "concept": "<which
  date, e.g. scheduled, created, joining>" or null}. Never a filter concept,
  never a requested attribute. Do not work out calendar dates yourself.
- Scope words -- all, list, show, give me, records, details, every -- set
  return_mode. They are never requested_attributes.
- A qualifier on the records is a FILTER, with the user's words as the concept:
  "active candidates"                -> concept "status", value "Active"
  "offer received status"            -> concept "status", value "Offer Received"
  "available to provide support"     -> concept "available to provide support", value true
  "available to take mock interviews"-> concept "available to take mock interviews", value true
  Write the value as the user wrote it; true/false for yes/no qualifiers.
- requested_attributes are only the properties the user wants SHOWN
  ("their emails", "the joining date"), never the filter or the scope.
- A yes/no question about ONE named record asks for a property: "is Jayesh
  Prajapati available to take mock interviews" -> filter name = "Jayesh
  Prajapati", requested_attribute "available to take mock interviews". The
  qualifier is what to SHOW, not what to filter on.
- Never restate the entity as a filter: "internal interviews" has no filter
  "type = internal"; the entity already says it.
- "currently", "now", "so far", "in total" do not restrict dates: temporal null.
- Always set all three requires_* booleans."""

SYSTEM_PROMPT += _enum_block()


class ExtractError(RuntimeError):
    """Raised when the endpoint is unreachable or answers unusably."""


@dataclass
class Extraction:
    request_type: str
    intent: str
    action: str
    business_entities: list[dict[str, str]] = dataclass_field(default_factory=list)
    filters: list[dict[str, Any]] = dataclass_field(default_factory=list)
    requested_attributes: list[dict[str, str]] = dataclass_field(default_factory=list)
    metadata_types: list[str] = dataclass_field(default_factory=list)
    requires_schema_discovery: bool | None = None
    requires_record_query: bool | None = None
    requires_metadata_context: bool | None = None
    # A date restriction as the user phrased it; resolved to a range by code.
    temporal: dict[str, Any] | None = None
    return_mode: str | None = None
    duration_ms: int = 0
    mode: str = ""
    completion_tokens: int = 0
    # Which model read the question, and where it was reached. Carried on the
    # result rather than left at the call site: a trace that records an intent
    # without the model that produced it cannot attribute a bad intent to
    # anything, and every caller would otherwise have to remember to pass both
    # in alongside.
    model: str = ""
    endpoint: str = ""

    @property
    def entity_names(self) -> list[str]:
        """Primary entities first: the resolver should ground those hardest."""
        order = {"primary_entity": 0, "target_entity": 1, "related_entity": 2,
                 "actor": 3, "context_entity": 4}
        ranked = sorted(self.business_entities,
                        key=lambda e: order.get(e.get("role", ""), 9))
        seen: dict[str, None] = {}
        for entity in ranked:
            name = str(entity.get("name") or "").strip()
            if name:
                seen.setdefault(name, None)
        return list(seen)

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


def _enum(name: str) -> list[str]:
    return SCHEMA["properties"][name]["enum"]


# The model returns the right idea in the wrong words: "metadata", "query",
# "count", "record_list", "question". Rejecting those threw away 9 of 12
# otherwise-correct extractions, so they are mapped rather than discarded.
_REQUEST_TYPE_ALIASES = {
    "DATA": "DATA", "RECORD": "DATA", "RECORDS": "DATA", "QUERY": "DATA",
    "COUNT": "DATA", "RECORD_LIST": "DATA", "AGGREGATE": "DATA",
    "METADATA": "METADATA", "SCHEMA": "METADATA", "CONFIG": "METADATA",
    "CONFIGURATION": "METADATA", "QUESTION": "METADATA",
    "MIXED": "MIXED", "BOTH": "MIXED", "HYBRID": "MIXED",
}


def _request_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return _REQUEST_TYPE_ALIASES.get(value.strip().upper().replace(" ", "_"))


def _validate(payload: Any) -> Extraction | None:
    """Accept only what the schema allows. A near-miss is a miss.

    Guided decoding should make this redundant, but it runs anyway: the
    fallback modes below are not grammar-constrained, and a value that reaches
    routing unvalidated is worse than no extraction at all.
    """
    if not isinstance(payload, dict):
        return None
    request_type = _request_type(payload.get("request_type"))
    if request_type is None:
        return None
    for key in ("intent", "action"):
        if payload.get(key) not in _enum(key):
            return None

    roles = SCHEMA["properties"]["business_entities"]["items"]["properties"]["role"]["enum"]
    entities = []
    for item in payload.get("business_entities") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        role = item.get("role")
        if name and role in roles:
            entities.append({"name": name[:60], "role": role})

    operators = SCHEMA["properties"]["filters"]["items"]["properties"]["operator"]["enum"]
    filters = [
        {"concept": str(f.get("concept") or "")[:60],
         "operator": f.get("operator"), "value": f.get("value")}
        for f in (payload.get("filters") or [])
        if isinstance(f, dict) and f.get("operator") in operators and f.get("concept")
    ]

    attributes = [
        {"entity": str(a.get("entity") or "")[:60],
         "attribute": str(a.get("attribute") or "")[:60]}
        for a in (payload.get("requested_attributes") or [])
        if isinstance(a, dict) and a.get("entity") and a.get("attribute")
        # "all", "records", "details" are how much to show, not what to show.
        # The prompt says so; this holds even when the model forgets.
        and _normal(a.get("attribute")) not in SCOPE_WORDS
    ]

    temporal = _temporal(payload.get("temporal"))
    # A filter whose concept is only a unit of time -- "month", "date" -- and
    # whose value is a period is a date restriction the model filed in the
    # wrong slot. Moved, not dropped: the restriction is real.
    kept = []
    for spec in filters:
        if (temporal is None and _normal(spec["concept"]) in TIME_UNIT_WORDS
                and isinstance(spec.get("value"), str) and spec["value"].strip()):
            temporal = {"expression": spec["value"].strip()[:60], "concept": None}
            continue
        kept.append(spec)
    filters = kept

    # "currently", "now", "in total" name no period; a range built from them
    # would quietly restrict to today.
    if temporal and _normal(temporal["expression"]) in OPEN_ENDED_WORDS:
        temporal = None
    # The same period written twice -- once as temporal, once as a date filter
    # ("created date between May 2026") -- is ONE restriction. Keeping both
    # would hand the linker a filter whose value is not a date at all.
    if temporal:
        expression = _normal(temporal["expression"])
        filters = [f for f in filters
                   if _normal(f.get("value")) != expression]
    # A generic concept whose value only repeats the entity's own name
    # ("internal interviews" + type = internal) is not a restriction: the
    # entity already is one. Measured live on a question that must keep
    # returning the plain count.
    entity_tokens = {t for e in entities for t in _normal(e["name"]).split()}
    filters = [f for f in filters
               if not (_normal(f["concept"]) in GENERIC_CONCEPTS
                       and isinstance(f.get("value"), str)
                       and set(_normal(f["value"]).split()) <= entity_tokens)]

    return_mode = payload.get("return_mode")
    if return_mode not in RETURN_MODES:
        return_mode = None

    # All three routing flags missing sends the question down route NONE and
    # nothing runs. Seen live on a DATA question. The request type already
    # says what the question needs, so the flags are derived from it rather
    # than left to mean "do nothing".
    flags = {key: payload.get(key) for key in (
        "requires_schema_discovery", "requires_record_query",
        "requires_metadata_context")}
    if all(value is None for value in flags.values()):
        flags = {
            "requires_schema_discovery": True,
            "requires_record_query": request_type in ("DATA", "MIXED"),
            "requires_metadata_context": request_type in ("METADATA", "MIXED"),
        }

    allowed_types = SCHEMA["properties"]["metadata_types"]["items"]["enum"]
    metadata_types = [t for t in (payload.get("metadata_types") or [])
                      if t in allowed_types]

    return Extraction(
        request_type=request_type,
        intent=payload["intent"],
        action=payload["action"],
        business_entities=entities[:6],
        filters=filters[:6],
        requested_attributes=attributes[:6],
        metadata_types=metadata_types[:5],
        requires_schema_discovery=flags["requires_schema_discovery"],
        requires_record_query=flags["requires_record_query"],
        requires_metadata_context=flags["requires_metadata_context"],
        temporal=temporal,
        return_mode=return_mode,
    )


# Words that say how many records to show, never which property of them.
SCOPE_WORDS = frozenset({
    "all", "list", "show", "give", "give me", "records", "record", "details",
    "detail", "every", "each", "everything", "data", "info", "information",
    "entries", "rows", "list out"})
# Units of time. As a filter concept on their own they mean "a date", and the
# value is the period.
TIME_UNIT_WORDS = frozenset({
    "month", "months", "year", "years", "week", "weeks", "day", "days", "date",
    "dates", "period", "time", "quarter"})
RETURN_MODES = ("records", "count", "aggregate", "single_record", "exists")
OPEN_ENDED_WORDS = frozenset({
    "currently", "current", "now", "right now", "so far", "till now",
    "until now", "to date", "in total", "total", "overall", "at present",
    "presently", "as of now", "all time", "ever"})
# Concepts that only say "a kind of"; a value that repeats the entity name
# under one of these restricts nothing.
GENERIC_CONCEPTS = frozenset({"type", "kind", "category", "record type", "class"})


def _normal(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _temporal(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    expression = str(value.get("expression") or "").strip()
    if not expression:
        return None
    concept = value.get("concept")
    concept = str(concept).strip()[:60] if concept else None
    if concept and _normal(concept) in TIME_UNIT_WORDS:
        # "month" as the concept says nothing about WHICH date field.
        concept = None
    return {"expression": expression[:60], "concept": concept}


def _bodies(model: str, question: str, max_tokens: int) -> list[tuple[str, dict[str, Any]]]:
    """Decoding modes, most ACCURATE first -- which is not the most constrained.

    json_object leads because it measured best: 11/12 intent against 1/12 for
    json_schema on the same model and cases. Grammar-constrained decoding does
    make an invalid enum unreachable, but it also collapses the answer toward
    one value, so it is kept only as a fallback for a server that rejects
    json_object.

    Thinking is disabled on every call. The main model is a reasoning model and
    otherwise spends the whole budget thinking: 320 tokens of reasoning,
    finish_reason "length", content None, 7.9 s and no answer. Off, the same
    question takes 610 ms. Classification needs no reasoning.
    """
    base = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": question}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    return [
        ("json_object", {**base, "response_format": {"type": "json_object"}}),
        ("json_schema", {**base, "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "intent", "schema": SCHEMA, "strict": True}}}),
        ("guided_json", {**base, "guided_json": SCHEMA}),
    ]


def safe_endpoint(url: str) -> str:
    """An endpoint identifier safe to store beside a trace.

    Scheme, host and port only. Credentials in a URL's userinfo, a token in a
    query string and any path beyond the API root are dropped -- a trace is
    read by more people than a configuration file is.
    """
    try:
        from urllib.parse import urlsplit
        parts = urlsplit(url)
    except Exception:                                   # noqa: BLE001
        return ""
    if not parts.scheme or not parts.hostname:
        return str(url).split("?", 1)[0]
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def extract(question: str, *, endpoint: str = DEFAULT_ENDPOINT,
            model: str = DEFAULT_MODEL, timeout: float = DEFAULT_TIMEOUT,
            max_tokens: int = 600) -> Extraction | None:
    """The question's intent, or None if it could not be read.

    None is a normal outcome, not an error: the caller falls back to the
    lexical path. Extraction improves discovery and must never be able to
    break it, so an endpoint that is down, slow or confused costs precision
    rather than an answer.
    """
    try:
        import httpx
    except ImportError as exc:
        raise ExtractError("extraction needs httpx") from exc

    url = f"{endpoint.rstrip('/')}/chat/completions"
    started = time.perf_counter()
    with httpx.Client() as client:
        for mode, body in _bodies(model, question, max_tokens):
            try:
                response = client.post(url, json=body, timeout=timeout)
            except Exception:
                return None
            if response.status_code != 200:
                # A server that rejects json_schema should still get a chance
                # at guided_json; anything else is not worth retrying.
                if response.status_code in (400, 404, 422):
                    continue
                return None
            try:
                payload = response.json()
                content = payload["choices"][0]["message"]["content"]
                usage = payload.get("usage") or {}
            except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                continue
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                continue
            result = _validate(parsed)
            if result is None:
                continue
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            result.mode = mode
            result.completion_tokens = int(usage.get("completion_tokens") or 0)
            result.model = model
            result.endpoint = safe_endpoint(endpoint)
            return result
    return None
