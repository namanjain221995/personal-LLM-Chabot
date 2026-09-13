"""One Bearer token in, one `ApiCaller` out — CONTRACT-3 §4.

This is the whole identity ladder for `/v1`, in one function, and every `/v1`
route is required to reach it through `resolve_api_caller()` rather than
reading the `Authorization` header itself. STANDARDS.md says why in the
OWASP API5 entry: deny-by-default is a property of having exactly one place
that can say yes, and a route that parses its own credential is a route that
can forget a rung of the ladder.

    Authorization: Bearer tsk_live_<public_id>_<secret><checksum>
      → shape + CRC32, offline                 → 401 invalid_api_key
      → api_keys row by public_id              → 401 invalid_api_key
      → environment reconciliation             → 401 invalid_api_key
      → HMAC digest, constant time             → 401 invalid_api_key
      → key status / expiry / rotation window  → 401 invalid_api_key
      → service account enabled                → 401 invalid_api_key
      → project enabled                        → 401 invalid_api_key
      → workspace enabled                      → 401 invalid_api_key
      → project ip_allowlist                   → 401 invalid_api_key
      → touch_api_key(key_id, ip), once
      → ApiCaller(workspace, project, service account, key, scopes, models,
                  limits, environment)

ONE REFUSAL, NINE REASONS. Every rung above answers the SAME
`401 invalid_api_key` with the same message, and that is the single most
important property in this file. A surface that distinguishes "revoked" from
"unknown" tells whoever is holding a leaked key that the key is real and was
noticed; one that distinguishes "project disabled" from "expired" tells them
the tenant exists. STANDARDS.md calls the general shape the 403-vs-404
existence oracle and warns that a differing body, a differing header set OR a
measurably different latency reopens it — which is why the digest is computed
before any status is looked at (so revoked, expired and disabled all do
identical work) and why an unknown `public_id` is charged the cost of an HMAC
against a fixed dummy digest before it is refused.

CONTRACT-3 §4's ladder sketch says `403` for a disabled workspace. This file
answers `401` there too, deliberately: a 403 means "your credential is good,
your privileges are not", which is precisely the fact that must not be
disclosed to the bearer of a key belonging to a workspace someone has just
switched off. The divergence is recorded in the wave notes for the contract
owner. `403 insufficient_scope` remains a 403 — by then the caller has proved
it holds a live credential for the project, so RFC 6750's challenge is the
right answer and `scopes.ScopeRequirement.check()` raises it, not this module.

THE COOKIE IS NOT READ, STRUCTURALLY. `resolve_api_caller` takes one string:
the `Authorization` header. There is no parameter through which a `Cookie`
could arrive, so `/v1` cannot become the confused deputy CONTRACT-3 §1
describes — a route that accepted both credentials would be drivable by any
page on the internet with a signed-in visitor's cookie. `authn/principal.py`
is the browser identity and nothing here imports it.
"""
from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, FrozenSet, Mapping, Optional, Sequence, Tuple

from .. import db
from ..publicapi import errors
from . import keys
from .scopes import Scope, UnknownScopeError, parse_scopes

log = logging.getLogger(__name__)

#: The scheme, case-insensitively. RFC 6750 §2.1: `credentials = "Bearer"
#: 1*SP b64token`. Anything else — `Basic`, `ApiKey`, a bare token, a pasted
#: cookie — is the same generic 401.
BEARER_SCHEME = "bearer"

#: A well-formed HMAC-SHA256 hex digest that no secret produces, used to pay
#: the HMAC cost on the unknown-key path. `verify_secret` against it is always
#: False; what it buys is that "this public id exists" and "it does not" cost
#: roughly the same amount of arithmetic. It does not equalise the database
#: round trip, and this file does not pretend otherwise — see `_charge_hmac`.
_DUMMY_DIGEST = "0" * 64


