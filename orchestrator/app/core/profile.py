"""Tabular profiling (Phase 4).

The model is shown a PROFILE — shape, dtypes, null rates, ranges — never the
file. Exactly THREE things in a profile are raw data, and they are the only
such content that reaches a prompt:

  * sample rows   (PROFILE_SAMPLE_ROWS, cells truncated)
  * top values    (PROFILE_TOP_VALUES, values truncated)
  * group values  (aggregates.by_group: the values of a column with at most
                   50 distinct values that occur in at least GROUP_MIN_ROWS
                   rows, from a column not named like an identifier or a
                   contact detail; values truncated)

All three go through `clip()`. Everything else is derived statistics: sums,
averages, counts, months. Group values are NOT the same class as top values:
top values show the 5 most common, group values list up to 50. QA measured
the difference on 2026-09-18 (a status seen once at row 4,322, 40 synthetic
SSNs and emails reaching the prompt), so a value seen in one row, and any
column named like a key or a contact, is never listed value by value.

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
import threading
from collections import Counter
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


#: Rows the CSV type sniffer reads. Types are GUESSED from these rows only.
_SNIFF_ROWS = 20_000


def _is_csv_source(path: str) -> bool:
    return not path.lower().endswith((".parquet", ".json", ".jsonl", ".ndjson"))


def _reader_sql(path: str, *, csv_options: str = "") -> str:
    lower = path.lower()
    quoted = path.replace("'", "''")
    if lower.endswith(".parquet"):
        return f"read_parquet('{quoted}')"
    if lower.endswith((".json", ".jsonl", ".ndjson")):
        return f"read_json_auto('{quoted}')"
    extra = f", {csv_options}" if csv_options else ""
    # store_rejects records every row IGNORE_ERRORS drops, in the SAME parse
    # (measured: no cost on a clean 1M-row file, 0.41 s either way). Without
    # it a row with 'N/A' in a number column past the sniff window vanished
    # from every total with nothing saying so.
    return (
        f"read_csv_auto('{quoted}', SAMPLE_SIZE={_SNIFF_ROWS}, IGNORE_ERRORS=true, "
        f"store_rejects=true{extra})"
    )


def _types_option(types: Optional[Dict[str, str]]) -> str:
    if not types:
        return ""
    pairs = ", ".join(
        "'" + str(n).replace("'", "''") + "': '" + t + "'" for n, t in sorted(types.items())
    )
    return f"types={{{pairs}}}"


def _join_options(*parts: str) -> str:
    return ", ".join(p for p in parts if p)


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
    con, path: str, source_columns: List[tuple], scratch: str, reader: Optional[str] = None,
    stem: str = "table",
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
    target = os.path.join(scratch, f"{stem}.parquet")
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
#: grouping key when its values repeat (and its name is not a key's or a
#: contact's, see _is_group_key). Group values go through clip() as top
#: values do, but a breakdown lists up to 50 of them where top values list 5.
TOP_VALUES_MAX_DISTINCT = 50
#: A group value is listed only when at least this many rows hold it. QA,
#: 2026-09-18: listing every value put a status seen ONCE, at row 4,322 of
#: 5,000, into the prompt; top values never surface such a cell, because
#: they show the 5 most common. Values below this are left out, the entry is
#: marked truncated, and `omitted` says how many there were.
GROUP_MIN_ROWS = 2
AGG_MAX_MEASURES = 8
AGG_MAX_GROUP_COLUMNS = 6
AGG_MAX_DATE_COLUMNS = 3
AGG_MAX_MONTHS = 60
#: Size of the aggregates block AS THE PROMPT RENDERS IT: dataset.format_profile
#: and the /v1 file context both print profiles with json indent=1, which QA
#: measured at 1.42x the compact size (38,196 compact -> 54,229 rendered on a
#: 1M-row file). The size is counted at AGG_RENDER_DEPTH, the nesting of a
#: sheet's block in the chat prompt (list > workbook > sheets > sheet), the
#: deepest place a block sits, so every rendering is at most this long.
#: A WORKBOOK shares one such budget across its sheets (profile_excel).
#: Over the cap the WIDEST breakdowns go first (_cap_aggregates), each drop
#: recorded in `omitted`.
AGG_MAX_CHARS = 40_000
AGG_RENDER_DEPTH = 4
#: How many column names one `omitted` reason lists before "and N more".
_REASON_NAMES = 10
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


# Names that make a column an identifier or a contact detail. Such a column is
# never broken down value by value, however few values it has: QA measured a
# 1,200-row payroll file whose 40 synthetic SSNs and 40 emails ALL reached the
# prompt through by_group (5 and 9 of them on 4810da0, through the sample and
# top values). Its top values are unchanged.
_CONTACT_TOKENS = frozenset({
    "email", "mail", "address", "addr", "street", "passport", "password",
    "passwd", "secret", "token", "iban", "ip",
})


def _is_group_key(name: Any) -> bool:
    tokens = _name_tokens(name)
    if set(tokens) & (_KEY_TOKENS | _CONTACT_TOKENS):
        return False
    return not (tokens and tokens[-1] in _KEY_LAST_TOKENS)


def _label(name: Any) -> str:
    """A column name inside an `omitted` reason: clipped, so a 6,000-character
    header cannot make one reason larger than the whole cap."""
    return str(clip(str(name)))


def _names(names: List[Any]) -> str:
    shown = ", ".join(_label(n) for n in names[:_REASON_NAMES])
    more = len(names) - _REASON_NAMES
    return shown + (f" and {more} more" if more > 0 else "")


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


def _json_exact(total: Any, mode: str) -> bool:
    """Whether the JSON number spells the exact total. A DECIMAL sum becomes a
    float, and a float carries about 15 significant digits: 12345678901234.56
    survives, 1234567890123456.78 does not."""
    if total is None or mode != "dec":
        return True
    return Decimal(repr(float(total))) == Decimal(total)


def _lossy_reason(name: Any) -> str:
    return (
        f"{_label(name)}: a total has more significant digits than a JSON number "
        f"carries exactly (about 15), so it is rounded and its cents are not exact"
    )


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
            [f"{_label(name)}: not summed, it holds infinite or NaN values"],
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
            f"{_label(name)}: values carry more than {_EXACT_DECIMALS} decimal places, so its totals are "
            f"double precision rounded to {_SIG_DIGITS} significant digits"
        )
    stats = {
        "sum": _json_sum(total, mode),
        "avg": _json_avg(total, n, mode),
        "median": _sig(med),
        "stddev": _sig(sd),
    }
    plan = {"name": name, "ident": ident, "mode": mode, "sum_sql": sum_sql, "lossy": False}
    if not _json_exact(total, mode):
        plan["lossy"] = True
        reasons.append(_lossy_reason(name))
    return stats, plan, reasons


def _ranked(rows: List[tuple]) -> List[tuple]:
    """Largest total first; ties on row count, then on the value's text
    exactly as top values break them; the blank group last."""
    return sorted(
        rows,
        key=lambda r: (r[0] is None, r[3] is None, -(r[3] if r[3] is not None else 0), -r[2], r[1] or ""),
    )


def _group_breakdowns(con, src: str, group: str, gident: str, plans: List[dict], omitted: List[str]) -> List[dict]:
    sums = ", ".join(f"{p['sum_sql']}, COUNT({p['ident']})" for p in plans)
    found = con.execute(
        f"SELECT {gident}, CAST({gident} AS VARCHAR), COUNT(*), {sums} FROM {src} GROUP BY 1, 2"
    ).fetchall()
    # A value held by fewer than GROUP_MIN_ROWS rows is one raw cell from
    # anywhere in the file: it is left out, and the entry says so. The blank
    # group (None) is not content and always stays.
    rare = [r for r in found if r[0] is not None and int(r[2]) < GROUP_MIN_ROWS]
    listed = [r for r in found if r[0] is None or int(r[2]) >= GROUP_MIN_ROWS]
    if rare:
        omitted.append(
            f"by_group {_label(group)}: {len(rare)} value(s) found in only one row are not "
            f"listed, so its listed groups add up to less than the column totals"
        )
    cap = TOP_VALUES_MAX_DISTINCT + 1  # every value, plus the blank group
    out = []
    for j, plan in enumerate(plans):
        per = [(v, text, int(n), rest[2 * j], int(rest[2 * j + 1] or 0)) for v, text, n, *rest in listed]
        ranked = _ranked(per)
        if not plan.get("lossy") and not all(_json_exact(r[3], plan["mode"]) for r in ranked[:cap]):
            plan["lossy"] = True
            omitted.append(_lossy_reason(plan["name"]))
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
            "truncated": len(ranked) > cap or bool(rare),
        })
    return out


def _month_breakdowns(con, src: str, date: str, dident: str, kind: str, plans: List[dict], omitted: List[str]) -> List[dict]:
    # A zoned timestamp is bucketed in UTC: the session zone is the host's,
    # and the same file must give the same months on every machine.
    stamp = f"timezone('UTC', {dident})" if kind == "tz" else dident
    sums = ", ".join(p["sum_sql"] for p in plans)
    found = con.execute(
        f"SELECT strftime(date_trunc('month', {stamp}), '%Y-%m') AS m, COUNT(*), {sums} "
        f"FROM {src} WHERE {dident} IS NOT NULL GROUP BY 1 ORDER BY 1"
    ).fetchall()
    kept = found[-AGG_MAX_MONTHS:]  # the most recent months
    for j, plan in enumerate(plans):
        if not plan.get("lossy") and not all(_json_exact(rest[j], plan["mode"]) for _m, _n, *rest in kept):
            plan["lossy"] = True
            omitted.append(_lossy_reason(plan["name"]))
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


# --- Size, as the prompt renders it ----------------------------------------
#
# json.dumps(indent=1) is the pure-Python encoder (the C one only runs without
# an indent), so a block is never re-rendered per drop. Its rendered length is
# the compact length (C encoder) plus the whitespace indent=1 adds, which is
# exact and depends only on the SHAPE: a non-empty container whose line sits
# at depth d, holding n items, gains n*(d+1) + d + 2 characters over compact
# (a newline and d+1 spaces per item, a newline and d spaces before the
# closing bracket, minus the space compact puts after each comma).


def _indent_extra(obj: Any, depth: int) -> int:
    if isinstance(obj, dict):
        items = list(obj.values())
    elif isinstance(obj, (list, tuple)):
        items = list(obj)
    else:
        return 0
    if not items:
        return 0
    n = len(items)
    return n * (depth + 1) + depth + 2 + sum(_indent_extra(v, depth + 1) for v in items)


def _rendered_len(obj: Any, depth: int = AGG_RENDER_DEPTH) -> int:
    """len(the text json.dumps(..., indent=1) prints for obj when its first
    line sits at `depth`)."""
    return len(json.dumps(obj, ensure_ascii=False, default=str)) + _indent_extra(obj, depth)


def _list_len(items_total: int, n: int, depth: int) -> int:
    """Rendered length of an n-item list whose line sits at `depth`, from the
    sum of its items' own rendered lengths (each measured at depth + 1)."""
    if not n:
        return 2
    return items_total + n * (depth + 2) + (n - 1) + depth + 3


