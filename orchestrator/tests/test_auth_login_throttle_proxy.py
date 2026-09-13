"""The login throttle and the client address behind the frontend proxy (2026-09-13).

WHY THIS FILE EXISTS. Browsers never reach the orchestrator themselves: the
frontend's BFF relays `/auth/login`, and unless the deployment names a header
its ingress writes, it forwards no client address at all. Every sign-in then
arrives from ONE socket peer, the frontend container, so the per-address half
of the login throttle (`ip:<address>`) counted every person's failures on the
same key — a handful of bad logins for made-up emails locked everybody out.

At the same time the address was read from the first `X-Forwarded-For` entry
from ANY peer while `AUTH_TRUST_PROXY_HEADERS` was on, so a caller that reached
the orchestrator directly could write the header itself: a fresh value per
attempt meant it was never counted, and a chosen value meant its failures were
counted against somebody else.

THE RULE these tests pin (authn/sessions.py `client_origin`):

* a forwarded address is believed only when the socket peer is one of the
  deployment's own proxies (`AUTH_TRUSTED_PROXIES`, else the list `/v1` already
  uses, `PUBLIC_API_TRUSTED_PROXIES`), read from the right the way
  `apiplatform/resolver.client_address` reads it;
* a request from our proxy that names no client carries no per-address
  identity, so it gets no per-address lock; the per-email lock still applies;
* every other peer is its own address, whatever header it sends;
* the frontend is also recognised by its compose service name, so the
  lockout stays closed where neither list was ever set (authn/proxy_trust.py);
* a network's gateway, which is what a host process calling a published port
  appears as, is never a proxy, even inside a trusted CIDR.

Every request here goes through the real HTTP login, with the socket peer set
per client, so the proxy / direct distinction is exercised as production sees it.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import struct
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db
from app.apiplatform import resolver
from app.authn import proxy_trust, sessions
from app.config import settings
from app.main import app

PASSWORD = "correct-horse-battery"  # login_client's default

#: The real lookup, kept before the autouse fixture swaps in a fake.
_REAL_RESOLVE_HOST = proxy_trust.resolve_host

#: The frontend's Docker network in these tests, and one address inside it.
PROXY_NET = "10.9.0.0/16"
PROXY_PEER = ("10.9.0.6", 41000)
#: A caller that reached the orchestrator's port directly.
DIRECT_PEER = ("192.168.1.50", 40000)


@pytest.fixture(autouse=True)
def isolated_proxy_trust(monkeypatch):
    """No test here depends on this machine's DNS or routing table: the
    frontend name resolves to nothing and there are no gateways unless a test
    says otherwise, and no cached answer leaks between tests."""
    proxy_trust.reset_caches()
    monkeypatch.delenv("AUTH_FRONTEND_HOST", raising=False)
    if hasattr(settings, "auth_frontend_host"):
        monkeypatch.setattr(settings, "auth_frontend_host", proxy_trust.FRONTEND_HOST_DEFAULT)
    monkeypatch.setattr(proxy_trust, "resolve_host", lambda host: frozenset())
    monkeypatch.setattr(proxy_trust, "read_local_gateways", lambda: frozenset())
    yield
    proxy_trust.reset_caches()


@pytest.fixture
def behind_proxy(monkeypatch):
    """Production's shape: proxy headers on, the frontend network trusted."""
    monkeypatch.delenv("AUTH_TRUSTED_PROXIES", raising=False)
    monkeypatch.setattr(settings, "auth_trust_proxy_headers", True)
    monkeypatch.setattr(settings, "public_api_trusted_proxies", (PROXY_NET,))
    if hasattr(settings, "auth_trusted_proxies"):
        monkeypatch.setattr(settings, "auth_trusted_proxies", ())


def _login(client: TestClient, email: str, password: str, *, xff: str | None = None):
    headers = {"X-Forwarded-For": xff} if xff is not None else {}
    return client.post(
        "/auth/login", json={"email": email, "password": password}, headers=headers
    )


def _nobody() -> str:
    return f"nobody-{uuid.uuid4().hex[:10]}@example.invalid"


def _audit_ips(action: str) -> list[str]:
    with db.connection() as con:
        rows = con.execute(
            "SELECT ip FROM audit_events WHERE action = %s ORDER BY id", (action,)
        ).fetchall()
    return [str(row["ip"]) for row in rows]


