"""The 14 record tools: schemas, argument validation, handlers, payload cap and evidence log.

``RecordTools`` has the duck-typed surface ``sf_assistant.agent.knowledge_agent.answer_question`` uses (``tools``,
``openai_tools()``, ``call()``). ``call`` never raises and returns ``{"ok","result","error","chunk_ids"}``;
the result JSON is at most 12,000 characters. Every call of a data or orientation tool is logged as
one :class:`sf_assistant.engine.types.QueryEvidence` (failed calls included): the arguments, what was applied,
every SQL statement with its params and rows, the tables and their sync times. SQL is never sent
to the model. Code builds every statement (``sf_assistant.engine.query_builder``); the model only picks typed
arguments.

The handlers live in mixin modules next to this one (``discovery``, ``records``, ``people``, ``names``, ``lookups``,
``meta``; shared parts in ``record_base``; the stored values a question names and their hint section in
``value_spots``). The tool definitions (names, descriptions, input schemas, handlers:
``TOOL_ORDER``, ``SCHEMAS``, ``RECORD_TOOLS``) live in ``sf_assistant.tools.registry``. This module keeps the 47 core
methods of :class:`RecordTools`. It exports ``RecordTools`` and ``TOOL_ORDER`` (OLD ``okrec/tools.py`` ``__all__``
without the limit ``MAX_RESULT_CHARS``); every other name of OLD ``okrec/tools.py`` is imported from the module that
defines it (``registry``, ``record_base``, a mixin module). A limit (``MAX_RESULT_CHARS``, ``SCAN_BUDGET_S``,
``PEOPLE_LISTED`` ...) lives in one module and every reader reads it there at call time: read and patch it there.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import threading
import time
from decimal import Decimal
from typing import Any

from sf_assistant.engine.catalog import (READ_AS, SYSTEM_LOOKUPS, Catalog, ObjectSpec, PathRef, RecordError,
                                         blocked_text, contact_text, norm_name)
from sf_assistant.engine.contact import asks_for_contact
from sf_assistant.engine.sql_guard import quote_ident
from sf_assistant.engine.term_contracts import PERSON_TYPE, Terms   # begin_question(terms=), other_record_types
from sf_assistant.engine.periods import Period, PeriodError, parse_period, today_in
from sf_assistant.engine.query_builder import (BuildCtx, Col, FilterOut, Frag, Scope, date_fields_text, filter_frag,
                                               join_frags, name_norm_sql, period_frag, related_frag,
                                               sensitive_object_text, where_sql)
from sf_assistant.engine.types import (DATA_TOOLS, ORIENTATION_TOOLS, QueryEvidence, RecordsStatus, Statement,
                                       display_path)
from sf_assistant.engine.warehouse import WarehouseError
from sf_assistant.tools import record_base as _base   # the limit MAX_RESULT_CHARS: read (and patched) there
from sf_assistant.tools.registry import _ARG_HINTS, OPS, RECORD_TOOLS, TOOL_ALIASES, TOOL_ORDER, Tool
from sf_assistant.tools.record_base import _Call, _cell, _disp, _role, _round

# The tool groups of RecordTools, one mixin module each.
from sf_assistant.tools.discovery import DiscoveryMixin
from sf_assistant.tools.records import RecordQueryMixin
from sf_assistant.tools.people import PeopleMixin
from sf_assistant.tools.names import NamesMixin
from sf_assistant.tools.lookups import LookupsMixin
from sf_assistant.tools.meta import MetaMixin
from sf_assistant.tools.value_spots import ValueSpotsMixin
from sf_assistant.tools.term_enforce import TermEnforceMixin   # the business-term contracts (2026-11-24)
# the spelling help's hint section (docs/resolver_integration.md (1)): the knowledge side's text and line format
from sf_assistant.engine.business_terms import is_not_the_term, not_the_term   # the owner's terms (2026-11-16)
from sf_assistant.tools.knowledge_tools import (SpellingSpan, _metadata_targets, corrected_text, question_spans,
                                               spelling_section)


__all__ = ["RecordTools", "TOOL_ORDER"]

_HARMLESS_KEYS = frozenset({"reason", "purpose", "explanation", "thought", "thinking", "note", "notes", "description"})
_TRIM_KEYS = ("also_held", "child_people", "rows", "groups", "buckets", "values", "matches", "changes", "children", "fields", "objects",
              "date_fields", "matching_values", "segments", "record_types", "tracked_fields", "value_matches",
              "system_references", "references", "matched_records", "links")
#: Keys the last-resort trimming of ``_fit`` never removes.
_PROTECTED_KEYS = frozenset({"query_id", "object", "notes", "lookup_check", "same_target_lookups", "lookup_groups",
                             "split_totals", "unsynced_fits", "source", "no_data_fields", "people", "links",
                             "links_checked", "pairs_without_links", "not_found", "same_name", "scope", "asked",
                             "partial", "not_checked", "more_people", "references_partial", "read_as"})
#: The keys of a correction in ``result["read_as"]`` and ``QueryEvidence.read_as`` (docs/resolver_integration.md (3)).
READ_AS_KEYS = ("arg", "typed", "used", "kind", "target", "how")


def _arg_path(args: Any, typed: str, path: str = "") -> str | None:
    """The path of the first argument whose text is ``typed`` (or a dotted part of it): ``object``,
    ``filters[0].field``, ``filters[1].values[0]``; None when no argument holds it."""
    if isinstance(args, dict):
        for k, v in args.items():
            got = _arg_path(v, typed, f"{path}.{k}" if path else str(k))
            if got is not None:
                return got
    elif isinstance(args, list):
        for i, v in enumerate(args):
            got = _arg_path(v, typed, f"{path}[{i}]")
            if got is not None:
                return got
    elif isinstance(args, (str, int, float)) and not isinstance(args, bool):
        s = str(args).strip()
        if s == typed or typed in (p.strip() for p in s.split(".")):
            return path or None
    return None


def _corrections(sink: list[dict[str, Any]] | None, received: Any) -> list[dict[str, Any]]:
    """The corrections of one call's READ_AS channel as ``result["read_as"]`` / ``QueryEvidence.read_as`` entries
    (:data:`READ_AS_KEYS`), each with the path of the argument it came from."""
    out: list[dict[str, Any]] = []
    for e in sink or []:
        if e.get("arg") is None:
            e["arg"] = _arg_path(received, str(e.get("typed") or ""))
        out.append({k: e.get(k) for k in READ_AS_KEYS})
    return out


def _read_as(call: _Call, received: Any) -> list[dict[str, Any]]:
    """:func:`_corrections` of a successful call; the note of each correction that is not already among the call's
    notes (the value notes are) goes first in them."""
    notes = [e["note"] for e in call.read_as or [] if e.get("note") and e["note"] not in call.notes]
    call.notes[:0] = list(dict.fromkeys(notes))
    return _corrections(call.read_as, received)

_OP_SYNONYMS = {"=": "eq", "==": "eq", "equals": "eq", "is": "eq", "equal": "eq", "!=": "ne", "<>": "ne",
                "not_equals": "ne", "is_not": "ne", "not_equal": "ne", "like": "contains", "ilike": "contains",
                "includes": "contains", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte", "in": "in", "not in": "not_in",
                "not_in": "not_in", "null": "is_blank", "empty": "is_blank", "is_null": "is_blank", "blank": "is_blank",
                "not_null": "is_not_blank", "is_not_null": "is_not_blank", "not_blank": "is_not_blank",
                "not_empty": "is_not_blank", "is not null": "is_not_blank", "is null": "is_blank",
                "startswith": "starts_with", "starts with": "starts_with", "not contains": "not_contains",
                "does_not_contain": "not_contains"}
_ENUM_SYNONYMS = {"monthly": "month", "daily": "day", "weekly": "week", "quarterly": "quarter", "yearly": "year",
                  "annual": "year", "annually": "year", "average": "avg", "mean": "avg", "total": "sum",
                  "minimum": "min", "maximum": "max", "distinct": "count_distinct", "count distinct": "count_distinct",
                  "descending": "desc", "ascending": "asc"}

#: Integer arguments that are part of the question's meaning: below the minimum they are refused, not clamped.
_NO_CLAMP = {"min_count": "For 'has at least one' omit min_count (exists=true); for 'has none' use exists=false."}
#: What the "also stored in" overlap of a filter is counted against, by default (see ``_also_notes``).
RESULT_CONDS = "all conditions of this query"

#: The note okrec.values.ground_value adds when a filter value is also stored in another field of the object.
_ALSO_RE = re.compile(r"^'(?P<val>.+)' is also stored in (?P<fld>\w+) \((?P<n>\d+) records(?:, legacy value)?\)$")


class ToolArgError(RecordError):
    """Argument validation error (E11*)."""


# ------------------------------------------------------------------------------------- registry

class RecordTools(DiscoveryMixin, RecordQueryMixin, PeopleMixin, NamesMixin, LookupsMixin, MetaMixin,
                  ValueSpotsMixin, TermEnforceMixin):
    """Registry of the 14 record tools over one open warehouse snapshot."""

    tools: dict[str, Tool]

    def __init__(self, warehouse: Any, catalog: Catalog, rcfg: Any, tz: str, tz_from: str,
                 meta: dict | None = None) -> None:
        self.warehouse = warehouse
        self.catalog = catalog
        self.rcfg = rcfg
        self.tz = tz
        self.tz_from = tz_from
        self.meta = meta or {}
        self.made_private = bool(getattr(warehouse, "made_private", False))
        self._lock = threading.Lock()
        self._evidence: list[QueryEvidence] = []
        self._qn = 0
        self._question = ""
        self._index_lock = threading.Lock()
        self._names_full: dict[str, list[tuple]] | None = None    # name_key(name) -> [(object, Id, name, detail)]
        self._names_fl: dict[str, list[tuple]] | None = None      # "first last" of names of 3 or more words -> records
        self._names_first: dict[str, list[tuple]] | None = None   # first word -> records of every person object
        self._names_last: dict[str, list[tuple]] = {}              # last word of names of 2 or more words -> records
        self._company_ids: set[str] = set()                        # Account Ids of client companies (no person)
        self._names_partial = False   # the name index stopped early (time budget or row cap)
        self._vocab: set[str] | None = None
        self._design: frozenset[str] | None = None   # design_words(): catalog vocabulary, knowledge words, stopwords
        self._given_people: list[dict] | None = None   # people of the question given by the session (begin_question)
        self._given_values: list[dict] | None = None   # value spots of the question given by the session (begin_question)
        self.resolver_lines: list[str] = []   # start-up lines of the spelling help (open_records; [] when it is off)
        self._reset_caches()
        org = warehouse.org() if hasattr(warehouse, "org") else {}
        self.org = org or {}
        self.fiscal_start = int(self.org.get("FiscalYearStartMonth") or 1) if str(
            self.org.get("FiscalYearStartMonth") or "1").isdigit() else 1
        self.tools = {}
        for d in RECORD_TOOLS:   # sf_assistant.tools.registry: TOOL_ORDER, SCHEMAS, RECORD_HANDLERS
            self.tools[d.name] = d.bind(self)

    # ================================================================== public API
    def list_tools(self) -> list[dict]:
        """MCP ``tools/list`` shape."""
        return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema} for t in self.tools.values()]

    def openai_tools(self) -> list[dict]:
        """OpenAI chat-completions ``tools`` payload."""
        return [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                  "parameters": t.input_schema}} for t in self.tools.values()]

    def begin_question(self, question: str = "", people: list[dict] | None = None,
                       values: list[dict] | None = None, terms: Terms | None = None,
                       contact_ok: bool | None = None) -> None:
        """Clear the evidence log, restart query ids at q1, clear the per-question caches (names, links, reference
        scans) and keep ``question`` for the role cues (``cues`` and ``fits`` of lookup checks) and for the contact rule
        (phase 1: :func:`sf_assistant.engine.contact.asks_for_contact`; the contact fields are shown, listed, grouped,
        filtered by value and added to profiles only when the question asks for contact details; "" never asks).

        ``people``: the people of the question the session confirmed (:meth:`spot_people` entries, RECORDS_SPEC §22.3);
        the hints and :meth:`question_people` use them. None (the default) spots them here, as before.

        ``values``: the stored values the question names that the session's triage kept (:meth:`spot_values` entries,
        the listed ones); the hint section of the named records uses them. None (the default) spots them here.

        The business-term contracts (2026-11-24): the terms the question names are read here once
        (:meth:`~sf_assistant.tools.term_enforce.TermEnforceMixin._terms_for`); ``call`` enforces them on every record
        query tool. "" names none, so a call with no question is never changed.

        ``terms`` (Salesforce conversation memory, design v2 §7.1): the terms a follow-up runs with, rebuilt by code
        from the stored plan's typed words and the person's typed follow-up (``agent.query_state.terms_for``), in
        place of the terms of ``question``. None (the default) reads them from ``question`` as before, so a job that
        passes none gets exactly today's terms and never the terms of the job before it (C15). The contact rule is
        read from ``question`` alone, unless ``contact_ok`` says it: whether the person's typed question asks for
        contact details, for a follow-up whose ``question`` is code's plan line, which can hold a typed value with a
        contact word ("Phone Support"; stage-2 review fix PE1). No contact flag is ever carried from a job before."""
        with self._lock:
            self._evidence = []
            self._qn = 0
            self._question = str(question or "")
            self._contact_ok = asks_for_contact(self._question) if contact_ok is None else bool(contact_ok)
            self._terms = terms if isinstance(terms, Terms) else self._terms_for(self._question)
            self._given_people = [dict(p) for p in people if isinstance(p, dict)] if people is not None else None
            self._given_values = [dict(v) for v in values if isinstance(v, dict)] if values is not None else None
            self.list_limit = None
            self._reset_caches()

    def other_record_types(self) -> tuple[str, ...]:
        """The Account record type names and developer names other than Person Account: the record-type override
        words ``engine.term_contracts.detect`` reads, as :meth:`TermEnforceMixin._terms_for
        <sf_assistant.tools.term_enforce.TermEnforceMixin._terms_for>` reads them (conversation memory rebuilds a
        follow-up's terms with them, ``agent.query_state.terms_for``)."""
        acc = self.catalog.objects.get("Account")
        names: list[str] = []
        for r in (acc.record_types if acc is not None else []):
            if r.get("developer_name") != PERSON_TYPE:
                names += [str(x) for x in (r.get("name"), r.get("developer_name")) if x and x != "(none)"]
        return tuple(names)

    def _reset_caches(self) -> None:
        self._g1_rows: dict[tuple[str, str], list[tuple]] = {}        # (object, norm name or ~pattern) -> rows
        self._g1_st: dict[tuple[str, str], Statement] = {}
        self._g2_rows: dict[tuple[str, str], list[tuple[str, int]]] = {}
        self._g2_st: dict[tuple[str, str], Statement] = {}
        self._fwd: dict[str, list[tuple]] = {}                        # src Id -> [(link, object, Id, name, detail)]
        self._fwd_src: dict[str, tuple] = {}                          # src Id -> (object, Id, name, detail)
        self._fwd_st: dict[str, Statement] = {}
        self._rev: dict[str, list[str]] = {}                          # dst Id -> src Ids
        self._rev_st: dict[str, Statement] = {}
        self._scan_counts: dict[tuple[str, str, str], int] = {}       # (child, via, Id) -> records
        self._scan_st: dict[tuple[str, str, str], Statement] = {}
        self._noun_refs: list[dict] = []                              # hint time: objects the counted noun can mean

    # ================================================================== spelling help: question hints
    def object_hints(self, question: str, missing: Any = ()) -> str:
        """:meth:`NamesMixin.object_hints <sf_assistant.tools.names.NamesMixin.object_hints>`; with the spelling help
        on (``catalog.resolver``, docs/resolver_integration.md (1)) the question's misspelt names come first.

        The objects and the stored values are searched with the corrected question (:func:`corrected_text`:
        ``backgrond chech`` finds the background-check objects, ``Sheduled`` the stored value), and a first section
        "Spelling (read by code ...)" lists each misspelt word or phrase with its spelling (``- 'accont' -> Account
        (object Account)``) or its did-you-means (``- 'persn' may mean: ...``). Person names are not listed there and
        stay as typed: the people lines cover them, for the people of the question as typed (:meth:`_question_spots`).
        Off (``SFA_RESOLVER=off``: ``catalog.resolver`` None): exactly the mixin's hints. When the question (as typed
        or corrected) names a business term, a spelling whose only metadata is a stored value that merely shares the
        term's word (``engine.business_terms.NOT_THE_TERM``: 'placd' -> Account.Candidate_Status__c 'Placed') is not
        listed (review of 2026-11-16)."""
        if self.catalog.resolver is None:
            return NamesMixin.object_hints(self, question, missing)
        spans = self._spelling_spans(question)
        fixed = corrected_text(question, spans)
        hints = NamesMixin.object_hints(self, fixed, missing)
        other = not_the_term(question) | not_the_term(fixed)
        section = spelling_section(self._without_other_reading(spans, other) if other else spans)
        if not section:
            return hints
        head = "[Hints from code, not from the user]"
        if not hints:
            return head + "\n" + section
        first, _, rest = hints.partition("\n")
        return first + "\n" + section + ("\n" + rest if rest else "")

    def _without_other_reading(self, spans: list, other: Any) -> list:
        """``spans`` with no spelling that names only a stored value of ``other`` (``engine.business_terms``
        :func:`not_the_term`): such a did-you-mean option is dropped, a span left with none is dropped, and so is a
        correction whose every target is such a value. The corrected text the hints search with is not changed."""
        resolver = self.catalog.resolver

        def only_other(targets: Any) -> bool:
            targets = list(targets or ())
            if not targets:
                return False
            for kind, target in targets:
                if kind != "value" or "=" not in str(target):
                    return False
                place, value = str(target).split("=", 1)
                obj, _, field = place.partition(".")
                if not is_not_the_term(other, obj, field, value):
                    return False
            return True

        def targets_of(text: str) -> tuple:
            try:   # what the did-you-mean was kept for (knowledge_tools._hint_span, stored values included)
                return _metadata_targets(resolver, text, True)
            except Exception:  # noqa: BLE001 - the spelling help is optional
                return ()

        out = []
        for s in spans:
            if s.decision == "corrected":
                if not only_other(s.targets):
                    out.append(s)
                continue
            options = tuple(o for o in s.options if not only_other(targets_of(o)))
            if options:
                out.append(s if options == s.options else SpellingSpan(s.typed, s.start, s.end, s.decision, s.used,
                                                                       options, s.targets))
        return out

    def _question_spots(self, question: str) -> list[dict]:
        """:meth:`NamesMixin._question_spots`; the corrected form of the current question (what :meth:`object_hints`
        searches with when the spelling help is on) stands for the question itself, so the hints keep the people the
        session gave for the question as typed."""
        if self.catalog.resolver is not None and question != self._question and self._question \
                and question == corrected_text(self._question, self._spelling_spans(self._question)):
            question = self._question
        return NamesMixin._question_spots(self, question)

    def _spelling_spans(self, question: str) -> list:
        """The misspelt metadata names of ``question`` (``knowledge_tools.question_spans`` over the catalog's resolver,
        stored values included, person names left out), cached for the last question; [] when the spelling help is
        off."""
        cached = self._spelling_cache
        if cached is not None and cached[0] == question:
            return cached[1]
        spans = question_spans(self.catalog.resolver, question, stored_values=True)
        self._spelling_cache = (question, spans)
        return spans

    _spelling_cache: tuple[str, list] | None = None   # (question, its spans) of _spelling_spans
    _contact_ok: bool = False   # the current question asks for contact details (begin_question; phase 1)
    #: development A (2026-11-27): the rows a list_records call without a ``limit`` returns in this question; None (the
    #: default, and after begin_question) keeps the tool's default of 10. The record session sets it for a list
    #: question; the call's ``arguments`` (evidence, stored plans) stay what the model sent.
    list_limit: int | None = None

    @property
    def evidence(self) -> list[QueryEvidence]:
        """Copy of the evidence log, in call order."""
        with self._lock:
            return list(self._evidence)

    def close(self) -> None:
        self.warehouse.close()

    def today(self) -> dt.date:
        return self.rcfg.today or today_in(self.tz)

    def build_ctx(self) -> BuildCtx:
        return BuildCtx(self.catalog, self.tz, self.today(), contact_ok=self._contact_ok)

    def origin(self) -> str:
        sb = self.org.get("IsSandbox")
        return "preprod SANDBOX copy" if sb is True else "PRODUCTION copy" if sb is False else "copy of unknown origin"

    def status(self) -> RecordsStatus:
        """Where the data comes from and how fresh it is (recomputed now)."""
        if self.meta.get("copied_at_utc"):
            copied, frm = str(self.meta["copied_at_utc"]), "meta"
        else:
            mt = self.warehouse.file_mtime_utc() if hasattr(self.warehouse, "file_mtime_utc") else None
            copied, frm = (mt.strftime("%Y-%m-%d %H:%M UTC") if mt else None), "file time"
        newest = max(self.catalog.sync.values()) if self.catalog.sync else None
        age = None
        if newest is not None:
            now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            age = round((now - newest).total_seconds() / 3600.0, 1)
        sb = self.org.get("IsSandbox")
        return RecordsStatus(
            snapshot_path=display_path(self.warehouse.path), source=f"{self.rcfg.container}:/data/warehouse.read.duckdb",
            copied_at_utc=copied, copied_at_from=frm, org_is_sandbox=None if sb is None else bool(sb),
            origin=self.origin(), newest_sync_utc=newest.strftime("%Y-%m-%d %H:%M UTC") if newest else None,
            age_hours=age, stale=bool(age is not None and age > self.rcfg.max_age_h), time_zone=self.tz,
            time_zone_from=self.tz_from, today=self.today().isoformat(), today_pinned=self.rcfg.today is not None,
            objects=len(self.catalog.queryable()), duckdb_version=str(getattr(self.warehouse, "duckdb_version", "")))

    # ================================================================== call
    def call(self, name: str, arguments: dict | None = None) -> dict:
        """Run a tool. Never raises. ``{"ok","result","error","chunk_ids"}``; result JSON <= 12,000 chars.

        An alias (:data:`TOOL_ALIASES`: ``find_references`` with ``name``) runs its tool (``about_people`` with
        ``names=[name]``) and is logged as that tool.

        Business-term contracts (2026-11-24): between the argument check and the handler the question's terms may add
        a filter, set an argument or refuse the call (T1, T2, T3: ``tools/term_enforce.py``); an added filter that
        would pass the join cap is left out and the call runs again without it, with a note."""
        t0 = time.perf_counter()
        received = copy.deepcopy(arguments) if isinstance(arguments, dict) else {"_raw": repr(arguments)[:500]}
        if isinstance(name, str) and name in TOOL_ALIASES:
            name, arguments = TOOL_ALIASES[name], self._alias_args(arguments)
        elif name == "about_people":
            arguments = self._alias_args(arguments)
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            return {"ok": False, "result": None, "error": f"unknown tool '{name}'; available: {', '.join(TOOL_ORDER)}",
                    "chunk_ids": []}
        with self._lock:
            self._qn += 1
            qid = f"q{self._qn}"
        call: _Call | None = None
        out: dict
        code: str | None = None
        # the spelling help (SFA_RESOLVER=on): the correction channel of this call (docs/resolver_integration.md (3))
        sink: list[dict[str, Any]] | None = [] if self.catalog.resolver is not None else None
        token = READ_AS.set(sink) if sink is not None else None
        try:
            call = _Call(self, name)
            call.read_as = sink
            args, lenient = self._validate(tool, arguments)
            if name == "list_records" and self.list_limit and "limit" not in args:   # a list question (development A)
                args = {**args, "limit": int(self.list_limit)}
            for n in lenient:
                call.note(n)
            run_args = self._enforce_terms(call, name, args)
            try:
                facts = tool.handler(call, **run_args)
            except RecordError as exc:
                if not self._join_capped(exc, call):
                    raise
                plan, call = call.term_plan, _Call(self, name)   # the added filter passes the join cap: without it
                if sink is not None:
                    sink.clear()
                call.read_as = sink
                for n in lenient + [x for x in plan.notes if x not in plan.added_notes] + plan.unapplied:
                    call.note(n)
                call.term_plan, call.terms_dropped = plan, True
                run_args = self._without_added(args, plan)
                call.enforced = run_args if plan.set_args else None
                facts = tool.handler(call, **run_args)
            self._finish_terms(call, facts)
            facts.update(self._finish_names(call))
            if call.no_data:   # a person filter on a lookup with no data: the data does not record it (never "0")
                facts["no_data_fields"] = list(dict.fromkeys(call.no_data))
            facts.update(self._unsynced_check(call))
            self._also_notes(call)
            facts.update(self._mirror_notes(name, call))
            result: dict[str, Any] = {"query_id": qid}
            if call.object:
                result["object"] = call.object
            result.update(facts)
            if call.applied:
                result["applied"] = {k: v for k, v in call.applied.items() if v not in (None, [], {}, "")}
            self._sync_notes(call)
            read_as = _read_as(call, received) if sink else []
            if call.notes:
                result["notes"] = list(call.notes)
            if read_as:
                result["read_as"] = read_as
            result["source"] = self._source(call)
            # development A (2026-11-27): the rows of a list before the size cut; code writes every one of them under
            # the answer (agent/tables.py), while the model reads the cut result
            listed = result.get("rows") if name == "list_records" and isinstance(result.get("rows"), list) else None
            result = self._fit(result, call.max_chars)
            if listed is not None and isinstance(result.get("rows"), list) and len(result["rows"]) < len(listed):
                call.full_rows = list(listed)
            json.dumps(result)
            out = {"ok": True, "result": result, "error": None, "chunk_ids": self._chunk_ids(call)}
        except RecordError as exc:
            out = {"ok": False, "result": None, "error": str(exc), "chunk_ids": []}
            code = getattr(exc, "code", None) or "E"
            dym = getattr(exc, "did_you_mean", None)
            if call is not None and isinstance(dym, dict):
                call.did_you_mean.append(dict(dym))
        except PeriodError as exc:
            out = {"ok": False, "result": None, "error": str(exc), "chunk_ids": []}
            code = getattr(exc, "code", None) or "E5"
        except Exception as exc:  # defensive: a tool must never crash the agent loop
            if call is not None:
                call.detail = f"{type(exc).__name__}: {exc}"
            out = {"ok": False, "result": None, "error": "internal error in the record tools (not in your call): "
                   f"{type(exc).__name__}; try a simpler query.", "chunk_ids": []}
            code = "internal"
        finally:
            if token is not None:
                READ_AS.reset(token)
        if not out["ok"] and call is not None and call.term_plan is not None:   # the call failed: no filter applied,
            call.notes[:] = [n for n in call.notes if n not in call.term_plan.added_notes]   # so no note says so
        self._log(qid, name, received, out, call, t0, code)
        return out

    @staticmethod
    def _alias_args(arguments: Any) -> Any:
        """about_people arguments from the alias's (or a one-name call's) ``name``: ``names=[name]``; a ``names``
        string becomes a list of one name."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                return arguments
        if not isinstance(arguments, dict):
            return arguments
        args = dict(arguments)
        if "names" not in args and isinstance(args.get("name"), str):
            args["names"] = [args.pop("name")]
        elif "name" in args and isinstance(args.get("names"), list) and isinstance(args.get("name"), str):
            nm = args.pop("name")
            if nm.strip() and nm not in args["names"]:
                args["names"] = [nm] + list(args["names"])
        if isinstance(args.get("names"), str):
            args["names"] = [args["names"]]
        return args

    def _log(self, qid: str, name: str, received: dict, out: dict, call: _Call | None, t0: float,
             code: str | None = None) -> None:
        if name not in DATA_TOOLS and name not in ORIENTATION_TOOLS:
            return
        ok = bool(out.get("ok"))
        err = out.get("error")
        if call is not None and call.detail and not ok:
            err = f"{err} [detail: {call.detail}]"
        res = out.get("result") if ok else None
        tables = list(call.tables) if call else []
        ev = QueryEvidence(
            query_id=qid, tool=name, kind="data" if name in DATA_TOOLS else "orientation",
            object=call.object if call else None, arguments=received, ok=ok, error=None if ok else err,
            applied=dict(res.get("applied") or {}) if ok and isinstance(res, dict) else {},
            notes=list(call.notes) if call else [], statements=list(call.statements) if call else [],
            tables=tables, synced_at={t: self.catalog.synced_at(t) for t in tables},
            summary=(call.summary if call and ok else "error"), model_result=res,
            elapsed_ms=round((time.perf_counter() - t0) * 1000, 2), error_code=None if ok else code,
            fields_used=list(dict.fromkeys(call.fields_used)) if call else [],
            not_found=[dict(x) for x in call.not_found] if call and not ok else [],
            names_matched=self._names_matched(call) if call and ok else [])
        if call is not None and (call.read_as or call.did_you_mean):   # the spelling help (resolver_integration (3))
            ev.read_as = _corrections(call.read_as, received)
            ev.did_you_mean = [dict(x) for x in call.did_you_mean] if not ok else []
        if call is not None and call.enforced is not None:   # the business-term contracts: what the handler ran with
            ev.enforced_arguments = copy.deepcopy(call.enforced)
        if ok and call is not None and call.full_rows is not None:   # development A: the rows the size cut left out
            ev.full_rows = call.full_rows
        with self._lock:
            self._evidence.append(ev)

    @staticmethod
    def _names_matched(call: _Call) -> list[dict[str, Any]]:
        """``QueryEvidence.names_matched``: the call's positive name filters (a lookup, a path or the base ``Name``)
        whose names matched stored records of the filter's target, with the distinct stored names (at most 5)."""
        out: list[dict[str, Any]] = []
        for chk, _o, _b in call.name_checks:
            if not chk.positive:
                continue
            names = list(dict.fromkeys(_disp(r[1]) for t in chk.targets for r in chk.found.get(t, []) if r[1]))
            if names:
                out.append({"filter": chk.shown, "names": names[:5],
                            "match": "contains" if chk.pattern is not None else "exact"})
        return out

    # ================================================================== validation (§9.2)
    def _validate(self, tool: Tool, arguments: Any) -> tuple[dict, list[str]]:
        notes: list[str] = []
        if arguments is None:
            arguments = {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                raise ToolArgError("arguments were not valid JSON; resend the call with a JSON object", "E11d") from None
        if not isinstance(arguments, dict):
            raise ToolArgError("arguments must be a JSON object, e.g. {\"object\": \"Interview__c\"}", "E11d")
        if "_raw" in arguments and len(arguments) == 1:
            raise ToolArgError("arguments were not valid JSON; resend the call with a JSON object", "E11d")
        schema = tool.input_schema
        props = schema.get("properties", {})
        args = {k: v for k, v in arguments.items()
                if v is not None and not (isinstance(v, str) and not v.strip()) and v != [] and v != {}}
        ignored = [k for k in args if k not in props and k in _HARMLESS_KEYS]
        for k in ignored:
            args.pop(k)
        if ignored:
            notes.append(f"ignored argument(s): {', '.join(ignored)}")
        unknown = [k for k in args if k not in props]
        if unknown:
            extra = ""
            if tool.name == "compare_records" and "period" in unknown:
                extra = " Put each period in its segment (segments[i].period) and set date_field."
            raise ToolArgError(f"{tool.name} has no argument '{unknown[0]}'. Allowed: {', '.join(props)}.{extra}", "E11a")
        # group_by given as a list
        if "group_by" in props and isinstance(args.get("group_by"), list):
            gb = [g for g in args["group_by"] if g]
            if len(gb) == 1:
                args["group_by"] = gb[0]
                notes.append("group_by list of one field read as that field")
            elif len(gb) == 2 and "split_by" in props and "split_by" not in args:
                args["group_by"], args["split_by"] = gb
                notes.append(f"group_by list read as group_by={gb[0]} and split_by={gb[1]}")
        out: dict[str, Any] = {}
        for key, spec in props.items():
            if key not in args:
                continue
            out[key] = self._coerce(tool.name, key, args[key], spec, notes, key)
        missing = [r for r in schema.get("required", []) if r not in out]
        if missing:
            r = missing[0]
            raise ToolArgError(f"{tool.name} needs '{r}' ({_ARG_HINTS.get(r, props[r].get('description', r))}).", "E11c")
        return out, notes

    def _coerce(self, tool: str, key: str, val: Any, spec: dict, notes: list[str], where: str) -> Any:
        typ = spec.get("type")
        if key in ("filters", "part_filters", "base_filters", "any_of") or (where.endswith(".filters")):
            val = self._filters_shape(val, where, notes)
        if typ == "array":
            if isinstance(val, (str, dict)) and key in ("fields", "objects"):
                val = [val] if isinstance(val, dict) else [v.strip() for v in val.split(",") if v.strip()]
            if isinstance(val, dict) and key in ("related", "segments"):
                val = [val]
            if not isinstance(val, list):
                raise ToolArgError(f"'{where}' must be a list, got {type(val).__name__}", "E11d")
            mx, mn = spec.get("maxItems"), spec.get("minItems")
            if mx is not None and len(val) > mx:
                raise ToolArgError(f"'{where}' allows at most {mx} items, got {len(val)}", "E11d")
            if mn is not None and len(val) < mn:
                if key == "segments":
                    raise ToolArgError("compare needs at least 2 segments.", "E7")
                if key == "any_of":
                    raise ToolArgError("any_of needs at least 2 items (an OR group); for one condition use filters.", "E11d")
                raise ToolArgError(f"'{where}' needs at least {mn} item(s)", "E11d")
            items = spec.get("items") or {}
            return [self._coerce(tool, key + "[]", v, items, notes, f"{where} item {i + 1}") for i, v in enumerate(val)]
        if typ == "object":
            if not isinstance(val, dict):
                raise ToolArgError(f"'{where}' must be an object, got {type(val).__name__}", "E11d")
            props = spec.get("properties", {})
            val = {k: v for k, v in val.items() if v is not None and not (isinstance(v, str) and not v.strip() and k != "value")}
            if "op" in props and "field" in props:
                val = self._filter_item(val, where, notes)
            for k in val:
                if k not in props:
                    hint = " Put time windows in the top-level date_field + period." if k in ("period", "date_field") \
                        else ""
                    raise ToolArgError(f"{where} has unknown key '{k}'; allowed keys: {', '.join(props)}.{hint}", "E11b")
            res = {}
            for k, sub in props.items():
                if k in val:
                    res[k] = self._coerce(tool, k, val[k], sub, notes, f"{where}.{k}")
            for r in spec.get("required", []):
                if r not in res:
                    raise ToolArgError(f"{where} needs '{r}'.", "E11c")
            return res
        if isinstance(typ, list):  # SCALAR
            if isinstance(val, (list, dict)):
                raise ToolArgError(f"'{where}' must be a single value (text, number or true/false), got "
                                   f"{type(val).__name__}", "E11d")
            return val
        if typ == "string":
            if isinstance(val, (list, dict)):
                raise ToolArgError(f"'{where}' must be a string, got {type(val).__name__}", "E11d")
            s = str(val).strip() if not isinstance(val, bool) else ("true" if val else "false")
            enum = spec.get("enum")
            if enum:
                low = s.lower()
                low = _ENUM_SYNONYMS.get(low, low)
                if key == "op" and spec is not None and low in _OP_SYNONYMS:
                    low = _OP_SYNONYMS[low]
                if low not in enum:
                    raise ToolArgError(f"'{where}' must be one of {', '.join(enum)}, got '{s}'", "E11d")
                if low != s:
                    notes.append(f"{where} '{s}' read as '{low}'")
                return low
            return s
        if typ == "integer":
            if isinstance(val, bool):
                raise ToolArgError(f"'{where}' must be integer, got boolean", "E11d")
            try:
                if isinstance(val, float) and val != int(val):
                    raise ValueError
                n = int(str(val).strip()) if not isinstance(val, (int, float)) else int(val)
            except (TypeError, ValueError):
                raise ToolArgError(f"'{where}' must be integer, got {val!r}", "E11d") from None
            lo, hi = spec.get("minimum"), spec.get("maximum")
            if key in _NO_CLAMP and lo is not None and n < lo:  # a threshold of the question: never change it
                raise ToolArgError(f"'{where}' must be at least {lo}, got {n}. {_NO_CLAMP[key]}", "E11d")
            m = n
            if lo is not None and n < lo:
                m = lo
            if hi is not None and n > hi:
                m = hi
            if m != n:
                notes.append(f"{where} {n} clamped to {m}")
            return m
        if typ == "number":
            if isinstance(val, bool):
                raise ToolArgError(f"'{where}' must be number, got boolean", "E11d")
            try:
                return float(str(val).strip().rstrip("%")) if not isinstance(val, (int, float)) else val
            except ValueError:
                raise ToolArgError(f"'{where}' must be number, got {val!r}", "E11d") from None
        if typ == "boolean":
            if isinstance(val, bool):
                return val
            s = str(val).strip().lower()
            if s in ("true", "yes", "1"):
                notes.append(f"{where} '{val}' read as true")
                return True
            if s in ("false", "no", "0"):
                notes.append(f"{where} '{val}' read as false")
                return False
            raise ToolArgError(f"'{where}' must be boolean (true or false), got {val!r}", "E11d")
        return val

    @staticmethod
    def _filters_shape(val: Any, where: str, notes: list[str]) -> Any:
        if isinstance(val, dict):
            if "field" in val or "op" in val:
                notes.append(f"{where} given as one object; read as a list of one filter")
                return [val]
            items = []
            for k, v in val.items():
                if isinstance(v, list):
                    items.append({"field": k, "op": "in", "values": v})
                else:
                    items.append({"field": k, "op": "eq", "value": v})
            notes.append(f"{where} given as {{field: value}}; read as eq filters")
            return items
        return val

    @staticmethod
    def _filter_item(item: dict, where: str, notes: list[str]) -> dict:
        item = dict(item)
        op = item.get("op")
        if isinstance(op, str):
            low = op.strip().lower()
            mapped = _OP_SYNONYMS.get(low, low.replace(" ", "_") if low.replace(" ", "_") in OPS else low)
            if mapped != op:
                if mapped in OPS:
                    notes.append(f"{where} op '{op}' read as '{mapped}'")
                item["op"] = mapped
        op = item.get("op")
        if isinstance(item.get("value"), list):
            vals = item.pop("value")
            if op in ("eq", "in"):
                item["op"], item["values"] = "in", vals
                if op == "eq":
                    notes.append(f"{where} eq with a list read as in")
            elif op in ("ne", "not_in"):
                item["op"], item["values"] = "not_in", vals
                if op == "ne":
                    notes.append(f"{where} ne with a list read as not_in")
            else:
                raise ToolArgError(f"'{where}.value' must be a single value for op {op}; use op in with values for a "
                                   "list", "E11d")
        if item.get("values") is not None and op in ("eq", "ne"):
            item["op"] = "in" if op == "eq" else "not_in"
            notes.append(f"{where} {op} with values read as {item['op']}")
        if item.get("op") in ("in", "not_in") and item.get("values") is None and item.get("value") is not None:
            item["values"] = [item.pop("value")]
        if isinstance(item.get("values"), (str, int, float, bool)):
            item["values"] = [item["values"]]
        return item

    # ================================================================== shared building
    def _object(self, call: _Call, name: Any) -> ObjectSpec:
        spec = self.catalog.resolve_object(name)
        call.object = spec.api
        call.table(spec.api)
        return spec

    def _date_ref(self, spec: ObjectSpec, name: Any) -> PathRef:
        raw = str(name or "").strip()
        low = raw.lower()
        if low in ("created", "created date", "creation date", "created_at"):
            raw = "CreatedDate"
        elif low in ("modified", "updated", "last modified", "last_modified", "updated_at", "last updated"):
            raw = "LastModifiedDate"
        if spec.knowledge is not None:
            ts = spec.knowledge.temporal_semantics()
            for key, v in ts.items():
                if isinstance(v, dict) and key.lower() == low and v.get("field") and v["field"] in spec.fields:
                    raw = str(v["field"])
                    break
        try:
            ref = self.catalog.resolve_path(spec, raw)
        except RecordError as exc:
            if exc.code == "E2":
                raise RecordError(f"{spec.api} has no date field '{name}'. Date fields of {spec.api}: "
                                  f"{date_fields_text(spec)}.", "E5e") from None
            raise
        if not ref.field.is_date:
            raise RecordError(f"{ref.text} is {ref.field.kind}, not a date or date/time field. Date fields of {spec.api}: "
                              f"{date_fields_text(spec)}.", "E5e")
        if ref.field.sensitivity == "blocked":
            raise RecordError(blocked_text(ref.obj, ref.field.api), "E2c")
        return ref

    def _period(self, text: str, today: dt.date) -> Period:
        try:
            return parse_period(text, today)
        except PeriodError as exc:
            raise RecordError(str(exc), exc.code) from None

    def _where(self, call: _Call, spec: ObjectSpec, scope: Scope, args: dict, *, filters_key: str = "filters",
               use_period: bool = True, keep_date_field: bool = False, result_conds: list[Frag] | None = None,
               result_name: str = RESULT_CONDS, lookup_check: bool = False) -> list[Frag]:
        """Filters, any_of, period and related -> conditions; fills call.applied and notes.

        ``result_conds`` / ``result_name``: the conditions of the result these filters belong to and its name,
        for the "also stored in" overlap notes (default: the returned conditions, "all conditions of this query").
        A caller that adds conditions later passes its own list and fills it. Every name compared on a lookup is
        grounded first (E4h for an unknown name, before the main statement); ``lookup_check`` marks ``filters`` as
        base filters whose person filters get a lookup check after the handler.
        """
        ctx = call.ctx
        conds: list[Frag] = []
        rconds = conds if result_conds is None else result_conds
        texts: list[str] = []
        outs = [filter_frag(scope, item, ctx) for item in args.get(filters_key) or []]
        any_outs = [filter_frag(scope, item, ctx) for item in args.get("any_of") or []]
        self._ground(call, outs + any_outs, [lookup_check] * len(outs) + [False] * len(any_outs))
        for out in outs:
            conds.append(out.frag)
            texts.append(out.text)
            self._filter_notes(call, scope, out, rconds, result_name)
            call.use(out.ref)
        if texts:
            call.applied["filters"] = texts
        if any_outs:
            conds.append(Frag("(") + join_frags(" OR ", [o.frag for o in any_outs]) + Frag(")"))
            call.applied["any_of"] = [o.text for o in any_outs]
            for o in any_outs:
                call.use(o.ref)
                self._filter_notes(call, scope, o, rconds, result_name)
        if use_period:
            if args.get("period"):
                if not args.get("date_field"):
                    raise RecordError(f"period needs date_field. Date fields of {spec.api}: {date_fields_text(spec)}.",
                                      "E5c")
                dref = self._date_ref(spec, args["date_field"])
                per = self._period(args["period"], ctx.today)
                pf, pa = period_frag(scope, dref, per, ctx)
                conds.append(pf)
                call.applied["period"] = pa
                call.use(dref)
                self._period_notes(call, per, ctx.today)
            elif args.get("date_field") and not keep_date_field:
                call.note("date_field is ignored without period")
        rel_texts = []
        for i, item in enumerate(args.get("related") or []):
            frag, text, notes, rel_outs = related_frag(scope, item, i + 1, ctx, resolve_date=self._date_ref)
            self._ground(call, rel_outs)
            conds.append(frag)
            rel_texts.append(text)
            for n in notes:
                call.note(n)
            child = self.catalog.objects.get(str(item.get("child_object")))
            if child is not None:
                call.table(child.api)
        if rel_texts:
            call.applied["related"] = rel_texts
        for t in scope.tables:
            call.table(t)
        return conds

    @staticmethod
    def _filter_notes(call: _Call, scope: Scope, out: FilterOut, result_conds: list[Frag],
                      result_name: str = RESULT_CONDS) -> None:
        """Add the notes of one filter; "also stored in" notes are completed by ``_also_notes``.

        ``result_conds`` holds (or will hold) every condition of the result the filter belongs to.
        """
        for n in out.notes:
            call.note(n)
            if out.ref is not None and _ALSO_RE.match(n):
                call.also.append((n, scope, out, result_conds, result_name))

    def _also_notes(self, call: _Call) -> None:
        """Complete "'v' is also stored in F (n records)" notes with how many of those n records match the result.

        Runs after the handler, so the main statement stays first. The overlap is counted with EVERY condition
        of the result the filter belongs to (filters, any_of, period, related; the base for a part, the shared
        filters for a segment), so "k of them match" means "k of them are in that result". Without it a model
        guesses whether those records are inside the result. A lookup-path filter only gets a remark.

        Step-2 fixes of 2026-10-30 (F12: a record agent read "none of them matches <result>" as "outside the period"):
        when none of them match, one more COUNT with only the result's own filter on the value says why: "none of
        them meets the filter <f>" (that filter alone leaves them out) or "those that meet the filter <f> fail another
        condition"; if that statement fails, the note keeps its short form. Neither names the period.
        """
        for text, scope, out, conds, name in call.also:
            m = _ALSO_RE.match(text)
            if m is None:
                continue
            val, fld, n = m["val"], m["fld"], int(m["n"])
            spec = scope.spec
            other = spec.fields.get(fld)
            if out.ref.hops or other is None:
                new = f"{text}; only {out.text} is filtered here, {fld} is not"
            else:
                c = f"{scope.alias}.{quote_ident(fld)}"
                has = (Frag(f"list_contains(string_split({c}, ';'), ?)", (val,)) if other.kind == "multipicklist"
                       else Frag(f"{c} = ?", (val,)))
                frag = Frag(f"SELECT COUNT(*) AS n FROM {scope.from_sql()}") + where_sql([has] + list(conds))
                try:
                    st = call.run(frag, max_rows=1)
                except RecordError:
                    continue
                k = int(st.rows[0][0]) if st.rows else 0
                if n == 1:
                    new = f"{text}; that record {'matches' if k else 'does not match'} {name}"
                elif k == n:
                    new = f"{text}; all {n} of them match {name}"
                elif k == 0:
                    try:   # F12: does the value filter alone leave them out, or another condition of the result?
                        st0 = call.run(Frag(f"SELECT COUNT(*) AS n FROM {scope.from_sql()}") + where_sql([has, out.frag]),
                                       max_rows=1)
                        k0: int | None = int(st0.rows[0][0]) if st0.rows else 0
                    except RecordError:
                        k0 = None
                    new = (f"{text}; none of them matches {name}" if k0 is None else
                           f"{text}; none of them matches {name}: none of them meets the filter {out.text}" if k0 == 0
                           else f"{text}; none of them matches {name}: those that meet the filter {out.text} fail "
                                f"another condition")
                else:
                    new = (f"{text}; {k} of them {'matches' if k == 1 else 'match'} {name}, the other "
                           f"{n - k} {'does' if n - k == 1 else 'do'} not")
            if text in call.notes:
                call.notes[call.notes.index(text)] = new
            else:  # the same value in a second filter (e.g. another compare segment)
                call.note(new)

    def _period_notes(self, call: _Call, per: Period, today: dt.date) -> None:
        if self.fiscal_start != 1:
            call.note(f"periods use calendar quarters and years; the org fiscal year starts in month {self.fiscal_start}")

    # ================================================================== names, links and scans (RECORDS_SPEC §21.4-21.6)
    def _exec(self, call: _Call | None, frag: Frag, *, max_rows: int = 1000, timeout: float | None = None) -> Statement:
        """Run through the call (logged as evidence) or, for the hints, straight through the guarded warehouse."""
        if call is not None:
            return call.run(frag, max_rows=max_rows, timeout=timeout)
        try:
            return self.warehouse.run(frag.sql, list(frag.params), max_rows=max_rows,
                                      timeout_s=timeout if timeout is not None else self.rcfg.timeout_s)
        except WarehouseError as exc:
            raise RecordError(f"a hint statement failed ({exc.kind})", "E13") from None

    def _queryable(self, api: str) -> bool:
        o = self.catalog.objects.get(api)
        return bool(o is not None and o.has_table and "Name" in o.fields and o.fields["Name"].sensitivity != "blocked"
                    and not o.sensitive)

    def _detail_sql(self, obj: str, a: str) -> tuple[str, str]:
        """(expression, extra join) of a record's detail: record type (Account), portal or standard user (User),
        the first status picklist, else NULL."""
        cat = self.catalog
        spec = cat.objects[obj]
        rt = cat.objects.get("RecordType")
        if obj == "Account" and "RecordTypeId" in spec.fields and rt is not None and rt.has_table:
            return f'{a}rt."Name"', f' LEFT JOIN main."RecordType" AS {a}rt ON {a}rt."Id" = {a}."RecordTypeId"'
        if obj == "User" and "AccountId" in spec.fields:
            return f"CASE WHEN {a}.\"AccountId\" IS NOT NULL THEN 'portal user' ELSE 'standard user' END", ""
        st = self._status_fields(spec)
        if st:
            return f"CAST({a}.{quote_ident(st[0].api)} AS VARCHAR)", ""
        return "CAST(NULL AS VARCHAR)", ""

    def _base_notes(self, call: _Call, spec: ObjectSpec) -> None:
        if spec.rows == 0:
            call.note(f"{spec.api} has 0 records in the warehouse")

    def _sync_notes(self, call: _Call) -> None:
        if not self.catalog.sync:
            return
        newest = max(self.catalog.sync.values())
        for t in call.tables:
            ts = self.catalog.sync_dt(t)
            if ts is not None and (newest - ts).total_seconds() > 24 * 3600:
                hours = round((newest - ts).total_seconds() / 3600.0)
                call.note(f"{t} was last synced {ts.strftime('%Y-%m-%d %H:%M UTC')}, {hours} h before the newest sync")

    def _mirror_notes(self, name: str, call: _Call) -> dict:
        """For a counting query that used a lookup copying a path of the same record (``FieldSpec.mirror_of``, from
        the knowledge field_dependencies) without that path: a note (the path is the source) and the result key
        ``copied_lookups`` ``[{"field", "source"}]`` (read by the session's answer check). For a lookup that copies a
        lookup of a child object (``FieldSpec.child_copy``) without that source: a note and the result key
        ``child_copy`` ``[{"field", "source", "via", "filled", "source_filled"}]`` (C6). ``{}`` otherwise. The
        lookup check's count through a copy's source (``call.copy_sources``) is added to the copy's note, else noted
        on its own."""
        spec = self.catalog.objects.get(call.object) if call.object else None
        if name not in ("count_records", "aggregate_records", "group_records", "trend_records", "compare_records",
                        "percentage_of_records", "list_records") or spec is None:
            for fapi, held in list(call.copy_sources.items()):
                call.note(f"{call.object}.{fapi}: {held}")
            call.copy_sources.clear()
            return {}
        used = {fapi for o, fapi in call.fields_used if o == spec.api}
        copied: list[dict] = []
        for fapi in dict.fromkeys(fapi for o, fapi in call.fields_used if o == spec.api):
            f = spec.fields.get(fapi)
            if f is None or not f.mirror_of or f.mirror_of.split(".")[0] in used:
                continue
            rule = f" ({f.mirror_rule.rstrip('.')})" if f.mirror_rule else ""
            call.note(f"{spec.api}.{f.api} copies {f.mirror_of}{rule}; {f.mirror_of} is the source: group or filter "
                      f"by {f.mirror_of} (the copy can be blank or out of date)")
            copied.append({"field": f.api, "source": f.mirror_of})
        # C6: a lookup that copies a lookup of a child object (FieldSpec.child_copy), when the query did not use the
        # source: the source is the record of the role, the copy can be partial
        child: list[dict] = []
        for fapi in dict.fromkeys(fapi for o, fapi in call.fields_used if o == spec.api):
            f = spec.fields.get(fapi)
            if f is None or not f.child_copy:
                continue
            c, k, lk = f.child_copy
            if any(o == c and fa == lk for o, fa in call.fields_used):
                continue
            rule = f" ({f.mirror_rule.rstrip('.')})" if f.mirror_rule else ""
            part = "a partial copy" if f.copy_source_filled > f.filled else "a copy"
            held = call.copy_sources.pop(f.api, "")   # the lookup check's count through the source (one note)
            call.note(f"{spec.api}.{f.api} is {part} of {c}.{lk}, taken from the {c} records whose {k} points to the "
                      f"{spec.api} record{rule}: it is filled on {f.filled} {spec.api} records, while "
                      f"{f.copy_source_filled} have a {c} row with {lk} set; {c}.{lk} is the source: count {c} filtered "
                      f"or grouped by {lk} (count_distinct {k} for different {spec.api} records)"
                      + (f"; {held}" if held else ""))
            child.append({"field": f.api, "source": f"{c}.{lk}", "via": f"{c}.{k}", "filled": f.filled,
                          "source_filled": f.copy_source_filled})
        for fapi, held in list(call.copy_sources.items()):   # no copy note above (the call used the source too)
            call.note(f"{spec.api}.{fapi}: {held}")
        call.copy_sources.clear()
        out: dict = {}
        if copied:
            out["copied_lookups"] = copied
        if child:
            out["child_copy"] = child
        return out

    def _source(self, call: _Call) -> str:
        """Origin and sync time of each object read; past 6 objects, the first 4 and the range of the others' sync
        times (the link finder of about_people reads about 25 objects)."""
        parts = [f"{t} synced {self.catalog.synced_at(t) or 'unknown'}" for t in call.tables]
        if len(parts) > 6:
            rest = sorted(str(self.catalog.synced_at(t) or "unknown") for t in call.tables[4:])
            parts = parts[:4] + [f"{len(rest)} more objects synced {rest[0]} to {rest[-1]}"]
        src = self.origin().lower()
        return src + ("; " + ", ".join(parts) if parts else "")

    def _chunk_ids(self, call: _Call) -> list[str]:
        out: list[str] = []
        if call.object and self.catalog.objects.get(call.object) and self.catalog.objects[call.object].documented:
            out.append(f"{call.object}::object")
        for obj, fld in call.fields_used:
            spec = self.catalog.objects.get(obj)
            if spec is not None and spec.fields.get(fld) is not None and spec.fields[fld].documented:
                out.append(f"{obj}::field:{fld}")
        return list(dict.fromkeys(out))[:30]

    def _fit(self, result: dict, limit: int = _base.MAX_RESULT_CHARS) -> dict:
        """Trim list-valued keys from the end until the result JSON fits ``limit`` (MAX_RESULT_CHARS)."""
        MAX_RESULT_CHARS = limit   # noqa: N806 - the module limit, or a tool's own (about_people with links)

        def size(r: dict) -> int:
            return len(json.dumps(r, default=str))

        if size(result) <= MAX_RESULT_CHARS:
            return result
        for key in _TRIM_KEYS:
            lst = result.get(key)
            if not isinstance(lst, list) or not lst:
                continue
            n = len(lst)
            lo, hi = 0, n
            while lo < hi:
                mid = (lo + hi + 1) // 2
                trial = dict(result)
                trial[key] = lst[:mid]
                if size(trial) <= MAX_RESULT_CHARS - 150:
                    lo = mid
                else:
                    hi = mid - 1
            result = dict(result)
            result[key] = lst[:lo]
            if key in ("rows", "groups", "buckets", "values", "matches", "changes"):
                result["shown"] = lo
            notes = list(result.get("notes") or [])
            listed = key in ("rows", "groups", "buckets", "values", "matches", "changes")
            notes.append(f"result trimmed to fit: showing {lo} of {n}" + ("" if listed else f" {key}"))
            result["notes"] = notes
            if size(result) <= MAX_RESULT_CHARS:
                return result
        while size(result) > MAX_RESULT_CHARS:
            removable = [k for k in result if k not in _PROTECTED_KEYS and k != "trimmed"]
            if not removable:
                break
            result.pop(removable[-1])
            result["trimmed"] = True
        return result

    # ================================================================== field helpers
    def _check_use(self, spec: ObjectSpec, ref: PathRef, purpose: str) -> None:
        """Sensitivity rules for selecting, grouping and listing values."""
        f = ref.field
        if f.sensitivity == "blocked":
            raise RecordError(blocked_text(ref.obj, f.api), "E2c")
        if f.sensitivity == "contact" and purpose in ("group", "values", "split", "measure") and not self._contact_ok:
            raise RecordError(contact_text(ref.obj, f.api), "E2d")
        if spec.sensitive:
            if purpose in ("list", "values_text", "history"):
                raise RecordError(sensitive_object_text(spec.api), "E1d")
            if purpose in ("group", "split") and not (f.kind in ("picklist", "boolean", "date", "datetime")
                                                     or f.api == "RecordTypeId"):
                raise RecordError(sensitive_object_text(spec.api), "E1d")

    def _record_type_note(self, call: _Call, spec: ObjectSpec, args: dict) -> None:
        """The note that a count includes every record type of its object, unless the call filters the object's own
        record type: RecordTypeId or RecordType (``RecordTypeId.DeveloperName``; business-term contracts of
        2026-11-24: the candidate filter code added too). A filter through a lookup (``Candidate__c.RecordTypeId``)
        narrows the related record, not the counted object's record types, so the note stays. IsPersonAccount alone is
        no record type (review fixes of 2026-11-24: the employees' own person accounts have it too): the note then says
        that the flag spans record types."""
        heads = [str(f.get("field", "")).split(".")[0].lower()
                 for f in (args.get("filters") or []) + (args.get("any_of") or []) + (args.get("base_filters") or [])]
        used = any(h in ("recordtypeid", "recordtype") for h in heads)
        with_data = [r for r in spec.record_types if r.get("records")]
        if not used and len(with_data) > 1:
            listed = ", ".join(f"{r.get('developer_name') or r.get('name')} {r['records']}" for r in with_data)
            if "ispersonaccount" in heads:
                call.note(f"IsPersonAccount is no record type: the accounts it keeps can be of several record types "
                          f"(all {spec.api} records: {listed}); filter RecordTypeId to narrow")
            else:
                call.note(f"includes all record types ({listed}); filter RecordTypeId to narrow")

    def _future_note(self, call: _Call) -> None:
        per = call.applied.get("period")
        if per and per.get("to_exclusive"):
            end = dt.date.fromisoformat(per["to_exclusive"])
            if end > call.ctx.today + dt.timedelta(days=1):
                call.note(f"the period runs past today (to {(end - dt.timedelta(days=1)).isoformat()}); records dated "
                          "after today are included")
        elif per and per.get("from") and not per.get("to_exclusive"):
            call.note("the period has no end; records dated after today are included")

    def _measure(self, call: _Call, spec: ObjectSpec, scope: Scope, measure: str, mfield: Any) -> Col | None:
        if measure == "count":
            if mfield:
                call.note("measure_field is ignored for measure=count")
            return None
        if not mfield:
            raise RecordError(f"measure {measure} needs measure_field (a field of {spec.api}).", "E11c")
        ref = self.catalog.resolve_path(spec, mfield)
        self._check_use(spec, ref, "measure")
        f = ref.field
        if measure in ("sum", "avg") and not f.is_number:
            raise RecordError(self._e6(measure, spec, ref), "E6")
        if measure in ("min", "max") and not (f.is_number or f.is_date):
            raise RecordError(self._e6(measure, spec, ref), "E6")
        call.use(ref)
        return scope.col(ref)

    def _e6(self, op: str, spec: ObjectSpec, ref: PathRef) -> str:
        nums = [f.api for f in spec.fields.values() if f.is_number and f.has_data and not f.sensitivity][:10]
        return (f"{op} needs a number field; {ref.obj}.{ref.field.api} is {ref.field.kind} ({ref.field.db_type}). "
                f"Number fields of {spec.api}: {', '.join(nums) or 'none'}.")

    def _group_col(self, call: _Call, spec: ObjectSpec, scope: Scope, name: Any, purpose: str) -> Col:
        ref = self.catalog.resolve_path(spec, name)
        self._check_use(spec, ref, purpose)
        f = ref.field
        if f.is_date:
            raise RecordError(f"{ref.text} is a date; use trend_records with grain=month (or day, week, quarter, year).",
                              "E7")
        if f.kind == "longtext":
            raise RecordError(f"{ref.text} is long text and cannot be grouped.", "E7")
        if f.kind == "id":
            raise RecordError(f"{ref.text} is the record Id; every group would have one record. Group by a picklist or "
                              "lookup field.", "E7")
        call.use(ref)
        col = scope.col(ref, label=True)
        return col

    @staticmethod
    def _grouped_by_text(cat: Catalog, col: Col) -> str:
        f = col.field
        if f.kind == "reference":
            if f.api == "OwnerId":
                return f"{col.display} (owner name)"
            tgt = next((t for t in f.ref_targets if t in cat.objects and cat.objects[t].has_table), None)
            label = cat.objects[tgt].label if tgt else "related record"
            return f"{col.display} ({label} name)"
        return col.display

    def _numbering(self, call: _Call, col: Col, rows: list[list]) -> dict[tuple[str, str], int] | None:
        """L12: ``(normalised name, Id) -> k`` for target records that share a name, in Id order (one statement per
        target object), for the group labels of a lookup; None when the column is not a lookup."""
        f = col.field
        if f.kind != "reference" or not col.id_expr:
            return None
        targets = [t for t in (("User", "Group") if f.api == "OwnerId" else f.ref_targets) if self._queryable(t)]
        names = list(dict.fromkeys(norm_name(r[1]) for r in rows
                                   if r[1] is not None and str(r[1]) not in ("(blank)", "(unresolved id)", "(no name)")))
        out: dict[tuple[str, str], int] = {}
        if not names:
            return out
        n = name_norm_sql('x."Name"')
        for t in targets:
            st = call.run(Frag(f'SELECT {n} AS n, list(x."Id" ORDER BY x."Id") AS ids FROM main.{quote_ident(t)} AS x '
                               f"WHERE {n} = ANY(?) GROUP BY 1 HAVING COUNT(*) > 1", (names,)), max_rows=1000)
            for nv, ids in st.rows:
                for k, i in enumerate(ids or [], 1):
                    out[(str(nv), str(i))] = k
        return out

    def _label_groups(self, call: _Call, rows: list[list], has_gid: bool, target: str | None,
                      numbering: dict[tuple[str, str], int] | None = None) -> list[dict]:
        """Rows (gid?, grp, value) -> groups; records that share a name become 'Name (#k)', numbered in Id order (L12)."""
        groups: list[dict] = []
        seen: dict[str, list[Any]] = {}
        for r in rows:
            gid, grp, value = (r[0], r[1], r[2]) if has_gid else (None, r[0], r[1])
            grp = "(blank)" if grp is None else str(grp)
            label = grp
            if has_gid and gid is not None and grp not in ("(blank)", "(unresolved id)"):
                ids = seen.setdefault(grp, [])
                if gid not in ids:
                    ids.append(gid)
                if numbering is not None:
                    k = numbering.get((norm_name(grp), str(gid)))
                    if k:
                        label = f"{grp} (#{k})"
                else:
                    k = ids.index(gid) + 1
                    if k > 1:
                        label = f"{grp} (#{k})"
            groups.append({"group": _cell(label), "value": _round(value, 2) if isinstance(value, (float, Decimal)) else value})
        dup = [g for g, ids in seen.items() if len(ids) > 1]
        if numbering:
            shared = {nv for nv, _i in numbering}
            dup = [g for g in seen if norm_name(g) in shared]
        if dup:
            call.note(f"{len(dup)} name(s) are shared by different {target or 'related'} records; 'Name (#k)' numbers "
                      "records that share a name, in Id order")
        return groups

    def _same_target_key(self, col: Col) -> dict | None:
        """C4.2: a group_by or split_by lookup that has other lookups to the same object (no counts)."""
        cat = self.catalog
        f = col.field
        holder = cat.objects.get(col.ref.obj)
        if f.kind != "reference" or holder is None or f.api in SYSTEM_LOOKUPS or f.api == "RecordTypeId":
            return None
        sibs = [g for g in cat.same_target_lookups(holder, f) if g.filled > 0 and g.api not in SYSTEM_LOOKUPS][:6]
        t = cat.target_of(f)
        if not sibs or t is None:
            return None
        return {"field": col.display, "role": _role(f), "target": t, "target_label": cat.label_of(t),
                "others": [{"field": g.api, "role": _role(g)} for g in sibs]}

    def _lookup_col_notes(self, call: _Call, col: Col) -> None:
        """N5b and N6 for a group_by or split_by lookup."""
        cat = self.catalog
        f = col.field
        holder = cat.objects.get(col.ref.obj)
        if f.kind != "reference" or holder is None:
            return
        if cat.almost_empty(holder, f) and f.api not in SYSTEM_LOOKUPS:
            sibs = [g for g in cat.same_target_lookups(holder, f) if g.filled > 0 and g.api not in SYSTEM_LOOKUPS][:4]
            t = cat.target_of(f)
            call.note(f"{holder.api}.{f.api} is filled on only {f.filled} of {holder.rows} {holder.api} records (under 1%); "
                      f"if the question means another role, the other lookups of {holder.api} to "
                      f"{cat.label_of(t) if t else 'it'} are " + (", ".join(f"{g.api} ({_role(g)})" for g in sibs) or "none"))
        if f.unresolved > 0:
            call.note(self._n6(holder, f))

    @staticmethod
    def _split_totals(totals: dict[str, float], measure: str) -> dict[str, Any]:
        """C4.3: totals per split value over every matching record: the 10 most frequent plus '(other)'."""
        ranked = sorted(totals.items(), key=lambda t: (-t[1], t[0]))
        conv = (lambda v: int(v)) if measure == "count" else (lambda v: _round(v, 2))  # noqa: E731
        out = {k: conv(v) for k, v in ranked[:10]}
        rest = sum(v for _k, v in ranked[10:])
        if rest:
            out["(other)"] = conv(rest)
        return out
