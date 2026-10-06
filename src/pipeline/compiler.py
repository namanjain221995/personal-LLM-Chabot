"""Stages 1 and 2: the main model reads a question into the canonical IR and
routes it to the information sources that can answer it -- one call, traced as
two stages (spec §42).

The model is the semantic authority on MEANING: which words are entities, which
are values, which are operations. It never names a Salesforce API identifier
and never writes SQL -- it answers in the user's own business words, and
grounding maps those words to verified schema later.

The prompt's examples are deliberately about a made-up domain (projects,
tasks, people). They teach the roles; they must not teach the answers to the
org's own test questions.

One structured retry when the first answer is unusable, then give up: a
question the model cannot read is declined, never guessed.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from .ir import SemanticIR, from_dict

log = logging.getLogger(__name__)

SYSTEM = """Read ONE question about a Salesforce org and return its meaning as JSON.

Use the user's own business words for every concept. NEVER write Salesforce API
names, SQL, or calendar arithmetic. Output minified JSON only. OMIT every key
whose value would be empty, null or false -- write only what the question
actually contains. Available keys:

family: record | schema | metadata | operational | history | search | text |
        prediction | business_definition | none
output_mode: records | single_record | count | value | grouped | ranking |
        comparison | percentage | ratio | trend | exists | summary | duplicate_groups |
        schema_facts | metadata_facts | operational_facts | history_timeline |
        search_results | answer
entities: [{ref:"e0", concept, role: primary|related|returned}]
   An entity is a KIND of record (interview, candidate, project). A specific
   person, company or record is a VALUE in a filter, never an entity.
   primary = the records the query runs over (counted, filtered, listed).
   returned = the kind of record the answer is ABOUT when it differs.
   Keep EVERY kind of record the question walks through: "the person who
   manages the project linked to task TK-1" has entities task, project
   (related) and person (returned). Never write how entities are linked as
   a filter -- links come from the schema.
attributes: [{entity, concept}]   properties to SHOW only
filters: [{entity, concept, operator, right, group}]
   entity = whose attribute it is. "tasks for person Ann Lee" -> the filter
   {entity:<person>, concept:"name", right:{type:"literal", value:"Ann Lee"}},
   NOT a filter on the task.
   right: {type:"literal", value} | {type:"boolean", value:true|false}
        | {type:"field_reference", entity, concept}  (compare two fields:
          "tasks whose due date is before their start date")
        | {type:"set", value:[...]} | {type:"null"}
   operators: equals not_equals greater_than greater_than_or_equal less_than
        less_than_or_equal contains not_contains starts_with in not_in
        is_null is_not_null
   Yes/no qualifiers are booleans: "tasks marked urgent" -> concept
   "urgent", boolean true. Keep the whole qualifier as the concept: "people
   active for night shifts" is ONE filter, concept "active for night shifts",
   boolean true -- not a second entity "night shift".
   A record number or code (TK-00042, INV-1003) identifies one record: filter
   concept "name" on that record's entity.
   A value the user named for a status/type/stage stays a literal value
   ("Completed", "Offer Received") with a concept such as "status".
   group: filters with the same group are ANDed, different groups are ORed.
measures: [{op: count|count_distinct|sum|avg|min|max, entity, concept}]
   "how many X" -> count of X (concept null). "average cost" -> avg of
   "cost". "How many unique people had tasks" -> primary is TASK (the
   records scanned), measure count_distinct entity <task> concept "person".
   Operation words (count, number, total, average, most, top) are NEVER
   concepts.
dimensions: [{entity, concept, grain}]  "by status", "per person", "monthly"
   grain: day|week|month|quarter|year for date dimensions, else null.
   Duplicate detection uses output_mode duplicate_groups and puts every
   duplicate key in dimensions. "duplicate email" -> dimension "email";
   "duplicate first and last name combinations" -> two dimensions. Add a
   count measure and duplicate_threshold 1 (COUNT > 1) unless the user gives
   another threshold. Never treat "duplicate" as a field concept.
ordering: [{target:"measure:0"|"date"|"attribute", entity, concept, direction}]
   "top 5 people by tasks" -> dimension person, measure count of tasks,
   ordering measure:0 desc, limit 5. "latest task" -> ordering target
   "date" desc, limit 1 -- latest/earliest/first/last are ORDERINGS.
limit: number or null
temporal: [{expression, entity, concept, purpose: filter|window}]
   The date words exactly as the user wrote them ("last six months",
   "May 2026", "this month"). concept = which date if said ("created",
   "due"), else null. "currently", "now", "in total" -> no temporal.
