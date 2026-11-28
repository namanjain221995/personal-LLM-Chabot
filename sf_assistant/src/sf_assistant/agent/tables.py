"""The record rows code writes (latency plan step 3, O1, the owner's go of 2026-10-29; new code, no notebook function
behind it). Used only with ``SFA_TABLES=code`` (:func:`sf_assistant.config.tables_settings`); with ``model``, the
default, nothing here runs and the record agent types its rows as before.

The record agent writes 1-3 sentences (the lead with the numbers the question asks for), a table line ``{{table qN}}``
alone on its line for each result with 2 or more rows to show (``RECORDS_TABLES_NOTE``, appended to its system prompt
by ``RecordSession.ask``), then one bullet on what was counted, any caveat and the Source line; ``qN`` is the
``query_id`` every successful record result carries as its first key. Code writes that result's rows in place of the
line (:func:`expand`): the same rows, order and values the model saw.

Table lines (:data:`MARKER_RE`): case, inner spaces, ":" or "=", a bare number and bold or backtick wrappers are
read (``**{{ Table: Q3 }}**``, ``{{table 3}}``); the canonical form is ``{{table q3}}``. A span that only looks like
one (:data:`LOOSE_RE`: ``{{table}}``, ``{{tables q3 and q4}}``, ``{{q3}}``, and the near misses ``{table q3}``,
``{{table q3}``, ``{{table_q3}}``, ``{{tbl q3}}``, ``{{query q3}}``, ``{{result q3}}``, ``{{q_3}}``; review of
2026-10-30) is malformed: always removed, never filled. Any other ``{{...}}`` text stays (a stored template value the
model copied); :func:`left_over` counts what a final answer still holds of it.

Which results get rows (:data:`TABLE_TOOLS`): a successful data call of list_records (its ``rows``), group_records
(``groups``), trend_records (``buckets``), compare_records (``segments``) or count_records with a non-empty
``breakdown``, whose result reached the model whole (the length of its tool message, with room for the chunk ids the
evidence does not keep, is within ``LIMITS.knowledge_tool_result_chars``). Rows come only from
``QueryEvidence.model_result``, the dict the model received; ``statements`` (record Ids, the limit+1 row, raw values,
rows the result cap removed) are never read. Every other tool (about_people and find_references above all: people
answers carry reference codes and can be longer than the model saw), a failed call and an unknown id give no rows:
the line is removed and counted (``unknown``, ``failed``, ``no_rows``, ``empty``, ``unseen``; a second line of an id
already filled: ``repeated``; every row typed by the model itself as well, one row per line, in the forms
:func:`_key_re` and :func:`_has_value` read: ``typed``, :func:`_typed`; a bullet that names some rows types none, and rows
typed only in part are filled and counted as ``partly_typed``). Of a typed block only its Total line can stay, when it
holds split totals the model's text does not give (:func:`_total_left`), just before the Source line; when a later check
cuts a line of the model's copy, the whole block comes back (:func:`put_back`).

The form (owner's choice D3, the forms today's answers and graders read): list_records rows are a markdown table
(the result's columns, headed by their catalog labels: :func:`column_labels`); groups, buckets, segments and a
breakdown are ``- label: value`` lines, a split inline on its row's line (``- Completed: 5 (Full Time: 4, Contract:
1)``; a split value the row lacks is left out, never written as 0), and a period the result marks partial (its first
or last) ends with ``(partial)``. Values stand as the result gives them: numbers in their JSON form, dates and
buckets as given, ``(blank)``, ``(unresolved id)``, ``Name (#2)``, ``[hidden]``; in a table cell ``|`` reads ``¦``
and a line break one space, and an empty value is an empty cell (``(no value)`` on a line). Under 2 or more groups
or buckets a Total line (``prompts/records_table_words.md``) only when the result holds the total of exactly the rows
shown and they add up to it; under a list cut by its limit, the 50-group cap or the result cap a "Showing k of n" line
from the result's own numbers, unless the model's text gives both. Code cuts nothing the tool returned:
:data:`TABLE_MAX_ROWS` is a safety net past which one number-free "…" row ends the block.

Placement (:func:`expand`): a table line on or after the last Source line moves just before it when that line ends
the text; blocks whose lines come before the first prose line go after the first prose paragraph, so the lead stays
first; any other block goes right after its own line (a table line inside a sentence leaves the sentence; a list
marker or a joining word left with table lines, ``1.`` or ``and``, goes with them). Each block gets one blank line
before and after it, and a lines block right after another block a number-free label line (``By Status:``, ``By
month:``, ``Compared:``) so that two lists do not read as one; a lines block written before any other text starts with
that label line too, when the lead checks skip it (``nudges._LABEL_LINE_RE``, no digit), so bare rows never open the
answer. :func:`restore` puts stored blocks back into a merged text the same way, and takes the same typed decision
(the merge call gets the record answer with its blocks as table lines: :func:`marked`,
``COMBINE_TABLES_NOTE``). Nothing here raises: a failure gives the text without its table lines
(:func:`strip_markers`).
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from sf_assistant import prompts as _prompt_files   # the texts of the code-built rows (prompts/records_table*.md ...)
from sf_assistant.config import LIMITS
from sf_assistant.agent.checks import (_DATE_TIME_RE, _LIST_ITEM_RE, _TOOL_WORD_RE, _count_numbers, _is_source_line,
                                       numbers_in)
from sf_assistant.agent.nudges import _LABEL_LINE_RE

__all__ = ["RECORDS_TABLES_NOTE", "COMBINE_TABLES_NOTE", "RECORDS_TABLES_MARKER_MESSAGE", "RECORDS_TABLE_WORDS",
           "TABLE_TOOLS", "TABLE_MAX_ROWS", "MARKER_RE", "LOOSE_RE", "Expansion", "column_labels", "render", "expand",
           "own_lines", "strip_markers", "gap_before_source", "holds", "marked", "restore", "marker_message",
           "answer_counts", "left_over", "line_count", "put_back", "RECORDS_LIST_NOTE", "list_rows"]

#: The record agent's note on the table lines, appended to its system prompt in code mode only (never inside
#: ``render_records_system``, which the MCP template answer_from_records also serves).
RECORDS_TABLES_NOTE = _prompt_files.text("records_tables_note")
#: The merge call's note, appended to ``COMBINE_SYSTEM`` when the record answer reaches it with table lines.
COMBINE_TABLES_NOTE = _prompt_files.text("combine_tables_note")
#: The first draft check's message for table lines code cannot fill (``<<QUERY_IDS>>``: :func:`marker_message`).
RECORDS_TABLES_MARKER_MESSAGE = _prompt_files.text("records_tables_marker_message")
RECORDS_TABLE_WORDS = _prompt_files.text("records_table_words")   # raw text; _WORDS is its dict
#: The record agent's note on a list question (development A of 2026-11-27, ``agent.display.asks_list``): appended to
#: its system prompt in RecordSession.ask whatever ``SFA_TABLES`` says, never inside ``render_records_system``.
RECORDS_LIST_NOTE = _prompt_files.text("records_list_note")
_WORDS = dict(ln.split(": ", 1) for ln in RECORDS_TABLE_WORDS.split("\n") if ": " in ln)

#: The tools whose results code writes as rows, and the key of each one's rows (count_records: its breakdown).
TABLE_TOOLS = ("list_records", "group_records", "trend_records", "compare_records", "count_records")
_ROWS_KEY = {"list_records": "rows", "group_records": "groups", "trend_records": "buckets",
             "compare_records": "segments"}
_LABEL_KEY = {"group_records": "group", "trend_records": "bucket", "compare_records": "label"}
#: The most rows code writes for one table line; read at call time. A safety net only: the tools cap their lists
#: at or below it (a list at most ``LIMITS.list_max_rows`` rows, 200 since development A of 2026-11-27; 50 groups, a
#: trend at most 120 periods, 4 segments, a breakdown 9 values). 120 until development A.
TABLE_MAX_ROWS = LIMITS.list_max_rows
#: A table line: ``{{table q3}}``; case, inner spaces, ":" or "=", a bare number and markdown wrappers read too.
MARKER_RE = re.compile(r"[*_`]{0,3}\{\{\s*table\s*[:=]?\s*q?0*(\d{1,4})\s*\}\}[*_`]{0,3}", re.IGNORECASE)
#: A span that only looks like a table line (malformed): removed, never filled. One or two braces on each side of a
#: table, tbl, list, rows, query or result word (``{table q3}``, ``{{table_q3}}``, ``{{tbl q3}``; review of 2026-10-30:
#: these near misses reached the user), or a query id in two braces (``{{q3}}``, ``{{q_3}}``).
LOOSE_RE = re.compile(r"\{\{?\s*(?:tables?|tbl|list|rows?|query|results?)(?![A-Za-z])[^{}\n]{0,80}\}\}?"
                      r"|\{\{\s*[qt][_-]?\d{1,4}\s*\}\}?", re.IGNORECASE)
#: What a final answer still holds of a table line once its patterns are removed: "{{" or "{table" (:func:`left_over`).
_LEFT_RE = re.compile(r"\{\{|\{\s*(?:table|tbl)", re.IGNORECASE)
#: What may stand between two table lines of one line and only joins them (", ", " and ", " & "): it goes with them.
_JOIN_RE = re.compile(r"[\s,;/&+]*(?:\b(?:and|or|then|plus)\b[\s,;/&+]*)?", re.IGNORECASE)
#: All that is left of a line once its table lines are removed is markup: marks and at most a list marker ("1.").
_MARKS_ONLY_RE = re.compile(r"[\s*_`>#\-•+]*(?:\d{1,3}[.)])?[\s*_`>#\-•+]*")
_FENCE_RE = re.compile(r"^\s*```")
#: A "showing k of n" note of group_records (the 50-group cap: "... groups") or of the result cap on the listed
#: rows (``RecordTools._fit``: nothing after n; its note on another list ends with that list's key: no cut of the rows).
_SHOWING_RE = re.compile(r"\bshowing (\d+) of (\d+)(?: groups)?$", re.IGNORECASE)
#: A header's catalog path without its " (name)" / " (Employee name)" suffix.
_SUFFIX_RE = re.compile(r"\s+\([^()]*\)$")
#: Room the seen-whole guard keeps for what the evidence does not hold of the tool message (its chunk ids).
_SEEN_ROOM = 2500
_WORD_CHAR = "A-Za-z0-9_"
_GONE = object()   # a line that held only table lines (layout)
#: The month names a ``YYYY-MM`` key may be written with (English literals: ``calendar.month_name`` follows the locale).
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December")
_MONTH_KEY_RE = re.compile(r"(\d{4})-(0[1-9]|1[0-2])")
#: What the model writes for the ``(blank)`` row.
_BLANK_WORDS = ("blank", "no value", "not set")
_JSON_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
#: What holds digits on a line but gives no row value: a year-month, a record code, a month and year ("2031-05",
#: "GO-00005", "05/2031"). :func:`numbers_in` reads 5 out of each of them.
_NOT_VALUE_RE = re.compile(r"\b\d{4}-\d{2}\b|\b[A-Za-z_]+-\d+\b|\b\d{1,2}/\d{4}\b")


@dataclass
class Expansion:
    """What :func:`expand` made of one reply: ``text`` with every table line replaced or removed; ``blocks`` (the
    canonical table line -> its rows as written, in placement order); ``sources`` (the blocks' text as one audit
    source: a stored line break reads as a space in a cell, as ``n15`` in the facts); ``bad`` (the ids and malformed
    spans the first draft check names, never logged); ``counts`` (ints, fixed words, query ids and tool names); ``typed``
    (the table line -> the whole block of each block dropped because the model typed it: :func:`put_back`)."""

    text: str
    blocks: dict[str, str] = field(default_factory=dict)
    sources: list[str] = field(default_factory=list)
    bad: list[str] = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    typed: dict[str, str] = field(default_factory=dict)


def _zero() -> dict:
    return {"markers": 0, "built": 0, "rows": 0, "dropped": {}, "partly_typed": 0, "items": []}


def _marker(qid: str) -> str:
    return "{{table " + qid + "}}"


# ------------------------------------------------------------------------------------------------ values and labels
def _cell(v: Any) -> str:
    """A table cell: empty for null, true/false, a number in its JSON form (2000.0 stays 2000.0), text on one line
    with ``|`` read as ``¦`` (the page's renderer has no escape)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return " ".join(s.split()).replace("|", "¦")


def _num(v: Any) -> str:
    """A value on a ``- label: value`` line: as :func:`_cell`, null as ``(no value)`` (never "none", a claim the
    graders read)."""
    if v is None:
        return _WORDS["no_value"]
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return " ".join(s.split())


def _label(v: Any) -> str:
    """A line's label: the text on one line (``|`` kept), ``(blank)`` for null or nothing."""
    s = " ".join((v if isinstance(v, str) else "" if v is None else _num(v)).split())
    return s or "(blank)"


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _has_word(text: str) -> bool:
    return re.search(r"[^\W_]", text or "") is not None


def _humanize(part: str) -> str:
    """An API name as words: no ``__c``/``__r``, no trailing ``Id``, ``_`` as a space, CamelCase split
    (``CreatedDate`` -> "Created Date", ``OwnerId`` -> "Owner")."""
    base = re.sub(r"__[cr]$", "", str(part or ""))
    if base.endswith("Id") and len(base) > 2:
        base = base[:-2]
    base = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", base.replace("_", " "))
    return " ".join(base.split()) or str(part or "")


def _column_label(catalog: Any, obj: Any, column: Any) -> str:
    path = _SUFFIX_RE.sub("", str(column or "")).strip()
    parts = [p for p in path.split(".") if p] or [path]
    objects = getattr(catalog, "objects", None) if catalog is not None else None
    objects = objects if isinstance(objects, dict) else {}
    spec = objects.get(obj) if obj else None
    f = None
    for i, part in enumerate(parts):   # walked by hand: resolve_path may read a name as its closest one
        fields = getattr(spec, "fields", None) if spec is not None else None
        f = fields.get(part) if isinstance(fields, dict) else None
        if i < len(parts) - 1:
            target = None
            if f is not None:
                try:
                    target = catalog.target_of(f)
                except Exception:  # noqa: BLE001 - a catalog without targets: the first one it holds
                    target = None
                target = target or next((t for t in getattr(f, "ref_targets", None) or [] if t in objects), None)
            spec = objects.get(target) if target else None
    last = parts[-1]
    label = " ".join(str(getattr(f, "label", "") or "").split()) if f is not None else ""
    if not label or label in (last, str(getattr(f, "api", "") or "")):
        label = _humanize(last)
    if (f is not None and getattr(f, "kind", "") == "reference") or last.endswith("Id"):
        label = re.sub(r"\s+ID$", "", label) or label
    label = label.replace("`", "").replace("|", "¦")
    if "__" in label or _TOOL_WORD_RE.search(label):   # an API name or a tool word is never a header
        label = _humanize(last)
    return " ".join(label.split()) or _humanize(last)


def column_labels(catalog: Any) -> Callable[[Any, Any], str]:
    """``labels(object, column) -> str``: a list column's header from the catalog (cached, never raises). The path
    loses its " (name)" suffix and is walked by hand (each hop's target, or the first ``ref_targets`` the catalog
    holds); the last field's label when it is no API name, else the humanized name; a lookup's trailing " ID" goes.
    Without a catalog (stub tools) every header is humanized. Headers are metadata, never record data."""
    cache: dict[tuple[str, str], str] = {}

    def labels(obj: Any, column: Any) -> str:
        key = (str(obj or ""), str(column or ""))
        if key not in cache:
            try:
                cache[key] = _column_label(catalog, obj, column)
            except Exception:  # noqa: BLE001 - a header never fails a table
                cache[key] = _humanize(_SUFFIX_RE.sub("", key[1]).split(".")[-1])
        return cache[key]

    return labels


# ------------------------------------------------------------------------------------------------ rows of one result
def _unfillable(e: Any) -> str | None:
    """Why the result ``e`` gives no rows (a drop reason), or None when code writes them."""
    if e is None:
        return "unknown"
    if not getattr(e, "ok", False):
        return "failed"
    tool, res = getattr(e, "tool", None), getattr(e, "model_result", None)
    if getattr(e, "kind", None) != "data" or tool not in TABLE_TOOLS or not isinstance(res, dict):
        return "no_rows"
    if tool == "count_records":
        bd = res.get("breakdown")
        if not (isinstance(bd, dict) and bd):
            return "no_rows"
    else:
        rows = res.get(_ROWS_KEY[tool])
        if not (isinstance(rows, list) and rows):
            return "empty"
    if not _seen_whole(res):
        return "unseen"
    return None


def _seen_whole(res: dict) -> bool:
    """The tool message that carried ``res`` (with room for its chunk ids) was within the cut the model's tool
    messages get (``LIMITS.knowledge_tool_result_chars``), so the model saw every row."""
    size = len(json.dumps({"ok": True, "result": res, "error": None, "chunk_ids": []}, ensure_ascii=False,
                          default=str))
    return size + _SEEN_ROOM <= LIMITS.knowledge_tool_result_chars


def _showing(shown: Any, total: Any, unit: str, said: Any) -> list[str]:
    """The "Showing k of n <unit>." line under a cut list (the "Not all <unit> are shown." line when ``shown`` is no
    number the result holds), after a blank line; none when the model's own text states both numbers (``said``:
    its counts, dates, times and Source lines aside, so a "10" of "synced 2026-10-13" says nothing)."""
    if _is_number(shown) and not isinstance(shown, float) and shown < total:
        if {str(shown), str(total)} <= set(said):
            return []
        return ["", _prompt_files.fill(_WORDS["shown"], SHOWN=str(shown), TOTAL=str(total), UNIT=unit)]
    return ["", _prompt_files.fill(_WORDS["not_all"], UNIT=unit)]


def _headers(obj: Any, columns: list, labels: Callable[[Any, Any], str]) -> list[str]:
    heads = [labels(obj, c) for c in columns]
    seen: set[str] = set()
    for i, h in enumerate(heads):
        parts = _SUFFIX_RE.sub("", str(columns[i])).split(".")
        if h.lower() in seen and len(parts) > 1:   # "Name" twice: the later one with its first hop ("Candidate Name")
            heads[i] = f"{labels(obj, parts[0])} {h}"
        elif h.lower() in seen:   # two fields with one label: the later one by its API name in words (review)
            own = _humanize(parts[0])
            heads[i] = own if "__" not in own and not _TOOL_WORD_RE.search(own) else h
        seen.add(heads[i].lower())
    return heads


def _table(res: dict, obj: Any, labels: Callable[[Any, Any], str], said: Any) -> tuple[list[str], int, str]:
    rows = res["rows"]
    columns = res.get("columns")
    if not (isinstance(columns, list) and columns):
        columns = list(rows[0])   # the result cap took the columns: the keys of the first row
    out = ["| " + " | ".join(_headers(obj, columns, labels)) + " |", "|" + "|".join("---" for _ in columns) + "|"]
    n = 0
    for row in rows:
        if n >= TABLE_MAX_ROWS:
            out.append("| " + " | ".join("…" for _ in columns) + " |")
            break
        out.append("| " + " | ".join(_cell(row.get(c)) for c in columns) + " |")
        n += 1
    total = res.get("total_count")
    if _is_number(total) and not isinstance(total, float) and total > len(rows):
        k = len(rows)
        shown = k if (k in (res.get("returned"), res.get("shown")) or k <= 12) else None
        out += _showing(shown, total, _WORDS["records"], said)
    return out, n, "table"


def _lines(res: dict, tool: str, said: Any) -> tuple[list[str], int, str]:
    out: list[str] = []
    values: list[Any] = []
    cut = False
    items = res[_ROWS_KEY[tool]]
    # a period the result marks partial (the first covers only part of its period, the last is still running) says so
    # on its own line, as the model's lines did (review of 2026-10-30); a fixed word, no number
    partial = {i for i, flag in ((0, "first_bucket_partial"), (len(items) - 1, "last_bucket_partial"))
               if tool == "trend_records" and res.get(flag) is True}
    for i, it in enumerate(items):
        if len(values) >= TABLE_MAX_ROWS:
            out.append("- …")
            cut = True
            break
        line = f"- {_label(it.get(_LABEL_KEY[tool]))}: {_num(it.get('value'))}"
        by = it.get("by")
        if by:   # the split, inline and in its stored order (a value the row lacks is left out, never 0)
            line += " (" + ", ".join(f"{_label(k)}: {_num(v)}" for k, v in by.items()) + ")"
        out.append(line + (" " + _WORDS["partial"] if i in partial else ""))
        values.append(it.get("value"))
    key = ("total_across_groups" if tool == "group_records" and res.get("measure") == "count"
           and "records_matched" not in res else "total" if tool == "trend_records" else None)
    total = res.get(key) if key else None
    # the total of exactly these rows, never a sum; not under one row, whose line it would only repeat (fidelity
    # preview of 2026-10-30: one group with a split gave its split twice)
    if (key in res and not cut and len(values) > 1 and all(_is_number(v) for v in values) and _is_number(total)
            and round(sum(values), 2) == round(float(total), 2)):
        line = f"- {_WORDS['total']}: {_num(total)}"
        st = res.get("split_totals")
        if isinstance(st, dict) and st:
            line += " (" + ", ".join(f"{_label(k)}: {_num(v)}" for k, v in st.items()) + ")"
        out.append(line)
    notes = res.get("notes") if tool in ("group_records", "trend_records") else None
    pairs = [(int(a), int(b)) for n in notes if isinstance(n, str) for a, b in _SHOWING_RE.findall(n.strip())] \
        if isinstance(notes, list) else []
    if pairs:   # the 50-group cap or the result cap
        out += _showing(res.get("shown"), max(b for _a, b in pairs),
                        _WORDS["groups" if tool == "group_records" else "periods"], said)
    return out, len(values), "lines"


def _breakdown(res: dict, obj: Any, labels: Callable[[Any, Any], str]) -> tuple[list[str], int, str]:
    out: list[str] = []
    n = 0
    several = len(res["breakdown"]) > 1
    for fld, by in res["breakdown"].items():
        if several:   # each field under its label line, a blank line between fields
            out += ([""] if out else []) + [f"{labels(obj, fld)}:"]
        for k, v in by.items():
            if n >= TABLE_MAX_ROWS:
                out.append("- …")
                return out, n, "lines"
            out.append(f"- {_label(k)}: {_num(v)}")
            n += 1
    return out, n, "lines"


def _head(e: Any, labels: Callable[[Any, Any], str]) -> str | None:
    """The number-free label line of the lines block of ``e`` for when it comes right after another block (review of
    2026-10-30: the page, like Markdown, reads two lists that only a blank line separates as one): "By <the grouped
    field's label>:", "By <grain>:", "By <the breakdown field's label>:", "Compared:"; None when the result names no
    field (a breakdown of several fields has its label lines already)."""
    res = e.model_result
    obj = res.get("object") or getattr(e, "object", None)
    what = None
    if e.tool == "compare_records":
        return _WORDS["compared"]
    if e.tool == "group_records" and isinstance(res.get("grouped_by"), str) and res["grouped_by"].strip():
        what = labels(obj, res["grouped_by"])
    elif e.tool == "trend_records" and isinstance(res.get("grain"), str):
        what = " ".join(res["grain"].split())
    elif e.tool == "count_records" and len(res.get("breakdown") or {}) == 1:
        what = labels(obj, next(iter(res["breakdown"])))
    return _prompt_files.fill(_WORDS["by"], LABEL=what) if what else None


def _render(e: Any, labels: Callable[[Any, Any], str], said: Any = ()) -> tuple[list[str], int, str]:
    """The rows of the fillable result ``e`` as lines, their number and their form ("table" or "lines"). Raises on a
    malformed result (:func:`expand` then falls back)."""
    res = e.model_result
    obj = res.get("object") or getattr(e, "object", None)
    if e.tool == "list_records":
        return _table(_all_rows(e, res), obj, labels, said)
    if e.tool == "count_records":
        return _breakdown(res, obj, labels)
    return _lines(res, e.tool, said)


def _all_rows(e: Any, res: dict) -> dict:
    """A list_records result with every row of its list (development A of 2026-11-27): ``QueryEvidence.full_rows``, the
    rows before the result cap cut what the model read (the same cells and columns), in place of the cut ``rows``;
    ``res`` itself when nothing was cut."""
    full = getattr(e, "full_rows", None)
    rows = res.get("rows")
    if isinstance(full, list) and isinstance(rows, list) and len(full) > len(rows) and full[:len(rows)] == rows:
        return {**res, "rows": full}
    return res


def render(e: Any, labels: Callable[[Any, Any], str] | None = None, said: Any = ()) -> tuple[str, int, str] | None:
    """``(block text, rows, form)`` code writes for the record result ``e`` (a ``QueryEvidence``), or None when it
    gives no rows (:func:`_unfillable`) or is malformed. ``said``: the numbers of the model's own text (no "Showing"
    line when it gives both). Never raises."""
    try:
        if _unfillable(e) is not None:
            return None
        lines, n, form = _render(e, labels or column_labels(None), set(said))
        return "\n".join(lines), n, form
    except Exception:  # noqa: BLE001 - no rows rather than a failure
        return None


# ------------------------------------------------------------------------------------------------ table lines
def _matches(line: str) -> list[tuple[int, int, str, str]]:
    """``(start, end, kind, value)`` of the table lines of ``line`` (kind "marker", value the canonical line) and
    of its malformed spans that overlap none of them (kind "loose", value the inner text), in order."""
    found = [(m.start(), m.end(), "marker", _marker(f"q{int(m.group(1))}")) for m in MARKER_RE.finditer(line)]
    for m in LOOSE_RE.finditer(line):
        if not any(m.start() < end and start < m.end() for start, end, _k, _v in found):
            found.append((m.start(), m.end(), "loose", " ".join(m.group(0).strip("{} \t").split())))   # no braces
    return sorted(found)


def _cut(line: str, spans: list) -> str:
    for start, end, _k, _v in reversed(spans):
        line = line[:start] + line[end:]
    return line


def _clean(line: str, spans: list) -> str | None:
    """``line`` without its spans and what only joins two of them (", ", " and "), leftover "()" and "[]", double
    spaces and a space before a punctuation mark; None when only markup is left (no letter or digit, or a list marker
    alone: a bold, backticked, bulleted, numbered or heading table line goes whole)."""
    indent = re.match(r"[ \t]*", line).group(0)
    parts, pos = [], 0
    for k, (start, end, _k, _v) in enumerate(spans):
        if not (k and _JOIN_RE.fullmatch(line[pos:start])):   # "{{table q1}} and {{table q2}}": "and" goes too
            parts.append(line[pos:start])
        pos = end
    tail = line[pos:]
    if re.fullmatch(r"[\s.,;:!?]*", tail) and "".join(parts).rstrip()[-1:] in (":", ",", ";"):
        tail = ""   # "Tables: {{table q1}}." keeps "Tables:", not "Tables:."
    body = re.sub(r"\(\s*\)|\[\s*\]", "", "".join(parts + [tail]).strip())
    body = re.sub(r"[ \t]+([.,;:!?])", r"\1", re.sub(r"[ \t]{2,}", " ", body)).strip()
    return indent + body if _has_word(body) and not _MARKS_ONLY_RE.fullmatch(body) else None


def _only_marks(line: str) -> bool:
    spans = _matches(line)
    return bool(spans) and _clean(line, spans) is None


def _drop_fences(lines: list[str]) -> list[str]:
    """``lines`` without a fenced code pair (```) whose inner lines are all table lines or blank."""
    out = list(lines)
    i = 0
    while i < len(out):
        if not _FENCE_RE.match(out[i]):
            i += 1
            continue
        j = next((k for k in range(i + 1, len(out)) if _FENCE_RE.match(out[k])), None)
        if j is None:
            break
        inner = out[i + 1:j]
        if any(_only_marks(x) for x in inner) and all(not x.strip() or _only_marks(x) for x in inner):
            del out[j], out[i]
            i = j - 1
        else:
            i = j + 1
    return out


def _splits(rest: str) -> list[tuple[str, str]]:
    """The ``(label, value)`` pairs of a split, from the text after a label ("5 (Full Time: 4, Contract: 1) (partial)");
    none without a split. A label may hold ", " or ": "."""
    _value, sep, inner = rest.removesuffix(" " + _WORDS["partial"]).partition(" (")
    if not (sep and inner.endswith(")")):
        return []
    value = r"-?\d+(?:\.\d+)?|" + re.escape(_WORDS["no_value"])
    return re.findall(r"(?:^|, )(.+?): (" + value + r")(?=, |$)", inner[:-1])


def _block_rows(block: str) -> list[tuple[str, list[str], list[tuple[str, str]]]]:
    """``(key, values, split)`` of each row of a block: a table row's first cell, its other non-empty cells and no split;
    a line's label, its value and the ``(label, number)`` pairs of its split (the Total line, the "Showing" line and the
    "…" row are no rows)."""
    lines = (block or "").split("\n")
    out: list[tuple[str, list[str], list[tuple[str, str]]]] = []
    if lines and lines[0].startswith("|"):
        for ln in lines[2:]:
            if not ln.startswith("|"):
                break
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if cells and not all(c == "…" for c in cells):
                out.append((cells[0], [c for c in cells[1:] if c], []))
        return out
    items = [ln for ln in lines if ln.startswith("- ") and ln != "- …"]
    if items and items[-1].startswith(f"- {_WORDS['total']}: "):
        items = items[:-1]
    for ln in items:
        key, sep, rest = ln[2:].partition(": ")
        if sep:
            out.append((key, [rest.split(" (", 1)[0]], [(k, v) for k, v in _splits(rest) if _JSON_NUMBER_RE.fullmatch(v)]))
    return out


def _word(text: str) -> re.Pattern:
    return re.compile(f"(?<![{_WORD_CHAR}])" + re.escape(text) + f"(?![{_WORD_CHAR}])", re.IGNORECASE)


def _key_re(key: str) -> re.Pattern:
    """Where a line writes a row's key: the key as a whole word anywhere; a ``YYYY-MM`` key also "June 2026" or "Jun
    2026" and ``(blank)`` also "blank", "no value" or "not set", only first on the line, where a row line writes its
    key (never a bare month: "May" clashes with the verb and case is ignored; a caveat or a dated record further on
    types no row)."""
    month = _MONTH_KEY_RE.fullmatch(key)
    name = _MONTHS[int(month.group(2)) - 1] if month else ""
    forms = ((f"{name} {month.group(1)}", f"{name[:3]} {month.group(1)}") if month
             else _BLANK_WORDS if key == "(blank)" else ())
    pattern = f"(?<![{_WORD_CHAR}])" + re.escape(key) + f"(?![{_WORD_CHAR}])"
    if forms:
        pattern += f"|^[^{_WORD_CHAR}]*(?:" + "|".join(re.escape(f) for f in forms) + f")(?![{_WORD_CHAR}])"
    return re.compile(pattern, re.IGNORECASE)


def _line_numbers(line: str, key: re.Pattern | None = None) -> list[str]:
    """The numbers of ``line`` a row value can be: :func:`numbers_in` without its dates and times, then its ``key``, then
    its year-months and codes (:data:`_NOT_VALUE_RE`): the day of a date or the digits of a code are no value."""
    text = _DATE_TIME_RE.sub(" ", line)
    return numbers_in(_NOT_VALUE_RE.sub(" ", key.sub(" ", text) if key is not None else text))


def _has_value(value: str, line: str, numbers: list[str]) -> bool:
    """``line`` holds ``value``. A non-negative JSON number in the forms :func:`numbers_in` reads ("$1,234.00", "1,234"
    and "1234.0" are one number; ``numbers``: :func:`_line_numbers` of the line); any other value as a whole word (a
    negative number with its sign)."""
    if _JSON_NUMBER_RE.fullmatch(value) and not value.startswith("-"):
        return numbers_in(value)[0] in numbers
    return bool(_word(value).search(line))


def _holds_row(values: list[str], splits: list[tuple[str, str]], rest: str, numbers: list[str]) -> bool:
    """``rest`` (a line without the row's key; ``numbers``: its numbers, :func:`_line_numbers`) types the row. No split:
    one of the row's values (or the row has none). A split: every value of it, each in a place of its own, and the
    row's value too or a split that adds up to it: a line with only the row's total or half of its split must not
    lose the rest with the block. A split of one value equal to the row's value also needs its label or the number
    twice: one number alone may be only the total, and the label would be lost."""
    has_value = not values or any(_has_value(v, rest, numbers) for v in values if v)
    if not splits:
        return has_value
    wanted = [numbers_in(v)[0] for _label, v in splits]
    if Counter(wanted) - Counter(numbers):
        return False
    if len(splits) == 1 and numbers_in(values[0])[:1] == wanted:
        return bool(_key_re(splits[0][0]).search(rest)) or numbers.count(wanted[0]) >= 2
    if has_value:
        return True
    try:
        return abs(sum(float(v) for _label, v in splits) - float(values[0])) <= 0.01 * len(splits)
    except (ValueError, IndexError):
        return False


def _whole_row(rows: list[tuple[str, list[str], list[tuple[str, str]]]], line: str) -> int | None:
    """The list row a line that names no row's key types: the one row whose other cells, at least 3, are all on the
    line (a line of cells several rows share types none; a line with fewer cells than its row may be a summary, and
    the table holds more than it says)."""
    numbers = _line_numbers(line)
    fits = [i for i, (_k, values, _s) in enumerate(rows)
            if len(values) >= 3 and all(_has_value(c, line, numbers) for c in values)]
    return fits[0] if len(fits) == 1 else None


def _typed(block: str, items: list[str]) -> tuple[int, int]:
    """The typed-copy rule: how many of ``block``'s rows the list-item or table lines ``items`` of the other text
    hold, and how many rows it has. A row is typed by a line that holds its key (:func:`_key_re`), its value or whole
    split (:func:`_holds_row`) and no key of another row: one row per line, so a bullet that names the top groups ("-
    Engineering (20), Sales (12) and HR (8) lead") types none (review of 2026-10-30: such a bullet counted as a copy of
    every row and the rows it left out were lost). A list row is also typed by the line that names no key but fits it
    whole (:func:`_whole_row`). A copy (:func:`_copy`) is every row typed."""
    rows = _block_rows(block)
    if not rows or not items:
        return 0, len(rows)
    keys = [_key_re(k) if k else None for k, _v, _s in rows]
    done = [False] * len(rows)
    for ln in items:
        hits = [i for i, k in enumerate(keys) if k is not None and k.search(ln)]
        found = {rows[i][0].lower() for i in hits}
        found = {k for k in found if not any(k != o and k in o for o in found)}   # "Open" inside "Open - Hold"
        hits = [i for i in hits if rows[i][0].lower() in found]
        if not found and block.startswith("|"):   # a list row keyed on another column
            row = _whole_row(rows, ln)
            if row is not None:
                done[row] = True
        if len(found) != 1:   # no key, or the keys of several rows: no row of its own
            continue
        for i in hits:
            numbers = _line_numbers(ln, keys[i])
            if not done[i] and _holds_row(rows[i][1], rows[i][2], keys[i].sub(" ", ln), numbers):
                done[i] = True
                break
    return sum(done), len(rows)


def _total_left(block: str, own: list[str]) -> str | None:
    """The Total line of a block the model typed (:func:`_copy`), when it holds split totals that no line of the
    model's own text ``own`` gives: the rows are typed, these totals are nowhere else. None otherwise."""
    start = f"- {_WORDS['total']}: "
    line = next((ln for ln in reversed((block or "").split("\n")) if ln.startswith(start)), None)
    for label, value in _splits(line[len(start):]) if line else []:
        key = _key_re(label)
        if not any(key.search(ln) and _has_value(value, key.sub(" ", ln), _line_numbers(ln, key)) for ln in own):
            return line
    return None


def _copy(typed: int, rows: int) -> bool:
    """The other text holds a copy of a block (:func:`_typed`): every one of its rows, and at least 2."""
    return rows >= 2 and typed == rows


def _layout(lines: list[str], spans: list[list], placed: list[tuple[int, str, str]], extra: list[tuple[str, str]],
            heads: dict[str, str] | None = None) -> tuple[str, dict[str, str]]:
    """``lines`` with their spans removed (:func:`_clean`) and the blocks put in: ``placed`` (``(i, table line,
    block)``: the block of a table line on line i) on or after the last Source line goes just before it when that line
    ends the text (only blank lines and table lines after it; review of 2026-10-30: a Source line written first kept
    the rows above the lead), one before the first prose line after the first prose paragraph, any other right after
    its own line; ``extra`` (``(table line, block)``) just before that Source line, or at the end. One blank line
    before and after each block; a lines block right after another block starts with its label line from ``heads``
    (table line -> label), when it has one, and so does one written before any other text when ``nudges._lead`` would
    skip that label (only :func:`_expand` passes ``heads``). Other spacing is kept. Returns the text and each table
    line's block as written in it."""
    n = len(lines)
    cleaned = [ln if not sp else _clean(ln, sp) for ln, sp in zip(lines, spans)]
    src = max((i for i, ln in enumerate(lines) if _is_source_line(ln)), default=n)
    if any(cleaned[i] is not None and cleaned[i].strip() for i in range(src + 1, n)):
        src = n   # the Source line does not end the text: the blocks stay with the text around them
    prose = next((i for i in range(src) if cleaned[i] is not None and _has_word(cleaned[i])
                  and not _LIST_ITEM_RE.match(cleaned[i]) and not _is_source_line(cleaned[i])), None)
    end = prose
    if prose is not None:   # the first prose paragraph: its lines up to a blank, list, table or Source line
        while (end + 1 < src and not spans[end + 1] and cleaned[end + 1].strip()
               and not _LIST_ITEM_RE.match(cleaned[end + 1])):
            end += 1
    after: dict[int, list[tuple[str, str]]] = {}
    before: list[tuple[str, str]] = []
    for i, key, block in placed:
        if i >= src:
            before.append((key, block))
        elif prose is not None and i < prose:   # a row list never leads: the lead is read first
            after.setdefault(end, []).append((key, block))
        else:
            after.setdefault(i, []).append((key, block))
    before += list(extra)
    items: list[Any] = []
    for i in range(n):
        if i == src:
            items += [("block", kb) for kb in before]
        items.append(_GONE if cleaned[i] is None else cleaned[i])
        items += [("block", kb) for kb in after.get(i, [])]
    if src == n:
        items += [("block", kb) for kb in before]
    out: list[str] = []
    written: dict[str, str] = {}
    gap = skip = False   # gap: a block was just written; skip: a removed line, with a blank line before it
    for it in items:
        if isinstance(it, tuple):
            key, block = it[1]
            head = (heads or {}).get(key)
            # two lists in a row would read as one; bare rows never open the answer, when _lead skips the label line
            if head and block.startswith("- ") and (gap or (not any(x.strip() for x in out)
                                                            and _LABEL_LINE_RE.match(head))):
                block = head + "\n" + block
            while out and not out[-1].strip():
                out.pop()
            out += ([""] if out else []) + [block]
            written[key] = block
            gap, skip = True, False
        elif it is _GONE:
            skip = gap or not out or not out[-1].strip()
        elif not it.strip():
            if not (gap or skip):
                out.append(it)
        else:
            out += ([""] if gap else []) + [it]
            gap = skip = False
    while out and not out[-1].strip():
        out.pop()
    while out and not out[0].strip():
        out.pop(0)
    return "\n".join(out), written


def expand(text: str, evidence: Any, labels: Callable[[Any, Any], str] | None = None) -> Expansion:
    """``text`` (a record agent reply or draft) with each table line replaced by the rows of its result, placed as
    the module text says, and the lines code cannot fill removed and counted. Fast path: a text without "{" (or whose
    braces hold no table line) is returned as the same object. Never raises: on an internal error the text without its
    table lines (:func:`strip_markers`) and ``counts`` {"error": <exception class name>}."""
    if not text or "{" not in text:
        return Expansion(text=text, counts=_zero())
    try:
        return _expand(text, evidence, labels or column_labels(None))
    except Exception as exc:  # noqa: BLE001 - the text without its table lines rather than a failure
        return Expansion(text=strip_markers(text), counts={"error": type(exc).__name__})


def _expand(text: str, evidence: Any, labels: Callable[[Any, Any], str]) -> Expansion:
    lines = _drop_fences(text.split("\n"))
    spans = [_matches(ln) for ln in lines]
    if not any(spans):   # braces, but no table line: as the fast path
        return Expansion(text=text, counts=_zero())
    other = [_cut(ln, sp) for ln, sp in zip(lines, spans)]   # the model's own text
    said = _count_numbers("\n".join(other))   # the counts it states (no Source line, date or time)
    items = [ln for ln in other if _LIST_ITEM_RE.match(ln)]
    by_id: dict[str, Any] = {}
    for e in evidence or []:
        by_id.setdefault(str(getattr(e, "query_id", "")), e)
    blocks: dict[str, str] = {}
    placed: list[tuple[int, str, str]] = []
    heads: dict[str, str] = {}
    dropped: dict[str, int] = {}
    bad: list[str] = []
    built: list[dict] = []
    totals: list[tuple[str, str]] = []   # the Total lines left of blocks the model typed (:func:`_total_left`)
    no_rows: dict[str, int] = {}   # the no_rows drops by tool name
    copies: dict[str, str] = {}   # the whole blocks dropped because the model typed them (:func:`put_back`)
    markers = rows = partly = 0
    for i, line_spans in enumerate(spans):
        for _s, _e, kind, value in line_spans:
            markers += 1
            reason = "malformed"
            if kind == "loose":
                bad.append(value)
            elif value in blocks:
                reason = "repeated"
            else:
                qid = value[len("{{table "):-2]
                e = by_id.get(qid)
                reason = _unfillable(e)
                if reason is None:
                    out, n, form = _render(e, labels, said)
                    block = "\n".join(out)
                    typed, total = _typed(block, items)
                    if not _copy(typed, total):   # rows typed only in part: the rows are filled all the same
                        blocks[value] = block
                        placed.append((i, value, block))
                        head = _head(e, labels) if form == "lines" else None
                        if head:
                            heads[value] = head
                        rows += n
                        partly += 1 if typed else 0
                        built.append({"query_id": qid, "tool": e.tool, "rows": n, "form": form})
                        continue
                    reason = "typed"   # the model typed every row too: its own copy stays
                    copies[value] = block
                    left = _total_left(block, other)
                    if left and all(k != value for k, _b in totals):
                        totals.append((value, left))
                        head = _head(e, labels) if form == "lines" else None
                        if head:   # right after another block it must not read as that block's total
                            heads[value] = head
                elif reason in ("unknown", "failed", "no_rows", "empty"):
                    bad.append(qid)
                    if reason == "no_rows":
                        no_rows[str(e.tool)] = no_rows.get(str(e.tool), 0) + 1
            dropped[reason] = dropped.get(reason, 0) + 1
    text, blocks = _layout(lines, spans, placed, totals, heads)
    counts = {"markers": markers, "built": len(built), "rows": rows, "dropped": dropped, "partly_typed": partly,
              "items": built}
    if no_rows:
        counts["no_rows_by"] = no_rows
    return Expansion(text=text, blocks=blocks, sources=["\n\n".join(blocks.values())] if blocks else [], bad=bad,
                     counts=counts, typed=copies)


def own_lines(text: str) -> str:
    """``text`` with each table line that shares its line with other text on a line of its own right after it (the
    rest of the line as :func:`expand` keeps it): the record session runs this before the reply's cleaners, so a
    cleaner that drops a planning sentence ("Let me list them: {{table q1}}") never drops its table line (review of
    2026-10-30). Unchanged (the same object) when there is nothing to move. Never raises."""
    try:
        if not text or "{" not in text:
            return text
        lines = text.split("\n")
        out: list[str] = []
        moved = False
        for ln in lines:
            spans = _matches(ln)
            rest = _clean(ln, spans) if spans else None
            if rest is None:   # no table line, or nothing but table lines and markup: as it is
                out.append(ln)
                continue
            out += [rest] + [ln[s:e] for s, e, _k, _v in spans]
            moved = True
        return "\n".join(out) if moved else text
    except Exception:  # noqa: BLE001 - the text as it is
        return text


def strip_markers(text: str) -> str:
    """``text`` without its table lines and malformed spans (also out of a code fence that held nothing else); a line
    left with only markup goes, and blank runs collapse. Unchanged (the same object) when it holds none. Never
    raises."""
    try:
        if not text or "{" not in text:
            return text
        lines = _drop_fences(text.split("\n"))
        spans = [_matches(ln) for ln in lines]
        if not any(spans):
            return text
        kept = [ln if not sp else _clean(ln, sp) for ln, sp in zip(lines, spans)]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(ln for ln in kept if ln is not None)).strip("\n")
    except Exception:  # noqa: BLE001 - the last resort: the spans alone
        try:
            return LOOSE_RE.sub("", MARKER_RE.sub("", str(text)))
        except Exception:  # noqa: BLE001
            return text


