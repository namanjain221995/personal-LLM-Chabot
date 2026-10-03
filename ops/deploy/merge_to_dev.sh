#!/bin/bash -p
# merge_to_dev.sh: the only way the autopilot moves `dev` (operator decision
# 2026-10-03, docs/ai-platform-upgrade/MASTER_PROMPT.md section 0).
#
# Fast-forwards origin/dev to one exact commit and refuses unless:
#   1. the commit is the pushed tip of origin/autopilot/dev;
#   2. origin/dev is an ancestor of it (a fast-forward: no force, no rebase);
#   3. docs/ai-platform-upgrade/FINAL_REPORT.md in that commit is a non-empty
#      regular file;
#   4. .github/ in that commit is the same tree as on origin/dev, or the
#      operator approved that tree in ~/.llm-autopilot/approved-ci-trees (a
#      commit must not be graded by CI definitions it changed itself);
#   5. the Pipeline workflow ran on that exact commit for the pull request
#      autopilot/dev -> dev and succeeded, its "CI passed" check (GitHub
#      Actions) succeeded, every other check run on the commit completed as
#      success, skipped or neutral, and the combined commit status (if any) is
#      success.
# It never touches main; the operator releases dev -> main.
#
# It trusts nothing from whoever runs it: bash -p ignores BASH_ENV, ENV and
# exported functions, every other variable is cleared below, and the tools,
# repository, branches, remote and log are fixed. The autopilot runs the
# installed copy (~/.llm-autopilot/bin/merge_to_dev.sh), which it cannot edit;
# this file is the source. Tests run a copy with the configuration block
# replaced (ops/autopilot/tests/test_merge_to_dev.py).
#
# Usage: merge_to_dev.sh [--dry-run] [<40-hex commit> | origin/autopilot/dev]

set -euo pipefail

# --- clean environment (nothing from the caller reaches git, gh or the log) ---
for _var in $(compgen -e); do
    unset "$_var" 2>/dev/null || true
done
PATH=/usr/bin:/bin
HOME=$(getent passwd "$(id -u)" | cut -d: -f6)
LANG=C.UTF-8
export PATH HOME LANG
umask 022

# --- configuration (constants; tests replace this block in a temporary copy) ---
GIT=/usr/bin/git
GH=/usr/bin/gh
REPO=$HOME/work/llm-dev
ORIGIN_URL=https://github.com/namanjain221995/personal-LLM-Chabot.git
LOG=$HOME/.llm-autopilot/logs/merge_to_dev.log
CI_APPROVALS=$HOME/.llm-autopilot/approved-ci-trees
# --- end of configuration ---
SOURCE_BRANCH=autopilot/dev
TARGET_BRANCH=dev
REPORT=docs/ai-platform-upgrade/FINAL_REPORT.md
REQUIRED_WORKFLOW=Pipeline
REQUIRED_CHECK="CI passed"
REQUIRED_APP=github-actions

dry_run=0
want=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        -h|--help) sed -n '2,27p' "$0"; exit 0 ;;
        *)
            if [ -z "$want" ] && { [[ "$arg" =~ ^[0-9a-f]{40}$ ]] || [ "$arg" = "origin/$SOURCE_BRANCH" ]; }; then
                want=$arg
            else
                echo "merge_to_dev: refused: arguments are --dry-run and at most one 40-hex commit or origin/$SOURCE_BRANCH" >&2
                exit 2
            fi
            ;;
    esac
done

log() {
    # one line per event; control characters and shell metacharacters never reach the log
    local msg
    msg=$(printf '%s' "$*" | LC_ALL=C tr '\000-\037\177' ' ' | LC_ALL=C tr ';|&$`' '?????')
    mkdir -p "$(dirname "$LOG")"
    printf '%s %s\n' "$(date -u +%FT%TZ)" "$msg" | tee -a "$LOG" >&2
}

refuse() {
    log "REFUSED: $*"
    exit 1
}

slug=${ORIGIN_URL#https://github.com/}
slug=${slug%.git}

# 0. origin is the repository, for fetches and pushes alike (insteadOf and pushurl are expanded here)
for kind in fetch push; do
    if [ "$kind" = push ]; then url=$("$GIT" -C "$REPO" remote get-url --push origin) || refuse "cannot read the push URL of origin in $REPO"
    else url=$("$GIT" -C "$REPO" remote get-url origin) || refuse "cannot read the URL of origin in $REPO"; fi
    [ "${url%.git}" = "${ORIGIN_URL%.git}" ] || refuse "the $kind URL of origin in $REPO is not $ORIGIN_URL"
done

"$GIT" -C "$REPO" fetch --quiet "$ORIGIN_URL" \
    "+refs/heads/$TARGET_BRANCH:refs/remotes/origin/$TARGET_BRANCH" \
    "+refs/heads/$SOURCE_BRANCH:refs/remotes/origin/$SOURCE_BRANCH" \
    || refuse "git fetch of $TARGET_BRANCH and $SOURCE_BRANCH failed"
tip=$("$GIT" -C "$REPO" rev-parse --verify "refs/remotes/origin/$SOURCE_BRANCH^{commit}") \
    || refuse "origin/$SOURCE_BRANCH does not exist"
sha=$("$GIT" -C "$REPO" rev-parse --verify "${want:-$tip}^{commit}") \
    || refuse "cannot resolve ${want:-$tip}"

# 1. exactly the pushed tip of the source branch
[ "$sha" = "$tip" ] || refuse "$sha is not the pushed tip of origin/$SOURCE_BRANCH ($tip)"

# 2. fast-forward only
"$GIT" -C "$REPO" merge-base --is-ancestor "refs/remotes/origin/$TARGET_BRANCH" "$sha" \
    || refuse "origin/$TARGET_BRANCH is not an ancestor of $sha; merge the latest $TARGET_BRANCH into $SOURCE_BRANCH, re-run everything, push, and try again"

# 3. the final report is a non-empty regular file in the commit
entry=$("$GIT" -C "$REPO" ls-tree "$sha" -- "$REPORT" | cut -f1)
case "$entry" in
    "100644 blob "*|"100755 blob "*) ;;
    "") refuse "$REPORT does not exist in $sha" ;;
    *) refuse "$REPORT in $sha is not a regular file" ;;
