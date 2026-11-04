# Monitoring — Grafana + Prometheus for the DGX Spark fleet

One browser tab that answers, live: *is the fleet healthy, what needs attention,
is every Spark alive, how hot are they, how much power are they drawing, is the
distributed model actually distributed, what is every container and every database
doing, and if throughput drops — why?*

**Built for any number of Sparks.** No dashboard, rule or scrape config names one:
a new DGX Spark on the management LAN is found by itself and every page grows by
one. See [Adding a Spark](#adding-a-spark).

Everything runs locally. No Grafana Cloud, no SaaS, no paid API, no data
leaving the machines.

---

## Quick start

```bash
./scripts/monitoring.sh up        # start here + the exporters on every Spark, turn discovery on
./scripts/monitoring.sh status    # containers + every Prometheus target
./scripts/monitoring.sh verify    # prove EVERY Spark is reporting
./scripts/monitoring.sh fleet     # which Sparks exist, and what state each is in
./scripts/monitoring.sh logs      # follow logs (add a service name to filter)
./scripts/monitoring.sh restart
./scripts/monitoring.sh stop      # stop, keep containers
./scripts/monitoring.sh down      # remove containers, KEEP metric history
./scripts/monitoring.sh url       # the URL and how to log in
```

| | URL |
|---|---|
| **Grafana** | <http://127.0.0.1:3300> |
| **Prometheus** | <http://127.0.0.1:9090> |

Log in to Grafana as `admin`. The password is generated once into
`.runtime/secrets.env` (mode 0600, gitignored) and is never printed by the
tooling or committed:

```bash
grep '^GRAFANA_ADMIN_PASSWORD=' .runtime/secrets.env | cut -d= -f2-
```

Grafana refuses to start if that variable is missing — deliberately, so it can
never come up as `admin/admin` on a LAN with no firewall.

---

## Architecture

```
                            Browser
                               |
                          Grafana :3300          Spark 1  "spark-1"  head
                               |                 192.168.9.54
                         Prometheus :9090 <---- .runtime/prometheus/sparks-*.json
                       /       |        \            ^
          node-exporter   dgx-gpu-      blackbox      |  every minute
             :9100        exporter    (app liveness)  |
               ^           :9835                 scripts/lib/fleet.py
               |                                 (systemd user timer, mDNS)
        spark-probe, spark-local
        (write files, open no port)
                               |
                               |  management LAN 192.168.9.0/22
                               v
          node-exporter :9100     dgx-gpu-exporter :9835      Spark 2, Spark 3, ...
                 ^                                            each identical
          spark-probe, spark-local

          vLLM :8000 /metrics  <- ONE endpoint, whole distributed engine
          vLLM :8002/:8003/:8004/:8005  router / embed / OCR / reranker
          engine-controller :9838  <- the real canary; /state, /metrics
                 |  RoCE rail A (10.100.184.0/24) — the ONE monitoring
                 v  hop that must use the fabric, see below
          vllm-worker-sentinel :9839              Spark 2 (not scraped)
```

**Scraping never touches the RoCE fabric.** Prometheus reaches Spark 2 over
the management interface `enP7s7` (192.168.9.0/22). The two ConnectX links
(10.100.184.0/24 and 10.100.185.0/24) are reserved for NCCL tensor-parallel
traffic — monitoring must not compete with the thing it measures.

### Components and why each one is here

| Component | Where | Purpose |
|---|---|---|
| Prometheus 3.1.0 | Spark 1 | scrape + store + rules |
| Grafana 11.5.1 | Spark 1 | dashboards, provisioned from this repo |
| node-exporter 1.9.1 | **every Spark** | CPU, unified memory, disk, **RoCE/IB counters**, systemd units, and the files below |
| `dgx-gpu-exporter` (custom) | **every Spark** | GPU telemetry from `nvidia-smi`, per-process busy share |
| `spark-probe` (custom) | **every Spark** | containers, databases, kernel faults, RoCE hardware counters, top processes. **Opens no port** |
| `spark-local` (custom) | **every Spark** | NVMe wear and power-loss count, Docker health. **Has no network at all** |
| `fleet.py` (custom) | Spark 1, user timer | finds Sparks over mDNS, enrols them, writes Prometheus' targets |
| blackbox-exporter 0.25.0 | Spark 1 | HTTP liveness for services with no `/metrics` |
| postgres-exporter 0.16.0 | Spark 1 | PostgreSQL server statistics |
| `data-stores-exporter` (custom) | Spark 1 | DuckDB / LanceDB / Parquet size + freshness |
| `engine-controller` (custom) | Spark 1 | the vLLM engine's **real** state: a streamed canary completion every 30 s, both ranks, coordinated recovery; `techsara_vllm_*` |
| `vllm-worker-sentinel` (custom) | Spark 2 | watches the worker rank process for the controller; **not scraped** (see Ports) |

Loki/Promtail are deliberately **not** included: this is metric observability,
and log aggregation would be a second storage system to run and bound.

### Ports

| Port | What | Where it binds | Scraped by |
|---|---|---|---|
| 3300 | Grafana | `MONITORING_BIND_ADDRESS` (loopback) | — |
| 9090 | Prometheus | `MONITORING_BIND_ADDRESS` (loopback) | itself, 30 s |
| 9100 | node-exporter, carrying `spark-probe` and `spark-local` through its textfile collector | host network, every Spark (other than the head: management IP only) | `node`, 10 s |
| 9835 | `dgx-gpu-exporter` | every Spark (other than the head: management IP only) | `dgx-gpu`, 5 s |
| 9836 | `data-stores-exporter` | `application` network | `data-stores`, 30 s |
| 9187 | postgres-exporter | `application` network | `postgres`, 15 s |
| 9115 | blackbox-exporter | `application` network | `blackbox-http`, 15 s |
| 8000 | vLLM main engine `/metrics` | head, host network | `vllm-main`, 5 s |
| 30002 / 30003 / 30005 / 30004 | router / embed / reranker / OCR `/metrics` | `application` network (OCR: wherever `scripts/ocr.sh` put it) | `vllm-aux`, 15 s |
| **9838** | **`engine-controller`** — `/state`, `/metrics`, `/healthz`; `POST /recover` from loopback only | head, host network, all interfaces | **`engine-controller`, 5 s** |
| **9839** | **`vllm-worker-sentinel`** — `/state`, `/diagnostics`, `POST /restart` | Spark 2, **the RoCE rail-A address only** (`CLUSTER_WORKER_IP`) | **nobody** |

The sentinel is the one exception to "monitoring never touches the RoCE
fabric", and it is an exception for the controller, not for Prometheus: it
binds to the rail-A address because that is a point-to-point link only the
head can reach, so a `POST /restart` can never arrive from the office LAN. Its
few bytes a second ride a fabric that moves a gigabyte a second under load.
Prometheus still does not scrape it — everything it knows is folded into the
controller's `techsara_vllm_worker_*` gauges, scraped over the management LAN
like everything else.

---

## GPU telemetry on GB10 — and why not DCGM

`dcgm-exporter` was tested on this hardware, not assumed. The honest result:
**it runs, but it is a strict subset that costs more than it returns here.**

* Framebuffer memory is unavailable at the **DCGM library level**, not just in
  the exporter — `dcgmi dmon -e 250,251,252,253` returns `N/A` for `fb_total`,
  `fb_free`, `fb_used`, `fb_resv`. So do power limits, ECC and remapped rows.
  8 of its 13 default series are structurally zero on this part.
* The DCP profiling module will not load (`Result: -33`), so there is no
  `SM_ACTIVE`, `DRAM_ACTIVE`, `TENSOR_ACTIVE` or PCIe byte counter either.
* Its default 30 s collection interval made values dangerously stale: during a
  live generation it reported `GPU_UTIL=85` for four seconds *after* the load
  had finished while `nvidia-smi` already read 0 %.
* It cannot attribute memory to processes at all (`compute_pids` is `N/A`).

So GPU metrics come from a small stdlib-only exporter
(`monitoring/exporters/dgx-gpu/dgx_gpu_exporter.py`) that runs `nvidia-smi` on
a stock `python:3.12-slim` — the NVIDIA container runtime injects `nvidia-smi`
into any container started with `--gpus all`, so there is no build step and no
pip install.

### What GB10 does and does not report

| Works | Reported `N/A` by the driver |
|---|---|
| GPU utilisation | `memory.total` / `used` / `free` |
| memory-controller utilisation | `power.limit`, `enforced.power.limit` |
| temperature + thermal-limit headroom | `clocks.mem` |
| power draw (average and instant) | `fan.speed` |
| SM / graphics / max clocks | memory temperature |
| performance state | ECC counters |
| throttle reasons **and** their cumulative counters | |
| per-process GPU memory | |

**Metrics that would have to be invented are absent, not zero.** There is no
`dgx_gpu_memory_used_bytes` panel showing 0 — the metric simply does not exist.

### GPU memory on a unified-memory part

The GB10 has no separate framebuffer, so the device-level memory counters are
`N/A`. Two honest substitutes are provided:

* `dgx_gpu_memory_allocated_bytes` — the sum of all CUDA context allocations,
  from `nvidia-smi --query-compute-apps`. This is the only GPU memory
  accounting the hardware offers. It has **no capacity denominator**.
* **Unified memory** on the Host dashboard (`node_memory_*`) — on this
  architecture the model weights and KV cache live in host memory, so this is
  the real memory-pressure signal. Running out shows up as CUDA OOM in vLLM,
  never as an `nvidia-smi` memory reading.

> The GPU exporter needs `pid: host`. Without a shared PID namespace,
> `--query-compute-apps` returns an **empty list with exit code 0** — the
> per-process metrics would silently be missing rather than error.

### Temperature, honestly

`temperature.gpu` on this integrated part is a **shared SoC die temperature**.
An 8-thread CPU-only burn moved it 44 °C → 48 °C while GPU power moved 0.31 W.
Treat it as a machine temperature, not proof of GPU load.

Thresholds are the hardware's own limits, read once via DCGM (nvidia-smi
reports all three as `N/A`): **slowdown 86 °C, shutdown 90 °C, max operating
99 °C**. Dashboards go amber at 86 and red at 90.

