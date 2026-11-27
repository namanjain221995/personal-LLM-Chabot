"""Salesforce conversation memory: the query state of one answer, built, checked and patched by code (design v2 of
2026-11-25, sections 5 to 10; lane S1, "state core").

The service returns a ``state`` after every answer (contract v2) and gets the parent answer's state back with the next
question. The platform stores it unread; this module is the only code that writes or reads inside it. A state holds
the plan that ran (the record tool calls in the model's own form, never ``enforced_arguments``), the business-term
keys and the person's typed words that set them, the display flags and a pending clarifying question. It holds
nothing from the answer: no rows, no counts, no answer text, no SQL, no names the results matched, no term flags
(C16) and no contact flag.

Shape (``v`` 1; jsonb may reorder the keys, so nothing here depends on key order)::

    {"v": 1, "made_at": "2026-11-25T10:02:00+05:30", "kb": "<knowledge build id>", "profile": null,
     "plan": {"question": "...", "terms": {"keys": ["candidates"], "text": "<typed questions, <= 600 chars>"},
              "calls": [{"role": "main" | "side", "tool": "group_records", "args": {...},
                         "period": {"phrase", "from", "to", "date_field"},     a call with a period (applied dates)
                         "segments": [{"label", "phrase", "from", "to"}],      compare_records, one per segment
                         "set_digest": "sha256:...",                           list_records, group_records with top
                         "keys_from": {"field", "tool", "args", "period", "digest", "rank"},   a set as a filter
                         "nonempty": true}],                                   the call returned a row (C7)
              "show": {"chart": false, "type": null, "table": false}, "age": 0},
     "pending": null | {"asked": "...", "for": "...", "choices": [{"n", "label", "question", "ops"}]},
     "last": {"kind": "records", "reason": "shortcut", "op": "set_top"}}

The parts:

- **Terms** (§7.1, C18): :func:`terms_for` rebuilds the :class:`~sf_assistant.engine.term_contracts.Terms` of a run
  from the stored keys and typed words, the typed follow-up and the model's standalone question. Keys may come from
  all three (they only restrict); the flags come from the typed words only, per contract (the newest typed question
  of the chain that names the contract or one of its flags decides them), so a relaxation never comes from the model
  and a narrowing follow-up ends it for the rest of the chain. :func:`next_terms` gives the next state's ``terms``.
- **Building** (§7.3, §8, §9): :func:`build` writes the next state from the answer kind and, for a records answer,
  the evidence (:func:`plan_from_evidence`: the successful calls of the 7 contract tools; main picked as
  ``charts._pick`` picks, a seeded main call kept as the main call, and of several counts the one whose object the
  question names alone (step-5 fix W1); a zero-row call the record agent replaced is no call (step-5 fix RR-M);
  more than 3 calls store no plan; a call of the
  record agent's with a value the person never gave, a name, a figure or a date it took from a result, is left out,
  and its compare labels are code's words; when every call is left out so, no plan). Answers that ran no query carry
  the parent plan, one age older; a clarify, an unclear reply or small talk carry it at the same age.
- **Checking** (§5, §7.4): :func:`check` reads a state that came back (bad shapes, impossible dates, an unknown
  ``v``, an access profile that differs, more than 32 KB as the platform measures it) and, with a view of the
  catalog, names the fields that no longer resolve or are blocked.
- **Patching** (§6.4, §10): :func:`apply` applies at most 3 closed operations to a copy of the plan and checks the
  result (fields, blocked fields, typed values, the 8-filter cap, the schema, the term contracts, the call cap,
  contact fields). A field name that does not resolve is first matched to the one field it names
  (:func:`_ground_field`), and a candidates scope the record agent wrote itself goes when the typed words relax the
  contract (:func:`_relax_scope`; step-5 fixes). A
  check that fails gives a :class:`Clarify` (a fixed code and the field or value it is about), never a silent run.
- **Running** (§7.4, §10): :func:`run_calls` gives the calls to send, with the stored dates kept for an unchanged
  relative period (D1); :func:`keys_filter`, :func:`set_keys` and :func:`keys_digest` turn a stored record set into an
  ``in`` filter at run time, so no record id is ever stored; :func:`stale_empty` marks an unchanged re-run that now
  finds nothing although it found rows before (C7).
- **The plan line** (C20): :func:`plan_line` describes a plan in code-written words (:data:`PLAN_LINE_WORDS`, the
  prompt file ``prompts/plan_line_words.md``): object,
  measure, grouping, filters as stored, term scope, period dates, top N and display. It holds no figure from a result;
  the only figures in it are dates, limits and the values of the plan's own arguments.

Every function is pure: no I/O, no clock (the caller passes ``today`` and ``made_at``), no module state written. A
catalog is read only through a view (:class:`PlanView` over the record tools' catalog, or any object with the same
methods). No duckdb at import.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Iterable

from sf_assistant import prompts as _prompt_files   # the plan line's words (prompts/plan_line_words.md) and fill
from sf_assistant.engine import followup_shortcuts as _shortcuts   # the period grammar of the typed words
from sf_assistant.engine.catalog import SYSTEM_LOOKUPS, RecordError
from sf_assistant.engine import term_contracts as _tc   # the candidates contract's own readers of a type filter
from sf_assistant.engine.contact import asks_for_contact
from sf_assistant.engine.periods import PeriodError, bucket_end, parse_period
from sf_assistant.engine.term_contracts import CONTRACTS, ENFORCED_TOOLS, NO_TERMS, Terms, detect
from sf_assistant.engine.term_contracts import plan as _term_plan
from sf_assistant.tools.record_tools import RecordTools
from sf_assistant.tools.registry import OPS, SCHEMAS
from sf_assistant.tools.term_enforce import _CatalogView

__all__ = ["STATE_VERSION", "MAX_STATE_BYTES", "MAX_CALLS", "MAX_AGE", "MAX_OPS", "MAX_FILTERS", "MAX_TERMS_TEXT",
           "MAX_PLAN_LINE", "MAX_SET_KEYS", "MAX_CHOICES", "ANSWER_KINDS", "OP_NAMES", "CLARIFY_CODES", "GRAINS",
           "MEASURES", "CHART_TYPES", "CONTRACT_KEYS", "TERM_FLAGS", "RELAXING_FLAGS", "CONTRACT_FLAGS",
           "PLAN_LINE_WORDS", "schema_value", "terms_for", "next_terms", "terms_text", "model_args", "nonempty",
           "set_keys", "keys_digest",
           "plan_from_evidence", "build", "state_bytes", "fit", "Checked", "check", "usable", "clean_show",
           "make_pending", "normalize_ops", "typed_value", "Clarify", "Patched", "apply", "RunCall", "run_calls",
           "keys_filter", "with_item", "stale_empty", "plan_line", "PlanView"]

# ------------------------------------------------------------------------------------------------------ the limits
STATE_VERSION = 1
#: The largest state kept, measured as the platform's store measures it (:func:`state_bytes`); a larger one is None
#: (the bridge also writes NULL above it).
MAX_STATE_BYTES = 32 * 1024
#: Data calls of one plan: main plus at most 2 side calls (D2; ``SFA_MEMORY_MAX_CALLS``).
MAX_CALLS = 3
#: Answers since a plan ran after which no patch applies (``SFA_MEMORY_MAX_AGE``).
MAX_AGE = 2
#: Patch operations in one turn.
MAX_OPS = 3
#: Filter items of one call (the record tools' schema, ``FILTERS`` maxItems).
MAX_FILTERS = 8
#: The person's typed words that set the terms (whole questions, newest kept).
MAX_TERMS_TEXT = 600
#: The plan line (the platform's CHECK on ``sf_query_state.plan_line``).
MAX_PLAN_LINE = 600
#: The standalone question of a plan and a pending choice (the page's limit on a question).
MAX_QUESTION_CHARS = 1000
#: Keys of a record set turned into an ``in`` filter (the schema's ``values`` maxItems).
MAX_SET_KEYS = 20
#: Choices of a pending clarifying question.
MAX_CHOICES = 4
#: ``group_records.top`` and ``list_records.limit`` (the schema's maxima).
MAX_TOP, MAX_LIMIT = 100, 50
#: A stored age above this is no state of ours.
_MAX_STORED_AGE = 1000

#: The answer kinds :func:`build` takes (``merged`` reads as ``records``; ``people`` as ``no_query``).
ANSWER_KINDS = ("records", "about_answer", "knowledge", "no_query", "clarify", "unclear", "small_talk", "refused")
_KIND_ALIASES = {"merged": "records", "people": "no_query", "about_people": "no_query", "search": "no_query",
                 "history": "no_query", "sync": "no_query", "not_found": "no_query"}
#: The patch operations (§6.4).
OP_NAMES = ("set_period", "add_filter", "remove_filter", "set_top", "set_show", "set_group", "set_grain",
            "set_measure", "drill", "compare_period", "follow_set", "new_question")
#: Why a patch asks instead of running (:class:`Clarify` ``code``): the renderer (lane S2/S3) writes the words.
CLARIFY_CODES = (
    "bad_ops",             # not a list of operations of the grammar
    "too_many_ops",        # more than MAX_OPS operations
    "no_plan",             # an operation needs a plan and there is none
    "no_date_field",       # set_period, set_grain or compare_period on calls with no date field
    "no_period",           # set_grain or compare_period on a call with a date field but no period
    "which_side",          # set_period on a compare plan
    "bad_period",          # the period words do not parse (``detail``: the error code)
    "same_period",         # compare_period with the period the call already has
    "unknown_field",       # a field that does not resolve on the main call's object (``field``)
    "unknown_object",      # follow_set to an object the catalog does not know (``field``: the object)
    "value_not_typed",     # a filter or drill value the person did not type (``field``, ``value``)
    "too_many_filters",    # more than MAX_FILTERS filter items on a call
    "no_such_filter",      # remove_filter of a filter the plan does not hold (``field``)
    "top_of_what",         # set_top on a call with no ranking (count, aggregate, trend, compare, percentage)
    "top_too_large",       # set_top past the schema's maximum (``value``)
    "list_bottom",         # set_top bottom N on a list: the list's own order decides its rows (``value``: N)
    "group_not_for",       # set_group on a call it cannot group
    "grain_not_for",       # set_grain on a call it cannot turn into a trend
    "grain_two_splits",    # set_grain on a group with a split: a trend has one split
    "grain_top_too_large",  # set_grain on a top N with N above MAX_SET_KEYS
    "measure_not_for",     # set_measure on a call with no measure (list, percentage) or a measure it cannot take
    "measure_field",       # set_measure other than count with no field
    "number_field",        # sum or avg of a field that is not a number (``field``)
    "drill_not_group",     # drill on a call that is not a grouping
    "rank_not_shown",      # drill by rank past the ranking shown, or on a grouping with no top N
    "compare_not_for",     # compare_period on a call it cannot compare (``detail``: why)
    "call_cap",            # the patched plan would need more than MAX_CALLS calls
    "which_call",          # an operation on the main call of a plan whose answer ran several counts, and the main
    #                        call is not the one its question names (step-5 fix W1): which query is meant
    "no_link",             # follow_set: no lookup from the object to the set's records
    "which_link",          # follow_set: several lookups (``choices``)
    "set_not_supported",   # follow_set on a set that cannot become a filter (``detail``: why)
    "set_too_large",       # follow_set on a ranking or list of more than MAX_SET_KEYS
    "term_reject",         # a business-term contract refuses a call (``value``: T1/T2/T3, ``detail``: its text)
    "bad_call",            # the record tools' argument check refuses a patched call (``detail``: its code)
)
GRAINS = ("day", "week", "month", "quarter", "year")
MEASURES = ("count", "count_distinct", "sum", "avg", "min", "max")
#: The chart types a display flag may name (agent/charts.py ``_TYPE_WORDS``, without "unsupported").
CHART_TYPES = ("bar", "horizontal_bar", "line", "pie", "donut")
CONTRACT_KEYS = tuple(c.key for c in CONTRACTS)
#: The boolean fields of Terms (term_contracts.py:193-208); never stored, re-derived on every use.
TERM_FLAGS = tuple(f.name for f in dataclasses.fields(Terms) if f.name not in ("question", "keys"))
#: The flags that widen what a contract allows (performance completes the support call; it widens nothing).
RELAXING_FLAGS = tuple(f for f in TERM_FLAGS if f != "performance")
#: The relaxing flags of each contract (the rule of term_contracts.plan that reads them): a typed question that names
#: the contract (its words) or one of these flags decides them, so naming one contract never resets another's
#: (stage-1 review fixes H1, M1 and F1).
CONTRACT_FLAGS: dict[str, tuple[str, ...]] = {
    "candidates": ("other_accounts", "all_types", "leads", "no_candidate"),
    "placement": ("placed_status", "onboarding"),
    "support_performance": ("sessions", "preferred", "mock"),
}
_NEW_KINDS = frozenset({"new", "new_question"})
#: The tools a chart can draw from as they ran (agent/charts.py ``CHART_ARG_TOOLS``; a count only with a breakdown).
_CHARTABLE = ("group_records", "trend_records", "compare_records")
_NUMBER_KINDS = frozenset({"integer", "number", "percent", "currency"})
_DATE_KINDS = frozenset({"date", "datetime"})
_SPECIAL_GROUPS = ("(blank)", "(unresolved id)")
#: The label a grouping gives records that share a name ("Name (#2)", RecordTools._label_groups): no filter by name
#: can tell them apart, so such keys never become a filter.
_NAMESAKE_RE = re.compile(r" \(#\d+\)$")
#: A Salesforce record Id as a filter value: 15 or 18 letters and digits, at least 5 of them digits (stricter than
#: ``engine.values.looks_like_id``, which a 15-letter picklist value would pass).
_RECORD_ID_RE = re.compile(r"(?=(?:\D*\d){5})[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
#: A date or date-time filter value (a period's bound the agent wrote): a date may stand in a plan (§16.1).
_DATE_VALUE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?",
                            re.ASCII)
#: Period words that name a point in time of their own (a year, a date, a month, a quarter), so they could come from a
#: result (a date of birth, the busiest month); words without them ("last 12 months", "this year", "pichle mahine")
#: are relative to today and carry nothing a result gave (stage-2 review fix PE2).
_ABSOLUTE_PERIOD_RE = re.compile(r"\d{4}|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\bq[1-4]\b",
                                 re.IGNORECASE)
#: A group's ``min_value`` this small carries no figure of a result (the schema's own "2 for duplicates").
_PLAIN_MIN_VALUES = (0, 1, 2)
_REASON_RE = re.compile(r"[a-z0-9_]{0,40}")
_TOKEN_RE = re.compile(r"[^\W_]+")
_NUMBER_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")


def _canon(value: Any) -> str:
    """``value`` as compact JSON with sorted keys: equal for equal data whatever the key order (jsonb reorders)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _cut(text: Any, limit: int) -> str:
    """``text`` as one line of at most ``limit`` characters, cut at a word boundary when it is longer."""
    s = " ".join(str(text or "").split())
    if len(s) <= limit:
        return s
    head = s[:limit]
    return head.rsplit(" ", 1)[0] if " " in head else head


def _items(value: Any) -> list[dict]:
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def _filter_key(tool: str) -> str:
    """Where a call's AND filters live (term_contracts ``_filter_key``): the base of a percentage, else ``filters``."""
    return "base_filters" if tool == "percentage_of_records" else "filters"


# ------------------------------------------------------------------------------------------------------- the terms
def _stored_terms(stored: Any) -> tuple[frozenset[str], str]:
    """(keys, typed text) of a stored ``terms`` entry; unknown keys and other shapes give nothing."""
    if not isinstance(stored, dict):
        return frozenset(), ""
    keys = stored.get("keys")
    keys = frozenset(k for k in keys if isinstance(k, str) and k in CONTRACT_KEYS) if isinstance(keys, list) else \
        frozenset()
    text = stored.get("text") if isinstance(stored.get("text"), str) else ""
    return keys, text


