#!/usr/bin/env python3
"""Which DGX Sparks exist, and which of them Prometheus should scrape.

    fleet.py render            write Prometheus' target files from the registry
    fleet.py discover          one discovery pass (what the timer runs)
    fleet.py list              the registry as a table
    fleet.py add TARGET        enrol a Spark by address or user@address
    fleet.py forget NODE       stop scraping a Spark and stop rediscovering it
    fleet.py enroll NODE       copy the exporters to one Spark and start them
    fleet.py sync              enroll every known Spark (scripts/monitoring.sh up)
    fleet.py install-timer     run discovery every minute as a systemd user timer
    fleet.py uninstall-timer

HOW A NEW SPARK ARRIVES. Every DGX Spark advertises `spark-XXXX SSH` over mDNS
out of the box, so plugging one into the management LAN is enough to be seen: no
port scan, no address list. Then, if this host's SSH key is accepted, its exporters
are copied over and started, and it joins the scrape targets. If not, it shows on
the Monitoring page as `needs_ssh_key` with the one command that fixes it.

MDNS IS A HINT, NEVER AN AUTHORITY. Anyone on the office LAN can advertise any name.
So nothing mDNS says, and no port that happens to answer, ever makes a machine a
scrape target or moves an existing one:

  * A machine becomes a target only after SSH proves it: this host's key is accepted,
    a throwaway key is refused (a server that accepts everything is not a Spark), it
    has /etc/dgx-release and a GPU, and its own hostname matches.
  * Its host key is pinned at that moment under its hostname, in this directory's own
    known_hosts file, so every later connection must be the same machine at any address.
  * An enrolled Spark's address only changes when that pinned machine itself says it
    holds the new address. Advertising `spark-476e` from elsewhere changes nothing.

A determined attacker on the LAN can still pass the first proof by telling the two
keys apart, and appear as a NEW Spark. They get exporter code and a network map, which
hold no secret, and a bounded cost to Prometheus (see the job limits in prometheus.yml).
They cannot impersonate a Spark that is already enrolled. For a closed fleet, set
MONITORING_FLEET_AUTO_ENROLL=false and use `fleet add`.

A SPARK NEVER SILENTLY DISAPPEARS. Once scraped, a Spark stays a target until someone
runs `forget`. A machine that dies must read as DOWN; a target list that tracked what
is currently visible would make it vanish from every panel, when it matters most.

NAMES ARE STABLE. `spark-N` is handed out once and tied to the hostname, not the
address, so a DHCP move (2026-11-01, worker 192.168.9.68 -> 192.168.9.3) updates one
field and every dashboard and alert keeps its history.

MANAGEMENT LAN ONLY. Discovery only trusts answers on the management interface, from
inside its subnet, and only a node's LAN address is ever written as a target. The RoCE
addresses are never used: monitoring must not share the fabric it measures.

No third-party packages: launcher tests run this on a bare interpreter.
"""
from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("TECHSARA_ROOT", Path(__file__).resolve().parents[2]))
RUNTIME = ROOT / ".runtime"
FLEET_DIR = RUNTIME / "fleet"
REGISTRY = FLEET_DIR / "registry.json"
PUBLIC = FLEET_DIR / "fleet.json"
KNOWN_HOSTS = FLEET_DIR / "known_hosts"
TARGETS_DIR = RUNTIME / "prometheus"
LOG_FILE = RUNTIME / "logs" / "fleet.log"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"

REMOTE_DIR = ".techsara-cluster"
REMOTE_PROJECT = "sf-local-ai-monitoring"
NODE_PORT = 9100
GPU_PORT = 9835
HEAD_NODE = "spark-1"
ENROLL_RETRY_S = 600
#: What a flood of made-up names can cost: bounded registry, bounded SSH per pass.
MAX_MEMBERS = 32
MAX_ENROLL_ATTEMPTS_PER_PASS = 4
#: Copied to every Spark. None of them holds a secret.
ENROLL_FILES = (
    "compose/compose.monitoring-worker.yaml",
    "monitoring/exporters/dgx-gpu/dgx_gpu_exporter.py",
    "monitoring/exporters/spark-probe/spark_probe.py",
    "monitoring/exporters/spark-probe/spark_local.py",
    "scripts/host-guard.sh",  # installing it needs that machine's sudo; see docs/MONITORING.md
)
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8"]


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {message}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def env_file(path: Path) -> dict[str, str]:
    """`KEY=value` lines, last one wins, quotes stripped. No shell evaluation."""
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[match.group(1)] = value
    return values


