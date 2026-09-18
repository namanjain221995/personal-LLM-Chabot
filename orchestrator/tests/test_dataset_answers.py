"""The dataset route quotes computed numbers; it never does arithmetic.

THE FAILURES THIS PINS (audit 2026-09-18, backlog 2, 16, 18 and 19).

  * A group-by over a 200-row file the model could fully see came back
    +165% (true total 1,022,098.02, answered 2,707,720.92), with a closing
    note claiming the figure was "calculated by summing the revenue column
    for all 200 rows". The prompt had TOLD it to: "you can compute sums,
    group-bys, correlations and any other aggregate directly from full_rows,
    exactly". A model adding 200 numbers in its head is a guess dressed as a
    measurement.
  * Refusals named internal fields ("the uploaded file profile does not
    include `full_rows`") and sent the person to Excel, Python or SQL.
  * "Generate a PDF report of monthly revenue by region" produced a PDF of
    column types with no revenue figure in it.
  * A long answer stopped at 6,000 tokens mid-row (order 104 of 200) with
    nothing in the stream saying so.

UNTIL THE PROFILE TRACK LANDS (dataset-numbers-computed adds the aggregates
to `profile_tabular`), the profiles below get their `aggregates` block and
per-column sum/avg/median/stddev from DuckDB INSIDE THIS TEST, in the shape
that track's contract names. A profile that already carries `aggregates` is
used as it is, so the same tests read the real block once it exists.
"""
from __future__ import annotations

import asyncio
import csv
import json
import random
import re
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Dict, List

from app import db, llm
from app.config import settings
from app.core import profile as profiler
from app.engines import dataset
from app.engines.dataset_report import build_report_markdown

CENT = Decimal("0.01")
REGIONS = ("East", "North", "South", "West")
CATEGORIES = ("Hardware", "Services", "Software")


# ---------------------------------------------------------------------------
# A seeded order file whose truth is computed with Decimal, never with floats
# ---------------------------------------------------------------------------


def make_orders(n: int, seed: int) -> List[Dict[str, object]]:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        month, day = rng.randint(1, 12), rng.randint(1, 28)
        rows.append({
            "order_date": f"2025-{month:02d}-{day:02d}",
            "region": rng.choice(REGIONS),
            "category": rng.choice(CATEGORIES),
            "units": rng.randint(1, 40),
            "revenue": Decimal(rng.randint(5_000, 1_000_000)) / 100,
        })
    rows.sort(key=lambda r: r["order_date"])
    for i, row in enumerate(rows, 1):
        row["order_id"] = f"ORD-{i:05d}"
    return rows


def write_orders(path: Path, rows) -> Path:
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["order_id", "order_date", "region", "category", "units", "revenue"])
        for r in rows:
            w.writerow([r["order_id"], r["order_date"], r["region"], r["category"], r["units"], f"{r['revenue']:.2f}"])
    return path


def truth(rows) -> Dict[str, object]:
    """Every figure a correct answer may state, by Decimal."""
    out: Dict[str, object] = {"rows": len(rows)}
    for m in ("revenue", "units"):
        vals = [Decimal(str(r[m])) for r in rows]
        out[f"{m}_total"] = sum(vals)
        out[f"{m}_avg"] = (sum(vals) / len(vals)).quantize(CENT, ROUND_HALF_UP)
    for key, label in (("region", "by_region"), ("category", "by_category")):
        groups: Dict[str, List[Decimal]] = {}
        for r in rows:
            groups.setdefault(r[key], []).append(Decimal(str(r["revenue"])))
        out[label] = {g: (len(v), sum(v), (sum(v) / len(v)).quantize(CENT, ROUND_HALF_UP)) for g, v in groups.items()}
    months: Dict[str, List[Decimal]] = {}
    for r in rows:
        months.setdefault(r["order_date"][:7], []).append(Decimal(str(r["revenue"])))
    out["by_month"] = {m: (len(v), sum(v)) for m, v in sorted(months.items())}
    return out


