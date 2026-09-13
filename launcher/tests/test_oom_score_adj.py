"""Out-of-memory protection: who the kernel may kill, and what it costs to say so.

The requirement (2026-09-13): the kernel must not OOM-kill the main vLLM engine,
and applying that must never restart the engine. Docker applies
``oom_score_adj`` only when a container is CREATED, and Compose recreates a
container whenever the rendered definition's hash changes, so the design rests
on facts proven here:

* an unset or ``0`` ``${ENGINE_OOM_SCORE_ADJ:-0}`` (and
  ``${AUX_ENGINE_OOM_SCORE_ADJ:-0}`` on the router, embed and reranker engines)
  hashes exactly like a service without the key (Compose omits a zero), so
  merging the switches drifts nothing and no path -- not even the launcher's
  "not serving" branch that runs ``up -d`` on both ranks -- recreates an engine
  because of them;
* both ranks, and both head chains (dual, and the single-node chain
  CLUSTER_MODE=auto falls back to), read the same key, and the worker's copy
  is resolved with the launcher's own dotenv parser;
* the only literal positive values live in files no deploy ever brings up
  (OCR, speech, monitoring);
* oom_score_adj orders victims, it cannot exempt one for free: at -1000 the
  kernel kills dockerd (-500) instead of the engine, and a restarted dockerd
  restarts every container, so the recommended engine value is -450 --
  pinned below against the scores measured on both nodes.

Static checks use the standard library only; the Compose checks shell out to
``docker compose config`` (resolve and render, nothing is created) and skip
when Docker Compose v2.24+ is unavailable.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

# The overlay suite is imported as a MODULE, never its TestCase by name: a
# TestCase bound in this namespace would be collected and run a second time.
try:
    from . import test_compose_overlays as overlays
    from .support import REPO_ROOT
    from .test_cluster_scripts import _function_source
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    import test_compose_overlays as overlays
    from support import REPO_ROOT
    from test_cluster_scripts import _function_source

from techsara_cli.utils import render_env

COMPOSE = REPO_ROOT / "compose"
SCRIPTS = REPO_ROOT / "scripts"
SWITCH = "${ENGINE_OOM_SCORE_ADJ:-0}"
AUX_SWITCH = "${AUX_ENGINE_OOM_SCORE_ADJ:-0}"
#: The values OPERATIONS.md section 14 tells an operator to set (the switches
#: default to 0; nothing in the repository sets them). See OomOrderProjectionTests.
RECOMMENDED_ENGINE_ADJ = -450
RECOMMENDED_AUX_ENGINE_ADJ = 700
AUX_ENGINES = ("vllm-router", "vllm-embed", "vllm-reranker")
OPERATIONS = REPO_ROOT / "docs" / "developer-platform" / "OPERATIONS.md"

#: Every file in the launcher's DGX chain (state.json compose_files) -- the
#: files a routine deploy's `techsara up` renders.
LAUNCHER_CHAIN = (
    REPO_ROOT / "compose.yaml",
    COMPOSE / "compose.dgx-spark.yaml",
    COMPOSE / "compose.published-dgx-spark.yaml",
    COMPOSE / "compose.cluster-dgx-spark.yaml",
)

#: Side stacks: brought up only by their own scripts (ocr.sh, whisper.sh,
#: monitoring.sh), never by `techsara up`, scripts/deploy.sh or the Pipeline.
#: The ORDER is the contract: the engines that hold the most unified memory
#: the kernel cannot see go first, telemetry next, application services (0)
#: last, and the main engine never.
EXPECTED_SIDE_STACK = {
    "compose.ocr.yaml": {"ocr": 900},
    "compose.whisper.yaml": {"whisper": 800},
    "compose.monitoring.yaml": {
        "grafana": 600, "cadvisor": 600, "postgres-exporter": 600,
        "data-stores-exporter": 600, "blackbox-exporter": 600,
        "prometheus": 500, "node-exporter": 500, "dgx-gpu-exporter": 500,
    },
    "compose.monitoring-worker.yaml": {"node-exporter": 500, "dgx-gpu-exporter": 500},
}


def _service_blocks(path: Path) -> dict[str, str]:
    """Top-level ``services:`` entries of one compose file, as raw text."""
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^services:\n(.*?)(?=^\S|\Z)", text, re.S | re.M)
    if match is None:
        return {}
    body = match.group(1)
    blocks: dict[str, str] = {}
    for item in re.finditer(r"^  ([a-z0-9][a-z0-9-]*):\n(.*?)(?=^  [a-z0-9][a-z0-9-]*:\n|\Z)", body, re.S | re.M):
        blocks[item.group(1)] = item.group(2)
    return blocks


def _oom_values(block: str) -> list[str]:
    return re.findall(r"^    oom_score_adj:\s*(.+?)\s*$", block, re.M)


class OomScoreAdjStaticTests(unittest.TestCase):
    def test_the_only_oom_score_adj_in_the_launchers_chain_are_the_inert_switches(self) -> None:
        """Every service `techsara up` starts is recreated by a routine deploy
        as soon as its definition changes (`up -d --no-deps <service>`): a
        literal value on postgres, the orchestrator, the frontend, the
        router/embed/reranker engines or the controller would restart it on the
        next push to main. The engines carry only switches, which render
        nothing until they are set. The head engine's switch lives in
        compose.dgx-spark.yaml -- the file BOTH head chains share -- and not in
        the cluster overlay, which the single-node fallback of
        CLUSTER_MODE=auto leaves out."""
        found: dict[str, list[str]] = {}
        for path in LAUNCHER_CHAIN:
            for service, block in _service_blocks(path).items():
                values = _oom_values(block)
                if values:
                    found[f"{path.name}:{service}"] = values
        expected = {"compose.dgx-spark.yaml:vllm": [SWITCH]}
        expected.update({f"compose.dgx-spark.yaml:{service}": [AUX_SWITCH] for service in AUX_ENGINES})
        self.assertEqual(found, expected)

    def test_both_ranks_read_the_same_switch_and_the_sentinel_cannot_pull_the_worker_in(self) -> None:
        """The worker rank reads the same key (cluster-sync.sh ships it), so
        the two ranks are created from the same number. The routine deploy's
        `cluster-worker.sh start vllm-worker-sentinel` is `up -d` WITHOUT
        --no-deps, so a depends_on from the sentinel to vllm-worker would make
        Compose converge -- and recreate a drifted -- worker rank."""
        worker = _service_blocks(COMPOSE / "compose.cluster-worker.yaml")
        self.assertEqual(_oom_values(worker["vllm-worker"]), [SWITCH])
        self.assertEqual(_oom_values(worker["vllm-worker-sentinel"]), [])
        for service in ("vllm-worker-sentinel", "vllm-worker"):
            keys = [line for line in worker[service].splitlines() if not line.lstrip().startswith("#")]
            self.assertFalse([line for line in keys if re.match(r"^    depends_on:", line)], service)

    def test_side_stacks_put_the_big_hidden_memory_holders_first_and_nothing_below_zero(self) -> None:
        for name, expected in EXPECTED_SIDE_STACK.items():
            blocks = _service_blocks(COMPOSE / name)
            actual = {service: _oom_values(block) for service, block in blocks.items()}
            with self.subTest(file=name):
                self.assertEqual({s: v for s, v in actual.items() if v}, {s: [str(n)] for s, n in expected.items()})
        ocr = EXPECTED_SIDE_STACK["compose.ocr.yaml"]["ocr"]
        whisper = EXPECTED_SIDE_STACK["compose.whisper.yaml"]["whisper"]
        telemetry = [n for f in ("compose.monitoring.yaml", "compose.monitoring-worker.yaml") for n in EXPECTED_SIDE_STACK[f].values()]
        # A UVM-driven OOM ends only when a GPU holder dies, so every
        # expendable GPU holder (OCR, speech, the auxiliary engines once their
        # switch is set) ranks ahead of telemetry, which frees next to nothing.
        self.assertTrue(1000 >= ocr > whisper > RECOMMENDED_AUX_ENGINE_ADJ > max(telemetry) >= min(telemetry) > 0)

    def test_the_public_tunnel_is_left_alone(self) -> None:
        """cloudflared is the site's front door; scripts/tunnel.sh recreates it
        on a definition change. It is neither expendable nor worth a recreate."""
        for service, block in _service_blocks(COMPOSE / "compose.cloudflare.yaml").items():
            with self.subTest(service=service):
                self.assertEqual(_oom_values(block), [])


class ClusterSyncShipsTheSwitchTests(unittest.TestCase):
    """scripts/cluster-sync.sh resolves ENGINE_OOM_SCORE_ADJ the way the head's
    chain does -- the launcher's own parse_env_file, .env < secrets.env <
    generated.env over the process environment (compose.py _environment) --
    and ships it only when set."""

    def setUp(self) -> None:
        if shutil.which("bash") is None or shutil.which("python3") is None:
            self.skipTest("bash and python3 are required")
        temporary = tempfile.TemporaryDirectory(prefix="techsara-oom-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _resolve(self, dotenv: str | None, generated: str | None, secrets: str | None = None, *,
                 exported: str | None = None) -> subprocess.CompletedProcess:
        env_file = self.root / ".env"
        generated_env = self.root / "generated.env"
        secrets_env = self.root / "secrets.env"
        for path, content in ((env_file, dotenv), (generated_env, generated), (secrets_env, secrets)):
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content.encode("utf-8"))
        script = (
            "set -euo pipefail\n"
            'die() { printf "error: %s\\n" "$*" >&2; exit 2; }\n'
            f"ROOT={json.dumps(str(REPO_ROOT))}\n"
            f"{_function_source(SCRIPTS / 'cluster-sync.sh', 'engine_oom_score_adj')}\n"
            f"ENV_FILE={json.dumps(str(env_file))}\n"
            f"GENERATED_ENV={json.dumps(str(generated_env))}\n"
            f"SECRETS_ENV={json.dumps(str(secrets_env))}\n"
            'oom_adj="$(engine_oom_score_adj)" || exit 2\n'
            'printf "[%s]" "$oom_adj"\n'
        )
        environment = {k: v for k, v in os.environ.items() if k not in {"ENGINE_OOM_SCORE_ADJ", "PYTHONPATH"}}
        if exported is not None:
            environment["ENGINE_OOM_SCORE_ADJ"] = exported
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=environment, check=False)

    def test_unset_everywhere_ships_nothing(self) -> None:
        result = self._resolve("CLUSTER_MODE=dual\n", "TECHSARA_CLUSTER_MODE=dual\n")
        self.assertEqual((result.returncode, result.stdout), (0, "[]"), result.stderr)
        result = self._resolve(None, None)
        self.assertEqual((result.returncode, result.stdout), (0, "[]"), result.stderr)

    def test_dotenv_value_is_shipped_and_generated_env_outranks_it_like_the_heads_chain(self) -> None:
        result = self._resolve("ENGINE_OOM_SCORE_ADJ=-450\n", "TECHSARA_CLUSTER_MODE=dual\n")
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "[-450]", ""))
        result = self._resolve('ENGINE_OOM_SCORE_ADJ="-450"\n', None)
        self.assertEqual((result.returncode, result.stdout), (0, "[-450]"), result.stderr)
        result = self._resolve("ENGINE_OOM_SCORE_ADJ=-450\n", "ENGINE_OOM_SCORE_ADJ=-400\n")
        self.assertEqual((result.returncode, result.stdout), (0, "[-400]"), result.stderr)

    def test_secrets_env_sits_between_the_two_and_a_present_but_empty_key_wins_its_layer(self) -> None:
        """The launcher applies .env, then secrets.env, then generated.env, each
        overriding the last, and an EMPTY value still overrides (it renders as
        0, i.e. nothing). Skipping an empty layer would ship .env's value to the
        worker while the head renders 0: two ranks created from two numbers."""
        result = self._resolve("ENGINE_OOM_SCORE_ADJ=-450\n", None, "ENGINE_OOM_SCORE_ADJ=-400\n")
        self.assertEqual((result.returncode, result.stdout), (0, "[-400]"), result.stderr)
        result = self._resolve("ENGINE_OOM_SCORE_ADJ=-450\n", "ENGINE_OOM_SCORE_ADJ=-300\n", "ENGINE_OOM_SCORE_ADJ=-400\n")
        self.assertEqual((result.returncode, result.stdout), (0, "[-300]"), result.stderr)
        result = self._resolve("ENGINE_OOM_SCORE_ADJ=-450\n", "ENGINE_OOM_SCORE_ADJ=\n")
        self.assertEqual((result.returncode, result.stdout), (0, "[]"), result.stderr)
        result = self._resolve("ENGINE_OOM_SCORE_ADJ=-450\n", None, "ENGINE_OOM_SCORE_ADJ=\n")
        self.assertEqual((result.returncode, result.stdout), (0, "[]"), result.stderr)

    #: Lines Compose and the launcher both accept, with the value they render.
    PARITY_LINES = {
        "ENGINE_OOM_SCORE_ADJ=-450 # protect the engine\n": "-450",
        "ENGINE_OOM_SCORE_ADJ=-450   \n": "-450",
        "ENGINE_OOM_SCORE_ADJ=-450\r\n": "-450",
        "export ENGINE_OOM_SCORE_ADJ=-450\n": "-450",
        "ENGINE_OOM_SCORE_ADJ='-450' # quoted, then a comment\n": "-450",
        "  ENGINE_OOM_SCORE_ADJ=700\n": "700",
    }

    def test_every_line_the_launcher_accepts_resolves_to_the_launchers_value(self) -> None:
        """Review finding: the old resolver read the raw line, so
        `-1000 # comment`, trailing spaces or CRLF -- all rendered as a number
        by Compose on the head -- stopped the --env-only sync and, with it,
        every routine deploy. The resolver now uses the launcher's parser;
        this is the parity check (Compose's own render is checked in
        ComposeHashesTheSwitchTests)."""
        from techsara_cli.utils import parse_env_file

        for line, value in self.PARITY_LINES.items():
            with self.subTest(line=line):
                result = self._resolve(line, None)
                self.assertEqual((result.returncode, result.stdout), (0, f"[{value}]"), result.stderr)
                self.assertEqual(parse_env_file(self.root / ".env").get("ENGINE_OOM_SCORE_ADJ"), value)

    def test_a_value_compose_would_refuse_or_the_kernel_would_reject_stops_the_sync(self) -> None:
        for bad in ("abc", "-1001", "1001", "- 5", "10000", "-450abc"):
            with self.subTest(value=bad):
                result = self._resolve(f"ENGINE_OOM_SCORE_ADJ={bad}\n", None)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("not an integer in [-1000, 1000]", result.stderr)

    def test_an_exported_key_is_the_fallback_the_launcher_uses_and_a_conflicting_one_is_refused(self) -> None:
        """compose.py _environment starts from os.environ and lets the files
        override it, so an exported key with no file value is what the head
        renders; the worker must get it too. An exported key that DIFFERS from
        the files is refused: the launcher renders the files' value, a plain
        `docker compose` (cluster-recover.sh) the exported one."""
        result = self._resolve("CLUSTER_MODE=dual\n", None, exported="-450")
        self.assertEqual((result.returncode, result.stdout), (0, "[-450]"), result.stderr)
        result = self._resolve("ENGINE_OOM_SCORE_ADJ=-450\n", None, exported="-450")
        self.assertEqual((result.returncode, result.stdout), (0, "[-450]"), result.stderr)
        result = self._resolve("ENGINE_OOM_SCORE_ADJ=-450\n", None, exported="-1000")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("exported in this shell", result.stderr)

    def test_a_value_at_or_below_dockerds_is_shipped_so_the_ranks_agree_but_warned_about(self) -> None:
        for value in ("-500", "-999", "-1000"):
            with self.subTest(value=value):
                result = self._resolve(f"ENGINE_OOM_SCORE_ADJ={value}\n", None)
                self.assertEqual((result.returncode, result.stdout), (0, f"[{value}]"), result.stderr)
                self.assertIn("at or below dockerd (-500)", result.stderr)
        result = self._resolve(f"ENGINE_OOM_SCORE_ADJ={RECOMMENDED_ENGINE_ADJ}\n", None)
        self.assertEqual(result.stderr, "")

    def test_worker_env_gets_the_key_only_when_set_and_it_is_resolved_before_the_file_is_rewritten(self) -> None:
        text = (SCRIPTS / "cluster-sync.sh").read_text(encoding="utf-8")
        self.assertIn('if [ -n "$oom_adj" ]; then echo "ENGINE_OOM_SCORE_ADJ=$oom_adj"; fi', text)
        resolved_at = text.index('oom_adj="$(engine_oom_score_adj)" || exit 2')
        rewritten_at = text.index('rm -f "$WORKER_ENV_LOCAL"')
        self.assertLess(resolved_at, rewritten_at, "an invalid value must fail before worker.env is replaced")
        # Never through the generic CLUSTER_* copy: the key is not CLUSTER_-prefixed,
        # so it cannot be written twice.
        self.assertFalse(SWITCH.startswith("${CLUSTER_"))


LAUNCHER_CLI = REPO_ROOT / "launcher" / "techsara_cli" / "cli.py"
CONTROLLER_DIR = REPO_ROOT / "monitoring" / "engine-controller"


def _code_lines(path: Path) -> list[str]:
    """A shell script's lines without comments (whole-line or trailing)."""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append(re.sub(r"\s+#\s.*$", "", line))
    return lines


