"""What the last deliverable WAS, in the few fields a follow-up needs.

    "visualise this table on pie chart"  ->  kind document, formats ('png',),
                                             chart_type 'pie', binding {table_id, x, y, agg…}
    "make it a bar chart instead"        ->  the SAME binding, type 'bar'

WHY A ROW AND NOT A RE-READ. The binding already survives on disk in the
version's `spec.json`, and the formats already survive in
`artifact_versions.formats` — but the intent gate runs on the chat event
loop, before any artifact directory is opened, and it has to decide whether
"make it a bar chart instead" is a change to the file that was just made or
a brand-new request. Production 2026-09-16 answered that message with a new
artifact and a fresh model call for the data. `artifact_versions.deliverable`
(V39) is that answer as one small jsonb the conversation's artifact list
already carries: shape in, shape out, no extra query and no file read.

WHY ONE CHART. `binding` is filled only when the version holds exactly ONE
chart. "The same" means nothing about a report with five of them, and a
follow-up that names no chart must not silently re-bind the wrong one.

WHAT IS NOT HERE. Not the values, not the categories, not the style: those
are computed from the binding by chart_data every time the spec is
resolved, and a copy of them here would be a second truth that ages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from . import types as T

#: The Binding fields worth carrying to a follow-up. The whole model is
#: small, but it is a MODEL — it grows — and this row is read on every chat
#: turn that has an artifact, so the columns are listed by name.
BINDING_FIELDS: Tuple[str, ...] = (
    "table_id", "x", "y", "agg", "group_by", "date_bucket", "date_order",
    "bins", "sort", "top_n", "other_bucket", "y2", "start", "end", "label", "size",
)


@dataclass(frozen=True)
class Deliverable:
    """The shape of one published version: what kind of thing it is, which
    files it came out as, and — when it is a single chart — how that chart
    was bound to its data."""

    kind: str = ""
    formats: Tuple[str, ...] = ()
    #: How many charts the spec holds (0, 1, or more).
    charts: int = 0
    #: The type of the ONLY chart; empty when the version has none or many.
    chart_type: str = ""
    chart_title: str = ""
    #: That chart's Binding, as stored JSON; None when there is not exactly one.
    binding: Optional[Dict[str, Any]] = field(default=None)

    @property
    def is_chart(self) -> bool:
        """The deliverable IS a chart — every file it produced is a chart
        image. A report that HAS a chart is not one: its files are a docx
        and a pdf, and "make it a bar chart" means the chart inside it."""
        return bool(self.formats) and all(f in T.IMAGE_FORMATS for f in self.formats)

    @property
    def has_chart(self) -> bool:
        return self.charts > 0

    def to_json(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"kind": self.kind, "formats": list(self.formats), "charts": int(self.charts)}
        if self.chart_type:
            out["chart_type"] = self.chart_type
        if self.chart_title:
            out["chart_title"] = self.chart_title
        if self.binding is not None:
            out["binding"] = dict(self.binding)
        return out


def from_json(obj: Any) -> Deliverable:
    """A stored row → a Deliverable. Anything unreadable is the empty one:
    a version published before V39 has `{}` and simply says nothing."""
    if not isinstance(obj, Mapping):
        return Deliverable()
    formats = tuple(str(f) for f in (obj.get("formats") or []) if isinstance(f, str))
    binding = obj.get("binding")
    return Deliverable(
        kind=str(obj.get("kind") or ""),
        formats=formats,
        charts=int(obj.get("charts") or 0),
        chart_type=str(obj.get("chart_type") or ""),
        chart_title=str(obj.get("chart_title") or ""),
        binding=dict(binding) if isinstance(binding, Mapping) else None,
    )


def _charts_of(spec: Any) -> list:
    """Every chart in `spec`, as plain dicts. Returns [] for a spec this
    build cannot walk — the shape is an optimisation, never a gate."""
    try:
        from . import chart_spec as CS
    except Exception:  # noqa: BLE001 — the charts track is not in this build
        return []
    out = []
    try:
        for _path, raw in CS.iter_chart_slots(spec):
            if isinstance(raw, dict):
                out.append(raw)
            elif hasattr(raw, "model_dump"):
                out.append(raw.model_dump(mode="json", exclude_none=True))
    except Exception:  # noqa: BLE001
        return []
    return out


def of_spec(spec: Any, *, kind: str = "", formats: Sequence[str] = ()) -> Deliverable:
    """The shape of a published version, from its spec and its file formats."""
    kind = str(kind or getattr(spec, "kind", "") or "")
    charts = _charts_of(spec)
    shape = Deliverable(kind=kind, formats=tuple(str(f) for f in formats if f), charts=len(charts))
    if len(charts) != 1:
        return shape
    chart = charts[0]
    data = chart.get("data") if isinstance(chart.get("data"), Mapping) else None
    binding = {k: data[k] for k in BINDING_FIELDS if data is not None and data.get(k) not in (None, [], "")} if data else None
    return Deliverable(
        kind=shape.kind,
        formats=shape.formats,
        charts=1,
        chart_type=str(chart.get("type") or ""),
        chart_title=str(chart.get("title") or "")[:120],
        binding=binding,
    )


def of_version(row: Any) -> Deliverable:
    """The shape recorded on a version row (`deliverable`), falling back to
    what the row itself says: a version published before V39 still knows its
    formats, which is all `is_chart` needs."""
    if not isinstance(row, Mapping):
        return Deliverable()
    stored = from_json(row.get("deliverable"))
    if stored.formats or stored.charts:
        return stored
    files = row.get("files") if isinstance(row.get("files"), Iterable) else ()
    formats = [str(f.get("format")) for f in files if isinstance(f, Mapping) and f.get("format")]
    return Deliverable(kind=str(row.get("kind") or ""), formats=tuple(formats or [str(f) for f in (row.get("formats") or [])]))


__all__ = ["Deliverable", "BINDING_FIELDS", "from_json", "of_spec", "of_version"]
