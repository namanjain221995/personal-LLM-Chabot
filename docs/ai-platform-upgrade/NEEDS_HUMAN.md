# Needs a human

Items only the operator can resolve. Each has the exact action, why, the risk, the command and the rollback. Open items first.

## NH-011 — Approve a `.github/` change before the finish line (OPEN, action later)

- **What:** `autopilot/dev` changes one CI file, `.github/workflows/gitleaks-baseline.json`: two reviewed entries for fabricated test values in `ops/autopilot/tests/test_runner.py` (commit df091d61), in the file's existing format. Without them the blocking secret scan fails on every run, because gitleaks reads history and the commit is already pushed.
- **Why you:** once NH-007 is installed, the gate refuses a commit whose `.github/` tree differs from `origin/dev` until you approve that tree.
- **When:** at the finish line, or whenever `.github/` changes again (the gate prints the tree hash). Review, then approve:
  ```bash
  git -C ~/work/llm-dev diff origin/dev origin/autopilot/dev -- .github/
  git -C ~/work/llm-dev rev-parse origin/autopilot/dev:.github >> ~/.llm-autopilot/approved-ci-trees
  ```
- **Risk:** none until the finish line; an unapproved tree only holds the merge into `dev`.
- **Rollback:** delete the line from `~/.llm-autopilot/approved-ci-trees`.

## NH-012 — Two leftover test processes (OPEN, action, harmless)

- **What:** two `bash /tmp/ap-install-qaqw2crc/harness.sh` processes (PIDs 1009132 and 1009142) are left over from a cycle-5 QA probe of install.sh's signal handling. They are inside the `llm-autopilot.service` cgroup; one spins a CPU core.
- **Why you:** the guard refused the agent's `kill`, and the autopilot does not work around a refusal.
- **Risk:** none beyond one busy core. They only ask `systemctl --user is-active`, never start or stop anything, and they die at the next stop of the unit.
- **Command:** `kill -KILL 1009142 1009132`
- **Rollback:** none needed.

## NH-007 — Install the reviewed guardrail fixes (OPEN, action)

