"""Expected answers that survive a data refresh: re-run the saved SQL read-only at grading time.

The lookup set (``okeval_run/lookup_eval/expected.json``) and the person set (``.../people/expected.json``) save, for
every fact, the SQL that computed it. :func:`check_entry` re-runs that SQL on the warehouse the run used (opened
read-only, external access off, configuration locked) and compares the fresh rows with the saved fact:

* every key the same -> ``status == "same"``: the expected numbers hold as written;
* a key changed -> ``status == "changed"``: the old -> new numbers of the changed keys (aligned cell by cell when the
  rows keep their shape, plus the sums and row counts of each key) go into ``number_map``, and the numbers of keys
  that changed shape, or that have no SQL (the reference scan of one lookup question), are ``suspect``.

:func:`refresh_numbers` then maps the numbers a grader requires: unchanged ones stay, a number with exactly one new
value is replaced, and an ambiguous or suspect one is reported as stale (the grader returns ``needs_review``). When
the SQL fails (``status == "error"``, e.g. a column renamed by a sync), every number is stale; when only some
statements fail, the numbers no checked key confirms are stale. Text cells (names, picklist values, dates) of a
changed key that are gone from its fresh rows are ``gone_text``: a grader requiring one of them returns
``needs_review`` too (:func:`stale_texts`).

People facts use three SQL shapes: one statement (a scalar, ``{key: count}`` pairs or rows), a list of statements
(one per person object, their rows together) and a mapping (per record: ``business`` / ``system``, the latter cut to
the saved length). ``positive_statements`` of a link fact are re-run one by one against the saved counts (pairs that
had no link when the file was built are not searched again). Only single ``SELECT`` / ``WITH`` statements run.
"""
from __future__ import annotations

import datetime as dt
import decimal
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

__all__ = ["DataCheck", "open_readonly", "check_entry", "check_lookup_entry", "check_people_entry",
           "refresh_numbers", "stale_texts", "canon_number", "numbers_of", "simple_rows"]

_SELECT_RE = re.compile(r"^\s*(?:SELECT|WITH)\b", re.IGNORECASE)


def open_readonly(path: str):
    """A read-only duckdb connection to the snapshot ``path`` (the record tools' connection settings, configuration
    locked). Refuses the live warehouse paths like the record tools do (``RecordsUnavailable``)."""
    import duckdb

    from sf_assistant.engine.snapshot import check_warehouse_path
    from sf_assistant.engine.warehouse import CONNECT_CONFIG

    real = check_warehouse_path(path)
    con = duckdb.connect(real, read_only=True, config=dict(CONNECT_CONFIG))
    try:
        con.execute("SET GLOBAL TimeZone='UTC'")
        con.execute("SET lock_configuration=true")
    except duckdb.Error:
        # the record tools opened the same file in this process first (one shared database instance): its
        # configuration is already locked, with the same settings
        if con.execute("SELECT current_setting('TimeZone')").fetchone()[0] != "UTC":
            con.close()
            raise
    return con


def _cell(v: Any) -> Any:
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


def _run(con, sql: str) -> list[list]:
    if not isinstance(sql, str) or not _SELECT_RE.match(sql) or ";" in sql.strip().rstrip(";"):
        raise ValueError("only one SELECT / WITH statement may run")
    return [[_cell(c) for c in row] for row in con.execute(sql).fetchall()]


def simple_rows(rows: list[list]) -> Any:
    """The lookup set's shape of a result (``build_expected.simple``): one cell -> the value, two columns -> a
    ``{key: value}`` dict ('(blank)' for a NULL key), else the rows."""
    if len(rows) == 1 and len(rows[0]) == 1:
        return rows[0][0]
    if rows and all(len(r) == 2 for r in rows):
        return {str(k) if k is not None else "(blank)": v for k, v in rows}
    return rows


