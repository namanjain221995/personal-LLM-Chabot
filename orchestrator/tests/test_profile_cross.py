"""A breakdown by TWO things at once, computed at profile time (2026-09-21).

THE GAP. The 2026-09 audit's own headline request — "Generate a PDF report of
monthly revenue by region" — still did not produce that figure. The profile
computed revenue BY REGION and revenue BY MONTH separately and said so
honestly ("A breakdown of revenue by region for each month is not computed for
this report; the two breakdowns above are"). Honest, and not what was asked.

Every truth below is computed HERE, from the fixture's own text, with Python
Decimal — never from DuckDB, and never from another part of the profile. A
cross cell must match to the cent, exactly as a group total must.
"""
from __future__ import annotations

import csv
import json
import os
import random
from collections import defaultdict
from decimal import Decimal

import pytest

from app.core import profile as profiler
from app.engines import dataset_report as report

REGIONS = ("North", "South", "East", "West", "Central")
CHANNELS = ("direct", "partner", "online")


def _write(path, rows, *, extra_lines=()):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        for line in extra_lines:
            # csv.writer ends every line CRLF; a file of MIXED line endings
            # makes DuckDB 1.5.5 refuse to detect the dialect at all, so a
            # deliberately broken line is written the same way.
            fh.write(line + "\r\n")


def _rows(n, *, seed, months=12, blanks=0, regions=REGIONS):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        m = rng.randrange(months)
        rows.append({
            "order_id": 100000 + i,
            "order_date": f"{2024 + m // 12}-{m % 12 + 1:02d}-{rng.randrange(1, 28):02d}",
            "region": regions[rng.randrange(len(regions))],
            "channel": CHANNELS[rng.randrange(len(CHANNELS))],
            "revenue": f"{rng.randrange(100, 900000) / 100:.2f}",
        })
    for i in rng.sample(range(n), blanks) if blanks else ():
        rows[i]["order_date"] = ""
    return rows


def _truth_cross(rows, left, right, *, monthly):
    """{(left value, right value): (rows, exact total)} with Decimal."""
    acc = defaultdict(lambda: [0, Decimal(0)])
    for r in rows:
        key = r[right][:7] if monthly else r[right]
        if monthly and not key:
            continue
        acc[(r[left], key)][0] += 1
        acc[(r[left], key)][1] += Decimal(r["revenue"])
    return {k: (v[0], v[1]) for k, v in acc.items()}


def _cents(value):
    assert value is not None
    return Decimal(format(value, ".15g") if isinstance(value, float) else str(value)).quantize(
        Decimal("0.01")
    )


def _cross(prof):
    entries = prof["aggregates"].get("by_cross") or []
    return entries[0] if entries else None


def _reasons(prof, lead="by_cross "):
    return [r for r in prof["aggregates"]["omitted"] if r.startswith(lead)]


def _profile(path, message, **kwargs):
    """Profile `path` twice, as the engine does: once to learn what it can
    break down, then again with the pair the request names."""
    first = profiler.profile_tabular(str(path), **kwargs)
    spec = report.cross_request(first, report.request_text(message))
    if spec is None:
        return first, None
    return profiler.profile_tabular(str(path), cross=spec, **kwargs), spec


# ---------------------------------------------------------------------------
# 1. The figure that was missing is computed, and it is exact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n,blanks,broken", [(200, 0, 0), (3000, 137, 1)])
def test_monthly_revenue_by_region_is_computed_exactly(tmp_path, n, blanks, broken):
    rows = _rows(n, seed=n, blanks=blanks)
    path = tmp_path / f"sales_{n}.csv"
    # A line of the wrong width: read, dropped by IGNORE_ERRORS, counted.
    _write(path, rows, extra_lines=["999999,2024-06-01,North"] * broken)

    prof, spec = _profile(path, "Generate a PDF report of monthly revenue by region")
    assert spec == {"group": "region", "across": "order_date", "measure": "revenue"}
    assert prof["rows"] == n
    assert prof.get("rows_not_read", 0) == broken

    entry = _cross(prof)
    assert entry is not None, prof["aggregates"]["omitted"]
    assert (entry["group"], entry["across"], entry["kind"], entry["measure"]) == (
        "region", "order_date", "month", "revenue",
    )
    truth = _truth_cross(rows, "region", "order_date", monthly=True)
    seen = set()
    for row in entry["rows"]:
        for cell in row["cells"]:
            key = (row["value"], cell["month"])
            assert key in truth, f"{key} is not a pair of the file at all"
            want_rows, want_sum = truth[key]
            assert cell["count"] == want_rows, key
            assert _cents(cell["sum"]) == want_sum.quantize(Decimal("0.01")), key
            seen.add(key)
    assert not entry["truncated"] and seen == set(truth), "every pair of a small file is computed"


