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


def guard_soql(soql: str) -> str:
    """Validate and normalise model-generated SOQL. Raises UnsafeSoql.

    Returns the query with a LIMIT guaranteed.
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

    # SOQL's aggregate COUNT() returns a single number and REJECTS a LIMIT
    # ("COUNT() aggregate queries do not support LIMIT"). Adding one turned a
    # working count into an error, so the row cap simply does not apply here —
    # a count cannot return a large result set anyway.
    if re.match(r"^SELECT\s+COUNT\(\s*\)", text, re.I):
        return re.sub(r"\s+LIMIT\s+\d+\s*$", "", text, flags=re.I)

    # Enforce a LIMIT rather than trusting one to be present. A top-level
    # LIMIT that is too high is lowered; a missing one is added.
    limit_match = re.search(r"\sLIMIT\s+(\d+)\s*$", text, re.I)
    if limit_match:
        if int(limit_match.group(1)) > MAX_ROWS:
            text = text[: limit_match.start()] + f" LIMIT {MAX_ROWS}"
    else:
        text = f"{text} LIMIT {MAX_ROWS}"
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

    records = resp.json().get("records", [])
    return safe, [_clean(r) for r in records][:MAX_ROWS]


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
    return {
        "name": payload.get("name", name),
        "label": payload.get("label", name),
        "fields": [
            {"name": f["name"], "type": f.get("type", ""),
             "label": f.get("label", f["name"])}
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
