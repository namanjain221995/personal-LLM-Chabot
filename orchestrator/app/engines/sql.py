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
import time
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from . import NO_DATA_MESSAGE, recent_turns
from .. import llm
from ..config import settings
from ..core import chart_decision, org_brief, sf_dictionary
from ..core.chart_pipeline import ChartResult, build_chart
from ..core.exports import cap_rows, export_csv, export_xlsx, slugify
from ..core.schema_cache import format_schema, relevant_schema, schema_cache
from ..core.sql_guard import guard_sql

Emit = Callable[[str, dict], Awaitable[None]]

# The chart trigger now lives in core/chart_decision.py, where the mode
# (explicit | hybrid), the named-type parse and the false-positive filter
# sit together. Re-exported because the historical name is what callers and
# tests reach for, and it is still exactly the pattern that used to gate
# charting here.
CHART_RE = chart_decision.LEGACY_CHART_RE
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
    # This org's users write dates day-first (03-07-2026 = 3 July 2026). The
    # local and live engines answering the SAME question with DIFFERENT dates
    # is exactly the inconsistency that erodes trust in both.
    "- The user writes dates DAY-MONTH-YEAR: 03-07-2026 or 3/7/2026 means "
    "3 July 2026, NEVER March 7. Only ISO dates (2026-07-03) are year-first.\n"
    # Everything the warehouse stores is VARCHAR (19,519 of 19,520 columns).
    # Without the cast rule, ORDER BY on an amount sorts lexicographically and
    # "top 10 invoices by value" answers 999 over 27000 — no error, just wrong.
    + org_brief.SQL_HARD_RULES + "\n"
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
    # The dictionary says what things are CALLED; the brief says what they
    # MEAN. Knowing Interview__c exists does not stop a model counting the
    # 5,566 Initial Call rows as interviews.
    grounding = org_brief.grounding_for(question)
    user = f"Database schema:\n{schema_text}\n\nQuestion: {question}"
    if hint:
        user = f"{hint}\n\n{user}"
    if grounding:
        user = f"{grounding}\n\n{user}"
    if error is not None:
        user += (
            f"\n\nYour previous SQL failed.\nPrevious SQL:\n{previous_sql}\n"
            f"Error:\n{error}\n\nWrite a corrected single SELECT statement."
        )
    # Earlier turns already carry queries that WORKED for this user, and a
    # "(Clarified: ...)" line records a reading they explicitly chose. Reuse
    # both rather than re-deriving the join from scratch each turn.
    user += (
        "\n\nThis conversation may already contain SQL that answered an "
        "earlier question, and any line beginning '(Clarified:' is a reading "
        "the user picked themselves. Follow those — reuse the joins and "
        "filters that already worked here, and keep the same reading unless "
        "this question changes it."
    )
    messages = (
        [{"role": "system", "content": _SQL_SYSTEM}]
        + recent_turns(history, 6)
        + [{"role": "user", "content": user}]
    )
    # No reasoning pass: writing SQL from a schema is translation, and the
    # thinking tokens come out of the same budget as the statement. With
    # thinking on, an 11,500-token prompt produced 121 seconds of silence and
    # an empty reply.
    raw = await llm.chat_completion(
        messages, temperature=0.1, max_tokens=6000, thinking=False
    )
    return extract_sql(raw)


class WarehouseBusy(RuntimeError):
    """The warehouse file is write-locked by the sync-worker right now.

    DuckDB allows one writing process OR many readers; while a sync batch is
    being written, a read-only connect fails with "Could not set lock". The
    worker holds the lock per-write (milliseconds) since 2026-08-06, so a
    short retry almost always clears it — this exception is the residual
    case, handled by falling back to LIVE Salesforce instead of showing the
    user a raw IO error.
    """


def _is_lock_error(exc: Exception) -> bool:
    return "lock" in str(exc).lower()


#: How long a chat query waits for the sync-worker's per-write lock.
_LOCK_WAIT_SECONDS = 4.0
_LOCK_WAIT_STEP = 0.25


