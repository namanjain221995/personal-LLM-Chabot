"""The verify job's engine exposure gate: can the unauthenticated engine API be
reached from outside the cluster? (audit F065; owner option A, 2026-09-13)

The first version of this gate failed on ANY wildcard listener, so under the
approved cluster shape (the head on every interface, the host packet filter
closing the LAN and the tailnet) it was red on every deploy and the only way
to turn it green was the bind that takes the two-node engine down. These tests
pin the replacement: a specific listener is judged exactly as before, and a
wildcard passes only when a probe FROM THE WORKER proves the fabric reaches
the port and nothing else does.

Every address below is a documentation address (RFC 5737, RFC 3849) or
loopback. PUBLIC LOGS: every test that renders output also asserts that no
address, interface name or ssh target appears in it.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import engine_bind  # noqa: E402

# -- the head, in documentation addresses ------------------------------------
LAN = "192.0.2.54"
FABRIC = "203.0.113.1"
FABRIC_B = "203.0.113.129"
TAILNET4 = "198.51.100.7"
TAILNET6 = "2001:db8::7"
BRIDGE = "198.51.100.129"
COMPOSE_BRIDGE = "198.51.100.193"
WORKER = "203.0.113.2"  # the worker's rail A address, on FABRIC's /25
WORKER_B = "203.0.113.130"  # the worker's rail B address, on FABRIC_B's /25
WORKER_SSH = f"operator@{WORKER}"
PORT = 8000

ADDRESS_TEXT = [LAN, FABRIC, FABRIC_B, TAILNET4, TAILNET6, BRIDGE, COMPOSE_BRIDGE, WORKER, WORKER_B,
                "192.0.2.", "203.0.113.", "198.51.100.", "2001:db8",
                "operator@", "fe80::", "eth0", "eth1", "enp1s0", "enp2s0", "tailscale0", "br-0123456789ab",
                "br-lan", "veth1234"]

ENV_CLUSTER = (
    f"TECHSARA_CLUSTER_MODE=dual\nCLUSTER_API_BIND_ADDRESS={BRIDGE}\nVLLM_PORT={PORT}\n"
    f"CLUSTER_HEAD_IP={FABRIC}\nCLUSTER_HEAD_IP_2={FABRIC_B}\nCLUSTER_WORKER_SSH={WORKER_SSH}\n"
    f"CLUSTER_WORKER_IP={WORKER}\nCLUSTER_WORKER_IP_2={WORKER_B}\n"
)
ENV_CLUSTER_WILDCARD_CONFIG = ENV_CLUSTER.replace(f"CLUSTER_API_BIND_ADDRESS={BRIDGE}", "CLUSTER_API_BIND_ADDRESS=0.0.0.0")

SS_WILDCARD = f"LISTEN 0      2048   0.0.0.0:{PORT} 0.0.0.0:*\n"
SS_IPV6_WILDCARD = f"LISTEN 0      4096   [::]:{PORT} [::]:*\n"
SS_STAR = f"LISTEN 0      4096   *:{PORT} *:*\n"
SS_BRIDGE = f"LISTEN 0      2048   {BRIDGE}:{PORT} 0.0.0.0:*\n"
SS_LOOPBACK = f"LISTEN 0 4096 127.0.0.1:{PORT} 0.0.0.0:* users:((\"docker-proxy\",pid=1,fd=4))\n"
SS_LAN = f"LISTEN 0 2048 {LAN}:{PORT} 0.0.0.0:*\n"
SS_OTHER_PORTS = "LISTEN 0 4096 0.0.0.0:9100 0.0.0.0:*\nLISTEN 0 4096 0.0.0.0:18000 0.0.0.0:*\n"


def _link(name, *addrs, master=None):
    """One `ip -j addr` link. Each address is (family, address, scope[, prefixlen])."""
    link = {"ifname": name, "addr_info": [
        {"family": a[0], "local": a[1], "prefixlen": a[3] if len(a) > 3 else 24, "scope": a[2]} for a in addrs]}
    if master:
        link["master"] = master
    return link


IP_HEAD = [
    _link("lo", ("inet", "127.0.0.1", "host"), ("inet6", "::1", "host")),
    _link("eth0", ("inet", LAN, "global"), ("inet6", "fe80::1", "link")),
    _link("enp1s0", ("inet", FABRIC, "global", 25), ("inet6", "fe80::2", "link")),
    _link("enp2s0", ("inet", FABRIC_B, "global", 25), ("inet6", "fe80::5", "link")),
    _link("tailscale0", ("inet", TAILNET4, "global", 32), ("inet6", TAILNET6, "global"), ("inet6", "fe80::3", "link")),
    _link("docker0", ("inet", BRIDGE, "global"), ("inet6", "fe80::6", "link")),
    _link("br-0123456789ab", ("inet", COMPOSE_BRIDGE, "global")),
    _link("veth1234", ("inet6", "fe80::4", "link"), master="br-0123456789ab"),
]
#: The head's default route leaves through the LAN NIC.
DEFAULT_ROUTES = frozenset({"eth0"})
#: Only the two fabric ports carry an RDMA device, as on the Sparks.
RDMA_LINKS = frozenset({"enp1s0", "enp2s0"})
OUTSIDE = {LAN, TAILNET4, TAILNET6}


class FakeProber:
    """The worker. Default: the host filter is in place (fabric and control
    ports connect, every outside engine connect times out)."""

    def __init__(self, overrides=None, raises=None):
        self.overrides = dict(overrides or {})
        self.raises = raises
        self.calls = []

    def run(self, probes):
        self.calls.append(list(probes))
        if self.raises:
            raise engine_bind.ProbeUnavailable(self.raises)
        out = []
        for p in probes:
            default = "connected" if p.address == FABRIC or p.port == engine_bind.CONTROL_PORT else "timeout"
            out.append(self.overrides.get((p.address, p.port), default))
        return out


def check(env_text=ENV_CLUSTER, ss=SS_WILDCARD, ip=None, prober=None, ip_raises=None,
          routes=DEFAULT_ROUTES, routes_raises=None, rdma=RDMA_LINKS, rdma_raises=None):
    prober = prober if prober is not None else FakeProber()
    made = []

    def default_routes():
        if routes_raises:
            raise routes_raises
        return routes

    def rdma_links():
        if rdma_raises:
            raise rdma_raises
        return rdma

    def factory(target):
        made.append(target)
        if not engine_bind.SSH_TARGET_RE.match(target):
            raise engine_bind.ProbeUnavailable("CLUSTER_WORKER_SSH is not of the form user@host, so there is no worker to probe from")
        return prober

    def ip_json():
        if ip_raises:
            raise ip_raises
        return json.dumps(IP_HEAD if ip is None else ip)

    env = engine_bind.read_env(pathlib.Path("/nonexistent")) if env_text is None else _env(env_text)
    report = engine_bind.evaluate(env, env_text is not None, ss, ip_json=ip_json, prober_factory=factory,
                                  default_routes=default_routes, rdma_links=rdma_links)
    text = "\n".join(report.lines)
    return report, text, prober, made


def _env(text):
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "generated.env"
        path.write_text(text, encoding="utf-8")
        return engine_bind.read_env(path)


class NoAddressEverReachesThePublicLog:
    def assertAddressFree(self, text):
        for needle in ADDRESS_TEXT:
            self.assertNotIn(needle, text, f"{needle!r} leaked into public output:\n{text}")
        self.assertIsNone(re.search(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text.replace("0.0.0.0", "")), text)


def _run_main(env_text, *argv, ss=None):
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        env_path = root / "generated.env"
        if env_text is not None:
            env_path.write_text(env_text, encoding="utf-8")
        args = [argv[0], "--generated-env", str(env_path), *argv[1:]]
        if ss is not None:
            (root / "ss.txt").write_text(ss, encoding="utf-8")
            args += ["--ss-output", str(root / "ss.txt")]
        if argv[0] == "resolve":
            args += ["--github-output", str(root / "out")]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = engine_bind.main(args)
        produced = (root / "out").read_text(encoding="utf-8") if argv[0] == "resolve" else ""
        return rc, out.getvalue(), err.getvalue(), produced


# ============================================================== resolve ======

class TheProbeGoesWhereTheEngineWasToldToListen(NoAddressEverReachesThePublicLog, unittest.TestCase):
    def test_the_bridge_gateway_bind_is_probed_at_the_bridge_gateway_not_loopback(self):
        rc, _, _, produced = _run_main(f"CLUSTER_API_BIND_ADDRESS={BRIDGE}\nVLLM_PORT=8000\n", "resolve")
        self.assertEqual(rc, 0)
        self.assertIn(f"url=http://{BRIDGE}:8000", produced)

    def test_a_missing_key_falls_back_to_loopback_and_the_default_port(self):
        rc, _, _, produced = _run_main("MAIN_MODEL=x\n", "resolve")
        self.assertEqual(rc, 0)
        self.assertIn("url=http://127.0.0.1:8000", produced)

    def test_a_missing_generated_env_falls_back_to_loopback(self):
        rc, _, _, produced = _run_main(None, "resolve")
        self.assertEqual(rc, 0)
        self.assertIn("url=http://127.0.0.1:8000", produced)

    def test_the_configured_port_is_used(self):
        _, _, _, produced = _run_main(f'CLUSTER_API_BIND_ADDRESS="{BRIDGE}"\nVLLM_PORT=8011\n', "resolve")
        self.assertIn(f"url=http://{BRIDGE}:8011", produced)

    def test_a_wildcard_configuration_is_probed_on_loopback_which_a_wildcard_answers(self):
        rc, _, _, produced = _run_main("CLUSTER_API_BIND_ADDRESS=0.0.0.0\n", "resolve")
        self.assertEqual(rc, 0)
        self.assertIn("url=http://127.0.0.1:8000", produced)

    def test_resolve_prints_no_address_to_the_log(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}):
            _, out, err, _ = _run_main(ENV_CLUSTER, "resolve")
        self.assertAddressFree(out + err)

    def test_under_actions_the_configured_address_is_masked_and_appears_only_in_the_mask_command(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            _, out, _, _ = _run_main(ENV_CLUSTER, "resolve")
        self.assertIn(f"::add-mask::{BRIDGE}", out.splitlines())
        self.assertAddressFree("\n".join(line for line in out.splitlines() if not line.startswith("::add-mask::")))


# ============================================== specific listeners: unchanged ==

class ASpecificListenerIsJudgedExactlyAsBefore(NoAddressEverReachesThePublicLog, unittest.TestCase):
    def test_the_configured_bridge_address_passes_without_asking_the_worker_anything(self):
        report, text, prober, made = check(ss=SS_BRIDGE + SS_OTHER_PORTS)
        self.assertEqual(report.failures, [])
        self.assertEqual((prober.calls, made), ([], []))
        self.assertAddressFree(text)

    def test_a_single_node_engine_published_on_loopback_passes_without_a_worker(self):
        report, _, prober, _ = check(env_text="MAIN_MODEL=x\n", ss=SS_LOOPBACK)
        self.assertEqual(report.failures, [])
        self.assertEqual(prober.calls, [])

    def test_a_single_node_with_no_generated_env_and_a_loopback_listener_passes(self):
        report, _, _, _ = check(env_text=None, ss=SS_LOOPBACK)
        self.assertEqual(report.failures, [])

    def test_the_configured_address_plus_a_loopback_listener_passes(self):
        report, _, _, _ = check(ss=SS_BRIDGE + SS_LOOPBACK)
        self.assertEqual(report.failures, [])

    def test_a_listener_on_any_other_specific_address_fails_and_the_address_is_not_printed(self):
        report, text, _, _ = check(ss=SS_LAN)
        self.assertEqual(len(report.failures), 1)
        self.assertIn("neither the configured address nor loopback", text)
        self.assertAddressFree(text)

    def test_a_specific_listener_elsewhere_still_fails_next_to_a_proven_wildcard(self):
        report, _, _, _ = check(ss=SS_WILDCARD + SS_LAN)
        self.assertEqual(len(report.failures), 1)

    def test_nothing_listening_fails_because_the_bind_cannot_be_proved(self):
        report, text, _, _ = check(ss=SS_OTHER_PORTS)
        self.assertTrue(report.failures)
        self.assertIn("nothing is listening on port 8000", text)

    def test_a_wildcard_on_a_different_port_is_not_this_gates_business(self):
        report, _, prober, _ = check(ss=SS_BRIDGE + SS_OTHER_PORTS)
        self.assertEqual(report.failures, [])
        self.assertEqual(prober.calls, [])


# ======================================== a wildcard passes only on proof ====

class AWildcardPassesOnlyWhenTheWorkerProvesItClosed(NoAddressEverReachesThePublicLog, unittest.TestCase):
    def test_a_guarded_wildcard_passes_when_the_fabric_connects_and_every_outside_address_is_blocked(self):
        report, text, prober, made = check()
        self.assertEqual(report.failures, [], text)
        self.assertEqual(made, [WORKER_SSH])
        self.assertIn("cluster fabric address: reachable from the worker (required)", text)
        self.assertIn("3 non-cluster addresses: 3 blocked, 0 ACCEPTED, 0 not proven (required: all blocked)", text)
        self.assertAddressFree(text)

    def test_the_production_state_before_the_host_filter_is_red_because_the_lan_accepts(self):
        report, text, _, _ = check(prober=FakeProber({(LAN, PORT): "connected"}))
        self.assertEqual(len(report.failures), 1, text)
        self.assertIn("1 non-cluster address (interface class: lan) ACCEPTED the connection", text)
        self.assertIn("non-cluster address (interface class: lan, IPv4): ACCEPTED the connection on the engine port", text)
        self.assertAddressFree(text)

    def test_a_tailnet_address_that_accepts_fails(self):
        report, text, _, _ = check(prober=FakeProber({(TAILNET6, PORT): "connected"}))
        self.assertTrue(report.failures)
        self.assertIn("(interface class: tailnet) ACCEPTED", text)
        self.assertAddressFree(text)

    def test_a_refusal_or_an_unreachable_answer_counts_as_blocked_once_the_control_port_proves_the_path(self):
        report, text, _, _ = check(prober=FakeProber({(LAN, PORT): "refused", (TAILNET4, PORT): "unreachable"}))
        self.assertEqual(report.failures, [], text)

    def test_every_global_non_cluster_address_is_probed_on_the_engine_port_and_on_the_control_port(self):
        _, _, prober, _ = check()
        probes = {(p.address, p.port) for p in prober.calls[0]}
        expected = {(FABRIC, PORT)} | {(a, PORT) for a in OUTSIDE} | {(a, engine_bind.CONTROL_PORT) for a in OUTSIDE}
        self.assertEqual(probes, expected)
        self.assertEqual((prober.calls[0][0].address, prober.calls[0][0].port), (FABRIC, PORT))

    def test_the_second_rail_the_bridges_loopback_and_link_local_addresses_are_never_probed(self):
        _, _, prober, _ = check()
        probed = {p.address for p in prober.calls[0]}
        for never in (FABRIC_B, BRIDGE, COMPOSE_BRIDGE, "127.0.0.1", "::1", "fe80::1", "fe80::2", "fe80::4"):
            self.assertNotIn(never, probed)

    def test_the_addresses_come_from_the_kernel_listing_not_from_a_list_in_the_gate(self):
        extra = IP_HEAD + [_link("wlan0", ("inet", "192.0.2.200", "global"))]
        report, text, prober, _ = check(ip=extra, prober=FakeProber({("192.0.2.200", PORT): "connected"}))
        self.assertIn(("192.0.2.200", PORT), {(p.address, p.port) for p in prober.calls[0]})
        self.assertIn("(interface class: wireless) ACCEPTED", text)
        self.assertTrue(report.failures)

    def test_a_global_address_parked_on_a_fabric_link_is_still_probed(self):
        ip = [dict(link) for link in IP_HEAD]
        ip[2] = _link("enp1s0", ("inet", FABRIC, "global"), ("inet6", "2001:db8::99", "global"))
        _, _, prober, _ = check(ip=ip)
        self.assertIn("2001:db8::99", {p.address for p in prober.calls[0]})

    def test_an_ipv6_wildcard_is_proved_the_same_way(self):
        ip = [link for link in IP_HEAD if link["ifname"] != "veth1234"]
        ip = [_link(l["ifname"], *[(i["family"], i["local"], i["scope"]) for i in l["addr_info"]
                                     if not (i["scope"] == "link" and l["ifname"] in ("eth0", "tailscale0"))]) for l in ip]
        self.assertEqual(check(ss=SS_IPV6_WILDCARD, ip=ip)[0].failures, [])
        self.assertTrue(check(ss=SS_STAR, ip=ip, prober=FakeProber({(LAN, PORT): "connected"}))[0].failures)

    def test_a_wildcard_configuration_alone_is_no_longer_a_failure(self):
        report, text, _, _ = check(env_text=ENV_CLUSTER_WILDCARD_CONFIG, ss=SS_WILDCARD)
        self.assertEqual(report.failures, [], text)
        self.assertIn("asks for a wildcard", text)

    def test_a_wildcard_configuration_makes_the_proof_mandatory_even_when_the_live_listener_is_loopback(self):
        _, _, prober, _ = check(env_text=ENV_CLUSTER_WILDCARD_CONFIG, ss=SS_LOOPBACK)
        self.assertEqual(len(prober.calls), 1)
        report, _, _, _ = check(env_text=ENV_CLUSTER_WILDCARD_CONFIG, ss=SS_LOOPBACK,
                                prober=FakeProber(raises="ssh to the worker failed (exit 255)"))
        self.assertTrue(report.failures)


# ============================ anything that makes the proof impossible fails ==

class AnythingThatMakesTheProofImpossibleIsAFailure(NoAddressEverReachesThePublicLog, unittest.TestCase):
    def assertFailsWith(self, fragment, **kwargs):
        report, text, _, _ = check(**kwargs)
        self.assertTrue(report.failures, text)
        self.assertIn(fragment, text)
        self.assertAddressFree(text)
        return report

    def test_the_fabric_connect_failing_fails_even_when_every_outside_address_is_blocked(self):
        self.assertFailsWith("cluster fabric address: NOT reachable from the worker (timed out)",
                             prober=FakeProber({(FABRIC, PORT): "timeout"}))

    def test_an_outside_timeout_without_a_reachable_control_port_is_not_proof(self):
        self.assertFailsWith("engine port timed out but the control port timed out",
                             prober=FakeProber({(TAILNET4, engine_bind.CONTROL_PORT): "timeout"}))

    def test_a_refused_control_port_is_not_proof_either(self):
        self.assertFailsWith("could not be proved closed",
                             prober=FakeProber({(LAN, engine_bind.CONTROL_PORT): "refused"}))

    def test_an_address_the_worker_owns_itself_is_not_proof(self):
        self.assertFailsWith("the worker owns itself", prober=FakeProber({(LAN, PORT): "local"}))

    def test_a_fabric_address_the_worker_owns_itself_is_not_proof(self):
        self.assertFailsWith("cluster fabric address: NOT reachable", prober=FakeProber({(FABRIC, PORT): "local"}))

    def test_an_unexpected_error_on_the_engine_connect_is_not_proof(self):
        self.assertFailsWith("NOT PROVEN (failed with an unexpected error)", prober=FakeProber({(LAN, PORT): "error"}))

    def test_ssh_failing_fails(self):
        self.assertFailsWith("ssh to the worker failed", prober=FakeProber(raises="ssh to the worker failed (exit 255)"))

    def test_python3_missing_on_the_worker_fails(self):
        self.assertFailsWith("python3 is not installed on the worker",
                             prober=FakeProber(raises="python3 is not installed on the worker, so the connects cannot be made"))

    def test_a_missing_generated_env_fails_a_wildcard(self):
        self.assertFailsWith("generated.env is missing", env_text=None)

    def test_a_single_node_wildcard_with_no_worker_to_probe_from_fails(self):
        self.assertFailsWith("the deployment is not clustered", env_text="TECHSARA_CLUSTER_MODE=single\n")

    def test_a_generated_env_without_the_cluster_mode_fails_a_wildcard(self):
        self.assertFailsWith("not clustered (TECHSARA_CLUSTER_MODE is unset)",
                             env_text=ENV_CLUSTER.replace("TECHSARA_CLUSTER_MODE=dual\n", ""))

    def test_a_missing_worker_ssh_target_fails(self):
        self.assertFailsWith("no worker to probe from",
                             env_text=ENV_CLUSTER.replace(f"CLUSTER_WORKER_SSH={WORKER_SSH}\n", ""))

    def test_a_missing_fabric_address_fails(self):
        self.assertFailsWith("cluster fabric address is unknown",
                             env_text=ENV_CLUSTER.replace(f"CLUSTER_HEAD_IP={FABRIC}\n", ""))

    def test_a_fabric_address_that_is_not_on_this_host_fails(self):
        self.assertFailsWith("not an address of this host",
                             env_text=ENV_CLUSTER.replace(f"CLUSTER_HEAD_IP={FABRIC}", "CLUSTER_HEAD_IP=203.0.113.77"))

    def test_no_addresses_found_fails(self):
        self.assertFailsWith("no addresses were found", ip=[])

    def test_the_kernel_listing_failing_fails(self):
        self.assertFailsWith("`ip -j addr` failed",
                             ip_raises=engine_bind.ProbeUnavailable("`ip -j addr` failed (exit 1)"))

    def test_an_unreadable_kernel_listing_fails(self):
        self.assertFailsWith("could not be read from the kernel", ip_raises=ValueError("not json"))

    def test_a_link_local_address_a_wildcard_covers_fails_because_this_gate_does_not_probe_it(self):
        report = self.assertFailsWith("link-local address(es) on non-cluster interfaces (interface class: lan, tailnet)",
                                      ss=SS_IPV6_WILDCARD)
        text = "\n".join(report.failures)
        # Honest wording: the worker CAN reach a LAN link-local address; this gate just does not probe one.
        self.assertIn("this gate does not probe link-local addresses", text)
        self.assertNotIn("cannot be probed from the worker", text)

    def test_the_default_routes_failing_to_read_fails(self):
        self.assertFailsWith("routes could not be read from the kernel",
                             routes_raises=ValueError("not json"))
        self.assertFailsWith("route show table all default` failed",
                             routes_raises=engine_bind.ProbeUnavailable(
                                 "`ip -j -4 route show table all default` failed (exit 1)"))

    def test_an_unexpected_exception_from_the_prober_is_a_reasoned_failure_not_a_crash(self):
        class Exploding:
            def run(self, probes):
                raise UnicodeDecodeError("utf-8", b"\xe9", 0, 1, "invalid continuation byte")

        report = self.assertFailsWith("the probe on the worker failed unexpectedly (UnicodeDecodeError)",
                                      prober=Exploding())
        self.assertEqual(len(report.failures), 1)

    def test_a_link_local_ipv6_address_an_ipv4_wildcard_cannot_accept_on_is_not_a_failure(self):
        report, text, _, _ = check(ss=SS_WILDCARD)
        self.assertEqual(report.failures, [], text)


# ============================================================ the ssh prober ==

def _completed(rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def _good_output(results):
    lines = [json.dumps(engine_bind.PROBE_HEADER)] + [json.dumps({"i": i, "r": r}) for i, r in enumerate(results)]
    return "\n".join(lines) + "\n"


PROBES = [engine_bind.Probe(FABRIC, 4, PORT), engine_bind.Probe(LAN, 4, PORT), engine_bind.Probe(LAN, 4, 22)]


class TheSshProber(unittest.TestCase):
    def prober(self, *responses, raises=None):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if raises:
                raise raises
            return responses[0]

        return engine_bind.SshProber(WORKER_SSH, run=run), calls

    def test_the_ssh_command_is_batch_mode_with_the_cluster_scripts_connect_timeout(self):
        prober, calls = self.prober(_completed(stdout=_good_output(["connected", "timeout", "connected"])))
        self.assertEqual(prober.run(PROBES), ["connected", "timeout", "connected"])
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[:7], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                                   "-o", "StrictHostKeyChecking=yes"])
        self.assertEqual(cmd[7:9], [WORKER_SSH, "--"])
        self.assertIn("command -v python3", cmd[-1])
        self.assertLessEqual(kwargs["timeout"], 8 + 3 * 4 + 15)

    def test_the_addresses_travel_to_the_worker_on_stdin_never_on_the_command_line(self):
        prober, calls = self.prober(_completed(stdout=_good_output(["connected", "timeout", "connected"])))
        prober.run(PROBES)
        cmd, kwargs = calls[0]
        self.assertNotIn(LAN, " ".join(cmd[6:]))
        self.assertIn(LAN, kwargs["input"])

    def test_the_production_prober_always_sends_the_self_probe_refusal(self):
        prober, calls = self.prober(_completed(stdout=_good_output(["connected", "timeout", "connected"])))
        prober.run(PROBES)
        self.assertIn("SELF_CHECK = True", calls[0][1]["input"])

    def test_an_ssh_failure_is_reported_without_repeating_ssh_text_which_names_the_host(self):
        prober, _ = self.prober(_completed(255, stderr="ssh: connect to host 203.0.113.2 port 22: Connection timed out\n"))
        with self.assertRaises(engine_bind.ProbeUnavailable) as caught:
            prober.run(PROBES)
        self.assertIn("timed out", str(caught.exception))
        self.assertNotIn("203.0.113.2", str(caught.exception))

    def test_an_unrecognised_ssh_error_is_reported_by_exit_status_alone(self):
        stderr = "kex_exchange_identification: read: Connection reset by peer\r\nConnection reset by 203.0.113.2 port 22\r\n"
        prober, _ = self.prober(_completed(255, stderr=stderr))
        with self.assertRaises(engine_bind.ProbeUnavailable) as caught:
            prober.run(PROBES)
        self.assertEqual(str(caught.exception), "ssh to the worker failed (exit 255)")

    def test_a_refused_key_is_reported_as_such(self):
        prober, _ = self.prober(_completed(255, stderr="operator@203.0.113.2: Permission denied (publickey).\n"))
        with self.assertRaisesRegex(engine_bind.ProbeUnavailable, "key authentication was refused"):
            prober.run(PROBES)

    def test_a_missing_python3_on_the_worker_is_reported_as_a_missing_tool(self):
        prober, _ = self.prober(_completed(engine_bind.MISSING_TOOL_EXIT, stdout=engine_bind.MISSING_TOOL_MARKER + "\n"))
        with self.assertRaisesRegex(engine_bind.ProbeUnavailable, "python3 is not installed on the worker"):
            prober.run(PROBES)

    def test_the_remote_wrapper_really_detects_a_missing_python3(self):
        env = {"PATH": "/nonexistent-bin-dir", "HOME": os.environ.get("HOME", "/tmp")}
        bash = "/bin/bash" if pathlib.Path("/bin/bash").exists() else "/usr/bin/bash"
        proc = subprocess.run([bash, "-c", engine_bind.REMOTE_COMMAND], input="", capture_output=True,
                              text=True, env=env, timeout=10)
        self.assertEqual(proc.returncode, engine_bind.MISSING_TOOL_EXIT)
        self.assertIn(engine_bind.MISSING_TOOL_MARKER, proc.stdout)

    def test_a_failing_probe_program_fails(self):
        prober, _ = self.prober(_completed(1, stderr="Traceback ... 192.0.2.54"))
        with self.assertRaises(engine_bind.ProbeUnavailable) as caught:
            prober.run(PROBES)
        self.assertNotIn(LAN, str(caught.exception))

    def test_output_without_the_header_is_not_trusted(self):
        prober, _ = self.prober(_completed(stdout='{"i": 0, "r": "connected"}\n{"i": 1, "r": "timeout"}\n{"i": 2, "r": "connected"}\n'))
        with self.assertRaisesRegex(engine_bind.ProbeUnavailable, "did not run"):
            prober.run(PROBES)

    def test_a_missing_result_is_not_trusted(self):
        prober, _ = self.prober(_completed(stdout=_good_output(["connected", "timeout"])))
        with self.assertRaisesRegex(engine_bind.ProbeUnavailable, "2 of 3"):
            prober.run(PROBES)

    def test_an_unknown_result_word_is_not_trusted(self):
        prober, _ = self.prober(_completed(stdout=_good_output(["connected", "blocked", "connected"])))
        with self.assertRaisesRegex(engine_bind.ProbeUnavailable, "malformed"):
            prober.run(PROBES)

    def test_a_duplicated_result_is_not_trusted(self):
        out = _good_output(["connected", "timeout", "connected"]) + json.dumps({"i": 1, "r": "connected"}) + "\n"
        prober, _ = self.prober(_completed(stdout=out))
        with self.assertRaisesRegex(engine_bind.ProbeUnavailable, "malformed"):
            prober.run(PROBES)

    def test_a_login_banner_is_ignored(self):
        prober, _ = self.prober(_completed(stdout="Welcome to 192.0.2.54\n" + _good_output(["connected", "refused", "connected"])))
        self.assertEqual(prober.run(PROBES), ["connected", "refused", "connected"])

    def test_a_probe_that_hangs_is_bounded_and_fails(self):
        prober, _ = self.prober(raises=subprocess.TimeoutExpired(cmd="ssh", timeout=1))
        with self.assertRaisesRegex(engine_bind.ProbeUnavailable, "did not finish within"):
            prober.run(PROBES)

    def test_non_utf8_bytes_from_a_real_subprocess_are_decoded_not_a_crash(self):
        # A real child process stands in for ssh, with the prober's own
        # subprocess keyword arguments: an sshd banner in Latin-1, then the
        # probe's output. Before the fix this raised UnicodeDecodeError.
        child = ("import sys; sys.stdin.read(); sys.stdout.buffer.write(b'Welcome \\xe9t\\xe9\\n' + "
                 + repr(_good_output(["connected", "timeout", "connected"]).encode()) + "); sys.stdout.flush()")

        def run(cmd, **kwargs):
            return subprocess.run([sys.executable, "-c", child], **kwargs)

        prober = engine_bind.SshProber(WORKER_SSH, run=run)
        self.assertEqual(prober.run(PROBES), ["connected", "timeout", "connected"])

    def test_non_utf8_bytes_on_a_real_ssh_failure_still_give_the_reason(self):
        child = ("import sys; sys.stdin.read(); sys.stderr.buffer.write(b'\\xff\\xfe Permission denied (publickey).\\n'); "
                 "sys.exit(255)")

        def run(cmd, **kwargs):
            return subprocess.run([sys.executable, "-c", child], **kwargs)

        with self.assertRaisesRegex(engine_bind.ProbeUnavailable, "key authentication was refused"):
            engine_bind.SshProber(WORKER_SSH, run=run).run(PROBES)

    def test_a_worker_target_that_could_be_read_as_an_ssh_option_is_refused_before_ssh_runs(self):
        for bad in ("-oProxyCommand=sh", "", "host-without-user", "user@host extra"):
            with self.assertRaises(engine_bind.ProbeUnavailable):
                engine_bind.SshProber(bad, run=lambda *a, **k: self.fail("ssh ran"))


class TheProbeProgramItself(unittest.TestCase):
    """The program that runs on the worker, executed here for real on loopback."""

    def run_program(self, probes, self_check=True):
        program = engine_bind.remote_program(probes, timeout_s=2, self_check=self_check)
        proc = subprocess.run([sys.executable, "-"], input=program, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout, engine_bind.parse_probe_output(proc.stdout, len(probes))

    def test_it_reports_connected_and_refused_by_index_and_never_prints_an_address(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        closed.bind(("127.0.0.1", 0))
        closed_port = closed.getsockname()[1]
        closed.close()
        try:
            stdout, results = self.run_program(
                [engine_bind.Probe("127.0.0.1", 4, listener.getsockname()[1]),
                 engine_bind.Probe("127.0.0.1", 4, closed_port)], self_check=False)
        finally:
            listener.close()
        self.assertEqual(results, ["connected", "refused"])
        self.assertNotIn("127.0.0.1", stdout)

    def test_it_refuses_to_count_an_address_the_prober_owns_itself(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        try:
            # 127.0.0.2 is local too, but its routed source is 127.0.0.1: the
            # bind signal is what catches it.
            _, results = self.run_program([engine_bind.Probe("127.0.0.1", 4, listener.getsockname()[1]),
                                           engine_bind.Probe("127.0.0.2", 4, listener.getsockname()[1])])
        finally:
            listener.close()
        self.assertEqual(results, ["local", "local"])


# ============================ the review findings of 2026-09-13, pinned =======

SS_MAPPED_WILDCARD = f"LISTEN 0      4096   [::ffff:0.0.0.0]:{PORT} *:*\n"
NO_FIREWALL = "connected"  # every address of the head answers every port


class NoFirewallProber(FakeProber):
    """The worker when no host filter is applied: everything connects."""

    def run(self, probes):
        self.calls.append(list(probes))
        return [NO_FIREWALL for _ in probes]


class AnIpv4MappedWildcardIsAWildcard(NoAddressEverReachesThePublicLog, unittest.TestCase):
    """`::ffff:0.0.0.0` on an AF_INET6 socket accepts every IPv4 address, but
    ipaddress does not call it unspecified. Matching it in generated.env used to
    pass as "on the configured address" with no probe at all."""

    def test_every_spelling_of_the_mapped_wildcard_is_recognised_as_an_ipv4_wildcard(self):
        for spelling in ("::ffff:0.0.0.0", "[::ffff:0.0.0.0]", "::ffff:0:0", "[::ffff:0.0.0.0]%eth0",
                         "[::ffff:0:0]", "[::]%eth0"):
            self.assertTrue(engine_bind.is_wildcard(spelling), spelling)
        for spelling in ("::ffff:0.0.0.0", "[::ffff:0:0]"):
            self.assertEqual(engine_bind.wildcard_families(spelling), {4}, spelling)
        for specific in ("::ffff:192.0.2.54", "::ffff:127.0.0.1", "2001:db8::7", "[fe80::1%eth0]"):
            self.assertFalse(engine_bind.is_wildcard(specific), specific)

    def test_brackets_and_a_device_scope_are_removed_in_either_order(self):
        for written, bare in (("[::ffff:0.0.0.0]%eth0", "::ffff:0.0.0.0"), ("[fe80::1%eth0]", "fe80::1"),
                              ("[::]%eth0", "::"), ("0.0.0.0%eth0", "0.0.0.0"), (" [2001:db8::7] ", "2001:db8::7"),
                              ("*%eth0", "*")):
            self.assertEqual(engine_bind._normalise(written), bare, written)

    def test_a_mapped_wildcard_listener_matching_a_mapped_wildcard_configuration_needs_the_outside_proof(self):
        for config in ("::ffff:0.0.0.0", "::ffff:0:0"):
            env = ENV_CLUSTER.replace(f"CLUSTER_API_BIND_ADDRESS={BRIDGE}", f"CLUSTER_API_BIND_ADDRESS={config}")
            report, text, prober, _ = check(env_text=env, ss=SS_MAPPED_WILDCARD, prober=NoFirewallProber())
            self.assertEqual(len(prober.calls), 1, f"{config}: the worker was never asked\n{text}")
            self.assertTrue(report.failures, f"{config}: an unguarded mapped wildcard passed\n{text}")
            self.assertIn("1 wildcard (IPv4)", text)
            self.assertNotIn("1 on the configured address", text)
            self.assertAddressFree(text)

    def test_a_mapped_wildcard_listener_is_proved_like_any_ipv4_wildcard_and_passes_when_guarded(self):
        report, text, _, _ = check(ss=SS_MAPPED_WILDCARD)
        self.assertEqual(report.failures, [], text)
        self.assertIn("cluster fabric address: reachable from the worker (required)", text)


class OnlyRealDockerBridgesAreExempt(NoAddressEverReachesThePublicLog, unittest.TestCase):
    """A bridge is exempt by what the kernel says it is, not by a name prefix."""

    def probed(self, ip, **kwargs):
        report, text, prober, _ = check(ip=ip, prober=NoFirewallProber(), **kwargs)
        self.assertAddressFree(text)
        return report, text, {p.address for p in prober.calls[0]} if prober.calls else set()

    def test_a_lan_address_on_a_host_bridge_that_is_not_dockers_is_probed(self):
        ip = [link for link in IP_HEAD if link["ifname"] != "eth0"] + [
            _link("br-lan", ("inet", LAN, "global"), ("inet6", "2001:db8::5", "global")),
            _link("eth1", master="br-lan"),
        ]
        report, text, probed = self.probed(ip, routes=frozenset({"br-lan"}))
        self.assertIn(LAN, probed)
        self.assertIn("2001:db8::5", probed)
        self.assertIn("(interface class: host bridge", text)
        self.assertTrue(report.failures)

    def test_a_bridge_whose_name_docker_never_gives_is_probed_even_with_no_members_and_no_default_route(self):
        ip = IP_HEAD + [_link("br-lan", ("inet", "192.0.2.77", "global"))]
        _, _, probed = self.probed(ip)
        self.assertIn("192.0.2.77", probed)

    def test_a_docker_shaped_bridge_that_enslaves_a_nic_is_the_lan_and_is_probed(self):
        ip = [link for link in IP_HEAD if link["ifname"] != "eth0"] + [
            _link("br-abcdef012345", ("inet", LAN, "global")),
            _link("eth1", master="br-abcdef012345"),
        ]
        _, _, probed = self.probed(ip, routes=frozenset())
        self.assertIn(LAN, probed)

    def test_a_docker_shaped_bridge_that_carries_a_default_route_is_probed(self):
        ip = [link for link in IP_HEAD if link["ifname"] != "eth0"] + [_link("br-abcdef012345", ("inet", LAN, "global"))]
        _, _, probed = self.probed(ip, routes=frozenset({"br-abcdef012345"}))
        self.assertIn(LAN, probed)

    def test_a_global_address_on_a_veth_is_probed(self):
        ip = IP_HEAD + [_link("veth9876", ("inet", "192.0.2.99", "global"), master="docker0")]
        _, _, probed = self.probed(ip)
        self.assertIn("192.0.2.99", probed)

    def test_real_docker_bridges_with_only_veth_members_stay_exempt(self):
        _, _, probed = self.probed(IP_HEAD)
        for never in (BRIDGE, COMPOSE_BRIDGE, "fe80::4", "fe80::6"):
            self.assertNotIn(never, probed)

    def test_a_wildcard_with_no_non_cluster_address_at_all_is_not_a_vacuous_pass(self):
        ip = [link for link in IP_HEAD if link["ifname"] not in ("eth0", "tailscale0")]
        report, text, _, _ = check(ip=ip, prober=NoFirewallProber(), routes=frozenset())
        self.assertIn("no non-cluster address was found on this host", text)
        self.assertTrue(report.failures)
        self.assertAddressFree(text)


class TheFabricAddressIsOnlyTrustedWhenTheKernelAgrees(NoAddressEverReachesThePublicLog, unittest.TestCase):
    """generated.env is the thing under test, so CLUSTER_HEAD_IP(_2) must sit on
    a link with no default route and the paired worker address on its subnet."""

    def test_the_production_shaped_fabric_is_trusted(self):
        report, text, _, _ = check()
        self.assertEqual(report.failures, [], text)
        self.assertNotIn("does not look like a cluster link", text)

    def test_a_second_rail_address_that_is_really_the_lan_is_probed_and_fails(self):
        env = ENV_CLUSTER.replace(f"CLUSTER_HEAD_IP_2={FABRIC_B}", f"CLUSTER_HEAD_IP_2={LAN}")
        report, text, prober, _ = check(env_text=env, prober=NoFirewallProber())
        self.assertIn("second-rail fabric address (CLUSTER_HEAD_IP_2) does not look like a cluster link", text)
        self.assertIn(LAN, {p.address for p in prober.calls[0]})
        self.assertIn("(interface class: lan, IPv4): ACCEPTED", text)
        self.assertAddressFree(text)

    def test_a_second_rail_address_that_is_really_the_lan_fails_even_when_the_lan_is_guarded(self):
        env = ENV_CLUSTER.replace(f"CLUSTER_HEAD_IP_2={FABRIC_B}", f"CLUSTER_HEAD_IP_2={LAN}")
        report, text, _, _ = check(env_text=env)
        self.assertEqual(len(report.failures), 1, text)
        self.assertIn("CLUSTER_HEAD_IP_2", report.failures[0])

    def test_a_primary_fabric_address_that_is_really_the_lan_fails_without_a_probe(self):
        ip = [_link("lo", ("inet", "127.0.0.1", "host")), _link("eth0", ("inet", LAN, "global"), ("inet6", "fe80::1", "link")),
              _link("docker0", ("inet", BRIDGE, "global"))]
        env = ENV_CLUSTER.replace(f"CLUSTER_HEAD_IP={FABRIC}", f"CLUSTER_HEAD_IP={LAN}").replace(
            f"CLUSTER_HEAD_IP_2={FABRIC_B}\n", "")
        report, text, prober, _ = check(env_text=env, ip=ip, prober=NoFirewallProber())
        self.assertIn("(CLUSTER_HEAD_IP) does not look like a cluster link", text)
        self.assertEqual(prober.calls, [])
        self.assertTrue(report.failures)
        self.assertAddressFree(text)

    def test_a_lan_port_with_no_default_route_and_the_worker_on_its_subnet_is_not_trusted_unless_it_is_an_rdma_link(self):
        """The re-review's case: the default route is on another NIC, the
        worker is on the same LAN, and only the RDMA test tells them apart."""
        env = ENV_CLUSTER.replace(f"CLUSTER_HEAD_IP={FABRIC}", f"CLUSTER_HEAD_IP={LAN}").replace(
            f"CLUSTER_WORKER_IP={WORKER}", "CLUSTER_WORKER_IP=192.0.2.60")
        report, text, prober, _ = check(env_text=env, prober=NoFirewallProber(), routes=frozenset({"tailscale0"}))
        self.assertIn("its interface is not an RDMA (RoCE) link", text)
        self.assertEqual(prober.calls, [])
        self.assertTrue(report.failures)
        self.assertAddressFree(text)

    def test_a_second_rail_that_is_not_an_rdma_link_is_probed_as_a_non_cluster_address(self):
        report, text, prober, _ = check(rdma=frozenset({"enp1s0"}))
        self.assertIn("(CLUSTER_HEAD_IP_2) does not look like a cluster link: its interface is not an RDMA", text)
        self.assertIn(FABRIC_B, {p.address for p in prober.calls[0]})
        self.assertTrue(report.failures)
        self.assertAddressFree(text)

    def test_rdma_links_that_cannot_be_read_fail_closed(self):
        report, text, prober, _ = check(rdma_raises=OSError("sysfs"))
        self.assertIn("RDMA links could not be read", text)
        self.assertTrue(report.failures)
        self.assertEqual(prober.calls, [])

    def test_the_rdma_reader_finds_only_interfaces_with_an_infiniband_device(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "enp1s0" / "device" / "infiniband" / "rocep1s0").mkdir(parents=True)
            (root / "eth0" / "device").mkdir(parents=True)
            (root / "enp9s0" / "device" / "infiniband").mkdir(parents=True)  # empty: not a device
            self.assertEqual(engine_bind._rdma_link_devs(root), frozenset({"enp1s0"}))

    def test_a_default_route_through_the_fabric_interface_alone_is_enough_to_distrust_it(self):
        report, text, _, _ = check(routes=frozenset({"eth0", "enp1s0"}))
        self.assertIn("a default route leaves through its interface", text)
        self.assertTrue(report.failures)
        self.assertAddressFree(text)

    def test_a_worker_address_off_the_fabric_subnet_alone_is_enough_to_distrust_it(self):
        env = ENV_CLUSTER.replace(f"CLUSTER_WORKER_IP={WORKER}", "CLUSTER_WORKER_IP=192.0.2.60")
        report, text, _, _ = check(env_text=env)
        self.assertIn("the paired worker address is not on its subnet", text)
        self.assertTrue(report.failures)
        self.assertAddressFree(text)

    def test_a_missing_worker_address_fails(self):
        env = ENV_CLUSTER.replace(f"CLUSTER_WORKER_IP={WORKER}\n", "")
        report, text, _, _ = check(env_text=env)
        self.assertIn("the paired worker address is missing", text)
        self.assertTrue(report.failures)

    def test_default_routes_are_read_from_every_table_and_every_next_hop(self):
        v4 = json.dumps([{"dst": "default", "gateway": "192.0.2.1", "dev": "eth0"},
                         {"dst": "default", "dev": "tailscale0", "table": "52"},
                         {"type": "unreachable", "dst": "default", "dev": "lo"}])
        v6 = json.dumps([{"dst": "default", "nexthops": [{"gateway": "fe80::1", "dev": "enp1s0"},
                                                           {"gateway": "fe80::9", "dev": "wlan0"}]}])
        self.assertEqual(engine_bind.parse_default_route_devs([v4, v6, ""]),
                         {"eth0", "tailscale0", "enp1s0", "wlan0"})

    def test_the_kernel_route_reader_asks_every_table_for_both_families(self):
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout='[{"dst": "default", "dev": "eth0"}]', stderr="")

        with mock.patch.object(engine_bind.subprocess, "run", fake_run), \
                mock.patch.object(engine_bind.shutil, "which", lambda name: "/usr/sbin/ip"):
            self.assertEqual(engine_bind._default_route_devs(), {"eth0"})
        self.assertEqual(seen, [["ip", "-j", "-4", "route", "show", "table", "all", "default"],
                                ["ip", "-j", "-6", "route", "show", "table", "all", "default"]])


# ===================================================== end to end via main() ==

class TheCommandLine(NoAddressEverReachesThePublicLog, unittest.TestCase):
    def run_check(self, env_text, ss, prober):
        def no_real_subprocess(*args, **kwargs):
            raise AssertionError(f"a unit test tried to run a real command: {args[0] if args else kwargs}")

        with mock.patch.object(engine_bind.subprocess, "run", no_real_subprocess), \
                mock.patch.object(engine_bind, "SshProber", lambda target: prober), \
                mock.patch.object(engine_bind, "_ip_addr_json", lambda: json.dumps(IP_HEAD)), \
                mock.patch.object(engine_bind, "_default_route_devs", lambda: DEFAULT_ROUTES), \
                mock.patch.object(engine_bind, "_rdma_link_devs", lambda: RDMA_LINKS), \
                mock.patch.object(engine_bind.socket, "gethostname", lambda: "head-node-name"):
            rc, out, err, _ = _run_main(env_text, "check", ss=ss)
        return rc, out + err

    def test_the_unguarded_production_shape_exits_1_with_an_address_free_report(self):
        prober = FakeProber({(LAN, PORT): "connected"})
        rc, text = self.run_check(ENV_CLUSTER, SS_WILDCARD, prober)
        self.assertEqual(rc, 1)
        self.assertEqual(len(prober.calls), 1, "the report must come from the worker's answers, not an ssh failure")
        self.assertIn("(interface class: lan) ACCEPTED the connection", text)
        self.assertIn("RESULT: FAILED (1 reason(s) above)", text)
        self.assertAddressFree(text)
        self.assertNotIn("head-node-name", text)

    def test_the_guarded_production_shape_exits_0(self):
        rc, text = self.run_check(ENV_CLUSTER, SS_WILDCARD, FakeProber())
        self.assertEqual(rc, 0, text)
        self.assertIn("RESULT: PASSED", text)
        self.assertAddressFree(text)

    def test_the_scrubber_redacts_a_value_the_wording_should_never_have_contained(self):
        text = engine_bind.scrub(f"leak {LAN} and {TAILNET6} and {WORKER_SSH}", [TAILNET6, WORKER_SSH, "operator"])
        self.assertAddressFree(text)


# ============================================================ pipeline.yml ==

class TheVerifyJobUsesTheGate(unittest.TestCase):
    """The shape of pipeline.yml's `verify` job, which the gate is useless without."""

    @classmethod
    def setUpClass(cls):
        import yaml

        pipeline = pathlib.Path(__file__).resolve().parents[2] / "pipeline.yml"
        cls.doc = yaml.safe_load(pipeline.read_text(encoding="utf-8"))
        cls.job = cls.doc["jobs"]["verify"]
        cls.steps = cls.job["steps"]
        cls.runs = [step.get("run", "") for step in cls.steps]

    def test_no_verify_step_probes_the_engine_at_a_hardcoded_loopback_address(self):
        for body in self.runs:
            self.assertNotIn("127.0.0.1:8000", body)
            self.assertNotIn("127.0.0.1:${port}", body)

    def test_the_engine_probes_use_the_resolved_url(self):
        joined = "\n".join(self.runs)
        self.assertIn("engine_bind.py resolve", joined)
        self.assertIn("${ENGINE_URL}/v1/models", joined)
        self.assertIn("${ENGINE_URL}/v1/chat/completions", joined)

    def test_the_exposure_check_is_the_last_step_and_runs_unless_cancelled(self):
        last = self.steps[-1]
        self.assertIn("engine_bind.py check", last.get("run", ""))
        self.assertIn("!cancelled()", str(last.get("if", "")))
        self.assertIn("pipefail", last.get("run", ""))
        self.assertIn("outside the cluster", last.get("name", ""))

    def test_the_exposure_check_cannot_be_softened_or_fed_evidence(self):
        last = self.steps[-1]
        self.assertNotIn("continue-on-error", last)
        self.assertNotIn("continue-on-error", self.job)
        self.assertNotIn("--ss-output", last.get("run", ""))
        self.assertNotIn("||", last.get("run", ""))
        condition = str(last.get("if", ""))
        self.assertNotIn("vars.", condition)
        self.assertNotIn("env.", condition)

    def test_the_step_is_bounded_well_inside_the_job_timeout(self):
        last = self.steps[-1]
        self.assertLess(int(last.get("timeout-minutes", 0)) or 999, int(self.job["timeout-minutes"]))

    def test_a_verify_failure_still_triggers_the_recovery_job(self):
        recovery = self.doc["jobs"]["recovery"]
        self.assertIn("verify", recovery["needs"])
        self.assertIn("needs.verify.result == 'failure'", recovery["if"])


if __name__ == "__main__":
    unittest.main()