def _throttle_keys() -> set[str]:
    with db.connection() as con:
        rows = con.execute("SELECT key FROM login_throttle").fetchall()
    return {row["key"] for row in rows}


# ---------------------------------------------------------------------------
# 1. The shared proxy address is not a global lock
# ---------------------------------------------------------------------------


def test_failed_logins_for_other_emails_through_the_proxy_do_not_lock_out_a_valid_user(
    login_client, behind_proxy
):
    """The lockout: max_fails misses for DIFFERENT, non-existent emails, all
    relayed by the frontend with no client address, used to answer the next
    person's correct password with 429."""
    login_client("victim")
    with TestClient(app, client=PROXY_PEER) as internet:
        for _ in range(settings.auth_login_max_fails):
            assert _login(internet, _nobody(), "wrong").status_code == 401
        victim = _login(internet, "victim@test.local", PASSWORD)
        assert victim.status_code == 200, victim.text
        # Another full round past the threshold: still nobody else is locked.
        for _ in range(settings.auth_login_max_fails):
            assert _login(internet, _nobody(), "wrong").status_code == 401
        assert _login(internet, "victim@test.local", PASSWORD).status_code == 200
    assert not any(key.startswith("ip:") for key in _throttle_keys())


def test_the_proxy_is_not_a_global_lock_with_proxy_headers_off_either(
    login_client, behind_proxy, monkeypatch
):
    """A deployment that leaves AUTH_TRUST_PROXY_HEADERS off still relays every
    browser through the same frontend peer; the lock must not be shared there
    either."""
    monkeypatch.setattr(settings, "auth_trust_proxy_headers", False)
    login_client("victim")
    with TestClient(app, client=PROXY_PEER) as internet:
        for _ in range(settings.auth_login_max_fails + 1):
            assert _login(internet, _nobody(), "wrong").status_code == 401
        assert _login(internet, "victim@test.local", PASSWORD).status_code == 200


def test_the_per_email_lock_still_holds_through_the_proxy(login_client, behind_proxy):
    login_client("marge")
    login_client("homer")
    with TestClient(app, client=PROXY_PEER) as internet:
        for _ in range(settings.auth_login_max_fails):
            assert _login(internet, "marge@test.local", "wrong-every-time").status_code == 401
        locked = _login(internet, "marge@test.local", PASSWORD)
        # The lock is that email's, not the proxy's.
        other = _login(internet, "homer@test.local", PASSWORD)
    assert locked.status_code == 429
    assert "Too many attempts" in locked.json()["detail"]
    assert other.status_code == 200


def test_a_client_address_the_proxy_forwards_is_still_throttled_per_address(
    login_client, behind_proxy
):
    """When the ingress does name the client, each client keeps its own
    per-address lock — and only that client is locked."""
    login_client("victim")
    with TestClient(app, client=PROXY_PEER) as internet:
        for _ in range(settings.auth_login_max_fails):
            assert _login(internet, _nobody(), "wrong", xff="203.0.113.20").status_code == 401
        sprayer = _login(internet, "victim@test.local", PASSWORD, xff="203.0.113.20")
        someone_else = _login(internet, "victim@test.local", PASSWORD, xff="203.0.113.21")
    assert sprayer.status_code == 429
    assert someone_else.status_code == 200
    assert "ip:203.0.113.20" in _throttle_keys()


# ---------------------------------------------------------------------------
# 2. A direct caller cannot choose its address
# ---------------------------------------------------------------------------


def test_a_direct_caller_cannot_dodge_the_ip_lock_by_rotating_x_forwarded_for(
    login_client, behind_proxy
):
    login_client("sprayed")
    with TestClient(app, client=DIRECT_PEER) as lan:
        for i in range(settings.auth_login_max_fails):
            response = _login(lan, _nobody(), "Summer2026!", xff=f"203.0.113.{i + 1}")
            assert response.status_code == 401
        # A new header value and a new email: still the same caller, still locked.
        locked = _login(lan, "sprayed@test.local", PASSWORD, xff="203.0.113.250")
    assert locked.status_code == 429
    assert _throttle_keys() >= {f"ip:{DIRECT_PEER[0]}"}
    assert not any(key.startswith("ip:203.0.113.") for key in _throttle_keys())


