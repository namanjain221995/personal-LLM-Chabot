"""spark-probe and spark-local: what the fleet dashboards are built on.

Each of these guards a way the numbers could lie or a secret could leak. Docker
state files, cgroups, /proc and the database protocols are faked on disk and on a
loopback socket, so nothing here needs Docker, root or a real database.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

try:
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests`
    from support import REPO_ROOT

PROBE_DIR = REPO_ROOT / "monitoring" / "exporters" / "spark-probe"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, PROBE_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    # dataclass-free, but `from __future__ import annotations` modules still expect this.
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


probe = _load("spark_probe")
local = _load("spark_local")

SECRET = "s3cret-Pass-Do-Not-Leak"
OTHER_SECRET = "OPENAI_KEY_never_kept"


def config(name="/db-1", image="postgres:17.11-alpine", project="stagevote", service="db", *, running=True, env=None, cmd=None,
           started="2026-11-05T02:08:22.123456789Z", restarts=0, manual=False, networks=None, ports=None, mounts=None, cid=None) -> dict:
    return {
        "ID": cid or ("a" * 64),
        "Name": name,
        "RestartCount": restarts,
        "HasBeenManuallyStopped": manual,
        "State": {"Running": running, "Restarting": False, "Paused": False, "Dead": False, "Pid": 4242 if running else 0,
                  "ExitCode": 0 if running else 137, "OOMKilled": False, "StartedAt": started,
                  "FinishedAt": "0001-01-01T00:00:00Z", "Health": {"Status": "starting", "FailingStreak": 0}},
        "Config": {"Image": image, "Labels": {"com.docker.compose.project": project, "com.docker.compose.service": service} if project else {},
                   "Env": env if env is not None else [f"POSTGRES_PASSWORD={SECRET}", "POSTGRES_USER=app", "PG_VERSION=17.11-1.pgdg13+1", f"OPENAI_API_KEY={OTHER_SECRET}"],
                   "Cmd": cmd or ["postgres"]},
        "NetworkSettings": {"Networks": networks if networks is not None else {"app": {"IPAddress": "172.18.0.5"}},
                            "Ports": ports if ports is not None else {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55433"}]}},
        "MountPoints": mounts if mounts is not None else {"/var/lib/postgresql/data": {"Name": "stagevote_pgdata", "Type": "volume"}},
    }


HOSTCONFIG = {"RestartPolicy": {"Name": "unless-stopped"}, "NetworkMode": "app"}


class ExpositionTests(unittest.TestCase):
    def test_labels_are_escaped_and_families_grouped(self):
        exp = probe.Exposition()
        exp.gauge("m", "help", 1, a='x"y\\z\nw')
        exp.gauge("m", "help", 2, a="b")
        text = exp.render()
        self.assertIn(r'm{a="x\"y\\z\nw"} 1', text)
        self.assertEqual(text.count("# TYPE m gauge"), 1)

    def test_an_absent_reading_is_left_out_not_written_as_zero(self):
        exp = probe.Exposition()
        for bad in (None, float("nan"), float("inf"), "not a number"):
            exp.gauge("m", "help", bad)
        self.assertNotIn("\nm ", "\n" + exp.render())

    def test_a_repeated_label_set_cannot_blank_the_file(self):
        # node-exporter drops a file with duplicates, which would lose everything.
        exp = probe.Exposition()
        exp.gauge("m", "help", 1, a="b")
        exp.gauge("m", "help", 2, a="b")
        self.assertEqual(exp.render().count('m{a="b"}'), 1)

    def test_a_label_called_kind_does_not_collide_with_the_metric_type(self):
        # Found running it: a label named `kind` hit a parameter of the same name.
        exp = probe.Exposition()
        exp.counter("spark_kernel_events_total", "help", 1, kind="gpu_xid")
        self.assertIn('spark_kernel_events_total{kind="gpu_xid"} 1', exp.render())


class DetectionTests(unittest.TestCase):
    def test_databases_are_recognised_from_the_image(self):
        for image, engine in (
            ("postgres:18-alpine", "postgres"),
            ("postgres@sha256:9a8afca5", "postgres"),
            ("pgvector/pgvector:0.8.7-pg17-trixie", "postgres"),
            ("timescale/timescaledb:latest-pg16", "postgres"),
            ("docker.io/library/redis:8.8.3", "redis"),
            ("valkey/valkey:8", "redis"),
            ("mariadb:11", "mysql"),
            ("mongo:7", "mongodb"),
            ("clickhouse/clickhouse-server:24", "clickhouse"),
            ("qdrant/qdrant:v1.9", "qdrant"),
        ):
            detected = probe.detect_engine(image)
            self.assertIsNotNone(detected, image)
            self.assertEqual(detected[0], engine, image)

    def test_tools_named_after_a_database_are_not_databases(self):
        for image in ("quay.io/prometheuscommunity/postgres-exporter:v0.20.1", "oliver006/redis_exporter:v1.92.1-alpine",
                      "dpage/pgadmin4", "adminer:4", "edoburu/pgbouncer", "python:3.12-slim", "grafana/grafana:11.5.1"):
            self.assertIsNone(probe.detect_engine(image), image)

    def test_postgres_process_titles_give_connection_states_with_no_login(self):
        for title, want in (
            (b"postgres: app appdb 172.18.0.6(40412) idle\0", "idle"),
            (b"postgres: app appdb 172.18.0.6(40412) idle in transaction\0", "idle_in_transaction"),
            (b"postgres: app appdb 172.18.0.6(40412) SELECT\0", "active"),
            (b"postgres: app appdb [local] UPDATE waiting\0", "active"),
            (b"postgres: prod: app appdb 172.18.0.6(40412) idle\0", "idle"),
            (b"postgres: checkpointer \0", None),
            (b"postgres: background writer \0", None),
            (b"postgres: walwriter \0", None),
            (b"postgres: autovacuum launcher \0", None),
            (b"postgres: logical replication launcher \0", None),
            (b"postgres: io worker 2\0", None),
            (b"postgres\0-c\0shared_buffers=512MB\0", None),
        ):
            self.assertEqual(probe.classify_pg_title(title), want, title)

    def test_no_titles_means_no_count_rather_than_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            cg = Path(tmp, "cg")
            cg.mkdir()
            (cg / "cgroup.procs").write_text("7\n")
            Path(tmp, "proc", "7").mkdir(parents=True)
            Path(tmp, "proc", "7", "cmdline").write_bytes(b"/usr/lib/postgresql/bin/postgres\0-D\0/data\0")
            self.assertIsNone(probe.pg_connections_from_titles(str(cg), str(Path(tmp, "proc"))))


class ContainerParsingTests(unittest.TestCase):
    def parse(self, cfg=None, host=None, allowed="sf-local-ai"):
        return probe.parse_container("a" * 64, cfg or config(), host or HOSTCONFIG, allowed)

    def test_another_projects_password_is_never_held(self):
        c = self.parse()
        self.assertEqual(c.engine, "postgres")
        self.assertIsNone(c.login)

    def test_a_password_is_held_only_for_an_allowed_project(self):
        c = self.parse(config(project="sf-local-ai"))
        self.assertEqual(c.login["POSTGRES_PASSWORD"], SECRET)
        c = self.parse(allowed="*")
        self.assertEqual(c.login["POSTGRES_PASSWORD"], SECRET)
        c = self.parse(config(project=""), allowed="standalone")
        self.assertEqual(c.login["POSTGRES_PASSWORD"], SECRET)

    def test_nothing_else_from_the_environment_survives_parsing(self):
        c = self.parse(config(project="sf-local-ai"))
        blob = json.dumps({slot: str(getattr(c, slot)) for slot in c.__slots__})
        self.assertNotIn(OTHER_SECRET, blob)
        self.assertNotIn("OPENAI_API_KEY", blob)

    def test_a_non_database_container_keeps_no_environment_at_all(self):
        c = self.parse(config(image="nginx:1", project="sf-local-ai", env=[f"POSTGRES_PASSWORD={SECRET}"]))
        self.assertEqual(c.engine, "")
        self.assertIsNone(c.login)

    def test_redis_requirepass_is_read_from_the_command_only_when_allowed(self):
        redis = config(image="redis:8.8.3", env=["REDIS_VERSION=8.8.3"], cmd=["redis-server", "--requirepass", SECRET])
        self.assertIsNone(self.parse(redis).login)
        self.assertEqual(self.parse(redis, allowed="*").login["REDIS_PASSWORD"], SECRET)

    def test_version_comes_from_the_image_environment_without_a_login(self):
        self.assertEqual(self.parse().version, "17.11")

    def test_docker_times(self):
        self.assertAlmostEqual(probe.parse_docker_time("2026-11-05T02:08:22.5Z"), 1793844502.5 - 0, delta=86400 * 2)
        self.assertIsNone(probe.parse_docker_time("0001-01-01T00:00:00Z"))
        self.assertIsNone(probe.parse_docker_time(None))
        self.assertIsNone(probe.parse_docker_time("yesterday"))

    def test_a_leftover_is_not_expected_to_run_but_a_crash_is(self):
        c = self.parse(config(running=False, manual=True))
        self.assertFalse(c.should_be_running())
        c = self.parse(config(running=False))
        self.assertTrue(c.should_be_running())
        c = self.parse(config(running=False), {"RestartPolicy": {"Name": "no"}, "NetworkMode": "bridge"})
        self.assertFalse(c.should_be_running())


class DockerStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def add(self, cid, cfg):
        d = self.root / "containers" / cid
        d.mkdir(parents=True)
        (d / "config.v2.json").write_text(json.dumps(cfg))
        (d / "hostconfig.json").write_text(json.dumps(HOSTCONFIG))

    def test_a_removed_container_disappears_and_a_broken_one_does_not_stop_the_rest(self):
        self.add("a" * 64, config(cid="a" * 64))
        self.add("b" * 64, config(name="/db-2", cid="b" * 64))
        bad = self.root / "containers" / ("c" * 64)
        bad.mkdir()
        (bad / "config.v2.json").write_text("{not json")
        (bad / "hostconfig.json").write_text("{}")
        state = probe.DockerState(str(self.root))
        self.assertEqual(sorted(c.name for c in state.load()), ["db-1", "db-2"])
        import shutil
        shutil.rmtree(self.root / "containers" / ("b" * 64))
        self.assertEqual([c.name for c in state.load()], ["db-1"])

    def test_an_unchanged_file_is_not_parsed_twice(self):
        self.add("a" * 64, config())
        state = probe.DockerState(str(self.root))
        first = state.load()[0]
        self.assertIs(state.load()[0], first)

    def test_a_missing_docker_root_is_an_error_not_an_empty_machine(self):
        # "No containers" and "cannot see containers" must not look the same.
        with self.assertRaises(OSError):
            probe.DockerState(str(self.root / "nope")).load()


class ContainerMetricsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cgroup = root / "cg"
        self.proc = root / "proc"
        scope = self.cgroup / "system.slice" / f"docker-{'a' * 64}.scope"
        scope.mkdir(parents=True)
        (scope / "cpu.stat").write_text("usage_usec 2500000\nthrottled_usec 500000\n")
        (scope / "memory.current").write_text("1000\n")
        (scope / "memory.stat").write_text("anon 300\nfile 600\ninactive_file 400\n")
        (scope / "memory.max").write_text("max\n")
        (scope / "memory.events").write_text("oom_kill 2\n")
        (scope / "pids.current").write_text("9\n")
        (scope / "io.stat").write_text("259:0 rbytes=100 wbytes=200 rios=1 wios=2\n259:1 rbytes=1 wbytes=2\n")
        net = self.proc / "4242" / "net"
        net.mkdir(parents=True)
        (net / "dev").write_text(
            "Inter-|   Receive\n face |bytes\n"
            "    lo: 999 1 0 0 0 0 0 0 999 1 0 0 0 0 0 0\n"
            "  eth0: 10 1 0 0 0 0 0 0 20 1 0 0 0 0 0 0\n"
        )

    def collect(self, c):
        exp = probe.Exposition()
        probe.collect_containers(exp, [c], str(self.cgroup), str(self.proc))
        return exp.render()

    def parse(self, cfg, host=None):
        return probe.parse_container("a" * 64, cfg, host or HOSTCONFIG)

    def test_usage_comes_from_the_cgroup_in_base_units(self):
        text = self.collect(self.parse(config()))
        for line in (
            'spark_container_cpu_seconds_total{container="db-1",project="stagevote",service="db"} 2.5',
            'spark_container_memory_working_set_bytes{container="db-1",project="stagevote",service="db"} 600',
            'spark_container_oom_kills_total{container="db-1",project="stagevote",service="db"} 2',
            'spark_container_disk_written_bytes_total{container="db-1",project="stagevote",service="db"} 202',
            'spark_container_network_received_bytes_total{container="db-1",project="stagevote",service="db"} 10',
        ):
            self.assertIn(line, text)

    def test_no_limit_is_absent_rather_than_zero(self):
        self.assertNotIn("spark_container_memory_limit_bytes{", self.collect(self.parse(config())))

    def test_a_host_network_container_claims_no_traffic(self):
        text = self.collect(self.parse(config(), {"RestartPolicy": {"Name": "no"}, "NetworkMode": "host"}))
        self.assertNotIn("spark_container_network_received_bytes_total{", text)

    def test_a_stopped_container_is_listed_with_no_usage(self):
        text = self.collect(self.parse(config(running=False)))
        self.assertIn('spark_container_running{container="db-1",project="stagevote",service="db"} 0', text)
        self.assertNotIn("spark_container_cpu_seconds_total{", text)
        self.assertIn('spark_containers{status="exited"} 1', text)

    def test_the_stale_health_in_the_state_file_is_not_believed(self):
        # Docker leaves "starting" in config.v2.json long after a container goes
        # healthy, so the verdict must only ever come from spark_local.
        self.assertNotIn("spark_container_health_status", self.collect(self.parse(config())))

    def test_gpu_memory_is_attributed_through_the_cgroup(self):
        for pid, cgroup in (("100", f"0::/system.slice/docker-{'a' * 64}.scope\n"), ("200", "0::/user.slice/session-1.scope\n")):
            d = self.proc / pid
            d.mkdir(exist_ok=True)
            (d / "cgroup").write_text(cgroup)
        body = ('dgx_gpu_process_memory_bytes{pid="100",process="VLLM::Worker_TP"} 2048\n'
                'dgx_gpu_process_memory_bytes{pid="200",process="python3"} 1024\n')
        exp = probe.Exposition()
        probe.collect_gpu_owners(exp, [self.parse(config())], "http://unused", str(self.proc), fetch=lambda _u: body)
        text = exp.render()
        self.assertIn('spark_container_gpu_memory_bytes{container="db-1",project="stagevote",service="db"} 2048', text)
        self.assertIn("spark_host_gpu_memory_bytes 1024", text)


class ProcessTests(unittest.TestCase):
    def test_cpu_is_a_rate_and_an_exiting_process_cannot_go_negative(self):
        scanner = probe.ProcessScanner()
        exp = probe.Exposition()
        scanner.collect(exp, [(1, 0, "vllm", 100, 4096), (2, 0, "vllm", 200, 4096)], now=0.0)
        self.assertNotIn("spark_process_cpu_cores{", exp.render())
        exp = probe.Exposition()
        ticks = probe.CLOCK_TICKS
        scanner.collect(exp, [(1, 0, "vllm", 100 + 10 * ticks, 4096)], now=10.0)
        text = exp.render()
        self.assertIn('spark_process_cpu_cores{comm="vllm"} 1', text)
        self.assertIn('spark_process_rss_bytes{comm="vllm"} 4096', text)

    def test_the_label_set_stays_small_however_many_names_exist(self):
        scanner = probe.ProcessScanner()
        exp = probe.Exposition()
        scanner.collect(exp, [(i, 0, f"proc{i}", i, i) for i in range(500)], now=0.0, top=12)
        # The top few by memory plus the top few by CPU.
        self.assertLessEqual(exp.render().count("spark_process_rss_bytes{"), 24)

    def test_a_native_postgres_is_found_and_its_workers_are_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pid, cgroup in (("10", "0::/system.slice/postgresql.service\n"), ("11", "0::/system.slice/postgresql.service\n"),
                                ("20", f"0::/system.slice/docker-{'a' * 64}.scope\n")):
                Path(tmp, pid).mkdir()
                Path(tmp, pid, "cgroup").write_text(cgroup)
            rows = [(10, 1, "postgres", 0, 0), (11, 10, "postgres", 0, 0), (20, 1, "postgres", 0, 0), (1, 0, "systemd", 0, 0)]
            found = probe.native_instances(rows, tmp)
            self.assertEqual([c.name for c in found], ["host-postgres"])
            self.assertEqual(found[0].pid, 10)


class KernelTests(unittest.TestCase):
    def test_real_faults_are_counted(self):
        for message, kind in (
            ("NVRM: Xid (PCI:0000:01:00): 79, pid=12, GPU has fallen off the bus.", "gpu_xid"),
            ("Out of memory: Killed process 123 (python)", "oom_kill"),
            ("INFO: task kworker:1 blocked for more than 120 seconds.", "hung_task"),
            ("watchdog: BUG: soft lockup - CPU#3 stuck for 22s!", "lockup"),
            ("pcieport 0000:00:00.0: AER: Corrected error received: 0000:01:00.0", "pcie_aer"),
            ("nvme nvme0: I/O 12 QID 3 timeout, aborting", "nvme_error"),
            ("EXT4-fs error (device nvme0n1p2): ext4_lookup", "fs_error"),
            ("mlx5_core 0000:01:00.1: mlx5_health_try_recover:0: health recovery flow aborted", "roce_error"),
        ):
            self.assertIn(kind, probe.classify_kernel_message(message), message)

    def test_what_every_boot_prints_is_not_a_fault(self):
        # All three were counted on the worker the first time this ran.
        for message in ("pcieport 0002:00:00.0: AER: enabled with IRQ 277",
                        "watchdog: NMI not fully supported",
                        "watchdog: Hard watchdog permanently disabled"):
            self.assertEqual(probe.classify_kernel_message(message), [], message)

    def test_a_process_cannot_report_a_fault_by_choosing_its_name(self):
        # A crash line quotes the program's own name, up to 15 characters.
        for message in ("NVRM: Xid (a):7[123]: segfault at 0 ip 0 sp 0 error 4",
                        "EXT4-fs error[9]: segfault at 0 ip 0 sp 0 error 4",
                        "Out of memory: Killed process 123 (NVRM: Xid (a):7)"):
            self.assertNotIn("gpu_xid", probe.classify_kernel_message(message), message)
            self.assertNotIn("fs_error", probe.classify_kernel_message(message), message)
        self.assertEqual(probe.classify_kernel_message("NVRM: Xid (a):7[123]: segfault at 0 ip 0"), ["segfault"])
        self.assertEqual(probe.classify_kernel_message("Out of memory: Killed process 123 (x)"), ["oom_kill"])

    def test_only_the_kernel_counts(self):
        events = probe.KernelEvents("/nonexistent")
        events.record("30,1,5000000,-;NVRM: Xid (PCI:0000:01:00): 79, from userspace")  # facility 3
        self.assertEqual(events.counts["gpu_xid"], 0)
        events.record("3,2,5000000,-;NVRM: Xid (PCI:0000:01:00): 79, pid=1")
        self.assertEqual(events.counts["gpu_xid"], 1)
        self.assertEqual(events.xid, {"79": 1})

    def test_every_kind_starts_at_zero_so_the_first_event_is_an_increase(self):
        events = probe.KernelEvents("/nonexistent")
        events.available = True
        exp = probe.Exposition()
        events.collect(exp)
        text = exp.render()
        for kind in probe.KERNEL_PATTERNS:
            self.assertIn(f'spark_kernel_events_total{{kind="{kind}"}} 0', text)

    def test_an_unreadable_kernel_log_fails_the_collector_instead_of_reading_zero(self):
        events = probe.KernelEvents("/nonexistent")
        with self.assertRaises(RuntimeError):
            events.collect(probe.Exposition())


class RoceTests(unittest.TestCase):
    def test_only_active_ports_and_known_counters(self):
        with tempfile.TemporaryDirectory() as tmp:
            for device, state in (("rocep1s0f1", "4: ACTIVE"), ("rocep1s0f0", "1: DOWN")):
                ports = Path(tmp, device, "ports", "1")
                (ports / "hw_counters").mkdir(parents=True)
                (ports / "state").write_text(state + "\n")
                (ports / "hw_counters" / "roce_adp_retrans").write_text("7\n")
                (ports / "hw_counters" / "lifespan").write_text("10\n")
            exp = probe.Exposition()
            probe.collect_roce(exp, tmp)
            text = exp.render()
        self.assertIn('spark_roce_hw_counter_total{counter="roce_adp_retrans",device="rocep1s0f1",port="1"} 7', text)
        self.assertIn('spark_roce_port_active{device="rocep1s0f0",port="1"} 0', text)
        self.assertNotIn('device="rocep1s0f0",port="1"} 7', text)
        self.assertNotIn("lifespan", text)

    def test_no_rdma_hardware_is_not_an_error(self):
        probe.collect_roce(probe.Exposition(), "/nonexistent")


class FakePostgres(threading.Thread):
    """Enough of the server side of SCRAM-SHA-256 and the simple query protocol."""

    daemon = True

    def __init__(self, password: str, rows: dict[str, list]) -> None:
        super().__init__()
        self.password, self.rows = password, rows
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.queries: list[str] = []
        self.startup: dict[str, str] = {}

    @staticmethod
    def _recv(conn, n):
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _msg(self, conn):
        kind = self._recv(conn, 1)
        length = struct.unpack("!I", self._recv(conn, 4))[0]
        return kind, self._recv(conn, length - 4)

    @staticmethod
    def _send(conn, kind, payload=b""):
        conn.sendall(kind + struct.pack("!I", len(payload) + 4) + payload)

    def run(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                try:
                    self.serve(conn)
                except (ConnectionError, OSError):
                    pass

    def serve(self, conn):
        length = struct.unpack("!I", self._recv(conn, 4))[0]
        body = self._recv(conn, length - 4)
        code = struct.unpack("!I", body[:4])[0]
        if code == 80877103:  # SSLRequest
            conn.sendall(b"N")
            return
        parts = body[4:].split(b"\0")
        self.startup = {parts[i].decode(): parts[i + 1].decode() for i in range(0, len(parts) - 2, 2)}
        self._send(conn, b"R", struct.pack("!I", 10) + b"SCRAM-SHA-256\0\0")
        _, first = self._msg(conn)
        mechanism, _, rest = first.partition(b"\0")
        client_first = rest[4:].decode()
        bare = client_first.split(",", 2)[2]
        client_nonce = dict(i.split("=", 1) for i in bare.split(","))["r"]
        salt, iterations, nonce = b"saltsaltsalt", 4096, client_nonce + "server"
        server_first = f"r={nonce},s={base64.b64encode(salt).decode()},i={iterations}"
        self._send(conn, b"R", struct.pack("!I", 11) + server_first.encode())
        _, final = self._msg(conn)
        final = final.decode()
        without_proof, _, proof = final.rpartition(",p=")
        salted = hashlib.pbkdf2_hmac("sha256", self.password.encode(), salt, iterations)
        auth_message = f"{bare},{server_first},{without_proof}".encode()
        client_key = hmac.digest(salted, b"Client Key", "sha256")
        signature = hmac.digest(hashlib.sha256(client_key).digest(), auth_message, "sha256")
        expected = bytes(a ^ b for a, b in zip(client_key, signature))
        if base64.b64decode(proof) != expected:
            self._send(conn, b"E", b"SFATAL\0C28P01\0Mpassword authentication failed for user secretname\0\0")
            return
        server_sig = hmac.digest(hmac.digest(salted, b"Server Key", "sha256"), auth_message, "sha256")
        self._send(conn, b"R", struct.pack("!I", 12) + b"v=" + base64.b64encode(server_sig))
        self._send(conn, b"R", struct.pack("!I", 0))
        self._send(conn, b"Z", b"I")
        while True:
            kind, payload = self._msg(conn)
            if kind == b"X":
                return
            sql = payload[:-1].decode()
            self.queries.append(sql)
            rows = next((r for key, r in self.rows.items() if key in sql), None)
            if rows is None:
                self._send(conn, b"E", b"SERROR\0C42601\0Mno such thing\0\0")
            else:
                for row in rows:
                    body = struct.pack("!H", len(row))
                    for cell in row:
                        body += struct.pack("!i", -1) if cell is None else struct.pack("!i", len(str(cell).encode())) + str(cell).encode()
                    self._send(conn, b"D", body)
                self._send(conn, b"C", b"SELECT\0")
            self._send(conn, b"Z", b"I")


PG_ROWS = {
    "server_version": [["17.11 (Debian)", "200", "3600.5", "f"]],
    "pg_stat_database": [["10", "1", "5", "95", "100", "50", "4", "3", "2", "0", "0", "1"]],
    "GROUP BY 1": [["active", "2"], ["idle", "7"], [None, "1"]],
    "max(now()-xact_start)": [["12.5", "3"]],
    "pg_wal_lsn_diff(pg_current_wal_lsn(),'0/0')": [["1048576"]],
    "pg_stat_replication": [["0", "0"]],
    "pg_database_size": [["appdb", "4096"]],
}


class FakeRedis(threading.Thread):
    daemon = True

    def __init__(self, password: str | None) -> None:
        super().__init__()
        self.password = password
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]

    def run(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                self.serve(conn)

    def serve(self, conn):
        reader = conn.makefile("rb")
        authed = self.password is None
        while True:
            line = reader.readline()
            if not line:
                return
            if line.startswith(b"*"):
                args = []
                for _ in range(int(line[1:])):
                    size = int(reader.readline()[1:])
                    args.append(reader.read(size + 2)[:-2].decode())
            else:
                args = line.decode().split()
            command = args[0].upper()
            if command == "PING":
                conn.sendall(b"+PONG\r\n" if authed else b"-NOAUTH Authentication required.\r\n")
            elif command == "AUTH":
                authed = args[1] == self.password
                conn.sendall(b"+OK\r\n" if authed else b"-WRONGPASS invalid\r\n")
            elif command == "INFO":
                if not authed:
                    conn.sendall(b"-NOAUTH Authentication required.\r\n")
                else:
                    body = (b"# Server\r\nredis_version:8.8.3\r\nuptime_in_seconds:60\r\n# Memory\r\nused_memory:2048\r\nmaxmemory:0\r\n"
                            b"connected_clients:4\r\ntotal_commands_processed:99\r\nkeyspace_hits:90\r\nkeyspace_misses:10\r\n"
                            b"evicted_keys:0\r\nexpired_keys:5\r\nrejected_connections:0\r\nrdb_last_save_time:1000\r\n"
                            b"# Keyspace\r\ndb0:keys=7,expires=0,avg_ttl=0\r\ndb1:keys=3,expires=0,avg_ttl=0\r\n")
                    conn.sendall(b"$%d\r\n%s\r\n" % (len(body), body))


def db_container(image, port, *, project="sf-local-ai", env=None, allowed="sf-local-ai", cmd=None):
    cfg = config(image=image, project=project, env=env, cmd=cmd, networks={"app": {"IPAddress": "127.0.0.1"}}, mounts={})
    c = probe.parse_container("d" * 64, cfg, HOSTCONFIG, allowed)
    c.pid = 0  # nothing to read in /proc; dial the fake's port directly
    c.engine_port = port
    return c


class DatabaseCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def run_collect(self, *containers):
        collector = probe.DatabaseCollector(self.tmp.name, self.tmp.name, volumes_file=None)
        exp = probe.Exposition()
        collector.collect(exp, list(containers), [])
        return exp.render()

    def test_a_login_reads_statistics_and_never_exports_the_password(self):
        pg = FakePostgres(SECRET, PG_ROWS)
        pg.start()
        self.addCleanup(pg.server.close)
        text = self.run_collect(db_container("postgres:17", pg.port))
        self.assertIn('stats="login"', text)
        self.assertIn('version="17.11"', text)
        self.assertRegex(text, r'spark_db_transactions_total\{[^}]*outcome="commit"[^}]*\} 10')
        self.assertRegex(text, r'spark_db_cache_reads_total\{[^}]*source="hit"[^}]*\} 95')
        self.assertRegex(text, r"spark_db_connections_max\{[^}]*\} 200")
        self.assertRegex(text, r"spark_db_longest_transaction_seconds\{[^}]*\} 12.5")
        self.assertRegex(text, r'spark_db_size_bytes\{[^}]*datname="appdb"[^}]*\} 4096')
        self.assertNotIn(SECRET, text)

    def test_the_session_is_read_only_and_on_the_maintenance_database(self):
        # On an application database it would block a DROP DATABASE.
        pg = FakePostgres(SECRET, PG_ROWS)
        pg.start()
        self.addCleanup(pg.server.close)
        self.run_collect(db_container("postgres:17", pg.port))
        self.assertEqual(pg.startup["database"], "postgres")
        self.assertIn("default_transaction_read_only=on", pg.startup["options"])
        self.assertEqual(pg.startup["application_name"], "spark-probe")
        for sql in pg.queries:
            self.assertTrue(sql.lstrip().upper().startswith("SELECT"), sql)

    def test_a_database_outside_the_allowed_projects_is_never_logged_in_to(self):
        pg = FakePostgres(SECRET, PG_ROWS)
        pg.start()
        self.addCleanup(pg.server.close)
        text = self.run_collect(db_container("postgres:17", pg.port, project="stagevote"))
        self.assertRegex(text, r"spark_db_up\{[^}]*\} 1")
        self.assertIn('stats="liveness"', text)
        self.assertEqual(pg.queries, [])
        self.assertEqual(pg.startup, {})

    def test_a_wrong_password_reads_as_a_stats_error_not_as_down(self):
        pg = FakePostgres("a different password", PG_ROWS)
        pg.start()
        self.addCleanup(pg.server.close)
        text = self.run_collect(db_container("postgres:17", pg.port))
        self.assertRegex(text, r"spark_db_up\{[^}]*\} 1")
        self.assertRegex(text, r"spark_db_stats_error\{[^}]*\} 1")
        self.assertNotIn("secretname", text)

    def test_nothing_listening_reads_as_down(self):
        with socket.create_server(("127.0.0.1", 0)) as held:
            port = held.getsockname()[1]
        text = self.run_collect(db_container("postgres:17", port))
        self.assertRegex(text, r"spark_db_up\{[^}]*\} 0")
        self.assertNotIn("spark_db_probe_seconds{", text)

    def test_redis_with_no_password_gives_its_statistics_freely(self):
        redis = FakeRedis(None)
        redis.start()
        self.addCleanup(redis.server.close)
        text = self.run_collect(db_container("redis:8", redis.port, project="other", env=[]))
        self.assertIn('stats="info"', text)
        self.assertIn('version="8.8.3"', text)
        self.assertRegex(text, r"spark_db_keys\{[^}]*\} 10")
        self.assertRegex(text, r'spark_db_cache_reads_total\{[^}]*source="miss"[^}]*\} 10')
        self.assertNotIn("spark_db_memory_limit_bytes{", text)

    def test_redis_with_a_password_outside_the_allowed_projects_is_liveness_only(self):
        redis = FakeRedis(SECRET)
        redis.start()
        self.addCleanup(redis.server.close)
        text = self.run_collect(db_container("redis:8", redis.port, project="other", env=[f"REDIS_PASSWORD={SECRET}"]))
        self.assertRegex(text, r"spark_db_up\{[^}]*\} 1")
        self.assertIn('stats="liveness"', text)
        self.assertRegex(text, r"spark_db_stats_error\{[^}]*\} 0")
        self.assertNotIn(SECRET, text)

    def test_redis_with_an_allowed_password_logs_in(self):
        redis = FakeRedis(SECRET)
        redis.start()
        self.addCleanup(redis.server.close)
        text = self.run_collect(db_container("redis:8", redis.port, env=[f"REDIS_PASSWORD={SECRET}"]))
        self.assertRegex(text, r"spark_db_memory_used_bytes\{[^}]*\} 2048")
        self.assertNotIn(SECRET, text)

    def test_a_leftover_is_counted_not_alarmed_on(self):
        c = probe.parse_container("e" * 64, config(running=False, manual=True), HOSTCONFIG)
        text = self.run_collect(c)
        self.assertIn('spark_db_stopped_instances{engine="postgres"} 1', text)
        self.assertNotIn("spark_db_up{", text)

    def test_a_stopped_database_that_should_run_is_down(self):
        c = probe.parse_container("e" * 64, config(running=False), HOSTCONFIG)
        text = self.run_collect(c)
        self.assertRegex(text, r"spark_db_up\{[^}]*\} 0")

    def test_a_custom_port_is_believed_only_when_the_default_is_not_listening(self):
        c = db_container("postgres:17", 5432)
        with tempfile.TemporaryDirectory() as proc:
            c.pid = 77
            net = Path(proc, "77", "net")
            net.mkdir(parents=True)
            header = "  sl  local_address rem_address   st\n"
            (net / "tcp").write_text(header + "   0: 00000000:1538 00000000:0000 0A\n")  # 5432
            self.assertEqual(probe.dial_target(c, proc)[1], 5432)
            (net / "tcp").write_text(header + "   0: 00000000:1F90 00000000:0000 0A\n")  # 8080
            self.assertEqual(probe.dial_target(c, proc)[1], 8080)


class ScriptedServer(threading.Thread):
    """Accepts one connection at a time and runs `script(conn)`."""

    daemon = True

    def __init__(self, script) -> None:
        super().__init__()
        self.script = script
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.seen: list[bytes] = []

    def run(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            with conn:
                try:
                    self.script(self, conn)
                except OSError:
                    pass


def pg_auth(code: int, extra: bytes = b"") -> bytes:
    body = struct.pack("!I", code) + extra
    return b"R" + struct.pack("!I", len(body) + 4) + body


def startup(conn) -> None:
    length = struct.unpack("!I", conn.recv(4))[0]
    while length - 4 > 0:
        length -= len(conn.recv(length - 4))


class HostileDatabaseTests(unittest.TestCase):
    """It is someone else's program. It must not stall the cycle, fill memory or get a password."""

    def serve(self, script):
        server = ScriptedServer(script)
        server.start()
        self.addCleanup(server.server.close)
        return server

    def login(self, server, timeout=1.0):
        return probe.MiniPg("127.0.0.1", server.port, "app", SECRET, "postgres", timeout=timeout)

    def test_the_password_is_never_sent_in_clear(self):
        def ask_for_cleartext(srv, conn):
            startup(conn)
            conn.sendall(pg_auth(3))
            conn.settimeout(0.5)
            try:
                srv.seen.append(conn.recv(4096))
            except OSError:
                pass
        server = self.serve(ask_for_cleartext)
        with self.assertRaises(probe.DbError):
            self.login(server)
        self.assertFalse([chunk for chunk in server.seen if SECRET.encode() in chunk])

    def test_md5_is_refused_too(self):
        def ask_for_md5(srv, conn):
            startup(conn)
            conn.sendall(pg_auth(5, b"salt"))
            conn.settimeout(0.5)
            try:
                srv.seen.append(conn.recv(4096))
            except OSError:
                pass
        server = self.serve(ask_for_md5)
        with self.assertRaises(probe.DbError):
            self.login(server)
        self.assertFalse([chunk for chunk in server.seen if b"md5" in chunk])

    def test_a_server_that_skips_its_own_proof_is_refused(self):
        def fake_ok(_srv, conn):
            startup(conn)
            conn.sendall(pg_auth(10, b"SCRAM-SHA-256\0\0"))
            conn.recv(4096)
            conn.sendall(pg_auth(0))
            conn.sendall(b"Z\x00\x00\x00\x05I")
        server = self.serve(fake_ok)
        with self.assertRaisesRegex(probe.DbError, "proof"):
            self.login(server)

    def test_a_huge_iteration_count_is_refused_rather_than_computed(self):
        def expensive(_srv, conn):
            startup(conn)
            conn.sendall(pg_auth(10, b"SCRAM-SHA-256\0\0"))
            first = conn.recv(4096)
            nonce = first.split(b"r=")[1].decode()
            conn.sendall(pg_auth(11, f"r={nonce}x,s=c2FsdA==,i=2147483647".encode()))
            conn.settimeout(0.5)
            try:
                conn.recv(4096)
            except OSError:
                pass
        server = self.serve(expensive)
        started = time.monotonic()
        with self.assertRaisesRegex(probe.DbError, "iteration"):
            self.login(server)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_a_server_dripping_one_byte_at_a_time_cannot_hold_a_read(self):
        def drip(_srv, conn):
            startup(conn)
            for _ in range(60):
                conn.sendall(b"N")
                time.sleep(0.05)
        server = self.serve(drip)
        started = time.monotonic()
        with self.assertRaises((probe.DbError, OSError)):
            self.login(server, timeout=0.3)
        self.assertLess(time.monotonic() - started, 1.5)

    def test_a_reply_past_the_budget_is_refused(self):
        def flood(_srv, conn):
            startup(conn)
            conn.sendall(b"N" + struct.pack("!I", (1 << 31) - 1))
            conn.settimeout(0.2)
            try:
                while True:
                    conn.sendall(b"x" * 65536)
            except OSError:
                pass
        server = self.serve(flood)
        with self.assertRaisesRegex(probe.DbError, "too large"):
            self.login(server)

    def test_an_endless_redis_line_is_refused(self):
        def flood(_srv, conn):
            conn.recv(4096)
            conn.settimeout(0.2)
            try:
                while True:
                    conn.sendall(b"+" + b"x" * 65536)
            except OSError:
                pass
        server = self.serve(flood)
        with self.assertRaises(probe.DbError):
            probe.redis_info("127.0.0.1", server.port, None, 1.0)

    def test_one_broken_database_does_not_take_the_others_with_it(self):
        # struct.error and friends used to escape the pool and blank every database.
        good = FakeRedis(None)
        good.start()
        self.addCleanup(good.server.close)

        def garbage(_srv, conn):
            startup(conn)
            conn.sendall(b"R\x00\x00\x00\x04")
        bad = self.serve(garbage)
        collector = probe.DatabaseCollector("/nonexistent", "/nonexistent", volumes_file=None)
        original = collector._probe
        calls = []

        def explode(c):
            calls.append(c.name)
            if c.name == "boom":
                raise struct.error("bad bytes")
            return original(c)
        collector._probe = explode
        one = db_container("redis:8", good.port, project="other", env=[])
        one.name = "fine"
        two = db_container("postgres:17", bad.port)
        two.name = "boom"
        exp = probe.Exposition()
        collector.collect(exp, [one, two], [])
        text = exp.render()
        self.assertRegex(text, r'spark_db_up\{container="fine"[^}]*\} 1')
        self.assertRegex(text, r'spark_db_up\{container="boom"[^}]*\} 0')

    def test_a_hostile_version_string_becomes_unknown(self):
        self.assertEqual(probe.safe_version("8.8.3"), "8.8.3")
        self.assertEqual(probe.safe_version("x" * 5000), "unknown")
        self.assertEqual(probe.safe_version('1"} evil 1\n'), "unknown")
        self.assertEqual(probe.safe_version(None), "unknown")

    def test_a_label_cannot_grow_past_the_limit_prometheus_enforces(self):
        exp = probe.Exposition()
        exp.gauge("m", "help", 1, container="x" * 5000)
        line = [l for l in exp.render().splitlines() if l.startswith("m{")][0]
        self.assertLess(len(line), 400)

    def test_volume_names_go_to_spark_local_with_nothing_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v.json")
            collector = probe.DatabaseCollector(tmp, tmp, volumes_file=path)
            c = probe.parse_container("a" * 64, config(project="sf-local-ai"), HOSTCONFIG, "*")
            collector._publish_volumes([c])
            text = Path(path).read_text()
        self.assertEqual(json.loads(text), [{"container": "db-1", "engine": "postgres", "project": "sf-local-ai", "volumes": ["stagevote_pgdata"]}])
        self.assertNotIn(SECRET, text)


