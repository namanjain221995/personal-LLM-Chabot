# Phase A discovery: request path, configuration and symptoms

- **Date:** 2026-10-03.
- **Describes:** `autopilot/dev` at **6ae978a3**. This document was written on `upgrade/a/discovery`.
- **Scope:** MASTER_PROMPT §10 (discovery), with §13, §14.1, §18 and §21 where they bear on the request path.
- **Method:** reading code, configuration, tests, docs and git history only.
  - Nothing was run against an engine, a stack or a browser. Every test state here is `TEST_NOT_RUN`.
  - `.env`, `.runtime/` and host files were not read, so every production value below is unverified unless its label says otherwise.
  - "Working" in this document means implemented and wired in code. It never means observed working at runtime.

**Evidence labels.** Every claim carries one of these.

| Label | Meaning |
|---|---|
| **VIC** (VERIFIED-IN-CODE) | The mechanism exists at the cited `path:line`. This says nothing about how often it fires or what value production uses. |
| **FDU** (FROM-DOCS-UNVERIFIED) | Taken from a doc or a code comment and not checked. |
| **HYP** (HYPOTHESIS) | Needs a measurement before it can be relied on. |

**Paths.** `app/` stands for `orchestrator/app/`. All other paths are relative to the repository root.

**Citation check.** About 60 citations were opened and compared with the code at 6ae978a3. Six were corrected: the `fit_request` return (now `app/context.py:1028-1030`), the p50/p95 pre-pass comment (`app/main.py:6196-6199`), the second-tenant note (`app/admission.py:54-55`), the Max-loop dispatch (`app/engines/chat.py:589-641`), AnswerGuard (`app/engines/chat.py:838`) and controller staging (`launcher/techsara_cli/environment.py:629`). The key list of the compose `environment:` block was also corrected (§3.1).

---

## 1. Inventory (A-01)

### 1.1 Repository instructions read

| Source | What it says | Label |
|---|---|---|
| `CLAUDE.md` | The programme text is `docs/ai-platform-upgrade/MASTER_PROMPT.md`, and its §0 wins. The file also sets the hard limits, says to work in worktrees, to check the autopilot lock or PAUSE, to keep the public repo free of host data, to start dev stacks with `--env-file ops/dev/stack.vars`, and to give tests a `TEST_DATABASE_URL` ending in `_test`. | VIC |
| `frontend/CLAUDE.md` → `frontend/AGENTS.md` | This is Next's generated rule ("This is NOT the Next.js you know"). It says to read `node_modules/next/dist/docs/` before writing any Next code. | VIC |
| `.claude/` | Absent from the worktree and not tracked (`git ls-files .claude` is empty). §10 lists it as a candidate location. | VIC |
| `README.md` (1,093 lines) | Repository map at `README.md:142-200`. Test commands at `README.md:943-975`. Several of its counts are stale (§6). | VIC |
| `docs/README.md` | "Updated 2026-08-11". It marks the 2026-07-31 audit layer as historical. | VIC |

### 1.2 Size and components

- **Size.** There are 4,374 tracked paths (`docs/00-INVENTORY.md:18` still says 504). The largest areas are `brain/` (2,082 files, 2,017 of them Salesforce metadata XML), `orchestrator/` (945), `frontend/` (580), `docs/` (252) and `scripts/` (70).
- **Python versions differ by place:** `.python-version` says 3.12, `orchestrator/Dockerfile.cpu:14` uses 3.11, CI sets 3.11 five times and 3.12 three times, and the launcher runs uv-managed 3.12.

| Component | Entry point(s) | Size (tracked) | Tests and CI | Deployed by | Label |
|---|---|---|---|---|---|
| orchestrator (FastAPI) | `app.main:app` via uvicorn (`orchestrator/Dockerfile.cuda:88`). The app object is at `app/main.py:1380`, and `POST /chat` is at `app/main.py:4140`. | `app/`: 297 .py, about 238k lines. `main.py` alone is 7,821 lines. | `orchestrator/tests/`: about 488 modules and about 9,375 `def test_` (static count). Needs a `_test` PostgreSQL. CI shards (`.github/workflows/pipeline.yml:680`). | Base compose builds `Dockerfile.cpu` (`compose.yaml:210-213`). The DGX and NVIDIA overlays build `Dockerfile.cuda` (`compose/compose.dgx-spark.yaml:21-22`). | VIC |
| frontend (Next `^16.3.6`, React 19) | `frontend/app/page.tsx` is a 12-line wrapper around `frontend/components/ChatApp.tsx` (4,326 lines). There are 45 `app/api/**/route.ts` proxies. Standalone server with `server-preload.cjs` (`frontend/Dockerfile:92`). | components 135 files, lib 62 | 216 Vitest files. CI runs `npm test`, eslint, tsc and `next build` (`pipeline.yml:743-768`). | `compose.yaml:443-446` | VIC |
| v1-gateway (Node 20, no dependencies) | `node server.cjs` (`gateway/Dockerfile:31`). Relays `/v1` only (`gateway/server.cjs:6-9`). | `lib/` 8 files, about 3k lines | `gateway/test/` has 8 files, but **`pipeline.yml` never references them**. Its Dockerfile is not in the aarch64 gate (`pipeline.yml:1279-1283`). | `compose.yaml:573-576` | VIC (HYP: nothing else runs these tests) |
| knowledge-service | `uvicorn app:app`. Mounts a vendored `graphrag` (`knowledge-service/compose.fragment.yaml:33-37`). | 5 files | none | **No compose file deploys it.** `compose.fragment.yaml:1` says "Add this service to docker-compose.yml". The only caller is `evaluation/runners/discovery_runner.py:35`. | VIC |
| launcher (`techsara`) | `python -m techsara_cli`. Subcommands at `launcher/techsara_cli/cli.py:2943-2977`. | 13 .py files, 8,610 lines | 21 files, 695 `def test_`. The CI floor is 450 executed (`pipeline.yml:395`). | Not containerised. Generates `.runtime/generated.env`. | VIC |
| compose | `compose.yaml` plus 17 overlays in `compose/`. The project name comes from `TECHSARA_STACK` (`compose.yaml:10`). | 25 files | `compose/voice-store/tests` runs in CI (`pipeline.yml:704-717`). | Selected by the launcher (§3.1). `docker-compose.yml` is superseded and kept for rollback (`docker-compose.yml:2-16`). | VIC |
| config | `config/hardware-profiles.yaml`, `config/model-manifest.yaml` | 3 files | through the launcher tests | read by the launcher | VIC |
| monitoring | Prometheus rules, 8 dashboards, exporters. The engine-controller (`monitoring/engine-controller/controller.py`, 3,330 lines) is staged by `launcher/techsara_cli/environment.py:629`. | 41 files | Only `monitoring/developer-api/tests` is gated (`pipeline.yml:428-434`). The controller tests and the promtool tests are not referenced. | monitoring overlays | VIC |
| sync-worker | `python -m syncworker.main` (`sync-worker/Dockerfile:45`) | 15 modules | 252 tests, in CI (`pipeline.yml:735-736`) | `compose.yaml:408-411` | VIC |
| evaluation, conformance, benchmarks, tools | Salesforce discovery eval; `/v1` OpenAI-SDK conformance; ASR benchmark; API soak | small | not in `pipeline.yml` | run by hand | VIC |
| e2e | `e2e/platform/run.js`, `e2e/ci/stack.sh` | 29 files | `e2e-hosted` job (`pipeline.yml:1608-1723`) | CI stack | VIC |
| brain | 21 YAML packs read by `app/core/brain.py`; vendored `graphrag`. The generated bundle is gitignored (`.gitignore:27`). | 2,082 files | the graphrag test is not in CI | packs bind-mounted (FDU) | VIC |
| scripts, ops | cluster, deploy, host-guard and service scripts; `scripts/aiq/` quality harness; `ops/autopilot/`, `ops/deploy/merge_to_dev.sh` | 70 + 16 files | shell parse check plus ruff (`pipeline.yml:181-186`). `ops/autopilot/tests` is not in CI. | The deploy job runs on main only (`pipeline.yml:2375-2384`). | VIC |
| `src/personal_llm_chabot` | a 2-line uv-init stub, declared by the root `pyproject.toml` | 1 file | none | none: dead scaffold | VIC |

**§10 candidate locations.** All of them exist except `.claude/`. That includes `app/{main,config,llm,context,compaction}.py`, `app/{engines,core,publicapi}/`, and every top-level directory named in §10 (VIC).

Context work also touches these files: `app/kv_budget.py`, `app/summarize.py`, `app/history.py`, `app/recall.py`, `app/continuation.py`, `app/fast_lane.py`, `app/admission.py` and `app/sse.py`.

Persistence is PostgreSQL through psycopg (`app/db.py:66`). The migrations are inline strings, up to `_MIGRATION_V44` (`app/db.py:3080`) (VIC).

**§4 reported architecture compared with the code.**

| §4 says | The code shows | Label |
|---|---|---|
| "knowledge-service (LanceDB RAG, DuckDB)" | `knowledge-service/app.py:1-12` is a read-only Salesforce-schema resolver. LanceDB and DuckDB are used inside the orchestrator (`orchestrator/requirements.txt`: `lancedb==0.37.1`, `duckdb>=1.0`) and the sync-worker. | VIC |
| "sandboxed code interpreter" | A grep of `app/` found no user-facing interpreter. `docs/ARCHITECTURE_CURRENT.md:199` lists it as "MISSING". The only sandbox found is the eval harness `scripts/aiq/code_sandbox.py`. | HYP (grep-negative only) |
| Video understanding in progress | `app/video/` (14 files) and `app/engines/video.py` exist. | VIC (code only) |

**Hygiene candidates for §27:** the dead scaffold (`src/`, root `pyproject.toml`, `uv.lock`), the stale `docs/00-INVENTORY.md`, and stray tracked binaries at the root (a 12 KB file named `→` from commit 3c6a734d, `loading.webm` and 60 `screenshots/`).

**Test suites that `pipeline.yml` never references:** `gateway/test`, `monitoring/engine-controller/tests`, `monitoring/prometheus/tests`, `evaluation/tests`, `conformance/*/selftest`, the brain graphrag tests and `ops/autopilot/tests`.

---

## 2. Request path end to end (A-02)

### 2.1 Hop table: browser → orchestrator → engine → vLLM → stream back

The `gateway/` (v1-gateway) is **not** on the browser chat path. It relays `/v1` only (`gateway/server.cjs:6-9`) (VIC). Every row below is VIC unless marked.

