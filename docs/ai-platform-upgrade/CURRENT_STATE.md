# Current state

Facts established in Phase 0 on 2026-10-03. Each fact names the command that produced it. The repository is public (operator decision 6): host names, addresses, ports, internal endpoints and security findings are kept on the host in `~/.llm-autopilot/host.json` and `~/.llm-autopilot/agent/private/`, not here.

## Hosts

| Fact | Value | Evidence |
|---|---|---|
| Production head | The host the autopilot runs on: aarch64 DGX Spark, 20 cores, 121 GiB unified memory; it runs the `sf-local-ai` compose project from the production checkout | `uname -m; nproc; free -g`; `docker ps` compose labels |
| Head memory at 11:00 IST | 76 GiB used, 45 GiB available, swap 31 of 63 GiB used | `free -g` |
| Head disk | 3.7 TB, 33% used | `df -h /` |
| Worker node | Second DGX Spark, reachable with existing SSH keys; 64 GiB available, 2.9 TB free | `ssh -o BatchMode=yes <worker> 'free -g; df -h /'` |
| Owner rule | Nothing new may use head memory (2026-09-16); dev and test services go on the worker | memory note `head-memory-is-off-limits` |

## Claude Code

| Fact | Value | Evidence |
|---|---|---|
| CLI | `~/.npm-global/bin/claude`, version 2.1.288 | `which -a claude; claude --version` |
| Auth | Logged in with a claude.ai Max subscription | `claude auth status` |
| Unattended call | Exit 0, result `OK`, 1 turn | `claude -p "Reply with exactly the two letters OK…" --model haiku --max-turns 1 --permission-prompts none --no-session-persistence --output-format json` |
| Flags | `--max-turns` accepted although `--help` omits it; `--permission-mode auto`, `--permission-prompts none`, `--settings`, `--effort` present | same call; `claude --help` |
| Auto-mode rules | 17 allow, 72 soft_deny, 1 hard_deny, 21 environment defaults | `claude auto-mode defaults` |
| Memory cap | No tool memory-limit variable; the user systemd manager delegates `cpu memory pids` | `cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers` |

## Supervisors

| Fact | Value | Evidence |
|---|---|---|
| systemd user manager | Running; linger on | `loginctl show-user "$USER" -p Linger` |
| Existing user units | GitHub Actions runner for this repo, `techsara-brain.timer`, `techsara-reconcile.timer` (restarts stopped `sf-local-ai` containers every 2 min) | `systemctl --user list-units`, `list-timers` |

## Git and GitHub

| Fact | Value | Evidence |
|---|---|---|
| Production checkout | `~/Documents/project/personal-LLM-Chabot`, branch `main`, clean, HEAD `3c75af1c` (deployed by Pipeline run 37092252160) | `git status --short; git rev-parse HEAD`; `gh run list` |
| Baseline tag | `baseline/pre-upgrade-2026-10-03` → `3c75af1c` | `git tag -a …` |
| `DEV_BRANCH` | `autopilot/dev` (operator decision 1), worktree `~/work/llm-dev`, created from `3c75af1c` | `git worktree add -b autopilot/dev ~/work/llm-dev origin/main` |
| `dev` | Release target (operator decision 2); checked out in the operator's `devapi` worktree; moves only through `ops/deploy/merge_to_dev.sh` | `git worktree list` |
| Shared repository | 378 worktrees and 397 local branches share one `.git`, including the stash, config and `info/exclude` | `git worktree list \| wc -l` |
| Remote | `github.com/namanjain221995/personal-LLM-Chabot`, public | `gh repo view --json visibility` |
| Protection on `main` | Required check `CI passed`; admins not enforced; no required reviews | `gh api repos/…/branches/main/protection` |
| CI triggers | `pull_request` (any branch), `push` to `main` and `dev`, `workflow_dispatch`. A push to `autopilot/dev` alone runs no CI; the draft PR `autopilot/dev → dev` does | `.github/workflows/pipeline.yml` |
| Deploys | Every push to `main` deploys production; the agent never pushes or merges to `main` (operator decisions 2 and 5) | `pipeline.yml` deploy condition |
| CI duration | About 30–35 minutes for a dev push | `gh run view --json jobs` |

## Isolation

- The production checkout is bind-mounted into running containers, and some files there act without a deploy (knowledge packs, dashboards, the reconcile script). It is read-only to the autopilot (guard hook, deny rules).
- No container mounts anything under `~/work` or `~/.llm-autopilot`.
- The compose files now take the project, volume and image names from a variable whose defaults are today's values. `docker compose config` of all three production chains (core, +cloudflare, +monitoring; production profiles and all profiles; YAML and JSON) is byte-identical before and after: 12 of 12 renders, same SHA-256. With `--env-file ops/dev/stack.vars` the same files render project `llmdev` and only `llmdev_*` volumes and networks.
- The guard renders every mutating `docker compose` command (`docker compose … config --format json`, in-process, nothing printed) and blocks it when anything resolves to the production project; long-lived dev services must run on the worker's Docker daemon.
- The orchestrator suite runs only against the autopilot's dedicated test database on the worker (operator decision 7). Its clock was 21.5 ms behind the head's (median of 15 samples, round trip 0.05 ms).

## Production mechanisms (operator-owned)

- The Pipeline `deploy` job runs `scripts/deploy.sh --ref $GITHUB_SHA` in the production checkout after `CI passed`: a lock, a clean-tree check, a rolling `./techsara up` that keeps the main model running, health checks and an automatic rollback when its reversibility verdict allows. Manual rollback: `scripts/deploy-rollback.sh`.
- Backups are manual; the newest database dump predates the programme by a month (NEEDS_HUMAN NH-004).

## Inference

The main model (Qwen3.6-35B-A3B-NVFP4, tensor-parallel across both nodes) reports a maximum model length of 1,000,000 tokens; a router, an embedding model and a reranker run beside it (`GET /v1/models` on each engine). Endpoint addresses are in the host config.

## Low-traffic window

Measured from 14 days of Prometheus data (`chat_request_total`, `vllm:request_success_total`): early morning IST is the quietest. The hours used by the guard come from the host config; hourly counts are in the host-only notes. Phase B re-measures it from 7 or more days of request logs.