class ProbeCycleTests(unittest.TestCase):
    def test_one_blind_collector_does_not_blank_the_others(self):
        p = probe.Probe()
        p.docker = probe.DockerState("/nonexistent-docker-root")
        text = p.cycle()
        self.assertIn('spark_probe_collector_success{collector="containers"} 0', text)
        self.assertIn('spark_probe_collector_success{collector="processes"} 1', text)
        self.assertIn("spark_probe_last_run_timestamp_seconds", text)
        self.assertNotIn("spark_containers{", text)  # not "zero containers"

    def test_the_file_appears_whole_or_not_at_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.prom")
            probe.write_atomically(path, "a 1\n")
            self.assertEqual(Path(path).read_text(), "a 1\n")
            self.assertEqual(os.listdir(tmp), ["x.prom"])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o644)


class SourceRulesTests(unittest.TestCase):
    """The promises in the module docstrings, held to in the source."""

    def setUp(self):
        self.probe_src = (PROBE_DIR / "spark_probe.py").read_text()
        self.local_src = (PROBE_DIR / "spark_local.py").read_text()

    def test_standard_library_only(self):
        for src in (self.probe_src, self.local_src):
            for third_party in ("psycopg", "redis", "requests", "prometheus_client", "psutil", "docker"):
                self.assertNotRegex(src, rf"^\s*(import|from) {third_party}\b")

    def test_neither_process_listens_on_anything(self):
        for src in (self.probe_src, self.local_src):
            self.assertNotIn(".listen(", src)
            self.assertNotIn("http.server", src)
            self.assertNotIn(".bind(", src)

    def test_the_probe_has_no_way_to_read_volumes(self):
        # With DAC_READ_SEARCH it could read any file on the host (open_by_handle_at).
        self.assertNotIn("os.scandir", self.probe_src)
        self.assertNotIn("/host/docker/volumes", self.probe_src)

    def test_the_probe_never_touches_the_docker_socket(self):
        self.assertNotIn("AF_UNIX", self.probe_src)
        self.assertNotIn("/var/run/docker", self.probe_src)

    def test_spark_local_only_ever_sends_get(self):
        self.assertIn('connection.request("GET", path)', self.local_src)
        for verb in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertNotIn(f'"{verb}"', self.local_src)

    def test_passwords_are_never_logged(self):
        for line in self.probe_src.splitlines():
            if "log." in line:
                self.assertNotIn("password", line.lower())
                self.assertNotIn("login", line.replace("logins", ""))


