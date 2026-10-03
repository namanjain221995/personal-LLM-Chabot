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
#      success;
#   6. GitHub's compare of origin/dev with the commit says "ahead" (or
#      "identical"), so the fast-forward holds in the origin's history too.
# Both branch tips are read from the origin itself (git ls-remote), never from
# the repository's remote-tracking refs, which other processes can repoint.
# It never touches main; the operator releases dev -> main.
#
# It trusts nothing from whoever runs it: bash -p ignores BASH_ENV, ENV and
# exported functions, every other variable is cleared below, and the tools,
# repository, branches, remote and log are fixed. It acts only when the
# repository's git directory is the expected main .git or a linked worktree's
# directory inside it, and then runs git on that verified common directory
# alone, pinned on every call. Every git call goes through one wrapper that
# reads no replace refs or grafts and turns off the commands a repository's
# configuration can make git run (hooks, fsmonitor, the alternate-refs
# command, a pager, push signing, automatic maintenance, askpass and
# credential helpers: only the operator's global helper answers, for the
# origin's host alone), and git may use only the origin's transport protocol.
# It refuses a repository whose configuration rewrites URLs or sets http.*,
# core.sshCommand, a remote named by a URL or an include. The autopilot runs
# the installed copy (~/.llm-autopilot/bin/merge_to_dev.sh), which it cannot
# edit; this file is the source. Tests run a copy with the configuration block
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
# git reads every object as stored, never through refs/replace, so the checks
# below see the commit that is pushed and nothing standing in for it
GIT_NO_REPLACE_OBJECTS=1
# nor through a grafts file in the repository (info/grafts would rewrite a
# commit's parents as git reads them, for the ancestry and tree checks alike)
GIT_GRAFT_FILE=/dev/null
# git never asks anyone for a credential: no terminal prompt, and no askpass
# program (the loop above cleared GIT_ASKPASS and SSH_ASKPASS with everything
# else; they are named here so that stays true; core.askPass is pinned in g())
GIT_TERMINAL_PROMPT=0
unset GIT_ASKPASS SSH_ASKPASS
export PATH HOME LANG GIT_NO_REPLACE_OBJECTS GIT_GRAFT_FILE GIT_TERMINAL_PROMPT
umask 022

# --- configuration (constants; tests replace this block in a temporary copy) ---
GIT=/usr/bin/git
GH=/usr/bin/gh
REPO=$HOME/work/llm-dev
EXPECTED_COMMON_DIR=$HOME/Documents/project/personal-LLM-Chabot/.git
ORIGIN_URL=https://github.com/namanjain221995/personal-LLM-Chabot.git
ORIGIN_PROTOCOL=https
LOG=$HOME/.llm-autopilot/logs/merge_to_dev.log
CI_APPROVALS=$HOME/.llm-autopilot/approved-ci-trees
# --- end of configuration ---
SOURCE_BRANCH=autopilot/dev
TARGET_BRANCH=dev
REPORT=docs/ai-platform-upgrade/FINAL_REPORT.md
REQUIRED_WORKFLOW=Pipeline
REQUIRED_CHECK="CI passed"
REQUIRED_APP=github-actions
# git may use no transport but the origin's: whatever URL a configuration
# rewrites the origin to, it cannot reach a command (ext::), a local path or ssh
GIT_ALLOW_PROTOCOL=$ORIGIN_PROTOCOL
export GIT_ALLOW_PROTOCOL

dry_run=0
want=""
for arg in "$@"; do
    case "$arg" in
        --dry-run) dry_run=1 ;;
        -h|--help) sed -n '2,/^# Usage:/p' "$0"; exit 0 ;;
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

