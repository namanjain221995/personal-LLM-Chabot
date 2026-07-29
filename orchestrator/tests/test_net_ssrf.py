"""SSRF guard tests (Phase 1). Verifies the blocklist without real network I/O."""
import ipaddress

import pytest

from app.core import net


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.4.4",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",
        "::1",
        "fd00::1",
        "fe80::1",
    ],
)
def test_private_and_reserved_literals_are_blocked(ip):
    with pytest.raises(net.UnsafeURLError):
        net.resolve_public_ips(ip)


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34"])
def test_public_literals_pass(ip):
    assert net.resolve_public_ips(ip) == [ip]


def test_scheme_must_be_http_s():
    with pytest.raises(net.UnsafeURLError):
        net.assert_url_is_fetchable("file:///etc/passwd")
    with pytest.raises(net.UnsafeURLError):
        net.assert_url_is_fetchable("gopher://evil/")


def test_missing_host_blocked():
    with pytest.raises(net.UnsafeURLError):
        net.assert_url_is_fetchable("http:///nohost")


def test_hostname_resolving_to_private_is_blocked(monkeypatch):
    # A name that resolves (partly) to a private IP must be refused.
    def fake_getaddrinfo(host, *a, **k):
        return [(2, 1, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(net.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(net.UnsafeURLError):
        net.assert_url_is_fetchable("http://sneaky.internal.example/")


def test_hostname_with_mixed_public_and_private_is_blocked(monkeypatch):
    # DNS-rebinding style: one public, one private → still blocked.
    def fake_getaddrinfo(host, *a, **k):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("127.0.0.1", 0)),
        ]

    monkeypatch.setattr(net.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(net.UnsafeURLError):
        net.assert_url_is_fetchable("http://rebind.example/")


def test_public_hostname_passes(monkeypatch):
    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    assert net.assert_url_is_fetchable("https://example.com/page") == (
        "https://example.com/page"
    )


def test_safe_fetch_revalidates_redirect_to_private(monkeypatch):
    """A public URL that 302-redirects to a private IP is refused at the hop."""
    import asyncio

    import httpx

    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
        if host == "example.com"
        else [(2, 1, 6, "", ("10.0.0.9", 0))],
    )

    def handler(request):
        return httpx.Response(302, headers={"location": "http://internal.local/"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(net.httpx, "AsyncClient", client_factory)
    with pytest.raises(net.UnsafeURLError):
        asyncio.run(
            net.safe_fetch("https://example.com/", timeout_ms=2000, max_bytes=1000)
        )


def test_safe_fetch_returns_body_and_caps_size(monkeypatch):
    """A normal 200 returns the body; oversized responses are rejected."""
    import asyncio

    import httpx

    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
    )
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"<h1>hi</h1>"
        )
    )
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        net.httpx,
        "AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": transport}),
    )
    res = asyncio.run(
        net.safe_fetch("https://example.com/", timeout_ms=2000, max_bytes=1000)
    )
    assert res.status == 200 and b"hi" in res.body
    with pytest.raises(net.FetchError):
        asyncio.run(
            net.safe_fetch("https://example.com/", timeout_ms=2000, max_bytes=3)
        )
