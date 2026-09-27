#!/usr/bin/env python3
"""The standing watch: is the production box still true BETWEEN releases?

WHY THIS SCRIPT EXISTS
----------------------
`engine_bind.py check` -- the proof that the unauthenticated vLLM port is
unreachable from the LAN and from the tailnet -- is invoked from exactly one
place in this repository: the last step of pipeline.yml's `verify` job, whose
`if:` requires `needs.deploy.result == 'success' && github.ref ==
'refs/heads/main'`. Deploys to main are days apart. Between them nothing looks
at the property at all: the check is a RELEASE GATE standing in for a MONITOR.

On 2026-09-21 that difference had a price. The engine port was found with a
wildcard IPv4 listener and non-cluster addresses ACCEPTING the connection,
because the nftables guard had been lost in a reboot -- a fact that had been
true for an unknown number of days and was discovered only because a release
happened to be deployed that afternoon.

This script is the observation half of the fix. It does NOT re-fix the
property: `feat/host-guard-at-boot` installs the systemd unit that survives a
reboot, and `verify`'s exposure assertion stays exactly where it is and is not
retired by anything here.

WHAT IT REFUSES TO DO
---------------------
It is READ-ONLY apart from ONE write: the node-exporter textfile that carries
its verdict. It never takes the deploy lock, never restarts a container, never
touches the engine. That one write happens on every run that actually OBSERVED
the box -- clean or faulty alike -- because rewriting the file is the only
thing that turns a previous run's fault back off again.

Its FIRST probe asks whether a rollout is in flight, and if one is it records
`deferred`, exits 0 and touches nothing: a monitor yields to a release, it does
not compete with it and it does not report a rollout as a fault.

THE PROBES ARE NOT IMPLEMENTED HERE
-----------------------------------
The four read-only probes -- engine exposure, a REAL completion (never
`/health`; a wedged engine serves a green `/health` for hours while
`vllm:generation_tokens_total` stands still), container states, and whether
the host guard's boot unit is active -- are the box-readiness track's
`box_probes.py`, sitting next to this file. This script IMPORTS them and MAY
NOT EDIT that module.

Importing rather than copying is also what makes the output rules inherited by
construction: the exact-key environment reader, the print allowlist, and the
rule that `.runtime/generated.env`, its path and its parsed mapping are never
echoed on ANY branch, live in `box_probes.py` and are not re-implemented here.
This file opens no environment file of its own. Everything it prints goes
through `scrub()` first, on the success path and on the exception path alike.

If a probe this watch needs turns out to be missing from `box_probes.py`, the
answer is to implement it HERE or hand it back to that track -- never to edit
their file. `REQUIRED_PROBES` below is the contract, in one place, so the
reconciliation at merge time is a diff and not an archaeology exercise.

FAIL CLOSED ON THE CHANNEL -- THE DELIBERATE EXCEPTION
------------------------------------------------------
Every other gate in this repository goes red when the thing it measures is
bad. This one does not, and the reason is written down rather than slipped in.

The owner's standing instruction after the 2026-09-15 uptime work is NO ALERT
MAIL, and a red scheduled run mails. So when this watch finds a real fault it
hands the fault to Prometheus -- which owns alerting -- and stays green. That
is only honest if the handover actually happened, so:

    verdict `ok`                                          -> GREEN, and the
                                                             textfile is
                                                             REWRITTEN so a
                                                             cleared fault
                                                             clears
    verdict `deferred` (a rollout is in flight)           -> GREEN, nothing
                                                             probed, nothing
                                                             written
    a fault verdict, AND the textfile was written, AND a
      Prometheus readback shows the metric present with a
      FRESH timestamp                                     -> GREEN, verdict carried
    a fault verdict, but the textfile directory is
      missing, the write failed, or the metric is absent
      or stale on readback                                -> RED
    a probe could not be PERFORMED                        -> RED

A clean run does NOT do the readback: the readback buys the right to be green
OVER A FAULT, and `ok` has no fault to justify. It writes, reports whether the
write landed, and stays green either way -- a healthy box must never mail.

The readback is what makes the green honest. Without it, "wrote a file" is
indistinguishable from "wrote a file nobody reads" -- and on this box today
nobody reads it: `/var/lib/node_exporter/textfile_collector` does not exist
(verified 2026-09-27 and again 2026-09-28: `ls -ld` says "No such file or
directory"), and the running node-exporter carries no
`--collector.textfile.directory` flag (verified both days: `docker inspect
--format '{{json .Config.Cmd}}' sf-local-ai-node-exporter-1` lists
--path.rootfs, --path.procfs, --path.sysfs, --web.listen-address, the
netdev/netclass collectors, FIVE --no-collector flags -- mdadm, nfs, nfsd, zfs,
xfs -- and a filesystem mount-point exclusion, and no textfile flag). That
install is TWO owner actions, both listed in production-watch.yml under blocker
2: the directory (root: `sudo install -d -o root -g techsphere -m 2775 ...`,
because the writer is the runner account and not root) and the node-exporter
recreate that makes something read it. This script does not work around either;
it reports RED until they are done, and doing only the first is worse than
doing neither, because then the write lands and still nobody reads it.

THE READBACK IS SCOPED to this run's own writer. A series of the right NAME is
not this run's series: see READBACK_JOB for the measurement that made a live
engine exposure green through one foreign sample, and `--readback-node` for the
label that refuses it.

AND THE HANDOVER IS NOT COMPLETE EITHER, YET. "Hands the fault to Prometheus,
which owns alerting" is the justification for being green over a real fault,
and the readback proves only the first half of it: that Prometheus SCRAPED the
file. Nothing yet ACTS on the scrape. Measured 2026-09-27 on this branch:
`grep -rn techsara_production_truth monitoring/` returns nothing -- no rule
under monitoring/prometheus/rules/ and no entry in
monitoring/developer-api/metrics-contract.json, where the precedent metric
`techsara_host_guard_*` has both. Writing that rule is the THIRD blocker on
the cron commit, listed with the other two in production-watch.yml; until it
exists, a fault this watch reports is read by a person or by nobody.

Usage:
    production_truth.py [--dry-run] [--deploy-root PATH] [--textfile-dir PATH]
                        [--prometheus-url URL] [--readback-node NODE]
                        [--scrape-interval-seconds N]
                        [--readback-deadline-seconds N] [--step-summary FILE]

`--dry-run` performs every read-only probe and reaches the same verdict and
the same exit code, but writes NOTHING: instead of using the channel it probes
it (does the directory exist and is it writable, does Prometheus answer) and
labels the result `predicted`. It is the hand-run form, safe on a live box.
"""
from __future__ import annotations

