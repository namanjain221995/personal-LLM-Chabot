"""Compile a validated SalesforceQueryPlan into SOQL. The model never writes
query text.

This is the security boundary of Salesforce Intelligence Mode. `engines/sql.py`
and `engines/live_sf.py` let the model author SOQL and then guard the string
afterwards, which is defensible for a read-only integration user but leaves the
model in control of the query's SHAPE. Here the model controls only a
structure: an object name, field names, an operator from an allowlist and a
value. Everything that becomes syntax — the clause order, the quoting, the
escaping, the LIMIT — is written by this module.

What is checked before a single character of SOQL exists:

  - the object exists and is queryable BY THIS CONNECTION (describe, not a
    hardcoded list — an object the integration user cannot see never appears);
  - every field exists on that object and is readable;
  - every relationship traversal is a real parent path, bounded in depth;
  - the operator is in the allowlist and its operand shape matches;
  - aggregates are only combined with GROUP BY in ways SOQL accepts;
  - date operands are real Salesforce date literals or ISO dates;
  - the LIMIT is capped whatever the plan asked for.

A plan that fails any of these raises `PlanRejected` and the caller asks the
user rather than running a query nobody validated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .models import SalesforceQueryPlan, QueryFilter

#: Hard ceiling on rows ONE compiled query may ask for, whatever the plan says.
#: Matches core/salesforce.MAX_ROWS so the two paths cannot disagree.
MAX_LIMIT = 200

#: How deep a parent traversal may go. `Account.Owner.Name` is 2 hops and is
#: the deepest anything in this app has needed; SOQL itself allows 5, and each
#: extra hop is a join the org pays for on a shared production instance.
MAX_RELATIONSHIP_DEPTH = 3

#: Bound on total selected expressions, so a plan cannot ask for 800 columns.
MAX_SELECT_FIELDS = 40

#: Bound on predicate count — a WHERE with 200 clauses is not a question.
MAX_FILTERS = 20

_API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_PATH_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)*$")

#: SOQL relative date literals that take no argument.
_DATE_LITERALS = frozenset(
    """
    YESTERDAY TODAY TOMORROW LAST_WEEK THIS_WEEK NEXT_WEEK LAST_MONTH THIS_MONTH
    NEXT_MONTH LAST_90_DAYS NEXT_90_DAYS THIS_QUARTER LAST_QUARTER NEXT_QUARTER
    THIS_YEAR LAST_YEAR NEXT_YEAR THIS_FISCAL_QUARTER LAST_FISCAL_QUARTER
    NEXT_FISCAL_QUARTER THIS_FISCAL_YEAR LAST_FISCAL_YEAR NEXT_FISCAL_YEAR
    """.split()
)

#: …and the ones that take :n.
_DATE_LITERAL_N_RE = re.compile(
    r"^(LAST_N_DAYS|NEXT_N_DAYS|LAST_N_WEEKS|NEXT_N_WEEKS|LAST_N_MONTHS|"
    r"NEXT_N_MONTHS|LAST_N_QUARTERS|NEXT_N_QUARTERS|LAST_N_YEARS|NEXT_N_YEARS|"
    r"LAST_N_FISCAL_QUARTERS|NEXT_N_FISCAL_QUARTERS|LAST_N_FISCAL_YEARS|"
    r"NEXT_N_FISCAL_YEARS):\d{1,4}$"
)

#: Placeholders a planner reaches for when it wants "me" or "now" and has no
#: literal to put there. SOQL has no equivalent — `UserInfo.getUserId()` is
#: Apex, not SOQL — so quoting one of these produces a syntactically perfect
#: query that matches NOTHING, and reports zero with full confidence.
#:
#: Found live on 2026-08-11: asked for "open opportunities I own", the planner
#: emitted `OwnerId = 'CURRENT_USER_ID'`. This deployment authenticates as a
#: read-only INTEGRATION user, not as the person asking, so there is also no
#: honest value to substitute — resolving it to the integration user's id would
#: answer a different question just as confidently.
#:
#: So it is refused, and the caller falls back to the warehouse engine, which
#: knows how to scope by owner name. A refusal that produces a real answer by
#: another route beats a zero nobody can question.
_UNRESOLVABLE_PLACEHOLDERS = frozenset(
    {
        "current_user_id",
        "current_user",
        "currentuser",
        "$current_user",
        "me",
        "my_user_id",
        "userinfo.getuserid()",
        "current_user_name",
        "today()",
        "now()",
    }
)

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

#: Salesforce field types whose operand is written UNQUOTED.
_NUMERIC_TYPES = frozenset(
    {"int", "integer", "double", "currency", "percent", "long", "number"}
)
_BOOLEAN_TYPES = frozenset({"boolean"})
_DATE_TYPES = frozenset({"date", "datetime"})
_ID_TYPES = frozenset({"reference", "id"})

#: A Salesforce record Id: 15 or 18 case-sensitive alphanumerics.
_SF_ID_RE = re.compile(r"^[a-zA-Z0-9]{15}([a-zA-Z0-9]{3})?$")


class PlanRejected(ValueError):
    """The structured plan did not survive validation. Nothing was executed."""


@dataclass(frozen=True)
class FieldInfo:
    name: str
    type: str = "string"
    label: str = ""
    #: False for fields the connection cannot read. A field the integration user
    #: is not permitted to see must not become part of a query.
    readable: bool = True
    #: For reference fields: the relationship name used in dotted traversals
    #: (`AccountId` → `Account`), and the objects it can point at.
    relationship_name: str = ""
    reference_to: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ObjectSchema:
    """The slice of a describe this module needs. Built by the resolver from
    the live describe API or from the synced warehouse, never hardcoded."""

    name: str
    label: str = ""
    queryable: bool = True
    fields: Mapping[str, FieldInfo] = field(default_factory=dict)

    def field_named(self, name: str) -> Optional[FieldInfo]:
        return self.fields.get(name.lower())

    def relationship(self, name: str) -> Optional[FieldInfo]:
        wanted = name.lower()
        for info in self.fields.values():
            if info.relationship_name and info.relationship_name.lower() == wanted:
                return info
        return None


def build_object_schema(described: Mapping[str, Any]) -> ObjectSchema:
    """Adapt a `core.salesforce.describe_object` payload (or a richer describe).

    Tolerant on purpose: `describe_object` returns a trimmed shape today, and a
    future one that carries `filterable`/`relationshipName` should improve this
    without needing a second adapter.
    """
    fields: Dict[str, FieldInfo] = {}
    for raw in described.get("fields", []) or []:
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        references = raw.get("referenceTo") or raw.get("reference_to") or ()
        fields[name.lower()] = FieldInfo(
            name=name,
            type=str(raw.get("type") or "string").lower(),
            label=str(raw.get("label") or name),
            # An explicit False disables the field; anything else (including a
            # describe that does not carry the key) is treated as readable,
            # because the org itself rejects a truly unreadable field and the
            # integration user is already the real boundary.
            readable=raw.get("accessible") is not False,
            relationship_name=str(
                raw.get("relationshipName") or raw.get("relationship_name") or ""
            ),
            reference_to=tuple(str(r) for r in references),
        )
    return ObjectSchema(
        name=str(described.get("name") or ""),
        label=str(described.get("label") or described.get("name") or ""),
        queryable=described.get("queryable") is not False,
        fields=fields,
    )


# ---------------------------------------------------------------------------
# Literal escaping
# ---------------------------------------------------------------------------

def escape_soql_string(value: str) -> str:
    """Escape a value for a single-quoted SOQL string literal.

    Backslash FIRST — escaping the quote first and the backslash second turns
    `\\'` into `\\\\'`, which closes the literal and is exactly the injection
    this function exists to prevent. Control characters are escaped rather than
    stripped so a value is never silently altered.
    """
    out = value.replace("\\", "\\\\").replace("'", "\\'")
    out = out.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    return out


def _quote(value: str) -> str:
    return f"'{escape_soql_string(value)}'"


def normalize_date_operand(raw: str) -> str:
    """A Salesforce date literal, an ISO date, or a rejection.

    Anything not on this list becomes a QUOTED string instead of a keyword, so
    an unrecognised operand can never inject SOQL — but a date filter written
    as a string matches nothing, silently, which is the worse failure. So a
    date-typed field with an unrecognised operand is refused here.
    """
    text = (raw or "").strip()
    upper = text.upper()
    if upper in _DATE_LITERALS or _DATE_LITERAL_N_RE.match(upper):
        return upper
    if _ISO_DATE_RE.match(text) or _ISO_DATETIME_RE.match(text):
        return text
    raise PlanRejected(
        f"{raw!r} is not a Salesforce date literal (TODAY, THIS_QUARTER, "
        "LAST_N_DAYS:30) or an ISO date (2026-08-11)"
    )


def _boolean_operand(raw: str) -> str:
    text = (raw or "").strip().lower()
    if text in ("true", "1", "yes"):
        return "true"
    if text in ("false", "0", "no"):
        return "false"
    raise PlanRejected(f"{raw!r} is not a boolean")


def _numeric_operand(raw: str) -> str:
    text = (raw or "").strip()
    if not re.fullmatch(r"-?\d+(\.\d+)?", text):
        raise PlanRejected(f"{raw!r} is not a number")
    return text


# ---------------------------------------------------------------------------
# Field resolution
# ---------------------------------------------------------------------------

def _resolve_path(
    schema: ObjectSchema,
    path: str,
    resolve_object,
) -> FieldInfo:
    """Validate a possibly dotted field path and return its LEAF field.

    `resolve_object(api_name) -> ObjectSchema | None` is how the caller supplies
    the parent describes; a path whose parent cannot be described is refused
    rather than assumed valid.
    """
    if not _PATH_RE.match(path or ""):
        raise PlanRejected(f"{path!r} is not a valid field path")
    parts = path.split(".")
    if len(parts) - 1 > MAX_RELATIONSHIP_DEPTH:
        raise PlanRejected(
            f"{path!r} traverses {len(parts) - 1} relationships; the limit is "
            f"{MAX_RELATIONSHIP_DEPTH}"
        )

    current = schema
    for hop in parts[:-1]:
        reference = current.relationship(hop)
        if reference is None:
            raise PlanRejected(
                f"{hop!r} is not a relationship on {current.name}"
            )
        if not reference.reference_to:
            raise PlanRejected(
                f"{hop!r} on {current.name} does not name a parent object"
            )
        parent = resolve_object(reference.reference_to[0])
        if parent is None:
            raise PlanRejected(
                f"cannot read the schema of {reference.reference_to[0]} "
                f"(traversed by {path!r})"
            )
        current = parent

    leaf = current.field_named(parts[-1])
    if leaf is None:
        raise PlanRejected(f"{parts[-1]!r} is not a field on {current.name}")
    if not leaf.readable:
        raise PlanRejected(
            f"{leaf.name} on {current.name} is not readable by this connection"
        )
    return leaf


def _canonical_path(schema: ObjectSchema, path: str, resolve_object) -> str:
    """The path re-spelled with the API's own casing, having been validated."""
    parts = path.split(".")
    current = schema
    out: List[str] = []
    for hop in parts[:-1]:
        reference = current.relationship(hop)
        assert reference is not None  # _resolve_path ran first
        out.append(reference.relationship_name)
        parent = resolve_object(reference.reference_to[0])
        assert parent is not None
        current = parent
    leaf = current.field_named(parts[-1])
    assert leaf is not None
    out.append(leaf.name)
    return ".".join(out)


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompiledQuery:
    soql: str
    object_api_name: str
    #: The expressions actually selected, in order — used to label result
    #: columns without re-parsing the query we just wrote.
    columns: Tuple[str, ...]
    result_mode: str
    limit: int
    is_aggregate: bool


