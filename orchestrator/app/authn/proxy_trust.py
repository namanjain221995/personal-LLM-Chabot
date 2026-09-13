"""Which socket peers are this deployment's own proxies (2026-09-13).

Sign-in, sessions and audit need one answer to "did this request come
through our frontend, or straight at the orchestrator?". It decides two
things (authn/sessions.py `client_origin`): whether `X-Forwarded-For` is
believed at all, and whether the peer address is one shared by everyone
behind the proxy, which must never become a per-address login lock.

WHY IT IS MORE THAN A CONFIGURED LIST (2026-09-13, security review).

* A list the operator has to remember is a fix that silently does nothing
  where the list was never set: the frontend is then an unknown peer, and the
  per-address lock keyed on its address is shared by every person signing in.
  So the frontend is ALSO recognised by name: `AUTH_FRONTEND_HOST` (default
  `frontend`, the compose service) is resolved through the container's DNS and
  its addresses count as a trusted proxy, next to whatever the lists name.
  The name is looked up as an absolute name (trailing dot), so a host search
  domain can never turn it into some other machine, and only private,
  non-loopback answers are kept.
* A trusted CIDR that covers a whole container network also covers that
  network's gateway, and a process on the host that connects to a published
  port arrives FROM the gateway. Such a caller must not count as our proxy:
  it could then name any address it likes. The container's gateways (read
  from the kernel's routing table) are therefore excluded from every CIDR
  match; only an exact single-address entry can still name one on purpose.

Nothing here raises or blocks the event loop: lookups run on a short-lived
daemon thread, at most one at a time, and callers on the event loop read the
last answer. The login path, which already runs in a worker thread, waits a
bounded moment for a lookup it needs.
"""
from __future__ import annotations

import functools
import ipaddress
import logging
import os
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from ..config import settings

log = logging.getLogger(__name__)

#: The compose service name of the frontend (BFF) container.
FRONTEND_HOST_DEFAULT = "frontend"
#: How long a lookup answer is used before it is refreshed in the background.
FRONTEND_TTL_S = 30.0
#: The shortest gap between two lookups triggered by an unrecognised peer: a
#: recreated frontend comes back with a new address and should be recognised
#: on its first request, but a stream of direct callers must not become a
#: stream of DNS queries.
FRONTEND_RETRY_S = 2.0
#: How long the (threaded) login path waits for a lookup it triggered.
RESOLVE_WAIT_S = 1.0
#: How long the routing-table read is reused. Networks attach rarely.
GATEWAY_TTL_S = 30.0

_RTF_GATEWAY = 0x0002
_RTF_REJECT = 0x0200


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_address(value: Any) -> Optional[Any]:
    """An ip_address, or None. An IPv4-mapped IPv6 value is the IPv4 address:
    a dual-stack socket reports IPv4 peers that way, and compared as v6 it
    would never match a v4 entry."""
    try:
        address = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def configured_entries() -> Tuple[str, ...]:
    """The operator's list: `AUTH_TRUSTED_PROXIES` when set (comma list of
    addresses or CIDRs, parsed the way config.py parses
    `PUBLIC_API_TRUSTED_PROXIES`), otherwise the list `/v1` already uses."""
    configured = getattr(settings, "auth_trusted_proxies", None)
    if configured is None:
        configured = tuple(
            item.strip()
            for item in os.environ.get("AUTH_TRUSTED_PROXIES", "").split(",")
            if item.strip()
        )
    if not configured:
        configured = getattr(settings, "public_api_trusted_proxies", ()) or ()
    return tuple(str(item) for item in configured)


@functools.lru_cache(maxsize=16)
def parse_networks(entries: Tuple[str, ...]) -> Tuple[Any, ...]:
    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry.strip(), strict=False))
        except ValueError:
            log.warning("ignoring malformed trusted proxy entry %r", entry)
    return tuple(networks)


def frontend_host() -> str:
    """`AUTH_FRONTEND_HOST`; set it empty to turn recognition by name off."""
    configured = getattr(settings, "auth_frontend_host", None)
    if configured is None:
        configured = os.environ.get("AUTH_FRONTEND_HOST", FRONTEND_HOST_DEFAULT)
    return str(configured or "").strip()


