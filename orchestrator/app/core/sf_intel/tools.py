"""The Salesforce capabilities the intelligence engine is allowed to use.

    get_salesforce_schema        which objects and fields this connection sees
    search_salesforce_entities   real candidate records, for "which Acme?"
    execute_salesforce_query_plan a validated plan → SOQL → rows
    get_salesforce_query_more    the next page of the same query
    calculate_result             totals, shares and rankings, computed in code
    ask_clarifying_question      a CLIENT-side tool — see engines/sf_intel.py

Everything here reads. There is no write path, and none is added by this
feature: reinterpreting a read request as a write is the one mistake that
cannot be undone by asking again.

CACHING. Describes are cached per (org identity, object). The key is
`salesforce.org_key()` — instance URL + connected app + API version — so a
credential change, an org change or an API-version change all miss the cache
rather than serving one context's field list to another.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ...config import settings
from .. import salesforce
from .models import SalesforceQueryPlan
from .plan import (
    CompiledQuery,
    ObjectSchema,
    PlanRejected,
    build_object_schema,
    calculate,
    compile_plan,
)

log = logging.getLogger(__name__)

#: How long an object list / describe stays cached. Schema changes in a
#: production org are rare and a stale describe fails loudly (Salesforce refuses
#: the field), so this is generous — but it is a TTL, not a permanent cache.
DESCRIBE_TTL_SECONDS = 900.0

#: Objects whose describes are prefetched when Salesforce mode turns on. Cheap,
#: inexpensive metadata only — the directive's "prefetch only inexpensive
#: metadata already supported by the application".
COMMON_OBJECTS = ("Opportunity", "Account", "Case", "Task", "Contact", "Lead")

#: Total records ONE request may pull across all pages. Pagination exists to
#: answer "how many", not to mirror the org into a prompt.
MAX_TOTAL_RECORDS = 2000

#: Pages one request may fetch. A bound on the loop as well as on the rows.
MAX_PAGES = 10


class SalesforceToolError(RuntimeError):
    """A tool failed. Distinct from "no records matched" — the answer prompt is
    told which of the two happened, because presenting a failure as an empty
    result is the lie this whole feature exists to avoid."""


# ---------------------------------------------------------------------------
# Describe cache
# ---------------------------------------------------------------------------

@dataclass
class _Entry:
    value: Any
    at: float


_describe_cache: Dict[Tuple[str, str], _Entry] = {}
_objects_cache: Dict[str, _Entry] = {}
_cache_lock = asyncio.Lock()


def clear_caches() -> None:
    """Drop every cached describe. Called by tests, and safe at any time."""
    _describe_cache.clear()
    _objects_cache.clear()


def _fresh(entry: Optional[_Entry]) -> bool:
    return entry is not None and (time.monotonic() - entry.at) < DESCRIBE_TTL_SECONDS


async def list_objects(org: str) -> List[Dict[str, Any]]:
    """Queryable objects for this identity, cached per org."""
    entry = _objects_cache.get(org)
    if _fresh(entry):
        return entry.value  # type: ignore[union-attr]
    async with _cache_lock:
        entry = _objects_cache.get(org)
        if _fresh(entry):
            return entry.value  # type: ignore[union-attr]
        objects = await salesforce.list_objects()
        _objects_cache[org] = _Entry(objects, time.monotonic())
        return objects


async def describe(org: str, name: str) -> Optional[ObjectSchema]:
    """One object's schema, cached per (org, object). None when it cannot be read."""
    key = (org, name.lower())
    entry = _describe_cache.get(key)
    if _fresh(entry):
        return entry.value  # type: ignore[union-attr]
    try:
        described = await salesforce.describe_object(name)
    except Exception as exc:  # noqa: BLE001 — an object that is not there
        log.info("cannot describe %s: %s", name, str(exc)[:160])
        return None
    schema = build_object_schema(described)
    _describe_cache[(org, schema.name.lower())] = _Entry(schema, time.monotonic())
    _describe_cache[key] = _Entry(schema, time.monotonic())
    return schema


# ---------------------------------------------------------------------------
# get_salesforce_schema
# ---------------------------------------------------------------------------

def _dictionary_summary(question: str) -> str:
    """Object/field names from the org export, when it is available.

    The dictionary is the offline path: it is built from the org's own schema
    export, so it works when live credentials are not configured, and it names
    fields precisely — which is what SOQL needs and what a model guessing from
    table names never gets right.
    """
    try:
        from .. import sf_dictionary

        return sf_dictionary.hint_for(question) or ""
    except Exception:  # noqa: BLE001 — the export is optional
        return ""


def _warehouse_summary(question: str) -> str:
    """Object names from the synced warehouse, as a last resort."""
    try:
        from ..schema_cache import format_schema, relevant_schema, schema_cache

        schema = schema_cache.get(settings.duckdb_path)
        return format_schema(relevant_schema(schema, question))
    except Exception:  # noqa: BLE001 — first run has no warehouse
        return ""


