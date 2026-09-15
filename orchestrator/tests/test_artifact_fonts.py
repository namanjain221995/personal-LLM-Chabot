"""The fonts the styling engine relies on, as fontconfig sees them.

The orchestrator images (Dockerfile.cuda / Dockerfile.cpu) install DejaVu,
Liberation 2, fonts-crosextra-carlito/caladea (metric twins of Calibri and
Cambria, the TechSara Classic families; Caladea is also Georgia's documented
fallback), fonts-noto-core and fonts-lohit-deva/gujr (Hindi and Gujarati).
Hosts and CI may carry fewer, so the image-only families are asserted where
they are installed and skipped, with the reason, elsewhere — the same file
checks the host, CI and the rebuilt image. The name mapping itself is
tests/test_artifact_font_mapping.py.
"""
from __future__ import annotations

import shutil

import pytest

from app.artifacts import style as ST
from app.artifacts.render import theme

pytestmark = pytest.mark.skipif(shutil.which("fc-match") is None, reason="fontconfig (fc-match) is not installed")

BASELINE = ("DejaVu Sans", "Liberation Sans", "Liberation Serif")
PROPOSED = ("Carlito", "Caladea", "Lohit Devanagari", "Lohit Gujarati", "Noto Sans Devanagari", "Noto Sans Gujarati")


@pytest.mark.parametrize("family", BASELINE)
def test_the_baseline_fonts_resolve(family):
    assert theme.font_installed(family), f"{family} is in every orchestrator image (Dockerfile*: fonts-dejavu-core, fonts-liberation)"


@pytest.mark.parametrize("family", PROPOSED)
def test_the_proposed_fonts_resolve_when_installed(family):
    if not theme.font_installed(family):
        pytest.skip(f"{family} is not installed here (Dockerfile.cuda/.cpu install it; this host or image predates the rebuild)")
    assert theme.installed_family((family,)) == family


def test_the_probe_never_accepts_a_fallback_or_a_hostile_name():
    assert theme.font_installed("No Such Family 12345") is False
    assert theme.font_installed("Liberation Sans; rm -rf /") is False
    assert theme.font_installed("") is False


def test_classic_falls_back_to_a_metric_twin_or_liberation():
    face = ST.font_face("Calibri")
    chosen = theme.installed_family(face.installed_file_candidates) or (face.metric_substitute if theme.font_installed(face.metric_substitute) else "")
    assert chosen in ("Calibri", "Carlito", "") and "Liberation Sans" in face.css_stack


@pytest.mark.parametrize("script,sample,family", [("Devanagari", "सारांश", "Lohit Devanagari"), ("Gujarati", "સારાંશ", "Lohit Gujarati")])
def test_indic_text_warns_only_where_no_font_covers_it(script, sample, family):
    covered = bool(theme.installed_family(theme.SCRIPT_FONTS[script]))
    warning = theme.font_coverage_warning(sample)
    if covered:
        assert warning == ""
    else:
        assert script in warning
    if not theme.font_installed(family) and not covered:
        pytest.skip(f"{family} is not installed here")
