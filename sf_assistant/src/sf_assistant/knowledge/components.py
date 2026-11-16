"""Load, validate and read the component knowledge: one JSON file per Salesforce metadata component (flows for now)
beside the object knowledge, plus the generated reverse-link index. Standard library only.

The folder (``config.COMPONENTS_DIR``, ``knowledge/components``; built by ``kb_build/flows``, see its README):

* ``flows/<ApiName>.json``: one flow (``format_version`` 1, ``kind`` ``flow``, ``id`` ``flow:<ApiName>``) with its
  effective production state, start (trigger, entry rule, schedule), effects, elements, fields, caveats and the
  curated meaning (title, one line, aliases, business meaning);
* ``index.json``: the reverse links (``components``, ``by_object``, ``by_field``, ``by_value``, ``written_by``,
  ``called_by``, ``excluded``, ``not_built``, ``by_feature``, ``by_process``, ``by_department``).

:class:`ComponentKnowledge` loads both read-only and checks their structure and their agreement (:meth:`validate`;
:meth:`ComponentKnowledge.load` raises :class:`ComponentError` with the problems, as an invalid object file stops a
start). It gives out the public view of every file only: :func:`strip_evidence` removes the development evidence at
any depth when the file is read, and the cards, the search chunks and everything the component tools return are made
from that view. :class:`ComponentKnowledge` also resolves a name typed by a model (API name, label, title or alias,
with did-you-means), and writes the one search card per component (:meth:`ComponentKnowledge.card`,
:meth:`ComponentKnowledge.chunks`) and the plain phrases of effects and starts that the cards and the tools share.

Evidence policy (owner, 2026-10-23): each component file keeps ``evidence`` (document names and locators) for
development reference. It must never reach a model or an answer: the tools strip it before returning anything, the
search never indexes it, and the knowledge prompt says it is never cited. :data:`EVIDENCE_KEYS` names the keys. The
curated text names no source document either (review P1 of 2026-10-24; ``kb_build/flows/merge_flows.py`` refuses
document titles and types such as a handbook or the KB), so what the tools return says "documented" at most.

Load-time guard (review P4): :meth:`ComponentKnowledge.validate` also refuses a file whose public view holds an
e-mail address, a URL, an org, Slack or record Id (:data:`PRIVACY_PATTERNS`; the build masks them, so such a value
means a hand edit or an unaudited file). Withheld fields are checked at build time only (``audit.py --components``).
"""

from __future__ import annotations

import copy
import difflib
import glob
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sf_assistant.knowledge.store import split_api_name

__all__ = ["ComponentKnowledge", "Component", "ComponentError", "EVIDENCE_KEYS", "KINDS", "strip_evidence",
           "has_index", "load_components", "effect_phrase", "effect_line", "start_phrase", "effects_summary",
           "NOT_COVERED", "PRIVACY_PATTERNS", "privacy_hits", "value_sources"]

#: Keys that hold development evidence (development reference only): never shown, never indexed.
EVIDENCE_KEYS: frozenset[str] = frozenset({"evidence"})
#: Component kind -> its folder under the components folder.
KINDS: dict[str, str] = {"flow": "flows"}
FORMAT_VERSION = 1
#: Top-level keys every component file must carry (a short inactive file has no ``fields`` and no ``also_written_by``).
REQUIRED_KEYS: tuple[str, ...] = ("format_version", "kind", "id", "api_name", "label", "title", "one_line", "aliases",
                                  "state", "business_meaning", "flow", "caveats", "calls", "objects")
INDEX_KEYS: tuple[str, ...] = ("components", "excluded", "not_built", "called_by", "by_object", "by_field", "by_value",
                               "written_by")
#: What the components index does not cover (the tools quote it with an empty or a writer result).
NOT_COVERED: tuple[str, ...] = (
    "Apex classes and triggers: their bodies are not scanned; Apex writers of a field are only those the object "
    "knowledge automation entries name",
    "launch points: buttons, quick actions, pages and the Apex callers of autolaunched flows are not scanned",
    "flows left out of the build: the B2B staffing flows and the test flow (owner rule; active in production, but "
    "their writes are in no list) and production flows with no document source (index not_built: only the active "
    "ones' writes are listed)",
    "validation rules: they are in the object knowledge (get_validation_rules)",
)

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

