# Task board

Status values: READY, IN_PROGRESS, BLOCKED, DONE. Work the highest READY task in phase order (§8); finish IN_PROGRESS work first. New ideas go to the backlog at the bottom with a priority. Operator decisions (§0) apply to every task: integrate on `autopilot/dev`; never touch `main` or production; `dev` only through the gate at the finish line.

## Phase 0 — Autopilot bootstrap (§5)

| ID | Task | Status | Evidence |
|---|---|---|---|
| P0-01 | Master prompt saved and copied verbatim; operator decisions recorded in both copies | DONE | `cmp` identical (decision 10 added by the operator, committed 6ae978a3) |
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
| P0-13 | Watch the draft PR `autopilot/dev → dev` CI run; fix any failure caused by the Phase 0 changes (conftest contract, compose parameterization, docs, ops/autopilot tests) | DONE | No failure came from Phase 0. Each of three runs hit one pre-existing timing flake: run 37106060235 (`test_health_dependency_cache`, fixed 49489640), run 37108039568 (`test_video_media_hardening`, fixed 242bd04d), run 37109933435 (`test_voice_sessions.py:890` memory peak, a known flake; `gh run rerun --failed`). `gh pr checks 98` on 242bd04d: every check pass, `CI passed` pass |
| P0-14 | Complete `CLOCK_SKEW_SENSITIVE` in `orchestrator/tests/conftest.py` from a full orchestrator run against the remote test database: run strict (TEST_DATABASE_REMOTE unset) to list failures, prove each fails on untouched `main` too, add only timestamp-vs-server-now tests, re-run with TEST_DATABASE_REMOTE=1 | DONE | ba1f619d (merged d0887579): 24 exact node ids, guarded by a new test. Strict run `3 failed, 16982 passed`; the 23 webhook/Files tests fail identically on `main` (`23 failed, 344 passed`); remote run `16962 passed, 49 skipped, 30 xfailed, 1 xpassed`, 0 failed. The node clock offset changes sign (−36 to +54 ms on 2026-10-03), so which tests fail depends on the hour |
| P0-15 | Independent review of the guardrails, runner and dev gate by a fresh-context subagent (bypasses, false positives, failure modes). Fix source + tests in ops/autopilot; the autopilot cannot reinstall its own guardrails, so add a NEEDS_HUMAN item asking the operator to run `ops/autopilot/install.sh` | DONE (source; not installed) | Workflow of 3 reviewers, 3 adversarial verifiers and 1 fixer: 26 findings confirmed, 25 fixed (95622dc6, 435a7196, df091d61). A second fresh review returned FIX_FIRST on one regression, fixed in a8ad8b28; merged b37e961e. `ops/autopilot/tests`: `82 passed, 852 subtests passed`. Install: NH-007. Server-side item: NH-008. Details are host-only: `~/.llm-autopilot/agent/private/p0-15-review-2026-10-03.md` |
| P0-16 | Guard bypass sweep that the P0-15 review did not finish: quoting, `eval`, here-docs, base64, symlinks into protected paths, `git push`/`gh api`/`docker compose` spellings, hook matcher coverage, hook latency against the 60 s timeout. Write probes as `decide()` cases in `ops/autopilot/tests/test_guard_hook.py`, never as live commands (the auto-mode classifier cut the first attempt off). Fix source + tests; the operator installs (NH-007) | READY | P0-15 guard reviewer's coverage note |
| P0-17 | Remaining P0-15 review notes: count repeated SIGTERM-interrupted cycles toward the crash cap; gate hardening (`GIT_NO_REPLACE_OBJECTS=1`, `core.hooksPath=/dev/null`, assert `--git-common-dir`); the guard denies moving or deleting `~/.llm-autopilot/agent` and directories that hold guard files; `install.sh` writes settings atomically and traps Ctrl-C in `--restart-after-cycle`; one overall deadline for the push-path secret scan | READY | Second review's non-blocking list (host-only report) |

## Phase A — Discovery (§10)

| ID | Task | Status | Notes |
|---|---|---|---|
| A-01 | Read repository instructions and the docs index; inventory authored code and configuration | DONE | `DISCOVERY.md` §1 (91421fb1) |
| A-02 | Trace the request path end to end (input → effort → frontend proxy → mode → intent → sources → context → execution → model → rendering) with file:line evidence | DONE | `DISCOVERY.md` §2; code evidence only, about 60 citations spot-checked |
| A-03 | Classify features: working, incomplete, duplicated, bypassed, documented only | DONE | `DISCOVERY.md` §4, 49 rows; "working" means wired in code, not runtime-verified |
| A-04 | Record configuration precedence: environment, files, launcher overrides, per-request parameters | DONE | `DISCOVERY.md` §3; production environment values were not read (rule) |
| A-05 | Capability registry draft per endpoint: served maximum length, completion limits, KV pool, chat template, reasoning parser | DONE | `CONTEXT_CAPACITY.md` (c418519f), metadata only: main serves W = 1,000,000 with a KV pool of 1,663,201 tokens per rank, so one ~1M sequence at a time. Not a verified capability (Phase E) |
| A-06 | Investigate the reported symptoms 1–6 (§4) to hypotheses with evidence, not fixes | DONE | `DISCOVERY.md` §5, each with the Phase B experiment that would confirm or reject it |

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
| P1 | Known CI flake `orchestrator/tests/test_voice_sessions.py:890` (two-hour memory peak; 24.2 MiB growth on run 37109933435). Find the allocation or measure the noise; until then use `gh run rerun --failed` on PR runs. |
| P1 | The model manifest's `context_limit` disagrees with the served windows (main, router, embed, reranker), and the router runs without a tool parser (`CONTEXT_CAPACITY.md`). Feed the Phase D capability registry from what is served. |
| P2 | CI runs no tests for `ops/autopilot`, `gateway`, `monitoring/engine-controller` or `evaluation` (`DISCOVERY.md` §7). Adding them changes `.github/`, which the gate holds until the operator approves the tree. |
| P2 | `knowledge-service` is in no compose file and the orchestrator never calls it; a dead uv scaffold sits at the root; `docs/00-INVENTORY.md` is stale (`DISCOVERY.md` §6). |
| P2 | The `-p ci_statvfs84` pytest plugin named in the agent rules is not in the repository. |
| P2 | Known flaky frontend test `tests/voice-session-recorder.test.tsx` (failed the first attempt of run 37092252160). |
| P3 | `validate_long_context.py` defaults to the production engine; require an explicit target. |
| P3 | Cycle 2 scratch: `/tmp/p013-*`, `/tmp/p014`, `/tmp/p015*`, `/tmp/a05` can be deleted; a test-only database `llmdev_p013_test` exists on the test server (left in place: databases are never deleted). |