def _compile_filter(
    schema: ObjectSchema, predicate: QueryFilter, resolve_object
) -> str:
    leaf = _resolve_path(schema, predicate.field, resolve_object)
    path = _canonical_path(schema, predicate.field, resolve_object)
    op = predicate.operator
    ftype = leaf.type

    if op == "is_null":
        return f"{path} = null"
    if op == "is_not_null":
        return f"{path} != null"

    def operand(raw: str) -> str:
        if (raw or "").strip().lower() in _UNRESOLVABLE_PLACEHOLDERS:
            raise PlanRejected(
                f"{path} was filtered on {raw!r}, which is a placeholder rather "
                "than a value. SOQL has no equivalent, and this connection "
                "authenticates as an integration user rather than as the person "
                "asking — so there is nothing honest to substitute."
            )
        if predicate.is_date_literal or ftype in _DATE_TYPES:
            return normalize_date_operand(raw)
        if ftype in _BOOLEAN_TYPES:
            return _boolean_operand(raw)
        if ftype in _NUMERIC_TYPES:
            return _numeric_operand(raw)
        if ftype in _ID_TYPES and not _SF_ID_RE.match((raw or "").strip()):
            # An ID column compared to something that is not an ID. The live
            # failure this catches: `RecordTypeId != 'Internal_Interview__c'` —
            # an object NAME quoted into an ID comparison — compiled cleanly
            # here and Salesforce rejected it at runtime ("invalid ID field"),
            # ending a fully-clarified request with a raw SOQL error. Rejecting
            # it HERE means the engine falls back to the warehouse and the
            # question still gets answered. The message teaches the repair,
            # because it is fed back verbatim on the planner's retry.
            raise PlanRejected(
                f"{path} is an ID field, and {raw!r} is not a Salesforce Id. "
                "Filter on the parent's readable field instead — for a record "
                "type use RecordType.Name = '…'; for a lookup use the dotted "
                "path to the parent's Name."
            )
        return _quote(raw)

    if op in ("in", "not_in"):
        if ftype in _DATE_TYPES:
            # An IN list of relative date literals is not something SOQL
            # accepts; refusing beats emitting a query that errors at the org.
            raise PlanRejected(f"IN is not supported on the date field {path}")
        rendered = ", ".join(operand(v) for v in predicate.values)
        keyword = "IN" if op == "in" else "NOT IN"
        return f"{path} {keyword} ({rendered})"

    if op in ("like", "starts_with", "ends_with", "contains"):
        if ftype in _DATE_TYPES or ftype in _NUMERIC_TYPES or ftype in _BOOLEAN_TYPES:
            raise PlanRejected(f"LIKE is not supported on {ftype} field {path}")
        # The wildcards are OURS. The user's text is escaped first, so a value
        # containing % or _ is matched literally rather than becoming a pattern.
        literal = escape_soql_string(predicate.value or "")
        literal = literal.replace("%", r"\%").replace("_", r"\_")
        pattern = {
            "like": f"%{literal}%",
            "contains": f"%{literal}%",
            "starts_with": f"{literal}%",
            "ends_with": f"%{literal}",
        }[op]
        return f"{path} LIKE '{pattern}'"

    symbol = {"eq": "=", "ne": "!=", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}[op]
    return f"{path} {symbol} {operand(predicate.value or '')}"


