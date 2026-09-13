"""API keys: the format, the offline checksum, the digest, and the pepper.

CONTRACT-3 §5. One line, four parts:

    tsk_live_<public_id>_<secret><checksum>
    tsk_test_<public_id>_<secret><checksum>

* `public_id` — 16 hex characters (`secrets.token_hex(8)`). The LOOKUP half.
  Stored in clear, safe to log, safe to show in the console. Nothing about it
  is secret, which is exactly why the resolver may index on it.
* `secret` — `secrets.token_urlsafe(32)`: 256 bits of CSPRNG entropy, 43
  characters. The BEARER half. Shown once, at creation, and never again — not
  in a log line, not in a response, not in the repr of anything defined here.
* `checksum` — 6 Base62 characters of CRC32 over `public_id + secret`,
  validated with NO database access at all.

WHY THE CHECKSUM IS NOT A SECURITY CONTROL. An attacker can compute it as
easily as we can; it exists so that a typo, a truncated copy-paste, and the
thousands of scanner probes that hit any public API are all discarded before
a connection is taken from the pool. That is why `split_key` returns None for
a bad checksum and the caller answers with the SAME generic 401 as for an
unknown key (CONTRACT-3 §9): a distinguishable "bad checksum" reply would
turn this cheap filter into an oracle.

STORAGE. `key_hash = HMAC-SHA256(pepper, secret)`, hex, compared with
`hmac.compare_digest`. A slow password hash (Argon2, PBKDF2) is the wrong
tool here and STANDARDS.md says why: NIST scopes it to secrets below 112 bits
of entropy, and this secret carries 256. The keyed digest is what makes the
column both indexable and useless to someone who stole only the table.

THE PEPPER, AND ITS HONEST WEAKNESS. `API_KEY_PEPPER` is the intended source
and the one that satisfies NIST's "keep the key somewhere the database role
cannot read". When it is unset, the platform generates one and persists it
through the injected saver — `platform_secrets` (V34), the SAME database that
holds the digests. That keeps a fresh install working without a deploy-time
secret and is documented, here and in CONTRACT-3 §5, as the weaker of the
two: an attacker with the whole database has the pepper too.

WHY THIS MODULE NEVER IMPORTS `db`. Key format is a pure function and the
tests for it must run with no PostgreSQL in the room; the pepper is the one
thing that needs storage, so storage arrives as an injected loader/saver pair
(`configure_pepper_store`) that the wiring wave fills in with
`db.get_platform_secret` / `db.set_platform_secret`. The shape follows
`app/sharing.py:45-98` (`mint_token` / `split_token` / `secret_matches` /
`redact`), which already does mint-and-split for share links; read that file
first if this one looks unfamiliar.
"""
from __future__ import annotations

import binascii
import hmac
import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, FrozenSet, Optional

from ..config import settings

# ---------------------------------------------------------------------------
# The format
# ---------------------------------------------------------------------------

#: The greppable product prefix. STANDARDS.md (GitHub's secret-scanning
#: partner programme): a unique, fixed prefix is what makes a leaked key
#: detectable by a scanner at all, and it dropped their false positives to
#: ~0.5% on its own. Changing it retires every key in existence.
KEY_PREFIX = "tsk"

#: Live and test keys differ in the PREFIX, never only in their random part,
#: so a human reading a config file and a scanner reading a repository can
#: both tell at a glance which one leaked.
#:
#: THE PREFIX IS A LABEL, NOT AN ASSERTION. CONTRACT-3 §5 puts the checksum
#: over `public_id + secret`, which leaves these four characters outside the
#: integrity check — a presented token can therefore claim an environment its
#: key does not have. `environment_matches()` below is the reconciliation the
#: resolver is required to perform against `api_keys.environment`; read its
#: docstring before trusting `ParsedKey.environment` for anything.
ENVIRONMENTS = ("live", "test")

