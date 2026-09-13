"""Projects, service accounts and keys — what the console API calls.

The management half of the developer platform: the operations an
`api.console.access` session performs through `/admin`-style routes, as
opposed to what an API key does on `/v1`. Everything here is tenant-scoped by
a `workspace_id` the CALLER'S SESSION supplied, never a body did, and every
accessor underneath takes that workspace as a positional argument it puts in
the `WHERE` clause rather than in an `if` afterwards.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE: no secret material crosses this
boundary outwards. `db.api_key_by_public_id` returns `SELECT *`, which includes
`api_keys.key_hash` (the resolver needs it); `db.list_api_keys` and
`db.revoke_api_key` already project an explicit column list without it; `db.list_webhook_endpoints`
returns `secret` and `previous_secret` and says in its own docstring that they
are "server-side only, never to be returned by any API". A console route that
forwarded a row from either would publish them. So every row leaves here
through `public_key_view` / `public_project_view` / `public_service_account_view`,
which are ALLOW-LISTS — they name the columns that go out, rather than naming
the ones that do not. OWASP API3:2023 is explicit that the allow-list
direction is the one that survives a new column being added: a deny-list
silently starts leaking the day somebody adds `key_hash_v2`.

THE PLAINTEXT KEY EXISTS ONCE. `create_key` and `rotate_key` return a
`CreatedKey` carrying the token; it is never stored, never logged, never
returned by any read, and the dataclass redacts itself in `repr`. The console
copies the show-once dialog from `InviteDialog` (CONTRACT-3 §17).

WHAT THIS MODULE DOES NOT DO: authorization. Whether this session may create a
key is `rbac.Cap.API_KEYS_CREATE`, checked by the route before it gets here,
and a missing capability is a 404 rather than a 403 so the console's existence
is not disclosed (CONTRACT-3 §6). Keeping the check out of this module is what
lets the whole file be tested without a session.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .. import db
from ..config import settings
from . import keys
from .scopes import DEFAULT_SCOPES, parse_scopes, scope_names

log = logging.getLogger(__name__)

#: Columns that must never leave the server, whatever row they arrive on.
#: Named so a reviewer can grep for one place, and asserted by
#: `test_no_public_view_can_ever_carry_secret_material`.
SECRET_COLUMNS = frozenset({"key_hash", "secret", "previous_secret"})

#: What a project row looks like to the console. An allow-list: a column added
#: to `api_projects` later does not appear here until somebody decides it
#: should.
_PROJECT_FIELDS = (
    "id", "workspace_id", "name", "environment", "status",
    "allowed_models", "allowed_origins", "ip_allowlist",
    "rpm", "input_tpm", "output_tpm", "max_concurrency", "daily_token_quota",
    "max_input_tokens", "max_output_tokens", "retention_days",
    "metadata", "created_by", "created_at", "disabled_at",
)

#: What a key row looks like to the console. `key_hash` is absent, and that is
#: the entire point of this tuple.
_KEY_FIELDS = (
    "id", "public_id", "last_four", "project_id", "service_account_id",
    "workspace_id", "environment", "name", "scopes", "allowed_models",
    "rpm", "max_concurrency", "status", "expires_at",
    "created_by", "created_at", "last_used_at", "last_used_ip",
    "revoked_at", "revoked_by", "rotated_from", "rotation_expires_at",
)

_SERVICE_ACCOUNT_FIELDS = (
    "id", "project_id", "workspace_id", "name", "description", "status",
    "scopes", "allowed_models", "created_by", "created_at",
    "last_used_at", "disabled_at",
)


def _as_datetime(value: Any) -> Optional[datetime]:
    """A stored `timestamptz` back into a datetime.

    `db._row` turns every timestamp into an ISO string on the way out, so a
    value read from one row and written straight into another would travel as
    text. PostgreSQL would usually cast it and the bug would surface only
    where it did not — so it is converted here, once, rather than trusted.
    """
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _view(row: Optional[Mapping[str, Any]], fields: Sequence[str]) -> Optional[Dict[str, Any]]:
    """One row, reduced to the named columns.

    Missing columns are simply absent rather than `None`: a view that invents
    a key the row did not have makes a schema drift look like a null value.
    """
    if row is None:
        return None
    out = {name: row[name] for name in fields if name in row}
    leaked = SECRET_COLUMNS & set(out)
    if leaked:  # pragma: no cover - the allow-lists above make this impossible
        # Belt and braces. If somebody ever adds a secret column name to one
        # of the tuples above, fail loudly here rather than serve it.
        raise AssertionError(f"refusing to return secret columns: {sorted(leaked)}")
    return out


def public_project_view(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    return _view(row, _PROJECT_FIELDS)


def public_key_view(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """A key row with `key_hash` removed.

    `db.list_api_keys` and `db.revoke_api_key` already select an explicit
    column list without `key_hash` (corrected 2026-09-13: this comment used to
    say they did `SELECT *`). Only `db.api_key_by_public_id` still returns the
    digest, because the resolver must compare it. This view is the second
    door anyway: an allow-list, so a row from any accessor — including one
    added later that forgets to project — cannot carry a digest out.
    """
    return _view(row, _KEY_FIELDS)


def public_service_account_view(
    row: Optional[Mapping[str, Any]],
) -> Optional[Dict[str, Any]]:
    return _view(row, _SERVICE_ACCOUNT_FIELDS)


@dataclass(frozen=True, eq=False)
class CreatedKey:
    """A newly minted key: the row, and the plaintext, this once.

    `eq=False` for the same reason `keys.MintedKey` has it — dataclass `==`
    compares field by field with Python's short-circuiting `==`, which is not
    constant time, and one of these fields is a credential.
    """

    #: The console shows this once and never again. Not stored anywhere.
    token: str
    #: The stored row, already reduced by `public_key_view`.
    key: Dict[str, Any]

    @property
    def public_id(self) -> str:
        return str(self.key.get("public_id") or "")

    def __repr__(self) -> str:
        # A repr reaches tracebacks, debuggers, pytest failure output and the
        # log line somebody adds at 2am. The token may reach none of them.
        return (
            f"CreatedKey(public_id={self.public_id!r}, "
            f"last_four={self.key.get('last_four')!r}, token='<redacted>')"
        )

    __str__ = __repr__


@dataclass(frozen=True, eq=False)
class RotatedKey:
    """A rotation: the replacement, and what must happen to the old key.

    `previous_expires_at` is when the old credential stops working — written
    onto the old row as `rotation_expires_at` and enforced by the resolver.
    `previous_revoked` says whether it was revoked outright (zero overlap).
    """

    created: CreatedKey
    previous_key_id: str
    previous_public_id: str
    previous_expires_at: datetime
    previous_revoked: bool
    #: The OLD key as the claim left it (dated or revoked), already reduced by
    #: `public_key_view` — what a console response and its audit row need
    #: without a second read.
    previous: Optional[Dict[str, Any]] = None

    @property
    def token(self) -> str:
        return self.created.token

    def __repr__(self) -> str:
        return (
            f"RotatedKey(previous_public_id={self.previous_public_id!r}, "
            f"previous_expires_at={self.previous_expires_at.isoformat()!r}, "
            f"previous_revoked={self.previous_revoked!r}, "
            f"created={self.created!r})"
        )

    __str__ = __repr__


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


def create_project(
    workspace_id: str,
    name: str,
    *,
    environment: str = "live",
    created_by: Optional[int] = None,
    **limits: Any,
) -> Dict[str, Any]:
    """A new project in this workspace.

    `environment` is normalised through `keys._normalise_environment` so a
    project and the keys minted inside it can never disagree about which of
    the two worlds they are in — `db.create_api_key` copies the environment
    FROM the project and raises on a disagreement, and this is the other half
    of that guarantee.

    `**limits` is forwarded to `db.create_api_project`, which ignores what it
    was not given and leaves the DDL defaults (CONTRACT-3 §12's numbers) in
    place. Anything it does not recognise is its own `TypeError`, which is the
    right failure: a console form posting `rpmm=1000` must not create a
    project that silently ignores it.
    """
    row = db.create_api_project(
        workspace_id,
        name,
        keys._normalise_environment(environment),
        created_by=created_by,
        **limits,
    )
    return public_project_view(row)  # type: ignore[return-value]


def get_project(project_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
    """One project, or None — including when it is another workspace's, which
    reads as missing rather than forbidden (no cross-tenant existence oracle)."""
    return public_project_view(db.get_api_project(project_id, workspace_id))


def list_projects(
    workspace_id: str, *, include_disabled: bool = True, limit: int = 100
) -> List[Dict[str, Any]]:
    return [
        public_project_view(row)  # type: ignore[misc]
        for row in db.list_api_projects(
            workspace_id, include_disabled=include_disabled, limit=limit
        )
    ]


def update_project(
    project_id: str, workspace_id: str, /, **fields: Any
) -> Optional[Dict[str, Any]]:
    """Change a project's allow-listed columns; None when it is not this
    workspace's.

    The tenancy arguments are POSITIONAL-ONLY, exactly as
    `db.update_api_project`'s are and for the same reason: `**fields` is built
    from a request body, and a body carrying its own `workspace_id` must land
    in `fields` — where it is refused as not updatable — rather than colliding
    with the argument that scopes the statement.
    """
    return public_project_view(db.update_api_project(project_id, workspace_id, **fields))


def disable_project(project_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
    """Switch a project off. Every key in it stops working on the NEXT request
    — the resolver reads the project row every time and there is no cached
    state anywhere — and each one answers the same generic 401 as an unknown
    key, so nobody holding one learns that the project exists."""
    return update_project(project_id, workspace_id, status="disabled")


def enable_project(project_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
    return update_project(project_id, workspace_id, status="active")


# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------


def create_service_account(
    project_id: str,
    workspace_id: str,
    name: str,
    *,
    description: str = "",
    scopes: Optional[Iterable[Any]] = None,
    allowed_models: Optional[Sequence[str]] = None,
    created_by: Optional[int] = None,
) -> Dict[str, Any]:
    """A named integration inside a project.

    ITS SCOPES ARE A CEILING, NOT A GRANT. A key attached to this account can
    hold at most these scopes (`resolver._effective_scopes` intersects), so
    the account is where an administrator caps a whole integration in one
    place. An account created with no explicit scopes gets `DEFAULT_SCOPES`
    written explicitly rather than the DDL's `'[]'` — an empty list is
    ambiguous between "no policy" and "no permissions", and writing the
    intended value removes the ambiguity from the row rather than from the
    reader.
    """
    wanted = DEFAULT_SCOPES if scopes is None else parse_scopes(scopes)
    row = db.create_service_account(
        project_id,
        workspace_id,
        name,
        description=description,
        scopes=scope_names(wanted),
        allowed_models=list(allowed_models) if allowed_models is not None else None,
        created_by=created_by,
    )
    return public_service_account_view(row)  # type: ignore[return-value]


def list_service_accounts(project_id: str, workspace_id: str) -> List[Dict[str, Any]]:
    return [
        public_service_account_view(row)  # type: ignore[misc]
        for row in db.list_service_accounts(project_id, workspace_id)
    ]


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def list_keys(
    project_id: str, workspace_id: str, *, include_revoked: bool = True
) -> List[Dict[str, Any]]:
    """A project's keys with `key_hash` stripped. There is no other way to
    read a key out of this module."""
    return [
        public_key_view(row)  # type: ignore[misc]
        for row in db.list_api_keys(
            project_id, workspace_id, include_revoked=include_revoked
        )
    ]


def get_key(key_id: str, project_id: str, workspace_id: str) -> Optional[Dict[str, Any]]:
    """One key, by id, inside a project this workspace owns.

    Implemented as a filter over `list_api_keys` because V34 ships no
    `get_api_key(key_id, workspace_id)` accessor — noted for the database
    owner. The tenancy is still the database's decision, not this function's:
    `list_api_keys` puts both `project_id` and `workspace_id` in its `WHERE`
    clause, so a key belonging to another tenant is never in the list to be
    filtered.
    """
    for row in db.list_api_keys(project_id, workspace_id, include_revoked=True):
        if str(row.get("id")) == str(key_id):
            return public_key_view(row)
    return None


def create_key(
    project_id: str,
    workspace_id: str,
    name: str,
    *,
    scopes: Optional[Iterable[Any]] = None,
    service_account_id: Optional[str] = None,
    allowed_models: Optional[Sequence[str]] = None,
    rpm: Optional[int] = None,
    max_concurrency: Optional[int] = None,
    expires_at: Optional[datetime] = None,
    created_by: Optional[int] = None,
    lifetime: Optional[timedelta] = None,
    environment: Optional[str] = None,
) -> CreatedKey:
    """Mint a key for this project and store only its digest.

    THE ORDER MATTERS. The token is minted, the digest computed, and the row
    written — and only then is the plaintext handed back. If the INSERT fails
    (a project in another workspace, a service account from another project),
    `db.create_api_key` raises and no key exists in any form; the caller never
    receives a token for a key that was not stored, which would be a
    credential that authenticates to nothing and looks real.

    `expires_at` defaults to `keys.default_expires_at()` — ninety days.
    STANDARDS.md (OWASP Secrets Management): rotation bounds the useful life
    of a stolen key only if the key has a life to bound. Pass
    `expires_at=False` explicitly for a key that never expires, which V34
    permits (`expires_at` is nullable) and the console is expected to warn
    about.

    `scopes=None` means `DEFAULT_SCOPES`, the narrow set: models, read
    responses, write responses. Usage counters and anything added later are
    opted into. `scopes=[]` means a key that can call nothing, which is a real
    and occasionally useful state — it is how you park a key.
    """
    env = keys._normalise_environment(environment) if environment else None
    project = db.get_api_project(project_id, workspace_id)
    if project is None:
        raise ValueError(f"no api_project {project_id!r} in this workspace")
    minted = keys.mint_key(env or str(project["environment"]))
    wanted = DEFAULT_SCOPES if scopes is None else parse_scopes(scopes)

    if expires_at is False:  # type: ignore[comparison-overlap]
        expiry: Optional[datetime] = None
    elif expires_at is None:
        expiry = keys.default_expires_at(
            lifetime=lifetime or keys.DEFAULT_KEY_LIFETIME
        )
    else:
        expiry = expires_at

    row = db.create_api_key(
        project_id,
        workspace_id,
        name,
        minted.public_id,
        keys.key_digest(minted.secret),
        minted.last_four,
        service_account_id=service_account_id,
        scopes=scope_names(wanted),
        allowed_models=list(allowed_models) if allowed_models is not None else None,
        rpm=rpm,
        max_concurrency=max_concurrency,
        expires_at=expiry,
        created_by=created_by,
        environment=minted.environment,
    )
    log.info(
        "api key created: %s for project %s",
        keys.redact(minted.token, environment=str(row["environment"])),
        project_id,
    )
    return CreatedKey(token=minted.token, key=public_key_view(row))  # type: ignore[arg-type]


def revoke_key(
    key_id: str, workspace_id: str, *, revoked_by: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """Kill a key now. Idempotent; None when it is not this workspace's.

    Takes effect on the next request everywhere, because the resolver reads
    the row on every request and nothing caches key state (CONTRACT-3 §5).
    A second call keeps the FIRST `revoked_at` and `revoked_by`: rewriting
    them would destroy the only record of who actually did it.
    """
    return public_key_view(db.revoke_api_key(key_id, workspace_id, revoked_by=revoked_by))


class RotationRefused(ValueError):
    """A rotation that must not happen, with the status a route should answer.

    `status == 404` — no such key in this project and workspace (including a
    key of another tenant: the same answer, so existence is not disclosed).
    `status == 409` — the key exists but is revoked, expired or already
    inside a rotation, so exactly one of two racing rotations wins.

    A `ValueError` subclass so every caller written against the earlier
    `ValueError` keeps working.
    """

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = int(status)


def rotate_key(
    key_id: str,
    project_id: str,
    workspace_id: str,
    *,
    overlap: Optional[timedelta] = None,
    created_by: Optional[int] = None,
    name: Optional[str] = None,
    now: Optional[datetime] = None,
) -> RotatedKey:
    """Mint a replacement for `key_id` and date the old key out.

    Rotation and revocation are DIFFERENT OPERATIONS with opposite safety
    properties, which is why `keys.plan_rotation` and `keys.plan_revocation`
    are separate verbs and why this function takes an explicit `overlap`.

    * `overlap == 0` — the compromise path. The old key is revoked FIRST, in
      the statement that claims the rotation, before anything is minted.
      `previous_revoked` is True. If minting then fails the old key stays
      dead: a leaked credential staying live because a second INSERT failed
      would be the wrong way round.
    * `overlap > 0` — the planned path (default
      `settings.public_api_key_rotation_overlap_hours`). The OLD row gets
      `rotation_expires_at = now + overlap`, and the resolver refuses that key
      from that instant on — enforced on every request, with no sweep needed.
      The replacement carries `rotated_from` and NO `rotation_expires_at` of
      its own: that column means "when does THIS key stop working", and
      writing the predecessor's deadline onto the replacement would kill the
      new key when the old one died.

    WHY THE CLAIM IS A CONDITIONAL UPDATE (2026-09-13, wave-2 review). The
    first cut read the key, checked it, and minted in separate transactions,
    so two concurrent rotations both passed and left two live replacements;
    and nothing wrote `rotation_expires_at` at all, so a rotated-out key lived
    until someone revoked it by hand. The UPDATE below is the check and the
    write in one statement: it only matches an ACTIVE key of this project and
    workspace that is not already inside a rotation, so exactly one of two
    racing calls gets a row and the other is refused. If minting fails after a
    planned claim, the deadline is taken back off the old key.

    THE ONLY ROTATION IMPLEMENTATION (2026-09-13, wave-3 re-verify). The
    console route had its own copy that held `pg_advisory_xact_lock` on one
    pooled connection while `db.list_api_keys`, `db.create_api_key` and
    `db.revoke_api_key` each checked out a SECOND one — the pattern that, at
    pool_max concurrency, stalls the whole app (chat included) until
    PoolTimeout — and it took its default overlap from a different constant.
    This function is the one both must call. It never holds a connection
    while opening another: the pre-read, the claim and the insert each run on
    their own checkout, one after the other, and the claim — a conditional
    UPDATE whose row lock and predicate are the whole race decision — commits
    before anything else is opened. No lock is waited on while a connection
    is held beyond that single UPDATE's row lock.

    Everything that can fail WITHOUT touching the database — the overlap
    check, minting, and the pepper (`keys.PepperUnavailable`) — happens
    BEFORE the claim, so a deployment with no pepper can never revoke a key
    and then fail to mint its replacement.

    Refusals raise `RotationRefused` (a ValueError) carrying 404 or 409.

    The plaintext of the new key is returned once, in `RotatedKey.created`,
    and nowhere else. The replacement inherits the old key's scopes, model
    allowlist, per-key limits and expiry AS THE CLAIM READ THEM, because a
    rotation must not quietly change what a credential may do.
    """
    if overlap is None:
        overlap = timedelta(hours=float(settings.public_api_key_rotation_overlap_hours))
    moment = keys._now(now)

    previous = None
    for row in db.list_api_keys(project_id, workspace_id, include_revoked=True):
        if str(row.get("id")) == str(key_id):
            previous = row
            break
    if previous is None:
        raise RotationRefused(
            f"no api_key {key_id!r} in project {project_id!r}", status=404
        )

    # Validates the overlap (negative or beyond 30 days is refused, never
    # clamped) and mints the replacement in memory. Nothing is written yet.
    plan = keys.plan_rotation(
        str(previous["public_id"]),
        str(previous["environment"]),
        overlap=overlap,
        now=moment,
    )
    immediate = overlap <= timedelta(0)
    # Before the claim: a missing pepper raises here, while the old key is
    # still untouched.
    digest = keys.key_digest(plan.minted.secret)
    returning = ", ".join(db._API_KEY_PUBLIC_COLUMNS)

    with db.connection() as con:
        if immediate:
            claimed = con.execute(
                "UPDATE api_keys SET status = 'revoked', "
                "       revoked_at = COALESCE(revoked_at, %s), "
                "       revoked_by = COALESCE(revoked_by, %s), "
                "       rotation_expires_at = LEAST(COALESCE(rotation_expires_at, %s), %s) "
                " WHERE id = %s AND project_id = %s AND workspace_id = %s "
                "   AND status = 'active' "
                f"RETURNING {returning}",
                (
                    moment,
                    None if created_by is None else int(created_by),
                    moment,
                    moment,
                    key_id,
                    project_id,
                    workspace_id,
                ),
            ).fetchone()
        else:
            claimed = con.execute(
                "UPDATE api_keys SET rotation_expires_at = %s "
                " WHERE id = %s AND project_id = %s AND workspace_id = %s "
                "   AND status = 'active' AND rotation_expires_at IS NULL "
                "   AND (expires_at IS NULL OR expires_at > %s) "
                f"RETURNING {returning}",
                (plan.previous_expires_at, key_id, project_id, workspace_id, moment),
            ).fetchone()
    if claimed is None:
        raise RotationRefused(
            f"api_key {key_id!r} is not an active key that can be rotated "
            "(revoked, expired, or already inside a rotation)",
            status=409,
        )
    # The row as the winning claim saw it, not the pre-read: a scope change
    # that committed in between is inherited rather than silently undone.
    current = dict(claimed)

    try:
        row = db.create_api_key(
            project_id,
            workspace_id,
            name or str(current.get("name") or previous["name"]),
            plan.minted.public_id,
            digest,
            plan.minted.last_four,
            service_account_id=current.get("service_account_id"),
            scopes=scope_names(parse_scopes(current.get("scopes"))),
            allowed_models=list(current.get("allowed_models") or []) or None,
            rpm=current.get("rpm"),
            max_concurrency=current.get("max_concurrency"),
            expires_at=_as_datetime(current.get("expires_at")),
            created_by=created_by,
            environment=plan.minted.environment,
            rotated_from=plan.rotated_from,
        )
    except BaseException:
        if not immediate:
            try:
                with db.connection() as con:
                    con.execute(
                        "UPDATE api_keys SET rotation_expires_at = NULL "
                        " WHERE id = %s AND workspace_id = %s "
                        "   AND rotation_expires_at = %s",
                        (key_id, workspace_id, plan.previous_expires_at),
                    )
            except Exception:  # noqa: BLE001 - never mask the original failure
                log.exception(
                    "could not take the rotation deadline back off key %s", key_id
                )
        raise

    log.info(
        "api key rotated: %s replaces %s (old key %s)",
        keys.redact(plan.minted.token, environment=str(row["environment"])),
        previous["public_id"],
        "revoked now" if immediate else f"expires {plan.previous_expires_at.isoformat()}",
    )
    return RotatedKey(
        created=CreatedKey(token=plan.minted.token, key=public_key_view(row)),  # type: ignore[arg-type]
        previous_key_id=str(previous["id"]),
        previous_public_id=str(previous["public_id"]),
        previous_expires_at=plan.previous_expires_at,
        previous_revoked=immediate,
        previous=public_key_view(current),
    )


def revoke_key_now(
    key_id: str,
    project_id: str,
    workspace_id: str,
    *,
    created_by: Optional[int] = None,
) -> RotatedKey:
    """Rotate with no grace period at all — the compromise verb.

    A separate name rather than a default argument, so the code that handles a
    leaked key reads as what it is and cannot be reached by forgetting to pass
    `overlap`."""
    return rotate_key(
        key_id,
        project_id,
        workspace_id,
        overlap=timedelta(0),
        created_by=created_by,
    )


__all__ = [
    "SECRET_COLUMNS",
    "CreatedKey",
    "RotatedKey",
    "RotationRefused",
    "create_key",
    "create_project",
    "create_service_account",
    "disable_project",
    "enable_project",
    "get_key",
    "get_project",
    "list_keys",
    "list_projects",
    "list_service_accounts",
    "public_key_view",
    "public_project_view",
    "public_service_account_view",
    "revoke_key",
    "revoke_key_now",
    "rotate_key",
    "update_project",
]
