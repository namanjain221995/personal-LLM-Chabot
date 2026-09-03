"""SSRF guard tests (Phase 1 + the 2026-09-03 hardening).

Everything runs without the public network: DNS is faked by monkeypatching
``socket.getaddrinfo``, sockets are either httpx ``MockTransport`` handlers
or an httpcore recording backend, and the two real-TLS tests talk to a
loopback listener with a throwaway certificate minted by ``openssl``.
"""
import asyncio
import ipaddress
import shutil
import ssl
import subprocess

import httpcore
import httpx
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


# ---------------------------------------------------------------------------
# 2026-09-03 hardening. Shared helpers first.
# ---------------------------------------------------------------------------

PUBLIC = "93.184.216.34"


def _dns(monkeypatch, table):
    """Fake resolver: ``table`` maps host → IP (str) or a callable(host) → IP.
    Records every lookup so tests can assert *how many times* DNS was asked —
    the rebinding property is precisely "exactly once, by the guard"."""
    lookups = []

    def fake(host, *a, **k):
        lookups.append(host)
        ip = table(host) if callable(table) else table[host]
        family = 10 if ":" in ip else 2
        return [(family, 1, 6, "", (ip, 0))]

    monkeypatch.setattr(net.socket, "getaddrinfo", fake)
    return lookups


