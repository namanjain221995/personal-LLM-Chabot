"""Pure SQL builders (no I/O): fragments with bound parameters, FROM/JOIN scopes, filters, periods,
child conditions and the statement templates of RECORDS_SPEC §11.6.

Every builder returns a :class:`Frag` whose ``?`` placeholders and ``params`` stay in the same order.
Identifiers come only from the catalog and go through :func:`okrec.guard.quote_ident`. Lookup paths
are LEFT JOINs (many-to-one, never multiplying rows); child records are reached only through
``EXISTS`` or count subqueries. Canonical text: single spaces, no newlines, no trailing spaces.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from dataclasses import field as _dc_field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .catalog import Catalog, FieldSpec, Hop, ObjectSpec, PathRef, RecordError, blocked_text, norm_name
from .sql_guard import quote_ident
from .periods import Period, PeriodError, column_bounds, local_midnight_utc, parse_period
from .values import escape_like, ground_value, id18, looks_like_id, normalize, parse_bool

__all__ = ["Frag", "join_frags", "Scope", "Col", "BuildCtx", "FilterOut", "NameCheck", "filter_frag", "period_frag",
           "related_frag", "where_sql", "count_sql", "aggregate_sql", "group_sql", "trend_sql",
           "percentage_sql", "list_sql", "MAX_JOINS", "GRAINS", "measure_expr", "group_measure_expr", "quote_value",
           "name_norm_sql", "NUMBERED_RE", "NAME_OPS", "POSITIVE_OPS", "CONTAINS_OPS"]

MAX_JOINS = 6
GRAINS = {"day": "day", "week": "week", "month": "month", "quarter": "quarter", "year": "year"}
STRING_KINDS = ("text", "name", "id", "picklist", "longtext")
NUMBER_KINDS = ("integer", "number", "percent", "currency")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_TEXT = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*$", re.IGNORECASE)
#: A value that picks one of several records with the same name: "Recruiter B (#2)" (records numbered in Id order).
NUMBERED_RE = re.compile(r"^(?P<name>.+?)\s*\(#(?P<k>\d+)\)$")
NAME_OPS = ("eq", "ne", "in", "not_in", "contains", "starts_with", "not_contains")
POSITIVE_OPS = ("eq", "in", "contains", "starts_with")
CONTAINS_OPS = ("contains", "starts_with", "not_contains")


def name_norm_sql(expr: str) -> str:
    """Normalised name in SQL (L9): non-breaking spaces as spaces, whitespace runs as one space, trimmed, lower case.
    The Python twin is :func:`okrec.catalog.norm_name`."""
    return f"lower(trim(regexp_replace(replace({expr}, chr(160), ' '), '\\s+', ' ', 'g')))"


@dataclass(frozen=True)
class Frag:
    """SQL text plus its bound parameters, in order."""

    sql: str
    params: tuple = ()

    def __add__(self, other: "Frag") -> "Frag":
        return Frag(self.sql + other.sql, self.params + other.params)


def join_frags(sep: str, frags: Iterable[Frag]) -> Frag:
    """Join fragments with ``sep``; text and params keep the same order."""
    items = [f for f in frags if f is not None and f.sql]
    return Frag(sep.join(f.sql for f in items), tuple(p for f in items for p in f.params))


def quote_value(v: Any) -> str:
    """Readable value for "applied" texts: strings quoted, numbers/dates/booleans bare."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float, Decimal)):
        return _num_text(v)
    if isinstance(v, (dt.date, dt.datetime, dt.time)):
        return v.isoformat()
    return "'" + str(v) + "'"


def _num_text(v: Any) -> str:
    if isinstance(v, Decimal):
        t = format(v.normalize(), "f")
        return t
    return str(v)


@dataclass
class BuildCtx:
    """What builders need besides the catalog: business time zone and today.

    ``names(objects, normalised_name)`` returns ``[(object, Id, stored name, detail)]`` in Id order (it resolves
    ``'Name (#k)'`` values; the record tools supply it). ``person_names(value, contains)`` returns the person
    objects with a record of that name (note N10). Both are optional: without them pure builds stay pure.
    ``contact_ok``: the question asks for contact details (phase 1, ``sf_assistant.engine.contact``); a filter on a
    contact field then shows its value (False, the default: "<value hidden>").
    """

    catalog: Catalog
    tz: str
    today: dt.date
    names: Any = None
    person_names: Any = None
    contact_ok: bool = False


@dataclass
class Col:
    """A column expression in a scope."""

    expr: str                    # value expression (for a reference: the stored Id)
    ref: PathRef
    label_expr: str | None = None   # reference: the related record's Name
    id_expr: str | None = None      # reference: the related record's Id
    blank_case: str | None = None   # reference: CASE ... '(blank)' / '(unresolved id)' ... END

    @property
    def field(self) -> FieldSpec:
        return self.ref.field

    @property
    def display(self) -> str:
        return self.ref.text

    @property
    def multi(self) -> bool:
        """A multi-select picklist: grouped once per selected value, not per stored combination."""
        return self.field.kind == "multipicklist" and self.label_expr is None


