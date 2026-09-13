"""Outbound webhooks: sign it, refuse to dial anything private, retry a bounded
number of times, and record what happened (CONTRACT-3 §14).

A webhook is the only place this platform makes an outbound connection to an
address a customer chose, which makes it the one piece of the developer
platform whose threat model points OUTWARD. The package is split so that each
of the four decisions can be read, and tested, on its own:

    signer.py   the `TechSara-Signature` header: HMAC-SHA256 over
                `"<t>.<raw body>"`, a ±5-minute tolerance, a constant-time
                compare, two live secrets during a rotation, and the
                `verify()` helper we publish verbatim in `/docs`
    ssrf.py     the CONTRACT-3 §14 defence: https only, no credentials in the
                URL, every resolved address checked against `core/net`'s
                policy, the connection pinned to a checked address so DNS
                cannot be rebound between check and connect, three redirects
                each re-validated, one 10-second budget, a bounded read
    sender.py   what one delivery carries (never a prompt, never generated
                text unless the project opted in), and when the next attempt
                is due — bounded exponential backoff with jitter
    queue.py    the FAIR, CLAIMING sweep query (per-project round robin,
                `FOR UPDATE SKIP LOCKED` plus a lease) and the console's
                delivery history
    worker.py   the loop that picks up due deliveries; `start()`/`stop()` for
                the lifespan, which this package deliberately does not edit

Two rules hold across all four and are each pinned by a test in
`tests/test_api_platform_webhooks.py`:

* nothing here ever logs, returns or stores a signing secret — only
  `signer.redact_secret()` renders one outside its database row;
* a refusal by the address policy is PERMANENT and a network failure is
  TRANSIENT, and the two are different exception types precisely so a retry
  loop cannot turn the first into a slow scan of the private network.
"""
from __future__ import annotations

from . import queue, sender, signer, ssrf, worker
from .sender import (
    MAX_ATTEMPTS,
    RESPONSE_CANCELLED,
    RESPONSE_COMPLETED,
    RESPONSE_FAILED,
    SUBSCRIBABLE_EVENTS,
    Attempt,
    backoff_seconds,
    build_payload,
    deliver,
    emit_response_event,
    event_id_for,
    payload_bytes,
)
from .signer import (
    DEFAULT_TOLERANCE_SECONDS,
    SIGNATURE_HEADER,
    active_secrets,
    new_secret,
    sign,
    signature_header,
    verify,
)
from .ssrf import (
    UnsafeWebhookURL,
    WebhookResponse,
    WebhookTransportError,
    post_json,
    validate_url,
)

__all__ = [
    "Attempt",
    "DEFAULT_TOLERANCE_SECONDS",
    "MAX_ATTEMPTS",
    "RESPONSE_CANCELLED",
    "RESPONSE_COMPLETED",
    "RESPONSE_FAILED",
    "SIGNATURE_HEADER",
    "SUBSCRIBABLE_EVENTS",
    "UnsafeWebhookURL",
    "WebhookResponse",
    "WebhookTransportError",
    "active_secrets",
    "backoff_seconds",
    "build_payload",
    "deliver",
    "emit_response_event",
    "event_id_for",
    "new_secret",
    "payload_bytes",
    "post_json",
    "queue",
    "sender",
    "sign",
    "signature_header",
    "signer",
    "ssrf",
    "validate_url",
    "verify",
    "worker",
]
