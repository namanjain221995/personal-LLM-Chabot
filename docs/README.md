# Architecture Review & Technical Due Diligence

> **2026-08-10 — app state moved from SQLite to PostgreSQL.** [`data-model.md`](01-codebase/data-model.md)
> and the CHANGELOG are current. Other pages here still describe `/data/app.sqlite3`
> and carry a banner saying so; their DuckDB, LanceDB, engine and frontend content is
> unaffected.


**System:** TechSara — a fully local Salesforce AI analytics + chat platform running on an NVIDIA DGX Spark
workstation.
**Audited:** 2026-07-31 · **251 files · 43,189 LOC**, every in-scope file read in full.
**Result:** 38 findings — 1 P0, 7 P1, 22 P2, 8 P3 — plus complete module documentation and a 24-diagram suite.

---

## The one-paragraph version

This is a well-engineered codebase with **one deployment-level flaw that dominates its entire risk profile**.
The security-sensitive modules (`archive.py`, `report_paths.py`, `net.py`) are written to a standard above what
we normally see; there are 1,141 automated tests and **all of them pass**; there is not a single `TODO` or
`FIXME` in 43k lines. But the application has **no authentication of any kind**, and every port — including the
four GPU inference servers — is published on `0.0.0.0`. The code's own comment
(`orchestrator/app/auth.py:17-20`) says this posture is *"NOT fine if the port is published to a network you do
not control — see the compose port bindings"*. The compose port bindings are exactly that. **Fixing it is a
one-line change per service.** Four of the five top risks are fixable in under two days combined.

---

## Reading order

**If you have 5 minutes** → [`03-report/IMPROVEMENT-REPORT.md` §1 Executive summary](03-report/IMPROVEMENT-REPORT.md)

**If you have 30 minutes**
1. [`03-report/IMPROVEMENT-REPORT.md`](03-report/IMPROVEMENT-REPORT.md) — §1 summary, §2 scorecard, §3 what's good, §7 this week
2. [`02-diagrams/render/21-network-ports.svg`](02-diagrams/render/21-network-ports.svg) — the exposure picture in one image
3. [`03-report/QUICK-WINS.md`](03-report/QUICK-WINS.md) — everything fixable in under a day

**If you are the engineer doing the work**
1. [`01-codebase/CRITICAL-PATHS.md`](01-codebase/CRITICAL-PATHS.md) — the 8 end-to-end flows, `file:line` at every hop
2. [`03-report/JIRA-BACKLOG.md`](03-report/JIRA-BACKLOG.md) — import-ready tickets with Given/When/Then criteria
3. The module doc for whatever you are touching, in [`01-codebase/`](01-codebase/)

**If you are new to the codebase** → [`01-codebase/README.md`](01-codebase/README.md), then
[`00-INVENTORY.md`](00-INVENTORY.md), then diagrams 01 → 02 → 06 → 07.

---

## What is here

| Path | What it is |
|---|---|
| [`00-INVENTORY.md`](00-INVENTORY.md) | Every one of the 251 files with LOC, purpose, criticality and risk flag; dependency inventory; entrypoint map; full config surface; dead-code analysis |
| [`01-codebase/`](01-codebase/) | Module documentation — 13 documents, each module on a fixed 10-section schema |
| ├─ [`CRITICAL-PATHS.md`](01-codebase/CRITICAL-PATHS.md) | **The most useful document here.** 8 flows traced end to end with `file:line` at every hop |
| ├─ [`frontend-api-contracts.md`](01-codebase/frontend-api-contracts.md) | Every route, every status code, and all 8 SSE events with exact payloads on both sides |
| ├─ [`security-model.md`](01-codebase/security-model.md) | Auth, network exposure, the four guard modules assessed honestly, secret handling, STRIDE table |
| ├─ [`data-model.md`](01-codebase/data-model.md) | **PostgreSQL app state** (2026-08-10, was app.sqlite3) · DuckDB warehouse · LanceDB — keys, indexes, retention, what is not cleaned up |
| ├─ [`test-map.md`](01-codebase/test-map.md) | All 83 test files, what they assert, the coverage gaps, and the 10 highest-value tests to add next |
| └─ *(orchestrator-engines / -core / -context / -search, frontend, sync-worker, infra-docker-compose)* | |
| [`02-diagrams/`](02-diagrams/) | 24 PlantUML diagrams — [`src/`](02-diagrams/src/) sources, [`render/`](02-diagrams/render/) SVG + PNG, [`README`](02-diagrams/README.md) with draw.io import steps, [`_STYLE.md`](02-diagrams/_STYLE.md) |
| [`03-report/`](03-report/) | [`IMPROVEMENT-REPORT.md`](03-report/IMPROVEMENT-REPORT.md) · [`FINDINGS.csv`](03-report/FINDINGS.csv) · [`JIRA-BACKLOG.md`](03-report/JIRA-BACKLOG.md) · [`QUICK-WINS.md`](03-report/QUICK-WINS.md) |
| [`04-VERIFICATION.md`](04-VERIFICATION.md) | Every self-check run against this audit, with actual output |
| [`ASSUMPTIONS.md`](ASSUMPTIONS.md) | Every judgement call, including **findings that were investigated and dropped** because the code was correct |
| [`_evidence/`](_evidence/) | Raw per-subsystem evidence notes the documents were written from — working material, kept for traceability |

