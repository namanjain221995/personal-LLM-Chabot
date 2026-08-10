# Infrastructure — Docker Compose, images, build contexts

> **⚠ Superseded in part (2026-08-10).** The app-state layer described below was
> `/data/app.sqlite3` (stdlib `sqlite3`). It is now PostgreSQL — see
> [`data-model.md`](data-model.md) and the CHANGELOG entry
> "App state moved from SQLite to PostgreSQL". Every `sqlite3` reference,
> `db.py` line number and finding about SQLite locking below is a snapshot of
> the pre-migration code and has NOT been re-derived. The DuckDB warehouse and
> LanceDB sections are unaffected and remain accurate.

The entire platform is one 355-line compose file
([`docker-compose.yml`](../../docker-compose.yml)): 4 vLLM model servers, the FastAPI orchestrator,
the Salesforce sync worker, SearXNG and the Next.js frontend. Project name `sf-local-ai`
([:5](../../docker-compose.yml#L5)); three named volumes `hf-cache`, `data`, `reports`
([:7-10](../../docker-compose.yml#L7-L10)). Secrets arrive **only** by `${VAR}` interpolation from the
host `.env` ([:3](../../docker-compose.yml#L3)).

## Service matrix

| Service | Image / build | Host ports | GPU | Healthcheck | Restart | `depends_on` | Profile |
|---|---|---|---|---|---|---|---|
| `vllm` [:28](../../docker-compose.yml#L28) | `vllm/vllm-openai:nightly` [:36](../../docker-compose.yml#L36) | `8000:30000` [:86](../../docker-compose.yml#L86) | `count: all` [:95-101](../../docker-compose.yml#L95-L101) | `curl /health`, 30 s × 60, `start_period 30m` [:90-94](../../docker-compose.yml#L90-L94) | `unless-stopped` [:89](../../docker-compose.yml#L89) | — | default |
| `vllm-vision` [:103](../../docker-compose.yml#L103) | `nvcr.io/nvidia/vllm:26.05-py3` [:104](../../docker-compose.yml#L104) | `8001:30001` [:133](../../docker-compose.yml#L133) | `count: all` [:140-146](../../docker-compose.yml#L140-L146) | 30 s × 60, `start_period 20m` [:135-139](../../docker-compose.yml#L135-L139) | `unless-stopped` [:134](../../docker-compose.yml#L134) | — | `vision` [:116](../../docker-compose.yml#L116) |
| `vllm-router` [:155](../../docker-compose.yml#L155) | `vllm/vllm-openai:nightly` [:156](../../docker-compose.yml#L156) | `8002:30002` [:171](../../docker-compose.yml#L171) | `count: all` [:178-184](../../docker-compose.yml#L178-L184) | 30 s × 60, `start_period 15m` [:173-177](../../docker-compose.yml#L173-L177) | `unless-stopped` [:172](../../docker-compose.yml#L172) | — | default |
| `vllm-embed` [:186](../../docker-compose.yml#L186) | `nvcr.io/nvidia/vllm:26.05-py3` [:187](../../docker-compose.yml#L187) | `8003:30003` [:202](../../docker-compose.yml#L202) | `count: all` [:209-215](../../docker-compose.yml#L209-L215) | 30 s × 60, `start_period 10m` [:204-208](../../docker-compose.yml#L204-L208) | `unless-stopped` [:203](../../docker-compose.yml#L203) | — | default |
| `orchestrator` [:217](../../docker-compose.yml#L217) | `build: ./orchestrator` [:218](../../docker-compose.yml#L218) | `8080:8080` [:272-273](../../docker-compose.yml#L272-L273) | `count: all` [:283-289](../../docker-compose.yml#L283-L289) | **none** | **none** | `vllm`, `vllm-router`, `vllm-embed` — all `service_healthy` [:274-282](../../docker-compose.yml#L274-L282) | default |
| `sync-worker` [:291](../../docker-compose.yml#L291) | `build: ./sync-worker` [:292](../../docker-compose.yml#L292) | **none** | none | **none** | **none** | `vllm-embed: service_healthy` [:329-331](../../docker-compose.yml#L329-L331) | default |
| `searxng` [:336](../../docker-compose.yml#L336) | `searxng/searxng:latest` [:337](../../docker-compose.yml#L337) | **none** (internal only) | none | **none** | `unless-stopped` [:343](../../docker-compose.yml#L343) | — | `search` [:344](../../docker-compose.yml#L344) |
| `frontend` [:346](../../docker-compose.yml#L346) | `build: ./frontend` [:347](../../docker-compose.yml#L347) | `3000:3000` [:351-352](../../docker-compose.yml#L351-L352) | none | **none** | **none** | `orchestrator: service_started` [:353-355](../../docker-compose.yml#L353-L355) | default |

Six host ports are published, all on **0.0.0.0** — there is no `127.0.0.1:` prefix anywhere in the
file. There is **no `networks:` section**, so every container shares the default project bridge and
any container can reach any other, including the four unauthenticated model servers.

## vLLM serving flags

| Service | Weights | `--max-model-len` | `--gpu-memory-utilization` | Other flags |
|---|---|---|---|---|
| `vllm` | `/models/Qwen3.6-35B-A3B-NVFP4`, served as `Qwen/Qwen3.6-35B-A3B-NVFP4` [:64-65](../../docker-compose.yml#L64-L65) | `262144` [:68](../../docker-compose.yml#L68) | **0.35** [:69](../../docker-compose.yml#L69) | `--kv-cache-dtype fp8` [:70](../../docker-compose.yml#L70), `--reasoning-parser qwen3` [:71](../../docker-compose.yml#L71), `--trust-remote-code` [:72](../../docker-compose.yml#L72), `--quantization modelopt` [:73](../../docker-compose.yml#L73), `--attention-backend flashinfer` [:74](../../docker-compose.yml#L74), `--moe-backend marlin` [:75](../../docker-compose.yml#L75), `--enable-chunked-prefill` [:76](../../docker-compose.yml#L76), `--enable-prefix-caching` [:77](../../docker-compose.yml#L77), `--max-num-batched-tokens 8192` [:78](../../docker-compose.yml#L78) |
| `vllm-vision` | `Qwen/Qwen3-VL-2B-Instruct` (HF pull) [:122](../../docker-compose.yml#L122) | `8192` [:125](../../docker-compose.yml#L125) | **0.11** [:126](../../docker-compose.yml#L126) | `--enforce-eager` [:127](../../docker-compose.yml#L127) |
| `vllm-router` | `/models/qwen3-vl-8b-fp8`, served as `Qwen/Qwen3-VL-8B-Instruct-FP8` [:158-159](../../docker-compose.yml#L158-L159) | `65536` [:162](../../docker-compose.yml#L162) | **0.14** [:163](../../docker-compose.yml#L163) | `--kv-cache-dtype fp8` [:164](../../docker-compose.yml#L164) |
| `vllm-embed` | `Qwen/Qwen3-Embedding-0.6B` [:191](../../docker-compose.yml#L191) | `4096` [:194](../../docker-compose.yml#L194) | **0.04** [:195](../../docker-compose.yml#L195) | `--runner pooling` [:196](../../docker-compose.yml#L196) |

`vllm` and `vllm-router` use images whose `ENTRYPOINT` is already `["vllm","serve"]`, so their
`command:` supplies arguments only ([:34-35](../../docker-compose.yml#L34-L35)); the two NGC-based
services spell out `vllm serve` ([:122](../../docker-compose.yml#L122),
[:191](../../docker-compose.yml#L191)).

## GPU budget arithmetic

The GB10 has **121 GB of unified memory** — not a discrete GPU, so each
`--gpu-memory-utilization` fraction comes out of the same pool as the OS, the containers and the
page cache ([:18-20](../../docker-compose.yml#L18-L20)).

| Configuration | Sum | ≈ GB | vs. the file's own ceiling |
|---|---|---|---|
| Default `docker compose up -d` (`vllm` + `vllm-router` + `vllm-embed`) | `0.35 + 0.14 + 0.04 = **0.53**` | ~64 GB | under `≲ 0.6` [:25](../../docker-compose.yml#L25) |
| With `--profile vision` (`+ vllm-vision` at 0.11) | `0.35 + 0.11 + 0.14 + 0.04 = **0.64**` | ~77 GB | **over the ceiling** |

The header comment states the 0.53 sum and the ≲0.6 rule
([:21-25](../../docker-compose.yml#L21-L25)) and warns that over-reserving "took the box to 0 GB free
once" — but it **omits `vllm-vision` from the arithmetic entirely**, and
[`README.md:283-288`](../../README.md#L283-L288) repeats the same incomplete sum. Enabling the
documented image-answer path ([:112-113](../../docker-compose.yml#L112-L113)) therefore silently
exceeds the file's own limit.

Two further consumers sit **outside** the budget:
- The **orchestrator holds its own GPU reservation** ([:283-289](../../docker-compose.yml#L283-L289))
  for the in-process `Qwen/Qwen3-Reranker-0.6B` ([:243-244](../../docker-compose.yml#L243-L244),
  `RERANK_ENABLED: "true"`), loaded lazily at
  [rag.py:52-66](../../orchestrator/app/engines/rag.py#L52-L66).
- `shm_size: 32g` on `vllm` ([:87](../../docker-compose.yml#L87)) plus `ipc: host`
  ([:88](../../docker-compose.yml#L88)).

**Nothing enforces the ceiling at runtime.** There is no `mem_limit`, no `cpus:`, no
`deploy.resources.limits` on any service — the `deploy.resources.reservations.devices` blocks are
device *reservations*, not memory caps, and are duplicated verbatim five times
([:95-101](../../docker-compose.yml#L95-L101), [:140-146](../../docker-compose.yml#L140-L146),
[:178-184](../../docker-compose.yml#L178-L184), [:209-215](../../docker-compose.yml#L209-L215),
[:283-289](../../docker-compose.yml#L283-L289)). The only guard is the comment.

## Cold-start gate

`orchestrator` starts only after **all three** default model servers report healthy
([:274-282](../../docker-compose.yml#L274-L282)); `vllm-vision` is deliberately excluded
([:279-280](../../docker-compose.yml#L279-L280)) so the profile can stay off without blocking the app
layer. `sync-worker` waits on `vllm-embed` alone ([:329-331](../../docker-compose.yml#L329-L331)).

The gate's worst-case duration is set by `start_period`, during which failing probes do not count
against `retries`:

| Gating service | `start_period` | Justification in file |
|---|---|---|
| `vllm` | **30m** [:94](../../docker-compose.yml#L94) | "~65 GB download on first start" |
| `vllm-router` | **15m** [:177](../../docker-compose.yml#L177) | — |
| `vllm-embed` | **10m** [:208](../../docker-compose.yml#L208) | — |
| (`vllm-vision`, not gating) | 20m [:139](../../docker-compose.yml#L139) | — |

All four also allow `retries: 60` at `interval: 30s`, i.e. up to a further 30 minutes of failing
probes after the start period. `frontend` depends on `service_started` only
([:355](../../docker-compose.yml#L355)), so the UI is reachable and will render before the backend
can answer anything.

---

## `docker-compose.yml`

**Purpose** — Single-file deployment of the whole platform, with every secret injected by `${VAR}`
interpolation from the host `.env`.

**Public surface** — 7 services, 3 named volumes, 1 project name — see the service matrix above.

**Control flow** — startup order:
1. Compose resolves `${VAR}` from the host `.env` for ~25 interpolated values
   ([:80,84,129,166,198,224-267,299-328,339](../../docker-compose.yml#L80)).
2. `vllm`, `vllm-router`, `vllm-embed` start in parallel; each waits on its own `curl /health`
   ([:90-94](../../docker-compose.yml#L90-L94), [:173-177](../../docker-compose.yml#L173-L177),
   [:204-208](../../docker-compose.yml#L204-L208)).
3. `orchestrator` starts after all three are healthy ([:274-282](../../docker-compose.yml#L274-L282)).
4. `sync-worker` starts after `vllm-embed` is healthy ([:329-331](../../docker-compose.yml#L329-L331)).
5. `frontend` starts on `service_started` of the orchestrator ([:353-355](../../docker-compose.yml#L353-L355)).
6. `searxng` and `vllm-vision` never start unless their profile is named
   ([:344](../../docker-compose.yml#L344), [:116](../../docker-compose.yml#L116)).

**State & side effects**
- **Volumes/mounts**

  | Mount | Services | Mode |
  |---|---|---|
  | `hf-cache:/root/.cache/huggingface` | `vllm` [:82](../../docker-compose.yml#L82), `vllm-vision` [:131](../../docker-compose.yml#L131), `vllm-router` [:168](../../docker-compose.yml#L168), `vllm-embed` [:200](../../docker-compose.yml#L200), **and the orchestrator** [:271](../../docker-compose.yml#L271) | rw |
  | `${VLLM_MODELS_DIR:-/home/techsphere/Documents/projects/vllm_models}:/models` | `vllm` [:84](../../docker-compose.yml#L84), `vllm-router` [:169](../../docker-compose.yml#L169) | **ro** |
  | `data:/data` — DuckDB warehouse, LanceDB, `app.sqlite3`, Parquet, workspaces | orchestrator [:269](../../docker-compose.yml#L269), sync-worker [:320](../../docker-compose.yml#L320) | **rw on both** |
  | `reports:/reports` | orchestrator [:270](../../docker-compose.yml#L270) | rw |
  | `./sync-worker/config.yaml:/app/config.yaml` | sync-worker [:324](../../docker-compose.yml#L324) | **ro** |
  | `${SF_PRIVATE_KEY_HOST_FILE:-/dev/null}:/run/secrets/sf_jwt_key.pem` | sync-worker [:328](../../docker-compose.yml#L328) | **ro** |
  | `./searxng:/etc/searxng` | searxng [:342](../../docker-compose.yml#L342) | **rw** — the host config dir is writable by the container, and on this box it is owned by uid/gid 977 |

- **Network egress**: Hugging Face model downloads on first start of `vllm-vision` and `vllm-embed`
  (`HF_TOKEN` [:129,198](../../docker-compose.yml#L129)); Salesforce REST from the orchestrator and the
  sync worker (`SF_LOGIN_URL` [:226,304](../../docker-compose.yml#L226)); the open web via SearXNG when
  `SEARCH_ENABLED` ([:251](../../docker-compose.yml#L251)).

**Dependencies** — Inbound: [`README.md:105-108,217-231,259-271,300-302`](../../README.md#L105-L108),
[`CHANGELOG.md:181-182`](../../CHANGELOG.md#L181-L182). Outbound: three container registries
(`vllm/vllm-openai`, `nvcr.io/nvidia/vllm`, `searxng/searxng`) and three local build contexts
(`./orchestrator`, `./sync-worker`, `./frontend`).

**Config**
- Interpolated from the host `.env`: `HF_TOKEN`, `VLLM_MODELS_DIR`, `SF_CLIENT_ID`,
  `SF_CLIENT_SECRET`, `SF_LOGIN_URL`, `SF_API_VERSION`, `SF_LIVE_ENABLED`, `SESSION_SECRET`,
  `SEARCH_ENABLED`, `SEARCH_PROVIDER`, `SEARXNG_URL`, `TAVILY_API_KEY`, `BRAVE_API_KEY`,
  `SEARCH_MAX_RESULTS`, `FETCH_TIMEOUT_MS`, `FETCH_MAX_BYTES`, `MODEL_MAX_CONTEXT`,
  `MODEL_MAX_OUTPUT`, `URL_ANALYSIS_ENABLED`, `URL_MAX_PAGES`, `REPO_ANALYSIS_ENABLED`,
  `REPO_MAX_MB`, `REPO_MAX_FILES`, `WORKSPACE_TTL_HOURS`, `WORKSPACE_QUOTA_GB`, `SF_USERNAME`,
  `SF_PRIVATE_KEY_HOST_FILE`, `SF_PRIVATE_KEY_B64`, `SEARXNG_SECRET`
  ([:80-84,224-267,299-328,339](../../docker-compose.yml#L224-L267)).
- Hard-coded, non-overridable: `OPENAI_BASE_URL` [:229](../../docker-compose.yml#L229),
  `OPENAI_API_KEY: local-no-key` [:230](../../docker-compose.yml#L230), `MAIN_MODEL`
  [:231](../../docker-compose.yml#L231), `DEFAULT_MAX_CONTEXT: "32768"`
  [:232](../../docker-compose.yml#L232), `ROUTER_BASE_URL`/`ROUTER_MODEL`
  [:234-235](../../docker-compose.yml#L234-L235), `AGENT_BASE_URL`/`AGENT_MODEL`
  [:236-237](../../docker-compose.yml#L236-L237), `VISION_BASE_URL`/`VISION_MODEL`
  [:239-240](../../docker-compose.yml#L239-L240), `EMBED_BASE_URL`/`EMBED_MODEL`
  [:241-242](../../docker-compose.yml#L241-L242), `RERANKER_MODEL`/`RERANK_ENABLED`
  [:243-244](../../docker-compose.yml#L243-L244),
  `DUCKDB_PATH`/`LANCEDB_DIR`/`PARQUET_DIR`/`REPORTS_DIR`
  [:245-248](../../docker-compose.yml#L245-L248), `SYNC_INTERVAL_MINUTES: "30"`
  [:313](../../docker-compose.yml#L313), `EMBED_VIA` [:314](../../docker-compose.yml#L314),
  `ORCHESTRATOR_URL` [:349](../../docker-compose.yml#L349), `NEXT_PUBLIC_APP_NAME`
  [:350](../../docker-compose.yml#L350).
- **There is no `env_file:` anywhere in the file** (verified). Consequence: any `.env` variable not
  named in a service's `environment:` block never reaches that container. Cross-checking
  [`orchestrator/app/config.py`](../../orchestrator/app/config.py) (88 env names) against
  [:219-267](../../docker-compose.yml#L219-L267) shows ~40 documented settings that are silently
  inert, including `CHART_TRIGGER_MODE`, `CHART_FUNNEL_STAGE_ORDER`, `CONTEXT_*`, `KEEP_RECENT_TURNS`,
  `SEMANTIC_RECALL_ENABLED`, `RETRIEVE_TOP_K`, `LOCAL_USERNAME`, `CORS_ALLOW_ORIGINS`, `APP_DB_PATH`,
  `UPLOAD_MAX_MB`, `WORKSPACE_DIR`, `RAG_TOP_K`, `SQL_PREVIEW_ROW_CAP`, `EXPORT_ROW_CAP`,
  `LLM_REQUEST_TIMEOUT`, `HEALTH_PROBE_TIMEOUT`, `LANCEDB_TABLE`, `SF_LIVE_TIMEOUT`. The same holds
  for the worker: `SYNC_AUTO_FIELDS`, `SYNC_MAX_FIELDS` and `SYNC_REPORT_NEW_OBJECTS` are read by
  [config.py:37-41](../../sync-worker/syncworker/config.py#L37-L41) and documented at
  [`README.md:150-152`](../../README.md#L150-L152) but are absent from
  [:293-318](../../docker-compose.yml#L293-L318).
- Conversely, `SESSION_SECRET` **is** forwarded ([:249](../../docker-compose.yml#L249)) but nothing
  reads it — [config.py:259](../../orchestrator/app/config.py#L259) reads `SESSION_SECRET_FILE` only,
  and login was removed ([`CHANGELOG.md:3-12`](../../CHANGELOG.md#L3-L12)). Dead config.

**Failure modes**
- `vllm/vllm-openai:nightly` ([:36,156](../../docker-compose.yml#L36)) and `searxng/searxng:latest`
  ([:337](../../docker-compose.yml#L337)) are **moving tags with no digest pin**.
  [`README.md:273-276`](../../README.md#L273-L276) explicitly instructs the operator *not* to run
  `docker compose pull` because a newer nightly "can silently break model loading" — the deployment is
  reproducible only by never re-pulling.
- `SEARXNG_SECRET: ${SEARXNG_SECRET:-please-change-me}` ([:339](../../docker-compose.yml#L339)) starts
  SearXNG with a publicly-known secret when the operator forgets the variable.
- **`orchestrator`, `sync-worker` and `frontend` have no `restart:` policy**, so after a daemon or host
  restart the four model servers come back and the entire application layer does not.
- **No healthcheck** on orchestrator, sync-worker, searxng or frontend, so `docker compose ps` cannot
  distinguish "running" from "serving".
- No `networks:` segmentation, no `mem_limit`, no `cpus:`, no `read_only:` root filesystems, no
  `cap_drop`, no `user:` overrides.

**Concurrency** — `sync-worker` and `orchestrator` both mount `data:/data` **read-write**
([:269,320](../../docker-compose.yml#L269)) and both open `/data/warehouse.duckdb`
([:245,316](../../docker-compose.yml#L245)). DuckDB is single-writer; the split is safe only because
every orchestrator handle is opened `read_only=True`
([sql.py:124-132](../../orchestrator/app/engines/sql.py#L124-L132),
[schema_cache.py:40-47](../../orchestrator/app/core/schema_cache.py#L40-L47),
[health.py:54-56](../../orchestrator/app/health.py#L54-L56)) — a convention with no enforcement in the
compose file. `SYNC_INTERVAL_MINUTES: "30"` ([:313](../../docker-compose.yml#L313)) sets the write cadence.

**Complexity hotspots** — None (declarative). The `vllm` block
([:28-101](../../docker-compose.yml#L28-L101)) is 74 lines, 39 of them comment.

**Findings** — `SEC-01` (every app and model port published on 0.0.0.0:
[:86](../../docker-compose.yml#L86), [:133](../../docker-compose.yml#L133),
[:171](../../docker-compose.yml#L171), [:202](../../docker-compose.yml#L202),
[:272-273](../../docker-compose.yml#L272-L273), [:351-352](../../docker-compose.yml#L351-L352)),
`COST-01` (unauthenticated vLLM ports allow unmetered GPU consumption), `REL-02`
(sync-worker has no `restart:` and no healthcheck, unlike
[:89,134,172,203,343](../../docker-compose.yml#L89)), `SEC-06` (the sync-worker comment block at
[:294-295,327](../../docker-compose.yml#L294) still references AWS Secrets Manager, removed from the
code on 2026-07-28), `DX-02` (`MOCK_MODE` is not forwarded and not documented here either).

---

## `orchestrator/Dockerfile`

**Purpose** — Builds the FastAPI orchestrator on the NGC vLLM base so the CUDA torch stack the lazy
reranker needs is already present. 52 LOC.

**Public surface**

| Directive | Value | `file:line` |
|---|---|---|
| `FROM` | `nvcr.io/nvidia/vllm:26.05-py3` | [:14](../../orchestrator/Dockerfile#L14) |
| `ENV` | `DEBIAN_FRONTEND`, `PYTHONUNBUFFERED=1`, `PIP_NO_CACHE_DIR=1` | [:16-18](../../orchestrator/Dockerfile#L16-L18) |
| `RUN apt-get` | pandoc + Pango/Cairo/GDK-PixBuf/harfbuzz/ffi + DejaVu & Liberation fonts | [:24-36](../../orchestrator/Dockerfile#L24-L36) |
| `COPY` / `RUN pip` | `requirements.txt` then `pip install -r` | [:40-41](../../orchestrator/Dockerfile#L40-L41) |
| `COPY` | `app ./app` | [:43](../../orchestrator/Dockerfile#L43) |
| `ENV` | `REPORTS_DIR=/reports`, `DUCKDB_PATH=/data/warehouse.duckdb`, `LANCEDB_DIR=/data/lancedb` | [:46-48](../../orchestrator/Dockerfile#L46-L48) |
| `EXPOSE` / `CMD` | `8080`; `uvicorn app.main:app --host 0.0.0.0 --port 8080` | [:51-52](../../orchestrator/Dockerfile#L51-L52) |

**Control flow** — Pull/reuse the NGC base (chosen because it is already on the box, so "the build
downloads nothing" [:4-6](../../orchestrator/Dockerfile#L4-L6)) → install pandoc and the WeasyPrint
native stack, wiping apt lists in the same layer [:24-36](../../orchestrator/Dockerfile#L24-L36) →
`COPY requirements.txt` before `COPY app` so the pip layer caches across app edits → create `/reports`
and `/data` [:49](../../orchestrator/Dockerfile#L49) (both bind-mounted over at runtime) → run uvicorn.

**State & side effects** — Build-time egress to the apt mirrors [:24](../../orchestrator/Dockerfile#L24)
and PyPI [:41](../../orchestrator/Dockerfile#L41). Creates `/reports` and `/data`.

**Dependencies** — Inbound: [docker-compose.yml:218](../../docker-compose.yml#L218). Outbound:
`nvcr.io/nvidia/vllm:26.05-py3`, apt, PyPI,
[`requirements.txt`](../../orchestrator/requirements.txt), `app/`.

**Config** — Sets `REPORTS_DIR`, `DUCKDB_PATH`, `LANCEDB_DIR` as image defaults
([:46-48](../../orchestrator/Dockerfile#L46-L48)); all three are re-set by compose
([:245-248](../../docker-compose.yml#L245-L248)).

**Failure modes**
- **No `USER` directive**, so the container inherits the base image's user while holding `data:/data`
  read-write and the shared `hf-cache`. (NGC CUDA images ship as `root` — **UNVERIFIED — not read**,
  the base image was not inspected.)
- No `HEALTHCHECK`; no pinned base digest — `26.05-py3` is a mutable tag.
- `pip install` with only `>=` constraints and no lockfile (see below), so two builds a week apart
  install different dependency versions with nothing to diff.
- A pandoc or WeasyPrint failure is not detected at build time — there is no smoke step.

**Concurrency** — `CMD` starts **one** uvicorn worker ([:52](../../orchestrator/Dockerfile#L52)) with
no `--workers`, no `--proxy-headers`, no `--timeout-keep-alive`. All concurrency is in-process, so a
blocking call inside an `async def` stalls every request — a class of bug this project has already hit
([`CHANGELOG.md:98-104`](../../CHANGELOG.md#L98-L104)) and still carries at
[sql.py:201,206](../../orchestrator/app/engines/sql.py#L201).

**Complexity hotspots** — None; the longest instruction is the 13-line `apt-get`
([:24-36](../../orchestrator/Dockerfile#L24-L36)).

**Findings** — `DX-01` ([`orchestrator/requirements.txt`](../../orchestrator/requirements.txt) pins
**every** package with an unbounded `>=` and there is no lockfile anywhere;
`argon2-cffi` and `itsdangerous` [:17-19](../../orchestrator/requirements.txt#L17-L19) are still
installed and still commented as "V2 auth" although login was removed), `PERF-01` (single worker
amplifies the blocking-DuckDB defect), `SEC-01` (binds `0.0.0.0`).

---

## `sync-worker/Dockerfile`

**Purpose** — Builds the sync worker on `python:3.11-slim`, runs it as a non-root user, and sets the
`/data`-based defaults. 34 LOC.

**Public surface** — `FROM python:3.11-slim` [:3](../../sync-worker/Dockerfile#L3);
four `PYTHON*`/`PIP_*` env vars [:5-8](../../sync-worker/Dockerfile#L5-L8);
`COPY requirements.txt` + `pip install` [:13-14](../../sync-worker/Dockerfile#L13-L14);
`COPY config.yaml` [:16](../../sync-worker/Dockerfile#L16) and `COPY syncworker`
[:17](../../sync-worker/Dockerfile#L17); `useradd --uid 10001 worker` + `chown` + `USER worker`
[:20-23](../../sync-worker/Dockerfile#L20-L23);
`CMD ["python","-m","syncworker.main"]` [:34](../../sync-worker/Dockerfile#L34).

**Control flow** — Standard layered build: deps layer first so source changes do not invalidate the
pip cache, then source, then user creation, then the env defaults
[:26-32](../../sync-worker/Dockerfile#L26-L32).

**State & side effects** — Build-time `pip install` from PyPI
([:14](../../sync-worker/Dockerfile#L14)) — the one network dependency of the build, which sits
awkwardly against the "fully local" framing. Creates and chowns `/data`
([:21-22](../../sync-worker/Dockerfile#L21-L22)).

**Dependencies** — Inbound: [docker-compose.yml:292](../../docker-compose.yml#L292). Outbound:
`python:3.11-slim`, PyPI, [`sync-worker/requirements.txt`](../../sync-worker/requirements.txt).

**Config** — Image defaults, all overridden by compose: `SYNC_INTERVAL_MINUTES=30`
[:26](../../sync-worker/Dockerfile#L26), `PARQUET_DIR` [:27](../../sync-worker/Dockerfile#L27),
`DUCKDB_PATH` [:28](../../sync-worker/Dockerfile#L28), `LANCEDB_DIR`
[:29](../../sync-worker/Dockerfile#L29), `EMBED_VIA` [:30](../../sync-worker/Dockerfile#L30),
`EMBED_MODEL` [:31](../../sync-worker/Dockerfile#L31), `SYNC_CONFIG_PATH`
[:32](../../sync-worker/Dockerfile#L32).

**Failure modes**
- **No `HEALTHCHECK`.** With no `restart:` on the compose service
  ([:291-331](../../docker-compose.yml#L291-L331)), a hung or crashed worker is invisible to Docker.
- `tests/`, `conftest.py` and `requirements-dev.txt` are not copied, so the image cannot self-test;
  [`README.md:301`](../../README.md#L301) works around it by bind-mounting the tests back in.
- The image runs as uid 10001 but `/data` is a **named volume**
  ([docker-compose.yml:319](../../docker-compose.yml#L319)) whose first-creation ownership is taken
  from the image's `/data`. This works today; a pre-existing root-owned `data` volume would make every
  Parquet/DuckDB/LanceDB write fail with `PermissionError`.
- No multi-stage build, so the final image carries pip and the whole build context.
- The comment at [:12](../../sync-worker/Dockerfile#L12) asserts every runtime dep ships a manylinux
  aarch64 wheel — plausible on this box but **UNVERIFIED — not read**: there is no wheel manifest in
  the repo.

**Concurrency** — n/a (build-time artefact). The process it starts is single-threaded.

**Complexity hotspots** — None.

**Findings** — `REL-02`. `DX-01` does **not** apply here:
[`sync-worker/requirements.txt:3-10`](../../sync-worker/requirements.txt#L3-L10) caps every major
(`httpx>=0.27,<1`, `PyJWT>=2.8,<3`, `cryptography>=42,<47`, `duckdb>=1.0,<2`, `pyarrow>=16,<22`,
`pandas>=2.2,<3`, `PyYAML>=6.0,<7`, `lancedb>=0.8,<1`) — but there is still no lockfile and no hashes,
and `lancedb>=0.8,<1` spans a large API-churn window whose breakage
[main.py:177-184](../../sync-worker/syncworker/main.py#L177-L184) would swallow. `boto3` is absent,
consistent with the AWS removal.

---

## `frontend/Dockerfile`

**Purpose** — Multi-stage arm64 build producing a Next.js standalone server image. 30 LOC.

**Public surface**

| Stage | Directives | `file:line` |
|---|---|---|
| `deps` | `FROM node:20-alpine`; `COPY package.json package-lock.json`; `npm ci --no-audit --no-fund` | [:4-7](../../frontend/Dockerfile#L4-L7) |
| `build` | `FROM node:20-alpine`; `NEXT_TELEMETRY_DISABLED=1`; copy `node_modules`; `COPY . .`; `npm run build` | [:10-15](../../frontend/Dockerfile#L10-L15) |
| `run` | `FROM node:20-alpine`; `NODE_ENV=production`, `PORT=3000`, `HOSTNAME=0.0.0.0`; `addgroup/adduser nextjs`; copy `public`, `.next/standalone`, `.next/static`; `USER nextjs`; `EXPOSE 3000`; `CMD ["node","server.js"]` | [:18-30](../../frontend/Dockerfile#L18-L30) |

**Control flow** — `deps` installs from the lockfile → `build` compiles with the Next.js standalone
output → `run` copies only the standalone bundle, drops to the unprivileged `nextjs` user
([:28](../../frontend/Dockerfile#L28)) and starts `server.js`.

**State & side effects** — Build-time egress to the npm registry ([:7](../../frontend/Dockerfile#L7)).
Runtime: binds `0.0.0.0:3000` ([:22-23](../../frontend/Dockerfile#L22-L23)).

**Dependencies** — Inbound: [docker-compose.yml:347](../../docker-compose.yml#L347). Outbound:
`node:20-alpine`, the npm registry, `package-lock.json`.

**Config** — Sets `NODE_ENV`, `NEXT_TELEMETRY_DISABLED`, `PORT`, `HOSTNAME`
([:20-23](../../frontend/Dockerfile#L20-L23)). Runtime `ORCHESTRATOR_URL` and `NEXT_PUBLIC_APP_NAME`
come from compose ([:349-350](../../docker-compose.yml#L349-L350)).

**Failure modes** — `node:20-alpine` is a mutable tag with no digest pin. No `HEALTHCHECK`, which is
why the compose `depends_on` can only use `service_started`
([:355](../../docker-compose.yml#L355)). `npm ci` ([:7](../../frontend/Dockerfile#L7)) is the only
lockfile-driven install in the monorepo — a genuine strength relative to both Python services.

**Concurrency** — One Node process; Next.js handles requests on its own event loop. No clustering.

**Complexity hotspots** — None.

**Findings** — `SEC-01` (published on 0.0.0.0 at [docker-compose.yml:351-352](../../docker-compose.yml#L351-L352);
[`README.md:361-363`](../../README.md#L361-L363) names only this port and omits 8080 and 8000-8003).
This is the only image of the three that both runs non-root **and** installs from a lockfile.

---

## `.dockerignore` files

**Purpose** — Trim each build context before it is sent to the daemon.

**Public surface**

| File | Patterns | LOC |
|---|---|---|
| [`orchestrator/.dockerignore`](../../orchestrator/.dockerignore) | `.venv/`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `tests/` | 5 |
| [`frontend/.dockerignore`](../../frontend/.dockerignore) | `node_modules`, `.next`, `.git`, `Dockerfile`, `.dockerignore`, `npm-debug.log`, `tests`, `*.md` | 8 |
| `sync-worker/.dockerignore` | **does not exist** (verified by `ls`) | — |

**Control flow** — Each pattern is excluded from the context tarball before the `COPY` instructions
run.

**State & side effects** — None at runtime.

**Dependencies** — Inbound: `docker build` for
[docker-compose.yml:218](../../docker-compose.yml#L218) and
[:347](../../docker-compose.yml#L347). Outbound: none.

**Config** — None.

**Failure modes**
- `tests/` is excluded from the orchestrator image
  ([`orchestrator/.dockerignore:5`](../../orchestrator/.dockerignore#L5)), so the suite cannot be run
  inside the built container; [`README.md:298`](../../README.md#L298) is therefore host-only.
- Neither file excludes `.env`, `secrets/`, `*.pem`, `*.key` or `*.sqlite3`. Currently harmless because
  the orchestrator Dockerfile copies only `requirements.txt` and `app/`
  ([:40,43](../../orchestrator/Dockerfile#L40)) — but `frontend/Dockerfile` does `COPY . .`
  ([:14](../../frontend/Dockerfile#L14)), so anything added under `frontend/` outside the ignore list
  ships into the build stage.
- **`sync-worker` has no `.dockerignore` at all**; it is protected only by its Dockerfile copying just
  `requirements.txt`, `config.yaml` and `syncworker/`
  ([:13,16,17](../../sync-worker/Dockerfile#L13)) — which means the untracked
  `sync-worker/config.yaml.bak` and the local `.venv/` are still sent to the daemon as build context
  on every build.

**Concurrency** — n/a.

**Complexity hotspots** — n/a.

**Findings** — `SEC-04`-adjacent: the ignore lists do not defend against secret material entering a
build context; only the narrow `COPY` lines do.

---

## `searxng/settings.yml`

**Purpose** — SearXNG configuration for the self-hosted metasearch behind the web-search path; enables
the JSON output format the orchestrator's `SearxngProvider` consumes. 56 LOC.

**Public surface**

| Key | Value | `file:line` |
|---|---|---|
| `use_default_settings` | `true` | [:9](../../searxng/settings.yml#L9) |
| `server.secret_key` | literal `ultrasecretkey` placeholder | [:12](../../searxng/settings.yml#L12) |
| `server.limiter` | `false` | [:13](../../searxng/settings.yml#L13) |
| `server.image_proxy` | `false` | [:14](../../searxng/settings.yml#L14) |
| `search.safe_search` | `1` | [:17](../../searxng/settings.yml#L17) |
| `search.formats` | `[html, json]` | [:18-20](../../searxng/settings.yml#L18-L20) |
| `ui.static_use_hash` | `true` | [:23](../../searxng/settings.yml#L23) |
| `engines[]` | mojeek, bing, qwant, yahoo, dogpile, mwmbl, wikipedia, stackexchange, github, arxiv — all `disabled: false` | [:36-56](../../searxng/settings.yml#L36-L56) |

**Control flow**
1. The container starts and the official image's entrypoint `sed`-replaces the literal
   `ultrasecretkey` with `$SEARXNG_SECRET` (documented at [:6-8](../../searxng/settings.yml#L6-L8);
   the variable is supplied at [docker-compose.yml:339](../../docker-compose.yml#L339)).
2. `use_default_settings: true` merges these keys over the shipped defaults.
3. `search.formats` includes `json` ([:20](../../searxng/settings.yml#L20)), which
   `app/search/searxng.py` requires.
4. Ten engines are force-enabled to spread load across independent upstream quotas (rationale
   [:25-35](../../searxng/settings.yml#L25-L35), matching
   [`CHANGELOG.md:105-113`](../../CHANGELOG.md#L105-L113)).

**State & side effects** — Outbound network to ten third-party search engines. The mount is `rw`
([docker-compose.yml:342](../../docker-compose.yml#L342)) and the on-disk directory is owned by
uid/gid 977, so the container has in fact written to the host path.

**Dependencies** — Inbound: [docker-compose.yml:342](../../docker-compose.yml#L342) (bind mount) and
`orchestrator/app/search/searxng.py` via `SEARXNG_URL`
([docker-compose.yml:253](../../docker-compose.yml#L253)). Outbound: SearXNG's own default settings.

**Config** — `SEARXNG_SECRET`, consumed indirectly through the entrypoint
([:6-8](../../searxng/settings.yml#L6-L8), supplied at
[docker-compose.yml:339](../../docker-compose.yml#L339)).

**Failure modes**
- `limiter: false` ([:13](../../searxng/settings.yml#L13)) disables SearXNG's own rate limiter on the
  assumption of "a single trusted internal caller" — but with no `networks:` segmentation, **any**
  container on the project bridge is that caller.
- If `SEARXNG_SECRET` is unset, the entrypoint substitutes the compose default `please-change-me`
  rather than failing.
- Engine suspension is the real operational failure and is documented in-file
  ([:28-31](../../searxng/settings.yml#L28-L31)): when all engines suspend, search returns zero results
  and the app answers from model knowledge silently.
- **Exposure**: the service declares no `ports:` ([docker-compose.yml:336-344](../../docker-compose.yml#L336-L344)),
  so it is reachable only at `http://searxng:8080` on the project bridge, and only under
  `--profile search`. This is the one service that is *not* part of `SEC-01`.

**Concurrency** — n/a (declarative).

**Complexity hotspots** — n/a.

**Findings** — `SEC-05` (this is the ingress for untrusted web text that reaches the model prompt with
no instruction-stripping or provenance tainting). The placeholder secret is checked into the working
tree ([:12](../../searxng/settings.yml#L12)) and correctness depends entirely on the upstream image's
`sed`, which is asserted by a comment and by no test.

---

## `docker-compose.yml.bak-preperf`

**Purpose** — Pre-performance-tuning snapshot of the compose file, left on disk (13,446 bytes, 335
LOC). Untracked — `.gitignore:47` (`*.bak-*`) matches it.

**Public surface** — Identical service/volume/port surface to the live file: same 7 services, same six
published ports, same volumes, same healthchecks, same `depends_on`.

**Control flow** — Identical to the live file.

**Diff vs. `docker-compose.yml` — the complete set of differences**
1. The `vllm` `command` stops after `--reasoning-parser qwen3`
   (`docker-compose.yml.bak-preperf:50-58`). The live file adds seven flags:
   `--trust-remote-code`, `--quantization modelopt`, `--attention-backend flashinfer`,
   `--moe-backend marlin`, `--enable-chunked-prefill`, `--enable-prefix-caching`,
   `--max-num-batched-tokens 8192` ([docker-compose.yml:72-78](../../docker-compose.yml#L72-L78)).
2. The live file adds the 12-line performance rationale comment
   ([:51-62](../../docker-compose.yml#L51-L62)).
3. **Nothing else differs** — same image tags, same `--gpu-memory-utilization` values
   (0.35 / 0.11 / 0.14 / 0.04 at `:56, 106, 143, 175`), same `--max-model-len`, same ports, same GPU
   reservations. The 355 − 335 = 20-line delta is entirely accounted for by (1) and (2).

**State & side effects** — None; nothing references it.

**Dependencies** — Inbound: none. Outbound: none.

**Config** — Identical `${VAR}` set to the live file.

**Failure modes** — It is a stale duplicate of a security-relevant file with **no marker saying it is
obsolete**. An operator who runs `docker compose -f docker-compose.yml.bak-preperf up` gets the same
all-interfaces port publishing with none of the performance flags and the same 0.53 budget.

**Concurrency** — n/a.

**Complexity hotspots** — n/a.

**Findings** — `SEC-04`-adjacent: it is one of four `.bak*` artefacts sitting in the working tree
alongside `.env.bak-205921` (553 bytes, mode 600), `searxng/settings.yml.bak` and
`sync-worker/config.yaml.bak`. `.gitignore:46-49` keeps all four out of git — the pattern works — but
they remain on disk, and only the `.env` copy is mode-restricted.

---

## Infrastructure-level findings

| ID | Where it lands in this file |
|---|---|
| `SEC-01` (P0) | [:86](../../docker-compose.yml#L86), [:133](../../docker-compose.yml#L133), [:171](../../docker-compose.yml#L171), [:202](../../docker-compose.yml#L202), [:272-273](../../docker-compose.yml#L272-L273), [:351-352](../../docker-compose.yml#L351-L352) — six ports on 0.0.0.0, no `networks:`, no auth in front of any of them |
| `COST-01` (P2) | The four vLLM ports accept OpenAI-compatible requests with `OPENAI_API_KEY: local-no-key` ([:230](../../docker-compose.yml#L230)) — unmetered GPU on an open port |
| `REL-02` (P2) | `sync-worker` [:291-331](../../docker-compose.yml#L291-L331): no `restart:`, no healthcheck; `orchestrator` and `frontend` share the missing `restart:` |
| `SEC-04` (P2) | Four `.bak*` files in the working tree, including `.env.bak-205921` |
| `SEC-05` (P2) | SearXNG is the untrusted-text ingress ([:251-253](../../docker-compose.yml#L251-L253), [`searxng/settings.yml`](../../searxng/settings.yml)) |
| `SEC-06` (P2) | Stale AWS Secrets Manager references at [:294-295,327](../../docker-compose.yml#L294) |
| `DX-01` (P2) | `orchestrator/requirements.txt` unbounded `>=`, no lockfile; sync-worker caps majors; frontend uses `npm ci` |
| `DX-02` (P3) | `MOCK_MODE` reaches no container and appears in no template |
| `PERF-01` (P1) | One uvicorn worker ([`orchestrator/Dockerfile:52`](../../orchestrator/Dockerfile#L52)) makes the blocking-DuckDB call in `async def` a whole-process stall |
| `TEST-01` (P1) | No `.github/`, no `.gitlab-ci.yml`, no Jenkinsfile — nothing builds or validates any of these files automatically |
</content>