# The only way git runs here: in $REPO (on the common directory verified in
# step 0 once that is known), without replace objects, grafts, a commit-graph
# file (whose stored parents the ancestry check would trust) or a pager, and with every
# setting through which a repository's configuration makes git run a command
# of its choosing pinned off: hooks (the hooks directory is /dev/null, so
# neither .git/hooks nor a configured core.hooksPath runs), the fsmonitor hook,
# the alternate-refs command, push-certificate signing (gpg.program),
# automatic maintenance (gc --auto and the commands it may run), the askpass
# program and every configured credential helper. A command-line -c wins over
# every config file, and git hands it on to the git processes it starts. An
# empty credential.helper empties the list of helpers git has collected from
# all config files (URL-scoped ones included); the one helper added back after
# it ($cred, set in step 0d) is the operator's, from the global git config.
git_dir=""
cred=()
g() {
    if [ -n "$git_dir" ]; then
        set -- --git-dir="$git_dir" "$@"
    fi
    "$GIT" --no-pager --no-replace-objects \
        -c core.hooksPath=/dev/null -c core.fsmonitor=false -c core.alternateRefsCommand=true \
        -c push.gpgSign=false -c maintenance.auto=false -c gc.auto=0 -c core.commitGraph=false \
        -c credential.useHttpPath=false -c core.askPass= -c credential.helper= ${cred[@]+"${cred[@]}"} \
        -C "$REPO" "$@"
}

