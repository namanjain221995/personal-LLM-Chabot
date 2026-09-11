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


#: Characters XML cannot carry (Open XML refuses the file; WeasyPrint drops
#: them silently). Tab, newline and carriage return stay.
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return _XML_ILLEGAL.sub("", value)
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    return value


def _fold(name: str) -> str:
    """A header as a lookup key: case and inner whitespace do not count."""
    return " ".join(str(name).split()).casefold()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _no_illegal_characters(cls, data: Any) -> Any:
        """Every string in every model, before validation: a control
        character the model emitted must not reach a renderer that will
        refuse the whole file for it."""
        return _scrub(data) if isinstance(data, (dict, list)) else data


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
    #: Finite only: NaN and infinity are valid JSON to Python and pydantic,
    #: and crash the chart and PPTX writers.
    values: List[float] = Field(min_length=1, max_length=T.MAX_CHART_POINTS)

    @field_validator("values")
    @classmethod
    def _finite(cls, v: List[float]) -> List[float]:
        import math

        if any(not math.isfinite(x) for x in v):
            raise ValueError("chart values must be finite numbers")
        return v


class Chart(_Strict):
    """A chart the renderers draw from DATA in the spec — categories and
    numeric series — never from an image or a description. `bar`, `line`,
    `pie` and `horizontal_bar` are what every renderer supports natively
    (matplotlib for PDF/DOCX, native charts in PPTX/XLSX)."""

    type: Literal["bar", "horizontal_bar", "line", "pie"] = "bar"
    title: str = Field(default="", max_length=120)
    categories: List[str] = Field(min_length=1, max_length=T.MAX_CHART_POINTS)

    @field_validator("categories")
    @classmethod
    def _category_text(cls, v: List[str]) -> List[str]:
        # A label, not a paragraph: matplotlib lays out every character of
        # every tick label, and 200 labels of 10,000 characters cost ~100 s.
        return [_clip(c, 80) or f"#{i + 1}" for i, c in enumerate(v)]
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
        if all(v == 0 for s in self.series for v in s.values):
            # Every value zero is a chart the model emptied rather than
            # filled (the Think deck of 2026-09-11 drew two zero bars after
            # its review); it is refused so the repair drops or fills it.
            raise ValueError("the chart has no data: every value is 0 — fill it from the material or leave the chart out")
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
    the model names the column and the function; it never writes `=SUM(`.

    `column` is the column's header text, or its position. The e2e run of
    2026-09-11 lost a workbook to `total over column 5 is out of range`: the
    model counted the five columns from 1, was told the index was out of
    range, and did it again. A header name has no base to get wrong, so the
    schema asks for that; a position is still accepted, and Sheet._shape
    resolves both to a 0-based index before the renderer sees it.
    """

    column: Union[str, int] = Field(
        description="The header text of the column to total (exactly as written in `columns`), or its 0-based position.",
    )
    fn: Literal["sum", "average", "count", "min", "max"] = "sum"
    label: str = Field(default="Total", max_length=40)

    @field_validator("column", mode="before")
    @classmethod
    def _column(cls, v: Any) -> Union[str, int]:
        # Before coercion: a bool would otherwise pass as the position 0/1.
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ValueError("a column is a header name or a position")
        if isinstance(v, int):
            if v < 0:
                raise ValueError("a column position cannot be negative")
            return v
        name = v.strip()
        if not name or len(name) > 80:
            raise ValueError("a column name must be 1-80 characters")
        return name


# --- code-made rows ------------------------------------------------------------
#
# WHY. A 500-row dataset typed by the model costs ~53 tokens a row (24,067
# tokens for 500×8 on the live tokenizer), is cut off at Fast's ceiling
# around row 225, and is re-emitted whole on every correction (discovery
# of 2026-09-12, C6). A pasted 30-row audit table arrived flattened, with
# its blank cells lost, because the model retyped it (C5). Neither is a
# job for a language model: rows that already exist are COPIED by code
# (`rows_from`), and rows that are to be invented are GENERATED by code
# from a column recipe the model writes once (`generator`). The model's
# answer then carries no rows; the composer fills them before validation
# and the same Sheet validates in both states.

GenKind = Literal["id", "name", "email", "choice", "int", "float", "date", "datetime", "text", "derived"]
_UNIQUE_KINDS = ("id", "name", "email", "text")
_NUMERIC_KINDS = ("int", "float", "derived")


class OnlyWhen(_Strict):
    """The cell is filled only when another column's value is one of
    `in`; otherwise it is blank. "completion_time only when status is
    Completed"."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    column: str = Field(min_length=1, max_length=80, description="The header text of the column the condition reads.")
    in_: List[str] = Field(alias="in", min_length=1, max_length=200, description="The values of that column for which this cell is filled; any other value leaves it blank.")


