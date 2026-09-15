"""AS3 final-check fixes (2026-09-15): the document language follows a Hindi or
Gujarati request, and every pie slice above the size floor keeps its label."""
from types import SimpleNamespace

from app.artifacts import compose
from app.artifacts.render import charts


def _system(instruction: str) -> str:
    req = compose.ComposeRequest(kind="document", formats=["pdf"], template_id="generic", effort="fast",
                                 instruction=instruction)
    budget = SimpleNamespace(max_sources=5, max_sections=8, max_slides=10, max_sheets=3)
    return compose._material_messages(req, budget=budget)[0]["content"]


def test_a_hindi_request_asks_for_a_hindi_document():
    assert "Hindi (Devanagari script)" in _system("कर्मचारियों के लिए प्रशिक्षण योजना पर एक पीडीएफ बनाओ")


def test_a_gujarati_request_asks_for_a_gujarati_document():
    assert "Gujarati (Gujarati script)" in _system("ગ્રાહક પ્રતિસાદ સર્વે માટે એક પીડીએફ બનાવો")


def test_english_and_hinglish_requests_add_no_language_line():
    for text in ("make a pdf on the employee training plan", "training plan ki pdf bana do"):
        assert "Write every title" not in _system(text)


def _ctx():
    d = charts.ChartStyleDefaults()
    return SimpleNamespace(d=d, ink=d.ink)


def test_mid_tone_slices_keep_a_readable_label():
    ctx = _ctx()
    for fill in ("#4285F4", "#EA4335", "#34A853", "#FBBC05", "#1F3864", "#FFFFFF"):
        colour, bbox = charts._slice_label_style(ctx, fill)
        if bbox is None:
            assert charts.contrast_ratio(colour, fill) >= 4.5
        else:
            assert bbox["facecolor"] == "#FFFFFF"
            assert charts.contrast_ratio(colour, "#FFFFFF") >= 4.5
