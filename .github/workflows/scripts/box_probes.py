#!/usr/bin/env python3
"""Read-only probes of the production box, written to be SAFE TO PRINT.

WHAT THIS IS
------------
The shared library behind two jobs: `box-readiness` in pipeline.yml (the
pre-deploy read, before the release path touches anything) and, later, the
standing watch in production-watch.yml. Both ask the same question — "is this
machine in a state where a deploy would succeed?" — and both answer it from a
PUBLIC repository's log, so both need the same two properties.

WHAT THE SECOND CONSUMER MAY CALL is SECOND_CONSUMER_PROBES, further down, and
it is a short list. Until 2026-09-28 this paragraph claimed a shared library
while nothing in the file was callable by a consumer that did not already hold
an `Environment` — and box_readiness.py is the only thing that builds one, so
"shared" was aspirational. The two probes on that surface now take a deploy root
and a timeout by keyword, and the names and result shape there are the contract
production_truth.REQUIRED_PROBES is reconciled against. Two probes that watch
asks for are NOT here and are not meant to be; the surface says which and why.

The two properties:

  1. EVERY PROBE IS READ-ONLY. Nothing here starts, stops, recreates, prunes
     or changes the STATE OF THE STACK. The deploy flock is ASKED whether it is
     free, by exactly the mechanism scripts/lib/deploy-common.sh's own
     `dr_lock_is_held` uses: a non-blocking `flock -n` on a descriptor that is
     closed immediately.

     "Nothing writes" was too strong, and is corrected here. Two places touch
     something, and both are named so that nobody has to discover them:

       * `git status` REFRESHES THE INDEX unless it is told not to. Measured on
         git 2.43.0 in a scratch repository on this box on 2026-09-27: a plain
         `git status --porcelain --untracked-files=no` against a stale index
         opens `.git/index.lock` with O_CREAT|O_EXCL and rewrites `.git/index`
         (mtime moved), while `git --no-optional-locks status ...` opens no lock
         and leaves the index byte-for-byte alone. The deploy root is a SHARED
         working tree, so every git call this library makes there carries
         `--no-optional-locks`. (The same measurement also refutes the worry
         that a peer holding `.git/index.lock` makes this probe fail: with a
         lock file present, plain `git status --porcelain` still exited 0 with
         empty stderr -- the refresh is best-effort and git skips it silently.)
       * `probe_migrations` reaches `dr_live_schema_version`, whose fallback
         runs `docker exec <production postgres> psql -tAc 'SELECT
         COALESCE(MAX(version), 0) FROM schema_migrations'`
         (scripts/lib/deploy-common.sh:274-275). That is a SELECT, but it is a
         command executed inside the production database container, which is
         more than the phrase "seven reads" suggests on its own.

     An earlier version of this paragraph said the flock was "OBSERVED, never
     taken". That was not true and the claim is withdrawn: `flock -n` DOES
     take the lock. What is true, and is the property that matters next to a
     rollout, is that it is held only for the lifetime of a `bash -c` that
     does nothing else, it is never waited for, and it is never taken in order
     to act. A rollout that wants it is delayed by microseconds, never by this
     job's decision, and never for the length of a probe.

  2. NOTHING THAT IS NOT ON AN ALLOWLIST REACHES STDOUT. See THE PRINT
     ALLOWLIST below. This is a security control, not formatting.

EVERY PROBE FAILS CLOSED
------------------------
A probe that cannot be PERFORMED is a refusal, never a shrug. `curl` did not
connect, the controller returned something that is not JSON, `git` exited
non-zero, the helper raised, the subprocess timed out: all of them refuse. The
one deliberate exception is a deploy lock file that does not exist yet, which
is the first-deploy state and is exactly what scripts/deploy-preflight.sh's
equivalent check treats as "not created yet".

THE PRINT ALLOWLIST (audit B6)
------------------------------
The deploy root's `.runtime/generated.env` was verified on the box to hold 220
keys. Among them: `TECHSARA_SECRET_ENV`, which POINTS AT THE REAL SECRETS FILE
where this deployment's owner password lives, and at least six address-valued
keys (`TECHSARA_ENGINE_HEAD_API_URL`, `TECHSARA_BIND_ADDRESS`,
`TECHSARA_MODEL_BIND_ADDRESS`, `TECHSARA_ROUTER_HEALTH_URL`,
`TECHSARA_HEAD_GPU_EXPORTER_URL`, `TECHSARA_WORKER_GPU_EXPORTER_URL`). A run
summary on this repository is world-readable. So:

  * `read_env_keys` parses ONLY keys named in READABLE_KEYS, by exact match.
    It never `source`s the file — a shell that sources it inherits all 220
    values, and one `set -x` later they are in the log. It never returns, logs
    or raises anything containing the file's contents, and the CLI never
    prints the file, the mapping, or any value read from it;
  * separately from that READ allowlist there is a PRINT allowlist. A probe
    reports FACTS as a mapping, and only keys in FACT_KINDS are rendered at
    all. Each kind validates its value — a count is a non-negative int, a sha
    is 7-40 hex digits, a token matches a narrow charset — and a value that
    fails validation is rendered as the fixed label `<withheld>`, never as
    itself;
  * `sanitize()` is the last line of defence and is applied to EVERY line any
    caller emits, including lines that came out of another program: it redacts
    anything with a URL scheme and any IPv4 or IPv6 literal;
  * an exception's MESSAGE is never printed. Only its class name, and only
    after charset validation. `str(exc)` on the wire is how a value from a
    file reaches a log without anyone deciding that it should.

The rule lives HERE rather than in the CLI so that the second consumer
inherits it by construction instead of by remembering — which is only true of a
consumer that actually calls into this file, hence SECOND_CONSUMER_PROBES.

WHAT THIS DOES NOT CLAIM
------------------------
A probe is a reading taken at one moment. It is not a guarantee held later:
scripts/deploy.sh's own preflight still runs and stays authoritative. Nothing
in this file may be used as an argument for removing a check from it.
"""
from __future__ import annotations

import dataclasses
import ipaddress
import json
import pathlib
import re
import subprocess
import time
from typing import Callable, Iterable, Mapping, Sequence

# --------------------------------------------------------------------- policy

#: The free-space floor, in GB. It is the SAME number pipeline.yml's deploy
#: preflight enforces ("20 GB is roughly one orchestrator image plus
#: headroom"). A readiness check that used a different threshold would either
#: pass things the deploy then refuses, or refuse things the deploy allows —
#: and both make this job noise rather than evidence.
DISK_FLOOR_GB = 20

#: The ONLY generated.env keys this library is allowed to read. Adding one is
#: a reviewable act: the file also holds the pointer to the secrets file and
#: every address the cluster uses.
READABLE_KEYS = frozenset({"MAIN_MODEL"})

#: Where the engine controller answers. Loopback, and the same default
#: scripts/cluster-status.sh and scripts/cluster-recover.sh use.
DEFAULT_CONTROLLER_URL = "http://127.0.0.1:9838"

#: The controller's own state codes for "serving" (2 = READY, 3 = BUSY), read
#: the same way scripts/cluster-status.sh reads them.
CONTROLLER_SERVING_CODES = (2, 3)

#: The completion probe asks for a handful of tokens and nothing more. This
#: shares a GPU with live chat, and the main model is tensor-parallel across
#: BOTH nodes, so one generation costs both of them.
COMPLETION_MAX_TOKENS = 8
COMPLETION_PROMPT = "Reply with the single word: READY."

