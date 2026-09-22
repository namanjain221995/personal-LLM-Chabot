#!/usr/bin/env python3
"""Read-only probes of the production box, written to be SAFE TO PRINT.

WHAT THIS IS
------------
The shared library behind two jobs: `box-readiness` in pipeline.yml (the
pre-deploy read, before the release path touches anything) and, later, the
standing watch in production-watch.yml. Both ask the same question — "is this
machine in a state where a deploy would succeed?" — and both answer it from a
PUBLIC repository's log, so both need the same two properties:

  1. EVERY PROBE IS READ-ONLY. Nothing here starts, stops, recreates, prunes,
     writes or locks. The deploy flock in particular is OBSERVED, never taken:
     asking "is it free?" and taking it are different acts, and only the first
     one is safe to do next to a rollout.

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
inherits it by construction instead of by remembering.

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

#: Per-probe subprocess ceilings, in seconds. Their sum plus the checkout has
#: to stay inside the job's 8-minute ceiling even when every one of them times
#: out: 30 + 30 + 15 + 150 + 15 + 120 + 45 = 405s.
TIMEOUTS = {
    "git": 30,
    "disk": 30,
    "lock": 15,
    "exposure": 150,
    "controller": 15,
    "completion": 120,
    "migrations": 45,
}

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
        "closed", "exposed", "unproven", "unavailable",
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

#: The verdicts that let a probe pass. EVERYTHING ELSE IS A REFUSAL — including
#: any verdict added later and forgotten here, which is the direction a
#: fail-closed default has to lean.
PASSING: Mapping[str, frozenset] = {
    "deploy-root": frozenset({"clean-on-default"}),
    "disk": frozenset({"ok"}),
    # A lock file that was never created is the first-deploy state, and it is
    # what the deploy's own preflight already accepts. `held` is a refusal: a
    # second deploy must not start while one is in flight, and refusing here
    # is also what keeps this job honest without sharing the release path's
    # concurrency group.
    "deploy-lock": frozenset({"free", "never-created"}),
    "engine-exposure": frozenset({"closed"}),
    "engine-controller": frozenset({"ready"}),
    "completion": frozenset({"generated"}),
    "migrations": frozenset({"equal", "forward"}),
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
    detail: Sequence[str] = ()

    @property
    def ok(self) -> bool:
        """Fail closed: a verdict nobody listed as passing is a refusal."""
        return self.verdict in PASSING.get(self.probe, frozenset())

    @property
    def safe_verdict(self) -> str:
        return self.verdict if self.verdict in VERDICTS else WITHHELD


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


# -------------------------------------------------------------------- probes

def probe_deploy_root(env: Environment) -> ProbeResult:
    """Is the deploy root a clean checkout of the default branch?

    The same two preconditions scripts/deploy.sh enforces (deploy.sh:189 reads
    `git status --porcelain --untracked-files=no` and aborts on any output).
    This is the most likely refusal of the seven: the deploy root is a SHARED
    working tree that several sessions write to at once, and a dirty tree
    blocks the deploy after the full CI has already run.
    """
    inside = env.runner.run(
        ["git", "-C", str(env.deploy_root), "rev-parse", "--is-inside-work-tree"],
        timeout=TIMEOUTS["git"],
    )
    if inside.rc != 0 or inside.stdout.strip() != "true":
        return ProbeResult("deploy-root", "not-a-checkout", {"exit_code": inside.rc})

    status = env.runner.run(
        ["git", "-C", str(env.deploy_root), "status", "--porcelain", "--untracked-files=no"],
        timeout=TIMEOUTS["git"],
    )
    if status.rc != 0:
        return ProbeResult("deploy-root", "git-unreadable", {"exit_code": status.rc})
    dirty = [line for line in status.stdout.splitlines() if line.strip()]

    branch = env.runner.run(
        ["git", "-C", str(env.deploy_root), "rev-parse", "--abbrev-ref", "HEAD"],
        timeout=TIMEOUTS["git"],
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
        return ProbeResult("deploy-root", "wrong-branch", facts)
    return ProbeResult("deploy-root", "clean-on-default", facts)


def probe_disk(env: Environment) -> ProbeResult:
    """At least DISK_FLOOR_GB free on the deploy root's filesystem."""
    out = env.runner.run(
        ["df", "-BG", "--output=avail", str(env.deploy_root)],
        timeout=TIMEOUTS["disk"],
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
    """Is the deploy flock FREE? Observed, never taken.

    `flock -n` on a duplicate descriptor, released the instant the subshell
    exits. The descriptor is opened READ-ONLY (`exec 9<`), which is stricter
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
        timeout=TIMEOUTS["lock"],
    )
    if probe.rc == 0:
        return ProbeResult("deploy-lock", "free")
    if probe.rc == 1:
        return ProbeResult("deploy-lock", "held", _holder_facts(env))
    return ProbeResult("deploy-lock", "unreadable", {"exit_code": probe.rc})


def probe_engine_exposure(env: Environment) -> ProbeResult:
    """The unauthenticated engine port is unreachable from outside the cluster.

    engine_bind.py is REUSED as a subprocess rather than reimplemented. Its own
    header states that it prints roles, counts, interface CLASSES and address
    families and never an address, a hostname or the ssh target; its output is
    shown here under that contract, and passed through `sanitize` anyway.

    An `ACCEPTED` line is a refusal even if the exit code is zero. That
    combination should be impossible, which is exactly why it is worth
    checking: a gate that trusts one signal cannot notice when it stops
    meaning what it meant.
    """
    out = env.runner.run(
        [
            "python3",
            str(env.engine_bind),
            "check",
            "--generated-env",
            str(env.generated_env),
        ],
        timeout=TIMEOUTS["exposure"],
    )
    text = (out.stdout or "") + (out.stderr or "")
    detail = [line for line in text.splitlines() if line.strip()][:20]
    accepted = len(re.findall(r"ACCEPTED the connection", text))
    if accepted:
        return ProbeResult(
            "engine-exposure",
            "exposed",
            {"accepted_addresses": accepted, "exit_code": out.rc},
            detail,
        )
    if out.rc == 0:
        return ProbeResult("engine-exposure", "closed", {"exit_code": 0}, detail)
    return ProbeResult("engine-exposure", "unproven", {"exit_code": out.rc}, detail)


def probe_engine_controller(env: Environment) -> ProbeResult:
    """What the engine controller says about itself.

    /state is the controller's own view (contract section 6, the document the
    orchestrator and Grafana read). It is NOT a completion probe and is not
    treated as one: that is the next probe, and the reason it exists.
    """
    out = env.runner.run(
        ["curl", "-fsS", "-m", "5", env.controller_url + "/state"],
        timeout=TIMEOUTS["controller"],
    )
    if out.rc != 0:
        return ProbeResult("engine-controller", "unreachable", {"exit_code": out.rc})
    try:
        doc = json.loads(out.stdout)
    except (json.JSONDecodeError, TypeError):
        return ProbeResult("engine-controller", "unreadable")
    if not isinstance(doc, dict):
        return ProbeResult("engine-controller", "unreadable")

    recovery = doc.get("recovery") or {}
    facts: dict[str, object] = {}
    state = doc.get("state")
    if isinstance(state, str):
        facts["engine_state"] = state
    code = doc.get("state_code")
    if isinstance(code, int) and not isinstance(code, bool):
        facts["state_code"] = code
    facts["primary_ready"] = bool(doc.get("primary_ready"))
    in_progress = bool(recovery.get("in_progress")) if isinstance(recovery, dict) else False
    facts["recovery_in_progress"] = in_progress

    if in_progress:
        return ProbeResult("engine-controller", "recovering", facts)
    if code in CONTROLLER_SERVING_CODES and facts["primary_ready"]:
        return ProbeResult("engine-controller", "ready", facts)
    return ProbeResult("engine-controller", "not-ready", facts)


def _generation_tokens(text: str) -> float | None:
    """Sum every `vllm:generation_tokens_total` series in a /metrics body."""
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
        total = value if total is None else total + value
    return total


def probe_completion(env: Environment) -> ProbeResult:
    """A REAL completion, and the token counter must advance.

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
    """
    import tempfile

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
            timeout=TIMEOUTS["controller"],
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
            timeout=TIMEOUTS["controller"],
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
            "curl", "-fsS", "-m", str(TIMEOUTS["completion"]),
            "-H", "Content-Type: application/json",
            "-d", body,
            url + "/v1/chat/completions",
        ],
        timeout=TIMEOUTS["completion"] + 15,
    )
    elapsed = max(0.0, env.clock() - started)
    if reply_out.rc != 0:
        return ProbeResult("completion", "unreachable", {"exit_code": reply_out.rc, "elapsed_s": elapsed})
    try:
        reply = json.loads(reply_out.stdout)["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        reply = ""
    reply = (reply or "").strip()

    after = counter()
    if after is None:
        return ProbeResult("completion", "metrics-unreadable", {"elapsed_s": elapsed})

    # The counters are reported as COUNTS, never as the model's words. The
    # reply itself is model output; its LENGTH is all this check needs.
    facts: dict[str, object] = {
        "tokens_before": int(before),
        "tokens_after": int(after),
        "reply_chars": len(reply),
        "elapsed_s": elapsed,
    }
    if not reply:
        return ProbeResult("completion", "empty-reply", facts)
    if after <= before:
        return ProbeResult("completion", "wedged", facts)
    return ProbeResult("completion", "generated", facts)


def _schema_version(env: Environment, root: pathlib.Path, snippet: str) -> int | None:
    """One number out of deploy-common.sh, with DR_ROOT pointed where asked."""
    out = env.runner.run(
        ["bash", "-c", f'. "$1" || exit 9; {snippet}', "_", str(env.deploy_common)],
        timeout=TIMEOUTS["migrations"],
        env={"TECHSARA_DEPLOY_ROOT": str(root)},
    )
    if out.rc != 0:
        return None
    digits = out.stdout.strip()
    return int(digits) if digits.isdigit() else None


def probe_migrations(env: Environment) -> ProbeResult:
    """The incoming commit's migrations against what the database has applied.

    Read with the helpers scripts/lib/deploy-common.sh already exposes, so this
    cannot disagree with the numbers deploy.sh and deploy-rollback.sh use:
    `dr_live_schema_version` (the orchestrator's /health, falling back to
    PostgreSQL itself, which matters precisely when the orchestrator is the
    thing that is down) and `dr_code_schema_version_from_git`.

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
    code = _schema_version(env, env.repo_root, f'dr_code_schema_version_from_git "{env.ref}"')
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
    root = str(env.deploy_root)
    repo = str(env.repo_root)
    return {
        ("deploy-root", "dirty-tree"): (
            f"git -C {root} status --porcelain --untracked-files=no",
            f"git -C {root} diff",
        ),
        ("deploy-root", "wrong-branch"): (
            f"git -C {root} rev-parse --abbrev-ref HEAD",
            f"git -C {root} checkout {env.default_branch}",
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
        ("engine-exposure", "unavailable"): (
            f"python3 {repo}/.github/workflows/scripts/engine_bind.py check "
            f"--generated-env {root}/.runtime/generated.env",
        ),
        ("engine-controller", "unreachable"): (
            "docker ps --filter name=sf-local-ai-engine-controller-1 --format '{{.Names}} {{.Status}}'",
            f"{root}/scripts/cluster-status.sh",
        ),
        ("engine-controller", "unreadable"): (f"{root}/scripts/cluster-status.sh",),
        ("engine-controller", "not-ready"): (
            f"{root}/scripts/cluster-status.sh",
            f"{root}/scripts/cluster-verify-engine.sh",
        ),
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
        ("migrations", "unreadable"): (
            f"bash -c '. {root}/scripts/lib/deploy-common.sh; dr_live_schema_version'",
            "docker ps --filter name=sf-local-ai-postgres-1 --format '{{.Names}} {{.Status}}'",
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
    ("deploy-root", "wrong-branch"): (
        "The deploy job compares the deploy root's HEAD with the commit it is deploying, "
        "so the deploy root stays on the default branch."
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
    # probe-raised / probe-timed-out, and any verdict added without a remedy.
    return (
        f"python3 {env.repo_root}/.github/workflows/scripts/box_readiness.py "
        f"--deploy-root {env.deploy_root} --ref {env.ref}",
    )


def note_for(result: ProbeResult) -> str:
    return NOTES.get((result.probe, result.verdict), "")