def _connect_warehouse(duckdb):
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        try:
            return duckdb.connect(
                settings.duckdb_path,
                read_only=True,
                config={
                    "enable_external_access": False,
                    "autoinstall_known_extensions": False,
                    "autoload_known_extensions": False,
                },
            )
        except duckdb.Error as exc:
            if not _is_lock_error(exc):
                raise
            if time.monotonic() >= deadline:
                raise WarehouseBusy(
                    "the local warehouse is being refreshed by the sync worker"
                ) from exc
            time.sleep(_LOCK_WAIT_STEP)


def _execute(sql: str, fetch_cap: int) -> Tuple[List[str], List[list]]:
    import duckdb  # lazy

    # §8/§12: read-only, and NO external access — DuckDB's read_csv/read_blob/
    # glob/httpfs table functions would otherwise let a guard-approved SELECT
    # read arbitrary host files or reach the network. With this config DuckDB
    # raises PermissionException for every such function.
    con = _connect_warehouse(duckdb)
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


class EmptySql(RuntimeError):
    """The model returned no statement at all, twice.

    Distinct from NoSuchTable on purpose. "The warehouse does not hold this"
    and "the model failed to answer" are different facts, and conflating them
    sent the second case to live Salesforce — which cannot know the model
    failed, and answered from the wrong object with full confidence.
    """


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
    try:
        schema = schema_cache.get(settings.duckdb_path)
    except Exception as exc:
        # No schema AND no stale copy — with the file locked there is nothing
        # to ground a SQL prompt on; the caller answers from live Salesforce.
        if _is_lock_error(exc):
            raise WarehouseBusy(str(exc)) from exc
        raise
    # The warehouse mirrors the whole org (900+ tables incl. Share/History
    # shadows and setup objects); ground the prompt on the relevant slice so
    # the business tables stay prominent. Validation below still accepts any
    # table that truly exists.
    # The schema slice is capped, so pin the tables the matched metric's own
    # definition joins — otherwise a metric can be injected while the table it
    # names is ranked out of the prompt.
    sliced = relevant_schema(
        schema, question, must_include=org_brief.tables_for(question)
    )
    schema_text = format_schema(sliced)
    # Column types alone cannot say which lookup points where, so the model
    # guessed join paths that match zero rows. Spell the edges out.
    joins = sf_dictionary.join_map(list(sliced))
    if joins:
        schema_text = f"{schema_text}\n\n{joins}"
    cap = fetch_cap if fetch_cap is not None else settings.sql_preview_row_cap + 1
    raw = await _ask_sql(question, schema_text, history)
    if not raw.strip():
        # The model spent its whole generation budget reasoning and emitted no
        # statement. That used to read as "no FROM" -> NoSuchTable -> ask live
        # Salesforce, which then answered off whatever object the dictionary
        # had suggested. An empty reply is a retry, never a routing decision.
        raw = await _ask_sql(
            question,
            schema_text,
            history,
            error=(
                "Your previous reply contained no SQL statement. Do not "
                "explain and do not reason in the reply — output the single "
                "SELECT statement and nothing else."
            ),
        )
    if not raw.strip():
        raise EmptySql("the model did not produce a SQL statement")
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
    except WarehouseBusy:
        # A locked file is not a SQL mistake — retrying the model cannot help.
        raise
    except Exception as exc:  # one retry on guard/execution error (§8)
        raw2 = await _ask_sql(question, schema_text, history, previous_sql=raw, error=str(exc))
        sql2 = guard_sql(raw2)
        columns, rows = _execute(sql2, cap)
        return sql2, columns, rows


async def _ask_chart_model(messages: List[dict]) -> str:
    return await llm.chat_completion(messages, temperature=0.0, max_tokens=2500)


async def attach_chart(
    meta: dict,
    message: str,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    title: str = "",
) -> Optional[ChartResult]:
    """Attach `meta.chart` (and `meta.chart_data` when needed). Never raises.

    `meta.chart` keeps its historical shape. `meta.chart_data` is new and
    OPTIONAL: it appears only when the chart draws something other than the
    query result verbatim — histogram bins, or a funnel in trusted stage
    order — so `meta.data` stays exactly what the SQL returned and the Data
    tab is never silently re-sorted. Consumers written before this key fall
    back to `meta.data`, which is all old payloads carry.
    """
    result = await build_chart(
        message,
        columns,
        rows,
        mode=settings.chart_trigger_mode,
        ask_model=_ask_chart_model,
        title=title,
    )
    if result is None:
        return None
    meta["chart"] = result.spec.wire_dump()
    if result.derived:
        meta["chart_data"] = [dict(zip(result.columns, row)) for row in result.rows]
    return result