def _agg_chars(agg: Dict[str, Any]) -> int:
    return _rendered_len(agg, AGG_RENDER_DEPTH)


def _cap_aggregates(agg: Dict[str, Any], limit: Optional[int] = None) -> None:
    """Shrink the block until its rendered size fits `limit` (AGG_MAX_CHARS).

    First the WIDEST breakdowns go: the most ROWS (groups or months), not the
    most characters. Measured on a 1M-row sales file: ranked by characters,
    the cap dropped revenue-by-month and cost-by-month and kept unit_price-
    and discount-by-month, because a money total has more digits — the most
    useful breakdowns went first. Row count does not depend on the values.

    Only a pathological file gets further (QA: eight measures with
    6,000-character headers made the `measures` list alone 48,249 characters
    against the 40,000 cap). Then `measures` is cut from the end — each
    measure's totals stay in its own column entry — and last the `omitted`
    reasons, keeping the first.
    """
    limit = AGG_MAX_CHARS if limit is None else limit
    if _agg_chars(agg) <= limit:
        return
    d = AGG_RENDER_DEPTH
    item_depth = d + 2  # agg keys sit at d + 1, their list items at d + 2
    widths = {}
    rows = {}
    for kind in ("by_group", "by_month"):
        for i, entry in enumerate(agg[kind]):
            widths[(kind, i)] = _rendered_len(entry, item_depth)
            rows[(kind, i)] = len(entry["rows"])
    # Most rows first; on a tie the later breakdown goes first (the later
    # measure, then the later column), so the order is fixed by the file.
    order = sorted(rows, key=lambda k: (-rows[k], k[0] == "by_group", -k[1]))
    # Sizes are tracked arithmetically: the skeleton (everything but the three
    # lists) plus each list rendered from its items' lengths.
    skeleton = _rendered_len({**agg, "by_group": [], "by_month": [], "omitted": []}, d) - 6
    kept = {kind: [widths[(kind, i)] for i in range(len(agg[kind]))] for kind in ("by_group", "by_month")}
    sums = {kind: sum(v) for kind, v in kept.items()}
    counts = {kind: len(v) for kind, v in kept.items()}
    notes = [len(json.dumps(r, ensure_ascii=False)) for r in agg["omitted"]]
    note_sum, note_n = sum(notes), len(notes)

    def size() -> int:
        return (
            skeleton
            + _list_len(sums["by_group"], counts["by_group"], d + 1)
            + _list_len(sums["by_month"], counts["by_month"], d + 1)
            + _list_len(note_sum, note_n, d + 1)
        )

    dropped = set()
    for key in order:
        if size() <= limit:
            break
        kind, i = key
        entry = agg[kind][i]
        label = entry.get("group") if kind == "by_group" else entry.get("date")
        reason = (
            f"{kind} {_label(label)} x {_label(entry['measure'])}: dropped to keep the aggregates "
            f"under {limit} characters"
        )
        dropped.add(key)
        agg["omitted"].append(reason)
        sums[kind] -= widths[key]
        counts[kind] -= 1
        note_sum += len(json.dumps(reason, ensure_ascii=False))
        note_n += 1
    for kind in ("by_group", "by_month"):
        agg[kind] = [e for i, e in enumerate(agg[kind]) if (kind, i) not in dropped]
    # The arithmetic above is exact for json.dumps' separators; this re-check
    # keeps the cap a guarantee if that ever changes.
    while _agg_chars(agg) > limit and (agg["by_group"] or agg["by_month"]):
        kind, i = min(
            ((k, i) for k in ("by_group", "by_month") for i in range(len(agg[k]))),
            key=lambda k: (-len(agg[k[0]][k[1]]["rows"]), k[0] == "by_group", -k[1]),
        )
        entry = agg[kind].pop(i)
        label = entry.get("group") if kind == "by_group" else entry.get("date")
        agg["omitted"].append(
            f"{kind} {_label(label)} x {_label(entry['measure'])}: dropped to keep the aggregates "
            f"under {limit} characters"
        )
    if _agg_chars(agg) <= limit:
        return
    # Only column NAMES and reasons are left. Cut measures from the end,
    # keeping room for one reason that says so.
    note = "{} measures not listed here: their names alone exceed the size cap; each keeps its totals in its column"
    cut = 0
    while agg["measures"] and _agg_chars(agg) + len(note) + 16 > limit:
        agg["measures"].pop()
        cut += 1
    if cut:
        agg["omitted"].insert(0, note.format(cut))
    popped = 0
    while _agg_chars(agg) > limit and len(agg["omitted"]) > 1:
        agg["omitted"].pop()
        popped += 1
    if popped:
        agg["omitted"][-1] = f"{popped + 1} omissions not listed: the column names alone exceed the cap"
    while _agg_chars(agg) > limit and agg["omitted"]:
        agg["omitted"].pop()


