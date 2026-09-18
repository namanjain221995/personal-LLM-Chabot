"""Dataset numbers are COMPUTED at profile time (B2, 2026-09-18).

Before this, the profile carried no sum, average or group total, so the model
added up `full_rows` itself. Measured live by the audit on 200 rows that were
entirely in the prompt: true revenue 1,022,098.02, the model's 2,707,720.92
(+165%), regions off by +151% to +206% and ranked wrong.

Every truth below is computed HERE, from the fixture's text, with Python
Decimal, never from DuckDB. Money sums must match to the cent; averages to the
sixth decimal (the profile's stated rounding); median and standard deviation
within 1e-9 relative (double precision, rounded to 12 significant digits for
run-to-run stability).
"""
from __future__ import annotations

import csv
import json
import os
import random
import statistics
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

import pytest

from app.core import profile as profiler

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "dataset_truth")
AVG_STEP = Decimal("0.000001")


def _fixture(n: int) -> str:
    return os.path.join(FIXTURES, f"sales_{n}.csv")


def _dec(value) -> Decimal:
    """A profile number as the Decimal its JSON text spells."""
    assert isinstance(value, (int, float)) and not isinstance(value, bool), value
    return Decimal(repr(value)) if isinstance(value, float) else Decimal(value)


def _avg(total: Decimal, n: int) -> Decimal:
    return (total / Decimal(n)).quantize(AVG_STEP, rounding=ROUND_HALF_EVEN)


def _close(got, want, rel=1e-9) -> bool:
    return abs(float(got) - float(want)) <= rel * max(abs(float(want)), 1e-300)


def _truth(path: str) -> dict:
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    revenue = [Decimal(r["revenue"]) for r in rows if r["revenue"] != ""]
    quantity = [int(r["quantity"]) for r in rows]
    region = defaultdict(lambda: {"count": 0, "n": 0, "revenue": Decimal(0), "quantity": 0})
    month = defaultdict(lambda: {"count": 0, "revenue": Decimal(0)})
    for r in rows:
        g = region[r["region"]]
        g["count"] += 1
        g["quantity"] += int(r["quantity"])
        m = month[r["order_date"][:7]]
        m["count"] += 1
        if r["revenue"] != "":
            g["n"] += 1
            g["revenue"] += Decimal(r["revenue"])
            m["revenue"] += Decimal(r["revenue"])
    return {"rows": rows, "revenue": revenue, "quantity": quantity, "region": dict(region), "month": dict(month)}


@pytest.fixture(scope="module", params=[200, 3000], ids=["200rows", "3000rows"])
def sales(request):
    path = _fixture(request.param)
    return profiler.profile_tabular(path), _truth(path), request.param


def _col(prof, name):
    return next(c for c in prof["columns"] if c["name"] == name)


def _group(prof, group, measure):
    return next(
        e for e in prof["aggregates"]["by_group"] if e["group"] == group and e["measure"] == measure
    )


# ---------------------------------------------------------------------------
# The contract the dataset-answers track codes against
# ---------------------------------------------------------------------------


def test_the_aggregates_block_has_the_agreed_shape(sales):
    prof, _truth_, _n = sales
    agg = prof["aggregates"]
    assert set(agg) == {"computed", "measures", "by_group", "by_month", "omitted"}
    assert agg["computed"] == "exact"
    for entry in agg["by_group"]:
        assert set(entry) == {"group", "measure", "rows", "truncated"}
        assert all(set(r) == {"value", "count", "sum", "avg"} for r in entry["rows"])
    for entry in agg["by_month"]:
        assert set(entry) == {"date", "measure", "rows", "truncated"}
        assert all(set(r) == {"month", "count", "sum"} for r in entry["rows"])
    assert isinstance(agg["omitted"], list)