def test_a_direct_caller_cannot_aim_the_ip_lock_at_someone_else(login_client, behind_proxy):
    login_client("victim")
    victim_address = "203.0.113.77"
    with TestClient(app, client=DIRECT_PEER) as lan:
        for _ in range(settings.auth_login_max_fails):
            assert _login(lan, _nobody(), "wrong", xff=victim_address).status_code == 401
    # The victim arrives through the real proxy, which names them correctly.
    with TestClient(app, client=PROXY_PEER) as internet:
        through_proxy = _login(internet, "victim@test.local", PASSWORD, xff=victim_address)
    # ...or straight from that address.
    with TestClient(app, client=(victim_address, 52000)) as direct:
        from_own_address = _login(direct, "victim@test.local", PASSWORD)
    assert through_proxy.status_code == 200
    assert from_own_address.status_code == 200
    assert f"ip:{victim_address}" not in _throttle_keys()


def test_a_direct_caller_cannot_escape_the_lock_by_claiming_to_be_the_proxy(
    login_client, behind_proxy
):
    """Naming an address inside the trusted network in the header does not make
    the caller the proxy: only the socket peer decides that."""
    login_client("victim")
    with TestClient(app, client=DIRECT_PEER) as lan:
        for _ in range(settings.auth_login_max_fails):
            assert _login(lan, _nobody(), "wrong", xff=PROXY_PEER[0]).status_code == 401
        assert _login(lan, "victim@test.local", PASSWORD, xff=PROXY_PEER[0]).status_code == 429


def test_the_audit_trail_records_the_peer_not_a_header_a_direct_caller_wrote(
    login_client, behind_proxy
):
    login_client("bob")
    with TestClient(app, client=DIRECT_PEER) as lan:
        assert _login(lan, "bob@test.local", "wrong", xff="198.51.100.9").status_code == 401
        assert _login(lan, "bob@test.local", PASSWORD, xff="198.51.100.9").status_code == 200
    assert _audit_ips("login_failure") == [DIRECT_PEER[0]]
    assert _audit_ips("login_success")[-1] == DIRECT_PEER[0]


def test_through_the_proxy_the_hop_it_appended_is_the_client_not_the_leftmost(
    login_client, behind_proxy
):
    """A proxy appends; the leftmost entry is whatever the client typed."""
    login_client("bob")
    with TestClient(app, client=PROXY_PEER) as internet:
        r = _login(internet, "bob@test.local", "wrong", xff="198.51.100.1, 203.0.113.31")
    assert r.status_code == 401
    assert _audit_ips("login_failure") == ["203.0.113.31"]
    assert "ip:203.0.113.31" in _throttle_keys()


def test_no_trusted_proxy_configured_means_no_forwarded_address_is_believed(
    login_client, monkeypatch, caplog
):
    """Empty lists trust nobody (fail closed), even with the header switch on."""
    monkeypatch.delenv("AUTH_TRUSTED_PROXIES", raising=False)
    monkeypatch.setattr(settings, "auth_trust_proxy_headers", True)
    monkeypatch.setattr(settings, "public_api_trusted_proxies", ())
    if hasattr(settings, "auth_trusted_proxies"):
        monkeypatch.setattr(settings, "auth_trusted_proxies", ())
    monkeypatch.setattr(sessions, "_warned_no_trusted_proxy", False)
    login_client("bob")
    with caplog.at_level(logging.WARNING, logger=sessions.log.name):
        with TestClient(app, client=PROXY_PEER) as caller:
            assert _login(caller, "bob@test.local", "wrong", xff="198.51.100.9").status_code == 401
            assert _login(caller, "bob@test.local", "wrong", xff="198.51.100.9").status_code == 401
    assert _audit_ips("login_failure") == [PROXY_PEER[0]] * 2
    warnings = [r for r in caplog.records if "AUTH_TRUST_PROXY_HEADERS is on" in r.getMessage()]
    assert len(warnings) == 1


# ---------------------------------------------------------------------------
# client_origin, directly
# ---------------------------------------------------------------------------


def _request(peer: str | None, xff: str | None = None):
    headers = {"x-forwarded-for": xff} if xff is not None else {}
    client = SimpleNamespace(host=peer) if peer is not None else None
    return SimpleNamespace(client=client, headers=headers)