class Derived(_Strict):
    """A cell computed from other columns of the same row: `sum`, `mean`,
    `min`, `max` over numeric columns, `diff` of exactly two, `concat` of
    any columns' text."""

    op: Literal["sum", "mean", "min", "max", "diff", "concat"]
    columns: List[str] = Field(min_length=1, max_length=20, description="Header texts of the columns the value is computed from, in order.")


class TextPool(_Strict):
    pool: List[str] = Field(min_length=1, max_length=500, description="Phrases to draw from at random; each 1-300 characters.")

    @field_validator("pool")
    @classmethod
    def _pool_text(cls, v: List[str]) -> List[str]:
        items = [_clip(i, 300) for i in v if (i or "").strip()]
        if not items:
            raise ValueError("a text pool needs at least one phrase")
        return items


class GenColumn(_Strict):
    """One column's recipe. `kind` says what the cells are; the fields
    that do not apply to the kind are ignored, and the ones a kind needs
    are checked below so the error names the column."""

    name: str = Field(min_length=1, max_length=80, description="The header text, exactly as in the sheet's `columns`.")
    kind: GenKind = Field(default="text", description="id (a numbered pattern), name (a realistic person name), email (from the row's name), choice (one of `values`, weighted), int / float (between `min` and `max`), date / datetime (between `start` and `end`, ISO), text (one of `text.pool`), derived (computed from other columns).")
    pattern: Optional[str] = Field(default=None, max_length=80, description='For id: a Python format with `n` as the 1-based row number, e.g. "CAND-{n:04d}".')
    values: List[str] = Field(default_factory=list, max_length=200, description="For choice: the possible values.")
    weights: List[float] = Field(default_factory=list, max_length=200, description="For choice: one non-negative weight per value (optional; equal when omitted).")
    min: Optional[float] = Field(default=None, description="For int / float: the smallest value (inclusive).")
    max: Optional[float] = Field(default=None, description="For int / float: the largest value (inclusive).")
    decimals: int = Field(default=2, ge=0, le=6, description="For float: decimal places.")
    start: Optional[str] = Field(default=None, max_length=40, description="For date / datetime: the earliest, ISO 8601 (2026-01-01 or 2026-01-01T09:00).")
    end: Optional[str] = Field(default=None, max_length=40, description="For date / datetime: the latest, ISO 8601.")
    unique: bool = Field(default=False, description="Every row gets a different value (id, name, email, text only).")
    only_when: Optional[OnlyWhen] = Field(default=None, description="Fill this cell only when another column's value is one of a list; otherwise leave it blank.")
    derived: Optional[Derived] = Field(default=None, description="For derived: the operation and the columns it reads.")
    text: Optional[TextPool] = Field(default=None, description="For text: the phrases to draw from.")

    @field_validator("values")
    @classmethod
    def _values_text(cls, v: List[str]) -> List[str]:
        return [_clip(i, 120) for i in v]

    @model_validator(mode="after")
    def _kind_has_what_it_needs(self) -> "GenColumn":
        who = f"generator column {self.name!r}"
        if self.kind == "choice":
            if not self.values:
                raise ValueError(f"{who} is a choice and needs `values`")
            if self.weights and len(self.weights) != len(self.values):
                raise ValueError(f"{who} has {len(self.weights)} weights for {len(self.values)} values")
            if self.weights and (any(w < 0 for w in self.weights) or sum(self.weights) <= 0):
                raise ValueError(f"{who}: weights must be non-negative and not all zero")
        if self.kind in ("int", "float") and self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"{who}: min {self.min:g} is above max {self.max:g}")
        if self.kind in ("date", "datetime"):
            lo, hi = _iso_moment(self.start, who, "start"), _iso_moment(self.end, who, "end")
            if lo is not None and hi is not None and lo > hi:
                raise ValueError(f"{who}: start {self.start} is after end {self.end}")
        if self.kind == "derived" and self.derived is None:
            raise ValueError(f"{who} is derived and needs `derived: {{op, columns}}`")
        if self.kind == "text" and self.text is None:
            raise ValueError(f"{who} is text and needs `text: {{pool: [...]}}`")
        if self.kind == "id" and self.pattern:
            try:
                self.pattern.format(n=1)
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(f"{who}: pattern {self.pattern!r} must use only {{n}} ({exc})") from exc
        if self.unique and self.kind not in _UNIQUE_KINDS:
            raise ValueError(f"{who}: unique is only meaningful for {', '.join(_UNIQUE_KINDS)} columns; use an id pattern for a unique key")
        return self


