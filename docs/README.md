# TechSara architecture documentation

This directory contains two layers of documentation:

1. current portable-runtime and repository maps, updated 2026-08-11; and
2. a detailed due-diligence audit captured on 2026-07-31, before the portable
   launcher, Compose overlays, PostgreSQL migration, local-only network changes,
   and subsequent application work.

Start with the current layer. Historical audit pages remain useful for deep
module traces, but their line numbers, file/test counts, deployment topology,
and unresolved-finding status must not be treated as current without checking
source.

## Current reading order

| Need | Read |
|---|---|
| Install/start/stop/troubleshoot | [`../README.md`](../README.md) |
| Hardware matrix, model policy, state, fallback and upgrades | [`PORTABLE-RUNTIME.md`](PORTABLE-RUNTIME.md) |
| Two-node DGX Spark cluster (`CLUSTER_MODE=dual`): topology, generated config, scripts, benchmarks, limitations | [`CLUSTER.md`](CLUSTER.md) |
| Current files and entrypoints | [`00-INVENTORY.md`](00-INVENTORY.md) |
| Launcher plus application request flows | [`01-codebase/CRITICAL-PATHS.md`](01-codebase/CRITICAL-PATHS.md) |
| Base Compose and runtime overlays | [`01-codebase/infra-docker-compose.md`](01-codebase/infra-docker-compose.md) |
| Test entrypoints and current verification boundary | [`01-codebase/test-map.md`](01-codebase/test-map.md) |
| Current application data stores | [`01-codebase/data-model.md`](01-codebase/data-model.md) |
| Salesforce clarification, query-plan safety, context budgeting | [`06-agent-design/SALESFORCE-INTELLIGENCE-MODE.md`](06-agent-design/SALESFORCE-INTELLIGENCE-MODE.md) |

## Current deployment boundary

TechSara is no longer documented as a DGX-only monolith. The launcher supports
Apple Silicon, Linux/Windows NVIDIA, CPU-minimal, application-only, and explicit
local external-development profiles. It combines `compose.yaml` with a selected
overlay and uses native vLLM-Metal on Apple Silicon.

