"""Outbound webhooks — the signature, the SSRF refusals, and the retry ledger.

CONTRACT-3 §14 is the only part of the developer platform whose threat model
points OUTWARD: a webhook URL is a string a customer typed, and this box then
connects to it. So the properties pinned here are the ones whose failure is an
incident rather than a bug report.

1. THE SIGNATURE PROVES THE BODY AND THE MOMENT. A signature that covered only
   the body would be replayable forever by anyone who ever saw one delivery,
   so `t` is inside the signed string and a stale (or future-dated) timestamp
   is refused with the same firmness as a tampered byte.

2. EVERY SSRF CASE IS REFUSED, AND THE TABLE IS THE TEST. The cases below are
   parametrised rather than written out, because the one that gets forgotten
   in a hand-written list is the one that matters —
   `::ffff:127.0.0.1` was missing from the first draft of this file, and it
   passes `is_global` on a mapped public address.

3. A POLICY REFUSAL AND A NETWORK FAILURE ARE DIFFERENT THINGS. `core/net`
   raises one exception for both; this package raises two, because a delivery
   loop that retried "that address is forbidden" six times would be a slow
   scan of the private network on a timer.

4. RETRIES END. Growing delay, a hard attempt cap, and a recorded history row
   for every attempt — asserted against real PostgreSQL, because the counter
   that bounds the loop lives in a column.

The HTTP layer is exercised with `httpx.MockTransport`: the pinned transport
itself is `app/core/net.py`'s and has its own coverage in
`tests/test_net_ssrf.py` (including a real loopback TLS handshake). What is
under test here is the policy around it — which URLs are refused, which
redirects are re-validated, and what is recorded afterwards.
"""
from __future__ import annotations

import asyncio
import json
import random
import socket
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import db
from app.apiplatform.webhooks import queue, sender, signer, ssrf, worker

WORKSPACE = "ws-webhooks"
OTHER_WORKSPACE = "ws-webhooks-other"

SECRET = "whsec_the-current-signing-secret-0123456789"
OLD_SECRET = "whsec_the-previous-signing-secret-abcdefgh"

BODY = b'{"id":"evt_1","type":"response.completed"}'


# ---------------------------------------------------------------------------
# 1. The signature
# ---------------------------------------------------------------------------


def test_a_signature_round_trips_over_the_exact_bytes_that_were_sent():
    header = signer.sign(BODY, SECRET)

    assert header.startswith("t=")
    assert ",v1=" in header
    assert signer.verify(BODY, header, SECRET) is True


def test_a_body_that_was_tampered_with_in_transit_does_not_verify():
    header = signer.sign(BODY, SECRET)

    tampered = BODY.replace(b"completed", b"failed\x20\x20\x20")

    assert len(tampered) == len(BODY)  # same length, so nothing but the MAC can catch it
    assert signer.verify(tampered, header, SECRET) is False


def test_a_signature_made_with_another_secret_does_not_verify():
    header = signer.sign(BODY, "whsec_someone-elses-secret-000000000000000")

    assert signer.verify(BODY, header, SECRET) is False


def test_a_replayed_delivery_is_refused_once_it_is_older_than_the_tolerance():
    """The whole reason `t` is inside the signed string: a captured delivery
    must stop being acceptable, and the consumer must be able to tell."""
    signed_at = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
    header = signer.sign(BODY, SECRET, timestamp=signed_at)

    just_inside = signed_at + timedelta(seconds=signer.DEFAULT_TOLERANCE_SECONDS)
    just_outside = signed_at + timedelta(seconds=signer.DEFAULT_TOLERANCE_SECONDS + 1)

    assert signer.verify(BODY, header, SECRET, now=just_inside) is True
    assert signer.verify(BODY, header, SECRET, now=just_outside) is False


def test_a_timestamp_from_the_future_is_refused_as_firmly_as_a_stale_one():
    """A far-future `t` is how a replay would buy itself a longer window, and
    from the consumer's side it is indistinguishable from a wrong clock."""
    signed_at = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
    header = signer.sign(BODY, SECRET, timestamp=signed_at)

    long_before = signed_at - timedelta(seconds=signer.DEFAULT_TOLERANCE_SECONDS + 1)

    assert signer.verify(BODY, header, SECRET, now=long_before) is False


def test_the_timestamp_in_the_header_cannot_be_moved_without_breaking_the_digest():
    signed_at = datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)
    header = signer.sign(BODY, SECRET, timestamp=signed_at)
    parsed = signer.parse_header(header)
    moved = f"t={parsed.timestamp + 600},v1={parsed.signatures[0]}"

    # Fresh by the clock, and still refused: `t` is signed, not merely sent.
    assert signer.verify(BODY, moved, SECRET, now=signed_at + timedelta(seconds=600)) is False


def test_a_rotation_signs_with_both_secrets_so_neither_consumer_misses_a_delivery():
    endpoint = {
        "secret": SECRET,
        "previous_secret": OLD_SECRET,
        "previous_secret_expires_at": datetime.now(timezone.utc) + timedelta(days=1),
    }

    live = signer.active_secrets(endpoint)
    header = signer.signature_header(BODY, live)

    assert live == (SECRET, OLD_SECRET)
    assert header.count("v1=") == 2
    # A consumer that has already switched, and one that has not, both verify.
    assert signer.verify(BODY, header, SECRET) is True
    assert signer.verify(BODY, header, OLD_SECRET) is True


def test_the_old_secret_stops_being_used_the_moment_its_overlap_lapses():
    endpoint = {
        "secret": SECRET,
        "previous_secret": OLD_SECRET,
        "previous_secret_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1),
    }

    live = signer.active_secrets(endpoint)

    assert live == (SECRET,)
    assert signer.verify(BODY, signer.signature_header(BODY, live), OLD_SECRET) is False


def test_a_previous_secret_with_no_expiry_is_treated_as_closed_not_as_forever():
    """An unfinished rotation must not leave the old secret live indefinitely,
    or rotation would buy nothing."""
    endpoint = {"secret": SECRET, "previous_secret": OLD_SECRET, "previous_secret_expires_at": None}

    assert signer.active_secrets(endpoint) == (SECRET,)


@pytest.mark.parametrize(
    "header",
    [
        "",
        "   ",
        "nonsense",
        "v1=abc",  # no timestamp
        "t=1789200000",  # no digest
        "t=not-a-number,v1=abc",
        "t=1789200000,v1=",
    ],
)
def test_a_malformed_signature_header_is_false_and_never_an_exception(header):
    """`verify()` is the helper we publish, so a customer pastes it into a
    route that hostile input reaches directly. It answers a boolean."""
    assert signer.verify(BODY, header, SECRET) is False


def test_an_unknown_scheme_beside_v1_is_ignored_rather_than_refused():
    """Forward compatibility: adding `v2=` later must not break a consumer
    written today."""
    header = signer.sign(BODY, SECRET) + ",v2=whatever-comes-next"

    assert signer.verify(BODY, header, SECRET) is True


