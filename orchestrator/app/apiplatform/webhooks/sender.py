"""One delivery attempt: what we send, how it is signed, and when we try again.

CONTRACT-3 §14 makes three promises about an outbound webhook, and this
module is where each becomes code.

THE PAYLOAD CARRIES NO CONTENT. "The payload carries the response id and
bounded metadata, never the prompt or the generated text, unless the project
has explicitly opted in." A webhook goes to a URL a customer typed into a
console months ago, over a TLS connection we do not control the other end of,
and is replayed from a delivery-history row afterwards. Putting a person's
prompt in it would spread the one thing this platform is careful not to store
(CONTRACT-3 §16: "prompt and output content are not stored by default") into
a place with none of the same controls. `build_payload` therefore builds from
an ALLOWLIST of fields — adding a column to `api_responses` cannot leak it
into a payload by accident — and reads `output_text` only when the endpoint
row says `include_output`.

RETRIES ARE BOUNDED AND JITTERED. "Retries are bounded exponential backoff
with jitter, a maximum attempt count, and a recorded history — never
infinite." The attempt counter lives in the database and moves only inside
`db.record_webhook_attempt`, so a loop cannot run forever by forgetting to
increment it; `backoff_seconds` doubles, clamps at `CAP_DELAY_SECONDS`, and
spreads each delay over a ±25% band so that a consumer coming back from an
outage is not hit by every queued delivery in the same tenth of a second.

A POLICY REFUSAL IS NOT A FAILURE TO RETRY. `ssrf.UnsafeWebhookURL` means the
endpoint points somewhere we must never connect to. Retrying it six times
would turn the delivery loop into a slow scan of the private network, so it
is recorded as `dropped` on the first attempt and never scheduled again. A
DNS blip, a refused connection, a timeout or a 500 are `pending` until the
attempt cap, then `failed`. That distinction is the whole reason
`apiplatform/webhooks/ssrf.py` raises two different exception types.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from ... import db
from . import signer, ssrf

log = logging.getLogger(__name__)

#: The events a project may subscribe an endpoint to (CONTRACT-3 §14).
RESPONSE_COMPLETED = "response.completed"
RESPONSE_FAILED = "response.failed"
RESPONSE_CANCELLED = "response.cancelled"
SUBSCRIBABLE_EVENTS = (RESPONSE_COMPLETED, RESPONSE_FAILED, RESPONSE_CANCELLED)

#: Sent only when a person presses "send test event" in the console. It is
#: deliberately NOT subscribable: a test that could be subscribed to would
#: eventually be relied on as a heartbeat, and then somebody's monitoring
#: would break the day we stopped sending it.
EVENT_TEST = "webhook.test"

#: Matches the `max_attempts` default of the V34 DDL. Six attempts on the
#: backoff below spans roughly five minutes, which covers a rolling restart
#: of a consumer without keeping a dead endpoint's queue alive for days.
MAX_ATTEMPTS = 6

#: The first retry waits this long; each subsequent one doubles.
BASE_DELAY_SECONDS = 10.0

#: The ceiling a doubling delay clamps to. Nothing here reaches it at six
#: attempts — it is the guard for the day somebody raises `max_attempts` on a
#: row and the doubling would otherwise schedule a delivery next week.
CAP_DELAY_SECONDS = 3600.0

#: ±25%. Not full jitter (0…delay): a delay that can be near zero defeats the
#: backoff on the very attempt that most needs it, when a consumer has just
#: come back and every queued delivery is due at once.
JITTER_RATIO = 0.25

#: No retry is ever scheduled sooner than this, whatever the jitter rolls.
MIN_DELAY_SECONDS = 1.0


@dataclass(frozen=True)
class Attempt:
    """The outcome of one attempt, as it was written to the history row."""

    delivery_id: str
    status: str
    attempts_made: int
    http_status: Optional[int] = None
    error: str = ""
    next_attempt_at: Optional[datetime] = None

    @property
    def delivered(self) -> bool:
        return self.status == "delivered"

    @property
    def will_retry(self) -> bool:
        return self.status == "pending"


def _now(now: Optional[datetime] = None) -> datetime:
    return now or datetime.now(timezone.utc)


def backoff_seconds(
    attempts_made: int,
    *,
    base: float = BASE_DELAY_SECONDS,
    cap: float = CAP_DELAY_SECONDS,
    jitter: float = JITTER_RATIO,
    rng: Optional[random.Random] = None,
) -> float:
    """How long to wait before attempt number `attempts_made + 1`.

    `attempts_made` is how many attempts have ALREADY been made, which is what
    `db.record_webhook_attempt` has just written, so the caller never has to
    reason about an off-by-one between the counter and the delay.

    The clamp is applied BEFORE the jitter, so the jitter can push a delay up
    to 25% past the cap. That is deliberate: clamping after would pile every
    exhausted-backoff delivery onto exactly `cap` seconds, which is the
    thundering herd the jitter exists to prevent.
    """
    n = max(1, int(attempts_made))
    # 2 ** n grows without bound and the exponent comes from a database
    # column; capping the exponent keeps a hand-edited `max_attempts` of 400
    # from computing a float that overflows before the min() sees it.
    doublings = min(n - 1, 32)
    delay = min(float(cap), float(base) * (2.0 ** doublings))
    spread = max(0.0, float(jitter))
    if spread:
        source = rng or random
        delay = source.uniform(delay * (1.0 - spread), delay * (1.0 + spread))
    return max(MIN_DELAY_SECONDS, delay)


def next_attempt_at(
    attempts_made: int,
    *,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
    **kwargs: Any,
) -> datetime:
    """The absolute time `backoff_seconds` works out to."""
    return _now(now) + timedelta(seconds=backoff_seconds(attempts_made, rng=rng, **kwargs))


def event_id_for(response_id: str, event_type: str) -> str:
    """A stable id for "this response reaching this state", once.

    Deterministic rather than random on purpose. The unique index on
    `(endpoint_id, event_id)` then makes a double enqueue — two code paths
    both noticing a completion, a retried console action — a no-op instead of
    a second delivery, and a consumer that deduplicates on `event_id` gets the
    idempotency CONTRACT-3 §14 promises even across our own restarts. It is a
    digest, not the ids in clear, so an event id reveals nothing to a consumer
    about another project's identifiers.
    """
    material = f"{response_id}|{event_type}".encode("utf-8")
    return "evt_" + hashlib.sha256(material).hexdigest()[:24]


def _usage_of(response: Mapping[str, Any]) -> Optional[Dict[str, int]]:
    """Tokens, or None when the engine never reported any.

    NULL in `api_responses.input_tokens` means NOT MEASURED (SCHEMA-V34), and
    CONTRACT-3 §9 says usage is null — never 0 — in that case. A consumer
    billing off this payload must see the difference.
    """
    input_tokens = response.get("input_tokens")
    output_tokens = response.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    prompt = int(input_tokens or 0)
    completion = int(output_tokens or 0)
    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _timestamp(value: Any) -> Optional[int]:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(moment.timestamp())
    return None


def build_payload(
    response: Mapping[str, Any],
    event_type: str,
    *,
    include_output: bool = False,
    event_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The JSON a consumer receives.

    Built field by field from an ALLOWLIST. `api_responses` holds a
    `fingerprint` (an idempotency hash of the request body), a `request_id`
    and an `output_text`; none of them belong in a payload by default, and a
    `{**row}` spread would have shipped all three the first time somebody
    added a column. The list below is the contract, and the test
    `test_the_payload_carries_no_prompt_or_output_text_by_default` reads it
    back out of a real row to prove it.
    """
    response_id = str(response.get("id") or "")
    payload: Dict[str, Any] = {
        "id": event_id or event_id_for(response_id, event_type),
        "object": "event",
        "type": str(event_type),
        "created_at": int(_now(now).timestamp()),
        "data": {
            "response": {
                "id": response_id,
                "object": "response",
                "status": str(response.get("status") or ""),
                "model": str(response.get("model") or ""),
                "background": bool(response.get("background")),
                "created_at": _timestamp(response.get("created_at")),
                "completed_at": _timestamp(response.get("completed_at")),
                "usage": _usage_of(response),
                # The caller's OWN metadata, which they set on the request and
                # which CONTRACT-3 §8 already bounds to 16 keys of 512 chars.
                # Echoing it is how a consumer correlates the event with the
                # job that asked for it without us inventing a second id.
                "metadata": dict(response.get("metadata") or {}),
            }
        },
    }
    error_code = response.get("error_code")
    if error_code:
        payload["data"]["response"]["error"] = {
            "code": str(error_code),
            "message": str(response.get("error_message") or ""),
        }
    if include_output:
        # The opt-in of CONTRACT-3 §14, and the ONLY branch in this module
        # that can put generated text on the wire. Shaped exactly like
        # CONTRACT-3 §9's `output`, so a consumer parses the webhook and the
        # `GET /v1/responses/{id}` body with one type.
        text = response.get("output_text")
        payload["data"]["response"]["output"] = (
            []
            if text is None
            else [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": str(text)}],
                }
            ]
        )
    return payload


