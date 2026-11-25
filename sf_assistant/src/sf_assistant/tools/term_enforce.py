"""The business-term contracts in the record tools (2026-11-24): :class:`TermEnforceMixin` of
:class:`~sf_assistant.tools.record_tools.RecordTools`.

``begin_question`` reads the question's terms once (:meth:`TermEnforceMixin._terms_for`,
:func:`sf_assistant.engine.term_contracts.detect`); ``RecordTools.call`` runs :meth:`TermEnforceMixin._enforce_terms`
between the argument validation and the handler (the plan of :func:`~sf_assistant.engine.term_contracts.plan`: the
added candidate filter, the completed support call, or a refusal T1 / T2 / T3 the model reads as the error text;
T2 also refuses placements dated by another field than Date_of_Interview__c) and
:meth:`TermEnforceMixin._finish_terms` after it (``applied["term_filters"]``, the offer rate of a completed support
call). An added filter is an ordinary filter item: it runs through the same filter code, so the SQL, ``applied``, the
evidence, the lookup check, the chart subtitle and the merge's Counted line all state it. ``_Call.enforced`` holds the
arguments the handler ran with when the plan changed them (``QueryEvidence.enforced_arguments``); the evidence keeps
the model's own ``arguments``. A call with no question (``begin_question("")``: raw MCP tool calls, tool-level tests)
is never changed.
"""

from __future__ import annotations

import copy
from typing import Any

from sf_assistant.engine.catalog import RecordError
from sf_assistant.engine.term_contracts import (BLANK_SHARE_NOTE, NO_TERMS, PERSON_TYPE, PathInfo, Plan, Step,
                                                Terms, blank_outcomes, detect, offer_rates, plan, text)
from sf_assistant.engine.values import looks_like_id, normalize

__all__ = ["TermEnforceMixin", "JOIN_CAP_TEXT"]

#: The start of the join-cap error (engine/query_builder.py ``Scope._join``, E3): an added filter that would pass the
#: cap is left out instead, with a note.
JOIN_CAP_TEXT = "a statement can join at most"


class _CatalogView:
    """:class:`sf_assistant.engine.term_contracts.View` over the record tools' catalog. A name or path that does not
    resolve is None: the handler then raises its own error (E1, E2, E3 ...). ``date_ref``: the record tools' own reader
    of a date_field argument (``RecordTools._date_ref``), so the placement date rule reads it as the handler does."""

    def __init__(self, catalog: Any, date_ref: Any = None) -> None:
        self.cat = catalog
        self.date_ref = date_ref

    def _info(self, ref: Any) -> PathInfo:
        refs = [Step(h.src, h.field.api, None if h.target == "User|Group" else h.target, h.field.ref_role or "")
                for h in ref.hops]
        if ref.field.kind == "reference":
            refs.append(Step(ref.obj, ref.field.api, self.cat.target_of(ref.field), ref.field.ref_role or ""))
        return PathInfo(ref.text, tuple(refs), ref.field.api)

    def object(self, name: Any) -> str | None:
        try:
            return self.cat.resolve_object(name).api
        except Exception:  # noqa: BLE001 - the handler raises the error
            return None

    def path(self, obj: str, path_text: Any) -> PathInfo | None:
        spec = self.cat.objects.get(obj)
        if spec is None or not isinstance(path_text, str) or not path_text.strip():
            return None
        try:
            ref = self.cat.resolve_path(spec, path_text)
        except Exception:  # noqa: BLE001 - the handler raises the error
            return None
        return self._info(ref)

    def date_path(self, obj: str, path_text: Any) -> PathInfo | None:
        spec = self.cat.objects.get(obj)
        if spec is None or not isinstance(path_text, str) or not path_text.strip():
            return None
        try:
            ref = (self.date_ref(spec, path_text) if self.date_ref is not None
                   else self.cat.resolve_path(spec, path_text))
        except Exception:  # noqa: BLE001 - the handler raises the error (E5e, E2c)
            return None
        return self._info(ref) if ref.field.is_date else None

    def record_type(self, obj: str, value: Any) -> str | None:
        spec = self.cat.objects.get(obj)
        if spec is None or value is None:
            return None
        if looks_like_id(value):
            hit = [r for r in spec.record_types if str(r.get("id")) == str(value)]
        else:
            nv = normalize(value)
            hit = [r for r in spec.record_types
                   if r.get("id") and nv in (normalize(r.get("name")), normalize(r.get("developer_name")))]
        return str(hit[0].get("developer_name") or hit[0].get("name")) if hit else None

    def person_type(self, obj: str) -> str | None:
        spec = self.cat.objects.get(obj)
        if spec is None or "RecordTypeId" not in spec.fields:
            return None
        hit = next((r for r in spec.record_types if r.get("id") and r.get("developer_name") == PERSON_TYPE), None)
        return str(hit.get("name") or PERSON_TYPE) if hit else None

    def has_field(self, obj: str, api: str) -> bool:
        spec = self.cat.objects.get(obj)
        return bool(spec is not None and api in spec.fields)

    def record_types(self, obj: str) -> list[tuple[str, str]]:
        spec = self.cat.objects.get(obj)
        return [(str(r.get("name") or ""), str(r.get("developer_name") or ""))
                for r in (spec.record_types if spec is not None else []) if r.get("id")]