def _aggregates(
    con, src: str, measures: List[dict], groups: List[dict], dates: List[dict], omitted: List[str],
    limit: Optional[int] = None,
) -> Dict[str, Any]:
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
            + _names(extra)
        )
    agg["measures"] = [p["name"] for p in plans]
    if not plans:
        _cap_aggregates(agg, limit)
        return agg
    if len(groups) > AGG_MAX_GROUP_COLUMNS:
        omitted.append(
            f"group columns beyond the first {AGG_MAX_GROUP_COLUMNS} are not broken down: "
            + _names([g["name"] for g in groups[AGG_MAX_GROUP_COLUMNS:]])
        )
    for g in groups[:AGG_MAX_GROUP_COLUMNS]:
        try:
            agg["by_group"].extend(_group_breakdowns(con, src, g["name"], g["ident"], plans, omitted))
        except Exception as exc:  # noqa: BLE001 — one bad column must not sink the rest
            omitted.append(f"by_group {_label(g['name'])}: could not be computed ({type(exc).__name__})")
    if len(dates) > AGG_MAX_DATE_COLUMNS:
        omitted.append(
            f"date columns beyond the first {AGG_MAX_DATE_COLUMNS} have no monthly totals: "
            + _names([d["name"] for d in dates[AGG_MAX_DATE_COLUMNS:]])
        )
    for d in dates[:AGG_MAX_DATE_COLUMNS]:
        try:
            agg["by_month"].extend(_month_breakdowns(con, src, d["name"], d["ident"], d["kind"], plans, omitted))
        except Exception as exc:  # noqa: BLE001
            omitted.append(f"by_month {_label(d['name'])}: could not be computed ({type(exc).__name__})")
    _cap_aggregates(agg, limit)
    return agg


