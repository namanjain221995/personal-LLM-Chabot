"""Charts → PNG and SVG with matplotlib (Agg), from COMPUTED values only.

WHAT IT DRAWS. Every chart_spec type (20) from a resolved Chart v2 — the
numbers chart_data computed from a bound table — or a legacy spec.Chart
(literal numbers in an old spec.json, converted with chart_spec.from_legacy).
PNG is embedded in DOCX (200 dpi at the content width) and is a standalone
download; SVG goes to the PDF/HTML path and is a standalone download.

DEFAULTS AND OVERRIDES. The look comes from ResolvedStyle.chart_defaults when
the styling track provides one (duck-typed: palette, font_family,
title_size_pt, axis_size_pt, grid_color, axis_text_color, label_color_for),
else from ChartStyleDefaults below (the style guide's values). A chart's own
ChartStyle (colours, fonts, sizes, legend, labels, axis ranges) wins.

READABILITY RULES (style guide §6). Bars start at zero unless y_min is set.
Data labels sit OUTSIDE bars in ink; inside only when the label colour
reaches 4.5:1 on that bar and the bar is tall enough. Pie/donut slice labels
are white or ink, whichever reaches 4.5:1 on the slice. Line charts with 3+
series get distinct markers and dash styles (grayscale print); more than 5
series get direct end labels instead of a legend. Thousands separators, and
Indian grouping for currency_INR.

DETERMINISM AND SAFETY. PNG metadata is pinned; SVG uses svg.fonttype='path'
(text becomes outlines: no font dependency and no text node a viewer could
interpret), a fixed svg.hashsalt, no metadata block and no DOCTYPE, so two
renders are byte-identical and the output passes validate.validate_svg_bytes.
Mathtext is off: finance text full of dollars is text, not TeX.

pyplot is imported lazily (tests/test_imports.py): importing this module
costs nothing.
"""
from __future__ import annotations

import io
import math
import re
import textwrap
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import chart_colours as CC
from .. import chart_spec as CS

INK = "#1F2937"
WHITE = "#FFFFFF"
MUTED = "#5F6B7A"
GRID = CC.GRID
NAVY = "#1F3864"
DPI_EMBED = 200
PORTRAIT_WIDTH_IN = 6.3
LANDSCAPE_WIDTH_IN = 9.7
LABEL_MAX_CATEGORIES = 12

#: Tried in order for every glyph (matplotlib >= 3.6 falls back per glyph).
FONT_FALLBACKS: Tuple[str, ...] = (
    "Carlito", "Calibri", "Liberation Sans", "DejaVu Sans",
)

#: Script → (sample character range, candidate font families).
SCRIPT_FONTS: Dict[str, Tuple[Tuple[int, int], Tuple[str, ...]]] = {
    "Devanagari": ((0x0900, 0x097F), ("Noto Sans Devanagari", "Lohit Devanagari", "Samyak Devanagari", "Kalimati", "Nirmala UI", "Mangal")),
    "Gujarati": ((0x0A80, 0x0AFF), ("Noto Sans Gujarati", "Lohit Gujarati", "Samyak Gujarati", "Rekha", "Nirmala UI", "Shruti")),
}

_MARKERS = ("o", "s", "^", "D", "v", "P", "X", "*")
_DASHES = ("-", "--", "-.", ":", (0, (5, 1)), (0, (3, 1, 1, 1, 1, 1)), (0, (1, 1)), (0, (8, 2, 2, 2)))


# ------------------------------------------------------------------ colour --


