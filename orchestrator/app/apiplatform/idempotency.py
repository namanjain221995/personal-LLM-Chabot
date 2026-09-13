"""`Idempotency-Key` — CONTRACT-3 §13, over the race-free primitive in V34.

A generation is expensive, slow, and metered. A client that times out at
thirty seconds and retries must not be charged twice, and must not get two
different answers to the same question. So `POST /v1/responses` and
`POST /v1/chat/completions` accept an `Idempotency-Key`, scoped by
`(project_id, endpoint, key)` and retained 24 hours:

    same key + same body fingerprint  →  the original response, work not rerun
    same key + different fingerprint  →  409 idempotency_conflict
    a new key                         →  the work runs, exactly once

THE CLAIM IS THE DATABASE'S DECISION, NOT OURS. `_claim_row` is one
`INSERT … ON CONFLICT … DO UPDATE … WHERE <reclaimable> RETURNING *`: the
winner gets a row and every later claimant gets None. That single statement is
the whole concurrency argument.

A CLAIM CANNOT PIN A KEY FOREVER (2026-09-13, wave-2 review). The first cut
used `ON CONFLICT DO NOTHING`, so a row past its `expires_at` blocked the key
until a prune happened to run, and a process that died mid-generation left an
`in_flight` row that told every retry "still running" for a day. The same
statement now takes the row over when it is (a) past `expires_at`, whatever
it held; (b) `in_flight` with the SAME fingerprint and older than
`settings.public_api_idempotency_in_flight_lease_seconds` — twice the
generation wall clock, so its owner cannot still be running; or (c)
`completed` with no response (the original failed and left nothing) and the
same fingerprint. Case (c) used to be decided in Python after a read, so two
concurrent retries could both re-run the work; in the statement, exactly one
of them wins. A `SELECT` followed by an `INSERT` has a window in the middle exactly
wide enough for a client's retry to slip through and invoke the model a second
time — and the window is widest under the load that causes retries.

ONE CLOCK: THE DATABASE'S (2026-09-13, wave-3 re-verify). The wave-2 claim
decided "expired / lease passed" against a Python timestamp, and then read
the loser's view through `db.get_idempotency`, which compares `expires_at`
with the database's `now()`. When the two clocks disagreed about a row near
its deadline, the claim said "alive, you lost" and the read said "expired,
nothing here" — twice — and the request was answered `500 internal_error`.
`claim()` now reads `clock_timestamp()` once on its connection and uses that
single instant for the takeover predicate, the new deadline, and the read of
the row it lost to; all on ONE pooled connection, never two at once.

THE FINGERPRINT IS OF THE NORMALISED BODY, not of the bytes. Two requests that
differ only in key order, whitespace or float spelling are the same request,
and a client library that re-serialises its own payload between attempts must
not be told it changed its mind. `fingerprint()` canonicalises: sorted keys, no
insignificant whitespace, UTF-8, then SHA-256. It is a comparison value, not a
secret, and it is stored in clear in `api_idempotency.fingerprint`.

WHAT IS DELIBERATELY NOT STORED HERE: the body. CONTRACT-3 §16 keeps prompt and
completion content out of the request log, and a fingerprint answers "is this
the same request" without keeping the request.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional, Tuple, TypeVar

from .. import db
from ..config import settings
from ..publicapi import errors

log = logging.getLogger(__name__)

T = TypeVar("T")

#: What an `Idempotency-Key` header value may look like before it is used as
#: part of a primary key. Printable ASCII, bounded — a client picks the value,
#: so it is untrusted text that ends up in a unique index, a log line and the
#: console. 255 is Stripe's published bound and is far more than a UUID needs.
_KEY_RE = re.compile(r"\A[\x21-\x7e]{1,255}\Z")

#: The state values `api_idempotency.state` may hold (SCHEMA-V34's CHECK).
IN_FLIGHT = "in_flight"
COMPLETED = "completed"


class IdempotencyKeyError(ValueError):
    """The header value is not usable as a key.

    A `ValueError` rather than an `ApiError` so this module stays free of the
    decision about which 400 to raise — the router turns it into
    `errors.invalid_request(..., param="Idempotency-Key")`. The offending
    value is NOT carried: it is caller-controlled text and echoing it into an
    error body is how a log viewer ends up rendering someone's injected
    newlines (`publicapi/errors.py::_echoable` refuses the same thing).
    """


def normalise_key(value: Optional[str]) -> Optional[str]:
    """The header value, validated; None when the header was absent.

    An absent header means "not idempotent", which is a legitimate request —
    CONTRACT-3 §13 makes the header optional. An EMPTY or blank header is not
    the same thing: the client tried to be idempotent and sent nothing usable,
    and silently treating that as "not idempotent" would remove the protection
    the client asked for without telling anybody.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise IdempotencyKeyError("the Idempotency-Key header must be a string")
    candidate = value.strip()
    if not _KEY_RE.match(candidate):
        raise IdempotencyKeyError(
            "the Idempotency-Key header must be 1-255 printable ASCII characters"
        )
    return candidate


