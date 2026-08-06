"""Pydantic ChartSpec (spec §8, wire shape §10).

A chart spec is produced either deterministically by trusted backend code
(`chart_decision.build_spec`) or by a model call for genuinely ambiguous
requests. Model JSON is PARSED and VALIDATED here — model output is NEVER
executed. An invalid spec yields None → table only.

Wire shape (§10, mirrored by frontend/lib/types.ts ChartSpec):
    {type: ChartType, x_key: string, y_keys: string[], title: string,
     stacked: boolean}
plus, only when they differ from their defaults, the optional keys
`bins` / `show_legend` / `show_values`. See `wire_dump`: payloads for the
five original chart types are byte-identical to what shipped before, so
every conversation already persisted keeps validating and rendering.

The renderer is deliberately NOT described here. This spec is
renderer-independent: the browser draws it with Apache ECharts, reports
draw it with matplotlib, and neither takes drawing instructions from the
model. There is no field through which renderer options, JavaScript,
formatters, HTML or CSS could reach either renderer.

Pure module: stdlib + pydantic.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Literal, Optional, Sequence

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

ChartType = Literal[
    # original five — wire-compatible with every persisted conversation
    "bar",
    "line",
    "scatter",
    "pie",
    "area",
    # added in the ECharts migration
    "horizontal_bar",
    "donut",
    "funnel",
    "histogram",
]

#: Every member of ChartType, as data. Kept next to the Literal so a type
#: added to one and forgotten in the other is caught by test_chart_spec.
CHART_TYPES: tuple = (
    "bar",
    "line",
    "scatter",
    "pie",
    "area",
    "horizontal_bar",
    "donut",
    "funnel",
    "histogram",
)

#: Types whose x axis is a category and whose y values must be non-negative
#: shares-of-a-whole.
PART_TO_WHOLE_TYPES = frozenset({"pie", "donut"})

#: Histogram bin bounds. The model never chooses these — `chart_data`
#: computes the default and clamps anything a caller passes.
MIN_BINS = 2
MAX_BINS = 50

_LEGACY_WIRE_KEYS = ("type", "x_key", "y_keys", "title", "stacked")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


class ChartSpec(BaseModel):
    """Declarative, renderer-independent chart description.

    `wire_dump()` produces the §10 `chart` payload. For the five original
    types with default options it is exactly the historical
    {type, x_key, y_keys, title, stacked} — no new keys appear, so old
    frontends and old persisted messages are unaffected.

    The legacy model-output keys "x"/"y" are accepted as validation aliases
    only — they never appear on the wire.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: ChartType
    x_key: str = Field(validation_alias=AliasChoices("x_key", "x"))
    y_keys: List[str] = Field(validation_alias=AliasChoices("y_keys", "y"))
    title: str = ""
    stacked: bool = False

    # --- optional, added in the ECharts migration --------------------------
    # Each is a bounded integer or a bool. None is a free-form string, dict
    # or renderer option: there is deliberately no field through which model
    # output could reach ECharts or matplotlib as configuration.
    bins: Optional[int] = Field(default=None, ge=MIN_BINS, le=MAX_BINS)
    show_legend: bool = True
    show_values: bool = False

    @field_validator("x_key")
    @classmethod
    def _x_non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("x_key must be a non-empty column name")
        return v

    @field_validator("y_keys", mode="before")
    @classmethod
    def _y_coerce(cls, v: object) -> object:
        # A single column name is accepted and normalized to a one-item list.
        return [v] if isinstance(v, str) else v

    @field_validator("y_keys")
    @classmethod
    def _y_non_empty(cls, v: List[str]) -> List[str]:
        cleaned = [s.strip() for s in v]
        if not cleaned or any(not s for s in cleaned):
            raise ValueError("y_keys must contain non-empty column names")
        return cleaned

    @field_validator("title", mode="before")
    @classmethod
    def _title_str(cls, v: object) -> str:
        return "" if v is None else str(v)

    @model_validator(mode="after")
    def _normalize_options(self) -> "ChartSpec":
        # `bins` describes histogram binning and nothing else. A model that
        # volunteers it on a bar chart is confused, not hostile — drop the
        # field rather than throwing the whole (otherwise valid) chart away.
        if self.type != "histogram" and self.bins is not None:
            object.__setattr__(self, "bins", None)
        # Part-to-whole charts draw one measure. Extra y_keys would silently
        # go missing at render time; truncating here keeps every renderer,
        # and the legend, agreeing about what is on screen.
        if self.type in PART_TO_WHOLE_TYPES and len(self.y_keys) > 1:
            object.__setattr__(self, "y_keys", self.y_keys[:1])
        return self

    # --- wire boundary -----------------------------------------------------

    def wire_dump(self) -> Dict[str, Any]:
        """The §10 `chart` payload.

        Optional keys are emitted only when they are NOT at their default,
        so a bar/line/area/pie/scatter spec serializes to exactly the five
        keys it always did. Persisted conversations and any downstream
        consumer written against the old shape keep working unchanged.
        """
        out: Dict[str, Any] = {
            "type": self.type,
            "x_key": self.x_key,
            "y_keys": list(self.y_keys),
            "title": self.title,
            "stacked": self.stacked,
        }
        if self.bins is not None:
            out["bins"] = self.bins
        if self.show_legend is not True:
            out["show_legend"] = self.show_legend
        if self.show_values is not False:
            out["show_values"] = self.show_values
        return out


def _extract_json(text: str) -> Optional[str]:
    t = _THINK_RE.sub("", text or "").strip()
    m = _FENCE_RE.search(t)
    if m:
        t = m.group(1).strip()
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    return t[start : end + 1]


def parse_chart_spec(raw: object, columns: Optional[Sequence[str]] = None) -> Optional[ChartSpec]:
    """Parse + validate a model-produced chart spec. Returns None when invalid.

    When `columns` is given, x_key and every y_keys entry must be one of
    those result columns. That check is the whole reason a chart spec can be
    trusted: every key the renderers dereference is a column the database
    actually returned, so nothing the model wrote is ever looked up blindly.
    """
    payload: object = raw
    if isinstance(raw, ChartSpec):
        spec: Optional[ChartSpec] = raw
    else:
        if isinstance(raw, (str, bytes)):
            blob = _extract_json(raw.decode() if isinstance(raw, bytes) else raw)
            if blob is None:
                return None
            try:
                payload = json.loads(blob)
            except (json.JSONDecodeError, ValueError):
                return None
        if not isinstance(payload, dict):
            return None
        try:
            spec = ChartSpec.model_validate(payload)
        except ValidationError:
            return None

    if spec is not None and columns is not None:
        colset = set(columns)
        if spec.x_key not in colset or any(y not in colset for y in spec.y_keys):
            return None
    return spec
