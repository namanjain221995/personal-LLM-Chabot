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
import logging
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
    # That rule says how to READ the user's date. This one says how to WRITE
    # it, and it is a separate mistake: asked about "17 Aug 2026" the model
    # understood the date correctly and then emitted
    # TRY_CAST('17-08-2026' AS DATE), which DuckDB evaluates to NULL. The
    # predicate could not match anything, the query returned no rows, and the
    # answer reported that the people asked about had no interviews at all.
    # Names in this org are free text typed by many people: the warehouse holds
    # 'Khushi ghorawath' with a lower-case surname. An exact match on one name
    # in a five-name question returns 0 for that person and correct figures for
    # the rest, so the answer looks authoritative and is wrong about someone.
    "- Match a PERSON or RECORD NAME case-insensitively and loosely, never with "
    "= or IN: use ILIKE with wildcards, e.g. Name ILIKE '%khushi%ghorawath%', "
    "or compare lower(Name). Casing, spacing and middle names vary row to row, "
    "and an exact match silently yields 0 for that one person while everybody "
    "else's figures look right.\n"
    # The org's picklists are exact strings the users never quote precisely:
    # asked about "background checks pending payment verification", the model
    # filtered on that phrase verbatim, while the stored value is 'Payment
    # Verification Pending' — zero rows, no error, a silently wrong answer.
    "- STATUS/TYPE/PICKLIST filters: when the prompt lists the column's exact "
    "values (shown as [a | b]), use one of those verbatim. When it does NOT, "
    "never guess an exact literal — match loosely, e.g. "
    "Status__c ILIKE '%payment%verification%pending%' OR reorderings of the "
    "same words, so a differently-worded picklist value still matches.\n"
    "- WRITE every date literal in the SQL as ISO, e.g. DATE '2026-08-17' or "
    "TRY_CAST(col AS DATE) = DATE '2026-08-17'. A day-first literal like "
    "'17-08-2026' is NOT parseable: TRY_CAST returns NULL, the comparison is "
    "never true, and you get an empty result instead of an error. Translate "
    "the user's day-first date into ISO yourself before writing it.\n"
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
    grounding_question: Optional[str] = None,
) -> str:
    """Write ONE SELECT for `question`.

    `grounding_question` is what the KNOWLEDGE layers match on, and it is a
    separate argument because the agent route asks in someone else's words.
    A plan step's input is written by the planner ("Count internal interviews
    grouped by decision"), so the user's own sentence — the names, the domain
    nouns the brain packs trigger on, the words that say which object a person
    lives on — is gone by the time the SQL is written. Asked how many internal
    interviews five named recruiters had completed, the agent route answered
    "no data available for the specific individuals" and charted a lone
    Human_Decision__c of 0, while the SAME question at a lower effort level
    (which skips the planner) answered correctly. Grounding on both texts is
    what makes the two routes agree; `attach_chart` already did exactly this
    for chart intent."""
    # The org dictionary maps what people SAY to what the API calls it. Left
    # to guess, the model writes a plausible-looking field name that returns
    # no rows instead of erroring — a silently wrong answer.
    from ..core.sf_dictionary import hint_for

    # Everything below keys on the user's words when they differ from the
    # instruction — see `grounding_question`.
    ground = grounding_question or question
    hint = hint_for(ground)
    # The dictionary says what things are CALLED; the brief says what they
    # MEAN. Knowing Interview__c exists does not stop a model counting the
    # 5,566 Initial Call rows as interviews.
    grounding = org_brief.grounding_for(ground)
    # Learn-from-chat: a join the user already thumbs-up'd for a similar
    # question beats one re-derived from scratch (core/learned_examples.py).
    from ..core import learned_examples

    examples = learned_examples.block_for(ground)
    user = f"Database schema:\n{schema_text}\n\nQuestion: {question}"
    # Closest to the question, because it is the most specific thing we know:
    # not a rule about people in general, but who THESE people are.
    people = who_these_people_are(ground)
    if people:
        user = f"{people}\n\n{user}"
    if hint:
        user = f"{hint}\n\n{user}"
    if examples:
        user = f"{examples}\n\n{user}"
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


