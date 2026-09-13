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
      outcome available. A `runs-on` that is an EXPRESSION (a matrix leg, a
      variable, a `fromJSON`) counts as self-hosted unless every value it can
      take is resolvable here and none of them is self-hosted, and the branch
      guard must be the positive `github.ref == 'refs/heads/<default>'` —
      an inverted `!=` guard is refused outright;
  P5  a restrictive top-level `permissions:`, and an explicit `permissions:`
      on every job that grants no WRITE scope unless the job is named in
      WRITE_SCOPES_NEEDED with the reason it needs it, so the GITHUB_TOKEN is
      least-privilege by construction and not merely by declaration;
  P6  no `${{ }}` interpolation of attacker-controllable values directly into
      a `run:` body. Those values must arrive through `env:`, where the shell
      sees them as data rather than as script text;
  P7  every job declares `timeout-minutes`. A job that does not inherits
      GitHub's SIX HOUR default. On a hosted runner that is wasted quota; on
      [self-hosted, dgx-spark] it is the production box's only runner held for
      a working day by a `docker compose up` waiting on a model that will
      never load, with every subsequent deploy queued behind it. A boolean is
      refused: YAML reads `timeout-minutes: true` as True, and Python's
      `isinstance(True, int)` is True;
  P8  job display names are plain ASCII. `name:` is not decoration: it is the
      string branch protection matches a required check against, the string
      the checks API and every notification integration report, and the string
      an auditor reads in an exported run. Decorative emoji in it are a
      matching hazard (they survive copy-paste inconsistently and cannot be
      typed reliably into a protection rule) and they make the audit trail
      look unserious. Status glyphs inside a step's SUMMARY output are fine —
      this is about the identity of the check.

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

#: `${{ … }}` inside a job name is legitimate (the launcher matrix names its
#: legs after the Python version). Strip the expressions before asking whether
#: what is left is plain ASCII, so a matrix expression is never mistaken for
#: decoration.
EXPRESSION = re.compile(r"\$\{\{.*?\}\}", re.DOTALL)

#: `matrix.<key>` inside a runs-on expression. The only expression P4 can
#: resolve statically, and only when the matrix itself is literal YAML.
MATRIX_REF = re.compile(r"^\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}$")

#: The one branch guard P4 accepts: a POSITIVE equality on github.ref. The
#: substring test that preceded it (2026-09-12 audit, F066) passed
#: `github.ref != 'refs/heads/main'`, which restricts a self-hosted job to
#: every branch EXCEPT main — the opposite of the rule, blessed by a green gate.
def _positive_ref_guard(default_branch: str) -> re.Pattern[str]:
    return re.compile(
        r"github\.ref\s*==\s*['\"]refs/heads/" + re.escape(default_branch) + r"['\"]"
    )


NEGATED_REF_GUARD = re.compile(r"github\.ref\s*!=")


def _split_top_level(expr: str, operator: str) -> list[str]:
    """Split an Actions expression on `||` or `&&` outside parens and quotes."""
    parts, depth, quote, start, i = [], 0, False, 0, 0
    while i < len(expr):
        ch = expr[i]
        if ch == "'":
            quote = not quote  # '' inside a string toggles twice: still correct
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
        elif not quote and depth == 0 and expr.startswith(operator, i):
            parts.append(expr[start:i])
            i += len(operator)
            start = i
            continue
        i += 1
    parts.append(expr[start:])
    return [part.strip() for part in parts]


def _strip_parens(expr: str) -> str:
    """`(a && b)` -> `a && b`, only when the outer pair encloses the whole."""
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth, quote = 0, False
        for i, ch in enumerate(expr):
            if ch == "'":
                quote = not quote
            elif not quote and ch == "(":
                depth += 1
            elif not quote and ch == ")":
                depth -= 1
                if depth == 0 and i != len(expr) - 1:
                    return expr  # `(a) && (b)`: the first pair closes early
        expr = expr[1:-1].strip()
    return expr


