"""One chart pipeline, shared by the SQL engine, the agent route and reports.

    decide (trusted)  →  [model, only if ambiguous]  →  validate  →  prepare data

Everything a caller needs is `build_chart`. It NEVER raises: a chart is an
enhancement, and no failure in here may cost the user their answer, their
table, or — on the streaming path — the rest of the stream. Failures are
logged and come back as None, which every caller already treats as
"table only".

The model is consulted for exactly one case: the user explicitly asked for
"a chart" and the result shape has no single obvious reading. Even then it
sees only column METADATA (`ColumnProfile.to_prompt_dict`) — never a cell
value — and its JSON is validated against the real result columns before
anything is drawn.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from .chart_data import build_histogram
from .chart_decision import ChartDecision, build_spec, decide
from .chart_profile import ColumnProfile, profile_columns
from .chart_spec import ChartSpec, parse_chart_spec

log = logging.getLogger(__name__)

#: An async callable returning the model's raw chart JSON for a prompt.
AskModel = Callable[[List[dict]], Awaitable[str]]


@dataclass
class ChartResult:
    """A validated spec plus the exact data both renderers should draw."""

    spec: ChartSpec
    columns: List[str]
    rows: List[List[object]]
    reason: str = ""
    confidence: float = 0.0
    #: True when `rows` is NOT the query result (histograms are re-binned),
    #: so the caller knows to ship it alongside `meta.data`, not instead.
    derived: bool = False


def chart_prompt(
    question: str, profiles: Sequence[ColumnProfile], types: Sequence[str]
) -> List[dict]:
    """Messages for the ambiguous-explicit chart call.

    Carries the user's own question and COLUMN METADATA only. No Salesforce
    cell value is ever placed in this prompt: record values are data, not
    instructions, and a Case subject must not be able to talk to the model
    through the chart path.
    """
    meta = json.dumps([p.to_prompt_dict() for p in profiles], default=str)
    system = (
        "Design a chart for a SQL result. Respond with ONLY a JSON object, "
        "no prose:\n"
        '{"type": "' + "|".join(types) + '", "x_key": "<column>", '
        '"y_keys": ["<column>", ...], "title": "<short title>", '
        '"stacked": true or false}\n'
        "Rules: x_key and every y_keys entry MUST be one of the column names "
        "below. y_keys must be numeric columns. Use pie or donut only for a "
        "single non-negative measure across few categories. Use "
        "horizontal_bar when category labels are long. Use scatter only when "
        "both axes are numeric.\n"
        "Column metadata (no row values are shown, by design):\n" + meta
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]


#: Types the model is allowed to name. `histogram` is excluded on purpose —
#: histograms require trusted binning, which is decided in Python, and
#: `funnel` is excluded because a funnel needs a trusted stage order the
#: model has no way to establish.
MODEL_CHART_TYPES: Tuple[str, ...] = (
    "bar",
    "horizontal_bar",
    "line",
    "area",
    "pie",
    "donut",
    "scatter",
)


async def build_chart(
    message: str,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    *,
    mode: str = "explicit",
    ask_model: Optional[AskModel] = None,
    title: str = "",
    force: bool = False,
) -> Optional[ChartResult]:
    """Decide, build and validate a chart. Returns None for "table only".

    `force=True` asserts that the caller has already established the user's
    intent to see a chart (the report planner's per-section `chart: true`).
    It skips the wording check, never the validation.
    """
    try:
        return await _build_chart(
            message, list(columns), list(rows), mode=mode,
            ask_model=ask_model, title=title, force=force,
        )
    except Exception:  # never cost the caller its answer
        log.warning("chart generation failed; continuing without a chart", exc_info=True)
        return None


async def _build_chart(
    message: str,
    columns: List[str],
    rows: List[Sequence],
    *,
    mode: str,
    ask_model: Optional[AskModel],
    title: str,
    force: bool = False,
) -> Optional[ChartResult]:
    if not columns or not rows:
        return None

    profiles = profile_columns(columns, rows)
    decision = decide(
        message, columns, rows, mode=mode, profiles=profiles,
        explicit_override=True if force else None,
    )
    if not decision.should_chart:
        return None

    # --- histogram: bin in trusted Python, then chart the binned table ---
    if decision.chart_type == "histogram" and decision.histogram_source:
        binned = build_histogram(columns, rows, decision.histogram_source)
        if binned is None:
            return None
        hist_cols, hist_rows, k = binned
        spec = ChartSpec(
            type="histogram",
            x_key=hist_cols[0],
            y_keys=[hist_cols[1]],
            title=title or f"Distribution of {decision.histogram_source}",
            bins=k,
            show_legend=False,
        )
        return ChartResult(
            spec, hist_cols, [list(r) for r in hist_rows],
            decision.reason, decision.confidence, derived=True,
        )

    # --- ambiguous explicit request: the one model call ------------------
    if decision.use_model:
        if ask_model is None:
            return None
        raw = await ask_model(chart_prompt(message, profiles, MODEL_CHART_TYPES))
        spec = parse_chart_spec(raw, columns=columns)
        if spec is None:
            return None
        spec = _repair(spec, profiles)
        if spec is None:
            return None
        return ChartResult(
            spec, columns, [list(r) for r in rows], "model_spec", 0.5
        )

    # --- deterministic ---------------------------------------------------
    spec = build_spec(decision, columns, title=title or _auto_title(decision))
    if spec is None:
        return None
    ordered = _order_rows(spec, columns, rows)
    # A funnel's stage order and a line's chronology are part of the chart's
    # meaning, so a reordered copy travels with the chart. `meta.data` keeps
    # the order the SQL asked for — the table is not silently resorted.
    reordered = [list(r) for r in rows] != ordered
    return ChartResult(
        spec, columns, ordered, decision.reason, decision.confidence, derived=reordered
    )


def _auto_title(decision: ChartDecision) -> str:
    ys = ", ".join(decision.y_keys)
    return f"{ys} by {decision.x_key}" if ys and decision.x_key else ""


def _repair(spec: ChartSpec, profiles: Sequence[ColumnProfile]) -> Optional[ChartSpec]:
    """Last trusted check on a model spec: are the y_keys actually numeric?

    Column existence is already guaranteed by `parse_chart_spec`. This
    catches the other way a model spec goes wrong — pointing the measure at
    a text column, which renders as a flat row of zeros and reads as real.
    """
    index = {p.name: p for p in profiles}
    numeric = [y for y in spec.y_keys if index.get(y) is not None and index[y].is_numeric]
    if not numeric:
        return None
    if spec.type == "scatter":
        x = index.get(spec.x_key)
        if x is None or not x.is_numeric:
            return None
    if numeric == list(spec.y_keys):
        return spec
    return spec.model_copy(update={"y_keys": numeric})


def _order_rows(
    spec: ChartSpec, columns: Sequence[str], rows: Sequence[Sequence]
) -> List[List[object]]:
    """Order rows for the chart types whose meaning depends on order.

    Only two do:
      funnel — trusted stage order, never alphabetical and never by value.
      line   — chronological, when the x axis is a date-like label that is
               not already sorted.
    Everything else keeps the order the query produced; the SQL's own
    ORDER BY is the user's intent and must not be second-guessed.
    """
    from .chart_decision import trusted_stage_order
    from .chart_profile import profile_column, _column_values

    cols = list(columns)
    if spec.x_key not in cols:
        return [list(r) for r in rows]
    xi = cols.index(spec.x_key)
    materialized = [list(r) for r in rows]

    if spec.type == "funnel":
        labels = [str(r[xi]) for r in materialized]
        ordered = trusted_stage_order(labels)
        if ordered is None:
            return materialized
        rank = {lbl: i for i, lbl in enumerate(ordered)}
        return sorted(materialized, key=lambda r: rank.get(str(r[xi]), len(rank)))

    if spec.type in ("line", "area"):
        prof = profile_column(spec.x_key, _column_values(materialized, xi))
        if prof.is_date and not prof.monotonic:
            return sorted(materialized, key=lambda r: str(r[xi]))
    return materialized
