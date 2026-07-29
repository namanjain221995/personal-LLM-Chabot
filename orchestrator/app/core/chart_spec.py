"""Pydantic ChartSpec (spec §8, wire shape §10).

A chart spec is only produced when the user explicitly asked for a
chart/graph/plot. The model's JSON output is PARSED and VALIDATED here —
model output is NEVER executed. An invalid spec yields None → table only.

Wire shape (§10, mirrored by frontend/lib/types.ts ChartSpec):
    {type: "bar"|"line"|"area"|"pie"|"scatter",
     x_key: string, y_keys: string[], title: string, stacked: boolean}

Pure module: stdlib + pydantic.
"""
from __future__ import annotations

import json
import re
from typing import List, Literal, Optional, Sequence

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

ChartType = Literal["bar", "line", "scatter", "pie", "area"]

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)


class ChartSpec(BaseModel):
    """Declarative chart description rendered by the frontend / charts_png.

    `model_dump()` matches the §10 `chart` payload exactly:
    {type, x_key, y_keys, title, stacked}. The legacy model-output keys
    "x"/"y" are accepted as validation aliases only — they never appear on
    the wire.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: ChartType
    x_key: str = Field(validation_alias=AliasChoices("x_key", "x"))
    y_keys: List[str] = Field(validation_alias=AliasChoices("y_keys", "y"))
    title: str = ""
    stacked: bool = False

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
    those result columns.
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
