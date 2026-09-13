#!/usr/bin/env python3
"""Prove the Developer API dashboard and alert rules against a running
Prometheus — read-only; the standard library plus PyYAML to read the rule file
(2026-09-13).

For every PromQL expression in the dashboard and the rule file (and every
`query "..."` inside an annotation template) this:

  1. asks Prometheus to PARSE it (GET /api/v1/parse_query), so a dashboard
     expression gets the same syntax check promtool gives the rules;
  2. collects the metric names from the parse tree;
  3. checks each name against metrics-contract.json and against the names
     Prometheus holds right now (GET /api/v1/label/__name__/values):
       live          must exist                               else FAIL
       event_driven  may be absent (registered on first use)  reported
       pending       absent is expected; present = promote it  WARN
       proposed      absent is expected; present = promote it  WARN
       not declared                                            FAIL
  4. checks every live selector, labels included, matches series in the
     last hour (GET /api/v1/series), so a wrong job or relname is caught;
  5. compares the parse tree's names with the offline extractor the unit
     tests use, so the tests cannot drift from what Prometheus parses.

Only metadata and parse endpoints are called — no query of sample values,
no range query, no load on the engines.

    python3 monitoring/developer-api/check_metrics.py [--prometheus http://127.0.0.1:9090]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, Iterable, List, Set, Tuple

HERE = Path(__file__).resolve().parent
MONITORING = HERE.parent
CONTRACT = HERE / "metrics-contract.json"
DASHBOARD = MONITORING / "grafana" / "dashboards" / "dgx-developer-api.json"
RULES = MONITORING / "prometheus" / "rules" / "developer-api.yml"

#: Grafana's built-in interval variables, replaced before parsing.
GRAFANA_VARS = {"$__rate_interval": "5m", "$__interval": "1m", "$__range": "1h"}
HISTOGRAM_SUFFIXES = ("_bucket", "_sum", "_count")

_KEYWORDS = {
    "and", "or", "unless", "by", "without", "on", "ignoring", "group_left",
    "group_right", "offset", "bool", "inf", "nan", "atan2",
}


# --------------------------------------------------------------- collection


def dashboard_exprs(path: Path = DASHBOARD) -> List[Tuple[str, str]]:
    """(where, expr) for every target of every panel, rows' children included."""
    doc = json.loads(path.read_text())
    out: List[Tuple[str, str]] = []

    def walk(panels):
        for panel in panels:
            for target in panel.get("targets", []):
                if target.get("expr"):
                    out.append((f"panel {panel.get('id')} {panel.get('title')!r}", target["expr"]))
            walk(panel.get("panels", []))

    walk(doc.get("panels", []))
    return out


def rule_exprs(path: Path = RULES) -> List[Tuple[str, str]]:
    """(where, expr) for every rule expression and every annotation query.
    Read with a tiny YAML-free scan when PyYAML is missing would be fragile,
    so PyYAML is required here (it is in every environment this repo uses)."""
    import yaml  # noqa: PLC0415 - optional at import time for the tests' own skip

    doc = yaml.safe_load(path.read_text())
    out: List[Tuple[str, str]] = []
    for group in doc.get("groups", []):
        for rule in group.get("rules", []):
            name = rule.get("alert") or rule.get("record")
            out.append((f"rule {name}", rule["expr"]))
            for key, value in (rule.get("annotations") or {}).items():
                for query in re.findall(r'query\s+"((?:[^"\\]|\\.)*)"', str(value)):
                    out.append((f"rule {name} annotation {key}", query.replace('\\"', '"')))
    return out


def substitute(expr: str) -> str:
    for var, value in GRAFANA_VARS.items():
        expr = expr.replace(var, value)
    return expr


# ------------------------------------------------ offline name extraction


def metric_names_offline(expr: str) -> Set[str]:
    """Metric names in a PromQL expression without a parser: drop strings,
    label matchers, grouping clauses and ranges, then keep identifiers that
    are not function calls or keywords. Checked against Prometheus's own
    parse tree by main(), so it is trusted only as far as that check goes."""
    s = substitute(expr)
    s = re.sub(r'"(?:[^"\\]|\\.)*"', '""', s)
    s = re.sub(r"\{[^{}]*\}", " ", s)
    s = re.sub(r"\b(by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)", " ", s)
    s = re.sub(r"\[[^\[\]]*\]", " ", s)
    s = re.sub(r"\boffset\s+-?\d+[smhdwy]", " ", s)
    names = set()
    for match in re.finditer(r"(?<![\w:.])([a-zA-Z_:][a-zA-Z0-9_:]*)(\s*\()?", s):
        token, call = match.group(1), match.group(2)
        if call or token.lower() in _KEYWORDS:
            continue
        names.add(token)
    return names


