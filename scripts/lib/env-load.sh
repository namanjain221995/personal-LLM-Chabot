#!/usr/bin/env bash
# The supported way for a shell script to read this repository's env files.
# Source this file; do not execute it.
#
#     . "$(dirname "$0")/lib/env-load.sh"
#     load_env_file .env .runtime/secrets.env .runtime/generated.env
#
# WHY THIS EXISTS
#
# `set -a && . ./.env` is not a dotenv parser. It is the shell interpreting the
# file, and this repository's env files contain values it cannot survive:
#
#   NEXT_PUBLIC_APP_NAME=TechSara AI                 -> "AI: command not found"
#   TECHSARA_CLUSTER_REASON=... (auto-detected)      -> syntax error, file abandoned
#   CLUSTER_SPECULATIVE_CONFIG={"method":"mtp",...}  -> loads with the quotes SILENTLY stripped
#
# The first prints an error to a stderr nobody reads and continues, so the
# environment ends up partially built; under `set -e` it aborts and every later
# assignment is missing. The third does not even produce an error -- the value
# is just quietly corrupted. An engineer on this repository lost a day to
# exactly this, concluding that five failing tests were application defects
# when they were a half-loaded environment.
#
# load_env_file routes the file through the launcher's canonical parser, which
# is asserted to agree with Docker Compose, and re-quotes every value with
# shlex.quote before it reaches the shell. Later files win, matching Compose's
# precedence for a repeated --env-file.

# shellcheck shell=bash

_ENV_LOAD_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

load_env_file() {
    if [ "$#" -eq 0 ]; then
        printf 'load_env_file: needs at least one env file\n' >&2
        return 2
    fi

    local file
    for file in "$@"; do
        if [ ! -f "$file" ]; then
            printf 'load_env_file: no such env file: %s\n' "$file" >&2
            return 1
        fi
    done

    local rendered
    # Capture first: a failure inside the parser must not be eval'd, and must
    # not leave the caller with a half-built environment.
    if ! rendered="$(python3 "$_ENV_LOAD_LIB_DIR/env_export.py" "$@")"; then
        printf 'load_env_file: refusing to load %s\n' "$*" >&2
        return 1
    fi

    # Safe: every value was passed through shlex.quote, so each line is a
    # single-quoted literal with no expansion of any kind.
    eval "$rendered"
}

# check_env_files FILE...: refuse to continue if a file carries a line a shell
# would mis-parse. Prints keys and reasons, never values. Useful in a script
# that also hands these files to something other than load_env_file.
check_env_files() {
    "$_ENV_LOAD_LIB_DIR/../check-env-files.sh" "$@"
}
