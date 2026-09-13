"""scripts/host-guard.sh: the packet filter that closes the engine ports (option A).

WHY (developer-platform audit F050/F042/F012/F065 and F043/F051; owner
decision 2026-09-13). The owner kept the main vLLM head on 0.0.0.0:8000 and the
worker's OCR and speech engines on the worker's management address, because the
narrower head bind of commit 229031c would have taken the two-node engine down:
the worker's container healthcheck curls http://10.100.184.1:8000/health over
RoCE rail A and kills its own rank after 8 misses. The exposure is closed by an
nftables table instead. A wrong rule in that table is an outage on a
production cluster, so every property the owner asked for is pinned here:

* the rail and Docker sources reach 8000, the office LAN and the tailnet do not;
* ssh, the rank ports and every non-TCP packet are never judged;
* the first rule keeps established connections;
* one table, no flush, no Docker chain;
* `plan` needs no root and changes nothing;
* `apply` refuses, before nft is ever called, a plan that drops a declared
  consumer or a peer connected right now; installs with one `nft -f`; re-runs
  install the same bytes;
* `remove` deletes only its own table.

Nothing here runs the real nft, ip, ss, ssh or curl: each is a fake on PATH
that reads a fixture and records its argv. The rules are also read back by a
Python evaluator written independently of the script's bash one, so a bug has
to be made twice, the same way, to pass.
"""

from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

