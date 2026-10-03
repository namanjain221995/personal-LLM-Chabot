# ops/autopilot

Guardrails and the unattended runner that carry out `docs/ai-platform-upgrade/MASTER_PROMPT.md` without an operator session. The operator approved the runner on 2026-10-03 (MASTER_PROMPT.md section 0, decision 9).

The runner starts fresh `claude -p` cycles in the dev worktree, one after another; each cycle reads the governing files, so nothing important lives only in chat history. Between cycles it waits for usage-limit resets, backs off on failures, honours `STOP`/`PAUSE`, and stops at the finish line or at `MAX_AUTONOMOUS_DAYS`.

## Files

| File | Purpose |
|---|---|
| `autopilot.py` | The runner. Installed at `~/.llm-autopilot/bin/autopilot.py` (read-only to the agent); this is the source. |
| `CYCLE_PROMPT.md` | The `/goal` cycle prompt. Installed at `~/.llm-autopilot/bin/CYCLE_PROMPT.md`. |
| `status.sh` | Prints the heartbeat, recent events and the integration branch. Installed at `~/.llm-autopilot/bin/status.sh`. |
| `guard/guard_hook.py` | Layer 3 `PreToolUse` hook (see the matcher below for the tools it covers). Exit 2 blocks with a reason; it fails closed, and a wall-clock deadline (~40 s, under the 60 s hook timeout) blocks rather than letting a slow check time out and fall open. Installed at `~/.llm-autopilot/guard/`. |
| `guard/stop_failure_hook.py` | `StopFailure` hook: appends the failed turn's `error_type` (and `session_id`/`agent_id`) to `~/.llm-autopilot/stopfailure.jsonl` so the runner can tell usage limits from auth failures and outages. |
| `settings.autopilot.template.json` | Template for the session settings, with `{{AP_HOME}}` and `{{HOST_DETAILS}}` placeholders. `install.sh` renders it to `~/.llm-autopilot/settings.autopilot.json` (the installed copy passed with `--settings`). Layer 1 is `permissions.deny`; layer 2 is `autoMode` (`environment`, `allow`, `soft_deny`, `hard_deny`); the hooks are layer 3. |
| `host.example.json` | Template for `~/.llm-autopilot/host.json` (host names, addresses and ports). Never commit the filled copy: this repository is public. |
| `llm-autopilot.service` | The systemd user unit template (`@AP_HOME@`, `@WORKTREE@`, `@HOME@`). `install.sh` renders it to `~/.config/systemd/user/llm-autopilot.service`. Its `MemoryHigh`/`MemoryMax` cap the runner tree (claude, subagents, tests); containers and image builds run under the Docker daemon, outside that cap, so the guard keeps them off the head (worker only, or small `--rm` tool containers). |
| `tests/` | `test_guard_hook.py` (the hook's decisions), `test_guard_sweep.py` (the P0-16 bypass-sweep regression tables, matcher coverage and latency deadline), `test_runner.py` (runner acceptance and units, via `tests/stub_claude.py`), `test_merge_to_dev.py` (the dev gate). |
| `../deploy/merge_to_dev.sh` | The only way the agent moves `dev`: a fast-forward to a reviewed, CI-green commit. Installed at `~/.llm-autopilot/bin/merge_to_dev.sh` and run as a plain command with no environment overrides. |

### Which tools the PreToolUse hook matches, and why

The matcher in `settings.autopilot.template.json` names every tool that can run a
shell command, touch a file, or reach the network or an external service, so none
of them skips layer 3:

| Tool(s) | Why matched | What the guard does |
|---|---|---|
| `Bash`, `Monitor` | both run a shell command (Monitor can also open a WebSocket) | the full command analysis; a Monitor WebSocket to an external host is refused |
| `Write`, `Edit`, `MultiEdit`, `NotebookEdit` | write files | refuse writes outside the autopilot's zones, over guard files or over a directory that holds one |
| `Read`, `Grep`, `NotebookRead` | read file contents | refuse reads of secret files, and recursive greps (including a pathless one, which covers the working directory) rooted at a tree that holds untracked secrets, unless an `--include`/`-g`/`-t` scopes them off secret names and the tree holds no secret directory |
| `Glob` | list file names | refuse only a glob that points at a named secret path; a name-listing pattern such as `**/*.env` is allowed because a returned path is re-checked when `Read`/`Grep` opens it |
| `WebFetch`, `WebSearch` | reach a URL or an external search service | refuse a production host, a private/link-local/loopback address, or a credential-shaped string |
| `Agent`, `Task`, `Workflow`, `Skill` | spawn subagents / run a workflow (the operator REQUIRES multiple agents) | stay allowed — a subagent's own tool calls pass back through this hook — except an input that carries a secret shape or asks for remote/cloud isolation (running outside the local guard) |
| `CronCreate`, `ScheduleWakeup` | schedule a prompt or a wakeup | refuse only when the prompt carries a secret shape (the scheduling itself is a layer-1/2 concern) |
| `EnterWorktree` | can move the session's write access | refuse pointing at the production checkout, `~/Documents`, a secret path or the guard files |
| `mcp__.*` | MCP tools reach external services | refuse unless the tool name is clearly read-only (get/list/read/view/search/...) |

The command analysis reads shell text best-effort and fails closed on anything
it cannot follow. It resolves a `$VAR` only to a literal value a command set
earlier in the same text in a segment certain to run, and only when that value
is one word bash could neither split nor glob; it never resolves the command
word, and it resolves nothing in a command that defines a function, sources a
file, uses `printf -v`, a `:=`/`=` default-assignment or arithmetic, or sets
`IFS`. So a computed command name, a space-separated refspec or a glob built
from a variable stays unresolved and trips the literal-name / literal-refspec /
literal-path refusals. The `docker` and `docker compose` parsers consume each
global option's value (so a hidden `--tlskey`/`--workdir` cannot shift the
subcommand) and fail closed on an unrecognised global option or subcommand.