def test_verify_is_false_rather_than_true_when_no_secret_is_supplied():
    header = signer.sign(BODY, SECRET)

    assert signer.verify(BODY, header, []) is False
    assert signer.verify(BODY, header, "") is False


def test_a_signing_secret_never_appears_in_a_rendered_form():
    redacted = signer.redact_secret(SECRET)

    assert SECRET not in redacted
    assert redacted.startswith(signer.SECRET_PREFIX)
    assert redacted.endswith(SECRET[-4:])
    assert signer.redact_secret("") == "<empty>"
    assert signer.redact_secret("not-one-of-ours") == "<malformed>"


def test_the_outbound_header_set_carries_the_event_id_but_not_the_secret():
    headers = signer.headers_for(
        BODY, [SECRET, OLD_SECRET], event_id="evt_x", event_type="response.completed",
        delivery_id="whd_1",
    )

    rendered = json.dumps(headers)

    assert headers["TechSara-Event-Id"] == "evt_x"
    assert headers["Content-Type"] == "application/json"
    assert SECRET not in rendered and OLD_SECRET not in rendered


def test_a_naive_timestamp_is_refused_because_every_v34_column_is_timestamptz():
    with pytest.raises(ValueError):
        signer.sign(BODY, SECRET, timestamp=datetime(2026, 9, 13, 12, 0, 0))


# ---------------------------------------------------------------------------
# 2. The SSRF defence — the table IS the test
# ---------------------------------------------------------------------------

#: Every address CONTRACT-3 §14 names, plus the ones `core/net`'s 2026-09-03
#: review added. Each is used BOTH as a literal host and as the answer a
#: hostname resolves to, because a guard that only checks literals is defeated
#: by the first CNAME.
FORBIDDEN_ADDRESSES = [
    "127.0.0.1",
    "127.0.0.53",
    "::1",
    "10.0.0.5",
    "10.255.255.254",
    "172.16.4.4",
    "192.168.1.1",
    "169.254.169.254",  # the cloud metadata address, named in §14
    "169.254.1.1",
    "fd00::1",  # unique-local
    "fd00:ec2::254",  # the IPv6 metadata address
    "fe80::1",  # link-local
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "::ffff:10.0.0.5",  # IPv4-mapped private
    "::ffff:8.8.8.8",  # IPv4-mapped PUBLIC — refused anyway, see §14
    "100.64.0.1",  # CGNAT: is_private is False on 3.12, is_global is what catches it
    "0.0.0.0",
    "224.0.0.1",  # multicast
    "2002:0808:0808::1",  # 6to4
]


def _resolving_to(monkeypatch, addresses):
    """Point every hostname at `addresses`, with no real resolver involved."""
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)) for address in addresses]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)


@pytest.mark.parametrize("address", FORBIDDEN_ADDRESSES)
def test_a_webhook_url_whose_host_is_a_forbidden_address_literal_is_refused(address):
    host = f"[{address}]" if ":" in address else address

    with pytest.raises(ssrf.UnsafeWebhookURL):
        ssrf.validate_url(f"https://{host}/hook")


@pytest.mark.parametrize("address", FORBIDDEN_ADDRESSES)
def test_a_hostname_that_resolves_to_a_forbidden_address_is_refused(monkeypatch, address):
    _resolving_to(monkeypatch, [address])

    with pytest.raises(ssrf.UnsafeWebhookURL):
        ssrf.validate_url("https://hooks.customer.example/endpoint")


@pytest.mark.parametrize(
    "address,reason",
    [
        ("::ffff:8.8.8.8", "IPv4-mapped"),
        ("::ffff:127.0.0.1", "IPv4-mapped"),
        ("2002:0808:0808::1", "6to4"),
        ("169.254.169.254", "cloud metadata"),
        ("fd00:ec2::254", "cloud metadata"),
    ],
)
def test_the_forms_contract_14_names_are_refused_by_name_and_not_merely_by_range(address, reason):
    """`app/core/net.py` happens to refuse all of these today through its
    reserved/link-local ranges. CONTRACT-3 §14 names them, so this package
    refuses them in its own right and says which rule did it — a rule that
    exists only as a side effect of another module's range table is one
    nobody knows they are removing."""
    host = f"[{address}]" if ":" in address else address

    with pytest.raises(ssrf.UnsafeWebhookURL) as raised:
        ssrf.validate_url(f"https://{host}/hook")

    assert reason in str(raised.value)


def test_a_hostname_publishing_one_public_and_one_private_record_is_refused(monkeypatch):
    """The textbook rebinding setup. Dialling whichever record the resolver
    hands back first would make the refusal a coin toss."""
    _resolving_to(monkeypatch, ["93.184.216.34", "10.1.2.3"])

    with pytest.raises(ssrf.UnsafeWebhookURL):
        ssrf.validate_url("https://rebind.example/hook")


def test_a_public_endpoint_validates_and_keeps_every_address_it_may_dial(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"])

    target = ssrf.validate_url("https://hooks.customer.example/endpoint")

    assert target.pin_key == "hooks.customer.example"
    assert target.addresses == ("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.customer.example/hook",  # §14: https only
        "ftp://hooks.customer.example/hook",
        "file:///etc/passwd",
        "gopher://hooks.customer.example/",
        "https:///nohost",
        "",
        "   ",
    ],
)
def test_a_url_that_is_not_an_https_url_with_a_host_is_refused(monkeypatch, url):
    _resolving_to(monkeypatch, ["93.184.216.34"])

    with pytest.raises(ssrf.UnsafeWebhookURL):
        ssrf.validate_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@hooks.customer.example/hook",
        "https://user@hooks.customer.example/hook",
    ],
)
def test_a_url_carrying_credentials_is_refused(monkeypatch, url):
    """`safe_fetch` passes URL credentials through as basic auth. A webhook
    must not: the credential would be stored in clear and sent to whatever
    the redirect chain ends at."""
    _resolving_to(monkeypatch, ["93.184.216.34"])

    with pytest.raises(ssrf.UnsafeWebhookURL):
        ssrf.validate_url(url)


def test_a_host_that_does_not_resolve_is_transient_and_not_a_policy_refusal(monkeypatch):
    """The distinction the whole retry ledger hangs on: a resolver blip during
    a deploy must be retried, and an address policy refusal must not."""
    def gaierror(*args, **kwargs):
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", gaierror)

    with pytest.raises(ssrf.WebhookTransportError):
        ssrf.validate_url("https://hooks.customer.example/hook")


# ---- the HTTP half -------------------------------------------------------


