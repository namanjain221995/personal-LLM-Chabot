# Implementation status

History of the programme, newest first. Designed ≠ Implemented ≠ Tested ≠ Deployed; each entry says which.

## 2026-10-03 — Phase 0 (interactive bootstrap sessions)

### First session (refused at the runner)

- Facts, production map, dev worktree, baseline tag, Claude Code verification, guard layers and state files were built.
- Writing the runner was refused by the auto-mode classifier (`[Create Unsafe Agents]`); the session stopped and asked the operator (NEEDS_HUMAN NH-001).

### Operator decisions and approval

The operator's decisions 1–7 and the runner approval were recorded in §0 of both copies of `MASTER_PROMPT.md` (`cmp` identical). The approval arrived as pasted text and was re-confirmed by the operator before any action.

### Second session (decisions carried out)

| Item | State | Evidence (command → result) |
|---|---|---|
| Host details out of the repository | Implemented | Guard and settings read `~/.llm-autopilot/host.json`; template `ops/autopilot/host.example.json`; tests use RFC 5737 addresses; grep of committed files finds no host names or host addresses |
| Guard: dev only through the installed gate, no pushes to main, rerun check, resolved compose names, dev services on the worker | Implemented, TEST_PASSED | `python3 -m unittest discover -s ops/autopilot/tests -p 'test_*.py'` → `Ran 41 tests … OK` (guard, gate, runner) |
| Dev gate `ops/deploy/merge_to_dev.sh` | Implemented, TEST_PASSED | `test_merge_to_dev.py`: 6 tests against a throwaway origin and a fake `gh` (happy path, dry run, missing report, non-fast-forward, not the pushed tip, failed/missing/pending checks, failed status) |
| Compose parameterization (`TECHSARA_STACK`) | Implemented, TEST_PASSED | `docker compose config` of 3 production chains × 2 profile sets × YAML/JSON → 12/12 byte-identical (same SHA-256) before and after; dev render with `ops/dev/stack.vars` → project `llmdev`, only `llmdev_*` volumes and networks |
| Launcher and workflow-script suites after the compose change | TEST_PASSED | `unittest_gate.py --label launcher` → 694 executed, 1 skipped (Python 3.12 and 3.11); `--label workflow-scripts` → 710 executed |
| Test database on the worker | Implemented | `llmdev-test-pg` (CI's Postgres digest, `--memory 2g`, `--oom-score-adj 1000`, fsync off), `pg_isready` → accepting connections; recorded in `~/.llm-autopilot/manifest.jsonl` |
| Conftest contract (no default server, `_test` suffix, allowlist, skew marker) | Implemented; full-suite run in progress | 5 suspected files against the worker database → `1 failed, 311 passed` (only `test_crawl_queue.py::test_enqueue_is_deduped_by_scope_and_recent_crawls`); clock skew −21.5 ms |
| Runner `ops/autopilot/autopilot.py` | Implemented, TEST_PASSED | `test_runner.py` (§5.5 items 1–5 with a stub `claude`): limit with reset time → wake = reset + 2–5 min; limit without reset → 20/40/60/60 min; 6 crashes → 5 × 60 s then 2 h; PAUSE holds, STOP finishes the cycle then exit 64; pause window; second instance → exit 3; auth → one NEEDS_HUMAN entry, 30 min retry; max turns normal; COMPLETE → exit 64; flags `--permission-mode auto --permission-prompts none --settings … --max-turns 150` |
| §5.5 item 6: service restart | TEST_PASSED | Transient user unit with the real unit's properties: `kill -9` of the runner → systemd restarted it (new PID, new cycle); `oom_score_adj=1000`, nice 10, `memory.max=6442450944`, `memory.high=5368709120`; stop left no child processes; exit 64 → no restart, `Result=success` |
| §5.5 item 7: guard layers in real sessions | TEST_PASSED | Three `claude -p --permission-mode auto --permission-prompts none` sessions (haiku): all layers / deny rules only / classifier only. In each, the force push, the push to `dev`, the `.env` read and the write into the guard directory were refused; `git status` and `echo` ran |
| `/goal` in print mode | TEST_PASSED | `claude -p "/goal …"` in the untrusted worktree → "Goal set", met after 1 turn |
| Service environment | TEST_PASSED | Under `systemd-run --user`: `claude -p` → OK; `git push --dry-run origin HEAD:refs/heads/autopilot/dev` authenticated; `gh api` works; SSH to the worker works |
| Reboot path | TEST_NOT_RUN | Not rebooting the production host; covered by linger plus an enabled user unit |
