# Resume

STATUS: IN PROGRESS — Phase 0 hardening (P0-18 under review, P0-19 open), then Phase B

Updated 2026-10-03 about 16:00 UTC by autopilot cycle 5.

## Phase and active task

- **Phase:** 0 and A are done except guardrail hardening. P0-16 and P0-17 are DONE (source; not installed). Phase B (baseline and evaluation harness, §11) follows.
- **Active task: P0-18** (dev gate, install.sh and runner follow-ups from the P0-17 re-QA). It is built on `upgrade/i/p0-18-gate-followups` (`~/work/llm-p018`, 7 commits c23b17e2..ea25fba1; the builder's suite gave `130 passed, 893 subtests passed`). An independent review was running when this checkpoint was written; its worktree is `~/work/llm-p018-rv` (detached at ea25fba1).
- **Exact next step:** if that review's result is not recorded in `IMPLEMENTATION_STATUS.md`, re-run an independent review of `git diff 11550954..ea25fba1` against the host-only list `~/.llm-autopilot/agent/private/p0-17-reqa-2026-10-03.md`. Fix any blocking finding on the branch. Then merge into `autopilot/dev` and run `orchestrator/.venv/bin/python -m pytest ops/autopilot/tests -q -p no:cacheprovider`. Push, and update NH-007 (one install carries P0-15..P0-18).
- P0-16's last fresh review (fb29fb03..4ff6bbac) is done; its one finding is fixed (e969264c, merged b21088b3).
- Then P0-19 (non-blocking guard follow-ups) in parallel with Phase B: B-02/B-03/B-05 are independent (decision 10).
- Operator decisions in `MASTER_PROMPT.md` §0 govern everything: integrate on `autopilot/dev`; never touch `main` or production; `dev` only through `~/.llm-autopilot/bin/merge_to_dev.sh` at the finish line.

## Branches and worktrees

- `~/work/llm-dev` on `autopilot/dev`. Draft PR #98 `autopilot/dev → dev` is the status board and runs CI.
- Merged and kept: `upgrade/a/discovery`, `upgrade/g/capability-registry`, `upgrade/i/p0-14-clock-skew`, `upgrade/i/p0-15-guardrail-review`, `upgrade/i/p0-16-guard-bypass-sweep` (worktree `~/work/llm-p016`), `upgrade/i/p0-17-runner-gate-hardening` (worktree `~/work/llm-p017`).
- Open: `upgrade/i/p0-18-gate-followups` (worktree `~/work/llm-p018`).
- Detached review worktrees, no changes, removable: `~/work/llm-p016-rv1`..`rv4`, `~/work/llm-p017-rv`, `~/work/llm-p018-rv`.
- Tag `baseline/pre-upgrade-2026-10-03` → `3c75af1c` (production before the programme).

## Running services

- Runner: `llm-autopilot.service` (systemd user unit), still on the Phase 0 runner, guard and gate (NH-007). Watch `~/.llm-autopilot/bin/status.sh`; stop `touch ~/.llm-autopilot/STOP`; pause `touch ~/.llm-autopilot/PAUSE`.
- Test database `llmdev-test-pg` on the worker (connection variables exported into each cycle). Also on it: the test-only database `llmdev_p013_test` (cycle 2).
- Two leftover QA processes in the runner's cgroup (NH-012).

## Experiments in flight

None. Cycle 4–5 scratch is listed in the TASK_BOARD backlog (P3).

## Known CI flakes

`test_voice_sessions.py:890` (memory peak) is not fixed: if it fails, `gh run rerun <id> --failed` on the PR run.

## Last known-good production

`3c75af1c` (`main`), tag `baseline/pre-upgrade-2026-10-03`.

## Blockers

None for the agent. Operator items: NH-007 (install guardrail fixes, best after P0-18), NH-012 (two leftover processes), NH-011 (approve the `.github/` tree at the finish line), NH-008, NH-009, NH-010, NH-004, NH-005, NH-006.