def _reject_floor(con) -> int:
    """The last reject scan so far (-1 when none): rows dropped by LATER scans
    are the ones the statistics about to be computed will miss."""
    try:
        got = con.execute("SELECT max(scan_id) FROM reject_scans").fetchone()[0]
    except Exception:  # noqa: BLE001 — no CSV scanned yet: no table
        return -1
    return -1 if got is None else int(got)


def _dropped_rows(con, floor: int) -> Tuple[int, List[str]]:
    """(rows IGNORE_ERRORS dropped in the widest scan after `floor`, the
    columns named in their errors). One scan in the parse-once path; per
    statement, each scan drops only rows failing in the columns it reads."""
    try:
        found = con.execute(
            "SELECT scan_id, COUNT(DISTINCT line), list(DISTINCT column_name) "
            f"FROM reject_errors WHERE scan_id > {int(floor)} GROUP BY 1"
        ).fetchall()
    except Exception:  # noqa: BLE001 — no CSV scanned: nothing dropped
        return 0, []
    if not found:
        return 0, []
    _scan, n, cols = max(found, key=lambda r: (r[1], r[0]))
    return int(n), sorted(str(c) for c in (cols or []) if c is not None)


#: _lines_at_least never reads more than this: a 2 GB file of a few very long
#: lines would otherwise be read whole just to count them.
_LINES_READ_MAX = 64 * 1024 * 1024


