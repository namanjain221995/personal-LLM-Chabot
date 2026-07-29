"""SQL engine (spec §8).

Flow: schema (cached) → gpt-oss-120b writes ONE SELECT → sql_guard validates
→ DuckDB executes READ-ONLY → one retry on failure feeding the error text
back → meta.data capped at 500 rows with truncated flag → optional export
(100k cap) → chart spec ONLY when the user asked for a chart/graph/plot,
validated by pydantic (invalid → table only; model output is NEVER executed)
→ streamed narrative answer.
"""
from __future__ import annotations

import json
import os
import re
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from . import NO_DATA_MESSAGE, recent_turns
from .. import llm
from ..config import settings
from ..core.chart_spec import parse_chart_spec
from ..core.exports import cap_rows, export_csv, export_xlsx, slugify
from ..core.schema_cache import format_schema, schema_cache
from ..core.sql_guard import guard_sql

Emit = Callable[[str, dict], Awaitable[None]]

CHART_RE = re.compile(r"\b(chart|graph|plot|visuali[sz]e|visuali[sz]ation)\b", re.I)
EXPORT_RE = re.compile(r"\b(export|download|excel|xlsx|csv|spreadsheet)\b", re.I)
_CSV_RE = re.compile(r"\bcsv\b", re.I)
_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)

_SQL_SYSTEM = (
    "You are a senior analytics engineer writing DuckDB SQL over Salesforce "
    "data. Rules:\n"
    "- Output exactly ONE SELECT statement (WITH ... SELECT is allowed).\n"
    "- Never write INSERT/UPDATE/DELETE/DDL — the database is read-only.\n"
    "- Use only the tables and columns from the provided schema.\n"
    # Salesforce objects have wildly different column sets, so the obvious
    # "sample every object" answer (SELECT * FROM A UNION ALL SELECT * FROM B)
    # dies on DuckDB's "Set operations can only apply to expressions with the
    # same number of result columns".
    "- UNION / UNION ALL / INTERSECT / EXCEPT require every branch to select "
    "the SAME number of columns, in the same order, with compatible types. "
    "Never combine different Salesforce objects with SELECT *; instead select "
    "an explicit, identical column list in each branch (cast or use NULL AS "
    "col to line them up), or return one object per query.\n"
    # Qwen tends to emit MySQL backticks; DuckDB only accepts double quotes.
    '- This is DuckDB. Quote reserved-word identifiers (e.g. the "Case" table) '
    'with DOUBLE QUOTES like "Case" — NEVER backticks.\n'
    # Salesforce checkbox fields land here as the TEXT 'true'/'false', not as
    # booleans. `WHERE IsWon = 'True'` therefore matches nothing and answers
    # "0" with total confidence — a wrong number is worse than an error.
    "- Salesforce checkbox columns (IsWon, IsClosed, IsActive, IsDeleted, "
    "Is*__c ...) are stored as the lowercase TEXT 'true' / 'false'. Compare "
    "them as text in lower case, e.g. WHERE IsWon = 'true'. Never compare "
    "them to True, 1, or 'True'.\n"
    # A status question answered off the wrong column is silently wrong.
    "- Prefer the column the question names. For Opportunity outcomes use "
    "StageName (e.g. StageName = 'Closed Won'), which carries the wording the "
    "user sees, rather than inferring from a checkbox.\n"
    "- Output ONLY the SQL, no explanation, no markdown fence."
)

# Backticks are never valid DuckDB; the model (Qwen) emits them for reserved
# words like `Case`. Rewrite `ident` → "ident" so the query parses.
_BACKTICK_RE = re.compile(r"`([^`]+)`")


def extract_sql(text: str) -> str:
    """Strip <think> blocks and markdown fences, and normalize backtick quoting
    (MySQL `ident` → DuckDB "ident") from model output."""
    t = _THINK_RE.sub("", text or "").strip()
    m = _FENCE_RE.search(t)
    if m:
        t = m.group(1).strip()
    t = _BACKTICK_RE.sub(r'"\1"', t)
    return t.strip()


