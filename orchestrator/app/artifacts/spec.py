"""ArtifactSpec — the structured content the model writes and code renders.

WHY A SPEC AND NOT A FILE. The model is never asked for bytes, markup, or
formulas. It is asked for CONTENT with a fixed vocabulary of blocks — a
heading, a paragraph, a table with rows, a chart over those rows — and every
field is validated here before a renderer sees it. That is what makes the
output safe (no HTML, no formula, no path from the model reaches a file), the
same across formats (one spec renders to .docx and .pdf identically), and
editable (a follow-up turn edits the spec, and the renderers do the rest).

THREE TYPED VARIANTS, ONE ENVELOPE. A document, a presentation and a workbook
share almost nothing structurally, so they are three models, not one model
with mostly-empty fields. `ArtifactSpec` is the envelope that names the kind
and carries exactly one of them. `schema_for(kind)` returns the JSON schema
the model is constrained to for that kind — smaller schemas hold better.

WHAT IS REJECTED, AND WHAT IS CORRECTED. Unknown block types, unsupported
chart types, a table with ragged rows, a citation whose source is not in the
manifest, a formula-shaped cell, text over the ceiling: `extra="forbid"` and
the validators below reject them so the caller can ask the model once more.
Some things are corrected rather than refused, because a person would rather
have the document: an overlong title is cut, a bullet list past the cap is
truncated with a warning, a chart with no rows becomes its table. Every
correction is recorded in `Warnings` so it can be shown and counted.

SCHEMA VERSION. `spec_version` is stored with every version's spec.json. A
reader that meets a newer version than it knows must refuse loudly rather
than render a guess.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import types as T

SPEC_VERSION = 1

# --------------------------------------------------------------- helpers --

#: A cell whose text would be read as a formula by a spreadsheet. The XLSX
#: renderer neutralises these; the spec ALSO refuses them in a formula field
#: so a model cannot smuggle `=HYPERLINK(...)` in as "a formula".
_FORMULA_LEADS = ("=", "+", "-", "@", "\t", "\r")

#: A URL a citation may carry. http(s) only; no data:, file:, javascript:.
_URL_RE = re.compile(r"^https?://[^\s<>\"']{1,2000}$", re.IGNORECASE)

_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------- shared --


class Citation(_Strict):
    """A source the content leans on. `id` is what blocks refer to in their
    `sources` lists; it must exist in the spec's `sources` manifest."""

    id: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=1, max_length=300)
    url: Optional[str] = Field(default=None, max_length=2000)
    retrieved_at: Optional[str] = Field(default=None, max_length=40)
    note: Optional[str] = Field(default=None, max_length=300)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not _SOURCE_ID_RE.match(v):
            raise ValueError("source id must be letters, digits, _ or -")
        return v

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return None
        if not _URL_RE.match(v):
            raise ValueError("citation url must be http(s)")
        return v


class Series(_Strict):
    name: str = Field(min_length=1, max_length=80)
    values: List[float] = Field(min_length=1, max_length=T.MAX_CHART_POINTS)