#: Where a person can be recorded in this org, and what that means for a
#: question about them. Ordered: the first hit is what the block leads with.
#: `Account` needs its record type because the SAME human is often BOTH — a
#: recruiter has an Account row of record type 'Recruiter' as well as a
#: Recruiter__c row, and only the latter is what an interview points at.
_PERSON_SOURCES = (
    (
        "Recruiter__c",
        "SELECT Name FROM Recruiter__c WHERE lower(Name) LIKE ?",
        "staff (Recruiter__c) — interviews they RAN link through "
        "Internal_Interview__c.Interviewer__c",
    ),
    (
        "Account",
        "SELECT a.Name FROM Account a JOIN RecordType rt ON a.RecordTypeId = rt.Id "
        "WHERE rt.Name = 'Person Account' AND lower(a.Name) LIKE ?",
        "a candidate (Account, record type 'Person Account') — interviews they "
        "SAT link through Internal_Interview__c.Candidate__c",
    ),
)

#: A question naming more people than this is a report, not a lookup; resolving
#: each one is not worth the round trips.
_MAX_RESOLVED_PEOPLE = 8


def people_in_question(question: str) -> List[str]:
    """Capitalised multi-word names the question appears to be about.

    Deliberately conservative: two adjacent capitalised words. A single one is
    far more often a product, an object or the first word of a sentence.
    """
    from ..core import org_brief

    out: List[str] = []
    for match in re.finditer(r"\b([A-Z][a-z]{2,})\s+([A-Z][a-z]{2,})\b", question or ""):
        first, last = match.group(1), match.group(2)
        if first.lower() in org_brief._NOT_A_NAME or last.lower() in org_brief._NOT_A_NAME:
            continue
        name = f"{first} {last}"
        if name not in out:
            out.append(name)
    return out[:_MAX_RESOLVED_PEOPLE]


def resolve_people(question: str) -> List[dict]:
    """Who each named person actually is, looked up in the warehouse.

    → [{"asked", "object", "meaning", "matches": [stored names]}] — one entry
    per name that matched anything. `matches` has one element for a person the
    data knows unambiguously, several when the name genuinely fits more than
    one stored row. Empty list when the question names nobody or the warehouse
    is unreachable: grounding is an optimisation, never fatal.

    This is the structured half; `who_these_people_are` renders it for the SQL
    prompt, and the Salesforce planner consumes it directly — the same facts
    decide both what to JOIN and whether to ASK. Before it did, the planner ran
    a live SOSL over only the FIRST capitalised token: asked about five staff
    by full name, it searched "Jayesh", got every Jayesh in the org plus fuzzy
    noise, labelled them all "candidates", and interrupted the user to choose —
    for names the warehouse matches exactly.
    """
    names = people_in_question(question)
    if not names:
        return []
    import duckdb  # lazy, same as _execute

    try:
        con = _connect_warehouse(duckdb)
    except Exception:  # noqa: BLE001
        return []
    out: List[dict] = []
    try:
        for name in names:
            pattern = "%" + "%".join(p.lower() for p in name.split()) + "%"
            for table, sql, meaning in _PERSON_SOURCES:
                try:
                    rows = con.execute(sql, [pattern]).fetchall()
                except Exception:  # noqa: BLE001 — a missing table is not fatal
                    continue
                if rows:
                    out.append(
                        {
                            "asked": name,
                            "object": table,
                            "meaning": meaning,
                            "matches": sorted({str(r[0]) for r in rows})[:4],
                        }
                    )
                    break
    finally:
        con.close()
    return out


