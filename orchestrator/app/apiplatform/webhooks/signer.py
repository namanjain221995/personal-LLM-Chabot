"""The webhook signature — what proves a delivery came from us (CONTRACT-3 §14).

    TechSara-Signature: t=1789200000,v1=9f86d081884c7d65…

`t` is the unix second the payload was signed. `v1` is
`HMAC-SHA256(secret, "<t>.<raw body>")` in lower-case hex. The timestamp is
inside the signed string, not merely beside it, which is the whole point: a
signature that covered only the body could be replayed forever by anyone who
ever saw one delivery, and the consumer would have no way to tell the copy
from the original.

THREE PROPERTIES THIS MODULE EXISTS TO GUARANTEE

1. THE CONSUMER CAN BE STRICT WITHOUT BEING BRITTLE. `verify()` is the helper
   we publish verbatim in `/docs`, so the code a customer pastes into their
   server is the code this repository tests. It takes the raw request body —
   `bytes`, before any JSON parse — because `json.loads` followed by
   `json.dumps` does not round-trip byte for byte (key order, separators,
   unicode escapes), and a consumer who signs the re-serialised form gets a
   mismatch they cannot debug.

2. ROTATION DOES NOT DROP A DELIVERY. `api_webhook_endpoints` carries
   `previous_secret` and `previous_secret_expires_at` precisely so an
   endpoint can be moved to a new secret without a flag day. During the
   overlap we send BOTH digests in one header (`v1=…,v1=…`), so a consumer
   verifies whether they have already switched or not. Sending two is safe:
   a digest reveals nothing about the key that made it, and the consumer's
   rule is "at least one v1 matches", never "the first one".

3. THE COMPARISON IS CONSTANT TIME, AND STAYS CONSTANT TIME EVEN WITH TWO
   CANDIDATES. The loop below accumulates with `|=` rather than returning on
   the first match, because `any(...)` short-circuits and would leak, through
   timing, which of the two secrets the consumer holds. That is a small leak,
   but it is free to close and impossible to close later once somebody has
   copied the obvious version.

Nothing here reads the database, the clock beyond `time.time()`, or the
network, so every property above is pinned by an offline test.
"""
from __future__ import annotations

import hmac
import logging
import secrets as _secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping, Optional, Sequence, Tuple, Union

log = logging.getLogger(__name__)

#: The header name, exactly as it goes on the wire. HTTP header names are
#: case-insensitive, so a consumer may read it in any case; we send this one.
SIGNATURE_HEADER = "TechSara-Signature"

#: The scheme label. Versioned from the first delivery so that the day the
#: digest changes (v2), a consumer can accept both during their own migration
#: instead of having to cut over in the same minute we do.
SCHEME = "v1"

#: ±5 minutes, CONTRACT-3 §14. Wide enough for ordinary clock drift between
#: two machines that are not running NTP against the same source, narrow
#: enough that a captured delivery is useless by the time it is replayed.
DEFAULT_TOLERANCE_SECONDS = 300

#: The prefix on a generated signing secret. It exists so that a secret
#: pasted into a chat, a ticket or a log is recognisable as one — the same
#: reason API keys carry `tsk_`, and the same reason a scanner can be taught
#: one pattern instead of "any high-entropy string".
SECRET_PREFIX = "whsec_"

#: 256 bits, the same CSPRNG strength as an API key secret (CONTRACT-3 §5).
_SECRET_BYTES = 32

#: The most `v1=` digests `parse_header` keeps. A delivery carries at most two
#: (a rotation overlap), so eight is generous. Unbounded, a hostile header of
#: ten thousand `v1=` values costs one HMAC compare per value per live secret
#: INSIDE the helper consumers paste into their servers — attacker-chosen CPU
#: on the verifying side (2026-09-13 adversarial review).
MAX_SIGNATURES = 8

BodyLike = Union[bytes, bytearray, memoryview, str]


class SignatureFormatError(ValueError):
    """The header is not a `t=…,v1=…` signature at all.

    Kept separate from "the signature did not match" because the two mean
    different things to whoever is reading the log: a malformed header is
    usually a proxy that rewrote it or a consumer reading the wrong header,
    while a mismatch is either the wrong secret or a tampered body.
    """


@dataclass(frozen=True)
class ParsedSignature:
    """The two things a header carries: when it was signed, and the digests."""

    timestamp: int
    signatures: Tuple[str, ...]

    def age_seconds(self, *, now: Optional[float] = None) -> float:
        """Seconds since signing. NEGATIVE when the signer's clock is ahead —
        which `verify()` treats exactly as strictly as a stale signature,
        because a future timestamp is how a replay buys itself a longer
        window."""
        return (time.time() if now is None else float(now)) - self.timestamp


def new_secret() -> str:
    """A fresh signing secret for an endpoint. Shown once in the console."""
    return SECRET_PREFIX + _secrets.token_urlsafe(_SECRET_BYTES)


