"""Trusted histogram binning.

Bin edges are computed here, in Python, over the rows the database
returned. The model is never asked where they should go and never shown a
record to decide — that is what keeps a histogram a measurement rather
than an opinion.
"""
from app.core.chart_data import (
    BIN_COLUMN,
    COUNT_COLUMN,
    build_histogram,
    clamp_bins,
    default_bin_count,
)
from app.core.chart_spec import MAX_BINS, MIN_BINS

COLS = ["name", "amount"]


def rows_for(values):
    return [[f"r{i}", v] for i, v in enumerate(values)]


def test_bin_count_is_deterministic_for_a_given_row_count():
    assert default_bin_count(100) == default_bin_count(100)
    assert default_bin_count(100) == 10


def test_bin_count_stays_inside_sane_bounds():
    assert default_bin_count(2) >= 5
    assert default_bin_count(10_000) <= 20


def test_a_requested_bin_count_is_clamped_not_trusted():
    assert clamp_bins(1, 100) == MIN_BINS
    assert clamp_bins(10_000, 100) == MAX_BINS
    assert clamp_bins(12, 100) == 12


def test_a_nonsense_bin_count_falls_back_to_the_default():
    assert clamp_bins("many", 100) == default_bin_count(100)
    assert clamp_bins(None, 100) == default_bin_count(100)


def test_every_observation_lands_in_exactly_one_bin():
    values = list(range(0, 100))
    result = build_histogram(COLS, rows_for(values), "amount", bins=10)
    assert result is not None
    cols, binned, k = result
    assert cols == [BIN_COLUMN, COUNT_COLUMN]
    assert k == 10
    assert sum(r[1] for r in binned) == len(values)


def test_the_maximum_value_is_counted_not_dropped():
    """The obvious int((v-lo)/width) puts the maximum in bin k, which does
    not exist. Closing the last bin on the right is what keeps the counts
    adding up to the row count."""
    result = build_histogram(COLS, rows_for([0, 5, 10]), "amount", bins=5)
    cols, binned, _ = result
    assert sum(r[1] for r in binned) == 3
    assert binned[-1][1] >= 1  # the 10 is in the last bin


def test_a_constant_column_gives_one_bin_not_a_division_by_zero():
    result = build_histogram(COLS, rows_for([7, 7, 7]), "amount")
    cols, binned, k = result
    assert k == 1
    assert binned == [["7", 3]]


def test_integer_data_gets_integer_labels():
    _, binned, _ = build_histogram(COLS, rows_for(list(range(0, 20))), "amount", bins=5)
    assert all(" - " in str(r[0]) for r in binned)
    assert "." not in str(binned[0][0])


def test_non_numeric_values_are_skipped_not_coerced_to_zero():
    rows = [["a", 10], ["b", None], ["c", "n/a"], ["d", 20]]
    _, binned, _ = build_histogram(COLS, rows, "amount")
    assert sum(r[1] for r in binned) == 2  # only 10 and 20 counted


def test_salesforce_text_booleans_are_not_binned_as_numbers():
    rows = [["a", "true"], ["b", "false"]]
    assert build_histogram(COLS, rows, "amount") is None


def test_a_missing_column_returns_none():
    assert build_histogram(COLS, rows_for([1, 2]), "ghost") is None


def test_a_column_with_no_numeric_values_returns_none():
    assert build_histogram(COLS, [["a", "x"], ["b", "y"]], "amount") is None


def test_binning_is_stable_across_calls():
    """The browser and the report PNG draw the SAME binned rows, because
    the binning happens once, here, and both renderers receive its output."""
    values = [3, 9, 14, 22, 27, 31, 44, 58, 61, 79]
    first = build_histogram(COLS, rows_for(values), "amount")
    second = build_histogram(COLS, rows_for(values), "amount")
    assert first == second
