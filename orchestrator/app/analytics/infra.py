"""Infrastructure telemetry for the analytics console — read from Prometheus.

WHY PROMETHEUS AND NOT A NEW EXPORTER. Everything this module needs is
already scraped every 15 seconds: `dgx_gpu_*` from monitoring/exporters/
dgx-gpu on both Sparks, `node_*` from node_exporter, and the `vllm:*` family
from each engine. Re-collecting any of it in the orchestrator would be a
second source of truth that disagrees with Grafana on the same screen.

WHAT THE LABELS MEAN HERE.

  node     spark-1 (head) / spark-2 (worker) — the physical machine
  service  main / router / embed / ocr / reranker — which engine
  role     head / worker

DEGRADED, NOT BROKEN. Prometheus being unreachable is a normal state for a
self-hosted stack (it is an optional profile). Every function returns
`available: False` with a reason instead of raising, and the console renders
"telemetry unavailable" rather than zeros — a GPU reported at 0% because
nobody asked is indistinguishable from an idle GPU, and that is exactly the
confusion this avoids.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

#: Overridable for a stack whose Prometheus lives elsewhere; the default is
#: the compose service name on the shared `application` network.
PROM_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")

#: Prometheus is a monitoring dependency, not a request dependency. A console
#: page must not hang on it.
TIMEOUT_S = float(os.environ.get("PROMETHEUS_TIMEOUT_S", "4"))


class Unavailable(Exception):
    """Prometheus could not answer. Carries a reason fit to show a human."""


async def _query(expr: str) -> List[Dict[str, Any]]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            res = await client.get(
                f"{PROM_URL}/api/v1/query", params={"query": expr}
            )
            res.raise_for_status()
            body = res.json()
    except Exception as exc:  # noqa: BLE001 — every failure is the same answer
        raise Unavailable(str(exc)) from exc
    if body.get("status") != "success":
        raise Unavailable(str(body.get("error") or "query failed"))
    return list(body.get("data", {}).get("result", []))


async def _query_range(expr: str, start: float, end: float, step: int) -> List[Dict[str, Any]]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S * 2) as client:
            res = await client.get(
                f"{PROM_URL}/api/v1/query_range",
                params={"query": expr, "start": start, "end": end, "step": step},
            )
            res.raise_for_status()
            body = res.json()
    except Exception as exc:  # noqa: BLE001
        raise Unavailable(str(exc)) from exc
    if body.get("status") != "success":
        raise Unavailable(str(body.get("error") or "query failed"))
    return list(body.get("data", {}).get("result", []))


def _scalar(rows: List[Dict[str, Any]]) -> Optional[float]:
    for row in rows:
        try:
            return float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return None


def _by_label(rows: List[Dict[str, Any]], label: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for row in rows:
        key = str(row.get("metric", {}).get(label) or "")
        if not key:
            continue
        try:
            out[key] = float(row["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return out


async def _gather(**queries: str) -> Dict[str, List[Dict[str, Any]]]:
    """Run the whole page's queries concurrently — a nodes page is a dozen
    instant vectors and running them in series would make it feel slow for no
    reason."""
    keys = list(queries)
    results = await asyncio.gather(
        *(_query(queries[k]) for k in keys), return_exceptions=True
    )
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, result in zip(keys, results):
        if isinstance(result, Exception):
            raise Unavailable(str(result))
        out[key] = result
    return out


async def nodes() -> Dict[str, Any]:
    """Per-machine health: GPU, memory, temperature, power, CPU, RAM, network.

    One row per `node` label, assembled from several instant vectors so a
    machine whose GPU exporter is down still reports its CPU rather than
    disappearing from the list.
    """
    q = await _gather(
        gpu_up="dgx_gpu_up",
        gpu_util="dgx_gpu_utilization_percent",
        gpu_mem="dgx_gpu_memory_allocated_bytes",
        gpu_temp="dgx_gpu_temperature_celsius",
        gpu_power="dgx_gpu_power_instant_watts",
        gpu_throttle="dgx_gpu_throttle_active",
        gpu_procs="dgx_gpu_compute_processes",
        gpu_clock="dgx_gpu_clock_sm_hertz",
        node_up='up{job="node"}',
        cpu=(
            '100 - (avg by (node) (rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
        ),
        mem_total="node_memory_MemTotal_bytes",
        mem_avail="node_memory_MemAvailable_bytes",
        swap_total="node_memory_SwapTotal_bytes",
        swap_free="node_memory_SwapFree_bytes",
        uptime="node_time_seconds - node_boot_time_seconds",
        rx="sum by (node) (rate(node_network_receive_bytes_total[5m]))",
        tx="sum by (node) (rate(node_network_transmit_bytes_total[5m]))",
        load="node_load1",
    )
    names = set()
    for rows in q.values():
        for row in rows:
            name = row.get("metric", {}).get("node")
            if name:
                names.add(str(name))
    roles = {
        str(r["metric"].get("node")): str(r["metric"].get("role") or "")
        for r in q["node_up"] + q["gpu_up"]
        if r.get("metric", {}).get("node")
    }
    out = []
    for name in sorted(names):
        pick = lambda key: _by_label(q[key], "node").get(name)  # noqa: E731
        mem_total, mem_avail = pick("mem_total"), pick("mem_avail")
        swap_total, swap_free = pick("swap_total"), pick("swap_free")
        out.append(
            {
                "node": name,
                "role": roles.get(name, ""),
                "gpu_present": pick("gpu_up") is not None,
                "gpu_up": pick("gpu_up") == 1,
                "node_up": pick("node_up") == 1,
                "gpu_utilization": pick("gpu_util"),
                "gpu_memory_bytes": pick("gpu_mem"),
                "gpu_temperature_c": pick("gpu_temp"),
                "gpu_power_w": pick("gpu_power"),
                "gpu_throttled": pick("gpu_throttle") == 1,
                "gpu_processes": pick("gpu_procs"),
                "gpu_clock_hz": pick("gpu_clock"),
                "cpu_percent": pick("cpu"),
                "load1": pick("load"),
                "memory_total_bytes": mem_total,
                "memory_used_bytes": (
                    None if mem_total is None or mem_avail is None
                    else mem_total - mem_avail
                ),
                "swap_used_bytes": (
                    None if swap_total is None or swap_free is None
                    else swap_total - swap_free
                ),
                "uptime_seconds": pick("uptime"),
                "network_rx_bps": pick("rx"),
                "network_tx_bps": pick("tx"),
            }
        )
    return {"available": True, "nodes": out}


async def engines() -> Dict[str, Any]:
    """Per-vLLM-engine state: which model it serves, what it is doing now, and
    how it has performed since it started.

    The rate() windows are 30 minutes because these engines are not busy
    continuously — a 5-minute window on an idle stack reports NaN and the page
    would show a working engine as having no throughput.
    """
    q = await _gather(
        info='vllm:num_requests_running',
        running="vllm:num_requests_running",
        waiting="vllm:num_requests_waiting",
        kv="vllm:kv_cache_usage_perc",
        prompt_rate="sum by (service) (rate(vllm:prompt_tokens_total[30m]))",
        gen_rate="sum by (service) (rate(vllm:generation_tokens_total[30m]))",
        prompt_total="sum by (service) (vllm:prompt_tokens_total)",
        gen_total="sum by (service) (vllm:generation_tokens_total)",
        ttft=(
            "sum by (service) (rate(vllm:time_to_first_token_seconds_sum[30m])) "
            "/ sum by (service) (rate(vllm:time_to_first_token_seconds_count[30m]))"
        ),
        e2e=(
            "sum by (service) (rate(vllm:e2e_request_latency_seconds_sum[30m])) "
            "/ sum by (service) (rate(vllm:e2e_request_latency_seconds_count[30m]))"
        ),
        queue=(
            "sum by (service) (rate(vllm:request_queue_time_seconds_sum[30m])) "
            "/ sum by (service) (rate(vllm:request_queue_time_seconds_count[30m]))"
        ),
        itl=(
            "sum by (service) (rate(vllm:inter_token_latency_seconds_sum[30m])) "
            "/ sum by (service) (rate(vllm:inter_token_latency_seconds_count[30m]))"
        ),
        finished="sum by (service) (vllm:request_success_total)",
        preempt="sum by (service) (vllm:num_preemptions_total)",
        cache_hits="sum by (service) (vllm:prefix_cache_hits_total)",
        cache_queries="sum by (service) (vllm:prefix_cache_queries_total)",
    )
    meta: Dict[str, Dict[str, str]] = {}
    for row in q["info"]:
        labels = row.get("metric", {})
        service = str(labels.get("service") or "")
        if service:
            meta[service] = {
                "model": str(labels.get("model_name") or ""),
                "node": str(labels.get("node") or labels.get("role") or ""),
                "instance": str(labels.get("instance") or ""),
            }
    out = []
    for service in sorted(meta):
        pick = lambda key: _by_label(q[key], "service").get(service)  # noqa: E731
        hits, queries = pick("cache_hits"), pick("cache_queries")
        out.append(
            {
                "service": service,
                **meta[service],
                "running": pick("running"),
                "waiting": pick("waiting"),
                "kv_cache_percent": (
                    None if pick("kv") is None else pick("kv") * 100
                ),
                "prompt_tokens_per_second": pick("prompt_rate"),
                "generation_tokens_per_second": pick("gen_rate"),
                "prompt_tokens_total": pick("prompt_total"),
                "generation_tokens_total": pick("gen_total"),
                "avg_ttft_seconds": pick("ttft"),
                "avg_e2e_seconds": pick("e2e"),
                "avg_queue_seconds": pick("queue"),
                "avg_inter_token_seconds": pick("itl"),
                "finished_requests": pick("finished"),
                "preemptions": pick("preempt"),
                "prefix_cache_hit_rate": (
                    None if not hits or not queries else hits / queries
                ),
            }
        )
    return {"available": True, "engines": out}


async def gpu_series(hours: int = 6, step: int = 120) -> Dict[str, Any]:
    """GPU utilisation / memory / temperature / power over time, per node."""
    import time

    end = time.time()
    start = end - hours * 3600
    wanted = {
        "utilization": "dgx_gpu_utilization_percent",
        "memory": "dgx_gpu_memory_allocated_bytes",
        "temperature": "dgx_gpu_temperature_celsius",
        "power": "dgx_gpu_power_instant_watts",
    }
    out: Dict[str, Any] = {"available": True, "hours": hours, "series": {}}
    for key, expr in wanted.items():
        rows = await _query_range(expr, start, end, step)
        out["series"][key] = [
            {
                "node": str(r.get("metric", {}).get("node") or ""),
                "points": [
                    [int(float(t)), None if v in ("NaN", None) else float(v)]
                    for t, v in r.get("values", [])
                ],
            }
            for r in rows
        ]
    return out


async def inference_series(hours: int = 6, step: int = 120) -> Dict[str, Any]:
    """Fleet-wide inference performance over time: TTFT, throughput, queue
    depth and concurrency, from the engines themselves."""
    import time

    end = time.time()
    start = end - hours * 3600
    wanted = {
        "ttft_seconds": (
            "sum by (service) (rate(vllm:time_to_first_token_seconds_sum[10m])) "
            "/ sum by (service) (rate(vllm:time_to_first_token_seconds_count[10m]))"
        ),
        "generation_tokens_per_second": (
            "sum by (service) (rate(vllm:generation_tokens_total[10m]))"
        ),
        "requests_running": "sum by (service) (vllm:num_requests_running)",
        "requests_waiting": "sum by (service) (vllm:num_requests_waiting)",
        "kv_cache_percent": "sum by (service) (vllm:kv_cache_usage_perc) * 100",
    }
    out: Dict[str, Any] = {"available": True, "hours": hours, "series": {}}
    for key, expr in wanted.items():
        rows = await _query_range(expr, start, end, step)
        out["series"][key] = [
            {
                "service": str(r.get("metric", {}).get("service") or "all"),
                "points": [
                    [int(float(t)), None if v in ("NaN", None) else float(v)]
                    for t, v in r.get("values", [])
                ],
            }
            for r in rows
        ]
    return out


async def safe(coro) -> Dict[str, Any]:
    """Await an infra call, turning unavailability into a renderable answer.

    The console needs to distinguish three states — a number, "no telemetry
    configured", and "the collector is down" — and only the first is a
    number. Collapsing the other two into 0 is how dashboards come to lie.
    """
    try:
        return await coro
    except Unavailable as exc:
        return {"available": False, "reason": str(exc), "source": PROM_URL}
    except Exception as exc:  # noqa: BLE001
        log.debug("infra telemetry failed", exc_info=True)
        return {"available": False, "reason": str(exc), "source": PROM_URL}
