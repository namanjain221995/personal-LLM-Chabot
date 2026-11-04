#!/usr/bin/env python3
"""spark-probe: what node-exporter and nvidia-smi cannot see on a DGX Spark.

WHAT IT PUBLISHES, per Spark, identically on every node:

  * every Docker container: state, health, restarts, OOM kills, CPU, memory,
    disk and network I/O, and the GPU memory each one holds;
  * every database it can find, with NO configuration: liveness through the
    engine's real protocol, version, connections by state, data size on disk;
  * the kernel's own account of hardware trouble: GPU Xid errors, OOM kills,
    PCIe AER, NVMe and filesystem errors, hung tasks, lockups;
  * the RoCE hardware counters node-exporter does not read: retransmits,
    congestion notifications, sequence errors;
  * the processes using the most CPU and memory.

WHY THIS IS NOT A SERVER. The process opens no listening socket. It writes one
Prometheus text file, atomically, into a volume that node-exporter's textfile
collector reads, so it adds no port to any Spark, needs no host-guard change,
and nothing on the LAN can talk to it. A process that can read every
container's configuration should not also be the one answering the network.

WHY NOT THE DOCKER SOCKET. A read-only bind of docker.sock is not read-only: it
still lets anything inside create a privileged container (developer-platform
audit F057). Docker's own state files give names, labels and restarts without
any write path to the daemon, and the cgroup tree gives the usage. The one thing
they do not hold is the health verdict, so that comes from spark_local.py, which
has the socket and no network.

WHAT IT DOES WITH CREDENTIALS. Liveness, version, connection counts (from
PostgreSQL's process titles) and data size need no database login at all. A
login is only used for the projects named in SPARK_PROBE_DEEP_DB_PROJECTS, by
default only this platform's own stack. For any other project the password is
discarded while the container's configuration is parsed, so it is never held.
No password, environment value or query text is ever logged or exported.

ABSENT IS NOT ZERO. A reading the probe could not take is left out rather than
written as 0, and each collector reports its own success, so a blind spot shows
up as a failing collector instead of as a healthy-looking zero.

Standard library only, so it runs on a stock python:3.12-slim with no build
step, like the GPU and data-store exporters beside it.
"""
from __future__ import annotations

import base64
import calendar
import concurrent.futures
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import socket
import stat as stat_module
import struct
import threading
import time
import urllib.request

log = logging.getLogger("spark-probe")


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


TEXTFILE = _env("SPARK_PROBE_TEXTFILE", "/textfile/spark_probe.prom")
INTERVAL_S = float(_env("SPARK_PROBE_INTERVAL", "15"))
DOCKER_ROOT = _env("SPARK_PROBE_DOCKER_ROOT", "/host/docker")
CGROUP_ROOT = _env("SPARK_PROBE_CGROUP_ROOT", "/host/cgroup")
PROC_ROOT = _env("SPARK_PROBE_PROC", "/proc")
KMSG_PATH = _env("SPARK_PROBE_KMSG", "/dev/kmsg")
IB_ROOT = _env("SPARK_PROBE_IB_ROOT", "/sys/class/infiniband")
GPU_METRICS_URL = _env("SPARK_PROBE_GPU_URL", "http://127.0.0.1:9835/metrics")
DGX_RELEASE = _env("SPARK_PROBE_DGX_RELEASE", "/host/dgx-release")
FLEET_FILE = _env("SPARK_PROBE_FLEET_FILE", "/fleet/fleet.json")
#: Compose projects the probe may log in to, comma separated; `*` means all and
#: `standalone` means containers outside Compose. Empty disables logins.
DEEP_DB_PROJECTS = _env("SPARK_PROBE_DEEP_DB_PROJECTS", "sf-local-ai")
TOP_PROCESSES = int(_env("SPARK_PROBE_TOP_PROCESSES", "12"))
DB_DIAL_TIMEOUT_S = float(_env("SPARK_PROBE_DB_TIMEOUT", "2"))
DEEP_INTERVAL_S = float(_env("SPARK_PROBE_DEEP_INTERVAL", "30"))
#: pg_database_size walks every file of a database, so it runs far less often.
SIZE_QUERY_INTERVAL_S = float(_env("SPARK_PROBE_SIZE_QUERY_INTERVAL", "300"))
DATA_WALK_INTERVAL_S = float(_env("SPARK_PROBE_DATA_WALK_INTERVAL", "300"))
DATA_WALK_BUDGET_S = float(_env("SPARK_PROBE_DATA_WALK_BUDGET", "20"))

CLOCK_TICKS = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096

# --------------------------------------------------------------- exposition --


class Exposition:
    """Prometheus text format, one family at a time.

    Duplicates are collapsed on purpose. A repeated name and label set makes
    node-exporter drop the file, which would blank the whole probe for one
    container that happened to be listed twice.
    """

    def __init__(self) -> None:
        self._families: dict[str, tuple[str, str, dict[tuple, float]]] = {}

    def add(self, name: str, metric_type: str, help_text: str, value, **labels) -> None:
        if value is None:
            return
        try:
            number = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(number):
            return
        family = self._families.setdefault(name, (metric_type, help_text, {}))
        family[2][tuple(sorted((k, str(v)) for k, v in labels.items()))] = number

    def gauge(self, name: str, help_text: str, value, **labels) -> None:
        self.add(name, "gauge", help_text, value, **labels)

    def counter(self, name: str, help_text: str, value, **labels) -> None:
        self.add(name, "counter", help_text, value, **labels)

    @staticmethod
    def _escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    @staticmethod
    def _number(value: float) -> str:
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else repr(value)

    def render(self) -> str:
        out: list[str] = []
        for name in sorted(self._families):
            metric_type, help_text, samples = self._families[name]
            out.append(f"# HELP {name} {self._escape(help_text)}")
            out.append(f"# TYPE {name} {metric_type}")
            for labels, value in sorted(samples.items()):
                body = ",".join(f'{k}="{self._escape(v)}"' for k, v in labels)
                head = f"{name}{{{body}}}" if body else name
                out.append(f"{head} {self._number(value)}")
        return "\n".join(out) + "\n"


