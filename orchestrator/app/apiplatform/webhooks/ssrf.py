"""The outbound-delivery SSRF defence (CONTRACT-3 §14).

A webhook URL is the one thing on this platform that a customer supplies and
that this server then connects to. That makes it the classic server-side
request forgery lever: a project whose endpoint is `https://whatever/` can
point it at `http://169.254.169.254/latest/meta-data/`, at the vLLM head on
the RoCE address, at pgAdmin, or at any of the internal services CONTRACT-3
§1 says must never be reachable from any surface — and the request would go
out with this box's network position and nobody's credential to stop it.

WHAT THIS MODULE REUSES RATHER THAN REBUILDS. `app/core/net.py` already owns
the address policy for inbound-fetch SSRF (the 2026-09-03 hardening) and,
more importantly, already owns the DNS-PINNED TRANSPORT that makes "the
address we checked" and "the address we dial" the same address. Both are used
here as-is:

* `net.resolve_public_ips(<literal>)` is the single source of truth for
  whether one address is allowed. Calling it with an IP LITERAL (never a
  hostname) makes it a pure policy check with no resolver call of its own,
  which is what lets this module tell "the name does not resolve" (a transient
  failure worth a retry) apart from "the name resolves somewhere forbidden"
  (a permanent refusal that must never be retried). `core/net` raises the same
  exception for both, and a webhook that retried a policy refusal six times
  would be a port scanner on a timer.
* `net._PinnedBackend` / `net._PinnedTransport` dial the validated IP while
  httpx keeps deriving SNI, the certificate hostname check and the `Host`
  header from the URL. That closes the rebinding TOCTOU: between our check
  and our connect there is no second DNS lookup for an attacker's 1-second
  TTL record to win.

WHAT THIS MODULE ADDS ON TOP, AND WHY

1. HTTPS ONLY, no exceptions. `core/net` allows http because a public web
   page may only exist there; a webhook carries a signed statement about a
   customer's traffic and must not be readable by the first hop.
2. NO CREDENTIALS IN THE URL. `https://user:pw@host/` is refused outright
   rather than passed through as basic auth (which is what `safe_fetch`
   does). A stored credential would be sent to whatever the redirect chain
   ends at, and it would sit in the endpoint row in clear.
3. IPv4-MAPPED IPv6, 6to4 AND THE METADATA ADDRESS ARE REFUSED BY NAME.
   Measured 2026-09-13: `core/net` already refuses all three today —
   `::ffff:8.8.8.8` passes `is_global` but is caught by `is_reserved`, and
   169.254.169.254 is link-local. So this is belt and braces, and it is worth
   the lines for two reasons. CONTRACT-3 §14 names these forms explicitly, and
   a rule that exists only as a side effect of another module's range table is
   one nobody knows they are removing; and the refusal reason a delivery
   history shows should say which rule refused it, not "not publicly
   routable". Each is pinned by a test that asserts the REASON, so the
   redundancy cannot rot into a lie.
4. POST, WITH A BODY, AND A WHOLE-DELIVERY DEADLINE. `safe_fetch` is a GET
   helper. This is a POST of exactly the bytes that were signed, under a
   single 10 s budget that covers DNS, connect, TLS, the write, every
   redirect hop and the bounded read — not 10 s per phase, which a slow
   consumer could stretch into a minute of a worker slot.
5. THE RESPONSE IS READ AND DISCARDED TO A SMALL BOUND. What a consumer
   returns is not data we have any use for; we keep only the status code and
   a short excerpt for the delivery history, so a consumer answering with a
   gigabyte cannot cost us memory.

WHAT IS DELIBERATELY NOT GUARANTEED. A public address that is itself a
gateway to something internal is indistinguishable from any other public
host — the policy is about addresses, exactly as `core/net` documents. And a
redirect to another origin IS followed (up to three hops, each re-validated),
which means the signature header travels there too. That is accepted: the
`v1` digest is a MAC, not the secret, the redirect was configured by the
endpoint's own owner, and refusing redirects outright would break consumers
who sit behind a path-rewriting proxy.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import logging
import re
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx

from ...core import net

log = logging.getLogger(__name__)

#: CONTRACT-3 §14. Three hops, each fully re-validated before a byte is sent.
MAX_REDIRECTS = 3

#: CONTRACT-3 §14: "10 s timeout". One budget for the WHOLE delivery.
TIMEOUT_SECONDS = 10.0

#: Enough to keep a useful excerpt of an error page in the delivery history,
#: small enough that a hostile consumer cannot make us hold anything.
MAX_RESPONSE_BYTES = 64 * 1024

#: How much of the response body is kept for the history row. A consumer's
#: 500 page usually says what is wrong in its first line.
ERROR_EXCERPT_CHARS = 200

#: Named for the reader, though every one of them is already refused by the
#: address policy below. The 2026-09-03 review of `core/net` made the point
#: that a rule nobody can see is a rule that gets removed in a refactor.
CLOUD_METADATA_ADDRESSES = frozenset({"169.254.169.254", "fd00:ec2::254"})

#: How long one endpoint's name may take to resolve. `socket.getaddrinfo` has
#: no timeout of its own; a nameserver that blackholes the query leaves the
#: calling thread blocked for the libc retry schedule (tens of seconds).
DNS_TIMEOUT_SECONDS = 5.0

#: Threads reserved for webhook name resolution, and ONLY for that. Before
#: 2026-09-13 the lookup ran through `asyncio.to_thread`, i.e. the loop's
#: DEFAULT executor (min(32, cpu+4) threads) that `health.py`, `uploads.py`
#: and `web_index.py` also use. The whole-delivery `wait_for` abandoned a
#: stuck lookup but not its thread, and freed the sweep slot, so endpoint
#: hostnames served by a blackholing nameserver could pile blocked threads into
#: that shared pool until health checks and uploads queued behind them (the
#: 2026-09-13 adversarial review). A private pool of four means the worst a
#: hostile endpoint owner can exhaust is webhook resolution itself.
DNS_RESOLVER_THREADS = 4

_resolver_lock = threading.Lock()
_resolver_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None


def _resolver_executor() -> concurrent.futures.ThreadPoolExecutor:
    """The dedicated resolver pool, created on first use.

    Lazily, because an import-time pool would start threads in every process
    that merely imports the package (the OpenAPI generator, the test
    collector)."""
    global _resolver_pool
    with _resolver_lock:
        if _resolver_pool is None:
            _resolver_pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=DNS_RESOLVER_THREADS,
                thread_name_prefix="webhook-dns",
            )
        return _resolver_pool


#: A path segment shown as-is: short enough that it cannot be a meaningful
#: credential, or made only of lowercase words (`services`, `api`, `webhooks`,
#: `webhook-receiver`) the way route names are and random tokens are not.
_SHORT_SEGMENT_CHARS = 8
_WORD_SEGMENT = re.compile(r"[a-z]+(?:[-_.][a-z]+)*")
_WORD_SEGMENT_MAX_CHARS = 24
_REDACTED = "<redacted>"


def _redact_segment(segment: str) -> str:
    base, semicolon, _params = segment.partition(";")
    if (
        len(base) <= _SHORT_SEGMENT_CHARS
        and "%" not in base
    ) or (len(base) <= _WORD_SEGMENT_MAX_CHARS and _WORD_SEGMENT.fullmatch(base)):
        shown = base
    else:
        shown = _REDACTED
    # Matrix parameters (`;token=…`, `;jsessionid=…`) are credentials as often
    # as a query string is, and are dropped the same way — wholesale.
    return f"{shown};{_REDACTED}" if semicolon else shown


def redact_url(url: Any) -> str:
    """A webhook URL as it may be DISPLAYED or AUDITED: no userinfo, no query,
    no fragment, no matrix parameters, and no path segment that could be a
    credential.

    Consumers put credentials in webhook URLs, in two places. `?token=…` is the
    home-grown receiver's shape; the PATH is Slack's and Discord's — an
    incoming-webhook URL is `/services/T…/B…/<secret>` or
    `/api/webhooks/<id>/<token>`, with no query at all. The 2026-09-13
    adversarial review found query tokens in clear in the audit log and every
    webhook listing; the wave-3 re-verify then found the path secrets still
    there, because this function only dropped the query (and its docstring
    wrongly named Slack as a query-token receiver). The row keeps the URL
    intact (the delivery has to reach it); every rendering goes through here,
    so a person with `api.webhooks.manage` can recognise an endpoint by host
    and route words without being handed the credential that authenticates
    to it.

    The query string is replaced wholesale by `?<redacted>` rather than masked
    value by value: a bare `?s3cr3t` has no key to keep. A path segment is kept
    only when it is at most 8 characters or is lowercase words; anything
    longer and mixed — a token, an id, a percent-encoded blob — becomes
    `<redacted>`. That keeps `/v2/webhook-receiver` legible and loses
    `/B0000/PATHSECRETxyz123`'s last segment.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        # Unparseable (a bad port, a broken IPv6 literal): nothing in it can be
        # trusted to be free of a credential, so nothing of it is shown.
        return "<unparseable url>"
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = "/".join(_redact_segment(segment) for segment in parts.path.split("/"))
    query = _REDACTED if parts.query else ""
    return urlunsplit((parts.scheme, netloc, path, query, ""))