def test_measures_are_the_numbers_to_add_and_the_key_is_not_one(sales):
    prof, _truth_, _n = sales
    assert prof["aggregates"]["measures"] == ["quantity", "revenue"]
    order_id = _col(prof, "order_id")
    assert not {"sum", "avg", "median", "stddev"} & set(order_id)
    assert all(e["measure"] != "order_id" for e in prof["aggregates"]["by_group"])
    # A measure is not a grouping key even when it has few distinct values
    # (quantity has 20); region and channel are.
    groups = {e["group"] for e in prof["aggregates"]["by_group"]}
    assert groups == {"region", "channel"}


# ---------------------------------------------------------------------------
# Per-measure statistics against the Decimal truth
# ---------------------------------------------------------------------------


def test_revenue_sum_avg_median_stddev_match_the_decimal_truth(sales):
    prof, truth, _n = sales
    col = _col(prof, "revenue")
    rev = truth["revenue"]
    assert _dec(col["sum"]) == sum(rev), "to the cent"
    assert _dec(col["avg"]) == _avg(sum(rev), len(rev))
    assert _close(col["median"], statistics.median(rev))
    assert _close(col["stddev"], statistics.stdev(rev)), "the SAMPLE standard deviation (n - 1)"


def test_quantity_is_summed_as_an_exact_integer(sales):
    prof, truth, _n = sales
    col = _col(prof, "quantity")
    qty = truth["quantity"]
    assert isinstance(col["sum"], int) and col["sum"] == sum(qty)
    assert _dec(col["avg"]) == _avg(Decimal(sum(qty)), len(qty))
    assert _close(col["median"], statistics.median(qty))
    assert _close(col["stddev"], statistics.stdev([Decimal(q) for q in qty]))


def test_the_blank_revenues_are_skipped_not_counted_as_zero():
    prof = profiler.profile_tabular(_fixture(3000))
    truth = _truth(_fixture(3000))
    blanks = sum(1 for r in truth["rows"] if r["revenue"] == "")
    assert blanks > 0, "the 3,000-row fixture carries blank revenues on purpose"
    col = _col(prof, "revenue")
    # A blank counted as 0 would pull the mean down by this factor.
    assert _dec(col["avg"]) == _avg(sum(truth["revenue"]), 3000 - blanks)


# ---------------------------------------------------------------------------
# Group and monthly totals
# ---------------------------------------------------------------------------


def test_revenue_by_region_matches_to_the_cent_and_is_ranked_by_the_total(sales):
    prof, truth, _n = sales
    entry = _group(prof, "region", "revenue")
    assert entry["truncated"] is False
    got = {r["value"]: r for r in entry["rows"]}
    assert set(got) == set(truth["region"])
    for name, want in truth["region"].items():
        row = got[name]
        assert row["count"] == want["count"], "rows in the group, blank revenues included"
        assert _dec(row["sum"]) == want["revenue"], name
        assert _dec(row["avg"]) == _avg(want["revenue"], want["n"]), name
    sums = [_dec(r["sum"]) for r in entry["rows"]]
    assert sums == sorted(sums, reverse=True), "ranked by the total, largest first"
    assert sum(sums) == sum(truth["revenue"]), "the groups reconcile to the file total"


def test_quantity_by_region_is_exact(sales):
    prof, truth, _n = sales
    entry = _group(prof, "region", "quantity")
    got = {r["value"]: r["sum"] for r in entry["rows"]}
    assert got == {k: v["quantity"] for k, v in truth["region"].items()}


def test_monthly_revenue_matches_to_the_cent(sales):
    prof, truth, _n = sales
    entry = next(
        e for e in prof["aggregates"]["by_month"] if e["date"] == "order_date" and e["measure"] == "revenue"
    )
    assert entry["truncated"] is False
    assert [r["month"] for r in entry["rows"]] == sorted(truth["month"])
    for row in entry["rows"]:
        want = truth["month"][row["month"]]
        assert row["count"] == want["count"]
        assert _dec(row["sum"]) == want["revenue"], row["month"]


