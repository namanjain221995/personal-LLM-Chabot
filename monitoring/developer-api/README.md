# Developer API observability

Production monitoring for the public developer API (`/v1`) and for the shared
engines it loads: a Grafana dashboard **Developer API**, the alert rules in
`monitoring/prometheus/rules/developer-api.yml`, and a machine-readable record
of which metric exists today and which does not yet (2026-09-13).

Most `/v1`-specific metrics do not exist yet. This directory does not hide
that. Every panel and rule on a metric that is not in Prometheus today is
marked, and the metric contract below says exactly what to emit. The engine,
admission, node-memory, speech-to-text, upload-rail and database panels work
today.

**No alert here reaches a person yet.** Prometheus has no Alertmanager
(`GET /api/v1/alertmanagers` returns empty lists on 2026-09-13, and
docs/MONITORING.md says the same). The alerts are visible on Prometheus's
`/alerts` page and in Grafana, and nowhere else, until the owner configures a
receiver. Read every severity below as "how urgent", not "who is told".

## What is here

| file | what it is |
| --- | --- |
| `monitoring/grafana/dashboards/dgx-developer-api.json` | the dashboard, uid `dgx-developer-api`, folder DGX Spark |
| `monitoring/prometheus/rules/developer-api.yml` | 19 alerts in five groups: `node-memory`, `engine-pressure`, `developer-api`, `speech-to-text`, `host-guard` |
| `monitoring/prometheus/tests/developer_api.yml` | promtool unit tests: every alert fires on its shape and stays quiet on the look-alikes measured here |
| `monitoring/developer-api/metrics-contract.json` | every queried metric with its status: `live`, `event_driven`, `pending`, `proposed` |
| `monitoring/developer-api/check_metrics.py` | proves the contract against a running Prometheus (read-only) |
| `monitoring/developer-api/tests/` | offline tests: dashboard schema, contract coverage, panel markers, the textfile writer |
| `monitoring/exporters/host-guard/host_guard_textfile.sh` | writes the host packet filter's state for node-exporter (not installed) |

## How it reaches production

Nothing here restarts an engine, the orchestrator or a scrape target.

* **Rules.** Prometheus reads `/etc/prometheus/rules/*.yml` from the
  bind-mounted `monitoring/prometheus/rules`. A new file is loaded on the next
  configuration reload: `curl -X POST http://127.0.0.1:9090/-/reload` (the
  lifecycle API is enabled). After that, check `/rules` for the five groups
  with no `lastError`. `PrometheusRuleEvaluationFailing` in alerts.yml
  watches evaluation errors.
* **Delivery. Not in place.** There is no Alertmanager and no Grafana contact
  point, so a firing alert is a line on `/alerts`. Before any alert here can be
  an early warning, the owner has to choose a receiver (an Alertmanager with a
  route, or a Grafana contact point) and point Prometheus at it. That is a
  decision and a monitoring-container change, not part of this directory.
* **Dashboard.** Grafana's file provider rescans every 30 s. The dashboard
  appears once the deploy checkout contains the file.
* **Scrape jobs.** Unchanged. Every live metric here comes from a job that
  already exists.

## Metric status

`check_metrics.py` output on 2026-09-13 against production Prometheus:
82 expressions parsed, 44 metrics (17 live, 12 event_driven, 5 pending, 10
proposed), **RESULT PASS**, exit 0. Every live selector, labels included,
matched series in the last hour. 23 NOTE lines, all event_driven selectors
with no series in the last hour (no speech or upload since the orchestrator
started, no admission rejection ever); each of those selectors except
`llm_admission_rejections_total` matched series within the last 14 days, which
proves their labels. The result no longer depends on recent chat traffic: the
lazily published admission gauges are event_driven.