# ---------------------------------------------------------------------------
# The aggregates contract, computed by DuckDB here until profile.py ships it
# ---------------------------------------------------------------------------

_NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "DOUBLE", "FLOAT", "REAL", "DECIMAL")


def add_aggregates(prof: dict, path: Path) -> dict:
    """Per-column sum/avg/median/stddev and `aggregates`, the contract's shape.

    Sums are left as DuckDB returns them for a DOUBLE column — float noise
    and all — so the report's formatting is tested against what a real
    profile will hold, not against pre-rounded numbers.
    """
    if "aggregates" in prof:
        return prof
    import duckdb

    con = duckdb.connect(":memory:")
    src = f"read_csv_auto('{path}')"
    cols = {c["name"]: c for c in prof["columns"]}
    q = lambda name: '"' + name.replace('"', '""') + '"'  # noqa: E731
    measures = [n for n, c in cols.items() if str(c["dtype"]).upper().startswith(_NUMERIC)]
    for m in measures:
        s, a, med, sd = con.execute(
            f"SELECT SUM({q(m)}), AVG({q(m)}), MEDIAN({q(m)}), STDDEV_SAMP({q(m)}) FROM {src}"
        ).fetchone()
        cols[m].update(sum=s, avg=a, median=med, stddev=sd)
    groups = [n for n, c in cols.items() if str(c["dtype"]).upper() == "VARCHAR" and 0 < (c.get("distinct") or 0) <= 50]
    dates = [n for n, c in cols.items() if str(c["dtype"]).upper() in ("DATE", "TIMESTAMP")]
    by_group, by_month = [], []
    for g in groups:
        for m in measures:
            rows = con.execute(
                f"SELECT {q(g)}, COUNT(*), SUM({q(m)}), AVG({q(m)}) FROM {src} GROUP BY 1 ORDER BY 3 DESC, 1"
            ).fetchall()
            by_group.append({"group": g, "measure": m, "truncated": False,
                             "rows": [{"value": v, "count": n, "sum": s, "avg": a} for v, n, s, a in rows]})
    for d in dates:
        for m in measures:
            rows = con.execute(
                f"SELECT strftime(date_trunc('month', {q(d)}), '%Y-%m'), COUNT(*), SUM({q(m)}) FROM {src} GROUP BY 1 ORDER BY 1"
            ).fetchall()
            by_month.append({"date": d, "measure": m, "truncated": False,
                             "rows": [{"month": mo, "count": n, "sum": s} for mo, n, s in rows]})
    con.close()
    prof["aggregates"] = {"computed": True, "measures": measures, "by_group": by_group,
                          "by_month": by_month, "omitted": []}
    return prof


def profiled_upload(tmp_path: Path, n: int, seed: int):
    rows = make_orders(n, seed)
    path = write_orders(tmp_path / f"orders_{n}.csv", rows)
    prof = add_aggregates(profiler.profile_tabular(str(path)), path)
    # What the uploads row holds: the profile went through JSON on its way in.
    prof = json.loads(json.dumps(prof, default=str))
    upload = {"filename": path.name, "bytes": path.stat().st_size, "status": "ready",
              "profile": [prof], "notes": None}
    return rows, upload


# ---------------------------------------------------------------------------
# 1. The prompt: no permission to compute, arithmetic forbidden
# ---------------------------------------------------------------------------


def test_the_prompt_no_longer_permits_the_model_to_compute():
    system = dataset._SYSTEM
    # The sentence that produced +165%: it GRANTED the model the arithmetic.
    assert "FULL CONTENT" not in system
    assert "compute sums" not in system
    assert "any other aggregate directly" not in system
    assert not re.search(r"\byou can compute\b", system, re.I)


