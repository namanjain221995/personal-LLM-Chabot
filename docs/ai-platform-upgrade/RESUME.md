# Resume

STATUS: IN PROGRESS — Phase 0 handed over to the runner; next: P0-13, P0-14, P0-15, then Phase A

Updated 2026-10-03 by the interactive Phase 0 session.

## Phase and active task

- **Phase:** 0 finishing (handover tasks P0-13…P0-15), then A (discovery, §10).
- **Next step:** the top READY task on `TASK_BOARD.md`.
- Operator decisions in `MASTER_PROMPT.md` §0 govern everything: integrate on `autopilot/dev`; never touch `main` or production; `dev` only through `~/.llm-autopilot/bin/merge_to_dev.sh` at the finish line.

## Branches and worktrees

- `~/work/llm-dev` on `autopilot/dev` (pushed to origin). Draft PR `autopilot/dev → dev` is the status board and runs CI.
- Tag `baseline/pre-upgrade-2026-10-03` → `3c75af1c` (production before the programme).

## Running services

- Runner: `llm-autopilot.service` (systemd user unit). Watch `~/.llm-autopilot/bin/status.sh`; stop `touch ~/.llm-autopilot/STOP`; pause `touch ~/.llm-autopilot/PAUSE`.
- Test database `llmdev-test-pg` on the worker (connection variables exported into each cycle by the runner). Remove: see NEEDS_HUMAN NH-003.

## Experiments in flight

None.

## Last known-good production

`3c75af1c` (`main`), tag `baseline/pre-upgrade-2026-10-03`.

## Blockers

None. Operator FYIs: NH-004 (backups), NH-005 (test variables), NH-006 (runner memory cap).
