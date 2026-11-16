"""SQL engine (spec §8).

Flow: schema (cached) → gpt-oss-120b writes ONE SELECT → sql_guard validates
→ DuckDB executes READ-ONLY → one retry on failure feeding the error text
back, or on a result the data itself shows to be wrong → what the question did
not name is held back → meta.data capped at 500 rows with truncated flag → optional export
(100k cap) → chart spec ONLY when the user asked for a chart/graph/plot,
validated by pydantic (invalid → table only; model output is NEVER executed)
→ streamed narrative answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from decimal import Decimal
from itertools import islice
from typing import Any, AsyncIterator, Awaitable, Callable, Collection, Dict, List, Optional, Sequence, Set, Tuple
from uuid import UUID

from . import NO_DATA_MESSAGE, recent_turns
from .. import admission, llm, resilience
from ..config import settings
from ..continuity import LeaseLost, QueuedForRecovery
from ..core import brain, chart_decision, org_brief, sf_dictionary, sql_tree, tracing
from ..core.chart_pipeline import ChartResult, build_chart
from ..core.exports import cap_rows, export_csv, export_xlsx, slugify, preview_row_limit
from ..core.schema_cache import (
    _is_business_table,
    _stem,
    _table_tokens,
    columns_on,
    format_schema,
    is_lock_error,
    own_words,
    relevant_schema,
    same_object,
    schema_cache,
)
from ..core.sql_guard import guard_sql, mask_literals, outside_literals

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
    # Asked for QMS escalation rules, the writer counted Case rows whose reason
    # holds "escalation" and the answer gave that count as QMS escalation rules. Asked
    # for a Follow Up Setting, it read Interview__c.Scheduled_Minutes__c and gave a number.
    # The check on the statement passes, because those tables exist.
    "- If no table in the schema holds the thing the question asks about, reply "
    "with exactly NO_SUCH_OBJECT and nothing else. Never substitute another "
    "object that merely sounds close: a number counted from the wrong object is "
    "worse than saying it is not here.\n"
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
    # A short alias picked from the object's initials lands on a keyword
    # surprisingly often. "Assigned_Trainer__c at" produced
    #   Parser Error: syntax error at or near "."
    # on at.First_Name__c — DuckDB reserves AT for time-travel syntax — and
    # the message never says which word is the problem, so the retry could
    # not fix it either. Both attempts failed and the user got nothing.
    "- Table aliases must not be SQL keywords. AT, TO, IN, IS, AS, ON, BY, OR, "
    "AND, ALL, ANY, DO, END, FOR, FROM, LEFT, RIGHT, FULL, ORDER, GROUP, "
    "WHEN, WITH and LIKE are all reserved. Use a distinctive alias instead "
    "(trainer, cand, iv) — never a bare two-letter abbreviation.\n"
    # A checkbox shown as text holds 'true'/'false', and `= 'True'` on it matches
    # nothing and answers "0" with total confidence. The typed views show BOOLEAN.
    "- Salesforce checkbox columns (IsWon, IsClosed, IsActive, IsDeleted, "
    "Is*__c ...): when the schema shows BOOLEAN compare with = true or = false. "
    "When it shows VARCHAR they hold the lowercase TEXT 'true' / 'false': compare "
    "them as text in lower case. Never compare those to True, 1 or 'True'.\n"
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
    # Plain counts were asked "over what period?", and the first option was Today.
    "- A question that names no period gets NO date filter: it covers every date, "
    "and the engine says so. A month named without a year means its most recent "
    "occurrence up to today.\n"
    # The raw tables are VARCHAR and the views the engine reads are typed. Without
    # the cast rule, ORDER BY on an amount held as text sorts lexicographically
    # and "top 10 invoices by value" answers 999 over 27000 — no error, just wrong.
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


@dataclass(frozen=True)
class _PlannerHandback:
    #: The planner's own words for why this may not be in the org's records.
    note: str
    #: It chose ANSWER_GENERAL: no Salesforce record is needed for this at all.
    not_a_record_question: bool


#: Per request, like context._trim_notice: sf_intel runs first and hands main.py
#: a plain text, and the engine that reads this is several layers on from it.
_planner_handback: ContextVar[Optional[_PlannerHandback]] = ContextVar(
    "sql_planner_handback", default=None
)

#: How the planner words "there is no such thing here".
_NOT_ACCESSIBLE_RE = re.compile(
    r"\bnot\b[^.]{0,80}\b(?:accessible|exposed|synced|in (?:the|this) (?:provided )?schema|"
    r"(?:a )?standard salesforce)",
    re.I,
)


def note_planner_handback(
    assumptions: Sequence[str], *, not_a_record_question: bool = False
) -> None:
    """What the planner concluded, for the SQL writer this turn may yet reach.

    "QMS escalation rules are not ... accessible via SOQL" was in the planner's
    decision, and the question still went to a writer that never saw it and
    counted Case rows. Stored runs: every question the planner handed back as
    general and the router then sent to SQL was about an object with no synced
    copy.
    """
    notes = [
        text for text in assumptions
        if text and (not_a_record_question or _NOT_ACCESSIBLE_RE.search(text))
    ]
    if not notes and not not_a_record_question:
        return
    lead = ["It judged that this needs no Salesforce records."] if not_a_record_question else []
    _planner_handback.set(_PlannerHandback(" ".join([*lead, *notes])[:800], not_a_record_question))


#: The first words of memory_recall.format_recall_block.
_RECALL_BLOCK_START = "Context from the user's earlier conversations"


def _without_recall(history: Sequence[dict]) -> List[dict]:
    """`history` without the cross-chat recall block.

    It carries earlier ANSWERS, false numbers included ("There are 19 training
    mock results"), into a prompt whose job is to count from the rows. In a repeat
    run 10 questions had their own earlier answer in the writer's prompt, and n01
    answered 19 again. The turns of this conversation stay: that is where a query
    that worked and a clarification the person made live.
    """
    return [
        message for message in history
        if not (
            message.get("role") == "system"
            and str(message.get("content") or "").startswith(_RECALL_BLOCK_START)
        )
    ]


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
    # A plan step's input is the planner's paraphrase, so on the agent route what
    # the person clicked never reached the statement that answers it.
    carried = [line for line in _READING_LINE_RE.findall(ground) if line not in question]
    if carried:
        question = "\n\n".join([question, *carried])
    hint = hint_for(ground)
    # The dictionary says what things are CALLED; the brief says what they
    # MEAN. Knowing Account exists does not stop a model counting recruiter
    # rows as candidates.
    grounding = org_brief.grounding_for(ground)
    # Learn-from-chat: a join the user already thumbs-up'd for a similar
    # question beats one re-derived from scratch (core/learned_examples.py).
    from ..core import learned_examples

    examples = learned_examples.block_for(ground)
    user = f"Database schema:\n{schema_text}\n\nQuestion: {question}"
    # Closest to the question, because it is the most specific thing we know:
    # not a rule about people in general, but who THESE people are.
    # A warehouse lookup (DuckDB, blocking): off the event loop, like every
    # other caller of resolve_people. Inline it held the loop for the whole
    # lookup — ~205 ms per SQL attempt before core/warehouse.py cached the
    # snapshot, still a scan of the name columns on the fuzzy path after.
    people = await asyncio.to_thread(who_these_people_are, ground)
    if people:
        user = f"{people}\n\n{user}"
    if hint:
        user = f"{hint}\n\n{user}"
    if examples:
        user = f"{examples}\n\n{user}"
    if grounding:
        user = f"{grounding}\n\n{user}"
    handback = _planner_handback.get()
    if handback is not None and handback.note:
        # The planner had already said so, in writing, and the writer never saw it.
        user += (
            f"\n\nThe request planner's note on this question: {handback.note}\n"
            "If that says the thing asked about is not accessible here, and no "
            "table above IS that thing, reply NO_SUCH_OBJECT."
        )
    if error is not None:
        user += (
            f"\n\nYour previous SQL failed.\nPrevious SQL:\n{previous_sql}\n"
            f"Error:\n{error}\n\nWrite a corrected single SELECT statement."
        )
    # Earlier turns already carry queries that WORKED for this user: reuse them
    # rather than re-deriving the join each turn. A click is a harder thing. Asked
    # "All interviews" the writer still kept `rt.Name = 'Interview'`, and "All
    # submitted attempts" kept IN ('Pass','Fail'), under a sentence that said only
    # "follow those". Nor is every line a click: 9 of 23 had no card behind them.
    user += (
        "\n\nThis conversation may already contain SQL that answered an "
        "earlier question: reuse the joins and filters that already worked here "
        "unless this question changes them. A line beginning '(Clarified:' is what "
        "the person chose on a card. It is a hard instruction: the statement must "
        "do exactly that, and if another rule above would narrow or widen it, the "
        "person's choice wins. A line beginning '(Assumed:' is not the person's "
        "choice: it is a reading filled in for them, to use only where the question "
        "leaves the point open. The question's own words win over it."
    )
    messages = (
        [{"role": "system", "content": _SQL_SYSTEM}]
        + recent_turns(_without_recall(history), 6)
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
    case, handled by telling the user the copy is being refreshed instead of
    showing a raw IO error.
    """


class WarehouseUnreadable(RuntimeError):
    """The warehouse file is damaged, unreadable or not a database at all.

    Not a SQL mistake and not a busy file: no retry and no wait can help, so the
    model is not asked again and the person is not told to try in a few seconds.
    The message is fixed because agent and report steps carry `str(exc)` onward,
    and the raw DuckDB text holds the server's file path.
    """


def _is_lock_error(exc: Exception) -> bool:
    return is_lock_error(exc)


def _raise_if_warehouse_fault(exc: Exception) -> None:
    """A fault of the FILE, never of the statement, stops the retry here."""
    import duckdb  # lazy, same as _execute

    if isinstance(exc, (WarehouseBusy, WarehouseUnreadable)):
        raise exc
    if not isinstance(exc, duckdb.IOException):
        return
    if _is_lock_error(exc):
        raise WarehouseBusy("the local warehouse is being refreshed by the sync worker") from exc
    logging.getLogger(__name__).warning("the warehouse file cannot be read: %s", str(exc)[:300])
    raise WarehouseUnreadable("the synced Salesforce copy cannot be read") from exc


#: How long a chat query waits for the sync-worker's per-write lock.
_LOCK_WAIT_SECONDS = 4.0
_LOCK_WAIT_STEP = 0.25


def _connect_warehouse(duckdb):
    # The published snapshot is served from one cached instance (a cursor per
    # call, core/warehouse.py): opening the 1,023-object catalog cost ~205 ms
    # per connect. The live file is never held open — see that module.
    from ..core import warehouse

    cached = warehouse.cursor(settings.duckdb_path)
    if cached is not None:
        return cached
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
    # Contact and Lead carry the SAME human under a different lifecycle stage.
    # "Everything about X" spans them, and a question that resolves only to
    # Account silently hides the rest of the record.
    (
        "Contact",
        "SELECT Name FROM Contact WHERE lower(Name) LIKE ?",
        "a person (Contact) — related records link through their ContactId",
    ),
    (
        "Lead",
        "SELECT Name FROM Lead WHERE lower(Name) LIKE ?",
        "a person (Lead) — an unconverted enquiry, before Account/Contact exist",
    ),
)

#: A question naming more people than this is a report, not a lookup; resolving
#: each one is not worth the round trips.
_MAX_RESOLVED_PEOPLE = 8


#: Ordinary words that pair up next to a name in a lowercase question and
#: would otherwise be proposed as one ("entire information", "about samyukt").
#: Only needed for the uncapitalised path — the capitalised path has the shape
#: of the words doing that job. Kept next to org_brief._NOT_A_NAME rather than
#: merged into it: that set is about ORG vocabulary, this is about English.
_FILLER_WORDS = frozenset(
    """about above after also always any are ask asked before below best both
    but complete completed data date detail details different does done each
    entire every everything find first from full get give given has have here
    his her info information into its just know last like list look made make
    many more most much need needs new now number only other our out over
    person please pull rest same see share should show single some status
    still such summary tell than that their them then there these they thing
    things this those total under upto using very want was were what when
    where which while will with within would year years your
    """.split()
) | frozenset(
    # Analytics vocabulary. Without it "total revenue by quarter" proposed
    # "revenue quarter" as a person — harmless to the ANSWER (the warehouse
    # recognises nobody, so it is dropped) but it puts a name lookup, and
    # possibly a full fuzzy scan, on the metric path where there is never a
    # person to find.
    """above below top bottom highest lowest internal external overall recent
    latest current previous next past upcoming active inactive open closed
    amount average budget category city complete completed cost count
    country created department goal group invoice invoices kpi manager median
    metric modified month open owner payment payments pending price quarter
    rate region revenue role score stage state sum target team title type
    updated value week""".split()
) | frozenset(
    # The wording of the lines PendingIntent.resolved_text folds into a question:
    # slot labels and the sentence around them. No person is called "chose metric".
    """clarified assumed chose exactly said object record identity metric range
    owner scope region comparison baseline grouping result format filter
    """.split()
)


def people_in_question(question: str) -> List[str]:
    """Capitalised multi-word names the question appears to be about.

    PRECISE by contract: two adjacent capitalised words. A single one is far
    more often a product, an object or the first word of a sentence. Callers
    treat what comes back as a name, so this must not guess.

    `name_candidates` below is the generous counterpart — it exists because
    users do not capitalise, and its guesses are checked against the warehouse
    before anything believes them.
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


def _is_vocabulary(word: str) -> bool:
    """Org or English vocabulary, singular OR plural.

    _NOT_A_NAME holds singulars ("account", "interview"), so a bare membership
    test let "top accounts" and "internal interviews" through as people.
    Stripping one trailing 's' covers every plural in both lists at once
    rather than maintaining a parallel set. Safe for real surnames: "Jones"
    singularises to "jone", which is in neither list.
    """
    from ..core import org_brief

    low = word.lower()
    singular = low[:-1] if len(low) > 3 and low.endswith("s") else low
    return (
        low in org_brief._NOT_A_NAME
        or singular in org_brief._NOT_A_NAME
        or low in _FILLER_WORDS
        or singular in _FILLER_WORDS
    )


def name_candidates(question: str) -> List[str]:
    """Names to LOOK UP — capitalised ones plus lowercase guesses.

    Separate from `people_in_question` on purpose. That function's answer is
    treated as fact by its callers, so it stays conservative; this one feeds
    `resolve_people`, which checks every candidate against the warehouse and
    keeps only what the data recognises. A wrong guess costs one indexed query
    and disappears — so here it is worth guessing.

    This is what fixes the reported bug: "give me entire information about
    samyukt challa candidate" resolved nobody, because the capitalised pass
    is blind to lowercase and it was the only pass there was. The SQL model
    was left to invent an object and failed twice.
    """
    out = list(people_in_question(question))
    if len(out) >= _MAX_RESOLVED_PEOPLE:
        return out
    seen = {n.lower() for n in out}
    tokens = re.findall(r"\b[A-Za-z][A-Za-z'-]{2,}\b", question or "")
    for first, last in zip(tokens, tokens[1:]):
        if _is_vocabulary(first) or _is_vocabulary(last):
            continue
        pair = f"{first} {last}"
        if pair.lower() in seen:
            continue
        seen.add(pair.lower())
        out.append(pair)
        if len(out) >= _MAX_RESOLVED_PEOPLE:
            break
    return out


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
    names = name_candidates(question)
    if not names:
        return []
    import duckdb  # lazy, same as _execute

    try:
        con = _connect_warehouse(duckdb)
    except Exception:  # noqa: BLE001
        return []
    # One slot per name, so the answer keeps the question's order whether a
    # name resolved exactly or through the fuzzy pass below.
    slots: List[Optional[dict]] = [None] * len(names)
    unmatched: List[int] = []
    try:
        for index, name in enumerate(names):
            pattern = "%" + "%".join(p.lower() for p in name.split()) + "%"
            for table, sql, meaning in _PERSON_SOURCES:
                try:
                    rows = con.execute(sql, [pattern]).fetchall()
                except Exception:  # noqa: BLE001 — a missing table is not fatal
                    continue
                if rows:
                    slots[index] = {
                        "asked": name,
                        "object": table,
                        "meaning": meaning,
                        "matches": sorted({str(r[0]) for r in rows})[:4],
                    }
                    break
            else:
                unmatched.append(index)
        if unmatched:
            # SUBSTRING MATCHING IS EXACT ABOUT SPELLING, and people are
            # not. "samyukth chala" (one l) finds nothing at all, while
            # the org stores "Samyukth - challa"; so does any transposed
            # or dropped letter. Fall back to edit distance, which costs a
            # scan of the name columns and only runs when the cheap path
            # already failed — ONE statement for every unmatched name
            # (2026-09-14: it was one per name, ~6 ms each over 63k rows, and
            # a lowercase question can propose up to eight names).
            fuzzy = _fuzzy_people(con, [names[i] for i in unmatched])
            for index, found in zip(unmatched, fuzzy):
                slots[index] = found
    finally:
        con.close()
    return [entry for entry in slots if entry is not None]


#: Jaro-Winkler above this is the same human spelled differently; below it is
#: a different person who happens to share a few letters. Measured on this org:
#: "samyukt challa" scores 0.97 against "Samyukth challa" and 0.83 against the
#: nearest unrelated name, so the gap is wide and the threshold is not delicate.
_FUZZY_NAME_MIN = 0.90


def _fuzzy_person(con, name: str) -> Optional[dict]:
    """Closest stored name across the person objects, or None."""
    return _fuzzy_people(con, [name])[0]


def _fuzzy_people(con, names: Sequence[str]) -> List[Optional[dict]]:
    """Closest stored names across the person objects, one entry per name
    (None where nothing clears the threshold).

    One UNION ALL scored against every name in a single statement, so the
    whole thing is one scan however many names are asked. Ordered by
    similarity, and anything under the threshold is discarded rather than
    guessed at — a confidently wrong name is worse than no grounding at all.

    DETERMINISTIC (2026-09-14). `ORDER BY s DESC` alone left equal scores in
    whatever order the scan produced them: the same human stored on
    Recruiter__c and on Account scores identically, and repeated calls in one
    process answered "staff" once and "candidate" twice (dbperf2 revalidation).
    Ties now break on `_PERSON_SOURCES` order — the precedence the exact path
    already applies — and then on the stored name.
    """
    if not names:
        return []
    parts = []
    for rank, (table, _sql, _meaning) in enumerate(_PERSON_SOURCES):
        parts.append(
            f"SELECT '{table}' AS obj, {rank} AS src_rank, CAST(Name AS VARCHAR) AS nm "
            f"FROM \"{table}\" WHERE Name IS NOT NULL"
        )
    union = " UNION ALL ".join(parts)
    asked = ", ".join(f"({i}, ?)" for i in range(len(names)))
    try:
        rows = con.execute(
            f"""SELECT i, obj, nm, s
                  FROM (SELECT q.i, p.obj, p.src_rank, p.nm,
                               jaro_winkler_similarity(p.lnm, q.name) AS s
                          FROM (SELECT obj, src_rank, nm, lower(nm) AS lnm
                                  FROM ({union})) p
                         CROSS JOIN (VALUES {asked}) q(i, name))
                 WHERE s >= ?
               QUALIFY row_number() OVER (
                           PARTITION BY i ORDER BY s DESC, src_rank, nm) <= 4
                 ORDER BY i, s DESC, src_rank, nm""",
            [n.lower() for n in names] + [_FUZZY_NAME_MIN],
        ).fetchall()
    except Exception:  # noqa: BLE001 — older DuckDB without the function
        return [None] * len(names)
    by_name: Dict[int, List[tuple]] = {}
    for row in rows:
        by_name.setdefault(int(row[0]), []).append(row)
    out: List[Optional[dict]] = []
    for index, name in enumerate(names):
        hits = by_name.get(index)
        if not hits:
            out.append(None)
            continue
        table = hits[0][1]
        meaning = next((m for t, _s, m in _PERSON_SOURCES if t == table), table)
        out.append(
            {
                "asked": name,
                "object": table,
                "meaning": meaning,
                "matches": sorted({str(r[2]) for r in hits})[:4],
                "fuzzy": True,
            }
        )
    return out


def who_these_people_are(
    question: str, dialect: str = "sql", resolved: Optional[List[dict]] = None
) -> str:
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
    # `resolved` lets a caller that already ran the lookup render it without
    # paying for a second warehouse round trip. The live path needs the
    # structured result too, to ground the SOQL on the right object's fields.
    found = resolve_people(question) if resolved is None else resolved
    if not found:
        return ""
    lines = []
    for person in found:
        stored = person["matches"][:3]
        shown = ", ".join(repr(name) for name in stored)
        lines.append(f"- {person['asked']} is {person['meaning']}.")
        lines.append(f"    STORED NAME: {shown}")
        # A ready-made predicate, because telling the model the spelling was
        # not enough. Asked about "samyukt challa" — stored "Samyukth - challa"
        # — it acknowledged the lookup and then filtered on the USER's wording:
        #   WHERE a.Name ILIKE '%samyukt challa%'
        # which matches nothing, since the stored form has "h - " in the middle.
        # A query that runs and returns zero rows is worse than one that fails
        # loudly: it reads as "this person has no records".
        if len(stored) == 1:
            lines.append(f"    FILTER WITH: Name = {stored[0]!r}")
        else:
            joined = ", ".join(repr(name) for name in stored)
            lines.append(f"    FILTER WITH: Name IN ({joined})")
    # The live path resolves names against the warehouse mirror too, but it
    # cannot JOIN -- SOQL traverses relationships instead -- so the closing
    # advice differs. The lookup and the FILTER WITH predicate are identical:
    # `Name = 'Samyukth - challa'` is valid in both dialects.
    if dialect == "soql":
        shape = (
            "\n\nIf the question is open-ended (\"everything\", \"entire "
            "information\", \"full details\"), answer with ONE "
            "`SELECT FIELDS(ALL) FROM <object> WHERE <filter> LIMIT 200` — "
            "that returns every field of the record without naming any of "
            "them. Do NOT enumerate fields, do not issue a query per object, "
            "and do not invent generic Detail1/Detail2 fields."
        )
    else:
        shape = (
            "\n\nIf the question is open-ended (\"everything\", \"entire "
            "information\", \"full details\"), answer with ONE focused SELECT "
            "from the object above joined to its two or three most informative "
            "related tables. Do NOT build a UNION across many dissimilar "
            "objects, and do not invent generic Detail1/Detail2 columns to "
            "make them line up — a wide, readable row from the main record is "
            "the answer."
        )
    return (
        "Who the people named in this question are, looked up in the warehouse "
        "just now — treat this as fact:\n"
        + "\n".join(lines)
        + "\n\nUSE THE STORED NAME VERBATIM in every filter. The user's "
        "spelling is NOT in the data — filtering on what they typed returns "
        "zero rows and reads as 'this person has no records'. Copy the "
        "FILTER WITH clause above exactly; do not rewrite it into a LIKE "
        "pattern built from the question."
        # "Give me everything about X" invites a UNION of a dozen unrelated
        # objects flattened into Detail1..Detail5. Asked exactly that, the
        # model produced 180 lines and a Binder Error on its own CTE alias —
        # nothing ran. A person profile is a handful of joins from the record
        # that identifies them; breadth is what makes it fail.
        + shape
    )


#: Non-finite values can only sit in these. Json has no word for one: a frame holding
#: a bare Infinity is dropped by the browser and refused by jsonb, so the table goes
#: missing and the answer is not stored.
_MAY_NOT_BE_FINITE_RE = re.compile(r"FLOAT|DOUBLE|\[|STRUCT|MAP|UNION", re.I)
#: How long a figure about a result may take. It decorates an answer that already ran.
_COUNT_SECONDS = 5.0
#: The connections running a statement for this request, so a cancelled one can stop it.
_running: ContextVar[Optional[List[Any]]] = ContextVar("sql_running", default=None)


def _finite(value: Any) -> Any:
    """`n / 0` and `0 / 0`: no figure, however deep in a cell."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [_finite(item) for item in value]
    if isinstance(value, dict):
        # Any float key, not only a non-finite one: DuckDB's histogram merges a 0/0 row
        # into a finite bucket and leaves no trace, so the map's own counts are wrong.
        if any(isinstance(key, float) for key in value):
            raise sql_tree.StatementRefused(
                "histogram() and map() miscount a computed figure when a denominator is 0. "
                "Write SELECT figure, COUNT(*) ... GROUP BY 1 with NULLIF on the denominator."
            )
        return {key: _finite(item) for key, item in value.items()}
    return value