| # | Hop | file:line | Decision / limit |
|---|---|---|---|
| 1 | Effort chip | `frontend/components/ModelPicker.tsx:25`, `:113-118` | Offers `fast\|think\|max`. Picking one calls `onChange('smart', effort)`, so `model` is pinned to `smart`. The header comment at `:6-16` still describes four levels. |
| 2 | Prefs | `frontend/components/Composer.tsx:1535-1541`; `frontend/components/ChatApp.tsx:1905-1919`; `frontend/lib/prefs.ts:175-206` | Stored per conversation in localStorage. New chats use a draft slot. |
| 3 | Defaults and migration | `frontend/lib/prefs.ts:75-83`, `:99-112`, `:130` | Defaults: `effort:'fast'`, `model:'smart'`, `webSearch:'auto'`. `agent` is forced to false. `deepResearch` is one-shot. Legacy `low/medium/high/extra_high` map to the 3-level ladder. |
| 4 | Slash commands, send guard | `frontend/components/ChatApp.tsx:1945-1961`; `frontend/lib/slashCommands.ts:124-160` | A slash command overrides prefs for one send. There is a double-click guard. |
| 5 | `startStream()` | `frontend/lib/streams.ts:1223-1355` | Builds the body from `prefsRef`. Sends the **whole visible transcript** on every send (`:1301-1310`). If inline photos exceed 48 MiB they are pre-uploaded and sent as `image_refs` (`frontend/lib/orchestrator.ts:188`; `streams.ts:1272-1296`). Count ceiling: 999 (`orchestrator.ts:168,176`). Regenerate, edit and retry use the *current* effort (`ChatApp.tsx:2798,2996,3155`). |
| 6 | Next middleware | `frontend/middleware.ts:89-90` | Does not run on `/api/*`. Sign-in is enforced at the orchestrator (row 8). |
| 7 | `POST /api/chat` | `frontend/app/api/chat/route.ts:52`, `:296-312`, `:356-389`; `frontend/lib/orchestrator.ts:294-386` | Body capped at 128 MiB (413). Translates the body: `current_text` and `dataset` are dropped, and `message` is derived (`orchestrator.ts:282-332`). Forwards only the `cookie` header. `fetch(..., {signal: req.signal})` sets no timeout or dispatcher, so undici defaults apply. No correlation id is forwarded; `requestIdOf` is used for logs only (`route.ts:246-261`). |
| 8 | `POST /chat` | `app/main.py:4140-4161` | Returns 401 before reading the body. Then the bounded reader runs. Its cap is 128 MiB (`app/main.py:803`), overridable by `CHAT_MAX_REQUEST_BODY_BYTES`, so it can diverge from the fixed proxy cap. |
| 9 | Request defaults | `app/main.py:2675`, `:2680-2683`, `:2688`, `:2706` | Server defaults: `mode="salesforce"`, `model="smart"`, `effort="think"`, `deep_research=False`, `web_search="off"`. The browser sends every field it controls, so these defaults apply only to other callers. Whether they apply to resumed snapshots is HYP. |
| 10 | Effort canonicalisation | `app/main.py:2838-2844` → `app/llm.py:810-821` | `low→fast`, `medium/high→think`, `extra_high→max`. Unknown values become `think`. |
| 11 | Feature gate | `app/main.py:4279-4290`, `:4295-4325` | `feature_access.enforce_chat` rewrites `mode`, `web_search`, `deep_research` and `sf_live`. Attachments and video are cleared when the account may not use them. |
| 12 | Ownership, durable intent | `app/main.py:4333-4360`, `:4390+`, `:4533-4566` | Returns 404 for someone else's conversation. A V29 `chat_requests` row is written before any work starts. A new send cancels the previous generation (`replaced`). |
| 13 | Detached generation | `app/main.py:1781-1795`, `:4170-4172` | The generation runs as a detached task. Closing the SSE releases only the reader. |
| 14 | Fast flag | `app/main.py:4933`; `app/llm.py:1549-1550` | `mark_fast_turn` is a ContextVar. On a Fast turn, every main-model call sends `enable_thinking=false`. |
| 15 | History source | `app/main.py:5000`, `:2824-2836` | The history is the client's `messages` minus the trailing user turn, with in-process `memory.history` as the fallback. **The prompt history is not loaded from the database.** |
| 16 | Fast small-talk lane | `app/main.py:5025` → `app/fast_lane.py:339-387` | Applies only when effort is `fast`, mode is `assistant`, there are no attachments, the message is at most 40 characters with no digit or URL, it matches a closed lexicon, and the previous turn was answered. It skips the pre-passes and compaction. Limits: 1,024 tokens and the last 2 exchanges (`app/fast_lane.py:49,54-55`). |
| 17 | Concurrent context reads | `app/main.py:5044-5180` | Facts, cross-chat recall, keyword recall, repo keys, crawl hits, URL docs, documents, videos, uploads, summary and artifacts. Each read is gated separately. |
| 18 | Deep Research gate | `app/main.py:5184-5192` | Needs the explicit pill, `DEEP_RESEARCH_ENABLED` and `SEARCH_ENABLED` (code default **false**, `app/config.py:1172`). Also needs text, no PDF or image, and auto web search allowed. **Max alone never runs Deep Research.** |
| 19 | Search policy | `app/main.py:5212-5228` | Requires `search_enabled`, `web_search != "off"`, assistant mode and a non-lane turn. A per-user rate window is checked here and spent only when a search starts. |
| 20 | Knowledge pre-pass dispatch | `app/main.py:5276-5322` | Assistant mode, text only, no agent, no Deep Research. At Think/Max it is dispatched beside `decide()` (`CHAT_PREPASS_BESIDE_DECIDE`). |
| 21 | Auto-plan | `app/main.py:5338-5356` → `app/engines/orchestrate.py:128-176` | Fast: no router call (`ALLOWED`, `:128-132`). Think/Max: one router call with `max_tokens=40`, input clipped to 2,000 characters (head and tail) and the last 2 turns (`:79-108`, `:158-160`). Asks to transform pasted text skip the router (`:150-156`). At Max, search implies agent (`:174-175`). Errors become no agent and no search (`:162-164`). |
| 22 | want_agent / want_search | `app/main.py:5358-5395` | The `on` pill forces search. Otherwise the plan decides. On the Agent-toggle path, `should_search` is asked (`:5390`). |
| 23 | Pinned memory blocks | `app/main.py:5475-5554`, `:5715-5810` | Facts, cross-chat recall, shared pages (6,000 characters each, `:5721`), stored documents (8,000 each, `:5754`) and videos. Each is prepended as a `system` message, with no total cap. |
| 24 | Compaction | `app/main.py:5913-5936` → `compaction.prepare_deferred` / `prepare` | Signed-in user, a `conversation_id`, and not a lane turn. **No `requested_max_tokens` is passed.** |
| 25 | Salesforce intel / clarify | `app/main.py:6007-6150` | Salesforce mode only. |
| 26 | Await the knowledge pre-pass | `app/main.py:6174-6191`; `_await_knowledge` `:7499-7548` | Bounded by `KNOWLEDGE_PREPARE_DEADLINE_S` = 12 s (`app/config.py:1356`). A confident local answer (`local_first`) cancels the auto search. At Think/Max, an `escalate` verdict turns search on (`:6249-6270`). |
| 27 | Artifact intent | `app/main.py:6275-6400` | Rules first; a classifier only for the ambiguous band. |
| 28 | Engine dispatch | `app/main.py:6413-6978` | Branch order: SF intel → artifact file → unsupported visual → video → document → image → image follow-up → image unavailable → repo → crawl → site Q&A → URL → deep_research → agent → search → dataset → small-talk lane → **assistant chat** → `sf_live` SQL → LangGraph router (`get_graph().ainvoke`, `:6962`). |
| 29 | Main-model call | `app/llm.py:1450-1700` (`stream_chat_events`) | Thinking floor at `:1566-1570`. `normalize_system` folds every system block into one message (`:1592`). `_fit` is applied (`:1591-1598`). `extra_body` at `:1622-1637`. No stop sequences anywhere in `app/` (grep). |
| 30 | Fit to the window | `app/context.py:893-1031` | `max_tokens = max(1, min(ceiling, W − P − 512))` (`:1028-1030`). §2.5 has the detail. |
| 31 | Admission | `app/admission.py:10-32`; `app/config.py:2578-2582` | NORMAL lane: at most 10. LONG lane (prompt above 131,072 tokens): one at a time, waits up to 600 s for an idle engine, then closes NORMAL until its own first token. |
| 32 | Continuation | `app/continuation.py:448-468` | Each segment resends `base + assistant tail (≤ 6,000 characters) + instruction`. The run deadline is 21,600 s (`app/config.py:2441`). |
| 33 | Post-processing | `app/engines/chat.py:838` (AnswerGuard), `:914-930` (rewrite coverage); `app/main.py:6979-7087` (AS3 backstop) | `RESPONSE_VALIDATED` is recorded as `skipped`: `semantic_answer_validator_not_implemented` (`app/main.py:7105-7109`). |
| 34 | Persistence | `app/main.py:7093` → `_store_answer` `:3812-3870` | The server writes only the **assistant** row, before `done`, deduplicated on `generation_id`. User turns arrive through the client's `/history` (`app/history.py:325,347`). |
| 35 | Background compaction | `app/main.py:7135-7155` → `:2412` → `app/compaction.py:604-642` | Detached. Folds at more than 0.70 of the usable budget **or** more than 40,000 tokens. |
| 36 | Orchestrator stream | `app/main.py:1853-1862`, `:1898-1987`, `:4053-4079`; `app/sse.py:99-104`, `:123-135` | Every event is appended to `LiveGeneration.events`, which has **no size cap** (`:1804`). `follow()` replays, then streams live. `token`/`reasoning` are coalesced in a 25 ms window (the first token is never held). A `: keep-alive` comment is sent after 15 s idle. Headers: `text/event-stream` and `X-Accel-Buffering: no`. |
| 37 | Next pipe | `frontend/app/api/chat/route.ts:54-59`, `:418` | `new Response(upstream.body)` with `no-cache, no-transform`. Next's `compress` is not disabled; HYP: `no-transform` makes it skip. No reverse proxy is in the repo; cloudflared is outside it (FDU). |
| 38 | Browser parser | `frontend/lib/sse.ts:38-135`, `:117` | Incremental parser, O(n) on large events. **Heartbeat comments are dropped.** Unknown events are ignored. |
| 39 | Event handling, render | `frontend/lib/streams.ts:861-1043`, `:112-172`; `frontend/lib/markdownSegments.ts:1-40`; `frontend/components/Markdown.tsx:118-160` | Several `meta` events per turn (the first carries `generation_id`/`intent_id`). At most one React notify per animation frame; the first token commits immediately. The markdown keeps a frozen prefix and re-parses only the live tail. |
| 40 | End of stream | `frontend/lib/streams.ts:1038-1042` | A stream that ends cleanly **without** `done`/`error` is finalised as `done` and saved. |