def settings() -> dict[str, str]:
    """.env, then what the launcher generated. secrets.env is never read."""
    return {**env_file(ROOT / ".env"), **env_file(RUNTIME / "generated.env"), **{
        k: v for k, v in os.environ.items() if k.startswith(("MONITORING_", "CLUSTER_"))
    }}


def is_lan_address(value: str) -> bool:
    """A private unicast address that is not loopback, a bridge or the RoCE subnets."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if (address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_unspecified or address.is_reserved):
        return False
    for excluded in ("10.100.184.0/24", "10.100.185.0/24", "172.16.0.0/12"):
        if address in ipaddress.ip_network(excluded):
            return False
    return True


def management_network(cfg: dict[str, str], run=subprocess.run):
    """The subnet discovery trusts. None, and so nothing discovered, if it is unknown."""
    configured = cfg.get("MONITORING_FLEET_SUBNET")
    if configured:
        try:
            return ipaddress.ip_network(configured, strict=False)
        except ValueError:
            log(f"MONITORING_FLEET_SUBNET is not a network: {configured}")
            return None
    interface = cfg.get("MONITORING_FLEET_INTERFACE") or "enP7s7"
    try:
        proc = run(["ip", "-4", "-o", "addr", "show", "dev", interface], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"\binet (\d+\.\d+\.\d+\.\d+/\d+)", proc.stdout)
    return ipaddress.ip_network(match.group(1), strict=False) if match else None


# ------------------------------------------------------------------- registry --


def load_registry() -> dict:
    try:
        doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("members", {})
    doc.setdefault("ignored", [])
    for member in doc["members"].values():
        # A Spark scraped before enrolment needed proof was enrolled by a person.
        if member.get("ever_scraped"):
            member["enrolled"] = True
    return doc


def write_if_changed(path: Path, text: str) -> bool:
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def save_registry(doc: dict) -> None:
    write_if_changed(REGISTRY, json.dumps(doc, indent=2, sort_keys=True) + "\n")


def seed(doc: dict, cfg: dict[str, str]) -> None:
    """The head is always known. So is the worker the cluster is configured with."""
    now = int(time.time())
    members = doc["members"]
    head = members.setdefault(HEAD_NODE, {"role": "head", "first_seen": now})
    head.update(hostname=socket.gethostname(), address="host.docker.internal", state="scraping",
                ever_scraped=True, enrolled=True, last_seen=now)
    mode = cfg.get("TECHSARA_CLUSTER_MODE") or cfg.get("CLUSTER_MODE") or "single"
    worker_ip = cfg.get("CLUSTER_WORKER_MGMT_IP") or cfg.get("MONITORING_WORKER_BIND") or ""
    if mode == "dual" and is_lan_address(worker_ip) and "spark-2" not in members:
        members["spark-2"] = {
            "role": "worker", "first_seen": now, "address": worker_ip, "hostname": "",
            "ssh": cfg.get("CLUSTER_WORKER_SSH") or "", "state": "scraping", "ever_scraped": True, "enrolled": True,
        }


def next_name(members: dict) -> str:
    used = {int(m.group(1)) for name in members for m in [re.fullmatch(r"spark-(\d+)", name)] if m}
    number = 1
    while number in used:
        number += 1
    return f"spark-{number}"


def render(doc: dict) -> bool:
    """Prometheus' file_sd targets. The head stays static in prometheus.yml."""
    node_groups, gpu_groups = [], []
    for name, member in sorted(doc["members"].items()):
        # Enrolled means proven over SSH. mDNS and an open port alone never get here.
        if name == HEAD_NODE or not member.get("enrolled") or not member.get("ever_scraped") or not member.get("address"):
            continue
        labels = {"node": name, "role": member.get("role") or "member"}
        node_groups.append({"targets": [f"{member['address']}:{NODE_PORT}"], "labels": labels})
        gpu_groups.append({"targets": [f"{member['address']}:{GPU_PORT}"], "labels": labels})
    changed = write_if_changed(TARGETS_DIR / "sparks-node.json", json.dumps(node_groups, indent=2) + "\n")
    changed |= write_if_changed(TARGETS_DIR / "sparks-gpu.json", json.dumps(gpu_groups, indent=2) + "\n")
    public = {
        "generated_at": int(time.time()),
        "members": {
            name: {key: member.get(key) for key in ("hostname", "address", "role", "state")}
            for name, member in doc["members"].items()
        },
    }
    write_if_changed(PUBLIC, json.dumps(public, indent=2, sort_keys=True) + "\n")
    return changed