---

## Combined power — what it does and does not mean

`cluster:gpu_power_watts:sum` = Spark 1 GPU watts + Spark 2 GPU watts.

**It is the accelerator rail only. It is NOT whole-machine AC draw.** That is
not a caveat about accuracy — whole-machine power is *not measurable at all* on
this hardware:

* `/sys/class/power_supply` and `/sys/class/powercap` are both **empty**
* there are **zero** `power*` / `curr*` / `energy*` / `in*` inputs under any
  hwmon device
* `/sys/bus/iio` does not exist, and the six i2c adapters are GPU-internal with
  no INA rails bound

The driver also reports **no power limit**, so there is no
percentage-of-cap or headroom figure anywhere.

Energy panels (`last 1 h`, `last 24 h`) are `avg_over_time(power) × duration` —
an integration of the 5 s samples, accurate to the sampling, and still GPU-rail
only.

---

## The interconnect: read the IB counters, never netdev

**RoCE/RDMA traffic is invisible to the ordinary network counters.** Measured
during one 300-token generation:

| Counter | Movement |
|---|---|
| IB `port_xmit_data` on `rocep1s0f1` | **+678 MB** |
| IB `port_xmit_data` on `roceP2p1s0f1` | **+146 MB** |
| netdev `tx_bytes` on `enp1s0f1np1` | +170 kB (TCP bootstrap only) |
| netdev `tx_bytes` on `enP2p1s0f1np1` | **exactly 0** |

A netdev-based interconnect panel would read flat zero on a perfectly healthy
fabric. Every RoCE panel therefore uses node-exporter's **infiniband**
collector (on by default; it must not be disabled).

| Rail | IB device | netdev | subnet | NCCL |
|---|---|---|---|---|
| 1 | `rocep1s0f1` | `enp1s0f1np1` | 10.100.184.0/24 | `NET/IB/0` |
| 2 | `roceP2p1s0f1` | `enP2p1s0f1np1` | 10.100.185.0/24 | `NET/IB/1` |

The `...f0` ports are DOWN by design and are filtered out everywhere.

**The split is not even** — one measured generation ran 81.5 % / 18.5 % across
the rails, so the dashboard shows them separately. A summed panel would hide a
dead rail.

