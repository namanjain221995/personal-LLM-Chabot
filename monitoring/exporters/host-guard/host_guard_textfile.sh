#!/bin/sh
# Writes the host packet filter's state as a node-exporter textfile
# (2026-09-13, monitoring/developer-api/README.md "host packet filter").
#
# WHY A TEXTFILE. The filter is one nftables table (`inet techsara_guard`,
# scripts/host-guard.sh). It lives in kernel memory; since 2026-09-22 a
# systemd unit re-applies it at every boot (`host-guard.sh install-boot`), and
# this writer is how you find out that it did not. Listing a table
# needs CAP_NET_ADMIN ("Operation not permitted (you must be root)" as the
# normal user), and no network probe from inside the cluster can tell a
# filtered port from an open one, because every in-cluster path is one the
# filter deliberately accepts. So the only honest signal is a local check
# that node-exporter publishes. It reads; it never adds, flushes or changes a
# rule.
#
# TWO SOURCES, named in the `source` label:
#   nft         run as root: `nft list table` is the truth.
#   state_file  run as anyone: scripts/host-guard.sh writes
#               /run/techsara-host-guard/state on apply and deletes it on
#               remove; /run is tmpfs, so a reboot clears it. It proves
#               "applied since boot and not removed by the script", NOT that
#               nobody flushed the ruleset by hand. Prefer root.
#
# THE DEGRADED FALLBACK COUNTS AS ABSENT. When the boot unit cannot install
# the guard it loads a fallback under the SAME table name, which closes the
# office LAN and the tailnet and nothing else. Both sources detect it (the
# rule comment under nft, GUARD_MODE under state_file) and report
# table_present=0, because the guard is not in place.
#
# Output (atomic rename into the textfile directory):
#   techsara_host_guard_table_present{source}   1 loaded, 0 not loaded
#   techsara_host_guard_check_ok{source}        1 the check could run, 0 it could not
#                                               (then table_present is 0 and means nothing)
#   techsara_host_guard_check_timestamp_seconds when this ran
#
# Install (an operator decision; nothing here is installed):
#   1. node-exporter: add --collector.textfile.directory=/host<DIR> (the
#      host's / is already mounted at /host read-only), recreate node-exporter.
#   2. run this every minute with TEXTFILE_DIR=<DIR>: a root systemd timer
#      (source nft) or the user's own timer (source state_file).
set -u

DIR="${TEXTFILE_DIR:-/var/lib/node_exporter/textfile_collector}"
TABLE="${HOST_GUARD_TABLE:-techsara_guard}"
NFT="${HOST_GUARD_NFT:-nft}"
STATE="${HOST_GUARD_STATE_FILE:-/run/techsara-host-guard/state}"
MODE="${HOST_GUARD_SOURCE:-auto}"   # auto | nft | state_file
NOW="$(date +%s)"

present=0
ok=0
if [ "$MODE" = auto ]; then
  if [ "$(id -u)" = 0 ] && command -v "$NFT" >/dev/null 2>&1; then MODE=nft; else MODE=state_file; fi
fi

case "$MODE" in
  nft)
    if out="$("$NFT" list table inet "$TABLE" 2>/dev/null)"; then
      ok=1
      case "$out" in
        *"DEGRADED fallback"*) present=0 ;;
        *) present=1 ;;
      esac
    else
      # A missing table is ENOENT ("No such file or directory"); anything
      # else (no permission, no nf_tables) is a check that did not run.
      err="$("$NFT" list table inet "$TABLE" 2>&1 >/dev/null)" || true
      case "$err" in
        *"No such file or directory"*) present=0; ok=1 ;;
        *) present=0; ok=0 ;;
      esac
    fi
    ;;
  state_file)
    if [ -e "$STATE" ]; then
      if grep -q '^GUARD_APPLIED_AT=' "$STATE" 2>/dev/null; then
        ok=1
        if grep -q '^GUARD_MODE=degraded$' "$STATE" 2>/dev/null; then present=0; else present=1; fi
      else
        ok=0
      fi
    else
      # `-e` is false both for a file that is not there and for one this user
      # cannot see. "Absent" is only honest when the lookup could have found
      # it: the state directory exists and is searchable, or it does not exist
      # and its parent is searchable (a reboot clears /run/techsara-host-guard
      # itself). A directory we cannot search is "cannot say", check_ok=0 —
      # never a missing table (fixed 2026-09-13: the old `|| [ -r /run ]` made
      # a chmod 000 state directory report the table MISSING).
      sdir="$(dirname "$STATE")"
      if [ -d "$sdir" ]; then
        if [ -x "$sdir" ]; then present=0; ok=1; else ok=0; fi
      elif [ -x "$(dirname "$sdir")" ]; then
        present=0; ok=1
      else
        ok=0
      fi
    fi
    ;;
  *)
    echo "HOST_GUARD_SOURCE must be auto, nft or state_file" >&2
    exit 2
    ;;
esac

mkdir -p "$DIR" || exit 1
tmp="$DIR/.techsara_host_guard.prom.$$"
{
  echo "# HELP techsara_host_guard_table_present 1 when nftables table inet $TABLE is loaded (per source), 0 when it is not."
  echo "# TYPE techsara_host_guard_table_present gauge"
  echo "techsara_host_guard_table_present{source=\"$MODE\"} $present"
  echo "# HELP techsara_host_guard_check_ok 1 when the check could run; 0 means table_present carries no information."
  echo "# TYPE techsara_host_guard_check_ok gauge"
  echo "techsara_host_guard_check_ok{source=\"$MODE\"} $ok"
  echo "# HELP techsara_host_guard_check_timestamp_seconds Unix time of the last check."
  echo "# TYPE techsara_host_guard_check_timestamp_seconds gauge"
  echo "techsara_host_guard_check_timestamp_seconds $NOW"
} >"$tmp" && mv -f "$tmp" "$DIR/techsara_host_guard.prom"