_PUBLIC_ID_BYTES = 8  # -> 16 hex characters
_SECRET_BYTES = 32  # -> 256 bits, 43 urlsafe-base64 characters

PUBLIC_ID_CHARS = _PUBLIC_ID_BYTES * 2
CHECKSUM_CHARS = 6
LAST_FOUR_CHARS = 4

#: Base62, digits-uppercase-lowercase. CRC32 tops out at 2**32-1, and
#: 62**6 (5.68e10) comfortably covers it, so six characters always suffice
#: and the value is left-padded to exactly six.
_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

#: Secret lengths this platform accepts, in characters.
#:
#: ADD to this set, never replace it. `token_urlsafe(32)` is 43 characters
#: today; if a future release mints longer secrets, every key already in the
#: field still has to pass the offline shape check, and dropping 43 from here
#: would 401 the entire installed base before a single database row was read.
_ACCEPTED_SECRET_LENGTHS: FrozenSet[int] = frozenset({43})

_PUBLIC_ID_RE = re.compile(r"\A[0-9a-f]+\Z")
#: The urlsafe-base64 alphabet `token_urlsafe` emits, no padding.
_SECRET_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")
_CHECKSUM_RE = re.compile(r"\A[0-9A-Za-z]+\Z")


def _base62(value: int, width: int) -> str:
    out = []
    while value:
        value, rest = divmod(value, 62)
        out.append(_BASE62[rest])
    return "".join(reversed(out)).rjust(width, _BASE62[0])


def checksum_for(public_id: str, secret: str) -> str:
    """The six trailing characters: Base62 of CRC32 over `public_id + secret`.

    CRC32 and not a hash on purpose — this is an integrity check against
    fumbled copy-paste, not a MAC. `binascii.crc32` is used rather than
    `zlib.crc32` only because it needs no decompression machinery; the value
    is identical.
    """
    raw = (public_id + secret).encode("utf-8")
    return _base62(binascii.crc32(raw) & 0xFFFFFFFF, CHECKSUM_CHARS)


def _normalise_environment(environment: str) -> str:
    """`"  LIVE "` -> `"live"`; anything else is a `ValueError`.

    THE ISINSTANCE CHECK IS NOT DEFENSIVE NOISE (2026-09-13, wave-1 review).
    The previous version read `(environment or "").strip()`, which passes a
    truthy non-string straight through to `.strip()` — so `mint_key(5)` raised
    `AttributeError: 'int' object has no attribute 'strip'` while this
    function, `mint_key`'s docstring and the tests all promised `ValueError`.
    A management-surface handler that catches `ValueError` to turn a bad
    environment into a 400 would instead have let an `AttributeError` through
    as a 500. The parametrised test never caught it because every case it
    tried was a string or `None`.
    """
    if environment is not None and not isinstance(environment, str):
        raise ValueError(
            f"environment must be one of {ENVIRONMENTS!r}, not {environment!r}"
        )
    env = (environment or "").strip().lower()
    if env not in ENVIRONMENTS:
        raise ValueError(
            f"environment must be one of {ENVIRONMENTS!r}, not {environment!r}"
        )
    return env


@dataclass(frozen=True, eq=False)
class MintedKey:
    """The one and only moment the plaintext exists.

    The caller writes `public_id`, `key_digest(secret)` and `last_four` to
    `api_keys`, hands `token` to the person who asked for the key, and drops
    this object. Nothing here is ever stored whole.

    `eq=False` is deliberate: `==` on a dataclass compares field by field with
    Python's own short-circuiting `==`, which is not constant time. Comparing
    a credential is what `verify_secret` is for.
    """

    token: str
    public_id: str
    secret: str
    last_four: str
    checksum: str
    environment: str

    def __repr__(self) -> str:
        # A repr lands in a traceback, a debugger, an f-string someone added
        # to a log line at 2am, and in pytest's own failure output. Neither
        # the token nor the secret may appear in any of them.
        return (
            f"MintedKey(environment={self.environment!r}, "
            f"public_id={self.public_id!r}, last_four={self.last_four!r}, "
            "secret='<redacted>', token='<redacted>')"
        )

    __str__ = __repr__