def _mock_client(monkeypatch, handler):
    """Swap the pinned transport for `httpx.MockTransport`.

    The pinning itself is `app/core/net.py`'s and is proved there against a
    real loopback TLS handshake (`tests/test_net_ssrf.py`). What these tests
    are about is the policy wrapped around it: which hops are re-validated and
    what is recorded — neither of which needs a socket.
    """
    def factory(_backend):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            timeout=httpx.Timeout(5.0),
        )

    monkeypatch.setattr(ssrf, "_pinned_client", factory)


def test_a_delivery_posts_the_signed_bytes_and_reads_a_bounded_response(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34"])
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["signature"] = request.headers.get(signer.SIGNATURE_HEADER)
        seen["host"] = request.headers.get("host")
        return httpx.Response(200, text="ok")

    _mock_client(monkeypatch, handler)
    headers = signer.headers_for(BODY, [SECRET], event_id="evt_1", event_type="x")

    result = asyncio.run(ssrf.post_json("https://hooks.customer.example/hook", BODY, headers))

    assert result.ok and result.status == 200
    assert seen["body"] == BODY
    # The Host header is the URL's name, not the dialled address — that is
    # what keeps TLS and the pin agreeing (see app/core/net.py).
    assert seen["host"] == "hooks.customer.example"
    assert signer.verify(seen["body"], seen["signature"], SECRET) is True


def test_a_redirect_to_a_private_address_is_refused_at_the_hop(monkeypatch):
    """A public URL that 302s to `https://10.0.0.5/` is the SSRF that gets
    past a check performed only on the first URL."""
    def fake_getaddrinfo(host, *args, **kwargs):
        address = "10.0.0.5" if host == "internal.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", fake_getaddrinfo)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hooks.customer.example":
            return httpx.Response(302, headers={"location": "https://internal.example/hook"})
        return httpx.Response(200, text="reached the private host")  # pragma: no cover

    _mock_client(monkeypatch, handler)

    with pytest.raises(ssrf.UnsafeWebhookURL):
        asyncio.run(ssrf.post_json("https://hooks.customer.example/hook", BODY, {}))


def test_a_redirect_to_the_cloud_metadata_address_is_refused_at_the_hop(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34"])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "hooks.customer.example":
            return httpx.Response(
                307, headers={"location": "https://169.254.169.254/latest/meta-data/"}
            )
        return httpx.Response(200)  # pragma: no cover

    _mock_client(monkeypatch, handler)

    with pytest.raises(ssrf.UnsafeWebhookURL):
        asyncio.run(ssrf.post_json("https://hooks.customer.example/hook", BODY, {}))


def test_a_redirect_chain_longer_than_three_hops_is_refused(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34"])
    hops = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hops["n"] += 1
        return httpx.Response(302, headers={"location": f"https://hop{hops['n']}.example/hook"})

    _mock_client(monkeypatch, handler)

    with pytest.raises(ssrf.WebhookTransportError):
        asyncio.run(ssrf.post_json("https://hooks.customer.example/hook", BODY, {}))

    assert hops["n"] == ssrf.MAX_REDIRECTS + 1


def test_three_redirects_are_followed_and_the_body_survives_every_hop(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34"])
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        hop = len(bodies)
        if hop <= ssrf.MAX_REDIRECTS:
            return httpx.Response(307, headers={"location": f"https://hop{hop}.example/hook"})
        return httpx.Response(200, text="ok")  # pragma: no cover - reached below

    _mock_client(monkeypatch, handler)

    result = asyncio.run(ssrf.post_json("https://hooks.customer.example/hook", BODY, {}))

    assert result.redirects == ssrf.MAX_REDIRECTS
    # A GET on a redirect would deliver nothing while recording success.
    assert bodies == [BODY] * (ssrf.MAX_REDIRECTS + 1)


def test_a_redirect_without_a_location_header_is_a_transport_failure(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34"])
    _mock_client(monkeypatch, lambda request: httpx.Response(302))

    with pytest.raises(ssrf.WebhookTransportError):
        asyncio.run(ssrf.post_json("https://hooks.customer.example/hook", BODY, {}))


def test_a_consumer_that_answers_with_a_huge_body_is_cut_off(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34"])
    _mock_client(
        monkeypatch,
        lambda request: httpx.Response(200, content=b"x" * (ssrf.MAX_RESPONSE_BYTES + 1)),
    )

    with pytest.raises(ssrf.WebhookTransportError):
        asyncio.run(ssrf.post_json("https://hooks.customer.example/hook", BODY, {}))


def test_only_a_short_excerpt_of_an_error_page_is_kept(monkeypatch):
    _resolving_to(monkeypatch, ["93.184.216.34"])
    _mock_client(monkeypatch, lambda request: httpx.Response(500, text="boom " * 500))

    result = asyncio.run(ssrf.post_json("https://hooks.customer.example/hook", BODY, {}))

    assert result.ok is False and result.status == 500
    assert len(result.excerpt) <= ssrf.ERROR_EXCERPT_CHARS


def test_a_delivery_that_outlives_its_deadline_is_a_transport_failure(monkeypatch):
    """One budget for the WHOLE delivery, not per socket operation: a consumer
    dribbling a byte a second must not hold a worker slot for minutes."""
    async def slow(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(ssrf, "_post", slow)

    with pytest.raises(ssrf.WebhookTransportError):
        asyncio.run(
            ssrf.post_json("https://hooks.customer.example/hook", BODY, {}, timeout_seconds=0.05)
        )


# ---------------------------------------------------------------------------
# 3. The retry schedule (pure)
# ---------------------------------------------------------------------------


def test_the_delay_between_attempts_grows_and_then_stops_growing_at_the_cap():
    delays = [
        sender.backoff_seconds(n, jitter=0.0)
        for n in range(1, 15)
    ]

    assert delays[0] == sender.BASE_DELAY_SECONDS
    assert delays[:5] == [10.0, 20.0, 40.0, 80.0, 160.0]
    assert all(later >= earlier for earlier, later in zip(delays, delays[1:]))
    assert max(delays) == sender.CAP_DELAY_SECONDS
    assert delays[-1] == sender.CAP_DELAY_SECONDS


def test_the_jitter_spreads_each_delay_without_ever_collapsing_it_to_zero():
    """A consumer coming back from an outage must not be hit by every queued
    delivery in the same tenth of a second — and a jittered delay must still
    be a delay."""
    source = random.Random(20260913)
    samples = [sender.backoff_seconds(3, rng=source) for _ in range(200)]

    assert min(samples) >= 40.0 * (1 - sender.JITTER_RATIO)
    assert max(samples) <= 40.0 * (1 + sender.JITTER_RATIO)
    assert len(set(samples)) > 100  # actually spread, not a constant
    assert min(samples) >= sender.MIN_DELAY_SECONDS


def test_an_absurd_attempt_count_cannot_overflow_the_doubling():
    """`max_attempts` is a database column somebody can edit."""
    assert sender.backoff_seconds(4000, jitter=0.0) == sender.CAP_DELAY_SECONDS


def test_the_event_id_is_stable_for_one_response_reaching_one_state():
    first = sender.event_id_for("resp_abc", sender.RESPONSE_COMPLETED)

    assert first == sender.event_id_for("resp_abc", sender.RESPONSE_COMPLETED)
    assert first != sender.event_id_for("resp_abc", sender.RESPONSE_FAILED)
    assert first != sender.event_id_for("resp_xyz", sender.RESPONSE_COMPLETED)
    # Opaque: no other project's identifiers are legible from it.
    assert "resp_abc" not in first


def test_the_bytes_that_are_signed_are_the_bytes_that_are_sent():
    payload = {"b": 2, "a": 1, "nested": {"z": [1, 2]}}

    once = sender.payload_bytes(payload)

    assert once == sender.payload_bytes(dict(reversed(list(payload.items()))))
    assert json.loads(once) == payload


# ---------------------------------------------------------------------------
# 4. Delivery against real PostgreSQL
# ---------------------------------------------------------------------------


@pytest.fixture()
def project():
    with db.connection() as con:
        for workspace_id, name in ((WORKSPACE, "Webhooks"), (OTHER_WORKSPACE, "Other")):
            con.execute("INSERT INTO workspaces (id, name) VALUES (%s, %s)", (workspace_id, name))
    return db.create_api_project(WORKSPACE, "Webhook project", "live")


@pytest.fixture()
def endpoint(project):
    return db.create_webhook_endpoint(
        project["id"],
        WORKSPACE,
        "https://hooks.customer.example/endpoint",
        list(sender.SUBSCRIBABLE_EVENTS),
        SECRET,
    )


@pytest.fixture()
def response_row(project):
    row = db.create_api_response(
        project["id"], WORKSPACE, "techsara-35b", "req_1",
        background=True, metadata={"customer_request_id": "abc-123"},
    )
    return db.update_api_response(
        row["id"], project["id"],
        status="completed", input_tokens=37, output_tokens=112,
        output_text="The generated answer nobody outside may read.",
    )


def _delivered(monkeypatch, status=200):
    async def post_json(url, body, headers, **kwargs):
        return ssrf.WebhookResponse(status=status, url=url)

    monkeypatch.setattr(sender.ssrf, "post_json", post_json)


def test_the_payload_carries_no_prompt_or_generated_text_by_default(response_row, endpoint):
    payload = sender.build_payload(response_row, sender.RESPONSE_COMPLETED)

    rendered = sender.payload_bytes(payload).decode()

    assert "output" not in payload["data"]["response"]
    assert "The generated answer" not in rendered
    assert "fingerprint" not in rendered and "req_1" not in rendered
    # What it DOES carry: the id, the state, and the caller's own metadata.
    assert payload["data"]["response"]["id"] == response_row["id"]
    assert payload["data"]["response"]["status"] == "completed"
    assert payload["data"]["response"]["metadata"] == {"customer_request_id": "abc-123"}
    assert payload["data"]["response"]["usage"] == {
        "input_tokens": 37, "output_tokens": 112, "total_tokens": 149
    }


def test_the_generated_text_travels_only_when_the_project_opted_in(response_row):
    payload = sender.build_payload(response_row, sender.RESPONSE_COMPLETED, include_output=True)

    assert payload["data"]["response"]["output"] == [
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "The generated answer nobody outside may read."}
            ],
        }
    ]


def test_usage_is_null_rather_than_zero_when_the_engine_reported_nothing(project):
    row = db.create_api_response(project["id"], WORKSPACE, "techsara-35b", "req_2")

    payload = sender.build_payload(row, sender.RESPONSE_COMPLETED)

    assert payload["data"]["response"]["usage"] is None


def test_an_event_is_queued_to_every_subscribed_endpoint_and_to_no_other(project, response_row):
    subscribed = db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://a.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )
    other_event = db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://b.example/hook",
        [sender.RESPONSE_FAILED], SECRET,
    )
    disabled = db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://c.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )
    db.update_webhook_endpoint(disabled["id"], WORKSPACE, status="disabled")

    queued = asyncio.run(
        sender.emit_response_event(
            response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE
        )
    )

    assert len(queued) == 1
    rows = db.due_webhook_deliveries(limit=50)
    assert [row["endpoint_id"] for row in rows] == [subscribed["id"]]
    assert other_event["id"] not in {row["endpoint_id"] for row in rows}


def test_queueing_the_same_event_twice_delivers_it_once(project, response_row, endpoint):
    first = asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )
    second = asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )

    assert len(first) == 1 and second == []


