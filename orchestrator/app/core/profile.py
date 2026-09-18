"""Tabular profiling (Phase 4).

The model is shown a PROFILE — shape, dtypes, null rates, ranges — never the
file. Exactly TWO things in a profile are raw data, and they are the only such
content that reaches a prompt:

  * sample rows   (PROFILE_SAMPLE_ROWS, cells truncated)
  * top values    (PROFILE_TOP_VALUES, values truncated)

Both go through `clip()`. Everything else is derived statistics — including
the computed totals in `aggregates` (sums, averages, group and monthly
totals), whose group VALUES are the same low-cardinality values top values
report, clipped the same way.

Note what is deliberately ABSENT: min/max VALUES for string columns. Those are
an arbitrary raw cell from anywhere in the file — the alphabetically first
value can be a secret buried at row 500, and truncation cannot help when the
secret is short. String columns report min/max LENGTH instead.

Counting is done with DuckDB rather than pandas so a large CSV is never loaded
into memory — the box has ~27 GB free and an upload may be 2 GB extracted.
Nothing here executes file content: no pickle, no macros, no eval.
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import math
import os
import re
import shutil
import tempfile
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Any, Dict, List, Optional, Tuple

from ..config import settings
from . import archive

TABULAR_SUFFIXES = {".csv", ".tsv", ".txt", ".parquet", ".json", ".jsonl", ".ndjson"}
EXCEL_SUFFIXES = {".xlsx"}


def clip(value: Any) -> Any:
    """Truncate any raw value before it can reach a prompt."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return value
    text = str(value)
    cap = settings.profile_cell_chars
    return text if len(text) <= cap else text[:cap] + "…[truncated]"


_STRINGISH = ("VARCHAR", "TEXT", "STRING", "CHAR", "BLOB", "JSON", "UUID")


def _is_stringish(dtype: str) -> bool:
    return any(token in (dtype or "").upper() for token in _STRINGISH)


def _duck():
    import duckdb  # lazy: keeps import time off the request path

    con = duckdb.connect(":memory:")
    # Profiling must not reach the NETWORK. `enable_external_access=false`
    # cannot be used here — it also blocks reading the local file we were
    # asked to profile — so the network is closed off specifically:
    # no extension can be fetched or auto-loaded, and the HTTP/S3
    # filesystems are disabled outright.
    for pragma in (
        "SET autoinstall_known_extensions=false",
        "SET autoload_known_extensions=false",
        "SET disabled_filesystems='HTTPFileSystem,S3FileSystem'",
    ):
        try:
            con.execute(pragma)
        except Exception:
            pass
    return con


def _reader_sql(path: str, *, csv_options: str = "") -> str:
    lower = path.lower()
    quoted = path.replace("'", "''")
    if lower.endswith(".parquet"):
        return f"read_parquet('{quoted}')"
    if lower.endswith((".json", ".jsonl", ".ndjson")):
        return f"read_json_auto('{quoted}')"
    extra = f", {csv_options}" if csv_options else ""
    return f"read_csv_auto('{quoted}', SAMPLE_SIZE=20000, IGNORE_ERRORS=true{extra})"


