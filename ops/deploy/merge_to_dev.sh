#!/usr/bin/env bash
# merge_to_dev.sh: the only way the autopilot moves `dev` (operator decision
# 2026-10-03, docs/ai-platform-upgrade/MASTER_PROMPT.md section 0).
#
# Fast-forwards origin/dev to one exact commit and refuses unless:
#   1. the commit is the pushed tip of origin/autopilot/dev;
#   2. origin/dev is an ancestor of it (a fast-forward: no force, no rebase);
#   3. docs/ai-platform-upgrade/FINAL_REPORT.md exists in that commit;
#   4. every GitHub check run on that exact commit completed as success,
#      skipped or neutral, "CI passed" among them succeeded, and the combined
#      commit status (if any) is success.
# It never touches main; the operator releases dev -> main.
#
# The autopilot runs the installed copy (~/.llm-autopilot/bin/merge_to_dev.sh),
# which its guard trusts and which it cannot edit; this file is the source.
#
# Usage: merge_to_dev.sh [--dry-run] [<commit>]   (default: origin/autopilot/dev)

set -euo pipefail

REPO=${MERGE_TO_DEV_REPO:-$HOME/work/llm-dev}
SOURCE_BRANCH=${MERGE_TO_DEV_SOURCE:-autopilot/dev}
TARGET_BRANCH=dev
REPORT=docs/ai-platform-upgrade/FINAL_REPORT.md
REQUIRED_CHECK="CI passed"
LOG=${MERGE_TO_DEV_LOG:-$HOME/.llm-autopilot/logs/merge_to_dev.log}

dry_run=0
want=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        -*) echo "merge_to_dev: unknown option $arg" >&2; exit 2 ;;
        *) want=$arg ;;
    esac
done

log() {
    mkdir -p "$(dirname "$LOG")"
    printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG" >&2
}

refuse() {
    log "REFUSED: $*"
    exit 1
}

git -C "$REPO" fetch --quiet origin "$TARGET_BRANCH" "$SOURCE_BRANCH" \
    || refuse "git fetch origin $TARGET_BRANCH $SOURCE_BRANCH failed"
tip=$(git -C "$REPO" rev-parse --verify "refs/remotes/origin/$SOURCE_BRANCH^{commit}") \
    || refuse "origin/$SOURCE_BRANCH does not exist"
sha=$(git -C "$REPO" rev-parse --verify "${want:-$tip}^{commit}") \
    || refuse "cannot resolve ${want:-$tip}"

# 1. exactly the pushed tip of the source branch
[ "$sha" = "$tip" ] || refuse "$sha is not the pushed tip of origin/$SOURCE_BRANCH ($tip)"

# 2. fast-forward only
git -C "$REPO" merge-base --is-ancestor "refs/remotes/origin/$TARGET_BRANCH" "$sha" \
    || refuse "origin/$TARGET_BRANCH is not an ancestor of $sha; merge the latest $TARGET_BRANCH into $SOURCE_BRANCH, re-run everything, push, and try again"

# 3. the final report is part of the commit
git -C "$REPO" cat-file -e "$sha:$REPORT" 2>/dev/null || refuse "$REPORT does not exist in $sha"

# 4. every check on this exact commit passed
slug=$(git -C "$REPO" remote get-url origin | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##')
runs=$(gh api --paginate "repos/$slug/commits/$sha/check-runs?per_page=100" \
        --jq '.check_runs[] | [.name, .status, (.conclusion // "none")] | @tsv') \
    || refuse "could not read the check runs of $sha"
[ -n "$runs" ] || refuse "no check runs on $sha; open or update the draft PR $SOURCE_BRANCH -> $TARGET_BRANCH so CI runs on it"
bad=$(printf '%s\n' "$runs" | awk -F'\t' '$2 != "completed" || ($3 != "success" && $3 != "skipped" && $3 != "neutral")')
[ -z "$bad" ] || refuse "checks on $sha not passed: $(printf '%s' "$bad" | tr '\t\n' ' ;')"
printf '%s\n' "$runs" | awk -F'\t' -v want="$REQUIRED_CHECK" '$1 == want && $3 == "success" {found=1} END {exit !found}' \
    || refuse "required check '$REQUIRED_CHECK' has not succeeded on $sha"
status=$(gh api "repos/$slug/commits/$sha/status" --jq '[.state, (.total_count|tostring)] | @tsv') \
    || refuse "could not read the combined status of $sha"
state=${status%%$'\t'*}
count=${status##*$'\t'}
if [ "$count" != "0" ] && [ "$state" != "success" ]; then
    refuse "combined commit status of $sha is $state"
fi

log "OK: $sha passes every gate ($(printf '%s\n' "$runs" | wc -l) check runs)"
if [ "$dry_run" = 1 ]; then
    log "dry run: would push $sha to origin/$TARGET_BRANCH"
    exit 0
fi

git -C "$REPO" push origin "$sha:refs/heads/$TARGET_BRANCH" || refuse "push to origin/$TARGET_BRANCH failed (not a fast-forward any more?)"
now=$(git -C "$REPO" ls-remote origin "refs/heads/$TARGET_BRANCH" | cut -f1)
[ "$now" = "$sha" ] || refuse "origin/$TARGET_BRANCH is $now after the push, expected $sha"
log "MERGED: origin/$TARGET_BRANCH is now $sha"
