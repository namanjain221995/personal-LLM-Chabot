# Resume

STATUS: IN PROGRESS — Phase B (B-02, B-03, B-05 done; B-01 and B-04 next)

Updated 2026-10-03 about 18:20 UTC by autopilot cycle 7.

## Phase and active task

- **Phase:** 0 and A are done; every Phase 0 hardening task (P0-15..P0-19) is DONE in source and waits for the operator's install (NH-007). Phase B (baseline and evaluation harness, §11) is active.
- **P0-19 DONE in cycle 7:** a differential replay found regressions in four of its relaxations; fixed (33622efa, 6cebad03), reviewed (`fix_first`), fixed again (d4fa481d), merged bffc6d7a and pushed. Merged-tree suite `183 passed, 1083 subtests passed`.
- **B-02 DONE** (16-case evaluation set in `scripts/aiq/`, merged 0a32efca) and **B-05 DONE** (window 05:00–07:00 IST, merged 5c3b2920).
- **B-03 DONE** (merged 03755512): QA `fix_first` blocking item fixed (1479d31b) and the browser-chosen id removed (85746921); merged-tree focused files `138 passed, 5 skipped`, run alone. Follow-ups are in the backlog (P2).
- **Active task: B-01** (dev stack on the worker). Exact next step: read `ops/dev/stack.vars` and the compose chain, write `ops/dev/` (generated env with synthetic secrets, memory limits, `--oom-score-adj 1000`, loopback ports), bring it up with `DOCKER_HOST=ssh://<worker>` only, health-check, record in the manifest.
- **Then:** B-04 (baseline; it must first extend `scripts/aiq/run.py` for the eval set: seed history, upload attachments, deep_research, `eval_set.FAIL_TEXT`, capture cited passages).
- Operator decisions in `MASTER_PROMPT.md` §0 govern everything: integrate on `autopilot/dev`; never touch `main` or production; `dev` only through `~/.llm-autopilot/bin/merge_to_dev.sh` at the finish line.

## Branches and worktrees

- `~/work/llm-dev` on `autopilot/dev` (pushed). Draft PR #98 `autopilot/dev → dev` is the status board and runs CI.
- Merged and kept: `upgrade/a/discovery`, `upgrade/g/capability-registry`, `upgrade/i/p0-14-clock-skew`, `upgrade/i/p0-15-guardrail-review`, `upgrade/i/p0-16-guard-bypass-sweep`, `upgrade/i/p0-17-runner-gate-hardening`, `upgrade/i/p0-18-gate-followups`, `upgrade/i/p0-18-install-runner`, `upgrade/i/p0-19-guard-followups` (`~/work/llm-p019`), `upgrade/b/eval-set` (`~/work/llm-b02`), `upgrade/b/low-traffic-window` (`~/work/llm-b05`).
- Merged in cycle 7 too: `upgrade/b/correlation-ids` (`~/work/llm-b03`; `frontend/node_modules` installed there, gitignored). No open feature branches.
- `~/work/llm-ci` on `upgrade/ci/reliability` (1eab1707) was not created by cycles 6–7 as far as the records show; left alone.
- Detached review worktrees, no changes, removable: `~/work/llm-p016-rv1`..`rv4`, `~/work/llm-p017-rv`, `~/work/llm-p018-rv`, `~/work/llm-p019-rv`.
- Tag `baseline/pre-upgrade-2026-10-03` → `3c75af1c` (production before the programme).

## Running services

- Runner: `llm-autopilot.service` (systemd user unit), still on the Phase 0 runner, guard and gate (NH-007). Watch `~/.llm-autopilot/bin/status.sh`; stop `touch ~/.llm-autopilot/STOP`; pause `touch ~/.llm-autopilot/PAUSE`.
- Test database `llmdev-test-pg` on the worker (connection variables exported into each cycle). Only ONE orchestrator pytest process at a time: the per-test TRUNCATE wipes a parallel run's rows.
- Two leftover QA processes in the runner's cgroup (NH-012).

## Experiments in flight

None. Scratch from cycles 4–7 is listed in the TASK_BOARD backlog (P3).

## Known CI flakes

`test_voice_sessions.py:890` (memory peak) is not fixed: if it fails, `gh run rerun <id> --failed` on the PR run.

## Last known-good production

`3c75af1c` (`main`), tag `baseline/pre-upgrade-2026-10-03`.

## Blockers

None for the agent. Operator items: NH-007 (install guardrail fixes; one install now carries P0-15..P0-19), NH-008 (one `dev` repository setting), NH-012 (two leftover processes), NH-011 (approve the `.github/` tree at the finish line), NH-009, NH-010, NH-004, NH-005, NH-006.