@dataclass(frozen=True, eq=False)
class ParsedKey:
    """What `split_key` recovered from a presented token. Offline-validated
    shape only: nothing here says the key EXISTS, is active, or is this
    project's. `eq=False` for the same reason as `MintedKey`."""

    environment: str
    public_id: str
    secret: str
    checksum: str

    @property
    def last_four(self) -> str:
        return self.checksum[-LAST_FOUR_CHARS:]

    def __repr__(self) -> str:
        return (
            f"ParsedKey(environment={self.environment!r}, "
            f"public_id={self.public_id!r}, last_four={self.last_four!r}, "
            "secret='<redacted>')"
        )

    __str__ = __repr__


def mint_key(environment: str = "live") -> MintedKey:
    """A fresh key. CSPRNG only — `secrets`, never `random`, never a UUID.

    `last_four` is taken from the END OF THE TOKEN, which is the checksum, and
    NOT from the secret. The console shows "…" plus four characters so a
    person can recognise which key a service is using (STANDARDS.md, Stripe's
    prefix+last-4 rule); taking those four from the secret would put four
    characters of a credential in a database column that is read by the
    console, the audit log and every support query, for no gain — the checksum
    is a deterministic function of the key and identifies it just as well.
    """
    env = _normalise_environment(environment)
    public_id = secrets.token_hex(_PUBLIC_ID_BYTES)
    secret = secrets.token_urlsafe(_SECRET_BYTES)
    checksum = checksum_for(public_id, secret)
    token = f"{KEY_PREFIX}_{env}_{public_id}_{secret}{checksum}"
    return MintedKey(
        token=token,
        public_id=public_id,
        secret=secret,
        last_four=token[-LAST_FOUR_CHARS:],
        checksum=checksum,
        environment=env,
    )


def split_key(token: str) -> Optional[ParsedKey]:
    """Shape and checksum, with NO database access. None for anything wrong.

    None — never an exception, never a reason — because malformed, truncated,
    mistyped and wholly invented must be indistinguishable to the caller;
    `sharing.split_token` returns None for the same reason. The handler above
    turns every one of them into the same 401 `invalid_api_key`.

    Parsing runs from the END, not by splitting on `_`: `token_urlsafe`
    emits `_` inside the secret, so `token.split("_")` would tear a perfectly
    good key apart roughly two times in three.
    """
    if not token or not isinstance(token, str):
        return None
    for env in ENVIRONMENTS:
        head = f"{KEY_PREFIX}_{env}_"
        if token.startswith(head):
            break
    else:
        return None

    rest = token[len(head) :]
    public_id, sep, tail = rest.partition("_")
    if not sep:
        return None
    if len(public_id) != PUBLIC_ID_CHARS or not _PUBLIC_ID_RE.match(public_id):
        return None
    if len(tail) <= CHECKSUM_CHARS:
        return None
    secret, checksum = tail[:-CHECKSUM_CHARS], tail[-CHECKSUM_CHARS:]
    if len(secret) not in _ACCEPTED_SECRET_LENGTHS:
        return None
    if not _SECRET_RE.match(secret) or not _CHECKSUM_RE.match(checksum):
        return None
    if not hmac.compare_digest(checksum_for(public_id, secret), checksum):
        return None
    return ParsedKey(
        environment=env, public_id=public_id, secret=secret, checksum=checksum
    )


