"""MCP-style tool registry over one object-knowledge file.

Each tool has a name, a description, a JSON-Schema ``input_schema`` (the MCP
``inputSchema`` shape) and a handler. :class:`ToolRegistry` exposes the tools three ways:

* ``list_tools()``  – the MCP ``tools/list`` payload,
* ``openai_tools()`` – the OpenAI ``tools=[...]`` payload for chat completions,
* ``call(name, arguments)`` – the MCP ``tools/call`` equivalent. It never raises and
  always returns ``{"ok": bool, "result": <json>, "error": str | None, "chunk_ids": [...]}``.

Handler contract (internal): a handler takes the validated keyword arguments and returns
``{"result": <json-serialisable>, "chunk_ids": [<chunk ids it returned>]}``. User-facing
problems (unknown field, bad enum value) are signalled by raising :class:`ToolError`, which
``call`` turns into ``ok: False`` with a readable message.

Results are deliberately self-contained: e.g. ``get_field`` returns every attribute of the
field together with its picklist values, reference targets, relationship semantics, the
flows / apex that read or write it and the validation rules that involve it, so an agent
can answer from one call without guessing. Attributes that are ``null`` in the file are
listed under ``unspecified_in_knowledge`` instead of being silently dropped.

Spelling help (``SFA_RESOLVER=on``, docs/resolver_integration.md (1), (2), (3), (6)): ``attach_resolver`` gives the
registry the knowledge term resolver and the BM25 indexes their expander (``search_knowledge``, ``search_fields`` and
``search_all_knowledge`` then report ``read_as``: what a misspelt query word was searched as). Step (3), the tool
arguments: an ``object`` (the several-object tools) or a field argument (``get_field``, ``get_picklist_values``,
``get_validation_rules``, ``get_automation``) that no exact tier knows is read as its closest name when the resolver
says ``corrected`` with one target (the object's, for a field), and the result states it in ``read_as``: a list of
``{"arg", "typed", "used", "kind", "target", "how"}`` (``read_as_arguments`` when the result already holds a search's
``read_as``). ``ambiguous``, an ``exact`` reading the tiers missed and a corrected one outside a tool call only give a
"did you mean" in the error; another object's exact field API name is never read as a field of this one. The functions
at the end of this module write the spelling section of both agents' question hints (:func:`question_spans`,
:func:`spelling_section`, :func:`corrected_text`) and the answer's "Read as" line (:func:`with_read_as_line`). Without
a resolver (no ``attach_resolver`` call: ``SFA_RESOLVER=off``) nothing of this runs and every result is as before.
"""

from __future__ import annotations

import contextvars
import difflib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from sf_assistant import prompts as _prompt_files   # the spelling help's texts (spelling_hints, read_as_line)
from sf_assistant.knowledge.search import METADATA_KINDS, KnowledgeIndex, resolver_expander, tokenize
from sf_assistant.knowledge.store import SYSTEM_FIELDS, ObjectKnowledge, split_api_name
# the flow component tools (knowledge/components): appended to the several-object tools when components are loaded
from sf_assistant.tools.component_tools import ComponentTools
# one Tool class for the knowledge and the record tools; every tool definition lives in the registry
from sf_assistant.tools.registry import Tool, knowledge_tool_defs, multi_object_tool, multi_tool_defs

__all__ = ["Tool", "ToolRegistry", "ToolError"]

_NULL_NOTE = "not specified in the knowledge file (null or absent); do not infer it"

# Field capability attributes; when null *or absent* they are reported as unspecified.
_CAPABILITY_ATTRS: tuple[str, ...] = (
    "required",
    "unique",
    "external_id",
    "filterable",
    "sortable",
    "groupable",
    "calculated",
)


class ToolError(Exception):
    """A user-level tool error (bad argument, unknown name). Becomes ``ok: False``."""


def _compact(d: dict) -> tuple[dict, list[str]]:
    """Split a dict into (non-null entries, keys whose value is null)."""
    kept = {k: v for k, v in d.items() if v is not None}
    nulls = [k for k, v in d.items() if v is None]
    return kept, nulls


def _dedupe(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(i for i in ids if i))


# ------------------------------------------------------------------ spelling help, step (3): tool arguments
#: The correction channel of the current knowledge tool call (docs/resolver_integration.md (3), knowledge side).
#: :meth:`ToolRegistry.call` and :meth:`MultiToolRegistry.call` set a fresh list here around a call when the spelling
#: help is on (``registry.resolver`` is not None); the object and field tiers append one entry per argument they read
#: as its closest name (:func:`_record_read_as`). Unset (None: a handler called directly, the switch off), a close
#: match is never used on its own: the error lists it as a did-you-mean. The record tools have their own channel
#: (``READ_AS`` of the record engine), which this knowledge-side module may not import (tests/test_layering.py).
ARGUMENT_READ_AS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "KNOWLEDGE_ARGUMENT_READ_AS", default=None)
#: Decisions of the resolver whose names an error lists as did-you-means. Only ``corrected`` is ever used on its own
#: (decision B); ``exact`` here means the resolver's wider normalising (fillers, plurals) found a name the exact tiers
#: did not, so it is offered, not used.
_SUGGEST = ("corrected", "ambiguous", "exact")
#: Did-you-means named in an unknown-object error, and field names the resolver puts first in a field error's
#: closest list.
_DID_YOU_MEAN = 3
_RESOLUTION_CACHE_MAX = 4096


def _in_call() -> bool:
    """True inside a knowledge tool call with the spelling help on (a close match may then be used, and is stated)."""
    return ARGUMENT_READ_AS.get() is not None


def _record_read_as(typed: str, used: str, kind: str, target: str, how: str, arg: str | None = None) -> None:
    """Add one correction to the current call's channel (nothing outside a call); the same one twice is kept once.
    ``arg`` None: :func:`_with_read_as` names the argument that holds ``typed``."""
    sink = ARGUMENT_READ_AS.get()
    if sink is None:
        return
    if not any(e["kind"] == kind and e["typed"] == typed and e["target"] == target for e in sink):
        sink.append({"arg": arg, "typed": typed, "used": used, "kind": kind, "target": target, "how": how})


def _spelling(owner: Any, text: str, kinds: tuple[str, ...], context_object: str | None = None,
              within: str | None = None) -> Any:
    """``owner.resolver.resolve_term(...)`` (a tool lookup), cached on ``owner`` (the resolver is read-only); None
    without a resolver or when it fails (the spelling help is optional). Called only after every exact tier failed,
    so a correctly spelt argument never reaches it."""
    r = getattr(owner, "resolver", None)
    if r is None:
        return None
    cache = owner.__dict__.setdefault("_resolutions", {})
    key = (str(text), kinds, context_object, within)
    res = cache.get(key)
    if res is None:
        try:
            res = r.resolve_term(text, kinds=list(kinds), context_object=context_object, within=within)
        except Exception:  # noqa: BLE001 - the spelling help is optional
            return None
        if len(cache) >= _RESOLUTION_CACHE_MAX:
            cache.clear()
        cache[key] = res
    return res


