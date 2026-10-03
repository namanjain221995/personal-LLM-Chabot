# Implementation status

History of the programme, newest first. Designed ≠ Implemented ≠ Tested ≠ Deployed; each entry says which.

## 2026-10-03 — Autopilot cycle 7 (from about 17:01 UTC)

Finished P0-19 and Phase B tasks B-02 and B-05; built B-03. Agents ran in parallel, each in its own worktree: B-02 (`~/work/llm-b02`), B-03 (`~/work/llm-b03`), B-05 (`~/work/llm-b05`), one P0-19 reviewer and one B-03 QA reviewer (read-only). The main session alone edited the guard and integrated.

| Item | State | Evidence (command → result) |
|---|---|---|
| P0-19 differential replay (no new bypass inputs) | TEST_EXECUTED | Base 1eab1707 vs 1820a187 through `evaluate()` only, TEST_HOST and real host config: committed test inputs 1,578/1,646, reviewer cases 578, spec corpus 2,866; base-refused/branch-allowed inputs reaching a hard limit found in four relaxations (details host-only) |
| P0-19 fixes 33622efa, 6cebad03 | Implemented, TEST_PASSED | 39 new deny subtests fail on 1820a187 and pass; suite `159 passed, 993 subtests passed`; extended replay 190,269 inputs, 0 internal errors |
| P0-19 fresh review → round 2 d4fa481d | Implemented, TEST_PASSED | Review `fix_first` (2 blocking, 7 non-blocking); every input it named now refused (its probe lists, three guards side by side); suite `160 passed, 1020 subtests passed`; spec replay p99 1.15 ms, max 5.8 ms |
| P0-19 merged bffc6d7a | TEST_PASSED | `pytest ops/autopilot/tests` on merged autopilot/dev → `183 passed, 1083 subtests passed`; pushed. Not installed (NH-007) |
| B-02 eval set (merged 0a32efca) | Implemented, TEST_PASSED | `pytest scripts/aiq/tests` → `78 passed` (main session, branch and merged tree); `eval_set.py` → `16 cases, valid`. Not yet run against a stack (B-04) |
| B-05 low-traffic window (merged 5c3b2920) | Measured | 9 read-only `query_range` GETs, 13.2 days; 05:00–07:00 IST (2 h), 04:00–08:00 IST (4 h) |
| B-03 correlation id + stage times | Implemented (branch), TEST_PASSED by builder, review pending | Builder: focused orchestrator files `78 passed`; vitest `162 passed`; two broad runs had failures from a concurrent pytest on the shared test DB that passed when re-run alone |

## 2026-10-03 — Autopilot cycle 6 (from about 16:10 UTC)

Finished P0-18 and built P0-19. Agents ran in parallel, each in its own worktree: an install.sh/runner builder (`~/work/llm-p018-b`), a P0-19 guard builder (`~/work/llm-p019`), two P0-18 reviewers and one P0-19 reviewer (detached worktrees). The main session alone edited the gate and integrated.

