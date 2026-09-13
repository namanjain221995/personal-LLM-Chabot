"""Two-node DGX Spark vLLM cluster: user settings, host detection, engine args.

The dgx-spark profile can run its main model across two DGX Spark hosts with
vLLM's native multiprocessing executor (``--nnodes 2``).  Node 1 keeps the
existing ``vllm`` service (switched to host networking by
``compose/compose.cluster-dgx-spark.yaml``); Node 2 runs the headless worker
from ``compose/compose.cluster-worker.yaml``.  Both nodes must build the same
engine configuration, so the launcher renders ONE ``CLUSTER_ENGINE_ARGS``
string that both Compose files interpolate verbatim.

Since 2026-08-25 ``CLUSTER_MODE=auto`` (the default) discovers the second
DGX Spark on this node's direct RoCE links: an ``up`` interface carrying an
IPv4 address and backed by an RDMA device is a cluster link, the peer address
comes from the neighbour table (or NVIDIA's ``.1``/``.2`` convention) and is
verified over ssh before dual mode is generated.  Anything short of that
falls back to the single-node deployment with a one-line reason.

Everything here is pure and injectable: detection shells out only through the
``runner``/``sysfs``/``probe`` parameters so tests never touch real interfaces.
"""

from __future__ import annotations

import getpass
import ipaddress
import json
import re
import shlex
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, NamedTuple, Sequence

from .errors import TechSaraError
from .utils import run_command

#: Same shape as ``hardware.Command``: ``(argv, timeout) -> (rc, stdout, stderr)``.
Command = Callable[[Sequence[str], float], tuple[int, str, str]]

CLUSTER_MODES = ("auto", "single", "dual")
DEFAULT_CLUSTER_MODE = "auto"
CLUSTER_NODES = 2
CLUSTER_PROFILE_ID = "dgx-spark"
#: Head (node-rank 0) overlay, layered last by the launcher in dual mode.
CLUSTER_COMPOSE_OVERLAY = "compose/compose.cluster-dgx-spark.yaml"
DEFAULT_MASTER_PORT = 29501
DEFAULT_TENSOR_PARALLEL_SIZE = 2
DEFAULT_PIPELINE_PARALLEL_SIZE = 1
DEFAULT_GPU_MEMORY_UTILIZATION = "0.30"
GPU_MEMORY_UTILIZATION_RANGE = (0.05, 0.95)
DEFAULT_NCCL_DEBUG = "INFO"
NCCL_DEBUG_LEVELS = ("VERSION", "WARN", "INFO", "TRACE")
#: The MTP draft this checkpoint ships (``Qwen3_5MoeMTP``), as the value
#: ``CLUSTER_SPECULATIVE_CONFIG`` takes to turn speculative decoding ON.
MTP_SPECULATIVE_CONFIG = '{"method":"mtp","num_speculative_tokens":1}'
#: Speculative decoding is OFF unless .env asks for it. Until 2026-09-11 the
#: default was the MTP draft above. It was withdrawn during the GDN fault
#: investigation (docs/ISSUE/), and it stays off for two measured reasons:
#: without spec-decode this vLLM build (0.26.1rc1.dev77) keeps FULL CUDA
#: graphs for decode instead of downgrading to PIECEWISE, and single-stream
#: decode went from 69 to 101 tok/s (docs/CLUSTER.md, "Engine tuning"); and
#: the draft's mixed spec/non-spec batches were one path into the build's
#: GDN `misaligned address` fault. That fault is NOT closed by this switch --
#: the same day's soak reproduced it in the GDN prefill kernel on an ordinary
#: mixed prefill+decode batch (docs/ISSUE/gdn-spec-decode-remediation-
#: 2026-09-11.md, section 1.1); the durable fix is a vLLM build with the
#: later GDN kernel fixes. Setting the key to MTP_SPECULATIVE_CONFIG opts
#: back in; the launcher never does it silently.
DEFAULT_SPECULATIVE_CONFIG = ""
DEFAULT_MAX_NUM_BATCHED_TOKENS = 8192
# Explicit per-node KV budget. On GB10 unified memory "free GPU memory" is free
# system memory and moves while vLLM profiles (page cache from the weight read
# is released), which trips vLLM's "free memory grew during profiling" assertion
# and kills the start-up. An explicit --kv-cache-memory-bytes makes vLLM skip
# that profiling path entirely (observed 2026-08-25).
DEFAULT_KV_CACHE_MEMORY_GIB = 16
KV_CACHE_MEMORY_GIB_RANGE = (2, 96)
#: The KV cache element type both nodes serve with; the launcher sizes the pool
#: with the same dtype it puts on the command line.
CLUSTER_KV_CACHE_DTYPE = "fp8"
MINIMUM_MAX_NUM_BATCHED_TOKENS = 256
DISTRIBUTED_TIMEOUT_SECONDS = 300
#: Where the host-network cluster head listens: EVERY interface, on purpose.
#:
#: 2026-09-13, owner decision (option A). Commit 229031c narrowed this to the
#: Docker bridge gateway after the developer-platform audit (F050/F042/F012/
#: F065) reached the unauthenticated engine on the office LAN and the tailnet.
#: That bind would have taken the two-node engine DOWN on its next recreate:
#: vLLM takes ONE --host, and the head's callers sit on four networks at once --
#:   * rail A (enp1s0f1np1): the WORKER's vllm-worker healthcheck curls
#:     http://CLUSTER_HEAD_IP:8000/health every 30 s and kill -9s its own rank
#:     after 8 misses (120 hits/h from 10.100.184.2 in the head's access log),
#:     and the interview-analysis tenant on the worker posts completions to
#:     http://CLUSTER_HEAD_IP:8000/v1;
#:   * the application bridge / docker0 (172.17.0.1 via vllm:host-gateway and
#:     host.docker.internal): orchestrator, sync-worker, Prometheus, litellm;
#:   * lo: the head's own healthcheck, deploy.sh's completion gate, the
#:     cluster-*.sh scripts and the CI verify job (127.0.0.1:8000).
#: Only 0.0.0.0 serves all of them, which is what production runs today
#: (docker inspect: --host 0.0.0.0; ss: 0.0.0.0:8000). The LAN and tailnet
#: exposure is closed by the host packet filter instead (scripts/host-guard.sh,
#: operator actions OA-4/OA-6: port 8000 dropped on enP7s7 and tailscale0).
#: Do not narrow this again without first moving every cross-node caller.
DEFAULT_API_BIND_ADDRESS = "0.0.0.0"
#: ``--gdn-prefill-backend`` (candidate B, docs/availability/CANDIDATE-B.md).
#: The flag is spelled the same in the pinned build and in the candidate
#: ``vllm/vllm-openai@sha256:819ec9c0...`` (``engine/arg_utils.py``:
#: ``choices=["flashinfer", "triton", "cutedsl"]``); on the pinned build it is
#: accepted and ignored on GB10, on the candidate it moves the GDN prefill
#: recurrence onto FlashInfer's SM120 kernel. Empty = the flag is absent and
#: the engine's own ``auto`` resolution applies. Both ranks must agree, so it
#: is rendered into CLUSTER_ENGINE_ARGS exactly like the speculative config.
GDN_PREFILL_BACKENDS = ("flashinfer", "triton", "cutedsl")
#: The per-rank engine PROCESS environment (candidate B): each entry is a
#: ``CLUSTER_*`` .env key, the vLLM variable it becomes, and the values it
#: may take. Rendered ONLY when set -- an empty ``VLLM_ALLREDUCE_USE_FLASHINFER``
#: is ``int("")`` inside vLLM's envs module, i.e. a crash at import -- so these
#: never go through a compose ``environment:`` map (whose empty default would
#: set the empty string) but through ``.runtime/engine.env``, an ``env_file``
#: both ranks read (the head from the cluster overlay, the worker from the
#: copy scripts/cluster-sync.sh ships).
CLUSTER_ENGINE_ENV_KEYS = (
    ("CLUSTER_VLLM_ALLREDUCE_USE_FLASHINFER", "VLLM_ALLREDUCE_USE_FLASHINFER", ("0", "1")),
    ("CLUSTER_VLLM_USE_V2_MODEL_RUNNER", "VLLM_USE_V2_MODEL_RUNNER", ("0", "1")),
)
#: The worker sentinel restarts the worker on its own only when this is 1
#: (contract §6.4); the controller is the single recovery authority, so the
#: production value is 0 and the launcher never turns it on by itself.
DEFAULT_SENTINEL_AUTONOMOUS = "0"
#: The head's and the worker's Docker healthchecks are report-only except one
#: last-resort kill after this many consecutive misses (30 s apart): 8 is
#: four minutes, long after the controller (10 s) would have acted, so the
#: two can never race (contract §6). Both nodes read the same number.
DEFAULT_HEALTHCHECK_KILL_AFTER = 8
HEALTHCHECK_KILL_AFTER_RANGE = (2, 120)
#: The kill tier's AGE GUARD: the vLLM process must be older than this many
#: seconds before the last-resort kill may fire, so a cold start is never
#: interrupted. It follows the controller's cold-start budget
#: (ENGINE_COLD_START_BUDGET_S, contract §6.3; 900 s = the measured 5 m 20 s
#: cold load with margin) unless VLLM_HEALTHCHECK_MIN_AGE_S says otherwise --
#: a raised budget for a slower image must reach the healthchecks too, or
#: the head is killed at 15 min + 4 min of misses while still legitimately
#: loading (review round 2). Shipped to the worker in worker.env.
DEFAULT_COLD_START_BUDGET_S = 900
COLD_START_BUDGET_RANGE = (60, 86400)
#: The healthchecks' `start_period` in both cluster compose files, for the
#: validation message: an age guard longer than it is fine (Docker keeps the
#: container "starting" only that long; the guard is the script's own).
HEALTHCHECK_START_PERIOD_S = 1800
INFINIBAND_SYSFS = Path("/sys/class/infiniband")
#: Parent of both ``net/<ifname>/operstate`` and ``infiniband/<hca>/device/net``.
CLASS_SYSFS = Path("/sys/class")
SSH_PORT = 22
PEER_PROBE_TIMEOUT = 2.0
PREFLIGHT_TIMEOUT = 30.0
#: One remote shell line: hostname, first GPU, Docker version, RDMA device node.
PREFLIGHT_REMOTE_COMMAND = (
    "hostname; nvidia-smi -L | head -n1; docker --version; "
    "test -e /dev/infiniband/rdma_cm && echo rdma-ok"
)
WORKER_GPU_MARKER = "GB10"
_SSH_TARGET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,253}$")


