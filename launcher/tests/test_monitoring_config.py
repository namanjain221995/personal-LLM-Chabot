"""Validation for the monitoring stack's configuration.

These tests exist because every one of them corresponds to a defect that was
actually hit while building this stack, and each would have been invisible
until a dashboard was silently empty:

* the head overlay was first invoked without the dgx-spark overlays, and
  ``docker compose up`` recreated the ORCHESTRATOR from the base file,
  downgrading it from the :cuda image to :cpu;
* node-exporter was first put on a bridge network, where its netdev collector
  reports the container's eth0 and misses the RoCE links entirely — while
  still reporting success;
* the RoCE rules first read netdev counters, which cannot see RDMA traffic at
  all and would have shown a flat-zero fabric under full load;
* the GPU exporter was first run without ``pid: host``, where
  ``--query-compute-apps`` returns an empty list and exit code 0.

Like test_compose_overlays.py, the compose checks shell out to real
``docker compose config`` rather than a hand parser. Everything here resolves
and reads files; nothing starts, pulls or mutates.
"""

from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests`
    from support import REPO_ROOT

MONITORING = REPO_ROOT / "monitoring"
HEAD_OVERLAY = REPO_ROOT / "compose" / "compose.monitoring.yaml"
WORKER_OVERLAY = REPO_ROOT / "compose" / "compose.monitoring-worker.yaml"


def _docker_compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return subprocess.run(
            ["docker", "compose", "version"], capture_output=True, timeout=30
        ).returncode == 0
    except Exception:  # noqa: BLE001
        return False


HAVE_COMPOSE = _docker_compose_available()

#: What `compose.yaml` demands that has NOTHING to do with monitoring. The env
#: handed to Compose below is deliberately scrubbed, so without these it aborts
#: on the FIRST thing it cannot resolve and every render here fails — which is
#: what these six tests had been doing on every CI run, unnoticed because the
#: job piped its output through `tail` and reported tail's exit status.
#:
#: `test_grafana_refuses_to_start_without_a_password` was the worst of it:
#: Compose failed on POSTGRES_PASSWORD before it ever reached the Grafana
#: guard, so the `assertIn` on the message failed and the test was RED — a
#: red that CI masked. Each test still omits the one variable it is about.
#:
#: `.runtime/generated.env` is `required: true` in the x-runtime-env anchor but
#: is written by `techsara up` and gitignored, so it never exists in a fresh
#: checkout or on CI. Point the documented override at an EMPTY file rather
#: than at `.env.example`, whose values would perturb the very settings these
#: tests assert on — the bind address above all.
_EMPTY_ENV_DIR = tempfile.mkdtemp(prefix="techsara-monitoring-test-")
_EMPTY_ENV_FILE = Path(_EMPTY_ENV_DIR) / "generated.env"
_EMPTY_ENV_FILE.write_text("")
atexit.register(shutil.rmtree, _EMPTY_ENV_DIR, True)

_REQUIRED_BY_BASE_COMPOSE = {
    "POSTGRES_PASSWORD": "test-only",
    "TECHSARA_GENERATED_ENV": str(_EMPTY_ENV_FILE),
}


def _render(files, env=None, profile="monitoring"):
    """`docker compose config --format json` over the given overlay chain.

    The profile is required: without it Compose omits every profile-gated
    service, which is exactly the set under test.
    """
    cmd = ["docker", "compose", "--project-name", "monitoring-test"]
    for f in files:
        cmd += ["-f", str(f)]
    if profile:
        cmd += ["--profile", profile]
    cmd += ["config", "--format", "json"]
    proc = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={
            **{"PATH": "/usr/bin:/bin:/usr/local/bin"},
            **_REQUIRED_BY_BASE_COMPOSE,
            **(env or {}),
        },
    )
    if proc.returncode != 0:
        raise AssertionError(f"compose config failed:\n{proc.stderr[:2000]}")
    return json.loads(proc.stdout)


def _uncommented(text: str) -> str:
    """Config lines only. The comments deliberately NAME the things these
    tests forbid (TECHSARA_BIND_ADDRESS, the RoCE subnets) in order to explain
    why they are forbidden, so a naive substring check matches the
    explanation instead of a violation."""
    out = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split(" #", 1)[0])
    return "\n".join(out)


@unittest.skipUnless(HAVE_COMPOSE, "docker compose unavailable")
class MonitoringComposeTests(unittest.TestCase):
    def test_head_overlay_renders(self):
        cfg = _render(
            [REPO_ROOT / "compose.yaml", HEAD_OVERLAY],
            env={"GRAFANA_ADMIN_PASSWORD": "test-only"},
        )
        for name in (
            "prometheus",
            "grafana",
            "node-exporter",
            "dgx-gpu-exporter",
            "cadvisor",
            "blackbox-exporter",
            "postgres-exporter",
            "data-stores-exporter",
        ):
            self.assertIn(name, cfg["services"], f"{name} missing from the overlay")

    def test_every_monitoring_service_is_behind_the_profile(self):
        # Otherwise `./techsara up` would start Grafana for everyone.
        cfg = _render(
            [REPO_ROOT / "compose.yaml", HEAD_OVERLAY],
            env={"GRAFANA_ADMIN_PASSWORD": "test-only"},
        )
        for name in (
            "prometheus",
            "grafana",
            "node-exporter",
            "dgx-gpu-exporter",
            "cadvisor",
            "blackbox-exporter",
            "postgres-exporter",
            "data-stores-exporter",
        ):
            self.assertEqual(
                cfg["services"][name].get("profiles"),
                ["monitoring"],
                f"{name} must be behind the monitoring profile",
            )

    def test_grafana_refuses_to_start_without_a_password(self):
        # The repo has no application login, so a defaulted admin password
        # would be a LAN-reachable admin account on first boot.
        with self.assertRaises(AssertionError) as ctx:
            _render([REPO_ROOT / "compose.yaml", HEAD_OVERLAY])
        self.assertIn("GRAFANA_ADMIN_PASSWORD", str(ctx.exception))

    def test_grafana_and_prometheus_bind_loopback_by_default(self):
        cfg = _render(
            [REPO_ROOT / "compose.yaml", HEAD_OVERLAY],
            env={"GRAFANA_ADMIN_PASSWORD": "test-only"},
        )
        for name in ("grafana", "prometheus"):
            for port in cfg["services"][name].get("ports", []):
                self.assertEqual(
                    port.get("host_ip"),
                    "127.0.0.1",
                    f"{name} must not publish beyond loopback by default",
                )

    def test_monitoring_does_not_inherit_the_app_bind_address(self):
        # TECHSARA_BIND_ADDRESS is 0.0.0.0 on this deployment; inheriting it
        # would publish Grafana to the LAN the moment the profile started.
        self.assertNotIn("TECHSARA_BIND_ADDRESS", _uncommented(HEAD_OVERLAY.read_text()))

    def test_node_exporter_uses_host_networking_on_both_nodes(self):
        # Its netdev collector is network-namespace scoped: on a bridge it
        # reports the container's eth0 and silently misses the RoCE links.
        for overlay in (HEAD_OVERLAY, WORKER_OVERLAY):
            head = overlay is HEAD_OVERLAY
            cfg = _render(
                [REPO_ROOT / "compose.yaml", overlay] if head else [overlay],
                env={"GRAFANA_ADMIN_PASSWORD": "test-only"},
                profile="monitoring" if head else None,
            )
            svc = cfg["services"]["node-exporter"]
            self.assertEqual(
                svc.get("network_mode"),
                "host",
                f"node-exporter in {overlay.name} must use host networking",
            )

    def test_gpu_exporter_shares_the_host_pid_namespace(self):
        # Without it `nvidia-smi --query-compute-apps` returns an EMPTY list
        # with exit code 0 - per-process GPU memory silently disappears.
        for overlay in (HEAD_OVERLAY, WORKER_OVERLAY):
            head = overlay is HEAD_OVERLAY
            cfg = _render(
                [REPO_ROOT / "compose.yaml", overlay] if head else [overlay],
                env={"GRAFANA_ADMIN_PASSWORD": "test-only"},
                profile="monitoring" if head else None,
            )
            self.assertEqual(
                cfg["services"]["dgx-gpu-exporter"].get("pid"),
                "host",
                f"dgx-gpu-exporter in {overlay.name} needs pid: host",
            )

    def test_worker_exporters_survive_a_reboot_of_spark_2(self):
        """Neither worker exporter may rely on Docker publishing a host port.

        Spark 2 rebooted on 2026-08-31 and came back reporting host metrics but
        NO GPU metrics, so every per-Spark GPU panel in Grafana showed one node.
        The GPU exporter had been published as `192.168.9.68:9835:9835`, and
        Docker starts containers before enP7s7 is assigned its address:

            failed to bind host port 192.168.9.68:9835/tcp:
            cannot assign requested address

        That failure happens in Docker's networking setup, BEFORE the process
        starts, so `restart: unless-stopped` never engaged and the container was
        left dead with RestartCount=0. node-exporter survived the same reboot
        because it binds the address itself, where a missing IP is an ordinary
        process failure the restart policy retries until the NIC is up.
        """
        cfg = _render([WORKER_OVERLAY], profile=None)
        for name in ("node-exporter", "dgx-gpu-exporter"):
            svc = cfg["services"][name]
            self.assertEqual(
                svc.get("network_mode"),
                "host",
                f"{name} must use host networking, not a published port",
            )
            self.assertFalse(
                svc.get("ports"),
                f"{name} must not publish a host port - it dies at boot",
            )
            self.assertEqual(
                svc.get("restart"),
                "unless-stopped",
                f"{name} must retry until the management NIC is up",
            )

    def test_the_worker_gpu_exporter_binds_the_management_ip_only(self):
        """Host networking must not become "listening on every interface".

        The RoCE addresses (10.100.184/185.x) carry NCCL tensor-parallel
        traffic; monitoring must not be reachable on the fabric it measures.
        """
        cfg = _render([WORKER_OVERLAY], profile=None)
        env = cfg["services"]["dgx-gpu-exporter"].get("environment") or {}
        self.assertIn(
            "DGX_GPU_EXPORTER_HOST",
            env,
            "with host networking the exporter must bind an explicit address, "
            "or it serves on 0.0.0.0 including the RoCE links",
        )
        self.assertNotIn("0.0.0.0", str(env.get("DGX_GPU_EXPORTER_HOST")))

    def test_the_worker_healthcheck_probes_the_address_it_binds(self):
        """A hard-coded 127.0.0.1 probe reports unhealthy forever once the
        listener moves off the loopback - which is what host networking did."""
        body = WORKER_OVERLAY.read_text()
        gpu = body.split("dgx-gpu-exporter:", 1)[1]
        self.assertNotIn(
            "http://127.0.0.1:9835/healthz",
            gpu,
            "the healthcheck must follow DGX_GPU_EXPORTER_HOST, not assume "
            "the loopback",
        )
        self.assertIn("DGX_GPU_EXPORTER_HOST", gpu)

    def test_worker_overlay_renders_standalone(self):
        cfg = _render([WORKER_OVERLAY], profile=None)
        self.assertEqual(
            sorted(cfg["services"]), ["dgx-gpu-exporter", "node-exporter"]
        )

    def test_images_are_digest_pinned(self):
        # House rule: reproducible pulls. python:3.12-slim is the documented
        # exception - the exporter is a bind-mounted script and the image is
        # only a Python runtime with nvidia-smi injected by the runtime.
        for overlay in (HEAD_OVERLAY, WORKER_OVERLAY):
            for line in overlay.read_text().splitlines():
                stripped = line.strip()
                if not stripped.startswith("image:"):
                    continue
                image = stripped.split("image:", 1)[1].strip()
                if image == "python:3.12-slim":
                    continue
                self.assertIn("@sha256:", image, f"{image} is not digest-pinned")


class PrometheusConfigTests(unittest.TestCase):
    """Text-level checks that do not need Prometheus itself."""

    def setUp(self):
        self.prom = (MONITORING / "prometheus" / "prometheus.yml").read_text()
        self.recording = (MONITORING / "prometheus" / "rules" / "recording.yml").read_text()
        self.alerts = (MONITORING / "prometheus" / "rules" / "alerts.yml").read_text()

    def test_both_sparks_are_scraped_for_gpu_and_host(self):
        for job_marker in ("node: spark-1", "node: spark-2"):
            self.assertIn(job_marker, self.prom)

    def test_scraping_never_uses_the_roce_subnets(self):
        # Monitoring must not share the fabric it measures.
        config_only = _uncommented(self.prom)
        for roce in ("10.100.184.", "10.100.185."):
            self.assertNotIn(
                roce, config_only, "scrape targets must use the management LAN"
            )

    def test_roce_rules_read_infiniband_not_netdev(self):
        # RDMA traffic is invisible to netdev counters: measured 335 MB on the
        # IB counters vs 170 kB and exactly 0 on netdev during one generation.
        self.assertIn("node_infiniband_port_data_transmitted_bytes_total", self.recording)
        self.assertIn("node_infiniband_port_data_received_bytes_total", self.recording)
        roce_block = self.recording[self.recording.index("ROCE FABRIC"):]
        roce_block = roce_block[: roce_block.index("# --------")] if "# --------" in roce_block else roce_block
        self.assertNotIn(
            "node_network_receive_bytes_total",
            roce_block,
            "RoCE rules must not use netdev counters",
        )

    def test_roce_rules_exclude_the_down_ports(self):
        # rocep1s0f0 / roceP2p1s0f0 are DOWN by design on this hardware.
        self.assertIn('device=~"rocep1s0f1|roceP2p1s0f1"', self.recording)

    def test_vllm_rules_are_never_split_per_node(self):
        # One EngineCore drives both TP ranks; a per-node split would invent a
        # distinction the metric does not have.
        vllm_block = self.recording[self.recording.index("- name: vllm"):]
        self.assertNotIn("by (node)", vllm_block)

    def test_alert_thresholds_match_the_hardware_limits(self):
        # GB10 slowdown 86 C, shutdown 90 C.
        self.assertIn("dgx_gpu_temperature_celsius > 80", self.alerts)
        self.assertIn("dgx_gpu_temperature_celsius > 88", self.alerts)

    def test_every_alert_has_a_summary(self):
        import re

        names = re.findall(r"- alert: (\w+)", self.alerts)
        self.assertGreater(len(names), 8)
        self.assertEqual(
            self.alerts.count("summary:"),
            len(names),
            "every alert needs a summary annotation",
        )


class DataStoreMonitoringTests(unittest.TestCase):
    """The database layer, added after the GPU/host layer was proven."""

    def setUp(self):
        self.overlay = HEAD_OVERLAY.read_text()

    def test_postgres_exporter_reuses_the_app_credential(self):
        # No second copy of the password anywhere: it reads the SAME variable
        # the postgres service itself reads.
        self.assertIn("DATA_SOURCE_PASS: ${POSTGRES_PASSWORD", self.overlay)

    def test_no_credential_is_hardcoded(self):
        import re

        for line in self.overlay.splitlines():
            if "DATA_SOURCE_PASS" in line or "POSTGRES_PASSWORD" in line:
                # Must be a variable reference, never a literal.
                self.assertIn("${", line, f"literal credential in: {line.strip()}")
        self.assertIsNone(
            re.search(r"DATA_SOURCE_URI:.*://[^$]*:[^@$]+@", self.overlay),
            "credentials must not be inlined into the DSN",
        )

    def test_data_volumes_are_mounted_read_only(self):
        # The exporter measures the stores; it must never be able to alter them.
        block = self.overlay[self.overlay.index("data-stores-exporter:"):]
        for mount in ("data:/data:ro", "reports:/reports:ro"):
            self.assertIn(mount, block, f"{mount} must be read-only")

    def test_data_store_exporter_is_stdlib_only(self):
        source = (
            MONITORING / "exporters" / "data-stores" / "data_stores_exporter.py"
        ).read_text()
        for third_party in ("import duckdb", "import lancedb", "import psycopg", "import prometheus_client"):
            self.assertNotIn(third_party, source)
        compile(source, "data_stores_exporter.py", "exec")

    def test_warehouse_snapshot_freshness_is_alerted(self):
        # The one failure this whole exporter exists to catch: a stale
        # snapshot serves old Salesforce answers while everything looks healthy.
        alerts = (MONITORING / "prometheus" / "rules" / "alerts.yml").read_text()
        self.assertIn("WarehouseSnapshotStale", alerts)
        self.assertIn('techsara_store_age_seconds{store="warehouse_snapshot"}', alerts)

    def test_postgres_is_scraped(self):
        prom = (MONITORING / "prometheus" / "prometheus.yml").read_text()
        self.assertIn("postgres-exporter:9187", prom)
        self.assertIn("data-stores-exporter:9836", prom)


class GrafanaProvisioningTests(unittest.TestCase):
    def test_dashboards_are_valid_json_with_stable_uids(self):
        seen = set()
        files = sorted((MONITORING / "grafana" / "dashboards").glob("*.json"))
        self.assertGreaterEqual(len(files), 6, "expected six dashboards")
        for path in files:
            doc = json.loads(path.read_text())  # raises on malformed JSON
            uid = doc.get("uid")
            self.assertTrue(uid, f"{path.name} has no uid")
            self.assertNotIn(uid, seen, f"duplicate dashboard uid {uid}")
            seen.add(uid)
            self.assertTrue(doc.get("title"), f"{path.name} has no title")
            self.assertTrue(doc.get("panels"), f"{path.name} has no panels")

    def test_every_panel_targets_the_provisioned_datasource(self):
        # A dashboard pointing at a missing datasource uid renders empty.
        for path in (MONITORING / "grafana" / "dashboards").glob("*.json"):
            doc = json.loads(path.read_text())
            for panel in doc["panels"]:
                for target in panel.get("targets", []):
                    self.assertEqual(
                        target.get("datasource", {}).get("uid"),
                        "dgx-prometheus",
                        f"{path.name}/{panel.get('title')} uses a foreign datasource",
                    )

    def test_datasource_uid_matches_what_dashboards_reference(self):
        ds = (
            MONITORING / "grafana" / "provisioning" / "datasources" / "prometheus.yml"
        ).read_text()
        self.assertIn("uid: dgx-prometheus", ds)

    def test_dashboard_provider_points_at_the_mounted_path(self):
        provider = (
            MONITORING / "grafana" / "provisioning" / "dashboards" / "dashboards.yml"
        ).read_text()
        self.assertIn("/var/lib/grafana/dashboards", provider)


class ExporterTests(unittest.TestCase):
    def test_exporter_is_importable_stdlib_only(self):
        """It runs on a stock python:3.12-slim with no pip install."""
        source = (
            MONITORING / "exporters" / "dgx-gpu" / "dgx_gpu_exporter.py"
        ).read_text()
        for third_party in ("import prometheus_client", "import requests", "import psutil"):
            self.assertNotIn(third_party, source)
        compile(source, "dgx_gpu_exporter.py", "exec")  # syntax check

    def test_exporter_never_fabricates_unavailable_metrics(self):
        # GB10 reports N/A for framebuffer memory; exporting it as 0 would be
        # a lie, so those names must not exist at all.
        source = (
            MONITORING / "exporters" / "dgx-gpu" / "dgx_gpu_exporter.py"
        ).read_text()
        for forbidden in (
            "dgx_gpu_memory_total_bytes",
            "dgx_gpu_memory_used_bytes",
            "dgx_gpu_memory_free_bytes",
            "dgx_gpu_power_limit_watts",
            "dgx_gpu_fan_speed",
        ):
            self.assertNotIn(forbidden, source)


class ScriptTests(unittest.TestCase):
    def test_monitoring_script_is_executable_and_parses(self):
        script = REPO_ROOT / "scripts" / "monitoring.sh"
        self.assertTrue(script.exists())
        proc = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_monitoring_script_layers_the_full_overlay_chain(self):
        # Invoking compose with a SUBSET of the project's files makes `up`
        # recreate unrelated services from the base definition - it silently
        # downgraded the orchestrator from :cuda to :cpu once.
        text = (REPO_ROOT / "scripts" / "monitoring.sh").read_text()
        for overlay in (
            "compose.yaml",
            "compose/compose.dgx-spark.yaml",
            "compose/compose.cluster-dgx-spark.yaml",
            "compose/compose.monitoring.yaml",
        ):
            self.assertIn(overlay, text)

    def test_monitoring_script_never_prints_the_password(self):
        text = (REPO_ROOT / "scripts" / "monitoring.sh").read_text()
        self.assertNotIn("echo $GRAFANA_ADMIN_PASSWORD", text)
        self.assertNotIn('echo "$GRAFANA_ADMIN_PASSWORD"', text)


if __name__ == "__main__":
    unittest.main()
