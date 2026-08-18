# Configuration Reference

Started in Phase 1 of the reasoning-modes + code-interpreter mission with
the reasoning knobs; Phase 4 will centralize the remaining subsystems here.
Every value is an environment variable read once at orchestrator startup
(`orchestrator/app/config.py`).

## Reasoning effort & thinking budgets

### Measured basis (2026-08-19, this DGX Spark)

Decode rate on the main model (`Qwen/Qwen3.6-35B-A3B-NVFP4`, vLLM 0.20.1
NGC 26.05, thinking on, warm prefix cache, single stream, 1,200-token
generations):

| run | tok/s |
|---|---|
| 1 | 43.4 |
| 2 | 49.7 |
| **mean** | **46.6** |

Also verified on this build: **one streamed chunk = one completion token**
(`usage.completion_tokens` equalled the chunk count exactly), which is why
client-side budget enforcement counts chunks.

### Budget derivation — `budget ≈ target_minutes × 60 × 46.6`

| Effort | Thinking target | Budget (tokens) | Env override |
|---|---|---|---|
| fast | none (thinking off) | — | — |
| low | none (thinking off) | — | — |
| medium | ~1.5 min | 4,000 | `THINKING_BUDGET_MEDIUM` |
| high | ~4.3 min | 12,000 | `THINKING_BUDGET_HIGH` |
| extra_high | ~8.6 min | 24,000 | `THINKING_BUDGET_EXTRA_HIGH` |

extra_high runs its samples concurrently (best-of-N), so per-stream decode
drops below 46.6 tok/s under load and the wall-clock target stretches
accordingly — budgets are token ceilings, not time guarantees.

### Enforcement

| Var | Default | Meaning |
|---|---|---|
| `THINKING_BUDGET_GRACE` | `1.25` | Client-side cap = budget × grace; the stream is force-closed past it and the answer is regenerated with thinking off (warning logged, partial reasoning stays visible). |
| `SERVER_THINKING_BUDGET` | `false` | Send `chat_template_kwargs.thinking_token_budget` server-side. **Leave false on this build**: tested empirically 2026-08-19 — `thinking_token_budget`, `thinking_budget` and `max_thinking_tokens` were all silently ignored (600/600 reasoning tokens generated against a budget of 64, no error). If a future vLLM upgrade honors it, re-run the probe before enabling, and note it is *never* used when tools are attached (a server-side cut inside `<think>` can corrupt tool-call arguments — client-side sizing handles the tools path). |

The budget always **grows** `max_tokens` (answer ceiling + thinking budget)
so reasoning can never starve the answer — the historical failure was 121 s
of thinking and zero output on an 11,500-token SQL prompt.

### Best-of-N (extra_high)

| Var | Default | Meaning |
|---|---|---|
| `EXTRA_HIGH_SAMPLES` | `3` | Concurrent candidate generations at extra_high on the chat route; a thinking-off judge (`select_best`, guided JSON) picks the winner, whose thinking + answer stream to the UI. Losing candidates are logged at INFO for debugging. `1` disables best-of-N while keeping the extra_high thinking budget. |

### Related pre-existing knobs

| Var | Default | Meaning |
|---|---|---|
| `MAIN_MODEL_DEFAULT_MAX_OUTPUT_TOKENS` | `8192` | Answer reservation, fast/low/medium |
| `MAIN_MODEL_HIGH_MAX_OUTPUT_TOKENS` | `16384` | Answer reservation, high and extra_high |