def test_client_origin_classifies_every_shape(behind_proxy):
    origin = sessions.client_origin
    # Direct peers: the peer, whatever the header says.
    assert origin(_request("192.168.1.50", "203.0.113.1")) == sessions.ClientOrigin(
        ip="192.168.1.50", forwarded=False, via_trusted_proxy=False
    )
    # Our proxy, no client named: the proxy, flagged as shared.
    bare = origin(_request("10.9.0.6"))
    assert (bare.ip, bare.forwarded, bare.shared_proxy) == ("10.9.0.6", False, True)
    # Our proxy naming a client.
    named = origin(_request("10.9.0.6", "203.0.113.5"))
    assert (named.ip, named.forwarded, named.shared_proxy) == ("203.0.113.5", True, False)
    # Trusted hops are skipped from the right.
    chained = origin(_request("10.9.0.6", "203.0.113.5, 10.9.0.9"))
    assert (chained.ip, chained.forwarded) == ("203.0.113.5", True)
    # A hop we cannot parse: we could not tell, so the proxy, shared.
    garbled = origin(_request("10.9.0.6", "not-an-address"))
    assert (garbled.ip, garbled.shared_proxy) == ("10.9.0.6", True)
    # An IPv4-mapped IPv6 peer is the IPv4 address for matching.
    mapped = origin(_request("::ffff:10.9.0.6", "203.0.113.5"))
    assert (mapped.ip, mapped.forwarded) == ("203.0.113.5", True)
    # No socket peer at all.
    assert origin(_request(None, "203.0.113.5")).ip == ""
    # A non-address peer (the test transport) is never a proxy.
    assert origin(_request("testclient", "203.0.113.5")).ip == "testclient"


def test_client_origin_ignores_the_header_when_proxy_headers_are_off(behind_proxy, monkeypatch):
    monkeypatch.setattr(settings, "auth_trust_proxy_headers", False)
    got = sessions.client_origin(_request("10.9.0.6", "203.0.113.5"))
    assert (got.ip, got.forwarded, got.shared_proxy) == ("10.9.0.6", False, True)


def test_auth_trusted_proxies_overrides_the_v1_list(behind_proxy, monkeypatch):
    override = "172.30.0.0/24, bogus-entry"
    if hasattr(settings, "auth_trusted_proxies"):
        # Once config.py parses the variable itself, the attribute is the source.
        monkeypatch.setattr(
            settings,
            "auth_trusted_proxies",
            tuple(item.strip() for item in override.split(",") if item.strip()),
        )
    else:
        monkeypatch.setenv("AUTH_TRUSTED_PROXIES", override)
    # The /v1 list no longer applies to authn...
    assert not sessions.client_origin(_request("10.9.0.6", "203.0.113.5")).forwarded
    # ...the override does, and a malformed entry is skipped rather than fatal.
    got = sessions.client_origin(_request("172.30.0.4", "203.0.113.5"))
    assert (got.ip, got.forwarded) == ("203.0.113.5", True)


def test_client_meta_keeps_its_shape(behind_proxy):
    request = _request("192.168.1.50", "203.0.113.1")
    request.headers["user-agent"] = "pytest-agent"
    assert sessions.client_meta(request) == ("192.168.1.50", "pytest-agent")


@pytest.mark.parametrize(
    "peer, xff",
    [
        ("192.168.1.50", "203.0.113.1"),
        ("10.9.0.6", None),
        ("10.9.0.6", "203.0.113.5"),
        ("10.9.0.6", "198.51.100.1, 203.0.113.31"),
        ("10.9.0.6", "203.0.113.5, 10.9.0.9"),
        ("::ffff:10.9.0.6", "203.0.113.5"),
    ],
)
def test_authn_and_v1_agree_on_who_the_client_is(behind_proxy, peer, xff):
    """Sign-in and the `/v1` allowlist must not disagree about which caller a
    request came from: one reading the header the other ignores is how a
    spoofing bypass reappears on one surface. (Two differences are deliberate
    and not covered here: sign-in also recognises the frontend by name, and
    never treats a gateway address as a proxy.)"""
    expected = resolver.client_address(peer, xff, trusted_proxies=(PROXY_NET,))
    assert sessions.client_origin(_request(peer, xff)).ip == expected


# ---------------------------------------------------------------------------
# 3. No list configured: the frontend is recognised by name
# ---------------------------------------------------------------------------

#: What the container's DNS answers for the frontend service in these tests.
FRONTEND_BY_NAME = ("10.20.0.9", 43000)