#: A load-time guard (review P4, defence in depth): the build masks these values (kb_build/flows/flowlib.py, whose
#: patterns these copy; the package never imports kb_build), so a component file that still holds one was hand-edited
#: or not audited, and :meth:`ComponentKnowledge.validate` refuses it (counts only in the message, never the value).
PRIVACY_PATTERNS: dict[str, re.Pattern] = {
    "e-mail address": re.compile(r"[\w.%+-]+@[\w-]+\.[\w.-]*[\w-]"),
    "URL": re.compile(r"(?i)\b(?:https?://|www\.)[^\s\"'<>{}]+"),
    "org id": re.compile(r"\b00D[A-Za-z0-9]{12,15}\b"),
    "Slack id": re.compile(r"\b[CGDUWTAB][0-9][A-Z0-9]{7,11}\b"),
    "record Id": re.compile(r"(?<![A-Za-z0-9_])(?!00D)(?=(?:[A-Za-z]*\d){3})(?=(?:\d*[A-Za-z]){2})"
                            r"[0-9A-Za-z][A-Za-z0-9]{14}(?:[A-Za-z0-9]{3})?(?![A-Za-z0-9_])"),
}


def _strings(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def privacy_hits(doc: Any) -> dict[str, int]:
    """``{kind: count}`` of the :data:`PRIVACY_PATTERNS` values in every key and string of ``doc`` ({} = none)."""
    out: dict[str, int] = {}
    for s in _strings(doc):
        for kind, rx in PRIVACY_PATTERNS.items():
            n = len(rx.findall(s))
            if n:
                out[kind] = out.get(kind, 0) + n
    return out


class ComponentError(ValueError):
    """The component folder is missing its index or a file does not validate (the message lists the problems)."""


def strip_evidence(obj: Any) -> Any:
    """A deep copy of ``obj`` without any key of :data:`EVIDENCE_KEYS`, at any depth (dicts in lists included)."""
    if isinstance(obj, dict):
        return {k: strip_evidence(v) for k, v in obj.items() if k not in EVIDENCE_KEYS}
    if isinstance(obj, list):
        return [strip_evidence(v) for v in obj]
    return copy.deepcopy(obj)


def has_index(folder: str | os.PathLike | None) -> bool:
    """True when ``folder`` holds an ``index.json`` (a components folder to load)."""
    return folder is not None and os.path.isfile(os.path.join(str(folder), "index.json"))


def _norm(text: Any) -> str:
    return _NON_ALNUM_RE.sub("", str(text or "").lower())


def _short(text: Any, limit: int) -> str:
    """``text`` on one line, cut at a word boundary to at most ``limit`` characters (``...`` marks a cut)."""
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    cut = t[: max(0, limit - 3)].rsplit(" ", 1)[0].rstrip(",;:")
    return cut + "..."


# ------------------------------------------------------------------------------------------------ plain phrases
_VERBS = {"update": "updates", "create": "creates", "upsert": "creates or updates", "delete": "deletes"}


def _value_text(v: Any) -> str | None:
    """A set's literal value as text (``Scheduled``, ``false``), else None (computed, copied or a variable)."""
    if not isinstance(v, dict) or "literal" not in v:
        return None
    lit = v["literal"]
    if isinstance(lit, bool):
        return "true" if lit else "false"
    return "blank" if lit is None else str(lit)


def value_sources(effects: Iterable[dict], obj: str, field: str) -> tuple[list[str], list[str]]:
    """``(literal values, value_from)`` of every create, update or upsert of ``obj`` in ``effects`` whose ``sets``
    holds ``field`` (the bare field name): the literal values as text (``blank`` for a null or an empty text, which
    both clear the field), and how the others set it when the value is
    not fixed in the flow (``variable <name>``, ``from <field or element output>``, ``computed: <formula, text
    template or system value name>``; never a formula's text). An ``any_of`` gives each of its options."""
    lits: list[str] = []
    other: list[str] = []

    def add(v: Any) -> None:
        if not isinstance(v, dict):
            return
        if "any_of" in v:
            for x in v.get("any_of") or []:
                add(x)
            return
        lit = _value_text(v)
        if lit is not None:
            text = lit if lit != "" else "blank"   # an empty text literal clears the field, as a null does
            target = lits
        elif "variable" in v:
            text, target = f"variable {v['variable']}", other
        elif "from" in v:
            text, target = f"from {v['from']}", other
        elif v.get("computed") is not None:
            text, target = f"computed: {v['computed']}", other
        else:
            text, target = "computed", other
        if text not in target:
            target.append(text)

    for e in effects or []:
        if isinstance(e, dict) and e.get("kind") in ("create", "update", "upsert") and e.get("object") == obj \
                and isinstance(e.get("sets"), dict) and field in e["sets"]:
            add(e["sets"][field])
    return lits, other


def _sets_text(sets: Any, max_fields: int = 8) -> str:
    """``Status__c = Scheduled, Meeting_Link__c, ... (+3 more)`` of an effect's ``sets``."""
    if not isinstance(sets, dict) or not sets:
        return ""
    parts = []
    for field, v in sets.items():
        lit = _value_text(v)
        parts.append(f"{field} = {lit}" if lit is not None else str(field))
    more = len(parts) - max_fields
    return ", ".join(parts[:max_fields]) + (f" (+{more} more)" if more > 0 else "")


def _recipient_text(r: Any) -> str:
    if not isinstance(r, dict):
        return str(r)
    if r.get("literal"):
        return "a fixed recipient set in the flow (not copied)"
    src = r.get("from") or r.get("variable") or "?"
    hops = r.get("hops") or r.get("uses") or []
    return f"{src} via {' > '.join(str(h) for h in hops)}" if hops else str(src)


def effect_phrase(e: dict, max_fields: int = 8) -> str:
    """One effect in plain words: ``updates the triggering Session__c (Status__c = Scheduled, ...)``, ``sends an
    e-mail``, ``calls the external service CreateInterview.CreateInterviewMethod`` ... (no route, no path)."""
    kind = str(e.get("kind") or "")
    if kind in _VERBS:
        obj = e.get("object") or "?"
        target = "the triggering " if e.get("target") == "triggering_record" else ""
        sets = _sets_text(e.get("sets"), max_fields) if kind != "delete" else ""
        return f"{_VERBS[kind]} {target}{obj}" + (f" ({sets})" if sets else "")
    if kind == "email":
        return "sends an e-mail"
    if kind == "slack":
        return "posts a Slack message"
    if kind == "bell":
        return "sends a bell notification"
    if kind == "callout":
        return f"calls the external service {e.get('service')}.{e.get('operation')}"
    if kind == "apex":
        return f"runs the Apex action {e.get('class')}"
    if kind == "subflow":
        return f"runs the subflow {e.get('flow')}"
    if kind == "blocks_save":
        return "blocks the save with an error message" + (f" on {e['field']}" if e.get("field") else "")
    if kind == "fails_async_run":
        return "stops the asynchronous run with an error" + (f" on {e['field']}" if e.get("field") else "")
    if kind == "wait":
        return "waits (" + "; ".join(str(x) for x in e.get("resumes") or []) + ")"
    if kind == "rollback":
        return "rolls back the changes of the run"
    return kind or "effect"


def effect_line(e: dict, max_fields: int = 8, route_chars: int = 140) -> str:
    """:func:`effect_phrase` with its path (when not immediate), its recipients and its routes in short."""
    out = effect_phrase(e, max_fields)
    if e.get("kind") in ("email", "slack", "bell") and e.get("recipients"):
        out += " to " + "; ".join(_recipient_text(r) for r in e["recipients"][:3])
        if len(e["recipients"]) > 3:
            out += f" (+{len(e['recipients']) - 3} more)"
    path = e.get("path")
    if path and path != "immediate":
        out += f" [path {path}]"
    when = e.get("when")
    if isinstance(when, list) and when:
        first = _short(when[0], route_chars)
        out += f"; when {first}" + (f" (or {len(when) - 1} other routes)" if len(when) > 1 else "")
    elif e.get("when_routes"):
        out += f"; on {e['when_routes']} routes"
    if e.get("on_fault") not in (None, "none"):
        out += f"; on fault: {e['on_fault']}"
    return out


def _recipients_short(e: dict, n: int = 2, chars: int = 60) -> str:
    """`` to Recruiter__c.Email__c; ...`` of a notification effect (at most ``n``, each cut to ``chars``), or ''."""
    rs = e.get("recipients") if e.get("kind") in ("email", "slack", "bell") else None
    if not rs:
        return ""
    out = " to " + "; ".join(_short(_recipient_text(r), chars) for r in rs[:n])
    return out + (f" (+{len(rs) - n} more)" if len(rs) > n else "")


def effects_summary(effects: Iterable[dict], limit: int = 280, recipients: bool = False) -> str:
    """Every effect of a flow grouped by what it does to which object: ``updates Deliverable__c: Status__c (Approved,
    Failed), Approved_At__c; sends an e-mail (2)``, cut to ``limit`` characters. '' without effects.
    ``recipients``: a notification names its recipients in short (``sends an e-mail to Recruiter__c.Email__c``; the
    summary's folded form); the search cards and chunks leave them out."""
    groups: dict[tuple, dict] = {}
    for e in effects or []:
        if not isinstance(e, dict):
            continue
        kind = str(e.get("kind") or "")
        if kind in _VERBS:
            key = (kind, e.get("object"))
            g = groups.setdefault(key, {"n": 0, "fields": {}})
            g["n"] += 1
            for field, v in (e.get("sets") or {}).items() if isinstance(e.get("sets"), dict) else ():
                vals = g["fields"].setdefault(field, [])
                lit = _value_text(v)
                if lit is not None and lit not in vals:
                    vals.append(lit)
        else:
            phrase = effect_phrase(e) + (_recipients_short(e) if recipients else "")
            g = groups.setdefault(("other", phrase), {"n": 0})
            g["n"] += 1
    parts = []
    for (kind, what), g in groups.items():
        if kind == "other":
            parts.append(what + (f" ({g['n']})" if g["n"] > 1 else ""))
            continue
        fields = ", ".join(f + (f" ({', '.join(v)})" if v else "") for f, v in g["fields"].items())
        parts.append(f"{_VERBS[kind]} {what}" + (f": {fields}" if fields else ""))
    return _short("; ".join(parts), limit)


def start_phrase(start: Any) -> str:
    """When a flow runs, in words: ``after save of Session__c on create, update; only when the save makes the entry
    rule true; scheduled path Schedule_Path: 0 Minutes after the triggering save`` (no entry rule text)."""
    if not isinstance(start, dict):
        return "unknown start"
    typ = str(start.get("type") or "unknown")
    obj = start.get("object")
    if typ in ("before_save", "after_save"):
        out = f"{typ.replace('_', ' ')} of {obj}"
        if start.get("events"):
            out += " on " + ", ".join(str(x) for x in start["events"])
    elif typ == "scheduled":
        sch = start.get("schedule") or {}
        out = f"scheduled {str(sch.get('frequency') or '').lower()}".rstrip()
        if sch.get("org_time"):
            out += f" at {sch['org_time']} {sch.get('org_time_zone') or ''}".rstrip()
        if obj:
            out += f", {start.get('runs') or 'over'} {obj}" if start.get("runs") else f", on {obj}"
    elif typ == "screen":
        out = "screen flow (a user starts it)"
    elif typ == "autolaunched":
        out = "autolaunched (another flow, Apex or a process starts it)"
    else:
        out = typ.replace("_", " ")
    entry = start.get("entry") if isinstance(start.get("entry"), dict) else None
    if entry and entry.get("only_when_changed_to_meet_criteria"):
        out += "; only when the save makes the entry rule true"
    for p in start.get("paths") or []:
        if isinstance(p, dict) and p.get("kind") != "immediate":
            when = f"{p.get('offset')} {p.get('unit')} after {p.get('from')}" if p.get("offset") is not None \
                else "after the save commits"
            out += f"; {p.get('kind', 'scheduled')} path {p.get('id')}: {when}"
    return out


# ------------------------------------------------------------------------------------------------ the components
@dataclass
class Component:
    """One loaded component: ``doc`` is its public view (:func:`strip_evidence` applied); the file's own text is not
    kept."""

    kind: str
    api_name: str
    cid: str                 # flow:<ApiName>
    file: str                # the file name inside the kind folder
    doc: dict

    @property
    def label(self) -> str:
        return str(self.doc.get("label") or self.api_name)

    @property
    def title(self) -> str:
        return str(self.doc.get("title") or self.label)

    @property
    def aliases(self) -> list[str]:
        return [str(a) for a in self.doc.get("aliases") or []]

    @property
    def state(self) -> dict:
        return self.doc.get("state") or {}

    @property
    def start(self) -> dict:
        return (self.doc.get("flow") or {}).get("start") or {}

    @property
    def effects(self) -> list[dict]:
        return [e for e in (self.doc.get("flow") or {}).get("effects") or [] if isinstance(e, dict)]


class ComponentKnowledge:
    """The component knowledge of one folder: the public view of every component file and of the index.

    ``index``: the public view of ``index.json``; ``components``: ``{cid: Component}`` in id order. Read-only after
    load, so one instance can serve every thread."""

    def __init__(self, index: dict, docs: dict[str, dict], folder: str | None = None) -> None:
        if not isinstance(index, dict):
            raise TypeError("the component index must be a dict parsed from index.json")
        self.folder = folder
        self.index: dict = strip_evidence(index)
        self.components: dict[str, Component] = {}
        self._problems: list[str] = []
        self._evidence_entries = 0          # how many evidence entries were stripped (a count; never the text)
        for key in sorted(docs):
            raw = docs[key]
            if not isinstance(raw, dict):
                self._problems.append(f"{key}: not a JSON object")
                continue
            self._evidence_entries += sum(len(raw.get(k) or []) for k in EVIDENCE_KEYS if isinstance(raw.get(k), list))
            kind = str(raw.get("kind") or "")
            api = str(raw.get("api_name") or "")
            cid = f"{kind}:{api}"
            self.components[cid] = Component(kind=kind, api_name=api, cid=cid, file=str(key), doc=strip_evidence(raw))
        self._names: dict[str, set[str]] = {}       # exact lower-case name -> cids (api, id, label, title)
        self._loose: dict[str, set[str]] = {}       # normalised name -> cids
        self._aliases: dict[str, set[str]] = {}     # normalised alias -> cids
        for cid, c in self.components.items():
            for name in (c.api_name, cid, c.label, c.title):
                self._names.setdefault(name.lower(), set()).add(cid)
                self._loose.setdefault(_norm(name), set()).add(cid)
            for a in c.aliases:
                self._aliases.setdefault(_norm(a), set()).add(cid)
        self._chunks: list[dict] | None = None

    # ------------------------------------------------------------------ loading
    @classmethod
    def load(cls, folder: str | os.PathLike, validate: bool = True) -> "ComponentKnowledge":
        """Read ``folder``'s ``index.json`` and every ``<kind folder>/*.json``. Raises :class:`ComponentError` when
        the index is missing or unreadable, or (``validate``) when :meth:`validate` finds a problem."""
        folder = str(folder)
        path = os.path.join(folder, "index.json")
        try:
            with open(path, encoding="utf-8") as fh:
                index = json.load(fh)
        except FileNotFoundError:
            raise ComponentError(f"no index.json in {folder}") from None
        except (OSError, ValueError) as exc:
            raise ComponentError(f"{path} is not readable JSON: {exc}") from None
        docs: dict[str, dict] = {}
        problems: list[str] = []
        for kind, sub in KINDS.items():
            for p in sorted(glob.glob(os.path.join(folder, sub, "*.json"))):
                key = f"{sub}/{os.path.basename(p)}"
                try:
                    with open(p, encoding="utf-8") as fh:
                        docs[key] = json.load(fh)
                except (OSError, ValueError) as exc:
                    problems.append(f"{key} is not readable JSON: {exc}")
        out = cls(index, docs, folder)
        problems += out.validate() if validate else []
        if problems:
            raise ComponentError(f"component knowledge in {folder} is invalid:\n  " + "\n  ".join(problems[:20])
                                 + (f"\n  ... and {len(problems) - 20} more" if len(problems) > 20 else ""))
        return out

    def validate(self) -> list[str]:
        """Structure, agreement and privacy problems ([] = valid): the index keys, every component's required keys,
        its ``format_version``, kind, id and file name, its state, no :data:`PRIVACY_PATTERNS` value in its public
        view, and its index entry (one per file, the same label, title and state); every name in the by_* lists is a
        component, an excluded or a not-built flow."""
        problems = list(self._problems)
        idx = self.index
        if idx.get("format_version") != FORMAT_VERSION or idx.get("kind") != "component_index":
            problems.append("index.json: format_version 1 and kind 'component_index' expected")
        for key in INDEX_KEYS:
            if not isinstance(idx.get(key), dict):
                problems.append(f"index.json: '{key}' missing or not an object")
        listed = idx.get("components") if isinstance(idx.get("components"), dict) else {}
        for cid, c in self.components.items():
            d = c.doc
            missing = [k for k in REQUIRED_KEYS if k not in d]
            if missing:
                problems.append(f"{c.file}: missing keys {missing}")
                continue
            if d.get("format_version") != FORMAT_VERSION:
                problems.append(f"{c.file}: format_version {d.get('format_version')!r}, expected {FORMAT_VERSION}")
            if c.kind not in KINDS or not c.file.startswith(KINDS.get(c.kind, "?") + "/"):
                problems.append(f"{c.file}: kind {c.kind!r} does not match its folder")
            if d.get("id") != cid or c.file.rsplit("/", 1)[-1] != f"{c.api_name}.json":
                problems.append(f"{c.file}: id {d.get('id')!r} / api_name {c.api_name!r} do not match the file name")
            st = d.get("state")
            if not isinstance(st, dict) or not isinstance(st.get("is_active"), bool) \
                    or not isinstance(st.get("effective_status"), str):
                problems.append(f"{c.file}: state.is_active (bool) and state.effective_status (str) expected")
            if not isinstance((d.get("flow") or {}).get("start"), dict):
                problems.append(f"{c.file}: flow.start missing")
            for key in ("aliases", "caveats", "calls", "objects"):
                if not isinstance(d.get(key), list):
                    problems.append(f"{c.file}: '{key}' must be a list")
            leaks = privacy_hits(d)
            if leaks:
                problems.append(f"{c.file}: " + ", ".join(f"{n} {k}-like value(s)" for k, n in sorted(leaks.items()))
                                + " in the text (the build masks them: rebuild the file and run audit.py --components)")
            entry = listed.get(cid)
            if not isinstance(entry, dict):
                problems.append(f"{c.file}: no index.json components entry {cid}")
                continue
            if entry.get("file") != c.file:
                problems.append(f"index.json {cid}: file {entry.get('file')!r} is not {c.file!r}")
            for key, want in (("label", d.get("label")), ("title", d.get("title"))):
                if entry.get(key) != want:
                    problems.append(f"index.json {cid}: {key} differs from the file")
            if isinstance(st, dict) and (entry.get("is_active"), entry.get("effective_status")) != \
                    (st.get("is_active"), st.get("effective_status")):
                problems.append(f"index.json {cid}: state differs from the file")
        for cid in listed:
            if cid not in self.components:
                problems.append(f"index.json lists {cid}, which has no file")
        known = set(self.components) | set(idx.get("excluded") or {}) | set(idx.get("not_built") or {})
        bad: set[str] = set()
        for obj, roles in (idx.get("by_object") or {}).items():
            for refs in (roles or {}).values():
                bad.update(r for r in refs or [] if r not in known)
        for rows in (idx.get("by_field") or {}).values():
            bad.update(r.get("flow") for r in rows or [] if isinstance(r, dict) and r.get("flow") not in known)
        for refs in (idx.get("written_by") or {}).values():
            bad.update(r for r in refs or [] if str(r).startswith("flow:") and r not in known)
        problems += [f"index.json names {r}, which is neither a component nor an excluded or not-built flow"
                     for r in sorted(str(x) for x in bad)]
        return problems

    # ------------------------------------------------------------------ access
    def ids(self) -> list[str]:
        return list(self.components)

    def get(self, cid: str) -> Component | None:
        return self.components.get(cid)

    @property
    def evidence_entries(self) -> int:
        """How many evidence entries the files hold (stripped at load; for start-up lines and tests)."""
        return self._evidence_entries

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.components.values():
            out[c.kind] = out.get(c.kind, 0) + 1
        return out

    def coverage(self) -> dict:
        """What the components cover and what they do not (quoted by the tools when nothing matches)."""
        idx = self.index
        return {"kinds": sorted(self.counts()), "components": self.counts(),
                "excluded_flows": len(idx.get("excluded") or {}), "not_built_flows": len(idx.get("not_built") or {}),
                "not_covered": list(NOT_COVERED)}

    def left_out(self, ref: str) -> dict | None:
        """``{"component", "built": False, "status", "reason"}`` of a flow the index lists as excluded or not built,
        else None."""
        idx = self.index
        if ref in (idx.get("excluded") or {}):
            e = idx["excluded"][ref]
            return {"component": ref, "built": False, "status": "excluded by an owner rule",
                    "reason": e.get("reason") if isinstance(e, dict) else str(e)}
        if ref in (idx.get("not_built") or {}):
            return {"component": ref, "built": False, "status": "not built", "reason": str(idx["not_built"][ref])}
        return None

    # ------------------------------------------------------------------ names
    def resolve(self, name: str, kind: str = "flow") -> tuple[Component | None, dict]:
        """``(component, how)`` for a typed name, or ``(None, how)``.

        Tiers: the id (``flow:Api``), the API name, the label or the title (any case); the same with only letters and
        digits compared; the id or API name of an excluded or not-built flow; an alias that names exactly one
        component; the API name of an excluded or not-built flow with only letters and digits compared. ``how`` says
        which tier matched (``{"matched": "api_name" | "label" | "title" | "alias" | "loose"}``), or for no match:
        ``{"ambiguous": [cids]}`` (an alias several components share), ``{"left_out": {...}}`` (an excluded or
        not-built flow; with ``did_you_mean`` when built flows have a close name) or ``{"did_you_mean": [cids]}``
        (close names, at most 5)."""
        typed = str(name or "").strip()
        if not typed:
            return None, {"did_you_mean": []}
        if ":" in typed and typed.split(":", 1)[0].lower() in KINDS:
            k, _, rest = typed.partition(":")
            kind, typed = k.lower(), rest.strip()
        prefix = f"{kind}:"
        low = typed.lower()
        for tier, table, key in (("exact", self._names, low), ("exact", self._names, f"{prefix}{low}"),
                                 ("loose", self._loose, _norm(typed))):
            hits = sorted(c for c in table.get(key, set()) if c.startswith(prefix))
            if len(hits) == 1:
                c = self.components[hits[0]]
                if tier == "exact":
                    tier = next(t for t, v in (("api_name", c.api_name), ("label", c.label), ("title", c.title),
                                               ("api_name", c.cid)) if v.lower() in (low, f"{prefix}{low}"))
                return c, {"matched": tier}
        left_refs = [r for r in list(self.index.get("excluded") or {}) + list(self.index.get("not_built") or {})
                     if r.startswith(prefix)]
        # a left-out flow named exactly (its id or API name) wins over an alias of a built flow (review P3)
        for ref in left_refs:
            if ref.lower() in (low, f"{prefix}{low}"):
                return None, self._left_out_how(ref, typed, kind)
        hits = sorted(c for c in self._aliases.get(_norm(typed), set()) if c.startswith(prefix))
        if len(hits) == 1:
            return self.components[hits[0]], {"matched": "alias"}
        if len(hits) > 1:
            return None, {"ambiguous": hits}
        for ref in left_refs:
            if _norm(ref[len(prefix):]) == _norm(typed):
                return None, self._left_out_how(ref, typed, kind)
        return None, {"did_you_mean": self.close_names(typed, kind)}

    def _left_out_how(self, ref: str, typed: str, kind: str) -> dict:
        """``{"left_out": {...}}`` of an excluded or not-built flow, with ``did_you_mean`` when built flows have a close
        name (a typed name that differs from a built flow by a suffix lands here; review L2)."""
        how: dict[str, Any] = {"left_out": self.left_out(ref)}
        close = self.close_names(typed, kind, n=3)
        if close:
            how["did_you_mean"] = close
        return how

    def close_names(self, typed: str, kind: str = "flow", n: int = 5) -> list[str]:
        """Up to ``n`` component ids whose API name, label, title or an alias is close to ``typed`` (difflib), then
        the best search hits."""
        prefix = f"{kind}:"
        cands: dict[str, set[str]] = {}
        for cid, c in self.components.items():
            if not cid.startswith(prefix):
                continue
            for key in (c.api_name.lower(), c.label.lower(), c.title.lower(), split_api_name(c.api_name),
                        *(a.lower() for a in c.aliases)):
                cands.setdefault(key, set()).add(cid)
        out: list[str] = []
        for m in difflib.get_close_matches(typed.lower(), list(cands), n=10, cutoff=0.6):
            out += [c for c in sorted(cands[m]) if c not in out]
        return out[:n]

    # ------------------------------------------------------------------ cards and search chunks
    def card(self, cid: str, *, runs_chars: int = 240, effects_chars: int = 240, aliases: int = 8) -> dict:
        """The search card of one component (about 800 characters at most): what it is called, what it does, when
        it runs, what it changes and its state. Never fields, never evidence."""
        c = self.components[cid]
        st = c.state
        entry = c.start.get("entry") if isinstance(c.start.get("entry"), dict) else {}
        runs = start_phrase(c.start)
        if entry.get("plain"):
            runs = f"{runs}. {entry['plain']}"
        card: dict[str, Any] = {"component": cid, "title": c.title}
        if c.label != c.api_name:
            card["label"] = c.label
        card["one_line"] = c.doc.get("one_line")
        if c.aliases:
            card["aliases"] = c.aliases[:aliases]
        card["runs"] = _short(runs, runs_chars)
        eff = effects_summary(c.effects, effects_chars)
        card["effects"] = eff or "none in the flow file"
        state = st.get("effective_status") or ("active" if st.get("is_active") else "inactive")
        if st.get("facts_match_running_version") is False:
            state += "; the facts describe a version that does not run (see get_component)"
        card["state"] = f"{state} ({st.get('facts_from')})" if st.get("facts_from") else state
        return card

    def chunks(self) -> list[dict]:
        """One search chunk per component (``id`` ``component::<cid>``, ``section`` the kind, ``title`` the title):
        the text holds the label, the API name in words, the one line, the aliases, the concept, the start, the entry
        rule in words and the effects summary of the card. Never fields, never evidence."""
        if self._chunks is None:
            out = []
            for cid, c in self.components.items():
                card = self.card(cid, runs_chars=600, effects_chars=600, aliases=50)
                concept = (c.doc.get("semantic") or {}).get("concept") or ""
                text = " ".join(str(x) for x in (
                    c.label, c.api_name, split_api_name(c.api_name), card.get("one_line") or "",
                    " ".join(card.get("aliases") or []), concept, card["runs"], card["effects"],
                    " ".join(str(o.get("object")) for o in c.doc.get("objects") or [] if isinstance(o, dict))))
                out.append({"id": f"component::{cid}", "section": c.kind, "title": c.title, "text": " ".join(text.split()),
                            "data": {"component": cid}})
            self._chunks = out
        return [dict(x) for x in self._chunks]


def load_components(folder: str | os.PathLike | None) -> ComponentKnowledge | None:
    """:meth:`ComponentKnowledge.load` of ``folder``, or None when ``folder`` is None or has no ``index.json``
    (no component tools). Raises :class:`ComponentError` when the folder's files are invalid."""
    if not has_index(folder):
        return None
    return ComponentKnowledge.load(Path(str(folder)))

