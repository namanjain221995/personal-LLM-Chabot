#!/usr/bin/env python3
"""Can the unauthenticated main engine API be reached from outside the cluster?

WHY (audit F065, 2026-09-12; reshaped under owner option A, 2026-09-13)
-----------------------------------------------------------------------
The raw vLLM OpenAI server has no authentication. Anyone who can reach its
port bypasses every `/v1` key, quota and usage record the orchestrator
enforces. The audit found it on every interface of the production head, the
office LAN and the tailnet included, and found the `verify` job blind to it.

The first version of this gate failed on ANY wildcard listener. Under option A
that is the wrong question: the cluster head listens on every interface ON
PURPOSE, because its callers sit on loopback, the docker bridges and the RoCE
cluster fabric at once (the worker's healthcheck and a second tenant dial the
head over the fabric, and vLLM takes exactly one --host). A narrower bind takes
the two-node engine down on its next recreate. The exposure is closed instead
by the host packet filter, `scripts/host-guard.sh`, which filters by ingress
interface. A gate that could only go green on the outage bind was red on every
deploy whether or not the filter was in place, so it proved nothing.

So the gate now answers the question that matters, by BEHAVIOUR:

  resolve  reads CLUSTER_API_BIND_ADDRESS and VLLM_PORT from the deploy root's
           `.runtime/generated.env` (127.0.0.1 and 8000 when absent) and hands
           the probe URL to the later steps. Unchanged.

  check    1. reads the host's real listening sockets (`ss -ltnH`). A listener
              on the engine port bound to a specific address that is neither
              loopback nor the configured one FAILS, as before. Nothing
              listening FAILS, as before.
           2. a wildcard listener (0.0.0.0, ::, *, and the IPv4-mapped
              ::ffff:0.0.0.0, which an IPv6 socket uses to take every IPv4
              address), or a configuration that asks for one, is not a failure
              by itself -- it is the approved shape -- but it makes the
              OUTSIDE PROOF mandatory:
                * the head's addresses and links are read from the kernel
                  (`ip -j addr`, `ip -j route show table all default`), never
                  from a list. The cluster fabric address is CLUSTER_HEAD_IP (a
                  second rail, CLUSTER_HEAD_IP_2, is also fabric), and it is
                  only trusted as fabric if its interface looks like a cluster
                  link: no default route leaves through it and the paired
                  CLUSTER_WORKER_IP (_2) is on its subnet. A docker bridge is
                  docker0 or br-<12 hex> with no member but veth* and no
                  default route; a veth* only exempts its link-local addresses.
                  Every other global address is "outside", and a head with no
                  outside address at all is not trusted (it reaches the network
                  somehow, so the classification ate that address);
                * from the WORKER (ssh with BatchMode and
                  StrictHostKeyChecking=yes, CLUSTER_WORKER_SSH), a TCP
                  connect to the fabric address on the engine port MUST
                  succeed: that proves the probe path works;
                * a TCP connect to EVERY other global address of the head on
                  the engine port MUST fail, and a connect to the same address
                  on a CONTROL port the host filter never judges (ssh, 22)
                  MUST succeed. Without the control connect a timeout from a
                  dead path (the worker off the tailnet, a route gone) would be
                  indistinguishable from the filter's drop, and the gate would
                  pass on an address it never reached.
              A probe from the head itself proves nothing (local traffic
              enters on `lo`, which the filter accepts), so none is made; the
              worker also refuses to probe an address it owns itself.
           3. anything that makes the proof impossible -- no generated.env, not
              clustered, no worker, ssh failing, python3 missing on the worker,
              the fabric connect failing, a fabric address that does not look
              like a cluster link, a link-local address a wildcard covers
              (this gate does not probe link-local addresses), a control
              connect failing, output from the worker that cannot be read --
              is a FAILURE with the reason, never a pass. There is no advisory
              mode and no switch that turns this off.

PUBLIC LOGS. This repository's Actions logs and step summaries are public. The
check prints roles, counts, interface CLASSES and address families only: never
an address, a hostname, an interface name next to an address, or the ssh
target. Every line is scrubbed against the values it read before it is
printed, as a second line of defence behind the wording.

Usage:
    engine_bind.py resolve --generated-env PATH [--github-output FILE]
    engine_bind.py check   --generated-env PATH [--ss-output FILE]
"""
from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
from typing import Callable, Iterable, NamedTuple, Sequence

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

#: Every spelling `ss` and a config file use for "all interfaces".
WILDCARDS = frozenset({"0.0.0.0", "::", "[::]", "*", "::0", "0:0:0:0:0:0:0:0"})