class Scope:
    """FROM clause of one statement: the base table plus LEFT JOINs added by lookup paths."""

    def __init__(self, catalog: Catalog, spec: ObjectSpec, alias: str = "t0", join_prefix: str = "t") -> None:
        self.catalog = catalog
        self.spec = spec
        self.alias = alias
        self.join_prefix = join_prefix
        self.joins: list[str] = []
        self.tables: list[str] = [spec.api]
        self._aliases: dict[tuple, Any] = {}
        self._n = 0

    def _next(self) -> str:
        self._n += 1
        return f"{self.join_prefix}{self._n}"

    def _join(self, src_alias: str, hop: Hop, key: tuple) -> Any:
        """Alias of the hop's target (a pair (user, group) for OwnerId).

        ``key`` is the path so far as ``((field, target), ...)``: the target is part of it because one
        lookup can be joined two ways (``OwnerId`` as a label joins User and Group, ``OwnerId.IsActive``
        joins User only), and each way needs its own alias.
        """
        if key in self._aliases:
            return self._aliases[key]
        col = f"{src_alias}.{quote_ident(hop.field.api)}"
        if hop.target == "User|Group":
            if len(self.joins) + 2 > MAX_JOINS:
                raise RecordError("a statement can join at most 6 related objects; use fewer lookup paths.", "E3")
            n = self._next()
            au, ag = f"{n}u", f"{n}g"
            self.joins.append(f"LEFT JOIN main.\"User\" AS {au} ON {au}.\"Id\" = {col}")
            self.joins.append(f"LEFT JOIN main.\"Group\" AS {ag} ON {ag}.\"Id\" = {col}")
            for t in ("User", "Group"):
                if t not in self.tables:
                    self.tables.append(t)
            self._aliases[key] = (au, ag)
            return self._aliases[key]
        if len(self.joins) + 1 > MAX_JOINS:
            raise RecordError("a statement can join at most 6 related objects; use fewer lookup paths.", "E3")
        a = self._next()
        self.joins.append(f"LEFT JOIN main.{quote_ident(hop.target)} AS {a} ON {a}.\"Id\" = {col}")
        if hop.target not in self.tables:
            self.tables.append(hop.target)
        self._aliases[key] = a
        return a

    def _walk(self, hops: list[Hop]) -> tuple[str | tuple, list[tuple[str, str, Any]]]:
        """Join every hop; returns the last alias and (src_alias, field, target_alias) per hop."""
        src: Any = self.alias
        chain: list[tuple[str, str, Any]] = []
        key: tuple = ()
        for hop in hops:
            key = key + ((hop.field.api, hop.target),)
            tgt = self._join(src, hop, key)
            chain.append((src, hop.field.api, tgt))
            src = tgt
        return src, chain

    def col(self, ref: PathRef, *, label: bool = False) -> Col:
        """Column for ``ref``; with ``label`` a reference also joins its target for the Name."""
        last, chain = self._walk(ref.hops)
        if isinstance(last, tuple):  # OwnerId.Name
            au, ag = last
            src, fld, _ = chain[-1]
            expr = f"COALESCE({au}.\"Name\", {ag}.\"Name\")"
            blank = _blank_case(chain[:-1] + [(src, fld, (au, ag))])
            return Col(expr, ref, label_expr=expr, id_expr=f"COALESCE({au}.\"Id\", {ag}.\"Id\")", blank_case=blank)
        expr = f"{last}.{quote_ident(ref.field.api)}"
        if ref.field.kind != "reference" or not label:
            return Col(expr, ref)
        f = ref.field
        if f.api == "OwnerId":
            hop = Hop(ref.obj, f, "User|Group")
        else:
            targets = [t for t in f.ref_targets if t in self.catalog.objects and self.catalog.objects[t].has_table]
            if len(targets) != 1:
                raise RecordError(f"{ref.obj}.{f.api} can point to several objects ({', '.join(f.ref_targets)}); "
                                  "use it with an Id value, or OwnerId.Name for owners.", "E3d")
            hop = Hop(ref.obj, f, targets[0])
        key = tuple((h.field.api, h.target) for h in ref.hops) + ((f.api, hop.target),)
        tgt = self._join(last, hop, key)
        full = chain + [(last, f.api, tgt)]
        if isinstance(tgt, tuple):
            au, ag = tgt
            return Col(expr, ref, label_expr=f"COALESCE({au}.\"Name\", {ag}.\"Name\")",
                       id_expr=f"COALESCE({au}.\"Id\", {ag}.\"Id\")", blank_case=_blank_case(full))
        name_col = "Name" if "Name" in self.catalog.objects[hop.target].fields else "Id"
        return Col(expr, ref, label_expr=f"{tgt}.{quote_ident(name_col)}", id_expr=f"{tgt}.\"Id\"",
                   blank_case=_blank_case(full))

    def from_sql(self) -> str:
        base = f"main.{quote_ident(self.spec.api)} AS {self.alias}"
        return " ".join([base] + self.joins)


def _blank_case(chain: list[tuple[str, str, Any]]) -> str:
    parts = []
    for src, fld, tgt in chain:
        idx = f"COALESCE({tgt[0]}.\"Id\", {tgt[1]}.\"Id\")" if isinstance(tgt, tuple) else f"{tgt}.\"Id\""
        parts.append(f"WHEN {src}.{quote_ident(fld)} IS NULL THEN '(blank)' WHEN {idx} IS NULL THEN '(unresolved id)'")
    return "CASE " + " ".join(parts) + " ELSE '(no name)' END"


# ------------------------------------------------------------------------------------- filters

@dataclass(eq=False)
class NameCheck:
    """A lookup (or a base ``Name``) compared by name or Id; the record tools ground it before the main statement."""

    target: str                     # object(s) the names are looked up in; "User|Group" for OwnerId
    names: list[str]                # values as given (stripped), without the 'Name (#k)' ones
    shown: str                      # the filter's field as applied (e.g. "Recruiter__c.Name")
    op: str = "eq"
    field: FieldSpec | None = None  # the lookup; None for a Name filter on the base object
    obj: str = ""                   # object that holds the lookup (or the base object for a Name filter)
    on_base: bool = False           # the lookup is a field of the base object
    frag: Frag | None = None        # this filter's own condition (compared by identity)
    pattern: str | None = None      # ILIKE pattern of contains / starts_with / not_contains
    ids: list[str] = _dc_field(default_factory=list)       # Id values given directly
    picked: dict[str, tuple] = _dc_field(default_factory=dict)  # 'Name (#k)' value -> (object, Id, name, detail)
    raw: list[str] = _dc_field(default_factory=list)       # every value as given
    # filled during grounding
    found: dict[str, list[tuple[str, str, str | None]]] = _dc_field(default_factory=dict)  # object -> (Id, name, detail)
    missing: list[str] = _dc_field(default_factory=list)
    kind: str = "other"

    @property
    def targets(self) -> list[str]:
        return self.target.split("|")

    @property
    def positive(self) -> bool:
        return self.op in POSITIVE_OPS


@dataclass
class FilterOut:
    frag: Frag
    text: str
    notes: list[str] = field(default_factory=list)
    ref: PathRef | None = None
    checks: list[NameCheck] = field(default_factory=list)


def _parse_number(obj: str, f: FieldSpec, v: Any) -> Decimal:
    if isinstance(v, bool):
        raise RecordError(f"{obj}.{f.api} is a number field; '{v}' is not a number.", "E4c")
    s = str(v).strip().replace(",", "")
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        d = Decimal(s)
        if not d.is_finite():
            raise InvalidOperation
        return d
    except (InvalidOperation, ValueError):
        raise RecordError(f"{obj}.{f.api} is a number field; '{v}' is not a number.", "E4c") from None


def _parse_date(ctx: BuildCtx, f: FieldSpec, shown: str, v: Any) -> dt.date:
    s = str(v).strip()
    if _ISO_DATE.match(s):
        try:
            return dt.date.fromisoformat(s)
        except ValueError:
            pass
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]", s):
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            pass
    try:
        parse_period(s, ctx.today)
        is_phrase = True
    except PeriodError:
        is_phrase = False
    if is_phrase:
        raise RecordError(f"{shown} is a date field: do not put '{s}' in filters. Use date_field='{shown}' with "
                          f"period='{s}'.", "E4b")
    raise RecordError(f"'{s}' is not a date; write YYYY-MM-DD, or use date_field + period for phrases.", "E4g")


def _parse_time(f: FieldSpec, v: Any) -> dt.time:
    m = _TIME_TEXT.match(str(v))
    if not m:
        raise RecordError(f"'{v}' is not a time; write HH:MM (24 h) or 08:30 AM.", "E4g")
    h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    ap = (m.group(4) or "").lower().replace(".", "")
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    try:
        return dt.time(h, mi, se)
    except ValueError:
        raise RecordError(f"'{v}' is not a time; write HH:MM (24 h) or 08:30 AM.", "E4g") from None


