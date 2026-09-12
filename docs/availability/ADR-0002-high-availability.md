# ADR-0002 — One main model, hardened self-healing, and request continuity through its reloads

**Status:** Accepted 2026-09-12; **revised the same day to v2 (strict one-model mode)** — the
2026-09-12 05:14 IST decision ("Option D", the router as a stand-in answer model) is withdrawn by
product requirement, see § "Fallback answer model rejected by product requirement". Lead: principal
AI-infrastructure engineer; evidence in `docs/availability/INCIDENT-2026-09-11-vllm.md` and
`.runtime/incidents/20260911T223140Z-vllm-down/`.
**Supersedes:** the shell watchdog design note in `compose/compose.dgx-spark.yaml` (2026-09-01).
**Related:** `docs/availability/CONTRACT.md` v2 (the mechanism, binding), `docs/availability/SLO.md`
(the numbers), `docs/availability/CANDIDATE-B.md` (the engine candidate and its evaluation order),
`docs/ISSUE/gdn-spec-decode-remediation-2026-09-11.md` (the fault class itself).

## Context

The main model, `nvidia/Qwen3.6-35B-A3B-NVFP4` with a verified 1,000,000-token window, runs as
**one** vLLM engine tensor-parallel across the two DGX Sparks (TP=2, `--nnodes 2`, the `mp`
executor). Two machines, one replica: when either rank's process dies, the engine is gone until
both ranks reload the model (measured 3 m 32 s warm, 5 m 20 s cold — `10-vllm-head-full.log`).

The rank processes die about once a day under the second tenant's concurrency-10 mixed
prefill+decode load, from an unclosed Triton-kernel bug in vLLM `0.26.1rc1.dev77`'s GDN prefill
path (`misaligned address`, Xid 13/31; 2026-09-09 19:52Z, 09-10 21:34Z, 09-11 07:05Z, 09-11
22:15Z). Whether or not a newer vLLM build closes that bug (§ "Follow-ups"), a single replica of a
two-node engine will always have *some* failure that needs a reload, and the 2026-09-11 22:15Z
incident showed that today's stack turns a sub-second fault into an 11-minute outage with the
first five minutes reported healthy.

Memory is the binding constraint on the head (121.7 GiB unified): main rank 0 (25.4 GiB GPU
allocation), the router `Qwen3-VL-8B-Instruct-FP8` (21.3 GiB), embed (4.0), reranker (4.0), whisper
(3.4), plus PostgreSQL, the orchestrator, the sync worker and developer tooling; 12 GiB was in swap
during the incident and `MemAvailable` fell to 17.9 GiB under load (`52-swap-timeseries.txt`). The
worker has ~53 GiB available. No additional model can be loaded on the head without pushing the
primary into swap-stall territory.

**The product requirement (2026-09-12, v2).** Only `nvidia/Qwen3.6-35B-A3B-NVFP4` may generate an
answer a person reads. No 8B stand-in, no external provider, no second answer-generating model of
any size. The router engine stays what it was before this programme: an internal classifier
(intent, freshness, frame captions) whose output is never shown as an answer. A person who sends a
request while the main model is reloading is told the truth, the request is kept, and the *same*
request is answered by the main model when it is back.

## Options considered

| | **A — TP=2 primary, hardened, with request continuity** | B — two single-Spark replicas | C — four Sparks, two TP=2 replicas | D — TP=2 primary + resident small stand-in model | E — approved external provider |
|---|---|---|---|---|---|
| Large model, 1M window | yes | **no** (an 8–14B model per Spark, ≤ ~256K) | yes | yes (stand-in is 8B / 49K while the primary reloads) | yes (primary) |
| Same-model hardware redundancy | no | yes (for the smaller model) | **yes** | no | n/a |
| Answers served **during** a reload of the 35B | **none** — accepted requests are durably queued and resume on the same generation when READY | ~0 outage (router in front) | ~0 outage, same model | 8B answers for eligible chat | external answers |
| What a person sees during a reload | one truthful line, the request kept, the answer arrives from the main model ≈ 4–6 min later | nothing | nothing | an answer from a smaller model, labelled | an answer from outside |
| Only one model ever answers | **yes** | yes (but a smaller one) | **yes** | **no** | **no** |
| Extra memory on the head | 0 | −25 GiB (rank 0 gone) | 0 | 0 (the router is already resident) | 0 |
| Extra hardware | 0 | 0 | **+2 DGX Sparks** | 0 | 0 |
| Privacy / data governance | local | local | local | local | **data leaves the machine** — not authorised |
| Implementable now | **yes** | needs a model re-selection + re-validation of every feature | no (hardware) | yes | no (product authorisation) |
| Verdict | **chosen** | rejected (capability) | **the only path to answers during a reload**; hardware request | **rejected by product requirement** | rejected (privacy, authorisation) |