def test_the_prompt_forbids_arithmetic_and_says_what_a_number_may_be():
    system = dataset._SYSTEM
    assert "NUMBERS: you never perform arithmetic." in system
    # Seeing every row is not a licence: that is exactly where +165% came from.
    assert "not even over rows you can see" in system
    # What a stated number may be, and the obligation to say which.
    assert "a single cell copied from a row" in system
    assert "a count, min, max, sum, avg or median copied from the computed figures" in system
    assert "you say which it is in plain words" in system
    # Live 2026-09-18: "say which" alone was read as "say where", and the
    # provenance came out as "(from the `by_group` aggregates ...)".
    assert "never where it sits in the data" in system
    # Comparing figures that are there is not arithmetic: "which region sold
    # most" must not be refused (the over-refusal risk of this rule).
    assert "Ranking or comparing figures that are there is fine" in system


def test_the_prompt_forbids_internal_field_names_and_outside_tools():
    system = dataset._SYSTEM
    # Live 2026-09-18: with only the first three named, 9 of 18 answers still
    # wrote "the aggregates section" or "`by_month` aggregates".
    for word in ("'profile'", "'full_rows'", "'full_content'", "'aggregates'", "'by_group'", "'by_month'"):
        assert word in system, word
    assert "Never suggest Excel, pandas, Python, SQL or another tool" in system
    # The capability line (engines/capability.py) names Excel and offers "make
    # this a Word document"; the last word, read after it, forbids that offer
    # for a figure that is not computed.
    last = dataset.build_messages("q", [], [])[0]["content"].split("LAST WORD ON NUMBERS:")[1]
    assert "do not offer an Excel, Word, CSV or PDF file" in last
    assert "or downloading the file to work it out" in system
    # The old refusal paragraph is gone with its homework.
    assert "HONESTY:" not in system
    assert "CANNOT compute new aggregates" not in system


def test_the_offer_the_prompt_teaches_is_one_the_platform_really_makes():
    """The refusal offers a chart, and the example it quotes is read by the
    artifact gate as a request for a file — the path a dataset conversation
    reaches since 187191b. A Word or Excel file "with the calculation" is not
    offered: its figures would be typed by a model, which is the defect."""
    from app.artifacts import intent as I

    system = dataset._SYSTEM
    rule = system.split("NUMBERS:")[1].split("CHARTS:")[0]
    offers = re.findall(r'the exact request they can send, for example "([^"]+)"', rule)
    offers += re.findall(r'or for two things at once "([^"]+)"', rule)
    assert len(offers) == 2, "the rule quotes the requests to make"
    for offer in offers:
        verdict = I.decide(offer, has_assistant_answer=True)
        assert verdict.wants_file, offer
        assert "chart" in offer
    assert "Word document" not in rule and "Excel file" not in rule
    # Live 2026-09-18: told only to "offer a chart", the model also offered "a
    # table of revenue by region and month" — a file whose figures a model
    # would type. The rule now names the one offer.
    assert "Offer only a chart — not a table, a document or a spreadsheet." in rule
    assert "end by offering one chart" in rule


def test_the_rule_reaches_the_prompt_the_engine_sends_with_the_data_still_fenced(tmp_path):
    _, upload = profiled_upload(tmp_path, 40, 7)
    messages = dataset.build_messages("total revenue?", [upload], [])
    system, user = messages[0]["content"], messages[-1]["content"]
    assert "NUMBERS: you never perform arithmetic." in system
    assert system.index("SECURITY:") < system.index("NUMBERS:") < system.index("CHARTS:")
    assert dataset.DATA_START in user and dataset.DATA_END in user
    # The computed figures are INSIDE the fence: they are data too.
    start, end = user.index(dataset.DATA_START), user.index(dataset.DATA_END)
    assert start < user.index('"aggregates"') < end


