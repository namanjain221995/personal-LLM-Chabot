# Configuration Reference

Started in Phase 1 of the reasoning-modes + code-interpreter mission with
the reasoning knobs; Phase 4 will centralize the remaining subsystems here.
Every value is an environment variable read once at orchestrator startup
(`orchestrator/app/config.py`).

## The effort ladder (2026-08-19 collapse)

| Level | Thinking | Tools | Extra |
|---|---|---|---|
| `fast` | off | none | answers straight away |
| `think` | **unbounded** | agent + search | default level |
| `max` | **unbounded** | agent + search (planning forced with search) | best-of-N with a judge |

Legacy wire values are accepted forever and normalized at the API boundary:
`low → fast`, `medium → think`, `high → think`, `extra_high → max`. Two
deliberate consequences: legacy *low* loses its search-only allowance, and
legacy *high* searches at Think depth (15 sources) — the old High research
depth now lives at Max.

## Unbounded thinking — the trade-off, stated plainly

**Budgets are OFF by default** (`THINKING_BUDGET_MODE=off`): this is a local
deployment with no per-token cost, so thinking runs until the model closes
it naturally. That buys maximum answer quality and costs **variable
latency**: hard questions at Think/Max may reason for **5–20+ minutes**
(the measured decode rate is ~46.6 tok/s single-stream, lower when Max runs
its N candidates concurrently). Nothing cuts a long thought — only two
physical guards exist:

- the **context window** (prompt + 65,536-token completion floor inside the
  262,144 window), and
- the **hang guard** (below), which only catches degenerate repetition
  loops, never real thinking.

**How to watch it live:**

```sh
# The thinking stream in the UI: the "Thinking…" panel updates live.
# Server side — per-generation usage telemetry (chunks ≈ tokens) and guards:
docker logs -f sf-local-ai-orchestrator-1 2>&1 | grep -E "generation usage|WALL CLOCK|best-of"
```

Every thinking generation logs `generation usage: <reasoning> + <answer>
chunks in <seconds>` — with budgets off this is the record of what
unbounded thinking actually costs, and the data any future budget decision
should be made from.

## Reasoning env vars

| Var | Default | Meaning |
|---|---|---|
| `THINKING_BUDGET_MODE` | `off` | `off`: unbounded thinking, no cutoff, no regeneration. `client`: re-enables the Phase 1 client-side enforcement exactly as built. |
| `MAX_OUTPUT_TOKENS` | `65536` | Completion floor for thinking-on requests (streaming, collector, and tools paths), so thinking + answer always fit. |
| `GEN_WALL_CLOCK_S` | `1800` | Hang guard per generation stream: past it the stream is killed, an ERROR is logged, and what was produced is returned with an inline note. 1800 s ≈ 84k tokens at 46.6 tok/s — far beyond any real answer; it exists for degenerate loops only. Also guards each best-of-N candidate via the non-streaming collector. |
| `EXTRA_HIGH_SAMPLES` | `3` | Best-of-N candidates at `max`, generated CONCURRENTLY; a thinking-off guided-JSON judge picks the winner (losers logged at INFO). `1` disables sampling. |

### Re-enabling budgets (if ever needed)

1. Set `THINKING_BUDGET_MODE=client` (and optionally tune
   `THINKING_BUDGET_HIGH` → Think, `THINKING_BUDGET_EXTRA_HIGH` → Max;
   `THINKING_BUDGET_MEDIUM` is retired by the ladder collapse).
2. Restart the orchestrator. Enforcement resumes exactly as built in
   Phase 1: max_tokens grows by the budget, reasoning chunks are counted
   (1 chunk = 1 token on this build), and past budget ×
   `THINKING_BUDGET_GRACE` (1.25) the stream is force-closed and the answer
   regenerates thinking-off on the original ceiling.
3. `SERVER_THINKING_BUDGET` stays `false` on this vLLM build: probed
   2026-08-19 under three key spellings and silently ignored (600/600
   reasoning tokens vs a budget of 64). If a future vLLM upgrade claims
   support, re-run the probe before flipping it, and never with tools
   attached (a server-side cut inside `<think>` can corrupt tool-call
   arguments).

Budget values were derived from the measured decode rate — see the
derivation kept below for the client mode.

### Measured basis (2026-08-19, this DGX Spark)

Decode rate on `Qwen/Qwen3.6-35B-A3B-NVFP4` (vLLM 0.20.1 NGC 26.05,
thinking on, warm, single stream): runs 43.4 and 49.7 → **mean 46.6 tok/s**.
Verified: one streamed chunk = one completion token on this build.

Client-mode budgets (`budget ≈ target_minutes × 60 × 46.6`):

| Effort (canonical) | Env | Tokens | Thinking target |
|---|---|---|---|
| think | `THINKING_BUDGET_HIGH` | 12,000 | ~4.3 min |
| max | `THINKING_BUDGET_EXTRA_HIGH` | 24,000 | ~8.6 min |

### Related pre-existing knobs

| Var | Default | Meaning |
|---|---|---|
| `MAIN_MODEL_DEFAULT_MAX_OUTPUT_TOKENS` | `8192` | Answer reservation (context budgeting), fast |
| `MAIN_MODEL_HIGH_MAX_OUTPUT_TOKENS` | `16384` | Answer reservation, think and max |