def _ops_for(kind: str) -> list[str]:
    if kind in ("text", "name", "id", "picklist"):
        return ["eq", "ne", "in", "not_in", "contains", "not_contains", "starts_with", "is_blank", "is_not_blank"]
    if kind == "longtext":
        return ["eq", "ne", "contains", "not_contains", "starts_with", "is_blank", "is_not_blank"]
    if kind == "multipicklist":
        return ["eq", "ne", "in", "not_in", "contains", "not_contains", "is_blank", "is_not_blank"]
    if kind == "boolean":
        return ["eq", "ne", "is_blank", "is_not_blank"]
    if kind in NUMBER_KINDS or kind in ("date", "datetime", "time"):
        return ["eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "is_blank", "is_not_blank"]
    if kind == "reference":
        return ["eq", "ne", "in", "not_in", "contains", "not_contains", "starts_with", "is_blank", "is_not_blank"]
    return ["is_blank", "is_not_blank"]


_CMP = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}


def filter_frag(scope: Scope, item: dict, ctx: BuildCtx) -> FilterOut:
    """One filter item -> SQL condition, readable text and notes. Raises :class:`RecordError`."""
    cat = ctx.catalog
    ref = cat.resolve_path(scope.spec, item.get("field"))
    f = ref.field
    obj = ref.obj
    shown = ref.text
    op = item["op"]
    if f.sensitivity == "blocked":
        raise RecordError(blocked_text(obj, f.api), "E2c")
    base = cat.objects.get(scope.spec.api)
    if base is not None and base.sensitive and not ref.hops and not (
            f.kind in ("picklist", "boolean", "date", "datetime") or f.kind in NUMBER_KINDS or f.system):
        raise RecordError(sensitive_object_text(base.api), "E1d")
    hide = f.sensitivity == "contact" and not ctx.contact_ok
    notes: list[str] = list(ref.notes)
    if item.get("other_field") and op not in ("is_blank", "is_not_blank"):
        return _other_field(scope, ref, scope.col(ref).expr, op, item["other_field"], ctx, notes)
    if op not in _ops_for(f.kind):
        raise RecordError(f"op '{op}' does not work on {f.kind} field {shown}; use one of: {', '.join(_ops_for(f.kind))}.",
                          "E4f")
    if op in ("is_blank", "is_not_blank"):
        c = scope.col(ref).expr
        sql = f"{c} IS NULL" if op == "is_blank" else f"{c} IS NOT NULL"
        return FilterOut(Frag(sql), f"{shown} is {'blank' if op == 'is_blank' else 'not blank'}", notes, ref)
    values = item.get("values") if op in ("in", "not_in") else None
    value = item.get("value")
    if op in ("in", "not_in"):
        if not values:
            raise RecordError(f"filter on {shown} with op {op} needs 'values': a list such as [\"A\", \"B\"].", "E11c")
    elif value is None or (isinstance(value, str) and not value.strip() and op not in ("eq", "ne")):
        raise RecordError(f"filter on {shown} with op {op} needs 'value'.", "E11c")
    shown_v = (lambda v: "<value hidden>") if hide else quote_value

    # ---------------------------------------------------------------- names through a lookup (F.Name, A.B.Name)
    if ref.hops and f.api == "Name" and op in NAME_OPS and ref.hops[-1].field.api != "RecordTypeId":
        last = ref.hops[-1]
        lref = PathRef(".".join(ref.text.split(".")[:-1]) or last.field.api, ref.hops[:-1], last.src, last.field,
                       list(ref.notes))
        out = _reference_filter(scope, lref, scope.col(lref).expr, op, value, values, ctx, notes)
        out.ref = ref
        return out
    c = scope.col(ref).expr
    if not ref.hops and f.api == "Name" and op in ("eq", "ne", "in", "not_in") and f.kind in STRING_KINDS:
        return _base_name_filter(scope, ref, c, op, value, values, ctx, notes)

    # ---------------------------------------------------------------- booleans
    if f.kind == "boolean":
        b = parse_bool(value)
        if b is None:
            raise RecordError(f"{obj}.{f.api} is a checkbox; use true or false (also yes/no, checked/unchecked).", "E4d")
        if op == "eq":
            return FilterOut(Frag(f"{c} = ?", (b,)), f"{shown} = {quote_value(b)}", notes, ref)
        return FilterOut(Frag(f"{c} IS DISTINCT FROM ?", (b,)), f"{shown} <> {quote_value(b)} (blank included)", notes, ref)

    # ---------------------------------------------------------------- numbers
    if f.kind in NUMBER_KINDS:
        if f.kind == "percent":
            notes.append(f"{shown} is a percent stored as 0-100")
        if op in ("in", "not_in"):
            nums = [_parse_number(obj, f, v) for v in values]
            txt = ", ".join(_num_text(n) for n in nums)
            if op == "in":
                return FilterOut(Frag(f"{c} = ANY(?)", (nums,)), f"{shown} in ({txt})", notes, ref)
            return FilterOut(Frag(f"({c} IS NULL OR NOT ({c} = ANY(?)))", (nums,)),
                             f"{shown} not in ({txt}) (blank included)", notes, ref)
        n = _parse_number(obj, f, value)
        if op == "ne":
            return FilterOut(Frag(f"({c} IS NULL OR {c} <> ?)", (n,)), f"{shown} <> {_num_text(n)} (blank included)", notes, ref)
        return FilterOut(Frag(f"{c} {_CMP[op]} ?", (n,)), f"{shown} {_CMP[op]} {_num_text(n)}", notes, ref)

    # ---------------------------------------------------------------- dates
    if f.kind == "date":
        if op in ("in", "not_in"):
            ds = [_parse_date(ctx, f, shown, v) for v in values]
            txt = ", ".join(d.isoformat() for d in ds)
            if op == "in":
                return FilterOut(Frag(f"{c} = ANY(?)", (ds,)), f"{shown} in ({txt})", notes, ref)
            return FilterOut(Frag(f"({c} IS NULL OR NOT ({c} = ANY(?)))", (ds,)), f"{shown} not in ({txt}) (blank included)",
                             notes, ref)
        d = _parse_date(ctx, f, shown, value)
        if op == "ne":
            return FilterOut(Frag(f"({c} IS NULL OR {c} <> ?)", (d,)), f"{shown} <> {d.isoformat()} (blank included)", notes, ref)
        return FilterOut(Frag(f"{c} {_CMP[op]} ?", (d,)), f"{shown} {_CMP[op]} {d.isoformat()}", notes, ref)
    if f.kind == "datetime":
        if op in ("in", "not_in"):
            raise RecordError(f"op '{op}' does not work on datetime field {shown}; use one of: eq, ne, gt, gte, lt, lte, "
                              "is_blank, is_not_blank (or date_field + period).", "E4f")
        d = _parse_date(ctx, f, shown, value)
        lo, hi = local_midnight_utc(d, ctx.tz), local_midnight_utc(d + dt.timedelta(days=1), ctx.tz)
        tzn = f" ({ctx.tz})"
        if op == "eq":
            return FilterOut(Frag(f"{c} >= ? AND {c} < ?", (lo, hi)), f"{shown} on {d.isoformat()}{tzn}", notes, ref)
        if op == "ne":
            return FilterOut(Frag(f"({c} IS NULL OR {c} < ? OR {c} >= ?)", (lo, hi)),
                             f"{shown} not on {d.isoformat()}{tzn} (blank included)", notes, ref)
        bound, sym = {"gt": (hi, ">="), "gte": (lo, ">="), "lt": (lo, "<"), "lte": (hi, "<")}[op]
        return FilterOut(Frag(f"{c} {sym} ?", (bound,)), f"{shown} {_CMP[op]} {d.isoformat()}{tzn}", notes, ref)
    if f.kind == "time":
        if op in ("in", "not_in"):
            ts = [_parse_time(f, v) for v in values]
            sql = f"{c} = ANY(?)" if op == "in" else f"({c} IS NULL OR NOT ({c} = ANY(?)))"
            return FilterOut(Frag(sql, (ts,)), f"{shown} {op.replace('_', ' ')} ({', '.join(t.isoformat() for t in ts)})",
                             notes, ref)
        t = _parse_time(f, value)
        if op == "ne":
            return FilterOut(Frag(f"({c} IS NULL OR {c} <> ?)", (t,)), f"{shown} <> {t.isoformat()} (blank included)", notes, ref)
        return FilterOut(Frag(f"{c} {_CMP[op]} ?", (t,)), f"{shown} {_CMP[op]} {t.isoformat()}", notes, ref)

    # ---------------------------------------------------------------- references
    if f.kind == "reference":
        return _reference_filter(scope, ref, c, op, value, values, ctx, notes)

    # ---------------------------------------------------------------- multipicklist
    if f.kind == "multipicklist":
        split = f"string_split({c}, ';')"
        if op in ("contains", "not_contains", "eq", "ne"):
            g = ground_value(cat, obj, f, value, shown)
            vals = g.values if g else [str(value)]
            if g:
                notes += g.notes
            fn = f"list_contains({split}, ?)" if len(vals) == 1 else f"list_has_any({split}, ?)"
            param = vals[0] if len(vals) == 1 else vals
            if op in ("contains", "eq"):
                return FilterOut(Frag(fn, (param,)), f"{shown} includes {quote_value(g.shown if g else value)}", notes, ref)
            return FilterOut(Frag(f"({c} IS NULL OR NOT {fn})", (param,)),
                             f"{shown} excludes {quote_value(g.shown if g else value)} (blank included)", notes, ref)
        bound: list[str] = []
        for v in values:
            g = ground_value(cat, obj, f, v, shown)
            bound += g.values if g else [str(v)]
            if g:
                notes += g.notes
        txt = ", ".join(quote_value(v) for v in bound)
        if op == "in":
            return FilterOut(Frag(f"list_has_any({split}, ?)", (bound,)), f"{shown} includes any of ({txt})", notes, ref)
        return FilterOut(Frag(f"({c} IS NULL OR NOT list_has_any({split}, ?))", (bound,)),
                         f"{shown} includes none of ({txt}) (blank included)", notes, ref)

    # ---------------------------------------------------------------- strings (picklist, text, name, id)
    person = _person_text(ctx, ref, f, op, value, values, hide)
    if person:
        notes.append(person)
    if op in ("contains", "not_contains", "starts_with"):
        pat = (f"%{escape_like(str(value))}%" if op != "starts_with" else f"{escape_like(str(value))}%")
        if op == "contains":
            return FilterOut(Frag(f"{c} ILIKE ? ESCAPE '\\'", (pat,)), f"{shown} contains {shown_v(value)}", notes, ref)
        if op == "starts_with":
            return FilterOut(Frag(f"{c} ILIKE ? ESCAPE '\\'", (pat,)), f"{shown} starts with {shown_v(value)}", notes, ref)
        return FilterOut(Frag(f"({c} IS NULL OR {c} NOT ILIKE ? ESCAPE '\\')", (pat,)),
                         f"{shown} does not contain {shown_v(value)}", notes, ref)
    if f.kind == "id":   # the warehouse stores 18-character Ids; a 15-character one is converted
        value = id18(value) if looks_like_id(value) else value
        values = [id18(v) if looks_like_id(v) else v for v in values] if values else values
    if op in ("eq", "ne"):
        g = None if hide or f.kind in ("longtext", "id") or person else ground_value(cat, obj, f, value, shown)
        entry = cat.values_of(obj, f.api)
        if g is None:
            if op == "eq":
                return FilterOut(Frag(f"lower({c}) = lower(?)", (str(value),)), f"{shown} = {shown_v(value)}", notes, ref)
            return FilterOut(Frag(f"({c} IS NULL OR lower({c}) <> lower(?))", (str(value),)),
                             f"{shown} <> {shown_v(value)} (blank included)", notes, ref)
        notes += g.notes
        if op == "eq":
            if len(g.values) == 1:
                return FilterOut(Frag(f"{c} = ?", (g.values[0],)), f"{shown} = {quote_value(g.values[0])}", notes, ref)
            return FilterOut(Frag(f"{c} = ANY(?)", (list(g.values),)),
                             f"{shown} in ({', '.join(quote_value(v) for v in g.values)})", notes, ref)
        if ref.hops:
            notes.append(f"records whose {shown} is blank, including records with no {ref.hops[0].field.api}, are "
                         "included by <>")
        elif entry is not None and entry.blank:
            notes.append(f"records with a blank {shown} are included by <> ({entry.blank} of all {obj} records have a "
                         f"blank {shown})")
        if len(g.values) == 1:
            return FilterOut(Frag(f"({c} IS NULL OR {c} <> ?)", (g.values[0],)),
                             f"{shown} <> {quote_value(g.values[0])} (blank included)", notes, ref)
        return FilterOut(Frag(f"({c} IS NULL OR NOT ({c} = ANY(?)))", (list(g.values),)),
                         f"{shown} not in ({', '.join(quote_value(v) for v in g.values)}) (blank included)", notes, ref)
    # in / not_in
    bound = []
    lowered = False
    for v in values:
        g = None if hide or f.kind == "id" or person else ground_value(cat, obj, f, v, shown)
        if g is None:
            bound.append(str(v).lower())
            lowered = True
        else:
            bound += g.values
            notes += g.notes
    bound = list(dict.fromkeys(bound))
    expr = f"lower({c})" if lowered else c
    if lowered:
        bound = [b.lower() for b in bound]
    txt = ", ".join("<value hidden>" if hide else quote_value(v) for v in bound)
    if op == "in":
        return FilterOut(Frag(f"{expr} = ANY(?)", (bound,)), f"{shown} in ({txt})", notes, ref)
    return FilterOut(Frag(f"({c} IS NULL OR NOT ({expr} = ANY(?)))", (bound,)), f"{shown} not in ({txt}) (blank included)",
                     notes, ref)