def environment_matches(presented: str, stored: str) -> bool:
    """Does the environment the BEARER claimed match the one we recorded?

    THE GAP THIS CLOSES, and why it lives here rather than in the resolver
    (2026-09-13, wave-1 review). CONTRACT-3 §5 puts the environment in the
    PREFIX and the checksum over `public_id + secret` only — so the four
    characters that say `live` or `test` are outside the integrity check.
    Swapping them produces a token `split_key` still accepts and for which it
    reports the attacker's chosen environment:

        tsk_live_<id>_<secret><crc>  ->  tsk_test_<id>_<secret><crc>

    Both parse. Both carry the same real credential. Only the label moved.
    Nothing is stolen by that on its own — the secret still has to verify —
    but `redact()` then writes the attacker-chosen label into whatever log
    line or audit row consumes it, and an operator reading "a TEST key was
    used" about a LIVE key is an operator who investigates the wrong thing.
    The module's own header claims the opposite property at lines 71-73.

    THE FIX IS RECONCILIATION, NOT A NEW CHECKSUM. Binding the environment
    into `checksum_for` would change CONTRACT-3 §5, which says the contract
    must move first, and would retire every key already minted under it. So
    the token stays exactly as the contract specifies and the RESOLVER is
    required to reconcile what was presented against `api_keys.environment`,
    answering the same generic 401 on a mismatch — see
    `apiplatform/resolver.py`, which calls this, and the test named
    `test_a_prefix_swapped_token_is_refused_by_the_resolver_not_by_the_parser`.

    `compare_digest` rather than `==` is habit, not necessity: neither value is
    secret. It costs nothing and it keeps every credential-adjacent comparison
    in this module reading the same way.
    """
    if not isinstance(presented, str) or not isinstance(stored, str):
        return False
    return hmac.compare_digest(presented, stored)


def redact(token: str, *, environment: Optional[str] = None) -> str:
    """What a log line, an audit row or an error report may contain.

    The public half identifies the key well enough to investigate an incident;
    the rest is the credential. A full token in a log is a working key in a
    log, and logs travel further than databases do.

    `environment`, when given, OVERRIDES the one read off the token. Pass the
    value from `api_keys.environment` wherever you have it: the token's own
    prefix is attacker-controlled (see `environment_matches`), so a log line
    built from it alone can be made to say `test` about a live key. The
    argument is keyword-only and optional so every existing call site keeps
    working unchanged.
    """
    parsed = split_key(token)
    if parsed is None:
        return "<malformed>"
    env = environment if isinstance(environment, str) and environment else parsed.environment
    return f"{KEY_PREFIX}_{env}_{parsed.public_id}_<redacted>"


def last_four(token: str) -> str:
    """The four recognition characters for a presented token, or "" if the
    token is not one of ours."""
    return token[-LAST_FOUR_CHARS:] if split_key(token) else ""


# ---------------------------------------------------------------------------
# The pepper
# ---------------------------------------------------------------------------

#: The row name in `platform_secrets` (V34) for the generated pepper.
PEPPER_SECRET_NAME = "api_key_pepper"

#: The environment variable the wiring wave must expose as
#: `settings.api_key_pepper`. Named here so the two waves cannot disagree.
PEPPER_ENV_VAR = "API_KEY_PEPPER"

#: A configured pepper shorter than this is refused outright. NIST 800-131A
#: disallows an HMAC key below 112 bits; 32 characters is a comfortable margin
#: over that for any encoding an operator is likely to paste. Refusing is the
#: right failure: a short pepper that "works" is a security property silently
#: downgraded, and this check fires on the first mint rather than never.
MIN_PEPPER_CHARS = 32

#: Bytes of the generated fallback pepper — 256 bits, the same strength as the
#: secrets it protects, and well under SHA-256's 64-byte block size past which
#: NIST 800-107 says a longer key buys nothing.
_PEPPER_BYTES = 32

PepperLoader = Callable[[], Optional[str]]
PepperSaver = Callable[[str], Any]

_pepper_lock = threading.Lock()
_pepper_cache: Optional[str] = None
_pepper_loader: Optional[PepperLoader] = None
_pepper_saver: Optional[PepperSaver] = None