def _parse_once_enabled() -> bool:
    raw = os.environ.get("PROFILE_PARSE_ONCE", "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _scratch_dir() -> Optional[str]:
    """A private scratch directory for the columnar copy, or None (then the
    source is read per statement, exactly as before). PROFILE_SCRATCH_DIR
    places it; the default is the process temp directory."""
    base = os.environ.get("PROFILE_SCRATCH_DIR", "").strip() or None
    try:
        return tempfile.mkdtemp(prefix="profile-", dir=base)
    except OSError:
        return None


def _columnar_copy(
    con, path: str, source_columns: List[tuple], scratch: str, reader: Optional[str] = None
) -> Optional[str]:
    """Parse a CSV/JSON source ONCE into a scratch Parquet file; the reader
    expression over it, or None to keep reading the source itself.

    `profile_tabular` issues 2 statements per column plus one per
    low-cardinality column, and every one of them used to re-run
    `read_csv_auto` — sniffing and parsing the whole file again. Measured
    2026-09-14 (dbperf2, DuckDB 1.5.5, including the DESCRIBE check below),
    same statistics, types and samples:

        10k rows x 20 cols CSV (2.4 MB)     1.90 s -> 0.14 s
        100k rows x 30 cols CSV (36 MB)     6.89 s -> 0.49 s
        100k rows x 30 cols JSONL (57 MB)   8.65 s -> 0.55 s
        1M rows x 40 cols CSV (486 MB)     17.31 s -> 1.41-1.85 s

    Parquet keeps the types the CSV sniffer chose and the row order, so the
    profile — types, counts, samples, `full_rows` — is unchanged. That is
    CHECKED, not assumed: the copy is used only when its column names and
    types equal the source's DESCRIBE (Parquet has no HUGEINT, BIT or UNION;
    such a file keeps the per-statement path).

    ONE EXCEPTION, verified 2026-09-14: a DIRTY file under IGNORE_ERRORS. The
    per-statement path let each statement skip only the rows that failed to
    parse in the columns IT projected, so its numbers disagreed with each
    other: a 60,002-line CSV with two bad `amount` values, a bad date and two
    malformed lines reported rows=60,001, id distinct=60,000, amount
    distinct=59,998. The copy drops every row that fails in ANY column, once,
    so all statistics describe the same 59,998 rows, the rows a
    `SELECT *` over the file returns. Clean files profile identically.
    COPY streams, but its row-group
    buffers raised peak RSS at 1M x 40 from 1.5 GB to 1.9 GB. Any failure here
    (disk full, an odd file the COPY rejects) falls back to per-statement
    parsing, which then reports whatever it always reported.
    """
    if path.lower().endswith(".parquet") or not _parse_once_enabled():
        return None
    target = os.path.join(scratch, "table.parquet")
    quoted = target.replace("'", "''")
    copy = f"read_parquet('{quoted}')"
    try:
        source = reader or _reader_sql(path)
        con.execute(f"COPY (SELECT * FROM {source}) TO '{quoted}' (FORMAT parquet)")
        copied = con.execute(f"DESCRIBE SELECT * FROM {copy}").fetchall()
    except Exception:  # noqa: BLE001 — the source path reports the real error
        return None
    if [tuple(r[:2]) for r in copied] != [tuple(r[:2]) for r in source_columns]:
        return None
    return copy


# ---------------------------------------------------------------------------
# Computed aggregates (B2, 2026-09-18)
# ---------------------------------------------------------------------------
#
# The profile used to carry no sum, average or group total, so the model added
# up `full_rows` itself. Measured live by the audit on 200 rows that were
# entirely in the prompt: true revenue 1,022,098.02, the model's 2,707,720.92
# (+165%), every region off by +151% to +206% and ranked wrong. Numbers the
# model is asked to quote are therefore computed HERE, over every row.
#
# out["aggregates"] — the contract the dataset engine codes against:
#   {"computed": "exact", "measures": [col, ...],
#    "by_group": [{"group", "measure", "rows": [{"value", "count", "sum", "avg"}], "truncated"}],
#    "by_month": [{"date", "measure", "rows": [{"month": "YYYY-MM", "count", "sum"}], "truncated"}],
#    "omitted": [reason, ...]}
#
# EXACTNESS, stated once:
#   * integer columns sum as integers (DuckDB HUGEINT): exact.
#   * DOUBLE columns whose every value is a number of at most 6 decimal places
#     (to within float noise: a spreadsheet's cached 12.339999999999998 is
#     12.34) sum as DECIMAL(38,6): exact to the cent and beyond. JSON carries
#     the sum as a float, which spells the exact decimal up to 15 significant
#     digits (a total below 10 trillion with cents).
#   * DOUBLE columns with more decimals than that are measurements, not money:
#     they sum in double precision (Kahan) rounded to 12 significant digits,
#     and `omitted` says so.
#   * avg is the exact sum divided by the non-null count, rounded half-even to
#     6 decimal places. median and stddev (the SAMPLE deviation, n - 1, as
#     Excel's STDEV) are double precision rounded to 12 significant digits:
#     parallel aggregation can move the last bits between runs, and a profile
#     must be the same every time it is built.
#   * count is ROWS in the group (as top values count); sum and avg skip nulls.

#: A column with at most this many distinct values gets top values, and is a
#: grouping key when its values repeat. One rule for both, and group values go
#: through clip() exactly as top values do, so a breakdown carries the same
#: class of raw content as top values. It lists EVERY value of such a column
#: (up to 50) where top values list the first PROFILE_TOP_VALUES (5).
TOP_VALUES_MAX_DISTINCT = 50
AGG_MAX_MEASURES = 8
AGG_MAX_GROUP_COLUMNS = 6
AGG_MAX_DATE_COLUMNS = 3
AGG_MAX_MONTHS = 60
#: Serialized size of the whole aggregates block (compact JSON, as stored).
#: Measured: the 200-row fixture (2 measures, 2 group columns, 24 months)
#: needs 4.0k; a 1M-row 12-column sales file (5 measures, 4 group columns of
#: 3-40 values, 60 months) needs 44.3k, so the cap drops two of its monthly
#: breakdowns. Over the cap the WIDEST breakdowns go first (_cap_aggregates),
#: each drop recorded in `omitted`.
AGG_MAX_CHARS = 40_000
_EXACT_DECIMALS = 6
_SIG_DIGITS = 12
_AVG_STEP = Decimal(1).scaleb(-_EXACT_DECIMALS)

_INT_TYPES = frozenset({
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT",
})
_FLOAT_TYPES = frozenset({"FLOAT", "REAL", "DOUBLE"})

# Name tokens that make a number a KEY, not a quantity to add. Matched as whole
# tokens (snake_case, camelCase, spaces), so "paid" and "valid" are not "id".
_KEY_TOKENS = frozenset({
    "id", "ids", "uuid", "guid", "key", "zip", "zipcode", "postcode", "postal",
    "pin", "pincode", "phone", "mobile", "tel", "telephone", "fax", "ssn",
    "code", "sku", "ean", "upc", "isbn", "rowid", "index", "idx",
})
_KEY_LAST_TOKENS = frozenset({"no", "num", "nbr", "number"})  # invoice_no, phone number
_CALENDAR_TOKENS = frozenset({
    "year", "yr", "fy", "fiscalyear", "month", "quarter", "qtr", "week",
    "day", "weekday", "hour", "minute", "dow", "doy",
})
_COORD_TOKENS = frozenset({"lat", "latitude", "lon", "lng", "longitude"})
# A name that says money or quantity keeps an all-distinct integer column a
# measure: twenty salaries in a small file are all different, and still add up.
_MEASURE_TOKENS = frozenset({
    "amount", "amt", "revenue", "sales", "sale", "salary", "salaries", "wage",
    "wages", "pay", "payment", "price", "cost", "costs", "total", "subtotal",
    "profit", "margin", "income", "expense", "expenses", "spend", "budget",
    "fee", "fees", "tax", "balance", "value", "qty", "quantity", "units",
    "volume", "score", "points", "weight", "duration", "hours", "minutes",
    "count", "gmv", "arr", "mrr", "discount", "commission", "bonus", "net",
    "gross", "usd", "eur", "inr", "gbp",
})


def _name_tokens(name: Any) -> List[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(name))
    return [t for t in re.split(r"[^0-9A-Za-z]+", spaced.lower()) if t]


def _numeric_kind(dtype: str) -> Optional[str]:
    d = (dtype or "").upper().strip()
    if d in _INT_TYPES:
        return "int"
    if d.startswith(("DECIMAL", "NUMERIC")):
        return "decimal"
    if d in _FLOAT_TYPES:
        return "float"
    return None


def _date_kind(dtype: str) -> Optional[str]:
    d = (dtype or "").upper().strip()
    if d == "DATE":
        return "date"
    if d.startswith("TIMESTAMP"):
        return "tz" if ("TIME ZONE" in d or d == "TIMESTAMPTZ") else "ts"
    return None


def _is_measure(name: Any, dtype: str, nonnull: int, distinct: int, lo: Any, hi: Any) -> bool:
    """A numeric column worth adding up — not a key, a code, a year or a place."""
    kind = _numeric_kind(dtype)
    if kind is None or nonnull <= 0:
        return False
    tokens = _name_tokens(name)
    words = set(tokens)
    if words & (_KEY_TOKENS | _CALENDAR_TOKENS | _COORD_TOKENS):
        return False
    if tokens and tokens[-1] in _KEY_LAST_TOKENS:
        return False
    if kind != "int" or words & _MEASURE_TOKENS:
        return True
    # A year column by its values (2019..2024 under any name) is a grouping key.
    if (
        distinct <= TOP_VALUES_MAX_DISTINCT
        and isinstance(lo, int) and isinstance(hi, int)
        and 1900 <= lo and hi <= 2100
    ):
        return False
    # All-distinct integers are a key (order numbers, row numbers).
    return not (nonnull >= 2 and distinct == nonnull)


def _sig(value: Any) -> Optional[float]:
    if value is None:
        return None
    f = float(value)
    if not math.isfinite(f):
        return None
    return float(f"{f:.{_SIG_DIGITS}g}")


def _json_sum(total: Any, mode: str) -> Any:
    if total is None:
        return None
    if mode == "int":
        return int(total)
    if mode == "dec":
        return float(total)  # correctly rounded: spells the exact decimal to 15 digits
    return _sig(total)


def _json_avg(total: Any, n: int, mode: str) -> Optional[float]:
    if total is None or not n:
        return None
    if mode in ("int", "dec"):
        with localcontext() as ctx:
            ctx.prec = 80  # a HUGEINT sum has 39 digits; quantize needs room for 6 more
            mean = (Decimal(total) / Decimal(n)).quantize(_AVG_STEP, rounding=ROUND_HALF_EVEN)
        return float(mean)
    return _sig(float(total) / n)


def _measure_stats(con, src: str, name: str, ident: str, dtype: str) -> Tuple[Dict[str, Any], Optional[dict], List[str]]:
    """(column stats, a summing plan for the breakdowns or None, omitted reasons)."""
    kind = _numeric_kind(dtype)
    as_double = f"CAST({ident} AS DOUBLE)"
    if kind == "float":
        # One scan decides exactness AND computes both candidate sums.
        exact_ok = (
            f"bool_and(isfinite({ident}) AND abs({ident}) < 1e30 AND "
            f"abs({ident} - round({ident}, {_EXACT_DECIMALS})) <= greatest(1e-9, abs({ident}) * 1e-14))"
        )
        dec_sum = f"SUM(TRY_CAST({ident} AS DECIMAL(38,{_EXACT_DECIMALS})))"
        head = f"COUNT({ident}), bool_and(isfinite({ident})), {exact_ok}, {dec_sum}, fsum({ident})"
    else:
        head = f"COUNT({ident}), true, true, SUM({ident}), NULL"
    tail = f"median({as_double}), stddev_samp({as_double}) FILTER (WHERE isfinite({as_double}))"
    try:
        n, finite, exact, dsum, fsum, med, sd = con.execute(f"SELECT {head}, {tail} FROM {src}").fetchone()
    except Exception:  # noqa: BLE001 — a deviation past DOUBLE range: keep the sum
        n, finite, exact, dsum, fsum, med = con.execute(
            f"SELECT {head}, median({as_double}) FROM {src}"
        ).fetchone()
        sd = None
    n = int(n or 0)
    if not finite:
        return (
            {"sum": None, "avg": None, "median": None, "stddev": None},
            None,
            [f"{name}: not summed, it holds infinite or NaN values"],
        )
    reasons: List[str] = []
    if kind == "int":
        mode, total, sum_sql = "int", dsum, f"SUM({ident})"
    elif kind == "decimal":
        mode, total, sum_sql = "dec", dsum, f"SUM({ident})"
    elif exact:
        mode, total, sum_sql = "dec", dsum, f"SUM(TRY_CAST({ident} AS DECIMAL(38,{_EXACT_DECIMALS})))"
    else:
        mode, total, sum_sql = "float", fsum, f"fsum({ident})"
        reasons.append(
            f"{name}: values carry more than {_EXACT_DECIMALS} decimal places, so its totals are "
            f"double precision rounded to {_SIG_DIGITS} significant digits"
        )
    stats = {
        "sum": _json_sum(total, mode),
        "avg": _json_avg(total, n, mode),
        "median": _sig(med),
        "stddev": _sig(sd),
    }
    plan = {"name": name, "ident": ident, "mode": mode, "sum_sql": sum_sql}
    return stats, plan, reasons


def _ranked(rows: List[tuple]) -> List[tuple]:
    """Largest total first; ties on row count, then on the value's text
    exactly as top values break them; the blank group last."""
    return sorted(
        rows,
        key=lambda r: (r[0] is None, r[3] is None, -(r[3] if r[3] is not None else 0), -r[2], r[1] or ""),
    )


def _group_breakdowns(con, src: str, group: str, gident: str, plans: List[dict]) -> List[dict]:
    sums = ", ".join(f"{p['sum_sql']}, COUNT({p['ident']})" for p in plans)
    found = con.execute(
        f"SELECT {gident}, CAST({gident} AS VARCHAR), COUNT(*), {sums} FROM {src} GROUP BY 1, 2"
    ).fetchall()
    cap = TOP_VALUES_MAX_DISTINCT + 1  # every value, plus the blank group
    out = []
    for j, plan in enumerate(plans):
        per = [(v, text, int(n), rest[2 * j], int(rest[2 * j + 1] or 0)) for v, text, n, *rest in found]
        ranked = _ranked(per)
        out.append({
            "group": group,
            "measure": plan["name"],
            "rows": [
                {
                    "value": clip(v),
                    "count": n,
                    "sum": _json_sum(total, plan["mode"]),
                    "avg": _json_avg(total, nn, plan["mode"]),
                }
                for v, _text, n, total, nn in ranked[:cap]
            ],
            "truncated": len(ranked) > cap,
        })
    return out


def _month_breakdowns(con, src: str, date: str, dident: str, kind: str, plans: List[dict]) -> List[dict]:
    # A zoned timestamp is bucketed in UTC: the session zone is the host's,
    # and the same file must give the same months on every machine.
    stamp = f"timezone('UTC', {dident})" if kind == "tz" else dident
    sums = ", ".join(p["sum_sql"] for p in plans)
    found = con.execute(
        f"SELECT strftime(date_trunc('month', {stamp}), '%Y-%m') AS m, COUNT(*), {sums} "
        f"FROM {src} WHERE {dident} IS NOT NULL GROUP BY 1 ORDER BY 1"
    ).fetchall()
    kept = found[-AGG_MAX_MONTHS:]  # the most recent months
    return [
        {
            "date": date,
            "measure": plan["name"],
            "rows": [
                {"month": m, "count": int(n), "sum": _json_sum(rest[j], plan["mode"])}
                for m, n, *rest in kept
            ],
            "truncated": len(found) > len(kept),
        }
        for j, plan in enumerate(plans)
    ]


def _agg_chars(agg: Dict[str, Any]) -> int:
    return len(json.dumps(agg, ensure_ascii=False, default=str))


def _cap_aggregates(agg: Dict[str, Any]) -> None:
    """Drop the widest breakdowns until the block fits AGG_MAX_CHARS.

    WIDEST means the most ROWS (groups or months), not the most characters.
    Measured on a 1M-row sales file: ranked by characters, the cap dropped
    revenue-by-month and cost-by-month and kept unit_price- and
    discount-by-month, because a money total has more digits — the most
    useful breakdowns went first. Row count does not depend on the values.
    """
    if _agg_chars(agg) <= AGG_MAX_CHARS:
        return
    widths = {}
    rows = {}
    for kind in ("by_group", "by_month"):
        for i, entry in enumerate(agg[kind]):
            widths[(kind, i)] = _agg_chars(entry)
            rows[(kind, i)] = len(entry["rows"])
    # Most rows first; on a tie the later breakdown goes first (the later
    # measure, then the later column), so the order is fixed by the file.
    order = sorted(rows, key=lambda k: (-rows[k], k[0] == "by_group", -k[1]))
    # Sizes are tracked arithmetically — re-serializing a 400k-character block
    # once per drop is quadratic. A list item costs its width plus ", " when
    # it has a neighbour.
    size = _agg_chars(agg)
    left = {kind: len(agg[kind]) for kind in ("by_group", "by_month")}
    notes = len(agg["omitted"])
    dropped = set()
    for key in order:
        if size <= AGG_MAX_CHARS:
            break
        kind, i = key
        entry = agg[kind][i]
        label = entry.get("group") if kind == "by_group" else entry.get("date")
        reason = (
            f"{kind} {label} x {entry['measure']}: dropped to keep the aggregates under "
            f"{AGG_MAX_CHARS} characters"
        )
        dropped.add(key)
        agg["omitted"].append(reason)
        size -= widths[key] + (2 if left[kind] > 1 else 0)
        left[kind] -= 1
        size += len(json.dumps(reason, ensure_ascii=False)) + (2 if notes else 0)
        notes += 1
    for kind in ("by_group", "by_month"):
        agg[kind] = [e for i, e in enumerate(agg[kind]) if (kind, i) not in dropped]
    # The arithmetic above is exact for json.dumps' default separators; this
    # re-check keeps the cap a guarantee if that ever changes.
    while _agg_chars(agg) > AGG_MAX_CHARS and (agg["by_group"] or agg["by_month"]):
        kind, i = min(
            ((k, i) for k in ("by_group", "by_month") for i in range(len(agg[k]))),
            key=lambda k: (-len(agg[k[0]][k[1]]["rows"]), k[0] == "by_group", -k[1]),
        )
        entry = agg[kind].pop(i)
        label = entry.get("group") if kind == "by_group" else entry.get("date")
        agg["omitted"].append(
            f"{kind} {label} x {entry['measure']}: dropped to keep the aggregates under "
            f"{AGG_MAX_CHARS} characters"
        )
    # Only column NAMES are left when this still does not fit (a file whose
    # headers are thousands of characters long): keep the first reasons.
    if _agg_chars(agg) > AGG_MAX_CHARS:
        cut = 0
        while _agg_chars(agg) > AGG_MAX_CHARS and len(agg["omitted"]) > 1:
            agg["omitted"].pop()
            cut += 1
        if cut:
            agg["omitted"][-1] = f"{cut + 1} omissions not listed: the column names alone exceed the cap"


def _aggregates(con, src: str, measures: List[dict], groups: List[dict], dates: List[dict], omitted: List[str]) -> Dict[str, Any]:
    agg: Dict[str, Any] = {
        "computed": "exact",
        "measures": [],
        "by_group": [],
        "by_month": [],
        "omitted": omitted,
    }
    plans = measures[:AGG_MAX_MEASURES]
    extra = [m["name"] for m in measures[AGG_MAX_MEASURES:]]
    if extra:
        omitted.append(
            f"measures beyond the first {AGG_MAX_MEASURES} have column totals but no breakdowns: "
            + ", ".join(map(str, extra))
        )
    agg["measures"] = [p["name"] for p in plans]
    if not plans:
        return agg
    if len(groups) > AGG_MAX_GROUP_COLUMNS:
        omitted.append(
            f"group columns beyond the first {AGG_MAX_GROUP_COLUMNS} are not broken down: "
            + ", ".join(str(g["name"]) for g in groups[AGG_MAX_GROUP_COLUMNS:])
        )
    for g in groups[:AGG_MAX_GROUP_COLUMNS]:
        try:
            agg["by_group"].extend(_group_breakdowns(con, src, g["name"], g["ident"], plans))
        except Exception as exc:  # noqa: BLE001 — one bad column must not sink the rest
            omitted.append(f"by_group {g['name']}: could not be computed ({type(exc).__name__})")
    if len(dates) > AGG_MAX_DATE_COLUMNS:
        omitted.append(
            f"date columns beyond the first {AGG_MAX_DATE_COLUMNS} have no monthly totals: "
            + ", ".join(str(d["name"]) for d in dates[AGG_MAX_DATE_COLUMNS:])
        )
    for d in dates[:AGG_MAX_DATE_COLUMNS]:
        try:
            agg["by_month"].extend(_month_breakdowns(con, src, d["name"], d["ident"], d["kind"], plans))
        except Exception as exc:  # noqa: BLE001
            omitted.append(f"by_month {d['name']}: could not be computed ({type(exc).__name__})")
    _cap_aggregates(agg)
    return agg


def profile_tabular(path: str, *, name: Optional[str] = None, csv_options: str = "") -> Dict[str, Any]:
    """Shape, per-column statistics, and a capped sample — no bulk load."""
    rel = name or os.path.basename(path)
    out: Dict[str, Any] = {
        "file": rel,
        "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
        "kind": "table",
    }
    con = _duck()
    scratch: Optional[str] = None
    try:
        # csv_options: only the spreadsheet path passes any (profile_excel).
        src = _reader_sql(path, csv_options=csv_options) if csv_options else _reader_sql(path)
        # The source's own DESCRIBE is the authority on names and types (the
        # sniffer reads a sample, not the file); the columnar copy is used
        # only when it reproduces it exactly.
        described = con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
        if _parse_once_enabled() and not path.lower().endswith(".parquet"):
            scratch = _scratch_dir()
            if scratch is not None:
                src = _columnar_copy(con, path, described, scratch, reader=src) or src
        out["rows"] = int(con.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0])
        columns = [{"name": r[0], "dtype": r[1]} for r in described]
        out["columns_total"] = len(columns)
        columns = columns[: settings.profile_max_columns]
        if len(described) > len(columns):
            out["columns_truncated"] = True

        rows = out["rows"] or 0
        measures: List[dict] = []
        groups: List[dict] = []
        dates: List[dict] = []
        omitted: List[str] = []
        for col in columns:
            ident = '"' + col["name"].replace('"', '""') + '"'
            lo = hi = None
            try:
                nulls, distinct = con.execute(
                    f"SELECT COUNT(*) FILTER (WHERE {ident} IS NULL), "
                    f"COUNT(DISTINCT {ident}) FROM {src}"
                ).fetchone()
                col["null_pct"] = round(100.0 * nulls / rows, 2) if rows else 0.0
                col["distinct"] = int(distinct)
                if _is_stringish(col["dtype"]):
                    # NEVER report min/max VALUES for a string column. They are
                    # an arbitrary raw cell from anywhere in the file — the
                    # alphabetically first value is whatever happens to sort
                    # first, which can be a secret buried at row 500 — and
                    # truncation cannot help when the secret is short. Length
                    # statistics carry the useful signal with no raw content.
                    lo_len, hi_len = con.execute(
                        f"SELECT MIN(LENGTH({ident})), MAX(LENGTH({ident})) FROM {src}"
                    ).fetchone()
                    col["min_length"] = int(lo_len) if lo_len is not None else None
                    col["max_length"] = int(hi_len) if hi_len is not None else None
                else:
                    lo, hi = con.execute(
                        f"SELECT MIN({ident}), MAX({ident}) FROM {src}"
                    ).fetchone()
                    col["min"], col["max"] = clip(lo), clip(hi)
                # Top values are raw data too → capped in count AND length.
                if 0 < col["distinct"] <= TOP_VALUES_MAX_DISTINCT:
                    tops = con.execute(
                        f"SELECT {ident} AS v, COUNT(*) AS n FROM {src} "
                        # Ties broken on the value's text: without it equal
                        # counts came back in scan order, and one file gave
                        # different profiles on repeated runs (3 of 10).
                        f"WHERE {ident} IS NOT NULL GROUP BY 1 "
                        f"ORDER BY n DESC, CAST(v AS VARCHAR) "
                        f"LIMIT {settings.profile_top_values}"
                    ).fetchall()
                    col["top_values"] = [
                        {"value": clip(v), "count": int(n)} for v, n in tops
                    ]
            except Exception:
                col["stats_unavailable"] = True
                continue
            nonnull = rows - int(nulls)
            if _is_measure(col["name"], col["dtype"], nonnull, col["distinct"], lo, hi):
                try:
                    stats, plan, why = _measure_stats(con, src, col["name"], ident, col["dtype"])
                except Exception as exc:  # noqa: BLE001 — e.g. a HUGEINT sum past 38 digits
                    stats, plan, why = {}, None, [f"{col['name']}: totals could not be computed ({type(exc).__name__})"]
                col.update(stats)
                omitted.extend(why)
                if plan is not None:
                    measures.append(plan)
            elif 2 <= col["distinct"] <= TOP_VALUES_MAX_DISTINCT and col["distinct"] < nonnull:
                # A grouping key: few values, and they REPEAT. An all-distinct
                # column would give one row per group, which is the file.
                groups.append({"name": col["name"], "ident": ident})
            kind = _date_kind(col["dtype"])
            if kind is not None and nonnull > 0:
                dates.append({"name": col["name"], "ident": ident, "kind": kind})
        out["columns"] = columns
        out["aggregates"] = _aggregates(con, src, measures, groups, dates, omitted)

        sample = con.execute(
            f"SELECT * FROM {src} LIMIT {settings.profile_sample_rows}"
        ).fetchall()
        names = [c["name"] for c in columns]
        out["sample_rows"] = [
            {n: clip(v) for n, v in zip(names, row[: len(names)])} for row in sample
        ]

        # Small-file full-content path (2026-08-06): at or under the row
        # threshold — with no columns cut — the ENTIRE table goes into the
        # profile so the model can compute exact aggregates, ChatGPT-style.
        # Cells still pass through clip(); the char cap catches the
        # few-rows-but-very-wide case, falling back to profile-only.
        if (
            0 < rows <= settings.profile_full_rows_max
            and not out.get("columns_truncated")
        ):
            all_rows = con.execute(f"SELECT * FROM {src}").fetchall()
            full = [
                {n: clip(v) for n, v in zip(names, row[: len(names)])}
                for row in all_rows
            ]
            if len(json.dumps(full, default=str)) <= settings.profile_full_chars:
                out["full_rows"] = full
                out["full_content"] = True
    except Exception as exc:
        out["error"] = f"could not be read as a table: {type(exc).__name__}"
    finally:
        con.close()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return out