| status | meaning | metrics |
| --- | --- | --- |
| live | in Prometheus now | `vllm:*` (running, waiting, KV, tokens, iterations, preemptions, TTFT, e2e) and `http_requests_total` from every engine; `node_memory_*`; `pg_stat_user_tables_n_tup_ins/upd`; `techsara_vllm_state`; `upload_sessions_open` |
| event_driven | merged code; the series appears at the first event, so absent after an orchestrator start is normal | `llm_admission_lane_active`, `llm_admission_waiting` (published when a lane is used: present only intermittently since 2026-09-12); `llm_admission_rejections_total`; `asr_queue_depth`, `asr_active_requests`, `asr_requests_total`, `asr_batch_requests_total`, `asr_request_duration_seconds`, `asr_batch_request_duration_seconds` (none since 2026-09-11 18:41Z); `upload_session_total`, `upload_part_bytes_total`, `upload_finalize_seconds` |
| pending | written, not merged or not deployed | `public_api_engine_in_flight`, `public_api_engine_waiting` (publicapi/capacity.py); `techsara_host_guard_*` (the writer below, once installed) |
| proposed | nothing emits it | `public_api_requests_total`, `public_api_request_duration_seconds`, `public_api_ttft_seconds`, `public_api_streams_in_flight`, `public_api_background_jobs`, `public_api_background_jobs_total`, `public_api_output_tokens_total`, `public_api_capacity_refusals_total`, `usage_events_written_total`, `public_api_usage_settle_total` |

A panel on a pending or proposed metric is titled `[pending]` or `[proposed]`.
Its description starts with the reason, and while empty it shows "pending: not
emitted yet" instead of "no data". When a metric starts to exist,
`check_metrics.py` warns `PROMOTE`. Then change its status in the contract and
drop the marker.

Engine panels count chat and API traffic together, because an engine cannot
tell who sent a request. Each public model maps to one engine through the
`service` label:

| public model | engine `service` |
| --- | --- |
| techsara-35b | `main` |
| techsara-8b-vision | `router` |
| techsara-ocr | `ocr` |
| techsara-embed | `embed` |
| techsara-rerank | `reranker` |
| techsara-whisper | no engine metrics. The orchestrator's `asr_*` count dictation and video clips on the same replicas; public `/v1/audio/transcriptions` calls do not pass through them (publicapi/sidecars.py sends its own request) |

### Known defect: the capacity gate gauges lose their engine name

`publicapi/capacity.py` publishes `public_api_engine_in_flight{engine}` and
`public_api_engine_waiting{engine}` for six gates: `main.long`, `router`,
`ocr`, `embed`, `rerank` and `asr`. But `orchestrator/app/metrics.py` bounds
the label **name** `engine` to `{"main"}` for every metric (it was added for
the breaker). So each gate is exported as `engine="other"`, and the last gate
to publish overwrites the others. Reproduced with the module itself:

```
set_gauge('public_api_engine_in_flight', 1, engine='main.long'); ... router=2, ocr=0, embed=3
-> public_api_engine_in_flight{engine="other"} 3
```

Until this is fixed, the capacity-gate panels and `PublicApiGateWaitSustained`
see one merged series. The deploy guard that capacity.py's docstring suggests
(`public_api_engine_in_flight{engine="main.long"} > 0`) would never see a
running long generation. The fix belongs to the owner of metrics.py: add
per-metric vocabularies, and leave the global `engine` set alone.

```python
_GATES = {"main.long", "router", "ocr", "embed", "rerank", "asr"}
_ALLOWED_BY_METRIC["public_api_engine_in_flight"] = {"engine": _GATES}
_ALLOWED_BY_METRIC["public_api_engine_waiting"] = {"engine": _GATES}
```

### Known defect: the speech pool gauges overwrite each other

`orchestrator/app/asr.py` has two admission pools: `POOL` (dictation) and
`BATCH_POOL` (video clips, a subclass that inherits `__aenter__`). Both publish
the same unlabelled `asr_queue_depth` and `asr_active_requests`, so the last
pool to move wins. Reproduced with the module on 2026-09-13:

```
async with asr.POOL:                 # asr_active_requests 1
    async with asr.BATCH_POOL: pass  # a video clip enters and leaves
    # POOL.active is still 1, but the exposition says asr_active_requests 0
```

So the speech queue panel and `AsrQueueSustained` can miss a dictation queue
while video clips move; they cannot invent one. The fix belongs to the owner of
asr.py: a `pool` label (`dictation`, `video`), declared in metrics.py.

## Metric contract for the proposed metrics

Rules that hold for all of them, taken from `orchestrator/app/metrics.py`:

* Every label name and value comes from a closed set. Declare the metric in
  `_LABELS_BY_METRIC`, so that an undeclared label name is dropped and an
  unknown value becomes `other`. **Never** label by key, project, workspace,
  user or request id.
* Put histogram edges in `_BUCKETS_BY_METRIC`. The default edges stop at 30 s,
  and a 1M-token answer runs for hours.
* Register each counter's label values at 0 on startup. A series that first
  appears at 1 shows an `increase()` of 0, so the first event is otherwise
  invisible to every rule.
