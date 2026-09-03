"""SSRF-safe HTTP fetch (Phase 1, shared by web-search / URL / repo / crawl).

Every server-side fetch of a user-influenced URL goes through ``safe_fetch``.
This is the single choke point that keeps "fetch this URL for me" from being
turned into a request against internal services (SSRF). The security
critique of 2026-09-03 found four gaps in the first version; each is closed
here and each has a test in ``tests/test_net_ssrf.py``.

WHAT IS GUARANTEED

1. Address policy. A host is refused when ANY address it resolves to is not
   globally routable: ``ip.is_global`` is False (which alone covers CGNAT
   100.64.0.0/10, the IETF protocol block 192.0.0.0/24, benchmarking
   198.18.0.0/15, the documentation nets, IPv4-mapped/6to4/Teredo IPv6),
   plus the explicit private / loopback / link-local (cloud metadata) /
   reserved (NAT64 64:ff9b::/96 embeds an IPv4 address) / multicast /
   unspecified checks. Blocking on *any* address, not the first, defeats
   names that publish one public and one private record.

2. Redirects are followed by hand (``follow_redirects=False``), at most
   ``_MAX_REDIRECTS`` hops, and every hop's Location is validated exactly
   like the first URL — scheme, host, DNS — before a byte of it is fetched.

3. The body is streamed, never buffered whole. A Content-Length above
   ``max_bytes`` is refused before the body is read; otherwise reading stops
   at the first decoded chunk that pushes the total past ``max_bytes`` and
   the connection is closed. The count is of *decoded* bytes, so a gzip bomb
   is measured by what it inflates to — which is what would sit in memory.

4. DNS pinning (the rebinding TOCTOU). The guard resolves a host in a worker
   thread; the connection is then made by a custom httpcore network backend
   (``_PinnedBackend``) that connects to one of the addresses the guard
   validated and refuses any host it has no pin for. The backend hands the
   anyio layer an IP *literal*, which anyio connects to without a resolver
   call (verified: zero ``getaddrinfo`` calls through ``AnyIOBackend`` for a
   literal), so the address that is checked is the address that is dialled.
   TLS is untouched: httpcore derives the SNI name and the certificate's
   hostname check from the URL's host, not from the dialled address, and the
   ``Host`` header is the URL's host. The loopback TLS test proves both — a
   certificate for the URL's name is accepted and one for another name is
   rejected, while every packet goes to 127.0.0.1.

WHAT IS NOT GUARANTEED

- A *public* address that is itself a gateway to something internal (a
  reverse proxy, a NAT64 gateway, a public IP bound on this very box) is
  indistinguishable from any other public host. The policy is about
  addresses, not about what answers on them.
- Environment proxies (HTTP_PROXY etc.) are deliberately ignored
  (``trust_env=False`` and an explicit transport). If a deployment ever
  routes egress through a proxy, the proxy resolves the name and pinning
  does not apply there; that would need re-review.
- The pin is the resolver's answer at guard time. A name that legitimately
  changes between guard and connect is dialled at the older, validated
  address — that is the point, not a bug.
- HTTP/2 is not negotiated (``http1`` only), so a server cannot coalesce
  another origin onto a pinned connection.
- URL credentials (``user:pw@host``) pass through to the request as basic
  auth, as before. Out of scope for the address policy.

Pure-ish: stdlib + httpx + httpcore. No network at import time.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpcore
import httpx

# Cloud metadata endpoints are link-local (169.254.169.254 / fd00:ec2::254) and
# already covered by the reserved-range checks, but we name them for clarity.
_MAX_REDIRECTS = 3
_ALLOWED_SCHEMES = {"http", "https"}


class UnsafeURLError(ValueError):
    """Raised when a URL is blocked before any bytes are fetched (SSRF guard)."""


class FetchError(RuntimeError):
    """Raised when a fetch fails for a benign reason (timeout, too big, status)."""

    #: HTTP status when the failure IS an HTTP status (4xx/5xx), else None.
    #: The crawler needs the distinction RFC 9309 draws for robots.txt:
    #: 4xx means "no rules, crawl allowed"; 5xx means "assume disallowed".
    status: "int | None" = None


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content_type: str
    body: bytes
    #: The few response headers provenance cares about (lower-cased keys:
    #: last-modified, etag, date). Empty for callers and tests that build a
    #: result by hand — nothing downstream requires them.
    headers: dict = field(default_factory=dict)


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """True for any address we must never connect to on the user's behalf."""
    return (
        # Not globally routable per the IANA special-purpose registries. On
        # Python 3.12 this is the only flag that catches CGNAT 100.64.0.0/10
        # (is_private is False there) — the range a home ISP or a tailnet
        # hands out, i.e. exactly where a neighbour's box would sit.
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 and fe80::/10 (incl. metadata IP)
        or ip.is_reserved  # includes NAT64 64:ff9b::/96, which is_global misses
        or ip.is_multicast
        or ip.is_unspecified
        or (getattr(ip, "is_site_local", False))  # fec0::/10
    )