class PepperUnavailable(RuntimeError):
    """No usable pepper, so no digest can be computed or verified.

    Raised, never papered over with a default: a fallback pepper would mean
    every installation that hit this path shared one, and a digest computed
    under the wrong pepper fails verification anyway — loudly at mint time is
    far cheaper to diagnose than silently at authentication time.
    """


def configure_pepper_store(loader: PepperLoader, saver: PepperSaver) -> None:
    """Inject persistence for the generated fallback pepper.

    The wiring wave calls this once at start-up with `db.get_platform_secret`
    and `db.set_platform_secret` bound to `PEPPER_SECRET_NAME`. Keeping it an
    injection is what lets this module — and its tests — run with no database.

    THE SAVER MUST BE INSERT-IF-ABSENT, NEVER AN OVERWRITE. Replacing a stored
    pepper invalidates every `key_hash` in `api_keys` at once, which is a
    total, silent authentication outage for every developer key in the
    workspace. `INSERT … ON CONFLICT DO NOTHING` is the right statement; this
    function re-reads through the loader afterwards precisely so that a
    concurrent writer's value wins over the one generated here.
    """
    global _pepper_loader, _pepper_saver, _pepper_cache
    with _pepper_lock:
        _pepper_loader = loader
        _pepper_saver = saver
        _pepper_cache = None


def reset_pepper_cache() -> None:
    """Forget the generated pepper held in memory.

    For tests, and for a configuration reload. It does not delete anything
    stored: the next call reads the same row back.
    """
    global _pepper_cache
    with _pepper_lock:
        _pepper_cache = None


def _configured_pepper() -> str:
    """`API_KEY_PEPPER` as the wiring wave exposes it, or "".

    Read on EVERY call rather than cached: an operator who sets the variable
    and restarts expects it to take effect, and a cached empty string from
    before the first successful read would outlive the change.
    """
    value = (getattr(settings, "api_key_pepper", "") or "").strip()
    if value and len(value) < MIN_PEPPER_CHARS:
        raise PepperUnavailable(
            f"{PEPPER_ENV_VAR} is set but shorter than {MIN_PEPPER_CHARS} "
            "characters; refusing to key HMAC-SHA256 with it"
        )
    return value


def _checked_stored_pepper(stored: str) -> str:
    """The entropy floor, applied to the STORE branch too (2026-09-13).

    `_configured_pepper()` has refused a short `API_KEY_PEPPER` since wave 1
    with the right reasoning — "a short pepper that 'works' is a security
    property silently downgraded" — but the stored branch accepted whatever
    the injected loader returned after nothing more than a `.strip()`. Because
    `settings.api_key_pepper` did not exist at all until today, the unchecked
    branch was the ONLY branch that ever ran: a `platform_secrets` row holding
    one character would have keyed HMAC-SHA256 for every `api_keys.key_hash`
    on the installation, and nothing would have said so.

    REFUSING IS THE ONLY SAFE ANSWER. Regenerating over the short value is
    not: overwriting a stored pepper invalidates every `key_hash` in
    `api_keys` at once, which `configure_pepper_store` correctly calls a
    total, silent authentication outage. An operator must decide whether to
    replace the row (and retire every key) or restore the right value.
    """
    if stored and len(stored) < MIN_PEPPER_CHARS:
        raise PepperUnavailable(
            f"the stored {PEPPER_SECRET_NAME!r} secret is shorter than "
            f"{MIN_PEPPER_CHARS} characters; refusing to key HMAC-SHA256 with "
            "it. Replacing it would invalidate every existing key_hash, so "
            "this is an operator decision, not something to fix automatically"
        )
    return stored