# ---------------------------------------------------------------------------
# The resolved identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallerLimits:
    """The ceilings this request is measured against (CONTRACT-3 §12).

    Resolved ONCE, here, from the project row and then narrowed by the key
    row, so the quota engine never has to decide which of two numbers wins —
    a question two call sites would answer differently on a bad day.

    `rpm` and `max_concurrency` are nullable on `api_keys` and NULL means
    "inherit the project" (SCHEMA-V34). A key may only narrow: a key asking
    for 600 requests a minute inside a 60-a-minute project gets 60. Letting
    the key widen would make `api_keys.rpm` a privilege-escalation field on a
    row the console can edit.

    TWO PARTITIONS, NOT ONE NUMBER (2026-09-13, wave-2 review). The first cut
    carried only `min(project, key)` and the quota engine compared it with a
    counter PER KEY — so ten keys in a rpm=60 project got 600 requests a
    minute and 40 concurrent generations on the NORMAL lanes the chat app
    shares. The project ceiling is now carried on its own
    (`project_rpm`, `project_max_concurrency`) and is enforced against the
    SUM over every key in the project; the key's own number
    (`key_rpm`, `key_max_concurrency`, None = inherit) is a second, narrower
    check on that key's share alone. `rpm` and `max_concurrency` remain the
    tightest ceiling that applies to this key, which is what a header or a
    console reads.

    ZERO IS ZERO. A stored 0 refuses everything; only NULL inherits.
    """

    rpm: int
    input_tpm: int
    output_tpm: int
    max_concurrency: int
    daily_token_quota: int
    #: The model's own ceilings still apply on top of these; None means the
    #: project expressed no opinion and the registry's number stands.
    max_input_tokens: Optional[int] = None
    max_output_tokens: Optional[int] = None
    #: The PROJECT's ceilings, counted across all of its keys. None only on a
    #: hand-built instance, where `rpm` / `max_concurrency` stand in.
    project_rpm: Optional[int] = None
    project_max_concurrency: Optional[int] = None
    #: This key's own tightening, counted over this key alone. None = inherit.
    key_rpm: Optional[int] = None
    key_max_concurrency: Optional[int] = None

    @property
    def project_rpm_limit(self) -> int:
        return int(self.rpm if self.project_rpm is None else self.project_rpm)

    @property
    def project_concurrency_limit(self) -> int:
        return int(
            self.max_concurrency
            if self.project_max_concurrency is None
            else self.project_max_concurrency
        )


@dataclass(frozen=True)
class ApiCaller:
    """Who is calling `/v1`, decided before any work happens.

    NOT a `Principal`. CONTRACT-3 §4 is explicit that the browser identity and
    the machine identity are different types on purpose: a `Principal` carries
    a person, a role and a capability set resolved from a session; an
    `ApiCaller` carries a tenant, a project and a scope set resolved from a
    credential that belongs to a machine. OWASP is blunt that "API keys should
    not be used for user authentication" — keeping the two types apart is what
    stops a route written for one from silently accepting the other.

    Frozen, because everything downstream of the ladder — the quota engine,
    the model registry, the recorder — reads this and must not be able to edit
    what was decided about the caller. Nothing in a request body may change
    any field here (CONTRACT-3 §8).
    """

    workspace_id: str
    project_id: str
    service_account_id: Optional[str]
    key_id: str
    scopes: FrozenSet[Scope]
    #: The model ids this key may address, already narrowed by project,
    #: service account and key. Empty means "no narrowing at any level", which
    #: `registry.resolve_public_model(allowed=…)` reads as the full public
    #: catalogue — not as "nothing".
    models: Tuple[str, ...]
    limits: CallerLimits
    environment: str

    # --- carried for the router and the log line, not part of §4's tuple ----
    #: The public half of the key. Safe to log (CONTRACT-3 §5) and the only
    #: identifier support can use to find a key without touching a secret.
    public_id: str = ""
    #: `api_projects.allowed_origins`, needed by the §3 CORS check on the
    #: ACTUAL request. Resolved here so the router does not re-read the
    #: project row it already has.
    allowed_origins: Tuple[str, ...] = ()
    #: The address the request came from, as the caller of this function
    #: reported it. Already checked against the project's `ip_allowlist`.
    ip: Optional[str] = None

    def redacted_key(self) -> str:
        """`tsk_live_<public_id>_<redacted>` — the only form of this key that
        may appear in a log line or an audit row, built from the STORED
        environment rather than the presented one."""
        return f"{keys.KEY_PREFIX}_{self.environment}_{self.public_id}_<redacted>"

    def has_scope(self, scope: Scope) -> bool:
        return scope in self.scopes