def fingerprint(body: Any) -> str:
    """SHA-256 over the canonical JSON form of a request body.

    `sort_keys=True` and the tightest separators, so `{"a":1,"b":2}` and
    `{ "b" : 2 , "a" : 1 }` fingerprint identically — they ARE the same
    request, and a client that re-serialises its payload on retry (every HTTP
    library does) must not be accused of changing it.

    `ensure_ascii=False` with an explicit UTF-8 encode, so the same prompt
    written in Gujarati or with an em dash produces the same digest whichever
    library serialised it.

    NO `default=str` (2026-09-13, wave-2 review). It made different bodies
    identical before the hash ran — `{"a": Decimal("1.0")}` and `{"a": "1.0"}`
    both became `{"a":"1.0"}` — which is exactly the collision the paragraph
    below says must not happen. A pydantic model is dumped with
    `model_dump(mode="json")`, which is the model's own JSON form; anything
    else JSON cannot represent (a `Decimal`, a `datetime`, NaN) raises
    `TypeError` / `ValueError` here rather than being quietly stringified.

    SHA-256 and not a cheaper digest because this value is compared to decide
    whether to REPLAY a stored answer: a collision would return one caller's
    response to another caller's request inside the same project.
    """
    if hasattr(body, "model_dump"):
        body = body.model_dump(mode="json")
    canonical = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Claim:
    """The outcome of trying to claim `(project, endpoint, key)`.

    `claimed` True means this request owns the work and must call `finish()`
    when it has an answer. False means somebody else got there first, and
    `response_id` says whether they finished: a value to replay, or None
    because the original is still running.
    """

    project_id: str
    endpoint: str
    idem_key: str
    fingerprint: str
    claimed: bool
    state: str = IN_FLIGHT
    response_id: Optional[str] = None

    @property
    def replayable(self) -> bool:
        """A finished original with a response to return."""
        return not self.claimed and self.state == COMPLETED and bool(self.response_id)

    @property
    def in_progress(self) -> bool:
        """Somebody else's attempt is still running. CONTRACT-3 §13 lets the
        caller attach to it; this module only reports the fact."""
        return not self.claimed and not self.response_id


_CLAIM_SQL = """
INSERT INTO api_idempotency
    (project_id, endpoint, idem_key, fingerprint, state, response_id, created_at, expires_at)
VALUES (%(project)s, %(endpoint)s, %(key)s, %(fingerprint)s, 'in_flight', NULL,
        %(now)s, %(expires)s)
ON CONFLICT (project_id, endpoint, idem_key) DO UPDATE SET
    fingerprint = excluded.fingerprint,
    state       = 'in_flight',
    response_id = NULL,
    created_at  = excluded.created_at,
    expires_at  = excluded.expires_at
WHERE api_idempotency.expires_at <= %(now)s
   OR (api_idempotency.fingerprint = excluded.fingerprint
       AND ((api_idempotency.state = 'in_flight'
             AND api_idempotency.created_at <= %(lease_cutoff)s)
         OR (api_idempotency.state = 'completed'
             AND api_idempotency.response_id IS NULL)))
RETURNING id
"""