* Emit once per request, in the place that already settles it exactly once:
  the `finish(outcome)` recorder in `publicapi/router.py`, and the matching
  settle in `publicapi/endpoints.py`. Both already call `usage.record_async`.
  They run shielded from cancellation, so abandoned streams are counted too.

| metric | type | labels (closed) | emit |
| --- | --- | --- | --- |
| `public_api_requests_total` | counter | `route` responses, chat_completions, embeddings, rerank, audio_transcriptions, files, uploads, models, other · `model` the six public ids, none, other · `status` HTTP status sent (200 202 400 401 403 404 409 413 415 422 499 500 502 503 504 other) · `error` the terminal ApiError code or none; **`at_capacity`** for `errors.model_at_capacity`, which otherwise shares `model_unavailable` | at settle. A stream that fails after its 200 keeps `status="200"` and carries its error |
| `public_api_request_duration_seconds` | histogram | `route`, `model`, `mode` sync/stream/background · buckets 0.1 … 21600 | at settle, from `outcome.duration_ms` |
| `public_api_ttft_seconds` | histogram | `route`, `model` · buckets 0.1 … 1800 (a full-window prefill measured 878 s) | at settle, from `outcome.ttft_ms` when not None |
| `public_api_streams_in_flight` | gauge | `route`, `model` | +1 when an SSE body starts, −1 in its `finally` (publicapi/streaming.py) |
| `public_api_background_jobs` | gauge | `status` queued, in_progress | on each transition in publicapi/background.py, and on the restart repair |
| `public_api_background_jobs_total` | counter | `status` completed, failed, cancelled | when a job reaches a terminal status |
| `public_api_output_tokens_total` | counter | `model` | at settle, by the MEASURED output tokens; unmeasured adds nothing (never 0 for "unknown") |
| `mode` on the pending `public_api_engine_waiting` | label | `mode` sync, stream, background | in capacity.py `_publish`, from the caller's wait bound. Without it a background job waiting up to an hour by design looks like a caller about to be refused (see "Capacity waits") |
| `public_api_capacity_refusals_total` | counter | `engine` main (admission lanes), main.long, router, ocr, embed, rerank, asr | beside each `errors.model_at_capacity(...)`: capacity.py's two gate timeouts, and streaming.py's `AdmissionRejected` mapping (engine main) |
| `usage_events_written_total` | counter | `result` ok, fail | `usage.record`: ok after the insert, fail in its `except` (today the only trace of a failure is a debug log line) |
| `public_api_usage_settle_total` | counter | `result` ok, fail | `apiplatform/quotas.record_usage`, same shape |

Series-count bound: 9 routes × 8 models × 16 statuses × 16 errors is the worst
case on paper, but real combinations are sparse (an embeddings call has one
model and a handful of outcomes). Expect a few hundred series.

## Alerts

All 19 alerts are in `developer-api.yml`, with one promtool case or more each.
The existing alerts in alerts.yml are unchanged. The rules here add only what
those cannot say (see the header of developer-api.yml).

| alert | severity | metric status | fires when |
| --- | --- | --- | --- |
| NodeMemAvailableLow | warning | live | a node under 8 GiB available for 2m |
| NodeMemAvailableCritical | critical | live | under 4 GiB for 1m |
| NodeSwapRisingUnderMemoryPressure | warning | live | swap +2 GiB in 15m while under 16 GiB available, 5m |
| NodeSwapNearlyExhausted | warning | live | swap over 80% for 5m |
| VllmMainKvCacheSaturatedSustained | critical | live | main KV over 90% for 15m (escalates VllmKvCacheNearlyFull) |
| VllmAuxKvCacheSaturatedSustained | warning | live | an auxiliary engine's KV over 90% for 10m |
| EnginePreemptionsIncreasing | warning | live | more than 5 preemptions in 15m on any engine, 5m |
| EngineHttp5xxRatioHigh | warning | live | an engine fails over 5% of its inference requests (probe handlers left out) across 10m, with at least 2 failures, 5m |
| VllmWedgeSignatureFromEngineCounters | critical | live | main: requests running for 3m, no counter or KV change, 1m (no controller needed) |
| AuxEngineWedgeSignature | warning | live | the same shape on an auxiliary engine, 2m |
| LlmAdmissionWaitSustained | warning | event_driven | anyone waiting for an admission lane for 5m |
| LlmAdmissionRejections | warning | event_driven | any admission refusal in 10m |
| PublicApiGateWaitSustained | warning | pending | a public gate other than `main.long` has waiters for 10m |
| PublicApiAtCapacityRefusals | warning | proposed | more than 5 at-capacity 503s on one engine in 10m, 5m |
| PublicApi5xxRatioHigh | warning | proposed | one model has more than 5% server-side failures across 10m, with at least 3 failures, 5m |
| AsrFailureRatioHigh | warning | event_driven | over 20% of dictation or of video transcriptions failed in 15m, with at least 2 failures, 5m |
| AsrQueueSustained | warning | event_driven | transcriptions waiting for a whisper slot for 5m |
| HostGuardTableMissing | critical | pending | the filter table is not loaded on a node whose check works, 2m |
| HostGuardSignalUnproven | warning | pending | the check cannot run, or stopped writing 15m ago |

