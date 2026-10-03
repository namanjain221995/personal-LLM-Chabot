# ops/autopilot

Guardrails for unattended Claude Code sessions that carry out `docs/ai-platform-upgrade/MASTER_PROMPT.md`.

Status (2026-10-03): the guard layers exist and the hook is unit-tested. The runner that would start the sessions is **not written**: Claude Code's auto-mode classifier refused to let an agent create it, and the operator decides how to proceed (`docs/ai-platform-upgrade/NEEDS_HUMAN.md`, NH-001).

| File | Purpose |
|---|---|
| `settings.autopilot.json` | Passed with `--settings`. Layer 1 `permissions.deny`, layer 2 `autoMode` (environment of this host, extra allow, soft_deny and hard_deny rules), and both hooks. Installed at `~/.llm-autopilot/settings.autopilot.json`. |
| `guard/guard_hook.py` | Layer 3 PreToolUse hook for Bash, Write, Edit, MultiEdit, NotebookEdit, Read and Grep. Exit 2 blocks with a reason. Fails closed. Installed at `~/.llm-autopilot/guard/`. |
| `guard/stop_failure_hook.py` | StopFailure hook: appends the failed turn's `error_type` to `~/.llm-autopilot/stopfailure.jsonl` so a runner can tell usage limits from auth failures and outages. |
| `tests/test_guard_hook.py` | Unit tests for the hook. |

Run the tests:

```bash
python3 -m unittest discover -s ops/autopilot/tests -v
```

The installed copies in `~/.llm-autopilot/` are what sessions load; the agent may not modify them (the hook, the deny rules and the classifier all refuse). Change the source here and reinstall as the operator.
