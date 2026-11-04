"""The generated dashboards: that they stay generated, and stay true to their rules."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import unittest

try:
    from .support import REPO_ROOT
except ImportError:  # `unittest discover -s launcher/tests`
    from support import REPO_ROOT

GRAFANA = REPO_ROOT / "monitoring" / "grafana"
DASHBOARDS = GRAFANA / "dashboards"
RULES = REPO_ROOT / "monitoring" / "prometheus" / "rules"
PROBE_DIR = REPO_ROOT / "monitoring" / "exporters" / "spark-probe"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


generator = _load(GRAFANA / "generate_dashboards.py", "generate_dashboards")
GENERATED = {name: json.loads((DASHBOARDS / name).read_text(encoding="utf-8")) for name in generator.BUILDERS}


def panels(doc):
    for panel in doc["panels"]:
        yield panel
        yield from panel.get("panels", [])


def exprs(doc):
    for panel in panels(doc):
        for target in panel.get("targets", []):
            yield panel, target["expr"]


class GeneratedFilesTests(unittest.TestCase):
    def test_the_files_on_disk_are_what_the_generator_writes(self):
        proc = subprocess.run([sys.executable, str(GRAFANA / "generate_dashboards.py"), "--check"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_the_home_dashboard_file_still_exists(self):
        # GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH points at it by name.
        compose = (REPO_ROOT / "compose" / "compose.monitoring.yaml").read_text()
        home = re.search(r"DEFAULT_HOME_DASHBOARD_PATH: /var/lib/grafana/dashboards/(\S+)", compose).group(1)
        self.assertIn(home, generator.BUILDERS)

    def test_bookmarked_addresses_keep_working(self):
        for name, uid in (("dgx-cluster-overview.json", "dgx-cluster-overview"), ("dgx-gpu-thermal-power.json", "dgx-gpu-thermal-power"),
                          ("dgx-interconnect.json", "dgx-interconnect"), ("dgx-host-containers.json", "dgx-host-containers")):
            self.assertEqual(GENERATED[name]["uid"], uid)

    def test_every_page_has_the_same_navigation_and_it_leads_somewhere(self):
        uids = {json.loads(p.read_text(encoding="utf-8"))["uid"] for p in DASHBOARDS.glob("*.json")}
        for _title, uid in generator.NAV:
            self.assertIn(uid, uids)
        for path in DASHBOARDS.glob("*.json"):
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["links"], generator.nav_links(), path.name)


class AnyNumberOfSparksTests(unittest.TestCase):
    def test_no_panel_names_a_spark(self):
        # The old overview said spark-1 and spark-2 37 times.
        for name, doc in GENERATED.items():
            for panel, expr in exprs(doc):
                self.assertNotRegex(expr, r"spark-\d", f"{name}: {panel.get('title')}")
            for panel in panels(doc):
                self.assertNotRegex(panel.get("title") or "", r"spark-\d", name)
                self.assertNotRegex(panel.get("description") or "", r"spark-\d", name)

    def test_the_spark_list_comes_from_a_query(self):
        for name, doc in GENERATED.items():
            for variable in doc["templating"]["list"]:
                if variable["name"] == "node":
                    self.assertEqual(variable["type"], "query", name)
                    self.assertEqual(variable["refresh"], 2, name)  # on every time-range change
                    self.assertIn('up{job="node"}', variable["definition"], name)
                    self.assertFalse(variable.get("options"), name)

    def test_the_home_page_repeats_a_strip_per_spark(self):
        rows = [p for p in GENERATED["dgx-cluster-overview.json"]["panels"] if p.get("repeat") == "node"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], "row")

    def test_a_spark_keeps_its_colour_on_every_chart(self):
        overrides = generator.node_colors()
        self.assertEqual(overrides[0]["properties"][0]["value"]["fixedColor"], generator.SERIES[0])
        self.assertEqual(overrides[0]["matcher"]["options"], "/^spark-1( |$)/")
        # spark-1 must not also match spark-10 and up.
        self.assertIsNone(re.match(r"^spark-1( |$)", "spark-10"))
        self.assertIsNotNone(re.match(r"^spark-1( |$)", "spark-1 rx"))


class LayoutTests(unittest.TestCase):
    def test_nothing_overlaps_or_runs_off_the_page(self):
        for name, doc in GENERATED.items():
            taken = set()
            for panel in doc["panels"]:
                grid = panel["gridPos"]
                self.assertLessEqual(grid["x"] + grid["w"], 24, f"{name}: {panel.get('title')}")
                for x in range(grid["x"], grid["x"] + grid["w"]):
                    for y in range(grid["y"], grid["y"] + grid["h"]):
                        self.assertNotIn((x, y), taken, f"{name}: {panel.get('title')}")
                        taken.add((x, y))

    def test_panel_ids_are_unique(self):
        for name, doc in GENERATED.items():
            ids = [panel["id"] for panel in doc["panels"]]
            self.assertEqual(len(ids), len(set(ids)), name)

    def test_titles_fit_their_panel(self):
        # About 8.5 px a character at Grafana's title size, and a grid column is
        # about 72 px wide on a 1920 px screen with the menu open. A description
        # adds an info icon. Truncated titles were a complaint about the old page.
        for name, doc in GENERATED.items():
            for panel in panels(doc):
                if panel["type"] == "row" or not panel.get("title"):
                    continue
                room = panel["gridPos"]["w"] * 72 - 24 - (30 if panel.get("description") else 0)
                self.assertLessEqual(len(panel["title"]) * 8.5, room, f"{name}: {panel['title']}")


class TheRulesThePagesFollowTests(unittest.TestCase):
    def test_gaps_stay_gaps(self):
        # A bridged line would have hidden the whole 2026-09-11 outage.
        for name, doc in GENERATED.items():
            for panel in panels(doc):
                if panel["type"] == "timeseries":
                    self.assertFalse(panel["fieldConfig"]["defaults"]["custom"]["spanNulls"], f"{name}: {panel['title']}")

    def test_every_panel_targets_the_provisioned_datasource(self):
        for name, doc in GENERATED.items():
            for panel in panels(doc):
                for target in panel.get("targets", []):
                    self.assertEqual(target["datasource"]["uid"], "dgx-prometheus", f"{name}: {panel.get('title')}")

    def test_gpu_busy_is_never_coloured_as_if_it_were_load(self):
        # The worker rank reads 95 % while it waits. Amber there is a false alarm.
        for name, doc in GENERATED.items():
            for panel in panels(doc):
                if panel.get("title") == "GPU busy" and panel["type"] == "stat":
                    self.assertEqual(len(panel["fieldConfig"]["defaults"]["thresholds"]["steps"]), 1, name)

    def test_state_timelines_with_value_mappings_are_not_in_threshold_colour_mode(self):
        # In that mode Grafana merges by threshold and draws a grey bar reading "-∞+".
        for name, doc in GENERATED.items():
            for panel in panels(doc):
                if panel["type"] == "state-timeline" and panel["fieldConfig"]["defaults"].get("mappings"):
                    self.assertNotIn("color", panel["fieldConfig"]["defaults"], f"{name}: {panel['title']}")

    def test_the_engine_tile_says_what_the_availability_contract_says(self):
        doc = GENERATED["dgx-cluster-overview.json"]
        engine = next(p for p in panels(doc) if p.get("title") == "Engine")
        self.assertEqual(engine["targets"][0]["expr"], "cluster:vllm_service_state:code")
        self.assertEqual(engine["fieldConfig"]["defaults"]["noValue"], "MONITORING_UNKNOWN")
        options = engine["fieldConfig"]["defaults"]["mappings"][0]["options"]
        names = [options[str(code)]["text"] for code in range(9)]
        self.assertEqual(names, ["MONITORING_UNKNOWN", "STARTING", "READY", "BUSY", "DEGRADED", "WEDGED", "RECOVERING", "QUEUEING", "DOWN"])

    def test_the_last_failure_tile_is_not_painted_as_a_current_problem(self):
        # It keeps its value after recovery; the old orange tile read as an outage.
        doc = GENERATED["dgx-cluster-overview.json"]
        tile = next(p for p in panels(doc) if p.get("title") == "Last failure")
        steps = tile["fieldConfig"]["defaults"]["thresholds"]["steps"]
        self.assertEqual([s["color"] for s in steps], [generator.MUTED])


class EveryMetricExistsTests(unittest.TestCase):
    """A typo in a query is a panel that says No data for ever."""

    @classmethod
    def setUpClass(cls):
        cls.probe_src = (PROBE_DIR / "spark_probe.py").read_text() + (PROBE_DIR / "spark_local.py").read_text()
        cls.gpu_src = (REPO_ROOT / "monitoring" / "exporters" / "dgx-gpu" / "dgx_gpu_exporter.py").read_text()
        local = _load(PROBE_DIR / "spark_local.py", "spark_local_for_dashboards")
        cls.nvme = {f"spark_nvme_{key}" for key in local.HELP} | {"spark_nvme_read_success", "spark_nvme_last_read_timestamp_seconds"}
        cls.recorded = set()
        for path in RULES.glob("*.yml"):
            cls.recorded |= set(re.findall(r"- record: (\S+)", path.read_text()))

    def all_text(self):
        for doc in GENERATED.values():
            for _panel, expr in exprs(doc):
                yield expr
        yield (RULES / "fleet.yml").read_text()

    def test_spark_metrics_are_published_by_the_probe(self):
        for text in self.all_text():
            for name in set(re.findall(r"\bspark_[a-z0-9_]+\b", text)):
                if name.startswith("spark_nvme_"):
                    self.assertIn(name, self.nvme, name)
                else:
                    self.assertIn(name, self.probe_src, name)

    def test_gpu_metrics_are_published_by_the_gpu_exporter(self):
        for text in self.all_text():
            for name in set(re.findall(r"\bdgx_gpu_[a-z0-9_]+\b", text)):
                self.assertIn(name, self.gpu_src, name)

    def test_recorded_series_are_defined_by_a_rule(self):
        for text in self.all_text():
            for name in set(re.findall(r"\b(?:fleet|cluster|node):[a-z0-9_:]+\b", text)):
                self.assertIn(name, self.recorded, name)

    def test_no_panel_still_reads_cadvisor(self):
        for path in DASHBOARDS.glob("*.json"):
            self.assertNotIn('job=\\"cadvisor\\"', path.read_text(encoding="utf-8"), path.name)
            self.assertNotIn("container_start_time_seconds", path.read_text(encoding="utf-8"), path.name)
        for path in RULES.glob("*.yml"):
            uncommented = "\n".join(line for line in path.read_text().splitlines() if not line.strip().startswith("#"))
            self.assertNotIn("container_start_time_seconds", uncommented, path.name)


class FleetRuleTests(unittest.TestCase):
    def setUp(self):
        self.rules = (RULES / "fleet.yml").read_text()
        self.tests = (REPO_ROOT / "monitoring" / "prometheus" / "tests" / "fleet.yml").read_text()

    def test_every_alert_has_a_promtool_case_a_summary_and_a_severity(self):
        names = re.findall(r"- alert: (\w+)", self.rules)
        self.assertGreater(len(names), 40)
        self.assertEqual(self.rules.count("summary:"), len(names))
        self.assertEqual(len(re.findall(r"severity: (?:critical|warning|info)", self.rules)), len(names))
        for name in names:
            self.assertIn(f"alertname: {name}", self.tests, f"{name} has no promtool test")

    def test_no_rule_names_a_spark(self):
        uncommented = "\n".join(line for line in self.rules.splitlines() if not line.strip().startswith("#"))
        self.assertNotRegex(uncommented, r'node="spark-\d"')


if __name__ == "__main__":
    unittest.main()