class SmartLogTests(unittest.TestCase):
    @staticmethod
    def page(**fields) -> bytes:
        raw = bytearray(512)
        raw[0] = fields.get("critical", 0)
        raw[1:3] = int(fields.get("kelvin", 328)).to_bytes(2, "little")
        raw[3] = fields.get("spare", 100)
        raw[4] = fields.get("threshold", 10)
        raw[5] = fields.get("used", 3)
        for offset, key in ((32, "read"), (48, "written"), (112, "cycles"), (128, "hours"), (144, "unsafe"), (160, "media")):
            raw[offset:offset + 16] = int(fields.get(key, 0)).to_bytes(16, "little")
        return bytes(raw)

    def test_the_fields_land_where_the_specification_puts_them(self):
        values = local.parse_smart_log(self.page(written=2, read=1, unsafe=28, hours=252, cycles=51, media=0, used=3))
        self.assertEqual(values["temperature_celsius"], 55)
        self.assertEqual(values["data_written_bytes_total"], 1_024_000)
        self.assertEqual(values["data_read_bytes_total"], 512_000)
        self.assertEqual(values["unsafe_shutdowns_total"], 28)
        self.assertEqual(values["power_on_seconds_total"], 252 * 3600)
        self.assertEqual(values["power_cycles_total"], 51)
        self.assertAlmostEqual(values["percentage_used_ratio"], 0.03)
        self.assertEqual(values["available_spare_ratio"], 1.0)

    def test_every_reading_has_help_text_and_renders(self):
        values = local.parse_smart_log(self.page())
        self.assertEqual(set(values), set(local.HELP))
        text = local.render({"nvme0": values}, [])
        self.assertIn('spark_nvme_read_success{device="nvme0"} 1', text)
        for key in values:
            self.assertIn(f"# TYPE spark_nvme_{key} ", text)

    def test_a_drive_that_cannot_be_read_says_so(self):
        text = local.render({}, ["nvme0"])
        self.assertIn('spark_nvme_read_success{device="nvme0"} 0', text)
        self.assertNotIn('spark_nvme_unsafe_shutdowns_total{device="nvme0"}', text)


class DatabaseSizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name, "volumes")
        data = self.root / "pgdata" / "_data"
        data.mkdir(parents=True)
        (data / "f").write_bytes(b"x" * 8192)
        self.list_path = Path(self.tmp.name, "list.json")

    def write(self, items):
        self.list_path.write_text(json.dumps(items))

    def test_sizes_are_measured_from_the_probes_list(self):
        self.write([{"container": "db-1", "engine": "postgres", "project": "app", "volumes": ["pgdata"]}])
        sizes = local.db_size_pass(str(self.root), str(self.list_path))
        text = local.render_db_sizes(list(sizes.values()))
        self.assertRegex(text, r'spark_db_data_bytes\{container="db-1",engine="postgres",project="app"\} \d+')
        self.assertGreater(sizes["db-1"][1], 0)

    def test_the_walk_cannot_be_pointed_outside_the_volumes(self):
        # spark-probe talks to other programs; spark-local trusts nothing from it.
        secret_dir = Path(self.tmp.name, "secret", "_data")
        secret_dir.mkdir(parents=True)
        for bad in ("../secret", "/etc", "a/b", "", ".."):
            self.write([{"container": "x", "engine": "postgres", "project": "app", "volumes": [bad]}])
            self.assertEqual(local.db_size_pass(str(self.root), str(self.list_path)), {}, bad)

    def test_labels_from_the_list_must_be_plain_names(self):
        self.write([{"container": 'x"} evil 1', "engine": "postgres", "project": "app", "volumes": ["pgdata"]}])
        self.assertEqual(local.db_size_pass(str(self.root), str(self.list_path)), {})

    def test_a_broken_or_missing_list_is_nothing_to_measure(self):
        self.assertEqual(local.db_size_pass(str(self.root), str(self.list_path)), {})
        self.list_path.write_text("{broken")
        self.assertEqual(local.db_size_pass(str(self.root), str(self.list_path)), {})
        self.list_path.write_text('{"not": "a list"}')
        self.assertEqual(local.db_size_pass(str(self.root), str(self.list_path)), {})

    def test_sizing_gives_up_rather_than_report_a_partial_total(self):
        self.assertGreater(local.directory_size(str(self.root), time.monotonic() + 5), 0)
        self.assertIsNone(local.directory_size(str(self.root), time.monotonic() - 1))

    def test_symlinks_are_not_followed_out_of_a_volume(self):
        outside = Path(self.tmp.name, "outside")
        outside.mkdir()
        (outside / "big").write_bytes(b"x" * (1 << 20))
        (self.root / "pgdata" / "_data" / "link").symlink_to(outside)
        size = local.directory_size(str(self.root / "pgdata"), time.monotonic() + 5)
        self.assertLess(size, 1 << 20)

    def test_a_walk_that_times_out_keeps_the_last_good_value(self):
        self.write([{"container": "db-1", "engine": "postgres", "project": "app", "volumes": ["pgdata"]}])
        good = local.db_size_pass(str(self.root), str(self.list_path))
        original = local.DB_SIZE_BUDGET_S
        local.DB_SIZE_BUDGET_S = -1
        try:
            self.assertEqual(local.db_size_pass(str(self.root), str(self.list_path), last=good), good)
        finally:
            local.DB_SIZE_BUDGET_S = original