def _iso_moment(value: Optional[str], who: str, what: str) -> Optional[str]:
    """An ISO date or datetime as a comparable string (dates become
    midnight), or None. Refuses anything datetime.fromisoformat cannot
    read, so the generator never meets a format it has to guess."""
    if value is None or not value.strip():
        return None
    import datetime as _dt

    text = value.strip()
    try:
        moment = _dt.datetime.fromisoformat(text) if len(text) > 10 else _dt.datetime.combine(_dt.date.fromisoformat(text), _dt.time())
    except ValueError as exc:
        raise ValueError(f"{who}: {what} {value!r} is not an ISO date (2026-01-31) or datetime (2026-01-31T09:00)") from exc
    return moment.replace(tzinfo=None).isoformat()


class Generator(_Strict):
    """Rows made by code, deterministically: exactly `rows` rows from
    `random.Random(seed)`, one recipe per column. The recipes name the
    sheet's columns, in the sheet's order; a recipe that reads another
    column (`only_when`, `derived`) names one that exists."""

    rows: int = Field(ge=1, le=T.MAX_ROWS_PER_SHEET, description=f"How many data rows to generate (1-{T.MAX_ROWS_PER_SHEET}); the person's count when they gave one.")
    seed: int = Field(default=42, description="The random seed; the same seed gives the same rows.")
    columns: List[GenColumn] = Field(min_length=1, max_length=T.MAX_COLUMNS_PER_SHEET, description="One recipe per sheet column, same names, same order.")

    @model_validator(mode="after")
    def _references_resolve(self) -> "Generator":
        by_name: Dict[str, GenColumn] = {}
        for c in self.columns:
            key = _fold(c.name)
            if key in by_name:
                raise ValueError(f"two generator columns are named {c.name!r}")
            by_name[key] = c
        for c in self.columns:
            if c.only_when is not None:
                other = by_name.get(_fold(c.only_when.column))
                if other is None:
                    raise ValueError(f"generator column {c.name!r}: only_when names no column {c.only_when.column!r}")
                if other is c:
                    raise ValueError(f"generator column {c.name!r}: only_when cannot read itself")
            if c.derived is not None:
                for ref in c.derived.columns:
                    other = by_name.get(_fold(ref))
                    if other is None:
                        raise ValueError(f"generator column {c.name!r}: derived names no column {ref!r}")
                    if other is c:
                        raise ValueError(f"generator column {c.name!r}: derived cannot read itself")
                    if c.derived.op != "concat" and other.kind not in _NUMERIC_KINDS:
                        raise ValueError(f"generator column {c.name!r}: {c.derived.op} over {ref!r}, which is {other.kind}, not a number")
                if c.derived.op == "diff" and len(c.derived.columns) != 2:
                    raise ValueError(f"generator column {c.name!r}: diff takes exactly two columns")
        return self


