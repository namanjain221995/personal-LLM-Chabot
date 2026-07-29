# TechSara Local AI Analysis Platform

A ChatGPT-style assistant for a Salesforce org that runs **entirely on one machine**.
No cloud LLM APIs, no accounts, no data leaving the box except the Salesforce API
calls it makes on your behalf.

Built for an NVIDIA DGX Spark (GB10 Grace Blackwell, aarch64, 121 GB **unified**
memory — CPU and GPU share one pool).

---

## What it does

Ask a question in plain English. The platform decides where the answer should come
from and says which source it used.

| Source | When | What you get |
|---|---|---|
| **DuckDB warehouse** | most questions | The model writes SQL; the database computes the numbers. The SQL is shown. |
| **RAG (LanceDB)** | questions about record *content* | Semantic search over case notes, descriptions, feedback — answers cite real records as clickable links. |
| **Live Salesforce** | data newer than the last sync, or an object that was never synced | Model-written SOQL over the REST API, guarded and row-capped. |
| **Web search** | only with the Salesforce toggle **off** | Self-hosted SearXNG, cited `[n]` sources. |

Plus: image and PDF reading, dataset/ZIP profiling, Word/PDF report generation,
Mermaid diagrams, charts, CSV export.

### Trust rules, non-negotiable

- Salesforce access is **read-only**, enforced by the integration user's permissions
  and again by a query guard.
- Every number traces to a **query you can read** in the UI.
- Every RAG claim **cites record IDs** that link into Salesforce.
- **Model-generated code is never executed.** SQL and SOQL are parsed and validated;
  uploaded files are profiled, never run. `.pkl` is refused outright because reading
  one executes code.
- **No cloud LLM APIs.** Every model runs locally on vLLM.
- With the Salesforce toggle **on**, the web is never used — at any effort level.

---

## Architecture

```
                       ┌─────────────────────────────────────────┐
  browser :3000  ───▶  │ frontend  (Next.js 15, React 19)        │
                       └───────────────┬─────────────────────────┘
                                       │  /api/* proxy
                       ┌───────────────▼─────────────────────────┐
                       │ orchestrator :8080  (FastAPI, SSE)      │
                       │  router → engine → streamed answer      │
                       └─┬────────┬────────┬───────────┬─────────┘
                         │        │        │           │
        ┌────────────────▼┐ ┌─────▼─────┐ ┌▼─────────┐ ┌▼──────────────┐
        │ DuckDB warehouse│ │ LanceDB   │ │ SearXNG  │ │ Salesforce    │
        │ (synced copy)   │ │ (RAG)     │ │ (web)    │ │ REST (live)   │
        └────────▲────────┘ └─────▲─────┘ └──────────┘ └───────────────┘
                 │                │
        ┌────────┴────────────────┴────────┐
        │ sync-worker — every 30 minutes   │
        └──────────────────────────────────┘

  models (vLLM, OpenAI-compatible):
    :8000  Qwen3.6-35B-A3B-NVFP4        main — chat, SQL, RAG, vision, synthesis
    :8002  Qwen3-VL-8B-Instruct-FP8     routing, classification, query rewriting
    :8003  Qwen3-Embedding-0.6B         embeddings for RAG and recall
```

### Services

| Service | Port | Purpose |
|---|---|---|
| `frontend` | 3000 | The web UI. The only port you open. |
| `orchestrator` | 8080 | Routing, engines, SSE streaming, history |
| `vllm` | 8000 | Main model, 262,144-token context |
| `vllm-router` | 8002 | Small model for cheap decisions |
| `vllm-embed` | 8003 | Embeddings |
| `sync-worker` | — | Salesforce → DuckDB + LanceDB, every 30 min |
| `searxng` | — | Self-hosted web search (internal only) |

---

## Quick start

```bash
cp .env.example .env        # then fill it in — see Configuration
docker compose up -d
```

First start downloads model weights and can take 20+ minutes. Watch progress:

```bash
docker compose logs -f vllm
curl -s localhost:8080/health | python3 -m json.tool
```

Then open **http://localhost:3000**. There is no login.

---

## Configuration (`.env`)

