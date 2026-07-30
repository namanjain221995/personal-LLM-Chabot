# Evidence — orch-engines-core4

Scope: `orchestrator/app/engines/__init__.py`, `router.py`, `orchestrate.py`, `agent.py`, `chat.py`.
Every claim below was read directly with the Read tool. Cross-references into `main.py`, `graph.py`,
`llm.py`, `config.py`, `sql.py`, `search.py`, `live_sf.py`, `rag.py`, `context.py`, `frontend/lib/sse.ts`
were read only in the ranges cited.

Total assigned LOC: **1132** (`__init__.py` 69, `router.py` 133, `orchestrate.py` 167, `agent.py` 658, `chat.py` 105).

---

## Cross-cutting answer: is there an engine ABC?

**No.** `orchestrator/app/engines/__init__.py` contains no `Protocol`, no `ABC`, no base class — only a helper
function and three prompt-fragment constants (`orchestrator/app/engines/__init__.py:6`, `:24`, `:35`, `:57`).
The "engine contract" is **pure duck typing**, re-declared per module as a bare type alias:

- `orchestrator/app/engines/chat.py:22` — `Emit = Callable[[str, dict], Awaitable[None]]`
- `orchestrator/app/engines/agent.py:34` — identical alias, redefined
- `orchestrator/app/graph.py:13` — identical alias, redefined a third time
- `orchestrator/app/engines/search.py:424` — `emit: Optional[Emit]`

There is no runtime check that an engine returns `str`, emits exactly one `meta`, or emits `meta` at all.
The single-`meta`-per-turn rule is enforced only by convention and code comments
(`orchestrator/app/graph.py:32-35`, `orchestrator/app/engines/agent.py:604`, `orchestrator/app/engines/chat.py:103`).

## Cross-cutting answer: EXACT SSE event names emitted from the assigned files

| Event name | Emitted at | Payload keys |
|---|---|---|
| `reasoning` | `orchestrator/app/engines/chat.py:98`, `orchestrator/app/engines/agent.py:599` | `text` |
| `token` | `orchestrator/app/engines/chat.py:101`, `orchestrator/app/engines/agent.py:602` | `text` |
| `meta` | `orchestrator/app/engines/chat.py:104` (`{"route": "chat"}`), `orchestrator/app/engines/agent.py:605` (`merge_step_meta(results)`) | `route`, `steps`, `sql?`, `data?`, `truncated?`, `chart?`, `chart_data?`, `citations?`, `sources?`, `report_files?` |
| `step` | `orchestrator/app/engines/agent.py:382` (running), `:391` (failed), `:400` (done) | `id`, `title`, `status`, `detail?` |

`router.py`, `orchestrate.py` and `__init__.py` emit **nothing** — they have no `emit` parameter at all.

Events reachable *through* the assigned files but emitted elsewhere:
- `research` — `orchestrator/app/engines/search.py:446` (`{"phase":"reading","count"}`) and `:454`
  (`{"phase":"read","count"}`), reached because `agent.py:341-343` forwards `emit` into `research_step`.
- `status` — `orchestrator/app/main.py:415`, emitted by the caller from `orchestrate.describe(plan)`.
- `done` / `error` — `orchestrator/app/main.py:647` / `:672`, emitted by the request worker, never by an engine.

Frontend decoder accepts exactly: `token`, `reasoning`, `status`, `step`, `research`, `meta`, `done`, `error`
(`frontend/lib/sse.ts:129-210`). No unknown event name from these engines would render.

---

### orchestrator/app/engines/__init__.py  (69 LOC)

**Purpose** — Shared engine helpers: a history-window slicer that pins system blocks, plus three global prompt
fragments (no-data message, mermaid rules, code-fence rules). No engine base class lives here.

**Public surface**
- `recent_turns(history: Sequence[dict], n: int) -> List[dict]` — `orchestrator/app/engines/__init__.py:6`
- `NO_DATA_MESSAGE: str` — `orchestrator/app/engines/__init__.py:24`
- `DIAGRAM_INSTRUCTION: str` — `orchestrator/app/engines/__init__.py:35`
- `CODE_INSTRUCTION: str` — `orchestrator/app/engines/__init__.py:57`

**Control flow** (`recent_turns`)
1. Materialize the sequence — `orchestrator/app/engines/__init__.py:16`.
2. Partition into `system` (all roles == "system") and `turns` (everything else) — `:17-18`.
3. Return `system + turns[-n:]` when `n > 0`, else `system` alone — `:19`. Precedence is
   `(system + turns[-n:]) if n > 0 else system`; `n <= 0` therefore drops **all** real turns.

**State & side effects** — none. Pure function; no I/O, no globals mutated, no env reads.

**Dependencies**
- Inbound (verified `rg -n`): `orchestrator/app/engines/chat.py:19`, `orchestrator/app/engines/agent.py:31`,
  `orchestrator/app/engines/sql.py` (`_narrative_messages`, `orchestrator/app/engines/sql.py:280`),
  `orchestrator/app/engines/rag.py`, `orchestrator/app/engines/search.py`,
  `orchestrator/app/engines/repo.py`, `orchestrator/app/engines/document.py`,
  `orchestrator/app/engines/dataset.py`, `orchestrator/app/engines/url.py`,
  and `orchestrator/tests/test_context_budget.py:16`.
- Outbound: `typing` only (`orchestrator/app/engines/__init__.py:3`).

**Config** — none.