def test_two_runs_give_byte_identical_json():
    path = _fixture(3000)
    first = json.dumps(profiler.profile_tabular(path), sort_keys=True, default=str)
    second = json.dumps(profiler.profile_tabular(path), sort_keys=True, default=str)
    assert first == second


# ---------------------------------------------------------------------------
# What is a measure, what is a key
# ---------------------------------------------------------------------------


def test_identifiers_codes_coordinates_and_years_are_not_measures(tmp_path):
    rng = random.Random(7)
    lines = ["id,customer_zip,phone,fiscal_year,period,ticket,amount,units,latitude,price"]
    for i in range(300):
        lines.append(
            f"{i + 1},{rng.choice([10001, 10002, 94107, 60601])},{5550000000 + i * 37},"
            f"{2021 + i % 4},{2019 + i % 5},{5001 + i},{i * 7 + 3},{1 + i % 9},"
            f"{40 + rng.random():.6f},{rng.randrange(100, 99999) / 100:.2f}"
        )
    path = tmp_path / "keys.csv"
    path.write_text("\n".join(lines) + "\n")
    prof = profiler.profile_tabular(str(path))
    agg = prof["aggregates"]
    # `ticket` is all-distinct integers with no key-like name: still a key.
    # `amount` is all-distinct integers too, but its NAME says money.
    assert agg["measures"] == ["amount", "units", "price"]
    for name in ("id", "customer_zip", "phone", "fiscal_year", "period", "ticket", "latitude"):
        assert "sum" not in _col(prof, name), name
    # A year is a grouping key, whether the name or the values say so.
    groups = {e["group"] for e in agg["by_group"]}
    assert {"fiscal_year", "period"} <= groups


def test_a_blank_group_value_is_its_own_row_so_groups_reconcile(tmp_path):
    lines = ["region,amount"]
    for i in range(60):
        region = ["north", "south", ""][i % 3]
        lines.append(f"{region},{i}.25")
    path = tmp_path / "blank_group.csv"
    path.write_text("\n".join(lines) + "\n")
    prof = profiler.profile_tabular(str(path))
    entry = _group(prof, "region", "amount")
    assert [r["value"] for r in entry["rows"]][-1] is None, "the blank group sorts last"
    total = sum(Decimal(f"{i}.25") for i in range(60))
    assert sum(_dec(r["sum"]) for r in entry["rows"]) == total
    assert _dec(_col(prof, "amount")["sum"]) == total


def test_group_values_are_clipped_exactly_like_top_values(tmp_path):
    secret = "CANARY-GROUP-TAIL-51d0"
    long_value = "west-" + "x" * 400 + secret
    lines = ["region,amount"] + [f"{['north', long_value][i % 2]},{i}.10" for i in range(100)]
    path = tmp_path / "long.csv"
    path.write_text("\n".join(lines) + "\n")
    prof = profiler.profile_tabular(str(path))
    agg_json = json.dumps(prof["aggregates"])
    assert secret not in agg_json
    values = {r["value"] for r in _group(prof, "region", "amount")["rows"]}
    tops = {t["value"] for t in _col(prof, "region")["top_values"]}
    assert values == tops, "the same clipped text top values carry"


# ---------------------------------------------------------------------------
# Float money, continuous floats, non-finite values
# ---------------------------------------------------------------------------


def test_float_noise_on_money_does_not_break_the_cent(tmp_path):
    """A spreadsheet formula's cached value is 12.339999999999998, not 12.34."""
    path = tmp_path / "noise.csv"
    path.write_text("amount\n" + "\n".join(["12.339999999999998", "0.30000000000000004", "7.1"] * 400) + "\n")
    prof = profiler.profile_tabular(str(path))
    col = _col(prof, "amount")
    assert _dec(col["sum"]) == (Decimal("12.34") + Decimal("0.30") + Decimal("7.10")) * 400
    assert prof["aggregates"]["omitted"] == []