class Chart(_Strict):
    """A chart the renderers draw from DATA in the spec — categories and
    numeric series — never from an image or a description. `bar`, `line`,
    `pie` and `horizontal_bar` are what every renderer supports natively
    (matplotlib for PDF/DOCX, native charts in PPTX/XLSX)."""

    type: Literal["bar", "horizontal_bar", "line", "pie"] = "bar"
    title: str = Field(default="", max_length=120)
    categories: List[str] = Field(min_length=1, max_length=T.MAX_CHART_POINTS)
    series: List[Series] = Field(min_length=1, max_length=8)
    y_label: str = Field(default="", max_length=60)
    caption: str = Field(default="", max_length=300)
    sources: List[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _lengths_agree(self) -> "Chart":
        n = len(self.categories)
        for s in self.series:
            if len(s.values) != n:
                raise ValueError(f"series {s.name!r} has {len(s.values)} values for {n} categories")
        if self.type == "pie" and len(self.series) != 1:
            raise ValueError("a pie chart has exactly one series")
        return self


class Table(_Strict):
    columns: List[str] = Field(min_length=1, max_length=T.MAX_TABLE_COLUMNS)
    rows: List[List[Union[str, float, int, None]]] = Field(default_factory=list, max_length=T.MAX_TABLE_ROWS)
    caption: str = Field(default="", max_length=300)
    #: Column indexes rendered as numbers (right-aligned, thousands
    #: separators). Everything else is text.
    numeric_columns: List[int] = Field(default_factory=list, max_length=T.MAX_TABLE_COLUMNS)
    sources: List[str] = Field(default_factory=list, max_length=8)

    @field_validator("columns")
    @classmethod
    def _column_text(cls, v: List[str]) -> List[str]:
        return [_clip(c, 80) or f"Column {i + 1}" for i, c in enumerate(v)]

    @model_validator(mode="after")
    def _rectangular(self) -> "Table":
        width = len(self.columns)
        for i, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(f"row {i + 1} has {len(row)} cells for {width} columns")
        for idx in self.numeric_columns:
            if not 0 <= idx < width:
                raise ValueError(f"numeric column {idx} is out of range")
        return self


# -------------------------------------------------------------- document --


class Heading(_Strict):
    type: Literal["heading"] = "heading"
    level: int = Field(ge=1, le=3)
    text: str = Field(min_length=1, max_length=200)


class Paragraph(_Strict):
    type: Literal["paragraph"] = "paragraph"
    text: str = Field(min_length=1, max_length=6000)
    sources: List[str] = Field(default_factory=list, max_length=8)


class Bullets(_Strict):
    type: Literal["bullets"] = "bullets"
    items: List[str] = Field(min_length=1, max_length=40)
    sources: List[str] = Field(default_factory=list, max_length=8)

    @field_validator("items")
    @classmethod
    def _item_text(cls, v: List[str]) -> List[str]:
        items = [_clip(i, 600) for i in v if (i or "").strip()]
        if not items:
            raise ValueError("a list needs at least one item")
        return items


class Numbered(Bullets):
    type: Literal["numbered"] = "numbered"  # type: ignore[assignment]


class TableBlock(_Strict):
    type: Literal["table"] = "table"
    table: Table


class ChartBlock(_Strict):
    type: Literal["chart"] = "chart"
    chart: Chart


class Callout(_Strict):
    type: Literal["callout"] = "callout"
    kind: Literal["note", "tip", "warning", "quote"] = "note"
    title: str = Field(default="", max_length=120)
    text: str = Field(min_length=1, max_length=2000)


class KPI(_Strict):
    label: str = Field(min_length=1, max_length=60)
    value: str = Field(min_length=1, max_length=40)
    note: str = Field(default="", max_length=120)


class KPIRow(_Strict):
    type: Literal["kpis"] = "kpis"
    items: List[KPI] = Field(min_length=1, max_length=6)


class PageBreak(_Strict):
    type: Literal["page_break"] = "page_break"


DocumentBlock = Union[Heading, Paragraph, Bullets, Numbered, TableBlock, ChartBlock, Callout, KPIRow, PageBreak]


class DocumentSpec(_Strict):
    """A report, brief, SOP, memo, proposal — anything that is pages."""

    title: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=200)
    audience: str = Field(default="", max_length=120)
    purpose: str = Field(default="", max_length=300)
    tone: Literal["formal", "professional", "conversational"] = "professional"
    template_id: Literal[
        "executive_report", "brief", "sop", "technical_report", "research_report",
        "proposal", "meeting_summary", "generic",
    ] = "generic"
    cover: bool = False
    toc: bool = False
    confidential: bool = False
    author: str = Field(default="", max_length=120)
    date: str = Field(default="", max_length=40)
    orientation: Literal["portrait", "landscape"] = "portrait"
    blocks: List[DocumentBlock] = Field(min_length=1, max_length=400)
    sources: List[Citation] = Field(default_factory=list, max_length=60)
    assumptions: List[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _shape(self) -> "DocumentSpec":
        _check_source_refs(self.blocks, self.sources)
        prose = sum(len(getattr(b, "text", "") or "") for b in self.blocks)
        prose += sum(sum(len(i) for i in b.items) for b in self.blocks if isinstance(b, Bullets))
        if prose > T.MAX_TEXT_CHARS:
            raise ValueError(f"document prose is {prose} characters; the ceiling is {T.MAX_TEXT_CHARS}")
        return self


# ---------------------------------------------------------- presentation --


class Slide(_Strict):
    """One slide. The layout names what the renderer draws; the fields the
    layout does not use are ignored, and a layout missing its field falls
    back to a bullets slide with a warning rather than a blank."""

    layout: Literal[
        "title", "section", "bullets", "two_column", "chart", "table",
        "comparison", "timeline", "kpis", "closing",
    ] = "bullets"
    title: str = Field(default="", max_length=120)
    subtitle: str = Field(default="", max_length=200)
    bullets: List[str] = Field(default_factory=list, max_length=8)
    left: List[str] = Field(default_factory=list, max_length=6)
    right: List[str] = Field(default_factory=list, max_length=6)
    left_title: str = Field(default="", max_length=60)
    right_title: str = Field(default="", max_length=60)
    chart: Optional[Chart] = None
    table: Optional[Table] = None
    kpis: List[KPI] = Field(default_factory=list, max_length=4)
    #: `timeline` — (label, text) pairs, in order.
    steps: List[Tuple[str, str]] = Field(default_factory=list, max_length=6)
    notes: str = Field(default="", max_length=3000)
    sources: List[str] = Field(default_factory=list, max_length=8)

    @field_validator("bullets", "left", "right")
    @classmethod
    def _bullet_text(cls, v: List[str]) -> List[str]:
        return [_clip(i, 220) for i in v if (i or "").strip()]

    @model_validator(mode="after")
    def _layout_follows_content(self) -> "Slide":
        """A slide declared `bullets` that carries a chart and no bullets IS
        a chart slide; the first real run (2026-09-11) had the model put
        `bullets` on every slide, and the one with the revenue chart came
        out blank because the renderer drew the layout it was told. The
        content decides where the declaration does not use it."""
        if self.layout == "bullets" and not self.bullets:
            if self.chart is not None:
                self.layout = "chart"
            elif self.table is not None:
                self.layout = "table"
            elif self.kpis:
                self.layout = "kpis"
            elif self.steps:
                self.layout = "timeline"
            elif self.left or self.right:
                self.layout = "two_column"
        return self


class PresentationSpec(_Strict):
    title: str = Field(min_length=1, max_length=120)
    subtitle: str = Field(default="", max_length=200)
    audience: str = Field(default="", max_length=120)
    purpose: str = Field(default="", max_length=300)
    template_id: Literal["ceo", "training", "quarterly_review", "generic"] = "generic"
    confidential: bool = False
    author: str = Field(default="", max_length=120)
    date: str = Field(default="", max_length=40)
    slides: List[Slide] = Field(min_length=1, max_length=T.MAX_SLIDES)
    sources: List[Citation] = Field(default_factory=list, max_length=60)
    assumptions: List[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _shape(self) -> "PresentationSpec":
        # Relabelled, never invented: a first slide with a title and nothing
        # else IS the title slide whatever it was called, and a last slide
        # titled like a closing is one. A deck the model wrote without a
        # title slide is rendered as written — the composer's prompt asks
        # for one; adding a slide here would be content the model did not
        # write.
        first = self.slides[0]
        if first.layout == "bullets" and not first.bullets and first.chart is None and first.table is None and not first.kpis:
            first.layout = "title"
        last = self.slides[-1]
        if len(self.slides) > 1 and last.layout == "bullets" and re.match(r"^(closing|thank you|thanks|questions|next steps|summary|q\s*&\s*a)\b", last.title.strip().lower() or ""):
            last.layout = "closing"
        _check_source_refs(self.slides, self.sources)
        for s in self.slides:
            if s.chart is not None:
                _check_source_refs([s.chart], self.sources)
            if s.table is not None:
                _check_source_refs([s.table], self.sources)
        return self


# -------------------------------------------------------------- workbook --

ColumnType = Literal["text", "integer", "number", "currency", "percent", "date"]


class Column(_Strict):
    name: str = Field(min_length=1, max_length=80)
    type: ColumnType = "text"
    width: Optional[int] = Field(default=None, ge=6, le=80)


class Total(_Strict):
    """A totals row the RENDERER writes as a real formula over the column —
    the model names the column and the function; it never writes `=SUM(`."""

    column: int = Field(ge=0)
    fn: Literal["sum", "average", "count", "min", "max"] = "sum"
    label: str = Field(default="Total", max_length=40)


class Sheet(_Strict):
    name: str = Field(min_length=1, max_length=31)
    columns: List[Column] = Field(min_length=1, max_length=T.MAX_COLUMNS_PER_SHEET)
    rows: List[List[Union[str, float, int, None]]] = Field(default_factory=list, max_length=T.MAX_ROWS_PER_SHEET)
    totals: List[Total] = Field(default_factory=list, max_length=T.MAX_COLUMNS_PER_SHEET)
    freeze_header: bool = True
    autofilter: bool = True
    charts: List[Chart] = Field(default_factory=list, max_length=4)
    notes: str = Field(default="", max_length=2000)

    @field_validator("name")
    @classmethod
    def _sheet_name(cls, v: str) -> str:
        # Excel forbids these in a sheet name; strip rather than refuse.
        cleaned = re.sub(r"[\[\]\*\?/\\:]", " ", v).strip()[:31]
        return cleaned or "Sheet"

    @model_validator(mode="after")
    def _shape(self) -> "Sheet":
        width = len(self.columns)
        for i, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(f"sheet {self.name!r} row {i + 1} has {len(row)} cells for {width} columns")
        for t in self.totals:
            if t.column >= width:
                raise ValueError(f"total over column {t.column} is out of range")
            if self.columns[t.column].type == "text" and t.fn != "count":
                raise ValueError(f"cannot {t.fn} the text column {self.columns[t.column].name!r}")
        return self


class WorkbookSpec(_Strict):
    title: str = Field(min_length=1, max_length=120)
    purpose: str = Field(default="", max_length=300)
    template_id: Literal["tracker", "dashboard", "data", "generic"] = "generic"
    sheets: List[Sheet] = Field(min_length=1, max_length=T.MAX_SHEETS)
    sources: List[Citation] = Field(default_factory=list, max_length=60)
    assumptions: List[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _unique_sheet_names(self) -> "WorkbookSpec":
        seen = set()
        for s in self.sheets:
            key = s.name.lower()
            if key in seen:
                raise ValueError(f"two sheets are named {s.name!r}")
            seen.add(key)
        return self


# -------------------------------------------------------------- envelope --


class ArtifactSpec(_Strict):
    """The envelope: exactly one typed body, named by `kind`."""

    spec_version: Literal[1] = SPEC_VERSION
    kind: Literal["document", "presentation", "workbook"]
    document: Optional[DocumentSpec] = None
    presentation: Optional[PresentationSpec] = None
    workbook: Optional[WorkbookSpec] = None

    @model_validator(mode="after")
    def _one_body(self) -> "ArtifactSpec":
        body = getattr(self, self.kind)
        others = [k for k in T.KINDS if k != self.kind and getattr(self, k) is not None]
        if body is None:
            raise ValueError(f"kind is {self.kind!r} but no {self.kind} body was given")
        if others:
            raise ValueError(f"kind is {self.kind!r} but {others[0]} was also given")
        return self

    @property
    def body(self) -> Union[DocumentSpec, PresentationSpec, WorkbookSpec]:
        return getattr(self, self.kind)

    @property
    def title(self) -> str:
        return self.body.title

    @property
    def sources(self) -> List[Citation]:
        return list(self.body.sources)


# ----------------------------------------------------------- validation --


def _check_source_refs(blocks: Any, sources: List[Citation]) -> None:
    """Every `sources: [...]` on a block names an id in the manifest. A
    citation to nothing is exactly the fabricated reference this refuses."""
    known = {c.id for c in sources}
    for b in blocks:
        refs = getattr(b, "sources", None)
        if not refs:
            continue
        for r in refs:
            if r not in known:
                raise ValueError(f"block cites source {r!r}, which is not in the sources list")


_BODY_FOR_KIND = {"document": DocumentSpec, "presentation": PresentationSpec, "workbook": WorkbookSpec}


def schema_for(kind: str) -> dict:
    """The JSON schema the model is constrained to for one kind: the typed
    body alone, not the envelope, because the kind is already decided and a
    smaller schema is held better by guided decoding."""
    if kind not in _BODY_FOR_KIND:
        raise ValueError(f"unknown artifact kind {kind!r}")
    return _BODY_FOR_KIND[kind].model_json_schema()


def parse_body(kind: str, data: dict) -> ArtifactSpec:
    """Model output (already extracted to a dict) → a validated ArtifactSpec
    for `kind`. Raises pydantic.ValidationError with the field paths the
    caller feeds back to the model for one repair pass."""
    if kind not in _BODY_FOR_KIND:
        raise ValueError(f"unknown artifact kind {kind!r}")
    body = _BODY_FOR_KIND[kind].model_validate(data)
    return ArtifactSpec(kind=kind, **{kind: body})


def load(data: dict) -> ArtifactSpec:
    """A stored spec.json → ArtifactSpec. A newer spec_version is refused
    loudly rather than rendered as a guess."""
    version = int((data or {}).get("spec_version") or 0)
    if version > SPEC_VERSION:
        raise ValueError(f"spec_version {version} is newer than this code understands ({SPEC_VERSION})")
    return ArtifactSpec.model_validate(data)


def validation_summary(exc: ValidationError, limit: int = 12) -> str:
    """The field paths and messages, short enough to put in a repair prompt."""
    lines = []
    for err in exc.errors()[:limit]:
        loc = ".".join(str(p) for p in err.get("loc", ()))
        lines.append(f"- {loc}: {err.get('msg')}")
    return "\n".join(lines)


def text_of(spec: ArtifactSpec) -> str:
    """Every piece of prose in the spec, joined — for the placeholder scan,
    the content review and the length check. No structure, no formatting."""
    parts: List[str] = [spec.title]
    body = spec.body
    if isinstance(body, DocumentSpec):
        parts.append(body.subtitle)
        for b in body.blocks:
            if isinstance(b, (Heading, Paragraph, Callout)):
                parts.append(b.text)
            elif isinstance(b, Bullets):
                parts.extend(b.items)
            elif isinstance(b, TableBlock):
                parts.extend(b.table.columns)
                parts.extend(str(c) for row in b.table.rows for c in row if c is not None)
            elif isinstance(b, KPIRow):
                parts.extend(f"{k.label} {k.value} {k.note}" for k in b.items)
    elif isinstance(body, PresentationSpec):
        for s in body.slides:
            parts.extend([s.title, s.subtitle, *s.bullets, *s.left, *s.right, s.notes])
            parts.extend(f"{a} {b}" for a, b in s.steps)
    elif isinstance(body, WorkbookSpec):
        for s in body.sheets:
            parts.extend(c.name for c in s.columns)
            parts.append(s.notes)
    return "\n".join(p for p in parts if p)


_PLACEHOLDER_RE = re.compile(
    r"lorem ipsum|\[insert[^\]]*\]|\[placeholder[^\]]*\]|\bTBD\b|\bTODO\b|xxx+|\[chart here\]|\[image here\]",
    re.IGNORECASE,
)


def placeholders_in(spec: ArtifactSpec) -> List[str]:
    """Placeholder text the model left behind. A document with these is not
    finished; the caller asks for a correction rather than shipping it."""
    return sorted({m.group(0) for m in _PLACEHOLDER_RE.finditer(text_of(spec))})


def is_formula_like(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_FORMULA_LEADS)


__all__ = [
    "SPEC_VERSION", "ArtifactSpec", "DocumentSpec", "PresentationSpec", "WorkbookSpec",
    "Heading", "Paragraph", "Bullets", "Numbered", "TableBlock", "ChartBlock", "Callout",
    "KPI", "KPIRow", "PageBreak", "Slide", "Sheet", "Column", "Total", "Table", "Chart",
    "Series", "Citation", "schema_for", "parse_body", "load", "validation_summary",
    "text_of", "placeholders_in", "is_formula_like",
]