def _execute(
    sql: str, fetch_cap: int, time_limit: Optional[float] = None, *, masked: bool = False
) -> Tuple[List[str], List[list]]:
    import duckdb  # lazy

    # §8/§12: read-only, and NO external access — DuckDB's read_csv/read_blob/
    # glob/httpfs table functions would otherwise let a guard-approved SELECT
    # read arbitrary host files or reach the network. With this config DuckDB
    # raises PermissionException for every such function.
    con = _connect_warehouse(duckdb)
    running = _running.get()
    if running is not None:
        running.append(con)
    timer = threading.Timer(time_limit or settings.sql_statement_seconds, _interrupt, [con])
    timer.daemon = True
    timer.start()
    try:
        if masked:
            # On the cursor that runs it, so the list and the rows come from one file. A
            # cached list missed a column the last publish added, and it read in the clear.
            sql = _behind_the_mask(sql, columns_on(con))
        cur = con.execute(sql)
        described = cur.description or []
        columns = [d[0] for d in described]
        rows = [list(r) for r in cur.fetchmany(fetch_cap)]
        suspects = [index for index, d in enumerate(described) if _MAY_NOT_BE_FINITE_RE.search(str(d[1]))]
        if suspects:
            for row in rows:
                for index in suspects:
                    row[index] = _finite(row[index])
    except duckdb.InterruptException as exc:
        raise QueryTooSlow(QUERY_TOO_SLOW) from exc
    finally:
        timer.cancel()
        con.close()
    return columns, rows


def _interrupt(con: Any) -> None:
    try:
        con.interrupt()
    except Exception:  # noqa: BLE001 — it finished as the timer fired, and closed
        pass


async def _execute_off_the_loop(run: Callable[[], Tuple[List[str], List[list]]]) -> Tuple[List[str], List[list]]:
    """One heavy statement froze every member on the process, for a minute or more."""
    connections: List[Any] = []
    token = _running.set(connections)
    try:
        return await asyncio.to_thread(run)
    except asyncio.CancelledError:
        for con in connections:
            _interrupt(con)
        raise
    finally:
        _running.reset(token)


class EmptySql(RuntimeError):
    """The model returned no statement at all, twice.

    Distinct from NoSuchTable on purpose. "The warehouse does not hold this"
    and "the model failed to answer" are different facts, and conflating them
    sent the second case to live Salesforce — which cannot know the model
    failed, and answered from the wrong object with full confidence.
    """


class NoSuchTable(RuntimeError):
    """The question is about an object the warehouse does not carry.

    Raised so callers say so instead of presenting a made-up number. This exists
    because of a real answer the model gave: asked about Course__c — a real
    object that was never synced — it wrote `SELECT 0 AS record_count` and
    reported "0 records" as fact. A query with no FROM cannot have counted
    anything, and answering 0 is worse than admitting the table is missing.

    `statement` is what the writer replied, when there was a reply: a rejected
    DESCRIBE or information_schema read is a question about the catalogue, which
    the catalogue can answer, whatever words the person used.
    """

    def __init__(self, message: str = "", statement: Optional[str] = None) -> None:
        super().__init__(message)
        self.statement = statement


class CredentialRead(RuntimeError):
    """A statement compared, grouped or told apart a credential field.

    Nobody reads those values, so such a statement can only be answered with a
    wrong number: `LIKE` on a withheld column finds nothing, and `COUNT(DISTINCT)`
    finds one. Fixed text, so a caller passing `str(exc)` on says the right thing.
    """


#: What leaves this module about a statement the database would not run.
QUERY_REFUSED = "the database refused the query"
QUERY_TOO_SLOW = "that query took too long"


class QueryTooSlow(RuntimeError):
    """The statement ran past its time limit and was stopped.

    Not a SQL mistake, so the writer is not asked again and "the database refused"
    would be false. Fixed text, because agent steps and report sections show `str(exc)`.
    """


def _is_model_failure(exc: BaseException) -> bool:
    """The model was not there, or did not answer. The database was never asked."""
    return (
        isinstance(exc, (resilience.ModelUnavailable, admission.AdmissionRejected, asyncio.TimeoutError, TimeoutError))
        or resilience.is_read_timeout(exc)
        or resilience.is_recoverable(exc)
    )


def failure_reason(exc: BaseException) -> str:
    """What a step or a report section may say of a failure: never a server's address."""
    return "the model could not be reached" if _is_model_failure(exc) else (str(exc) or type(exc).__name__)
#: DuckDB quotes the row's value in these.
_VALUE_ERROR_RE = re.compile(r"\s*(?:Conversion|Invalid Input|Out of Range) Error:", re.I)
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


class QueryRefused(RuntimeError):
    """The database refused both attempts.

    DuckDB's own words quote row values: "Could not convert string
    '<a passport number>' to INT32". Agent steps and report sections carry
    `str(exc)` to the screen, so that is fixed text. `category` is the first word of
    the error, for analytics. `detail` is for the server log, with the value masked.
    The writer still gets the raw error on its retry: it is the local model.
    """

    def __init__(self, cause: Exception) -> None:
        super().__init__(QUERY_REFUSED)
        text = str(cause)
        words = re.match(r"\s*(\w+)(?: \w+)? Error:", text)
        self.category = words.group(1) if words else type(cause).__name__
        self.detail = _QUOTED_RE.sub("'***'", text) if _VALUE_ERROR_RE.match(text) else text


_NOT_IN_WAREHOUSE = "the question refers to data that is not in the local warehouse"

#: FROM/JOIN <table>, the table being the LAST part of a qualified name:
#: `FROM main.Account` and `FROM "main"."Account"` read Account, and reading
#: "main" as the table refused both as unsynced. A name followed by "(" is a
#: table function, which is not a table anyone synced.
_FROM_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+(?:"?[A-Za-z_]\w*"?\s*\.\s*){0,2}"?([A-Za-z_]\w*)"?\b(?!\s*\()', re.I
)
#: `WITH x AS (` and `, y AS (`: names the statement defines for itself.
_CTE_RE = re.compile(
    r'(?:\bWITH\b(?:\s+RECURSIVE)?|,)\s*"?([A-Za-z_]\w*)"?\s*(?:\([^()]*\)\s*)?AS\s*'
    r"(?:(?:NOT\s+)?MATERIALIZED\s*)?\(",
    re.I,
)
#: FROM that is no table: `a IS DISTINCT FROM b`, `EXTRACT(YEAR FROM x)`.
_DISTINCT_FROM_RE = re.compile(r"\bIS\s+(?:NOT\s+)?DISTINCT\s+FROM\b", re.I)
_EXTRACT_FROM_RE = re.compile(r"\b(EXTRACT\s*\(\s*\w+)\s+FROM\b", re.I)
#: The statements that read the catalogue rather than a synced object. The
#: duckdb_*() forms are table functions, so `_FROM_RE` does not see them at all.
_CATALOGUE_STATEMENT_RE = re.compile(
    r'^\s*(?:DESCRIBE|DESC|SHOW|PRAGMA)\b'
    r'|\b(?:FROM|JOIN)\s+(?:"?[A-Za-z_]\w*"?\s*\.\s*){0,2}"?(?:information_schema|pg_catalog|duckdb_\w+)\b',
    re.I,
)
#: The writer's "no table holds this". Any mention counts, fenced or followed by
#: an apology: a statement never needs the word.
_NO_SUCH_OBJECT_RE = re.compile(r"\bNO_SUCH_OBJECT\b")
#: DuckDB's own words for a table it does not hold. A missing FUNCTION is a
#: Catalog Error too (TO_CHAR), and that one is a query mistake.
_MISSING_TABLE_RE = re.compile(r'Table with name "?([A-Za-z_]\w*)"? does not exist')
#: What PendingIntent.resolved_text folds in: a click, or a reading filled in.
_READING_LINE_RE = re.compile(r"\((?:Clarified|Assumed):[^\n]*")


def _tables_named(sql: str) -> List[str]:
    """Names the statement reads from, in order, without its own CTE names.

    A name is the statement's own only where its CTE can be seen: after the CTE, or
    in its own body under RECURSIVE. `main.Account` is always the table, and so is
    Account inside `WITH account AS (SELECT Id FROM Account)`.
    """
    text = _masked_strings(sql)
    text = _EXTRACT_FROM_RE.sub(r"\1 ,", _DISTINCT_FROM_RE.sub(" ", text))
    recursive = bool(re.search(r"\bWITH\s+RECURSIVE\b", text, re.I))
    own: Dict[str, int] = {}
    for cte in _CTE_RE.finditer(text):
        seen_from = cte.end() - 1 if recursive else _closing_paren(text, cte.end() - 1)
        own[cte.group(1).lower()] = min(own.get(cte.group(1).lower(), seen_from), seen_from)
    named: List[str] = []
    for found in _FROM_RE.finditer(text):
        name = found.group(1)
        is_own = "." not in found.group(0) and found.start() >= own.get(name.lower(), len(text) + 1)
        if not is_own and name not in named:
            named.append(name)
    return named


def references_a_known_table(sql: str, schema: dict) -> bool:
    """True when the query actually reads a table the warehouse has."""
    known = {t.lower() for t in schema}
    return any(name.lower() in known for name in _tables_named(sql))


def _tables_read(sql: str, schema: dict) -> List[str]:
    """The warehouse tables the statement reads, spelled as the warehouse does."""
    by_lower = {t.lower(): t for t in schema}
    return [by_lower[n.lower()] for n in _tables_named(sql) if n.lower() in by_lower]


def _tables_in_clarified_line(text: str, schema: dict) -> List[str]:
    line = " ".join(_READING_LINE_RE.findall(text or ""))
    return [word for word in dict.fromkeys(re.findall(r"[A-Za-z_]\w*", line)) if word in schema]


def _reads_the_catalogue(statement: Optional[str]) -> bool:
    return bool(_CATALOGUE_STATEMENT_RE.search(_masked_strings(statement)))


def _near_tables(names: Sequence[str], schema: dict) -> Optional[Dict[str, str]]:
    """{unknown name: the table it misspells}, or None when any of them misspells nothing.

    This is the guard on the retry. A plain "that table is not in the schema"
    invites the model to take the nearest table for an object that is really
    unsynced, Course__c becoming Program__c, which is a confident wrong number
    where an honest "could not find" stood. So the retry only happens at all when
    the name is a spelling of a real table, and it must then read that table.
    It also costs a truly unsynced object no model call.
    """
    known = {t.lower() for t in schema}
    unknown = [name for name in names if name.lower() not in known]
    if not unknown:
        return None
    tables = [t for t in schema if not t.startswith("_")]
    # A word some other table is named after is that table's word, not a typo.
    words = {word for t in tables for word in _table_tokens(t)}
    near: Dict[str, str] = {}
    for name in unknown:
        matches = [t for t in tables if same_object(name, t, words)]
        # "Invoices__c" means Invoice__c and not the empty standard Invoice. With no
        # suffix to go on, either could be meant, and must_read would refuse the other.
        if name.lower().endswith("__c"):
            matches = [t for t in matches if t.endswith("__c")] or matches
        if len(matches) != 1:
            return None
        near[name] = matches[0]
    return near


def _near_table_hint(near: Dict[str, str], schema: dict) -> str:
    lines = []
    for name, table in near.items():
        columns = ", ".join(column for column, _t in schema[table])
        lines.append(
            f"There is no table {name}. The table of that name is spelled {table}, "
            f"and its COMPLETE column list is: {columns}"
        )
    return "\n".join(lines) + (
        "\nUse that spelling if that table IS the thing the question asks about. "
        "If it is not, reply NO_SUCH_OBJECT. Do not read any other table in its place."
    )


def _reads_what_was_asked(statement: str, question: str, schema: dict) -> bool:
    """Does the statement read a table the person's own words point at?

    Only asked when the planner already said this is no question about records.
    The words are what decide: a table whose name shares one, a table the org's
    spoken vocabulary maps them to, or a table a "(Clarified:" line names exactly.
    """
    own = own_words(question)
    words = {_stem(w) for w in re.findall(r"[a-z0-9]+", own.lower())}
    spoken = {t.lower() for t in org_brief.tables_for(own, packs=False)}
    for table in _tables_read(statement, schema):
        if table.lower() in spoken or (_table_tokens(table) - {"c"}) & words:
            return True
        if re.search(rf"\b{re.escape(table)}\b", question):
            return True
    return False


def _refuse_a_stand_in(raw: str, question: str, schema: dict) -> None:
    """The two ways a reply says the thing asked about is not here."""
    if _NO_SUCH_OBJECT_RE.search(raw):
        raise NoSuchTable("the writer found no table holding this", statement=raw)
    handback = _planner_handback.get()
    if (
        handback is not None
        and handback.not_a_record_question
        and references_a_known_table(raw, schema)
        and not _reads_what_was_asked(raw, question, schema)
    ):
        raise NoSuchTable("the planner said this is not in the org's records", statement=raw)


#: Said to the writer when its retry repeated the failed statement.
_REPEATED_RETRY = (
    "\n\nYour last reply repeated the failed statement exactly, and it will fail the "
    "same way. Write a DIFFERENT statement, or reply NO_SUCH_OBJECT if no table "
    "holds this."
)


def _same_statement(left: str, right: str) -> bool:
    def bare(sql: str) -> str:
        return " ".join((sql or "").split()).rstrip("; ")

    return bare(left) == bare(right)


class SqlResult(tuple):
    """(sql, columns, rows), unpacked as ever, plus what was held back from them."""

    left_out: Dict[str, List[str]]
    #: When the copy these rows came from was published, read before they were.
    snapshot_time: Optional[str]

    def __new__(
        cls,
        sql: str,
        columns: List[str],
        rows: List[list],
        left_out: Optional[dict] = None,
        snapshot_time: Optional[str] = None,
    ):
        result = super().__new__(cls, (sql, columns, rows))
        result.left_out = left_out or {}
        result.snapshot_time = snapshot_time
        return result


async def _run_statement(
    raw: str,
    attempt: int,
    cap: int,
    schema: dict,
    must_read: Sequence[str] = (),
) -> Tuple[str, List[str], List[list]]:
    validation_started = time.perf_counter()
    sql = _rename_reserved_aliases(guard_sql(raw))
    await tracing.event(
        "QUERY_PLAN_VALIDATED",
        component="orchestrator.app.core.sql_guard.guard_sql",
        details={"attempt": attempt, "dialect": "duckdb", "sql": sql},
        duration_ms=round((time.perf_counter() - validation_started) * 1000),
    )
    # After the guard, so an empty or prose retry stays "could not write" rather
    # than turning into "could not find", and only on the retry: attempt 1 already
    # passed this before it got here.
    if attempt > 1:
        read = _tables_read(sql, schema)
        if not read or (must_read and not set(must_read) & set(read)):
            raise NoSuchTable("the corrected query reads nothing the local warehouse holds", statement=raw)
    execution_started = time.perf_counter()
    snapshot_time = _snapshot_time()
    sql_tree.refuse_too_deep(sql_tree.read(sql))
    columns, rows = await _execute_off_the_loop(lambda: _execute(sql, cap, masked=True))
    await tracing.event(
        "QUERY_EXECUTED",
        component="orchestrator.app.engines.sql._execute",
        details={
            "source": "local_salesforce_warehouse",
            "environment": settings.sf_environment,
            "freshness": "synced_snapshot",
            "attempt": attempt,
            "sql": sql,
            "returned_rows": len(rows),
            "columns": columns,
            "fetch_cap": cap,
        },
        duration_ms=round((time.perf_counter() - execution_started) * 1000),
    )
    return SqlResult(sql, columns, rows, snapshot_time=snapshot_time)