def terms_for(stored: Any, typed: Any, standalone: Any = "", kind: str = "patch",
              other_record_types: Iterable[str] = ()) -> Terms:
    """The terms a run uses (§7.1, C18), from the stored ``terms`` (``{"keys", "text"}``), the person's typed
    question and the model's ``standalone`` question; ``other_record_types`` as for ``term_contracts.detect``.

    - ``kind`` "new" (or "new_question"): keys of the typed and the standalone question; flags of the typed one;
      ``question`` is the typed question.
    - otherwise: keys of the stored terms, the typed and the standalone question; ``question`` is the stored words and
      the typed question, one per line (``_placement`` reads it: "active or placed candidates" keeps its status list).
      The relaxing flags are read per contract (:data:`CONTRACT_FLAGS`): the newest typed question of the chain that
      names that contract or one of its flags (the typed follow-up first, then the stored questions from the newest
      line back) decides that contract's flags, and a contract no question speaks to has none. So "all account
      types", then "only candidates", then "visualize it" stays scoped to candidates (the narrowing is the newest
      question that names the candidates contract), and "only candidates who joined this year" keeps the status flag
      an earlier "status placed" set (it names no placement word). ``performance`` widens nothing (it completes the
      support call), so any typed question of the chain sets it.

    The standalone question never sets a flag: only the person's own words widen a contract."""
    typed = typed if isinstance(typed, str) else ""
    standalone = standalone if isinstance(standalone, str) else ""
    names = tuple(other_record_types or ())
    t_typed = detect(typed, names)
    t_std = detect(standalone, names)
    if kind in _NEW_KINDS:
        return dataclasses.replace(t_typed, question=typed, keys=frozenset(t_typed.keys | t_std.keys))
    keys, text = _stored_terms(stored)
    chain = [t_typed] + [detect(line, names) for line in reversed(text.split("\n")) if line.strip()]
    question = "\n".join(x for x in (text, typed) if x.strip())
    return dataclasses.replace(NO_TERMS, question=question, keys=frozenset(keys | t_typed.keys | t_std.keys),
                               **_chain_flags(chain))


def _chain_flags(chain: list[Terms]) -> dict[str, bool]:
    """The flags of a chain of typed questions, newest first (see :func:`terms_for`)."""
    out = {f: False for f in TERM_FLAGS}
    for key, flags in CONTRACT_FLAGS.items():
        for t in chain:
            if key in t.keys or any(getattr(t, f) for f in flags):
                out.update({f: bool(getattr(t, f)) for f in flags})
                break
    out["performance"] = any(t.performance for t in chain)
    return out


def terms_text(previous: Any, typed: Any) -> str:
    """The next ``terms.text``: the earlier typed questions and ``typed``, one per line, whole questions, the newest
    kept within :data:`MAX_TERMS_TEXT` characters (a single longer question is cut at a word boundary)."""
    lines = [" ".join(q.split()) for q in str(previous or "").split("\n")] + [" ".join(str(typed or "").split())]
    lines = [q for q in lines if q]
    kept: list[str] = []
    size = 0
    for q in reversed(lines):
        add = len(q) + (1 if kept else 0)
        if size + add > MAX_TERMS_TEXT:
            if not kept:
                kept.append(_cut(q, MAX_TERMS_TEXT))
            break
        kept.append(q)
        size += add
    return "\n".join(reversed(kept))


def next_terms(stored: Any, typed: Any, terms: Terms, kind: str = "patch") -> dict:
    """The next state's ``terms``: the keys of the terms the run used (``terms``, from :func:`terms_for`) and the
    typed words since the chain's last new question (a new question starts them again)."""
    _keys, text = _stored_terms(stored)
    return {"keys": sorted(k for k in terms.keys if k in CONTRACT_KEYS),
            "text": terms_text("" if kind in _NEW_KINDS else text, typed)}


def _clean_terms(value: Any) -> dict:
    keys, text = _stored_terms(value)
    return {"keys": sorted(keys), "text": text[:MAX_TERMS_TEXT]}


# ------------------------------------------------------------------------------------------- the arguments of a call
class _Validator:
    """The record tools' argument check (``RecordTools._validate`` and the helpers it calls, which read only the tool
    schema), borrowed so a stored call holds exactly the arguments the tools validate to (§5 ``args``)."""

    _validate = RecordTools._validate
    _coerce = RecordTools._coerce
    _filters_shape = staticmethod(RecordTools._filters_shape)
    _filter_item = staticmethod(RecordTools._filter_item)


def _check_args(tool: str, args: Any) -> tuple[dict | None, str | None]:
    """(validated arguments, None), or (None, the error code) when the record tools would refuse them."""
    if tool not in SCHEMAS:
        return None, "E11"
    schema = SCHEMAS[tool][1]
    try:
        out, _notes = _Validator()._validate(SimpleNamespace(name=tool, input_schema=schema), copy.deepcopy(args))
    except RecordError as exc:
        return None, getattr(exc, "code", None) or "E11"
    except Exception:  # noqa: BLE001 - a shape the check cannot read is refused
        return None, "E11"
    return out, None


def model_args(tool: str, arguments: Any, validate: Callable[[str, dict], dict] | None = None) -> dict | None:
    """The model's arguments of a call as the record tools validate them (never the enforced form); None when they do
    not validate. ``validate``: the caller's own check (``(tool, args) -> args``) in place of the borrowed one."""
    if not isinstance(arguments, dict):
        return None
    if validate is not None:
        try:
            out = validate(tool, copy.deepcopy(arguments))
        except Exception:  # noqa: BLE001 - a call that does not validate is no plan call
            return None
        return copy.deepcopy(out) if isinstance(out, dict) else None
    out, err = _check_args(tool, arguments)
    return out if err is None else None


# --------------------------------------------------------------------------------------------- what a call returned
def nonempty(tool: str, result: Any) -> bool:
    """The call returned at least one row (C7): a count above 0, a group, a bucket or segment with a value, a base of
    a share, a listed record. False for a shape it cannot read (so no "no longer matches" line is ever wrong)."""
    r = result if isinstance(result, dict) else {}

    def pos(v: Any) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0

    if tool == "count_records":
        return pos(r.get("count"))
    if tool == "aggregate_records":
        return pos(r.get("records_matched")) or pos(r.get("records_with_value"))
    if tool == "group_records":
        return bool(_items(r.get("groups")))
    if tool == "trend_records":
        return pos(r.get("total")) or any(pos(b.get("value")) or pos(b.get("base")) for b in _items(r.get("buckets")))
    if tool == "compare_records":
        return any(pos(s.get("value")) for s in _items(r.get("segments")))
    if tool == "percentage_of_records":
        return pos(r.get("base"))
    if tool == "list_records":
        return pos(r.get("total_count")) or bool(_items(r.get("rows")))
    return False


def set_keys(evidence: Any) -> list[str]:
    """The ordered keys of the records a call showed (§10): the record Ids of a ``list_records`` call (the first
    column of its main statement, in the order shown), the group labels of a ``group_records`` call (blank and
    unresolved groups left out); [] for any other call. The keys live only for the run that uses them."""
    tool = getattr(evidence, "tool", "")
    res = getattr(evidence, "model_result", None)
    res = res if isinstance(res, dict) else {}
    if tool == "list_records":
        sts = list(getattr(evidence, "statements", None) or [])
        returned = res.get("returned") if isinstance(res.get("returned"), int) else None
        if sts and list(getattr(sts[0], "columns", []) or [])[:1] == ["Id"]:
            rows = list(getattr(sts[0], "rows", []) or [])
            rows = rows[:returned] if returned is not None else rows
            return [str(r[0]) for r in rows if isinstance(r, (list, tuple)) and r and r[0] is not None]
        return []
    if tool == "group_records":
        return [str(g.get("group")) for g in _items(res.get("groups"))
                if g.get("group") is not None and str(g.get("group")) not in _SPECIAL_GROUPS]
    return []


def keys_digest(keys: Iterable[Any]) -> str:
    """``sha256:<hex>`` of the ordered keys (one per line)."""
    joined = "\n".join(str(k) for k in keys)
    return "sha256:" + hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _digest_of(evidence: Any, args: dict) -> str | None:
    """The set digest of a call (§5): only for list_records, and for group_records with top."""
    tool = getattr(evidence, "tool", "")
    if tool == "list_records" or (tool == "group_records" and args.get("top")):
        return keys_digest(set_keys(evidence))
    return None


# -------------------------------------------------------------------------------------------------- building a plan
def _period_info(applied: Any, phrase: Any) -> dict | None:
    """The §5 ``period`` of a call from its ``applied.period`` (the words and the dates code read them as)."""
    pa = applied.get("period") if isinstance(applied, dict) else None
    if not isinstance(pa, dict) or not (pa.get("from") or pa.get("to")):
        return None
    words = phrase if isinstance(phrase, str) and phrase.strip() else pa.get("phrase")
    return {"phrase": str(words or ""), "from": pa.get("from"), "to": pa.get("to"),
            "date_field": pa.get("date_field") if isinstance(pa.get("date_field"), str) else None}


def _segment_info(args: dict, result: Any) -> list[dict] | None:
    """The §5 ``segments`` of a compare call: each segment's label and, when it has a period, its words and dates."""
    segs = _items(args.get("segments"))
    shown = _items(result.get("segments")) if isinstance(result, dict) else []
    if not segs:
        return None
    out = []
    for i, seg in enumerate(segs):
        item: dict[str, Any] = {"label": str(seg.get("label") or "")}
        applied = shown[i].get("applied") if i < len(shown) else None
        info = _period_info(applied, seg.get("period"))
        if info is not None:
            item.update({"phrase": info["phrase"], "from": info["from"], "to": info["to"]})
        out.append(item)
    return out


def _without_keys(arguments: Any, keys_from: dict | None, tool: str) -> Any:
    """A call's arguments without the filter a record set added at run time (the last ``in`` / ``eq`` item on the
    set's field), so a seeded call is matched with its stored form."""
    if not keys_from or not isinstance(arguments, dict):
        return arguments
    key = _filter_key(tool)
    items = list(arguments.get(key) or []) if isinstance(arguments.get(key), list) else []
    for i in range(len(items) - 1, -1, -1):
        it = items[i]
        if isinstance(it, dict) and it.get("field") == keys_from.get("field") and it.get("op") in ("in", "eq"):
            out = dict(arguments)
            rest = items[:i] + items[i + 1:]
            if rest:
                out[key] = rest
            else:
                out.pop(key, None)
            return out
    return arguments


def _match_seeds(calls: list[Any], seeds: Iterable[Any]) -> list[tuple[Any, Any]]:
    """(run call or None, evidence) of the plan calls of an answer: each seeded call (a :class:`RunCall`) takes the
    first evidence entry it sent; the fresh re-run of a record set before it (``set_args``) is left out, since it only
    gave the keys; every other successful call is the record agent's own."""
    used = [False] * len(calls)
    assigned: dict[int, Any] = {}

    def first(tool: str, want: str, strip: dict | None = None) -> int | None:
        for j, e in enumerate(calls):
            if used[j] or getattr(e, "tool", "") != tool:
                continue
            if _canon(_without_keys(getattr(e, "arguments", None), strip, tool)) == want:
                return j
        return None

    seeds = list(seeds or ())
    for rc in seeds:
        kf = getattr(rc, "keys_from", None)
        if kf and getattr(rc, "set_args", None) is not None:
            j = first(kf.get("tool", ""), _canon(rc.set_args))
            if j is not None:
                used[j] = True
        j = first(rc.tool, _canon(rc.args), kf)
        if j is not None:
            used[j] = True
            assigned[j] = rc
    for rc in seeds:   # the record agent sent a seeded call again (with the set's keys it saw): it is that call
        while True:
            j = first(rc.tool, _canon(rc.args), getattr(rc, "keys_from", None))
            if j is None:
                break
            used[j] = True
            assigned[j] = rc
    return [(assigned.get(j), e) for j, e in enumerate(calls) if j in assigned or not used[j]]


def _value_items(args: dict) -> list[tuple[Any, Any, Any]]:
    """(object, field, value) of every filter value of a call: its own filters (the base and the parts of a
    percentage, ``any_of``, each compare segment's) and each related child's filters on the child object."""
    out: list[tuple[Any, Any, Any]] = []

    def take(obj: Any, it: dict) -> None:
        vals = it.get("values") if isinstance(it.get("values"), list) else ([it["value"]] if "value" in it else [])
        out.extend((obj, it.get("field"), v) for v in vals)

    for key in ("filters", "base_filters", "any_of", "part_filters"):
        for it in _items(args.get(key)):
            take(args.get("object"), it)
    for seg in _items(args.get("segments")):
        for it in _items(seg.get("filters")):
            take(args.get("object"), it)
    for r in _items(args.get("related")):
        for it in _items(r.get("filters")):
            take(r.get("child_object"), it)
    return out


def _view_object(view: Any, name: Any) -> str | None:
    if view is None:
        return None
    try:
        return view.object(name)
    except Exception:  # noqa: BLE001 - not resolved
        return None


def schema_value(view: Any, obj: Any, path: Any, value: Any) -> bool:
    """``value`` is schema data of the field ``path`` on ``obj``, not record data: one of the field's picklist values
    or labels (case, spacing and punctuation aside), or the name, developer name or Id of a record type of the object
    a record-type path points to. Needs a view with ``picklist_values`` (:class:`PlanView`) or ``record_type``; False
    without one, for any other field and for anything but a string."""
    if view is None or not isinstance(value, str) or not value.strip():
        return False
    name = _view_object(view, obj)
    if name is None or not isinstance(path, str):
        return False
    reader = getattr(view, "picklist_values", None)
    if reader is not None:
        try:
            listed = list(reader(name, path) or ())
        except Exception:  # noqa: BLE001 - no picklist read
            listed = []
        want = _tokens(value)
        if want and any(_tokens(v) == want for v in listed):
            return True
    parts = path.split(".")
    low = [p.lower() for p in parts]
    for i, p in enumerate(low):
        if p not in ("recordtypeid", "recordtype"):
            continue
        if i < len(low) - 2 or (i == len(low) - 2 and low[-1] not in ("name", "developername")):
            return False
        owner = name
        if i:
            target = getattr(view, "target", None)
            try:
                owner = target(name, ".".join(parts[:i])) if target is not None else None
            except Exception:  # noqa: BLE001
                owner = None
        try:
            return owner is not None and view.record_type(owner, value) is not None
        except Exception:  # noqa: BLE001
            return False
    return False


def _typed_ranges(typed: str, today: dt.date) -> list[tuple[str | None, str | None]]:
    """The (first day, last day) of every period the typed words name, read on ``today`` with the period grammar of
    the shortcuts (``engine.followup_shortcuts``; several per question, Hinglish included); (None, None) for words that
    mean all time. What the record agent may write as a date or a period without the person typing it as such."""
    out: list[tuple[str | None, str | None]] = []
    for line in str(typed or "").splitlines():
        try:
            ws = _shortcuts.words(line)
            used = [False] * len(ws)
            for _round in range(6):
                phrase = _shortcuts._period(ws, used, today)
                if phrase is None:
                    break
                got = (None, None) if phrase == _shortcuts.ALL_TIME else _dates(phrase, today)
                if got is not None and got not in out:
                    out.append(got)
        except Exception:  # noqa: BLE001 - no period read on this line
            continue
    return out


def _boundaries(ranges: Iterable[tuple[str | None, str | None]]) -> frozenset[str]:
    """The days a filter bound of a typed period can be: its first and last day, the day before the first (``gt``)
    and the day after the last (``lt``)."""
    out: set[str] = set()
    for first, last in ranges:
        for day, step in ((first, -1), (last, 1)):
            if not day:
                continue
            out.add(day)
            try:
                out.add((dt.date.fromisoformat(day) + dt.timedelta(days=step)).isoformat())
            except ValueError:
                pass
    return frozenset(out)


class _TypedPeriods:
    """The periods the typed words name (:func:`_typed_ranges`) and their days (:func:`_boundaries`), read once and
    only when a date or a period needs them (reading long turns takes a while)."""

    def __init__(self, typed: str, today: dt.date) -> None:
        self.typed, self.today = typed, today
        self._ranges: list | None = None
        self._bounds: frozenset[str] | None = None

    @property
    def ranges(self) -> list:
        if self._ranges is None:
            self._ranges = _typed_ranges(self.typed, self.today)
        return self._ranges

    @property
    def bounds(self) -> frozenset[str]:
        if self._bounds is None:
            self._bounds = _boundaries(self.ranges)
        return self._bounds


def _untyped_period(phrase: Any, have: str, periods: _TypedPeriods, today: dt.date) -> bool:
    """A call's period words that the person never gave (stage-2 review fix PE2): words that are not typed, whose
    dates are not those of a period the typed words name and do not start and end on its days (a date of birth the
    agent read in a result, written as "1990-01-01 to 2026-10-13", is such a period). Words relative to today with no
    year, date, month or quarter of their own (:data:`_ABSOLUTE_PERIOD_RE`: "last 12 months", the chart note's default)
    and words that mean all time are kept."""
    if not isinstance(phrase, str) or not phrase.strip() or typed_value(phrase, have):
        return False
    if not _ABSOLUTE_PERIOD_RE.search(phrase):
        return False
    try:
        if _shortcuts.period_phrase(phrase, today=today) == _shortcuts.ALL_TIME:
            return False
        got = _dates(phrase, today)
    except Exception:  # noqa: BLE001 - words that do not read as a period: never typed as one
        got = None
    if got is None:
        return True
    if got in periods.ranges:
        return False
    first, last = got
    bounds = periods.bounds
    return not ((first is not None or last is not None) and (first is None or first in bounds)
                and (last is None or last in bounds))


