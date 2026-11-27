"""The single place that defines every tool the models can call: name, description, input schema and handler.

Three tool sets (``docs/tools.md`` lists them; ``scripts/gen_tools_doc.py`` writes it from this module and
``tests/test_tool_registry.py`` fails when it is not current):

* **knowledge, one object** - :func:`knowledge_tool_defs`: 15 tools over one object-knowledge file
  (:class:`~sf_assistant.tools.knowledge_tools.ToolRegistry`);
* **knowledge, several objects** - :func:`multi_tool_defs` (3 tools of its own) and :func:`multi_object_tool` (the 15
  per-object tools with a required ``object`` argument) (:class:`~sf_assistant.tools.knowledge_tools.MultiToolRegistry`);
* **records** - :data:`RECORD_TOOLS`: the 14 record tools in :data:`TOOL_ORDER` with their :data:`SCHEMAS`, and
  :data:`TOOL_ALIASES` (names the model does not see) (:class:`~sf_assistant.tools.record_tools.RecordTools`);
* **flow components** - :func:`component_tool_defs`: the 3 component tools (search_components, get_component,
  find_usages) over ``knowledge/components`` (:class:`~sf_assistant.tools.component_tools.ComponentTools`), which the
  several-object registry appends to its tools when the components are loaded.

A :class:`ToolDef` names its handler as a method of the registry that owns it; :meth:`ToolDef.bind` makes the
:class:`Tool` that registry keeps. The registries build their tools from here and keep their public behaviour
(``tools``, ``list_tools()``, ``openai_tools()``, ``call()``). The definitions were moved here unchanged from
``okeval/tools.py``, ``okeval/multi.py`` and ``okrec/tools.py``; :func:`mcp_tool_list` and :func:`openai_tool_list` give
the same payload shapes as the registries' own methods.

This module imports nothing from the engine (no duckdb), so docs and servers can read every definition without a
warehouse.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from sf_assistant.config import LIMITS   # list_max_rows: the list_records page (development A of 2026-11-27)

__all__ = ["Tool", "ToolDef", "knowledge_tool_defs", "multi_tool_defs", "multi_object_tool", "RECORD_TOOLS",
           "RECORD_HANDLERS", "TOOL_ORDER", "TOOL_ALIASES", "SCHEMAS", "mcp_tool_list", "openai_tool_list",
           "knowledge_tool_list", "multi_tool_list", "record_tool_list", "tools_markdown", "component_tool_defs",
           "component_tool_list", "COMPONENT_KINDS", "COMPONENT_DETAILS", "USAGE_ROLES"]


@dataclass
class Tool:
    """One MCP-style tool."""

    name: str
    description: str
    input_schema: dict  # JSON Schema, MCP "inputSchema" shape
    handler: Callable[..., dict]


@dataclass(frozen=True)
class ToolDef:
    """One tool definition: ``handler`` is the name of the method of the owning registry that runs it."""

    name: str
    description: str
    input_schema: dict
    handler: str

    def bind(self, owner: Any) -> Tool:
        """The :class:`Tool` of ``owner`` (its method ``handler``, looked up now, as the registries did)."""
        return Tool(name=self.name, description=self.description, input_schema=self.input_schema,
                    handler=getattr(owner, self.handler))


def mcp_tool_list(tools: Any) -> list[dict]:
    """MCP ``tools/list`` shape ``[{name, description, inputSchema}]`` of :class:`Tool` or :class:`ToolDef` objects."""
    return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in tools]


def openai_tool_list(tools: Any) -> list[dict]:
    """OpenAI chat-completions ``tools`` payload of :class:`Tool` or :class:`ToolDef` objects."""
    return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                              "parameters": t.input_schema}} for t in tools]


# ================================================================================ knowledge tools, one object

def _schema(properties: dict | None = None, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


def knowledge_tool_defs(obj: str, sections: list[str]) -> list[ToolDef]:
    """The 15 knowledge tools of one object (:class:`~sf_assistant.tools.knowledge_tools.ToolRegistry`), in order.

    ``obj`` is the object API name the descriptions name; ``sections`` the knowledge file's section ids
    (``KnowledgeIndex.sections()``), listed in the ``search_knowledge`` description and enum. Handlers are
    ``ToolRegistry`` methods. Moved from ``ToolRegistry._register_all`` (okeval/tools.py) unchanged.
    """
    defs: list[ToolDef] = []
    str_prop = lambda d: {"type": "string", "description": d}  # noqa: E731
    k_prop = {"type": "integer", "description": "Max results (1-25, default 8).", "minimum": 1, "maximum": 25, "default": 8}

    defs.append(ToolDef(
        "describe_object",
        f"Overview of {obj}: label, description, business domain, business semantics/concepts, record "
        "grain (what one record represents), discovery summary, section counts, record types and "
        "field dependencies. Start here for 'what is this object' questions.",
        _schema(),
        "_describe_object",
    ))
    defs.append(ToolDef(
        "list_fields",
        f"List {obj} fields (api_name, label, type, semantic concept, roles, reference targets, "
        "legacy / api_name_typo flags). Optional filters: type (e.g. picklist, reference, date, "
        "datetime, number, boolean, text) and role (substring of a semantic role or concept, e.g. "
        "score, legacy, feedback, temporal, outcome, schedule_mirror).",
        _schema({"type": str_prop("Field type filter."), "role": str_prop("Semantic role/concept substring filter.")}),
        "_list_fields",
    ))
    defs.append(ToolDef(
        "get_field",
        f"Everything about one {obj} field, by API name or label (case-insensitive, typo-tolerant "
        "suggestions): type, business meaning, semantic concept/roles, picklist values with meanings, "
        "reference targets and relationship semantics, which flows/apex read or write it, which "
        "validation rules involve it, legacy/typo flags, and which attributes are unspecified.",
        _schema({"name": str_prop("Field API name or label, e.g. Status__c or 'Date Of Interview'.")}, ["name"]),
        "_get_field",
    ))
    defs.append(ToolDef(
        "search_fields",
        f"Find {obj} fields matching a business description or concept (keyword search over field "
        "labels, meanings, concepts, roles and picklist values). Use when you do not know the API name.",
        _schema({"query": str_prop("Business words, e.g. 'who conducts the interview'."), "k": k_prop}, ["query"]),
        "_search_fields",
    ))
    defs.append(ToolDef(
        "get_picklist_values",
        "Allowed values of a picklist or multi-select picklist field, with value labels and business "
        "meanings when present. Tells you plainly when the field is not a picklist.",
        _schema({"field": str_prop("Field API name or label.")}, ["field"]),
        "_get_picklist_values",
    ))
    defs.append(ToolDef(
        "get_relationships",
        f"All lookup/parent relationships from {obj}: field, label, target objects, cardinality, "
        "business role, business meaning and direction semantics.",
        _schema(),
        "_get_relationships",
    ))
    defs.append(ToolDef(
        "get_child_relationships",
        f"All child objects that reference {obj} (child object, its lookup field, business role, "
        "meaning, direction semantics).",
        _schema(),
        "_get_child_relationships",
    ))
    defs.append(ToolDef(
        "find_relationship",
        "Find the relationship (parent lookup or child object) that represents a business concept such "
        "as 'interviewer', 'candidate', 'previous interview', 'attendees' or 'session'. Matches business "
        "role, field label, direction semantics and target object names.",
        _schema({"concept": str_prop("Business concept or role words.")}, ["concept"]),
        "_find_relationship",
    ))
    defs.append(ToolDef(
        "get_temporal_semantics",
        "Audit timestamps/users (created_at, updated_at, created_by, last_updated_by -> system fields) "
        "and every date/datetime/time-related field with its meaning and legacy flag.",
        _schema(),
        "_get_temporal_semantics",
    ))
    defs.append(ToolDef(
        "get_validation_rules",
        "Validation rules (name, active, meaning, formula, fields involved, condition and error "
        "meaning). Optional field filter.",
        _schema({"field": str_prop("Only rules involving this field (API name or label).")}),
        "_get_validation_rules",
    ))
    defs.append(ToolDef(
        "get_automation",
        "Flows and Apex that act on this object: purpose, type, trigger (object + events), fields read, "
        "fields written, related objects and business process. Filters: kind ('flow' or 'apex'), field "
        "(only automation that reads or writes it), process_id (process id or name).",
        _schema(
            {
                "kind": {"type": "string", "enum": ["flow", "apex"], "description": "Automation kind."},
                "field": str_prop("Field API name or label."),
                "process_id": str_prop("Business process id or name."),
            }
        ),
        "_get_automation",
    ))
    defs.append(ToolDef(
        "get_business_processes",
        f"Business processes {obj} takes part in: process id, name, the object's role, meaning, the "
        "flows/apex implementing each and the fields they write.",
        _schema(),
        "_get_business_processes",
    ))
    defs.append(ToolDef(
        "search_knowledge",
        "Keyword (BM25) search across the whole knowledge file; returns the best matching knowledge "
        "chunks as text. Use for anything the specific tools do not cover. Optional section filter: "
        + ", ".join(sections)
        + ".",
        _schema(
            {
                "query": str_prop("Search words."),
                "k": k_prop,
                "section": {"type": "string", "enum": sections, "description": "Restrict to one section."},
            },
            ["query"],
        ),
        "_search_knowledge",
    ))
    defs.append(ToolDef(
        "get_provenance",
        "Where this knowledge came from: source environment, sources with knowledge type and confidence, "
        "review status, and caveats about what must be verified elsewhere.",
        _schema(),
        "_get_provenance",
    ))
    defs.append(ToolDef(
        "get_record_types",
        f"Record types defined for {obj} in the knowledge file (says explicitly when there are none).",
        _schema(),
        "_get_record_types",
    ))
    return defs


# ============================================================================== knowledge tools, several objects

def multi_tool_defs(names: list[str]) -> list[ToolDef]:
    """The 3 tools of :class:`~sf_assistant.tools.knowledge_tools.MultiToolRegistry` itself, in order (``names``: the
    loaded object API names, sorted). Moved from ``MultiToolRegistry._build_tools`` (okeval/multi.py) unchanged."""
    return [
        ToolDef(
            "list_objects",
            "List every Salesforce object that has a knowledge file, with its one-line summary and key concepts. Use it to pick the object a question is about.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            "_list_objects",
        ),
        ToolDef(
            "search_all_knowledge",
            "Full-text search across the knowledge of ALL objects (fields, picklists, relationships, flows, Apex, validation rules, business processes, provenance). Each result names its object. Use it first when the object is unclear.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Plain-language words or API names to search for."},
                    "k": {"type": "integer", "description": "Maximum results (default 8).", "minimum": 1, "maximum": 20},
                    "object": {"type": "string", "enum": names, "description": "Optional: restrict to one object."},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            "_search_all",
        ),
        ToolDef(
            "find_links_to_object",
            "Find every lookup / master-detail field, across ALL loaded objects, that points at the given target object (which may or may not have its own knowledge file). Use it for 'which objects link to / reference / look up X'.",
            {
                "type": "object",
                "properties": {"target": {"type": "string", "description": "Exact API name of the target object, e.g. Interview__c or Account."}},
                "required": ["target"],
                "additionalProperties": False,
            },
            "_links_to",
        ),
    ]


def multi_object_tool(listed: dict, names: list[str]) -> ToolDef:
    """One per-object knowledge tool as :class:`~sf_assistant.tools.knowledge_tools.MultiToolRegistry` offers it:
    ``listed`` is an entry of ``ToolRegistry.list_tools()`` of the first loaded object (all registries share tool
    names and schemas), plus a required ``object`` argument (enum ``names``). The handler routes the call to that
    object's registry. Moved from ``MultiToolRegistry._build_tools`` unchanged.

    The description is the first object's, so it names that object (a known quirk, kept as it was).
    """
    schema = json.loads(json.dumps(listed["inputSchema"]))
    props = schema.setdefault("properties", {})
    props["object"] = {"type": "string", "enum": names, "description": "Object API name to query."}
    schema["required"] = ["object"] + [r for r in schema.get("required", []) if r != "object"]
    return ToolDef(listed["name"], f"{listed['description']} Set `object` to the object API name.", schema,
                   "_object_tool")


# ============================================================================== component tools
# (flow components, knowledge/components: stage 4 of the flow build, 2026-10-24; format review A02, A07, A17, A18)

#: The component kinds the tools take (``knowledge.components.KINDS``; flows only for now).
COMPONENT_KINDS = ["flow"]
#: The detail levels of get_component (summary: one result; the others in pages).
COMPONENT_DETAILS = ["summary", "effects", "logic", "fields", "caveats"]
#: find_usages roles.
USAGE_ROLES = ["reads", "writes", "triggered_on", "any"]


def component_tool_defs() -> list[ToolDef]:
    """The 3 component tools (:class:`~sf_assistant.tools.component_tools.ComponentTools`), in order. Compact on
    purpose (no object enum: code resolves every name, with did-you-means); :class:`~sf_assistant.tools.
    knowledge_tools.MultiToolRegistry` appends them to its tools when a components folder is loaded."""
    kind = {"type": "string", "enum": COMPONENT_KINDS}   # flow when left out
    return [
        ToolDef(
            "search_components",
            "Find flows by plain words: what is automated, what happens when a record is created or changed, "
            "schedules, reminders, notifications. One card per flow (what it does, when it runs, what it changes, "
            "state); about 5 fit. Then call get_component.",
            _schema({"query": {"type": "string", "description": "Plain words or a flow name."}, "kind": kind,
                     "k": {"type": "integer", "description": "Max cards (default 5).", "minimum": 1, "maximum": 10}},
                    ["query"]),
            "_search_components",
        ),
        ToolDef(
            "get_component",
            "One flow by API name, label, title or alias. detail: summary (default), effects (one line each), logic "
            "(effects, start, elements), fields (read or written, other writers), caveats (all, dev10 notes). All "
            "but summary are paged (next_page).",
            _schema({"name": {"type": "string", "description": "API name, label, title or alias."}, "kind": kind,
                     "detail": {"type": "string", "enum": COMPONENT_DETAILS},
                     "page": {"type": "integer", "minimum": 1}}, ["name"]),
            "_get_component",
        ),
        ToolDef(
            "find_usages",
            "Flows that read, write or start on an object or field (a row per flow, at most 40). With value: its "
            "writers, and those whose value is not fixed. Apex bodies and launch points are not scanned.",
            _schema({"target": {"type": "string", "description": "Object or Object.Field (API name or label)."},
                     "role": {"type": "string", "enum": USAGE_ROLES, "description": "Default any."},
                     "value": {"type": "string", "description": "Field target only."}},
                    ["target"]),
            "_find_usages",
        ),
    ]


# ============================================================================== record tools
# (moved verbatim from okrec/tools.py)

TOOL_ORDER = ("find_objects", "describe_fields", "get_field_values", "count_records", "aggregate_records",
              "group_records", "trend_records", "compare_records", "percentage_of_records", "list_records",
              "search_records", "about_people", "get_field_history", "get_sync_status")
#: Tool names the model does not see that ``call`` maps to a visible tool (RECORDS_SPEC §22.5): the old one-name
#: reference tool is an alias of ``about_people``.
TOOL_ALIASES = {"find_references": "about_people"}

# ------------------------------------------------------------------------------------- schemas (§9.1)
OPS = ["eq", "ne", "in", "not_in", "contains", "not_contains", "starts_with", "gt", "gte", "lt", "lte", "is_blank",
       "is_not_blank"]
SCALAR = {"type": ["string", "number", "boolean"]}
OBJECT = {"type": "string", "description": "Object API name, e.g. Interview__c (client interviews) or "
                                           "Internal_Interview__c (internal mock interviews)."}
FILTER_ITEM = {"type": "object", "properties": {
    "field": {"type": "string", "description": "Field API name (Status__c), a lookup compared with a name or Id "
                                               "(Host_User__c), or a lookup path (Program_Version__c.Program__c.Name)."},
    "op": {"type": "string", "enum": OPS},
    "value": {**SCALAR, "description": "Value for eq, ne, contains, not_contains, starts_with, gt, gte, lt, lte."},
    "values": {"type": "array", "items": SCALAR, "minItems": 1, "maxItems": 20, "description": "Values for in / not_in."},
    "other_field": {"type": "string", "description": "Compare with another field of the same record instead of a "
                                                     "value (eq, ne, gt, gte, lt, lte)."}},
    "required": ["field", "op"], "additionalProperties": False}
FILTER_ITEM_BRIEF = {"type": "object", "properties": {
    "field": {"type": "string"}, "op": {"type": "string", "enum": OPS}, "value": SCALAR,
    "values": {"type": "array", "items": SCALAR, "minItems": 1, "maxItems": 20}, "other_field": {"type": "string"}},
    "required": ["field", "op"], "additionalProperties": False}
FILTERS = {"type": "array", "maxItems": 8, "items": FILTER_ITEM,
           "description": "Conditions that must ALL hold (AND). 'A or B' on one field: op in. Text and picklist values "
                          "match ignoring case, spaces and hyphens; the stored spelling is used. A lookup (or "
                          "LookupField.Name) compared with a name matches the related record's Name. Never put a year, "
                          "month or date phrase here: use date_field + period."}
ANY_OF = {"type": "array", "minItems": 2, "maxItems": 5, "items": FILTER_ITEM_BRIEF,
          "description": "Optional OR group: a record must match AT LEAST ONE of these, and all of filters. Only for OR "
                         "across DIFFERENT fields; for one field use op in."}
DATE_FIELD = {"type": "string", "description": "Date or date/time field the period applies to: API name (CreatedDate, "
                                               "Date_of_Interview__c) or a temporal key from describe_fields "
                                               "(interview_date). Required when period is set."}
PERIOD = {"type": "string", "description": "Time window in words, resolved by code in the business time zone: today | "
                                           "yesterday | tomorrow | this/last/next week|month|quarter|year | last N "
                                           "days|weeks|months|quarters|years | next N days|weeks|months | year to date | "
                                           "2026 | Q2 2026 | May 2026 | 2026-05 | 2026-05-03 | since 2026-08-01 | after "
                                           "2026-06-30 | before 2026-01-01 | until 2026-03-31 | 2026-06-01 to 2026-06-30 "
                                           "| upcoming | past. Add 'so far' to stop at today. Leave it out for all time."}
RELATED = {"type": "array", "maxItems": 2,
           "description": "Conditions on child records that point to this record. exists=false keeps records with NO "
                          "such child (e.g. trainings with no sessions); exists=true with min_count keeps records with at "
                          "least that many.",
           "items": {"type": "object", "properties": {
               "child_object": {"type": "string"},
               "exists": {"type": "boolean"},
               "min_count": {"type": "integer", "minimum": 1, "description": "With exists=true: at least this many "
                                                                             "matching children (1 = exists)."},
               "via_field": {"type": "string", "description": "Lookup on the child that points here; only needed when "
                                                              "the child has more than one."},
               "filters": {"type": "array", "maxItems": 5, "items": FILTER_ITEM_BRIEF},
               "date_field": {"type": "string"}, "period": {"type": "string"}},
               "required": ["child_object", "exists"], "additionalProperties": False}}
MEASURE = {"type": "string", "enum": ["count", "count_distinct", "sum", "avg", "min", "max"],
           "description": "What to compute per group; default count (number of records)."}
MEASURE_FIELD = {"type": "string", "description": "Field for count_distinct, sum, avg, min, max (sum and avg need a "
                                                  "number field)."}
SPLIT_BY = {"type": "string", "description": "Optional second field (picklist, checkbox or lookup; not a date) to break "
                                             "each group down by, e.g. Interview_Outcome__c."}


#: The rate per period of trend_records (placement fixes, 2026-11-16): the one change of the record tool list against
#: the notebook's (tests/test_tool_registry.py takes both out to compare with the notebook's golden lists).
TREND_RATE_TEXT = ("With part_filters: part, base and percent per bucket (a rate per period in one call); a bucket "
                   "with no base records has part and base 0 and percent null (no rate, not 0%). ")
TREND_PART_FILTERS = {"type": "array", "minItems": 1, "maxItems": 8, "items": FILTER_ITEM_BRIEF,
                      "description": "What makes a record count as the part (filters are the base); count only."}


def _obj_schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


SCHEMAS: dict[str, tuple[str, dict]] = {
    "find_objects": (
        "Find which object holds the records the user means. Searches object labels, descriptions, record grain and "
        "key concepts in the knowledge files, plus stored picklist values that match the words. Returns objects with "
        "record counts and last sync. Example: {\"query\":\"client interviews\"}",
        _obj_schema({"query": {"type": "string", "description": "The user's words for the records, e.g. 'client "
                                                                "interviews' or 'locked deliverables'."},
                     "k": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Maximum objects (default 5)."}},
                    ["query"])),
    "describe_fields": (
        "Fields, date fields, lookups and stored values of one object, with meanings from the knowledge files. Give "
        "query (the user's words for a condition or attribute, e.g. 'offer', 'payment pending', 'visa') to get the best "
        "matching fields AND the stored values that match. Use the returned API names and values exactly.",
        _obj_schema({"object": OBJECT,
                     "query": {"type": "string", "description": "Words to match fields and stored values; omit to list "
                                                                "all fields briefly."},
                     "k": {"type": "integer", "minimum": 1, "maximum": 30, "description": "Maximum matching fields "
                                                                                          "(default 12)."}},
                    ["object"])),
    "get_field_values": (
        "Stored values of one field with record counts (blank = no value), marked as current picklist value, legacy "
        "(stored but no longer in the picklist) or unused (in the picklist, 0 records). For a lookup field: the related "
        "records' names with counts. For number and date fields: min, max and how many records have a value.",
        _obj_schema({"object": OBJECT, "field": {"type": "string", "description": "Field API name or lookup path."},
                     "contains": {"type": "string", "description": "Only values containing this text."},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Maximum values "
                                                                                              "(default 30)."}},
                    ["object", "field"])),
    "count_records": (
        "Exact number of records of one object that match filters, an optional time window and optional child-record "
        "conditions (no row limit). Set count_distinct to a field to count different values instead (e.g. Candidate__c "
        "= how many different candidates). Example: {\"object\":\"Interview__c\",\"filters\":[{\"field\":"
        "\"Interview_Outcome__c\",\"op\":\"eq\",\"value\":\"Offer Received\"}]}",
        _obj_schema({"object": OBJECT, "filters": FILTERS, "any_of": ANY_OF, "date_field": DATE_FIELD, "period": PERIOD,
                     "related": RELATED,
                     "count_distinct": {"type": "string", "description": "Count the different values of this field "
                                                                         "(records with a value) instead of records."}},
                    ["object"])),
    "aggregate_records": (
        "Sum, average, median, minimum or maximum of one number field (min/max also work on dates) over matching "
        "records. Returns the value plus how many records matched and how many had a value.",
        _obj_schema({"object": OBJECT, "op": {"type": "string", "enum": ["sum", "avg", "median", "min", "max"]},
                     "field": {"type": "string"}, "filters": FILTERS, "any_of": ANY_OF, "date_field": DATE_FIELD,
                     "period": PERIOD, "related": RELATED},
                    ["object", "op", "field"])),
    "group_records": (
        "Break records down by one field (count per status, per type, per recruiter...) or rank them (top N). A lookup "
        "field or path groups by the related record's Name (Recruiter__c, Program_Version__c.Program__c). Count what the "
        "question counts: 'interviews per recruiter' = object Interview__c, group_by its recruiter-role lookup "
        "Recruiter__c; when an object has several lookups to the same object (employee roles), group by the one whose "
        "role fits. For a breakdown by date use trend_records.",
        _obj_schema({"object": OBJECT, "group_by": {"type": "string", "description": "Field or lookup path to group by."},
                     "split_by": SPLIT_BY, "measure": MEASURE, "measure_field": MEASURE_FIELD, "filters": FILTERS,
                     "any_of": ANY_OF, "date_field": DATE_FIELD, "period": PERIOD, "related": RELATED,
                     "top": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Keep only the N highest "
                             "groups (lowest with order=asc); the blank group is left out."},
                     "order": {"type": "string", "enum": ["desc", "asc"], "description": "Default desc."},
                     "min_value": {"type": "number", "description": "Keep only groups whose value is at least this "
                                                                    "(e.g. 2 for duplicates)."}},
                    ["object", "group_by"])),
    "trend_records": (
        "Counts (or another measure) per day/week/month/quarter/year over a time window, bucketed on the SAME date "
        "field the window uses. Empty buckets are filled with 0. " + TREND_RATE_TEXT + "Example: {\"object\":"
        "\"Interview__c\",\"grain\":\"month\",\"date_field\":\"Date_of_Interview__c\",\"period\":\"last 6 months\"}",
        _obj_schema({"object": OBJECT, "grain": {"type": "string", "enum": ["day", "week", "month", "quarter", "year"]},
                     "date_field": DATE_FIELD, "period": PERIOD, "split_by": SPLIT_BY, "measure": MEASURE,
                     "measure_field": MEASURE_FIELD, "filters": FILTERS, "any_of": ANY_OF, "related": RELATED,
                     "part_filters": TREND_PART_FILTERS},
                    ["object", "grain", "date_field", "period"])),
    "compare_records": (
        "Compare 2 to 4 segments of the same object (e.g. ghosted vs rejected, this month vs last month). Each segment "
        "has its own label, filters and/or period; shared filters apply to all. Do not repeat a segment condition in the "
        "shared filters. Returns each value, which is higher, the difference, the ratio and the percent change.",
        _obj_schema({"object": OBJECT,
                     "segments": {"type": "array", "minItems": 2, "maxItems": 4, "items": {
                         "type": "object", "properties": {
                             "label": {"type": "string"},
                             "filters": {"type": "array", "maxItems": 5, "items": FILTER_ITEM_BRIEF},
                             "period": {"type": "string"}},
                         "required": ["label"], "additionalProperties": False}},
                     "date_field": DATE_FIELD, "measure": MEASURE, "measure_field": MEASURE_FIELD, "filters": FILTERS,
                     "related": RELATED},
                    ["object", "segments"])),
    "percentage_of_records": (
        "What share of records match a condition. part_filters = what makes a record count as the part; base_filters = "
        "the whole (empty = all records of the object). The part is always taken inside the base. period/related apply "
        "to both. Returns part, base and percent.",
        _obj_schema({"object": OBJECT,
                     "part_filters": {"type": "array", "minItems": 1, "maxItems": 8, "items": FILTER_ITEM},
                     "base_filters": FILTERS, "date_field": DATE_FIELD, "period": PERIOD, "related": RELATED},
                    ["object", "part_filters"])),
    "list_records": (
        "List matching records with chosen fields (lookups show the related Name), sorted, with the total number that "
        "match. Use for 'show/list/which ...', 'latest/earliest' (order_by a date, limit 1) and 'details of <record "
        "number>' (filter Name eq the number; fields omitted = key fields).",
        _obj_schema({"object": OBJECT,
                     "fields": {"type": "array", "maxItems": 12, "items": {"type": "string"},
                                "description": "Fields or lookup paths to show; omit for key fields."},
                     "filters": FILTERS, "any_of": ANY_OF, "date_field": DATE_FIELD, "period": PERIOD, "related": RELATED,
                     "order_by": {"type": "string", "description": "Field to sort by (default: date_field if set, else "
                                                                   "CreatedDate)."},
                     "order": {"type": "string", "enum": ["desc", "asc"], "description": "Default desc."},
                     "limit": {"type": "integer", "minimum": 1, "maximum": LIMITS.list_max_rows,
                               "description": f"Rows to return (default 10, at most {LIMITS.list_max_rows})."}},
                    ["object"])),
    "search_records": (
        "Find records by name or record number across objects when you do not know the object (a person, a program, "
        "'REC-00028'). Exact name first, then contains. Never returns e-mail or phone. For who named people are or how "
        "they are linked, use about_people.",
        _obj_schema({"name": {"type": "string", "description": "Name or record number, at least 2 characters."},
                     "objects": {"type": "array", "maxItems": 10, "items": {"type": "string"},
                                 "description": "Limit to these objects."},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Maximum records per "
                                                                                              "object (default 5)."}},
                    ["name"])),
    "about_people": (
        "Who named people (or named records) are and how they are linked. names: 1 to 4 names as the question gives them "
        "(full name, first name, 'Name (#k)', record number or Id), all in one call. For each name it lists every stored "
        "person with it (Employee, User, candidate Account, Lead; one person's linked records together): kind, profile "
        "(their own record's fields), where the person is referenced (lookup, role, records), creator/editor/owner "
        "counts; for two or more names, every stored record that links them (same record, own record, one or two parent "
        "hops, same activity record, same manager or department) and links_checked (what was checked). Names that are "
        "not stored come back in not_found with the closest stored names. Counts cover all records of each object (no "
        "period, filter or status): use a query tool for those, and never add counts of different lookups. Example: "
        "{\"names\":[\"<name>\",\"<name>\"]}",
        _obj_schema({"names": {"type": "array", "minItems": 1, "items": {"type": "string"},
                               "description": "1 to 4 names as the question gives them (full or first name, 'Name (#k)', "
                                              "record number or Id), at least 2 characters each."},
                     "object": {"type": "string", "description": "Only this person object (Recruiter__c, User, Account "
                                                                 "or Lead); default: every person object, then every "
                                                                 "object with names."},
                     "in_objects": {"type": "array", "maxItems": 10, "items": {"type": "string"},
                                    "description": "Only references and links from these objects."}},
                    ["names"])),
    "get_field_history": (
        "Recorded field changes (who changed what, old and new value, when) for an object or one record, from the "
        "object's field-history table. Only tracked fields and the history period are covered; if the object has no "
        "history table, changes are not recorded. Set count_by to count changes instead of listing them.",
        _obj_schema({"object": OBJECT,
                     "record": {"type": "string", "description": "Record Name or number, e.g. REC-00024."},
                     "field": {"type": "string", "description": "Tracked field API name, or 'created'."},
                     "changed_to": {"type": "string", "description": "Only changes whose new value is this."},
                     "period": {"type": "string", "description": "When the change happened (same words as period "
                                                                 "elsewhere)."},
                     "count_by": {"type": "string", "enum": ["field", "new_value", "changed_by", "month"],
                                  "description": "Return counts per group instead of rows."},
                     "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Rows to return "
                                                                                              "(default 20)."}},
                    ["object"])),
    "get_sync_status": (
        "When the local warehouse copy was taken and when each object was last synced from Salesforce (preprod "
        "sandbox). Sync failures, API usage and batch history are not recorded.",
        _obj_schema({"objects": {"type": "array", "maxItems": 20, "items": {"type": "string"}}}, [])),
}

_ARG_HINTS = {"object": "the object API name; call find_objects if unsure", "query": "the user's words",
              "field": "a field API name; call describe_fields if unsure", "group_by": "the field to group by",
              "grain": "day, week, month, quarter or year", "date_field": "the date field the period applies to",
              "period": "a time window such as 'last 6 months'", "segments": "2 to 4 segments, each with a label",
              "part_filters": "the conditions that make a record count as the part",
              "name": "the name to search for, at least 2 characters",
              "names": "1 to 4 names as the question gives them",
              "op": "sum, avg, median, min or max"}


#: Record tool -> the :class:`~sf_assistant.tools.record_tools.RecordTools` method that runs it (it lives in one of
#: the mixins: discovery, records, people, meta).
RECORD_HANDLERS = {
    "find_objects": "_find_objects", "describe_fields": "_describe_fields",
    "get_field_values": "_get_field_values", "count_records": "_count_records",
    "aggregate_records": "_aggregate_records", "group_records": "_group_records",
    "trend_records": "_trend_records", "compare_records": "_compare_records",
    "percentage_of_records": "_percentage_of_records", "list_records": "_list_records",
    "search_records": "_search_records", "about_people": "_about_people",
    "get_field_history": "_get_field_history", "get_sync_status": "_get_sync_status"}

#: The 14 record tools in :data:`TOOL_ORDER` (the order the model sees them).
RECORD_TOOLS: tuple[ToolDef, ...] = tuple(ToolDef(name, SCHEMAS[name][0], SCHEMAS[name][1], RECORD_HANDLERS[name])
                                          for name in TOOL_ORDER)


# ================================================================================ lists and docs

def knowledge_tool_list(obj: str, sections: list[str]) -> list[dict]:
    """MCP ``tools/list`` of the one-object knowledge tools: equal to ``ToolRegistry.list_tools()`` of a knowledge
    file of object ``obj`` with these ``sections``."""
    return mcp_tool_list(knowledge_tool_defs(obj, sections))


def multi_tool_list(names: list[str], first_object: str, sections: list[str], components: bool = False) -> list[dict]:
    """MCP ``tools/list`` of ``MultiToolRegistry`` over the objects ``names`` (sorted), whose first loaded knowledge file
    is of ``first_object`` with ``sections``; ``components``: with the flow components loaded (the 3 component tools
    appended)."""
    names = list(names)
    defs = multi_tool_defs(names) + [multi_object_tool(t, names) for t in knowledge_tool_list(first_object, sections)]
    return mcp_tool_list(defs + (component_tool_defs() if components else []))


def component_tool_list() -> list[dict]:
    """MCP ``tools/list`` of the component tools (``ComponentTools.list_tools()``), without loading a component."""
    return mcp_tool_list(component_tool_defs())


def record_tool_list() -> list[dict]:
    """MCP ``tools/list`` of the record tools: equal to ``RecordTools.list_tools()``, without opening a warehouse."""
    return mcp_tool_list(RECORD_TOOLS)


_DOC_OBJECT, _DOC_FIRST, _DOC_SECTIONS, _DOC_OBJECTS = "{object}", "{first object}", ["{sections}"], ["{objects}"]


def _arg_names(schema: dict) -> tuple[str, str]:
    props = list((schema.get("properties") or {}).keys())
    required = [p for p in schema.get("required") or [] if p in props] or []
    optional = [p for p in props if p not in required]
    fmt = lambda names: ", ".join(f"`{n}`" for n in names) or "-"  # noqa: E731
    return fmt(required), fmt(optional)


def _tool_section(defs: list[ToolDef], owner: str) -> list[str]:
    lines = ["| # | Tool | Required arguments | Optional arguments | Handler |", "|---|---|---|---|---|"]
    for i, d in enumerate(defs, 1):
        req, opt = _arg_names(d.input_schema)
        lines.append(f"| {i} | `{d.name}` | {req} | {opt} | `{owner}.{d.handler}` |")
    for d in defs:
        lines += ["", f"### `{d.name}`", "", "```text", d.description, "```", "", "Input schema:", "", "```json",
                  json.dumps(d.input_schema, indent=2, ensure_ascii=False), "```"]
    return lines


def tools_markdown() -> str:
    """The text of ``docs/tools.md``: every tool set, rendered from the definitions above (no knowledge file needed)."""
    one = knowledge_tool_defs(_DOC_OBJECT, list(_DOC_SECTIONS))
    multi_own = multi_tool_defs(list(_DOC_OBJECTS))
    multi_per = [multi_object_tool(t, list(_DOC_OBJECTS)) for t in mcp_tool_list(knowledge_tool_defs(_DOC_FIRST,
                                                                                                      list(_DOC_SECTIONS)))]
    comp = component_tool_defs()
    lines = [
        "# Tools",
        "",
        "Generated by `scripts/gen_tools_doc.py` from `src/sf_assistant/tools/registry.py`. Do not edit this file by "
        "hand: change the registry, then run `.venv/bin/python scripts/gen_tools_doc.py`. `tests/test_tool_registry.py` "
        "fails when this file is not current.",
        "",
        "Every tool a model can call is defined once, in `sf_assistant.tools.registry`: its name, its description, its "
        "input schema (JSON Schema, the MCP `inputSchema`) and the method that runs it. The registries build their "
        "tools from these definitions:",
        "",
        "| Tool set | Registry | Tools | Who gets it |",
        "|---|---|---|---|",
        f"| Knowledge, one object | `sf_assistant.tools.knowledge_tools.ToolRegistry` | {len(one)} | the knowledge agent "
        "with exactly one knowledge file (`--knowledge FILE`) |",
        f"| Knowledge, several objects | `sf_assistant.tools.knowledge_tools.MultiToolRegistry` | "
        f"{len(multi_own) + len(multi_per)} (+{len(comp)} component tools) | the knowledge agent with several files "
        "(default: every file in `knowledge/objects/`); the MCP server |",
        f"| Flow components | `sf_assistant.tools.component_tools.ComponentTools` | {len(comp)} | appended to the several-"
        "object tools when `knowledge/components/` holds an `index.json` (the full knowledge agent, the MCP server); "
        "not the one-object registry, not the lean step |",
        f"| Records | `sf_assistant.tools.record_tools.RecordTools` | {len(RECORD_TOOLS)} | the record agent (local "
        "warehouse snapshot) |",
        "",
        "The lean knowledge step (`sf_assistant.agent.lean`) offers a subset of the knowledge tools with the same "
        "definitions.",
        "",
        "Each registry gives its tools as `list_tools()` (MCP `tools/list`: `name`, `description`, `inputSchema`) and "
        "`openai_tools()` (`{\"type\": \"function\", \"function\": {name, description, parameters}}`), in the order "
        "below, and runs one with `call(name, arguments)`, which never raises.",
        "",
        "Placeholders on this page: `{object}` is the API name of the object of the knowledge file, `{sections}` the "
        "section ids of that file (one enum value each), `{objects}` the API names of the loaded objects (sorted, one "
        "enum value each) and `{first object}` the object of the first loaded knowledge file.",
        "",
        "## Knowledge tools, one object",
        "",
        f"`ToolRegistry` over one object-knowledge file: {len(one)} tools.",
        "",
        *_tool_section(one, "ToolRegistry"),
        "",
        "## Knowledge tools, several objects",
        "",
        f"`MultiToolRegistry` over several knowledge files: {len(multi_own)} tools of its own, then the "
        f"{len(multi_per)} one-object tools with a required `object` argument (enum: the loaded objects) and "
        "\" Set `object` to the object API name.\" added to their description. `_object_tool` sends the call to that "
        "object's `ToolRegistry`.",
        "",
        "Known quirk (kept as it was): the per-object descriptions are copied from the first loaded object's "
        "registry, so they name that object (`{first object}`), whatever object the call is for.",
        "",
        *_tool_section(multi_own + multi_per, "MultiToolRegistry"),
        "",
        "## Flow component tools",
        "",
        f"`ComponentTools` over the flow components (`knowledge/components/`: `flows/<ApiName>.json` and `index.json`, "
        f"loaded by `sf_assistant.knowledge.components`): {len(comp)} tools, offered after the several-object tools "
        "when the components are loaded (`config.COMPONENTS_DIR`, `SFA_COMPONENTS_DIR`). They take no object enum: code "
        "resolves every name (API name, label, title or alias, with did-you-means; `find_usages` also takes an "
        "object label). Budgets (`config.LIMITS`): `search_components` at most 4,000 characters (one card per flow, "
        "about 5 fit; `k` 5 by default, at most 10; `more` counts only the hits that score close to the best one); "
        "`get_component` summary at most 3,500 (shortened least needed first: dev10 notes, meaning, caveats, lists, "
        "and the effect lines last), its `effects` (one line per effect), `logic`, `fields` and `caveats` levels in "
        "pages of at most 6,000 (`page`, `pages`, `next_page`; an item larger than a page is split into `part`s); "
        "`find_usages` at most 40 rows and 9,000 characters, then `more`, always with `not_covered`. A writer row "
        "gives `values` (literal) and `value_from` (a variable, a field or a formula); with `value`, the rows write "
        "that literal value, `values_not_fixed` may write it, `values_unknown` (Apex, not built) are not scanned and "
        "`writes_other_values` write other literal values only. The files' development evidence is stripped when "
        "they are loaded: no result carries it, and the card index never holds it.",
        "",
        *_tool_section(comp, "ComponentTools"),
        "",
        "## Record tools",
        "",
        "On the MCP server with `SFA_CHARTS` on, the default, `group_records`, `trend_records` and `compare_records` "
        "also take an optional `chart: true`, which adds the envelope key `chart` "
        "([runbook.md](runbook.md#charts-sfa_charts)). It is not in the schemas below, and the record agent never sees it.",
        "",
        f"`RecordTools` over one open warehouse snapshot: {len(RECORD_TOOLS)} tools. The handlers live in the mixins "
        "of `RecordTools` (`sf_assistant.tools.discovery`, `records`, `people`, `meta`).",
        "",
        "Aliases (names the model does not see; `call()` runs the visible tool): "
        + ", ".join(f"`{a}` -> `{t}`" for a, t in TOOL_ALIASES.items()) + ".",
        "",
        *_tool_section(list(RECORD_TOOLS), "RecordTools"),
        "",
    ]
    return "\n".join(lines)
