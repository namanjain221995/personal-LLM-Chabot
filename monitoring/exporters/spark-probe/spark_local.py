#!/usr/bin/env python3
"""spark-local: the readings that need root-equivalent access.

  * NVMe SMART health. The drive's own record of wear, media errors and power
    loss. node-exporter reports the model and the temperature but none of this,
    and on these machines the numbers matter: the drive takes the model weights,
    the swap and every database, and `unsafe_shutdowns` is the only durable
    evidence of a power cut. Reading it is an NVMe admin command, which the
    kernel only allows with CAP_SYS_ADMIN.

  * Docker health status. Docker does not write health results to its state
    files (config.v2.json still said "starting" for containers days after they
    went healthy, measured 2026-11-05), so spark-probe cannot get them there and
    the only other source is the API.

  * Database sizes. Walking a database's volume means reading directories owned
    by the database's own user, which takes CAP_DAC_READ_SEARCH. spark-probe says
    which volumes belong to which database; only names are taken from it, and the
    walk stays inside the volumes mount and follows no symlink.

WHY THIS IS A SEPARATE CONTAINER WITH NO NETWORK. Each of those privileges is as
good as root: CAP_SYS_ADMIN, the Docker socket, and CAP_DAC_READ_SEARCH (which, under
Docker's default seccomp profile, also unlocks open_by_handle_at). So the process
holding them has no network interface at all: nothing can reach it, and all it can
affect is the files it writes. spark-probe, which talks to databases, gets none of
them. cAdvisor, which this replaces, held the same socket while serving HTTP. Only
GET requests are ever sent.

The log page layout is the NVMe base specification's "SMART / Health
Information" (log identifier 02h). Data units are thousands of 512-byte blocks.
Standard library only.
"""
from __future__ import annotations

import ctypes
import fcntl
import glob
import http.client
import json
import logging
import os
import re
import socket
import stat as stat_module
import threading
import time

log = logging.getLogger("spark-local")

NVME_TEXTFILE = os.environ.get("SPARK_LOCAL_NVME_TEXTFILE", "/textfile/spark_nvme.prom")
HEALTH_TEXTFILE = os.environ.get("SPARK_LOCAL_HEALTH_TEXTFILE", "/textfile/spark_health.prom")
NVME_INTERVAL_S = float(os.environ.get("SPARK_LOCAL_NVME_INTERVAL", "300"))
HEALTH_INTERVAL_S = float(os.environ.get("SPARK_LOCAL_HEALTH_INTERVAL", "15"))
DOCKER_SOCKET = os.environ.get("SPARK_LOCAL_DOCKER_SOCKET", "/var/run/docker.sock")
DB_SIZE_TEXTFILE = os.environ.get("SPARK_LOCAL_DB_SIZE_TEXTFILE", "/textfile/spark_db_size.prom")
#: Written by spark-probe beside the .prom files; node-exporter ignores other names.
DB_VOLUMES_FILE = os.environ.get("SPARK_LOCAL_DB_VOLUMES_FILE", "/textfile/.db_volumes.json")
VOLUMES_ROOT = os.environ.get("SPARK_LOCAL_VOLUMES_ROOT", "/host/docker/volumes")
DB_SIZE_INTERVAL_S = float(os.environ.get("SPARK_LOCAL_DB_SIZE_INTERVAL", "300"))
DB_SIZE_BUDGET_S = float(os.environ.get("SPARK_LOCAL_DB_SIZE_BUDGET", "20"))
#: _IOWR('N', 0x41, struct nvme_admin_cmd)
NVME_IOCTL_ADMIN_CMD = 0xC0484E41
ADMIN_GET_LOG_PAGE = 0x02
LOG_SMART = 0x02
LOG_SIZE = 512
DATA_UNIT_BYTES = 512_000