# ---------------------------------------------------------------------------
# The workspace rung, which V34 gave no column for
# ---------------------------------------------------------------------------

WorkspaceStatusReader = Callable[[str], str]

_workspace_status_reader: Optional[WorkspaceStatusReader] = None


def configure_workspace_status(reader: Optional[WorkspaceStatusReader]) -> None:
    """Tell the resolver how to find out whether a workspace is switched off.

    CONTRACT-3 §4 has a workspace rung. SCHEMA-V34 has no `workspaces.status`
    column — `workspaces` is `id`, `name`, `created_at`, plus the two jsonb
    policy blobs V17 and V18 added — and this wave does not own `db.py`, so it
    cannot add one. Rather than pretend the rung is enforced, the seam is
    named here and left injectable, exactly as `keys.configure_pepper_store`
    names the seam for the pepper store.

    WITH NO READER WIRED, EVERY WORKSPACE IS ACTIVE. That is not a weakened
    check: there is no way to disable a workspace in this schema, so "active"
    is the only answer the data can give, and it is the answer the platform
    gives today. What the seam buys is that the check EXISTS, is exercised by
    a test, and needs one line of wiring rather than a new code path the day
    the column lands. The wave notes ask the database owner for
    `workspaces.status text NOT NULL DEFAULT 'active' CHECK (status IN
    ('active','disabled'))`.

    The reader is given a workspace id and returns a status string; anything
    other than `"active"` refuses the request, so a reader that raises or
    returns nonsense fails CLOSED. Pass `None` to unwire it.
    """
    global _workspace_status_reader
    _workspace_status_reader = reader


def workspace_status(workspace_id: str) -> str:
    """`"active"` unless a wired reader says otherwise. Never raises: a reader
    that blows up is treated as a refusal, because a workspace whose status
    cannot be determined is not one to serve API traffic for."""
    reader = _workspace_status_reader
    if reader is None:
        return "active"
    try:
        return str(reader(workspace_id) or "").strip().lower()
    except Exception:  # noqa: BLE001 - a broken reader must not be a 500
        log.exception("workspace status reader failed for %r", workspace_id)
        return "unavailable"


# ---------------------------------------------------------------------------
# Small readers over rows that arrive as JSON-ish dicts
# ---------------------------------------------------------------------------


def _as_datetime(value: Any) -> Optional[datetime]:
    """`db._row` turns every `timestamptz` into an ISO string on the way out,
    so a row's `expires_at` is text and `keys.is_expired` wants a datetime.
    Anything unparseable is None — i.e. "no expiry recorded", which `is_expired`
    already treats as a deliberate operator choice rather than an accident."""
    if value is None or isinstance(value, datetime):
        return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _string_list(value: Any) -> Tuple[str, ...]:
    """A jsonb array column as a tuple of non-blank strings. A column holding
    something that is not a list reads as empty rather than raising: the
    resolver's job is to refuse credentials, not to crash on a malformed
    configuration row that an operator can still fix."""
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item or "").strip())


def _narrow(current: Optional[Tuple[str, ...]], level: Sequence[str]) -> Optional[Tuple[str, ...]]:
    """Intersect one narrowing level into the running allowlist.

    An EMPTY list at a level means that level expressed no opinion, not "this
    level permits nothing" — `api_projects.allowed_models` defaults to `'[]'`
    in the DDL and every project would otherwise be born unable to call
    anything. A NON-empty list narrows, and narrowing only ever shrinks:
    a key cannot name a model its project does not allow.
    """
    values = _string_list(level)
    if not values:
        return current
    if current is None:
        return values
    kept = tuple(item for item in current if item in set(values))
    return kept