There is deliberately **no link-utilisation-percent panel**. The original
reason given here — "this fabric measures ~13 Gb/s per direction" — was a
unit error and is withdrawn (see [`CLUSTER.md`](CLUSTER.md); re-measured
2026-09-07 at 108.91 Gb/s per rail and 171.57 Gb/s NCCL bus bandwidth, which
is 97 % of NVIDIA's healthy reference). The panel is still not worth adding,
but for a different and narrower reason: a single-device `ib_write_bw` on a
DGX Spark is PCIe-bound near ~109 Gb/s because one QSFP port reaches the SoC
through two independent PCIe Gen5 x4 links, so a percentage against the
advertised 200 Gb/s line rate compares against a ceiling this hardware cannot
reach through one device and would read low no matter how healthy the fabric
is.

### NCCL observability

NCCL is watched **indirectly** and cheaply — verbose `NCCL_DEBUG` is not
enabled in production. What proves the distributed model is alive:

1. **RoCE rail throughput** rising during generation (the direct evidence)
2. **GPU utilisation on both Sparks** rising together
3. `up{job="dgx-gpu"}` for both nodes
4. vLLM `num_requests_running` on the head

For a deeper session, `scripts/cluster-doctor.sh` and
`scripts/cluster-bench.sh` already exist for on-demand NCCL diagnostics.

---

## vLLM metrics: one endpoint, whole engine

Spark 2 runs vLLM with `--headless --node-rank 1` and has **no API server** —
curl to 8000/8001/8080 on the worker is refused. The head's `EngineCore`
schedules both tensor-parallel ranks, so the counters on
`http://<head>:8000/metrics` already describe the **whole distributed engine**.

They are therefore **not** labelled per node. Splitting `tokens/sec` by Spark
would invent a distinction the metric does not have.

Latency percentiles are only shown where the histogram buckets support them:

* **`time_to_first_token_seconds` and `inter_token_latency_seconds`** have
  fine-grained ladders → p50/p90/p95/p99 are meaningful.
* **`e2e_request_latency_seconds`** shares a coarse ladder starting at
  `le=0.3` → percentiles are shown but are coarse for short requests.
* **`request_queue_time_seconds` and `request_prefill_time_seconds`** share
  that same ladder, and observed queue time is ~16 µs, so every sample lands in
  bucket one. These are plotted as **means, not percentiles** — a p95 would
  read 0.3 s and be meaningless.

---

## Engine state — what each top-level state means and which signal drives it

**A process being alive proves nothing.** On 2026-09-11 the worker rank on
Spark 2 died at 22:15:50 UTC in a GDN prefill kernel. For the next five
minutes `up{job="vllm-main"}` read 1, `/health` and `/v1/models` answered
200, `/metrics` kept serving — with `generation_tokens_total` frozen at
2,138,504 and `num_requests_running` frozen at 9 — and the overview's "vLLM"
tile said **UP**. Ten requests hung until vLLM's own 300 s execute timeout
turned them into HTTP 500s. Only then did `up` go to 0, and the "vLLM DOWN"
that Grafana finally showed described the five minutes the restart was
*designed* to take, not the five minutes that mattered.

So the engine's state on the overview no longer comes from Prometheus asking
whether a port answers. It comes from the **engine controller**
(`monitoring/engine-controller/`, compose service `engine-controller`, port
9838), which every 30 s sends the engine a real streamed chat completion,
watches the worker rank through the sentinel on Spark 2, and publishes one of
exactly nine states. The contract is
[`availability/CONTRACT.md`](availability/CONTRACT.md) (v2, **strict
one-model mode**: only `nvidia/Qwen3.6-35B-A3B-NVFP4` ever answers a person;
there is no fallback model, and while the primary is not READY the
orchestrator holds every accepted request in a durable queue and resumes the
same generation when READY returns). This table is the operator's view of it.

| Code | State | What it means | The signal that decides it | Alert |
|---|---|---|---|---|
| 0 | `MONITORING_UNKNOWN` | the telemetry cannot be observed, or the controller's verdict is unproven. **Never rendered as DOWN.** | the controller cannot observe (it exports 0); **or** Grafana finds no series at all (`noValue`); **or** the controller's snapshot stamp `techsara_vllm_generated_at_seconds` is more than 30 s old (a frozen tick) — the derived state drops its sample; **or** `absent_over_time(techsara_vllm_state_code[2m])` | `VllmMonitoringUnknown` (warning, labelled with *what* is missing: `controller_state`, `vllm_scrape_target`, `controller_observation`, `controller_stale`, `controller_stamp`); `VllmReadyUnproven` (critical) when READY is claimed with no real success for 2 minutes |
| 1 | `STARTING` | containers up, model loading; the readiness sequence has not passed since the head started; cold-start budget (900 s) not exceeded. Also the shape of a head restarted **outside** the controller (Docker's restart policy, an operator): the reason reads `head restarted outside the controller; re-pairing the worker`, the controller has restarted the worker through the sentinel, no head restart, no budget spent (category `head_restarted_externally`, `techsara_vllm_recovery_attempts_total{outcome="repaired_worker"}`) | head `started_at` newer than the last proven readiness | none — `VllmScrapeTargetDown`, `HeadSwapActivity` and `NcclTcpFallback` are silenced here |
| 2 | `READY` | the **readiness sequence** passed and a canary succeeded within the last two probe intervals: (1) a non-streaming completion, (2) a streaming completion with TTFT measured and a terminal chunk seen, (3) `generation_tokens_total` on `/metrics` advanced by at least the tokens received, (4) both GPU exporters seen above 30 % during a 256-token participation probe, (5) the sentinel reports the rank process alive | `techsara_vllm_synthetic_success` + `techsara_vllm_participation_ok` + `techsara_vllm_worker_rank_alive` + `cluster:vllm_last_success_age_seconds` | `VllmPrimaryRestored` (info) when it follows an outage |
| 3 | `BUSY` | READY, and `vllm:num_requests_running > 0` | as READY, plus the scheduler gauge | `VllmQueueBacklog` (warning) if waiting > 5 for 5 m |
| 4 | `DEGRADED` | the canary still succeeds but a non-critical signal failed: sentinel unreachable, metrics stale, canary TTFT > 10 s, the router *classifier* unhealthy (it routes intents; it never answers a person), a GPU exporter unreachable | `techsara_vllm_worker_reachable`, `techsara_vllm_head_metrics_ok`, `techsara_vllm_synthetic_ttft_seconds`, `techsara_vllm_router_available`, `techsara_vllm_participation_ok` | `VllmCanaryTtftHigh`, `VllmMetricsStale`, `HeadSwapActivity`, `NcclTcpFallback`, `RoceRailMissing` (all warning) |
| 5 | `WEDGED` | requests exist but neither token counter moves for ≥ 90 s, or the canary has been outstanding ≥ 60 s while `/health` still answers 200; two consecutive observations; or three canary probes answered with an error; or every canary failing for 120 s. **Known false positive:** one long prompt prefilling alone — on 2026-09-12 09:04–09:16Z a ~950K-token needle moved neither counter for 680 s and the controller read WEDGED for 11 min 45 s while the engine was working (the restart was refused only by the spent budget; `availability/INCIDENT-2026-09-11-vllm.md` §7.2) | `techsara_vllm_generation_frozen_seconds` with running > 0; canary timeout | `VllmEngineWedged` (critical, 30 s), `VllmGenerationFrozen` (critical), `VllmWorkerRankAbsent` (critical) |
| 6 | `RECOVERING` | a coordinated recovery holds the lock: diagnostics captured, the pair restarted worker-first, model reloading, readiness sequence pending, queued generations resumed once it passes | `techsara_vllm_recovery_in_progress`, `techsara_vllm_recovery_step` | `VllmRecoveryStarted` (info); `VllmRepeatedRecoveryFailure` (critical) after two failures in an hour |
| 7 | `QUEUEING` | the primary is not READY/BUSY/DOWN **and** the orchestrator holds ≥ 1 accepted generation durably queued for it; the person reads *Main model is recovering—your request is safely queued.* and the **same** generation resumes when READY returns | derived in Prometheus (`cluster:vllm_service_state:code`): controller state ∉ {2, 3, 8} and `llm_queued_generations > 0` | `VllmRequestsQueued` (warning, after 2 m); `VllmQueueDrained` (info) when it ends |
| 8 | `DOWN` | the primary is not serving and the restart budget is exhausted or the last recovery failed; **nothing answers users** — queued requests are held (never lost, never answered by another model) until an operator acts | controller state 8 with a fresh verdict. A queue never softens 8 into 7 | `VllmPrimaryDown` (critical, 1 m), `VllmRecoveryBudgetExhausted` (critical) |

The overview's **vLLM engine** tile and the **Engine state** timeline in the
correlation strip both read `cluster:vllm_service_state:code`. The tiles next
to it are the signals behind the state, so a bad state can be read without
leaving the page: **Readiness** (proven / GPUs unproven / canary failing /
unproven / unknown — steps 2 and 4 of the readiness sequence, from
`techsara_vllm_synthetic_success` and `techsara_vllm_participation_ok`),
**Queued requests** (`llm_queued_generations`, the head-count of people
waiting; `none` when the orchestrator is not scraped), **Worker rank** (alive
/ ABSENT / unknown), **Last successful inference** (seconds since a real
completion — the one liveness number a hung process cannot fake), **Canary
TTFT**, **Incident duration**, **Recovery attempts (1h)** against the budget
of 3, and **Reason** (the controller's last failure category, a bounded set
that now includes `head_restarted_externally`).

What the series showed on 2026-09-12, the first day the controller ran in
production (Prometheus, `techsara_vllm_state_code` at 15 s resolution): the
controller went live at 08:44Z on candidate B; RECOVERING → READY three times
for drill 16 (08:45–08:58Z, 179–186 s each by
`techsara_vllm_recovery_duration_seconds`); DEGRADED → WEDGED → DEGRADED →
BUSY under the A/B needle (09:02–09:16Z, the false positive above, with
`llm_breaker_transitions_total{to="OPEN"}` +1 at 09:04:30Z); RECOVERING →
READY for drills 3 and 4 (09:46Z, 09:51Z; 170 and 168 s); STARTING for
12 min 45 s in the first drill-5 run (09:56–10:08Z, `cold_start_seconds`
428 s — the un-re-paired worker) and for ≈ 2.7 min in drill 6 and the drill-5
re-run (10:43Z, 10:46Z; `repaired_worker` 2); `llm_queued_generations` 1 → 0
at 10:09:00Z and again at 10:49:30Z; BUSY from 10:50:45Z under the soak.

### What "no data" means, and why it is never zero

Every one of those tiles distinguishes *absent* from *zero*. The engine tile
shows `MONITORING_UNKNOWN`, the readiness and rank tiles `unknown`, the queue
tile `none`, the incident tile `none`, the counters `no data`. None of the
recording rules behind them ends in `or vector(0)`: when the controller is
not being scraped, `cluster:vllm_service_state:code` produces **no sample**,
and the one alert that fires on absence — `VllmMonitoringUnknown` — is worded
as *monitoring cannot see*, not as *inference is down*, because that is all it
knows. The same rule governs the vLLM scrape: a gap in the token-rate series
is drawn as a gap (`spanNulls` is off on every vLLM panel), because on
2026-09-11 a bridged line would have hidden the whole outage.

**A stale verdict is absence too.** The controller stamps every snapshot with
`techsara_vllm_generated_at_seconds`. Its tick is 5 s, the scrape 5 s and the
rule group 5 s, so a live verdict is never more than ~15 s old. If the tick
stops (an exception before publish, a blocked observe) the controller's HTTP
thread keeps serving the *last* snapshot with fresh scrape timestamps — the
"process alive, answers stale" blind spot moved one level up. So when the
stamp is more than 30 s old every `vllm-state` rule drops its sample, the tile
turns grey, and `VllmMonitoringUnknown{missing="controller_stale"}` fires a
minute later. A controller that exports no stamp at all is treated the same
way (`missing="controller_stamp"`): an unstamped verdict is unproven, never
READY on trust. Independently, `VllmReadyUnproven` (critical) fires when the
controller still claims READY/BUSY while `cluster:vllm_last_success_age_seconds`
— deliberately *not* gated on freshness — passes 120 s: a READY that has
proven nothing for four canary intervals is a lie, whether the tick is frozen
or the canary thread is dead.

`VllmDown` — `up{job="vllm-main"} == 0`, critical — is now
`VllmScrapeTargetDown`, a warning, silenced while the controller reports
STARTING or RECOVERING. It says exactly what it measures: the scrape target
disappeared. Whether the *model* is down is the controller's call.

### The rules, and how they are proven

`monitoring/prometheus/rules/recording.yml` (group `vllm-state`)
derives five series from the controller's gauges; `alerts.yml` (group
`vllm-availability`, plus `PrometheusRuleEvaluationFailing` in
`monitoring-self`) holds the alerts above. Every alert carries the label
`service` and — where it speaks for one — a **fixed** `state` label (the §2
state the alert is about: `VllmGenerationFrozen` is always `WEDGED`,
`VllmWorkerRankAbsent` always `DOWN`), never the controller's live name. A
label is part of the alert's identity: the first version joined the live
name in with `* on() group_left(state)`, and every READY↔BUSY flip (81 in six
hours under normal load) resolved the pending alert and restarted `for:`, so
the critical alerts for the 2026-09-11 shape could never reach firing. The
live name is now the `state_now` **annotation** (read from the controller's
one-hot; `MONITORING_UNKNOWN` when it is missing or ambiguous), beside
`since`, `reason`, `runbook` (`docs/availability/RUNBOOK.md#<anchor>`) and
`safe_command` — a read-only command, or the runbook's one sanctioned
recovery entry point. Two alerts prove nothing about the state and carry no
`state` label at all: `VllmScrapeTargetDown` and `VllmQueueBacklog`.

Both files are unit-tested with `promtool test rules`
(`monitoring/prometheus/tests/`) — the absent-series, stale-marker,
frozen-stamp and state-churn cases included — using the same pinned image
the stack runs:

```bash
IMG=$(grep -o 'prom/prometheus@sha256:[0-9a-f]*' compose/compose.monitoring.yaml | head -1)
docker run --rm -v "$PWD/monitoring/prometheus:/p:ro" --entrypoint promtool "$IMG" \
  check rules /p/rules/alerts.yml /p/rules/recording.yml
docker run --rm -v "$PWD/monitoring/prometheus:/p:ro" --entrypoint promtool "$IMG" \
  test rules /p/tests/recording_availability.yml /p/tests/alerts_availability.yml
```

(List the test files explicitly: `/p/tests/*.yml` is a path inside the
container, so the host shell cannot expand it, and promtool does not glob.)

**Apply** a change to any of these files with `./scripts/monitoring.sh
restart`. Not `curl -X POST :9090/-/reload`: `prometheus.yml` is a
*single-file* bind mount (`compose/compose.monitoring.yaml`), and a `git
checkout` or an editor that writes-and-renames replaces the file's inode, so
the running container keeps reading the old one — `/-/reload` re-reads the
`rules/` directory mount (a new inode inside a directory is fine) but not a
replaced `prometheus.yml`, and a new scrape job silently never appears.
Grafana re-reads the provisioned dashboards on its own within 30 s.
**Roll back** the same way:
`git checkout <previous-sha> -- monitoring/prometheus monitoring/grafana/dashboards && ./scripts/monitoring.sh restart`.

**There is no Alertmanager.** Every alert above is visible on Prometheus's
`/alerts` page and in Grafana's alert list, and nowhere else: nothing pages
anyone until a receiver is configured. That was true before this work and is
still true — the alerts are now *correct*, not yet *delivered*.

The runbook anchors the alerts link to (`docs/availability/RUNBOOK.md`):
`#wedged`, `#primary-down`, `#ready-unproven`, `#recovery-budget-exhausted`,
`#worker-rank-absent`, `#generation-frozen`, `#repeated-recovery-failure`,
`#monitoring-unknown`, `#metrics-stale`, `#requests-queued`,
`#queue-backlog`, `#canary-ttft-high`, `#head-swap-activity`,
`#nccl-tcp-fallback`, `#roce-rail-missing`, `#scrape-target-down`,
`#recovery-timeline` (the four info notices), `#rule-evaluation-failing` and
`#config-drift`.

Three alerts deserve a note on what they can and cannot see. `NcclTcpFallback`
is **inferred, not reported**: it reads the signature this page measured
under "read the IB counters, never netdev" — RDMA bytes never appear on the
RoCE NICs' netdev counters, so megabytes per second *there* while the IB port
counters idle and real requests run is NCCL on sockets; it is gated on
`requests_running > 0` (not on tokens flowing, which the canary makes always
true) and silenced during STARTING/RECOVERING, because `scripts/cluster-sync.sh`
rsyncs a model over the rail-A address before every start and used to fire it
on every deploy. `RoceRailMissing` is one rail at exactly zero while the
other rail on the same node moves (zero such minutes in seven days of
history). `VllmMetricsStale` has two branches, told apart by its `cause`
label: `prometheus_age` is the contract's `cluster:vllm_metrics_age_seconds >
60`, which can only see Prometheus itself falling behind (a failed scrape
or a vanished family writes a staleness marker and the age series goes
*absent*, so it self-clears at the 5-minute lookback by construction);
`head_metrics_endpoint` is the controller's own `GET /metrics` failing while
its canary succeeds — the hung-handler case, which `VllmScrapeTargetDown`
also reports. `VllmConfigDrift` is **not defined**: nothing exports the
intended configuration to compare the live `vllm:cache_config_info` labels
against; `scripts/cluster-verify-engine.sh` is the drift check.
`PrometheusRuleEvaluationFailing` watches the watchers: a rule that errors at
evaluation time is otherwise silent, and the alert it would have raised
simply never exists.

---

## Dashboards

Provisioned from `monitoring/grafana/dashboards/` — no manual import. Grafana
opens on the Command Center. A bar across the top of every page moves between them.

| Dashboard | For |
|---|---|
| **Command Center** (home) | the always-on screen: one verdict, then the engine and the signals behind it, what needs attention, every Spark, a correlation strip on one time axis, every database |
| **Spark** | everything about one machine: GPU, every core, unified memory, NVMe health, network, RoCE, every thermal sensor, its containers and databases, systemd, kernel faults |
| **GPU** | the GB10 on every Spark: busy, by process, power, memory by owner, thermal, throttling, energy |
| **Fabric** | the RoCE links: ports, per-rail throughput, retransmits and congestion from the NIC itself |
| **Hosts** | the same reading from every Spark on one chart, to spot the one that behaves differently |
| **Containers** | every container on every Spark: state, health, restarts, CPU, memory, swap, GPU memory, network, disk |
| **Databases** | every database on every Spark, found automatically, plus the file-based stores |
| **Postgres** | TechSara's own PostgreSQL in depth: tables, checkpoints, WAL, query timing from the orchestrator |
| **Engine** | vLLM: throughput, TTFT/ITL percentiles, KV cache, preemptions |
| **Alerts** | what is firing, what fired, and availability over 24 hours and 7 days |
| **Developer API**, **Files** | their own subsystems |
| **Monitoring** | fleet membership, every scrape target, what monitoring itself costs, hardware inventory |
| **Old pages** (dropdown beside Home) | the eight pages from before the fleet pages, kept as they were. The old overview, GPU, Interconnect and Host pages are `classic-*.json`, not generated, and name two Sparks |

**Most of these are generated.** `monitoring/grafana/generate_dashboards.py` writes
them, so units, thresholds, colours and layout cannot drift apart, and a test fails
if the committed JSON differs from what it would write. Change the generator and
run it; do not edit the generated files or save from the Grafana UI
(`allowUiUpdates: false` overwrites UI edits from disk).

The rules every panel follows:

* **Colour means something.** Green, amber and red only mark states and limits. A
  number that is neither, such as GPU busy or tokens per second, stays neutral.
* **A Spark keeps its colour.** spark-1 is blue on every chart. The palette is
  checked for colour-vision deficiency against the panel surface.
* **Absent is not zero.** Missing data draws a gap, and a tile says what is missing.
* **GPU busy is not load.** The worker rank spins at about 95 % while drawing about
  34 W as it waits at a collective, so GPU power always sits next to it.

### Answering "why did tokens/sec drop?"

The overview's correlation strip shares one time axis. Read it left to right:

| If this moved | The cause is |
|---|---|
| GPU temperature high / throttle active | **thermal** |
| unified memory near 100 % / swap rising | **memory pressure** |
| KV cache near 100 % / preemptions > 0 | **KV capacity** |
| waiting requests climbing, GPU < 100 % | **concurrency / scheduling** |
| RoCE throughput collapsed | **interconnect** |
| CPU load pinned, GPU idle | **CPU bottleneck** |
| GPU util pinned at ~100 % | **genuinely GPU-bound** |

---

## Databases and data stores

This platform's data lives in two very different places, and both are covered.

### PostgreSQL

`postgres-exporter` connects as the **same user the application uses**, over
the internal `application` network. The password comes from
`${POSTGRES_PASSWORD}` — the identical variable the `postgres` service itself
reads — so no credential is duplicated into a monitoring file or committed.

Collectors enabled: `stat_user_tables`, `statio_user_tables`, `stat_database`,
`database`, `locks`. Replication collectors are off — this is a single instance
and they would only add cardinality. `stat_bgwriter` is off because 0.16.0's
query fails on PostgreSQL 17+, and `long_running_transactions` is off because
its 0.16.0 query is misleading (no `xact_start` filter: it counted background
processes and reported the current time as the "oldest" age).

Custom queries (`monitoring/exporters/postgres/queries.yaml`, mounted through
`PG_EXPORTER_EXTEND_QUERY_PATH`, 2026-09-14) put back what those collectors
lost on PostgreSQL 18, in 37 series:

| Series | Reads |
|---|---|
| `pg_stat_checkpointer_*_total` | timed vs requested checkpoints, write/sync ms, buffers (names identical to exporter 0.20's collector) |
| `pg_stat_wal_{records,fpi,bytes,buffers_full}_total` | WAL generated |
| `pg_stat_io_wal_{writes,write_bytes,fsyncs,write_time,fsync_time}_total{backend}` | WAL writes/fsyncs by client_backend, walwriter, checkpointer, other (times need `track_wal_io_timing`) |
| `pg_client_transactions_{oldest_seconds,idle_in_transaction_oldest_seconds,over_60s}` | open client transactions only |
| `pg_lock_waits_{backends,locks}` | sessions waiting on a lock, ungranted locks (`pg_locks_count` counts granted locks too) |

The file needs PostgreSQL 18: 0.16.0 ignores `runonserver` for user queries,
so on an older major every scrape reports `pg_exporter_last_scrape_error 1`
(alert `PostgresExporterQueriesFailing`). The `postgres` job's
`scrape_timeout` is 10 s, not the global 4 s: the `database` collector's
`pg_database_size()` walk over ~20 databases overran 4 s eleven times on
2026-09-13 while the NVMe was busy.

**Rolling this out needs a container recreate, not only a restart.** The queries
file arrives through a new bind mount, a new environment variable and a changed
`command:`; `docker compose restart` (what `./scripts/monitoring.sh restart`
runs) reuses the existing container definition and applies none of them. Use
`./scripts/monitoring.sh up`, which recreates only the monitoring services whose
definition changed (here postgres-exporter), followed by
`./scripts/monitoring.sh restart` so Prometheus re-binds `prometheus.yml` (the
10 s scrape timeout) and loads `rules/database-timing.yml`; its own compose
definition did not change, so `up` leaves it running on the old file. Then
check, read-only in Prometheus:
`pg_exporter_user_queries_load_error` is 0, `pg_stat_checkpointer_num_timed_total`
exists, and `pg_long_running_transactions` is gone.

What that gives you: per-table live-row counts (conversations, messages,
`web_pages`, `research_runs`…), database size, connections against
`max_connections` (60), cache hit ratio, commits vs rollbacks, dead tuples
(vacuum pressure), deadlocks, checkpoints, WAL, lock waits and transaction age.

### Database timing from the orchestrator

`orchestrator/app/db_metrics.py` times every `db.connection()` transaction at
the pool seam (one edit in `db.connection()`, one in `db.run_in_thread()`):

| Series | Meaning |
|---|---|
| `techsara_db_pool_wait_seconds` | checkout wait, including the pool's liveness-check round trip |
| `techsara_db_thread_wait_seconds` | `db.run_in_thread` queue for an anyio worker thread |
| `techsara_db_transaction_seconds{site}` | checkout to commit/rollback |
| `techsara_db_statement_seconds{site}` | one `execute`/`executemany` round trip; `_count` is statements per site |
| `techsara_db_transaction_errors_total{site}` | transactions rolled back by an exception |
| `techsara_db_pool_{size,available,max,requests_waiting}` | psycopg_pool counters at scrape time |

`site` is a closed vocabulary of about 40 values (`history`, `recall`,
`web_lexical`, `api_key`, `durable_flush`, `files_claim`, `metrics_scrape`, …)
resolved from the function that opened the transaction; an unlisted function
falls back to its module's area, then `other`. Buckets have edges at the
owner's targets (2, 5, 20, 30, 50, 100 ms), so "share of statements under
5 ms" is a bucket ratio. Recording rules and alerts are in
`monitoring/prometheus/rules/database-timing.yml` (tests:
`monitoring/prometheus/tests/database_timing.yml`); the panels are the last row
of the Databases / Data Stores dashboard. Measured cost (paired live A/B on a
PostgreSQL 18 container with the production settings, 2026-09-14): 10-20 µs
median per one-statement transaction, and 44-78 µs median for a transaction of
20 statements (about 2.5-4 µs per statement). Statement durations are recorded
in chunks of 256, so a long per-row loop inside one transaction neither grows
memory nor holds the metrics lock for long at commit.

### The file-based stores

Most of this platform's data is **not** in PostgreSQL and speaks no wire
protocol at all:

| Store | What it is |
|---|---|
| `warehouse_live` | the DuckDB Salesforce warehouse the sync worker writes |
| `warehouse_snapshot` | the atomic read copy the orchestrator actually opens |
| `lancedb_salesforce` | vector index over Salesforce long-text fields |
| `lancedb_web` | vector index over pages the search/crawler stored |
| `parquet_landing` | the sync worker's landing zone |
| `reports`, `workspaces`, `brain` | generated documents, clones, knowledge packs |
| `video`, `voice` | video analyses; stored dictation (V42), which falls to transcripts plus the last day's recordings once the voice archive moves finished audio to the worker |

`data-stores-exporter` walks the volume **read-only** and publishes
`techsara_store_size_bytes`, `techsara_store_files`,
`techsara_store_age_seconds` and `techsara_store_present`. Walks are cached
30 s — a `du` over a 1.6 GB LanceDB directory is not free.

**The number to watch is `warehouse_snapshot` age.** The sync worker
republishes that snapshot every cycle. If the age keeps climbing, every
Salesforce answer is being served from a stale copy *while every container
still reports healthy* — a failure mode nothing else in this stack would
catch. It alerts at six hours.

Measured at the time of writing: 2.49 GB total — LanceDB 1.16 GB, Parquet
597 MB, the two DuckDB files 361 MB each.

### What is deliberately not measured

There is no row-count-per-table for DuckDB or LanceDB. Getting it would mean
opening the warehouse and the vector store from a third process on every
scrape — real query cost against files the sync worker and the orchestrator are
already contending over, to answer a question the PostgreSQL table counts and
the store sizes already answer well enough. File size and freshness are the
cheap, honest signals.

---

## Scrape intervals and overhead

| Job | Interval | Why |
|---|---|---|
| `dgx-gpu`, `vllm-main`, `engine-controller` | **5 s** | the panels that must feel live; the engine state within one canary interval |
| `node` | 10 s | host metrics move slowly |
| `vllm-aux`, `blackbox` | 15 s | |
| `prometheus` (self) | 30 s | |

One GPU scrape costs **63 ms** of `nvidia-smi` (41 ms + 21 ms), about **1.3 %
duty cycle** at 5 s. Measured end to end:

* decode throughput **with** monitoring: **70.1 tok/s**
* decode throughput **before** monitoring existed: **70.0 tok/s**
* whole stack: **~2 % of one CPU core, 212 MB RAM** of 121.7 GB

Cardinality is bounded on purpose. Docker's veths and bridges are not collected (they
were half of everything node-exporter published), per-core governor, softnet and
scheduler series are dropped, 360 unused `pg_settings_*` gauges are dropped, and
Prometheus client `_created` gauges (64 useless series per vLLM scrape) are dropped.
`sample_limit` on the `node` and `dgx-gpu` jobs bounds what any one machine can cost.

Measured on 2026-11-05 replacing cAdvisor (77 MiB, 2.75 % of a core, on the head only):
`spark-probe` holds about 21 MiB and `spark-local` about 13 MiB, on every Spark.
`docker stats` can read over 100 MiB for the probe on the head: that is the kernel's
reclaimable cache from walking database volumes, not the process's own memory.

---

## Storage and retention

Prometheus writes to the `sf-local-ai_prometheus` Docker volume with **two**
limits, whichever is hit first:

```
--storage.tsdb.retention.time=30d      PROMETHEUS_RETENTION_TIME
--storage.tsdb.retention.size=20GB     PROMETHEUS_RETENTION_SIZE
```

The size cap is the backstop that stops Prometheus ever filling the disk (3.3 TB
free at the time of writing). To change either, set the variable in `.env` and
`./scripts/monitoring.sh restart`.

`down` keeps the volume. To discard history deliberately:
`docker volume rm sf-local-ai_prometheus`.

---

## Public access via the Cloudflare Tunnel

Grafana can be served on `grafana.techsarasolutions.com` through the **same**
tunnel that already serves `ai.techsarasolutions.com` — no second tunnel, no
port forward, no new firewall rule. `cloudflared` and `grafana` are both on the
`application` Docker network, so the tunnel reaches `http://grafana:3000`
directly and the loopback binding on 3300 never has to be widened.

**Prometheus is deliberately not routed.** It has no authentication of any
kind; anything that reaches it can read every metric in the cluster. Grafana is
the only thing with a login, so Grafana is the only thing exposed.

Two settings make Grafana behave correctly behind the hostname, both in `.env`:

| Variable | Value | Why |
| --- | --- | --- |
| `GRAFANA_ROOT_URL` | `https://grafana.techsarasolutions.com` | Grafana only knows it listens on `:3000`. Without its external address every absolute URL it builds — share links, alert links, the post-login redirect — points at the container. Blank it for a LAN-only install. |
| `GRAFANA_COOKIE_SECURE` | `false` | Cloudflare terminates TLS, so the browser connection is already HTTPS. Set this `true` **only** once `http://localhost:3300` is no longer used: a secure-only cookie is silently dropped over plain HTTP, and the login then appears to succeed and bounces straight back to the login page.

### It runs on a SECOND tunnel

`grafana.techsarasolutions.com` is served by its own `cloudflared` container
(`cloudflared-grafana`, profile `tunnel-grafana`, token
`CLOUDFLARE_TUNNEL_TOKEN_GRAFANA`) rather than as a second ingress rule on the
tunnel that serves `ai.`.

That is a consequence of where the hostname was registered, not a design
preference: a tunnel is **remotely managed** — it authenticates with its token
and pulls ingress from Cloudflare — so routing can only be changed on the
tunnel it was defined against. Until something ran that tunnel, Cloudflare
answered `530 / error 1033` ("tunnel not available") for the hostname, even
though DNS resolved and Grafana was healthy.

The two are deliberately independent: separate profiles, separate tokens,
separate containers. Stopping either leaves the other serving, and a Grafana
problem cannot take `ai.techsarasolutions.com` down with it.

`scripts/tunnel.sh up|down|status` drives both, and silently ignores the second
whenever `CLOUDFLARE_TUNNEL_TOKEN_GRAFANA` is absent — a single-tunnel install
behaves exactly as before.

To consolidate onto one tunnel later: move the hostname onto the `ai.` tunnel
in the dashboard (Public Hostname → `grafana` / `techsarasolutions.com` →
`HTTP` → `grafana:3000`), then delete the `cloudflared-grafana` service and its
token. Note the service is `grafana:3000` — a Docker network name. `localhost`
inside `cloudflared` means the tunnel container itself and cannot reach
Grafana.

### Protect it with Cloudflare Access

Grafana's login page is a small but real attack surface once it is on the
public internet, and Grafana has no lockout on repeated failures. Put Access in
front of it so the login page is never reached by an unauthenticated request:

> Zero Trust → Access → Applications → Add → Self-hosted
> * Domain `grafana.techsarasolutions.com`
> * Policy: Allow → Emails ending in `@techsarasolutions.com`

This is independent of Grafana's own admin login, which stays in force behind
it.

---

## Security

* **Grafana and Prometheus bind to `127.0.0.1` by default.** They deliberately
  do **not** use `TECHSARA_BIND_ADDRESS` — that variable is `0.0.0.0` on this
  deployment, and this repo has no application login, so inheriting it would
  publish Grafana to the LAN on first boot. Widen with
  `MONITORING_BIND_ADDRESS` only on purpose.
* **Grafana requires a password to start** (`${GRAFANA_ADMIN_PASSWORD:?}`),
  generated into `.runtime/secrets.env` (0600, gitignored). Anonymous access
  and sign-up are off.
* **Grafana's own `/metrics` is off.** It needs no login by default, and this container
  is on a public hostname: until 2026-11-05 anyone could read it through the tunnel.
* **The other Sparks' exporters bind to their management IP only**, never `0.0.0.0` and
  never the RoCE addresses. They are read-only telemetry.
* **`spark-local` is the one holder of the Docker socket, and it has no network.** It
  also holds `CAP_SYS_ADMIN` for the NVMe log and `CAP_DAC_READ_SEARCH` to walk database
  volumes. Nothing else in the stack has any of them.
* **`spark-probe` reads containers' state files and the kernel log** (`SYSLOG`, its only
  capability), read-only, and opens no port. It deliberately lacks `DAC_READ_SEARCH`: under
  Docker's default seccomp profile that also unlocks `open_by_handle_at`, which reads any
  file on the host. It keeps only the environment values it needs from database containers
  and drops the rest while parsing. Passwords survive parsing only for projects in
  `MONITORING_DB_LOGIN_PROJECTS`.