def _use_mock_transport(monkeypatch, handler):
    """Route safe_fetch's client through an httpx MockTransport (no sockets).
    Bypasses the pinned backend on purpose: these tests are about the HTTP-
    level logic (redirect hops, body cap), not the dial."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        net.httpx,
        "AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": transport}),
    )


class _RecordingStream(httpcore.AsyncMockStream):
    """Serves canned bytes and records the hostname TLS was started for."""

    def __init__(self, buffer, log):
        super().__init__(list(buffer))
        self._log = log

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self._log.append(("tls", server_hostname))
        return self


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """The 'real socket layer' stand-in beneath _PinnedBackend. Records what
    it was asked to dial — an IP literal if pinning works, a hostname if it
    does not — and can be told to fail specific addresses."""

    def __init__(self, responses=(), fail=()):
        self.log = []
        self._responses = [list(r) for r in responses]
        self._fail = set(fail)

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        self.log.append(("connect", host, port, timeout))
        if host in self._fail:
            raise httpcore.ConnectError(f"refused by test: {host}")
        buf = self._responses.pop(0) if self._responses else []
        return _RecordingStream(buf, self.log)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise AssertionError("unix sockets must never be dialled")

    async def sleep(self, seconds):
        pass

    def dialled(self):
        return [e[1] for e in self.log if e[0] == "connect"]


def _use_backend(monkeypatch, backend):
    monkeypatch.setattr(net, "_new_inner_backend", lambda: backend)


def _http(status, body=b"", extra=""):
    line = {200: "200 OK", 302: "302 Found", 500: "500 Internal Server Error"}[status]
    head = f"HTTP/1.1 {line}\r\nContent-Length: {len(body)}\r\n{extra}\r\n"
    return [head.encode(), body]


# ---------------------------------------------------------------------------
# (1) non-global addresses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "100.64.0.1",  # CGNAT / shared address space — is_private is False here
        "100.127.255.254",  # last CGNAT address
        "192.0.0.8",  # IETF protocol assignments
        "198.18.0.1",  # benchmarking
        "192.0.2.10",  # TEST-NET-1
        "240.0.0.1",  # class E
        "::ffff:169.254.169.254",  # IPv4-mapped metadata IP
        "64:ff9b::a9fe:a9fe",  # NAT64-embedded metadata IP (is_global is True!)
        "2002:7f00:1::1",  # 6to4 embedding 127.0.0.1
    ],
)
def test_non_global_literals_are_blocked(ip):
    assert net._ip_is_blocked(ipaddress.ip_address(ip))
    with pytest.raises(net.UnsafeURLError):
        net.resolve_public_ips(ip)


def test_cgnat_hostname_is_blocked(monkeypatch):
    _dns(monkeypatch, {"tailnet.example": "100.101.102.103"})
    with pytest.raises(net.UnsafeURLError, match="private/reserved"):
        net.assert_url_is_fetchable("https://tailnet.example/")


def test_resolver_order_is_kept_and_deduplicated(monkeypatch):
    # glibc sorts per RFC 6724; the pin dials in that order, so keep it.
    monkeypatch.setattr(
        net.socket,
        "getaddrinfo",
        lambda *a, **k: [
            (10, 1, 6, "", ("2606:4700::1111", 0, 0, 0)),
            (2, 1, 6, "", ("1.1.1.1", 0)),
            (2, 2, 17, "", ("1.1.1.1", 0)),
        ],
    )
    assert net.resolve_public_ips("one.one.one.one") == ["2606:4700::1111", "1.1.1.1"]


# ---------------------------------------------------------------------------
# (4) pinning: the guard's address is the dialled address
# ---------------------------------------------------------------------------


def test_pin_key_is_the_host_httpcore_will_ask_for(monkeypatch):
    lookups = _dns(monkeypatch, lambda host: PUBLIC)
    assert net._validate("https://EXAMPLE.com/x")[0] == "example.com"
    assert net._validate("https://bücher.example/")[0] == "xn--bcher-kva.example"
    assert net._validate("http://[2606:4700::1111]/")[0] == "2606:4700::1111"
    # The resolved string is the normalised one, not the raw URL text.
    assert lookups == ["example.com", "xn--bcher-kva.example"]


def test_pinned_backend_dials_the_ip_not_the_name():
    inner = _RecordingBackend()
    backend = net._PinnedBackend(inner)
    backend.pin("example.com", [PUBLIC])
    asyncio.run(backend.connect_tcp("example.com", 443, timeout=3.0))
    assert inner.dialled() == [PUBLIC]


def test_pinned_backend_refuses_unpinned_hosts():
    inner = _RecordingBackend()
    backend = net._PinnedBackend(inner)
    with pytest.raises(httpcore.ConnectError, match="not validated"):
        asyncio.run(backend.connect_tcp("evil.example", 80, timeout=3.0))
    assert inner.dialled() == []  # allowlist: nothing left the process


def test_pinned_backend_never_dials_unix_sockets():
    backend = net._PinnedBackend(_RecordingBackend())
    with pytest.raises(httpcore.ConnectError):
        asyncio.run(backend.connect_unix_socket("/var/run/docker.sock"))


def test_pinned_backend_falls_through_to_the_next_validated_address():
    inner = _RecordingBackend(fail={"2606:4700::1111"})
    backend = net._PinnedBackend(inner)
    backend.pin("h.example", ["2606:4700::1111", "1.1.1.1"])
    asyncio.run(backend.connect_tcp("h.example", 443, timeout=3.0))
    assert inner.dialled() == ["2606:4700::1111", "1.1.1.1"]


def test_pinned_backend_shares_one_connect_budget(monkeypatch):
    """A dead 4-address host must not cost 4 × the connect timeout."""
    clock = [0.0]
    monkeypatch.setattr(net.time, "monotonic", lambda: clock[0])

    class Slow(_RecordingBackend):
        async def connect_tcp(self, host, port, timeout=None, **kw):
            self.log.append(("connect", host, port, timeout))
            clock[0] += 1.0  # each attempt burns a second, then fails
            raise httpcore.ConnectError("down")

    inner = Slow()
    backend = net._PinnedBackend(inner)
    backend.pin("dead.example", ["1.1.1.1", "1.0.0.1", "9.9.9.9", "8.8.8.8"])
    with pytest.raises(httpcore.ConnectError):
        asyncio.run(backend.connect_tcp("dead.example", 443, timeout=2.5))
    # 2.5 s budget → attempts at t=0, 1, 2; the fourth address is never tried.
    assert inner.dialled() == ["1.1.1.1", "1.0.0.1", "9.9.9.9"]
    # And each attempt got only what was left, not the full budget again.
    assert [e[3] for e in inner.log] == [2.5, 1.5, 0.5]


def test_rebinding_connects_to_the_validated_address(monkeypatch):
    """DNS rebinding: the guard sees a public IP, a *second* lookup would see
    a private one. There must be no second lookup — the dial goes to the
    address the guard validated, and TLS is still for the URL's host."""
    lookups = []

    def rebinding_dns(host, *a, **k):
        lookups.append(host)
        ip = PUBLIC if len(lookups) == 1 else "10.0.0.9"
        return [(2, 1, 6, "", (ip, 0))]

    monkeypatch.setattr(net.socket, "getaddrinfo", rebinding_dns)
    inner = _RecordingBackend([_http(200, b"ok", "Content-Type: text/plain\r\n")])
    _use_backend(monkeypatch, inner)

    res = asyncio.run(
        net.safe_fetch("https://rebind.example/p", timeout_ms=2000, max_bytes=100)
    )
    assert res.status == 200 and res.body == b"ok"
    assert lookups == ["rebind.example"]  # resolved once — by the guard
    assert inner.dialled() == [PUBLIC]  # TCP went to the validated address
    assert ("tls", "rebind.example") in inner.log  # SNI/verify = URL host
    assert "10.0.0.9" not in inner.dialled()