#: Each TCP connect on the worker is bounded by this many seconds.
CONNECT_TIMEOUT_S = 3
#: The ssh options scripts/lib/cluster-common.sh uses (keys only, never a prompt),
#: plus a pinned host key check: an unknown or changed worker key fails the
#: gate whatever ~/.ssh/config says (`accept-new` there would otherwise let a
#: first-contact key through silently).
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=yes")
SSH_CONNECT_TIMEOUT_S = 8
#: A port the host packet filter never judges (host-guard.sh: "ssh ... untouched").
#: A connect to it proves the worker's path to that address works, so a failed
#: connect to the engine port on the same address is the filter, not the path.
CONTROL_PORT = 22
#: Exit status and marker of the remote wrapper when python3 is missing.
MISSING_TOOL_EXIT = 3
MISSING_TOOL_MARKER = "engine-bind-probe:missing-tool:python3"
PROBE_HEADER = {"probe": "engine-bind", "version": 1}

BLOCKED_RESULTS = frozenset({"refused", "timeout", "unreachable"})
ALL_RESULTS = BLOCKED_RESULTS | {"connected", "local", "error"}

SSH_TARGET_RE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*@[A-Za-z0-9._:\[\]-]+$")
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


@dataclasses.dataclass(frozen=True)
class Target:
    host: str
    port: int
    configured: bool  # False when the key was absent and the default applied

    @property
    def url(self) -> str:
        """Where to PROBE. A wildcard is not an address to connect to: probe
        loopback, which a wildcard listener answers; whether the wildcard is
        reachable from OUTSIDE is `check`'s question, not this URL's."""
        host = DEFAULT_HOST if is_wildcard(self.host) else self.host
        host = f"[{host}]" if ":" in host else host
        return f"http://{host}:{self.port}"