_LIVE_ROW_SQL = """
SELECT fingerprint, state, response_id
  FROM api_idempotency
 WHERE project_id = %(project)s AND endpoint = %(endpoint)s AND idem_key = %(key)s
   AND expires_at > %(now)s
"""


def _database_now(con: Any) -> datetime:
    """`clock_timestamp()` on this connection — the one clock `claim` uses.
    Not `now()`, which is frozen at the start of the transaction."""
    row = con.execute("SELECT clock_timestamp() AS at").fetchone()
    value = row["at"] if isinstance(row, dict) else row[0]
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _live_row_on(
    con: Any, project_id: str, endpoint: str, idem_key: str, now: datetime
) -> Optional[dict]:
    """The row this claim lost to, judged alive by the SAME instant the claim
    statement used — so "you lost" and "here is who won" cannot disagree."""
    row = con.execute(
        _LIVE_ROW_SQL,
        {"project": project_id, "endpoint": str(endpoint), "key": str(idem_key), "now": now},
    ).fetchone()
    return dict(row) if row is not None else None


def _claim_row(
    con: Any,
    project_id: str,
    endpoint: str,
    idem_key: str,
    body_fingerprint: str,
    *,
    ttl_hours: float,
    now: datetime,
) -> bool:
    """One statement: claim a fresh key or take over a reclaimable row.
    True when this call owns the work. Runs on the caller's connection."""
    lease = max(0.0, float(settings.public_api_idempotency_in_flight_lease_seconds))
    row = con.execute(
        _CLAIM_SQL,
        {
            "project": project_id,
            "endpoint": str(endpoint),
            "key": str(idem_key),
            "fingerprint": body_fingerprint,
            "now": now,
            "expires": now + timedelta(hours=float(ttl_hours)),
            "lease_cutoff": now - timedelta(seconds=lease),
        },
    ).fetchone()
    return row is not None


def _moment(now: Optional[datetime]) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("a naive datetime cannot be compared with a timestamptz")
    return now.astimezone(timezone.utc)


