"""Row caps: 500-row meta.data preview and 100k-row export (spec §8)."""
from app.core.exports import EXPORT_ROW_CAP, PREVIEW_ROW_CAP, apply_export_cap, cap_rows


def test_cap_constants():
    assert PREVIEW_ROW_CAP == 500
    assert EXPORT_ROW_CAP == 100_000


def test_preview_cap_truncates_at_500():
    rows = [[i] for i in range(600)]
    capped, truncated = cap_rows(rows, PREVIEW_ROW_CAP)
    assert len(capped) == 500
    assert truncated is True
    assert capped[0] == [0] and capped[-1] == [499]


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
    assert s.sql_preview_row_cap == 500
    assert s.export_row_cap == 100_000