* **Scrapes are bounded.** `sample_limit`, `body_size_limit` and the label limits on the
  `node` and `dgx-gpu` jobs fail a scrape rather than grow the head's memory. None of them
  stops a compromised enrolled Spark sending all-new series on every scrape.
* **node-exporter runs with AppArmor off** so the systemd collector can use D-Bus (the
  default profile refuses it). In exchange it has no capabilities, a read-only root
  filesystem, and runs as `nobody`.
* `ufw` is **installed but disabled** on both nodes (`ENABLED=no`), and
  `iptables`/`nft` cannot be read without root. Exporter exposure is therefore
  controlled by bind address, not by a firewall. If you add one, allow
  9100/9835 from Spark 1 only.
* No credentials appear in any committed file.

---

## Troubleshooting

**Check targets first — it answers most questions:**

```bash
./scripts/monitoring.sh status     # every target, UP/DOWN, with the error
open http://127.0.0.1:9090/targets
```

### No GPU metrics

```bash
docker logs sf-local-ai-dgx-gpu-exporter-1 | tail -20
curl -s http://127.0.0.1:9835/metrics | head       # Spark 1, from the host
nvidia-smi --query-gpu=temperature.gpu --format=csv # does the driver answer?
```

`dgx_gpu_up 0` means the exporter is alive but `nvidia-smi` is failing — that
is a driver/runtime problem, not a monitoring one. If **per-process GPU memory**
is missing but everything else works, the container lost `pid: host`.