def _compile_aggregate(
    schema: ObjectSchema, spec: str, resolve_object
) -> str:
    """`count`, `count(Id)`, `sum(Amount)` → a validated SELECT expression."""
    text = spec.strip()
    match = re.fullmatch(r"(?i)([a-z_]+)\s*(?:\(\s*([A-Za-z0-9_.]*)\s*\))?", text)
    if match is None:
        raise PlanRejected(f"{spec!r} is not a valid aggregate expression")
    name = match.group(1).upper()
    argument = (match.group(2) or "").strip()
    if name in ("COUNT", "COUNT_DISTINCT") and not argument:
        if name == "COUNT_DISTINCT":
            raise PlanRejected("COUNT_DISTINCT needs a field")
        return "COUNT(Id)"
    if not argument:
        raise PlanRejected(f"{name} needs a field")
    _resolve_path(schema, argument, resolve_object)
    path = _canonical_path(schema, argument, resolve_object)
    return f"{name}({path})"


def compile_plan(
    plan: SalesforceQueryPlan,
    schema: ObjectSchema,
    *,
    resolve_object=lambda _name: None,
    limit_cap: int = MAX_LIMIT,
) -> CompiledQuery:
    """Validate and compile. Raises PlanRejected; never returns partial SOQL."""
    if not _API_NAME_RE.match(plan.object_api_name or ""):
        raise PlanRejected(f"{plan.object_api_name!r} is not a valid object name")
    if schema.name.lower() != plan.object_api_name.lower():
        raise PlanRejected(
            f"schema for {schema.name!r} was supplied for a plan on "
            f"{plan.object_api_name!r}"
        )
    if not schema.queryable:
        raise PlanRejected(f"{schema.name} is not queryable by this connection")

    aggregates = [_compile_aggregate(schema, a, resolve_object) for a in plan.aggregate_functions]
    grouped: List[str] = []
    for path in plan.group_by:
        _resolve_path(schema, path, resolve_object)
        grouped.append(_canonical_path(schema, path, resolve_object))

    selected: List[str] = []
    if plan.result_mode == "count" and not aggregates:
        aggregates = ["COUNT(Id)"]

    if aggregates:
        # SOQL rejects a plain field in an aggregate SELECT unless it is also in
        # the GROUP BY. Silently dropping the field would answer a different
        # question, so grouped fields lead and anything else is refused.
        selected.extend(grouped)
        selected.extend(aggregates)
        for path in plan.select_fields:
            _resolve_path(schema, path, resolve_object)
            canonical = _canonical_path(schema, path, resolve_object)
            if canonical not in grouped:
                raise PlanRejected(
                    f"{canonical} is selected alongside an aggregate but is not "
                    "in GROUP BY"
                )
    else:
        wanted = list(plan.select_fields) + list(plan.relationship_paths)
        if not wanted:
            wanted = ["Id"]
        for path in wanted:
            _resolve_path(schema, path, resolve_object)
            canonical = _canonical_path(schema, path, resolve_object)
            if canonical not in selected:
                selected.append(canonical)
        # Id first and always: it is what lets a live row be matched against a
        # record already held locally (core/salesforce.merge_rows).
        id_field = schema.field_named("id")
        if id_field is not None and id_field.name not in selected:
            selected.insert(0, id_field.name)

    if not selected:
        raise PlanRejected("the plan selects nothing")
    if len(selected) > MAX_SELECT_FIELDS:
        raise PlanRejected(
            f"the plan selects {len(selected)} expressions; the limit is "
            f"{MAX_SELECT_FIELDS}"
        )
    if len(plan.filters) > MAX_FILTERS:
        raise PlanRejected(
            f"the plan has {len(plan.filters)} filters; the limit is {MAX_FILTERS}"
        )

    clauses = [f"SELECT {', '.join(selected)}", f"FROM {schema.name}"]

    if plan.filters:
        predicates = [_compile_filter(schema, f, resolve_object) for f in plan.filters]
        clauses.append("WHERE " + " AND ".join(predicates))

    if grouped:
        clauses.append("GROUP BY " + ", ".join(grouped))
        if plan.having:
            if not aggregates:
                raise PlanRejected("HAVING requires an aggregate")
            having = [_compile_filter(schema, f, resolve_object) for f in plan.having]
            clauses.append("HAVING " + " AND ".join(having))
    elif plan.having:
        raise PlanRejected("HAVING requires GROUP BY")

    if plan.order_by:
        ordered = []
        for item in plan.order_by:
            _resolve_path(schema, item.field, resolve_object)
            canonical = _canonical_path(schema, item.field, resolve_object)
            if aggregates and canonical not in grouped:
                raise PlanRejected(
                    f"cannot ORDER BY {canonical}: it is not in GROUP BY"
                )
            ordered.append(f"{canonical} {item.direction.upper()}")
        clauses.append("ORDER BY " + ", ".join(ordered))

    limit = max(1, min(int(plan.limit), int(limit_cap)))
    is_aggregate = bool(aggregates)
    # SOQL refuses LIMIT on a NON-GROUPED aggregate ("Non-grouped query that
    # uses overall aggregate functions cannot also use LIMIT"). One grouped row
    # per group means the cap still applies there — same rule core/salesforce.py
    # learned live on 2026-08-06.
    if not (is_aggregate and not grouped):
        clauses.append(f"LIMIT {limit}")
        if plan.offset:
            # OFFSET is capped by Salesforce at 2000 and is only legal with a
            # LIMIT; deep pagination uses queryMore, not OFFSET.
            clauses.append(f"OFFSET {min(int(plan.offset), 2000)}")

    return CompiledQuery(
        soql=" ".join(clauses),
        object_api_name=schema.name,
        columns=tuple(selected),
        result_mode=plan.result_mode,
        limit=limit,
        is_aggregate=is_aggregate,
    )


