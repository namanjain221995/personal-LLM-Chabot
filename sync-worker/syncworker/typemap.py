"""Salesforce field type -> DuckDB column type.

WHY THIS EXISTS. The warehouse lands every value as VARCHAR
(`storage.normalize_records`), for two good reasons: Bulk CSV and REST SOQL
disagree about shape for the same field, and inferring a column type from the
DATA is what wedged Interview__c into failing every cycle (an all-None batch
let DuckDB resolve the NULL type to INTEGER, and the first real value failed
the upsert forever after).

The mistake was concluding that types were therefore unknowable. They are
declared, per field, by Salesforce's own describe -- which the sync worker
already fetches every cycle and throws away. This module turns that declaration
into a DuckDB type.

TYPES ARE APPLIED IN A VIEW, NEVER IN THE TABLE. Casting at ingest destroys
anything unparseable: Salesforce genuinely returns '$1,234.56' into currency
fields, and at write time that becomes a permanent NULL with no way to see what
arrived. In a view it is NULL to the query and the original string is still
readable in the `raw` table. A view also makes a Salesforce type change a
regenerate instead of a migration across a thousand tables.

NAMING. Keys here are describe() type names -- `reference`, `string`, `double` --
NOT Metadata-API names. The .field-meta.xml files under brain/sources say
`Lookup`, `Text`, `Number` for the same three types. Mixing the two vocabularies
silently maps nothing.

TIME ZONES. Salesforce persists every DateTime in UTC and *displays* it in the
reading user's timezone -- 203 of this org's users are America/New_York. The
raw table therefore holds UTC, and the view converts to the org's zone, so a
figure in an answer matches what the same person sees in Salesforce. Only
`datetime` converts: Salesforce `date` and `time` are timezone-independent
values with no instant attached, and shifting them would invent an error.

Pure module: stdlib only, no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: DuckDB refuses DECIMAL wider than this. Salesforce caps precision at 18, so
#: this is a guard against malformed describe output rather than a real limit.
_MAX_DECIMAL_PRECISION = 38

#: Money keeps at least this many decimal places even when the org declares
#: fewer. 7 of 33 Currency fields in this org are scale=0; if Salesforce ever
#: returns '65.50' for one, a scale-0 column rounds it away silently. Cents
#: cost nothing to carry.
_CURRENCY_MIN_SCALE = 2

#: Never reach the warehouse at all -- compound fields break Bulk CSV, and
#: encrypted fields are credentials that must not land in an LLM-queryable
#: store. Listed so a caller can assert the exclusion rather than assume it.
UNSYNCED_TYPES = frozenset({"base64", "encryptedstring", "address", "location"})

#: Salesforce types that are already correctly VARCHAR. Enumerated rather than
#: defaulted, so a describe type nobody anticipated is visible as an unknown
#: instead of being silently swallowed as text.
TEXT_TYPES = frozenset({
    # identifiers -- 18-character alphanumerics, never numeric
    "id", "reference",
    # a reference to a data-category group; an identifier, not a value
    "datacategorygroupreference",
    # free text and constrained text
    "string", "textarea", "picklist", "multipicklist", "combobox",
    "email", "phone", "url",
    # `anyType` is a polymorphic column: one row can hold a number, the next a
    # date, the next free text. There is no type that describes it, and any
    # cast would NULL whichever rows disagree with the guess. Text is the only
    # representation that keeps every row.
    "anytype",
    # `complexvalue` arrives already serialised (REST renders it as JSON-ish
    # text, Bulk CSV as a flat string). Parsing it into a STRUCT would require
    # the source format to be guaranteed, and it is not -- the two transports
    # do not agree. Kept as the string Salesforce sent.
    "complexvalue",
})

#: Fixed mappings that need no precision/scale.
_SIMPLE = {
    "boolean": "BOOLEAN",
    "date": "DATE",
    "datetime": "TIMESTAMP",
    "time": "TIME",
    "int": "BIGINT",
    "long": "BIGINT",
}

#: Numeric types whose width comes from describe's precision/scale.
_DECIMAL_TYPES = frozenset({"double", "currency", "percent"})

#: What the raw column holds before conversion. Salesforce is UTC, always.
STORED_TIMEZONE = "UTC"

#: IANA zone names only. The value reaches SQL as a literal, so it is validated
#: rather than trusted -- it comes from configuration, and configuration is
#: still an input.
_TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+/-]{0,63}$")


def validate_timezone(timezone: str) -> str:
    """Return the zone name, or raise. IANA form, e.g. 'America/New_York'."""
    name = str(timezone or "").strip()
    if not _TZ_RE.match(name):
        raise ValueError(f"not a valid IANA timezone name: {timezone!r}")
    return name


class UnknownSalesforceType(ValueError):
    """A describe type this module has no rule for."""


@dataclass(frozen=True)
class FieldSpec:
    """One field as describe reports it."""

    name: str
    type: str
    precision: int | None = None
    scale: int | None = None

    @classmethod
    def from_describe(cls, field: dict) -> "FieldSpec":
        """Build from one entry of describe()'s `fields` list."""

        def _int_or_none(value) -> int | None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return cls(
            name=str(field.get("name", "")),
            type=str(field.get("type", "")).strip().lower(),
            precision=_int_or_none(field.get("precision")),
            scale=_int_or_none(field.get("scale")),
        )