def who_these_people_are(question: str) -> str:
    """`resolve_people`, rendered for a prompt.

    "How many internal interviews has X completed" is genuinely ambiguous —
    X may be the candidate who sat them or the employee who ran them, and the
    two live on different objects. The model resolved it by guessing, and
    guessed candidate for five people who are staff: it reported that they had
    none when they had 84 between them, then on a later run reported four of
    five correctly and a silent 0 for the fifth, whose surname is stored
    lower-case. A rule in the prompt cannot know who these particular people
    are; one indexed lookup can. Returns "" when the question names nobody.
    """
    found = resolve_people(question)
    if not found:
        return ""
    lines = []
    for person in found:
        stored = ", ".join(person["matches"][:3])
        lines.append(f"- {person['asked']} is {person['meaning']}. Stored as: {stored}")
    return (
        "Who the people named in this question are, looked up in the warehouse "
        "just now — treat this as fact and join accordingly:\n"
        + "\n".join(lines)
        + "\nMatch these names case-insensitively (ILIKE); the stored spelling "
        "above is what the data actually contains."
    )


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
    grounding_question: Optional[str] = None,
) -> Tuple[str, List[str], List[list]]:
    """Generate, guard, and execute SQL with ONE retry feeding the error back.

    Returns (sql, columns, rows). Also reused by the report and agent engines.

    `grounding_question` is the USER's wording when the caller is asking in
    someone else's — the agent's plan steps, a report section instruction. The
    schema slice, the dictionary hint, the brain packs and the person lookup
    all key on it, so an agent-routed question is grounded exactly as well as
    the same question asked directly.
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
    # The slice is chosen from BOTH: the user's words carry the domain nouns
    # the table aliases and brain packs key on, the instruction carries what
    # this particular step needs.
    ground = f"{grounding_question}\n{question}" if grounding_question else question
    sliced = relevant_schema(
        schema, ground, must_include=org_brief.tables_for(ground)
    )
    schema_text = format_schema(sliced)
    # Column types alone cannot say which lookup points where, so the model
    # guessed join paths that match zero rows. Spell the edges out.
    joins = sf_dictionary.join_map(list(sliced))
    if joins:
        schema_text = f"{schema_text}\n\n{joins}"
    # The default cap is the SUMMARY cap, not the 500-row preview cap: the
    # deterministic figures are computed over what is fetched, and fetching
    # 501 rows of a 33,000-row result made "authoritative" totals cover 1.5%
    # of the data. The preview stays 500 (a UI concern, applied by callers).
    cap = fetch_cap if fetch_cap is not None else settings.sql_summary_row_cap + 1
    raw = await _ask_sql(
        question, schema_text, history, grounding_question=ground
    )
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
        raw2 = await _ask_sql(
            question, schema_text, history, previous_sql=raw,
            error=_enriched_error(str(exc), raw, schema),
            grounding_question=ground,
        )
        sql2 = guard_sql(raw2)
        columns, rows = _execute(sql2, cap)
        return sql2, columns, rows


#: DuckDB's binder error for a missing column, e.g.
#:   Binder Error: Table "p" does not have a column named "Status__c"
_BINDER_COLUMN_RE = re.compile(
    r'Table "(?P<alias>[^"]+)" does not have a column named "(?P<column>[^"]+)"'
)
#: FROM/JOIN <table> [AS] <alias> — enough to resolve which real table the
#: failing alias referred to in the SQL we just ran.
_ALIAS_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+"?(?P<table>[A-Za-z_][A-Za-z0-9_]*)"?\s+(?:AS\s+)?'
    r'(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\b',
    re.I,
)


def _enriched_error(error: str, failed_sql: str, schema: dict) -> str:
    """The execution error, plus the failing table's REAL column list.

    The retry used to get only the error string. For a hallucinated column
    ("p.Status__c = 'Paid'" — the real column is Payment_Status__c) that tells
    the model WHAT failed but not what to use instead, so it guessed again,
    failed again, and the raw binder error reached the user as a red pill.
    The alias is resolved from the SQL we just ran, and the retry gets the
    whole truth about that one table.
    """
    match = _BINDER_COLUMN_RE.search(error)
    if not match:
        return error
    alias = match.group("alias").lower()
    table = next(
        (
            m.group("table")
            for m in _ALIAS_RE.finditer(failed_sql or "")
            if m.group("alias").lower() == alias
        ),
        # No alias in the SQL: DuckDB names the table itself in that case.
        match.group("alias"),
    )
    columns = schema.get(table) or next(
        (cols for name, cols in schema.items() if name.lower() == table.lower()),
        None,
    )
    if not columns:
        return error
    listed = ", ".join(name for name, _t in columns)
    return (
        f"{error}\n\nThe COMPLETE column list of {table} is: {listed}\n"
        f"Use only these exact names for {table}. Do not invent columns."
    )


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
    if not rows:
        # An EMPTY result is the most dangerous shape this function handles,
        # because it reads as an answer. Asked how many internal interviews
        # five named people had completed, a query that joined the interviewer
        # to the wrong object returned nothing, and the reply was "there are no
        # internal interview records in the synced data for [them]" — a claim
        # about the org, made from the silence of a query the model wrote
        # itself. They had 84 between them.
        #
        # Nothing about zero rows distinguishes "these records do not exist"
        # from "this query did not find them", so the model is not asked to
        # tell them apart — it is told, in the authoritative block it is
        # required to quote from, that it cannot.
        out["empty_result"] = True
        out["what_zero_rows_means"] = (
            "The query matched no rows. This is NOT evidence that the records "
            "do not exist: a wrong join, a name spelled differently in the "
            "data, an unparseable date literal or a too-narrow filter all "
            "return an empty result rather than an error. Say the query found "
            "nothing and say what it looked for, and where a person or record "
            "was named, say that the name may be stored differently or the "
            "link may run through another object. Never state that the "
            "business has no such records."
        )
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

    # A GROUPED aggregate ("status, count" rows) profiled as if rows were
    # records reads as nonsense: asked how many envelopes were completed vs
    # voided (rows: Completed 72, Voided 33), value_counts said "Completed: 1
    # (50%)" and the narrative answered "2 envelopes". When every label is
    # unique and there is exactly one numeric column, pair each label with
    # its value and drop the meaningless occurrence counts.
    if (
        total > 1
        and len(numeric) == 1
        and len(categorical) == 1
        and all(
            profile["denominator"] == len(profile["values"])
            for profile in categorical.values()
        )
    ):
        [(value_name, value_stats)] = list(numeric.items())
        [label_name] = list(categorical.keys())
        label_index = names.index(label_name)
        value_index = names.index(value_name)
        breakdown = {}
        for row in rows[:40]:
            if label_index < len(row) and value_index < len(row):
                breakdown[str(row[label_index])] = row[value_index]
        out["row_breakdown"] = breakdown
        if total > 40:
            out["row_breakdown_truncated"] = f"first 40 of {total} rows"
        out["counts_cover"] = (
            f"each result ROW pairs one {label_name} with its {value_name} "
            f"value (see row_breakdown); the sum across rows is "
            f"{value_stats['sum']}. total_rows is the number of rows, not a "
            "count of records."
        )
        del out["value_counts"]

    # A ONE-ROW, all-numeric result is an AGGREGATE — its values are the
    # answer. Left as-is, the summary said "total_rows: 1" next to
    # "sum: 866.0" for `SELECT count(*) ... WHERE Status = 'Locked'`, and the
    # narrative model either answered `1` (the sf_intel path did exactly that,
    # 2026-08-17) or reasoned aloud about the contradiction on the way to 866.
    # State the aggregate meaning explicitly and promote a count-shaped column
    # to the record count.
    if total == 1 and numeric and not categorical:
        out["aggregate_result"] = {
            name: stats["sum"] for name, stats in numeric.items()
        }
        out["counts_cover"] = (
            "a single AGGREGATE result row — aggregate_result holds the "
            "answer; total_rows is the number of result rows, not of records"
        )
        # \btotal\b: a bare `AS total` on a count is common; total_amount is a
        # SUM and must not be promoted to a record count.
        count_like = [n for n in numeric if re.search(r"count|\bcnt\b|how_many|\btotal\b", n, re.I)]
        if count_like:
            out["record_count"] = int(numeric[count_like[0]]["sum"])
    return out


def _chart_line(chart_attached: bool) -> str:
    """What the narration is told about charts. A MECHANISM, not a hope.

    Asked for a "bar chat" (typo), no chart was attached, and the model
    helpfully drew the bar chart itself — █████ characters in a ```text block.
    Both branches are stated because both failure modes are real: with a chart
    attached, the model describes it in ASCII anyway "for clarity"; without
    one, it improvises.
    """
    if chart_attached:
        return (
            "A REAL, interactive chart of this result is already rendered "
            "directly beneath your answer. Do not draw a chart of any kind in "
            "text, do not repeat the per-category numbers as a pseudo-chart, "
            "and do not say a chart could not be shown — it is shown. One "
            "sentence pointing at it is enough.\n"
        )
    return (
        "NEVER draw a chart out of text characters — no ASCII/Unicode bars "
        "(█ ▓ ■ #), no ```text blocks arranged as a graph, no emoji charts. "
        "If the user asked for a chart that is not attached, give the figures "
        "as a normal list and say the data table below can be charted on "
        "request — do not imitate a chart in text.\n"
    )