def test_a_successful_delivery_is_recorded_and_clears_the_endpoints_failure_count(
    monkeypatch, project, endpoint, response_row
):
    _delivered(monkeypatch, 200)
    asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )

    counts = asyncio.run(worker.run_once())

    assert counts == {"due": 1, "delivered": 1, "retrying": 0, "failed": 0, "dropped": 0}
    delivery = db.due_webhook_deliveries(limit=50)
    assert delivery == []  # nothing is due any more
    refreshed = db.list_webhook_endpoints(project["id"], WORKSPACE)[0]
    assert refreshed["last_delivery_status"] == "delivered"
    assert refreshed["consecutive_failures"] == 0


def test_a_delivery_is_signed_with_the_endpoints_secret_and_verifies_end_to_end(
    monkeypatch, endpoint, response_row
):
    captured = {}

    async def post_json(url, body, headers, **kwargs):
        captured["body"] = body
        captured["headers"] = headers
        return ssrf.WebhookResponse(status=200, url=url)

    monkeypatch.setattr(sender.ssrf, "post_json", post_json)
    asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )

    asyncio.run(worker.run_once())

    assert signer.verify(
        captured["body"], captured["headers"][signer.SIGNATURE_HEADER], SECRET
    ) is True
    assert SECRET not in json.dumps(captured["headers"])


def test_a_failing_delivery_retries_with_a_growing_delay_and_stops_at_the_attempt_cap(
    monkeypatch, project, endpoint, response_row
):
    """The bound CONTRACT-3 §14 demands: never infinite. The attempt counter
    lives in the row, so this is asserted against the database rather than
    against a loop variable."""
    _delivered(monkeypatch, 500)
    asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )
    delivery_id = db.due_webhook_deliveries(limit=1)[0]["id"]

    scheduled = []
    moment = datetime.now(timezone.utc)
    for attempt in range(sender.MAX_ATTEMPTS + 2):
        # Each sweep is run from far enough in the future that the previous
        # backoff has elapsed — which is also an assertion that the sweep only
        # picks up what is actually due.
        moment = moment + timedelta(hours=1)
        counts = asyncio.run(worker.run_once(now=moment))
        with db.connection() as con:
            row = con.execute(
                "SELECT attempt, status, next_attempt_at FROM api_webhook_deliveries WHERE id = %s",
                (delivery_id,),
            ).fetchone()
        if counts["due"] == 0:
            break
        if row["next_attempt_at"] is not None and row["status"] == "pending":
            scheduled.append((row["attempt"], row["next_attempt_at"] - moment))

    assert row["attempt"] == sender.MAX_ATTEMPTS
    assert row["status"] == "failed"
    # Five scheduled retries before the sixth attempt exhausts the budget, and
    # each waits longer than the one before it.
    assert [attempt for attempt, _delay in scheduled] == [1, 2, 3, 4, 5]
    delays = [delay for _attempt, delay in scheduled]
    assert all(later > earlier for earlier, later in zip(delays, delays[1:]))
    # And it is gone from the sweep for good, not merely not-due-yet.
    assert db.due_webhook_deliveries(limit=50, now=moment + timedelta(days=365)) == []