def _lines_at_least(path: str, n: int) -> bool:
    """Whether the file has at least n line breaks, reading no further than
    that. Rows never outnumber line breaks, so a file with fewer is inside the
    sniff window and needs no re-check."""
    seen_n = seen_r = 0
    try:
        if os.path.getsize(path) >= _LINES_READ_MAX:
            return True  # a file this size is re-checked without counting
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    return False
                seen_n += chunk.count(b"\n")
                seen_r += chunk.count(b"\r")
                if max(seen_n, seen_r) >= n:
                    return True
    except OSError:
        return False


def _fractional_int_columns(path: str, csv_options: str, ints: List[tuple]) -> Optional[Dict[str, int]]:
    """Whole-number columns that hold fractional values the sniffer never saw.

    The sniffer types a column from the first _SNIFF_ROWS rows. QA, 2026-09-18:
    whole-dollar revenue for 25,000 rows and cents after it was typed BIGINT,
    and DuckDB ROUNDS '2051.01' into a BIGINT without an error, so the sum
    was 127,559,755 against a true 127,559,748.92 with nothing flagged. The
    file is read once more, as text, for the integer columns only. It runs on
    its OWN connection while the typed copy is made (_FractionCheck): on 1M
    rows x 12 columns it took 0.105 s alone and the pair took 0.235-0.258 s
    against 0.230-0.253 s for the copy by itself. {name: fractional values},
    or None when the check could not run.
    """
    quoted = path.replace("'", "''")
    checks = ", ".join(
        "count_if(TRY_CAST({q} AS DOUBLE) <> round(TRY_CAST({q} AS DOUBLE)))".format(
            q='"' + str(c[0]).replace('"', '""') + '"'
        )
        for c in ints
    )
    con = _duck()
    try:
        def run(extra: str):
            reader = (
                f"read_csv('{quoted}', SAMPLE_SIZE={_SNIFF_ROWS}, IGNORE_ERRORS=true, "
                f"all_varchar=true{extra})"
            )
            return con.execute(f"SELECT {checks} FROM {reader}").fetchone()

        try:
            counts = run(f", {csv_options}" if csv_options else "")
        except Exception:  # noqa: BLE001 — as text the header can be read differently
            if csv_options:
                return None
            try:
                has_header = con.execute(
                    f"SELECT HasHeader FROM sniff_csv('{quoted}', sample_size={_SNIFF_ROWS}, "
                    f"ignore_errors=true)"
                ).fetchone()[0]
                counts = run(f", header={'true' if has_header else 'false'}")
            except Exception:  # noqa: BLE001
                return None
        return {str(c[0]): int(k) for c, k in zip(ints, counts) if k}
    finally:
        con.close()


class _FractionCheck:
    """_fractional_int_columns in a thread (DuckDB releases the GIL while it
    runs a statement), so the re-read overlaps the typed COPY."""

    def __init__(self, path: str, csv_options: str, ints: List[tuple]) -> None:
        self._result: Optional[Dict[str, int]] = None
        self._thread = threading.Thread(
            target=self._run, args=(path, csv_options, ints), name="profile-sniff-check", daemon=True
        )
        self._thread.start()

    def _run(self, path: str, csv_options: str, ints: List[tuple]) -> None:
        try:
            self._result = _fractional_int_columns(path, csv_options, ints)
        except Exception:  # noqa: BLE001 — reported as "could not re-check"
            self._result = None

    def result(self) -> Optional[Dict[str, int]]:
        self._thread.join()
        return self._result