def _acceptable_proxy_address(address: Any) -> bool:
    """A container network address. Loopback is refused because it would
    make every local process "the frontend"; a public answer is refused
    because no container network the frontend sits on is public."""
    return bool(
        address.is_private
        and not address.is_loopback
        and not address.is_unspecified
        and not address.is_link_local
        and not address.is_multicast
    )


def resolve_host(host: str) -> FrozenSet[Any]:
    """The acceptable addresses `host` resolves to right now (empty on any
    failure: an unresolvable name recognises nobody)."""
    literal = parse_address(host)
    if literal is not None:
        return frozenset({literal}) if _acceptable_proxy_address(literal) else frozenset()
    name = host if host.endswith(".") else host + "."
    try:
        infos = socket.getaddrinfo(name, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError, ValueError):
        return frozenset()
    found = set()
    for info in infos:
        address = parse_address(str(info[4][0]).split("%", 1)[0])
        if address is not None and _acceptable_proxy_address(address):
            found.add(address)
    return frozenset(found)


def parse_ipv4_routes(text: str) -> FrozenSet[Any]:
    """Gateway-shaped addresses from `/proc/net/route` text.

    Every next hop of a gateway route, plus, for every directly connected
    subnet, its network address, broadcast address and first host (where a
    container network's gateway sits unless its IPAM says otherwise; that is
    also the source address a host process shows up with).
    """
    found = set()
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[0] == "lo":
            continue
        try:
            dest, gateway, mask = (
                ipaddress.IPv4Address(struct.pack("=L", int(fields[i], 16)))
                for i in (1, 2, 7)
            )
            flags = int(fields[3], 16)
        except (ValueError, struct.error):
            continue
        if flags & _RTF_GATEWAY and int(gateway):
            found.add(gateway)
        elif not int(gateway) and int(mask):
            try:
                network = ipaddress.IPv4Network(f"{dest}/{mask}", strict=False)
            except ValueError:
                continue
            if network.prefixlen <= 30:
                found.update(
                    (network.network_address, network.network_address + 1,
                     network.broadcast_address)
                )
    return frozenset(found)


def parse_ipv6_routes(text: str) -> FrozenSet[Any]:
    """The same for `/proc/net/ipv6_route` (next hops, and the subnet-router
    and first-host addresses of connected prefixes)."""
    found = set()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or fields[9] == "lo":
            continue
        try:
            dest = ipaddress.IPv6Address(bytes.fromhex(fields[0]))
            prefixlen = int(fields[1], 16)
            next_hop = ipaddress.IPv6Address(bytes.fromhex(fields[4]))
            flags = int(fields[8], 16)
        except ValueError:
            continue
        if flags & _RTF_REJECT:
            continue
        if int(next_hop):
            found.add(next_hop)
        elif 0 < prefixlen <= 126:
            network = ipaddress.IPv6Network((dest, prefixlen), strict=False)
            found.update((network.network_address, network.network_address + 1))
    return frozenset(found)


def read_local_gateways() -> FrozenSet[Any]:
    found: set = set()
    for path, parser in (
        ("/proc/net/route", parse_ipv4_routes),
        ("/proc/net/ipv6_route", parse_ipv6_routes),
    ):
        try:
            with open(path, encoding="ascii", errors="replace") as handle:
                found.update(parser(handle.read()))
        except OSError:
            continue
    return frozenset(found)


# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------


class _Gateways:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: FrozenSet[Any] = frozenset()
        self._read_at: Optional[float] = None

    def get(self) -> FrozenSet[Any]:
        with self._lock:
            now = time.monotonic()
            if self._read_at is None or now - self._read_at >= GATEWAY_TTL_S:
                self._value = read_local_gateways()
                self._read_at = now
            return self._value

    def reset(self) -> None:
        with self._lock:
            self._value, self._read_at = frozenset(), None


