# Claude Code notes for this repository

## Platform upgrade programme (autopilot)

- The programme's governing text is `docs/ai-platform-upgrade/MASTER_PROMPT.md` (host copy: `~/.llm-autopilot/MASTER_PROMPT.md`); the operator decisions in its §0 win over everything else. Its state files live next to it: `RESUME.md`, `TASK_BOARD.md`, `NEEDS_HUMAN.md`.
- Hard limits are in its §3.3. In short: no sudo; no force push, history rewrite or deleting others' branches; never push or merge to `main`; `dev` moves only through the installed gate `~/.llm-autopilot/bin/merge_to_dev.sh`; never delete weights, volumes, databases, documents, chats, memory or backups; never weaken security controls; never print, log, commit or hand a subagent any secret; no private data to external services; no paid services or hosted-model substitution; no training on employee conversations; no disruptive load on production; keep 15% disk free; never modify the autopilot's guardrails.
- Never edit the production checkout `~/Documents/project/personal-LLM-Chabot`. It is the deploy root and is bind-mounted into running containers. Work in a worktree under `~/work/`.
- The autopilot works in `~/work/llm-dev` on `autopilot/dev`. Before editing there, check `~/.llm-autopilot/lock` and `~/.llm-autopilot/heartbeat.json`. If the autopilot is running, work read-only or `touch ~/.llm-autopilot/PAUSE` first and remove it when done. Two agents editing one tree corrupt each other's work.
- The repository is public. Committed documents, code and configuration carry no host names, addresses, internal endpoints or security findings; those live in `~/.llm-autopilot/host.json` and `~/.llm-autopilot/agent/private/`.
- The compose files take their project, volume and image names from `TECHSARA_STACK` (default: production's). A dev stack passes `--env-file ops/dev/stack.vars` and runs on the worker node; never run `docker compose` against the production project outside the production deploy.
- Orchestrator tests truncate every table. They need `TEST_DATABASE_URL` (a database ending in `_test`) and `TEST_DATABASE_ALLOWED_HOSTS`; there is no default server.