| Item | State | Evidence (command → result) |
|---|---|---|
| Gate reads both tips from the origin (f896cbe0) | Implemented, TEST_PASSED | 4 new tests (a ref repointed after the fetch, the review's background fetch loop, a planted grafts file); on ea25fba1's gate each merged (`MERGED`, or `dry run: would push`); on f896cbe0 they pass |
| No commit-graph file (b6f4966b) | Implemented, TEST_PASSED | Static test of the wrapper flags |
| Review of f896cbe0/b6f4966b | Reviewed: ship | Race closed; reviewer's own run of the new tests against the old gate → all 5 fail; suite `134 passed, 893 subtests passed`. Two older non-blocking findings (host-only) |
| Check 6: GitHub compare (8f5d33e3, 621c2c1b, 3e7d3ac4); exact post-push ref match | Implemented, TEST_PASSED | Read-only `gh api …/compare/<dev>...<autopilot/dev>` → `ahead`, reverse → `behind`, same → `identical`. New tests fail on b6f4966b (`5 failed`), pass after |
| install.sh final start ignores interrupts (cccf963d, 755d4005) | Implemented, TEST_PASSED | Fake `systemctl` signals its process group from inside `start`; old code → rc −2/−15/−1, new → start completes, STOP gone |
| Unusable cycle timeout falls back (7a9da45b) | Implemented, TEST_PASSED | `0`, `-5`, `4h` → default 14400/120 with `operator-setting-ignored` events; old code: `0` → kill counted as timeout, `-5` → every cycle exit 125, `4h` → `ValueError` at import |
| Review of cccf963d, 7a9da45b, 8f5d33e3, 621c2c1b | Reviewed: ship | Old code fails the new tests (`13 failed, 6 passed`); suite `139 passed, 922 subtests passed`. Non-blocking findings fixed in 3e7d3ac4 and 755d4005, or recorded |
| P0-18 merge (51f80b4b) | Merged on autopilot/dev, pushed, TEST_PASSED, not installed | `orchestrator/.venv/bin/python -m pytest ops/autopilot/tests -q -p no:cacheprovider` → `175 passed, 922 subtests passed` |
| P0-19 (f2b9d60d..1820a187) | Implemented, TEST_PASSED on its branch; not reviewed; not merged | Builder's suite `158 passed, 938 subtests passed`; the reviewer's run gave the same. A safety classifier stopped the review before any comparison ran |

## 2026-10-03 — Autopilot cycles 3–5 (10:07–about 16:00 UTC)

- **Cycle 3** (10:07–10:24 UTC, 19 turns): 051b4235 (status-board updates go to PR #98 as comments) and 64e4f699 (gitleaks baseline for two fabricated test values). It ended without updating `RESUME.md`.
- **Cycle 4** (10:24–14:24 UTC) hit the 4 h cycle timeout (rc 124) while a workflow agent was running the final suite. It merged P0-17 (11550954) and took P0-16 through rounds 2–4 on its branch. The round-5 fixes were left uncommitted in `~/work/llm-p016`. `RESUME.md` and `TASK_BOARD.md` were not updated.
- **Cycle 5** (from 14:25 UTC): verified and committed round 5, ran independent reviews in parallel, fixed what they found, merged P0-16, and built P0-18. Agents: a differential regression reviewer, a corpus false-positive reviewer, a P0-17 re-QA, a P0-18 builder, two further guard reviewers and a P0-18 reviewer. Each had its own detached worktree or branch; the main session alone edited the guard.

| Item | State | Evidence (command → result) |
|---|---|---|
| P0-16 round 5 (fdc58202) | Implemented, TEST_PASSED | `orchestrator/.venv/bin/python -m pytest ops/autopilot/tests -q -p no:cacheprovider` in `~/work/llm-p016` → `118 passed, 852 subtests passed` |
| Review of fdc58202, regression lens | Reviewed: ship | 148 inputs through base (autopilot/dev) and branch guards: 0 refused by base and allowed by branch, 0 internal errors |
| Review of fdc58202, corpus lens | Reviewed: fix_first | 2,114 spec and 185,951 extended corpus inputs; 2 regressions and 1 hygiene item, fixed in 9afe1f6a and fb29fb03 (details host-only) |
| Review of 9afe1f6a | Reviewed: fix_first | 383 cases, 18,000 fuzz inputs; 6 regressions in the `find` and argument-joining handling, fixed in 4ff6bbac |
| P0-16 after 4ff6bbac | Implemented, TEST_PASSED | Same suite → `118 passed, 852 subtests passed`. The 383-case replay: no input refused by base and allowed by branch that runs anything (7 quoted `';'` arguments remain, which bash passes as data). Spec corpus replay (2,309 inputs): 3 new refusals (2 correct, 1 false positive → P0-19), 7 read-only relaxations, 0 internal errors, p99 1.04 ms in-process |
| P0-16 merge (04e55f06) | Merged on autopilot/dev, TEST_PASSED, not installed | Merged-tree suite → `152 passed, 859 subtests passed` |
| Last review of fb29fb03..4ff6bbac | Reviewed: fix_first, fixed | 141 new cases plus the earlier 383, malformed `find` inputs: no fail-open; 1 regression (an argument-joining tool's operands skipped the secret-file check; the tool is not installed here), fixed in e969264c, merged b21088b3 → `152 passed, 859 subtests passed` |
| P0-17 (11550954) | TEST_PASSED, not installed | Suite on 11550954 → `116 passed, 859 subtests passed` |
| P0-17 re-QA | Reviewed: fix_first | The first QA's blocking item is fixed. 3 new blocking items → P0-18 (host-only `p0-17-reqa-2026-10-03.md`) |
| P0-18 (c23b17e2..ea25fba1) | Implemented, TEST_PASSED; reviewed: fix_first (all 5 items closed; 1 older gate item, host-only) | Builder's suite → `130 passed, 893 subtests passed`; each new test fails on the previous code (builder's check) |

## 2026-10-03 — Autopilot cycle 2 (07:30–about 10:00 UTC)

Cycle 1 (07:21–07:30 UTC) ended by SIGTERM when the operator restarted the service to add decision 10; it had committed 7ccbe9eb locally. Cycle 2 ran the work in parallel (decision 10): one agent for P0-14, a 7-agent workflow for P0-15, a 6-agent workflow for Phase A, one agent for A-05, one fresh reviewer for the P0-15 patch. Each worked in its own worktree under `~/work` on its own files; this session integrated. Nothing touched production; nothing is installed or deployed.

| Item | State | Evidence (command → result) |
|---|---|---|
| P0-13 CI of PR #98 | TEST_PASSED | Run 37106060235 (bb0ac9f9): `1 failed, 4991 passed` in shard 3, `test_concurrent_callers_share_the_probe_in_flight`. Run 37108039568 (49489640): `1 failed, 5607 passed` in shard 1, `test_a_fatal_clip_cancels_its_siblings_instead_of_leaving_them_decoding`. Run 37109933435 (242bd04d): `1 failed, 6179 passed` in shard 2, `test_two_hours_transcribe_word_for_word_with_no_ceiling_and_flat_memory` (known flake); `gh run rerun --failed` → `completed success`; `gh pr checks 98` → all pass |
| Health-cache flake fix (49489640) | Implemented, TEST_PASSED | Cause: the uncached engine overlay carries real-clock ages rounded to 0.1 s (`breaker` `since_s`): a probe printed `since_s: 0.1 -> 0.2` across 0.15 s. A reproduction with a 0.12 s pause per caller fails on the old fixture, passes on the new one; `tests/test_health_dependency_cache.py` → `43 passed` |
| Video sibling-cancel flake fix (242bd04d) | Implemented, TEST_PASSED | Cause: window 0 raised while the sibling was still building its clip in a worker thread. With every clip build after the first slowed by 0.3 s: old test `assert 0 >= 1` (as on CI), new test passes; both changed files → `46 passed` |
| P0-14 skew list (ba1f619d) | Implemented, TEST_PASSED | See TASK_BOARD P0-14: strict `3 failed, 16982 passed`; `main` `23 failed, 344 passed` on the candidate files; remote `16962 passed, 49 skipped, 30 xfailed, 1 xpassed`, 0 failed |
| P0-15 guardrail review and fixes (95622dc6, 435a7196, df091d61, a8ad8b28) | Implemented, TEST_PASSED, not installed (NH-007) | `orchestrator/.venv/bin/python -m pytest ops/autopilot/tests -q` → `82 passed, 852 subtests passed`; each case added for the second review fails on the previous guard and passes now; hook latency about 33 ms median, 99 ms worst (second reviewer's measurement) |
| Gate under a cleared environment | TEST_PASSED | `env -i HOME=… PATH=/usr/bin:/bin /usr/bin/gh api repos/<slug> --jq .full_name` → the repository name; `env -i … /usr/bin/git push --dry-run origin HEAD:refs/heads/autopilot/dev` → `Everything up-to-date` |
| Phase A discovery (91421fb1) | Documented (code evidence) | `DISCOVERY.md`, 579 lines; host-detail scan clean |
| A-05 capability registry (c418519f) | Documented (metadata) | `CONTEXT_CAPACITY.md`; GETs of `/v1/models`, `/version`, `/metrics` and 5 tiny `/tokenize` calls; no generation |
| Lint and shards on the merged tree | TEST_PASSED | `ruff_gate.py` → clean; `shard_tests.py --check --of 3` → exit 0 |

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