def claim(
    project_id: str,
    endpoint: str,
    idem_key: str,
    body: Any,
    *,
    ttl_hours: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Claim:
    """Try to own this idempotency key, or discover who does.

    Raises `errors.ApiError` `409 idempotency_conflict` when the key was
    already used with a DIFFERENT body — never a 200 with the old answer,
    because returning one request's response to a different request is worse
    than refusing both.

    THE COMPLETED-WITHOUT-A-RESPONSE CASE: a claim in state `completed` whose
    `response_id` is NULL means the original attempt finished WITHOUT
    producing anything durable — it raised. Re-running the work then is not a
    double invocation, and refusing would poison the key for a day over one
    transient failure. It is taken over INSIDE the claim statement, so of two
    concurrent retries exactly one re-runs the work and the other is told the
    first is in progress.
    """
    body_fingerprint = fingerprint(body)
    if now is not None:
        _moment(now)  # a naive datetime is refused before a connection is taken
    ttl = (
        float(ttl_hours)
        if ttl_hours is not None
        else float(settings.public_api_idempotency_ttl_hours)
    )
    existing: Optional[dict] = None
    with db.connection() as con:
        # ONE instant for every comparison below (see "ONE CLOCK" above).
        moment = _moment(now) if now is not None else _database_now(con)
        won = _claim_row(
            con, project_id, endpoint, idem_key, body_fingerprint, ttl_hours=ttl, now=moment
        )
        if not won:
            # Commit first: the losing ON CONFLICT still row-locked the
            # winner's row, and the winner's `finish` must not wait on our read.
            con.commit()
            existing = _live_row_on(con, project_id, endpoint, idem_key, moment)
            if existing is None:
                # Lost the INSERT and then found nothing live: the winner
                # released its claim between the two statements. Rare, and
                # recoverable — try the claim once more, same instant.
                won = _claim_row(
                    con, project_id, endpoint, idem_key, body_fingerprint,
                    ttl_hours=ttl, now=moment,
                )
                if not won:
                    con.commit()
                    existing = _live_row_on(con, project_id, endpoint, idem_key, moment)
    if won:
        return Claim(
            project_id=project_id,
            endpoint=endpoint,
            idem_key=idem_key,
            fingerprint=body_fingerprint,
            claimed=True,
        )
    if existing is None:
        # Twice in a row is not a race, it is a broken store. Refusing is the
        # only safe answer: proceeding would run the work with no idempotency
        # protection at all, which is the one thing the caller asked for.
        log.error(
            "idempotency claim for project %s could neither be won nor read",
            project_id,
        )
        raise errors.internal_error()

    stored = str(existing.get("fingerprint") or "")
    if stored != body_fingerprint:
        raise errors.idempotency_conflict()

    state = str(existing.get("state") or IN_FLIGHT)
    response_id = existing.get("response_id") or None
    # A `completed` row with no response and this fingerprint cannot be seen
    # here unless another retry took it over between our statement and this
    # read — in which case it is now theirs and in flight. Reported as such.
    return Claim(
        project_id=project_id,
        endpoint=endpoint,
        idem_key=idem_key,
        fingerprint=body_fingerprint,
        claimed=False,
        state=state,
        response_id=str(response_id) if response_id else None,
    )


def finish(held: Claim, response_id: Optional[str]) -> None:
    """Attach the finished response to the claim so a retry can replay it.

    `response_id=None` is the FAILURE signal, and it is meaningful: it marks
    the claim completed with nothing to replay, which `claim()` reads as "the
    original produced nothing, re-run it". Without this call on the failure
    path, a request that raised would leave its claim `in_flight` and every
    retry of that key would be told to attach to an attempt that is not
    running, for a full day.
    """
    db.finish_idempotency(
        held.project_id, held.endpoint, held.idem_key, response_id or None
    )


def execute_once(
    *,
    project_id: str,
    endpoint: str,
    idem_key: Optional[str],
    body: Any,
    work: Callable[[], Tuple[Optional[str], T]],
    replay: Callable[[Optional[str]], T],
    ttl_hours: Optional[float] = None,
    now: Optional[datetime] = None,
) -> T:
    """Run `work` at most once for this `(project, endpoint, key)`.

    * `idem_key=None` — the header was absent, so there is nothing to
      de-duplicate against and `work()` simply runs. The result is not
      recorded anywhere, because there is no key to record it under;
    * `work()` returns `(response_id, payload)`. The id is what gets attached
      to the claim, so a later retry can find the answer; the payload is what
      this call returns;
    * `replay(response_id)` is called instead when the key is already claimed.
      The argument is None when the ORIGINAL IS STILL RUNNING — CONTRACT-3 §13
      lets the caller attach to it, and only the router knows how (for a
      stream, that means joining it; for a background response, reading the
      row). Deciding that here would drag HTTP into this module.

    If `work()` raises, the claim is marked completed with no response so the
    caller's key is not poisoned for a day, and the exception is re-raised
    unchanged — a failure must surface as itself, not as an idempotency error.
    """
    if not idem_key:
        _, payload = work()
        return payload

    held = claim(project_id, endpoint, idem_key, body, ttl_hours=ttl_hours, now=now)
    if not held.claimed:
        return replay(held.response_id)

    try:
        response_id, payload = work()
    except BaseException:
        # Release the claim, but NEVER at the cost of the original exception.
        # If the database is the thing that failed, `finish` fails too, and a
        # `psycopg.OperationalError` replacing the real cause is how an
        # incident gets diagnosed as the wrong outage. The claim then expires
        # on its own TTL, which is the slow-but-correct fallback.
        try:
            finish(held, None)
        except Exception:  # noqa: BLE001
            log.exception(
                "could not release the idempotency claim for project %s after a "
                "failed request; it will expire on its own",
                project_id,
            )
        raise
    finish(held, response_id)
    return payload


__all__ = [
    "COMPLETED",
    "IN_FLIGHT",
    "Claim",
    "IdempotencyKeyError",
    "claim",
    "execute_once",
    "fingerprint",
    "finish",
    "normalise_key",
]