def _decimal_type(sf_type: str, precision: int | None, scale: int | None) -> str:
    """DECIMAL(p, s) for a numeric field, or BIGINT when it holds no decimals.

    Scale is honoured as declared rather than fixed at 2: this org has Number
    fields at scale 0, 1, 2, 4 and 6, and a blanket DECIMAL(18,2) would
    silently truncate the 82 fields carrying more than two decimal places.
    """
    scale = 0 if scale is None else max(0, scale)
    if sf_type == "currency":
        scale = max(scale, _CURRENCY_MIN_SCALE)

    # A whole-number field is friendlier as BIGINT: SUM and AVG behave without
    # decimal-overflow surprises, and nothing downstream wants DECIMAL(2,0).
    if scale == 0:
        return "BIGINT"

    # precision counts TOTAL digits in Salesforce metadata, scale of which are
    # decimals -- the same convention DECIMAL(p, s) uses.
    precision = 18 if not precision or precision <= 0 else precision
    precision = min(precision, _MAX_DECIMAL_PRECISION)
    if precision <= scale:
        # Malformed describe (or a scale we widened past the declared
        # precision). Widen precision rather than emit an invalid type.
        precision = min(scale + 1, _MAX_DECIMAL_PRECISION)
    return f"DECIMAL({precision},{scale})"


def duckdb_type(spec: FieldSpec) -> str | None:
    """The DuckDB type for one field, or None to leave it VARCHAR.

    None is a real answer, not a failure: 72% of this org's fields are text
    and stay exactly as they are. `UnknownSalesforceType` is the failure.
    """
    sf_type = spec.type
    if not sf_type or sf_type in UNSYNCED_TYPES or sf_type in TEXT_TYPES:
        return None
    if sf_type in _SIMPLE:
        return _SIMPLE[sf_type]
    if sf_type in _DECIMAL_TYPES:
        return _decimal_type(sf_type, spec.precision, spec.scale)
    raise UnknownSalesforceType(
        f"{spec.name!r}: no DuckDB mapping for describe type {sf_type!r}"
    )


def cast_expression(spec: FieldSpec, org_timezone: str | None = None) -> str:
    """The SELECT expression for one column of a typed view.

    TRY_CAST, never CAST: one unparseable value must yield NULL for that row,
    not abort the whole query. That is the property that makes typing the read
    side safe when typing the write side would not be.

    `org_timezone` converts `datetime` columns out of stored UTC into the
    org's zone, so `date_trunc('day', x)` gives the day the user saw in
    Salesforce rather than the UTC day. Passing None (or UTC) leaves the
    value as stored. The result stays a naive TIMESTAMP either way, so the
    schema the SQL prompt reads does not change shape.
    """
    quoted = '"' + spec.name.replace('"', '""') + '"'
    # A describe type nobody anticipated must cost that ONE column its type,
    # not the whole object its view. `duckdb_type` stays strict so callers who
    # want the error can have it; here the column falls back to the string
    # Salesforce sent, and `unknown_types()` reports what was skipped.
    try:
        target = duckdb_type(spec)
    except UnknownSalesforceType:
        return quoted
    if target is None:
        return quoted

    cast = f"TRY_CAST({quoted} AS {target})"
    if target == "TIMESTAMP" and org_timezone:
        zone = validate_timezone(org_timezone)
        if zone.upper() != STORED_TIMEZONE:
            # Two steps, and the order matters: the first marks the naive
            # value as the UTC instant it actually is, the second renders
            # that instant as local wall-clock. DST is handled by the zone
            # database, so August (EDT) and January (EST) both come out right.
            cast = (
                f"(({cast} AT TIME ZONE '{STORED_TIMEZONE}')"
                f" AT TIME ZONE '{zone}')"
            )
    return f"{cast} AS {quoted}"


def plan_columns(
    specs: list[FieldSpec] | tuple[FieldSpec, ...],
    present: set[str] | frozenset[str] | None = None,
    org_timezone: str | None = None,
) -> list[str]:
    """Cast expressions for every field, in order.

    `present` restricts output to columns the warehouse table actually has --
    describe reports fields the sync skipped (field-level security, compound
    types, the SYNC_MAX_FIELDS ceiling), and selecting one of those in a view
    is a binder error at creation time.
    """
    out: list[str] = []
    seen: set[str] = set()
    for spec in specs:
        if not spec.name or spec.name in seen:
            continue
        if present is not None and spec.name not in present:
            continue
        seen.add(spec.name)
        out.append(cast_expression(spec, org_timezone))
    return out


def unknown_types(
    specs: list[FieldSpec] | tuple[FieldSpec, ...],
    present: set[str] | frozenset[str] | None = None,
) -> dict[str, list[str]]:
    """{describe type: [field names]} for types this module has no rule for.

    The counterpart to `cast_expression`'s fallback: the column keeps working
    as text, and this is how that decision becomes visible instead of silent.
    A non-empty result means the type map needs a new entry.
    """
    out: dict[str, list[str]] = {}
    for spec in specs:
        if present is not None and spec.name not in present:
            continue
        try:
            duckdb_type(spec)
        except UnknownSalesforceType:
            out.setdefault(spec.type, []).append(spec.name)
    return out


def summarize(specs: list[FieldSpec] | tuple[FieldSpec, ...]) -> dict[str, int]:
    """{duckdb type or 'VARCHAR': count} -- for logging what a rebuild changed."""
    counts: dict[str, int] = {}
    for spec in specs:
        try:
            target = duckdb_type(spec) or "VARCHAR"
        except UnknownSalesforceType:
            target = "UNKNOWN"
        counts[target] = counts.get(target, 0) + 1
    return counts