def _untyped_value(args: dict, typed: str, view: Any, today: dt.date | None = None,
                   periods: _TypedPeriods | None = None) -> bool:
    """The record agent's call holds a value the person never gave (stage-1 review fixes F2 and M11; design §2.4,
    §10, C20, D14): a filter value that is not typed (``typed``: the chain's typed questions), not true or false, not
    a date, and not schema data of its field (:func:`schema_value`: a picklist value, a record type); a record Id the
    person did not type; or a group's ``min_value`` that is neither typed nor 0, 1 or 2 (the schema's "2 for
    duplicates"). Such a call is not kept: a state holds no name, figure or record Id the agent took from a result, so
    none reaches ``plan_line``, the resolver's prompt or the Salesforce summary.

    ``today`` (the business date; stage-2 review fix PE2): a date is then kept only when it is typed or is a day of a
    period the typed words name (:func:`_boundaries`), and the call's period words and each compare segment's are
    screened the same way (:func:`_untyped_period`), so a date the agent read in a result (a date of birth) is not
    kept either. Without ``today`` every date and period is kept, as in stage 1. ``periods``: the typed periods,
    shared by the calls of one answer (read here when not given)."""
    have = str(typed or "")
    if today is not None and periods is None:
        periods = _TypedPeriods(have, today)
    if today is not None and any(_untyped_period(p, have, periods, today) for p in (
            [args.get("period")] + [s.get("period") for s in _items(args.get("segments"))])):
        return True
    for obj, field_path, v in _value_items(args):
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            if typed_value(v, have):
                continue
            return True
        s = str(v).strip()
        if _RECORD_ID_RE.fullmatch(s):
            if s.casefold() in have.casefold() or schema_value(view, obj, field_path, s):
                continue
            return True
        if _DATE_VALUE_RE.fullmatch(s) and periods is not None:
            if typed_value(s, have) or s[:10] in periods.bounds:
                continue
            return True
        if typed_value(s, have) or _DATE_VALUE_RE.fullmatch(s) or schema_value(view, obj, field_path, s):
            continue
        return True
    mv = args.get("min_value")
    if mv is not None and not isinstance(mv, bool) and mv not in _PLAIN_MIN_VALUES and not typed_value(mv, have):
        return True
    return False


def _code_labels(args: dict, typed: str) -> dict:
    """A compare call of the record agent's with its segment labels in code's words: a label the person typed stays;
    any other becomes the segment's period words, or "part N" (the model writes the labels, and one could carry a
    name or a figure of a result; stage-1 review fix F2)."""
    if not _items(args.get("segments")):
        return args
    out = copy.deepcopy(args)
    used: set[str] = set()
    for i, seg in enumerate(_items(out.get("segments"))):
        label = str(seg.get("label") or "")
        if not (label.strip() and typed_value(label, typed)):
            per = seg.get("period")
            label = _cut(per, 80) if isinstance(per, str) and per.strip() else ""
            if not label or label.casefold() in used:
                label = _w("segment", N=i + 1)
            seg["label"] = label
        used.add(label.casefold())
    return out


def _entry(e: Any, seed: Any, validate: Callable | None, typed: str = "") -> dict | None:
    """One plan call from its evidence (and the seeded call it ran from, when it was one)."""
    tool = getattr(e, "tool", "")
    if seed is not None:
        args = copy.deepcopy(seed.entry.get("args") or {})
        keys_from = copy.deepcopy(seed.entry.get("keys_from")) if seed.entry.get("keys_from") else None
    else:
        args = model_args(tool, getattr(e, "arguments", None), validate)
        keys_from = None
        if args is None:
            return None
        if tool == "compare_records":
            args = _code_labels(args, typed)
    applied = getattr(e, "applied", None) if isinstance(getattr(e, "applied", None), dict) else {}
    result = getattr(e, "model_result", None)
    out: dict[str, Any] = {"role": "side", "tool": tool, "args": args}
    info = _period_info(applied, args.get("period"))
    if info is not None:
        out["period"] = info
        if seed is None and info["date_field"] and isinstance(args.get("date_field"), str):
            args["date_field"] = info["date_field"]   # the same field, as code resolved it (a temporal key read)
    if tool == "compare_records":
        segs = _segment_info(args, result)
        if segs is not None:
            out["segments"] = segs
    digest = _digest_of(e, args)
    if digest is not None:
        out["set_digest"] = digest
    if keys_from:
        out["keys_from"] = keys_from
    out["nonempty"] = nonempty(tool, result)
    return out


def _filter_fields(args: dict, tool: str) -> frozenset[str]:
    """The fields of a call's AND filters and any-of items, case aside."""
    return frozenset(str(it.get("field") or "").casefold() for key in (_filter_key(tool), "any_of")
                     for it in _items(args.get(key)))


def _dead_end(item: tuple[dict, Any, Any], later: list[tuple[dict, Any, Any]]) -> bool:
    """A call of the record agent's that found no row while it looked for the records (step-5 fix RR-M): a later call
    of its own with the same tool, object and period found rows with the same filter values on other fields (a list
    filtered on a team by the department's name, then on the department). It was a dead end, not a part of the
    answer, so it is no side call (a later patch would re-run it). A seeded call, a call with no filter value, and a
    call whose later twin filters other values (a count for each of two typed regions, a count of a region and one of
    a status) stay."""
    entry, _e, seed = item
    if seed is not None or entry.get("nonempty"):
        return False
    args, tool = entry["args"], entry["tool"]
    keep = {k: args.get(k) for k in ("object", "date_field", "period")}
    fields, values = _filter_fields(args, tool), _filter_values(args)
    return bool(values) and any(
        s is None and c.get("nonempty") and c["tool"] == tool and {k: c["args"].get(k) for k in keep} == keep
        and _filter_fields(c["args"], tool) != fields and values <= _filter_values(c["args"])
        for c, _x, s in later)


#: What makes two lists list the same records (their columns, order and size aside).
_LIST_SET = ("object", "filters", "any_of", "related", "date_field", "period")


def _same_list_later(item: tuple[dict, Any, Any], later: list[tuple[dict, Any, Any]]) -> bool:
    """A list of the record agent's that a later list of its own repeats for the same records (the same object,
    filters, record conditions and period; other columns, order or size): the agent was still looking, and the later
    list is the one it showed (step-5 fix RR-M: a first page beside the sorted list made a plan line of two lists of
    the same records)."""
    entry, _e, seed = item
    if seed is not None or entry["tool"] != "list_records":
        return False
    key = _canon({k: entry["args"].get(k) for k in _LIST_SET})
    return any(s is None and c["tool"] == "list_records" and _canon({k: c["args"].get(k) for k in _LIST_SET}) == key
               for c, _x, s in later)


def _filter_values(args: dict) -> frozenset[str]:
    """The filter values of a call in words (:func:`_value_items`; true and false left out)."""
    return frozenset(" ".join(_tokens(v)) for _o, _f, v in _value_items(args) if not isinstance(v, bool))


def _stem(word: str) -> str:
    """A word without a plural ending ("interviews" -> "interview", "entries" -> "entry")."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _stems(text: Any) -> frozenset[str]:
    return frozenset(_stem(w) for w in _tokens(text))


def _named_calls(calls: list[dict], question: Any, label: Callable | None) -> list[int]:
    """The indexes of ``calls`` whose object ``question`` names: every word of the object's label (or of its API name
    in words) is a word of the question, plurals aside. "how many interviews this month" names Interview__c
    ("Interviews") and not Internal_Interview__c ("Internal Interviews")."""
    have = _stems(question)
    if not have:
        return []
    out = []
    for i, c in enumerate(calls):
        obj = (c.get("args") or {}).get("object")
        names = [n for n in (_label(label, obj), _pretty(obj)) if n]
        if any(_stems(n) and _stems(n) <= have for n in names):
            out.append(i)
    return out


def _chartable_result(e: Any) -> bool:
    """A result a chart is drawn from (agent/charts.py ``_pick``): group, trend, compare, a count with a breakdown."""
    tool = getattr(e, "tool", "")
    res = getattr(e, "model_result", None)
    return tool in _CHARTABLE or (tool == "count_records" and isinstance(res, dict) and bool(res.get("breakdown")))


def plan_from_evidence(evidence: Iterable[Any], *, question: str = "", terms: Any = None, show: Any = None,
                       seeds: Iterable[Any] | None = None, max_calls: int = MAX_CALLS,
                       validate: Callable[[str, dict], dict] | None = None,
                       label: Callable | None = None, view: Any = None, today: dt.date | None = None,
                       typed_also: str = "") -> tuple[dict | None, str]:
    """(plan, why) from one answer's evidence (§7.3): the successful calls of the 7 contract tools, seeded or added
    by the record agent, in call order, an identical call once (its last run). ``why`` is "" with a plan, "no_calls"
    when none ran, "screened" when calls ran but the value screen left every one out (stage-2 review fix M3: no plan,
    and never the plan of an earlier answer) and "too_many" past ``max_calls`` (D2: no plan). The main call is the
    last one a chart is drawn from, else the last call; a seeded main call stays the main call unless the record agent
    added a call a chart is drawn from (stage-2 review fix M2: an operation on a plan never swaps its main and side
    calls). ``seeds``: the :class:`RunCall` list the answer started from (their stored form and record sets are kept;
    the fresh re-run of a set, and the agent sending a seeded call again, are no calls of their own). A call of the
    agent's that holds a value the person never gave (:func:`_untyped_value`: not typed in ``terms["text"]`` or
    ``typed_also``, no date, no schema value of its field) is left out, and its compare labels are code's words.
    ``view``: the catalog view (:class:`PlanView`) that knows the picklist values and record types; without one only
    typed values, dates and true/false are kept. ``today``: the business date; with it a date or a period is kept
    only when typed or a day of a typed period (stage-2 review fix PE2). ``typed_also``: more of the person's own
    typed words that count for the screen only (the user turns the bridge sent; stage-2 review fix M3), never stored.
    ``question``: the standalone question, or empty for the plan line."""
    calls = [e for e in evidence or () if getattr(e, "ok", False) and getattr(e, "kind", "") == "data"
             and getattr(e, "tool", "") in ENFORCED_TOOLS]
    typed = _clean_terms(terms)["text"]
    have = "\n".join(t for t in (typed, str(typed_also or "")) if t)
    built: list[tuple[dict, Any, Any]] = []
    screened = False
    periods = _TypedPeriods(have, today) if today is not None else None
    for seed, e in _match_seeds(calls, seeds or ()):
        entry = _entry(e, seed, validate, have)
        if entry is None:
            continue
        if seed is None and _untyped_value(entry["args"], have, view, today, periods):
            screened = True
            continue
        built.append((entry, e, seed))
    last: dict[str, int] = {}
    for i, (entry, _e, _s) in enumerate(built):
        last[_canon([entry["tool"], entry["args"], entry.get("keys_from")])] = i
    built = [built[i] for i in sorted(last.values())]
    built = [b for i, b in enumerate(built) if not (_dead_end(b, built[i + 1:]) or _same_list_later(b, built[i + 1:]))]
    if not built:
        return None, "screened" if screened else "no_calls"
    if len(built) > max_calls:
        return None, "too_many"
    main = max((i for i, (_c, e, _s) in enumerate(built) if _chartable_result(e)), default=len(built) - 1)
    seeded_main = next((i for i, (_c, _e, s) in enumerate(built) if s is not None
                        and getattr(s, "role", "") == "main"), None)
    if seeded_main is not None and not any(s is None and _chartable_result(e) for _c, e, s in built):
        main = seeded_main
    elif seeded_main is None and not any(_chartable_result(e) for _c, e, _s in built):
        named = _named_calls([c for c, _e, _s in built], question or typed.rsplit("\n", 1)[-1], label)
        if len(named) == 1:   # several counts: the one whose object the question names answered it (fix W1)
            main = named[0]
    for i, (entry, _e, _s) in enumerate(built):
        entry["role"] = "main" if i == main else "side"
    plan = {"question": _cut(question, MAX_QUESTION_CHARS), "terms": _clean_terms(terms),
            "calls": [entry for entry, _e, _s in built], "show": clean_show(show), "age": 0}
    if not plan["question"]:
        plan["question"] = _cut(plan_line(plan, label=label, view=view), MAX_QUESTION_CHARS)
    return plan, ""


def clean_show(value: Any) -> dict:
    """The display flags: ``chart`` and ``table`` (booleans) and the chart ``type`` (one of :data:`CHART_TYPES`)."""
    v = value if isinstance(value, dict) else {}
    kind = v.get("type")
    return {"chart": v.get("chart") is True, "type": kind if kind in CHART_TYPES else None,
            "table": v.get("table") is True}


def _plan_of(value: Any) -> dict | None:
    """The plan of a state, or the value itself when it is a plan."""
    if not isinstance(value, dict):
        return None
    if "calls" in value:
        return value
    p = value.get("plan")
    return p if isinstance(p, dict) else None


def _carried(plan: dict | None, add: int = 0, age: int | None = None) -> dict | None:
    """A copy of the parent's plan, ``add`` answers older (or at ``age``)."""
    if not isinstance(plan, dict):
        return None
    out = copy.deepcopy(plan)
    base = out.get("age") if isinstance(out.get("age"), int) else 0
    out["age"] = min(_MAX_STORED_AGE, max(0, age if age is not None else base + add))
    return out


def build(kind: str, *, parent: Any = None, evidence: Iterable[Any] = (), question: str = "", terms: Any = None,
          show: Any = None, seeds: Iterable[Any] | None = None, pending: Any = None, kb: str = "",
          made_at: str = "", profile: str | None = None, reason: str = "", op: str | None = None,
          max_calls: int = MAX_CALLS, validate: Callable[[str, dict], dict] | None = None,
          label: Callable | None = None, view: Any = None, today: dt.date | None = None,
          typed_also: str = "") -> dict | None:
    """The state after one answer (§9), or None when it is larger than :data:`MAX_STATE_BYTES`.

    ``kind`` (:data:`ANSWER_KINDS`): ``records`` (records or merged: the plan of its evidence,
    :func:`plan_from_evidence`; the parent plan one age older when no contract call succeeded; no plan past the call
    cap, and none when the value screen left out every call that ran: stage-2 review fix M3), ``about_answer`` (the
    parent plan at age 0: it re-ran, and its breakdown calls are not kept, C6), ``knowledge`` and ``no_query`` (the
    people answer, about_people, search, history, sync, not found: the parent plan one age older), ``clarify`` (the
    parent plan at its age, with ``pending``), ``unclear`` and ``small_talk`` (the parent plan at its age; the
    pending clarification ends) and ``refused`` (no plan). ``parent``: the checked parent state (:func:`check`).
    ``terms``: :func:`next_terms`. ``reason`` and ``op``: the fixed codes the reply gives (``last``). ``view``: the
    catalog view (:class:`PlanView`) the value screen of :func:`plan_from_evidence` reads picklists and record types
    through; pass it, or a record agent's call with a picklist value it did not see typed is not kept. ``today`` and
    ``typed_also``: the business date and more of the person's typed words, for the value screen
    (:func:`plan_from_evidence`)."""
    kind = _KIND_ALIASES.get(kind, kind)
    if kind not in ANSWER_KINDS:
        raise ValueError(f"unknown answer kind {kind!r}")
    if isinstance(parent, Checked):
        parent = parent.state
    parent_plan = _plan_of(parent) if isinstance(parent, dict) and "plan" in parent else None
    plan: dict | None = None
    pend = None
    if kind == "records":
        plan, why = plan_from_evidence(evidence, question=question, terms=terms, show=show, seeds=seeds,
                                       max_calls=max_calls, validate=validate, label=label, view=view,
                                       today=today, typed_also=typed_also)
        if why == "no_calls":
            plan = _carried(parent_plan, add=1)
    elif kind == "about_answer":
        plan = _carried(parent_plan, age=0)
    elif kind in ("knowledge", "no_query"):
        plan = _carried(parent_plan, add=1)
    elif kind == "clarify":
        plan = _carried(parent_plan)
        pend = _clean_pending(pending, []) if isinstance(pending, dict) else None
    elif kind in ("unclear", "small_talk"):
        plan = _carried(parent_plan)
    state = {"v": STATE_VERSION, "made_at": str(made_at or "")[:64], "kb": str(kb or "")[:200],
             "profile": profile if isinstance(profile, str) else None, "plan": plan,
             "pending": copy.deepcopy(pend), "last": {"kind": kind, "reason": _reason(reason), "op": _op_name(op)}}
    return fit(state)