async def generate_and_run_sql(
    question: str,
    *,
    history: Sequence[dict] = (),
    fetch_cap: Optional[int] = None,
    grounding_question: Optional[str] = None,
) -> SqlResult:
    """Generate, guard, and execute SQL with ONE retry feeding the error back.

    Returns (sql, columns, rows). Also reused by the report and agent engines,
    which is why what a person may see is decided here: `_hold_back` runs on the
    way out of this function, so no caller holds a contact field or a raw record
    id the question did not name.

    `grounding_question` is the USER's wording when the caller is asking in
    someone else's — the agent's plan steps, a report section instruction. The
    schema slice, the dictionary hint, the brain packs and the person lookup
    all key on it, so an agent-routed question is grounded exactly as well as
    the same question asked directly.
    """
    try:
        schema = await asyncio.to_thread(schema_cache.get, settings.duckdb_path)
    except Exception as exc:
        # No schema AND no stale copy — with the file locked there is nothing
        # to ground a SQL prompt on; the caller says the copy is busy.
        _raise_if_warehouse_fault(exc)
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
    # The prompt tells the writer who the person is ("is staff (Recruiter__c)"),
    # so that object must be in the slice. Filled up with DuplicateRecordItem and
    # SharingRecordCollection, it was not, and the writer guessed `r.Title__c`.
    # A "(Clarified:" line naming a real table pins it the same way: its label
    # words no longer rank anything (relevant_schema), but its value is a fact.
    people = await asyncio.to_thread(resolve_people, ground)
    sliced = relevant_schema(
        schema,
        ground,
        must_include=[
            *org_brief.tables_for(ground),
            *(person["object"] for person in people),
            *_tables_in_clarified_line(ground, schema),
        ],
    )
    schema_text = format_schema(sliced)
    # Column types alone cannot say which lookup points where, so the model
    # guessed join paths that match zero rows. Spell the edges out.
    joins = sf_dictionary.join_map(list(sliced))
    if joins:
        schema_text = f"{schema_text}\n\n{joins}"
    # Filters on a picklist are guesses when the prompt does not hold its values:
    # "first-round" became `Round__c = '1'` where the rows say 'First', and the
    # answer was 0 where rows match. `_SQL_SYSTEM` tells it to use these verbatim.
    stored = await asyncio.to_thread(_stored_values_block, sliced, ground)
    if stored:
        schema_text = f"{schema_text}\n\n{stored}"
    await tracing.event(
        "METADATA_RETRIEVED",
        component="orchestrator.app.core.schema_cache.relevant_schema",
        details={
            "source": "local_salesforce_warehouse",
            "selected_tables": list(sliced),
            "required_metric_tables": org_brief.tables_for(ground),
            "matched_brain_packs": [
                pack.get("name", "") for pack in brain.matched_packs(ground)
            ],
            "schema_evidence": tracing.text_fingerprint(schema_text),
            "join_map_included": bool(joins),
        },
    )
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
            grounding_question=ground,
        )
    if not raw.strip():
        raise EmptySql("the model did not produce a SQL statement")
    await tracing.event(
        "QUERY_PLAN_CREATED",
        component="orchestrator.app.engines.sql._ask_sql",
        details={"attempt": 1, "dialect": "duckdb", "sql": raw},
    )

    async def finish(result: Tuple[str, List[str], List[list]]) -> SqlResult:
        # Off the loop: it reads every cell, and a result can be 100,000 rows long.
        held = await asyncio.to_thread(_hold_back, *result, own_words(grounding_question or question), schema)
        held.snapshot_time = getattr(result, "snapshot_time", None)
        return held

    #: What the retry is told, what it must read, and what stands if it fails:
    #: a statement that failed raises again, a statement that ran is still good.
    must_read: Sequence[str] = ()
    failure: Exception = NoSuchTable(_NOT_IN_WAREHOUSE, statement=raw)
    fallback: Optional[Tuple[str, List[str], List[list]]] = None
    _refuse_a_stand_in(raw, ground, schema)
    if not references_a_known_table(raw, schema):
        # No FROM against anything we hold: the model is inventing a result
        # rather than reading data. Refuse instead of answering, unless the name
        # it wrote is only a misspelling of a table we do hold.
        near = None if _reads_the_catalogue(raw) else _near_tables(_tables_named(raw), schema)
        if not near:
            raise failure
        must_read = tuple(near.values())
        retry_error = _near_table_hint(near, schema)
    else:
        try:
            result = await _run_statement(raw, 1, cap, schema)
            advice = await asyncio.to_thread(_result_advice, *result, ground, schema)
            if not advice:
                return await finish(result)
            fallback = result
            retry_error = (
                "The statement ran, but its result cannot be used as it stands:\n"
                + "\n".join(f"- {line}" for line in advice)
            )
        except Exception as exc:  # one retry on guard/execution error (§8)
            # A locked or damaged file is not a SQL mistake — retrying the model
            # cannot help.
            _raise_if_warehouse_fault(exc)
            if isinstance(exc, (CredentialRead, QueryTooSlow)):
                raise
            await tracing.event(
                "QUERY_ATTEMPT_FAILED",
                status="failed",
                component="orchestrator.app.engines.sql.generate_and_run_sql",
                details={"attempt": 1, "sql": raw},
                error=exc,
            )
            failure = exc
            missing = _MISSING_TABLE_RE.search(str(exc))
            if missing:
                # DuckDB's own "Did you mean" names the nearest table, which is
                # exactly what must not reach the retry for an unsynced object.
                near = _near_tables([missing.group(1)], schema)
                if not near:
                    raise NoSuchTable(_NOT_IN_WAREHOUSE, statement=raw) from exc
                must_read = tuple(near.values())
                retry_error = _near_table_hint(near, schema)
            else:
                retry_error = _enriched_error(str(exc), raw, schema)

    try:
        raw2 = await _ask_sql(
            question, schema_text, history, previous_sql=raw,
            error=retry_error,
            grounding_question=ground,
        )
    except (QueuedForRecovery, LeaseLost):
        raise
    except Exception as exc:
        # The model could not be asked again. Raw Ids can stand, because code names them.
        # A figure the retry might have disowned cannot, so that one fails with the model.
        if fallback is None or not await asyncio.to_thread(_figures_stand, fallback, ground, schema):
            raise
        logging.getLogger(__name__).warning("the advisory retry could not be asked: %s", type(exc).__name__)
        return await finish(fallback)
    if _same_statement(raw2, raw):
        # Three real questions spent the retry on the same SQL, whitespace aside.
        # Running it again can only fail again, so the retry is not spent on it.
        if fallback is not None:
            return await finish(fallback)
        raw2 = await _ask_sql(
            question, schema_text, history, previous_sql=raw,
            error=retry_error + _REPEATED_RETRY,
            grounding_question=ground,
        )
        if _same_statement(raw2, raw):
            raise failure if isinstance(failure, NoSuchTable) else QueryRefused(failure)
    await tracing.event(
        "QUERY_PLAN_CREATED",
        component="orchestrator.app.engines.sql._ask_sql",
        details={"attempt": 2, "dialect": "duckdb", "sql": raw2},
    )
    try:
        _refuse_a_stand_in(raw2, ground, schema)
        result = await _run_statement(raw2, 2, cap, schema, must_read)
    except Exception as exc:
        _raise_if_warehouse_fault(exc)
        # "No table holds this" stands, whatever the first statement ran on: that
        # first number is the stand-in the writer has just disowned.
        if fallback is not None and not _NO_SUCH_OBJECT_RE.search(raw2):
            return await finish(fallback)
        if isinstance(exc, (NoSuchTable, CredentialRead, QueryTooSlow)):
            raise
        if _MISSING_TABLE_RE.search(str(exc)):
            raise NoSuchTable(_NOT_IN_WAREHOUSE, statement=raw2) from exc
        raise QueryRefused(exc) from exc
    # Told to improve on a result that ran, the retry lost the rows: keep them.
    if fallback is not None and not result[2]:
        return await finish(fallback)
    return await finish(result)


#: DuckDB's binder error for a missing column. On a plain table it reads
#:   Binder Error: Table "p" does not have a column named "Status__c"
#: and on a view, which is every table the engine reads:
#:   Binder Error: Values list "r" does not have a column named "Title__c"
_BINDER_COLUMN_RE = re.compile(
    r'(?:Table|Values list) "(?P<alias>[^"]+)" does not have a column named "(?P<column>[^"]+)"'
)
#: FROM/JOIN <table> [AS] <alias> — enough to resolve which real table the
#: failing alias referred to in the SQL we just ran.
_ALIAS_RE = re.compile(
    r'\b(?:FROM|JOIN)\s+(?:"?[A-Za-z_]\w*"?\s*\.\s*){0,2}"?(?P<table>[A-Za-z_][A-Za-z0-9_]*)"?\s+(?:AS\s+)?'
    r'(?P<alias>[A-Za-z_][A-Za-z0-9_]*)\b',
    re.I,
)
#: Written from other SQL dialects. DuckDB's own suggestion is `to_hours`.
_NO_DATE_FORMAT_RE = re.compile(r"Scalar Function with name (?:to_char|to_varchar) does not exist", re.I)
_DID_YOU_MEAN_RE = re.compile(r'\n?Did you mean "[^"]*"\?')
_SYNTAX_ERROR_RE = re.compile(r'syntax error at or near "')
_BAD_CONVERSION_RE = re.compile(r"""Could not convert string ['"].*['"] to """)
#: Every shape DuckDB words it in, on a number, a decimal, a date, a time or a timestamp.
_BLANK_LABEL_REFUSED_RE = re.compile(r"Conversion Error:.*\(blank\)", re.I | re.S)
#: Words `_SQL_SYSTEM` already forbids as aliases, because a short alias picked
#: from an object's initials lands on one surprisingly often.
_RESERVED_ALIASES = (
    "at", "to", "in", "is", "as", "on", "by", "or", "and", "all", "any", "do", "end", "for",
    "from", "left", "right", "full", "order", "group", "when", "with", "like",
)


def _rename_reserved_aliases(sql: str) -> str:
    """`Assigned_Trainer__c at` ... `at.Id` fails with `syntax error at or near "."`.

    The message never says which word is the problem, so the retry could not fix
    it either: one count was rescued once and lost twice. Code can read this one.
    An alias is only renamed when it is both declared and used with a dot, so
    `now() AT TIME ZONE` and `FROM t LEFT JOIN u` are never touched, and text
    inside a string literal is never touched at all.
    """
    masked = _masked_strings(sql)
    for word in _RESERVED_ALIASES:
        if not re.search(rf'(?<![\w."]){word}\s*\.\s*["A-Za-z_*]', masked, re.I):
            continue
        declared = re.compile(
            rf'(\b(?:FROM|JOIN)\s+(?:"?\w+"?\s*\.\s*){{0,2}}"?\w+"?\s+(?:AS\s+)?){word}\b', re.I
        )
        if not declared.search(masked):
            continue
        used = re.compile(rf'(?<![\w."]){word}(?=\s*\.)', re.I)
        sql = outside_literals(
            sql, lambda part: used.sub(f"{word}_t", declared.sub(lambda m: f"{m.group(1)}{word}_t", part))
        )
        masked = _masked_strings(sql)
    return sql


def _enriched_error(error: str, failed_sql: str, schema: dict) -> str:
    """The execution error, plus what the writer needs to fix it.

    The retry used to get only the error string. For a hallucinated column
    ("p.Status__c = 'Paid'" — the real column is Payment_Status__c) that tells
    the model WHAT failed but not what to use instead, so it guessed again,
    failed again, and the raw binder error reached the user as a red pill.
    The alias is resolved from the SQL we just ran, and the retry gets the
    whole truth about that one table.
    """
    if _NO_DATE_FORMAT_RE.search(error):
        return (
            f"{_DID_YOU_MEAN_RE.sub('', error)}\n\nDuckDB has no TO_CHAR and no TO_VARCHAR. "
            "To format a date write strftime(<date col>, '%Y-%m')."
        )
    if _BLANK_LABEL_REFUSED_RE.search(error):
        return (
            f"{error}\n\nThat column is typed, so the text '(blank)' cannot stand in it. Keep the "
            "blank bucket under a new alias: COALESCE(CAST(<col> AS VARCHAR), '(blank)') AS <label> "
            "... GROUP BY <col> ORDER BY <col>."
        )
    if _SYNTAX_ERROR_RE.search(error):
        return (
            f"{error}\n\nCheck two things. A table alias must not be a SQL keyword: "
            f"{', '.join(word.upper() for word in _RESERVED_ALIASES)} are all reserved. "
            'A table named after a keyword takes double quotes, like "Case".'
        )
    if _BAD_CONVERSION_RE.search(error):
        return (
            f"{error}\n\nThat column is typed, as the schema shows, so it never holds "
            "text. Do not compare it with a string or with ''; test for empty with "
            "IS NULL or IS NOT NULL."
        )
    match = _BINDER_COLUMN_RE.search(error)
    if not match:
        return error
    aliases = _alias_tables(_rename_reserved_aliases(failed_sql or ""))
    # No alias in the SQL: DuckDB names the table itself in that case.
    table = aliases.get(match.group("alias").lower(), match.group("alias"))
    if table is None:
        return error
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


# ---------------------------------------------------------------------------
# What the data itself says about a statement that ran
# ---------------------------------------------------------------------------

#: A text column with at most this many stored values is a category worth showing
#: and checking, such as Interview__c.Round__c.
_MAX_STORED_VALUES = 25
_MAX_STORED_VALUE_CHARS = 60
#: How many columns' values go into one prompt, and how much of it they may take.
_MAX_VALUE_COLUMNS = 30
_MAX_VALUES_BLOCK_CHARS = 6000
#: Same lifetime as schema_cache: picklists change about as rarely as columns.
_STORED_VALUES_TTL = 300.0
_stored_values_cache: Dict[Tuple[Any, str, str], Tuple[float, Optional[List[str]]]] = {}
_stored_values_copy: Any = None

#: Name-word that makes a column a category, whatever the question says.
_CATEGORY_WORDS = frozenset(
    {
        "status", "stage", "type", "outcome", "round", "result", "decision", "reason",
        "category", "source", "priority", "level", "mode", "purpose", "kind", "verdict",
    }
)
#: Never offered as stored values: keys, free text and anything that names a person.
_NOT_A_CATEGORY_RE = re.compile(
    r"name|id$|id__c$|url|link|description|comment|note|body|text|subject|title|summary|json", re.I
)
#: "Salesforce Id": 15 or 18 alphanumerics with digits in them, every value of a column.
_RECORD_ID_RE = re.compile(r"^(?=(?:[A-Za-z]*\d){3})(?=.*[A-Za-z])[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
#: Bounded: this runs over every cell, and unbounded it is quadratic on a long token.
_EMAIL_VALUE_RE = re.compile(r"[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}")
_PHONE_VALUE_RE = re.compile(r"^\+?[\d\s().\-]{10,20}$")
_DATE_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
#: Shaped like a phone and no phone: 31-12-2026, a pay range 900000 - 1200000, and
#: a long decimal.
_NOT_A_PHONE_RE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}\b|^\d{6,}\s*-\s*\d{6,}$|^\d+\.\d+$")
#: A phone inside longer text, on one line. A date in it is a list of dates ("1.
#: 12-08-2019 2."), and a URL holds digit runs that are nobody's number.
_PHONE_SPAN_RE = re.compile(r"(?<![\w/.\-])\+?\(?\d[\d \t().\-]{8,18}\d(?![\w/\-])")
_DATE_INSIDE_RE = re.compile(r"\b\d{1,2}[-/.]\d{1,2}[-/.]\d{4}\b|\b\d{4}-\d{2}-\d{2}\b|\)\(")
_URL_RE = re.compile(r"https?://\S+", re.I)


def _looks_like_phone(value: str) -> bool:
    return bool(_PHONE_VALUE_RE.match(value) and not _DATE_VALUE_RE.match(value) and not _NOT_A_PHONE_RE.match(value))


def _phone_spans(text: str) -> List[Tuple[int, int]]:
    """Where a phone number sits inside a longer text, 10 to 15 digits of it."""
    if not _PHONE_SPAN_RE.search(text):
        return []
    links = [m.span() for m in _URL_RE.finditer(text)]
    spans = []
    for match in _PHONE_SPAN_RE.finditer(text):
        found = match.group(0)
        if (
            10 <= sum(char.isdigit() for char in found) <= 15
            and not _DATE_INSIDE_RE.search(found)
            and not _NOT_A_PHONE_RE.match(found)
            and not any(start <= match.start() < end for start, end in links)
        ):
            spans.append(match.span())
    return spans


def _stored_values_stamp() -> Any:
    """Which copy is being read. Read BEFORE the cursor opens: then a swap can only file
    newer values under an older stamp, which nothing reads again.

    Under the path alone, a value new to a just-published copy was told to the writer
    as not stored, for up to the whole TTL, and a writer that believed it gave another
    value's count.
    """
    from ..core import warehouse

    global _stored_values_copy
    stamp = warehouse._identity(settings.duckdb_path)
    if stamp != _stored_values_copy:
        _stored_values_cache.clear()
        _stored_values_copy = stamp
    return stamp


def _read_stored_values(con, table: str, column: str, stamp: Any = None) -> Optional[List[str]]:
    """Every value a text column holds, or None when it is no category.

    None too when any value looks like a record id or a way to reach somebody: a
    small table can make any column low-cardinality, and these values go to the
    model whether or not the question asked for them.
    """
    key = (stamp, table, column)
    hit = _stored_values_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _STORED_VALUES_TTL:
        return hit[1]
    rows = con.execute(
        f'SELECT DISTINCT "{column}" FROM "{table}" WHERE "{column}" IS NOT NULL '
        f"LIMIT {_MAX_STORED_VALUES + 1}"
    ).fetchall()
    values: Optional[List[str]] = sorted(str(row[0]) for row in rows if str(row[0]))
    if len(rows) > _MAX_STORED_VALUES or any(
        len(value) > _MAX_STORED_VALUE_CHARS
        or _RECORD_ID_RE.match(value)
        or _EMAIL_VALUE_RE.search(value)
        or _looks_like_phone(value)
        for value in values
    ):
        values = None
    _stored_values_cache[key] = (time.monotonic(), values)
    return values


def _is_value_column(column: str, dtype: str) -> bool:
    return (
        str(dtype).upper().startswith("VARCHAR")
        and not _NOT_A_CATEGORY_RE.search(column)
        and not _CREDENTIAL_COLUMN_RE.search(column)
        and not any(pattern.search(column.lower()) for pattern, _asked_by in _CONTACT_FIELDS)
    )


def _stored_values_block(sliced: dict, question: str) -> str:
    """`Interview__c.Round__c [First | Second]` for the columns this question may filter on.

    Tables the person's words point at only, the columns those words name first:
    a whole slice of value lists would be the haystack `_MAX_TABLES` exists to
    avoid. Grounding is an optimisation, never fatal.
    """
    own = own_words(question)
    words = {_stem(w) for w in re.findall(r"[a-z0-9]+", own.lower()) if len(w) >= 4}
    spoken = set(org_brief.tables_for(own))
    picks = []
    for table, columns in sliced.items():
        if table not in spoken and not (_table_tokens(table) - {"c"}) & words:
            continue
        for column, dtype in columns:
            tokens = _table_tokens(column) - {"c"}
            named = bool(tokens & words)
            if (
                _is_value_column(column, dtype)
                and not _is_secret_column(table, column, dtype)
                and (named or tokens & _CATEGORY_WORDS)
            ):
                picks.append((not named, table, column))
    if not picks:
        return ""
    import duckdb  # lazy, same as _execute

    try:
        stamp = _stored_values_stamp()
        con = _connect_warehouse(duckdb)
    except Exception:  # noqa: BLE001
        return ""
    lines: List[str] = []
    size = 0
    try:
        for _not_named, table, column in sorted(picks)[:_MAX_VALUE_COLUMNS]:
            try:
                values = _read_stored_values(con, table, column, stamp)
            except Exception:  # noqa: BLE001 — one unreadable column costs one line
                continue
            if values:
                line = f"{table}.{column} [{' | '.join(values)}]"
                size += len(line)
                if size > _MAX_VALUES_BLOCK_CHARS:
                    break
                lines.append(line)
    finally:
        con.close()
    if not lines:
        return ""
    return "Stored values of the columns this question may filter on (exact):\n" + "\n".join(lines)


_LITERAL = r"'(?:[^']|'')*'"
#: `alias.column <op> 'text'` or `alias.column IN ('a', 'b')`. A column inside a
#: function call is skipped on purpose: this only reports what it can be sure of.
_PREDICATE_RE = re.compile(
    r'(?:"?(?P<alias>[A-Za-z_]\w*)"?\s*\.\s*)?"?(?P<column>[A-Za-z_]\w*)"?\s*'
    r"(?P<op>=|!=|<>|NOT\s+IN\b|IN\b|NOT\s+I?LIKE\b|I?LIKE\b)\s*"
    rf"(?P<value>{_LITERAL}|\(\s*{_LITERAL}(?:\s*,\s*{_LITERAL})*\s*\))",
    re.I,
)
_NOT_NULL_RE = re.compile(r'(?:"?(?P<alias>[A-Za-z_]\w*)"?\s*\.\s*)?"?(?P<column>[A-Za-z_]\w*)"?\s+IS\s+NOT\s+NULL\b', re.I)
_WHERE_END_RE = re.compile(r"(?:GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|QUALIFY|UNION|INTERSECT|EXCEPT|WINDOW)\b", re.I)
_AND_RE = re.compile(r"AND\b", re.I)
_OR_RE = re.compile(r"OR\b", re.I)
_FILTER_OPEN_RE = re.compile(r"\bFILTER\s*\(\s*$", re.I)
_BETWEEN_RE = re.compile(r"BETWEEN\b", re.I)
#: Words saying the person asked for the blank rows to go.
_ASKS_TO_DROP_BLANKS_RE = re.compile(r"\b(?:blank|empty|null|unset|filled|non-?blank|excluding)\b", re.I)


def _masked_strings(sql: Optional[str]) -> str:
    """The statement with its string literals blanked, every position where it was."""
    return mask_literals(sql or "")


def _split_conditions(text: str, masked: str, start: int, ends: "re.Pattern") -> List[str]:
    """The top-level AND parts of the clause at `start`.

    AND binds tighter than OR, so C in `A OR B AND C` filters nothing by itself: a
    clause with a top-level OR is one condition. Saying "rows with no C are left
    out too" over it was false for many of the rows it matched.
    """
    depth = 0
    between = False
    has_or = False
    parts: List[str] = []
    piece = position = start
    while position < len(masked):
        char = masked[position]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and (position == 0 or not (masked[position - 1].isalnum() or masked[position - 1] == "_")):
            if ends.match(masked, position):
                break
            if _BETWEEN_RE.match(masked, position):
                between = True
            elif _OR_RE.match(masked, position):
                has_or = True
            elif _AND_RE.match(masked, position):
                if between:
                    between = False
                else:
                    parts.append(text[piece:position])
                    piece = position + 3
        position += 1
    parts.append(text[piece:position])
    return [text[start:position]] if has_or else parts


def _clause_conditions(text: str, masked: str, keyword: str, ends: "re.Pattern") -> List[str]:
    """Every condition of every clause `keyword` opens, read off `masked`."""
    found: List[str] = []
    for clause in re.finditer(keyword, masked, re.I):
        found.extend(_split_conditions(text, masked, clause.end(), ends))
    return [" ".join(condition.split()) for condition in found if condition.strip()]


