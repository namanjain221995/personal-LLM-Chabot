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
import re
from typing import Awaitable, Callable, List, Literal, Optional, Sequence, Tuple, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import CODE_INSTRUCTION, DIAGRAM_INSTRUCTION, recent_turns
from .. import llm

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
    '"kind": "sql" or "rag" or "salesforce" or "llm" or "web", '
    '"input": "<full instruction for the step>"}]}\n'
    'Step kinds: "sql" computes exact numbers from the SYNCED Salesforce '
    'warehouse (fast, but up to 30 minutes stale and only the configured '
    'objects); "salesforce" queries the LIVE org over the API — use it when '
    "the question is about something created or changed today, names a record "
    "directly, or concerns an object the warehouse does not carry; "
    '"rag" finds and summarizes content inside Salesforce records; "web" '
    "searches the internet and answers from the pages it reads — use it for "
    "current events, market or competitor research, prices, and anything that "
    'changed after training; "llm" reasons '
    "over the conversation context (including any web pages, documents, or text "
    "the user shared earlier in this chat) and general knowledge. Steps must not "
    "depend on each other's outputs — they run in parallel.\n"
    "A request for DATA — a list, a count, records, a breakdown — MUST be a "
    "sql or salesforce step. Never plan an llm step whose output would be a "
    "query for the user to run: this platform executes queries itself, and "
    "an answer telling the user to open Workbench is a failure."
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


def _fallback_plan(message: str) -> AgentPlan:
    return AgentPlan(
        steps=[PlanStep(id=1, title=(message.strip()[:60] or "Answer"), kind="llm", input=message)]
    )


def _coerce_no_salesforce(plan: AgentPlan) -> AgentPlan:
    """When Salesforce is off, no step may reach the warehouse or records — the
    toggle gates ALL Salesforce access. "web" is unrelated to Salesforce and is
    gated separately by `coerce_allowed`, so it survives here."""
    for step in plan.steps:
        # "salesforce" is a LIVE query against the org — the toggle gates it
        # exactly like the warehouse paths. _run_step_impl checks the flag too;
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
        + recent_turns(history, 6)
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
            raw = await llm.chat_completion(prompt, temperature=0.1, max_tokens=6000)
            plan = parse_agent_plan(raw)
            return plan if salesforce else _coerce_no_salesforce(plan)
        except Exception as exc:
            last_error = str(exc)[:400]
    return _fallback_plan(message)


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
    if step.kind == "sql" and salesforce:
        from ..config import settings
        from ..core.exports import cap_rows
        from .live_sf import fetch_live
        from .sql import (  # reuse (§3b)
            NoSuchTable,
            WarehouseBusy,
            attach_chart,
            generate_and_run_sql,
        )

        try:
            sql, columns, rows = await generate_and_run_sql(
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
        except (WarehouseBusy, NoSuchTable) as reason:
            # The direct SQL route answers these from live Salesforce. This
            # one used to let the exception out, so a locked warehouse — which
            # is most of the day — reached the user as a raw
            # "Could not set lock on file /data/warehouse.duckdb ... PID 0".
            soql, live_rows = await fetch_live(step.input, list(history))
            sample = json.dumps(live_rows[:30], default=str)
            preview, truncated = live_rows[: settings.sql_preview_row_cap], (
                len(live_rows) > settings.sql_preview_row_cap
            )
            return (
                f"Live Salesforce result ({len(live_rows)} row(s)):\n{sample}",
                f"{len(live_rows)} row(s), live from Salesforce",
                {
                    "sql": soql,
                    "live": True,
                    "reason": str(reason),
                    "data": preview,
                    "truncated": truncated,
                },
            )
        sample = json.dumps(
            {"columns": list(columns), "rows": [list(r) for r in rows[:30]]}, default=str
        )
        preview, truncated = cap_rows(rows, settings.sql_preview_row_cap)
        sub_meta: dict = {
            "sql": sql,
            "data": [dict(zip(columns, row)) for row in preview],
            "truncated": truncated,
        }
        # Same pipeline, same guarantees as the direct SQL route — an
        # agent-routed chart request now behaves identically to a direct one.
        await attach_chart(sub_meta, f"{message}\n{step.input}", columns, preview)
        # The synthesis used to see ONLY a 30-row sample — no authoritative
        # figures at all — so an agent-routed count was read off the sample
        # while the direct route quoted numbers computed over every row. Same
        # mechanism now: exact counts/totals over the WHOLE fetched result
        # ride with the step output, and the sample stays an illustration.
        from .sql import deterministic_summary

        computed = deterministic_summary(columns, rows)
        return (
            f"SQL result ({len(rows)} row(s)):\n{sample}\n"
            "Computed figures (AUTHORITATIVE — calculated in code over every "
            f"row; quote these, never count the sample):\n"
            f"{json.dumps(computed, default=str)[:3000]}",
            f"{len(rows)} row(s)",
            sub_meta,
        )

    if step.kind == "rag" and salesforce:
        from ..config import settings
        from ..core.citations import build_citations
        from .rag import _answer_messages, select_context  # reuse (§3b)

        hits = await select_context(step.input)
        answer = await llm.chat_completion(
            _answer_messages(step.input, hits, []), temperature=0.2, max_tokens=5000
        )
        citations = build_citations(hits, base_url=settings.sf_lightning_base_url)
        return answer, f"{len(hits)} record(s)", {"citations": citations}

    if step.kind == "salesforce" and salesforce:
        # Straight to the org: newer than the warehouse, and works on objects
        # the warehouse does not carry. Honors SF_LIVE_ENABLED like the SQL
        # engine's fallback does — this path used to bypass the flag entirely.
        from ..config import settings
        from ..core.salesforce import (SalesforceUnavailable, UnsafeSoql,
                                       merge_rows)
        from .live_sf import describe_rows, fetch_live

        if not settings.sf_live_enabled:
            return (
                "Live Salesforce lookups are disabled (SF_LIVE_ENABLED). "
                "Answer from the synced warehouse instead.",
                "live lookups disabled",
                {},
            )
        try:
            soql, live_rows = await fetch_live(step.input, history)
        except (SalesforceUnavailable, UnsafeSoql) as exc:
            # The warehouse is still there — degrade to it rather than failing
            # the step and losing this part of the plan.
            return (
                f"Live Salesforce lookup unavailable ({exc}). "
                "Answer from the synced warehouse instead.",
                "live lookup unavailable",
                {},
            )
        rows = merge_rows([], live_rows)
        return (
            f"Live Salesforce query:\n{soql}\n\nRows ({len(rows)}):\n"
            + describe_rows(rows),
            f"{len(rows)} live record(s)",
            {"sql": soql, "data": rows[:50]},
        )

    if step.kind == "web":
        from .search import research_step  # reuse the Phase 1 pipeline

        # Pass emit so a plan's web steps feed ONE combined research panel.
        answer, sources = await research_step(
            step.input, list(history), effort, emit
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
                *recent_turns(history, 6),
                {"role": "user", "content": step.input},
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        return (note + answer, "search returned nothing readable", {})

    # kind == "llm" (or sql/rag with Salesforce off, or web with no results):
    # reason over the conversation context (which includes any shared
    # URL/document content).
    answer = await llm.chat_completion(
        [
            {"role": "system", "content": _STEP_LLM_SYSTEM},
            *recent_turns(history, 8),
            {"role": "user", "content": step.input},
        ],
        temperature=0.3,
        max_tokens=5000,
    )
    return answer, _shorten(answer, 80), {}


async def execute_steps(
    plan: AgentPlan,
    history: Sequence[dict],
    emit: Emit,
    salesforce: bool = True,
    effort: str = "medium",
    message: str = "",
) -> List[dict]:
    """Run all steps concurrently (cap 3), emitting step events (§3b)."""
    semaphore = asyncio.Semaphore(STEP_CONCURRENCY)

    async def run(step: PlanStep) -> dict:
        async with semaphore:
            await emit("step", {"id": step.id, "title": step.title, "status": "running"})
            try:
                output, detail, sub_meta = await _run_step_impl(
                    step, history, salesforce, effort, emit, message
                )
            except Exception as exc:
                detail = _shorten(str(exc) or exc.__class__.__name__, 200)
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

    return list(await asyncio.gather(*(run(step) for step in plan.steps)))


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
    }
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
    "user could query it by hand."
)


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
    plan: AgentPlan
    results: List[dict]
    answer: str