comparison: [{label, filters, temporal:{expression, concept}}]
   One segment per side: "this month vs last month", "design vs build".
derived: [{kind: percentage|ratio|difference|growth, numerator:[filters],
   denominator:[filters]}]  "what percentage of tasks are done" ->
   percentage, numerator filter status=done, denominator [] (all). The
   numerator's filters go ONLY in numerator, never also in filters.
existence: [{mode: exists|not_exists, entity, filters}]
   "people with no tasks" -> primary person, related task,
   existence not_exists entity <task>.
schema: [{kind, object_concept, field_concept, other_object_concept}]
   kind: object_for_concept | fields_of_object | field_for_concept |
   field_datatype | picklist_values | lookup_target | objects_referencing |
   record_types | required_fields | standard_or_custom |
   relationship_between | describe_object
   For questions about the org's STRUCTURE, not its records: objects,
   fields, data types, picklist values, record types, required fields and
   relationships are all family schema (never metadata). Put the whole
   field phrase in field_concept ("client bill rate"), object_concept only
   when the question names the object.
search_value: a value to find across many kinds of record, else null
follow_up: true when the question refers to a previous answer ("those",
   "the ones you just showed", "break them down"). Then return the COMPLETE
   meaning of the current question: carry over from the previous meaning every
   entity, filter, period and measure the user did not change, and replace
   only what they changed. "Break them down by recruiter" after "completed
   interviews" keeps entity interview and filter status Completed, and adds
   dimension recruiter with a count measure.
sources: REQUIRED unless family is none. EVERY information source the answer
   needs (routing):
   RECORD_DATA        values stored on records (counts, lists, sums, lookups)
   RUNTIME_SCHEMA     objects, fields, data types, picklist values,
                      relationships, record types, required fields
   METADATA_CONTEXT   flows, triggers, Apex, validation rules, page layouts,
                      permissions, approval processes
   BUSINESS_KNOWLEDGE what a business term means in this org
   OPERATIONAL_CONTEXT when data was last synchronised or refreshed
   HISTORY_CONTEXT    who changed a field, previous values, change timelines
   TEXT_ANALYSIS      themes, concerns or sentiment across free-text fields
                      (with RECORD_DATA)
   CONVERSATION_CONTEXT the question builds on the previous answer (add the
                      sources the restated question needs too)
   "Which field stores the interview status and how many are Completed?" ->
   ["RUNTIME_SCHEMA","RECORD_DATA"]. Small talk -> family none, no sources.
duplicate_threshold: for duplicate_groups only; 1 means values occurring more
   than once. Use the user's stated threshold when present.