def _filters(sql: str, *, aggregate_filters: bool = True) -> List[str]:
    """The top-level conditions of every WHERE in a statement, in order."""
    text = sql or ""
    masked = _masked_strings(text)
    found: List[str] = []
    for where in re.finditer(r"\bWHERE\b", masked, re.I):
        if not aggregate_filters and _FILTER_OPEN_RE.search(masked[: where.start()]):
            continue
        found.extend(_split_conditions(text, masked, where.end(), _WHERE_END_RE))
    return [" ".join(condition.split()) for condition in found if condition.strip()]


def _alias_tables(sql: str) -> Dict[str, Optional[str]]:
    """{alias: the table it reads}, None for an alias two tables share."""
    found: Dict[str, Optional[str]] = {}
    for match in _ALIAS_RE.finditer(sql):
        alias, table = match.group("alias").lower(), match.group("table")
        if alias in found and (found[alias] or "").lower() != table.lower():
            found[alias] = None
        else:
            found.setdefault(alias, table)
    return found


def _column_table(alias: Optional[str], column: str, sql: str, schema: dict) -> Optional[str]:
    """The warehouse table `alias.column` reads, or None when that is not certain."""
    if alias:
        aliases = _alias_tables(sql)
        if alias.lower() in aliases and aliases[alias.lower()] is None:
            return None
        candidates = [aliases.get(alias.lower()) or alias]
    else:
        candidates = _tables_read(sql, schema)
    by_lower = {t.lower(): t for t in schema}
    tables = [
        by_lower[name.lower()]
        for name in candidates
        if name.lower() in by_lower
        and any(c.lower() == column.lower() for c, _t in schema[by_lower[name.lower()]])
    ]
    return tables[0] if len(tables) == 1 else None


def _like_matches(pattern: str, value: str) -> bool:
    """LIKE, in one pass: a regex of `.*` runs took ten seconds on eight wildcards, and
    holds the GIL for all of it, so the whole process stopped on one crafted filter."""
    pieces = re.split(r"%+", pattern)

    def fits(piece: str, at: int) -> bool:
        return at + len(piece) <= len(value) and all(p == "_" or p == value[at + i] for i, p in enumerate(piece))

    if len(pieces) == 1:
        return len(pattern) == len(value) and fits(pattern, 0)
    first, *middle, last = pieces
    end = len(value) - len(last)
    if end < len(first) or not fits(first, 0) or not fits(last, end):
        return False
    at = len(first)
    for piece in middle:
        while at + len(piece) <= end and not fits(piece, at):
            at += 1
        if at + len(piece) > end:
            return False
        at += len(piece)
    return True


def _literal_misses(sql: str, schema: dict) -> List[str]:
    """Filter values a category column does not hold: such a filter can only match nothing."""
    misses: List[str] = []
    import duckdb  # lazy, same as _execute

    con = None
    stamp = None
    masked = _masked_strings(sql)
    try:
        for match in _PREDICATE_RE.finditer(sql or ""):
            op = match.group("op").upper()
            if op not in ("=", "IN", "LIKE", "ILIKE"):
                continue
            column = match.group("column")
            table = _column_table(match.group("alias"), column, sql, schema)
            if table is None:
                continue
            # `CASE ... END AS Status__c` is no longer the stored column.
            if re.search(rf'\bAS\s+"?{re.escape(column)}"?(?![\w"])', masked, re.I):
                continue
            real, dtype = next((c, t) for c, t in schema[table] if c.lower() == column.lower())
            if not _is_value_column(real, dtype) or _is_secret_column(table, real, dtype):
                continue
            if con is None:
                stamp = _stored_values_stamp()
                con = _connect_warehouse(duckdb)
            values = _read_stored_values(con, table, real, stamp)
            if not values:
                continue
            for literal in re.findall(_LITERAL, match.group("value")):
                text = literal[1:-1].replace("''", "'")
                if op == "LIKE":
                    hit = any(_like_matches(text, value) for value in values)
                elif op == "ILIKE":
                    hit = any(_like_matches(text.lower(), value.lower()) for value in values)
                else:
                    hit = text in values
                if not hit:
                    misses.append(
                        f"{real} has no stored value {literal}. It holds: {' | '.join(values)}."
                    )
    except Exception:  # noqa: BLE001 — a check, and only a check
        return misses
    finally:
        if con is not None:
            con.close()
    return list(dict.fromkeys(misses))


def _null_drops(sql: str, columns: Sequence[str], question: str, schema: dict) -> List[str]:
    """Rows a filter drops for being blank, when the rows exist and nobody asked for that.

    `Invoice_Status__c != 'Paid'` lost every unpaid invoice with no status, and
    `Status__c IS NOT NULL` before a GROUP BY lost every interview with none, each with
    no error. A rule in the prompt had said neither was wanted.
    """
    # Nor when a cut was asked for: "top 5 recruiters" has no use for a blank one.
    if _ASKS_TO_DROP_BLANKS_RE.search(own_words(question)) or _ASKS_FOR_A_CUT_RE.search(own_words(question)):
        return []
    canonical = " ".join(metric["sql"] for metric in org_brief.match_metrics(own_words(question)))
    found: List[Tuple[Optional[str], str, str]] = []
    grouped = re.search(r"\bGROUP\s+BY\b(?P<by>.*)", sql or "", re.I | re.S)
    for condition in _filters(sql, aggregate_filters=False):
        predicate = _PREDICATE_RE.fullmatch(condition)
        if predicate and predicate.group("op").upper().replace(" ", "") in ("!=", "<>", "NOTIN", "NOTLIKE", "NOTILIKE"):
            if predicate.group("value") != "''":
                found.append(
                    (
                        predicate.group("alias"),
                        predicate.group("column"),
                        f"{condition} also drops the <n> rows where {predicate.group('column')} "
                        "is NULL. When those rows belong in the answer, write "
                        f"{predicate.group('column')} IS DISTINCT FROM ... instead.",
                    )
                )
            continue
        not_null = _NOT_NULL_RE.fullmatch(condition)
        if (
            not_null
            and grouped
            and f"{not_null.group('column')} IS NOT NULL".lower() not in canonical.lower()
            and (
                not_null.group("column").lower() in grouped.group("by").lower()
                or not_null.group("column").lower() in {c.lower() for c in columns}
            )
        ):
            found.append(
                (
                    not_null.group("alias"),
                    not_null.group("column"),
                    f"{condition} before the GROUP BY drops the blank bucket of <n> rows. Keep "
                    "it and name it: <label>.",
                )
            )
    out: List[str] = []
    for alias, column, line in found:
        table = _column_table(alias, column, sql, schema)
        if table is None:
            continue
        try:
            _c, rows = _execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" IS NULL', 2)
            blank = int(rows[0][0])
        except Exception:  # noqa: BLE001 — a check, and only a check
            continue
        if blank:
            dtype = next((t for c, t in schema[table] if c.lower() == column.lower()), "VARCHAR")
            out.append(line.replace("<n>", f"{blank:,}").replace("<label>", _blank_label(column, dtype)))
    return out


def _blank_label(column: str, dtype: str) -> str:
    """A text label fails on any other type the moment one NULL row reaches it."""
    if str(dtype).upper().startswith("VARCHAR"):
        return f"COALESCE({column}, '(blank)')"
    return f"COALESCE(CAST({column} AS VARCHAR), '(blank)') AS <label> ... GROUP BY {column} ORDER BY {column}"


def _figures_stand(result: Sequence[Any], question: str, schema: dict) -> bool:
    """Nothing the data can prove against this statement's figures."""
    sql, columns = result[0], result[1]
    return not _literal_misses(sql, schema) and not _null_drops(sql, columns, question, schema)


def _cell_texts(value: Any) -> List[str]:
    """Every string a cell holds, through a list() or a struct_pack() of them."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _cell_texts(item)]
    return []


def _cell_keys(value: Any) -> List[str]:
    """Every string a cell holds as a key: histogram() and map() put the values there."""
    if isinstance(value, dict):
        return [
            *(key for key in value if isinstance(key, str)),
            *(key for item in value.values() for key in _cell_keys(item)),
        ]
    if isinstance(value, (list, tuple)):
        return [key for item in value for key in _cell_keys(item)]
    return []


def _id_texts(value: Any) -> List[str]:
    """A cell's texts, and those of its keys that are Ids. `struct_pack(a := Id)` has labels."""
    return [*_cell_texts(value), *(key for key in _cell_keys(value) if _RECORD_ID_RE.match(key))]


def _ids_in(values: Sequence[Any]) -> List[str]:
    return [text for value in values for text in _id_texts(value) if _RECORD_ID_RE.match(text)]


def _holds_record_ids(values: Sequence[Any]) -> bool:
    """Ids, and at most one label standing in for the rows with none.

    The writer is told to name a blank bucket, COALESCE(Department__c, '(blank)'),
    and that one label used to switch every check of the Ids around it off.
    """
    if not values or not all(_id_texts(value) for value in values):
        return False
    ids = set(_ids_in(values))
    return bool(ids) and len({text for value in values for text in _id_texts(value)} - ids) <= 1


def _sampled(rows: Sequence[Sequence], index: int) -> List[Any]:
    """A column's first filled values, however far down they start.

    Filled values within the first 200 rows made a sparse lookup look empty: the raw
    Ids sat in a long result, every one of them past row 200.
    """
    filled = (r[index] for r in rows if index < len(r) and r[index] not in (None, ""))
    return list(islice(filled, 200))


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) or type(value).__name__ == "Decimal"


def _all_numbers(values: Sequence[Any]) -> bool:
    return bool(values) and all(_is_number(value) for value in values)


def _group_key_of_ids(columns: Sequence[str], rows: Sequence[Sequence]) -> Optional[int]:
    """The column that labels each row with a raw Id, when nothing else labels it."""
    labels = []
    measured = False
    for index in range(len(columns)):
        values = _sampled(rows, index)
        if values and all(_is_number(v) for v in values):
            measured = True
        elif values:
            labels.append(index)
    if measured and len(labels) == 1 and _holds_record_ids(_sampled(rows, labels[0])):
        return labels[0]
    return None


#: How many Ids of one prefix are tried before its parent is given up on.
_MAX_PARENT_TRIES = 3


def _parent_of(record_id: str, column: str, schema: dict) -> Optional[str]:
    """The table this Id is really a row of, read from the data rather than guessed."""
    import duckdb  # lazy, same as _execute

    tokens = _table_tokens(column) - {"c"}
    ranked = sorted(
        (
            (-len(tokens & (_table_tokens(table) - {"c"})), table)
            for table, cols in schema.items()
            if not table.startswith("_")
            and any(name == "Id" for name, _t in cols)
            and (tokens & _table_tokens(table) or _is_business_table(table))
        ),
    )
    try:
        con = _connect_warehouse(duckdb)
    except Exception:  # noqa: BLE001
        return None
    try:
        for _overlap, table in ranked[:200]:
            try:
                if con.execute(f'SELECT 1 FROM "{table}" WHERE "Id" = ? LIMIT 1', [record_id]).fetchone():
                    return table
            except Exception:  # noqa: BLE001
                continue
    finally:
        con.close()
    return None


def _parent_of_any(ids: Sequence[str], column: str, schema: dict) -> Optional[str]:
    """The parent of the first of these Ids that still has one: a row can be gone."""
    for record_id in ids[:_MAX_PARENT_TRIES]:
        parent = _parent_of(record_id, column, schema)
        if parent:
            return parent
    return None


def _label_column(table: str, schema: dict) -> Optional[str]:
    """What a person calls a row of this table: its Name, or the record number it has instead."""
    names = [name for name, _t in schema.get(table, ())]
    if "Name" in names:
        return "Name"
    return next((name for name in names if name.endswith("Number") and not _CREDENTIAL_COLUMN_RE.search(name)), None)


def _raw_id_advice(columns: Sequence[str], rows: Sequence[Sequence], question: str, schema: dict) -> List[str]:
    """Lookups shown as Ids where the person asked for what they point at.

    "Department a0X...: 12" in all three runs, and a reports-to read as null. The
    rule forbidding this was in the prompt the whole time.
    """
    own = own_words(question)
    if _ASKS_FOR_RECORD_IDS_RE.search(own):
        return []
    words = {_stem(w) for w in re.findall(r"[a-z0-9]+", own.lower())}
    key = _group_key_of_ids(columns, rows)
    out: List[str] = []
    for index, name in enumerate(columns):
        values = _sampled(rows, index)
        if name.lower() == "id" or not _holds_record_ids(values):
            continue
        if index != key and not (_table_tokens(name) - {"c"}) & words:
            continue
        parent = _parent_of_any(_ids_in(values), name, schema)
        label = _label_column(parent, schema) if parent else None
        where = (
            f"LEFT JOIN {parent} ON <that lookup> = {parent}.Id and select {parent}.{label}"
            if label
            else "join the table that lookup points at and select what names its rows"
        )
        out.append(f"{name} holds raw record Ids, which a person cannot read: {where}.")
    return out