def _as_bytes(body: BodyLike) -> bytes:
    """The raw body, exactly as it will be written to the socket.

    A `str` is encoded UTF-8 and nothing else — no re-serialisation, no
    normalisation. `sender.payload_bytes()` is the one place that turns a
    payload dict into these bytes, and the same object is both signed and
    sent, so the two can never disagree.
    """
    if isinstance(body, (bytes, bytearray, memoryview)):
        return bytes(body)
    return str(body).encode("utf-8")


def _as_unix(timestamp: Optional[Union[int, float, datetime]]) -> int:
    if timestamp is None:
        return int(time.time())
    if isinstance(timestamp, datetime):
        if timestamp.tzinfo is None:
            # Every V34 column is timestamptz and every comparison here is
            # absolute. A naive datetime would be read as local time on one
            # box and as UTC on another, which is a signature that verifies
            # in development and fails in production.
            raise ValueError("signer: the timestamp must be timezone-aware")
        return int(timestamp.astimezone(timezone.utc).timestamp())
    return int(timestamp)


def signing_payload(timestamp: int, body: BodyLike) -> bytes:
    """`b"<t>.<raw body>"` — the exact bytes the HMAC is taken over.

    Published so a consumer's implementation and ours cannot drift on the
    separator, the encoding of the timestamp, or whether the body is the raw
    one. The dot is unambiguous: `t` is decimal digits only, so the first `.`
    is always the separator no matter what the body starts with.
    """
    return str(int(timestamp)).encode("ascii") + b"." + _as_bytes(body)


def compute(body: BodyLike, secret: str, *, timestamp: Optional[Union[int, float, datetime]] = None) -> Tuple[int, str]:
    """`(t, hexdigest)` for one secret. The low-level primitive."""
    if not secret:
        raise ValueError("signer: a signing secret is required")
    unix = _as_unix(timestamp)
    digest = hmac.new(
        str(secret).encode("utf-8"), signing_payload(unix, body), sha256
    ).hexdigest()
    return unix, digest


def sign(body: BodyLike, secret: str, *, timestamp: Optional[Union[int, float, datetime]] = None) -> str:
    """The full header value for a single secret."""
    return signature_header(body, [secret], timestamp=timestamp)


def signature_header(
    body: BodyLike,
    secret_values: Union[str, Sequence[str]],
    *,
    timestamp: Optional[Union[int, float, datetime]] = None,
) -> str:
    """`t=…,v1=…[,v1=…]` — one `v1` per live secret, one shared `t`.

    One timestamp for all of them on purpose: the consumer checks freshness
    once and then asks only whether some digest matches. Two timestamps would
    let a consumer accept a stale digest because a fresh one sat beside it.
    """
    values = [secret_values] if isinstance(secret_values, str) else list(secret_values)
    live = [str(value) for value in values if value]
    if not live:
        raise ValueError("signer: at least one signing secret is required")
    unix = _as_unix(timestamp)
    parts = [f"t={unix}"]
    seen = set()
    for secret in live:
        if secret in seen:
            # The overlap columns can legitimately hold the same value twice
            # (a rotation that was rolled back). Sending the identical digest
            # twice tells a watcher on the wire nothing useful and makes the
            # header look like a bug.
            continue
        seen.add(secret)
        _, digest = compute(body, secret, timestamp=unix)
        parts.append(f"{SCHEME}={digest}")
    return ",".join(parts)


def parse_header(header: str) -> ParsedSignature:
    """Read `t=…,v1=…` into its parts, tolerating spaces and extra schemes.

    Unknown `k=v` pairs are IGNORED rather than refused, so adding a `v2=`
    beside `v1=` later does not break a consumer written today — that is the
    forward compatibility the scheme label is for. A header with no `t` or no
    `v1` is a `SignatureFormatError`, because there is nothing to check.
    """
    text = str(header or "").strip()
    if not text:
        raise SignatureFormatError("the signature header is empty")
    timestamp: Optional[int] = None
    digests: list = []
    for chunk in text.split(","):
        key, separator, value = chunk.strip().partition("=")
        if not separator:
            continue
        key = key.strip().lower()
        value = value.strip()
        if key == "t" and timestamp is None:
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise SignatureFormatError("the signature timestamp is not a number") from exc
        elif key == SCHEME and value:
            if len(digests) >= MAX_SIGNATURES:
                raise SignatureFormatError(
                    f"the signature header carries more than {MAX_SIGNATURES} {SCHEME} digests"
                )
            digests.append(value.lower())
    if timestamp is None:
        raise SignatureFormatError("the signature header carries no timestamp")
    if not digests:
        raise SignatureFormatError(f"the signature header carries no {SCHEME} digest")
    return ParsedSignature(timestamp=timestamp, signatures=tuple(digests))