def profile_tabular(
    path: str,
    *,
    name: Optional[str] = None,
    csv_options: str = "",
    types: Optional[Dict[str, str]] = None,
    agg_max_chars: Optional[int] = None,
    full_chars: Optional[int] = None,
) -> Dict[str, Any]:
    """Shape, per-column statistics, and a capped sample — no bulk load.

    csv_options / types: only the spreadsheet path passes any (profile_excel).
    agg_max_chars / full_chars: a workbook's sheets share one budget for
    their aggregates and their full rows; a file alone gets the full budget.
    """
    rel = name or os.path.basename(path)
    out: Dict[str, Any] = {
        "file": rel,
        "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
        "kind": "table",
    }
    con = _duck()
    scratch: Optional[str] = None
    omitted: List[str] = []
    check: Optional[_FractionCheck] = None
    try:
        csv_source = _is_csv_source(path)

        def bind(column_types: Optional[Dict[str, str]]) -> str:
            opts = _join_options(csv_options, _types_option(column_types)) if csv_source else ""
            return _reader_sql(path, csv_options=opts) if opts else _reader_sql(path)

        src = bind(types)
        # The source's own DESCRIBE is the authority on names and types (the
        # sniffer reads a sample, not the file); the columnar copy is used
        # only when it reproduces it exactly.
        described = con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()
        # Past the sniff window a column's type is a guess (_fractional_int_columns).
        # A file inside it was sniffed whole (measured: cents at row 18,501
        # of 19,500 made the column DOUBLE), so it is parsed once, as before.
        ints = [r for r in described[: settings.profile_max_columns] if _numeric_kind(r[1]) == "int"]
        if csv_source and ints and _lines_at_least(path, _SNIFF_ROWS // 2):
            check = _FractionCheck(path, csv_options, ints)
        if _parse_once_enabled() and not path.lower().endswith(".parquet"):
            scratch = _scratch_dir()
        floor = _reject_floor(con)
        if scratch is not None:
            src = _columnar_copy(con, path, described, scratch, reader=src) or src
        out["rows"] = int(con.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0])

        if check is not None:
            fractional = check.result()
            if fractional is None:
                omitted.append(
                    f"whole-number columns could not be re-checked past the first {_SNIFF_ROWS} rows, "
                    f"so a total there may have lost decimals"
                )
            elif fractional:
                retyped = {**(types or {}), **{n: "DOUBLE" for n in fractional}}
                try:
                    src2 = bind(retyped)
                    described2 = con.execute(f"DESCRIBE SELECT * FROM {src2}").fetchall()
                    kinds = {r[0]: r[1] for r in described2}
                    usable = [r[0] for r in described2] == [r[0] for r in described] and all(
                        kinds.get(n) == "DOUBLE" for n in fractional
                    )
                except Exception:  # noqa: BLE001
                    usable = False
                if usable:
                    # Read as DOUBLE, exactly what the sniffer picks when it
                    # sees a decimal; money then sums as DECIMAL, to the cent.
                    floor = _reject_floor(con)
                    copy2 = (
                        _columnar_copy(con, path, described2, scratch, reader=src2, stem="retyped")
                        if scratch is not None else None
                    )
                    src, described = copy2 or src2, described2
                    out["rows"] = int(con.execute(f"SELECT COUNT(*) FROM {src}").fetchone()[0])
                else:
                    for n, k in sorted(fractional.items()):
                        omitted.append(
                            f"{_label(n)}: {k} values have decimals but were read as whole numbers, "
                            f"so its totals are rounded"
                        )

        columns = [{"name": r[0], "dtype": r[1]} for r in described]
        out["columns_total"] = len(columns)
        columns = columns[: settings.profile_max_columns]
        if len(described) > len(columns):
            out["columns_truncated"] = True

        rows = out["rows"] or 0
        measures: List[dict] = []
        groups: List[dict] = []
        dates: List[dict] = []
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
                    stats, plan, why = {}, None, [f"{_label(col['name'])}: totals could not be computed ({type(exc).__name__})"]
                col.update(stats)
                omitted.extend(why)
                if plan is not None:
                    measures.append(plan)
            elif (
                2 <= col["distinct"] <= TOP_VALUES_MAX_DISTINCT
                and col["distinct"] < nonnull
                and _is_group_key(col["name"])
            ):
                # A grouping key: few values, and they REPEAT. An all-distinct
                # column would give one row per group, which is the file.
                groups.append({"name": col["name"], "ident": ident})
            kind = _date_kind(col["dtype"])
            if kind is not None and nonnull > 0:
                dates.append({"name": col["name"], "ident": ident, "kind": kind})
        out["columns"] = columns
        dropped, where = _dropped_rows(con, floor)
        if dropped:
            # First in the list: the cap trims reasons from the end, and this
            # one changes what every total means.
            omitted.insert(0, (
                f"{dropped} row(s) could not be read under the detected column types"
                + (f" (a bad value in {_names(where)})" if where else "")
                + " and are left out of every total"
            ))
        out["aggregates"] = _aggregates(con, src, measures, groups, dates, omitted, agg_max_chars)

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
            budget = settings.profile_full_chars if full_chars is None else full_chars
            if len(json.dumps(full, default=str)) <= budget:
                out["full_rows"] = full
                out["full_content"] = True
    except Exception as exc:
        out["error"] = f"could not be read as a table: {type(exc).__name__}"
    finally:
        if check is not None:
            check.result()  # its connection reads the file: never outlive the call
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

