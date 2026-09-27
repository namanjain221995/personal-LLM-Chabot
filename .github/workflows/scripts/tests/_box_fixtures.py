"""Shared fixtures for the box-readiness tests: a fake box and a fake shell.

Nothing here touches a machine. Every probe in box_probes.py reaches the
outside world through ONE injected CommandRunner, so a scenario is a mapping
from "which command is this" to "what it printed", and a test is that mapping
plus an assertion.

The deploy root IS a real temporary directory, because three probes ask the
filesystem directly (does the lock file exist, what did the holder write, what
is in the environment file) and stubbing that as well would be stubbing the
thing under test.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import box_probes  # noqa: E402

#: A value that must NEVER reach stdout. Three properties on purpose: it is
#: not an address and not a URL, so a test that passes proves the PRINT
#: ALLOWLIST held rather than that the sanitizer happened to match it; it sits
#: behind a password-shaped key name, because that is the shape the real file
#: has; and it is a low-entropy English phrase, because the `security` job
#: scans this repository's full history and a high-entropy literal next to the
#: word PASSWORD is a gitleaks finding waiting to happen.
SENTINEL_SECRET = "this-fixture-value-must-never-be-printed"
SENTINEL_SECRET_PATH = "/run/techsara/owner-secrets.env"
SENTINEL_ADDRESS = "10.77.88.99"

GENERATED_ENV = f"""# a trimmed stand-in for the 220-key file on the box
MAIN_MODEL=nvidia/Qwen-Test-Model
TECHSARA_SECRET_ENV={SENTINEL_SECRET_PATH}
TECHSARA_OWNER_BOOTSTRAP_PASSWORD={SENTINEL_SECRET}
TECHSARA_BIND_ADDRESS={SENTINEL_ADDRESS}
TECHSARA_ENGINE_HEAD_API_URL=http://{SENTINEL_ADDRESS}:8000
CLUSTER_API_BIND_ADDRESS=0.0.0.0
VLLM_PORT=8000
"""

HOLDER = """purpose=deploy
pid=40321
host=spark-0e68
actor=some-operator
origin=github-actions run 918273
started_at=2026-09-23T00:00:00Z
head=bd532e383e5bcafe0123456789abcdef01234567
"""

#: A /metrics body. The counter is substituted per scenario.
METRICS = """# HELP vllm:generation_tokens_total Number of generation tokens processed.
# TYPE vllm:generation_tokens_total counter
vllm:generation_tokens_total{{model_name="nvidia/Qwen-Test-Model"}} {count}
vllm:prompt_tokens_total{{model_name="nvidia/Qwen-Test-Model"}} 99
"""

CONTROLLER_READY = (
    '{"state": "READY", "state_code": 2, "primary_ready": true, '
    '"recovery": {"in_progress": false, "step": "idle"}, "schema": 1}'
)

#: engine_bind.py's own report, closed. It carries a URL and an address on
#: purpose: foreign output is sanitized on the way out, and the test proves it.
BIND_CLOSED = f"""### Main engine exposure: can the unauthenticated engine API be reached from outside the cluster?
- engine port 8000 listeners: 1 wildcard (IPv4), 0 loopback
- a wildcard is in play: proving from the worker that only the cluster fabric reaches the engine port
- 3 non-cluster addresses: 3 blocked, 0 ACCEPTED, 0 not proven (required: all blocked)
- probed via http://{SENTINEL_ADDRESS}:8000/v1/models on the fabric address {SENTINEL_ADDRESS}
RESULT: PASSED
"""

#: engine_bind.py's own report, ONE address accepting. The `- FAIL:` line is
#: part of the real output and is here for a reason: engine_bind prints
#: "ACCEPTED the connection" once per address (engine_bind.py:879) AND once more
#: inside `report.fail` (engine_bind.py:896, rendered by Report.fail at
#: :647-649, printed by main() at :988). A fixture without it hid an off-by-one
#: in box_probes.probe_engine_exposure: measured on 2026-09-27 against the real
#: `engine_bind.evaluate`, the bare phrase occurs 2 times for 1 accepting
#: address and 3 times for 2, so `accepted_addresses` was always one too many
#: and no test could see it.
BIND_EXPOSED = f"""### Main engine exposure: can the unauthenticated engine API be reached from outside the cluster?
- 3 non-cluster addresses: 2 blocked, 1 ACCEPTED, 0 not proven (required: all blocked)
  - non-cluster address (interface class: lan, IPv4): ACCEPTED the connection on the engine port
