#!/usr/bin/env python3
"""Drive the `db-rehearsal` job: pick a baseline, prove the rollback direction, rule.

WHAT THE JOB PROVES AND WHY IT IS NOT THE `schema` JOB
-----------------------------------------------------
The `schema` job already migrates a fresh database and an old database forward
and compares the two shapes. It does that with the migrations from the CHECKOUT,
in the runner's own Python. That is the right test for "are these migrations
consistent with each other".

It is the wrong test for "is this schema change safe to release", for two
reasons this script exists to cover:

  1. THE CODE THAT SHIPS IS AN IMAGE, NOT A CHECKOUT. The deployed orchestrator
     runs `app/db.py` inside orchestrator-cpu/cuda, against that image's pinned
     dependency set. A migration that needs a psycopg feature the image's
     version does not have is green in the `schema` job and red on the box.
  2. AN IMAGE ROLLBACK DOES NOT ROLL THE DATABASE BACK. There are no down
     migrations and `init_schema` skips versions already in
     `schema_migrations`, so rolling the orchestrator back leaves the PREVIOUS
     release's code in front of THIS release's schema. Nothing in CI has ever
     run that combination. `scripts/deploy-rollback.sh` refuses to cross a
     schema boundary precisely because nobody knows whether it is survivable —
     and "nobody knows" is a thing a pipeline can fix.

So the job builds two real orchestrator-cpu images — this commit's and the
previous released commit's — and:

    FRESH          empty database -> this commit's head version
    UPGRADE        the previous release's schema, seeded through the previous
                   release's own API, migrated forward by this commit
    RESTORE        pg_dump/pg_restore of the upgraded database, then proved
                   deployable
    REVERSIBILITY  the database staged at THIS commit's version, with the
                   PREVIOUS release's image booted in front of it, answering
                   /health and a route smoke

The first three are `scripts/deploy-db-rehearsal.sh`, unchanged in substance —
the same assertions the box runs before a deploy. The fourth is here.

WHAT ONLY A REAL ACTIONS RUN CAN PROVE
--------------------------------------
Nothing in this file needs GitHub, and the two arms and the reversibility proof
have been run locally against a throwaway PostgreSQL 18 of the same major
production runs. Three things have NOT been measured and are not claimed:

  * the job's wall-clock on a hosted runner, which is dominated by two
    orchestrator-cpu builds;
  * that `postgres@sha256:...` — the digest the `orchestrator` and `schema`
    jobs already use — resolves to major 18 on the runner's amd64. The
    `--expect-major` declaration turns that into an assertion, so a run says so
    either way, but only a run says it;
  * that a `fetch-depth: 0` checkout makes `github.event.before` resolvable.
    `baseline` refuses rather than guesses when it does not, which is the
    failure a first run would show.

Subcommands
-----------
    db_rehearsal.py baseline       resolve the previous released commit
    db_rehearsal.py reversibility  boot the old image over the new schema
    db_rehearsal.py verdict        turn the arms' outcomes into a labelled FACT
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.request

#: The arms the verdict requires. An arm that is MISSING from the results file
#: fails the gate, for the same reason ci_gate.py fails a required job that is
#: absent from `needs`: a step that was deleted or renamed must not be able to
#: turn a gate green by no longer reporting.
REQUIRED_ARMS = ("fresh-and-upgrade", "reversibility")

#: The zero sha `push` events carry in `before` for a branch's FIRST push.
#: There is no previous commit in that case, and pretending one exists is how a
#: baseline resolver ends up rehearsing against an empty tree.
NULL_SHA = "0" * 40

# --------------------------------------------------------------------- helpers


def _run(argv: list[str], *, cwd: str | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - argv is built here, never from a shell string
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _git(repo: str, *args: str) -> tuple[int, str]:
    proc = _run(["git", "-C", repo, *args])
    return proc.returncode, (proc.stdout or "").strip()


def _summary(text: str) -> None:
    """Append to the job summary when there is one; always print."""
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text.rstrip("\n") + "\n")


def _emit_outputs(pairs: dict[str, str]) -> None:
    """Write step outputs, so the workflow reads values rather than re-deriving them."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in pairs.items():
            fh.write(f"{key}={value}\n")