The supported Compose files bind frontend, orchestrator, PostgreSQL, and
optional pgAdmin publications to loopback by default. Model containers are
`expose`-only on an internal inference network. This mitigates the historical
audit's broad `0.0.0.0` host-publication finding for the supported launcher
path. The one documented exception is `CLUSTER_MODE=dual` on DGX Spark: the
`vllm` head runs with host networking for NCCL over RoCE, so its API is a host
listener at `VLLM_PORT` (`0.0.0.0` with `PUBLISH_MODEL_PORTS=true`, otherwise
the Docker bridge gateway); see [`CLUSTER.md`](CLUSTER.md#security-notes).

There is still no real application login/session boundary: `/auth/me` reports a
stable single local identity. Loopback is therefore a core security assumption,
not merely a convenient default. A reverse proxy, LAN binding, or public
publication needs a new threat model and explicit authentication/TLS/access
control.

On Mac, native model upstreams bind `127.0.0.1`; container-facing bridge
listeners bind `0.0.0.0` on ports 18100/18103/18105 so Docker can reach them.
Those bridge endpoints require a generated bearer token, strip auth/hop-by-hop
headers before forwarding, cap bodies, and do not log requests. Host firewall
policy remains relevant.

## Current documents

### Repository and codebase

| Path | Purpose |
|---|---|
| [`CLUSTER.md`](CLUSTER.md) | two-node DGX Spark cluster: what TP=2 sharding is and is not, measured interconnect and benchmarks, `CLUSTER_*` configuration, `scripts/cluster-*.sh`, failure behaviour, limitations |
| [`00-INVENTORY.md`](00-INVENTORY.md) | regenerated repository composition, entrypoints, configs, tests and generated/ignored state |
| [`01-codebase/CRITICAL-PATHS.md`](01-codebase/CRITICAL-PATHS.md) | current launcher Flow 0 plus historical detailed application flows |
| [`01-codebase/infra-docker-compose.md`](01-codebase/infra-docker-compose.md) | current base/overlay/service/network/volume/env topology |
| [`01-codebase/test-map.md`](01-codebase/test-map.md) | current suite/config/command map and honest verification results |
| [`01-codebase/data-model.md`](01-codebase/data-model.md) | PostgreSQL app state plus DuckDB/LanceDB data plane |
| [`01-codebase/frontend-api-contracts.md`](01-codebase/frontend-api-contracts.md) | browser/orchestrator route and SSE contract snapshot |
| [`01-codebase/frontend.md`](01-codebase/frontend.md) | frontend module analysis snapshot |
| [`01-codebase/orchestrator-core.md`](01-codebase/orchestrator-core.md) | orchestrator core analysis snapshot |
| [`01-codebase/orchestrator-engines.md`](01-codebase/orchestrator-engines.md) | engine analysis snapshot |
| [`01-codebase/orchestrator-context.md`](01-codebase/orchestrator-context.md) | context/compaction analysis snapshot |
| [`01-codebase/orchestrator-search.md`](01-codebase/orchestrator-search.md) | search/URL/repository analysis snapshot |
| [`01-codebase/sync-worker.md`](01-codebase/sync-worker.md) | sync worker analysis snapshot |
| [`01-codebase/security-model.md`](01-codebase/security-model.md) | historical threat assessment; re-check against current loopback/overlay/auth state |

### Historical diagrams and report artifacts

| Path | Purpose/status |
|---|---|
| [`02-diagrams/`](02-diagrams/) | 24 PlantUML sources plus rendered SVG/PNG from the 2026-07-31 topology; deployment/network diagrams predate the portable overlays |
| [`03-report/IMPROVEMENT-REPORT.md`](03-report/IMPROVEMENT-REPORT.md) | historical audit narrative and scorecard |
| [`03-report/FINDINGS.csv`](03-report/FINDINGS.csv) | historical finding register; remediation status is not automatically current |
| [`03-report/JIRA-BACKLOG.md`](03-report/JIRA-BACKLOG.md) | historical import-ready backlog |
| [`03-report/QUICK-WINS.md`](03-report/QUICK-WINS.md) | historical remediation suggestions |
| [`04-VERIFICATION.md`](04-VERIFICATION.md) | commands and evidence from the audit date, not the current portable pass |
| [`ASSUMPTIONS.md`](ASSUMPTIONS.md) | audit scope/judgment record |
| [`_evidence/`](_evidence/) | raw historical subsystem notes |

## Historical snapshot warning

The 2026-07-31 audit described a fixed DGX Spark deployment, a single Compose
file, broad host port publication, an older app-state implementation, and the
then-current source/test inventory. Later pages may still contain statements
such as:

- fixed vLLM sidecars and model IDs for every deployment;
- `/data/app.sqlite3` rather than PostgreSQL;
- host ports published on every interface;
- no Compose `env_file`, network separation, restart policy, or health checks;
- old source/test totals and old full-suite results;
- line links into files that have since changed.

Those statements are evidence of that snapshot, not current operational
instructions. Current source and the documents in the first table win when
they disagree.

## Verification status

Every suite was run on 2026-08-11 on the DGX Spark host:

| Suite | Result |
|---|---|
| Launcher (`PYTHONPATH=launcher python3 -m pytest launcher/tests -q`) | 264 passed, 266 subtests |
| Orchestrator (`pytest tests -q`, needs a test PostgreSQL) | 1014 passed |
| Sync worker (`pytest tests -q`) | 157 passed |
| Frontend (`npm test`) | 310 passed |
| Frontend types / lint | clean |

Launcher coverage is mocked except for `test_compose_overlays.py`, which renders
all 13 supported host fixtures through real `docker compose config`. No live
model download, container start, or process signal is exercised. See
[`01-codebase/test-map.md`](01-codebase/test-map.md) for the current boundary.

## Maintaining these docs

When changing launcher or infrastructure behavior:

1. update the declarative manifest/profile or Compose overlay;
2. add unit/contract tests and the relevant live platform validation;
3. update `PORTABLE-RUNTIME.md`, infrastructure, critical paths, test map, and
   inventory in the same change;
4. add a dated changelog entry without rewriting historical entries;
5. report each suite result separately with its command/date/environment.