**Allowed SSE events.** `token`, `meta`, `done`, `error`, `reasoning`, `step`, `status`, `research` (`app/sse.py:35-45`) (VIC).

### 2.2 How a request is interpreted (§13)

There is no single typed task spec. At least eight deciders each read a different slice of the turn (VIC):

| Decider | Kind | Where |
|---|---|---|
| Small-talk lexicon | rules | `app/fast_lane.py:196-235`, `:339-387` |
| Pasted-text transform detection | rules | `app/core/pasted.py` (`is_transform_ask`), used in `app/engines/orchestrate.py:150` |
| Agent/search plan | router LLM, 40 tokens | `app/engines/orchestrate.py:140-176` |
| Freshness | regex, then router (`max_tokens=4`, 0.6 s deadline) | `app/freshness.py:154-235`, `:266` |
| Auto search wish (Agent-toggle path) | router LLM, 200 tokens | `app/engines/search.py:754` |
| Artifact intent | rules, then a classifier | `app/artifacts/intent.py`; `app/main.py:6275+` |
| Salesforce route and clarification | router and planner LLMs | `app/engines/router.py:103-150`; `app/core/sf_intel/planner.py:226,245,390` |
| Max contract (sections and rules) | rules plus LLM `extract` (`max_tokens=300`) | `app/core/contract.py:832` |
| `app/core/effort_policy.py` | none: **no runtime caller** | its docstring (`:3-14`); grep finds no import |

### 2.3 Fast, Think and Max in code

| Aspect | Fast | Think | Max |
|---|---|---|---|
| `enable_thinking` | false (`app/llm.py:1549-1550`) | true (`app/llm.py:1254-1281`) | true |
| Auto agent/search router call | none | yes | yes; search forces agent |
| Knowledge pre-pass | live lookup inside `living_knowledge.prepare`, deadline 8 s (`app/living_knowledge.py:1770-1863`; `app/config.py:1279`) | no lookup; `escalate` runs the full search engine | as Think |
| Chat engine shape | one continuation run | one continuation run | `max_loop` (plan → draft → check → critique → revise) when the ask names sections (`app/engines/chat.py:589-641`). Otherwise best-of-N, `EXTRA_HIGH_SAMPLES` = 3 **buffered** candidates (`app/core/best_of.py:83-88`), judged on 4,000 characters each (`:29`) |
| Temperature | 0.6 (`app/engines/chat.py:524`; `app/core/answer_sampling.py:178`) | 0.3 | 0.3 |
| top_p / top_k / presence | only with `ANSWER_SAMPLING_PROFILE=qwen_instruct` (`answer_sampling.py:646-676`) | not sent | not sent |

`THINKING_SAMPLING` (`app/core/answer_sampling.py:94-100`), `routed_thinking_sampling` and `closure_sampling` (`:680-687`) have no caller outside that module and the tests (VIC).

### 2.4 Parameters that reach vLLM (main-model stream)

| Parameter | Value / source (VIC) |
|---|---|
| `messages` | `normalize_system` output: one system message at index 0 (`app/llm.py:824-870`, `:1592`). `apply_reasoning_effort` is a no-op (`:1403-1414`). |
| `max_tokens` | The `budget` from `_fit` → `context.fit_request` (`app/llm.py:1591-1606`). |
| `temperature` | The caller's value; the default is 0.2 (`app/llm.py:1455`). |
| `chat_template_kwargs.enable_thinking` | Sent only when the capability profile allows `chat_template_kwargs` (`app/llm.py:1314-1331`, `:1622`). |
| `thinking_token_budget` | Only when `THINKING_BUDGET_MODE=client` **and** `SERVER_THINKING_BUDGET=true`. Both are off by default (`app/llm.py:1624-1629`; `app/config.py:958-959,1004`). |
| `top_k` / `min_p` / `repetition_penalty` | Only from a Fast `answer_plan` (`app/llm.py:1630-1637`). |
| `stop` | Never sent. |
| `continue_final_message` | Only for `/v1` durable runs (`app/llm.py:1638-1643`). Chat continuation resends the tail instead (row 32). |
| Wall clock | `GEN_WALL_CLOCK_S` = 1,800 s per call (`app/config.py:969`). |

### 2.5 Every output and context budget, and the §18.2 budget law

**Core arithmetic (VIC).**
- **W.** W is read from the engine's `/tokenize` `max_model_len` and cached per base URL (`app/context.py:741-744`). Without it, `model_window` falls back to `MAIN_MODEL_MAX_LEN` → `MODEL_MAX_CONTEXT` → **262,144** (`app/context.py:767-779`; `app/config.py:941-943`). The compose default is `--max-model-len ${MODEL_MAX_CONTEXT:-262144}` (`compose/compose.dgx-spark.yaml:47`). That production serves 1,000,000 via YaRN is FDU (`docs/ARCHITECTURE_CURRENT.md:128`; `app/publicapi/registry.py:640-641`).
- **G.**
  - `fit_request` sets `ceiling = requested or MODEL_MAX_OUTPUT (8,192)` (`app/context.py:917`). Then `max_tokens = max(1, min(ceiling, W − P − CONTEXT_SAFETY_MARGIN 512))` (`:1028-1030`).
  - The prompt is trimmed **only** when room falls below `MIN_OUTPUT_TOKENS` = 256 (`:59`, `:958-959`). Trimming then continues until room reaches `min(ceiling, 32,768, W/4)` (`:65-69`): oldest turns first, then the longest message is clipped in the middle (`:993-1003`).
- **Thinking floor.** When thinking is on and budgets are off, `requested = max(max_tokens, MAX_OUTPUT_TOKENS 65,536)` (`app/llm.py:1566-1570`). Every Think/Max call therefore asks for at least 65,536 tokens, whatever ceiling the engine passed, and **reasoning and answer share that one pool**.
- **Units.** The final fit is in tokens when `/tokenize` answers. If it fails or times out (`TOKENIZE_TIMEOUT` = 5 s, `app/config.py:1036`), the count falls back to 3 characters per ASCII token (`app/context.py:73`, `:232-238`). Almost every *input* cap upstream of the fit is in **characters** (table below). §18.2's "never by slicing characters" is met only at the last step.

**Worked example (arithmetic from code, not observed).** Take W = 262,144 and a Think turn (ceiling 65,536):

| P | G |
|---|---|
| up to about 196K | the full 65,536 |
| 200K | about 61.6K |
| 250K | about 11.6K |
| about 261.4K | 256 |

Nothing is trimmed and no notice is given along the way. Fast (ceiling 8,000) gets `min(8,000, W − P − 512)`. If the W in force is 1,000,000, this squeeze happens near 1M, not near 250K.

| Budget | Value | Unit | Where (VIC) |
|---|---|---|---|
| Chat engine segment | 6,000 small talk / 8,000 / 16,000 at Think and Max (16,000 is raised to 65,536 by the floor) | tokens | `app/engines/chat.py:516-521` |
| Fast segment / total | 8,000 / 1,000,000 | tokens | `app/core/answer_sampling.py:109,122`; `app/engines/chat.py:531-538` |
| Logical total per effort | `MAX_LOGICAL_OUTPUT_TOKENS`, `CONTINUATION_BUDGET_*` = 1,000,000 | tokens | `app/config.py:2392-2426`; `app/continuation.py:899-917` |
| Continuation tail / min segment / max segments | 6,000 / 512 / 400 | chars / tokens / count | `app/config.py:2429-2436` |
| Agent synthesis | Think 6,000, Max 12,000. **A single call, no continuation.** | tokens | `app/engines/agent.py:48`, `:814-826` |
| Search answer | 12,000, single call | tokens | `app/engines/search.py:2258` |
| Search evidence | 8,000 per source | **chars** | `app/config.py:1214` |
| Document answer / excerpt / stored | 12,000 single call / 48,000 per question / 400,000 per document | tokens / **chars** | `app/engines/document.py:1262`, `:82`, `:90` |
| URL engine | 12,000; 12,000 per document, 90,000 total | tokens / **chars** | `app/engines/url.py:348,366,27-28` |
| Pinned stored-doc / URL blocks on later turns | 8,000 / 6,000 per item, no total cap | **chars** | `app/main.py:5754`, `:5721` |
| Deep Research report | 6,000 per segment, 24,000 total, thinking off | tokens | `app/config.py:1630-1632`, `:2451-2453` |
| Max loop plan / critique / critic draft | 700 / 1,200 / 60,000 | tokens / tokens / **chars** | `app/core/max_loop.py:94,98,104` |
| Best-of judge | 300 tokens; 4,000 characters per candidate | tokens / **chars** | `app/core/best_of.py:203,29` |
| Router calls | decide 40, freshness 4, route 200/50 | tokens | `app/engines/orchestrate.py:159`; `app/freshness.py:223`; `app/engines/router.py:127,142` |
| Summary | `SUMMARY_MAX_TOKENS` 2,000. Each folded turn is clipped to its **first 4,000 characters**. | tokens / **chars** | `app/summarize.py:35`, `:47-48`; `app/config.py:1073` |
| Compaction reservation | `max(MIN_OUTPUT_FLOOR 1,024, ceiling or MODEL_MAX_OUTPUT 8,192)`, capped at W/2 | tokens | `app/compaction.py:99-115` |
| Compaction triggers | sync 0.80, background 0.70 of `W − reserve − 512`, **or** more than 40,000 tokens. Keeps 8 recent turns, halving to 2. | tokens | `app/compaction.py:268-281`, `:552-588`; `app/config.py:1047-1072` |
| Engine history windows | `CHAT_HISTORY_TURNS` 400; SQL 6; search rewrite 4/2; Deep Research 4/2 | turns | `app/config.py:2476`; `app/engines/__init__.py:6-19`; `app/engines/sql.py:211`; `app/engines/search.py:750,964` |
| Admission LONG lane | above 131,072 tokens: one at a time, up to 600 s wait | tokens / s | `app/config.py:2578-2582` |

**Where the code departs from the §18.2 budget law (VIC):**
1. **G has no real floor.** The 65,536 "floor" in `docs/CONFIG.md:32,56` is a requested ceiling, not a guarantee. `fit_request` leaves G alone while it is at least 256.
2. **Compaction measures a different request from the one sent.**
   - The probe is `assemble(history) + user` (`app/compaction.py:167-185`). It leaves out the engine's system prompt and the grounding.
   - It reserves 8,192 tokens because `app/main.py:5913-5936` passes no `requested_max_tokens`. The real Think call asks for at least 65,536.