def test_the_cross_adds_up_to_the_group_totals_beside_it(tmp_path):
    """The cross reconciles: a group's cells add up to its by_group total,
    less exactly the undated rows `omitted` names."""
    rows = _rows(3000, seed=5, blanks=137)
    path = tmp_path / "sales.csv"
    _write(path, rows)
    prof, _ = _profile(path, "monthly revenue by region")
    entry = _cross(prof)
    by_group = next(
        e for e in prof["aggregates"]["by_group"]
        if e["group"] == "region" and e["measure"] == "revenue"
    )
    undated = defaultdict(lambda: Decimal(0))
    for r in rows:
        if not r["order_date"]:
            undated[r["region"]] += Decimal(r["revenue"])
    for row in entry["rows"]:
        cells = sum(_cents(c["sum"]) for c in row["cells"])
        total = _cents(next(g["sum"] for g in by_group["rows"] if g["value"] == row["value"]))
        assert total - cells == undated[row["value"]].quantize(Decimal("0.01")), row["value"]


def test_group_by_group_is_computed_and_reconciles(tmp_path):
    rows = _rows(3000, seed=9)
    path = tmp_path / "sales.csv"
    _write(path, rows)
    prof, spec = _profile(path, "total revenue by region and by channel")
    assert spec == {"group": "region", "across": "channel", "measure": "revenue"}
    entry = _cross(prof)
    assert entry["kind"] == "group"
    truth = _truth_cross(rows, "region", "channel", monthly=False)
    for row in entry["rows"]:
        for cell in row["cells"]:
            want_rows, want_sum = truth[(row["value"], cell["across"])]
            assert cell["count"] == want_rows
            assert _cents(cell["sum"]) == want_sum.quantize(Decimal("0.01"))
    by_group = next(
        e for e in prof["aggregates"]["by_group"]
        if e["group"] == "region" and e["measure"] == "revenue"
    )
    # No row is in no channel, so this cross reconciles exactly.
    for row in entry["rows"]:
        cells = sum(_cents(c["sum"]) for c in row["cells"])
        assert cells == _cents(next(g["sum"] for g in by_group["rows"] if g["value"] == row["value"]))


# ---------------------------------------------------------------------------
# 2. What it leaves out, it says — beside the figures it leaves it out of
# ---------------------------------------------------------------------------


def test_rows_with_no_date_are_said_against_the_cross_with_the_exact_amount(tmp_path):
    rows = _rows(3000, seed=5, blanks=137)
    path = tmp_path / "sales.csv"
    _write(path, rows)
    prof, _ = _profile(path, "monthly revenue by region")
    gap = sum((Decimal(r["revenue"]) for r in rows if not r["order_date"]), Decimal(0))
    said = _reasons(prof)
    assert len(said) == 1, said
    assert said[0].startswith("by_cross region x order_date: 137 row(s) have no date")
    assert "add up to less than the column totals" in said[0]
    assert str(gap.quantize(Decimal("0.01"))) in said[0], said[0]


def test_a_cross_the_budget_cannot_hold_is_cut_honestly_not_silently(tmp_path):
    """Thirty regions over thirty-six months is 1,080 pairs. The cross keeps
    the largest groups and the most recent months INSIDE CROSS_MAX_CELLS, and
    every value it leaves out is counted in `omitted`."""
    regions = tuple(f"R{i:02d}" for i in range(30))
    rows = _rows(20000, seed=3, months=36, regions=regions)
    path = tmp_path / "wide.csv"
    _write(path, rows)
    prof, _ = _profile(path, "monthly revenue by region")
    entry = _cross(prof)
    cells = sum(len(r["cells"]) for r in entry["rows"])
    assert cells <= profiler.CROSS_MAX_CELLS
    assert len(entry["rows"]) <= profiler.CROSS_MAX_GROUPS
    assert entry["truncated"] is True
    left_out = _reasons(prof)[0]
    assert f"{30 - len(entry['rows'])} further region value(s)" in left_out
    months = {c["month"] for r in entry["rows"] for c in r["cells"]}
    assert f"{36 - len(months)} further month(s)" in left_out
    # Every figure that IS listed is still exact.
    truth = _truth_cross(rows, "region", "order_date", monthly=True)
    for row in entry["rows"]:
        for cell in row["cells"]:
            want_rows, want_sum = truth[(row["value"], cell["month"])]
            assert cell["count"] == want_rows
            assert _cents(cell["sum"]) == want_sum.quantize(Decimal("0.01"))