**Failure modes** — `m.get(...)` at `:17-18` assumes every history item is a `dict`; a non-dict element raises
`AttributeError` inside every engine that calls it. Nothing is caught here. Unbounded: every system message is
retained regardless of size or count (`:17`, `:19`) — a conversation carrying a recall block, a shared-pages block
and a repo block accumulates all of them; only `context.fit_request`
(`orchestrator/app/context.py:205`) later trims to the window.

**Concurrency** — sync, pure, no shared state.

**Complexity hotspots** — none (largest body is 4 statements).

**Notable** — `DIAGRAM_INSTRUCTION` (`:35-52`, ~17 lines of prompt) and `CODE_INSTRUCTION` (`:57-69`) are
prompt-engineering constants embedded in code, versioned with the source and duplicated into every engine prompt
that concatenates them. `NO_DATA_MESSAGE` (`:24`) hard-codes the operational instruction "it needs the AWS
credentials and region in `.env`" into a user-facing string. No TODO/FIXME/HACK markers.

---

### orchestrator/app/engines/router.py  (133 LOC)

**Purpose** — Classify a user message into one of five engine routes with the small router model, with a
tolerant parser, a main-model fallback and a hard default of `"rag"`.

**Public surface**
- `ROUTES: tuple = ("sql","rag","vision","report","chat")` — `orchestrator/app/engines/router.py:17`
- `_SYSTEM: str` (classification prompt) — `:23-38`
- `FEW_SHOTS: list[tuple[str,str]]` (7 shots) — `:41-49`
- `parse_route(text: object) -> Optional[str]` — `:52`
- `_messages(message: str) -> List[dict]` — `:83`
- `route_request(message: str, has_image: bool = False, history: Sequence[dict] = ()) -> str` — `:92`

Module regexes: `_THINK_RE` `:19`, `_FENCE_RE` `:20`, `_ROUTE_RE` `:21`.

**Control flow** (`route_request`)
1. `has_image` short-circuits to `"vision"` before any model call — `orchestrator/app/engines/router.py:103-104`.
2. Collect prior user turns; if the new message is **≤ 12 words**, prepend the previous user turn truncated to
   **400 chars** as `"(earlier question: …)\nFollow-up: …"` — `:106-110`.
3. Primary call: `llm.router_chat_completion(_messages(message), temperature=0.0, max_tokens=200)` — `:115-117`.
   That helper clips each message to `settings.router_input_char_cap` and forces `enable_thinking: false`
   (`orchestrator/app/llm.py:283-299`).
4. `parse_route(raw)`; return on success — `:118-120`. Any exception is swallowed by a bare
   `except Exception: pass` — `:121-122`.
5. Fallback call: `llm.chat_completion(_messages(message), temperature=0.0, max_tokens=50)` on the **main**
   model — `:126`. Note `chat_completion` forces `enable_thinking: True`
   (`orchestrator/app/llm.py:112`), so a thinking preamble is expected here and `max_tokens=50` may be consumed
   entirely by reasoning.
6. `parse_route`; on success return — `:127-129`. Second bare `except Exception: pass` — `:130-131`.
7. Unconditional default `return "rag"` — `:133`.