3. **Pinned blocks can be clipped.** They are system turns that `fit_request` never drops (`app/context.py:782-794`). `normalize_system` folds them into one message before the fit, so the "longest message" that gets middle-clipped can be that folded system block (`app/context.py:999-1003`).
4. **Detail past 4,000 characters is lost before summarisation.** Exact items that §19 must preserve (file names, IDs, constraints) are dropped if they sit beyond the first 4,000 characters of a turn (`app/summarize.py:47-48`).

### 2.6 Browser-path limits and timeouts

| Where | Limit | Evidence | Label |
|---|---|---|---|
| Next server timers | `requestTimeout 0`, `headersTimeout 100 s`, `keepAliveTimeout 95 s`; request body idle 60 s (`FRONTEND_BODY_IDLE_S`) | `frontend/server-preload.cjs:77-89` | VIC |
| Next → orchestrator fetch | No timeout set; undici defaults apply. A 300 s body-idle cut was seen before heartbeats existed. | `frontend/app/api/chat/route.ts:371-389`; `app/sse.py:89-99` | VIC / FDU |
| Frontend drain | On SIGTERM, chat SSE sockets are destroyed after 15 s (`FRONTEND_DRAIN_SSE_S`) | `frontend/server-preload.cjs:44-61`, `:197-231` | VIC |
| Edge | Request body capped at about 100 MB; idle limit not in the repo | `frontend/lib/orchestrator.ts:179-181` (comment); `gateway/lib/relay.cjs:29` (cites a 524 at 125 s) | FDU |
| `/v1` gateway only | requestTimeout 0, headersTimeout 100 s, keepAlive 95 s, upstream silence 300 s | `gateway/lib/settings.cjs:77-90` | VIC |

### 2.7 Cancellation and reconnect

- **Stop path.** Stop comes from the button, the Escape key or chat deletion (`frontend/components/ChatApp.tsx:4121,3375,3266`).
  - `stopStream` aborts the local fetch, then sends a fire-and-forget `POST /api/chat/stop`, whose reply is never read (`frontend/lib/streams.ts:192-202`).
  - The orchestrator finds `_live_generations[conversation_id]`, checks the owner, calls `gen.task.cancel()` and marks the row `cancelled` (`app/main.py:7622-7641`) (VIC).
  - Whether the cancel reaches the in-flight vLLM stream and the Max child tasks is HYP.
- **Gaps (path VIC, effect HYP):**
  - (a) A Stop pressed before the generation is registered gets `{stopped:false}`. That window covers the body read, the ownership and intent writes (`app/main.py:4337-4590`) and the photo pre-upload. The generation then starts detached.
  - (b) If `/chat/stop` fails, the tab has already finalised as `stopped` (`frontend/lib/streams.ts:1375-1377`). The 8 s poll then sees the conversation still active and re-attaches (`frontend/components/ChatApp.tsx:1061-1075`), so the stopped answer starts streaming again.
- **Reattach.**
  - After a reload, the mount and the 8 s poll call `/api/chat/active`, then `/requests/{intent}`, then `/attach/{id}` (`frontend/components/ChatApp.tsx:1029-1140`; `frontend/lib/streams.ts:1438-1550`).
  - Attach replays the whole buffer, then streams live. If nothing is live but the row is resumable, it re-runs from the stored snapshot (`app/main.py:7754-7800`).
  - A pipe lost mid-stream reconnects with backoff from 1 s up to 30 s, at most 20 times (`frontend/lib/streams.ts:557-583`, `:630-780`). Retries are idempotent through the browser-minted `intent_id` (VIC).
- **No client stall detector.** Heartbeats are dropped (`frontend/lib/sse.ts:117`) and `consume()` has no idle timer, so a half-open pipe can leave a row "streaming" indefinitely. The poll skips streaming conversations (`frontend/components/ChatApp.tsx:1071`) (HYP on effect).

**Measurements §21 asks for:** first status, first token per hop, cadence after coalescing, total time, and buffering at the edge. All `TEST_NOT_RUN`.

Existing tests that bear on them were not run. They include `frontend/tests/{sse,sse-large-event,streams,streams-queued,streaming-frame-coalescing,server-preload}.test.ts` and `orchestrator/tests/{test_sse,test_sse_v2,test_live_generation}.py`. Server metrics already exist: `relay_overhead` (`app/main.py:1864-1896`) and `chat_ttft_seconds` (`app/main.py:4869-4876`).

---

## 3. Configuration precedence (A-04)

### 3.1 Precedence chain (highest wins)

| # | Layer | Mechanism | Evidence (VIC) |
|---|---|---|---|
| 1 | Per-request body (`/chat`) | `effort`, `model`, `web_search`, `deep_research`, `agent`, `mode`. **No per-request `max_tokens`, context or timeout.** | `app/main.py:2640-2707` |
| 2 | Served window from the engine | `/tokenize` `max_model_len` overrides the configured window for fitting | `app/context.py:741-744`, `:767-779` |
| 3 | Compose `environment:` on the orchestrator | Overrides `env_file`. It lists SF_*, ASR_*, VOICE_*, OCR_BASE_URL, PUBLIC_API_*, API_PLAYGROUND_*, the body caps (`MAX_REQUEST_BODY_BYTES`, `CHAT_MAX_REQUEST_BODY_BYTES`), `CPU_POOL_*` and the loop-probe keys, interpolated from the same env files. It sets **no** context, output, timeout or thinking key. | `compose.yaml:215-357` |
| 4 | Container `env_file` | `.env`, then the secrets env, then the **generated env, which is last and wins** | `compose.yaml:12-18`, `:214` |
| 5 | Compose interpolation | The launcher passes `--env-file` for `.env`, secrets and generated, in that order, then re-applies them over `os.environ` so an exported shell variable cannot win | `launcher/techsara_cli/compose.py:90-118` |
| 6 | Launcher-generated values | `build_generated_environment` derives window, YaRN, capability and serving keys | `launcher/techsara_cli/environment.py:801-1181` |
| 7 | Hardware profile and manifest | dgx-spark `initial_context` 262,144, clamped to the manifest `context_limit`. An explicit `MAIN_MODEL_MAX_LEN` is not clamped (range 4,096 to 1,048,576). | `config/hardware-profiles.yaml:89`; `config/model-manifest.yaml:194`; `launcher/techsara_cli/environment.py:337`, `:358-379` |
| 8 | `config.py` defaults | Unset or blank means the default. Values are frozen at import (`settings = Settings()`), so any change needs a recreated container. | `app/config.py:25-56`, `:2969` |

- **Aliases.**
  - `MAIN_MODEL_MAX_LEN` > `MODEL_MAX_CONTEXT` > 262,144 (`app/config.py:941-943`).
  - `MAIN_MODEL_DEFAULT_MAX_OUTPUT_TOKENS` > `MODEL_MAX_OUTPUT` > 8,192 (`:944-946`).
  - `LLM_REQUEST_TIMEOUT` > `GEN_WALL_CLOCK_S` (`:2486-2488`).
- **Module-level env reads that bypass `Settings` and `/health`.**
  - `SSE_HEARTBEAT_SECONDS` (`app/sse.py:99`).
  - `SSE_COALESCE_MS` (`app/sse.py:123-126`).
  - `ANSWER_SAMPLING_PROFILE` and `ANSWER_FAST_PROSE_TOTAL_TOKENS` (`app/core/answer_sampling.py:178-180`).
  - The `KNOWLEDGE_FAST_*` fallbacks (`app/living_knowledge.py:154,163`).
- **Compose `-f` order (launcher)** (`launcher/techsara_cli/cli.py:216-244`):
  1. `compose.yaml`
  2. the profile overlay
  3. `compose.windows-wsl2.yaml` (Windows only)
  4. `compose.published-<family>.yaml` (when ports are published)
  5. `compose/compose.cluster-dgx-spark.yaml`, always last in dual mode

  The monitoring, tunnel, OCR and whisper overlays are not in the launcher chain (`cli.py:259-262`).
- **Project name.** The launcher hard-codes `--project-name sf-local-ai` (`launcher/techsara_cli/compose.py:91`), while `compose.yaml:10` derives the name from `TECHSARA_STACK`. HYP: a dev stack must not be started through the launcher.
- **Deploy path.** The deploy path's own `-f` chain and env order were not read (the guard blocks it), so they are unverified.

### 3.2 Settings that matter for this programme

The values are code defaults. Production values are unverified. The "Override" column shows which layer can change the setting: `.env` is the operator file, `gen` is the launcher's generated env, and `req` is a per-request field.

