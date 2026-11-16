"""The component tools: search_components, get_component and find_usages over one :class:`~sf_assistant.knowledge.
components.ComponentKnowledge` (the flow components of knowledge/components; format review A01, A02, A07, A17, A18).

:class:`ComponentTools` keeps a components-only BM25 index (:class:`~sf_assistant.knowledge.search.KnowledgeIndex`,
the same backend as the object knowledge, ``rank_bm25``) with one card per component, and the three tools of
:func:`sf_assistant.tools.registry.component_tool_defs`. :class:`~sf_assistant.tools.knowledge_tools.MultiToolRegistry`
offers them to the full knowledge agent (and so to the MCP server) after its own tools when a components folder is
loaded; the one-object registry and the lean step do not offer them. ``call`` never raises and returns the envelope of
the knowledge tools: ``{"ok", "result", "error", "chunk_ids"}`` (chunk ids ``component::<kind>:<ApiName>``).

Budgets (:data:`sf_assistant.config.LIMITS`, all well under the 16,000-character cut of a tool result):

* ``search_components``: one card per component, at most ``k`` (default 5, at most 10), the result at most
  ``component_search_chars`` (4,000; about 5 cards): the lowest cards are left out; ``more`` counts the other hits
  that score close to the best one (at least :data:`MORE_SCORE_SHARE` of its score), so it is not there when the
  rest only share a word;
* ``get_component``: ``detail="summary"`` (default) at most ``component_summary_chars`` (3,500): the parts that do
  not fit are shortened, least needed first (the dev10 notes, the meaning, the caveats, the lists, and the effect
  lines last; folded effects still name their recipients), and the summary names the detail level that holds them in
  full; ``effects`` (one line per effect: what it creates, updates or sends, to whom, on which path and route),
  ``logic`` (the effects in full first, then the start, the paths and the elements), ``fields`` (every field read or
  written, with the other writers of each written one) and ``caveats`` (the state notes, the meaning, the production
  caveats, then the dev10 notes) come in pages of at most ``component_page_chars`` (6,000) with ``page``, ``pages``
  and ``next_page``; an item larger than a page is split into parts (``part``: ``"i/n"``);
* ``find_usages``: at most ``component_usage_rows`` (40) compact rows, then ``more``, and at most
  ``component_usage_chars`` (9,000); always with ``not_covered`` (Apex bodies, launch points, the flows left out).
  A writer row says how the flow sets the field: ``values`` (the literal values) and ``value_from`` (a variable,
  another field or a formula: not fixed in the flow). With ``value`` the rows are the writers of that literal value;
  ``values_not_fixed`` lists the writers that set the field from a variable, a field or a formula (they may write
  the value too), ``values_unknown`` the Apex and not-built writers (not scanned), and ``writes_other_values`` the
  writers of other literal values only (review H1). An object target may be its label (``Interview``); the result
  then says ``resolved_from``.

Evidence (owner rule): the tools read only the public view of the files (``ComponentKnowledge`` strips ``evidence``
when it loads them), and the search chunks never hold it, so no tool result can carry it. The curated text names no
source document (``kb_build/flows/merge_flows.py`` refuses one).

Production facts first: a summary opens with the state (and a warning when the facts describe a version that does not
run), and a caveat that starts with ``dev10`` is a dev10 note, listed after the production caveats under
``dev10_notes``.
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Callable

from sf_assistant.config import LIMITS
from sf_assistant.knowledge.components import (ComponentKnowledge, effect_line, effects_summary, start_phrase,
                                               strip_evidence, value_sources)
from sf_assistant.knowledge.search import KnowledgeIndex, resolver_expander
from sf_assistant.tools.registry import COMPONENT_DETAILS, COMPONENT_KINDS, USAGE_ROLES, Tool, component_tool_defs

__all__ = ["ComponentTools", "ComponentToolError", "SEARCH_CHARS", "SUMMARY_CHARS", "PAGE_CHARS", "USAGE_ROWS",
           "USAGE_CHARS", "dev10_note", "split_caveats"]

SEARCH_CHARS = LIMITS.component_search_chars     # 4,000
SUMMARY_CHARS = LIMITS.component_summary_chars   # 3,500
PAGE_CHARS = LIMITS.component_page_chars         # 6,000
USAGE_ROWS = LIMITS.component_usage_rows         # 40
USAGE_CHARS = LIMITS.component_usage_chars       # 9,000

#: The order of the start types in find_usages rows (A17: before save, after save, scheduled, then the others).
_START_ORDER = {"before_save": 0, "after_save": 1, "scheduled": 2, "platform_event": 3, "autolaunched": 4, "screen": 5}
_WRITE_OPS = ("create", "update", "upsert", "delete", "write")
_DEV10_RE = re.compile(r"^\s*dev10\b", re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z(`'\"])")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
ORDER_NOTE = ("The components record no trigger order: flows of the same timing on one object run in no order you can "
              "rely on.")
EXCLUDED_NOTE = "no component file: the knowledge holds no facts of this flow beyond this line"
MORE_ROWS_NOTE = "more rows: narrow the role or the target"
#: search_components: a hit counts towards ``more`` when it scores at least this share of the best hit's score.
MORE_SCORE_SHARE = 0.6
#: The get_component detail levels that come in pages (every level but the summary).
PAGED_DETAILS = tuple(d for d in COMPONENT_DETAILS if d != "summary")
#: A screen or autolaunched flow's ``launched_from`` (review L1: the meaning may name a launch point from the
#: documentation; ``called_by`` lists the flows that run it as a subflow).
LAUNCH_NOTE = {"screen": "launch points (buttons, quick actions, pages) are not scanned from the metadata; the business "
                         "meaning may name one",
               "autolaunched": "Apex callers and launch points (buttons, quick actions, pages) are not scanned from the "
                               "metadata; the business meaning may name them"}
CALLED_BY_NOTE = "; called_by lists the flows that run it as a subflow"
VALUE_NOTE = ("no flow component writes the literal value {value} to {target}; the writers under values_not_fixed set it "
              "from a variable, another field or a formula and may write {value}, and those under values_unknown are "
              "not scanned")


class ComponentToolError(Exception):
    """A user-level tool error (bad argument, unknown name). Becomes ``ok: False`` with the message."""


def _size(obj: Any) -> int:
    """Characters of ``obj`` as the model sees it (``json.dumps``, ``ensure_ascii=False``, as the agent sends it)."""
    return len(json.dumps(obj, ensure_ascii=False, default=str))


def _short(text: Any, limit: int) -> str:
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    return t[: max(0, limit - 3)].rsplit(" ", 1)[0].rstrip(",;:") + "..."


def _first_sentence(text: str, limit: int = 220) -> str:
    first = _SENTENCE_RE.split(" ".join(str(text or "").split()), 1)[0]
    return _short(first, limit)


def dev10_note(caveat: Any) -> bool:
    """A caveat that starts with ``dev10``: a note about the dev10 sandbox copy, not about production."""
    return bool(_DEV10_RE.match(str(caveat or "")))


def split_caveats(caveats: Any) -> tuple[list[str], list[str]]:
    """``(production caveats, dev10 notes)`` in file order (:func:`dev10_note`)."""
    prod, dev10 = [], []
    for c in caveats or []:
        (dev10 if dev10_note(c) else prod).append(str(c))
    return prod, dev10


# ------------------------------------------------------------------------------------------------ paging helpers
def _cut_string(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    keep = max(0, budget - 40)
    return text[:keep] + f"... [cut: {len(text) - keep} more characters]"


#: The keys that name an item; a continued part carries them (and only them) besides its share of the split value.
_IDENT_KEYS = ("kind", "type", "element", "api_name", "object", "path", "field", "name", "outcome")


def _split_item(item: dict, budget: int, depth: int = 0) -> list[dict]:
    """``item`` as parts that each fit ``budget`` characters. Its largest list (or dict) value is spread: the first
    part holds every other key and as many entries as fit, each further part the item's naming keys
    (:data:`_IDENT_KEYS`) and the next entries. An entry too large for any part is split the same way (a string is
    cut: ``[cut: N more characters]``). Parts get ``part`` ``"i/n"`` when there are several."""
    out = _split(item, budget - 14, depth)
    if len(out) > 1:
        n = len(out)
        out = [{**p, "part": f"{i}/{n}"} for i, p in enumerate(out, 1)]
    return out


def _split(item: dict, budget: int, depth: int) -> list[dict]:
    if _size(item) <= budget or depth > 8:
        return [item]
    containers = [(k, v) for k, v in item.items() if isinstance(v, (list, dict)) and len(v) > 0]
    if not containers:
        strings = [(k, v) for k, v in item.items() if isinstance(v, str)]
        if not strings:
            return [item]
        k, v = max(strings, key=lambda kv: len(kv[1]))
        rest = _size({**item, k: ""})
        return [{**item, k: _cut_string(v, max(40, budget - rest))}]
    key, value = max(containers, key=lambda kv: _size(kv[1]))
    is_dict = isinstance(value, dict)
    entries = list(value.items()) if is_dict else list(value)
    pack = (lambda chunk: dict(chunk)) if is_dict else (lambda chunk: list(chunk))
    head = {k: v for k, v in item.items() if k != key}
    ident = {k: item[k] for k in _IDENT_KEYS if k in item and k != key and not isinstance(item[k], (list, dict))}
    parts: list[dict] = []
    if _size({**head, key: pack([])}) > budget:   # the other keys alone are too large: split them first
        parts += _split(head, budget, depth + 1)
        base = ident
    else:
        base = head
    chunk: list = []
    for entry in entries:
        if _size({**base, key: pack(chunk + [entry])}) <= budget:
            chunk.append(entry)
            continue
        if chunk:
            parts.append({**base, key: pack(chunk)})
            base = ident
            chunk = []
        if _size({**base, key: pack([entry])}) <= budget:
            chunk = [entry]
            continue
        # one entry alone is too large: split inside it
        room = budget - _size({**base, key: pack([])}) - 8
        if is_dict:
            name, val = entry
            inner = _split({"v": val}, room - len(str(name)) - 8, depth + 1) if isinstance(val, (list, dict)) \
                else [{"v": _cut_string(str(val), max(40, room - len(str(name)) - 16))}]
            parts += [{**base, key: {name: x.get("v")}} for x in inner]
        elif isinstance(entry, dict):
            parts += [{**base, key: [x]} for x in _split(entry, room, depth + 1)]
        else:
            parts.append({**base, key: [_cut_string(str(entry), max(40, room - 8))]})
        base = ident
    if chunk:
        parts.append({**base, key: pack(chunk)})
    return parts


def _paginate(header: dict, items: list[tuple[str, Any]], budget: int) -> list[dict]:
    """Pack ``(section, item)`` pairs, in order, into pages whose JSON (``header`` + ``page``/``pages``/``next_page``
    + ``content``) is at most ``budget`` characters; ``content`` maps each section to its items on that page."""
    probe = {**header, "page": 999, "pages": 999, "next_page": 999}
    room = budget - _size({**probe, "content": {}})
    pieces: list[tuple[str, Any]] = []
    for section, item in items:
        wrapped = item if isinstance(item, dict) else {"value": item}
        limit = room - len(section) - 12
        for part in (_split_item(wrapped, limit) if _size(wrapped) > limit else [wrapped]):
            pieces.append((section, part if isinstance(item, dict) else part.get("value")))
    pages: list[dict] = []
    content: dict[str, list] = {}
    for section, piece in pieces:
        trial = {k: list(v) for k, v in content.items()}
        trial.setdefault(section, []).append(piece)
        if content and _size({**probe, "content": trial}) > budget:
            pages.append(content)
            content = {section: [piece]}
        else:
            content = trial
    if content or not pages:
        pages.append(content)
    n = len(pages)
    return [{**header, "page": i, "pages": n, "next_page": i + 1 if i < n else None, "content": c}
            for i, c in enumerate(pages, 1)]


# ------------------------------------------------------------------------------------------------ the tools
class ComponentTools:
    """The component tools over one :class:`ComponentKnowledge` (read-only; one instance serves every thread).

    ``object_flows``: flow API name -> the objects whose knowledge file has an automation entry for it (the full agent's
    registry passes it), so a summary names the object files that describe the same flow. ``object_labels``: an
    object's label or plural label (any case) -> its API name, from the loaded object knowledge (the registry passes
    it), so find_usages takes ``Interview`` for ``Interview__c``; ``known_objects``: every object API name of the
    loaded object knowledge (an object no flow acts on is then a known object, not an unknown target)."""

    def __init__(self, knowledge: ComponentKnowledge, object_flows: dict[str, list[str]] | None = None,
                 object_labels: dict[str, str] | None = None, known_objects: Any = None) -> None:
        self.knowledge = knowledge
        self.index = KnowledgeIndex(knowledge.chunks())
        self.object_flows = {k: sorted(set(v)) for k, v in (object_flows or {}).items()}
        self.resolver: Any = None
        self._pages: dict[tuple[str, str], list[dict]] = {}
        self.tools: dict[str, Tool] = {d.name: d.bind(self) for d in component_tool_defs()}
        self._lower_objects = {o.lower(): o for o in self._objects()}
        self._lower_fields = {f.lower(): f for f in self._fields()}
        self._known_objects = {str(o).lower(): str(o) for o in (known_objects or [])}
        self._object_labels = {str(k).strip().lower(): str(v) for k, v in (object_labels or {}).items() if k}

    def attach_resolver(self, resolver: Any) -> None:
        """The spelling help (``SFA_RESOLVER=on``): the card index reads a misspelt query word as it reads
        (``read_as``), as the object search does. None: off."""
        self.resolver = resolver
        self.index.expand = resolver_expander(resolver) if resolver is not None else None

    # ================================================================== public API
    def list_tools(self) -> list[dict]:
        return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in self.tools.values()]

    def openai_tools(self) -> list[dict]:
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}} for t in self.tools.values()]

    def call(self, name: str, arguments: Any = None) -> dict:
        """Run one component tool. Never raises; ``{"ok", "result", "error", "chunk_ids"}``."""
        tool = self.tools.get(name)
        if tool is None:
            close = difflib.get_close_matches(str(name), list(self.tools), n=3, cutoff=0.4)
            return self._fail(f"unknown tool '{name}'; available: {sorted(self.tools)}"
                              + (f"; closest: {close}" if close else ""))
        try:
            out = tool.handler(**self._arguments(tool, arguments))
            result = strip_evidence(out.get("result"))   # a second guard: nothing the files hold as evidence
            json.dumps(result)
            return {"ok": True, "result": result, "error": None,
                    "chunk_ids": list(dict.fromkeys(c for c in out.get("chunk_ids") or [] if c))}
        except ComponentToolError as exc:
            return self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - a tool must never crash the agent loop
            return self._fail(f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _fail(msg: str) -> dict:
        return {"ok": False, "result": None, "error": msg, "chunk_ids": []}

    @staticmethod
    def _arguments(tool: Tool, arguments: Any) -> dict:
        """The knowledge tools' argument rules: JSON text or a dict, unknown keys ignored, null and '' dropped,
        integers coerced and clamped, strings stripped, enums checked, required ones enforced."""
        if arguments is None:
            arguments = {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise ComponentToolError(f"arguments are not valid JSON: {exc}") from None
        if not isinstance(arguments, dict):
            raise ComponentToolError("arguments must be a JSON object")
        kwargs: dict[str, Any] = {}
        for key, spec in (tool.input_schema.get("properties") or {}).items():
            val = arguments.get(key)
            if val is None or val == "":
                continue
            if spec.get("type") == "integer":
                try:
                    val = int(val)
                except (TypeError, ValueError):
                    raise ComponentToolError(f"argument '{key}' must be an integer") from None
                val = max(spec.get("minimum", val), min(spec.get("maximum", val), val))
            elif spec.get("type") == "string":
                if isinstance(val, (list, dict)):
                    raise ComponentToolError(f"argument '{key}' must be a string")
                val = str(val).strip()
                if spec.get("enum") and val.lower() not in [e.lower() for e in spec["enum"]]:
                    raise ComponentToolError(f"argument '{key}' must be one of {spec['enum']}")
                if spec.get("enum"):
                    val = val.lower()
            kwargs[key] = val
        missing = [r for r in tool.input_schema.get("required", []) if r not in kwargs]
        if missing:
            raise ComponentToolError(f"missing required argument(s) {missing} for {tool.name}")
        return kwargs

    # ================================================================== names
    def _component(self, name: str, kind: str):
        c, how = self.knowledge.resolve(name, kind)
        if c is not None:
            return c, how
        if how.get("left_out"):
            return None, how
        if how.get("ambiguous"):
            raise ComponentToolError(f"'{name}' is an alias of several flows: {self._names(how['ambiguous'])}; give "
                                     "the API name")
        dym = list(how.get("did_you_mean") or [])
        if len(dym) < 3:
            dym += [h["data"]["component"] for h in self.index.search(name, k=3, section=kind)
                    if h["data"]["component"] not in dym]
        raise ComponentToolError(f"no {kind} named '{name}'" + (f"; did you mean {self._names(dym[:5])}?" if dym else "")
                                 + "; search_components finds a flow by what it does")

    def _names(self, cids: list[str]) -> str:
        return ", ".join(f"{c} ({self.knowledge.components[c].title})" if c in self.knowledge.components else c
                         for c in cids)

    # ================================================================== search_components
    #: The search card as the result shows it (shorter than the indexed text, which keeps the full card).
    CARD_SHOWN = {"runs_chars": 200, "effects_chars": 200, "aliases": 6}

    def _search_components(self, query: str, kind: str | None = None, k: int = 5) -> dict:
        k = max(1, int(k))
        hits, read_as = self.index.search_read_as(query, k=k + 10, section=kind)   # the extra hits only count as more
        cards: list[dict] = []
        chunk_ids: list[str] = []
        for h in hits[:k]:
            cards.append({**self.knowledge.card(h["data"]["component"], **self.CARD_SHOWN), "score": h["score"]})
            chunk_ids.append(h["id"])
        left = self._left_out_matches(query)
        close = MORE_SCORE_SHARE * hits[0]["score"] if hits else 0.0
        while True:
            result = {"query": query, "kind": kind or "flow", "count": len(cards), "results": cards}
            more = sum(1 for h in hits[len(cards):] if h["score"] >= close)
            if more:   # review L3: only hits that score close to the best one, so a shared word is no "more"
                result["more"] = more
                result["note"] = ("other cards score close to these: refine the words (a larger k does not fit more "
                                  "cards), or call get_component")
            if left:
                result["left_out"] = left
            if not cards:
                result["note"] = "no flow component matched these words"
                result["coverage"] = self.knowledge.coverage()
            if read_as:
                result["read_as"] = read_as
            if _size(result) <= SEARCH_CHARS or len(cards) <= 1:
                break
            cards, chunk_ids = cards[:-1], chunk_ids[:-1]
        return {"result": result, "chunk_ids": chunk_ids}

    def _left_out_matches(self, query: str) -> list[dict]:
        """Excluded or not-built flows whose API name the query names (as written or in words)."""
        q = " " + " ".join(re.sub(r"[^a-z0-9]+", " ", query.lower()).split()) + " "
        out = []
        for ref in list(self.knowledge.index.get("excluded") or {}) + list(self.knowledge.index.get("not_built") or {}):
            api = ref.split(":", 1)[1]
            words = " ".join(re.sub(r"[^a-z0-9]+", " ", api.lower()).split())
            if len(words.split()) >= 2 and f" {words} " in q or api.lower() in query.lower():
                out.append(self.knowledge.left_out(ref))
        return out[:5]

    # ================================================================== get_component
    def _get_component(self, name: str, kind: str = "flow", detail: str = "summary", page: int = 1) -> dict:
        kind = kind or "flow"
        if kind not in COMPONENT_KINDS:
            raise ComponentToolError(f"kind must be one of {COMPONENT_KINDS}")
        c, how = self._component(name, kind)
        if c is None:   # an excluded or not-built flow: what the index says, nothing more
            left = dict(how["left_out"])
            left["note"] = EXCLUDED_NOTE
            if how.get("did_you_mean"):   # review L2: a built flow of a close name (Interview_Creation_V2)
                left["did_you_mean"] = [self._names([x]) for x in how["did_you_mean"]]
                left["note"] += "; did_you_mean lists built flows of a close name, which may be the one meant"
            return {"result": left, "chunk_ids": []}
        detail = detail or "summary"
        if detail not in COMPONENT_DETAILS:
            raise ComponentToolError(f"detail must be one of {COMPONENT_DETAILS}")
        chunk = f"component::{c.cid}"
        if detail == "summary":
            out = self.summary(c)
            if how.get("matched") not in ("api_name", None):
                out = {**out, "resolved_from": {"typed": name, "matched": how["matched"]}}
            return {"result": out, "chunk_ids": [chunk]}
        pages = self.pages(c, detail)
        page = max(1, int(page or 1))
        if page > len(pages):
            raise ComponentToolError(f"page {page} is past the last page ({len(pages)}) of detail={detail}")
        return {"result": pages[page - 1], "chunk_ids": [chunk]}

    # ------------------------------------------------------------------ the summary
    def _state(self, c) -> dict:
        st = c.state
        out: dict[str, Any] = {"active": st.get("is_active"), "status": st.get("effective_status"),
                               "facts_from": st.get("facts_from")}
        if st.get("latest_version_status") not in (None, "Active"):
            out["latest_version_status"] = st["latest_version_status"]
        if st.get("active_version_number") is not None:
            out["active_version"] = st["active_version_number"]
        if st.get("facts_match_running_version") is not None:
            out["facts_match_running_version"] = st["facts_match_running_version"]
        if st.get("owner_rules"):
            out["owner_rules"] = st["owner_rules"]
        return out

    def _start(self, c, plain_chars: int = 900) -> dict:
        st = c.start
        out: dict[str, Any] = {"type": st.get("type")}
        if st.get("object"):
            out["object"] = st["object"]
        if st.get("events"):
            out["events"] = st["events"]
        out["runs"] = start_phrase(st)
        entry = st.get("entry") if isinstance(st.get("entry"), dict) else None
        if entry:
            out["entry_rule"] = _short(entry.get("plain") or f"{entry.get('logic')}: "
                                       + "; ".join(str(x) for x in entry.get("conditions") or []), plain_chars)
        sch = st.get("schedule") if isinstance(st.get("schedule"), dict) else None
        if sch:
            out["schedule"] = {k: sch[k] for k in ("frequency", "start_date", "org_time", "org_time_zone", "utc",
                                                   "ist", "first_run") if sch.get(k) is not None}
        callers = self._called_by(c)
        if callers:
            out["called_by"] = callers
        if st.get("type") in LAUNCH_NOTE:
            out["launched_from"] = LAUNCH_NOTE[st["type"]] + (CALLED_BY_NOTE if callers else "")
        return out

    def _called_by(self, c) -> list[str]:
        """The flows (and other refs) that the index lists as running this flow (``index.json`` ``called_by``)."""
        return [str(x) for x in (self.knowledge.index.get("called_by") or {}).get(c.cid) or []]

    def _objects(self) -> list[str]:
        idx = self.knowledge.index
        out = set(idx.get("by_object") or {})
        out.update(k.split(".", 1)[0] for k in list(idx.get("by_field") or {}) + list(idx.get("written_by") or {}))
        return sorted(out)

    def _fields(self) -> list[str]:
        idx = self.knowledge.index
        return sorted(set(idx.get("by_field") or {}) | set(idx.get("written_by") or {}))

    def _features(self, c) -> list[str]:
        return [f"{f.get('id')} {f.get('name')}" if isinstance(f, dict) else str(f) for f in c.doc.get("features") or []]

    def summary(self, c) -> dict:
        """``detail="summary"`` of one component, at most :data:`SUMMARY_CHARS` characters (see the module doc)."""
        d = c.doc
        prod, dev10 = split_caveats(d.get("caveats"))
        st = c.state
        out: dict[str, Any] = {"component": c.cid, "kind": c.kind, "api_name": c.api_name}
        if c.label != c.api_name:
            out["label"] = c.label
        out["title"] = c.title
        out["one_line"] = d.get("one_line")
        if st.get("facts_match_running_version") is False:
            out["warning"] = (f"The facts below come from {st.get('facts_from')}, which is not the version that runs "
                              f"in production: {st.get('active_version_basis')}")
        out["state"] = self._state(c)
        out["start"] = self._start(c)
        lines = [effect_line(e) for e in c.effects]
        out["effects"] = lines if lines else "none in the flow file"
        if d.get("detail"):
            out["detail_note"] = d["detail"]
        out["business_meaning"] = d.get("business_meaning")
        out["caveats"] = prod
        if dev10:
            out["dev10_notes"] = dev10
        calls = [f"{x.get('kind')} {str(x.get('ref') or '').split(':', 1)[-1]}"
                 + (f".{x['operation']}" if x.get("operation") else "") for x in d.get("calls") or []
                 if isinstance(x, dict)]
        if calls:
            out["calls"] = calls
        out["objects"] = [f"{o.get('object')}: {', '.join(o.get('role') or o.get('operations') or [])}"
                          for o in d.get("objects") or [] if isinstance(o, dict)]
        for key, val in (("business_processes", d.get("business_processes")), ("features", self._features(c)),
                         ("departments", d.get("departments"))):
            if val:
                out[key] = val
        entries = self.object_flows.get(c.api_name)
        if entries:
            out["object_entries"] = {"objects": entries, "rule": "these object files describe this flow too; where "
                                     "they differ, this component's state and start are current"}
        out["more"] = {lv: f"{len(self.pages(c, lv))} page(s)" for lv in PAGED_DETAILS}
        return self._fit_summary(c, out)

    #: The key order of a summary (a shortened part's note follows it).
    _SUMMARY_ORDER = ("component", "kind", "api_name", "label", "title", "one_line", "warning", "state", "start",
                      "effects", "effects_note", "detail_note", "business_meaning", "caveats", "caveats_note",
                      "dev10_notes", "dev10_notes_note", "calls", "objects", "business_processes", "features",
                      "departments", "object_entries", "lists_note", "resolved_from", "more")

    def _fit_summary(self, c, out: dict) -> dict:
        """Shorten the summary until it fits :data:`SUMMARY_CHARS`, least needed first, each step naming where the
        full text is (review M1: effects first, production facts first): the dev10 notes (first sentences, then a
        count), the meaning (its start), the production caveats (first sentences), the lists (processes, features,
        departments, object entries, calls: counts), then the effect lines (fewer fields, shorter routes), the
        production caveats (the first ones), the entry rule, and last the effects in one line (still naming the
        recipients of each notification); a last cut of the longest string keeps the bound. The state, the warning
        and the start are never dropped."""
        def fits() -> bool:
            return _size(out) <= SUMMARY_CHARS

        effects = c.effects
        n_prod = len(split_caveats(c.doc.get("caveats"))[0])
        if not fits() and out.get("dev10_notes"):
            out["dev10_notes"] = [_first_sentence(x, 160) for x in out["dev10_notes"]]
            out["dev10_notes_note"] = "first sentences only; in full: detail=caveats"
            if not fits():
                out["dev10_notes"] = len(out["dev10_notes"])
                out["dev10_notes_note"] = "dev10 notes (not production): detail=caveats"
        if not fits() and out.get("business_meaning"):
            out["business_meaning"] = _short(out["business_meaning"], 450) + " (in full: detail=caveats)"
        if not fits() and out.get("caveats"):
            out["caveats"] = [_first_sentence(x, 220) for x in out["caveats"]]
            out["caveats_note"] = "first sentences only; in full: detail=caveats"
        cut = []
        for key in ("object_entries", "departments", "features", "business_processes", "calls"):
            if not fits() and out.get(key):
                n = len(out[key]["objects"]) if key == "object_entries" else len(out[key])
                out[key] = n
                cut.append(key)
        if cut:
            out["lists_note"] = f"{', '.join(cut)}: counts only; in full: detail=caveats"
        folded = []

        def fold_effects() -> None:
            out["effects"] = [effects_summary(effects, 900, recipients=True)]
            out["effects_note"] = (f"{len(effects)} effects in one line; one line each: detail=effects; with their "
                                   "routes: detail=logic")
            folded.append(True)

        if not fits() and isinstance(out.get("effects"), list):
            out["effects"] = [effect_line(e, max_fields=4, route_chars=70) for e in effects]
            out["effects_note"] = "lines in short; in full: detail=effects (one line each) or detail=logic"
            if not fits():   # the short lines cannot fit even with one caveat and a short entry rule: fold them now
                probe = {**out, "caveats": (out.get("caveats") or [])[:1] if isinstance(out.get("caveats"), list)
                         else out.get("caveats"), "start": {**out["start"], "entry_rule": _short(
                             out["start"].get("entry_rule") or "", 300)}}
                if _size(probe) > SUMMARY_CHARS:
                    fold_effects()
        while not fits() and isinstance(out.get("caveats"), list) and len(out["caveats"]) > 1:
            out["caveats"] = out["caveats"][:-1]
            out["caveats_note"] = (f"the first {len(out['caveats'])} of {n_prod}, first sentences only; all in full: "
                                   "detail=caveats")
        if not fits() and out["start"].get("entry_rule"):
            out["start"]["entry_rule"] = _short(out["start"]["entry_rule"], 300) + " (in full: detail=logic)"
        if not fits() and isinstance(out.get("effects"), list) and not folded:
            fold_effects()
        while not fits():   # a last resort: cut the longest string
            path, longest = _longest_string(out)
            if longest is None or len(longest) < 80:
                break
            _set_path(out, path, _cut_string(longest, max(60, len(longest) - (_size(out) - SUMMARY_CHARS) - 50)))
        order = {k: i for i, k in enumerate(self._SUMMARY_ORDER)}
        return dict(sorted(out.items(), key=lambda kv: order.get(kv[0], len(order))))

    # ------------------------------------------------------------------ the paged levels
    def _items(self, c, detail: str) -> list[tuple[str, Any]]:
        d = c.doc
        flow = d.get("flow") or {}
        items: list[tuple[str, Any]] = []
        if detail == "effects":   # one line per effect (review M1: 'what does it create, update or send' in a page)
            items += [("effects", f"{e.get('element') or e.get('kind')}: {effect_line(e, max_fields=12, route_chars=160)}")
                      for e in c.effects]
            if not c.effects:
                items.append(("note", "no effects in the flow file" + (f": {d['detail']}" if d.get("detail") else "")))
            items.append(("routes", "each line names the first route to the effect; every route and the elements: "
                                    "detail=logic"))
        elif detail == "logic":
            items += [("effects", e) for e in c.effects]
            start = {k: v for k, v in (flow.get("start") or {}).items()}
            callers = self._called_by(c)
            if callers:
                start["called_by"] = callers
            items.append(("start", start))
            items += [("elements", el) for el in flow.get("elements") or []]
            items += [("ends_without_action", x) for x in flow.get("ends_without_action") or []]
            for key in ("interface", "run_context"):
                if flow.get(key) is not None:
                    items.append((key, flow[key]))
            if d.get("detail"):
                items.append(("detail_note", d["detail"]))
        elif detail == "fields":
            also = d.get("also_written_by") or {}
            for f in d.get("fields") or []:
                row = dict(f)
                others = also.get(f.get("field"))
                if others is not None and set(f.get("operations") or []) & set(_WRITE_OPS):
                    row["also_written_by"] = others
                items.append(("fields", row))
            if not d.get("fields"):
                items.append(("note", "no field list in this file" + (f": {d['detail']}" if d.get("detail") else "")))
        else:   # caveats
            st = c.state
            items.append(("state", {k: st[k] for k in ("is_active", "effective_status", "latest_version_status",
                                                        "active_version_number", "active_version_basis",
                                                        "facts_from", "facts_match_running_version", "owner_rules")
                                    if k in st}))
            items.append(("business_meaning", d.get("business_meaning")))
            prod, dev10 = split_caveats(d.get("caveats"))
            items += [("caveats", x) for x in prod]
            items += [("dev10_notes", x) for x in dev10]
            if not prod and not dev10:
                items.append(("caveats", "none documented"))
            for key, val in (("business_processes", d.get("business_processes")), ("features", self._features(c)),
                             ("departments", d.get("departments")), ("calls", d.get("calls")),
                             ("objects", d.get("objects"))):
                if val:
                    items.append((key, {"values": val}))
            entries = self.object_flows.get(c.api_name)
            if entries:
                items.append(("object_entries", {"objects": entries, "rule": "these object files describe this flow "
                              "too; where they differ, this component's state and start are current"}))
        return items

    def pages(self, c, detail: str) -> list[dict]:
        """Every page of ``detail`` (effects, logic, fields or caveats) of one component (made once, then kept)."""
        key = (c.cid, detail)
        got = self._pages.get(key)
        if got is None:
            header = {"component": c.cid, "title": c.title, "detail": detail}
            got = self._pages[key] = _paginate(header, self._items(c, detail), PAGE_CHARS)
        return [dict(p) for p in got]

    # ================================================================== find_usages
    def _resolve_object(self, typed: str) -> tuple[str | None, str | None]:
        """``(object API name, how)`` of a typed object: an object of the components index or of the loaded object
        knowledge by API name (any case), by label or plural label (review L4), or with only letters and digits
        compared and ``__c`` ignored (``background check``); ``(None, None)`` for an unknown name."""
        low = typed.strip().lower()
        if not low:
            return None, None
        for table in (self._lower_objects, self._known_objects):
            if low in table:
                return table[low], "api_name"
        if low in self._object_labels:
            return self._object_labels[low], "label"
        loose = _NON_ALNUM.sub("", re.sub(r"__c$", "", low))
        hits = sorted({api for api in list(self._lower_objects.values()) + list(self._known_objects.values())
                       if _NON_ALNUM.sub("", re.sub(r"__c$", "", api.lower())) == loose})
        if len(hits) == 1:
            return hits[0], "loose"
        return None, None

    def _find_usages(self, target: str, role: str = "any", value: str | None = None) -> dict:
        role = role or "any"
        if role not in USAGE_ROLES:
            raise ComponentToolError(f"role must be one of {USAGE_ROLES}")
        if value is not None and role in ("reads", "triggered_on"):
            raise ComponentToolError("value selects the flows that write it: use role writes or any")
        raw = target.strip()
        obj_typed, _, field_typed = raw.partition(".")
        obj, how = self._resolve_object(obj_typed)
        result: dict[str, Any] = {"target": raw, "role": role}
        if value is not None:
            result["value"] = value
        if how in ("label", "loose"):
            result["resolved_from"] = {"typed": obj_typed, "matched": f"object {how}", "object": obj}
        groups: dict[str, list] = {}
        if field_typed:
            key = self._lower_fields.get(f"{obj or obj_typed}.{field_typed}".lower())
            rows, groups = self._field_rows(key, role, value) if key else ([], {})
            if key:
                result["target"] = key
        else:
            key = self._lower_objects.get((obj or "").lower())
            if value is not None:
                raise ComponentToolError("value needs a field target (Object.Field)")
            rows, _other = self._object_rows(key, role) if key else ([], [])
            if key:
                result["target"] = key
        if key is None:
            pool = self._fields() if field_typed else self._objects()
            close = difflib.get_close_matches(raw, pool, n=5, cutoff=0.6) or \
                difflib.get_close_matches(raw.lower(), [p.lower() for p in pool], n=5, cutoff=0.6)
            result["count"] = 0
            result["rows"] = []
            if field_typed:
                result["note"] = f"no flow component reads or writes {obj or obj_typed}.{field_typed}"
            elif obj:   # an object of the object knowledge that no flow component touches
                result["target"] = obj
                result["note"] = f"no flow component acts on {obj}"
            else:
                result["note"] = (f"unknown target {raw}: no object of the flow components or of the object knowledge "
                                  "has this API name or label")
            if close:
                result["did_you_mean"] = [self._lower_fields.get(x.lower(), self._lower_objects.get(x.lower(), x))
                                          for x in close]
            result["not_covered"] = self.knowledge.coverage()["not_covered"]
            return {"result": result, "chunk_ids": []}
        rows.sort(key=self._row_order)
        result["count"] = len(rows)
        shown = rows[:USAGE_ROWS]
        result["rows"] = shown
        not_fixed = sorted(groups.get("values_not_fixed") or [], key=self._row_order)
        nf_shown = not_fixed[: max(0, USAGE_ROWS - len(shown))]
        if value is not None:
            result["values_not_fixed"] = nf_shown
            result["values_unknown"] = groups.get("values_unknown") or []
            result["writes_other_values"] = groups.get("writes_other_values") or []
        if role in ("triggered_on", "any") and any(r.get("start") in ("before_save", "after_save") for r in rows):
            result["order_note"] = ORDER_NOTE
        result["not_covered"] = self.knowledge.coverage()["not_covered"]
        while _size({**result, "more": 999, "more_note": MORE_ROWS_NOTE, "more_values_not_fixed": 999}) > USAGE_CHARS \
                and (nf_shown or len(shown) > 1):
            if nf_shown:
                nf_shown = nf_shown[:-1]
                result["values_not_fixed"] = nf_shown
            else:
                shown = shown[:-1]
                result["rows"] = shown
        if len(shown) < len(rows):
            result["more"] = len(rows) - len(shown)
            result["more_note"] = MORE_ROWS_NOTE
        if len(nf_shown) < len(not_fixed):
            result["more_values_not_fixed"] = len(not_fixed) - len(nf_shown)
        if not rows:
            if value is not None and (not_fixed or result.get("values_unknown")):
                result["note"] = VALUE_NOTE.format(value=value, target=result["target"])
            else:
                what = {"reads": "reads", "writes": "writes", "triggered_on": "starts on", "any": "uses"}[role]
                result["note"] = f"no flow component {what} {result['target']}" + (
                    f" with the value {value}" if value is not None else "")
        chunk_ids = [f"component::{r['flow']}" for r in shown + nf_shown if r.get("flow") in self.knowledge.components]
        return {"result": result, "chunk_ids": chunk_ids}

    @staticmethod
    def _row_order(r: dict) -> tuple:
        """Active first, then before save, after save, scheduled and the other starts (A17), then by name."""
        return (0 if r.get("active", True) else 1, _START_ORDER.get(str(r.get("start")), 9),
                str(r.get("flow") or r.get("writer")))

    def _row(self, ref: str, **extra: Any) -> dict:
        c = self.knowledge.components.get(ref)
        if c is None:
            left = self.knowledge.left_out(ref) or {}
            row = {"flow": ref, "built": False, **extra}
            if left.get("reason"):
                row["note"] = left["reason"]
            return row
        row: dict[str, Any] = {"flow": ref, "title": _short(c.title, 60), **extra}
        st = c.start
        row["start"] = st.get("type")
        if st.get("object"):
            row["on"] = st["object"]
        if st.get("events"):
            row["events"] = st["events"]
        entry = st.get("entry") if isinstance(st.get("entry"), dict) else {}
        if entry.get("only_when_changed_to_meet_criteria"):
            row["only_when_changed"] = True
        if any(isinstance(p, dict) and p.get("kind") != "immediate" for p in st.get("paths") or []):
            row["async_path"] = True
        row["active"] = bool(c.state.get("is_active"))
        return row

    def _object_rows(self, obj: str, role: str) -> tuple[list[dict], list[str]]:
        """Rows of an object target: the flows of its index roles, then (writes, any) the Apex and not-built writers
        of its fields; no other writers."""
        bo = (self.knowledge.index.get("by_object") or {}).get(obj) or {}
        want = {"triggered_on": ("triggered_on", "scheduled_on"), "writes": ("creates", "updates", "deletes"),
                "reads": ("reads",)}.get(role, ("triggered_on", "scheduled_on", "creates", "updates", "deletes",
                                                 "reads"))
        roles: dict[str, list[str]] = {}
        for r in want:
            for ref in bo.get(r) or []:
                roles.setdefault(ref, []).append(r)
        rows = [self._row(ref, roles=rs) for ref, rs in roles.items()]
        other: list[str] = []
        if role in ("writes", "any"):   # Apex and not-built writers of the object's fields (written_by)
            seen = {r["flow"] for r in rows}
            writers: dict[str, int] = {}
            for fkey, refs in (self.knowledge.index.get("written_by") or {}).items():
                if fkey.split(".", 1)[0] != obj:
                    continue
                for ref in refs or []:
                    if ref not in seen:
                        writers[ref] = writers.get(ref, 0) + 1
            for ref, n in sorted(writers.items()):
                if ref.startswith("apex:"):
                    rows.append({"writer": ref, "roles": ["writes"], "fields": n,
                                 "from": "object knowledge entry (Apex body not scanned)"})
                else:
                    rows.append(self._row(ref, roles=["writes"], fields=n))
        return rows, other

    def _field_rows(self, key: str, role: str, value: str | None) -> tuple[list[dict], dict[str, list]]:
        """Rows of a field target. A writer row carries ``values`` (the literal values it writes: the index's
        values_written and the literals of its effects' sets) and ``value_from`` (how it sets the field when the value
        is not fixed in the flow: ``variable <name>``, ``from <field>``, ``computed: <formula or system value>``).
        With ``value``: the rows are the writers of that literal value, and the groups (review H1) name the writers
        that may still write it (``values_not_fixed``: rows with ``may_write``), those whose values are not scanned
        (``values_unknown``: Apex and not-built writers) and those that write other literal values only
        (``writes_other_values``)."""
        obj, fname = key.split(".", 1)
        idx = self.knowledge.index
        rows: dict[str, dict] = {}
        for r in (idx.get("by_field") or {}).get(key) or []:
            ref = r.get("flow")
            ops = list(r.get("operations") or [])
            c = self.knowledge.components.get(ref)
            if c is not None and self._in_start(c, key):
                ops.append("start_condition")
            extra: dict[str, Any] = {"ops": ops}
            if set(ops) & set(_WRITE_OPS):
                vals = [str(v).split("=", 1)[1] or "blank" for v in r.get("values_written") or [] if "=" in str(v)]
                lits, value_from = value_sources(c.effects, obj, fname) if c is not None else ([], [])
                vals += [v for v in lits if v not in vals]
                if vals:
                    extra["values"] = vals
                if value_from:
                    extra["value_from"] = value_from
            rows[ref] = self._row(ref, **extra)
            if r.get("active") is False:
                rows[ref]["active"] = False
        for ref in (idx.get("written_by") or {}).get(key) or []:
            if ref in rows:
                if not set(rows[ref]["ops"]) & set(_WRITE_OPS):
                    rows[ref]["ops"].append("write")
                continue
            if ref.startswith("apex:"):
                rows[ref] = {"writer": ref, "ops": ["write"], "from": "object knowledge entry (Apex body not scanned)"}
            else:
                rows[ref] = self._row(ref, ops=["write"])
        out = list(rows.values())
        if role == "reads":
            out = [r for r in out if "read" in r["ops"]]
        elif role == "writes":
            out = [r for r in out if set(r["ops"]) & set(_WRITE_OPS)]
        elif role == "triggered_on":
            out = [r for r in out if "start_condition" in r["ops"] and r.get("on") == obj
                   and r.get("start") in ("before_save", "after_save", "scheduled")]
        groups: dict[str, list] = {}
        if value is not None:
            want = value.strip().lower()
            want = "blank" if want in ("", "null", "empty", "none") else want   # a cleared field
            writers = [r for r in out if set(r["ops"]) & set(_WRITE_OPS)]
            exact = [r for r in writers if want in [str(v).lower() for v in r.get("values") or []]]
            rest = [r for r in writers if r not in exact]
            groups["values_not_fixed"] = [{**r, "may_write": True} for r in rest if r.get("value_from")]
            groups["values_unknown"] = [
                f"{r.get('writer') or r.get('flow')} ("
                + ("Apex body not scanned" if r.get("writer") else
                   "not built: its values are not in the knowledge" if r.get("built") is False else
                   "its values are not in the knowledge") + ")"
                for r in rest if not r.get("value_from") and not r.get("values")]
            groups["writes_other_values"] = [f"{r.get('flow') or r.get('writer')} ({', '.join(map(str, r['values']))})"
                                             for r in rest if r.get("values") and not r.get("value_from")]
            out = exact
        return out, groups

    @staticmethod
    def _in_start(c, key: str) -> bool:
        """The field is read by the flow's start (its entry rule), per the component's field list."""
        return any(isinstance(f, dict) and f.get("field") == key and "$Start" in (f.get("used_in") or [])
                   for f in c.doc.get("fields") or [])


# ------------------------------------------------------------------------------------------------ small helpers
def _longest_string(obj: Any, path: tuple = ()) -> tuple[tuple, str | None]:
    best: tuple[tuple, str | None] = ((), None)
    if isinstance(obj, str):
        return path, obj
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else ()
    for k, v in items:
        p, s = _longest_string(v, path + (k,))
        if s is not None and (best[1] is None or len(s) > len(best[1])):
            best = (p, s)
    return best


def _set_path(obj: Any, path: tuple, value: Any) -> None:
    for k in path[:-1]:
        obj = obj[k]
    obj[path[-1]] = value


#: The handler names of :func:`~sf_assistant.tools.registry.component_tool_defs` (checked by tests/test_tool_registry.py).
HANDLERS: dict[str, Callable[..., dict]] = {d.name: getattr(ComponentTools, d.handler) for d in component_tool_defs()}