async def get_salesforce_schema(
    question: str, *, objects: Sequence[str] = ()
) -> Tuple[str, List[str]]:
    """A schema summary for the planner. → (summary text, object api names).

    Live describes when credentials exist, the org export when they do not, and
    the warehouse schema underneath both. Never raises: a planner with no schema
    summary asks a broader question, which is a worse answer, not a failure.
    """
    named = [n for n in objects if n]
    if settings.sf_live_enabled and salesforce.configured():
        try:
            org = await salesforce.org_key()
            available = await list_objects(org)
            by_name = {o["name"].lower(): o for o in available}
            wanted = [n for n in named if n.lower() in by_name][:4]
            blocks: List[str] = []
            for name in wanted:
                schema = await describe(org, by_name[name.lower()]["name"])
                if schema is None:
                    continue
                fields = ", ".join(
                    f"{f.name} ({f.type})" for f in list(schema.fields.values())[:120]
                )
                blocks.append(f"{schema.name} — {schema.label}\n  fields: {fields}")
            if not blocks:
                # No specific object yet: list what exists so the planner picks a
                # name that is real rather than one that sounds right.
                custom = [o["name"] for o in available if o["custom"]][:60]
                standard = [o["name"] for o in available if not o["custom"]][:60]
                blocks.append(
                    f"{len(available)} queryable objects. "
                    f"Standard: {', '.join(standard)}. "
                    f"Custom: {', '.join(custom)}."
                )
            return "\n\n".join(blocks), [o["name"] for o in available]
        except Exception as exc:  # noqa: BLE001
            log.info("live schema summary unavailable: %s", str(exc)[:160])

    offline = _dictionary_summary(question) or _warehouse_summary(question)
    return offline, []


# ---------------------------------------------------------------------------
# search_salesforce_entities
# ---------------------------------------------------------------------------

#: Fields safe to SHOW as clarification metadata. Deliberately narrow: enough to
#: tell two records apart, and nothing an org would consider sensitive.
_DISPLAY_FIELDS = {
    "Account": ["Id", "Name", "BillingCity", "Industry", "Type"],
    "Contact": ["Id", "Name", "Title", "Email"],
    "Lead": ["Id", "Name", "Company", "Status"],
    "Opportunity": ["Id", "Name", "StageName", "CloseDate"],
    "Case": ["Id", "CaseNumber", "Subject", "Status"],
}

_HIDDEN_IN_LABELS = frozenset({"Id"})


async def search_salesforce_entities(
    term: str, *, objects: Sequence[str] = ("Account",), limit: int = 4
) -> List[Dict[str, Any]]:
    """Real candidate records for an ambiguous name. → option-shaped dicts.

    This is what turns "How is Acme doing?" from a guess into a question with
    ACTUAL answers on it. The record id rides in `value` (the planner needs it to
    build a filter) but never in the visible metadata — see `_DISPLAY_FIELDS`.
    """
    if not (settings.sf_live_enabled and salesforce.configured()):
        return []
    wanted = [o for o in objects if o] or ["Account"]
    fields = {name: _DISPLAY_FIELDS.get(name, ["Id", "Name"]) for name in wanted}
    try:
        grouped = await salesforce.search_records(term, wanted, fields)
    except Exception as exc:  # noqa: BLE001 — a failed search is not an answer
        log.info("entity search failed for %r: %s", term[:60], str(exc)[:160])
        return []

    out: List[Dict[str, Any]] = []
    for name in wanted:
        for record in grouped.get(name, [])[:limit]:
            display = {
                key: str(value)
                for key, value in record.items()
                if key in fields[name] and key not in _HIDDEN_IN_LABELS and value
            }
            label = display.get("Name") or display.get("CaseNumber") or name
            detail = ", ".join(
                f"{k}: {v}" for k, v in display.items() if k not in ("Name", "CaseNumber")
            )
            out.append(
                {
                    "object": name,
                    "record_id": record.get("Id", ""),
                    "label": label[:120],
                    "description": detail[:240],
                    "metadata": display,
                }
            )
    return out[: limit * len(wanted)]


# ---------------------------------------------------------------------------
# execute_salesforce_query_plan / get_salesforce_query_more
# ---------------------------------------------------------------------------

@dataclass
class QueryResult:
    soql: str
    object_api_name: str
    rows: List[Dict[str, Any]]
    #: What Salesforce says MATCHED, which is not what was returned.
    total_size: Optional[int]
    pages: int
    truncated: bool
    result_mode: str
    queried_at: str


