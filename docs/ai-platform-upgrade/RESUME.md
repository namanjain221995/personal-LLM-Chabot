# Resume

STATUS: IN PROGRESS — Phase B (B-01, B-02, B-03, B-05 done; B-04 next)

Updated 2026-10-03 about 19:20 UTC by autopilot cycle 8.

## Phase and active task

- **Phase:** 0 and A are done; every Phase 0 hardening task (P0-15..P0-19) is DONE in source and waits for the operator's install (NH-007). Phase B (baseline and evaluation harness, §11) is active.
- **Done in Phase B:** B-02 (16-case evaluation set, merged 0a32efca), B-03 (correlation ids and stage times, merged 03755512), B-05 (low-traffic window 05:00–07:00 IST, merged 5c3b2920).
- **B-01 DONE in cycle 8** (merged 0669102b): the dev stack runs on the worker (`ops/dev/README.md`, AD-010). QA `fix_first` (the inference cap could run out of memory) fixed and re-checked with the reviewer's probe; merged-tree `ops/dev/tests` `72 passed, 97 subtests passed`, `ops/autopilot/tests` `183 passed, 1083 subtests passed`.
- **Active task: B-04** (freeze the baseline). Exact next step: extend `scripts/aiq/run.py` for the eval set (seed history, upload attachments, deep_research, `eval_set.FAIL_TEXT`, capture cited passages), then run the 16 cases against the dev stack. From the head, open a local forward over existing access, `ssh -N -L 127.0.0.1:28080:127.0.0.1:28080 <worker>`, and point the harness at `http://127.0.0.1:28080` as `dev-admin@dev.test` (password in the gitignored file `ops/dev/.runtime/admin-login` of `~/work/llm-b01`; feed it on stdin, never argv). Heavy or long runs only in 05:00–07:00 IST. Every number must state its conditions: dev stack, CPU image, router shared on the main model, no embeddings, reranking or search (NH-013), cap of two requests in flight.
- Operator decisions in `MASTER_PROMPT.md` §0 govern everything: integrate on `autopilot/dev`; never touch `main` or production; `dev` only through `~/.llm-autopilot/bin/merge_to_dev.sh` at the finish line.

## Branches and worktrees

- `~/work/llm-dev` on `autopilot/dev` (pushed). Draft PR #98 `autopilot/dev → dev` is the status board and runs CI.
- Merged and kept: `upgrade/a/discovery`, `upgrade/g/capability-registry`, `upgrade/i/p0-14-clock-skew`, `upgrade/i/p0-15-guardrail-review`, `upgrade/i/p0-16-guard-bypass-sweep`, `upgrade/i/p0-17-runner-gate-hardening`, `upgrade/i/p0-18-gate-followups`, `upgrade/i/p0-18-install-runner`, `upgrade/i/p0-19-guard-followups` (`~/work/llm-p019`), `upgrade/b/eval-set` (`~/work/llm-b02`), `upgrade/b/low-traffic-window` (`~/work/llm-b05`), `upgrade/b/correlation-ids` (`~/work/llm-b03`).
- Merged in cycle 8: `upgrade/b/dev-stack` (`~/work/llm-b01`; KEEP it: it holds the live dev stack's generated env files under `ops/dev/`, gitignored) and `upgrade/b/dev-stack-cap` (`~/work/llm-b01-cap`, removable). No open feature branches.
- `~/work/llm-ci` on `upgrade/ci/reliability` (1eab1707) was not created by cycles 6–8 as far as the records show; left alone.
- Detached review worktrees, no changes, removable: `~/work/llm-p016-rv1`..`rv4`, `~/work/llm-p017-rv`, `~/work/llm-p018-rv`, `~/work/llm-p019-rv`.
- Tag `baseline/pre-upgrade-2026-10-03` → `3c75af1c` (production before the programme).

## Running services

- Runner: `llm-autopilot.service` (systemd user unit), still on the Phase 0 runner, guard and gate (NH-007). Watch `~/.llm-autopilot/bin/status.sh`; stop `touch ~/.llm-autopilot/STOP`; pause `touch ~/.llm-autopilot/PAUSE`.
- **Dev stack `llmdev` on the worker** (since 2026-10-03 about 18:45 UTC): postgres, inference-cap, orchestrator (127.0.0.1:28080), frontend (127.0.0.1:23000), on the worker's loopback; images `llmdev-*`, volumes `llmdev_*` (all in the manifest). Status: `cd ~/work/llm-b01 && DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh status`; stop: `… devstack.sh down` (keeps volumes). Exact commands with addresses: host-only `b01-dev-stack-2026-10-04.md`.
- Test database `llmdev-test-pg` on the worker (connection variables exported into each cycle). Only ONE orchestrator pytest process at a time: the per-test TRUNCATE wipes a parallel run's rows.
- Leftover test processes (NH-012): two QA harness shells from cycle 5 and one fake model server from a cycle-8 builder (127.0.0.1 only).

## Experiments in flight

None. Scratch from cycles 4–8 is listed in the TASK_BOARD backlog (P3).

## Known CI flakes

`test_voice_sessions.py:890` (memory peak) is not fixed: if it fails, `gh run rerun <id> --failed` on the PR run.

## Last known-good production

`3c75af1c` (`main`), tag `baseline/pre-upgrade-2026-10-03`.

## Blockers

None for the agent. Operator items: NH-013 (how the dev stack reaches the router, embedding and reranker engines), NH-007 (install guardrail fixes; one install carries P0-15..P0-19), NH-008 (one `dev` repository setting), NH-012 (leftover processes), NH-011 (approve the `.github/` tree at the finish line), NH-009, NH-010, NH-004, NH-005, NH-006.