- **What:** P0-15, P0-16, P0-17, P0-18 and P0-19 fixed the review findings in the guard hook, the deny rules, the runner and the dev gate (`ops/autopilot/`, `ops/deploy/merge_to_dev.sh`; P0-16 merged into `autopilot/dev` at 04e55f06 and b21088b3, P0-18 at 51f80b4b, P0-19 at bffc6d7a). The P0-18 gate adds check 6 (GitHub's compare must say the push is a fast-forward), so `gh` must reach the API when the gate runs. The autopilot may not install its own guardrails (§3.3), so the running copies in `~/.llm-autopilot/` are still the Phase 0 ones.
- **Why:** the installed guard lacks every fix since Phase 0. The host-only reports `~/.llm-autopilot/agent/private/p0-15-review-2026-10-03.md`, `p0-16-sweep-2026-10-03.md` (see "Found while fixing") and `p0-17-reqa-2026-10-03.md` say why installing matters.
- **Best moment:** now: P0-19 is merged (bffc6d7a), so one install carries every guard and gate fix so far. New refusals you may notice: `env -S`/`env -C`, any read of `~/.llm-autopilot/agent/test-db.vars` (it holds the test database password), curl data or `file://` URLs naming secret files, and inline code that writes through a variable or alias while it names a protected path.
- **Command** (outside any autopilot session; `install.sh` refuses inside one):
  ```bash
  cd ~/work/llm-dev && git log -1 --oneline   # a commit at or after bffc6d7a
  ops/autopilot/install.sh --restart-after-cycle
  ```
  It re-installs the hook, settings, runner, gate and `status.sh`, then touches `STOP`, waits for the running cycle to end (up to its 4 h limit), removes `STOP` and starts the new runner. Do not use `systemctl --user restart`: it would kill the cycle in flight.
- **What changes:** about 16 new deny rules; the gate runs with a cleared environment (checked: `gh` and `git push --dry-run origin` work under `env -i HOME=… PATH=/usr/bin:/bin`) and refuses a commit whose `.github/` tree differs from `origin/dev` unless you list that tree in `~/.llm-autopilot/approved-ci-trees` (see `ops/autopilot/README.md`).
- **Risk:** a guard false positive slows the agent (it reroutes); a gate refusal holds only the finish line.
- **Rollback:** install the Phase 0 source again from a scratch worktree:
  ```bash
  git -C ~/work/llm-dev worktree add --detach ~/work/ap-rollback bb0ac9f9
  ~/work/ap-rollback/ops/autopilot/install.sh --restart-after-cycle
  ```

## NH-008 — A repository setting for `dev` (OPEN, action)

One GitHub repository setting for the `dev` branch should be changed by you; the agent may not change repository settings (decision 5). The exact setting, the reason and the command are in the host-only report `~/.llm-autopilot/agent/private/p0-15-review-2026-10-03.md` (finding D5), kept out of this public file. The cycle-6 review of the dev gate gives a second reason for the same setting (`p0-17-reqa-2026-10-03.md`, section "Review of f896cbe0/b6f4966b").

## NH-009 — Host-only notes to read (OPEN, FYI)

Cycle 2 kept security-relevant observations out of the public repository: `~/.llm-autopilot/agent/private/phase-a-discovery-2026-10-03.md` (Phase A), `a05-capability-2026-10-03.md` (engine metadata) and `p0-15-review-2026-10-03.md` (guardrails). None was acted on in production; each needs your decision.

## NH-010 — The two nodes' clocks drift apart (OPEN, FYI)

On 2026-10-03 the test database's clock (worker) was 36 ms behind this host at 07:34 UTC and 15 to 54 ms ahead between 08:34 and 09:37 UTC (sampled every 30 s). The programme may not change clocks (§3.3), so 24 orchestrator tests that compare a Python timestamp with the server's `now()` are expected failures against the remote test database only (P0-14); they stay strict in CI. If the nodes are meant to be time-synchronised, check their time sync service; nothing else depends on this item.

## NH-004 — Production database backups are manual and old (OPEN, FYI)

The newest database dump predates the programme by a month, and `scripts/backup-knowledge.sh` pauses the production sync worker while it runs. The programme never changes production (operator decisions 2 and 3), so it will not run that script. Before you release `dev → main` after the programme's final merge, take and verify a fresh backup yourself.

## NH-005 — Orchestrator tests now need two variables (OPEN, FYI for interactive sessions)

Once this branch reaches `dev`, `orchestrator/tests/conftest.py` has no default server. Every run needs `TEST_DATABASE_URL` (a database whose name ends in `_test`) and `TEST_DATABASE_ALLOWED_HOSTS=<host>:<port>` for that server. Names like `test_ha_x` or `test` are refused now; use `ha_x_test`. GitHub Actions jobs are exempt from the allowlist. Rollback: revert the conftest commit.

## NH-006 — The runner uses some head memory (OPEN, FYI)

The runner and every cycle (Claude Code, subagents, tests) run on the production head because the worktree and its Git repository live there; the operator approved this host for the runner. The systemd unit caps the whole tree at `MemoryHigh=5G`/`MemoryMax=6G` with `OOMScoreAdjust=1000`, so the kernel kills it before any production process. Long-lived dev services and builds go to the worker (the guard enforces this). To change the cap: edit `ops/autopilot/llm-autopilot.service` and re-run `ops/autopilot/install.sh`.

## NH-001 — Approve or create the autopilot runner (RESOLVED 2026-10-03)

Approved by the operator on 2026-10-03 ("I approve creating and starting the unattended autopilot runner …"). Built, tested and installed in Phase 0; see `IMPLEMENTATION_STATUS.md`.

## NH-002 — `dev` is shared with your own releases (RESOLVED 2026-10-03)

Operator decisions 1 and 2: the autopilot integrates on `autopilot/dev`; `dev` is the release target and moves only through `ops/deploy/merge_to_dev.sh` at the finish line. The status board is a draft PR `autopilot/dev → dev`.

## NH-003 — Where the autopilot's test database lives (RESOLVED 2026-10-03)

Operator decision 7: on the worker node. A dedicated `llmdev-test-pg` container (CI's Postgres digest, 2 GiB memory limit, `--oom-score-adj 1000`) serves the database `llmdev_orchestrator_test`. Its connection variables are in the host-only file named by `test_db.url_file` in `~/.llm-autopilot/host.json`; the runner exports them into each cycle. Removal: `ssh <worker> docker rm -f llmdev-test-pg` (recorded in `~/.llm-autopilot/manifest.jsonl`).