def test_computed_figures_reach_the_model_without_float_noise_and_cells_stay_as_they_are():
    """Told to copy figures exactly, the model copied a DOUBLE sum stored as
    912646.9999999999 digit for digit (live, 2026-09-18)."""
    prof = {"file": "x.csv", "rows": 2,
            "columns": [{"name": "revenue", "dtype": "DOUBLE", "sum": 912646.9999999999, "avg": 4563.234999999999}],
            "aggregates": {"computed": True, "measures": ["revenue"],
                           "by_group": [{"group": "region", "measure": "revenue", "truncated": False,
                                         "rows": [{"value": "East", "count": 1, "sum": 1022098.0200000001, "avg": 1.5},
                                                  {"value": "West", "count": 768, "sum": 3886287.29999999,
                                                   "avg": 5060.26992187499}]}],
                           "by_month": [], "omitted": []},
            "sample_rows": [{"revenue": 0.12345678901234567}]}
    block = dataset.format_profile([{"filename": "x.csv", "bytes": 1, "status": "ready", "profile": [prof]}])
    assert "912646.9999999999" not in block and "912647.0" in block
    assert "1022098.0200000001" not in block and "1022098.02" in block
    assert "4563.235" in block
    # The 3,000-row sum the model quoted as 3,886,287.29999999.
    assert "3886287.29999999" not in block and "3886287.3" in block
    assert "5060.269922" in block
    # A cell is data: it reaches the model exactly as the file had it.
    assert repr(prof["sample_rows"][0]["revenue"]) in block


# ---------------------------------------------------------------------------
# 2. The PDF's computed section equals truth
# ---------------------------------------------------------------------------


def _table_after(md: str, heading_part: str) -> List[List[str]]:
    """The data rows of the first Markdown table under a heading naming `heading_part`."""
    lines = md.splitlines()
    at = next(i for i, ln in enumerate(lines) if ln.startswith("###") and heading_part in ln)
    rows: List[List[str]] = []
    for ln in lines[at + 1:]:
        if ln.startswith("#") or (rows and not ln.startswith("|")):
            break  # the next heading, or the blank line that ends the table
        if ln.startswith("|") and not ln.startswith("| ---"):
            rows.append([c.strip() for c in ln.strip("|").split("|")])
    return rows[1:]  # drop the header row


def _num(cell: str) -> Decimal:
    return Decimal(cell.replace(",", ""))


def _cents(value) -> Decimal:
    return Decimal(value).quantize(CENT, ROUND_HALF_UP)


def test_the_report_states_the_asked_monthly_and_regional_figures_equal_to_truth(tmp_path):
    rows, upload = profiled_upload(tmp_path, 240, 20260918)
    t = truth(rows)
    md = build_report_markdown("Data Report", [upload], "", "now",
                               message="Generate a PDF report of monthly revenue by region")

    totals = {r[0]: r for r in _table_after(md, "computed from every row")}
    assert _num(totals["revenue"][1]) == _cents(t["revenue_total"])
    assert _num(totals["revenue"][2]) == t["revenue_avg"]
    assert _num(totals["units"][1]) == t["units_total"]

    by_region = {r[0]: r for r in _table_after(md, "revenue by region")}
    assert set(by_region) == set(REGIONS)
    for region, (count, total, avg) in t["by_region"].items():
        assert int(by_region[region][1]) == count, region
        assert _num(by_region[region][2]) == _cents(total), region
        assert _num(by_region[region][3]) == avg, region

    by_month = {r[0]: r for r in _table_after(md, "revenue by month")}
    assert list(by_month) == list(t["by_month"])
    for month, (count, total) in t["by_month"].items():
        assert int(by_month[month][1]) == count, month
        assert _num(by_month[month][2]) == _cents(total), month


def test_with_nothing_named_the_breakdown_is_the_fewest_valued_group_by_the_largest_measure(tmp_path):
    rows, upload = profiled_upload(tmp_path, 240, 20260918)
    t = truth(rows)
    md = build_report_markdown("Data Report", [upload], "", "now", message="Generate a PDF report")
    # category has 3 values, region 4; revenue's total dwarfs units'.
    by_category = {r[0]: r for r in _table_after(md, "revenue by category")}
    assert {k: _num(v[2]) for k, v in by_category.items()} == {k: _cents(v[1]) for k, v in t["by_category"].items()}
    # Monthly figures only when they are asked for.
    assert "by month" not in md