async def compile_and_validate(plan: SalesforceQueryPlan) -> CompiledQuery:
    """Validate a plan against the live describes and compile it.

    Raises PlanRejected. Nothing is executed and no partial SOQL escapes: the
    caller either gets a query every one of whose names was checked against the
    org, or an exception it can turn into a question for the user.
    """
    if not (settings.sf_live_enabled and salesforce.configured()):
        raise PlanRejected("live Salesforce lookups are not configured")
    org = await salesforce.org_key()
    schema = await describe(org, plan.object_api_name)
    if schema is None:
        raise PlanRejected(
            f"{plan.object_api_name} is not an object this connection can read"
        )

    # Parent describes are fetched on demand, and only for traversals the plan
    # actually names — a plan with no dotted paths costs zero extra calls.
    resolved: Dict[str, Optional[ObjectSchema]] = {}
    pending = {
        path.split(".")[0]
        for path in (
            list(plan.select_fields)
            + list(plan.relationship_paths)
            + [f.field for f in plan.filters]
            + list(plan.group_by)
            + [o.field for o in plan.order_by]
        )
        if "." in path
    }
    for hop in pending:
        reference = schema.relationship(hop)
        if reference is None or not reference.reference_to:
            continue
        parent_name = reference.reference_to[0]
        if parent_name not in resolved:
            resolved[parent_name] = await describe(org, parent_name)

    def resolve_object(name: str) -> Optional[ObjectSchema]:
        return resolved.get(name)

    return compile_plan(plan, schema, resolve_object=resolve_object)


async def execute_salesforce_query_plan(
    plan: SalesforceQueryPlan,
    *,
    on_page=None,
) -> QueryResult:
    """Compile, run, and follow queryMore until the caps are reached.

    `on_page(rows_so_far)` is awaited after each page so the caller can report
    real progress ("Retrieved 400 records so far") without this function knowing
    what an SSE event is.
    """
    compiled = await compile_and_validate(plan)
    queried_at = _now_iso()
    try:
        page = await salesforce.run_soql_page(compiled.soql)
    except salesforce.UnsafeSoql:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SalesforceToolError(f"the Salesforce query failed: {exc}") from exc

    rows: List[Dict[str, Any]] = list(page["rows"])
    pages = 1
    next_url = page.get("next_records_url") or ""
    truncated = False

    # Only paginate when the answer needs the whole set. A records-mode request
    # is showing a page to a person; pulling 2,000 rows to display 50 spends the
    # org's API limits for nothing.
    wants_all = compiled.result_mode in ("aggregate", "count", "comparison", "timeline")
    while next_url and wants_all and pages < MAX_PAGES:
        if len(rows) >= MAX_TOTAL_RECORDS:
            truncated = True
            break
        try:
            more = await salesforce.query_more(next_url)
        except Exception as exc:  # noqa: BLE001
            log.info("queryMore failed after %d page(s): %s", pages, str(exc)[:160])
            truncated = True
            break
        rows.extend(more["rows"])
        pages += 1
        next_url = more.get("next_records_url") or ""
        if on_page is not None:
            await on_page(len(rows))
    if next_url and wants_all:
        truncated = True

    return QueryResult(
        soql=compiled.soql,
        object_api_name=compiled.object_api_name,
        rows=rows[:MAX_TOTAL_RECORDS],
        total_size=page.get("total_size"),
        pages=pages,
        truncated=truncated or len(rows) > MAX_TOTAL_RECORDS,
        result_mode=compiled.result_mode,
        queried_at=queried_at,
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# calculate_result
# ---------------------------------------------------------------------------

def _numeric_field_names(rows: Sequence[Mapping[str, Any]]) -> List[str]:
    """Columns that look numeric across the first rows.

    Sampled rather than described: an aggregate result has synthetic columns
    (`expr0`) that no describe knows about, and those are exactly the ones worth
    totalling.
    """
    if not rows:
        return []
    candidates: List[str] = []
    for key in rows[0].keys():
        values = [r.get(key) for r in rows[:25] if r.get(key) is not None]
        if not values:
            continue
        numeric = 0
        for value in values:
            if isinstance(value, bool):
                break
            if isinstance(value, (int, float)):
                numeric += 1
            elif isinstance(value, str):
                try:
                    float(value.replace(",", ""))
                    numeric += 1
                except ValueError:
                    break
            else:
                break
        if numeric == len(values):
            candidates.append(key)
    return candidates


def calculate_result(result: QueryResult, *, group_by: str = "") -> Dict[str, Any]:
    """Every number the answer is allowed to state, computed here.

    `record_count` prefers Salesforce's own `totalSize` over `len(rows)`: those
    differ whenever a page was capped, and quoting the page size as the total is
    the single most common way a data answer becomes wrong.
    """
    total = result.total_size if result.total_size is not None else len(result.rows)
    computed = calculate(
        result.rows,
        total_records=total,
        numeric_fields=_numeric_field_names(result.rows),
        group_by=group_by,
    )
    computed["pages_fetched"] = result.pages
    computed["result_truncated"] = result.truncated
    computed["object"] = result.object_api_name
    computed["queried_at"] = result.queried_at
    if result.truncated:
        computed["counts_cover"] = (
            "the records retrieved, not necessarily every matching record"
        )
    return computed