def left_over(text: str) -> int:
    """How many "{{" or "{table" a final answer still holds once its table lines are removed (a near miss of the table
    line no pattern reads, or a stored value with braces): the night run sees misses in ``code_tables`` (review of
    2026-10-30). Never raises (0 on an error)."""
    try:
        return len(_LEFT_RE.findall(text or ""))
    except Exception:  # noqa: BLE001
        return 0


def gap_before_source(text: str, blocks: dict[str, str]) -> str:
    """``text`` with a blank line between a block and the last Source line when the block's last line comes right
    before it (``with_source_line`` adds the Source line without one). Idempotent; never raises."""
    try:
        if not text or not blocks:
            return text
        lines = text.split("\n")
        src = max((i for i, ln in enumerate(lines) if _is_source_line(ln)), default=0)
        if src == 0 or not lines[src - 1].strip():
            return text
        for block in blocks.values():
            bl = block.split("\n")
            if len(bl) <= src and lines[src - len(bl):src] == bl:
                return "\n".join(lines[:src] + [""] + lines[src:])
        return text
    except Exception:  # noqa: BLE001
        return text


def holds(text: str, block: str) -> bool:
    """``text`` holds ``block`` whole, as lines of their own."""
    try:
        return ("\n" + block + "\n") in ("\n" + (text or "") + "\n")
    except Exception:  # noqa: BLE001
        return False