class HealthTests(unittest.TestCase):
    def test_docker_status_text_becomes_a_verdict(self):
        containers = [
            {"Names": ["/a"], "Status": "Up 3 days (healthy)", "Labels": {"com.docker.compose.project": "p", "com.docker.compose.service": "s"}},
            {"Names": ["/b"], "Status": "Up 1 hour (unhealthy)", "Labels": {}},
            {"Names": ["/c"], "Status": "Up 2 seconds (health: starting)", "Labels": {}},
            {"Names": ["/d"], "Status": "Up 3 days", "Labels": {}},
            {"Names": ["/e"], "Status": "Exited (0) 2 days ago", "Labels": {}},
        ]
        text = local.render_health(containers, {"ServerVersion": "29.6.2", "Driver": "overlay2", "Images": 304})
        self.assertIn('spark_container_health_status{container="a",project="p",service="s"} 1', text)
        self.assertIn('spark_container_health_status{container="b",project="standalone",service="b"} 0', text)
        self.assertIn('spark_container_health_status{container="c",project="standalone",service="c"} 2', text)
        self.assertNotIn('container="d"', text)  # no healthcheck is not "healthy"
        self.assertNotIn('container="e"', text)
        self.assertIn("spark_docker_up 1", text)
        self.assertIn('spark_docker_info{version="29.6.2",storage_driver="overlay2"} 1', text)

    def test_docker_being_away_is_not_an_empty_machine(self):
        text = local.render_health(None, None)
        self.assertIn("spark_docker_up 0", text)
        self.assertNotIn("spark_container_health_status{", text)


