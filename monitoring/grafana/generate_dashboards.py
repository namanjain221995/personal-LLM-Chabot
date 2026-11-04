#!/usr/bin/env python3
"""Generates the fleet dashboards in monitoring/grafana/dashboards/.

    python3 monitoring/grafana/generate_dashboards.py          write them
    python3 monitoring/grafana/generate_dashboards.py --check  fail if stale

WHY GENERATED. A dashboard with one panel per Spark has to be edited by hand when
a Spark arrives, which is how the old overview came to name spark-1 and spark-2
37 times. Everything written here is driven by the `node` label: per-node panels
repeat over a query variable, tables have one row per node, and charts have one
line per node. A new Spark needs no edit. The same code also keeps units,
thresholds, colours and layout the same on every page.

The JSON is committed: Grafana reads files, reviewers read diffs, and a test fails
if the two ever disagree. Do not edit the generated files; edit this one.

THE RULES EVERY PANEL FOLLOWS.
  * Colour means something. Green, amber and red are only used for states and
    limits that are good or bad. A number that is neither, such as GPU busy or
    tokens per second, is drawn in a neutral colour.
  * A Spark keeps its colour on every chart: spark-1 is always blue. The palette
    is validated for colour-vision deficiency against the panel surface.
  * Absent is not zero. Missing data draws a gap (spanNulls off) and a tile says
    what is missing instead of showing 0.
  * GPU busy is never presented as load. The worker rank spins at about 95 %
    while drawing about 34 W, so power sits next to it.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "dashboards"
DS = {"type": "prometheus", "uid": "dgx-prometheus"}

# Status colours. Reserved: never used to tell one series from another.
OK = "#76b900"       # NVIDIA green
BUSY = "#4e9e3a"     # also healthy, but distinguishable from READY on a timeline
WARN = "#fab219"
SERIOUS = "#ec835a"
CRIT = "#f2495c"
INFO = "#5794f2"
NEUTRAL = "#d8d9da"  # a number that is neither good nor bad
ACCENT = "#6ea6ff"   # the same, where a sparkline sits behind it: grey would drown the digits
MUTED = "#8e929c"    # unknown, not applicable
# Identity colours, in the validated order (dataviz validator, dark, on #181b1f).
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]

UP_DOWN = {"1": ("UP", OK), "0": ("DOWN", CRIT)}
ENGINE_STATES = {
    "0": ("MONITORING_UNKNOWN", MUTED), "1": ("STARTING", INFO), "2": ("READY", OK), "3": ("BUSY", BUSY),
    "4": ("DEGRADED", WARN), "5": ("WEDGED", SERIOUS), "6": ("RECOVERING", SERIOUS), "7": ("QUEUEING", SERIOUS),
    "8": ("DOWN", CRIT),
}
RATE = "$__rate_interval"

NAV = [
    ("Home", "dgx-cluster-overview"), ("Spark", "spark-node"), ("GPU", "dgx-gpu-thermal-power"),
    ("Fabric", "dgx-interconnect"), ("Hosts", "fleet-hosts"), ("Containers", "dgx-host-containers"),
    ("Databases", "fleet-databases"), ("Postgres", "dgx-data-stores"), ("Engine", "dgx-vllm-performance"),
    ("Alerts", "fleet-alerts"), ("Monitoring", "fleet-monitoring"),
]


def nav_links() -> list[dict]:
    return [
        {"title": title, "type": "link", "url": f"/d/{uid}", "icon": "dashboard", "targetBlank": False,
         "keepTime": True, "includeVars": False, "asDropdown": False, "tags": [], "tooltip": ""}
        for title, uid in NAV
    ]


# ------------------------------------------------------------------ primitives --


def mappings(table: dict[str, tuple[str, str]]) -> list[dict]:
    return [{"type": "value", "options": {
        key: {"text": text, "color": color, "index": i} for i, (key, (text, color)) in enumerate(table.items())
    }}]


def steps(base: str = NEUTRAL, *rest: tuple[float, str]) -> dict:
    return {"mode": "absolute", "steps": [{"color": base, "value": None},
                                          *({"color": c, "value": v} for v, c in rest)]}


def q(expr: str, legend: str | None = None, *, instant: bool = False, table: bool = False, interval: str | None = None) -> dict:
    target = {"datasource": DS, "editorMode": "code", "expr": expr, "range": not (instant or table),
              "instant": instant or table, "legendFormat": legend if legend is not None else "__auto"}
    if table:
        target["format"] = "table"
    if interval:
        target["interval"] = interval
    return target


def with_refs(targets: list[dict]) -> list[dict]:
    for index, target in enumerate(targets):
        target["refId"] = chr(ord("A") + index)
    return targets


def field_override(matcher_id: str, matcher: str, **properties) -> dict:
    return {"matcher": {"id": matcher_id, "options": matcher},
            "properties": [{"id": k.replace("__", "."), "value": v} for k, v in properties.items()]}


def color_override(matcher_id: str, matcher: str, color: str) -> dict:
    return {"matcher": {"id": matcher_id, "options": matcher},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}


def node_colors() -> list[dict]:
    """spark-N is the same colour everywhere, whatever else is on the chart."""
    return [color_override("byRegexp", f"/^spark-{n + 1}( |$)/", SERIES[n]) for n in range(len(SERIES))]


def panel(kind: str, title: str, targets: list[dict] | None, w: int, h: int, *, desc: str | None = None, **extra) -> dict:
    out = {"type": kind, "title": title, "datasource": DS, "_w": w, "_h": h, **extra}
    if desc:
        out["description"] = desc
    if targets is not None:
        out["targets"] = with_refs(targets)
    return out


def stat(title: str, targets: list[dict], w: int = 3, h: int = 3, *, unit: str = "none", th: dict | None = None,
         maps: list[dict] | None = None, desc: str | None = None, decimals: int | None = None,
         no_value: str | None = None, spark: bool = False, text: str = "auto", overrides: list[dict] | None = None,
         fields: str = "", size: int | None = None, title_size: int | None = None, orientation: str = "auto",
         min_: float | None = None, max_: float | None = None, links: list[dict] | None = None, **extra) -> dict:
    defaults: dict = {"unit": unit, "mappings": maps or [], "thresholds": th or steps(ACCENT if spark else NEUTRAL),
                      "color": {"mode": "thresholds"}}
    if decimals is not None:
        defaults["decimals"] = decimals
    if no_value is not None:
        defaults["noValue"] = no_value
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    if links:
        defaults["links"] = links
    options = {
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": fields, "values": False},
        "orientation": orientation, "textMode": text, "colorMode": "value",
        "graphMode": "area" if spark else "none", "justifyMode": "auto",
        "wideLayout": True, "showPercentChange": False, "percentChangeColorMode": "standard",
    }
    if size or title_size:
        options["text"] = {k: v for k, v in (("valueSize", size), ("titleSize", title_size)) if v}
    return panel("stat", title, targets, w, h, desc=desc, options=options,
                 fieldConfig={"defaults": defaults, "overrides": overrides or []}, **extra)


def ts(title: str, targets: list[dict], w: int = 8, h: int = 8, *, unit: str = "short", stack: bool = False,
       fill: int = 8, mirror: str | None = None, nodes: bool = True, min_: float | None = 0, max_: float | None = None,
       calcs: tuple[str, ...] = ("lastNotNull", "max"), desc: str | None = None, decimals: int | None = None,
       overrides: list[dict] | None = None, legend: bool = True, limits: dict | None = None, right: bool = False,
       quiet: bool = False, **extra) -> dict:
    """`quiet` is for counts that should be zero: a flat line then sits on the floor
    of a 0-1 axis, instead of Grafana inventing a 0-100 one. `right` puts the legend
    beside the chart, for charts with too many series to list underneath."""
    custom = {
        "drawStyle": "line", "lineInterpolation": "linear", "lineWidth": 1.5, "fillOpacity": 40 if stack else fill,
        "gradientMode": "none" if stack else "opacity", "showPoints": "never", "spanNulls": False,
        "stacking": {"mode": "normal" if stack else "none", "group": "A"}, "axisBorderShow": False,
        "axisCenteredZero": bool(mirror), "axisPlacement": "auto", "axisLabel": "",
        "scaleDistribution": {"type": "linear"}, "barAlignment": 0, "pointSize": 5,
        "hideFrom": {"legend": False, "tooltip": False, "viz": False},
        "thresholdsStyle": {"mode": "line" if limits else "off"},
    }
    if quiet:
        custom["axisSoftMin"] = 0
        custom["axisSoftMax"] = 1
    # A templated legend means names nobody knows yet, so the colour is derived from
    # the name: a container keeps its colour when a filter changes what else is shown.
    defaults: dict = {"unit": unit, "custom": custom, "color": {"mode": "palette-classic-by-name"},
                      "thresholds": limits or steps(), "mappings": []}
    if mirror:
        min_ = None
    if min_ is not None:
        defaults["min"] = min_
    if max_ is not None:
        defaults["max"] = max_
    if decimals is not None:
        defaults["decimals"] = decimals
    all_overrides = list(node_colors()) if nodes else []
    # A fixed legend gets the validated palette, in its validated order.
    fixed = [t.get("legendFormat") for t in targets if "{{" not in (t.get("legendFormat") or "{{")]
    all_overrides += [color_override("byName", name, SERIES[i % len(SERIES)]) for i, name in enumerate(fixed)]
    if mirror:
        all_overrides.append({"matcher": {"id": "byRegexp", "options": mirror},
                              "properties": [{"id": "custom.transform", "value": "negative-Y"}]})
    all_overrides += overrides or []
    options = {
        "legend": {"displayMode": "table", "placement": "right" if right else "bottom", "showLegend": legend, "calcs": list(calcs)},
        "tooltip": {"mode": "multi", "sort": "desc", "hideZeros": False},
    }
    return panel("timeseries", title, targets, w, h, desc=desc, options=options,
                 fieldConfig={"defaults": defaults, "overrides": all_overrides}, **extra)


def gauge(title: str, targets: list[dict], w: int, h: int, *, overrides: list[dict], desc: str | None = None, **extra) -> dict:
    return panel("gauge", title, targets, w, h, desc=desc, options={
        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "orientation": "auto",
        "showThresholdLabels": False, "showThresholdMarkers": True, "sizing": "auto", "minVizWidth": 70, "minVizHeight": 70,
    }, fieldConfig={"defaults": {"unit": "percent", "min": 0, "max": 100, "mappings": [], "decimals": 0,
                                 "thresholds": steps(ACCENT), "color": {"mode": "thresholds"}},
                    "overrides": overrides}, **extra)


def state_timeline(title: str, targets: list[dict], table: dict, w: int = 24, h: int = 3, *, desc: str | None = None,
                   kind: str = "state-timeline", row_height: float = 0.85, legend: bool = True, **extra) -> dict:
    options = {"mergeValues": True, "showValue": "auto", "alignValue": "left", "rowHeight": row_height,
               "legend": {"displayMode": "list", "placement": "bottom", "showLegend": legend},
               "tooltip": {"mode": "single", "sort": "none"}}
    if kind == "status-history":
        options = {"showValue": "never", "rowHeight": row_height, "colWidth": 0.95,
                   "legend": {"displayMode": "list", "placement": "bottom", "showLegend": False},
                   "tooltip": {"mode": "single", "sort": "none"}}
    defaults = {"mappings": mappings(table) if table else [], "thresholds": extra.pop("th", steps(MUTED)),
                "unit": extra.pop("unit", "none"),
                "custom": {"lineWidth": 0, "fillOpacity": 85, "hideFrom": {"legend": False, "tooltip": False, "viz": False}}}
    if not table:
        # With value mappings this must stay unset: a state timeline in threshold
        # colour mode merges values into threshold buckets and ignores the mappings.
        defaults["color"] = {"mode": "thresholds"}
    return panel(kind, title, targets, w, h, desc=desc, options=options,
                 fieldConfig={"defaults": defaults, "overrides": []}, **extra)


def table(title: str, columns: list[tuple], w: int = 24, h: int = 7, *, labels: list[tuple[str, str]] | None = None,
          desc: str | None = None, sort: tuple[str, bool] | None = None, no_data: str | None = None,
          widths: dict[str, int] | None = None, **extra) -> dict:
    """One row per label set, one column per instant query.

    `columns` is a list of (expr, heading, cell options); a heading of None means
    the query is only there for its labels. `labels` is the list of (label, heading)
    columns to keep. Everything else is filtered out, so a new label on a metric
    cannot add a stray column.
    """
    targets, renames, order, overrides, keep = [], {}, {}, [], []
    position = 0
    for label, heading in labels or []:
        keep.append(label)
        renames[label] = heading
        order[label] = position
        position += 1
    for index, spec in enumerate(columns):
        expr, heading, opts = spec[0], spec[1], (spec[2] if len(spec) > 2 else {})
        targets.append(q(expr, "", table=True))
        # Grafana only adds the "#A" when a panel has more than one query.
        source = "Value" if len(columns) == 1 else f"Value #{chr(ord('A') + index)}"
        if heading is None:
            continue  # this query is here for its labels
        keep.append(source)
        renames[source] = heading
        order[source] = position
        position += 1
        props = []
        for key, value in opts.items():
            props.append({"id": {"cell": "custom.cellOptions", "width": "custom.width", "th": "thresholds",
                                 "maps": "mappings", "max": "max", "min": "min", "unit": "unit",
                                 "decimals": "decimals", "links": "links", "no_value": "noValue"}[key], "value": value})
        if props:
            overrides.append({"matcher": {"id": "byName", "options": heading}, "properties": props})
    for heading, width in (widths or {}).items():
        overrides.append({"matcher": {"id": "byName", "options": heading},
                          "properties": [{"id": "custom.width", "value": width}]})
    transformations = [
        {"id": "merge", "options": {}},
        {"id": "filterFieldsByName", "options": {"include": {"names": keep}}},
        {"id": "organize", "options": {"excludeByName": {}, "indexByName": order, "renameByName": renames}},
    ]
    options = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["sum"], "fields": ""},
               "sortBy": [{"displayName": sort[0], "desc": sort[1]}] if sort else []}
    defaults = {"custom": {"align": "auto", "cellOptions": {"type": "auto"}, "inspect": False, "filterable": False},
                "thresholds": steps(), "mappings": [], "color": {"mode": "thresholds"}}
    if no_data:
        defaults["noValue"] = no_data
    return panel("table", title, targets, w, h, desc=desc, options=options, transformations=transformations,
                 fieldConfig={"defaults": defaults, "overrides": overrides}, **extra)


def text(content: str, w: int = 24, h: int = 3) -> dict:
    return panel("text", "", None, w, h, options={"mode": "markdown", "content": content,
                                                  "code": {"language": "plaintext", "showLineNumbers": False, "showMiniMap": False}},
                 transparent=True)


# Table cells.
def gauge_cell(th: dict, *, max_: float = 100, unit: str = "percent") -> dict:
    return {"cell": {"type": "gauge", "mode": "basic", "valueDisplayMode": "text"}, "th": th, "min": 0, "max": max_, "unit": unit, "width": 104, "decimals": 0}


def colored(th: dict | None = None, *, unit: str | None = None, maps: list[dict] | None = None, width: int | None = None,
            decimals: int | None = None) -> dict:
    out: dict = {"cell": {"type": "color-text"}}
    if th:
        out["th"] = th
    if unit:
        out["unit"] = unit
    if maps:
        out["maps"] = maps
    if width:
        out["width"] = width
    if decimals is not None:
        out["decimals"] = decimals
    return out


TH_MEM = steps(OK, (85, WARN), (92, CRIT))
TH_SWAP = steps(OK, (50, WARN), (85, CRIT))
TH_CPU = steps(OK, (85, WARN), (97, SERIOUS))
TH_DISK = steps(OK, (80, WARN), (90, CRIT))
TH_GPU_TEMP = steps(OK, (86, WARN), (90, CRIT))
TH_NVME_LIFE = steps(OK, (60, WARN), (80, CRIT))
TH_NONZERO_BAD = steps(OK, (1, CRIT))
TH_NONZERO_WARN = steps(OK, (1, WARN))
HEALTH_MAP = mappings({"1": ("healthy", OK), "0": ("UNHEALTHY", CRIT), "2": ("starting", INFO)})
UP_MAP = mappings(UP_DOWN)
NODE_LINK = [{"title": "Open this Spark", "url": "/d/spark-node?var-node=${__data.fields[\"Node\"]}&${__url_time_range}", "targetBlank": False}]


def node_var(*, multi: bool = True, name: str = "node", label: str = "Spark") -> dict:
    return {
        "name": name, "label": label, "type": "query", "datasource": DS, "refresh": 2, "sort": 3,
        "query": {"query": 'label_values(up{job="node"}, node)', "refId": "fleet-nodes"},
        "definition": 'label_values(up{job="node"}, node)', "includeAll": multi, "multi": multi, "regex": "",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]} if multi else {},
        "allValue": ".*" if multi else None, "hide": 0, "options": [], "skipUrlSync": False,
    }


def label_var(name: str, label: str, query: str, *, multi: bool = True) -> dict:
    return {
        "name": name, "label": label, "type": "query", "datasource": DS, "refresh": 2, "sort": 1,
        "query": {"query": query, "refId": f"fleet-{name}"}, "definition": query, "includeAll": multi, "multi": multi,
        "regex": "", "current": {"selected": True, "text": ["All"], "value": ["$__all"]} if multi else {},
        "allValue": ".*" if multi else None, "hide": 0, "options": [], "skipUrlSync": False,
    }


class Dashboard:
    def __init__(self, uid: str, title: str, *, description: str, refresh: str = "30s", time_from: str = "now-1h",
                 variables: list[dict] | None = None, tags: tuple[str, ...] = ()) -> None:
        self.uid, self.title, self.description = uid, title, description
        self.refresh, self.time_from = refresh, time_from
        self.variables = variables or []
        self.tags = ["dgx-spark", "fleet", *tags]
        self.panels: list[dict] = []
        self.x = self.y = self.row_h = 0
        self.next_id = 1

    def row(self, title: str) -> None:
        if self.x:
            self.y += self.row_h
        self.x = self.row_h = 0
        self.panels.append({"id": self.next_id, "type": "row", "title": title, "collapsed": False, "panels": [],
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}})
        self.next_id += 1
        self.y += 1

    def add(self, *items: dict) -> None:
        for item in items:
            w, h = item.pop("_w"), item.pop("_h")
            if self.x + w > 24:
                self.y += self.row_h
                self.x = self.row_h = 0
            item["id"] = self.next_id
            self.next_id += 1
            item["gridPos"] = {"h": h, "w": w, "x": self.x, "y": self.y}
            self.panels.append(item)
            self.x += w
            self.row_h = max(self.row_h, h)

    def to_json(self) -> str:
        doc = {
            "uid": self.uid, "title": self.title, "description": self.description, "tags": self.tags,
            "schemaVersion": 39, "version": 1, "editable": False, "graphTooltip": 1, "fiscalYearStartMonth": 0,
            "liveNow": False, "weekStart": "", "timezone": "browser", "refresh": self.refresh,
            "time": {"from": self.time_from, "to": "now"},
            "timepicker": {"refresh_intervals": ["5s", "10s", "30s", "1m", "5m", "15m", "1h"]},
            "templating": {"list": self.variables}, "annotations": {"list": []}, "links": nav_links(),
            "panels": self.panels,
        }
        return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# -------------------------------------------------------------- command center --


def command_center() -> Dashboard:
    d = Dashboard("dgx-cluster-overview", "DGX Fleet — Command Center", refresh="10s", time_from="now-30m",
                  variables=[node_var()],
                  description="Is the fleet healthy, what needs attention, and how is each Spark doing. New Sparks appear by themselves.")
    d.add(
        stat("Fleet status", [q("fleet:health:code", instant=True)], 5, 4, size=28, no_value="MONITORING BLIND",
             maps=mappings({"0": ("ALL SYSTEMS NOMINAL", OK), "1": ("ATTENTION NEEDED", WARN), "2": ("CRITICAL", CRIT)}),
             desc="From the alerts that are firing now: any critical makes this red, any warning amber. Info alerts do not count. If this says MONITORING BLIND, Prometheus itself is not answering."),
        stat("Sparks", [q("fleet:sparks:up", "online", instant=True),
                        q("(fleet:sparks:total - fleet:sparks:up) or vector(0)", "down", instant=True),
                        q('count(spark_fleet_member{state=~"discovered|needs_ssh_key|enrolling|enroll_failed|not_a_dgx_spark"}) or vector(0)', "waiting", instant=True)],
             4, 4, text="value_and_name", no_value="no data",
             overrides=[field_override("byFrameRefID", "B", thresholds=steps(OK, (1, CRIT))),
                        field_override("byFrameRefID", "C", thresholds=steps(OK, (1, INFO)))],
             desc="Online and down are from Prometheus scrapes. Waiting means fleet discovery saw a Spark on the LAN that is not reporting yet; the Monitoring page says why."),
        stat("Alerts", [q('count(ALERTS{alertstate="firing",severity="critical"}) or vector(0)', "critical", instant=True),
                        q('count(ALERTS{alertstate="firing",severity="warning"}) or vector(0)', "warning", instant=True)],
             3, 4, text="value_and_name",
             overrides=[field_override("byFrameRefID", "A", thresholds=steps(OK, (1, CRIT))),
                        field_override("byFrameRefID", "B", thresholds=steps(OK, (1, WARN)))]),
        stat("Containers", [q("fleet:containers:running", "running", instant=True),
                            q("fleet:containers:unhealthy", "unhealthy", instant=True)],
             3, 4, text="value_and_name", no_value="no data",
             overrides=[field_override("byFrameRefID", "B", thresholds=steps(OK, (1, CRIT)))],
             desc="Every container on every Spark, not only TechSara's."),
        stat("Databases", [q("fleet:databases:up", "up", instant=True),
                           q("(fleet:databases:total - fleet:databases:up) or vector(0)", "down", instant=True)],
             3, 4, text="value_and_name", no_value="no data",
             overrides=[field_override("byFrameRefID", "B", thresholds=steps(OK, (1, CRIT)))],
             desc="Detected automatically, on every Spark. Up means the engine answered in its own protocol."),
        stat("GPU power", [q("cluster:gpu_power_watts:sum", "", instant=False)], 3, 4, unit="watt", decimals=0, spark=True,
             desc="All GPU rails added up. Not wall power: this hardware has no sensor for that."),
        stat("Hottest GPU", [q("cluster:gpu_temperature_celsius:max", "", instant=False)], 3, 4, unit="celsius", decimals=0, spark=True, th=TH_GPU_TEMP,
             desc="Shared SoC temperature. Slowdown at 86 °C, shutdown at 90 °C."),
    )
    d.row("Main model")
    d.add(
        stat("Engine", [q("cluster:vllm_service_state:code", instant=True)], 4, 3, maps=mappings(ENGINE_STATES), no_value="MONITORING_UNKNOWN", size=22,
             desc="The engine state, from a real completion and not from a process being alive. READY and BUSY serve; DEGRADED serves with a warning; WEDGED, RECOVERING and QUEUEING hold requests; DOWN needs a person."),
        stat("Readiness", [q("2 * techsara_vllm_participation_ok + techsara_vllm_synthetic_success", instant=True)], 3, 3, size=22, no_value="unknown",
             maps=mappings({"3": ("proven", OK), "1": ("GPUs unproven", WARN), "2": ("canary failing", CRIT), "0": ("unproven", CRIT)}),
             desc="Proven means the last canary completion succeeded and both GPUs were seen working."),
        stat("Worker rank", [q("techsara_vllm_worker_rank_alive and on() (techsara_vllm_worker_reachable == 1)", instant=True)], 3, 3, size=22, no_value="unknown",
             maps=mappings({"1": ("alive", OK), "0": ("ABSENT", CRIT)}),
             desc="Is the worker's model process alive, according to the sentinel on that Spark. Unknown while the sentinel cannot be reached."),
        stat("Last inference", [q("cluster:vllm_last_success_age_seconds", instant=True)], 3, 3, unit="s", decimals=0, size=22, no_value="no data",
             th=steps(OK, (60, WARN), (300, CRIT)), desc="Seconds since a real streamed completion finished. A hung process cannot fake this."),
        stat("Canary TTFT", [q("techsara_vllm_synthetic_ttft_seconds and on() (techsara_vllm_synthetic_success == 1)", instant=True)], 3, 3, unit="s", decimals=2, size=22, no_value="no data",
             th=steps(OK, (3, WARN), (10, CRIT))),
        stat("Incident", [q("cluster:vllm_incident_duration_seconds", instant=True)], 2, 3, unit="s", decimals=0, size=22, no_value="none",
             th=steps(MUTED, (1, SERIOUS))),
        stat("Recoveries 1h", [q('increase(techsara_vllm_recovery_attempts_total{outcome="started"}[1h])', instant=True)], 3, 3, decimals=0, size=22, no_value="no data",
             th=steps(OK, (1, WARN), (3, CRIT)), desc="Coordinated restarts the controller started in the last hour. The budget is 3 an hour."),
        stat("Last failure", [q("techsara_vllm_last_failure_category == 1", instant=True)], 3, 3, fields="/^category$/", size=14, no_value="none",
             th=steps(MUTED),
             desc="The kind of the last failure. This is history, not status: it keeps its value after recovery, so read it with Incident. Grey on purpose."),
    )
    d.add(
        stat("Tokens/s", [q("cluster:vllm_generation_tokens_per_second:rate1m", "")], 4, 3, spark=True, decimals=0, size=22, no_value="no data"),
        stat("Prompt tokens/s", [q("cluster:vllm_prompt_tokens_per_second:rate1m", "")], 4, 3, spark=True, decimals=0, size=22, no_value="no data"),
        stat("Requests running", [q("cluster:vllm_requests_running:current", "")], 4, 3, spark=True, decimals=0, size=22, no_value="no data"),
        stat("Requests waiting", [q("cluster:vllm_requests_waiting:current", "")], 4, 3, spark=True, decimals=0, size=22, no_value="no data",
             th=steps(OK, (1, WARN), (6, SERIOUS))),
        stat("Queued for recovery", [q("llm_queued_generations", "")], 4, 3, spark=True, decimals=0, size=22, no_value="none",
             th=steps(OK, (1, SERIOUS)), desc="Accepted requests held while the model is not ready. They resume by themselves."),
        stat("KV cache", [q("cluster:vllm_kv_cache_usage_percent:current", "")], 4, 3, spark=True, unit="percent", decimals=1, size=22, no_value="no data",
             th=steps(OK, (70, WARN), (90, CRIT)), desc="Above about 90 % vLLM starts preempting requests."),
    )
    d.row("Needs attention")
    d.add(
        table("Active alerts", [
            ('max by (alertname, severity, node, container, device, service) (ALERTS_FOR_STATE and ignoring (alertstate) ALERTS{alertstate="firing"}) * 1000',
             "Since", {"unit": "dateTimeFromNow", "width": 130})],
            14, 6, labels=[("severity", "Severity"), ("alertname", "Alert"), ("node", "Node"), ("container", "Container"), ("device", "Device"), ("service", "Service")],
            sort=("Since", True), desc="Everything firing now. Definitions and history: the Alerts + SLO page. Empty means nothing is firing.",
            overrides_severity=True),
        table("Containers in trouble", [
            ("max by (node, container, project) (spark_container_health_status == 0)", "Unhealthy", {"cell": {"type": "color-text"}, "th": steps(CRIT), "width": 90, "maps": mappings({"0": ("yes", CRIT)})}),
            ("max by (node, container, project) ((spark_container_expected_running == 1) and on (node, container) (spark_container_running == 0))", "Stopped", {"cell": {"type": "color-text"}, "th": steps(CRIT), "width": 80, "maps": mappings({"1": ("yes", CRIT)})}),
            ("sum by (node, container, project) (changes(spark_container_started_timestamp_seconds[1h]) > 0)", "Restarts 1h", {"cell": {"type": "color-text"}, "th": steps(WARN), "width": 90}),
        ], 10, 6, labels=[("node", "Node"), ("container", "Container"), ("project", "Project")],
            desc="Failing a health check, stopped when its restart policy says it should run, or restarted in the last hour. Empty means none."),
    )
    d.row("Sparks")
    d.add(fleet_table())
    d.row("$node")
    d.panels[-1]["repeat"] = "node"
    d.add(*node_strip())
    d.row("Live correlation: one time axis, to explain a dip")
    d.add(state_timeline("Engine state", [q("cluster:vllm_service_state:code", "engine")], ENGINE_STATES, 24, 3, legend=False,
                         desc="A gap is a gap: a stale controller verdict draws nothing rather than a line across it."))
    d.add(
        ts("Tokens per second", [q("cluster:vllm_generation_tokens_per_second:rate1m", "generation"),
                                 q("cluster:vllm_prompt_tokens_per_second:rate1m", "prompt")], nodes=False,
           ),
        ts("Requests", [q("cluster:vllm_requests_running:current", "running"), q("cluster:vllm_requests_waiting:current", "waiting")],
           nodes=False),
        ts("KV cache", [q("cluster:vllm_kv_cache_usage_percent:current", "used")], nodes=False, unit="percent", legend=False),
        ts("GPU power", [q('node:gpu_power_watts:sum{node=~"$node"}', "{{node}}")], unit="watt", desc="The honest load signal: a spinning rank reads 95 % busy at about 34 W."),
        ts("GPU busy", [q('node:gpu_utilization_percent:avg{node=~"$node"}', "{{node}}")], unit="percent", max_=100),
        ts("GPU temperature", [q('node:gpu_temperature_celsius:max{node=~"$node"}', "{{node}}")], unit="celsius", min_=None, limits=steps(NEUTRAL, (86, WARN), (90, CRIT))),
        ts("Unified memory", [q('node:memory_used_percent:current{node=~"$node"}', "{{node}}")], unit="percent", max_=100),
        ts("Swap traffic", [q('node:swap_pages_per_second:rate5m{node=~"$node"}', "{{node}}")], unit="short",
           desc="Pages in and out per second. Sustained traffic means memory pressure, and it slows the model."),
        ts("Stalled on memory", [q(f'100 * rate(node_pressure_memory_stalled_seconds_total{{node=~"$node"}}[{RATE}])', "{{node}}", interval="15s")], unit="percent",
           desc="Share of time every task was blocked waiting for memory (PSI full)."),
        ts("CPU", [q('node:cpu_utilization_percent:avg{node=~"$node"}', "{{node}}")], unit="percent", max_=100),
        ts("RoCE fabric", [q('node:roce_receive_bytes_per_second:sum{node=~"$node"}', "{{node}} rx"),
                           q('node:roce_transmit_bytes_per_second:sum{node=~"$node"}', "{{node}} tx")], unit="Bps", mirror="/ tx$/",
           desc="Received above the line, sent below. Rises during generation: the proof the model is really distributed."),
        ts("Management LAN", [q('node:ethernet_receive_bytes_per_second:sum{node=~"$node"}', "{{node}} rx"),
                              q('node:ethernet_transmit_bytes_per_second:sum{node=~"$node"}', "{{node}} tx")], unit="Bps", mirror="/ tx$/"),
    )
    d.row("Databases")
    d.add(database_table(h=8))
    d.row("Service health")
    d.add(
        stat("Application endpoints", [q("probe_success", "{{service}}", instant=True)], 10, 3, maps=UP_MAP, th=steps(CRIT, (1, OK)), text="value_and_name", size=18,
             desc="HTTP probes of the services that have no metrics of their own."),
        stat("Model services", [q('up{job="vllm-aux"}', "{{service}}", instant=True)], 10, 3, maps=UP_MAP, th=steps(CRIT, (1, OK)), text="value_and_name", size=18),
        stat("Data", [q("pg_up", "postgres", instant=True), q('techsara_store_present{store="warehouse_snapshot"}', "warehouse", instant=True)],
             4, 3, maps=UP_MAP, th=steps(CRIT, (1, OK)), text="value_and_name", size=18),
    )
    return d


def fleet_table() -> dict:
    return table("All Sparks", [
        ('max by (node, role) (up{job="node"})', "Status", colored(maps=UP_MAP, th=steps(CRIT, (1, OK)), width=66)),
        ("max by (node, nodename) (node_uname_info)", None),
        ("max by (node) (node:uptime_seconds:current)", "Uptime", {"unit": "dtdurations", "width": 78, "decimals": 0}),
        ("max by (node) (node:gpu_utilization_percent:avg)", "GPU busy", {"unit": "percent", "width": 80, "decimals": 0}),
        ("max by (node) (node:gpu_power_watts:sum)", "GPU W", {"unit": "watt", "width": 68, "decimals": 0}),
        ("max by (node) (node:gpu_temperature_celsius:max)", "GPU °C", colored(TH_GPU_TEMP, unit="celsius", width=70, decimals=0)),
        ("max by (node) (node:memory_used_percent:current)", "Memory", gauge_cell(TH_MEM)),
        ("max by (node) (node:swap_used_percent:current)", "Swap", gauge_cell(TH_SWAP)),
        ("max by (node) (node:cpu_utilization_percent:avg)", "CPU", gauge_cell(TH_CPU)),
        ("max by (node) (node:disk_used_percent:root)", "Disk", gauge_cell(TH_DISK)),
        ("100 * max by (node) (spark_nvme_percentage_used_ratio)", "SSD used", colored(TH_NVME_LIFE, unit="percent", width=82, decimals=0)),
        ("max by (node) (spark_nvme_unsafe_shutdowns_total)", "Power cuts", {"width": 92}),
        ("max by (node) (node:roce_receive_bytes_per_second:sum + node:roce_transmit_bytes_per_second:sum)", "RoCE", {"unit": "Bps", "width": 88, "decimals": 0}),
        ('max by (node) (spark_containers{status="running"})', "Containers", {"width": 92}),
        ("max by (node) (node:containers:unhealthy)", "Unhealthy", colored(TH_NONZERO_BAD, width=88)),
        ("max by (node) (spark_db_instances)", "DBs", {"width": 48}),
        ("max by (node) (node:kernel_faults:increase1h)", "Faults 1h", colored(TH_NONZERO_BAD, width=80, decimals=0)),
    ], 24, 6, labels=[("node", "Node"), ("nodename", "Host"), ("role", "Role")], sort=("Node", False), widths={"Host": 100, "Role": 70},
        desc="One row per Spark. Click a name for everything about it. A Spark that dies stays listed as DOWN. Blank means the reading is missing, not zero. Power cuts: shutdowns the SSD was not warned about, over its whole life.",
        node_link=True)


def node_strip() -> list[dict]:
    """One strip per Spark, repeated by Grafana for however many exist."""
    this = '{node="$node"}'
    dials = gauge("Now", [
        q(f"max(node:gpu_utilization_percent:avg{this})", "GPU busy"),
        q(f"max(node:cpu_utilization_percent:avg{this})", "CPU"),
        q(f"max(node:memory_used_percent:current{this})", "Memory"),
        q(f"max(node:swap_used_percent:current{this})", "Swap"),
        q(f"max(node:disk_used_percent:root{this})", "Disk"),
        q(f"max(node:gpu_temperature_celsius:max{this})", "GPU temp"),
    ], 10, 5, overrides=[
        field_override("byFrameRefID", "B", thresholds=TH_CPU),
        field_override("byFrameRefID", "C", thresholds=TH_MEM),
        field_override("byFrameRefID", "D", thresholds=TH_SWAP),
        field_override("byFrameRefID", "E", thresholds=TH_DISK),
        field_override("byFrameRefID", "F", unit="celsius", thresholds=TH_GPU_TEMP),
    ], desc="GPU busy is not load: the model's worker rank spins at about 95 % while it waits, so read it with GPU power.",
        links=[{"title": "Everything about this Spark", "url": "/d/spark-node?var-node=${node}&${__url_time_range}", "targetBlank": False}])
    facts = stat("Details", [
        q(f"max(node:gpu_power_watts:sum{this})", "GPU power", instant=True),
        q(f"max(node:roce_receive_bytes_per_second:sum{this} + node:roce_transmit_bytes_per_second:sum{this})", "RoCE", instant=True),
        q(f"max(node:uptime_seconds:current{this})", "Uptime", instant=True),
        q(f'max(spark_containers{{node="$node",status="running"}})', "Containers", instant=True),
        q(f"max(spark_db_instances{this})", "Databases", instant=True),
        q(f"100 * max(spark_nvme_percentage_used_ratio{this})", "SSD used", instant=True),
    ], 6, 5, text="value_and_name", size=20, title_size=11, no_value="-", overrides=[
        field_override("byFrameRefID", "A", unit="watt", decimals=0),
        field_override("byFrameRefID", "B", unit="Bps", decimals=0),
        field_override("byFrameRefID", "C", unit="dtdurations", decimals=0),
        field_override("byFrameRefID", "F", unit="percent", decimals=0, thresholds=TH_NVME_LIFE),
    ])
    trend = ts("Trend", [
        q(f"max(node:gpu_utilization_percent:avg{this})", "GPU busy"),
        q(f"max(node:cpu_utilization_percent:avg{this})", "CPU"),
        q(f"max(node:memory_used_percent:current{this})", "Memory"),
        q(f"max(node:swap_used_percent:current{this})", "Swap"),
    ], 8, 5, unit="percent", max_=100, nodes=False, calcs=(), fill=0)
    trend["options"]["legend"]["displayMode"] = "list"
    return [dials, facts, trend]


def database_table(h: int = 9, where: str = "") -> dict:
    """`where` is a selector such as {node="$node"}. Resource columns come from the
    container series, so they are filtered through the database they belong to."""
    sel = where
    inner = where[1:-1] + "," if where else ""
    return table("All databases", [
        (f"max by (node, container, engine, project, version, stats) (spark_db_info{sel})", None),
        (f"max by (node, container) (spark_db_up{sel})", "Status", colored(maps=UP_MAP, th=steps(CRIT, (1, OK)), width=68)),
        (f"1000 * max by (node, container) (spark_db_probe_seconds{sel})", "Reply", {"unit": "ms", "width": 76, "decimals": 1}),
        (f"sum by (node, container) (spark_db_connections{sel})", "Conns", {"width": 66}),
        (f'sum by (node, container) (spark_db_connections{{{inner}state="active"}})', "Active", {"width": 66}),
        (f"max by (node, container) (spark_db_connections_max{sel})", "Limit", {"width": 58}),
        (f"sum by (node, container) (rate(spark_db_transactions_total{sel}[2m]) or rate(spark_db_commands_total{sel}[2m]))", "Ops/s", {"width": 72, "decimals": 1}),
        (f'100 * sum by (node, container) (rate(spark_db_cache_reads_total{{{inner}source="hit"}}[5m])) / sum by (node, container) (rate(spark_db_cache_reads_total{sel}[5m]) > 0)',
         "Cache hit", colored(steps(CRIT, (80, WARN), (95, OK)), unit="percent", width=82, decimals=1)),
        (f"sum by (node, container) (rate(spark_container_cpu_seconds_total[2m]) and on (node, container) spark_db_up{sel})", "CPU", {"width": 62, "decimals": 2}),
        (f"max by (node, container) (spark_container_memory_working_set_bytes and on (node, container) spark_db_up{sel})", "Memory", {"unit": "bytes", "width": 88, "decimals": 1}),
        (f"max by (node, container) (spark_container_memory_swap_bytes and on (node, container) spark_db_up{sel})", "Swap", {"unit": "bytes", "width": 88, "decimals": 1}),
        (f"max by (node, container) (spark_db_data_bytes{sel})", "On disk", {"unit": "bytes", "width": 88, "decimals": 1}),
        (f"sum by (node, container) (rate(spark_container_disk_written_bytes_total[2m]) and on (node, container) spark_db_up{sel})", "Writes", {"unit": "Bps", "width": 88, "decimals": 0}),
    ], 24, h, labels=[("node", "Node"), ("engine", "Engine"), ("container", "Database"), ("project", "Project"), ("version", "Version"), ("stats", "Detail")],
        sort=("On disk", True), widths={"Node": 72, "Engine": 78, "Database": 250, "Project": 150, "Version": 70, "Detail": 72},
        desc="Found automatically on every Spark. Status, version, connections, CPU, memory and disk need no login. Detail says what else is known: `login` means full statistics, `info` means the engine answers without one, `liveness` means no credentials are in use for this project. Blank means not measured, not zero.")


# ------------------------------------------------------------- one spark --


def spark_node() -> Dashboard:
    n = '{node="$node"}'
    d = Dashboard("spark-node", "Spark — Everything", refresh="30s", variables=[node_var(multi=False)],
                  description="Every reading from one DGX Spark: GPU, CPU, unified memory, NVMe, network, RoCE, thermals, containers, databases, systemd and kernel.")
    d.add(
        stat("Status", [q('up{job="node",node="$node"}', instant=True)], 2, 3, maps=UP_MAP, th=steps(CRIT, (1, OK)), size=22),
        stat("Host", [q(f"node_uname_info{n}", "{{nodename}}", instant=True)], 3, 3, text="name", size=16),
        stat("Uptime", [q(f"node:uptime_seconds:current{n}", instant=True)], 2, 3, unit="dtdurations", decimals=0, size=16, th=steps(WARN, (3600, NEUTRAL))),
        stat("Power cuts", [q(f"spark_nvme_unsafe_shutdowns_total{n}", instant=True)], 3, 3, size=22, no_value="no data",
             desc="Shutdowns the SSD was not warned about, over its whole life: power cuts and hard resets."),
        stat("Faults 1h", [q(f"node:kernel_faults:increase1h{n}", instant=True)], 2, 3, th=TH_NONZERO_BAD, size=22, decimals=0, no_value="no data"),
        stat("Failed", [q('count(node_systemd_unit_state{node="$node",state="failed"} == 1) or vector(0)', instant=True)], 2, 3, th=TH_NONZERO_WARN, size=22),
        stat("Host guard", [q('node_systemd_unit_state{node="$node",name="techsara-host-guard.service",state="active"}', instant=True)], 3, 3, size=16, no_value="NOT INSTALLED",
             maps=mappings({"1": ("active", OK), "0": ("INACTIVE", CRIT)}), th=steps(WARN),
             desc="The packet filter that keeps model and exporter ports off the office LAN."),
        stat("OS", [q(f"node_os_info{n}", "{{pretty_name}}", instant=True)], 3, 3, text="name", size=12),
        stat("Kernel", [q(f"node_uname_info{n}", "{{release}}", instant=True)], 2, 3, text="name", size=12),
        stat("DGX OS", [q(f"spark_node_info{n}", "{{dgx_os}}", instant=True)], 2, 3, text="name", size=14, no_value="no data"),
    )
    d.row("GPU")
    d.add(
        ts("GPU busy", [q(f"node:gpu_utilization_percent:avg{n}", "busy")], 6, nodes=False, unit="percent", max_=100, legend=False,
           overrides=[color_override("byName", "busy", SERIES[0])], desc="Share of time a kernel was executing. The worker rank spins, so this can read 95 % while nothing useful happens: read it with power."),
        ts("Busy by process", [q(f"sum by (process) (dgx_gpu_process_utilization_percent{n})", "{{process}}")], 6, nodes=False, unit="percent", stack=True, desc="Which process the busy share belongs to."),
        ts("GPU power", [q(f"sum(dgx_gpu_power_watts{n})", "average"), q(f"sum(dgx_gpu_power_instant_watts{n})", "instant")], 6, nodes=False, unit="watt"),
        ts("Temperature and headroom", [q(f"dgx_gpu_temperature_celsius{n}", "temperature"), q(f"dgx_gpu_temperature_headroom_celsius{n}", "headroom to throttle")], 6, nodes=False, unit="celsius",
           limits=steps(NEUTRAL, (86, WARN), (90, CRIT))),
        ts("Clocks", [q(f"dgx_gpu_clock_sm_hertz{n}", "SM"), q(f"dgx_gpu_clock_graphics_hertz{n}", "graphics"), q(f"dgx_gpu_clock_sm_max_hertz{n}", "SM maximum")], 6, nodes=False, unit="hertz"),
        ts("Processes using the GPU", [q(f"dgx_gpu_compute_processes{n}", "processes")], 6, nodes=False, legend=False),
        ts("GPU memory by owner", [q(f"sum by (container) (spark_container_gpu_memory_bytes{n})", "{{container}}"), q(f"spark_host_gpu_memory_bytes{n}", "outside containers")], 6, nodes=False, unit="bytes", stack=True,
           desc="Unified memory held by CUDA contexts, attributed to containers."),
        state_timeline("Throttling", [q(f'max by (reason) (dgx_gpu_throttle_active{{node="$node",reason!="sync_boost"}})', "{{reason}}")], {"0": ("no", "#2b2f36"), "1": ("THROTTLED", CRIT)}, 6, 7, legend=False,
                       desc="The hardware's own explanation for a slowdown."),
    )
    d.row("CPU")
    d.add(
        ts("By mode", [q(f'100 * sum by (mode) (rate(node_cpu_seconds_total{{node="$node",mode!="idle"}}[{RATE}])) / scalar(count(node_cpu_seconds_total{{node="$node",mode="idle"}}))', "{{mode}}", interval="15s")], 8, nodes=False, unit="percent", stack=True, max_=100,
           desc="iowait high with user low means the CPU is waiting on disk, not busy."),
        state_timeline("Every core", [q(f'label_replace(100 * (1 - avg by (cpu) (rate(node_cpu_seconds_total{{node="$node",mode="idle"}}[{RATE}]))), "cpu", "0$1", "cpu", "^(\\\\d)$")', "{{cpu}}", interval="15s")], {}, 8, 8, kind="status-history", row_height=1, unit="percent",
                       th=steps("#1f2733", (10, "#184f95"), (35, "#256abf"), (60, "#3987e5"), (80, "#d95926"), (95, "#f2495c")),
                       desc="This SoC has ten fast cores at 3.9 GHz and ten efficient ones at 2.8 GHz. A single hot line is a single-threaded bottleneck the average hides."),
        ts("Frequency by core group", [q(f'avg(node_cpu_scaling_frequency_hertz{n} and on (cpu) (node_cpu_frequency_max_hertz{n} > 3.5e9))', "fast cores"),
                                       q(f'avg(node_cpu_scaling_frequency_hertz{n} and on (cpu) (node_cpu_frequency_max_hertz{n} < 3.5e9))', "efficient cores")], 8, nodes=False, unit="hertz", min_=None),
        ts("Load", [q(f"node_load1{n}", "1 min"), q(f"node_load5{n}", "5 min"), q(f"node_load15{n}", "15 min"), q(f'count(node_cpu_seconds_total{{node="$node",mode="idle"}})', "cores")], 6, nodes=False),
        ts("Waiting for CPU", [q(f"100 * rate(node_pressure_cpu_waiting_seconds_total{n}[{RATE}])", "some tasks waiting", interval="15s")], 6, nodes=False, unit="percent", legend=False, desc="PSI: share of time at least one task wanted a CPU and did not get it."),
        ts("Runnable and blocked tasks", [q(f"node_procs_running{n}", "runnable"), q(f"node_procs_blocked{n}", "blocked on I/O")], 6, nodes=False),
        ts("Context switches and interrupts", [q(f"rate(node_context_switches_total{n}[{RATE}])", "context switches", interval="15s"), q(f"rate(node_intr_total{n}[{RATE}])", "interrupts", interval="15s")], 6, nodes=False, unit="ops"),
        ts("Top processes by CPU", [q(f"topk(10, spark_process_cpu_cores{n})", "{{comm}}")], 8, nodes=False, unit="short", calcs=("lastNotNull", "max"), desc="Grouped by process name. In cores."),
        table("Top processes by memory", [(f"max by (comm) (spark_process_rss_bytes{n})", "Resident", {"unit": "bytes", "decimals": 1}), (f"max by (comm) (spark_process_count{n})", "Count", {"width": 70}), (f"max by (comm) (spark_process_cpu_cores{n})", "CPU cores", {"decimals": 2, "width": 90})], 8, 8, labels=[("comm", "Process")], sort=("Resident", True),
              desc="Shared memory counts once per process, so PostgreSQL reads high. GPU memory is not in these figures on this hardware."),
        ts("Processes, threads, open files", [q(f"node_processes_pids{n}", "processes"), q(f"node_processes_threads{n}", "threads"), q(f"node_filefd_allocated{n}", "open files")], 8, nodes=False),
    )
    d.row("Unified memory: shared between the CPU and the GPU")
    d.add(
        ts("Where the memory is", [
            q(f"node_memory_AnonPages_bytes{n}", "processes"),
            q(f"node_memory_Cached_bytes{n} - node_memory_Shmem_bytes{n} + node_memory_Buffers_bytes{n}", "page cache"),
            q(f"node_memory_Shmem_bytes{n}", "shared"),
            q(f"node_memory_Slab_bytes{n} + node_memory_PageTables_bytes{n} + node_memory_KernelStack_bytes{n}", "kernel"),
            q(f"node_memory_MemTotal_bytes{n} - node_memory_MemFree_bytes{n} - node_memory_Cached_bytes{n} - node_memory_Buffers_bytes{n} - node_memory_AnonPages_bytes{n} - node_memory_Slab_bytes{n} - node_memory_PageTables_bytes{n} - node_memory_KernelStack_bytes{n}", "GPU and driver (the rest)"),
            q(f"node_memory_MemFree_bytes{n}", "free"),
        ], 12, 8, nodes=False, unit="bytes", stack=True, calcs=("lastNotNull",),
           desc="GPU memory is not in any process's resident size on this hardware, so model weights show up as the remainder."),
        ts("Available and GPU-held", [q(f"node_memory_MemAvailable_bytes{n}", "available"), q(f"dgx_gpu_memory_allocated_bytes{n}", "held by GPU contexts"), q(f"node_memory_MemTotal_bytes{n}", "total")], 6, 8, nodes=False, unit="bytes"),
        ts("Swap", [q(f"node:swap_used_bytes:current{n}", "used"), q(f"node_memory_SwapTotal_bytes{n}", "size")], 6, 8, nodes=False, unit="bytes", desc="When swap is full, the next shortfall goes straight to the OOM killer."),
        ts("Swap traffic", [q(f"rate(node_vmstat_pswpin{n}[{RATE}])", "in", interval="15s"), q(f"rate(node_vmstat_pswpout{n}[{RATE}])", "out", interval="15s")], 6, nodes=False, unit="short", mirror="/out$/", desc="Pages per second."),
        ts("Pressure", [q(f"100 * rate(node_pressure_memory_stalled_seconds_total{n}[{RATE}])", "all tasks stalled", interval="15s"), q(f"100 * rate(node_pressure_memory_waiting_seconds_total{n}[{RATE}])", "some tasks stalled", interval="15s")], 6, nodes=False, unit="percent", desc="PSI. All tasks stalled on memory is time the machine did no work at all."),
        ts("Major page faults", [q(f"rate(node_vmstat_pgmajfault{n}[{RATE}])", "faults/s", interval="15s")], 6, nodes=False, unit="short", legend=False, desc="A fault that has to go to disk."),
        ts("Kernel OOM kills", [q(f"increase(node_vmstat_oom_kill{n}[1h])", "last hour")], 6, nodes=False, unit="short", legend=False, quiet=True),
    )
    d.row("Storage")
    d.add(
        stat("Root disk", [q('100 - 100 * node_filesystem_avail_bytes{node="$node",mountpoint="/",fstype!~"tmpfs|overlay"} / node_filesystem_size_bytes{node="$node",mountpoint="/",fstype!~"tmpfs|overlay"}', instant=True)], 3, 3, unit="percent", decimals=0, th=TH_DISK, size=22),
        stat("SSD life used", [q(f"100 * spark_nvme_percentage_used_ratio{n}", instant=True)], 3, 3, unit="percent", decimals=0, th=TH_NVME_LIFE, size=22, no_value="no data", desc="The vendor's estimate. It can pass 100 %; data is not lost at that point, but the warranty is."),
        stat("Spare left", [q(f"100 * spark_nvme_available_spare_ratio{n}", instant=True)], 2, 3, unit="percent", decimals=0, th=steps(CRIT, (20, WARN), (50, OK)), size=22, no_value="no data"),
        stat("Errors", [q(f"spark_nvme_media_errors_total{n}", instant=True)], 2, 3, th=TH_NONZERO_BAD, size=22, no_value="no data"),
        stat("Warning", [q(f"spark_nvme_critical_warning{n}", instant=True)], 2, 3, th=TH_NONZERO_BAD, size=22, no_value="no data"),
        stat("Written", [q(f"spark_nvme_data_written_bytes_total{n}", instant=True)], 3, 3, unit="decbytes", decimals=1, size=22, no_value="no data"),
        stat("Read", [q(f"spark_nvme_data_read_bytes_total{n}", instant=True)], 3, 3, unit="decbytes", decimals=1, size=22, no_value="no data"),
        stat("Powered on", [q(f"spark_nvme_power_on_seconds_total{n}", instant=True)], 3, 3, unit="dtdurations", decimals=0, size=16, no_value="no data",
             desc="The drive's own count. The NVMe specification lets it leave out time in low-power states, so it runs far behind the calendar."),
        stat("Power cycles", [q(f"spark_nvme_power_cycles_total{n}", instant=True)], 3, 3, size=22, no_value="no data"),
    )
    d.add(
        ts("Throughput", [q(f'rate(node_disk_read_bytes_total{{node="$node",device=~"nvme.*"}}[{RATE}])', "{{device}} read", interval="15s"), q(f'rate(node_disk_written_bytes_total{{node="$node",device=~"nvme.*"}}[{RATE}])', "{{device}} write", interval="15s")], 6, nodes=False, unit="Bps", mirror="/write$/"),
        ts("Operations", [q(f'rate(node_disk_reads_completed_total{{node="$node",device=~"nvme.*"}}[{RATE}])', "{{device}} read", interval="15s"), q(f'rate(node_disk_writes_completed_total{{node="$node",device=~"nvme.*"}}[{RATE}])', "{{device}} write", interval="15s")], 6, nodes=False, unit="iops", mirror="/write$/"),
        ts("Latency", [q(f'rate(node_disk_read_time_seconds_total{{node="$node",device=~"nvme.*"}}[{RATE}]) / rate(node_disk_reads_completed_total{{node="$node",device=~"nvme.*"}}[{RATE}])', "{{device}} read", interval="15s"), q(f'rate(node_disk_write_time_seconds_total{{node="$node",device=~"nvme.*"}}[{RATE}]) / rate(node_disk_writes_completed_total{{node="$node",device=~"nvme.*"}}[{RATE}])', "{{device}} write", interval="15s")], 6, nodes=False, unit="s", desc="Average time per completed operation."),
        ts("Busy and queue", [q(f'100 * rate(node_disk_io_time_seconds_total{{node="$node",device=~"nvme.*"}}[{RATE}])', "{{device}} busy %", interval="15s"), q(f'rate(node_disk_io_time_weighted_seconds_total{{node="$node",device=~"nvme.*"}}[{RATE}])', "{{device}} queue depth", interval="15s")], 6, nodes=False),
        ts("Stalled on disk", [q(f"100 * rate(node_pressure_io_stalled_seconds_total{n}[{RATE}])", "all tasks stalled", interval="15s"), q(f"100 * rate(node_pressure_io_waiting_seconds_total{n}[{RATE}])", "some tasks stalled", interval="15s")], 6, nodes=False, unit="percent"),
        ts("Filesystems", [q('100 - 100 * node_filesystem_avail_bytes{node="$node",fstype!~"tmpfs|overlay|squashfs|ramfs|fuse.*"} / node_filesystem_size_bytes{node="$node",fstype!~"tmpfs|overlay|squashfs|ramfs|fuse.*"}', "{{mountpoint}}")], 6, nodes=False, unit="percent", max_=100),
        ts("Inodes used", [q('100 - 100 * node_filesystem_files_free{node="$node",fstype=~"ext4|xfs|btrfs"} / node_filesystem_files{node="$node",fstype=~"ext4|xfs|btrfs"}', "{{mountpoint}}")], 6, nodes=False, unit="percent", max_=100),
        ts("SSD writes per day", [q(f"increase(spark_nvme_data_written_bytes_total{n}[1d])", "written, last 24 h")], 6, nodes=False, unit="decbytes", legend=False, desc="What wears the drive. Swap traffic lands here."),
    )
    d.row("Network: everything except RDMA traffic")
    d.add(
        table("Interfaces", [
            (f'max by (device) (node_network_up{{node="$node",device!~"lo|veth.*|br-.*|docker.*"}})', "Up", colored(maps=mappings({"1": ("up", OK), "0": ("down", MUTED)}), th=steps(MUTED, (1, OK)), width=60)),
            (f'max by (device) (node_network_speed_bytes{{node="$node",device!~"lo|veth.*|br-.*|docker.*"}} > 0) * 8', "Speed", {"unit": "bps", "decimals": 0, "width": 90}),
            (f'max by (device) (node_network_mtu_bytes{{node="$node",device!~"lo|veth.*|br-.*|docker.*"}})', "MTU", {"width": 65}),
            (f'sum by (device) (rate(node_network_receive_bytes_total{{node="$node",device!~"lo|veth.*|br-.*|docker.*"}}[2m]))', "Received", {"unit": "Bps", "decimals": 1}),
            (f'sum by (device) (rate(node_network_transmit_bytes_total{{node="$node",device!~"lo|veth.*|br-.*|docker.*"}}[2m]))', "Sent", {"unit": "Bps", "decimals": 1}),
            (f'sum by (device) (rate(node_network_receive_errs_total{{node="$node",device!~"lo|veth.*|br-.*|docker.*"}}[5m]) + rate(node_network_transmit_errs_total{{node="$node",device!~"lo|veth.*|br-.*|docker.*"}}[5m]))', "Errors/s", colored(TH_NONZERO_BAD, decimals=2, width=80)),
            (f'sum by (device) (rate(node_network_receive_drop_total{{node="$node",device!~"lo|veth.*|br-.*|docker.*"}}[5m]) + rate(node_network_transmit_drop_total{{node="$node",device!~"lo|veth.*|br-.*|docker.*"}}[5m]))', "Drops/s", colored(TH_NONZERO_WARN, decimals=2, width=80)),
            (f'sum by (device) (increase(node_network_carrier_changes_total{{node="$node",device!~"lo|veth.*|br-.*|docker.*"}}[1h]))', "Flaps 1h", colored(steps(NEUTRAL, (3, WARN)), decimals=0, width=75)),
        ], 12, 8, labels=[("device", "Interface")], sort=("Interface", False),
            desc="Docker's veths and bridges are left out; per-container traffic is on the Containers page."),
        ts("Traffic", [q(f'rate(node_network_receive_bytes_total{{node="$node",device!~"lo|veth.*|br-.*|docker.*|.*np[0-9]"}}[{RATE}])', "{{device}} rx", interval="15s"), q(f'rate(node_network_transmit_bytes_total{{node="$node",device!~"lo|veth.*|br-.*|docker.*|.*np[0-9]"}}[{RATE}])', "{{device}} tx", interval="15s")], 12, 8, nodes=False, unit="Bps", mirror="/ tx$/"),
        ts("TCP connections", [q(f"node_netstat_Tcp_CurrEstab{n}", "established"), q(f"node_sockstat_TCP_tw{n}", "time wait"), q(f"node_sockstat_TCP_orphan{n}", "orphaned"), q(f"node_sockstat_sockets_used{n}", "all sockets")], 6, nodes=False),
        ts("TCP retransmits", [q(f"100 * rate(node_netstat_Tcp_RetransSegs{n}[{RATE}]) / clamp_min(rate(node_netstat_Tcp_OutSegs{n}[{RATE}]), 1)", "share of segments", interval="15s")], 6, nodes=False, unit="percent", legend=False),
        ts("Listen queue and receive drops", [q(f"rate(node_netstat_TcpExt_ListenDrops{n}[{RATE}])", "listen drops", interval="15s"), q(f"rate(node_netstat_TcpExt_TCPRcvQDrop{n}[{RATE}])", "receive queue drops", interval="15s"), q(f"rate(node_netstat_Udp_RcvbufErrors{n}[{RATE}])", "UDP buffer errors", interval="15s")], 6, nodes=False, unit="short", quiet=True),
        ts("Clock offset", [q(f"node_timex_offset_seconds{n}", "offset")], 3, nodes=False, unit="s", min_=None, legend=False, desc="Timing-sensitive tests fail on the worker past about 40 ms."),
        ts("Connection tracking", [q(f"100 * node_nf_conntrack_entries{n} / node_nf_conntrack_entries_limit{n}", "used")], 3, nodes=False, unit="percent", legend=False, desc="At 100 % the kernel starts dropping new connections, Docker's included."),
    )
    d.row("RoCE fabric")
    roce_rows(d, node_filter='node="$node"', legend_prefix="", w=6)
    d.row("Thermals")
    d.add(
        ts("Every sensor", [
            q(f"dgx_gpu_temperature_celsius{n}", "GPU / SoC"),
            q('node_thermal_zone_temp{node="$node"}', "ACPI zone {{zone}}"),
            q('node_hwmon_temp_celsius{node="$node",chip="nvme_nvme0",sensor="temp1"}', "NVMe composite"),
            q('node_hwmon_temp_celsius{node="$node",chip="nvme_nvme0",sensor=~"temp[23]"}', "NVMe {{sensor}}"),
            q('max by (chip) (node_hwmon_temp_celsius{node="$node",chip=~"0000.*|0002.*"})', "ConnectX {{chip}}"),
            q('node_hwmon_temp_celsius{node="$node",chip=~"ieee80211.*"}', "Wi-Fi"),
        ], 18, 9, nodes=False, unit="celsius", min_=None, calcs=("lastNotNull", "mean", "max"), right=True,
           desc="The ACPI zones are not labelled by the firmware, so they cannot be tied to a component. NVMe and ConnectX are."),
        ts("Time above temperature limits", [q(f"rate(spark_nvme_warning_temperature_seconds_total{n}[1h]) * 3600", "NVMe above warning, s/h"), q(f"rate(spark_nvme_critical_temperature_seconds_total{n}[1h]) * 3600", "NVMe above critical, s/h"), q(f'sum(rate(dgx_gpu_throttle_seconds_total{{node="$node",reason=~".*thermal.*"}}[1h])) * 3600', "GPU thermal throttle, s/h")], 6, 9, nodes=False, quiet=True),
    )
    d.row("Containers on this Spark")
    d.add(containers_table(where='node="$node"', h=11))
    d.add(
        ts("CPU by container", [q(f'topk(10, sum by (container) (rate(spark_container_cpu_seconds_total{{node="$node"}}[2m])))', "{{container}}")], 8, nodes=False, unit="short", calcs=("lastNotNull", "max"), desc="In cores. The ten busiest."),
        ts("Memory by container", [q(f'topk(10, max by (container) (spark_container_memory_working_set_bytes{{node="$node"}}))', "{{container}}")], 8, nodes=False, unit="bytes", calcs=("lastNotNull", "max"), desc="Working set, excluding GPU memory. The ten largest."),
        ts("Swap by container", [q(f'topk(10, max by (container) (spark_container_memory_swap_bytes{{node="$node"}} > 0))', "{{container}}")], 8, nodes=False, unit="bytes", calcs=("lastNotNull", "max"), desc="Who has been pushed out to disk."),
    )
    d.row("Databases on this Spark")
    d.add(database_table(h=7, where='{node="$node"}'))
    d.row("System")
    d.add(
        table("Services", [
            ('max by (name) (node_systemd_unit_state{node="$node",state="active"})', "Active", colored(maps=mappings({"1": ("active", OK), "0": ("no", MUTED)}), th=steps(MUTED, (1, OK)), width=80)),
            ('max by (name) (node_systemd_unit_state{node="$node",state="failed"} == 1)', "Failed", colored(TH_NONZERO_BAD, maps=mappings({"1": ("FAILED", CRIT)}), width=80)),
        ], 8, 9, labels=[("name", "Unit")], sort=("Unit", False), desc="The services this platform depends on, and any other service that has failed."),
        table("Kernel events since boot", [
            (f"max by (kind) (spark_kernel_events_total{n})", "Count", colored(steps(OK, (1, WARN)), width=70)),
            (f"max by (kind) (spark_kernel_event_last_timestamp_seconds{n}) * 1000", "Last", {"unit": "dateTimeFromNow", "width": 120}),
        ], 8, 9, labels=[("kind", "Kind")], sort=("Count", True), desc="Counted from the kernel's own log. Xid is a GPU fault, AER a PCIe fault. link_down also happens when a peer reboots."),
        ts("Kernel events", [q(f'sum by (kind) (increase(spark_kernel_events_total{n}[10m]) > 0)', "{{kind}}")], 8, 9, nodes=False, unit="short", quiet=True, desc="Per 10 minutes. Empty is the good case."),
    )
    return d


def roce_rows(d: Dashboard, node_filter: str, legend_prefix: str, w: int = 8) -> None:
    prefix = legend_prefix
    rail = 'device=~"rocep1s0f1|roceP2p1s0f1"'
    d.add(
        ts("Throughput by rail", [
            q(f'8 * rate(node_infiniband_port_data_received_bytes_total{{{node_filter},{rail}}}[{RATE}])', f"{prefix}{{{{device}}}} rx", interval="15s"),
            q(f'8 * rate(node_infiniband_port_data_transmitted_bytes_total{{{node_filter},{rail}}}[{RATE}])', f"{prefix}{{{{device}}}} tx", interval="15s"),
        ], w, 7, nodes=False, unit="bps", mirror="/ tx$/", desc="From the InfiniBand counters. RDMA traffic never touches the kernel network stack, so netdev counters read flat zero on a healthy fabric. The split between rails is uneven by design."),
        ts("Retransmits and sequence errors", [q(f'sum by (counter) (rate(spark_roce_hw_counter_total{{{node_filter},counter=~"roce_adp_retrans|roce_adp_retrans_to|local_ack_timeout_err|out_of_sequence|packet_seq_err|implied_nak_seq_err|rnr_nak_retry_err|duplicate_request"}}[{RATE}]))', f"{prefix}{{{{counter}}}}", interval="30s")], w, 7, nodes=False, unit="short", quiet=True, desc="From the NIC itself. Zero on a healthy fabric. These rise before throughput falls."),
        ts("Congestion", [q(f'sum by (counter) (rate(spark_roce_hw_counter_total{{{node_filter},counter=~"np_cnp_sent|np_ecn_marked_roce_packets|rp_cnp_handled|rp_cnp_ignored|roce_slow_restart|roce_slow_restart_cnps"}}[{RATE}]))', f"{prefix}{{{{counter}}}}", interval="30s")], w, 7, nodes=False, unit="short", quiet=True, desc="Congestion notifications. On a direct cable between two Sparks these should stay at zero."),
        ts("Errors", [q(f'sum by (counter) (rate(spark_roce_hw_counter_total{{{node_filter},counter=~"req_cqe_error|resp_cqe_error|req_transport_retries_exceeded|req_rnr_retries_exceeded|resp_local_length_error|req_remote_access_errors|resp_remote_access_errors|out_of_buffer|rx_icrc_encapsulated"}}[{RATE}]))', f"{prefix}{{{{counter}}}}", interval="30s"),
                      q(f'sum(rate(node_infiniband_port_errors_received_total{{{node_filter},{rail}}}[{RATE}]) + rate(node_infiniband_port_discards_transmitted_total{{{node_filter},{rail}}}[{RATE}]) + rate(node_infiniband_symbol_error_total{{{node_filter},{rail}}}[{RATE}]))', f"{prefix}port errors", interval="30s")], w, 7, nodes=False, unit="short", quiet=True, desc="A peer dying also makes the transport counters move once."),
    )


def containers_table(where: str = "", h: int = 14) -> dict:
    sel = f"{{{where}}}" if where else ""
    show_node = not where or "=~" in where
    labels = [("node", "Node")] if show_node else []
    labels += [("container", "Container"), ("project", "Project"), ("image", "Image"), ("status", "Status")]
    return table("Containers", [
        (f"max by (node, container, project, image, status) (spark_container_info{sel})", None),
        (f"max by (node, container) (spark_container_health_status{sel})", "Health", colored(maps=HEALTH_MAP, th=steps(MUTED), width=85)),
        (f"max by (node, container) (time() - spark_container_started_timestamp_seconds{sel} and on (node, container) spark_container_running == 1)", "Up for", {"unit": "dtdurations", "decimals": 0, "width": 90}),
        (f"max by (node, container) (spark_container_restarts_total{sel})", "Restarts", colored(steps(NEUTRAL, (1, WARN)), width=70)),
        (f"sum by (node, container) (rate(spark_container_cpu_seconds_total{sel}[2m]))", "CPU cores", {"decimals": 2, "width": 80}),
        (f"max by (node, container) (spark_container_memory_working_set_bytes{sel})", "Memory", {"unit": "bytes", "decimals": 1, "width": 85}),
        (f"max by (node, container) (spark_container_memory_swap_bytes{sel})", "Swap", {"unit": "bytes", "decimals": 1, "width": 85}),
        (f"max by (node, container) (spark_container_gpu_memory_bytes{sel})", "GPU memory", {"unit": "bytes", "decimals": 1, "width": 90}),
        (f"max by (node, container) (spark_container_pids{sel})", "Tasks", {"width": 60}),
        (f"sum by (node, container) (rate(spark_container_network_received_bytes_total{sel}[2m]))", "Net in", {"unit": "Bps", "decimals": 0, "width": 85}),
        (f"sum by (node, container) (rate(spark_container_network_sent_bytes_total{sel}[2m]))", "Net out", {"unit": "Bps", "decimals": 0, "width": 85}),
        (f"sum by (node, container) (rate(spark_container_disk_read_bytes_total{sel}[2m]))", "Disk read", {"unit": "Bps", "decimals": 0, "width": 85}),
        (f"sum by (node, container) (rate(spark_container_disk_written_bytes_total{sel}[2m]))", "Disk write", {"unit": "Bps", "decimals": 0, "width": 85}),
    ], 24, h, labels=labels, sort=("Memory", True), widths={"Node": 72, "Container": 250, "Project": 130, "Image": 170, "Status": 70},
        desc="Blank GPU memory means none. Blank network means the container shares the host's network, so its traffic cannot be told apart. Exited containers are listed with their status.")


# -------------------------------------------------------- fleet-wide pages --


def gpu_fleet() -> Dashboard:
    sel = '{node=~"$node"}'
    d = Dashboard("dgx-gpu-thermal-power", "GPU, thermal and power", refresh="10s", time_from="now-1h", variables=[node_var()],
                  description="The GB10 on every Spark: what it is doing, what it draws, how hot it runs and whether it is throttling.")
    d.add(table("Every GPU", [
        (f"max by (node, name) (dgx_gpu_power_watts{sel})", None),
        (f"max by (node) (dgx_gpu_up{sel})", "nvidia-smi", colored(maps=mappings({"1": ("answering", OK), "0": ("NOT ANSWERING", CRIT)}), th=steps(CRIT, (1, OK)), width=110)),
        (f"max by (node) (dgx_gpu_utilization_percent{sel})", "Busy", {"unit": "percent", "decimals": 0, "width": 70}),
        (f"max by (node) (dgx_gpu_power_watts{sel})", "Power", {"unit": "watt", "decimals": 1, "width": 80}),
        (f"max by (node) (dgx_gpu_temperature_celsius{sel})", "Temp", colored(TH_GPU_TEMP, unit="celsius", width=70, decimals=0)),
        (f"max by (node) (dgx_gpu_temperature_headroom_celsius{sel})", "Headroom", {"unit": "celsius", "width": 92, "decimals": 0}),
        (f"max by (node) (dgx_gpu_clock_sm_hertz{sel})", "SM clock", {"unit": "hertz", "decimals": 2, "width": 90}),
        (f"max by (node) (dgx_gpu_performance_state{sel})", "P-state", {"width": 70}),
        (f"max by (node) (dgx_gpu_memory_allocated_bytes{sel})", "GPU memory", {"unit": "bytes", "decimals": 1, "width": 100}),
        (f"max by (node) (dgx_gpu_compute_processes{sel})", "Contexts", {"width": 80}),
        (f'sum by (node) (dgx_gpu_throttle_active{{node=~"$node",reason!="sync_boost"}})', "Throttling", colored(TH_NONZERO_BAD, width=90)),
        ('max by (node, driver) (dgx_gpu_info{node=~"$node"})', None),
    ], 24, 6, labels=[("node", "Node"), ("name", "GPU"), ("driver", "Driver version")], sort=("Node", False), widths={"Node": 80, "GPU": 170, "Driver version": 120}))
    d.row("Activity")
    d.add(
        ts("GPU busy", [q(f"node:gpu_utilization_percent:avg{sel}", "{{node}}")], 8, unit="percent", max_=100, desc="The worker rank spins while it waits, so this reads high when nothing useful is happening. Read it with power."),
        ts("GPU power", [q(f"node:gpu_power_watts:sum{sel}", "{{node}}"), q("cluster:gpu_power_watts:sum", "all Sparks")], 8, unit="watt", overrides=[color_override("byName", "all Sparks", MUTED)]),
        ts("Processes using the GPU", [q(f"dgx_gpu_compute_processes{sel}", "{{node}}")], 8),
        ts("Busy by process", [q(f"sum by (node, process) (dgx_gpu_process_utilization_percent{sel})", "{{node}} {{process}}")], 12, nodes=False, unit="percent", calcs=("lastNotNull", "max"), desc="Worker_TP at 95 % with low power is the rank waiting at a collective. GNOME and remote desktop sessions use the GPU too."),
        ts("GPU memory by owner", [q(f"sum by (node, container) (spark_container_gpu_memory_bytes{sel})", "{{node}} {{container}}"), q(f"spark_host_gpu_memory_bytes{sel}", "{{node}} outside containers")], 12, nodes=False, unit="bytes", stack=True, calcs=("lastNotNull",), desc="Unified memory held by CUDA contexts, attributed through each process's cgroup."),
    )
    d.row("Thermal")
    d.add(
        ts("GPU temperature", [q(f"node:gpu_temperature_celsius:max{sel}", "{{node}}")], 8, unit="celsius", min_=None, limits=steps(NEUTRAL, (86, WARN), (90, CRIT)), desc="A shared SoC temperature: heavy CPU work moves it too. Slowdown at 86 °C, shutdown at 90 °C."),
        ts("Headroom before throttling", [q(f"dgx_gpu_temperature_headroom_celsius{sel}", "{{node}}")], 8, unit="celsius"),
        ts("Clock", [q(f"dgx_gpu_clock_sm_hertz{sel}", "{{node}}")], 8, unit="hertz"),
    )
    d.row("Throttling: the hardware's own explanation for a slowdown")
    d.add(
        state_timeline("Throttle reasons", [q('max by (node, reason) (dgx_gpu_throttle_active{node=~"$node",reason!="sync_boost"})', "{{node}} {{reason}}")], {"0": ("no", "#2b2f36"), "1": ("THROTTLED", CRIT)}, 12, 8, legend=False),
        ts("Time throttled", [q(f'sum by (node, reason) (rate(dgx_gpu_throttle_seconds_total{{node=~"$node",reason!="sync_boost"}}[{RATE}]))', "{{node}} {{reason}}", interval="30s")], 12, 8, nodes=False, unit="percentunit", quiet=True, desc="Share of time spent throttled, by reason."),
    )
    d.row("Energy: GPU rail only, not wall power")
    d.add(
        stat("Last hour", [q(f'sum by (node) (avg_over_time(dgx_gpu_power_watts{sel}[1h]))', "{{node}}", instant=True)], 6, 3, unit="watth", decimals=0, text="value_and_name", desc="Average watts over one hour is watt-hours."),
        stat("Last 24 hours", [q(f'24 * sum by (node) (avg_over_time(dgx_gpu_power_watts{sel}[24h]))', "{{node}}", instant=True)], 6, 3, unit="watth", decimals=0, text="value_and_name"),
        stat("Last 7 days", [q(f'168 * sum by (node) (avg_over_time(dgx_gpu_power_watts{sel}[7d]))', "{{node}}", instant=True)], 6, 3, unit="watth", decimals=0, text="value_and_name"),
        stat("Exporter errors 24h", [q(f'sum by (node) (increase(dgx_gpu_scrape_errors_total{sel}[24h]))', "{{node}}", instant=True)], 6, 3, decimals=0, text="value_and_name", th=TH_NONZERO_WARN, desc="nvidia-smi calls that failed."),
    )
    return d


def interconnect() -> Dashboard:
    d = Dashboard("dgx-interconnect", "RoCE fabric", refresh="10s", variables=[node_var()],
                  description="The links carrying the model's tensor-parallel traffic between Sparks.")
    d.add(text("Every panel reads **InfiniBand and NIC counters**. RDMA traffic never touches the kernel network stack, so ordinary network counters read flat zero on a healthy fabric. "
               "Only `rocep1s0f1` and `roceP2p1s0f1` carry a link: the two PCIe rails of one 200 Gb/s QSFP port. A single rail tops out near 109 Gb/s by design, so there is no percent-of-capacity panel here.", h=2))
    d.add(table("Ports", [
        ('max by (node, device) (node_infiniband_state_id{node=~"$node",device=~"rocep1s0f1|roceP2p1s0f1"})', "State", colored(maps=mappings({"4": ("ACTIVE", OK), "1": ("DOWN", CRIT), "2": ("INIT", WARN), "3": ("ARMED", WARN)}), th=steps(CRIT, (4, OK)), width=80)),
        ('8 * max by (node, device) (rate(node_infiniband_port_data_received_bytes_total{node=~"$node",device=~"rocep1s0f1|roceP2p1s0f1"}[2m]))', "Received", {"unit": "bps", "decimals": 1}),
        ('8 * max by (node, device) (rate(node_infiniband_port_data_transmitted_bytes_total{node=~"$node",device=~"rocep1s0f1|roceP2p1s0f1"}[2m]))', "Sent", {"unit": "bps", "decimals": 1}),
        ('sum by (node, device) (rate(spark_roce_hw_counter_total{node=~"$node",counter=~"roce_adp_retrans|local_ack_timeout_err|out_of_sequence|packet_seq_err|implied_nak_seq_err|rnr_nak_retry_err"}[5m]))', "Retransmits/s", colored(steps(OK, (0.1, WARN), (1, CRIT)), decimals=2)),
        ('sum by (node, device) (rate(spark_roce_hw_counter_total{node=~"$node",counter=~"np_cnp_sent|rp_cnp_handled"}[5m]))', "Congestion/s", colored(steps(OK, (1, WARN)), decimals=2)),
        ('max by (node, device) (increase(spark_roce_hw_counter_total{node=~"$node",counter=~"req_transport_retries_exceeded|req_rnr_retries_exceeded"}[1h]))', "Gave up 1h", colored(TH_NONZERO_BAD, decimals=0)),
    ], 24, 6, labels=[("node", "Node"), ("device", "Port")], sort=("Node", False)))
    d.row("Every Spark")
    d.add(
        ts("Throughput", [q('node:roce_receive_bytes_per_second:sum{node=~"$node"}', "{{node}} rx"), q('node:roce_transmit_bytes_per_second:sum{node=~"$node"}', "{{node}} tx")], 12, unit="Bps", mirror="/ tx$/", desc="Received above the line, sent below."),
        ts("Port temperature", [q('max by (node) (node_hwmon_temp_celsius{node=~"$node",chip=~"0000.*|0002.*"})', "{{node}}")], 12, unit="celsius", min_=None),
    )
    d.row("$node")
    d.panels[-1]["repeat"] = "node"
    roce_rows(d, node_filter='node="$node"', legend_prefix="", w=6)
    return d


def fleet_hosts() -> Dashboard:
    sel = '{node=~"$node"}'
    d = Dashboard("fleet-hosts", "Hosts — side by side", refresh="30s", variables=[node_var()],
                  description="The same reading from every Spark on one chart, to spot the one that behaves differently.")
    d.add(fleet_table())
    d.row("CPU")
    d.add(
        ts("CPU busy", [q(f"node:cpu_utilization_percent:avg{sel}", "{{node}}")], 8, unit="percent", max_=100),
        ts("Load per core", [q(f'node_load1{sel} / on (node) group_left () count by (node) (node_cpu_seconds_total{{mode="idle"}})', "{{node}}")], 8, desc="Above 1 means tasks are queueing for a CPU."),
        ts("Waiting for CPU", [q(f"100 * rate(node_pressure_cpu_waiting_seconds_total{sel}[{RATE}])", "{{node}}", interval="15s")], 8, unit="percent"),
    )
    d.row("Memory")
    d.add(
        ts("Unified memory used", [q(f"node:memory_used_percent:current{sel}", "{{node}}")], 8, unit="percent", max_=100),
        ts("Available", [q(f"node_memory_MemAvailable_bytes{sel}", "{{node}}")], 8, unit="bytes"),
        ts("Held by GPU contexts", [q(f"dgx_gpu_memory_allocated_bytes{sel}", "{{node}}")], 8, unit="bytes"),
        ts("Swap used", [q(f"node:swap_used_bytes:current{sel}", "{{node}}")], 8, unit="bytes"),
        ts("Swap traffic", [q(f"node:swap_pages_per_second:rate5m{sel}", "{{node}}")], 8, desc="Pages per second."),
        ts("Stalled on memory", [q(f"100 * rate(node_pressure_memory_stalled_seconds_total{sel}[{RATE}])", "{{node}}", interval="15s")], 8, unit="percent", desc="PSI: share of time every task was blocked on memory."),
    )
    d.row("Storage")
    d.add(
        ts("Disk used", [q(f"node:disk_used_percent:root{sel}", "{{node}}")], 6, unit="percent", max_=100),
        ts("Disk throughput", [q(f'sum by (node) (rate(node_disk_read_bytes_total{{node=~"$node",device=~"nvme.*"}}[{RATE}]))', "{{node}} read", interval="15s"), q(f'sum by (node) (rate(node_disk_written_bytes_total{{node=~"$node",device=~"nvme.*"}}[{RATE}]))', "{{node}} write", interval="15s")], 6, unit="Bps", mirror="/ write$/"),
        ts("Stalled on disk", [q(f"100 * rate(node_pressure_io_stalled_seconds_total{sel}[{RATE}])", "{{node}}", interval="15s")], 6, unit="percent"),
        ts("SSD temperature", [q(f"spark_nvme_temperature_celsius{sel}", "{{node}}")], 6, unit="celsius", min_=None),
    )
    d.row("Network and time")
    d.add(
        ts("Management LAN", [q(f"node:ethernet_receive_bytes_per_second:sum{sel}", "{{node}} rx"), q(f"node:ethernet_transmit_bytes_per_second:sum{sel}", "{{node}} tx")], 8, unit="Bps", mirror="/ tx$/"),
        ts("TCP retransmits", [q(f"100 * rate(node_netstat_Tcp_RetransSegs{sel}[{RATE}]) / clamp_min(rate(node_netstat_Tcp_OutSegs{sel}[{RATE}]), 1)", "{{node}}", interval="15s")], 8, unit="percent"),
        ts("Clock offset", [q(f"node_timex_offset_seconds{sel}", "{{node}}")], 8, unit="s", min_=None, desc="Timing-sensitive tests fail on the worker past about 40 ms."),
    )
    return d


def containers() -> Dashboard:
    where = 'node=~"$node",project=~"$project"'
    d = Dashboard("dgx-host-containers", "Containers", refresh="30s",
                  variables=[node_var(), label_var("project", "Project", "label_values(spark_container_info, project)")],
                  description="Every Docker container on every Spark, with its state, health and what it costs.")
    d.add(
        stat("Running", [q(f'sum(spark_containers{{node=~"$node",status="running"}})', instant=True)], 4, 3, size=26, no_value="no data"),
        stat("Unhealthy", [q(f'count(spark_container_health_status{{{where}}} == 0) or vector(0)', instant=True)], 4, 3, th=TH_NONZERO_BAD, size=26),
        stat("Should be running", [q(f'count((spark_container_expected_running{{{where}}} == 1) and on (node, container) (spark_container_running == 0)) or vector(0)', instant=True)], 4, 3, th=TH_NONZERO_BAD, size=26, desc="Stopped although the restart policy says otherwise, and not stopped by hand."),
        stat("Restarted in 1h", [q(f'count(changes(spark_container_started_timestamp_seconds{{{where}}}[1h]) > 0) or vector(0)', instant=True)], 4, 3, th=TH_NONZERO_WARN, size=26),
        stat("OOM kills in 24h", [q(f'sum(increase(spark_container_oom_kills_total{{{where}}}[24h])) or vector(0)', instant=True)], 4, 3, th=TH_NONZERO_BAD, decimals=0, size=26),
        stat("Stopped", [q(f'sum(spark_containers{{node=~"$node",status="exited"}})', instant=True)], 4, 3, th=steps(MUTED), size=26, desc="Leftovers nobody expects to be up."),
    )
    d.add(containers_table(where=where, h=20))
    d.row("What they use")
    d.add(
        ts("CPU", [q(f'topk(12, sum by (container) (rate(spark_container_cpu_seconds_total{{{where}}}[2m])))', "{{container}}")], 12, 8, nodes=False, calcs=("lastNotNull", "max"), desc="In cores. The twelve busiest."),
        ts("Memory", [q(f'topk(12, max by (container) (spark_container_memory_working_set_bytes{{{where}}}))', "{{container}}")], 12, 8, nodes=False, unit="bytes", calcs=("lastNotNull", "max"), desc="Working set. GPU memory is separate on this hardware."),
        ts("GPU memory", [q(f'sum by (node, container) (spark_container_gpu_memory_bytes{{{where}}})', "{{node}} {{container}}")], 12, 8, nodes=False, unit="bytes", stack=True, calcs=("lastNotNull",)),
        ts("Swap", [q(f'topk(12, max by (container) (spark_container_memory_swap_bytes{{{where}}} > 0))', "{{container}}")], 12, 8, nodes=False, unit="bytes", calcs=("lastNotNull", "max")),
        ts("Network", [q(f'topk(8, sum by (container) (rate(spark_container_network_received_bytes_total{{{where}}}[2m])))', "{{container}} rx"), q(f'topk(8, sum by (container) (rate(spark_container_network_sent_bytes_total{{{where}}}[2m])))', "{{container}} tx")], 12, 8, nodes=False, unit="Bps", mirror="/ tx$/", calcs=("lastNotNull", "max")),
        ts("Disk", [q(f'topk(8, sum by (container) (rate(spark_container_disk_read_bytes_total{{{where}}}[2m])))', "{{container}} read"), q(f'topk(8, sum by (container) (rate(spark_container_disk_written_bytes_total{{{where}}}[2m])))', "{{container}} write")], 12, 8, nodes=False, unit="Bps", mirror="/ write$/", calcs=("lastNotNull", "max")),
    )
    d.row("Restarts and exits")
    d.add(table("What stopped or restarted", [
        (f"sum by (node, container, project) (changes(spark_container_started_timestamp_seconds{{{where}}}[24h]) > 0)", "Starts in 24h", colored(steps(NEUTRAL, (3, WARN)), width=110)),
        (f"max by (node, container, project) (spark_container_started_timestamp_seconds{{{where}}} * 1000 and on (node, container) (changes(spark_container_started_timestamp_seconds{{{where}}}[24h]) > 0))", "Last start", {"unit": "dateTimeFromNow", "width": 120}),
        (f"max by (node, container, project) (spark_container_exit_code{{{where}}} and on (node, container) (changes(spark_container_started_timestamp_seconds{{{where}}}[24h]) > 0))", "Last exit code", colored(steps(NEUTRAL, (1, WARN)), width=110)),
        (f"max by (node, container, project) (spark_container_restarts_total{{{where}}} and on (node, container) (changes(spark_container_started_timestamp_seconds{{{where}}}[24h]) > 0))", "Automatic restarts", {"width": 130}),
    ], 24, 9, labels=[("node", "Node"), ("container", "Container"), ("project", "Project")], sort=("Starts in 24h", True),
        desc="Every container started in the last 24 hours, whether by a person, a deploy or a crash. Automatic restarts is Docker's lifetime count under the restart policy."))
    return d


def databases() -> Dashboard:
    where = 'node=~"$node",engine=~"$engine",project=~"$project"'
    sel = "{" + where + "}"
    d = Dashboard("fleet-databases", "Databases", refresh="30s",
                  variables=[node_var(), label_var("engine", "Engine", "label_values(spark_db_info, engine)"),
                             label_var("project", "Project", "label_values(spark_db_info, project)")],
                  description="Every database on every Spark, found automatically: PostgreSQL, Redis, MySQL, MongoDB, ClickHouse, vector stores and others.")
    d.add(
        stat("Databases", [q(f"count(spark_db_up{sel})", instant=True)], 3, 3, size=26, no_value="none found"),
        stat("Down", [q(f"count(spark_db_up{sel} == 0) or vector(0)", instant=True)], 3, 3, th=TH_NONZERO_BAD, size=26),
        stat("Connections", [q(f"sum(spark_db_connections{sel})", instant=True)], 3, 3, size=26, no_value="no data"),
        stat("On disk", [q(f"sum(spark_db_data_bytes{sel})", instant=True)], 3, 3, unit="bytes", decimals=1, size=26, no_value="no data"),
        stat("Memory", [q(f"sum(spark_container_memory_working_set_bytes and on (node, container) spark_db_up{sel})", instant=True)], 3, 3, unit="bytes", decimals=1, size=26, no_value="no data"),
        stat("CPU", [q(f"sum(rate(spark_container_cpu_seconds_total[2m]) and on (node, container) spark_db_up{sel})", instant=True)], 3, 3, decimals=2, unit="short", size=26, no_value="no data", desc="In cores."),
        stat("Long txns", [q(f"count(spark_db_longest_transaction_seconds{sel} > 300) or vector(0)", instant=True)], 3, 3, th=TH_NONZERO_WARN, size=26, desc="Open more than 5 minutes. Only measured where a login is allowed."),
        stat("Leftovers", [q("sum(spark_db_stopped_instances)", instant=True)], 3, 3, th=steps(MUTED), size=26, no_value="0", desc="Stopped database containers with no restart policy, such as old test databases. They are not outages, and they still hold disk."),
    )
    d.add(database_table(h=11, where=sel))
    d.row("Any database, with no login")
    d.add(
        state_timeline("Up over time", [q(f"max by (node, container) (spark_db_up{sel})", "{{node}} {{container}}")], {"1": ("up", OK), "0": ("DOWN", CRIT)}, 24, 10, legend=False, row_height=0.8,
                       desc="From a handshake in the engine's own protocol: PostgreSQL answers an SSL request, Redis a PING, MySQL its greeting. A gap means the Spark was not reporting."),
        ts("Connections", [q(f"sum by (node, container) (spark_db_connections{sel})", "{{node}} {{container}}")], 8, nodes=False, calcs=("lastNotNull", "max"), desc="PostgreSQL: read from each backend's process title, so no login is needed."),
        ts("Active connections", [q(f'sum by (node, container) (spark_db_connections{{{where},state=~"active|idle_in_transaction"}})', "{{node}} {{container}}")], 8, nodes=False, calcs=("lastNotNull", "max")),
        ts("Reply time", [q(f"1000 * max by (node, container) (spark_db_probe_seconds{sel})", "{{node}} {{container}}")], 8, nodes=False, unit="ms", calcs=("lastNotNull", "max")),
        ts("CPU", [q(f"sum by (node, container) (rate(spark_container_cpu_seconds_total[2m]) and on (node, container) spark_db_up{sel})", "{{node}} {{container}}")], 8, nodes=False, calcs=("lastNotNull", "max"), desc="In cores."),
        ts("Memory", [q(f"max by (node, container) (spark_container_memory_working_set_bytes and on (node, container) spark_db_up{sel})", "{{node}} {{container}}")], 8, nodes=False, unit="bytes", calcs=("lastNotNull", "max")),
        ts("Data on disk", [q(f"max by (node, container) (spark_db_data_bytes{sel})", "{{node}} {{container}}")], 8, nodes=False, unit="bytes", calcs=("lastNotNull",), desc="Disk blocks in the database's named volumes, measured every 5 minutes."),
        ts("Disk reads", [q(f"sum by (node, container) (rate(spark_container_disk_read_bytes_total[2m]) and on (node, container) spark_db_up{sel})", "{{node}} {{container}}")], 12, nodes=False, unit="Bps", calcs=("lastNotNull", "max")),
        ts("Disk writes", [q(f"sum by (node, container) (rate(spark_container_disk_written_bytes_total[2m]) and on (node, container) spark_db_up{sel})", "{{node}} {{container}}")], 12, nodes=False, unit="Bps", calcs=("lastNotNull", "max")),
    )
    d.row("Detailed statistics: where a login is allowed, or the engine needs none")
    d.add(
        ts("Transactions per second", [q(f'sum by (node, container) (rate(spark_db_transactions_total{{{where},outcome="commit"}}[2m]))', "{{node}} {{container}} commit"), q(f'sum by (node, container) (rate(spark_db_transactions_total{{{where},outcome="rollback"}}[2m]))', "{{node}} {{container}} rollback")], 8, nodes=False, calcs=("lastNotNull", "max")),
        ts("Cache hit ratio", [q(f'100 * sum by (node, container) (rate(spark_db_cache_reads_total{{{where},source="hit"}}[5m])) / sum by (node, container) (rate(spark_db_cache_reads_total{sel}[5m]) > 0)', "{{node}} {{container}}")], 8, nodes=False, unit="percent", max_=100, calcs=("lastNotNull", "min")),
        ts("Rows handled", [q(f"sum by (container, op) (rate(spark_db_tuples_total{sel}[2m]))", "{{container}} {{op}}")], 8, nodes=False, calcs=("lastNotNull", "max")),
        ts("Longest transaction", [q(f"max by (node, container) (spark_db_longest_transaction_seconds{sel})", "{{node}} {{container}}")], 8, nodes=False, unit="s", calcs=("lastNotNull", "max"), desc="A long one stops vacuum and holds locks."),
        ts("Sessions waiting on locks", [q(f"max by (node, container) (spark_db_lock_waits{sel})", "{{node}} {{container}}")], 8, nodes=False, calcs=("lastNotNull", "max"), quiet=True),
        ts("Deadlocks per hour", [q(f"sum by (node, container) (increase(spark_db_deadlocks_total{sel}[1h]))", "{{node}} {{container}}")], 8, nodes=False, quiet=True),
        ts("Spilled to temp files", [q(f"sum by (node, container) (rate(spark_db_temp_bytes_total{sel}[5m]))", "{{node}} {{container}}")], 8, nodes=False, unit="Bps", calcs=("lastNotNull", "max"), desc="Queries that ran out of work memory."),
        ts("WAL written", [q(f"sum by (node, container) (rate(spark_db_wal_bytes_total{sel}[5m]))", "{{node}} {{container}}")], 8, nodes=False, unit="Bps", calcs=("lastNotNull", "max")),
        ts("Largest databases", [q(f"topk(12, max by (container, datname) (spark_db_size_bytes{sel}))", "{{container}} {{datname}}")], 8, nodes=False, unit="bytes", calcs=("lastNotNull",)),
        ts("Redis memory", [q(f"max by (node, container) (spark_db_memory_used_bytes{sel})", "{{node}} {{container}} used"), q(f"max by (node, container) (spark_db_memory_limit_bytes{sel})", "{{node}} {{container}} limit")], 8, nodes=False, unit="bytes", calcs=("lastNotNull", "max")),
        ts("Redis commands per second", [q(f"sum by (node, container) (rate(spark_db_commands_total{sel}[2m]))", "{{node}} {{container}}")], 8, nodes=False, calcs=("lastNotNull", "max")),
        ts("Redis keys", [q(f"max by (node, container) (spark_db_keys{sel})", "{{node}} {{container}}")], 8, nodes=False, calcs=("lastNotNull", "max")),
    )
    d.row("File-based stores: DuckDB, LanceDB, Parquet and everything else under the data volume")
    d.add(
        table("Stores", [
            ("max by (store, kind) (techsara_store_present)", "Present", colored(maps=mappings({"1": ("yes", OK), "0": ("MISSING", CRIT)}), th=steps(CRIT, (1, OK)), width=80)),
            ("max by (store, kind) (techsara_store_size_bytes)", "Size", {"unit": "bytes", "decimals": 1, "width": 100}),
            ("max by (store, kind) (techsara_store_files)", "Files", {"width": 90}),
            ("max by (store, kind) (techsara_store_age_seconds)", "Last changed", {"unit": "dtdurations", "decimals": 0, "width": 120}),
        ], 12, 14, labels=[("store", "Store"), ("kind", "Kind")], sort=("Size", True),
            desc="These speak no wire protocol, so they are measured from disk: size, file count and age. The warehouse snapshot's age is the one to watch. Anything else that appears at the top of the data volume is listed by itself."),
        ts("Store size", [q("max by (store) (techsara_store_size_bytes)", "{{store}}")], 12, 14, nodes=False, unit="bytes", calcs=("lastNotNull",), right=True),
    )
    return d


def alerts_slo() -> Dashboard:
    d = Dashboard("fleet-alerts", "Alerts and SLOs", refresh="30s", time_from="now-24h",
                  description="What is firing, what fired, and how available the service has been.")
    d.add(
        stat("Critical", [q('count(ALERTS{alertstate="firing",severity="critical"}) or vector(0)', instant=True)], 3, 3, th=TH_NONZERO_BAD, size=30),
        stat("Warning", [q('count(ALERTS{alertstate="firing",severity="warning"}) or vector(0)', instant=True)], 3, 3, th=TH_NONZERO_WARN, size=30),
        stat("Info", [q('count(ALERTS{alertstate="firing",severity="info"}) or vector(0)', instant=True)], 3, 3, th=steps(MUTED, (1, INFO)), size=30),
        stat("Pending", [q('count(ALERTS{alertstate="pending"}) or vector(0)', instant=True)], 3, 3, th=steps(MUTED), size=30, desc="Conditions that are true now but have not lasted long enough to fire."),
        stat("Engine 24h", [q("100 * avg_over_time((clamp_max(cluster:vllm_service_state:code == bool 2, 1) + clamp_max(cluster:vllm_service_state:code == bool 3, 1) + clamp_max(cluster:vllm_service_state:code == bool 4, 1))[24h:1m])", instant=True)], 3, 3, unit="percent", decimals=2, th=steps(CRIT, (99, WARN), (99.9, OK)), size=22, no_value="no data",
             desc="Share of minutes the engine could serve people: READY, BUSY or DEGRADED."),
        stat("Engine 7d", [q("100 * avg_over_time((clamp_max(cluster:vllm_service_state:code == bool 2, 1) + clamp_max(cluster:vllm_service_state:code == bool 3, 1) + clamp_max(cluster:vllm_service_state:code == bool 4, 1))[7d:5m])", instant=True)], 3, 3, unit="percent", decimals=2, th=steps(CRIT, (99, WARN), (99.9, OK)), size=22, no_value="no data"),
        stat("Canary 24h", [q("100 * avg_over_time(techsara_vllm_synthetic_success[24h])", instant=True)], 3, 3, unit="percent", decimals=2, th=steps(CRIT, (99, WARN), (99.9, OK)), size=22, no_value="no data"),
        stat("Recoveries 7d", [q('increase(techsara_vllm_recovery_attempts_total{outcome="started"}[7d])', instant=True)], 3, 3, decimals=0, th=steps(OK, (1, WARN), (5, CRIT)), size=22, no_value="no data"),
    )
    d.row("Firing now")
    d.add(table("Firing", [
        ('max by (alertname, severity, node, container, device, service, kind, mountpoint) (ALERTS_FOR_STATE and ignoring (alertstate) ALERTS{alertstate="firing"}) * 1000', "Since", {"unit": "dateTimeFromNow", "width": 130})],
        24, 7, labels=[("severity", "Severity"), ("alertname", "Alert"), ("node", "Node"), ("container", "Container"), ("device", "Device"), ("service", "Service"), ("kind", "Kind"), ("mountpoint", "Mount")],
        sort=("Since", True), desc="Nothing is sent anywhere, by the owner's decision: this page and Prometheus' /alerts are where alerts live. Empty means nothing is firing.", overrides_severity=True))
    d.row("History")
    d.add(
        state_timeline("Alerts over time", [q('max by (alertname, node) (ALERTS{alertstate="firing"})', "{{alertname}} {{node}}")], {"1": ("firing", SERIOUS)}, 24, 12, legend=False, row_height=0.8, desc="When each alert fired during the selected time range."),
        state_timeline("Engine state", [q("cluster:vllm_service_state:code", "engine")], ENGINE_STATES, 24, 3, legend=False),
    )
    d.row("Availability")
    d.add(
        table("Sparks", [
            ('100 * max by (node) (avg_over_time(up{job="node"}[24h]))', "24 h", colored(steps(CRIT, (99, WARN), (99.9, OK)), unit="percent", decimals=2)),
            ('100 * max by (node) (avg_over_time(up{job="node"}[7d]))', "7 days", colored(steps(CRIT, (99, WARN), (99.9, OK)), unit="percent", decimals=2)),
            ('max by (node) (changes(node_boot_time_seconds[7d]))', "Reboots 7d", colored(steps(OK, (1, WARN)), decimals=0)),
            ('max by (node) (increase(spark_nvme_unsafe_shutdowns_total[7d]))', "Power cuts 7d", colored(steps(OK, (1, CRIT)), decimals=0)),
        ], 10, 8, labels=[("node", "Node")], sort=("Node", False), desc="Read from Prometheus, so a Prometheus outage counts as missing rather than as down. History goes back as far as retention."),
        table("Services", [
            ('100 * max by (service) (avg_over_time(probe_success[24h]))', "24 h", colored(steps(CRIT, (99, WARN), (99.9, OK)), unit="percent", decimals=2)),
            ('100 * max by (service) (avg_over_time(probe_success[7d]))', "7 days", colored(steps(CRIT, (99, WARN), (99.9, OK)), unit="percent", decimals=2)),
        ], 6, 8, labels=[("service", "Service")], sort=("Service", False)),
        table("Databases", [
            ('100 * max by (node, container) (avg_over_time(spark_db_up[24h]))', "24 h", colored(steps(CRIT, (99, WARN), (99.9, OK)), unit="percent", decimals=2, width=80)),
            ('100 * max by (node, container) (avg_over_time(spark_db_up[7d]))', "7 days", colored(steps(CRIT, (99, WARN), (99.9, OK)), unit="percent", decimals=2, width=80)),
        ], 8, 8, labels=[("node", "Node"), ("container", "Database")], sort=("24 h", False), widths={"Node": 70}),
    )
    return d


def monitoring_health() -> Dashboard:
    d = Dashboard("fleet-monitoring", "Monitoring itself", refresh="30s",
                  description="Is the monitoring telling the truth, what does it cost, and what hardware is in the fleet.")
    d.add(
        stat("Targets up", [q("sum(up)", "up", instant=True), q("count(up) - sum(up)", "down", instant=True)], 4, 3, text="value_and_name", overrides=[field_override("byFrameRefID", "B", thresholds=TH_NONZERO_BAD)]),
        stat("Series", [q('prometheus_tsdb_head_series{job="prometheus"}', "")], 4, 3, spark=True, decimals=0, unit="short", desc="More series means more of the head's memory."),
        stat("Samples/s", [q('sum(rate(prometheus_tsdb_head_samples_appended_total{job="prometheus"}[5m]))', "")], 4, 3, spark=True, decimals=0, unit="short"),
        stat("History kept", [q('(time() - prometheus_tsdb_lowest_timestamp_seconds{job="prometheus"})', "", instant=True)], 4, 3, unit="dtdurations", decimals=0),
        stat("Disk used", [q('prometheus_tsdb_storage_blocks_bytes{job="prometheus"} + prometheus_tsdb_wal_storage_size_bytes{job="prometheus"}', "", instant=True)], 4, 3, unit="bytes", decimals=1),
        stat("Prometheus memory", [q('process_resident_memory_bytes{job="prometheus"}', "")], 4, 3, spark=True, unit="bytes", decimals=0),
    )
    d.row("Fleet membership")
    d.add(table("Sparks known to discovery", [
        ("max by (member, hostname, address, member_role, state) (spark_fleet_member)", "Known", {"width": 70}),
        ('max by (member) (label_replace(up{job="node"}, "member", "$1", "node", "(.+)"))', "Host metrics", colored(maps=UP_MAP, th=steps(CRIT, (1, OK)), width=100)),
        ('max by (member) (label_replace(up{job="dgx-gpu"}, "member", "$1", "node", "(.+)"))', "GPU metrics", colored(maps=UP_MAP, th=steps(CRIT, (1, OK)), width=100)),
        ('max by (member) (label_replace(time() - spark_probe_last_run_timestamp_seconds, "member", "$1", "node", "(.+)"))', "Probe age", colored(steps(OK, (60, WARN), (120, CRIT)), unit="s", decimals=0, width=90)),
        ('max by (member) (label_replace(time() - spark_local_last_run_timestamp_seconds, "member", "$1", "node", "(.+)"))', "Local age", colored(steps(OK, (60, WARN), (120, CRIT)), unit="s", decimals=0, width=90)),
        ('max by (member) (label_replace(node_textfile_scrape_error, "member", "$1", "node", "(.+)"))', "Textfile error", colored(TH_NONZERO_BAD, width=100)),
    ], 24, 6, labels=[("member", "Node"), ("hostname", "Host"), ("address", "Address"), ("member_role", "Role"), ("state", "State")], sort=("Node", False),
        desc="A new Spark is found by mDNS within a minute. `needs_ssh_key` means this head cannot log in yet: run ssh-copy-id, and enrolment finishes by itself. A Spark that dies stays here as `down`."))
    d.row("Scrapes")
    d.add(
        table("Targets", [
            ("max by (job, instance, node, service) (up)", "Up", colored(maps=UP_MAP, th=steps(CRIT, (1, OK)), width=70)),
            ("1000 * max by (job, instance, node, service) (scrape_duration_seconds)", "Scrape time", colored(steps(NEUTRAL, (1000, WARN), (3000, CRIT)), unit="ms", decimals=0, width=100)),
            ("max by (job, instance, node, service) (scrape_samples_scraped)", "Samples", {"width": 90}),
        ], 12, 12, labels=[("job", "Job"), ("node", "Node"), ("service", "Service"), ("instance", "Address")], sort=("Samples", True), widths={"Job": 130, "Node": 70, "Service": 90}),
        ts("Series by job", [q("sum by (job) (scrape_samples_post_metric_relabeling)", "{{job}}")], 12, 12, nodes=False, calcs=("lastNotNull",), stack=True),
    )
    d.row("Spark probe")
    d.add(
        state_timeline("Collectors", [q("max by (node, collector) (spark_probe_collector_success)", "{{node}} {{collector}}")], {"1": ("ok", OK), "0": ("FAILING", CRIT)}, 12, 11, legend=False, row_height=0.8,
                       desc="A failing collector means its panels are empty, which does not mean zero."),
        ts("Collector time", [q("max by (node, collector) (spark_probe_collector_seconds)", "{{node}} {{collector}}")], 12, 11, nodes=False, unit="s", calcs=("lastNotNull", "max")),
    )
    d.row("What monitoring costs")
    mon = 'container=~".*(prometheus|grafana|exporter|spark-probe|spark-local|cloudflared-grafana).*",container!~"interview.*"'
    d.add(
        ts("CPU", [q(f'sum by (node, container) (rate(spark_container_cpu_seconds_total{{{mon}}}[2m]))', "{{node}} {{container}}")], 12, 8, nodes=False, calcs=("lastNotNull", "max"), desc="In cores. Monitoring must stay small next to the model."),
        ts("Memory", [q(f'max by (node, container) (spark_container_memory_working_set_bytes{{{mon}}})', "{{node}} {{container}}")], 12, 8, nodes=False, unit="bytes", calcs=("lastNotNull", "max")),
    )
    d.row("Rules and queries")
    d.add(
        ts("Rule group duration", [q('topk(8, max by (rule_group) (prometheus_rule_group_last_duration_seconds))', "{{rule_group}}")], 8, nodes=False, unit="s", calcs=("lastNotNull", "max")),
        ts("Rule failures", [q('sum(rate(prometheus_rule_evaluation_failures_total[5m]))', "failures/s")], 8, nodes=False, legend=False, quiet=True, desc="Flat at zero. A rule that errors is otherwise silent."),
        ts("Query latency", [q('max by (slice) (prometheus_engine_query_duration_seconds{quantile="0.9"})', "{{slice}} p90")], 8, nodes=False, unit="s"),
    )
    d.row("Inventory")
    d.add(table("Hardware and software", [
        ("max by (node, hostname, product, dgx_os, serial) (spark_node_info)", None),
        ("max by (node, release) (node_uname_info)", None),
        ("max by (node, driver) (dgx_gpu_info)", None),
        ('max by (node, model, ssd_serial, firmware_revision) (label_replace(node_nvme_info, "ssd_serial", "$1", "serial", "(.*)"))', None),
        ("max by (node, version) (spark_docker_info)", None),
        ("max by (node) (spark_docker_images)", "Images", {"width": 80}),
        ("max by (node) (spark_nvme_power_on_seconds_total)", "Powered on", {"unit": "dtdurations", "decimals": 0, "width": 110}),
    ], 24, 6, labels=[("node", "Node"), ("hostname", "Host"), ("product", "Product"), ("serial", "Serial"), ("dgx_os", "DGX OS"), ("release", "Kernel"), ("driver", "GPU driver"), ("model", "SSD"), ("ssd_serial", "SSD serial"), ("firmware_revision", "SSD firmware"), ("version", "Docker")], sort=("Node", False)))
    return d


# ------------------------------------------------------------------------ main --


def finish(dashboard: Dashboard) -> Dashboard:
    """Options that need the finished panel: severity colours and node links."""
    severity = mappings({"critical": ("critical", CRIT), "warning": ("warning", WARN), "info": ("info", INFO)})
    for item in dashboard.panels:
        if item.pop("overrides_severity", False):
            item["fieldConfig"]["overrides"].append({
                "matcher": {"id": "byName", "options": "Severity"},
                "properties": [{"id": "mappings", "value": severity},
                               {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                               {"id": "custom.width", "value": 90}],
            })
        if item.pop("node_link", False):
            item["fieldConfig"]["overrides"].append({
                "matcher": {"id": "byName", "options": "Node"},
                "properties": [{"id": "links", "value": NODE_LINK}, {"id": "custom.width", "value": 92}],
            })
    return dashboard


BUILDERS = {
    "dgx-cluster-overview.json": command_center,
    "spark-node.json": spark_node,
    "dgx-gpu-thermal-power.json": gpu_fleet,
    "dgx-interconnect.json": interconnect,
    "fleet-hosts.json": fleet_hosts,
    "dgx-host-containers.json": containers,
    "fleet-databases.json": databases,
    "fleet-alerts.json": alerts_slo,
    "fleet-monitoring.json": monitoring_health,
}


def add_nav_to_handwritten(check: bool) -> list[str]:
    """Pages that are not generated get the same navigation bar, and nothing else."""
    stale = []
    for path in sorted(OUT.glob("*.json")):
        if path.name in BUILDERS:
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("links") != nav_links():
            doc["links"] = nav_links()
            if check:
                stale.append(path.name)
            else:
                path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a file differs from what would be generated")
    args = parser.parse_args()
    stale = []
    for name, build in BUILDERS.items():
        body = finish(build()).to_json()
        path = OUT / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != body:
                stale.append(name)
        else:
            path.write_text(body, encoding="utf-8")
    stale += add_nav_to_handwritten(args.check)
    if stale:
        print("out of date, run monitoring/grafana/generate_dashboards.py:", ", ".join(stale), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