def _narrative_messages(
    question: str,
    columns: Sequence[str],
    rows: Sequence[Sequence],
    history: Sequence[dict],
    total_rows: Optional[int] = None,
    computed: Optional[dict] = None,
    chart_attached: bool = False,
) -> List[dict]:
    # 120, not 30: the computed block stays the authority for every NUMBER,
    # but a wider sample is what lets the model describe the data honestly
    # instead of generalising from a handful of rows.
    shown = list(rows[:120])
    total = len(rows) if total_rows is None else total_rows
    sample = json.dumps(
        {"columns": list(columns), "rows": [list(r) for r in shown]}, default=str
    )
    system = (
        "You are a concise data analyst. Summarize the query result for the "
        "user in a short paragraph (plus brief bullets if helpful). Use only "
        "the numbers present in the result — never fabricate values.\n"
        + _chart_line(chart_attached)
        # It answered "the live Salesforce check confirms…" from the synced
        # copy. Claiming a source you did not read is its own kind of wrong.
        + "These rows come from the LOCAL SYNCED COPY of Salesforce, refreshed "
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
    fetch_cap = (settings.export_row_cap + 1) if wants_export else (settings.sql_summary_row_cap + 1)

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
        # The chart is decided BEFORE the narration streams, for the same
        # reason the warehouse branch does it: the narration can only be told
        # "a real chart is rendered below" if that is already a fact. This
        # branch used to attach afterwards, which left the model free to draw
        # ASCII bars over a result that was about to get a real chart.
        live_preview = live_rows[:settings.sql_preview_row_cap]
        live_meta: dict = {
            "route": "sql", "sql": soql,
            "data": live_preview, "truncated": False,
        }
        if live_preview and isinstance(live_preview[0], dict):
            await attach_chart(
                live_meta, message, live_columns,
                [[r.get(c) for c in live_columns] for r in live_preview],
            )
        msgs = [
            {"role": "system", "content":
             "Answer from these LIVE Salesforce records. State plainly that "
             "the figures come straight from Salesforce, not the local copy. "
             "Never invent values that are not in the rows.\n"
             + _chart_line(bool(live_meta.get("chart"))) +
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
        # live_meta (chart included) was built before the narration streamed,
        # so the model was told the truth about whether a chart exists.
        await emit("meta", live_meta)
        return "".join(parts)

    except Exception as exc:
        # BOTH query attempts failed — the retry (which now carries the failing
        # table's real column list) still produced something the database
        # refused. This used to re-raise, and main.py turned it into an `error`
        # SSE frame: after answering two clarifications the user got a red pill
        # reading `Binder Error: Table "p" does not have a column named
        # "Status__c"`. A query WE wrote wrong is our bug — say what happened
        # in words, keep the conversation, and keep the raw detail in meta for
        # the proof drawer rather than the transcript.
        logging.getLogger(__name__).warning(
            "both SQL attempts failed for %r: %s", message[:120], str(exc)[:300]
        )
        text = (
            "I could not write a valid query for that — I tried twice and the "
            "second attempt was still wrong, so nothing was run and there is "
            "no number to report. Naming the object usually fixes this (for "
            "example 'payments', 'invoices' or 'training enrollments')."
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "sql", "salesforce_error": str(exc)[:300]})
        return text

    # Even the summary cap can overflow. When it does, get the TRUE total with
    # a COUNT(*) wrap — cheap for DuckDB — so the model states real numbers
    # with an honest coverage note instead of presenting the cap as the total.
    summary_overflow: Optional[int] = None
    if len(rows) > settings.sql_summary_row_cap:
        rows = rows[: settings.sql_summary_row_cap]
        try:
            _c, count_rows = _execute(
                f"SELECT COUNT(*) FROM ({sql.rstrip(';')})", 2
            )
            summary_overflow = int(count_rows[0][0])
        except Exception:  # noqa: BLE001 — the wrap is best-effort
            summary_overflow = -1  # unknown, but definitely more than the cap

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
    if summary_overflow is not None:
        # The one case the figures do NOT cover everything — say so in the
        # authoritative block itself, with the true total when the COUNT(*)
        # wrap got one, so the model reports "N of M" instead of passing the
        # cap off as the population.
        true_total = summary_overflow if summary_overflow > 0 else None
        computed["true_total_rows"] = true_total or "unknown (larger than summarised)"
        computed["counts_cover"] = (
            f"the FIRST {len(rows)} rows of a larger result"
            + (f" of {true_total} total rows" if true_total else "")
            + " — state every figure as covering those rows, never as the "
            "whole population"
        )

    parts: List[str] = []
    async for token in llm.stream_chat_completion(
        _narrative_messages(
            message,
            columns,
            preview,
            history,
            total_rows=len(rows),
            computed=computed,
            # attach_chart already ran on this meta, so this is a fact, not a
            # prediction — the mechanism that stops the model drawing ASCII
            # bars over a result that has a real chart under it.
            chart_attached=bool(meta.get("chart")),
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