def _reason(value: Any) -> str:
    s = str(value or "")
    return s if _REASON_RE.fullmatch(s) else ""


def _op_name(value: Any) -> str | None:
    return value if value in OP_NAMES else None


def state_bytes(state: Any) -> int:
    """The size of a state in UTF-8 bytes, measured as the platform's store measures it before it writes the row
    (orchestrator ``sf_memory_store``: ``sse.strict_json``, ``json.dumps`` with ``ensure_ascii=False`` and the
    default ", " and ": " separators), so a state this side keeps is never one the bridge then stores as NULL
    (stage-1 review fix F9). Key order does not change the size."""
    return len(json.dumps(state, ensure_ascii=False, default=str).encode("utf-8"))


def fit(state: dict | None) -> dict | None:
    """``state``, or None when it is larger than :data:`MAX_STATE_BYTES`."""
    if state is None or state_bytes(state) > MAX_STATE_BYTES:
        return None
    return state


# ----------------------------------------------------------------------------------------------------- the pending
def make_pending(asked: str, for_question: str, choices: Iterable[Any]) -> dict | None:
    """A pending clarification (§5): the question asked (code text), the typed question it is for and at most
    :data:`MAX_CHOICES` numbered choices, each a complete standalone question with optional patch ops
    (``{"label", "question", "ops"}``; ops that are not of the grammar are left out). None without a valid choice."""
    out = []
    for c in choices or ():
        if not isinstance(c, dict):
            continue
        label = _cut(c.get("label"), 200)
        question = _cut(c.get("question"), MAX_QUESTION_CHARS)
        if not label or not question:
            continue
        ops = normalize_ops(c.get("ops")) if c.get("ops") else []
        out.append({"n": len(out) + 1, "label": label, "question": question, "ops": ops or []})
        if len(out) == MAX_CHOICES:
            break
    if not out:
        return None
    return {"asked": _cut(asked, MAX_QUESTION_CHARS), "for": _cut(for_question, MAX_QUESTION_CHARS), "choices": out}


def _clean_pending(p: Any, problems: list[str]) -> dict | None:
    if not isinstance(p, dict) or not isinstance(p.get("choices"), list):
        problems.append("pending")
        return None
    choices = p["choices"]
    if not 1 <= len(choices) <= MAX_CHOICES:
        problems.append("pending.choices")
        return None
    out = []
    for i, c in enumerate(choices):
        ok = isinstance(c, dict) and c.get("n") == i + 1 and isinstance(c.get("label"), str) \
            and isinstance(c.get("question"), str) and c["label"].strip() and c["question"].strip() \
            and len(c["label"]) <= 200 and len(c["question"]) <= MAX_QUESTION_CHARS
        ops = normalize_ops(c.get("ops")) if ok and c.get("ops") else []
        if not ok or ops is None:
            problems.append(f"pending.choices[{i}]")
            return None
        out.append({"n": i + 1, "label": c["label"], "question": c["question"], "ops": ops})
    asked, wanted = p.get("asked"), p.get("for")
    if not isinstance(asked, str) or not isinstance(wanted, str) or len(asked) > MAX_QUESTION_CHARS \
            or len(wanted) > MAX_QUESTION_CHARS:
        problems.append("pending.asked")
        return None
    return {"asked": asked, "for": wanted, "choices": out}


# -------------------------------------------------------------------------------------------------------- checking
@dataclass(frozen=True)
class Checked:
    """What :func:`check` read from a state that came back.

    ``state``: the state in its checked form, or None (no state, or one that cannot be used at all). ``reason``: ""
    or why it was dropped: ``state_invalid``, ``state_version_unknown`` (C10), ``state_too_large``,
    ``profile_mismatch`` (C17). ``missing``: the objects and fields of the plan that no longer resolve (a release
    removed them): the plan is then not usable and the caller asks, naming them. ``problems``: the parts of a dropped
    state that failed (fixed words, for the log)."""

    state: dict | None
    reason: str = ""
    missing: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()

    @property
    def plan(self) -> dict | None:
        return _plan_of(self.state) if self.state else None

    @property
    def pending(self) -> dict | None:
        p = (self.state or {}).get("pending")
        return p if isinstance(p, dict) else None


def _is_day(value: Any) -> bool:
    """None, or a real calendar date written ``YYYY-MM-DD`` (2026-13-45 is none: stage-1 review fix L6)."""
    if value is None:
        return True
    if not isinstance(value, str) or not _DAY_RE.fullmatch(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _clean_period(p: Any) -> dict | None:
    if not isinstance(p, dict) or not isinstance(p.get("phrase"), str) or not _is_day(p.get("from")) \
            or not _is_day(p.get("to")) or not (p.get("date_field") is None or isinstance(p.get("date_field"), str)):
        return None
    return {"phrase": p["phrase"][:200], "from": p.get("from"), "to": p.get("to"), "date_field": p.get("date_field")}


def _clean_keys_from(k: Any) -> dict | None:
    if not isinstance(k, dict) or not isinstance(k.get("field"), str) or not k["field"].strip() \
            or k.get("tool") not in ("group_records", "list_records"):
        return None
    args, err = _check_args(k["tool"], k.get("args"))
    digest, rank = k.get("digest"), k.get("rank")
    if err is not None or not (digest is None or (isinstance(digest, str) and _DIGEST_RE.fullmatch(digest))):
        return None
    if rank is not None and (not isinstance(rank, int) or isinstance(rank, bool) or not 1 <= rank <= MAX_SET_KEYS):
        return None
    out: dict[str, Any] = {"field": k["field"], "tool": k["tool"], "args": args, "digest": digest, "rank": rank}
    if k.get("period") is not None:
        period = _clean_period(k.get("period"))
        if period is None:
            return None
        out["period"] = period
    return out


def _clean_call(c: Any, where: str, problems: list[str]) -> dict | None:
    if not isinstance(c, dict) or c.get("role") not in ("main", "side") or c.get("tool") not in ENFORCED_TOOLS:
        problems.append(where)
        return None
    args, err = _check_args(c["tool"], c.get("args"))
    if err is not None:
        problems.append(f"{where}.args")
        return None
    out: dict[str, Any] = {"role": c["role"], "tool": c["tool"], "args": args}
    if c.get("period") is not None:
        period = _clean_period(c["period"])
        if period is None:
            problems.append(f"{where}.period")
            return None
        out["period"] = period
    if c.get("segments") is not None:
        segs = c["segments"]
        if not isinstance(segs, list) or len(segs) != len(_items(args.get("segments"))) or not all(
                isinstance(s, dict) and isinstance(s.get("label"), str) and _is_day(s.get("from"))
                and _is_day(s.get("to")) and (s.get("phrase") is None or isinstance(s.get("phrase"), str))
                for s in segs):
            problems.append(f"{where}.segments")
            return None
        out["segments"] = [{k: s[k] for k in ("label", "phrase", "from", "to") if k in s} for s in segs]
    if c.get("set_digest") is not None:
        if not isinstance(c["set_digest"], str) or not _DIGEST_RE.fullmatch(c["set_digest"]):
            problems.append(f"{where}.set_digest")
            return None
        out["set_digest"] = c["set_digest"]
    if c.get("keys_from") is not None:
        k = _clean_keys_from(c["keys_from"])
        if k is None:
            problems.append(f"{where}.keys_from")
            return None
        out["keys_from"] = k
    if not isinstance(c.get("nonempty"), bool):
        problems.append(f"{where}.nonempty")
        return None
    out["nonempty"] = c["nonempty"]
    return out


def _clean_plan(p: Any, problems: list[str], max_calls: int) -> dict | None:
    if not isinstance(p, dict) or not isinstance(p.get("question"), str) or len(p["question"]) > MAX_QUESTION_CHARS:
        problems.append("plan")
        return None
    terms = p.get("terms")
    if not isinstance(terms, dict) or not isinstance(terms.get("keys"), list) \
            or not all(k in CONTRACT_KEYS for k in terms["keys"]) or not isinstance(terms.get("text"), str) \
            or len(terms["text"]) > MAX_TERMS_TEXT:
        problems.append("plan.terms")
        return None
    calls = p.get("calls")
    if not isinstance(calls, list) or not 1 <= len(calls) <= max_calls:
        problems.append("plan.calls")
        return None
    out_calls = []
    for i, c in enumerate(calls):
        got = _clean_call(c, f"plan.calls[{i}]", problems)
        if got is None:
            return None
        out_calls.append(got)
    if sum(1 for c in out_calls if c["role"] == "main") != 1:
        problems.append("plan.calls.main")
        return None
    age = p.get("age")
    if not isinstance(age, int) or isinstance(age, bool) or not 0 <= age <= _MAX_STORED_AGE \
            or not isinstance(p.get("show"), dict):
        problems.append("plan.age")
        return None
    return {"question": p["question"], "terms": _clean_terms(terms), "calls": out_calls,
            "show": clean_show(p.get("show")), "age": age}


def check(state: Any, *, profile: str | None = None, view: Any = None, max_calls: int = MAX_CALLS) -> Checked:
    """Read a state that came back (§5, §7.4). Nothing is trusted: every part is checked again, and each stored call
    passes the record tools' argument check. ``profile``: the request's access profile (a state built under another
    one is dropped). ``view``: the catalog view; with it, the objects and fields that no longer resolve are named in
    ``missing``. None gives ``Checked(None)``."""
    if state is None:
        return Checked(None)
    if not isinstance(state, dict) or "v" not in state:
        return Checked(None, "state_invalid", problems=("state",))
    if state.get("v") != STATE_VERSION or isinstance(state.get("v"), bool):
        return Checked(None, "state_version_unknown")
    if state_bytes(state) > MAX_STATE_BYTES:
        return Checked(None, "state_too_large")
    problems: list[str] = []
    for key, kind in (("made_at", str), ("kb", str)):
        if not isinstance(state.get(key, ""), kind) or len(state.get(key, "")) > 200:
            problems.append(key)
    prof = state.get("profile")
    if prof is not None and not isinstance(prof, str):
        problems.append("profile")
    plan = _clean_plan(state["plan"], problems, max_calls) if state.get("plan") is not None else None
    pend = _clean_pending(state["pending"], problems) if state.get("pending") is not None else None
    last = state.get("last")
    if not isinstance(last, dict) or last.get("kind") not in ANSWER_KINDS or not isinstance(last.get("reason"), str) \
            or not _REASON_RE.fullmatch(last["reason"]) or not (last.get("op") is None or last["op"] in OP_NAMES):
        problems.append("last")
    if problems:
        return Checked(None, "state_invalid", problems=tuple(problems))
    if prof != profile:
        return Checked(None, "profile_mismatch")
    clean = {"v": STATE_VERSION, "made_at": state.get("made_at", ""), "kb": state.get("kb", ""), "profile": prof,
             "plan": plan, "pending": pend,
             "last": {"kind": last["kind"], "reason": last["reason"], "op": last.get("op")}}
    missing = _missing(plan, view) if plan is not None and view is not None else ()
    return Checked(clean, "", missing=missing)


def _paths(tool: str, args: dict) -> list[tuple[str, Any]]:
    """(kind, path) of every field a call names on its object: filters (and other_field), group and split, measure,
    columns, sort and the date field. A related child's own paths are :func:`_related_paths`."""
    out: list[tuple[str, Any]] = []
    for key in ("filters", "base_filters", "any_of", "part_filters"):
        for it in _items(args.get(key)):
            out.append(("field", it.get("field")))
            if it.get("other_field"):
                out.append(("field", it.get("other_field")))
    for seg in _items(args.get("segments")):
        for it in _items(seg.get("filters")):
            out.append(("field", it.get("field")))
    for key in ("group_by", "split_by", "count_distinct", "order_by"):
        if args.get(key):
            out.append(("field", args[key]))
    if args.get("measure_field") and args.get("measure") not in (None, "count"):
        out.append(("field", args["measure_field"]))
    if tool == "aggregate_records" and args.get("field"):
        out.append(("field", args["field"]))
    for f in args.get("fields") or [] if isinstance(args.get("fields"), list) else []:
        out.append(("field", f))
    if args.get("date_field"):
        out.append(("date", args["date_field"]))
    return out


def _blocked(view: Any, obj: Any, path: Any) -> bool:
    """The field ``path`` on ``obj`` is a blocked field (credentials, passcodes, identity documents): the record
    tools refuse it (E2c), so a patch or a stored call never names it (stage-1 review fix F12)."""
    reader = getattr(view, "is_blocked", None)
    if reader is None or obj is None:
        return False
    try:
        return bool(reader(obj, path))
    except Exception:  # noqa: BLE001 - a view that cannot read it: not blocked here; the record tools still refuse it
        return False


def _resolve(view: Any, obj: str, path: Any, kind: str = "field") -> bool:
    try:
        if kind == "date" and hasattr(view, "date_path"):
            return view.date_path(obj, path) is not None and not _blocked(view, obj, path)
        return view.path(obj, path) is not None and not _blocked(view, obj, path)
    except Exception:  # noqa: BLE001 - a view that cannot read it: not resolved
        return False


def _call_missing(view: Any, tool: str, args: dict) -> list[str]:
    out: list[str] = []
    try:
        obj = view.object(args.get("object"))
    except Exception:  # noqa: BLE001
        obj = None
    if obj is None:
        return [str(args.get("object"))]
    for kind, path in _paths(tool, args):
        if not _resolve(view, obj, path, kind):
            out.append(f"{obj}.{path}")
    for r in _items(args.get("related")):
        try:
            child = view.object(r.get("child_object"))
        except Exception:  # noqa: BLE001
            child = None
        if child is None:
            out.append(str(r.get("child_object")))
            continue
        for it in _items(r.get("filters")):
            if not _resolve(view, child, it.get("field")):
                out.append(f"{child}.{it.get('field')}")
        for key, kind in (("via_field", "field"), ("date_field", "date")):
            if r.get(key) and not _resolve(view, child, r[key], kind):
                out.append(f"{child}.{r[key]}")
    return out


def _missing(plan: dict, view: Any) -> tuple[str, ...]:
    """The objects and fields of a plan that no longer resolve (or are blocked): each call's, its record set's call's,
    and the field of the call that the set's fresh keys filter (``keys_from.field``; stage-1 review fix L2)."""
    found: list[str] = []
    for c in plan.get("calls") or []:
        found += _call_missing(view, c["tool"], c["args"])
        k = c.get("keys_from")
        if k:
            found += _call_missing(view, k["tool"], k["args"])
            obj = _view_object(view, c["args"].get("object"))
            if obj is not None and not _resolve(view, obj, k.get("field")):
                found.append(f"{obj}.{k.get('field')}")
    return tuple(dict.fromkeys(found))


def usable(checked: Checked, gap: int = 0, max_age: int = MAX_AGE) -> bool:
    """A usable plan (§6.2): a plan, no gap, an age within ``max_age`` and every field still resolving."""
    plan = checked.plan
    return bool(plan) and gap == 0 and not checked.missing and isinstance(plan.get("age"), int) \
        and plan["age"] <= max_age


# --------------------------------------------------------------------------------------------- the patch operations
#: name -> (allowed parameters, required parameters)
_OP_PARAMS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "set_period": (frozenset({"phrase"}), frozenset({"phrase"})),
    "add_filter": (frozenset({"field", "op", "value", "values"}), frozenset({"field", "op"})),
    "remove_filter": (frozenset({"field", "value"}), frozenset({"field"})),
    "set_top": (frozenset({"n", "order"}), frozenset({"n"})),
    "set_show": (frozenset({"chart", "type", "table"}), frozenset()),
    "set_group": (frozenset({"field"}), frozenset({"field"})),
    "set_grain": (frozenset({"grain"}), frozenset({"grain"})),
    "set_measure": (frozenset({"measure", "field"}), frozenset({"measure"})),
    "drill": (frozenset({"value", "rank"}), frozenset()),
    "compare_period": (frozenset({"phrase"}), frozenset({"phrase"})),
    "follow_set": (frozenset({"object", "via_field"}), frozenset({"object"})),
    "new_question": (frozenset(), frozenset()),
}


def _scalar(v: Any) -> bool:
    return isinstance(v, (str, int, float, bool)) and not (isinstance(v, str) and not v.strip())


