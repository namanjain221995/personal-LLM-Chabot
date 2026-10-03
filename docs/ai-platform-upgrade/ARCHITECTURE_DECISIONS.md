# Architecture decisions

Non-obvious decisions of the upgrade programme: the decision, the alternatives, the evidence and how to reverse it. Operator decisions of 2026-10-03 are recorded in `MASTER_PROMPT.md` §0 and win over anything here.

## AD-001 — Integration branch `autopilot/dev` in `~/work/llm-dev` (2026-10-03)

- **Decision.** All programme work happens in the worktree `~/work/llm-dev` on `autopilot/dev` (operator decision 1), created from what production runs (`3c75af1c`). Feature branches `upgrade/<workstream>/<slug>` merge into it; `main` is merged in regularly; shared history is never rebased.
- **Why.** The production checkout is the deploy root and is bind-mounted into running containers. `dev` is checked out in the operator's `devapi` worktree and is the operator's release branch.
- **Reverse.** `git worktree remove ~/work/llm-dev` after its branch is pushed.

## AD-002 — The production checkout and everything under `~/Documents` are read-only to the autopilot (2026-10-03)

- **Decision.** The guard hook refuses writes under `~/Documents` and mutating commands whose working directory is there.
- **Why.** Files there reach production without a deploy, a dirty tree blocks every deploy, and a checkout that deletes a mounted directory silently breaks running containers.
- **Reverse.** Edit `WRITE_ALLOW` in `ops/autopilot/guard/guard_hook.py` and re-run `ops/autopilot/install.sh` (operator only).

## AD-003 — Three guard layers; the hook fails closed (2026-10-03)

- **Decision.** Layer 1: `permissions.deny` (applies to every subcommand, including `$(…)`, after wrappers are stripped). Layer 2: the auto-mode classifier with this host's environment and extra allow, soft_deny and hard_deny rules. Layer 3: a PreToolUse hook that parses shell text (quotes, heredocs, substitutions, `bash -c`, `ssh host cmd`, `docker exec`, `find -exec`, `xargs`, script files), resolves paths through symlinks, renders compose projects, and scans outgoing commits for secrets before a push. Internal errors, unparseable commands, malformed payloads and a missing host config block the call.
- **Why.** Deny rules match command text as written; the classifier is semantic but probabilistic; the hook is deterministic but best-effort. A hook that crashes or times out does not block, so the hook catches its own exceptions.
- **Alternatives.** The built-in sandbox: it would need broad exceptions for Docker, SSH to the worker and GitHub.
- **Reverse.** Remove the `PreToolUse` entry from the template and reinstall (operator only).

## AD-004 — Host details stay out of the public repository (2026-10-03)

- **Decision.** Committed documents, code and configuration contain no host names, addresses, internal endpoints or security findings (operator decision 6). The guard and the settings renderer read them from `~/.llm-autopilot/host.json` (template: `ops/autopilot/host.example.json`); findings go to `~/.llm-autopilot/agent/private/`. Tests use documentation addresses (RFC 5737).
- **Reverse.** None needed.

## AD-005 — No fixed subagent cap (2026-10-03, reverses the first Phase 0 choice)

- **Decision.** The `Workflow` and `Agent` tools are allowed without a fixed cap (operator decision 4). Runtime agents in the application are limited only by the shared run budget and measured DGX capacity.
- **Risk.** Parallel subagent waves have used up the shared Max plan's weekly limit before; `AUTOPILOT_PAUSE_WINDOWS` can keep hours free for the operator.

## AD-006 — The runner (2026-10-03)

- **Decision.** `ops/autopilot/autopilot.py` runs fresh `claude -p --permission-mode auto --permission-prompts none` cycles under the systemd user unit `llm-autopilot.service`, approved by the operator on 2026-10-03. The installed copies in `~/.llm-autopilot/bin` and `~/.llm-autopilot/guard` are what runs; the agent cannot edit them (hook, deny rules, classifier, file modes).
- **Behaviour.** Single instance (`flock`); usage limits wait for the reset time plus 2–5 min of jitter, else 20 → 40 → 60 min; network and overload errors back off exponentially; auth failures write `~/.llm-autopilot/NEEDS_HUMAN.runtime.md` and retry every 30 min; crashes restart after 60 s and sleep 2 h after six in a row; `STOP`, `PAUSE`, `WAKE` and pause windows; heartbeat every minute; logs per cycle with secrets redacted and rotation by count and size; stops on `STATUS: COMPLETE` with `FINAL_REPORT.md`, or after `MAX_AUTONOMOUS_DAYS`.
- **Resources.** `Nice=10`, best-effort I/O priority 7, `CPUWeight=20`, `MemoryHigh=5G`, `MemoryMax=6G`, `OOMScoreAdjust=1000` (NH-006).

## AD-007 — Parameterized compose names (2026-10-03)

- **Decision.** `TECHSARA_STACK` (default `sf-local-ai`) names the compose project, its volumes and its image tags. Production never sets it; `ops/dev/stack.vars` sets it for a dev stack, which runs on the worker's Docker daemon (the pinned v1relay subnet stays as it is: a dev stack there cannot collide with it, and the launcher's network-exposure test forbids address defaults).
- **Evidence.** All 12 production renders byte-identical before and after (`CURRENT_STATE.md`, Isolation).
- **Alternatives.** A separate dev compose file: it would drift from production. Leaving names pinned: a dev stack would mount production's database volume.
- **Reverse.** Revert the compose commit; production output is unchanged either way.

## AD-008 — Test database contract (2026-10-03)

- **Decision.** No default test server; the database name must end in `_test`; the server's host:port must be listed in `TEST_DATABASE_ALLOWED_HOSTS` (GitHub Actions jobs exempt); the existing server guard still refuses any server holding a non-test database. The autopilot's database runs on the worker; tests that compare Python timestamps with the server's `now()` are expected failures only when `TEST_DATABASE_REMOTE=1` (`CLOCK_SKEW_SENSITIVE` in `orchestrator/tests/conftest.py`), so CI still checks them strictly.
- **Why.** The old default was another session's test server on a shared host, and the suite truncates every table.
- **Reverse.** Revert the conftest commit.

## AD-009 — `dev` moves only through an installed gate (2026-10-03)

- **Decision.** `ops/deploy/merge_to_dev.sh` fast-forwards `origin/dev` to the pushed tip of `autopilot/dev` only when every check run on that exact commit completed as success, skipped or neutral, `CI passed` succeeded, the combined status is success, and `docs/ai-platform-upgrade/FINAL_REPORT.md` exists in the commit. The guard allows only the installed copy (`~/.llm-autopilot/bin/merge_to_dev.sh`) and blocks every other push to `dev`, every push to `main`, `gh pr merge`, workflow dispatch and re-runs of `main` runs.
- **Evidence.** `ops/autopilot/tests/test_merge_to_dev.py` (6 tests against a throwaway origin and a fake `gh`).
- **Reverse.** Operator action only.