@pytest.fixture
def no_list_configured(monkeypatch):
    """A deployment with the header switch on and BOTH trusted-proxy lists
    empty: the shape a fix that relies on configuration alone leaves open."""
    monkeypatch.delenv("AUTH_TRUSTED_PROXIES", raising=False)
    monkeypatch.setattr(settings, "auth_trust_proxy_headers", True)
    monkeypatch.setattr(settings, "public_api_trusted_proxies", ())
    if hasattr(settings, "auth_trusted_proxies"):
        monkeypatch.setattr(settings, "auth_trusted_proxies", ())


class _FakeDns:
    """A stand-in for the container resolver that records every lookup."""

    def __init__(self, answers: dict[str, str]):
        self.answers = dict(answers)
        self.lookups: list[str] = []

    def __call__(self, host: str):
        self.lookups.append(host)
        value = self.answers.get(host)
        return frozenset({ipaddress.ip_address(value)}) if value else frozenset()


def test_with_no_list_configured_the_frontend_is_recognised_by_name(
    login_client, no_list_configured, monkeypatch, caplog
):
    """The lockout must close without anyone remembering a setting: failed
    logins for made-up emails through the frontend, with no trusted-proxy
    list at all, leave a valid user signing in."""
    dns = _FakeDns({proxy_trust.FRONTEND_HOST_DEFAULT: FRONTEND_BY_NAME[0]})
    monkeypatch.setattr(proxy_trust, "resolve_host", dns)
    monkeypatch.setattr(sessions, "_warned_no_trusted_proxy", False)
    login_client("victim")
    with caplog.at_level(logging.WARNING, logger=sessions.log.name):
        with TestClient(app, client=FRONTEND_BY_NAME) as internet:
            for _ in range(settings.auth_login_max_fails):
                assert _login(internet, _nobody(), "wrong").status_code == 401
            first = _login(internet, "victim@test.local", PASSWORD)
            for _ in range(settings.auth_login_max_fails):
                assert _login(internet, _nobody(), "wrong").status_code == 401
            second = _login(internet, "victim@test.local", PASSWORD)
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert not any(key.startswith("ip:") for key in _throttle_keys())
    assert dns.lookups and set(dns.lookups) == {proxy_trust.FRONTEND_HOST_DEFAULT}
    assert not [r for r in caplog.records if "no trusted proxy is known" in r.getMessage()]
    assert proxy_trust.status()["sources"] == ["frontend_host"]


def test_the_frontend_by_name_is_still_the_only_proxy(login_client, no_list_configured, monkeypatch):
    """Recognising the frontend does not make anyone else a proxy: a direct
    caller rotating the header is still locked on its own address."""
    monkeypatch.setattr(
        proxy_trust, "resolve_host",
        _FakeDns({proxy_trust.FRONTEND_HOST_DEFAULT: FRONTEND_BY_NAME[0]}),
    )
    login_client("sprayed")
    with TestClient(app, client=DIRECT_PEER) as lan:
        for i in range(settings.auth_login_max_fails):
            assert _login(lan, _nobody(), "wrong", xff=f"203.0.113.{i + 1}").status_code == 401
        locked = _login(lan, "sprayed@test.local", PASSWORD, xff=FRONTEND_BY_NAME[0])
    assert locked.status_code == 429
    assert f"ip:{DIRECT_PEER[0]}" in _throttle_keys()


def test_a_frontend_that_moved_is_recognised_on_its_first_request(
    login_client, no_list_configured, monkeypatch
):
    """A recreated frontend comes back on a new address; waiting for the
    cached answer to expire would leave that address as a shared lock."""
    monkeypatch.setattr(proxy_trust, "FRONTEND_RETRY_S", 0.0)
    dns = _FakeDns({proxy_trust.FRONTEND_HOST_DEFAULT: "10.20.0.9"})
    monkeypatch.setattr(proxy_trust, "resolve_host", dns)
    login_client("victim")
    with TestClient(app, client=("10.20.0.9", 43000)) as before:
        assert _login(before, "victim@test.local", PASSWORD).status_code == 200
    dns.answers[proxy_trust.FRONTEND_HOST_DEFAULT] = "10.20.0.44"
    with TestClient(app, client=("10.20.0.44", 43001)) as after:
        for _ in range(settings.auth_login_max_fails):
            assert _login(after, _nobody(), "wrong").status_code == 401
        assert _login(after, "victim@test.local", PASSWORD).status_code == 200
    assert "ip:10.20.0.44" not in _throttle_keys()