#: Per-probe subprocess ceilings, in seconds.
#:
#: These are PER CALL, not per probe, and that distinction is the bug this
#: comment used to hide. It read "their sum plus the checkout has to stay
#: inside the job's 8-minute ceiling even when every one of them times out:
#: 30 + 30 + 15 + 150 + 15 + 120 + 45 = 405s". Every term of that sum was
#: wrong in the same direction:
#:
#:   * probe_deploy_root runs THREE git calls (is-inside-work-tree, status,
#:     rev-parse), so its ceiling is 90s and not 30s;
#:   * probe_migrations runs TWO bash calls (live, then code), so 90s not 45s;
#:   * probe_real_completion runs FOUR calls -- resolve, /metrics, the
#:     completion, /metrics again -- and the completion's own subprocess timeout
#:     is TIMEOUTS["completion"] + COMPLETION_CURL_GRACE_S.
#:
#: `completion` is 180 and not 120 BECAUSE THAT IS WHAT `verify` GIVES THE SAME
#: CURL (pipeline.yml, "The model actually generates (not just answers
#: /health)": `curl -fsS -m 180`). The two were 120 and 180, which is a gate
#: that refuses a box the job an hour later would have passed -- on a slow
#: prefill, the pre-deploy read times out and the post-deploy read does not.
#: A readiness check with a tighter deadline than the check it stands in for is
#: noise, exactly as a different disk floor would be.
#:
#: The real worst case does NOT fit in eight minutes: a box
#: slow enough to walk every call up to its ceiling would have the job killed
#: by GitHub before box_readiness.py printed its table, and the table with the
#: remedies in it is the entire product of this job. So the ceiling below is
#: the one pipeline.yml declares, and the arithmetic is no longer written down
#: anywhere as a number a human maintains: tests/test_box_wiring.py MEASURES
#: it by running every probe against a recording runner, and asserts both that
#: it fits under JOB_TIMEOUT_MINUTES and that pipeline.yml declares that same
#: number.
TIMEOUTS = {
    "git": 30,
    "disk": 30,
    "lock": 15,
    "exposure": 150,
    "controller": 15,
    "completion": 180,
    "migrations": 45,
}

#: How much longer the completion's subprocess may live than the deadline curl
#: was itself given, so that a curl which honours `-m` reports its own timeout
#: (an exit code, which names a remedy) instead of being killed from outside it
#: (a TimeoutExpired, which names none).
COMPLETION_CURL_GRACE_S = 15

#: The `timeout-minutes` pipeline.yml declares on the `box-readiness` job, and
#: the slice of it reserved for actions/checkout on the box. The test suite
#: asserts BOTH directions: that pipeline.yml declares exactly this number, and
#: that the measured worst-case subprocess wall clock fits inside it with the
#: checkout allowance subtracted. Neither can drift without a red test.
#:
#: It was 14 while TIMEOUTS["completion"] was 120 (worst case 570s, budget 660s,
#: 90s of headroom). Aligning that deadline with the one `verify` gives the same
#: curl (180) moved the worst case to 630s, which still fits in 14 -- but with
#: 30s of headroom, and 30s is not headroom on the only box this job exists for.
#: 16 restores it to 150s. The cost of a larger ceiling is bounded and small: a
#: job that hangs delays the SKIP of a deploy that was not going to happen, by
#: two minutes, against the 55 minutes of hosted CI this job is here to save.
#: Measured on 2026-09-28: worst case 630s, budget 780s, headroom 150s.
JOB_TIMEOUT_MINUTES = 16
CHECKOUT_ALLOWANCE_S = 180

WITHHELD = "<withheld>"

# ------------------------------------------------------------------- verdicts

#: Every verdict any probe may report. A verdict is the one string that is
#: printed verbatim, so the set is closed: a typo cannot invent a new one, and
#: a value from outside this file can never be reported as a verdict.
VERDICTS = frozenset(
    {
        # deploy-root
        "clean-on-default", "dirty-tree", "wrong-branch", "not-a-checkout", "git-unreadable",
        # disk
        "ok", "below-floor", "unreadable",
        # deploy-lock
        "free", "held", "never-created",
        # engine-exposure
        "closed", "exposed", "unproven",
        # engine-controller
        "ready", "not-ready", "recovering", "unreachable",
        # completion
        "generated", "wedged", "empty-reply", "metrics-unreadable", "model-unknown",
        # migrations
        "equal", "forward", "behind",
        # universal
        "probe-raised", "probe-timed-out",
    }
)

#: The two verdicts `run_probe` can report for ANY probe, whatever the probe
#: itself does. They are the reason `remedy_for` has a fallback.
UNIVERSAL_VERDICTS = frozenset({"probe-raised", "probe-timed-out"})

#: Verdicts that mean the probe COULD NOT BE CARRIED OUT, as distinct from
#: carried out and refused. `ProbeResult.could_run` reads this, and the second
#: consumer reads that: a reading that was never taken must not be reported as
#: a fault, because a fault is handed to Prometheus and stays green while a
#: run that could not measure the box goes red.
#:
#: ("migrations", "unreadable") is deliberately in BOTH this set and
#: PASSING["migrations"], and the two are not in conflict. They answer different
#: questions: the probe could not be performed (this set), AND scripts/deploy.sh
#: proceeds anyway in exactly that case (PASSING), so box-readiness does too.
NOT_PERFORMED = frozenset(
    {
        "probe-raised", "probe-timed-out",
        "git-unreadable", "unreadable", "unreachable", "unproven",
        "metrics-unreadable", "model-unknown",
    }
)

#: Which verdicts each probe can actually REPORT. Not decoration: the remedy
#: coverage test iterates this rather than `VERDICTS`, and `VERDICTS` used to be
#: the only list, which made that test ask for a remedy for impossible pairs
#: like ("disk", "wedged") -- 200-odd pairs that all fell through to
#: `remedy_for`'s catch-all, so the test passed with the whole remedy table
#: deleted. Measured on 2026-09-27 by deleting `_remedies()`'s body: 23 tests,
#: OK. tests/test_box_probes.py now asserts this map against VERDICTS in both
#: directions, so a verdict a probe cannot produce cannot sit in either list
#: pretending to be covered -- which is how `unavailable` (declared for
#: engine-exposure, returned by nothing) survived review.
PROBE_VERDICTS: Mapping[str, frozenset] = {
    "deploy-root": frozenset(
        {"clean-on-default", "dirty-tree", "wrong-branch", "not-a-checkout", "git-unreadable"}
    ),
    "disk": frozenset({"ok", "below-floor", "unreadable"}),
    "deploy-lock": frozenset({"free", "held", "never-created", "unreadable"}),
    "engine-exposure": frozenset({"closed", "exposed", "unproven"}),
    "engine-controller": frozenset({"ready", "not-ready", "recovering", "unreachable", "unreadable"}),
    "completion": frozenset(
        {"generated", "wedged", "empty-reply", "metrics-unreadable", "model-unknown", "unreachable"}
    ),
    "migrations": frozenset({"equal", "forward", "behind", "unreadable"}),
}