import argparse
import dataclasses
import fcntl
import importlib
import inspect
import json
import os
import pathlib
import re
import socket
import sys
import time
import types
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Callable

# The probe module lives next to this file. Running the script directly puts
# that directory on sys.path already; this makes `python3 -m` and an import
# from another cwd behave the same.
_HERE = pathlib.Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

#: 0 = the box is as it should be (or the verdict reached its sink), 1 = it is
#: not (or the verdict reached nobody), 2 = this script was asked to do
#: something impossible.
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2

#: The box-readiness track owns this module. This watch imports it; it does
#: not edit it and it does not copy it.
PROBE_MODULE = "box_probes"

#: THE CONTRACT. Each entry is a callable `box_probes.py` must expose, and the
#: question it answers. A probe returns either a mapping or an object carrying
#: `ok` (True = the property HOLDS), optionally `performed` (False = the probe
#: could not be carried out at all, which is never the same as a clean result)
#: and optionally `detail` (free text, scrubbed before it is printed).
REQUIRED_PROBES: dict[str, str] = {
    "probe_engine_exposure": (
        "is the unauthenticated engine port unreachable from every non-cluster address"
    ),
    "probe_real_completion": (
        "did a real completion advance vllm:generation_tokens_total (never /health)"
    ),
    "probe_container_states": (
        "is every expected container running, and none of them restarting"
    ),
    "probe_host_guard_unit": (
        "is the host packet filter's boot unit active"
    ),
}

#: Which verdict a FAILED probe produces. `degraded` is this watch's own, and
#: it is here rather than folded into the other two on purpose: a restarting
#: container and an inactive boot unit are faults that are neither an exposed
#: engine port nor a wedged engine, and labelling them as one of those would
#: corrupt the alert that reads this metric. It travels under exactly the same
#: channel rules as `exposed` and `wedged`.
FAULT_FOR_PROBE: dict[str, str] = {
    "probe_engine_exposure": "exposed",
    "probe_real_completion": "wedged",
    "probe_container_states": "degraded",
    "probe_host_guard_unit": "degraded",
}

#: Worst wins. `unavailable` outranks every fault: a run that could not carry
#: out one of its probes cannot claim to have measured the box, whatever the
#: other three said, and it goes red rather than reporting a partial reading
#: as a verdict.
SEVERITY: dict[str, int] = {"ok": 0, "degraded": 1, "wedged": 2, "exposed": 3, "unavailable": 4}

#: Verdicts that may be green whatever the channel does, because they hand
#: nothing over. `ok` is still WRITTEN (see REPORTED_VERDICTS) -- it just does
#: not have to prove the write was read back before it is allowed to be green.
NO_CHANNEL_NEEDED = frozenset({"ok", "deferred"})

#: Verdicts that are handed to Prometheus and may therefore be green.
FAULT_VERDICTS = frozenset({"exposed", "wedged", "degraded"})

#: Verdicts the textfile is WRITTEN for: the run observed the box and reached a
#: conclusion about it.
#:
#: `ok` is in here, and that is the fix for a defect that made this metric
#: useless. The file is one series per verdict and it is replaced wholesale, so
#: the only thing that turns `verdict{verdict="exposed"} 1` back into 0 -- and
#: the only thing that moves `check_timestamp_seconds` forward -- is a LATER run
#: writing the file. While `ok` wrote nothing, the first fault latched for good:
#: the Prometheus alert fired forever after the box had been fixed, nothing but
#: a root `rm` cleared it, and every run stayed green while it did.
#:
#: `deferred` and `unavailable` are deliberately ABSENT. Both mean this run did
#: not observe the box -- a rollout held the deploy lock, or a probe could not be
#: performed -- and a run that measured nothing must not zero another run's fault
#: or advance the freshness timestamp. Their silence leaves the last real reading
#: in place and lets the age of `check_timestamp_seconds` say "the watch has not
#: looked lately", which is a different statement from "the box is well" -- to a
#: person reading it. No alert rule reads that age yet; see `refresh_channel`.
#:
#: `unavailable` is therefore carried by the RED RUN and not by this metric. A
#: blind run exits 1 and writes nothing at all, which is why no series below
#: stands for it.
REPORTED_VERDICTS = frozenset({"ok"}) | FAULT_VERDICTS

#: The verdict series the textfile carries, worst last, derived from
#: REPORTED_VERDICTS rather than written out again. Deriving it is the point: a
#: series for a verdict `main()` never renders would be 0 in every file this
#: script can write -- a label a reader could alert on that nothing can ever
#: set. `unavailable` and `deferred` had exactly that shape and are gone.
VERDICT_SERIES: tuple[str, ...] = tuple(sorted(REPORTED_VERDICTS, key=lambda name: SEVERITY[name]))

METRIC_PREFIX = "techsara_production_truth"

#: The series NAME the textfile carries. Bare, with no labels: node-exporter's
#: textfile collector emits what is in the file and Prometheus attaches `job`,
#: `instance`, `node` and `role` itself (monitoring/prometheus/prometheus.yml,
#: `job_name: node`), so a label written here would either collide with those
#: or invent an identity the scrape does not agree with.
READBACK_METRIC = f"{METRIC_PREFIX}_check_timestamp_seconds"