def test_unrecognised_peers_do_not_become_a_stream_of_lookups(monkeypatch):
    dns = _FakeDns({})
    monkeypatch.setattr(proxy_trust, "resolve_host", dns)
    for last in range(2, 52):
        peer = ipaddress.ip_address(f"192.168.1.{last}")
        view = proxy_trust.view_for_peer(peer, wait=1.0)
        assert not proxy_trust.is_trusted(peer, view)
    assert len(dns.lookups) == 1


def test_recognition_by_name_can_be_turned_off_and_the_gap_is_reported(
    no_list_configured, monkeypatch
):
    """The empty shape is pinned as detectable: nothing recognised, and the
    status a health check reads says so."""
    monkeypatch.setenv("AUTH_FRONTEND_HOST", "")
    if hasattr(settings, "auth_frontend_host"):
        monkeypatch.setattr(settings, "auth_frontend_host", "")
    dns = _FakeDns({proxy_trust.FRONTEND_HOST_DEFAULT: FRONTEND_BY_NAME[0]})
    monkeypatch.setattr(proxy_trust, "resolve_host", dns)
    got = sessions.client_origin(_request(FRONTEND_BY_NAME[0]), resolve_wait=1.0)
    assert (got.via_trusted_proxy, got.shared_proxy) == (False, False)
    assert dns.lookups == []
    assert proxy_trust.status() == {
        "recognised": False, "sources": [], "frontend_host_enabled": False, "settled": True,
    }