### Node memory

The thresholds come from 14 days of this cluster's own history, and from the
OOM work's recommendation (docs/developer-platform/OPERATIONS.md §14). The
head fell below 8 GiB on 6 days (12 separate runs at 60 s resolution). Its
lowest point was 3.08 GiB at 2026-09-12 22:37Z: 36.3 GiB at 22:25Z, 7.0 GiB
three minutes later, 55 GiB again at 22:40:30Z. **The cause is not
established.** The main engine was not restarted (its process start time
stayed 2026-09-12 10:46:27Z), no container started, the kernel counted no OOM
kill, per-process GPU memory did not move, and no container was above about
2.8 GiB. The worker never went below 11.3 GiB. Swap growth on its own happens
about three times a day on the head, because a model reload pushes idle pages
out. That is why the swap alert also requires available memory under 16 GiB.

These alerts are **not** silenced during a model reload. HeadSwapActivity is,
but the OOM killer does not care why memory ran out. They are also **not
delivered** (see the top of this file): until a receiver exists, the
one-minute critical warning is only useful to someone already looking.

`NodeMemAvailableLow` overlaps alerts.yml's `UnifiedMemoryPressure` (used
above 92% for 5m, which is MemAvailable under about 9.7 GiB on these nodes),
and the two fire together. It is kept because it is earlier on a fast drain
(2m against 5m) and because it carries this runbook and a command that can see
GPU memory.

**First action: find the holder with a command that can see it.** On GB10 the
engines' memory is unified GPU memory, which neither `docker stats` nor a
process's RSS counts. On 2026-09-13 `docker stats` showed the main engine
container at 3.25 GiB while `nvidia-smi` showed its rank holding 24.8 GiB, and
the router at 845 MiB against 16.2 GiB. Run both views:

```bash
free -g
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv   # GPU holders
ps -eo pid,rss,comm --sort=-rss | head -8                                   # host RSS
```

The 2026-09-12 dip was invisible to `docker stats` and to the GPU view, so do
not stop at one of them.

**Then free memory in the order of OPERATIONS.md §14:**

1. What costs nobody: `pg-test`, the e2e stack, a desktop session, a running
   test suite.
2. The expendable GPU holders, which free the most: the OCR engine (~15 GiB),
   the auxiliary engines (router ~16 GiB, embed and reranker ~4 GiB each) and
   speech (3-5 GiB). Each one takes a public model offline, so this is an
   operator's decision.
3. Never postgres, the orchestrator or the main engine pair.

### KV cache saturated

The main engine's pool is 1.66 full windows. One public generation near 1M
tokens plus one full-window chat prompt can fill it. Past about 90%, vLLM
preempts and recomputes: a near-full chat prompt costs about 15 minutes. Look
at what holds the KV: the `main.long` gate, a long chat turn, or both. The
router's pool is 1.07 of its window, and the chat app calls it on every turn.

### Preemptions

On every engine, preemptions were zero for the 14 days before this release.
Several in 15 minutes means KV pressure. Read this with the KV panel for the
same engine.

### Engine 5xx

This is the engine's own HTTP answers (`http_requests_total{status="5xx"}`),
for chat and API together, **on inference handlers only**. `/v1/models`,
`/health`, `/metrics`, `/ping`, `/version`, `/tokenize`, `/detokenize`,
`/openapi.json` and unmatched paths are left out of both sides of the ratio.
Those polls answer while inference is broken, and they outnumber it: 7-day
medians per 10 minutes were 70.6 `/v1/models` polls against 10.1 completions
on main, and 19.5 polls against 0 completions on router and ocr. The first
version counted them, so a router failing its only real request, or main
failing 3 of 10 completions (3/80 = 3.75%), stayed silent. There is no
request floor any more: 2 failed completions out of 2 fire.