Scope words (all, list, show, give me, details, overview, information) set
output_mode; they are never attributes. family is metadata ONLY for the org's
automation and configuration (flows, triggers, validation rules, page
layouts, permissions, Apex); a question about values stored on records
("maximum daily hours of people") is record. When the question asks when data was
last synchronised/refreshed, family is operational. When it asks who changed
a field or its previous values, family is history."""

EXAMPLE = ('Example -- "Which 3 people own the most overdue tasks this quarter?" -> '
           '{"family":"record","output_mode":"ranking","sources":["RECORD_DATA"],'
           '"entities":[{"ref":"e0",'
           '"concept":"task","role":"primary"},{"ref":"e1","concept":"person",'
           '"role":"returned"}],"filters":[{"entity":"e0","concept":"overdue",'
           '"operator":"equals","right":{"type":"boolean","value":true}}],'
           '"measures":[{"op":"count","entity":"e0","concept":null}],'
           '"dimensions":[{"entity":"e1","concept":"name","grain":null}],'
           '"ordering":[{"target":"measure:0","direction":"desc"}],"limit":3,'
           '"temporal":[{"expression":"this quarter","entity":"e0","concept":null,'
           '"purpose":"filter"}]}')

SCOPE_WORDS = {"all", "list", "show", "give me", "records", "record", "details",
               "detail", "everything", "data", "info", "information", "overview",
               "entries", "rows"}
OPERATION_WORDS = {"count", "number", "total", "sum", "average", "avg", "mean",
                   "most", "least", "top", "bottom", "maximum", "minimum", "max",
                   "min", "percentage", "percent", "ratio", "trend", "number of",
                   "duplicate", "duplicates", "duplicated"}
TIME_UNITS = {"month", "months", "year", "years", "week", "weeks", "day", "days",
              "date", "dates", "quarter", "period", "time"}
OPEN_ENDED = {"currently", "current", "now", "right now", "so far", "till now",
              "until now", "to date", "in total", "total", "overall", "at present",
              "presently", "as of now", "all time", "ever"}


def _normal(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def clean(ir: SemanticIR) -> SemanticIR:
    """Deterministic guards on the model's reading. Each fixes a slip seen live.

    Guards remove or move; they never add meaning the model did not state.
    """
    ir.attributes = [a for a in ir.attributes if _normal(a["concept"]) not in SCOPE_WORDS]
    for measure in ir.measures:
        if measure.concept and _normal(measure.concept) in OPERATION_WORDS:
            measure.concept = None          # "count" is the operation, not a field
    for dimension in list(ir.dimensions):
        if _normal(dimension.concept) in OPERATION_WORDS:
            ir.dimensions.remove(dimension)
    kept = []
    for f in ir.filters:
        if _normal(f.concept) in TIME_UNITS and f.right.type == "literal" \
                and isinstance(f.right.value, str):
            from .ir import Temporal
            ir.temporal.append(Temporal(expression=f.right.value, entity=f.entity))
            continue
        if _normal(f.concept) in ("type", "kind", "category") and f.right.type == "literal" \
                and isinstance(f.right.value, str):
            entity = ir.entity(f.entity)
            if entity and set(_normal(f.right.value).split()) <= set(_normal(entity.concept).split()):
                continue                    # "internal interviews" + type=internal
        kept.append(f)
    ir.filters = kept
    # A percentage's numerator filters restrict ONLY the numerator. Left at
    # top level too they restrict the denominator, and every percentage reads
    # 100%. Seen live.
    derived_keys = {(f.entity, _normal(f.concept), _normal(f.right.value))
                    for d in ir.derived for f in d.numerator}
    if derived_keys:
        ir.filters = [f for f in ir.filters
                      if (f.entity, _normal(f.concept), _normal(f.right.value))
                      not in derived_keys]
    # A literal shaped like a record number (BCN-00028, JS-00014) is the
    # record's Name, whatever concept the model called it.
    import re
    for f in ir.filters:
        if f.right.type == "literal" and isinstance(f.right.value, str) \
                and re.fullmatch(r"[A-Za-z]{1,6}-\d{2,}", f.right.value.strip()):
            f.concept = "name"
    # "Show X by status": a grouping with nothing measured is a count per
    # group. Left without a measure it became a plain record listing.
    if ir.dimensions and not ir.measures and ir.primary is not None:
        from .ir import Measure
        ir.measures.append(Measure(op="count", entity=ir.primary.ref))
        if ir.output_mode in ("records", "summary", "answer"):
            ir.output_mode = "grouped"
    # The duplicate operation is field- and object-independent: group by the
    # grounded keys, count records, then apply the threshold in SQL.
    if ir.output_mode == "duplicate_groups" and ir.primary is not None:
        from .ir import Measure, Ordering
        if not ir.measures:
            ir.measures.append(Measure(op="count", entity=ir.primary.ref,
                                       alias="duplicate_count"))
        if not ir.ordering:
            ir.ordering.append(Ordering(target="measure:0", descending=True))
    ir.temporal = [t for t in ir.temporal if _normal(t.expression) not in OPEN_ENDED]
    seen, unique = set(), []
    for t in ir.temporal:                   # one period stated twice is one
        key = (_normal(t.expression), t.purpose)
        if key not in seen:
            seen.add(key)
            unique.append(t)
    ir.temporal = unique
    if ir.temporal:                          # and never also as a literal filter
        periods = {_normal(t.expression) for t in ir.temporal}
        ir.filters = [f for f in ir.filters
                      if not (f.right.type == "literal" and _normal(f.right.value) in periods)]
    return ir


AUTOMATION = re.compile(r"\b(flows?|apex|triggers?|validation rules?|workflow rules?|"
                        r"process builders?|page layouts?|permission sets?|"
                        r"approval process(?:es)?|automations?)\b", re.IGNORECASE)


def lexical_features(question: str) -> list[str]:
    """Words code can spot. Evidence for the model, never a decision (§7)."""
    found = sorted({m.group(0).lower() for m in AUTOMATION.finditer(question or "")})
    if found:
        return [f"platform automation/configuration words present: {', '.join(found)}"]
    return []


def _valid(ir: SemanticIR) -> str:
    """Why this IR is unusable, or "" when it is usable. Fails the stage."""
    if ir.family == "none":
        return ""
    if not ir.sources:
        return ("sources is missing: name every information source the answer "
                "needs, e.g. [\"RECORD_DATA\"]")
    if ir.family in ("record", "history", "text") and not ir.entities:
        return "a record question named no entity"
    if ir.family == "schema" and not ir.schema and not ir.entities:
        return "a schema question named nothing to inspect"
    if "RUNTIME_SCHEMA" in ir.sources and not ir.schema and "RECORD_DATA" not in ir.sources:
        return ("RUNTIME_SCHEMA was named but schema is empty: say what to look up "
                "(schema[].kind, object_concept, field_concept)")
    return ""


def _shape(ir: SemanticIR) -> str:
    """A shape problem worth one retry. Never fails the stage: the planning
    stage decides the final shape with verified fields in view."""
    if ir.family == "metadata" and (ir.measures or ir.derived or ir.filters):
        return ("family metadata is only automation/configuration (flows, triggers, "
                "validation rules); counts or record filters make it record or schema")
    # An output mode whose defining part is missing would silently become a
    # record listing. Seen live: "what percentage of interviews have Completed
    # status" came back with no numerator at all.
    if ir.output_mode in ("percentage", "ratio") and not any(
            d.numerator for d in ir.derived if d.kind in ("percentage", "ratio")):
        return ("a percentage needs derived[0].numerator: the filters that select "
                "the part, e.g. status equals the value the user named")
    if ir.output_mode == "comparison" and len(ir.comparison) < 2:
        return "a comparison needs two comparison segments, one per side"
    if ir.output_mode == "duplicate_groups":
        if not ir.dimensions:
            return "duplicate detection needs at least one key in dimensions"
        if not ir.measures or ir.measures[0].op != "count":
            return "duplicate detection needs a count measure"
    return ""


@dataclass
class CompileResult:
    ir: SemanticIR | None
    error: str = ""
    failure: str = ""                   # "" | unavailable | invalid


class IntentCompiler:
    def __init__(self, endpoint: str, model: str, *, timeout: float = 60.0,
                 max_tokens: int = 1200, client: Any = None) -> None:
        from .stage_model import StageModel
        self.endpoint = endpoint
        self.model = model
        self.max_tokens = max_tokens
        self.stage = StageModel(endpoint, model, timeout=timeout, client=client)

    @property
    def client(self) -> Any:
        return self.stage.client

    @client.setter
    def client(self, value: Any) -> None:
        self.stage.client = value

    def compile(self, question: str, *, previous: dict[str, Any] | None = None,
                log_to: Any = None) -> CompileResult:
        from .stage_model import StageLog
        log_to = log_to if log_to is not None else StageLog()
        started = time.perf_counter()
        user = question
        if previous:
            # The previous question's MEANING, not its prose. The model decides
            # what a follow-up inherits and restates the whole meaning (§30).
            user = (f"Previous question: {previous.get('question')}\n"
                    f"Its meaning: {json.dumps(previous.get('ir_summary'), ensure_ascii=False)}\n"
                    f"Current question: {question}")
        hints = lexical_features(question)
        if hints:
            user += "\nLexical features (evidence only): " + "; ".join(hints)
        messages = [{"role": "system", "content": SYSTEM + "\n\n" + EXAMPLE},
                    {"role": "user", "content": user}]

        def read(payload: Any) -> SemanticIR:
            return clean(from_dict(payload, question))

        payload, call = self.stage.call(
            "intent", messages, log_to=log_to, max_tokens=self.max_tokens,
            validate=lambda p: _valid(read(p)), soft=lambda p: _shape(read(p)) or "")
        ir = read(payload) if payload is not None else None
        log_to.combined("routing", call, selected=ir.sources if ir else None)
        if ir is None:
            return CompileResult(None, call.error or "no usable answer", call.failure)
        call.selected = {"family": ir.family, "output_mode": ir.output_mode,
                         "entities": [e.concept for e in ir.entities]}
        ir.model, ir.endpoint = self.model, call.endpoint
        ir.mode = "json_object"
        ir.retried = bool(call.retries)
        ir.duration_ms = int((time.perf_counter() - started) * 1000)
        ir.completion_tokens = call.completion_tokens
        return CompileResult(ir)


_HTTP = None


def _client():
    """One pooled HTTP client for the process (§50): no socket per question."""
    global _HTTP
    if _HTTP is None:
        import httpx
        _HTTP = httpx.Client(limits=httpx.Limits(max_keepalive_connections=8,
                                                 max_connections=16))
    return _HTTP


def _safe(url: str) -> str:
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme and parts.hostname:
        return f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    return url.split("?", 1)[0]