#: The verdicts that let a probe pass. EVERYTHING ELSE IS A REFUSAL — including
#: any verdict added later and forgotten here, which is the direction a
#: fail-closed default has to lean.
PASSING: Mapping[str, frozenset] = {
    # `wrong-branch` PASSES, and it is reported. It was a refusal, and that was
    # a self-lockout: scripts/deploy.sh puts the production checkout on a
    # DETACHED HEAD on seven distinct paths (`detach_to`, deploy.sh:290, called
    # from deploy.sh:310/314/332/338/346/356/361), one of which is "the branch
    # is checked out in another worktree" -- a state deploy.sh's own comment
    # calls "a real configuration on this box". `git rev-parse --abbrev-ref
    # HEAD` then prints `HEAD`, so on_default_branch is false. Refusing on that
    # blocked `deploy` on every subsequent push AND dispatch until a human ran
    # `git checkout main` in the shared checkout -- while the very deploy being
    # blocked is what recovers it: `land_on_branch` (deploy.sh:300) checks the
    # branch out and fast-forwards it.
    #
    # And there was no authority behind the refusal. Nothing in the release
    # path requires the deploy root to be ON a branch: deploy.sh's preflight
    # does not check, `land()` handles an unset DEPLOY_BRANCH by detaching ON
    # PURPOSE, and the workflow's own "the box is serving the commit we asked
    # for" step compares `git rev-parse HEAD`, which reads the same on a
    # detached HEAD. So the branch is a FACT worth printing (on_default_branch
    # is in the table either way) and not a verdict worth blocking a release on.
    "deploy-root": frozenset({"clean-on-default", "wrong-branch"}),
    "disk": frozenset({"ok"}),
    # A lock file that was never created is the first-deploy state, and it is
    # what the deploy's own preflight already accepts. `held` is a refusal: a
    # second deploy must not start while one is in flight, and refusing here
    # is also what keeps this job honest without sharing the release path's
    # concurrency group.
    "deploy-lock": frozenset({"free", "never-created"}),
    "engine-exposure": frozenset({"closed"}),
    # `not-ready` PASSES, and it is reported. This is the same self-lockout
    # shape as `wrong-branch`, found the same way -- by running the probe against
    # the real box -- and the authority it was resting on does not exist.
    #
    # MEASURED 2026-09-28, load average 29.75: the controller answered `DEGRADED`
    # (state_code 4, primary_ready false) with its own `reason` reading "canary
    # timed out twice but the engine is progressing (2 running, 1 waiting, kv
    # 22%): saturation, not a wedge". The box was busy, not broken, and that is
    # not the controller's opinion but a measurement: Prometheus, queried at the
    # same time and WITHOUT spending a generation, gave
    # `increase(vllm:generation_tokens_total{service="main"}[10m])` = 5605 for
    # Qwen/Qwen3.6-35B-A3B-NVFP4. The engine produced five and a half thousand
    # tokens in ten minutes while the controller called itself DEGRADED. This
    # probe refused, and `deploy` would therefore have been skipped for as long
    # as the box stayed busy -- on a machine that is busy most of the time.
    #
    # THE RELEASE PATH ALLOWS IT. scripts/deploy.sh's post-rollout health gate
    # (deploy.sh:938) runs `scripts/cluster-status.sh` and gates on its exit
    # status. That script embeds `sys.exit(0 if code in (2, 3) else 3)` -- which
    # is where this probe's (2, 3) comes from -- but it consumes that exit with
    # `check_warn "engine controller reports a non-ready state"`, and
    # `check_summary` in scripts/lib/cluster-common.sh:44-45 returns
    # `[ "$CHECK_FAIL" -eq 0 ]`. A warning is not a failure. Measured on the same
    # DEGRADED box: `./scripts/cluster-status.sh` printed "11 passed, 2 warnings,
    # 0 failed" and exited 0. So the state this probe refused on is one the
    # deploy's own health gate passes.
    #
    # AND THE QUESTION IT WAS STANDING IN FOR IS ANSWERED PROPERLY ELSEWHERE IN
    # THIS JOB. What a non-ready controller might mean is "the engine cannot
    # serve", and `probe_real_completion` measures that directly: a real
    # generation, with `vllm:generation_tokens_total` required to advance. A
    # wedged engine still refuses -- as `completion wedged`, by the strong
    # signal, not by a state code. A saturated one passes, which is correct,
    # because it is serving. This is the same rule as everywhere else here:
    # prove an engine is wedged with a completion, never with a status field.
    #
    # `recovering` STAYS A REFUSAL, and the argument above is deliberately not
    # extended to it: a recovery in flight is moving the same containers the
    # rollout will move, which is a race the release path cannot see and
    # cluster-status.sh's exit code says nothing about. `unreachable` and
    # `unreadable` also stay refusals -- not from an argument, but from the
    # absence of one: no reading of this box in either state has been taken, and
    # the fail-closed default holds until somebody has one.
    "engine-controller": frozenset({"ready", "not-ready"}),
    "completion": frozenset({"generated"}),
    # `unreadable` PASSES, because scripts/deploy.sh:244-252 deliberately
    # proceeds in exactly that case: "cannot read the live schema version
    # (orchestrator down and psql unavailable) ... proceeding WITHOUT the
    # compatibility check - this is the one case where the check cannot be made,
    # and it is worth knowing it was skipped". Refusing it here meant a stack
    # that is DOWN could no longer be deployed to through CI -- and a deploy is
    # what brings it back. deploy.sh:256-257 tolerates an unreadable CODE
    # version for the same reason. Whichever number WAS readable stays in the
    # facts, so the table still reports the reading that could not be taken:
    # that is deploy.sh's "proceed and say so", not a shrug.
    #
    # `behind` stays a refusal. deploy.sh refuses it too (deploy.sh:263-271),
    # and its ALLOW_SCHEMA_DOWNGRADE=1 override has never been reachable from
    # CI on any branch -- verified on 2026-09-27: `git grep ALLOW_SCHEMA_DOWNGRADE
    # origin/dev -- .github/` and the same on this branch both return nothing.
    # It is a hand-run-deploy override, and a hand-run deploy does not pass
    # through this job at all.
    "migrations": frozenset({"equal", "forward", "unreadable"}),
}

# -------------------------------------------------------------- print allowlist

#: fact key -> kind. A key that is not here is not printable at all.
FACT_KINDS: Mapping[str, str] = {
    "dirty_files": "count",
    "on_default_branch": "flag",
    "free_gb": "count",
    "floor_gb": "count",
    "holder_pid": "count",
    "holder_origin": "token",
    "holder_head": "sha",
    "held_for_s": "duration",
    "engine_state": "token",
    "state_code": "count",
    "primary_ready": "flag",
    "recovery_in_progress": "flag",
    "accepted_addresses": "count",
    "tokens_before": "count",
    "tokens_after": "count",
    "reply_chars": "count",
    "reasoning_chars": "count",
    "elapsed_s": "duration",
    "live_schema": "count",
    "code_schema": "count",
    "forward_n": "count",
    "exit_code": "count",
    "exception_type": "token",
    # Header fields, printed once by the CLI. A path is allowed because the
    # only two this library ever renders are the deploy root and the workspace
    # checkout, both of which pipeline.yml already names in the clear -- and
    # the kind still refuses anything carrying a URL or an address.
    "ref": "sha",
    "deploy_root": "path",
    "repo_root": "path",
}

#: A token is a short identifier-ish string. Controller state names and
#: exception class names come from outside this file, so they are validated
#: rather than trusted: no dots, no slashes, no colons, nothing that could
#: carry a host, a path or a URL.
_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

#: A URL scheme and everything attached to it.
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S*")
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
#: Candidates for an IPv6 literal: anything hex-and-colon with at least two
#: colons. Each candidate is then handed to `ipaddress` to decide, so a short
#: sha, a duration or a count is never mistaken for an address.
_IPV6_CANDIDATE_RE = re.compile(r"[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:]*(?:%[0-9A-Za-z_.-]+)?")


#: engine_bind.py's PER-ADDRESS detail line, which is one line per address that
#: accepted a connection to the engine port (engine_bind.py:879). Anchored to
#: the start of the line and to the list-item dash so that the summary
#: `report.fail` renders as "- FAIL: ... ACCEPTED the connection: ..." cannot be
#: counted as an address.
_ACCEPTED_ADDRESS_RE = re.compile(r"(?m)^\s*-\s.*ACCEPTED the connection on the engine port\s*$")

#: The bare phrase, used only as a trip-wire: see probe_engine_exposure.
_ACCEPTED_PHRASE_RE = re.compile(r"ACCEPTED the connection")


def _is_ip_literal(text: str) -> bool:
    candidate = text.split("%", 1)[0]
    if not candidate:
        return False
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def sanitize(text: str) -> str:
    """Redact URLs and IP literals from a line before it is printed.

    Applied to EVERY line, including text produced by another program. The
    wording above never interpolates an address, and this makes sure of it —
    the same belt-and-braces shape engine_bind.py's own `scrub` uses, and for
    the same reason: this log is public.
    """
    text = _SCHEME_RE.sub(WITHHELD, text)
    text = _IPV4_RE.sub(WITHHELD, text)

    def _ipv6(match: re.Match[str]) -> str:
        return WITHHELD if _is_ip_literal(match.group(0)) else match.group(0)

    return _IPV6_CANDIDATE_RE.sub(_ipv6, text)