# -------------------------------------------------------------------- baseline


class BaselineError(RuntimeError):
    """No previous released commit could be established. Never guess one."""


def resolve_baseline(
    *,
    repo: str,
    head: str,
    event_name: str,
    event: dict,
    default_branch: str,
    declared: str = "",
    commit_exists=None,
) -> tuple[str, str]:
    """Return (sha, how). Raise BaselineError rather than guess.

    `how` is carried into the summary on purpose. "the previous release" is not
    a thing a hosted runner can look up — this repository has no release tags
    and records deploys in `.runtime/releases` on the box, which CI cannot read
    — so the honest report names the rule that produced the sha.
    """
    if commit_exists is None:

        def commit_exists(sha: str) -> bool:
            return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")[0] == 0

    def accept(sha: str, how: str) -> tuple[str, str]:
        sha = (sha or "").strip()
        if not sha or sha == NULL_SHA:
            raise BaselineError(
                f"{how} gives no usable commit ({sha or '<empty>'}). This is the first push "
                "to this ref, so there is no previous release to rehearse an upgrade from. "
                "Pass --previous <sha> if a baseline is known."
            )
        if sha == head:
            raise BaselineError(
                f"{how} resolves to HEAD ({sha[:12]}). An upgrade from this commit to itself "
                "proves nothing; refusing to report it as a rehearsal."
            )
        if not commit_exists(sha):
            raise BaselineError(
                f"{how} resolves to {sha[:12]}, which is not in this checkout. "
                "actions/checkout fetches DEPTH 1 by default, and neither `github.event.before` "
                "nor a merge-base exists in a one-commit clone. Set `fetch-depth: 0` on the "
                "checkout step."
            )
        return sha, how

    if declared:
        return accept(declared, "--previous (declared by the caller)")

    if event_name == "pull_request":
        base = ((event.get("pull_request") or {}).get("base") or {}).get("sha") or ""
        return accept(base, "the pull request's base commit")

    if event_name == "push":
        ref = str(event.get("ref") or "")
        if ref == f"refs/heads/{default_branch}":
            candidate = str(event.get("before") or "")
            how = f"the commit {default_branch} was at before this push"
        else:
            # A push to any other branch: the baseline is where this branch left
            # the default branch, which is the released schema this work will
            # land on top of.
            rc, out = _git(repo, "merge-base", head, f"origin/{default_branch}")
            if rc != 0 or not out:
                raise BaselineError(
                    f"no merge-base between {head[:12]} and origin/{default_branch}. "
                    "A shallow checkout has no merge-base: set `fetch-depth: 0`."
                )
            candidate, how = out, f"the merge-base with origin/{default_branch}"

        # THE FIRST-PARENT FALLBACK, and why it is not a fudge.
        #
        # Both rules above can legitimately land on HEAD itself: `before` does
        # on a re-pushed tip, and the merge-base does whenever the branch is AT
        # or behind the default branch - which is every branch on the day it is
        # cut, and every branch that has just merged the default branch in with
        # nothing of its own on top.
        #
        # Failing those runs would make the job red for a reason that has
        # nothing to do with the schema, and a check that is red for bookkeeping
        # is a check people learn to ignore. So fall back to HEAD's FIRST
        # PARENT, which on the default branch's line is the same commit
        # `before` would have named: for a merge commit it is the branch tip
        # before the merge, and for an ordinary commit it is the one it was
        # written on top of. It is labelled differently in the summary, so a
        # reader always knows which rule produced the baseline.
        if candidate and candidate != NULL_SHA and candidate != head:
            return accept(candidate, how)
        rc, first_parent = _git(repo, "rev-parse", f"{head}^1")
        if rc != 0 or not first_parent:
            raise BaselineError(
                f"{how} gives no commit other than HEAD, and HEAD ({head[:12]}) has no first "
                "parent either - it is a root commit, so there is no earlier schema to rehearse "
                "against. Pass --previous <sha> if a baseline is known."
            )
        return accept(first_parent, f"HEAD's first parent ({how} named HEAD itself)")

    raise BaselineError(
        f"event `{event_name or '<none>'}` carries no baseline this script knows how to read. "
        "Pass --previous <sha>."
    )