def canon_number(v: Any) -> str | None:
    """'7', '6.8', ... for an int / float / Decimal (bools and text: None)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, decimal.Decimal)):
        return None
    try:
        d = decimal.Decimal(str(v))
    except decimal.InvalidOperation:
        return None
    return format(d.normalize(), "f") if d != 0 else "0"


def _canon(v: Any) -> str:
    if v is None or v == "(blank)":
        return "∅"
    if isinstance(v, bool):
        return "true" if v else "false"
    n = canon_number(v)
    if n is not None:
        return n
    text = str(v)
    # string_agg(DISTINCT ...) lists come back in any order: compare them as sets
    return ", ".join(sorted(text.split(", "))) if ", " in text else text


def _flat(v: Any, keys_are_data: bool = True) -> list[Any]:
    """Every scalar of a fact in order. A dict's keys count as data when its values are scalars and it is not inside
    a list (``{"Completed": 4}``); keys of a dict of rows (``{"User": [...]}``) or of a row dict are labels."""
    if isinstance(v, dict):
        scalar_values = all(not isinstance(x, (dict, list)) for x in v.values())
        out: list[Any] = []
        for k, x in v.items():
            if keys_are_data and scalar_values:
                out.append(k)
            out += _flat(x, keys_are_data and not scalar_values)
        return out
    if isinstance(v, list):
        out = []
        for x in v:
            out += _flat(x, False)
        return out
    return [v]


def numbers_of(v: Any) -> list[str]:
    """Canonical numbers of a fact (leaves) plus its aggregates: the sum of a ``{key: count}`` dict, the row count of a
    list and the sum of the last column of a list of rows."""
    out = [n for n in (canon_number(x) for x in _flat(v)) if n is not None]
    return out + _aggregates(v)


def _aggregates(v: Any) -> list[str]:
    out: list[str] = []
    if isinstance(v, dict):
        vals = [x for x in v.values() if canon_number(x) is not None]
        if vals and len(vals) == len(v):
            out.append(canon_number(sum(decimal.Decimal(str(x)) for x in vals)))
        for x in v.values():
            if isinstance(x, (dict, list)):
                out += _aggregates(x)
    elif isinstance(v, list):
        out.append(str(len(v)))
        last = [r[-1] for r in v if isinstance(r, list) and r and canon_number(r[-1]) is not None]
        if last and len(last) == len(v):
            out.append(canon_number(sum(decimal.Decimal(str(x)) for x in last)))
        for x in v:
            if isinstance(x, dict):
                out += _aggregates(x)
    return [n for n in out if n is not None]


@dataclass
class DataCheck:
    """Result of re-running one entry's SQL. ``status``: same | changed | unchecked (no SQL, no connection) |
    error. ``number_map``: old -> new numbers of the changed keys; ``suspect``: numbers that changed in a way that
    cannot be mapped; ``unchanged``: numbers of keys that stayed the same; ``unverified``: keys without SQL;
    ``gone_text``: text cells of changed keys that are not in their fresh rows any more."""

    status: str = "unchecked"
    keys: int = 0
    changed: list[str] = field(default_factory=list)
    number_map: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    suspect: set[str] = field(default_factory=set)
    unchanged: set[str] = field(default_factory=set)
    unverified: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    gone_text: set[str] = field(default_factory=set)

    def summary(self) -> dict:
        return {"status": self.status, "keys": self.keys, "changed": list(self.changed),
                "unverified": list(self.unverified), "errors": list(self.errors)[:5]}


def _compare(chk: DataCheck, key: str, old: Any, fresh: Any) -> None:
    chk.keys += 1
    a, b = [_canon(x) for x in _flat(old)], [_canon(x) for x in _flat(fresh)]
    if sorted(a) == sorted(b):
        chk.unchanged.update(numbers_of(old))
        return
    chk.changed.append(key)
    if _scalar_dict(old) and _scalar_dict(fresh):   # {label: count}: align by label, whatever the row order
        for label in old:
            x = _canon(old[label])
            if label not in fresh:
                chk.gone_text.add(_canon(label))
                if canon_number(_num_or_none(x)) is not None:
                    chk.suspect.add(x)
                continue
            y = _canon(fresh[label])
            if x != y:
                chk.number_map[x].add(y)
            elif canon_number(_num_or_none(x)) is not None:
                chk.unchanged.add(x)
        for x, y in zip(_aggregates(old), _aggregates(fresh)):
            if x != y:
                chk.number_map[x].add(y)
        return
    text_a = [x for x in a if canon_number(_num_or_none(x)) is None]
    text_b = [x for x in b if canon_number(_num_or_none(x)) is None]
    fresh_text = set(text_b)
    chk.gone_text.update(x for x in text_a if x not in fresh_text and x != "∅")
    if len(a) == len(b) and text_a == text_b:
        for x, y in zip(a, b):
            if x != y:
                chk.number_map[x].add(y)
            elif canon_number(_num_or_none(x)) is not None:
                chk.unchanged.add(x)   # a cell that kept its number
    else:
        chk.suspect.update(n for n in (canon_number(_num_or_none(x)) for x in a) if n is not None)
    for x, y in zip(_aggregates(old), _aggregates(fresh)):
        if x != y:
            chk.number_map[x].add(y)


def _scalar_dict(v: Any) -> bool:
    return isinstance(v, dict) and bool(v) and all(not isinstance(x, (dict, list)) for x in v.values())


def _num_or_none(s: str) -> Any:
    try:
        return decimal.Decimal(s) if re.fullmatch(r"-?\d+(?:\.\d+)?", s) else None
    except decimal.InvalidOperation:
        return None


def _finish(chk: DataCheck) -> DataCheck:
    if chk.errors and not chk.keys:
        chk.status = "error"
    elif chk.changed:
        chk.status = "changed"
    elif chk.keys:
        chk.status = "same" if not chk.errors else "error"
    return chk


def check_lookup_entry(con, entry: dict) -> DataCheck:
    """Re-run every ``sql`` statement of a lookup-set entry and compare it with ``expected``."""
    chk = DataCheck()
    expected = entry.get("expected") or {}
    sqls = entry.get("sql") or {}
    for key, sql in sqls.items():
        try:
            fresh = simple_rows(_run(con, sql))
        except Exception as exc:  # noqa: BLE001 - a broken statement is reported, not raised
            chk.errors.append(f"{key}: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}")
            continue
        _compare(chk, key, expected.get(key), fresh)
    chk.unverified = [k for k in expected if k not in sqls]
    return _finish(chk)


def _people_pair(chk: DataCheck, con, key: str, fact: Any, sql: Any) -> None:
    if sql is None:
        if fact not in (None, [], {}):
            chk.unverified.append(key)
        return
    try:
        if isinstance(sql, str):
            rows = _run(con, sql)
            _compare(chk, key, fact, rows if not isinstance(fact, dict) or isinstance(next(iter(fact.values()), None),
                                                                                         (dict, list))
                     else {str(k) if k is not None else "(blank)": v for k, v in rows})
            return
        if isinstance(sql, list) and all(isinstance(s, str) for s in sql):
            rows: list[list] = []
            for s in sql:
                rows += _run(con, s)
            if isinstance(fact, dict) and fact and all(not isinstance(x, (dict, list)) for x in fact.values()):
                fresh: Any = {str(r[0]) if r[0] is not None else "(blank)": r[1] for r in rows if len(r) == 2}
            else:
                fresh = rows
            _compare(chk, key, fact, fresh)
            return
        if isinstance(sql, dict) and "positive_statements" in sql:
            rows_old = fact if isinstance(fact, list) else []
            for i, s in enumerate(sql.get("positive_statements") or []):
                old = rows_old[i] if i < len(rows_old) else None
                new = _run(con, s)
                _compare(chk, f"{key}[{i}]", old[-1] if isinstance(old, list) and old else old,
                         new[0][0] if new and new[0] else None)
            return
        if isinstance(sql, dict) and isinstance(fact, dict):
            for sub, s in sql.items():
                fk = sub if sub in fact else ("system_top" if sub == "system" and "system_top" in fact else None)
                if fk is None:
                    if s is not None:
                        chk.unverified.append(f"{key}/{sub}")
                    continue
                if fk == "system_top" and isinstance(s, str):
                    # the saved fact keeps the first rows only (build_people.refs_per_record system_top)
                    old = fact[fk] or []
                    fresh_rows = _run(con, s)
                    _compare(chk, f"{key}/{sub}", old, fresh_rows[:max(len(old), 12)] if old else fresh_rows[:12])
                    continue
                _people_pair(chk, con, f"{key}/{sub}", fact[fk], s)
            return
    except Exception as exc:  # noqa: BLE001
        chk.errors.append(f"{key}: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}")
        return
    chk.unverified.append(key)


def check_people_entry(con, entry: dict) -> DataCheck:
    """Re-run the SQL of a person-set entry (questions and controls) and compare it with ``expected``."""
    chk = DataCheck()
    expected = entry.get("expected") or {}
    sqls = entry.get("sql") or {}
    for key, sql in sqls.items():
        if key not in expected:
            chk.unverified.append(key)
            continue
        _people_pair(chk, con, key, expected[key], sql)
    chk.unverified += [k for k in expected if k not in sqls and expected[k] not in (None, [], {}, False, True)
                       and not isinstance(expected[k], str)]
    return _finish(chk)


def check_entry(con, entry: dict | None, kind: str) -> DataCheck:
    """:func:`check_lookup_entry` (``kind == "lookup"``) or :func:`check_people_entry` (people, control); an
    ``unchecked`` result without a connection or an entry."""
    if con is None or not entry:
        return DataCheck()
    return check_lookup_entry(con, entry) if kind == "lookup" else check_people_entry(con, entry)


def refresh_numbers(numbers: list[str], chk: DataCheck | None, entry_numbers: set[str] | None = None
                    ) -> tuple[list[str], list[str], list[str]]:
    """``(numbers to require, stale numbers, notes)`` for the canonical ``numbers`` a grader requires.

    Without a change every number stays. After a change: a number of a changed key with exactly one new value (and
    not also in an unchanged key) is replaced by it; a suspect, ambiguous or (``entry_numbers`` given) underived one
    is stale. When the SQL failed (``status == "error"``) every number is stale; when some statements failed, a number
    no checked key confirms is stale. ``notes`` holds only ``"data changed: <old> -> <new>"`` lines."""
    if chk is None or chk.status == "error":
        return ([], list(numbers), []) if chk is not None else (list(numbers), [], [])
    if chk.status != "changed" and not chk.errors:
        return list(numbers), [], []
    keep, stale, notes = [], [], []
    for n in numbers:
        new = chk.number_map.get(n) or set()
        if n in chk.suspect or (new and n in chk.unchanged) or len(new) > 1:
            stale.append(n)
        elif len(new) == 1:
            m = next(iter(new))
            keep.append(m)
            notes.append(f"data changed: {n} -> {m}")
        elif entry_numbers is not None and n not in entry_numbers and n not in chk.unchanged:
            stale.append(n)
        elif (chk.unverified or chk.errors) and n not in chk.unchanged:
            stale.append(n)
        else:
            keep.append(n)
    return keep, stale, notes


def stale_texts(values: list[str], chk: DataCheck | None) -> list[str]:
    """The ``values`` (names, picklist values, ISO dates of must items) that a changed key no longer holds: a value
    equal to a gone text cell, or a date that starts one (a timestamp cell)."""
    if chk is None or not chk.gone_text:
        return []
    out = []
    for v in values:
        c = _canon(v)
        if c in chk.gone_text or (re.fullmatch(r"\d{4}-\d{2}-\d{2}", c) and any(g.startswith(c) for g in chk.gone_text)):
            out.append(v)
    return out