**Control flow** (`parse_route`)
1. Non-str / falsy → `None` — `:59-60`.
2. Strip `<think>…</think>` into `t` — `:61`.
3. If a ``` fence exists in `t`, replace `t` with the **first** fence body — `:62-64`.
4. Strict path: slice `t` from first `{` to last `}`, `json.loads`, accept `obj["route"].strip().lower()` when in
   `ROUTES` — `:66-73`. `json.JSONDecodeError`/`ValueError` swallowed — `:74-75`.
5. Lenient path: `_ROUTE_RE.search(text)` — searches the **original, un-stripped** argument, not `t` — `:77`.
6. `None` — `:80`.

**State & side effects** — no DB, no filesystem, no globals. Network egress: two OpenAI-compatible POSTs, to
`settings.router_base_url` and `settings.openai_base_url` respectively, plus the tokenizer/window probes that
`context.fit_request` performs inside both helpers (`orchestrator/app/llm.py:284-291`, `:101-106`). GPU/model
calls: 1–2 completions per routed turn.

**Dependencies**
- Inbound: `orchestrator/app/graph.py:30` (`_router_node`) — the **only** production caller; the `/chat` worker
  reaches it via `get_graph().ainvoke(...)` at `orchestrator/app/main.py:633`. Tests:
  `orchestrator/tests/test_router_parse.py:2`, `orchestrator/tests/test_chat_modes.py:20-21`,
  `orchestrator/tests/test_llm_clients.py:18`, `orchestrator/tests/test_salesforce_toggle.py:272,290`.
- Outbound: `json`, `re`, `typing`, `from .. import llm` (`:15`).

**Config** — no direct env reads. Indirect, via `llm.router_chat_completion`:
`ROUTER_BASE_URL` / `ROUTER_MODEL` (`orchestrator/app/config.py:61`, `:64`),
`ROUTER_INPUT_CHAR_CAP` default 6000 (`orchestrator/app/config.py:138`),
`LLM_REQUEST_TIMEOUT` default 300.0 s (`orchestrator/app/config.py:264`, applied at
`orchestrator/app/llm.py:78`).

**Failure modes**
- Two bare `except Exception: pass` blocks (`:121-122`, `:130-131`) hide connection refused, 400 window errors,
  and timeouts identically — nothing is logged anywhere in this module.
- **No timeout of its own.** The only bound is the client-level 300 s. Worst case a wedged router endpoint plus a
  wedged main endpoint costs **600 s** before `return "rag"` (`:133`).
- No retry, no circuit breaker, no confidence score. `parse_route` returns a route or `None`; there is **no
  threshold, probability, or logprob anywhere in the file** — "confidence" does not exist in this router.
- The terminal fallback is `"rag"` (`:133`), i.e. a total classifier outage silently sends greetings into the
  vector-search engine.

**Concurrency** — `route_request` is `async`, awaits only the two llm helpers; no blocking calls, no module-level
mutable state (all module globals are immutable strings/tuples/compiled regexes).

**Complexity hotspots** — none over 60 LOC. Largest: `route_request` `:92-133` (42 LOC, ~6 branches),
`parse_route` `:52-80` (29 LOC, ~9 branches).

**Notable**
- Magic numbers: `<= 12` words (`:107`), `[:400]` chars (`:108`), `max_tokens=200` (`:116`), `max_tokens=50`
  (`:126`).
- `_THINK_RE` / `_FENCE_RE` are byte-identical to `orchestrator/app/engines/agent.py:51-52` — copy-paste
  duplication of the model-output parsers.
- `parse_route`'s lenient branch reads `text` while the strict branch reads `t` (`:77` vs `:66`) — the `<think>`
  stripping at `:61` is defeated on exactly the path where it matters. See finding **F2**.
- `_SYSTEM`'s route menu (`:23-38`) lists 5 classes, but `graph.build_graph` also wires exactly 5 nodes
  (`orchestrator/app/graph.py:100-104`), so the sets match. There is no default branch in the conditional edge
  map — a route outside `ROUTES` would be a LangGraph KeyError, which `parse_route` prevents by construction.
- No TODO/FIXME/HACK markers.

---

### orchestrator/app/engines/orchestrate.py  (167 LOC)

**Purpose** — One cheap non-thinking classification call decides whether a turn deserves agent planning and/or
web search; the effort level is a hard ceiling the classifier can only narrow.

**Public surface**
- `Plan` dataclass — fields `agent: bool`, `search: bool`, `auto: bool = True` — `orchestrator/app/engines/orchestrate.py:77-81`
- `parse_plan(raw: str) -> Plan` — `:98`
- `ALLOWED: dict[str, dict[str, bool]]` — `:114-119` (`fast` = no/no, `low` = no/yes, `medium` = yes/yes, `high` = yes/yes)
- `allowances(effort: str) -> dict` — `:122`
- `decide(message: str, history: Sequence[dict], effort: str) -> Plan` — `:127`
- `describe(plan: Plan) -> str` — `:159`
- `_SYSTEM` `:40-55`, `_FEW_SHOTS` `:57-71`, `_INPUT_CAP = 2000` `:74`, `_JSON_RE` `:38`, `_messages` `:84`

**Control flow** (`decide`)
1. `allowed = allowances(effort)`; unknown effort silently maps to `medium` — `:134`, `:124`.
2. Short-circuit `Plan(False, False)` when the level permits nothing **or** the message is blank — `:135-136`.
3. `llm.router_chat_completion(_messages(message, history), temperature=0.0, max_tokens=40)` — `:138-140`.
   `_messages` builds system + 6 few-shots + the last 2 **non-system** history turns + the message truncated to
   2000 chars (`:84-95`).
4. `parse_plan(raw)` — `:141`. Any exception → `Plan(False, False)` (`:142-144`), bare `except Exception`.
5. Intersect with the ceiling: `agent = plan.agent and allowed["agent"]`, `search = plan.search and
   allowed["search"]` — `:145-146`.
6. High-only escalation: `if effort == "high" and search: agent = True` — `:154-155`.
7. Return `Plan(agent, search)` — `:156`.

**Control flow** (`parse_plan`)
1. `_JSON_RE.search(raw or "")`; no match → `Plan(False, False)` — `:101-102`.
2. `json.loads(match.group(0))`; bare `except Exception` → `Plan(False, False)` — `:103-106`.
3. Fields accepted only on **literal `True`** (`data.get("agent") is True`) — `:107-110`; `"true"`, `1`, `"yes"`
   all read as False.

**State & side effects** — no DB, no filesystem, no globals mutated. Network egress: one POST to
`settings.router_base_url` per qualifying turn (`orchestrator/app/llm.py:283`) plus that helper's
tokenizer/window probes. GPU/model calls: exactly one, thinking disabled (`orchestrator/app/llm.py:298`).

**Dependencies**
- Inbound: `orchestrator/app/main.py:410` imports `decide, describe` inside the request worker and calls
  `decide(request.text, history, request.effort)` at `orchestrator/app/main.py:412`, gated by
  `request.text and not request.pdf_data and not request.image_data and not request.agent`
  (`orchestrator/app/main.py:404-409`). `describe`'s label is emitted as a `status` event at
  `orchestrator/app/main.py:415`. Tests: `orchestrator/tests/test_orchestrate.py:16`,
  `orchestrator/tests/test_salesforce_toggle.py:38,57,86,124`.
- Outbound: `json`, `re`, `dataclasses`, `typing`, `from .. import llm` (`:35`), `from ..config import settings`
  (`:36` — **never used in this file**, see Notable).

**Config** — no direct env reads. Indirect: `ROUTER_BASE_URL`/`ROUTER_MODEL`
(`orchestrator/app/config.py:61,64`), `ROUTER_INPUT_CHAR_CAP` (`orchestrator/app/config.py:138`),
`LLM_REQUEST_TIMEOUT` (`orchestrator/app/config.py:264`). The consumer-side gate `SEARCH_ENABLED`
(`orchestrator/app/config.py:192`) is applied by the caller at `orchestrator/app/main.py:425`, not here.

**Failure modes**
- Two swallow-everything handlers: `:104-106` and `:142-144`. A 400 from a too-long prompt and a dead endpoint
  produce the same silent "do neither".
- **No timeout.** Only the 300 s client timeout applies, and this call sits on the critical path of *every*
  text turn at effort ≥ low, before any `status` event is emitted (`orchestrator/app/main.py:412` precedes `:415`).
  See finding **F1**.
- No retry. No logging.
- `_JSON_RE = re.compile(r"\{.*\}", re.S)` (`:38`) is **greedy**: with two JSON objects in the output it spans
  from the first `{` to the last `}` and `json.loads` fails → silent downgrade. Unlike `router.py` and
  `agent.py`, this module does **not** strip `<think>` blocks or code fences.

**Concurrency** — `decide` is `async` with a single await; everything else is sync and pure. `ALLOWED` (`:114`)
is module-level mutable and `allowances` returns the **inner dict by reference** (`:124`) — a caller that mutated
the result would permanently rewrite global policy for all requests. No current caller mutates it.

**Complexity hotspots** — none over 60 LOC. Largest: `decide` `:127-156` (30 LOC).

**Notable**
- Dead import: `settings` at `:36` is never referenced (`rg -n 'settings' orchestrator/app/engines/orchestrate.py`
  returns only line 36).
- Dead field: `Plan.auto` (`:81`) is never set to `False` and never read anywhere in `orchestrator/` or
  `frontend/` (`rg -n 'auto='` and `rg -n '\.auto\b'` over `orchestrator/app` return no hits). The docstring at
  `:20-21` describes behaviour ("an explicit user choice always wins") that is actually implemented in
  `orchestrator/app/main.py:408` (`not request.agent`) — never through this field.
- `ALLOWED["high"]` is byte-identical to `ALLOWED["medium"]` (`:117-118`); the only real difference is the
  `effort == "high"` escalation at `:154`.
- `allowances`'s "unknown → medium" branch is unreachable over HTTP: `ChatRequest.effort` is
  `Literal["fast","low","medium","high"]` (`orchestrator/app/main.py:193`).
- Magic numbers: `_INPUT_CAP = 2000` (`:74`), `max_tokens=40` (`:139`), `list(history)[-2:]` (`:93`).
- No TODO/FIXME/HACK markers.

---

### orchestrator/app/engines/agent.py  (658 LOC)

**Purpose** — Deep-task engine: a LangGraph subgraph PLAN → EXECUTE → SYNTHESIZE that turns a request into ≤8
pydantic-validated steps, runs them through the existing sql/rag/live-Salesforce/web engines with concurrency 3,
and streams one merged answer plus one merged `meta`.

**Public surface**
- `MAX_STEPS = 8` — `orchestrator/app/engines/agent.py:36`
- `STEP_CONCURRENCY = 3` — `:37`
- `_STEP_BUDGET = {"medium": 5, "high": MAX_STEPS}` — `:42`; `_SYNTH_TOKENS = {"medium": 6000, "high": 12000}` — `:45`
- `step_budget(effort: str) -> int` — `:48`
- `class PlanStep(BaseModel)` — `id: int`, `title: str(1..200)`, `kind: Literal["sql","rag","llm","web","salesforce"]`, `input: str(min 1)` — `:59-63`
- `class AgentPlan(BaseModel)` — `steps: List[PlanStep]` with `min_length=1, max_length=MAX_STEPS` and a unique-id validator — `:66-75`
- `parse_agent_plan(raw: object) -> AgentPlan` (raises `ValueError`) — `:78`
- `_PLAN_SYSTEM` `:108-127`, `_PLAN_SYSTEM_NO_SF` `:131-142`
- `_fallback_plan(message) -> AgentPlan` — `:145`
- `_coerce_no_salesforce(plan) -> AgentPlan` — `:151`
- `coerce_allowed(plan, *, web: bool) -> AgentPlan` — `:164`
- `make_plan(message, history, salesforce=True, effort="medium") -> AgentPlan` — `:178`
- `_STEP_LLM_SYSTEM` `:222`; `_shorten(text, limit=120)` `:228`
- `_run_step_impl(step, history, salesforce, effort="medium", emit=None, message="") -> Tuple[str,str,dict]` — `:233`
- `execute_steps(plan, history, emit, salesforce=True, effort="medium", message="") -> List[dict]` — `:369`
- `renumber_web_sources(results) -> None` (mutates in place) — `:411`
- `_SQL_PAYLOAD_KEYS = ("sql","data","truncated","chart","chart_data")` — `:464`
- `merge_step_meta(results) -> dict` — `:467`
- `_SYNTH_SYSTEM` `:525-532`; `_synthesis_messages(message, results)` `:535`
- `class AgentState(TypedDict, total=False)` — `:549-558`
- `_plan_node` `:561`, `_execute_node` `:571`, `_synthesize_node` `:583`
- `build_agent_graph()` `:609`, `get_agent_graph()` `:624`, module global `_compiled = None` `:621`
- `run_agent_engine(message, history, emit, *, effort="medium", salesforce=True, web=True) -> str` — `:631`

**Control flow**
1. `run_agent_engine` builds the initial `AgentState` and calls `get_agent_graph().ainvoke(...)` —
   `orchestrator/app/engines/agent.py:648-657`; returns `state.get("answer") or ""` — `:658`.
2. `get_agent_graph` lazily compiles and caches the graph in the module global `_compiled` — `:624-628`.
   Graph edges: entry `plan` → `execute` → `synthesize` → `END` — `:614-617`.
3. **PLAN** (`_plan_node` `:561-568`) calls `make_plan(...)` then `coerce_allowed(plan, web=state.get("web", True))`.
4. `make_plan` picks `_PLAN_SYSTEM` or `_PLAN_SYSTEM_NO_SF` on the `salesforce` flag — `:188` — then rewrites
   `"at most 8 "` to `"at most {step_budget(effort)} "` — `:190`.
5. Prompt = `llm.apply_reasoning_effort([system], "high")` (a documented no-op passthrough,
   `orchestrator/app/llm.py:198-209`) + `recent_turns(history, 6)` + the user message — `:191-195`.
6. Loop `for _attempt in range(2)` — `:197`. On the retry the previous error text (`[:400]`) is appended as an
   extra user turn — `:200-209`. Call: `llm.chat_completion(prompt, temperature=0.1, max_tokens=6000)` — `:210`.
7. `parse_agent_plan(raw)` — `:211`; on success return `plan` or `_coerce_no_salesforce(plan)` — `:212`.
   `except Exception as exc: last_error = str(exc)[:400]` — `:213-214` (bare, no logging).
8. After 2 failures: `_fallback_plan(message)` — a single `kind="llm"` step titled `message[:60]` — `:215`, `:145-148`.
9. **EXECUTE** (`_execute_node` `:571-580`) calls `execute_steps(plan, history, emit, salesforce, effort, message)`.
   Note `web` is **not** forwarded.
10. `execute_steps` creates a per-call `asyncio.Semaphore(3)` — `:378` — and `asyncio.gather`s one `run()` per
    step — `:405`. `gather` preserves plan order in the result list.
11. `run(step)` acquires the semaphore, emits `step` `status="running"` — `:381-382` — awaits `_run_step_impl`,
    then emits `status="done"` with `detail` — `:399-402` — returning
    `{"step","status","output","meta"}` — `:403`.
12. On any `Exception` in the step: `detail = _shorten(str(exc) or exc.__class__.__name__, 200)`, emit
    `status="failed"` with that detail, and return `output = f"Step failed: {detail}"` — `:387-398`.
    The plan continues; other steps are unaffected.
13. `_run_step_impl` dispatch, in source order:
    - **sql** (`:252-274`, requires `salesforce`): imports `settings`, `cap_rows`, and
      `sql.attach_chart`/`sql.generate_and_run_sql` lazily; `await generate_and_run_sql(step.input,
      history=list(history))` (`:257`); builds `sample` from `rows[:30]` (`:258-260`);
      `preview, truncated = cap_rows(rows, settings.sql_preview_row_cap)` (`:261`); `sub_meta` gets
      `sql`/`data`/`truncated` (`:262-266`); `await attach_chart(sub_meta, f"{message}\n{step.input}", columns,
      preview)` (`:269`); returns `f"SQL result ({len(rows)} row(s)):\n{sample}"` (`:270-274`).
    - **rag** (`:276-286`, requires `salesforce`): `select_context(step.input)` (`:281`) →
      `llm.chat_completion(_answer_messages(step.input, hits, []), temperature=0.2, max_tokens=5000)` (`:282-284`)
      → `build_citations(hits, base_url=settings.sf_lightning_base_url)` (`:285`).
    - **salesforce** (`:288-310`, requires `salesforce`): `fetch_live(step.input, history)` (`:293`); on
      `SalesforceUnavailable`/`UnsafeSoql` returns a *prose* degradation string and an empty sub-meta
      (`:294-303`) — the step still reports `status="done"`; otherwise `merge_rows([], live_rows)` and
      `describe_rows(rows)` (30-row cap, `orchestrator/app/engines/live_sf.py:148`) with
      `sub_meta = {"sql": soql, "data": rows[:50]}` (`:304-310`).
    - **salesforce, second copy** (`:312-335`): byte-equivalent duplicate of the block above, guarded by the
      identical condition. **Unreachable dead code.**
    - **web** (`:337-352`): `research_step(step.input, list(history), effort, emit)` (`:341-343`). With sources →
      returns `(answer, "<n> source(s): <domains>", {"sources": sources})` (`:344-350`). With **no** sources the
      branch **falls through** (`:351-352`, no `return`) into the llm tail.
    - **llm tail** (`:354-366`): `llm.chat_completion([_STEP_LLM_SYSTEM, *recent_turns(history, 8), step.input],
      temperature=0.3, max_tokens=5000)`; returns `(answer, _shorten(answer, 80), {})`.
14. **SYNTHESIZE** (`_synthesize_node` `:583-606`): `renumber_web_sources(results)` mutates step outputs and
    sub-metas **before** the prompt is built (`:588`), then
    `llm.stream_chat_events(_synthesis_messages(...), model_choice="smart", effort=..., temperature=0.2,
    max_tokens=_SYNTH_TOKENS.get(effort, 6000))` (`:591-597`), emitting `reasoning` (`:599`) and `token` (`:602`).
15. Final single `meta` = `merge_step_meta(results)` — `:605` — then `{"answer": "".join(parts)}` — `:606`.
16. `merge_step_meta` (`:467-518`): always sets `route: "agent"` and `steps` (`:473-479`); the **last** step whose
    sub-meta has a truthy `sql` contributes the whole `_SQL_PAYLOAD_KEYS` tuple atomically (`:489-493`);
    citations dedupe on `record_id` (`:494-498`); report files on `filename` (`:499-503`); sources on `url`
    (`:506-510`); optional keys added only when non-empty (`:512-517`).

**State & side effects**
- **Filesystem / DB (indirect):** the `sql` step reads DuckDB via `generate_and_run_sql` →
  `_execute` (`orchestrator/app/engines/sql.py:179-206`); the `rag` step reads the vector store via
  `select_context` (`orchestrator/app/engines/rag.py:91`).
- **Network egress:**
  - main model at `OPENAI_BASE_URL` — plan (`:210`), rag answer (`:284`), step-llm answer (`:357`), synthesis
    stream (`:591`);
  - live Salesforce org over the API via `fetch_live` (`:293`, `orchestrator/app/engines/live_sf.py:140-145`);
  - **the public internet** via `research_step` (`:341`) — search backend queries plus arbitrary page fetches
    chosen by the model.
- **GPU/model calls per agent turn:** 1–2 (plan) + 1 per llm/rag/web step + 1–2 per sql step (SQL generation and
  the chart-spec model, `orchestrator/app/engines/sql.py:210-212`) + 1 streaming synthesis. With 8 steps this is
  ~11–19 model calls for a single user message.
- **Global mutation:** `_compiled` (`:621`, written at `:627`). `renumber_web_sources` mutates the caller's
  `results` list, each `r["meta"]` dict and each `r["output"]` string in place (`:441-452`).
  `_coerce_no_salesforce` (`:159-160`) and `coerce_allowed` (`:172-174`) mutate `PlanStep.kind` in place.
- **Env reads:** none direct; `settings.sql_preview_row_cap` (`:261`) and `settings.sf_lightning_base_url` (`:285`).

**Dependencies**
- Inbound (verified `rg -n`): `orchestrator/app/main.py:592` imports `run_agent_engine` and calls it at
  `orchestrator/app/main.py:594-601` with `salesforce=(request.mode != "assistant")` and `web=want_search`.
  Tests: `orchestrator/tests/test_agent.py:18-21`, `test_agent_web_step.py:14`,
  `test_agent_salesforce_gate.py:5-6`, `test_chart_routes.py:21`, `test_live_salesforce.py:166-233`,
  `test_effort_depth.py:82-114`, `test_salesforce_toggle.py:65,243,256`.
- Outbound: `asyncio`, `json`, `re`, `typing`, `langgraph.graph` (`:28`), `pydantic` (`:29`),
  `. (recent_turns, CODE_INSTRUCTION, DIAGRAM_INSTRUCTION)` (`:31`), `.. llm` (`:32`), and lazy in-function
  imports of `..config.settings`, `..core.exports.cap_rows`, `.sql`, `..core.citations`, `.rag`,
  `..core.salesforce`, `.live_sf`, `.search` (`:253-255`, `:277-279`, `:289-291`, `:315-316`, `:338`).

**Config** — `SQL_PREVIEW_ROW_CAP` (default 500, `orchestrator/app/config.py:234`) read at `:261`;
`SF_LIGHTNING_BASE_URL` (`orchestrator/app/config.py:103`) read at `:285`. Everything else arrives through
`llm.*` (`LLM_REQUEST_TIMEOUT` `orchestrator/app/config.py:264`) or through the sub-engines
(`SEARCH_ENABLED` `:192`, `FETCH_TIMEOUT_MS` `:198`, `SF_LIVE_TIMEOUT` `:123`).

**Failure modes**
- `make_plan`'s loop catches **every** `Exception` including timeouts and 400s (`:213-214`), logs nothing, and
  after two attempts degrades to a one-step plan (`:215`).
- `execute_steps.run` catches every `Exception` per step (`:387`); the plan always "succeeds" as a whole. The raw
  `str(exc)` is truncated to 200 chars and **shipped to the browser** as `step.detail` (`:388-392`).
- `asyncio.gather` at `:405` is called **without** `return_exceptions=True`; a `BaseException` escaping `run`
  (e.g. `asyncio.CancelledError` on Stop) aborts the gather and leaves the sibling coroutines running.
- **No timeout anywhere in this file** — `rg -n 'wait_for|timeout' orchestrator/app/engines/*.py` returns only
  `url.py:36` and `search.py:315/338`. The only bound is the 300 s OpenAI client timeout
  (`orchestrator/app/llm.py:78`). Worst-case wall clock for one agent turn: plan 2×300 s + ⌈8/3⌉=3 waves ×
  (per-step model + fetch time) + synthesis — well past 20 minutes with no cancellation.
- **No overall iteration loop.** There is exactly one PLAN → EXECUTE → SYNTHESIZE pass; the graph has no cycle
  (`:614-617`) and no step can enqueue another. It therefore **cannot** run unbounded in step count; the bound is
  `MAX_STEPS = 8` enforced by pydantic (`:67`) *and* by the prompt (`:190`).
- Web-step degradation is silent: no sources → an LLM answer marked `status="done"` (`:351-352` → `:399-402`),
  with no `search_unavailable` flag. Compare `orchestrator/app/engines/search.py:416`, where the direct route
  sets `meta.search_unavailable = True`.
- Live-Salesforce degradation is also reported as `status="done"` (`:294-303`).
- `renumber_web_sources` skips sources without a `url` (`:435-437`), leaving their local `[n]` markers unmapped
  and pointing at whichever page ends up with that plan-wide number.

**Concurrency**
- Fully async. No blocking (sync) call inside an `async def` in this file — the DuckDB execution and the HTTP
  fetches happen inside the sub-engines.
- `execute_steps` runs up to 3 steps at once (`:378`, `:381`) sharing a single `emit` callable; `research`
  events from concurrent web steps interleave into one frontend research panel
  (`frontend/lib/sse.ts:166-199` merges them by phase/count with no step id).
- `_compiled` (`:621`) is a lazily-initialised module global with no lock (`:624-628`); benign under a single
  event loop, a double-compile race under threads.
- `renumber_web_sources` mutates shared `results` after all steps have joined — no race.
- Race window: `execute_steps` receives the same `history` list object for every concurrent step (`:384`) and
  each step copies it (`list(history)` at `:257`, `:341`) or slices it (`recent_turns` at `:360`); nothing
  writes to it.

**Complexity hotspots**
- `_run_step_impl` — `orchestrator/app/engines/agent.py:233-366`, **134 LOC**, 5 dispatch branches + 2
  try/except + a fall-through: the largest function in the assigned set and the largest in the repo's engine
  layer for this slice.
- `merge_step_meta` — `:467-518`, **52 LOC**, 4 nested dedupe loops (cyclomatic ≈ 12).
- `renumber_web_sources` — `:411-452`, **42 LOC**, nested loops plus a regex-callback closure.
- `execute_steps` — `:369-405`, **37 LOC**, contains a nested async closure.
- `make_plan` — `:178-215`, **38 LOC**.

**Notable**
- **Dead code:** the entire second `if step.kind == "salesforce" and salesforce:` block, `:312-335` (24 LOC), is
  unreachable — the identical condition at `:288` always returns first. Confirmed with
  `rg -n 'step.kind == "salesforce"' orchestrator/app/engines/agent.py` → lines 288 and 312.
- **Duplication:** `_THINK_RE`/`_FENCE_RE` (`:51-52`) duplicate `orchestrator/app/engines/router.py:19-20`;
  `parse_agent_plan`'s fence/think/brace-slice logic (`:86-97`) duplicates `parse_route` (`router.py:61-73`).
- **Divergence from the direct SQL route:** the agent's sql step tells the synthesizer
  `f"SQL result ({len(rows)} row(s)):\n{sample}"` with `sample = rows[:30]` (`:258-274`) and **no** statement
  that this is a sample. `orchestrator/app/engines/sql.py:267-278` carries an explicit
  "You are shown only the FIRST FEW ROWS… quote the true row count" instruction — added, per its own comment,
  because "314 rows came back and the summary said 29 records". See finding **F3**.
- Magic numbers: `rows[:30]` (`:259`), `rows[:50]` (`:309`, `:334`), `recent_turns(history, 6)` (`:193`) vs
  `recent_turns(history, 8)` (`:360`), `_shorten` limits 120/200/80/60 (`:228`, `:388`, `:366`, `:347`),
  `max_tokens` 6000/5000/5000 (`:210`, `:283`, `:364`), `str(exc)[:400]` (`:214`).
- Prompt fragility: `make_plan` rewrites the system prompt by **string substitution** on the literal
  `f"at most {MAX_STEPS} "` (`:190`); both prompt constants must keep that exact phrasing
  (`:110`, `:138`) or the budget silently stops applying.
- `_STEP_BUDGET`/`_SYNTH_TOKENS` have no `"fast"`/`"low"` keys (`:42`, `:45`); an explicit `agent=true` at
  `effort="fast"` falls back to the medium values (`:49`, `:596`).
- `_run_step_impl` accepts `emit: Optional[Emit] = None` (`:238`) but `execute_steps` always passes a real one
  (`:385`); only the web branch uses it (`:342`).
- No TODO/FIXME/HACK markers anywhere in the file (`rg -n 'TODO|FIXME|HACK|XXX'` → no hits).

---

### orchestrator/app/engines/chat.py  (105 LOC)

**Purpose** — Plain streamed completion with no data engines: assistant mode (router bypassed entirely) and the
Salesforce-mode `"chat"` router class (greetings/small talk).

**Public surface**
- `Emit` type alias — `orchestrator/app/engines/chat.py:22`
- `ASSISTANT_SYSTEM: str` — `:24-31`
- `SALESFORCE_CHAT_SYSTEM: str` — `:33-48`
- `_messages(message, history, mode) -> List[dict]` — `:51`
- `run_chat_engine(message, history, emit, *, mode="salesforce", model_choice="smart", effort="medium") -> str` — `:66`

**Control flow**
1. `max_tokens = 8000 if mode == "assistant" else 6000` — `orchestrator/app/engines/chat.py:80`.
2. `if effort == "high" and mode == "assistant": max_tokens = 16000` — `:84-85`.
3. `temperature = 0.3 if effort in ("medium","high") else 0.6` — `:88`.
4. `_messages` picks `ASSISTANT_SYSTEM + DIAGRAM_INSTRUCTION + CODE_INSTRUCTION` for assistant mode, or
   `SALESFORCE_CHAT_SYSTEM` alone otherwise — `:54-58` — and builds
   `[system] + recent_turns(history, 6) + [user]` — `:59-63`.
5. `async for kind, text in llm.stream_chat_events(..., model_choice, effort, temperature, max_tokens)` — `:90-96`.
6. `kind == "reasoning"` → `emit("reasoning", {"text": text})` — `:97-98`; otherwise accumulate and
   `emit("token", {"text": text})` — `:99-101`.
7. `emit("meta", {"route": "chat"})` after the stream — `:104`; return the joined answer — `:105`.

**State & side effects** — no DB, no filesystem, no globals. Network egress: one streaming POST to
`llm.resolve_model_choice(model_choice)[0]` — `OPENAI_BASE_URL` for `"smart"`, `ROUTER_BASE_URL` for `"fast"`
(`orchestrator/app/llm.py:158-166`) — plus `context.fit_request`'s window/tokenize probes
(`orchestrator/app/llm.py:229-234`). Thinking is on only for `model_choice=="smart"` and effort in
`("medium","high")` (`orchestrator/app/llm.py:174-185`, applied at `:242`).

**Dependencies**
- Inbound: `orchestrator/app/main.py:622` (assistant mode, called at `:624-631`) and
  `orchestrator/app/graph.py:77` (`_chat_node`, called at `:79-86` with `mode="salesforce"`). Tests:
  `orchestrator/tests/test_chat_modes.py`, `orchestrator/tests/test_salesforce_toggle.py:308`.
- Outbound: `typing`; `from . import CODE_INSTRUCTION, DIAGRAM_INSTRUCTION, recent_turns` (`:19`);
  `from .. import llm` (`:20`).

**Config** — no direct env reads. Indirect via `llm.resolve_model_choice`: `OPENAI_BASE_URL`/`MAIN_MODEL`,
`ROUTER_BASE_URL`/`ROUTER_MODEL` (`orchestrator/app/config.py:61,64`), `LLM_REQUEST_TIMEOUT`
(`orchestrator/app/config.py:264`).

**Failure modes**
- **Nothing is caught.** Any exception from `stream_chat_events` (connection refused, 400 window error, read
  timeout) propagates to `orchestrator/app/main.py:670-672`, which publishes `error` with the raw `str(exc)`.
- If the stream raises **after** tokens were emitted, `meta` at `:104` never fires — the client keeps a partial
  answer with no `meta`, then receives `error`.
- No timeout beyond the 300 s client timeout; no retry; no bound on `parts` (`:89`) other than `max_tokens`.
- `mode` and `model_choice` are plain `str` parameters with no validation (`:71-73`); any value other than
  `"assistant"` silently takes the Salesforce-chat prompt (`:56-58`) and any value other than `"fast"` resolves
  to the smart model (`orchestrator/app/llm.py:164-166`). HTTP callers are constrained by
  `orchestrator/app/main.py:191-193`.

**Concurrency** — async, single streaming await loop, no shared mutable state, no blocking call.

**Complexity hotspots** — none. `run_chat_engine` is `:66-105` (40 LOC, 4 branches); `_messages` is `:51-63`.

**Notable**
- Magic numbers: 8000 / 6000 / 16000 token ceilings (`:80`, `:85`), temperatures 0.3 / 0.6 (`:88`),
  `recent_turns(history, 6)` (`:61`).
- `SALESFORCE_CHAT_SYSTEM` (`:33-48`) contains an inline post-mortem comment (`:39-42`) documenting a previous
  hallucination ("I don't have direct access to your live Salesforce org"); the fix is prompt text only, with no
  output check.
- Asymmetry: `DIAGRAM_INSTRUCTION`/`CODE_INSTRUCTION` are attached only in assistant mode (`:54-58`), so the
  Salesforce chat class can emit a mermaid block the UI renders without the safety rules ever having been sent.
- No TODO/FIXME/HACK markers.

---

## Measured metrics for this slice

- Total LOC read: 1132.
- Largest function: `orchestrator/app/engines/agent.py:233` `_run_step_impl` — **134 LOC**.
- Runner-up: `orchestrator/app/engines/agent.py:467` `merge_step_meta` — 52 LOC.
- TODO/FIXME/HACK markers in the assigned files: **none**
  (`rg -n 'TODO|FIXME|HACK|XXX'` over all five files returned no hits).
- Bare `except Exception` / swallow sites in the assigned files: `router.py:121`, `router.py:130`,
  `router.py:74`, `orchestrate.py:105`, `orchestrate.py:142`, `agent.py:95`, `agent.py:99`, `agent.py:213`,
  `agent.py:294`, `agent.py:320`, `agent.py:387`. Eleven total; **zero** of them log.
- `asyncio.wait_for` / explicit per-call timeout in the assigned files: **zero**.

## Findings index (detail in the returned JSON)

- **F1** `orchestrate.py:138` — the pre-flight classification call has no timeout and sits ahead of the first
  `status` event on every turn.
- **F2** `router.py:77` — the lenient route regex scans the un-stripped text, defeating the `<think>` strip at `:61`.
- **F3** `agent.py:258-274` — agent sql steps hand the synthesizer a 30-row sample labelled as the full result.
- **F4** `agent.py:351-352` — a web step with no sources silently becomes a model-knowledge answer reported `done`.
- **F5** `agent.py:388-392` — raw exception text is streamed to the browser as `step.detail`.
- **F6** `agent.py:312-335` — 24 lines of unreachable duplicate Salesforce branch.
- **F7** `agent.py:571-580` / `:337` — the `web` gate is enforced only at plan time; `execute_steps` never sees it.
- **F8** `router.py:114-133` — a wedged router endpoint costs 600 s before the `"rag"` default.
- **F9** `orchestrate.py:38` — greedy `\{.*\}` and no `<think>`/fence stripping in the plan parser.
- **F10** `agent.py:405` — `asyncio.gather` without `return_exceptions` leaves siblings running on cancellation.
- **F11** `orchestrate.py:36`, `:81` — dead import and dead `Plan.auto` field.
- **F12** `chat.py:54-58` — diagram/code safety rules omitted from the Salesforce chat prompt.
