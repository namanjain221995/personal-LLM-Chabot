"""Live, read-only Salesforce access for questions the warehouse cannot answer.

The synced warehouse is fast and cheap but it is a SNAPSHOT: up to one sync
interval stale, and limited to configured objects. When a question needs
something newer than the last cycle, or an object nobody added, the honest
options are "answer from stale data", "say I don't know", or "go and look".
This module is the third one.

SAFETY. Model-generated SOQL reaches a PRODUCTION org here, so it is treated
exactly like the DuckDB path:
  - one SELECT, nothing else. No DML verbs exist in SOQL over REST, but a
    semicolon-joined second statement or a subquery-shaped injection still
    must not get through;
  - a LIMIT is ALWAYS enforced, so a careless query cannot pull an org;
  - the credentials belong to a read-only integration user, which is the real
    boundary — everything here is defence in depth behind that.
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import settings

#: Hard ceiling on rows returned to a prompt, whatever the model asked for.
MAX_ROWS = 200

#: SOQL is read-only over REST, but these must never appear even so — a
#: model that has been told "you may write SQL" sometimes tries.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|UPSERT|MERGE|UNDELETE|CREATE|ALTER|DROP|GRANT|REVOKE)\b",
    re.I,
)


class SalesforceUnavailable(RuntimeError):
    """No credentials, or the org could not be reached."""


class UnsafeSoql(ValueError):
    """The generated SOQL was refused before it reached Salesforce."""


def configured() -> bool:
    """True when live lookups are possible at all."""
    return bool(
        settings.sf_client_id and settings.sf_login_url
        and (settings.sf_client_secret or settings.sf_private_key_b64)
    )


def _outer_select_clause(text: str) -> str:
    """The SELECT list of the OUTER query, with child subqueries removed.

    `SELECT Id, (SELECT COUNT(Id) FROM Contacts) FROM Account` must not read as
    an aggregate query: the aggregate belongs to the child. Parenthesised
    groups are stripped before the split so the first surviving " FROM " is the
    outer one.
    """
    out: List[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char != "(":
            out.append(char)
            index += 1
            continue
        # Balanced group: blank it out ONLY when it is a child subquery.
        # COUNT(Id) is a function call and must keep its parentheses, or the
        # aggregate test below stops recognising it and a LIMIT gets appended
        # to a query SOQL forbids one on.
        depth = 0
        end = index
        while end < length:
            if text[end] == "(":
                depth += 1
            elif text[end] == ")":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        group = text[index : end + 1]
        out.append(" " * len(group) if re.match(r"\(\s*SELECT\s", group, re.I) else group)
        index = end + 1
    return re.split(r"\sFROM\s", "".join(out), maxsplit=1, flags=re.I)[0]


def guard_soql(soql: str, *, max_rows: int = MAX_ROWS) -> str:
    """Validate and normalise model-generated SOQL. Raises UnsafeSoql.

    Returns the query with a LIMIT guaranteed, capped at `max_rows`. Callers
    that page through a whole result set pass a higher ceiling — Salesforce
    only returns a `nextRecordsUrl` when the LIMIT exceeds one 2,000-row batch,
    so a hard 200 makes pagination impossible by construction.
    """
    if not soql or not soql.strip():
        raise UnsafeSoql("empty query")
    text = " ".join(soql.strip().split())
    text = text.rstrip(";")

    if ";" in text:
        raise UnsafeSoql("only a single statement is allowed")
    if not re.match(r"^SELECT\s", text, re.I):
        raise UnsafeSoql("only SELECT queries are allowed")
    m = _FORBIDDEN.search(text)
    if m:
        raise UnsafeSoql(f"forbidden keyword: {m.group(1).upper()}")
    if not re.search(r"\sFROM\s", text, re.I):
        raise UnsafeSoql("query has no FROM clause")

    # SOQL REJECTS a LIMIT on any non-grouped aggregate query — not just the
    # bare COUNT() form ("COUNT() aggregate queries do not support LIMIT")
    # but every overall aggregate ("Non-grouped query that uses overall
    # aggregate functions cannot also use LIMIT", found live 2026-08-06 when
    # the model wrote SELECT COUNT(Id) FROM Contact and the forced LIMIT
    # broke it). An aggregate WITH a GROUP BY returns one row per group, so
    # the row cap still applies there.
    #
    # The aggregate test must look at the OUTER select list only. Splitting on
    # the first " FROM " read `SELECT Id, (SELECT COUNT(Id) FROM Contacts)
    # FROM Account` as an aggregate query and stripped the LIMIT altogether —
    # an UNBOUNDED query against a production org, from a child subquery the
    # SOQL prompt actively tells the model to write (2026-08-29).
    select_clause = _outer_select_clause(text)
    has_aggregate = re.search(
        r"\b(COUNT|COUNT_DISTINCT|SUM|AVG|MIN|MAX)\s*\(", select_clause, re.I
    )
    has_group_by = re.search(r"\sGROUP\s+BY\s", text, re.I)
    if has_aggregate and not has_group_by:
        return re.sub(r"\s+LIMIT\s+\d+(\s+OFFSET\s+\d+)?\s*$", "", text, flags=re.I)

    # Enforce a LIMIT rather than trusting one to be present. A top-level
    # LIMIT that is too high is lowered; a missing one is added.
    #
    # OFFSET is part of the tail: anchoring only on LIMIT missed
    # "... LIMIT 200 OFFSET 50" and appended a SECOND limit, producing
    # "... LIMIT 200 OFFSET 50 LIMIT 200" — MALFORMED_QUERY on every paged
    # request the planner makes (2026-08-29).
    limit_match = re.search(
        r"\sLIMIT\s+(\d+)(\s+OFFSET\s+\d+)?\s*$", text, re.I
    )
    if limit_match:
        if int(limit_match.group(1)) > max_rows:
            offset = limit_match.group(2) or ""
            text = text[: limit_match.start()] + f" LIMIT {max_rows}{offset}"
    else:
        text = f"{text} LIMIT {max_rows}"
    return text


class _Token:
    """Cached access token. Salesforce tokens outlive a single request, and
    re-authenticating per question would add a round trip to every answer."""

    TTL = 25 * 60

    def __init__(self) -> None:
        self.value: Optional[str] = None
        self.instance: Optional[str] = None
        self.at: float = 0.0

    def stale(self) -> bool:
        return self.value is None or (time.monotonic() - self.at) > self.TTL


_token = _Token()


async def _authenticate() -> Tuple[str, str]:
    if not configured():
        raise SalesforceUnavailable("Salesforce credentials are not configured")
    if not _token.stale() and _token.value and _token.instance:
        return _token.value, _token.instance

    if not settings.sf_client_secret:
        # The JWT grant needs a signing key; that path lives in the sync
        # worker. Live lookups simply stay off rather than duplicating it.
        raise SalesforceUnavailable(
            "live Salesforce lookups need SF_CLIENT_SECRET (client-credentials grant)"
        )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.sf_login_url.rstrip('/')}/services/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "client_id": settings.sf_client_id,
                "client_secret": settings.sf_client_secret,
            },
        )
    if resp.status_code != 200:
        # Never echo the body: it can contain the client id.
        raise SalesforceUnavailable(
            f"Salesforce refused the credentials (HTTP {resp.status_code})"
        )
    body = resp.json()
    _token.value = body["access_token"]
    _token.instance = body["instance_url"].rstrip("/")
    _token.at = time.monotonic()
    return _token.value, _token.instance


async def run_soql(soql: str) -> Tuple[str, List[Dict[str, Any]]]:
    """Run a guarded SOQL query live. → (query actually run, rows)."""
    safe = guard_soql(soql)
    token, instance = await _authenticate()
    async with httpx.AsyncClient(timeout=settings.sf_live_timeout) as client:
        resp = await client.get(
            f"{instance}/services/data/{settings.sf_api_version}/query",
            params={"q": safe},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code == 401:
        _token.value = None  # expired underneath us — one retry
        token, instance = await _authenticate()
        async with httpx.AsyncClient(timeout=settings.sf_live_timeout) as client:
            resp = await client.get(
                f"{instance}/services/data/{settings.sf_api_version}/query",
                params={"q": safe},
                headers={"Authorization": f"Bearer {token}"},
            )
    if resp.status_code != 200:
        detail = ""
        try:
            payload = resp.json()
            if isinstance(payload, list) and payload:
                detail = f": {payload[0].get('message', '')[:200]}"
        except Exception:
            pass
        raise SalesforceUnavailable(f"Salesforce rejected the query{detail}")

    body = resp.json()
    records = body.get("records", [])
    rows = [_clean(r) for r in records][:MAX_ROWS]
    # `SELECT COUNT() FROM X` answers via totalSize with EMPTY records —
    # returning [] here made every live count question look like "no data".
    # (COUNT(Field) comes back as a normal expr0 record and is unaffected.)
    if not rows and re.search(r"^\s*SELECT\s+COUNT\(\)", safe, re.I):
        total = body.get("totalSize")
        if isinstance(total, int):
            rows = [{"count": total}]
    return safe, rows


async def org_key() -> str:
    """A stable identifier for the org+identity this process talks to.

    Every metadata cache in Salesforce Intelligence Mode is keyed on this. A
    describe is a function of the ORG and of what the connected identity may
    see, so a cache keyed on the object name alone would serve one org's field
    list — or one permission set's — to another after a credential change. That
    is a data-leak shape, not a staleness shape, so the key is not optional.

    The instance URL plus the client id covers both: a different org has a
    different instance, and a different connected app has a different client id.
    Neither is a secret, and neither is logged by this module.
    """
    _token_value, instance = await _authenticate()
    material = f"{instance}|{settings.sf_client_id}|{settings.sf_api_version}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


async def run_soql_page(soql: str) -> Dict[str, Any]:
    """Run a guarded query and return the FULL first page envelope.

    `run_soql` deliberately returns only rows, which is right for the engines
    that summarise one page. Pagination needs `totalSize` (how many records
    matched, not how many were returned) and `nextRecordsUrl`, and inventing
    those from a row list is exactly how a summary claims "29 records" for a
    314-record result.
    """
    safe = guard_soql(soql)
    body = await _query(safe)
    records = body.get("records", []) or []
    rows = [_clean(r) for r in records]
    if not rows and re.search(r"^\s*SELECT\s+COUNT\(\)", safe, re.I):
        total = body.get("totalSize")
        if isinstance(total, int):
            rows = [{"count": total}]
    return {
        "soql": safe,
        "rows": rows,
        "total_size": body.get("totalSize"),
        "done": bool(body.get("done", True)),
        "next_records_url": body.get("nextRecordsUrl") or "",
    }


async def query_more(next_records_url: str) -> Dict[str, Any]:
    """Fetch the next page of a query. `next_records_url` comes from Salesforce.

    Validated rather than trusted: it is a value from a previous response, but
    it becomes a URL path on an authenticated request, so anything that is not
    the shape Salesforce actually returns is refused instead of being fetched.
    """
    path = (next_records_url or "").strip()
    if not re.fullmatch(
        r"/services/data/v\d+\.\d+/query/[A-Za-z0-9_-]+", path
    ):
        raise UnsafeSoql(f"not a Salesforce queryMore locator: {next_records_url!r}")
    body = await _get(path)
    return {
        "rows": [_clean(r) for r in body.get("records", []) or []],
        "total_size": body.get("totalSize"),
        "done": bool(body.get("done", True)),
        "next_records_url": body.get("nextRecordsUrl") or "",
    }


async def search_records(
    term: str, objects: List[str], fields_by_object: Dict[str, List[str]]
) -> Dict[str, List[Dict[str, Any]]]:
    """SOSL search for `term` across `objects`. → {object: [record, ...]}.

    The search term is ESCAPED for SOSL, which reserves a different character
    set than SOQL string literals do; the object and field names are validated
    as API names, never interpolated from user text.
    """
    if not term or not term.strip():
        raise UnsafeSoql("empty search term")
    for name in objects:
        if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", name or ""):
            raise UnsafeSoql(f"not a valid object name: {name!r}")
    returning = []
    for name in objects:
        fields = [
            f for f in fields_by_object.get(name, ["Id", "Name"])
            if re.match(r"^[A-Za-z][A-Za-z0-9_.]*$", f or "")
        ] or ["Id", "Name"]
        returning.append(f"{name}({', '.join(fields[:8])} LIMIT {SEARCH_LIMIT})")
    sosl = f"FIND {{{escape_sosl(term)}}} IN ALL FIELDS RETURNING {', '.join(returning)}"

    token, instance = await _authenticate()
    async with httpx.AsyncClient(timeout=settings.sf_live_timeout) as client:
        resp = await client.get(
            f"{instance}/services/data/{settings.sf_api_version}/search",
            params={"q": sosl},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        raise SalesforceUnavailable(
            f"Salesforce rejected the search (HTTP {resp.status_code})"
        )
    grouped: Dict[str, List[Dict[str, Any]]] = {name: [] for name in objects}
    for record in resp.json().get("searchRecords", []) or []:
        kind = (record.get("attributes") or {}).get("type") or ""
        grouped.setdefault(kind, []).append(_clean(record))
    return grouped


#: Per-object cap on search candidates. A clarification shows at most four
#: options, so anything beyond a handful is fetched for nothing.
SEARCH_LIMIT = 5

#: SOSL reserves these; a term containing one must have it escaped or the
#: search errors — and an unescaped `}` would close the FIND clause.
_SOSL_RESERVED = r'?&|!{}[]()^~*:\\"\'+-'


def escape_sosl(term: str) -> str:
    """Escape every SOSL metacharacter in a search term."""
    out = []
    for char in term:
        if char in _SOSL_RESERVED:
            out.append("\\")
        out.append(char)
    return "".join(out)


async def _query(soql: str) -> Dict[str, Any]:
    """GET /query with one re-authentication retry. Returns the raw envelope."""
    token, instance = await _authenticate()
    async with httpx.AsyncClient(timeout=settings.sf_live_timeout) as client:
        resp = await client.get(
            f"{instance}/services/data/{settings.sf_api_version}/query",
            params={"q": soql},
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401:
            _token.value = None  # expired underneath us — one retry
            token, instance = await _authenticate()
            resp = await client.get(
                f"{instance}/services/data/{settings.sf_api_version}/query",
                params={"q": soql},
                headers={"Authorization": f"Bearer {token}"},
            )
    if resp.status_code != 200:
        detail = ""
        try:
            payload = resp.json()
            if isinstance(payload, list) and payload:
                detail = f": {payload[0].get('message', '')[:200]}"
        except Exception:
            pass
        raise SalesforceUnavailable(f"Salesforce rejected the query{detail}")
    return resp.json()


async def _get(path: str, params: Optional[dict] = None) -> Any:
    """Authenticated GET against the REST API."""
    token, instance = await _authenticate()
    async with httpx.AsyncClient(timeout=settings.sf_live_timeout) as client:
        resp = await client.get(
            f"{instance}{path}",
            params=params or {},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        raise SalesforceUnavailable(f"Salesforce returned HTTP {resp.status_code}")
    return resp.json()


async def list_objects() -> List[Dict[str, Any]]:
    """Every object this user can query, with its label and custom flag.

    SCHEMA questions ("how many objects do we have?", "what fields does X
    have?") cannot be answered with SOQL — the model tried
    `SELECT ... FROM EntityDefinition` with an invented relationship and
    Salesforce rejected it. The describe API is the actual answer.
    """
    payload = await _get(f"/services/data/{settings.sf_api_version}/sobjects/")
    return [
        {
            "name": s["name"],
            "label": s.get("label", s["name"]),
            "custom": bool(s.get("custom")),
        }
        for s in payload.get("sobjects", [])
        if s.get("queryable") and not s.get("deprecatedAndHidden")
    ]


async def describe_object(name: str) -> Dict[str, Any]:
    """Field names and types for ONE object, as this user sees them."""
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", name or ""):
        raise UnsafeSoql(f"not a valid object name: {name!r}")
    payload = await _get(
        f"/services/data/{settings.sf_api_version}/sobjects/{name}/describe"
    )
    # The extra keys (2026-08-11) are what the query-plan compiler validates
    # against: `queryable`/`accessible` decide whether a plan may touch this at
    # all, and `relationshipName`/`referenceTo` are the only honest way to check
    # that `Account.Owner.Name` is a real traversal rather than a plausible
    # guess. Purely additive — every existing caller reads name/type/label.
    return {
        "name": payload.get("name", name),
        "label": payload.get("label", name),
        "queryable": payload.get("queryable", True),
        "fields": [
            {
                "name": f["name"],
                "type": f.get("type", ""),
                "label": f.get("label", f["name"]),
                # No `accessible` key on purpose: a describe only RETURNS the
                # fields this identity may read, so presence is the permission
                # check. Inventing a flag here would state a guarantee this
                # payload does not carry.
                "relationshipName": f.get("relationshipName") or "",
                "referenceTo": list(f.get("referenceTo") or ()),
            }
            for f in payload.get("fields", [])
        ],
    }


def _clean(record: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the API bookkeeping so rows read like the warehouse's."""
    return {k: v for k, v in record.items() if k != "attributes"}


def merge_rows(
    local: List[Dict[str, Any]], live: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Combine warehouse rows with live rows, LIVE winning on conflicts.

    The same record legitimately appears in both — that is the normal case,
    not an error — so it must be shown once. Salesforce Ids are globally
    unique and stable, which makes them the right key; rows without an Id
    (aggregates, GROUP BY results) cannot be deduped and are kept as they are.

    Live wins because it is by definition the newer of the two: a record
    edited since the last sync differs, and showing the stale copy would be
    the one outcome worse than not answering.
    """
    out: List[Dict[str, Any]] = []
    index: Dict[str, int] = {}

    for row in local:
        rid = row.get("Id") or row.get("id")
        if isinstance(rid, str) and rid:
            index[rid] = len(out)
        out.append(dict(row))

    for row in live:
        rid = row.get("Id") or row.get("id")
        if isinstance(rid, str) and rid and rid in index:
            # Same record from both sources: keep the local column set and
            # overlay the fresher values, so a live query selecting fewer
            # fields does not silently blank the rest of the row.
            out[index[rid]].update({k: v for k, v in row.items() if v is not None})
        else:
            if isinstance(rid, str) and rid:
                index[rid] = len(out)
            out.append(dict(row))
    return out