def marked(text: str, blocks: dict[str, str]) -> str:
    """``text`` with each block (whole lines, in order) replaced by its table line: the record answer as the merge
    call gets it. "" when a block is not there whole. Never raises."""
    try:
        out = "\n" + (text or "") + "\n"
        for marker, block in blocks.items():
            k = out.find("\n" + block + "\n")
            if k < 0:
                return ""
            out = out[:k] + "\n" + marker + "\n" + out[k + len(block) + 2:]
        return out[1:-1]
    except Exception:  # noqa: BLE001
        return ""


def _own_label(lines: list[str], i: int, block: str) -> int | None:
    """The line above table line ``i`` when it reads as the label line ``block`` starts with ("By Tier:", marks and case
    aside): the merge call wrote what code stored. None otherwise."""
    head = block.split("\n", 1)[0]
    if not head.strip() or head.startswith(("- ", "|")):
        return None
    k = next((j for j in range(i - 1, -1, -1) if lines[j].strip()), None)
    bare = [re.sub(r"[\W_]+", "", x).lower() for x in (head, lines[k] if k is not None else "")]
    return k if bare[0] == bare[1] else None


def restore(text: str, blocks: dict[str, str]) -> tuple[str, dict] | None:
    """A merged ``text`` with the stored ``blocks`` (table line -> rows) put back: the first table line of a block
    gets it (placed as :func:`expand` places it, also out of a code fence that holds nothing else), a later one goes
    (``repeated``); a block the merged text typed itself, every row on a line of its own (:func:`_typed`), is not put
    back and its lines go (``retyped``), but for a Total line with split totals the merged text does not give
    (:func:`_total_left`), which goes just before the last Source line, or at the end; an unknown table line and a
    malformed span go; a block with neither a line nor a typed copy goes just before the last Source line, or at the
    end (``lost``). A block the merged text typed in part is put back all the same (``partly_retyped``: a value or two
    twice rather than rows lost). A stored Total line alone is an ordinary block (``lines`` or ``lost``). A line of the
    merged text right above a table line that repeats the block's label line goes (:func:`_own_label`). Returns
    ``(text, {"lines", "lost", "retyped", "repeated", "partly_retyped"})``, or None on an internal error."""
    try:
        lines = _drop_fences((text or "").split("\n"))
        spans = [_matches(ln) for ln in lines]
        own = [_cut(x, sp) for x, sp in zip(lines, spans)]
        items = [ln for ln in own if _LIST_ITEM_RE.match(ln)]
        typed = {m: _typed(b, items) for m, b in blocks.items()}
        copies = {m for m, (k, n) in typed.items() if _copy(k, n)}
        totals = [(m, t) for m, t in ((m, _total_left(blocks[m], own)) for m in blocks if m in copies) if t]
        placed: list[tuple[int, str, str]] = []
        done: set[str] = set()
        counts = {"lines": 0, "lost": 0, "retyped": len(copies), "repeated": 0,
                  "partly_retyped": sum(1 for m, (k, _n) in typed.items() if k and m not in copies)}
        for i, line_spans in enumerate(spans):
            for _s, _e, kind, value in line_spans:
                if kind != "marker" or value not in blocks or value in copies:
                    continue
                if value in done:
                    counts["repeated"] += 1
                    continue
                done.add(value)
                placed.append((i, value, blocks[value]))
                counts["lines"] += 1
                label = _own_label(lines, i, blocks[value])
                if label is not None:
                    lines[label], spans[label] = "", []
        lost = [(m, b) for m, b in blocks.items() if m not in done and m not in copies]
        counts["lost"] = len(lost)
        return gap_before_source(_layout(lines, spans, placed, lost + totals)[0], blocks), counts
    except Exception:  # noqa: BLE001
        return None


