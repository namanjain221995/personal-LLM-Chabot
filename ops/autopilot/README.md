# ops/autopilot

Guardrails and the unattended runner that carry out `docs/ai-platform-upgrade/MASTER_PROMPT.md` without an operator session. The operator approved the runner on 2026-10-03 (MASTER_PROMPT.md section 0, decision 9).

The runner starts fresh `claude -p` cycles in the dev worktree, one after another; each cycle reads the governing files, so nothing important lives only in chat history. Between cycles it waits for usage-limit resets, backs off on failures, honours `STOP`/`PAUSE`, and stops at the finish line or at `MAX_AUTONOMOUS_DAYS`.

## Files

| File | Purpose |
|---|---|
| `autopilot.py` | The runner. Installed at `~/.llm-autopilot/bin/autopilot.py` (read-only to the agent); this is the source. |
| `CYCLE_PROMPT.md` | The `/goal` cycle prompt. Installed at `~/.llm-autopilot/bin/CYCLE_PROMPT.md`. |
| `status.sh` | Prints the heartbeat, recent events and the integration branch. Installed at `~/.llm-autopilot/bin/status.sh`. |
| `guard/guard_hook.py` | Layer 3 `PreToolUse` hook for Bash, Write, Edit, MultiEdit, NotebookEdit, Read and Grep. Exit 2 blocks with a reason; it fails closed. Installed at `~/.llm-autopilot/guard/`. |
| `guard/stop_failure_hook.py` | `StopFailure` hook: appends the failed turn's `error_type` (and `session_id`/`agent_id`) to `~/.llm-autopilot/stopfailure.jsonl` so the runner can tell usage limits from auth failures and outages. |
| `settings.autopilot.template.json` | Template for the session settings, with `{{AP_HOME}}` and `{{HOST_DETAILS}}` placeholders. `install.sh` renders it to `~/.llm-autopilot/settings.autopilot.json` (the installed copy passed with `--settings`). Layer 1 is `permissions.deny`; layer 2 is `autoMode` (`environment`, `allow`, `soft_deny`, `hard_deny`); the hooks are layer 3. |
| `host.example.json` | Template for `~/.llm-autopilot/host.json` (host names, addresses and ports). Never commit the filled copy: this repository is public. |
| `llm-autopilot.service` | The systemd user unit template (`@AP_HOME@`, `@WORKTREE@`, `@HOME@`). `install.sh` renders it to `~/.config/systemd/user/llm-autopilot.service`. Its `MemoryHigh`/`MemoryMax` cap the runner tree (claude, subagents, tests); containers and image builds run under the Docker daemon, outside that cap, so the guard keeps them off the head (worker only, or small `--rm` tool containers). |
| `tests/` | `test_guard_hook.py` (the hook's decisions), `test_runner.py` (runner acceptance and units, via `tests/stub_claude.py`), `test_merge_to_dev.py` (the dev gate). |
| `../deploy/merge_to_dev.sh` | The only way the agent moves `dev`: a fast-forward to a reviewed, CI-green commit. Installed at `~/.llm-autopilot/bin/merge_to_dev.sh` and run as a plain command with no environment overrides. |

The gate also refuses a commit whose `.github/` tree differs from `origin/dev`, because that commit's checks could have been graded by edited CI. When the programme changes `.github/`, the operator reviews `git diff origin/dev <commit> -- .github/` and approves it by adding the tree hash the gate prints as one line of `~/.llm-autopilot/approved-ci-trees` (operator-owned; the agent cannot write it). The agent raises a `NEEDS_HUMAN.md` item as soon as the trees first differ.

Runtime state lives in `~/.llm-autopilot/`: `state.json`, `heartbeat.json`, `events.jsonl`, `logs/`, `lock`, and the control files `STOP`, `PAUSE`, `WAKE`, `RENEW`. These and the installed copies above are read-only to the agent (the guard, the deny rules and the classifier all refuse to change them).

## Tests

```bash
# system python3 has no pytest; use a venv that does, e.g. the orchestrator's:
orchestrator/.venv/bin/python -m pytest ops/autopilot/tests -q -p no:cacheprovider
# or, where python3 already has pytest:
python3 -m pytest ops/autopilot/tests -q -p no:cacheprovider
```

`test_runner.py` scales time down (`AP_TIME_SCALE`) and uses a stub `claude`, so the acceptance tests cost no usage and finish in under a minute.

## Install, start, stop (operator only)

`install.sh` refuses to run inside an autopilot session. It needs `~/.llm-autopilot/host.json` and `~/.llm-autopilot/MASTER_PROMPT.md` to exist first.

```bash
ops/autopilot/install.sh                          # install + enable (does not start)
ops/autopilot/install.sh --start                  # install + enable + start (first time)
ops/autopilot/install.sh --restart-after-cycle    # install, then STOP, wait for the
                                                   # cycle to finish, and start the new code
```

Re-run `install.sh` after changing anything here. The guard hooks, settings and `CYCLE_PROMPT.md` are re-read on every call or cycle and pick up changes at once. **`autopilot.py` is the exception:** `systemctl start` on an active unit is a no-op, so the running process keeps the old runner code until it is restarted. Use `--restart-after-cycle`; a plain `systemctl --user restart` would SIGTERM the in-flight cycle. `install.sh` warns when `autopilot.py` changed while the runner was active.

Ctrl-C during the `--restart-after-cycle` wait (or `TERM`, or the terminal closing) removes the `STOP` file that run created and leaves the old runner running its cycle on the old code; it says so, and whether the runner is still up. A `STOP` that was there before the run stays. Further signals are ignored while it cleans up, so pressing Ctrl-C twice cannot leave `STOP` behind. Re-run `--restart-after-cycle` to try again.

Control the running runner through the host-only files:

```bash
touch ~/.llm-autopilot/STOP     # finish the current cycle, then exit (service stays stopped)
touch ~/.llm-autopilot/PAUSE    # sleep without starting cycles; remove to resume
touch ~/.llm-autopilot/WAKE     # end a wait (usage-limit reset, backoff) early
touch ~/.llm-autopilot/RENEW    # after MAX_AUTONOMOUS_DAYS: reset the clock and continue
~/.llm-autopilot/bin/status.sh  # heartbeat, events, branch
```

A pending wait (a usage-limit reset or the crash cap) is kept in `state.json` across `STOP`, `PAUSE` and a restart, so the runner still honours it when it comes back; `WAKE` clears it on purpose.

How a cycle ends decides what comes next:

- A cycle runs for at most 4 hours (`AP_CYCLE_TIMEOUT_S`); a CLI that ignores the timeout's SIGTERM is killed 120 s later (`AP_KILL_AFTER_S`). Reaching the timeout or the turn limit is a normal end, also when the CLI had to be killed; the next cycle starts after the short pause.
- A crash restarts after 60 s; six crashes in a row (interrupted cycles included, below) start a 2-hour sleep (the crash cap).
- A cycle cut off by a signal (`systemctl --user restart`, a stray SIGTERM) is *interrupted*. Interrupted cycles count toward the crash cap like crashes, but a lone one never triggers the cap: after it the runner resumes after the short pause. A second interruption in a row waits like a crash.
- The runner sets `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` for every cycle, so print mode keeps the cycle open while its background tasks (a running workflow) finish instead of ending them 600 s after the main turn. The 4-hour cycle timeout still bounds that wait.

Exit codes: `0` a test run finished, `3` another instance holds the lock, `64` stopped on purpose (`STOP`, complete, or expired — systemd does not restart on `3` or `64`).