#: THE READBACK IS SCOPED, and this is the whole of why. The readback is the
#: only thing that buys this job the right to be GREEN over a real fault, and it
#: used to query the BARE metric name and then take `max(samples, key=value)`
#: across every series that came back. Measured 2026-09-28, driving `main()` with
#: a live engine exposure, this run's own file written but NEVER scraped, and one
#: FOREIGN series of the same name from another instance whose value was an hour
#: ahead: `verdict exposed / channel written and read back from Prometheus,
#: sample 0s old / result GREEN`, exit 0, printing "the verdict reached a sink
#: that reads it" -- when it had reached nobody. `value >= written_at` separates
#: this writer's own earlier file from its newer one; it cannot separate this
#: writer from a different one.
#:
#: So the query names the job and the node, exactly as every host-guard rule in
#: this repository does (`{job="node"}` and `on(node)`,
#: monitoring/prometheus/rules/developer-api.yml:541, :542, :558, :560), and a
#: match on MORE THAN ONE series is a channel failure rather than an argmax:
#: after scoping there is one writer, and if there are two this run cannot tell
#: which sample is its own.
READBACK_JOB = "node"

#: The `node` label prometheus.yml attaches to the HEAD's node-exporter target.
#: The watch runs on the head -- there is one self-hosted runner and it is there
#: -- so this is the node whose textfile carries this run's write.
#: `test_the_readback_node_matches_the_prometheus_target` pins this against
#: monitoring/prometheus/prometheus.yml so it cannot drift silently, and running
#: the watch anywhere else fails CLOSED: the scoped query returns nothing, the
#: readback reports the metric ABSENT, and the run goes red.
DEFAULT_READBACK_NODE = "spark-1"

TEXTFILE_NAME = f"{METRIC_PREFIX}.prom"


#: A Prometheus label value is interpolated into a query string here, so the
#: characters that could end the string or the selector are refused rather than
#: escaped. A label value on this box is `spark-1`; anything outside this class
#: is a mistake, and a mistake in the readback's scope must not be a query that
#: still parses and means something else.
NODE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


def readback_expr(node: str) -> str:
    """The instant query the readback asks, scoped to this run's own writer."""
    if not NODE_LABEL_RE.match(node):
        raise ValueError(
            "the readback node must be a plain label value (letters, digits, "
            f"'_', '.', ':' and '-'), not {node!r}: it is interpolated into a "
            "Prometheus selector, and a value that can close the string could "
            "widen the scope this query exists to narrow"
        )
    return f'{READBACK_METRIC}{{job="{READBACK_JOB}", node="{node}"}}'

#: The node-exporter textfile convention this repository already uses -- see
#: monitoring/exporters/host-guard/host_guard_textfile.sh, which writes the
#: host packet filter's state the same way, into the same directory, with the
#: same atomic rename.
DEFAULT_TEXTFILE_DIR = "/var/lib/node_exporter/textfile_collector"

#: Loopback only. Prometheus is not reachable from off the box and this script
#: never dials anything else.
DEFAULT_PROMETHEUS_URL = "http://127.0.0.1:9090"

#: monitoring/prometheus/prometheus.yml, `job_name: node` -> scrape_interval 10s.
#: Two of those is the freshness window the readback allows.
DEFAULT_SCRAPE_INTERVAL_SECONDS = 10

#: How long to keep asking Prometheus for this run's own sample before calling
#: the channel dead. Only ever spent on the bad path.
DEFAULT_READBACK_DEADLINE_SECONDS = 90

DEFAULT_PROBE_TIMEOUT_SECONDS = 60.0


# ---------------------------------------------------------------- output safety
#
# This repository is PUBLIC and a scheduled run's log is public with it. The
# rule is the one engine_bind.py already follows: report ROLES and interface
# CLASSES, never an address, a URL, a hostname or an ssh target. Probe detail
# strings come from another module and are therefore untrusted text -- they go
# through here like everything else, and so does an exception's class name on
# the failure path.

IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")

#: Any token of hex digits and colons carrying at least TWO colons, plus an
#: optional `%scope`. Enumerating IPv6's forms is how a scrubber misses one:
#: the first version here was `(?:[0-9A-Fa-f]{1,4}:){2,}…`, which does not
#: match the COMPRESSED form `fd7a:115c:a1e0::9` at all -- the exact shape of
#: this cluster's tailnet addresses. Two colons is the discriminator, and it
#: cannot be reached by a clock time (one colon) or by ordinary prose.
IPV6_RE = re.compile(
    r"(?<![0-9A-Fa-f:%])(?=[0-9A-Fa-f]*:[0-9A-Fa-f]*:)[0-9A-Fa-f:]{2,}"
    r"(?:%[0-9A-Za-z_.\-]+)?(?![0-9A-Fa-f:])"
)

#: Both stop before a closing bracket or a sentence's punctuation, so a
#: redacted address does not take the surrounding parenthesis with it and
#: leave an unbalanced line. Nothing droppable from an address lives here.
URL_RE = re.compile(r"[^\s()\[\]]*://[^\s()\[\]]*")
GENERATED_ENV_RE = re.compile(r"[\w./\\-]*generated\.env[\w./\\-]*")

#: generated.env / environment keys whose VALUES are addresses, hosts or the
#: ssh target. Same shape as engine_bind.SECRET_KEY_RE, deliberately.
SECRET_KEY_RE = re.compile(r"(?:^|_)(?:IP|ADDR|ADDRESS|HOST|SSH|IFNAME)(?:_\d+)?$")

REDACTED = "[redacted]"


def environment_secrets(env: Mapping[str, str] | None = None) -> list[str]:
    """Values that must never appear in this script's output.

    Only the environment is read -- never `.runtime/generated.env`. This file
    does not open that file at all, which is the strongest form of the rule.
    """
    env = os.environ if env is None else env
    values: list[str] = []
    for key, value in env.items():
        if value and (SECRET_KEY_RE.search(key) or key == "TECHSARA_SECRET_ENV"):
            values.append(value)
            values.extend(part for part in re.split(r"[@,\s]+", value) if part)
    try:
        values.append(socket.gethostname())
    except OSError:
        pass
    return values


