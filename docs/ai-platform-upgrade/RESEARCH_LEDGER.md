# Research ledger

One entry per material finding: question, source, passage, date or version, conclusion, limitations, affected implementation. Observed facts, hypotheses and results are kept apart.

## R-001 — Path anchors in permission rules

- **Question.** How do `Read(...)` and `Edit(...)` rules write absolute paths?
- **Source.** https://code.claude.com/docs/en/permissions.md, fetched 2026-10-03.
- **Passage.** "`//path` — Absolute path from filesystem root … `/path` — Path relative to the settings source … A pattern like `/Users/alice/file` isn't an absolute path. The single leading slash anchors at the settings source, not the filesystem root."
- **Conclusion.** Use `//…` or `~/…` in `settings.autopilot.json`. A discovery subagent had reported the opposite; the primary source wins.
- **Affects.** `ops/autopilot/settings.autopilot.json` deny rules.

## R-002 — Deny rules and compound commands

- **Source.** Same page.
- **Passage.** "Deny and ask rules apply when any subcommand matches them, including a command nested inside a subshell, a command substitution, or a control-flow body…"; wrappers `timeout`, `time`, `nice`, `nohup`, `stdbuf`, `command`, `builtin` are stripped; "A deny or ask rule matches past any leading assignment".
- **Limitation.** "It doesn't match the same program invoked in a different form" (by path, inside `sh -c`).
- **Conclusion.** Deny rules are layer 1; the PreToolUse hook covers other spellings.

## R-003 — Read deny rules also block writes; Edit rules cover Write

- **Source.** Same page.
- **Passage.** "A `Read` deny rule also blocks the Edit and Write tools on the same path, including creating a new file there." "`Edit` rules apply to all built-in tools that edit files." Rules written for `Write`, `NotebookEdit`, `Glob` or `MultiEdit` are accepted but never consulted.
- **Conclusion.** Read denies are limited to production secrets and credential stores, so the autopilot can still create its own dev env files.

## R-004 — Tool-name globs

- **Passage.** "`\"mcp__*\"` matches every MCP tool across all servers. A tool matched by a bare-name glob deny rule is removed from Claude's context."
- **Conclusion.** The autopilot gets no MCP connectors (Drive, Atlassian, Docs).

## R-005 — Auto-mode configuration

- **Source.** `claude auto-mode --help`, `claude auto-mode defaults` on 2.1.288 (82 KB JSON).
- **Observed.** Keys `environment`, `allow`, `soft_deny`, `hard_deny`; `"$defaults"` keeps the built-in list; defaults include soft_deny "Create Unsafe Agents", "Production Deploy", "Production Reads", "Remote Shell Writes", "Merge Without Review", "Self-Modification", "Instruction Poisoning", and the hard_deny "Data Exfiltration".
- **Conclusion.** The environment list is written in full (labelled entries), and allow entries name the gated deploy scripts, integration pushes, the status PR, the worker dev stack and read-only telemetry.

## R-006 — Usage-limit messages

- **Source.** https://code.claude.com/docs/en/errors.md (via discovery subagent, 2026-10-03).
- **Passage.** "You've hit your session limit · resets 3:45pm"; "You've hit your weekly limit · resets Mon 12:00am".
- **Limitation.** Not observed live; the runner's parser must also accept other formats and fall back to 20/40/60-minute backoff.

## R-007 — StopFailure hook

- **Source.** https://code.claude.com/docs/en/hooks.md (via discovery subagent).
- **Observed.** Input has `error_type` (rate_limit, overloaded, authentication_failed, oauth_org_not_allowed, account_on_hold, billing_error, invalid_request, model_not_found, server_error, max_output_tokens, cloud_credential_error, unknown); no reset time; output and exit code are ignored, but the command runs, so it can append to a file.

## R-008 — `/goal` and print mode

- **Source.** https://code.claude.com/docs/en/goal.md (via discovery subagent).
- **Observed.** Usable as `claude -p "/goal <condition>"`; condition up to 4,000 characters; cleared after unrecoverable errors.
- **Limitation.** Whether `/goal` needs workspace trust in an untrusted folder under `-p` is unclear in the docs. TEST_NOT_RUN (needs NH-001).

## R-009 — No tool memory-limit variable

- **Observed.** `CLAUDE_CODE_TOOL_MEMORY_LIMIT` is not in the environment-variable reference for 2.1.288. The user systemd manager delegates `cpu memory pids`, so a user unit can cap memory with `MemoryHigh`/`MemoryMax` and set `OOMScoreAdjust`; `io` is not delegated, so I/O priority comes from `ionice`/`IOSchedulingClass`.
