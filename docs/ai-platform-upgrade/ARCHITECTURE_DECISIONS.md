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

## AD-010 — The dev stack: compose overlay on the worker, production main engine behind a two-request cap (2026-10-04)

- **Decision.** `ops/dev/` runs `compose.yaml`'s application plane (postgres, the orchestrator's CPU image, the frontend) as project `llmdev` on the worker's Docker daemon (`DOCKER_HOST=ssh://<worker>`), through the overlay `ops/dev/compose.dev.yaml`: literal `llmdev*` names, memory and CPU limits, `oom_score_adj` 1000, `restart: "no"`, loopback ports 28080/23000, no bind mounts, Salesforce/search/speech/video/OCR/v1-gateway off. Secrets are synthetic and generated by `ops/dev/init-env.sh` into gitignored files. Every engine call goes through `inference-cap` (`ops/dev/inference_cap/`, standard library only), which holds the whole stack to two in-flight non-GET requests across all upstreams; the limit cannot be raised above 2.
- **Why.** §9.3 asks for a separate stack that reuses production inference with at most two dev requests in flight. The orchestrator has no global engine cap (its admission lanes cover only the main engine and are separate lanes), so the cap is enforced outside the application, where a dev setting cannot loosen it. The owner's rule keeps new long-lived services off the head.
- **Engines.** From the worker only the main engine answers; the router, embedding and reranker engines listen on the head's loopback. By default the main model therefore also serves router, agent and vision calls, and embeddings and reranking are off; `init-env.sh --router/--embed/--rerank` takes addresses when they become reachable (NH-013).
- **Alternatives.** `scripts/e2e-stack.sh` (head, copies the production engine contract; refused by the owner's head-memory rule); separate dev engines on the worker (GPU contention with the tensor-parallel main model); a reverse tunnel started on the head (`scripts/aiq/aiq-stack.sh` precedent; a long-lived process outside the guard's view, so left to the operator).
- **Parity gaps.** The CPU image instead of production's CUDA image (no in-process reranker; production uses the remote one), no brain packs, no Salesforce warehouse (`/health` reports `duckdb` error), no web search; listed in `ops/dev/README.md`.
- **Reverse.** `DOCKER_HOST=ssh://<worker> ops/dev/devstack.sh down`; the `llmdev_*` volumes and images stay until the operator removes them.


## AD-011 — How the baseline is measured and what counts as a regression (2026-10-04)

- **Decision.** The programme's baseline is the 16-case evaluation set (B-02) run by `scripts/aiq/run_evalset.py` against the dev stack, three to five run directories of one repeat each, every run on its own fresh account, pooled and frozen by `scripts/aiq/baseline.py`. Quality gates per case, per check of a case and overall, plus the Fast thinking-leak and failed-turn rates. Latency gates per workload class on the geometric mean of per-case median ratios, with the tolerance 1 + max(0.20, 2σ) + abs ÷ scale, where σ is the repeat-to-repeat log noise (the larger of the class's own and its effort family's pooled figure); p95 gates only from 20 samples a side. The full rule text travels inside every baseline.
- **Why.** The first draft took the spread of all samples in a class as its noise; that measured how different the cases are, not run-to-run noise, and let every direct_fast case run 1.5× slower (QA Monte Carlo). A per-class noise estimate alone false-blocked unchanged builds 13 % / 33–42 % of the time, because two classes hold one case each; pooling over every class let Max's noise loosen the Fast tolerances (a 1.3× Fast slowdown caught in 34 % of trials). Pooling within the effort family keeps both errors small (`BENCHMARK_RESULTS.md`, tolerance method). One fresh account per run, because cross-chat recall and saved facts would feed one repeat's answers into the next.
- **Alternatives.** Fixed percentage tolerances (blind to the measured noise); a t-test on log latencies (assumes normal noise and many samples; we have three to five per case); the existing `scripts/aiq/gate.py` (fixed thresholds for the artifact suite, a different case set).
- **Reverse.** A new baseline frozen with a changed `baseline.py` replaces this one; old baselines refuse to verify under changed constants, so the two never mix silently.
