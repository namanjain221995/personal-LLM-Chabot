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
It is READ-ONLY. It never takes the deploy lock, never restarts a container,
never touches the engine. Its FIRST probe asks whether a rollout is in flight,
and if one is it records `deferred`, exits 0 and touches nothing: a monitor
yields to a release, it does not compete with it and it does not report a
rollout as a fault.

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

    verdict `ok`                                          -> GREEN
    verdict `deferred` (a rollout is in flight)           -> GREEN, nothing probed
    a fault verdict, AND the textfile was written, AND a
      Prometheus readback shows the metric present with a
      FRESH timestamp                                     -> GREEN, verdict carried
    a fault verdict, but the textfile directory is
      missing, the write failed, or the metric is absent
      or stale on readback                                -> RED
    a probe could not be PERFORMED                        -> RED

The readback is what makes the green honest. Without it, "wrote a file" is
indistinguishable from "wrote a file nobody reads" -- and on this box today
nobody reads it: `/var/lib/node_exporter/textfile_collector` does not exist,
and the running node-exporter carries no `--collector.textfile.directory`
flag. That install is an owner/root action, and this script does not work
around it; it reports RED until it is done.

Usage:
    production_truth.py [--dry-run] [--deploy-root PATH] [--textfile-dir PATH]
                        [--prometheus-url URL] [--scrape-interval-seconds N]
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

#: Verdicts that need no channel, because there is nothing to hand over.
NO_CHANNEL_NEEDED = frozenset({"ok", "deferred"})

#: Verdicts that are handed to Prometheus and may therefore be green.
FAULT_VERDICTS = frozenset({"exposed", "wedged", "degraded"})

METRIC_PREFIX = "techsara_production_truth"
READBACK_EXPR = f"{METRIC_PREFIX}_check_timestamp_seconds"
TEXTFILE_NAME = f"{METRIC_PREFIX}.prom"

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


def render_textfile(verdict: str, now: float, reasons: list[str]) -> str:
    """The node-exporter textfile, in the shape host_guard_textfile.sh uses.

    One series per verdict rather than a verdict label on a single series, so
    a verdict that STOPS being reported leaves its old value at 0 instead of
    leaving a stale 1 behind for the alert to read.
    """
    lines = [
        f"# HELP {METRIC_PREFIX}_verdict 1 for the verdict this watch reached, 0 for the others.",
        f"# TYPE {METRIC_PREFIX}_verdict gauge",
    ]
    for name in ("ok", "degraded", "wedged", "exposed", "unavailable", "deferred"):
        lines.append(f'{METRIC_PREFIX}_verdict{{verdict="{name}"}} {1 if name == verdict else 0}')
    lines += [
        f"# HELP {METRIC_PREFIX}_check_ok 1 when every probe could be performed; 0 means the verdict carries no information.",
        f"# TYPE {METRIC_PREFIX}_check_ok gauge",
        f"{METRIC_PREFIX}_check_ok {0 if verdict == 'unavailable' else 1}",
        f"# HELP {METRIC_PREFIX}_faults How many probes reported a fault or could not run.",
        f"# TYPE {METRIC_PREFIX}_faults gauge",
        f"{METRIC_PREFIX}_faults {len(reasons)}",
        f"# HELP {READBACK_EXPR} Unix time of the last watch run.",
        f"# TYPE {READBACK_EXPR} gauge",
        f"{READBACK_EXPR} {now:.0f}",
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
    written_at: float,
    scrape_interval: int,
    deadline_seconds: int,
) -> Channel:
    """Did Prometheus actually pick up THIS run's write, and is it live?

    Two questions, both of which a naive "the metric exists" check answers
    wrongly. `value >= written_at` proves the scrape saw this run's file
    rather than a stale one left by a previous run, and the sample's own age
    proves Prometheus is still scraping at all.
    """
    window = 2 * scrape_interval
    started = runtime.now()
    last = "the readback never ran"
    while True:
        try:
            samples = runtime.query(READBACK_EXPR)
        except ChannelError as exc:
            last = str(exc)
            samples = []
        if samples:
            at, value = max(samples, key=lambda pair: pair[1])
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
            last = "the metric is ABSENT from Prometheus: the textfile is written but not scraped"
        if runtime.now() - started >= deadline_seconds:
            return Channel(required=True, ok=False, detail=last)
        runtime.sleep(min(float(scrape_interval), 5.0))


def use_channel(
    runtime: Runtime,
    *,
    verdict: str,
    reasons: list[str],
    textfile_dir: pathlib.Path,
    scrape_interval: int,
    deadline_seconds: int,
) -> Channel:
    now = runtime.now()
    try:
        runtime.write_textfile(textfile_dir, render_textfile(verdict, now, reasons))
    except ChannelError as exc:
        return Channel(required=True, ok=False, detail=str(exc))
    except OSError as exc:
        return Channel(required=True, ok=False, detail=f"the textfile write failed ({type(exc).__name__})")
    return readback(
        runtime,
        written_at=now,
        scrape_interval=scrape_interval,
        deadline_seconds=deadline_seconds,
    )


def predict_channel(runtime: Runtime, *, textfile_dir: pathlib.Path) -> Channel:
    """`--dry-run`: probe the sink instead of using it, and say so."""
    blocker = runtime.textfile_blocker(textfile_dir)
    if blocker:
        return Channel(required=True, ok=False, detail=f"predicted: {blocker}")
    try:
        runtime.query(READBACK_EXPR)
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

    runtime = runtime or default_runtime(args)
    report = Report()
    step_summary = pathlib.Path(args.step_summary) if args.step_summary else None
    deploy_root = pathlib.Path(args.deploy_root)
    textfile_dir = pathlib.Path(args.textfile_dir)

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
        if verdict in NO_CHANNEL_NEEDED:
            channel = Channel(required=False, ok=True, detail="not required (nothing to hand over)")
        elif verdict in FAULT_VERDICTS:
            if args.dry_run:
                channel = predict_channel(runtime, textfile_dir=textfile_dir)
            else:
                channel = use_channel(
                    runtime,
                    verdict=verdict,
                    reasons=reasons,
                    textfile_dir=textfile_dir,
                    scrape_interval=args.scrape_interval_seconds,
                    deadline_seconds=args.readback_deadline_seconds,
                )
        else:  # unavailable: red whatever the channel does
            channel = Channel(required=False, ok=False, detail="not consulted (the verdict is unavailable)")
        report.row("channel", channel.detail)

        if verdict == "ok":
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
