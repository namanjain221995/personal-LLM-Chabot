# Security review

Statuses of security work. The repository is public (operator decision 6): concrete findings about the hosts are kept in `~/.llm-autopilot/agent/private/` and only counted here.

## Autopilot guardrails

| ID | Item | Status | Evidence |
|---|---|---|---|
| S-001 | Layer 1 deny rules for the §3.3 limits and the operator decisions (privilege escalation, force push, pushes to `main` and `dev`, remote deletes, prunes, volume removal, `gh pr merge`, workflow dispatch, repository settings and secrets, guard and settings files, MCP tools) | Implemented; rendered and installed by `install.sh` | `ops/autopilot/settings.autopilot.template.json` |
| S-002 | Layer 2 auto-mode environment and rules for this host (host details appended from the host-only config at install) | Implemented | same file; `install.sh` |
| S-003 | Layer 3 PreToolUse hook: fail-closed; symlink-resolved write zones; secret-read blocking; read-only production checkout, containers and database; compose projects rendered and refused when they resolve to production; long-lived dev services only on the worker; `dev` only through the installed gate; secret scan of outgoing commits and PR text | Implemented; TEST_PASSED | `ops/autopilot/tests/test_guard_hook.py` |
| S-004 | The hook reads shell text best-effort and cannot see inside programs the agent runs | Accepted residual risk; layers 1–2 and the final independent review cover it | AD-003 |
| S-005 | Process listings and service logs can contain secrets | Open; runner logs are redacted (credential shapes and the exact values of the production secret files) | `ops/autopilot/autopilot.py` `Redactor` |
| S-006 | The autopilot's test database credentials live in a host-only file and the cycle's environment, never in the repository | Implemented | NH-003 |

## Host findings

Host findings recorded on 2026-10-03 are kept in `~/.llm-autopilot/agent/private/HOST_FINDINGS.md` and reported to the operator. The programme does not change production or remove resources it did not create.
