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
out of the box, so plugging one into the management LAN is enough to be seen:
no port scan, no address list. Then:

  1. Its exporters answer: it joins the scrape targets within a minute, and every
     dashboard grows by one Spark because every panel is built from labels.
  2. Its exporters do not answer, but this host's SSH key is accepted: the
     exporters are copied over and started, then 1 applies on the next pass.
  3. Neither: it shows on the Fleet dashboard as `needs_ssh_key`, with the one
     command that fixes it (`ssh-copy-id`). Nothing else needs to change.

A SPARK NEVER SILENTLY DISAPPEARS. Once scraped, a Spark stays a target until
someone runs `forget`. A machine that dies must read as DOWN; a target list that
tracked what is currently visible would make it vanish from every panel instead,
exactly when it matters most.

NAMES ARE STABLE. `spark-N` is handed out once and tied to the hostname, not the
address, so a DHCP move (2026-11-01, worker 192.168.9.68 -> 192.168.9.3) updates
one field and every dashboard and alert keeps its history.

MANAGEMENT LAN ONLY. Discovery only trusts answers on the management interface,
and only the node's LAN address is ever written as a target. The RoCE addresses
are never used: monitoring must not share the fabric it measures.

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
import time
from pathlib import Path

ROOT = Path(os.environ.get("TECHSARA_ROOT", Path(__file__).resolve().parents[2]))
RUNTIME = ROOT / ".runtime"
FLEET_DIR = RUNTIME / "fleet"
REGISTRY = FLEET_DIR / "registry.json"
PUBLIC = FLEET_DIR / "fleet.json"
TARGETS_DIR = RUNTIME / "prometheus"
LOG_FILE = RUNTIME / "logs" / "fleet.log"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"