def test_rebinding_on_a_redirect_hop_is_pinned_too(monkeypatch):
    """Each hop is re-resolved and re-pinned; the hop's dial uses ITS
    validated address, never a fresh lookup."""
    lookups = _dns(
        monkeypatch, {"a.example": PUBLIC, "b.example": "8.8.8.8"}
    )
    inner = _RecordingBackend(
        [
            _http(302, b"", "Location: https://b.example/final\r\n"),
            _http(200, b"done", "Content-Type: text/plain\r\n"),
        ]
    )
    _use_backend(monkeypatch, inner)
    res = asyncio.run(
        net.safe_fetch("https://a.example/", timeout_ms=2000, max_bytes=100)
    )
    assert res.url == "https://b.example/final" and res.body == b"done"
    assert lookups == ["a.example", "b.example"]
    assert inner.dialled() == [PUBLIC, "8.8.8.8"]
    assert [e for e in inner.log if e[0] == "tls"] == [
        ("tls", "a.example"),
        ("tls", "b.example"),
    ]


# ---------------------------------------------------------------------------
# (2) redirects: every hop re-validated, private targets refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "location",
    [
        "http://10.0.0.9/admin",  # RFC 1918 literal
        "http://100.64.0.1/",  # CGNAT literal
        "http://169.254.169.254/latest/meta-data/",  # metadata
        "http://[::1]:8080/",  # loopback v6
        "http://internal.local/",  # a name that resolves privately
        "file:///etc/passwd",  # scheme change
    ],
)
def test_redirect_to_a_private_target_is_blocked_before_dialling(monkeypatch, location):
    _dns(monkeypatch, {"example.com": PUBLIC, "internal.local": "192.168.0.7"})
    inner = _RecordingBackend([_http(302, b"", f"Location: {location}\r\n")])
    _use_backend(monkeypatch, inner)
    with pytest.raises(net.UnsafeURLError):
        asyncio.run(
            net.safe_fetch("https://example.com/", timeout_ms=2000, max_bytes=100)
        )
    # Only the first, public hop was ever dialled.
    assert inner.dialled() == [PUBLIC]


def test_redirect_chain_is_bounded_and_each_hop_is_resolved(monkeypatch):
    lookups = _dns(monkeypatch, lambda host: PUBLIC)
    _use_mock_transport(
        monkeypatch,
        lambda req: httpx.Response(302, headers={"location": "/again"}),
    )
    with pytest.raises(net.FetchError, match="too many redirects"):
        asyncio.run(
            net.safe_fetch("https://example.com/", timeout_ms=2000, max_bytes=100)
        )
    assert len(lookups) == net._MAX_REDIRECTS + 1  # first URL + every hop


def test_redirect_without_location_is_an_error(monkeypatch):
    _dns(monkeypatch, lambda host: PUBLIC)
    _use_mock_transport(monkeypatch, lambda req: httpx.Response(302))
    with pytest.raises(net.FetchError, match="Location"):
        asyncio.run(
            net.safe_fetch("https://example.com/", timeout_ms=2000, max_bytes=100)
        )


# ---------------------------------------------------------------------------
# (3) body cap: streamed, aborted early
# ---------------------------------------------------------------------------


class _Firehose(httpx.AsyncByteStream):
    """1 GB if anyone reads it to the end. Counts the chunks pulled."""

    def __init__(self):
        self.pulled = 0

    async def __aiter__(self):
        for _ in range(1_000_000):
            self.pulled += 1
            yield b"x" * 1024


def test_oversized_body_is_aborted_while_streaming(monkeypatch):
    _dns(monkeypatch, lambda host: PUBLIC)
    hose = _Firehose()
    _use_mock_transport(monkeypatch, lambda req: httpx.Response(200, stream=hose))
    with pytest.raises(net.FetchError, match="exceeds 4096 bytes"):
        asyncio.run(
            net.safe_fetch("https://example.com/big", timeout_ms=2000, max_bytes=4096)
        )
    # The cap is 4 KB and the hose yields 1 KB chunks: the fifth chunk is the
    # first one over, and reading stops there. The old code would have pulled
    # all 1,000,000 chunks into memory before comparing.
    assert hose.pulled == 5


def test_declared_oversize_is_refused_before_reading_the_body(monkeypatch):
    _dns(monkeypatch, lambda host: PUBLIC)
    hose = _Firehose()
    _use_mock_transport(
        monkeypatch,
        lambda req: httpx.Response(
            200, headers={"content-length": "999999999"}, stream=hose
        ),
    )
    with pytest.raises(net.FetchError, match="declares 999999999 bytes"):
        asyncio.run(
            net.safe_fetch("https://example.com/big", timeout_ms=2000, max_bytes=4096)
        )
    assert hose.pulled == 0