def _int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _one_op(raw: Any) -> dict | None:
    """One operation in its canonical form ``{name: {params}}``, or None. Accepted: ``{"set_period": {"phrase": ...}}``
    (canonical), ``{"op": "set_period", "args": {...}}`` and the flat ``{"op": "set_period", "phrase": ...}`` (not for
    add_filter, whose parameter ``op`` is the filter's operator)."""
    if not isinstance(raw, dict):
        return None
    if len(raw) == 1 and next(iter(raw)) in OP_NAMES:
        name, params = next(iter(raw.items()))
    elif raw.get("op") in OP_NAMES and set(raw) <= {"op", "args"} and (raw.get("args") is None
                                                                      or isinstance(raw.get("args"), dict)):
        name, params = raw["op"], raw.get("args") or {}
    elif raw.get("op") in OP_NAMES and raw.get("op") != "add_filter":
        name, params = raw["op"], {k: v for k, v in raw.items() if k != "op"}
    else:
        return None
    params = {} if params is None else params
    if not isinstance(params, dict):
        return None
    params = {k: v for k, v in params.items() if v is not None}
    allowed, required = _OP_PARAMS[name]
    if not set(params) <= allowed or not required <= set(params):
        return None
    for key in ("phrase", "field", "object", "via_field"):
        if key in params and (not isinstance(params[key], str) or not params[key].strip()):
            return None
    if name == "add_filter":
        if params["op"] not in OPS:
            return None
        blank = params["op"] in ("is_blank", "is_not_blank")
        if "value" in params and "values" in params:
            return None
        if "value" in params and not _scalar(params["value"]):
            return None
        if "values" in params and (not isinstance(params["values"], list) or not 1 <= len(params["values"]) <= 20
                                   or not all(_scalar(v) for v in params["values"])):
            return None
        if not blank and "value" not in params and "values" not in params:
            return None
        if blank:
            params = {"field": params["field"], "op": params["op"]}
    if name == "remove_filter" and "value" in params and not _scalar(params["value"]):
        return None
    if name == "set_top" and (not _int(params["n"]) or params["n"] < 1 or params.get("order", "desc") not in
                              ("desc", "asc")):
        return None
    if name == "set_show":
        if not params or any(k in params and not isinstance(params[k], bool) for k in ("chart", "table")) \
                or ("type" in params and params["type"] not in CHART_TYPES):
            return None
    if name == "set_grain" and params["grain"] not in GRAINS:
        return None
    if name == "set_measure" and params["measure"] not in MEASURES:
        return None
    if name == "drill" and (("value" in params) == ("rank" in params) or ("value" in params and not _scalar(
            params["value"])) or ("rank" in params and (not _int(params["rank"]) or params["rank"] < 1))):
        return None
    return {name: dict(params)}


def normalize_ops(ops: Any) -> list[dict] | None:
    """The operations in their canonical form ``[{name: {params}}, ...]``; None when ``ops`` is not a list of at most
    :data:`MAX_OPS` operations of the grammar (§6.4)."""
    if not isinstance(ops, list) or len(ops) > MAX_OPS:
        return None
    out = []
    for raw in ops:
        one = _one_op(raw)
        if one is None:
            return None
        out.append(one)
    return out


def _tokens(text: Any) -> list[str]:
    return _TOKEN_RE.findall(unicodedata.normalize("NFKC", str(text or "")).casefold())


def typed_value(value: Any, *texts: Any) -> bool:
    """``value`` appears in one of ``texts`` (the person's typed question, the pending choice): its words in a row,
    ignoring case, spacing and punctuation; a number as a number. True/false is never a name, so it always passes."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        for t in texts:
            if any(float(n) == float(value) for n in _NUMBER_RE.findall(str(t or ""))):
                return True
        return False
    want = _tokens(value)
    if not want:
        return False
    for t in texts:
        have = _tokens(t)
        for i in range(len(have) - len(want) + 1):
            if have[i:i + len(want)] == want:
                return True
    return False


@dataclass(frozen=True)
class Clarify:
    """Why a patch asks instead of running: a fixed ``code`` (:data:`CLARIFY_CODES`), the field, value or detail it is
    about, and for some codes the choices to offer (``{"label", "ops"}``). The words are the renderer's."""

    code: str
    field: str = ""
    value: str = ""
    detail: str = ""
    choices: tuple[dict, ...] = ()


@dataclass(frozen=True)
class Patched:
    """What :func:`apply` made of a plan and its operations.

    ``plan``: the patched plan (age 0, ``question`` empty until the run) or None; ``clarify``: why it cannot run; the
    plan is then None. ``new_question``: the operations drop the plan. ``touched``: the indexes of the calls an
    operation changed or added (the others re-run as stored). ``display_only``: only ``set_show`` (the data calls
    re-run unchanged). ``not_chartable``: a chart was asked of a plan with no chartable call (the charts reason line).
    ``contact_blocked``: contact fields of listed columns this question does not ask for (the record tools leave them
    out). ``ops``: the operations in their canonical form."""

    plan: dict | None
    clarify: Clarify | None = None
    new_question: bool = False
    touched: frozenset[int] = frozenset()
    display_only: bool = False
    not_chartable: bool = False
    contact_blocked: tuple[str, ...] = ()
    ops: tuple[dict, ...] = ()


class _Work:
    """The plan being patched: copies of its calls, the display flags and what the checks read."""

    def __init__(self, plan: dict, view: Any, texts: tuple[str, ...], today: dt.date, terms: Terms,
                 max_calls: int = MAX_CALLS) -> None:
        self.calls: list[dict] = copy.deepcopy(plan.get("calls") or [])
        self.show = clean_show(plan.get("show"))
        self.view, self.texts, self.today, self.terms = view, texts, today, terms
        self.max_calls = max_calls
        self.touched: set[int] = set()   # id() of the call dicts an operation changed or added
        self.not_chartable = False

    def main(self) -> dict:
        return next(c for c in self.calls if c.get("role") == "main")

    def touch(self, call: dict) -> None:
        """``call`` changed: it no longer runs as stored, and its set digest no longer describes it."""
        self.touched.add(id(call))
        call.pop("set_digest", None)

    def obj(self, call: dict) -> str | None:
        try:
            return self.view.object(call["args"].get("object"))
        except Exception:  # noqa: BLE001
            return None

    def path(self, obj: str | None, text: Any) -> Any:
        """The resolved path, or None when it does not resolve or names a blocked field (asked as unknown_field)."""
        if obj is None or not isinstance(text, str):
            return None
        try:
            got = self.view.path(obj, text)
        except Exception:  # noqa: BLE001
            return None
        return None if got is not None and _blocked(self.view, obj, text) else got

    def kind(self, obj: str | None, text: Any) -> str | None:
        reader = getattr(self.view, "field_kind", None)
        if reader is None or obj is None:
            return None
        try:
            return reader(obj, text)
        except Exception:  # noqa: BLE001
            return None

    def replace_main(self, new: dict, keep_sides: bool = True) -> None:
        """``new`` becomes the main call (the sides stay when ``keep_sides``)."""
        new["role"] = "main"
        self.touch(new)
        self.calls = [new if c.get("role") == "main" else c for c in self.calls] if keep_sides else [new]


def _new_call(tool: str, args: dict, src: dict | None = None, keep_period: bool = True) -> dict:
    """A plan call ``tool(args)``; the stored dates of ``src`` stay when its period words do (D1)."""
    out: dict[str, Any] = {"role": "side", "tool": tool, "args": {k: v for k, v in args.items() if v is not None}}
    if keep_period and src is not None and isinstance(src.get("period"), dict) \
            and src["period"].get("phrase") == args.get("period"):
        out["period"] = copy.deepcopy(src["period"])
    out["nonempty"] = False
    return out


def _carry(args: dict, *keys: str) -> dict:
    return {k: copy.deepcopy(args[k]) for k in keys if k in args}


def _same_field(work: _Work, obj: str | None, a: Any, b: Any) -> bool:
    pa, pb = work.path(obj, a), work.path(obj, b)
    if pa is not None and pb is not None:
        return pa.text == pb.text
    return str(a or "").casefold() == str(b or "").casefold()


