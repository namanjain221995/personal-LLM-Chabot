# Rollout and rollback

The programme's release target is `dev` (operator decision 2). The operator releases `dev → main`; production rollout and rollback stay with the operator's existing mechanisms.

## The programme's release into `dev`

1. Definition of Done (§28) met; all tests, evaluations and the independent review pass; `FINAL_REPORT.md` written.
2. Merge the latest `origin/dev` into `autopilot/dev`, resolve conflicts, re-run everything, push `autopilot/dev`.
3. Wait for the draft PR's CI run on that exact commit.
4. Run `~/.llm-autopilot/bin/merge_to_dev.sh --dry-run`, then without `--dry-run`. It fast-forwards `origin/dev` only when every check on the commit passed and `FINAL_REPORT.md` exists.
5. Record the result in `DEPLOY_LOG.md`.

Rollback of `dev`: a revert commit on `autopilot/dev`, through the same gate (never a force push).

Thresholds for health, canaries per mode, error rate and p95 are defined here before the final release (Phase G), for the operator's `dev → main` release.

## Production mechanisms (operator-owned, for reference)

- Rollout: a merge to `main` runs the Pipeline; its `deploy` job runs `scripts/deploy.sh --ref $GITHUB_SHA` (rolling, keeps the main model running) and `verify` checks health and a real completion.
- Automatic rollback: `deploy.sh` rolls back when its recorded reversibility verdict allows; it restores images, never the database.
- Manual rollback: `scripts/deploy-rollback.sh --list`, `--to <release> --dry-run`, `--yes`.