def _reference_filter(scope: Scope, ref: PathRef, c: str, op: str, value: Any, values: list | None, ctx: BuildCtx,
                      notes: list[str]) -> FilterOut:
    cat = ctx.catalog
    f = ref.field
    shown = ref.text
    vals = [value] if op not in ("in", "not_in") else list(values or [])
    # Record types by name.
    if f.api == "RecordTypeId":
        spec = cat.objects[ref.obj]
        if op in ("contains", "not_contains", "starts_with"):
            raise RecordError(f"op '{op}' does not work on RecordTypeId; use eq or in with a record type name.", "E4f")
        ids: list[str] = []
        names: list[str] = []
        for v in vals:
            if looks_like_id(v):
                ids.append(str(v))
                names.append(str(v))
                continue
            nv = normalize(v)
            hit = [r for r in spec.record_types if r.get("id") and nv in (normalize(r.get("name")), normalize(r.get("developer_name")))]
            if not hit:
                listed = ", ".join(f"{r['name']} ({r.get('developer_name')}) {r['records']}" for r in spec.record_types) or "none"
                raise RecordError(f"'{v}' is not a record type of {ref.obj}. Record types (records): {listed}. Use one of "
                                  "them exactly.", "E4")
            ids += [r["id"] for r in hit]
            names.append(str(hit[0]["name"]))
        txt = ", ".join(quote_value(n) for n in names)
        pos = op in ("eq", "in")
        sql = f"{c} = ANY(?)" if pos else f"({c} IS NULL OR NOT ({c} = ANY(?)))"
        label = (f"{shown} = {txt}" if op == "eq" else f"{shown} in ({txt})" if op == "in"
                 else f"{shown} <> {txt} (blank included)" if op == "ne" else f"{shown} not in ({txt}) (blank included)")
        return FilterOut(Frag(sql, (ids,)), label, notes, ref)
    # Ids.
    if f.api == "OwnerId":
        targets = [t for t in ("User", "Group") if t in cat.objects and cat.objects[t].has_table] or ["User", "Group"]
    else:
        targets = [t for t in f.ref_targets if t in cat.objects and cat.objects[t].has_table]
    if vals and all(looks_like_id(v) for v in vals) and op in ("eq", "ne", "in", "not_in"):
        prefixes = {cat.objects[t].id_prefix for t in f.ref_targets if t in cat.objects}
        if all(str(v)[:3] in prefixes for v in vals) or not prefixes - {None}:
            ids = [id18(v) for v in vals]   # the warehouse stores 18-character Ids; a 15-character one is converted
            txt = ", ".join(quote_value(i) for i in ids)
            chk = NameCheck("|".join(targets) or (f.ref_targets[0] if f.ref_targets else ""), [], shown, op, f, ref.obj,
                            not ref.hops, ids=list(ids), raw=list(ids))
            if op == "eq":
                frag, text = Frag(f"{c} = ?", (ids[0],)), f"{shown} = {txt}"
            elif op == "ne":
                frag, text = Frag(f"({c} IS NULL OR {c} <> ?)", (ids[0],)), f"{shown} <> {txt} (blank included)"
            elif op == "in":
                frag, text = Frag(f"{c} = ANY(?)", (ids,)), f"{shown} in ({txt})"
            else:
                frag, text = Frag(f"({c} IS NULL OR NOT ({c} = ANY(?)))", (ids,)), f"{shown} not in ({txt}) (blank included)"
            chk.frag = frag
            return FilterOut(frag, text, notes, ref, [chk] if targets else [])
    # Names.
    if f.api != "OwnerId" and len(targets) != 1:
        raise RecordError(f"{ref.obj}.{f.api} can point to several objects ({', '.join(f.ref_targets)}); use it with "
                          "an Id value, or OwnerId.Name for owners.", "E3d")
    for t in targets:
        if "Name" not in cat.objects[t].fields:
            raise RecordError(f"{t} has no Name; compare {shown} with an Id value.", "E3")
    disp = f"{shown}.Name" if not shown.endswith(".Name") else shown
    chk = NameCheck("|".join(targets), [], disp, op, f, ref.obj, not ref.hops)
    if op in CONTAINS_OPS:
        pat = f"%{escape_like(str(value))}%" if op != "starts_with" else f"{escape_like(str(value))}%"
        subs = [Frag(f"{c} IN (SELECT x.\"Id\" FROM main.{quote_ident(t)} AS x WHERE x.\"Name\" ILIKE ? ESCAPE '\\')", (pat,))
                for t in targets]
        inner = join_frags(" OR ", subs)
        word = {"contains": "contains", "starts_with": "starts with", "not_contains": "does not contain"}[op]
        text = f"{shown} name {word} {quote_value(value)}"
        chk.names, chk.raw, chk.pattern = [str(value)], [str(value)], pat
        if op == "not_contains":
            frag = Frag(f"({c} IS NULL OR NOT ({inner.sql}))", inner.params)
        else:
            frag = Frag(inner.sql if len(subs) == 1 else f"({inner.sql})", inner.params)
        chk.frag = frag
        return FilterOut(frag, text, notes, ref, [chk])
    raw = [str(v).strip() for v in vals]
    ids, plain = _split_numbered(ctx, targets, raw, chk)
    norms = list(dict.fromkeys(norm_name(v) for v in plain))
    parts: list[Frag] = []
    if ids:
        parts.append(Frag(f"{c} = ?", (ids[0],)) if len(ids) == 1 else Frag(f"{c} = ANY(?)", (ids,)))
    if norms:
        nsql = name_norm_sql('x."Name"')
        for t in targets:
            if op in ("eq", "ne") and len(norms) == 1:
                parts.append(Frag(f"{c} IN (SELECT x.\"Id\" FROM main.{quote_ident(t)} AS x WHERE {nsql} = ?)", (norms[0],)))
            else:
                parts.append(Frag(f"{c} IN (SELECT x.\"Id\" FROM main.{quote_ident(t)} AS x WHERE {nsql} = ANY(?))",
                                  (list(norms),)))
    inner = join_frags(" OR ", parts)
    txt = ", ".join(quote_value(n) for n in raw)
    chk.names, chk.raw = plain, raw
    if op in ("eq", "in"):
        frag = Frag(inner.sql if len(parts) == 1 else f"({inner.sql})", inner.params)
        text = f"{disp} = {txt}" if op == "eq" else f"{disp} in ({txt})"
    else:
        frag = Frag(f"({c} IS NULL OR NOT ({inner.sql}))", inner.params)
        text = f"{disp} <> {txt} (blank included)" if op == "ne" else f"{disp} not in ({txt}) (blank included)"
    chk.frag = frag
    return FilterOut(frag, text, notes, ref, [chk])