def write_atomically(path: str, text: str) -> None:
    """A reader must never see half a file: node-exporter would reject it."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


# ------------------------------------------------------------- small helpers --


def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def read_int(path: str) -> int | None:
    raw = read_text(path)
    if raw is None:
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None  # memory.max reads "max" when there is no limit


def read_kv(path: str) -> dict[str, int]:
    """`key value` per line: cpu.stat, memory.stat, memory.events."""
    raw = read_text(path)
    if raw is None:
        return {}
    out: dict[str, int] = {}
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                out[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return out


_RFC3339 = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.(\d+))?Z$")


def parse_docker_time(value: str | None) -> float | None:
    """Docker writes nanosecond RFC 3339, and 0001-01-01 for "never"."""
    if not value:
        return None
    match = _RFC3339.match(value)
    if not match:
        return None
    year, month, day, hour, minute, second = (int(match.group(i)) for i in range(1, 7))
    if year < 2000:
        return None
    fraction = float(f"0.{match.group(7)}") if match.group(7) else 0.0
    return calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0)) + fraction


# --------------------------------------------------------- database detection --

#: engine, image pattern, default port, how to prove it is alive.
#: The patterns match a path component of the image name so that
#: `pgvector/pgvector:0.8.7-pg17` and `docker.io/library/postgres` both count.
DB_ENGINES = (
    ("postgres", r"(^|/)(postgres|postgis|pgvector|timescaledb|timescaledb-ha|paradedb|citus|pgrouting)([:@/]|$)", 5432, "postgres"),
    ("postgres", r"(^|/)supabase/postgres", 5432, "postgres"),
    ("redis", r"(^|/)(redis|redis-stack|redis-stack-server|valkey|keydb|dragonfly)([:@/]|$)", 6379, "redis"),
    ("mysql", r"(^|/)(mysql|mariadb|percona|percona-server)([:@/]|$)", 3306, "mysql"),
    ("mongodb", r"(^|/)(mongo|mongodb|mongodb-community-server)([:@/]|$)", 27017, "tcp"),
    ("clickhouse", r"(^|/)clickhouse(-server)?([:@/]|$)", 8123, "http:/ping"),
    ("elasticsearch", r"(^|/)elasticsearch([:@/]|$)", 9200, "http:/"),
    ("opensearch", r"(^|/)opensearch([:@/]|$)", 9200, "http:/"),
    ("qdrant", r"(^|/)qdrant([:@/]|$)", 6333, "http:/healthz"),
    ("milvus", r"(^|/)milvus([:@/]|$)", 19530, "tcp"),
    ("weaviate", r"(^|/)weaviate([:@/]|$)", 8080, "http:/v1/.well-known/live"),
    ("chroma", r"(^|/)chroma([:@/]|$)", 8000, "http:/api/v2/heartbeat"),
    ("influxdb", r"(^|/)influxdb([:@/]|$)", 8086, "http:/health"),
    ("neo4j", r"(^|/)neo4j([:@/]|$)", 7474, "http:/"),
    ("cassandra", r"(^|/)(cassandra|scylla)([:@/]|$)", 9042, "tcp"),
    ("couchdb", r"(^|/)couchdb([:@/]|$)", 5984, "http:/_up"),
    ("etcd", r"(^|/)etcd([:@/]|$)", 2379, "http:/health"),
    ("minio", r"(^|/)minio([:@/]|$)", 9000, "http:/minio/health/live"),
    ("memcached", r"(^|/)memcached([:@/]|$)", 11211, "tcp"),
)
_DB_ENGINES = tuple((e, re.compile(p, re.I), port, how) for e, p, port, how in DB_ENGINES)
#: Tools that carry a database's name in their image but hold no data.
_NOT_A_DB = re.compile(
    r"exporter|pgadmin|adminer|pgbouncer|pgpool|redisinsight|redis-commander|"
    r"mongo-express|phpmyadmin|backup|migrate|flyway|liquibase|pgweb|sentinel",
    re.I,
)
#: Environment keys kept while parsing. Everything else in Config.Env is dropped
#: on the spot, so the probe never holds another service's API keys in memory.
_VERSION_KEYS = {"PG_VERSION", "REDIS_VERSION", "MYSQL_VERSION", "MARIADB_VERSION",
                 "MONGO_VERSION", "CLICKHOUSE_VERSION", "ELASTIC_VERSION", "NEO4J_VERSION"}
_LOGIN_KEYS = {"POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "REDIS_PASSWORD"}
#: Native (non-container) engines, by the kernel's 15-character process name.
NATIVE_DB = {
    "postgres": ("postgres", 5432, "postgres"),
    "mysqld": ("mysql", 3306, "mysql"),
    "mariadbd": ("mysql", 3306, "mysql"),
    "redis-server": ("redis", 6379, "redis"),
    "valkey-server": ("redis", 6379, "redis"),
    "mongod": ("mongodb", 27017, "tcp"),
    "clickhouse-serv": ("clickhouse", 8123, "http:/ping"),
    "qdrant": ("qdrant", 6333, "http:/healthz"),
}


def detect_engine(image: str) -> tuple[str, int, str] | None:
    if not image or _NOT_A_DB.search(image):
        return None
    for engine, pattern, port, how in _DB_ENGINES:
        if pattern.search(image):
            return engine, port, how
    return None


def deep_login_allowed(project: str, allowed: str = "") -> bool:
    names = {p.strip() for p in (allowed or DEEP_DB_PROJECTS).split(",") if p.strip()}
    return "*" in names or (project or "standalone") in names


# ----------------------------------------------------------- docker state files --


class Container:
    """The fields the probe uses. Nothing else from the state files is kept."""

    __slots__ = (
        "id", "name", "image", "project", "service", "running", "restarting",
        "paused", "dead", "pid", "exit_code", "oom_killed", "started_at",
        "finished_at", "restart_count", "manual_stop",
        "restart_policy", "host_network", "ip", "published", "volumes", "engine",
        "engine_port", "engine_how", "version", "login",
    )

    def status(self) -> str:
        if self.running:
            return "paused" if self.paused else "running"
        if self.restarting:
            return "restarting"
        if self.dead:
            return "dead"
        return "exited" if self.started_at else "created"

    def should_be_running(self) -> bool:
        """A leftover test database is not an outage. One with a restart policy
        that nobody stopped by hand is."""
        return self.restart_policy in {"always", "unless-stopped", "on-failure"} and not self.manual_stop


def _version_from(env: dict[str, str], image: str) -> str:
    for key in _VERSION_KEYS:
        if env.get(key):
            match = re.match(r"\d+(\.\d+)*", env[key])
            if match:
                return match.group(0)
    if ":" in image and "@" not in image:
        tag = image.rsplit(":", 1)[1]
        match = re.search(r"\d+(\.\d+)*", tag)
        if match:
            return match.group(0)
    return "unknown"


def parse_container(cid: str, config: dict, hostconfig: dict, volumes_root: str,
                    allowed: str = "") -> Container:
    c = Container()
    state = config.get("State") or {}
    cfg = config.get("Config") or {}
    labels = cfg.get("Labels") or {}
    c.id = config.get("ID") or cid
    c.name = (config.get("Name") or cid[:12]).lstrip("/")
    c.image = cfg.get("Image") or ""
    c.project = labels.get("com.docker.compose.project") or ""
    c.service = labels.get("com.docker.compose.service") or ""
    c.running = bool(state.get("Running"))
    c.restarting = bool(state.get("Restarting"))
    c.paused = bool(state.get("Paused"))
    c.dead = bool(state.get("Dead"))
    c.pid = int(state.get("Pid") or 0)
    c.exit_code = int(state.get("ExitCode") or 0)
    c.oom_killed = bool(state.get("OOMKilled"))
    c.started_at = parse_docker_time(state.get("StartedAt"))
    c.finished_at = parse_docker_time(state.get("FinishedAt"))
    c.restart_count = int(config.get("RestartCount") or 0)
    c.manual_stop = bool(config.get("HasBeenManuallyStopped"))
    c.restart_policy = ((hostconfig.get("RestartPolicy") or {}).get("Name")) or "no"
    c.host_network = (hostconfig.get("NetworkMode") or "") == "host"

    network = config.get("NetworkSettings") or {}
    c.ip = ""
    for net in (network.get("Networks") or {}).values():
        if isinstance(net, dict) and net.get("IPAddress"):
            c.ip = net["IPAddress"]
            break
    c.published = ""
    for bindings in (network.get("Ports") or {}).values():
        for binding in bindings or []:
            if isinstance(binding, dict) and binding.get("HostPort"):
                c.published = f'{binding.get("HostIp") or "0.0.0.0"}:{binding["HostPort"]}'
                break
        if c.published:
            break

    # Only named volumes can be measured: they live under the Docker root the
    # probe has mounted. A bind mount elsewhere stays unmeasured, not zero.
    c.volumes = []
    for mount in (config.get("MountPoints") or {}).values():
        if not isinstance(mount, dict):
            continue
        name = mount.get("Name")
        if name and (mount.get("Type") or "volume") == "volume":
            c.volumes.append(os.path.join(volumes_root, name, "_data"))

    detected = detect_engine(c.image)
    c.engine, c.engine_port, c.engine_how = detected if detected else ("", 0, "")
    c.version = ""
    c.login = None
    if detected:
        wanted = _VERSION_KEYS | _LOGIN_KEYS
        env: dict[str, str] = {}
        for item in cfg.get("Env") or []:
            key, _, value = str(item).partition("=")
            if key in wanted:
                env[key] = value
        c.version = _version_from(env, c.image)
        # The credential survives this function only for an allowed project.
        if deep_login_allowed(c.project, allowed):
            login = {k: env[k] for k in _LOGIN_KEYS if k in env}
            args = [str(a) for a in (config.get("Args") or [])] + [str(a) for a in (cfg.get("Cmd") or [])]
            for i, arg in enumerate(args):
                if arg == "--requirepass" and i + 1 < len(args):
                    login.setdefault("REDIS_PASSWORD", args[i + 1])
                    break
            c.login = login
    return c


class DockerState:
    """Reads the state files, re-parsing a container only when its file changed."""

    def __init__(self, root: str = DOCKER_ROOT, allowed: str = "") -> None:
        self.containers_dir = os.path.join(root, "containers")
        self.volumes_root = os.path.join(root, "volumes")
        self.allowed = allowed
        self._cache: dict[str, tuple[tuple[int, int], Container]] = {}

    def load(self) -> list[Container]:
        entries = os.listdir(self.containers_dir)  # a failure here fails the collector
        seen: dict[str, tuple[tuple[int, int], Container]] = {}
        out: list[Container] = []
        for cid in entries:
            directory = os.path.join(self.containers_dir, cid)
            config_path = os.path.join(directory, "config.v2.json")
            host_path = os.path.join(directory, "hostconfig.json")
            try:
                stamp = (os.stat(config_path).st_mtime_ns, os.stat(host_path).st_mtime_ns)
            except OSError:
                continue  # removed between the listing and the stat
            cached = self._cache.get(cid)
            if cached and cached[0] == stamp:
                seen[cid] = cached
                out.append(cached[1])
                continue
            try:
                with open(config_path, encoding="utf-8") as handle:
                    config = json.load(handle)
                with open(host_path, encoding="utf-8") as handle:
                    hostconfig = json.load(handle)
                container = parse_container(cid, config, hostconfig, self.volumes_root, self.allowed)
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                log.debug("skipping container %s: %s", cid[:12], type(exc).__name__)
                continue
            seen[cid] = (stamp, container)
            out.append(container)
        self._cache = seen
        return out


# -------------------------------------------------------------- cgroup usage --


def cgroup_dir(root: str, cid: str) -> str | None:
    for candidate in (
        os.path.join(root, "system.slice", f"docker-{cid}.scope"),
        os.path.join(root, "docker", cid),
        os.path.join(root, "docker.slice", f"docker-{cid}.scope"),
    ):
        if os.path.isdir(candidate):
            return candidate
    return None


def read_io_stat(path: str) -> tuple[int, int] | None:
    raw = read_text(path)
    if raw is None:
        return None
    read_bytes = written = 0
    for line in raw.splitlines():
        for field in line.split()[1:]:
            key, _, value = field.partition("=")
            if key == "rbytes":
                read_bytes += int(value)
            elif key == "wbytes":
                written += int(value)
    return read_bytes, written


def read_net_dev(pid: int, proc: str = PROC_ROOT) -> tuple[int, int] | None:
    """Received and sent bytes in the container's network namespace, without lo."""
    raw = read_text(os.path.join(proc, str(pid), "net", "dev"))
    if raw is None:
        return None
    received = sent = 0
    for line in raw.splitlines()[2:]:
        name, _, rest = line.partition(":")
        if name.strip() == "lo":
            continue
        fields = rest.split()
        if len(fields) >= 9:
            received += int(fields[0])
            sent += int(fields[8])
    return received, sent