### No Spark 2 metrics

```bash
ssh <worker> docker ps                       # are the two exporters running?
curl -s http://192.168.9.68:9835/metrics | head -3
curl -s http://192.168.9.68:9100/metrics | head -3
./scripts/monitoring.sh up                   # re-syncs and restarts them
```

Common causes: the worker exporters bound to loopback instead of the
management IP (set `MONITORING_WORKER_BIND`), or the management LAN is down
while RoCE is fine — the cluster keeps serving and only monitoring breaks.

### The engine tile says MONITORING_UNKNOWN

That is the telemetry, not the model. Either the controller is not being
scraped, or it is scraped but its verdict is stale — `VllmMonitoringUnknown`'s
`missing` label says which:

```bash
curl -s http://127.0.0.1:9838/healthz                 # controller liveness
curl -s http://127.0.0.1:9838/state | jq '.state,.generated_at,.reason'
date +%s                                              # generated_at more than 30 s behind this = a frozen tick
docker ps --filter name=engine-controller
docker logs --tail 50 sf-local-ai-engine-controller-1  # "tick failed" lines = the exception before publish
```

If the controller answers here with a current `generated_at` but the tile
stays grey, it is the scrape: `./scripts/monitoring.sh status` shows the
`engine-controller` target and its last error. If `generated_at` is old, the
controller's HTTP thread is serving a snapshot its tick no longer refreshes;
restarting the *controller* container (never the engine) is the fix, and the
log says why it stopped. While the tile is grey nothing on the page vouches
for the engine either way — the honest next step is a real completion
(`scripts/cluster-verify-engine.sh --probe`), not an engine restart.