def _implies_ref_guard(cond: str, default_branch: str) -> bool:
    """True only if the condition cannot be true unless ref is the default branch.

    The substring test this replaced accepted a guard anywhere in the text,
    so `github.ref == 'refs/heads/main' || github.event_name == 'push'` — true
    on EVERY branch push — read as restricted. Here the guard has to be a
    conjunct of every top-level disjunct (recursively through parentheses);
    a negated conjunct (`!(...)`) never counts.
    """
    guard = _positive_ref_guard(default_branch)
    body = cond.strip()
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2]
    body = _strip_parens(body)
    for disjunct in _split_top_level(body, "||"):
        satisfied = False
        for conjunct in _split_top_level(_strip_parens(disjunct), "&&"):
            conjunct = conjunct.strip()
            if conjunct.startswith("!"):
                continue
            inner = _strip_parens(conjunct)
            if inner != conjunct:  # a parenthesised sub-expression
                if _implies_ref_guard(inner, default_branch):
                    satisfied = True
                    break
                continue
            if guard.fullmatch(conjunct):
                satisfied = True
                break
        if not satisfied:
            return False
    return True

#: Write scopes a job is ALLOWED to hold, keyed by (workflow file, job id), each
#: with the reason. EMPTY on 2026-09-13, and that is the finding it closes
#: (audit N022): P5 used to check only that a job DECLARED `permissions:`, so
#: `permissions: {contents: write, packages: write, id-token: write}` on a
#: pull_request job passed the gate as "least privilege". Every job in
#: pipeline.yml needs `contents: read` and nothing more. Adding an entry here is
#: the reviewable act of granting a job the power to push, publish or mint an
#: OIDC token; the reason is required so the review has something to read.
WRITE_SCOPES_NEEDED: dict[tuple[str, str], dict[str, str]] = {}

#: Scope values GitHub accepts. Anything else is a typo or a trick, and a
#: permissions block the policy cannot read is not one it can vouch for.
PERMISSION_VALUES = frozenset({"read", "write", "none"})


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


def _runs_on_values(job: dict) -> list[str]:
    """Every literal label `runs-on` names, WITHOUT resolving expressions."""
    runs_on = job.get("runs-on", "")
    if isinstance(runs_on, (list, tuple)):
        return [str(x) for x in runs_on]
    if isinstance(runs_on, dict):
        return [str(x) for x in (runs_on.get("labels") or [])] + [str(runs_on.get("group", ""))]
    return [str(runs_on)]


def _matrix_values(job: dict, key: str) -> list | None:
    """Every value `matrix.<key>` can take, or None if that cannot be known.

    Unknowable is: the matrix is itself an expression (`fromJSON(...)`), the
    key is absent (GitHub renders an empty string — not a label anyone meant),
    or an `include:` entry is not literal. `exclude:` only removes legs, so it
    cannot add a self-hosted one and is ignored.
    """
    strategy = job.get("strategy")
    if not isinstance(strategy, dict):
        return None
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return None
    values: list = []
    found = False
    if key in matrix:
        found = True
        raw = matrix[key]
        if not isinstance(raw, list):
            return None  # `runner: ${{ fromJSON(vars.RUNNERS) }}`
        values.extend(raw)
    include = matrix.get("include")
    if include is not None:
        if not isinstance(include, list):
            return None
        for entry in include:
            if not isinstance(entry, dict):
                return None
            if key in entry:
                found = True
                values.append(entry[key])
    return values if found else None