def scrub(text: str, secrets: list[str] | None = None) -> str:
    """Redact addresses, URLs, generated.env and every known secret value.

    Longest first, so a secret that contains another is not half-replaced. A
    value under four characters is skipped: it would redact ordinary words and
    make the report unreadable without hiding anything an address regex has
    not already taken.
    """
    if secrets is None:
        secrets = environment_secrets()
    for secret in sorted({s for s in secrets if s and len(s) >= 4}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = URL_RE.sub(REDACTED, text)
    text = GENERATED_ENV_RE.sub(REDACTED, text)
    text = IPV6_RE.sub(REDACTED, text)
    return IPV4_RE.sub(REDACTED, text)


# --------------------------------------------------------------------- results


class ProbeContractError(RuntimeError):
    """`box_probes.py` is absent, or does not match REQUIRED_PROBES."""


class ChannelError(RuntimeError):
    """The verdict could not be handed to anything that would read it."""


@dataclasses.dataclass(frozen=True)
class ProbeOutcome:
    name: str
    performed: bool
    ok: bool
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class Channel:
    required: bool
    ok: bool
    detail: str


@dataclasses.dataclass(frozen=True)
class Runtime:
    """Every side effect this script has, in one injectable place."""

    import_module: Callable[[str], types.ModuleType]
    now: Callable[[], float]
    lock_state: Callable[[pathlib.Path], str]
    write_textfile: Callable[[pathlib.Path, str], None]
    textfile_blocker: Callable[[pathlib.Path], str]
    query: Callable[[str], list[tuple[float, float]]]
    sleep: Callable[[float], None]


# ------------------------------------------------------------------ probe layer


def load_probe_module(importer: Callable[[str], types.ModuleType]) -> types.ModuleType:
    """Import `box_probes` and assert it matches REQUIRED_PROBES.

    The import error's TEXT is deliberately dropped and only its class name
    kept: an ImportError carries filesystem paths, and this log is public.
    """
    try:
        module = importer(PROBE_MODULE)
    except Exception as exc:  # noqa: BLE001 - any import failure is the same answer
        raise ProbeContractError(
            f"{PROBE_MODULE}.py could not be imported ({type(exc).__name__}). "
            "It is the box-readiness track's module and this watch imports it "
            "rather than copying it, so until that track has merged this job "
            "cannot perform its probes -- which is a RED run, not a green one."
        ) from None
    missing = [name for name in REQUIRED_PROBES if not callable(getattr(module, name, None))]
    if missing:
        raise ProbeContractError(
            f"{PROBE_MODULE}.py does not expose {', '.join(sorted(missing))}. "
            "Implement the missing probe in production_truth.py or hand it back "
            f"to the box-readiness track; never edit {PROBE_MODULE}.py from here."
        )
    return module


def normalise(name: str, raw: object) -> ProbeOutcome:
    """Turn whatever a probe returned into a ProbeOutcome, or refuse it."""
    if isinstance(raw, ProbeOutcome):
        return raw
    if isinstance(raw, Mapping):
        def get(key, default=None):
            return raw.get(key, default)
    elif hasattr(raw, "ok"):
        def get(key, default=None):
            return getattr(raw, key, default)
    else:
        raise ProbeContractError(
            f"{PROBE_MODULE}.{name}() returned a {type(raw).__name__}, which carries "
            "no `ok`. A probe returns a mapping or an object with `ok` (True = the "
            "property holds), optionally `performed` and `detail`."
        )
    ok = get("ok")
    if not isinstance(ok, (bool, int)) or isinstance(ok, float):
        raise ProbeContractError(
            f"{PROBE_MODULE}.{name}() returned `ok` as a {type(ok).__name__}. It must "
            "be a boolean: a truthy string like 'false' would read as a clean probe."
        )
    performed = get("performed")
    if performed is None:
        performed = get("could_run", True)
    detail = get("detail") or get("note") or ""
    return ProbeOutcome(name=name, performed=bool(performed), ok=bool(ok), detail=str(detail))


def call_probe(module: types.ModuleType, name: str, context: dict[str, object]) -> ProbeOutcome:
    """Call one probe, passing only the context keys its signature declares.

    A probe that requires something this watch cannot supply is a CONTRACT
    error, not a guess: the alternative is calling it with a default that
    happens to parse and reading the answer as if it meant something.
    """
    fn = getattr(module, name)
    kwargs: dict[str, object] = {}
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        for param_name, param in signature.parameters.items():
            if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                continue
            if param_name in context:
                kwargs[param_name] = context[param_name]
            elif param.default is param.empty:
                raise ProbeContractError(
                    f"{PROBE_MODULE}.{name}() requires a parameter this watch cannot "
                    f"supply: {param_name!r}. It offers {', '.join(sorted(context))}. "
                    "Add the parameter to the context here, or hand it back to the "
                    "box-readiness track."
                )
    try:
        raw = fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 - a probe that raised did not run
        return ProbeOutcome(
            name=name, performed=False, ok=False, detail=f"raised {type(exc).__name__}"
        )
    return normalise(name, raw)


def run_probes(module: types.ModuleType, context: dict[str, object]) -> list[ProbeOutcome]:
    return [call_probe(module, name, context) for name in REQUIRED_PROBES]


def classify(outcomes: list[ProbeOutcome]) -> tuple[str, list[str]]:
    """Worst verdict across the probes, and the reason lines behind it."""
    verdict, reasons = "ok", []
    for outcome in outcomes:
        if not outcome.performed:
            candidate = "unavailable"
            reasons.append(f"{outcome.name}: could not be performed ({outcome.detail or 'no detail'})")
        elif not outcome.ok:
            candidate = FAULT_FOR_PROBE[outcome.name]
            reasons.append(f"{outcome.name}: {candidate} ({outcome.detail or 'no detail'})")
        else:
            continue
        if SEVERITY[candidate] > SEVERITY[verdict]:
            verdict = candidate
    return verdict, reasons


# --------------------------------------------------------------- the deploy lock


def deploy_lock_state(lock_path: pathlib.Path) -> str:
    """`free`, `held` or `unknown` -- exactly the test the deploy path makes.

    scripts/lib/deploy-common.sh:219 `dr_lock_is_held` opens the lock for
    append and tries a non-blocking exclusive flock. So does this. A lock we
    cannot OPEN is `unknown`, never `free`: a monitor that cannot tell whether
    a rollout is running must not start probing on the assumption that one is
    not.
    """
    try:
        if not lock_path.exists():
            return "free"  # the lock is created by the first deploy
    except OSError:
        return "unknown"
    try:
        fd = os.open(str(lock_path), os.O_RDWR | os.O_APPEND)
    except OSError:
        return "unknown"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "held"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "free"
    finally:
        os.close(fd)


# -------------------------------------------------------------------- the sink


#: The check timestamp's resolution, in ONE place, because two numbers have to
#: agree exactly: the value `render_textfile` writes into the file, and the
#: `written_at` that `readback` compares Prometheus's scraped sample against.
#: They did not agree. The file carried `{now:.0f}` while `written_at` carried
#: the unrounded `time.time()`, so at every instant whose fraction rounded DOWN
#: the written value was strictly less than the value it was checked against,
#: and a Prometheus that had scraped this run's own file instantly was reported
#: "present but STALE" -- a RED run on half of all wall-clock instants, which on
#: a schedule means a mailed alert saying the monitor is broken while the box is
#: fine. Quantise BEFORE the number is used anywhere.
TIMESTAMP_SPEC = ".0f"


def quantise_timestamp(now: float) -> float:
    """`now` as the textfile will carry it: exactly what a scraper reads back."""
    return float(format(now, TIMESTAMP_SPEC))


def format_timestamp(now: float) -> str:
    """The only place the check timestamp is turned into text."""
    return format(now, TIMESTAMP_SPEC)


def render_textfile(verdict: str, now: float, reasons: list[str]) -> str:
    """The node-exporter textfile, in the shape host_guard_textfile.sh uses.

    One series per verdict rather than a verdict label on a single series, so
    a verdict that STOPS being reported leaves its old value at 0 instead of
    leaving a stale 1 behind for the alert to read.

    That argument only holds while the file is REWRITTEN on a clean run too: a
    series nobody rewrites keeps its last value, it does not decay to zero. See
    `refresh_channel` -- `ok` writes this file exactly as a fault does, and that
    is what lets a cleared fault clear.

    `now` is expected to be quantised already (`quantise_timestamp`), and the
    timestamp goes out through `format_timestamp`, so the number written here
    and the number `readback` compares cannot drift apart.

    Every series here is one a run can actually set. The label set is
    VERDICT_SERIES -- i.e. REPORTED_VERDICTS -- so `unavailable` and `deferred`
    get NO series at all: `main()` never renders them (a run that observed
    nothing writes nothing), so a series for either would read 0 in every file
    this script can write, which is a label promising information that nothing
    can ever put there. `unavailable` is carried by the run going RED, not by
    this metric.

    AND IT REFUSES ANYTHING ELSE. Narrowing the label set to REPORTED_VERDICTS
    cost this function its fail-closed edge: with the `unavailable` series gone,
    an out-of-contract verdict -- `unavailable`, `deferred`, or a typo -- used to
    render a perfectly well-formed file with EVERY verdict series at 0, plus
    `check_ok 1` and a fresh `check_timestamp_seconds`. That file says "a watch
    run got a reading" and names no reading: it would clear a live fault and move
    the freshness timestamp forward on the strength of a verdict this metric
    cannot express. Before the narrowing the same input at least set its own
    label to 1, so the shape was visible. `main()` cannot reach it -- it renders
    only REPORTED_VERDICTS, which
    `test_every_verdict_main_can_reach_writes_the_whole_label_set` pins -- and
    that is exactly why it is refused HERE rather than trusted to stay
    unreachable: the next caller is the one that would not know.
    """
    if verdict not in REPORTED_VERDICTS:
        raise ValueError(
            f"{verdict!r} is not a verdict this metric can carry, so there is no "
            "honest file for it. A run that did not observe the box writes "
            f"NOTHING; only {', '.join(sorted(REPORTED_VERDICTS))} are written."
        )
    lines = [
        f"# HELP {METRIC_PREFIX}_verdict 1 for the verdict this watch reached, 0 for the others.",
        f"# TYPE {METRIC_PREFIX}_verdict gauge",
    ]
    for name in VERDICT_SERIES:
        lines.append(f'{METRIC_PREFIX}_verdict{{verdict="{name}"}} {1 if name == verdict else 0}')
    lines += [
        # 1 in every file that exists, and that is the whole statement: only a
        # run that performed every probe writes this file. There is no reachable
        # 0 -- a run that could not perform a probe leaves the previous file
        # alone -- so the presence of this series means "a watch run got a
        # reading", and its ABSENCE (with no file at all) is the blind case.
        f"# HELP {METRIC_PREFIX}_check_ok 1 whenever this file exists: only a run that performed every probe writes it.",
        f"# TYPE {METRIC_PREFIX}_check_ok gauge",
        f"{METRIC_PREFIX}_check_ok 1",
        # Faults only. A probe that could not be PERFORMED makes the verdict
        # `unavailable` (SEVERITY 4 outranks every fault), and that run writes
        # no file at all, so a could-not-run probe can never be counted here.
        f"# HELP {METRIC_PREFIX}_faults How many probes reported a fault in the run that wrote this file.",
        f"# TYPE {METRIC_PREFIX}_faults gauge",
        f"{METRIC_PREFIX}_faults {len(reasons)}",
        f"# HELP {READBACK_METRIC} Unix time of the last watch run.",
        f"# TYPE {READBACK_METRIC} gauge",
        f"{READBACK_METRIC} {format_timestamp(now)}",
    ]
    return "\n".join(lines) + "\n"


def write_textfile(directory: pathlib.Path, text: str) -> None:
    """Atomic rename into the textfile directory, or raise.

    The directory is NOT created. Its absence is the finding -- creating it
    would turn "nobody is reading this" into a green tick, which is the exact
    defect this design exists to refuse.
    """
    if not directory.is_dir():
        raise ChannelError(
            "the node-exporter textfile directory does not exist, so nothing "
            "would ever read this verdict. Installing it is an owner/root "
            "action and this watch does not work around it."
        )
    tmp = directory / f".{TEXTFILE_NAME}.{os.getpid()}"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, directory / TEXTFILE_NAME)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise ChannelError(f"the textfile write failed ({type(exc).__name__})") from None