_URL_IN_TEXT = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s'\"<>]+")


def redact_urls_in_text(text: Any) -> str:
    """Every URL inside a free-text string, redacted with `redact_url`.

    For delivery-history `error` strings: an httpx exception or a redirect
    refusal can quote the URL it was handling, query string and all."""
    return _URL_IN_TEXT.sub(lambda match: redact_url(match.group(0)), str(text or ""))


class UnsafeWebhookURL(ValueError):
    """The URL is refused by policy. PERMANENT — never retried.

    Scheme, credentials, a missing host, or an address the platform must not
    connect to. Retrying cannot change any of them, and a delivery loop that
    retried this would be a slow scan of the private network.
    """


class WebhookTransportError(RuntimeError):
    """The delivery could not be completed for a transient reason.

    DNS did not answer, the connection was refused, TLS failed, the deadline
    passed, the consumer sent more than the bound, or the redirect chain was
    too long. Worth another attempt later, which is exactly what makes it a
    different type from `UnsafeWebhookURL`.
    """


@dataclass(frozen=True)
class ValidatedTarget:
    """One URL that has passed every check, with the addresses it may dial."""

    url: str
    #: The host as httpcore will ask the backend for it: lower-case, IDNA
    #: punycode, IPv6 without brackets. Pinning under any other spelling
    #: would silently fail open (the backend would find no pin and refuse,
    #: which is at least loud — but this way it simply works).
    pin_key: str
    addresses: Tuple[str, ...]