def test_a_named_measure_and_group_are_the_ones_broken_down(tmp_path):
    rows, upload = profiled_upload(tmp_path, 120, 3)
    md = build_report_markdown("Data Report", [upload], "", "now", message="create a pdf of units by region")
    by_region = {r[0]: r for r in _table_after(md, "units by region")}
    want: Dict[str, int] = {}
    for r in rows:
        want[r["region"]] = want.get(r["region"], 0) + int(r["units"])
    assert {k: int(_num(v[2])) for k, v in by_region.items()} == want


def test_figures_print_to_the_cent_without_float_noise():
    """A DOUBLE SUM is stored as 912646.9999999999 and an AVG as
    4563.234999999999; the report prints the figures they are."""
    prof = {"file": "x.csv", "rows": 200, "columns_total": 2,
            "columns": [{"name": "revenue", "dtype": "DOUBLE", "null_pct": 0.0, "distinct": 200,
                         "sum": 912646.9999999999, "avg": 4563.234999999999, "median": 4616.64},
                        {"name": "units", "dtype": "BIGINT", "null_pct": 0.0, "distinct": 40,
                         "sum": 4006, "avg": 20.03, "median": 20.0}],
            "aggregates": {"computed": True, "measures": ["units", "revenue"], "by_group": [],
                           "by_month": [], "omitted": []}}
    md = build_report_markdown("T", [{"filename": "x.csv", "profile": [prof]}], "", "now", message="pdf please")
    totals = {r[0]: r for r in _table_after(md, "computed from every row")}
    assert totals["revenue"][1:] == ["912,647.00", "4,563.24", "4,616.64"]
    assert totals["units"][1:] == ["4,006", "20.03", "20"]


def test_a_one_page_request_keeps_the_figures_and_folds_the_column_table(tmp_path):
    """H-03's own wording measured two pages once the figures were in; the
    column table becomes one line so the report stays on one page."""
    rows, upload = profiled_upload(tmp_path, 200, 20260918)
    t = truth(rows)
    md = build_report_markdown("Data Report", [upload], "", "now",
                               message="generate a very short pdf file in one page about this csv file")
    assert "| Column | Type |" not in md
    assert "## orders_200.csv" not in md and "**orders_200.csv** — **200** rows" in md
    assert "**Columns:** order_id (VARCHAR), order_date (DATE), region (VARCHAR)" in md
    assert "No missing values." in md
    totals = {r[0]: r for r in _table_after(md, "computed from every row")}
    assert _num(totals["revenue"][1]) == _cents(t["revenue_total"])
    # Without computed figures the one-page report is exactly what it was.
    old = {k: v for k, v in upload["profile"][0].items() if k != "aggregates"}
    md_old = build_report_markdown("Data Report", [{"filename": "x.csv", "profile": [old]}], "", "now",
                                   message="generate a very short pdf file in one page about this csv file")
    assert "| Column | Type | Nulls | Distinct | Range |" in md_old
    assert "## orders_200.csv" in md_old


def test_a_one_page_request_asks_for_a_shorter_summary(tmp_path, monkeypatch):
    """A live five-sentence summary spilled three lines onto page two; the
    one-page request now asks for at most three sentences."""
    from app.engines import dataset_report

    _, upload = profiled_upload(tmp_path, 40, 5)
    seen = []

    async def completion(messages, **kw):
        seen.append(messages[-1]["content"])
        return "Prose."

    monkeypatch.setattr(llm, "chat_completion", completion)
    asyncio.run(dataset_report._narrative("a very short pdf in one page", [upload], "smart"))
    asyncio.run(dataset_report._narrative("a pdf of revenue by region", [upload], "smart"))
    assert seen[0].endswith("at most three sentences: the report must fit on one page.")
    assert seen[1].endswith("Write the summary section.")


def test_the_answer_no_longer_promises_one_page(tmp_path, monkeypatch):
    from app.engines import dataset_report

    _, upload = profiled_upload(tmp_path, 40, 5)
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub")

    async def render(markdown, reports_dir, **kw):
        return pdf

    async def narrative(*a, **kw):
        return "Prose."

    monkeypatch.setattr(dataset_report, "render_markdown_pdf", render)
    monkeypatch.setattr(llm, "chat_completion", narrative)
    events = []

    async def emit(kind, data):
        events.append((kind, data))

    answer = asyncio.run(dataset_report.run_dataset_report("create a PDF of revenue by region", [upload], emit))
    # The figures it asked for made the report two pages (measured): the
    # sentence says what was made, not how long it is.
    assert "one-page" not in answer
    assert answer.startswith("I generated a PDF report from your uploaded data")