## Decision

**Option A, hardened, plus request continuity.** The single TP=2 instance stays the only thing
that answers. The programme changes how fast its death is *seen*, how it is *brought back*, what
happens to the *requests that arrive while it is down*, and how load is *admitted* so the fault is
triggered less often. It does not change who answers. Concretely (`CONTRACT.md` v2 is binding):

1. **Detection and recovery, one authority.** The engine controller (compose service
   `engine-controller`, `monitoring/engine-controller/controller.py`) is the **only** actor that
   restarts the pair (CONTRACT §6). It proves the engine with the five-step readiness sequence of
   §5 v2 — a non-streaming completion, a streaming completion, token-counter progress on
   `/metrics`, both GPUs observed working during a participation probe, the worker rank process
   reported alive by the sentinel — and the routine canary (a streamed 4-token completion every
   30 s) keeps proving it. The worker sentinel (`sentinel.py`, `vllm-worker-sentinel` on Spark 2)
   is a sensor, and an actuator only on the controller's `POST /restart`
   (`SENTINEL_AUTONOMOUS=0` in production). The Docker healthchecks are report-only, with one
   documented last-resort tier that fires only after `VLLM_HEALTHCHECK_KILL_AFTER` consecutive
   misses (default 8 = 4 min — long after a live controller would have acted). Manual recovery
   (`scripts/cluster-recover.sh` → loopback `POST /recover`) is subject to the same budget and
   cooldown as an automatic one. `ulimits: core: 1` removes the 4 m 51 s apport hold. The
   choreography: diagnostics captured first, worker restarted first, then the head, under one
   host `flock`, budget 3 per hour, cooldown 120 s.
2. **Request continuity** (CONTRACT §8.3 v2, `orchestrator/app/continuity.py`). When the breaker
   is OPEN or the controller reports STARTING / WEDGED / RECOVERING / DOWN, a request is
   **accepted and persisted first** (the V29 `chat_requests` row, status `queued`, with its
   logical `generation_id` and `intent_id` fixed for its whole life), the person reads exactly one
   status line — `Main model is recovering—your request is safely queued.` — the SSE stream
   stays alive with heartbeats, the worker waits on the engine-state client's READY event (never
   polling the dead port) up to `LLM_QUEUE_MAX_WAIT_S=900`, and then the **same** generation
   resumes exactly once (`attempt` + 1, `retry_reason=recovery`); the V29 guards make a second
   answer impossible. If the wait expires the row stays `queued` with the line `The main model is
   still recovering. Your request is kept and will resume automatically.` — never a 500, never an
   answer from another model — and the resume sweep (§8.4 v2) picks it up under a lease when
   READY arrives. `chat_with_tools`, vision, Deep Research, video and the artifact stages queue
   the same way. Every attempt records `attempt`, `engine` (always `primary`), `retry_reason`,
   `terminal_state`; `llm_resumed_generations_total{outcome="resumed|expired|duplicate_suppressed"}`
   counts what the sweep did.