def payload_bytes(payload: Mapping[str, Any]) -> bytes:
    """The exact bytes that are signed AND sent.

    One serialisation, used twice, because a signature over a different
    rendering of the same object is a signature over a different message.
    `sort_keys` and the compact separators are not cosmetic: they make the
    bytes reproducible, so a delivery replayed from its history row signs
    identically to the original.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def _subscribed(endpoint: Mapping[str, Any], event_type: str) -> bool:
    if str(endpoint.get("status") or "active") != "active":
        return False
    events = endpoint.get("events") or []
    try:
        return event_type in {str(name) for name in events}
    except TypeError:  # pragma: no cover — a malformed jsonb column
        return False


async def emit_response_event(
    response: Mapping[str, Any],
    event_type: str,
    *,
    workspace_id: str,
    now: Optional[datetime] = None,
) -> List[str]:
    """Queue this event to every active endpoint subscribed to it.

    Queue, not send: the row is durable before anything is dialled, so a
    process that dies between "the response completed" and "the consumer
    acknowledged" loses nothing — the sweep in `worker.py` picks the delivery
    up on the next start. Returns the delivery ids that were created; an
    endpoint that already has this `event_id` queued contributes nothing,
    which is the unique index doing its job rather than an error.

    Never raises into the caller. A background response that completed must
    not be recorded as failed because a webhook row could not be written
    (2026-09-13: this is the ONLY call the response task makes after its own
    terminal update, and it is best-effort by design).
    """
    project_id = str(response.get("project_id") or "")
    response_id = str(response.get("id") or "")
    if not project_id or not response_id:
        return []
    try:
        endpoints = await db.run_in_thread(
            db.list_webhook_endpoints, project_id, workspace_id
        )
    except Exception:  # noqa: BLE001 — see the docstring
        log.warning("could not list webhook endpoints for %s", project_id, exc_info=True)
        return []
    queued: List[str] = []
    event_id = event_id_for(response_id, event_type)
    for endpoint in endpoints:
        if not _subscribed(endpoint, event_type):
            continue
        payload = build_payload(
            response,
            event_type,
            include_output=bool(endpoint.get("include_output")),
            event_id=event_id,
            now=now,
        )
        try:
            row = await db.run_in_thread(
                db.enqueue_webhook_delivery,
                endpoint["id"],
                project_id,
                event_type,
                event_id,
                payload,
                response_id=response_id,
                max_attempts=MAX_ATTEMPTS,
            )
        except Exception:  # noqa: BLE001 — see the docstring
            log.warning(
                "could not queue %s for endpoint %s", event_type, endpoint.get("id"),
                exc_info=True,
            )
            continue
        if row is not None:
            queued.append(str(row["id"]))
    if queued:
        _kick_worker()
    return queued


def _kick_worker() -> None:
    """Wake the sweep: something was just queued.

    Without it every event waited up to a full poll interval (5 s × 1.15), and
    a console "send test event" press looked broken for that long — `kick()`
    was documented as called here and was called nowhere (2026-09-13 review).
    Imported at call time because `worker` imports this module.
    """
    from . import worker

    worker.kick()


async def enqueue_test_delivery(
    endpoint: Mapping[str, Any], *, now: Optional[datetime] = None
) -> Optional[dict]:
    """The console's "send a test event", queued like any other delivery.

    It goes through the same signing, the same SSRF guard and the same retry
    ledger, because a test that took a shortcut would prove the shortcut
    works rather than the delivery path. Its `event_id` is unique per press,
    so pressing twice sends twice — that is what a person testing an endpoint
    expects, and it is the one place a non-deterministic event id is right.
    """
    moment = _now(now)
    event_id = event_id_for(
        f"{endpoint.get('id')}|{moment.timestamp()}", EVENT_TEST
    )
    payload = {
        "id": event_id,
        "object": "event",
        "type": EVENT_TEST,
        "created_at": int(moment.timestamp()),
        "data": {"endpoint_id": str(endpoint.get("id") or "")},
    }
    row = await db.run_in_thread(
        db.enqueue_webhook_delivery,
        endpoint["id"],
        endpoint["project_id"],
        EVENT_TEST,
        event_id,
        payload,
        max_attempts=MAX_ATTEMPTS,
    )
    if row is not None:
        _kick_worker()
    return row


async def _record(
    delivery_id: str,
    *,
    status: str,
    attempts_made: int,
    http_status: Optional[int] = None,
    error: str = "",
    scheduled: Optional[datetime] = None,
) -> Attempt:
    """Write the attempt to its history row, off the event loop.

    `db.record_webhook_attempt` is the only place the attempt counter moves
    (SCHEMA-V34), and it is a synchronous accessor like every other one in
    `app/db.py` — so it goes through `run_in_thread`, or one slow UPDATE
    would stall every in-flight SSE stream on this process.
    """
    # The history row is DISPLAYED by the console, so any URL an error quotes
    # — an httpx message, a consumer's error page naming its own callback —
    # loses its query string and userinfo before it is stored (2026-09-13:
    # webhook tokens were reaching console responses in clear).
    error = ssrf.redact_urls_in_text(error)
    await db.run_in_thread(
        db.record_webhook_attempt,
        delivery_id,
        status=status,
        http_status=http_status,
        error=error,
        next_attempt_at=scheduled,
    )
    return Attempt(
        delivery_id=delivery_id,
        status=status,
        attempts_made=attempts_made,
        http_status=http_status,
        error=error,
        next_attempt_at=scheduled,
    )


async def deliver(
    delivery: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
) -> Attempt:
    """Make ONE attempt at a delivery and write the outcome to its history.

    `delivery` is a row from `db.due_webhook_deliveries`, which carries the
    endpoint it must go to under `endpoint` (url, secret, previous_secret,
    the overlap expiry, include_output). Never raises: every path ends in a
    recorded attempt, because a delivery that failed without being recorded
    would be retried by the next sweep with its counter unmoved — forever.
    """
    delivery_id = str(delivery.get("id") or "")
    endpoint = delivery.get("endpoint") or {}
    attempts_made = int(delivery.get("attempt") or 0) + 1
    max_attempts = int(delivery.get("max_attempts") or MAX_ATTEMPTS)
    moment = _now(now)

    secrets_in_use = signer.active_secrets(endpoint, now=moment)
    if not secrets_in_use:
        # An endpoint with no secret cannot be signed, and an unsigned
        # delivery is indistinguishable from a forged one. Dropped rather
        # than retried: no amount of waiting adds a secret to the row.
        return await _record(
            delivery_id,
            status="dropped",
            attempts_made=attempts_made,
            error="the endpoint has no signing secret",
        )

    body = payload_bytes(delivery.get("payload") or {})
    headers = signer.headers_for(
        body,
        secrets_in_use,
        event_id=str(delivery.get("event_id") or ""),
        event_type=str(delivery.get("event_type") or ""),
        delivery_id=delivery_id,
        timestamp=moment,
    )

    try:
        response = await ssrf.post_json(str(endpoint.get("url") or ""), body, headers)
    except ssrf.UnsafeWebhookURL as exc:
        # PERMANENT. See the module docstring: retrying a policy refusal is a
        # port scan on a timer.
        log.warning(
            "webhook delivery %s dropped: the endpoint is not a permitted target (%s)",
            delivery_id, exc,
        )
        return await _record(
            delivery_id,
            status="dropped",
            attempts_made=attempts_made,
            error=f"refused by the endpoint policy: {exc}",
        )
    except ssrf.WebhookTransportError as exc:
        return await _finish_failure(
            delivery_id,
            attempts_made=attempts_made,
            max_attempts=max_attempts,
            error=str(exc),
            http_status=None,
            now=moment,
            rng=rng,
        )
    except Exception as exc:  # noqa: BLE001 — never leave an attempt unrecorded
        log.warning("webhook delivery %s failed unexpectedly", delivery_id, exc_info=True)
        return await _finish_failure(
            delivery_id,
            attempts_made=attempts_made,
            max_attempts=max_attempts,
            error=f"delivery failed: {type(exc).__name__}",
            http_status=None,
            now=moment,
            rng=rng,
        )

    if response.ok:
        return await _record(
            delivery_id,
            status="delivered",
            attempts_made=attempts_made,
            http_status=response.status,
        )
    # Every non-2xx is retried, 4xx included. A consumer mid-deploy answers
    # 404 or 502 for a minute, and treating that as permanent would silently
    # lose the customer's event; the attempt cap is what keeps it bounded.
    return await _finish_failure(
        delivery_id,
        attempts_made=attempts_made,
        max_attempts=max_attempts,
        error=f"HTTP {response.status}: {response.excerpt}".strip(),
        http_status=response.status,
        now=moment,
        rng=rng,
    )


async def _finish_failure(
    delivery_id: str,
    *,
    attempts_made: int,
    max_attempts: int,
    error: str,
    http_status: Optional[int],
    now: datetime,
    rng: Optional[random.Random],
) -> Attempt:
    """Schedule the next attempt, or stop because the cap has been reached."""
    if attempts_made >= max_attempts:
        # `failed`, not `dropped`: we tried the full budget and the consumer
        # never took it. The console shows the difference, and so does an
        # operator deciding whether to re-enable an endpoint.
        return await _record(
            delivery_id,
            status="failed",
            attempts_made=attempts_made,
            http_status=http_status,
            error=error,
        )
    return await _record(
        delivery_id,
        status="pending",
        attempts_made=attempts_made,
        http_status=http_status,
        error=error,
        scheduled=next_attempt_at(attempts_made, now=now, rng=rng),
    )


__all__ = [
    "Attempt",
    "BASE_DELAY_SECONDS",
    "CAP_DELAY_SECONDS",
    "EVENT_TEST",
    "JITTER_RATIO",
    "MAX_ATTEMPTS",
    "RESPONSE_CANCELLED",
    "RESPONSE_COMPLETED",
    "RESPONSE_FAILED",
    "SUBSCRIBABLE_EVENTS",
    "backoff_seconds",
    "build_payload",
    "deliver",
    "emit_response_event",
    "enqueue_test_delivery",
    "event_id_for",
    "next_attempt_at",
    "payload_bytes",
]