def _guarded_by_not_preserve_main(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    """True when ``node`` sits in the BODY of an ``if`` whose test requires
    ``not preserve_main`` (alone, or as one operand of an ``and``)."""

    def requires_not_preserve(test: ast.expr) -> bool:
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            return isinstance(test.operand, ast.Name) and test.operand.id == "preserve_main"
        if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.And):
            return any(requires_not_preserve(value) for value in test.values)
        return False

    child = node
    parent = parents.get(child)
    while parent is not None:
        if isinstance(parent, ast.If) and requires_not_preserve(parent.test):
            if any(child is statement for statement in parent.body):
                return True
        child, parent = parent, parents.get(parent)
    return False


def _string_args(call: ast.Call) -> list[str | None]:
    return [arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None for arg in call.args]


class TheEngineValueArrivesOnlyWhenARankIsCreatedTests(unittest.TestCase):
    """Docker applies oom_score_adj when a container is CREATED and never on a
    restart, so the switch can reach an engine only through a path that
    creates one. These tests pin every such path to a recreate that happens
    for its own reason (a --full deploy, an incident's --clear-kernel-cache,
    an engine that was not serving) and every routine path to "never creates
    a rank" -- no model reload is ever scheduled just to apply the value."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = ast.parse(LAUNCHER_CLI.read_text(encoding="utf-8"))
        cls.parents = {child: parent for parent in ast.walk(cls.tree) for child in ast.iter_child_nodes(parent)}
        cls.calls = [node for node in ast.walk(cls.tree) if isinstance(node, ast.Call)]

    def _calls_named(self, name: str) -> list[ast.Call]:
        found = []
        for call in self.calls:
            func = call.func
            called = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else None
            if called == name:
                found.append(call)
        return found

    def test_a_routine_deploy_runs_the_launcher_with_the_preserve_flag_and_only_full_drops_it(self) -> None:
        lines = _code_lines(SCRIPTS / "deploy.sh")
        self.assertIn('PRESERVE=1; [ "$FULL" = 1 ] && PRESERVE=', [line.strip() for line in lines])
        launches = [line for line in lines if "./techsara up" in line]
        self.assertEqual(len(launches), 1, launches)
        self.assertIn('TECHSARA_PRESERVE_MAIN_MODEL="$PRESERVE" ./techsara up', launches[0])

    def test_every_launcher_call_that_can_create_the_head_engine_is_behind_not_preserve_main(self) -> None:
        """`up -d --no-deps vllm` (and the safer-context --force-recreate retry)
        are the launcher's only ways to create the head. Both must sit in the
        body of an `if not preserve_main`, so a serving engine under the
        routine deploy's flag is never handed to Compose at all -- no
        --no-recreate subtlety, no hash comparison, just no `up`."""
        head_ups = [call for call in self._calls_named("up_service") if _string_args(call)[:1] == ["vllm"]]
        self.assertGreaterEqual(len(head_ups), 2, "the start and the safer-context retry")
        for call in head_ups:
            with self.subTest(line=call.lineno):
                self.assertTrue(_guarded_by_not_preserve_main(call, self.parents), f"cli.py:{call.lineno} can create the head during a routine deploy")
        generic = [call for call in self._calls_named("run") if "up" in _string_args(call)]
        self.assertEqual(generic, [], "no bare compose `up` that could converge vllm alongside another service")

    def test_the_only_worker_start_outside_a_pair_restart_names_the_sentinel(self) -> None:
        """`cluster-worker.sh start` with no service converges vllm-worker; the
        full `cluster-sync.sh` re-ships the model. Both are pair-restart steps.
        What the routine deploy runs is `--env-only` and a start that NAMES the
        sentinel, which has no depends_on (see the static test above)."""
        scripts = [call for call in self._calls_named("_run_cluster_script") if len(call.args) >= 2]
        self.assertTrue(scripts)
        routine = []
        for call in scripts:
            args = tuple(_string_args(call)[1:])
            # Only the calls that can CREATE a worker container: `down` (the
            # launcher's teardown) removes, and a removed rank is recreated
            # by the next start, which is what is checked here.
            if args[:2] != ("cluster-worker.sh", "start") and args[:1] != ("cluster-sync.sh",):
                continue
            if _guarded_by_not_preserve_main(call, self.parents):
                continue
            routine.append(args)
        self.assertEqual(
            sorted(routine),
            sorted([("cluster-sync.sh", "--env-only"), ("cluster-worker.sh", "start", "vllm-worker-sentinel")]),
        )
        start = [line.strip() for line in _code_lines(SCRIPTS / "cluster-worker.sh") if "worker_compose up" in line]
        self.assertEqual(start, ['worker_compose up -d "$@"'], "start forwards the named service, nothing added")

    def test_the_env_only_sync_brings_up_nothing_but_the_engine_controller(self) -> None:
        ups = [line.strip() for line in _code_lines(SCRIPTS / "cluster-sync.sh") if re.search(r"\bup\s+-d\b", line)]
        self.assertEqual(len(ups), 1, ups)
        self.assertIn("head_compose up -d --no-deps engine-controller", ups[0])

    def test_the_recovery_actors_can_restart_an_engine_but_have_no_call_that_creates_one(self) -> None:
        """The engine controller and the worker sentinel recover through the
        Docker API. A restart keeps the HostConfig the container was created
        with, so no automatic recovery can apply -- or undo -- the value: it
        is decided at the pair restart and survives every recovery after."""
        common = (CONTROLLER_DIR / "common.py").read_text(encoding="utf-8")
        endpoints = set(re.findall(r"/containers/\{[^}]*\}/([a-z]+)", common))
        self.assertEqual(endpoints, {"json", "top", "logs", "restart"})
        self.assertNotRegex(common, r"/containers/create|/containers/\{[^}]*\}/(start|update|kill)")
        for program in ("controller.py", "sentinel.py"):
            source = (CONTROLLER_DIR / program).read_text(encoding="utf-8")
            with self.subTest(program=program):
                self.assertLessEqual(set(re.findall(r"\bdocker\.([a-z_]+)\(", source)), {"inspect", "top", "logs", "restart"})
                self.assertNotRegex(source, r"docker compose|/containers/create|subprocess")

    def test_force_recovery_only_restarts_and_clear_kernel_cache_recreates_both_ranks_from_current_files(self) -> None:
        """--force (restart) keeps each rank's old value; --clear-kernel-cache
        stops both ranks and brings them back with `up -d`, so after
        `cluster-sync.sh --env-only` it is a create path that applies the
        switch to BOTH ranks together -- worker first, then the head through
        the launcher's own chain (which carries .env, secrets.env and
        generated.env, like the head's normal start)."""
        lines = [line.strip() for line in _code_lines(SCRIPTS / "cluster-recover.sh")]
        self.assertIn('worker_compose restart --timeout 5 vllm-worker || die "worker restart failed; nothing else was touched"', lines)
        self.assertIn('docker restart -t 10 "$HEAD_CTR" || die "head restart failed"', lines)
        worker_up = next(i for i, line in enumerate(lines) if line.startswith("worker_compose up -d vllm-worker"))
        head_up = next(i for i, line in enumerate(lines) if line.startswith("head_compose_current up -d --no-deps vllm"))
        self.assertLess(worker_up, head_up)
        ups = [line for line in lines if re.search(r"\bup\s+-d\b", line)]
        self.assertEqual(len(ups), 2, ups)

    def test_applying_the_side_stack_values_names_its_services_and_never_converges_an_engine(self) -> None:
        """monitoring.sh shares the head's project (sf-local-ai) and renders the
        launcher chain underneath its overlay; a bare `up -d` there would
        reconcile vllm. It must name its services with --no-deps."""
        lines = _code_lines(SCRIPTS / "monitoring.sh")
        text = "\n".join(lines)
        head_up = re.search(r"head_monitoring_compose up -d --no-deps \\\n\s*(.+?)\"\$@\"", text, re.S)
        self.assertIsNotNone(head_up, "monitoring.sh up must be `up -d --no-deps <named services>`")
        named = set(head_up.group(1).replace("\\", " ").split())
        self.assertEqual(named, set(EXPECTED_SIDE_STACK["compose.monitoring.yaml"]))
        self.assertNotIn("vllm", named)