def test_continuous_floats_are_summed_in_double_precision_and_say_so(tmp_path):
    rng = random.Random(3)
    values = [Decimal(f"{rng.random() * 1000:.9f}") for _ in range(500)]
    path = tmp_path / "sensor.csv"
    path.write_text("reading\n" + "\n".join(str(v) for v in values) + "\n")
    prof = profiler.profile_tabular(str(path))
    col = _col(prof, "reading")
    assert _close(col["sum"], sum(values), rel=1e-11)
    assert _close(col["avg"], sum(values) / len(values), rel=1e-11)
    assert any("reading" in why and "12 significant digits" in why for why in prof["aggregates"]["omitted"])


def test_infinite_values_are_not_summed(tmp_path):
    path = tmp_path / "inf.csv"
    path.write_text("x\n" + "\n".join(["1.5", "inf", "2.5"] * 10) + "\n")
    prof = profiler.profile_tabular(str(path))
    col = _col(prof, "x")
    assert col.get("sum") is None and col.get("avg") is None
    assert any("x" in why and "infinite" in why for why in prof["aggregates"]["omitted"])
    json.loads(json.dumps(prof["aggregates"], allow_nan=False))


# ---------------------------------------------------------------------------
# Caps: a 60-column file
# ---------------------------------------------------------------------------


def _wide_file(tmp_path) -> str:
    rng = random.Random(60)
    months = []
    d = date(2019, 1, 1)
    while d < date(2025, 1, 1):
        months.append(d)
        d = (d + timedelta(days=32)).replace(day=1)
    assert len(months) == 72
    header = (
        [f"m{i:02d}" for i in range(1, 13)]
        + [f"g{i:02d}" for i in range(1, 11)]
        + [f"d{i}" for i in range(1, 5)]
        + [f"s{i:02d}" for i in range(1, 35)]
    )
    assert len(header) == 60
    rows = [",".join(header)]
    for r in range(2000):
        cells = [f"{rng.randrange(0, 10_000_000) / 100:.2f}" for _ in range(12)]
        cells += [f"group-{g}-{'y' * 120}-{rng.randrange(40)}" for g in range(10)]
        cells += [(months[(r + k) % 72] + timedelta(days=rng.randrange(28))).isoformat() for k in range(4)]
        cells += [f"text {r} {k}" for k in range(34)]
        rows.append(",".join(cells))
    path = tmp_path / "wide.csv"
    path.write_text("\n".join(rows) + "\n")
    return str(path)


def _rendered(agg) -> int:
    """The block's length as dataset.format_profile prints it for a sheet of a
    workbook (json indent=1, four levels deep), measured by rendering it."""
    wrapped = [{"sheets": [{"aggregates": agg}]}]
    shell = [{"sheets": [{"aggregates": 0}]}]
    return len(json.dumps(wrapped, ensure_ascii=False, indent=1, default=str)) - len(
        json.dumps(shell, indent=1)
    ) + 1


def _width(entry) -> int:
    """How wide a breakdown is: its row count (groups or months)."""
    return len(entry["rows"])