def cmd_baseline(args: argparse.Namespace) -> int:
    event: dict = {}
    if args.event:
        try:
            event = json.loads(pathlib.Path(args.event).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"FATAL: cannot read the event payload {args.event}: {exc}", file=sys.stderr)
            return 2
        if not isinstance(event, dict):
            print("FATAL: the event payload is not a JSON object", file=sys.stderr)
            return 2

    head = args.head or _git(args.repo, "rev-parse", "HEAD")[1]
    if not head:
        print("FATAL: cannot read HEAD", file=sys.stderr)
        return 2

    try:
        sha, how = resolve_baseline(
            repo=args.repo,
            head=head,
            event_name=args.event_name,
            event=event,
            default_branch=args.default_branch,
            declared=args.previous or os.environ.get("DB_REHEARSAL_PREVIOUS_SHA", ""),
        )
    except BaselineError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        _summary(
            "### Database rehearsal: NOT RUN\n\n"
            f"**NOT PROVED** - no previous released commit could be established: {exc}\n"
        )
        return 1

    print(f"head     {head}")
    print(f"baseline {sha}")
    print(f"resolved by: {how}")
    _emit_outputs({"head": head, "previous": sha, "how": how})
    return 0


# --------------------------------------------------------------- reversibility


def _http(url: str, *, method: str = "GET", body: bytes | None = None, timeout: int = 20) -> tuple[int, str]:
    """(status, text). A transport failure is status 0, so callers can retry."""
    req = urllib.request.Request(url, data=body, method=method)  # noqa: S310 - 127.0.0.1 only
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - "not up yet" and "refused" are the same to a poller
        return 0, str(exc)


class Findings:
    """PASS/FAIL lines, and whether anything failed."""

    def __init__(self) -> None:
        self.rows: list[tuple[bool, str]] = []

    def ok(self, text: str) -> None:
        self.rows.append((True, text))
        print(f"  PASS {text}")

    def bad(self, text: str) -> None:
        self.rows.append((False, text))
        print(f"  FAIL {text}")
        # An annotation as well as a line: the container log this script dumps
        # on failure is dozens of lines long, and a finding that scrolls past
        # is a finding nobody reads.
        print("::error title=db-rehearsal reversibility::" + text.replace("\n", " ")[:900])

    @property
    def failed(self) -> int:
        return sum(1 for good, _ in self.rows if not good)