| Setting | Default | Read at | Override | Remark |
|---|---|---|---|---|
| `MAIN_MODEL_MAX_LEN` | unset (profile 262,144) | `launcher/techsara_cli/environment.py:366-379`; `app/config.py:941` | .env | Above 262,144 the launcher adds YaRN `--hf-overrides` (`environment.py:834-836`; `launcher/techsara_cli/modelshape.py:218-254`). Wins over `MODEL_MAX_CONTEXT` in `config.py`. |
| `MODEL_MAX_CONTEXT` | 262,144 | `app/config.py:942`; `compose/compose.dgx-spark.yaml:47` | gen (`environment.py:971`) | A deprecated alias in `.env`. |
| Dual-mode `--max-model-len` | the context value | `launcher/techsara_cli/cluster.py:778` | launcher | The KV-capacity check refuses a window the pool cannot hold (`environment.py:395-430`). `CLUSTER_KV_CACHE_MEMORY_GIB` defaults to **16** (`cluster.py:76`). Docs say production uses 8 because 16 exhausted memory at 1M (FDU, `docs/CLUSTER.md:382`). |
| Single-node start-up retry | `profile.startup_retry_context` | `launcher/techsara_cli/cli.py:1133-1145` | launcher | Silently lowers gen `MODEL_MAX_CONTEXT` and drops concurrency to 1. HYP: `/health`, admission and sf_intel then read a stale window (`app/health.py:690`; `app/core/sf_intel/budget.py:83`). |
| `DEFAULT_MAX_CONTEXT` / `REPORT_MAX_CONTEXT` | 32,768 / 65,536 | `app/config.py:912-913` | gen sets both to the window (`environment.py:972-973`) | |
| `MAIN_MODEL_DEFAULT_MAX_OUTPUT_TOKENS` | 8,192 | `app/config.py:944-946` | .env | The fallback ceiling (`app/context.py:917`) **and** the compaction reservation (`app/compaction.py:111`). |
| `MAIN_MODEL_HIGH_MAX_OUTPUT_TOKENS` | 16,384 | `app/config.py:950` | .env | Read only by `app/core/sf_intel/budget.py:77-81` and `/health` (`app/health.py:697`). Not by the chat engine. |
| `MAX_OUTPUT_TOKENS` | 65,536 | `app/config.py:964`; `app/llm.py:1566-1570` | .env | The thinking floor (§2.5). |
| `MAX_LOGICAL_OUTPUT_TOKENS`, `CONTINUATION_BUDGET_*` | 1,000,000 | `app/config.py:2392-2426` | .env | `CONTINUATION_ENABLED=false` gives a single call. |
| `CHAT_HISTORY_TURNS` / `KEEP_RECENT_TURNS` | 400 / 8 | `app/config.py:2476`, `:1072` | .env | |
| `CONTEXT_WARN_THRESHOLD` / `CONTEXT_BG_COMPACT_THRESHOLD` / `CONTEXT_COMPACT_THRESHOLD` | 0.60 / 0.70 / 0.80 | `app/config.py:1047-1055` | .env | Fractions of `W − reserve − margin` (`app/compaction.py:96`). |
| `CONTEXT_COMPACT_MAX_TOKENS` | 40,000 | `app/config.py:1069-1071` | .env | An absolute trigger; whichever fires first wins. |
| `SUMMARY_MAX_TOKENS` / `MIN_OUTPUT_FLOOR` | 2,000 / 1,024 | `app/config.py:1073`, `:1077` | .env | |
| `CONTEXT_SAFETY_MARGIN` | 512 | `app/config.py:1032` | .env | Chat sizing uses this one. |
| `MAIN_MODEL_CONTEXT_SAFETY_MARGIN` | 8,192 | `app/config.py:1806-1808` | .env | Only sf_intel and `/health`. |
| `GEN_WALL_CLOCK_S` | **1,800** | `app/config.py:969`; `app/llm.py:320-338` | .env | Per-stream hang guard. |
| `LLM_REQUEST_TIMEOUT` | = `GEN_WALL_CLOCK_S` | `app/config.py:2486-2488` | .env | The "≥ wall clock" invariant is **not enforced** on chat; only `/v1` checks it (`app/publicapi/planning.py:219-240`). CI sets 60 (`e2e/ci/stack.sh:237`). |
| `LLM_CONNECT_TIMEOUT` / `LLM_WRITE_TIMEOUT` / `LLM_MAX_RETRIES` | 10 / 60 / 0 | `app/config.py:2492-2499` | .env | |
| `TOKENIZE_TIMEOUT` | 5 | `app/config.py:1036` | .env | On timeout the count falls back to an estimate. |
| `CONTINUATION_DEADLINE_S` | 21,600 | `app/config.py:2441` | .env | |
| `SSE_HEARTBEAT_SECONDS` | 15 | `app/sse.py:99` | .env | |
| `ADMISSION_LONG_THRESHOLD_TOKENS` / `ADMISSION_NORMAL_MAX` / `ADMISSION_LONG_WAIT_S` | 131,072 / 10 / 600 | `app/config.py:2578-2582` | .env | |
| `FRESHNESS_FAST_DEADLINE_S` | 8.0 | `app/config.py:1279`; `app/living_knowledge.py:335` | .env (commented in `.env.example:1027`) | Bounds the Fast live lookup. |
| `KNOWLEDGE_PREPARE_DEADLINE_S` | 12.0 | `app/config.py:1356`; `app/main.py:6180` | .env (commented in `.env.example:1054`) | Bounds the whole pre-answer stage. Not gated on effort. |
| `FRESHNESS_FAST_SECOND_SOURCE_GRACE_S` | 0 (off) | `app/config.py:1286-1288` | .env | Opt-in. |
| `KNOWLEDGE_FAST_TOPICAL_PRECHECK` / `KNOWLEDGE_FAST_CONCURRENT_RETRIEVE` / `KNOWLEDGE_WARM_ON_START` | true / true / true (false under pytest) | `app/config.py:1407`, `:1425`, `:1367-1369` | .env | |
| `SEARCH_ENABLED` | **false** | `app/config.py:1172` | .env | Gates Deep Research, auto search and the Fast lookup. |
| `ANSWER_SAMPLING_PROFILE` | `legacy` | `app/core/answer_sampling.py:178` | .env | Only `qwen_instruct` sends top_p, top_k or presence. |
| `CPU_POOL_WORKERS` | 0 (threads) | `app/config.py:2609-2612`; `compose.yaml:346` | .env | |
| Thinking capability gate | gen emits `MAIN_SUPPORTS_REASONING=true` and `MAIN_EXTRA_BODY_ALLOWED=chat_template_kwargs` for vllm-cuda | `launcher/techsara_cli/environment.py:783-795`; `app/llm.py:1314-1331` | gen / .env | With the gate off, no `enable_thinking` is sent. HYP: the template default may then think on Fast. |
| `THINKING_BUDGET_MODE`, `THINKING_BUDGET_HIGH/_EXTRA_HIGH/_GRACE`, `SERVER_THINKING_BUDGET` | off; 12,000 / 24,000 / 1.25; false | `app/config.py:958-959`, `:989-995`, `:1004` | .env | Active only in `client` mode. |
| `EXTRA_HIGH_SAMPLES` | 3 | `app/config.py:1008` | .env | Best-of-N at Max. |
| Engine reasoning parser | `--reasoning-parser qwen3` | `compose/compose.dgx-spark.yaml:51` | compose | |

**Inconsistencies.**
- **The `GEN_WALL_CLOCK_S` default disagrees with comments.** The code default and `docs/CONFIG.md:57` say 1,800. Comments at `app/config.py:2388`, `compose.yaml:522` and `app/publicapi/registry.py:216` say 4,200. HYP: production sets 4,200.
- **There are two default-output keys.** `.env.example:509` sets `MODEL_MAX_OUTPUT` and `:596` sets `MAIN_MODEL_DEFAULT_MAX_OUTPUT_TOKENS`. The latter wins.
- **These keys are missing from `.env.example`:**
  - `GEN_WALL_CLOCK_S`, `LLM_REQUEST_TIMEOUT`, `MAX_OUTPUT_TOKENS`
  - `THINKING_BUDGET_MODE`, `CHAT_HISTORY_TURNS`, `CONTEXT_COMPACT_MAX_TOKENS`
  - `MAX_LOGICAL_OUTPUT_TOKENS`, `CONTINUATION_*`, `ADMISSION_*`, `SSE_HEARTBEAT_SECONDS`
  - four of the six Fast switches

---

## 4. Feature classification (A-03)

Classes: **working-in-code** means implemented and wired on a default path (never runtime-verified here). **Incomplete** means wired but missing a part the programme needs. **Duplicated** means two or more mechanisms decide the same thing, or a superseded copy is kept. **Bypassed** means implemented but off by default, overridden, or not on any path. **Documented-only** means described in docs or code with no runtime caller.