def render_fact(key: str, value: object) -> str:
    """One fact as a printable string, or the fixed label if it is not allowed.

    The rendering is the allowlist. There is no path from a value to stdout
    that does not pass through here, so a key nobody reviewed, or a value that
    is not the shape its kind promises, cannot be printed by accident.
    """
    kind = FACT_KINDS.get(key)
    if kind is None:
        return WITHHELD
    if kind == "count":
        if isinstance(value, bool) or not isinstance(value, int):
            return WITHHELD
        return str(value) if 0 <= value <= 10**12 else WITHHELD
    if kind == "flag":
        return ("yes" if value else "no") if isinstance(value, bool) else WITHHELD
    if kind == "duration":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return WITHHELD
        return f"{float(value):.1f}s" if 0 <= float(value) < 10**7 else WITHHELD
    if kind == "sha":
        return value[:12] if isinstance(value, str) and _SHA_RE.match(value) else WITHHELD
    if kind == "token":
        return value if isinstance(value, str) and _TOKEN_RE.match(value) else WITHHELD
    if kind == "path":
        if not isinstance(value, str) or not value or len(value) > 200:
            return WITHHELD
        if any(ch in value for ch in "\r\n") or "://" in value:
            return WITHHELD
        return WITHHELD if sanitize(value) != value else value
    return WITHHELD


def render_facts(facts: Mapping[str, object]) -> str:
    """`key value | key value`, allowlisted, in the order the probe set them."""
    return " | ".join(f"{k} {render_fact(k, v)}" for k, v in facts.items())


# ---------------------------------------------------------------- the results

@dataclasses.dataclass(frozen=True)
class ProbeResult:
    probe: str
    verdict: str
    facts: Mapping[str, object] = dataclasses.field(default_factory=dict)
    #: Lines of FOREIGN output (today: engine_bind.py's own report) to show
    #: under the table. Sanitized on the way out, never trusted on the way in.
    #:
    #: It was called `detail` until the second consumer arrived. `detail` is now
    #: the ONE-LINE property below, because production_truth.normalise() reads
    #: an attribute of that name and renders it inside a single table cell:
    #: handing it twenty lines of engine_bind report would have put the whole
    #: report in one row of a 3 a.m. alert table.
    report_lines: Sequence[str] = ()

    @property
    def ok(self) -> bool:
        """Fail closed: a verdict nobody listed as passing is a refusal."""
        return self.verdict in PASSING.get(self.probe, frozenset())

    @property
    def safe_verdict(self) -> str:
        return self.verdict if self.verdict in VERDICTS else WITHHELD

    @property
    def could_run(self) -> bool:
        """Was the probe CARRIED OUT at all, whatever it then found?

        Part of the second-consumer surface: production_truth.normalise() reads
        `performed`, falling back to `could_run`, and a probe that could not be
        carried out must not be reported as a measurement of the box. Without
        this property that fallback defaulted to True, so a `probe-raised`
        reading would have been handed to Prometheus as a real fault instead of
        making the run red.
        """
        return self.verdict not in NOT_PERFORMED

    @property
    def detail(self) -> str:
        """ONE short allowlisted line, for a consumer that has no table.

        The facts and nothing else: the verdict is what the consumer already
        prints beside it, and every value here has been through `render_fact`.
        """
        return render_facts(self.facts)


# --------------------------------------------------------------- the commands

@dataclasses.dataclass(frozen=True)
class Completed:
    rc: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    """Runs a command and returns its output. Injected, so tests need no box."""

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        env: Mapping[str, str] | None = None,
    ) -> Completed:
        import os as _os

        merged = dict(_os.environ)
        if env:
            merged.update(env)
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged,
            stdin=subprocess.DEVNULL,
        )
        return Completed(proc.returncode, proc.stdout or "", proc.stderr or "")


@dataclasses.dataclass
class Environment:
    """Everything a probe is allowed to know about where it is running."""

    deploy_root: pathlib.Path
    repo_root: pathlib.Path
    ref: str
    runner: CommandRunner = dataclasses.field(default_factory=CommandRunner)
    clock: Callable[[], float] = time.time
    default_branch: str = "main"
    controller_url: str = DEFAULT_CONTROLLER_URL
    #: A CEILING a caller may put on every per-call subprocess timeout. None
    #: means this library's own TIMEOUTS stand, which is what box-readiness
    #: wants: its ceiling is measured against them. The standing watch passes
    #: one, because a monitor that ticks on a schedule cannot spend this job's
    #: worst case. It only ever LOWERS a timeout -- `budget` takes the min, so a
    #: second consumer cannot widen a deadline this file chose.
    timeout_cap: float | None = None

    def budget(self, kind: str) -> float:
        """The subprocess timeout for ONE call of `kind`, honouring the cap.

        Every probe asks for its timeout here rather than reading TIMEOUTS
        directly, so there is one place where a cap can be applied and one
        place tests/test_box_wiring.py has to record to measure the worst case.
        """
        base = float(TIMEOUTS[kind])
        if self.timeout_cap is None:
            return base
        return max(1.0, min(base, float(self.timeout_cap)))

    # Paths, kept in one place so no probe spells one twice.
    @property
    def generated_env(self) -> pathlib.Path:
        return self.deploy_root / ".runtime" / "generated.env"

    @property
    def lock_file(self) -> pathlib.Path:
        return self.deploy_root / ".runtime" / "locks" / "deploy.lock"

    @property
    def holder_file(self) -> pathlib.Path:
        return self.deploy_root / ".runtime" / "locks" / "deploy.holder"

    @property
    def engine_bind(self) -> pathlib.Path:
        return self.repo_root / ".github" / "workflows" / "scripts" / "engine_bind.py"

    @property
    def deploy_common(self) -> pathlib.Path:
        return self.repo_root / "scripts" / "lib" / "deploy-common.sh"


# ------------------------------------------------------------ the env reader

class ProbeCallRefused(Exception):
    """A probe was called with too little to build an Environment.

    Fixed text, for the same reason EnvReadRefused carries fixed text: a caller
    may render an exception's message, and nothing that reaches a public log
    this way may have come out of a file or an argument.

    A probe that raises this REFUSES -- `run_probe` turns it into
    `probe-raised`, and production_truth.call_probe turns it into a reading that
    was not performed. Neither is a pass.
    """


class EnvReadRefused(Exception):
    """Raised with a FIXED sentence. It never carries file content.

    Every message this class is constructed with is a literal in this file.
    That is deliberate: the CLI's exception path prints an exception's class
    name and nothing else, and this keeps the class honest even if a future
    caller decides to print the message too.
    """


def read_env_keys(path: pathlib.Path, keys: Iterable[str]) -> dict[str, str]:
    """The value of each NAMED key in an env file, by exact match.

    Not a parser for the file: a reader for a handful of keys out of it. The
    file is never sourced, never returned, never logged. A key that is not on
    READABLE_KEYS is refused before the file is even opened, so a future caller
    cannot quietly widen what this process holds in memory.

    First occurrence wins, which is what pipeline.yml's verify step already
    does (`grep -m1 '^MAIN_MODEL='`).
    """
    wanted = list(keys)
    if not set(wanted) <= READABLE_KEYS:
        raise EnvReadRefused("a key that is not on the read allowlist was requested")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Deliberately not `from exc` and deliberately without the path: an
        # OSError's string carries the filename, and this exception may be
        # rendered by a caller that forgets.
        raise EnvReadRefused("the environment file could not be read") from None
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key not in wanted or key in found:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        found[key] = value
    return found


# ------------------------------------------------- the second-consumer surface