def collect_containers(exp: Exposition, containers: list[Container], cgroup_root: str = CGROUP_ROOT,
                       proc: str = PROC_ROOT) -> None:
    counts: dict[str, int] = {}
    for c in containers:
        status = c.status()
        counts[status] = counts.get(status, 0) + 1
        ident = {"container": c.name, "project": c.project or "standalone", "service": c.service or c.name}
        exp.gauge("spark_container_info", "One per container; the image and the current status are labels here only.",
                  1, image=c.image.split("@")[0], status=status, restart_policy=c.restart_policy, **ident)
        exp.gauge("spark_container_running", "1 while the container is running.", 1 if c.running else 0, **ident)
        exp.gauge("spark_container_expected_running",
                  "1 when the restart policy says this container should be up and nobody stopped it by hand.",
                  1 if c.should_be_running() else 0, **ident)
        exp.counter("spark_container_restarts_total", "Restarts Docker has made under the restart policy.",
                    c.restart_count, **ident)
        exp.gauge("spark_container_oom_killed", "1 when the container's last exit was the kernel OOM killer.",
                  1 if c.oom_killed else 0, **ident)
        exp.gauge("spark_container_exit_code", "Exit code of the last run.", c.exit_code, **ident)
        exp.gauge("spark_container_started_timestamp_seconds", "When the current or last run started.",
                  c.started_at, **ident)
        if not c.running:
            exp.gauge("spark_container_finished_timestamp_seconds", "When the last run ended.", c.finished_at, **ident)
        if not c.running:
            continue

        directory = cgroup_dir(cgroup_root, c.id)
        if directory:
            cpu = read_kv(os.path.join(directory, "cpu.stat"))
            if "usage_usec" in cpu:
                exp.counter("spark_container_cpu_seconds_total", "CPU time used.", cpu["usage_usec"] / 1e6, **ident)
            if "throttled_usec" in cpu:
                exp.counter("spark_container_cpu_throttled_seconds_total",
                            "Time held back by a CPU limit.", cpu["throttled_usec"] / 1e6, **ident)
            memory = read_int(os.path.join(directory, "memory.current"))
            detail = read_kv(os.path.join(directory, "memory.stat"))
            if memory is not None:
                exp.gauge("spark_container_memory_usage_bytes", "All memory charged, page cache included.", memory, **ident)
                exp.gauge("spark_container_memory_working_set_bytes",
                          "Usage minus reclaimable file cache: what would not come back under pressure.",
                          max(0, memory - detail.get("inactive_file", 0)), **ident)
            if "anon" in detail:
                exp.gauge("spark_container_memory_anon_bytes", "Anonymous memory (heap and stacks).", detail["anon"], **ident)
            if "file" in detail:
                exp.gauge("spark_container_memory_file_bytes", "File-backed page cache.", detail["file"], **ident)
            swap = read_int(os.path.join(directory, "memory.swap.current"))
            exp.gauge("spark_container_memory_swap_bytes", "Swap in use by this container.", swap, **ident)
            limit = read_int(os.path.join(directory, "memory.max"))
            exp.gauge("spark_container_memory_limit_bytes", "Memory limit. Absent when there is none.", limit, **ident)
            events = read_kv(os.path.join(directory, "memory.events"))
            if "oom_kill" in events:
                exp.counter("spark_container_oom_kills_total", "Processes the OOM killer took in this container.",
                            events["oom_kill"], **ident)
            exp.gauge("spark_container_pids", "Tasks in the container.",
                      read_int(os.path.join(directory, "pids.current")), **ident)
            io = read_io_stat(os.path.join(directory, "io.stat"))
            if io:
                exp.counter("spark_container_disk_read_bytes_total", "Bytes read from block devices.", io[0], **ident)
                exp.counter("spark_container_disk_written_bytes_total", "Bytes written to block devices.", io[1], **ident)
        # A host-network container shares the node's counters; attributing them
        # to it would be a lie, so it has no network series of its own.
        if not c.host_network and c.pid:
            net = read_net_dev(c.pid, proc)
            if net:
                exp.counter("spark_container_network_received_bytes_total", "Bytes received, loopback excluded.", net[0], **ident)
                exp.counter("spark_container_network_sent_bytes_total", "Bytes sent, loopback excluded.", net[1], **ident)
    for status in ("running", "restarting", "paused", "exited", "created", "dead"):
        exp.gauge("spark_containers", "Containers on this Spark, by status.", counts.get(status, 0), status=status)


# ---------------------------------------------------------- GPU by owner --

_GPU_LINE = re.compile(r'^dgx_gpu_process_memory_bytes\{pid="(\d+)",process="([^"]*)"\}\s+([0-9.eE+-]+)')
_DOCKER_CGROUP = re.compile(r"docker[-/]([0-9a-f]{64})")


def container_of_pid(pid: str, proc: str = PROC_ROOT) -> str | None:
    raw = read_text(os.path.join(proc, pid, "cgroup"))
    if raw is None:
        return None
    match = _DOCKER_CGROUP.search(raw)
    return match.group(1) if match else None