def put_back(text: str, x: Expansion) -> str:
    """``text`` (an answer after the checks that cut lines) with the rows back of each block dropped as typed
    (``x.typed``) when a check cut a line of the model's copy: the block went for that copy, so the row would be nowhere.
    They go where its kept Total line stood, or just before the last Source line, or at the end (:func:`restore`).
    ``x.blocks`` and ``x.sources`` take them in and ``x.counts["put_back"]`` counts them. The same object when every copy
    is whole, so nothing changes unless a check cut a line. Never raises."""
    try:
        if not x.typed:
            return text
        own, stored = marked(text, x.blocks), x.blocks
        if not own:   # a block is no longer there whole: the text as it is, and only the rows that come back
            own, stored = text, {}
        items = [ln for ln in own.split("\n") if _LIST_ITEM_RE.match(ln)]
        gone = {m: b for m, b in x.typed.items() if not _copy(*_typed(b, items))}
        back = restore(own, {**stored, **gone}) if gone else None
        if back is None:
            return text
        x.blocks.update(gone)
        x.sources = ["\n\n".join(x.blocks.values())]
        x.counts["put_back"] = len(gone)
        return back[0]
    except Exception:  # noqa: BLE001 - the text as it is
        return text


def marker_message(bad: list[str]) -> str:
    """The first draft check's message naming the table lines code cannot fill (``bad``: ids and malformed spans,
    no braces), at most 200 characters of them. Never raises ("" on an error)."""
    try:
        return _prompt_files.fill(RECORDS_TABLES_MARKER_MESSAGE,
                                  QUERY_IDS=", ".join(dict.fromkeys(str(b) for b in bad or []))[:200])
    except Exception:  # noqa: BLE001
        return ""