# ------------------------------------------------------------------ discovery --


def parse_avahi(text: str, interface: str, host_pattern: str, network=None) -> dict[str, str]:
    """`avahi-browse -rtp` resolved lines: hostname -> LAN address."""
    pattern = re.compile(host_pattern)
    found: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split(";")
        if len(fields) < 9 or fields[0] != "=" or fields[2] != "IPv4" or fields[1] != interface:
            continue
        host = fields[6].removesuffix(".local")
        if not pattern.fullmatch(host) or not is_lan_address(fields[7]):
            continue
        if network is not None and ipaddress.ip_address(fields[7]) not in network:
            continue
        found[host] = fields[7]
    return found


def browse_mdns(interface: str, host_pattern: str, network=None, run=subprocess.run) -> dict[str, str]:
    try:
        proc = run(["avahi-browse", "-rtp", "--no-db-lookup", "_ssh._tcp"],
                   capture_output=True, text=True, timeout=12)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"mDNS browse unavailable: {type(exc).__name__}")
        return {}
    return parse_avahi(proc.stdout, interface, host_pattern, network)


def port_open(address: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def exporters_answer(address: str) -> bool:
    return port_open(address, NODE_PORT) and port_open(address, GPU_PORT)


# ------------------------------------------------------------------ enrolment --


def ssh_target(member: dict, cfg: dict[str, str], address: str | None = None) -> str:
    """The configured worker has a rail address; anyone else is reached at `address`."""
    if member.get("ssh"):
        return member["ssh"]
    user = member.get("ssh_user") or cfg.get("MONITORING_FLEET_SSH_USER") or os.environ.get("USER") or "techsphere"
    return f"{user}@{address or member['address']}"


def ssh_identity_options(member: dict, *, first: bool) -> list[str]:
    """Pin the host key under the machine's hostname, so it is checked at any address.

    `first` is the one moment a new key is taken on trust, and only for a machine that
    is not enrolled yet. After that a different key is a refusal.
    """
    alias = member.get("hostname") or member.get("address") or "unknown"
    return ["-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", f"HostKeyAlias={alias}",
            "-o", f"StrictHostKeyChecking={'accept-new' if first else 'yes'}"]


def ssh_base(cfg: dict[str, str], member: dict, *, first: bool) -> list[str]:
    return ["ssh", *SSH_OPTIONS, *ssh_identity_options(member, first=first), *shlex.split(cfg.get("CLUSTER_WORKER_SSH_OPTS") or "")]


def accepts_anything(target: str, cfg: dict[str, str], run=subprocess.run) -> bool:
    """A real sshd refuses a key nobody installed. One that lets it in is not a Spark."""
    with tempfile.TemporaryDirectory(prefix="fleet-canary-") as tmp:
        key = os.path.join(tmp, "key")
        try:
            made = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", key], capture_output=True, text=True, timeout=20)
            if made.returncode != 0:
                return True  # cannot prove it refuses, so do not trust it
            proc = run(["ssh", *SSH_OPTIONS, "-i", key, "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                        "-o", "PreferredAuthentications=publickey", "-o", "UserKnownHostsFile=/dev/null",
                        "-o", "StrictHostKeyChecking=no", *shlex.split(cfg.get("CLUSTER_WORKER_SSH_OPTS") or ""),
                        target, "--", "true"], capture_output=True, text=True, timeout=40)
        except (OSError, subprocess.SubprocessError):
            return True
    return proc.returncode == 0


def read_machine(member: dict, target: str, cfg: dict[str, str], *, first: bool, run=subprocess.run) -> tuple[dict | None, str]:
    """Ask the machine itself. Returns its facts, or None and why not."""
    check = ("test -f /etc/dgx-release && docker info --format ok >/dev/null && command -v nvidia-smi >/dev/null "
             "&& echo spark-ok && hostname && ip -4 -o addr show")
    try:
        proc = run([*ssh_base(cfg, member, first=first), target, "--", check], capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, type(exc).__name__
    if proc.returncode == 255:
        return None, "ssh_refused"
    lines = proc.stdout.splitlines()
    if not lines or lines[0].strip() != "spark-ok" or len(lines) < 2:
        return None, "not_a_dgx_spark"
    return {"hostname": lines[1].strip(), "addresses": re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)/", proc.stdout)}, "ok"


def prove_new_spark(member: dict, cfg: dict[str, str], run=subprocess.run) -> tuple[bool, str]:
    """Everything SSH can say about a machine that has not been enrolled yet."""
    target = ssh_target(member, cfg)
    facts, reason = read_machine(member, target, cfg, first=True, run=run)
    if facts is None:
        return False, reason
    if accepts_anything(target, cfg, run=run):
        return False, "accepts_any_key"
    if member.get("hostname") and facts["hostname"] != member["hostname"]:
        return False, "hostname_mismatch"
    member["hostname"] = facts["hostname"]
    return True, "ok"


def proves_move(member: dict, candidate: str, cfg: dict[str, str], run=subprocess.run) -> bool:
    """An enrolled Spark moves only when the pinned machine says it holds the address.

    The worker is asked over its rail address, which needs no LAN address at all. Any
    other Spark is reached at the new address, where StrictHostKeyChecking=yes means
    only the pinned machine can answer.
    """
    target = ssh_target(member, cfg, candidate)
    facts, _reason = read_machine(member, target, cfg, first=False, run=run)
    return bool(facts and facts["hostname"] == member.get("hostname") and candidate in facts["addresses"])


def enroll(member: dict, cfg: dict[str, str], run=subprocess.run, recreate: bool = False) -> bool:
    """Idempotent: the same copy and `compose up` start or repair the exporters.

    It only ever touches the `sf-local-ai-monitoring` project on the far side,
    so it cannot disturb a model rank or anything else running there. `recreate`
    restarts them too: the scripts are bind mounts, so new code needs a restart
    that Compose cannot see a reason for.
    """
    target = ssh_target(member, cfg)
    bind = member["address"]
    if not is_lan_address(bind):
        log(f"enrol {member.get('hostname') or target}: refusing to bind {bind}")
        return False
    first = not member.get("key_pinned")
    login_projects = cfg.get("MONITORING_DB_LOGIN_PROJECTS", "sf-local-ai")
    remote = (
        f"cd {REMOTE_DIR} && MONITORING_WORKER_BIND={shlex.quote(bind)} "
        f"MONITORING_DB_LOGIN_PROJECTS={shlex.quote(login_projects)} "
        f"docker compose --project-name {REMOTE_PROJECT} -f compose.monitoring-worker.yaml up -d"
        + (" --force-recreate" if recreate else "")
    )
    files = [str(ROOT / item) for item in ENROLL_FILES]
    scp = ["scp", "-q", *SSH_OPTIONS, *ssh_identity_options(member, first=first), *shlex.split(cfg.get("CLUSTER_WORKER_SSH_OPTS") or "")]
    ssh = ssh_base(cfg, member, first=first)
    try:
        for command, timeout in (
            ([*ssh, target, "--", f"mkdir -p {REMOTE_DIR}"], 40),
            ([*scp, *files, f"{target}:{REMOTE_DIR}/"], 60),
            ([*ssh, target, "--", remote], 300),
        ):
            proc = run(command, capture_output=True, text=True, timeout=timeout)
            if proc.returncode != 0:
                log(f"enrol {member.get('hostname') or target}: step failed: {proc.stderr.strip()[-300:]}")
                return False
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"enrol {member.get('hostname') or target}: {type(exc).__name__}")
        return False
    member["key_pinned"] = True
    log(f"enrolled {member.get('hostname') or target}: exporters started, bind {bind}")
    return True


# ---------------------------------------------------------------------- passes --


def merge_discovered(doc: dict, found: dict[str, str]) -> None:
    """Fold what mDNS saw into the registry, as hints.

    A new name becomes a candidate that is not scraped. A name that is already enrolled
    keeps its address: a different one is only recorded for `discover` to check with the
    machine itself.
    """
    members = doc["members"]
    now = int(time.time())
    me = socket.gethostname()
    for host, address in sorted(found.items()):
        if host == me or host in doc["ignored"]:
            continue
        name = next((n for n, m in members.items() if m.get("hostname") == host), None)
        if name is None:
            # This address belongs to an enrolled Spark, under another name or none yet.
            # A claim to it is not a new machine, and only that machine can name itself.
            if any(m.get("enrolled") and m.get("address") == address for m in members.values()):
                continue
            if len(members) >= MAX_MEMBERS:
                log(f"ignoring {host}: the registry already holds {MAX_MEMBERS} Sparks")
                continue
            name = next_name(members)
            members[name] = {"role": "member", "first_seen": now, "state": "discovered", "ever_scraped": False,
                             "enrolled": False, "hostname": host, "address": address}
            log(f"new Spark on the LAN: {host} at {address}, named {name}. Not scraped until proven.")
            continue
        member = members[name]
        member["last_seen"] = now
        if member.get("enrolled"):
            if member.get("address") != address:
                member["candidate_address"] = address
            else:
                member.pop("candidate_address", None)
        else:
            member["address"] = address


def identify_machine(member: dict, cfg: dict[str, str], run=subprocess.run) -> str | None:
    """What the worker from .env calls itself. mDNS does not get to say."""
    facts, _reason = read_machine(member, ssh_target(member, cfg), cfg, first=not member.get("key_pinned"), run=run)
    return facts["hostname"] if facts else None


def discover(doc: dict, cfg: dict[str, str], found: dict[str, str], *, answers=exporters_answer,
             proof=prove_new_spark, move_proof=proves_move, enroll_fn=enroll, identify=identify_machine) -> None:
    for name, member in doc["members"].items():
        if name != HEAD_NODE and member.get("enrolled") and not member.get("hostname") and member.get("address"):
            member["hostname"] = identify(member, cfg) or ""
    merge_discovered(doc, found)
    auto = (cfg.get("MONITORING_FLEET_AUTO_ENROLL") or "true").lower() not in {"0", "false", "no"}
    now = int(time.time())
    attempts = 0
    for name, member in doc["members"].items():
        if name == HEAD_NODE or not member.get("address"):
            continue
        visible = member.get("hostname") in found
        due = now - member.get("enroll_attempt", 0) >= ENROLL_RETRY_S

        if member.get("enrolled"):
            candidate = member.get("candidate_address")
            if candidate and auto and due:
                member["enroll_attempt"] = now
                if move_proof(member, candidate, cfg):
                    log(f"{name} ({member.get('hostname')}) moved from {member['address']} to {candidate}")
                    member["address"] = candidate
                    member.pop("candidate_address", None)
                    enroll_fn(member, cfg)
                else:
                    log(f"{name}: mDNS says {candidate}, but the machine does not. Ignoring it.")
            if answers(member["address"]):
                member.update(state="scraping", ever_scraped=True, last_seen=now)
                member.pop("enroll_state", None)
                continue
            member["state"] = "down" if member.get("ever_scraped") else member.get("enroll_state", "enrolling")
            # Same address, machine visible, exporters gone: repair them. Only a Spark
            # still at its pinned address gets this; anything else is the check above.
            if auto and visible and due and not candidate:
                member["enroll_attempt"] = now
                enroll_fn(member, cfg)
            continue

        # Not enrolled: whatever answers on this address proves nothing.
        member["state"] = member.get("enroll_state") or "discovered"
        if not (auto and visible and due) or attempts >= MAX_ENROLL_ATTEMPTS_PER_PASS:
            continue
        attempts += 1
        member["enroll_attempt"] = now
        ok, reason = proof(member, cfg)
        if not ok:
            member["enroll_state"] = {"ssh_refused": "needs_ssh_key"}.get(reason, "not_a_dgx_spark" if reason != "accepts_any_key" else "refused")
            member["state"] = member["enroll_state"]
            fix = f" Fix: ssh-copy-id {ssh_target(member, cfg)}" if reason == "ssh_refused" else ""
            log(f"{name} ({member.get('hostname')}) waits for enrolment: {reason}.{fix}")
            continue
        enrolled = enroll_fn(member, cfg)
        member["enroll_state"] = "enrolling" if enrolled else "enroll_failed"
        member["state"] = member["enroll_state"]
        member["enrolled"] = bool(enrolled)


def cmd_render(_args, doc, cfg) -> int:
    seed(doc, cfg)
    render(doc)
    save_registry(doc)
    return 0


def cmd_discover(_args, doc, cfg) -> int:
    seed(doc, cfg)
    interface = cfg.get("MONITORING_FLEET_INTERFACE") or "enP7s7"
    pattern = cfg.get("MONITORING_FLEET_HOST_REGEX") or r"spark-[0-9a-f]{4}"
    network = management_network(cfg)
    if network is None:
        log(f"cannot tell the management subnet from {interface}: not trusting any mDNS answer this pass")
        found: dict[str, str] = {}
    else:
        found = browse_mdns(interface, pattern, network)
    discover(doc, cfg, found)
    render(doc)
    save_registry(doc)
    return 0


def cmd_list(_args, doc, cfg) -> int:
    seed(doc, cfg)
    print(f"{'NODE':<10}{'HOSTNAME':<16}{'ADDRESS':<24}{'ROLE':<9}STATE")
    for name, member in sorted(doc["members"].items(), key=lambda kv: (len(kv[0]), kv[0])):
        print(f"{name:<10}{member.get('hostname') or '-':<16}{member.get('address') or '-':<24}"
              f"{member.get('role') or '-':<9}{member.get('state') or '-'}")
        if member.get("state") == "needs_ssh_key":
            print(f"          fix: ssh-copy-id {ssh_target(member, cfg)}")
    if doc["ignored"]:
        print("ignored:", ", ".join(doc["ignored"]))
    return 0


def cmd_add(args, doc, cfg) -> int:
    seed(doc, cfg)
    user, _, host = args.target.rpartition("@")
    try:
        address = host if is_lan_address(host) else socket.gethostbyname(host)
    except OSError:
        print(f"cannot resolve {host}", file=sys.stderr)
        return 2
    if not is_lan_address(address):
        print(f"{address} is not a management LAN address", file=sys.stderr)
        return 2
    existing = next((n for n, m in doc["members"].items() if m.get("address") == address), None)
    name = args.name or existing or next_name(doc["members"])
    member = doc["members"].setdefault(name, {"role": args.role or "member", "first_seen": int(time.time())})
    member["address"] = address
    if user:
        member["ssh_user"] = user
    if host in doc["ignored"]:
        doc["ignored"].remove(host)
    # A person asked for this one, so the host name need not match what mDNS said, and
    # a renamed machine can be added. The other checks still apply.
    member.pop("hostname", None)
    ok, reason = prove_new_spark(member, cfg)
    if not ok and not args.force:
        fix = f" If the key is the problem: ssh-copy-id {ssh_target(member, cfg)}" if reason == "ssh_refused" else ""
        print(f"refused: {reason}.{fix}", file=sys.stderr)
        doc["members"].pop(name)
        return 1
    if not member.get("hostname"):
        member["hostname"] = name
    if not enroll(member, cfg):
        return 1
    member.update(state="enrolling", enrolled=True)
    render(doc)
    save_registry(doc)
    print(f"{name} enrolled at {address}. It appears on the dashboards within a minute.")
    return 0


def cmd_forget(args, doc, cfg) -> int:
    seed(doc, cfg)
    if args.node == HEAD_NODE:
        print("the head cannot be forgotten", file=sys.stderr)
        return 2
    member = doc["members"].pop(args.node, None)
    if member is None:
        print(f"no such node: {args.node}", file=sys.stderr)
        return 2
    if member.get("hostname") and member["hostname"] not in doc["ignored"]:
        doc["ignored"].append(member["hostname"])
    render(doc)
    save_registry(doc)
    print(f"{args.node} removed. Its exporters still run on that machine; history stays in Prometheus.")
    return 0


def cmd_enroll(args, doc, cfg) -> int:
    seed(doc, cfg)
    member = doc["members"].get(args.node)
    if member is None or args.node == HEAD_NODE:
        print(f"not an enrollable node: {args.node}", file=sys.stderr)
        return 2
    if not member.get("enrolled"):
        print(f"{args.node} is not enrolled yet. Use: fleet add {ssh_target(member, cfg)}", file=sys.stderr)
        return 2
    ok = enroll(member, cfg)
    if ok:
        member["state"] = "enrolling"
    render(doc)
    save_registry(doc)
    return 0 if ok else 1


def cmd_sync(_args, doc, cfg) -> int:
    """Every known Spark, each on its own: one being away must not stop the rest."""
    seed(doc, cfg)
    failures = 0
    for name, member in sorted(doc["members"].items()):
        if name == HEAD_NODE or not member.get("address") or not member.get("enrolled"):
            continue
        if not enroll(member, cfg, recreate=True):
            failures += 1
            log(f"{name} was not updated; `fleet.py enroll {name}` retries it")
    render(doc)
    save_registry(doc)
    return 1 if failures else 0


def cmd_install_timer(_args, _doc, _cfg) -> int:
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    python = sys.executable or "/usr/bin/python3"
    (UNIT_DIR / "techsara-fleet.service").write_text(
        "[Unit]\nDescription=TechSara: discover DGX Sparks for monitoring\nAfter=network-online.target\n\n"
        f"[Service]\nType=oneshot\nExecStart={python} {ROOT / 'scripts' / 'lib' / 'fleet.py'} discover\n",
        encoding="utf-8",
    )
    (UNIT_DIR / "techsara-fleet.timer").write_text(
        "[Unit]\nDescription=TechSara: DGX Spark discovery every minute\n\n"
        "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=60s\nAccuracySec=10s\nUnit=techsara-fleet.service\n\n"
        "[Install]\nWantedBy=timers.target\n",
        encoding="utf-8",
    )
    for command in (["daemon-reload"], ["enable", "--now", "techsara-fleet.timer"]):
        proc = subprocess.run(["systemctl", "--user", *command], capture_output=True, text=True, timeout=30)
        if proc.returncode != 0:
            print(f"systemctl --user {' '.join(command)}: {proc.stderr.strip()}", file=sys.stderr)
            return 1
    print("techsara-fleet.timer installed: discovery runs every minute")
    return 0


def cmd_uninstall_timer(_args, _doc, _cfg) -> int:
    subprocess.run(["systemctl", "--user", "disable", "--now", "techsara-fleet.timer"],
                   capture_output=True, text=True, timeout=30)
    for name in ("techsara-fleet.timer", "techsara-fleet.service"):
        (UNIT_DIR / name).unlink(missing_ok=True)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, text=True, timeout=30)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DGX Spark fleet membership for monitoring")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn in (("render", cmd_render), ("discover", cmd_discover), ("list", cmd_list), ("sync", cmd_sync),
                     ("install-timer", cmd_install_timer), ("uninstall-timer", cmd_uninstall_timer)):
        sub.add_parser(name).set_defaults(fn=fn)
    add = sub.add_parser("add")
    add.add_argument("target")
    add.add_argument("--name")
    add.add_argument("--role")
    add.add_argument("--force", action="store_true")
    add.set_defaults(fn=cmd_add)
    forget = sub.add_parser("forget")
    forget.add_argument("node")
    forget.set_defaults(fn=cmd_forget)
    enrol = sub.add_parser("enroll")
    enrol.add_argument("node")
    enrol.set_defaults(fn=cmd_enroll)
    args = parser.parse_args(argv)

    FLEET_DIR.mkdir(parents=True, exist_ok=True)
    with (FLEET_DIR / ".lock").open("w", encoding="utf-8") as lock:
        # One pass at a time: the timer and an operator must not race on the file. A
        # timer pass that finds one running just stops; there is another in a minute.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if args.command == "discover" else 0))
        except BlockingIOError:
            return 0
        return args.fn(args, load_registry(), settings())


if __name__ == "__main__":
    raise SystemExit(main())
