# Needs a human

Items only the operator can resolve. Each has the exact action, why, the risk, the command and the rollback. Open items first.

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
