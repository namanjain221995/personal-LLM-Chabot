# Needs a human

Items only the operator can resolve. Each has the exact action, why, the risk, the command and the rollback. Open items first.

## NH-014 — The status board's secret scan has failed on every run since cycle 3 (OPEN, action)

- **What:** every CI run on PR #98 since 64e4f699 (cycle 3, 2026-10-03 09:57 UTC) failed `Security scanning`, so `CI passed` failed too: runs 37114769359, 37135812356, 37138911391, 37143364865, 37147459830. The cause is one new gitleaks finding, `generic-api-key` at `.github/workflows/gitleaks-baseline.json:126`: the explanatory note that 64e4f699 added to the baseline quotes, verbatim, one of the two fabricated runner-test values it baselines. Nothing else failed in those runs except one orchestrator shard in the first.
- **Why you:** the fix is another reviewed entry in the secret-scan baseline (and rewording the note so it no longer quotes the value). Cycle 9 prepared that edit and the auto-mode classifier refused it as a change to a security control, so the autopilot leaves it to you. The finish-line gate (`merge_to_dev.sh`) needs `CI passed` green on the exact commit, so this blocks the final merge, not today's work.
- **What to check:** `git -C ~/work/llm-dev show 64e4f699 -- .github/workflows/gitleaks-baseline.json` (the note at line 126 names a fabricated command-line token fed to the runner's log redactor in `ops/autopilot/tests/test_runner.py`; it was never minted).
- **Command (if you agree):** add to `findings` in `.github/workflows/gitleaks-baseline.json`
  ```json
  {"fingerprint": "64e4f6995cdb7fddefc738cc623678bbf3230f7f:.github/workflows/gitleaks-baseline.json:generic-api-key:126", "rule": "generic-api-key", "file": ".github/workflows/gitleaks-baseline.json", "line": 126}
  ```
  reword line 126 so it describes the value instead of quoting it, commit on `autopilot/dev`, push, and approve the new `.github/` tree as NH-011 describes. The next PR #98 run should show `leaks found: 30` with no `NEW` line.
- **Risk:** a baseline entry silences exactly one fingerprint (commit, file, rule, line); the gate fails if the entry ever stops matching.
- **Rollback:** revert that commit.

## NH-013 — How the dev stack should reach the router, embedding and reranker engines (OPEN, decision)

- **What:** the dev stack (B-01, `ops/dev/README.md`) runs on the worker. From there only the production main engine answers; the router, embedding and reranker engines listen on the head's loopback. So the dev stack runs with the main model serving router, agent and vision calls, and with embeddings and reranking off. Baselines measured on it (B-04) say so, and questions about uploaded documents lose semantic retrieval there.
- **Why you:** each way to close the gap changes how production engines are exposed or adds a long-lived process on the head, which the autopilot does not do on its own.
- **Options:** (a) do nothing: dev keeps the shared-router, no-embedding setup and every dev measurement states it; (b) publish the three engines on the head's address on the link to the worker, allowed from the worker only in the host guard, then give the addresses to `ops/dev/init-env.sh --router … --embed … --rerank …` and re-run `ops/dev/devstack.sh up`; (c) run a reverse SSH tunnel from the head during evaluation runs only, as `scripts/aiq/aiq-stack.sh tunnel` describes (the dev orchestrator would then need the tunnel's ports reachable from its containers).
- **Risk:** (b) widens what the worker can reach on the head; (c) is a process you start and stop by hand.
- **Rollback:** (b) remove the publish and the host-guard rule; (c) stop the tunnel. Either way, re-run `init-env.sh` without the flags and `devstack.sh up`.

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

## NH-012 — Leftover test processes (OPEN, action, harmless)

- **What:** two `bash /tmp/ap-install-qaqw2crc/harness.sh` processes (PIDs 1009132 and 1009142) are left over from a cycle-5 QA probe of install.sh's signal handling. They are inside the `llm-autopilot.service` cgroup; one spins a CPU core.
- **Why you:** the guard refused the agent's `kill`, and the autopilot does not work around a refusal.
- **Risk:** none beyond one busy core. They only ask `systemctl --user is-active`, never start or stop anything, and they die at the next stop of the unit.
- **Also (cycle 8):** a fake `/v1/models` server a B-01 test builder started and could not stop: PID 2486898, `python3 /tmp/gtest/fake_models.py 38471`, listening on 127.0.0.1 only. Harmless; the guard refused the kill.
- **Command:** `kill -KILL 1009142 1009132 2486898`
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