#: A text column with at most this many distinct values is a CATEGORY worth
#: counting ("Cleared"/"Failed", stage names, statuses). Above it, counting
#: every value produces noise rather than an answer.
_MAX_CATEGORY_VALUES = 15

#: Columns considered for the breakdown. A wide Salesforce result can have
#: dozens; the first ones are the ones the query actually selected for.
_MAX_PROFILED_COLUMNS = 20


def deterministic_summary(
    columns: Sequence[str], rows: Sequence[Sequence]
) -> dict:
    """Exact figures over EVERY row, computed here rather than by the model.

    This is the fix for a real wrong answer (owner report 2026-08-11). Asked for
    slot 128's mocks, the summary said "Total Mocks: 3, Cleared: 2, Failed: 0,
    Pass Ratio: 0.67" — three statements that cannot all be true, because they
    were read off the 30 sample rows rather than computed over the 18-row (and
    in other cases 500-row) result.

    The prompt had ALWAYS told the model not to do that. It did it anyway, which
    is the whole lesson: an instruction is not a mechanism. Counts, totals and
    ratios now arrive pre-computed and the model is told to quote them.

    Returns exact row counts, per-numeric-column totals, and value counts for
    low-cardinality columns — which is precisely the "how many cleared, how many
    failed, what is the ratio" shape that was being guessed at.
    """
    names = list(columns)[:_MAX_PROFILED_COLUMNS]
    total = len(rows)
    out: dict = {"total_rows": total, "counts_cover": "every row in the result"}
    if not names or not rows:
        return out

    numeric: dict = {}
    categorical: dict = {}
    for index, name in enumerate(names):
        values = [r[index] for r in rows if index < len(r)]
        present = [v for v in values if v is not None and v != ""]
        if not present:
            continue

        numbers = []
        for value in present:
            if isinstance(value, bool):
                numbers = []
                break
            if isinstance(value, (int, float)):
                numbers.append(float(value))
            elif isinstance(value, str):
                try:
                    numbers.append(float(value.replace(",", "")))
                except ValueError:
                    numbers = []
                    break
            else:
                numbers = []
                break

        if numbers and len(numbers) == len(present):
            numeric[name] = {
                "sum": round(sum(numbers), 4),
                "average": round(sum(numbers) / len(numbers), 4),
                "min": round(min(numbers), 4),
                "max": round(max(numbers), 4),
                "non_empty": len(numbers),
            }
            continue

        distinct: dict = {}
        for value in present:
            key = str(value)
            distinct[key] = distinct.get(key, 0) + 1
            if len(distinct) > _MAX_CATEGORY_VALUES:
                break
        if len(distinct) <= _MAX_CATEGORY_VALUES:
            denominator = sum(distinct.values())
            categorical[name] = {
                "denominator": denominator,
                "values": [
                    {
                        "value": value,
                        "count": count,
                        # Stated with its denominator on purpose: a percentage
                        # whose population is unclear is how "0.67" appeared
                        # next to counts that did not add up to it.
                        "percent_of_non_empty": round(
                            100.0 * count / denominator, 2
                        ),
                    }
                    for value, count in sorted(
                        distinct.items(), key=lambda kv: -kv[1]
                    )
                ],
                "empty_or_null": total - denominator,
            }

    if numeric:
        out["numeric_totals"] = numeric
    if categorical:
        out["value_counts"] = categorical
    return out


