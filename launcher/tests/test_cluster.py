"""Two-node DGX Spark cluster settings: validation, detection, engine args.

Everything runs against fake command runners and a temporary sysfs tree; no
test here inspects a real interface, RDMA device, or Docker network.
"""

from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path

try:
    from .support import CommandStub, fake_discovery
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import CommandStub, fake_discovery

from techsara_cli.cluster import (
    DEFAULT_DETECTORS,
    DEFAULT_DISCOVERY,
    ClusterDetectors,
    ClusterLink,
    WorkerPreflight,
    build_engine_arguments,
    default_worker_ssh,
    detect_cluster_links,
    detect_docker_bridge_gateway,
    detect_hcas_for_ifnames,
    detect_ifname_for_ip,
    discover_cluster_peer,
    discover_peer_ip,
    mirror_address,
    preflight_worker,
    resolve_cluster,
    resolve_cluster_mode,
    resolve_cluster_settings,
)
from techsara_cli.errors import TechSaraError

HEAD_IP = "192.168.100.1"
WORKER_IP = "192.168.100.2"
STARTUP_ARGUMENTS = ("--quantization", "modelopt", "--attention-backend", "flashinfer")

IP_ADDR_JSON = json.dumps(
    [
        {"ifindex": 1, "ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
        {"ifindex": 2, "ifname": "enp1s0", "addr_info": [{"family": "inet", "local": "10.0.0.5"}]},
        {
            "ifindex": 3,
            "ifname": "enP2p1s0f1np1",
            "addr_info": [{"family": "inet", "local": HEAD_IP}],
        },
        {"ifindex": 4, "ifname": "enP2p1s0f0np0", "addr_info": [{"family": "inet", "local": "192.168.101.1"}]},
        {"ifindex": 5, "ifname": "docker0", "addr_info": []},
    ]
)


def fake_detectors(
    *,
    ifnames: dict[str, str] | None = None,
    hcas: dict[str, str] | None = None,
    gateway: str | None = "172.17.0.1",
) -> ClusterDetectors:
    interface_map = {HEAD_IP: "enP2p1s0f1np1", "192.168.101.1": "enP2p1s0f0np0"} if ifnames is None else ifnames
    hca_map = {"enP2p1s0f1np1": "rocep1s0f1", "enP2p1s0f0np0": "rocep1s0f0"} if hcas is None else hcas
    return ClusterDetectors(
        ifname_for_ip=lambda ip: interface_map.get(ip),
        hcas_for_ifnames=lambda names: [hca_map[name] for name in names if name in hca_map],
        docker_bridge_gateway=lambda: gateway,
    )


def resolve(values: dict[str, str], **overrides):
    options = {
        "profile_id": "dgx-spark",
        "publish_model_ports": False,
        "context": 262144,
        "startup_arguments": STARTUP_ARGUMENTS,
        "vllm_port": 8000,
        "detectors": fake_detectors(),
    }
    options.update(overrides)
    return resolve_cluster_settings(values, **options)


def dual(**extra: str) -> dict[str, str]:
    values = {"CLUSTER_MODE": "dual", "CLUSTER_HEAD_IP": HEAD_IP, "CLUSTER_WORKER_IP": WORKER_IP}
    values.update(extra)
    return values


class ClusterModeTests(unittest.TestCase):
    def test_mode_defaults_to_auto_and_accepts_every_mode_case_insensitively(self) -> None:
        self.assertEqual(resolve_cluster_mode({}), "auto")
        self.assertEqual(resolve_cluster_mode({"CLUSTER_MODE": ""}), "auto")
        self.assertEqual(resolve_cluster_mode({"CLUSTER_MODE": "  "}), "auto")
        self.assertEqual(resolve_cluster_mode({"CLUSTER_MODE": "single"}), "single")
        self.assertEqual(resolve_cluster_mode({"CLUSTER_MODE": " Dual "}), "dual")
        self.assertEqual(resolve_cluster_mode({"CLUSTER_MODE": "AUTO"}), "auto")

    def test_unknown_mode_is_rejected(self) -> None:
        for value in ("triple", "true", "1", "cluster"):
            with self.subTest(value=value), self.assertRaisesRegex(TechSaraError, "CLUSTER_MODE"):
                resolve_cluster_mode({"CLUSTER_MODE": value})


class ValidationTests(unittest.TestCase):
    def test_defaults_are_applied_for_every_optional_key(self) -> None:
        values = resolve(dual())
        self.assertEqual(values["CLUSTER_HEAD_IP"], HEAD_IP)
        self.assertEqual(values["CLUSTER_WORKER_IP"], WORKER_IP)
        self.assertEqual(values["CLUSTER_HEAD_IP_2"], "")
        self.assertEqual(values["CLUSTER_WORKER_IP_2"], "")
        self.assertEqual(values["CLUSTER_MASTER_PORT"], "29501")
        self.assertEqual(values["CLUSTER_TENSOR_PARALLEL_SIZE"], "2")
        self.assertEqual(values["CLUSTER_PIPELINE_PARALLEL_SIZE"], "1")
        self.assertEqual(values["CLUSTER_GPU_MEMORY_UTILIZATION"], "0.30")
        self.assertEqual(values["CLUSTER_NCCL_DEBUG"], "INFO")
        self.assertEqual(values["CLUSTER_NCCL_SOCKET_IFNAME"], "enP2p1s0f1np1")
        self.assertEqual(values["CLUSTER_NCCL_IB_HCA"], "rocep1s0f1")
        self.assertEqual(values["CLUSTER_API_BIND_ADDRESS"], "172.17.0.1")
        self.assertIn("--speculative-config '{\"method\":\"mtp\",\"num_speculative_tokens\":1}'", values["CLUSTER_ENGINE_ARGS"])
        self.assertIn("--max-num-batched-tokens 8192", values["CLUSTER_ENGINE_ARGS"])
        self.assertEqual(
            set(values),
            {
                "CLUSTER_HEAD_IP", "CLUSTER_WORKER_IP", "CLUSTER_HEAD_IP_2", "CLUSTER_WORKER_IP_2",
                "CLUSTER_MASTER_PORT", "CLUSTER_TENSOR_PARALLEL_SIZE", "CLUSTER_PIPELINE_PARALLEL_SIZE",
                "CLUSTER_GPU_MEMORY_UTILIZATION", "CLUSTER_KV_CACHE_MEMORY_GIB", "CLUSTER_NCCL_DEBUG", "CLUSTER_NCCL_SOCKET_IFNAME",
                "CLUSTER_NCCL_IB_HCA", "CLUSTER_API_BIND_ADDRESS", "CLUSTER_ENGINE_ARGS",
            },
        )

    def test_dual_mode_is_only_supported_on_the_dgx_spark_profile(self) -> None:
        for profile_id in ("nvidia-large", "local-minimal", "mac-128gb-plus", "external-development"):
            with self.subTest(profile=profile_id), self.assertRaisesRegex(
                TechSaraError, "CLUSTER_MODE=dual is only supported on the dgx-spark profile"
            ):
                resolve(dual(), profile_id=profile_id)

    def test_head_and_worker_addresses_are_required_literal_ipv4(self) -> None:
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_HEAD_IP is required"):
            resolve({"CLUSTER_MODE": "dual", "CLUSTER_WORKER_IP": WORKER_IP})
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_WORKER_IP is required"):
            resolve({"CLUSTER_MODE": "dual", "CLUSTER_HEAD_IP": HEAD_IP})
        for bad in ("spark-1", "192.168.100", "fe80::1", "192.168.100.1/24", "0.0.0.0 ; id"):
            with self.subTest(value=bad):
                with self.assertRaisesRegex(TechSaraError, "CLUSTER_HEAD_IP must be a literal IPv4"):
                    resolve(dual(CLUSTER_HEAD_IP=bad))
                with self.assertRaisesRegex(TechSaraError, "CLUSTER_WORKER_IP must be a literal IPv4"):
                    resolve(dual(CLUSTER_WORKER_IP=bad))
        with self.assertRaisesRegex(TechSaraError, "must be different hosts"):
            resolve(dual(CLUSTER_WORKER_IP=HEAD_IP))

    def test_second_rail_must_be_set_on_both_sides_and_be_ipv4(self) -> None:
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_HEAD_IP_2 and CLUSTER_WORKER_IP_2 must be set together"):
            resolve(dual(CLUSTER_HEAD_IP_2="192.168.101.1"))
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_HEAD_IP_2 and CLUSTER_WORKER_IP_2 must be set together"):
            resolve(dual(CLUSTER_WORKER_IP_2="192.168.101.2"))
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_WORKER_IP_2 must be a literal IPv4"):
            resolve(dual(CLUSTER_HEAD_IP_2="192.168.101.1", CLUSTER_WORKER_IP_2="spark-2"))
        values = resolve(dual(CLUSTER_HEAD_IP_2="192.168.101.1", CLUSTER_WORKER_IP_2="192.168.101.2"))
        self.assertEqual(values["CLUSTER_HEAD_IP_2"], "192.168.101.1")
        self.assertEqual(values["CLUSTER_WORKER_IP_2"], "192.168.101.2")
        # Both rails' HCAs, in rail order, feed NCCL_IB_HCA.
        self.assertEqual(values["CLUSTER_NCCL_IB_HCA"], "rocep1s0f1,rocep1s0f0")
        # The socket interface stays the primary rail.
        self.assertEqual(values["CLUSTER_NCCL_SOCKET_IFNAME"], "enP2p1s0f1np1")

    def test_master_port_is_bounded(self) -> None:
        self.assertEqual(resolve(dual(CLUSTER_MASTER_PORT="40000"))["CLUSTER_MASTER_PORT"], "40000")
        for bad in ("80", "1023", "65536", "abc", "-1"):
            with self.subTest(value=bad), self.assertRaisesRegex(TechSaraError, "CLUSTER_MASTER_PORT"):
                resolve(dual(CLUSTER_MASTER_PORT=bad))
        with self.assertRaisesRegex(TechSaraError, "must not equal VLLM_PORT"):
            resolve(dual(CLUSTER_MASTER_PORT="8000"))

    def test_parallelism_product_must_be_exactly_two(self) -> None:
        pipeline = resolve(dual(CLUSTER_TENSOR_PARALLEL_SIZE="1", CLUSTER_PIPELINE_PARALLEL_SIZE="2"))
        self.assertEqual(pipeline["CLUSTER_TENSOR_PARALLEL_SIZE"], "1")
        self.assertEqual(pipeline["CLUSTER_PIPELINE_PARALLEL_SIZE"], "2")
        self.assertIn("--tensor-parallel-size 1 --pipeline-parallel-size 2", pipeline["CLUSTER_ENGINE_ARGS"])
        for tensor, pipe in (("1", "1"), ("2", "2"), ("4", "1"), ("0", "2"), ("x", "1")):
            with self.subTest(tp=tensor, pp=pipe), self.assertRaisesRegex(
                TechSaraError, "CLUSTER_TENSOR_PARALLEL_SIZE|CLUSTER_PIPELINE_PARALLEL_SIZE"
            ):
                resolve(dual(CLUSTER_TENSOR_PARALLEL_SIZE=tensor, CLUSTER_PIPELINE_PARALLEL_SIZE=pipe))

    def test_gpu_memory_utilization_is_bounded_and_normalised(self) -> None:
        self.assertEqual(resolve(dual(CLUSTER_GPU_MEMORY_UTILIZATION="0.5"))["CLUSTER_GPU_MEMORY_UTILIZATION"], "0.50")
        self.assertEqual(resolve(dual(CLUSTER_GPU_MEMORY_UTILIZATION=".95"))["CLUSTER_GPU_MEMORY_UTILIZATION"], "0.95")
        self.assertEqual(resolve(dual(CLUSTER_GPU_MEMORY_UTILIZATION="0.05"))["CLUSTER_GPU_MEMORY_UTILIZATION"], "0.05")
        for bad in ("0.04", "0.96", "1", "0", "-0.3", "nan", "half"):
            with self.subTest(value=bad), self.assertRaisesRegex(TechSaraError, "CLUSTER_GPU_MEMORY_UTILIZATION"):
                resolve(dual(CLUSTER_GPU_MEMORY_UTILIZATION=bad))

    def test_nccl_debug_levels_are_passed_through_or_rejected(self) -> None:
        for level in ("VERSION", "WARN", "INFO", "TRACE", "warn"):
            with self.subTest(level=level):
                self.assertEqual(resolve(dual(CLUSTER_NCCL_DEBUG=level))["CLUSTER_NCCL_DEBUG"], level.upper())
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_NCCL_DEBUG"):
            resolve(dual(CLUSTER_NCCL_DEBUG="DEBUG"))

    def test_speculative_config_defaults_disables_on_empty_and_requires_a_json_object(self) -> None:
        default = resolve(dual())["CLUSTER_ENGINE_ARGS"]
        self.assertIn("--speculative-config '{\"method\":\"mtp\",\"num_speculative_tokens\":1}'", default)
        disabled = resolve(dual(CLUSTER_SPECULATIVE_CONFIG=""))["CLUSTER_ENGINE_ARGS"]
        self.assertNotIn("--speculative-config", disabled)
        self.assertIn("--enable-prefix-caching --max-num-batched-tokens 8192", disabled)
        custom = resolve(dual(CLUSTER_SPECULATIVE_CONFIG='{"method": "mtp", "num_speculative_tokens": 2}'))
        self.assertIn(
            "--speculative-config '{\"method\":\"mtp\",\"num_speculative_tokens\":2}'",
            custom["CLUSTER_ENGINE_ARGS"],
        )
        for bad in ("mtp", "{", "[1, 2]", "{}", "42"):
            with self.subTest(value=bad), self.assertRaisesRegex(TechSaraError, "CLUSTER_SPECULATIVE_CONFIG"):
                resolve(dual(CLUSTER_SPECULATIVE_CONFIG=bad))

    def test_max_num_batched_tokens_has_a_floor(self) -> None:
        self.assertIn("--max-num-batched-tokens 4096", resolve(dual(CLUSTER_MAX_NUM_BATCHED_TOKENS="4096"))["CLUSTER_ENGINE_ARGS"])
        self.assertIn("--max-num-batched-tokens 256", resolve(dual(CLUSTER_MAX_NUM_BATCHED_TOKENS="256"))["CLUSTER_ENGINE_ARGS"])
        for bad in ("255", "0", "many"):
            with self.subTest(value=bad), self.assertRaisesRegex(TechSaraError, "CLUSTER_MAX_NUM_BATCHED_TOKENS"):
                resolve(dual(CLUSTER_MAX_NUM_BATCHED_TOKENS=bad))

    def test_interface_and_hca_overrides_replace_detection_and_reject_shell_metacharacters(self) -> None:
        calls: list[str] = []
        detectors = ClusterDetectors(
            ifname_for_ip=lambda ip: calls.append(ip) or "should-not-be-used",
            hcas_for_ifnames=lambda names: calls.append(",".join(names)) or ["unused"],
            docker_bridge_gateway=lambda: "172.17.0.1",
        )
        values = resolve(
            dual(CLUSTER_NCCL_SOCKET_IFNAME="enp9s0", CLUSTER_NCCL_IB_HCA="mlx5_0,mlx5_1"), detectors=detectors
        )
        self.assertEqual(values["CLUSTER_NCCL_SOCKET_IFNAME"], "enp9s0")
        self.assertEqual(values["CLUSTER_NCCL_IB_HCA"], "mlx5_0,mlx5_1")
        self.assertEqual(calls, [])
        for key in ("CLUSTER_NCCL_SOCKET_IFNAME", "CLUSTER_NCCL_IB_HCA"):
            for bad in ("eth0 eth1", "$(id)", "a'b", 'a"b'):
                with self.subTest(key=key, value=bad), self.assertRaisesRegex(TechSaraError, key):
                    resolve(dual(**{key: bad}))

    def test_missing_interface_for_the_head_address_is_an_actionable_error(self) -> None:
        with self.assertRaisesRegex(TechSaraError, "no host interface carries CLUSTER_HEAD_IP"):
            resolve(dual(), detectors=fake_detectors(ifnames={}))

    def test_missing_rdma_devices_degrade_to_an_empty_hca_list_without_failing(self) -> None:
        values = resolve(dual(), detectors=fake_detectors(hcas={}))
        self.assertEqual(values["CLUSTER_NCCL_IB_HCA"], "")
        self.assertEqual(values["CLUSTER_NCCL_SOCKET_IFNAME"], "enP2p1s0f1np1")

    def test_api_bind_address_follows_the_publish_opt_in(self) -> None:
        self.assertEqual(resolve(dual(), publish_model_ports=True)["CLUSTER_API_BIND_ADDRESS"], "0.0.0.0")
        self.assertEqual(resolve(dual(), publish_model_ports=False)["CLUSTER_API_BIND_ADDRESS"], "172.17.0.1")
        unknown = fake_detectors(gateway=None)
        self.assertEqual(resolve(dual(), publish_model_ports=False, detectors=unknown)["CLUSTER_API_BIND_ADDRESS"], "0.0.0.0")
        # The bridge probe is not needed when publishing on every interface.
        probed: list[str] = []
        detectors = ClusterDetectors(
            ifname_for_ip=fake_detectors().ifname_for_ip,
            hcas_for_ifnames=fake_detectors().hcas_for_ifnames,
            docker_bridge_gateway=lambda: probed.append("bridge") or "172.17.0.1",
        )
        resolve(dual(), publish_model_ports=True, detectors=detectors)
        self.assertEqual(probed, [])


class EngineArgumentTests(unittest.TestCase):
    def test_exact_engine_argument_string_for_a_known_input(self) -> None:
        values = resolve(dual(), context=131072, vllm_port=8000)
        self.assertEqual(
            values["CLUSTER_ENGINE_ARGS"],
            "--max-model-len 131072 --gpu-memory-utilization 0.30 --kv-cache-memory-bytes 17179869184 "
            "--kv-cache-dtype fp8 --trust-remote-code "
            "--quantization modelopt --attention-backend flashinfer --enable-chunked-prefill "
            "--enable-prefix-caching --speculative-config '{\"method\":\"mtp\",\"num_speculative_tokens\":1}' "
            "--max-num-batched-tokens 8192 --tensor-parallel-size 2 --pipeline-parallel-size 1 "
            "--distributed-executor-backend mp --nnodes 2 --master-addr 192.168.100.1 --master-port 29501 "
            "--distributed-timeout-seconds 300",
        )

    def test_engine_arguments_are_shell_splittable_with_json_as_one_element(self) -> None:
        values = resolve(dual())
        argv = shlex.split(values["CLUSTER_ENGINE_ARGS"])
        self.assertIn('{"method":"mtp","num_speculative_tokens":1}', argv)
        self.assertEqual(argv[argv.index("--speculative-config") + 1], '{"method":"mtp","num_speculative_tokens":1}')
        self.assertEqual(argv[argv.index("--nnodes") + 1], "2")
        self.assertEqual(argv[argv.index("--master-addr") + 1], HEAD_IP)
        self.assertNotIn("\n", values["CLUSTER_ENGINE_ARGS"])
        self.assertNotIn("  ", values["CLUSTER_ENGINE_ARGS"])

    def test_empty_startup_arguments_and_disabled_speculation_leave_no_double_spaces(self) -> None:
        rendered = build_engine_arguments(
            context=4096,
            gpu_memory_utilization="0.30",
            startup_arguments=(),
            speculative_config="",
            max_num_batched_tokens=512,
            tensor_parallel_size=2,
            pipeline_parallel_size=1,
            head_ip=HEAD_IP,
            master_port=29501,
        )
        self.assertNotIn("  ", rendered)
        self.assertIn("--trust-remote-code --enable-chunked-prefill --enable-prefix-caching --max-num-batched-tokens 512", rendered)
        self.assertNotIn("--speculative-config", rendered)

    def test_startup_arguments_with_spaces_are_quoted(self) -> None:
        rendered = build_engine_arguments(
            context=4096,
            gpu_memory_utilization="0.30",
            startup_arguments=("--override-generation-config", '{"temperature": 0.6}'),
            speculative_config="",
            max_num_batched_tokens=512,
            tensor_parallel_size=2,
            pipeline_parallel_size=1,
            head_ip=HEAD_IP,
            master_port=29501,
        )
        argv = shlex.split(rendered)
        self.assertIn('{"temperature": 0.6}', argv)


class DetectionTests(unittest.TestCase):
    def test_ifname_for_ip_reads_the_json_address_table(self) -> None:
        runner = CommandStub(lambda args, _timeout: (0, IP_ADDR_JSON, "") if args == ("ip", "-j", "-4", "addr", "show") else (127, "", "unexpected"))
        self.assertEqual(detect_ifname_for_ip(HEAD_IP, runner=runner), "enP2p1s0f1np1")
        self.assertEqual(detect_ifname_for_ip("192.168.101.1", runner=runner), "enP2p1s0f0np0")
        self.assertIsNone(detect_ifname_for_ip("192.168.100.9", runner=runner))
        self.assertTrue(runner.called("ip", "-j", "-4", "addr", "show"))

    def test_ifname_for_ip_returns_none_on_any_failure(self) -> None:
        self.assertIsNone(detect_ifname_for_ip(HEAD_IP, runner=CommandStub(lambda a, t: (1, "", "no ip tool"))))
        self.assertIsNone(detect_ifname_for_ip(HEAD_IP, runner=CommandStub(lambda a, t: (0, "not json", ""))))
        self.assertIsNone(detect_ifname_for_ip(HEAD_IP, runner=CommandStub(lambda a, t: (0, '{"ifname": "x"}', ""))))

        def explode(_args, _timeout):
            raise OSError("ip is not installed")

        self.assertIsNone(detect_ifname_for_ip(HEAD_IP, runner=CommandStub(explode)))

    def test_hcas_for_ifnames_walks_sysfs_in_interface_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sysfs = Path(temporary) / "infiniband"
            for hca, ifname in (("rocep1s0f0", "enP2p1s0f0np0"), ("rocep1s0f1", "enP2p1s0f1np1"), ("mlx5_2", "ib0")):
                (sysfs / hca / "device" / "net" / ifname).mkdir(parents=True)
            self.assertEqual(detect_hcas_for_ifnames(["enP2p1s0f1np1", "enP2p1s0f0np0"], sysfs=sysfs), ["rocep1s0f1", "rocep1s0f0"])
            self.assertEqual(detect_hcas_for_ifnames(["enP2p1s0f0np0"], sysfs=sysfs), ["rocep1s0f0"])
            self.assertEqual(detect_hcas_for_ifnames(["enp1s0", ""], sysfs=sysfs), [])
            self.assertEqual(detect_hcas_for_ifnames(["ib0", "ib0"], sysfs=sysfs), ["mlx5_2"])
            self.assertEqual(detect_hcas_for_ifnames(["enP2p1s0f1np1"], sysfs=Path(temporary) / "missing"), [])

    def test_docker_bridge_gateway_uses_network_inspect_and_validates_the_answer(self) -> None:
        expected = ("docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}")
        runner = CommandStub(lambda args, _timeout: (0, "172.17.0.1\n", "") if args == expected else (127, "", ""))
        self.assertEqual(detect_docker_bridge_gateway(runner=runner), "172.17.0.1")
        self.assertTrue(runner.called(*expected))
        self.assertIsNone(detect_docker_bridge_gateway(runner=CommandStub(lambda a, t: (1, "", "no such network"))))
        self.assertIsNone(detect_docker_bridge_gateway(runner=CommandStub(lambda a, t: (0, "<no value>", ""))))
        self.assertIsNone(detect_docker_bridge_gateway(runner=CommandStub(lambda a, t: (0, "", ""))))

    def test_default_detectors_are_bound_to_the_real_probes(self) -> None:
        self.assertIs(DEFAULT_DETECTORS.ifname_for_ip, detect_ifname_for_ip)
        self.assertIs(DEFAULT_DETECTORS.hcas_for_ifnames, detect_hcas_for_ifnames)
        self.assertIs(DEFAULT_DETECTORS.docker_bridge_gateway, detect_docker_bridge_gateway)
        self.assertIs(DEFAULT_DISCOVERY.links, detect_cluster_links)
        self.assertIs(DEFAULT_DISCOVERY.peer_ip, discover_peer_ip)
        self.assertIs(DEFAULT_DISCOVERY.preflight, preflight_worker)
        self.assertIs(DEFAULT_DISCOVERY.worker_ssh, default_worker_ssh)


# Real DGX Spark naming: the lowercase "enp1..." NIC sorts before "enP2...".
LINK_A = ClusterLink("enp1s0f1np1", "10.100.184.1", 24)
LINK_B = ClusterLink("enP2p1s0f1np1", "10.100.185.1", 24)
PREFLIGHT_ARGV_PREFIX = (
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", "-o", "StrictHostKeyChecking=accept-new",
)
PREFLIGHT_OUTPUT = (
    "spark-476e\nGPU 0: NVIDIA GB10 (UUID: GPU-1864538f)\nDocker version 29.2.1, build a5c7197\nrdma-ok\n"
)


def addr_show_json(*rows: tuple[str, str | None, int]) -> str:
    """``ip -j -4 addr show`` rows; ``None`` for an interface without an address."""
    payload = []
    for index, (ifname, ip, prefixlen) in enumerate(rows, start=1):
        info = [] if ip is None else [{"family": "inet", "local": ip, "prefixlen": prefixlen}]
        payload.append({"ifindex": index, "ifname": ifname, "operstate": "UP", "addr_info": info})
    return json.dumps(payload)


def neigh_json(*rows: tuple[str, str]) -> str:
    return json.dumps([{"dst": dst, "lladdr": "4c:bb:47:00:00:01", "state": [state]} for dst, state in rows])


class LinkDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.sysfs = Path(self.temporary.name)
        # Four RDMA ports as on a real Spark; only two netdevs carry addresses.
        for hca, ifname in (
            ("rocep1s0f0", "enp1s0f0np0"), ("rocep1s0f1", "enp1s0f1np1"),
            ("roceP2p1s0f0", "enP2p1s0f0np0"), ("roceP2p1s0f1", "enP2p1s0f1np1"),
        ):
            (self.sysfs / "infiniband" / hca / "device" / "net" / ifname).mkdir(parents=True)
        for ifname, state in (
            ("lo", "unknown"), ("enP7s7", "up"), ("docker0", "up"),
            ("enp1s0f1np1", "up"), ("enP2p1s0f1np1", "up"), ("enp1s0f0np0", "down"), ("enP2p1s0f0np0", "down"),
        ):
            (self.sysfs / "net" / ifname).mkdir(parents=True)
            (self.sysfs / "net" / ifname / "operstate").write_text(f"{state}\n", encoding="utf-8")

    def runner(self, addr_json: str) -> CommandStub:
        return CommandStub(lambda args, _t: (0, addr_json, "") if args == ("ip", "-j", "-4", "addr", "show") else (127, "", ""))

    def test_links_need_operstate_up_an_ipv4_address_and_an_rdma_device(self) -> None:
        addr = addr_show_json(
            ("lo", "127.0.0.1", 8),
            ("enP7s7", "192.168.9.54", 22),          # management NIC: no RDMA device
            ("docker0", "172.17.0.1", 16),
            ("enP2p1s0f1np1", "10.100.185.1", 24),   # listed first, sorts second
            ("enp1s0f1np1", "10.100.184.1", 24),
            ("enp1s0f0np0", "10.100.186.1", 24),     # RDMA but operstate down
            ("enP2p1s0f0np0", None, 0),              # up? no: down, and no address
        )
        links = detect_cluster_links(runner=self.runner(addr), sysfs=self.sysfs)
        self.assertEqual(links, [LINK_A, LINK_B])
        self.assertEqual(links[0].ifname, "enp1s0f1np1")
        self.assertEqual((links[0].ip, links[0].prefixlen), ("10.100.184.1", 24))

    def test_links_are_empty_when_nothing_qualifies_or_the_tool_fails(self) -> None:
        only_management = addr_show_json(("enP7s7", "192.168.9.54", 22), ("enp1s0f1np1", None, 0))
        self.assertEqual(detect_cluster_links(runner=self.runner(only_management), sysfs=self.sysfs), [])
        self.assertEqual(detect_cluster_links(runner=CommandStub(lambda a, t: (1, "", "no ip")), sysfs=self.sysfs), [])
        self.assertEqual(detect_cluster_links(runner=CommandStub(lambda a, t: (0, "nope", "")), sysfs=self.sysfs), [])
        # A sysfs tree without the interface (or without RDMA at all) yields nothing.
        addr = addr_show_json(("enp1s0f1np1", "10.100.184.1", 24))
        self.assertEqual(detect_cluster_links(runner=self.runner(addr), sysfs=self.sysfs / "missing"), [])

    def test_operstate_is_read_from_sysfs_not_from_the_ip_output(self) -> None:
        (self.sysfs / "net" / "enp1s0f1np1" / "operstate").write_text("down\n", encoding="utf-8")
        addr = addr_show_json(("enp1s0f1np1", "10.100.184.1", 24), ("enP2p1s0f1np1", "10.100.185.1", 24))
        self.assertEqual(detect_cluster_links(runner=self.runner(addr), sysfs=self.sysfs), [LINK_B])


class PeerDiscoveryTests(unittest.TestCase):
    @staticmethod
    def runner(neigh: str, *, ifname: str = "enp1s0f1np1") -> CommandStub:
        expected = ("ip", "-j", "-4", "neigh", "show", "dev", ifname)
        return CommandStub(lambda args, _t: (0, neigh, "") if args == expected else (127, "", "unexpected"))

    @staticmethod
    def probe(*answering: str):
        tried: list[tuple[str, int]] = []

        def check(ip: str, port: int) -> bool:
            tried.append((ip, port))
            return ip in answering

        check.tried = tried  # type: ignore[attr-defined]
        return check

    def test_mirror_address_swaps_the_last_octet_between_one_and_two(self) -> None:
        self.assertEqual(mirror_address("10.100.184.1"), "10.100.184.2")
        self.assertEqual(mirror_address("10.100.184.2"), "10.100.184.1")
        self.assertIsNone(mirror_address("10.100.184.7"))
        self.assertIsNone(mirror_address("not-an-ip"))

    def test_neighbours_are_tried_before_the_mirror_and_failed_entries_are_skipped(self) -> None:
        neigh = neigh_json(("10.100.184.9", "FAILED"), ("10.100.184.5", "STALE"), ("10.100.184.2", "REACHABLE"), ("10.100.184.7", "INCOMPLETE"))
        probe = self.probe("10.100.184.2")
        self.assertEqual(discover_peer_ip(LINK_A, runner=self.runner(neigh), probe=probe), "10.100.184.2")
        # 10.100.184.2 is both a neighbour and the mirror: probed once, in table order.
        self.assertEqual(probe.tried, [("10.100.184.5", 22), ("10.100.184.2", 22)])

    def test_mirror_convention_is_the_fallback_when_the_neighbour_table_is_empty(self) -> None:
        probe = self.probe("10.100.184.2")
        self.assertEqual(discover_peer_ip(LINK_A, runner=self.runner("[]"), probe=probe), "10.100.184.2")
        self.assertEqual(probe.tried, [("10.100.184.2", 22)])
        probe_b = self.probe("10.100.185.2")
        self.assertEqual(discover_peer_ip(LINK_B, runner=self.runner("[]", ifname="enP2p1s0f1np1"), probe=probe_b), "10.100.185.2")

    def test_no_answer_anywhere_yields_none_after_every_candidate_was_probed(self) -> None:
        neigh = neigh_json(("10.100.184.5", "STALE"))
        probe = self.probe()
        self.assertIsNone(discover_peer_ip(LINK_A, runner=self.runner(neigh), probe=probe))
        self.assertEqual(probe.tried, [("10.100.184.5", 22), ("10.100.184.2", 22)])
        # Failing `ip neigh` still leaves the mirror convention.
        probe = self.probe()
        self.assertIsNone(discover_peer_ip(LINK_A, runner=CommandStub(lambda a, t: (1, "", "")), probe=probe))
        self.assertEqual(probe.tried, [("10.100.184.2", 22)])
        # A link outside the .1/.2 convention with no neighbours has no candidates.
        odd = ClusterLink("enp1s0f1np1", "10.100.184.7", 24)
        probe = self.probe()
        self.assertIsNone(discover_peer_ip(odd, runner=self.runner("[]"), probe=probe))
        self.assertEqual(probe.tried, [])

    def test_discover_cluster_peer_pairs_link_b_only_when_it_answers_too(self) -> None:
        both = fake_discovery(links=[LINK_A, LINK_B], peers={"enp1s0f1np1": "10.100.184.2", "enP2p1s0f1np1": "10.100.185.2"})
        peer = discover_cluster_peer(discovery=both)
        self.assertTrue(peer.found)
        self.assertEqual(
            peer.address_values(),
            {"CLUSTER_HEAD_IP": "10.100.184.1", "CLUSTER_WORKER_IP": "10.100.184.2",
             "CLUSTER_HEAD_IP_2": "10.100.185.1", "CLUSTER_WORKER_IP_2": "10.100.185.2"},
        )
        only_a = fake_discovery(links=[LINK_A, LINK_B], peers={"enp1s0f1np1": "10.100.184.2"})
        peer = discover_cluster_peer(discovery=only_a)
        self.assertTrue(peer.found)
        self.assertEqual(peer.address_values()["CLUSTER_HEAD_IP_2"], "")
        self.assertEqual(peer.address_values()["CLUSTER_WORKER_IP_2"], "")
        nothing = discover_cluster_peer(discovery=fake_discovery())
        self.assertFalse(nothing.found)
        self.assertEqual(nothing.failure, "no RoCE link carries an address")
        silent = discover_cluster_peer(discovery=fake_discovery(links=[LINK_A]))
        self.assertFalse(silent.found)
        self.assertEqual(silent.failure, "no second DGX Spark answered on 10.100.184.x")


class WorkerPreflightTests(unittest.TestCase):
    @staticmethod
    def runner(code: int, stdout: str, stderr: str = "") -> CommandStub:
        return CommandStub(lambda args, _t: (code, stdout, stderr))

    def test_success_requires_ssh_gb10_docker_and_rdma_in_one_call(self) -> None:
        runner = self.runner(0, PREFLIGHT_OUTPUT)
        report = preflight_worker("techsphere@10.100.184.2", runner=runner)
        self.assertEqual(report, WorkerPreflight(True, "spark-476e", "GPU 0: NVIDIA GB10 (UUID: GPU-1864538f)", "Docker version 29.2.1, build a5c7197", ""))
        self.assertEqual(len(runner.calls), 1)
        argv, timeout = runner.calls[0]
        self.assertEqual(argv[:7], PREFLIGHT_ARGV_PREFIX)
        self.assertEqual(argv[7:9], ("techsphere@10.100.184.2", "--"))
        self.assertIn("hostname; nvidia-smi -L | head -n1; docker --version; test -e /dev/infiniband/rdma_cm && echo rdma-ok", argv[9])
        self.assertGreaterEqual(timeout, 10.0)

    def test_each_failure_names_its_reason(self) -> None:
        denied = preflight_worker("techsphere@10.100.184.2", runner=self.runner(255, "", "techsphere@10.100.184.2: Permission denied (publickey).\n"))
        self.assertFalse(denied.ok)
        self.assertEqual(denied.detail, "ssh: Permission denied (publickey).")
        unreachable = preflight_worker("u@10.100.184.2", runner=self.runner(255, "", "ssh: connect to host 10.100.184.2 port 22: Connection timed out"))
        self.assertIn("Connection timed out", unreachable.detail)
        self.assertTrue(unreachable.detail.startswith("ssh: "))
        no_gpu = preflight_worker("u@h", runner=self.runner(0, "other-box\nDocker version 29.2.1\nrdma-ok\n", "bash: nvidia-smi: command not found"))
        self.assertFalse(no_gpu.ok)
        self.assertEqual(no_gpu.hostname, "other-box")
        self.assertIn("no NVIDIA GB10 GPU visible on other-box", no_gpu.detail)
        self.assertIn("nvidia-smi: command not found", no_gpu.detail)
        wrong_gpu = preflight_worker("u@h", runner=self.runner(0, "box\nGPU 0: NVIDIA A100 (UUID: x)\nDocker version 1\nrdma-ok\n"))
        self.assertIn("no NVIDIA GB10 GPU visible on box (GPU 0: NVIDIA A100 (UUID: x))", wrong_gpu.detail)
        no_docker = preflight_worker("u@h", runner=self.runner(0, "box\nGPU 0: NVIDIA GB10\nrdma-ok\n", "bash: docker: command not found"))
        self.assertFalse(no_docker.ok)
        self.assertIn("docker is not installed on box", no_docker.detail)
        no_rdma = preflight_worker("u@h", runner=self.runner(1, "box\nGPU 0: NVIDIA GB10\nDocker version 29.2.1\n"))
        self.assertFalse(no_rdma.ok)
        self.assertIn("/dev/infiniband/rdma_cm is missing on box", no_rdma.detail)
        odd_exit = preflight_worker("u@h", runner=self.runner(3, PREFLIGHT_OUTPUT, "weird"))
        self.assertFalse(odd_exit.ok)
        self.assertIn("exited with status 3", odd_exit.detail)

        def explode(_args, _timeout):
            raise OSError("ssh is not installed")

        missing = preflight_worker("u@h", runner=CommandStub(explode))
        self.assertFalse(missing.ok)
        self.assertEqual(missing.detail, "ssh: ssh is not installed")

    def test_default_ssh_target_is_the_current_user_at_the_worker(self) -> None:
        import getpass

        self.assertEqual(default_worker_ssh("10.100.184.2"), f"{getpass.getuser()}@10.100.184.2")


class ResolutionTests(unittest.TestCase):
    """``resolve_cluster``: the effective mode for every profile and CLUSTER_MODE."""

    def resolve(self, values: dict[str, str], *, discovery=None, calls=None, **overrides):
        options = {
            "profile_id": "dgx-spark",
            "publish_model_ports": False,
            "context": 262144,
            "startup_arguments": STARTUP_ARGUMENTS,
            "vllm_port": 8000,
            "detectors": fake_detectors(),
            "discovery": discovery or fake_discovery(calls=calls),
        }
        options.update(overrides)
        return resolve_cluster(values, **options)

    @staticmethod
    def two_sparks(calls=None, **extra):
        return fake_discovery(
            links=[LINK_A, LINK_B],
            peers={"enp1s0f1np1": "10.100.184.2", "enP2p1s0f1np1": "10.100.185.2"},
            preflight=WorkerPreflight(True, "spark-476e", "GPU 0: NVIDIA GB10", "Docker version 29.2.1"),
            calls=calls,
            **extra,
        )

    @staticmethod
    def detectors_for_real_links() -> ClusterDetectors:
        return fake_detectors(
            ifnames={"10.100.184.1": "enp1s0f1np1", "10.100.185.1": "enP2p1s0f1np1"},
            hcas={"enp1s0f1np1": "rocep1s0f1", "enP2p1s0f1np1": "roceP2p1s0f1"},
        )

    def test_auto_off_dgx_spark_is_single_and_silent(self) -> None:
        for profile_id in ("nvidia-large", "local-minimal", "mac-128gb-plus", "external-development"):
            calls: list[str] = []
            with self.subTest(profile=profile_id):
                for values in ({}, {"CLUSTER_MODE": "auto"}, {"CLUSTER_MODE": "single"}):
                    result = self.resolve(values, profile_id=profile_id, calls=calls)
                    self.assertEqual((result.mode, result.reason, result.values), ("single", "", {}))
                self.assertEqual(calls, [])
                self.assertEqual(result.generated(), {"TECHSARA_CLUSTER_MODE": "single", "TECHSARA_CLUSTER_REASON": ""})
                with self.assertRaisesRegex(TechSaraError, "CLUSTER_MODE=dual is only supported on the dgx-spark profile"):
                    self.resolve({"CLUSTER_MODE": "dual"}, profile_id=profile_id, calls=calls)
                self.assertEqual(calls, [])

    def test_forced_single_on_dgx_spark_skips_discovery_entirely(self) -> None:
        calls: list[str] = []
        result = self.resolve({"CLUSTER_MODE": "single", "CLUSTER_HEAD_IP": "garbage"}, discovery=self.two_sparks(calls))
        self.assertEqual(result.mode, "single")
        self.assertEqual(result.reason, "CLUSTER_MODE=single in .env")
        self.assertEqual(result.values, {})
        self.assertEqual(calls, [])

    def test_auto_with_no_links_is_single_with_the_reason(self) -> None:
        calls: list[str] = []
        result = self.resolve({}, calls=calls)
        self.assertEqual((result.mode, result.reason), ("single", "no RoCE link carries an address"))
        self.assertEqual(calls, ["links"])
        self.assertEqual(result.generated()["TECHSARA_CLUSTER_MODE"], "single")
        self.assertNotIn("CLUSTER_HEAD_IP", result.generated())

    def test_auto_with_links_but_no_peer_is_single_with_the_subnet_in_the_reason(self) -> None:
        calls: list[str] = []
        result = self.resolve({}, discovery=fake_discovery(links=[LINK_A, LINK_B], calls=calls))
        self.assertEqual((result.mode, result.reason), ("single", "no second DGX Spark answered on 10.100.184.x"))
        self.assertEqual(calls, ["links", "peer:enp1s0f1np1"])

    def test_auto_with_a_peer_but_failed_ssh_is_single_with_an_actionable_reason(self) -> None:
        calls: list[str] = []
        discovery = fake_discovery(
            links=[LINK_A], peers={"enp1s0f1np1": "10.100.184.2"},
            preflight=WorkerPreflight(False, "spark-476e", detail="ssh: Permission denied (publickey)"), calls=calls,
        )
        result = self.resolve({}, discovery=discovery, detectors=self.detectors_for_real_links())
        self.assertEqual(result.mode, "single")
        self.assertEqual(
            result.reason,
            "second DGX Spark spark-476e found at 10.100.184.2 but ssh failed: ssh: Permission denied (publickey) "
            "(set up key auth for tester@10.100.184.2 or CLUSTER_MODE=single to silence)",
        )
        self.assertEqual(result.values, {})
        self.assertEqual(calls, ["links", "peer:enp1s0f1np1", "preflight:tester@10.100.184.2"])
        # Without a hostname the reason still names the address.
        anonymous = fake_discovery(links=[LINK_A], peers={"enp1s0f1np1": "10.100.184.2"}, preflight=WorkerPreflight(False, detail="ssh: Connection refused"))
        result = self.resolve({}, discovery=anonymous, detectors=self.detectors_for_real_links())
        self.assertTrue(result.reason.startswith("second DGX Spark found at 10.100.184.2 but ssh failed: ssh: Connection refused"))

    def test_auto_success_is_dual_with_both_links_and_the_default_ssh_target(self) -> None:
        calls: list[str] = []
        result = self.resolve({}, discovery=self.two_sparks(calls), detectors=self.detectors_for_real_links())
        self.assertEqual(result.mode, "dual")
        self.assertEqual(result.reason, "second DGX Spark spark-476e at 10.100.184.2 (auto-detected)")
        values = result.values
        self.assertEqual(values["CLUSTER_HEAD_IP"], "10.100.184.1")
        self.assertEqual(values["CLUSTER_WORKER_IP"], "10.100.184.2")
        self.assertEqual(values["CLUSTER_HEAD_IP_2"], "10.100.185.1")
        self.assertEqual(values["CLUSTER_WORKER_IP_2"], "10.100.185.2")
        self.assertEqual(values["CLUSTER_WORKER_SSH"], "tester@10.100.184.2")
        self.assertEqual(values["CLUSTER_NCCL_SOCKET_IFNAME"], "enp1s0f1np1")
        self.assertEqual(values["CLUSTER_NCCL_IB_HCA"], "rocep1s0f1,roceP2p1s0f1")
        self.assertIn("--master-addr 10.100.184.1 --master-port 29501", values["CLUSTER_ENGINE_ARGS"])
        self.assertEqual(calls, ["links", "peer:enp1s0f1np1", "peer:enP2p1s0f1np1", "preflight:tester@10.100.184.2"])
        generated = result.generated()
        self.assertEqual(generated["TECHSARA_CLUSTER_MODE"], "dual")
        self.assertEqual(generated["TECHSARA_CLUSTER_REASON"], result.reason)
        self.assertNotIn("\n", generated["TECHSARA_CLUSTER_REASON"])
        # A second link that does not answer leaves the second rail unset.
        one_rail = fake_discovery(links=[LINK_A, LINK_B], peers={"enp1s0f1np1": "10.100.184.2"})
        result = self.resolve({}, discovery=one_rail, detectors=self.detectors_for_real_links())
        self.assertEqual(result.mode, "dual")
        self.assertEqual(result.values["CLUSTER_HEAD_IP_2"], "")
        self.assertEqual(result.values["CLUSTER_NCCL_IB_HCA"], "rocep1s0f1")

    def test_explicit_addresses_override_discovery_but_are_still_preflighted(self) -> None:
        calls: list[str] = []
        values = {"CLUSTER_HEAD_IP": HEAD_IP, "CLUSTER_WORKER_IP": WORKER_IP, "CLUSTER_WORKER_SSH": "ops@spark-2.local"}
        result = self.resolve(values, discovery=self.two_sparks(calls))
        self.assertEqual(result.mode, "dual")
        self.assertEqual(result.reason, f"second DGX Spark spark-476e at {WORKER_IP} (configured)")
        self.assertEqual(result.values["CLUSTER_HEAD_IP"], HEAD_IP)
        self.assertEqual(result.values["CLUSTER_WORKER_IP"], WORKER_IP)
        self.assertEqual(result.values["CLUSTER_WORKER_SSH"], "ops@spark-2.local")
        self.assertEqual(calls, ["preflight:ops@spark-2.local"], "no link or neighbour probe when addresses are explicit")
        # An explicit second rail survives discovery of the first one, even when
        # link B answers nobody (discovery must not blank the user's _2 pair).
        calls = []
        one_rail = fake_discovery(
            links=[LINK_A, LINK_B], peers={"enp1s0f1np1": "10.100.184.2"},
            preflight=WorkerPreflight(True, "spark-476e", "GPU 0: NVIDIA GB10", "Docker version 29.2.1"), calls=calls,
        )
        result = self.resolve(
            {"CLUSTER_HEAD_IP_2": "10.100.185.1", "CLUSTER_WORKER_IP_2": "10.100.185.2"},
            discovery=one_rail, detectors=self.detectors_for_real_links(),
        )
        self.assertEqual(result.mode, "dual")
        self.assertEqual((result.values["CLUSTER_HEAD_IP"], result.values["CLUSTER_WORKER_IP"]), ("10.100.184.1", "10.100.184.2"))
        self.assertEqual((result.values["CLUSTER_HEAD_IP_2"], result.values["CLUSTER_WORKER_IP_2"]), ("10.100.185.1", "10.100.185.2"))
        self.assertEqual(result.values["CLUSTER_NCCL_IB_HCA"], "rocep1s0f1,roceP2p1s0f1")
        self.assertEqual(calls, ["links", "peer:enp1s0f1np1", "peer:enP2p1s0f1np1", "preflight:tester@10.100.184.2"])
        # Half a pair is a configuration error in auto mode, not a silent fallback.
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_HEAD_IP and CLUSTER_WORKER_IP must be set together"):
            self.resolve({"CLUSTER_HEAD_IP": HEAD_IP}, discovery=self.two_sparks())
        # Explicit but invalid addresses are validated, as in dual mode.
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_WORKER_IP must be a literal IPv4"):
            self.resolve({"CLUSTER_HEAD_IP": HEAD_IP, "CLUSTER_WORKER_IP": "spark-2"}, discovery=self.two_sparks())
        for bad in ("-oProxyCommand=id", "user @host", "a;b"):
            with self.subTest(target=bad), self.assertRaisesRegex(TechSaraError, "CLUSTER_WORKER_SSH"):
                self.resolve({**values, "CLUSTER_WORKER_SSH": bad}, discovery=self.two_sparks())

    def test_forced_dual_errors_instead_of_degrading(self) -> None:
        # Bad preflight with explicit addresses.
        failing = fake_discovery(preflight=WorkerPreflight(False, "spark-476e", detail="ssh: Permission denied (publickey)"))
        with self.assertRaisesRegex(TechSaraError, r"CLUSTER_MODE=dual but the worker tester@192\.168\.100\.2 failed preflight: ssh: Permission denied"):
            self.resolve(dual(), discovery=failing)
        # Nothing discovered and nothing configured.
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_MODE=dual but no RoCE link carries an address"):
            self.resolve({"CLUSTER_MODE": "dual"})
        with self.assertRaisesRegex(TechSaraError, "no second DGX Spark answered on 10.100.184.x"):
            self.resolve({"CLUSTER_MODE": "dual"}, discovery=fake_discovery(links=[LINK_A]))
        # Forced dual still discovers when the addresses are left empty.
        result = self.resolve({"CLUSTER_MODE": "dual"}, discovery=self.two_sparks(), detectors=self.detectors_for_real_links())
        self.assertEqual(result.mode, "dual")
        self.assertEqual(result.reason, "second DGX Spark spark-476e at 10.100.184.2 (auto-detected)")
        # The existing validation errors are unchanged.
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_WORKER_IP is required when CLUSTER_MODE=dual"):
            self.resolve({"CLUSTER_MODE": "dual", "CLUSTER_HEAD_IP": HEAD_IP}, discovery=self.two_sparks())
        with self.assertRaisesRegex(TechSaraError, "must not equal VLLM_PORT"):
            self.resolve(dual(CLUSTER_MASTER_PORT="8000"), discovery=self.two_sparks())

    def test_validation_runs_before_the_ssh_round_trip(self) -> None:
        calls: list[str] = []
        with self.assertRaisesRegex(TechSaraError, "CLUSTER_MASTER_PORT"):
            self.resolve(dual(CLUSTER_MASTER_PORT="80"), discovery=self.two_sparks(calls))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()


class KvCacheBudgetTests(unittest.TestCase):
    def test_default_and_override_and_range(self) -> None:
        self.assertEqual(resolve(dual())["CLUSTER_KV_CACHE_MEMORY_GIB"], "16")
        self.assertIn("--kv-cache-memory-bytes 17179869184", resolve(dual())["CLUSTER_ENGINE_ARGS"])
        custom = dict(dual()); custom["CLUSTER_KV_CACHE_MEMORY_GIB"] = "24"
        self.assertIn("--kv-cache-memory-bytes 25769803776", resolve(custom)["CLUSTER_ENGINE_ARGS"])
        for bad in ("1", "97", "x"):
            broken = dict(dual()); broken["CLUSTER_KV_CACHE_MEMORY_GIB"] = bad
            with self.assertRaises(TechSaraError):
                resolve(broken)