def textfile_blocker(directory: pathlib.Path) -> str:
    """Empty when the sink would accept a write, else why it would not."""
    try:
        if not directory.is_dir():
            return "the node-exporter textfile directory does not exist"
        if not os.access(directory, os.W_OK | os.X_OK):
            return "the node-exporter textfile directory is not writable by this user"
    except OSError as exc:
        return f"the textfile directory could not be inspected ({type(exc).__name__})"
    return ""


def http_query(base_url: str, expr: str, timeout: float = 10.0) -> list[tuple[float, float]]:
    """Prometheus instant query -> [(sample_time, value)]. Raises ChannelError.

    Neither the URL nor the response body ever reaches the output: only the
    exception's class name does, because both can carry an address.
    """
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ChannelError("the Prometheus URL is not http(s)")
    url = base_url.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - scheme checked
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 - every failure is "no readback"
        raise ChannelError(f"Prometheus did not answer ({type(exc).__name__})") from None
    if not isinstance(payload, Mapping) or payload.get("status") != "success":
        raise ChannelError("Prometheus answered, but not with a successful query result")
    result = (payload.get("data") or {}).get("result") or []
    samples: list[tuple[float, float]] = []
    for row in result:
        try:
            at, value = row["value"]
            samples.append((float(at), float(value)))
        except (KeyError, TypeError, ValueError):
            continue
    return samples