def _result_advice(
    sql: str, columns: List[str], rows: List[list], question: str, schema: dict
) -> List[str]:
    """What is wrong with a statement that ran: `generate_and_run_sql` retries once on it.

    Each is something the data can prove and the prompt could only ask for. Never
    raises: a statement that ran is an answer, whatever this check does.
    """
    try:
        return [
            *_literal_misses(sql, schema),
            *_null_drops(sql, columns, question, schema),
            *_raw_id_advice(columns, rows, question, schema),
        ]
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning("result check failed", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# What a person may see of a result
# ---------------------------------------------------------------------------
# The owner's rule of 2026-10-26: contact info only when asked. A member asking
# who someone is got meta.data with every column, phone and Slack id among them, and
# a list of background checks carried every candidate's Salesforce Id. An answer
# rule cannot reach a row payload, so this runs before any payload, narrative,
# chart or export exists. By name and by value, so a new column that looks like a
# way to reach somebody is held back without anyone adding it to a list.

#: A result wider than this is a record dump, not an answer. Same width the
#: deterministic figures profile (`_MAX_PROFILED_COLUMNS`).
_MAX_RESULT_COLUMNS = 20

#: Never shown, even when asked for by name: ANSWER_RULES says say the field exists
#: and do not read it out, and now nothing can. The owner's very-high list, the same
#: as BLOCKED_COLUMN_RE in sf_assistant's catalog. Failed_Login_Count__c and
#: Question_Bank__c only sound like one, and so does a login's date.
_CREDENTIAL_COLUMN_RE = re.compile(
    r"pass(?:w|code)|pwd|secret|credential|api_?key|access_?key|session_?key|token|username"
    r"|(?<![a-z])login(?:__c|_?(?:id|name|user|mail|pass|pwd|key|code))"
    r"|(?<![a-z])ssn(?![a-z])|social_?security|passport|(?<![a-z])(?<!question_)bank|routing|iban|swift"
    r"|aadhaar|aadhar|(?<![a-z])pan(?![a-z])|card_?(?:no|num)|cvv|(?<![a-z])otp(?![a-z])"
    r"|identification_?number|(?<![a-z0-9])session_?id(?![a-z0-9])|account_?number"
    r"|security_?(?:question|answer)|verification_?code|integrity_?hash|(?<![a-z0-9])signature__c",
    re.I,
)
#: A date, a time or a checkbox holds no secret: User.LastPasswordChangeDate is
#: metadata about one. An identity document's own dates stay with the document.
_NO_SECRET_KINDS = ("BOOL", "DATE", "TIME")
_IDENTITY_DOCUMENT_RE = re.compile(r"passport|(?<![a-z])ssn(?![a-z])|social_?security|aadha|identification_?number", re.I)
#: Objects that are credentials as a whole (sf_assistant's SENSITIVE_OBJECTS): their
#: text is withheld whatever a column is called, but for what names a row or links it.
_CREDENTIAL_OBJECTS = frozenset({"Candidate_Portal_Credential__c", "Candidate_Portal_Session__c"})
_READABLE_ON_CREDENTIAL_OBJECTS = frozenset({"name", "candidate__c", "candidate_credential__c", "role__c"})
#: What a withheld cell reads as inside the engine. Never reaches a person.
_WITHHELD = "[withheld]"
CREDENTIAL_LINE = "Credential fields are never shown."
#: (what a column's lowercased name contains, the words that ask for it).
_REACHING_WORDS = frozenset({"contact", "reach"})
_CONTACT_FIELDS = (
    (re.compile(r"mail(?!ing)"), frozenset({"email", "emails", "mail", "mails"}) | _REACHING_WORDS),
    (
        re.compile(r"phone|mobile|whatsapp|(?<![a-z])mob(?![a-z])|(?<![a-z])cell(?![a-z])|(?<![a-z])fax|(?<![a-z])sms(?![a-z])"),
        frozenset(
            {"phone", "phones", "telephone", "telephones", "landline", "landlines", "mobile", "mob", "cell", "cellphone", "whatsapp", "fax", "sms"}
        )
        | _REACHING_WORDS,
    ),
    (re.compile(r"slack"), frozenset({"slack"})),
    (
        re.compile(r"linkedin|telegram|skype|twitter|facebook|instagram|handle"),
        frozenset({"linkedin", "telegram", "skype", "twitter", "facebook", "instagram", "handle"}),
    ),
    (
        re.compile(r"address(?!__(?:city|state(?:code)?|country(?:code)?)__s)|street|postal|postcode|pincode|pin_code|(?<![a-z])zip|latitude|longitude"),
        frozenset({"address", "street", "postal", "postcode", "pincode", "zip"}),
    ),
    (re.compile(r"contact(?!_?(?:name|source))|emergency|next_of_kin|guardian"), frozenset({"emergency", "guardian"}) | _REACHING_WORDS),
    # Where someone lives, not how to reach them: asked for in place words, and the
    # label of a count per city needs no asking (`_labels_a_figure`).
    (
        re.compile(r"address__(?:city|state(?:code)?|country(?:code)?)__s"),
        frozenset({"address", "city", "cities", "town", "state", "states", "country", "countries", "location", "locations", "live", "lives"}),
    ),
)
_ADDRESS_PART_RE = re.compile(r"address__(?:city|state(?:code)?|country(?:code)?)__s", re.I)
#: Machine state, not an answer: COMPASS_State_JSON__c came back in full.
_INTERNAL_COLUMN_RE = re.compile(r"(?<![a-z])json(?![a-z])", re.I)
#: A name part that says nothing about what a person asked for.
_NOISE_WORDS = frozenset({"c", "s", "pc", "no", "num", "number", "id", "user", "work", "personal", "name", "alt"})
_EMAIL_WORDS = _CONTACT_FIELDS[0][1]
_PHONE_WORDS = _CONTACT_FIELDS[1][1]
#: "record id", "the Id", "record link". A Slack id or a user id is not one.
_ASKS_FOR_RECORD_IDS_RE = re.compile(
    r"(?<!slack )(?<!user )(?<!employee )(?<!email )(?<!mail )\bids?\b|\brecord links?\b"
    r"|\blinks? to (?:the )?records?\b",
    re.I,
)
_MEASURE_WORDS = frozenset(
    {"count", "cnt", "total", "sum", "avg", "average", "percent", "percentage", "pct", "ratio", "rate"}
)


def _is_measure_name(column: str) -> bool:
    return bool(_table_tokens(column) & _MEASURE_WORDS)


def _names_the_field(name: str, question: str, real: Collection[str] = ()) -> bool:
    """The question spells this API name out. An alias the writer chose is not one.

    "Which department is she in?" with `Department__c AS Department` kept a raw Id
    under a word the question happened to use.
    """
    if not name.endswith(("__c", "__s", "Id")) and name.lower() not in real:
        return False
    return bool(re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", question, re.I))


def _columns_read(sql: str, schema: dict) -> Set[str]:
    """The real column names of the tables a statement reads, in lower case."""
    return {column.lower() for table in _tables_read(sql, schema) for column, _t in schema[table]}


def _is_only_a_count(sql: str, name: str, values: Sequence[Any]) -> bool:
    """`COUNT(...) AS name`: how many there are, never what they hold."""
    return (
        bool(values)
        and all(_is_number(value) for value in values)
        and bool(
            re.search(
                r"\bCOUNT(?:_IF)?\s*\((?:[^()]|\([^()]*\))*\)\s*(?:FILTER\s*\((?:[^()]|\([^()]*\))*\)\s*)?(?:AS\s+)?"
                rf'"?{re.escape(name)}"?(?![A-Za-z0-9_])',
                sql or "",
                re.I,
            )
        )
    )


def _holds_no_secret(values: Sequence[Any]) -> bool:
    return bool(values) and all(
        isinstance(v, bool)
        or hasattr(v, "isoformat")
        or (isinstance(v, str) and (_DATE_VALUE_RE.match(v) or v.lower() in ("true", "false")))
        for v in values
    )


def _holds_a_credential(name: str, values: Sequence[Any]) -> bool:
    if not _CREDENTIAL_COLUMN_RE.search(name):
        return False
    return bool(_IDENTITY_DOCUMENT_RE.search(name)) or not _holds_no_secret(values)


#: What the text rules can read. `row(link, 1)` is a tuple and `encode(link)` is bytes:
#: both passed every rule whole, so a cell that is neither of these has nothing to say.
_READABLE_CELLS = (str, int, float, Decimal, date, clock_time, timedelta, UUID)


def _is_unreadable(value: Any) -> bool:
    if value is None or isinstance(value, _READABLE_CELLS):
        return False
    if isinstance(value, list):
        return any(_is_unreadable(item) for item in value)
    if isinstance(value, dict):
        # A key is not rewritten: two keys that differ by the secret would merge and lose a figure.
        return any(
            _is_unreadable(key) or _is_unreadable(item) or (isinstance(key, str) and _hide_embedded_secrets(key) != key)
            for key, item in value.items()
        )
    return True


def _holds_unreadable(rows: Sequence[Sequence], index: int) -> bool:
    return any(index < len(row) and _is_unreadable(row[index]) for row in rows)


def _holds_withheld(rows: Sequence[Sequence], index: int) -> bool:
    """A withheld value came through under this name: an alias, a concat, a whole row."""
    return any(
        "[" in text and _WITHHELD in text.lower()
        for row in rows
        if index < len(row)
        for text in (*_cell_texts(row[index]), *_cell_keys(row[index]))
    )


def _is_secret_column(table: str, column: str, dtype: str) -> bool:
    """Does nobody read this column's values, whatever a statement calls them?"""
    kind = str(dtype).upper()
    if kind.startswith(_NO_SECRET_KINDS) and not _IDENTITY_DOCUMENT_RE.search(column):
        return False
    if table in _CREDENTIAL_OBJECTS and kind.startswith("VARCHAR"):
        return not column.endswith("Id") and column.lower() not in _READABLE_ON_CREDENTIAL_OBJECTS
    return bool(_CREDENTIAL_COLUMN_RE.search(column))


def _compares_a_secret(sql: str, held: Sequence[str]) -> bool:
    """Is a withheld column compared, grouped or told apart? Is it filled in is fine."""
    masked = _masked_strings(sql)
    clauses = [
        *_filters(sql),
        *_clause_conditions(sql, masked, r"\bON\b", _ON_END_RE),
        *_clause_conditions(sql, masked, r"\bUSING\b", _ON_END_RE),
        *_clause_conditions(sql, masked, r"\bHAVING\b", _WHERE_END_RE),
        *_clause_conditions(sql, masked, r"\bGROUP\s+BY\b", _WHERE_END_RE),
    ]
    for distinct in re.finditer(r"\bDISTINCT\b", masked, re.I):
        if not re.search(r"\bIS\s+(?:NOT\s+)?$", masked[: distinct.start()], re.I):
            end = re.compile(r"\bFROM\b", re.I).search(masked, distinct.end())
            clauses.append(masked[distinct.end() : end.start() if end else None])
    #: Not `LENGTH(col) > 0 + 4` and not `COALESCE(col, '') <> '' || 'x'`.
    ends = r"(?=\s*(?:$|\)|AND\b|OR\b))"
    for column in held:
        name = rf'(?:"?\w+"?\s*\.\s*)?"?{re.escape(column)}"?(?![\w"])'
        harmless = re.compile(
            rf"(?<![\w\"]){name}\s*(?:IS\s+(?:NOT\s+)?NULL\b|(?:=|!=|<>)\s*'(?:|true|false)')"
            rf"|\bCOUNT\s*\(\s*{name}\s*\)"
            rf"|\bCOALESCE\s*\(\s*{name}\s*,\s*''\s*\)\s*(?:=|!=|<>)\s*''{ends}"
            rf"|\bNULLIF\s*\(\s*{name}\s*,\s*''\s*\)\s*IS\s+(?:NOT\s+)?NULL\b"
            rf"|\bLENGTH\s*\(\s*{name}\s*\)\s*(?:>|=)\s*0{ends}",
            re.I,
        )
        used = re.compile(rf'(?<!\w)"?{re.escape(column)}"?(?![\w"])', re.I)
        if any(used.search(harmless.sub(" ", clause)) for clause in clauses):
            return True
    return False


def _refuse_what_counts_a_marker(tree: dict, held: List[str], links: List[str], columns: Dict[str, List[str]]) -> None:
    if held and sql_tree.reads_a_held_value(tree, held, columns):
        raise CredentialRead(CREDENTIAL_LINE)
    # A hidden value makes one link of many: COUNT(DISTINCT link) said 1, and a join on
    # one blew up its rows. Nothing about a link is a credential to say so of, so it gets the retry.
    if links and sql_tree.reads_a_held_value(tree, [*held, *links], columns):
        raise sql_tree.StatementRefused(
            "A link or URL column can only be shown as it is, counted, or tested for being filled in. "
            "Show the column itself, or filter and group on another column."
        )


def _behind_the_mask(sql: str, schema: dict) -> str:
    """The statement to run, with every withheld column reading as a marker.

    A rule on result names is one alias away from a real passport number: `AS note`
    came back whole, and so do a CTE, a concat, a whole row and COLUMNS('regex'). So
    the column is gone before the statement sees it, by a CTE named as the table that
    shadows it. `COUNT(col)` and `IS NOT NULL` stay right, because only filled values
    change. The statement itself stays what the Scope line and the trace read.

    What it reads comes from DuckDB's own parser (core/sql_tree.py): a regex can be
    told `warehouse.T`, `FROM 'T'` or `main<zero-width space>.T`, and DuckDB reads
    every one of those as the table.

    Every other text column reads with the by-value rules already applied, because
    hiding the finished cell is one `split_part(link, '=', 2)` too late.
    """
    tree = sql_tree.read(sql)
    if tree is None:
        return sql  # It does not parse: DuckDB says so in its own words, for the retry.
    sql_tree.refuse_unseen_reads(tree)
    named = sql_tree.tables_read(tree)
    secrets: Dict[str, List[str]] = {}
    texts: Dict[str, List[str]] = {}
    for table, columns in schema.items():
        if table.lower() in named:
            held = [column for column, dtype in columns if _is_secret_column(table, column, dtype)]
            if held:
                secrets[table] = held
            # Exactly Id, not a list of names: a secret sat in a comment body and a work
            # history as well as in links, and any list of names is one name away from it.
            text = [
                column
                for column, dtype in columns
                if str(dtype).upper() == "VARCHAR" and column != "Id" and column not in held
            ]
            if text:
                texts[table] = text
    shadowed = sorted({*secrets, *texts})
    if not shadowed:
        return sql
    held_columns = [column for held in secrets.values() for column in held]
    links = [column for text in texts.values() for column in text if _LINK_COLUMN_RE.search(column)]
    real_columns = {table: [column for column, _t in columns] for table, columns in schema.items() if table.lower() in named}
    if held_columns and _compares_a_secret(sql, held_columns):
        raise CredentialRead(CREDENTIAL_LINE)
    _refuse_what_counts_a_marker(tree, held_columns, links, real_columns)
    # A shadow of that name would be a duplicate, and a wrapper around the statement
    # would let its own CTE stand in for `main.T`: a wrong number. So it is renamed.
    clash = sorted(table for table in shadowed if table.lower() in sql_tree.cte_names(tree))
    if clash:
        raise sql_tree.StatementRefused(
            f"A WITH name cannot be the name of a table: {clash[0]}. Rename your WITH table and write the statement again."
        )
    # main.T and warehouse.T would read past the shadow, so they read it too.
    qualified = re.compile(
        r'(?<![\w"])(?:"?\w+"?\s*\.\s*)?"?(?:main|warehouse)"?\s*\.\s*"?('
        + "|".join(re.escape(table) for table in shadowed)
        + r')"?(?![\w"])',
        re.I,
    )
    statement = outside_literals(sql, lambda part: qualified.sub(lambda m: f'"{m.group(1)}"', part))
    # Whatever the rewrite could not see is still written with its qualifier here.
    rewritten = sql_tree.read(statement)
    if sql_tree.reads_past_a_name(rewritten, {table.lower() for table in shadowed}):
        raise sql_tree.StatementRefused("Read each table by its bare name, with no catalog or schema before it.")
    # main.T in a column's place is the whole row, and the rewrite just spelled it bare.
    _refuse_what_counts_a_marker(rewritten, held_columns, links, real_columns)
    shadows = ", ".join(
        f'"{table}" AS (SELECT * REPLACE ('
        + ", ".join(
            [
                *(
                    f"""CASE WHEN "{column}" IS NULL OR CAST("{column}" AS VARCHAR) IN ('', 'true', 'false') """
                    f"""THEN CAST("{column}" AS VARCHAR) ELSE '{_WITHHELD}' END AS "{column}\""""
                    for column in secrets.get(table, ())
                ),
                *(f'{_hide_in_sql(column)} AS "{column}"' for column in texts.get(table, ())),
            ]
        )
        + f') FROM main."{table}")'
        for table in shadowed
    )
    opening = re.match(r"\s*WITH\b(?:\s+RECURSIVE\b)?", statement, re.I)
    if opening:
        return f"{statement[: opening.end()]} {shadows}, {statement[opening.end() :].lstrip()}"
    return f"WITH {shadows} {statement}"


#: Very-high values inside a stored text, the same rules as sf_assistant's catalog:
#: a Zoom join link carries the meeting passcode as ?pwd=, a form link a token. The
#: link stays readable and the value goes.
_HIDDEN = "[hidden]"
_SECRET_WORD_RE = re.compile(
    r"pass|pwd|otp|token|secret|signature|key|zak|tk=|sig=|credential|ssn|social security|aadha|pan|account|a/c|acct"
    r"|routing|ifsc",
    re.I,
)
_SECRET_PARAM_RE = re.compile(
    r"([?&;](?:[\w.-]*?(?:pwd|passw(?:or)?d|passcode|token|secret|signature|api_?key|access_?key)"
    r"|zak|tk|sig|otp|x-amz-credential)=)[^&\s\"'<>]+",
    re.I,
)
_SECRET_LABEL = (
    r"(?:[\w-]{0,128}?(?:pass ?code|passw(?:or)?d|pwd|secret|token|api[ _-]?key|access[ _-]?key)[\w-]{0,128}|otp|ssn)"
    r"[\"']?[ \t]*[:=][ \t]*[\"']?"
)
_NUMBER_LABEL = (
    r"(?:(?:passport|social security|aadhaa?r|ssn|ifsc)(?: ?(?:no\.?|number|num|#|code|card))?"
    r"|(?:pan|(?:bank )?(?:account|a/c|acct)|routing) ?(?:no\.?|number|num|#|card))[\"']?[ \t]*[:=-]?[ \t]*[\"']?"
)
_SECRET_VALUE_CHAR = r"[^\s\"'<>,;&\\]"
_LABELLED_SECRET_RE = re.compile(
    rf"((?<![\w]){_SECRET_LABEL})(?!\[hidden\])({_SECRET_VALUE_CHAR}+)"
    rf"|((?<![\w]){_NUMBER_LABEL})(?!\[hidden\])((?={_SECRET_VALUE_CHAR}{{0,128}}\d){_SECRET_VALUE_CHAR}+)",
    re.I,
)
#: The same rules for DuckDB, whose regex engine (RE2) has no lookaround: the character
#: before a label is captured, and rewriting an already hidden value is harmless.
_HIDE_IN_SQL = (
    (_SECRET_PARAM_RE.pattern, rf"\1{_HIDDEN}"),
    (rf"(^|[^\w])({_SECRET_LABEL}){_SECRET_VALUE_CHAR}+", rf"\1\2{_HIDDEN}"),
    (
        rf"(^|[^\w])({_NUMBER_LABEL}){_SECRET_VALUE_CHAR}{{0,128}}\d{_SECRET_VALUE_CHAR}*",
        rf"\1\2{_HIDDEN}",
    ),
)
#: A link holds its secret in place: hidden, one link looks like another, so anything
#: but showing, counting or testing it for being filled answers wrong.
_LINK_COLUMN_RE = re.compile(r"(?<![a-zA-Z])(?:link|url)(?![a-zA-Z])", re.I)


def _hide_in_sql(column: str) -> str:
    expression = f'"{column}"'
    for pattern, replacement in _HIDE_IN_SQL:
        quoted = pattern.replace("'", "''")
        expression = f"regexp_replace({expression}, '{quoted}', '{replacement}', 'gi')"
    return expression


def _hide_embedded_secrets(text: str) -> str:
    if not _SECRET_WORD_RE.search(text):
        return text
    if "=" in text:
        text = _SECRET_PARAM_RE.sub(rf"\1{_HIDDEN}", text)
    return _LABELLED_SECRET_RE.sub(lambda m: (m.group(1) or m.group(3)) + _HIDDEN, text)


def _hide_contact(text: str, words: Set[str]) -> str:
    """The rest of the text stays: one phone in a task's subject is no reason to lose the subject."""
    if "@" in text and not words & _EMAIL_WORDS:
        text = _EMAIL_VALUE_RE.sub(_HIDDEN, text)
    if not words & _PHONE_WORDS:
        for start, end in reversed(_phone_spans(text)):
            text = text[:start] + _HIDDEN + text[end:]
    return text


def _cleaned(value: Any, hide_contact: bool, words: Set[str]) -> Any:
    if isinstance(value, str):
        text = _hide_embedded_secrets(value)
        return _hide_contact(text, words) if hide_contact else text
    if isinstance(value, list):
        return [_cleaned(item, hide_contact, words) for item in value]
    if isinstance(value, dict):
        return {key: _cleaned(item, hide_contact, words) for key, item in value.items()}
    return value


def _cleaned_rows(rows: List[list], kept: Sequence[int], masked: Sequence[int], words: Set[str]) -> List[list]:
    """Every row of what will be sent, the same list when nothing in it changed."""
    out = rows
    for number, row in enumerate(rows):
        new_row = row
        for index in kept:
            if index < len(row) and isinstance(row[index], (str, list, dict)):
                cell = _cleaned(row[index], index in masked, words)
                if cell != row[index]:
                    if new_row is row:
                        new_row = list(row)
                    new_row[index] = cell
        if new_row is not row:
            if out is rows:
                out = list(rows)
            out[number] = new_row
    return out


def _contact_not_asked_for(name: str, values: Sequence[Any], words: set) -> bool:
    """Does this column's name say it reaches somebody, and did the question NOT ask?"""
    # A checkbox or a date cannot reach anybody: Email_Sent__c, Last_Contact_Date__c.
    # Neither can a count of them. A phone held as a number stays a phone.
    if _holds_no_secret(values):
        return False
    if _is_measure_name(name) and values and all(_is_number(v) for v in values):
        return False
    lowered = name.lower()
    asked_by = [asked for pattern, asked in _CONTACT_FIELDS if pattern.search(lowered)]
    # Only a contact word unlocks its own kind. "Which candidate was on it?" is no
    # request for Candidate_Phone_Number__c, nor "What's" for Interviewer_s_Email__c.
    return bool(asked_by) and not words & set().union(*asked_by)


def _asks_for_the_column(name: str, words: Set[str]) -> bool:
    """Its own name's words ask for it, plural or not. Only ever said of a real column."""
    own = {_stem(part) for part in re.findall(r"[a-z]+", name.lower())} - _NOISE_WORDS
    if name.lower() == "name":
        own = {"name"}
    return bool(own & {_stem(word) for word in words})


def _contact_by_value(name: str, rows: Sequence[Sequence], index: int, words: Set[str], real: Collection[str]) -> str:
    """"hold" or "mask" for a column whose name says nothing of contact, "" when it holds none.

    Most of it is contact details: the column goes. Only some is: those values go, and
    the column stays, because one e-mail typed into a long list of company names should not
    empty the list. A computed column is judged on what it has inside: `Phone || ' ext'`
    is a phone, a real free-text subject with a number in it is still a subject.
    """
    cells = [r[index] for r in rows if index < len(r) and r[index] not in (None, "")]
    wants_email, wants_phone = bool(words & _EMAIL_WORDS), bool(words & _PHONE_WORDS)
    is_real = name.lower() in real
    whole = inside = 0
    for cell in cells:
        for text in (*_cell_texts(cell), *_cell_keys(cell)):
            has_email = not wants_email and "@" in text and bool(_EMAIL_VALUE_RE.search(text))
            is_phone = not wants_phone and _looks_like_phone(text.strip())
            if has_email or is_phone or (not wants_phone and _phone_spans(text)):
                if not isinstance(cell, str):
                    return "hold"
                inside += 1
                whole += bool(is_phone or (has_email and _EMAIL_VALUE_RE.fullmatch(text.strip())))
    if not inside:
        return ""
    if (whole if is_real else inside) * 2 > len(cells):
        return "" if is_real and _asks_for_the_column(name, words) else "hold"
    return "mask"


def _labels_a_figure(name: str, index: int, columns: Sequence[str], rows: Sequence[Sequence], sql: str) -> bool:
    """A city or a state as the label of a count: "how many per city?" asks for just that.

    Beside a person's name it is that person's address, so only other address parts
    and figures may stand next to it.
    """
    return bool(
        _ADDRESS_PART_RE.search(name)
        and _AGGREGATE_RE.search(sql or "")
        and all(
            _ADDRESS_PART_RE.search(other) or _all_numbers(_sampled(rows, position))
            for position, other in enumerate(columns)
            if position != index
        )
    )


def _hold_back(
    sql: str, columns: List[str], rows: List[list], question: str, schema: dict
) -> SqlResult:
    """Drop what the question did not name, before anything downstream can see it.

    Keeps what a question names explicitly: a work e-mail or phone asked for by
    name is an answer. Fails closed everywhere else.
    """
    if not rows:
        # Nothing matched, so nothing is held back. "Contact details were left out"
        # over an empty result says the record exists, and hides that none was found.
        return SqlResult(sql, columns, rows)
    words = set(re.findall(r"[a-z]+", question.lower()))
    # "The number for Asha" is a phone, "the number of interviews for Asha" is not.
    says_number = "number" in words and not re.search(r"\bnumbers?\s+of\b", question, re.I)
    wants_ids = bool(_ASKS_FOR_RECORD_IDS_RE.search(question))
    real = _columns_read(sql, schema)
    key = _group_key_of_ids(columns, rows)
    left_out: Dict[str, List[str]] = {}
    kept: List[int] = []
    masked: List[int] = []
    for index, name in enumerate(columns):
        values = _sampled(rows, index)
        counted = _is_only_a_count(sql, name, values)
        asking = words
        if says_number and not any(
            _is_measure_name(other) or _all_numbers(_sampled(rows, position))
            for position, other in enumerate(columns)
            if position != index
        ):
            asking = words | {"phone"}
        if (
            (not counted and _holds_a_credential(name, values))
            or _holds_withheld(rows, index)
            or _holds_unreadable(rows, index)
        ):
            left_out.setdefault("credentials", []).append(name)
        elif name.lower() != "id" and _names_the_field(name, question, real):
            kept.append(index)
        elif (
            not counted
            and index != key
            and not _labels_a_figure(name, index, columns, rows, sql)
            and _contact_not_asked_for(name, values, asking)
        ):
            left_out.setdefault("contact", []).append(name)
        elif _INTERNAL_COLUMN_RE.search(name):
            left_out.setdefault("internal", []).append(name)
        elif not wants_ids and index != key and (
            _holds_record_ids(values) or (name.lower() == "id" and values)
        ):
            left_out.setdefault("record_ids", []).append(name)
        else:
            by_value = "" if counted or index == key else _contact_by_value(name, rows, index, asking, real)
            if by_value == "hold":
                left_out.setdefault("contact", []).append(name)
            else:
                kept.append(index)
                if by_value == "mask":
                    masked.append(index)
    if (
        (left_out.get("contact") or left_out.get("credentials") or (left_out.get("record_ids") and _AGGREGATE_RE.search(sql or "")))
        and len(rows) > 1
        and all(_all_numbers(_sampled(rows, index)) for index in kept)
    ):
        # What told the rows apart is gone. Numbers nobody can read are no answer.
        kept = []
    if len(kept) > _MAX_RESULT_COLUMNS and not EXPORT_RE.search(question):
        # The columns the question points at first, then the order they came in.
        ranked = sorted(
            kept,
            key=lambda i: (not (_table_tokens(columns[i]) - {"c"}) & {_stem(w) for w in words | {"name"}}, i),
        )
        chosen = set(ranked[: _MAX_RESULT_COLUMNS])
        left_out["wide"] = [columns[i] for i in kept if i not in chosen]
        kept = [i for i in kept if i in chosen]
    if key is not None and not wants_ids and key in kept:
        # The retry was told to join and did not. A count with no label is no
        # answer, and an Id is no label: read the parent's name here instead.
        rows = _named_group_key(rows, key, columns[key], schema)
    cleaned = _cleaned_rows(rows, kept, masked, words)
    hidden = [
        columns[index]
        for index in masked
        if index in kept and any(a[index] != b[index] for a, b in zip(rows, cleaned))
    ]
    if hidden:
        left_out["masked"] = hidden
    if len(kept) == len(columns):
        return SqlResult(sql, columns, cleaned, left_out)
    return SqlResult(
        sql, [columns[i] for i in kept], [[r[i] for i in kept] for r in cleaned], left_out
    )


def _named_group_key(rows: List[list], key: int, column: str, schema: dict) -> List[list]:
    """Replace the Ids in `key` with what each one's own parent calls it.

    One lookup can point at several objects and the top Id at a row that is gone, so
    every Id prefix finds its own parent. A label such as '(blank)' stays. An Id with
    no name gets a number, because two groups must stay two groups.
    """
    import duckdb  # lazy, same as _execute

    values = list(dict.fromkeys(r[key] for r in rows if isinstance(r[key], str) and _RECORD_ID_RE.match(r[key])))
    by_prefix: Dict[str, List[str]] = {}
    for value in values:
        by_prefix.setdefault(value[:3], []).append(value)
    names: Dict[str, str] = {}
    for ids in by_prefix.values():
        parent = _parent_of_any(ids, column, schema)
        label = _label_column(parent, schema) if parent else None
        if not label:
            continue
        try:
            con = _connect_warehouse(duckdb)
            try:
                marks = ", ".join("?" for _ in ids)
                names.update(
                    (str(row[0]), str(row[1]))
                    for row in con.execute(
                        f'SELECT "Id", "{label}" FROM "{parent}" WHERE "Id" IN ({marks})', ids
                    ).fetchall()
                    if row[1] not in (None, "")
                )
            finally:
                con.close()
        except Exception:  # noqa: BLE001 — the fallback text below is the answer
            continue
    unnamed: Dict[str, str] = {}

    def said(cell: Any) -> Any:
        if not (isinstance(cell, str) and _RECORD_ID_RE.match(cell)):
            return cell
        return names.get(cell) or unnamed.setdefault(cell, f"(unnamed record {len(unnamed) + 1})")

    return [[said(cell) if index == key else cell for index, cell in enumerate(row)] for row in rows]


def _left_out_line(left_out: Dict[str, List[str]]) -> str:
    """What the reader should know was held back, in the engine's own words."""
    parts: List[str] = []
    named = [
        label
        for key, label in (("contact", "contact details"), ("record_ids", "record ids"))
        if left_out.get(key)
    ]
    if named:
        # Not "the question did not ask": no word list knows every way of asking.
        parts.append(
            f"{' and '.join(named).capitalize()} were left out: "
            + ("they are shown only when asked for by name, such as phone or e-mail." if left_out.get("contact") else "they are shown only when asked for by name.")
        )
    if left_out.get("credentials"):
        parts.append(CREDENTIAL_LINE)
    if left_out.get("wide"):
        parts.append(f"{len(left_out['wide'])} more fields were left out to keep this readable.")
    if left_out.get("masked"):
        parts.append("Contact details found inside other fields were hidden.")
    return " ".join(parts)


def _nothing_shown_line(
    sql: str, rows: Sequence[Sequence], left_out: Dict[str, List[str]], total: Optional[int] = None
) -> str:
    """What is said when every column was held back: the records were still found."""
    count = records_matched(sql, rows, total)
    matched = f"{count:,} record{'' if count == 1 else 's'} matched. " if count else ""
    if not count and rows and _collapses_rows(sql):
        # Distinct values are no records, but a figure is still owed.
        came_back = total if total not in (None, -1) else len(rows)
        matched = f"{came_back:,} row{'' if came_back == 1 else 's'} came back. "
    return matched + (
        _left_out_line(left_out) or "The query read only fields that are not shown unless asked for by name."
    )


# ---------------------------------------------------------------------------
# What was counted, said by the engine
# ---------------------------------------------------------------------------

#: Grouped results label their rows with a blank label, so say what that is.
_BLANK_LABEL = "(blank)"
_MAX_SCOPE_FILTERS = 6
_MAX_SCOPE_FILTER_CHARS = 120
_TRY_CAST_RE = re.compile(r"TRY_CAST\(\s*([^()]+?)\s+AS\s+\w+(?:\([^()]*\))?\s*\)", re.I)
_TODAY_RE = re.compile(r"\(\s*now\(\)\s+AT\s+TIME\s+ZONE\s+'[^']*'\s*\)\s*::\s*date", re.I)
#: Interview__c is the one object whose record types the owner settled: see
#: org_brief.INTERVIEW_COUNT_RECORD_TYPE. A filter naming the record type shows
#: up as a filter; its absence is what needs saying.
_RECORD_TYPE_SCOPED = {"Interview__c": "every Interview__c record type, initial calls included"}
#: The cut at the very end of the outer statement, in any order and either spelling.
_TRAILING_CUT_RE = re.compile(
    r"(?:\s+(?:LIMIT\s+(?P<limit>\d+)|OFFSET\s+(?P<offset>\d+)(?:\s+ROWS?)?"
    r"|FETCH\s+(?:FIRST|NEXT)\s+(?P<fetch>\d+)\s+ROWS?\s+ONLY))+\s*;?\s*$",
    re.I,
)
_ORDER_BY_RE = re.compile(r"\bORDER\s+BY\b", re.I)
#: A number in the person's words that counts time is no size of a result.
_TIME_UNIT = r"(?!\s*(?:days?|weeks?|months?|years?|hours?|hrs?|minutes?|mins?|quarters?)\b)"
_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    12: "twelve", 15: "fifteen", 20: "twenty", 25: "twenty five", 30: "thirty", 50: "fifty", 100: "hundred",
}
#: The words that ask for one row by an order: "the latest", "who has the most".
_ASKS_FOR_ONE_PICK_RE = re.compile(
    r"\b(?:most|least|highest|lowest|largest|smallest|biggest|best|worst|maximum|minimum|max|min"
    r"|latest|newest|oldest|earliest|longest|shortest|busiest|top|bottom|first|last)\b",
    re.I,
)
#: The person asked for a cut: a top N, a latest one, a fixed number.
_ASKS_FOR_A_CUT_RE = re.compile(
    r"\b(?:top|bottom|first|last|latest|newest|oldest|earliest|recent)\s+(?:\d+|one|two|three|four|five|ten|few)\b"
    r"|\b(?:most|least|highest|lowest|largest|smallest|biggest|best|worst|maximum|minimum|max|min"
    r"|latest|newest|oldest|earliest|longest|shortest|busiest|sample|some|example|examples|few)\b",
    re.I,
)


#: What ends an ON clause.
_ON_END_RE = re.compile(
    r"(?:LEFT|RIGHT|FULL|CROSS|INNER|SEMI|ANTI|NATURAL|ASOF|POSITIONAL|JOIN|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT"
    r"|QUALIFY|UNION|INTERSECT|EXCEPT|WINDOW)\b",
    re.I,
)
_JOIN_WORD_RE = re.compile(r"\b((?:(?:LEFT|RIGHT|FULL|CROSS|INNER|OUTER|SEMI|ANTI|NATURAL|ASOF|POSITIONAL)\s+)*)JOIN\b", re.I)
_NOT_A_COLUMN = r"(?!(?i:true|false|null|current_date|current_time|current_timestamp|localtime|localtimestamp)\b)"
#: `a.x = b.y` and nothing more: it matches rows up and filters nothing by itself.
#: `= true` and `= current_date` are filters in that shape.
_KEY_MATCH_RE = re.compile(
    rf'{_NOT_A_COLUMN}(?:"?\w+"?\s*\.\s*)?"?[A-Za-z_]\w*"?\s*=\s*{_NOT_A_COLUMN}(?:"?\w+"?\s*\.\s*)?"?[A-Za-z_]\w*"?'
)
#: The joins that keep rows with no match on some side, or only those.
_KEEPS_UNMATCHED = frozenset({"LEFT", "RIGHT", "FULL", "ANTI"})
#: The joins after which the rows counted need not be rows of the first table at all.
_NOT_ITS_ROWS = frozenset({"RIGHT", "FULL", "ANTI"})
_SET_OPERATION_RE = re.compile(r"\b(?:UNION|INTERSECT|EXCEPT)\b", re.I)
_STARTS_WITH_CTE_RE = re.compile(r"\s*WITH\b", re.I)
_SUBQUERY_OPEN_RE = re.compile(r"\(\s*(?:SELECT|WITH)\b", re.I)
#: A subquery after one of these is a part of the statement that the rest reads.
_READ_BY_THE_REST_RE = re.compile(r"\b(?:FROM|JOIN|AS|MATERIALIZED)\s*$", re.I)
_FILTER_CLAUSE_RE = re.compile(r"\bFILTER\s*\(", re.I)
_RECORD_TYPE_WORD_RE = re.compile(r"record_?type", re.I)
_OR_WORD_RE = re.compile(r"\bOR\b", re.I)
_ANY_PAREN_RE = re.compile(r"\(")


def _closing_paren(masked: str, opening: int) -> int:
    depth = 0
    for position in range(opening, len(masked)):
        if masked[position] == "(":
            depth += 1
        elif masked[position] == ")":
            depth -= 1
            if not depth:
                return position
    return len(masked) - 1


def _blanked(masked: str, opener: "re.Pattern", keeps: Optional["re.Pattern"] = None) -> str:
    """`masked` with each parenthesis `opener` finds blanked out, every position where it was."""
    out = masked
    position = 0
    while True:
        found = opener.search(masked, position)
        if not found:
            return out
        if keeps is not None and keeps.search(masked[: found.start()]):
            position = found.end()
            continue
        opening = masked.index("(", found.start())
        closing = _closing_paren(masked, opening)
        out = out[: found.start()] + " " * (closing + 1 - found.start()) + out[closing + 1 :]
        position = closing + 1


def _where_count(masked: str) -> int:
    return sum(
        1 for where in re.finditer(r"\bWHERE\b", masked, re.I) if not _FILTER_OPEN_RE.search(masked[: where.start()])
    )


def _is_a_chain(masked: str) -> bool:
    """Each CTE reads the one before it and the statement reads the last: filters stack."""
    names: List[str] = []
    position = 0
    while True:
        position = len(masked) - len(masked[position:].lstrip())
        cte = _CTE_RE.match(masked, position)
        if not cte:
            break
        closing = _closing_paren(masked, cte.end() - 1)
        read = {name.lower() for name in _tables_named(masked[cte.end() : closing])}
        names.append(cte.group(1).lower())
        if read & set(names[:-1]) != set(names[-2:-1]):
            return False
        position = closing + 1
    return not names or {name.lower() for name in _tables_named(masked[position:])} & set(names) == {names[-1]}


def _one_scope(masked: str, outer: str) -> bool:
    """Do all the statement's filters narrow the same rows?

    Over two sibling CTEs, 'Status__c = x and Status__c IN (y, z)' can match nothing
    while the answer lists every status. Filters in a NOT IN name what was left out.
    """
    if _SET_OPERATION_RE.search(masked):
        return False
    wheres = _where_count(masked)
    if wheres != _where_count(outer):
        return False
    if _STARTS_WITH_CTE_RE.match(masked):
        return not wheres or _is_a_chain(masked)
    return wheres <= 1


def _on_conditions(text: str, outer: str) -> Tuple[List[str], List[str]]:
    """(ON conditions that dropped rows, ON conditions that only limit what was joined).

    An outer join keeps its rows whatever its ON says, so `LEFT JOIN RecordType rt ON ...
    AND rt.Name = 'Interview'` counted every row under a line that said "where". The first
    key match of a clause is the key. A second one is a filter: it can drop most rows.
    """
    joins = list(_JOIN_WORD_RE.finditer(outer))
    dropped: List[str] = []
    limited: List[str] = []
    for clause in re.finditer(r"\bON\b", outer, re.I):
        before = [join for join in joins if join.start() < clause.start()]
        words = set(before[-1].group(1).upper().split()) if before else set()
        keyed = False
        for found in _split_conditions(text, outer, clause.end(), _ON_END_RE):
            condition = " ".join(found.split())
            if not condition:
                continue
            if not keyed and _KEY_MATCH_RE.fullmatch(condition):
                keyed = True
            else:
                (limited if words & _KEEPS_UNMATCHED else dropped).append(condition)
    return dropped, limited


def _counted_line(sql: str, schema: dict) -> str:
    """One line of what the statement read, from the statement.

    The narrative only ever saw the rows, so it could describe filters that did
    not run: "N first-round client interviews" with no round filter in the
    SQL. This is written by the engine and it reads the SQL that ran, so it says
    "every row" only of a statement that cannot have dropped one.
    """
    tables = objects_read(sql, schema)
    if not tables:
        return ""
    text = sql or ""
    masked = _masked_strings(text)
    outer = _blanked(masked, _SUBQUERY_OPEN_RE, _READ_BY_THE_REST_RE)
    # What the rest reads is joined to it. What only a subquery reads is not.
    main = [t for t in objects_read(outer, schema) if t != "RecordType"] or [t for t in tables if t != "RecordType"] or tables
    first = main[0]
    aliases = _alias_tables(text)

    def spelled_out(match) -> str:
        table = aliases.get(match.group(1).lower())
        if table == first:
            return ""
        return f"{table}." if table else match.group(0)

    def spoken(condition: str) -> str:
        said = _TODAY_RE.sub("today", _TRY_CAST_RE.sub(r"\1", condition))
        # Never inside a literal: a value 'r.varnok@example.test' is not an alias.
        said = outside_literals(said, lambda piece: re.sub(r'\b([A-Za-z_]\w*)\.(?=["A-Za-z_])', spelled_out, piece))
        not_null = _NOT_NULL_RE.fullmatch(condition)
        if not_null:
            said = f"{not_null.group('column')} is filled in"
        return said if len(said) <= _MAX_SCOPE_FILTER_CHARS else said[: _MAX_SCOPE_FILTER_CHARS - 1] + "…"

    def noted(condition: str) -> str:
        said = spoken(condition)
        predicate = _PREDICATE_RE.fullmatch(condition)
        if predicate and predicate.group("op").upper().replace(" ", "") in ("!=", "<>", "NOTIN", "NOTLIKE", "NOTILIKE"):
            said += f" (rows with no {predicate.group('column')} are left out too)"
        return said

    real = _filters(text, aggregate_filters=False)
    per_figure = [condition for condition in _filters(text) if condition not in real]
    # A join's own condition and a HAVING drop rows as a WHERE does, on a join that drops any.
    on_dropped, on_limits = _on_conditions(text, outer)
    narrowing = [
        *on_dropped,
        *_clause_conditions(text, outer, r"\bHAVING\b", _WHERE_END_RE),
        *_clause_conditions(text, outer, r"\bQUALIFY\b", _WHERE_END_RE),
    ]
    conditions = [*real, *narrowing]
    joined = main[1:]
    joins = [set(m.group(1).upper().split()) for m in _JOIN_WORD_RE.finditer(outer)]
    # No JOIN word over two tables is a comma join, or a CTE nothing reads: no "every row".
    keeps_first_table = all("LEFT" in kinds for kinds in joins) and (bool(joins) or not joined)
    read_too = [t for t in tables if t not in main and t != "RecordType"]
    tree = _tree_of(text)
    inner_cut = sql_tree.inner_cut(tree, _is_aggregate_function)
    cut = inner_cut or sql_tree.outer_cut(tree, _is_aggregate_function)
    set_operation = bool(_SET_OPERATION_RE.search(masked))
    if set_operation:
        head = f"Scope: {', '.join(main)}"
    else:
        head = f"Scope: {first}"
        if joined:
            head += f", joined to {', '.join(joined)}"
            if joins and all("ANTI" in kinds for kinds in joins):
                head += " (only rows with no match are kept)"
            elif joins and not any(kinds & _KEEPS_UNMATCHED for kinds in joins):
                head += " (rows with no match are left out)"
    if read_too:
        head += f", also reading {', '.join(read_too)}"
    if not _one_scope(masked, outer):
        if conditions or per_figure:
            head += ", with filters that differ by part of the query"
        per_figure = []
    elif conditions:
        lines: List[str] = []
        for condition in conditions:
            said = noted(condition)
            # "A and B OR C" would read as a different filter from the one that ran.
            if len(conditions) > 1 and _OR_WORD_RE.search(_blanked(_masked_strings(condition), _ANY_PAREN_RE)):
                said = f"({said})"
            if said not in lines:
                lines.append(said)
        shown = lines[: _MAX_SCOPE_FILTERS]
        extra = len(lines) - len(shown)
        head += ", where " + " and ".join(shown) + (f" and {extra} more condition(s)" if extra else "")
    elif not per_figure and keeps_first_table and not read_too and not on_limits and not cut:
        head = f"Scope: every row of {first}" + (f", joined to {', '.join(joined)}" if joined else "")
    head += "."
    if on_limits:
        shown = list(dict.fromkeys(spoken(condition) for condition in on_limits))[: _MAX_SCOPE_FILTERS]
        head += f" {' and '.join(shown)} only limits what was joined."
    if inner_cut:
        head += " A LIMIT inside the statement can have left rows out."
    if per_figure:
        # On a figure, not on the rows: `COUNT(*), COUNT(*) FILTER (WHERE x)` has one
        # column that counts every row, and "where x" would be false of that one.
        lines = list(dict.fromkeys(spoken(condition) for condition in per_figure))
        shown = lines[: _MAX_SCOPE_FILTERS]
        extra = len(lines) - len(shown)
        head += " Some figures count only rows where " + ", or only where ".join(shown)
        head += (f", and {extra} more condition(s)" if extra else "") + "."
    # True only if no part of the statement can have narrowed the record types. A
    # join to RecordType with any condition about can have: `kind = 'Interview'`.
    if on_limits or cut or any(kinds & _NOT_ITS_ROWS for kinds in joins):
        covers_every_type = False
    elif "RecordType" in tables:
        covers_every_type = not conditions
    else:
        covers_every_type = not _RECORD_TYPE_WORD_RE.search(_blanked(masked, _FILTER_CLAUSE_RE))
    for table, scope in _RECORD_TYPE_SCOPED.items():
        if table in main and covers_every_type:
            head += f" That covers {scope}."
    return head


#: A statement that returns figures rather than records.
_AGGREGATE_RE = re.compile(
    r"\b(?:COUNT|COUNT_IF|COUNTIF|SUM|FSUM|AVG|FAVG|MIN|MAX|MEDIAN|MODE|QUANTILE\w*|PERCENTILE_\w+|APPROX_\w+|STDDEV\w*"
    r"|VAR_\w+|VARIANCE|STRING_AGG|LISTAGG|GROUP_CONCAT|ARRAY_AGG|LIST|ARG_MAX|ARG_MIN|MAX_BY|MIN_BY|ANY_VALUE|FIRST|LAST"
    r"|BOOL_AND|BOOL_OR|PRODUCT)\s*\(|\bGROUP\s+BY\b",
    re.I,
)
#: What leaves fewer rows than matched, or one row per distinct value: no count of records.
_COLLAPSES_ROWS_RE = re.compile(r"\bSELECT\s+DISTINCT\b|\b(?:UNION|INTERSECT|EXCEPT)\b", re.I)
#: A period anywhere in a statement, whatever the column is called. A date held as
#: text reads 05-11-2026 or 10/%/2026%, so a year inside a literal counts too.
_DATE_IN_STATEMENT_RE = re.compile(
    r"\bnow\(\)|\btoday\s*\(|\bcurrent_(?:date|timestamp)\b|\bdate_(?:trunc|part|diff|add|sub)\b|\bstrftime\b"
    r"|\b(?:year|month)\s*\(|\bINTERVAL\b|\bDATE\s*'|'\d{4}-\d{2}"
    r"|'\d{1,2}[-/]\d{1,2}[-/]\d{4}|'[^']*(?<!\d)(?:19|20)\d{2}(?!\d)",
    re.I,
)
#: A period by name: Is_Current_Month__c = true, Leave_Year__c = 2026, Week_Number__c = 3,
#: Due_Date_IST__c, Joined_At__c. Never Candidate__c or Application_Update_Status__c.
_PERIOD_NAME_RE = re.compile(
    r"year|month|week|quarter|fiscal|period|(?<![a-z])days?(?![a-z])|today|(?<![a-z])(?:date|time|at)(?![a-z])|(?<![a-z])when_",
    re.I,
)


def _is_aggregate_function(name: str) -> bool:
    return name.lower() == "count_star" or bool(_AGGREGATE_RE.search(f"{name}("))


def _tree_of(sql: str) -> Optional[dict]:
    """DuckDB's reading of a statement that already ran, for what is said about it."""
    try:
        return sql_tree.read(sql)
    except ValueError:
        return None


def _collapses_rows(sql: Optional[str]) -> bool:
    return bool(_COLLAPSES_ROWS_RE.search(_masked_strings(sql)))


def _has_inner_cut(sql: str) -> bool:
    return sql_tree.inner_cut(_tree_of(sql), _is_aggregate_function)


def _no_date_line(sql: str, rows: Sequence[Sequence], schema: dict) -> str:
    """Says the answer covers every date, when the statement that ran shows it does.

    Plain counts used to be asked "over what period?". They are not any more, so
    this is where the person learns the reading taken. Worded about the statement,
    so it stays true whatever was or was not asked. A single record has no period.
    """
    if len(rows) <= 1 and not _AGGREGATE_RE.search(sql or ""):
        return ""
    # Anywhere, not only in a WHERE: a period in a JOIN or a HAVING is a filter too.
    # Saying nothing over a monthly bucket costs less than one false line.
    statement = _masked_strings(sql)
    if _DATE_IN_STATEMENT_RE.search(sql or "") or _PERIOD_NAME_RE.search(statement):
        return ""
    # A statement that read part of the rows covers no "all" of anything.
    tree = _tree_of(sql)
    if sql_tree.inner_cut(tree, _is_aggregate_function) or sql_tree.outer_cut(tree, _is_aggregate_function):
        return ""
    dated = {
        column.lower()
        for table in (_tables_read(sql, schema) if schema else [])
        for column, dtype in schema[table]
        if "DATE" in dtype.upper() or "TIME" in dtype.upper()
    }
    # `Date_of_Interview__c AS d` in a CTE, and `WHERE d IS NOT NULL` outside it.
    for source, alias in re.findall(r'"?([A-Za-z_]\w*)"?\)?\s+AS\s+"?([A-Za-z_]\w*)', statement, re.I):
        if source.lower() in dated:
            dated.add(alias.lower())
    for condition in _filters(sql):
        if {word.lower() for word in re.findall(r"[A-Za-z_]\w*", condition)} & dated:
            return ""
    return "No date filter was applied, so this covers all dates."


def _snapshot_time() -> Optional[str]:
    """When the synced copy being read was published, in UTC."""
    try:
        stamp = os.path.getmtime(settings.duckdb_path)
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")


def synced_sources(
    tables: Sequence[str],
    *,
    record_count: Optional[int] = None,
    truncated: bool = False,
    snapshot_time: Optional[str] = None,
) -> Optional[dict]:
    """`meta.salesforce_sources` for an answer read from the synced copy.

    None when no object is known, never an empty list: what reads this key relies
    on `objects` having something in it. RecordType is plumbing, as in the Scope line.
    """
    objects = [t for t in dict.fromkeys(tables) if t != "RecordType"] or list(dict.fromkeys(tables))
    if not objects:
        return None
    sources: dict = {
        "source": "synced",
        "objects": objects,
        "freshness": "synced_snapshot",
        "truncated": bool(truncated),
    }
    # The copy the rows came from, not the one published since.
    stamp = snapshot_time or _snapshot_time()
    if stamp:
        sources["query_timestamp"] = stamp
    if record_count is not None:
        sources["record_count"] = record_count
    return sources


def objects_read(sql: str, schema: dict) -> List[str]:
    """The objects a statement read, spelled as the warehouse does when it can be."""
    return _tables_read(sql, schema) if schema else _tables_named(sql)


def records_matched(sql: str, rows: Sequence[Sequence], total: Optional[int] = None) -> Optional[int]:
    """How many records matched, or None for a result of figures: one row of COUNT(*)
    is no "1 record", the very misreading "Number of Invoices: 1" came from."""
    if _AGGREGATE_RE.search(sql or "") or _collapses_rows(sql):
        return None
    if total == -1:  # the COUNT(*) wrap failed: more than was read, unknown how many
        return None
    return total if total is not None else len(rows)


def is_aggregate(sql: str) -> bool:
    return bool(_AGGREGATE_RE.search(sql or ""))


def asks_the_catalogue(message: str, statement: Optional[str]) -> bool:
    """Is a "not in the copy" really a question about the catalogue?

    The person's words decide, or a statement that tried to read the catalogue. A
    "(Clarified: object: X)" line would satisfy the check with its own label, and a
    statement naming an ordinary table is no question about the catalogue.
    """
    from .live_sf import is_schema_question

    if _reads_the_catalogue(statement):
        return True
    return is_schema_question(own_words(message)) and not (
        _STARTS_A_STATEMENT_RE.match(statement or "") and _tables_named(statement)
    )


def count_every_row(sql: str) -> int:
    """How many rows a result too long to summarise really holds, or -1 if unknown."""
    try:
        return _count_rows(sql.rstrip(";"))
    except Exception:  # noqa: BLE001 — the wrap is best-effort
        return -1  # unknown, but definitely more than the cap


def _count_rows(statement: str) -> int:
    """How many rows a statement holds, on what it read: behind the mask, or the count
    of a filter on a withheld value is a working oracle on that value, and in the time
    a figure may take. It runs for one question and is only ever an addition to it."""
    _c, counted = _execute(f"SELECT COUNT(*) FROM ({statement})", 2, time_limit=_COUNT_SECONDS, masked=True)
    return int(counted[0][0])


def _orders_its_outer_rows(sql: str) -> bool:
    """An ORDER BY of the statement itself, not of a subquery or a CTE."""
    masked = _masked_strings(sql)
    depth = 0
    for position, char in enumerate(masked):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif not depth and _ORDER_BY_RE.match(masked, position):
            return True
    return False


def _limit_total(sql: str, rows: Sequence[Sequence], question: str) -> Optional[int]:
    """The true row count, when a cut nobody asked for shaped this result, else None.

    "There are 30 Background_Check__c records": the writer ended the statement
    with LIMIT 30 and more match. Only when the person did not ask for a cut: an
    asked-for top 5 that returns 5 is the answer. -1 is a cut whose total could not
    be counted: None would read as the whole result.
    """
    match = _TRAILING_CUT_RE.search(sql or "")
    if not match:
        return None
    limit = match.group("limit") or match.group("fetch")
    offset = int(match.group("offset") or 0)
    if limit is None and not offset:
        return None
    # A page past its first can come back short and still be a cut.
    if not offset and int(limit) != len(rows):
        return None
    if len(rows) == 1 and _orders_its_outer_rows(sql[: match.start()]):
        # "Who is the top recruiter?", "what was the last invoice?": a single ordered
        # pick is what was asked, not the first row of something larger.
        return None
    if limit is not None and _asks_for_this_cut(int(limit), own_words(question)):
        return None
    try:
        total = _count_rows(sql[: match.start()])
    except Exception:  # noqa: BLE001 — the wrap is best-effort
        return -1
    return total if total > len(rows) else None


def _fetch_cut_total(sql: str, rows: Sequence[Sequence], question: str) -> Optional[int]:
    """The true total of a result the fetch cap cut, None when it cut nothing.

    An unasked LIMIT 1000 over 1,200 rows is not "of 1,000", so that LIMIT comes off first.
    """
    match = _TRAILING_CUT_RE.search(sql or "")
    limit = (match.group("limit") or match.group("fetch")) if match else None
    statement = sql[: match.start()] if match and not (limit and _asks_for_this_cut(int(limit), own_words(question))) else sql
    try:
        total = _count_rows(statement.rstrip().rstrip(";"))
    except Exception:  # noqa: BLE001 — the wrap is best-effort
        return -1
    return total if total > len(rows) else None


def _asks_for_this_cut(limit: int, own: str) -> bool:
    """Do the person's own words ask for exactly this many rows?

    Words alone are not enough: "some pending documents", "a 15 min call" and "the last
    30 days" turned the line off over a list that was cut. A line saying "first 5 of 47"
    is true however the question was put, so only these stay silent.
    """
    said = _NUMBER_WORDS.get(limit)
    if re.search(rf"\b(?:{limit}{'|' + said if said else ''})\b{_TIME_UNIT}", own, re.I):
        return True
    unquantified = re.sub(r"\bat\s+(?:least|most)\b", " ", own, flags=re.I)
    return limit == 1 and bool(_ASKS_FOR_ONE_PICK_RE.search(unquantified))


def _trailing_offset(sql: str) -> int:
    match = _TRAILING_CUT_RE.search(sql or "")
    return int(match.group("offset") or 0) if match else 0


def _cut_line(sql: str, shown: int, total: int) -> str:
    """What the reader is told of a result a cut shaped: never "first" of a later page."""
    offset = _trailing_offset(sql)
    if total <= 0:
        return f"These are {'the first ' if not offset else ''}{shown} rows of a larger result."
    if not offset:
        return f"These are the first {shown} of {total} matching rows."
    if not shown:
        return f"The statement skipped past all {total} matching rows, so none are shown."
    return f"These are rows {offset + 1} to {offset + shown} of {total} matching rows."


def _note_a_cut(computed: dict, sql: str, shown: int, total: int) -> None:
    """In the figures themselves, so the model reports "N of M" and not the cap as the population."""
    offset = _trailing_offset(sql)
    known = total if total > 0 else None
    computed["true_total_rows"] = known or "unknown (larger than summarised)"
    part = f"rows {offset + 1} to {offset + shown}" if offset else f"the FIRST {shown} rows"
    computed["counts_cover"] = (
        f"{part} of a larger result"
        + (f" of {known} total rows" if known else "")
        + " — state every figure as covering those rows, never as the whole population"
    )
    if offset and not shown:
        # Rows match, and the statement skipped past all of them: no "matched no rows".
        computed.pop("empty_result", None)
        computed.pop("what_zero_rows_means", None)
        computed["counts_cover"] = f"no rows: the statement skipped past all {known or 'of the'} matching rows"


async def _ask_chart_model(messages: List[dict]) -> str:
    # A chart spec is a small structured answer over columns the caller
    # already computed. Thinking draws from the same 2,500 tokens and has
    # returned an empty spec; off at every effort.
    return await llm.chat_completion(
        messages, temperature=0.0, max_tokens=2500, thinking=False
    )


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


#: Said by the ENGINE whenever a query matched no rows, before the model's
#: narration. The authoritative block below tells the model what zero rows
#: mean and asks it to say so — but an instruction is not a mechanism, and
#: the incident that block documents (five people with 84 interviews between
#: them reported as having none) is precisely the failure of trusting a model
#: to relay a caveat. Measured 2026-09-16 (E3): neither branch emitted a
#: fixed sentence; the live branch had this text only as a fallback for an
#: EMPTY model answer.
ZERO_ROWS_LINE = (
    "The query ran and matched no rows. That is not evidence the records do not exist: a wrong join, a name spelled "
    "differently in the data, an unparseable date or a narrower filter than intended all return an empty result."
)


#: The same warning for the other shape of nothing. `SELECT count(*)` always
#: returns a row, so the prefix never fired on a count: `Round__c = '1'` answered
#: 0 where rows match, as if it were a finding.
ZERO_FIGURE_LINE = (
    "The query ran and the figure came back 0. That is not evidence the records do not exist: a wrong join, a value "
    "spelled differently in the data, an unparseable date or a narrower filter than intended all count nothing."
)


#: No figure at all: MAX(date) or a list of names over no rows, or a rate whose divisor
#: counted nothing. "Came back 0" is false of each, and a 0 is not what they hold.
NOTHING_BACK_LINE = (
    "The query ran and no figure came back: there was nothing to work it out from. That is not evidence the records "
    "do not exist: a wrong join, a value spelled differently in the data, an unparseable date or a narrower filter "
    "than intended all leave nothing to work from."
)


def zero_rows_prefix(computed: dict) -> str:
    """The zero warning plus a blank line when the result counted nothing, else ""."""
    computed = computed or {}
    if computed.get("empty_table"):
        return computed["empty_table"] + "\n\n"
    if computed.get("empty_result"):
        return ZERO_ROWS_LINE + "\n\n"
    if computed.get("nothing_back"):
        return NOTHING_BACK_LINE + "\n\n"
    return ZERO_FIGURE_LINE + "\n\n" if computed.get("zero_figure") else ""


def _note_an_empty_table(computed: dict, sql: str, schema: dict) -> None:
    """The one table read holds no rows at all: then that is what the 0 says.

    "A wrong join or a value spelled differently" cannot explain `SELECT COUNT(*) FROM
    Asset`. Proved by reading the table, not by the statement's shape: a count of an
    empty column in a table full of rows has no filter or join either, and there
    the warning is right.
    """
    if not (computed.get("empty_result") or computed.get("zero_figure") or computed.get("nothing_back")):
        return
    tables = _tables_read(sql, schema)
    if len(tables) != 1:
        return
    try:
        _c, counted = _execute(f'SELECT COUNT(*) FROM "{tables[0]}"', 2)
    except Exception:  # noqa: BLE001 — a check, and only a check
        return
    if not counted[0][0]:
        line = (
            f"The synced copy of {tables[0]} holds no rows, so this counted nothing. "
            "The records may be kept under another object."
        )
        computed["empty_table"] = line
        computed["what_zero_rows_means"] = line


#: MIN, MAX, AVG or MEDIAN of a bare column or a plain cast of one: NULL over no rows,
#: so a 0 from one of these is a stored value.
_PLAIN_FIGURE_RE = re.compile(
    r"\b(?:MIN|MAX|AVG|MEDIAN)\s*\(\s*(?:(?:TRY_)?CAST\s*\(\s*(?:\w+\.)?\"?\w+\"?\s+AS\s+\w+(?:\s*\([\d,\s]*\))?\s*\)"
    r"|(?:\w+\.)?\"?\w+\"?)\s*\)",
    re.I,
)
_ZERO_MAKERS_RE = re.compile(r"\b(?:COALESCE|IFNULL|NVL|ZEROIFNULL|CASE|IF)\b", re.I)


def _zero_is_stored(statement: Optional[str]) -> bool:
    """A true 0 over real rows: `MIN(Outstanding_Amount__c)`. Anything unrecognised keeps the warning."""
    if not statement:
        return False
    masked = _masked_strings(statement)
    rest, found = _PLAIN_FIGURE_RE.subn(" ", masked)
    return bool(found) and not _AGGREGATE_RE.search(rest) and not _ZERO_MAKERS_RE.search(rest)


def deterministic_summary(
    columns: Sequence[str],
    rows: Sequence[Sequence],
    *,
    aggregated: Optional[bool] = None,
    statement: Optional[str] = None,
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

    `aggregated` says the statement computed figures (`is_aggregate`). Column names
    cannot say so: rows with a Total_Target__c are still one row per record, and a
    blank field on a record that was found is no figure of 0. `statement` is what ran,
    for the rest that only it can say.
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
            # Decimal too: a SUM on the typed money columns is DECIMAL, and it was
            # profiled as a category, "Paid: 1 (50%)", with no total at all.
            if _is_number(value):
                numbers.append(float(value))
            elif isinstance(value, str):
                try:
                    number = float(value.replace(",", ""))
                except ValueError:
                    numbers = []
                    break
                # The text 'Nan' is text.
                if not math.isfinite(number):
                    numbers = []
                    break
                numbers.append(number)
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
    # Value counts are occurrences, which only means something over RECORDS. On a
    # row that already carries its measure, or on one row, every value occurs once:
    # "OPT: 1 (14.29%)" for 7 grouped statuses was quoted as "each status is about
    # 14.29%", and "100% of" a single row was quoted in 6 of 40 answers.
    tree = _tree_of(statement) if statement else None
    # Two labels repeat, so an `AS interviews` with no measure word was profiled as
    # records: how many status rows a round has, under "quote these", as if it were
    # how many interviews it has.
    grouped = sql_tree.rows_are_groups(tree, _is_aggregate_function)
    collapsed = _collapses_rows(statement)
    carries_measure = bool(numeric) and (
        grouped
        or (aggregated is not False and any(_is_measure_name(name) for name in numeric))
        or all(p["denominator"] == len(p["values"]) for p in categorical.values())
    )
    if categorical and total > 1 and not carries_measure and not grouped and not collapsed:
        out["value_counts"] = categorical
    if categorical and total == 1:
        out["counts_cover"] = (
            "a single result row; total_rows is the number of result rows, not a "
            "count of records"
        )
    elif categorical and carries_measure:
        out["counts_cover"] = (
            "each result ROW carries its own figures in its numeric columns; "
            "total_rows is the number of rows, not a count of records"
        )
    elif categorical and grouped:
        out["counts_cover"] = "each result ROW is one group; total_rows is the number of rows, not a count of records"
    elif categorical and collapsed:
        out["counts_cover"] = "each result ROW is one distinct value; total_rows is the number of rows, not a count of records"

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
                label = row[label_index]
                breakdown[_BLANK_LABEL if label in (None, "") else str(label)] = row[value_index]
        out["row_breakdown"] = breakdown
        if total > 40:
            out["row_breakdown_truncated"] = f"first 40 of {total} rows"
        out["counts_cover"] = (
            f"each result ROW pairs one {label_name} with its {value_name} "
            f"value (see row_breakdown); the sum across rows is "
            f"{value_stats['sum']}. total_rows is the number of rows, not a "
            "count of records."
        )

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
        # A COUNT comes back whole: a DECIMAL `AS total` is a sum of money.
        count_like = [
            n
            for n in numeric
            if re.search(r"count|\bcnt\b|how_many|\btotal\b", n, re.I)
            and type(rows[0][names.index(n)]).__name__ != "Decimal"
        ]
        if count_like:
            out["record_count"] = int(numeric[count_like[0]]["sum"])
    # One row, and every figure in it nothing at all: a COUNT of 0, or a figure over
    # no rows, which is NULL. The empty-result warning belongs here too.
    if aggregated and total == 1 and not categorical and not numeric:
        out["nothing_back"] = True
        out["what_zero_rows_means"] = (
            "No figure came back, so do not state 0 or any other value for it. This is NOT evidence "
            "that the records do not exist: a wrong join, a value spelled differently in the data, an "
            "unparseable date literal or a too-narrow filter all leave nothing to work from. Say what "
            "the query looked for, and never state that the business has no such records."
        )
    elif (
        aggregated
        and total == 1
        and not categorical
        and not any(stats["sum"] for stats in numeric.values())
        and not _zero_is_stored(statement)
    ):
        out["zero_figure"] = True
        out["what_zero_rows_means"] = (
            "The figure came back 0. This is NOT evidence that the records do not "
            "exist: a wrong join, a value spelled differently in the data, an "
            "unparseable date literal or a too-narrow filter all count nothing. Say "
            "what the query looked for, and never state that the business has no "
            "such records."
        )
    if aggregated is False and total == 1:
        _note_blank_fields(out, names, rows[0], tree, bool(statement))
    return out


def _note_blank_fields(out: dict, names: Sequence[str], row: Sequence, tree: Optional[dict], ran: bool) -> None:
    """What a blank on one record says, and it depends on how the value was read.

    A join on the wrong key, or a lookup that points at another object, leaves a blank
    where the field is filled. "Blank on the record" was then a false fact, handed over
    in the block the model is told to quote.
    """
    blank = [name for index, name in enumerate(names) if index < len(row) and row[index] in (None, "")]
    plain = sql_tree.plain_columns(tree) if ran else {"*"}
    read = [name for name in blank if plain is not None and ("*" in plain or name.lower() in plain)]
    missed = [name for name in blank if name not in read]
    if read:
        out["blank_on_this_record"] = {
            "fields": read,
            "means": "The record was found and these fields are blank on it. Say that field is blank, "
            "and do not conclude there is none: another field may hold it.",
        }
    if missed:
        out["not_found_through_this_link"] = {
            "fields": missed,
            "means": "Nothing came back through this link. The link may be blank or may run through "
            "another object. Say it could not be found this way, never that the field is blank or "
            "that there is none.",
        }


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
    counted: str = "",
    held_back: Sequence[str] = (),
    file_line: str = "",
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
        "do not have it rather than deriving it. That block is for you: never "
        "name it to the reader, so no 'computed figures' and no 'based on the "
        "computed figures'.\n"
        "When you give a percentage or a ratio, state what it is a percentage "
        "OF, using the denominator given in the computed block. Do not present "
        "a ratio alongside counts that do not add up to its denominator.\n"
        + file_line
        + org_brief.ANSWER_RULES
    )
    row_count = (
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
    # The rows cannot show which filters ran, so it described ones that did not.
    scope = (
        f"What the query read, exactly as it ran (the reader is shown this line under "
        f"your answer, so do not repeat it, and describe no other filter): {counted}\n\n"
        if counted
        else ""
    )
    withheld = (
        "Held back from these rows, so not yours to guess: "
        f"{', '.join(held_back)}. If the question asked for a credential among them, "
        "say the field exists and that you will not read it out.\n\n"
        if held_back
        else ""
    )
    user = (
        f"Question: {question}\n\n{row_count}{scope}{withheld}{figures}"
        f"Result sample (illustration only, JSON): {sample}"
    )
    return [{"role": "system", "content": system}] + recent_turns(_without_recall(history), settings.chat_history_turns) + [
        {"role": "user", "content": user}
    ]


#: Internal wording the narrative still reached for in 13 of 40 answers. Longest
#: phrase it can start in the hold-back below; the opening clause goes entirely.
_BLOCK_CLAUSE_RE = re.compile(
    r"(?:based on|according to|as per|per|from)\s+the\s+(?:computed|authoritative)\s+"
    # The tail never takes a newline or a hyphen: "-450.00" came out as 450.00, and a
    # table lost the line break before its header.
    r"(?:figures?|block|results?)(?:\s+(?:for|over)\b)?[ \t,:;\u2014]*",
    re.I,
)
_BLOCK_ASIDE_RE = re.compile(r"\s*\(from\s+(?:the\s+)?computed\s+figures?\)", re.I)
_BLOCK_NAME_RE = re.compile(r"\b(computed|authoritative)\s+(figures?|block|results?)\b", re.I)
_BLOCK_WORDING_HOLD = 48
#: How much of one sentence naming Salesforce is held. Past this the reader has waited
#: long enough: the longest stored sentence is well under a third of it.
_SENTENCE_HOLD_CAP = 800


#: A phrase ending the text so far may still grow, by an "s", a ", there" or a "for". So
#: may the word after a clause: whether it takes a capital depends on the whole of it.
_BLOCK_PHRASES = (_BLOCK_ASIDE_RE, _BLOCK_CLAUSE_RE, _BLOCK_NAME_RE, re.compile(_BLOCK_CLAUSE_RE.pattern + r"\S*", re.I))
#: Where a sentence ends. A stop inside 3.5 or Status__c.Name is no end.
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s)|\n")

_LIVE_CHECK = r"(?:(?:the|a)\s+)?\*{0,2}(?:direct|live|real[- ]time)\s+salesforce\s+(?:checks?|quer(?:y|ies)|lookups?|reads?|data|results?)\b\*{0,2}"
#: "Based on the direct Salesforce check, there are 12 open cases", from the copy:
#: the model echoed the person's "check directly in Salesforce". Only these shapes
#: claim a live read. Everything else that mentions one is about one: "I cannot run a
#: live Salesforce check", "it can lag behind live Salesforce data". The Source line
#: already says where the rows came from, so what is not sure stays as it was said.
_CLAIM_ON_RE = re.compile(
    r"(?:\b(?:based\s+on|according\s+to|as\s+per)|(?:^\s*|[,;:(]\s*)(?:per|from)|\b(?:comes?|came|is|are|was|were)\s+from)\s+"
    rf"(?P<check>{_LIVE_CHECK})",
    re.I,
)
_CLAIM_BY_RE = re.compile(
    rf"(?P<check>{_LIVE_CHECK})\s+(?:shows?|showed|confirms?|confirmed|returns?|returned|reports?|reported|found|says?|said|indicates?)\b",
    re.I,
)
_CLAIM_FROM_RE = re.compile(r"\b(?:comes?|came|is|are|was|were)\s+(?P<check>(?:directly|live)\s+from\s+salesforce)\b", re.I)
#: Takes the claim back only when it negates the claim itself: "not based on a live
#: check", "No live check shows this". The "no" in "found no cases" denies nothing.
_DENIED_RE = re.compile(r"(?:\b(?:not|never|nor|than|without|instead\s+of)|n['’]t)\s+(?:\w+\s+){0,3}$", re.I)
_NO_BEFORE_RE = re.compile(r"\bno\s+$", re.I)
#: Said before the claim, these make the sentence about the live data, or a condition.
_ABOUT_THE_LIVE_DATA_RE = re.compile(
    r"\b(?:cannot|unable|unavailable|asked\s+for|if\s+you\s+need|differ\w*|lags?|behind|rather|if|whether|unless"
    r"|would|could|should|might|once|until|when|while)\b|\bwere\s+(?:from|based)\b",
    re.I,
)


def _without_live_claims(sentence: str, before: str = "") -> str:
    """One whole sentence, minus a claim of a live check.

    Once, on the whole sentence, because a denial can come after the claim ("A live
    Salesforce check is not available") or long before it. `before` is the part of
    this sentence already said: it counts, and it cannot be changed any more.
    """
    if "salesforce" not in sentence.lower():
        return sentence
    spoken = before + sentence

    def replaced(match, words: str, *, subject: bool = False) -> str:
        check, stop = match.span("check")
        text = match.string
        if check < len(before) or _ABOUT_THE_LIVE_DATA_RE.search(text[:check]):
            return match.group(0)
        lead = text[:check] if subject else text[: match.start()]
        if _DENIED_RE.search(lead) or _NO_BEFORE_RE.search(lead):
            return match.group(0)
        # "A live check shows" claims only when it opens a clause: not after "so", "only"
        # or "what", where it is a remark about the live data.
        clause_start = max(text.rfind(mark, 0, check) for mark in ",;:") + 1
        if subject and text[clause_start:check].strip(" *\t\n("):
            return match.group(0)
        said = words[0].upper() + words[1:] if text[check].isupper() else words
        return text[match.start() : check] + said + text[stop : match.end()]

    spoken = _CLAIM_ON_RE.sub(lambda m: replaced(m, "the synced Salesforce copy"), spoken)
    spoken = _CLAIM_BY_RE.sub(lambda m: replaced(m, "the synced Salesforce copy", subject=True), spoken)
    spoken = _CLAIM_FROM_RE.sub(lambda m: replaced(m, "from the synced Salesforce copy"), spoken)
    return spoken[len(before) :]


def _reworded(text: str, opening: bool) -> str:
    """Only the opening clause goes, and only when what follows reads on its own.

    Everywhere else the model's words stay and only the block's name goes: taking out
    a clause mid-answer left "****, there were 80", "80 interviews, ." and "row. the
    contact details".
    """
    text = _BLOCK_ASIDE_RE.sub("", text)
    start = _BLOCK_CLAUSE_RE.match(text)
    if opening and start and text[start.end() :] and text[start.end()] not in ".!?)\n-":
        said = start.group(0).rstrip(" \t,:;—")
        # "Based on the computed figures for all-time interviews" keeps its "for".
        kept = said[-4:] + " " if said.lower().endswith(" for") else said[-5:] + " " if said.lower().endswith(" over") else ""
        rest = kept.lstrip() + text[start.end() :]
        # A record Id or an e-mail keeps its case.
        first = (rest.split(None, 1) or [""])[0].strip(",.;:!?")
        text = rest[:1].upper() + rest[1:] if first.isalpha() and first.islower() else rest

    def name(match) -> str:
        word = "figures" if match.group(2).lower() == "block" else match.group(2)
        return word[:1].upper() + word[1:] if match.group(1)[:1].isupper() else word

    return _BLOCK_NAME_RE.sub(name, text)


class WordingHold:
    """A token stream minus any mention of the computed block or of a live check.

    A rule against naming it went into the prompt, and the prompt had already
    failed to stop a dozen other things. A phrase can span tokens, so the tail of
    the stream is held back until it can no longer be the start of one, and a
    sentence that names Salesforce until it ends. A class because the agent's stream
    carries reasoning events past it.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._opening = True
        #: What is already out of the sentence being said.
        self._said = ""

    def _whole_sentences(self) -> str:
        ready = ""
        while True:
            end = _SENTENCE_END_RE.search(self._pending)
            if not end:
                return ready
            ready += _without_live_claims(self._pending[: end.end()], self._said)
            self._pending = self._pending[end.end() :]
            self._said = ""

    def feed(self, token: str) -> str:
        self._pending += token
        if any(m.end() == len(self._pending) for phrase in _BLOCK_PHRASES for m in phrase.finditer(self._pending)):
            return ""
        self._pending = _reworded(self._pending, self._opening)
        ready = self._whole_sentences()
        over = len(self._pending) > _SENTENCE_HOLD_CAP
        if over:
            # Rewritten before any of it goes: a claim that opens a long sentence must
            # not reach the reader as it stands.
            self._pending = _without_live_claims(self._pending, self._said)
        if (over or "salesforce" not in self._pending.lower()) and len(self._pending) > _BLOCK_WORDING_HOLD:
            cut = len(self._pending) - _BLOCK_WORDING_HOLD
            self._said += self._pending[:cut]
            ready += self._pending[:cut]
            self._pending = self._pending[cut:]
        if ready:
            self._opening = False
        return ready

    def flush(self) -> str:
        self._pending = _reworded(self._pending, self._opening)
        ready = self._whole_sentences() + _without_live_claims(self._pending, self._said)
        self._pending = self._said = ""
        return ready


async def _without_block_wording(tokens: AsyncIterator[str]) -> AsyncIterator[str]:
    hold = WordingHold()
    async for token in tokens:
        ready = hold.feed(token)
        if ready:
            yield ready
    tail = hold.flush()
    if tail:
        yield tail


#: How many objects one catalogue reply lists. Common words are real table names
#: in a whole-org mirror (Group, Order, Note, Task, Case, User), so a sentence can
#: name more objects than it means.
_MAX_CATALOGUE_OBJECTS = 3

#: Whatever the capital letters, a `__c` name, or one of the standard objects a
#: person names outright: said back when the copy does not hold it.
_UNSYNCED_NAME_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*__c\b|\b(?:Account|Contact|Lead|Opportunity|Case|User)\b")


_STARTS_A_STATEMENT_RE = re.compile(r"\s*(?:SELECT|WITH)\b", re.I)


def _objects_named(message: str, tables: dict) -> Tuple[List[str], List[str]]:
    """(tables the message names, names it gives that the copy does not hold).

    The catalogue's own table names decide what counts as named, in any case and
    with or without `__c`. Ranked before the cap: what was written exactly and
    what ends in `__c` are meant, a lowercase "order" or "user" may just be a word.
    """
    own = own_words(message)
    by_lower = {name.lower(): name for name in tables}
    ranked = []
    for position, word in enumerate(dict.fromkeys(re.findall(r"[A-Za-z][A-Za-z0-9_]*", own))):
        low = word.lower()
        table = next(
            (by_lower[n] for n in (low, f"{low}__c", _stem(low), f"{_stem(low)}__c") if n in by_lower),
            None,
        )
        if table is not None:
            ranked.append((word != table and not table.endswith("__c"), word != table, position, table))
    named = list(dict.fromkeys(table for *_rank, table in sorted(ranked)))
    # The clarified line's words are machine labels ("metric", "object"), so only
    # an exact table name counts there: it is how "which fields does it have?"
    # still answers about the object this conversation carries.
    named += [t for t in _tables_in_clarified_line(message, tables) if t not in named]
    missing = [
        name for name in dict.fromkeys(_UNSYNCED_NAME_RE.findall(own))
        if name.lower() not in by_lower
    ]
    return named[:_MAX_CATALOGUE_OBJECTS], missing[:_MAX_CATALOGUE_OBJECTS]


def _catalogue_line(message: str) -> str:
    """"How many objects?", "which fields?": the warehouse's own catalogue, no model."""
    try:
        schema = schema_cache.get(settings.duckdb_path)
    except Exception:  # first boot, or locked with nothing cached
        schema = {}
    # _sync_meta and friends are the sync worker's bookkeeping, not synced objects.
    tables = {name: columns for name, columns in schema.items() if not name.startswith("_")}
    if not tables:
        return "The local Salesforce copy cannot be read right now — please try again in a few seconds."
    named, missing = _objects_named(message, tables)
    lines = [f"The synced Salesforce copy holds {len(tables)} tables."]
    for table in named:
        lines.append(f"{table} has {len(tables[table])} synced fields: " + ", ".join(c for c, _ in tables[table]))
    for name in missing:
        lines.append(f"{name} is not in the synced copy; an administrator can add it.")
    if not named and not missing:
        lines[0] += " Name an object to see its fields."
    return "\n\n".join(lines)


async def _object_advice(message: str) -> str:
    """" Naming the object usually helps", unless the question names a real person:
    their object is already known, so the advice sends the reader the wrong way."""
    try:
        people = await asyncio.to_thread(resolve_people, own_words(message))
    except Exception:  # noqa: BLE001 — the advice is never worth a failed answer
        people = []
    if people:
        return ""
    return " Naming the object usually helps (for example 'payments', 'invoices' or 'training enrollments')."


def _source_meta(
    tables: Sequence[str],
    *,
    record_count: Optional[int] = None,
    truncated: bool = False,
    assumptions: Sequence[str] = (),
    snapshot_time: Optional[str] = None,
) -> dict:
    """The meta keys that say where an answer came from.

    `assumptions_applied` is what tells a new answer's list from an old one's: until
    this change the key held whatever the planner assumed, shown only on live
    answers, whether or not the statement did it. Reopening an old chat must not
    put those on the screen, and here every line is checked against the statement.
    """
    meta: dict = {}
    sources = synced_sources(tables, record_count=record_count, truncated=truncated, snapshot_time=snapshot_time)
    if sources:
        meta["salesforce_sources"] = sources
    if assumptions:
        meta["assumptions"] = list(assumptions)
        meta["assumptions_applied"] = True
    return meta


async def run_sql_engine(
    message: str,
    history: Sequence[dict],
    emit: Emit,
) -> str:
    if not os.path.exists(settings.duckdb_path):
        await emit("token", {"text": NO_DATA_MESSAGE})
        await emit("meta", {"route": "sql"})
        return NO_DATA_MESSAGE

    wants_export = bool(EXPORT_RE.search(message))
    fetch_cap = (settings.export_row_cap + 1) if wants_export else (settings.sql_summary_row_cap + 1)

    left_out: Dict[str, List[str]] = {}
    try:
        result = await generate_and_run_sql(
            message, history=history, fetch_cap=fetch_cap
        )
        sql, columns, rows = result
        left_out = getattr(result, "left_out", {})
    except EmptySql:
        # Not a routing problem — the warehouse has the data and the model
        # failed to ask for it. Say so; going live here would answer a
        # question nobody successfully wrote a query for.
        text = (
            "I could not turn that into a query — the request came back empty "
            "twice. Try asking for one thing at a time."
            + await _object_advice(message)
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "sql"})
        return text
    except WarehouseBusy:
        # The sync worker holds the write lock for a moment. Never the raw
        # "Could not set lock" IO error.
        logging.getLogger(__name__).warning("the warehouse stayed locked past the wait")
        text = (
            "The local Salesforce copy is being refreshed by the sync "
            "worker right now — please try again in a few seconds."
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "sql"})
        return text
    except WarehouseUnreadable:
        # No query was written, so "could not write a valid query" and "name the
        # object" would both be false. salesforce_error is what the analytics page
        # counts as a failure, and its text reaches the browser, so never the raw
        # error: that holds the server's file path. _raise_if_warehouse_fault logged it.
        text = (
            "The synced Salesforce copy cannot be read right now, so I have no figure "
            "to give you. Please try again later, or tell an administrator if this "
            "keeps happening."
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "sql", "salesforce_error": "the synced copy cannot be read"})
        return text
    except (QueuedForRecovery, LeaseLost):
        raise  # parked for the main model, or another attempt owns the turn: never an answer
    except CredentialRead as refused:
        text = str(refused)
        await emit("token", {"text": text})
        await emit("meta", {"route": "sql"})
        return text
    except QueryTooSlow:
        # Stopped, not refused: naming the object would not help, and nor would a retry.
        text = (
            "That query took too long, so I stopped it and there is no number to report. "
            "Asking for one thing at a time usually helps."
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "sql", "salesforce_error": QUERY_TOO_SLOW, "salesforce_error_kind": "QueryTooSlow"})
        return text
    except NoSuchTable as reason:
        # A count of an unsynced object must not get the table total. The catalogue
        # line still reads the whole text, so "which fields does it have?" keeps the
        # object this conversation carries.
        if asks_the_catalogue(message, reason.statement):
            text = await asyncio.to_thread(_catalogue_line, message)
            await emit("token", {"text": text})
            await emit("meta", {"route": "sql"})
            return text
        text = (
            "I could not find that in the synced Salesforce copy, so I will "
            "not guess."
            + await _object_advice(message)
            + " If it is not synced yet, an administrator can add it."
        )
        await emit("token", {"text": text})
        await emit("meta", {"route": "sql"})
        return text

    except QueryRefused as exc:
        # BOTH query attempts failed — the retry (which now carries the failing
        # table's real column list) still produced something the database
        # refused. This used to re-raise, and main.py turned it into an `error`
        # SSE frame: after answering two clarifications the user got a red pill
        # reading `Binder Error: Table "p" does not have a column named
        # "Status__c"`. A query WE wrote wrong is our bug — say what happened
        # in words, keep the conversation. The raw detail stays on the server: it
        # quotes row values, and meta reaches the browser and the stored message.
        logging.getLogger(__name__).warning("both SQL attempts failed for %r: %s", message[:120], exc.detail[:300])
        # Not "nothing was run": two statements ran, and the database refused both.
        # The object advice is no use once the person named is already resolved.
        text = (
            "I could not write a valid query for that — I tried twice and the "
            "database refused both attempts, so there is no number to report."
            + await _object_advice(message)
        )
        await emit("token", {"text": text})
        await emit(
            "meta",
            {
                "route": "sql",
                "salesforce_error": QUERY_REFUSED,
                "salesforce_error_kind": exc.category,
            },
        )
        return text

    except Exception as exc:
        # No statement was refused. The model was away: the worker has the sentence and
        # the code for that, and a turn that never ran is not stored as an answer.
        if _is_model_failure(exc):
            raise
        logging.getLogger(__name__).warning("a SQL question failed for %r: %s", message[:120], type(exc).__name__)
        text = "I could not finish that query, so there is no number to report. Please try again."
        await emit("token", {"text": text})
        await emit("meta", {"route": "sql", "salesforce_error": "the query could not be finished", "salesforce_error_kind": type(exc).__name__})
        return text

    snapshot_time = getattr(result, "snapshot_time", None)
    try:
        schema = await asyncio.to_thread(schema_cache.get, settings.duckdb_path)
    except Exception:  # noqa: BLE001 — the source and scope lines can read the statement alone
        schema = {}

    if not columns and left_out:
        # Every column it read was one the question did not name. The empty-result
        # warning would say the query matched nothing, which is not what happened,
        # so the records it did match are counted here.
        total = await asyncio.to_thread(_limit_total, sql, rows, message)
        if len(rows) > settings.sql_summary_row_cap:
            total = await asyncio.to_thread(count_every_row, sql)
        text = _nothing_shown_line(sql, rows, left_out, total)
        await emit("token", {"text": text})
        await emit(
            "meta",
            {
                "route": "sql",
                "sql": sql,
                "data": [],
                "truncated": False,
                **_source_meta(objects_read(sql, schema), record_count=records_matched(sql, rows, total), snapshot_time=snapshot_time),
            },
        )
        return text

    # Even the summary cap can overflow. When it does, get the TRUE total with
    # a COUNT(*) wrap — cheap for DuckDB — so the model states real numbers
    # with an honest coverage note instead of presenting the cap as the total.
    summary_overflow: Optional[int] = await asyncio.to_thread(_limit_total, sql, rows, message)
    limited = summary_overflow is not None
    if len(rows) > settings.sql_summary_row_cap:
        rows = rows[: settings.sql_summary_row_cap]
        summary_overflow = await asyncio.to_thread(count_every_row, sql)

    # Bounded by cells as well as rows: the warehouse hands back every matching
    # row (fetch cap 100,001) in milliseconds, so the only reason to cut here is
    # what the browser and the message jsonb can carry — and that is a function
    # of WIDTH as much as length.
    preview, truncated = cap_rows(
        rows, preview_row_limit(len(columns), settings.sql_preview_row_cap)
    )
    # §10: data = array of row objects (≤500), truncated = TOP-LEVEL boolean.
    meta: dict = {
        "route": "sql",
        "sql": sql,
        "data": [dict(zip(columns, row)) for row in preview],
        "truncated": truncated,
        "provenance": {
            "source": "duckdb_snapshot",
            "environment": settings.sf_environment,
            "freshness": "synced_snapshot",
        },
    }

    file_failed = False
    if wants_export:
        exporter = export_csv if _CSV_RE.search(message) else export_xlsx
        try:
            path, _export_truncated = await asyncio.to_thread(
                exporter, columns, rows, settings.reports_dir, slug=slugify(message, fallback="query-export")
            )
        except Exception as exc:  # noqa: BLE001 — the answer matters, the file does not
            # The type only: openpyxl's own message quotes the cell.
            logging.getLogger(__name__).warning("the export could not be written: %s", type(exc).__name__)
            file_failed = True
        else:
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
    # distinction is the entire point: the preview and the 120-row sample the
    # model sees were both being used as the population for counts and ratios.
    computed = deterministic_summary(columns, rows, aggregated=is_aggregate(sql), statement=sql)
    await asyncio.to_thread(_note_an_empty_table, computed, sql, schema)
    if summary_overflow is not None:
        # The one case the figures do NOT cover everything — say so in the
        # authoritative block itself, with the true total when the COUNT(*)
        # wrap got one, so the model reports "N of M" instead of passing the
        # cap off as the population.
        _note_a_cut(computed, sql, len(rows), summary_overflow)

    await tracing.event(
        "RESULT_VERIFIED",
        component="orchestrator.app.engines.sql.deterministic_summary",
        details={
            "source": "local_salesforce_warehouse",
            "returned_rows": len(rows),
            "columns": columns,
            "computed": computed,
            "verification_scope": "deterministic_result_summary_only",
            "business_definition_verified": False,
        },
    )

    counted = _counted_line(sql, schema)
    no_date = _no_date_line(sql, rows, schema)
    meta.update(
        _source_meta(
            objects_read(sql, schema),
            record_count=records_matched(sql, rows, summary_overflow),
            # Part of the rows was read: a cut inside the statement is as much a cap.
            truncated=summary_overflow is not None or _has_inner_cut(sql),
            assumptions=[no_date] if no_date else [],
            snapshot_time=snapshot_time,
        )
    )

    parts: List[str] = []
    _zero = zero_rows_prefix(computed)
    if _zero:
        # Said by the engine because it knows: the retry was told and the filter
        # is still on a value the column does not hold.
        misses = await asyncio.to_thread(_literal_misses, sql, schema)
        if misses:
            _zero = f"{_zero.rstrip()} {' '.join(misses)}\n\n"
        parts.append(_zero)
        await emit("token", {"text": _zero})
    if limited:
        _first = f"{_cut_line(sql, len(rows), summary_overflow)}\n\n"
        parts.append(_first)
        await emit("token", {"text": _first})
    async for token in _without_block_wording(llm.stream_chat_completion(
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
            counted=" ".join(line for line in (counted, no_date) if line),
            held_back=[name for kind, names in left_out.items() if kind != "masked" for name in names],
            file_line=(
                "No file could be made of this result. Do not say a file is attached or ready.\n"
                if file_failed
                else "A file of this result is attached under your answer.\n"
                if wants_export
                else ""
            ),
        ),
        temperature=0.2,
        max_tokens=6000,
        thinking=False,
    )):
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

    # Written here, from the statement that ran: the reader can check what a number
    # counts without trusting the sentence above it.
    footer = " ".join(line for line in (counted, _left_out_line(left_out)) if line)
    if file_failed:
        footer = f"{footer} The file could not be made, so none is attached.".strip()
    if footer:
        answer = f"{answer}\n\n{footer}"
        await emit("token", {"text": f"\n\n{footer}"})

    # §10: the SINGLE meta event, emitted after the token stream, before done.
    await emit("meta", meta)
    return answer