```ini
# --- Salesforce (read-only integration user) -------------------------------
SF_CLIENT_ID=<connected app Consumer Key>
SF_CLIENT_SECRET=<connected app Consumer Secret>
SF_USERNAME=integration.user@example.com
# MUST be your My Domain URL. Salesforce REFUSES the client-credentials grant
# on login.salesforce.com with "request not supported on this domain".
SF_LOGIN_URL=https://yourorg.my.salesforce.com

# --- Models ----------------------------------------------------------------
HF_TOKEN=<hugging face token, for weight downloads>
MODEL_MAX_CONTEXT=262144        # fallback only; the real window is probed live
MODEL_MAX_OUTPUT=8192

# --- Web search (used only when the Salesforce toggle is OFF) --------------
SEARCH_ENABLED=true
SEARCH_PROVIDER=searxng
SEARXNG_URL=http://searxng:8080
SEARXNG_SECRET=<random 64 hex chars>
SEARCH_MAX_RESULTS=10

# --- Sync behaviour (optional) --------------------------------------------
SYNC_AUTO_FIELDS=true           # adopt fields added in Salesforce automatically
SYNC_MAX_FIELDS=80              # ceiling per object
SYNC_REPORT_NEW_OBJECTS=true    # log objects that exist but are not synced
```

### Salesforce authentication

Two grants are supported. **Client credentials** is simplest and needs no key:

```ini
SF_CLIENT_ID=...
SF_CLIENT_SECRET=...
SF_LOGIN_URL=https://yourorg.my.salesforce.com
```

The connected app must have a **Run As** user set for the client-credentials flow.

**JWT bearer** is also supported, if you prefer certificate auth:

```ini
SF_PRIVATE_KEY_HOST_FILE=./secrets/sf_jwt_key.pem   # mounted read-only
# or SF_PRIVATE_KEY_B64=<base64 of the PEM>
```

> The private key is the `-----BEGIN PRIVATE KEY-----` file (~1,700 bytes) that
> pairs with the certificate uploaded to the connected app. The 64-character hex
> string Salesforce shows next to that certificate is its **thumbprint** — a
> fingerprint, not a key. It cannot sign anything, and the app rejects it with a
> message saying so.

---

## How the assistant decides what to do

### The Salesforce toggle

**On** — answers come from your org: the warehouse, the RAG index, or a live query.
The web is never touched, at any level.

**Off** — a general assistant: coding, writing, diagrams, web search. It cannot
read your CRM data at all; the toggle is a hard gate, not a hint.

### The effort levels

One model, four levels. "Fast" is fast because the reasoning pass is switched
off, not because it is a smaller model.

| Level | Reasoning | Searches | Plan steps | Answer room |
|---|---|---|---|---|
| Fast | no | none | no | 8k |
| Low | no | up to 2 | no | 8k |
| Medium | yes | up to 3 | up to 5 | 8k |
| High | yes | up to 6 | up to 8 | 16k |

High is guaranteed to do at least what Medium does. There is no Agent switch —
the small model classifies each message and escalates when the work needs it,
and each level is a ceiling it can narrow but never exceed.

---

## Managing which Salesforce data is synced

Objects and fields live in `sync-worker/config.yaml`, mounted live — a restart
applies changes, no rebuild.

```bash
# what is configured
docker compose exec sync-worker python3 -m syncworker.objects list

# add an object (Id and SystemModstamp are added automatically)
docker compose exec sync-worker python3 -m syncworker.objects \
  add Project__c --fields Name,Status__c,AccountId --rag-fields Notes__c

# add fields to an existing object (merges, does not replace)
docker compose exec sync-worker python3 -m syncworker.objects \
  add-fields Case --fields Priority,Origin

# import an org export, keeping only what this user can actually read
docker compose exec sync-worker python3 -m syncworker.objects \
  import-sheet /tmp/org.csv --dry-run

docker compose up -d --force-recreate sync-worker
```

**New fields added in Salesforce are adopted automatically** on the next cycle,
and long-text ones join the RAG index. **New objects are reported, not adopted** —
a new object means a full extract of something nobody asked for, so it is logged
for you to decide.

### The field dictionary

An org export (`Object API Name, Object Label, Field API Name, Field Label,
Field Type`) teaches the model what your users *call* things versus what the API
calls them:

```bash
docker compose cp org-export.xlsx orchestrator:/tmp/org.xlsx
docker compose exec orchestrator python3 -m app.core.sf_dictionary /tmp/org.xlsx
```

This matters more than it looks. Without it the model guesses field names, and a
plausible-but-wrong guess like `Status__c` (when the field is
`Interview_Status__c`) does not error — it returns no rows, and the answer is
confidently zero.

---

## Operations

```bash
# health of every dependency
curl -s localhost:8080/health | python3 -m json.tool

# what the sync is doing
docker compose logs -f sync-worker | grep cycle_done

# rebuild and restart one service
docker compose up -d --no-deps --build orchestrator

# full refresh (models reload — allow ~10 minutes)
docker compose up -d --force-recreate
```

> **Do not run `docker compose pull`.** `vllm/vllm-openai:nightly` is a moving
> tag; the local image is the specific nightly that loads this NVFP4 checkpoint.
> Pulling a newer one can silently break model loading.

### Memory budget

GB10 has **unified memory** — `--gpu-memory-utilization` fractions come out of
the same 121 GB pool as the OS and every container:

```
0.35  vllm         (22 GB weights + ~10.5 GB KV at 262144, fp8)
0.14  vllm-router  (10 GB weights + ~4.7 GB KV at 65536)
0.04  vllm-embed
────
0.53  ≈ 88 GB used, ~33 GB free
```

Over-reserving took this box to **0 GB available** once. Keep the sum ≲ 0.6.
Running a model "on CPU" saves nothing here — same pool, 10–50× slower.

---

## Testing

```bash
cd orchestrator && python3 -m pytest tests/ -q          # 637 tests
cd frontend && npx vitest run                            # 200 tests
docker compose run --rm --no-deps \
  -v "$PWD/sync-worker/tests:/app/tests:ro" sync-worker \
  sh -c "pip install -q pytest && cd /app && python3 -m pytest tests/ -q"   # 104 tests
```

The suites are written around failures that actually happened, and the docstrings
say what broke. A few worth knowing about, because they will bite anyone
extending this:

- **Qwen3.6 accepts exactly one system message, at index 0.** This app injects
  several (engine prompt, rolling summary, semantic recall, search sources).
  `llm.normalize_system` folds them; without it every request 400s.
- **`sse_event()` raises on an unlisted event type, from inside the stream.** Add
  an `emit("newthing", …)` without registering the name and the answer dies
  mid-stream with no error event.
- **Salesforce checkboxes are the TEXT `'true'`/`'false'` in DuckDB.**
  `WHERE IsWon = 'True'` matches nothing and answers "0" with confidence.
- **Reasoning is drawn from the same budget as the answer.** A small `max_tokens`
  with thinking on returns an empty answer, not a short one.
- **trafilatura is not thread-safe.** Extraction runs on a dedicated
  single-worker executor; `asyncio.to_thread` can abort the interpreter.

---

## Repository layout

```
orchestrator/          FastAPI backend
  app/engines/         sql, rag, vision, report, chat, agent, search, live_sf, …
  app/core/            guards, schema cache, Salesforce REST, field dictionary
  app/context.py       token budgeting against the live model window
  app/compaction.py    rolling summaries so long chats keep working
frontend/              Next.js UI
  components/          composer, message row, research panel, context meter
  lib/                 SSE parsing, streams, history, prefs
sync-worker/           Salesforce → DuckDB + LanceDB
  config.yaml          which objects and fields are synced
searxng/               self-hosted web search config
```

---

## Known limitations

- **Field-level security caps what can be read.** The integration user sees only
  the fields granted to it — on this org, 52 of `Interview__c`'s 259. Everything
  else is invisible to the platform until FLS is granted.
- **There is no authentication.** Anyone who can reach port 3000 can read every
  conversation and query the Salesforce data. Compose publishes on `0.0.0.0`;
  bind to `127.0.0.1:3000:3000` if the network is not fully trusted.
- **The warehouse is up to 30 minutes stale.** Ask for "live" data explicitly to
  bypass it.
- Charts render only when you ask for one ("chart", "graph", "plot").