#: Measured read-only on 2026-09-13 (/proc/meminfo, /proc/<pid>/status,
#: /proc/<pid>/oom_score_adj): RAM + swap in GiB, and the host RSS + swap of
#: the processes that bound the engine's place in the kill order.
NODE_RAM_PLUS_SWAP_GIB = {"head": 121.69 + 64.0, "worker": 121.69 + 32.0}
#: (oom_score_adj, host RSS + swap GiB) -- systemd-journald and dockerd.
JOURNALD = {"head": (-250, 0.04), "worker": (-250, 0.06)}
DOCKERD = {"head": (-500, 0.45), "worker": (-500, 0.10)}
#: The engine's largest process today (VLLM::Worker_TP): head 0.71 GiB RSS +
#: 3.66 GiB swap, worker 8.56 GiB RSS. The projection allows it to grow.
ENGINE_HOST_MEMORY_RANGE_GIB = (0.0, 25.0)


def _badness(adj: int, host_gib: float, total_gib: float) -> float:
    """The kernel's oom_badness in thousandths of RAM+swap
    (mm/oom_kill.c: rss + swap + page tables, plus adj * totalpages / 1000);
    the victim is the eligible process with the highest value."""
    return host_gib * 1000.0 / total_gib + adj


class OomOrderProjectionTests(unittest.TestCase):
    """Review finding (high): at -1000 the engine is ineligible, so once the
    expendable processes are gone the kernel's next victim is dockerd (-500);
    with live-restore off (both nodes) a restarted dockerd stops and restarts
    EVERY container, the engine included, and with nothing killable left the
    node hangs (kernel.panic=0). The recommended value must rank the engine
    after every application process (adj 0) and systemd-journald, and before
    dockerd -- pinned against the scores measured on both nodes."""

    def test_the_recommended_engine_value_sits_between_journald_and_dockerd_on_both_nodes(self) -> None:
        low, high = ENGINE_HOST_MEMORY_RANGE_GIB
        for node, total in NODE_RAM_PLUS_SWAP_GIB.items():
            for engine_gib in (low, 5.0, 9.0, high):
                with self.subTest(node=node, engine_host_gib=engine_gib):
                    engine = _badness(RECOMMENDED_ENGINE_ADJ, engine_gib, total)
                    self.assertLess(engine, _badness(*JOURNALD[node], total), "journald must go before the engine")
                    self.assertLess(engine, _badness(0, 0.0, total), "every adj-0 application process goes before the engine")
                    self.assertGreater(engine, _badness(*DOCKERD[node], total), "the engine must go before dockerd")

    def test_minus_999_ranks_the_engine_after_dockerd_so_it_is_not_the_last_victim(self) -> None:
        """-1000 is OOM_SCORE_ADJ_MIN: oom_badness() never selects the process,
        so dockerd is chosen before it by construction. -999, offered as "the
        last victim" in the first draft of the runbook, is no better: even a
        25 GiB engine still ranks after dockerd on both nodes."""
        low, high = ENGINE_HOST_MEMORY_RANGE_GIB
        for node, total in NODE_RAM_PLUS_SWAP_GIB.items():
            for engine_gib in (low, high):
                with self.subTest(node=node, engine_host_gib=engine_gib):
                    self.assertLess(_badness(-999, engine_gib, total), _badness(*DOCKERD[node], total))

    def test_the_auxiliary_engines_rank_ahead_of_every_application_service(self) -> None:
        """With the aux switch set, the router/embed/reranker (about 24.5 GiB of
        GPU memory the kernel cannot see) are chosen before postgres or the
        orchestrator, whatever host RSS those have (up to 20% of RAM+swap)."""
        for node, total in NODE_RAM_PLUS_SWAP_GIB.items():
            with self.subTest(node=node):
                self.assertGreater(_badness(RECOMMENDED_AUX_ENGINE_ADJ, 0.0, total), _badness(0, 0.2 * total, total))