def collect_gpu_owners(exp: Exposition, containers: list[Container], gpu_url: str = GPU_METRICS_URL,
                       proc: str = PROC_ROOT, fetch=None) -> None:
    """Attribute GPU memory to containers.

    The GB10 has unified memory and no framebuffer counters, so "who holds the
    GPU memory" is the question nvidia-smi can only answer in PIDs. The cgroup
    of each PID turns the answer into a service name.
    """
    if fetch is None:
        def fetch(url: str) -> str:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 - fixed local URL
                return response.read().decode("utf-8", "replace")
    body = fetch(gpu_url)
    owners: dict[str, float] = {}
    host = 0.0
    seen_any = False
    for line in body.splitlines():
        match = _GPU_LINE.match(line)
        if not match:
            continue
        seen_any = True
        cid = container_of_pid(match.group(1), proc)
        if cid:
            owners[cid] = owners.get(cid, 0.0) + float(match.group(3))
        else:
            host += float(match.group(3))
    by_id = {c.id: c for c in containers}
    for cid, value in owners.items():
        c = by_id.get(cid)
        if c is None:
            host += value
            continue
        exp.gauge("spark_container_gpu_memory_bytes", "GPU memory held by this container's CUDA contexts.", value,
                  container=c.name, project=c.project or "standalone", service=c.service or c.name)
    if seen_any:
        exp.gauge("spark_host_gpu_memory_bytes", "GPU memory held by processes outside any container.", host)


# -------------------------------------------------------- processes --


class ProcessScanner:
    """Per-name CPU and memory across all processes, keeping only the top few.

    Grouped by the kernel's process name, so the label set stays small however
    many workers come and go. CPU is computed per PID between two scans, so a
    process that exits does not look like a negative rate.
    """

    def __init__(self, proc: str = PROC_ROOT) -> None:
        self.proc = proc
        self._prev: dict[int, int] = {}
        self._prev_at: float | None = None

    def scan(self) -> list[tuple[int, int, str, int, int]]:
        rows = []
        for entry in os.listdir(self.proc):
            if not entry.isdigit():
                continue
            raw = read_text(os.path.join(self.proc, entry, "stat"))
            if not raw:
                continue
            left, right = raw.find("("), raw.rfind(")")
            if left < 0 or right < 0:
                continue
            comm = raw[left + 1:right]
            fields = raw[right + 2:].split()
            try:
                ppid, ticks, rss = int(fields[1]), int(fields[11]) + int(fields[12]), int(fields[21])
            except (IndexError, ValueError):
                continue
            rows.append((int(entry), ppid, comm, ticks, rss * PAGE_SIZE))
        return rows

    def collect(self, exp: Exposition, rows: list[tuple[int, int, str, int, int]], now: float | None = None,
                top: int = TOP_PROCESSES) -> None:
        now = time.monotonic() if now is None else now
        elapsed = (now - self._prev_at) if self._prev_at is not None else None
        groups: dict[str, list[float]] = {}
        current: dict[int, int] = {}
        for pid, _ppid, comm, ticks, rss in rows:
            group = groups.setdefault(comm, [0.0, 0, 0])
            group[1] += rss
            group[2] += 1
            current[pid] = ticks
            if elapsed and pid in self._prev:
                group[0] += max(0, ticks - self._prev[pid]) / CLOCK_TICKS / elapsed
        self._prev, self._prev_at = current, now
        exp.gauge("spark_processes", "All processes on this Spark.", len(rows))
        by_rss = sorted(groups, key=lambda k: groups[k][1], reverse=True)[:top]
        by_cpu = sorted(groups, key=lambda k: groups[k][0], reverse=True)[:top]
        for comm in {*by_rss, *by_cpu}:
            cores, rss, count = groups[comm]
            exp.gauge("spark_process_rss_bytes", "Resident memory of processes with this name.", rss, comm=comm)
            exp.gauge("spark_process_count", "Processes with this name.", count, comm=comm)
            if elapsed:
                exp.gauge("spark_process_cpu_cores", "CPU cores used by processes with this name.", round(cores, 4), comm=comm)


# ------------------------------------------------------ database protocols --


class DbError(Exception):
    pass


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise DbError("connection closed")
        data += chunk
    return data


def ping_postgres(host: str, port: int, timeout: float) -> None:
    """An SSLRequest is answered before any login and is logged by nothing:
    PostgreSQL deliberately stays silent when a client hangs up after it."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(struct.pack("!II", 8, 80877103))
        if _recv_exact(sock, 1) not in (b"S", b"N"):
            raise DbError("not postgres")


def ping_redis(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(b"PING\r\n")
        # NOAUTH and DENIED are answers too: the server is up and is Redis.
        if sock.recv(64)[:1] not in (b"+", b"-"):
            raise DbError("not redis")


def ping_mysql(host: str, port: int, timeout: float) -> str:
    """The server speaks first, version included."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        header = _recv_exact(sock, 4)
        length = int.from_bytes(header[:3], "little")
        packet = _recv_exact(sock, min(length, 256))
        if packet[0] == 0xFF:
            return ""  # "too many connections" and friends: alive, version unknown
        return packet[1:].split(b"\0", 1)[0].decode("ascii", "replace")


def ping_http(host: str, port: int, path: str, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nUser-Agent: spark-probe\r\n\r\n".encode())
        first = sock.recv(64).decode("ascii", "replace")
    parts = first.split()
    if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) >= 500:
        raise DbError("http error")


def ping_tcp(host: str, port: int, timeout: float) -> None:
    with socket.create_connection((host, port), timeout=timeout):
        pass


def ping_engine(how: str, host: str, port: int, timeout: float) -> str:
    if how == "postgres":
        ping_postgres(host, port, timeout)
    elif how == "redis":
        ping_redis(host, port, timeout)
    elif how == "mysql":
        return ping_mysql(host, port, timeout)
    elif how.startswith("http:"):
        ping_http(host, port, how[5:], timeout)
    else:
        ping_tcp(host, port, timeout)
    return ""


class MiniPg:
    """Just enough of the PostgreSQL protocol to read statistics.

    Simple queries over the startup + trust/cleartext/md5/SCRAM-SHA-256 path,
    read only, with a short statement timeout. The session is opened on the
    `postgres` maintenance database so that it can never block a DROP DATABASE
    or a CREATE DATABASE ... TEMPLATE of an application or test database.
    """

    def __init__(self, host: str, port: int, user: str, password: str, database: str, timeout: float = 3.0) -> None:
        self.user, self.password = user, password
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        try:
            self._startup(database)
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "MiniPg":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.sock.sendall(b"X\x00\x00\x00\x04")
        except OSError:
            pass
        self.sock.close()

    def _read(self) -> tuple[bytes, bytes]:
        kind = _recv_exact(self.sock, 1)
        length = struct.unpack("!I", _recv_exact(self.sock, 4))[0]
        return kind, _recv_exact(self.sock, length - 4)

    def _send(self, kind: bytes, payload: bytes) -> None:
        self.sock.sendall(kind + struct.pack("!I", len(payload) + 4) + payload)

    @staticmethod
    def _error(payload: bytes) -> DbError:
        # Severity and SQLSTATE only: a message can quote user or query text.
        fields = {f[:1]: f[1:] for f in payload.split(b"\0") if f}
        return DbError(f"{fields.get(b'S', b'').decode()} {fields.get(b'C', b'').decode()}".strip())

    def _startup(self, database: str) -> None:
        params = {
            "user": self.user,
            "database": database,
            "application_name": "spark-probe",
            "options": "-c default_transaction_read_only=on -c statement_timeout=2500 -c lock_timeout=500",
            "client_encoding": "UTF8",
        }
        body = struct.pack("!I", 196608)
        for key, value in params.items():
            body += key.encode() + b"\0" + value.encode() + b"\0"
        body += b"\0"
        self.sock.sendall(struct.pack("!I", len(body) + 4) + body)
        nonce = client_first_bare = salted = auth_message = None
        while True:
            kind, payload = self._read()
            if kind == b"E":
                raise self._error(payload)
            if kind == b"Z":
                return
            if kind != b"R":
                continue
            code = struct.unpack("!I", payload[:4])[0]
            if code == 0:
                continue
            if code == 3:
                self._send(b"p", self.password.encode() + b"\0")
            elif code == 5:
                inner = hashlib.md5(self.password.encode() + self.user.encode()).hexdigest()  # noqa: S324 - the protocol's hash
                outer = hashlib.md5(inner.encode() + payload[4:8]).hexdigest()  # noqa: S324
                self._send(b"p", b"md5" + outer.encode() + b"\0")
            elif code == 10:
                if b"SCRAM-SHA-256\0" not in payload[4:]:
                    raise DbError("no supported SASL mechanism")
                nonce = base64.b64encode(secrets.token_bytes(18)).decode()
                client_first_bare = f"n=,r={nonce}"
                first = f"n,,{client_first_bare}".encode()
                self._send(b"p", b"SCRAM-SHA-256\0" + struct.pack("!I", len(first)) + first)
            elif code == 11:
                server_first = payload[4:].decode()
                attrs = dict(item.split("=", 1) for item in server_first.split(","))
                if not attrs["r"].startswith(nonce):
                    raise DbError("SCRAM nonce mismatch")
                salted = hashlib.pbkdf2_hmac("sha256", self.password.encode(), base64.b64decode(attrs["s"]), int(attrs["i"]))
                client_key = hmac.digest(salted, b"Client Key", "sha256")
                without_proof = f"c=biws,r={attrs['r']}"
                auth_message = f"{client_first_bare},{server_first},{without_proof}".encode()
                signature = hmac.digest(hashlib.sha256(client_key).digest(), auth_message, "sha256")
                proof = bytes(a ^ b for a, b in zip(client_key, signature))
                self._send(b"p", f"{without_proof},p={base64.b64encode(proof).decode()}".encode())
            elif code == 12:
                attrs = dict(item.split("=", 1) for item in payload[4:].decode().split(","))
                expected = hmac.digest(hmac.digest(salted, b"Server Key", "sha256"), auth_message, "sha256")
                if base64.b64decode(attrs.get("v", "")) != expected:
                    raise DbError("SCRAM server signature mismatch")
            else:
                raise DbError(f"unsupported authentication {code}")

    def query(self, sql: str) -> list[list[str | None]]:
        self._send(b"Q", sql.encode() + b"\0")
        rows: list[list[str | None]] = []
        error: DbError | None = None
        while True:
            kind, payload = self._read()
            if kind == b"D":
                count = struct.unpack("!H", payload[:2])[0]
                offset, row = 2, []
                for _ in range(count):
                    size = struct.unpack("!i", payload[offset:offset + 4])[0]
                    offset += 4
                    if size < 0:
                        row.append(None)
                    else:
                        row.append(payload[offset:offset + size].decode("utf-8", "replace"))
                        offset += size
                rows.append(row)
            elif kind == b"E":
                error = self._error(payload)
            elif kind == b"Z":
                if error:
                    raise error
                return rows


