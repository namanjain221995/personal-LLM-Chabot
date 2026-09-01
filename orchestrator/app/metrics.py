"""Prometheus metrics for the living knowledge layer — stdlib only.

No prometheus_client dependency, for the same reason monitoring/exporters/
dgx-gpu is stdlib-only: this process already carries torch-adjacent weight and
a metrics library is a large surface for a few dozen counters. The text
exposition format is a handful of lines to render correctly.

CARDINALITY IS THE ONLY REAL RISK. A URL, a query or a user id as a label
would produce unbounded series and eventually take Prometheus down with it, so
every label here is drawn from a small closed set (freshness level, rule name,
hit/miss) and the module refuses anything it does not recognise.

Every function is called from request paths and must never raise.
"""
from __future__ import annotations

import threading
from typing import Dict, Iterable, List, Tuple

_lock = threading.Lock()

#: name -> {label-tuple: value}
_counters: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
_gauges: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
#: name -> {label-tuple: (bucket counts, sum, count)}
_hists: Dict[str, Dict[Tuple[Tuple[str, str], ...], Tuple[List[int], float, int]]] = {}

_HELP: Dict[str, str] = {}
_TYPE: Dict[str, str] = {}

#: Seconds. Tuned for retrieval and small fetches, not for model generation.
_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

#: Closed label vocabularies. Anything else becomes "other" rather than a new
#: series — a typo in a call site must not be able to grow the index.
_ALLOWED = {
    "level": {"static", "recent", "realtime"},
    "rule": {
        "lexical:realtime", "lexical:office", "lexical:recent", "lexical:static",
        "router", "default", "empty",
    },
    "result": {"hit", "miss", "fresh", "stale", "ok", "fail"},
    "job": {"index", "refresh", "expand"},
}


def _clean(labels: Dict[str, str]) -> Tuple[Tuple[str, str], ...]:
    out = []
    for key, value in sorted(labels.items()):
        allowed = _ALLOWED.get(key)
        v = str(value)
        if allowed is not None and v not in allowed:
            v = "other"
        if v.startswith("year:"):  # freshness rule carries a year; bucket it
            v = "year"
        out.append((key, v))
    return tuple(out)


def _declare(name: str, kind: str, help_text: str) -> None:
    _HELP.setdefault(name, help_text)
    _TYPE.setdefault(name, kind)


def inc(name: str, help_text: str = "", **labels: str) -> None:
    try:
        _declare(name, "counter", help_text or name)
        key = _clean(labels)
        with _lock:
            _counters.setdefault(name, {})
            _counters[name][key] = _counters[name].get(key, 0.0) + 1.0
    except Exception:  # noqa: BLE001 — a metric must never break a request
        pass


def set_gauge(name: str, value: float, help_text: str = "", **labels: str) -> None:
    try:
        _declare(name, "gauge", help_text or name)
        key = _clean(labels)
        with _lock:
            _gauges.setdefault(name, {})
            _gauges[name][key] = float(value)
    except Exception:  # noqa: BLE001
        pass


def observe(name: str, seconds: float, help_text: str = "", **labels: str) -> None:
    try:
        _declare(name, "histogram", help_text or name)
        key = _clean(labels)
        with _lock:
            _hists.setdefault(name, {})
            counts, total, n = _hists[name].get(key, ([0] * len(_BUCKETS), 0.0, 0))
            counts = list(counts)
            for i, edge in enumerate(_BUCKETS):
                if seconds <= edge:
                    counts[i] += 1
            _hists[name][key] = (counts, total + float(seconds), n + 1)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# The living-knowledge call sites, named once so spelling cannot drift.
# ---------------------------------------------------------------------------


def freshness_classified(level: str, rule: str) -> None:
    inc(
        "techsara_freshness_classified_total",
        "Questions classified by required freshness.",
        level=level,
        rule=rule,
    )


def web_memory_query(*, hit: bool, fresh: bool, seconds: float) -> None:
    inc(
        "techsara_web_memory_queries_total",
        "Local web-memory retrievals attempted.",
        result="hit" if hit else "miss",
    )
    if hit:
        inc(
            "techsara_web_memory_hits_total",
            "Local web-memory retrievals that returned evidence.",
            result="fresh" if fresh else "stale",
        )
    observe(
        "techsara_web_memory_seconds",
        seconds,
        "Time to retrieve local web evidence.",
    )


def freshness_auto_search(ok: bool) -> None:
    inc(
        "techsara_freshness_auto_search_total",
        "Lightweight live lookups triggered because local evidence was insufficient.",
        result="ok" if ok else "fail",
    )


def worker_job(job: str, ok: bool, seconds: float) -> None:
    inc(
        "techsara_web_worker_jobs_total",
        "Background knowledge-worker jobs.",
        job=job,
        result="ok" if ok else "fail",
    )
    observe("techsara_web_worker_seconds", seconds, "Background job duration.", job=job)


def corpus_gauges(pages: int, pending: int, due: int) -> None:
    set_gauge("techsara_web_pages_total", pages, "Pages in the public web corpus.")
    set_gauge(
        "techsara_web_embedding_pending", pending, "Pages stored but not yet embedded."
    )
    set_gauge(
        "techsara_web_refresh_queue_depth", due, "Pages past their refresh deadline."
    )


# ---------------------------------------------------------------------------
# Exposition
# ---------------------------------------------------------------------------


def _fmt_labels(key: Tuple[Tuple[str, str], ...], extra: str = "") -> str:
    parts = [f'{k}="{v}"' for k, v in key]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


def render() -> str:
    """The whole registry in Prometheus text exposition format."""
    lines: List[str] = []
    with _lock:
        counters = {n: dict(v) for n, v in _counters.items()}
        gauges = {n: dict(v) for n, v in _gauges.items()}
        hists = {n: dict(v) for n, v in _hists.items()}

    for name, series in sorted(counters.items()):
        lines.append(f"# HELP {name} {_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} counter")
        for key, value in sorted(series.items()):
            lines.append(f"{name}{_fmt_labels(key)} {value:g}")

    for name, series in sorted(gauges.items()):
        lines.append(f"# HELP {name} {_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} gauge")
        for key, value in sorted(series.items()):
            lines.append(f"{name}{_fmt_labels(key)} {value:g}")

    for name, series in sorted(hists.items()):
        lines.append(f"# HELP {name} {_HELP.get(name, name)}")
        lines.append(f"# TYPE {name} histogram")
        for key, (counts, total, n) in sorted(series.items()):
            # The le= label is built OUTSIDE the f-string. A backslash inside
            # an f-string expression is only legal from Python 3.12 (PEP 701),
            # and the containers run 3.11 — this file parsed fine on the dev
            # box and on the 3.12 image, then failed to import in CI.
            for edge, c in zip(_BUCKETS, counts):
                edge_label = 'le="{}"'.format(edge)
                lines.append(
                    "{}_bucket{} {}".format(name, _fmt_labels(key, edge_label), c)
                )
            inf_label = 'le="+Inf"'
            lines.append("{}_bucket{} {}".format(name, _fmt_labels(key, inf_label), n))
            lines.append(f"{name}_sum{_fmt_labels(key)} {total:g}")
            lines.append(f"{name}_count{_fmt_labels(key)} {n}")

    return "\n".join(lines) + "\n"


def reset() -> None:
    """Tests only."""
    with _lock:
        _counters.clear()
        _gauges.clear()
        _hists.clear()