@dataclass
class WebhookResponse:
    """What came back. The body is not kept, only its size and an excerpt."""

    status: int
    url: str
    redirects: int = 0
    body_bytes: int = 0
    excerpt: str = ""
    headers: Dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """2xx and nothing else.

        A 3xx never reaches here (redirects are followed or refused), and a
        1xx from a webhook consumer is not an acknowledgement of anything.
        """
        return 200 <= int(self.status) < 300


def _extra_address_policy(ip: ipaddress._BaseAddress) -> Optional[str]:
    """The rules this module adds on top of `core/net`'s address policy.

    Returns the reason a delivery must not go to this address, or None.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            # Refused even when the mapped half is public (`::ffff:8.8.8.8`),
            # which is the case `is_global` alone would let through. See the
            # module docstring, point 3, for why this is stated here as well
            # as inherited from `core/net`.
            return "IPv4-mapped IPv6 addresses are not allowed"
        if ip.sixtofour is not None:
            return "6to4 addresses are not allowed"
        if ip.teredo is not None:
            return "Teredo addresses are not allowed"
    if str(ip) in CLOUD_METADATA_ADDRESSES:
        return "the cloud metadata address is not allowed"
    return None


def _check_address(address: str) -> None:
    """One address against the whole policy. Raises `UnsafeWebhookURL`."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:  # pragma: no cover — getaddrinfo returns literals
        raise UnsafeWebhookURL(f"{address!r} is not an IP address") from exc
    reason = _extra_address_policy(parsed)
    if reason is not None:
        raise UnsafeWebhookURL(reason)
    try:
        # A LITERAL, so this is `core/net`'s address policy and nothing else —
        # no resolver call, and therefore no way for this to fail for a
        # transient reason and be mistaken for a policy refusal.
        net.resolve_public_ips(str(parsed))
    except net.UnsafeURLError as exc:
        raise UnsafeWebhookURL(
            "the endpoint resolves to an address that is not publicly routable"
        ) from exc