slug=${ORIGIN_URL#https://github.com/}
slug=${slug%.git}

# 0. $REPO belongs to the expected repository: its git common directory (the
#    main checkout's .git, which every linked worktree shares) is the expected
#    one, and its git directory is that directory itself or a linked
#    worktree's directory directly under its worktrees/ (where `git worktree
#    add` makes them).
#    From here on git runs on the verified common directory itself, named by
#    --git-dir on every call and pinned in GIT_COMMON_DIR. The gate needs
#    nothing that lives in a worktree's own git directory (no HEAD, no index),
#    and git reads a git directory's commondir file again on every call (the
#    ref store follows that file even when GIT_COMMON_DIR is set), so neither
#    $REPO's .git file nor a commondir file can redirect git after this check.
want_common=$(cd "$EXPECTED_COMMON_DIR" 2>/dev/null && pwd -P) \
    || refuse "the expected git common directory $EXPECTED_COMMON_DIR does not exist"
found=$(g rev-parse --absolute-git-dir 2>/dev/null) && [ -n "$found" ] \
    || refuse "cannot read the git directory of $REPO (not a git repository?)"
git_dir=$(cd "$found" 2>/dev/null && pwd -P) || refuse "cannot resolve the git directory $found of $REPO"
common=$(g rev-parse --path-format=absolute --git-common-dir 2>/dev/null) && [ -n "$common" ] \
    || refuse "cannot read the git common directory of $REPO"
common=$(cd "$common" 2>/dev/null && pwd -P) || refuse "cannot resolve the git common directory $common of $REPO"
[ "$common" = "$want_common" ] \
    || refuse "the git common directory of $REPO is $common, not the expected $want_common"
not_ours="the git directory $git_dir of $REPO is not $want_common or a linked worktree's directory $want_common/worktrees/<name>"
case "$git_dir" in
    "$want_common") ;;
    "$want_common"/worktrees/*/*) refuse "$not_ours" ;;
    "$want_common"/worktrees/?*) ;;
    *) refuse "$not_ours" ;;
esac
# a main repository's git directory has no commondir file; with one, git would
# keep its refs wherever that file points
if [ -e "$want_common/commondir" ] || [ -L "$want_common/commondir" ]; then
    refuse "$want_common has a commondir file, so it is not the main repository's git directory"
fi
git_dir=$want_common
GIT_COMMON_DIR=$want_common
export GIT_COMMON_DIR

# 0b. origin is exactly the repository, for fetches and pushes alike, and git
#     resolves the URL the gate fetches from and lists as itself (insteadOf,
#     pushInsteadOf and pushurl are expanded by these reads)
for kind in fetch push; do
    if [ "$kind" = push ]; then url=$(g remote get-url --push origin) || refuse "cannot read the push URL of origin in $REPO"
    else url=$(g remote get-url origin) || refuse "cannot read the URL of origin in $REPO"; fi
    [ "$url" = "$ORIGIN_URL" ] || refuse "the $kind URL of origin in $REPO is not $ORIGIN_URL"
done
url=$(g ls-remote --get-url "$ORIGIN_URL") || refuse "cannot resolve $ORIGIN_URL in $REPO"
[ "$url" = "$ORIGIN_URL" ] || refuse "git in $REPO rewrites the URL $ORIGIN_URL to another one"

# 0c. the repository's own configuration (local, worktree and whatever they
#     include) sets nothing that reroutes git's connection to the origin: no
#     URL rewriting (url.*.insteadOf, url.*.pushInsteadOf), no http.* setting
#     (proxy, curloptResolve, TLS, extra headers and the rest, URL-scoped ones
#     included), no core.sshCommand, no remote section named by a URL (the
#     fetch and push below name the origin by URL, and remote.<url>.pushurl
#     would send the push elsewhere), and no include, so every key git reads
#     is listed here. Only key NAMES are read, never values, and the log shows
#     them without their subsection. The production checkout's repository
#     configuration has none of these keys, so there is no exception; its
#     credential.<url>.helper is not refused, because g() never lets any
#     configured helper run. The system and global configuration are the
#     operator's and are not checked. The check runs again right before the
#     push, so a key added while the checks below run is refused too.
refuse_rerouting_config() {
    local names bad
    names=$(g config --show-scope --name-only --list) || refuse "cannot list the git configuration names of $REPO"
    bad=$(printf '%s\n' "$names" | awk -F'\t' '
        $1 == "system" || $1 == "global" || $1 == "command" || NF < 2 { next }
        {
            k = tolower($2)
            if (k ~ /^(url|http|include|includeif)\./ || k == "core.sshcommand" || k ~ /^remote\..*\/.*\.[^.]*$/) {
                s = substr(k, 1, index(k, ".") - 1); v = k; sub(/^.*\./, "", v)
                shown = k
                if (length(s) + length(v) + 1 < length(k)) shown = s ".<...>." v
                print shown
            }
        }' | sort -u | paste -sd, -)
    [ -z "$bad" ] || refuse "the git configuration of $REPO sets $bad, which can reroute or rewrite the gate's fetch and push; the operator removes them (list them with: git -C $REPO config --show-scope --name-only --list)"
}
refuse_rerouting_config

# 0d. the credential for the push comes only from the helper the operator's
#     global git config names for the origin (gh's, where gh set up git),
#     scoped to the origin's scheme and host, so it is never offered for any
#     other server. Its value is passed on to git and never logged.
case "$ORIGIN_URL" in
    *://*)
        host_part=${ORIGIN_URL#*://}
        cred_scope=${ORIGIN_URL%%://*}://${host_part%%/*}
        helper=$(g config --global --get-urlmatch credential.helper "$ORIGIN_URL" 2>/dev/null) || helper=""
        if [ -n "$helper" ]; then
            cred=(-c "credential.$cred_scope.helper=$helper")
        fi
        ;;
esac

# The fetch only brings the objects. Which commits the two branches are on is
# read from the origin itself (ls-remote), never from the remote-tracking refs
# the fetch updates: those live in the shared common directory, where any
# process using the repository can repoint them while the checks below run.
# The objects of both commits must then be here.
g fetch --quiet --no-recurse-submodules --no-write-fetch-head "$ORIGIN_URL" \
    "+refs/heads/$TARGET_BRANCH:refs/remotes/origin/$TARGET_BRANCH" \
    "+refs/heads/$SOURCE_BRANCH:refs/remotes/origin/$SOURCE_BRANCH" \
    || refuse "git fetch of $TARGET_BRANCH and $SOURCE_BRANCH failed"
heads=$(g ls-remote "$ORIGIN_URL" "refs/heads/$TARGET_BRANCH" "refs/heads/$SOURCE_BRANCH") \
    || refuse "cannot list $TARGET_BRANCH and $SOURCE_BRANCH on origin"
dev_sha=$(printf '%s\n' "$heads" | awk -F'\t' -v r="refs/heads/$TARGET_BRANCH" '$2 == r { print $1 }')
tip=$(printf '%s\n' "$heads" | awk -F'\t' -v r="refs/heads/$SOURCE_BRANCH" '$2 == r { print $1 }')
[[ "$tip" =~ ^[0-9a-f]{40}$ ]] || refuse "origin/$SOURCE_BRANCH does not exist"
[[ "$dev_sha" =~ ^[0-9a-f]{40}$ ]] || refuse "origin/$TARGET_BRANCH does not exist"
g cat-file -e "$tip^{commit}" && g cat-file -e "$dev_sha^{commit}" \
    || refuse "the fetch did not bring origin's current $TARGET_BRANCH and $SOURCE_BRANCH (did one move during the fetch?); try again"
if [ "$want" = "origin/$SOURCE_BRANCH" ]; then
    want=$tip
fi
sha=$(g rev-parse --verify "${want:-$tip}^{commit}") \
    || refuse "cannot resolve ${want:-$tip}"

# 1. exactly the pushed tip of the source branch
[ "$sha" = "$tip" ] || refuse "$sha is not the pushed tip of origin/$SOURCE_BRANCH ($tip)"

# 2. fast-forward only
g merge-base --is-ancestor "$dev_sha" "$sha" \
    || refuse "origin/$TARGET_BRANCH ($dev_sha) is not an ancestor of $sha; merge the latest $TARGET_BRANCH into $SOURCE_BRANCH, re-run everything, push, and try again"

# 3. the final report is a non-empty regular file in the commit
entry=$(g ls-tree "$sha" -- "$REPORT" | cut -f1)
case "$entry" in
    "100644 blob "*|"100755 blob "*) ;;
    "") refuse "$REPORT does not exist in $sha" ;;
    *) refuse "$REPORT in $sha is not a regular file" ;;
esac
size=$(g cat-file -s "$sha:$REPORT") || refuse "cannot read $REPORT in $sha"
[ "$size" -gt 0 ] || refuse "$REPORT in $sha is empty"

# 4. the CI definition is the one dev already has, or one the operator approved
dev_ci=$(g rev-parse -q --verify "$dev_sha:.github" || echo none)
new_ci=$(g rev-parse -q --verify "$sha:.github" || echo none)
if [ "$dev_ci" != "$new_ci" ] && ! grep -qxF "$new_ci" "$CI_APPROVALS" 2>/dev/null; then
    refuse "the CI definition (.github/) in $sha differs from origin/$TARGET_BRANCH ($dev_sha), so its checks could have been graded by edited CI; the operator reviews git diff $dev_sha $sha -- .github/ and approves it by adding the tree $new_ci as a line of $CI_APPROVALS"
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

# 6. GitHub's own history agrees that the push is a fast-forward. Check 2 walks
#    parent commits in the shared local object store, where git does not
#    re-hash the parents it reads, and git push trusts the same walk.
compare=$("$GH" api "repos/$slug/compare/$dev_sha...$sha?per_page=1" --jq .status) \
    || refuse "could not compare origin/$TARGET_BRANCH ($dev_sha) with $sha on GitHub"
case "$compare" in
    ahead|identical) ;;
    *) refuse "GitHub reports $sha as '$compare' against origin/$TARGET_BRANCH ($dev_sha), not ahead of it" ;;
esac

log "OK: $sha passes every gate ($(printf '%s\n' "$runs" | wc -l) check runs)"
if [ "$dry_run" = 1 ]; then
    log "dry run: would push $sha to origin/$TARGET_BRANCH"
    exit 0
fi

refuse_rerouting_config
g push --no-follow-tags --no-verify --no-recurse-submodules "$ORIGIN_URL" "$sha:refs/heads/$TARGET_BRANCH" \
    || refuse "push to origin/$TARGET_BRANCH failed (not a fast-forward any more, or no credential: the gate uses only the credential helper the operator's global git config names for the origin)"
now=$(g ls-remote "$ORIGIN_URL" "refs/heads/$TARGET_BRANCH" | awk -F'\t' -v r="refs/heads/$TARGET_BRANCH" '$2 == r { print $1 }') \
    || refuse "the push ran, but origin/$TARGET_BRANCH could not be read back to confirm it is $sha; check it by hand"
[ "$now" = "$sha" ] || refuse "origin/$TARGET_BRANCH is $now after the push, expected $sha"
log "MERGED: origin/$TARGET_BRANCH is now $sha"