def _narrative_messages(
    question: str,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    history: Sequence[dict],
    total_rows: Optional[int] = None,
    computed: Optional[dict] = None,
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
        "You are shown only the FIRST FEW ROWS of a larger result, as an "
        "ILLUSTRATION of the shape of the data.\n"
        # The instruction below used to be the ONLY defence, and it failed:
        # "Total Mocks: 3, Cleared: 2, Failed: 0, Pass Ratio: 0.67" was read
        # off the sample and is not internally consistent. The computed block
        # is the mechanism; this paragraph now just points at it.
        "EVERY number you state — every count, total, average, percentage and "
        "ratio — MUST be taken from the 'Computed figures' block below. It was "
        "calculated in code over EVERY row of the result, not over the sample. "
        "Do not count, add up, or work out a proportion from the sample rows "
        "yourself. If a figure you want is not in the computed block, say you "
        "do not have it rather than deriving it.\n"
        "When you give a percentage or a ratio, state what it is a percentage "
        "OF, using the denominator given in the computed block. Do not present "
        "a ratio alongside counts that do not add up to its denominator.\n"
        + org_brief.ANSWER_RULES
    )
    counted = (
        f"Total rows in the result: {total} (you are shown the first "
        f"{len(shown)} as an illustration)\n\n"
    )
    figures = ""
    if computed:
        figures = (
            "Computed figures (AUTHORITATIVE — calculated in code over every "
            "row; quote these):\n"
            + json.dumps(computed, default=str)[:6000]
            + "\n\n"
        )
    user = (
        f"Question: {question}\n\n{counted}{figures}"
        f"Result sample (illustration only, JSON): {sample}"
    )
    return [{"role": "system", "content": system}] + recent_turns(history, 6) + [
        {"role": "user", "content": user}
    ]