| # | Feature | Class | Evidence |
|---|---|---|---|
| 1 | Effort ladder fast/think/max (UI → wire → canonical) | working-in-code | `frontend/components/ModelPicker.tsx:25`; `app/main.py:2838-2844`; `app/llm.py:810-821` |
| 2 | Fast turns never think (ContextVar) | working-in-code | `app/main.py:4933`; `app/llm.py:1549-1550` |
| 3 | Thinking switch via capability gate | working-in-code | `app/llm.py:1314-1331`; `launcher/techsara_cli/environment.py:783-795` |
| 4 | Served-window discovery from `/tokenize` | working-in-code | `app/context.py:741-744`, `:767-779` |
| 5 | Prompt fit and overflow trim | working-in-code | `app/context.py:893-1031` |
| 6 | Output floor for thinking (G guarantee) | incomplete: a requested ceiling only, no floor under W − P | `app/llm.py:1566-1570`; `app/context.py:958-959`, `:1028-1030` |
| 7 | Per-engine `max_tokens` at Think/Max | bypassed: raised to 65,536 by the thinking floor | `app/engines/chat.py:516-521`; `app/llm.py:1566-1570` |
| 8 | `MAIN_MODEL_HIGH_MAX_OUTPUT_TOKENS` for chat | bypassed: read only by sf_intel and `/health` | `app/core/sf_intel/budget.py:77-81`; `app/health.py:697` |
| 9 | Compaction (sync and background) | working-in-code | `app/main.py:5913-5936`, `:7135-7155`; `app/compaction.py:552-642` |
| 10 | Compaction reservation per effort | incomplete: always 8,192, no system prompt in the probe | `app/main.py:5913-5936`; `app/compaction.py:111`, `:167-185` |
| 11 | Rolling summary | incomplete: only the first 4,000 characters of each folded turn | `app/summarize.py:35`, `:47-48` |
| 12 | Continuation in the chat engine (also dataset, artifact, Deep Research per reader) | working-in-code | `app/continuation.py:448-468`; `app/engines/chat.py:879` |
| 13 | Continuation for agent synthesis, search, document and URL answers | incomplete: single call | `app/engines/agent.py:814-826`; `app/engines/search.py:2258`; `app/engines/document.py:1262`; `app/engines/url.py:348,366` |
| 14 | Two continuation mechanisms (resend tail vs `continue_final_message`) | duplicated | `app/continuation.py:448-468`; `app/llm.py:1638-1643` |
| 15 | Semantic answer validation | incomplete: recorded `skipped` | `app/main.py:7105-7109` |
| 16 | Repetition guard (AnswerGuard) | working-in-code | `app/engines/chat.py:838`; `app/core/answer_guard.py:16-45` |
| 17 | Max loop (plan/draft/check/critique/revise) | working-in-code | `app/engines/chat.py:589-641`; `app/core/max_loop.py:94-135` |
| 18 | Max best-of-N | working-in-code (the judge sees 4,000 characters) | `app/engines/chat.py:659-731`; `app/core/best_of.py:29`, `:83-88` |
| 19 | Deep Research | working-in-code, explicit pill only | `app/main.py:5184-5192` |
| 20 | Auto agent/search planner (`decide`) | working-in-code | `app/engines/orchestrate.py:128-176` |
| 21 | Fast small-talk lane | working-in-code | `app/fast_lane.py:339-387` |
| 22 | Knowledge pre-pass and Fast live lookup | working-in-code | `app/main.py:6174-6191`; `app/living_knowledge.py:1770-1863` |
| 23 | Request interpretation (8+ deciders, no task spec) | duplicated | §2.2 |
| 24 | Freshness / time-sensitivity detection | duplicated | `app/freshness.py:154-266`; `app/engines/search.py:487` (`_FRESH_RE`); `app/fast_lane.py` live cues |
| 25 | History windows per engine on top of compaction | duplicated | `app/engines/__init__.py:6-19`; `app/engines/sql.py:211`; `app/engines/search.py:750` |
| 26 | `core/effort_policy.py` | documented-only | `app/core/effort_policy.py:3-14`; no import under `app/` |
| 27 | `THINKING_SAMPLING`, `routed_thinking_sampling`, `closure_sampling` | documented-only (tests only) | `app/core/answer_sampling.py:94-100`, `:680-687` |
| 28 | Client thinking budgets (`THINKING_BUDGET_MODE=client`) | bypassed (off by default) | `app/config.py:958-959`; `app/llm.py:1284-1302` |
| 29 | Server thinking budget | bypassed (off; a comment says this build ignores it) | `app/llm.py:1624-1629`; `app/config.py:1004` |
| 30 | `qwen_instruct` sampling profile | bypassed (default `legacy`) | `app/core/answer_sampling.py:178`, `:646-676` |
| 31 | CPU worker processes | bypassed (`CPU_POOL_WORKERS=0`) | `app/config.py:2609-2612` |
| 32 | Browser `agent` flag | bypassed (always false) | `frontend/lib/prefs.ts:130` |
| 33 | `model` request field | duplicated (pinned to `smart`; superseded by effort) | `frontend/components/ModelPicker.tsx:113-118`; `app/main.py:2680` |
| 34 | Detached generation, attach, intent reconciliation | working-in-code | `app/main.py:1781-1795`, `:7754-7800`; `frontend/lib/streams.ts:1438-1550` |
| 35 | Stop → server cancel | incomplete: unacknowledged, with race windows | `frontend/lib/streams.ts:192-202`; `app/main.py:7622-7641` |
| 36 | SSE heartbeat, coalescing, drain | working-in-code | `app/sse.py:99-104`, `:123-135`; `frontend/server-preload.cjs:44-61` |
| 37 | Server-side answer persistence | working-in-code (assistant row only) | `app/main.py:3812-3870`, `:7093` |
| 38 | Admission lanes NORMAL/LONG | working-in-code | `app/admission.py:10-32`; `app/config.py:2578-2582` |
| 39 | `LLM_REQUEST_TIMEOUT ≥ GEN_WALL_CLOCK_S` invariant | incomplete: enforced on `/v1` only | `app/publicapi/planning.py:219-240`; `app/config.py:2486-2488` |
| 40 | Context meter in the browser | duplicated (own 131,072 default and ÷4 estimate) | `frontend/lib/contextMeter.ts:38-50` |
| 41 | `/health` context budget | duplicated (configured window, 8,192 margin) | `app/health.py:690-704` |
| 42 | YaRN window extension, KV capacity check | working-in-code (launcher) | `launcher/techsara_cli/modelshape.py:218-254`; `launcher/techsara_cli/environment.py:395-430` |
| 43 | Query tracing `/chat/trace/{id}` | working-in-code | `app/main.py:7339` |
| 44 | v1-gateway (`/v1` relay) | working-in-code | `gateway/server.cjs:6-9`; `compose.yaml:573-576` |
| 45 | Engine-controller and sentinel | working-in-code (staged by the launcher) | `launcher/techsara_cli/environment.py:629`; `monitoring/engine-controller/controller.py` |
| 46 | knowledge-service | bypassed (built, in no compose file, not called by the orchestrator) | `knowledge-service/compose.fragment.yaml:1`; `evaluation/runners/discovery_runner.py:35` |
| 47 | `docker-compose.yml`, `orchestrator/Dockerfile` | duplicated (superseded, kept for rollback) | `docker-compose.yml:2-16`; `orchestrator/Dockerfile:2-4` |
| 48 | Sandboxed code interpreter (§4) | documented-only (reported in §4; the docs say MISSING) | `docs/ARCHITECTURE_CURRENT.md:199`; grep-negative in `app/` |
| 49 | Video understanding | working-in-code (code present; runtime not checked) | `app/video/`; `app/engines/video.py` |

**Not found in code (missing, not a class).** These have no implementation in code:
- a typed task spec (§13);
- a client stall detector;
- stop sequences;
- a per-request capability registry for G;
- a pending-cancel record for a Stop that arrives before the generation is registered.

---

## 5. Symptom hypotheses (A-06)

Plausibility is a ranking for Phase B, not a finding. Mechanisms already shown in §2 are referenced by number, not repeated.

**Phase B ground rules.** Use dev resources (`--env-file ops/dev/stack.vars`) or the guarded low-traffic window, synthetic prompts only, and per-request metadata only: route, effort, prompt tokens, `max_tokens` as sent (`llm.get_applied_max_tokens`, `app/llm.py:305-308`), finish reason, admission wait, stage timings from `/chat/trace/{id}` (`app/main.py:7339`), and the existing metrics `knowledge_prepare_seconds`, `knowledge_blocked_seconds`, `knowledge_fast_lookup_seconds`, `llm_admission_wait_seconds` and `llm_admission_rejections_total` (`app/metrics.py:61-72,212`).

### Symptom 3: near ~250K the output gets short, stalls, or loses relevance

**Different layers assume different W values:**

| Layer | W it assumes | Evidence | Label |
|---|---|---|---|
| Code default | 262,144 | `app/config.py:941-943` | VIC |
| Docs (served) | 1,000,000 | `docs/ARCHITECTURE_CURRENT.md:128` | FDU |
| Frontend meter (before the first reply) | 131,072 | `frontend/lib/contextMeter.ts:38-46` | VIC |
| Router engine | 65,536 | `app/context.py:874-880` | VIC |
| `/health` budget | the configured window, not the served one | `app/health.py:690-704` | VIC |

| # | Hypothesis | Evidence | Label | Plaus. | Cheapest experiment |
|---|---|---|---|---|---|
| 3.1 | **§18.3 leading hypothesis: G is squeezed toward 0** wherever the W in force is about 262,144 (§2.5 worked example). At Think/Max, reasoning eats the remainder first. | §2.5 | VIC (mechanism); HYP (which W) | High if W = 262K, else Low | Read `/tokenize` `max_model_len` on the dev engine and `/health` `context_window`. Send synthetic pastes at 240K, 250K, 256K, 260K and 262,144 at Fast and Think. Log `max_tokens` sent, finish reason, completion and reasoning tokens. Rejected if G equals the ceiling at 250K. |
| 3.2 | **The LONG admission lane stalls.** Above 131,072 tokens: one slot, up to 600 s wait for idle, then NORMAL is closed until the first token (60 s + tokens/1000, at most 1,200 s). | `app/admission.py:10-32`, `:406-409`, `:606-609`, `:1597-1609`; `app/config.py:2578-2594` | VIC | High | Two concurrent 200K prompts plus a stream of Fast turns. Measure `llm_admission_wait_seconds` by lane, TTFT per request, and how long NORMAL stays closed. |
| 3.3 | **Each continuation segment re-prefills in full.** The base prompt is resent (row 32) and prefix caching is off, so every segment repeats the 250K prefill and passes through the LONG lane again. Think/Max segments also re-think (`app/engines/chat.py:879` passes no `answer_plan`). | `app/continuation.py:590-604`; `app/kv_budget.py:17-19`; `docs/CLUSTER.md:176-177` | VIC (resend); FDU (timings) | High | One 250K prompt asking for about 20K output tokens. Record per-segment start and first-token times, and `meta.continuation`. |
| 3.4 | **Compaction loses the task.** It fires at 40,000 tokens absolute and keeps 8 to 2 recent turns. The summary is capped at 2,000 tokens and built from the first 4,000 characters of each turn. | §2.5 table; `app/compaction.py:553-583` | VIC (mechanism); HYP (impact) | High for multi-turn chats | A multi-turn dev chat that grows past 250K, with facts placed early, mid and late in old pastes. Compare recall against a single-turn send, and record the `context` meta. |
| 3.5 | **Character budgets and estimate fallback.** The document route sends a 48,000-character excerpt whatever the file size (`app/engines/document.py:82`, `:1150-1168`). If `/tokenize` takes more than 5 s at large P, the ÷3 estimate over-counts English and trimming starts early. | `app/context.py:73`, `:749-757` | VIC; HYP (timeout rate) | Medium | Time `/tokenize` at 250K idle and under load. Count trim and clip warnings. Score cross-section questions on a 250K upload. |
| 3.6 | **Static YaRN (×3.82) changes behaviour at all lengths.** | `launcher/techsara_cli/modelshape.py:218-254` | HYP | Medium | A/B on dev: native 262,144 against the 1M YaRN setup, with multi-fact probes at 32K, 128K and 240K. |
| 3.7 | **Layers disagree about W**, so "~250K" may not be the real P. The meter estimates characters ÷ 4; the orchestrator uses ÷ 3. | table above | HYP | Medium | Paste a known 250K-token text in the browser and compare the meter with the orchestrator's `prompt_tokens`. |
| 3.8 | **Unbounded reasoning reads as a stall**: a long silent phase at Think/Max. | `app/llm.py:1556-1570` | HYP | Medium | At 250K, compare the reasoning share and the time to the first answer token, Fast against Think. |
| 3.9 | **Proxy or wall-clock timeouts.** | §2.6; `app/config.py:969` | HYP | Low | One request sent through direct vLLM, the orchestrator, `/api/chat` and the browser. Compare where it ends and the recorded cancel cause. |
| 3.10 | **Silent fallback to a smaller model.** | `app/llm.py:1212-1222`, `:1243-1251` refuse it for answers | VIC (rejected) | Low | Check `meta.model` on the runs above. |

The existing probe `orchestrator/scripts/validate_long_context.py` (sizes 65,536 to 240,000; needles at 2%, 50% and 97%) tests direct vLLM and needle recall only. §18.3 also needs the proxy, backend and browser layers, and tasks that are not needle searches.

### Symptom 1: Fast is slow while gathering sources

These steps run before the first token on a Fast turn that needs sources (VIC):

1. The freshness router, with a 0.6 s deadline (`app/freshness.py:266`).
2. Speculative local retrieval: embed, dense, lexical, rerank (`app/config.py:1422-1447`).
3. If nothing fresh is stored, `_fast_lookup` runs (`app/living_knowledge.py:1770-1863`): one SearXNG query with a new HTTP client per call (`app/search/searxng.py:47`), 2 page reads, extraction on **one thread for the whole process** (`app/engines/search.py:132-133`; `EXTRACT_TIMEOUT_MS` 5 s at `app/config.py:1197`), then a store write and synchronous indexing. All of this runs under the 8 s deadline plus a 0.5 s backstop (`app/living_knowledge.py:333-339`).
4. Readback retrieval, **outside** the fetch budget.
5. The whole pre-pass is capped at 12 s (row 26).

