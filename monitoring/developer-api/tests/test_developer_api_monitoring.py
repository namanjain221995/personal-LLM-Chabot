"""Offline checks for the Developer API dashboard, its alert rules, the metric
contract and the host packet filter's textfile writer (2026-09-13).

Nothing here needs Prometheus, Grafana or root. The promtool unit tests
(monitoring/prometheus/tests/developer_api.yml) prove the alert logic; the
live proof that every metric exists or is honestly marked is
check_metrics.py. These tests pin the rest: schema sanity of the dashboard,
that no queried metric is undeclared, that an empty panel says why, and that
the writer script tells "missing" from "could not look".

    python3 -m unittest discover -s monitoring/developer-api/tests -v
"""
from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
MONITORING = PKG.parent
sys.path.insert(0, str(PKG))

import check_metrics  # noqa: E402

try:
    import yaml  # noqa: F401
    HAVE_YAML = True
except ImportError:  # pragma: no cover - every environment here has PyYAML
    HAVE_YAML = False

DASHBOARD = MONITORING / "grafana" / "dashboards" / "dgx-developer-api.json"
RULES = MONITORING / "prometheus" / "rules" / "developer-api.yml"
PROMTOOL_TESTS = MONITORING / "prometheus" / "tests" / "developer_api.yml"
CONTRACT = PKG / "metrics-contract.json"
README = PKG / "README.md"
WRITER = MONITORING / "exporters" / "host-guard" / "host_guard_textfile.sh"
# Live dictation (2026-09-29): its own rule file, promtool file and scrape job;
# its panels are a row of the dashboard above and its metrics are in the same
# contract.
VOICE_RULES = MONITORING / "prometheus" / "rules" / "voice-stream.yml"
VOICE_PROMTOOL_TESTS = MONITORING / "prometheus" / "tests" / "voice_stream.yml"
PROMETHEUS_CONFIG = MONITORING / "prometheus" / "prometheus.yml"
OWN_FILES = [DASHBOARD, RULES, PROMTOOL_TESTS, CONTRACT, README, WRITER, PKG / "check_metrics.py", Path(__file__),
             VOICE_RULES, VOICE_PROMTOOL_TESTS]


def _panels(doc):
    for panel in doc["panels"]:
        yield panel
        yield from panel.get("panels", [])


def _contract():
    return json.loads(CONTRACT.read_text())["metrics"]


def _statuses_for(exprs):
    contract = _contract()
    found = set()
    for expr in exprs:
        for name in check_metrics.metric_names_offline(expr):
            found.add(contract.get(check_metrics.base_name(name, contract), {}).get("status", "undeclared"))
    return found


