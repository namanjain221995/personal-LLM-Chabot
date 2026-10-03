# Resume

STATUS: IN PROGRESS — Phase 0 hardening (P0-19 built, review pending), then Phase B

Updated 2026-10-03 about 17:00 UTC by autopilot cycle 6.

## Phase and active task

- **Phase:** 0 and A are done except guardrail hardening. P0-16, P0-17 and P0-18 are DONE (source; not installed, NH-007). Phase B (baseline and evaluation harness, §11) follows.
- **P0-18 DONE in cycle 6:** merged into `autopilot/dev` at 51f80b4b and pushed. Merged-tree suite: `175 passed, 922 subtests passed`. Two fresh reviews returned `ship`; their non-blocking findings were fixed or recorded (host-only `~/.llm-autopilot/agent/private/p0-17-reqa-2026-10-03.md`, last three sections).
- **Active task: P0-19** (guard false positives and the fetch-destination rule). Built on `upgrade/i/p0-19-guard-followups` (`~/work/llm-p019`, 7 commits f2b9d60d..1820a187, from 1eab1707). Builder's suite: `158 passed, 938 subtests passed`. **Not merged.**
- **Why not merged:** its independent review did not finish. A safety classifier stopped the reviewer while it read the guard's network-check code, before any comparison ran. The reviewer did not continue another way, and neither did the main session.
- **Exact next step for P0-19:** run a differential regression review that needs no new bypass inputs: replay the committed guard test inputs and the spec corpus (cycle 5's method, `p0-16-sweep-2026-10-03.md`) through the base (1eab1707) and branch guards, and list every input that base refuses and branch allows. Read-only worktree `~/work/llm-p019-rv` (detached at 1820a187). If that also stops, mark P0-19 BLOCKED and add a NEEDS_HUMAN item asking the operator to run the adversarial review interactively. Then merge with `--no-ff` into `autopilot/dev`, run `orchestrator/.venv/bin/python -m pytest ops/autopilot/tests -q -p no:cacheprovider`, push, and update NH-007.
- **In parallel with P0-19:** Phase B. B-02, B-03 and B-05 are independent (decision 10).
- Operator decisions in `MASTER_PROMPT.md` §0 govern everything: integrate on `autopilot/dev`; never touch `main` or production; `dev` only through `~/.llm-autopilot/bin/merge_to_dev.sh` at the finish line.

## Branches and worktrees

- `~/work/llm-dev` on `autopilot/dev` (51f80b4b, pushed). Draft PR #98 `autopilot/dev → dev` is the status board and runs CI.
- Merged and kept: `upgrade/a/discovery`, `upgrade/g/capability-registry`, `upgrade/i/p0-14-clock-skew`, `upgrade/i/p0-15-guardrail-review`, `upgrade/i/p0-16-guard-bypass-sweep` (`~/work/llm-p016`), `upgrade/i/p0-17-runner-gate-hardening` (`~/work/llm-p017`), `upgrade/i/p0-18-gate-followups` (`~/work/llm-p018`), `upgrade/i/p0-18-install-runner` (`~/work/llm-p018-b`, merged through the P0-18 branch).
- Open: `upgrade/i/p0-19-guard-followups` (`~/work/llm-p019`).
- Detached review worktrees, no changes, removable: `~/work/llm-p016-rv1`..`rv4`, `~/work/llm-p017-rv`, `~/work/llm-p018-rv`. Keep `~/work/llm-p019-rv` for the P0-19 review.
- Tag `baseline/pre-upgrade-2026-10-03` → `3c75af1c` (production before the programme).

## Running services

- Runner: `llm-autopilot.service` (systemd user unit), still on the Phase 0 runner, guard and gate (NH-007). Watch `~/.llm-autopilot/bin/status.sh`; stop `touch ~/.llm-autopilot/STOP`; pause `touch ~/.llm-autopilot/PAUSE`.
- Test database `llmdev-test-pg` on the worker (connection variables exported into each cycle). Also on it: the test-only database `llmdev_p013_test` (cycle 2).
- Two leftover QA processes in the runner's cgroup (NH-012).

## Experiments in flight

None. Scratch from cycles 4–6 is listed in the TASK_BOARD backlog (P3).

## Known CI flakes

`test_voice_sessions.py:890` (memory peak) is not fixed: if it fails, `gh run rerun <id> --failed` on the PR run.

## Last known-good production

`3c75af1c` (`main`), tag `baseline/pre-upgrade-2026-10-03`.

## Blockers

None for the agent. Operator items: NH-007 (install guardrail fixes; one install now carries P0-15..P0-18), NH-008 (one `dev` repository setting; the cycle-6 gate review adds a reason, host-only), NH-012 (two leftover processes), NH-011 (approve the `.github/` tree at the finish line), NH-009, NH-010, NH-004, NH-005, NH-006.