def test_an_endpoint_that_points_somewhere_forbidden_is_dropped_on_the_first_attempt(
    monkeypatch, project, response_row
):
    """A policy refusal is permanent. Retrying it six times would make the
    delivery loop a slow scan of the private network."""
    db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://internal.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )

    async def refuse(url, body, headers, **kwargs):
        raise ssrf.UnsafeWebhookURL("the endpoint resolves to an address that is not routable")

    monkeypatch.setattr(sender.ssrf, "post_json", refuse)
    asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )

    counts = asyncio.run(worker.run_once())

    assert counts["dropped"] == 1
    with db.connection() as con:
        row = con.execute("SELECT attempt, status FROM api_webhook_deliveries").fetchone()
    assert (row["attempt"], row["status"]) == (1, "dropped")
    assert db.due_webhook_deliveries(limit=50, now=datetime.now(timezone.utc) + timedelta(days=7)) == []


def test_a_transient_network_failure_is_retried_rather_than_dropped(
    monkeypatch, endpoint, response_row
):
    async def blip(url, body, headers, **kwargs):
        raise ssrf.WebhookTransportError("could not resolve the endpoint host")

    monkeypatch.setattr(sender.ssrf, "post_json", blip)
    asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )

    counts = asyncio.run(worker.run_once())

    assert counts["retrying"] == 1
    with db.connection() as con:
        row = con.execute("SELECT attempt, status, next_attempt_at FROM api_webhook_deliveries").fetchone()
    assert (row["attempt"], row["status"]) == (1, "pending")
    assert row["next_attempt_at"] is not None


def test_a_disabled_endpoints_queue_is_paused_and_resumes_rather_than_being_lost(
    monkeypatch, project, endpoint, response_row
):
    _delivered(monkeypatch, 200)
    asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )
    db.update_webhook_endpoint(endpoint["id"], WORKSPACE, status="disabled")

    paused = asyncio.run(worker.run_once())
    db.update_webhook_endpoint(endpoint["id"], WORKSPACE, status="active")
    resumed = asyncio.run(worker.run_once())

    assert paused["due"] == 0
    assert resumed["delivered"] == 1


def test_an_endpoint_with_no_signing_secret_is_dropped_rather_than_sent_unsigned(
    monkeypatch, project, response_row
):
    """An unsigned delivery is indistinguishable from a forged one."""
    endpoint = db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://hooks.customer.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )
    db.update_webhook_endpoint(endpoint["id"], WORKSPACE, secret="")

    sent = []

    async def post_json(url, body, headers, **kwargs):  # pragma: no cover - must not run
        sent.append(url)
        return ssrf.WebhookResponse(status=200, url=url)

    monkeypatch.setattr(sender.ssrf, "post_json", post_json)
    asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )

    counts = asyncio.run(worker.run_once())

    assert counts["dropped"] == 1 and sent == []


def test_one_broken_delivery_does_not_stop_the_others_in_the_same_sweep(
    monkeypatch, project, response_row
):
    good = db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://good.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )
    db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://bad.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )

    async def post_json(url, body, headers, **kwargs):
        if "bad.example" in url:
            raise RuntimeError("something nobody anticipated")
        return ssrf.WebhookResponse(status=200, url=url)

    monkeypatch.setattr(sender.ssrf, "post_json", post_json)
    asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )

    counts = asyncio.run(worker.run_once())

    assert counts["due"] == 2 and counts["delivered"] == 1 and counts["retrying"] == 1
    with db.connection() as con:
        delivered = con.execute(
            "SELECT status FROM api_webhook_deliveries WHERE endpoint_id = %s", (good["id"],)
        ).fetchone()
    assert delivered["status"] == "delivered"


def test_a_test_delivery_goes_through_the_same_signing_and_retry_path(
    monkeypatch, endpoint
):
    _delivered(monkeypatch, 200)

    queued = asyncio.run(sender.enqueue_test_delivery(endpoint))
    counts = asyncio.run(worker.run_once())

    assert queued is not None and queued["event_type"] == sender.EVENT_TEST
    assert counts["delivered"] == 1


def test_the_sweep_is_empty_and_cheap_when_nothing_is_due():
    assert asyncio.run(worker.run_once()) == {
        "due": 0, "delivered": 0, "retrying": 0, "failed": 0, "dropped": 0
    }


# ---------------------------------------------------------------------------
# 5. The 2026-09-13 adversarial review: fairness, claims, DNS, the real client
# ---------------------------------------------------------------------------


def _second_project():
    return db.create_api_project(WORKSPACE, "Another tenant's project", "live")


def _queue(endpoint_row, count, *, due_at):
    ids = []
    for index in range(count):
        row = db.enqueue_webhook_delivery(
            endpoint_row["id"], endpoint_row["project_id"], sender.RESPONSE_COMPLETED,
            f"evt_{endpoint_row['id']}_{index}", {"n": index},
            next_attempt_at=due_at,
        )
        ids.append(row["id"])
    return ids