def _or_names(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def _with_read_as(out: dict, entries: list[dict], arguments: Any) -> dict:
    """``out`` (a call's payload) stating the arguments the call read as their closest names: each entry's ``arg``
    is the argument that holds its typed text; a successful dict result gets them under ``read_as``, or under
    ``read_as_arguments`` when it already has a ``read_as`` (a search tool's query expansion, a dict, which the answer
    does not state); a failed call's error starts with one note per reading. ``out`` itself without a reading."""
    if not entries:
        return out
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except json.JSONDecodeError:
            args = {}
    args = args if isinstance(args, dict) else {}
    rows = []
    for e in entries:
        row = dict(e)
        if row.get("arg") is None:
            row["arg"] = next((k for k, v in args.items() if isinstance(v, (str, int, float))
                               and str(v).strip() == row["typed"]), None)
        rows.append(row)
    out = dict(out)
    result = out.get("result")
    if out.get("ok") and isinstance(result, dict):
        out["result"] = {**result, ("read_as" if "read_as" not in result else "read_as_arguments"): rows}
    elif not out.get("ok") and out.get("error"):
        notes = "; ".join(f"{r['kind']} '{r['typed']}' read as '{r['used']}' ({r['how']})" for r in rows)
        out["error"] = f"{notes}: {out['error']}"
    return out


def _call_reading(call: Callable[[str, Any], dict], name: str, arguments: Any) -> dict:
    """``call(name, arguments)`` with a fresh correction channel (:data:`ARGUMENT_READ_AS`), its readings stated in
    the payload (:func:`_with_read_as`). Never raises (``call`` never does)."""
    sink: list[dict[str, Any]] = []
    token = ARGUMENT_READ_AS.set(sink)
    try:
        out = call(name, arguments)
    finally:
        ARGUMENT_READ_AS.reset(token)
    return _with_read_as(out, sink, arguments)


class ToolRegistry:
    """Registry of the knowledge tools for one :class:`ObjectKnowledge`."""

    tools: dict[str, Tool]

    def __init__(self, knowledge: ObjectKnowledge, index: KnowledgeIndex) -> None:
        self.knowledge = knowledge
        self.index = index
        self._chunk_ids = set(knowledge.section_ids())
        self._rel_profiles: list[dict] | None = None
        self.resolver: Any = None              # the knowledge term resolver (attach_resolver), None: spelling help off
        self.resolver_lines: list[str] = []    # its start-up lines (sf_assistant.pipeline.build)
        self.tools = {}
        self._register_all()

    def attach_resolver(self, resolver: Any) -> None:
        """Give the tools the knowledge term resolver (docs/resolver_integration.md, "Build";
        :func:`sf_assistant.knowledge.terms.knowledge_resolver`): the knowledge files and the word list only, never a
        stored value or a person name. Called by :func:`sf_assistant.pipeline.build` when the spelling help is on; None
        (the default) leaves every tool as it is. The BM25 index gets the resolver's expander
        (:func:`sf_assistant.knowledge.search.resolver_expander`, every kind: this resolver has no person and no stored
        value), so the search tools read a misspelt query word as the knowledge spells it (``read_as``), and a misspelt
        field argument is read as its closest field name (step (3): :meth:`_resolve_field`)."""
        self.resolver = resolver
        self._resolutions = {}                 # the readings of this resolver (_spelling)
        self.index.expand = resolver_expander(resolver) if resolver is not None else None

    # ================================================================== public API
    def list_tools(self) -> list[dict]:
        """MCP ``tools/list`` shape: ``[{name, description, inputSchema}]``."""
        return [
            {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
            for t in self.tools.values()
        ]

    def openai_tools(self) -> list[dict]:
        """OpenAI chat-completions ``tools`` payload."""
        return [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
            }
            for t in self.tools.values()
        ]

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """Run a tool. Never raises.

        Returns ``{"ok": bool, "result": <json>|None, "error": str|None, "chunk_ids": [...]}``. With the spelling
        help on, the call runs with its own correction channel (unless a :class:`MultiToolRegistry` call already set
        one) and a field argument read as its closest name is stated (``read_as``; :func:`_with_read_as`).
        """
        if self.resolver is not None and ARGUMENT_READ_AS.get() is None:
            return _call_reading(self.call, name, arguments)
        tool = self.tools.get(name)
        if tool is None:
            close = difflib.get_close_matches(str(name), list(self.tools), n=3, cutoff=0.4)
            return self._fail(f"unknown tool '{name}'; available: {sorted(self.tools)}" + (f"; closest: {close}" if close else ""))
        try:
            kwargs = self._validate_args(tool, arguments)
            out = tool.handler(**kwargs)
            result = out.get("result")
            chunk_ids = [c for c in _dedupe(list(out.get("chunk_ids") or [])) if c in self._chunk_ids]
            json.dumps(result)  # guarantee serialisable output
            return {"ok": True, "result": result, "error": None, "chunk_ids": chunk_ids}
        except ToolError as exc:
            return self._fail(str(exc))
        except Exception as exc:  # defensive: a tool must never crash the agent loop
            return self._fail(f"{type(exc).__name__}: {exc}")

    # ================================================================== arg handling
    @staticmethod
    def _fail(msg: str) -> dict:
        return {"ok": False, "result": None, "error": msg, "chunk_ids": []}

    @staticmethod
    def _validate_args(tool: Tool, arguments: Any) -> dict:
        """Light JSON-Schema check + coercion (strings to ints, drop nulls, ignore unknown keys)."""
        if arguments is None:
            arguments = {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise ToolError(f"arguments are not valid JSON: {exc}") from None
        if not isinstance(arguments, dict):
            raise ToolError("arguments must be a JSON object")
        if "_raw" in arguments and len(arguments) == 1 and tool.input_schema.get("properties"):
            raise ToolError(f"arguments were not valid JSON: {str(arguments['_raw'])[:200]}")
        props = tool.input_schema.get("properties", {})
        # Unknown arguments are ignored (models sometimes add extras); required ones are enforced.
        kwargs: dict[str, Any] = {}
        for key, spec in props.items():
            if key not in arguments or arguments[key] is None or arguments[key] == "":
                continue
            val = arguments[key]
            typ = spec.get("type")
            if typ == "integer":
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    raise ToolError(f"argument '{key}' must be an integer") from None
                val = max(spec.get("minimum", val), min(spec.get("maximum", val), val))
            elif typ == "string":
                if isinstance(val, (list, dict)):
                    raise ToolError(f"argument '{key}' must be a string")
                val = str(val).strip()
            kwargs[key] = val
        missing = [r for r in tool.input_schema.get("required", []) if r not in kwargs]
        if missing:
            raise ToolError(f"missing required argument(s) {missing} for {tool.name}")
        return kwargs

    # ================================================================== helpers
    def _resolve_field(self, name: str) -> dict:
        """Field dict for ``name`` or a ToolError with up to 5 difflib suggestions.

        With the spelling help on (``self.resolver``), a name no exact tier knows is read as its closest field name
        inside a tool call when the resolver says ``corrected`` with one field of this object (:meth:`_close_field`,
        stated in ``read_as``); otherwise the error lists the resolver's names first (:meth:`_suggest_fields`)."""
        f = self.knowledge.field(name)
        if f is not None:
            return f
        if self.resolver is not None:   # the spelling help (SFA_RESOLVER=on): one clear match, or did-you-means first
            f = self._close_field(name)
            if f is not None:
                return f
        raise ToolError(f"no field named {name}; closest: {self._suggest_fields(name)}")

    # ------------------------------------------------------------------ spelling help, step (3): field arguments
    def _field_spelling(self, raw: Any) -> Any:
        """The resolver's reading of ``raw`` among this object's fields (an ``<this object>.`` prefix dropped)."""
        key = str(raw or "").strip()
        prefix = self.knowledge.api_name + "."
        if key.lower().startswith(prefix.lower()):
            key = key[len(prefix):]
        return _spelling(self, key, ("field",), self.knowledge.api_name, prefix) if key else None

    def _own_field_names(self, res: Any) -> list[str]:
        """The field API names of this object among ``res``'s targets (``Obj.Field``), best first."""
        prefix = self.knowledge.api_name + "."
        return list(dict.fromkeys(t[len(prefix):] for t in res.targets if t.startswith(prefix)))

    def _close_field(self, raw: Any) -> dict | None:
        """Step (3) for a field name no exact tier knows: the field of one ``corrected`` reading inside a tool call
        (recorded in :data:`ARGUMENT_READ_AS`), else None. Another object's exact field API name is never read as
        a field of this object (the agent picked the wrong object, or a name common to many objects: today's error
        and its closest list answer it)."""
        if not _in_call() or self._fields_elsewhere(raw):
            return None
        res = self._field_spelling(raw)
        if res is None or res.decision != "corrected":
            return None
        apis = self._own_field_names(res)
        f = self.knowledge.field(apis[0]) if len(apis) == 1 else None
        if f is None or str(f.get("api_name")).lower() != apis[0].lower():
            return None
        api = str(f.get("api_name"))
        _record_read_as(str(raw).strip(), api, "field", f"{self.knowledge.api_name}.{api}", "closest field name")
        return f

    def _close_field_names(self, raw: Any) -> list[str]:
        """The resolver's did-you-means for an unknown field of this object (at most :data:`_DID_YOU_MEAN`)."""
        res = self._field_spelling(raw)
        if res is None or res.decision not in _SUGGEST:
            return []
        out = []
        for a in self._own_field_names(res):
            f = self.knowledge.field(a)
            if f is not None and str(f.get("api_name")).lower() == a.lower():
                out.append(str(f.get("api_name")))
        return _dedupe(out)[:_DID_YOU_MEAN]

    def _fields_elsewhere(self, raw: Any) -> list[str]:
        """The other objects with a field whose API name is exactly ``raw`` (any case), by the resolver's exact
        reading over every loaded knowledge file; [] without one (:meth:`_close_field` then never reads ``raw``)."""
        key = str(raw or "").strip()
        if not key:
            return []
        res = _spelling(self, key, ("field",))
        if res is None or res.decision != "exact":
            return []
        own, low = self.knowledge.api_name, key.lower()
        objs = [str(c.target).split(".", 1)[0] for c in res.candidates
                if "." in str(c.target) and str(c.target).split(".", 1)[1].lower() == low]
        return [o for o in dict.fromkeys(objs) if o != own]

    def _suggest_fields(self, name: str) -> list[str]:
        """Up to five closest field API names (matching on API names and labels); with the spelling help on, the
        resolver's did-you-means first (:meth:`_close_field_names`)."""
        cands: dict[str, str] = {}
        for f in self.knowledge.fields():
            api = str(f.get("api_name"))
            for key in (api.lower(), str(f.get("label") or "").lower(), split_api_name(api)):
                if key:
                    cands.setdefault(key, api)
        matches = difflib.get_close_matches(str(name).lower(), list(cands), n=10, cutoff=0.5)
        out = _dedupe([cands[m] for m in matches])
        if len(out) < 5:  # fall back to keyword search on the field section
            for hit in self.index.search(str(name), k=5, section="field"):
                out = _dedupe(out + [hit["id"].split(":", 1)[1]])
        if self.resolver is not None:   # the spelling help: its did-you-means first, then today's list, 5 in all
            out = _dedupe(self._close_field_names(name) + out)
        return out[:5]

    def _field_brief(self, f: dict) -> dict:
        """Compact one-line view of a field used in listings."""
        sem = f.get("semantic") or {}
        row: dict[str, Any] = {
            "api_name": f.get("api_name"),
            "label": f.get("label"),
            "type": f.get("type"),
            "concept": sem.get("concept"),
            "roles": sem.get("roles") or [],
        }
        ref = f.get("reference") or {}
        if ref.get("target_objects"):
            row["targets"] = ref["target_objects"]
        flags = self.knowledge.field_flags(f)
        if flags["legacy"]:
            row["legacy"] = True
        if flags["api_name_typo"]:
            row["api_name_typo"] = True
        return row

    def _picklist_payload(self, f: dict) -> dict | None:
        pick = f.get("picklist")
        if not isinstance(pick, dict):
            return None
        values = pick.get("values")
        payload: dict[str, Any] = {
            "multi_select": str(f.get("type")).lower() == "multipicklist",
            "restricted": pick.get("restricted"),
        }
        if isinstance(values, list):
            payload["values"] = [
                {k: v for k, v in (val if isinstance(val, dict) else {"value": val}).items() if v is not None}
                for val in values
            ]
            payload["value_count"] = len(values)
        else:
            payload["values"] = None
            payload["values_description"] = values
            payload["note"] = "The knowledge file describes the values instead of listing them."
        return payload

    def _automation_row(self, kind: str, item: dict) -> dict:
        row = {"kind": kind, **{k: v for k, v in item.items() if v is not None}}
        if item.get("trigger") is None:
            row["trigger"] = None
            row["trigger_note"] = "no record-triggered start in the knowledge file (user/screen or code launched)"
        bp = item.get("business_process")
        proc = self.knowledge.process(bp) if bp else None
        row["business_process_name"] = proc.get("process_name") if proc else None
        if bp and not proc:
            row["business_process_note"] = f"process id '{bp}' is not documented in business_process_usage"
        return row

    def _match_process(self, process_id: str) -> str:
        """Resolve a process id or name (case-insensitive / fuzzy) to a canonical id."""
        known: dict[str, str] = {}
        for p in self.knowledge.business_processes():
            pid = str(p.get("process_id"))
            known[pid.lower()] = pid
            known[str(p.get("process_name") or "").lower()] = pid
        for _, item in self.knowledge.automation_items():
            bp = item.get("business_process")
            if bp:
                known.setdefault(str(bp).lower(), str(bp))
        key = process_id.lower().strip()
        if key in known:
            return known[key]
        norm = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
        if norm in known:
            return known[norm]
        close = difflib.get_close_matches(key, list(known), n=1, cutoff=0.6)
        if close:
            return known[close[0]]
        raise ToolError(
            f"no business process '{process_id}'; known: {sorted(set(known.values()))}"
        )

    # ================================================================== tool handlers
    def _describe_object(self) -> dict:
        k = self.knowledge
        obj = k.object_info
        result = {
            **{k_: v for k_, v in obj.items()},
            "counts": {
                "fields": len(k.fields()),
                "picklist_fields": sum(1 for f in k.fields() if isinstance(f.get("picklist"), dict)),
                "relationships": len(k.relationships()),
                "child_relationships": len(k.child_relationships()),
                "validation_rules": len(k.validation_rules()),
                "flows": len(k.flows()),
                "apex": len(k.apex()),
                "business_processes": len(k.business_processes()),
                "record_types": len(k.record_types()),
                "field_dependencies": len(k.field_dependencies()),
            },
            "record_types": k.record_types(),
            "field_dependencies": k.field_dependencies(),
            "knowledge_scope_note": (
                "This file holds metadata and business meaning only. It has no record data "
                "(counts, values of specific records), no sharing/ownership, page layouts or "
                "field-level security."
            ),
        }
        return {
            "result": result,
            "chunk_ids": [
                "object",
                "object.semantics",
                "object.record_grain",
                "object.discovery_summary",
                "record_types",
                "field_dependencies",
            ],
        }

    _TYPE_ALIASES: dict[str, set[str]] = {
        "lookup": {"reference"},
        "reference": {"reference"},
        "relationship": {"reference"},
        "text": {"string", "long_text_area", "rich_text_area", "textarea"},
        "string": {"string"},
        "long_text": {"long_text_area"},
        "textarea": {"long_text_area", "rich_text_area", "textarea"},
        "date": {"date"},
        "datetime": {"datetime"},
        "date_time": {"datetime"},
        "temporal": {"date", "datetime"},
        "number": {"number"},
        "numeric": {"number", "percent", "currency", "double", "integer"},
        "checkbox": {"boolean"},
        "boolean": {"boolean"},
        "picklist": {"picklist"},
        "multipicklist": {"multipicklist"},
        "multi_select_picklist": {"multipicklist"},
        "any_picklist": {"picklist", "multipicklist"},
        "rollup": {"rollup_summary"},
        "formula": {"formula"},
    }

    def _list_fields(self, type: str | None = None, role: str | None = None) -> dict:
        fields = self.knowledge.fields()
        all_types = sorted({str(f.get("type")) for f in fields})
        rows = fields
        if type:
            t = re.sub(r"[^a-z0-9]+", "_", type.lower()).strip("_")
            wanted = self._TYPE_ALIASES.get(t, {t})
            rows = [f for f in rows if str(f.get("type")).lower() in wanted]
        if role:
            r = re.sub(r"[^a-z0-9]+", "_", role.lower()).strip("_")
            rows = [
                f
                for f in rows
                if any(r in str(x).lower() for x in (f.get("semantic") or {}).get("roles") or [])
                or r in str((f.get("semantic") or {}).get("concept") or "").lower()
            ]
        result: dict[str, Any] = {
            "filters": {"type": type, "role": role},
            "count": len(rows),
            "fields": [self._field_brief(f) for f in rows],
        }
        if not rows:
            result["available_types"] = all_types
            result["available_roles"] = sorted(
                {str(x) for f in fields for x in (f.get("semantic") or {}).get("roles") or []}
            )
        return {"result": result, "chunk_ids": [f"field:{f.get('api_name')}" for f in rows]}

    def _get_field(self, name: str) -> dict:
        k = self.knowledge
        f = k.field(name)
        if f is None:
            # Standard system fields referenced by temporal_semantics are not in `fields`.
            sys_name = next((s for s in SYSTEM_FIELDS if s.lower() == name.lower()), None)
            temporal = [
                (key, v)
                for key, v in k.temporal_semantics().items()
                if isinstance(v, dict) and sys_name and str(v.get("field", "")).lower() == sys_name.lower()
            ]
            if temporal:
                return {
                    "result": {
                        "api_name": sys_name,
                        "source": "temporal_semantics (standard system field, not listed under fields)",
                        "temporal_roles": [{"key": key, **v} for key, v in temporal],
                    },
                    "chunk_ids": [f"temporal:{key}" for key, _ in temporal],
                }
            f = self._resolve_field(name)  # raises with suggestions (the spelling help may read a close name)
        api = str(f.get("api_name"))
        attrs, nulls = _compact({k_: v for k_, v in f.items() if k_ not in ("picklist", "reference")})
        result: dict[str, Any] = dict(attrs)
        chunk_ids = [f"field:{api}"]
        flags = k.field_flags(f)
        result["flags"] = flags

        pick = self._picklist_payload(f)
        if pick is not None:
            result["picklist"] = pick
            if pick.get("values"):
                chunk_ids.append(f"picklist:{api}")
        ref = f.get("reference")
        if isinstance(ref, dict):
            result["reference"] = {k_: v for k_, v in ref.items() if v is not None}
        rel = k.relationship_for_field(api)
        if rel:
            result["relationship"] = {k_: v for k_, v in rel.items() if k_ != "field" and v is not None}
            chunk_ids.append(f"relationship:{api}")
        elif isinstance(ref, dict):
            result["relationship_note"] = (
                "the relationships section has no entry for this field; cardinality and direction "
                "semantics are not specified"
            )

        usage = k.field_usage(api)
        result["read_by_automation"] = usage["read_by"]
        result["written_by_automation"] = usage["written_by"]
        rules_by_name = {r.get("name"): r for r in k.validation_rules()}
        result["validation_rules"] = [
            {
                **v,
                "condition_summary": rules_by_name.get(v["name"], {}).get("condition_summary"),
                "error_meaning": rules_by_name.get(v["name"], {}).get("error_meaning"),
            }
            for v in usage["validation_rules"]
        ]
        if usage["temporal_roles"]:
            result["temporal_roles"] = usage["temporal_roles"]
        for a in usage["read_by"] + usage["written_by"]:
            chunk_ids.append(f"{a['kind']}:{a['name']}")
        chunk_ids += [f"validation:{v['name']}" for v in usage["validation_rules"]]
        chunk_ids += [f"temporal:{t['key']}" for t in usage["temporal_roles"]]

        same = [x.get("api_name") for x in k.fields_with_label(str(f.get("label") or "")) if x is not f]
        if same:
            result["other_fields_with_same_label"] = same
        nulls = _dedupe(nulls + [a for a in _CAPABILITY_ATTRS if a not in f])
        if nulls:
            result["unspecified_in_knowledge"] = nulls
            result["unspecified_note"] = _NULL_NOTE
        if name.lower() != api.lower():
            result["resolved_from"] = name
        return {"result": result, "chunk_ids": chunk_ids}

    def _search_fields(self, query: str, k: int = 8) -> dict:
        best: dict[str, tuple[float, str]] = {}
        read_as: dict[str, str] = {}   # the spelling help's reading of misspelt query words ({} when it is off)
        for section in ("field", "picklist", "relationship"):
            hits, seen = self.index.search_read_as(query, k=max(k * 2, 10), section=section)
            read_as.update(seen)
            for hit in hits:
                api = hit["id"].split(":", 1)[1]
                if self.knowledge.field(api) is None:
                    continue
                if api not in best or hit["score"] > best[api][0]:
                    best[api] = (hit["score"], section)
        # Exact label / api hits always come first.
        exact = self.knowledge.field(query)
        if exact is not None:
            api = str(exact.get("api_name"))
            top = max([s for s, _ in best.values()] or [0.0])
            best[api] = (round(top + 1.0, 4), "exact_name")
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:k]
        rows = []
        for api, (score, where) in ranked:
            f = self.knowledge.field(api) or {}
            row = self._field_brief(f)
            row["business_meaning"] = f.get("business_meaning")
            row["score"] = score
            row["matched_in"] = where
            rows.append(row)
        result: dict[str, Any] = {"query": query, "count": len(rows), "fields": rows}
        if not rows:
            result["note"] = "no field matched; try search_knowledge or list_fields"
        if read_as:
            result["read_as"] = read_as
        return {"result": result, "chunk_ids": [f"field:{r['api_name']}" for r in rows]}

    def _get_picklist_values(self, field: str) -> dict:
        f = self._resolve_field(field)
        api = str(f.get("api_name"))
        pick = self._picklist_payload(f)
        base = {"field": api, "label": f.get("label"), "type": f.get("type")}
        if pick is None:
            return {
                "result": {
                    **base,
                    "is_picklist": False,
                    "note": (
                        f"{api} is a {f.get('type')} field, not a picklist; the knowledge file lists no "
                        "allowed values for it."
                    ),
                    "business_meaning": f.get("business_meaning"),
                },
                "chunk_ids": [f"field:{api}"],
            }
        chunk_ids = [f"field:{api}"] + ([f"picklist:{api}"] if pick.get("values") else [])
        return {"result": {**base, "is_picklist": True, **pick}, "chunk_ids": chunk_ids}

    def _relationship_row(self, r: dict) -> dict:
        api = str(r.get("field"))
        f = self.knowledge.field(api) or {}
        row = {
            "field": api,
            "label": f.get("label"),
            **{k: v for k, v in r.items() if k not in ("field", "_source") and v is not None},
        }
        if r.get("_source") == "field_reference":
            row["note"] = (
                "derived from the field's reference block; the relationships section has no entry for this "
                "field, so cardinality and direction semantics are not specified"
            )
        if r.get("relationship_name") is None:
            row["relationship_name"] = None
        if f and self.knowledge.field_flags(f)["legacy"]:
            row["legacy"] = True
        return row

    def _all_parent_relationships(self) -> list[dict]:
        """``relationships`` entries plus reference fields that have no relationships entry.

        The latter are synthesised from the field's ``reference`` block and marked
        ``_source: "field_reference"`` so callers can tell the knowledge file is incomplete.
        """
        rels = list(self.knowledge.relationships())
        listed = {str(r.get("field", "")).lower() for r in rels}
        for f in self.knowledge.fields():
            ref = f.get("reference")
            api = str(f.get("api_name"))
            if isinstance(ref, dict) and ref.get("target_objects") and api.lower() not in listed:
                rels.append(
                    {
                        "field": api,
                        "type": None,
                        "target_objects": ref.get("target_objects"),
                        "relationship_name": ref.get("relationship_name"),
                        "business_role": ref.get("business_role"),
                        "business_meaning": ref.get("business_meaning"),
                        "_source": "field_reference",
                    }
                )
        return rels

    def _get_relationships(self) -> dict:
        rels = self.knowledge.relationships()
        extra = [r for r in self._all_parent_relationships() if r.get("_source") == "field_reference"]
        result: dict[str, Any] = {
            "object": self.knowledge.api_name,
            "count": len(rels),
            "relationships": [self._relationship_row(r) for r in rels],
            "note": "relationship_name null = exact Salesforce relationship name not specified in the knowledge file",
        }
        chunk_ids = [f"relationship:{r.get('field')}" for r in rels]
        if extra:
            result["reference_fields_without_relationship_entry"] = [self._relationship_row(r) for r in extra]
            chunk_ids += [f"field:{r['field']}" for r in extra]
        return {"result": result, "chunk_ids": chunk_ids}

    def _get_child_relationships(self) -> dict:
        kids = self.knowledge.child_relationships()
        rows = [{k: v for k, v in c.items()} for c in kids]
        result = {
            "parent_object": self.knowledge.api_name,
            "count": len(rows),
            "child_relationships": rows,
        }
        return {"result": result, "chunk_ids": [self.knowledge.child_chunk_id(c) for c in kids]}

    def _relationship_profiles(self) -> list[dict]:
        """Per-relationship token sets (cached) used by ``find_relationship``.

        Each profile has ``kind`` (parent/child), ``row`` (the compact relationship dict),
        ``chunk_id``, ``phrase`` (normalised text for phrase matches) and ``facets``:
        ``[(weight, token_set), ...]`` – business role, label/api name, direction semantics,
        target/child object words, semantic concept and business meaning.
        """
        if self._rel_profiles is not None:
            return self._rel_profiles
        toks = lambda s: set(tokenize(str(s or "").replace("_", " ")))  # noqa: E731
        profiles: list[dict] = []
        for r in self._all_parent_relationships():
            api = str(r.get("field"))
            f = self.knowledge.field(api) or {}
            ds = r.get("direction_semantics") or {}
            ref = f.get("reference") or {}
            targets = " ".join(split_api_name(t) for t in r.get("target_objects") or [])
            facets = [
                (3.0, toks(r.get("business_role")) | toks(ref.get("business_role"))),
                (3.0, toks(f"{f.get('label') or ''} {split_api_name(api)}")),
                (2.0, toks(" ".join(str(v) for v in ds.values()))),
                (2.0, toks(targets)),
                (1.5, toks((f.get("semantic") or {}).get("concept"))),
                (0.75, toks(f"{r.get('business_meaning') or ''} {ref.get('business_meaning') or ''}")),
            ]
            phrase = " ".join(
                tokenize(
                    f"{r.get('business_role')} | {f.get('label')} | {' | '.join(map(str, ds.values()))} | "
                    f"{targets} | {r.get('business_meaning')} | {ref.get('business_meaning')}".replace("_", " ")
                )
            )
            exact = {
                " ".join(tokenize(str(x or "").replace("_", " ")))
                for x in (r.get("business_role"), ref.get("business_role"), f.get("label"), targets, split_api_name(api))
            }
            chunk_id = f"relationship:{api}" if r.get("_source") != "field_reference" else f"field:{api}"
            profiles.append(
                {"kind": "parent", "row": self._relationship_row(r), "chunk_id": chunk_id, "facets": facets, "phrase": phrase, "exact": exact}
            )
        for c in self.knowledge.child_relationships():
            child = str(c.get("child_object"))
            ds = c.get("direction_semantics") or {}
            facets = [
                (3.0, toks(c.get("business_role"))),
                (2.0, toks(" ".join(str(v) for v in ds.values()))),
                (2.5, toks(split_api_name(child))),
                (0.75, toks(c.get("business_meaning"))),
            ]
            phrase = " ".join(
                tokenize(f"{c.get('business_role')} | {split_api_name(child)} | {' | '.join(map(str, ds.values()))} | {c.get('business_meaning')}".replace("_", " "))
            )
            exact = {
                " ".join(tokenize(str(x or "").replace("_", " ")))
                for x in (c.get("business_role"), split_api_name(child), *ds.values())
            }
            profiles.append(
                {"kind": "child", "row": dict(c), "chunk_id": self.knowledge.child_chunk_id(c), "facets": facets, "phrase": phrase, "exact": exact}
            )
        self._rel_profiles = profiles
        return profiles

    def _find_relationship(self, concept: str) -> dict:
        """Rank parent and child relationships against a business concept.

        Score = sum over query tokens of ``idf(token) * best facet weight`` (fuzzy token
        matches count 60%), multiplied by the share of query tokens matched, plus a bonus
        when the whole (multi-word) concept appears as a phrase. IDF is computed over the
        relationships themselves, so ubiquitous words such as "interview" barely count.
        """
        q = list(dict.fromkeys(tokenize(concept)))
        if not q:
            raise ToolError("concept must contain at least one meaningful word")
        profiles = self._relationship_profiles()
        n = len(profiles)
        all_tokens = [set().union(*(t for _, t in p["facets"])) for p in profiles]
        idf = {t: math.log(1 + n / (1 + sum(1 for a in all_tokens if t in a))) for t in q}
        q_phrase = " ".join(q)

        scored: list[tuple[float, dict]] = []
        for p, vocab in zip(profiles, all_tokens):
            total, matched = 0.0, 0
            for t in q:
                best = 0.0
                for weight, facet in p["facets"]:
                    if t in facet:
                        best = max(best, weight)
                    elif len(t) > 3 and difflib.get_close_matches(t, list(facet), n=1, cutoff=0.85):
                        best = max(best, 0.6 * weight)
                if best:
                    matched += 1
                    total += idf[t] * best
            if not matched:
                continue
            score = total * (matched / len(q))
            if q_phrase in p["exact"]:  # concept is exactly a role / label / object name
                score += 1.5 * sum(idf.values())
            elif len(q) > 1 and q_phrase in p["phrase"]:
                score += 1.0 * sum(idf.values())
            scored.append((score, p))

        scored.sort(key=lambda t: -t[0])
        top = scored[:5]
        if top:  # drop weak tail matches (< 40% of the best score)
            top = [t for t in top if t[0] >= 0.4 * top[0][0]]
        matches = [{"kind": p["kind"], "match_score": round(s, 2), **p["row"]} for s, p in top]
        result: dict[str, Any] = {"concept": concept, "count": len(matches), "matches": matches}
        if not matches:
            result["note"] = "no relationship in the knowledge file matches this concept"
            result["available_parent_roles"] = [r.get("business_role") for r in self._all_parent_relationships()]
            result["available_child_roles"] = [c.get("business_role") for c in self.knowledge.child_relationships()]
        elif len(top) > 1 and abs(top[0][0] - top[1][0]) < 1e-9:
            result["note"] = "several relationships match equally; disambiguate using business_meaning"
        return {"result": result, "chunk_ids": [p["chunk_id"] for _, p in top]}

    def _get_temporal_semantics(self) -> dict:
        k = self.knowledge
        temporal = k.temporal_semantics()
        date_fields = [
            {**self._field_brief(f), "business_meaning": f.get("business_meaning")}
            for f in k.fields()
            if str(f.get("type")).lower() in ("date", "datetime")
            or any(str(r).startswith("temporal") for r in (f.get("semantic") or {}).get("roles") or [])
        ]
        result = {
            "audit_fields": {key: v for key, v in temporal.items()},
            "audit_note": "audit fields are standard Salesforce system fields (not listed under fields)",
            "date_and_time_fields": date_fields,
        }
        chunk_ids = [f"temporal:{key}" for key in temporal] + [f"field:{f['api_name']}" for f in date_fields]
        return {"result": result, "chunk_ids": chunk_ids}

    def _get_validation_rules(self, field: str | None = None) -> dict:
        rules = self.knowledge.validation_rules()
        api = None
        if field:
            api = str(self._resolve_field(field).get("api_name"))
            rules = [
                r
                for r in rules
                if any(str(x).split(".")[-1].lower() == api.lower() for x in r.get("fields_involved") or [])
            ]
        result: dict[str, Any] = {"filter_field": api, "count": len(rules), "validation_rules": rules}
        if field and not rules:
            result["note"] = f"no validation rule in the knowledge file involves {api}"
        return {"result": result, "chunk_ids": [f"validation:{r.get('name')}" for r in rules]}

    def _get_automation(self, kind: str | None = None, field: str | None = None, process_id: str | None = None) -> dict:
        k = self.knowledge
        items = k.automation_items()
        if kind:
            kk = kind.lower().strip()
            kk = {"flows": "flow", "apex_class": "apex", "class": "apex", "classes": "apex", "code": "apex"}.get(kk, kk)
            if kk not in ("flow", "apex"):
                raise ToolError("kind must be 'flow' or 'apex'")
            items = [(t, i) for t, i in items if t == kk]
        api = None
        if field:
            api = str(self._resolve_field(field).get("api_name"))
        pid = self._match_process(process_id) if process_id else None
        if pid:
            items = [(t, i) for t, i in items if i.get("business_process") == pid]
        rows = []
        for t, item in items:
            row = self._automation_row(t, item)
            if api:
                usage = k.field_usage(api)
                reads = any(a["name"] == item.get("name") for a in usage["read_by"])
                writes = any(a["name"] == item.get("name") for a in usage["written_by"])
                if not (reads or writes):
                    continue
                row["field_access"] = [x for x, ok in (("reads", reads), ("writes", writes)) if ok]
            rows.append(row)
        result: dict[str, Any] = {
            "filters": {"kind": kind, "field": api, "process_id": pid},
            "count": len(rows),
            "automation": rows,
        }
        if not rows:
            result["note"] = "no flow or apex in the knowledge file matches these filters"
        return {"result": result, "chunk_ids": [f"{r['kind']}:{r['name']}" for r in rows]}

    def _get_business_processes(self) -> dict:
        k = self.knowledge
        procs = []
        for p in k.business_processes():
            pid = p.get("process_id")
            autos = [(t, i) for t, i in k.automation_items() if i.get("business_process") == pid]
            procs.append(
                {
                    **p,
                    "automation": [{"kind": t, "name": i.get("name"), "type": i.get("type")} for t, i in autos],
                    "fields_written_by_its_automation": _dedupe(
                        [str(x) for _, i in autos for x in i.get("fields_written") or []]
                    ),
                }
            )
        documented = {p.get("process_id") for p in k.business_processes()}
        undocumented = sorted(
            {str(i.get("business_process")) for _, i in k.automation_items() if i.get("business_process") and i.get("business_process") not in documented}
        )
        result: dict[str, Any] = {"count": len(procs), "business_processes": procs}
        if undocumented:
            result["process_ids_used_by_automation_but_undocumented"] = [
                {
                    "process_id": u,
                    "automation": [i.get("name") for _, i in k.automation_items() if i.get("business_process") == u],
                }
                for u in undocumented
            ]
        return {"result": result, "chunk_ids": [f"process:{p.get('process_id')}" for p in procs]}

    def _search_knowledge(self, query: str, k: int = 8, section: str | None = None) -> dict:
        valid = set(self.index.sections()) | {"child", "object"}
        if section and section.lower() not in {s.lower() for s in valid}:
            raise ToolError(f"unknown section '{section}'; valid: {sorted(valid)}")
        hits, read_as = self.index.search_read_as(query, k=k, section=section)
        rows = [
            {"id": h["id"], "section": h["section"], "title": h["title"], "text": h["text"], "score": h["score"]}
            for h in hits
        ]
        result: dict[str, Any] = {"query": query, "section": section, "count": len(rows), "results": rows}
        if not rows:
            result["note"] = "no knowledge chunk matched this query"
        if read_as:   # the spelling help read a misspelt query word ({} when it is off)
            result["read_as"] = read_as
        return {"result": result, "chunk_ids": [r["id"] for r in rows]}

    def _get_provenance(self) -> dict:
        return {"result": dict(self.knowledge.provenance()), "chunk_ids": ["provenance"]}

    def _get_record_types(self) -> dict:
        rts = self.knowledge.record_types()
        result: dict[str, Any] = {"count": len(rts), "record_types": rts}
        if not rts:
            result["note"] = (
                "No record types are defined in this knowledge file. Provenance says record types "
                "should be verified from the runtime schema."
            )
        return {"result": result, "chunk_ids": ["record_types"]}

    # ================================================================== registration
    def _add(self, name: str, description: str, schema: dict, handler: Callable[..., dict]) -> None:
        self.tools[name] = Tool(name=name, description=description, input_schema=schema, handler=handler)

    def _register_all(self) -> None:
        """The 15 tools of :func:`sf_assistant.tools.registry.knowledge_tool_defs`, bound to this registry."""
        for d in knowledge_tool_defs(self.knowledge.api_name, self.index.sections()):
            self._add(d.name, d.description, d.input_schema, getattr(self, d.handler))


# -------------------------------------------------------------------------------------------- several objects
# moved from okeval/multi.py: one ToolRegistry per object behind one duck-typed surface (tools, openai_tools(),
# call()); used by the cli and the web page when more than one knowledge file is loaded.

class MultiToolRegistry:
    """Tool registry over several :class:`ObjectKnowledge` files.

    ``components`` (a :class:`~sf_assistant.knowledge.components.ComponentKnowledge`, or None): the flow components;
    with them the registry offers the 3 component tools (:class:`~sf_assistant.tools.component_tools.ComponentTools`:
    search_components, get_component, find_usages) after its other tools, and ``self.components`` holds them. None
    (the default): exactly the tools and results without components."""

    def __init__(self, knowledges: list[ObjectKnowledge], components: Any = None) -> None:
        if not knowledges:
            raise ValueError("no knowledge files loaded")
        self.objects: dict[str, ObjectKnowledge] = {}
        self.registries: dict[str, ToolRegistry] = {}
        all_chunks: list[dict] = []
        for kn in knowledges:
            api = kn.api_name
            if api in self.objects:
                raise ValueError(f"object '{api}' loaded twice")
            self.objects[api] = kn
            self.registries[api] = ToolRegistry(kn, KnowledgeIndex(kn.chunks()))
            label = kn.object_info.get("label") or api
            for c in kn.chunks():
                c = dict(c)
                c["id"] = f"{api}::{c['id']}"
                c["title"] = f"[{api}] {c.get('title', '')}"
                c["text"] = f"{label} {api}. {c.get('text', '')}"
                c["object"] = api
                all_chunks.append(c)
        self.index = KnowledgeIndex(all_chunks)
        self.components: ComponentTools | None = None
        if components is not None:   # flow name -> the objects whose file has an automation entry for it
            entries: dict[str, list[str]] = {}
            labels: dict[str, set[str]] = {}   # an object's label and plural label -> its API name (find_usages)
            for api, kn in self.objects.items():
                for f in kn.flows():
                    entries.setdefault(str(f.get("name")), []).append(api)
                for lab in (kn.label, (kn.raw.get("object") or {}).get("plural_label")):
                    if lab:
                        labels.setdefault(str(lab).strip().lower(), set()).add(api)
            self.components = ComponentTools(components, object_flows=entries,
                                             object_labels={k: next(iter(v)) for k, v in labels.items() if len(v) == 1},
                                             known_objects=list(self.objects))
        self._schemas: dict[str, dict] = {}
        self._own: dict[str, Callable[[dict], dict]] = {}
        self.resolver: Any = None              # the knowledge term resolver (attach_resolver), None: spelling help off
        self.resolver_lines: list[str] = []    # its start-up lines (sf_assistant.pipeline.build)
        self._build_tools()
        self.tools = {name: spec for name, spec in self._schemas.items()}

    def attach_resolver(self, resolver: Any) -> None:
        """The knowledge term resolver for these tools and for every per-object registry (:meth:`ToolRegistry.
        attach_resolver`): one read-only resolver over all loaded knowledge files, shared. The shared BM25 index of
        ``search_all_knowledge`` gets its expander too (each per-object index gets its own through the registry), and
        a misspelt ``object`` argument is read as its closest object name (step (3): :meth:`_close_object`)."""
        self.resolver = resolver
        self._resolutions = {}                 # the readings of this resolver (_spelling)
        self.index.expand = resolver_expander(resolver) if resolver is not None else None
        for reg in self.registries.values():
            reg.attach_resolver(resolver)
        if self.components is not None:   # the component cards' index reads a misspelt word too
            self.components.attach_resolver(resolver)

    # ------------------------------------------------------------------ tool schemas
    def _build_tools(self) -> None:
        """The tools of :func:`sf_assistant.tools.registry.multi_tool_defs`, then every per-object tool
        (:func:`~sf_assistant.tools.registry.multi_object_tool`). All registries share tool names and schemas, so the
        first one is used as the template."""
        names = sorted(self.objects)
        own = multi_tool_defs(names)
        self._own = {d.name: getattr(self, d.handler) for d in own}
        for d in own:
            self._schemas[d.name] = {"description": d.description, "parameters": d.input_schema}
        template = next(iter(self.registries.values()))
        for t in template.list_tools():
            d = multi_object_tool(t, names)
            self._schemas[d.name] = {"description": d.description, "parameters": d.input_schema}
        if self.components is not None:   # the component tools, last (the list before them is unchanged)
            for t in self.components.tools.values():
                self._schemas[t.name] = {"description": t.description, "parameters": t.input_schema}

    def list_tools(self) -> list[dict]:
        """MCP ``tools/list`` shape."""
        return [{"name": n, "description": s["description"], "inputSchema": s["parameters"]} for n, s in self._schemas.items()]

    def openai_tools(self) -> list[dict]:
        """OpenAI chat-completions ``tools`` payload."""
        return [{"type": "function", "function": {"name": n, "description": s["description"], "parameters": s["parameters"]}}
                for n, s in self._schemas.items()]

    # ------------------------------------------------------------------ dispatch
    @staticmethod
    def _fail(msg: str) -> dict:
        return {"ok": False, "result": None, "error": msg, "chunk_ids": []}

    def _resolve_object(self, name: Any) -> str | None:
        if not name:
            return None
        s = str(name).strip()
        for api in self.objects:
            if api.lower() == s.lower() or str(self.objects[api].object_info.get("label") or "").lower() == s.lower():
                return api
        return None

    def _close_object(self, name: Any) -> tuple[str | None, list[str]]:
        """Step (3) for an ``object`` argument no exact tier of :meth:`_resolve_object` knows (the spelling help on):
        ``(API name to use, [])`` for one ``corrected`` reading inside a tool call (recorded in
        :data:`ARGUMENT_READ_AS`), else ``(None, did-you-means)``: the objects of a ``corrected`` reading outside a
        call, of an ``ambiguous`` one or of an ``exact`` one the tiers missed (plurals, fillers), at most
        :data:`_DID_YOU_MEAN`; ``(None, [])`` for ``none``, a blank name or the switch off (today's error).
        ``find_links_to_object`` keeps the exact tiers only."""
        s = str(name or "").strip()
        if self.resolver is None or not s:
            return None, []
        res = _spelling(self, s, ("object",))
        if res is None:
            return None, []
        apis = [t for t in res.targets if t in self.objects]
        if res.decision == "corrected" and len(apis) == 1 and _in_call():
            _record_read_as(s, apis[0], "object", apis[0], "closest object name", arg="object")
            return apis[0], []
        return None, (apis[:_DID_YOU_MEAN] if res.decision in _SUGGEST else [])

    def _did_you_mean(self, apis: list[str]) -> str:
        """`` (did you mean Gadget__c (Gadget) or ...?)`` for an unknown-object error, or '' without names."""
        if not apis:
            return ""
        names = []
        for a in apis:
            label = str(self.objects[a].object_info.get("label") or "")
            names.append(f"{a} ({label})" if label and label != a else a)
        return f" (did you mean {_or_names(names)}?)"

    def call(self, name: str, arguments: dict | None = None) -> dict:
        """Run a tool. Never raises. Same return shape as :meth:`ToolRegistry.call`. With the spelling help on, the
        call (and the per-object registry's call inside it) runs with one correction channel, and an object or field
        argument read as its closest name is stated (``read_as``; :func:`_with_read_as`)."""
        if self.resolver is not None and ARGUMENT_READ_AS.get() is None:
            return _call_reading(self.call, name, arguments)
        args = dict(arguments or {})
        try:
            own = self._own.get(name)   # list_objects, search_all_knowledge, find_links_to_object
            if own is not None:
                return own(args)
            if self.components is not None and name in self.components.tools:
                return self.components.call(name, args)
            if name not in self._schemas:
                close = difflib.get_close_matches(str(name), list(self._schemas), n=3, cutoff=0.4)
                return self._fail(f"unknown tool '{name}'; available: {sorted(self._schemas)}" + (f"; closest: {close}" if close else ""))
            return self._object_tool(name, args)
        except Exception as exc:  # defensive: a tool must never crash the agent loop
            return self._fail(f"{type(exc).__name__}: {exc}")

    def _list_objects(self, args: dict) -> dict:
        return {"ok": True, "error": None, "chunk_ids": [], "result": {"objects": [self._object_row(api) for api in sorted(self.objects)]}}

    def _object_tool(self, name: str, args: dict) -> dict:
        """A per-object tool: the call goes to the registry of ``args["object"]``."""
        raw_obj = args.pop("object", None)
        api = self._resolve_object(raw_obj)
        if api is None:
            api, dym = self._close_object(raw_obj)   # (None, []) with the spelling help off
            if api is None:
                return self._fail(f"unknown or missing object '{raw_obj}'{self._did_you_mean(dym)}; valid objects: "
                                  f"{sorted(self.objects)}")
        out = self.registries[api].call(name, args)
        out["chunk_ids"] = [f"{api}::{c}" for c in out.get("chunk_ids") or []]
        if out.get("ok") and isinstance(out.get("result"), dict):
            out["result"] = {"object": api, **out["result"]}
        return out

    def _object_row(self, api: str) -> dict:
        info = self.objects[api].object_info
        summary = info.get("discovery_summary") or {}
        return {"object": api, "label": info.get("label"), "one_line": summary.get("one_line") or info.get("description"),
                "key_concepts": summary.get("key_concepts") or []}

    def _search_all(self, args: dict) -> dict:
        query = str(args.get("query") or "").strip()
        if not query:
            return self._fail("search_all_knowledge needs a non-empty 'query'")
        k = int(args.get("k") or 8)
        api = None
        if args.get("object"):
            api = self._resolve_object(args["object"])
            if api is None:
                api, dym = self._close_object(args["object"])   # (None, []) with the spelling help off
                if api is None:
                    return self._fail(f"unknown object '{args['object']}'{self._did_you_mean(dym)}; valid objects: "
                                      f"{sorted(self.objects)}")
        hits, read_as = self.index.search_read_as(query, k=k * 4 if api else k)
        if api:
            hits = [h for h in hits if h.get("object") == api][:k]
        rows = [{"object": h["object"], "id": h["id"], "section": h["section"], "title": h["title"],
                 "text": h["text"], "score": h["score"]} for h in hits]
        result: dict[str, Any] = {"query": query, "count": len(rows), "results": rows}
        if not rows:
            result["note"] = "no knowledge chunk matched this query"
        if read_as:   # the spelling help read a misspelt query word ({} when it is off)
            result["read_as"] = read_as
        return {"ok": True, "error": None, "result": result, "chunk_ids": [r["id"] for r in rows]}

    def _links_to(self, args: dict) -> dict:
        target = str(args.get("target") or "").strip()
        if not target:
            return self._fail("find_links_to_object needs a non-empty 'target'")
        rows, chunk_ids = [], []
        for api in sorted(self.objects):
            for r in self.objects[api].relationships():
                if target.lower() in (str(t).lower() for t in r.get("target_objects") or []):
                    rows.append({"from_object": api, "field": r.get("field"), "type": r.get("type"),
                                 "relationship_name": r.get("relationship_name"),
                                 "business_meaning": r.get("business_meaning")})
                    chunk_ids.append(f"{api}::relationship:{r.get('field')}")
        result: dict[str, Any] = {"target": target, "count": len(rows), "links": rows,
                                  "has_own_knowledge_file": self._resolve_object(target) is not None}
        if not rows:
            result["note"] = f"no loaded object has a lookup or master-detail to {target}"
        return {"ok": True, "error": None, "result": result, "chunk_ids": chunk_ids}


# ------------------------------------------------------------------------------------------------ spelling help
# docs/resolver_integration.md (1) and (6): the question's misspelt names as both agents' hints show them, and the
# answer's "Read as" line. Only called with a term resolver (on by default); nothing here runs with SFA_RESOLVER=off.

#: The spelling section of both agents' question hints (``<<LINES>>``: one :meth:`SpellingSpan.line` each).
SPELLING_HINTS = _prompt_files.text("spelling_hints")
#: The answer's code line for the corrected spellings it rests on (``<<PAIRS>>``: ``'typed' as used; ...``).
READ_AS_LINE = _prompt_files.text("read_as_line")

#: Value sources that are the snapshot's stored values: the knowledge side never sees them.
STORED_VALUE_SOURCES = ("stored", "stored_text")
#: Did-you-means listed on a "may mean" line, and names per kind of target on a corrected line (two kinds at most).
SPELLING_OPTIONS = 4
SPELLING_TARGETS = 2

_KIND_WORDS = {"object": "object", "field": "field", "value": "value of", "record_type": "record type of",
               "component": ""}
_SOURCE_LINE_RE = re.compile(r"source\s*:")


@dataclass(frozen=True)
class SpellingSpan:
    """One misspelt word or phrase of a question as the hints show it: ``typed`` (``question[start:end]``) and the
    resolver's ``decision``: ``corrected`` (one clear spelling, ``used``, with the metadata ``targets`` it names,
    ``(kind, target)`` best first) or ``ambiguous`` (the did-you-means ``options``)."""

    typed: str
    start: int
    end: int
    decision: str
    used: str | None = None
    options: tuple[str, ...] = ()
    targets: tuple[tuple[str, str], ...] = ()

    def line(self) -> str:
        """``- 'accont' -> Account (object Account)``, or ``- 'persn' may mean: A, B (say which one you used, or
        ask)``."""
        if self.decision == "corrected":
            what = _targets_text(self.targets)
            return f"- '{self.typed}' -> {self.used}" + (f" ({what})" if what else "")
        return (f"- '{self.typed}' may mean: {', '.join(self.options[:SPELLING_OPTIONS])} "
                "(say which one you used, or ask)")

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe form (the typed words, the decision and the spellings; no record)."""
        return {"typed": self.typed, "decision": self.decision, "used": self.used, "options": list(self.options)}


def _target_name(kind: str, target: str) -> str:
    if kind == "value":
        return target.split("=", 1)[0]                  # Obj.Field=value -> the field that holds the value
    if kind == "record_type":
        return target.split(".RecordType=", 1)[0]       # Obj.RecordType=Name -> the object
    return target


def _targets_text(targets: Iterable[tuple[str, str]]) -> str:
    """``object Account`` (a spelling that names an object: the object, not the lookups named after it); else
    ``field A.B, C.D; value of E.F``: the first two kinds, :data:`SPELLING_TARGETS` names each."""
    targets = list(targets)
    if any(kind == "object" for kind, _t in targets):
        targets = [(kind, t) for kind, t in targets if kind == "object"]
    groups: dict[str, list[str]] = {}
    for kind, target in targets:
        names = groups.setdefault(kind, [])
        name = _target_name(kind, str(target))
        if name not in names:
            names.append(name)
    parts = []
    for kind, names in list(groups.items())[:2]:
        shown = ", ".join(names[:SPELLING_TARGETS]) + (", ..." if len(names) > SPELLING_TARGETS else "")
        parts.append(f"{_KIND_WORDS.get(kind, kind)} {shown}".strip())
    return "; ".join(parts)


def _metadata_targets(resolver: Any, text: str, stored_values: bool) -> tuple[tuple[str, str], ...]:
    """``(kind, target)`` of the metadata terms spelt exactly ``text`` (no person name; no stored value unless
    ``stored_values``), best first; () when no such term exists."""
    res = resolver.resolve_term(text, kinds=METADATA_KINDS)
    if res.decision != "exact":
        return ()
    return tuple(dict.fromkeys((c.kind, c.target) for c in res.candidates
                               if stored_values or not (c.kind == "value" and c.source in STORED_VALUE_SOURCES)))


def _hint_span(resolver: Any, c: Any, stored_values: bool) -> SpellingSpan | None:
    if c.decision == "corrected" and c.suggestion:
        targets = _metadata_targets(resolver, c.suggestion, stored_values)
        return SpellingSpan(c.text, c.start, c.end, "corrected", c.suggestion, (), targets) if targets else None
    if c.decision == "ambiguous":
        options = tuple(s for s in dict.fromkeys(str(x) for x in c.suggestions or () if x)
                        if _metadata_targets(resolver, s, stored_values))
        return SpellingSpan(c.text, c.start, c.end, "ambiguous", None, options) if options else None
    return None


def question_spans(resolver: Any, question: str, *, stored_values: bool = True,
                   corrections: Iterable[Any] | None = None) -> list[SpellingSpan]:
    """The misspelt words and phrases of ``question`` that the hints list, left to right: the ``corrected`` and
    ``ambiguous`` spans of ``resolver.spell_correct(question)`` (or of the given ``corrections`` of it) whose
    spellings name metadata: an object, a field, a value, a record type or a component, never a person name (the
    record agent's people lines and the answer's lead sentence cover people). ``stored_values`` False (the knowledge
    side): a value stored in the snapshot does not count, a picklist value does. A did-you-mean keeps only such
    spellings, and a span with none is left out. [] without a resolver; never raises."""
    if resolver is None:
        return []
    try:
        found = list(resolver.spell_correct(question) if corrections is None else corrections)
    except Exception:  # noqa: BLE001 - the spelling help is optional
        return []
    out: list[SpellingSpan] = []
    for c in found:
        try:
            span = _hint_span(resolver, c, stored_values)
        except Exception:  # noqa: BLE001
            span = None
        if span is not None:
            out.append(span)
    return out


def spelling_section(spans: Iterable[SpellingSpan]) -> str:
    """The "Spelling" section of the hints (:data:`SPELLING_HINTS`, one line per span), or '' without spans."""
    lines = [s.line() for s in spans]
    return _prompt_files.fill(SPELLING_HINTS, LINES="\n".join(lines)) if lines else ""


def corrected_text(question: str, spans: Iterable[SpellingSpan]) -> str:
    """``question`` with every ``corrected`` span replaced by its spelling (``how does backgrond chech work`` ->
    ``how does Background Check work``): what the record hints search objects and stored values with."""
    out = str(question or "")
    for s in sorted((x for x in spans if x.decision == "corrected" and x.used), key=lambda x: -x.start):
        if out[s.start:s.end] == s.typed:
            out = out[:s.start] + str(s.used) + out[s.end:]
    return out


def read_as_pairs(spans: Iterable[SpellingSpan] = (), tool_read_as: Iterable[Any] = ()) -> list[tuple[str, str]]:
    """``(typed, used)`` once per typed text (case aside), first come first: the ``corrected`` spans, then the
    ``read_as`` entries of tool results (``{"typed", "used", "kind", ...}`` dicts). A person entry is left out (a
    person's correction is the answer's lead sentence), and so is an entry that only changes the case."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(typed: Any, used: Any) -> None:
        t, u = " ".join(str(typed or "").split()), " ".join(str(used or "").split())
        if t and u and t.lower() != u.lower() and t.lower() not in seen:
            seen.add(t.lower())
            out.append((t, u))

    for s in spans:
        if s.decision == "corrected":
            add(s.typed, s.used)
    for x in tool_read_as:
        if isinstance(x, dict) and x.get("kind") != "person":
            add(x.get("typed"), x.get("used"))
    return out


def _says(text: str, words: str) -> bool:
    t = " ".join(str(text or "").lower().split())
    w = " ".join(str(words or "").lower().split())
    return bool(w) and re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", t) is not None


def says_words(text: str, words: str) -> bool:
    """``text`` holds ``words`` as whole words (case and spacing aside): what the "Read as" line and the person lead
    sentence take for "the answer already names it"."""
    return _says(text, words)


def _is_source_line(line: str) -> bool:
    """A last "Source: ..." line in any bullet or bold form (as ``sf_assistant.agent.checks`` reads it)."""
    return _SOURCE_LINE_RE.match(re.sub(r"[*_`]+", "", line or "").strip().lstrip("-> ").strip().lower()) is not None


def with_read_as_line(text: str, pairs: Iterable[tuple[str, str]]) -> str:
    """``text`` with one code line ``Read as: 'accont' as Account; 'backgrond chech' as Background Check.``
    (:data:`READ_AS_LINE`) for the ``(typed, used)`` pairs it does not state already (a pair is stated when the text
    names both spellings), before a last ``Source:`` line, else at the end. Unchanged for an empty text or when no
    pair is left."""
    if not (text or "").strip():
        return text
    keep = [(t, u) for t, u in pairs if not (_says(text, t) and _says(text, u))]
    if not keep:
        return text
    line = _prompt_files.fill(READ_AS_LINE, PAIRS="; ".join(f"'{t}' as {u}" for t, u in keep))
    lines = text.rstrip().split("\n")
    if _is_source_line(lines[-1]):
        return "\n".join(lines[:-1] + [line, lines[-1]])
    return "\n".join(lines + [line])