def read_env(path: pathlib.Path) -> dict[str, str]:
    """KEY=VALUE lines; comments, blanks and surrounding quotes stripped."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def resolve(env: dict[str, str]) -> Target:
    host = env.get("CLUSTER_API_BIND_ADDRESS", "").strip()
    port_text = env.get("VLLM_PORT", "").strip()
    try:
        port = int(port_text) if port_text else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT
    return Target(host=host or DEFAULT_HOST, port=port, configured=bool(host))


def _normalise(address: str) -> str:
    """The bare address: brackets and a `%device` scope removed, in either
    order `ss` and a config file write them (`[::]%eth0`, `[fe80::1%eth0]`).
    `0.0.0.0%eth0` is still every address on eth0."""
    address = address.strip()
    if address.startswith("["):
        address = address[1:].split("]", 1)[0]
    return address.split("%", 1)[0]


def _ip(address: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(_normalise(address))
    except ValueError:
        return None


def _mapped_v4(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    return getattr(ip, "ipv4_mapped", None)


def is_wildcard(address: str) -> bool:
    bare = _normalise(address)
    if address.strip() in WILDCARDS or bare in WILDCARDS:
        return True
    ip = _ip(bare)
    if ip is None:
        return False
    # An AF_INET6 socket bound to `::ffff:0.0.0.0` (or `::ffff:0:0`) accepts
    # every IPv4 address, and ipaddress does not call it unspecified.
    mapped = _mapped_v4(ip)
    return ip.is_unspecified or (mapped is not None and mapped.is_unspecified)


def wildcard_families(address: str) -> frozenset[int]:
    """Which address families a wildcard listener accepts. `0.0.0.0` and the
    IPv4-mapped `::ffff:0.0.0.0` are IPv4 only; `::`, `[::]` and `*` are
    counted as BOTH, because a dual-stack IPv6 socket takes IPv4 too and `ss`
    does not always say which it is."""
    ip = _ip(address)
    if ip is not None and (ip.version == 4 or _mapped_v4(ip) is not None):
        return frozenset({4})
    return frozenset({4, 6})


def is_loopback(address: str) -> bool:
    if _normalise(address) == "localhost":
        return True
    ip = _ip(address)
    if ip is None:
        return False
    # `::ffff:127.0.0.1` is loopback too, and ipaddress does not say so itself.
    mapped = _mapped_v4(ip)
    return ip.is_loopback or (mapped is not None and mapped.is_loopback)


def listeners(ss_text: str, port: int) -> list[str]:
    """Local addresses listening on `port`, from `ss -ltnH` output."""
    found = []
    for line in ss_text.splitlines():
        fields = line.split()
        # State Recv-Q Send-Q Local:Port Peer:Port [Process]
        if len(fields) < 5:
            continue
        local = fields[3]
        address, sep, local_port = local.rpartition(":")
        if not sep or local_port != str(port):
            continue
        found.append(address)
    return found


def _same_address(a: str, b: str) -> bool:
    a, b = _normalise(a), _normalise(b)
    if a == b:
        return True
    try:
        return ipaddress.ip_address(a) == ipaddress.ip_address(b)
    except ValueError:
        return False


# ---------------------------------------------------------------- inventory --

@dataclasses.dataclass(frozen=True)
class HostAddress:
    """One address of the head. `address` and `ifname` are NEVER printed."""

    ifname: str
    address: str
    family: int
    scope: str
    role: str  # loopback | fabric | fabric-secondary | fabric-link-local | bridge | link-local | outside
    iface_class: str  # lan | tailnet | vpn | wireless | cluster fabric link | docker bridge | host bridge | loopback | other


class AddrEntry(NamedTuple):
    ifname: str
    family: int
    address: str
    scope: str
    prefixlen: int | None


def _ip_links(text: str) -> list[dict]:
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("`ip -j addr` did not return a list")
    return [link for link in data if isinstance(link, dict)]


def parse_ip_addr_json(text: str) -> list[AddrEntry]:
    """Every address in `ip -j addr`, with its interface, scope and prefix."""
    out = []
    for link in _ip_links(text):
        ifname = str(link.get("ifname", ""))
        for info in link.get("addr_info") or []:
            if not isinstance(info, dict) or "local" not in info:
                continue
            family = {"inet": 4, "inet6": 6}.get(info.get("family"))
            if family is None:
                continue
            prefixlen = info.get("prefixlen")
            prefixlen = prefixlen if isinstance(prefixlen, int) and not isinstance(prefixlen, bool) else None
            out.append(AddrEntry(ifname, family, str(info["local"]), str(info.get("scope", "global")), prefixlen))
    return out


def parse_link_masters(text: str) -> dict[str, str]:
    """ifname -> the device it is enslaved to ("" for none), for EVERY link in
    `ip -j addr`, including the ones that carry no address (a NIC enslaved to
    a bridge usually has none)."""
    return {str(link.get("ifname", "")): str(link.get("master") or "") for link in _ip_links(text)}


#: The names docker gives its bridges: the default one and a compose network's.
DOCKER_BRIDGE_RE = re.compile(r"^(?:docker0|br-[0-9a-f]{12})$")


def docker_bridges(masters: dict[str, str], ifnames: Iterable[str], default_devs: Iterable[str]) -> frozenset[str]:
    """The interfaces that really are docker bridges: a docker-shaped name, no
    default route leaving through it, and no member but veth* (a bridge that
    enslaves a NIC -- the netplan/libvirt LAN bridge -- is the LAN, whatever
    it is called)."""
    defaults = set(default_devs)
    names = {n for n in set(ifnames) | set(masters) if DOCKER_BRIDGE_RE.match(n)}
    return frozenset(
        n for n in names
        if n not in defaults
        and not any(master == n and not member.startswith("veth") for member, master in masters.items())
    )


def interface_class(ifname: str, fabric_ifaces: Iterable[str], docker: Iterable[str] = ()) -> str:
    if ifname in set(fabric_ifaces):
        return "cluster fabric link"
    if ifname in set(docker) or ifname.startswith("veth"):
        return "docker bridge"
    if ifname.startswith(("br", "virbr")):
        return "host bridge"
    if ifname == "lo":
        return "loopback"
    if ifname.startswith("tailscale"):
        return "tailnet"
    if ifname.startswith(("wg", "tun", "tap", "ppp", "zt")):
        return "vpn"
    if ifname.startswith("wl"):
        return "wireless"
    if ifname.startswith(("en", "eth", "ib", "bond")):
        return "lan"
    return "other"


def classify_addresses(
    entries: Sequence[AddrEntry], fabric: str, fabric_secondary: str = "",
    masters: dict[str, str] | None = None, default_devs: Iterable[str] = (),
) -> list[HostAddress]:
    """`fabric` and `fabric_secondary` must already be validated as cluster
    links (`fabric_link_problem`); this only sorts addresses into roles."""
    fabric_ifaces = {
        e.ifname for e in entries
        if (fabric and _same_address(e.address, fabric))
        or (fabric_secondary and _same_address(e.address, fabric_secondary))
    }
    docker = docker_bridges(masters or {}, (e.ifname for e in entries), default_devs)
    out = []
    for e in entries:
        ip = _ip(e.address)
        if ip is None:
            continue
        link_local = e.scope == "link" or ip.is_link_local
        if ip.is_loopback:
            role = "loopback"
        elif fabric and _same_address(e.address, fabric):
            role = "fabric"
        elif fabric_secondary and _same_address(e.address, fabric_secondary):
            role = "fabric-secondary"
        elif e.ifname in docker or (link_local and e.ifname.startswith("veth")):
            # A veth's link-local address only answers its container end; a
            # GLOBAL address on a veth is not docker's doing, so it is probed.
            role = "bridge"
        elif link_local:
            # A link-local address is only reachable from the same link, so on
            # a fabric link it is inside the cluster.
            role = "fabric-link-local" if e.ifname in fabric_ifaces else "link-local"
        else:
            # A global address, wherever it sits (a LAN, the tailnet, a global
            # address parked on `lo`, on a fabric link or on a non-docker
            # bridge): an outsider may route to it.
            role = "outside"
        out.append(HostAddress(e.ifname, e.address, e.family, e.scope, role,
                               interface_class(e.ifname, fabric_ifaces, docker)))
    return out


def parse_default_route_devs(texts: Iterable[str]) -> frozenset[str]:
    """The devices a default route leaves through, from `ip -j [-6] route show
    table all default` (every table, multipath next hops included). Routes
    that forward nothing (unreachable, prohibit, blackhole, throw) are not
    a way out."""
    devs = set()
    for text in texts:
        data = json.loads(text) if text.strip() else []
        if not isinstance(data, list):
            raise ValueError("`ip -j route` did not return a list")
        for route in data:
            if not isinstance(route, dict) or route.get("type") in ("unreachable", "prohibit", "blackhole", "throw"):
                continue
            hops = [route] + [h for h in route.get("nexthops") or [] if isinstance(h, dict)]
            devs.update(str(h["dev"]) for h in hops if h.get("dev"))
    return frozenset(devs)


def fabric_link_problem(entries: Sequence[AddrEntry], head: str, worker: str,
                        default_devs: Iterable[str], rdma_devs: Iterable[str]) -> str | None:
    """Why the interface carrying `head` is NOT a cluster link to `worker`, or
    None when it looks like one (or `head` is not on this host at all, which
    the caller reports itself). generated.env is the thing under test, so its
    fabric address is only trusted when the kernel agrees: the interface is an
    RDMA (RoCE) link, no default route leaves through it, and the worker's
    address is on its subnet.

    The RDMA test closes the case the default-route test alone leaves open
    (re-review, 2026-09-13): a wired office-LAN port with no default route of
    its own — the default on another NIC — and the worker also on that LAN
    passes the other two tests, so a CLUSTER_HEAD_IP pointed at it would have
    exempted the LAN from the probe. On the Sparks the fabric ports carry an
    RDMA device and the LAN port does not."""
    matches = [e for e in entries if _same_address(e.address, head)]
    if not matches:
        return None
    defaults = set(default_devs)
    rdma = set(rdma_devs)
    worker_ip = _ip(worker)
    for e in matches:
        if e.ifname not in rdma:
            return "its interface is not an RDMA (RoCE) link, so it is not the cluster fabric"
        if e.ifname in defaults:
            return "a default route leaves through its interface, so it is not a cluster link"
        if worker_ip is None:
            return "the paired worker address is missing or not an IP address"
        if e.prefixlen is None:
            return "the kernel gave no prefix length for it"
        network = ipaddress.ip_interface(f"{_normalise(e.address)}/{e.prefixlen}").network
        if worker_ip.version != network.version or worker_ip not in network:
            return "the paired worker address is not on its subnet, so it is not the link to the worker"
    return None


# -------------------------------------------------------------------- probe --

class ProbeUnavailable(Exception):
    """The outside proof could not be obtained. The message never names an address."""


@dataclasses.dataclass(frozen=True)
class Probe:
    address: str
    family: int
    port: int


#: Runs on the WORKER under python3, fed on ssh's stdin. It prints one header
#: line and then one line per probe holding only the probe's index and a result
#: word -- never an address -- so nothing the worker says can leak one.
REMOTE_PROGRAM = r'''
import errno, ipaddress, json, socket
PROBES = json.loads(__PROBES__)
TIMEOUT = __TIMEOUT__
SELF_CHECK = __SELF_CHECK__
print(json.dumps(__HEADER__), flush=True)
UNREACHABLE = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EADDRNOTAVAIL}


def owned_here(address, fam):
    """A self-probe proves nothing, so an address this host owns is refused.
    Two independent signals: binding to it succeeds only if it is local (with
    ip_nonlocal_bind on, EVERY address looks local and the gate fails closed),
    and the source the routing table picks for it is the address itself."""
    b = socket.socket(fam, socket.SOCK_STREAM)
    try:
        b.bind((address, 0))
        return True
    except OSError:
        pass
    finally:
        b.close()
    u = socket.socket(fam, socket.SOCK_DGRAM)  # a UDP connect sends no packet
    try:
        u.connect((address, 9))
        return ipaddress.ip_address(u.getsockname()[0].split("%")[0]) == ipaddress.ip_address(address)
    finally:
        u.close()


for index, (address, family, port) in enumerate(PROBES):
    fam = socket.AF_INET6 if family == 6 else socket.AF_INET
    result = None
    if SELF_CHECK:
        try:
            if owned_here(address, fam):
                result = "local"
        except OSError as exc:
            result = "unreachable" if exc.errno in UNREACHABLE else "error"
    if result is None:
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(TIMEOUT)
        try:
            s.connect((address, port))
            result = "connected"
        except socket.timeout:
            result = "timeout"
        except ConnectionRefusedError:
            result = "refused"
        except OSError as exc:
            result = "unreachable" if exc.errno in UNREACHABLE else "error"
        finally:
            s.close()
    print(json.dumps({"i": index, "r": result}), flush=True)
'''

#: The remote command: check the tool exists (a harmless `command -v`), then
#: run the program from stdin.
REMOTE_COMMAND = (
    f"command -v python3 >/dev/null 2>&1 || {{ echo {MISSING_TOOL_MARKER}; exit {MISSING_TOOL_EXIT}; }}; "
    "exec python3 -"
)


def remote_program(probes: Sequence[Probe], timeout_s: float = CONNECT_TIMEOUT_S, self_check: bool = True) -> str:
    """The program for the worker. `self_check=False` exists ONLY so the unit
    tests can exercise connect/refuse on loopback (which is, correctly, always
    "local"); SshProber never passes it."""
    payload = json.dumps([[p.address, p.family, p.port] for p in probes])
    return (
        REMOTE_PROGRAM.replace("__PROBES__", repr(payload))
        .replace("__TIMEOUT__", repr(float(timeout_s)))
        .replace("__SELF_CHECK__", repr(bool(self_check)))
        .replace("__HEADER__", repr(PROBE_HEADER))
    )


def _ssh_failure_reason(returncode: int, stderr: str) -> str:
    """Why ssh failed, in words that never repeat ssh's own text (it names the host)."""
    text = stderr.lower()
    for needle, reason in (
        ("host key verification failed", "host key verification failed"),
        ("permission denied", "key authentication was refused (BatchMode allows no password)"),
        ("could not resolve", "the worker's name did not resolve"),
        ("no route to host", "there is no route to the worker"),
        ("network is unreachable", "the network to the worker is unreachable"),
        ("connection refused", "the worker refused the ssh connection"),
        ("timed out", "the ssh connection to the worker timed out"),
    ):
        if needle in text:
            return f"ssh to the worker failed: {reason} (exit {returncode})"
    return f"ssh to the worker failed (exit {returncode})"


