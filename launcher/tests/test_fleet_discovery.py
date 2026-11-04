"""scripts/lib/fleet.py: a new Spark joins by itself, and one that dies stays visible.

mDNS, SSH and the exporters are faked; everything else is the real code writing the
real files under a temporary root.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests`
    from support import REPO_ROOT


def load_fleet(root: str):
    with mock.patch.dict(os.environ, {"TECHSARA_ROOT": root}):
        spec = importlib.util.spec_from_file_location("fleet_under_test", REPO_ROOT / "scripts" / "lib" / "fleet.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["fleet_under_test"] = module
        spec.loader.exec_module(module)
    return module


#: What `avahi-browse -rtp --no-db-lookup _ssh._tcp` printed on the head, 2026-11-05,
#: with the same machine seen again on the RoCE rails, the Docker bridges and loopback.
AVAHI = r"""+;enP7s7;IPv4;spark-476e\032SSH;_ssh._tcp;local
=;br-086515c3ab95;IPv4;spark-0e68\032SSH;_ssh._tcp;local;spark-0e68.local;172.23.0.1;22;
=;enp1s0f1np1;IPv4;spark-0e68\032SSH;_ssh._tcp;local;spark-0e68.local;10.100.184.1;22;
=;enp1s0f1np1;IPv4;spark-476e\032SSH;_ssh._tcp;local;spark-476e.local;10.100.184.2;22;
=;enP2p1s0f1np1;IPv4;spark-476e\032SSH;_ssh._tcp;local;spark-476e.local;10.100.184.2;22;
=;enP7s7;IPv4;spark-0e68\032SSH;_ssh._tcp;local;spark-0e68.local;192.168.9.54;22;
=;enP7s7;IPv4;spark-476e\032SSH;_ssh._tcp;local;spark-476e.local;192.168.9.68;22;
=;enP7s7;IPv6;spark-476e\032SSH;_ssh._tcp;local;spark-476e.local;fe80::1;22;
=;enP7s7;IPv4;LAPTOP\032SSH;_ssh._tcp;local;LAPTOP-JTKFSCUG.local;192.168.8.104;22;
=;enP7s7;IPv4;MacBook\032SSH;_ssh._tcp;local;MacBook-Pro.local;192.168.9.89;22;
=;lo;IPv4;spark-0e68\032SSH;_ssh._tcp;local;spark-0e68.local;127.0.0.1;22;
"""
PATTERN = r"spark-[0-9a-f]{4}"
DUAL = {"TECHSARA_CLUSTER_MODE": "dual", "CLUSTER_WORKER_MGMT_IP": "192.168.9.68", "CLUSTER_WORKER_SSH": "techsphere@10.100.184.2"}


class FleetCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fleet = load_fleet(self.tmp.name)
        patcher = mock.patch.object(self.fleet.socket, "gethostname", return_value="spark-0e68")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.doc = self.fleet.load_registry()
        self.fleet.seed(self.doc, DUAL)

    def discover(self, found, answers=lambda _a: False, ok=(True, "ok"), enrolled=True, cfg=None):
        self.spark_checks = []
        self.enrolled = []

        def check(target, _cfg):
            self.spark_checks.append(target)
            return ok

        def enrol(member, _cfg):
            self.enrolled.append(member["address"])
            return enrolled

        self.fleet.discover(self.doc, {**DUAL, **(cfg or {})}, found, answers=answers, spark_check=check, enroll_fn=enrol)

    def targets(self, name="sparks-node.json"):
        self.fleet.render(self.doc)
        return json.loads((self.root / ".runtime" / "prometheus" / name).read_text())


class MdnsParsingTests(FleetCase):
    def test_only_sparks_on_the_management_interface_count(self):
        found = self.fleet.parse_avahi(AVAHI, "enP7s7", PATTERN)
        self.assertEqual(found, {"spark-0e68": "192.168.9.54", "spark-476e": "192.168.9.68"})

    def test_a_rail_address_is_never_used(self):
        # Monitoring must not share the fabric it measures, whatever mDNS says.
        found = self.fleet.parse_avahi(AVAHI, "enp1s0f1np1", PATTERN)
        self.assertEqual(found, {})

    def test_addresses_that_cannot_be_a_management_lan(self):
        for bad in ("127.0.0.1", "10.100.184.2", "10.100.185.1", "172.17.0.1", "172.23.0.1", "fe80::1", "169.254.1.1", "nonsense"):
            self.assertFalse(self.fleet.is_lan_address(bad), bad)
        self.assertTrue(self.fleet.is_lan_address("192.168.9.68"))

    def test_avahi_being_missing_is_not_a_crash(self):
        def missing(*_a, **_k):
            raise FileNotFoundError
        self.assertEqual(self.fleet.browse_mdns("enP7s7", PATTERN, run=missing), {})


class SeedTests(FleetCase):
    def test_the_head_is_always_known_and_never_a_file_target(self):
        self.assertEqual(self.doc["members"]["spark-1"]["role"], "head")
        self.assertFalse([g for g in self.targets() if g["labels"]["node"] == "spark-1"])

    def test_the_configured_worker_keeps_its_name_and_address(self):
        groups = self.targets()
        self.assertEqual(groups, [{"targets": ["192.168.9.68:9100"], "labels": {"node": "spark-2", "role": "worker"}}])
        self.assertEqual(self.targets("sparks-gpu.json")[0]["targets"], ["192.168.9.68:9835"])

    def test_a_single_node_install_has_no_phantom_worker(self):
        doc = self.fleet.load_registry()
        self.fleet.seed(doc, {"TECHSARA_CLUSTER_MODE": "single"})
        self.assertEqual(list(doc["members"]), ["spark-1"])

    def test_the_target_files_are_readable_by_prometheus_when_empty(self):
        doc = self.fleet.load_registry()
        self.fleet.seed(doc, {"TECHSARA_CLUSTER_MODE": "single"})
        self.fleet.render(doc)
        self.assertEqual(json.loads((self.root / ".runtime" / "prometheus" / "sparks-node.json").read_text()), [])


class NewSparkTests(FleetCase):
    NEW = {"spark-476e": "192.168.9.68", "spark-abcd": "192.168.9.70"}

    def test_exporters_already_answering_join_within_one_pass(self):
        self.discover(self.NEW, answers=lambda _a: True)
        member = self.doc["members"]["spark-3"]
        self.assertEqual((member["hostname"], member["address"], member["state"]), ("spark-abcd", "192.168.9.70", "scraping"))
        self.assertIn({"targets": ["192.168.9.70:9100"], "labels": {"node": "spark-3", "role": "member"}}, self.targets())

    def test_no_exporters_but_a_trusted_key_enrols_automatically(self):
        self.discover(self.NEW, answers=lambda a: a == "192.168.9.68")
        self.assertEqual(self.enrolled, ["192.168.9.70"])
        self.assertEqual(self.doc["members"]["spark-3"]["state"], "enrolling")
        # Not a target yet: it would read as DOWN while it starts.
        self.assertFalse([g for g in self.targets() if g["labels"]["node"] == "spark-3"])
        # The next pass finds the exporters.
        self.discover(self.NEW, answers=lambda _a: True)
        self.assertEqual(self.doc["members"]["spark-3"]["state"], "scraping")
        self.assertTrue([g for g in self.targets() if g["labels"]["node"] == "spark-3"])

    def test_no_key_says_exactly_that_and_starts_nothing(self):
        self.discover(self.NEW, answers=lambda a: a == "192.168.9.68", ok=(False, "ssh_refused"))
        self.assertEqual(self.enrolled, [])
        self.assertEqual(self.doc["members"]["spark-3"]["state"], "needs_ssh_key")
        public = (self.fleet.render(self.doc), json.loads((self.root / ".runtime" / "fleet" / "fleet.json").read_text()))[1]
        self.assertEqual(public["members"]["spark-3"]["state"], "needs_ssh_key")

    def test_a_machine_that_is_not_a_dgx_is_refused(self):
        self.discover(self.NEW, answers=lambda a: a == "192.168.9.68", ok=(False, "not_a_dgx_spark"))
        self.assertEqual(self.enrolled, [])
        self.assertEqual(self.doc["members"]["spark-3"]["state"], "not_a_dgx_spark")

    def test_automatic_enrolment_can_be_turned_off(self):
        self.discover(self.NEW, answers=lambda a: a == "192.168.9.68", cfg={"MONITORING_FLEET_AUTO_ENROLL": "false"})
        self.assertEqual(self.spark_checks, [])
        self.assertEqual(self.doc["members"]["spark-3"]["state"], "discovered")

    def test_a_failed_attempt_is_not_retried_every_minute(self):
        self.discover(self.NEW, answers=lambda a: a == "192.168.9.68", ok=(False, "ssh_refused"))
        self.assertEqual(len(self.spark_checks), 1)
        self.discover(self.NEW, answers=lambda a: a == "192.168.9.68", ok=(False, "ssh_refused"))
        self.assertEqual(self.spark_checks, [])
        self.assertEqual(self.doc["members"]["spark-3"]["state"], "needs_ssh_key")
        # Ten minutes later it tries again, and the key is there now.
        self.doc["members"]["spark-3"]["enroll_attempt"] -= self.fleet.ENROLL_RETRY_S
        self.discover(self.NEW, answers=lambda a: a == "192.168.9.68")
        self.assertEqual(self.enrolled, ["192.168.9.70"])

    def test_this_machine_is_not_discovered_as_a_new_one(self):
        self.discover({"spark-0e68": "192.168.9.54"}, answers=lambda _a: True)
        self.assertEqual(sorted(self.doc["members"]), ["spark-1", "spark-2"])

    def test_names_are_handed_out_once(self):
        members = {"spark-1": {}, "spark-2": {}, "spark-4": {}}
        self.assertEqual(self.fleet.next_name(members), "spark-3")


class StayingVisibleTests(FleetCase):
    ALL = {"spark-476e": "192.168.9.68", "spark-abcd": "192.168.9.70"}

    def join(self):
        self.discover(self.ALL, answers=lambda _a: True)

    def test_a_spark_that_dies_stays_a_target(self):
        # It must read as DOWN everywhere, not vanish from every panel.
        self.join()
        self.discover({}, answers=lambda _a: False)
        self.assertEqual(self.doc["members"]["spark-3"]["state"], "down")
        self.assertEqual(self.spark_checks, [])  # nothing to enrol into
        self.assertTrue([g for g in self.targets() if g["labels"]["node"] == "spark-3"])

    def test_exporters_down_on_a_visible_spark_are_repaired(self):
        self.join()
        self.discover(self.ALL, answers=lambda a: a == "192.168.9.68")
        self.assertEqual(self.enrolled, ["192.168.9.70"])

    def test_a_dhcp_move_updates_one_field_and_rebinds_the_exporters(self):
        # 2026-11-01: the worker moved 9.68 -> 9.3 and every exporter crash-looped.
        self.join()
        self.discover({"spark-476e": "192.168.9.3", "spark-abcd": "192.168.9.70"}, answers=lambda _a: True)
        self.assertEqual(self.doc["members"]["spark-2"]["address"], "192.168.9.3")
        self.assertEqual(self.enrolled, ["192.168.9.3"])
        self.assertIn({"targets": ["192.168.9.3:9100"], "labels": {"node": "spark-2", "role": "worker"}}, self.targets())

    def test_a_seeded_worker_learns_its_hostname_instead_of_becoming_a_new_spark(self):
        self.assertEqual(self.doc["members"]["spark-2"]["hostname"], "")
        self.discover({"spark-476e": "192.168.9.68"}, answers=lambda _a: True)
        self.assertEqual(self.doc["members"]["spark-2"]["hostname"], "spark-476e")
        self.assertEqual(sorted(self.doc["members"]), ["spark-1", "spark-2"])

    def test_forgetting_a_spark_removes_it_and_keeps_it_out(self):
        self.join()
        args = mock.Mock(node="spark-3")
        self.assertEqual(self.fleet.cmd_forget(args, self.doc, DUAL), 0)
        self.assertFalse([g for g in self.targets() if g["labels"]["node"] == "spark-3"])
        self.discover(self.ALL, answers=lambda _a: True)
        self.assertNotIn("spark-3", self.doc["members"])

    def test_the_head_cannot_be_forgotten(self):
        self.assertEqual(self.fleet.cmd_forget(mock.Mock(node="spark-1"), self.doc, DUAL), 2)

    def test_the_registry_survives_a_restart(self):
        self.join()
        self.fleet.render(self.doc)
        self.fleet.save_registry(self.doc)
        again = self.fleet.load_registry()
        self.fleet.seed(again, DUAL)
        self.assertEqual(again["members"]["spark-3"]["hostname"], "spark-abcd")

    def test_a_corrupt_registry_does_not_stop_discovery(self):
        path = self.root / ".runtime" / "fleet" / "registry.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken")
        doc = self.fleet.load_registry()
        self.assertEqual(doc["members"], {})


class EnrolmentTests(FleetCase):
    def calls(self, returncodes=None):
        recorded = []

        def run(command, **_kwargs):
            recorded.append(command)
            code = (returncodes or {}).get(len(recorded) - 1, 0)
            return mock.Mock(returncode=code, stdout="spark-ok\n", stderr="boom" if code else "")

        return recorded, run

    def test_it_only_touches_the_monitoring_project_and_binds_the_lan_address(self):
        recorded, run = self.calls()
        member = {"address": "192.168.9.70", "hostname": "spark-abcd"}
        self.assertTrue(self.fleet.enroll(member, DUAL, run=run))
        remote = recorded[-1][-1]
        self.assertIn("--project-name sf-local-ai-monitoring", remote)
        self.assertIn("-f compose.monitoring-worker.yaml up -d", remote)
        self.assertIn("MONITORING_WORKER_BIND=192.168.9.70", remote)
        self.assertNotIn("sudo", remote)
        self.assertNotIn("down", remote)

    def test_the_login_switch_travels_with_it(self):
        recorded, run = self.calls()
        self.fleet.enroll({"address": "192.168.9.70"}, {**DUAL, "MONITORING_DB_LOGIN_PROJECTS": "*"}, run=run)
        self.assertIn("MONITORING_DB_LOGIN_PROJECTS='*'", recorded[-1][-1])

    def test_every_file_it_copies_exists_and_none_holds_a_secret(self):
        for item in self.fleet.ENROLL_FILES:
            path = REPO_ROOT / item
            self.assertTrue(path.exists(), item)
            self.assertNotIn(".env", item)
            self.assertNotIn("secrets", item)

    def test_a_failing_step_stops_there_and_reports_it(self):
        recorded, run = self.calls({1: 1})
        self.assertFalse(self.fleet.enroll({"address": "192.168.9.70"}, DUAL, run=run))
        self.assertEqual(len(recorded), 2)

    def test_a_refused_key_is_told_apart_from_a_wrong_machine(self):
        refused = lambda *_a, **_k: mock.Mock(returncode=255, stdout="", stderr="Permission denied")
        self.assertEqual(self.fleet.is_a_spark("u@h", DUAL, run=refused), (False, "ssh_refused"))
        other = lambda *_a, **_k: mock.Mock(returncode=1, stdout="", stderr="")
        self.assertEqual(self.fleet.is_a_spark("u@h", DUAL, run=other), (False, "not_a_dgx_spark"))
        good = lambda *_a, **_k: mock.Mock(returncode=0, stdout="spark-ok\n", stderr="")
        self.assertEqual(self.fleet.is_a_spark("u@h", DUAL, run=good), (True, "ok"))

    def test_ssh_never_prompts(self):
        # This runs from a timer: a password prompt would hang it for ever.
        self.assertIn("BatchMode=yes", self.fleet.SSH_OPTIONS)

    def test_the_worker_uses_the_configured_ssh_target_and_a_new_spark_its_lan_address(self):
        self.assertEqual(self.fleet.ssh_target(self.doc["members"]["spark-2"], DUAL), "techsphere@10.100.184.2")
        target = self.fleet.ssh_target({"address": "192.168.9.70"}, {"MONITORING_FLEET_SSH_USER": "ops"})
        self.assertEqual(target, "ops@192.168.9.70")

    def test_settings_never_read_the_secrets_file(self):
        (self.root / ".runtime").mkdir(exist_ok=True)
        (self.root / ".runtime" / "secrets.env").write_text("GRAFANA_ADMIN_PASSWORD=nope\n")
        (self.root / ".env").write_text("CLUSTER_MODE=dual\n# comment\nexport MONITORING_FLEET_SSH_USER='ops'\n")
        cfg = self.fleet.settings()
        self.assertEqual(cfg["MONITORING_FLEET_SSH_USER"], "ops")
        self.assertNotIn("GRAFANA_ADMIN_PASSWORD", cfg)


if __name__ == "__main__":
    unittest.main()