class TheRunbookRecommendsWhatTheTestsPinTests(unittest.TestCase):
    """OPERATIONS.md section 14 is what an operator follows; it must recommend
    the values proven above and must not schedule a model reload to apply
    them (review finding: the zero-downtime rule)."""

    @classmethod
    def setUpClass(cls) -> None:
        text = OPERATIONS.read_text(encoding="utf-8")
        start = text.index("## 14. Out-of-memory protection")
        following = text.find("\n## ", start + 1)
        cls.section = text[start:] if following < 0 else text[start:following]

    def test_the_recommended_values_are_the_pinned_ones(self) -> None:
        self.assertIn(f"ENGINE_OOM_SCORE_ADJ={RECOMMENDED_ENGINE_ADJ}", self.section)
        self.assertIn(f"AUX_ENGINE_OOM_SCORE_ADJ={RECOMMENDED_AUX_ENGINE_ADJ}", self.section)
        self.assertNotIn("ENGINE_OOM_SCORE_ADJ=-1000`", self.section.replace("AUX_", ""))

    def test_no_step_restarts_the_pair_to_apply_the_value(self) -> None:
        for forbidden in ("scripts/cluster-up.sh`. Do **not** set", "Restart the pair through the launcher"):
            self.assertNotIn(forbidden, self.section)
        self.assertIn("kernel.panic", self.section)
        self.assertIn("live-restore", self.section)