def test_one_projects_backlog_to_a_dead_endpoint_cannot_crowd_out_another_projects_delivery(
    monkeypatch, project
):
    """The review's cross-tenant starvation: thirty OLDER deliveries of one
    project to a dead endpoint used to fill every batch of the global FIFO, so
    a second project's single delivery was never picked up."""
    dead = db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://dead.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )
    other = _second_project()
    healthy = db.create_webhook_endpoint(
        other["id"], WORKSPACE, "https://healthy.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )
    now = datetime.now(timezone.utc)
    _queue(dead, 30, due_at=now - timedelta(hours=1))
    [healthy_delivery] = _queue(healthy, 1, due_at=now - timedelta(seconds=1))
    sent = []

    async def post_json(url, body, headers, **kwargs):
        sent.append(url)
        if "dead.example" in url:
            raise ssrf.WebhookTransportError("connection refused")
        return ssrf.WebhookResponse(status=200, url=url)

    monkeypatch.setattr(sender.ssrf, "post_json", post_json)

    counts = asyncio.run(worker.run_once(limit=worker.BATCH_SIZE))

    assert "https://healthy.example/hook" in sent
    with db.connection() as con:
        row = con.execute(
            "SELECT status FROM api_webhook_deliveries WHERE id = %s", (healthy_delivery,)
        ).fetchone()
    assert row["status"] == "delivered"
    # And the dead project got no more than its per-sweep share.
    assert sum("dead.example" in url for url in sent) <= queue.PER_PROJECT_PER_SWEEP
    assert counts["delivered"] == 1


def test_two_overlapping_sweeps_never_send_the_same_delivery_twice(monkeypatch, endpoint):
    """A blue/green overlap: two sweeps running at once over the same queue.
    Without a claim both select the same pending rows, both send, and both
    increment `attempt`. Run for real, concurrently, against PostgreSQL."""
    now = datetime.now(timezone.utc)
    ids = _queue(endpoint, 12, due_at=now - timedelta(seconds=5))
    sends = {}

    async def post_json(url, body, headers, **kwargs):
        delivery_id = headers["TechSara-Delivery-Id"]
        sends[delivery_id] = sends.get(delivery_id, 0) + 1
        await asyncio.sleep(0.3)  # the send window the race lives in
        return ssrf.WebhookResponse(status=200, url=url)

    monkeypatch.setattr(sender.ssrf, "post_json", post_json)
    monkeypatch.setattr(queue, "PER_PROJECT_PER_SWEEP", 50)

    async def both():
        return await asyncio.gather(
            worker.run_once(limit=50), worker.run_once(limit=50), worker.run_once(limit=50)
        )

    # `per_project` is read as a default argument, so pin it on the call path.
    real_claim = queue.claim_due_deliveries
    monkeypatch.setattr(
        queue, "claim_due_deliveries",
        lambda limit=20, **kw: real_claim(limit, per_project=50, **kw),
    )
    results = asyncio.run(both())

    assert sorted(sends) == sorted(ids)
    assert set(sends.values()) == {1}
    assert sum(result["delivered"] for result in results) == len(ids)
    with db.connection() as con:
        attempts = {
            row["attempt"]
            for row in con.execute(
                "SELECT attempt FROM api_webhook_deliveries WHERE endpoint_id = %s",
                (endpoint["id"],),
            ).fetchall()
        }
    assert attempts == {1}


def test_concurrent_claims_from_many_threads_are_disjoint(endpoint):
    """The claim itself, from eight threads on eight pooled connections at the
    same instant: every due row is claimed by exactly one of them."""
    import threading

    now = datetime.now(timezone.utc)
    ids = _queue(endpoint, 40, due_at=now - timedelta(seconds=5))
    barrier = threading.Barrier(8)
    claimed = []
    lock = threading.Lock()

    def claim():
        barrier.wait()
        rows = queue.claim_due_deliveries(40, per_project=40)
        with lock:
            claimed.extend(row["id"] for row in rows)

    threads = [threading.Thread(target=claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(claimed) == sorted(ids)
    assert len(claimed) == len(set(claimed))


def test_a_claimed_delivery_whose_sender_died_comes_back_when_its_lease_lapses(endpoint):
    now = datetime.now(timezone.utc)
    [delivery_id] = _queue(endpoint, 1, due_at=now - timedelta(seconds=5))

    first = queue.claim_due_deliveries(10, now=now)
    again = queue.claim_due_deliveries(10, now=now + timedelta(seconds=1))
    after_lease = queue.claim_due_deliveries(
        10, now=now + timedelta(seconds=queue.CLAIM_LEASE_SECONDS + 1)
    )

    assert [row["id"] for row in first] == [delivery_id]
    assert again == []
    assert [row["id"] for row in after_lease] == [delivery_id]
    assert after_lease[0]["attempt"] == 0  # nothing was recorded, nothing counted


def test_the_sweep_never_has_more_deliveries_in_flight_than_its_gate(monkeypatch, project):
    endpoints = [
        db.create_webhook_endpoint(
            project["id"], WORKSPACE, f"https://h{index}.example/hook",
            [sender.RESPONSE_COMPLETED], SECRET,
        )
        for index in range(3)
    ]
    now = datetime.now(timezone.utc)
    for row in endpoints:
        _queue(row, 6, due_at=now - timedelta(seconds=5))
    state = {"now": 0, "peak": 0}

    async def post_json(url, body, headers, **kwargs):
        state["now"] += 1
        state["peak"] = max(state["peak"], state["now"])
        await asyncio.sleep(0.05)
        state["now"] -= 1
        return ssrf.WebhookResponse(status=200, url=url)

    monkeypatch.setattr(sender.ssrf, "post_json", post_json)
    real_claim = queue.claim_due_deliveries
    monkeypatch.setattr(
        queue, "claim_due_deliveries",
        lambda limit=20, **kw: real_claim(limit, per_project=50, **kw),
    )

    counts = asyncio.run(worker.run_once(limit=18))

    assert counts["delivered"] == 18
    assert state["peak"] == worker.MAX_CONCURRENT_DELIVERIES


def test_an_event_is_never_queued_to_another_workspaces_endpoint(project, response_row):
    """The payload names project A; an endpoint in workspace B, and a caller
    naming workspace B for project A's response, must both queue nothing."""
    foreign_project = db.create_api_project(OTHER_WORKSPACE, "Foreign", "live")
    db.create_webhook_endpoint(
        foreign_project["id"], OTHER_WORKSPACE, "https://foreign.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )
    db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://own.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )

    wrong_workspace = asyncio.run(
        sender.emit_response_event(
            response_row, sender.RESPONSE_COMPLETED, workspace_id=OTHER_WORKSPACE
        )
    )
    right_workspace = asyncio.run(
        sender.emit_response_event(
            response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE
        )
    )

    assert wrong_workspace == []
    assert len(right_workspace) == 1
    with db.connection() as con:
        urls = [
            row["url"]
            for row in con.execute(
                "SELECT e.url FROM api_webhook_deliveries d "
                "JOIN api_webhook_endpoints e ON e.id = d.endpoint_id"
            ).fetchall()
        ]
    assert urls == ["https://own.example/hook"]


def test_queueing_an_event_wakes_the_sweep_instead_of_waiting_for_its_poll(
    project, response_row, endpoint
):
    async def scenario():
        worker._wake = asyncio.Event()
        await sender.emit_response_event(
            response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE
        )
        woken_by_event = worker._wake.is_set()
        worker._wake.clear()
        await sender.enqueue_test_delivery(endpoint)
        return woken_by_event, worker._wake.is_set()

    try:
        assert asyncio.run(scenario()) == (True, True)
    finally:
        worker._wake = None


def test_a_lapsed_rotation_stops_signing_with_the_old_secret_on_a_real_queue_row(
    project, response_row
):
    """`db._row` renders timestamptz as an ISO STRING, and `active_secrets`
    used to compare only datetimes — so on a row read from PostgreSQL a lapsed
    overlap looked open for ever and the leaked old secret kept signing."""
    row = db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://rotated.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )
    db.update_webhook_endpoint(
        row["id"], WORKSPACE,
        previous_secret=OLD_SECRET,
        previous_secret_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )
    asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )

    [claimed] = queue.claim_due_deliveries(10)

    assert isinstance(claimed["endpoint"]["previous_secret_expires_at"], str)
    assert signer.active_secrets(claimed["endpoint"]) == (SECRET,)


def test_an_open_rotation_read_from_the_database_still_signs_with_both_secrets(
    project, response_row
):
    row = db.create_webhook_endpoint(
        project["id"], WORKSPACE, "https://rotating.example/hook",
        [sender.RESPONSE_COMPLETED], SECRET,
    )
    db.update_webhook_endpoint(
        row["id"], WORKSPACE,
        previous_secret=OLD_SECRET,
        previous_secret_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    asyncio.run(
        sender.emit_response_event(response_row, sender.RESPONSE_COMPLETED, workspace_id=WORKSPACE)
    )

    [claimed] = queue.claim_due_deliveries(10)

    assert signer.active_secrets(claimed["endpoint"]) == (SECRET, OLD_SECRET)


def test_the_delivery_history_is_scoped_to_the_endpoint_project_and_workspace(project, endpoint):
    now = datetime.now(timezone.utc)
    _queue(endpoint, 3, due_at=now)

    own = queue.list_deliveries(endpoint["id"], project["id"], WORKSPACE)
    foreign = queue.list_deliveries(endpoint["id"], project["id"], OTHER_WORKSPACE)
    paged = queue.list_deliveries(endpoint["id"], project["id"], WORKSPACE, limit=2, offset=2)
    clamped = queue.list_deliveries(
        endpoint["id"], project["id"], WORKSPACE, limit=10**9, offset=10**9
    )

    assert len(own) == 3 and foreign == [] and len(paged) == 1 and clamped == []
    assert all("payload" not in row for row in own)


# ---- the signer --------------------------------------------------------------


def test_verify_compares_every_digest_against_every_secret_even_after_a_match(monkeypatch):
    """Constant time in the property that matters: the number of compares does
    not depend on WHICH secret or digest matched, so timing cannot say."""
    calls = []
    real = signer.hmac.compare_digest

    def counting(a, b):
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(signer.hmac, "compare_digest", counting)
    signed_at = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    header = signer.signature_header(BODY, [SECRET, OLD_SECRET], timestamp=signed_at)

    assert signer.verify(BODY, header, [SECRET, OLD_SECRET], now=signed_at) is True
    assert len(calls) == 2 * 2


def test_a_header_with_more_digests_than_any_delivery_carries_is_refused_cheaply(monkeypatch):
    calls = []
    monkeypatch.setattr(signer.hmac, "compare_digest", lambda a, b: calls.append(1) or False)
    signed_at = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    genuine = signer.sign(BODY, SECRET, timestamp=signed_at)
    hostile = genuine + "".join(f",v1={index:064x}" for index in range(10_000))

    assert signer.verify(BODY, hostile, SECRET, now=signed_at) is False
    assert calls == []
    with pytest.raises(signer.SignatureFormatError):
        signer.parse_header(hostile)


# ---- redaction ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url, shown",
    [
        ("https://hooks.example/cb?token=s3cr3t", "https://hooks.example/cb?<redacted>"),
        ("https://hooks.example/cb?s3cr3t#frag", "https://hooks.example/cb?<redacted>"),
        ("https://user:pw@hooks.example:8443/cb", "https://hooks.example:8443/cb"),
        ("https://[2001:db8::1]/cb?k=v", "https://[2001:db8::1]/cb?<redacted>"),
        ("https://hooks.example/plain/path", "https://hooks.example/plain/path"),
        ("https://hooks.example:99999/cb?t=x", "<unparseable url>"),
    ],
)
def test_a_webhook_url_is_displayed_without_its_query_string_or_credentials(url, shown):
    assert ssrf.redact_url(url) == shown


def test_a_transport_error_quoting_the_url_is_stored_without_the_token(monkeypatch, endpoint):
    db.update_webhook_endpoint(
        endpoint["id"], WORKSPACE, url="https://hooks.customer.example/cb?token=s3cr3t-value"
    )
    now = datetime.now(timezone.utc)
    [delivery_id] = _queue(endpoint, 1, due_at=now - timedelta(seconds=1))

    async def post_json(url, body, headers, **kwargs):
        raise ssrf.WebhookTransportError(f"delivery failed: 502 from {url}")

    monkeypatch.setattr(sender.ssrf, "post_json", post_json)
    asyncio.run(worker.run_once())

    with db.connection() as con:
        stored = con.execute(
            "SELECT error FROM api_webhook_deliveries WHERE id = %s", (delivery_id,)
        ).fetchone()["error"]
    assert "s3cr3t-value" not in stored
    assert "https://hooks.customer.example/cb?<redacted>" in stored


# ---- DNS resolution is bounded and off the shared executor ---------------------


def test_a_nameserver_that_never_answers_cannot_hold_the_shared_executor(monkeypatch):
    """The review's thread-exhaustion hole: `getaddrinfo` has no timeout and
    ran on the loop's DEFAULT executor, which health checks and uploads share.
    Twelve lookups against a blackholing resolver must each fail within the
    DNS deadline, run only on the webhook pool, and leave the default executor
    free."""
    import threading

    released = threading.Event()
    threads_used = set()

    def blackhole(host, *args, **kwargs):
        threads_used.add(threading.current_thread().name)
        released.wait(20)
        raise socket.gaierror("released by the test")

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", blackhole)

    async def scenario():
        loop = asyncio.get_running_loop()
        started = loop.time()
        lookups = [
            ssrf.validate_url_async(f"https://blackhole{index}.example/hook", dns_timeout_seconds=0.3)
            for index in range(12)
        ]
        outcomes = await asyncio.wait_for(
            asyncio.gather(*lookups, return_exceptions=True), timeout=5
        )
        elapsed = loop.time() - started
        # The default executor still answers at once.
        probe_started = loop.time()
        assert await asyncio.wait_for(loop.run_in_executor(None, lambda: 42), timeout=1) == 42
        return outcomes, elapsed, loop.time() - probe_started

    try:
        outcomes, elapsed, probe = asyncio.run(scenario())
    finally:
        released.set()

    assert all(isinstance(outcome, ssrf.WebhookTransportError) for outcome in outcomes)
    assert elapsed < 2.0
    assert probe < 0.5
    assert threads_used and all(name.startswith("webhook-dns") for name in threads_used)
    assert len(threads_used) <= ssrf.DNS_RESOLVER_THREADS


# ---- the REAL pinned, no-redirect client (no MockTransport) ----------------------


def test_the_delivery_client_is_pinned_does_not_follow_redirects_and_ignores_proxies():
    async def build():
        client = ssrf._pinned_client(ssrf._new_backend())
        try:
            return (
                type(client._transport).__name__,
                isinstance(client._transport, ssrf.net._PinnedTransport),
                client.follow_redirects,
                client._trust_env,
            )
        finally:
            await client.aclose()

    name, pinned, follows, trusts_env = asyncio.run(build())

    assert pinned, name
    assert follows is False
    assert trusts_env is False


@pytest.fixture
def loopback_cert(tmp_path):
    import shutil
    import subprocess

    if shutil.which("openssl") is None:
        pytest.skip("openssl binary not available")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=hooks.pinned.test",
            "-addext", "subjectAltName=DNS:hooks.pinned.test,DNS:internal.pinned.test",
        ],
        check=True,
        capture_output=True,
    )
    return str(key), str(cert)


def _serve_tls(monkeypatch, loopback_cert, respond, *, resolver):
    """A real TLS listener on 127.0.0.1 and a real `ssrf.post_json` against it.

    Nothing between them is mocked: `_pinned_client`, `_PinnedTransport`,
    `_PinnedBackend`, httpcore and OpenSSL all run. Only DNS (so a made-up name
    can point at loopback) and the loopback half of the address policy (so the
    listener is dialable) are replaced."""
    import ssl

    key, cert = loopback_cert
    monkeypatch.setenv("SSL_CERT_FILE", cert)
    seen = {"connections": 0, "requests": [], "sni": []}

    async def main():
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)
        server_ctx.sni_callback = lambda sock, name, ctx: seen["sni"].append(name)

        async def handler(reader, writer):
            seen["connections"] += 1
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                body = await reader.readexactly(length) if length else b""
                seen["requests"].append((head, body))
                writer.write(respond(port))
                await writer.drain()
            except Exception:  # noqa: BLE001 — a refused handshake ends here
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(ssrf.socket, "getaddrinfo", resolver)
        real_check = ssrf._check_address

        def allow_only_the_listener(address):
            if address == "127.0.0.1":
                return None
            return real_check(address)

        monkeypatch.setattr(ssrf, "_check_address", allow_only_the_listener)
        body = BODY
        headers = signer.headers_for(body, [SECRET], event_id="evt_real", event_type="x")
        try:
            return await ssrf.post_json(
                f"https://hooks.pinned.test:{port}/hook", body, headers, timeout_seconds=8
            )
        except Exception as exc:  # noqa: BLE001 — the test inspects it
            return exc
        finally:
            server.close()

    return asyncio.run(main()), seen


