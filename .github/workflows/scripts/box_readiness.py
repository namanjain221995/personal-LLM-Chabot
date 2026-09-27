#!/usr/bin/env python3
"""Can this machine be deployed to? Asked BEFORE the release path touches it.

WHY THIS EXISTS
---------------
The pipeline used to be a line: nine hosted test jobs, an aggregate gate, and
only then a self-hosted `deploy` job whose FIRST act was a preflight against
the box. Every box-state refusal therefore cost a full CI run before it was
even asked. Re-measured from the GitHub Actions API on 2026-09-27: run
35002778440 spent 55.4 minutes of wall clock before the `deploy` job started
and then failed in 233s (3m53s) at "The box is serving the commit we asked
for", and runs 35100814873, 35427466262 and 36304046169 each failed in `verify`
at "The engine API cannot be reached from outside the cluster" - a fact that
was already true before each run started. The four most recent main-run
failures were all box state, not code.

So the graph becomes two pipes that converge at `deploy`: test evidence on
hosted runners, and box evidence gathered at the same time on the box. This is
the box half. It runs while the 18-28 minute orchestrator shards are still
running, and it refuses in seconds rather than in an hour.

WHAT IT IS NOT
--------------
It is NOT a guarantee held at deploy time. It is a reading taken minutes
earlier, and the box is shared: the deploy root is a working tree several
sessions write to, and a lock can be taken in the gap. scripts/deploy.sh's own
preflight still runs and stays authoritative, and nobody may remove a check
from it on the strength of this job.

It is also not a code review. See the `box-readiness` job comment in
pipeline.yml for what gates it and what that does and does not buy.

EVERYTHING IT DOES IS READ-ONLY
-------------------------------
Seven probes. None of them starts, stops, recreates, prunes or changes the state
of the stack. Three things are worth saying out loud rather than letting the
phrase "seven reads" cover them:

  * the deploy flock is asked whether it is free by the same non-blocking
    `flock -n` scripts/lib/deploy-common.sh uses, which holds it for the
    lifetime of a `bash -c` that does nothing else. The earlier claim that it is
    "never taken" was withdrawn as untrue, and box_probes.py says exactly what
    happens instead;
  * every git call against the shared deploy root carries
    `--no-optional-locks`, because `git status` otherwise takes
    `.git/index.lock` there and rewrites `.git/index` (measured on git 2.43.0 on
    2026-09-27; box_probes.probe_deploy_root has the measurement);
  * the migrations probe reaches `dr_live_schema_version`, whose fallback runs
    `docker exec <production postgres> psql -tAc 'SELECT COALESCE(MAX(version),
    0) FROM schema_migrations'`. A SELECT, but a command run inside the
    production database container.

Every probe fails closed: a probe that cannot be performed is a refusal, and a
probe that raises or times out is a refusal too. Every refusal prints the exact
command a human runs to clear it, in full.

Nothing that is not on an allowlist reaches stdout: this repository is public,
its run summaries are world-readable, and the deploy root's environment file
holds the pointer to the real secrets file. The rule lives in box_probes.py so
that the second consumer of these probes inherits it by construction.

Usage:
    box_readiness.py --deploy-root PATH --ref SHA [--repo-root PATH]
                     [--default-branch main] [--summary FILE]

Exit: 0 every probe passed, 1 at least one refused, 2 the arguments are wrong.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
from typing import Sequence, TextIO

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import box_probes  # noqa: E402

EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2

#: Column widths. Fixed, so the table is readable in a log that has no CSS.
_PROBE_W, _VERDICT_W = 18, 19


class _Writer:
    """Every line of the REPORT passes through here, and therefore through
    `sanitize`.

    Not every line this script prints. Three sites bypass it, all of them on
    stderr, all of them fixed text chosen in this file, and all of them at
    points where the report they would otherwise have joined does not exist:

      * `_write_summary`'s note that the summary could not be written;
      * `main`'s `--deploy-root` usage error;
      * `main`'s last-resort refusal, which renders the exception's CLASS NAME
        through `box_probes.render_fact` and never its message.

    None of the three interpolates anything that came from the box, a file or an
    argument. The earlier wording here was "there is no second way out", which
    was simply not true; the property that IS true, and the one the allowlist
    exists for, is that nothing derived from outside this file reaches stdout or
    stderr without passing through an allowlist first.
    """

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        for raw in text.split("\n"):
            line = box_probes.sanitize(raw).rstrip()
            self.lines.append(line)
            print(line, file=self._out)


def run(
    env: box_probes.Environment,
    *,
    out: TextIO | None = None,
    summary_path: str | None = None,
) -> int:
    """Run every probe, report, and return the exit code."""
    say = _Writer(out if out is not None else sys.stdout)
    started = env.clock()

    say("Box readiness - a READ-ONLY pre-deploy reading of the production box.")
    say("It does not replace scripts/deploy.sh's preflight, which still runs and stays authoritative.")
    say("")
    say(f"  deploy root  {box_probes.render_fact('deploy_root', str(env.deploy_root))}")
    say(f"  workspace    {box_probes.render_fact('repo_root', str(env.repo_root))}")
    say(f"  commit       {box_probes.render_fact('ref', env.ref)}")
    say("")

    results = box_probes.run_all(env)

    say(f"{'probe'.ljust(_PROBE_W)}{'verdict'.ljust(_VERDICT_W)}facts")
    say(f"{'-' * (_PROBE_W - 1):<{_PROBE_W}}{'-' * (_VERDICT_W - 1):<{_VERDICT_W}}{'-' * 40}")
    for result in results:
        say(
            f"{result.probe.ljust(_PROBE_W)}"
            f"{result.safe_verdict.ljust(_VERDICT_W)}"
            f"{box_probes.render_facts(result.facts)}"
        )

    for result in results:
        if not result.report_lines:
            continue
        say("")
        say(f"{result.probe}: the report engine_bind.py printed for itself")
        for line in result.report_lines:
            say(f"  {line}")

    refusals = [r for r in results if not r.ok]
    elapsed = max(0.0, env.clock() - started)

    if refusals:
        say("")
        say(f"REFUSED by {len(refusals)} of {len(results)} probes. Nothing was changed.")
        for result in refusals:
            say("")
            say(f"  {result.probe}: {result.safe_verdict}")
            note = box_probes.note_for(result)
            if note:
                for line in _wrap(note, 92):
                    say(f"    {line}")
            say("    To clear it, run:")
            for command in box_probes.remedy_for(env, result):
                say(f"      {command}")

    say("")
    verdict = "NOT READY" if refusals else "READY"
    passed = len(results) - len(refusals)
    say(
        f"VERDICT: {verdict}  ({passed} of {len(results)} probes passed, "
        f"{box_probes.render_fact('elapsed_s', elapsed)})"
    )

    _write_summary(summary_path, say.lines, verdict, passed, len(results))
    return EXIT_REFUSED if refusals else EXIT_OK


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


def _write_summary(path: str | None, lines: Sequence[str], verdict: str, passed: int, total: int) -> None:
    """The step summary is the SAME text, already sanitized.

    Not a second rendering: a second rendering is a second place for a value to
    escape the allowlist, and the only reason this file has an allowlist is
    that the summary is world-readable.
    """
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"### Box readiness: {verdict} ({passed}/{total})\n\n")
            handle.write("```\n")
            for line in lines:
                handle.write(line + "\n")
            handle.write("```\n")
    except OSError:
        # A summary that cannot be written must not turn a READY box into a
        # failed job, and must not be silent either.
        print("box_readiness: the step summary could not be written", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A read-only pre-deploy reading of the production box.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--deploy-root", required=True, help="the production checkout")
    parser.add_argument("--ref", required=True, help="the commit that would be deployed")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="the checkout this script ships in (default: derived from its own path)",
    )
    parser.add_argument("--default-branch", default="main")
    parser.add_argument(
        "--controller-url",
        default=os.environ.get("ENGINE_CONTROLLER_URL", box_probes.DEFAULT_CONTROLLER_URL),
        help="the engine controller (default: the loopback address the cluster scripts use)",
    )
    parser.add_argument(
        "--summary",
        default=os.environ.get("GITHUB_STEP_SUMMARY"),
        help="append the report here as well (default: $GITHUB_STEP_SUMMARY)",
    )
    args = parser.parse_args(argv)

    deploy_root = pathlib.Path(args.deploy_root).expanduser()
    if not deploy_root.is_dir():
        print(
            "box_readiness: --deploy-root is not a directory. It must be the "
            "production checkout, not the runner workspace.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    repo_root = (
        pathlib.Path(args.repo_root).expanduser()
        if args.repo_root
        else pathlib.Path(__file__).resolve().parents[3]
    )

    env = box_probes.Environment(
        deploy_root=deploy_root,
        repo_root=repo_root,
        ref=args.ref,
        clock=time.time,
        default_branch=args.default_branch,
        controller_url=args.controller_url,
    )
    # THIS JOB ALWAYS REACHES A VERDICT. `deploy` has this job in its `needs:`,
    # so a run that ends in a traceback instead of a verdict is a deploy that
    # never happens AND an operator with no reason for it. box_probes.run_probe
    # already turns anything a PROBE does into a refusal; this is the rest of
    # the script -- the rendering, the wrapping, the summary write -- and it is
    # the difference between "NOT READY, here is why" and a stack trace that
    # also happens to print filesystem paths into a world-readable log.
    #
    # It refuses rather than passes: a reading that could not be completed is
    # not evidence that the box is fine. Only the exception's CLASS NAME is
    # printed, through the same allowlist every fact goes through, because an
    # exception's message is how a value out of a file reaches a public log.
    try:
        return run(env, summary_path=args.summary)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - a refusal, whatever went wrong
        kind = box_probes.render_fact("exception_type", type(exc).__name__)
        print(
            f"\nVERDICT: NOT READY  (this reading could not be completed: {kind})",
            file=sys.stderr,
        )
        print(
            "box_readiness: the probes are guarded individually, so this is a "
            "failure in the reporting path rather than in a probe. Re-run the "
            "command in the step above by hand; it changes nothing on the box.",
            file=sys.stderr,
        )
        return EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