def _split_numbered(ctx: BuildCtx, targets: list[str], raw: list[str], chk: NameCheck) -> tuple[list[str], list[str]]:
    """Ids of the 'Name (#k)' values (resolved by code, in Id order) and the other values."""
    ids: list[str] = []
    plain: list[str] = []
    for v in raw:
        m = NUMBERED_RE.match(v)
        if not m:
            plain.append(v)
            continue
        name, k = m["name"].strip(), int(m["k"])
        tl = ctx.catalog.target_text("|".join(targets))
        if ctx.names is None:
            raise RecordError(f"'{v}' picks one of several records named '{name}'; filter with '{name}' instead.", "E4h")
        rows = list(ctx.names(targets, norm_name(name)))
        if not rows:
            raise RecordError(f"no {tl} record is named '{name}'. Filter with a name exactly as listed, or call "
                              f"search_records(name='{name}').", "E4h")
        if k < 1 or k > len(rows):
            listed = ", ".join(f"#{i} {r[3] or r[2]}" for i, r in enumerate(rows, 1))
            raise RecordError(f"'{name}' has {len(rows)} {tl} records ({listed}); there is no #{k}.", "E4h")
        pick = rows[k - 1]
        chk.picked[v] = tuple(pick)
        ids.append(str(pick[1]))
    return ids, plain