def _labels_of(value) -> list[str] | None:
    """A matrix value as a list of labels; None if it is not literal."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def _could_be_self_hosted(job: dict) -> bool:
    """True unless every value `runs-on` can take is known and hosted.

    Audit N021 (2026-09-12): `runs-on: ${{ matrix.runner }}` with
    `matrix.runner: [ubuntu-latest, self-hosted]` passed P4 with no `if:` at
    all, because the old check searched the LITERAL YAML text for
    "self-hosted" and the text was an expression. An expression is now an
    unknown, and an unknown is treated as the dangerous answer: only a
    `matrix.<key>` reference over a literal matrix is expanded, and anything
    else — `vars.RUNNER`, `inputs.runner`, `fromJSON(...)`, a ternary — is
    presumed able to land on the production box.
    """
    for label in _runs_on_values(job):
        if "self-hosted" in label.lower():
            return True
        if "${{" not in label:
            continue
        m = MATRIX_REF.match(label.strip())
        if not m:
            return True
        values = _matrix_values(job, m.group(1))
        if values is None:
            return True
        for value in values:
            labels = _labels_of(value)
            if labels is None:
                return True
            for item in labels:
                if "${{" in item or "self-hosted" in item.lower():
                    return True
    return False


def _write_scopes(perms) -> list[str] | None:
    """The write scopes a `permissions:` value grants; None if unreadable."""
    if perms == {} or perms is None:
        return []
    if isinstance(perms, str):
        if perms in ("read-all", "none"):
            return []
        if perms == "write-all":
            return ["write-all"]
        return None
    if isinstance(perms, dict):
        granted = []
        for scope, value in perms.items():
            if str(value) not in PERMISSION_VALUES:
                return None
            if value == "write":
                granted.append(str(scope))
        return granted
    return None


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
        if not _could_be_self_hosted(job):
            continue
        where = f"{name}:{job_id}"
        cond = str(job.get("if", ""))
        expression_runner = any("${{" in label for label in _runs_on_values(job))
        runner_phrase = (
            f"`runs-on: {_runs_on_text(job)}` is an expression that could "
            "evaluate to a self-hosted label, and it"
            if expression_runner
            else "runs on a self-hosted runner and"
        )
        if not cond:
            f.fail(
                "P4 self-hosted",
                where,
                f"{runner_phrase} has NO `if:` guard — every event "
                "the workflow accepts, including pull_request, would execute "
                "untrusted code on the persistent production box",
            )
            continue
        if NEGATED_REF_GUARD.search(cond):
            f.fail(
                "P4 self-hosted",
                where,
                f"`if:` contains an inverted ref guard (`github.ref !=`): {cond}. "
                "A self-hosted job is restricted TO the default branch, never "
                "away from it.",
            )
        if pr_triggered and "pull_request" in cond and "!=" not in cond:
            f.fail("P4 self-hosted", where, f"`if:` appears to admit pull_request: {cond}")
        if not _implies_ref_guard(cond, default_branch):
            f.fail(
                "P4 self-hosted",
                where,
                f"{runner_phrase} `if:` is not restricted to "
                f"`github.ref == 'refs/heads/{default_branch}'` on every path "
                "through it (the guard must be ANDed into each `||` branch). "
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
        where = f"{name}:{job_id}"
        if "permissions" not in job:
            if "uses" in job:
                continue  # reusable workflow call: permissions are declared there
            f.fail("P5 permissions", where, "job does not declare its own `permissions:`")
            continue
        # Declared is not the same as least. Audit N022: a job could grant
        # itself every write scope and P5 reported it as compliant. A reusable
        # call's `permissions:` is checked too — it caps what the callee gets.
        granted = _write_scopes(job["permissions"])
        if granted is None:
            f.fail(
                "P5 permissions",
                where,
                f"`permissions: {job['permissions']!r}` is not a mapping of scope "
                "to read/write/none (or read-all/none), so the policy cannot "
                "say what the token can do",
            )
            continue
        allowed = WRITE_SCOPES_NEEDED.get((name, str(job_id)), {})
        for scope in granted:
            if scope in allowed:
                continue
            grant = "`write-all`" if scope == "write-all" else f"`{scope}: write`"
            f.fail(
                "P5 permissions",
                where,
                f"job grants {grant} and is not listed in "
                "workflow_policy.WRITE_SCOPES_NEEDED with a reason. A token that "
                "can push, publish or mint OIDC credentials is granted "
                "deliberately, in review, or not at all.",
            )

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

    # ------------------------------------------------- P7 explicit timeouts
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        if "uses" in job:
            continue  # reusable workflow call: the timeout is declared there
        timeout = job.get("timeout-minutes")
        where = f"{name}:{job_id}"
        if timeout is None:
            f.fail(
                "P7 timeout",
                where,
                "job declares no `timeout-minutes`, so it inherits GitHub's "
                "six-hour default. A wedged job on the self-hosted runner "
                "holds the production box for six hours with every later "
                "deploy queued behind it.",
            )
        # `bool` first: True is an int to Python and a YAML `timeout-minutes:
        # true` passed this check as a one-minute budget (2026-09-12 review).
        elif isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
            f.fail("P7 timeout", where, f"`timeout-minutes: {timeout!r}` is not a positive whole number")

    # ------------------------------------------------ P8 ASCII display names
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        display = job.get("name")
        if display is None:
            continue  # GitHub falls back to the job id, which is already ASCII
        literal = EXPRESSION.sub("", str(display))
        stray = sorted({ch for ch in literal if ord(ch) > 127})
        if stray:
            f.fail(
                "P8 job name",
                f"{name}:{job_id}",
                f"display name {display!r} contains non-ASCII character(s) "
                f"{' '.join(stray)}. A job name is what branch protection "
                "matches, what the checks API reports and what an auditor "
                "reads; keep it plain text.",
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
        print("workflow policy: OK (P1-P8)")
        return 0
    print(f"\nworkflow policy: {len(f.rows)} finding(s)\n", file=sys.stderr)
    for check, where, detail in f.rows:
        print(f"  [{check}] {where}\n      {detail}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