def base_name(name: str, contract: Dict[str, dict]) -> str:
    """A histogram's _bucket/_sum/_count series belong to its base entry."""
    for suffix in HISTOGRAM_SUFFIXES:
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            if contract.get(base, {}).get("type") == "histogram":
                return base
    return name


# ------------------------------------------------------------ prometheus


def _get(base: str, path: str, params: Dict[str, str] | None = None) -> dict:
    url = base.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:  # 400 carries the parse error body
        return json.load(exc)


def names_from_tree(node) -> Set[str]:
    return {name for name, _ in selectors_from_tree(node)}


def selectors_from_tree(node) -> Set[Tuple[str, str]]:
    """(metric name, selector string) for every selector in a parse tree."""
    found: Set[Tuple[str, str]] = set()
    if isinstance(node, dict):
        if node.get("type") in ("vectorSelector", "matrixSelector") and node.get("name"):
            matchers = ",".join(
                f'{m["name"]}{m["type"]}{json.dumps(m["value"])}' for m in node.get("matchers", [])
            )
            found.add((node["name"], "{" + matchers + "}"))
        for value in node.values():
            found |= selectors_from_tree(value)
    elif isinstance(node, list):
        for value in node:
            found |= selectors_from_tree(value)
    return found


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prometheus", default="http://127.0.0.1:9090")
    args = parser.parse_args(list(argv) if argv is not None else None)

    contract = json.loads(CONTRACT.read_text())["metrics"]
    exprs = dashboard_exprs() + rule_exprs()
    present = set(_get(args.prometheus, "/api/v1/label/__name__/values")["data"])

    failures: List[str] = []
    warnings: List[str] = []
    notes: List[str] = []
    used: Dict[str, int] = {}
    for where, expr in exprs:
        parsed = _get(args.prometheus, "/api/v1/parse_query", {"query": substitute(expr)})
        if parsed.get("status") != "success":
            failures.append(f"PARSE  {where}: {parsed.get('error')}")
            continue
        tree_names = names_from_tree(parsed["data"])
        offline = metric_names_offline(expr)
        if tree_names != offline:
            failures.append(f"EXTRACT {where}: parse tree {sorted(tree_names)} != offline {sorted(offline)}")
        for name in tree_names:
            used[base_name(name, contract)] = used.get(base_name(name, contract), 0) + 1
        # A name can exist while the SELECTOR matches nothing (a wrong job or
        # relname label). One metadata lookup per live selector, last hour.
        for name, selector in selectors_from_tree(parsed["data"]):
            status = contract.get(base_name(name, contract), {}).get("status")
            if status not in ("live", "event_driven"):
                continue
            found = _get(args.prometheus, "/api/v1/series", {"match[]": selector, "start": str(int(time.time()) - 3600)})
            if found.get("status") != "success":
                failures.append(f"SERIES {where}: {selector}: {found.get('error')}")
            elif not found["data"]:
                (failures if status == "live" else notes).append(
                    f"NOSERIES {where}: {selector} matched no series in the last hour ({status})")

    rows = []
    for name in sorted(used):
        entry = contract.get(name)
        if entry is None:
            failures.append(f"UNDECLARED {name}: add it to metrics-contract.json")
            rows.append((name, "UNDECLARED", "?", used[name]))
            continue
        status = entry["status"]
        series = [name + s for s in HISTOGRAM_SUFFIXES] if entry.get("type") == "histogram" else [name]
        exists = any(s in present for s in series)
        if status == "live" and not exists:
            failures.append(f"MISSING {name}: declared live but Prometheus has no such metric")
        if status in ("pending", "proposed") and exists:
            warnings.append(f"PROMOTE {name}: declared {status} but it exists now; mark it live and drop the panel marker")
        rows.append((name, status, "yes" if exists else "no", used[name]))
    for name, entry in sorted(contract.items()):
        if name not in used:
            warnings.append(f"UNUSED {name}: declared ({entry['status']}) but no expression reads it")

    width = max(len(r[0]) for r in rows)
    print(f"{'metric'.ljust(width)}  {'status':12} present  queries")
    for name, status, exists, count in rows:
        print(f"{name.ljust(width)}  {status:12} {exists:7}  {count}")
    print(f"\n{len(exprs)} expressions parsed by Prometheus; {len(rows)} distinct metrics")
    for line in notes:
        print("NOTE", line)
    for line in warnings:
        print("WARN", line)
    for line in failures:
        print("FAIL", line)
    print("RESULT", "FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