def redis_call(sock: socket.socket, reader, *args: str):
    command = f"*{len(args)}\r\n" + "".join(f"${len(a.encode())}\r\n{a}\r\n" for a in args)
    sock.sendall(command.encode())
    line = reader.readline()
    if not line:
        raise DbError("connection closed")
    if line[:1] == b"-":
        raise DbError(line[1:].split(b" ", 1)[0].decode("ascii", "replace"))
    if line[:1] == b"$":
        size = int(line[1:])
        return b"" if size < 0 else reader.read(size + 2)[:-2]
    return line[1:].rstrip()


def redis_info(host: str, port: int, password: str | None, timeout: float) -> dict[str, str]:
    """INFO, logging in only when the probe was allowed the password."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        reader = sock.makefile("rb")
        if password:
            redis_call(sock, reader, "AUTH", password)
        raw = redis_call(sock, reader, "INFO")
    info: dict[str, str] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            info[key] = value
    return info


# ------------------------------------------------------------- database stats --

_PG_TITLE = re.compile(rb"^postgres:\s+(?:[^:]+:\s+)?(\S+)\s+(\S+)\s+(\S+\(\d+\)|\[local\]|\S+)\s*(.*)$")
_PG_AUX = (b"checkpointer", b"background writer", b"walwriter", b"autovacuum", b"logical replication",
           b"io worker", b"stats collector", b"archiver", b"walreceiver", b"walsender", b"startup",
           b"parallel worker", b"walsummarizer", b"slotsync", b"pg_cron")


def classify_pg_title(cmdline: bytes) -> str | None:
    """PostgreSQL rewrites each backend's title to `postgres: user db host state`.

    That gives connections by state for ANY PostgreSQL, with no login at all.
    Returns None for the postmaster and the background processes.
    """
    title = cmdline.replace(b"\0", b" ").strip()
    if not title.startswith(b"postgres:"):
        return None
    rest = title[len(b"postgres:"):].strip()
    if rest.startswith(_PG_AUX):
        return None
    match = _PG_TITLE.match(title)
    if not match:
        return None
    state = match.group(4).strip().lower()
    if state.startswith(b"idle in transaction"):
        return "idle_in_transaction"
    if state.startswith(b"idle"):
        return "idle"
    if state.startswith((b"authentication", b"startup")):
        return "connecting"
    return "active"


def pg_connections_from_titles(directory: str, proc: str = PROC_ROOT) -> dict[str, int] | None:
    raw = read_text(os.path.join(directory, "cgroup.procs"))
    if raw is None:
        return None
    counts = {"active": 0, "idle": 0, "idle_in_transaction": 0, "connecting": 0}
    titled = False
    for pid in raw.split():
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as handle:
                cmdline = handle.read(512)
        except OSError:
            continue
        if cmdline.startswith(b"postgres:"):
            titled = True  # auxiliary processes prove that titles are switched on
        state = classify_pg_title(cmdline)
        if state:
            counts[state] += 1
    return counts if titled else None


def listening_ports(pid: int, proc: str = PROC_ROOT) -> set[int]:
    """TCP ports in LISTEN inside the process's own network namespace."""
    ports: set[int] = set()
    for name in ("tcp", "tcp6"):
        raw = read_text(os.path.join(proc, str(pid), "net", name))
        if raw is None:
            continue
        for line in raw.splitlines()[1:]:
            fields = line.split()
            if len(fields) > 3 and fields[3] == "0A":
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
    return ports


def dial_target(c: Container, proc: str = PROC_ROOT) -> tuple[str, int] | None:
    host = "127.0.0.1" if c.host_network else c.ip
    if not host:
        return None
    port = c.engine_port
    if c.pid:
        ports = listening_ports(c.pid, proc)
        # A custom port is believed only when the default is not listening.
        if ports and port not in ports:
            port = min(ports)
    return host, port


def directory_size(path: str, deadline: float) -> int | None:
    """Disk blocks used, not apparent size. None when the walk ran out of time,
    because a partial total would look like a database that shrank."""
    total = 0
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            return None
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += info.st_blocks * 512
                    if stat_module.S_ISDIR(info.st_mode):
                        stack.append(entry.path)
        except OSError:
            continue
    return total


class DbSizes(threading.Thread):
    """Walks database volumes in the background: it must never delay a cycle."""

    daemon = True

    def __init__(self) -> None:
        super().__init__(name="db-sizes")
        self._lock = threading.Lock()
        self._targets: dict[str, list[str]] = {}
        self._sizes: dict[str, int] = {}

    def set_targets(self, targets: dict[str, list[str]]) -> None:
        with self._lock:
            self._targets = targets

    def size_of(self, name: str) -> int | None:
        with self._lock:
            return self._sizes.get(name)

    def walk_once(self) -> None:
        with self._lock:
            targets = dict(self._targets)
        fresh: dict[str, int] = {}
        for name, paths in targets.items():
            deadline = time.monotonic() + DATA_WALK_BUDGET_S
            total = 0
            for path in paths:
                size = directory_size(path, deadline)
                if size is None:
                    total = -1
                    break
                total += size
            if total >= 0:
                fresh[name] = total
        with self._lock:
            # A walk that timed out keeps its last good value rather than vanish.
            self._sizes = {**{k: v for k, v in self._sizes.items() if k in targets}, **fresh}

    def run(self) -> None:
        # The first cycle names the volumes a moment after this thread starts.
        # Without this wait, sizes stay blank for the first whole interval.
        for _ in range(60):
            with self._lock:
                if self._targets:
                    break
            time.sleep(1)
        while True:
            try:
                self.walk_once()
            except Exception:  # noqa: BLE001 - a sizing thread must never die
                log.warning("database size walk failed", exc_info=True)
            time.sleep(DATA_WALK_INTERVAL_S)