# --- What one workbook may cost -------------------------------------------
#
# A few kilobytes of .xlsx can describe far more work than they hold, and
# every cap in core/archive.py passes (it bounds the ZIP, not the sheet):
#   * one 32,000-character shared string referenced by every cell: a
#     29,591-byte file wrote a 64,002,030-byte scratch CSV; 65,803 bytes
#     wrote 640 MB, took 6.49 s (0.10 s on 4810da0) and peaked at 1.8 GB RSS;
#   * a <dimension> of A1:XFD50000 with one data row: openpyxl yields 50,000
#     rows of 16,384 empty cells, the writer padded each, and a 4,843-byte
#     file ran past 1,200 s at 18 GB RSS (0.0 s and 31 MB on 4810da0).
# The chat upload profiles ON its event loop (uploads.py _finalise_dataset),
# so this is a stall for every user. Three bounds, one budget per WORKBOOK:
#: cells written to scratch CSVs (a row costs its written width, blank or not)
SHEET_MAX_CELLS = 20_000_000
#: bytes written to scratch CSVs: SHEET_CSV_MAX_RATIO x the workbook's
#: uncompressed size, at least SHEET_CSV_MIN_BYTES, at most the ceiling an
#: uploaded zip of CSVs faces (settings.archive_max_uncompressed_mb). New
#: work stays within a constant factor of the XML openpyxl already parses.
SHEET_CSV_MAX_RATIO = 4
SHEET_CSV_MIN_BYTES = 8 * 1024 * 1024
# and the WIDTH written is settings.profile_max_columns, the columns a CSV
# profile describes: DuckDB never parses 16,384 columns.


class _WorkbookBudget:
    def __init__(self, unpacked: int) -> None:
        ceiling = settings.archive_max_uncompressed_mb * 1024 * 1024
        self.cells = SHEET_MAX_CELLS
        self.bytes = min(ceiling, max(SHEET_CSV_MIN_BYTES, SHEET_CSV_MAX_RATIO * unpacked))
        self.full_chars = settings.profile_full_chars


class _CountingWriter:
    """What csv.writer writes to: encodes each row once and counts its bytes
    against the workbook budget."""

    def __init__(self, raw, budget: _WorkbookBudget) -> None:
        self.raw = raw
        self.budget = budget

    def write(self, text: str) -> None:
        data = text.encode("utf-8")
        self.budget.bytes -= len(data)
        self.raw.write(data)


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


