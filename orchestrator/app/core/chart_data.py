"""Trusted, deterministic chart data preparation.

Only histograms need this today. Binning happens HERE, in Python, over the
rows the database returned — the model is never asked where the bin edges
should go, and raw records are never shown to it to make that decision.
What reaches both renderers is an already-binned (label, count) table, so
the browser chart and the report PNG are guaranteed to show the same bars.

Pure module: stdlib only.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from .chart_profile import _as_number
from .chart_spec import MAX_BINS, MIN_BINS

#: Column names of the binned result. Deliberately generic and stable —
#: they become `x_key` / `y_keys[0]` of the histogram ChartSpec.
BIN_COLUMN = "bin"
COUNT_COLUMN = "count"

#: Default bin-count bounds. Sturges/√n both behave; √n is used because it
#: is stable for the row counts this app sees (≤500 preview rows) and needs
#: no floating-point log.
_DEFAULT_MIN_BINS = 5
_DEFAULT_MAX_BINS = 20


def default_bin_count(n: int) -> int:
    """Deterministic bin count for `n` observations. Never model-chosen."""
    if n <= 1:
        return 1
    k = int(math.ceil(math.sqrt(n)))
    return max(_DEFAULT_MIN_BINS, min(_DEFAULT_MAX_BINS, k))


def clamp_bins(bins: Optional[int], n: int) -> int:
    """Clamp a requested bin count into [MIN_BINS, MAX_BINS]."""
    if bins is None:
        return default_bin_count(n)
    try:
        b = int(bins)
    except (TypeError, ValueError):
        return default_bin_count(n)
    return max(MIN_BINS, min(MAX_BINS, b))


def _fmt_edge(v: float, integral: bool) -> str:
    if integral:
        return str(int(round(v)))
    if abs(v) >= 1000 or (v != 0 and abs(v) < 0.01):
        return f"{v:.3g}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def build_histogram(
    columns: Sequence[str],
    rows: Sequence[Sequence],
    value_column: str,
    bins: Optional[int] = None,
) -> Optional[Tuple[List[str], List[List[object]], int]]:
    """Bin `value_column` into (["bin","count"], rows, bin_count).

    Returns None when the column is missing or holds no numeric values —
    the caller then emits no chart rather than an empty one.

    Bins are equal-width over [min, max]. The last bin is closed on the
    right so the maximum observation is counted rather than dropped.
    """
    cols = list(columns)
    if value_column not in cols:
        return None
    idx = cols.index(value_column)

    values: List[float] = []
    for r in rows:
        try:
            raw = r[idx]
        except (IndexError, KeyError, TypeError):
            continue
        n = _as_number(raw)
        if n is not None and math.isfinite(n):
            values.append(n)
    if not values:
        return None

    lo, hi = min(values), max(values)
    integral = all(float(v).is_integer() for v in values)

    if lo == hi:
        label = _fmt_edge(lo, integral)
        return [BIN_COLUMN, COUNT_COLUMN], [[label, len(values)]], 1

    k = clamp_bins(bins, len(values))
    width = (hi - lo) / k
    counts = [0] * k
    for v in values:
        # Right-closed final bin: (hi - lo) / width == k exactly at v == hi.
        slot = int((v - lo) / width)
        if slot >= k:
            slot = k - 1
        counts[slot] += 1

    out_rows: List[List[object]] = []
    for i, c in enumerate(counts):
        edge_lo = lo + i * width
        edge_hi = lo + (i + 1) * width if i < k - 1 else hi
        out_rows.append(
            [f"{_fmt_edge(edge_lo, integral)} - {_fmt_edge(edge_hi, integral)}", c]
        )
    return [BIN_COLUMN, COUNT_COLUMN], out_rows, k