#: Keys that give a container a HARD memory ceiling (the cgroup OOM-kills it
#: at the limit, whatever the host has free) or switch the killer off for it.
HARD_MEMORY_KEYS = ("mem_limit", "memswap_limit", "oom_kill_disable")


class NoServiceGainsAHardMemoryLimitTests(unittest.TestCase):
    def test_no_compose_file_gives_any_service_a_hard_memory_limit(self) -> None:
        """Measured 2026-09-13: no container on either node has a memory limit,
        and GB10 engine memory is unified and uncharged to the cgroup, so a
        limit would bound only the small host RSS -- a cgroup OOM kill inside
        the container at a number nobody measured, which is the outage this
        work exists to prevent. Ordering (oom_score_adj) is the mechanism; a
        limit may be added only with measured headroom, and this test is where
        that decision has to be made on purpose."""
        files = [REPO_ROOT / "compose.yaml", *sorted(COMPOSE.glob("*.yaml"))]
        offenders: list[str] = []
        for path in files:
            lines = path.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines, 1):
                if line.strip().startswith("#"):
                    continue
                key = re.match(r"^\s*-?\s*([a-z_]+)\s*:", line)
                if key is None:
                    continue
                if key.group(1) in HARD_MEMORY_KEYS:
                    offenders.append(f"{path.name}:{number}: {line.strip()}")
                elif key.group(1) == "memory":
                    # deploy.resources.limits.memory is the v3 spelling of mem_limit;
                    # a reservation is a soft floor and is not what this test forbids.
                    indent = len(line) - len(line.lstrip())
                    owner = next(
                        (prev for prev in reversed(lines[:number - 1])
                         if prev.strip() and not prev.strip().startswith("#") and len(prev) - len(prev.lstrip()) < indent),
                        "",
                    )
                    if owner.strip().startswith("limits:"):
                        offenders.append(f"{path.name}:{number}: limits.{line.strip()}")
        self.assertEqual(offenders, [])
        self.assertGreater(len(files), 10, "the glob found the compose overlays")