def test_a_profile_without_aggregates_still_renders_and_invents_nothing():
    """An upload profiled before the aggregates existed keeps its old report."""
    prof = {"file": "old.csv", "rows": 3, "columns_total": 1,
            "columns": [{"name": "amount", "dtype": "DOUBLE", "null_pct": 0.0, "distinct": 3, "min": 1, "max": 3}]}
    md = build_report_markdown("T", [{"filename": "old.csv", "profile": [prof]}], "", "now",
                               message="pdf of monthly amount")
    assert "computed from every row" not in md
    assert "| amount | DOUBLE |" in md


# ---------------------------------------------------------------------------
# 3. Long answers continue, and a stopped one says so
# ---------------------------------------------------------------------------


class _EndlessModel:
    """Every call runs out of room ("length") and writes something new."""

    def __init__(self):
        self.calls = 0
        self.reason = None
        self.completion = 0

    async def stream(self, messages, **kwargs):
        self.calls += 1
        text = "".join(f"| ORD-{self.calls:03d}-{i:03d} | {i}.00 |\n" for i in range(60))
        for i in range(0, len(text), 11):
            yield "token", text[i:i + 11]
        self.reason = "length"
        self.completion += len(text) // 4

    def finish_reason(self):
        return self.reason

    def usage(self):
        return {"completion_tokens": self.completion, "prompt_tokens": 0}


def _run_engine(monkeypatch, upload, message="list every order with its total"):
    events: List[tuple] = []

    async def emit(kind, data):
        events.append((kind, data))

    monkeypatch.setattr(db, "get_uploads", lambda _conv: [upload])
    answer = asyncio.run(dataset.run_dataset_engine(message, "conv-x", [], emit, effort="fast"))
    return answer, events


def test_a_long_answer_is_continued_and_a_truncated_one_says_where_it_stopped(tmp_path, monkeypatch):
    _, upload = profiled_upload(tmp_path, 40, 11)
    fake = _EndlessModel()
    monkeypatch.setattr(llm, "stream_chat_events", fake.stream)
    monkeypatch.setattr(llm, "get_finish_reason", fake.finish_reason)
    monkeypatch.setattr(llm, "get_usage", fake.usage)
    # A small total budget, so the endless model is stopped by it.
    monkeypatch.setattr(settings, "continuation_budget_fast", 3000)

    answer, events = _run_engine(monkeypatch, upload)

    assert fake.calls >= 2, "a call that ran out of room is continued, not dropped"
    # The stop is IN the answer — the stored text, what the next turn and the
    # API read — not only in a UI notice.
    assert "This answer stops here, before it was finished" in answer
    assert "reached its length limit" in answer
    streamed = "".join(d["text"] for k, d in events if k == "token")
    assert streamed == answer, "what is stored is exactly what was streamed"
    meta = [d for k, d in events if k == "meta"][-1]
    assert meta["route"] == "dataset"
    assert meta["continuation"]["truncated"] is True
    assert meta["continuation"]["stop_reason"] == "budget"


def test_an_answer_that_finished_carries_no_stop_note(tmp_path, monkeypatch):
    _, upload = profiled_upload(tmp_path, 40, 11)

    async def stream(messages, **kwargs):
        yield "token", "Total revenue, the sum over all 40 orders: 1.00."

    monkeypatch.setattr(llm, "stream_chat_events", stream)
    monkeypatch.setattr(llm, "get_finish_reason", lambda: "stop")
    answer, events = _run_engine(monkeypatch, upload, "total revenue?")
    assert answer == "Total revenue, the sum over all 40 orders: 1.00."
    meta = [d for k, d in events if k == "meta"][-1]
    assert "continuation" not in meta
