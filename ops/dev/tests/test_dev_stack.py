"""The dev stack on the worker (ops/dev/): compose overlay, env files, scripts.

Stdlib only (unittest style, collected by pytest too). Nothing here talks to a
Docker daemon: the renders are `docker compose ... config` (no daemon), the
script tests put a stub `docker` first on PATH, and init-env.sh probes a fake
/v1/models server on 127.0.0.1. Every file is written under a temp directory;
nothing touches ops/dev/'s real env files.

    python3 -m pytest ops/dev/tests/test_dev_stack.py -q -p no:cacheprovider
"""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DEV = ROOT / "ops" / "dev"
INIT = DEV / "init-env.sh"
DEVSTACK = DEV / "devstack.sh"
OVERLAY = DEV / "compose.dev.yaml"
STACK_VARS = DEV / "stack.vars"

CAP = "http://inference-cap:9100/"
DISABLED_URL = "http://disabled.invalid/v1"
PREFIX = (
    "docker compose --env-file ops/dev/stack.vars --env-file ops/dev/.env "
    "-f compose.yaml -f ops/dev/compose.dev.yaml -p llmdev"
)
DEFAULT_SERVICES = {"postgres", "orchestrator", "frontend", "inference-cap"}
ENGINE_URL_KEYS = (
    "OPENAI_BASE_URL", "ROUTER_BASE_URL", "AGENT_BASE_URL", "VISION_BASE_URL",
    "EMBED_BASE_URL", "EMBED_VIA", "OCR_BASE_URL", "RERANK_BASE_URL",
)
MAIN_ID = "Qwen/Qwen3.6-35B-A3B-NVFP4"  # in config/model-manifest.yaml
ROUTER_ID = "Qwen/Qwen3-VL-8B-Instruct-FP8"  # in config/model-manifest.yaml
# Shell variables that outrank the dev files in Compose interpolation and that
# devstack.sh therefore refuses when they are set at all.
REFUSED_SHELL_VARS = (
    "POSTGRES_PASSWORD", "POSTGRES_USER", "POSTGRES_DB", "SESSION_SECRET", "API_KEY_PEPPER",
    "ORCHESTRATOR_PORT", "FRONTEND_PORT", "TECHSARA_BIND_ADDRESS", "OCR_REMOTE_BASE_URL",
)
# Variables that would steer a compose render or a script away from what the
# test means to check; removed from every child environment.
_STEERING = re.compile(r"^(TECHSARA_|COMPOSE_|DOCKER_|DEV_ENV_DIR$|DEV_INIT_|(%s)$)" % "|".join(REFUSED_SHELL_VARS))


def clean_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not _STEERING.match(k)}
    env.update(extra)
    return env


def compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        done = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True,
                              timeout=30, env=clean_env())
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def read_env(path: Path) -> dict:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeModels:
    """A /v1/models endpoint on 127.0.0.1, like vLLM's."""

    def __init__(self, model_id: str, window: int | None) -> None:
        self.model_id, self.window, self.hits = model_id, window, 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server API)
                outer.hits += 1
                if self.path != "/v1/models":
                    self.send_response(404)
                    self.end_headers()
                    return
                item = {"id": outer.model_id, "object": "model"}
                if outer.window is not None:
                    item["max_model_len"] = outer.window
                body = json.dumps({"object": "list", "data": [item]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "FakeModels":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


def run_init(out_dir: Path, *args: str, loopback: bool = True, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    extra = {"DEV_ENV_DIR": str(out_dir)}
    if loopback:
        extra["DEV_INIT_ALLOW_LOOPBACK"] = "1"
    return subprocess.run([str(INIT), *args], cwd=cwd, env=clean_env(**extra), capture_output=True,
                          text=True, timeout=90, stdin=subprocess.DEVNULL)


def env_paths(out_dir: Path) -> tuple[Path, Path, Path]:
    return out_dir / ".env", out_dir / ".runtime" / "orchestrator.env", out_dir / ".runtime" / "engines.env"


def compose_json(args: list[str], env: dict, *config_args: str) -> dict:
    done = subprocess.run(["docker", "compose", *args, "config", "--format", "json", *config_args], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise AssertionError(f"docker compose config failed: {done.stderr[-2000:]}")
    return json.loads(done.stdout)


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------
# (a) The dev render
# --------------------------------------------------------------------------
class DevRenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not compose_available():
            raise unittest.SkipTest("docker compose is not available")
        cls.tmp = Path(tempfile.mkdtemp(prefix="llmdev-render-"))
        with FakeModels(MAIN_ID, 262144) as main:
            done = run_init(cls.tmp, "--main", main.url)
        if done.returncode != 0:
            raise AssertionError(done.stderr)
        secrets_file, generated, engines = env_paths(cls.tmp)
        override = cls.tmp / "paths.vars"
        override.write_text(
            f"TECHSARA_SECRET_ENV={secrets_file}\n"
            f"TECHSARA_GENERATED_ENV={generated}\n"
            f"TECHSARA_DEV_ENGINES_ENV={engines}\n",
            encoding="utf-8",
        )
        cls.secrets_file, cls.generated = secrets_file, generated
        cls.args = ["--env-file", "ops/dev/stack.vars", "--env-file", str(secrets_file), "--env-file", str(override),
                    "-f", "compose.yaml", "-f", "ops/dev/compose.dev.yaml", "-p", "llmdev"]
        cls.doc = compose_json(cls.args, clean_env())

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def services(self) -> dict:
        return self.doc["services"]

    def test_project_is_llmdev_and_nothing_is_named_after_production(self) -> None:
        self.assertEqual(self.doc["name"], "llmdev")
        names = []
        for kind in ("volumes", "networks"):
            for key, val in (self.doc.get(kind) or {}).items():
                names.append((val or {}).get("name") or key)
        for svc in self.services().values():
            names += [svc.get("image"), svc.get("container_name")]
            names += [v.get("source") for v in svc.get("volumes") or [] if v.get("type") == "volume"]
        hits = [n for n in names if n and str(n).startswith("sf-local-ai")]
        self.assertEqual(hits, [])
        for key, val in self.doc["volumes"].items():
            self.assertTrue(val["name"].startswith("llmdev_"), (key, val))
        for key, val in self.doc["networks"].items():
            self.assertTrue(val["name"].startswith("llmdev"), (key, val))
        for name, svc in self.services().items():
            if "build" in svc:
                self.assertTrue(svc["image"].startswith("llmdev-"), (name, svc["image"]))

    def test_only_the_four_default_services_render(self) -> None:
        self.assertEqual(set(self.services()), DEFAULT_SERVICES)
        for absent in ("sync-worker", "v1-gateway", "pgadmin", "searxng"):
            self.assertNotIn(absent, self.services())

    def test_ports_bind_loopback_and_never_production_ports(self) -> None:
        published = []
        for name, svc in self.services().items():
            for port in svc.get("ports") or []:
                published.append((name, port))
                self.assertEqual(port.get("host_ip"), "127.0.0.1", (name, port))
                self.assertNotIn(str(port.get("published")), {"8080", "3000", "5432"}, (name, port))
        self.assertEqual(sorted((n, str(p["published"])) for n, p in published),
                         [("frontend", "23000"), ("orchestrator", "28080")])

    def test_every_service_has_limits_and_is_first_to_die(self) -> None:
        for name, svc in self.services().items():
            with self.subTest(service=name):
                self.assertTrue(svc.get("mem_limit"), name)
                self.assertTrue(svc.get("cpus"), name)
                self.assertTrue(svc.get("pids_limit"), name)
                self.assertEqual(svc.get("oom_score_adj"), 1000)
                self.assertEqual(svc.get("restart"), "no")
                self.assertEqual(str(svc.get("memswap_limit")), str(svc.get("mem_limit")))
        gib = 1024 ** 3
        expected = {"postgres": (2 * gib, 2), "orchestrator": (6 * gib, 4), "frontend": (gib, 2),
                    "inference-cap": (gib, 1)}
        for name, (mem, cpus) in expected.items():
            self.assertEqual(int(self.services()[name]["mem_limit"]), mem, name)
            self.assertEqual(float(self.services()[name]["cpus"]), float(cpus), name)

    def test_the_cap_has_room_for_its_queued_bodies(self) -> None:
        # The cap may buffer up to 512 MiB of queued request bodies
        # (CAP_MAX_BUFFERED_BYTES) plus the two requests in flight.
        cap = self.services()["inference-cap"]
        self.assertGreaterEqual(int(cap["mem_limit"]), 1024 ** 3)
        self.assertGreaterEqual(int(cap["memswap_limit"]), 1024 ** 3)

    def test_orchestrator_reads_only_the_two_dev_env_files(self) -> None:
        # Without `env_file: !override` compose.yaml's own list (the worktree
        # root's .env and the production secrets layer) would come back. A
        # plain render folds env files into `environment`; this one keeps them.
        doc = compose_json(self.args, clean_env(), "--no-env-resolution")
        entries = doc["services"]["orchestrator"]["env_file"]
        paths = [Path(e["path"] if isinstance(e, dict) else e).resolve() for e in entries]
        self.assertEqual(paths, [self.secrets_file.resolve(), self.generated.resolve()])
        for entry in entries:
            if isinstance(entry, dict):
                self.assertTrue(entry.get("required", True), entry)

    def test_no_bind_mounts_anywhere(self) -> None:
        for name, svc in self.services().items():
            for vol in svc.get("volumes") or []:
                self.assertEqual(vol.get("type"), "volume", (name, vol))

    def test_orchestrator_reaches_engines_only_through_the_cap(self) -> None:
        env = self.services()["orchestrator"]["environment"]
        for key in ENGINE_URL_KEYS:
            value = env.get(key) or ""
            self.assertTrue(value == "" or value.startswith(CAP) or value == DISABLED_URL, (key, value))
        for key in ("OPENAI_BASE_URL", "ROUTER_BASE_URL", "AGENT_BASE_URL", "VISION_BASE_URL"):
            self.assertTrue(env[key].startswith(CAP), (key, env[key]))
        self.assertNotIn("127.0.0.1", json.dumps(env))
        deps = self.services()["orchestrator"]["depends_on"]
        self.assertEqual(deps["inference-cap"]["condition"], "service_healthy")
        self.assertEqual(deps["postgres"]["condition"], "service_healthy")

    def test_orchestrator_dev_switches_hold(self) -> None:
        env = self.services()["orchestrator"]["environment"]
        expected = {"SF_LIVE_ENABLED": "false", "ASR_ENABLED": "false", "VIDEO_ANALYSIS_ENABLED": "false",
                    "VOICE_ARCHIVE_ENABLED": "false", "OCR_ENABLED": "false", "SEARCH_ENABLED": "false",
                    "HF_HUB_OFFLINE": "1", "OCR_BASE_URL": DISABLED_URL, "ENGINE_CONTROLLER_URL": "",
                    "SF_CLIENT_ID": "", "SF_CLIENT_SECRET": "", "SF_PRIVATE_KEY_B64": "", "VOICE_ARCHIVE_URL": "",
                    "ASR_BASE_URLS": ""}
        for key, value in expected.items():
            self.assertEqual(env.get(key), value, key)
        self.assertEqual(set(self.services()["orchestrator"]["networks"]), {"application", "inference"})
        self.assertIn("dev", self.services()["frontend"]["environment"]["NEXT_PUBLIC_APP_NAME"])
        self.assertEqual(self.services()["frontend"]["environment"]["V1_GATEWAY_URL"], "")

    def test_the_pinned_v1relay_subnet_is_not_created(self) -> None:
        for svc in self.services().values():
            self.assertNotIn("v1relay", svc.get("networks") or {})
        self.assertNotIn("v1relay", self.doc.get("networks") or {})

    def test_the_cap_is_locked_down_on_the_application_network(self) -> None:
        cap = self.services()["inference-cap"]
        self.assertEqual(set(cap["networks"]), {"application"})
        self.assertFalse(cap.get("ports"))
        self.assertTrue(cap.get("read_only"))
        self.assertEqual(cap.get("cap_drop"), ["ALL"])
        self.assertIn("no-new-privileges:true", cap.get("security_opt") or [])
        self.assertEqual(cap["image"], "llmdev-inference-cap:dev")
        self.assertTrue(cap["build"]["context"].endswith("ops/dev/inference_cap"), cap["build"])
        self.assertEqual(cap["environment"]["CAP_MAX_INFLIGHT"], "2")
        self.assertTrue(cap["environment"]["CAP_UPSTREAMS"].startswith("main=http://"))
        self.assertIn("_cap/health", json.dumps(cap["healthcheck"]["test"]))
        self.assertNotIn("inference", self.services()["frontend"].get("networks") or {})

    def test_a_hostile_shell_cannot_move_names_to_production(self) -> None:
        # A shell variable outranks --env-file in Compose interpolation; the
        # overlay pins the names as literals, so the default services stay dev.
        doc = compose_json(self.args, clean_env(TECHSARA_STACK="sf-local-ai", TECHSARA_BIND_ADDRESS="0.0.0.0",
                                                SF_LIVE_ENABLED="true", SF_CLIENT_SECRET="from-the-shell"))
        self.assertEqual(doc["name"], "llmdev")
        for key, val in doc["volumes"].items():
            self.assertTrue(val["name"].startswith("llmdev_"), (key, val))
        for name, svc in doc["services"].items():
            self.assertFalse(str(svc.get("image", "")).startswith("sf-local-ai"), name)
            for port in svc.get("ports") or []:
                self.assertEqual(port.get("host_ip"), "127.0.0.1", (name, port))
        env = doc["services"]["orchestrator"]["environment"]
        self.assertEqual(env["SF_LIVE_ENABLED"], "false")
        self.assertEqual(env["SF_CLIENT_SECRET"], "")

    def test_a_hostile_shell_cannot_move_ports_or_point_ocr_at_an_engine(self) -> None:
        # The published ports are literals and OCR_BASE_URL is a literal, so
        # the shell values below change nothing in the default services.
        doc = compose_json(self.args, clean_env(ORCHESTRATOR_PORT="8080", FRONTEND_PORT="3000",
                                                TECHSARA_BIND_ADDRESS="0.0.0.0",
                                                OCR_REMOTE_BASE_URL="http://192.0.2.10:1/v1"))
        published = sorted((name, port.get("host_ip"), str(port.get("published")), str(port.get("target")))
                           for name, svc in doc["services"].items() for port in svc.get("ports") or [])
        self.assertEqual(published, [("frontend", "127.0.0.1", "23000", "3000"),
                                     ("orchestrator", "127.0.0.1", "28080", "8080")])
        env = doc["services"]["orchestrator"]["environment"]
        self.assertEqual(env["OCR_BASE_URL"], DISABLED_URL)
        self.assertNotIn("192.0.2.10", json.dumps(doc["services"]))


# --------------------------------------------------------------------------
# (b) The production render is unchanged by ops/dev/
# --------------------------------------------------------------------------
class ProductionRenderTest(unittest.TestCase):
    def test_production_chain_still_renders_production(self) -> None:
        if not compose_available():
            self.skipTest("docker compose is not available")
        with tempfile.TemporaryDirectory(prefix="llmdev-prod-render-") as tmp:
            generated = Path(tmp) / "generated.vars"
            generated.write_text("OPENAI_BASE_URL=http://vllm:30000/v1\n", encoding="utf-8")
            interp = Path(tmp) / "interp.vars"
            interp.write_text(f"POSTGRES_PASSWORD=fake-not-a-secret\nTECHSARA_GENERATED_ENV={generated}\n",
                              encoding="utf-8")
            doc = compose_json(["--env-file", str(interp), "-f", "compose.yaml"], clean_env())
        self.assertEqual(doc["name"], "sf-local-ai")
        services = doc["services"]
        self.assertNotIn("inference-cap", services)
        self.assertIn("sync-worker", services)
        self.assertIn("v1-gateway", services)
        self.assertEqual(services["orchestrator"]["image"], "sf-local-ai-orchestrator:cpu")
        self.assertIn("v1relay", services["orchestrator"]["networks"])
        self.assertTrue(any(v.get("type") == "bind" and v.get("target") == "/data/brain"
                            for v in services["orchestrator"]["volumes"]))
        for key, val in doc["volumes"].items():
            self.assertTrue(val["name"].startswith("sf-local-ai_"), (key, val))
        for name, svc in services.items():
            self.assertNotEqual(svc.get("oom_score_adj"), 1000, name)
            self.assertNotEqual(svc.get("restart"), "no", name)
        subnet = doc["networks"]["v1relay"]["ipam"]["config"][0]["subnet"]
        self.assertEqual(subnet, "10.231.231.0/28")

    def test_stack_vars_names_only_dev_values(self) -> None:
        values = read_env(STACK_VARS)
        self.assertEqual(values["TECHSARA_STACK"], "llmdev")
        self.assertEqual(values["SF_LIVE_ENABLED"], "false")
        # The published ports are literals in the overlay; a value here would
        # only suggest a knob that does not move them.
        for key in ("ORCHESTRATOR_PORT", "FRONTEND_PORT", "TECHSARA_BIND_ADDRESS"):
            self.assertNotIn(key, values)
        for key in ("TECHSARA_SECRET_ENV", "TECHSARA_GENERATED_ENV", "TECHSARA_DEV_ENGINES_ENV"):
            self.assertTrue(values[key].startswith("ops/dev/"), key)
        # Non-empty values only for names, paths and switches: no hosts.
        text = STACK_VARS.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b(?!\.)", text.replace("127.0.0.1", "")))
        self.assertNotIn("://", text)

    def test_dev_env_files_are_gitignored(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        paths = ["ops/dev/.env", "ops/dev/.runtime/orchestrator.env", "ops/dev/.runtime/engines.env",
                 "ops/dev/.env.tmp-x", "ops/dev/.runtime/engines.env.tmp-x"]
        done = subprocess.run(["git", "check-ignore", *paths], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(sorted(done.stdout.split()), sorted(paths))


# --------------------------------------------------------------------------
# (c) init-env.sh
# --------------------------------------------------------------------------
class InitEnvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="llmdev-init-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def assert_refused(self, done: subprocess.CompletedProcess, needle: str) -> None:
        self.assertNotEqual(done.returncode, 0, done.stdout)
        self.assertIn(needle, done.stderr)
        self.assertEqual(list(self.tmp.iterdir()), [], "a refused run must write nothing")

    def test_main_only_writes_three_private_files(self) -> None:
        with FakeModels(MAIN_ID, 262144) as main:
            done = run_init(self.tmp, "--main", main.url)
        self.assertEqual(done.returncode, 0, done.stderr)
        secrets_file, generated, engines = env_paths(self.tmp)
        for path in (secrets_file, generated, engines):
            self.assertEqual(mode_of(path), 0o600, path)
        self.assertEqual(mode_of(self.tmp / ".runtime"), 0o700)
        self.assertEqual(sorted(p.name for p in (self.tmp / ".runtime").iterdir()),
                         ["engines.env", "orchestrator.env"])

        held = read_env(secrets_file)
        self.assertEqual(set(held), {"POSTGRES_PASSWORD", "SESSION_SECRET", "API_KEY_PEPPER"})
        for value in held.values():
            self.assertRegex(value, r"^[0-9a-f]{64}$")
        self.assertEqual(len(set(held.values())), 3)
        for value in held.values():
            self.assertNotIn(value, done.stdout + done.stderr)

        cap = read_env(engines)
        self.assertEqual(cap["CAP_UPSTREAMS"], f"main={main.url}")
        self.assertEqual(cap["CAP_MAX_INFLIGHT"], "2")

        gen = read_env(generated)
        self.assertEqual(gen["MAIN_MODEL"], MAIN_ID)
        for key in ("MODEL_MAX_CONTEXT", "DEFAULT_MAX_CONTEXT", "REPORT_MAX_CONTEXT", "MAIN_CONTEXT_LENGTH"):
            self.assertEqual(gen[key], "262144", key)
        self.assertEqual(gen["OPENAI_BASE_URL"], CAP + "main/v1")
        # router shared: router, agent and vision are the main model
        for role in ("ROUTER", "AGENT", "VISION"):
            self.assertEqual(gen[f"{role}_BASE_URL"], CAP + "main/v1", role)
            self.assertEqual(gen[f"{role}_MODEL"], MAIN_ID, role)
        # embed and rerank disabled, the launcher's way
        self.assertEqual(gen["EMBED_MODEL"], "disabled")
        self.assertEqual(gen["EMBED_BASE_URL"], DISABLED_URL)
        self.assertEqual(gen["EMBED_VIA"], DISABLED_URL)
        self.assertEqual(gen["EMBED_ENABLED"], "false")
        self.assertEqual(gen["RERANK_BACKEND"], "disabled")
        self.assertEqual(gen["RERANK_ENABLED"], "false")
        self.assertEqual(gen["RERANK_BASE_URL"], "")
        for key, value in {"OCR_ENABLED": "false", "SEARCH_ENABLED": "false", "ASR_ENABLED": "false",
                           "ENGINE_CONTROLLER_URL": "", "SF_LIVE_ENABLED": "false",
                           "WEB_KNOWLEDGE_WORKER_ENABLED": "false"}.items():
            self.assertEqual(gen[key], value, key)
        # the manifest's capabilities for the main model
        self.assertEqual(gen["MAIN_SUPPORTS_REASONING"], "true")
        self.assertEqual(gen["MAIN_EXTRA_BODY_ALLOWED"], "chat_template_kwargs")
        self.assertEqual(gen["MAIN_OUTPUT_LIMIT"], "8192")
        # every URL goes through the cap (or nowhere); no host address at all
        for key, value in gen.items():
            if "://" in value:
                self.assertTrue(value.startswith(CAP) or value == DISABLED_URL, (key, value))
        self.assertNotIn("127.0.0.1", generated.read_text(encoding="utf-8"))
        for role in ("EMBED", "OCR", "RERANKER"):
            self.assertGreaterEqual(int(gen[f"{role}_CONCURRENCY"]), 1, role)
        self.assertIn("router  shared", done.stdout)
        self.assertIn("embed   disabled", done.stdout)
        self.assertIn("rerank  disabled", done.stdout)

    def test_secrets_survive_a_second_run_and_a_missing_key_is_appended(self) -> None:
        with FakeModels(MAIN_ID, 262144) as main:
            self.assertEqual(run_init(self.tmp, "--main", main.url).returncode, 0)
            secrets_file = env_paths(self.tmp)[0]
            first = secrets_file.read_bytes()
            second = run_init(self.tmp, "--main", main.url)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(secrets_file.read_bytes(), first)
            self.assertIn("secrets kept", second.stdout)
            # An older file without the pepper keeps its two values and gains one.
            old = {k: v for k, v in read_env(secrets_file).items() if k != "API_KEY_PEPPER"}
            secrets_file.write_text("".join(f"{k}={v}\n" for k, v in old.items()), encoding="utf-8")
            third = run_init(self.tmp, "--main", main.url)
        self.assertEqual(third.returncode, 0, third.stderr)
        now = read_env(secrets_file)
        for key, value in old.items():
            self.assertEqual(now[key], value)
        self.assertRegex(now["API_KEY_PEPPER"], r"^[0-9a-f]{64}$")
        self.assertEqual(mode_of(secrets_file), 0o600)

    def test_every_engine_given_goes_through_the_cap_with_its_own_window(self) -> None:
        with FakeModels(MAIN_ID, 1048576) as main, FakeModels(ROUTER_ID, 65536) as router, \
                FakeModels("Qwen/Qwen3-Embedding-0.6B", 32768) as embed, \
                FakeModels("Qwen/Qwen3-Reranker-0.6B", None) as rerank:
            done = run_init(self.tmp, "--main", main.url + "/v1", "--router", router.url, "--embed", embed.url,
                            "--rerank", rerank.url)
        self.assertEqual(done.returncode, 0, done.stderr)
        _, generated, engines = env_paths(self.tmp)
        cap = read_env(engines)
        self.assertEqual(cap["CAP_UPSTREAMS"], f"main={main.url},router={router.url},embed={embed.url},"
                                               f"rerank={rerank.url}")
        gen = read_env(generated)
        self.assertEqual(gen["MODEL_MAX_CONTEXT"], "1048576")
        self.assertEqual(gen["ROUTER_BASE_URL"], CAP + "router/v1")
        self.assertEqual(gen["AGENT_BASE_URL"], CAP + "router/v1")
        self.assertEqual(gen["ROUTER_MODEL"], ROUTER_ID)
        self.assertEqual(gen["ROUTER_CONTEXT_LENGTH"], "65536")
        self.assertEqual(gen["ROUTER_SUPPORTS_REASONING"], "false")
        self.assertEqual(gen["ROUTER_REASONING_FIELD"], "none")
        self.assertEqual(gen["VISION_BASE_URL"], CAP + "main/v1")
        self.assertEqual(gen["EMBED_BASE_URL"], CAP + "embed/v1")
        self.assertEqual(gen["EMBED_VIA"], CAP + "embed/v1")
        self.assertEqual(gen["EMBED_MODEL"], "Qwen/Qwen3-Embedding-0.6B")
        self.assertEqual(gen["EMBED_SUPPORTS_EMBEDDINGS"], "true")
        self.assertEqual(gen["RERANK_BACKEND"], "remote")
        self.assertEqual(gen["RERANK_ENABLED"], "true")
        self.assertEqual(gen["RERANK_BASE_URL"], CAP + "rerank")
        self.assertEqual(gen["RERANK_MODEL"], "Qwen/Qwen3-Reranker-0.6B")
        self.assertEqual(gen["RERANKER_CONTEXT_LENGTH"], "32768")  # manifest limit when the probe has none
        self.assertNotIn("127.0.0.1", generated.read_text(encoding="utf-8"))
        for role in ("router", "embed", "rerank"):
            self.assertIn(f"{role}", done.stdout)

    def test_refuses_https(self) -> None:
        self.assert_refused(run_init(self.tmp, "--main", "https://192.0.2.10:30000"), "only http://")

    def test_refuses_credentials(self) -> None:
        self.assert_refused(run_init(self.tmp, "--main", "http://user:pw@192.0.2.10:30000"), "credentials")

    def test_refuses_loopback_without_the_test_flag(self) -> None:
        for url in ("http://127.0.0.1:30000", "http://localhost:30000", "http://[::1]:30000", "http://0.0.0.0:1"):
            with self.subTest(url=url):
                self.assert_refused(run_init(self.tmp, "--main", url, loopback=False), "loopback")
        self.assert_refused(run_init(self.tmp, "--main", "http://192.0.2.10:30000", "--router",
                                     "http://127.0.0.2:30002", loopback=False), "loopback")

    def test_refuses_a_dead_main(self) -> None:
        self.assert_refused(run_init(self.tmp, "--main", f"http://127.0.0.1:{free_port()}"), "--main: GET")

    def test_refuses_a_dead_optional_engine(self) -> None:
        with FakeModels(MAIN_ID, 262144) as main:
            done = run_init(self.tmp, "--main", main.url, "--router", f"http://127.0.0.1:{free_port()}")
        self.assert_refused(done, "--router: GET")

    def test_refuses_a_model_id_that_would_break_the_env_file(self) -> None:
        for bad in ("evil ${HOME}", "a#b", 'q"x', "line\nbreak"):
            with self.subTest(model_id=bad), FakeModels(bad, 4096) as main:
                self.assert_refused(run_init(self.tmp, "--main", main.url), "model id")

    def test_refuses_a_main_without_a_window(self) -> None:
        with FakeModels(MAIN_ID, None) as main:
            self.assert_refused(run_init(self.tmp, "--main", main.url), "max_model_len")

    def test_refuses_malformed_urls_and_arguments(self) -> None:
        cases = [
            (["--main", "http://192.0.2.10:30000/api"], "nothing else"),
            (["--main", "http://192.0.2.10:30000/v1?x=1"], "nothing else"),
            (["--main", "http://192.0.2.10"], "explicit port"),
            (["--main", "ftp://192.0.2.10:21"], "only http://"),
            (["--main", "http://bad_host:1"], "not a host"),
            (["--router", "http://192.0.2.10:30002"], "--main is required"),
            (["--main", "http://192.0.2.10:30000", "--bogus"], "unknown argument"),
            (["--main"], "needs a URL"),
        ]
        for args, needle in cases:
            with self.subTest(args=args):
                self.assert_refused(run_init(self.tmp, *args), needle)

    def test_refuses_outside_the_worktree_root(self) -> None:
        self.assert_refused(run_init(self.tmp, "--main", "http://192.0.2.10:30000", cwd=DEV), "worktree root")

    def test_generated_env_loads_in_the_orchestrator_settings(self) -> None:
        with FakeModels(MAIN_ID, 262144) as main:
            self.assertEqual(run_init(self.tmp, "--main", main.url).returncode, 0)
        gen = read_env(env_paths(self.tmp)[1])
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.tmp), "PYTHONDONTWRITEBYTECODE": "1", **gen}
        program = (
            "import json\n"
            "try:\n"
            "    from app.config import settings\n"
            "except ImportError as exc:\n"
            "    print(json.dumps({'skip': str(exc)})); raise SystemExit(0)\n"
            "caps = settings.model_capabilities\n"
            "print(json.dumps({'main': settings.openai_base_url, 'model': settings.llm_model,\n"
            "    'router': settings.router_base_url, 'embed_on': caps.embed.enabled,\n"
            "    'rerank': settings.rerank_backend.value, 'ocr': settings.ocr_enabled,\n"
            "    'controller': settings.engine_controller_url, 'window': settings.model_max_context,\n"
            "    'search': settings.search_enabled, 'reasoning': caps.main.supports_reasoning}))\n"
        )
        done = subprocess.run([sys.executable, "-c", program], cwd=ROOT / "orchestrator", env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr[-3000:])
        result = json.loads(done.stdout.strip().splitlines()[-1])
        if "skip" in result:
            self.skipTest(f"the orchestrator's dependencies are not importable here: {result['skip']}")
        self.assertEqual(result["main"], CAP + "main/v1")
        self.assertEqual(result["model"], MAIN_ID)
        self.assertEqual(result["router"], CAP + "main/v1")
        self.assertFalse(result["embed_on"])
        self.assertEqual(result["rerank"], "disabled")
        self.assertFalse(result["ocr"])
        self.assertEqual(result["controller"], "")
        self.assertEqual(result["window"], 262144)
        self.assertFalse(result["search"])
        self.assertTrue(result["reasoning"])


# --------------------------------------------------------------------------
# (d) devstack.sh, against a stub docker
# --------------------------------------------------------------------------
STUB_DOCKER = """#!/usr/bin/env python3
import json, os, sys
record = {"argv": sys.argv[1:], "docker_host": os.environ.get("DOCKER_HOST", "")}
if "exec" in sys.argv[1:]:
    record["stdin"] = sys.stdin.read()
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(record) + "\\n")
"""


class DevstackTest(unittest.TestCase):
    """devstack.sh runs from a scratch copy of the worktree layout, so its
    refusal checks see fake env files and never the real ops/dev ones."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="llmdev-devstack-"))
        self.tree = self.tmp / "tree"
        (self.tree / "ops" / "dev" / ".runtime").mkdir(parents=True)
        shutil.copy2(ROOT / "compose.yaml", self.tree / "compose.yaml")
        for name in ("compose.dev.yaml", "stack.vars", "devstack.sh"):
            shutil.copy2(DEV / name, self.tree / "ops" / "dev" / name)
        (self.tree / "ops" / "dev" / ".env").write_text("POSTGRES_PASSWORD=fake\n", encoding="utf-8")
        (self.tree / "ops" / "dev" / ".runtime" / "orchestrator.env").write_text("X=1\n", encoding="utf-8")
        (self.tree / "ops" / "dev" / ".runtime" / "engines.env").write_text("X=1\n", encoding="utf-8")
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        stub = self.bin / "docker"
        stub.write_text(STUB_DOCKER, encoding="utf-8")
        stub.chmod(0o755)
        self.log = self.tmp / "docker.log"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_devstack(self, *args: str, docker_host: str | None = "ssh://192.0.2.10", stdin: str | None = None,
                     **extra: str) -> subprocess.CompletedProcess:
        env = clean_env(PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", STUB_LOG=str(self.log), **extra)
        if docker_host is not None:
            env["DOCKER_HOST"] = docker_host
        return subprocess.run(["ops/dev/devstack.sh", *args], cwd=self.tree, env=env, capture_output=True,
                              text=True, timeout=60, input=stdin if stdin is not None else "")

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_refuses_without_docker_host(self) -> None:
        done = self.run_devstack("up", docker_host=None)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("DOCKER_HOST=ssh://", done.stderr)
        self.assertEqual(self.calls(), [])

    def test_refuses_a_local_socket(self) -> None:
        for host in ("unix:///var/run/docker.sock", "tcp://192.0.2.10:2375", "ssh://"):
            with self.subTest(host=host):
                done = self.run_devstack("up", docker_host=host)
                self.assertNotEqual(done.returncode, 0)
                self.assertEqual(self.calls(), [])

    def test_refuses_ssh_to_this_host(self) -> None:
        # The script cannot know the worker's name; it refuses only the names
        # that always mean this host (the guard pins the worker).
        for host in ("ssh://localhost", "ssh://LocalHost:22", "ssh://user@localhost", "ssh://localhost.localdomain",
                     "ssh://127.0.0.1", "ssh://user@127.0.0.2:2222", "ssh://0.0.0.0", "ssh://[::1]",
                     "ssh://user@[::1]:22", "ssh://user@", "ssh://:22"):
            with self.subTest(host=host):
                done = self.run_devstack("up", docker_host=host)
                self.assertEqual(done.returncode, 2, done.stderr)
                self.assertIn("devstack:", done.stderr)
                self.assertEqual(self.calls(), [])
        for host in ("ssh://192.0.2.10", "ssh://user@192.0.2.10:22", "ssh://worker.example"):
            with self.subTest(host=host):
                self.assertEqual(self.run_devstack("status", docker_host=host).returncode, 0, host)
        self.assertEqual(len(self.calls()), 3)

    def test_refuses_shell_values_that_outrank_the_dev_files(self) -> None:
        for key in REFUSED_SHELL_VARS:
            for value in ("x", ""):
                with self.subTest(key=key, value=value):
                    done = self.run_devstack("up", **{key: value})
                    self.assertEqual(done.returncode, 2, done.stderr)
                    self.assertIn(f"{key} is set in the environment", done.stderr)
        self.assertEqual(self.calls(), [])

    def test_up_uses_exactly_the_literal_flags(self) -> None:
        done = self.run_devstack("up")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls()
        base = PREFIX.split()[1:]
        self.assertEqual(calls[0]["argv"], base + ["up", "-d", "--build", "--wait", "--wait-timeout", "900"])
        self.assertEqual(calls[1]["argv"], base + ["ps"])
        for call in calls:
            self.assertEqual(call["docker_host"], "ssh://192.0.2.10")

    def test_down_never_removes_volumes_or_images(self) -> None:
        done = self.run_devstack("down")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls()
        self.assertEqual([c["argv"] for c in calls], [PREFIX.split()[1:] + ["down"]])
        for call in calls:
            for flag in ("-v", "--volumes", "--rmi"):
                self.assertNotIn(flag, call["argv"])
        self.assertNotEqual(self.run_devstack("down", "-v").returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_status_logs_and_smoke(self) -> None:
        base = PREFIX.split()[1:]
        self.assertEqual(self.run_devstack("status").returncode, 0)
        self.assertEqual(self.run_devstack("logs", "orchestrator").returncode, 0)
        self.assertNotEqual(self.run_devstack("logs", "--volumes").returncode, 0)
        smoke = self.run_devstack("smoke")
        self.assertEqual(smoke.returncode, 0, smoke.stderr)
        argvs = [c["argv"] for c in self.calls()]
        self.assertEqual(argvs[0], base + ["ps", "--all"])
        self.assertEqual(argvs[1], base + ["logs", "--no-color", "--tail", "200", "orchestrator"])
        execs = [a for a in argvs if "exec" in a]
        self.assertEqual([a[len(base) + 2] for a in execs], ["orchestrator", "frontend", "inference-cap"])
        for argv in argvs:
            self.assertEqual(argv[: len(base)], base)

    def test_seed_sends_the_password_on_stdin_only(self) -> None:
        password = "correct horse battery staple 42"
        done = self.run_devstack("seed", "alice", stdin=password + "\n")
        self.assertEqual(done.returncode, 0, done.stderr)
        (call,) = self.calls()
        argv = call["argv"]
        base = PREFIX.split()[1:]
        self.assertEqual(argv[: len(base)], base)
        self.assertEqual(argv[len(base): len(base) + 5], ["exec", "-T", "orchestrator", "python3", "-c"])
        self.assertEqual(argv[-1], "alice")
        self.assertIn("bootstrap_super_admin", argv[-2])
        self.assertNotIn(password, json.dumps(argv))
        self.assertEqual(call["stdin"], password + "\n")
        self.assertNotIn(password, done.stdout + done.stderr)

    def test_seed_refusals(self) -> None:
        self.assertNotEqual(self.run_devstack("seed", "alice", stdin="").returncode, 0)
        self.assertNotEqual(self.run_devstack("seed", "-rf", stdin="pw\n").returncode, 0)
        self.assertNotEqual(self.run_devstack("seed", "a b", stdin="pw\n").returncode, 0)
        self.assertNotEqual(self.run_devstack("seed", stdin="pw\n").returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_refuses_without_the_env_files(self) -> None:
        (self.tree / "ops" / "dev" / ".runtime" / "engines.env").unlink()
        done = self.run_devstack("up")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("init-env.sh", done.stderr)
        self.assertEqual(self.calls(), [])

    def test_refuses_a_steering_shell(self) -> None:
        for key, value in (("TECHSARA_STACK", "sf-local-ai"), ("TECHSARA_STACK", ""),
                           ("TECHSARA_GENERATED_ENV", ".runtime/generated.env"), ("COMPOSE_PROFILES", "admin")):
            with self.subTest(key=key, value=value):
                done = self.run_devstack("up", **{key: value})
                self.assertNotEqual(done.returncode, 0)
                self.assertIn(key, done.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.run_devstack("status", TECHSARA_STACK="llmdev").returncode, 0)

    def test_refuses_outside_the_worktree_root(self) -> None:
        env = clean_env(PATH=f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}", STUB_LOG=str(self.log),
                        DOCKER_HOST="ssh://192.0.2.10")
        done = subprocess.run([str(self.tree / "ops" / "dev" / "devstack.sh"), "up"], cwd=self.tree / "ops",
                              env=env, capture_output=True, text=True, timeout=60, input="")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("worktree root", done.stderr)
        self.assertEqual(self.calls(), [])

    def test_unknown_command(self) -> None:
        self.assertNotEqual(self.run_devstack("purge").returncode, 0)
        self.assertEqual(self.calls(), [])


# --------------------------------------------------------------------------
# Static checks of the script texts
# --------------------------------------------------------------------------
def code_lines(path: Path) -> list[str]:
    """Shell lines that are not comments (heredoc bodies included; they hold
    no docker command either)."""
    return [line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]


class ScriptTextTest(unittest.TestCase):
    def test_every_compose_call_has_the_literal_prefix(self) -> None:
        lines = [ln.strip() for ln in code_lines(DEVSTACK) if re.search(r"\bdocker\b", ln)]
        self.assertEqual(lines, [PREFIX + ' "$@"'])

    def test_devstack_never_steers_or_escapes(self) -> None:
        code = "\n".join(code_lines(DEVSTACK))
        self.assertIsNone(re.search(r"(^|[;&|]\s*)\s*(export\s+|declare\s+-x\s+)?DOCKER_HOST=", code, re.M))
        self.assertIsNone(re.search(r"\b(export|unset)\s+DOCKER_HOST", code))
        self.assertIsNone(re.search(r"(^|[;&|]\s*)\s*(cd|pushd|ssh|scp|sudo)\s", code, re.M))
        self.assertIsNone(re.search(r"docker\s+run|--volumes|\s-v(\s|$)|--rmi", code))
        down = [ln for ln in code_lines(DEVSTACK) if re.search(r"compose_dev\s+down", ln)]
        self.assertEqual([ln.strip() for ln in down], ["compose_dev down"])

    def test_init_env_runs_no_docker(self) -> None:
        code = "\n".join(code_lines(INIT))
        self.assertIsNone(re.search(r"\bdocker\b|\bssh\b|\bsudo\b", code))

    def test_scripts_parse_and_are_executable(self) -> None:
        for path in (INIT, DEVSTACK):
            with self.subTest(path=path.name):
                self.assertTrue(os.access(path, os.X_OK), path)
                done = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)
                if shutil.which("shellcheck"):
                    lint = subprocess.run(["shellcheck", "-x", str(path)], capture_output=True, text=True)
                    self.assertEqual(lint.returncode, 0, lint.stdout)

    def test_no_host_addresses_in_committed_files(self) -> None:
        # RFC 5737 documentation addresses and loopback only.
        allowed = re.compile(r"^(127\.0\.0\.\d+|0\.0\.0\.0|192\.0\.2\.\d+|198\.51\.100\.\d+|203\.0\.113\.\d+|"
                             r"10\.231\.231\.\d+)$")
        for path in (STACK_VARS, OVERLAY, INIT, DEVSTACK, DEV / "README.md", Path(__file__)):
            if not path.exists():
                continue
            for addr in re.findall(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])", path.read_text(encoding="utf-8")):
                self.assertRegex(addr, allowed, f"{path.name}: {addr}")


if __name__ == "__main__":
    unittest.main()