# ---------------------------------------------------------------------------
# Deterministic calculation over returned records
# ---------------------------------------------------------------------------

def _numeric(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        try:
            return float(text)
        except ValueError:
            return None
    return None


def calculate(
    rows: Sequence[Mapping[str, Any]],
    *,
    total_records: Optional[int] = None,
    numeric_fields: Iterable[str] = (),
    group_by: str = "",
) -> Dict[str, Any]:
    """Counts, sums, averages and shares — computed in CODE, never by the model.

    The model is shown this result and told to quote it. Asking a language model
    to count a list of 180 records is how "29 records" was reported for a
    314-row result (see engines/sql.py's narrative rules); it is not a thing to
    do twice.
    """
    records = list(rows)
    out: Dict[str, Any] = {
        "record_count": int(total_records) if total_records is not None else len(records),
        "rows_examined": len(records),
    }
    for name in numeric_fields:
        values = [v for v in (_numeric(r.get(name)) for r in records) if v is not None]
        if not values:
            continue
        total = sum(values)
        out.setdefault("totals", {})[name] = {
            "sum": round(total, 4),
            "average": round(total / len(values), 4),
            "min": round(min(values), 4),
            "max": round(max(values), 4),
            "counted": len(values),
        }
    if group_by:
        buckets: Dict[str, int] = {}
        for record in records:
            key = record.get(group_by)
            buckets[str(key) if key is not None else "(none)"] = (
                buckets.get(str(key) if key is not None else "(none)", 0) + 1
            )
        denominator = sum(buckets.values()) or 1
        out["groups"] = [
            {
                "value": key,
                "count": count,
                "share_percent": round(100.0 * count / denominator, 2),
            }
            for key, count in sorted(buckets.items(), key=lambda kv: -kv[1])
        ]
        out["group_denominator"] = denominator
    return out
