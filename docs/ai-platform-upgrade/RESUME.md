# Resume

STATUS: IN PROGRESS — Phase B (B-01, B-02, B-03, B-04a, B-05 done; B-04b next, in the 05:00–07:00 IST window)

Updated 2026-10-03 about 22:00 UTC (04-10 03:30 IST) by autopilot cycle 9.

## Phase and active task

- **Phase:** 0 and A are done; every Phase 0 hardening task (P0-15..P0-19) is DONE in source and waits for the operator's install (NH-007). Phase B (baseline and evaluation harness, §11) is active.
- **Done in Phase B:** B-01 (dev stack, merged 0669102b), B-02 (eval set, 0a32efca), B-03 (correlation ids, 03755512), B-05 (window, 5c3b2920), and in cycle 9 **B-04a** (merged c4fbcca6): `scripts/aiq/run_evalset.py` runs the 16-case set against the dev stack, `scripts/aiq/baseline.py` freezes and compares; the tolerance method is frozen in `BENCHMARK_RESULTS.md` and AD-011.
- **Active task: B-04b**, the baseline run. Not before 05:00 IST: the dev README and §9.3 keep evaluations inside the 05:00–07:00 IST window. Outside the window, take backlog work (for example the P1 items below the table) instead of waiting. Exact steps, from the head:
  1. `ssh -N -L 127.0.0.1:28080:127.0.0.1:28080 <worker>` (existing access; run it in the background, stop it after).
  2. Per run r1..r5 (never fewer than 3): seed a fresh account from `~/work/llm-b01`: write a random password to `ops/dev/.runtime/aiq-base-<date>-rN.pw` (umask 077, never printed), then `DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh seed aiq-base-<date>-rN < that file`.
  3. From `~/work/llm-dev`: `orchestrator/.venv/bin/python scripts/aiq/run_evalset.py --base http://127.0.0.1:28080 --email aiq-base-<date>-rN@dev.test --password-file <that file> --repeats 1 --workers 1 --not-before 05:00 --deadline 07:00 --label "<conditions, no hosts>" --out scripts/aiq/runs/b04-<date>-rN`. Label text that passes the screen: `dev stack llmdev, CPU orchestrator image, main engine via the dev cap (2 in flight), router shared on main, no embeddings, reranking or search, window 05:00-07:00 IST`.
  4. `scripts/aiq/baseline.py freeze scripts/aiq/runs/b04-<date>-r1 ... --out scripts/aiq/runs/evalset-baseline-<date>/baseline.json --markdown /tmp/b04b.md`, un-ignore that directory in `scripts/aiq/.gitignore`, commit `baseline.json`, paste the tables into `BENCHMARK_RESULTS.md` (section "Baseline"), and mark B-04b DONE.
  - Host-only details (worker address, the commands with it): `~/.llm-autopilot/agent/private/b01-dev-stack-2026-10-04.md`.
- Operator decisions in `MASTER_PROMPT.md` §0 govern everything: integrate on `autopilot/dev`; never touch `main` or production; `dev` only through `~/.llm-autopilot/bin/merge_to_dev.sh` at the finish line.

## Branches and worktrees

- `~/work/llm-dev` on `autopilot/dev` (pushed). Draft PR #98 `autopilot/dev → dev` is the status board and runs CI; its secret scan fails on every run since cycle 3 (NH-014, operator).
- Merged in cycle 9: `upgrade/b/evalset-runner` (`~/work/llm-b04-run`), `upgrade/b/baseline-stats` (`~/work/llm-b04-stats`), both into `upgrade/b/baseline` (`~/work/llm-b04`), merged into autopilot/dev. The two builder worktrees are removable; keep `~/work/llm-b04` until B-04b ends.
- Merged earlier and kept: `upgrade/a/discovery`, `upgrade/g/capability-registry`, `upgrade/i/p0-14-clock-skew`, `upgrade/i/p0-15-guardrail-review`, `upgrade/i/p0-16-guard-bypass-sweep`, `upgrade/i/p0-17-runner-gate-hardening`, `upgrade/i/p0-18-gate-followups`, `upgrade/i/p0-18-install-runner`, `upgrade/i/p0-19-guard-followups`, `upgrade/b/eval-set`, `upgrade/b/low-traffic-window`, `upgrade/b/correlation-ids`, `upgrade/b/dev-stack` (`~/work/llm-b01`: KEEP, it holds the dev stack's generated env files and the seeded accounts' password files), `upgrade/b/dev-stack-cap`.
- `~/work/llm-ci` on `upgrade/ci/reliability` (1eab1707) was not created by cycles 6–9 as far as the records show; left alone.
- Detached review worktrees, no changes, removable: `~/work/llm-p016-rv1`..`rv4`, `~/work/llm-p017-rv`, `~/work/llm-p018-rv`, `~/work/llm-p019-rv`.
- Tag `baseline/pre-upgrade-2026-10-03` → `3c75af1c` (production before the programme).

## Running services

- Runner: `llm-autopilot.service` (systemd user unit), still on the Phase 0 runner, guard and gate (NH-007). Watch `~/.llm-autopilot/bin/status.sh`; stop `touch ~/.llm-autopilot/STOP`; pause `touch ~/.llm-autopilot/PAUSE`.
- **Dev stack `llmdev` on the worker** (since 2026-10-03 about 18:45 UTC): postgres, inference-cap, orchestrator (127.0.0.1:28080), frontend (127.0.0.1:23000), on the worker's loopback. Status: `cd ~/work/llm-b01 && DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh status`; stop: `… devstack.sh down` (keeps volumes). Dev accounts so far: the admin and `aiq-smoke-20261004a`/`b` (synthetic; their password files are in `~/work/llm-b01/ops/dev/.runtime/`, gitignored).
- Test database `llmdev-test-pg` on the worker. Only ONE orchestrator pytest process at a time.
- Cycle 9's local forward to 28080 ends with the cycle.
- Leftover test processes (NH-012).

## Experiments in flight

None. Scratch from cycles 2–9 is listed in the TASK_BOARD backlog (P3).

## Known CI state

- PR #98: `Security scanning` fails on every run since 64e4f699 (NH-014); everything else passed on 6ee4c2f7 (run 37147459830).
- Flake `test_voice_sessions.py:890` (memory peak): `gh run rerun <id> --failed` on the PR run.

## Last known-good production

`3c75af1c` (`main`), tag `baseline/pre-upgrade-2026-10-03`.

## Blockers

None for the agent (B-04b waits only for the window). Operator items: NH-014 (secret-scan baseline, blocks the finish line), NH-013 (how the dev stack reaches router, embedding and reranker engines), NH-007 (install guardrail fixes), NH-008, NH-012, NH-011, NH-009, NH-010, NH-004, NH-005, NH-006.