def line_count(text: str | None) -> int:
    """How many table lines and malformed spans ``text`` holds, counted as :func:`expand` counts ``markers``. Never
    raises (0 on an error)."""
    try:
        return sum(len(_matches(ln)) for ln in str(text or "").split("\n"))
    except Exception:  # noqa: BLE001
        return 0


def answer_counts(x: Expansion | None, answer: str, blocks: dict[str, str], nudged: int, cut: int = 0) -> dict:
    """``x.counts`` with ``kept`` (the blocks ``answer`` holds whole), ``chars`` (their summed length: what code wrote
    into the record answer), ``cut`` (the table lines ``strip_reasoning`` removed before expand saw them) and ``nudged``
    (1 when the first draft was asked to fix its table lines): the record answer's ``code_tables``. Never raises."""
    try:
        out = dict(x.counts) if x is not None and isinstance(x.counts, dict) else {}
        kept = [b for b in (blocks or {}).values() if holds(answer, b)]
        out["kept"] = len(kept)
        out["chars"] = sum(len(b) for b in kept)
        out["cut"] = cut
        out["nudged"] = 1 if nudged else 0
        return out
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------------------------------------------ long lists
# Development A of 2026-11-27 (the owner's choice): a question that asks to list records gets the whole list written by
# code from the list_records rows (every row of the call, also those the result cap kept from the model), whatever
# SFA_TABLES says; the model writes the lead and a table line (RECORDS_LIST_NOTE).
_TABLE_HEAD_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|[\s:|-]*-[\s:|-]*\|\s*$")