def _status_of(row: Optional[Mapping[str, Any]]) -> str:
    return str((row or {}).get("status") or "").strip().lower()


# ---------------------------------------------------------------------------
# The header
# ---------------------------------------------------------------------------


def bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    """The token out of `Authorization: Bearer <token>`, or None.

    None — never a reason — for a missing header, a non-Bearer scheme, an
    empty credential, a header carrying more than two parts, or a value that
    is not a string at all. Every one of those becomes the same 401 upstairs.

    A COOKIE OFFERED HERE IS JUST A BAD TOKEN. `ts_session=abc123` has no
    scheme, so it fails the split and is refused like any other malformed
    credential; `Bearer ts_session=abc123` passes the split and then fails
    `keys.split_key`. Neither is special-cased, because a special case is a
    branch that can be got wrong — the surface simply has no code path that
    reads a cookie.
    """
    if not isinstance(authorization_header, str):
        return None
    parts = authorization_header.strip().split()
    if len(parts) != 2:
        return None
    scheme, token = parts
    if scheme.lower() != BEARER_SCHEME:
        return None
    return token or None


# ---------------------------------------------------------------------------
# The IP allowlist
# ---------------------------------------------------------------------------


def ip_allowed(ip: Optional[str], allowlist: Sequence[str]) -> bool:
    """Is this address inside the project's `ip_allowlist`?

    An EMPTY allowlist permits everything: the feature is opt-in, and a
    project that has not configured it must keep working. A NON-empty one is
    a closed list, and an address we do not know — `ip is None`, because the
    deployment does not trust a forwarding header — fails it. Fail-closed is
    the only sane reading: the operator asked for "only from these
    addresses", and "we could not tell" is not one of them.

    Entries may be single addresses or CIDR networks, v4 or v6. A malformed
    entry is skipped rather than treated as a match — a typo in the console
    must not silently open the list.

    STANDARDS.md (Stripe, Cloudflare) recommends this on every live key
    precisely because it turns a stolen key into one that only works from the
    customer's own infrastructure, and because the block is itself a
    high-fidelity compromise signal.
    """
    entries = _string_list(allowlist)
    if not entries:
        return True
    if not ip:
        return False
    address = _parse_address(ip)
    if address is None:
        return False
    for entry in entries:
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            log.warning("ignoring malformed ip_allowlist entry %r", entry)
            continue
        if address.version == network.version and address in network:
            return True
    return False


def _parse_networks(entries: Sequence[str]) -> Tuple[Any, ...]:
    networks = []
    for entry in entries or ():
        try:
            networks.append(ipaddress.ip_network(str(entry).strip(), strict=False))
        except ValueError:
            log.warning("ignoring malformed trusted proxy entry %r", entry)
    return tuple(networks)


def _parse_address(value: Any) -> Optional[Any]:
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    # An IPv4-mapped IPv6 peer (`::ffff:10.0.0.2`, what a dual-stack socket
    # reports) is the IPv4 address for every purpose here; comparing it as v6
    # would make a v4 allowlist or trusted-proxy entry silently never match.
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _in_any(address: Any, networks: Sequence[Any]) -> bool:
    return any(
        address.version == network.version and address in network
        for network in networks
    )