async def _plan_node(state: AgentState) -> dict:
    plan = await make_plan(
        state["message"],
        state.get("history", []),
        state.get("salesforce", True),
        state.get("effort", "medium"),
    )
    plan = coerce_allowed(plan, web=state.get("web", True))
    if state.get("web", True) and state.get("web_forced", False):
        plan = ensure_web_step(plan, state["message"])
    return {"plan": plan}


async def _execute_node(state: AgentState) -> dict:
    results = await execute_steps(
        state["plan"],
        state.get("history", []),
        state["emit"],
        state.get("salesforce", True),
        state.get("effort", "medium"),
        state.get("message", ""),
    )
    return {"results": results}


async def _synthesize_node(state: AgentState) -> dict:
    emit = state["emit"]
    results = state.get("results", [])
    # Must happen BEFORE the synthesis prompt is built: the model copies the
    # markers it reads, so they have to be plan-wide by the time it sees them.
    renumber_web_sources(results)
    parts: List[str] = []
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
            parts.append(text)
            await emit("token", {"text": text})

    # §10: the SINGLE final meta, after the token stream, before done.
    await emit("meta", merge_step_meta(results))
    return {"answer": "".join(parts)}


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
) -> str:
    """Entry point used by /chat when agent=true.

    `salesforce` gates Salesforce access: when False the agent plans and runs
    only llm steps over the conversation context (shared URLs/docs) + general
    knowledge — it never queries the warehouse or records.

    `web` gates internet access the same way: when False, web steps become llm
    steps, so a user who turned web search off never gets a network fetch."""
    state = await get_agent_graph().ainvoke(
        {
            "message": message,
            "history": list(history),
            "emit": emit,
            "effort": effort,
            "salesforce": salesforce,
            "web": web,
            "web_forced": web_forced,
        }
    )
    return state.get("answer") or ""