### No vLLM metrics

```bash
curl -s http://127.0.0.1:8000/metrics | grep -c '^# HELP vllm:'   # expect ~76
```

Zero means the model is not serving. Remember there is **no** second endpoint
on Spark 2 to check — one endpoint covers the whole engine.

### RoCE panels flat at zero

Expected when idle. During a generation they must move. If they stay zero:

```bash
cat /sys/class/infiniband/rocep1s0f1/ports/1/counters/port_xmit_data   # twice
curl -s http://127.0.0.1:9100/metrics | grep -c node_infiniband        # expect ~100
```

If the IB metric count is 0, the infiniband collector was disabled — it must
stay enabled. **Do not** "fix" this by switching the panels to netdev; netdev
cannot see RDMA at all.

### No database metrics

```bash
docker logs sf-local-ai-postgres-exporter-1 | tail -20
curl -s http://127.0.0.1:9090/api/v1/query?query=pg_up
```

`pg_up 0` means the exporter is running but cannot authenticate or reach
PostgreSQL — check `POSTGRES_PASSWORD` is set in `.env`. A restart loop with
`unknown long flag` means a collector flag does not exist in this exporter
version; `docker run --rm <image> --help` lists the real ones.

If **store** metrics are missing, check the volume mounts:

```bash
docker exec sf-local-ai-data-stores-exporter-1 ls -la /data
```

### Container or database panels empty

The **Monitoring** page's Spark probe row says which collector failed. Then, on that
Spark:

```bash
docker logs --tail 20 sf-local-ai-spark-probe-1      # on a worker: sf-local-ai-monitoring-spark-probe-1
docker exec sf-local-ai-node-exporter-1 ls -la /textfile   # the files should be seconds old
curl -s http://127.0.0.1:9090/api/v1/query?query=node_textfile_scrape_error
```

A Spark reporting host metrics with no probe data raises `SparkProbeMissing`.
`./scripts/monitoring.sh fleet enroll spark-N` repairs it.

### A new Spark does not appear

```bash
./scripts/monitoring.sh fleet                  # its state, and the fix if it needs one
systemctl --user status techsara-fleet.timer   # is discovery running?
avahi-browse -rt _ssh._tcp | grep spark        # can this head see it?
tail .runtime/logs/fleet.log
```

---

## Containers, databases and the kernel: `spark-probe`

`monitoring/exporters/spark-probe/spark_probe.py`, on every Spark, writing one file
every 15 s that node-exporter's textfile collector publishes. Standard library
only. One cycle measured 59 ms on the worker and 84 ms on the head.

**Why it opens no port.** It reads every container's configuration, so it should
not also answer the network. With no listener it needs no host-guard change and
nothing on the LAN can talk to it. It uses host networking and the host PID
namespace to read: a database on any Compose network can be dialled, and `/proc`
supplies per-container traffic and PostgreSQL's process titles.