def _same_value(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return _tokens(a) == _tokens(b)


def _period_words(work: _Work, phrase: str) -> tuple[str | None, Clarify | None]:
    """("phrase", None) when the words parse; (None, None) for "all time" words (no period); else a clarify."""
    try:
        parse_period(phrase, work.today)
    except PeriodError as exc:
        if getattr(exc, "code", "") == "E5d":
            return None, None
        return None, Clarify("bad_period", value=phrase, detail=getattr(exc, "code", "E5"))
    return phrase, None


#: The arguments that do not make another query of the same shape (a ranking's size and order, the period).
_NOT_SHAPE = frozenset({"period", "top", "order", "limit", "min_value"})


def _period_sides(work: _Work) -> bool:
    """The plan compares periods with side calls: a side call of the main call's tool and shape (object, grouping,
    measure, filters) with another period, as ``compare_period`` on a grouping or a trend makes it, or as the record
    agent ran it (stage-1 review fix M10)."""
    main = work.main()
    shape = {k: v for k, v in main["args"].items() if k not in _NOT_SHAPE}
    return any(c is not main and c["tool"] == main["tool"]
               and {k: v for k, v in c["args"].items() if k not in _NOT_SHAPE} == shape
               and c["args"].get("period") != main["args"].get("period") for c in work.calls)


def _op_set_period(work: _Work, p: dict) -> Clarify | None:
    main = work.main()
    if main["tool"] == "compare_records" and any(s.get("period") for s in _items(main["args"].get("segments"))):
        return Clarify("which_side")
    if _period_sides(work):
        return Clarify("which_side")
    dated = [c for c in work.calls if c["args"].get("date_field") and c["tool"] != "compare_records"]
    if not dated:
        return Clarify("no_date_field")
    phrase, err = _period_words(work, p["phrase"])
    if err is not None:
        return err
    if phrase is None and any(c["tool"] == "trend_records" for c in dated):
        return Clarify("bad_period", value=p["phrase"], detail="E5d")
    for c in dated:
        if phrase is None:
            c["args"].pop("period", None)
            c["args"].pop("date_field", None)
        else:
            c["args"]["period"] = phrase
        c.pop("period", None)   # a typed period is relative to today: its dates come from the run
        work.touch(c)
    return None


def _op_add_filter(work: _Work, p: dict) -> Clarify | None:
    main = work.main()
    obj = work.obj(main)
    path = work.path(obj, p["field"])
    if path is None:
        return Clarify("unknown_field", field=p["field"])
    vals = p.get("values") if "values" in p else ([p["value"]] if "value" in p else [])
    for v in vals:
        if not typed_value(v, *work.texts):
            return Clarify("value_not_typed", field=p["field"], value=str(v))
    item: dict[str, Any] = {"field": path.text, "op": p["op"]}
    if "values" in p:
        item["values"] = list(p["values"])
    elif "value" in p:
        item["value"] = p["value"]
    targets = [main] + [c for c in work.calls if c is not main and work.path(work.obj(c), p["field"]) is not None]
    for c in targets:
        key = _filter_key(c["tool"])
        items = _items(c["args"].get(key))
        cobj = work.obj(c)
        if p["op"] in ("eq", "in"):
            items = [it for it in items
                     if not (it.get("op") in ("eq", "in") and _same_field(work, cobj, it.get("field"), p["field"]))]
        items.append(dict(item, field=work.path(cobj, p["field"]).text))
        if len(items) > MAX_FILTERS:
            return Clarify("too_many_filters")
        c["args"][key] = items
        work.touch(c)
    return None


def _op_remove_filter(work: _Work, p: dict) -> Clarify | None:
    removed = 0
    for c in work.calls:
        key = _filter_key(c["tool"])
        cobj = work.obj(c)
        kept = []
        for it in _items(c["args"].get(key)):
            if not _same_field(work, cobj, it.get("field"), p["field"]):
                kept.append(it)
                continue
            if "value" not in p:
                removed += 1
                continue
            if it.get("op") in ("in", "not_in") and isinstance(it.get("values"), list):
                rest = [v for v in it["values"] if not _same_value(v, p["value"])]
                if len(rest) < len(it["values"]):
                    removed += 1
                    if rest:
                        kept.append(dict(it, values=rest))
                    continue
            elif "value" in it and _same_value(it.get("value"), p["value"]):
                removed += 1
                continue
            kept.append(it)
        if kept != _items(c["args"].get(key)):
            if kept:
                c["args"][key] = kept
            else:
                c["args"].pop(key, None)
            work.touch(c)
    return None if removed else Clarify("no_such_filter", field=p["field"])


def _op_set_top(work: _Work, p: dict) -> Clarify | None:
    main = work.main()
    n = p["n"]
    if main["tool"] == "group_records":
        if n > MAX_TOP:
            return Clarify("top_too_large", value=str(n))
        main["args"]["top"] = n
    elif main["tool"] == "list_records":
        # a list's top N is its first N rows in the order it was shown: only the limit changes. Its bottom N is no
        # query of its own (a reversed sort would list other records), so it asks (stage-1 review fix M9)
        if p.get("order") == "asc":
            return Clarify("list_bottom", value=str(n))
        if n > MAX_LIMIT:
            return Clarify("top_too_large", value=str(n))
        main["args"]["limit"] = n
        work.touch(main)
        return None
    else:
        return Clarify("top_of_what")
    if "order" in p:
        main["args"]["order"] = p["order"]
    work.touch(main)
    return None


def _op_set_show(work: _Work, p: dict) -> Clarify | None:
    for key in ("chart", "type", "table"):
        if key in p:
            work.show[key] = p[key]
    if work.show.get("type") and "chart" not in p:
        work.show["chart"] = True
    if work.show["chart"] and not any(c["tool"] in _CHARTABLE or c["tool"] == "count_records" for c in work.calls):
        work.not_chartable = True
    return None


def _measured(args: dict) -> tuple[str, str | None]:
    """(measure, measure field) of a count, aggregate, group, trend or compare call."""
    if args.get("count_distinct"):
        return "count_distinct", args["count_distinct"]
    if args.get("op") and args.get("field"):   # aggregate_records
        return str(args["op"]), args["field"]
    return str(args.get("measure") or "count"), args.get("measure_field")


def _measure_args(measure: str, mfield: str | None) -> dict:
    return {"measure": measure, "measure_field": mfield} if measure != "count" else {}


def _op_set_group(work: _Work, p: dict) -> Clarify | None:
    main = work.main()
    obj = work.obj(main)
    path = work.path(obj, p["field"])
    if path is None:
        return Clarify("unknown_field", field=p["field"])
    if work.kind(obj, p["field"]) in _DATE_KINDS:
        return _op_set_grain(work, {"grain": "month"})
    a, tool = main["args"], main["tool"]
    if tool == "group_records":
        a["group_by"] = path.text
        if a.get("split_by") and _same_field(work, obj, a["split_by"], path.text):
            a.pop("split_by")
        main.pop("set_digest", None)
        work.touch(main)
        return None
    if tool == "trend_records":
        a["split_by"] = path.text
        work.touch(main)
        return None
    if tool in ("count_records", "aggregate_records"):
        measure, mfield = _measured(a)
        if measure not in MEASURES:
            return Clarify("measure_not_for", value=measure)
        new = _carry(a, "object", "filters", "any_of", "date_field", "period", "related")
        new.update({"group_by": path.text, **_measure_args(measure, mfield)})
        work.replace_main(_new_call("group_records", new, main))
        return None
    return Clarify("group_not_for", detail=tool)


def _op_set_grain(work: _Work, p: dict) -> Clarify | None:
    main = work.main()
    a, tool, grain = main["args"], main["tool"], p["grain"]
    if tool == "trend_records":
        a["grain"] = grain
        if isinstance(main.get("period"), dict):
            main["period"] = _trend_dates(main["period"], grain, work.today)
        work.touch(main)
        return None
    if tool not in ("group_records", "count_records", "aggregate_records", "percentage_of_records"):
        return Clarify("grain_not_for", detail=tool)
    if not a.get("date_field"):
        return Clarify("no_date_field")
    if not a.get("period"):
        return Clarify("no_period")
    new = _carry(a, "object", "date_field", "period", "any_of", "related")
    new["grain"] = grain
    keys_from = None
    if tool == "percentage_of_records":
        new["part_filters"] = copy.deepcopy(a.get("part_filters"))
        if a.get("base_filters"):
            new["filters"] = copy.deepcopy(a["base_filters"])
    else:
        new.update(_carry(a, "filters"))
        measure, mfield = _measured(a)
        if measure not in MEASURES:
            return Clarify("measure_not_for", value=measure)
        new.update(_measure_args(measure, mfield))
    if tool == "group_records":
        if a.get("split_by"):
            return Clarify("grain_two_splits")
        new["split_by"] = a["group_by"]
        if a.get("top"):
            if a["top"] > MAX_SET_KEYS:
                return Clarify("grain_top_too_large", value=str(a["top"]))
            keys_from = _set_ref(main, a["group_by"])
    call = _new_call("trend_records", new, main)
    if isinstance(call.get("period"), dict):
        call["period"] = _trend_dates(call["period"], grain, work.today)
    if keys_from:
        call["keys_from"] = keys_from
    work.replace_main(call)
    return None


def _set_ref(set_call: dict, field_path: str, rank: int | None = None) -> dict:
    """``keys_from`` (§10): the call that made a set (in its stored form, with its stored dates), the field of the new
    call its keys filter, its digest and, for an ordinal drill, the rank."""
    out: dict[str, Any] = {"field": field_path, "tool": set_call["tool"], "args": copy.deepcopy(set_call["args"]),
                           "digest": set_call.get("set_digest"), "rank": rank}
    if isinstance(set_call.get("period"), dict):
        out["period"] = copy.deepcopy(set_call["period"])
    return out


def _op_set_measure(work: _Work, p: dict) -> Clarify | None:
    main = work.main()
    a, tool, measure = main["args"], main["tool"], p["measure"]
    obj = work.obj(main)
    mfield = p.get("field")
    if measure != "count":
        if not mfield:
            return Clarify("measure_field", value=measure)
        path = work.path(obj, mfield)
        if path is None:
            return Clarify("unknown_field", field=mfield)
        mfield = path.text
        kind = work.kind(obj, mfield)
        if measure in ("sum", "avg") and kind is not None and kind not in _NUMBER_KINDS:
            return Clarify("number_field", field=mfield)
        if measure in ("min", "max") and kind is not None and kind not in _NUMBER_KINDS | _DATE_KINDS:
            return Clarify("number_field", field=mfield)
    if tool in ("group_records", "compare_records") or (tool == "trend_records" and not a.get("part_filters")):
        a.pop("measure", None)
        a.pop("measure_field", None)
        a.update(_measure_args(measure, mfield))
        work.touch(main)
        return None
    if tool in ("count_records", "aggregate_records"):
        base = _carry(a, "object", "filters", "any_of", "date_field", "period", "related")
        if measure == "count":
            work.replace_main(_new_call("count_records", base, main))
        elif measure == "count_distinct":
            work.replace_main(_new_call("count_records", dict(base, count_distinct=mfield), main))
        else:
            work.replace_main(_new_call("aggregate_records", dict(base, op=measure, field=mfield), main))
        return None
    return Clarify("measure_not_for", detail=tool)


def _op_drill(work: _Work, p: dict) -> Clarify | None:
    main = work.main()
    a = main["args"]
    if main["tool"] != "group_records":
        return Clarify("drill_not_group", detail=main["tool"])
    base = _carry(a, "object", "filters", "any_of", "date_field", "period", "related")
    if "value" in p:
        if not typed_value(p["value"], *work.texts):
            return Clarify("value_not_typed", field=a["group_by"], value=str(p["value"]))
        base["filters"] = _items(base.get("filters")) + [{"field": a["group_by"], "op": "eq", "value": p["value"]}]
        if len(base["filters"]) > MAX_FILTERS:
            return Clarify("too_many_filters")
        call = _new_call("list_records", base, main)
    else:
        top = a.get("top")
        if not top or p["rank"] > min(top, MAX_SET_KEYS):
            return Clarify("rank_not_shown", value=str(p["rank"]))
        call = _new_call("list_records", base, main)
        call["keys_from"] = _set_ref(main, a["group_by"], rank=p["rank"])
    work.replace_main(call, keep_sides=False)
    work.show = clean_show(None)
    return None


_COUNTS = ("count_records", "aggregate_records")


def _twins(work: _Work) -> list[dict]:
    """The other counts of a plan whose main call is a count or an aggregate (step-5 fix W1): side calls of those
    tools that are not the same query for another period. An answer that ran two counts ("how many interviews this
    month": the client and the internal interviews) showed both, and the main call is only the one picked."""
    main = work.main()
    if main["tool"] not in _COUNTS:
        return []
    shape = {k: v for k, v in main["args"].items() if k not in _NOT_SHAPE}
    return [c for c in work.calls if c is not main and c["tool"] in _COUNTS
            and {k: v for k, v in c["args"].items() if k not in _NOT_SHAPE} != shape]


def _unsure_main(work: _Work, question: Any) -> bool:
    """The plan's main call may not be the query the person means (step-5 fix W1): the answer ran other counts as
    well (:func:`_twins`), and the plan's question does not name the main call's object alone
    (:func:`_named_calls`). An operation of the main call alone then asks which query is meant."""
    if not _twins(work):
        return False
    main = work.main()
    named = _named_calls(work.calls, question, getattr(work.view, "label", None))
    return [work.calls[i] for i in named] != [main]


def _compare_of(call: dict, phrase: str) -> tuple[dict | None, Clarify | None]:
    """The compare_records call of a count or an aggregate with a period: its period beside ``phrase`` (§6.4)."""
    a = call["args"]
    if a.get("any_of"):
        return None, Clarify("compare_not_for", detail="any_of")
    measure, mfield = _measured(a)
    if measure not in MEASURES:
        return None, Clarify("measure_not_for", value=measure)
    new = _carry(a, "object", "date_field", "filters", "related")
    new["segments"] = [{"label": _cut(a["period"], 80), "period": a["period"]},
                       {"label": _cut(phrase, 80), "period": phrase}]
    new.update(_measure_args(measure, mfield))
    out = _new_call("compare_records", new, call, keep_period=False)
    if isinstance(call.get("period"), dict):
        info = call["period"]
        out["segments"] = [{"label": new["segments"][0]["label"], "phrase": info.get("phrase"),
                            "from": info.get("from"), "to": info.get("to")},
                           {"label": new["segments"][1]["label"]}]
    return out, None


def _op_compare_period(work: _Work, p: dict) -> Clarify | None:
    main = work.main()
    a, tool = main["args"], main["tool"]
    if tool in ("list_records", "compare_records"):
        return Clarify("compare_not_for", detail=tool)
    if not a.get("date_field"):
        return Clarify("no_date_field")
    if not a.get("period"):
        return Clarify("no_period")
    phrase, err = _period_words(work, p["phrase"])
    if err is not None:
        return err
    if phrase is None:
        return Clarify("bad_period", value=p["phrase"], detail="E5d")
    info = main.get("period") if isinstance(main.get("period"), dict) and main["period"].get("phrase") == a["period"] \
        else None
    current = (info.get("from"), info.get("to")) if info else _dates(a["period"], work.today)
    if _tokens(phrase) == _tokens(a["period"]) or _dates(phrase, work.today) == current:
        return Clarify("same_period", value=phrase)   # also a typed period that now means the kept dates (D1)
    if tool in ("count_records", "aggregate_records"):
        call, err = _compare_of(main, phrase)
        if err is not None:
            return err
        # the other counts the answer showed for the same period are compared too (step-5 fix W1: "how many
        # interviews this month" ran the client and the internal interviews; "compare with previous month" compares
        # what the answer showed, never only one of them)
        twins = {id(c) for c in _twins(work)}
        for i, twin in enumerate(list(work.calls)):
            if id(twin) in twins and twin["args"].get("period") == a["period"] and twin["args"].get("date_field"):
                other, bad = _compare_of(twin, phrase)
                if bad is None:
                    work.calls[i] = other
                    work.touch(other)
        work.replace_main(call)
        return None
    if len(work.calls) + 1 > work.max_calls:
        return Clarify("call_cap")
    side = _new_call(tool, dict(copy.deepcopy(a), period=phrase), None)
    work.touch(side)
    work.calls.append(side)
    return None


def _set_object(work: _Work, call: dict) -> tuple[str | None, Clarify | None]:
    """The object whose records a set holds: a list's or a count's own object, a grouping's lookup target."""
    obj = work.obj(call)
    if call["tool"] in ("list_records", "count_records"):
        return obj, None
    if call["tool"] != "group_records":
        return None, Clarify("set_not_supported", detail=call["tool"])
    reader = getattr(work.view, "target", None)
    target = None
    if reader is not None:
        try:
            target = reader(obj, call["args"].get("group_by"))
        except Exception:  # noqa: BLE001
            target = None
    else:
        path = work.path(obj, call["args"].get("group_by"))
        if path is not None and path.refs and path.refs[-1].field == path.field:
            target = path.refs[-1].target
    return (target, None) if target else (None, Clarify("set_not_supported", detail="group_by"))


def _op_follow_set(work: _Work, p: dict) -> Clarify | None:
    main = work.main()
    a, tool = main["args"], main["tool"]
    try:
        new_obj = work.view.object(p["object"])
    except Exception:  # noqa: BLE001
        new_obj = None
    if new_obj is None:
        return Clarify("unknown_object", field=p["object"])
    set_obj, err = _set_object(work, main)
    if err is not None:
        return err
    ranked = (tool == "group_records" and a.get("top")) or (tool == "list_records" and a.get("limit"))
    if tool == "group_records" and not a.get("top"):
        return Clarify("set_not_supported", detail="group_by")
    if ranked and (a.get("top") or a.get("limit")) > MAX_SET_KEYS:
        return Clarify("set_too_large", value=str(a.get("top") or a.get("limit")))
    reader = getattr(work.view, "lookups", None)
    links = list(reader(new_obj, set_obj) or []) if reader is not None else []
    if p.get("via_field"):
        via = work.path(new_obj, p["via_field"])
        if via is None or not any(_same_field(work, new_obj, via.text, x) for x in links):
            return Clarify("no_link", field=p["via_field"])
        via_text = via.text
    elif len(links) == 1:
        via_text = links[0]
    elif links:
        return Clarify("which_link", choices=tuple({"label": x, "ops": [{"follow_set": {"object": new_obj,
                                                                                       "via_field": x}}]}
                                                   for x in links))
    else:
        return Clarify("no_link", field=new_obj)
    if ranked:
        call = _new_call("list_records", {"object": new_obj}, None)
        call["keys_from"] = _set_ref(main, via_text)
    else:
        if a.get("any_of") or a.get("related"):
            return Clarify("set_not_supported", detail="any_of" if a.get("any_of") else "related")
        enforced = _term_plan(work.terms, tool, a, work.view)
        if enforced.reject is not None:
            return Clarify("term_reject", value=enforced.reject[0], detail=enforced.reject[1])
        items = _items(a.get("filters")) + [dict(it) for key, it in enforced.add if key == "filters"]
        if any(it.get("other_field") for it in items):
            return Clarify("set_not_supported", detail="other_field")
        filters = [dict(it, field=f"{via_text}.{it['field']}") for it in items]
        if a.get("date_field") and a.get("period"):
            info = main.get("period") if isinstance(main.get("period"), dict) else None
            span = (info.get("from"), info.get("to")) if info else _dates(a["period"], work.today)
            if not span or not (span[0] or span[1]):
                return Clarify("no_period")
            dfield = (info or {}).get("date_field") or a["date_field"]
            if span[0]:
                filters.append({"field": f"{via_text}.{dfield}", "op": "gte", "value": span[0]})
            if span[1]:
                end = (dt.date.fromisoformat(span[1]) + dt.timedelta(days=1)).isoformat()
                filters.append({"field": f"{via_text}.{dfield}", "op": "lt", "value": end})
        if len(filters) > MAX_FILTERS:
            return Clarify("too_many_filters")
        call = _new_call("list_records", {"object": new_obj, **({"filters": filters} if filters else {})}, None)
    work.replace_main(call, keep_sides=False)
    work.show = clean_show(None)
    return None


_OP_FUNCS: dict[str, Callable[[_Work, dict], Clarify | None]] = {
    "set_period": _op_set_period, "add_filter": _op_add_filter, "remove_filter": _op_remove_filter,
    "set_top": _op_set_top, "set_show": _op_set_show, "set_group": _op_set_group, "set_grain": _op_set_grain,
    "set_measure": _op_set_measure, "drill": _op_drill, "compare_period": _op_compare_period,
    "follow_set": _op_follow_set,
}


#: The operations that change the main call alone into another query (a compare covers the other counts itself).
_MAIN_ONLY = frozenset({"set_group", "set_grain", "set_measure", "follow_set"})
#: Words of a field name that say nothing of the field.
_NAME_FILLER = frozenset({"c", "r", "the", "of", "field"})


def _name_words(text: Any) -> frozenset[str]:
    """The words of a field's API name or label, plurals aside ("Status__c" -> {"status"}; "Client_Account__c" ->
    {"client", "account"})."""
    return frozenset(_stem(w) for w in _tokens(_pretty(text)) if w not in _NAME_FILLER)


def _value_fits(work: _Work, obj: str, api: str, kind: str | None, values: list) -> bool:
    """The filter values could be stored in the field ``obj.api``: a picklist that lists them, a number field for
    numbers, a date field for dates, a true/false field for true/false, any other field for a text."""
    for v in values:
        if isinstance(v, bool):
            if kind != "boolean":
                return False
        elif isinstance(v, (int, float)):
            if kind not in _NUMBER_KINDS:
                return False
        elif kind in ("picklist", "multipicklist"):
            if not schema_value(work.view, obj, api, str(v)):
                return False
        elif kind in _NUMBER_KINDS or kind == "boolean" or (
                kind in _DATE_KINDS and not _DATE_VALUE_RE.fullmatch(str(v).strip())):
            return False
    return True


def _field_matches(work: _Work, obj: str, raw: str, values: list | None, want: str) -> list[str]:
    """The fields of ``obj`` (``view.fields_of``: API name, label, kind; blocked and contact fields left out) whose
    label or API name holds every word of ``raw``; those whose label or name is just those words first; then only
    those the values fit (:func:`_value_fits`), or of the kind ``want`` needs ("number" for sum and avg)."""
    reader = getattr(work.view, "fields_of", None)
    words = _name_words(raw)
    if reader is None or not words:
        return []
    try:
        listed = list(reader(obj) or ())
    except Exception:  # noqa: BLE001 - no field list: nothing is matched
        return []
    hits = [(api, kind, words == _name_words(label) or words == _name_words(api)) for api, label, kind in listed
            if words <= _name_words(label) or words <= _name_words(api)]
    if values:
        hits = [h for h in hits if _value_fits(work, obj, h[0], h[1], values)]
    if want == "number":
        hits = [h for h in hits if h[1] in _NUMBER_KINDS]
    exact = [h for h in hits if h[2]]
    return [h[0] for h in (exact or hits)]


def _plan_lookups(work: _Work) -> set[str]:
    """The first lookup of every field path the plan's calls group, split or filter by (a hop the plan already took)."""
    out: set[str] = set()
    for c in work.calls:
        a = c["args"]
        for raw in [a.get("group_by"), a.get("split_by")] + [it.get("field") for key in ("filters", "base_filters")
                                                              for it in _items(a.get(key))]:
            if isinstance(raw, str) and raw.strip():
                out.add(raw.split(".")[0].casefold())
    return out


def _ground_field(work: _Work, raw: Any, values: list | None = None, want: str = "") -> str | None:
    """The path a field name the model guessed means on the main call's object, when it does not resolve as it is
    (step-5 fix, field grounding: "Status__c" for Interview_Status__c, "Client__c" for Client_Account__c,
    "Department__c" for Recruiter__c.Department__c): the one field of the object whose label or API name holds its
    words and that the values fit (:func:`_field_matches`); with none on the object, the one such field of an object
    a lookup of it points to (a lookup the plan already groups or filters by first). None when no field or more than
    one fits: the check then asks which field, as before. Never matches a value: only names and labels of the schema."""
    main = work.main()
    obj = work.obj(main)
    if obj is None or not isinstance(raw, str) or not raw.strip() or "." in raw:
        return None
    if _name_words(raw) <= _name_words(obj) | _name_words(_label(getattr(work.view, "label", None), obj) or ""):
        return None   # the object's own name ("Interview__c" on interviews) names no field of it
    own = _field_matches(work, obj, raw, values, want)
    if own:
        return own[0] if len(own) == 1 else None
    reader = getattr(work.view, "fields_of", None)
    hops: list[str] = []
    try:
        for api, _words, kind in list(reader(obj) or ()) if reader is not None else []:
            if kind != "reference" or api in SYSTEM_LOOKUPS:
                continue
            target = getattr(work.view, "target", lambda *_a: None)(obj, api)
            if not target or target == obj:
                continue
            hops += [f"{api}.{f}" for f in _field_matches(work, target, raw, values, want)]
    except Exception:  # noqa: BLE001 - no hop read: nothing is matched
        return None
    taken = _plan_lookups(work)
    near = [h for h in hops if h.split(".")[0].casefold() in taken]
    pick = near or hops
    return pick[0] if len(pick) == 1 else None


def _grounded(work: _Work, op: dict) -> dict:
    """``op`` with a field name that does not resolve on the main call's object replaced by the path it means
    (:func:`_ground_field`), for add_filter, remove_filter (the plan's own filter fields first), set_group and
    set_measure; anything else as it is."""
    name, p = next(iter(op.items()))
    field_name = p.get("field") if isinstance(p, dict) else None
    if name not in ("add_filter", "remove_filter", "set_group", "set_measure") or not isinstance(field_name, str):
        return op
    try:
        obj = work.obj(work.main())
        if obj is None or work.view.path(obj, field_name) is not None:
            return op   # a field that resolves (a blocked one too: its own check refuses it) is never replaced
        if name == "remove_filter":
            words = _name_words(field_name)
            have = [it.get("field") for c in work.calls for key in ("filters", "base_filters")
                    for it in _items(c["args"].get(key)) if isinstance(it.get("field"), str)]
            label = getattr(work.view, "label", None)
            hit = sorted({f for f in have if words and (words <= _name_words(f.split(".")[-1])
                                                        or words <= _name_words(_label(label, obj, f) or ""))})
            got = hit[0] if len(hit) == 1 else None
        else:
            values = p.get("values") if name == "add_filter" and isinstance(p.get("values"), list) else (
                [p["value"]] if name == "add_filter" and "value" in p else None)
            want = "number" if name == "set_measure" and p.get("measure") in ("sum", "avg") else ""
            got = _ground_field(work, field_name, values, want)
    except Exception:  # noqa: BLE001 - not grounded: the check asks as before
        return op
    return {name: dict(p, field=got)} if got else op


def _scope_items(view: Any, obj: str | None, items: list[dict]) -> list[tuple[int, str, str]]:
    """(index, candidate prefix, state) of the record-type and person-account filters among a call's AND filter
    ``items`` that keep only candidates ("present") or other accounts ("conflict"), read as the candidates contract
    reads them (``term_contracts._own_state``)."""
    out = []
    for i, it in enumerate(items):
        try:
            p = view.path(obj, it.get("field")) if obj else None
        except Exception:  # noqa: BLE001
            p = None
        if p is None:
            continue
        prefixes = ([""] if obj == "Account" else []) + [x for x in [_tc._candidate_prefix(p)] if x]
        for pre in prefixes:
            rest = _tc._rest(p, pre)
            use = _tc._type_use(rest)
            if use not in ("rt", "flag"):
                continue
            try:
                state = _tc._own_state(it, rest, view)
            except Exception:  # noqa: BLE001
                state = None
            if use == "flag" and it.get("op") == "eq" and _tc._bool(it.get("value")) is True:
                state = "present"   # it keeps person accounts only, whichever record type code would add
            if state in ("present", "conflict"):
                out.append((i, pre, state))
    return out


def _relax_scope(work: _Work) -> None:
    """Step-5 fix W3: a stored call whose own filters keep only candidates (a record type or person-account filter the
    record agent wrote itself) runs without that filter when this run's terms relax the candidates contract, as the
    term rules relax it: the person asked for every account type (``all_types``), or named other account types and
    the call also keeps another type (``other_accounts``, a "conflict" filter). Code's own candidate filter is never
    stored, so the Read-as line ("every account type") and what runs agree. Typed words only relax (§7.1)."""
    t = work.terms
    if "candidates" not in t.keys or not (t.all_types or t.other_accounts) or work.view is None:
        return
    for c in work.calls:
        key = _filter_key(c["tool"])
        items = _items(c["args"].get(key))
        found = _scope_items(work.view, work.obj(c), items)
        if t.all_types:
            drop = {i for i, _pre, state in found if state == "present"}
        else:
            other = {pre for _i, pre, state in found if state == "conflict"}
            drop = {i for i, pre, state in found if state == "present" and pre in other}
        if not drop:
            continue
        kept = [it for i, it in enumerate(items) if i not in drop]
        if kept:
            c["args"][key] = kept
        else:
            c["args"].pop(key, None)
        work.touch(c)


def apply(plan: Any, ops: Any, *, typed: str, view: Any, today: dt.date, terms: Terms = NO_TERMS,
          pending_text: str = "", contact_ok: bool | None = None, max_calls: int = MAX_CALLS) -> Patched:
    """Apply at most :data:`MAX_OPS` operations (§6.4) to a copy of ``plan`` and check every call of the result.

    ``typed``: the person's typed question (filter and drill values must appear in it, or in ``pending_text``, the
    pending choice it answers); ``view``: the catalog view; ``today``: for the period words; ``terms``: the terms of
    the run (:func:`terms_for`), checked on every call (a T1, T2 or T3 refusal asks); ``contact_ok``: whether this
    question asks for contact details (default: read from ``typed``; never carried); ``max_calls``: the call cap
    (``SFA_MEMORY_MAX_CALLS``). Operations apply in order, the first failing check gives the clarify.
    ``new_question`` drops the plan."""
    canon = normalize_ops(ops)
    if canon is None:
        return Patched(None, Clarify("too_many_ops" if isinstance(ops, list) and len(ops) > MAX_OPS else "bad_ops"))
    names = [next(iter(o)) for o in canon]
    if "new_question" in names:
        return Patched(None, new_question=True, ops=tuple(canon))
    plan = _plan_of(plan)
    if not canon:
        return Patched(None, Clarify("bad_ops"))
    if not plan or not any(isinstance(c, dict) and c.get("role") == "main" for c in plan.get("calls") or []):
        return Patched(None, Clarify("no_plan"), ops=tuple(canon))
    work = _Work(plan, view, (str(typed or ""), str(pending_text or "")), today, terms, max_calls)
    canon = [_grounded(work, one) for one in canon]
    for one in canon:
        name, params = next(iter(one.items()))
        if name in _MAIN_ONLY and _unsure_main(work, plan.get("question")):
            return Patched(None, Clarify("which_call"), ops=tuple(canon))
        got = _OP_FUNCS[name](work, params)
        if got is not None:
            return Patched(None, got, ops=tuple(canon))
    _relax_scope(work)
    err = _final_checks(work)
    if err is not None:
        return Patched(None, err, ops=tuple(canon))
    ok = asks_for_contact(typed) if contact_ok is None else bool(contact_ok)
    blocked = () if ok else _contact_fields(work)
    new_plan = {"question": "", "terms": _clean_terms(plan.get("terms")), "calls": work.calls, "show": work.show,
                "age": 0}
    touched = frozenset(i for i, c in enumerate(work.calls) if id(c) in work.touched)
    return Patched(new_plan, touched=touched, display_only=all(n == "set_show" for n in names),
                   not_chartable=work.not_chartable, contact_blocked=blocked, ops=tuple(canon))


def _final_checks(work: _Work) -> Clarify | None:
    """The checks of every patched plan (§6.4): the record tools' argument check, the filter cap (with room for a
    record set's filter), the term contracts on each call and each set call, and the call cap."""
    if len(work.calls) > work.max_calls:
        return Clarify("call_cap")
    for c in work.calls:
        args, err = _check_args(c["tool"], c["args"])
        if err is not None:
            return Clarify("bad_call", detail=err)
        c["args"] = args
        kf = c.get("keys_from")
        if len(_items(args.get(_filter_key(c["tool"])))) + (1 if kf else 0) > MAX_FILTERS:
            return Clarify("too_many_filters")
        for tool, a in ((c["tool"], args),) + (((kf["tool"], kf["args"]),) if kf else ()):
            p = _term_plan(work.terms, tool, a, work.view)
            if p.reject is not None:
                return Clarify("term_reject", value=p.reject[0], detail=p.reject[1])
    return None


def _contact_fields(work: _Work) -> tuple[str, ...]:
    reader = getattr(work.view, "is_contact", None)
    if reader is None:
        return ()
    out = []
    for c in work.calls:
        if c["tool"] != "list_records":
            continue
        obj = work.obj(c)
        for f in c["args"].get("fields") or []:
            try:
                if obj is not None and reader(obj, f):
                    out.append(f"{obj}.{f}")
            except Exception:  # noqa: BLE001
                continue
    return tuple(dict.fromkeys(out))


# ------------------------------------------------------------------------------------------------------- running
def _dates(phrase: Any, today: dt.date, grain: Any = None) -> tuple[str | None, str | None] | None:
    """(first day, last day) the period words mean on ``today`` (ISO), or None when they do not parse. ``grain``: a
    trend's grain; a trend ends its window with the bucket of today (``RecordQueryMixin._trend_records``), so its last
    day is clipped the same way."""
    try:
        per = parse_period(str(phrase or ""), today)
    except PeriodError:
        return None
    end_x = per.end_exclusive
    if grain in GRAINS and per.start is not None and per.start <= today:
        end_x = min(end_x, bucket_end(grain, today)) if end_x else bucket_end(grain, today)
    start = per.start.isoformat() if per.start else None
    end = (end_x - dt.timedelta(days=1)).isoformat() if end_x else None
    return start, end


def _trend_dates(info: Any, grain: str, today: dt.date) -> Any:
    """The stored dates of a call that becomes a trend, with its last day clipped as the trend clips it (so a trend
    made on the same day reads the same dates and keeps no dates by mistake)."""
    if not isinstance(info, dict) or not info.get("from") or info["from"] > today.isoformat():
        return info
    last = (bucket_end(grain, today) - dt.timedelta(days=1)).isoformat()
    return dict(info, to=min(info["to"], last) if info.get("to") else last)


def _absolute(first: str | None, last: str | None) -> str:
    """The stored dates as period words the record tools read (registry.py ``PERIOD``)."""
    if first and last:
        return f"{first} to {last}"
    return f"since {first}" if first else f"until {last}"


def _keep_dates(args: dict, entry: dict, tool: str, today: dt.date) -> bool:
    """D1: when a stored relative period now means other dates, ``args`` gets the stored dates as an absolute range
    (top level, and each compare segment). True when it did (the answer gets a one-line note)."""
    kept = False
    info = entry.get("period")
    phrase = args.get("period")
    if isinstance(phrase, str) and isinstance(info, dict) and info.get("phrase") == phrase \
            and (info.get("from") or info.get("to")):
        now = _dates(phrase, today, args.get("grain") if tool == "trend_records" else None)
        if now is not None and now != (info.get("from"), info.get("to")):
            args["period"] = _absolute(info.get("from"), info.get("to"))
            kept = True
    if tool == "compare_records":
        stored = entry.get("segments") if isinstance(entry.get("segments"), list) else []
        for i, seg in enumerate(_items(args.get("segments"))):
            s = stored[i] if i < len(stored) and isinstance(stored[i], dict) else {}
            if isinstance(seg.get("period"), str) and s.get("phrase") == seg["period"] and (s.get("from")
                                                                                          or s.get("to")):
                now = _dates(seg["period"], today)
                if now is not None and now != (s.get("from"), s.get("to")):
                    seg["period"] = _absolute(s.get("from"), s.get("to"))
                    kept = True
    return kept


@dataclass(frozen=True)
class RunCall:
    """One call to send through ``RecordTools.call`` for a stored or patched plan (§7.1, §7.4).

    ``args``: what to send (the stored dates kept, D1), without the filter of a record set; ``entry``: the plan call
    as stored (``build`` keeps its model form); ``unchanged``: it runs as stored (the C7 check applies); ``kept_dates``:
    a stored relative period now means other dates and the stored ones are used (a note); ``keys_from`` and
    ``set_args``: the set to re-run fresh first and its arguments (:func:`keys_filter` turns its keys into the filter);
    ``set_kept_dates``: the set call kept its stored dates; ``expect_rows``: the stored call found rows (C7)."""

    index: int
    role: str
    tool: str
    args: dict
    entry: dict
    unchanged: bool
    kept_dates: bool
    keys_from: dict | None = None
    set_args: dict | None = None
    set_kept_dates: bool = False
    expect_rows: bool = False


def run_calls(plan: Any, today: dt.date, touched: Iterable[int] = ()) -> list[RunCall]:
    """The calls of ``plan`` (a plan, a state, or a :class:`Patched` plan) to send, in plan order. ``touched``: the
    indexes an operation changed (``Patched.touched``); the others are unchanged stored calls."""
    if isinstance(plan, Patched):
        plan, touched = plan.plan, plan.touched
    if isinstance(plan, Checked):
        plan = plan.plan
    plan = _plan_of(plan)
    if not plan:
        return []
    changed = frozenset(touched or ())
    out = []
    for i, c in enumerate(plan.get("calls") or []):
        args = copy.deepcopy(c["args"])
        kept = _keep_dates(args, c, c["tool"], today)
        kf = copy.deepcopy(c.get("keys_from")) if c.get("keys_from") else None
        set_args, set_kept = None, False
        if kf:
            set_args = copy.deepcopy(kf["args"])
            set_kept = _keep_dates(set_args, kf, kf["tool"], today)
        out.append(RunCall(index=i, role=c.get("role", "side"), tool=c["tool"], args=args, entry=copy.deepcopy(c),
                           unchanged=i not in changed, kept_dates=kept, keys_from=kf, set_args=set_args,
                           set_kept_dates=set_kept, expect_rows=bool(c.get("nonempty")) and i not in changed))
    return out


def keys_filter(keys_from: dict, keys: Iterable[Any]) -> tuple[dict | None, str]:
    """(filter item, status) of a record set's fresh keys (§10): an ``in`` filter on the set's field (at most
    :data:`MAX_SET_KEYS` keys), or for an ordinal drill (``rank``) an ``eq`` filter on that key. Status: ``ok``;
    ``changed`` (the keys differ from the ones shown: the answer says so); ``rank_changed`` (an ordinal drill into a
    ranking that changed: no filter, ask, D3); ``empty`` (no key: no filter); ``namesakes`` (a grouping's key is one
    of several records that share a name, "Name (#2)": no filter, ask; until the grouping returns the lookup Ids, §10).
    """
    got = [str(k) for k in keys or () if k is not None and str(k).strip()]
    digest = keys_from.get("digest")
    changed = bool(digest) and keys_digest(got) != digest
    rank = keys_from.get("rank")
    chosen = got[rank - 1: rank] if rank else got[:MAX_SET_KEYS]
    if keys_from.get("tool", "group_records") == "group_records" and any(_NAMESAKE_RE.search(k) for k in chosen):
        return None, "namesakes"
    if rank:
        if changed:
            return None, "rank_changed"
        if len(got) < rank:
            return None, "empty"
        return {"field": keys_from["field"], "op": "eq", "value": got[rank - 1]}, "ok"
    if not got:
        return None, "empty"
    return {"field": keys_from["field"], "op": "in", "values": got[:MAX_SET_KEYS]}, "changed" if changed else "ok"


def with_item(tool: str, args: dict, item: dict) -> dict:
    """A copy of ``args`` with one more AND filter item (the base filters of a percentage)."""
    out = copy.deepcopy(args)
    key = _filter_key(tool)
    out[key] = _items(out.get(key)) + [dict(item)]
    return out


def stale_empty(run: RunCall, evidence: Any) -> bool:
    """C7: an unchanged stored call that found rows before ran fine and found none now (the answer then says the data
    may have changed, instead of reading as a plain zero)."""
    return bool(run.unchanged and run.expect_rows and getattr(evidence, "ok", False)
                and not nonempty(getattr(evidence, "tool", ""), getattr(evidence, "model_result", None)))


# ---------------------------------------------------------------------------------------------------- the plan line
#: The words of the plan line, one ``key: text`` line each (``<<NAME>>`` filled by code): prompts/plan_line_words.md
#: (moved there as they were, stage-1 review point N1).
PLAN_LINE_WORDS = _prompt_files.text("plan_line_words")
_WORDS: dict[str, str] = dict(ln.split(": ", 1) for ln in PLAN_LINE_WORDS.split("\n") if ": " in ln)
#: How a filter writes each operator (term_contracts ``_OP_TEXT``, the applied texts' own signs).
_OP_TEXT = {"eq": "=", "ne": "<>", "in": "in", "not_in": "not in", "contains": "contains", "starts_with": "starts with",
            "not_contains": "does not contain", "is_blank": "is blank", "is_not_blank": "is not blank", "gt": ">",
            "gte": ">=", "lt": "<", "lte": "<="}


def _w(key: str, **fill: Any) -> str:
    return _prompt_files.fill(_WORDS[key], **{k: str(v) for k, v in fill.items()})


def _pretty(text: Any) -> str:
    """An API name or path in words: no ``__c``, underscores and inner capitals read as spaces."""
    parts = []
    for seg in str(text or "").split("."):
        s = re.sub(r"__(?:c|r)$", "", seg).replace("_", " ")
        s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)
        parts.append(" ".join(s.split()))
    return " ".join(p for p in parts if p)