REMOTE_DIR = ".techsara-cluster"
REMOTE_PROJECT = "sf-local-ai-monitoring"
NODE_PORT = 9100
GPU_PORT = 9835
HEAD_NODE = "spark-1"
ENROLL_RETRY_S = 600
#: Copied to every Spark. None of them holds a secret.
ENROLL_FILES = (
    "compose/compose.monitoring-worker.yaml",
    "monitoring/exporters/dgx-gpu/dgx_gpu_exporter.py",
    "monitoring/exporters/spark-probe/spark_probe.py",
    "monitoring/exporters/spark-probe/spark_local.py",
    "scripts/host-guard.sh",  # installing it needs that machine's sudo; see docs/MONITORING.md
)
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]


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
    """A usable management address: not loopback, a bridge or the RoCE subnets."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.version != 4 or address.is_loopback or address.is_link_local:
        return False
    for excluded in ("10.100.184.0/24", "10.100.185.0/24", "172.16.0.0/12"):
        if address in ipaddress.ip_network(excluded):
            return False
    return True


# ------------------------------------------------------------------- registry --


def load_registry() -> dict:
    try:
        doc = json.loads(REGISTRY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        doc = {}
    doc.setdefault("members", {})
    doc.setdefault("ignored", [])
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
                ever_scraped=True, last_seen=now)
    mode = cfg.get("TECHSARA_CLUSTER_MODE") or cfg.get("CLUSTER_MODE") or "single"
    worker_ip = cfg.get("CLUSTER_WORKER_MGMT_IP") or cfg.get("MONITORING_WORKER_BIND") or ""
    if mode == "dual" and is_lan_address(worker_ip) and "spark-2" not in members:
        members["spark-2"] = {
            "role": "worker", "first_seen": now, "address": worker_ip, "hostname": "",
            "ssh": cfg.get("CLUSTER_WORKER_SSH") or "", "state": "scraping", "ever_scraped": True,
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
        if name == HEAD_NODE or not member.get("ever_scraped") or not member.get("address"):
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


def parse_avahi(text: str, interface: str, host_pattern: str) -> dict[str, str]:
    """`avahi-browse -rtp` resolved lines: hostname -> LAN address."""
    pattern = re.compile(host_pattern)
    found: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split(";")
        if len(fields) < 9 or fields[0] != "=" or fields[2] != "IPv4" or fields[1] != interface:
            continue
        host = fields[6].removesuffix(".local")
        if pattern.fullmatch(host) and is_lan_address(fields[7]):
            found[host] = fields[7]
    return found


def browse_mdns(interface: str, host_pattern: str, run=subprocess.run) -> dict[str, str]:
    try:
        proc = run(["avahi-browse", "-rtp", "--no-db-lookup", "_ssh._tcp"],
                   capture_output=True, text=True, timeout=12)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"mDNS browse unavailable: {type(exc).__name__}")
        return {}
    return parse_avahi(proc.stdout, interface, host_pattern)


def port_open(address: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return True
    except OSError:
        return False


def exporters_answer(address: str) -> bool:
    return port_open(address, NODE_PORT) and port_open(address, GPU_PORT)


# ------------------------------------------------------------------ enrolment --


def ssh_target(member: dict, cfg: dict[str, str]) -> str:
    if member.get("ssh"):
        return member["ssh"]
    user = cfg.get("MONITORING_FLEET_SSH_USER") or os.environ.get("USER") or "techsphere"
    return f"{user}@{member['address']}"


def ssh_base(cfg: dict[str, str]) -> list[str]:
    return ["ssh", *SSH_OPTIONS, *shlex.split(cfg.get("CLUSTER_WORKER_SSH_OPTS") or "")]


def is_a_spark(target: str, cfg: dict[str, str], run=subprocess.run) -> tuple[bool, str]:
    """Can this host log in, and is the far end a DGX with Docker and a GPU?"""
    check = "test -f /etc/dgx-release && docker info --format ok >/dev/null && command -v nvidia-smi >/dev/null && echo spark-ok"
    try:
        proc = run([*ssh_base(cfg), target, "--", check], capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, type(exc).__name__
    if proc.returncode == 255:
        return False, "ssh_refused"
    if "spark-ok" not in proc.stdout:
        return False, "not_a_dgx_spark"
    return True, "ok"


def enroll(member: dict, cfg: dict[str, str], run=subprocess.run, recreate: bool = False) -> bool:
    """Idempotent: the same copy and `compose up` start or repair the exporters.

    It only ever touches the `sf-local-ai-monitoring` project on the far side,
    so it cannot disturb a model rank or anything else running there. `recreate`
    restarts them too: the scripts are bind mounts, so new code needs a restart
    that Compose cannot see a reason for.
    """
    target = ssh_target(member, cfg)
    bind = member["address"]
    login_projects = cfg.get("MONITORING_DB_LOGIN_PROJECTS", "sf-local-ai")
    remote = (
        f"cd {REMOTE_DIR} && MONITORING_WORKER_BIND={shlex.quote(bind)} "
        f"MONITORING_DB_LOGIN_PROJECTS={shlex.quote(login_projects)} "
        f"docker compose --project-name {REMOTE_PROJECT} -f compose.monitoring-worker.yaml up -d"
        + (" --force-recreate" if recreate else "")
    )
    files = [str(ROOT / item) for item in ENROLL_FILES]
    scp = ["scp", "-q", *SSH_OPTIONS, *shlex.split(cfg.get("CLUSTER_WORKER_SSH_OPTS") or "")]
    try:
        for command, timeout in (
            ([*ssh_base(cfg), target, "--", f"mkdir -p {REMOTE_DIR}"], 40),
            ([*scp, *files, f"{target}:{REMOTE_DIR}/"], 60),
            ([*ssh_base(cfg), target, "--", remote], 300),
        ):
            proc = run(command, capture_output=True, text=True, timeout=timeout)
            if proc.returncode != 0:
                log(f"enrol {member.get('hostname') or target}: step failed: {proc.stderr.strip()[-300:]}")
                return False
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"enrol {member.get('hostname') or target}: {type(exc).__name__}")
        return False
    log(f"enrolled {member.get('hostname') or target}: exporters started, bind {bind}")
    return True


# ---------------------------------------------------------------------- passes --


def merge_discovered(doc: dict, found: dict[str, str]) -> list[str]:
    """Fold what mDNS saw into the registry. Returns members that need a re-enrol
    because their address moved and their exporters bind the old one."""
    members = doc["members"]
    now = int(time.time())
    moved: list[str] = []
    me = socket.gethostname()
    for host, address in sorted(found.items()):
        if host == me or host in doc["ignored"]:
            continue
        name = next((n for n, m in members.items() if m.get("hostname") == host), None)
        if name is None:
            # A seeded worker has an address but no hostname until mDNS names it.
            name = next((n for n, m in members.items() if m.get("address") == address and n != HEAD_NODE), None)
        if name is None:
            name = next_name(members)
            members[name] = {"role": "member", "first_seen": now, "state": "discovered", "ever_scraped": False}
            log(f"new Spark on the LAN: {host} at {address}, named {name}")
        member = members[name]
        member["hostname"] = host
        member["last_seen"] = now
        previous = member.get("address")
        if previous and previous != address:
            log(f"{name} ({host}) moved from {previous} to {address}")
            moved.append(name)
        member["address"] = address
    return moved


def discover(doc: dict, cfg: dict[str, str], found: dict[str, str], *, answers=exporters_answer,
             spark_check=is_a_spark, enroll_fn=enroll) -> None:
    moved = merge_discovered(doc, found)
    auto = (cfg.get("MONITORING_FLEET_AUTO_ENROLL") or "true").lower() not in {"0", "false", "no"}
    now = int(time.time())
    for name, member in doc["members"].items():
        if name == HEAD_NODE or not member.get("address"):
            continue
        if answers(member["address"]) and name not in moved:
            member.update(state="scraping", ever_scraped=True, last_seen=now)
            member.pop("enroll_error", None)
            continue
        visible = member.get("hostname") in found
        member["state"] = "down" if member.get("ever_scraped") else "discovered"
        if not (auto and visible):
            continue  # powered off or off the LAN: nothing to enrol into
        if now - member.get("enroll_attempt", 0) < ENROLL_RETRY_S and name not in moved:
            member["state"] = member.get("enroll_state", member["state"])
            continue
        member["enroll_attempt"] = now
        target = ssh_target(member, cfg)
        ok, reason = spark_check(target, cfg)
        if not ok:
            member["enroll_state"] = "needs_ssh_key" if reason == "ssh_refused" else "not_a_dgx_spark"
            member["state"] = member["enroll_state"]
            if not member.get("ever_scraped"):
                fix = f" Fix: ssh-copy-id {target}" if reason == "ssh_refused" else ""
                log(f"{name} ({member.get('hostname')}) waits for enrolment: {reason}.{fix}")
            continue
        enrolled = enroll_fn(member, cfg)
        member["enroll_state"] = "enrolling" if enrolled else "enroll_failed"
        member["state"] = member["enroll_state"]


def cmd_render(_args, doc, cfg) -> int:
    seed(doc, cfg)
    render(doc)
    save_registry(doc)
    return 0


def cmd_discover(_args, doc, cfg) -> int:
    seed(doc, cfg)
    interface = cfg.get("MONITORING_FLEET_INTERFACE") or "enP7s7"
    pattern = cfg.get("MONITORING_FLEET_HOST_REGEX") or r"spark-[0-9a-f]{4}"
    discover(doc, cfg, browse_mdns(interface, pattern))
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
        member["ssh"] = f"{user}@{address}"
    if host in doc["ignored"]:
        doc["ignored"].remove(host)
    ok, reason = is_a_spark(ssh_target(member, cfg), cfg)
    if not ok and not args.force:
        print(f"refused: {reason}. If the key is the problem: ssh-copy-id {ssh_target(member, cfg)}", file=sys.stderr)
        return 1
    if not enroll(member, cfg):
        return 1
    member.update(state="enrolling", ever_scraped=True)
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
    ok = enroll(member, cfg)
    if ok:
        member.update(state="enrolling", ever_scraped=True)
    render(doc)
    save_registry(doc)
    return 0 if ok else 1


def cmd_sync(_args, doc, cfg) -> int:
    """Every known Spark, each on its own: one being away must not stop the rest."""
    seed(doc, cfg)
    failures = 0
    for name, member in sorted(doc["members"].items()):
        if name == HEAD_NODE or not member.get("address"):
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
        # One pass at a time: the timer and an operator must not race on the file.
        fcntl.flock(lock, fcntl.LOCK_EX)
        return args.fn(args, load_registry(), settings())


if __name__ == "__main__":
    raise SystemExit(main())