**Why not the Docker socket.** A read-only bind of `docker.sock` is not read-only:
it still lets anything inside create a privileged container (audit F057). Docker's
state files give names, labels, state and restarts with no write path to the daemon.
They do not hold a true health verdict: `config.v2.json` still said `starting` days
after a container went healthy (measured 2026-11-05). That one reading comes from
`spark-local`.

### Databases are detected, not configured

Any running container whose image is a known engine is a database, and so is an
engine installed directly on the host: PostgreSQL (with pgvector, TimescaleDB and
others), Redis, Valkey, MySQL, MariaDB, MongoDB, ClickHouse, Elasticsearch,
OpenSearch, Qdrant, Milvus, Weaviate, Chroma, InfluxDB, Neo4j, Cassandra, CouchDB,
etcd, MinIO and Memcached. Exporters and admin tools named after one are not.

| What | How | Login? |
|---|---|---|
| Up or down | a handshake in the engine's own protocol: an SSL request to PostgreSQL, PING to Redis, MySQL's greeting, an HTTP health path | none |
| Version | the image's environment | none |
| Connections by state | PostgreSQL's process titles (`postgres: user db host state`), read from `/proc` | none |
| CPU, memory, swap, disk I/O, network | the container's cgroup | none |
| Size on disk | blocks in its named volumes, walked every 5 minutes with a time limit, by `spark-local` | none |
| Redis statistics | `INFO`, when the server has no password | none |
| Transactions, cache hit, rows, deadlocks, temp files, WAL, replication, per-database size | read-only queries on the `postgres` maintenance database | **yes** |
| Redis statistics with a password | `AUTH` then `INFO` | **yes** |

**The login is opt-in per project.** `MONITORING_DB_LOGIN_PROJECTS` lists the Compose
projects the probe may log in to, using the password in that container's own
environment. The password never crosses the wire: only SCRAM is spoken, cleartext and md5
are refused, and the server must prove it knew the password too. A database that offers
nothing else reads as a statistics error. The default is `sf-local-ai`, this platform's own stack. For any other
project the password is discarded while the container's configuration is parsed, so
it is never held in memory, and no credential, environment value or query text is
ever logged or exported. Set it to `*` in `.env` and run `./scripts/monitoring.sh up`
for full statistics everywhere; the Databases page's **Detail** column says what is
known about each one.

The session is on the `postgres` database so it can never block a `DROP DATABASE`
or a `CREATE DATABASE ... TEMPLATE` of an application or test database. It is
read-only with a short statement timeout, and it shows as `spark-probe` in
`pg_stat_activity`.

**A database is someone else's program.** Every conversation has one deadline and a 1 MiB
budget, SCRAM's iteration count is capped, and one that misbehaves shows as down without
costing the others their row.

A stopped database nobody expects to run, such as an old test container, is a
**leftover**: counted, and not an outage. One whose restart policy says it should be
up and that nobody stopped by hand is **down**.

**File-based stores** have no wire protocol. `data-stores-exporter` measures them from
disk. Anything at the top of the data volume that its list does not name appears by
itself as `undeclared/<name>`, so a new feature's directory cannot go unnoticed. On
the day this was added that found `api-files`, `chat-files`, `chat-media` and `traces`,
plus a corrupt index backup and an old warehouse copy nobody had cleaned up.

### What else it reads

| Series | From | Why |
|---|---|---|
| `spark_kernel_events_total{kind}`, `spark_gpu_xid_total{xid}` | `/dev/kmsg` | GPU Xid errors, OOM kills, PCIe AER, NVMe and filesystem errors, hung tasks and lockups. These kill a long-running model and no metric reports them |
| `spark_roce_hw_counter_total{counter}` | `/sys/class/infiniband/.../hw_counters` | retransmits, congestion notifications and sequence errors, from the NIC. They rise before throughput falls, and node-exporter does not read them |
| `spark_container_gpu_memory_bytes` | GPU exporter + each PID's cgroup | nvidia-smi only says "PID 10573 holds 25 GiB"; this says which service |
| `spark_process_*{comm}` | `/proc` | top processes by CPU and memory, grouped by name so the label set stays small |

## NVMe health, Docker health and database sizes: `spark-local`

Reading a drive's health log needs `CAP_SYS_ADMIN`, Docker's health verdicts need the
socket, and walking database volumes needs `CAP_DAC_READ_SEARCH`. Each is as good as root,
so `spark-local` holds them and has **no network interface at all**: nothing can reach it,
and the files it writes are all it can affect. It only sends GET requests. cAdvisor, which
this replaces, held the same socket while serving HTTP.

`spark-probe` tells it which volumes to measure, and it trusts nothing from that list:
names must match a closed pattern and the walk stays inside the volumes mount.

`spark_nvme_*` is the drive's own record: wear, spare capacity, media errors, bytes
written, and `unsafe_shutdowns_total`, the only durable evidence of a power cut.

---

## Adding a Spark

Connect it to the management LAN and turn it on. Every DGX Spark advertises
`spark-XXXX SSH` over mDNS out of the box, so the head sees it within a minute, with no
port scan and no address list. Then:

1. **If this head's SSH key is accepted**, its exporters are copied over and started,
   and on the next pass it joins Prometheus. Nothing else to do.
2. **If not**, it shows on the **Monitoring** page as `needs_ssh_key`, and in
   `./scripts/monitoring.sh fleet`. Run the one command it prints, and step 1 happens
   by itself:
   ```bash
   ssh-copy-id techsphere@<its address>
   ```
3. **Its packet filter still needs a person.** Installing it takes that machine's sudo
   password, so enrolment cannot do it, and until then its exporters are readable from
   the office LAN. `HostGuardNotInstalled` says so. `scripts/host-guard.sh` is copied to
   `~/.techsara-cluster/` on enrolment, but its defaults are the two current Sparks'
   addresses and the boot unit does not keep overrides yet, so this script needs
   extending before a third Spark runs unattended.

No scrape config, dashboard or rule changes: every panel is built from the `node` label.

To add one mDNS cannot see (another subnet, a renamed host):
`./scripts/monitoring.sh fleet add techsphere@192.168.x.y`.
To retire one: `./scripts/monitoring.sh fleet forget spark-3`.

Three properties worth knowing:

* **A Spark never silently disappears.** Once scraped it stays a target until someone
  runs `forget`. A machine that dies reads as DOWN; it does not vanish from every panel.
* **Names are stable.** `spark-N` is handed out once and tied to the hostname, so a
  DHCP move (2026-11-01, worker 9.68 to 9.3) updates one field, re-binds the exporters
  and keeps the history. The two existing Sparks keep `spark-1` and `spark-2`.
* **mDNS is a hint, never an authority.** Anyone on the office LAN can advertise any
  name, so nothing mDNS says, and no port that answers, makes a machine a target or moves
  an existing one. A machine becomes a target only after SSH proves it: this head's key is
  accepted, a throwaway key is refused, it has `/etc/dgx-release` and a GPU, and its own
  hostname matches. Its host key is then pinned in `.runtime/fleet/known_hosts` under that
  hostname. An enrolled Spark's address only changes when the pinned machine itself says
  it holds the new address, so advertising `spark-476e` from elsewhere changes nothing.
* **What is left.** An attacker on the LAN who tells the two keys apart can still appear as
  a *new* Spark. They get exporter code and a network map, which hold no secret, at a cost
  to Prometheus bounded by the job limits. They cannot impersonate an enrolled Spark. For a
  closed fleet set `MONITORING_FLEET_AUTO_ENROLL=false` and use `fleet add`.

A new Spark being monitored is not the same as it serving the model: the engine stays
TP=2 on the two Sparks it was started with until `CLUSTER_*` says otherwise.

---

## My files (orchestrator `/metrics`, 2026-09-30)

| metric | labels | what it answers |
|---|---|---|
| `myfiles_list_seconds` | view = list / summary | time to answer one `/files/mine` or `/files/mine/summary` request |
| `myfiles_list_total` | view, result = ok / bad_request / error | how often the page is used, and whether it fails |