def test_a_60_column_file_respects_every_cap(tmp_path, monkeypatch):
    path = _wide_file(tmp_path)
    monkeypatch.setattr(profiler, "AGG_MAX_CHARS", 10**9)
    full = profiler.profile_tabular(path)["aggregates"]
    monkeypatch.undo()
    capped_prof = profiler.profile_tabular(path)
    capped = capped_prof["aggregates"]

    # Count caps, applied before the size cap.
    assert full["measures"] == [f"m{i:02d}" for i in range(1, profiler.AGG_MAX_MEASURES + 1)]
    assert len({e["group"] for e in full["by_group"]}) == profiler.AGG_MAX_GROUP_COLUMNS
    assert len({e["date"] for e in full["by_month"]}) == profiler.AGG_MAX_DATE_COLUMNS
    assert len(full["by_group"]) == profiler.AGG_MAX_GROUP_COLUMNS * profiler.AGG_MAX_MEASURES
    omitted = " | ".join(full["omitted"])
    assert "m09" in omitted and "g07" in omitted and "d4" in omitted
    # Every measure column still carries its own sum, capped or not.
    assert "sum" in _col(capped_prof, "m12")

    # The month window keeps the LATEST 60 of 72 months, oldest first.
    for entry in full["by_month"]:
        months = [r["month"] for r in entry["rows"]]
        assert len(months) == profiler.AGG_MAX_MONTHS and entry["truncated"] is True
        assert months == sorted(months) and months[-1] == "2024-12" and months[0] == "2020-01"

    # The size cap holds, and it dropped the WIDEST breakdowns first.
    # Measured as the prompt renders it (indent=1, at a sheet's depth), not
    # compact: QA measured the prompt block at 1.42x the compact size.
    assert _rendered(capped) <= profiler.AGG_MAX_CHARS
    kept = capped["by_group"] + capped["by_month"]
    everything = full["by_group"] + full["by_month"]
    dropped = [e for e in everything if e not in kept]
    assert kept and dropped and all(e in everything for e in kept)
    assert min(map(_width, dropped)) >= max(map(_width, kept))
    # Among equally wide breakdowns the LATER one in the block goes first.
    for d in dropped:
        for k in kept:
            if _width(d) == _width(k):
                assert everything.index(d) > everything.index(k)
    for e in dropped:
        key = e.get("group") or e.get("date")
        assert any(key in why and e["measure"] in why for why in capped["omitted"]), (key, e["measure"])


def test_the_size_cap_does_not_drop_a_breakdown_for_having_bigger_numbers(tmp_path, monkeypatch):
    """Measured on a 1M-row sales file while building this: with "widest"
    meaning most CHARACTERS, the cap dropped revenue-by-month and
    cost-by-month and kept discount-by-month, because a money total has more
    digits than a sum of rates. Width is the row count; ties go by position."""
    rng = random.Random(11)
    lines = ["order_date,quantity,revenue,discount"]
    for i in range(3000):
        day = date(2020, 1, 1) + timedelta(days=(i * 37) % 1826)
        lines.append(f"{day.isoformat()},{rng.randint(1, 20)},{rng.randrange(10**6, 10**9) / 100:.2f},0.{rng.randrange(31):02d}")
    path = tmp_path / "sales.csv"
    path.write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(profiler, "AGG_MAX_CHARS", 10**9)
    full = profiler.profile_tabular(str(path))["aggregates"]
    widths = {e["measure"]: len(json.dumps(e)) for e in full["by_month"]}
    assert widths["revenue"] > widths["discount"], "the revenue totals ARE the widest text"
    # One character under the block's RENDERED size (the cap measures what
    # the prompt prints since QA round 1; this set compact size - 1 before).
    monkeypatch.setattr(profiler, "AGG_MAX_CHARS", _rendered(full) - 1)
    capped = profiler.profile_tabular(str(path))["aggregates"]
    kept = [e["measure"] for e in capped["by_month"]]
    assert kept == ["quantity", "revenue"], "the last of three equally wide breakdowns went"
    assert any("by_month order_date x discount" in why for why in capped["omitted"])


# ---------------------------------------------------------------------------
# QA round 1 fixes (2026-09-18): the sniff window, dropped rows, raw group
# values, names in reasons, JSON exactness, median rounding
# ---------------------------------------------------------------------------