def test_a_small_budget_shrinks_the_cross_and_leaves_the_other_figures_standing(tmp_path):
    """A workbook's sheet is capped against a slice of AGG_MAX_CHARS. Before
    _shrink_cross a 12,000-character budget could not hold the cross at all
    and the cap dropped EVERY breakdown in the block — 1,013 characters of
    nothing but reasons."""
    regions = tuple(f"R{i:02d}" for i in range(30))
    rows = _rows(20000, seed=3, months=36, regions=regions)
    path = tmp_path / "wide.csv"
    _write(path, rows)
    first = profiler.profile_tabular(str(path))
    spec = report.cross_request(first, report.request_text("monthly revenue by region"))
    prof = profiler.profile_tabular(str(path), cross=spec, agg_max_chars=12000)
    agg = prof["aggregates"]
    assert profiler._agg_chars(agg) <= 12000
    entry = _cross(prof)
    assert entry is not None, "the breakdown that was asked for survives the cap"
    assert sum(len(r["cells"]) for r in entry["rows"]) < profiler.CROSS_MAX_CELLS
    assert agg["by_group"], "and the one-dimensional breakdowns are not all starved for it"
    assert any("were left out to keep the aggregates under 12000 characters" in r for r in _reasons(prof))


# ---------------------------------------------------------------------------
# 3. A cross can never widen what reaches the prompt
# ---------------------------------------------------------------------------


def test_a_personal_column_is_never_an_axis(tmp_path):
    """`owner` holds emails, so the profile refuses to list its values. Asked
    for revenue by owner for each month, it refuses the cross too."""
    rows = _rows(300, seed=4)
    for i, r in enumerate(rows):
        r["owner"] = f"user{i % 12}@example.invalid"
    path = tmp_path / "contacts.csv"
    _write(path, rows)
    prof, spec = _profile(path, "monthly revenue by owner")
    assert _cross(prof) is None
    blocked = [r for r in prof["aggregates"]["omitted"] if "owner" in r]
    assert any("contact details or account numbers" in r for r in blocked), blocked
    # The column's five TOP VALUES are unchanged (a deliberate, capped piece
    # of raw content); what must not happen is the cross listing twelve.
    assert "@example.invalid" not in json.dumps(prof["aggregates"])


def test_a_value_held_by_one_row_is_not_named_by_a_cross(tmp_path):
    """A group value the one-dimensional breakdown will not name (GROUP_MIN_ROWS)
    is not a row of the cross either."""
    rows = _rows(300, seed=6)
    rows[0]["region"] = "SecretRegionSeenOnce"
    path = tmp_path / "rare.csv"
    _write(path, rows)
    prof, _ = _profile(path, "monthly revenue by region")
    entry = _cross(prof)
    assert entry is not None
    assert "SecretRegionSeenOnce" not in json.dumps(entry)


def test_an_upload_profiled_with_no_request_carries_no_cross(tmp_path):
    """A cross belongs to a question, not to a file: the upload path passes
    none, and the stored profile's shape is unchanged."""
    path = tmp_path / "sales.csv"
    _write(path, _rows(200, seed=1))
    prof = profiler.profile_tabular(str(path))
    assert "by_cross" not in prof["aggregates"]
    assert list(prof["aggregates"]) == ["computed", "omitted", "measures", "by_group", "by_month"]


def test_a_cross_is_offered_only_when_the_request_names_both(tmp_path):
    path = tmp_path / "sales.csv"
    _write(path, _rows(200, seed=2))
    prof = profiler.profile_tabular(str(path))
    ask = lambda text: report.cross_request(prof, report.request_text(text))  # noqa: E731
    assert ask("What is the total revenue?") is None
    assert ask("revenue by region") is None, "one axis is not a cross"
    assert ask("revenue by month") is None
    assert ask("monthly revenue by region")["across"] == "order_date"
    assert ask("revenue by region for each month")["across"] == "order_date"
    assert ask("revenue by region and channel")["across"] == "channel"


def test_a_cross_over_a_column_the_profile_does_not_break_down_says_so(tmp_path):
    """order_id is a key, not a group. Asked for it, nothing is invented."""
    path = tmp_path / "sales.csv"
    _write(path, _rows(200, seed=8))
    prof = profiler.profile_tabular(
        str(path), cross={"group": "order_id", "across": "order_date", "measure": "revenue"}
    )
    assert _cross(prof) is None
    assert _reasons(prof) == [
        "by_cross order_id x order_date: not computed, a breakdown by two things at once needs "
        "both of them to be broken down on their own first"
    ]


def test_a_group_by_group_cross_says_truly_what_it_leaves_out(tmp_path):
    """A rare value of the SECOND axis is not a missing date; the sentence
    that explains the gap must say what actually happened."""
    rows = _rows(400, seed=12)
    rows[0]["channel"] = "pilot-seen-once"
    path = tmp_path / "sales.csv"
    _write(path, rows)
    prof, _ = _profile(path, "revenue by region and channel")
    said = _reasons(prof)
    assert len(said) == 1, said
    assert "1 row(s) are under no channel value listed here" in said[0]
    assert "have no date" not in said[0]
    assert Decimal(rows[0]["revenue"]) == Decimal(said[0].rsplit(" ", 1)[-1])