def _base_name_filter(scope: Scope, ref: PathRef, c: str, op: str, value: Any, values: list | None, ctx: BuildCtx,
                      notes: list[str]) -> FilterOut:
    """``Name`` of the base object with eq, ne, in or not_in: compared normalised (L9), with a NameCheck."""
    obj = ref.obj
    vals = [value] if op in ("eq", "ne") else list(values or [])
    raw = [str(v).strip() for v in vals]
    chk = NameCheck(obj, [], ref.text, op, None, obj, False)
    ids, plain = _split_numbered(ctx, [obj], raw, chk)
    norms = list(dict.fromkeys(norm_name(v) for v in plain))
    parts: list[Frag] = []
    if ids:
        idc = f"{scope.alias}.\"Id\""
        parts.append(Frag(f"{idc} = ?", (ids[0],)) if len(ids) == 1 else Frag(f"{idc} = ANY(?)", (ids,)))
    if norms:
        n = name_norm_sql(c)
        parts.append(Frag(f"{n} = ?", (norms[0],)) if op in ("eq", "ne") and len(norms) == 1
                     else Frag(f"{n} = ANY(?)", (list(norms),)))
    inner = join_frags(" OR ", parts)
    chk.names, chk.raw = plain, raw
    txt = ", ".join(quote_value(v) for v in raw)
    if op in ("eq", "in"):
        frag = Frag(inner.sql if len(parts) == 1 else f"({inner.sql})", inner.params)
        text = f"{ref.text} = {txt}" if op == "eq" else f"{ref.text} in ({txt})"
    else:
        frag = Frag(f"({c} IS NULL OR NOT ({inner.sql}))", inner.params)
        text = f"{ref.text} <> {txt} (blank included)" if op == "ne" else f"{ref.text} not in ({txt}) (blank included)"
    chk.frag = frag
    return FilterOut(frag, text, notes, ref, [chk])


def _person_text(ctx: BuildCtx, ref: PathRef, f: FieldSpec, op: str, value: Any, values: list | None,
                 hide: bool) -> str | None:
    """Note N10: a text field compared with a person's name (from the record tools' name index; no SQL)."""
    if ctx.person_names is None or hide or f.kind not in ("text", "longtext") or f.api == "Name" or \
            op not in ("eq", "in", "contains", "starts_with"):
        return None
    cat = ctx.catalog
    vals = list(values or []) if op == "in" else [value]
    for v in vals:
        objs = list(ctx.person_names(str(v), op in ("contains", "starts_with")) or [])
        if not objs:
            continue
        o = objs[0]
        spec = cat.objects.get(ref.obj)
        looks = (cat.person_lookups(spec).get(o) or []) if spec is not None else []
        looks = [g for g in looks if g.filled > 0][:4]
        meaning = " ".join(str(f.meaning or "").split())
        meaning = meaning if len(meaning) <= 80 else meaning[:79].rstrip() + "…"
        text = (f"'{v}' is the name of {cat.target_text(o)} record(s), but {ref.obj}.{f.api} is a text field "
                f"({meaning}), not a link to them; ")
        if looks:
            text += f"lookups of {ref.obj} to {cat.label_of(o)}: " + ", ".join(f"{g.api} ({g.role or g.role_text()})"
                                                                          for g in looks)
        else:
            text += f"{ref.obj} has no lookup to {cat.label_of(o)} with data"
        return text
    return None


def _other_field(scope: Scope, ref: PathRef, c: str, op: str, other: Any, ctx: BuildCtx, notes: list[str]) -> FilterOut:
    if op not in _CMP:
        raise RecordError(f"other_field works with eq, ne, gt, gte, lt, lte; not '{op}'.", "E4f")
    oref = ctx.catalog.resolve_path(scope.spec, other)
    if oref.field.sensitivity == "blocked":
        raise RecordError(blocked_text(oref.obj, oref.field.api), "E2c")
    g = scope.col(oref).expr
    a, b = ref.field.kind, oref.field.kind
    sym = _CMP[op]
    num = NUMBER_KINDS
    params: tuple = ()
    if a == b and a in ("date", "datetime", "time"):
        left, right = c, g
    elif {a, b} == {"date", "datetime"}:
        # date/times are stored in UTC; the calendar day is the business time zone's, as for periods (D6)
        local_day = "CAST(({} AT TIME ZONE 'UTC') AT TIME ZONE ? AS DATE)"
        left = local_day.format(c) if a == "datetime" else c
        right = local_day.format(g) if b == "datetime" else g
        params = (ctx.tz,)
        notes.append(f"date/time compared with a date as calendar days in {ctx.tz}")
    elif a in num and b in num:
        left, right = c, g
    elif a in ("text", "picklist") and b in ("text", "picklist") and "time" in ref.field.api.lower() and \
            "time" in oref.field.api.lower():
        left, right = f"try_strptime({c}, '%I:%M %p')", f"try_strptime({g}, '%I:%M %p')"
        notes.append("text times compared as clock times (hh:mm AM/PM)")
    elif a == b and a in ("text", "picklist", "reference", "boolean", "name") and op in ("eq", "ne"):
        left, right = c, g
    else:
        raise RecordError(f"{ref.text} ({a}) and {oref.text} ({b}) cannot be compared.", "E4e")
    if op == "ne":
        return FilterOut(Frag(f"{left} IS DISTINCT FROM {right}", params), f"{ref.text} <> {oref.text}", notes, ref)
    return FilterOut(Frag(f"{left} {sym} {right}", params), f"{ref.text} {sym} {oref.text}", notes, ref)


def sensitive_object_text(obj: str) -> str:
    return (f"{obj} holds portal credentials or sessions; its records cannot be listed, searched or shown, and its "
            "history is not available. Counting, grouping by picklist/checkbox/date fields and trends are allowed.")


# ------------------------------------------------------------------------------------- periods

def period_frag(scope: Scope, date_ref: PathRef, period: Period, ctx: BuildCtx) -> tuple[Frag, dict]:
    """``c >= ? AND c < ?`` for a period on a date or datetime field, plus the "applied" dict."""
    f = date_ref.field
    if not f.is_date:
        raise RecordError(f"{date_ref.text} is {f.kind}, not a date or date/time field. Date fields of "
                          f"{scope.spec.api}: {date_fields_text(scope.spec)}.", "E5e")
    c = scope.col(date_ref).expr
    lo, hi = column_bounds(period, f.kind, ctx.tz)
    parts: list[Frag] = []
    if lo is not None:
        parts.append(Frag(f"{c} >= ?", (lo,)))
    if hi is not None:
        parts.append(Frag(f"{c} < ?", (hi,)))
    # "to" = last day inside the window, so answers can state "Sep 1 to Sep 30" without date arithmetic.
    applied: dict[str, Any] = {"date_field": date_ref.text, "phrase": period.label,
                               "from": period.start.isoformat() if period.start else None,
                               "to": (period.end_exclusive - dt.timedelta(days=1)).isoformat()
                               if period.end_exclusive else None,
                               "to_exclusive": period.end_exclusive.isoformat() if period.end_exclusive else None,
                               "time_zone": ctx.tz}
    if f.kind == "datetime":
        applied["utc_from"] = lo.strftime("%Y-%m-%d %H:%M") if lo is not None else None
        applied["utc_to_exclusive"] = hi.strftime("%Y-%m-%d %H:%M") if hi is not None else None
    return join_frags(" AND ", parts), applied