class DatabaseCollector:
    def __init__(self, cgroup_root: str = CGROUP_ROOT, proc: str = PROC_ROOT) -> None:
        self.cgroup_root = cgroup_root
        self.proc = proc
        self.sizes = DbSizes()
        self._deep: dict[str, tuple[float, dict]] = {}
        self._db_sizes: dict[str, tuple[float, list]] = {}
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="db")

    def start(self) -> None:
        self.sizes.start()

    # --- one instance -------------------------------------------------------
    def _probe_one(self, c: Container) -> dict:
        result: dict = {"container": c, "up": 0}
        target = dial_target(c, self.proc)
        if c.running and target:
            started = time.monotonic()
            try:
                version = ping_engine(c.engine_how, target[0], target[1], DB_DIAL_TIMEOUT_S)
                result["up"] = 1
                result["probe_seconds"] = time.monotonic() - started
                if version:
                    result["version"] = version
            except (OSError, DbError, ValueError, IndexError):
                result["up"] = 0
        if c.running and c.engine == "postgres":
            directory = cgroup_dir(self.cgroup_root, c.id)
            if directory:
                result["titles"] = pg_connections_from_titles(directory, self.proc)
        if result["up"] and target:
            if c.engine == "postgres" and c.login and c.login.get("POSTGRES_PASSWORD"):
                result["deep"] = self._postgres_deep(c, target)
            elif c.engine == "redis":
                result["deep"] = self._redis_deep(c, target)
        return result

    def _postgres_deep(self, c: Container, target: tuple[str, int]) -> dict | None:
        now = time.monotonic()
        cached = self._deep.get(c.id)
        if cached and now - cached[0] < DEEP_INTERVAL_S:
            return cached[1]
        user = c.login.get("POSTGRES_USER") or "postgres"
        password = c.login["POSTGRES_PASSWORD"]
        try:
            try:
                session = MiniPg(target[0], target[1], user, password, "postgres")
            except DbError:
                session = MiniPg(target[0], target[1], user, password, c.login.get("POSTGRES_DB") or user)
            with session as pg:
                deep = self._postgres_read(pg, c, now)
        except (OSError, DbError, ValueError, IndexError, KeyError) as exc:
            log.info("deep stats failed for %s: %s", c.name, type(exc).__name__ if isinstance(exc, OSError) else exc)
            self._deep.pop(c.id, None)
            return {"error": 1}
        self._deep[c.id] = (now, deep)
        return deep

    def _postgres_read(self, pg: MiniPg, c: Container, now: float) -> dict:
        settings = pg.query(
            "SELECT current_setting('server_version'), current_setting('max_connections')::int,"
            " extract(epoch from now()-pg_postmaster_start_time()), pg_is_in_recovery()"
        )[0]
        totals = pg.query(
            "SELECT sum(xact_commit), sum(xact_rollback), sum(blks_read), sum(blks_hit),"
            " sum(tup_returned), sum(tup_fetched), sum(tup_inserted), sum(tup_updated), sum(tup_deleted),"
            " sum(conflicts), sum(temp_bytes), sum(deadlocks) FROM pg_stat_database"
        )[0]
        activity = pg.query(
            "SELECT state, count(*) FROM pg_stat_activity WHERE backend_type='client backend'"
            " AND pid<>pg_backend_pid() GROUP BY 1"
        )
        pressure = pg.query(
            "SELECT coalesce(extract(epoch from max(now()-xact_start)),0),"
            " count(*) FILTER (WHERE wait_event_type='Lock')"
            " FROM pg_stat_activity WHERE backend_type='client backend' AND pid<>pg_backend_pid()"
        )[0]
        deep: dict = {
            "version": re.match(r"\S+", settings[0]).group(0),
            "max_connections": float(settings[1]),
            "uptime": float(settings[2]),
            "in_recovery": settings[3] == "t",
            "totals": [float(x) if x is not None else None for x in totals],
            "activity": {(row[0] or "other"): float(row[1]) for row in activity},
            "longest_xact": float(pressure[0]),
            "lock_waits": float(pressure[1]),
        }
        if deep["in_recovery"]:
            deep["replica_lag_seconds"] = float(pg.query(
                "SELECT coalesce(extract(epoch from now()-pg_last_xact_replay_timestamp()),0)"
            )[0][0])
        else:
            deep["wal_bytes"] = float(pg.query("SELECT pg_wal_lsn_diff(pg_current_wal_lsn(),'0/0')")[0][0])
            replicas = pg.query(
                "SELECT count(*), coalesce(max(pg_wal_lsn_diff(pg_current_wal_lsn(),replay_lsn)),0) FROM pg_stat_replication"
            )[0]
            deep["replicas"], deep["replica_lag_bytes"] = float(replicas[0]), float(replicas[1])
        cached = self._db_sizes.get(c.id)
        if not cached or now - cached[0] >= SIZE_QUERY_INTERVAL_S:
            cached = (now, pg.query(
                "SELECT datname, pg_database_size(oid) FROM pg_database WHERE datallowconn"
                " ORDER BY 2 DESC LIMIT 20"
            ))
            self._db_sizes[c.id] = cached
        deep["databases"] = cached[1]
        return deep

    def _redis_deep(self, c: Container, target: tuple[str, int]) -> dict | None:
        password = (c.login or {}).get("REDIS_PASSWORD")
        try:
            return {"info": redis_info(target[0], target[1], password, DB_DIAL_TIMEOUT_S)}
        except DbError:
            return None  # NOAUTH without an allowed password: liveness only
        except (OSError, ValueError, IndexError):
            return {"error": 1}

    # --- the whole node -----------------------------------------------------
    def collect(self, exp: Exposition, containers: list[Container], native: list[Container]) -> None:
        instances: list[Container] = []
        stopped: dict[str, int] = {}
        for c in containers:
            if not c.engine:
                continue
            if c.running or c.restarting or c.should_be_running():
                instances.append(c)
            else:
                stopped[c.engine] = stopped.get(c.engine, 0) + 1
        self.sizes.set_targets({c.name: c.volumes for c in instances if c.volumes})
        results = list(self._pool.map(self._probe_one, instances + native))
        for engine, count in stopped.items():
            exp.gauge("spark_db_stopped_instances",
                      "Stopped database containers nobody expects to be up (leftovers).", count, engine=engine)
        exp.gauge("spark_db_instances", "Databases detected on this Spark and expected to be up.", len(results))
        for result in results:
            self._emit(exp, result)

    def _emit(self, exp: Exposition, result: dict) -> None:
        c: Container = result["container"]
        deep = result.get("deep") or {}
        ident = {"container": c.name, "engine": c.engine, "project": c.project or ("host" if c.id.startswith("native") else "standalone")}
        version = deep.get("version") or result.get("version") or c.version or "unknown"
        if c.engine == "redis" and deep.get("info", {}).get("redis_version"):
            version = deep["info"]["redis_version"]
        level = "login" if deep and "error" not in deep and c.engine == "postgres" else (
            "info" if deep and "error" not in deep else "liveness")
        exp.gauge("spark_db_info",
                  "One per detected database. `stats` says how much is known: liveness, info (no login needed) or login.",
                  1, version=version, image=c.image.split("@")[0], published=c.published, stats=level, **ident)
        exp.gauge("spark_db_up", "1 when the engine answered in its own protocol this cycle.", result["up"], **ident)
        exp.gauge("spark_db_probe_seconds", "Round trip of the protocol probe.", result.get("probe_seconds"), **ident)
        if deep.get("error"):
            exp.gauge("spark_db_stats_error", "1 when a statistics read failed this cycle.", 1, **ident)
        else:
            exp.gauge("spark_db_stats_error", "1 when a statistics read failed this cycle.", 0, **ident)
        exp.gauge("spark_db_data_bytes", "Disk used by the database's named volumes.", self.sizes.size_of(c.name), **ident)
        titles = result.get("titles")
        if titles is not None:
            for state, count in titles.items():
                exp.gauge("spark_db_connections", "Client connections by state.", count, state=state, **ident)
        if c.engine == "postgres" and "totals" in deep:
            self._emit_postgres(exp, ident, deep)
        elif c.engine == "redis" and "info" in deep:
            self._emit_redis(exp, ident, deep["info"])

    @staticmethod
    def _emit_postgres(exp: Exposition, ident: dict, deep: dict) -> None:
        t = deep["totals"]
        exp.gauge("spark_db_connections_max", "Connection limit.", deep["max_connections"], **ident)
        exp.gauge("spark_db_uptime_seconds", "Time since the server started.", deep["uptime"], **ident)
        exp.counter("spark_db_transactions_total", "Transactions finished.", t[0], outcome="commit", **ident)
        exp.counter("spark_db_transactions_total", "Transactions finished.", t[1], outcome="rollback", **ident)
        exp.counter("spark_db_cache_reads_total", "Page or key lookups, by whether the cache held them.", t[3], source="hit", **ident)
        exp.counter("spark_db_cache_reads_total", "Page or key lookups, by whether the cache held them.", t[2], source="miss", **ident)
        for offset, op in ((4, "returned"), (5, "fetched"), (6, "inserted"), (7, "updated"), (8, "deleted")):
            exp.counter("spark_db_tuples_total", "Rows handled, by operation.", t[offset], op=op, **ident)
        exp.counter("spark_db_conflicts_total", "Queries cancelled by recovery conflicts.", t[9], **ident)
        exp.counter("spark_db_temp_bytes_total", "Bytes queries spilled to temporary files.", t[10], **ident)
        exp.counter("spark_db_deadlocks_total", "Deadlocks detected.", t[11], **ident)
        exp.gauge("spark_db_longest_transaction_seconds", "Age of the oldest open client transaction.", deep["longest_xact"], **ident)
        exp.gauge("spark_db_lock_waits", "Client sessions waiting on a lock.", deep["lock_waits"], **ident)
        exp.counter("spark_db_wal_bytes_total", "Write-ahead log generated.", deep.get("wal_bytes"), **ident)
        exp.gauge("spark_db_replicas", "Streaming replicas attached.", deep.get("replicas"), **ident)
        exp.gauge("spark_db_replica_lag_bytes", "Largest replay lag of an attached replica.", deep.get("replica_lag_bytes"), **ident)
        exp.gauge("spark_db_replica_lag_seconds", "Replay lag when this server is itself a replica.", deep.get("replica_lag_seconds"), **ident)
        for row in deep.get("databases") or []:
            exp.gauge("spark_db_size_bytes", "Size of one database inside the instance (the 20 largest).",
                      float(row[1]), datname=row[0], **ident)

    @staticmethod
    def _emit_redis(exp: Exposition, ident: dict, info: dict) -> None:
        def number(key: str) -> float | None:
            try:
                return float(info[key])
            except (KeyError, ValueError):
                return None

        exp.gauge("spark_db_connections", "Client connections by state.", number("connected_clients"), state="active", **ident)
        exp.gauge("spark_db_uptime_seconds", "Time since the server started.", number("uptime_in_seconds"), **ident)
        exp.gauge("spark_db_memory_used_bytes", "Memory the engine reports using.", number("used_memory"), **ident)
        limit = number("maxmemory")
        exp.gauge("spark_db_memory_limit_bytes", "Configured memory limit; absent when unlimited.", limit if limit else None, **ident)
        exp.counter("spark_db_commands_total", "Commands processed.", number("total_commands_processed"), **ident)
        exp.counter("spark_db_cache_reads_total", "Page or key lookups, by whether the cache held them.", number("keyspace_hits"), source="hit", **ident)
        exp.counter("spark_db_cache_reads_total", "Page or key lookups, by whether the cache held them.", number("keyspace_misses"), source="miss", **ident)
        exp.counter("spark_db_evicted_keys_total", "Keys evicted because memory ran out.", number("evicted_keys"), **ident)
        exp.counter("spark_db_expired_keys_total", "Keys removed by their expiry.", number("expired_keys"), **ident)
        exp.counter("spark_db_rejected_connections_total", "Connections refused at maxclients.", number("rejected_connections"), **ident)
        exp.gauge("spark_db_last_save_timestamp_seconds", "When the last snapshot reached disk.", number("rdb_last_save_time"), **ident)
        keys = sum(int(m.group(1)) for v in info.values() for m in [re.match(r"keys=(\d+)", v)] if m)
        exp.gauge("spark_db_keys", "Keys across all logical databases.", keys, **ident)


