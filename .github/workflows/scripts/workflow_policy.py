#!/usr/bin/env python3
"""Static policy checks for this repository's GitHub Actions workflows.

These are the invariants that, if they break, cost more than a red build:

  P1  every workflow file parses as YAML;
  P2  no `pull_request_target` trigger anywhere. It runs UNTRUSTED fork code
      in a context that has the repository's secrets and a write token;
  P3  every external action is pinned to a full 40-hex commit SHA, with a
      trailing `# vX.Y.Z` comment recording which release that SHA is. A tag
      is a moving pointer the action's owner can repoint at any time;
  P4  no job that can run on a SELF-HOSTED runner is reachable from a
      `pull_request` event, and every such job is restricted to the default
      branch. This repository is PUBLIC and the self-hosted runner is the
      production box, so "a fork PR runs code on the DGX" is the single worst
      outcome available;
  P5  a restrictive top-level `permissions:`, and an explicit `permissions:`
      on every job, so the GITHUB_TOKEN is least-privilege by construction;
  P6  no `${{ }}` interpolation of attacker-controllable values directly into
      a `run:` body. Those values must arrive through `env:`, where the shell
      sees them as data rather than as script text.

Usage:  workflow_policy.py [--dir .github/workflows] [--default-branch main]
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

import yaml

#: `on:` is parsed by PyYAML 1.1 semantics as the boolean True. Look for both.
ON_KEYS = ("on", True)

#: Actions maintained by the same org as this repo's runner tooling still get
#: pinned; there is no allowlist. Local (`./…`) and reusable-workflow paths are
#: exempt because their content is this repository's, at this commit.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
USES_LINE = re.compile(r"^\s*(?:-\s*)?uses:\s*(?P<ref>\S+)\s*(?P<comment>#.*)?$")
VERSION_COMMENT = re.compile(r"#\s*v?\d+\.\d+(\.\d+)?", re.IGNORECASE)
DIGEST_RE = re.compile(r"^docker://[^@\s]+@sha256:[0-9a-f]{64}$")

#: Contexts an attacker can influence: a branch name, a PR title, an issue
#: body, a dispatch input. Interpolated into `run:` they become shell code.
INJECTABLE = re.compile(
    r"\$\{\{\s*(github\.event\b|github\.head_ref\b|inputs\.|github\.event\.inputs\.)"
)


class Findings:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def fail(self, check: str, where: str, detail: str) -> None:
        self.rows.append((check, where, detail))

    @property
    def ok(self) -> bool:
        return not self.rows


def _jobs(doc: dict) -> dict:
    jobs = doc.get("jobs")
    return jobs if isinstance(jobs, dict) else {}


def _triggers(doc: dict) -> dict:
    for key in ON_KEYS:
        if key in doc:
            value = doc[key]
            if isinstance(value, dict):
                return value
            if isinstance(value, str):
                return {value: None}
            if isinstance(value, list):
                return {k: None for k in value}
    return {}


def _runs_on_text(job: dict) -> str:
    runs_on = job.get("runs-on", "")
    if isinstance(runs_on, (list, tuple)):
        return " ".join(str(x) for x in runs_on)
    if isinstance(runs_on, dict):  # runs-on: {group:…, labels:[…]}
        return " ".join(str(x) for x in runs_on.get("labels", []) or []) + " " + str(
            runs_on.get("group", "")
        )
    return str(runs_on)


def _perm_is_restrictive(perms) -> bool:
    """`{}`/`none` is ideal; otherwise nothing above `read` at the top level."""
    if perms is None:
        return False
    if perms == {} or perms == "none":
        return True
    if isinstance(perms, str):
        return perms == "read-all"
    if isinstance(perms, dict):
        return all(str(v) in ("read", "none") for v in perms.values())
    return False


def check_file(path: pathlib.Path, default_branch: str, f: Findings) -> None:
    text = path.read_text(encoding="utf-8")
    name = path.name

    # ---------------------------------------------------------------- P1 parse
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        f.fail("P1 yaml", name, f"does not parse: {exc}")
        return
    if not isinstance(doc, dict):
        f.fail("P1 yaml", name, "top level is not a mapping")
        return

    triggers = _triggers(doc)
    jobs = _jobs(doc)

    # ------------------------------------------- P2 no pull_request_target
    if "pull_request_target" in triggers:
        f.fail(
            "P2 pull_request_target",
            name,
            "runs untrusted fork code WITH this repository's secrets; use "
            "`pull_request` and keep privileged work in a separate workflow",
        )

    # ----------------------------------------------- P3 actions pinned to SHA
    for lineno, line in enumerate(text.splitlines(), 1):
        m = USES_LINE.match(line)
        if not m:
            continue
        ref = m.group("ref").strip("'\"")
        comment = (m.group("comment") or "").strip()
        where = f"{name}:{lineno}"
        if ref.startswith("./") or ref.startswith(".github/"):
            continue  # this repository's own code, at this commit
        if ref.startswith("docker://"):
            if not DIGEST_RE.match(ref):
                f.fail("P3 pin", where, f"container action `{ref}` is not pinned by @sha256 digest")
            continue
        if "@" not in ref:
            f.fail("P3 pin", where, f"`{ref}` has no ref at all")
            continue
        repo, _, rev = ref.rpartition("@")
        if not SHA_RE.match(rev):
            f.fail(
                "P3 pin",
                where,
                f"`{repo}` is pinned to `{rev}`, which is a TAG or branch, not a "
                "40-hex commit SHA. A tag can be repointed by the action's owner.",
            )
            continue
        if not VERSION_COMMENT.search(comment):
            f.fail(
                "P3 pin",
                where,
                f"`{repo}@{rev[:12]}…` has no trailing `# vX.Y.Z` comment saying "
                "which release the SHA is; an unlabelled SHA cannot be reviewed",
            )

    # ---------------------------- P4 self-hosted runners never see fork code
    pr_triggered = "pull_request" in triggers or "pull_request_target" in triggers
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if "self-hosted" not in _runs_on_text(job).lower():
            continue
        where = f"{name}:{job_id}"
        cond = str(job.get("if", ""))
        if not cond:
            f.fail(
                "P4 self-hosted",
                where,
                "runs on a self-hosted runner with NO `if:` guard — every event "
                "the workflow accepts, including pull_request, would execute "
                "untrusted code on the persistent production box",
            )
            continue
        if pr_triggered and "pull_request" in cond and "!=" not in cond:
            f.fail("P4 self-hosted", where, f"`if:` appears to admit pull_request: {cond}")
        if f"refs/heads/{default_branch}" not in cond:
            f.fail(
                "P4 self-hosted",
                where,
                f"`if:` does not restrict the job to refs/heads/{default_branch}. "
                "Every path into a self-hosted deploy — push AND workflow_dispatch "
                "— must carry the branch restriction; a dispatch clause without "
                "one lets any branch deploy.",
            )
        if "workflow_dispatch" in triggers and "workflow_dispatch" in cond:
            # The dispatch clause must ALSO be ref-restricted. Catch the shape
            # `event_name == 'workflow_dispatch' || (push && ref == main)`,
            # where the dispatch half is unrestricted.
            unguarded = re.search(
                r"github\.event_name\s*==\s*'workflow_dispatch'\s*(\|\||$|\n)", cond
            )
            if unguarded:
                f.fail(
                    "P4 self-hosted",
                    where,
                    "the workflow_dispatch clause is not itself ref-restricted: "
                    "`event_name == 'workflow_dispatch' ||` deploys whatever "
                    "branch the dispatch was fired from, bypassing the push-side "
                    "branch restriction",
                )

    # -------------------------------------------------- P5 least privilege
    if "permissions" not in doc:
        f.fail("P5 permissions", name, "no top-level `permissions:` — the token defaults to the repo setting")
    elif not _perm_is_restrictive(doc["permissions"]):
        f.fail("P5 permissions", name, f"top-level `permissions:` is not read-only: {doc['permissions']!r}")
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if "uses" in job:
            continue  # reusable workflow call: permissions are declared there
        if "permissions" not in job:
            f.fail("P5 permissions", f"{name}:{job_id}", "job does not declare its own `permissions:`")

    # ------------------------------------------- P6 no injection into run:
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for idx, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            body = step.get("run")
            if not isinstance(body, str):
                continue
            for hit in INJECTABLE.finditer(body):
                f.fail(
                    "P6 injection",
                    f"{name}:{job_id}:step[{idx}]",
                    f"`run:` interpolates {hit.group(1)}… directly into the shell. "
                    "Pass it through `env:` instead, so it is data and not script.",
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", default=".github/workflows")
    parser.add_argument("--default-branch", default="main")
    args = parser.parse_args(argv)

    root = pathlib.Path(args.dir)
    files = sorted([p for p in root.glob("*.yml")] + [p for p in root.glob("*.yaml")])
    if not files:
        print(f"no workflow files under {root}", file=sys.stderr)
        return 2

    f = Findings()
    for path in files:
        check_file(path, args.default_branch, f)

    print(f"checked {len(files)} workflow file(s): {', '.join(p.name for p in files)}")
    if f.ok:
        print("workflow policy: OK (P1-P6)")
        return 0
    print(f"\nworkflow policy: {len(f.rows)} finding(s)\n", file=sys.stderr)
    for check, where, detail in f.rows:
        print(f"  [{check}] {where}\n      {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