def _rgb(hex_colour: str) -> Tuple[float, float, float]:
    h = hex_colour.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _luminance(hex_colour: str) -> float:
    def ch(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (ch(c) for c in _rgb(hex_colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: str, b: str) -> float:
    """WCAG 2 contrast ratio of two '#RRGGBB' colours."""
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _mix(hex_colour: str, towards: str, amount: float) -> str:
    """`hex_colour` blended `amount` (0..1) of the way to `towards`."""
    a, b = _rgb(hex_colour), _rgb(towards)
    return _to_hex(tuple(a[i] + (b[i] - a[i]) * amount for i in range(3)))


def _worst_ratio(row: Sequence[float], side: float) -> float:
    """The worst width/height ratio of a row of areas laid along `side`."""
    total = sum(row)
    if total <= 0 or side <= 0:
        return float("inf")
    hi, lo = max(row), min(row)
    if lo <= 0:
        return float("inf")
    return max(side * side * hi / (total * total), total * total / (side * side * lo))


def squarified(values: Sequence[float], x: float, y: float, width: float, height: float) -> List[Tuple[float, float, float, float]]:
    """Bruls, Huizing & van Wijk's squarified treemap: one (x, y, w, h) per
    value, area-proportional and as close to square as the order allows.

    Written here rather than taken from a package because the whole chart
    path has no plotting dependency beyond matplotlib, and because an area
    that is not proportional to its value is a lie a library would hide.
    Values must be positive and in descending order (chart_data sorts them);
    a non-positive value has no area and is dropped by the caller.
    """
    total = float(sum(values))
    if total <= 0 or width <= 0 or height <= 0:
        return []
    scale = width * height / total
    remaining = [float(v) * scale for v in values]
    out: List[Tuple[float, float, float, float]] = []
    while remaining:
        side = min(width, height)
        row = [remaining[0]]
        taken = 1
        while taken < len(remaining) and _worst_ratio(row + [remaining[taken]], side) <= _worst_ratio(row, side):
            row.append(remaining[taken])
            taken += 1
        area = sum(row)
        if width >= height:
            thickness = area / height if height else 0.0
            oy = y
            for a in row:
                h = a / thickness if thickness else 0.0
                out.append((x, oy, thickness, h))
                oy += h
            x += thickness
            width -= thickness
        else:
            thickness = area / width if width else 0.0
            ox = x
            for a in row:
                w = a / thickness if thickness else 0.0
                out.append((ox, y, w, thickness))
                ox += w
            y += thickness
            height -= thickness
        remaining = remaining[taken:]
    return out


def _to_hex(colour: Any) -> str:
    if isinstance(colour, str) and colour.startswith("#") and len(colour) == 7:
        return colour.upper()
    r, g, b = colour[:3]
    return "#{:02X}{:02X}{:02X}".format(int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


@dataclass(frozen=True)
class ChartStyleDefaults:
    """The style guide's chart tokens; style.ResolvedStyle.chart_defaults
    supersedes it when the styling track is merged."""

    palette: Tuple[str, ...] = CS.DEFAULT_PALETTE
    font_family: str = "Carlito"
    title_size_pt: float = 12.0
    axis_size_pt: float = 10.0
    grid_color: str = GRID
    axis_text_color: str = MUTED
    ink: str = INK
    background: str = WHITE

    def label_color_for(self, fill_hex: str) -> str:
        """White or ink, whichever contrasts more with `fill_hex`."""
        return WHITE if contrast_ratio(WHITE, fill_hex) >= contrast_ratio(self.ink, fill_hex) else self.ink


def defaults_from(resolved: Any) -> ChartStyleDefaults:
    cd = getattr(resolved, "chart_defaults", None) if resolved is not None else None
    if cd is None:
        return ChartStyleDefaults()
    if isinstance(cd, ChartStyleDefaults):
        return cd
    base = ChartStyleDefaults()
    kw = {}
    for name in ("palette", "font_family", "title_size_pt", "axis_size_pt", "grid_color", "axis_text_color", "ink", "background"):
        value = getattr(cd, name, None)
        if value:
            kw[name] = tuple(value) if name == "palette" else value
    return ChartStyleDefaults(**{**base.__dict__, **kw})


# ------------------------------------------------------------------ numbers --


def indian_group(n: int) -> str:
    s = str(abs(int(n)))
    if len(s) <= 3:
        out = s
    else:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        out = ",".join(parts) + "," + tail
    return ("-" if n < 0 else "") + out


def format_value(value: float, number_format: Optional[str], *, fraction_percent: bool = False) -> str:
    """One number as a label. `fraction_percent`: the series holds 0..1."""
    v = float(value)
    fmt = number_format or ""
    integral = abs(v - round(v)) < 1e-9
    if fmt == "integer":
        return f"{int(round(v)):,}"
    if fmt == "decimal1":
        return f"{v:,.1f}"
    if fmt == "decimal2":
        return f"{v:,.2f}"
    if fmt == "percent":
        p = v * 100 if fraction_percent else v
        return f"{p:.0f}%" if abs(p - round(p)) < 1e-9 else f"{p:.1f}%"
    if fmt == "currency_INR":
        if integral:
            return "₹" + indian_group(int(round(v)))
        whole = int(abs(v))
        return ("-" if v < 0 else "") + "₹" + indian_group(whole) + f"{abs(v) - whole:.2f}"[1:]
    if fmt in ("currency_USD", "currency_EUR", "currency_GBP"):
        sym = {"currency_USD": "$", "currency_EUR": "€", "currency_GBP": "£"}[fmt]
        body = f"{abs(v):,.0f}" if integral else f"{abs(v):,.2f}"
        return ("-" if v < 0 else "") + sym + body
    if fmt == "compact":
        a = abs(v)
        for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
            if a >= div:
                x = v / div
                return (f"{x:.0f}" if abs(x - round(x)) < 0.05 else f"{x:.1f}") + suf
        return f"{v:,.0f}" if integral else f"{v:,.1f}"
    return f"{int(round(v)):,}" if integral else f"{v:,.1f}"


# -------------------------------------------------------------------- fonts --


@lru_cache(maxsize=1)
def _installed_families() -> Dict[str, List[str]]:
    from matplotlib import font_manager

    out: Dict[str, List[str]] = {}
    for f in font_manager.fontManager.ttflist:
        out.setdefault(f.name, []).append(f.fname)
    return out


@lru_cache(maxsize=64)
def _font_covers(path: str, codepoint: int) -> bool:
    try:
        from matplotlib.ft2font import FT2Font

        return codepoint in FT2Font(path).get_charmap()
    except Exception:
        return False


def scripts_in(text: str) -> List[str]:
    found = []
    for script, ((lo, hi), _) in SCRIPT_FONTS.items():
        if any(lo <= ord(ch) <= hi for ch in text):
            found.append(script)
    return found


def font_for_script(script: str) -> Optional[str]:
    (lo, _hi), candidates = SCRIPT_FONTS[script]
    families = _installed_families()
    for name in candidates:
        for path in families.get(name, []):
            if _font_covers(path, lo + 0x15):
                return name
    return None


def _chart_text(chart: CS.Chart) -> str:
    parts = [chart.title, chart.subtitle, chart.x_label, chart.y_label, chart.y2_label, chart.caption, *chart.categories]
    parts.extend(s.name for s in chart.series)
    return " ".join(p for p in parts if p)


def chart_warnings(chart: Any) -> List[str]:
    """One sentence per script the chart's text uses that no installed font
    can draw (the labels would show empty boxes)."""
    c = _as_chart(chart)
    out = []
    for script in scripts_in(_chart_text(c)):
        if font_for_script(script) is None:
            out.append(f"The chart labels use {script} script, and no {script} font is installed on this server, so those labels may show empty boxes.")
    return out


def _mapped(family: str) -> List[str]:
    """A requested family and the families that stand in for it on this
    server, in order (style.FontFace.pdf_candidates: the family, its metric
    twin, its documented open fallback, the generic Liberation/DejaVu), so a
    chart asked for in Georgia draws in the family the PDF uses rather than
    dropping to the default sans."""
    try:
        from .. import style as ST

        face = ST.font_face(family)
    except Exception:  # noqa: BLE001 - the painter never fails on a font
        face = None
    return list(face.pdf_candidates) if face is not None else [family]


def _families(style: Optional[CS.ChartStyle], d: ChartStyleDefaults, text: str) -> List[str]:
    fams: List[str] = []
    if style and style.font_family:
        fams.extend(_mapped(style.font_family))
    fams.extend(_mapped(d.font_family))
    for script in scripts_in(text):
        f = font_for_script(script)
        if f:
            fams.append(f)
    fams.extend(FONT_FALLBACKS)
    seen: List[str] = []
    installed = _installed_families()
    for f in fams:
        if f not in seen and f in installed:
            seen.append(f)
    return seen or ["DejaVu Sans"]


# ------------------------------------------------------------------- entry --


def _as_chart(chart: Any) -> CS.Chart:
    if isinstance(chart, CS.Chart):
        return chart
    try:
        from ..spec import Chart as LegacyChart
    except Exception:  # pragma: no cover
        LegacyChart = None  # type: ignore[assignment]
    if LegacyChart is not None and isinstance(chart, LegacyChart):
        return CS.from_legacy(chart)
    raise TypeError("the chart renderer requires a validated Chart")


def _drawable(c: CS.Chart) -> None:
    if not c.series and not (c.extra and (c.extra.box or c.extra.spans)):
        raise ValueError("the chart has no computed values; resolve it against its table first")


@dataclass
class _Ctx:
    chart: CS.Chart
    style: CS.ChartStyle
    d: ChartStyleDefaults
    ink: str
    muted: str
    bg: str
    fmt: Optional[str]
    fraction_percent: bool
    #: What chart_colours chose for this chart, consulted only after every
    #: explicit field of the chart's own style (chart_colours.PRECEDENCE).
    scheme: CC.Scheme = CC.EMPTY
    warnings: List[str] = field(default_factory=list)
    #: Set by a drawer when every mark it drew carries its own label, so the
    #: value gridlines can come off — the numbers are already on the page.
    labels_complete: bool = False

    def series_colour(self, i: int, name: str = "") -> str:
        s = self.chart.series[i] if i < len(self.chart.series) else None
        if s is not None and s.color:
            return s.color
        if name and name in self.style.series_colors:
            return self.style.series_colors[name]
        if i == 0 and self.style.color and (len(self.chart.series) == 1 or self.chart.type == "combo"):
            return self.style.color
        if self.style.palette:
            return self.style.palette[i % len(self.style.palette)]
        auto = self.scheme.series_colour(i, name)
        if auto:
            return auto
        return self.d.palette[i % len(self.d.palette)]

    def category_colour(self, j: int, label: str) -> str:
        if label in self.style.category_colors:
            return self.style.category_colors[label]
        part = self.chart.type in CS.NO_AXIS_TYPES
        if self.style.color and not part:
            return self.style.color
        if self.style.palette:
            palette = self.style.palette
            return palette[j % len(palette)] if (part or self.scheme.by_category) else palette[0]
        auto = self.scheme.category_colour(j, label)
        if auto:
            return auto
        return self.d.palette[j % len(self.d.palette)] if part else self.d.palette[0]

    def base_colour(self) -> str:
        """The one colour of a chart that paints a single measure (a box, a
        violin, a bullet row) — the drawer varies shade, not hue."""
        if self.style.color:
            return self.style.color
        if self.style.palette:
            return self.style.palette[0]
        return self.scheme.series_colour(0) or self.scheme.category_colour(0) or self.d.palette[0]

    def by_category(self) -> bool:
        """A one-series chart whose BARS take a colour each: the person named
        category colours, or the scheme's rule paints per category."""
        return len(self.chart.series) <= 1 and (bool(self.style.category_colors) or self.scheme.by_category)

    def signed_colours(self) -> Tuple[str, str, str]:
        """(gain, loss, total) for a chart of a signed measure."""
        st = self.style
        gain, loss, total = self.scheme.signed or (self.d.palette[0], self.d.palette[1], NAVY)
        return (st.series_colors.get("Increase") or st.color or gain,
                st.series_colors.get("Decrease") or loss,
                st.series_colors.get("Total") or total)

    def label(self, v: float) -> str:
        return format_value(v, self.fmt, fraction_percent=self.fraction_percent)


def render_png(chart: Any, resolved: Any = None, width_px: Optional[int] = None, height_px: Optional[int] = None, *,
               orientation: str = "portrait", section_heading: str = "", include_caption: bool = False) -> bytes:
    """The chart as PNG bytes. Size: the ChartStyle's width/height/dpi, else
    the content width for `orientation` at 200 dpi, else width_px/height_px."""
    return _render(chart, resolved, "png", width_px, height_px, orientation, section_heading, include_caption)


def render_svg(chart: Any, resolved: Any = None, *, orientation: str = "portrait", section_heading: str = "",
               include_caption: bool = False) -> bytes:
    """The chart as SVG bytes: text as paths, no metadata, no DOCTYPE,
    deterministic ids."""
    return _render(chart, resolved, "svg", None, None, orientation, section_heading, include_caption)


def render_chart_png(chart: Any, out_path: str | Path, resolved: Any = None) -> Path:
    """Back-compat entry the document/deck renderers call: 8 x 4.5 in at 160
    dpi (1280 x 720 before tight layout), written to `out_path`.

    `resolved` is optional only for the callers that have no style to give.
    Pass it whenever one exists: the document's colour plan rides on it
    (`ResolvedStyle.chart_plan`), and without it every chart of a document
    falls back to the chart-by-chart rules and the first palette slot — the
    one blue for revenue, head count and churn alike that this round is
    about."""
    c = _as_chart(chart)
    data = _render(c, resolved, "png", 1280, 720, "portrait", "", False, dpi_override=160, fixed_size=True)
    out = Path(out_path)
    out.write_bytes(data)
    return out


def inspect_figure(chart: Any, fn: Any, resolved: Any = None, **kw: Any) -> bytes:
    """Render to PNG, calling fn(figure) after drawing and before saving —
    for tests that read artists (label colours, markers, dash styles)."""
    return _render(chart, resolved, "png", kw.get("width_px"), kw.get("height_px"), kw.get("orientation", "portrait"),
                   kw.get("section_heading", ""), kw.get("include_caption", False), inspect=fn)


def render_standalone(spec: Any, fmt: str, out_dir: str | Path, resolved: Any = None, *, stem: str = "chart") -> List[Path]:
    """Every chart of `spec` as a standalone `<stem>-<n>.<fmt>` (png or svg)
    in `out_dir`, captions included. Charts without computed values are
    skipped (resolve the spec first)."""
    if fmt not in ("png", "svg"):
        raise ValueError(f"standalone charts are png or svg, not {fmt}")
    out = Path(out_dir)
    stem = re.sub(r"[^a-z0-9-]+", "-", (stem or "chart").lower()).strip("-") or "chart"
    paths: List[Path] = []
    n = 0
    for _path, raw in CS.iter_chart_slots(spec):
        c = raw if isinstance(raw, CS.Chart) else (CS.Chart.model_validate(raw) if isinstance(raw, dict) else _as_chart(raw))
        if not c.series:
            continue
        n += 1
        target = out / f"{stem}-{n}.{fmt}"
        if fmt == "png":
            target.write_bytes(render_png(c, resolved, 1600, 900, include_caption=True))
        else:
            target.write_bytes(render_svg(c, resolved, orientation="landscape", include_caption=True))
        paths.append(target)
    return paths


def _size(c: CS.Chart, width_px: Optional[int], height_px: Optional[int], orientation: str, dpi_override: Optional[int]) -> Tuple[float, float, int]:
    st = c.style
    dpi = dpi_override or (st.dpi if st and st.dpi else DPI_EMBED)
    if st and st.width_in:
        w = st.width_in
    elif width_px:
        w = width_px / dpi
    else:
        w = LANDSCAPE_WIDTH_IN if orientation == "landscape" else PORTRAIT_WIDTH_IN
    if st and st.height_in:
        h = st.height_in
    elif height_px:
        h = height_px / dpi
    else:
        h = max(3.0, min(w * 0.5625, 7.5))
    if c.type in ("pie", "donut", "radar", "sunburst", "treemap") and not (st and st.height_in) and not height_px:
        h = max(h, min(w * 0.62, 7.5))
    if c.type == "gantt" and not (st and st.height_in):
        h = max(h, min(10.0, 1.2 + 0.38 * max(1, len(c.categories))))
    if c.type == "bullet" and not (st and st.height_in):
        # Each row is one measure; a bullet row reads at about half an inch.
        h = max(2.2, min(8.0, 1.0 + 0.52 * max(1, len(c.categories))))
    return w, h, dpi


def _render(chart: Any, resolved: Any, fmt: str, width_px: Optional[int], height_px: Optional[int], orientation: str,
            section_heading: str, include_caption: bool, *, dpi_override: Optional[int] = None, fixed_size: bool = False,
            inspect: Any = None) -> bytes:
    c = _as_chart(chart)
    _drawable(c)
    import matplotlib

    matplotlib.use("Agg", force=True)
    # Every string is a person's or the model's text, never TeX (review
    # 2026-09-11: "Revenue $M vs $K" lost words and "$\\frac$" raised).
    matplotlib.rcParams["text.parse_math"] = False
    import matplotlib.pyplot as plt

    d = defaults_from(resolved)
    style = c.style or CS.ChartStyle()
    bg = style.background or d.background
    ink = d.ink if contrast_ratio(d.ink, bg) >= 4.5 else WHITE
    muted = d.axis_text_color if contrast_ratio(d.axis_text_color, bg) >= 4.5 else ink
    all_values = [v for s in c.series for v in s.values]
    fraction = bool(all_values) and max(abs(v) for v in all_values) <= 1.0 and style.number_format == "percent"
    # The scheme is built on the HOUSE palette: a palette the person asked
    # for wins before the scheme is ever consulted (chart_colours.PRECEDENCE).
    scheme = CC.scheme_for(c, plan=getattr(resolved, "chart_plan", None), palette=d.palette)
    ctx = _Ctx(chart=c, style=style, d=d, ink=ink, muted=muted, bg=bg, fmt=style.number_format,
               fraction_percent=fraction, scheme=scheme)
    w, h, dpi = _size(c, width_px, height_px, orientation, dpi_override)
    rc = {
        "text.parse_math": False,
        "font.family": _families(style, d, _chart_text(c)),
        "svg.fonttype": "path",
        "svg.hashsalt": "techsara-artifact-chart",
        "axes.unicode_minus": False,
        "path.simplify": True,
    }
    with matplotlib.rc_context(rc):
        if c.type == "radar":
            fig = plt.figure(figsize=(w, h))
            ax = fig.add_subplot(111, projection="polar")
        else:
            fig, ax = plt.subplots(figsize=(w, h))
        try:
            fig.patch.set_facecolor(bg)
            ax.set_facecolor(bg)
            drawer = _DRAWERS.get(c.type, _draw_bars)
            legend_handled = drawer(ax, ctx)
            _decorate(fig, ax, ctx, section_heading, include_caption, legend_handled)
            # Rotation first: a bottom legend is placed under the tick
            # labels AS DRAWN, and rotated names are twice as tall.
            _rotate_crowded_ticks(fig)
            _place_bottom_legends(fig)
            _align_titles_left(fig)
            if include_caption and c.caption:
                _caption_below(fig, ctx)
            if inspect is not None:
                fig.canvas.draw()
                inspect(fig)
            buf = io.BytesIO()
            if fmt == "png":
                if fixed_size:
                    fig.tight_layout()
                    fig.savefig(buf, dpi=dpi, format="png", metadata={"Software": None}, facecolor=bg)
                else:
                    fig.savefig(buf, dpi=dpi, format="png", metadata={"Software": None}, facecolor=bg, bbox_inches="tight", pad_inches=0.12)
                return buf.getvalue()
            fig.savefig(buf, format="svg", metadata={"Date": None, "Creator": None, "Format": None, "Type": None},
                        facecolor=bg, bbox_inches="tight", pad_inches=0.12)
            return clean_svg(buf.getvalue())
        finally:
            plt.close(fig)


_DOCTYPE_RE = re.compile(rb"<!DOCTYPE[^>]*>\s*", re.IGNORECASE)
_METADATA_RE = re.compile(rb"<metadata>.*?</metadata>\s*", re.DOTALL)
_COMMENT_RE = re.compile(rb"<!--.*?-->\s*", re.DOTALL)


def clean_svg(data: bytes) -> bytes:
    """matplotlib's SVG without the DOCTYPE (refused by the validator, and a
    classic entity vector), the metadata block and comments."""
    data = _DOCTYPE_RE.sub(b"", data)
    data = _METADATA_RE.sub(b"", data)
    data = _COMMENT_RE.sub(b"", data)
    return data


# ----------------------------------------------------------------- chrome --


def _text_kw(ts: Optional[CS.ChartTextStyle], size: float, colour: str, bold: bool = False) -> Dict[str, Any]:
    kw: Dict[str, Any] = {"fontsize": size, "color": colour, "fontweight": "bold" if bold else "normal"}
    if ts is not None:
        if ts.size_pt:
            kw["fontsize"] = ts.size_pt
        if ts.color:
            kw["color"] = ts.color
        if ts.bold is not None:
            kw["fontweight"] = "bold" if ts.bold else "normal"
        if ts.italic:
            kw["fontstyle"] = "italic"
        if ts.font_family:
            installed = _installed_families()
            mapped = [f for f in dict.fromkeys(_mapped(ts.font_family)) if f in installed]
            kw["fontfamily"] = [*(mapped or [ts.font_family]), *FONT_FALLBACKS]
    return kw


def _decorate(fig, ax, ctx: _Ctx, section_heading: str, include_caption: bool, legend_handled: bool) -> None:
    c, st, d = ctx.chart, ctx.style, ctx.d
    title = c.title
    if title and section_heading and " ".join(title.split()).casefold() == " ".join(section_heading.split()).casefold():
        title = ""
    if title:
        kw = _text_kw(st.title, d.title_size_pt, ctx.ink, bold=True)
        ax.set_title(title, loc="left", pad=22 if c.subtitle else 12, **kw)
    if c.subtitle:
        kw = _text_kw(st.axis, d.axis_size_pt, ctx.muted)
        # Kept on the axes so `_align_titles_left` can move it with the title.
        ax._ts_subtitle = ax.text(0.0, 1.02, c.subtitle, transform=ax.transAxes, ha="left", va="bottom", **kw)
    if c.type not in ("pie", "donut", "radar", "heatmap", "funnel", "treemap", "sunburst"):
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(d.grid_color if ctx.bg == WHITE else ctx.muted)
        axis_kw = _text_kw(st.axis, d.axis_size_pt, ctx.muted)
        ax.tick_params(colors=axis_kw["color"], labelsize=axis_kw["fontsize"])
        horizontal = c.type in ("horizontal_bar", "stacked_horizontal_bar", "gantt", "bullet")
        # A category axis needs no tick marks: the label already says which
        # bar it belongs to, and the little strokes only add noise.
        if c.type not in CS.XY_TYPES + ("histogram",):
            ax.tick_params(axis="y" if horizontal else "x", length=0)
        # Gridlines exist so a reader can estimate a value. When every mark
        # already carries its number, they are noise behind the data.
        if st.gridlines and not ctx.labels_complete:
            ax.grid(True, axis="x" if horizontal else "y", color=d.grid_color, linewidth=0.8)
            ax.set_axisbelow(True)
        else:
            ax.grid(False)
        xl = c.x_label
        yl = c.y_label
        if c.type in ("horizontal_bar", "stacked_horizontal_bar"):
            xl, yl = yl, xl
        if xl and c.type not in ("gantt", "bullet"):
            ax.set_xlabel(xl, **axis_kw)
        if yl and c.type != "bullet":
            ax.set_ylabel(yl, **axis_kw)
    if not legend_handled:
        _legend(ax, ctx)


def _place_bottom_legends(fig) -> None:
    """A bottom legend sits under the tick labels as actually drawn (long
    rotated category names push it down), never on top of them."""
    renderer = fig.canvas.get_renderer()
    for ax in fig.axes:
        leg = ax.get_legend()
        if leg is None or getattr(leg, "_ts_position", "") != "bottom":
            continue
        leg.set_visible(False)
        box = ax.get_tightbbox(renderer)
        leg.set_visible(True)
        if box is None:
            continue
        y = ax.transAxes.inverted().transform((0, box.y0))[1]
        leg.set_bbox_to_anchor((0.5, y - 0.02), transform=ax.transAxes)


def _rotate_crowded_ticks(fig) -> None:
    """Rotate category labels that still do not FIT after wrapping.

    `_wrap_tick` breaks a long label over two lines, which is the readable
    fix, but whether two lines fit depends on how wide the chart is and how
    many categories share it — five one-word stages in a half-page figure
    collide while the same five in a full-page figure do not. Character
    counts cannot see that; the drawn text can, so this measures it."""
    renderer = fig.canvas.get_renderer()
    for ax in fig.axes:
        if not getattr(ax, "_ts_measure_ticks", False):
            continue
        ticks = [t for t in ax.get_xticklabels() if t.get_text()]
        if len(ticks) < 2:
            continue
        slot = ax.get_window_extent(renderer).width / max(1, len(ax.get_xticks()))
        widest = max(t.get_window_extent(renderer).width for t in ticks)
        if widest <= slot * 0.98:
            continue
        # Set through the axis rather than on the Text artists: a fixed tick
        # formatter rewrites their strings on every redraw, and a figure is
        # drawn again on save.
        # A wrapped label rotated 45 degrees is two lines running diagonally;
        # one line reads better once it is on the slant.
        ax.set_xticklabels([t.get_text().replace("\n", " ") for t in ax.get_xticklabels()],
                           rotation=45, ha="right", rotation_mode="anchor")
        ax._ts_measure_ticks = False


def title_artist(ax):
    """The Text that carries the chart's title. An Axes keeps THREE title
    artists — left, centre and right — and `ax.title` is the centre one, so
    a title set with loc='left' is not on it."""
    for name in ("_left_title", "title"):
        artist = getattr(ax, name, None)
        if artist is not None and artist.get_text():
            return artist
    return ax.title


def _align_titles_left(fig) -> None:
    """The title (and the subtitle under it) starts at the FIGURE's left
    edge, over the value-axis labels, the way a report graphic is set —
    matplotlib's loc='left' means the left of the plot box, which leaves the
    title hanging in the middle of the image once the tick labels are wide."""
    renderer = fig.canvas.get_renderer()
    for ax in fig.axes:
        title = title_artist(ax)
        subtitle = getattr(ax, "_ts_subtitle", None)
        if not title.get_text() and subtitle is None:
            continue
        if title.get_text() and title.get_horizontalalignment() != "left":
            continue
        was = [(a, a.get_visible()) for a in (title, subtitle) if a is not None]
        for artist, _ in was:
            artist.set_visible(False)
        box = ax.get_tightbbox(renderer)
        for artist, visible in was:
            artist.set_visible(visible)
        if box is None:
            continue
        x = min(0.0, ax.transAxes.inverted().transform((box.x0, 0))[0])
        if title.get_text():
            title.set_position((x, title.get_position()[1]))
        if subtitle is not None:
            subtitle.set_x(x)


def _caption_below(fig, ctx: _Ctx) -> None:
    """The caption under everything already drawn (rotated tick labels, a
    bottom legend), so the tight bounding box grows instead of overlapping."""
    renderer = fig.canvas.get_renderer()
    boxes = [a.get_tightbbox(renderer) for a in fig.axes]
    boxes = [b for b in boxes if b is not None]
    inv = fig.transFigure.inverted()
    ymin = min(inv.transform((0, b.y0))[1] for b in boxes) if boxes else 0.0
    xmin = min(inv.transform((b.x0, 0))[0] for b in boxes) if boxes else 0.0
    fig.text(max(0.0, xmin), ymin - 0.02, ctx.chart.caption, ha="left", va="top", fontsize=max(7.0, ctx.d.axis_size_pt - 1),
             color=ctx.muted, fontstyle="italic")


def _legend(ax, ctx: _Ctx, handles=None, labels=None) -> None:
    st = ctx.style
    if st.legend_position == "none":
        return
    if handles is None:
        handles, labels = ax.get_legend_handles_labels()
    if len(handles) < 2:
        return
    kw = _text_kw(st.legend, ctx.d.axis_size_pt - 1, ctx.muted)
    pos = st.legend_position
    common = dict(frameon=False, fontsize=kw["fontsize"], labelcolor=kw["color"])
    if pos == "bottom":
        leg = ax.legend(handles, labels, loc="upper center", bbox_to_anchor=(0.5, -0.14), ncol=min(len(handles), 4), **common)
        leg._ts_position = "bottom"
    elif pos == "top":
        ax.legend(handles, labels, loc="lower center", bbox_to_anchor=(0.5, 1.0), ncol=min(len(handles), 4), **common)
    elif pos == "left":
        ax.legend(handles, labels, loc="center right", bbox_to_anchor=(-0.12, 0.5), **common)
    else:
        ax.legend(handles, labels, loc="center left", bbox_to_anchor=(1.01, 0.5), **common)


def _whole_numbers(ctx: _Ctx) -> bool:
    """Every value the chart plots is a whole number — a head count, an
    order count, a number of tickets. Halves on the axis of such a chart are
    numbers that cannot exist."""
    if ctx.style.number_format in ("decimal1", "decimal2", "percent"):
        return False
    values = [v for s in ctx.chart.series for v in s.values]
    if not values:
        return False
    return all(abs(v - round(v)) < 1e-9 for v in values)


def _value_axis(ax, ctx: _Ctx, axis: str = "y") -> None:
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    st = ctx.style
    fmt = FuncFormatter(lambda v, _pos: ctx.label(v))
    if _whole_numbers(ctx) and not st.log_y:
        (ax.yaxis if axis == "y" else ax.xaxis).set_major_locator(MaxNLocator(nbins="auto", integer=True))
    if axis == "y":
        ax.yaxis.set_major_formatter(fmt)
        if st.log_y:
            ax.set_yscale("log")
        lo, hi = ax.get_ylim()
        ax.set_ylim(st.y_min if st.y_min is not None else lo, st.y_max if st.y_max is not None else hi)
    else:
        ax.xaxis.set_major_formatter(fmt)
        lo, hi = ax.get_xlim()
        ax.set_xlim(st.y_min if st.y_min is not None else lo, st.y_max if st.y_max is not None else hi)


TICK_LABEL_MAX = 28


def _tick_text(label: str) -> str:
    """A category label on an axis, clipped: one 80-character label drawn
    at 45 degrees squeezed the plot to a sliver (verifier sample 2026-09-15).
    The full label stays in the data (XLSX Chart data, captions)."""
    label = str(label)
    return label if len(label) <= TICK_LABEL_MAX else label[: TICK_LABEL_MAX - 1].rstrip() + "…"


#: A category label longer than this asks for a second line.
TICK_WRAP_WIDTH = 12


def _wrap_tick(label: str, width: int = TICK_WRAP_WIDTH) -> str:
    """`label` over at most TWO lines, broken at a space. A two-line label
    stays horizontal and readable; rotating it 45 degrees is what everyone
    does instead, and it is the harder thing to read."""
    if len(label) <= width or " " not in label.strip():
        return label
    lines = textwrap.wrap(label, width=max(width, math.ceil(len(label) / 2)), max_lines=2, placeholder="…")
    return "\n".join(lines) if lines else label


def _category_ticks(ax, categories: Sequence[str], ctx: _Ctx, axis: str = "x") -> None:
    categories = [_tick_text(c) for c in categories]
    idx = list(range(len(categories)))
    if axis == "x":
        ax.set_xticks(idx)
        many = len(categories) > 24
        wrapped = [_wrap_tick(c) for c in categories]
        # Rotation is the LAST resort: only when wrapping left a line still
        # too long for the slot, or when there are too many slots to fit.
        longest = max((len(line) for c in wrapped for line in c.split("\n")), default=0)
        rotation = 45 if many or longest > TICK_WRAP_WIDTH else 0
        labels = [c if not many or i % max(1, len(categories) // 24) == 0 else "" for i, c in enumerate(wrapped)]
        ax.set_xticklabels(labels, rotation=rotation, ha="right" if rotation else "center")
        # Character counts only guess at width. `_rotate_crowded_ticks` measures
        # the drawn labels against the slots they have to fit in, and rotates
        # the ones that still collide.
        ax._ts_measure_ticks = not rotation
    else:
        ax.set_yticks(idx)
        ax.set_yticklabels(list(categories))


def _labels_on(ctx: _Ctx, n_categories: int) -> bool:
    if ctx.style.data_labels == "on":
        return True
    if ctx.style.data_labels == "off":
        return False
    # A status chart always shows its labels: colour alone must never be the
    # only thing that says "critical" (chart_colours, the status rule).
    if ctx.scheme.force_labels:
        return True
    return n_categories <= LABEL_MAX_CATEGORIES


def _slice_label_style(ctx: Any, fill: str) -> str:
    """Text colour for a label drawn on a slice. Every slice above the size
    floor keeps its label (2026-09-15: mid-tone fills such as #4285F4 and
    #EA4335 reach 4.5:1 with neither white nor the theme ink, and their labels
    were dropped). Pure black or pure white always reaches at least 4.58:1 on
    any opaque fill, so the fallback is whichever of the two contrasts more."""
    for colour in (ctx.d.label_color_for(fill), ctx.ink):
        if contrast_ratio(colour, fill) >= 4.5:
            return colour
    return max(("#000000", "#FFFFFF"), key=lambda c: contrast_ratio(c, fill))


def _label_kw(ctx: _Ctx, colour: str) -> Dict[str, Any]:
    kw = _text_kw(ctx.style.data_label, max(7.0, ctx.d.axis_size_pt - 2), colour)
    return kw


# ---------------------------------------------------------------- drawers --


def _draw_bars(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    t = c.type
    horizontal = t in ("horizontal_bar", "stacked_horizontal_bar")
    stacked = t in ("stacked_bar", "stacked_horizontal_bar", "percent_stacked_bar")
    percent = t == "percent_stacked_bar"
    cats = list(c.categories)
    n = len(c.series)
    idx = list(range(len(cats)))
    values = [list(s.values) for s in c.series]
    if percent:
        totals = [sum(max(0.0, values[k][j]) for k in range(n)) or 1.0 for j in idx]
        values = [[max(0.0, values[k][j]) / totals[j] * 100.0 for j in idx] for k in range(n)]
    labels_on = _labels_on(ctx, len(cats))
    base = [0.0] * len(cats)
    width = 0.8 if stacked else 0.8 / max(1, n)
    single_cat_colours = ctx.by_category()
    ctx.labels_complete = labels_on and bool(cats) and all(values[k][j] != 0 for k in range(n) for j in idx)
    for k, s in enumerate(c.series):
        colour = ctx.series_colour(k, s.name)
        colours = [ctx.category_colour(j, cats[j]) if single_cat_colours else colour for j in idx]
        pos = idx if stacked else [i + (k - (n - 1) / 2) * width for i in idx]
        vals = values[k]
        if horizontal:
            bars = ax.barh(pos, vals, height=width, left=base if stacked else None, label=s.name, color=colours)
        else:
            bars = ax.bar(pos, vals, width=width, bottom=base if stacked else None, label=s.name, color=colours)
        if labels_on:
            _bar_labels(ax, ctx, bars, vals, colours, stacked, horizontal, percent)
        if stacked:
            base = [b + v for b, v in zip(base, vals)]
    if stacked and not percent and labels_on and n > 1:
        # A stack's total is the number the reader is after; without it the
        # only way to get it is to add the segments up by eye.
        _stack_totals(ax, ctx, idx, base, horizontal)
        ctx.labels_complete = True
    if horizontal:
        _category_ticks(ax, cats, ctx, axis="y")
        ax.invert_yaxis()
        ax.margins(x=0.12)
        _value_axis(ax, ctx, "x")
        if ctx.style.y_min is None:
            lo, hi = ax.get_xlim()
            ax.set_xlim(min(0.0, lo), hi)
    else:
        _category_ticks(ax, cats, ctx)
        ax.margins(y=0.12)
        _value_axis(ax, ctx, "y")
        if ctx.style.y_min is None and not ctx.style.log_y:
            lo, hi = ax.get_ylim()
            ax.set_ylim(min(0.0, lo), hi)
    if percent:
        from matplotlib.ticker import FuncFormatter

        (ax.xaxis if horizontal else ax.yaxis).set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0f}%"))
    if ctx.scheme.legend and n == 1:
        # A signed chart has two meanings and one series, so the legend has
        # to come from the meanings: nothing on the axis says which is which.
        from matplotlib.patches import Patch

        gain, loss, total = ctx.signed_colours()
        seen = {"Increase": gain, "Decrease": loss, "Total": total}
        handles = [Patch(color=seen.get(name, colour), label=name) for name, colour in ctx.scheme.legend]
        _legend(ax, ctx, handles, [h.get_label() for h in handles])
        return True
    return False


def _stack_totals(ax, ctx: _Ctx, idx: Sequence[int], totals: Sequence[float], horizontal: bool) -> None:
    for i, total in zip(idx, totals):
        text = ctx.label(total)
        if horizontal:
            ax.annotate(text, xy=(total, i), xytext=(4, 0), textcoords="offset points", ha="left", va="center",
                        **_label_kw(ctx, ctx.ink))
        else:
            ax.annotate(text, xy=(i, total), xytext=(0, 3), textcoords="offset points", ha="center", va="bottom",
                        **_label_kw(ctx, ctx.ink))


def _bar_labels(ax, ctx: _Ctx, bars, vals, colours, stacked: bool, horizontal: bool, percent: bool) -> None:
    """Grouped bars: every label outside the bar, in ink. Stacked segments:
    inside, only where white/ink reaches 4.5:1 on the segment and the
    segment is tall enough to hold the text; otherwise no label."""
    texts = [("" if v == 0 else (f"{v:.0f}%" if percent else ctx.label(v))) for v in vals]
    if not stacked:
        ax.bar_label(bars, labels=texts, padding=3 if horizontal else 2, **_label_kw(ctx, ctx.ink))
        return
    extent = max((abs(v) for v in vals), default=0.0) or 1.0
    for bar, v, col, text in zip(bars, vals, colours, texts):
        if not text:
            continue
        inside_colour = ctx.d.label_color_for(col)
        size = bar.get_width() if horizontal else bar.get_height()
        if contrast_ratio(inside_colour, col) >= 4.5 and abs(size) >= 0.08 * extent:
            cx = bar.get_x() + bar.get_width() / 2
            cy = bar.get_y() + bar.get_height() / 2
            ax.text(cx, cy, text, ha="center", va="center", **_label_kw(ctx, inside_colour))


def _line_styles(n: int, k: int) -> Dict[str, Any]:
    if n >= 3:
        return {"marker": _MARKERS[k % len(_MARKERS)], "linestyle": _DASHES[k % len(_DASHES)]}
    return {"marker": "o", "linestyle": "-"}


def _draw_line(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    cats = list(c.categories)
    idx = list(range(len(cats)))
    n = len(c.series)
    direct = n > 5
    for k, s in enumerate(c.series):
        colour = ctx.series_colour(k, s.name)
        ax.plot(idx, list(s.values), linewidth=2, markersize=4.5, label=s.name, color=colour, **_line_styles(n, k))
        if direct and s.values:
            ax.annotate(s.name, xy=(idx[-1], s.values[-1]), xytext=(6, 0), textcoords="offset points", va="center",
                        **_label_kw(ctx, ctx.ink))
    if n == 1 and _labels_on(ctx, len(cats)) and ctx.style.data_labels != "auto":
        for i, v in zip(idx, c.series[0].values):
            ax.annotate(ctx.label(v), xy=(i, v), xytext=(0, 6), textcoords="offset points", ha="center", **_label_kw(ctx, ctx.ink))
    _category_ticks(ax, cats, ctx)
    _value_axis(ax, ctx)
    if direct:
        ax.margins(x=0.12)
    return direct


def _draw_area(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    cats = list(c.categories)
    idx = list(range(len(cats)))
    if c.type == "stacked_area":
        ax.stackplot(idx, *[list(s.values) for s in c.series], labels=[s.name for s in c.series],
                     colors=[ctx.series_colour(k, s.name) for k, s in enumerate(c.series)], alpha=0.9)
    else:
        for k, s in enumerate(c.series):
            colour = ctx.series_colour(k, s.name)
            ax.fill_between(idx, list(s.values), alpha=0.25 if len(c.series) > 1 else 0.35, color=colour)
            ax.plot(idx, list(s.values), color=colour, linewidth=2, label=s.name, **({"linestyle": _DASHES[k % len(_DASHES)]} if len(c.series) >= 3 else {}))
    _category_ticks(ax, cats, ctx)
    _value_axis(ax, ctx)
    if ctx.style.y_min is None and not ctx.style.log_y:
        lo, hi = ax.get_ylim()
        ax.set_ylim(min(0.0, lo), hi)
    return False


def _draw_pie(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    pairs = [(cat, max(float(v), 0.0)) for cat, v in zip(c.categories, c.series[0].values)]
    total = sum(v for _, v in pairs)
    if total <= 0:
        ax.text(0.5, 0.5, "No positive values to chart", ha="center", va="center", color=ctx.muted, transform=ax.transAxes)
        ax.set_axis_off()
        return True
    labels = [p[0] for p in pairs]
    sizes = [p[1] for p in pairs]
    colours = [ctx.category_colour(j, lab) for j, lab in enumerate(labels)]
    donut = c.type == "donut"
    wedgeprops = {"linewidth": 1.2, "edgecolor": ctx.bg}
    if donut:
        wedgeprops["width"] = 0.42
    wedges, texts = ax.pie(sizes, labels=labels, colors=colours, startangle=90, counterclock=False, wedgeprops=wedgeprops,
                           textprops=_text_kw(ctx.style.axis, ctx.d.axis_size_pt, ctx.muted), labeldistance=1.08)
    r_text = 0.79 if donut else 0.64
    for wedge, v, col in zip(wedges, sizes, colours):
        share = v / total
        if share < 0.035:
            continue
        ang = math.radians((wedge.theta1 + wedge.theta2) / 2)
        colour = _slice_label_style(ctx, col)
        text = f"{share * 100:.0f}%" if ctx.style.number_format in (None, "percent") else ctx.label(v)
        ax.text(r_text * math.cos(ang), r_text * math.sin(ang), text, ha="center", va="center", **_label_kw(ctx, colour))
    if donut:
        ax.text(0, 0, ctx.label(total), ha="center", va="center", **_text_kw(ctx.style.title, ctx.d.title_size_pt + 2, ctx.ink, bold=True))
    ax.axis("equal")
    return True


def _draw_scatter(ax, ctx: _Ctx) -> bool:
    import numpy as np

    c = ctx.chart
    bubble = c.type == "bubble"
    all_sizes = [z for s in c.series for z in (s.sizes or [])]
    zmin, zmax = (min(all_sizes), max(all_sizes)) if all_sizes else (0.0, 1.0)
    for k, s in enumerate(c.series):
        colour = ctx.series_colour(k, s.name)
        xs = list(s.x or [])
        if bubble and s.sizes:
            span = (zmax - zmin) or 1.0
            area = [40 + 900 * (z - zmin) / span for z in s.sizes]
            ax.scatter(xs, list(s.values), s=area, color=colour, alpha=0.55, edgecolors=ctx.bg, linewidths=0.8, label=s.name,
                       marker=_MARKERS[k % len(_MARKERS)] if len(c.series) >= 3 else "o")
        else:
            ax.scatter(xs, list(s.values), s=22, color=colour, alpha=0.85, label=s.name, marker=_MARKERS[k % len(_MARKERS)] if len(c.series) >= 3 else "o")
    for tr in (c.extra.trendlines if c.extra else []):
        k = next((i for i, s in enumerate(c.series) if s.name == tr.series), 0)
        xs = c.series[k].x or []
        if not xs:
            continue
        gx = np.linspace(min(xs), max(xs), 50)
        ax.plot(gx, tr.slope * gx + tr.intercept, linestyle="--", linewidth=1.6, color=ctx.ink if len(c.series) == 1 else ctx.series_colour(k, tr.series),
                label=f"Trend ({tr.series}): R² = {tr.r2:.2f}")
    _value_axis(ax, ctx, "y")
    st = ctx.style
    if st.x_min is not None or st.x_max is not None:
        lo, hi = ax.get_xlim()
        ax.set_xlim(st.x_min if st.x_min is not None else lo, st.x_max if st.x_max is not None else hi)
    from matplotlib.ticker import FuncFormatter

    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: format_value(v, None)))
    handles, labels = ax.get_legend_handles_labels()
    if len(handles) >= 2 or (c.extra and c.extra.trendlines):
        if len(handles) >= 2:
            _legend(ax, ctx, handles, labels)
    return True


def _draw_histogram(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    edges = list(c.extra.bin_edges) if c.extra and c.extra.bin_edges else []
    n = len(c.series)
    if len(edges) == len(c.categories) + 1:
        widths = [edges[i + 1] - edges[i] for i in range(len(c.categories))]
        for k, s in enumerate(c.series):
            colour = ctx.series_colour(k, s.name)
            if n == 1:
                ax.bar(edges[:-1], list(s.values), width=widths, align="edge", color=colour, edgecolor=ctx.bg, linewidth=0.8, label=s.name)
            else:
                ax.step(edges, [s.values[0], *s.values], where="pre", color=colour, linewidth=2, label=s.name, linestyle=_DASHES[k % len(_DASHES)])
        from matplotlib.ticker import FuncFormatter

        tick_fmt = ctx.fmt or ("compact" if max(abs(e) for e in edges) >= 100_000 else None)
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _p: format_value(v, tick_fmt)))
    else:
        return _draw_bars(ax, ctx)
    ax.set_ylim(0, None)
    return False


def _draw_combo(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    cats = list(c.categories)
    idx = list(range(len(cats)))
    bars = [(k, s) for k, s in enumerate(c.series) if (s.kind or "bar") == "bar" and s.axis == "primary"]
    lines = [(k, s) for k, s in enumerate(c.series) if (k, s) not in bars]
    nb = max(1, len(bars))
    width = 0.8 / nb
    for j, (k, s) in enumerate(bars):
        pos = [i + (j - (nb - 1) / 2) * width for i in idx]
        ax.bar(pos, list(s.values), width=width, color=ctx.series_colour(k, s.name), label=s.name)
    ax2 = None
    for j, (k, s) in enumerate(lines):
        target = ax
        if s.axis == "secondary":
            if ax2 is None:
                ax2 = ax.twinx()
                ax2.spines["top"].set_visible(False)
                ax2.tick_params(colors=ctx.muted, labelsize=ctx.d.axis_size_pt)
                if c.y2_label:
                    ax2.set_ylabel(c.y2_label, **_text_kw(ctx.style.axis, ctx.d.axis_size_pt, ctx.muted))
            target = ax2
        target.plot(idx, list(s.values), color=ctx.series_colour(k, s.name), linewidth=2.2, label=s.name, **_line_styles(len(lines) + 2 if len(lines) >= 1 else 1, j + 1))
    _category_ticks(ax, cats, ctx)
    _value_axis(ax, ctx)
    if ctx.style.y_min is None:
        lo, hi = ax.get_ylim()
        ax.set_ylim(min(0.0, lo), hi)
    if ax2 is not None:
        from matplotlib.ticker import FuncFormatter

        ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: ctx.label(v)))
        lo, hi = ax2.get_ylim()
        ax2.set_ylim(min(0.0, lo), hi)
    handles, labels = ax.get_legend_handles_labels()
    if ax2 is not None:
        h2, l2 = ax2.get_legend_handles_labels()
        handles, labels = handles + h2, labels + l2
    _legend(ax, ctx, handles, labels)
    return True


def _draw_box(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    boxes = c.extra.box if c.extra else []
    stats = [{"label": b.name, "whislo": b.whisker_low, "q1": b.q1, "med": b.median, "q3": b.q3, "whishi": b.whisker_high,
              "fliers": list(b.outliers), "mean": b.mean} for b in boxes]
    colour = ctx.base_colour()
    art = ax.bxp(stats, showfliers=True, patch_artist=True, widths=0.55,
                 medianprops={"color": ctx.ink, "linewidth": 2}, whiskerprops={"color": ctx.muted},
                 capprops={"color": ctx.muted}, flierprops={"marker": "o", "markersize": 3.5, "markerfacecolor": ctx.muted, "markeredgecolor": ctx.muted})
    for j, patch in enumerate(art["boxes"]):
        fill = ctx.style.category_colors.get(boxes[j].name, colour)
        patch.set_facecolor(fill)
        patch.set_alpha(0.55)
        patch.set_edgecolor(fill)
    _value_axis(ax, ctx)
    if len(boxes) > 6 or any(len(b.name) > 10 for b in boxes):
        for lab in ax.get_xticklabels():
            lab.set_rotation(30)
            lab.set_ha("right")
    return True


def _draw_heatmap(ax, ctx: _Ctx) -> bool:
    import numpy as np
    from matplotlib.colors import LinearSegmentedColormap

    c = ctx.chart
    grid = np.array([list(s.values) for s in c.series], dtype="float64")
    top = ctx.style.color or NAVY
    cmap = LinearSegmentedColormap.from_list("ts_heat", ["#F3F6FA", top])
    # pcolormesh, not imshow: imshow embeds a base64 raster <image> in the
    # SVG, which the validator (rightly) refuses as a non-fragment href.
    im = ax.pcolormesh([j - 0.5 for j in range(grid.shape[1] + 1)], [i - 0.5 for i in range(grid.shape[0] + 1)], grid, cmap=cmap, edgecolors=ctx.bg, linewidth=0.5)
    ax.set_xlim(-0.5, grid.shape[1] - 0.5)
    ax.set_ylim(grid.shape[0] - 0.5, -0.5)
    ax.set_xticks(range(len(c.categories)))
    ax.set_xticklabels(list(c.categories), rotation=45 if any(len(x) > 8 for x in c.categories) else 0,
                       ha="right" if any(len(x) > 8 for x in c.categories) else "center")
    ax.set_yticks(range(len(c.series)))
    ax.set_yticklabels([s.name for s in c.series])
    ax.tick_params(colors=ctx.muted, labelsize=ctx.d.axis_size_pt, length=0)
    for side in ax.spines.values():
        side.set_visible(False)
    if grid.size <= 225:
        vmin, vmax = float(grid.min()), float(grid.max())
        for i in range(grid.shape[0]):
            for j in range(grid.shape[1]):
                rgba = cmap((grid[i, j] - vmin) / ((vmax - vmin) or 1.0))
                cell = _to_hex(rgba)
                colour = ctx.d.label_color_for(cell)
                if contrast_ratio(colour, cell) >= 4.5:
                    ax.text(j, i, ctx.label(grid[i, j]), ha="center", va="center", **_label_kw(ctx, colour))
    cb = ax.figure.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
    cb.outline.set_visible(False)
    if cb.solids is not None:
        cb.solids.set_rasterized(False)  # a raster would be an <image> href in the SVG
    cb.ax.tick_params(colors=ctx.muted, labelsize=ctx.d.axis_size_pt - 1)
    if c.x_label:
        ax.set_xlabel(c.x_label, color=ctx.muted, fontsize=ctx.d.axis_size_pt)
    return True


def _draw_waterfall(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    deltas = list(c.series[0].values)
    cats = list(c.categories) + ["Total"]
    up, down, total_colour = ctx.signed_colours()
    running = 0.0
    bottoms, heights, colours = [], [], []
    for v in deltas:
        bottoms.append(running if v >= 0 else running + v)
        heights.append(abs(v))
        colours.append(up if v >= 0 else down)
        running += v
    bottoms.append(min(0.0, running))
    heights.append(abs(running))
    colours.append(total_colour)
    idx = list(range(len(cats)))
    ax.bar(idx, heights, bottom=bottoms, color=colours, width=0.62)
    cum = 0.0
    for i in range(len(deltas) - 1):
        cum += deltas[i]
        ax.plot([i + 0.31, i + 1 - 0.31], [cum, cum], color=ctx.muted, linewidth=0.8)
    if _labels_on(ctx, len(cats)):
        for i, (b, hgt) in enumerate(zip(bottoms, heights)):
            v = deltas[i] if i < len(deltas) else running
            text = ("+" if i < len(deltas) and v > 0 else "") + ctx.label(v)
            ax.annotate(text, xy=(i, b + hgt), xytext=(0, 3), textcoords="offset points", ha="center", va="bottom", **_label_kw(ctx, ctx.ink))
    _category_ticks(ax, cats, ctx)
    _value_axis(ax, ctx)
    ax.margins(y=0.12)
    from matplotlib.patches import Patch

    handles = [Patch(color=up, label="Increase"), Patch(color=down, label="Decrease"), Patch(color=total_colour, label="Total")]
    _legend(ax, ctx, handles, [h.get_label() for h in handles])
    return True


def _draw_funnel(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    vals = [max(0.0, v) for v in c.series[0].values]
    top = max(vals) or 1.0
    cats = list(c.categories)
    for j, (cat, v) in enumerate(zip(cats, vals)):
        colour = ctx.category_colour(j, cat)
        left = (top - v) / 2
        ax.barh(j, v, left=left, height=0.72, color=colour)
        share = f" ({v / vals[0] * 100:.0f}%)" if vals[0] and j > 0 else ""
        text = ctx.label(v) + share
        inside = ctx.d.label_color_for(colour)
        if v >= 0.3 * top and contrast_ratio(inside, colour) >= 4.5:
            ax.text(top / 2, j, text, ha="center", va="center", **_label_kw(ctx, inside))
        else:
            ax.text(left + v + 0.01 * top, j, text, ha="left", va="center", **_label_kw(ctx, ctx.ink))
    ax.set_yticks(range(len(cats)))
    ax.set_yticklabels(cats)
    ax.invert_yaxis()
    ax.set_xticks([])
    ax.set_xlim(0, top * 1.18)
    ax.tick_params(colors=ctx.muted, labelsize=ctx.d.axis_size_pt, length=0)
    for side in ax.spines.values():
        side.set_visible(False)
    return True


def _draw_gantt(ax, ctx: _Ctx) -> bool:
    import datetime as dt

    import matplotlib.dates as mdates

    c = ctx.chart
    spans = c.extra.spans if c.extra else []
    for j, sp in enumerate(spans):
        start = dt.date.fromisoformat(sp.start)
        end = dt.date.fromisoformat(sp.end)
        colour = ctx.style.category_colors.get(sp.label) or ctx.series_colour(0)
        ax.barh(j, max(0.8, (end - start).days), left=mdates.date2num(start), height=0.55, color=colour)
        if _labels_on(ctx, 13 if len(spans) > 30 else len(spans)):
            ax.annotate(f"{sp.days:.0f}d", xy=(mdates.date2num(end), j), xytext=(4, 0), textcoords="offset points", va="center", **_label_kw(ctx, ctx.ink))
    ax.set_yticks(range(len(spans)))
    ax.set_yticklabels([s.label for s in spans])
    ax.invert_yaxis()
    ax.xaxis_date()
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b"))
    ax.margins(x=0.06)
    return True


def _draw_radar(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    cats = list(c.categories)
    n = len(cats)
    angles = [2 * math.pi * i / n for i in range(n)] + [0.0]
    for k, s in enumerate(c.series):
        vals = list(s.values) + [s.values[0]]
        colour = ctx.series_colour(k, s.name)
        ax.plot(angles, vals, color=colour, linewidth=2, label=s.name, **_line_styles(len(c.series), k))
        ax.fill(angles, vals, color=colour, alpha=0.06)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(cats, color=ctx.muted, fontsize=ctx.d.axis_size_pt)
    ax.tick_params(axis="y", colors=ctx.muted, labelsize=ctx.d.axis_size_pt - 3)
    ax.set_rlabel_position(90)
    from matplotlib.ticker import FuncFormatter, MaxNLocator

    ax.yaxis.set_major_locator(MaxNLocator(4))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: format_value(v, ctx.fmt or "compact")))
    ax.grid(color=ctx.d.grid_color)
    ax.spines["polar"].set_color(ctx.d.grid_color)
    handles, labels = ax.get_legend_handles_labels()
    _legend(ax, ctx, handles, labels)
    return True


def _draw_pareto(ax, ctx: _Ctx) -> bool:
    """Bars (series 0) with the cumulative % (series 1) on a right-hand axis
    fixed to 0-100, plus the 80% line the chart exists to be read against."""
    c = ctx.chart
    cats = list(c.categories)
    idx = list(range(len(cats)))
    bars = list(c.series[0].values)
    colour = ctx.series_colour(0, c.series[0].name)
    colours = [ctx.style.category_colors.get(cat, colour) for cat in cats]
    ax.bar(idx, bars, width=0.72, color=colours, label=c.series[0].name)
    if _labels_on(ctx, len(cats)):
        for i, v in zip(idx, bars):
            ax.annotate(ctx.label(v), xy=(i, v), xytext=(0, 3), textcoords="offset points", ha="center", va="bottom",
                        **_label_kw(ctx, ctx.ink))
    _category_ticks(ax, cats, ctx)
    _value_axis(ax, ctx)
    ax.margins(y=0.14)
    if ctx.style.y_min is None and not ctx.style.log_y:
        lo, hi = ax.get_ylim()
        ax.set_ylim(min(0.0, lo), hi)
    cum = list(c.series[1].values) if len(c.series) > 1 else []
    line_colour = ctx.series_colour(1, c.series[1].name) if len(c.series) > 1 else ctx.ink
    ax2 = ax.twinx()
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_color(ctx.d.grid_color)
    ax2.plot(idx, cum, color=line_colour, linewidth=2, marker="o", markersize=4, label=c.series[1].name if len(c.series) > 1 else "")
    ax2.set_ylim(0, 105)
    from matplotlib.ticker import FuncFormatter

    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:.0f}%"))
    ax2.tick_params(colors=ctx.muted, labelsize=ctx.d.axis_size_pt)
    ax2.set_ylabel(c.y2_label or "Cumulative %", **_text_kw(ctx.style.axis, ctx.d.axis_size_pt, ctx.muted))
    ax2.axhline(80, color=ctx.muted, linewidth=0.9, linestyle=(0, (4, 3)))
    ax2.annotate("80%", xy=(1.0, 80), xycoords=("axes fraction", "data"), xytext=(-2, 3),
                 textcoords="offset points", ha="right", va="bottom", **_label_kw(ctx, ctx.muted))
    ax2.grid(False)
    handles, labels = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    _legend(ax, ctx, handles + h2, labels + l2)
    return True


def _text_width_pt(text: str, size_pt: float) -> float:
    """The drawn width of `text` at `size_pt`, in points.

    matplotlib.textpath measures the real glyphs; the fallback (0.62 em per
    character) is only for a font that cannot be loaded, and it is the
    estimate that was measured to be about half of what a label needs.
    """
    if not text:
        return 0.0
    try:
        from matplotlib.textpath import TextPath

        return float(TextPath((0.0, 0.0), text, size=size_pt).get_extents().width)
    except Exception:
        return 0.62 * size_pt * len(text)


def _draw_treemap(ax, ctx: _Ctx) -> bool:
    from matplotlib.patches import Rectangle

    c = ctx.chart
    pairs = [(cat, float(v)) for cat, v in zip(c.categories, c.series[0].values) if v > 0]
    total = sum(v for _, v in pairs)
    if not pairs or total <= 0:
        ax.text(0.5, 0.5, "No positive values to chart", ha="center", va="center", color=ctx.muted, transform=ax.transAxes)
        ax.set_axis_off()
        return True
    rects = squarified([v for _, v in pairs], 0.0, 0.0, 100.0, 62.0)
    # Points per treemap unit, MEASURED from the axes this figure actually
    # got. The box is always 100 units wide, so a fixed per-character
    # allowance in units only holds at one figure size: at the default
    # width_px=1400 the axes are 4.64 pt per unit and "Category number 12"
    # is 82-102 pt = 17.6-22.0 units wide, while the old guard
    # (w >= 1.0 + 0.62 * len(name)) asked for 12.2 — so at the 24-tile cap
    # names overprinted each other and spilled across tile borders
    # (measured 2026-09-16).
    pts_per_unit = ax.get_window_extent().width * 72.0 / ax.get_figure().dpi / 100.0
    name_pt = max(7.0, ctx.d.axis_size_pt - 2)
    value_pt = max(6.5, ctx.d.axis_size_pt - 3)
    for j, ((cat, value), (x, y, w, h)) in enumerate(zip(pairs, rects)):
        fill = ctx.category_colour(j, cat)
        ax.add_patch(Rectangle((x, y), w, h, facecolor=fill, edgecolor=ctx.bg, linewidth=1.4))
        share = value / total
        text_colour = _slice_label_style(ctx, fill)
        name = _tick_text(cat)
        value_text = f"{ctx.label(value)} · {share * 100:.0f}%"
        room = (w - 1.0) * pts_per_unit  # half a unit of padding each side
        fits = (h >= 5.0 and pts_per_unit > 0
                and _text_width_pt(name, name_pt) <= room
                and _text_width_pt(value_text, value_pt) <= room)
        if fits:
            ax.text(x + w / 2, y + h / 2 - 1.2, name, ha="center", va="center", **_label_kw(ctx, text_colour))
            ax.text(x + w / 2, y + h / 2 + 2.6, value_text, ha="center", va="center",
                    **_text_kw(ctx.style.data_label, value_pt, text_colour))
        elif w >= 6.0 and h >= 5.0:
            ax.text(x + w / 2, y + h / 2, f"{share * 100:.0f}%", ha="center", va="center", **_label_kw(ctx, text_colour))
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 62)
    ax.invert_yaxis()  # the largest rectangle reads top-left, as a treemap must
    ax.set_xticks([])
    ax.set_yticks([])
    for side in ax.spines.values():
        side.set_visible(False)
    ax.set_aspect("auto")
    return True


def _draw_violin(ax, ctx: _Ctx) -> bool:
    """matplotlib's violinplot over each group's sample, with that group's
    quartile box drawn inside it — the curve shows the shape, the box keeps
    the five numbers a reader can quote."""
    c = ctx.chart
    dists = c.extra.violin if c.extra else []
    boxes = {b.name: b for b in (c.extra.box if c.extra else [])}
    if not dists:
        return _draw_box(ax, ctx)
    positions = list(range(1, len(dists) + 1))
    parts = ax.violinplot([list(d.values) for d in dists], positions=positions, widths=0.78,
                          showextrema=False, showmedians=False)
    base = ctx.base_colour()
    for j, body in enumerate(parts["bodies"]):
        fill = ctx.style.category_colors.get(dists[j].name, base)
        body.set_facecolor(fill)
        body.set_edgecolor(fill)
        body.set_alpha(0.42)
    for j, d in enumerate(dists):
        bx = boxes.get(d.name)
        if bx is None:
            continue
        ax.vlines(positions[j], bx.whisker_low, bx.whisker_high, color=ctx.muted, linewidth=1.0)
        ax.vlines(positions[j], bx.q1, bx.q3, color=ctx.ink, linewidth=5.0)
        ax.plot([positions[j]], [bx.median], marker="o", markersize=4.0, color=WHITE, markeredgecolor=ctx.ink, markeredgewidth=0.8)
    ax.set_xticks(positions)
    ax.set_xticklabels([_tick_text(d.name) for d in dists],
                       rotation=30 if len(dists) > 6 or any(len(d.name) > 10 for d in dists) else 0,
                       ha="right" if len(dists) > 6 or any(len(d.name) > 10 for d in dists) else "center")
    ax.set_xlim(0.4, len(dists) + 0.6)
    _value_axis(ax, ctx)
    return True


def _draw_candlestick(ax, ctx: _Ctx) -> bool:
    from matplotlib.patches import Patch, Rectangle

    c = ctx.chart
    candles = c.extra.candles if c.extra else []
    up = ctx.style.series_colors.get("Up") or ctx.style.color or ctx.d.palette[0]
    down = ctx.style.series_colors.get("Down") or ctx.d.palette[1]
    # Wide candles overlap; the body keeps a gap at any count.
    body_w = 0.62 if len(candles) <= 60 else max(0.2, 40.0 / len(candles))
    for i, cd in enumerate(candles):
        colour = up if cd.close >= cd.open else down
        ax.vlines(i, cd.low, cd.high, color=colour, linewidth=1.0)
        lo, hi = min(cd.open, cd.close), max(cd.open, cd.close)
        height = hi - lo
        if height <= 0:
            ax.hlines(lo, i - body_w / 2, i + body_w / 2, color=colour, linewidth=1.6)
        else:
            ax.add_patch(Rectangle((i - body_w / 2, lo), body_w, height, facecolor=colour, edgecolor=colour, linewidth=0.8))
    _category_ticks(ax, [cd.label for cd in candles], ctx)
    _value_axis(ax, ctx)
    ax.set_xlim(-0.8, len(candles) - 0.2)
    lows = [cd.low for cd in candles] or [0.0]
    highs = [cd.high for cd in candles] or [1.0]
    pad = (max(highs) - min(lows)) * 0.06 or 1.0
    if ctx.style.y_min is None and ctx.style.y_max is None and not ctx.style.log_y:
        # A price axis does NOT start at zero: the whole point is the range.
        ax.set_ylim(min(lows) - pad, max(highs) + pad)
    handles = [Patch(color=up, label="Close at or above open"), Patch(color=down, label="Close below open")]
    _legend(ax, ctx, handles, [h.get_label() for h in handles])
    return True


def _draw_sunburst(ax, ctx: _Ctx) -> bool:
    """Two rings from one grouped computation: the inner ring is a category's
    total, the outer ring its `group_by` parts, each the parent's colour
    lightened so the parent is still readable."""
    c = ctx.chart
    cats = list(c.categories)
    per_cat = [[float(s.values[j]) for s in c.series] for j in range(len(cats))]
    inner = [sum(v for v in row if v > 0) for row in per_cat]
    total = sum(inner)
    if total <= 0:
        ax.text(0.5, 0.5, "No positive values to chart", ha="center", va="center", color=ctx.muted, transform=ax.transAxes)
        ax.set_axis_off()
        return True
    keep = [j for j, v in enumerate(inner) if v > 0]
    inner_colours = [ctx.category_colour(pos, cats[j]) for pos, j in enumerate(keep)]
    outer_values: List[float] = []
    outer_colours: List[str] = []
    outer_labels: List[str] = []
    for pos, j in enumerate(keep):
        parts = [(k, v) for k, v in enumerate(per_cat[j]) if v > 0]
        for step, (k, v) in enumerate(parts):
            outer_values.append(v)
            outer_colours.append(_mix(inner_colours[pos], WHITE, 0.18 + 0.52 * (step / max(1, len(parts) - 1)) if len(parts) > 1 else 0.3))
            outer_labels.append(c.series[k].name)
    edge = {"linewidth": 1.0, "edgecolor": ctx.bg}
    ax.pie([inner[j] for j in keep], colors=inner_colours, radius=0.66, startangle=90, counterclock=False,
           wedgeprops={**edge, "width": 0.42})
    wedges, _texts = ax.pie(outer_values, colors=outer_colours, radius=1.0, startangle=90, counterclock=False,
                            wedgeprops={**edge, "width": 0.32})
    for pos, j in enumerate(keep):
        share = inner[j] / total
        if share < 0.05:
            continue
        start = sum(1 for q in range(pos) for v in per_cat[keep[q]] if v > 0)
        run = sum(1 for v in per_cat[j] if v > 0)
        if not run:
            continue
        a0, a1 = wedges[start].theta1, wedges[start + run - 1].theta2
        ang = math.radians((a0 + a1) / 2)
        colour = _slice_label_style(ctx, inner_colours[pos])
        ax.text(0.46 * math.cos(ang), 0.46 * math.sin(ang), _tick_text(cats[j]), ha="center", va="center", **_label_kw(ctx, colour))
    for wedge, value, label, fill in zip(wedges, outer_values, outer_labels, outer_colours):
        if value / total < 0.045:
            continue
        ang = math.radians((wedge.theta1 + wedge.theta2) / 2)
        colour = _slice_label_style(ctx, fill)
        ax.text(0.84 * math.cos(ang), 0.84 * math.sin(ang), _tick_text(label), ha="center", va="center",
                **_text_kw(ctx.style.data_label, max(6.5, ctx.d.axis_size_pt - 3), colour))
    ax.text(0, 0, ctx.label(total), ha="center", va="center", **_text_kw(ctx.style.title, ctx.d.title_size_pt, ctx.ink, bold=True))
    ax.axis("equal")
    return True


#: Qualitative bands are context, never the measure: they stay grey so the
#: actual bar and the target line are the only coloured marks on the row.
_BAND_GREYS: Tuple[str, ...] = ("#E7ECF2", "#D7DEE7", "#C5CFDB", "#B3C0CF")


def _draw_bullet(ax, ctx: _Ctx) -> bool:
    c = ctx.chart
    rows = c.extra.bullets if c.extra else []
    base = ctx.base_colour()
    target_colour = ctx.style.series_colors.get("Target") or ctx.ink
    # The axis must hold every mark, including negative ones. The old span
    # was max(actual, target, *(bands or [0.0])) per row, so a table whose
    # numbers are ALL negative (Loss -30 against a target of -100) took the
    # injected 0.0 as each row's maximum, collapsed the span to 0.0 -> 1.0
    # and set xlim(0, 1.32): measured 2026-09-16, every bar was drawn outside
    # the axes and the picture was a blank grid with a floating
    # "-30 · 30% of target". The floor below keeps them on the canvas.
    marks = [v for r in rows for v in (r.actual, r.target, *(r.bands or ()))]
    top = max(marks) if marks else 0.0
    floor = min(marks) if marks else 0.0
    span = top if top > 0 else 1.0
    for j, r in enumerate(rows):
        edges = list(r.bands) or []
        previous = 0.0
        for b_i, edge in enumerate(edges):
            ax.barh(j, max(0.0, edge - previous), left=previous, height=0.66,
                    color=_BAND_GREYS[min(b_i, len(_BAND_GREYS) - 1)], linewidth=0)
            previous = edge
        fill = ctx.style.category_colors.get(r.label, base)
        ax.barh(j, r.actual, height=0.3, color=fill, label="Actual" if j == 0 else None, zorder=3)
        ax.vlines(r.target, j - 0.24, j + 0.24, color=target_colour, linewidth=2.6, zorder=4,
                  label="Target" if j == 0 else None)
        share = (r.actual / r.target * 100.0) if r.target else None
        text = ctx.label(r.actual) + (f" · {share:.0f}% of target" if share is not None else "")
        ax.annotate(text, xy=(max([r.actual, r.target, *edges]), j), xytext=(6, 0), textcoords="offset points",
                    va="center", ha="left", **_label_kw(ctx, ctx.ink))
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([_tick_text(r.label) for r in rows])
    ax.invert_yaxis()
    ax.set_ylim(len(rows) - 0.5, -0.5)
    ax.set_xlim(min(0.0, floor * 1.15), span * 1.32)
    _value_axis(ax, ctx, "x")
    handles, labels = ax.get_legend_handles_labels()
    _legend(ax, ctx, handles, labels)
    return True


_DRAWERS = {
    "bar": _draw_bars, "horizontal_bar": _draw_bars, "stacked_bar": _draw_bars, "stacked_horizontal_bar": _draw_bars,
    "percent_stacked_bar": _draw_bars, "line": _draw_line, "area": _draw_area, "stacked_area": _draw_area,
    "pie": _draw_pie, "donut": _draw_pie, "scatter": _draw_scatter, "bubble": _draw_scatter, "histogram": _draw_histogram,
    "combo": _draw_combo, "box": _draw_box, "heatmap": _draw_heatmap, "waterfall": _draw_waterfall, "funnel": _draw_funnel,
    "gantt": _draw_gantt, "radar": _draw_radar, "pareto": _draw_pareto, "treemap": _draw_treemap,
    "violin": _draw_violin, "candlestick": _draw_candlestick, "sunburst": _draw_sunburst, "bullet": _draw_bullet,
}


__all__ = [
    "ChartStyleDefaults", "defaults_from", "contrast_ratio", "format_value", "indian_group", "chart_warnings",
    "font_for_script", "scripts_in", "render_png", "render_svg", "render_chart_png", "render_standalone", "clean_svg",
    "title_artist",
    "squarified",
]