@unittest.skipUnless(overlays.COMPOSE_AVAILABLE, "Docker Compose v2.24+ is required")
class ComposeHashesTheSwitchTests(unittest.TestCase):
    """What `up -d` compares: Compose's own hash of the rendered definition."""

    def _hash(self, compose_text: str, service: str, env: dict[str, str], *, project_directory: Path | None = None,
              env_file: Path | None = None) -> tuple[str, dict]:
        with tempfile.TemporaryDirectory(prefix="techsara-oom-compose-") as temporary:
            path = Path(temporary) / "compose.yaml"
            path.write_text(compose_text, encoding="utf-8")
            base = ["docker", "compose", "--project-name", "techsara-oom-test"]
            if project_directory is not None:
                base += ["--project-directory", str(project_directory)]
            if env_file is not None:
                base += ["--env-file", str(env_file)]
            base += ["-f", str(path)]
            clean = {k: v for k, v in os.environ.items() if not k.startswith(("CLUSTER_", "VLLM_", "ENGINE_", "SENTINEL_"))}
            clean.update(env)
            rendered = subprocess.run(base + ["config", "--format", "json"], capture_output=True, text=True, env=clean, check=False)
            self.assertEqual(rendered.returncode, 0, rendered.stderr[-2000:])
            hashed = subprocess.run(base + ["config", f"--hash={service}"], capture_output=True, text=True, env=clean, check=False)
            self.assertEqual(hashed.returncode, 0, hashed.stderr[-2000:])
            name, _sep, digest = hashed.stdout.strip().partition(" ")
            self.assertEqual(name, service)
            return digest, json.loads(rendered.stdout)["services"][service]

    def test_compose_hashes_an_unset_empty_or_zero_switch_exactly_like_no_key(self) -> None:
        base = (
            "services:\n  engine:\n    image: busybox@sha256:" + "0" * 64 + "\n"
            "    command: [\"sleep\", \"1\"]\n"
        )
        without, _ = self._hash(base, "engine", {})
        for switch, key, applied in ((SWITCH, "ENGINE_OOM_SCORE_ADJ", RECOMMENDED_ENGINE_ADJ),
                                     (AUX_SWITCH, "AUX_ENGINE_OOM_SCORE_ADJ", RECOMMENDED_AUX_ENGINE_ADJ)):
            with_switch = base + f"    oom_score_adj: {switch}\n"
            for label, env in (("unset", {}), ("empty", {key: ""}), ("zero", {key: "0"})):
                with self.subTest(key=key, switch=label):
                    digest, service = self._hash(with_switch, "engine", env)
                    self.assertEqual(digest, without, "an inert switch must not be a definition change")
                    self.assertNotIn("oom_score_adj", service)
            with self.subTest(key=key, switch="set"):
                digest, service = self._hash(with_switch, "engine", {key: str(applied)})
                self.assertNotEqual(digest, without)
                self.assertEqual(service["oom_score_adj"], applied)

    def test_compose_renders_every_parity_line_to_the_number_the_worker_resolver_ships(self) -> None:
        """The other half of the resolver's parity test: what the HEAD renders
        from the same line through an --env-file (the launcher's layering)."""
        text = (
            "services:\n  engine:\n    image: busybox@sha256:" + "0" * 64 + "\n"
            f"    oom_score_adj: {SWITCH}\n"
        )
        with tempfile.TemporaryDirectory(prefix="techsara-oom-parity-") as temporary:
            for number, (line, value) in enumerate(ClusterSyncShipsTheSwitchTests.PARITY_LINES.items()):
                if "700" in value:
                    continue
                with self.subTest(line=line):
                    env_file = Path(temporary) / f"{number}.env"
                    env_file.write_bytes(line.encode("utf-8"))
                    _digest, service = self._hash(text, "engine", {}, env_file=env_file)
                    self.assertEqual(service["oom_score_adj"], int(value))

    def test_the_real_worker_file_is_unchanged_until_the_switch_is_set_and_the_sentinel_never_is(self) -> None:
        real = (COMPOSE / "compose.cluster-worker.yaml").read_text(encoding="utf-8")
        stripped = re.sub(r"^    oom_score_adj: .*\n", "", real, flags=re.M)
        self.assertEqual(real.count(SWITCH), 1)
        self.assertNotIn("oom_score_adj", stripped)
        worker_env = dict(overlays.ComposeOverlayValidationTests.WORKER_ENV, CLUSTER_WORKER_MODEL_CACHE="/srv/techsara/models")
        with tempfile.TemporaryDirectory(prefix="techsara-oom-env-") as temporary:
            unset_env = Path(temporary) / "worker.env"
            unset_env.write_text(render_env(worker_env), encoding="utf-8")
            set_env = Path(temporary) / "worker-set.env"
            set_env.write_text(render_env({**worker_env, "ENGINE_OOM_SCORE_ADJ": "-1000"}), encoding="utf-8")
            kwargs = {"project_directory": COMPOSE}
            before, _ = self._hash(stripped, "vllm-worker", {}, env_file=unset_env, **kwargs)
            after, rendered = self._hash(real, "vllm-worker", {}, env_file=unset_env, **kwargs)
            self.assertEqual(after, before, "merging the switch must not recreate the worker rank")
            self.assertNotIn("oom_score_adj", rendered)
            applied, rendered = self._hash(real, "vllm-worker", {}, env_file=set_env, **kwargs)
            self.assertNotEqual(applied, before)
            self.assertEqual(rendered["oom_score_adj"], -1000)
            sentinel_unset, _ = self._hash(real, "vllm-worker-sentinel", {}, env_file=unset_env, **kwargs)
            sentinel_set, _ = self._hash(real, "vllm-worker-sentinel", {}, env_file=set_env, **kwargs)
            self.assertEqual(sentinel_set, sentinel_unset, "the routine deploy's sentinel start must stay a no-op")


if __name__ == "__main__":
    unittest.main()