def test_a_real_delivery_dials_the_address_checked_once_and_ignores_a_rebound_answer(
    monkeypatch, loopback_cert
):
    """DNS rebinding against the real client: the first lookup answers the
    listener, every later one a private address. The delivery must succeed on
    the FIRST answer with exactly one lookup — an unpinned transport would ask
    again at connect time and be sent to 10.0.0.9."""
    lookups = []

    def rebinding(host, *args, **kwargs):
        lookups.append(host)
        address = "127.0.0.1" if len(lookups) == 1 else "10.0.0.9"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    def ok(port):
        return b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"

    result, seen = _serve_tls(monkeypatch, loopback_cert, ok, resolver=rebinding)

    assert isinstance(result, ssrf.WebhookResponse), result
    assert result.status == 200
    assert lookups == ["hooks.pinned.test"]
    assert seen["sni"] == ["hooks.pinned.test"]
    head, body = seen["requests"][0]
    assert body == BODY
    assert b"host: hooks.pinned.test" in head.lower()


def test_a_real_redirect_to_a_private_address_is_refused_and_never_followed_by_the_client(
    monkeypatch, loopback_cert
):
    """The server answers 302 to a name that resolves privately. The client
    must NOT follow it on its own (follow_redirects=False): the hop is
    re-validated by hand, refused as a policy violation, and the listener sees
    exactly one connection."""
    lookups = []

    def resolver(host, *args, **kwargs):
        lookups.append(host)
        address = "127.0.0.1" if host == "hooks.pinned.test" else "10.0.0.9"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    def redirect(port):
        return (
            f"HTTP/1.1 302 Found\r\nLocation: https://internal.pinned.test:{port}/steal\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n"
        ).encode()

    result, seen = _serve_tls(monkeypatch, loopback_cert, redirect, resolver=resolver)

    assert isinstance(result, ssrf.UnsafeWebhookURL), result
    assert lookups == ["hooks.pinned.test", "internal.pinned.test"]
    assert seen["connections"] == 1