def test_the_frontend_name_is_looked_up_as_an_absolute_name(monkeypatch):
    """A host search domain must never turn the service name into another
    machine, and only private, non-loopback answers can be a proxy."""
    asked = []

    def fake_getaddrinfo(name, port, *args, **kwargs):
        asked.append(name)
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 0))
            for addr in ("127.0.0.1", "1.1.1.1", "10.20.0.9")
        ] + [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", 0, 0, 2))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert _REAL_RESOLVE_HOST("frontend") == frozenset({ipaddress.ip_address("10.20.0.9")})
    assert asked == ["frontend."]
    assert _REAL_RESOLVE_HOST("frontend.") == frozenset({ipaddress.ip_address("10.20.0.9")})
    assert asked == ["frontend.", "frontend."]
    # Literals are judged the same way, without a lookup.
    assert _REAL_RESOLVE_HOST("127.0.0.1") == frozenset()
    assert _REAL_RESOLVE_HOST("1.1.1.1") == frozenset()
    assert len(asked) == 2


# ---------------------------------------------------------------------------
# 4. A network's gateway is never the proxy, even inside a trusted CIDR
# ---------------------------------------------------------------------------

#: The trusted network's gateway: what a host process calling a published
#: port shows up as.
GATEWAY_PEER = ("10.9.0.1", 44000)


@pytest.fixture
def wide_list_with_gateway(behind_proxy, monkeypatch):
    """A whole-network entry that also covers the network's gateway."""
    monkeypatch.setattr(
        proxy_trust, "read_local_gateways",
        lambda: frozenset({ipaddress.ip_address(GATEWAY_PEER[0])}),
    )


def test_a_gateway_caller_rotating_x_forwarded_for_is_locked_on_its_own_address(
    login_client, wide_list_with_gateway
):
    login_client("sprayed")
    with TestClient(app, client=GATEWAY_PEER) as host_local:
        for i in range(settings.auth_login_max_fails):
            response = _login(host_local, _nobody(), "Summer2026!", xff=f"203.0.113.{i + 1}")
            assert response.status_code == 401
        locked = _login(host_local, "sprayed@test.local", PASSWORD, xff="198.51.100.200")
    assert locked.status_code == 429
    assert f"ip:{GATEWAY_PEER[0]}" in _throttle_keys()
    assert not any(key.startswith("ip:203.0.113.") for key in _throttle_keys())
    assert set(_audit_ips("login_failure")) == {GATEWAY_PEER[0]}


def test_a_gateway_caller_cannot_aim_the_lock_at_someone_else(login_client, wide_list_with_gateway):
    login_client("victim")
    victim_address = "203.0.113.77"
    with TestClient(app, client=GATEWAY_PEER) as host_local:
        for _ in range(settings.auth_login_max_fails):
            assert _login(host_local, _nobody(), "wrong", xff=victim_address).status_code == 401
    with TestClient(app, client=PROXY_PEER) as internet:
        through_proxy = _login(internet, "victim@test.local", PASSWORD, xff=victim_address)
    assert through_proxy.status_code == 200, through_proxy.text
    assert f"ip:{victim_address}" not in _throttle_keys()


def test_a_gateway_is_excluded_from_cidrs_but_an_exact_entry_can_still_name_it():
    gateway = ipaddress.ip_address(GATEWAY_PEER[0])
    gateways = frozenset({gateway})

    def view(*entries):
        return proxy_trust.TrustView(
            networks=proxy_trust.parse_networks(tuple(entries)),
            frontend=frozenset(),
            gateways=gateways,
        )

    assert not proxy_trust.is_trusted(gateway, view(PROXY_NET))
    assert proxy_trust.is_trusted(ipaddress.ip_address(PROXY_PEER[0]), view(PROXY_NET))
    assert proxy_trust.is_trusted(gateway, view(GATEWAY_PEER[0]))
    # The network and broadcast addresses of a CIDR are nobody's peer either.
    assert not proxy_trust.is_trusted(ipaddress.ip_address("10.9.0.0"), view(PROXY_NET))
    assert not proxy_trust.is_trusted(ipaddress.ip_address("10.9.255.255"), view(PROXY_NET))
    # A gateway can never be recognised by name either.
    by_name = proxy_trust.TrustView(networks=(), frontend=gateways, gateways=gateways)
    assert not proxy_trust.is_trusted(gateway, by_name)


def _route_hex(address: str) -> str:
    """How /proc/net/route prints an IPv4 address (host byte order)."""
    return format(struct.unpack("=L", socket.inet_aton(address))[0], "08X")


def test_route_tables_yield_the_gateways_a_host_caller_can_appear_as():
    ipv4 = "\n".join(
        [
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT",
            f"eth0\t{_route_hex('0.0.0.0')}\t{_route_hex('10.9.0.1')}\t0003\t0\t0\t0\t{_route_hex('0.0.0.0')}\t0\t0\t0",
            f"eth0\t{_route_hex('10.9.0.0')}\t{_route_hex('0.0.0.0')}\t0001\t0\t0\t0\t{_route_hex('255.255.0.0')}\t0\t0\t0",
            f"eth1\t{_route_hex('10.30.0.0')}\t{_route_hex('0.0.0.0')}\t0001\t0\t0\t0\t{_route_hex('255.255.255.0')}\t0\t0\t0",
            f"lo\t{_route_hex('127.0.0.0')}\t{_route_hex('0.0.0.0')}\t0001\t0\t0\t0\t{_route_hex('255.0.0.0')}\t0\t0\t0",
            "garbage line",
        ]
    )
    assert {str(a) for a in proxy_trust.parse_ipv4_routes(ipv4)} == {
        "10.9.0.1", "10.9.0.0", "10.9.255.255", "10.30.0.0", "10.30.0.1", "10.30.0.255",
    }
    zero = "0" * 32
    ipv6 = "\n".join(
        [
            # default route via a next hop
            f"{zero} 00 {zero} 00 fd000000000000000000000000000001 00000400 00000001 00000000 00000003 eth0",
            # a connected /64
            f"fd000000000000010000000000000000 40 {zero} 00 {zero} 00000100 00000001 00000000 00000001 eth0",
            # a reject route and loopback are ignored
            f"{zero} 00 {zero} 00 {zero} ffffffff 00000001 00000000 00200200 lo",
            f"fd000000000000020000000000000000 40 {zero} 00 {zero} ffffffff 00000001 00000000 00000200 eth1",
        ]
    )
    assert {str(a) for a in proxy_trust.parse_ipv6_routes(ipv6)} == {
        "fd00::1", "fd00:0:0:1::", "fd00:0:0:1::1",
    }


def test_trust_status_names_sources_but_no_addresses(behind_proxy, monkeypatch):
    monkeypatch.setattr(
        proxy_trust, "resolve_host",
        _FakeDns({proxy_trust.FRONTEND_HOST_DEFAULT: FRONTEND_BY_NAME[0]}),
    )
    proxy_trust.view_for_peer(ipaddress.ip_address(FRONTEND_BY_NAME[0]), wait=1.0)
    status = proxy_trust.status()
    assert status["recognised"] is True
    assert status["sources"] == ["configured", "frontend_host"]
    assert FRONTEND_BY_NAME[0] not in repr(status) and "10.9." not in repr(status)