When the `view="list"` p95 passes 50 ms, add the `uploads.user_id` index
described in [`MY-FILES.md`](MY-FILES.md#cost). Labels never carry a file
name, a search or a user.

---

## Chat media (orchestrator `/metrics`, 2026-10-02)

Pictures sent in a chat are stored under `CHAT_MEDIA_DIR` for the life of the
chat (V44, [`chat-media/README.md`](chat-media/README.md)). All closed label
sets (`metrics._LABELS_BY_METRIC`); no label carries a user, a chat, an
attachment id or a file name.

| metric | labels | what it answers |
|---|---|---|
| `chat_media_writes_total` | source = chat / upload / backfill, result = stored / duplicate / unsupported / too_large / no_space / error / unlinked | pictures written, and why some were not (`chat` is `/chat`'s background store, which never fails a turn; `unlinked` is a `/chat` picture with no ids and no send intent to name it by, so not stored) |
| `chat_media_write_seconds` | source | verify + durable write of one picture |
| `chat_media_reads_total` | size = thumb / full, result = ok / not_modified / not_found / missing | byte reads, the admin route included; `missing` is a 410, a row whose file is gone |
| `chat_media_erase_total` | store = media / files, result = ok / error | bytes removed at once when a chat is deleted; an error is finished by the reaper |
| `chat_media_reaped_total` | kind = row / dir | orphans the reaper removed (past `CHAT_MEDIA_ORPHAN_GRACE_H`) |
| `chat_files_lasting_total` | purpose = document / dataset, result = stored / no_space / error | lasting copies of document and dataset originals under `CHAT_FILES_DIR` (CONTRACT §9) |

Worth an alert: any `chat_media_reads_total{result="missing"}` (a file
vanished under a live row); `chat_media_writes_total{result=~"no_space|error"}`
rising; `chat_media_erase_total{result="error"}`. `/data/chat-media` is on the
head's root NVMe and is not in `scripts/backup-knowledge.sh`.

---

## Knowledge pipeline metrics (orchestrator `/metrics`, ADR-0001 D12)

All emitted by the orchestrator's own registry (`app/metrics.py`); labels in
braces. Histograms use the registry's fixed buckets.

| metric | labels | what it answers |
|---|---|---|
| `chat_route_total` | route, effort | route mix — which engine answered |
| `chat_ttft_seconds`, `chat_total_seconds` | route, effort | orchestrator-side time to first token / total (vLLM's TTFT excludes every pre-pass) |
| `knowledge_stage_seconds` | stage = embed, dense_scan, lexical, meta, rerank | where retrieval time goes |
| `knowledge_decision_total` | decision, freshness | how questions were served: local, fast_lookup, escalate_search, stale_offline, degraded_busy, static_topical, static_model |
| `knowledge_verdict_total` | verdict = sufficient / stale / insufficient, freshness | the sufficiency decision itself |
| `knowledge_escalation_total` | effort, stage = local_first / search | auto searches cancelled by the store; Think escalations |
| `knowledge_degraded_total` | reason = rerank_busy, rerank_error, rerank_canary, rerank_degenerate, embed_busy, embed_error, prepare_timeout | every time the judge or the embedder was missing |
| `knowledge_evidence_cache_total` | outcome = hit / miss | evidence cache effectiveness |
| `rerank_requests_total` | outcome = ok / busy / error / degenerate / canary_failed / disabled, kind = local / bulk | reranker health per caller class |
| `rerank_seconds`, `rerank_queue_seconds`, `rerank_inflight` | kind, n | reranker latency, queueing, concurrency |
| `rerank_canary_ok`, `rerank_canary_margin` | — | 0 when the breaker is tripped; the positive−negative margin of the canary triple |
| `embed_requests_total`, `embed_seconds`, `embed_queue_seconds`, `embed_batch_size` | outcome, kind = query / index / batch | embedding sidecar pressure |
| `freshness_router_seconds` | outcome = ok / timeout / error | the 8B classifier's cost on the Fast path |

Alerting suggestions: `rerank_canary_ok == 0` for 10 min; rate of
`knowledge_degraded_total` > 5% of `chat_route_total`; `chat_ttft_seconds`
p95 for route=chat, effort=fast above 3 s.

## The voice archive (orchestrator `/metrics`, 2026-09-30)

Stored recordings' audio moves to a store on the worker
([`voice-archive.md`](voice-archive.md)). **There is no scrape job for the
store**: the orchestrator's mover relays the store's `/health` as
`voice_archive_store_*`, so Prometheus needs neither the worker's port nor its
pinned certificate. The series exist only in a process that has run a pass
(`VOICE_ARCHIVE_ENABLED` and the store configured); with the archive off the
rules in `rules/voice-archive.yml` are inert.

| metric | labels | what it answers |
|---|---|---|
| `voice_archive_backlog_sessions`, `voice_archive_backlog_bytes`, `voice_archive_overdue_sessions`, `voice_archive_overdue_oldest_seconds` | — | finished recordings still only on the head, and how late the oldest is |
| `voice_archive_copied_sessions`, `voice_archive_archived_sessions`, `voice_archive_archived_bytes` | — | where the audio is |
| `voice_archive_purge_pending`, `voice_archive_remote_missing` | — | deleted recordings still on the store (a privacy lag); archived recordings the store lost |
| `voice_archive_store_up`, `voice_archive_store_free_bytes`, `voice_archive_store_min_free_bytes`, `voice_archive_store_objects`, `voice_archive_store_bytes`, `voice_archive_store_scrub_mismatches` | — | the store's own `/health` |
| `voice_archive_errors_total` | reason (16 closed values) | why a copy or a store call failed |
| `voice_archive_proxy_total` | result = ok / partial / not_satisfiable / bad_request / busy / unavailable / missing | playback of archived recordings |
| `voice_archive_restored_total` | result = restored / held / unavailable / missing / mismatch / no_space / deleted | recordings brought back for a retranscription or a continuation |
| `voice_archive_reconcile_total` | result = orphan_quarantined / orphan_waiting / other_owner / unowned / deleted_row_purged / repaired / remote_missing / foreign | the daily reconcile; it never deletes a recording because a row is missing (docs/voice-archive.md, "Who owns a recording") |
| `voice_archive_copied_total`, `voice_archive_released_total`, `voice_archive_purged_total`, `voice_archive_last_pass_timestamp_seconds`, `voice_archive_enabled` | — | the mover's progress |

Rules (no mail): `VoiceArchiveStoreDown`, `VoiceArchiveBacklogOverdue`,
`VoiceArchiveCopiesMissing`, `VoiceArchivePurgeStuck`,
`VoiceArchiveStoreLowSpace`, `VoiceArchiveScrubMismatch`,
`VoiceArchivePassStalled`; promtool tests in `tests/voice_archive.yml`.

---

## Files

```
monitoring/
  prometheus/prometheus.yml            scrape config + topology labels
  prometheus/rules/recording.yml       cluster aggregates
  prometheus/rules/alerts.yml          alert rules
  prometheus/rules/fleet.yml           fleet recording rules and alerts, for any number of Sparks
  prometheus/tests/*.yml               promtool unit tests
  grafana/generate_dashboards.py       writes the fleet dashboards; edit this, not their JSON
  grafana/provisioning/datasources/    Prometheus datasource (uid dgx-prometheus)
  grafana/provisioning/dashboards/     dashboard provider
  grafana/dashboards/*.json            every dashboard
  blackbox/blackbox.yml                HTTP probe module
  exporters/dgx-gpu/dgx_gpu_exporter.py   the GB10 GPU exporter
  exporters/spark-probe/spark_probe.py containers, databases, kernel faults, RoCE counters
  exporters/spark-probe/spark_local.py NVMe health, Docker health and database sizes
  exporters/data-stores/data_stores_exporter.py  DuckDB/LanceDB/Parquet sizes
  engine-controller/                   the controller (head) and sentinel (worker); docs/availability/
compose/compose.monitoring.yaml        Spark 1 stack (profile: monitoring)
compose/compose.monitoring-worker.yaml every other Spark's exporters (own project)
scripts/monitoring.sh                  up/down/status/logs/verify/url/fleet
scripts/lib/fleet.py                   discovers Sparks, enrols them, writes Prometheus' targets
.runtime/fleet/                        the registry (generated, never committed)
.runtime/prometheus/sparks-*.json      Prometheus' target files (generated)
```

---

## Known limitations

* **No whole-machine power.** GPU rail only — the hardware exposes nothing else.
* **No GPU memory capacity.** No framebuffer exists; use unified memory.
* **No SM/tensor-core activity, no PCIe counters, no ECC, no fan speed** — the
  DCGM profiling module will not load on GB10 and the driver reports the rest
  as `N/A`.
* **Temperature is a shared SoC reading**, moved by CPU load as well as GPU.
* **CPU temperature is not attributable**: the seven `acpitz` zones are
  unlabelled and all report the same 104.8 °C critical trip. Named sensors that
  *are* trustworthy: NVMe composite and the four mlx5 ASICs.
* **No per-rank vLLM metrics** — one engine, one endpoint, by design.
* **Database logins are off for other projects by default.** Their detailed statistics
  are blank until `MONITORING_DB_LOGIN_PROJECTS` allows them; see above.
* **Data size is only measured for named volumes.** A database on a bind mount outside
  the Docker root has no `spark_db_data_bytes`.
* **SSD powered-on time runs behind the calendar.** The NVMe specification lets the
  drive leave out time in low-power states, so it read 252 hours on a year-old machine.
* **No DuckDB/LanceDB row counts** — only size, file count and freshness. See
  the reasoning above.
* **NCCL transport and rail health are inferred**, from netdev-vs-IB counter
  shapes, not reported by NCCL; `VllmConfigDrift` has no signal at all (see
  "Engine state").
* **Alerts are local**, visible in Prometheus and Grafana. No paging
  integration, by choice.