def _system_command(args: Sequence[str], timeout: float) -> tuple[int, str, str]:
    result = run_command(args, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


# --------------------------------------------------------------------------
# Host detection (Node 1 only; the worker script detects its own side).
# --------------------------------------------------------------------------


def _ip_json(runner: Command | None, *arguments: str) -> list[dict]:
    """Rows from ``ip -j -4 <arguments>``; empty on any failure."""
    command = runner or _system_command
    try:
        code, stdout, _stderr = command(("ip", "-j", "-4", *arguments), 10.0)
        if code != 0:
            return []
        rows = json.loads(stdout or "[]")
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def detect_ifname_for_ip(ip: str, *, runner: Command | None = None) -> str | None:
    """Name of the interface that carries ``ip`` per ``ip -j -4 addr show``."""
    for link in _ip_json(runner, "addr", "show"):
        for address in link.get("addr_info") or []:
            if isinstance(address, dict) and str(address.get("local", "")) == ip:
                name = str(link.get("ifname") or "")
                return name or None
    return None


def detect_hcas_for_ifnames(
    ifnames: Sequence[str], *, sysfs: Path = INFINIBAND_SYSFS
) -> list[str]:
    """RDMA device names whose netdev is one of ``ifnames``, in that order."""
    found: list[str] = []
    try:
        devices = sorted(path for path in sysfs.iterdir() if path.is_dir())
    except OSError:
        return found
    for ifname in ifnames:
        if not ifname:
            continue
        for device in devices:
            if device.name in found:
                continue
            if (device / "device" / "net" / ifname).exists():
                found.append(device.name)
    return found


def detect_docker_bridge_gateway(*, runner: Command | None = None) -> str | None:
    """Gateway address of Docker's default bridge, or None when unknown."""
    command = runner or _system_command
    try:
        code, stdout, _stderr = command(
            ("docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"),
            15.0,
        )
    except OSError:
        return None
    if code != 0:
        return None
    candidate = (stdout or "").strip().split("/")[0]
    try:
        return str(ipaddress.IPv4Address(candidate))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Peer discovery (CLUSTER_MODE=auto): links, neighbour, worker preflight.
# --------------------------------------------------------------------------


class ClusterLink(NamedTuple):
    """A local RoCE interface that can carry cluster traffic."""

    ifname: str
    ip: str
    prefixlen: int


def _operstate(net_sysfs: Path, ifname: str) -> str:
    try:
        return (net_sysfs / ifname / "operstate").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


def detect_cluster_links(*, runner: Command | None = None, sysfs: Path = CLASS_SYSFS) -> list[ClusterLink]:
    """Local interfaces that are up, carry IPv4, and sit on an RDMA device.

    Sorted by interface name (case-insensitively, so ``enp1...`` precedes
    ``enP2...``): the first link is link A (control plane, NCCL socket
    interface), the second, when present, is link B.
    """
    links: list[ClusterLink] = []
    for row in _ip_json(runner, "addr", "show"):
        ifname = str(row.get("ifname") or "")
        if not ifname or _operstate(sysfs / "net", ifname) != "up":
            continue
        if not detect_hcas_for_ifnames([ifname], sysfs=sysfs / "infiniband"):
            continue
        for address in row.get("addr_info") or []:
            if not isinstance(address, dict) or address.get("family", "inet") != "inet":
                continue
            try:
                ip = str(ipaddress.IPv4Address(str(address.get("local", ""))))
                prefixlen = int(address.get("prefixlen", 32))
            except (ValueError, TypeError):
                continue
            links.append(ClusterLink(ifname, ip, prefixlen))
            break
    links.sort(key=lambda link: (link.ifname.lower(), link.ifname))
    return links


def mirror_address(ip: str) -> str | None:
    """NVIDIA's back-to-back convention: ``.1`` faces ``.2`` and vice versa."""
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError:
        return None
    last = int(address) & 0xFF
    if last == 1:
        return str(address + 1)
    if last == 2:
        return str(address - 1)
    return None


def subnet_label(link: ClusterLink) -> str:
    """``10.100.184.x`` for a /24 or narrower; the CIDR otherwise."""
    if link.prefixlen >= 24:
        return ".".join(link.ip.split(".")[:3]) + ".x"
    return str(ipaddress.IPv4Interface(f"{link.ip}/{link.prefixlen}").network)


def tcp_probe(ip: str, port: int, *, timeout: float = PEER_PROBE_TIMEOUT) -> bool:
    """A TCP connect only; nothing is sent."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_peer_ip(
    link: ClusterLink,
    *,
    runner: Command | None = None,
    probe: Callable[[str, int], bool] = tcp_probe,
) -> str | None:
    """The first neighbour (or mirror) address on ``link`` that accepts ssh."""
    candidates: list[str] = []
    for row in _ip_json(runner, "neigh", "show", "dev", link.ifname):
        states = row.get("state") or []
        if isinstance(states, str):
            states = [states]
        if any(str(state).upper() in {"FAILED", "INCOMPLETE"} for state in states):
            continue
        try:
            candidate = str(ipaddress.IPv4Address(str(row.get("dst", ""))))
        except ValueError:
            continue
        if candidate != link.ip:
            candidates.append(candidate)
    mirror = mirror_address(link.ip)
    if mirror:
        candidates.append(mirror)
    for candidate in dict.fromkeys(candidates):
        if probe(candidate, SSH_PORT):
            return candidate
    return None


@dataclass(frozen=True)
class WorkerPreflight:
    """What one ssh round-trip learned about the worker host."""

    ok: bool
    hostname: str = ""
    gpu: str = ""
    docker: str = ""
    detail: str = ""


def preflight_worker(ssh_target: str, *, runner: Command | None = None) -> WorkerPreflight:
    """Verify over ssh that ``ssh_target`` is a DGX Spark ready to run the worker."""
    command = runner or _system_command
    argv = (
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=accept-new",
        ssh_target, "--", PREFLIGHT_REMOTE_COMMAND,
    )
    try:
        code, stdout, stderr = command(argv, PREFLIGHT_TIMEOUT)
    except OSError as exc:
        return WorkerPreflight(False, detail=f"ssh: {exc}")
    lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
    errors = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    last_error = errors[-1] if errors else ""
    if last_error.startswith(f"{ssh_target}: "):
        last_error = last_error[len(ssh_target) + 2:]
    hostname = lines[0] if lines else ""
    rest = lines[1:]
    gpu = next((line for line in rest if line.startswith("GPU")), "")
    docker = next((line for line in rest if line.lower().startswith("docker version")), "")
    rdma = "rdma-ok" in rest
    if not hostname:
        return WorkerPreflight(False, detail=f"ssh: {last_error or f'exited with status {code}'}")
    if WORKER_GPU_MARKER not in gpu:
        seen = gpu or last_error or "nvidia-smi printed nothing"
        return WorkerPreflight(False, hostname, gpu, docker, f"no NVIDIA {WORKER_GPU_MARKER} GPU visible on {hostname} ({seen})")
    if not docker:
        return WorkerPreflight(False, hostname, gpu, docker, f"docker is not installed on {hostname} ({last_error or 'docker --version printed nothing'})")
    if not rdma:
        return WorkerPreflight(
            False, hostname, gpu, docker,
            f"/dev/infiniband/rdma_cm is missing on {hostname} (RDMA/RoCE kernel modules are not loaded)",
        )
    if code != 0:
        return WorkerPreflight(False, hostname, gpu, docker, f"remote preflight exited with status {code} ({last_error or 'no error output'})")
    return WorkerPreflight(True, hostname, gpu, docker, "")


def default_worker_ssh(worker_ip: str) -> str:
    """``<current user>@<worker ip>``; keys only, never a password."""
    return f"{getpass.getuser()}@{worker_ip}"


@dataclass(frozen=True)
class ClusterDiscovery:
    """Injectable peer discovery so ``auto`` can be resolved without a network."""

    links: Callable[[], list[ClusterLink]] = detect_cluster_links
    peer_ip: Callable[[ClusterLink], str | None] = discover_peer_ip
    preflight: Callable[[str], WorkerPreflight] = preflight_worker
    worker_ssh: Callable[[str], str] = default_worker_ssh


DEFAULT_DISCOVERY = ClusterDiscovery()


@dataclass(frozen=True)
class PeerDiscovery:
    """Outcome of one discovery pass over the local RoCE links."""

    links: tuple[ClusterLink, ...]
    head_ip: str = ""
    worker_ip: str = ""
    head_ip_2: str = ""
    worker_ip_2: str = ""
    failure: str = ""

    @property
    def found(self) -> bool:
        return bool(self.worker_ip) and not self.failure

    def address_values(self) -> dict[str, str]:
        return {
            "CLUSTER_HEAD_IP": self.head_ip,
            "CLUSTER_WORKER_IP": self.worker_ip,
            "CLUSTER_HEAD_IP_2": self.head_ip_2,
            "CLUSTER_WORKER_IP_2": self.worker_ip_2,
        }


def discover_cluster_peer(*, discovery: ClusterDiscovery = DEFAULT_DISCOVERY) -> PeerDiscovery:
    """Find the second DGX Spark on link A (and, when both sides have one, link B)."""
    links = tuple(discovery.links())
    if not links:
        return PeerDiscovery(links, failure="no RoCE link carries an address")
    link_a = links[0]
    peer_a = discovery.peer_ip(link_a)
    if not peer_a:
        return PeerDiscovery(links, failure=f"no second DGX Spark answered on {subnet_label(link_a)}")
    head_ip_2 = worker_ip_2 = ""
    if len(links) > 1:
        link_b = links[1]
        peer_b = discovery.peer_ip(link_b)
        if peer_b and peer_b != peer_a:
            head_ip_2, worker_ip_2 = link_b.ip, peer_b
    return PeerDiscovery(links, link_a.ip, peer_a, head_ip_2, worker_ip_2)


@dataclass(frozen=True)
class ClusterDetectors:
    """Injectable host probes so settings can be resolved without hardware."""

    ifname_for_ip: Callable[[str], str | None] = detect_ifname_for_ip
    hcas_for_ifnames: Callable[[Sequence[str]], list[str]] = detect_hcas_for_ifnames
    docker_bridge_gateway: Callable[[], str | None] = detect_docker_bridge_gateway


DEFAULT_DETECTORS = ClusterDetectors()


# --------------------------------------------------------------------------
# User-owned .env keys
# --------------------------------------------------------------------------


def _raw(values: Mapping[str, str], name: str) -> str:
    return str(values.get(name, "") or "").strip()


def resolve_cluster_mode(values: Mapping[str, str]) -> str:
    """The user's intent: ``auto`` (default), ``single``, or ``dual``."""
    raw = _raw(values, "CLUSTER_MODE").lower() or DEFAULT_CLUSTER_MODE
    if raw not in CLUSTER_MODES:
        raise TechSaraError(
            f"CLUSTER_MODE must be one of {', '.join(CLUSTER_MODES)}; got {values.get('CLUSTER_MODE')!r}"
        )
    return raw


def _ipv4(values: Mapping[str, str], name: str, *, required: bool) -> str:
    raw = _raw(values, name)
    if not raw:
        if required:
            raise TechSaraError(
                f"{name} is required when CLUSTER_MODE=dual; set it to this node's RoCE-link IPv4 address"
                if name.startswith("CLUSTER_HEAD")
                else f"{name} is required when CLUSTER_MODE=dual; set it to the worker's RoCE-link IPv4 address"
            )
        return ""
    try:
        return str(ipaddress.IPv4Address(raw))
    except ValueError as exc:
        raise TechSaraError(f"{name} must be a literal IPv4 address such as 192.168.100.1; got {raw!r}") from exc


def _int(values: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int | None = None) -> int:
    raw = _raw(values, name)
    if not raw:
        return default
    try:
        number = int(raw)
    except ValueError as exc:
        raise TechSaraError(f"{name} must be an integer; got {raw!r}") from exc
    if number < minimum or (maximum is not None and number > maximum):
        bounds = f"at least {minimum}" if maximum is None else f"between {minimum} and {maximum}"
        raise TechSaraError(f"{name} must be {bounds}; got {number}")
    return number


def _gpu_memory_utilization(values: Mapping[str, str]) -> str:
    raw = _raw(values, "CLUSTER_GPU_MEMORY_UTILIZATION")
    if not raw:
        return DEFAULT_GPU_MEMORY_UTILIZATION
    try:
        number = float(raw)
    except ValueError as exc:
        raise TechSaraError(f"CLUSTER_GPU_MEMORY_UTILIZATION must be a number; got {raw!r}") from exc
    low, high = GPU_MEMORY_UTILIZATION_RANGE
    if not low <= number <= high:
        raise TechSaraError(
            f"CLUSTER_GPU_MEMORY_UTILIZATION must be between {low:.2f} and {high:.2f}; got {raw!r}"
        )
    return f"{number:.2f}"


def _nccl_debug(values: Mapping[str, str]) -> str:
    raw = _raw(values, "CLUSTER_NCCL_DEBUG").upper() or DEFAULT_NCCL_DEBUG
    if raw not in NCCL_DEBUG_LEVELS:
        raise TechSaraError(
            f"CLUSTER_NCCL_DEBUG must be one of {', '.join(NCCL_DEBUG_LEVELS)}; got {values.get('CLUSTER_NCCL_DEBUG')!r}"
        )
    return raw


def _speculative_config(values: Mapping[str, str]) -> str:
    """Compact JSON for ``--speculative-config``; empty (or absent) disables it."""
    raw = _raw(values, "CLUSTER_SPECULATIVE_CONFIG") or DEFAULT_SPECULATIVE_CONFIG
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise TechSaraError(
            "CLUSTER_SPECULATIVE_CONFIG must be a JSON object such as "
            f'{MTP_SPECULATIVE_CONFIG} or empty to disable speculative decoding; got {raw!r}'
        ) from exc
    if not isinstance(parsed, dict) or not parsed:
        raise TechSaraError(
            "CLUSTER_SPECULATIVE_CONFIG must be a non-empty JSON object or empty to disable speculative decoding"
        )
    return json.dumps(parsed, separators=(",", ":"))


def speculative_config_argument(values: Mapping[str, str]) -> str:
    """The rendered ``--speculative-config '<json>'`` segment, or ``""``.

    One validator for both deployment shapes: the dual-mode
    ``CLUSTER_ENGINE_ARGS`` string and the single-node overlay's
    ``MAIN_MODEL_SPECULATIVE_ARGUMENT`` are built from this same function, so
    the GDN fault above cannot come back on one path while the other is
    guarded. Single-quoted so Compose's shell-style split keeps the JSON as
    one argv element.
    """
    config = _speculative_config(values)
    return f"--speculative-config '{config}'" if config else ""


def _gdn_prefill_backend(values: Mapping[str, str]) -> str:
    """``CLUSTER_GDN_PREFILL_BACKEND``: one of the engine's choices, or ``""``."""
    raw = _raw(values, "CLUSTER_GDN_PREFILL_BACKEND").lower()
    if raw and raw not in GDN_PREFILL_BACKENDS:
        raise TechSaraError(
            "CLUSTER_GDN_PREFILL_BACKEND must be one of "
            f"{', '.join(GDN_PREFILL_BACKENDS)} or empty to leave the flag out; got {values.get('CLUSTER_GDN_PREFILL_BACKEND')!r}"
        )
    return raw


def gdn_prefill_backend_argument(values: Mapping[str, str]) -> str:
    """The rendered ``--gdn-prefill-backend <name>`` segment, or ``""``.

    One validator for both deployment shapes, like ``speculative_config_argument``:
    the dual-mode ``CLUSTER_ENGINE_ARGS`` string and the single-node overlay's
    ``MAIN_MODEL_GDN_PREFILL_ARGUMENT`` come from here, so a candidate-B
    evaluation cannot run one kernel on one path and another kernel elsewhere.
    """
    backend = _gdn_prefill_backend(values)
    return f"--gdn-prefill-backend {backend}" if backend else ""


def engine_process_environment(values: Mapping[str, str]) -> dict[str, str]:
    """The vLLM process variables both ranks get, from the ``CLUSTER_VLLM_*`` keys.

    Only the keys that are SET appear (see ``CLUSTER_ENGINE_ENV_KEYS`` for why
    an empty value must never be rendered); every value is validated against
    the bounded set the engine accepts.
    """
    rendered: dict[str, str] = {}
    for user_key, engine_key, allowed in CLUSTER_ENGINE_ENV_KEYS:
        raw = _raw(values, user_key)
        if not raw:
            continue
        if raw not in allowed:
            raise TechSaraError(
                f"{user_key} must be one of {', '.join(allowed)} or empty to leave {engine_key} unset; got {raw!r}"
            )
        rendered[engine_key] = raw
    return rendered


def render_engine_env(process_environment: Mapping[str, str]) -> str:
    """``.runtime/engine.env`` (and the worker's copy): one ``KEY=value`` per line.

    Always written, even when empty, so the ``env_file`` entry on both ranks
    is deterministic and a stale copy on the worker is caught by the sha256
    check in scripts/cluster-sync.sh rather than by a different engine config.
    """
    lines = [
        "# Generated by the launcher: the vLLM PROCESS environment of BOTH ranks",
        "# (compose.cluster-dgx-spark.yaml env_file; shipped verbatim to the worker",
        "# by scripts/cluster-sync.sh). Do not edit; set CLUSTER_VLLM_* in .env.",
    ]
    lines.extend(f"{key}={value}" for key, value in sorted(process_environment.items()))
    return "\n".join(lines) + "\n"


def _sentinel_autonomous(values: Mapping[str, str]) -> str:
    raw = _raw(values, "CLUSTER_SENTINEL_AUTONOMOUS") or DEFAULT_SENTINEL_AUTONOMOUS
    if raw not in ("0", "1"):
        raise TechSaraError(
            f"CLUSTER_SENTINEL_AUTONOMOUS must be 0 (the controller alone restarts the pair) or 1; got {raw!r}"
        )
    return raw


def healthcheck_kill_after(values: Mapping[str, str]) -> int:
    """``VLLM_HEALTHCHECK_KILL_AFTER``: consecutive misses before the last-resort kill."""
    return _int(
        values, "VLLM_HEALTHCHECK_KILL_AFTER", DEFAULT_HEALTHCHECK_KILL_AFTER,
        minimum=HEALTHCHECK_KILL_AFTER_RANGE[0], maximum=HEALTHCHECK_KILL_AFTER_RANGE[1],
    )


def cold_start_budget_s(values: Mapping[str, str]) -> int:
    """``ENGINE_COLD_START_BUDGET_S``: the controller's WAIT_FOR_MODEL_LOAD budget."""
    return _int(
        values, "ENGINE_COLD_START_BUDGET_S", DEFAULT_COLD_START_BUDGET_S,
        minimum=COLD_START_BUDGET_RANGE[0], maximum=COLD_START_BUDGET_RANGE[1],
    )


def healthcheck_min_age_s(values: Mapping[str, str]) -> int:
    """``VLLM_HEALTHCHECK_MIN_AGE_S``: the kill tier's age guard on both nodes.

    Defaults to the cold-start budget; an explicit value shorter than that
    budget is refused, because it would let the healthcheck kill a head (or
    worker) the controller is still legitimately waiting for.
    """
    budget = cold_start_budget_s(values)
    min_age = _int(
        values, "VLLM_HEALTHCHECK_MIN_AGE_S", budget,
        minimum=COLD_START_BUDGET_RANGE[0], maximum=COLD_START_BUDGET_RANGE[1],
    )
    if min_age < budget:
        raise TechSaraError(
            f"VLLM_HEALTHCHECK_MIN_AGE_S={min_age} is shorter than ENGINE_COLD_START_BUDGET_S={budget}: "
            "the healthchecks' last-resort kill would interrupt a cold start the controller still allows; "
            "raise it to at least the budget (or leave it empty to follow the budget; the healthchecks' "
            f"start_period is {HEALTHCHECK_START_PERIOD_S}s and does not stop the script's own kill)"
        )
    return min_age


def worker_management_ip(values: Mapping[str, str]) -> str:
    """The worker's MANAGEMENT address for the controller's participation probe.

    Three sources, first one wins: ``CLUSTER_WORKER_MGMT_IP``,
    ``MONITORING_WORKER_BIND`` (scripts/monitoring.sh's older spelling; a
    hostname there is that script's business and is skipped here), and the
    host of ``OCR_REMOTE_BASE_URL`` -- the address scripts/ocr.sh recorded when
    it put the OCR engine on the worker, which IS the worker's management
    address on this production (``http://192.168.9.68:30004/v1``) and is
    derived exactly the way scripts/lib/cluster-common.sh
    write_ocr_scrape_target does (scheme off, first path segment). Without
    this, the launcher rendered an empty exporter URL on a box whose .env
    names neither monitoring key, and the controller sat in DEGRADED
    ("participation unobserved") for the life of the deployment (review
    round 2). Only a literal IPv4 is accepted from the URL; anything else
    leaves the sample unobservable, never a failure.
    """
    ip = _ipv4(values, "CLUSTER_WORKER_MGMT_IP", required=False)
    if ip:
        return ip
    try:
        ip = _ipv4(values, "MONITORING_WORKER_BIND", required=False)
    except TechSaraError:
        ip = ""
    if ip:
        return ip
    remote = _raw(values, "OCR_REMOTE_BASE_URL")
    if not remote:
        return ""
    # http://192.168.9.68:30004/v1 -> 192.168.9.68:30004 -> 192.168.9.68
    host_port = remote.split("://", 1)[-1].split("/", 1)[0]
    host = host_port.rsplit(":", 1)[0] if host_port.count(":") == 1 else host_port
    try:
        return str(ipaddress.IPv4Address(host))
    except ValueError:
        return ""


def prefix_caching_argument(enabled: bool) -> str:
    """``--enable-prefix-caching`` or its explicit negation.

    vLLM's own default depends on the model: ON for a dense model, OFF
    (opt-in "while the feature matures") for a hybrid-Mamba one such as the
    Qwen3.6-35B-A3B, where the engine labels prefix caching experimental
    (``mamba_cache_mode=align``). Merely omitting the flag would therefore
    mean different things on different profiles, so the launcher always
    states the choice explicitly and the running command line shows it.
    """
    return "--enable-prefix-caching" if enabled else "--no-enable-prefix-caching"


def _identifier(values: Mapping[str, str], name: str) -> str:
    """An optional interface/HCA override that is safe to interpolate."""
    raw = _raw(values, name)
    if raw and (len(raw) > 128 or any(ch.isspace() or ch in "\"'$`\\" for ch in raw)):
        raise TechSaraError(f"{name} must be a plain device name list such as enP2p1s0f1np1 or rocep1s0f1; got {raw!r}")
    return raw


def _ssh_target(values: Mapping[str, str]) -> str:
    """An optional ``user@host`` override that is safe to hand to ssh's argv."""
    raw = _raw(values, "CLUSTER_WORKER_SSH")
    if raw and not _SSH_TARGET.fullmatch(raw):
        raise TechSaraError(f"CLUSTER_WORKER_SSH must look like user@host (no spaces or options); got {raw!r}")
    return raw


# --------------------------------------------------------------------------
# Engine arguments and generated keys
# --------------------------------------------------------------------------


def build_engine_arguments(
    *,
    context: int,
    gpu_memory_utilization: str,
    startup_arguments: Sequence[str],
    speculative_config: str,
    max_num_batched_tokens: int,
    tensor_parallel_size: int,
    pipeline_parallel_size: int,
    head_ip: str,
    master_port: int,
    kv_cache_memory_gib: int = DEFAULT_KV_CACHE_MEMORY_GIB,
    rope_override: str = "",
    enable_prefix_caching: bool = True,
    gdn_prefill_backend: str = "",
) -> str:
    """The single-line, shell-splittable engine argument string.

    Both Compose files interpolate this verbatim so the head and the worker
    build byte-identical engine configurations. The speculative JSON stays
    single-quoted so Compose's shell-style split keeps it as one argv element;
    ``rope_override`` (the ``--hf-overrides`` YaRN argument, empty unless the
    requested window exceeds the model's native one) is quoted the same way and
    sits next to ``--max-model-len`` because the two only make sense together.
    """
    segments = [
        f"--max-model-len {int(context)}",
        rope_override.strip(),
        f"--gpu-memory-utilization {gpu_memory_utilization}",
        f"--kv-cache-memory-bytes {int(kv_cache_memory_gib) * 1024 ** 3}",
        f"--kv-cache-dtype {CLUSTER_KV_CACHE_DTYPE}",
        "--trust-remote-code",
        shlex.join(list(startup_arguments)),
        f"--gdn-prefill-backend {gdn_prefill_backend}" if gdn_prefill_backend else "",
        "--enable-chunked-prefill",
        prefix_caching_argument(enable_prefix_caching),
        f"--speculative-config '{speculative_config}'" if speculative_config else "",
        f"--max-num-batched-tokens {int(max_num_batched_tokens)}",
        f"--tensor-parallel-size {int(tensor_parallel_size)}",
        f"--pipeline-parallel-size {int(pipeline_parallel_size)}",
        "--distributed-executor-backend mp",
        f"--nnodes {CLUSTER_NODES}",
        f"--master-addr {head_ip}",
        f"--master-port {int(master_port)}",
        f"--distributed-timeout-seconds {DISTRIBUTED_TIMEOUT_SECONDS}",
    ]
    return " ".join(" ".join(segment.split()) for segment in segments if segment.strip())


def resolve_cluster_settings(
    user_values: Mapping[str, str],
    *,
    profile_id: str,
    publish_model_ports: bool,
    context: int,
    startup_arguments: Sequence[str],
    vllm_port: int,
    detectors: ClusterDetectors = DEFAULT_DETECTORS,
    rope_override: str = "",
    enable_prefix_caching: bool = True,
) -> dict[str, str]:
    """Validate the user's ``CLUSTER_*`` keys and derive the generated ones.

    Called only for ``CLUSTER_MODE=dual``. Every failure is a ``TechSaraError``
    naming the .env key to fix; nothing here touches the host except through
    ``detectors``.

    ``publish_model_ports`` is accepted and deliberately does NOT move the
    head's bind address: in dual mode the head listens on every interface
    whether or not the ports are published (see DEFAULT_API_BIND_ADDRESS for
    the cross-node callers that require it, 2026-09-13).
    """
    if profile_id != CLUSTER_PROFILE_ID:
        raise TechSaraError("CLUSTER_MODE=dual is only supported on the dgx-spark profile")
    if context < 1:
        raise TechSaraError("CLUSTER_MODE=dual requires a positive main-model context length")
    if not 1 <= int(vllm_port) <= 65535:
        raise TechSaraError(f"VLLM_PORT must be between 1 and 65535; got {vllm_port}")

    head_ip = _ipv4(user_values, "CLUSTER_HEAD_IP", required=True)
    worker_ip = _ipv4(user_values, "CLUSTER_WORKER_IP", required=True)
    if head_ip == worker_ip:
        raise TechSaraError("CLUSTER_HEAD_IP and CLUSTER_WORKER_IP must be different hosts")
    head_ip_2 = _ipv4(user_values, "CLUSTER_HEAD_IP_2", required=False)
    worker_ip_2 = _ipv4(user_values, "CLUSTER_WORKER_IP_2", required=False)
    if bool(head_ip_2) != bool(worker_ip_2):
        raise TechSaraError(
            "CLUSTER_HEAD_IP_2 and CLUSTER_WORKER_IP_2 must be set together (second RoCE rail) or both left empty"
        )
    if head_ip_2 and (head_ip_2 == head_ip or head_ip_2 == worker_ip_2):
        raise TechSaraError("CLUSTER_HEAD_IP_2 must differ from CLUSTER_HEAD_IP and CLUSTER_WORKER_IP_2")

    master_port = _int(user_values, "CLUSTER_MASTER_PORT", DEFAULT_MASTER_PORT, minimum=1024, maximum=65535)
    if master_port == int(vllm_port):
        raise TechSaraError(f"CLUSTER_MASTER_PORT must not equal VLLM_PORT ({vllm_port})")
    tensor_parallel = _int(user_values, "CLUSTER_TENSOR_PARALLEL_SIZE", DEFAULT_TENSOR_PARALLEL_SIZE, minimum=1)
    pipeline_parallel = _int(user_values, "CLUSTER_PIPELINE_PARALLEL_SIZE", DEFAULT_PIPELINE_PARALLEL_SIZE, minimum=1)
    if tensor_parallel * pipeline_parallel != CLUSTER_NODES:
        raise TechSaraError(
            "CLUSTER_TENSOR_PARALLEL_SIZE x CLUSTER_PIPELINE_PARALLEL_SIZE must equal "
            f"{CLUSTER_NODES} (one GPU per DGX Spark); got {tensor_parallel} x {pipeline_parallel}"
        )
    utilization = _gpu_memory_utilization(user_values)
    nccl_debug = _nccl_debug(user_values)
    speculative = _speculative_config(user_values)
    gdn_backend = _gdn_prefill_backend(user_values)
    engine_environment = engine_process_environment(user_values)
    sentinel_autonomous = _sentinel_autonomous(user_values)
    kill_after = healthcheck_kill_after(user_values)
    min_age = healthcheck_min_age_s(user_values)
    # The worker's MANAGEMENT address (the one Prometheus scrapes its GPU
    # exporter on), for the controller's participation probe (contract §5
    # step 4): the cluster key, monitoring.sh's older spelling, or the host
    # scripts/ocr.sh recorded in OCR_REMOTE_BASE_URL (see worker_management_ip).
    worker_mgmt_ip = worker_management_ip(user_values)
    batched_tokens = _int(
        user_values, "CLUSTER_MAX_NUM_BATCHED_TOKENS", DEFAULT_MAX_NUM_BATCHED_TOKENS,
        minimum=MINIMUM_MAX_NUM_BATCHED_TOKENS,
    )
    kv_cache_gib = _int(
        user_values, "CLUSTER_KV_CACHE_MEMORY_GIB", DEFAULT_KV_CACHE_MEMORY_GIB,
        minimum=KV_CACHE_MEMORY_GIB_RANGE[0], maximum=KV_CACHE_MEMORY_GIB_RANGE[1],
    )

    socket_ifname = _identifier(user_values, "CLUSTER_NCCL_SOCKET_IFNAME")
    if not socket_ifname:
        socket_ifname = detectors.ifname_for_ip(head_ip) or ""
        if not socket_ifname:
            raise TechSaraError(
                f"no host interface carries CLUSTER_HEAD_IP={head_ip}; assign the RoCE-link address on this "
                "node or set CLUSTER_NCCL_SOCKET_IFNAME explicitly"
            )
    hca = _identifier(user_values, "CLUSTER_NCCL_IB_HCA")
    if not hca:
        ifnames = [socket_ifname]
        if head_ip_2:
            second = detectors.ifname_for_ip(head_ip_2) or ""
            if second and second not in ifnames:
                ifnames.append(second)
        hca = ",".join(detectors.hcas_for_ifnames(ifnames))

    # The head's bind and the bridge gateway are two different facts, and
    # 229031c conflated them. The head binds every interface in dual mode,
    # published or not: before 2026-09-13 the UNPUBLISHED branch already bound
    # the bridge gateway, which the worker's healthcheck (dialling
    # CLUSTER_HEAD_IP on rail A) cannot reach either, so that branch was the
    # same latent outage. See DEFAULT_API_BIND_ADDRESS for every caller.
    api_bind = DEFAULT_API_BIND_ADDRESS
    # The gateway is still what the engine controller's own /state and
    # /metrics must add to loopback (the orchestrator polls vllm:9838 through
    # host-gateway, Prometheus scrapes host.docker.internal:9838), and the
    # address it dials the head on. Its own key, so compose never derives the
    # controller's bind from the head's: "127.0.0.1,0.0.0.0" would collapse to
    # a wildcard 9838 (live since 02:54:50Z on 127.0.0.1 + 172.17.0.1).
    # Empty when unreadable; compose falls back to loopback, never wider.
    bridge_gateway = detectors.docker_bridge_gateway() or ""

    engine_args = build_engine_arguments(
        context=context,
        gpu_memory_utilization=utilization,
        startup_arguments=startup_arguments,
        speculative_config=speculative,
        max_num_batched_tokens=batched_tokens,
        tensor_parallel_size=tensor_parallel,
        pipeline_parallel_size=pipeline_parallel,
        head_ip=head_ip,
        master_port=master_port,
        kv_cache_memory_gib=kv_cache_gib,
        rope_override=rope_override,
        enable_prefix_caching=enable_prefix_caching,
        gdn_prefill_backend=gdn_backend,
    )
    return {
        "CLUSTER_HEAD_IP": head_ip,
        "CLUSTER_WORKER_IP": worker_ip,
        "CLUSTER_HEAD_IP_2": head_ip_2,
        "CLUSTER_WORKER_IP_2": worker_ip_2,
        "CLUSTER_WORKER_MGMT_IP": worker_mgmt_ip,
        "CLUSTER_MASTER_PORT": str(master_port),
        "CLUSTER_TENSOR_PARALLEL_SIZE": str(tensor_parallel),
        "CLUSTER_PIPELINE_PARALLEL_SIZE": str(pipeline_parallel),
        "CLUSTER_GPU_MEMORY_UTILIZATION": utilization,
        "CLUSTER_KV_CACHE_MEMORY_GIB": str(kv_cache_gib),
        "CLUSTER_NCCL_DEBUG": nccl_debug,
        "CLUSTER_NCCL_SOCKET_IFNAME": socket_ifname,
        "CLUSTER_NCCL_IB_HCA": hca,
        "CLUSTER_API_BIND_ADDRESS": api_bind,
        "CLUSTER_BRIDGE_GATEWAY": bridge_gateway,
        "CLUSTER_ENGINE_ARGS": engine_args,
        # Candidate-B knobs, recorded so scripts/cluster-sync.sh ships the
        # same values and cluster-status.sh can print what both ranks run.
        "CLUSTER_GDN_PREFILL_BACKEND": gdn_backend,
        # The per-rank process environment as one line, for the record; the
        # file both ranks actually read is rendered by render_engine_env().
        "CLUSTER_ENGINE_ENV": " ".join(f"{k}={v}" for k, v in sorted(engine_environment.items())),
        "CLUSTER_SENTINEL_AUTONOMOUS": sentinel_autonomous,
        "VLLM_HEALTHCHECK_KILL_AFTER": str(kill_after),
        # The kill tier's age guard, resolved here so both compose files
        # interpolate the same number (scripts/cluster-sync.sh ships it in
        # worker.env) and the launcher has validated it against the budget.
        "VLLM_HEALTHCHECK_MIN_AGE_S": str(min_age),
    }


# --------------------------------------------------------------------------
# Effective mode: auto-detection plus the dual-mode settings above
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterResolution:
    """The effective cluster mode, why it was chosen, and its generated keys."""

    mode: str
    reason: str
    values: dict[str, str]

    def generated(self) -> dict[str, str]:
        return {
            "TECHSARA_CLUSTER_MODE": self.mode,
            "TECHSARA_CLUSTER_REASON": " ".join(self.reason.split()),
            **self.values,
        }


def _single(reason: str = "") -> ClusterResolution:
    return ClusterResolution("single", reason, {})


def resolve_cluster(
    user_values: Mapping[str, str],
    *,
    profile_id: str,
    publish_model_ports: bool,
    context: int,
    startup_arguments: Sequence[str],
    vllm_port: int,
    detectors: ClusterDetectors = DEFAULT_DETECTORS,
    discovery: ClusterDiscovery = DEFAULT_DISCOVERY,
    rope_override: str = "",
    enable_prefix_caching: bool = True,
) -> ClusterResolution:
    """Turn ``CLUSTER_MODE`` (auto|single|dual) into an effective mode.

    Discovery and the ssh preflight run only on the dgx-spark profile in
    ``auto`` or ``dual`` mode; every other host resolves to ``single`` without
    touching the network.  Explicit ``CLUSTER_HEAD_IP``/``CLUSTER_WORKER_IP``
    (and ``_2``) override discovery but are still verified over ssh.  In
    ``auto`` an unusable peer degrades to ``single`` with a reason; in ``dual``
    it is a ``TechSaraError``.
    """
    mode = resolve_cluster_mode(user_values)
    if profile_id != CLUSTER_PROFILE_ID:
        if mode == "dual":
            raise TechSaraError("CLUSTER_MODE=dual is only supported on the dgx-spark profile")
        return _single()
    if mode == "single":
        return _single("CLUSTER_MODE=single in .env")

    explicit_head = _raw(user_values, "CLUSTER_HEAD_IP")
    explicit_worker = _raw(user_values, "CLUSTER_WORKER_IP")
    values = dict(user_values)
    if explicit_head or explicit_worker:
        if mode == "auto" and not (explicit_head and explicit_worker):
            raise TechSaraError(
                "CLUSTER_HEAD_IP and CLUSTER_WORKER_IP must be set together in .env, or both left empty so "
                "CLUSTER_MODE=auto can discover the second DGX Spark"
            )
        source = "configured"
    else:
        peer = discover_cluster_peer(discovery=discovery)
        if not peer.found:
            if mode == "auto":
                return _single(peer.failure)
            raise TechSaraError(
                f"CLUSTER_MODE=dual but {peer.failure}; connect the second DGX Spark on the RoCE links, set "
                "CLUSTER_HEAD_IP/CLUSTER_WORKER_IP in .env, or use CLUSTER_MODE=auto"
            )
        # Discovered addresses fill only the keys .env left empty, so an
        # explicit second rail (CLUSTER_*_IP_2) still overrides discovery.
        values.update({key: ip for key, ip in peer.address_values().items() if not _raw(user_values, key)})
        source = "auto-detected"

    # Cheap validation first: a bad port should not cost an ssh round-trip.
    settings = resolve_cluster_settings(
        values,
        profile_id=profile_id,
        publish_model_ports=publish_model_ports,
        context=context,
        startup_arguments=startup_arguments,
        vllm_port=vllm_port,
        detectors=detectors,
        rope_override=rope_override,
        enable_prefix_caching=enable_prefix_caching,
    )
    worker_ip = settings["CLUSTER_WORKER_IP"]
    ssh_target = _ssh_target(user_values) or discovery.worker_ssh(worker_ip)
    report = discovery.preflight(ssh_target)
    if not report.ok:
        host = f"second DGX Spark {report.hostname}" if report.hostname else "second DGX Spark"
        if mode == "auto":
            return _single(
                f"{host} found at {worker_ip} but ssh failed: {report.detail} "
                f"(set up key auth for {ssh_target} or CLUSTER_MODE=single to silence)"
            )
        raise TechSaraError(
            f"CLUSTER_MODE=dual but the worker {ssh_target} failed preflight: {report.detail}; "
            "fix the worker host, or set CLUSTER_MODE=single in .env"
        )
    settings["CLUSTER_WORKER_SSH"] = ssh_target
    return ClusterResolution("dual", f"second DGX Spark {report.hostname} at {worker_ip} ({source})", settings)
