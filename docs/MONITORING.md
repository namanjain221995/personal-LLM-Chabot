# Monitoring — Grafana + Prometheus for the two-node DGX Spark cluster

One browser tab that answers, live: *are both Sparks alive, how hot are they,
how much power are they drawing together, is the distributed model actually
distributed, how many requests are running, how many tokens/sec, and if
throughput drops — why?*

Everything runs locally. No Grafana Cloud, no SaaS, no paid API, no data
leaving the machines.

---

## Quick start

```bash
./scripts/monitoring.sh up        # start here + the two exporters on Spark 2
./scripts/monitoring.sh status    # containers + every Prometheus target
./scripts/monitoring.sh verify    # prove BOTH Sparks are reporting
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
                         Prometheus :9090
                       /       |        \
          node-exporter   dgx-gpu-      cadvisor + blackbox
             :9100        exporter          (containers,
                           :9835           app liveness)
                               |
                               |  management LAN 192.168.9.0/22
                               v
          node-exporter :9100     dgx-gpu-exporter :9835
                                                        Spark 2  "spark-2"  worker
                                                        192.168.9.68

          vLLM :8000 /metrics  <- ONE endpoint, whole distributed engine
          vLLM :8002/:8003/:8004/:8005  router / embed / OCR / reranker
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
| node-exporter 1.9.1 | **both** | CPU, unified memory, disk, **RoCE/IB counters** |
| `dgx-gpu-exporter` (custom) | **both** | GPU telemetry from `nvidia-smi` |
| cAdvisor 0.52.1 | Spark 1 | per-container CPU/memory/restarts |
| blackbox-exporter 0.25.0 | Spark 1 | HTTP liveness for services with no `/metrics` |
| postgres-exporter 0.16.0 | Spark 1 | PostgreSQL server statistics |
| `data-stores-exporter` (custom) | Spark 1 | DuckDB / LanceDB / Parquet size + freshness |

Loki/Promtail are deliberately **not** included: this is metric observability,
and log aggregation would be a second storage system to run and bound.

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

## Dashboards

Provisioned from `monitoring/grafana/dashboards/` — no manual import. Grafana
opens on the overview by default.

| Dashboard | For |
|---|---|
| **DGX Spark AI Cluster — Live Overview** | the always-on screen: status cards, combined power, max temp, tokens/sec, and a correlation strip on one time axis |
| **GPU / Thermal / Power** | per-Spark GPU detail, combined power and energy, throttling |
| **vLLM / LLM Performance** | throughput, TTFT/ITL percentiles, KV cache, prefix cache, preemptions |
| **Interconnect / NCCL / RoCE** | per-rail RX/TX, port state, IB errors, management LAN shown separately |
| **Host / Containers** | CPU, unified memory, swap, disk, per-container CPU/memory/restarts |
| **Databases / Data Stores** | PostgreSQL statistics, plus size and freshness of every file-based store |

GPU, and Host dashboards have a **Spark** variable (`spark-1`, `spark-2`, All).

Dashboards are files in git, not Grafana database rows: `allowUiUpdates: false`
means UI edits are overwritten from disk. To change one permanently, use
*Save JSON to file* in Grafana and commit the result.

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
`database`, `locks`, `long_running_transactions`. Replication collectors are
off — this is a single instance and they would only add cardinality.

What that gives you: per-table live-row counts (conversations, messages,
`web_pages`, `research_runs`…), database size, connections against the default
`max_connections` of 100, cache hit ratio, commits vs rollbacks, dead tuples
(vacuum pressure), deadlocks, and long-running transactions.

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
| `dgx-gpu`, `vllm-main` | **5 s** | the panels that must feel live |
| `node` | 10 s | host metrics move slowly |
| `cadvisor`, `vllm-aux`, `blackbox` | 15 s | |
| `prometheus` (self) | 30 s | |

One GPU scrape costs **63 ms** of `nvidia-smi` (41 ms + 21 ms), about **1.3 %
duty cycle** at 5 s. Measured end to end:

* decode throughput **with** monitoring: **70.1 tok/s**
* decode throughput **before** monitoring existed: **70.0 tok/s**
* whole stack: **~2 % of one CPU core, 212 MB RAM** of 121.7 GB

Cardinality is bounded on purpose: cAdvisor is filtered to ten whole-container
metric families with `id`/`image`/`container_label_*` dropped, and Prometheus
client `_created` gauges (64 useless series per vLLM scrape) are dropped.

---

## Storage and retention

Prometheus writes to the `sf-local-ai_prometheus` Docker volume with **two**
limits, whichever is hit first:

```
--storage.tsdb.retention.time=15d      PROMETHEUS_RETENTION_TIME
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
* **Spark 2's exporters bind to its management IP only** (192.168.9.68), never
  `0.0.0.0` and never the RoCE addresses. They are read-only telemetry.
* **cAdvisor gets the Docker socket read-only** and nothing else — no
  `privileged`. This is the one privilege in the stack, and it is what supplies
  container *names*; without it every metric is the root cgroup. No other
  service receives it.
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

### Container panels empty

cAdvisor must be **≥ v0.52**. v0.49 speaks Docker API 1.41 while this daemon
requires ≥ 1.44, so its Docker factory silently fails to register and every
metric comes back as `id="/"` with no name. Check:

```bash
docker logs sf-local-ai-cadvisor-1 | grep "docker container factory"
```

---

## Adding a third Spark

1. Copy `compose/compose.monitoring-worker.yaml` and the exporter to it (the
   `up` command does this for the existing worker automatically).
2. Start them there with `MONITORING_WORKER_BIND=<its management IP>`.
3. Add two target entries to `monitoring/prometheus/prometheus.yml`:

```yaml
      - targets: ["192.168.9.NN:9835"]
        labels: { node: spark-3, role: worker }
```

4. `curl -X POST http://127.0.0.1:9090/-/reload`

Nothing else changes: the exporters are identical everywhere, and every
dashboard aggregates by label rather than by a hardcoded node list.

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

---

## Files

```
monitoring/
  prometheus/prometheus.yml            scrape config + topology labels
  prometheus/rules/recording.yml       cluster aggregates
  prometheus/rules/alerts.yml          alert rules
  grafana/provisioning/datasources/    Prometheus datasource (uid dgx-prometheus)
  grafana/provisioning/dashboards/     dashboard provider
  grafana/dashboards/*.json            the five dashboards
  blackbox/blackbox.yml                HTTP probe module
  exporters/dgx-gpu/dgx_gpu_exporter.py   the GB10 GPU exporter
  exporters/data-stores/data_stores_exporter.py  DuckDB/LanceDB/Parquet sizes
compose/compose.monitoring.yaml        Spark 1 stack (profile: monitoring)
compose/compose.monitoring-worker.yaml Spark 2 exporters (own project)
scripts/monitoring.sh                  up/down/status/logs/verify/url
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
* **Container metrics are Spark 1 only**; Spark 2 runs only the vLLM worker and
  its two exporters.
* **No DuckDB/LanceDB row counts** — only size, file count and freshness. See
  the reasoning above.
* **Alerts are local**, visible in Prometheus and Grafana. No paging
  integration, by choice.
