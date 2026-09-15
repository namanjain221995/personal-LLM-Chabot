"""Requested proprietary font names map to installed open equivalents.

A person asks for Georgia, Arial or Calibri; the orchestrator images carry
open-licence fonts only. The Office files keep the requested name; what the
server DRAWS (the PDF, chart images) is the first installed family of
FontFace.pdf_candidates — the family, its metric twin, the documented open
fallback, the generic Liberation/DejaVu — and the self-check must call that
mapped family met and say which font was used. The fontconfig answers are
stubbed in the unit tests so they run anywhere; the real-render tests run
where the fonts are installed (the rebuilt image) and skip, with the reason,
elsewhere.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.artifacts import requirements as RQ
from app.artifacts import selfcheck as SC
from app.artifacts import style as ST
from app.artifacts.render import theme

ORCH = Path(__file__).resolve().parents[1]


def _installed(monkeypatch, *families: str) -> None:
    have = {f.casefold() for f in families}
    monkeypatch.setattr(theme, "font_installed", lambda name: bool(name) and name.casefold() in have)


# ------------------------------------------------------------- the images --


@pytest.mark.parametrize("dockerfile", ["Dockerfile.cuda", "Dockerfile.cpu"])
def test_both_images_install_the_open_font_set(dockerfile):
    text = (ORCH / dockerfile).read_text()
    block = re.search(r"apt-get install -y --no-install-recommends \\\n(.*?)&& rm -rf", text, re.S)
    assert block, dockerfile
    packages = set(re.findall(r"^\s+([a-z0-9][a-z0-9.+-]+) \\$", block.group(1), re.M))
    wanted = {"fonts-liberation", "fonts-liberation2", "fonts-crosextra-carlito", "fonts-crosextra-caladea",
              "fonts-dejavu-core", "fonts-noto-core", "fonts-lohit-deva", "fonts-lohit-gujr"}
    assert wanted <= packages, sorted(wanted - packages)


# ------------------------------------------------------------ the mapping --


def test_georgia_maps_to_its_twin_then_the_documented_fallback():
    face = ST.font_face("Georgia")
    assert face.pdf_candidates == ("Georgia", "Gelasio", "Caladea", "Liberation Serif", "DejaVu Serif")
    stack = face.css_stack
    assert stack.index('"Caladea"') < stack.index('"Liberation Serif"')
    assert ST.font_face("arial").pdf_candidates[:2] == ("Arial", "Liberation Sans") and ST.font_face("Arial").metric_compatible
    assert ST.font_face("Calibri").metric_compatible and ST.font_face("Cambria").metric_compatible
    assert not ST.font_face("Verdana").metric_compatible, "DejaVu Sans stands in for Verdana but is not its metric twin"


@pytest.mark.parametrize("installed,family,kind", [
    (("Georgia", "Caladea", "Liberation Serif"), "Georgia", "exact"),
    (("Gelasio", "Caladea", "Liberation Serif"), "Gelasio", "metric"),
    (("Caladea", "Liberation Serif"), "Caladea", "fallback"),
    (("Liberation Serif",), "Liberation Serif", "generic"),
    ((), "", "none"),
])
def test_resolve_font_names_the_family_and_why(monkeypatch, installed, family, kind):
    _installed(monkeypatch, *installed)
    chosen = theme.resolve_font(ST.font_face("Georgia"))
    assert (chosen.family, chosen.kind) == (family, kind)


def test_the_pdf_stack_puts_the_mapped_family_first(monkeypatch):
    """fontconfig's own alias for a missing name wins otherwise: on Ubuntu
    noble with fonts-noto-core, `fc-match Georgia` is Noto Serif, and on a
    host with DejaVu it is DejaVu Serif, ahead of the Caladea in the stack."""
    _installed(monkeypatch, "Caladea", "Liberation Serif")
    face = ST.font_face("Georgia")
    assert theme.pdf_font_stack(face) == f'"Caladea", {face.css_stack}'
    _installed(monkeypatch, "Georgia", "Caladea")
    assert theme.pdf_font_stack(face) == face.css_stack


def test_the_pdf_stylesheet_uses_the_mapped_family(monkeypatch):
    from app.artifacts.render import html as H

    _installed(monkeypatch, "Caladea", "Carlito", "Liberation Serif", "Liberation Sans")
    R = ST.resolve(None)
    ts = ST.TextStyle(font_family="Georgia")
    assert H.css_decls(ts, R, keys=["font_family"]).startswith('font-family:"Caladea", "Georgia"')


def test_the_render_warning_names_the_fallback(monkeypatch):
    from app.artifacts.render import font_substitution_warnings
    from tests.test_artifact_render_samples import document

    _installed(monkeypatch, "Caladea", "Liberation Serif")
    spec = document("generic", sections=1)
    spec.body.style = ST.StyleSpec(fonts=ST.FontSpec(body="Georgia"))
    warnings = font_substitution_warnings(spec, ["pdf", "docx"])
    assert warnings == ["Georgia is not installed on this server, so the PDF uses Caladea, its documented open fallback (no metric-compatible "
                        "font for it is packaged); the DOCX file names Georgia, which shows on a computer that has it."]


def test_the_chart_painter_maps_a_requested_family():
    from app.artifacts.render import charts as C

    assert C._mapped("Georgia")[:3] == ["Georgia", "Gelasio", "Caladea"]
    assert C._mapped("Not A Font") == ["Not A Font"]


# ------------------------------------------------------------- self-check --


def test_a_mapped_equivalent_is_met_and_says_which_font(monkeypatch):
    _installed(monkeypatch, "Caladea", "Carlito", "Liberation Serif", "Liberation Sans", "Noto Sans Devanagari")
    ok, note = SC.font_matches("Caladea", "Georgia")
    assert ok and "Caladea" in note and "Georgia" in note and "fallback" in note, note
    ok, note = SC.font_matches("Liberation Sans", "Arial")
    assert ok and "Liberation Sans" in note
    ok, note = SC.font_matches("Noto Sans Devanagari", "Georgia")
    assert ok and "Devanagari" in note
    assert SC.font_matches("Noto Serif Devanagari", "Georgia")[0] and SC.font_matches("Lohit Gujarati", "Calibri")[0], \
        "Pango picks the serif Devanagari face for a serif stack; the Latin family cannot draw that text"
    assert SC.font_matches("Georgia", "Georgia") == (True, "")


def test_an_unmapped_font_is_still_unmet(monkeypatch):
    # Gelasio installed: Georgia maps to Gelasio, so Caladea is NOT what the renderer would pick.
    _installed(monkeypatch, "Gelasio", "Caladea", "Liberation Serif")
    assert SC.font_matches("Caladea", "Georgia")[0] is False
    _installed(monkeypatch, "Caladea", "Liberation Serif", "Liberation Sans")
    assert SC.font_matches("Liberation Sans", "Georgia")[0] is False, "a sans font never meets a serif request"
    assert SC.font_matches("DejaVu Serif", "Georgia")[0] is False, "fontconfig's own alias is not the mapping"
    assert SC.font_matches("Liberation Serif", "Arial")[0] is False
    assert SC.font_matches("DejaVu Sans", "Georgia")[0] is False, "DejaVu Sans draws Arabic, but is no stand-in for Georgia"


def test_the_claimable_line_names_the_font_used(monkeypatch):
    _installed(monkeypatch, "Caladea", "Liberation Serif")
    item = RQ.ChecklistItem("c1", "style", "paragraph", "font_family", "Georgia", must=True)
    obs = [SC.I.Observation("paragraph", "font_family", "Georgia", "docx"),
           SC.I.Observation("paragraph", "font_family", "Caladea", "pdf")] + \
          [SC.I.Observation("paragraph", "font_family", "Caladea", "pdf") for _ in range(10)] + \
          [SC.I.Observation("paragraph", "font_family", "Noto Serif Devanagari", "pdf")]
    result = SC._eval_style(item, obs)
    assert result.result == "pass", result
    report = SC.summarize([result])
    assert report.unmet == []
    claim = report.false_claim_guard["claimable"][0]
    assert "Georgia" in claim and "Caladea" in claim, claim


# --------------------------------------------------- real files, real fonts --


def _real_fonts_or_skip(*families: str) -> None:
    theme.font_installed.cache_clear()
    missing = [f for f in families if not theme.font_installed(f)]
    if missing:
        pytest.skip(f"{', '.join(missing)} not installed here (the orchestrator images install them)")


def test_a_georgia_pdf_is_drawn_in_caladea_and_the_selfcheck_calls_it_met(tmp_path):
    pytest.importorskip("weasyprint")
    pytest.importorskip("pypdfium2")
    theme.font_installed.cache_clear()
    if theme.font_installed("Georgia") or theme.font_installed("Gelasio"):
        pytest.skip("Georgia or Gelasio is installed here, so the fallback path is not what renders")
    _real_fonts_or_skip("Caladea")
    from app.artifacts import inspect_files as I
    from app.artifacts.render import render_version
    from tests.test_artifact_render_samples import document

    spec = document("generic", sections=2, toc=False)
    spec.body.style = ST.StyleSpec(fonts=ST.FontSpec(body="Georgia"))
    report = render_version(spec, ["pdf", "docx"], str(tmp_path), title_slug="georgia", version=1)
    files = {f.format: tmp_path / f.filename for f in report.files}
    runs, _, _ = I.pdf_runs(files["pdf"])
    body = [r for r in runs if r.text.strip().startswith("This section explains")]
    assert body and all("Caladea" in r.font for r in body), sorted({r.font for r in body})
    obs = I.inspect(files, spec)
    item = RQ.ChecklistItem("c1", "style", "paragraph", "font_family", "Georgia", must=True)
    result = SC._eval_style(item, obs)
    assert result.result == "pass" and result.by_format.get("pdf") == "pass", (result.by_format, result.evidence)
    assert any("Caladea" in e for e in result.evidence), result.evidence
    assert any(w.startswith("Georgia is not installed on this server, so the PDF uses Caladea") for w in report.warnings), report.warnings