A code comment reports p50 4,072 ms and p95 7,740 ms on network turns (`app/main.py:6196-6199`). That figure is FDU.

| # | Hypothesis | Evidence | Label | Plaus. | Cheapest experiment |
|---|---|---|---|---|---|
| 1.1 | **A serial live-lookup chain sits before the first token.** Readback is outside the 8 s budget, so the true bound is 12 s. | above | VIC; FDU (latency) | High | Replay fixed Fast questions through dev `/chat`. Attribute time with `knowledge_fast_lookup_seconds{stage}`, `knowledge_blocked_seconds` and engine TTFT from the trace. |
| 1.2 | **Cross-user stall.** Any LONG prompt closes NORMAL until its first token, and Fast turns arriving then wait. | `app/admission.py:606-609` | VIC | High | Fast turns at concurrency 4, with and without one concurrent 200K request. |
| 1.3 | **CPU contention on the head.** One uvicorn process with no `--workers` (`orchestrator/Dockerfile.cuda:88`), CPU work in threads, one extraction thread. | `app/config.py:2609-2612` | VIC (structure); FDU (impact) | High | Lookup turns at concurrency 1, 8 and 16. Extraction stage, `search_stage_timeout_total{stage="extract"}`, and event-loop lag (py-spy on dev). |
| 1.4 | **Forced web search on Fast runs the full search engine with no overall deadline.** Up to 8 sources, with per-fetch (8 s) and per-extract (5 s) bounds only. | `app/engines/search.py:48-56`, `:714-770`, `:2156-2280` | VIC; HYP (tail) | Medium | 20 Fast turns with web search on: TTFT p50/p95 and the slowest stage. |
| 1.5 | **Cold indexes after a deploy**: 2.5–3.7 s against about 0.7 s warm. | `app/config.py:1357-1368` (comment) | FDU | Medium | The first 5 Fast turns after a dev restart, with warm-on-start on and off. |
| 1.6 | **Repeated `/tokenize` calls at large histories.** Compaction measures, then the fit counts again. | `app/compaction.py:118-137`; `app/context.py:938-950` | HYP | Low–Med | Count `/tokenize` calls per turn at 10K and 100K histories. |
| 1.7 | **The NORMAL lane holds at most 10 generations.** | `app/config.py:2579` | HYP (real concurrency unknown) | Medium | Peak `llm_admission_wait_seconds` and engine `requests_running`, read-only. |

### Symptom 2: informal prompts are misunderstood

| # | Hypothesis | Evidence | Label | Plaus. | Cheapest experiment |
|---|---|---|---|---|---|
| 2.1 | **Independent interpreters with no shared reading of the turn** (§2.2). | §2.2 | VIC (multiplicity); HYP (cause) | High | Offline: run `fast_lane.decide`, `freshness.classify_offline` and `intent.decide` over a labelled informal set (typos, Hinglish/Gujlish, terse follow-ups, pastes). Then run the router-backed deciders against the dev router. Build a disagreement matrix. |
| 2.2 | **The routers see too little context.** `decide()` sees 2 turns and 2,000 characters. The Salesforce route adds the previous user turn only for messages of 12 words or fewer, capped at 400 characters. | `app/engines/orchestrate.py:79`, `:107`; `app/engines/router.py:117-121` | VIC | Medium | The same set, with and without the history that gets dropped. |
| 2.3 | **Timeouts silently fall back to defaults**: freshness at 0.6 s, `decide()` errors become no tools, route errors become `rag`. | `app/freshness.py:266`; `app/engines/orchestrate.py:162-164`; `app/engines/router.py:150` | VIC | Medium | `freshness_router_seconds{outcome="timeout"}` at concurrency 8. |
| 2.4 | **The intent lexicon misses typed variants.** | `app/artifacts/intent.py:52-62`; `orchestrator/tests/test_artifact_prompt_understanding.py` | HYP | Medium | The existing intent tests plus new informal cases. |

### Symptom 4: Max does not consistently produce deep, verified research

| # | Hypothesis | Evidence | Label | Plaus. | Cheapest experiment |
|---|---|---|---|---|---|
| 4.1 | **Deep Research runs only with the explicit toggle.** Otherwise Max depth depends on `decide()` choosing agent or search. | rows 18, 21 | VIC | High | 30 research-shaped Max prompts, toggle on and off. Record the route, `meta.auto`, distinct sources and verification steps. |
| 4.2 | **The default Max chat path does not verify claims.** It is either the Max loop (needs at least 2 sections or 3 elements, `app/core/max_loop.py:116-135`) or best-of-3 judged on the first 4,000 characters. | `app/core/best_of.py:29`, `:189` | VIC | High | The same prompts. Count cited claims that trace to sources actually fetched. |
| 4.3 | **Deep Research capacity**: 2 concurrent runs, 1 per user, a 45 s queue then refusal, a 600 s run timeout. | `app/config.py:1584-1636` | VIC; HYP (hit rate) | Medium | Two simultaneous runs on dev. Record the stop reason, source count and whether verify ran. |
| 4.4 | **Narrow search reach.** SearXNG is the default; general queries reach few engines. | `app/config.py:1171-1173`; `app/search/searxng.py:28-34` (comment) | FDU | Medium | Log unresponsive engines and result counts per query. |

### Symptom 5: long answers and implementation requests are incomplete

| # | Hypothesis | Evidence | Label | Plaus. | Cheapest experiment |
|---|---|---|---|---|---|
| 5.1 | **Agent synthesis is a single call** with no continuation and no finish-reason check. Its 6,000/12,000 ceiling is floored to the shared 65,536 pool. At Think/Max, "build or design" requests go to the agent. | `app/engines/agent.py:48`, `:807-830`; `app/engines/orchestrate.py:45-47` | VIC | High | 10 multi-file implementation requests at Think and Max. Record the route, the synthesis finish reason and completeness against a checklist. |
| 5.2 | **Continuation stops early.** Possible causes: re-admission refusal (`app/continuation.py:752-772`), the per-call wall clock (`:851-854`), no progress (`:877`), or seam-repeat detection. | as cited | VIC | Medium | 20K, 50K and 100K-token deliverables. Tally `meta.continuation.stop_reason`. |
| 5.3 | **The loop guard fires falsely** on repetitive code or tables. | `app/core/answer_guard.py:16-45` | HYP | Medium | Run recorded long answers through `AnswerGuard` offline. |
| 5.4 | **Truncated JSON or artifact compositions** (the thinking pool). | `app/llm.py:2036-2051` | VIC (logged) | Low–Med | Grep dev logs for `json_completion: ... truncated` over an artifact test run. |

### Symptom 6: the two DGX systems may not be used well

| # | Hypothesis | Evidence | Label | Plaus. | Cheapest experiment |
|---|---|---|---|---|---|
| 6.1 | **The main model is TP=2 across both nodes**: one failure domain with no replica. Router, embedding and reranker sit on the head. | `docs/CLUSTER.md:10-41`, `:148-150`; `compose/compose.dgx-spark.yaml:271-439` | FDU (placement) | High | Read-only: per-node memory and GPU use, containers and engine metrics. Then, on dev only, compare TP=2 with one replica per node at concurrency 1, 4, 10 and 32. |
| 6.2 | **Long-context work is serialised**: LONG max 1, KV 8 GiB per node, about 1.66M-token pool. | `app/admission.py:10-32`; `app/kv_budget.py:15-26` | VIC (code); FDU (measured layout) | High | Combine with 3.2: KV usage and queueing under a 200K + Fast mix. |
| 6.3 | **App CPU work runs only on the head** (see 1.3). | 1.3 | VIC | Medium | As 1.3, plus per-core CPU. |
| 6.4 | **Raw-engine traffic from the second tenant is outside admission.** | `app/admission.py:54-55` | VIC (documented) | Medium | Per-client request rate from engine metrics, if they can tell clients apart. |
| 6.5 | **Prefix caching is off**, so shared prompts and continuations re-prefill. | `app/kv_budget.py:17-19`; `docs/CLUSTER.md:176-177` | FDU | Medium | A/B on dev with caching on, then a soak with `scripts/cluster-soak.py`. |
| 6.6 | **Whisper and OCR on the worker share the TP pair's GPU.** | `compose/compose.whisper.yaml:1-14`; `compose/compose.ocr.yaml:1-15` | FDU (runtime) | Medium | Chat decode tok/s on dev, with and without an ASR clip running. |

**Ranked starting set for Phase B:**
1. The W in force on each route (3.1, 3.7). This is cheap and decides whether the leading hypothesis is live.
2. The mixed-load admission run (3.2, 1.2, 6.2). One run covers three symptoms.
3. Per-segment timings at 250K (3.3) and multi-turn compaction recall (3.4).
4. Fast lookup stage attribution at concurrency 1, 8 and 16 (1.1, 1.3).
5. Agent-route completeness (5.1) and a Max route census (4.1, 4.2).

---

## 6. Stale documentation claims

The right column cites code at 6ae978a3.