def date_fields_text(spec: ObjectSpec, n: int = 8) -> str:
    out = []
    for f in spec.date_fields()[:n]:
        if f.api == "CreatedDate":
            out.append("CreatedDate (record created)")
        elif f.api == "LastModifiedDate":
            out.append("LastModifiedDate (last changed)")
        else:
            key = f.temporal_keys[0][0] if f.temporal_keys else f.kind
            out.append(f"{f.api} ({key}: {f.meaning[:80]})")
    return ", ".join(out) or "none"


# ------------------------------------------------------------------------------------- related

def related_frag(scope: Scope, item: dict, n: int, ctx: BuildCtx,
                 resolve_date: Any = None) -> tuple[Frag, str, list[str], list[FilterOut]]:
    """EXISTS / NOT EXISTS / count-subquery condition on child records of ``scope.spec``; also the child filters'
    outputs, whose name checks the record tools ground."""
    cat = ctx.catalog
    parent = scope.spec
    child = cat.resolve_object(item.get("child_object"))
    notes: list[str] = []
    cands = [f for f in child.fields.values() if f.kind == "reference" and parent.api in f.ref_targets
             and f.sensitivity != "blocked"]
    # The model sometimes names a PARENT of the object as child_object (via_field = the object's own lookup to it):
    # say that it is a parent and how to filter through it, instead of "<child> has no field <via_field>".
    ups = [f for f in parent.fields.values() if f.kind == "reference" and child.api in f.ref_targets
           and f.sensitivity != "blocked" and f.api not in ("CreatedById", "LastModifiedById", "OwnerId")]
    via_raw = str(item.get("via_field") or "").strip().lower()
    via_is_up = bool(via_raw) and any(f.api.lower() == via_raw for f in ups) and \
        not any(a.lower() == via_raw for a in child.fields)
    if ups and (not cands or via_is_up):
        up = next((f.api for f in ups if f.api.lower() == via_raw), ups[0].api)
        raise RecordError(f"{child.api} is not a child of {parent.api}: {parent.api}.{up} points to it, so it is a "
                          f"parent. related only takes child objects (records that point to {parent.api}). To use a "
                          f"field of the parent, filter or group {parent.api} by the lookup path, e.g. "
                          f"filters=[{{\"field\": \"{up}.<field of {child.api}>\", \"op\": \"eq\", \"value\": "
                          f"\"...\"}}] or group_by \"{up}.<field of {child.api}>\".", "E9c")
    if item.get("via_field"):
        via = cat.field(child, item["via_field"])
        if via not in cands:
            listed = ", ".join(f.api for f in cands) or "none"
            raise RecordError(f"{child.api}.{via.api} does not point to {parent.api}. Lookups on {child.api} to "
                              f"{parent.api}: {listed}.", "E9")
    elif len(cands) == 1:
        via = cands[0]
    elif not cands:
        kids = ", ".join(f"{c.child} (via {c.via})" for c in parent.children[:15]) or "none"
        raise RecordError(f"{child.api} has no lookup to {parent.api}. Children of {parent.api}: {kids}", "E9")
    else:
        raise RecordError(f"{child.api} has {len(cands)} lookups to {parent.api}: {', '.join(f.api for f in cands)}; "
                          "set via_field.", "E9b")
    alias = f"c{n}"
    sub = Scope(cat, child, alias=alias, join_prefix=f"{alias}t")
    conds: list[Frag] = [Frag(f"{alias}.{quote_ident(via.api)} = {scope.alias}.\"Id\"")]
    texts: list[str] = []
    outs: list[FilterOut] = []
    for i, fi in enumerate(item.get("filters") or []):
        out = filter_frag(sub, fi, ctx)
        conds.append(out.frag)
        texts.append(out.text)
        notes += out.notes
        outs.append(out)
    if item.get("period"):
        if not item.get("date_field"):
            raise RecordError(f"related period needs date_field. Date fields of {child.api}: {date_fields_text(child)}.", "E5c")
        dref = resolve_date(child, item["date_field"]) if resolve_date else cat.resolve_path(child, item["date_field"])
        try:
            per = parse_period(item["period"], ctx.today)
        except PeriodError as exc:
            raise RecordError(str(exc), exc.code) from None
        pf, pa = period_frag(sub, dref, per, ctx)
        conds.append(pf)
        texts.append(f"{pa['date_field']} in [{pa['from'] or '…'}, {pa['to_exclusive'] or '…'}) ({per.label})")
    where = join_frags(" AND ", conds)
    body = Frag(f"FROM {sub.from_sql()} WHERE {where.sql}", where.params)
    for t in sub.tables:
        if t not in scope.tables:
            scope.tables.append(t)
    cond_txt = f" with {' and '.join(texts)}" if texts else ""
    path = f"(via {child.api}.{via.api})"
    exists = item.get("exists", True)
    min_count = item.get("min_count")
    if min_count is not None and not exists:
        raise RecordError("min_count needs exists=true (at least N matching child records).", "E11d")
    if min_count is not None and int(min_count) <= 1:
        if int(min_count) < 1:
            raise RecordError("min_count must be at least 1. For 'has at least one' omit min_count (exists=true); for "
                              "'has none' use exists=false.", "E11d")
        notes.append("min_count 1 read as exists=true")
        min_count = None
    if min_count is not None:
        frag = Frag(f"(SELECT COUNT(*) {body.sql}) >= ?", body.params + (int(min_count),))
        return frag, f"has at least {int(min_count)} {child.api}{cond_txt} {path}", notes, outs
    if exists:
        return Frag(f"EXISTS (SELECT 1 {body.sql})", body.params), f"has a {child.api}{cond_txt} {path}", notes, outs
    return (Frag(f"NOT EXISTS (SELECT 1 {body.sql})", body.params), f"has no {child.api}{cond_txt} {path}", notes,
            outs)


# ------------------------------------------------------------------------------------- statements

def where_sql(conds: list[Frag]) -> Frag:
    w = join_frags(" AND ", conds)
    return Frag(f" WHERE {w.sql}", w.params) if w.sql else Frag("")


def count_sql(scope: Scope, conds: list[Frag], distinct: Col | None = None) -> Frag:
    sel = f"COUNT(DISTINCT {distinct.expr})" if distinct is not None else "COUNT(*)"
    return Frag(f"SELECT {sel} AS n FROM {scope.from_sql()}") + where_sql(conds)


_AGG = {"sum": "SUM", "avg": "AVG", "median": "MEDIAN", "min": "MIN", "max": "MAX"}