class Rewrite(_Strict):
    """A text column the composer rewrites row by row after the rows are
    filled — "concise professional audit comment" — one in, one out;
    timestamps and quoted text in the original survive or the row keeps
    its original. The rows themselves are never retyped by the model."""

    column: str = Field(min_length=1, max_length=80, description="The header text of the column to rewrite.")
    instruction: str = Field(min_length=1, max_length=300, description="How to rewrite each cell, in a phrase.")


class Highlight(_Strict):
    column: str = Field(min_length=1, max_length=80, description="The header text of the column whose cells are highlighted.")
    color: Literal["red", "amber", "green", "blue"] = "amber"


class SheetStyle(_Strict):
    """How the sheet LOOKS in the Excel, Word and PDF files; a CSV carries
    none of it. `orientation: auto` is landscape when the sheet has more
    than six columns (the tabular document, CONTRACT-2 §4)."""

    borders: Literal["thin", "none"] = "thin"
    header_bold: bool = True
    header_fill: Literal["dark", "light", "none"] = "dark"
    highlight: List[Highlight] = Field(default_factory=list, max_length=T.MAX_COLUMNS_PER_SHEET)
    wrap: bool = False
    orientation: Literal["landscape", "portrait", "auto"] = "auto"


class Sheet(_Strict):
    """One sheet. Its rows come from ONE of three places: the model's
    `rows`; a material table named by `rows_from`, copied by code; or a
    `generator`, made by code — and with either of the last two the model
    leaves `rows` empty. The composer fills them before validation, and
    keeps `rows_from`/`generator` on the filled sheet as provenance, so a
    Sheet is valid both before and after the fill."""

    name: str = Field(min_length=1, max_length=31)
    columns: List[Column] = Field(min_length=1, max_length=T.MAX_COLUMNS_PER_SHEET)
    rows: List[List[Union[str, float, int, None]]] = Field(default_factory=list, max_length=T.MAX_ROWS_PER_SHEET)
    totals: List[Total] = Field(default_factory=list, max_length=T.MAX_COLUMNS_PER_SHEET)
    freeze_header: bool = True
    autofilter: bool = True
    charts: List[Chart] = Field(
        default_factory=list, max_length=4,
        description="Charts over this sheet's rows: `categories` are the cells of the label column in row order (or just that column's header, and the cells are filled in), each series is named after a numeric column and carries that column's cells in row order (or no `values`, and they are filled in).",
    )
    notes: str = Field(default="", max_length=2000)
    rows_from: Optional[str] = Field(
        default=None, max_length=40,
        description="The id of a table in the material whose rows this sheet is: code copies every row verbatim, blanks included. Leave `rows` empty when this is set; never retype the rows.",
    )
    generator: Optional[Generator] = Field(
        default=None,
        description="A recipe for code to generate the rows (a dataset that does not exist yet). Leave `rows` empty when this is set; the rows are made deterministically from `seed`.",
    )
    rewrite: List[Rewrite] = Field(
        default_factory=list, max_length=T.MAX_COLUMNS_PER_SHEET,
        description="Text columns to rewrite cell by cell after the rows are filled (an audit comment made concise); every other cell is kept exactly.",
    )
    style: Optional[SheetStyle] = Field(default=None, description="Borders, header, highlighted columns, wrapping and page orientation for the Excel/Word/PDF files.")

    @model_validator(mode="before")
    @classmethod
    def _charts_from_columns(cls, data: Any) -> Any:
        """A sheet chart is drawn over the sheet's own rows, and the model
        keeps writing it that way — `categories: ["Plan"]` (the header, not
        the cells) with three values per series (the e2e run of 2026-09-11:
        "series 'Monthly Revenue' has 3 values for 1 categories", twice).
        Before Chart validates, a header named where cells belong is
        replaced by the cells, and a series with no values named after a
        column gets that column's cells. Anything else is left for the
        validator to refuse."""
        if not isinstance(data, dict):
            return data
        charts, columns, rows = data.get("charts"), data.get("columns"), data.get("rows")
        if not (isinstance(charts, list) and charts and isinstance(columns, list) and isinstance(rows, list) and rows):
            return data
        names: Dict[str, int] = {}
        for j, c in enumerate(columns):
            n = c.get("name") if isinstance(c, dict) else None
            if isinstance(n, str) and n.strip():
                names.setdefault(_fold(n), j)

        # A chart over a big sheet is drawn over its first MAX_CHART_POINTS
        # rows: the Chart validator refuses more, and the renderer
        # aggregates a long sheet itself. Below the cap nothing changes.
        head = rows[: T.MAX_CHART_POINTS]

        def cells(j: int) -> List[Any]:
            return [r[j] if isinstance(r, list) and j < len(r) else None for r in head]

        def numeric(vals: List[Any]) -> Optional[List[float]]:
            out: List[float] = []
            for v in vals:
                if isinstance(v, bool):
                    return None
                if isinstance(v, (int, float)):
                    out.append(float(v))
                elif v is None or (isinstance(v, str) and not v.strip()):
                    out.append(0.0)
                else:
                    try:
                        out.append(float(str(v).replace(",", "").lstrip("$€£").rstrip("%")))
                    except ValueError:
                        return None
            return out

        fixed_charts: List[Any] = []
        for ch in charts:
            if not isinstance(ch, dict):
                fixed_charts.append(ch)
                continue
            ch = dict(ch)
            cats = ch.get("categories")
            header = cats if isinstance(cats, str) else (cats[0] if isinstance(cats, list) and len(cats) == 1 and isinstance(cats[0], str) else None)
            if header is not None and _fold(header) in names and len(rows) > 1:
                ch["categories"] = ["" if v is None else str(v) for v in cells(names[_fold(header)])]
            n = len(ch["categories"]) if isinstance(ch.get("categories"), list) else 0
            series = ch.get("series")
            if isinstance(series, list) and n == len(head):
                fixed_series: List[Any] = []
                for s in series:
                    if isinstance(s, dict) and not s.get("values") and isinstance(s.get("name"), str) and _fold(s["name"]) in names:
                        vals = numeric(cells(names[_fold(s["name"])]))
                        if vals is not None:
                            s = {**s, "values": vals}
                    fixed_series.append(s)
                ch["series"] = fixed_series
            fixed_charts.append(ch)
        return {**data, "charts": fixed_charts}

    @field_validator("name")
    @classmethod
    def _sheet_name(cls, v: str) -> str:
        # Excel forbids these in a sheet name, and one that starts or ends
        # with an apostrophe cannot be referenced from a formula (the
        # dashboard's KPI cells point at the data sheet); strip rather than
        # refuse. "History" is reserved by Excel.
        cleaned = re.sub(r"[\[\]\*\?/\\:]", " ", v).strip().strip("'").strip()[:31].strip()
        if cleaned.lower() == "history":
            cleaned = "History data"
        return cleaned or "Sheet"

    @field_validator("rows_from")
    @classmethod
    def _rows_from_shape(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        if not _SOURCE_ID_RE.match(v):
            raise ValueError("rows_from must be the id of a material table (letters, digits, _ or -)")
        return v

    @property
    def rows_are_code_made(self) -> bool:
        """The rows come from a material table or a generator — copied or
        made by code, never typed by the model."""
        return bool(self.rows_from) or self.generator is not None

    @model_validator(mode="after")
    def _shape(self) -> "Sheet":
        width = len(self.columns)
        for i, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(f"sheet {self.name!r} row {i + 1} has {len(row)} cells for {width} columns")
        if self.rows_from and self.generator is not None:
            raise ValueError(f"sheet {self.name!r} has both rows_from and a generator; rows come from one place")
        self._resolve_totals(width)
        for t in self.totals:
            if self.columns[t.column].type == "text" and t.fn != "count":
                raise ValueError(f"cannot {t.fn} the text column {self.columns[t.column].name!r}")
        by_name = {}
        for c in self.columns:
            by_name.setdefault(_fold(c.name), c)
        if self.generator is not None:
            self._align_generator(by_name)
        for r in self.rewrite:
            if _fold(r.column) not in by_name:
                raise ValueError(f"sheet {self.name!r}: rewrite names no column {r.column!r}; the columns are {self._column_names()}")
        if self.style is not None:
            for h in self.style.highlight:
                if _fold(h.column) not in by_name:
                    raise ValueError(f"sheet {self.name!r}: highlight names no column {h.column!r}; the columns are {self._column_names()}")
        return self

    def _column_names(self) -> str:
        return ", ".join(repr(c.name) for c in self.columns)

    def _align_generator(self, by_name: Dict[str, Column]) -> None:
        """The generator's recipes ARE the sheet's columns: one each, the
        same names. Given in another order they are put in the sheet's
        order (a correction, not a refusal); a missing or an extra one is
        refused, because the rows would not be rectangular."""
        gen = self.generator
        assert gen is not None
        recipes = {_fold(c.name): c for c in gen.columns}
        missing = [c.name for c in self.columns if _fold(c.name) not in recipes]
        extra = [c.name for c in gen.columns if _fold(c.name) not in by_name]
        if missing or extra:
            what = []
            if missing:
                what.append(f"no recipe for column{'s' if len(missing) > 1 else ''} {', '.join(repr(m) for m in missing)}")
            if extra:
                what.append(f"recipe{'s' if len(extra) > 1 else ''} for {', '.join(repr(e) for e in extra)}, which the sheet has no column for")
            raise ValueError(f"sheet {self.name!r}: the generator must have one recipe per column — {'; '.join(what)}")
        gen.columns = [recipes[_fold(c.name)] for c in self.columns]

    def _resolve_totals(self, width: int) -> None:
        """Every total's `column` becomes a 0-based index. A header name is
        matched to a column (case-insensitively, whitespace-insensitively);
        positions given from 1 — recognisable ONLY when one of them is
        exactly one past the end and none is 0 — are shifted as a set, since
        shifting some and not others would total the wrong columns."""
        by_name = {}
        for i, c in enumerate(self.columns):
            by_name.setdefault(_fold(c.name), i)
        positions = [t.column for t in self.totals if isinstance(t.column, int)]
        from_one = bool(positions) and max(positions) == width and min(positions) >= 1
        for t in self.totals:
            if isinstance(t.column, int):
                idx = t.column - 1 if from_one else t.column
                if idx >= width:
                    raise ValueError(
                        f"total over column {t.column} is out of range: this sheet has {width} columns; "
                        "name the column by its header text"
                    )
                t.column = idx
            else:
                idx = by_name.get(_fold(t.column))
                if idx is None:
                    names = ", ".join(repr(c.name) for c in self.columns)
                    raise ValueError(f"total over column {t.column!r} names no column; the columns are {names}")
                t.column = idx


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
            parts.extend(f"{k.label} {k.value} {k.note}" for k in s.kpis)
            if s.table is not None:
                parts.extend(s.table.columns)
                parts.extend(str(c) for row in s.table.rows for c in row if c is not None)
    elif isinstance(body, WorkbookSpec):
        for s in body.sheets:
            parts.extend(c.name for c in s.columns)
            parts.append(s.notes)
    return "\n".join(p for p in parts if p)


#: Bracketed phrases are placeholders whatever the word inside: the Think
#: deck of 2026-09-11 came back from its correction with "[Verified Current
#: Monthly Revenue]" in every KPI tile. A bare number in brackets ([1]) is a
#: citation mark and is not matched.
_PLACEHOLDER_RE = re.compile(
    r"lorem ipsum|\[(?!\d+\])[A-Za-z][^\]\n]{1,80}\]|\bTBD\b|\bTODO\b|xxx+",
    re.IGNORECASE,
)


def placeholders_in(spec: ArtifactSpec) -> List[str]:
    """Placeholder text the model left behind. A document with these is not
    finished; the caller asks for a correction rather than shipping it."""
    return sorted({m.group(0) for m in _PLACEHOLDER_RE.finditer(text_of(spec))})


def templates_for(kind: str) -> Tuple[str, ...]:
    """The template ids a kind's body accepts — read from the Literal, so
    the composer's pin can never name one the validator would refuse."""
    body = _BODY_FOR_KIND.get(kind)
    if body is None:
        return ()
    return tuple(body.model_fields["template_id"].annotation.__args__)


def part_count(spec: ArtifactSpec) -> int:
    """Blocks, slides or sheets — the coarse size a correction is held to.
    A workbook counts its sheets and their FILLED rows: a sheet still
    waiting for its copied or generated rows counts as one."""
    body = spec.body
    if isinstance(body, DocumentSpec):
        return len(body.blocks)
    if isinstance(body, PresentationSpec):
        return len(body.slides)
    if isinstance(body, WorkbookSpec):
        return sum(1 + len(s.rows) for s in body.sheets)
    return 0


_BODY_BLOCKS = (Paragraph, Bullets, Numbered, TableBlock, ChartBlock, Callout)


def hollow(spec: ArtifactSpec) -> str:
    """Why the spec has no body, or "" when it has one. A KPI row on an
    empty page (the Think brief of 2026-09-11), a deck of one slide, a
    workbook with no rows: valid to the schema, useless to the reader."""
    body = spec.body
    if isinstance(body, DocumentSpec):
        if not any(isinstance(b, _BODY_BLOCKS) for b in body.blocks):
            return "it has no paragraphs, lists, tables or charts — only headings or headline numbers"
    elif isinstance(body, PresentationSpec):
        content = [s for s in body.slides if s.layout not in ("title", "section", "closing")]
        if len(content) < 2:
            return "it has fewer than two slides with content"
    elif isinstance(body, WorkbookSpec):
        # A sheet whose rows are still to be copied or generated has its
        # rows: they are code's to fill, not the model's to type.
        if not any(s.rows or s.rows_are_code_made for s in body.sheets):
            return "no sheet has any rows"
    return ""


def is_formula_like(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(_FORMULA_LEADS)


# ---------------------------------------------------------------- figures --
#
# WHY. The first Fast-effort brief of the 2026-09-11 e2e run was asked for
# "$59 a month" and "120 team accounts" and came back with a $49 current
# price, competitors "between $55 and $65", "1,000 active teams" and a 95%
# retention rate — none of them in the material, one of them declared as an
# assumption. Fast has no content review, so the deterministic check is the
# only one it gets: every figure the reader will see is looked up in the
# material, and the ones that are not there are named on the version as a
# warning (Think and Max also hand them to the reviewer). A derived figure
# (120 × $59 = $7,080) is named too — the warning says "not in the
# material", which is true, not "wrong", which it cannot know.

_FIGURE_RE = re.compile(
    r"(?<![\w.])(?P<cur>[$€£]\s?)?(?P<num>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?"
    r"(?:(?P<suf>k|m|mn|bn|b)\b|(?P<pct>\s?(?:%|percent\b)))?(?![\w.]\d)",
    re.IGNORECASE,
)

_SUFFIX_SCALE = {"k": 1_000, "m": 1_000_000, "mn": 1_000_000, "bn": 1_000_000_000, "b": 1_000_000_000}

#: Keys whose numeric values the reader sees (chart values, table cells, KPI
#: values); every other number in the structure is an index or a size.
_FIGURE_VALUE_KEYS = ("values", "rows", "value")


def _figure_core(m: "re.Match[str]") -> str:
    return m.group("num").replace(",", "") + (m.group("dec") or "")


def _figure_cores(m: "re.Match[str]") -> List[str]:
    """The number as written, and — for `410k`, `1.2M`, `3bn` — expanded,
    so a draft's 410,000 is found in a material that said 410k."""
    core = _figure_core(m)
    suffix = (m.group("suf") or "").lower()
    if suffix in _SUFFIX_SCALE:
        return [core, _core_of_number(float(core) * _SUFFIX_SCALE[suffix])]
    return [core]


def _figures_in_text(text: str) -> Dict[str, str]:
    """core → as written, for every number in `text`."""
    out: Dict[str, str] = {}
    for m in _FIGURE_RE.finditer(text or ""):
        for core in _figure_cores(m):
            out.setdefault(core, m.group(0).strip())
    return out


def _qualifies(m: "re.Match[str]") -> bool:
    """A figure worth checking: money, a percentage, a decimal, a count of
    a thousand or more (`410k` included) — not a day count, a step number
    or a year."""
    if m.group("cur") or m.group("pct") or m.group("dec") or m.group("suf"):
        return True
    n = int(m.group("num").replace(",", ""))
    return n >= 1000 and not (1900 <= n <= 2100 and "," not in m.group("num"))


def _core_of_number(n: Union[int, float]) -> str:
    if isinstance(n, float):
        return str(int(n)) if n == int(n) and abs(n) < 1e15 else f"{n:.10g}"
    return str(n)


def _numbers_in_structure(node: Any, key: str = "") -> List[Tuple[List[str], str]]:
    """(cores, as written) for every numeric leaf the reader sees."""
    found: List[Tuple[List[str], str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            found.extend(_numbers_in_structure(v, k))
    elif isinstance(node, list):
        for v in node:
            found.extend(_numbers_in_structure(v, key))
    elif isinstance(node, bool):
        return found
    elif isinstance(node, (int, float)) and key in _FIGURE_VALUE_KEYS:
        core = _core_of_number(node)
        found.append(([core], core))
    elif isinstance(node, str) and key in _FIGURE_VALUE_KEYS:
        found.extend((_figure_cores(m), m.group(0).strip()) for m in _FIGURE_RE.finditer(node))
    return found


def _model_made(body: Any) -> Any:
    """The body as a dict with the numbers the model did NOT write left
    out: the rows of a sheet copied from a material table or generated by
    code (and the totals and charts drawn over them) are code's, and a
    500-row generated dataset would otherwise be 500 rows of "figures the
    material never gave"."""
    dump = body.model_dump()
    if isinstance(body, WorkbookSpec):
        for sheet, raw in zip(body.sheets, dump.get("sheets") or []):
            if sheet.rows_are_code_made and isinstance(raw, dict):
                raw["rows"] = []
                raw["charts"] = []
                raw["generator"] = None
    return dump


def unsupported_figures(spec: ArtifactSpec, material_text: str) -> List[str]:
    """Figures in the spec that appear nowhere in `material_text` (the
    instruction, the conversation, the uploads, the sources and the tables,
    joined) and are not declared in the spec's assumptions — as written, in
    document order, deduplicated. Empty means every figure was given."""
    known = set(_figures_in_text(material_text))
    body = spec.body
    known.update(_figures_in_text("\n".join(getattr(body, "assumptions", None) or [])))
    seen: List[str] = []
    cores: set = set()
    for m in _FIGURE_RE.finditer(text_of(spec)):
        mine = _figure_cores(m)
        core = mine[-1]
        if any(c in known for c in mine) or core in cores or not _qualifies(m):
            continue
        cores.add(core)
        seen.append(m.group(0).strip())
    for mine, written in _numbers_in_structure(_model_made(body)):
        core = mine[-1]
        if any(c in known for c in mine) or core in cores:
            continue
        try:
            value = float(core)
        except ValueError:
            continue
        if written != core or value >= 1000 or value != int(value):
            cores.add(core)
            seen.append(written)
    return seen


__all__ = [
    "SPEC_VERSION", "ArtifactSpec", "DocumentSpec", "PresentationSpec", "WorkbookSpec",
    "Heading", "Paragraph", "Bullets", "Numbered", "TableBlock", "ChartBlock", "Callout",
    "KPI", "KPIRow", "PageBreak", "Slide", "Sheet", "Column", "Total", "Table", "Chart",
    "Series", "Citation", "Generator", "GenColumn", "OnlyWhen", "Derived", "TextPool", "Rewrite",
    "Highlight", "SheetStyle", "schema_for", "parse_body", "load", "validation_summary",
    "text_of", "placeholders_in", "is_formula_like", "unsupported_figures", "templates_for", "part_count", "hollow",
]