def _list_block_ids(x: Expansion) -> set[str]:
    """The table lines of ``x`` whose blocks are list_records rows (its ``counts["items"]``)."""
    items = x.counts.get("items") if isinstance(x.counts, dict) else None
    return {_marker(str(it.get("query_id"))) for it in items or [] if isinstance(it, dict)
            and it.get("tool") == "list_records"} & set(x.blocks)


def _block_lines(lines: list[str], blocks: list[str]) -> set[int]:
    """The indexes of ``lines`` that belong to one of ``blocks`` (each written whole, as lines of its own)."""
    inside: set[int] = set()
    for block in blocks:
        bl = block.split("\n")
        for k in range(len(lines) - len(bl) + 1):
            if lines[k:k + len(bl)] == bl and not inside & set(range(k, k + len(bl))):
                inside |= set(range(k, k + len(bl)))
                break
    return inside


def _row_keys(block: str) -> list[str]:
    """The key of each row of a block (a table row's first cell, a line's label; :func:`_block_rows`), 2 or more
    characters, no ``(blank)``, longest first."""
    keys = {k for k, _v, _s in _block_rows(block) if len(k.strip()) >= 2 and k != "(blank)"}
    return sorted(keys, key=lambda k: (-len(k), k))


#: Where a typed list item's label ends: the first ":", spaced dash, bracket, comma, semicolon or bar
#: ("- Person A — Recruiter").
_LABEL_END_RE = re.compile(r"\s*(?::|\s[\u2014\u2013-]\s|\(|,|;|\|)")
_MARKUP_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)|[*_`]")
_ITEM_MARK_RE = re.compile(r"^\s*(?:[-*\u2022+]|\d+[.)])\s+")
_GROUP_REST_RE = re.compile(r"[\W\d_]+|\b(?:and|or)\b", re.IGNORECASE)


def _plain(text: str) -> str:
    """``text`` without markdown emphasis, code ticks and link targets, single spaces, lower case."""
    return " ".join(_MARKUP_RE.sub(lambda m: m.group(1) or "", text).split()).lower()


def _line_key(line: str) -> str:
    """The key a typed line leads with, as :func:`_block_rows` keys code's rows: a table row's first cell that is
    not a bare number (a "#" column), a list item's label (its words up to the first ":", dash, bracket, comma or
    bar). Plain and lower case (:func:`_plain`); "" when there is none."""
    s = line.strip()
    if s.startswith("|"):
        for cell in s.strip("|").split("|"):
            c = _plain(cell)
            if c and not re.fullmatch(r"[\d.#)]+", c):
                return c
        return ""
    s = _ITEM_MARK_RE.sub("", s, count=1)
    m = re.match(r"\s*(\*\*|__)(.+?)\1", s)   # a bold label: its words, whatever separator follows
    if m:
        return _plain(m.group(2))
    return _plain(_LABEL_END_RE.split(s, maxsplit=1)[0])


def _drop_typed_rows(text: str, keep: list[str], lists: list[str]) -> tuple[str, int]:
    """``text`` without the model's own copy of a list code wrote: its list-item and table lines (outside the blocks
    ``keep``) that are a row of a block of ``lists``, and a table header and rule line the dropped rows leave alone.
    A line is a row when it leads with the key of a row (:func:`_line_key`: a table row's first cell, an item's label;
    a manager or team-lead column naming other listed rows does not change that) or names the key of exactly one row
    (a name, a record number: code's list holds that row). A group line stays: one that leads with a key and then
    names only 2 or more other keys (with numbers and "and"), as "- Person B: Person C, Person D" (in a table row, one
    cell holds those keys; a row with one name per cell is a copy). When most body rows
    of one of the model's tables went, its other rows that name a key go too (the copy is the model's whole table).
    Prose, a line that names no key (a total, a count by status) and a line that names several keys without leading
    with one stay. Returns the text and how many lines went."""
    keys = [k for block in lists for k in _row_keys(block)]
    if not keys:
        return text, 0
    rx = re.compile(f"(?<![{_WORD_CHAR}])(?:" + "|".join(re.escape(k) for k in dict.fromkeys(keys))
                    + f")(?![{_WORD_CHAR}])", re.IGNORECASE)
    plain_keys = {_plain(k) for k in keys}
    lines = text.split("\n")
    inside = _block_lines(lines, keep)

    def named(ln: str) -> set[str]:
        found = {m.group(0).lower() for m in rx.finditer(ln)}
        return {k for k in found if not any(k != o and k in o for o in found)}   # "Person 1" inside "Person 10"

    def group_line(ln: str, lead: str, found: set[str]) -> bool:
        others = {k for k in found if _plain(k) != lead}
        if len(others) < 2:
            return False
        if ln.lstrip().startswith("|"):   # a table row: one cell holds the group (two or more names), not one each
            if not any(len(named(c)) >= 2 for c in ln.strip().strip("|").split("|")[1:]):
                return False
        rest = _ITEM_MARK_RE.sub("", ln.strip().strip("|"), count=1)
        rest = _GROUP_REST_RE.sub(" ", _MARKUP_RE.sub(lambda m: m.group(1) or "", rx.sub(" ", rest)))
        return not rest.split()

    gone: set[int] = set()
    for i, ln in enumerate(lines):
        if i in inside or not _LIST_ITEM_RE.match(ln) or _TABLE_RULE_RE.match(ln):
            continue
        found = named(ln)
        lead = _line_key(ln)
        if (lead in plain_keys and not group_line(ln, lead, found)) or len(found) == 1:
            gone.add(i)
    if not gone:
        return text, 0
    i = 0
    while i < len(lines):   # the model's own tables: most body rows went, so the table is a copy of the list
        if i in inside or not lines[i].lstrip().startswith("|"):
            i += 1
            continue
        run = []
        while i < len(lines) and i not in inside and lines[i].lstrip().startswith("|"):
            run.append(i)
            i += 1
        body = [j for j in run if not _TABLE_RULE_RE.match(lines[j])]
        if len(run) >= 2 and _TABLE_RULE_RE.match(lines[run[1]]):
            body = [j for j in run[2:] if not _TABLE_RULE_RE.match(lines[j])]
        went = [j for j in body if j in gone]
        if len(went) >= 2 and 2 * len(went) >= len(body):
            gone |= {j for j in body if named(lines[j])}
    n = len(gone)
    kept = [i for i in range(len(lines)) if i not in gone]
    for j, i in enumerate(kept):   # a header and rule line of the model's own table whose rows went
        nxt = kept[j + 1] if j + 1 < len(kept) else None
        after = kept[j + 2] if j + 2 < len(kept) else None
        if i in inside or nxt is None or nxt in inside or not _TABLE_HEAD_RE.match(lines[i]) \
                or not _TABLE_RULE_RE.match(lines[nxt]):
            continue
        if after is None or after in inside or not lines[after].lstrip().startswith("|"):
            gone |= {i, nxt}
    out = "\n".join(ln for i, ln in enumerate(lines) if i not in gone)
    return re.sub(r"\n{3,}", "\n\n", out).strip("\n"), n


def _after_lead(text: str, block: str) -> str:
    """``text`` with ``block`` right after its lead (the first paragraph that is no list item, table line or Source
    line: the lead stays first, as :func:`expand` places rows), else just before a last Source line, else at the end;
    one blank line before and after it."""
    lines = text.rstrip("\n").split("\n")
    lead = next((i for i, ln in enumerate(lines) if ln.strip() and not _LIST_ITEM_RE.match(ln)
                 and not _is_source_line(ln)), None)
    if lead is not None:
        end = lead
        while end + 1 < len(lines) and lines[end + 1].strip() and not _LIST_ITEM_RE.match(lines[end + 1]) \
                and not _is_source_line(lines[end + 1]):
            end += 1
        at = end + 1
    else:
        at = len(lines) - 1 if lines and _is_source_line(lines[-1]) else len(lines)
    head = "\n".join(lines[:at]).rstrip("\n")
    tail = "\n".join(lines[at:]).strip("\n")
    return "\n\n".join(part for part in (head, block, tail) if part.strip())


def _whole_list(evidence: Any) -> Any:
    """The list a list question's answer shows when the model wrote no table line for one: the successful
    list_records results (each with every row of its call, :func:`_all_rows`) of one object and one column set,
    joined in call order (pages the model asked for one after another; a row equal to one of an earlier call is left
    out), the set with the most rows (2 or more; the later one on a tie). A ``QueryEvidence`` of the last call of the
    set whose result holds the joined rows (``returned``: their number; ``total_count``: the largest of the set), or
    None."""
    sets: dict[tuple, list] = {}
    for e in evidence or []:
        if getattr(e, "tool", None) != "list_records" or _unfillable(e) is not None:
            continue
        res = _all_rows(e, e.model_result)
        cols = res.get("columns") if isinstance(res.get("columns"), list) else list((res.get("rows") or [{}])[0])
        sets.setdefault((str(res.get("object") or getattr(e, "object", "")), tuple(map(str, cols))), []).append((e, res))
    best = None
    for calls in sets.values():
        rows: list = []
        for k, (_e, res) in enumerate(calls):
            earlier = list(rows)
            rows += [r for r in res.get("rows") or [] if k == 0 or r not in earlier]
        if len(rows) >= 2 and (best is None or len(rows) >= len(best[1])):
            best = (calls, rows)
    if best is None:
        return None
    calls, rows = best
    last, res = calls[-1]
    if len(calls) == 1:
        return last if res is last.model_result else dataclasses.replace(last, model_result=res)
    totals = [r.get("total_count") for _e, r in calls if _is_number(r.get("total_count"))]
    joined = {**res, "rows": rows, "returned": len(rows), "shown": len(rows)}
    if totals:
        joined["total_count"] = max(max(totals), len(rows))
    return dataclasses.replace(last, model_result=joined)


def list_rows(x: Expansion, evidence: Any, labels: Callable[[Any, Any], str] | None = None,
              cut: bool = False) -> Expansion:
    """A list question's answer (development A of 2026-11-27): ``x`` (the reply after :func:`expand`) with the whole
    list written by code. When no block of ``x`` holds list_records rows (no table line, or the model typed a copy of
    the rows, which :func:`expand` keeps in place of its block), the rows of the whole list (:func:`_whole_list`: the
    successful list_records calls of one object and column set, pages joined, the set with the most rows) go right
    after the lead (:func:`_after_lead`). Then the model's own list-item and table lines that each name one
    row of a list block go (:func:`_drop_typed_rows`): code's list is the only one. ``cut``: the
    reply stopped at its token limit; when its last line (outside the blocks) is a list-item or table line, the model
    was typing rows: that broken line goes first, and with a list block in the answer ``counts["list_cut_rows"]`` is 1
    (the rows the cut lost are code's; the caller adds no cut line). ``x`` is updated in place (``text``, ``blocks``,
    ``sources``, ``counts``: ``built``, ``rows``, ``items``, ``list_added``, ``list_dropped``, ``list_cut_rows``) and
    returned. Never raises: ``x`` as it was on any error."""
    try:
        labels = labels or column_labels(None)
        text, blocks = x.text, dict(x.blocks)
        ids = _list_block_ids(x)
        counts = dict(x.counts) if isinstance(x.counts, dict) else _zero()
        added = tail = 0
        if cut:   # the line the token limit broke off, when it is a row the model was typing
            lines = text.rstrip("\n").split("\n")
            inside = _block_lines(lines, list(blocks.values()))
            last = max((i for i, ln in enumerate(lines) if ln.strip()), default=None)
            if last is not None and last not in inside and _LIST_ITEM_RE.match(lines[last]):
                text, tail = "\n".join(lines[:last]).rstrip("\n"), 1
        if not ids:
            best = _whole_list(evidence)
            if best is None:
                return x
            e = best
            marker = _marker(str(getattr(e, "query_id", "")))
            if marker in blocks:
                return x
            own = marked(text, blocks) if blocks else text
            said = _count_numbers("\n".join(ln for ln in own.split("\n") if not MARKER_RE.search(ln)))
            out, rows, form = _render(e, labels, said)
            block = "\n".join(out)
            text = _after_lead(text, block)
            blocks[marker] = block
            ids = {marker}
            added = 1
            counts["built"] = int(counts.get("built", 0)) + 1
            counts["rows"] = int(counts.get("rows", 0)) + rows
            counts["items"] = list(counts.get("items") or []) + [
                {"query_id": str(getattr(e, "query_id", "")), "tool": "list_records", "rows": rows, "form": form}]
        text, dropped = _drop_typed_rows(text, list(blocks.values()), [blocks[m] for m in ids])
        if not added and not dropped and not (tail and ids):
            return x
        counts["list_added"] = added
        counts["list_dropped"] = dropped + (1 if tail and ids else 0)
        if tail and ids:
            counts["list_cut_rows"] = 1
        lists = {_marker(str(getattr(e, "query_id", ""))) for e in evidence or []
                 if getattr(e, "tool", None) == "list_records"}
        x.text, x.blocks, x.counts = text, blocks, counts
        x.typed = {m: b for m, b in x.typed.items() if m not in lists}   # code's list stands: no copy comes back
        x.sources = ["\n\n".join(blocks.values())] if blocks else []
        return x
    except Exception:  # noqa: BLE001 - the reply as expand left it
        return x
