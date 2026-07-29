"""SSRF-safe HTTP fetch (Phase 1, shared by web-search / URL / repo features).

Every server-side fetch of a user-influenced URL goes through here. It resolves
DNS *first* and refuses any host that resolves into a private, loopback,
link-local, or otherwise reserved range (and the cloud metadata IP), re-checking
at every redirect hop. It also bounds time and body size. This is the single
choke point that keeps "fetch this URL for me" from being turned into a request
against internal services (SSRF).

Pure-ish: only stdlib + httpx. No network at import time.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

import httpx

# Cloud metadata endpoints are link-local (169.254.169.254 / fd00:ec2::254) and
# already covered by the reserved-range checks, but we name them for clarity.
_MAX_REDIRECTS = 3
_ALLOWED_SCHEMES = {"http", "https"}


class UnsafeURLError(ValueError):
    """Raised when a URL is blocked before any bytes are fetched (SSRF guard)."""


class FetchError(RuntimeError):
    """Raised when a fetch fails for a benign reason (timeout, too big, status)."""


@dataclass
class FetchResult:
    url: str  # final URL after redirects
    status: int
    content_type: str
    body: bytes


def _ip_is_blocked(ip: ipaddress._BaseAddress) -> bool:
    """True for any address we must never connect to on the user's behalf."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local  # 169.254.0.0/16 and fe80::/10 (incl. metadata IP)
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        or (getattr(ip, "is_site_local", False))  # fec0::/10
    )


def resolve_public_ips(host: str) -> List[str]:
    """Resolve `host` and return its IPs, raising UnsafeURLError if ANY of them
    is a blocked (private/reserved) address.

    Blocking when *any* resolved address is private defeats DNS-rebinding-style
    tricks where a name resolves to both a public and a private IP.
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
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"could not resolve host {host!r}") from exc
    ips = {info[4][0] for info in infos}
    if not ips:
        raise UnsafeURLError(f"host {host!r} resolved to no addresses")
    for ip in ips:
        if _ip_is_blocked(ipaddress.ip_address(ip)):
            raise UnsafeURLError(
                f"host {host!r} resolves to a private/reserved address ({ip})"
            )
    return sorted(ips)


def assert_url_is_fetchable(url: str) -> str:
    """Validate scheme + host (with DNS resolution) before a fetch. Returns the
    normalized URL or raises UnsafeURLError."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parsed.scheme!r} is not allowed")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    resolve_public_ips(host)  # raises if any resolved IP is private/reserved
    return url


async def safe_fetch(
    url: str,
    *,
    timeout_ms: int,
    max_bytes: int,
    accept: Optional[str] = None,
) -> FetchResult:
    """Fetch a public URL with SSRF guards, manual redirect re-validation, a
    time limit, and a hard body-size cap.

    Redirects are followed manually (httpx `follow_redirects=False`) so every
    hop's Location is re-validated against the SSRF blocklist — a public URL
    that 302s to http://169.254.169.254/ is refused at the hop, not followed.
    """
    # SSRF validation resolves DNS, and socket.getaddrinfo is BLOCKING with no
    # timeout. Called inline it froze the whole event loop for seconds on a cold
    # lookup — stalling SSE token delivery for every other user, not just this
    # fetch. getaddrinfo is thread-safe, so the default executor is fine here.
    await asyncio.to_thread(assert_url_is_fetchable, url)
    # Split the budget: a dead host should shed on connect in 3s rather than
    # spend the caller's whole read allowance.
    timeout = httpx.Timeout(
        connect=3.0, read=timeout_ms / 1000.0, write=3.0, pool=2.0
    )
    headers = {"User-Agent": "TechSaraBot/1.0 (+local analytics)"}
    if accept:
        headers["Accept"] = accept

    current = url
    async with httpx.AsyncClient(
        follow_redirects=False, timeout=timeout, headers=headers
    ) as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            try:
                resp = await client.get(current)
            except httpx.HTTPError as exc:
                raise FetchError(f"fetch failed: {exc}") from exc

            if resp.is_redirect:
                location = resp.headers.get("location")
                if not location:
                    raise FetchError("redirect without a Location header")
                current = str(httpx.URL(current).join(location))
                # re-validate every hop (off-loop, same reason as above)
                await asyncio.to_thread(assert_url_is_fetchable, current)
                continue

            if resp.status_code >= 400:
                raise FetchError(f"HTTP {resp.status_code} for {current}")

            body = resp.content[: max_bytes + 1]
            if len(body) > max_bytes:
                raise FetchError(f"response exceeds {max_bytes} bytes")
            return FetchResult(
                url=current,
                status=resp.status_code,
                content_type=resp.headers.get("content-type", ""),
                body=resp.content[:max_bytes],
            )
    raise FetchError("too many redirects")
