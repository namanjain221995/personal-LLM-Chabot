# Salesforce Intelligence Mode

*Added 2026-08-11.*

The Salesforce pill used to be a retrieval filter: "answer from my data" versus
"answer from the model". It is now a context-aware agent that resolves a request
against the conversation before it queries anything, asks **one** targeted
question when a missing detail would materially change the answer, and resumes
the **original** request once that question is answered.

Everything here is behind feature flags with the previous behaviour intact
underneath them — see [Configuration](#configuration).

---

## Why

The engine's failure mode was never refusal. It was picking one reading of an
ambiguous question and reporting the number with full confidence. Asked *"how
many candidates completed the training from slot 128 and how many failed the
mock"*, three consecutive runs scoped the mock three different ways and returned
**7**, **20** and **0**. Every one of those is a defensible reading of the
English. Only one is what the asker meant, and nothing in the pipeline could
know which.

`app/core/clarify.py` addressed that with deterministic regex detectors. They
work, they are still here, and they are still the fallback — but they cannot do
the two things that actually make an assistant feel like it is listening:

* **resolve a reference.** "What about EMEA?" means nothing to a regex.
* **resume.** Picking an option sent a *rewritten question* as a brand-new
  message, and the server had to reconstruct what was being clarified from a
  transcript that does not always contain the assistant turn.

## The shape of a request

```
                      ┌─ conversational? ──────────────► hand back, emit nothing
                      │  ("hi", "thanks") — no planner call, no status events
  POST /chat          │
  mode=salesforce ────┤
                      │  pending question?  ──► resume.classify
                      │                         answers it   → merge slot, resume intent
                      │                         new topic    → cancel, start fresh
                      ▼
        state.load_state          what this conversation established
        state.new_intent          seeded with the carried slots
        tools.get_salesforce_schema
        tools.search_salesforce_entities     ("which Acme?")
                      ▼
        planner.plan  ──►  AgentDecision (validated; one repair; then the
                           deterministic detectors as a floor)
        planner.enforce_policy   round budget, repeated-question guard
                      ▼
   ┌──────────────────┼──────────────────┬─────────────────┐
   ▼                  ▼                  ▼                 ▼
ASK_CLARIFICATION  EXECUTE          ANSWER_GENERAL   DENY / UNSUPPORTED
persist + stream   compile → run    hand back to     say so plainly
the card           → paginate →     the chat engine
                   calculate →
                   verify → answer
```

## Modules

| File | Responsibility |
|---|---|
| `app/core/sf_intel/models.py` | Every typed contract. Nothing acts on model output that has not been through here. |
| `app/core/sf_intel/prompts.py` | The planner prompt and the answer prompt, kept apart. |
| `app/core/sf_intel/planner.py` | One validated `AgentDecision`, plus the policy that downgrades a question we must not ask. |
| `app/core/sf_intel/plan.py` | `SalesforceQueryPlan` → SOQL. **The security boundary.** |
| `app/core/sf_intel/tools.py` | Schema, entity search, plan execution, pagination, deterministic calculation. |
| `app/core/sf_intel/state.py` | Pending intents and per-conversation Salesforce state. |
| `app/core/sf_intel/resume.py` | "Is this message the answer, or a new subject?" |
| `app/core/sf_intel/phases.py` | The progress labels the UI is allowed to show. |
| `app/core/sf_intel/budget.py` | Priority-ordered context assembly for a 262K window. |
| `app/engines/sf_intel.py` | The engine that runs the pipeline above. |

Frontend: `lib/clarification.ts` (the contract mirror + all decision logic),
`lib/phases.ts`, `lib/salesforceApi.ts`, `components/ClarificationCard.tsx`,
`components/SalesforceStarterCard.tsx`, `components/ReasoningStar.tsx`,
`components/SalesforceSourceLine.tsx`.

---

## The clarification lifecycle

1. The planner proposes a `ClarificationDraft`.
2. `state.open_clarification` mints ids and an opaque `resume_token`, and
   **persists** the question. It returns `None` — meaning *answer with a stated
   assumption instead* — when:
   * the same question has already been asked in this intent
     (`question_fingerprint`, which is semantic: rewording does not evade it),
   * the round budget is spent, or
   * another request won the race to ask.
3. The card streams as `meta.clarification` and the run ends.
4. The client answers with a `ClarificationResponse` on the next `POST /chat`.
5. `state.apply_response` resolves it **idempotently**, merges the answer into
   `resolved_slots`, and the SAME intent continues.

### The two invariants that live in PostgreSQL

Both must survive two requests racing, so neither is enforced in Python:

```sql
-- one open question per conversation
CREATE UNIQUE INDEX idx_sf_clarifications_one_pending
    ON sf_clarifications (conversation_id) WHERE state = 'pending';

-- the first response wins
UPDATE sf_clarifications SET state = ... WHERE clarification_id = %s AND state = 'pending'
```

A double-clicked option, a fetch retried after a timeout, and a reconnect all
match zero rows the second time and receive the **first** answer back.

### Surviving a reload

Nothing about a pending card lives in the generation buffer, which is
per-process and dies with the answer it was streaming. The card comes back
because:

* the message carries `meta.clarification` through server history, and
* `GET /chat/salesforce/{conversation_id}` returns the pending question from the
  database on mount.

### When the source is switched off

The card disappearing is not what ends the question. `POST
/chat/salesforce/cancel` (and any `mode=assistant` send) cancels it server-side,
so the next Salesforce message in that chat is not read as an answer to
something the user visibly dismissed. Composer text is never cleared.

---

## Query safety

`engines/live_sf.py` lets the model write SOQL and guards the string afterwards.
That is defensible behind a read-only integration user, but it leaves the model
in control of the query's *shape*. This path never does.

The model supplies an object name, some field names, an operator **from an
allowlist**, and a value. Every character of syntax — clause order, quoting,
escaping, the `LIMIT` — is written by `plan.py`. Before one character exists:

* the object exists and is queryable **by this connection** (from `describe`,
  not a hardcoded list, so an object the integration user cannot see never
  appears);
* every field exists on that object and is readable;
* every relationship traversal is a real parent path, bounded in depth;
* the operator's operand shape matches, and the operand matches the field's real
  Salesforce type;
* aggregates combine with `GROUP BY` only in ways SOQL accepts;
* date operands are real Salesforce date literals or ISO dates — an
  unrecognised one is **refused**, because quoting it as a string would match
  nothing, silently;
* `LIKE` wildcards are ours: `%` and `_` in user text are escaped, so "50% off"
  is matched literally;
* the `LIMIT` is capped whatever the plan asked for.

A plan that fails any of these raises `PlanRejected`, and the caller falls back
to the warehouse engine rather than running a query nobody validated.

**Backslash is escaped before the quote.** Doing it the other way round turns
`\'` into `\\'`, which closes the literal — the exact injection the function
exists to prevent. There is a test for it.

### Caching

Describes are cached per `(org identity, object)`, where the org identity is
`salesforce.org_key()` — instance URL + connected app + API version. A describe
is a function of the org *and* of what the connected identity may see, so a
cache keyed on the object name alone would serve one org's field list, or one
permission set's, to another. That is a data-leak shape, not a staleness shape.

### There is no write path

None is added by this feature. Reinterpreting a read request as a write is the
one mistake that cannot be undone by asking again.

---

## Numbers

Every count, total, percentage and ranking the answer states comes from
`tools.calculate_result`, computed in code over everything retrieved. The answer
prompt is shown the figures and a **sample** of rows, and is told the sample is
an illustration.

`record_count` prefers Salesforce's own `totalSize` over `len(rows)`. Those
differ whenever a page was capped, and quoting the page size as the total is the
single most common way a data answer becomes wrong — a 314-row result was once
summarised as "29 records".

A tool failure is **never** reported as an empty result. They are different
facts, and the answer says which one happened.

---

## SSE and metadata

No new event names. The existing `status` event gained additive keys:

```json
{ "text": "Analyzing 42 records", "phase": "analyzing_records",
  "run_id": "…", "record_count": 42 }
```

`text` remains first-class — every pre-existing client reads that and nothing
else — so replay, persistence and the backend allowlist are untouched. A payload
with **no** `phase` is the older web-search/URL progress line and keeps its own
row.

New `meta` keys, all additive: `clarification`, `salesforce_sources`,
`salesforce_scope`, `salesforce_error`, `assumptions`, `status`,
`salesforce_mode`. `meta.chart` is unchanged. The legacy `meta.clarify` is still
emitted when the feature is off and still renders on every conversation
persisted before this existed.

### The processing indicator

ONE indicator for every "something is happening" state in the app —
`components/Loader.tsx`. It replaced four separate vocabularies: shimmer bars
before the first token, a bordered spinner beside the web-search line, a
shimmering "Thinking…" label, and another spinner in the agent timeline.

The artwork is `frontend/public/loading.webm` (owner-supplied, 2026-08-11):
150×150 VP8 with a **real alpha channel** — 20,425 of its 22,500 pixels are
fully transparent — so it composites onto either theme with no matte box. 44 KB,
2.3 s, ~15 fps. Phase changes alter `playbackRate` rather than swapping artwork,
so moving from "Searching Salesforce" to "Verifying the totals" does not restart
the loop.

WebM alpha is a VP8 extension **Safari does not implement**, where the video
either fails or paints an opaque rectangle. `ReasoningStar` listens for the real
`error` event — no browser sniffing — and falls back to the original SVG
starburst, which takes `currentColor` and therefore works in both themes.

`prefers-reduced-motion` holds the artwork on its poster frame rather than
removing it: an indicator that vanishes reads as "nothing is happening".

### Progress labels

They are summaries of work that has **started**, never a narration of the
model's thinking and never a timer walking through plausible steps. A greeting
does not produce a single status event, because "Checking Salesforce fields"
under "hey there" is a fabricated step.

Raw chain-of-thought is not streamed, stored, or logged on this route: the
answer stream's `reasoning` deltas are dropped, and `strip_reasoning` removes an
inlined `<think>` block from a planner reply as a second line of defence.

---

## Context budgeting

A 262,144-token window is permission to include what matters, not an instruction
to include everything. `budget.py` assembles blocks in a fixed priority order and
drops the tail:

| Priority | Block | |
|---|---|---|
| 1 | system + security instructions | **pinned** |
| 2 | the current user request | **pinned** |
| 3 | pending intent / clarification | **pinned** |
| 4 | tool definitions | **pinned** |
| 5 | recent conversation turns | |
| 6 | the compact session summary | |
| 7 | previous Salesforce query state | |
| 8 | retrieved schema context | |
| 9 | retrieved Salesforce records | |
| 10 | older semantically relevant turns | |

The documented budget at 262144: `262144 − 16384 reserved output − 8192 margin
= 237568` tokens of input.

Verify the window is real, not just configured:

```bash
curl -s localhost:8080/health | jq .context
orchestrator/.venv/bin/python orchestrator/scripts/validate_long_context.py \
    --base-url http://127.0.0.1:8000/v1
```

The script builds prompts with the **served model's own tokenizer**, sends them,
and checks needle retrieval near the beginning, middle and end. A model that
accepts 240k tokens and answers only from the tail has a 240k window in the same
sense that a bucket with a hole has a capacity.

Measured on this DGX Spark host, 2026-08-11, against
`Qwen/Qwen3.6-35B-A3B-NVFP4` served at `--max-model-len 262144`:

| Target | Actual prompt tokens | Latency | Needles recalled |
|---:|---:|---:|:---|
| 64K | 65,487 | 17.6 s | 3 / 3 |
| 128K | 131,005 | 48.5 s | 3 / 3 |
| 200K | 199,925 | 96.9 s | 3 / 3 |
| 240K | 239,938 | 132.4 s | 3 / 3 |

**239,938 input tokens accepted and fully recalled.** The remaining ~22k of the
262,144 window is the output reservation plus the safety margin, which is
exactly the documented budget. Latency is roughly linear in prefill, as
expected; these are cold-ish single requests with no other load.

---

## Configuration

| Variable | Default | Effect |
|---|---|---|
| `SALESFORCE_INTELLIGENCE_MODE_ENABLED` | `true` | Off → the previous behaviour, exactly. |
| `SALESFORCE_CONTEXTUAL_CLARIFICATION_ENABLED` | `true` | Off → context still resolves, but nothing interrupts; assumptions are stated. |
| `SALESFORCE_STARTER_CARD_ENABLED` | `true` | The suggestion strip above an empty composer. |
| `SALESFORCE_MAX_CLARIFICATION_ROUNDS` | `2` | Then answer with the safest reading and say so. |
| `SALESFORCE_ALLOW_CUSTOM_CLARIFICATION` | `true` | The "Something else" escape. |
| `SALESFORCE_PLANNER_TOOL_CALLING` | inherits `MAIN_MODEL_ENABLE_AUTO_TOOL_CHOICE` | Tool calling first, guided JSON on a 400. |
| `MAIN_MODEL_MAX_LEN` | `MODEL_MAX_CONTEXT` → `262144` | The app's view of `--max-model-len`. |
| `MAIN_MODEL_HIGH_MAX_OUTPUT_TOKENS` | `16384` | Output reserved at high effort. |
| `MAIN_MODEL_CONTEXT_SAFETY_MARGIN` | `8192` | Full-window headroom. |

### vLLM flags

Added to the main service in `docker-compose.yml`,
`compose/compose.dgx-spark.yaml` and `compose/compose.nvidia.yaml`:

```
--tool-call-parser qwen3_xml
--enable-auto-tool-choice
```

Deliberately **not** set, and why:

| Flag | Why not |
|---|---|
| `--async-scheduling` | Already the default in vLLM 0.26; the flag exists to turn it off. |
| `--load-format fastsafetensors` | Needs the `fastsafetensors` package inside the image; it is not in `vllm-openai:nightly`. A faster cold start is not worth a service that refuses to boot. |
| `--language-model-only` | This service also serves **vision** (`VISION_MODEL` points at it). Disabling multimodal input to reclaim KV would break image answers. |
| `--max-num-seqs 4` | Measured here, not copied: 4 would throttle this shared, multi-consumer server. |

---

## Effort levels

| Effort | Planner | Execution |
|---|---|---|
| Fast | no thinking pass | minimal planning; Salesforce tools still mandatory for Salesforce facts |
| Low | short planning pass | one primary query, basic validation |
| Medium | thinking | schema/entity resolution, clarification gate, structured plan, calculation, verification |
| High | thinking | as Medium plus a larger output reservation and cross-checks; **not** more verbose for its own sake |

---

## Tests

```bash
# backend — needs a PostgreSQL server, nothing else
cd orchestrator
TEST_DATABASE_URL=postgresql://test:test@127.0.0.1:55432/techsara_test \
    .venv/bin/python -m pytest -q

# just this feature
... -m pytest -q tests/test_sf_query_plan.py tests/test_sf_clarification.py \
                 tests/test_sf_intel_engine.py tests/test_sf_intel_api.py

# frontend
cd frontend && npx vitest run
```

| File | Covers |
|---|---|
| `tests/test_sf_query_plan.py` | The compiler, mostly through refusals — injection, unknown objects/fields, invented relationships, SOQL's own aggregate rules. |
| `tests/test_sf_clarification.py` | Persistence, the round budget, the fingerprint, idempotency, rejection, typed-answer classification, carried context. |
| `tests/test_sf_intel_engine.py` | The pipeline end to end with the model and the org stubbed; provenance, pagination, failure-vs-empty, reasoning suppression, budgeting. |
| `tests/test_sf_intel_api.py` | The HTTP contract: SSE frames, the single meta, resume, restore/cancel endpoints, ownership, the feature flag. |
| `frontend/tests/clarification*.ts(x)` | The contract mirror, the keyboard map, focus and ARIA, the double-click guard. |
| `frontend/tests/phases.test.ts` | The phase vocabulary and the backward-compatible `status` event. |
| `frontend/tests/reasoning-star.test.tsx` | That the star only claims work the backend reported, and that it stops. |

---

## Diagnosing a Salesforce tool failure

1. `curl -s localhost:8080/health | jq '.checks, .context'` — is the model
   server up, and is the window what the app thinks it is?
2. The answer itself distinguishes the two cases. *"The Salesforce lookup
   failed…"* is a tool error; *"No matching records were found"* is a real empty
   result.
3. `docker logs sf-local-ai-orchestrator-1 | grep salesforce` — the planner logs
   one line per decision: `run`, `action`, `internal_reason_code`, and the slot
   names it resolved. No prompts, no record contents, no credentials.
4. A rejected plan logs `query plan rejected: <reason>` and falls back to the
   warehouse engine, so the user still gets an answer.