def _docker_logs(container: str) -> str:
    proc = _run(["docker", "logs", "--tail", "40", container], timeout=60)
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def cmd_reversibility(args: argparse.Namespace) -> int:
    """Boot the PREVIOUS release's image against THIS commit's schema.

    The database is expected to be staged already, at `--expect-schema`. This
    reads that number back out of the OLD image's own /health rather than
    asking PostgreSQL directly, which makes the assertion the one that matters:
    the previous release, looking at a schema it has never seen, reports it
    honestly and does not try to change it.
    """
    f = Findings()
    container = args.container
    base = f"http://127.0.0.1:{args.port}"

    _run(["docker", "rm", "-f", container], timeout=120)
    start = _run(
        [
            "docker", "run", "-d", "--name", container, "--network", "host",
            "-e", f"APP_DATABASE_URL={args.dsn}",
            # A port nothing listens on: the model servers are not part of this
            # proof and /health must report them down rather than hang. The
            # rest of the app's configuration is deliberately left at the
            # image's own defaults - a rollback candidate that needs an
            # environment variable this commit introduced is a finding.
            "-e", f"OPENAI_BASE_URL=http://127.0.0.1:{args.dead_port}/v1",
            "--entrypoint", "uvicorn", args.old_image,
            "app.main:app", "--host", "127.0.0.1", "--port", str(args.port),
        ],
        timeout=300,
    )
    if start.returncode != 0:
        f.bad(f"the previous release's image would not start: {(start.stderr or start.stdout).strip()}")
        return _finish_reversibility(args, f, logs="")

    try:
        status, payload = 0, ""
        deadline = time.monotonic() + args.boot_timeout
        while time.monotonic() < deadline:
            status, payload = _http(f"{base}/health")
            if status == 200:
                break
            time.sleep(1)

        if status != 200:
            f.bad(
                f"the previous release's image did not answer GET /health within "
                f"{args.boot_timeout}s (last status {status})"
            )
            return _finish_reversibility(args, f, logs=_docker_logs(container))

        f.ok("the previous release's image booted against this commit's schema and answered GET /health 200")

        try:
            health = json.loads(payload)
        except json.JSONDecodeError as exc:
            f.bad(f"/health did not return JSON: {exc}")
            health = {}

        app_db = ((health.get("checks") or {}).get("app_db")) or {}
        if app_db.get("status") == "ok":
            f.ok("/health reports app_db status=ok - the previous release can read the newer schema")
        else:
            f.bad(f"/health reports app_db {app_db!r}, not status=ok")

        seen = app_db.get("schema_version")
        if seen == args.expect_schema:
            f.ok(
                f"the previous release reports schema_version={seen}, this commit's head version - "
                "it read the newer schema and left it alone"
            )
        else:
            f.bad(
                f"/health reports schema_version={seen!r}, expected {args.expect_schema}. "
                "Either the database was not staged at this commit's version, or the previous "
                "release CHANGED it - and a rollback that migrates is not a rollback."
            )

        # Routing and response serialisation only. /metrics swallows a database
        # error and still serves, so a 200 here is NOT evidence about the
        # database and is not reported as such.
        status, _ = _http(f"{base}/metrics")
        if status == 200:
            f.ok("GET /metrics answers 200 (routing and exposition only; not a database claim)")
        else:
            f.bad(f"GET /metrics returned {status}")

        # The real route-level database read. An unknown account is a SELECT
        # against `users` that finds nothing, so 401 proves the query ran on the
        # newer table. A 500 is what a users table the old code cannot read
        # looks like, and 200 would mean this fabricated account exists.
        status, body = _http(
            f"{base}/auth/login",
            method="POST",
            body=json.dumps(
                {"email": "db-rehearsal-no-such-account@invalid.example", "password": "not-a-password"}
            ).encode("utf-8"),
        )
        if status == 401:
            f.ok("POST /auth/login for an unknown account returns 401 - the old code queried `users` on the newer schema")
        else:
            f.bad(f"POST /auth/login returned {status}, expected 401. Body: {body[:300]}")

        # Reading is half a rollback. This is the other half, through the
        # previous release's own db.py - the same API scripts/deploy-db-rehearsal.sh
        # seeds with, so a column this commit made NOT NULL without a default
        # fails here instead of on the box.
        write = _run(
            [
                "docker", "run", "--rm", "--network", "host",
                "-e", f"APP_DATABASE_URL={args.dsn}",
                "--entrypoint", "python", args.old_image, "-c",
                write_smoke_source(f"{os.getpid()}-{int(time.time())}"),
            ],
            timeout=600,
        )
        if write.returncode == 0 and "ROLLBACK-WRITE-OK" in (write.stdout or ""):
            f.ok("the previous release WROTE a user, a conversation and a message through its own db.py and read them back")
        else:
            tail = ((write.stdout or "") + (write.stderr or "")).strip()[-1200:]
            f.bad(f"the previous release could not write against the newer schema:\n{tail}")

        return _finish_reversibility(args, f, logs="" if f.failed == 0 else _docker_logs(container))
    finally:
        _run(["docker", "rm", "-f", container], timeout=120)


#: Run inside the OLD image. Writes through the application's own API and reads
#: the rows back, then deletes nothing: the database is a throwaway and leaving
#: the rows makes a failure inspectable.
#:
#: The names carry a per-run suffix. `users.username` and the conversation id are
#: UNIQUE, so a fixed name turns a second run against the same database into a
#: UniqueViolation that reads exactly like "the previous release cannot write" -
#: which is the one thing this smoke must not be able to say wrongly.
_WRITE_SMOKE = """
import app.db as db

tag = {tag!r}
uid = db.create_user("rollback-smoke-user-" + tag, "not-a-real-hash")
conv = "rollback-smoke-conv-" + tag
db.create_conversation(uid, conv, "Rollback smoke")
db.add_message(uid, conv, "user", "written by the previous release")
rows = db.list_messages(conv)
assert rows, "the message the previous release just wrote could not be read back"
assert any("written by the previous release" in str(r.get("content", "")) for r in rows), rows
print("ROLLBACK-WRITE-OK", uid, len(rows))
"""