def current_pepper() -> str:
    """The HMAC key, resolved in the order CONTRACT-3 §5 gives.

    1. `API_KEY_PEPPER` — the intended source, outside the database.
    2. The value already generated and persisted by this installation.
    3. A fresh 256-bit value, written through the injected saver.

    With no configured pepper and no store, this raises: minting a key whose
    digest cannot be reproduced after a restart is worse than refusing to
    mint one.
    """
    configured = _configured_pepper()
    if configured:
        return configured

    global _pepper_cache
    with _pepper_lock:
        if _pepper_cache:
            return _pepper_cache
        if _pepper_loader is None or _pepper_saver is None:
            raise PepperUnavailable(
                f"no {PEPPER_ENV_VAR} is configured and no platform secret store "
                "has been injected; call configure_pepper_store() at start-up"
            )
        stored = _checked_stored_pepper((_pepper_loader() or "").strip())
        if not stored:
            generated = secrets.token_urlsafe(_PEPPER_BYTES)
            _pepper_saver(generated)
            # Re-read rather than trust what we just wrote: if another process
            # inserted first and the saver is the required INSERT … ON CONFLICT
            # DO NOTHING, the stored value is THEIRS, and every digest in the
            # table is keyed to it.
            stored = _checked_stored_pepper(
                (_pepper_loader() or "").strip()
            ) or generated
        _pepper_cache = stored
        return stored


# ---------------------------------------------------------------------------
# The digest
# ---------------------------------------------------------------------------


def key_digest(secret: str) -> str:
    """`HMAC-SHA256(pepper, secret)` as lowercase hex — the `key_hash` column.

    Takes the SECRET, not the token: the public id and the checksum are not
    secret and mixing them in would only make the digest harder to reason
    about.
    """
    if not secret:
        raise ValueError("refusing to digest an empty secret")
    pepper = current_pepper().encode("utf-8")
    return hmac.new(pepper, secret.encode("utf-8"), "sha256").hexdigest()


def verify_secret(secret: str, stored_digest: str) -> bool:
    """Constant-time comparison of a presented secret against `key_hash`.

    `hmac.compare_digest` and never `==`: a byte-by-byte comparison that
    returns early leaks the length of the matching prefix through timing,
    which is how a bearer credential gets guessed one character at a time.

    A missing or malformed stored digest is False, not an exception. A row
    whose hash somehow ended up empty must fail closed and look exactly like
    a wrong key from outside.
    """
    if not secret or not stored_digest:
        return False
    try:
        return hmac.compare_digest(key_digest(secret), stored_digest)
    except (TypeError, ValueError):
        # compare_digest refuses non-ASCII str operands; a digest column
        # holding something that is not hex is corrupt, not authenticating.
        return False


# ---------------------------------------------------------------------------
# Lifecycle: create -> rotate (with overlap) -> revoke
# ---------------------------------------------------------------------------

#: The grace period during which the OLD key still works after a rotation.
#: Seven days follows STANDARDS.md (Stripe): long enough to redeploy every
#: consumer of a server-to-server credential and to WATCH the old key's
#: `last_used_at` stop moving before revoking it, which is the step that makes
#: the revocation safe.
DEFAULT_ROTATION_OVERLAP = timedelta(days=7)

#: No overlap may be longer than this. An unbounded grace window is not a
#: rotation, it is two live credentials.
MAX_ROTATION_OVERLAP = timedelta(days=30)

#: New keys expire unless the creator says otherwise (STANDARDS.md, OWASP
#: Secrets Management): rotation bounds the useful life of a stolen key only
#: if the key has a life to bound.
DEFAULT_KEY_LIFETIME = timedelta(days=90)


def _now(now: Optional[datetime] = None) -> datetime:
    """Always timezone-aware UTC. Every V34 timestamp column is timestamptz,
    and a naive datetime compared against one is a bug that only shows up
    when the server is not on UTC."""
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("a naive datetime cannot be compared with a timestamptz")
    return now.astimezone(timezone.utc)