# The scratch CSV is written by us, so the reader is told its dialect; only
# the column TYPES are sniffed, exactly as for an uploaded CSV. Row 1 is the
# header by the spreadsheet contract (it always was: the old profile named
# columns from it), so a sheet of all-text columns keeps its names.
_SHEET_CSV_OPTIONS = "HEADER=true, DELIM=',', QUOTE='\"', ESCAPE='\"'"

#: Keys of a table profile that describe the scratch file, not the sheet.
_SHEET_DROPPED_KEYS = frozenset({"file", "bytes", "kind", "rows"})


def _sheet_cell(value: Any) -> str:
    """One cell as a CSV field an uploaded CSV of the same data would hold."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, _dt.datetime):
        # A spreadsheet has no DATE type: a date is a datetime at midnight.
        # Written as a date, it profiles as the DATE a CSV export would give.
        if value.tzinfo is None and value.time() == _dt.time(0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)  # shortest text that reads back as the same double
    return str(value)


def _profile_sheet(ws, scratch: Optional[str], index: int) -> Dict[str, Any]:
    """Stream one worksheet into a scratch CSV and profile it like a CSV.

    Falls back to the old minimal record (names, count, five sample rows)
    when there is no scratch space, the sheet has no header, the write fails
    (a sandbox RLIMIT_FSIZE raises EFBIG) or the CSV cannot be read.
    """
    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter, None) or ()
    names = [str(h) if h is not None else f"column_{i + 1}" for i, h in enumerate(header)]
    shown = names[: settings.profile_max_columns]
    width = len(names)
    sample: List[Dict[str, Any]] = []
    counted = 0

    csv_path: Optional[str] = None
    fh = None
    writer = None
    if scratch is not None and width:
        csv_path = os.path.join(scratch, f"sheet-{index + 1}.csv")
        try:
            fh = open(csv_path, "w", newline="", encoding="utf-8")
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(names)
        except OSError:
            writer = None
    # Per column: a datetime with a time of day, and any non-datetime cell.
    stamped = [False] * width
    other = [False] * width
    for row in rows_iter:
        counted += 1
        if len(sample) < settings.profile_sample_rows:
            sample.append({n: clip(v) for n, v in zip(shown, row[: len(shown)])})
        if writer is None:
            continue
        cells = tuple(row[:width]) + (None,) * max(0, width - len(row))
        for i, v in enumerate(cells):
            if isinstance(v, _dt.datetime):
                if v.time() != _dt.time(0) or v.tzinfo is not None:
                    stamped[i] = True
            elif v is not None:
                other[i] = True
        try:
            if all(v is None for v in cells):
                # Written by hand: csv quotes a lone empty field as "", which
                # a text column would read as an empty string, not a blank.
                fh.write("," * (width - 1) + "\n")
            else:
                writer.writerow([_sheet_cell(v) for v in cells])
        except OSError:
            writer = None
    minimal = {
        "name": ws.title,
        "rows": counted,
        "columns": [{"name": n} for n in shown],
        "sample_rows": sample,
    }
    if fh is not None:
        try:
            fh.close()
        except OSError:
            writer = None
    if writer is None or csv_path is None:
        if csv_path is not None and os.path.exists(csv_path):
            os.remove(csv_path)
        return minimal

    # Midnight cells were written as dates and the rest as timestamps, so a
    # column holding both reads as TEXT unless it is declared: declare it,
    # but only when every filled cell really was a datetime.
    forced = [
        names[i] for i in range(width)
        if stamped[i] and not other[i] and names.count(names[i]) == 1
    ]
    options = _SHEET_CSV_OPTIONS
    if forced:
        pairs = ", ".join("'" + n.replace("'", "''") + "': 'TIMESTAMP'" for n in forced)
        options += f", types={{{pairs}}}"
    try:
        prof = profile_tabular(csv_path, name=str(ws.title), csv_options=options)
    finally:
        os.remove(csv_path)
    if prof.get("error"):
        return minimal
    sheet: Dict[str, Any] = {"name": ws.title, "rows": counted}
    sheet.update({k: v for k, v in prof.items() if k not in _SHEET_DROPPED_KEYS})
    if prof.get("rows") != counted:
        # The CSV reader skipped rows it could not parse under the sniffed
        # types (IGNORE_ERRORS, as for any CSV): the statistics cover these.
        sheet["rows_profiled"] = prof.get("rows")
    return sheet


def profile_excel(path: str, *, name: Optional[str] = None) -> Dict[str, Any]:
    """Profile an .xlsx — AFTER the zip-container caps have been applied.

    An .xlsx is a ZIP. Handing one straight to openpyxl would walk around
    every bomb cap in core/archive.py, so the caller must have run
    `archive.check_zip_container` first; this asserts it rather than trusting.

    Each sheet (the first 10) is profiled LIKE A CSV (B15, 2026-09-18): the
    old profile carried only names, a row count and five sample rows, so the
    same 50 rows were answerable uploaded as .csv and refused as .xlsx. A
    sheet now carries everything a table profile does — types, nulls, ranges,
    top values, computed aggregates, full rows when small — under its name
    and openpyxl's row count.
    """
    rel = name or os.path.basename(path)
    # Belt and braces: re-run the container caps here so no future caller can
    # reach openpyxl without them (raises ArchiveError on a bomb).
    archive.check_zip_container(path, label="spreadsheet")

    from openpyxl import load_workbook

    out: Dict[str, Any] = {
        "file": rel,
        "bytes": os.path.getsize(path),
        "kind": "spreadsheet",
        "sheets": [],
    }
    # read_only streams rows; data_only avoids evaluating anything.
    wb = load_workbook(path, read_only=True, data_only=True)
    scratch: Optional[str] = None
    try:
        scratch = _scratch_dir()
        for index, ws in enumerate(wb.worksheets[:10]):
            out["sheets"].append(_profile_sheet(ws, scratch, index))
    finally:
        wb.close()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    return out


def profile_file(path: str, *, name: Optional[str] = None) -> Dict[str, Any]:
    """Profile one file, choosing a reader by extension AND magic bytes."""
    rel = name or os.path.basename(path)
    lower = rel.lower()
    for suffix in archive.REFUSED_SUFFIXES:
        if lower.endswith(suffix):
            return {"file": rel, "kind": "skipped", "reason": f"refused type ({suffix})"}
    if any(lower.endswith(s) for s in EXCEL_SUFFIXES):
        if not archive.is_zip_container(path):
            return {"file": rel, "kind": "skipped", "reason": "not a real .xlsx"}
        return profile_excel(path, name=rel)
    if any(lower.endswith(s) for s in TABULAR_SUFFIXES) or archive.sniff_format(
        path
    ) == "parquet":
        return profile_tabular(path, name=rel)
    return {
        "file": rel,
        "kind": "other",
        "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
    }


def profile_directory(root: str) -> List[Dict[str, Any]]:
    """Profile every readable file in an extracted archive, newest caps applied."""
    profiles: List[Dict[str, Any]] = []
    for dirpath, _dirs, files in os.walk(root):
        for fname in sorted(files):
            if len(profiles) >= settings.profile_max_files:
                return profiles
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, root)
            try:
                profiles.append(profile_file(full, name=rel))
            except archive.ArchiveError as exc:
                profiles.append({"file": rel, "kind": "skipped", "reason": str(exc)})
            except Exception as exc:  # a bad file must not sink the upload
                profiles.append(
                    {"file": rel, "kind": "skipped", "reason": type(exc).__name__}
                )
    return profiles


def profile_json(profiles: List[Dict[str, Any]]) -> str:
    return json.dumps(profiles, ensure_ascii=False, default=str)
