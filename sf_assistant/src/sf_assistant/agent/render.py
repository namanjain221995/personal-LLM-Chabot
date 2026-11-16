"""Plain-text rendering of the record evidence for ``ask.py`` (no duckdb import).

:func:`format_records_block` prints the RECORDS block from the evidence log: where the data comes
from and how fresh it is, then every query with what was applied, the notes, the tables and their
sync times, and each SQL statement with its bound params and rows. Nothing is re-run. The passcodes, tokens and
other very-high values in a row's text are shown as ``[hidden]``, as in the tool results (phase 1:
``engine.catalog.hide_embedded_secrets``).
:func:`record_trace_lines` prints one line per record tool call of the record agent, after a route line when the
triage overruled the route call (a named person, a name no stored person has, or a stored record value the question
names: value spots, 2026-10-28), and with ``SFA_TABLES=code`` one ``code rows`` line when the reply had table lines
(latency plan step 3: how many code filled, the rows, why the others were dropped).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable

from sf_assistant.engine.catalog import hide_embedded_secrets

if TYPE_CHECKING:  # type hints only
    from sf_assistant.engine.types import QueryEvidence, RecordsStatus, Statement

    from .results import RecordAnswer

__all__ = ["format_records_block", "record_trace_lines", "applied_text", "RULE"]

RULE = "-" * 72
CELL_MAX = 40
TABLE_MAX = 160  # wider tables print one "column : value" line per cell instead
INDENT = " " * 5


def _cell(value: Any) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= CELL_MAX else text[:CELL_MAX - 3] + "..."


def _table(columns: list, rows: list) -> list[str]:
    """Fixed-width text table, cells cut to CELL_MAX characters; wider than TABLE_MAX: one block per row."""
    if not rows:
        return ["(no rows)"]
    width = max([len(columns)] + [len(r) for r in rows if isinstance(r, (list, tuple))])
    cols = [_cell(columns[i]) if i < len(columns) else f"col{i + 1}" for i in range(width)]
    cells = [[_cell(r[i]) if isinstance(r, (list, tuple)) and i < len(r) else "" for i in range(width)] for r in rows]
    widths = [max([len(c)] + [len(r[i]) for r in cells]) for i, c in enumerate(cols)]
    if sum(widths) + 2 * (len(widths) - 1) > TABLE_MAX:
        name_w = max(len(c) for c in cols)
        lines: list[str] = []
        for n, r in enumerate(cells, 1):
            lines.append(f"[{n}]")
            lines += [f"  {c.ljust(name_w)} : {v}".rstrip() for c, v in zip(cols, r)]
        return lines
    lines = ["  ".join(c.ljust(w) for c, w in zip(cols, widths)).rstrip()]
    lines += ["  ".join(v.ljust(w) for v, w in zip(r, widths)).rstrip() for r in cells]
    return lines


def _period_text(pa: dict) -> str:
    start, end = pa.get("from"), pa.get("to_exclusive")
    field = pa.get("date_field") or "?"
    text = f"{field} in [{start or '…'}, {end or '…'})"
    extra = [str(pa["phrase"])] if pa.get("phrase") else []
    if pa.get("time_zone"):
        extra.append(str(pa["time_zone"]))
    if pa.get("utc_from") or pa.get("utc_to_exclusive"):
        extra.append(f"UTC [{pa.get('utc_from') or '…'}, {pa.get('utc_to_exclusive') or '…'})")
    return text + (f" ({', '.join(extra)})" if extra else "")


def _plain(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def applied_text(applied: dict, result: dict | None = None) -> str:
    """One line: filters, any_of, period, related (and compare segments), joined with '; '."""
    parts: list[str] = []
    for key, value in (applied or {}).items():
        if value in (None, "", [], {}):
            continue
        if key == "filters":
            parts += [str(v) for v in value]
        elif key == "base_filters":
            parts.append("base: " + " AND ".join(str(v) for v in value))
        elif key == "part_filters":
            parts.append("part: " + " AND ".join(str(v) for v in value))
        elif key == "any_of":
            parts.append("any of (" + " OR ".join(str(v) for v in value) + ")")
        elif key == "period" and isinstance(value, dict):
            parts.append(_period_text(value))
        elif key == "related":
            parts += [str(v) for v in value]
        elif key == "changed_to":
            parts.append(f"changed to '{_plain(value)}'")
        elif key == "contains":
            parts.append(f"value contains '{_plain(value)}'")
        elif key in ("record", "field"):
            parts.append(f"{key} = {_plain(value)}")
        else:
            parts.append(f"{key} = {json.dumps(value, ensure_ascii=False, default=str)}")
    for seg in (result or {}).get("segments") or []:
        if not isinstance(seg, dict):
            continue
        inner = applied_text(seg.get("applied") or {})
        parts.append(f"segment '{seg.get('label')}': {inner}")
    return "; ".join(parts) or "all records"


def _origin_text(status: "RecordsStatus") -> str:
    if status.org_is_sandbox is True:
        return f"{status.origin} (Organization.IsSandbox = true), not production"
    if status.org_is_sandbox is False:
        return f"{status.origin} (Organization.IsSandbox = false)"
    return f"{status.origin} (Organization.IsSandbox unknown)"


def _field_lines(label: str, lines: list[str], width: int) -> list[str]:
    head = f"{INDENT}{label.ljust(width)} : "
    if not lines:
        return [head.rstrip()]
    pad = " " * len(head)
    return [head + lines[0]] + [pad + ln for ln in lines[1:]]


def _masked(v: Any) -> Any:
    """A row cell without the passcodes, tokens and other very-high values of its text (the tool results' rule)."""
    return hide_embedded_secrets(v) if isinstance(v, str) else v


def _statement_lines(st: "Statement", idx: int | None, max_rows: int, width: int) -> list[str]:
    tag = "" if idx is None else f"[{idx}]"
    sql = st.sql + ("   (reused from an earlier call of this question)" if getattr(st, "reused", False)
                    else "   (cached at open)" if st.cached else "")
    out = _field_lines(f"sql{tag}", [sql], width)
    out += _field_lines(f"params{tag}", [repr(list(st.params))], width)
    k = max(0, min(len(st.rows), max_rows))
    rows = _table(list(st.columns), [[_masked(v) for v in r] if isinstance(r, (list, tuple)) else r
                                     for r in st.rows[:k]])
    if k < len(st.rows):
        rows.append(f"... showing {k} of {len(st.rows)} rows")
    if st.truncated:
        rows.append(f"... more rows existed; fetching stopped at {st.row_count}")
    out += _field_lines(f"rows{tag}", rows, width)
    return out


def _entry_lines(e: "QueryEvidence", max_rows: int, rows_of: Callable[[str], Any] | None) -> list[str]:
    obj = e.object or (e.arguments or {}).get("object") or "-"
    head = f"[{e.query_id}] {e.tool}  {obj}  -> {e.summary}   ({e.elapsed_ms:.0f} ms, SQL built by code)"
    many = len(e.statements) > 1
    width = 9 if many else 7
    out = [head]
    out += _field_lines("applied", [applied_text(e.applied, e.model_result)], width)
    if e.notes:
        out += _field_lines("notes", [str(n) for n in e.notes], width)
    if e.tables:
        tables = []
        for t in e.tables:
            n = None
            if rows_of is not None:
                try:
                    n = rows_of(t)
                except Exception:  # noqa: BLE001 - rendering must not fail
                    n = None
            synced = f"synced {e.synced_at.get(t) or 'unknown'}"
            tables.append(f"{t} ({n} records, {synced})" if n is not None else f"{t} ({synced})")
        out += _field_lines("tables", [", ".join(tables)], width)
    for i, st in enumerate(e.statements, 1):
        out += _statement_lines(st, i if many else None, max_rows, width)
    return out


def _orientation_line(e: "QueryEvidence") -> str:
    obj = e.object or (e.arguments or {}).get("object") or (e.arguments or {}).get("query") or "-"
    n = len(e.statements)
    cached = sum(1 for s in e.statements if s.cached)
    if n == 0:
        what = "orientation"
    elif cached == n:
        what = f"orientation; {n} cached value statement{'s' if n != 1 else ''}; -v shows them"
    else:
        what = f"orientation; {n} statement{'s' if n != 1 else ''}; -v shows them"
    return f"[{e.query_id}] {e.tool}  {obj}  ({what})"


def _failed_line(e: "QueryEvidence") -> str:
    obj = e.object or (e.arguments or {}).get("object") or "-"
    err = " ".join(str(e.error or "unknown error").split())
    return f"[{e.query_id}] FAILED {e.tool}  {obj}  error: {err[:400]}"


def format_records_block(rec: "RecordAnswer", status: "RecordsStatus", max_rows: int = 20, *, verbose: bool = False,
                         rows_of: Callable[[str], Any] | None = None) -> str:
    """The RECORDS block for one question, or '' when the record agent ran no data query.

    Order: successful data queries, then orientation queries (one line each; full with ``verbose``),
    then failed queries. ``rows_of(object)`` gives a table's record count for the tables line. An
    answer that rests only on orientation results (their stored value counts) prints them in full.
    """
    evidence = list(rec.evidence or [])
    if not any(e.kind == "data" for e in evidence):
        # An answer taken from orientation results (their value counts) still shows where they came from.
        if not (rec.answer and any(e.ok for e in evidence)):
            return ""
        verbose = True
    copied = status.copied_at_utc or "unknown"
    if status.copied_at_from == "file time":
        copied += " (file time)"
    today = status.today + (" pinned" if status.today_pinned else "")
    out = [f"{RULE} RECORDS",
           f"source  : {status.snapshot_path}, read-only local copy of {status.source}",
           f"origin  : {_origin_text(status)}",
           f"copied  : {copied}   newest sync {status.newest_sync_utc or 'unknown'}",
           f"today   : {today} ({status.time_zone})"]
    data = [e for e in evidence if e.ok and e.kind == "data"]
    orient = [e for e in evidence if e.ok and e.kind != "data"]
    failed = [e for e in evidence if not e.ok]
    for e in data:
        out.append("")
        out += _entry_lines(e, max_rows, rows_of)
    if orient:
        out.append("")
        for e in orient:
            if verbose:
                out += _entry_lines(e, max_rows, rows_of)
                out.append("")
            else:
                out.append(_orientation_line(e))
        if verbose and out[-1] == "":
            out.pop()
    if failed:
        out.append("")
        out += [_failed_line(e) for e in failed]
    return "\n".join(out)


def _args_text(args: Any) -> str:
    try:
        return json.dumps(args, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return repr(args)


#: Tool names the record tools map to another tool (okrec.tools.TOOL_ALIASES; no duckdb import here).
_ALIASES = {"find_references": "about_people"}


def _route_lines(rec: "RecordAnswer") -> list[str]:
    """'-- route: the route call said NO_RECORDS (or unclear); overruled by <reason>' when the triage overruled the
    route call (``rec.triage`` ``overruled_by`` / ``overrule_reason``, :class:`~sf_assistant.agent.triage.Triage`),
    else []. The reason names no stored value besides the question's own words, and no typed e-mail address or phone
    number."""
    tri = getattr(rec, "triage", None)
    if not isinstance(tri, dict) or not tri.get("overruled_by"):
        return []
    said = "NO_RECORDS" if tri.get("route") is False else "unclear"
    return [f"-- route: the route call said {said}; overruled by {tri.get('overrule_reason') or tri['overruled_by']}"]


def record_trace_lines(rec: "RecordAnswer", verbose: bool = False) -> list[str]:
    """'-- record agent: ...' plus one line per tool call: arguments, ok, '-> qN' or the error; first the route line
    when the triage overruled the route call (:func:`_route_lines`)."""
    r = rec.agent
    head = _route_lines(rec)
    if r is None and getattr(rec, "routed_out", False):
        return head + ["-- record agent: not run (the route call found no record data in the question, and no name or "
                       "stored record value in it overrules that)"]
    if r is None:
        return head + [f"-- record agent: no run ({rec.error or 'unknown error'})"]
    usage = r.usage or {}
    lines = head + [f"-- record agent: {len(r.tool_calls)} tool call(s), {r.steps} model call(s), {r.latency_s:.1f} s, "
                    f"{usage.get('prompt_tokens', 0)}+{usage.get('completion_tokens', 0)} tokens"]
    evidence = list(rec.evidence or [])
    pos = 0
    for c in r.tool_calls:
        args = c.get("arguments")
        ev = None
        for j in range(pos, len(evidence)):
            # an alias call (find_references) is logged as its tool (about_people) with the arguments as sent
            if evidence[j].tool in (c.get("name"), _ALIASES.get(str(c.get("name")))) and evidence[j].arguments == args:
                ev, pos = evidence[j], j + 1
                break
        line = f"   {c.get('name')}({_args_text(args)})  ok={c.get('ok')}"
        if c.get("ok"):
            line += f"  -> {ev.query_id}" if ev is not None else ""
        else:
            err = " ".join(str(c.get("error") or (ev.error if ev else "") or "error").split())
            line += f"  error: {err[:200]}"
        lines.append(line)
        if verbose and ev is not None and ev.ok and ev.model_result is not None:
            lines.append("     " + json.dumps(ev.model_result, ensure_ascii=False, default=str)[:1500])
    ct = getattr(rec, "code_tables", None) or {}
    if ct.get("markers"):   # SFA_TABLES=code (latency plan step 3): the table lines and the rows code wrote for them
        gone = ", ".join(f"{k} {v}" for k, v in (ct.get("dropped") or {}).items())
        lines.append(f"   code rows: {ct.get('built', 0)} of {ct['markers']} table line(s) filled ({ct.get('rows', 0)} "
                     "rows)" + (f"; dropped: {gone}" if gone else "")
                     + (f"; put back: {ct['put_back']}" if ct.get("put_back") else "")
                     + ("; the first draft was asked to fix its table lines" if ct.get("nudged") else ""))
    if r.error:
        lines.append(f"   record agent error: {r.error}")
    if getattr(rec, "code_answer", False):   # §22.8: the agent gave no answer about the named people
        ran = [e for e in evidence if e.tool == "about_people" and "run by code after the record agent gave no answer"
               in (e.notes or [])]
        lines.append("   code wrote the record answer from about_people"
                     + (f" ({ran[-1].query_id}, run by code)" if ran else " (the agent's own call)"))
    dropped = " ".join(str(getattr(rec, "dropped_answer", "") or "").split())
    if dropped:   # a count after a not-found error: the reply is not shown as the answer, but it is logged
        lines.append(f"   record reply dropped ({rec.error or 'not a not-found answer'}): "
                     + (dropped if verbose else dropped[:200]))
    removed = list(getattr(rec, "dropped_numbers", None) or [])
    if removed:   # numbers no result holds (a sum the model added up): code removed their sentences or items
        lines.append("   code removed the parts of the record answer with numbers found in no record result: "
                     + ", ".join(removed))
    return lines