def native_instances(rows: list[tuple[int, int, str, int, int]], proc: str = PROC_ROOT) -> list[Container]:
    """Databases installed directly on the host rather than in Docker."""
    found: dict[str, Container] = {}
    comms = {pid: comm for pid, _ppid, comm, _t, _r in rows}
    for pid, ppid, comm, _ticks, _rss in rows:
        spec = NATIVE_DB.get(comm)
        if not spec or comms.get(ppid) == comm:
            continue  # a worker forked from the postmaster, not an instance
        cgroup = read_text(os.path.join(proc, str(pid), "cgroup")) or ""
        if _DOCKER_CGROUP.search(cgroup) or "containerd" in cgroup or "kubepods" in cgroup:
            continue
        engine, port, how = spec
        c = Container()
        for slot in Container.__slots__:
            setattr(c, slot, None)
        c.id, c.name, c.image, c.project, c.service = f"native-{pid}", f"host-{engine}", "", "", ""
        c.running, c.restarting, c.paused, c.dead = True, False, False, False
        c.pid, c.host_network, c.ip, c.published, c.volumes = pid, True, "", "", []
        c.engine, c.engine_port, c.engine_how, c.version, c.login = engine, port, how, "unknown", None
        c.restart_policy, c.manual_stop = "always", False
        found.setdefault(c.name, c)
    return list(found.values())


# ------------------------------------------------------------- kernel events --

#: The kernel's own words for hardware and resource trouble. These are the
#: failures that kill a long-running model and that no metric reports.
KERNEL_PATTERNS = {
    "gpu_xid": re.compile(r"NVRM: Xid"),
    "gpu_fallen_off": re.compile(r"NVRM: .*(fallen off the bus|GPU at PCI.*has fallen)"),
    "oom_kill": re.compile(r"Out of memory: Killed process|Memory cgroup out of memory|oom-kill:constraint"),
    "hung_task": re.compile(r"blocked for more than \d+ seconds"),
    "lockup": re.compile(r"soft lockup|hard LOCKUP|rcu.*(stall|self-detected)"),
    "machine_check": re.compile(r"Hardware Error|\bmce:|\bEDAC\b.*(error|CE|UE)|\bRAS:|SError Interrupt|arm-smmu.*(fault|Unhandled)"),
    # "AER: enabled with IRQ n" is every boot, not an error.
    "pcie_aer": re.compile(r"AER: (Corrected|Uncorrected|Multiple|Correctable|Fatal|Non-Fatal)|PCIe Bus Error"),
    "nvme_error": re.compile(r"nvme nvme\d+:.*(timeout|reset|Abort|I/O \d+.*error)|nvme\d+.*I/O .*error"),
    "disk_error": re.compile(r"I/O error, dev |Buffer I/O error|critical medium error"),
    "fs_error": re.compile(r"EXT4-fs error|EXT4-fs warning.*(error|abort)|Remounting filesystem read-only|JBD2:.*(error|abort)"),
    "roce_error": re.compile(r"mlx5_core.*(fatal|syndrome|health|FW fatal|error|failed)|mlx5_ib.*(error|failed)"),
    "link_down": re.compile(r"(mlx5_core|igc|igb|e1000e|r8169|aqc|atlantic).*(Link down|link down|Link is Down)"),
    "thermal": re.compile(r"(critical temperature|temperature above threshold|thermal.*(critical|shutdown)|clock throttled)"),
    "segfault": re.compile(r"segfault at |general protection (fault|ip)"),
}
_XID = re.compile(r"NVRM: Xid \([^)]*\):\s*(\d+)")


def classify_kernel_message(message: str) -> list[str]:
    return [kind for kind, pattern in KERNEL_PATTERNS.items() if pattern.search(message)]