A variable set by a segment that may not run, or computed at run time, becomes
uncertain rather than unset: the checks that read `TEST_DATABASE_URL`,
`DOCKER_HOST`/`DOCKER_CONTEXT`/`DOCKER_CONFIG`, `TECHSARA_STACK` or `COMPOSE_*`
then refuse instead of falling back to the hook's own environment. A value set
earlier in the same `&&` chain counts as set. A `case` statement's arm patterns
are read as patterns, never as commands; its arm bodies are analysed like any
other command. `grep`, `rg` and `ag` are parsed per tool (options end at `--`,
each tool has its own value-taking options), and a pattern word or option value
that names an existing secret file is refused as well. A `#!/bin/sh` file is
read as a Python console-script entry point only when its header is exactly the
one pip/distlib writes and its interpreter is a real binary; otherwise it is
analysed as shell.

The matcher is a fixed alternation, so only the tools above (and any `mcp__*`)
reach layer 3; a tool not named here is left to layers 1 and 2 (`permissions.deny`
and the auto-mode classifier). For a matched tool the guard does not specially
model, the `evaluate()` fall-through still applies the shared safety net: a
`command` field is analysed like Bash, a path field runs the secret-file checks,
and a credential-shaped string anywhere in its input is refused.
`test_guard_sweep.py` asserts the template JSON parses and that its matcher
covers every tool in this table.

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

Ctrl-C during the `--restart-after-cycle` wait (or `TERM`, or the terminal closing) removes the `STOP` file that run created and leaves the old runner running its cycle on the old code; it says so, and whether the runner is still up. A `STOP` that was there before the run stays. Further signals are ignored while it cleans up, so pressing Ctrl-C twice cannot leave `STOP` behind. Re-run `--restart-after-cycle` to try again. Once install.sh has seen the old runner exit (it checks every 2 s), these signals are ignored until `systemctl --user start` returns, so an interrupt then cannot leave `STOP` removed and the runner down; that start can take a few minutes while the old unit finishes stopping, and Ctrl-C does nothing meanwhile. An interrupt in the short gap between the runner's exit and that check still goes to the cleanup above: it removes `STOP`, says the runner is not active and prints the command that starts it.

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

- A cycle runs for at most 4 hours (`AP_CYCLE_TIMEOUT_S`); a CLI that ignores the timeout's SIGTERM is killed 120 s later (`AP_KILL_AFTER_S`). Reaching the timeout or the turn limit is a normal end, also when the CLI had to be killed; the next cycle starts after the short pause. `timeout` treats 0 as "no limit", so an `AP_CYCLE_TIMEOUT_S` that is not a whole number of at least 60 seconds (`7200.0` counts as not whole) or an `AP_KILL_AFTER_S` of 0 or less (or either one unparsable) is ignored: the runner uses the default and records an `operator-setting-ignored` event when it starts. Its `runner-start` event shows the values in force.
- A crash restarts after 60 s; six crashes in a row (interrupted cycles included, below) start a 2-hour sleep (the crash cap).
- A cycle cut off by a signal (`systemctl --user restart`, a stray SIGTERM) is *interrupted*. Interrupted cycles count toward the crash cap like crashes, but a lone one never triggers the cap: after it the runner resumes after the short pause. A second interruption in a row waits like a crash.
- The runner sets `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS=0` for every cycle, so print mode keeps the cycle open while its background tasks (a running workflow) finish instead of ending them 600 s after the main turn. The 4-hour cycle timeout still bounds that wait.

Exit codes: `0` a test run finished, `3` another instance holds the lock, `64` stopped on purpose (`STOP`, complete, or expired — systemd does not restart on `3` or `64`).