def _profile_sheet(ws, scratch: Optional[str], index: int, budget: _WorkbookBudget) -> Dict[str, Any]:
    """Stream one worksheet into a scratch CSV and profile it like a CSV.

    Falls back to the old minimal record (names, count, five sample rows)
    when there is no scratch space, the sheet has no header, the write fails
    (a sandbox RLIMIT_FSIZE raises EFBIG), the workbook's budget runs out
    (then `note` says which) or the CSV cannot be read.
    """
    rows_iter = ws.iter_rows(values_only=True)
    header = next(rows_iter, None) or ()
    names = [str(h) if h is not None else f"column_{i + 1}" for i, h in enumerate(header)]
    shown = names[: settings.profile_max_columns]
    width = len(shown)  # the columns written; the header may be wider
    sample: List[Dict[str, Any]] = []
    counted = 0
    note: Optional[str] = None

    csv_path: Optional[str] = None
    raw = None
    writer = None
    if scratch is not None and width:
        csv_path = os.path.join(scratch, f"sheet-{index + 1}.csv")
        try:
            raw = open(csv_path, "wb")
            out_fh = _CountingWriter(raw, budget)
            writer = csv.writer(out_fh, lineterminator="\n")
            writer.writerow(shown)
        except OSError:
            writer = None
    # Written by hand: csv quotes a lone empty field as "", which a text
    # column would read as an empty string, not a blank.
    blank = ("," * (width - 1) + "\n").encode("utf-8")
    last_blank: Any = None
    # Per column: a datetime with a time of day, and any non-datetime cell.
    stamped = [False] * width
    other = [False] * width
    for row in rows_iter:
        counted += 1
        if len(sample) < settings.profile_sample_rows:
            sample.append({n: clip(v) for n, v in zip(shown, row[:width])})
        if writer is None:
            continue
        budget.cells -= width
        if budget.cells < 0:
            note = f"not profiled: the workbook holds more than {SHEET_MAX_CELLS:,} cells"
            writer = None
            continue
        try:
            # openpyxl yields ONE shared tuple for every missing row number:
            # skipped by identity, a gap of 50,000 rows costs 50,000 writes.
            if row is last_blank or all(v is None for v in row[:width]):
                last_blank = row
                budget.bytes -= len(blank)
                raw.write(blank)
            else:
                cells = tuple(row[:width]) + (None,) * max(0, width - len(row))
                for i, v in enumerate(cells):
                    if isinstance(v, _dt.datetime):
                        if v.time() != _dt.time(0) or v.tzinfo is not None:
                            stamped[i] = True
                    elif v is not None:
                        other[i] = True
                writer.writerow([_sheet_cell(v) for v in cells])
        except OSError:
            writer = None
            continue
        if budget.bytes < 0:
            note = (
                "not profiled: the workbook expands to more text than "
                f"{SHEET_CSV_MAX_RATIO}x its unpacked size"
            )
            writer = None
    minimal: Dict[str, Any] = {
        "name": ws.title,
        "rows": counted,
        "columns": [{"name": n} for n in shown],
        "sample_rows": sample,
    }
    if note:
        minimal["note"] = note
    if raw is not None:
        try:
            raw.close()
        except OSError:
            writer = None
    if writer is None or csv_path is None:
        if csv_path is not None and os.path.exists(csv_path):
            os.remove(csv_path)
        return minimal

    # Midnight cells were written as dates and the rest as timestamps, so a
    # column holding both reads as TEXT unless it is declared: declare it,
    # but only when every filled cell really was a datetime.
    seen = Counter(shown)
    forced = {
        shown[i]: "TIMESTAMP" for i in range(width)
        if stamped[i] and not other[i] and seen[shown[i]] == 1
    }
    try:
        prof = profile_tabular(
            csv_path,
            name=str(ws.title),
            csv_options=_SHEET_CSV_OPTIONS,
            types=forced,
            full_chars=max(0, budget.full_chars),
        )
    finally:
        os.remove(csv_path)
    if prof.get("error"):
        return minimal
    sheet: Dict[str, Any] = {"name": ws.title, "rows": counted}
    sheet.update({k: v for k, v in prof.items() if k not in _SHEET_DROPPED_KEYS})
    if len(names) > width:
        # As for a CSV wider than PROFILE_MAX_COLUMNS: the first columns are
        # described, and a table with columns cut never ships in full.
        sheet["columns_total"] = len(names)
        sheet["columns_truncated"] = True
        sheet.pop("full_rows", None)
        sheet.pop("full_content", None)
    if "full_rows" in sheet:
        budget.full_chars -= len(json.dumps(sheet["full_rows"], default=str))
    if prof.get("rows") != counted:
        # The CSV reader skipped rows it could not parse under the sniffed
        # types (IGNORE_ERRORS, as for any CSV): the statistics cover these.
        sheet["rows_profiled"] = prof.get("rows")
    return sheet


def _share_aggregates(sheets: List[Dict[str, Any]]) -> None:
    """One AGG_MAX_CHARS for the whole workbook, shared out by water-filling:
    a sheet that needs less than an equal share keeps all of it, the rest is
    split among the larger ones. QA measured the per-sheet cap on a 3-sheet x
    1,000-row workbook: its chat prompt block grew from 7,037 characters on
    4810da0 to 163,562, and the /v1 context cut sheets 2 and 3 off."""
    blocks = [s["aggregates"] for s in sheets if isinstance(s.get("aggregates"), dict)]
    sizes = [_agg_chars(b) for b in blocks]
    if sum(sizes) <= AGG_MAX_CHARS:
        return
    left = AGG_MAX_CHARS
    order = sorted(range(len(blocks)), key=lambda i: (sizes[i], i))
    for k, i in enumerate(order):
        share = left // (len(order) - k)
        allowed = min(sizes[i], share)
        if sizes[i] > allowed:
            _cap_aggregates(blocks[i], allowed)
        left -= min(_agg_chars(blocks[i]), allowed)


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
    and openpyxl's row count. The workbook is ONE file: its sheets share one
    cell, byte, full-rows and aggregates budget.
    """
    rel = name or os.path.basename(path)
    # Belt and braces: re-run the container caps here so no future caller can
    # reach openpyxl without them (raises ArchiveError on a bomb).
    plan = archive.check_zip_container(path, label="spreadsheet")

    from openpyxl import load_workbook

    out: Dict[str, Any] = {
        "file": rel,
        "bytes": os.path.getsize(path),
        "kind": "spreadsheet",
        "sheets": [],
    }
    budget = _WorkbookBudget(int(getattr(plan, "total_uncompressed", 0) or 0))
    # read_only streams rows; data_only avoids evaluating anything.
    wb = load_workbook(path, read_only=True, data_only=True)
    scratch: Optional[str] = None
    try:
        scratch = _scratch_dir()
        for index, ws in enumerate(wb.worksheets[:10]):
            out["sheets"].append(_profile_sheet(ws, scratch, index, budget))
    finally:
        wb.close()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
    _share_aggregates(out["sheets"])
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