class KernelEvents(threading.Thread):
    """Counts kernel messages since boot by replaying the ring from its start.

    The counters are the same after a restart of this process, so a restart is
    not an event. The first read covers the boot itself, which is when PCIe and
    driver errors tend to happen. Every kind is published from zero so that the
    first real event is an increase Prometheus can see.
    """

    daemon = True

    def __init__(self, path: str = KMSG_PATH) -> None:
        super().__init__(name="kmsg")
        self.path = path
        self._lock = threading.Lock()
        self.counts = {kind: 0 for kind in KERNEL_PATTERNS}
        self.last: dict[str, float] = {}
        self.xid: dict[str, int] = {}
        self.available = False
        self.boot = self._boot_time()

    @staticmethod
    def _boot_time() -> float:
        for line in (read_text(os.path.join(PROC_ROOT, "stat")) or "").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
        return 0.0

    def record(self, line: str) -> None:
        prefix, _, message = line.partition(";")
        fields = prefix.split(",")
        if len(fields) < 3:
            return
        try:
            facility = int(fields[0]) >> 3
            micros = int(fields[2])
        except ValueError:
            return
        if facility != 0:
            return  # userspace can write to /dev/kmsg; only the kernel counts
        for kind in classify_kernel_message(message):
            with self._lock:
                self.counts[kind] += 1
                self.last[kind] = self.boot + micros / 1e6
                if kind == "gpu_xid":
                    match = _XID.search(message)
                    if match:
                        self.xid[match.group(1)] = self.xid.get(match.group(1), 0) + 1

    def run(self) -> None:
        while True:
            try:
                fd = os.open(self.path, os.O_RDONLY)
            except OSError as exc:
                log.warning("cannot read the kernel log: %s", exc.strerror)
                time.sleep(300)
                continue
            self.available = True
            with os.fdopen(fd, "rb", buffering=0) as stream:
                while True:
                    try:
                        record = stream.read(8192)
                    except BrokenPipeError:
                        continue  # the ring wrapped past us; the next read resumes
                    except OSError:
                        break
                    if not record:
                        break
                    self.record(record.decode("utf-8", "replace"))
            self.available = False
            time.sleep(5)

    def collect(self, exp: Exposition) -> None:
        if not self.available:
            raise RuntimeError("kernel log unavailable")
        with self._lock:
            for kind, count in self.counts.items():
                exp.counter("spark_kernel_events_total", "Kernel log messages of this kind since boot.", count, kind=kind)
            for kind, at in self.last.items():
                exp.gauge("spark_kernel_event_last_timestamp_seconds", "When this kind last appeared.", at, kind=kind)
            for code, count in self.xid.items():
                exp.counter("spark_gpu_xid_total", "NVIDIA Xid errors, by code.", count, xid=code)


# ------------------------------------------------------------------ RoCE --

#: rx_* request counts show load; the rest are retransmits, congestion and errors.
ROCE_COUNTERS = {
    "out_of_sequence", "packet_seq_err", "local_ack_timeout_err", "rnr_nak_retry_err",
    "roce_adp_retrans", "roce_adp_retrans_to", "roce_slow_restart", "roce_slow_restart_cnps",
    "implied_nak_seq_err", "duplicate_request", "np_cnp_sent", "np_ecn_marked_roce_packets",
    "rp_cnp_handled", "rp_cnp_ignored", "req_cqe_error", "resp_cqe_error",
    "req_transport_retries_exceeded", "req_rnr_retries_exceeded", "resp_local_length_error",
    "req_remote_access_errors", "resp_remote_access_errors", "out_of_buffer",
    "rx_icrc_encapsulated", "rx_read_requests", "rx_write_requests",
}


def collect_roce(exp: Exposition, root: str = IB_ROOT) -> None:
    if not os.path.isdir(root):
        return  # a node with no RDMA hardware has nothing to report
    for device in sorted(os.listdir(root)):
        ports = os.path.join(root, device, "ports")
        if not os.path.isdir(ports):
            continue
        for port in sorted(os.listdir(ports)):
            state = read_text(os.path.join(ports, port, "state")) or ""
            exp.gauge("spark_roce_port_active", "1 when the RDMA port is ACTIVE.", 1 if "ACTIVE" in state else 0,
                      device=device, port=port)
            if "ACTIVE" not in state:
                continue  # the p0 ports are down by design; zeros would only add series
            directory = os.path.join(ports, port, "hw_counters")
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                if name not in ROCE_COUNTERS:
                    continue
                exp.counter("spark_roce_hw_counter_total", "RoCE hardware counter from the NIC.",
                            read_int(os.path.join(directory, name)), device=device, port=port, counter=name)


# -------------------------------------------------------- identity and fleet --


def collect_identity(exp: Exposition, path: str = DGX_RELEASE) -> None:
    fields: dict[str, str] = {}
    for line in (read_text(path) or "").splitlines():
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip().strip('"')
    exp.gauge("spark_node_info", "Identity of this machine.", 1,
              hostname=socket.gethostname(),
              product=fields.get("DGX_NAME", "unknown"),
              dgx_os=fields.get("DGX_SWBUILD_VERSION", "unknown"),
              serial=fields.get("DGX_SERIAL_NUMBER", "unknown"))


def collect_fleet(exp: Exposition, path: str = FLEET_FILE) -> None:
    """The head publishes what fleet discovery knows, so that a Spark waiting for
    enrolment shows on the dashboard. On any other node the file does not exist."""
    raw = read_text(path)
    if raw is None:
        return
    doc = json.loads(raw)
    exp.gauge("spark_fleet_last_discovery_timestamp_seconds", "When fleet discovery last completed.", doc.get("generated_at"))
    for node, member in (doc.get("members") or {}).items():
        # `member`, not `node`: Prometheus sets `node` to the Spark that published
        # this file (the head) and would rename a second one to exported_node.
        exp.gauge("spark_fleet_member", "One per known Spark; `state` says whether it is being scraped.", 1,
                  member=node, hostname=member.get("hostname") or "unknown", address=member.get("address") or "",
                  member_role=member.get("role") or "member", state=member.get("state") or "unknown")


# ---------------------------------------------------------------------- loop --


class Probe:
    def __init__(self) -> None:
        self.docker = DockerState()
        self.scanner = ProcessScanner()
        self.databases = DatabaseCollector()
        self.kernel = KernelEvents()

    def start_background(self) -> None:
        self.kernel.start()
        self.databases.start()

    def cycle(self) -> str:
        exp = Exposition()
        started = time.monotonic()
        containers: list[Container] = []
        rows: list[tuple[int, int, str, int, int]] = []

        def run(name, fn) -> None:
            begun = time.monotonic()
            ok = 1
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - one blind spot must not blank the rest
                ok = 0
                log.warning("collector %s failed: %s", name, type(exc).__name__)
                log.debug("collector %s", name, exc_info=True)
            exp.gauge("spark_probe_collector_success", "1 when this collector ran cleanly in the last cycle.", ok, collector=name)
            exp.gauge("spark_probe_collector_seconds", "How long this collector took.", round(time.monotonic() - begun, 4), collector=name)

        def load_containers() -> None:
            containers.extend(self.docker.load())
            collect_containers(exp, containers)

        def processes() -> None:
            rows.extend(self.scanner.scan())
            self.scanner.collect(exp, rows)

        run("identity", lambda: collect_identity(exp))
        run("containers", load_containers)
        run("processes", processes)
        run("gpu_owners", lambda: collect_gpu_owners(exp, containers))
        run("databases", lambda: self.databases.collect(exp, containers, native_instances(rows)))
        run("roce", lambda: collect_roce(exp))
        run("kernel", lambda: self.kernel.collect(exp))
        run("fleet", lambda: collect_fleet(exp))
        exp.gauge("spark_probe_last_run_timestamp_seconds", "When this file was last written.", time.time())
        exp.gauge("spark_probe_cycle_seconds", "How long the whole cycle took.", round(time.monotonic() - started, 4))
        return exp.render()


def main() -> int:
    logging.basicConfig(level=_env("SPARK_PROBE_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    probe = Probe()
    probe.start_background()
    if _env("SPARK_PROBE_ONCE", "") == "1":
        time.sleep(1.5)  # lets the kernel ring replay before the only cycle
        probe.cycle()    # primes the CPU rate
        time.sleep(1.0)
        write_atomically(TEXTFILE, probe.cycle())
        return 0
    os.makedirs(os.path.dirname(TEXTFILE), exist_ok=True)
    log.info("writing %s every %.0f s; database logins allowed for: %s", TEXTFILE, INTERVAL_S, DEEP_DB_PROJECTS or "(none)")
    while True:
        begun = time.monotonic()
        try:
            write_atomically(TEXTFILE, probe.cycle())
        except Exception:  # noqa: BLE001 - staleness is alerted; a dead loop would be silent
            log.exception("cycle failed")
        time.sleep(max(1.0, INTERVAL_S - (time.monotonic() - begun)))


if __name__ == "__main__":
    raise SystemExit(main())