class DashboardSchemaTests(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(DASHBOARD.read_text())

    def test_the_dashboard_has_a_stable_uid_no_other_dashboard_uses(self):
        self.assertEqual(self.doc["uid"], "dgx-developer-api")
        self.assertEqual(self.doc["title"], "Developer API")
        for other in (MONITORING / "grafana" / "dashboards").glob("*.json"):
            if other != DASHBOARD:
                self.assertNotEqual(json.loads(other.read_text()).get("uid"), self.doc["uid"], other.name)

    def test_the_dashboard_uses_the_same_schema_version_as_its_siblings(self):
        versions = {json.loads(p.read_text()).get("schemaVersion") for p in (MONITORING / "grafana" / "dashboards").glob("*.json")}
        self.assertEqual(versions, {self.doc["schemaVersion"]})

    def test_every_panel_has_a_unique_id_a_title_and_a_known_type(self):
        ids = [p["id"] for p in _panels(self.doc)]
        self.assertEqual(len(ids), len(set(ids)), "duplicate panel id")
        for panel in _panels(self.doc):
            self.assertTrue(panel.get("title"), panel["id"])
            self.assertIn(panel["type"], {"row", "text", "stat", "timeseries", "gauge", "bargauge", "state-timeline", "table"})

    def test_panels_fit_the_24_column_grid_and_never_overlap(self):
        cells = {}
        for panel in _panels(self.doc):
            g = panel["gridPos"]
            self.assertGreaterEqual(g["x"], 0)
            self.assertLessEqual(g["x"] + g["w"], 24, panel["title"])
            for dx in range(g["w"]):
                for dy in range(g["h"]):
                    key = (g["x"] + dx, g["y"] + dy)
                    self.assertNotIn(key, cells, f"{panel['title']!r} overlaps {cells.get(key)!r}")
                    cells[key] = panel["title"]

    def test_every_query_panel_and_target_uses_the_provisioned_datasource(self):
        for panel in _panels(self.doc):
            if panel["type"] in ("row", "text"):
                continue
            self.assertEqual(panel["datasource"], {"type": "prometheus", "uid": "dgx-prometheus"}, panel["title"])
            refs = [t["refId"] for t in panel["targets"]]
            self.assertEqual(len(refs), len(set(refs)), panel["title"])
            for target in panel["targets"]:
                self.assertEqual(target["datasource"]["uid"], "dgx-prometheus", panel["title"])
                self.assertTrue(target["expr"].strip(), panel["title"])

    def test_a_stat_title_fits_its_width_so_the_status_marker_is_never_cut_off(self):
        # Grafana 11.5 truncates a title near six characters per grid column
        # at 1600 px; "503 at capacity, 1h [proposed]" rendered as "…[pro…".
        for panel in _panels(self.doc):
            if panel["type"] == "stat":
                self.assertLessEqual(len(panel["title"]), 6 * panel["gridPos"]["w"], panel["title"])

    def test_rows_are_expanded_so_every_panel_is_top_level(self):
        # The launcher's provisioning test walks doc["panels"] only; a panel
        # hidden inside a collapsed row would escape its datasource check.
        for panel in self.doc["panels"]:
            if panel["type"] == "row":
                self.assertFalse(panel["collapsed"], panel["title"])
                self.assertEqual(panel["panels"], [], panel["title"])


class MetricStatusTests(unittest.TestCase):
    def setUp(self):
        self.doc = json.loads(DASHBOARD.read_text())
        self.contract = _contract()

    def test_every_metric_the_dashboard_queries_is_declared_in_the_contract(self):
        for panel in _panels(self.doc):
            for target in panel.get("targets", []):
                for name in check_metrics.metric_names_offline(target["expr"]):
                    self.assertIn(check_metrics.base_name(name, self.contract), self.contract, f"{panel['title']}: {name}")

    @unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
    def test_every_metric_the_rules_query_is_declared_in_the_contract(self):
        for where, expr in check_metrics.rule_exprs(RULES):
            for name in check_metrics.metric_names_offline(expr):
                self.assertIn(check_metrics.base_name(name, self.contract), self.contract, f"{where}: {name}")

    def test_a_panel_on_a_proposed_or_pending_metric_says_so_and_a_live_panel_does_not(self):
        for panel in _panels(self.doc):
            if not panel.get("targets"):
                continue
            statuses = _statuses_for(t["expr"] for t in panel["targets"])
            title, desc = panel["title"], panel.get("description", "")
            if "proposed" in statuses:
                self.assertTrue(title.endswith("[proposed]"), title)
                self.assertTrue(desc.startswith("PROPOSED"), title)
            elif "pending" in statuses:
                self.assertTrue(title.endswith("[pending]"), title)
                self.assertTrue(desc.startswith("PENDING"), title)
            else:
                self.assertNotRegex(title, r"\[(pending|proposed)\]")
            no_value = panel["fieldConfig"]["defaults"].get("noValue", "")
            if statuses & {"pending", "proposed"}:
                self.assertIn("not emitted yet", no_value, title)

    def test_a_panel_on_metrics_created_on_first_use_says_what_empty_means(self):
        # 2026-09-13 review: the admission gauges were marked live and shown as
        # "no data" while the orchestrator simply had not used a lane yet.
        for panel in _panels(self.doc):
            if not panel.get("targets"):
                continue
            if _statuses_for(t["expr"] for t in panel["targets"]) == {"event_driven"}:
                no_value = panel["fieldConfig"]["defaults"].get("noValue", "")
                self.assertNotIn(no_value, ("", "no data"), panel["title"])
                self.assertNotIn("not emitted yet", no_value, panel["title"])

    def test_every_status_in_the_contract_is_one_of_the_four_it_defines(self):
        doc = json.loads(CONTRACT.read_text())
        for name, entry in doc["metrics"].items():
            self.assertIn(entry["status"], doc["statuses"], name)

    def test_every_proposed_metric_names_its_type_and_a_closed_vocabulary_for_each_label(self):
        for name, entry in self.contract.items():
            if entry["status"] != "proposed":
                continue
            self.assertIn(entry["type"], {"counter", "gauge", "histogram"}, name)
            self.assertTrue(entry.get("labels"), name)
            for label, values in entry["labels"].items():
                if isinstance(values, str):
                    # "as <metric>": the same vocabulary as a label listed there.
                    self.assertIn(values[3:], self.contract, f"{name}.{label}")
                    continue
                self.assertTrue(values, f"{name}.{label}")
                self.assertEqual(len(values), len(set(values)), f"{name}.{label}")
                if label in ("route", "model", "error") or (name, label) == ("public_api_requests_total", "status"):
                    # values arriving from callers fold to "other" (metrics._clean)
                    self.assertIn("other", values, f"{name}.{label}")
            if entry["type"] == "histogram":
                self.assertEqual(entry["buckets"], sorted(entry["buckets"]), name)
            if entry["type"] == "counter":
                self.assertTrue(name.endswith("_total"), name)

    def test_the_offline_extractor_skips_functions_keywords_labels_and_ranges(self):
        expr = ('100 * sum by (service) (increase(http_requests_total{job=~"a|b", status="5xx"}[10m])) '
                '/ sum without (le) (rate(vllm:x_bucket{le="1"}[$__rate_interval])) and on(node) foo offset 5m')
        self.assertEqual(check_metrics.metric_names_offline(expr), {"http_requests_total", "vllm:x_bucket", "foo"})


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class AlertRuleTests(unittest.TestCase):
    def setUp(self):
        import yaml

        self.doc = yaml.safe_load(RULES.read_text())
        self.alerts = [r for g in self.doc["groups"] for r in g["rules"] if "alert" in r]

    def test_every_alert_carries_severity_summary_description_and_runbook(self):
        self.assertGreaterEqual(len(self.alerts), 15)
        for rule in self.alerts:
            self.assertIn(rule["labels"]["severity"], {"info", "warning", "critical"}, rule["alert"])
            for key in ("summary", "description", "runbook"):
                self.assertTrue(rule["annotations"].get(key), f"{rule['alert']} lacks {key}")

    def test_no_alert_name_or_group_name_collides_with_the_existing_rule_files(self):
        import yaml

        mine = {r["alert"] for r in self.alerts}
        my_groups = {g["name"] for g in self.doc["groups"]}
        for other in (MONITORING / "prometheus" / "rules").glob("*.yml"):
            if other == RULES:
                continue
            doc = yaml.safe_load(other.read_text())
            names = {r.get("alert") for g in doc["groups"] for r in g["rules"]}
            self.assertFalse(mine & names, other.name)
            self.assertFalse(my_groups & {g["name"] for g in doc["groups"]}, other.name)

    def test_no_rule_turns_a_missing_series_into_a_value(self):
        # alerts.yml's construction rules: absence is never zero.
        for group in self.doc["groups"]:
            for rule in group["rules"]:
                self.assertNotRegex(rule["expr"], r"or\s+vector\s*\(", rule.get("alert"))
                self.assertNotRegex(rule["expr"], r"\babsent(_over_time)?\s*\(", rule.get("alert"))

    def test_the_engine_5xx_ratio_leaves_the_same_probe_handlers_out_of_every_side(self):
        # A filter on the numerator but not the denominator (or the floor)
        # would dilute the ratio again with /v1/models polls.
        rule = next(r for r in self.alerts if r["alert"] == "EngineHttp5xxRatioHigh")
        selectors = re.findall(r"http_requests_total\{([^}]*)\}", rule["expr"])
        self.assertEqual(len(selectors), 3)
        filters = {re.search(r'handler!~"([^"]*)"', sel).group(1) for sel in selectors}
        self.assertEqual(len(filters), 1, filters)
        excluded = next(iter(filters)).split("|")
        for handler in ("/v1/models", "/health", "/metrics", "/tokenize"):
            self.assertIn(handler, excluded)

    def test_every_alert_has_a_promtool_case(self):
        tests = PROMTOOL_TESTS.read_text()
        for rule in self.alerts:
            self.assertRegex(tests, rf"alertname: {rule['alert']}\b", rule["alert"])

    def test_every_runbook_anchor_in_this_directory_exists_in_the_readme(self):
        anchors = set()
        for line in README.read_text().splitlines():
            if line.startswith("#"):
                heading = line.lstrip("#").strip().lower()
                anchors.add(re.sub(r"[^\w\- ]", "", heading).replace(" ", "-"))
        for rule in self.alerts:
            runbook = rule["annotations"]["runbook"]
            if runbook.startswith("monitoring/developer-api/README.md#"):
                self.assertIn(runbook.split("#", 1)[1], anchors, rule["alert"])


class ReadmeHonestyTests(unittest.TestCase):
    def test_the_readme_says_no_alert_reaches_a_person_until_a_receiver_exists(self):
        # 2026-09-13 review: Prometheus has no Alertmanager, so an alert text
        # that promises an early warning must not be read as one.
        text = README.read_text()
        self.assertIn("no Alertmanager", text)
        self.assertIn("receiver", text)

    def test_the_readme_first_memory_action_can_see_gpu_memory(self):
        section = README.read_text().split("### Node memory", 1)[1].split("\n### ", 1)[0]
        self.assertIn("nvidia-smi --query-compute-apps", section)
        self.assertNotIn("docker stats --no-stream | sort", section)


class PublicRepositoryHygieneTests(unittest.TestCase):
    def test_no_file_of_this_deliverable_carries_a_private_address(self):
        private = re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b")
        for path in OWN_FILES:
            if path.exists():
                self.assertIsNone(private.search(path.read_text()), path.name)


class HostGuardWriterTests(unittest.TestCase):
    """The writer is run with a fake `nft` on PATH, never the real one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "textfile"

    def tearDown(self):
        self.tmp.cleanup()

    def _fake_nft(self, exit_code, stderr="", stdout=""):
        fake = self.dir / "nft"
        fake.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' {json.dumps(stdout)}\n"
            f"printf '%s\\n' {json.dumps(stderr)} >&2\n"
            f"exit {exit_code}\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        return fake

    def _run(self, **env):
        base = {"PATH": "/usr/bin:/bin", "TEXTFILE_DIR": str(self.out)}
        proc = subprocess.run(["sh", str(WRITER)], env={**base, **env}, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = (self.out / "techsara_host_guard.prom").read_text()
        values = dict(re.findall(r"^(techsara_host_guard_\w+(?:\{[^}]*\})?) (\S+)$", text, re.M))
        return text, values

    def test_a_listed_table_is_present_and_the_check_ok(self):
        _, v = self._run(HOST_GUARD_SOURCE="nft", HOST_GUARD_NFT=str(self._fake_nft(0)))
        self.assertEqual(v['techsara_host_guard_table_present{source="nft"}'], "1")
        self.assertEqual(v['techsara_host_guard_check_ok{source="nft"}'], "1")

    def test_a_table_nft_cannot_find_is_missing_with_the_check_ok(self):
        fake = self._fake_nft(1, "Error: No such file or directory\nlist table inet techsara_guard")
        _, v = self._run(HOST_GUARD_SOURCE="nft", HOST_GUARD_NFT=str(fake))
        self.assertEqual(v['techsara_host_guard_table_present{source="nft"}'], "0")
        self.assertEqual(v['techsara_host_guard_check_ok{source="nft"}'], "1")

    def test_nft_without_permission_is_a_failed_check_not_a_missing_table(self):
        fake = self._fake_nft(1, "Operation not permitted (you must be root)")
        _, v = self._run(HOST_GUARD_SOURCE="nft", HOST_GUARD_NFT=str(fake))
        self.assertEqual(v['techsara_host_guard_check_ok{source="nft"}'], "0")

    def test_the_apply_scripts_state_file_counts_as_present_when_not_root(self):
        state = self.dir / "state"
        state.write_text("GUARD_APPLIED_AT=2026-09-13T07:09:00Z\n")
        _, v = self._run(HOST_GUARD_SOURCE="state_file", HOST_GUARD_STATE_FILE=str(state))
        self.assertEqual(v['techsara_host_guard_table_present{source="state_file"}'], "1")
        self.assertEqual(v['techsara_host_guard_check_ok{source="state_file"}'], "1")

    def test_no_state_file_after_a_reboot_is_missing(self):
        _, v = self._run(HOST_GUARD_SOURCE="state_file", HOST_GUARD_STATE_FILE=str(self.dir / "absent" / "state"))
        self.assertEqual(v['techsara_host_guard_table_present{source="state_file"}'], "0")
        self.assertEqual(v['techsara_host_guard_check_ok{source="state_file"}'], "1")

    def test_an_empty_state_directory_that_can_be_searched_is_missing(self):
        (self.dir / "run").mkdir()
        _, v = self._run(HOST_GUARD_SOURCE="state_file", HOST_GUARD_STATE_FILE=str(self.dir / "run" / "state"))
        self.assertEqual(v['techsara_host_guard_table_present{source="state_file"}'], "0")
        self.assertEqual(v['techsara_host_guard_check_ok{source="state_file"}'], "1")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root can search a mode-000 directory")
    def test_a_state_directory_it_cannot_search_is_an_unknown_not_a_missing_table(self):
        # 2026-09-13 review: `|| [ -r /run ]` was always true, so an applied
        # guard behind an unsearchable directory reported MISSING (critical).
        locked = self.dir / "locked"
        locked.mkdir()
        (locked / "state").write_text("GUARD_APPLIED_AT=2026-09-13T07:09:00Z\n")
        locked.chmod(0)
        try:
            _, v = self._run(HOST_GUARD_SOURCE="state_file", HOST_GUARD_STATE_FILE=str(locked / "state"))
        finally:
            locked.chmod(0o755)
        self.assertEqual(v['techsara_host_guard_check_ok{source="state_file"}'], "0")

    def test_the_degraded_fallback_is_reported_as_a_missing_table_under_nft(self):
        # The boot unit loads the fallback under the same table name when the
        # full apply fails. It closes the office LAN and the tailnet and
        # nothing else, so "the guard is in place" would be a lie.
        degraded = 'table inet techsara_guard {\n  chain input {\n    counter packets 0 bytes 0 accept comment "DEGRADED fallback: no catch-all drop, only the office LAN and the tailnet are closed"\n  }\n}'
        _, v = self._run(HOST_GUARD_SOURCE="nft", HOST_GUARD_NFT=str(self._fake_nft(0, stdout=degraded)))
        self.assertEqual(v['techsara_host_guard_table_present{source="nft"}'], "0")
        self.assertEqual(v['techsara_host_guard_check_ok{source="nft"}'], "1")

    def test_the_degraded_fallback_is_reported_as_a_missing_table_under_the_state_file(self):
        state = self.dir / "state"
        state.write_text("GUARD_ROLE=head\nGUARD_MODE=degraded\nGUARD_APPLIED_AT=2026-09-22T04:10:00Z\n")
        _, v = self._run(HOST_GUARD_SOURCE="state_file", HOST_GUARD_STATE_FILE=str(state))
        self.assertEqual(v['techsara_host_guard_table_present{source="state_file"}'], "0")
        self.assertEqual(v['techsara_host_guard_check_ok{source="state_file"}'], "1")

    def test_a_full_apply_recorded_in_the_state_file_is_present(self):
        state = self.dir / "state"
        state.write_text("GUARD_ROLE=head\nGUARD_MODE=full\nGUARD_APPLIED_AT=2026-09-22T04:10:00Z\n")
        _, v = self._run(HOST_GUARD_SOURCE="state_file", HOST_GUARD_STATE_FILE=str(state))
        self.assertEqual(v['techsara_host_guard_table_present{source="state_file"}'], "1")
        self.assertEqual(v['techsara_host_guard_check_ok{source="state_file"}'], "1")

    def test_the_file_is_valid_exposition_with_help_and_type_and_no_temp_file_is_left(self):
        text, v = self._run(HOST_GUARD_SOURCE="nft", HOST_GUARD_NFT=str(self._fake_nft(0)))
        for metric in ("techsara_host_guard_table_present", "techsara_host_guard_check_ok", "techsara_host_guard_check_timestamp_seconds"):
            self.assertIn(f"# TYPE {metric} gauge", text)
        self.assertGreater(int(v["techsara_host_guard_check_timestamp_seconds"]), 1_700_000_000)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["techsara_host_guard.prom"])

    def test_the_writer_never_changes_the_ruleset(self):
        body = "\n".join(l for l in WRITER.read_text().splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(body, r"\b(add|delete|flush|insert|replace|create|destroy)\b\s+(table|rule|chain|ruleset)")
        self.assertRegex(body, r'"\$NFT" list table inet')


# ------------------------------------------------------------ live dictation
#: The alerts live dictation shipped with (2026-09-29). A rename also changes
#: the README runbook and the promtool file, so the set is pinned here.
VOICE_ALERTS = {
    "VoiceStreamFirstPartialSlow",
    "VoiceStreamFinalSlow",
    "VoiceStreamEngineFallingBehind",
    "VoiceStreamErrorRatioHigh",
    "VoiceStreamDisconnectsAbnormal",
    "VoiceStreamEngineDown",
}
VOICE_ROW_TITLE = "Real-time speech to text"
#: The scrape job each family comes from (prometheus.yml): the gateway runs in
#: the orchestrator, the engine on the worker.
VOICE_JOBS = {"voice_stream_": "orchestrator", "stt_stream_": "stt-stream"}


def _readme_anchors():
    anchors = set()
    for line in README.read_text().splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            anchors.add(re.sub(r"[^\w\- ]", "", heading).replace(" ", "-"))
    return anchors


def _voice_selectors(expr):
    """(metric, "{matchers}" or "") for every live dictation metric an
    expression reads directly. A recorded name (level:metric:operation) is not
    one: the lookbehind skips the metric part after its colon."""
    return re.findall(r"(?<![\w:])((?:voice_stream|stt_stream)_\w+)(\{[^}]*\})?", expr)


def _expected_job(metric):
    return next(job for prefix, job in VOICE_JOBS.items() if metric.startswith(prefix))


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class VoiceStreamRuleTests(unittest.TestCase):
    """monitoring/prometheus/rules/voice-stream.yml under developer-api.yml's
    house rules, plus the ways a rule on a brand-new job goes silently inert."""

    def setUp(self):
        import yaml

        self.doc = yaml.safe_load(VOICE_RULES.read_text())
        rules = [r for g in self.doc["groups"] for r in g["rules"]]
        self.alerts = [r for r in rules if "alert" in r]
        self.records = [r for r in rules if "record" in r]

    def test_the_six_live_dictation_alerts_exist_once_each(self):
        self.assertEqual(sorted(r["alert"] for r in self.alerts), sorted(VOICE_ALERTS))

    def test_every_alert_carries_severity_summary_description_and_a_readme_runbook(self):
        anchors = _readme_anchors()
        for rule in self.alerts:
            self.assertIn(rule["labels"]["severity"], {"info", "warning", "critical"}, rule["alert"])
            for key in ("summary", "description", "runbook"):
                self.assertTrue(rule["annotations"].get(key), f"{rule['alert']} lacks {key}")
            page, _, anchor = rule["annotations"]["runbook"].partition("#")
            self.assertEqual(page, "monitoring/developer-api/README.md", rule["alert"])
            self.assertIn(anchor, anchors, rule["alert"])

    def test_no_rule_turns_a_missing_series_into_a_value(self):
        for group in self.doc["groups"]:
            for rule in group["rules"]:
                name = rule.get("alert") or rule.get("record")
                self.assertNotRegex(rule["expr"], r"or\s+vector\s*\(", name)
                self.assertNotRegex(rule["expr"], r"\babsent(_over_time)?\s*\(", name)

    def test_no_alert_group_or_recorded_name_collides_with_another_rule_file(self):
        import yaml

        mine = {r.get("alert") or r.get("record") for r in self.alerts + self.records}
        my_groups = {g["name"] for g in self.doc["groups"]}
        for other in (MONITORING / "prometheus" / "rules").glob("*.yml"):
            if other == VOICE_RULES:
                continue
            doc = yaml.safe_load(other.read_text())
            names = {r.get("alert") or r.get("record") for g in doc["groups"] for r in g["rules"]}
            self.assertFalse(mine & names, other.name)
            self.assertFalse(my_groups & {g["name"] for g in doc["groups"]}, other.name)

    def test_every_alert_and_recording_rule_has_a_promtool_case(self):
        tests = VOICE_PROMTOOL_TESTS.read_text()
        for rule in self.alerts:
            self.assertRegex(tests, rf"alertname: {rule['alert']}\b", rule["alert"])
        for rule in self.records:
            self.assertRegex(tests, rf"expr: .*{re.escape(rule['record'])}\b", rule["record"])

    def test_recording_rules_are_named_level_metric_operation(self):
        for rule in self.records:
            self.assertRegex(rule["record"], r"^[a-z]+:[a-z0-9_]+:[a-z0-9_]+$")

    def test_every_metric_the_rules_read_is_in_the_contract_or_recorded_here(self):
        # A misspelt metric name leaves a rule inert with no error anywhere.
        # `up` is Prometheus's own series for every target.
        contract = _contract()
        recorded = {r["record"] for r in self.records}
        for where, expr in check_metrics.rule_exprs(VOICE_RULES):
            for name in check_metrics.metric_names_offline(expr):
                if name == "up" or name in recorded:
                    continue
                self.assertIn(check_metrics.base_name(name, contract), contract, f"{where}: {name}")

    def test_every_selector_names_the_job_that_scrapes_it(self):
        # A job label no scrape config produces is the other silent way to go
        # inert: files-and-gateway.yml reads job="v1-gateway", which no job
        # scrapes (observability audit, 2026-09-29).
        import yaml

        jobs = {j["job_name"] for j in yaml.safe_load(PROMETHEUS_CONFIG.read_text())["scrape_configs"]}
        for where, expr in check_metrics.rule_exprs(VOICE_RULES):
            for job in re.findall(r'\bjob="([^"]+)"', expr):
                self.assertIn(job, jobs, f"{where}: no scrape job {job!r}")
            for metric, matchers in _voice_selectors(expr):
                self.assertIn(f'job="{_expected_job(metric)}"', matchers, f"{where}: {metric}")


@unittest.skipUnless(HAVE_YAML, "PyYAML not installed")
class VoiceStreamScrapeJobTests(unittest.TestCase):
    """The streaming engine's /metrics is scraped the way the worker's
    exporters are: the management address, never a RoCE rail, with the
    node/role/service labels its dashboards and joins key on."""

    def setUp(self):
        import yaml

        self.jobs = {j["job_name"]: j for j in yaml.safe_load(PROMETHEUS_CONFIG.read_text())["scrape_configs"]}

    def _worker_management_host(self):
        # The host the worker's node-exporter is scraped on. Read from the
        # file rather than spelled here, so this test carries no address.
        for config in self.jobs["node"]["static_configs"]:
            if config["labels"].get("role") == "worker":
                return config["targets"][0].rsplit(":", 1)[0]
        self.fail("job node has no worker target")

    def test_the_engine_is_scraped_on_the_workers_management_address_at_its_port(self):
        [config] = self.jobs["stt-stream"]["static_configs"]
        self.assertEqual(config["targets"], [f"{self._worker_management_host()}:30009"])

    def test_the_engine_target_carries_node_role_and_service_like_its_neighbours(self):
        [config] = self.jobs["stt-stream"]["static_configs"]
        self.assertEqual(config["labels"], {"node": "spark-2", "role": "worker", "service": "stt-stream"})


class VoiceStreamDashboardTests(unittest.TestCase):
    """The 'Real-time speech to text' row of the Developer API dashboard."""

    def setUp(self):
        self.doc = json.loads(DASHBOARD.read_text())

    def _row(self):
        panels = self.doc["panels"]
        start = next(i for i, p in enumerate(panels) if p["type"] == "row" and p["title"] == VOICE_ROW_TITLE)
        row = []
        for panel in panels[start + 1:]:
            if panel["type"] == "row":
                break
            row.append(panel)
        return row

    def _exprs(self):
        return [t["expr"] for p in self._row() for t in p.get("targets", [])]

    def test_the_row_shows_streams_latency_rtf_errors_disconnects_and_refusals(self):
        exprs = " ".join(self._exprs())
        for name in (
            "voice_stream_sessions_active",
            "voice_stream_audio_received_seconds_total",
            "voice_stream_first_partial_seconds_bucket",
            "voice_stream_final_seconds_bucket",
            "voice_stream_event_lag_seconds_bucket",
            "stt_stream_compute_seconds_total",
            "stt_stream_audio_seconds_total",
            "voice_stream_sessions_total",
            "voice_stream_errors_total",
            "voice_stream_rejections_total",
            "stt_stream_rejections_total",
        ):
            self.assertIn(name, exprs)

    def test_latency_is_shown_at_p50_and_p95(self):
        exprs = self._exprs()
        for histogram in ("voice_stream_first_partial_seconds", "voice_stream_final_seconds", "voice_stream_event_lag_seconds"):
            for quantile in ("0.5", "0.95"):
                self.assertTrue(
                    any(f"histogram_quantile({quantile}," in e and f"{histogram}_bucket" in e for e in exprs),
                    f"{histogram} p{quantile}",
                )

    def test_every_row_selector_names_the_job_that_scrapes_it(self):
        for panel in self._row():
            for target in panel.get("targets", []):
                selectors = _voice_selectors(target["expr"])
                self.assertTrue(selectors, panel["title"])
                for metric, matchers in selectors:
                    self.assertIn(f'job="{_expected_job(metric)}"', matchers, f"{panel['title']}: {metric}")

    def test_every_live_dictation_metric_is_pending_until_it_ships(self):
        # Nothing emits them in production on 2026-09-29. `live` would make
        # check_metrics.py FAIL on the missing series; `proposed` would deny
        # that the gateway and engine code is written.
        names = [n for n in _contract() if n.startswith(("voice_stream_", "stt_stream_"))]
        self.assertTrue(names)
        for name in names:
            self.assertEqual(_contract()[name]["status"], "pending", name)


if __name__ == "__main__":
    unittest.main()