- FAIL: 1 non-cluster address (interface class: lan) ACCEPTED the connection: the unauthenticated engine API is reachable from outside the cluster (apply scripts/host-guard.sh, see docs/developer-platform/OPERATIONS.md section 13)
- the address that answered was {SENTINEL_ADDRESS} at http://{SENTINEL_ADDRESS}:8000
RESULT: FAILED
"""

#: TWO addresses accepting. This is not invented: it is the engine_bind report
#: the `verify` job printed on the production box in run 36304046169 at
#: 2026-09-27T08:26:48Z, copied verbatim (engine_bind prints interface CLASSES
#: and never an address, so there is nothing here to redact). The bare phrase
#: occurs THREE times for TWO accepting addresses, so before the fix this real
#: log would have been reported as `accepted_addresses 3`.
BIND_EXPOSED_TWO = """### Main engine exposure: can the unauthenticated engine API be reached from outside the cluster?
- engine port 8000 listeners: 1 wildcard (IPv4), 0 wildcard (IPv6/dual-stack), 0 loopback, 0 on the configured address, 0 on another specific address
- configuration: asks for a wildcard (the approved cluster shape; outside proof is mandatory)
- a wildcard is in play: proving from the worker that only the cluster fabric reaches the engine port
- cluster fabric address: reachable from the worker (required)
- 3 non-cluster addresses: 1 blocked, 2 ACCEPTED, 0 not proven (required: all blocked)
  - non-cluster address (interface class: lan, IPv4): ACCEPTED the connection on the engine port
  - non-cluster address (interface class: tailnet, IPv4): ACCEPTED the connection on the engine port
  - non-cluster address (interface class: tailnet, IPv6): blocked (refused); control port reachable, so the path is proven
- FAIL: 2 non-cluster addresses (interface class: lan, tailnet) ACCEPTED the connection: the unauthenticated engine API is reachable from outside the cluster (apply scripts/host-guard.sh, see docs/developer-platform/OPERATIONS.md section 13)

RESULT: FAILED (1 reason(s) above)
"""

#: engine_bind said an address accepted, but NOT in the per-address shape the
#: probe counts - the trip-wire case. A narrowed pattern must not be able to
#: turn this gate blind, so this still has to refuse, with no count invented.
BIND_EXPOSED_UNCOUNTABLE = """### Main engine exposure: can the unauthenticated engine API be reached from outside the cluster?
- FAIL: 1 non-cluster address (interface class: lan) ACCEPTED the connection: the unauthenticated engine API is reachable from outside the cluster
RESULT: FAILED
"""


def make_box(tmp: str, *, lock_file: bool = True, holder: str | None = HOLDER) -> pathlib.Path:
    """A deploy root with the three files the filesystem probes read."""
    root = pathlib.Path(tmp) / "deploy-root"
    (root / ".runtime" / "locks").mkdir(parents=True)
    (root / ".runtime" / "generated.env").write_text(GENERATED_ENV, encoding="utf-8")
    if lock_file:
        (root / ".runtime" / "locks" / "deploy.lock").touch()
    if holder is not None:
        (root / ".runtime" / "locks" / "deploy.holder").write_text(holder, encoding="utf-8")
    return root


class Raises:
    """A scenario entry that blows up the way a real subprocess can."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def __call__(self, argv):  # noqa: ANN001 - a stub
        raise self.exc