def _label(label: Callable | None, obj: Any, path: Any = None) -> str | None:
    if label is None:
        return None
    try:
        got = label(obj, path)
    except Exception:  # noqa: BLE001 - the words fall back to the API names
        return None
    return got if isinstance(got, str) and got.strip() else None


def _obj_word(obj: Any, label: Callable | None) -> str:
    return _label(label, obj) or _w("records", OBJ=_pretty(obj))


def _field_word(obj: Any, path: Any, label: Callable | None) -> str:
    """A field path in words: its last field's label; the name or Id of a linked record reads as the lookup that
    links it ("Department__c.Name" is "Department", step-5 fix: never a bare "Name")."""
    parts = str(path or "").split(".")
    if len(parts) > 1 and parts[-1].casefold() in ("name", "id"):
        head = ".".join(parts[:-1])
        got = _label(label, obj, head)
        # a lookup with no label of its own (its API name, "RecordTypeId") reads as the path in words ("Record Type")
        return got if got and (" " in got or _pretty(got) == got) else _pretty(head)
    return _label(label, obj, path) or _pretty(path)


def _value_text(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (int, float)):
        return str(v)
    return f"'{v}'"


def _item_text(it: dict, obj: Any, label: Callable | None) -> str:
    op = str(it.get("op") or "")
    name = _field_word(obj, it.get("field"), label)
    if op in ("is_blank", "is_not_blank"):
        return f"{name} {_OP_TEXT[op]}"
    if it.get("other_field"):
        shown = _field_word(obj, it["other_field"], label)
    elif op in ("in", "not_in"):
        shown = "(" + ", ".join(_value_text(v) for v in it.get("values") or []) + ")"
    else:
        shown = _value_text(it.get("value"))
    return f"{name} {_OP_TEXT.get(op, op)} {shown}"


