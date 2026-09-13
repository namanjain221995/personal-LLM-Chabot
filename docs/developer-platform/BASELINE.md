# Baseline and current-state map — developer platform programme

Recorded 2026-09-12 before any change, from the isolated worktree
`/home/techsphere/Documents/project/personal-LLM-Chabot-devapi` on branch
`feat/developer-platform-security`, based on `origin/dev` at **4e28fcc**.

Nothing in this document is estimated. Every line is a command's output or a
file's content, and where a number could not be measured it says so.

## Git

| item | value |
|---|---|
| feature branch | `feat/developer-platform-security` |
| base commit | 4e28fcc `ci(security): baseline three reviewed entropy hits …` |
| `origin/dev` at start | 4e28fcc |
| `origin/main` at start | b047cc1 (merge of PR #59) |
| open pull requests | none |
| worktrees | shared checkout (dev), `-ha` (feat/vllm-availability-2026-09-12, another session), this one |
| uncommitted changes in the shared checkout | none |

Concurrent work: the vLLM-availability session finished and its work is on
`main`; it holds the `-ha` worktree and has nothing further to land. This
programme owns different files (see OWNERSHIP.md) and integrates from
`origin/dev` before landing.

## CI baseline

The pipeline was green on the base commit before this programme started —
these are the runs, not a claim:

| run | commit | result |
|---|---|---|
| Pipeline (pull_request) 34700906406 | 4e28fcc | success — nine jobs green, `CI passed` success |
| Pipeline (push, main) 34701778691 | b047cc1 | success — deploy and verify production both success |

So any red job after this programme's changes is a regression introduced here,
not pre-existing.

## Test baseline

Recorded on this branch before changes (same tree as 4e28fcc):

| suite | command | result |
|---|---|---|
| backend | `pytest -q` in `orchestrator/` with a private `TEST_DATABASE_URL` | 3942 passed, 5 skipped (49 min) on the artifact tree; CI's own run on 4e28fcc green |
| frontend | `npx tsc --noEmit`, `npm run lint`, `npx vitest run` | see `evidence/baseline-frontend.txt` |

The five backend skips are opt-in only (live vLLM ×3, live embedder, ffmpeg) —
no suite is silently skipped.

## Runtime snapshot

Filled from the read-only audit (`AUDIT.md`): container identities, published
ports and their bind interfaces, the edge mapping, and the database schema
version at the time of the audit.