def readback(
    runtime: Runtime,
    *,
    expr: str,
    written_at: float,
    scrape_interval: int,
    deadline_seconds: int,
) -> Channel:
    """Did Prometheus actually pick up THIS run's write, and is it live?

    THREE questions, and a naive "the metric exists" check answers all three
    wrongly. `value >= written_at` proves the scrape saw this run's file rather
    than a stale one left by an earlier run of this same writer; the sample's own
    age proves Prometheus is still scraping at all; and `expr` -- scoped to
    `job` and `node`, see READBACK_JOB -- proves the series is THIS writer's and
    not some other producer of the same metric name. The third one is not
    theoretical padding: unscoped, a single foreign series with a future value
    made a live engine exposure green (measured 2026-09-28, see READBACK_JOB).

    More than one series under a scoped query is a CHANNEL FAILURE, not an
    argmax. `max(samples, key=value)` was the old behaviour and it is exactly
    the wrong reflex here: picking the highest value means preferring whichever
    series is furthest in the future, i.e. preferring the foreign one. After
    scoping there is one writer on one node; two samples means the scope no
    longer identifies this run, and a readback that cannot tell which sample is
    its own has not read anything back.
    """
    window = 2 * scrape_interval
    started = runtime.now()
    last = "the readback never ran"
    while True:
        try:
            samples = runtime.query(expr)
        except ChannelError as exc:
            last = str(exc)
            samples = []
        if len(samples) > 1:
            return Channel(
                required=True,
                ok=False,
                detail=(
                    f"the readback matched {len(samples)} series, not one: scoped to "
                    f"job={READBACK_JOB!r} and one node it must identify exactly this "
                    "run's writer, and it cannot tell which sample is this run's"
                ),
            )
        if samples:
            at, value = samples[0]
            if value < written_at:
                last = (
                    "the metric is present but STALE: the scraped sample predates "
                    "this run's write, so Prometheus is reading an older file"
                )
            else:
                age = runtime.now() - at
                if age > window:
                    last = (
                        f"the metric is present but STALE: the sample is {age:.0f}s old, "
                        f"and two scrape intervals is {window}s"
                    )
                else:
                    return Channel(
                        required=True,
                        ok=True,
                        detail=f"written and read back from Prometheus, sample {age:.0f}s old",
                    )
        elif last == "the readback never ran":
            last = (
                "the metric is ABSENT from Prometheus under this run's own scope: the "
                "textfile is written but not scraped, or --readback-node does not name "
                "the node whose exporter reads it"
            )
        if runtime.now() - started >= deadline_seconds:
            return Channel(required=True, ok=False, detail=last)
        runtime.sleep(min(float(scrape_interval), 5.0))


def use_channel(
    runtime: Runtime,
    *,
    verdict: str,
    reasons: list[str],
    textfile_dir: pathlib.Path,
    expr: str,
    scrape_interval: int,
    deadline_seconds: int,
) -> Channel:
    # Quantised BEFORE it is used, so `written_at` below is the very number the
    # file carries. Passing the raw clock here is the rounding defect described
    # at TIMESTAMP_SPEC: it made a perfect scrape look stale half the time.
    now = quantise_timestamp(runtime.now())
    try:
        runtime.write_textfile(textfile_dir, render_textfile(verdict, now, reasons))
    except ChannelError as exc:
        return Channel(required=True, ok=False, detail=str(exc))
    except OSError as exc:
        return Channel(required=True, ok=False, detail=f"the textfile write failed ({type(exc).__name__})")
    return readback(
        runtime,
        expr=expr,
        written_at=now,
        scrape_interval=scrape_interval,
        deadline_seconds=deadline_seconds,
    )