#: WHAT THIS LIBRARY OFFERS A CONSUMER THAT IS NOT box_readiness.py, and the
#: question each entry answers. The contract, in one place, so reconciling it
#: with production_truth.REQUIRED_PROBES is a diff and not archaeology.
#:
#: Two properties make an entry here different from any other function in this
#: file, and tests/test_box_probes.py asserts both for every one of them:
#:
#:   1. IT IS CALLABLE WITHOUT AN `Environment`. production_truth.call_probe
#:      passes only the context keys a signature declares, and REFUSES a
#:      required parameter it cannot supply; it offers `deploy_root` and
#:      `timeout`. Every probe here therefore takes `env` with a default and
#:      accepts those two by keyword. This was the real defect behind the
#:      contract mismatch: this file's header has claimed since its first commit
#:      to be "the shared library behind two jobs", while no probe in it was
#:      callable by anything that did not already hold an Environment -- and
#:      box_readiness.py is the only thing that builds one. Measured against the
#:      sibling branch's own loader on 2026-09-28: probe_engine_exposure raised
#:      `ProbeContractError: box_probes.probe_engine_exposure() requires a
#:      parameter this watch cannot supply: 'env'. It offers deploy_root,
#:      timeout.`
#:   2. ITS RESULT IS READABLE BY production_truth.normalise(): `ok` is a
#:      bool property, `could_run` says whether the reading was taken at all,
#:      and `detail` is ONE allowlisted line rather than a report.
#:
#: NOT OFFERED, and deliberately: `probe_container_states` and
#: `probe_host_guard_unit`. production_truth.REQUIRED_PROBES names them, and
#: they are not box-readiness probes -- a pre-deploy gate asks the seven in
#: ALL_PROBES; a restart loop and a boot unit are a standing monitor's
#: questions. production_truth.py's own header states where they belong:
#: "implement it HERE or hand it back to that track -- never to edit their
#: file." Implementing them in this file would mean shipping two probes that
#: nothing on this branch runs and no test on this branch can exercise against
#: the box, with a container expectation invented here rather than derived.
SECOND_CONSUMER_PROBES: Mapping[str, str] = {
    "probe_engine_exposure": (
        "is the unauthenticated engine port unreachable from every non-cluster address"
    ),
    "probe_real_completion": (
        "did a real completion advance vllm:generation_tokens_total (never /health)"
    ),
}


def _env_for(
    env: Environment | None,
    deploy_root: pathlib.Path | str | None,
    timeout: float | None,
) -> Environment:
    """The Environment a probe runs against, built if the caller has none.

    `repo_root` is derived from this file's own path, which is correct for the
    second consumer by construction: production_truth.py imports this module,
    so it is sitting in the same checkout of the same repository.
    """
    if env is not None:
        return env if timeout is None else dataclasses.replace(env, timeout_cap=timeout)
    if deploy_root is None:
        raise ProbeCallRefused(
            "a probe was called with neither an Environment nor a deploy root"
        )
    return Environment(
        deploy_root=pathlib.Path(deploy_root),
        repo_root=pathlib.Path(__file__).resolve().parents[3],
        ref="",
        timeout_cap=timeout,
    )


# -------------------------------------------------------------------- probes

def probe_deploy_root(env: Environment) -> ProbeResult:
    """Is the deploy root a clean checkout of the default branch?

    The DIRTY-TREE half is the precondition scripts/deploy.sh enforces
    (deploy.sh:189 reads `git status --porcelain --untracked-files=no` and
    aborts on any output). It is the most likely refusal of the seven: the
    deploy root is a SHARED working tree that several sessions write to at once,
    and a dirty tree blocks the deploy after the full CI has already run.

    The BRANCH half is reported and never refused -- see the comment on
    PASSING["deploy-root"] for why a detached production checkout is a state the
    release path creates on purpose and recovers from by itself.

    `--no-optional-locks` ON EVERY CALL. Without it `git status` refreshes the
    index: measured on git 2.43.0 here on 2026-09-27, a plain
    `git status --porcelain --untracked-files=no` against a stale index opens
    `.git/index.lock` (O_CREAT|O_EXCL) and rewrites `.git/index`, and the flag
    removes both. The rev-parse calls never took the lock in that measurement,
    and carry the flag anyway so that "this probe does not write to the shared
    deploy root" is a property of the whole function rather than of one call.
    """
    git = ["git", "--no-optional-locks", "-C", str(env.deploy_root)]
    inside = env.runner.run(
        git + ["rev-parse", "--is-inside-work-tree"],
        timeout=env.budget("git"),
    )
    if inside.rc != 0 or inside.stdout.strip() != "true":
        return ProbeResult("deploy-root", "not-a-checkout", {"exit_code": inside.rc})

    status = env.runner.run(
        git + ["status", "--porcelain", "--untracked-files=no"],
        timeout=env.budget("git"),
    )
    if status.rc != 0:
        return ProbeResult("deploy-root", "git-unreadable", {"exit_code": status.rc})
    dirty = [line for line in status.stdout.splitlines() if line.strip()]

    branch = env.runner.run(
        git + ["rev-parse", "--abbrev-ref", "HEAD"],
        timeout=env.budget("git"),
    )
    if branch.rc != 0:
        return ProbeResult("deploy-root", "git-unreadable", {"exit_code": branch.rc})
    on_default = branch.stdout.strip() == env.default_branch

    facts = {"dirty_files": len(dirty), "on_default_branch": on_default}
    if dirty:
        # The file NAMES are not printed. They are paths in a private working
        # tree, the remedy prints the command that shows them, and a path is
        # not something a public log needs.
        return ProbeResult("deploy-root", "dirty-tree", facts)
    if not on_default:
        # A PASSING verdict: reported in the table, never a refusal. The branch
        # NAME is still not printed - `on_default_branch no` is the fact, and
        # the remedy command is what shows which branch it is.
        return ProbeResult("deploy-root", "wrong-branch", facts)
    return ProbeResult("deploy-root", "clean-on-default", facts)


def probe_disk(env: Environment) -> ProbeResult:
    """At least DISK_FLOOR_GB free on the deploy root's filesystem."""
    out = env.runner.run(
        ["df", "-BG", "--output=avail", str(env.deploy_root)],
        timeout=env.budget("disk"),
    )
    if out.rc != 0:
        return ProbeResult("disk", "unreadable", {"exit_code": out.rc})
    lines = [line for line in out.stdout.splitlines() if line.strip()]
    digits = re.sub(r"[^0-9]", "", lines[-1]) if lines else ""
    if not digits:
        return ProbeResult("disk", "unreadable", {"floor_gb": DISK_FLOOR_GB})
    free_gb = int(digits)
    facts = {"free_gb": free_gb, "floor_gb": DISK_FLOOR_GB}
    return ProbeResult("disk", "ok" if free_gb >= DISK_FLOOR_GB else "below-floor", facts)


