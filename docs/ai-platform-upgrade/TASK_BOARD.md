# Task board

Status values: READY, IN_PROGRESS, BLOCKED, DONE. Work the highest READY task in phase order (§8); finish IN_PROGRESS work first. New ideas go to the backlog at the bottom with a priority. Operator decisions (§0) apply to every task: integrate on `autopilot/dev`; never touch `main` or production; `dev` only through the gate at the finish line.

## Phase 0 — Autopilot bootstrap (§5)

| ID | Task | Status | Evidence |
|---|---|---|---|
| P0-01 | Master prompt saved and copied verbatim; operator decisions recorded in both copies | DONE | `cmp` identical |
| P0-02 | Facts (§5.1) | DONE | `CURRENT_STATE.md` |
| P0-03 | Production map, isolation, dev worktree (§5.2) | DONE | AD-001, AD-002 |
| P0-04 | Baseline tag | DONE | `baseline/pre-upgrade-2026-10-03` → `3c75af1c` |
| P0-05 | Claude Code behaviours verified against 2.1.288 | DONE | `RESEARCH_LEDGER.md` |
| P0-06 | Guard layers (§5.4) with host details kept private | DONE | `ops/autopilot/tests/test_guard_hook.py`; real-session test of each layer (IMPLEMENTATION_STATUS) |
| P0-07 | State files and CLAUDE.md section (§5.3) | DONE | this folder; `/CLAUDE.md` |
| P0-08 | Compose isolation (decision 7a) | DONE | AD-007; 12/12 production renders identical |
| P0-09 | Test database on the worker and the conftest contract (decision 7b) | DONE | AD-008; NH-003 |
| P0-10 | Dev gate `merge_to_dev.sh` and guard rules (decision 7c) | DONE | AD-009; `test_merge_to_dev.py` |
| P0-11 | Runner, cycle prompt, status and install scripts, systemd unit (§5.5) | DONE | `test_runner.py`; systemd test (IMPLEMENTATION_STATUS) |
| P0-12 | Push `autopilot/dev`, draft status PR, start the runner, confirm the first cycle (§5.6) | DONE | IMPLEMENTATION_STATUS |
| P0-13 | Watch the draft PR `autopilot/dev → dev` CI run; fix any failure caused by the Phase 0 changes (conftest contract, compose parameterization, docs, ops/autopilot tests) | READY | `gh pr checks`; a push to autopilot/dev re-runs it |
| P0-14 | Complete `CLOCK_SKEW_SENSITIVE` in `orchestrator/tests/conftest.py` from a full orchestrator run against the remote test database: run strict (TEST_DATABASE_REMOTE unset) to list failures, prove each fails on untouched `main` too, add only timestamp-vs-server-now tests, re-run with TEST_DATABASE_REMOTE=1 | READY | Phase 0 measured one (test_crawl_queue); the full run was interrupted at 55% |
| P0-15 | Independent review of the guardrails, runner and dev gate by a fresh-context subagent (bypasses, false positives, failure modes). Fix source + tests in ops/autopilot; the autopilot cannot reinstall its own guardrails, so add a NEEDS_HUMAN item asking the operator to run `ops/autopilot/install.sh` | READY | An interrupted Phase 0 review produced no results |

## Phase A — Discovery (§10)

| ID | Task | Status | Notes |
|---|---|---|---|
| A-01 | Read repository instructions and the docs index; inventory authored code and configuration | READY | Exclude dependency trees, data, backups, weights |
| A-02 | Trace the request path end to end (input → effort → frontend proxy → mode → intent → sources → context → execution → model → rendering) with file:line evidence | READY | Workstreams A, C, D |
| A-03 | Classify features: working, incomplete, duplicated, bypassed, documented only | READY | |
| A-04 | Record configuration precedence: environment, files, launcher overrides, per-request parameters | READY | `.env.example`, `launcher/techsara_cli/modelshape.py`, `orchestrator/app/config.py` |
| A-05 | Capability registry draft per endpoint: served maximum length, completion limits, KV pool, chat template, reasoning parser | READY | Start from `GET /v1/models` and `orchestrator/scripts/validate_long_context.py` |
| A-06 | Investigate the reported symptoms 1–6 (§4) to hypotheses with evidence, not fixes | READY | |

## Phase B — Baseline and evaluation harness (§11)

| ID | Task | Status | Notes |
|---|---|---|---|
| B-01 | Dev stack on the worker: `ops/dev/` (TECHSARA_STACK=llmdev via `stack.vars`, own generated env with synthetic secrets, memory limits, `--oom-score-adj 1000`, loopback ports); inference reuses the production endpoints at low priority | READY | The guard requires `DOCKER_HOST=ssh://<worker>` for long-lived services |
| B-02 | Synthetic evaluation set (the 9 cases of §11 plus the 7 of §13) with deterministic checks | READY | Reuse `scripts/aiq` where it fits |
| B-03 | Correlation IDs and per-stage timing through the whole path | READY | |
| B-04 | Freeze baseline numbers and tolerances in `BENCHMARK_RESULTS.md` | READY | Before any optimisation |
| B-05 | Re-measure the low-traffic window from 7+ days of request logs | READY | Prometheus `chat_request_total` |

## Backlog

| Priority | Item |
|---|---|
| P1 | Pipeline header comment says an unset `DEPLOY_ON_PUSH` means tests only; the `if:` deploys. Propose to the operator; do not edit the deploy path. |
| P2 | The `-p ci_statvfs84` pytest plugin named in the agent rules is not in the repository. |
| P2 | Known flaky frontend test `tests/voice-session-recorder.test.tsx` (failed the first attempt of run 37092252160). |
| P3 | `validate_long_context.py` defaults to the production engine; require an explicit target. |
