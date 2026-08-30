#!/usr/bin/env python3
"""Prometheus exporter for NVIDIA GB10 (DGX Spark) GPU telemetry.

WHY NOT dcgm-exporter. It was tested on this hardware, not assumed. NVIDIA's
arm64 image does pull, start and serve on GB10 — the honest summary is that it
works but is a strict subset that costs more than it returns here:

  * Framebuffer memory is unavailable at the DCGM LIBRARY level, not just in
    the exporter: `dcgmi dmon -e 250,251,252,253` returns N/A for fb_total,
    fb_free, fb_used and fb_resv. So do power limits, ECC, and remapped rows.
    8 of its 13 default series are structurally zero on this part.
  * The DCP profiling module will not load (`Result: -33`), so there is no
    SM_ACTIVE, DRAM_ACTIVE, TENSOR_ACTIVE or PCIe byte counter either.
  * Its default 30 s collection interval made values dangerously stale during
    a live generation: it reported GPU_UTIL=85 for four seconds AFTER the load
    had finished while nvidia-smi already read 0 %.
  * It cannot attribute memory to processes at all (`compute_pids` is N/A),
    which is the single most useful thing available on this machine.

`nvidia-smi` gives more, fresher, in one 63 ms call. DCGM does uniquely offer a
cumulative energy counter and the static slowdown/shutdown temperatures; if
exact energy accounting is ever needed it can be added alongside this.

WHAT GB10 ACTUALLY REPORTS, verified field by field on both nodes:
  works  - utilisation, memory-controller utilisation, temperature, thermal
           limit headroom, power draw (average and instant), SM/graphics/max
           clocks, performance state, throttle reasons AND their cumulative
           counters, and per-process GPU memory.
  N/A    - memory.total/used/free, power.limit, clocks.mem, fan.speed, memory
           temperature, ECC counters. These are absent from the output rather
           than exported as zero, because a zero here would be a lie.

GPU MEMORY ON A UNIFIED-MEMORY PART. The device-level FB counters are N/A, but
`--query-compute-apps` works and is the only GPU memory accounting available:
it reports the bytes each CUDA context holds. That is what
`dgx_gpu_process_memory_bytes` publishes. IMPORTANT: this returns an EMPTY
list inside a container that does not share the host PID namespace, so the
compose service sets `pid: host`. There is no total-capacity denominator to
divide by; the machine's own MemTotal is the honest one, and node_exporter
already provides it.

DESIGN. Standard library only: `nvidia-smi` is injected into any container
started with `--gpus all` by the NVIDIA container runtime, so this runs on a
stock `python:3.12-slim` with no pip install and no build-time network access.
Two calls per scrape, measured 41 ms + 21 ms on this hardware — about 1.3 %
duty cycle at a 5 s scrape, which is why that interval is safe against a
machine also serving an LLM.

Topology labels (`node`, `role`, `cluster`) are deliberately NOT set here.
Prometheus attaches them in `static_configs`, so the exporter is identical on
every Spark and the cluster layout lives in exactly one file.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN_HOST = os.environ.get("DGX_GPU_EXPORTER_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("DGX_GPU_EXPORTER_PORT", "9835"))
SMI_TIMEOUT_S = float(os.environ.get("DGX_GPU_EXPORTER_TIMEOUT", "8"))
#: Cached so several scrapers (or a human curling the endpoint) cannot
#: multiply the nvidia-smi load on a machine that is serving an LLM.
CACHE_TTL_S = float(os.environ.get("DGX_GPU_EXPORTER_CACHE_TTL", "1.5"))

log = logging.getLogger("dgx-gpu-exporter")

#: One call covers device state AND throttling: `nvidia-smi -q -d PERFORMANCE`
#: adds nothing the CSV interface does not already expose on GB10, so it is
#: not made.
CSV_FIELDS = [
    "index",
    "uuid",
    "name",
    "utilization.gpu",
    "utilization.memory",
    "temperature.gpu",
    "temperature.gpu.tlimit",
    "power.draw.average",
    "power.draw.instant",
    "clocks.sm",
    "clocks.gr",
    "clocks.max.sm",
    "pstate",
    "clocks_event_reasons.sw_power_cap",
    "clocks_event_reasons.hw_thermal_slowdown",
    "clocks_event_reasons.sw_thermal_slowdown",
    "clocks_event_reasons.hw_power_brake_slowdown",
    "clocks_event_reasons.hw_slowdown",
    "clocks_event_reasons.sync_boost",
    "clocks_event_reasons_counters.sw_power_cap",
    "clocks_event_reasons_counters.hw_thermal_slowdown",
    "clocks_event_reasons_counters.sw_thermal_slowdown",
    "clocks_event_reasons_counters.hw_power_brake_slowdown",
    "clocks_event_reasons_counters.sync_boost",
]

#: field suffix -> Prometheus `reason` label.
THROTTLE_REASONS = {
    "sw_power_cap": "sw_power_cap",
    "hw_thermal_slowdown": "hw_thermal_slowdown",
    "sw_thermal_slowdown": "sw_thermal_slowdown",
    "hw_power_brake_slowdown": "hw_power_brake",
    "hw_slowdown": "hw_slowdown",
    "sync_boost": "sync_boost",
}

_NA = {"n/a", "[n/a]", "[not supported]", "not supported", "unknown", ""}


def _is_na(value: str) -> bool:
    return value.strip().lower() in _NA


def _run(args: list[str]) -> str:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=SMI_TIMEOUT_S, check=True
    ).stdout


def query_devices() -> list[dict]:
    out = _run(
        [
            "nvidia-smi",
            f"--query-gpu={','.join(CSV_FIELDS)}",
            "--format=csv,noheader,nounits",
        ]
    )
    rows = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != len(CSV_FIELDS):
            continue
        rows.append(dict(zip(CSV_FIELDS, parts)))
    return rows


def query_processes() -> list[dict]:
    """Per-process GPU memory. Empty without a shared PID namespace."""
    try:
        out = _run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ]
        )
    except Exception:  # noqa: BLE001 — device metrics stand on their own
        log.debug("compute-apps query failed", exc_info=True)
        return []
    procs = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 3 or _is_na(parts[2]):
            continue
        try:
            mib = float(parts[2])
        except ValueError:
            continue
        # Full paths make for unusable legends; the basename is the identity
        # that matters (VLLM::Worker_TP0, VLLM::EngineCore).
        procs.append(
            {"pid": parts[0], "name": os.path.basename(parts[1]) or parts[1], "bytes": mib * 1024 * 1024}
        )
    return procs


class Collector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._body = ""
        self._at = 0.0
        self._errors = 0

    def render(self) -> str:
        with self._lock:
            if self._body and time.monotonic() - self._at < CACHE_TTL_S:
                return self._body
            self._body = self._build()
            self._at = time.monotonic()
            return self._body

    def _build(self) -> str:
        started = time.monotonic()
        lines: list[str] = []

        def head(name: str, mtype: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {mtype}")

        try:
            rows = query_devices()
            ok = 1
        except Exception as exc:  # noqa: BLE001 — publish the outage as data
            log.warning("nvidia-smi query failed: %s", exc)
            self._errors += 1
            rows, ok = [], 0

        head("dgx_gpu_up", "gauge", "1 if nvidia-smi answered this scrape.")
        lines.append(f"dgx_gpu_up {ok}")
        head(
            "dgx_gpu_scrape_errors_total",
            "counter",
            "nvidia-smi invocations that failed since exporter start.",
        )
        lines.append(f"dgx_gpu_scrape_errors_total {self._errors}")

        if rows:
            def labels(row: dict) -> str:
                return f'gpu="{row["index"]}",uuid="{row["uuid"]}",name="{row["name"]}"'

            def emit(name, mtype, help_text, field, scale=1.0) -> None:
                head(name, mtype, help_text)
                for row in rows:
                    raw = row.get(field, "")
                    if _is_na(raw):
                        continue  # the driver has no such reading on GB10
                    try:
                        lines.append(f"{name}{{{labels(row)}}} {float(raw) * scale}")
                    except ValueError:
                        continue

            emit("dgx_gpu_utilization_percent", "gauge",
                 "Percent of the last sample period with kernels executing.",
                 "utilization.gpu")
            emit("dgx_gpu_memory_controller_utilization_percent", "gauge",
                 ("Percent of the sample period the memory controller was busy. NOT "
                  "capacity used - GB10 reports no framebuffer capacity at all."),
                 "utilization.memory")
            emit("dgx_gpu_temperature_celsius", "gauge",
                 ("GPU die temperature. On this integrated part the die is shared with "
                  "the CPU complex, so heavy CPU-only load also moves this number."),
                 "temperature.gpu")
            emit("dgx_gpu_temperature_headroom_celsius", "gauge",
                 "Degrees below the driver's thermal limit before it throttles.",
                 "temperature.gpu.tlimit")
            emit("dgx_gpu_power_watts", "gauge",
                 ("GPU/accelerator power draw, averaged by the driver. NOT whole-machine "
                  "AC power - this hardware exposes no wall-socket telemetry."),
                 "power.draw.average")
            emit("dgx_gpu_power_instant_watts", "gauge",
                 "Instantaneous GPU/accelerator power draw.",
                 "power.draw.instant")
            emit("dgx_gpu_clock_sm_hertz", "gauge", "SM clock.", "clocks.sm", 1e6)
            emit("dgx_gpu_clock_graphics_hertz", "gauge", "Graphics clock.", "clocks.gr", 1e6)
            emit("dgx_gpu_clock_sm_max_hertz", "gauge",
                 "Maximum SM clock the part will boost to.", "clocks.max.sm", 1e6)

            head("dgx_gpu_performance_state", "gauge",
                 "Performance state as a number: P0 -> 0, P8 -> 8. Lower is faster.")
            for row in rows:
                m = re.match(r"^P(\d+)$", row.get("pstate", "").strip())
                if m:
                    lines.append(f"dgx_gpu_performance_state{{{labels(row)}}} {m.group(1)}")

            head("dgx_gpu_throttle_active", "gauge",
                 "1 while this clock-event (throttle) reason is active.")
            for row in rows:
                for field, reason in THROTTLE_REASONS.items():
                    raw = row.get(f"clocks_event_reasons.{field}", "")
                    if _is_na(raw):
                        continue
                    active = 1 if raw.strip().lower().startswith("active") else 0
                    lines.append(
                        f'dgx_gpu_throttle_active{{gpu="{row["index"]}",reason="{reason}"}} {active}'
                    )

            head("dgx_gpu_throttle_seconds_total", "counter",
                 "Cumulative seconds the GPU has been throttled for this reason.")
            for row in rows:
                for field, reason in THROTTLE_REASONS.items():
                    raw = row.get(f"clocks_event_reasons_counters.{field}", "")
                    if _is_na(raw):
                        continue
                    try:
                        # nvidia-smi reports microseconds; Prometheus wants base units.
                        seconds = float(raw) / 1_000_000.0
                    except ValueError:
                        continue
                    lines.append(
                        f'dgx_gpu_throttle_seconds_total{{gpu="{row["index"]}",'
                        f'reason="{reason}"}} {seconds}'
                    )

            # --- GPU memory, the only accounting this part offers ----------
            procs = query_processes()
            head("dgx_gpu_process_memory_bytes", "gauge",
                 ("GPU memory held by one CUDA context. The device-level total/used/free "
                  "counters are N/A on GB10, so this per-process view is the only GPU "
                  "memory accounting available. Empty unless the exporter shares the "
                  "host PID namespace."))
            for proc in procs:
                lines.append(
                    f'dgx_gpu_process_memory_bytes{{pid="{proc["pid"]}",'
                    f'process="{proc["name"]}"}} {proc["bytes"]}'
                )
            head("dgx_gpu_memory_allocated_bytes", "gauge",
                 ("Sum of all CUDA context allocations. The closest honest equivalent to "
                  "'GPU memory used' on a unified-memory part; there is no capacity "
                  "denominator, so compare it against the host's MemTotal."))
            lines.append(f"dgx_gpu_memory_allocated_bytes {sum(p['bytes'] for p in procs)}")
            head("dgx_gpu_compute_processes", "gauge",
                 "Number of processes holding a CUDA context.")
            lines.append(f"dgx_gpu_compute_processes {len(procs)}")

        head("dgx_gpu_scrape_duration_seconds", "gauge",
             "Wall time this exporter spent collecting the sample.")
        lines.append(f"dgx_gpu_scrape_duration_seconds {time.monotonic() - started:.6f}")
        return "\n".join(lines) + "\n"


COLLECTOR = Collector()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler API
        path = self.path.rstrip("/")
        if path in ("/metrics", ""):
            body = COLLECTOR.render().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"ok\n")
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # Per-scrape access logs at a 5 s interval are pure noise.
        log.debug(fmt, *args)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("DGX_GPU_EXPORTER_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        devices = query_devices()
        procs = query_processes()
        log.info("nvidia-smi reachable; %d GPU(s), %d CUDA context(s)", len(devices), len(procs))
        if devices and not procs:
            log.warning(
                "no CUDA contexts visible - if this container was started without "
                "`pid: host`, dgx_gpu_process_memory_bytes will stay empty"
            )
    except Exception as exc:  # noqa: BLE001 — serve dgx_gpu_up 0 rather than die
        log.warning(
            "nvidia-smi is not answering (%s). Serving dgx_gpu_up 0 so the outage "
            "is visible in Prometheus rather than silent.",
            exc,
        )
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.daemon_threads = True
    log.info("listening on %s:%d/metrics", LISTEN_HOST, LISTEN_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