@dataclass(frozen=True, eq=False)
class KeyRotation:
    """The result of planning a rotation: the new key, and when the old one
    stops working. Pure data — the caller performs the two UPDATEs.

    `rotated_from` and `previous_expires_at` map to `api_keys.rotated_from`
    and `api_keys.rotation_expires_at` (V34). `eq=False` because `minted`
    carries a plaintext secret.
    """

    minted: MintedKey
    rotated_from: str
    previous_expires_at: datetime

    def __repr__(self) -> str:
        return (
            f"KeyRotation(rotated_from={self.rotated_from!r}, "
            f"previous_expires_at={self.previous_expires_at.isoformat()!r}, "
            f"minted={self.minted!r})"
        )

    __str__ = __repr__


def plan_rotation(
    previous_public_id: str,
    environment: str = "live",
    overlap: timedelta = DEFAULT_ROTATION_OVERLAP,
    now: Optional[datetime] = None,
) -> KeyRotation:
    """Mint the replacement and say when the old key dies.

    The PLANNED rotation, with a grace window. It is a different operation
    from `plan_revocation` on purpose (STANDARDS.md): a single "rotate" verb
    with a built-in grace period cannot serve a compromise, where the whole
    point is that the old credential stops working now.

    A negative or over-long overlap is refused rather than clamped —
    CONTRACT-3 §8's rule that a parameter this platform cannot honour is
    rejected, never silently ignored, applies to the management surface too.
    """
    if overlap < timedelta(0):
        raise ValueError("a rotation overlap cannot be negative")
    if overlap > MAX_ROTATION_OVERLAP:
        raise ValueError(
            f"a rotation overlap may not exceed {MAX_ROTATION_OVERLAP}; "
            "use two keys if an integration genuinely needs longer"
        )
    if not isinstance(previous_public_id, str) or not previous_public_id.strip():
        # The same lesson as `_normalise_environment` (2026-09-13): `if not x`
        # accepts any truthy object, so a caller that passed the whole key ROW
        # instead of its public id got a rotation naming a dict, and the two
        # UPDATEs the caller then performs would write that dict's repr into
        # `api_keys.rotated_from`. A type is cheaper to refuse than to chase.
        raise ValueError("a rotation must name the key it replaces, by public id")
    moment = _now(now)
    return KeyRotation(
        minted=mint_key(environment),
        rotated_from=previous_public_id,
        previous_expires_at=moment + overlap,
    )


def plan_revocation(
    previous_public_id: str,
    environment: str = "live",
    now: Optional[datetime] = None,
) -> KeyRotation:
    """Rotate with ZERO overlap — the compromise and the departing-colleague
    case. The old key is dead the instant the caller writes the row."""
    return plan_rotation(
        previous_public_id, environment, overlap=timedelta(0), now=now
    )


def overlap_active(
    previous_expires_at: Optional[datetime], now: Optional[datetime] = None
) -> bool:
    """Is the rotated-out key still inside its grace window?

    `None` means no rotation window was ever set, which is NOT "forever": a
    key with no recorded expiry is governed by `status` and `expires_at`, not
    by this. The boundary is exclusive, so a zero-overlap revocation is
    already inactive at the moment it is written.
    """
    if previous_expires_at is None:
        return False
    return _now(now) < _now(previous_expires_at)


def default_expires_at(
    now: Optional[datetime] = None, lifetime: timedelta = DEFAULT_KEY_LIFETIME
) -> datetime:
    """When a key created right now should expire if nobody chooses."""
    if lifetime <= timedelta(0):
        raise ValueError("a key lifetime must be positive")
    return _now(now) + lifetime


def is_expired(
    expires_at: Optional[datetime], now: Optional[datetime] = None
) -> bool:
    """`expires_at` is nullable in V34 and NULL means "no expiry", which is a
    deliberate operator choice rather than an accident — so it is False here
    and the console is where the warning belongs."""
    if expires_at is None:
        return False
    return _now(now) >= _now(expires_at)