class FakeRunner(box_probes.CommandRunner):
    """The one seam every probe goes through.

    A scenario maps a command CLASS to what it produced. The classifier is
    deliberately literal - it matches on the same argv the probes build - so a
    probe that changes the command it runs makes these tests fail loudly rather
    than silently stop exercising anything.
    """

    def __init__(self, **overrides) -> None:
        self.calls: list[list[str]] = []
        #: The env overrides each call was given, index-aligned with `calls`.
        #: The schema probes point TECHSARA_DEPLOY_ROOT at two different trees
        #: on purpose, and that is worth asserting rather than assuming.
        self.envs: list[dict] = []
        self.metrics_counts = list(overrides.pop("metrics_counts", [1000, 1008]))
        self._metrics_seen = 0
        self.scenario = self._defaults()
        unknown = set(overrides) - set(self.scenario)
        assert not unknown, f"no such scenario key: {sorted(unknown)}"
        self.scenario.update(overrides)

    def _defaults(self) -> dict[str, object]:
        return {
            "git-inside": box_probes.Completed(0, "true\n"),
            "git-status": box_probes.Completed(0, ""),
            "git-branch": box_probes.Completed(0, "main\n"),
            "df": box_probes.Completed(0, "Avail\n2625G\n"),
            "flock": box_probes.Completed(0, ""),
            "bind-check": box_probes.Completed(0, BIND_CLOSED),
            "bind-resolve": self._resolve,
            "controller": box_probes.Completed(0, CONTROLLER_READY),
            "metrics": self._metrics,
            "completion": box_probes.Completed(
                0, '{"choices": [{"message": {"content": "READY."}}]}'
            ),
            "schema-live": box_probes.Completed(0, "41\n"),
            "schema-code": box_probes.Completed(0, "41\n"),
        }

    # -- handlers ---------------------------------------------------------
    def _resolve(self, argv):  # noqa: ANN001 - a stub
        path = pathlib.Path(argv[argv.index("--github-output") + 1])
        path.write_text(
            f"host={SENTINEL_ADDRESS}\nport=8000\nurl=http://{SENTINEL_ADDRESS}:8000\n",
            encoding="utf-8",
        )
        return box_probes.Completed(0, f"::add-mask::{SENTINEL_ADDRESS}\nresolved, port 8000\n")

    def _metrics(self, argv):  # noqa: ANN001 - a stub
        index = min(self._metrics_seen, len(self.metrics_counts) - 1)
        self._metrics_seen += 1
        return box_probes.Completed(0, METRICS.format(count=self.metrics_counts[index]))

    # -- the classifier ---------------------------------------------------
    @staticmethod
    def classify(argv: list[str]) -> str:
        joined = " ".join(argv)
        if argv[0] == "git":
            if "--is-inside-work-tree" in argv:
                return "git-inside"
            if "status" in argv:
                return "git-status"
            if "--abbrev-ref" in argv:
                return "git-branch"
        if argv[0] == "df":
            return "df"
        if argv[0] == "bash":
            if "flock" in joined:
                return "flock"
            if "dr_live_schema_version" in joined:
                return "schema-live"
            if "dr_code_schema_version_from_git" in joined:
                return "schema-code"
        if argv[0] == "python3":
            if "check" in argv:
                return "bind-check"
            if "resolve" in argv:
                return "bind-resolve"
        if argv[0] == "curl":
            target = argv[-1]
            if target.endswith("/state"):
                return "controller"
            if target.endswith("/metrics"):
                return "metrics"
            if target.endswith("/v1/chat/completions"):
                return "completion"
        raise AssertionError(f"the fake runner does not know this command: {argv!r}")

    def run(self, argv, *, timeout, env=None):  # noqa: ANN001 - matches the real signature
        argv = list(argv)
        self.calls.append(argv)
        self.envs.append(dict(env or {}))
        assert timeout and timeout > 0, "every probe must carry a timeout"
        handler = self.scenario[self.classify(argv)]
        if callable(handler):
            return handler(argv)
        return handler


def timeout_expired() -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired(cmd=["curl"], timeout=15)


class Clock:
    """A clock that advances a tenth of a second per reading."""

    def __init__(self, start: float = 1_790_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        self.t += 0.1
        return self.t


def make_env(root: pathlib.Path, runner: FakeRunner, **kwargs) -> box_probes.Environment:
    return box_probes.Environment(
        deploy_root=root,
        repo_root=root.parent / "workspace",
        ref="bd532e383e5bcafe0123456789abcdef01234567",
        runner=runner,
        clock=Clock(),
        **kwargs,
    )