def write_smoke_source(tag: str) -> str:
    """The write smoke, bound to a tag that makes its rows unique to this run."""
    return _WRITE_SMOKE.format(tag=tag)


def _finish_reversibility(args: argparse.Namespace, f: Findings, *, logs: str) -> int:
    passed = sum(1 for good, _ in f.rows if good)
    print(f"\nreversibility: {passed} passed, {f.failed} failed")
    if logs:
        print("\n--- the previous release's container log (last 40 lines) ---")
        print(logs)
    if args.json:
        pathlib.Path(args.json).write_text(
            json.dumps(
                {
                    "arm": "reversibility",
                    "outcome": "pass" if f.failed == 0 else "fail",
                    "old_image": args.old_image,
                    "expect_schema": args.expect_schema,
                    "checks": [{"ok": good, "text": text} for good, text in f.rows],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0 if f.failed == 0 else 1


# --------------------------------------------------------------------- verdict


def _incomplete_reason(arm: str, rec: dict) -> str:
    """Why a `pass` record does not actually prove its arm, or "" when it does.

    `scripts/deploy-db-rehearsal.sh` SKIPS its upgrade phase when no baseline
    image was supplied, and on a laptop with no history that skip is the honest
    thing to do -- it refuses to fake an old schema by deleting rows from
    `schema_migrations`, which would test a state that never existed. The script
    then exits 0 because its fresh-install assertions really did pass.

    In a gate that zero is a lie by omission. `--require-upgrade` makes the
    script itself refuse, and this is the second lock on the same door: the
    record says which image it upgraded FROM and which schema version it read
    OUT of that image, so a record that upgraded from nothing is caught here
    even if the flag is ever dropped from the workflow.

    There are two ways for the phase not to have run, and the record shows them
    apart: no baseline image at all (`from_image` empty), and a baseline image
    whose migration table could not be read (`from_image` set, `from_version`
    empty -- the script reports that as a FAILED assertion, so it should never
    reach here as a `pass`, but this lock does not depend on that).
    """
    if arm != "fresh-and-upgrade":
        return ""
    if not str(rec.get("from_image") or ""):
        return (
            "the record names no baseline image, so the UPGRADE phase was skipped. "
            "Fresh-install assertions alone do not prove an upgrade is safe."
        )
    if not str(rec.get("from_version") or ""):
        return (
            "the record names a baseline image whose migration table could not be read, "
            "so there was no old schema to migrate forward and the UPGRADE phase was "
            "skipped. Fresh-install assertions alone do not prove an upgrade is safe."
        )
    return ""


def build_verdict(records: list[dict], *, required: tuple[str, ...] = REQUIRED_ARMS) -> tuple[bool, list[str]]:
    """Return (ok, markdown lines). Every line says FACT or NOT PROVED.

    The distinction is the whole point. A reader of a green summary has to be
    able to tell "we ran this and it held" from "we did not run this", and a
    table of ticks cannot: an arm that never ran has no tick and no row.
    """
    by_arm = {str(r.get("arm") or ""): r for r in records if isinstance(r, dict)}
    ok = True
    lines: list[str] = []

    for arm in required:
        rec = by_arm.get(arm)
        if rec is None:
            ok = False
            lines.append(
                f"- **NOT PROVED** `{arm}`: this arm reported nothing. A step that no longer "
                "reports must not be able to pass the gate by being silent."
            )
            continue
        outcome = str(rec.get("outcome") or "")
        detail = str(rec.get("detail") or "")
        if outcome == "pass":
            incomplete = _incomplete_reason(arm, rec)
            if incomplete:
                ok = False
                lines.append(f"- **NOT PROVED** `{arm}`: {incomplete}")
            else:
                lines.append(f"- **FACT** `{arm}`: PASSED{(' - ' + detail) if detail else ''}")
        elif outcome == "fail":
            ok = False
            lines.append(f"- **FACT** `{arm}`: FAILED{(' - ' + detail) if detail else ''}")
        else:
            ok = False
            lines.append(
                f"- **NOT PROVED** `{arm}`: reported outcome `{outcome or '<none>'}`"
                f"{(' - ' + detail) if detail else ''}"
            )

    for arm in sorted(set(by_arm) - set(required)):
        rec = by_arm[arm]
        outcome = str(rec.get("outcome") or "")
        label = "FACT" if outcome in ("pass", "fail") else "NOT PROVED"
        lines.append(f"- **{label}** `{arm}` (not required): {outcome or '<none>'}")

    return ok, lines


def cmd_verdict(args: argparse.Namespace) -> int:
    records: list[dict] = []
    for path in args.results:
        p = pathlib.Path(path)
        if not p.exists():
            print(f"note: {path} was not written by its step", file=sys.stderr)
            continue
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"FATAL: {path} is not JSON: {exc}", file=sys.stderr)
            return 2
        records.extend(loaded if isinstance(loaded, list) else [loaded])

    ok, lines = build_verdict(records)

    # A **NOT PROVED** bullet about THIS JOB's own subject cannot sit under the
    # header "SAFE AND REVERSIBLE". The major is the fact the whole rehearsal
    # turns on -- a rehearsal on the wrong PostgreSQL major proves nothing, which
    # is why the script reads it off the running server and refuses a declaration
    # that disagrees -- so a run that did not record it has not earned the green,
    # and before this it got one anyway with exit 0.
    #
    # Not the same as the closing "NOT PROVED here, by construction" line, which
    # is permanent and names something deliberately OUT of scope (the deploy).
    # This one names something IN scope that went missing.
    #
    # Not reachable from the pipeline as it stands - the step always passes
    # --pg-major "$PG_MAJOR" - which is exactly why it was worth closing: the
    # next person to edit that step is the one who finds out.
    if not args.pg_major:
        ok = False

    head = args.head or "<unknown>"
    previous = args.previous or "<unresolved>"
    body = [
        f"### Database rehearsal: {'SAFE AND REVERSIBLE' if ok else 'REFUSED'}",
        "",
        f"- **FACT** this commit: `{head}`",
        f"- **FACT** rehearsed against the baseline: `{previous}`"
        + (f" (resolved by {args.how})" if args.how else ""),
        f"- **FACT** PostgreSQL major asserted on the rehearsal server: `{args.pg_major}`"
        if args.pg_major
        else "- **NOT PROVED** the rehearsal server's PostgreSQL major was not recorded",
        "",
        *lines,
        "",
        "**NOT PROVED here, by construction:** that the deploy itself is safe. This job "
        "rehearses the SCHEMA on a hosted runner against the same PostgreSQL major "
        "production runs. It does not touch the box, the GPU or the live database.",
    ]
    _summary("\n".join(body))

    if not ok:
        print(
            "\nRefusing a green verdict: an arm failed or reported nothing. "
            "A schema change that cannot be proved reversible must not reach the box.",
            file=sys.stderr,
        )
    return 0 if ok else 1


# ------------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    b = sub.add_parser("baseline", help="resolve the previous released commit")
    b.add_argument("--repo", default=".")
    b.add_argument("--head", default="")
    b.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    b.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH", ""))
    b.add_argument("--default-branch", default="main")
    b.add_argument("--previous", default="")
    b.set_defaults(func=cmd_baseline)

    r = sub.add_parser("reversibility", help="boot the previous release over this commit's schema")
    r.add_argument("--old-image", required=True)
    r.add_argument("--dsn", required=True, help="a database ALREADY staged at --expect-schema")
    r.add_argument("--expect-schema", required=True, type=int)
    r.add_argument("--port", type=int, default=18731)
    r.add_argument(
        "--dead-port",
        type=int,
        default=65535,
        help="a port nothing listens on, so the model checks fail fast instead of hanging",
    )
    r.add_argument("--container", default="db-rehearsal-previous-release")
    r.add_argument("--boot-timeout", type=int, default=180)
    r.add_argument("--json", default="")
    r.set_defaults(func=cmd_reversibility)

    v = sub.add_parser("verdict", help="turn the arms' outcomes into a labelled FACT")
    v.add_argument("--results", nargs="+", required=True)
    v.add_argument("--head", default="")
    v.add_argument("--previous", default="")
    v.add_argument("--how", default="")
    v.add_argument("--pg-major", default="")
    v.set_defaults(func=cmd_verdict)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