def resolve_public_ips(host: str) -> List[str]:
    """Resolve `host` and return its IPs, raising UnsafeURLError if ANY of them
    is a blocked (private/reserved/non-global) address.

    Blocking when *any* resolved address is private defeats DNS-rebinding-style
    tricks where a name resolves to both a public and a private IP. The list
    keeps the resolver's order (glibc sorts per RFC 6724) so the pinned
    connection dials the address the OS itself would have chosen first.
    """
    # A bare IP literal is validated directly (getaddrinfo would echo it back).
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _ip_is_blocked(literal):
            raise UnsafeURLError(f"host {host} is a private/reserved address")
        return [str(literal)]

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"could not resolve host {host!r}") from exc
    ips: List[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in ips:
            ips.append(ip)
    if not ips:
        raise UnsafeURLError(f"host {host!r} resolved to no addresses")
    for ip in ips:
        if _ip_is_blocked(ipaddress.ip_address(ip)):
            raise UnsafeURLError(
                f"host {host!r} resolves to a private/reserved address ({ip})"
            )
    return ips


def _validate(url: str) -> Tuple[str, List[str]]:
    """Scheme + host checks, then DNS. Returns ``(pin_key, ips)``.

    ``pin_key`` is the host exactly as httpcore will ask the network backend
    for it: lower-case, IDNA punycode, IPv6 without brackets (httpx's
    ``raw_host``). Resolving *that* string, and pinning under it, is what
    makes "the address we checked" and "the address we dial" the same thing.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parsed.scheme!r} is not allowed")
    if not parsed.hostname:
        raise UnsafeURLError("URL has no host")
    try:
        pin_key = httpx.URL(url).raw_host.decode("ascii")
    except (httpx.InvalidURL, UnicodeDecodeError) as exc:
        raise UnsafeURLError(f"URL is not fetchable: {exc}") from exc
    if not pin_key:
        raise UnsafeURLError("URL has no host")
    ips = resolve_public_ips(pin_key)  # raises if any resolved IP is blocked
    return pin_key, ips


def assert_url_is_fetchable(url: str) -> str:
    """Validate scheme + host (with DNS resolution) before a fetch. Returns the
    URL unchanged or raises UnsafeURLError."""
    _validate(url)
    return url


# ---------------------------------------------------------------------------
# Pinned transport: dial the validated address, keep TLS for the URL's host
# ---------------------------------------------------------------------------


def _new_inner_backend() -> httpcore.AsyncNetworkBackend:
    """The real socket layer. A function (not a constant) so tests can swap in
    a recording backend without touching httpcore's module state."""
    return httpcore.AnyIOBackend()


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    """An httpcore network backend that only ever dials pinned addresses.

    httpcore calls ``connect_tcp(host, port)`` with the URL's host; this
    backend looks the host up in the pins the SSRF guard recorded and hands
    the inner backend an IP literal instead. A host with no pin is refused —
    the backend is an allowlist, so even a bug elsewhere in the redirect
    logic cannot make it dial a name the guard never validated.
    """

    def __init__(
        self, inner: Optional[httpcore.AsyncNetworkBackend] = None
    ) -> None:
        self._inner = inner if inner is not None else _new_inner_backend()
        self._pins: Dict[str, List[str]] = {}

    def pin(self, host: str, ips: List[str]) -> None:
        self._pins[host] = list(ips)

    def pinned(self, host: str) -> List[str]:
        return list(self._pins.get(host, ()))

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: Optional[float] = None,
        local_address: Optional[str] = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        ips = self._pins.get(host)
        if not ips:
            raise httpcore.ConnectError(
                f"refusing to connect to {host!r}: not validated by the SSRF guard"
            )
        # One connect budget across all addresses, so a dead multi-homed host
        # still sheds in the caller's connect allowance (3 s) instead of
        # 3 s × N records. anyio's happy-eyeballs did that for a hostname;
        # with literals we do it ourselves, sequentially.
        deadline = None if timeout is None else time.monotonic() + timeout
        last_exc: Optional[Exception] = None
        for ip in ips:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
            try:
                return await self._inner.connect_tcp(
                    ip,
                    port,
                    timeout=remaining,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise httpcore.ConnectTimeout(f"connect budget exhausted for {host!r}")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        # Nothing user-influenced may ever reach a local socket.
        raise httpcore.ConnectError("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


#: Building an SSL context loads the certifi bundle: 11.7 ms measured here.
#: The crawler fetches thousands of pages, so one context is shared. A
#: context is safe to share across connections (that is its designed use),
#: and httpcore's per-connect ALPN mutation is idempotent. Keyed on the two
#: env vars httpx honours so a deployment's custom CA bundle — and the
#: loopback TLS test — get their own context instead of the cached one.
_SSL_CONTEXTS: Dict[Tuple[Optional[str], Optional[str]], ssl.SSLContext] = {}


def _ssl_context() -> ssl.SSLContext:
    key = (os.environ.get("SSL_CERT_FILE"), os.environ.get("SSL_CERT_DIR"))
    ctx = _SSL_CONTEXTS.get(key)
    if ctx is None:
        # verify=True → certifi (or SSL_CERT_FILE/DIR), check_hostname=True.
        ctx = httpx.create_ssl_context(verify=True, trust_env=True)
        _SSL_CONTEXTS[key] = ctx
    return ctx


class _PinnedTransport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool dials through ``_PinnedBackend``.

    httpx exposes no ``network_backend`` argument, while httpcore's pool
    does (documented). Subclassing keeps httpx's exception mapping and
    response streaming; only the pool is replaced. The attribute is private
    to httpx, hence the isinstance check — if an upgrade renames it we fail
    the first fetch loudly rather than silently dialling unpinned.
    """

    def __init__(self, backend: _PinnedBackend) -> None:
        ctx = _ssl_context()
        super().__init__(verify=ctx, http1=True, http2=False, retries=0, trust_env=False)
        if not isinstance(getattr(self, "_pool", None), httpcore.AsyncConnectionPool):
            raise RuntimeError(
                "httpx changed AsyncHTTPTransport internals; pinned transport needs review"
            )
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ctx,
            network_backend=backend,
            http1=True,
            http2=False,
            retries=0,
            # One fetch per client: a redirect chain touches a few origins
            # at most, and idle keep-alives die with the client.
            max_connections=4,
            max_keepalive_connections=4,
            keepalive_expiry=5.0,
        )


# ---------------------------------------------------------------------------
# The fetch
# ---------------------------------------------------------------------------


def _validate_and_pin(url: str, backend: _PinnedBackend) -> str:
    """Guard + pin in one step, run off-loop (DNS is blocking)."""
    pin_key, ips = _validate(url)
    backend.pin(pin_key, ips)
    return url


async def _read_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    """Stream the body, aborting the moment it is known to exceed `max_bytes`."""
    declared = resp.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes:
        raise FetchError(
            f"response declares {declared} bytes, exceeds {max_bytes} bytes"
        )
    chunks: List[bytes] = []
    total = 0
    # No chunk_size: httpx would otherwise buffer that many decoded bytes
    # before yielding, delaying the abort. httpcore already reads the socket
    # 64 KB at a time, so the loop is not fine-grained enough to matter.
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            # Leaving the `client.stream` block closes the response, which
            # drops the connection rather than draining the rest.
            raise FetchError(f"response exceeds {max_bytes} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


async def safe_fetch(
    url: str,
    *,
    timeout_ms: int,
    max_bytes: int,
    accept: Optional[str] = None,
) -> FetchResult:
    """Fetch a public URL with SSRF guards, DNS pinning, manual redirect
    re-validation, a time limit, and a streamed hard body-size cap.

    Redirects are followed manually (httpx `follow_redirects=False`) so every
    hop's Location is re-validated against the SSRF blocklist — a public URL
    that 302s to http://169.254.169.254/ is refused at the hop, not followed.
    See the module docstring for what is and is not guaranteed.
    """
    backend = _PinnedBackend()
    # SSRF validation resolves DNS, and socket.getaddrinfo is BLOCKING with no
    # timeout. Called inline it froze the whole event loop for seconds on a cold
    # lookup — stalling SSE token delivery for every other user, not just this
    # fetch. getaddrinfo is thread-safe, so the default executor is fine here.
    current = await asyncio.to_thread(_validate_and_pin, url, backend)
    # Split the budget: a dead host should shed on connect in 3s rather than
    # spend the caller's whole read allowance.
    timeout = httpx.Timeout(
        connect=3.0, read=timeout_ms / 1000.0, write=3.0, pool=2.0
    )
    headers = {"User-Agent": "TechSaraBot/1.0 (+local analytics)"}
    if accept:
        headers["Accept"] = accept

    async with httpx.AsyncClient(
        transport=_PinnedTransport(backend),
        follow_redirects=False,
        timeout=timeout,
        headers=headers,
        # No env proxies: a proxy would resolve the name itself and bypass
        # the pin. (httpx already ignores them with an explicit transport;
        # stating it keeps the property visible.)
        trust_env=False,
    ) as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            try:
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError("redirect without a Location header")
                        try:
                            next_url = str(httpx.URL(current).join(location))
                        except httpx.InvalidURL as exc:
                            raise FetchError(f"bad redirect target: {exc}") from exc
                    else:
                        if resp.status_code >= 400:
                            err = FetchError(f"HTTP {resp.status_code} for {current}")
                            err.status = resp.status_code
                            raise err
                        body = await _read_capped(resp, max_bytes)
                        return FetchResult(
                            url=current,
                            status=resp.status_code,
                            content_type=resp.headers.get("content-type", ""),
                            body=body,
                            # Kept for provenance: when the server says the page
                            # last changed, and the validators a conditional
                            # re-fetch needs.
                            headers={
                                k: resp.headers.get(k, "")
                                for k in ("last-modified", "etag", "date")
                                if resp.headers.get(k)
                            },
                        )
            except httpx.HTTPError as exc:
                raise FetchError(f"fetch failed: {exc}") from exc
            if _hop == _MAX_REDIRECTS:
                # Don't resolve a hop we will never fetch.
                raise FetchError("too many redirects")
            # Re-validate AND re-pin every hop (off-loop, same reason as above).
            # The redirect body was never read; leaving the stream block closed it.
            current = await asyncio.to_thread(_validate_and_pin, next_url, backend)
    raise FetchError("too many redirects")  # unreachable; keeps the type-checker honest
