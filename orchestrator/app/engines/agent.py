"""Agent (deep-task) engine — V2-DESIGN §3b.

LangGraph subgraph "agent": PLAN → EXECUTE → SYNTHESIZE.

1. PLAN: the smart model (effort high) produces a JSON plan
   {"steps": [{id, title, kind: "sql"|"rag"|"llm", input}]}, validated by
   pydantic (≤8 steps). Invalid JSON → one retry, then a single-step llm
   fallback plan.
2. EXECUTE: steps run through the EXISTING engines (sql/rag helpers are
   reused, never forked) or a plain llm call; independent steps run
   concurrently via asyncio.gather capped at 3; each emits `step` events
   (running → done/failed with a short detail). Engine sub-metas are
   collected (sql text, citations, files). Data caps unchanged.
3. SYNTHESIZE: the smart model merges step outputs into the final streamed
   answer (reasoning + token events).

meta = {route: "agent", steps: [{id, title, status}], sql?, citations?,
report_files?} — last sql, union citations, union files. mode/model/effort
are merged in centrally by the /chat endpoint.
"""
from __future__ import annotations

import asyncio
import json
import operator
import os
import re
from typing import Annotated, Awaitable, Callable, List, Literal, Optional, Sequence, Tuple, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import CODE_INSTRUCTION, DIAGRAM_INSTRUCTION, recent_turns
from .. import llm
from ..continuity import LeaseLost, QueuedForRecovery
from ..config import settings

Emit = Callable[[str, dict], Awaitable[None]]

MAX_STEPS = 8
STEP_CONCURRENCY = 3

# Plan size by level — the schema cap stays MAX_STEPS; this is how many the
# planner is ASKED for. High is the level you pick for a hard question, so it
# is allowed to break the work down further than Medium.
_STEP_BUDGET = {"think": 5, "max": MAX_STEPS}
# Room for the final answer. A long plan needs a long synthesis, or High reads
# more and then truncates what it learned.
_SYNTH_TOKENS = {"think": 6000, "max": 12000}


def step_budget(effort: str) -> int:
    return _STEP_BUDGET.get(llm.normalize_effort(effort), _STEP_BUDGET["think"])

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


# ---------------------------------------------------------------------------
# Plan schema (pydantic-validated; model output is parsed, NEVER executed)
# ---------------------------------------------------------------------------

class PlanStep(BaseModel):
    id: int
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["sql", "rag", "llm", "web", "salesforce"]
    input: str = Field(min_length=1)


