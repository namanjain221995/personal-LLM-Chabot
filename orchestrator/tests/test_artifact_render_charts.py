"""Chart PNGs: every chart type draws, output is deterministic, the palette
is the app's, and the module stays import-light (matplotlib.pyplot is lazy)."""
from __future__ import annotations

import sys

import pytest

from app.artifacts import spec as S
from app.artifacts.render import charts, theme

pytest.importorskip("matplotlib")


def chart(**over) -> S.Chart:
    base = dict(type="bar", title="T", categories=["A", "B", "C"], series=[S.Series(name="one", values=[1, 2, 3])])
    base.update(over)
    return S.Chart(**base)


@pytest.mark.parametrize("kind", ["bar", "horizontal_bar", "line", "pie"])
def test_every_chart_type_draws_a_png(tmp_path, kind):
    out = charts.render_chart_png(chart(type=kind), tmp_path / f"{kind}.png")
    data = out.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    from PIL import Image

    with Image.open(out) as im:
        w, h = im.size
    # 8 x 4.5 in at 160 dpi, before tight-layout trimming, is 1280 x 720.
    assert 1100 <= w <= 1300 and 600 <= h <= 740


def test_render_is_byte_deterministic(tmp_path):
    """The same Chart twice → the same bytes, so a cache keyed on the spec
    hash is safe and a re-render of a version is identical."""
    c = chart(type="bar", series=[S.Series(name="one", values=[1, 2, 3]), S.Series(name="two", values=[3, 2, 1])])
    a = charts.render_chart_png(c, tmp_path / "a.png").read_bytes()
    b = charts.render_chart_png(c, tmp_path / "b.png").read_bytes()
    assert a == b


def test_palette_is_the_apps_five_colours(tmp_path):
    """The first two series use --ts-chart-1 and --ts-chart-2 (charts_png.py
    and chartTheme.ts use the same order). Sampled from the bars."""
    from PIL import Image

    c = chart(type="bar", categories=["A"], series=[S.Series(name="one", values=[10]), S.Series(name="two", values=[10])])
    out = charts.render_chart_png(c, tmp_path / "p.png")
    with Image.open(out) as im:
        pixels = set(im.convert("RGB").getdata())
    assert theme.hex_to_rgb(theme.PALETTE[0]) in pixels
    assert theme.hex_to_rgb(theme.PALETTE[1]) in pixels


def test_theme_palette_matches_the_legacy_report_painter():
    """theme.PALETTE and core/charts_png.py both carry the app's five series
    colours as literals (the legacy painter is not this feature's to edit);
    this keeps them from drifting apart — a chart in a report and a chart in
    an artifact must be the same colours in the same order."""
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "app" / "core" / "charts_png.py").read_text(encoding="utf-8")
    legacy = re.findall(r'"(#[0-9a-f]{6})"', source.split("set_prop_cycle", 1)[1].split(")", 1)[0])
    assert tuple(legacy) == theme.PALETTE


def test_pie_folds_past_eight_slices(tmp_path):
    cats = [f"c{i}" for i in range(12)]
    c = chart(type="pie", categories=cats, series=[S.Series(name="s", values=list(range(1, 13)))])
    assert charts.render_chart_png(c, tmp_path / "pie.png").stat().st_size > 0


def test_pie_with_no_positive_values_does_not_crash(tmp_path):
    # All-zero is refused by the spec now (a chart the model emptied); a
    # pie of negatives and zeros still reaches the renderer and must not crash.
    c = chart(type="pie", series=[S.Series(name="s", values=[-1, 0, -2])])
    assert charts.render_chart_png(c, tmp_path / "zero.png").stat().st_size > 0


@pytest.mark.parametrize("kind", ["bar", "horizontal_bar", "line", "pie"])
def test_dollar_signs_are_text_not_tex(tmp_path, kind):
    """Finance text is full of dollars. With mathtext on, "Revenue $M vs $K"
    lost the words between the dollars and "a $\\frac$ b" raised a
    ParseSyntaxException that failed the whole render (review 2026-09-11).
    Title, category, series name and axis label each carry both shapes."""
    series = [S.Series(name="a $\\frac$ b $M vs $K", values=[1, 2, 3])]
    if kind != "pie":
        series.append(S.Series(name="$K", values=[3, 2, 1]))   # a second series puts the names in a legend
    c = S.Chart(
        type=kind, title="a $\\frac$ b — Revenue $M vs $K", categories=["$\\frac$", "$M vs $K", "$1,000"],
        series=series, y_label="$M vs $K $\\alpha$",
    )
    out = charts.render_chart_png(c, tmp_path / f"dollars-{kind}.png")
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    import matplotlib

    assert matplotlib.rcParams["text.parse_math"] is False


def test_rejects_anything_but_a_validated_chart(tmp_path):
    with pytest.raises(TypeError):
        charts.render_chart_png({"type": "bar"}, tmp_path / "x.png")  # type: ignore[arg-type]


def test_render_package_imports_without_pyplot():
    """The renderers are imported by the app (health, api); pyplot and the
    Office libraries must not come with them (tests/test_imports.py's rule)."""
    import subprocess

    script = (
        "import sys\n"
        "import app.artifacts.render, app.artifacts.render.charts, app.artifacts.render.html, "
        "app.artifacts.render.pdf, app.artifacts.render.docx, app.artifacts.render.pptx, "
        "app.artifacts.render.xlsx, app.artifacts.render.validate, app.artifacts.render.preview, "
        "app.artifacts.render.worker\n"
        "bad = [m for m in ('matplotlib.pyplot', 'weasyprint', 'docx', 'pptx', 'openpyxl', 'pypdfium2') if m in sys.modules]\n"
        "if bad: raise SystemExit('eager: ' + ', '.join(bad))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
