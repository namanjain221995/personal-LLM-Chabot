# Engine availability — the documents (2026-09-12, v2: strict one-model mode)

What was written after the main model's 11-minute outage of 2026-09-11 22:15Z,
and the order to read it in. The subject is one vLLM engine, TP=2 across two
DGX Sparks, and how it is detected dead, brought back, and how the requests
that arrive while it reloads are kept and resumed — by the same model, because
only `nvidia/Qwen3.6-35B-A3B-NVFP4` ever answers a person. The wider cluster
(topology, interconnect, benchmarks, `CLUSTER_*` configuration) is
[`../CLUSTER.md`](../CLUSTER.md); the observability platform is
[`../MONITORING.md`](../MONITORING.md).

| Read | When you need | It is |
|---|---|---|
| [`RUNBOOK.md`](RUNBOOK.md) | **something is wrong now** | the operator's procedures: status, real probe, both-GPU proof, logs and evidence, Xid / memory / RDMA checks, the coordinated restart, queued requests and the exactly-once checks, who may restart what, kernel caches, the candidate image switch, CPU topology, rollback; one section per alert anchor; boot and host recovery; deployment without avoidable outage |
| [`CONTRACT.md`](CONTRACT.md) | the exact name of anything | **binding** (v2): the nine states, the twenty signals, the failure categories, the readiness sequence, the controller's HTTP/JSON, kernel caches, admission lanes, every metric name, the alerts, the orchestrator's breaker and continuity rules, file ownership, testing rules |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | to understand why it is built this way | the TP=2 primary and its single-replica limit, the process model and what each endpoint proves, the controller and sentinel as the single recovery authority, the orchestrator's breaker, continuity and admission lanes, request durability and the resume sweep, monitoring semantics |
| [`INCIDENT-2026-09-11-vllm.md`](INCIDENT-2026-09-11-vllm.md) | what happened | the timeline to the second, the five independent delays that made a sub-second fault an 11-minute outage, the hypothesis matrix, the corrective actions and the Candidate B plan |
| [`ADR-0002-high-availability.md`](ADR-0002-high-availability.md) | why this option | options A–E compared; Option A hardened + request continuity chosen; the stand-in answer model (D) rejected by product requirement; Option C (+2 Sparks) named as the only way to serve answers during a reload; the follow-ups |
| [`SLO.md`](SLO.md) | the numbers | four SLOs with their PromQL/SQL (B = request continuity availability), the detection/recovery objectives measured before and targeted after, why 99.99 % is out of reach on one replica, the acceptance checklist |
| [`CANDIDATE-B.md`](CANDIDATE-B.md) | the engine candidate | the post-`f6326f5` build with `--gdn-prefill-backend flashinfer`, evaluated first; the secondary engine knobs one at a time (Track A `flashinfer_b12x` fifth); the A/B harness and its records |
| [`MEMORY-BUDGET.md`](MEMORY-BUDGET.md) | what fits on which Spark | the unified-memory map of both nodes, the placement verdicts, the limits in force and what happens past each |
| [`VLLM-UPGRADE-RESEARCH.md`](VLLM-UPGRADE-RESEARCH.md) | whether a newer vLLM fixes it | it does not: the fault class is open on every released build; what the candidates are, the validation plan, the rollback insurance |
| [`REVIEW-MANIFEST.md`](REVIEW-MANIFEST.md) | what was reviewed, file by file | the read-only review of the code production was running during the incident, findings tagged by owning workstream |
| [`REVIEW-FINDINGS-round1.md`](REVIEW-FINDINGS-round1.md) | what the v1 code got wrong | 12 adversarial reviews of the first build round (blockers, majors, minors) and the phase-2 durability report; consumed by the v2 round |

## The system in one paragraph

`monitoring/engine-controller/controller.py` (compose service
`engine-controller`, port 9838 on the head) is the single recovery authority.
It proves the engine with the readiness sequence after every start — a
non-streaming completion, a streaming completion, token-counter progress,
both GPUs observed working, the worker rank alive — runs a real streamed
completion every 30 s between, watches the worker rank through `sentinel.py`
(service `vllm-worker-sentinel`, port 9839 on the worker's RoCE address; a
sensor that restarts the worker only when the controller says so), and
publishes one of nine states at `GET /state` and as `techsara_vllm_*` metrics.
When a rank dies it captures diagnostics, restarts the worker and then the
head under one host `flock` (`scripts/lib/engine-lock.sh`) with a budget of
three recoveries an hour, and calls the pair READY only when the sequence
passes; compiled kernels are reused from persistent cache volumes. The
orchestrator (`orchestrator/app/{engine_state,breaker,continuity,admission}.py`)
opens a circuit breaker on observed failures or on the controller's verdict,
**accepts and durably queues** every request that arrives meanwhile with the
one line `Main model is recovering—your request is safely queued.`, and
resumes the same generation exactly once when READY returns; long prompts are
admitted one at a time onto an idle engine. No other model answers. Prometheus
derives `cluster:vllm_service_state:code` (state 7 = `QUEUEING`), never renders
a missing or stale sample as DOWN, and the alerts link to the runbook's
anchors. Nothing pages anyone yet: there is no Alertmanager.

## Where the code lives

| area | files |
|---|---|
| controller + sentinel | `monitoring/engine-controller/{controller.py,sentinel.py,common.py,README.md,tests/}` |
| compose | `compose/compose.dgx-spark.yaml` (engine-controller, `ulimits: core`, the kernel-cache volume), `compose/compose.cluster-dgx-spark.yaml` (head API URL, sentinel URL, token, healthcheck kill tier), `compose/compose.cluster-worker.yaml` (sentinel service, `ulimits: core`, the kernel-cache volume), `compose/compose.ocr.yaml` |
| launcher | `launcher/techsara_cli/cli.py` (the engine lock around the pair; controller started after the head is proven; the sentinel shipped on every `up`; legacy watchdog retired), `launcher/techsara_cli/environment.py` (`ENGINE_HEAD_API_URL`, `ENGINE_CONTROLLER_URL` and the router-classifier health URL, all generated from the real bind addresses) |
| scripts | `scripts/cluster-recover.sh` (`--force`, `--clear-kernel-cache`), `scripts/lib/engine-lock.sh`, `scripts/recovery-tests/engine_failure_drills.sh`, `scripts/cluster-ab.py`, `scripts/cluster-cpu.sh`, `scripts/cluster-{up,down,status,sync,doctor}.sh`, `scripts/monitoring.sh`, `scripts/deploy.sh` |
| orchestrator | `orchestrator/app/{engine_state,breaker,continuity,admission}.py`; edits in `llm.py`, `resilience.py`, `config.py`, `health.py`, `metrics.py`, `main.py` (the durability ledger); tests `orchestrator/tests/test_{breaker,continuity,admission,engine_state,llm_continuity,generation_durability}.py` |
| monitoring | `monitoring/prometheus/prometheus.yml`, `monitoring/prometheus/rules/{alerts,recording}.yml`, `monitoring/prometheus/tests/`, `monitoring/grafana/dashboards/dgx-cluster-overview.json`, `monitoring/grafana/dashboards/dgx-vllm-performance.json` |
| configuration | `.env.example` (`ENGINE_*`, `LLM_QUEUE_MAX_WAIT_S`, `ADMISSION_*`, `CLUSTER_SENTINEL_TOKEN`, `MAIN_MODEL_IMAGE`, `CLUSTER_GDN_PREFILL_BACKEND`, `VLLM_HEALTHCHECK_KILL_AFTER`) |