class SshProber:
    """Runs the probes on the worker over non-interactive ssh."""

    def __init__(self, ssh_target: str, run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
                 timeout_s: float = CONNECT_TIMEOUT_S) -> None:
        if not SSH_TARGET_RE.match(ssh_target or ""):
            raise ProbeUnavailable("CLUSTER_WORKER_SSH is not of the form user@host, so there is no worker to probe from")
        self.ssh_target = ssh_target
        self.run_fn = run
        self.timeout_s = timeout_s

    def command(self) -> list[str]:
        return ["ssh", *SSH_OPTIONS, self.ssh_target, "--", REMOTE_COMMAND]

    def budget_s(self, count: int) -> float:
        return SSH_CONNECT_TIMEOUT_S + count * (self.timeout_s + 1) + 15

    def run(self, probes: Sequence[Probe]) -> list[str]:
        if self.run_fn is subprocess.run and shutil.which("ssh") is None:
            raise ProbeUnavailable("`ssh` is not on PATH on the runner, so the worker cannot be asked")
        budget = self.budget_s(len(probes))
        try:
            # Decoded with errors="replace": an sshd banner or a worker shell
            # printing non-UTF-8 bytes must not crash the gate into a bare
            # traceback. The replaced text is parsed, never echoed.
            proc = self.run_fn(
                self.command(), input=remote_program(probes, self.timeout_s),
                capture_output=True, encoding="utf-8", errors="replace", timeout=budget,
            )
        except subprocess.TimeoutExpired:
            raise ProbeUnavailable(f"the probe on the worker did not finish within {budget:.0f} s") from None
        except OSError as exc:
            raise ProbeUnavailable(f"ssh could not be started on the runner ({type(exc).__name__})") from None
        stdout = proc.stdout or ""
        if proc.returncode == 255:
            raise ProbeUnavailable(_ssh_failure_reason(proc.returncode, proc.stderr or ""))
        if proc.returncode == MISSING_TOOL_EXIT and MISSING_TOOL_MARKER in stdout:
            raise ProbeUnavailable("python3 is not installed on the worker, so the connects cannot be made")
        if proc.returncode != 0:
            raise ProbeUnavailable(f"the probe program failed on the worker (exit {proc.returncode})")
        return parse_probe_output(stdout, len(probes))