def _load_path(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class GpuExporterTests(unittest.TestCase):
    PMON = """# gpu         pid   type     sm    mem    enc    dec    jpg    ofa    command
# Idx           #    C/G      %      %      %      %      %      %    name
    0      10573     C     94      0      -      -      -      -    VLLM::Worker_TP
    0      12125     C      -      -      -      -      -      -    VLLM::EngineCor
    0     106443     G      3      1      -      -      -      -    gnome-shell
    0     106444     G      2      0      -      -      -      -    gnome-shell
"""

    @classmethod
    def setUpClass(cls):
        cls.gpu = _load_path(REPO_ROOT / "monitoring" / "exporters" / "dgx-gpu" / "dgx_gpu_exporter.py", "dgx_gpu_exporter_under_test")

    def test_busy_share_is_added_up_by_name_and_no_sample_is_not_zero(self):
        self.assertEqual(self.gpu.parse_pmon(self.PMON), {"VLLM::Worker_TP": 94.0, "gnome-shell": 5.0})

    def test_a_format_it_does_not_recognise_gives_nothing_rather_than_nonsense(self):
        self.assertEqual(self.gpu.parse_pmon("nothing useful here\n"), {})
        self.assertEqual(self.gpu.parse_pmon("# gpu pid type command\n 0 1 C name\n"), {})

    def test_a_failing_extra_reading_cannot_take_the_gpu_metrics_with_it(self):
        row = dict(zip(self.gpu.CSV_FIELDS, ["0", "GPU-1", "GB10", "90", "0", "60", "30", "40", "41", "2500", "2500", "3000", "P0"]
                       + ["Not Active"] * 5 + ["Not Active"] + ["0"] * 5))
        def broken():
            raise OSError("pmon is unavailable")
        collector = self.gpu.Collector()
        original = (self.gpu.query_devices, self.gpu.query_processes, self.gpu.query_pmon, self.gpu.query_info)
        try:
            self.gpu.query_devices = lambda: [row]
            self.gpu.query_processes = lambda: []
            self.gpu.query_pmon = broken
            self.gpu.query_info = broken
            text = collector._build()
        finally:
            self.gpu.query_devices, self.gpu.query_processes, self.gpu.query_pmon, self.gpu.query_info = original
        self.assertIn("dgx_gpu_up 1", text)
        self.assertIn("dgx_gpu_power_watts", text)
        self.assertNotIn("dgx_gpu_process_utilization_percent{", text)
        self.assertNotIn("dgx_gpu_info{", text)


class UndeclaredStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stores = _load_path(REPO_ROOT / "monitoring" / "exporters" / "data-stores" / "data_stores_exporter.py", "data_stores_under_test")

    def test_anything_new_at_the_top_of_the_volume_is_found_by_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "warehouse.duckdb").write_bytes(b"x")
            (root / "warehouse.duckdb.wal").write_bytes(b"x")
            (root / "old copy.duckdb").write_bytes(b"x")
            (root / "app.sqlite3").write_bytes(b"x")
            (root / ".lancedb.write.lock").write_bytes(b"")
            (root / "lancedb" / "chunks.lance").mkdir(parents=True)
            (root / "backup-index" / "chunks.lance").mkdir(parents=True)
            (root / "chat-media").mkdir()
            found = self.stores.undeclared_stores(root, {root / "warehouse.duckdb", root / "lancedb"})
        self.assertEqual(sorted((label, kind) for label, _path, kind in found), [
            ("undeclared/app.sqlite3", "sqlite"),
            ("undeclared/backup-index", "lancedb"),
            ("undeclared/chat-media", "other"),
            ("undeclared/old_copy.duckdb", "duckdb"),
        ])

    def test_a_missing_volume_finds_nothing_rather_than_failing(self):
        self.assertEqual(self.stores.undeclared_stores(Path("/nonexistent"), set()), [])


if __name__ == "__main__":
    unittest.main()