async def _ask_sql(
    question: str,
    schema_text: str,
    history: Sequence[dict] = (),
    previous_sql: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    # The org dictionary maps what people SAY to what the API calls it. Left
    # to guess, the model writes a plausible-looking field name that returns
    # no rows instead of erroring — a silently wrong answer.
    from ..core.sf_dictionary import hint_for

    hint = hint_for(question)
    user = f"Database schema:\n{schema_text}\n\nQuestion: {question}"
    if hint:
        user = f"{hint}\n\n{user}"
    if error is not None:
        user += (
            f"\n\nYour previous SQL failed.\nPrevious SQL:\n{previous_sql}\n"
            f"Error:\n{error}\n\nWrite a corrected single SELECT statement."
        )
    messages = (
        [{"role": "system", "content": _SQL_SYSTEM}]
        + recent_turns(history, 6)
        + [{"role": "user", "content": user}]
    )
    raw = await llm.chat_completion(messages, temperature=0.1, max_tokens=6000)
    return extract_sql(raw)


def _execute(sql: str, fetch_cap: int) -> Tuple[List[str], List[list]]:
    import duckdb  # lazy

    # §8/§12: read-only, and NO external access — DuckDB's read_csv/read_blob/
    # glob/httpfs table functions would otherwise let a guard-approved SELECT
    # read arbitrary host files or reach the network. With this config DuckDB
    # raises PermissionException for every such function.
    con = duckdb.connect(
        settings.duckdb_path,
        read_only=True,
        config={
            "enable_external_access": False,
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
        },
    )
    try:
        cur = con.execute(sql)
        columns = [d[0] for d in cur.description or []]
        rows = [list(r) for r in cur.fetchmany(fetch_cap)]
    finally:
        con.close()
    return columns, rows


#: Asking for LIVE data explicitly. The warehouse is the default because it is
#: faster and cheaper, but "check Salesforce directly" is a clear instruction
#: and answering it from a copy — while saying "the live check confirms" —
#: is worse than being slow.
_LIVE_RE = re.compile(
    r"\b(live|directly in salesforce|real salesforce|from salesforce itself|"
    r"straight from salesforce|right now|up to date|up-to-date|latest from "
    r"salesforce|actual salesforce)\b",
    re.I,
)


def wants_live_lookup(question: str) -> bool:
    return bool(_LIVE_RE.search(question or ""))


class NoSuchTable(RuntimeError):
    """The question is about an object the warehouse does not carry.

    Raised so callers can go and ask Salesforce instead of presenting a made-up
    number. This exists because of a real answer the model gave: asked about
    Course__c — a real object that was never synced — it wrote
    `SELECT 0 AS record_count` and reported "0 records" as fact. A query with
    no FROM cannot have counted anything, and answering 0 is worse than
    admitting the table is missing.
    """


_FROM_RE = re.compile(r'\bFROM\s+"?([A-Za-z_][A-Za-z0-9_]*)"?', re.I)


def references_a_known_table(sql: str, schema: dict) -> bool:
    """True when the query actually reads a table the warehouse has."""
    known = {t.lower() for t in schema}
    return any(m.group(1).lower() in known for m in _FROM_RE.finditer(sql or ""))


async def generate_and_run_sql(
    question: str,
    *,
    history: Sequence[dict] = (),
    fetch_cap: Optional[int] = None,
) -> Tuple[str, List[str], List[list]]:
    """Generate, guard, and execute SQL with ONE retry feeding the error back.

    Returns (sql, columns, rows). Also reused by the report engine.
    """
    schema_text = format_schema(schema_cache.get(settings.duckdb_path))
    cap = fetch_cap if fetch_cap is not None else settings.sql_preview_row_cap + 1
    schema = schema_cache.get(settings.duckdb_path)
    raw = await _ask_sql(question, schema_text, history)
    if not references_a_known_table(raw, schema):
        # No FROM against anything we hold: the model is inventing a result
        # rather than reading data. Refuse instead of answering.
        raise NoSuchTable(
            "the question refers to data that is not in the local warehouse"
        )
    try:
        sql = guard_sql(raw)
        columns, rows = _execute(sql, cap)
        return sql, columns, rows
    except Exception as exc:  # one retry on guard/execution error (§8)
        raw2 = await _ask_sql(question, schema_text, history, previous_sql=raw, error=str(exc))
        sql2 = guard_sql(raw2)
        columns, rows = _execute(sql2, cap)
        return sql2, columns, rows


def _chart_messages(question: str, columns: Sequence[str]) -> List[dict]:
    system = (
        "Design a chart for a SQL result. Respond with ONLY a JSON object, "
        'no prose: {"type": "bar|line|scatter|pie|area", "x_key": "<column>", '
        '"y_keys": ["<column>", ...], "title": "<short title>", '
        '"stacked": true or false}. '
        f"Available columns: {', '.join(columns)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]


def _narrative_messages(
    question: str,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    history: Sequence[dict],
    total_rows: Optional[int] = None,
) -> List[dict]:
    shown = list(rows[:30])
    total = len(rows) if total_rows is None else total_rows
    sample = json.dumps(
        {"columns": list(columns), "rows": [list(r) for r in shown]}, default=str
    )
    system = (
        "You are a concise data analyst. Summarize the query result for the "
        "user in a short paragraph (plus brief bullets if helpful). Use only "
        "the numbers present in the result — never fabricate values.\n"
        # It answered "the live Salesforce check confirms…" from the synced
        # copy. Claiming a source you did not read is its own kind of wrong.
        "These rows come from the LOCAL SYNCED COPY of Salesforce, refreshed "
        "every 30 minutes — NOT a live query. Never say the result is live, "
        "direct from Salesforce, or current as of this moment.\n"
        # The model sees a SAMPLE. Left unsaid, it reports the sample size as
        # the answer: 314 rows came back and the summary said "29 records".
        "You are shown only the FIRST FEW ROWS of a larger result. The true "
        "row count is given below — quote THAT as the total, never the number "
        "of rows you can see, and do not present counts derived from the "
        "sample (how many are active, which department is biggest) as if they "
        "covered everything. Say they are from the first rows shown."
    )
    counted = (
        f"Total rows in the result: {total} (you are shown the first "
        f"{len(shown)})\n\n"
    )
    user = f"Question: {question}\n\n{counted}Result sample (JSON): {sample}"
    return [{"role": "system", "content": system}] + recent_turns(history, 6) + [
        {"role": "user", "content": user}
    ]


async def run_sql_engine(message: str, history: Sequence[dict], emit: Emit) -> str:
    if not os.path.exists(settings.duckdb_path):
        await emit("token", {"text": NO_DATA_MESSAGE})
        await emit("meta", {"route": "sql"})
        return NO_DATA_MESSAGE

    wants_export = bool(EXPORT_RE.search(message))
    fetch_cap = (settings.export_row_cap + 1) if wants_export else (settings.sql_preview_row_cap + 1)

    try:
        if wants_live_lookup(message):
            # The user asked for live data by name. Skip the warehouse.
            raise NoSuchTable("the user asked for a live Salesforce lookup")
        sql, columns, rows = await generate_and_run_sql(
            message, history=history, fetch_cap=fetch_cap
        )
    except NoSuchTable:
        # The warehouse does not carry this object. Ask Salesforce itself
        # rather than letting the model invent a number for it.
        from ..core import salesforce as sf_live

        if not (settings.sf_live_enabled and sf_live.configured()):
            text = (
                "That data is not in the local warehouse, and live Salesforce "
                "lookups are not configured, so I cannot answer it rather than "
                "guess. Add the object with "
                "`python -m syncworker.objects add <Object> --fields ...`."
            )
            await emit("token", {"text": text})
            await emit("meta", {"route": "sql"})
            return text

        await emit("status", {"text": "Not in the local copy — asking Salesforce…"})
        from .live_sf import (describe_rows, fetch_live, fetch_schema,
                              is_schema_question)

        # A question about the org's SHAPE (how many objects? which fields?)
        # has no SOQL answer — the model tried EntityDefinition with an
        # invented relationship and Salesforce refused it. Describe does.
        if is_schema_question(message):
            try:
                source, schema_text = await fetch_schema(message)
            except Exception as exc:
                text = f"Could not read the Salesforce schema ({exc})."
                await emit("token", {"text": text})
                await emit("meta", {"route": "sql"})
                return text
            parts: List[str] = []
            msgs = [
                {"role": "system", "content":
                 "Answer the user's question about their Salesforce org using "
                 "ONLY the schema below, read live from the Salesforce "
                 "describe API. Give the counts they asked for and list the "
                 "API names. Never invent objects or fields."},
                {"role": "user", "content":
                 f"Question: {message}\n\nLive Salesforce schema "
                 f"(via {source}):\n{schema_text[:60000]}"},
            ]
            async for kind, delta in llm.stream_chat_events(msgs, max_tokens=6000):
                await emit(kind, {"text": delta})
                if kind == "token":
                    parts.append(delta)
            await emit("meta", {"route": "sql"})
            return "".join(parts)

        try:
            soql, live_rows = await fetch_live(message, history)
        except Exception as exc:
            text = (
                "That data is not in the local warehouse and the live "
                f"Salesforce lookup failed ({exc}). I would rather say so than "
                "give you a number I did not read."
            )
            await emit("token", {"text": text})
            await emit("meta", {"route": "sql"})
            return text

        parts: List[str] = []
        msgs = [
            {"role": "system", "content":
             "Answer from these LIVE Salesforce records. State plainly that "
             "the figures come straight from Salesforce, not the local copy. "
             "Never invent values that are not in the rows."},
            {"role": "user", "content":
             f"Question: {message}\n\nSOQL run:\n{soql}\n\n"
             f"Rows ({len(live_rows)}):\n{describe_rows(live_rows)}"},
        ]
        async for kind, delta in llm.stream_chat_events(msgs, max_tokens=4000):
            await emit(kind, {"text": delta})
            if kind == "token":
                parts.append(delta)
        await emit("meta", {
            "route": "sql", "sql": soql,
            "data": live_rows[:settings.sql_preview_row_cap], "truncated": False,
        })
        return "".join(parts)

    preview, truncated = cap_rows(rows, settings.sql_preview_row_cap)  # 500-row meta cap
    # §10: data = array of row objects (≤500), truncated = TOP-LEVEL boolean.
    meta: dict = {
        "route": "sql",
        "sql": sql,
        "data": [dict(zip(columns, row)) for row in preview],
        "truncated": truncated,
    }

    if wants_export:
        exporter = export_csv if _CSV_RE.search(message) else export_xlsx
        path, _export_truncated = exporter(
            columns, rows, settings.reports_dir, slug=slugify(message, fallback="query-export")
        )
        # §10: exported files ride the report_files contract key
        # ([{filename, type, size}]) — the frontend renders those as
        # download cards; there is no bespoke export_file key.
        meta["report_files"] = [
            {
                "filename": path.name,
                "type": path.suffix.lstrip(".").lower(),
                "size": path.stat().st_size,
            }
        ]

    # Chart spec ONLY when the user asked for a chart/graph/plot (§8).
    if CHART_RE.search(message) and columns:
        spec_raw = await llm.chat_completion(
            _chart_messages(message, columns), temperature=0.0, max_tokens=2500
        )
        spec = parse_chart_spec(spec_raw, columns=columns)
        if spec is not None:
            meta["chart"] = spec.model_dump()
        # invalid spec → no "chart" key → table only; the model's JSON is
        # parsed and validated, NEVER executed.

    parts: List[str] = []
    async for token in llm.stream_chat_completion(
        _narrative_messages(message, columns, preview, history, total_rows=len(rows)),
        temperature=0.2,
        max_tokens=6000,
        thinking=False,
    ):
        parts.append(token)
        await emit("token", {"text": token})

    answer = "".join(parts).strip()
    if not answer:
        # Never leave the user staring at a table with nothing said about it.
        # A plain factual line is not a good answer, but it is an honest one,
        # and it tells them the rows below are real.
        answer = (
            f"Returned {len(rows)} row(s) across {len(columns)} column(s): "
            + ", ".join(columns[:12])
            + ("…" if len(columns) > 12 else "")
            + ". The full result is in the table below."
        )
        await emit("token", {"text": answer})

    # §10: the SINGLE meta event, emitted after the token stream, before done.
    await emit("meta", meta)
    return answer