def parse_probe_output(stdout: str, count: int) -> list[str]:
    """Results in probe order. Anything unexpected is a failure, never a guess."""
    header_seen = False
    results: dict[int, str] = {}
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue  # a login banner; ignored, and never echoed
        if item == PROBE_HEADER:
            header_seen = True
            continue
        if not header_seen or not isinstance(item, dict):
            continue
        index, result = item.get("i"), item.get("r")
        if (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < count
                or result not in ALL_RESULTS or index in results):
            raise ProbeUnavailable("the worker's probe output was malformed, so no result can be trusted")
        results[index] = result
    if not header_seen:
        raise ProbeUnavailable("the probe program did not run on the worker (no header in its output)")
    if len(results) != count:
        raise ProbeUnavailable(f"the worker reported {len(results)} of {count} probe results")
    return [results[i] for i in range(count)]


# -------------------------------------------------------------------- check --

RESULT_WORDS = {
    "connected": "connected",
    "refused": "refused",
    "timeout": "timed out",
    "unreachable": "unreachable",
    "local": "an address the worker owns itself (a self-probe proves nothing)",
    "error": "failed with an unexpected error",
}


def _family_word(family: int) -> str:
    return "IPv6" if family == 6 else "IPv4"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 'es' if word.endswith('ss') else 's'}"


@dataclasses.dataclass
class Report:
    lines: list[str] = dataclasses.field(default_factory=list)
    failures: list[str] = dataclasses.field(default_factory=list)

    def say(self, text: str) -> None:
        self.lines.append(text)

    def fail(self, text: str) -> None:
        self.failures.append(text)
        self.lines.append(f"- FAIL: {text}")