def _resolve(host: str) -> List[str]:
    """Every address `host` publishes, in the resolver's own order.

    Blocking: `socket.getaddrinfo` has no timeout and must never be called on
    the event loop (the 2026-09-04 incident where a cold lookup froze SSE
    delivery for every signed-in person — see `core/net.safe_fetch`). The
    async entry points below hand it to a thread.
    """
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return [str(literal)]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        # TRANSIENT, not a policy refusal: a resolver blip during a deploy is
        # the ordinary cause, and dropping a customer's delivery for it would
        # lose an event that a retry would have delivered.
        raise WebhookTransportError(f"could not resolve the endpoint host: {exc}") from exc
    out: List[str] = []
    for info in infos:
        address = info[4][0]
        if address not in out:
            out.append(address)
    if not out:
        raise WebhookTransportError("the endpoint host resolved to no addresses")
    return out


def validate_url(url: str) -> ValidatedTarget:
    """Scheme, credentials, host, DNS and the address policy, in that order.

    Blocking (it resolves). `validate_url_async` is the entry point for
    anything on the event loop.
    """
    text = str(url or "").strip()
    if not text:
        raise UnsafeWebhookURL("the webhook URL is empty")
    parsed = urlparse(text)
    if parsed.scheme.lower() != "https":
        # Named separately from every other refusal because it is the one a
        # developer hits by accident, and "https only" is actionable where
        # "unsafe URL" is not.
        raise UnsafeWebhookURL("a webhook URL must use https://")
    if parsed.username or parsed.password or "@" in (parsed.netloc or "").split("/")[0]:
        raise UnsafeWebhookURL("a webhook URL must not carry credentials")
    if not parsed.hostname:
        raise UnsafeWebhookURL("the webhook URL has no host")
    try:
        pin_key = httpx.URL(text).raw_host.decode("ascii")
    except (httpx.InvalidURL, UnicodeDecodeError, AttributeError) as exc:
        raise UnsafeWebhookURL("the webhook URL cannot be parsed") from exc
    if not pin_key:
        raise UnsafeWebhookURL("the webhook URL has no host")
    addresses = _resolve(pin_key)
    for address in addresses:
        # EVERY address, not the first: a name that publishes one public and
        # one private record is the textbook rebinding setup, and dialling
        # "whichever the resolver hands us" would be a coin toss.
        _check_address(address)
    return ValidatedTarget(url=text, pin_key=pin_key, addresses=tuple(addresses))


async def validate_url_async(
    url: str, *, dns_timeout_seconds: Optional[float] = None
) -> ValidatedTarget:
    """`validate_url` off the event loop, on the webhook resolver pool, under
    a deadline.

    DNS is blocking and has no timeout, so it runs on `_resolver_executor()`
    — never the loop's default executor (see `DNS_RESOLVER_THREADS`) — and the
    wait is bounded by `DNS_TIMEOUT_SECONDS`. A lookup that outlives it is a
    TRANSIENT failure (the nameserver may answer next time), and its future is
    cancelled so a lookup still waiting for a pool thread never starts.
    """
    timeout = DNS_TIMEOUT_SECONDS if dns_timeout_seconds is None else float(dns_timeout_seconds)
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(_resolver_executor(), validate_url, url)
    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise WebhookTransportError(
            f"the endpoint host did not resolve within {timeout:g}s"
        ) from exc


def _pinned_client(backend: Any) -> httpx.AsyncClient:
    """An httpx client that can only dial addresses the guard validated.

    The two names borrowed from `core/net` are private to that module, so
    their absence is checked rather than assumed: if an upgrade or a refactor
    renames them, this raises on the first delivery instead of quietly
    falling back to an unpinned transport that resolves the hostname again.
    """
    transport_factory = getattr(net, "_PinnedTransport", None)
    if transport_factory is None:  # pragma: no cover — guards a refactor
        raise RuntimeError(
            "app.core.net no longer exposes the pinned transport; webhook "
            "delivery needs review before it can dial anything"
        )
    return httpx.AsyncClient(
        transport=transport_factory(backend),
        follow_redirects=False,  # every hop is re-validated by hand below
        timeout=httpx.Timeout(connect=3.0, read=TIMEOUT_SECONDS, write=3.0, pool=2.0),
        trust_env=False,  # a proxy would resolve the name itself and void the pin
    )


