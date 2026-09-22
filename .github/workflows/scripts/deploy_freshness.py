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
       --fallback-timestamp    github.event.head_commit.timestamp, which is
                               always present on a push. Used when the API read
                               did not work - a 403, a rate limit, no network.
     Which one was used is PRINTED, because they measure slightly different
     things: the commit timestamp is older than the gate's completion by
     roughly the length of CI, so an age measured from it is conservative.

Neither parseable -> refuse. A guard that passes when it cannot see is not a
guard; the whole point of this file is that the 3 a.m. path stops guessing.

A workflow_dispatch run is a human asking out loud, so the AGE check is
reported and not enforced there. The tip check still applies: deploying a
commit that main has moved past is a mistake whoever asked for it.

Every refusal prints the exact command to deploy deliberately anyway.
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
    """The command an operator runs to deploy this anyway, on purpose."""
    return f"gh workflow run {workflow} --ref {branch} -f deploy=true"


def evaluate(
    *,
    sha: str,
    origin_tip: str,
    gate_completed_at: str | None,
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

    sha = (sha or "").strip().lower()
    origin_tip = (origin_tip or "").strip().lower()
    facts.append(("commit being deployed", sha or "<missing>"))
    facts.append((f"tip of origin/{branch}", origin_tip or "<unresolved>"))

    if not SHA_RE.match(sha):
        ok = False
        messages.append(
            f"REFUSE: the commit being deployed is not a full commit id ({sha or '<missing>'}). "
            "This value comes from github.sha; an empty or short one means the job is "
            "wired wrong, and a deploy is not the place to find that out by trying."
        )
    elif not SHA_RE.match(origin_tip):
        ok = False
        messages.append(
            f"REFUSE: the tip of origin/{branch} could not be resolved "
            f"({origin_tip or '<unresolved>'}). Without it there is no way to tell whether "
            "this commit is still the release, so this fails closed rather than open."
        )
    elif sha != origin_tip:
        ok = False
        messages.append(
            f"REFUSE: {sha[:12]} is no longer the tip of origin/{branch} "
            f"(that is {origin_tip[:12]} now). Deploying this would put production on a "
            "commit the branch has already moved past, without anyone choosing to."
        )
    else:
        messages.append(f"ok: {sha[:12]} is still the tip of origin/{branch}.")

    gate = parse_timestamp(gate_completed_at)
    fallback = parse_timestamp(fallback_timestamp)
    stamp = gate or fallback
    source = (
        "the gate's completion time"
        if gate is not None
        else ("the deployed commit's timestamp (the gate's own time was unavailable)" if fallback else "<none>")
    )
    facts.append(("age measured from", source))

    if stamp is None:
        facts.append(("age", "<unknown>"))
        if event_name == "workflow_dispatch":
            # A dispatch has no head_commit, so when the Actions API read also
            # fails there is no timestamp left. Refusing here would refuse the
            # exact command every other refusal in this file tells an operator
            # to run, which would leave the release path with no way out. A
            # dispatch is a person choosing; the age is theirs to own.
            messages.append(
                "note: the age of this release could not be determined (no gate time and, "
                "on a dispatch, no commit timestamp). This run was dispatched by hand, so "
                "a human has already chosen it. Not enforced."
            )
        else:
            ok = False
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
        messages.append("")
        messages.append("Nothing on the box has been touched. To deploy this deliberately:")
        messages.append("")
        messages.append(f"    {deliberate_command(workflow, branch)}")
        messages.append("")
        messages.append(
            "A dispatch is a person taking responsibility for the age, which is the "
            "difference this guard is drawing."
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