async def run_sql_engine(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    *,
    force_live: bool = False,
) -> str:
    if not force_live and not os.path.exists(settings.duckdb_path):
        await emit("token", {"text": NO_DATA_MESSAGE})
        await emit("meta", {"route": "sql"})
        return NO_DATA_MESSAGE

    wants_export = bool(EXPORT_RE.search(message))
    fetch_cap = (settings.export_row_cap + 1) if wants_export else (settings.sql_preview_row_cap + 1)

    try:
        if force_live:
            # The composer's "Live Salesforce" toggle: every answer comes
            # straight from the org, whatever the warehouse holds.
            raise NoSuchTable("the Live Salesforce toggle is on")
        if wants_live_lookup(message):
            # The user asked for live data by name. Skip the warehouse.
            raise NoSuchTable("the user asked for a live Salesforce lookup")
        sql, columns, rows = await generate_and_run_sql(
            message, history=history, fetch_cap=fetch_cap
        )
    except EmptySql:
        # Not a routing problem — the warehouse has the data and the model
        # failed to ask for it. Say so; going live here would answer a
        # question nobody successfully wrote a query for.
        text = (
            "I could not turn that into a query — the request came back empty "
            "twice. Try asking for one thing at a time, or name the object "
            "you mean (training, interviews, invoices)."
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "sql"})
        return text
    except (NoSuchTable, WarehouseBusy) as reason:
        # Two roads to the same place: the warehouse does not carry this
        # object (NoSuchTable — ask Salesforce rather than let the model
        # invent a number), or the warehouse file is briefly write-locked by
        # the sync worker (WarehouseBusy — the raw "Could not set lock" IO
        # error used to reach the user's screen here).
        from ..core import salesforce as sf_live

        busy = isinstance(reason, WarehouseBusy)
        if not (settings.sf_live_enabled and sf_live.configured()):
            if busy:
                text = (
                    "The local Salesforce copy is being refreshed by the sync "
                    "worker right now — please try again in a few seconds."
                )
            else:
                text = (
                    "That data is not in the local warehouse, and live Salesforce "
                    "lookups are not configured, so I cannot answer it rather than "
                    "guess. Add the object with "
                    "`python -m syncworker.objects add <Object> --fields ...`."
                )
            await emit("token", {"text": text})
            await emit("meta", {"route": "sql"})
            return text

        if force_live:
            status = "Asking Salesforce live…"
        elif busy:
            status = "Local copy is being refreshed — asking Salesforce live…"
        else:
            status = "Not in the local copy — asking Salesforce…"
        await emit("status", {"text": status})
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
            if force_live:
                # With the Live toggle on, the warehouse was skipped BY
                # CHOICE — "not in the local warehouse" would be a lie here.
                text = (
                    f"The live Salesforce lookup failed ({exc}). I would "
                    "rather say so than give you a number I did not read. "
                    "Turning off Live Salesforce will answer from the "
                    "synced copy instead."
                )
            else:
                text = (
                    "That data is not in the local warehouse and the live "
                    f"Salesforce lookup failed ({exc}). I would rather say so "
                    "than give you a number I did not read."
                )
            await emit("token", {"text": text})
            await emit("meta", {"route": "sql"})
            return text

        parts: List[str] = []
        # Same mechanism as the warehouse branch: counts and ratios are
        # computed over every returned row, and the model quotes them.
        live_columns = (
            [c for c in live_rows[0] if not isinstance(live_rows[0][c], (dict, list))]
            if live_rows and isinstance(live_rows[0], dict)
            else []
        )
        live_computed = deterministic_summary(
            live_columns,
            [[r.get(c) for c in live_columns] for r in live_rows],
        )
        msgs = [
            {"role": "system", "content":
             "Answer from these LIVE Salesforce records. State plainly that "
             "the figures come straight from Salesforce, not the local copy. "
             "Never invent values that are not in the rows.\n"
             "Every count, total, percentage and ratio you state MUST come "
             "from the 'Computed figures' block — it was calculated in code "
             "over every returned row. Do not work figures out from the rows "
             "yourself, and always say what a percentage is a percentage of."},
            {"role": "user", "content":
             f"Question: {message}\n\nSOQL run:\n{soql}\n\n"
             "Computed figures (AUTHORITATIVE):\n"
             f"{json.dumps(live_computed, default=str)[:6000]}\n\n"
             f"Rows ({len(live_rows)}):\n{describe_rows(live_rows)}"},
        ]
        async for kind, delta in llm.stream_chat_events(msgs, max_tokens=4000):
            await emit(kind, {"text": delta})
            if kind == "token":
                parts.append(delta)
        # A thinking model draws its reasoning from the SAME budget as the
        # answer, and over a wide result set it can spend the lot and stream
        # NOTHING — leaving a data table with an empty bubble above it. The
        # warehouse branch below has always had this fallback; the live branch
        # did not, and Salesforce Intelligence Mode reaches it far more often.
        if not "".join(parts).strip():
            fallback = (
                f"Salesforce returned {len(live_rows)} row(s) for this question. "
                "The records are in the table below — I could not summarize them "
                "in words this time, but the data itself is what the org returned."
                if live_rows
                else "Salesforce returned no matching records for this question. "
                "The query ran successfully — this is an empty result, not a "
                "failed lookup."
            )
            parts.append(fallback)
            await emit("token", {"text": fallback})
        live_preview = live_rows[:settings.sql_preview_row_cap]
        live_meta: dict = {
            "route": "sql", "sql": soql,
            "data": live_preview, "truncated": False,
        }
        # Live SOQL results chart exactly like warehouse results — the
        # pipeline only ever sees (columns, rows), never where they came
        # from. Scalar columns only: a nested Salesforce sub-object is not
        # an axis.
        if live_preview and isinstance(live_preview[0], dict):
            live_cols = [
                c for c in live_preview[0]
                if not isinstance(live_preview[0][c], (dict, list))
            ]
            await attach_chart(
                live_meta, message, live_cols,
                [[r.get(c) for c in live_cols] for r in live_preview],
            )
        await emit("meta", live_meta)
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

    # Chart spec (§8). In the default `explicit` trigger mode this fires on
    # exactly the same requests it always did; `hybrid` additionally charts
    # a few deterministic shapes. Never raises — a chart problem must not
    # cost the user the answer that is about to stream.
    await attach_chart(meta, message, columns, preview)

    # Computed over `rows` — the FULL result — not over `preview`. That
    # distinction is the entire point: the preview is capped at 500 and the
    # sample the model sees is capped at 30, and both were being used as the
    # population for counts and ratios.
    computed = deterministic_summary(columns, rows)

    parts: List[str] = []
    async for token in llm.stream_chat_completion(
        _narrative_messages(
            message,
            columns,
            preview,
            history,
            total_rows=len(rows),
            computed=computed,
        ),
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