class _FrontendAddresses:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._host: Optional[str] = None
        self._addresses: FrozenSet[Any] = frozenset()
        self._completed_at: Optional[float] = None
        self._started_at = float("-inf")
        self._inflight = False

    def get(
        self, host: str, *, unrecognised_peer: bool, wait: float
    ) -> Tuple[FrozenSet[Any], bool]:
        """(addresses, looked_up): looked_up is False until one lookup of
        `host` has finished, so "no addresses" is not yet an answer."""
        if not host:
            return frozenset(), True
        with self._lock:
            if host != self._host:
                self._host = host
                self._addresses = frozenset()
                self._completed_at = None
                self._started_at = float("-inf")
            now = time.monotonic()
            stale = self._completed_at is None or now - self._completed_at >= FRONTEND_TTL_S
            retry = unrecognised_peer and now - self._started_at >= FRONTEND_RETRY_S
            if (stale or retry) and not self._inflight:
                self._inflight = True
                self._started_at = now
                threading.Thread(
                    target=self._lookup, args=(host,), name="authn-frontend-lookup",
                    daemon=True,
                ).start()
            if wait > 0 and self._inflight and (
                self._completed_at is None or unrecognised_peer
            ):
                self._settled.wait_for(lambda: not self._inflight, timeout=wait)
            return self._addresses, self._completed_at is not None

    def _lookup(self, host: str) -> None:
        try:
            addresses = resolve_host(host)
        except Exception:  # noqa: BLE001 - a lookup failure recognises nobody
            log.exception("looking up AUTH_FRONTEND_HOST %r failed", host)
            addresses = frozenset()
        with self._lock:
            if host == self._host:
                if addresses != self._addresses:
                    log.info(
                        "sign-in proxy recognition: AUTH_FRONTEND_HOST %r now "
                        "resolves to %d usable address(es)", host, len(addresses),
                    )
                self._addresses = addresses
                self._completed_at = time.monotonic()
            self._inflight = False
            self._settled.notify_all()

    def reset(self) -> None:
        with self._lock:
            self._host = None
            self._addresses = frozenset()
            self._completed_at = None
            self._started_at = float("-inf")


_gateways = _Gateways()
_frontend = _FrontendAddresses()


def local_gateways() -> FrozenSet[Any]:
    """The addresses a host-local caller can appear as (cached)."""
    return _gateways.get()


def reset_caches() -> None:
    """Forget cached lookups (tests, or after a configuration change)."""
    _gateways.reset()
    _frontend.reset()
    parse_networks.cache_clear()


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustView:
    networks: Tuple[Any, ...]
    frontend: FrozenSet[Any]
    gateways: FrozenSet[Any]
    #: False until the frontend name has been looked up once.
    settled: bool = True

    @property
    def empty(self) -> bool:
        return not self.networks and not self.frontend


def is_trusted(address: Any, view: TrustView) -> bool:
    """Is `address` one of our own proxies under `view`?"""
    if address in view.gateways:
        # Only an exact single-address entry may name a gateway on purpose.
        return any(
            network.num_addresses == 1
            and network.version == address.version
            and address in network
            for network in view.networks
        )
    if address in view.frontend:
        return True
    for network in view.networks:
        if network.version != address.version or address not in network:
            continue
        if network.num_addresses > 2 and address in (
            network.network_address, network.broadcast_address
        ):
            continue
        return True
    return False


def _view(*, unrecognised_peer: bool, wait: float) -> TrustView:
    frontend, looked_up = _frontend.get(
        frontend_host(), unrecognised_peer=unrecognised_peer, wait=wait
    )
    return TrustView(
        networks=parse_networks(configured_entries()),
        frontend=frontend,
        gateways=local_gateways(),
        settled=looked_up,
    )


def view_for_peer(peer: Any, *, wait: float = 0.0) -> TrustView:
    """The trust view to classify `peer` with.

    A private peer the current view does not recognise may be a frontend
    that was just recreated with a new address, so it prompts a (rate-limited)
    fresh lookup, waited on for up to `wait` seconds. Pass `wait` only from a
    worker thread, never from the event loop.
    """
    view = _view(unrecognised_peer=False, wait=wait)
    if (
        frontend_host()
        and not is_trusted(peer, view)
        and peer not in view.gateways
        and _acceptable_proxy_address(peer)
    ):
        view = _view(unrecognised_peer=True, wait=wait)
    return view


def status() -> Dict[str, Any]:
    """Whether any proxy is recognised, for a health or startup check. Holds
    no addresses on purpose: health output can reach people who should not
    learn the network layout from it."""
    view = _view(unrecognised_peer=False, wait=0.0)
    sources: List[str] = []
    if view.networks:
        sources.append("configured")
    if view.frontend:
        sources.append("frontend_host")
    return {
        "recognised": bool(sources),
        "sources": sources,
        "frontend_host_enabled": bool(frontend_host()),
        "settled": view.settled,
    }