def verify(
    body: BodyLike,
    header: str,
    secret_values: Union[str, Sequence[str]],
    *,
    tolerance_seconds: int = DEFAULT_TOLERANCE_SECONDS,
    now: Optional[Union[int, float, datetime]] = None,
) -> bool:
    """True when `header` is our signature over `body` — THE PUBLISHED HELPER.

    This is the function `/docs` shows a customer, so it answers a plain
    boolean and never raises on hostile input: a missing header, a header
    from another product, a truncated digest and a body that was rewritten in
    transit all return False. A consumer's endpoint should return 400 on
    False and never look at the payload.

    Three checks, all of which must pass:

    * the header parses, and carries a timestamp and at least one `v1`;
    * the timestamp is within `tolerance_seconds` of now IN EITHER DIRECTION.
      A far-future timestamp is refused as firmly as a stale one, because
      "the clock is ahead" is indistinguishable, from the consumer's side,
      from an attacker buying themselves a replay window;
    * some `v1` equals the digest of `"<t>.<body>"` under some live secret,
      compared with `hmac.compare_digest`.
    """
    try:
        parsed = parse_header(header)
    except SignatureFormatError:
        return False
    values = [secret_values] if isinstance(secret_values, str) else list(secret_values or ())
    live = [str(value) for value in values if value]
    if not live:
        return False
    try:
        reference = _as_unix(now) if now is not None else int(time.time())
    except ValueError:
        return False
    if tolerance_seconds is not None and abs(reference - parsed.timestamp) > int(tolerance_seconds):
        return False
    payload = signing_payload(parsed.timestamp, body)
    matched = False
    for secret in live:
        expected = hmac.new(secret.encode("utf-8"), payload, sha256).hexdigest()
        for candidate in parsed.signatures:
            # `|=` and not `or`/`any`: short-circuiting here would make the
            # response time say which secret, and which digest, matched.
            matched |= hmac.compare_digest(expected, candidate)
    return matched


def active_secrets(
    endpoint: Mapping[str, Any], *, now: Optional[datetime] = None
) -> Tuple[str, ...]:
    """Every secret a delivery must be signed with right now.

    The current one always, plus `previous_secret` while its overlap window is
    still open. Reading the window here rather than at rotation time is what
    makes the overlap self-closing: nothing has to remember to come back and
    clear the column, and an endpoint whose window lapsed silently stops
    being signed with the old secret on the very next delivery.
    """
    current = str(endpoint.get("secret") or "")
    out = [current] if current else []
    previous = str(endpoint.get("previous_secret") or "")
    if not previous:
        return tuple(out)
    expires = endpoint.get("previous_secret_expires_at")
    if isinstance(expires, str):
        # `db._row` renders every timestamptz as an ISO string, so this is the
        # shape a row from `due_webhook_deliveries` actually carries. Before
        # 2026-09-13 only a `datetime` was compared, a string fell through to
        # "still in overlap", and a lapsed rotation kept signing with the OLD
        # secret for ever — the secret the operator rotated away from because
        # it leaked. An unparseable value is treated as closed, like None.
        try:
            expires = datetime.fromisoformat(expires.strip().replace("Z", "+00:00"))
        except ValueError:
            return tuple(out)
    if isinstance(expires, datetime):
        moment = now or datetime.now(timezone.utc)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if expires <= moment:
            return tuple(out)
    elif expires is None:
        # A previous secret with no expiry is a rotation that was never
        # finished. Treating it as "forever" would make rotation pointless,
        # so it is treated as already closed and only the current secret is
        # used — the visible failure (the old consumer stops verifying) is
        # the one that gets fixed, unlike the invisible one.
        return tuple(out)
    if previous not in out:
        out.append(previous)
    return tuple(out)


def headers_for(
    body: BodyLike,
    secret_values: Union[str, Sequence[str]],
    *,
    event_id: str,
    event_type: str,
    delivery_id: str = "",
    timestamp: Optional[Union[int, float, datetime]] = None,
) -> dict:
    """The complete outbound header set for one delivery.

    `TechSara-Event-Id` is duplicated out of the payload into a header so a
    consumer can deduplicate at the edge — in a proxy, a queue, or a load
    balancer — without parsing the body first. It is NOT a substitute for the
    signature: a header is trivially forgeable, and only the `v1` digest says
    the body is ours.
    """
    out = {
        "Content-Type": "application/json",
        "User-Agent": "TechSara-Webhooks/1.0",
        "Accept": "*/*",
        SIGNATURE_HEADER: signature_header(body, secret_values, timestamp=timestamp),
        "TechSara-Event-Id": str(event_id or ""),
        "TechSara-Event-Type": str(event_type or ""),
    }
    if delivery_id:
        out["TechSara-Delivery-Id"] = str(delivery_id)
    return out


def redact_secret(secret: str) -> str:
    """`whsec_…` plus four characters, for a log line or a console list.

    The ONLY form of a signing secret that may appear anywhere but the
    database row and the one-time reveal — the same rule API keys follow
    (`apiplatform/keys.redact`).
    """
    text = str(secret or "")
    if not text:
        return "<empty>"
    if not text.startswith(SECRET_PREFIX) or len(text) <= len(SECRET_PREFIX) + 4:
        return "<malformed>"
    return f"{SECRET_PREFIX}…{text[-4:]}"


__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "MAX_SIGNATURES",
    "ParsedSignature",
    "SCHEME",
    "SECRET_PREFIX",
    "SIGNATURE_HEADER",
    "SignatureFormatError",
    "active_secrets",
    "compute",
    "headers_for",
    "new_secret",
    "parse_header",
    "redact_secret",
    "sign",
    "signature_header",
    "signing_payload",
    "verify",
]
