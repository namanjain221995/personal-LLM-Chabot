"""The developer console's own API — `/admin/api/developers` (CONTRACT-3 §6, §17).

THIS IS THE BROWSER HALF OF THE DEVELOPER PLATFORM, and it is a different
surface from `/v1` in every way that matters. `/v1` reads one credential, the
`Authorization` header, and is cookie-blind. Everything here reads the
`ts_session` cookie and a CAPABILITY, and never looks at an API key: the two
vocabularies are kept apart on purpose (CONTRACT-3 §1), so a leaked key can
never be mistaken for an administrator and an administrator's cookie can never
drive the public API.

FOUR RULES DECIDE EVERY HANDLER BELOW.

1. **The workspace comes from the principal, never from the request.** Every
   read is scoped by `principal.workspace_id` in the WHERE clause (the V34
   accessors take it as a positional-only argument precisely so a request body
   cannot supply it), and every project id in a path is resolved through
   `_project_or_404`, which returns None for another tenant's project — so a
   foreign id reads as missing rather than as forbidden.
2. **A missing capability is 404, not 403** (CONTRACT-3 §6). That is the admin
   surface's convention (`authn/admin_api.py`) and it exists so a member
   probing `/admin/api/developers/projects` cannot even learn that a developer
   console exists. `require_capability` does it for us; nothing here may
   downgrade it to a 403.
3. **No secret leaves this surface, ever.** `api_keys.key_hash` and
   `api_webhook_endpoints.secret` / `previous_secret` are readable by these
   handlers because the same rows carry the metadata the console lists — so
   every payload is built from an EXPLICIT allow-list of fields rather than by
   handing a database row to the serialiser. A `{**row}` anywhere in this file
   would be a disclosure, which is why there is not one. The single exception
   is the plaintext API key, which is returned by exactly one handler
   (`create_key`), exactly once, and is never stored.
4. **Every mutation writes an audit event** through the existing writer
   (`authn/principal.audit`), with `resource_type` and `resource_id` set, in
   the same shape the rest of the admin surface uses — so the audit log reads
   as one story rather than two.

MOUNTING. This module exports `router` with the prefix `/admin/api/developers`
and is mounted by `app/main.py` (a single-owner file in this programme); there
is no `app.include_router` call here.

THE PLAYGROUND. `POST /playground/execute` runs a real generation for a
console user who has NOT pasted a key — that is the whole point of it: asking
an administrator to mint a live credential and paste it into a text box to try
the API is how long-lived keys end up in browser history. It reaches the model
through the same pieces `/v1` does and nothing lower (CONTRACT-3 §11): the
same `parse_responses_request` validation, the same registry resolution with
the same (per-workspace) database narrowing, the same QUOTA ENGINE in front of
admission — charged to a per-workspace playground allowance — and the same
pump (`publicapi.streaming`), so the same envelopes, error codes and §10 SSE
grammar. It writes no `api_responses` row and no conversation history: it is a
console action, not a project's API traffic, and a project's request log must
show what its keys did.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Sequence
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .. import db, usage as usage_ledger
from ..config import settings
from ..authn.principal import Principal, audit, require_capability
from ..authn.rbac import Cap
from ..publicapi import errors as api_errors
from ..publicapi import models as api_models
from ..publicapi import registry, streaming
from . import keys as key_tools
from . import projects as key_projects
from . import quotas
from .resolver import ApiCaller, CallerLimits
from .scopes import DEFAULT_SCOPES, UnknownScopeError, parse_scopes, scope_names
from .webhooks import queue as webhook_queue
from .webhooks import sender as webhook_sender
from .webhooks import signer as webhook_signer
from .webhooks import ssrf as webhook_ssrf

log = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/api/developers", tags=["developer-console"])


#: Striped process-local locks for the two console writes that must be
#: serialised (a key's rotation, a project's webhook cap). 2026-09-13, wave-3
#: re-verify: the first fix serialised them with a PostgreSQL advisory lock
#: held on one pooled connection while the work opened a second, and at
#: pool_max concurrency that deadlocked the ONE psycopg pool the chat
#: application shares. The rule since: wait for a lock holding NO connection —
#: a process-local lock first, then a single connection that does all the
#: work (and, where a cross-process guard is needed, holds the advisory lock
#: itself). Striped rather than one lock per key so the table cannot grow with
#: every key ever rotated; two keys that share a stripe merely take turns.
_LOCK_STRIPES = 64
_process_locks = [threading.Lock() for _ in range(_LOCK_STRIPES)]
#: How long a request may wait for its stripe before giving up with a 503.
#: Generous — the work under it is a few milliseconds of SQL — and finite, so
#: a wedged database cannot pile up worker threads without bound.
PROCESS_LOCK_TIMEOUT_S = 30.0


@contextlib.contextmanager
def _process_lock(name: str) -> Iterator[None]:
    stripe = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:4], "big")
    lock = _process_locks[stripe % _LOCK_STRIPES]
    if not lock.acquire(timeout=PROCESS_LOCK_TIMEOUT_S):
        raise HTTPException(
            status_code=503, detail="The console is busy; try again in a moment."
        )
    try:
        yield
    finally:
        lock.release()

#: The events a webhook endpoint may subscribe to (CONTRACT-3 §14). A closed
#: list, checked here, because an endpoint subscribed to an event this platform
#: never emits is a silent misconfiguration: the operator sees a saved row and
#: waits forever for a delivery.
WEBHOOK_EVENTS = webhook_sender.SUBSCRIBABLE_EVENTS

#: The event type of a console test delivery. Deliberately NOT one of the three
#: above: a consumer must be able to tell a drill from a real completion.
WEBHOOK_TEST_EVENT = webhook_sender.EVENT_TEST

#: The usage-ledger route name for a playground run. Distinct from
#: `v1_responses` (CONTRACT-3 §16) so the analytics console can tell a
#: developer's own experiment from the traffic their customers generate — the
#: two are paid for by the same GPU but they answer different questions.
PLAYGROUND_ROUTE = "api_playground"

#: How many webhook endpoints one project may register. Every endpoint
#: receives every subscribed event with up to six attempts and a 10 s budget
#: each, so an uncapped count is how one project buys itself a larger share of
#: the delivery sweep (2026-09-13 review: no cap existed anywhere).
MAX_WEBHOOK_ENDPOINTS_PER_PROJECT = 10
#: How many projects the usage panel aggregates: the same ceiling
#: `db.list_api_projects` clamps a page to.
MAX_USAGE_PROJECTS = 500

#: Request-log page bounds. `db.list_api_responses` clamps to 200 itself; the
#: query parameter is bounded here too so a client-chosen size is a 422 rather
#: than a silent clamp (OWASP API4).
MAX_LOG_LIMIT = 200

#: The output reservation when neither the model nor the deployment declares
#: one (CONTRACT-3 §12: "8,192 default"). Only reached on a deployment whose
#: settings carry no model ceiling at all, which is the shape a test harness
#: has.
FALLBACK_MAX_OUTPUT_TOKENS = 8192

#: How long a console usage window may be. `db.read_usage_daily` refuses more
#: than 93 days, and a client-chosen page size is a denial-of-service primitive
#: (OWASP API4), so the query parameter is bounded here as well as there.
MAX_USAGE_DAYS = 93


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _iso(value: Any) -> Optional[str]:
    """The admin surface's timestamp rendering, copied verbatim from
    `authn/admin_api.py` so both consoles put the same string on the wire."""
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _int_or_none(value: Any) -> Optional[int]:
    """An int, or None for "no value stored". Zero survives: a project whose
    quota really is 0 is a project that may not call the API, which is a
    meaningful state and not the same as "unset"."""
    return None if value is None else int(value)


def _list(value: Any) -> List[str]:
    """A jsonb array column as a list of strings. psycopg hands back a real
    list; anything else (a None from a row that predates the column) reads as
    empty rather than crashing the console."""
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


async def _project_or_404(principal: Principal, project_id: str) -> Dict[str, Any]:
    """The project named in the path, scoped to the caller's workspace.

    `db.get_api_project` takes the workspace id as its second argument and puts
    it in the WHERE clause, so another tenant's project is indistinguishable
    from one that never existed — which is exactly what this 404 says. Every
    nested resource (keys, webhooks, logs, limits) resolves through here first,
    so there is ONE tenancy check on this surface rather than one per handler.
    """
    row = await db.run_in_thread(db.get_api_project, project_id, principal.workspace_id)
    if row is None or _is_system_project(row):
        # The playground allowance row is a quota ledger, not a project: it
        # must never be listed, edited, given a key or used as a webhook
        # owner, so every path that names it reads it as missing.
        raise HTTPException(status_code=404, detail="No such project.")
    return row


def _customer_projects(workspace_id: str) -> List[Dict[str, Any]]:
    """`db.list_api_projects` minus the playground allowance row."""
    return [
        row
        for row in db.list_api_projects(workspace_id, include_playground=False)
        if not _is_system_project(row)
    ]


def _require(principal: Principal, cap: Cap) -> None:
    """A second capability on a handler that already has one.

    Same refusal style as the dependency: 404, never 403 (CONTRACT-3 §6).
    """
    if not principal.can(cap):
        raise HTTPException(status_code=404, detail="Not found.")


# ---------------------------------------------------------------------------
# Payloads — explicit allow-lists, never a spread of a database row
# ---------------------------------------------------------------------------


def _limits_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rpm": _int_or_none(row.get("rpm")),
        "input_tpm": _int_or_none(row.get("input_tpm")),
        "output_tpm": _int_or_none(row.get("output_tpm")),
        "max_concurrency": _int_or_none(row.get("max_concurrency")),
        "daily_token_quota": _int_or_none(row.get("daily_token_quota")),
        "max_input_tokens": _int_or_none(row.get("max_input_tokens")),
        "max_output_tokens": _int_or_none(row.get("max_output_tokens")),
    }


def _project_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "environment": row["environment"],
        "status": row["status"],
        "allowed_models": _list(row.get("allowed_models")),
        "allowed_origins": _list(row.get("allowed_origins")),
        "ip_allowlist": _list(row.get("ip_allowlist")),
        "retention_days": _int_or_none(row.get("retention_days")),
        "metadata": dict(row.get("metadata") or {}),
        "limits": _limits_payload(row),
        "created_by": _int_or_none(row.get("created_by")),
        "created_at": _iso(row.get("created_at")),
        "disabled_at": _iso(row.get("disabled_at")),
    }


def _service_account_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"],
        "description": row.get("description") or "",
        "status": row["status"],
        "scopes": _list(row.get("scopes")),
        "allowed_models": _list(row.get("allowed_models")),
        "created_at": _iso(row.get("created_at")),
        "last_used_at": _iso(row.get("last_used_at")),
        "disabled_at": _iso(row.get("disabled_at")),
    }


def _key_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """One key as the console may see it.

    `key_hash` IS NOT HERE, and neither is any derivative of it. The digest is
    HMAC-SHA256 under a pepper and is not directly usable as a credential, but
    it is the entire verification material for that key: an attacker holding
    the table plus the pepper can verify guesses offline, and a console that
    puts the digest in a JSON body puts it in a browser cache, a screenshot and
    a support ticket. `public_id` and `last_four` are the recognition values —
    both safe to log by CONTRACT-3 §5 — and they are enough for every question
    the console asks.
    """
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "service_account_id": row.get("service_account_id"),
        "name": row["name"],
        "environment": row["environment"],
        "public_id": row["public_id"],
        "last_four": row["last_four"],
        "scopes": _list(row.get("scopes")),
        "allowed_models": _list(row.get("allowed_models")),
        "rpm": _int_or_none(row.get("rpm")),
        "max_concurrency": _int_or_none(row.get("max_concurrency")),
        "status": row["status"],
        "created_by": _int_or_none(row.get("created_by")),
        "created_at": _iso(row.get("created_at")),
        "expires_at": _iso(row.get("expires_at")),
        "last_used_at": _iso(row.get("last_used_at")),
        "last_used_ip": row.get("last_used_ip") or "",
        "revoked_at": _iso(row.get("revoked_at")),
        "revoked_by": _int_or_none(row.get("revoked_by")),
        "rotated_from": row.get("rotated_from"),
        "rotation_expires_at": _iso(row.get("rotation_expires_at")),
    }


def _webhook_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """One webhook endpoint, WITHOUT its signing secret.

    `db.list_webhook_endpoints` returns `secret` and `previous_secret` because
    the delivery worker needs them to compute the signature. Nothing on an HTTP
    surface needs them: a signing secret in a response body is a forgery
    primitive — anyone who reads it can post a perfectly signed "your job
    finished" event to the customer's endpoint. The console shows that a
    secret EXISTS and that a rotation is in flight; the values stay server-side.
    """
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        # REDACTED: userinfo, query string and fragment removed. The row keeps
        # the URL intact because the delivery has to reach it; a listing is
        # read by everyone with `api.webhooks.manage` and cached by their
        # browser, and `?token=…` is how most receivers authenticate
        # (2026-09-13 review: tokens were in every listing in clear).
        "url": webhook_ssrf.redact_url(row["url"]),
        "events": _list(row.get("events")),
        "status": row["status"],
        "include_output": bool(row.get("include_output")),
        # The flags the database computes. Since the secret columns were
        # projected out of every read (V34 amendment, 2026-09-13) a truth test
        # on `row["secret"]` was always False — the console said every
        # endpoint was unsigned and no rotation was ever in flight.
        "has_secret": bool(row.get("has_secret")),
        "rotation_in_progress": _rotation_in_progress(row),
        "previous_secret_expires_at": _iso(row.get("previous_secret_expires_at")),
        "created_at": _iso(row.get("created_at")),
        "last_delivery_at": _iso(row.get("last_delivery_at")),
        "last_delivery_status": row.get("last_delivery_status") or "",
        "consecutive_failures": int(row.get("consecutive_failures") or 0),
        "disabled_at": _iso(row.get("disabled_at")),
    }


def _rotation_in_progress(row: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True while a previous secret is still being signed with — the same rule
    `webhooks.signer.active_secrets` applies at delivery time, so the console
    and the signature agree: a previous secret with no expiry, or one whose
    expiry has passed, is NOT an open rotation."""
    if not row.get("has_previous_secret"):
        return False
    expires = row.get("previous_secret_expires_at")
    if isinstance(expires, str):
        try:
            expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            return False
    if not isinstance(expires, datetime):
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires > (now or datetime.now(timezone.utc))