def _late_cents(tmp_path, *, header=True, n=25_500, switch=25_000):
    """Whole-dollar revenue for `switch` rows, then cents: the sniffer (20,000
    rows) types the column BIGINT, and DuckDB rounds '2051.01' into it."""
    rng = random.Random(1)
    lines = ["order_id,region,revenue"] if header else []
    truth = {"all": Decimal(0)}
    for i in range(n):
        r = Decimal(rng.randrange(1, 10**4)) if i < switch else Decimal(rng.randrange(100, 10**6)) / 100
        region = "NSEW"[i % 4]
        truth["all"] += r
        truth[region] = truth.get(region, Decimal(0)) + r
        lines.append(f"{i + 1},{region},{r}")
    path = tmp_path / ("late.csv" if header else "late_noheader.csv")
    path.write_text("\n".join(lines) + "\n")
    return str(path), truth


def test_cents_past_the_sniff_window_are_summed_to_the_cent(tmp_path):
    """Measured at 9045dbf: sum 127,559,755 against a true 127,559,748.92 and
    nothing in omitted. The column is re-read as DOUBLE, what the sniffer
    picks when it sees a decimal, so the total is exact, not just flagged."""
    path, truth = _late_cents(tmp_path)
    prof = profiler.profile_tabular(path)
    col = _col(prof, "revenue")
    assert col["dtype"] == "DOUBLE" and prof["rows"] == 25_500
    assert _dec(col["sum"]) == truth["all"]
    entry = _group(prof, "region", "revenue")
    assert {r["value"]: _dec(r["sum"]) for r in entry["rows"]} == {k: v for k, v in truth.items() if k != "all"}
    assert prof["aggregates"]["omitted"] == []


def test_a_header_less_file_is_re_checked_too(tmp_path):
    """Read as text, a header-less file can be taken to HAVE a header: the
    check then asks the sniffer, instead of silently skipping."""
    path, truth = _late_cents(tmp_path, header=False, n=12_500, switch=12_000)
    prof = profiler.profile_tabular(path)
    third = prof["columns"][2]
    assert third["dtype"] == "DOUBLE"
    assert _dec(third["sum"]) == truth["all"]


class _Spy:
    def __init__(self, con, seen):
        self._con, self._seen = con, seen

    def execute(self, sql, *a, **k):
        self._seen.append(sql)
        return self._con.execute(sql, *a, **k)

    def close(self):
        self._con.close()


def _statements(monkeypatch, path):
    seen = []
    real = profiler._duck
    monkeypatch.setattr(profiler, "_duck", lambda: _Spy(real(), seen))
    prof = profiler.profile_tabular(str(path))
    monkeypatch.setattr(profiler, "_duck", real)
    return prof, seen


@pytest.mark.parametrize("rows, rereads", [(9_000, 0), (12_000, 1)])
def test_only_a_file_longer_than_the_sniff_window_is_read_a_second_time(tmp_path, monkeypatch, rows, rereads):
    path = tmp_path / "n.csv"
    path.write_text("qty,amount\n" + "".join(f"{i % 7},{i}.25\n" for i in range(rows)))
    prof, seen = _statements(monkeypatch, path)
    assert "error" not in prof and prof["rows"] == rows
    assert sum("all_varchar" in s for s in seen) == rereads
    assert sum("read_csv_auto" in s for s in seen) == 2, "DESCRIBE and the one COPY, as before"


def test_rows_the_reader_dropped_are_said_first_in_omitted(tmp_path):
    """QA: one bad date at data row 22,001 (past the sniff window) dropped a
    250,000.00 row from every total with computed='exact' and omitted=[]."""
    rng = random.Random(9)
    lines = ["order_date,region,revenue"]
    truth = Decimal(0)
    for i in range(25_000):
        v = Decimal("250000.00") if i == 22_000 else Decimal(rng.randrange(100, 10**5)) / 100
        day = "2024-13-45" if i == 22_000 else f"2024-{1 + i % 12:02d}-{1 + i % 28:02d}"
        if i != 22_000:
            truth += v
        lines.append(f"{day},{'NSEW'[i % 4]},{v}")
    path = tmp_path / "bad_date.csv"
    path.write_text("\n".join(lines) + "\n")
    prof = profiler.profile_tabular(str(path))
    omitted = prof["aggregates"]["omitted"]
    assert prof["rows"] == 24_999
    assert omitted and omitted[0].startswith("1 row(s) could not be read") and "order_date" in omitted[0]
    assert _dec(_col(prof, "revenue")["sum"]) == truth, "the total is of the rows read, and says so"