def refresh_channel(
    runtime: Runtime,
    *,
    verdict: str,
    reasons: list[str],
    textfile_dir: pathlib.Path,
) -> Channel:
    """Write the textfile for a verdict that has nothing to hand over.

    A clean run MUST still write -- see REPORTED_VERDICTS for why: skip it and
    the first fault latches in Prometheus for good.

    No readback, and `required=False`. The readback buys the right to be green
    OVER A FAULT and there is no fault here to justify; spending the Prometheus
    deadline on every healthy tick would hold the production box's single runner
    for nothing. A refusal is REPORTED -- in the step summary and in the run's
    log -- and the run stays green, because `ok` must never mail.

    WHAT THAT COSTS, plainly, because an earlier version of this docstring
    named a compensating control that does not exist. If a clean run's write
    keeps failing and no fault verdict comes along to force the RED write path,
    nothing alerts: a refresh that has NEVER landed leaves no series at all,
    and a staleness expression over an absent series does not fire without
    `absent()`. Measured on this branch on 2026-09-27: `grep -rn
    techsara_production_truth` over this repository finds no Prometheus rule
    for this metric under monitoring/prometheus/rules/ and no entry in
    monitoring/developer-api/metrics-contract.json, while the precedent metric
    `techsara_host_guard_*` has both: the rules at
    monitoring/prometheus/rules/developer-api.yml:541 and :558, and the
    contract at lines 43-45. And even that staleness rule
    -- `(time() - techsara_host_guard_check_timestamp_seconds) > 900` --
    is silent while the series is missing. So today the only reader of a
    failed refresh is the person reading this run's summary. Catching it
    unattended needs an `absent()`-and-staleness rule for
    `techsara_production_truth_check_timestamp_seconds`, and that rule is the
    THIRD blocker on the cron commit (production-watch.yml, "SHIPPED IN TWO
    COMMITS") -- not a control this function may claim.
    """
    now = quantise_timestamp(runtime.now())
    try:
        runtime.write_textfile(textfile_dir, render_textfile(verdict, now, reasons))
    except ChannelError as exc:
        return Channel(required=False, ok=False, detail=f"not required, and the refresh FAILED: {exc}")
    except OSError as exc:
        return Channel(
            required=False,
            ok=False,
            detail=(
                "not required, and the refresh FAILED: the textfile write failed "
                f"({type(exc).__name__})"
            ),
        )
    return Channel(
        required=False,
        ok=True,
        detail="not required (nothing to hand over); the textfile was refreshed",
    )


def predict_channel(runtime: Runtime, *, textfile_dir: pathlib.Path, expr: str) -> Channel:
    """`--dry-run`: probe the sink instead of using it, and say so.

    It asks the SCOPED query a real run would ask, so a `--readback-node` that
    names nothing is visible by hand before a scheduled run depends on it. An
    empty result is still an answer here -- the file has not been written -- so
    only a query Prometheus refuses outright is a predicted failure.
    """
    blocker = runtime.textfile_blocker(textfile_dir)
    if blocker:
        return Channel(required=True, ok=False, detail=f"predicted: {blocker}")
    try:
        runtime.query(expr)
    except ChannelError as exc:
        return Channel(required=True, ok=False, detail=f"predicted: {exc}")
    return Channel(
        required=True,
        ok=True,
        detail="predicted: the textfile directory accepts writes and Prometheus answers",
    )


# ------------------------------------------------------------------- reporting


ROW_LABELS: dict[str, str] = {
    "deploy_lock": "deploy lock",
    "probe_engine_exposure": "engine exposure",
    "probe_real_completion": "real completion probe",
    "probe_container_states": "container states",
    "probe_host_guard_unit": "host guard boot unit",
    "verdict": "verdict",
    "channel": "channel",
    "result": "result",
}


class Report:
    """Every line this script prints, scrubbed on the way out."""

    def __init__(self, secrets: list[str] | None = None) -> None:
        self.rows: list[tuple[str, str]] = []
        self.notes: list[str] = []
        self._secrets = secrets

    def row(self, key: str, value: str) -> None:
        self.rows.append((ROW_LABELS[key], scrub(value, self._secrets)))

    def note(self, text: str) -> None:
        self.notes.append(scrub(text, self._secrets))

    def lines(self) -> list[str]:
        out = ["production truth: a read-only watch of the production box", ""]
        out += [f"  {label:<24} {value}" for label, value in self.rows]
        if self.notes:
            out.append("")
            out += [f"  - {note}" for note in self.notes]
        return out

    def markdown(self) -> list[str]:
        out = ["### Production truth (scheduled)", "", "| check | reading |", "| --- | --- |"]
        out += [f"| {label} | {value} |" for label, value in self.rows]
        if self.notes:
            out.append("")
            out += [f"- {note}" for note in self.notes]
        return out

    def emit(self, step_summary: pathlib.Path | None) -> None:
        for line in self.lines():
            print(line)
        if step_summary is None:
            return
        try:
            with open(step_summary, "a", encoding="utf-8") as handle:
                handle.write("\n".join(self.markdown()) + "\n")
        except OSError as exc:
            print(f"  (the step summary could not be written: {type(exc).__name__})")


# ------------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only watch of the production box.")
    parser.add_argument(
        "--deploy-root",
        default=os.environ.get("TECHSARA_DEPLOY_ROOT") or str(_HERE.parents[2]),
        help="the production checkout (only its deploy lock is read here)",
    )
    parser.add_argument("--textfile-dir", default=DEFAULT_TEXTFILE_DIR)
    parser.add_argument("--prometheus-url", default=DEFAULT_PROMETHEUS_URL)
    parser.add_argument(
        "--readback-node",
        default=os.environ.get("TECHSARA_WATCH_NODE") or DEFAULT_READBACK_NODE,
        help=(
            "the `node` label prometheus.yml attaches to the node-exporter that reads "
            "this run's textfile; the readback is scoped to it so a foreign series of "
            "the same metric name cannot satisfy it"
        ),
    )
    parser.add_argument("--scrape-interval-seconds", type=int, default=DEFAULT_SCRAPE_INTERVAL_SECONDS)
    parser.add_argument(
        "--readback-deadline-seconds", type=int, default=DEFAULT_READBACK_DEADLINE_SECONDS
    )
    parser.add_argument("--probe-timeout-seconds", type=float, default=DEFAULT_PROBE_TIMEOUT_SECONDS)
    parser.add_argument("--step-summary", default=os.environ.get("GITHUB_STEP_SUMMARY") or None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="probe and decide exactly as usual, but write nothing anywhere",
    )
    return parser