class TermEnforceMixin:
    """The business-term contracts of :class:`~sf_assistant.tools.record_tools.RecordTools`."""

    _terms: Terms = NO_TERMS

    def _terms_for(self, question: str) -> Terms:
        """The terms ``question`` names (``engine.term_contracts.detect``); the Account record type names other than
        Person Account are the record-type override words."""
        acc = self.catalog.objects.get("Account")
        names: list[str] = []
        for r in (acc.record_types if acc is not None else []):
            if r.get("developer_name") != PERSON_TYPE:
                names += [str(x) for x in (r.get("name"), r.get("developer_name")) if x and x != "(none)"]
        try:
            return detect(question, names)
        except Exception:  # noqa: BLE001 - a contract never stops a question
            return NO_TERMS

    def _enforce_terms(self, call: Any, name: str, args: dict) -> dict:
        """The arguments the handler runs with: ``args`` itself when no contract applies; a copy with the added filter
        items and set arguments otherwise (kept in ``call.enforced``). Raises :class:`RecordError` with code T1, T2 or
        T3 for a call a contract refuses. The plan's notes go first in the call's notes."""
        terms = self._terms
        if not terms.keys:
            return args
        p: Plan = plan(terms, name, args, _CatalogView(self.catalog, getattr(self, "_date_ref", None)))
        if p.reject is not None:
            code, msg = p.reject
            obj = _CatalogView(self.catalog).object(args.get("object"))
            if obj and not call.object:   # the refused call's evidence names its object, as a handler error does
                call.object = obj
            raise RecordError(msg, code)
        for n in p.notes:
            call.note(n)
        call.term_plan = p
        if not p.changes:
            return args
        new = copy.deepcopy(args)
        for key, item in p.add:
            new[key] = list(new.get(key) or []) + [dict(item)]
        new.update(p.set_args)
        call.enforced = new
        return new

    @staticmethod
    def _without_added(args: dict, p: Plan) -> dict:
        """``args`` as enforced, without the filter items the plan added (the join cap)."""
        new = copy.deepcopy(args)
        new.update(p.set_args)
        return new

    @staticmethod
    def _join_capped(exc: Exception, call: Any) -> bool:
        """The handler failed on the join cap and the plan added a filter: the call runs again without it."""
        p = getattr(call, "term_plan", None)
        return (p is not None and bool(p.add) and getattr(exc, "code", None) == "E3"
                and str(exc).startswith(JOIN_CAP_TEXT))

    def _finish_terms(self, call: Any, facts: dict) -> None:
        """After the handler: ``applied["term_filters"]``, the texts of the filters code added (the last ones of their
        ``applied`` list); a completed support call gets ``offers`` and ``offer_rate_pct`` per group and
        ``offer_rate_overall_pct`` (or a note why not), and a note on the interviews with no outcome when they are
        :data:`~sf_assistant.engine.term_contracts.BLANK_SHARE_NOTE` of the split or more (review fixes of 2026-11-24:
        recent months have few outcomes filled in, so their rate reads near 0%)."""
        p = getattr(call, "term_plan", None)
        if p is None:
            return
        added = [key for key, _item in p.add if not getattr(call, "terms_dropped", False)]
        texts: list[str] = []
        for key in dict.fromkeys(added):
            n = added.count(key)
            got = list(call.applied.get(key) or [])
            texts += got[-n:] if len(got) >= n else []
        if texts:
            call.applied["term_filters"] = texts
        if p.completed and isinstance(facts, dict):
            rate = offer_rates(facts.get("groups"), facts.get("split_totals"))
            if rate is False:
                call.note(text("support_rate_unapplied"))
                return
            facts["offer_rate_overall_pct"] = rate
            call.note(text("support_note" if p.set_args.get("split_by") else "support_rate_note"))
            blank, total = blank_outcomes(facts.get("split_totals"))
            if total and blank / total >= BLANK_SHARE_NOTE:
                call.note(text("support_blank_outcomes", BLANK=blank, TOTAL=total))