3. **Long-context admission** (CONTRACT §6.7, `orchestrator/app/admission.py`). Two lanes in
   front of vLLM: NORMAL (prompt ≤ 131,072 tokens, `ADMISSION_NORMAL_MAX=10` concurrent) and LONG
   (one at a time, started only when the engine reports `requests_running ≤ 0`, holding the
   NORMAL lane until its first token). The wait is durable and truthful (`Waiting for the model to
   finish current work before your large document (N ahead).`). The point is the fault's trigger
   shape — nine concurrent mixed prefill+decode requests — and the 1M-token window's memory: the
   advertised window stays 1,000,000 tokens; what is configured, tested and production-safe is
   reported separately in `MEMORY-BUDGET.md`.
4. **Compiled-kernel caches** (CONTRACT §6.6). Both ranks keep `/root/.cache/vllm` and
   `/root/.cache/flashinfer` on persistent named volumes so a recovery reuses the validated
   torch.compile / FlashInfer JIT artefacts (cold compile measured 50 s; a FlashInfer GDN JIT is
   longer). A stale cache is a `cold_start_timeout` or a compile-error signature in the head log;
   only the runbook's `scripts/cluster-recover.sh --clear-kernel-cache` empties the volumes, under
   the lock — the controller never deletes a cache.
5. **Nothing new is loaded on the head.** The router keeps its 21 GiB as the classifier it always
   was; no model is added anywhere.

B is rejected because the platform's value (1M-token documents, Deep Research, tool use at 35B
quality) is exactly what a single Spark cannot hold. E is rejected on privacy grounds and is not
authorised. D is rejected below. **C is the stated path** if the business needs answers *during*
a reload: it is the only option that keeps the 35B/1M-token capability, keeps one model, and
removes the single replica — and it costs two more DGX Sparks.

## Fallback answer model rejected by product requirement (2026-09-12)

The 05:14 IST version of this ADR chose Option D: the router engine (`vllm-router:30002`,
`Qwen/Qwen3-VL-8B-Instruct-FP8`) was to answer plain chat turns while the primary reloaded, with
tools stripped, thinking off, input capped at 24,000 tokens and a status line saying so. It had
been measured from the orchestrator's network (`60-fallback-router-quality.json`: TTFT 68 ms,
~30 tok/s, JSON mode valid, HTTP 400 on any `tools`), and the v1 code implemented it
(`orchestrator/app/fallback.py`, `FALLBACK_*` settings, `llm_fallback_active`, state 7
`FALLBACK_ACTIVE`, the `VllmFallbackActive` / `VllmPrimaryDownNoFallback` alerts).

**Withdrawn.** The product requirement is that one model, and only that model, writes what a
person reads: an 8B answer with a label is still an answer from another model, with a different
window, no tools, no thinking and a different voice, and a person cannot be expected to re-ask
the main model afterwards. The stand-in also inverted trust metadata in the v1 review (an answer
the primary wrote could carry `engine: fallback` when a sidecar call had used the router —
`REVIEW-FINDINGS-round1.md`, orchestrator findings) and it made the router's availability a
serving concern it was never designed for.

What replaces it is **not** a substitute answer but **continuity**: the request is durably
queued, the person is told the truth once, and the *same logical generation* resumes on the main
model when the controller proves it READY. The v1 answer-routing path is **deleted, not
disabled**: `orchestrator/app/fallback.py`, every `FALLBACK_*` key, the `fallback` breaker
engine, `llm_fallback_active` and the two alerts above are gone; the router's health remains a
DEGRADED signal for *routing* (`techsara_vllm_router_available`), never a serving path; state 7 is
`QUEUEING` (the orchestrator holds ≥ 1 accepted generation for a primary that is not
READY/BUSY). Drill 15 of `scripts/recovery-tests/engine_failure_drills.sh` now proves the queue
and the same-generation resume instead of a stand-in activation.

Stated plainly: **with one 35B instance, no request is answered while that instance reloads.**
The programme bounds the reload to ≈ 4–6 minutes and makes the wait safe and truthful. The only
way to *serve answers during* a reload of the single instance is a second TP=2 replica of the
same model — Option C, two more DGX Sparks — and the SLO document (`SLO.md` §4) says which
objectives each architecture can meet.

## Consequences