def test_a_value_held_by_one_row_is_left_out_of_its_breakdown_and_said(tmp_path):
    """The canary shape QA used: 20 repeating statuses and one cell seen once."""
    lines = ["status,amount"]
    total = Decimal(0)
    rare_amount = None
    for i in range(1000):
        status = "RARE-ONE-7f3a" if i == 700 else f"status-{i % 20:02d}"
        amount = Decimal(i) + Decimal("0.35")
        total += amount
        if i == 700:
            rare_amount = amount
        lines.append(f"{status},{amount}")
    path = tmp_path / "rare.csv"
    path.write_text("\n".join(lines) + "\n")
    prof = profiler.profile_tabular(str(path))
    entry = _group(prof, "status", "amount")
    values = [r["value"] for r in entry["rows"]]
    assert "RARE-ONE-7f3a" not in json.dumps(prof["aggregates"])
    assert len(values) == 20 and entry["truncated"] is True
    assert any(why.startswith("by_group status: 1 value(s) found in only one row") for why in prof["aggregates"]["omitted"])
    listed = sum(_dec(r["sum"]) for r in entry["rows"])
    assert listed + rare_amount == total == _dec(_col(prof, "amount")["sum"])


def test_identifier_and_contact_columns_are_never_group_keys(tmp_path):
    lines = ["store_id,customer_email,region,amount"]
    for i in range(400):
        lines.append(f"{100 + i % 10},person{i % 10}@example.invalid,{'NSEW'[i % 4]},{i}.50")
    path = tmp_path / "keys.csv"
    path.write_text("\n".join(lines) + "\n")
    prof = profiler.profile_tabular(str(path))
    assert {e["group"] for e in prof["aggregates"]["by_group"]} == {"region"}
    # Their top values are unchanged: that class of raw content was always there.
    assert len(_col(prof, "customer_email")["top_values"]) == 5


def test_column_names_in_reasons_are_clipped(tmp_path):
    names = [f"amount_{i}_" + "y" * 600 for i in range(12)]
    lines = ["region," + ",".join(names)]
    for r in range(100):
        lines.append("NS"[r % 2] + "," + ",".join(f"{r}.{k}5" for k in range(12)))
    path = tmp_path / "long.csv"
    path.write_text("\n".join(lines) + "\n")
    agg = profiler.profile_tabular(str(path))["aggregates"]
    beyond = next(w for w in agg["omitted"] if w.startswith("measures beyond the first"))
    assert beyond.count("…[truncated]") == 4 and len(beyond) < 4 * 260
    assert _rendered(agg) <= profiler.AGG_MAX_CHARS


def test_a_total_a_json_number_cannot_spell_is_flagged(tmp_path):
    """A DECIMAL sum is carried as a float: past about 15 significant digits
    its cents are not exact, and QA found nothing said so."""
    path = tmp_path / "big.csv"
    path.write_text("amount\n" + "".join("123456789012345.67\n" for _ in range(10)))
    agg = profiler.profile_tabular(str(path))["aggregates"]
    assert any(w.startswith("amount: a total has more significant digits") for w in agg["omitted"])


def test_median_is_rounded_to_twelve_significant_digits(tmp_path):
    """(0.1 + 0.2) / 2 is 0.15000000000000002 in double precision; the profile
    must say 0.15 on every run (QA mutation M15 survived without this)."""
    path = tmp_path / "m.csv"
    path.write_text("amount\n" + "0.1\n0.2\n" * 10)
    col = _col(profiler.profile_tabular(str(path)), "amount")
    assert repr(col["median"]) == "0.15"