---

## Findings at a glance

| Severity | Count | Headline |
|---|---:|---|
| **P0** | 1 | `SEC-01` No authentication + six ports published on `0.0.0.0` |
| **P1** | 7 | `sql_guard` bypass · warehouse stored entirely as text (wrong rankings) · blocking DuckDB call on the event loop · SSRF DNS-rebinding window · fail-open ownership check · unbounded request body · no CI |
| **P2** | 22 | Prompt injection · plaintext `.env` backup · dead AWS config · orphaned tables · no PK/index on `Id` · unpinned deps · no correlation IDs · raw exception text streamed to the browser · no router timeout |
| **P3** | 8 | Undocumented `MOCK_MODE` · no engine `Protocol` · dead `is_safe_select` · two god-files · four tests that cannot fail |

Also worth knowing: the orchestrator image is the only one that runs as **root** (`SEC-11`), `requirements-dev.txt`
omits a package needed at import time so a clean-host test run fails at collection (`DX-03`), and **18 of the 43
documented env vars reach no container at all** because compose has no `env_file:` (`DX-04`).

Machine-readable: [`03-report/FINDINGS.csv`](03-report/FINDINGS.csv).

### Two findings worth reading in full

- **`SEC-07`** — a genuine `sql_guard` bypass. `SELECT E'\'' , 1; DROP TABLE t` passes the guard *including its
  multi-statement check*. Confirmed by executing it against the real module. It is **not currently
  exploitable**, because DuckDB is opened `read_only=True, enable_external_access=False` and refused every
  payload — but the guard's documented promise is false and the whole safety argument now rests on two
  connection flags never regressing.
- **`DATA-04`** — every Salesforce value is stored as `VARCHAR`, so `ORDER BY Amount DESC` sorts `9000` above
  `10000` and `MAX(Amount)` returns the wrong record. `SUM` fails loudly and self-corrects; **ranking fails
  silently**. Verified against DuckDB 1.5.4. For a product whose purpose is answering questions about numbers,
  this is the most consequential correctness defect found.

---

## What was verified, not assumed

- **Tests were executed**, not just read: orchestrator **800 passed** (41.6 s) · sync-worker **104 passed**
  (1.1 s) · frontend **237 passed** (16 files) — **1,141 passing, 0 failing**.
- **The `sql_guard` bypass was executed** against the real module, and its payloads were then run against a
  DuckDB handle configured exactly as the engine configures it, to establish that defence-in-depth holds.
- **The `VARCHAR` consequences were measured** in DuckDB 1.5.4, not reasoned about.
- **All 24 diagrams pass `plantuml -checkonly` and render** to SVG and PNG — no `RENDER-SKIPPED` placeholders.
- **Five plausible findings were investigated and dropped** because the code proved correct — model-output XSS,
  the DuckDB replacement-scan "bypass", sync watermark data loss, an unbounded generation registry, and
  module-level dead code. See [`ASSUMPTIONS.md#a12`](ASSUMPTIONS.md).

Nothing outside `docs/` was created, modified or deleted. No dependency was installed. No git state was changed.