def _holder_facts(env: Environment) -> dict[str, object]:
    """Who holds the deploy lock, from the metadata the holder wrote.

    Only four fields, and every one of them is rendered through the print
    allowlist: the pid, the KIND of origin, the short sha the holder is on and
    how long it has been held. Not the hostname (engine_bind.py classes a
    hostname as an address) and not the actor.
    """
    facts: dict[str, object] = {}
    try:
        text = env.holder_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return facts
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    pid = fields.get("pid", "")
    if pid.isdigit():
        facts["holder_pid"] = int(pid)
    origin = fields.get("origin", "")
    if origin.startswith("github-actions"):
        facts["holder_origin"] = "github-actions"
    elif origin:
        facts["holder_origin"] = "manual-shell"
    else:
        facts["holder_origin"] = "unknown"
    head = fields.get("head", "")
    if _SHA_RE.match(head):
        facts["holder_head"] = head
    started = fields.get("started_at", "")
    try:
        held_since = time.strptime(started, "%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        pass
    else:
        import calendar

        facts["held_for_s"] = max(0.0, env.clock() - calendar.timegm(held_since))
    return facts


def probe_deploy_lock(env: Environment) -> ProbeResult:
    """Is the deploy flock FREE? Asked the way deploy-common.sh asks it.

    `flock -n` on a descriptor that is closed the instant the subshell exits.
    This DOES take the lock for that instant -- see the module docstring, where
    the claim that it never does is withdrawn -- and the reason that is safe is
    that the subshell does nothing else, never waits, and never acts on it.

    The descriptor is opened READ-ONLY (`exec 9<`), which is stricter
    than deploy-common.sh's own `dr_lock_is_held` (`exec 9>>`): flock(2) does
    not need write access, and a readiness check has no business opening a
    production file for writing. The lock file's absence is NOT a refusal —
    that is the first-deploy state, and it is what pipeline.yml's deploy
    preflight already reports as "not created yet".

    HELD IS A REFUSAL. Two deploys must never overlap, and this probe is also
    what keeps `box-readiness` honest about never running during a rollout
    without it having to join the release path's concurrency group.
    """
    if not env.lock_file.exists():
        return ProbeResult("deploy-lock", "never-created")
    probe = env.runner.run(
        ["bash", "-c", 'exec 9<"$1" || exit 3; flock -n 9', "_", str(env.lock_file)],
        timeout=env.budget("lock"),
    )
    if probe.rc == 0:
        return ProbeResult("deploy-lock", "free")
    if probe.rc == 1:
        return ProbeResult("deploy-lock", "held", _holder_facts(env))
    return ProbeResult("deploy-lock", "unreadable", {"exit_code": probe.rc})


def probe_engine_exposure(
    env: Environment | None = None,
    *,
    deploy_root: pathlib.Path | str | None = None,
    timeout: float | None = None,
) -> ProbeResult:
    """The unauthenticated engine port is unreachable from outside the cluster.

    ON THE SECOND_CONSUMER_PROBES SURFACE, hence the keyword arguments: the
    standing watch calls this with a deploy root and a timeout and holds no
    Environment. box-readiness passes `env` positionally as every other probe
    does. See SECOND_CONSUMER_PROBES.

    engine_bind.py is REUSED as a subprocess rather than reimplemented. Its own
    header states that it prints roles, counts, interface CLASSES and address
    families and never an address, a hostname or the ssh target; its output is
    shown here under that contract, and passed through `sanitize` anyway.

    An `ACCEPTED` line is a refusal even if the exit code is zero. That
    combination should be impossible, which is exactly why it is worth
    checking: a gate that trusts one signal cannot notice when it stops
    meaning what it meant.

    THE COUNT IS THE PER-ADDRESS LINES, and that is the fix for a wrong number
    this probe used to print. engine_bind.py emits the phrase "ACCEPTED the
    connection" TWICE per exposed run: once per address, in its own indented
    detail line (engine_bind.py:879), and once more in the summary that
    `report.fail` renders as "- FAIL: N non-cluster address ... ACCEPTED the
    connection: ..." (engine_bind.py:896 through Report.fail at :647-649), which
    main() prints at :988. Counting the bare phrase therefore reported one
    address too many. Measured on 2026-09-27 by driving the real
    `engine_bind.evaluate` through tests/test_engine_bind.check():

        1 accepting address -> bare phrase 2, per-address lines 1
        2 accepting addresses -> bare phrase 3, per-address lines 2

    The verdict was never wrong -- it fails closed on any match -- but
    `accepted_addresses` is the one number this probe exists to report, and it
    is read at 3 a.m.

    Narrowing a pattern can turn a gate blind, so the bare phrase is kept as a
    TRIP-WIRE: if engine_bind ever reports an acceptance in a shape the
    per-address pattern does not match, this still refuses, and it omits the
    count rather than guessing one.
    """
    env = _env_for(env, deploy_root, timeout)
    out = env.runner.run(
        [
            "python3",
            str(env.engine_bind),
            "check",
            "--generated-env",
            str(env.generated_env),
        ],
        timeout=env.budget("exposure"),
    )
    text = (out.stdout or "") + (out.stderr or "")
    report = [line for line in text.splitlines() if line.strip()][:20]
    accepted = len(_ACCEPTED_ADDRESS_RE.findall(text))
    if accepted:
        return ProbeResult(
            "engine-exposure",
            "exposed",
            {"accepted_addresses": accepted, "exit_code": out.rc},
            report,
        )
    if _ACCEPTED_PHRASE_RE.search(text):
        # engine_bind reported an acceptance in a shape this probe cannot count.
        # Refuse anyway - a gate that has lost track of the report it reads must
        # never answer `closed` - and report no count rather than a made-up one.
        # The detail lines carry the real report, which is what the operator
        # needs in this case.
        return ProbeResult("engine-exposure", "exposed", {"exit_code": out.rc}, report)
    if out.rc == 0:
        return ProbeResult("engine-exposure", "closed", {"exit_code": 0}, report)
    return ProbeResult("engine-exposure", "unproven", {"exit_code": out.rc}, report)


def probe_engine_controller(env: Environment) -> ProbeResult:
    """What the engine controller says about itself.

    /state is the controller's own view (contract section 6, the document the
    orchestrator and Grafana read). It is NOT a completion probe and is not
    treated as one: that is the next probe, and the reason it exists.
    """
    out = env.runner.run(
        ["curl", "-fsS", "-m", "5", env.controller_url + "/state"],
        timeout=env.budget("controller"),
    )
    if out.rc != 0:
        return ProbeResult("engine-controller", "unreachable", {"exit_code": out.rc})
    try:
        doc = json.loads(out.stdout)
    except (json.JSONDecodeError, TypeError):
        return ProbeResult("engine-controller", "unreadable")
    if not isinstance(doc, dict):
        return ProbeResult("engine-controller", "unreadable")

    # A `recovery` that is present but is not an object is a document this
    # probe cannot read, and "cannot read it" is a refusal, not a shrug. The
    # first version treated it as "no recovery in progress", which is the one
    # reading that lets a box mid-recovery pass: `in_progress` would silently
    # be False and a READY/BUSY state_code would carry the probe.
    recovery = doc.get("recovery")
    if recovery is None:
        recovery = {}
    if not isinstance(recovery, dict):
        return ProbeResult("engine-controller", "unreadable")
    facts: dict[str, object] = {}
    state = doc.get("state")
    if isinstance(state, str):
        facts["engine_state"] = state
    code = doc.get("state_code")
    if isinstance(code, int) and not isinstance(code, bool):
        facts["state_code"] = code
    facts["primary_ready"] = bool(doc.get("primary_ready"))
    in_progress = bool(recovery.get("in_progress"))
    facts["recovery_in_progress"] = in_progress

    if in_progress:
        return ProbeResult("engine-controller", "recovering", facts)
    if code in CONTROLLER_SERVING_CODES and facts["primary_ready"]:
        return ProbeResult("engine-controller", "ready", facts)
    return ProbeResult("engine-controller", "not-ready", facts)


def _generation_tokens(text: str) -> float | None:
    """Sum every `vllm:generation_tokens_total` series in a /metrics body.

    None means the body did not carry a readable counter, which is a refusal.

    The Prometheus text format permits `NaN`, `+Inf` and `-Inf` as sample
    values, and `float()` accepts all three. A NaN total would make the wedged
    test (`after <= before`) FALSE whatever the engine did, because every
    comparison against NaN is false -- so a NaN counter would report
    `generated` on a box that generated nothing. It happens that `int(nan)`
    raises further down and the probe refuses anyway, but a fail-closed default
    that depends on an unrelated line raising is not one anybody can rely on.
    A non-finite sample is therefore rejected here, where the reading is taken.
    """
    total = None
    for line in text.splitlines():
        if not line.startswith("vllm:generation_tokens_total"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            value = float(parts[-1])
        except ValueError:
            continue
        if value != value or value in (float("inf"), float("-inf")):
            return None
        total = value if total is None else total + value
    return total


def probe_real_completion(
    env: Environment | None = None,
    *,
    deploy_root: pathlib.Path | str | None = None,
    timeout: float | None = None,
) -> ProbeResult:
    """A REAL completion, and the token counter must advance.

    ON THE SECOND_CONSUMER_PROBES SURFACE, hence the keyword arguments and
    hence the name: `probe_real_completion` is what
    production_truth.REQUIRED_PROBES asks for, and `probe_completion` below is
    the same function under the name box-readiness's own table uses. See
    SECOND_CONSUMER_PROBES.

    NEVER /health. A wedged vLLM engine served a green /health for five and a
    half hours on this box while generating nothing; /health, /v1/models and
    the controller's own API signals all stayed green throughout, and the only
    difference visible from outside was `vllm:generation_tokens_total` refusing
    to move. That is the single reading this probe exists to take, and the
    reason a reply alone is not enough to pass it.

    The engine address is RESOLVED, not assumed: 127.0.0.1:8000 answers
    identically whether the unauthenticated API is on loopback or on every
    interface, and it stops answering at all once the launcher binds the
    cluster head elsewhere. engine_bind.py resolve reads the configured bind
    and hands back a URL, which is used as an argument and never printed.

    ONE tiny generation. The main model is TP=2 across both nodes, so this
    costs both of them, and it runs beside live chat.

    THE REPLY IS READ FROM BOTH `content` AND `reasoning_content`. Thinking is
    asked OFF, but `chat_template_kwargs.enable_thinking` is honoured by the
    chat TEMPLATE, not by the server: a template that ignores it puts the eight
    tokens in `reasoning_content` and leaves `content` empty. The counter would
    advance, the box would be healthy, and reading `content` alone would report
    `empty-reply` and refuse the deploy. Both are counted, and each is reported
    as its own number so the log says which one carried the answer.

    HOW THIS DIFFERS FROM `verify`'s "The model actually generates" STEP, stated
    exactly, because the first version of this note claimed the two were
    "character-for-character" the same probe and they were not:

      * SAME, since 2026-09-28: the curl deadline (`-m 180`; it was 120 here and
        180 there, so a slow prefill could refuse pre-deploy and pass
        post-deploy), the prompt, max_tokens, temperature,
        `chat_template_kwargs.enable_thinking`, reading the reply from `content`
        or `reasoning_content`, and the check itself -- the GLOBAL
        `vllm:generation_tokens_total` must advance.
      * DIFFERENT, on purpose: `_generation_tokens` refuses a `NaN`, `+Inf` or
        `-Inf` sample, where verify's `awk '/^vllm:generation_tokens_total/
        {s+=$2}'` adds it and would pass. The divergence is in the strict
        direction and is in this file's own reading, so it cannot make this
        probe refuse something verify accepts.

    STILL TRUE OF BOTH: the counter is global, so under concurrent live traffic
    it can advance for someone else's request; `usage.completion_tokens` on the
    response is the per-request number, and scripts/cluster-verify-engine.sh
    uses it. Worth changing, and worth changing in BOTH -- which is now a real
    argument rather than the cover it was while the two deadlines differed.
    """
    import tempfile

    env = _env_for(env, deploy_root, timeout)
    deadline = env.budget("completion")

    env_file = env.generated_env
    try:
        model = read_env_keys(env_file, ["MAIN_MODEL"]).get("MAIN_MODEL", "")
    except EnvReadRefused:
        model = ""
    if not model:
        return ProbeResult("completion", "model-unknown")

    with tempfile.TemporaryDirectory() as tmp:
        sink = pathlib.Path(tmp) / "resolved"
        sink.touch()
        resolved = env.runner.run(
            [
                "python3",
                str(env.engine_bind),
                "resolve",
                "--generated-env",
                str(env_file),
                "--github-output",
                str(sink),
            ],
            timeout=env.budget("controller"),
        )
        # resolve's STDOUT is deliberately dropped. Under GITHUB_ACTIONS it
        # emits a `::add-mask::` workflow command carrying the engine host,
        # and a captured workflow command is not consumed by the runner — it
        # would just be an address in a variable.
        if resolved.rc != 0:
            return ProbeResult("completion", "unreachable", {"exit_code": resolved.rc})
        url = ""
        for line in sink.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == "url":
                url = value.strip()
    if not url:
        return ProbeResult("completion", "unreachable")

    def counter() -> float | None:
        out = env.runner.run(
            ["curl", "-fsS", "-m", "15", url + "/metrics"],
            timeout=env.budget("controller"),
        )
        return _generation_tokens(out.stdout) if out.rc == 0 else None

    started = env.clock()
    before = counter()
    if before is None:
        return ProbeResult("completion", "metrics-unreadable")

    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": COMPLETION_PROMPT}],
            "max_tokens": COMPLETION_MAX_TOKENS,
            "temperature": 0,
            # Thinking off: a reasoning pass would spend tokens and wall clock
            # on a probe whose only question is "does the engine generate".
            "chat_template_kwargs": {"enable_thinking": False},
        }
    )
    reply_out = env.runner.run(
        [
            "curl", "-fsS", "-m", str(int(deadline)),
            "-H", "Content-Type: application/json",
            "-d", body,
            url + "/v1/chat/completions",
        ],
        timeout=deadline + COMPLETION_CURL_GRACE_S,
    )
    elapsed = max(0.0, env.clock() - started)
    if reply_out.rc != 0:
        return ProbeResult("completion", "unreachable", {"exit_code": reply_out.rc, "elapsed_s": elapsed})
    try:
        message = json.loads(reply_out.stdout)["choices"][0]["message"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        message = None
    if not isinstance(message, Mapping):
        message = {}
    content = str(message.get("content") or "").strip()
    reasoning = str(message.get("reasoning_content") or "").strip()

    after = counter()
    if after is None:
        return ProbeResult("completion", "metrics-unreadable", {"elapsed_s": elapsed})

    # The counters are reported as COUNTS, never as the model's words. The
    # reply itself is model output; its LENGTH is all this check needs.
    facts: dict[str, object] = {
        "tokens_before": int(before),
        "tokens_after": int(after),
        "reply_chars": len(content),
        "reasoning_chars": len(reasoning),
        "elapsed_s": elapsed,
    }
    if not content and not reasoning:
        return ProbeResult("completion", "empty-reply", facts)
    if after <= before:
        return ProbeResult("completion", "wedged", facts)
    return ProbeResult("completion", "generated", facts)


#: The same function under the name box-readiness's table uses. ALL_PROBES and
#: PROBE_VERDICTS key this probe as "completion", so the CLI-facing name stays
#: `probe_completion`; SECOND_CONSUMER_PROBES needs `probe_real_completion`.
#: One function, two names, so the two consumers cannot drift apart into two
#: implementations of "ask the engine to generate".
probe_completion = probe_real_completion


def _schema_version(
    env: Environment,
    root: pathlib.Path,
    function: str,
    *args: str,
) -> int | None:
    """One number out of deploy-common.sh, with DR_ROOT pointed where asked.

    `function` is a literal in THIS file; everything variable arrives as a
    positional argument, so the commit sha is data the shell never parses.
    The first version interpolated the ref into the script text
    (`f'dr_code_schema_version_from_git "{env.ref}"'`). Today's ref is
    `$GITHUB_SHA` and is forty hex digits, so nothing was exploitable -- but
    the shape is the one where a future caller passing `--ref` from somewhere
    less disciplined gets a shell injection for free, and the fix costs a
    positional parameter.
    """
    if not function.replace("_", "").isalnum():
        # NOT an `assert`. `python -O` strips asserts, and this is the only
        # check on the one identifier this function interpolates into a shell
        # script -- everything else arrives as a positional parameter the shell
        # never parses. The workflow runs plain `python3`, so the assert held
        # today; a future caller under -O would have lost it silently, which is
        # the class of "the gate is still there but it stopped checking" this
        # whole branch exists to close.
        raise ProbeCallRefused("the shell function name is not an identifier")
    out = env.runner.run(
        [
            "bash",
            "-c",
            f'. "$1" || exit 9; shift; {function} "$@"',
            "_",
            str(env.deploy_common),
            *args,
        ],
        timeout=env.budget("migrations"),
        env={"TECHSARA_DEPLOY_ROOT": str(root)},
    )
    if out.rc != 0:
        return None
    digits = out.stdout.strip()
    return int(digits) if digits.isdigit() else None


def probe_migrations(env: Environment) -> ProbeResult:
    """The incoming commit's migrations against what the database has applied.

    Read with the helpers scripts/lib/deploy-common.sh already exposes:
    `dr_live_schema_version` (the orchestrator's /health, falling back to
    `docker exec <production postgres> psql` -- deploy-common.sh:274-275 -- which
    matters precisely when the orchestrator is the thing that is down) and
    `dr_code_schema_version_from_git`.

    It therefore cannot disagree with deploy.sh about the NUMBERS. It is allowed
    to disagree about the DECISION, and the earlier wording here claimed
    otherwise. Where it disagrees, deploy.sh wins and this probe is aligned to
    it -- see the comment on PASSING["migrations"]. `behind` is the one verdict
    where they agree to refuse.

    The two calls point DR_ROOT at different trees ON PURPOSE. The live version
    belongs to the deploy root; the code version belongs to the commit being
    deployed, which is checked out in the runner workspace and is not
    necessarily in the deploy root's object store at all.

    BEHIND is a refusal. Migrations in orchestrator/app/db.py only go forward,
    so deploying code that knows fewer of them than the database has applied is
    old code in front of a newer schema; deploy.sh refuses it, and catching it
    here saves the hour of CI that would otherwise run first.
    """
    live = _schema_version(env, env.deploy_root, "dr_live_schema_version")
    code = _schema_version(env, env.repo_root, "dr_code_schema_version_from_git", env.ref)
    if live is None or code is None:
        facts: dict[str, object] = {}
        if live is not None:
            facts["live_schema"] = live
        if code is not None:
            facts["code_schema"] = code
        return ProbeResult("migrations", "unreadable", facts)
    facts = {"live_schema": live, "code_schema": code}
    if code > live:
        facts["forward_n"] = code - live
        return ProbeResult("migrations", "forward", facts)
    if code == live:
        return ProbeResult("migrations", "equal", facts)
    return ProbeResult("migrations", "behind", facts)


#: The order they run in, and the order they are reported in. Cheap and most
#: likely to refuse first: the shared deploy root is the one several sessions
#: write to, and there is no point spending a GPU generation to find out that
#: the tree is dirty.
ALL_PROBES: tuple[tuple[str, Callable[[Environment], ProbeResult]], ...] = (
    ("deploy-root", probe_deploy_root),
    ("disk", probe_disk),
    ("deploy-lock", probe_deploy_lock),
    ("engine-exposure", probe_engine_exposure),
    ("engine-controller", probe_engine_controller),
    ("completion", probe_completion),
    ("migrations", probe_migrations),
)


def run_probe(env: Environment, name: str, fn: Callable[[Environment], ProbeResult]) -> ProbeResult:
    """Run one probe. A probe that raises or times out REFUSES.

    The exception's class name is reported; its message is not, on any branch.
    An exception raised while reading a file carries that file's name, and a
    value read out of a file can be anything at all — `str(exc)` is how such a
    value reaches a public log without anyone having decided that it should.
    """
    try:
        result = fn(env)
    except subprocess.TimeoutExpired:
        return ProbeResult(name, "probe-timed-out")
    except BaseException as exc:  # noqa: BLE001 - a refusal, whatever went wrong
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return ProbeResult(name, "probe-raised", {"exception_type": type(exc).__name__})
    if not isinstance(result, ProbeResult):
        return ProbeResult(name, "probe-raised")
    return result


def run_all(env: Environment) -> list[ProbeResult]:
    return [run_probe(env, name, fn) for name, fn in ALL_PROBES]


# ------------------------------------------------------------------ remedies

def _remedies(env: Environment) -> Mapping[tuple[str, str], tuple[str, ...]]:
    """One entry per REFUSING (probe, verdict) pair, and no others.

    tests/test_box_probes.py asserts that set equality against
    PROBE_VERDICTS minus PASSING, in both directions. Both directions matter:
    a missing entry is a refusal with no command behind it, and a SPARE entry is
    text that can never print, which is what ("engine-exposure", "unavailable")
    was -- a remedy for a verdict no probe can report, sitting in the table
    making a vacuous coverage test look like coverage.
    """
    root = str(env.deploy_root)
    repo = str(env.repo_root)
    return {
        ("deploy-root", "dirty-tree"): (
            f"git -C {root} status --porcelain --untracked-files=no",
            f"git -C {root} diff",
        ),
        ("deploy-root", "not-a-checkout"): (f"git -C {root} status", f"ls -la {root}"),
        ("deploy-root", "git-unreadable"): (f"git -C {root} status",),
        ("disk", "below-floor"): (
            f"df -BG --output=avail {root}",
            "docker system df",
            f"du -xh --max-depth=1 {root} | sort -h | tail -20",
        ),
        ("disk", "unreadable"): (f"df -BG --output=avail {root}",),
        ("deploy-lock", "held"): (f"{root}/scripts/deploy-lock.sh --status",),
        ("deploy-lock", "unreadable"): (
            f"ls -l {root}/.runtime/locks/",
            f"{root}/scripts/deploy-lock.sh --status",
        ),
        ("engine-exposure", "exposed"): (
            "sudo systemctl status techsara-host-guard.service",
            "sudo systemctl restart techsara-host-guard.service",
            f"sudo {root}/scripts/host-guard.sh status",
        ),
        ("engine-exposure", "unproven"): (
            f"python3 {repo}/.github/workflows/scripts/engine_bind.py check "
            f"--generated-env {root}/.runtime/generated.env",
            "sudo systemctl status techsara-host-guard.service",
        ),
        ("engine-controller", "unreachable"): (
            "docker ps --filter name=sf-local-ai-engine-controller-1 --format '{{.Names}} {{.Status}}'",
            f"{root}/scripts/cluster-status.sh",
        ),
        ("engine-controller", "unreadable"): (f"{root}/scripts/cluster-status.sh",),
        ("engine-controller", "recovering"): (f"{root}/scripts/cluster-status.sh",),
        ("completion", "wedged"): (
            f"{root}/scripts/cluster-verify-engine.sh",
            f"{root}/scripts/cluster-recover.sh",
        ),
        ("completion", "empty-reply"): (f"{root}/scripts/cluster-verify-engine.sh",),
        ("completion", "unreachable"): (
            "docker ps --filter name=sf-local-ai-vllm-1 --format '{{.Names}} {{.Status}}'",
            f"{root}/scripts/cluster-status.sh",
        ),
        ("completion", "metrics-unreadable"): (f"{root}/scripts/cluster-status.sh",),
        ("completion", "model-unknown"): (f"cd {root} && ./techsara redetect",),
        ("migrations", "behind"): (
            f"bash -c '. {root}/scripts/lib/deploy-common.sh; dr_live_schema_version'",
            f"git -C {root} log --oneline -5 -- orchestrator/app/db.py",
        ),
    }


#: Sentences that are worth saying next to the command. Fixed text, chosen in
#: this file; no value from a probe is ever interpolated into one.
NOTES: Mapping[tuple[str, str], str] = {
    ("deploy-root", "dirty-tree"): (
        "The deploy root is a SHARED working tree - several sessions write to it. "
        "Ask before discarding anything, and never run `git stash`, `git reset --hard`, "
        "`git clean -fd` or `git checkout -- .` there: a stale checkout also empties "
        "running containers' bind mounts."
    ),
    ("disk", "below-floor"): (
        "Do NOT prune images before reading .runtime/releases/: the previous release's "
        "image ids are what an automatic rollback restores."
    ),
    ("deploy-lock", "held"): (
        "A deploy or a lock-wrapped command is in flight. Wait for it - do not force it."
    ),
    ("engine-exposure", "exposed"): (
        "NEEDS ROOT. The host packet filter is what closes the unauthenticated engine "
        "port; a reboot without the boot unit loses it, which is exactly how it was lost "
        "on 2026-09-21. Hand these commands to someone who has root; this job cannot run them."
    ),
    ("engine-controller", "recovering"): (
        "An engine recovery is already running. Let it finish - a deploy on top of one is "
        "two things moving the same containers."
    ),
    ("completion", "wedged"): (
        "The API answered and the engine did not generate. This is the wedged-engine shape: "
        "/health stays green for hours. Recovery RESTARTS THE ENGINE - only ever do that for "
        "the engine itself, never to fix something else, and never as part of a routine deploy."
    ),
    ("migrations", "behind"): (
        "The commit being deployed knows fewer migrations than the database has applied. "
        "Migrations only go forward, so this is old code in front of a newer schema; "
        "deploy.sh refuses it too."
    ),
}


def remedy_for(env: Environment, result: ProbeResult) -> tuple[str, ...]:
    """The exact command(s) a human runs to clear this refusal.

    Never abbreviated, never a placeholder. A readiness failure that only moves
    the 3 a.m. guessing an hour earlier has bought nothing.
    """
    table = _remedies(env)
    key = (result.probe, result.verdict)
    if key in table:
        return table[key]
    # The UNIVERSAL_VERDICTS, which belong to `run_probe` rather than to any one
    # probe and cannot have a probe-specific remedy. This fallback is also why
    # the coverage test in tests/test_box_probes.py asserts the KEY is in
    # `_remedies(env)` rather than asserting that `remedy_for` returned
    # something: asking this function was a test that could not fail.
    return (
        f"python3 {env.repo_root}/.github/workflows/scripts/box_readiness.py "
        f"--deploy-root {env.deploy_root} --ref {env.ref}",
    )


def note_for(result: ProbeResult) -> str:
    return NOTES.get((result.probe, result.verdict), "")