The rule still requires at least two failures in the window. On 2026-09-13,
embed failed one request about every 35 minutes on about 2 requests a minute
(40 of about 3,260 in 24 hours). That holds its 10-minute ratio near 5.0%,
right on the line, where `increase()` extrapolation decides whether it fires.
That lone failure is still visible on the dashboard's engine 5xx panel, and
still unexplained. Replayed over 14 days, the rule holds for 5 minutes once:
embed, 2026-09-06 01:14Z. The `handler` label says which endpoint. Read that
engine's container log next.

A handler's 5xx series is created on its first failure after an engine start,
and `increase()` does not count a series' first sample. So on a freshly
started engine it takes a third failure to fire.

### Aux engine wedged

No controller watches the auxiliary engines. The chat app degrades around a
missing router, embedder or reranker, but public calls to that engine hang
until their read timeout. Restarting that one service is an operator
decision. It is not a model-pair restart.

The **main**-engine rule's runbook is docs/availability/RUNBOOK.md#wedged. That
rule was replayed over 14 days of history. It fired on all 10 known fault
episodes. It did not fire on the 2026-09-12 needle_950k prefill: that prefill
kept every token and iteration counter flat for 12 minutes while KV usage rose
from 9% to 56%, and the controller of that day called it WEDGED. Changing KV
usage is the progress witness that tells the two apart.

### Capacity waits

The admission lanes are shared by chat and `/v1`. Their gauges are published
when a lane is used, so they are absent after an orchestrator restart until
the first generation; `LlmAdmissionWaitSustained` is silent while they are.

Public gates exist only on the public side: chat never waits for them. Waiting
behind a LONG request is by design, up to the lane's bound. So is a background
job waiting for its gate, for up to `PUBLIC_API_BACKGROUND_GATE_WAIT_S`
(3,600 s); a queue of them behind one `main.long` generation can last hours.
That is why `PublicApiGateWaitSustained` leaves `main.long` out. A sync or
stream request that waits out its gate (`PUBLIC_API_GATE_WAIT_S`, 30 s) gets
`503 model_unavailable` with `Retry-After`, which `PublicApiAtCapacityRefusals`
counts. On the other gates a background job waits the same way, and the gauge
cannot tell it from a sync caller (the proposed `mode` label would), so look at
the background jobs panel before calling it a capacity shortage.

`PublicApiGateWaitSustained` is **unreliable until the engine-label fix** in
metrics.py: every gate publishes to one folded series, and an idle gate that
publishes 0 overwrites a gate with waiters, so the rule can stay silent while
people wait.

### Public API 5xx

The metric is proposed. The alert counts `internal_error`, `timeout` and
`model_unavailable` outcomes, including streams that failed after their 200.
It leaves out `at_capacity`, which has its own alert, and `model_recovering`:
the engine restarted and said so, and the vllm-availability alerts own that
case. For each `internal_error`, the orchestrator logs `unhandled error on the
public API (request_id=…)`.

It is computed **per model**. One ratio across all six models let a busy one
hide a broken one: 1,000 good embeddings and 30 failed OCR calls is 2.9%
overall while techsara-ocr is 100% down. There is no per-model request floor,
because a floor hides exactly the quiet model. Instead it needs at least three
failures: the official SDKs retry a 5xx twice by default, so one caller request
that fails through its retries is three failures. The threshold is not
calibrated; the metric does not exist yet.

### Speech to text

`techsara-whisper`, dictation and video analysis share the whisper-large-v3
replicas. The engine has no `/metrics` (prometheus.yml explains why there is no
scrape job), so the orchestrator's `asr_*` series are the view, and they see
the chat app's own use only: dictation (`asr.transcribe`) and video clips
(`asr.transcribe_segments`). Public `/v1/audio/transcriptions` calls go to the
replicas by their own path and are not counted there. So a broken replica shows
up through dictation and video. Public whisper traffic alone shows only in the
proposed `public_api_requests_total{model="techsara-whisper"}` and the pending
`public_api_engine_*{engine="asr"}`.

Every `asr_*` series is created on first use. Production had them from
2026-09-04 to 2026-09-11 18:41Z and none since, so both alerts are silent until
someone transcribes. In that week there were 46 dictation attempts (7 failed)
and 283 video clips (6 failed). The queue never went above 0, and at most 2
transcriptions ran at once.