def aggregate_sql(scope: Scope, conds: list[Frag], op: str, col: Col) -> Frag:
    return Frag(f"SELECT {_AGG[op]}({col.expr}) AS v, COUNT({col.expr}) AS with_value, COUNT(*) AS matched "
                f"FROM {scope.from_sql()}") + where_sql(conds)


def measure_expr(measure: str, col: Col | None) -> str:
    if measure == "count":
        return "COUNT(*)"
    if measure == "count_distinct":
        return f"COUNT(DISTINCT {col.expr})"
    return f"{_AGG[measure]}({col.expr})"


def label_expr(col: Col) -> str:
    """Group label: the related Name for references, else the value as text; blanks become '(blank)'."""
    if col.label_expr:
        return f"COALESCE({col.label_expr}, {col.blank_case})"
    return f"COALESCE(CAST({col.expr} AS VARCHAR), '(blank)')"


def multi_value_expr(col: Col) -> str:
    """One row per selected value of a multi-select picklist; a blank field gives one '(blank)' row."""
    return f"unnest(COALESCE(string_split({col.expr}, ';'), ['(blank)']))"


def _inner_measure(mcol: Col | None) -> Col | None:
    """The measure column as seen outside the per-value subquery ``s``."""
    return Col("s.m", mcol.ref) if mcol is not None else None


def _per_value(group: Col, split: Col | None) -> bool:
    return group.multi or (split is not None and split.multi)


def group_measure_expr(measure: str, mcol: Col | None, group: Col, split: Col | None = None) -> str:
    """The measure as :func:`group_sql` computes it, for a HAVING clause."""
    return measure_expr(measure, _inner_measure(mcol) if _per_value(group, split) else mcol)


def group_sql(scope: Scope, conds: list[Frag], group: Col, measure: str, mcol: Col | None,
              split: Col | None = None, having: Frag | None = None, order: str = "desc", limit: int = 1001) -> Frag:
    """Grouped measure. A multi-select group or split field is unnested in a subquery ``s`` first, so a record
    is counted once in the group of each value it holds (build ``having`` with :func:`group_measure_expr`)."""
    keys = ["gid"] if group.id_expr else []
    keys.append("grp")
    if split is not None:
        keys.append("split")
    if _per_value(group, split):
        value = lambda c: multi_value_expr(c) if c.multi else label_expr(c)  # noqa: E731
        inner = ([f"{group.id_expr} AS gid"] if group.id_expr else []) + [f"{value(group)} AS grp"]
        if split is not None:
            inner.append(f"{value(split)} AS split")
        if mcol is not None:
            inner.append(f"{mcol.expr} AS m")
        outer = [f"s.{k} AS {k}" for k in keys] + [f"{measure_expr(measure, _inner_measure(mcol))} AS value"]
        head = (Frag(f"SELECT {', '.join(outer)} FROM (SELECT {', '.join(inner)} FROM {scope.from_sql()}")
                + where_sql(conds) + Frag(") AS s"))
    else:
        sel = ([f"{group.id_expr} AS gid"] if group.id_expr else []) + [f"{label_expr(group)} AS grp"]
        if split is not None:
            sel.append(f"{label_expr(split)} AS split")
        sel.append(f"{measure_expr(measure, mcol)} AS value")
        head = Frag(f"SELECT {', '.join(sel)} FROM {scope.from_sql()}") + where_sql(conds)
    tail = f" GROUP BY {', '.join(str(i) for i in range(1, len(keys) + 1))}"
    out = head + Frag(tail)
    if having is not None and having.sql:
        out = out + Frag(f" HAVING {having.sql}", having.params)
    if split is not None:
        out = out + Frag(f" ORDER BY grp ASC, split ASC LIMIT {int(limit)}")
    else:
        direction = "ASC" if order == "asc" else "DESC"
        out = out + Frag(f" ORDER BY value {direction} NULLS LAST, grp ASC LIMIT {int(limit)}")
    return out


def bucket_expr(col: Col, grain: str, ctx: BuildCtx) -> Frag:
    g = GRAINS[grain]
    if col.field.kind == "datetime":
        return Frag(f"CAST(date_trunc('{g}', ({col.expr} AT TIME ZONE 'UTC') AT TIME ZONE ?) AS DATE)", (ctx.tz,))
    return Frag(f"CAST(date_trunc('{g}', {col.expr}) AS DATE)")


def trend_sql(scope: Scope, conds: list[Frag], date_col: Col, grain: str, ctx: BuildCtx, measure: str,
              mcol: Col | None, split: Col | None = None) -> Frag:
    """Measure per bucket (and split value). A multi-select split is unnested per value, as in :func:`group_sql`."""
    b = bucket_expr(date_col, grain, ctx)
    if split is not None and split.multi:
        inner = Frag("SELECT ") + b + Frag(f" AS bucket, {multi_value_expr(split)} AS split"
                                           + (f", {mcol.expr} AS m" if mcol is not None else "")
                                           + f" FROM {scope.from_sql()}")
        return (Frag(f"SELECT s.bucket AS bucket, s.split AS split, {measure_expr(measure, _inner_measure(mcol))} AS "
                     "value FROM (") + inner + where_sql(conds) + Frag(") AS s GROUP BY 1, 2 ORDER BY 1, 2"))
    sel = Frag("SELECT ") + b + Frag(" AS bucket")
    if split is not None:
        sel = sel + Frag(f", {label_expr(split)} AS split")
    sel = sel + Frag(f", {measure_expr(measure, mcol)} AS value FROM {scope.from_sql()}")
    keys = "1, 2" if split is not None else "1"
    return sel + where_sql(conds) + Frag(f" GROUP BY {keys} ORDER BY {keys}")


def percentage_sql(scope: Scope, conds: list[Frag], part: Frag, blank_col: Col | None = None) -> Frag:
    """Part and base counts. With ``blank_col``: base records with a blank ``blank_col`` outside the part
    (``part_field_blank_out``) and inside it (``part_field_blank_in``, e.g. by ne / not_in)."""
    head = Frag("SELECT COUNT(*) FILTER (WHERE ") + part + Frag(") AS part, COUNT(*) AS base")
    if blank_col is not None:
        head = (head + Frag(f", COUNT(*) FILTER (WHERE {blank_col.expr} IS NULL AND (") + part
                + Frag(") IS NOT TRUE) AS part_field_blank_out")
                + Frag(f", COUNT(*) FILTER (WHERE {blank_col.expr} IS NULL AND (") + part + Frag(")) AS part_field_blank_in"))
    return head + Frag(f" FROM {scope.from_sql()}") + where_sql(conds)


def list_sql(scope: Scope, conds: list[Frag], cols: list[tuple[str, str]], order_expr: str, order: str,
             limit: int) -> Frag:
    """``cols`` = [(expression, alias)]; the base Id comes first for the evidence."""
    sel = [f"{scope.alias}.\"Id\""] + [f"{e} AS {quote_ident(a)}" for e, a in cols]
    direction = "ASC" if order == "asc" else "DESC"
    return (Frag(f"SELECT {', '.join(sel)} FROM {scope.from_sql()}") + where_sql(conds) +
            Frag(f" ORDER BY {order_expr} {direction} NULLS LAST, {scope.alias}.\"Id\" LIMIT {int(limit)}"))