def _new_backend() -> Any:
    backend_factory = getattr(net, "_PinnedBackend", None)
    if backend_factory is None:  # pragma: no cover — guards a refactor
        raise RuntimeError(
            "app.core.net no longer exposes the pinned backend; webhook "
            "delivery needs review before it can dial anything"
        )
    return backend_factory()


async def _read_capped(response: httpx.Response) -> Tuple[int, str]:
    """Read and throw away the body, keeping its size and a short excerpt."""
    total = 0
    kept = bytearray()
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if len(kept) < ERROR_EXCERPT_CHARS * 4:
            kept.extend(chunk[: ERROR_EXCERPT_CHARS * 4 - len(kept)])
        if total > MAX_RESPONSE_BYTES:
            raise WebhookTransportError(
                f"the endpoint sent more than {MAX_RESPONSE_BYTES} bytes"
            )
    excerpt = kept.decode("utf-8", "replace").strip()[:ERROR_EXCERPT_CHARS]
    return total, excerpt


async def _post(
    url: str, body: bytes, headers: Mapping[str, str]
) -> WebhookResponse:
    backend = _new_backend()
    target = await validate_url_async(url)
    backend.pin(target.pin_key, list(target.addresses))
    current = target.url
    redirects = 0
    async with _pinned_client(backend) as client:
        for hop in range(MAX_REDIRECTS + 1):
            next_url = ""
            try:
                async with client.stream(
                    "POST", current, content=body, headers=dict(headers)
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise WebhookTransportError("redirect without a Location header")
                        if hop == MAX_REDIRECTS:
                            # Refused BEFORE the target is resolved: a hop we
                            # will never take must not cost a DNS lookup, and
                            # a redirect loop must not be able to make us do
                            # one per hop forever.
                            raise WebhookTransportError("too many redirects")
                        try:
                            next_url = str(httpx.URL(current).join(location))
                        except httpx.InvalidURL:
                            # The parser's message quotes the Location, which
                            # can carry a token; the history row gets none.
                            raise WebhookTransportError("bad redirect target") from None
                    else:
                        size, excerpt = await _read_capped(response)
                        return WebhookResponse(
                            status=int(response.status_code),
                            url=current,
                            redirects=redirects,
                            body_bytes=size,
                            excerpt=excerpt,
                            headers={
                                key: response.headers.get(key, "")
                                for key in ("content-type", "retry-after")
                                if response.headers.get(key)
                            },
                        )
            except httpx.HTTPError as exc:
                raise WebhookTransportError(
                    f"delivery failed: {redact_urls_in_text(exc)}"
                ) from exc
            # Reached only on a redirect, and only after the stream block
            # closed the response — so the hop we are merely passing through
            # costs no body read. The target is validated AND pinned before
            # the next request opens, which is what stops
            # `https://public.example/` from 302-ing us to `https://10.0.0.5/`.
            # A refusal here raises UnsafeWebhookURL and is permanent, exactly
            # as it is for the first hop.
            hop_target = await validate_url_async(next_url)
            backend.pin(hop_target.pin_key, list(hop_target.addresses))
            current = hop_target.url
            redirects += 1
    raise WebhookTransportError("too many redirects")  # pragma: no cover


async def post_json(
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    *,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> WebhookResponse:
    """POST `body` to a validated endpoint under ONE deadline.

    The whole delivery — resolution, connection, TLS, the write, every
    redirect hop and the bounded read — shares `timeout_seconds`. Per-phase
    timeouts alone would let a consumer that dribbles one byte per second
    hold a worker slot for minutes; `asyncio.wait_for` is what turns the
    contract's "10 s timeout" into a promise about the delivery rather than
    about a socket operation.
    """
    try:
        return await asyncio.wait_for(_post(url, body, headers), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise WebhookTransportError(
            f"the endpoint did not answer within {int(timeout_seconds)}s"
        ) from exc


__all__ = [
    "CLOUD_METADATA_ADDRESSES",
    "DNS_RESOLVER_THREADS",
    "DNS_TIMEOUT_SECONDS",
    "ERROR_EXCERPT_CHARS",
    "MAX_REDIRECTS",
    "MAX_RESPONSE_BYTES",
    "TIMEOUT_SECONDS",
    "UnsafeWebhookURL",
    "ValidatedTarget",
    "WebhookResponse",
    "WebhookTransportError",
    "post_json",
    "redact_url",
    "redact_urls_in_text",
    "validate_url",
    "validate_url_async",
]