class AdminCommand(ctypes.Structure):
    _fields_ = [
        ("opcode", ctypes.c_uint8), ("flags", ctypes.c_uint8), ("rsvd1", ctypes.c_uint16),
        ("nsid", ctypes.c_uint32), ("cdw2", ctypes.c_uint32), ("cdw3", ctypes.c_uint32),
        ("metadata", ctypes.c_uint64), ("addr", ctypes.c_uint64),
        ("metadata_len", ctypes.c_uint32), ("data_len", ctypes.c_uint32),
        ("cdw10", ctypes.c_uint32), ("cdw11", ctypes.c_uint32), ("cdw12", ctypes.c_uint32),
        ("cdw13", ctypes.c_uint32), ("cdw14", ctypes.c_uint32), ("cdw15", ctypes.c_uint32),
        ("timeout_ms", ctypes.c_uint32), ("result", ctypes.c_uint32),
    ]


def read_smart_log(device: str) -> bytes:
    buffer = ctypes.create_string_buffer(LOG_SIZE)
    command = AdminCommand(
        opcode=ADMIN_GET_LOG_PAGE,
        nsid=0xFFFFFFFF,
        addr=ctypes.addressof(buffer),
        data_len=LOG_SIZE,
        cdw10=(((LOG_SIZE // 4) - 1) << 16) | LOG_SMART,
        timeout_ms=5000,
    )
    fd = os.open(device, os.O_RDONLY)
    try:
        fcntl.ioctl(fd, NVME_IOCTL_ADMIN_CMD, command)
    finally:
        os.close(fd)
    return buffer.raw


def parse_smart_log(raw: bytes) -> dict[str, float]:
    def u128(offset: int) -> int:
        return int.from_bytes(raw[offset:offset + 16], "little")

    def u32(offset: int) -> int:
        return int.from_bytes(raw[offset:offset + 4], "little")

    return {
        "critical_warning": raw[0],
        "temperature_celsius": int.from_bytes(raw[1:3], "little") - 273,
        "available_spare_ratio": raw[3] / 100,
        "available_spare_threshold_ratio": raw[4] / 100,
        "percentage_used_ratio": raw[5] / 100,
        "data_read_bytes_total": u128(32) * DATA_UNIT_BYTES,
        "data_written_bytes_total": u128(48) * DATA_UNIT_BYTES,
        "host_read_commands_total": u128(64),
        "host_write_commands_total": u128(80),
        "controller_busy_seconds_total": u128(96) * 60,
        "power_cycles_total": u128(112),
        "power_on_seconds_total": u128(128) * 3600,
        "unsafe_shutdowns_total": u128(144),
        "media_errors_total": u128(160),
        "error_log_entries_total": u128(176),
        "warning_temperature_seconds_total": u32(192) * 60,
        "critical_temperature_seconds_total": u32(196) * 60,
    }


HELP = {
    "critical_warning": "Critical warning bits from the drive; anything but 0 needs attention.",
    "temperature_celsius": "Composite temperature the drive reports.",
    "available_spare_ratio": "Spare capacity left, 1 is all of it.",
    "available_spare_threshold_ratio": "Spare level below which the drive raises a warning.",
    "percentage_used_ratio": "The vendor's estimate of rated life used; can pass 1.",
    "data_read_bytes_total": "Bytes the host has read over the drive's life.",
    "data_written_bytes_total": "Bytes the host has written over the drive's life.",
    "host_read_commands_total": "Read commands completed.",
    "host_write_commands_total": "Write commands completed.",
    "controller_busy_seconds_total": "Time the controller spent on I/O.",
    "power_cycles_total": "Power cycles.",
    "power_on_seconds_total": "Time powered on.",
    "unsafe_shutdowns_total": "Power losses and hard resets with no clean shutdown.",
    "media_errors_total": "Unrecovered data integrity errors.",
    "error_log_entries_total": "Entries in the controller's error log.",
    "warning_temperature_seconds_total": "Time above the warning temperature.",
    "critical_temperature_seconds_total": "Time above the critical temperature.",
}


def render(readings: dict[str, dict[str, float]], failed: list[str]) -> str:
    lines: list[str] = []
    for key, help_text in HELP.items():
        name = f"spark_nvme_{key}"
        kind = "counter" if key.endswith("_total") else "gauge"
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for device, values in sorted(readings.items()):
            lines.append(f'{name}{{device="{device}"}} {values[key]}')
    lines.append("# HELP spark_nvme_read_success 1 when the health log could be read.")
    lines.append("# TYPE spark_nvme_read_success gauge")
    for device in sorted(readings):
        lines.append(f'spark_nvme_read_success{{device="{device}"}} 1')
    for device in sorted(failed):
        lines.append(f'spark_nvme_read_success{{device="{device}"}} 0')
    lines.append("# HELP spark_nvme_last_read_timestamp_seconds When the drives were last read.")
    lines.append("# TYPE spark_nvme_last_read_timestamp_seconds gauge")
    lines.append(f"spark_nvme_last_read_timestamp_seconds {time.time()}")
    return "\n".join(lines) + "\n"


def devices() -> list[str]:
    # /dev/nvme0 is the controller; /dev/nvme0n1 and nvme-fabrics are not.
    return sorted(d for d in glob.glob("/dev/nvme*") if os.path.basename(d)[4:].isdigit())


def write_atomically(path: str, text: str) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def nvme_cycle() -> None:
    readings: dict[str, dict[str, float]] = {}
    failed: list[str] = []
    for path in devices():
        name = os.path.basename(path)
        try:
            readings[name] = parse_smart_log(read_smart_log(path))
        except OSError as exc:
            log.warning("cannot read %s: %s", path, exc.strerror)
            failed.append(name)
    write_atomically(NVME_TEXTFILE, render(readings, failed))


# -------------------------------------------------------------- docker health --


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=5)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self._path)


def docker_get(path: str, socket_path: str = DOCKER_SOCKET):
    """GET only. This function is the whole of this process's use of the API."""
    connection = _UnixConnection(socket_path)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read()
        if response.status != 200:
            raise OSError(f"docker answered {response.status}")
        return json.loads(body)
    finally:
        connection.close()


_HEALTH = re.compile(r"\((healthy|unhealthy|health: starting)\)")
HEALTH_CODE = {"healthy": 1, "unhealthy": 0, "health: starting": 2}


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_health(containers: list[dict] | None, info: dict | None) -> str:
    lines = [
        "# HELP spark_docker_up 1 when the Docker daemon answered this cycle.",
        "# TYPE spark_docker_up gauge",
        f"spark_docker_up {0 if containers is None else 1}",
        "# HELP spark_local_last_run_timestamp_seconds When this file was last written.",
        "# TYPE spark_local_last_run_timestamp_seconds gauge",
        f"spark_local_last_run_timestamp_seconds {time.time()}",
    ]
    if info:
        lines += [
            "# HELP spark_docker_info The Docker daemon on this Spark.",
            "# TYPE spark_docker_info gauge",
            f'spark_docker_info{{version="{_escape(str(info.get("ServerVersion", "unknown")))}",'
            f'storage_driver="{_escape(str(info.get("Driver", "unknown")))}"}} 1',
            "# HELP spark_docker_images Images held by the daemon.",
            "# TYPE spark_docker_images gauge",
            f"spark_docker_images {int(info.get('Images') or 0)}",
        ]
    lines += [
        "# HELP spark_container_health_status Docker healthcheck verdict: 1 healthy, 0 unhealthy, 2 starting. Absent when the container defines none.",
        "# TYPE spark_container_health_status gauge",
    ]
    seen = set()
    for container in containers or []:
        match = _HEALTH.search(container.get("Status") or "")
        names = container.get("Names") or []
        if not match or not names:
            continue
        name = names[0].lstrip("/")
        if name in seen:
            continue
        seen.add(name)
        labels = container.get("Labels") or {}
        project = labels.get("com.docker.compose.project") or "standalone"
        service = labels.get("com.docker.compose.service") or name
        lines.append(
            f'spark_container_health_status{{container="{_escape(name)}",project="{_escape(project)}",'
            f'service="{_escape(service)}"}} {HEALTH_CODE[match.group(1)]}'
        )
    return "\n".join(lines) + "\n"


def health_cycle(info: dict | None) -> None:
    try:
        containers = docker_get("/containers/json?all=1")
    except (OSError, ValueError, http.client.HTTPException) as exc:
        log.warning("docker API unavailable: %s", type(exc).__name__)
        containers = None
    write_atomically(HEALTH_TEXTFILE, render_health(containers, info))


# ------------------------------------------------------------- database sizes --

_VOLUME_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


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


def read_volume_list(path: str = DB_VOLUMES_FILE) -> list[tuple[dict[str, str], list[str]]]:
    """spark-probe's list. It talks to the outside and this process does not, so nothing
    in it is trusted: labels and names must match closed patterns or the entry is dropped."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = handle.read(4 << 20)
        doc = json.loads(raw)
    except (OSError, ValueError):
        return []
    out: list[tuple[dict[str, str], list[str]]] = []
    for item in doc if isinstance(doc, list) else []:
        if not isinstance(item, dict):
            continue
        labels = {key: item.get(key) for key in ("container", "engine", "project")}
        volumes = item.get("volumes")
        if not all(isinstance(v, str) and _LABEL.fullmatch(v) for v in labels.values()) or not isinstance(volumes, list):
            continue
        names = [v for v in volumes if isinstance(v, str) and _VOLUME_NAME.fullmatch(v) and ".." not in v]
        if names:
            out.append((labels, names))
    return out[:256]


def render_db_sizes(sizes: list[tuple[dict[str, str], int]]) -> str:
    lines = ["# HELP spark_db_data_bytes Disk used by the database's named volumes.", "# TYPE spark_db_data_bytes gauge"]
    for labels, size in sizes:
        body = ",".join(f'{key}="{labels[key]}"' for key in sorted(labels))
        lines.append(f"spark_db_data_bytes{{{body}}} {size}")
    return "\n".join(lines) + "\n"


def db_size_pass(root: str = VOLUMES_ROOT, list_path: str = DB_VOLUMES_FILE, last: dict | None = None) -> dict:
    """A walk that times out keeps its last good value instead of vanishing."""
    last = last if last is not None else {}
    fresh: dict = {}
    for labels, names in read_volume_list(list_path):
        key = labels["container"]
        deadline = time.monotonic() + DB_SIZE_BUDGET_S
        total = 0
        for name in names:
            size = directory_size(os.path.join(root, name, "_data"), deadline)
            if size is None:
                total = None
                break
            total += size
        if total is not None:
            fresh[key] = (labels, total)
        elif key in last:
            fresh[key] = last[key]
    return fresh


def db_size_loop() -> None:
    last: dict = {}
    # The probe writes its list a moment after both containers start.
    for _ in range(60):
        if os.path.exists(DB_VOLUMES_FILE):
            break
        time.sleep(1)
    while True:
        try:
            last = db_size_pass(last=last)
            write_atomically(DB_SIZE_TEXTFILE, render_db_sizes(list(last.values())))
        except Exception:  # noqa: BLE001 - sizing must never take the health file with it
            log.exception("database size pass failed")
        time.sleep(DB_SIZE_INTERVAL_S)


def main() -> int:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s %(message)s")
    once = os.environ.get("SPARK_LOCAL_ONCE") == "1"
    if not once:
        threading.Thread(target=db_size_loop, name="db-sizes", daemon=True).start()
    next_nvme = next_info = 0.0
    info: dict | None = None
    while True:
        now = time.monotonic()
        try:
            if now >= next_info:
                next_info = now + 300
                try:
                    info = docker_get("/info")
                except (OSError, ValueError, http.client.HTTPException):
                    info = None
            health_cycle(info)
            if now >= next_nvme:
                next_nvme = now + NVME_INTERVAL_S
                nvme_cycle()
        except Exception:  # noqa: BLE001 - staleness is alerted; a dead loop would be silent
            log.exception("cycle failed")
        if once:
            return 0
        time.sleep(HEALTH_INTERVAL_S)


if __name__ == "__main__":
    raise SystemExit(main())