def _ip_addr_json() -> str:
    if shutil.which("ip") is None:
        raise ProbeUnavailable("`ip` is not on PATH, so this host's addresses cannot be read from the kernel")
    proc = subprocess.run(["ip", "-j", "addr"], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise ProbeUnavailable(f"`ip -j addr` failed (exit {proc.returncode})")
    return proc.stdout


def _default_route_devs() -> frozenset[str]:
    if shutil.which("ip") is None:
        raise ProbeUnavailable("`ip` is not on PATH, so this host's routes cannot be read from the kernel")
    texts = []
    for family in ("-4", "-6"):
        proc = subprocess.run(["ip", "-j", family, "route", "show", "table", "all", "default"],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            raise ProbeUnavailable(f"`ip -j {family} route show table all default` failed (exit {proc.returncode})")
        texts.append(proc.stdout)
    return parse_default_route_devs(texts)


def _rdma_link_devs(sys_class_net: pathlib.Path = pathlib.Path("/sys/class/net")) -> frozenset[str]:
    """Interfaces backed by an RDMA device (`device/infiniband` is non-empty
    for a RoCE port). Read from sysfs, which needs no root."""
    try:
        links = list(sys_class_net.iterdir())
    except OSError as exc:
        raise ProbeUnavailable("this host's network interfaces could not be listed from sysfs") from exc
    devs = set()
    for link in links:
        ib = link / "device" / "infiniband"
        try:
            if ib.is_dir() and any(ib.iterdir()):
                devs.add(link.name)
        except OSError:
            continue
    return frozenset(devs)


def _describe_listeners(bound: Sequence[str], target: Target) -> str:
    wild4 = sum(1 for a in bound if is_wildcard(a) and wildcard_families(a) == {4})
    wild46 = sum(1 for a in bound if is_wildcard(a) and wildcard_families(a) != {4})
    loop = sum(1 for a in bound if not is_wildcard(a) and is_loopback(a))
    conf = sum(1 for a in bound if not is_wildcard(a) and not is_loopback(a) and _same_address(a, target.host))
    other = len(bound) - wild4 - wild46 - loop - conf
    parts = [f"{wild4} wildcard (IPv4)", f"{wild46} wildcard (IPv6/dual-stack)", f"{loop} loopback",
             f"{conf} on the configured address", f"{other} on another specific address"]
    return f"- engine port {target.port} listeners: " + ", ".join(parts)


def evaluate(
    env: dict[str, str],
    env_present: bool,
    ss_text: str,
    ip_json: Callable[[], str] | None = None,
    prober_factory: Callable[[str], object] | None = None,
    default_routes: Callable[[], Iterable[str]] | None = None,
    rdma_links: Callable[[], Iterable[str]] | None = None,
) -> Report:
    # Looked up at call time, not bound as defaults, so there is one place each
    # real side effect lives.
    ip_json = ip_json or (lambda: _ip_addr_json())
    prober_factory = prober_factory or (lambda target: SshProber(target))
    default_routes = default_routes or (lambda: _default_route_devs())
    rdma_links = rdma_links or (lambda: _rdma_link_devs())
    report = Report()
    target = resolve(env)
    report.say("")
    report.say("### Main engine exposure: can the unauthenticated engine API be reached from outside the cluster?")

    # 1. The listeners, exactly as strict as before for specific addresses.
    bound = listeners(ss_text, target.port)
    report.say(_describe_listeners(bound, target))
    if not bound:
        report.fail(f"nothing is listening on port {target.port}, so the bind cannot be proved "
                    "(the engine is down, or VLLM_PORT does not match what it serves)")
    for address in bound:
        if not is_wildcard(address) and not is_loopback(address) and not _same_address(address, target.host):
            report.fail("the engine port is bound to a specific address that is neither the configured "
                        "address nor loopback")

    config_wildcard = target.configured and is_wildcard(target.host)
    if not env_present:
        report.say("- configuration: no generated.env (the single-node default, loopback)")
    elif config_wildcard:
        report.say("- configuration: asks for a wildcard (the approved cluster shape; outside proof is mandatory)")
    elif target.configured:
        report.say("- configuration: a specific address")
    else:
        report.say("- configuration: no CLUSTER_API_BIND_ADDRESS (the single-node default, loopback)")

    wildcards = [a for a in bound if is_wildcard(a)]
    if not wildcards and not config_wildcard:
        report.say("- no wildcard listener and no wildcard configuration: no outside proof is needed")
        return report

    report.say("- a wildcard is in play: proving from the worker that only the cluster fabric reaches the engine port")
    _prove_from_outside(report, env, env_present, target, wildcards, ip_json, prober_factory, default_routes,
                        rdma_links)
    return report


def _prove_from_outside(report, env, env_present, target, wildcards, ip_json, prober_factory, default_routes,
                        rdma_links) -> None:
    if not env_present:
        report.fail("generated.env is missing, so the cluster fabric address and the worker are unknown "
                    "and the wildcard cannot be proved closed")
        return
    mode = env.get("TECHSARA_CLUSTER_MODE", "").strip() or "unset"
    if mode != "dual":
        report.fail(f"the deployment is not clustered (TECHSARA_CLUSTER_MODE is {mode}), so there is no worker "
                    "to prove the wildcard closed from")
        return
    fabric = env.get("CLUSTER_HEAD_IP", "").strip()
    try:
        ipaddress.ip_address(_normalise(fabric))
    except ValueError:
        report.fail("CLUSTER_HEAD_IP is missing or not an IP address, so the cluster fabric address is unknown")
        return
    try:
        prober = prober_factory(env.get("CLUSTER_WORKER_SSH", "").strip())
    except ProbeUnavailable as exc:
        report.fail(str(exc))
        return

    try:
        ip_text = ip_json()
        entries = parse_ip_addr_json(ip_text)
        masters = parse_link_masters(ip_text)
    except ProbeUnavailable as exc:
        report.fail(str(exc))
        return
    except (ValueError, subprocess.SubprocessError, OSError):
        report.fail("this host's addresses could not be read from the kernel")
        return
    try:
        default_devs = frozenset(default_routes())
    except ProbeUnavailable as exc:
        report.fail(str(exc))
        return
    except (ValueError, subprocess.SubprocessError, OSError):
        report.fail("this host's default routes could not be read from the kernel")
        return
    try:
        rdma_devs = frozenset(rdma_links())
    except ProbeUnavailable as exc:
        report.fail(str(exc))
        return
    except (ValueError, OSError):
        report.fail("this host's RDMA links could not be read from sysfs")
        return

    problem = fabric_link_problem(entries, fabric, env.get("CLUSTER_WORKER_IP", "").strip(), default_devs,
                                  rdma_devs)
    if problem:
        report.fail(f"the configured cluster fabric address (CLUSTER_HEAD_IP) does not look like a cluster link: "
                    f"{problem}; nothing can be exempted or proved through it")
        return
    fabric_secondary = env.get("CLUSTER_HEAD_IP_2", "").strip()
    if fabric_secondary:
        problem = fabric_link_problem(entries, fabric_secondary, env.get("CLUSTER_WORKER_IP_2", "").strip(),
                                      default_devs, rdma_devs)
        if problem:
            report.fail(f"the configured second-rail fabric address (CLUSTER_HEAD_IP_2) does not look like a "
                        f"cluster link: {problem}; it is probed as a non-cluster address instead")
            fabric_secondary = ""
    addresses = classify_addresses(entries, fabric, fabric_secondary, masters, default_devs)
    if not addresses:
        report.fail("no addresses were found on this host, so nothing can be proved")
        return
    fabric_addrs = [a for a in addresses if a.role == "fabric"]
    if not fabric_addrs:
        report.fail("the configured cluster fabric address is not an address of this host "
                    "(wrong CLUSTER_HEAD_IP, or the check is not running on the head)")
        return

    covered = frozenset().union(*(wildcard_families(a) for a in wildcards)) if wildcards else frozenset()
    link_local = [a for a in addresses if a.role == "link-local"]
    unprobeable = [a for a in link_local if a.family in covered]
    outside = [a for a in addresses if a.role == "outside"]

    def count(role: str) -> int:
        return sum(1 for a in addresses if a.role == role)

    report.say(
        f"- not probed (inside the cluster or the host): {count('fabric-secondary')} second-rail fabric, "
        f"{count('fabric-link-local')} fabric-link link-local, {count('bridge')} docker bridge and "
        f"{count('loopback')} loopback address(es); {len(link_local) - len(unprobeable)} link-local "
        "address(es) no wildcard listener covers"
    )
    if unprobeable:
        classes = ", ".join(sorted({a.iface_class for a in unprobeable}))
        report.fail(f"{len(unprobeable)} link-local address(es) on non-cluster interfaces (interface class: {classes}) "
                    "are covered by the wildcard, and this gate does not probe link-local addresses "
                    "(it would need the worker on the same link), so they are not proved closed")
    if not outside:
        report.fail("no non-cluster address was found on this host, yet it reaches the network somehow: "
                    "the classification left nothing to probe, so a pass would prove nothing")

    probes = [Probe(fabric_addrs[0].address, fabric_addrs[0].family, target.port)]
    for a in outside:
        probes.append(Probe(a.address, a.family, target.port))
        probes.append(Probe(a.address, a.family, CONTROL_PORT))
    try:
        results = prober.run(probes)
    except ProbeUnavailable as exc:
        report.fail(str(exc))
        return
    except Exception as exc:  # noqa: BLE001 -- anything else still ends in a reasoned FAIL and a report
        report.fail(f"the probe on the worker failed unexpectedly ({type(exc).__name__}), so nothing is proved")
        return

    fabric_result = results[0]
    if fabric_result == "connected":
        report.say("- cluster fabric address: reachable from the worker (required)")
    else:
        report.fail(f"cluster fabric address: NOT reachable from the worker ({RESULT_WORDS[fabric_result]}); "
                    "the probe path is not proven, so no refusal below can be trusted")

    accepted = unproven = blocked = 0
    details = []
    for n, a in enumerate(outside):
        engine, control = results[1 + 2 * n], results[2 + 2 * n]
        label = f"non-cluster address (interface class: {a.iface_class}, {_family_word(a.family)})"
        if engine == "connected":
            accepted += 1
            details.append((a.iface_class, a.family, f"  - {label}: ACCEPTED the connection on the engine port"))
        elif engine in BLOCKED_RESULTS and control == "connected":
            blocked += 1
            details.append((a.iface_class, a.family,
                            f"  - {label}: blocked ({RESULT_WORDS[engine]}); control port reachable, so the path is proven"))
        else:
            unproven += 1
            why = (RESULT_WORDS[engine] if engine not in BLOCKED_RESULTS
                   else f"engine port {RESULT_WORDS[engine]} but the control port {RESULT_WORDS[control]}")
            details.append((a.iface_class, a.family, f"  - {label}: NOT PROVEN ({why})"))

    report.say(f"- {_plural(len(outside), 'non-cluster address')}: {blocked} blocked, {accepted} ACCEPTED, "
               f"{unproven} not proven (required: all blocked)")
    for _, _, line in sorted(details):
        report.say(line)
    if accepted:
        classes = ", ".join(sorted({a.iface_class for n, a in enumerate(outside) if results[1 + 2 * n] == "connected"}))
        report.fail(f"{_plural(accepted, 'non-cluster address')} (interface class: {classes}) ACCEPTED the connection: "
                    "the unauthenticated engine API is reachable from outside the cluster "
                    "(apply scripts/host-guard.sh, see docs/developer-platform/OPERATIONS.md section 13)")
    if unproven:
        report.fail(f"{_plural(unproven, 'non-cluster address')} could not be proved closed from the worker")


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Replace every value that must not reach a public log. Defence in depth:
    the wording above never interpolates one, and this makes sure of it."""
    for secret in sorted({s for s in secrets if s and len(s) >= 3}, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return IPV4_RE.sub("[redacted]", text)


#: generated.env keys whose values are addresses, hosts or the ssh target.
SECRET_KEY_RE = re.compile(r"(?:^|_)(?:IP|ADDR|ADDRESS|HOST|SSH|IFNAME)(?:_\d+)?$")


def _secrets(env: dict[str, str], ip_text: str | None) -> list[str]:
    values = [v for k, v in env.items() if SECRET_KEY_RE.search(k) and v]
    for v in list(values):
        values.extend(part for part in re.split(r"[@,\s]+", v) if part)
    if ip_text:
        try:
            values.extend(e.address for e in parse_ip_addr_json(ip_text))
        except ValueError:
            pass
    try:
        values.append(socket.gethostname())
    except OSError:
        pass
    return values


def _ss_text(path: str | None) -> str:
    if path:
        return pathlib.Path(path).read_text(encoding="utf-8")
    if shutil.which("ss") is None:
        raise SystemExit("FATAL: `ss` is not on PATH, so the engine's listening sockets cannot be read")
    proc = subprocess.run(["ss", "-ltnH"], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise SystemExit(f"FATAL: `ss -ltnH` failed (exit {proc.returncode})")
    return proc.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("resolve", "check"):
        p = sub.add_parser(name)
        p.add_argument("--generated-env", required=True)
        if name == "resolve":
            p.add_argument("--github-output", default=None)
        else:
            p.add_argument("--ss-output", default=None, help="read `ss -ltnH` output from a file instead")
    args = ap.parse_args(argv)

    env_path = pathlib.Path(args.generated_env)
    env = read_env(env_path)
    target = resolve(env)

    if args.command == "resolve":
        if (os.environ.get("GITHUB_ACTIONS") == "true" and target.configured
                and not is_loopback(target.host) and not is_wildcard(target.host)):
            # The URL travels on as a step output and is shown in later steps'
            # env headers; mask the address so a public log shows *** instead.
            # (A workflow command line is consumed by the runner, not logged.)
            print(f"::add-mask::{target.host}", flush=True)
        source = ("CLUSTER_API_BIND_ADDRESS in generated.env" if target.configured
                  else "default loopback (" + ("no CLUSTER_API_BIND_ADDRESS" if env_path.is_file()
                                                else "no generated.env") + ")")
        print(f"main engine probe URL resolved from {source}, port {target.port}")
        if args.github_output:
            with open(args.github_output, "a", encoding="utf-8") as fh:
                fh.write(f"host={target.host}\nport={target.port}\nurl={target.url}\n")
        return 0

    ss_text = _ss_text(args.ss_output)
    captured: dict[str, str] = {}

    def ip_json() -> str:
        captured["ip"] = _ip_addr_json()
        return captured["ip"]

    report = evaluate(env, env_path.is_file(), ss_text, ip_json=ip_json)
    report.say("")
    if report.failures:
        report.say(f"RESULT: FAILED ({len(report.failures)} reason(s) above)")
    else:
        report.say("RESULT: PASSED")
    secrets = _secrets(env, captured.get("ip"))
    print(scrub("\n".join(report.lines), secrets), flush=True)
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