def test_body_exactly_at_the_cap_is_accepted(monkeypatch):
    _dns(monkeypatch, lambda host: PUBLIC)
    _use_mock_transport(
        monkeypatch, lambda req: httpx.Response(200, content=b"x" * 4096)
    )
    res = asyncio.run(
        net.safe_fetch("https://example.com/", timeout_ms=2000, max_bytes=4096)
    )
    assert len(res.body) == 4096


def test_http_error_status_keeps_the_status_for_robots(monkeypatch):
    _dns(monkeypatch, lambda host: PUBLIC)
    inner = _RecordingBackend([_http(500, b"boom")])
    _use_backend(monkeypatch, inner)
    with pytest.raises(net.FetchError) as ei:
        asyncio.run(
            net.safe_fetch("https://example.com/robots.txt", timeout_ms=2000, max_bytes=100)
        )
    assert ei.value.status == 500


# ---------------------------------------------------------------------------
# Real TLS on loopback: dial by pinned IP, verify the certificate against the
# URL's host. This is the property the whole pinning design rests on, so it
# is proven with actual sockets and OpenSSL rather than mocks.
# ---------------------------------------------------------------------------


@pytest.fixture
def loopback_cert(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("openssl binary not available")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "ec",
            "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "2",
            "-subj", "/CN=pinned.test",
            "-addext", "subjectAltName=DNS:pinned.test",
        ],
        check=True,
        capture_output=True,
    )
    return str(key), str(cert)


def _run_tls_fetch(monkeypatch, loopback_cert, hostname):
    """Start a TLS listener on 127.0.0.1, pin `hostname` to it, fetch.
    Returns (result_or_exception, sni_names_seen_by_server)."""
    key, cert = loopback_cert
    # httpx honours SSL_CERT_FILE, so the throwaway cert is trusted without
    # any test-only hook in the production code path.
    monkeypatch.setenv("SSL_CERT_FILE", cert)
    sni_seen = []

    async def main():
        server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_ctx.load_cert_chain(cert, key)
        server_ctx.sni_callback = lambda sock, name, ctx: sni_seen.append(name)

        async def handler(reader, writer):
            try:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                    b"Content-Length: 6\r\nConnection: close\r\n\r\npinned"
                )
                await writer.drain()
            except Exception:  # noqa: BLE001 — a failed handshake ends here
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]
        # Bind first, THEN fake DNS (the bind itself resolves "127.0.0.1").
        _dns(monkeypatch, lambda host: "127.0.0.1")
        # Loopback is blocked in production; allow it for this listener only.
        monkeypatch.setattr(net, "_ip_is_blocked", lambda ip: False)
        try:
            return await net.safe_fetch(
                f"https://{hostname}:{port}/", timeout_ms=5000, max_bytes=100
            )
        except net.FetchError as exc:
            return exc
        finally:
            server.close()

    return asyncio.run(main()), sni_seen


def test_tls_dials_the_pinned_ip_and_verifies_the_url_host(monkeypatch, loopback_cert):
    res, sni_seen = _run_tls_fetch(monkeypatch, loopback_cert, "pinned.test")
    assert isinstance(res, net.FetchResult), res
    assert res.body == b"pinned"
    # Every packet went to 127.0.0.1, yet the TLS layer asked for — and
    # verified — the URL's hostname.
    assert sni_seen == ["pinned.test"]


def test_tls_hostname_verification_still_rejects_a_wrong_name(monkeypatch, loopback_cert):
    """Same listener, same pinned IP, a URL whose host the certificate does
    not name: the handshake must fail. Proves pinning did not weaken
    verification into 'any cert for the IP'."""
    res, sni_seen = _run_tls_fetch(monkeypatch, loopback_cert, "other.test")
    assert isinstance(res, net.FetchError), res
    assert "certificate" in str(res).lower()
    assert sni_seen == ["other.test"]


def test_ssl_context_is_cached_per_trust_env(monkeypatch, tmp_path):
    """One certifi load per process (11.7 ms measured), but a different CA
    bundle gets its own context rather than the cached one."""
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    a = net._ssl_context()
    assert net._ssl_context() is a
    assert a.check_hostname is True and a.verify_mode == ssl.CERT_REQUIRED
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))  # an (empty) CA dir
    b = net._ssl_context()
    assert b is not a and b.check_hostname is True