def _delivery_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """One delivery-history line. METADATA ONLY: the payload is not read at
    all (with `include_output` it carries generated text), and `error` passes
    through URL redaction again on the way out, for rows written before the
    sender redacted at storage."""
    return {
        "id": row["id"],
        "event_id": row.get("event_id") or "",
        "event_type": row.get("event_type") or "",
        "response_id": row.get("response_id"),
        "status": row.get("status") or "",
        "attempt": int(row.get("attempt") or 0),
        "max_attempts": int(row.get("max_attempts") or 0),
        "http_status": _int_or_none(row.get("http_status")),
        "error": webhook_ssrf.redact_urls_in_text(row.get("error") or ""),
        "next_attempt_at": _iso(row.get("next_attempt_at")) if row.get("status") == "pending" else None,
        "created_at": _iso(row.get("created_at")),
        "delivered_at": _iso(row.get("delivered_at")),
    }


def _log_payload(row: Dict[str, Any], keys_by_id: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """One request-log line: METADATA ONLY (CONTRACT-3 §16).

    `output_text` and `error_message` are deliberately absent. The first is the
    generated answer, which the platform does not show on this surface at all;
    the second is an engine-produced string that can carry an upstream URL, a
    container name or a traceback fragment, and CONTRACT-3 §9 forbids all three
    on any wire. `error_code` is the closed vocabulary a person can act on.
    """
    key = keys_by_id.get(row.get("key_id") or "")
    return {
        "id": row["id"],
        "request_id": row.get("request_id") or "",
        "model": row.get("model") or "",
        "status": row["status"],
        "background": bool(row.get("background")),
        "streamed": bool(row.get("streamed")),
        "input_tokens": _int_or_none(row.get("input_tokens")),
        "output_tokens": _int_or_none(row.get("output_tokens")),
        "ttft_ms": _int_or_none(row.get("ttft_ms")),
        "duration_ms": _int_or_none(row.get("duration_ms")),
        "error_code": row.get("error_code") or "",
        "metadata": dict(row.get("metadata") or {}),
        "key": None
        if key is None
        else {"id": key["id"], "name": key["name"], "last_four": key["last_four"]},
        "created_at": _iso(row.get("created_at")),
        "started_at": _iso(row.get("started_at")),
        "completed_at": _iso(row.get("completed_at")),
    }


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class _Body(BaseModel):
    """`extra="forbid"` on every console body, for the reason CONTRACT-3 §8
    gives for `/v1`: a field we cannot honour is REJECTED, not ignored.

    It is also a tenancy guard. A body carrying `workspace_id` — hopefully, or
    hopefully-not — is a 422 here rather than a field a future reader has to
    prove is unused, and a body carrying `rpm` on a project create is a 422
    rather than a limit raised without `api.limits.manage`.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProjectCreateRequest(_Body):
    name: str = Field(min_length=1, max_length=120)
    environment: str = Field(default="live", pattern="^(live|test)$")
    allowed_models: List[str] = Field(default_factory=list)
    allowed_origins: List[str] = Field(default_factory=list)
    retention_days: Optional[int] = Field(default=None, ge=1, le=365)
    metadata: Dict[str, str] = Field(default_factory=dict)


class ProjectUpdateRequest(_Body):
    """Every field optional; None means "leave it alone".

    The LIMIT columns are absent on purpose — they move through
    `PUT /projects/{id}/limits`, which is gated on `api.limits.manage`. If they
    were here, an admin with `api.projects.manage` could raise their own
    ceiling by naming it in a project edit, and the ceilings are what stop one
    developer key from starving the chat application of admission lanes
    (CONTRACT-3 §11).
    """

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    status: Optional[str] = Field(default=None, pattern="^(active|disabled)$")
    allowed_models: Optional[List[str]] = None
    allowed_origins: Optional[List[str]] = None
    ip_allowlist: Optional[List[str]] = None
    retention_days: Optional[int] = Field(default=None, ge=1, le=365)
    metadata: Optional[Dict[str, str]] = None


class LimitsRequest(_Body):
    rpm: Optional[int] = Field(default=None, ge=0, le=100_000)
    input_tpm: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    output_tpm: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    max_concurrency: Optional[int] = Field(default=None, ge=0, le=1_000)
    daily_token_quota: Optional[int] = Field(default=None, ge=0, le=10_000_000_000)
    max_input_tokens: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    max_output_tokens: Optional[int] = Field(default=None, ge=1, le=1_000_000)


class KeyCreateRequest(_Body):
    name: str = Field(min_length=1, max_length=120)
    scopes: Optional[List[str]] = None
    service_account_id: Optional[str] = Field(default=None, max_length=64)
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=365)


class KeyRotateRequest(_Body):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    #: How long the OLD key keeps working (CONTRACT-3 §5). None takes
    #: `projects.rotate_key`'s default
    #: (`settings.public_api_key_rotation_overlap_hours`); 0 is a rotation with no overlap — the
    #: compromise case — and revokes the old key at once.
    overlap_hours: Optional[int] = Field(
        default=None,
        ge=0,
        le=int(key_tools.MAX_ROTATION_OVERLAP.total_seconds() // 3600),
    )


class WebhookCreateRequest(_Body):
    url: str = Field(min_length=8, max_length=2048)
    events: List[str] = Field(default_factory=list)
    include_output: bool = False


class WebhookUpdateRequest(_Body):
    url: Optional[str] = Field(default=None, min_length=8, max_length=2048)
    events: Optional[List[str]] = None
    status: Optional[str] = Field(default=None, pattern="^(active|disabled)$")
    include_output: Optional[bool] = None


class ModelToggleRequest(_Body):
    enabled: bool


# ---------------------------------------------------------------------------
# Validation shared by projects and webhooks
# ---------------------------------------------------------------------------


def _declared_ids() -> List[str]:
    return [model.id for model in registry.declared_models()]


def _model_overrides(workspace_id: str) -> Dict[str, bool]:
    """The disabled declared models FOR THIS WORKSPACE (CONTRACT-3 §15
    narrowing). Synchronous; callers hop through `run_in_thread`."""
    return db.public_model_overrides(workspace_id, _declared_ids())


def _check_allowed_models(values: Sequence[str]) -> List[str]:
    """A project's model allowlist may only name models the CODE declares.

    Not a security boundary — `registry.resolve_public_model` is, and it starts
    from the declared tuple — but an allowlist naming `techsara-35`, a typo
    away from the real id, is a project that 404s every request with no way to
    see why. Refusing it at the point of entry is the kind one.
    """
    declared = set(_declared_ids())
    cleaned: List[str] = []
    for value in values:
        name = str(value).strip()
        if not name:
            continue
        if name not in declared:
            raise HTTPException(
                status_code=422,
                detail=f"{name!r} is not a public model id.",
            )
        if name not in cleaned:
            cleaned.append(name)
    return cleaned


def _check_origins(values: Sequence[str]) -> List[str]:
    """Browser origins for the project's CORS allowlist (CONTRACT-3 §3).

    `*` is refused explicitly. An allowlist containing a wildcard is not an
    allowlist, and the `/v1` check is written as "non-empty allowlist plus a
    non-matching Origin is a 403" — a `*` entry would turn that check off while
    still looking, in the console, like it was on.
    """
    cleaned: List[str] = []
    for value in values:
        origin = str(value).strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            raise HTTPException(
                status_code=422,
                detail="A wildcard origin is not an allowlist; name each origin.",
            )
        parts = urlsplit(origin)
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.path:
            raise HTTPException(
                status_code=422,
                detail="An origin looks like https://app.example.com — scheme and host only.",
            )
        if origin not in cleaned:
            cleaned.append(origin)
    return cleaned


def _check_ip_allowlist(values: Sequence[str]) -> List[str]:
    """Bare IPv4/IPv6 addresses or CIDR blocks, nothing else.

    A hostname here would be resolved at request time, which makes the
    allowlist only as trustworthy as whoever answers DNS — the same rebinding
    problem CONTRACT-3 §14 solves for webhook delivery, in the other direction.
    """
    import ipaddress

    cleaned: List[str] = []
    for value in values:
        item = str(value).strip()
        if not item:
            continue
        try:
            ipaddress.ip_network(item, strict=False)
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="The IP allowlist takes addresses or CIDR blocks, not hostnames.",
            ) from None
        if item not in cleaned:
            cleaned.append(item)
    return cleaned


def _check_metadata(values: Dict[str, str]) -> Dict[str, str]:
    """The §8 metadata bounds, reused here so the console cannot store a shape
    the public API would refuse."""
    if len(values) > api_models.MAX_METADATA_KEYS:
        raise HTTPException(
            status_code=422,
            detail=f"metadata takes at most {api_models.MAX_METADATA_KEYS} keys.",
        )
    out: Dict[str, str] = {}
    for key, value in values.items():
        if len(key) > api_models.MAX_METADATA_KEY_CHARS:
            raise HTTPException(status_code=422, detail="A metadata key is too long.")
        if len(str(value)) > api_models.MAX_METADATA_VALUE_CHARS:
            raise HTTPException(status_code=422, detail="A metadata value is too long.")
        out[key] = str(value)
    return out


def _check_webhook_url(url: str) -> str:
    """HTTPS, a real host, and no credentials in the URL (CONTRACT-3 §14).

    This is the CHEAP half of the SSRF defence and not the important one: the
    address check has to happen per delivery, against the resolved IP, because
    a name that resolves publicly now can resolve to 169.254.169.254 later.
    That belongs to the webhooks team's delivery path. What is refused here is
    what can be refused honestly at configuration time.
    """
    cleaned = str(url).strip()
    parts = urlsplit(cleaned)
    if parts.scheme != "https" or not parts.hostname:
        raise HTTPException(status_code=422, detail="A webhook URL must be https://.")
    if parts.username or parts.password:
        raise HTTPException(
            status_code=422, detail="A webhook URL must not carry credentials."
        )
    return cleaned


def _check_events(values: Sequence[str]) -> List[str]:
    cleaned: List[str] = []
    for value in values:
        event = str(value).strip()
        if event not in WEBHOOK_EVENTS:
            raise HTTPException(
                status_code=422,
                detail=f"{event!r} is not a webhook event; choose from "
                f"{', '.join(WEBHOOK_EVENTS)}.",
            )
        if event not in cleaned:
            cleaned.append(event)
    if not cleaned:
        raise HTTPException(
            status_code=422, detail="Subscribe the endpoint to at least one event."
        )
    return cleaned


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _overview_stats(workspace_id: str) -> Dict[str, Any]:
    """Everything the landing page counts, in one worker-thread hop and ONE
    aggregate query.

    Written as a single synchronous function handed to `db.run_in_thread`
    rather than six awaited accessors, because each `run_in_thread` is a
    thread hand-off and the console's first paint should not cost six of them.

    ONE QUERY, NOT ONE PER PROJECT (2026-09-13, wave-3 re-verify). This used to
    loop `db.list_api_keys` and `db.read_usage_daily` over every project — two
    pooled checkouts per project on the landing page, with project creation
    uncapped. The counts are now aggregated in PostgreSQL over the workspace,
    so the page costs the same whatever the workspace holds. The playground
    allowance row is excluded by its mark, as everywhere else.
    """
    day = _today()
    with db.connection() as con:
        totals = con.execute(
            "WITH p AS ("
            "  SELECT id, status FROM api_projects "
            "   WHERE workspace_id = %(ws)s AND NOT is_playground"
            "), k AS ("
            "  SELECT count(*) AS keys, "
            "         count(*) FILTER (WHERE k.status = 'active') AS active_keys "
            "    FROM api_keys k JOIN p ON p.id = k.project_id "
            "   WHERE k.workspace_id = %(ws)s"
            "), u AS ("
            "  SELECT COALESCE(sum(d.requests), 0) AS requests, "
            "         COALESCE(sum(d.input_tokens), 0) AS input_tokens, "
            "         COALESCE(sum(d.output_tokens), 0) AS output_tokens, "
            "         COALESCE(sum(d.errors), 0) AS errors "
            "    FROM api_usage_daily d JOIN p ON p.id = d.project_id "
            "   WHERE d.day = %(day)s"
            ") "
            "SELECT (SELECT count(*) FROM p) AS projects, "
            "       (SELECT count(*) FROM p WHERE status = 'active') AS active_projects, "
            "       k.keys, k.active_keys, u.requests, u.input_tokens, "
            "       u.output_tokens, u.errors "
            "  FROM k, u",
            {"ws": workspace_id, "day": day},
        ).fetchone()
    disabled = _model_overrides(workspace_id)
    return {
        "projects": int(totals["projects"]),
        "active_projects": int(totals["active_projects"]),
        "keys": int(totals["keys"]),
        "active_keys": int(totals["active_keys"]),
        "models": len(registry.public_models(disabled)),
        "today": {
            "day": day.isoformat(),
            "requests": int(totals["requests"]),
            "input_tokens": int(totals["input_tokens"]),
            "output_tokens": int(totals["output_tokens"]),
            "errors": int(totals["errors"]),
        },
    }


@router.get("/overview")
async def overview(
    principal: Principal = Depends(require_capability(Cap.API_CONSOLE_ACCESS)),
) -> dict:
    """The console landing page: what exists, what it spent today, and what
    THIS person may do with it.

    `capabilities` is the honest list, computed from the principal, so the page
    can hide a control the server would refuse anyway. It is a convenience for
    the UI and never the check itself: every mutation below re-asks.
    """
    stats = await db.run_in_thread(_overview_stats, principal.workspace_id)
    # The landing page is behind `api.console.access` only, so what it reveals
    # is cut to what the reader could read elsewhere: counts of projects and
    # keys need `api.projects.read`, today's spend needs `api.usage.read`
    # (2026-09-13 review — a role with only console access was shown both).
    if not principal.can(Cap.API_PROJECTS_READ):
        for field in ("projects", "active_projects", "keys", "active_keys"):
            stats[field] = None
    if not principal.can(Cap.API_USAGE_READ):
        stats["today"] = None
    return {
        "workspace": {"id": principal.workspace_id, "name": principal.workspace_name},
        "stats": stats,
        "capabilities": _capabilities(principal),
    }


def _capabilities(principal: Principal) -> Dict[str, bool]:
    """What THIS person may do, for the UI to hide what the server refuses."""
    return {
        "projects_read": principal.can(Cap.API_PROJECTS_READ),
        "projects_manage": principal.can(Cap.API_PROJECTS_MANAGE),
        "keys_create": principal.can(Cap.API_KEYS_CREATE),
        "keys_revoke": principal.can(Cap.API_KEYS_REVOKE),
        "usage_read": principal.can(Cap.API_USAGE_READ),
        "logs_read": principal.can(Cap.API_LOGS_READ),
        "webhooks_manage": principal.can(Cap.API_WEBHOOKS_MANAGE),
        "models_manage": principal.can(Cap.API_MODELS_MANAGE),
        "limits_manage": principal.can(Cap.API_LIMITS_MANAGE),
    }


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


@router.get("/projects")
async def list_projects(
    principal: Principal = Depends(require_capability(Cap.API_PROJECTS_READ)),
) -> dict:
    rows = await db.run_in_thread(_customer_projects, principal.workspace_id)
    return {"projects": [_project_payload(row) for row in rows]}


@router.post("/projects")
async def create_project(
    body: ProjectCreateRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_PROJECTS_MANAGE)),
) -> dict:
    """Create a project in the CALLER'S workspace.

    There is no workspace field in the body and there never will be: the
    accessor takes the tenant as its first argument and this handler can only
    pass `principal.workspace_id`.
    """
    allowed_models = _check_allowed_models(body.allowed_models)
    allowed_origins = _check_origins(body.allowed_origins)
    metadata = _check_metadata(body.metadata)

    def work() -> Dict[str, Any]:
        return db.create_api_project(
            principal.workspace_id,
            body.name.strip(),
            body.environment,
            created_by=principal.user_id,
            allowed_models=allowed_models,
            allowed_origins=allowed_origins,
            retention_days=body.retention_days,
            metadata=metadata,
        )

    try:
        row = await db.run_in_thread(work)
    except db.IntegrityError:
        # The unique index is on (workspace_id, lower(name)): a duplicate is a
        # 409 rather than a 500, and the message names nothing outside this
        # workspace.
        raise HTTPException(
            status_code=409, detail="A project with that name already exists."
        ) from None
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_project_created",
        resource_type="api_project",
        resource_id=row["id"],
        meta={"name": row["name"], "environment": row["environment"]},
    )
    return {"project": _project_payload(row)}


@router.get("/projects/{project_id}")
async def project_detail(
    project_id: str,
    principal: Principal = Depends(require_capability(Cap.API_PROJECTS_READ)),
) -> dict:
    project = await _project_or_404(principal, project_id)
    accounts = await db.run_in_thread(
        db.list_service_accounts, project["id"], principal.workspace_id
    )
    keys = await db.run_in_thread(
        db.list_api_keys, project["id"], principal.workspace_id
    )
    return {
        "project": _project_payload(project),
        "service_accounts": [_service_account_payload(row) for row in accounts],
        "keys": [_key_payload(row) for row in keys],
    }


@router.patch("/projects/{project_id}")
async def update_project(
    project_id: str,
    body: ProjectUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_PROJECTS_MANAGE)),
) -> dict:
    project = await _project_or_404(principal, project_id)
    fields: Dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name.strip()
    if body.status is not None:
        fields["status"] = body.status
    if body.allowed_models is not None:
        fields["allowed_models"] = _check_allowed_models(body.allowed_models)
    if body.allowed_origins is not None:
        fields["allowed_origins"] = _check_origins(body.allowed_origins)
    if body.ip_allowlist is not None:
        fields["ip_allowlist"] = _check_ip_allowlist(body.ip_allowlist)
    if body.retention_days is not None:
        fields["retention_days"] = body.retention_days
    if body.metadata is not None:
        fields["metadata"] = _check_metadata(body.metadata)
    if not fields:
        raise HTTPException(status_code=422, detail="Nothing to change.")

    try:
        row = await db.run_in_thread(
            lambda: db.update_api_project(project["id"], principal.workspace_id, **fields)
        )
    except db.IntegrityError:
        raise HTTPException(
            status_code=409, detail="A project with that name already exists."
        ) from None
    if row is None:  # pragma: no cover — _project_or_404 already proved it exists
        raise HTTPException(status_code=404, detail="No such project.")
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_project_updated",
        resource_type="api_project",
        resource_id=row["id"],
        # The FIELD NAMES that moved, not the values: a metadata value is the
        # customer's text and the audit log is read by more people than the
        # project is.
        meta={"fields": sorted(fields)},
    )
    return {"project": _project_payload(row)}


@router.get("/projects/{project_id}/service-accounts")
async def list_project_service_accounts(
    project_id: str,
    principal: Principal = Depends(require_capability(Cap.API_PROJECTS_READ)),
) -> dict:
    project = await _project_or_404(principal, project_id)
    rows = await db.run_in_thread(
        db.list_service_accounts, project["id"], principal.workspace_id
    )
    return {"service_accounts": [_service_account_payload(row) for row in rows]}


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def _resolved_scopes(values: Optional[Sequence[str]]) -> List[str]:
    """The scope list for a new key: what was asked for, or the safe default.

    `parse_scopes` is the closed vocabulary from wave 1 — an unknown scope is a
    422 here (the caller's REQUEST is wrong, not their credential, so it is
    never a 403; `UnknownScopeError` says the same thing).
    """
    try:
        parsed = parse_scopes(values if values is not None else DEFAULT_SCOPES)
    except UnknownScopeError as exc:
        raise HTTPException(
            status_code=422, detail=f"{exc.value!r} is not a valid scope."
        ) from None
    if not parsed:
        raise HTTPException(status_code=422, detail="A key needs at least one scope.")
    return scope_names(parsed)


def _mint_or_503(environment: str) -> Any:
    """Mint a key, turning a missing pepper into an honest 503.

    `key_digest` raises `PepperUnavailable` when neither `API_KEY_PEPPER` nor a
    `platform_secrets` row nor a store to write one is configured — a
    deployment mistake, not a caller mistake. A 500 with a traceback would be
    the wrong answer twice over: wrong status, and CONTRACT-3 §9 forbids the
    traceback. The message names the configuration, never its value.
    """
    try:
        minted = key_tools.mint_key(environment)
        digest = key_tools.key_digest(minted.secret)
    except key_tools.PepperUnavailable:
        log.error(
            "refusing to mint an API key: no pepper is configured. Wire "
            "configure_pepper_store() at start-up or set %s.",
            key_tools.PEPPER_ENV_VAR,
        )
        raise HTTPException(
            status_code=503,
            detail="API key signing is not configured on this deployment.",
        ) from None
    return minted, digest


async def _key_in_project_or_404(
    principal: Principal, project: Dict[str, Any], key_id: str
) -> Dict[str, Any]:
    """The key named in the path, inside the project named in the path.

    Both halves matter. `db.list_api_keys` is scoped by project AND workspace,
    so a key belonging to another tenant — or to a sibling project of the same
    tenant — is simply not in the list, and this answers 404 without having
    touched anything.
    """
    rows = await db.run_in_thread(
        db.list_api_keys, project["id"], principal.workspace_id
    )
    for row in rows:
        if row["id"] == key_id:
            return row
    raise HTTPException(status_code=404, detail="No such key.")


@router.get("/projects/{project_id}/keys")
async def list_keys(
    project_id: str,
    principal: Principal = Depends(require_capability(Cap.API_PROJECTS_READ)),
) -> dict:
    project = await _project_or_404(principal, project_id)
    rows = await db.run_in_thread(
        db.list_api_keys, project["id"], principal.workspace_id
    )
    return {"keys": [_key_payload(row) for row in rows]}


@router.post("/projects/{project_id}/keys")
async def create_key(
    project_id: str,
    body: KeyCreateRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_KEYS_CREATE)),
) -> dict:
    """Mint a key and return the plaintext ONCE.

    `key` in this response is the only time the secret exists outside the
    caller's browser: what is stored is `HMAC-SHA256(pepper, secret)`, so there
    is no path — not for an administrator, not for this server — to show it
    again. The console must copy it to the clipboard immediately, the way
    `InviteDialog` does for a one-time invitation link (CONTRACT-3 §17).

    The audit row records `public_id` and `last_four` and NOTHING else about
    the credential: both are safe to log by CONTRACT-3 §5, and both are enough
    to tie a leaked key back to who created it.
    """
    project = await _project_or_404(principal, project_id)
    if project["status"] != "active":
        raise HTTPException(
            status_code=409, detail="This project is disabled; enable it first."
        )
    scopes = _resolved_scopes(body.scopes)
    minted, digest = _mint_or_503(project["environment"])
    expires_at = key_tools.default_expires_at()
    if body.expires_in_days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days)

    def work() -> Dict[str, Any]:
        return db.create_api_key(
            project["id"],
            principal.workspace_id,
            body.name.strip(),
            minted.public_id,
            digest,
            minted.last_four,
            service_account_id=body.service_account_id or None,
            scopes=scopes,
            expires_at=expires_at,
            created_by=principal.user_id,
            environment=project["environment"],
        )

    try:
        row = await db.run_in_thread(work)
    except ValueError as exc:
        # The accessor raises ValueError for a service account that belongs to
        # another project. That is a caller mistake about an object it may not
        # see, so it reads as "no such service account" rather than echoing
        # the accessor's text.
        log.info("api key creation refused: %s", exc)
        raise HTTPException(
            status_code=422, detail="No such service account in this project."
        ) from None
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_key_created",
        resource_type="api_key",
        resource_id=row["id"],
        meta={
            "project_id": project["id"],
            "public_id": row["public_id"],
            "last_four": row["last_four"],
            "environment": row["environment"],
            "scopes": scopes,
        },
    )
    return {
        "key": _key_payload(row),
        # Shown once. Never stored, never logged, never returned again.
        "secret": minted.token,
    }


@router.post("/projects/{project_id}/keys/{key_id}/rotate")
async def rotate_key(
    project_id: str,
    key_id: str,
    body: KeyRotateRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_KEYS_CREATE)),
) -> dict:
    """Replace a key: mint its successor, and date the original out.

    WITH AN OVERLAP (CONTRACT-3 §5). The old key keeps working until
    `now + overlap`, written onto the OLD key's `rotation_expires_at` through
    `db.update_api_key_rotation`; the resolver refuses it after that instant
    and `db.expire_rotated_keys` records the revocation. `overlap_hours: 0` is
    the compromise case and revokes the old key immediately.

    ONE IMPLEMENTATION: `projects.rotate_key`. Until 2026-09-13 this route
    carried its own copy, serialised with `pg_advisory_xact_lock` held on one
    pooled connection while `db.list_api_keys` / `db.create_api_key` inside
    the lock each checked out a SECOND one. The wave-3 re-verifier ran 24
    concurrent rotations against the shared pool of 16: every connection
    ended up held by a request waiting for the lock, the holder could never
    get its second, and the whole orchestrator — chat, history, auth — stalled
    for 10 s until PoolTimeout turned nine of them into 500s. The canonical
    implementation claims the rotation with one conditional UPDATE on one
    connection (exactly one of N racing calls matches the row), and only then
    mints on another — never two connections at once.

    A process-local lock per key goes in front of it, taken BEFORE any
    connection is checked out: in-process losers wait holding nothing, and
    then fail the claim immediately instead of queueing on a row lock with a
    pooled connection in hand. Across processes the UPDATE alone is correct.

    It needs BOTH key capabilities: it creates a credential and ends one.
    """
    _require(principal, Cap.API_KEYS_REVOKE)
    project = await _project_or_404(principal, project_id)
    await _key_in_project_or_404(principal, project, key_id)
    # None takes the ONE default, `projects.rotate_key`'s
    # (`settings.public_api_key_rotation_overlap_hours`).
    overlap = None if body.overlap_hours is None else timedelta(hours=int(body.overlap_hours))
    moment = datetime.now(timezone.utc)

    def work() -> Dict[str, Any]:
        with _process_lock(f"api_key_rotation|{key_id}"):
            try:
                rotated = key_projects.rotate_key(
                    key_id,
                    project["id"],
                    principal.workspace_id,
                    overlap=overlap,
                    created_by=principal.user_id,
                    name=(body.name or "").strip() or None,
                    now=moment,
                )
            except ValueError:
                # Revoked, expired, or already inside a rotation — the path's
                # key was proven to exist in this project a moment ago.
                return {"refused": 409}
        # Read back AFTER the claim and the mint have both committed, on its
        # own connection: the console shows the old key as it now stands.
        previous = next(
            (
                row
                for row in db.list_api_keys(project["id"], principal.workspace_id)
                if row["id"] == rotated.previous_key_id
            ),
            None,
        )
        return {"rotated": rotated, "previous": previous}

    try:
        result = await db.run_in_thread(work)
    except key_tools.PepperUnavailable:
        log.error("refusing to rotate an API key: no pepper is configured")
        raise HTTPException(
            status_code=503,
            detail="API key signing is not configured on this deployment.",
        ) from None
    if result.get("refused") == 409:
        raise HTTPException(
            status_code=409,
            detail="That key is revoked or already being rotated.",
        )
    rotated = result["rotated"]
    created = rotated.created.key
    previous = result["previous"]
    if previous is None:  # deleted between the claim and the read-back
        raise HTTPException(status_code=404, detail="No such key.")
    overlap_seconds = (
        0
        if rotated.previous_revoked
        else max(0, int(round((rotated.previous_expires_at - moment).total_seconds())))
    )
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_key_rotated",
        resource_type="api_key",
        resource_id=created["id"],
        meta={
            "project_id": project["id"],
            "replaced_key_id": rotated.previous_key_id,
            "replaced_public_id": rotated.previous_public_id,
            "public_id": created["public_id"],
            "overlap_seconds": overlap_seconds,
        },
    )
    return {
        "key": _key_payload(created),
        "previous": _key_payload(previous),
        # Kept for the console's existing shape: the old key when it was
        # revoked outright, None while it is still inside its overlap.
        "revoked": _key_payload(previous) if previous.get("status") == "revoked" else None,
        "secret": rotated.created.token,
    }


@router.post("/projects/{project_id}/keys/{key_id}/revoke")
async def revoke_key(
    project_id: str,
    key_id: str,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_KEYS_REVOKE)),
) -> dict:
    """Revoke a key. Idempotent, and effective immediately everywhere: the
    resolver reads the row on every request and no key state is cached
    (CONTRACT-3 §5)."""
    project = await _project_or_404(principal, project_id)
    # Resolved inside the project BEFORE anything is written. `revoke_api_key`
    # is scoped by workspace, not by project, so revoking first and checking
    # afterwards would let a path naming project A revoke project B's key and
    # then answer 404 — a mutation behind a "nothing here" response, which is
    # the worst possible pair.
    existing = await _key_in_project_or_404(principal, project, key_id)
    row = await db.run_in_thread(
        db.revoke_api_key,
        existing["id"],
        principal.workspace_id,
        revoked_by=principal.user_id,
    )
    if row is None:  # pragma: no cover — the lookup above already proved it exists
        raise HTTPException(status_code=404, detail="No such key.")
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_key_revoked",
        resource_type="api_key",
        resource_id=row["id"],
        meta={
            "project_id": project["id"],
            "public_id": row["public_id"],
            "last_four": row["last_four"],
        },
    )
    return {"key": _key_payload(row)}


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/limits")
async def read_limits(
    project_id: str,
    principal: Principal = Depends(require_capability(Cap.API_PROJECTS_READ)),
) -> dict:
    """An administrator may READ the ceilings — they explain a 429 — but only
    `api.limits.manage` may move them (CONTRACT-3 §6)."""
    project = await _project_or_404(principal, project_id)
    return {
        "limits": _limits_payload(project),
        "can_manage": principal.can(Cap.API_LIMITS_MANAGE),
    }


@router.put("/projects/{project_id}/limits")
async def set_limits(
    project_id: str,
    body: LimitsRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_LIMITS_MANAGE)),
) -> dict:
    """Move a project's ceilings. SUPER_ADMIN only, by capability.

    The rate, token and concurrency ceilings are what stop one developer key
    from consuming the ten NORMAL admission lanes the chat application shares
    with it (CONTRACT-3 §11). Raising them is an infrastructure decision, so it
    sits with the same role that decides which models exist publicly.
    """
    project = await _project_or_404(principal, project_id)
    fields = {
        name: value
        for name, value in body.model_dump(exclude_none=True).items()
    }
    if not fields:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    row = await db.run_in_thread(
        lambda: db.update_api_project(project["id"], principal.workspace_id, **fields)
    )
    if row is None:  # pragma: no cover — _project_or_404 already proved it exists
        raise HTTPException(status_code=404, detail="No such project.")
    before = _limits_payload(project)
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_limits_changed",
        resource_type="api_project",
        resource_id=row["id"],
        # Numbers, not customer text: a ceiling change is exactly the kind of
        # event somebody reads the audit log to reconstruct, so both sides of
        # each changed value are recorded.
        meta={
            "changed": {
                name: {"from": before.get(name), "to": value}
                for name, value in fields.items()
            }
        },
    )
    return {"limits": _limits_payload(row)}


# ---------------------------------------------------------------------------
# Request logs
# ---------------------------------------------------------------------------


@router.get("/projects/{project_id}/logs")
async def request_logs(
    project_id: str,
    status: str = Query(
        "", pattern="^(|queued|in_progress|completed|failed|cancelled)$"
    ),
    limit: int = Query(50, ge=1, le=MAX_LOG_LIMIT),
    principal: Principal = Depends(require_capability(Cap.API_LOGS_READ)),
) -> dict:
    """A project's recent API requests — METADATA ONLY.

    `db.list_api_responses` is the one V34 accessor with no workspace argument
    (it is project-scoped), so the project is resolved through
    `_project_or_404` FIRST and its id — not the caller's — is what reaches
    the query. Prompts and generated text never appear on this surface at all:
    see `_log_payload`.
    """
    project = await _project_or_404(principal, project_id)

    def work() -> Dict[str, Any]:
        rows = db.list_api_responses(
            project["id"], status=status or None, limit=limit
        )
        keys = {
            row["id"]: row
            for row in db.list_api_keys(project["id"], principal.workspace_id)
        }
        return {"rows": rows, "keys": keys}

    data = await db.run_in_thread(work)
    return {
        "project": {"id": project["id"], "name": project["name"]},
        "requests": [_log_payload(row, data["keys"]) for row in data["rows"]],
    }


# ---------------------------------------------------------------------------
# Usage
# ---------------------------------------------------------------------------


@router.get("/usage")
async def usage_series(
    project_id: str = Query("", max_length=64),
    days: int = Query(30, ge=1, le=MAX_USAGE_DAYS),
    principal: Principal = Depends(require_capability(Cap.API_USAGE_READ)),
) -> dict:
    """Daily usage for one project, or for every project in the workspace.

    The window is bounded twice — by the query parameter and by
    `db.read_usage_daily`'s own `max_days` — because an unbounded range is a
    client-chosen result size, which is the denial-of-service shape OWASP API4
    names directly.
    """
    end = _today()
    start = end - timedelta(days=days - 1)
    if project_id:
        await _project_or_404(principal, project_id)

    def work() -> Dict[str, Any]:
        # ONE QUERY for every project's days (2026-09-13, wave-3 re-verify):
        # this looped `db.read_usage_daily` once per project. The projects are
        # bounded the way `db.list_api_projects` bounds a page, newest first,
        # and the LEFT JOIN keeps a project with no traffic in the table.
        with db.connection() as con:
            rows = con.execute(
                "WITH p AS ("
                "  SELECT id, name, created_at FROM api_projects "
                "   WHERE workspace_id = %(ws)s AND NOT is_playground "
                "     AND (%(pid)s = '' OR id = %(pid)s) "
                "   ORDER BY created_at DESC, id LIMIT %(limit)s"
                ") "
                "SELECT p.id, p.name, d.day, d.requests, d.input_tokens, "
                "       d.output_tokens, d.errors, d.rate_limited "
                "  FROM p LEFT JOIN api_usage_daily d "
                "    ON d.project_id = p.id AND d.day BETWEEN %(start)s AND %(end)s "
                " ORDER BY p.created_at DESC, p.id, d.day",
                {
                    "ws": principal.workspace_id,
                    "pid": project_id,
                    "limit": MAX_USAGE_PROJECTS,
                    "start": start,
                    "end": end,
                },
            ).fetchall()
        fields = ("requests", "input_tokens", "output_tokens", "errors", "rate_limited")
        per_day: Dict[str, Dict[str, int]] = {}
        per_project: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            totals = per_project.setdefault(
                row["id"],
                {
                    "id": row["id"],
                    "name": row["name"],
                    "requests": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "errors": 0,
                },
            )
            if row["day"] is None:
                continue
            day = row["day"].isoformat() if hasattr(row["day"], "isoformat") else str(row["day"])
            bucket = per_day.setdefault(day, {field: 0 for field in fields})
            for field in fields:
                bucket[field] += int(row.get(field) or 0)
            for field in ("requests", "input_tokens", "output_tokens", "errors"):
                totals[field] += int(row.get(field) or 0)
        return {"per_day": per_day, "per_project": list(per_project.values())}

    data = await db.run_in_thread(work)
    series = [
        {"day": day, **counts} for day, counts in sorted(data["per_day"].items())
    ]
    totals = {
        field: sum(int(entry[field]) for entry in series)
        for field in ("requests", "input_tokens", "output_tokens", "errors", "rate_limited")
    }
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    return {
        "range": {"days": days, "start": start.isoformat(), "end": end.isoformat()},
        "series": series,
        "totals": totals,
        "projects": data["per_project"],
    }


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------


async def _endpoint_or_404(
    principal: Principal, project: Dict[str, Any], endpoint_id: str
) -> Dict[str, Any]:
    rows = await db.run_in_thread(
        db.list_webhook_endpoints, project["id"], principal.workspace_id
    )
    for row in rows:
        if row["id"] == endpoint_id:
            return row
    raise HTTPException(status_code=404, detail="No such webhook endpoint.")


@router.get("/projects/{project_id}/webhooks")
async def list_webhooks(
    project_id: str,
    principal: Principal = Depends(require_capability(Cap.API_WEBHOOKS_MANAGE)),
) -> dict:
    project = await _project_or_404(principal, project_id)
    rows = await db.run_in_thread(
        db.list_webhook_endpoints, project["id"], principal.workspace_id
    )
    return {"webhooks": [_webhook_payload(row) for row in rows]}


def _insert_webhook_endpoint_capped(
    con: Any,
    project_id: str,
    workspace_id: str,
    url: str,
    events: Sequence[str],
    secret: str,
    *,
    include_output: bool,
    created_by: Optional[int],
) -> Optional[Dict[str, Any]]:
    """`db.create_webhook_endpoint`'s INSERT, on the caller's connection, and
    only while the project holds fewer than the cap. None when it does not (or
    when the project is not this workspace's).

    On the CALLER'S connection because the caller holds the advisory lock on
    it: an accessor that opened its own connection is exactly the nested
    checkout that deadlocked the pool. The column projection and the value
    shaping are `db`'s own (`_WEBHOOK_ENDPOINT_PUBLIC_SELECT` keeps both
    secrets out of the returned row; `_json_array` refuses a non-list), so this
    cannot drift into returning a secret the accessor would not.
    """
    if not str(url).lower().startswith("https://"):
        raise ValueError("a webhook url must be https://")
    columns = ["id", "project_id", "workspace_id", "url", "events", "secret", "include_output"]
    selects = ["%s", "p.id", "p.workspace_id", "%s", "%s", "%s", "%s"]
    params: List[Any] = [
        db._api_id("whe"),
        db._text(str(url)),
        db._json_array("api_webhook_endpoints.events", list(events)),
        secret,
        bool(include_output),
    ]
    if created_by is not None:
        columns.append("created_by")
        selects.append("%s")
        params.append(int(created_by))
    params.extend([project_id, workspace_id, int(MAX_WEBHOOK_ENDPOINTS_PER_PROJECT)])
    row = con.execute(
        f"INSERT INTO api_webhook_endpoints ({', '.join(columns)}) "
        f"SELECT {', '.join(selects)} FROM api_projects p "
        "WHERE p.id = %s AND p.workspace_id = %s "
        "  AND (SELECT count(*) FROM api_webhook_endpoints e WHERE e.project_id = p.id) < %s "
        f"RETURNING {db._WEBHOOK_ENDPOINT_PUBLIC_SELECT}",
        params,
    ).fetchone()
    return db._api_row(row)


@router.post("/projects/{project_id}/webhooks")
async def create_webhook(
    project_id: str,
    body: WebhookCreateRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_WEBHOOKS_MANAGE)),
) -> dict:
    """Register a delivery target. The signing secret is generated HERE and
    stays here.

    The secret is 256 bits from `secrets.token_urlsafe`, stored because the
    server has to compute `TechSara-Signature` with it (CONTRACT-3 §14), and
    returned in THIS response and never again — the show-once pattern an API
    key follows (CONTRACT-3 §17). Every other response on this surface carries
    only `has_secret`; see `_webhook_payload` for why.
    """
    project = await _project_or_404(principal, project_id)
    url = _check_webhook_url(body.url)
    events = _check_events(body.events)
    secret = webhook_signer.new_secret()

    def work() -> Optional[Dict[str, Any]]:
        # THE CAP, WITHOUT THE POOL DEADLOCK (2026-09-13, wave-3 re-verify).
        # The first fix took the advisory lock on one pooled connection and
        # then counted and inserted through `db.list_webhook_endpoints` /
        # `db.create_webhook_endpoint`, each of which checks out a second one;
        # 24 concurrent creates against the pool of 16 stalled the whole app
        # until PoolTimeout. Now: the process-local stripe is taken holding no
        # connection, and ONE connection takes the advisory lock (for other
        # processes), counts and inserts. The lock is transaction-scoped, so
        # it is released by the same commit that makes the insert visible to
        # the next waiter's count.
        with _process_lock(f"api_webhook_endpoints|{project['id']}"):
            with db.connection() as con:
                con.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"api_webhook_endpoints|{project['id']}",),
                )
                return _insert_webhook_endpoint_capped(
                    con,
                    project["id"],
                    principal.workspace_id,
                    url,
                    events,
                    secret,
                    include_output=bool(body.include_output),
                    created_by=principal.user_id,
                )

    try:
        row = await db.run_in_thread(work)
    except ValueError:
        raise HTTPException(status_code=422, detail="A webhook URL must be https://.") from None
    if row is None:
        raise HTTPException(
            status_code=409,
            detail=f"A project may have at most {MAX_WEBHOOK_ENDPOINTS_PER_PROJECT} webhook endpoints.",
        )
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_webhook_created",
        resource_type="api_webhook_endpoint",
        resource_id=row["id"],
        # Redacted for the same reason as the listing: the audit log is read
        # by more people than the endpoint's owner, and outlives the endpoint.
        meta={
            "project_id": project["id"],
            "url": webhook_ssrf.redact_url(url),
            "events": events,
        },
    )
    return {
        "webhook": _webhook_payload(row),
        # SHOWN ONCE, exactly like an API key's plaintext: a consumer cannot
        # verify `TechSara-Signature` without it (CONTRACT-3 §14), and no later
        # read returns it — the accessors project the column out.
        "secret": secret,
    }


@router.patch("/projects/{project_id}/webhooks/{endpoint_id}")
async def update_webhook(
    project_id: str,
    endpoint_id: str,
    body: WebhookUpdateRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_WEBHOOKS_MANAGE)),
) -> dict:
    project = await _project_or_404(principal, project_id)
    await _endpoint_or_404(principal, project, endpoint_id)
    fields: Dict[str, Any] = {}
    if body.url is not None:
        fields["url"] = _check_webhook_url(body.url)
    if body.events is not None:
        fields["events"] = _check_events(body.events)
    if body.status is not None:
        fields["status"] = body.status
    if body.include_output is not None:
        fields["include_output"] = bool(body.include_output)
    if not fields:
        raise HTTPException(status_code=422, detail="Nothing to change.")
    # `secret` and `previous_secret` are in the accessor's allow-list because
    # rotation needs them. They are NOT in this handler's: a console request
    # cannot set a signing secret to a value it chose, which would make every
    # signature forgeable by whoever watched the request.
    row = await db.run_in_thread(
        lambda: db.update_webhook_endpoint(endpoint_id, principal.workspace_id, **fields)
    )
    if row is None:  # pragma: no cover — _endpoint_or_404 already proved it exists
        raise HTTPException(status_code=404, detail="No such webhook endpoint.")
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_webhook_updated",
        resource_type="api_webhook_endpoint",
        resource_id=row["id"],
        meta={
            "project_id": project["id"],
            "fields": sorted(fields),
            **({"url": webhook_ssrf.redact_url(fields["url"])} if "url" in fields else {}),
        },
    )
    return {"webhook": _webhook_payload(row)}


@router.delete("/projects/{project_id}/webhooks/{endpoint_id}")
async def delete_webhook(
    project_id: str,
    endpoint_id: str,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_WEBHOOKS_MANAGE)),
) -> dict:
    project = await _project_or_404(principal, project_id)
    await _endpoint_or_404(principal, project, endpoint_id)
    deleted = await db.run_in_thread(
        db.delete_webhook_endpoint, endpoint_id, principal.workspace_id
    )
    if not deleted:  # pragma: no cover — _endpoint_or_404 already proved it exists
        raise HTTPException(status_code=404, detail="No such webhook endpoint.")
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_webhook_deleted",
        resource_type="api_webhook_endpoint",
        resource_id=endpoint_id,
        meta={"project_id": project["id"]},
    )
    return {"ok": True}


@router.post("/projects/{project_id}/webhooks/{endpoint_id}/test")
async def test_webhook(
    project_id: str,
    endpoint_id: str,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_WEBHOOKS_MANAGE)),
) -> dict:
    """Queue a test delivery.

    ENQUEUED, NOT SENT FROM HERE. The delivery itself — the signature, the
    bounded retries, and above all the per-delivery SSRF check against the
    RESOLVED address (CONTRACT-3 §14) — belongs to the webhooks worker. A
    console handler that opened the connection itself would be an
    administrator-triggered outbound request from inside the orchestrator with
    none of those defences, which is the classic server-side request forgery
    shape. This writes a durable row and returns its id; the sweep does the
    rest, and the delivery history shows the outcome.

    The payload carries the endpoint and project ids and a timestamp. No prompt
    and no generated text, the same as a real event.
    """
    project = await _project_or_404(principal, project_id)
    endpoint = await _endpoint_or_404(principal, project, endpoint_id)
    if endpoint["status"] != "active":
        raise HTTPException(
            status_code=409, detail="This endpoint is disabled; enable it first."
        )
    # Through the sender, not a hand-built row: the same payload shape, the
    # same attempt budget, and the worker is WOKEN rather than left to find it
    # on its next poll.
    delivery = await webhook_sender.enqueue_test_delivery(endpoint)
    event_id = "" if delivery is None else str(delivery["event_id"])
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_webhook_test_sent",
        resource_type="api_webhook_endpoint",
        resource_id=endpoint["id"],
        meta={"project_id": project["id"], "event_id": event_id},
    )
    return {
        "queued": delivery is not None,
        "event_id": event_id,
        "delivery_id": None if delivery is None else delivery["id"],
    }


@router.get("/projects/{project_id}/webhooks/{endpoint_id}/deliveries")
async def webhook_deliveries(
    project_id: str,
    endpoint_id: str,
    limit: int = Query(50, ge=1, le=webhook_queue.MAX_HISTORY_LIMIT),
    offset: int = Query(0, ge=0, le=webhook_queue.MAX_HISTORY_OFFSET),
    principal: Principal = Depends(require_capability(Cap.API_WEBHOOKS_MANAGE)),
) -> dict:
    """An endpoint's delivery history (CONTRACT-3 §14), newest first.

    Resolved through the project and then the endpoint, both in the caller's
    workspace, and the query repeats both predicates — so another tenant's
    endpoint id reads as missing here exactly as everywhere else.
    """
    project = await _project_or_404(principal, project_id)
    endpoint = await _endpoint_or_404(principal, project, endpoint_id)
    rows = await db.run_in_thread(
        functools.partial(
            webhook_queue.list_deliveries,
            endpoint["id"],
            project["id"],
            principal.workspace_id,
            limit=limit,
            offset=offset,
        )
    )
    return {
        "endpoint": {"id": endpoint["id"], "url": webhook_ssrf.redact_url(endpoint["url"])},
        "deliveries": [_delivery_payload(row) for row in rows],
        "page": {"limit": limit, "offset": offset},
    }


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/settings")
async def platform_settings(
    principal: Principal = Depends(require_capability(Cap.API_CONSOLE_ACCESS)),
) -> dict:
    """What this deployment's API is and how it is bounded, in one read.

    Everything here is a property of the platform, not a preference, and none
    of it is secret: the base path, the key shape, the body cap, the webhook
    signature and retry policy, the playground allowance every console user in
    the workspace shares. Nothing environment-specific (a hostname, a pepper
    source, an internal URL) is included — CONTRACT-3 §9's list of what no
    body may carry applies to the console too.
    """
    return {
        "api": {
            "base_path": "/v1",
            "authentication": "Authorization: Bearer",
            "key_prefixes": {
                "live": f"{key_tools.KEY_PREFIX}_live_",
                "test": f"{key_tools.KEY_PREFIX}_test_",
            },
            "max_body_bytes": api_models.max_body_bytes(),
        },
        "keys": {
            "default_lifetime_days": key_tools.DEFAULT_KEY_LIFETIME.days,
            # The value `projects.rotate_key` applies when a rotation names no
            # overlap — the one default, not `keys.DEFAULT_ROTATION_OVERLAP`,
            # which only happens to agree with it out of the box.
            "default_rotation_overlap_hours": int(
                float(settings.public_api_key_rotation_overlap_hours)
            ),
            "max_rotation_overlap_hours": int(
                key_tools.MAX_ROTATION_OVERLAP.total_seconds() // 3600
            ),
        },
        "playground": {"allowance": playground_allowance()},
        "webhooks": {
            "events": list(WEBHOOK_EVENTS),
            "signature_header": webhook_signer.SIGNATURE_HEADER,
            "tolerance_seconds": webhook_signer.DEFAULT_TOLERANCE_SECONDS,
            "max_attempts": webhook_sender.MAX_ATTEMPTS,
            "timeout_seconds": webhook_ssrf.TIMEOUT_SECONDS,
            "max_redirects": webhook_ssrf.MAX_REDIRECTS,
            "max_endpoints_per_project": MAX_WEBHOOK_ENDPOINTS_PER_PROJECT,
        },
        "capabilities": _capabilities(principal),
    }


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@router.get("/models")
async def list_models(
    principal: Principal = Depends(require_capability(Cap.API_CONSOLE_ACCESS)),
) -> dict:
    """What the CODE declares, plus whether the database has disabled it.

    The database may only narrow (CONTRACT-3 §15), so this list is built from
    `registry.declared_models()` and the stored rows are read as a flag on each
    entry. A `public_models` row naming an id the code does not declare cannot
    appear here at all, because there is nothing for it to be a flag ON —
    which is the narrowing rule made visible in the console.
    """
    stored = await db.run_in_thread(db.public_model_overrides, principal.workspace_id)
    models = []
    for model in registry.declared_models():
        entry = model.to_wire()
        entry["enabled"] = bool(stored.get(model.id, True))
        models.append(entry)
    return {"models": models, "can_manage": principal.can(Cap.API_MODELS_MANAGE)}


@router.put("/models/{model_id}")
async def set_model_enabled(
    model_id: str,
    body: ModelToggleRequest,
    request: Request,
    principal: Principal = Depends(require_capability(Cap.API_MODELS_MANAGE)),
) -> dict:
    """Disable (or re-enable) a declared public model FOR THIS WORKSPACE.

    PER WORKSPACE since 2026-09-13. `api.models.manage` is a workspace
    membership capability, and the write used to land in a deployment-wide
    table: a super admin of one workspace withdrew a model from every tenant,
    and the audit row went only into the actor's own workspace, so the
    affected tenants could not see who did it. The accessor now takes the
    principal's workspace, so the blast radius and the audit trail are the
    same workspace.

    An id the code does not declare is a 404 and no row is written. Storing it
    would create a `public_models` row that can never do anything — and would
    invite the belief that writing one is how a model becomes public, which is
    exactly the belief CONTRACT-3 §15 exists to prevent.
    """
    if model_id not in _declared_ids():
        raise HTTPException(status_code=404, detail="No such model.")
    row = await db.run_in_thread(
        functools.partial(
            db.set_public_model_enabled,
            principal.workspace_id,
            model_id,
            bool(body.enabled),
            updated_by=principal.user_id,
        )
    )
    await db.run_in_thread(
        audit,
        principal,
        request,
        "api_model_enabled" if body.enabled else "api_model_disabled",
        resource_type="public_model",
        resource_id=model_id,
        meta={"enabled": bool(body.enabled)},
    )
    return {"model": {"id": model_id, "enabled": bool(row["enabled"])}}


# ---------------------------------------------------------------------------
# Playground
# ---------------------------------------------------------------------------


def _request_id() -> str:
    return f"req_{secrets.token_hex(12)}"


def _api_error_response(
    exc: api_errors.ApiError,
    request_id: str,
    extra_headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    """An `ApiError` as the console sees it — the SAME envelope `/v1` returns.

    One error table for the playground and for the customer's own integration
    means a developer debugging a 400 in the console is debugging the 400 their
    code will get, not a translation of it.
    """
    headers = dict(extra_headers or {})
    headers.update(exc.headers())
    headers["X-Request-Id"] = request_id
    return JSONResponse(
        status_code=exc.status, content=exc.envelope(request_id), headers=headers
    )


async def _read_capped_body(request: Request, limit: int) -> bytes:
    """The request body, COUNTED as it arrives and refused past `limit`.

    THE 2026-09-13 HOLE. The playground used to compare the declared
    `Content-Length` with the cap and then call `request.json()`. A chunked
    request declares no length and a lying one declares whatever it likes, so
    the adversarial review put a 3 MiB prompt into `stream_chat_events` both
    ways, and a non-numeric header was an unhandled ValueError (a 500).

    Now: a declared length must be digits (400 otherwise) and within the cap
    (413, before a byte is read); then the body is read chunk by chunk and the
    read STOPS the moment the count passes the cap — `request.body()` would
    have buffered all of it first, which is the memory the cap exists to
    protect.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        text = declared.strip()
        if not text.isdigit():
            raise api_errors.invalid_request("The Content-Length header is not a number.")
        if int(text) > limit:
            raise api_errors.request_too_large(limit)
    received = bytearray()
    async for chunk in request.stream():
        received.extend(chunk)
        if len(received) > limit:
            raise api_errors.request_too_large(limit)
    return bytes(received)


# --- the playground allowance ----------------------------------------------
#
# THE 2026-09-13 HOLE. The playground was gated only by `api.console.access`
# and reached `stream_chat_events` with no rate limit, no quota and no
# concurrency cap, so any console user could open unlimited parallel
# generations on the NORMAL admission lanes the chat application shares —
# exactly the starvation CONTRACT-3 §11 puts the quota gate in front of
# admission to prevent. It now goes through the SAME engine as `/v1`:
# `quotas.reserve` (atomic per project under an advisory lock) and
# `quotas.concurrency_slot` (one in-process counter per project), charged to a
# per-workspace PLAYGROUND ALLOWANCE.
#
# WHY A SYSTEM PROJECT ROW. The quota ledgers (`api_usage_minute`,
# `api_usage_daily`) key on `api_projects.id` with a foreign key, and the
# engine enforces limits per project. Charging the playground to whichever
# customer project the console user picked would make that project's quota a
# lie in both directions; a per-workspace row that exists only to carry the
# allowance keeps the engine unchanged and the accounting honest. The row is
# hidden from every console listing, answers 404 on every project route (so no
# key can ever be minted for it, and `/v1` can never reach it), and its limits
# are re-asserted from configuration on each run.

PLAYGROUND_PROJECT_PREFIX = "proj_playground_"
PLAYGROUND_PROJECT_NAME = "Console playground"
PLAYGROUND_SYSTEM_MARK = "console_playground"

#: The defaults, deliberately a fraction of a project's (CONTRACT-3 §12: 60
#: rpm, concurrency 4): the playground is for trying a request, not for
#: running a workload, and every run it makes is a lane a signed-in person is
#: not using. Overridable per deployment through the environment; 0 means ZERO
#: allowed (the playground is switched off), never "use the default".
PLAYGROUND_DEFAULTS: Dict[str, int] = {
    "rpm": 20,
    "input_tpm": 100_000,
    "output_tpm": 30_000,
    "max_concurrency": 2,
    "daily_token_quota": 500_000,
}


def _env_limit(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        log.warning("%s is not an integer; using the default %d", name, default)
        return default
    return max(0, value)


def playground_allowance() -> Dict[str, int]:
    """The per-workspace playground ceilings, read from the environment on
    every call so a changed value applies without a restart."""
    return {
        field: _env_limit(f"API_PLAYGROUND_{field.upper()}", default)
        for field, default in PLAYGROUND_DEFAULTS.items()
    }


def playground_project_id(workspace_id: str) -> str:
    """Deterministic, so two concurrent first runs in one workspace race on
    the PRIMARY KEY (one insert wins, the other re-reads) rather than creating
    two allowances."""
    digest = hashlib.sha256(f"playground|{workspace_id}".encode("utf-8")).hexdigest()
    return f"{PLAYGROUND_PROJECT_PREFIX}{digest[:24]}"


def _is_system_project(row: Optional[Dict[str, Any]]) -> bool:
    """Decided by `api_projects.is_playground` (V34), a column no request body
    can write — never by `metadata`, which a project editor can write (a
    customer who set `{"system": "console_playground"}` on their own project
    must not be able to make it vanish from the console), and never by a name.

    Until 2026-09-13 (wave-3 re-verify) the row was recognised by an id prefix
    but had to be CREATED under a name inside the per-workspace unique name
    index; the id is a hash of the workspace id /overview returns, so an admin
    who created projects called "Console playground" and "Console playground
    <last 8 of that id>" made every playground run in the workspace a 500. The
    marked row is outside the name index now, so no name can block it."""
    return bool(row) and row.get("is_playground") is True


def _ensure_playground_project(workspace_id: str) -> Dict[str, Any]:
    """The workspace's allowance row, created on first use, its limits kept in
    step with `playground_allowance()`. Synchronous: one `run_in_thread`.

    Found by the mark, not by id or name. A concurrent first run loses on the
    primary key or on `idx_api_projects_one_playground` and re-reads."""
    allowance = playground_allowance()
    row = db.get_playground_project(workspace_id)
    if row is None:
        try:
            row = db.create_api_project(
                workspace_id,
                PLAYGROUND_PROJECT_NAME,
                "live",
                project_id=playground_project_id(workspace_id),
                metadata={"system": PLAYGROUND_SYSTEM_MARK},
                is_playground=True,
                **allowance,
            )
        except db.IntegrityError:
            row = db.get_playground_project(workspace_id)
            if row is None:
                raise
    project_id = str(row["id"])
    stale = {
        field: value
        for field, value in allowance.items()
        if _int_or_none(row.get(field)) != value
    }
    if stale or row.get("status") != "active":
        fields: Dict[str, Any] = dict(stale)
        if row.get("status") != "active":
            fields["status"] = "active"
        row = db.update_api_project(project_id, workspace_id, **fields) or row
    return row


def _playground_caller(
    principal: Principal,
    allowance_row: Dict[str, Any],
    allowed_models: Sequence[str],
) -> ApiCaller:
    """The identity the quota engine meters a playground run as.

    The PROJECT is the workspace's allowance row, so every console user in a
    workspace shares one ceiling — which is the point: ten administrators must
    not get ten allowances' worth of lanes. The KEY id names the person, so
    the per-minute ledger still shows who spent it.
    """
    allowance = playground_allowance()
    return ApiCaller(
        workspace_id=principal.workspace_id,
        project_id=str(allowance_row["id"]),
        service_account_id=None,
        key_id=f"playground_user_{principal.user_id}",
        scopes=frozenset(),
        models=tuple(allowed_models),
        limits=CallerLimits(**allowance),
        environment="live",
        public_id="console-playground",
    )


class _Slot:
    """A concurrency slot released exactly once, from whichever path gets
    there first."""

    def __init__(self, stack: contextlib.ExitStack) -> None:
        self._stack: Optional[contextlib.ExitStack] = stack

    def release(self) -> None:
        stack, self._stack = self._stack, None
        if stack is not None:
            stack.close()


class _Settlement:
    """Settles a playground run's quota reservation on the paths where nothing
    else will.

    2026-09-13, wave-3 re-verify: a run refused by the concurrency cap
    returned 429 but never settled the reservation, so the input-token
    ESTIMATE `quotas.reserve` had already added stayed charged in the minute
    and daily ledgers — one refused 66k-token run left input_tokens=66674 and
    the next runs were refused for rate_limit_error without the engine ever
    running. `/v1` gives the estimate back on the same refusal (`_nothing_ran`
    in publicapi/router.py); so does this now.

    `quotas.record_usage` settles a reservation at most once, so every path
    may call in: whichever gets there first — the real record from
    `_record_playground`, or one of these — is the one that counts.
    """

    def __init__(self, caller: ApiCaller, reservation: quotas.Reservation) -> None:
        self.caller = caller
        self.reservation = reservation

    async def _settle(self, input_tokens: Optional[int]) -> None:
        # Shielded: these run from `finally` blocks and exception handlers
        # that may themselves be executing under cancellation.
        work = asyncio.ensure_future(
            db.run_in_thread(
                functools.partial(
                    quotas.record_usage,
                    self.caller,
                    input_tokens,
                    0 if input_tokens == 0 else None,
                    "cancelled",
                    reservation=self.reservation,
                )
            )
        )
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a refusal must still be answered
            log.warning("a playground reservation could not be settled", exc_info=True)

    async def nothing_ran(self) -> None:
        """The engine was never reached: the estimate comes back in full."""
        await self._settle(0)

    async def may_have_run(self) -> None:
        """The engine may have generated and nothing measured it: settle,
        keeping the estimate charged — an unmeasured run is charged at its
        estimate, never at zero (`quotas.record_usage` with None)."""
        await self._settle(None)


class _SlotStreamingResponse(StreamingResponse):
    """A StreamingResponse that gives its concurrency slot back even when the
    body generator is never iterated.

    A client that disconnects before the first byte never starts the body
    generator, so a release in the generator's `finally` never runs and the
    slot is stranded until restart. The response object's own `__call__`
    always runs once the handler has returned it, so releasing there too (the
    release is idempotent) closes that path.

    The reservation has the same hole: `on_finish` is called from inside the
    body generator, so a body that never starts never settles it. When
    `settle` is given, a body that never started gives the estimate back — the
    engine is only reached by iterating it. A body that DID start is settled by
    `streaming.responses_sse`'s own shielded `on_finish`, and nothing here
    races it.
    """

    def __init__(
        self, *args: Any, slot: _Slot, settle: Optional[_Settlement] = None, **kwargs: Any
    ) -> None:
        content = args[0] if args else kwargs.pop("content")
        self._body_started = False

        async def watched() -> AsyncIterator[Any]:
            self._body_started = True
            try:
                async for chunk in content:
                    yield chunk
            finally:
                # Close the inner generator NOW, not at garbage collection:
                # its `finally` releases the slot and records the outcome.
                aclose = getattr(content, "aclose", None)
                if aclose is not None:
                    await aclose()

        if args:
            args = (watched(),) + tuple(args[1:])
        else:
            kwargs["content"] = watched()
        super().__init__(*args, **kwargs)
        self._slot = slot
        self._settle = settle

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[override]
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._slot.release()
            if self._settle is not None and not self._body_started:
                await self._settle.nothing_ran()


async def _record_playground(
    *,
    principal: Principal,
    caller: ApiCaller,
    reservation: quotas.Reservation,
    project: Optional[Dict[str, Any]],
    model: registry.PublicModel,
    request_id: str,
    outcome: streaming.StreamOutcome,
    streamed: bool,
) -> None:
    """One usage row per playground run, streamed or not, plus the quota
    ledgers' token spend.

    Both modes go through here so they cannot drift: a run that streamed and a
    run that did not cost the same GPU, and the analytics console must see
    both. A FAILED run is recorded too — a generation that burned engine time
    and then died is exactly the kind a capacity review must not lose.
    """
    usage = api_models.Usage.from_llm(outcome.usage)
    try:
        # Settling swaps the input estimate `reserve` charged for the measured
        # count, so the playground's window reflects what the engine did.
        await db.run_in_thread(
            functools.partial(
                quotas.record_usage,
                caller,
                None if usage is None else usage.input_tokens,
                None if usage is None else usage.output_tokens,
                outcome.status,
                reservation=reservation,
            )
        )
    except Exception:  # noqa: BLE001 — recording must never break a response
        log.warning("playground token spend was not written to the quota ledger", exc_info=True)
    await usage_ledger.record_async(
        user_id=principal.user_id,
        workspace_id=principal.workspace_id,
        conversation_id=None,
        # The request id doubles as the ledger's generation id: the unique
        # index on it is what makes a double-recorded run impossible, and a
        # playground run has no conversation to key on.
        generation_id=request_id,
        route=PLAYGROUND_ROUTE,
        effort=streaming.PUBLIC_EFFORT,
        model=model.internal,
        mode="playground",
        input_tokens=None if usage is None else usage.input_tokens,
        output_tokens=None if usage is None else usage.output_tokens,
        ttft_ms=outcome.ttft_ms,
        duration_ms=outcome.duration_ms,
        status=usage_ledger.OK if outcome.error is None else usage_ledger.ERROR,
        error_kind="" if outcome.error is None else outcome.error.code,
        meta={
            "public_model": model.id,
            "request_id": request_id,
            "project_id": None if project is None else project["id"],
            "streamed": bool(streamed),
            "source": "console_playground",
        },
    )


@router.post("/playground/execute")
async def playground_execute(
    request: Request,
    project_id: str = Query("", max_length=64),
    principal: Principal = Depends(require_capability(Cap.API_CONSOLE_ACCESS)),
):
    """Run a `/v1/responses`-shaped request from the console, with NO API KEY.

    THE POINT: a "try it" box that demands a live credential teaches people to
    mint long-lived keys and paste them into a browser. This endpoint is
    authenticated by the session and `api.console.access`, so the person who
    can already administer the platform can exercise it without creating a
    secret that outlives the experiment.

    WHAT IS SHARED WITH `/v1`, deliberately: the request type and its
    `extra="forbid"` validation, the COUNTED body cap, the model registry
    including the database's narrowing, the per-model output ceiling, the QUOTA
    ENGINE (reserve + concurrency slot, against the workspace's playground
    allowance), and — since 2026-09-13 — the generation pump itself:
    `streaming.run_to_completion` and `streaming.responses_sse`. The console
    used to carry its own copy of that pump, and the copy mapped every engine
    refusal to 500 `internal_error` where `/v1` answers 429 / 503 / 504 with a
    `Retry-After`, and never emitted `response.queued` or `response.failed`.
    One pump means a developer debugging in the console sees what their code
    will get.

    `project_id` is a QUERY parameter, not a body field: the body is the public
    request shape byte for byte, and nothing in a body may select a tenant
    (CONTRACT-3 §8). The project it names is resolved in the caller's
    workspace and contributes only its model allowlist — the run is charged to
    the playground allowance, never to that project's quota.
    """
    request_id = _request_id()
    limit = api_models.max_body_bytes()
    try:
        raw = await _read_capped_body(request, limit)
    except api_errors.ApiError as exc:
        return _api_error_response(exc, request_id)
    try:
        payload = json.loads(raw) if raw.strip() else None
    except ValueError:
        # The decoder's message quotes an offset into the caller's prompt.
        payload = None
    if not isinstance(payload, dict):
        return _api_error_response(
            api_errors.invalid_request("The request body must be a JSON object."),
            request_id,
        )

    project: Optional[Dict[str, Any]] = None
    if project_id:
        project = await _project_or_404(principal, project_id)

    try:
        parsed = api_models.parse_responses_request(payload)
        if parsed.background:
            raise api_errors.invalid_request(
                "The console playground runs a request and waits for it; "
                "background runs belong to a key.",
                param="background",
            )
        overrides = await db.run_in_thread(
            _model_overrides, principal.workspace_id
        )
        allowed = _list(project.get("allowed_models")) if project else []
        model = registry.resolve_public_model(
            parsed.model,
            allowed=allowed or None,
            overrides=overrides,
        )
        if model is None:
            # 404, never 403: a model this caller may not use and a model that
            # does not exist are the same answer (CONTRACT-3 §4).
            raise api_errors.model_not_found(parsed.model)
        default_out = registry.default_max_output_tokens() or FALLBACK_MAX_OUTPUT_TOKENS
        ceiling = model.max_output_tokens or default_out
        max_tokens = parsed.resolve_max_output_tokens(
            ceiling=ceiling, default=min(default_out, ceiling)
        )
    except api_errors.ApiError as exc:
        return _api_error_response(exc, request_id)

    messages = parsed.chat_messages()
    kind = "stream" if parsed.stream else "sync"
    spec = streaming.GenerationSpec(
        response_id=f"resp_{secrets.token_hex(12)}",
        model=model.id,
        messages=messages,
        max_tokens=max_tokens,
        temperature=0.2 if parsed.temperature is None else float(parsed.temperature),
        created_at=int(time.time()),
    )
    allowance_row = await db.run_in_thread(
        _ensure_playground_project, principal.workspace_id
    )
    caller = _playground_caller(principal, allowance_row, allowed)

    # THE GATE, IN FRONT OF ADMISSION (CONTRACT-3 §11), in the same order
    # `/v1` uses: the durable window first, then the in-process slot.
    try:
        reservation = await db.run_in_thread(
            functools.partial(
                quotas.reserve,
                caller,
                kind=kind,
                estimated_input_tokens=_estimate_input_tokens(messages),
                # The ceiling this run resolved, so the output reservation is
                # what it may actually generate rather than the global default.
                max_output_tokens=max_tokens,
            )
        )
    except api_errors.ApiError as exc:
        return _api_error_response(exc, request_id)
    settlement = _Settlement(caller, reservation)
    rate_headers = dict(reservation.headers())
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(quotas.concurrency_slot(caller, kind))
    except api_errors.ApiError as exc:
        stack.close()
        # Refused before the engine: give the reserved estimate back.
        await settlement.nothing_ran()
        return _api_error_response(exc, request_id, rate_headers)
    except BaseException:
        stack.close()
        await settlement.nothing_ran()
        raise
    slot = _Slot(stack)

    if parsed.stream:

        async def on_finish(outcome: streaming.StreamOutcome) -> None:
            await _record_playground(
                principal=principal,
                caller=caller,
                reservation=reservation,
                project=project,
                model=model,
                request_id=request_id,
                outcome=outcome,
                streamed=True,
            )

        async def frames() -> AsyncIterator[str]:
            try:
                async for frame in streaming.responses_sse(spec, on_finish=on_finish):
                    yield frame
            finally:
                slot.release()

        return _SlotStreamingResponse(
            frames(),
            slot=slot,
            settle=settlement,
            media_type="text/event-stream",
            headers={**streaming.SSE_HEADERS, **rate_headers, "X-Request-Id": request_id},
        )

    try:
        outcome = await streaming.run_to_completion(spec)
    except BaseException:
        # Raised or cancelled before `_record_playground`: the engine may have
        # run, so the estimate stays charged — but the reservation is settled
        # rather than left dangling.
        slot.release()
        await settlement.may_have_run()
        raise
    finally:
        slot.release()
    await _record_playground(
        principal=principal,
        caller=caller,
        reservation=reservation,
        project=project,
        model=model,
        request_id=request_id,
        outcome=outcome,
        streamed=False,
    )
    if outcome.error is not None:
        return _api_error_response(outcome.error, request_id, rate_headers)
    return JSONResponse(
        content=outcome.response().to_wire(),
        headers={**rate_headers, "X-Request-Id": request_id},
    )


def _estimate_input_tokens(messages: Sequence[Dict[str, str]]) -> int:
    """The same pessimistic estimate `/v1` charges the input window with
    (`context.estimate_messages`). Imported late: `context` pulls the
    tokenizer stack, and the console's other routes must not need it."""
    from .. import context

    return int(context.estimate_messages(list(messages)))