def default_runtime(args: argparse.Namespace) -> Runtime:
    return Runtime(
        import_module=importlib.import_module,
        now=time.time,
        lock_state=deploy_lock_state,
        write_textfile=write_textfile,
        textfile_blocker=textfile_blocker,
        query=lambda expr: http_query(args.prometheus_url, expr),
        sleep=time.sleep,
    )


def main(argv: list[str] | None = None, runtime: Runtime | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    if args.scrape_interval_seconds <= 0 or args.readback_deadline_seconds < 0:
        print("FATAL: the scrape interval must be positive and the deadline non-negative", file=sys.stderr)
        return EXIT_USAGE

    if not args.readback_node.strip():
        print("FATAL: --readback-node must name a node, or the readback is unscoped", file=sys.stderr)
        return EXIT_USAGE
    try:
        readback_expr(args.readback_node.strip())
    except ValueError as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return EXIT_USAGE

    runtime = runtime or default_runtime(args)
    report = Report()
    step_summary = pathlib.Path(args.step_summary) if args.step_summary else None
    deploy_root = pathlib.Path(args.deploy_root)
    textfile_dir = pathlib.Path(args.textfile_dir)
    expr = readback_expr(args.readback_node.strip())

    try:
        # ---------------------------------------------- FIRST PROBE, before any other
        # A monitor yields to a release. It does not compete with one, and it
        # does not report a rollout as a fault.
        lock = runtime.lock_state(deploy_root / ".runtime" / "locks" / "deploy.lock")
        report.row("deploy_lock", lock)
        if lock == "held":
            report.row("verdict", "deferred")
            report.row("channel", "not required (nothing was probed)")
            report.row("result", "GREEN")
            report.note(
                "a rollout holds the deploy lock, so this run probed nothing and "
                "changed nothing. The next tick observes the box."
            )
            report.emit(step_summary)
            return EXIT_OK
        if lock == "unknown":
            report.row("verdict", "unavailable")
            report.row("channel", "not reached")
            report.row("result", "RED")
            report.note(
                "the deploy lock could not be read, so this run cannot tell "
                "whether a rollout is in flight and refuses to probe."
            )
            report.emit(step_summary)
            return EXIT_FAIL

        # ------------------------------------------------------------- the probes
        context: dict[str, object] = {
            "deploy_root": deploy_root,
            "timeout": args.probe_timeout_seconds,
        }
        try:
            module = load_probe_module(runtime.import_module)
            outcomes = run_probes(module, context)
        except ProbeContractError as exc:
            for name in REQUIRED_PROBES:
                report.row(name, "not performed")
            report.row("verdict", "unavailable")
            report.row("channel", "not reached")
            report.row("result", "RED")
            report.note(str(exc))
            report.emit(step_summary)
            return EXIT_FAIL

        for outcome in outcomes:
            if not outcome.performed:
                reading = "COULD NOT BE PERFORMED"
            elif outcome.ok:
                reading = "ok"
            else:
                reading = FAULT_FOR_PROBE[outcome.name].upper()
            if outcome.detail:
                reading = f"{reading} ({outcome.detail})"
            report.row(outcome.name, reading)

        verdict, reasons = classify(outcomes)
        report.row("verdict", verdict)

        # ------------------------------------------------------------- the channel
        if verdict not in REPORTED_VERDICTS:
            # `unavailable` only -- a `deferred` run has already returned. This
            # run did not observe the box, so it writes NOTHING: zeroing a
            # previous run's fault on the strength of a reading that was never
            # taken is the mistake REPORTED_VERDICTS exists to prevent. Red
            # whatever the channel would have done.
            channel = Channel(required=False, ok=False, detail="not consulted (the verdict is unavailable)")
        elif verdict in FAULT_VERDICTS:
            if args.dry_run:
                channel = predict_channel(runtime, textfile_dir=textfile_dir, expr=expr)
            else:
                channel = use_channel(
                    runtime,
                    verdict=verdict,
                    reasons=reasons,
                    textfile_dir=textfile_dir,
                    expr=expr,
                    scrape_interval=args.scrape_interval_seconds,
                    deadline_seconds=args.readback_deadline_seconds,
                )
        else:
            # `ok`: WRITTEN, but not proven. The file is refreshed so a cleared
            # fault clears, and the run is green whatever the sink says.
            if args.dry_run:
                channel = dataclasses.replace(
                    predict_channel(runtime, textfile_dir=textfile_dir, expr=expr),
                    required=False,
                )
            else:
                channel = refresh_channel(
                    runtime, verdict=verdict, reasons=reasons, textfile_dir=textfile_dir
                )
        report.row("channel", channel.detail)

        if verdict in NO_CHANNEL_NEEDED:
            green = True
        elif verdict in FAULT_VERDICTS:
            # THE DELIBERATE EXCEPTION, and the only one. A fault is green only
            # because Prometheus has it and owns the alert from here.
            green = channel.ok
        else:
            green = False
        report.row("result", "GREEN" if green else "RED")
        for reason in reasons:
            report.note(reason)
        if verdict in FAULT_VERDICTS and green:
            report.note(
                "the fault is REAL and is carried by Prometheus, which owns the "
                "alert. This run is green because the verdict reached a sink that "
                "reads it -- not because the box is well."
            )
        if verdict in FAULT_VERDICTS and not green:
            report.note(
                "the verdict reached nobody, so this run is red. A verdict nobody "
                "receives is not a verdict."
            )
        if args.dry_run:
            report.note("--dry-run: nothing was written; the channel reading is a prediction.")
        report.emit(step_summary)
        return EXIT_OK if green else EXIT_FAIL
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is still RED, and still scrubbed
        print(scrub(f"FATAL: production_truth raised {type(exc).__name__}: {exc}"), file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
