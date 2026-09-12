# Unified-memory budget, both DGX Sparks (measured 2026-09-12 04:07 IST, `53-`/`54-head-memory-map.txt`)

GB10 has no separate framebuffer: a "GPU allocation" is host memory, and `nvidia-smi memory.used`
reads N/A. Allocations below come from `nvidia-smi --query-compute-apps` (GPU) and `docker stats` /
`ps` RSS (CPU side); they overlap partially (a process's RSS includes some of its mapped GPU
buffers), so the column sum over-counts by a few GiB and is read as a ceiling.

## Spark 1 — head (121.7 GiB total; MemAvailable 24–35 GiB; 12 GiB in swap)

| Resident | GPU alloc (GiB) | cgroup / RSS (GiB) | Placement verdict |
|---|---|---|---|
| Main model rank 0 (`VLLM::Worker_TP0`, weights ½ of 21.8 + 8 GiB KV budget + graphs) | 24.8 | 15.9 (container) | stays — TP=2 measured win |
| Router `Qwen3-VL-8B-Instruct-FP8` (also the **fallback**) | 20.8 | 1.2 | **move to the worker** (next change window; ≈ 22 GiB freed, and the fallback then survives a head loss) |
| Embed `Qwen3-Embedding-0.6B` (2 GiB KV budget) | 3.9 | 0.3 | stays (on the chat path, 12–180 ms calls) |
| Reranker `Qwen3-Reranker-0.6B` (2 GiB KV budget) | 3.9 | 0.3 | stays |
| Whisper replica | 3.3 | 0.1 | stays (one per node) |
| PostgreSQL + exporter | — | 0.4 | stays |
| Orchestrator (uvicorn) | — | 1.2 | stays |
| Sync worker | — | 1.9 | stays (CPU 55 % avg — worth its own look) |
| Prometheus + Grafana + exporters + cAdvisor + SearXNG + tunnels | — | ≈ 0.6 | stays |
| Non-production residents: `pg-test` (0.8), `litellm-dgx` (0.1), `techsara-e2e-*` (0.25), `portainer`, `zealous_williamson` | — | ≈ 1.2 | stop when not in use (owner's call) |
| Developer tooling (two vscode-server trees, Claude sessions, pytest) | — | 5–25 | the swing that decides whether the head swaps |
| Page cache | — | 17–23 (reclaimable) | |
| **Sum of allocations** | **≈ 57** | **≈ 23 + tooling** | |

## Spark 2 — worker (121.7 GiB total; MemAvailable ≈ 53 GiB; 8 GiB in swap)

| Resident | GPU alloc (GiB) | cgroup / RSS (GiB) |
|---|---|---|
| Main model rank 1 (`VLLM::Worker_TP1`) | 24.8 | 26.5 (container) |
| OCR `Unlimited-OCR` | 13.5 | 5.8 |
| Whisper replica | 4.9 | 1.6 |
| `ir-team-automation-postgres` (second tenant) | — | 0.03 |
| Exporters | — | 0.03 |
| gnome-remote-desktop (two, on the GPU) | 0.4 | — |
| Page cache | — | ≈ 30 (reclaimable) |
| **Sum** | **≈ 44** | **≈ 34** |

## Did "82 % memory on Spark 1" contribute to the incident?

No, as cause: the fault was a GPU MMU fault inside a Triton kernel on **Spark 2**, whose
`MemAvailable` was 40–45 GiB throughout (`52-swap-timeseries.txt`), and no OOM-killer or
`NV_ERR_NO_MEMORY` line exists on either node in the 22:00–22:30Z window. Yes, as an aggravating
factor for recovery: the head paged out 1,286 pages/s during the reload (22:30Z) and the model
load read 21.8 GiB of weights through a page cache that had 17.9 GiB available at 22:10Z. Earlier
the same day (07:20–07:34Z and 09:21Z) the head kernel *did* log `NV_ERR_NO_MEMORY` during a
model start beside the other residents — that is the swap-stall risk the placement table
addresses, and why nothing new may be loaded on the head (ADR-0002).

## Limits in force and what happens when they are exceeded

| Limit | Configured | Tested | Safe production maximum | Over the limit |
|---|---|---|---|---|
| Context window | 1,000,000 tokens (`--max-model-len`) | needle at 949,915 tokens (2026-08-30); 32K prefill 4.1 s; 20K on the fallback | 1,000,000 at ≤ 1.66 concurrent full-window requests (KV budget 8 GiB → 1,663,201 tokens) | vLLM rejects the request (400) — never retried |
| KV cache | `--kv-cache-memory-bytes 8589934592` per rank | yes | as configured | requests queue (`num_requests_waiting`); `VllmKvCacheNearlyFull` at 90 % |
| Batched tokens / step | `--max-num-batched-tokens 8192` (chunked prefill) | yes | as configured | bounds every RPC to one 8,192-token step |
| Concurrent sequences | vLLM default (`MAIN_MODEL_MAX_NUM_SEQS=0`) | 10 concurrent mixed (soak) | 10 — the second tenant's load; **the load under which the GDN fault fires on this build** | queue; the orchestrator's `_LLM_SEM` caps its own generations at 2 |
| GPU memory utilisation | 0.30 per rank | yes | as configured (explicit KV budget makes it deterministic) | start fails with a negative KV budget (seen 2026-09-09 on embed before the explicit budget) |
| Fallback input | 24,000 tokens (`FALLBACK_MAX_INPUT_TOKENS`) | 20,000 answered correctly | 24,000 of the 49,152 window | history truncated, oldest turns first |
| Recovery restarts | 3 per hour | drill | 3 | controller stands down (DOWN, `budget_exhausted`), fallback stays, critical alert |
