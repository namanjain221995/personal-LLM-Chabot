"""Row caps: 500-row meta.data preview and 100k-row export (spec §8)."""
from app.core.exports import EXPORT_ROW_CAP, PREVIEW_ROW_CAP, apply_export_cap, cap_rows


def test_cap_constants():
    # Raised 500 -> 2000 on 2026-08-29: a 225-row answer was being cut to 500
    # only in the sense that larger ordinary results were; 2000 covers normal
    # Salesforce result sets while still bounding what the browser must paint.
    assert PREVIEW_ROW_CAP == 2000
    assert EXPORT_ROW_CAP == 100_000


def test_preview_cap_truncates_at_the_preview_cap():
    rows = [[i] for i in range(PREVIEW_ROW_CAP + 100)]
    capped, truncated = cap_rows(rows, PREVIEW_ROW_CAP)
    assert len(capped) == PREVIEW_ROW_CAP
    assert truncated is True
    assert capped[0] == [0] and capped[-1] == [PREVIEW_ROW_CAP - 1]


def test_preview_cap_exact_boundary_not_truncated():
    rows = [[i] for i in range(500)]
    capped, truncated = cap_rows(rows, PREVIEW_ROW_CAP)
    assert len(capped) == 500
    assert truncated is False


def test_preview_cap_under_limit():
    rows = [[i] for i in range(3)]
    capped, truncated = cap_rows(rows, PREVIEW_ROW_CAP)
    assert capped == [[0], [1], [2]]
    assert truncated is False


def test_export_cap_truncates_at_100k():
    rows = [[i] for i in range(EXPORT_ROW_CAP + 1)]
    capped, truncated = apply_export_cap(rows)
    assert len(capped) == EXPORT_ROW_CAP
    assert truncated is True


def test_export_cap_exact_boundary_not_truncated():
    rows = [[i] for i in range(EXPORT_ROW_CAP)]
    capped, truncated = apply_export_cap(rows)
    assert len(capped) == EXPORT_ROW_CAP
    assert truncated is False


def test_config_defaults_match_spec_caps():
    from app.config import Settings

    s = Settings()
    assert s.sql_preview_row_cap == PREVIEW_ROW_CAP
    assert s.export_row_cap == 100_000