- Availability of *answers* stays a single replica's figure (SLO A: 99.5 % on this build, one
  ≈ 5-min reload per day); what improves is that an accepted request is never lost, never
  duplicated, never answered by another model, and resumes without the person doing anything
  (SLO B, "request continuity availability"), and that a person sees one truthful line instead
  of a spinner or a generic error.
- Every request class — plain chat, tools, vision, Deep Research, video, artifacts — behaves the
  same way during a reload: it queues. There is no "eligible" subset any more.
- The router's availability matters once (classification), as before the programme. Its health
  is a DEGRADED signal for routing; nothing pages for it as a serving path.
- The head's memory remains the constraint. Moving the router to the worker (≈ 22 GiB off the
  head; `MEMORY-BUDGET.md`) is still the next memory step, now for memory alone.
- Long prompts are serialised (one LONG-lane generation at a time) and may wait; the wait is
  bounded (`ADMISSION_LONG_WAIT_S=600`), durable and told to the person.
- True same-model redundancy is **not** achieved by this decision. It needs Option C.

## Follow-ups (tracked in `docs/availability/RUNBOOK.md` §Follow-ups)

1. **The fault class is not closed by any released vLLM build, and this ADR records that an image
   upgrade was evaluated and rejected as a closure** (`docs/availability/VLLM-UPGRADE-RESEARCH.md`,
   2026-09-12): the two PRs the 2026-09-03 note relied on (#51812, #51674) are MTP-only; upstream
   issues #49926 and #37431 reproduce the same Xid 13/31 signature on this model and GPU on every
   build from 0.23 to 0.28.1 (including a two-DGX-Spark TP=2 pair on 2026-09-07 with Model Runner
   V2), with and without MTP, prefix caching, CUDA graphs or mixed batches. Production stays
   pinned on `sha256:24f2f897…`; the `nightly` images cached on both nodes (`sha256:7d5128a9…`)
   contain no fix and are **not** to be deployed. What remains is one candidate and a set of
   secondary tests, in a scheduled change window, each gated by the 120-min soak and the research
   report's §6.4 criteria, run **one variable at a time** by `scripts/cluster-ab.py` and recorded
   in `docs/availability/CANDIDATE-B.md`:
   - **Track B, evaluated first:** the post-`f6326f5` candidate (the first nightly after #55715
     "FlashInfer GDN prefill kernel on SM12x", merged 2026-09-08 — `nightly-385dce36…`, digest
     `sha256:819ec9c0…`) with `--gdn-prefill-backend flashinfer` set explicitly on both ranks
     (`CLUSTER_GDN_PREFILL_BACKEND`, rendered into `CLUSTER_ENGINE_ARGS`), image selected by
     `MAIN_MODEL_IMAGE` for the `vllm` and `vllm-worker` services only (router and OCR stay on the
     proven digest), `VLLM_ALLREDUCE_USE_FLASHINFER=0` on both ranks. It moves
     `chunk_gated_delta_rule` off the Triton kernel the py-spy captures sat in.
   - **Secondary tests, one at a time, after Track B's verdict:** `--max-num-partial-prefills 1`;
     `--max-long-partial-prefills 1`; `--max-num-batched-tokens 4096` vs `8192`;
     `--max-num-seqs 10` vs default; and **fifth, Track A** `--moe-backend flashinfer_b12x`
     (the only positive stability reports for this class on GB10 come from that backend; it
     executes the W4A16 checkpoint as W4A4 and adds ≈ 5 GiB per rank, so it needs the output-
     quality check of the research report's §6.4 as well as the soak).
   A 120-min pass is necessary, not sufficient: upstream MTBFs are 9–72 h, so the class counts as
   closed only after ≥ 7 days of production with no controller incident of category ≠ `none`.
2. Router to the worker (memory only, ≈ 22 GiB off the head).
3. Option C hardware request if the business needs answers during a reload.
4. The second tenant's pipeline hits the raw port over the RoCE address at concurrency 10 — the
   load shape that fires the fault — and is outside the orchestrator's admission lanes and
   continuity; its client-side patches (`docs/ISSUE/interview-analysis-client/`) remain the
   mitigation on its side.