@pytest.mark.parametrize(
    "url, secret",
    [
        # Slack incoming webhooks carry the credential in the PATH, not a query.
        ("https://hooks.slack.com/services/T0000/B0000/PATHSECRETxyz123", "PATHSECRETxyz123"),
        ("https://hooks.slack.com/services/T0EXMPL/B0EXMPL/abcdEFGHijklMNOPqrstUVWX",
         "abcdEFGHijklMNOPqrstUVWX"),
        # Discord: /api/webhooks/<id>/<token>.
        ("https://discord.com/api/webhooks/123456789012345678/Zx9-token_value_that_is_long",
         "Zx9-token_value_that_is_long"),
        ("https://discord.com/api/webhooks/123456789012345678/Zx9-token_value_that_is_long",
         "123456789012345678"),
        # Matrix parameters ride inside a path segment.
        ("https://h.example.com/cb;token=abc", "token=abc"),
        ("https://h.example.com/cb;jsessionid=0123456789abcdef", "0123456789abcdef"),
        # A lowercase-only random token is still random.
        ("https://h.example.com/hook/qwertyuiopasdfghjklzxcvbnmqwerty", "qwertyuiopasdfghjklzxcvbnmqwerty"),
    ],
)
def test_a_credential_carried_in_a_webhook_url_path_is_never_displayed(url, secret):
    """Wave-3 re-verify: redact_url dropped only the query, userinfo and
    fragment, so a Slack or Discord URL — whose secret IS a path segment —
    appeared in clear in listings, the audit log and delivery errors."""
    shown = ssrf.redact_url(url)
    assert secret not in shown, shown
    assert shown.startswith(url.split("/")[0] + "//" + url.split("/")[2])
    assert secret not in ssrf.redact_urls_in_text(f"502 from {url} after 3s")


@pytest.mark.parametrize(
    "url, shown",
    [
        ("https://hooks.slack.com/services/T0000/B0000/PATHSECRETxyz123",
         "https://hooks.slack.com/services/T0000/B0000/<redacted>"),
        ("https://discord.com/api/webhooks/123456789012345678/tok",
         "https://discord.com/api/webhooks/<redacted>/tok"),
        ("https://h.example.com/cb;token=abc", "https://h.example.com/cb;<redacted>"),
        ("https://h.example.com/v2/webhook-receiver/", "https://h.example.com/v2/webhook-receiver/"),
    ],
)
def test_a_webhook_url_keeps_its_recognisable_path_words_and_loses_the_rest(url, shown):
    assert ssrf.redact_url(url) == shown
