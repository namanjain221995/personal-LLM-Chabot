#!/usr/bin/env python3
"""Refuse an unattended deploy of something that is no longer the release.

THE FAILURE THIS GUARDS
-----------------------
A deploy job is a queued job. Between the moment `CI passed` went green and
the moment the rollout actually starts, the world can move:

  * MEASURED, on this repository: a gate that reported success at
    2026-09-19T07:33:29Z rolled out at 2026-09-21T14:11:26Z. Two days, six
    hours and thirty-eight minutes later, with nobody watching. (The runner
    had wedged after a DNS blip and the run sat queued until it came back.)
    Nothing about that rollout was wrong except its date, and nothing in the
    pipeline looked at the date.
  * Normal hand-offs on the same repository, for scale: 12 seconds, and
    10 minutes 46 seconds. The 90-minute default window is far above both and
    far below the two-day gap.

By then `main` has usually moved on, and deploying a two-day-old tip means
shipping a commit that is NOT what the branch says production should run,
without anyone choosing to.

WHAT IT DOES NOT GUARD
----------------------
Ordering. `concurrency: {group: deploy-dgx-spark, cancel-in-progress: false}`
already means a newer deploy replaces a PENDING one, so two rollouts cannot
land out of order and the older of two queued deploys is dropped by GitHub
before it starts. That is a different property from AGE: a single queued
deploy with nothing behind it is never superseded, waits as long as the runner
is away, and then runs. This guard is about stale TIME.

WHAT IT READS, AND WHY IT FAILS CLOSED
--------------------------------------
Two facts, both supplied by the caller through the environment (never
interpolated into a `run:` body - workflow_policy.py P6):

  1. the commit being deployed, and the current tip of the release branch on
     the remote. Not equal, or the tip could not be resolved -> refuse.
  2. when the gate finished, from the first of these that yields an instant:
       --gate-completed-at     an explicit value;
       --gate-jobs-file        a file holding this run's job list exactly as
                               `GET /repos/{repo}/actions/runs/{id}/jobs`
                               returns it, from which the `CI passed` job's
                               completed_at is taken. The caller curls it to a
                               file; this script only ever reads a file, so
                               the decision stays testable and no network call
                               lives inside the gate;
       --gate-self-reported-at the gate's OWN clock, published by the `CI
                               passed` job as a job output and read back with
                               `needs.ci-ok.outputs.completed_at`. No API call
                               and no network: job outputs cross a job
                               boundary by themselves, which is the same
                               mechanism the rollout already uses for
                               `manifest` and `record`. It is a few seconds
                               early - a step cannot time its own job's last
                               instant - and that direction is the safe one,
                               because a slightly EARLIER reading measures a
                               slightly LARGER age;
       --fallback-timestamp    github.event.head_commit.timestamp, used when
                               neither of those yielded an instant - the API
                               read got a 403 or a rate limit AND the job
                               output was missing. Always present on a push.
     A KNOWN WEAKNESS OF THE LAST ONE, recorded rather than papered over. The
     committer's clock is not the release's, and it can be arbitrarily older: a
     commit written on Monday and pushed on Wednesday carries Monday, so an age
     measured from it can refuse a release that is not stale at all. MEASURED
     here on 2026-09-27, by running this script: a release pushed ten seconds
     ago whose commit was written three hours earlier reports
     `age  180.2 min` / `refused because  age-stale`. An earlier
     version of this comment claimed it was "older than the gate's completion
     by roughly the length of CI", which is not true and made the fallback look
     safer than it is.
     A version after THAT claimed there was nothing better offline, because
     "every other way to read it is the same Actions API call that has just
     failed". That was also wrong, and it is the reason --gate-self-reported-at
     exists: the gate can hand its own completion time forward as a job output,
     with no API call to fail. What remains true is only the narrow part - the
     run's start time is NOT in the `github` context, it is not a property, and
     actionlint rejects `github.run_started_at`.
     The committer's clock is therefore now the THIRD source and not the
     second, and it is kept because measuring too large fails CLOSED: the
     refusal names the source it used, and the dispatch it recommends deploys
     the same commit. A false refusal that says which reading caused it beats a
     fail-open.
     The gate's completion time still wins whenever the API read worked.

None of the three parseable -> refuse. A guard that passes when it cannot see
is not a guard; the whole point of this file is that the 3 a.m. path stops
guessing.

A workflow_dispatch run is a human asking out loud, so the AGE check is
reported and not enforced there. The tip check still applies: deploying a
commit that main has moved past is a mistake whoever asked for it.

Every refusal prints what to do next, AND IT DIFFERS BY REASON. A single
`gh workflow run --ref <branch>` footer was right for the two AGE refusals and
wrong for the others: on "the branch has moved past this commit" it dispatches
the NEWER tip, so it deploys something else and does not put the refused commit
anywhere; on "the tip could not be resolved" it hits the same check and refuses
identically; on a malformed github.sha it arrives with the same malformed
value. Advice a pipeline cannot back up is the defect this file is about.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

#: A full commit id. The workflow always has one; anything else means the
#: caller wired the wrong value in, which is a refusal rather than a guess.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Default staleness ceiling, in minutes. See the module docstring for the
#: three measurements it sits between.
DEFAULT_WINDOW_MINUTES = 90

#: Clock skew between the runner and GitHub can make a timestamp look like it
#: is in the future. That is not staleness, so it is not a refusal; anything
#: beyond this is treated as zero age and said out loud.
FUTURE_TOLERANCE_SECONDS = 300


def parse_timestamp(raw: str | None) -> dt.datetime | None:
    """An ISO-8601 instant, or None. `Z` is accepted; naive input is UTC."""
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        value = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


def gate_time_from_jobs_file(path: str | None, job_name: str) -> str | None:
    """`completed_at` of the named job in an Actions jobs payload, or None.

    Every failure is None rather than an exception: a missing file, a 403 body
    that is not the expected shape, a job that has not finished. The caller
    then falls back, and if nothing is left the deploy is refused - which is
    the fail-closed direction, reached without this function having to decide
    anything.
    """
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        return None
    for job in jobs:
        if not isinstance(job, dict):
            continue
        if job.get("name") != job_name:
            continue
        completed = job.get("completed_at")
        if isinstance(completed, str) and completed.strip():
            return completed
    return None


def deliberate_command(workflow: str, branch: str) -> str:
    """The command an operator runs to deploy this anyway, on purpose.

    Only honest for an AGE refusal, where the refused commit IS the tip: a
    dispatch on the branch then rolls out that same commit with a human's name
    on it. `remedy()` is what decides whether this is the right advice.
    """
    return f"gh workflow run {workflow} --ref {branch} -f deploy=true"


def remedy(reason: str, *, sha: str, origin_tip: str, branch: str, workflow: str) -> list[str]:
    """What to actually do about THIS refusal, as printable lines.

    One footer for every refusal was wrong three times out of four. Each case
    below is the thing that genuinely moves the deploy forward, and where the
    dispatch is not it, the lines say so rather than sending an operator to run
    a command that cannot work.
    """
    lines = [""]
    if reason == "wiring":
        lines += [
            "Nothing on the box has been touched.",
            "",
            "This is a wiring fault in the workflow, not a state to override: the commit id",
            "reached this step empty or truncated. A dispatch arrives with the same value,",
            f"so `{deliberate_command(workflow, branch)}` would refuse the same way.",
            "Fix how github.sha is passed to this step.",
        ]
        return lines
    if reason == "tip-unresolved":
        lines += [
            "Nothing on the box has been touched.",
            "",
            f"The tip of origin/{branch} could not be read FROM THE RUNNER, so this is a",
            "network or credential fault on the runner, not a property of the release. A",
            "dispatch runs this same check and would refuse identically.",
            "Re-run this job once the runner can reach the remote again.",
        ]
        return lines
    if reason == "tip-moved":
        lines += [
            f"Nothing on the box has been touched, and nothing needs to be: {origin_tip[:12]}",
            "is the release now, and its own pipeline run deploys it. There is no action here.",
            "",
            f"`{deliberate_command(workflow, branch)}` is NOT the command for this:",
            f"it would deploy {origin_tip[:12]}, not {sha[:12]}.",
            f"To put {sha[:12]} specifically into production, it has to become the tip of",
            f"{branch} again (revert on top), or be deployed by hand on the box with a",
            "person present:",
            "",
            f"    scripts/deploy.sh --ref {sha}",
        ]
        return lines
    # The two AGE refusals. Here the refused commit IS the tip, so a dispatch
    # deploys exactly this commit and the only thing it adds is a human choice
    # about the age - which is the whole distinction being drawn.
    lines += [
        "Nothing on the box has been touched.",
        "To deploy this same commit deliberately, with the age on your name:",
        "",
        f"    {deliberate_command(workflow, branch)}",
        "",
        "A dispatch is a person taking responsibility for the age, which is the "
        "difference this guard is drawing.",
    ]
    return lines


def evaluate(
    *,
    sha: str,
    origin_tip: str,
    gate_completed_at: str | None,
    gate_self_reported_at: str | None,
    fallback_timestamp: str | None,
    now: dt.datetime,
    window_minutes: int,
    event_name: str,
    branch: str,
    workflow: str,
) -> tuple[bool, list[str], list[tuple[str, str]]]:
    """Return (ok, messages, facts). Pure: no clock, no network, no files."""
    facts: list[tuple[str, str]] = []
    messages: list[str] = []
    ok = True
    #: The FIRST thing that refused, because the advice differs by reason and
    #: the earliest refusal is the most fundamental one. A malformed commit id
    #: is not fixed by knowing the release is also stale.
    reason = ""

    sha = (sha or "").strip().lower()
    origin_tip = (origin_tip or "").strip().lower()
    facts.append(("commit being deployed", sha or "<missing>"))
    facts.append((f"tip of origin/{branch}", origin_tip or "<unresolved>"))

    if not SHA_RE.match(sha):
        ok = False
        reason = reason or "wiring"
        messages.append(
            f"REFUSE: the commit being deployed is not a full commit id ({sha or '<missing>'}). "
            "This value comes from github.sha; an empty or short one means the job is "
            "wired wrong, and a deploy is not the place to find that out by trying."
        )
    elif not SHA_RE.match(origin_tip):
        ok = False
        reason = reason or "tip-unresolved"
        messages.append(
            f"REFUSE: the tip of origin/{branch} could not be resolved "
            f"({origin_tip or '<unresolved>'}). Without it there is no way to tell whether "
            "this commit is still the release, so this fails closed rather than open."
        )
    elif sha != origin_tip:
        ok = False
        reason = reason or "tip-moved"
        messages.append(
            f"REFUSE: {sha[:12]} is no longer the tip of origin/{branch} "
            f"(that is {origin_tip[:12]} now). Deploying this would put production on a "
            "commit the branch has already moved past, without anyone choosing to."
        )
    else:
        messages.append(f"ok: {sha[:12]} is still the tip of origin/{branch}.")

    # PRECEDENCE, top to bottom. The gate's completion time from the Actions
    # API wins whenever it is there: it is the reading that measures exactly
    # what this guard is about.
    #
    # SECOND, and the reason the fallback below is no longer the only offline
    # source: the gate's own clock, published by the `CI passed` job as a job
    # output. Job outputs cross a job boundary with no API call, so this one
    # survives the 403 or the rate limit that takes the reading above away. It
    # is a few seconds early, because a step cannot time the instant its own
    # job ends, and early means the age measures slightly LARGER - the closed
    # direction.
    #
    # THIRD, and weak: the commit timestamp. It is the committer's clock, not
    # the release's, and it can be arbitrarily older - a commit written on
    # Monday and pushed on Wednesday carries Monday - so an age measured from it
    # can refuse a release that is not stale. That is a known false-refusal
    # path, not a rounding error, and it is kept only because measuring too
    # large fails closed while having no measurement at all would not. The
    # source is always named in the output so a refusal can be read for what it
    # is.
    candidates = (
        ("the gate's completion time", gate_completed_at),
        ("the gate's own clock, published as a job output", gate_self_reported_at),
        ("the deployed commit's timestamp (the gate's own time was unavailable)", fallback_timestamp),
    )
    stamp = None
    source = "<none>"
    for name, raw in candidates:
        parsed = parse_timestamp(raw)
        if parsed is not None:
            stamp, source = parsed, name
            break
    facts.append(("age measured from", source))

    if stamp is None:
        facts.append(("age", "<unknown>"))
        if event_name == "workflow_dispatch":
            # A dispatch has no head_commit, so when the Actions API read also
            # fails there is no timestamp left. Refusing here would refuse the
            # exact command the age refusals tell an operator to run, which
            # would leave the release path with no way out. A dispatch is a
            # person choosing; the age is theirs to own.
            messages.append(
                "note: the age of this release could not be determined (no gate time and, "
                "on a dispatch, no commit timestamp). This run was dispatched by hand, so "
                "a human has already chosen it. Not enforced."
            )
        else:
            ok = False
            reason = reason or "age-unknown"
            messages.append(
                "REFUSE: neither the gate's completion time nor the commit timestamp could be "
                "parsed, so the age of this release is unknown. An unknown age is a refusal: "
                "the whole reason this step exists is that a two-day-old queued deploy looks "
                "exactly like a fresh one from inside the job."
            )
    else:
        age_seconds = (now - stamp).total_seconds()
        if age_seconds < 0:
            if age_seconds < -FUTURE_TOLERANCE_SECONDS:
                messages.append(
                    f"note: {source} is {abs(age_seconds) / 60:.1f} minutes in the FUTURE; "
                    "the runner's clock and GitHub's disagree. Treating the age as zero."
                )
            age_seconds = 0.0
        age_minutes = age_seconds / 60
        facts.append(("age", f"{age_minutes:.1f} min"))
        facts.append(("window", f"{window_minutes} min"))
        if age_minutes > window_minutes:
            if event_name == "workflow_dispatch":
                messages.append(
                    f"note: this release is {age_minutes:.1f} minutes old, past the "
                    f"{window_minutes}-minute window - but this run was dispatched by hand, "
                    "so a human has already asked for it. Not enforced."
                )
            else:
                ok = False
                reason = reason or "age-stale"
                messages.append(
                    f"REFUSE: this release is {age_minutes:.1f} minutes old, measured from "
                    f"{source}, and the window is {window_minutes} minutes. A rollout this far "
                    "behind its gate is an unattended deploy of a stale tree, which has "
                    "happened here before: a gate green on 2026-09-19 rolled out on "
                    "2026-09-21, 2d 6h 37m later, because the runner had been away."
                )
        else:
            messages.append(
                f"ok: {age_minutes:.1f} minutes old, inside the {window_minutes}-minute window."
            )

    if not ok:
        facts.append(("refused because", reason or "unknown"))
        messages += remedy(
            reason, sha=sha, origin_tip=origin_tip, branch=branch, workflow=workflow
        )
    return ok, messages, facts


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value is not None and value.strip() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refuse a stale unattended deploy.")
    parser.add_argument("--sha", default=_env("DEPLOY_SHA"))
    parser.add_argument("--origin-tip", default=_env("ORIGIN_TIP"))
    parser.add_argument("--gate-completed-at", default=_env("GATE_COMPLETED_AT"))
    parser.add_argument("--gate-jobs-file", default=_env("GATE_JOBS_FILE"))
    parser.add_argument("--gate-job-name", default=os.environ.get("GATE_JOB_NAME") or "CI passed")
    parser.add_argument(
        "--gate-self-reported-at", default=_env("GATE_SELF_REPORTED_AT")
    )
    parser.add_argument("--fallback-timestamp", default=_env("FALLBACK_TIMESTAMP"))
    parser.add_argument("--event-name", default=os.environ.get("EVENT_NAME", "push"))
    parser.add_argument("--branch", default=os.environ.get("RELEASE_BRANCH", "main"))
    parser.add_argument("--workflow", default=os.environ.get("WORKFLOW_FILE", "pipeline.yml"))
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=int(os.environ.get("FRESHNESS_WINDOW_MINUTES") or DEFAULT_WINDOW_MINUTES),
    )
    parser.add_argument("--now", default=None, help="ISO-8601 instant; for tests")
    parser.add_argument("--summary", default=os.environ.get("GITHUB_STEP_SUMMARY"))
    args = parser.parse_args(argv)

    if args.window_minutes <= 0:
        print("FATAL: --window-minutes must be positive", file=sys.stderr)
        return 2

    now = parse_timestamp(args.now) if args.now else dt.datetime.now(dt.timezone.utc)
    if now is None:
        print(f"FATAL: --now is not an ISO-8601 instant: {args.now!r}", file=sys.stderr)
        return 2

    gate_completed_at = args.gate_completed_at
    if parse_timestamp(gate_completed_at) is None:
        gate_completed_at = gate_time_from_jobs_file(args.gate_jobs_file, args.gate_job_name)

    ok, messages, facts = evaluate(
        sha=args.sha or "",
        origin_tip=args.origin_tip or "",
        gate_completed_at=gate_completed_at,
        gate_self_reported_at=args.gate_self_reported_at,
        fallback_timestamp=args.fallback_timestamp,
        now=now,
        window_minutes=args.window_minutes,
        event_name=args.event_name,
        branch=args.branch,
        workflow=args.workflow,
    )

    width = max(len(k) for k, _ in facts)
    for key, value in facts:
        print(f"  {key.ljust(width)}  {value}")
    print()
    for line in messages:
        print(line)

    if args.summary:
        try:
            with open(args.summary, "a", encoding="utf-8") as fh:
                fh.write(f"### Freshness: {'ok' if ok else 'REFUSED'}\n\n")
                fh.write("| fact | value |\n| --- | --- |\n")
                for key, value in facts:
                    fh.write(f"| {key} | `{value}` |\n")
                if not ok:
                    fh.write("\n```\n")
                    fh.write("\n".join(m for m in messages if m))
                    fh.write("\n```\n")
        except OSError as exc:                      # noqa: BLE001 - never fail on reporting
            print(f"(could not write the step summary: {exc})", file=sys.stderr)

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