* `AsrFailureRatioHigh`: over 20% failed, with at least two failures, in 15
  minutes, for `path="dictation"` or `path="video"`. Replayed over 14 days it
  fires once on each path (2026-09-08 00:45Z dictation, 2026-09-11 02:05Z
  video). Both were real failures; the cause was not investigated.
* `AsrQueueSustained`: a transcription waiting for a slot for 5 minutes. A
  dictation waiter gives up after `ASR_QUEUE_WAIT_S` (8 s), so this is a
  steady stream of people told "busy". It can miss a queue because of the pool
  gauge defect above.

**First action:** `scripts/whisper.sh status` (container state and the engine's
own `/health`), then the orchestrator's `GET /audio/health` with an
administrator session. Restarting a replica is an operator's decision.

### Uploads

The dashboard's upload panels read the chat app's chunked upload rail
(`upload_sessions_open`, `upload_session_total`, `upload_part_bytes_total`,
`upload_finalize_seconds`). Its alerts already exist in alerts.yml
(`UploadSessionsExpiringInBursts`, `UploadStuckFinalizing`) and are not
duplicated. The public `/v1` Files and Uploads API has no code yet; its
requests will be counted by the proposed `public_api_requests_total` with
`route="files"` or `route="uploads"`, and one panel is ready for that.

## Host packet filter

The filter is the nftables table `inet techsara_guard` (OPERATIONS.md §13).
It does not survive a reboot.

**No signal exists today, and none is possible without extra setup.**

* `nft list table` needs root. As the normal user it prints `Operation not
  permitted (you must be root)`. That was checked on the head.
* A blackbox probe from inside the cluster cannot tell a filtered port from
  an open one. Every in-cluster path (loopback, the Docker bridges, the
  fabric) is one the filter deliberately accepts.
* node-exporter has no nftables collector, and no textfile collector is
  configured.

`monitoring/exporters/host-guard/host_guard_textfile.sh` writes the signal as
a node-exporter textfile. It only reads; it never changes a rule. It has two
sources, and the `source` label says which one produced a value:

* **`nft`**, when run as root. This is authoritative.
* **`state_file`**, when run as any user. `scripts/host-guard.sh apply` writes
  `/run/techsara-host-guard/state`, and `remove` deletes it. A reboot clears
  it, which is exactly the case to catch. It cannot see a ruleset flushed by
  hand.

Run once as the normal user on the head (2026-09-13), it wrote
`techsara_host_guard_table_present{source="state_file"} 1`.

To make it live, the owner decides and does two things:

1. Give node-exporter `--collector.textfile.directory=/host/<dir>` on both
   nodes. The host root is already mounted at `/host`. This needs a
   node-exporter recreate. It is a monitoring container, not a model.
2. Run the script every minute with `TEXTFILE_DIR=<dir>`. Use a root systemd
   timer for `source=nft`, or the user's own timer for `source=state_file`.

Until both are done, `HostGuardTableMissing` and `HostGuardSignalUnproven`
have nothing to read and stay silent. They are built never to treat a missing
series as a missing table.

The writer reports "absent" only when the lookup could have found the file:
the state directory exists and is searchable, or it does not exist and its
parent is searchable. A state directory it cannot search is `check_ok 0`, never
a missing table. The dashboard's stat shows `loaded`, `MISSING` (the check ran)
or `UNKNOWN` (the check could not run).

`HostGuardTableMissing` names the right verify command per node:
`scripts/host-guard.sh verify` on the head, and
`bash ~/.techsara-cluster/host-guard.sh verify --role worker` on the worker
(OPERATIONS.md §13).

## Validation

Run from the repository root:

```bash
IMG=$(grep -o 'prom/prometheus@sha256:[0-9a-f]*' compose/compose.monitoring.yaml | head -1)
docker run --rm -v "$PWD/monitoring/prometheus:/p:ro" --entrypoint promtool "$IMG" check rules /p/rules/developer-api.yml
docker run --rm -v "$PWD/monitoring/prometheus:/p:ro" --entrypoint promtool "$IMG" test rules /p/tests/developer_api.yml
python3 -m unittest discover -s monitoring/developer-api/tests -v
python3 monitoring/developer-api/check_metrics.py            # needs Prometheus on 127.0.0.1:9090, and PyYAML
```

The tests are not wired into CI; neither are the existing promtool tests.