esac
size=$("$GIT" -C "$REPO" cat-file -s "$sha:$REPORT") || refuse "cannot read $REPORT in $sha"
[ "$size" -gt 0 ] || refuse "$REPORT in $sha is empty"

# 4. the CI definition is the one dev already has, or one the operator approved
dev_ci=$("$GIT" -C "$REPO" rev-parse -q --verify "refs/remotes/origin/$TARGET_BRANCH:.github" || echo none)
new_ci=$("$GIT" -C "$REPO" rev-parse -q --verify "$sha:.github" || echo none)
if [ "$dev_ci" != "$new_ci" ] && ! grep -qxF "$new_ci" "$CI_APPROVALS" 2>/dev/null; then
    refuse "the CI definition (.github/) in $sha differs from origin/$TARGET_BRANCH, so its checks could have been graded by edited CI; the operator reviews git diff origin/$TARGET_BRANCH $sha -- .github/ and approves it by adding the tree $new_ci as a line of $CI_APPROVALS"
fi

# 5. CI on this exact commit: the Pipeline run of the pull request source -> target, by GitHub Actions
wf_runs=$("$GH" api --paginate "repos/$slug/actions/runs?head_sha=$sha&event=pull_request&per_page=100" \
        --jq '.workflow_runs[] | [.name, .status, (.conclusion // "none"), (.check_suite_id | tostring), ([.pull_requests[]? | (.base.ref + ">" + .head.ref)] | join(","))] | @tsv') \
    || refuse "could not read the workflow runs of $sha"
pipeline=$(printf '%s\n' "$wf_runs" | awk -F'\t' -v wf="$REQUIRED_WORKFLOW" -v pr="$TARGET_BRANCH>$SOURCE_BRANCH" \
    '$1 == wf { n = split($5, a, ","); for (i = 1; i <= n; i++) if (a[i] == pr) { print; break } }')
[ -n "$pipeline" ] || refuse "no $REQUIRED_WORKFLOW run of the pull request $SOURCE_BRANCH -> $TARGET_BRANCH on $sha; open or update the draft PR so CI runs on it"
bad=$(printf '%s\n' "$pipeline" | awk -F'\t' '$2 != "completed" || $3 != "success" { print $2 "/" $3 }' | paste -sd, -)
[ -z "$bad" ] || refuse "the $REQUIRED_WORKFLOW run of $SOURCE_BRANCH -> $TARGET_BRANCH on $sha has not succeeded: $bad"
suites=$(printf '%s\n' "$pipeline" | cut -f4 | paste -sd, -)

runs=$("$GH" api --paginate "repos/$slug/commits/$sha/check-runs?per_page=100" \
        --jq '.check_runs[] | [.name, .status, (.conclusion // "none"), (.app.slug // "none"), (.check_suite.id | tostring)] | @tsv') \
    || refuse "could not read the check runs of $sha"
[ -n "$runs" ] || refuse "no check runs on $sha; open or update the draft PR $SOURCE_BRANCH -> $TARGET_BRANCH so CI runs on it"
bad=$(printf '%s\n' "$runs" | awk -F'\t' '$2 != "completed" || ($3 != "success" && $3 != "skipped" && $3 != "neutral") { print $1 " " $2 " " $3 }' | paste -sd, -)
[ -z "$bad" ] || refuse "checks on $sha not passed: $bad"
printf '%s\n' "$runs" | awk -F'\t' -v want="$REQUIRED_CHECK" -v app="$REQUIRED_APP" -v suites="$suites" \
    'BEGIN { n = split(suites, s, ","); for (i = 1; i <= n; i++) ok[s[i]] = 1 }
     $1 == want && $3 == "success" && $4 == app && ($5 in ok) { found = 1 } END { exit !found }' \
    || refuse "required check '$REQUIRED_CHECK' from $REQUIRED_APP has not succeeded in the $REQUIRED_WORKFLOW run of $SOURCE_BRANCH -> $TARGET_BRANCH on $sha"
status=$("$GH" api "repos/$slug/commits/$sha/status" --jq '[.state, (.total_count|tostring)] | @tsv') \
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

"$GIT" -C "$REPO" push "$ORIGIN_URL" "$sha:refs/heads/$TARGET_BRANCH" \
    || refuse "push to origin/$TARGET_BRANCH failed (not a fast-forward any more?)"
now=$("$GIT" -C "$REPO" ls-remote "$ORIGIN_URL" "refs/heads/$TARGET_BRANCH" | cut -f1)
[ "$now" = "$sha" ] || refuse "origin/$TARGET_BRANCH is $now after the push, expected $sha"
log "MERGED: origin/$TARGET_BRANCH is now $sha"