| Stale claim (location) | Code says |
|---|---|
| `docs/00-INVENTORY.md:18` "504 paths"; `:33` "compose/ 6 files"; `:30-44` has no gateway, knowledge-service, monitoring, evaluation, conformance, e2e, brain, ops or tools rows | 4,374 tracked paths; `compose/` has 25 files; all those areas exist |
| `docs/01-codebase/README.md:7` "orchestrator 118 files / 21,377 LOC" | `orchestrator/app`: 297 .py, about 238k lines |
| `docs/01-codebase/README.md:21,76` "unauthenticated", "Authentication (there is none)"; `docs/README.md:53` "no real application login/session boundary" | `app/auth.py:33` (`ts_session`), `app/authn/` (15 modules); `/chat` returns 401 before the body (`app/main.py:4157-4158`) |
| `docs/01-codebase/README.md:71` "SQLite app state" | PostgreSQL via psycopg (`app/db.py:3-4`, `:66`) |
| `docs/01-codebase/README.md:45,67,72,73` 13–15 engines, 32 components, 14 lib modules, 10 route handlers | 26 engine files, 135 components, 62 lib modules, 45 `app/api/**/route.ts` |
| `README.md:151` "launcher/tests 355 tests" | CI floor 450 (`.github/workflows/pipeline.yml:395`); 695 `def test_` |
| `README.md:172` "db.py … 7 migrations" | `_MIGRATION_V44` (`app/db.py:3080`) |
| `README.md:178,970` "136 files, ~3,150 tests" | About 488 modules, 9,375 `def test_` (static) |
| `README.md:184` "page.tsx (the app)" | `frontend/app/page.tsx` is a 12-line wrapper; the app is `frontend/components/ChatApp.tsx` |
| `README.md:185-187,972` "39 components / 38 lib / 122 test files" | 135 / 62 / 216 |
| `README.md:142-200` map omits gateway, knowledge-service, monitoring, evaluation, conformance, e2e, ops, tools | All tracked |
| `docs/ARCHITECTURE_CURRENT.md:19` "Next.js 14" | `frontend/package.json:20` `^16.3.6` |
| `docs/ARCHITECTURE_CURRENT.md:21` "POST /chat … main.py:361" | `app/main.py:4140` |
| `docs/ARCHITECTURE_CURRENT.md:66-68` "Auth. None, deliberately" | `app/main.py:4157-4158`; `/chat/stop` uses `_require_viewer` (`app/main.py:7626`) |
| `docs/ARCHITECTURE_CURRENT.md:89-91` "startStream … proxied verbatim" | Translated: `frontend/lib/orchestrator.ts:294-386` drops `current_text`/`dataset` and derives `message` |
| `docs/ARCHITECTURE_CURRENT.md:106`; `docs/FLOWS.md:656,673` "exactly one meta per turn" | Several `meta` events since V29 (`frontend/lib/streams.ts:956-984`) |
| `docs/ARCHITECTURE_CURRENT.md:108` "answers persist via /history" | The server stores the assistant answer before `done` (`app/main.py:3812-3870`, `:7093`) |
| `docs/ARCHITECTURE_CURRENT.md:148-152` effort set `{fast, low, medium, high}`, `llm.py:215-238` | `fast\|think\|max` with aliases (`app/llm.py:810-821`); thinking body at `app/llm.py:1304-1331` |
| `docs/ARCHITECTURE_CURRENT.md:163-164` "no thinking token budgets" | They exist, gated by `THINKING_BUDGET_MODE=client` (`app/llm.py:1284-1301`; `app/config.py:958-995`) |
| `docs/CONFIG.md:32,56` 65,536 floor means "thinking + answer always fit" | A requested ceiling only (§2.5; `app/context.py:958-959`, `:1028-1030`) |
| `docs/CONFIG.md:109` `MAIN_MODEL_DEFAULT_MAX_OUTPUT_TOKENS` = "answer reservation, fast" | The Fast ceiling is `app/core/answer_sampling.py:109` / `app/engines/chat.py:516` / `app/fast_lane.py:49`. The setting is a fallback (`app/context.py:917`) and the compaction reservation. |
| `docs/CONFIG.md:110` `MAIN_MODEL_HIGH_MAX_OUTPUT_TOKENS` = "answer reservation, think and max" | Think/Max is hard-coded at 16,000 (`app/engines/chat.py:519-521`). The setting is read only at `app/core/sf_intel/budget.py:77-81` and `app/health.py:697`. |
| `docs/FLOWS.md:44-51` and its §3 diagram (dispatch list) | Omits artifact, unsupported-visual, video, image follow-up, small-talk lane and `sf_live`. Order is at `app/main.py:6413-6978`. |
| `docs/FLOWS.md` §4 "Normal chat — No web" | The assistant chat branch runs the knowledge pre-pass, which can do a live lookup at Fast (`app/living_knowledge.py:1770-1863`) |
| `docs/01-codebase/frontend-api-contracts.md:22-37` "Authentication: NONE … cookie forwarding is dead code"; `:512` "/chat/stop main.py:712-722, Auth NONE" | `app/main.py:7622-7641` (owner-scoped) |
| `docs/01-codebase/frontend-api-contracts.md:49` "/api/chat/active: every failure becomes 200" | 502 `NETWORK_ERROR` (`frontend/app/api/chat/active/route.ts:37-41`) |
| `docs/01-codebase/frontend-api-contracts.md:48,94` "/api/chat 185 LOC; errors 400×2, 502×2" | 413 (`route.ts:296-312`), 499, 504, 422 (`route.ts:358`, `:368`, `:392-399`) |
| `docs/01-codebase/orchestrator-context.md:19,69-70,159,179,341` (compaction 360 lines, `main.py:130/133`, `compaction.prepare`, `fit_request` at `context.py:205-275`) | `app/compaction.py` is 642 lines; `app/main.py:2001`, `:2412`; `prepare_deferred` (`app/main.py:5915`); `fit_request` at `app/context.py:893-1031` with a send-first path (`:919-947`) |
| `frontend/components/ModelPicker.tsx:6-16` "four levels" | Three levels (`:25`) |
| `frontend/app/api/chat/route.ts:5-9` orchestrator shape `{message, session_id, image_base64}` | About 20 forwarded fields (`frontend/lib/orchestrator.ts:338-385`) |
| `app/main.py:1416-1417` comment "/chat and /reports* remain auth-free" | `app/main.py:4157-4158` |
| `app/config.py:2388`, `compose.yaml:522`, `app/publicapi/registry.py:216` "GEN_WALL_CLOCK_S (4,200 s)" | Default 1,800 (`app/config.py:969`) |
| `app/config.py:960-963` "262k window minus prompt applies" | The window is configurable to 1,048,576 (`launcher/techsara_cli/environment.py:337`) and resolved from `/tokenize` (`app/context.py:767-779`) |
| `app/config.py:1802-1805` full-window budget 262144 − 16384 − 8192 | Chat sizing uses a 512 margin (`app/context.py:916`; `app/compaction.py:96`). The 8,192 margin is used only by `app/health.py:698` and `app/core/sf_intel/budget.py:85`. |
| `app/engines/chat.py:526-530` Fast caps "up to 64,000 across segments" | The total is `MAX_LOGICAL_OUTPUT_TOKENS`, 1,000,000 (`app/core/answer_sampling.py:112-123`) |
| `app/engines/router.py:1-7` "Qwen3-4B, fallback gpt-oss-120b" | `ROUTER_MODEL` default `Qwen/Qwen3-VL-8B-Instruct-FP8` (`app/config.py:185-187`); the fallback is the main model (`app/engines/router.py:131-142`) |
| `app/health.py:684-704` docstring "served max_model_len is the truth" | `max_input_tokens` is still computed from the configured window (`:690`, `:700-704`) |
| `frontend/lib/contextMeter.ts:38-46` "mirrors the orchestrator defaults (131072 …)" | Default 262,144 (`app/config.py:941-943`) |
| `orchestrator/scripts/validate_long_context.py:2,41-44` "prove the 262,144-token window"; 240K "the real working ceiling" | Served window recorded as 1,000,000 (`app/publicapi/registry.py:640-641`) |
| `docs/CLUSTER.md:25-41,148-150` "OCR on Node 1; Node 2 carries nothing else" | `compose/compose.ocr.yaml:1-15` and `compose/compose.whisper.yaml:1-8` are designed for the worker (runtime not verified) |
| `compose.yaml:5-10` "TECHSARA_STACK names the project" | The launcher always passes `--project-name sf-local-ai` (`launcher/techsara_cli/compose.py:91`) |
| `knowledge-service/compose.fragment.yaml:1` "Add this service to docker-compose.yml" | Not in any compose file |
| MASTER_PROMPT §4 "knowledge-service (LanceDB RAG, DuckDB)"; §10 candidate `.claude/` | See §1.2; `.claude/` is absent |

---

## 7. Open questions

Agents may not read `.env`, so the production-value questions need the operator or `/health`.

1. **Which W does production size against on each route?** Is `MAIN_MODEL_MAX_LEN` set, and does `/tokenize` report 1,000,000? This decides whether 3.1 explains the ~250K symptom or another cause does (LONG lane, compaction, the meter, a different route).
2. **When was the ~250K symptom first reported, and what does "~250K" refer to?** Compare the date with the 262,144 → 1,000,000 window change (commit 25db1181, 2026-08-29). The figure may come from the meter, a pasted document, an upload or a `/v1` prompt.
3. **Should compaction measure the real request?** That means the engine's system prompt, the grounding and the effort-specific reservation (65,536 at Think/Max), instead of history only with 8,192.
4. **Is re-thinking on every Think/Max continuation segment intended?** No `answer_plan` is passed (`app/engines/chat.py:879`).
5. **Which values does production run?** `GEN_WALL_CLOCK_S` and `LLM_REQUEST_TIMEOUT` (1,800 or 4,200), `MAX_OUTPUT_TOKENS`, `CLUSTER_KV_CACHE_MEMORY_GIB`, `SEARCH_ENABLED`, `ANSWER_SAMPLING_PROFILE`, `CPU_POOL_WORKERS`.
6. **Does the production deploy path use the same `-f` chain and env-file order as `cli._compose_files`?** `scripts/deploy.sh` is blocked by the guard and was not read.
7. **Does the pinned vLLM build return `max_model_len` from `/tokenize`?** And does the Qwen3.6 template think on Fast when `enable_thinking` is absent (capability gate off)?
8. **Does Next 16 abort `req.signal` when the browser disconnects?** Does `gen.task.cancel()` reach the in-flight vLLM stream and Max's child tasks?
9. **What should the client do?** Should Stop be acknowledged, with a pending cancel recorded by `intent_id`? Should a clean EOF without `done` count as interrupted? Should there be a heartbeat-based stall detector?
10. **What does a generation's event buffer cost in memory at the ~1M-output goal (§20)?** Is attach replay of a huge backlog still acceptable?
11. **Which edge and Next settings apply?** Which cloudflared/edge idle and buffering settings are in force, which undici header and body timeouts apply to the `/api/chat` proxy fetch, and is Next compression skipped for `/api/chat`?
12. **Request defaults.** Is it intended that regenerate and edit use the current effort, not the original turn's? Do resumed snapshots or non-browser callers depend on the server defaults (`salesforce` / `think` / `off`)?
13. **Worker node and Phase B capacity.** Are OCR and whisper running on the worker? Is the second tenant still sending raw-engine traffic? Can a dev engine serve 250K+ prompts for Phase B, or must those runs use the guarded low-traffic window?
14. **Do the suites with no CI reference run anywhere?** These are the gateway, engine-controller, promtool, evaluation, brain and `ops/autopilot` tests.
15. **Integrate knowledge-service (ADR-0001) or retire it?** Does the operator expect a sandboxed code interpreter? Should the dead uv scaffold and `docs/00-INVENTORY.md` be removed or regenerated under §27?
16. **How should discovery read production-touching files?** The guard blocks even read-only Bash on `scripts/deploy*.sh` and the long-context probe. Is the Read tool the intended route, or is an allowance needed?