def _items_text(items: list[dict], obj: Any, label: Callable | None, joiner: str) -> str:
    return f" {_w(joiner)} ".join(_item_text(it, obj, label) for it in items)


def _range_text(first: Any, last: Any) -> str:
    if first and last:
        return _w("range", FROM=first, TO=last)
    return _w("since", FROM=first) if first else _w("until", TO=last) if last else ""


def _period_text(phrase: Any, info: Any, today: dt.date | None, grain: Any = None) -> str:
    """The period words with their dates: the stored dates while the words are the stored ones, else the dates the
    words mean on ``today`` (when given; a trend's ``grain`` clips them as the trend does)."""
    if not isinstance(phrase, str) or not phrase.strip():
        return ""
    span = None
    if isinstance(info, dict) and info.get("phrase") == phrase and (info.get("from") or info.get("to")):
        span = (info.get("from"), info.get("to"))
    elif today is not None:
        span = _dates(phrase, today, grain)
    if not span or not (span[0] or span[1]):
        return phrase
    rng = _range_text(*span)
    return rng if re.search(r"\d{4}-\d{2}", phrase) else _w("period", PHRASE=phrase, RANGE=rng)


def _measure_text(tool: str, a: dict, obj: Any, label: Callable | None) -> str:
    measure, mfield = _measured(a)
    ow = _obj_word(obj, label)
    if measure == "count" or not measure:
        return _w("count", OBJ=ow)
    if measure == "count_distinct":
        return _w("distinct", FIELD=_field_word(obj, mfield, label), OBJ=ow)
    op = _WORDS.get(f"op_{measure}", measure)
    return _w("measure", OP=op, FIELD=_field_word(obj, mfield, label), OBJ=ow)


def _call_text(call: dict, today: dt.date | None, label: Callable | None) -> str:
    """One call in words: what it measures, its grouping, filters, period, ranking and record set."""
    tool, a = call["tool"], call["args"]
    obj = a.get("object")
    head = ""
    parts: list[str] = []
    if tool in ("count_records", "aggregate_records"):
        head = _measure_text(tool, a, obj, label)
    elif tool == "group_records":
        head = _w("group", MEASURE=_measure_text(tool, a, obj, label), GROUP=_field_word(obj, a.get("group_by"), label))
    elif tool == "trend_records":
        if a.get("part_filters"):
            head = _w("rate", OBJ=_obj_word(obj, label), PART=_items_text(_items(a["part_filters"]), obj, label,
                                                                          "and"))
            head = f"{head}, {_w('by', GRAIN=a.get('grain'))}"
        else:
            head = _w("trend", MEASURE=_measure_text(tool, a, obj, label), GRAIN=a.get("grain"))
    elif tool == "compare_records":
        stored = call.get("segments") if isinstance(call.get("segments"), list) else []
        segs = []
        for i, s in enumerate(_items(a.get("segments"))):
            info = stored[i] if i < len(stored) and isinstance(stored[i], dict) else None
            text = str(s.get("label") or "")
            when = _period_text(s.get("period"), info, today)
            if when and when != text:   # a label that is the period's own words is said once, with the dates
                same = isinstance(s.get("period"), str) and _tokens(text) == _tokens(s["period"])
                text = f"{text} ({when})" if text and not same else when
            if _items(s.get("filters")):
                text = f"{text} {_w('where', FILTERS=_items_text(_items(s['filters']), obj, label, 'and'))}"
            segs.append(text.strip())
        head = _w("compare", MEASURE=_measure_text(tool, a, obj, label), SEGMENTS=f" {_w('versus')} ".join(segs))
    elif tool == "percentage_of_records":
        head = _w("rate", OBJ=_obj_word(obj, label), PART=_items_text(_items(a.get("part_filters")), obj, label, "and"))
    elif tool == "list_records":
        head = _w("list", OBJ=_obj_word(obj, label))
    parts.append(head)
    if a.get("split_by"):
        parts.append(_w("split", FIELD=_field_word(obj, a["split_by"], label)))
    filters = _items(a.get(_filter_key(tool)))
    if filters:
        parts.append(_w("where", FILTERS=_items_text(filters, obj, label, "and")))
    if _items(a.get("any_of")):
        parts.append(_w("any_of", FILTERS=_items_text(_items(a["any_of"]), obj, label, "or")))
    for r in _items(a.get("related")):
        parts.append(_w("with_child" if r.get("exists", True) else "without_child",
                        CHILD=_obj_word(r.get("child_object"), label)))
    when = _period_text(a.get("period"), call.get("period"), today,
                        a.get("grain") if tool == "trend_records" else None)
    if when:
        parts.append(when)
    if tool == "group_records" and a.get("top"):
        parts.append(_w("bottom" if a.get("order") == "asc" else "top", N=a["top"]))
    if tool == "group_records" and a.get("min_value") is not None:
        parts.append(_w("min_value", N=_value_text(a["min_value"]).strip("'")))
    if tool == "list_records":
        if a.get("order_by"):
            parts.append(_w("sorted", FIELD=_field_word(obj, a["order_by"], label), ORDER=a.get("order") or "desc"))
        if a.get("limit"):
            parts.append(_w("limit", N=a["limit"]))
    kf = call.get("keys_from")
    if isinstance(kf, dict):
        ka = kf.get("args") or {}
        if kf.get("tool") == "group_records":
            what = _w("set_top", N=ka.get("top") or "", FIELD=_field_word(ka.get("object"), ka.get("group_by"), label))
        else:
            what = _w("set_list", OBJ=_obj_word(ka.get("object"), label))
        parts.append(_w("set_rank", N=kf["rank"], SET=what) if kf.get("rank") else _w("set", SET=what))
    return ", ".join(p for p in parts if p)


def _type_filtered(call: dict) -> bool:
    """The call filters on a record type or the person-account flag itself (its own scope, shown in its filters)."""
    a = call.get("args") or {}
    for key in ("filters", "base_filters", "any_of"):
        for it in _items(a.get(key)):
            if any(p.lower() in ("recordtypeid", "recordtype", "ispersonaccount")
                   for p in str(it.get("field") or "").split(".")):
                return True
    return False


def _adds_candidates(scope: Terms, call: dict, view: Any) -> bool:
    """The candidates contract adds its filter to ``call`` under ``scope`` (``term_contracts.plan``, pure: what code
    adds when the call runs)."""
    got = _term_plan(scope, call["tool"], call["args"], view)
    return any(str(item.get("field") or "").split(".")[-1] in ("RecordTypeId", "IsPersonAccount")
               for _key, item in got.add)


def _scope_word(scope: Terms, main: dict, view: Any) -> str | None:
    """The scope words of the main call from the filters that run (step-5 fix W2), or None when the view cannot read
    them (the scope is then read from the terms alone, as before). "candidates as Person Accounts" only when the call
    keeps only candidates (code adds the candidate filter, or the call's own filter keeps only Person Accounts);
    "every account type" only when the person widened a call the contract would scope; nothing for a call the
    candidates contract does not touch (a count of trainings in a question that says "candidates")."""
    try:
        narrow = dataclasses.replace(scope, all_types=False, other_accounts=False)
        kf = main.get("keys_from") if isinstance(main.get("keys_from"), dict) else None
        # a call that runs on a record set of an earlier answer is scoped as the set's own call is (its fresh re-run)
        calls = [main] + ([{"tool": kf.get("tool"), "args": kf.get("args")}] if kf and isinstance(kf.get("args"), dict)
                          else [])
        for call in calls:
            own = [state for _i, _pre, state in _scope_items(view, view.object(call["args"].get("object")),
                                                              _items(call["args"].get(_filter_key(call["tool"]))))]
            if not (_adds_candidates(narrow, call, view) or "present" in own):
                continue
            if scope.all_types:
                return _w("all_types") if "present" not in own else ""
            if _adds_candidates(scope, call, view) or ("present" in own and "conflict" not in own):
                return _w("candidates")
            return ""
        return ""
    except Exception:  # noqa: BLE001 - a view that cannot read it: the terms decide
        return None


def plan_line(state_or_plan: Any, *, today: dt.date | None = None, label: Callable | None = None,
              terms: Terms | None = None, view: Any = None) -> str:
    """The plan in code-written words (C20), at most :data:`MAX_PLAN_LINE` characters; "" without a plan. The main
    call first, then the term scope and the display, then each side call ("also ..."). ``today``: the dates of period
    words that have not run yet; ``label(object, path)``: display words for objects and fields (the API names in
    words otherwise). The only figures it holds are dates, limits and the plan's own argument values.

    ``terms``: the terms the plan runs with (:func:`terms_for`; a patch or shortcut passes them, so the Read-as line
    states the scope that ran). Without them the scope is read from the plan's own stored terms. The candidates scope
    is written only when the run keeps it: "every account type" when the person asked for every account type, and
    nothing when they named other account types and the call filters on the record type itself (stage-1 review fix
    M2).

    ``view``: the catalog view (:class:`PlanView`); with it the scope is written from the filters the call runs with
    (:func:`_scope_word`, step-5 fix W2), so a plan the contract does not scope never reads "candidates as Person
    Accounts"."""
    plan = _plan_of(state_or_plan)
    calls = [c for c in (plan or {}).get("calls") or [] if isinstance(c, dict) and isinstance(c.get("args"), dict)]
    if not calls:
        return ""
    main = next((c for c in calls if c.get("role") == "main"), calls[0])
    parts = [_call_text(main, today, label)]
    scope = terms if isinstance(terms, Terms) else terms_for(plan.get("terms"), "", "", "patch")
    word = _scope_word(scope, main, view) if view is not None and "candidates" in scope.keys else None
    if word is not None:
        parts.append(word)
    elif "candidates" in scope.keys:
        if scope.all_types:
            parts.append(_w("all_types"))
        elif not (scope.other_accounts and _type_filtered(main)):
            parts.append(_w("candidates"))
    show = clean_show(plan.get("show"))
    if show["chart"]:
        parts.append(_w("chart_type", TYPE=show["type"].replace("_", " ")) if show["type"] else _w("chart"))
    if show["table"]:
        parts.append(_w("table"))
    line = ", ".join(p for p in parts if p)
    for side in calls:
        if side is not main:
            line += "; " + _w("also", CALL=_call_text(side, today, label))
    return _cut(line, MAX_PLAN_LINE)


# ------------------------------------------------------------------------------------------------- the catalog view
class PlanView(_CatalogView):
    """The term contracts' view over the record tools' catalog (``tools/term_enforce.py``) and what the patch checks
    read besides: a field's kind, the path of a date field (temporal keys included), contact and blocked fields, a
    field's picklist values, the lookups from one object to another, a lookup's target and display labels. Reads the
    catalog in memory only."""

    def _ref(self, obj: str, path_text: Any) -> Any:
        spec = self.cat.objects.get(obj)
        if spec is None or not isinstance(path_text, str) or not path_text.strip():
            return None
        try:
            return self.cat.resolve_path(spec, path_text)
        except Exception:  # noqa: BLE001 - not resolved
            return None

    def date_path(self, obj: str, name: Any) -> str | None:
        """The path of the date field ``name`` means on ``obj`` (an API name, created / modified, a temporal key of
        the knowledge), as ``RecordTools._date_ref`` reads it; None when it is no date field."""
        spec = self.cat.objects.get(obj)
        raw = str(name or "").strip()
        low = raw.lower()
        if spec is None or not raw:
            return None
        if low in ("created", "created date", "creation date", "created_at"):
            raw = "CreatedDate"
        elif low in ("modified", "updated", "last modified", "last_modified", "updated_at", "last updated"):
            raw = "LastModifiedDate"
        if spec.knowledge is not None:
            for key, v in (spec.knowledge.temporal_semantics() or {}).items():
                if isinstance(v, dict) and key.lower() == low and v.get("field") and v["field"] in spec.fields:
                    raw = str(v["field"])
                    break
        ref = self._ref(obj, raw)
        return ref.text if ref is not None and ref.field.is_date else None

    def field_kind(self, obj: str, path_text: Any) -> str | None:
        ref = self._ref(obj, path_text)
        if ref is not None:
            return ref.field.kind
        return "date" if self.date_path(obj, path_text) else None

    def is_contact(self, obj: str, path_text: Any) -> bool:
        ref = self._ref(obj, path_text)
        return bool(ref is not None and ref.field.sensitivity == "contact")

    def is_blocked(self, obj: str, path_text: Any) -> bool:
        """The path's last field is blocked (the record tools refuse it, E2c)."""
        ref = self._ref(obj, path_text)
        return bool(ref is not None and ref.field.sensitivity == "blocked")

    def picklist_values(self, obj: str, path_text: Any) -> list[str]:
        """The picklist values and labels of the path's last field (schema data of the knowledge, no record data)."""
        ref = self._ref(obj, path_text)
        if ref is None:
            return []
        return [str(x) for value, lab, _meaning in ref.field.picklist for x in (value, lab) if x]

    def lookups(self, obj: str, target: str) -> list[str]:
        """The lookups of ``obj`` whose one target is ``target`` (the system lookups left out), by API name."""
        spec = self.cat.objects.get(obj)
        if spec is None:
            return []
        return sorted(f.api for f in spec.fields.values() if f.kind == "reference" and f.api not in SYSTEM_LOOKUPS
                      and self.cat.target_of(f) == target)

    def target(self, obj: str, path_text: Any) -> str | None:
        ref = self._ref(obj, path_text)
        return self.cat.target_of(ref.field) if ref is not None and ref.field.kind == "reference" else None

    def fields_of(self, obj: Any) -> list[tuple[str, str, str]]:
        """(API name, label, kind) of the fields of ``obj`` a question may name (step-5 fix, field grounding): no
        blocked or contact field and no legacy or deprecated one; the most useful kinds to filter and group by first
        (picklists, true/false, lookups, dates, numbers), then fields with data, then by API name. Schema only: no
        value of any record."""
        name = self.object(obj)
        spec = self.cat.objects.get(name) if name else None
        if spec is None:
            return []
        order = {"picklist": 0, "multipicklist": 0, "boolean": 1, "reference": 2, "date": 3, "datetime": 3}
        keep = [f for f in spec.fields.values()
                if f.sensitivity not in ("blocked", "contact") and not f.legacy and not f.deprecated]
        keep.sort(key=lambda f: (order.get(f.kind, 5 if f.kind in _NUMBER_KINDS else 6), not f.has_data,
                                 not f.documented, f.api))
        return [(f.api, f.label or f.api, f.kind) for f in keep]

    def label(self, obj: Any, path_text: Any = None) -> str | None:
        """The plural label of an object, or the label of a field path's last field."""
        name = self.object(obj)
        spec = self.cat.objects.get(name) if name else None
        if spec is None:
            return None
        if path_text is None:
            return spec.plural_label or spec.label or None
        ref = self._ref(name, path_text)
        return ref.field.label if ref is not None and ref.field.label else None