def client_address(
    peer: Optional[str],
    forwarded_for: Optional[str] = None,
    *,
    trusted_proxies: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """The address a project's `ip_allowlist` is checked against.

    WHY THIS EXISTS (2026-09-13, wave-2 review). The router used to take the
    first `X-Forwarded-For` entry whenever `AUTH_TRUST_PROXY_HEADERS` was on —
    and production runs with it on while the orchestrator listens on
    0.0.0.0:8080, so a LAN client holding a leaked key could claim any
    allowlisted address by sending the header itself. The header is a string
    the CLIENT wrote unless a proxy we trust wrote it.

    THE RULE:

    * the socket peer is NOT a trusted proxy → the peer is the address, and
      `X-Forwarded-For` is ignored entirely, whatever it says;
    * the peer IS a trusted proxy → walk `X-Forwarded-For` from the RIGHT,
      skipping hops that are themselves trusted proxies, and take the first
      one that is not. The rightmost entry is the one our proxy appended; the
      leftmost is whatever the client typed, so "first entry" is exactly
      the wrong end to trust;
    * a trusted peer with no usable header is the peer (a direct call from
      inside the proxy's own network);
    * a malformed hop in the part of the chain we have to read is None —
      "we could not tell" — which a non-empty allowlist refuses.

    `trusted_proxies` defaults to `settings.public_api_trusted_proxies`, which
    is empty unless an operator names the proxy: trusting nobody is the
    fail-closed default.
    """
    if trusted_proxies is None:
        from ..config import settings  # local: keeps import order cheap for tests

        trusted_proxies = tuple(getattr(settings, "public_api_trusted_proxies", ()) or ())
    peer_address = _parse_address(peer) if peer else None
    if peer_address is None:
        return None
    networks = _parse_networks(trusted_proxies)
    if not networks or not _in_any(peer_address, networks):
        return str(peer_address)
    hops = [hop.strip() for hop in str(forwarded_for or "").split(",") if hop.strip()]
    if not hops:
        return str(peer_address)
    for hop in reversed(hops):
        address = _parse_address(hop)
        if address is None:
            return None
        if not _in_any(address, networks):
            return str(address)
    # Every hop is one of our own proxies: the leftmost is the closest thing
    # to a client the chain names, and it is still an address we trust.
    return str(_parse_address(hops[0]))


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def _refuse() -> errors.ApiError:
    """THE one refusal. Built by a factory rather than raised inline so that
    every rung is provably the same object shape — a reviewer can grep for
    `_refuse()` and see there is nothing else."""
    return errors.invalid_api_key()


def _charge_hmac(secret: str) -> None:
    """Pay the HMAC cost on a path that will refuse anyway.

    An unknown `public_id` never reaches `verify_secret`, so without this the
    "no such key" path is measurably cheaper than the "wrong secret" path, and
    a patient attacker can learn which public ids exist by timing alone. This
    does not equalise the database round trip that the known path also pays —
    nothing short of a dummy query would — but it removes the one difference
    this module controls. If the pepper cannot be resolved, that is a server
    fault and it propagates: a 500 is the honest answer when the platform
    cannot verify ANY key, and answering 401 would hide a total outage behind
    a message that blames the caller's credential.
    """
    keys.verify_secret(secret, _DUMMY_DIGEST)


def resolve_api_caller(
    authorization_header: Optional[str] = None,
    *,
    ip: Optional[str] = None,
    authorization: Optional[str] = None,
    peer: Optional[str] = None,
    forwarded_for: Optional[str] = None,
) -> ApiCaller:
    """The single entry point. Returns an `ApiCaller` or raises `ApiError`.

    THE ADDRESS (2026-09-13, wave-2 review). Pass the two raw values —
    `peer=request.client.host` and
    `forwarded_for=request.headers.get("x-forwarded-for")` — and the resolver
    decides the address itself through `client_address()`, which believes a
    forwarded address only when the socket peer is a configured trusted proxy
    (`settings.public_api_trusted_proxies`). Strings, not the request object,
    so this signature still has no way to receive a cookie.

    `ip=` is the legacy form and is taken AS the socket peer: it is honoured
    only when `peer` is not given. A caller that computed it from
    `X-Forwarded-For` has reintroduced the forgery this function exists to
    close, so every `/v1` route must pass `peer=` / `forwarded_for=`. None is
    honest about not knowing and is refused by a non-empty allowlist.

    WHY THERE ARE TWO NAMES FOR ONE HEADER. `app/publicapi/router.py` was
    written against this function before it existed and adapts by INSPECTING
    the signature (`_call_resolver`, 2026-09-13): it looks for a parameter
    literally named `request` or `authorization` and, finding neither, falls
    back to calling `fn(request)` — which would hand this function a FastAPI
    `Request` object where it expects a string, and `/v1` would answer 401 to
    every request in production while every test that calls it directly kept
    passing. `authorization` is therefore accepted as a keyword alias so the
    router's middle branch matches. It is an alias and nothing more: whichever
    of the two arrives is the same header value, and this function still has
    no parameter through which a cookie could reach it.

    Raises only `errors.ApiError`. `keys.PepperUnavailable` is the one
    deliberate exception: it means the platform cannot verify any credential
    at all, which is a 500 (`errors.from_unexpected`), not a 401.
    """
    header = authorization_header if authorization_header is not None else authorization
    if peer is not None:
        ip = client_address(peer, forwarded_for)
    token = bearer_token(header)
    if token is None:
        raise _refuse()

    parsed = keys.split_key(token)
    if parsed is None:
        # Shape or checksum. No database work has happened and none will:
        # this is the filter that makes the thousands of scanner probes any
        # public endpoint receives cost nothing (CONTRACT-3 §5).
        raise _refuse()

    row = db.api_key_by_public_id(parsed.public_id)
    if row is None:
        _charge_hmac(parsed.secret)
        raise _refuse()

    # The environment BEFORE the digest, because the digest is the expensive
    # half and a prefix-swapped token is cheap to spot. Both refuse
    # identically, so the order is a cost decision, not a behavioural one.
    if not keys.environment_matches(parsed.environment, str(row.get("environment") or "")):
        _charge_hmac(parsed.secret)
        raise _refuse()

    if not keys.verify_secret(parsed.secret, str(row.get("key_hash") or "")):
        raise _refuse()

    # --- everything below here is "the secret was right, but" --------------
    #
    # Deliberately after the digest so that revoked, expired, rotated-out,
    # disabled-service-account, disabled-project and disabled-workspace all
    # cost the same as each other AND the same as a wrong secret on a real
    # key. Each one refuses with `_refuse()`; there is no branch that reports
    # which.

    if _status_of(row) != "active":
        raise _refuse()

    if keys.is_expired(_as_datetime(row.get("expires_at"))):
        raise _refuse()

    # A key that was rotated out with a grace window stops working when the
    # window shuts. `rotation_expires_at` is written on the OLD key by
    # `projects.rotate_key`; `keys.overlap_active` reads a missing value as
    # "no window was recorded", which is NOT "forever" — `status` and
    # `expires_at` govern that case.
    rotation_expires_at = _as_datetime(row.get("rotation_expires_at"))
    if rotation_expires_at is not None and not keys.overlap_active(rotation_expires_at):
        raise _refuse()

    service_account = row.get("service_account") or None
    if service_account is not None and _status_of(service_account) != "active":
        raise _refuse()

    project = row.get("project") or None
    if project is None or _status_of(project) != "active":
        # A key whose project row is missing is a key whose FK was violated;
        # it authenticates to nothing and is refused like any other.
        raise _refuse()

    workspace_id = str(project.get("workspace_id") or row.get("workspace_id") or "")
    if not workspace_id or workspace_status(workspace_id) != "active":
        raise _refuse()

    # Tenancy, asserted rather than assumed. Every V34 table stores
    # `workspace_id` explicitly (SCHEMA-V34) precisely so a quota query never
    # has to guess — which means the two copies can, in principle, disagree,
    # and a key whose own row names a different workspace from its project's
    # is a corrupted tenant boundary, not a request to serve.
    if str(row.get("workspace_id") or "") != workspace_id:
        log.error(
            "api key %s: workspace on the key row and on its project disagree",
            parsed.public_id,
        )
        raise _refuse()
    if str(row.get("project_id") or "") != str(project.get("id") or ""):
        raise _refuse()
    if service_account is not None and str(
        service_account.get("project_id") or ""
    ) != str(project.get("id") or ""):
        raise _refuse()

    if not ip_allowed(ip, project.get("ip_allowlist")):
        # STANDARDS.md: a request from outside the policy is a high-fidelity
        # compromise signal, so it is logged by its PUBLIC id — never the
        # token, never the digest — and refused with the same 401 as anything
        # else, so the address doing the probing learns nothing.
        log.warning(
            "api key %s refused: %s is outside the project ip_allowlist",
            parsed.public_id,
            ip or "an unknown address",
        )
        raise _refuse()

    caller = ApiCaller(
        workspace_id=workspace_id,
        project_id=str(project["id"]),
        service_account_id=(
            str(service_account["id"]) if service_account is not None else None
        ),
        key_id=str(row["id"]),
        scopes=_effective_scopes(row, service_account),
        models=_effective_models(row, service_account, project),
        limits=_effective_limits(row, project),
        environment=str(row.get("environment") or project.get("environment") or "live"),
        public_id=parsed.public_id,
        allowed_origins=_string_list(project.get("allowed_origins")),
        ip=ip or None,
    )

    # ONCE per request, and only after the request was accepted. Not per
    # token, not per stream event: `last_used_at` answers "is this integration
    # still alive" and `last_used_ip` answers "where is this leaked key being
    # used from", and neither needs more resolution than one row per request
    # (SCHEMA-V34, `touch_api_key`). It is deliberately NOT wrapped in a
    # try/except: if this write fails the database is unavailable and the
    # request was going to fail anyway, and swallowing the error would make
    # the only forensic trail the platform has quietly unreliable.
    db.touch_api_key(caller.key_id, ip or None)
    return caller


# ---------------------------------------------------------------------------
# Narrowing: a key may restrict what its project allows, never widen it
# ---------------------------------------------------------------------------


def _effective_scopes(
    key_row: Mapping[str, Any], service_account: Optional[Mapping[str, Any]]
) -> FrozenSet[Scope]:
    """What this credential may call.

    THE TWO EMPTIES MEAN DIFFERENT THINGS, and the asymmetry is on purpose:

    * an empty scope list on the KEY grants nothing. A key is a credential,
      and `scopes.py` is explicit that "no scopes" is a real state that
      satisfies every `requires()` check with a refusal. Deny by default.
    * an empty scope list on the SERVICE ACCOUNT narrows nothing. A service
      account is an organisational grouping, not a credential — you cannot
      authenticate as one — and `api_service_accounts.scopes` defaults to
      `'[]'` in the DDL, so treating that default as "permits nothing" would
      make every key created under a service account useless the moment it
      was attached to one.

    A non-empty service-account list DOES narrow, by intersection, so an
    administrator can cap a whole integration in one place.

    An unparseable stored scope — a value someone wrote into the jsonb column
    by hand, or a scope this release removed — refuses the whole credential
    rather than being dropped. `scopes.parse_scopes` raises `UnknownScopeError`
    for exactly that reason, and silently ignoring it would mean a key that
    looks configured and grants less than its row says.
    """
    try:
        granted = parse_scopes(key_row.get("scopes"))
    except UnknownScopeError:
        log.error(
            "api key %s carries a scope this release does not know; refusing it",
            key_row.get("public_id"),
        )
        raise _refuse() from None
    if service_account is None:
        return granted
    try:
        ceiling = parse_scopes(service_account.get("scopes"))
    except UnknownScopeError:
        log.error(
            "service account %s carries an unknown scope; refusing its keys",
            service_account.get("id"),
        )
        raise _refuse() from None
    return granted & ceiling if ceiling else granted


def _effective_models(
    key_row: Mapping[str, Any],
    service_account: Optional[Mapping[str, Any]],
    project: Mapping[str, Any],
) -> Tuple[str, ...]:
    """The model ids this key may address, narrowed project → account → key.

    Returns `()` when no level narrowed, which
    `registry.resolve_public_model(allowed=…)` reads as "the whole public
    catalogue". A level that narrows to the empty intersection returns `()`
    too — and that is the one genuinely ambiguous case, so it is resolved
    explicitly below rather than left to the reader.
    """
    allowed = _narrow(None, project.get("allowed_models") or ())
    if service_account is not None:
        allowed = _narrow(allowed, service_account.get("allowed_models") or ())
    allowed = _narrow(allowed, key_row.get("allowed_models") or ())
    if allowed is None:
        return ()
    if not allowed:
        # Two non-empty allowlists that share nothing. The key may address no
        # model at all, and it must NOT fall through to "no narrowing". A
        # sentinel that cannot be a model id says so without a second return
        # type; `resolve_public_model` will answer None for it, which the
        # router turns into the 404 CONTRACT-3 §4 requires (never a 403: no
        # existence disclosure).
        return (_NO_MODEL,)
    return allowed


#: The "narrowed to nothing" sentinel. Not a valid public model id — the
#: registry's ids are plain lowercase names — so it can never accidentally
#: match one.
_NO_MODEL = "\x00none"


def _effective_limits(
    key_row: Mapping[str, Any], project: Mapping[str, Any]
) -> CallerLimits:
    """The project's ceilings, narrowed by the key's own.

    `api_keys.rpm` and `api_keys.max_concurrency` are nullable and NULL means
    inherit. A value ABOVE the project's is clamped down rather than honoured:
    the console can edit a key row, and a field on an editable row that raises
    a limit is a privilege-escalation field. Clamped, not refused, because the
    number is not a request from the caller — it is configuration, and
    refusing every request of a mis-configured key would be a self-inflicted
    outage.

    ONLY NULL INHERITS; ZERO IS ZERO (2026-09-13, wave-2 review). The first
    cut replaced any value <= 0 with the platform default, so a project an
    operator froze at rpm=0 / daily_token_quota=0 / max_concurrency=0 — which
    the schema permits and the console writes — kept running at 60 rpm, two
    million tokens a day and four in flight while the console showed 0.
    CONTRACT-3 §8: a value the platform cannot honour is never silently
    ignored. So a NULL or unparseable column falls back to `settings`
    (CONTRACT-3 §12's defaults, in one runtime-readable place); 0 is enforced
    as 0 and refuses every request; a negative number (the CHECKs forbid it,
    so only a hand-edited row) is read as 0, the fail-closed direction.

    The same rule on the key: NULL inherits the project, 0 parks the key
    (the DDL's own comment says so), and a number above the project's is
    clamped to it.
    """
    from ..config import settings  # local: keeps import order cheap for tests

    def _project_int(name: str, fallback: int) -> int:
        value = project.get(name)
        if value is None:
            return int(fallback)
        try:
            number = int(value)
        except (TypeError, ValueError):
            return int(fallback)
        return max(0, number)

    def _key_int(name: str) -> Optional[int]:
        value = key_row.get(name)
        if value is None:
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return max(0, number)

    def _ceiling(source: Mapping[str, Any], name: str) -> Optional[int]:
        # `max_input_tokens` / `max_output_tokens`: the CHECK is `> 0`, and
        # NULL means "the registry's number stands".
        value = source.get(name)
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    rpm = _project_int("rpm", settings.public_api_default_rpm)
    concurrency = _project_int(
        "max_concurrency", settings.public_api_default_max_concurrency
    )
    key_rpm = _key_int("rpm")
    key_concurrency = _key_int("max_concurrency")
    return CallerLimits(
        rpm=min(rpm, key_rpm) if key_rpm is not None else rpm,
        input_tpm=_project_int("input_tpm", settings.public_api_default_input_tpm),
        output_tpm=_project_int("output_tpm", settings.public_api_default_output_tpm),
        max_concurrency=(
            min(concurrency, key_concurrency)
            if key_concurrency is not None
            else concurrency
        ),
        daily_token_quota=_project_int(
            "daily_token_quota", settings.public_api_default_daily_token_quota
        ),
        max_input_tokens=_ceiling(project, "max_input_tokens"),
        max_output_tokens=_ceiling(project, "max_output_tokens"),
        project_rpm=rpm,
        project_max_concurrency=concurrency,
        key_rpm=min(rpm, key_rpm) if key_rpm is not None else None,
        key_max_concurrency=(
            min(concurrency, key_concurrency) if key_concurrency is not None else None
        ),
    )


__all__ = [
    "ApiCaller",
    "CallerLimits",
    "WorkspaceStatusReader",
    "bearer_token",
    "client_address",
    "configure_workspace_status",
    "ip_allowed",
    "resolve_api_caller",
    "workspace_status",
]
