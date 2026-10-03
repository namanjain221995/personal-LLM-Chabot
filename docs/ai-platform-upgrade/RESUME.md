# Resume

STATUS: IN PROGRESS — Phase 0 hardening (P0-16, P0-17), then Phase B

Updated 2026-10-03 about 10:00 UTC by autopilot cycle 2.

## Phase and active task

- **Phase:** 0 and A are done except two guardrail hardening tasks (P0-16, P0-17). Phase B (baseline and evaluation harness, §11) follows.
- **Next step:** P0-16, the guard bypass sweep. Write each probe as a `decide()` case in `ops/autopilot/tests/test_guard_hook.py`, not as a live command; the auto-mode classifier stopped the first attempt. Fix `ops/autopilot/guard/guard_hook.py` and the template, run `orchestrator/.venv/bin/python -m pytest ops/autopilot/tests -q -p no:cacheprovider`, and add the result to NH-007 (the operator installs).
- P0-16, P0-17 and B-02/B-03/B-05 are independent; run them in parallel worktrees (decision 10).
- Operator decisions in `MASTER_PROMPT.md` §0 govern everything: integrate on `autopilot/dev`; never touch `main` or production; `dev` only through `~/.llm-autopilot/bin/merge_to_dev.sh` at the finish line.

## Branches and worktrees

- `~/work/llm-dev` on `autopilot/dev`, pushed. Draft PR #98 `autopilot/dev → dev` is the status board and runs CI. The last fully green run is 37109933435 on 242bd04d.
- Merged and kept (no worktrees): `upgrade/a/discovery`, `upgrade/g/capability-registry`, `upgrade/i/p0-14-clock-skew`, `upgrade/i/p0-15-guardrail-review`.
- Tag `baseline/pre-upgrade-2026-10-03` → `3c75af1c` (production before the programme).

## Running services

- Runner: `llm-autopilot.service` (systemd user unit), still running the Phase 0 runner and guard (NH-007 installs the reviewed ones). Watch `~/.llm-autopilot/bin/status.sh`; stop `touch ~/.llm-autopilot/STOP`; pause `touch ~/.llm-autopilot/PAUSE`.
- Test database `llmdev-test-pg` on the worker (connection variables exported into each cycle). A second test-only database, `llmdev_p013_test`, was created on it in cycle 2 by `/tmp/p013-probe/run_on_own_db.py`, so CI-failure fixes could be tested while the full suite held the main test database.

## Experiments in flight

None. Scratch files of cycle 2 are under `/tmp` (TASK_BOARD backlog P3).

## Known CI flakes

`test_voice_sessions.py:890` (memory peak) is not fixed: if it fails, `gh run rerun <id> --failed` on the PR run. Two other timing flakes were fixed in cycle 2.

## Last known-good production

`3c75af1c` (`main`), tag `baseline/pre-upgrade-2026-10-03`.

## Blockers

None for the agent. Operator items: NH-007 (install guardrail fixes), NH-008 (a `dev` repository setting), NH-009 (host-only notes), NH-010 (node clock drift, FYI), NH-004, NH-005, NH-006.