try:
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests` imports top-level modules.
    from support import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "host-guard.sh"

HEAD_ADDRS = """\
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever
1: lo    inet6 ::1/128 scope host noprefixroute \\       valid_lft forever preferred_lft forever
2: enP7s7    inet 192.168.9.54/22 brd 192.168.11.255 scope global dynamic noprefixroute enP7s7\\       valid_lft 1 preferred_lft 1
4: enp1s0f1np1    inet 10.100.184.1/24 brd 10.100.184.255 scope global noprefixroute enp1s0f1np1\\       valid_lft forever preferred_lft forever
6: enP2p1s0f1np1    inet 10.100.185.1/24 brd 10.100.185.255 scope global noprefixroute enP2p1s0f1np1\\       valid_lft forever preferred_lft forever
8: tailscale0    inet 100.94.16.2/32 scope global tailscale0\\       valid_lft forever preferred_lft forever
9: br-51534c8adf93    inet 172.18.0.1/16 brd 172.18.255.255 scope global br-51534c8adf93\\       valid_lft forever preferred_lft forever
10: br-6268c6fe25f0    inet 172.19.0.1/16 brd 172.19.255.255 scope global br-6268c6fe25f0\\       valid_lft forever preferred_lft forever
11: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever preferred_lft forever
"""

# `ss -tnH state established`: Recv-Q Send-Q Local Peer, the shapes the head showed on 2026-09-13.
HEAD_ESTABLISHED = """\
0      0      172.17.0.1:8000       172.18.0.19:51234
0      0      [::ffff:172.17.0.1]:9100  [::ffff:172.18.0.15]:40022
0      0      172.17.0.1:9838       172.18.0.20:60010
0      0      10.100.184.1:8000     10.100.184.2:45888
0      0      192.168.9.54:22       192.168.9.20:61000
0      0      127.0.0.1:8000        192.168.9.54:38000
"""

# peer -> what `ip -o route get` names as the device ("local" for one of ours).
HEAD_ROUTES = {
    "172.18.0.19": "br-51534c8adf93",
    "172.18.0.15": "br-51534c8adf93",
    "172.18.0.20": "br-51534c8adf93",
    "10.100.184.2": "enp1s0f1np1",
    "192.168.9.20": "enP7s7",
    "192.168.9.77": "enP7s7",
    "192.168.9.54": "local",
}


# ---------------------------------------------------------------- reader ----
def chain_rules(ruleset: str) -> list[str]:
    body = ruleset.split("chain input {", 1)[1].split("\n  }", 1)[0]
    rules = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("type "):
            continue
        rules.append(line)
    return rules


def set_elements(ruleset: str, name: str) -> list[tuple[int, int]]:
    match = re.search(rf"set {name} \{{.*?elements = \{{([^}}]*)\}}", ruleset, re.S)
    assert match, f"set {name} missing"
    spans = []
    for element in match.group(1).split(","):
        element = element.strip()
        low, _, high = element.partition("-")
        spans.append((int(low), int(high or low)))
    return spans


def python_verdict(ruleset: str, port: int, ifname: str, saddr: str, proto: str = "tcp", established: bool = False) -> str:
    """nft's first-match semantics for the rule shapes the script emits."""
    source = ipaddress.ip_address(saddr)
    for rule in chain_rules(ruleset):
        text = re.sub(r' comment "[^"]*"$', "", rule)
        text = re.sub(r"\bcounter\b ?", "", text).strip()
        verdict = text.rsplit(" ", 1)[-1]
        ok = True
        if text.startswith("ct state established,related"):
            ok = established
        elif text.startswith("meta l4proto != tcp"):
            ok = proto != "tcp"
        else:
            m = re.search(r'iifname "([^"]+)"', text)
            if m and not fnmatch.fnmatchcase(ifname, m.group(1)):
                ok = False
            m = re.search(r"ip saddr (\S+)", text)
            if m and (source.version != 4 or source not in ipaddress.ip_network(m.group(1), strict=False)):
                ok = False
            m = re.search(r"tcp dport (!= )?@(\w+)", text)
            if m:
                if proto != "tcp":
                    ok = False
                else:
                    inside = any(lo <= port <= hi for lo, hi in set_elements(ruleset, m.group(2)))
                    if inside == bool(m.group(1)):
                        ok = False
        if ok:
            return verdict
    return "accept"


class HostGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        if shutil.which("bash") is None or shutil.which("sha256sum") is None:
            self.skipTest("bash and coreutils are required")
        temporary = tempfile.TemporaryDirectory(prefix="techsara-guard-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        self.log.touch()
        self.state = self.root / "state"
        (self.root / "addrs").write_text(HEAD_ADDRS)
        (self.root / "established").write_text(HEAD_ESTABLISHED)
        (self.root / "routes").write_text("".join(f"{k} {v}\n" for k, v in HEAD_ROUTES.items()))
        self._fake("id", 'if [ "${1:-}" = -u ]; then echo "${FAKE_UID:-1000}"; else echo tester; fi\n')
        self._fake(
            "nft",
            'echo "nft $*" >>"$FAKE_LOG"\n'
            'case "$*" in\n'
            '  "-c -f "*) [ "${FAKE_NFT_CHECK_FAIL:-0}" = 1 ] && exit 1; exit 0 ;;\n'
            '  "-f "*) n=$(ls "$FAKE_DIR"/installed-* 2>/dev/null | wc -l); cp "$2" "$FAKE_DIR/installed-$n"; touch "$FAKE_DIR/table"; exit 0 ;;\n'
            '  "list table inet techsara_guard") [ -e "$FAKE_DIR/table" ] ;;\n'
            '  "delete table inet techsara_guard") rm -f "$FAKE_DIR/table" ;;\n'
            "  *) exit 64 ;;\n"
            "esac\n",
        )
        self._fake(
            "ip",
            'echo "ip $*" >>"$FAKE_LOG"\n'
            'case "$*" in\n'
            '  "-o addr show"|"-o -4 addr show") cat "$FAKE_DIR/addrs" ;;\n'
            '  "-o route get "*) dev=$(awk -v p="$4" \'$1 == p { print $2 }\' "$FAKE_DIR/routes");\n'
            '     [ -n "$dev" ] || exit 2;\n'
            '     if [ "$dev" = local ]; then echo "local $4 dev lo table local src $4 uid 1000 \\    cache <local>";\n'
            '     else echo "$4 dev $dev src 0.0.0.0 uid 1000 \\    cache"; fi ;;\n'
            "  *) exit 64 ;;\n"
            "esac\n",
        )
        self._fake("ss", 'echo "ss $*" >>"$FAKE_LOG"\ncat "$FAKE_DIR/established"\n')
        self._fake("curl", 'echo "curl $*" >>"$FAKE_LOG"\nprintf 200\n')
        self._fake("ssh", 'echo "ssh $*" >>"$FAKE_LOG"\nexit 255\n')
        self._fake("sudo", 'echo "sudo $*" >>"$FAKE_LOG"\nexit 99\n')

    def _fake(self, name: str, body: str) -> None:
        path = self.bin / name
        path.write_text("#!/usr/bin/env bash\n" + body)
        path.chmod(0o755)

    def run_guard(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.root),
            "FAKE_LOG": str(self.log),
            "FAKE_DIR": str(self.root),
            "GUARD_STATE_DIR": str(self.state),
            "LC_ALL": "C",
        }
        environment.update(env)
        return subprocess.run(
            ["bash", str(SCRIPT), *args], capture_output=True, text=True, env=environment, timeout=120
        )

    def plan(self, role: str, **env: str) -> str:
        result = self.run_guard("plan", "--role", role, **env)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def calls(self, tool: str) -> list[str]:
        return [line for line in self.log.read_text().splitlines() if line.startswith(tool + " ")]

    # -- what the head's ruleset does ---------------------------------------
    def test_the_rail_and_docker_sources_reach_8000_and_the_office_lan_and_tailnet_are_dropped(self) -> None:
        ruleset = self.plan("head")
        accepted = [
            ("enp1s0f1np1", "10.100.184.2"),      # worker healthcheck: 8 misses kill the TP=2 engine
            ("enP2p1s0f1np1", "10.100.185.2"),    # rail B failover
            ("docker0", "172.17.0.4"),            # litellm-dgx
            ("br-51534c8adf93", "172.18.0.19"),   # orchestrator
            ("br-51534c8adf93", "172.18.0.15"),   # Prometheus
            ("br-6268c6fe25f0", "172.19.0.7"),    # inference bridge
            ("lo", "127.0.0.1"),
            ("lo", "192.168.9.54"),               # controller canaries arrive on lo with the LAN source
        ]
        for ifname, source in accepted:
            with self.subTest(ifname=ifname, source=source):
                self.assertEqual(python_verdict(ruleset, 8000, ifname, source), "accept")
        dropped = [
            ("enP7s7", "192.168.9.20"),
            ("enP7s7", "192.168.9.68"),
            ("enP7s7", "172.18.0.19"),            # spoofed bridge source from the LAN
            ("enP7s7", "10.100.184.2"),           # spoofed rail source from the LAN
            ("tailscale0", "100.64.0.9"),
            ("tailscale0", "fd7a:115c:a1e0::9"),
            ("enP7s7", "fe80::1"),
        ]
        for port in (8000, 8001, 8005, 9100, 9835, 9838):
            for ifname, source in dropped:
                with self.subTest(port=port, ifname=ifname, source=source):
                    self.assertEqual(python_verdict(ruleset, port, ifname, source), "drop")

    def test_ssh_the_torch_master_port_and_the_nccl_listeners_are_never_judged(self) -> None:
        for role in ("head", "worker"):
            ruleset = self.plan(role)
            for port, ifname, source in [
                (22, "enP7s7", "192.168.9.20"),
                (22, "tailscale0", "fd7a:115c:a1e0::9"),
                (29501, "enp1s0f1np1", "10.100.184.2"),
                (33183, "enp1s0f1np1", "10.100.184.1"),
                (3000, "enP7s7", "192.168.9.20"),
            ]:
                with self.subTest(role=role, port=port, ifname=ifname):
                    self.assertEqual(python_verdict(ruleset, port, ifname, source), "accept")

    def test_established_connections_and_non_tcp_packets_pass_before_any_drop(self) -> None:
        for role in ("head", "worker"):
            rules = chain_rules(self.plan(role))
            self.assertTrue(rules[0].startswith("ct state established,related accept"), rules[0])
            first_drop = next(i for i, rule in enumerate(rules) if re.search(r"\bdrop\b", rule))
            l4 = next(i for i, rule in enumerate(rules) if rule.startswith("meta l4proto != tcp accept"))
            self.assertLess(l4, first_drop)
            ruleset = self.plan(role)
            self.assertEqual(python_verdict(ruleset, 8000, "enP7s7", "192.168.9.20", established=True), "accept")
            # DHCP, neighbour discovery and tailscale's WireGuard arrive on the LAN as UDP/ICMP.
            self.assertEqual(python_verdict(ruleset, 9100, "enP7s7", "192.168.8.1", proto="udp"), "accept")
            self.assertEqual(python_verdict(ruleset, 0, "tailscale0", "100.64.0.9", proto="icmp"), "accept")

    def test_the_plan_owns_one_table_and_never_flushes_or_touches_dockers_chains(self) -> None:
        for role in ("head", "worker"):
            ruleset = self.plan(role)
            statements = [l for l in ruleset.splitlines() if l and not l.startswith("#")]
            self.assertEqual(statements[:3], [
                "table inet techsara_guard",
                "delete table inet techsara_guard",
                "table inet techsara_guard {",
            ])
            self.assertEqual(re.findall(r"^table (\S+ \S+)", ruleset, re.M), ["inet techsara_guard"] * 2)
            for forbidden in ("flush", "DOCKER", "iptables", "hook forward", "hook prerouting", "hook output", "policy drop", "nat"):
                self.assertNotIn(forbidden, ruleset.replace("# techsara", ""))
            self.assertEqual(re.findall(r"hook (\w+) priority (-?\d+)", ruleset), [("input", "-10")])

    def test_every_rule_comment_fits_nfts_128_byte_limit(self) -> None:
        for role in ("head", "worker"):
            for comment in re.findall(r'comment "([^"]*)"', self.plan(role)):
                self.assertLessEqual(len(comment.encode()), 128, comment)

    # -- the worker's ruleset --------------------------------------------------
    def test_only_the_head_reaches_the_worker_ocr_and_speech_engines_over_the_lan(self) -> None:
        ruleset = self.plan("worker")
        for port in (30004, 30007, 9100, 9835):
            self.assertEqual(python_verdict(ruleset, port, "enP7s7", "192.168.9.54"), "accept")
            self.assertEqual(python_verdict(ruleset, port, "enP7s7", "192.168.9.20"), "drop")
            self.assertEqual(python_verdict(ruleset, port, "tailscale0", "100.64.0.9"), "drop")
            self.assertEqual(python_verdict(ruleset, port, "lo", "192.168.9.68"), "accept")
        self.assertEqual(python_verdict(ruleset, 9839, "enp1s0f1np1", "10.100.184.1"), "accept")
        self.assertEqual(python_verdict(ruleset, 9839, "enP7s7", "192.168.9.54"), "drop")

    def test_the_scripts_own_evaluator_agrees_with_an_independent_reading_of_the_rules(self) -> None:
        cases = {
            "head": [(8000, "enp1s0f1np1", "10.100.184.2"), (8000, "enP7s7", "192.168.9.20"), (9838, "br-x", "172.18.0.20"),
                     (8003, "tailscale0", "fd7a:115c:a1e0::9"), (22, "enP7s7", "1.2.3.4"), (8000, "wlP9s9", "192.168.50.3"),
                     (9100, "docker0", "172.17.0.4"), (8000, "enP2p1s0f1np1", "10.100.184.2")],
            "worker": [(30004, "enP7s7", "192.168.9.54"), (30004, "enP7s7", "192.168.9.55"), (9839, "enp1s0f1np1", "10.100.184.1"),
                       (9839, "enP7s7", "192.168.9.54"), (30007, "tailscale0", "100.64.0.9"), (33183, "enP7s7", "192.168.9.20")],
        }
        for role, rows in cases.items():
            ruleset = self.plan(role)
            for port, ifname, source in rows:
                with self.subTest(role=role, port=port, ifname=ifname, source=source):
                    result = self.run_guard("explain", str(port), ifname, source, "--role", role)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.split("\t", 1)[0], python_verdict(ruleset, port, ifname, source))

    # -- plan --------------------------------------------------------------------
    def test_plan_needs_no_root_and_calls_no_privileged_tool(self) -> None:
        for role in ("head", "worker"):
            self.plan(role)
        self.assertEqual(self.calls("nft") + self.calls("sudo") + self.calls("ss"), [])
        self.assertFalse(self.state.exists())

    def test_plan_itself_refuses_a_ruleset_that_would_drop_the_worker_healthcheck(self) -> None:
        result = self.run_guard("plan", "--role", "head", GUARD_RAIL_A_SUBNET="10.100.99.0/24")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worker vllm-worker healthcheck", result.stderr)

    # -- apply -------------------------------------------------------------------
    def test_apply_refuses_before_nft_when_a_declared_consumer_would_be_dropped(self) -> None:
        # A mistyped rail subnet: the worker's healthcheck from 10.100.184.2 falls to the final drop.
        result = self.run_guard("apply", "--role", "head", FAKE_UID="0", GUARD_RAIL_A_SUBNET="10.100.99.0/24")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("SELF-TEST FAIL: head:8000 from 10.100.184.2 on enp1s0f1np1", result.stderr)
        self.assertIn("Nothing was changed", result.stderr)
        self.assertEqual(self.calls("nft"), [])
        self.assertFalse((self.root / "table").exists())

    def test_apply_refuses_without_root_and_changes_nothing(self) -> None:
        result = self.run_guard("apply", "--role", "head")
        self.assertEqual(result.returncode, 2)
        self.assertIn("needs root", result.stderr)
        self.assertEqual(self.calls("nft"), [])

    def test_apply_refuses_when_a_peer_connected_now_would_lose_its_next_connection(self) -> None:
        (self.root / "established").write_text(HEAD_ESTABLISHED + "0 0 192.168.9.54:8000 192.168.9.77:50000\n")
        result = self.run_guard("apply", "--role", "head", FAKE_UID="0")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("192.168.9.77 (via enP7s7) is connected to :8000", result.stderr)
        self.assertEqual(self.calls("nft"), [])

    def test_the_established_lan_ssh_session_does_not_block_apply(self) -> None:
        # HEAD_ESTABLISHED carries 192.168.9.20 -> :22 over the LAN; 22 is not a guarded port.
        result = self.run_guard("apply", "--role", "head", FAKE_UID="0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_apply_refuses_when_a_docker_style_bridge_has_a_name_the_rules_do_not_accept(self) -> None:
        (self.root / "addrs").write_text(HEAD_ADDRS + "12: mybridge    inet 172.30.0.1/16 scope global mybridge\n")
        result = self.run_guard("apply", "--role", "head", FAKE_UID="0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("mybridge carries 172.30.0.1/16", result.stderr)
        self.assertEqual(self.calls("nft"), [])

    def test_apply_refuses_on_a_host_whose_rail_interface_is_named_differently(self) -> None:
        result = self.run_guard("apply", "--role", "head", FAKE_UID="0", GUARD_RAIL_A_IFNAME="enp9s0")
        self.assertEqual(result.returncode, 2)
        self.assertIn("interface enp9s0 is not on this host", result.stderr)
        self.assertEqual(self.calls("nft"), [])

    def test_apply_installs_exactly_the_plan_in_one_checked_transaction_and_a_rerun_installs_the_same_bytes(self) -> None:
        planned = self.plan("head")
        for _ in range(2):
            result = self.run_guard("apply", "--role", "head", FAKE_UID="0")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        nft = self.calls("nft")
        self.assertEqual([c.split(" ")[1] for c in nft], ["-c", "-f", "list"] * 2)
        installed = sorted(self.root.glob("installed-*"))
        self.assertEqual(len(installed), 2)
        self.assertEqual(installed[0].read_text(), planned)
        self.assertEqual(installed[1].read_text(), planned)
        state = (self.state / "state").read_text()
        self.assertIn("GUARD_ROLE=head\n", state)
        self.assertIn(f"GUARD_RULESET_SHA256={hashlib.sha256(planned.encode()).hexdigest()}\n", state)
        self.assertEqual((self.state / "ruleset.nft").read_text(), planned)

    def test_apply_stops_when_nft_rejects_the_ruleset_in_check_mode(self) -> None:
        result = self.run_guard("apply", "--role", "head", FAKE_UID="0", FAKE_NFT_CHECK_FAIL="1")
        self.assertEqual(result.returncode, 2)
        self.assertEqual([c.split(" ")[1] for c in self.calls("nft")], ["-c"])
        self.assertFalse((self.root / "table").exists())
        self.assertFalse((self.state / "state").exists())

    # -- remove ------------------------------------------------------------------
    def test_remove_deletes_only_its_own_table(self) -> None:
        self.assertEqual(self.run_guard("apply", "--role", "head", FAKE_UID="0").returncode, 0)
        self.log.write_text("")
        result = self.run_guard("remove", FAKE_UID="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("nft"), ["nft list table inet techsara_guard", "nft delete table inet techsara_guard"])
        self.assertFalse((self.state / "state").exists())

    def test_remove_on_a_host_without_the_table_is_a_no_op(self) -> None:
        result = self.run_guard("remove", FAKE_UID="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("nft"), ["nft list table inet techsara_guard"])

    def test_remove_refuses_without_root(self) -> None:
        self.assertEqual(self.run_guard("remove").returncode, 2)
        self.assertEqual(self.calls("nft"), [])

    # -- verify ------------------------------------------------------------------
    def test_verify_fails_when_there_is_no_proof_the_guard_is_installed(self) -> None:
        result = self.run_guard("verify", "--role", "head", "--no-remote")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no proof the guard is in place", result.stdout)

    def test_verify_as_root_fails_when_the_table_is_missing_even_if_the_ports_answer(self) -> None:
        result = self.run_guard("verify", "--role", "head", "--no-remote", FAKE_UID="0")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FAIL  table inet techsara_guard is not installed", result.stdout)

    def test_verify_passes_after_apply_and_reads_the_state_file_without_root(self) -> None:
        self.assertEqual(self.run_guard("apply", "--role", "head", FAKE_UID="0").returncode, 0)
        result = self.run_guard("verify", "--role", "head", "--no-remote")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the installed ruleset is the one this checkout plans", result.stdout)
        self.assertTrue(any("http://127.0.0.1:8000/health" in c for c in self.calls("curl")))


if __name__ == "__main__":
    unittest.main()