class AgentPlan(BaseModel):
    steps: List[PlanStep] = Field(min_length=1, max_length=MAX_STEPS)

    @field_validator("steps")
    @classmethod
    def _unique_ids(cls, steps: List[PlanStep]) -> List[PlanStep]:
        ids = [s.id for s in steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step ids must be unique")
        return steps


def parse_agent_plan(raw: object) -> AgentPlan:
    """Parse + validate a model-produced plan. Raises ValueError when invalid.

    Handles <think> preambles, code fences, and surrounding prose, like the
    other model-output parsers in this codebase.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("empty plan output")
    text = _THINK_RE.sub("", raw).strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in plan output")
    try:
        obj = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"plan is not valid JSON: {exc}") from exc
    try:
        return AgentPlan.model_validate(obj)
    except ValidationError as exc:
        raise ValueError(f"plan failed validation: {exc}") from exc


# ---------------------------------------------------------------------------
# PLAN
# ---------------------------------------------------------------------------

# Salesforce ON: the agent may query the warehouse (sql) and records (rag).
_PLAN_SYSTEM = (
    "You plan multi-step analysis work for the TechSara Local AI Analysis "
    f"Platform. Break the user's request into at most {MAX_STEPS} independent "
    "steps. Respond with ONLY this JSON object, no prose:\n"
    '{"steps": [{"id": 1, "title": "<short step title>", '
    '"kind": "sql" or "rag" or "llm" or "web", '
    '"input": "<full instruction for the step>"}]}\n'
    'Step kinds: "sql" computes exact numbers from the SYNCED Salesforce '
    'warehouse (fast, but up to 30 minutes stale and only the configured '
    'objects); "rag" finds and summarizes content inside Salesforce records; "web" '
    "searches the internet and answers from the pages it reads — use it for "
    "current events, market or competitor research, prices, and anything that "
    'changed after training; "llm" reasons '
    "over the conversation context (including any web pages, documents, or text "
    "the user shared earlier in this chat) and general knowledge. Steps must not "
    "depend on each other's outputs — they run in parallel.\n"
    "A request for DATA — a list, a count, records, a breakdown — MUST be a "
    "sql step. Never plan an llm step whose output would be a "
    "query for the user to run: this platform executes queries itself, and "
    "an answer telling the user to open Workbench is a failure.\n"
    "A web step's input is a search: never copy a person's name, e-mail "
    "address, phone number or other personal detail from pasted text into it."
)

# Salesforce OFF: NO Salesforce access. Everything is an "llm" step working from
# the conversation context (shared URLs/docs) + general knowledge.
_PLAN_SYSTEM_NO_SF = (
    "You plan multi-step work for a local AI assistant. Salesforce data is NOT "
    "available in this mode — do not try to query it. Two step kinds are "
    'available: "web" searches the internet and answers from the pages it '
    "reads (use it for current events, research, prices, and anything that "
    'changed after training), and "llm" reasons over the CONVERSATION CONTEXT '
    "(any web pages, documents, or text the user shared earlier in this chat) "
    f"plus general knowledge. Break the request into at most {MAX_STEPS} "
    "independent steps. Respond with ONLY this JSON object, no prose:\n"
    '{"steps": [{"id": 1, "title": "<short step title>", '
    '"kind": "llm" or "web", "input": "<full instruction>"}]}'
)


#: What a request for data sounds like, for the one moment no plan says so. An
#: agent turn is already a long request, so being generous costs little: the SQL
#: writer can still say no table holds this.
_DATA_REQUEST_RE = re.compile(
    r"\b(?:how many|how much|count|number of|total|sum|average|percent(?:age)?|ratio|"
    r"breakdown|list|top \d+|compare|comparison|audit|trend|status(?:es)?|by (?:status|month|week|year))\b",
    re.I,
)


def _fallback_plan(message: str, salesforce: bool = False) -> AgentPlan:
    """The plan when none came back. A request for data still gets a data step.

    An llm step cannot read a row: at effort think both plan calls came back empty,
    the fallback's one llm step ran no SQL, and after 654 s the answer said no data
    rows were provided.
    """
    wants_data = salesforce and _DATA_REQUEST_RE.search(message or "")
    return AgentPlan(
        steps=[
            PlanStep(
                id=1,
                title=(message.strip()[:60] or "Answer"),
                kind="sql" if wants_data else "llm",
                input=message,
            )
        ]
    )


def _coerce_no_salesforce(plan: AgentPlan) -> AgentPlan:
    """When Salesforce is off, no step may reach the warehouse or records — the
    toggle gates ALL Salesforce access. "web" is unrelated to Salesforce and is
    gated separately by `coerce_allowed`, so it survives here."""
    for step in plan.steps:
        # "salesforce" now reads the warehouse too — the toggle gates it
        # exactly like the other paths. _run_step_impl checks the flag too;
        # coercing here means the step never even gets planned.
        if step.kind in ("sql", "rag", "salesforce"):
            step.kind = "llm"
    return plan


def ensure_web_step(plan: AgentPlan, message: str) -> AgentPlan:
    """A FORCED web search must actually search.

    The composer's web pill collapses into the same boolean as the auto
    classifier by the time it reaches this engine, so the planner stayed free
    to plan zero web steps — and did: asked about a GPT-5.6 announcement with
    web search forced ON, it planned one "llm" step, answered from training
    memory that no such model exists, and the trust line under the composer
    still said "search queries are sent to the internet". The user switched
    the search on; honouring that is not the model's judgement call.

    The first llm step is converted rather than a step appended, so the plan
    shape (and its cost) is unchanged for the common one-step case.
    """
    if any(step.kind == "web" for step in plan.steps):
        return plan
    for step in plan.steps:
        if step.kind == "llm":
            step.kind = "web"
            return plan
    plan.steps.append(
        PlanStep(
            id=max((s.id for s in plan.steps), default=0) + 1,
            title="Web research",
            kind="web",
            input=message,
        )
    )
    return plan


def coerce_allowed(plan: AgentPlan, *, web: bool) -> AgentPlan:
    """Downgrade steps the caller is not allowed to run.

    Web search has its own switch (the composer's web toggle, and the effort
    ceiling in orchestrate.py). When it is off, a planner that asked for a web
    step gets an llm step — the work still happens, just from model knowledge.
    """
    if not web:
        for step in plan.steps:
            if step.kind == "web":
                step.kind = "llm"
    return plan


async def make_plan(
    message: str,
    history: Sequence[dict],
    salesforce: bool = True,
    effort: str = "medium",
) -> AgentPlan:
    """Smart model, effort high, big context; retry once, then llm fallback.

    When `salesforce` is False the planner is told it has no Salesforce access
    and any non-llm step is coerced to llm."""
    system = _PLAN_SYSTEM if salesforce else _PLAN_SYSTEM_NO_SF
    # The prompts are written with MAX_STEPS; ask for this level's budget.
    system = system.replace(f"at most {MAX_STEPS} ", f"at most {step_budget(effort)} ")
    # The agent path is reached BEFORE the router, so a "report" or "dashboard"
    # request handled here never sees engines/report.py and its canonical
    # section list. Left to invent its own steps, it planned a day-by-day
    # narrative off one of a candidate's five enrolments and drew nothing.
    user_content = message
    if salesforce:
        from ..core import org_brief

        template = org_brief.report_template_for(message)
        if template:
            user_content = (
                f"{template}\n\nPlan ONE \"sql\" step per section above, using "
                "the section title as the step title and its instruction as the "
                "step input. Each step must return a category column and a "
                "numeric count so the section can be drawn as a chart.\n\n"
                f"{message}"
            )
    messages = (
        llm.apply_reasoning_effort([{"role": "system", "content": system}], "max")
        + recent_turns(history, settings.chat_history_turns)
        + [{"role": "user", "content": user_content}]
    )
    last_error: Optional[str] = None
    for _attempt in range(2):  # one retry on invalid JSON (V2-DESIGN §3b)
        try:
            prompt = list(messages)
            if last_error:
                prompt.append(
                    {
                        "role": "user",
                        "content": (
                            f"Your previous plan was invalid: {last_error}\n"
                            "Return ONLY the corrected JSON object."
                        ),
                    }
                )
            # No reasoning pass, whatever the level. Reasoning draws on the same
            # budget as the plan, and at effort think both calls spent all 6,000
            # tokens on it (finish_reason length, empty content, 283 s each). A
            # plan is a short translation, like the planner and the SQL writer,
            # which turned thinking off for the same reason. The level still
            # decides how many steps are asked for and how the answer is written.
            raw = await llm.chat_completion(
                prompt,
                temperature=0.1,
                max_tokens=6000,
                thinking=False,
            )
            plan = parse_agent_plan(raw)
            return plan if salesforce else _coerce_no_salesforce(plan)
        except (QueuedForRecovery, LeaseLost):
            raise  # parked for the main model (CONTRACT §8.3): no fallback plan, the turn waits
        except Exception as exc:
            last_error = str(exc)[:400]
    return _fallback_plan(message, salesforce)


# ---------------------------------------------------------------------------
# EXECUTE — through the EXISTING engines; concurrency capped at 3
# ---------------------------------------------------------------------------

_STEP_LLM_SYSTEM = (
    "You are a careful analyst working on one step of a larger plan, inside a "
    "data platform that RUNS Salesforce queries itself.\n"
    # Asked for "a list of candidates having 150+ interviews", an llm step
    # answered like a coding tutor: SOQL "to run in Workbench" plus a Python
    # script — to a user who wanted rows, in a product whose entire point is
    # that IT runs the queries. The previous turn's SOQL error in the history
    # made it worse: the model saw broken SOQL and helpfully "fixed" it.
    "NEVER answer a request for data (a list, a count, records, a breakdown) "
    "with query code for the user to run — no SOQL, SQL, Apex or scripts 'for "
    "Workbench', the 'Developer Console' or any CLI. The user cannot run "
    "code; the platform's own query steps fetch data. If this step is really "
    "a data request, say in ONE line that the data should come from a "
    "Salesforce query step rather than writing the query as prose. Write code "
    "only when the user explicitly asked for code itself."
    + CODE_INSTRUCTION
)


def _shorten(text: str, limit: int = 120) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _run_step_impl(
    step: PlanStep,
    history: Sequence[dict],
    salesforce: bool,
    effort: str = "medium",
    emit: Optional[Emit] = None,
    message: str = "",
    user_id: Optional[int] = None,
    conversation_id: str = "",
) -> Tuple[str, str, dict]:
    """Run ONE step through the existing engines. → (output, detail, sub_meta).

    sql/rag only ever touch Salesforce, so they run only when the Salesforce
    toggle is on; otherwise the step is treated as an llm step. Every llm step
    receives the conversation history so it can use content the user shared in
    this chat (web pages, documents, pasted text).

    `message` is the user's original request. A plan step's `input` is
    written by the planner ("Count opportunities grouped by stage"), so the
    word the user actually typed — "chart" — is usually not in it. Chart
    intent is read from both."""
    # "salesforce" was the live step. A plan that still names it reads the
    # warehouse like any other data step.
    if step.kind in ("sql", "salesforce") and salesforce:
        from ..core.exports import cap_rows
        from ..core.schema_cache import schema_cache
        from .sql import (  # reuse (§3b)
            CredentialRead,
            NoSuchTable,
            QueryTooSlow,
            WarehouseBusy,
            WarehouseUnreadable,
            _catalogue_line,
            _counted_line,
            _cut_line,
            _has_inner_cut,
            _left_out_line,
            _limit_total,
            _literal_misses,
            _no_date_line,
            _note_a_cut,
            _note_an_empty_table,
            _nothing_shown_line,
            asks_the_catalogue,
            attach_chart,
            generate_and_run_sql,
            is_aggregate,
            objects_read,
            count_every_row,
            records_matched,
            zero_rows_prefix,
        )

        try:
            result = await generate_and_run_sql(
                step.input,
                history=list(history),
                # Ground on what the USER asked, not only on the planner's
                # paraphrase of it. The step input loses the names, the domain
                # nouns the brain packs trigger on, and the words that decide
                # which object a person lives on — so this route answered "no
                # data available for the specific individuals" and charted a
                # lone Human_Decision__c of 0, while the SAME question at Fast
                # or Low (which never reach the planner) answered correctly.
                grounding_question=message,
            )
            sql, columns, rows = result
        except CredentialRead as refused:
            return f"{refused} Say so; do not guess a value.", "credential fields", {}
        except QueryTooSlow:
            return (
                "That query took too long and was stopped, so this part has no figure. "
                "Say so; do not guess a value.",
                "query took too long",
                {},
            )
        except WarehouseBusy:
            return (
                "The synced Salesforce copy was being refreshed, so this part "
                "could not be read. Say so and suggest trying again in a few seconds.",
                "synced copy busy",
                {},
            )
        except WarehouseUnreadable:
            # A step's failure text reaches the screen and the synthesis, and the
            # raw DuckDB error holds the server's file path. Nothing is left for
            # the model to write, so this costs no call.
            if not os.path.exists(settings.duckdb_path):
                return (
                    "The synced Salesforce copy holds no data yet. Say so; do not "
                    "guess a value.",
                    "no synced copy yet",
                    {},
                )
            return (
                "The synced Salesforce copy cannot be read right now. Say so; do "
                "not guess a value.",
                "synced copy unreadable",
                {},
            )
        except NoSuchTable as reason:
            # The planner's input may name the object or the list word when the
            # person's message does not, so both are read. "not found" would be
            # false here: the catalogue holds the answer.
            asked = f"{message}\n{step.input}"
            if asks_the_catalogue(asked, reason.statement):
                # No statement ran, so no source is claimed. It is still the org's
                # field list, and sharing has to know that.
                return await asyncio.to_thread(_catalogue_line, asked), "from the synced catalogue", {"salesforce_catalogue": True}
            return (
                "This data was not found in the synced Salesforce copy. Say so; "
                "do not guess a value.",
                "not in the synced copy",
                {},
            )
        try:
            schema = await asyncio.to_thread(schema_cache.get, settings.duckdb_path)
        except Exception:  # noqa: BLE001 — the lines below can read the statement alone
            schema = {}
        # Who asked what decides what was held back, so the reader is told here too.
        left_out = getattr(result, "left_out", None) or {}
        left_out_line = _left_out_line(left_out)
        # The person's words only: a plan step that says "list 30" or "the latest" is
        # the planner's, and it asked for no cut.
        total = await asyncio.to_thread(_limit_total, sql, rows, message)
        if len(rows) > settings.sql_summary_row_cap:
            # The figures below would present the fetch cap as the population.
            rows = rows[: settings.sql_summary_row_cap]
            total = await asyncio.to_thread(count_every_row, sql)
        counted = _counted_line(sql, schema)
        no_date = _no_date_line(sql, rows, schema)
        sample = json.dumps(
            {"columns": list(columns), "rows": [list(r) for r in rows[:30]]}, default=str
        )
        preview, truncated = cap_rows(rows, settings.sql_preview_row_cap)
        sub_meta: dict = {
            "sql": sql,
            "data": [dict(zip(columns, row)) for row in preview],
            "truncated": truncated,
            # Read once by merge_step_meta and _source_footer, never sent on.
            "objects": objects_read(sql, schema),
            "counted": counted,
            "left_out_line": left_out_line,
            "no_date": no_date,
            "record_count": records_matched(sql, rows, total),
            "capped": total is not None or _has_inner_cut(sql),
            "snapshot_time": getattr(result, "snapshot_time", None),
        }
        if not columns and left_out:
            # Every column it read was one the question did not name: no rows of
            # nothing for the proof drawer, and the records it did match are counted.
            line = _nothing_shown_line(sql, rows, left_out, total)
            sub_meta.update(data=[], left_out_line=line)
            return line, "only fields not shown unless asked for", sub_meta
        # Same pipeline, same guarantees as the direct SQL route — an
        # agent-routed chart request now behaves identically to a direct one.
        await attach_chart(sub_meta, f"{message}\n{step.input}", columns, preview)
        # The synthesis used to see ONLY a 30-row sample — no authoritative
        # figures at all — so an agent-routed count was read off the sample
        # while the direct route quoted numbers computed over every row. Same
        # mechanism now: exact counts/totals over the WHOLE fetched result
        # ride with the step output, and the sample stays an illustration.
        from .sql import deterministic_summary

        computed = deterministic_summary(columns, rows, aggregated=is_aggregate(sql), statement=sql)
        await asyncio.to_thread(_note_an_empty_table, computed, sql, schema)
        if total is not None:
            _note_a_cut(computed, sql, len(rows), total)
        warnings = zero_rows_prefix(computed).strip()
        if warnings:
            # Same warning as the direct route: a 0 here reads like any other number.
            misses = await asyncio.to_thread(_literal_misses, sql, schema)
            warnings = " ".join([warnings, *misses])
        # Written under the answer by code too: an instruction is not a mechanism, and a
        # synthesis that wrote "There are 30" read as the whole truth over a cut result.
        sub_meta["zero_line"] = warnings
        if total is not None:
            sub_meta["first_line"] = _cut_line(sql, len(rows), total)
            warnings = f"{warnings} {sub_meta['first_line'].rstrip('.')}, and the answer must say so.".strip()
        told = "\n".join(
            line
            for line in (
                warnings,
                (
                    "What the query read, exactly as it ran (the reader is shown this "
                    "under the answer, so do not repeat it, and describe no other "
                    f"filter): {' '.join(x for x in (counted, no_date) if x)}"
                )
                if counted
                else "",
                left_out_line,
            )
            if line
        )
        return (
            f"SQL result ({len(rows)} row(s)):\n{sample}\n"
            + (f"{told}\n" if told else "")
            + "Computed figures (AUTHORITATIVE — calculated in code over every "
            f"row; quote these, never count the sample):\n"
            f"{json.dumps(computed, default=str)[:3000]}",
            f"{len(rows)} row(s)",
            sub_meta,
        )

    if step.kind == "rag" and salesforce:
        from ..core.citations import build_citations
        from .rag import _answer_messages, select_context  # reuse (§3b)

        hits = await select_context(step.input)
        answer = await llm.chat_completion(
            _answer_messages(step.input, hits, []),
            temperature=0.2,
            max_tokens=5000,
            thinking=llm.wants_thinking("smart", effort),
        )
        citations = build_citations(hits, base_url=settings.sf_lightning_base_url)
        return answer, f"{len(hits)} record(s)", {"citations": citations}

    if step.kind == "web":
        from .search import research_step  # reuse the Phase 1 pipeline

        # Pass emit so a plan's web steps feed ONE combined research panel.
        answer, sources = await research_step(
            step.input,
            list(history),
            effort,
            emit,
            # Who asked, for the V8 search log — the same attribution the
            # direct search route records (review round, 2026-08-30).
            user_id=user_id,
            conversation_id=conversation_id,
        )
        if sources:
            domains = ", ".join(dict.fromkeys(s["domain"] for s in sources))
            return (
                answer,
                f"{len(sources)} source(s): {_shorten(domains, 60)}",
                {"sources": sources},
            )
        # Search unavailable or nothing readable — answer from model knowledge
        # rather than failing the step and losing this part of the plan. But
        # SAY SO: this branch used to degrade silently, so an answer written
        # from training memory shipped under a trust line claiming search
        # queries were sent to the internet. The synthesis quotes step results,
        # so the caveat lands in front of the user.
        note = (
            "[Web search returned no readable sources for this step — the "
            "following is from general knowledge and may be out of date.]\n"
        )
        answer = await llm.chat_completion(
            [
                {"role": "system", "content":
                 "Answer from the conversation context and general knowledge. "
                 "State plainly that a web search found no readable sources "
                 "and that your knowledge may be out of date."},
                *recent_turns(history, settings.chat_history_turns),
                {"role": "user", "content": step.input},
            ],
            temperature=0.2,
            max_tokens=4000,
            thinking=llm.wants_thinking("smart", effort),
        )
        return (note + answer, "search returned nothing readable", {})

    # kind == "llm" (or sql/rag with Salesforce off, or web with no results):
    # reason over the conversation context (which includes any shared
    # URL/document content).
    answer = await llm.chat_completion(
        [
            {"role": "system", "content": _STEP_LLM_SYSTEM},
            *recent_turns(history, settings.chat_history_turns),
            {"role": "user", "content": step.input},
        ],
        temperature=0.3,
        max_tokens=5000,
        thinking=llm.wants_thinking("smart", effort),
    )
    return answer, _shorten(answer, 80), {}


async def execute_steps(
    plan: AgentPlan,
    history: Sequence[dict],
    emit: Emit,
    salesforce: bool = True,
    effort: str = "medium",
    message: str = "",
    user_id: Optional[int] = None,
    conversation_id: str = "",
) -> List[dict]:
    """Run all steps concurrently (cap 3), emitting step events (§3b)."""
    semaphore = asyncio.Semaphore(STEP_CONCURRENCY)

    async def run(step: PlanStep) -> dict:
        # Each step is its own task, in a COPY of the caller's context: the
        # usage its calls record dies with the task unless it is carried out
        # on the result (hotfix 1.2, P8).
        before = llm.get_usage()
        result = await _run(step)
        result["usage"] = llm.usage_since(before)
        return result

    async def _run(step: PlanStep) -> dict:
        async with semaphore:
            await emit("step", {"id": step.id, "title": step.title, "status": "running"})
            try:
                output, detail, sub_meta = await _run_step_impl(
                    step,
                    history,
                    salesforce,
                    effort,
                    emit,
                    message,
                    user_id,
                    conversation_id,
                )
            except (QueuedForRecovery, LeaseLost):
                raise  # parked for the main model: not a failed step
            except Exception as exc:
                from .sql import failure_reason

                # A failed model call carries the server's address in its message.
                detail = _shorten(failure_reason(exc), 200)
                await emit(
                    "step",
                    {"id": step.id, "title": step.title, "status": "failed", "detail": detail},
                )
                return {
                    "step": step,
                    "status": "failed",
                    "output": f"Step failed: {detail}",
                    "meta": {},
                }
            await emit(
                "step",
                {"id": step.id, "title": step.title, "status": "done", "detail": detail},
            )
            return {"step": step, "status": "done", "output": output, "meta": sub_meta}

    # Every failure is caught inside `run`; the one exception that escapes
    # is the park (QueuedForRecovery). Gathered with return_exceptions so
    # every sibling step ends (a parked hold ends them at once) before it is
    # raised once, rather than leaving sibling tasks to raise unread.
    results = await asyncio.gather(*(run(step) for step in plan.steps), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return list(results)


_CITE_RE = re.compile(r"\[(\d+)\]")


def renumber_web_sources(results: Sequence[dict]) -> None:
    """Give every web source ONE plan-wide number — in the prose AND the meta.

    Each web step searches independently and numbers its own sources from [1],
    so two steps both produce a "[1]" meaning different pages. Renumbering only
    the metadata (what this used to do) left every marker in the step prose
    pointing at the wrong entry: step 2's "[1]" is plan-wide source 4, but the
    synthesizer reads "[1]" and repeats it, so the finished answer cites a page
    that has nothing to do with the claim.

    Mutates `results` in place so the synthesis prompt and the final meta are
    built from the same numbering — computing them separately is exactly how
    the two drifted apart.
    """
    seen: dict = {}          # url -> plan-wide n
    for r in results:
        sub = (r.get("meta") or {})
        sources = sub.get("sources") or []
        if not sources:
            continue
        local_to_global: dict = {}
        renumbered = []
        for s in sources:
            url = s.get("url")
            if not url:
                continue
            if url not in seen:
                seen[url] = len(seen) + 1
            local_to_global[s.get("n")] = seen[url]
            renumbered.append({**s, "n": seen[url]})
        sub["sources"] = renumbered
        r["meta"] = sub
        # Rewrite this step's own markers. A number the step never cited is
        # left alone — it belongs to the page's text, not to our numbering.
        output = r.get("output")
        if isinstance(output, str) and local_to_global:
            r["output"] = _CITE_RE.sub(
                lambda m: f"[{local_to_global[int(m.group(1))]}]"
                if int(m.group(1)) in local_to_global
                else m.group(0),
                output,
            )


#: Keys that describe ONE sql step's result. They are carried across as a
#: unit, from the single step that produced them.
#:
#: THE BUG this fixes: only `sql` used to survive the merge, so an
#: agent-routed question never had `meta.data` and therefore never had a
#: chart — the exact same question answered by the direct SQL route drew
#: one. `data` also has to travel because the frontend renders the chart
#: over `meta.data`; carrying `chart` alone would leave a spec pointing at
#: rows that were dropped.
_SQL_PAYLOAD_KEYS = ("sql", "data", "truncated", "chart", "chart_data")


#: Written by the engine, never by the model: the synthesis once opened "Based on
#: the direct Salesforce check" over a warehouse read, echoing the person's words.
SOURCE_LINE = "Source: the synced Salesforce copy, not a live query."


def _source_footer(results: Sequence[dict]) -> str:
    """What the answer read, one line per sql step, from the statements that ran.

    Each line follows sql._counted_line, so it says the same about a record type or
    a blank-dropping filter as the direct route does.
    """
    sql_steps = [r for r in results if (r.get("meta") or {}).get("sql")]
    if not sql_steps:
        return ""
    lines = [SOURCE_LINE]
    seen = set()
    for r in sql_steps:
        sub = r["meta"]
        line = " ".join(
            x for x in (sub.get("zero_line"), sub.get("first_line"), sub.get("counted"), sub.get("left_out_line")) if x
        )
        if not line or line in seen:
            continue
        seen.add(line)
        lines.append(f"{_shorten(r['step'].title, 60)}: {line}" if len(sql_steps) > 1 else line)
    return "\n\n".join(lines)


def _source_meta_for(results: Sequence[dict]) -> dict:
    """`salesforce_sources` and `assumptions` across every sql step that ran."""
    from .sql import _source_meta

    subs = [r["meta"] for r in results if (r.get("meta") or {}).get("sql")]
    if not subs:
        return {}
    only = subs[0] if len(subs) == 1 else {}
    return _source_meta(
        [table for sub in subs for table in sub.get("objects") or []],
        # A figure per step does not add up to anything, so one step only.
        record_count=only.get("record_count"),
        truncated=any(sub.get("capped") for sub in subs),
        # True of the answer only if it is true of every number in it.
        assumptions=(
            [subs[0]["no_date"]]
            if all(sub.get("no_date") for sub in subs)
            else []
        ),
        # A mixed answer never claims newer than its oldest read.
        snapshot_time=min((sub["snapshot_time"] for sub in subs if sub.get("snapshot_time")), default=None),
    )


def merge_step_meta(results: Sequence[dict]) -> dict:
    """Merged agent meta (§3b): last sql step, union citations, union files.

    Exactly one meta frame is produced, here, by the caller — §10 allows one
    per turn and the frontend replaces it wholesale.
    """
    meta: dict = {
        "route": "agent",
        "steps": [
            {"id": r["step"].id, "title": r["step"].title, "status": r["status"]}
            for r in results
        ],
        **_source_meta_for(results),
    }
    if any((r.get("meta") or {}).get("salesforce_catalogue") for r in results):
        meta["salesforce_catalogue"] = True
    sql_payload: dict = {}
    citations: List[dict] = []
    seen_records = set()
    report_files: List[dict] = []
    seen_files = set()
    sources: List[dict] = []
    seen_urls = set()
    for r in results:
        sub = r.get("meta") or {}
        if sub.get("sql"):
            # Last sql step wins (plan order) — and its data/chart come with
            # it, atomically. Taking `chart` from one step and `data` from
            # another would render one query's spec over another's rows.
            sql_payload = {k: sub[k] for k in _SQL_PAYLOAD_KEYS if k in sub}
        for c in sub.get("citations") or []:
            rid = c.get("record_id")
            if rid and rid not in seen_records:
                seen_records.add(rid)
                citations.append(c)
        for f in sub.get("report_files") or []:
            name = f.get("filename")
            if name and name not in seen_files:
                seen_files.add(name)
                report_files.append(f)
        # Sources already carry plan-wide numbers (renumber_web_sources ran
        # before synthesis); just collect them, keeping the first of each url.
        for s in sub.get("sources") or []:
            url = s.get("url")
            if url and url not in seen_urls:
                seen_urls.add(url)
                sources.append(dict(s))
    meta.update(sql_payload)
    if citations:
        meta["citations"] = citations
    if sources:
        meta["sources"] = sources
    if report_files:
        meta["report_files"] = report_files
    return meta


# ---------------------------------------------------------------------------
# SYNTHESIZE
# ---------------------------------------------------------------------------

_SYNTH_SYSTEM = (
    "You are the TechSara analyst. Merge the step results below into one "
    "clear, well-structured answer to the user's request. Use ONLY facts and "
    "numbers present in the step results — never fabricate values. Briefly "
    "note any step that failed. Where a step result cites a web source with a "
    "bracketed number like [2], keep that citation on the claim it supports; "
    "never invent a citation number that no step produced.\n"
    "When the user asked for DATA, the answer is the data from the step "
    "results — never a SOQL/SQL query, an Apex class or a script for the "
    "user to run somewhere else. This platform runs queries itself; if the "
    "steps returned no rows, say what was looked for and found, not how the "
    "user could query it by hand.\n"
    # The code-written Source line under the answer is what the reader can rely on;
    # this keeps the sentences above it from saying the opposite. The model echoed
    # a person's "check directly in Salesforce" as "the direct Salesforce check".
    "SQL step results come from the local synced copy of Salesforce. Never say a "
    "number came from a live or direct check of Salesforce, whatever the request "
    "asked for. A step result that says what the query read is the only thing you "
    "may say about its filters: the reader is shown that line and the source under "
    "your answer, so write neither yourself."
)
# --- AS3 intent-capability BEGIN --- (the file capability line, engines/capability.py)
from .capability import capability_suffix as _as3_capability_suffix  # noqa: E402

_AS3_CAPABILITY = _as3_capability_suffix()

_SYNTH_SYSTEM = _SYNTH_SYSTEM + _AS3_CAPABILITY
# --- AS3 intent-capability END ---


def _synthesis_messages(message: str, results: Sequence[dict]) -> List[dict]:
    blocks = [
        f"### Step {r['step'].id}: {r['step'].title} [{r['status']}]\n{r['output']}"
        for r in results
    ]
    # The steps' chart survives the meta merge (see _SQL_PAYLOAD_KEYS), so at
    # synthesis time "a real chart will render below" is a fact the model can
    # be told — the same mechanism as the SQL engine's narration. Without it,
    # a synthesis asked for a "bar chat" drew the chart itself out of █
    # characters in a code block, on top of the real one.
    from .sql import _chart_line

    chart_attached = any(
        (r.get("meta") or {}).get("chart") for r in results
    )
    user = f"Request: {message}\n\nStep results:\n\n" + "\n\n".join(blocks)
    return [{"role": "system", "content": _SYNTH_SYSTEM + "\n"
             + _chart_line(chart_attached) + DIAGRAM_INSTRUCTION},
            {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# LangGraph subgraph "agent" (§3b): plan → execute → synthesize
# ---------------------------------------------------------------------------

class AgentState(TypedDict, total=False):
    message: str
    history: List[dict]
    emit: Emit
    effort: str
    salesforce: bool
    web: bool
    #: The user FORCED search on (web pill), as opposed to auto allowing it.
    web_forced: bool
    #: Search-log attribution: whose conversation these web steps belong to.
    user_id: Optional[int]
    conversation_id: str
    plan: AgentPlan
    results: List[dict]
    answer: str
    #: The usage every node's calls reported. LangGraph runs each node in a
    #: COPY of the caller's context, so `llm._usage` recorded inside a node
    #: never reached the turn: agent turns were stored with NULL tokens
    #: (hotfix 1.2, P8). Each node returns its own share; the reducer
    #: collects them and `run_agent_engine` folds them into the turn.
    usage: Annotated[List[dict], operator.add]


def _spent(before: Optional[dict]) -> List[dict]:
    spent = llm.usage_since(before)
    return [spent] if spent else []


async def _plan_node(state: AgentState) -> dict:
    before = llm.get_usage()
    plan = await make_plan(
        state["message"],
        state.get("history", []),
        state.get("salesforce", True),
        state.get("effort", "medium"),
    )
    plan = coerce_allowed(plan, web=state.get("web", True))
    if state.get("web", True) and state.get("web_forced", False):
        plan = ensure_web_step(plan, state["message"])
    return {"plan": plan, "usage": _spent(before)}


async def _execute_node(state: AgentState) -> dict:
    before = llm.get_usage()
    results = await execute_steps(
        state["plan"],
        state.get("history", []),
        state["emit"],
        state.get("salesforce", True),
        state.get("effort", "medium"),
        state.get("message", ""),
        state.get("user_id"),
        state.get("conversation_id", ""),
    )
    steps = [u for u in (r.pop("usage", None) for r in results) if u]
    return {"results": results, "usage": _spent(before) + steps}


async def _synthesize_node(state: AgentState) -> dict:
    from .sql import WordingHold

    before = llm.get_usage()
    emit = state["emit"]
    results = state.get("results", [])
    # Must happen BEFORE the synthesis prompt is built: the model copies the
    # markers it reads, so they have to be plan-wide by the time it sees them.
    renumber_web_sources(results)
    parts: List[str] = []
    footer = _source_footer(results)
    # Only over sql results: a web step's "live data" is about somewhere else.
    hold = WordingHold() if footer else None

    async def say(text: str) -> None:
        if text:
            parts.append(text)
            await emit("token", {"text": text})

    # Smart model merges step outputs (§3b); streams reasoning + token events.
    async for kind, text in llm.stream_chat_events(
        _synthesis_messages(state["message"], results),
        model_choice="smart",
        effort=state.get("effort", "medium"),
        temperature=0.2,
        max_tokens=_SYNTH_TOKENS.get(
            llm.normalize_effort(state.get("effort", "think")), 6000
        ),
    ):
        if kind == "reasoning":
            await emit("reasoning", {"text": text})
        else:
            await say(hold.feed(text) if hold else text)
    if hold:
        await say(hold.flush())
    if footer:
        await say(f"\n\n{footer}" if parts else footer)

    # §10: the SINGLE final meta, after the token stream, before done.
    await emit("meta", merge_step_meta(results))
    return {"answer": "".join(parts), "usage": _spent(before)}


def build_agent_graph():
    graph = StateGraph(AgentState)
    graph.add_node("plan", _plan_node)
    graph.add_node("execute", _execute_node)
    graph.add_node("synthesize", _synthesize_node)
    graph.set_entry_point("plan")
    graph.add_edge("plan", "execute")
    graph.add_edge("execute", "synthesize")
    graph.add_edge("synthesize", END)
    return graph.compile()


_compiled = None


def get_agent_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_agent_graph()
    return _compiled


async def run_agent_engine(
    message: str,
    history: Sequence[dict],
    emit: Emit,
    *,
    effort: str = "medium",
    salesforce: bool = True,
    web: bool = True,
    web_forced: bool = False,
    user_id: Optional[int] = None,
    conversation_id: str = "",
) -> str:
    """Entry point used by /chat when agent=true.

    `salesforce` gates Salesforce access: when False the agent plans and runs
    only llm steps over the conversation context (shared URLs/docs) + general
    knowledge — it never queries the warehouse or records.

    `web` gates internet access the same way: when False, web steps become llm
    steps, so a user who turned web search off never gets a network fetch."""
    # A web step's query never carries the message's pasted text, whatever
    # step input the planner wrote (hotfix 1.2, P6; checked in
    # engines/search.py `_collect_results`).
    from ..core import pasted

    pasted.mark_turn(message)
    before = llm.get_usage()
    state = await get_agent_graph().ainvoke(
        {
            "message": message,
            "history": list(history),
            "emit": emit,
            "effort": effort,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "salesforce": salesforce,
            "web": web,
            "web_forced": web_forced,
        }
    )
    llm.fold_usage(before, state.get("usage") or [])
    return state.get("answer") or ""
