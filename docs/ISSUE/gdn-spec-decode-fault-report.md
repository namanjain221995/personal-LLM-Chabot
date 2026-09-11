# Root-cause report — vLLM cluster dies mid-run and takes 15–20 minutes to return

**System:** DGX Spark two-node cluster, `Qwen/Qwen3.6-35B-A3B-NVFP4`, TP=2 / nnodes=2
**Window analysed:** 28 Aug – 10 Sep 2026
**Prepared:** 10 September 2026
**Changes applied:** none — this is analysis only

> Remediation status (2026-09-11): the fixes recommended in §9 were applied and
> validated; see `docs/ISSUE/gdn-spec-decode-remediation-2026-09-11.md`. This
> file is preserved as the incident analysis it was.

---

## 1. Finding

MTP speculative decoding drives the Qwen GDN (Mamba) attention layer into a code branch that
faults with `CUDA error: misaligned address` on mixed batches. Because the model is
tensor-parallel across two boxes with no redundancy, a fault on rank 1 hangs the entire
collective — the head engine can no longer complete any generation. The watchdog needs
~5 minutes to prove the engine is hung, and the reload costs another 4–9 minutes.

The in-flight analysis job dies inside that window because `APIConnectionError` is not caught
by the pipeline's retry loop.

**The fault is not reachable without speculative decoding.** This is established from the vLLM
source running in the container, not inferred from symptoms.

---

## 2. Why one fault is a total outage

The model is served tensor-parallel across two DGX Sparks. The head at `10.100.184.1:8000` is
the only HTTP endpoint; the second box runs `--node-rank 1 --headless` with no API server at
all. Engine arguments are byte-identical on both nodes, because vLLM's `mp` executor requires
every node to build the same `VllmConfig`.

TP=2 has no redundancy by construction. Every forward pass is a collective across both ranks,
so whichever rank faults, the other cannot proceed — it blocks in the collective until
`--distributed-timeout-seconds 300` expires. There is no degraded mode: the endpoint either
has both ranks or it serves nothing.

Recovery is correspondingly expensive. Both nodes reload 35B of weights, replay `torch.compile`
artifacts, run FlashInfer autotune, and recapture CUDA graphs before the endpoint answers again.

---

## 3. The fault chain (measured, 1–2 September)

| # | Step | Duration |
|---|------|----------|
| 1 | **Rank 1 faults.** A mixed batch enters the GDN spec-decode branch. `index_select` throws `CUDA error: misaligned address`. Kernel logs `Xid 13` across ~24 SMs, then `Xid 43`. | t = 0 |
| 2 | **The collective hangs.** Rank 0 on the head blocks waiting for a peer that will never answer. The HTTP server stays up and healthy; generations simply never return. | instant |
| 3 | **Watchdog proves it hung.** Two consecutive 120 s generation timeouts, three minutes apart, on a previously-armed engine. Fast HTTP errors are deliberately not counted. | ~5 min |
| 4 | **Head engine restarted.** `docker restart -t 30 sf-local-ai-vllm-1`. The worker's own healthcheck then kills its `vllm serve` process, because a restarted head is a new process group the old worker can never rejoin. | ~30 s |
| 5 | **Both nodes reload.** Weights, torch.compile replay, FlashInfer autotune, CUDA-graph capture, NCCL re-init, then `engine PROVEN ready`. | 4–9 min |

---

## 4. Primary evidence — the crash site

Every fault resolves to the same line of
`vllm/model_executor/layers/mamba/gdn/qwen_gdn_linear_attn.py`. This is the code as it exists
in the pinned image on the worker:

```python
if spec_sequence_masks is not None:              # only true when spec-decode is ON
    if attn_metadata.num_prefills == 0 and attn_metadata.num_decodes == 0:
        mixed_qkv_spec     = mixed_qkv
        mixed_qkv_non_spec = None
    else:                                        # MIXED batch: spec + prefill/decode
        mixed_qkv_spec     = mixed_qkv.index_select(0, spec_token_indx)
        mixed_qkv_non_spec = mixed_qkv.index_select(0, non_spec_token_indx)   # line 1255 — FAULTS
else:
    mixed_qkv_spec     = None
    mixed_qkv_non_spec = mixed_qkv               # no kernel launch — cannot fault
```

Traceback path:
`gpu_model_runner.execute_model` → `qwen3_5.forward` → `qwen3_next.forward` →
`qwen_gdn_attention_core` → `_forward_core`

### Three consequences follow directly from this control flow

1. **The fault is gated on speculative decoding.** Line 1255 sits inside
   `if spec_sequence_masks is not None:`, which is only non-`None` when spec-decode is active.
   Both nodes carry `--speculative-config '{"method":"mtp","num_speculative_tokens":1}'`.
   Without it, control takes the `else` branch — a plain assignment, no `index_select`, no
   kernel launch, no possible misaligned address.

2. **It only fires on mixed batches.** The faulting branch requires spec-decode sequences *and*
   prefills or decodes in the same batch. The pipeline runs `text_concurrency: 10`, so batches
   are continuously mixed — which is why this hits during long analysis runs and never when the
   cluster is idle.

3. **vLLM already degrades itself for this config.** Startup logs
   `CUDAGraphMode.FULL_AND_PIECEWISE is not supported with spec-decode … setting
   cudagraph_mode=PIECEWISE`, and JIT-compiles `eagle_prepare_inputs_padded_kernel` and
   `rejection_greedy_sample_kernel` *during inference*.

### Logged CUDA faults (worker)

| Timestamp | Error |
|---|---|
| 01 Sep 19:46:49 | `CUDA error: an illegal memory access was encountered` |
| 01 Sep 23:36:49 | `CUDA error: misaligned address` |
| 02 Sep 08:28:54 | `CUDA error: misaligned address` |

Matching kernel records: `Xid 13` (Graphics SM Warp Exception: Misaligned Address, ~24 SMs),
`Xid 31` (MMU fault, `ACCESS_TYPE_VIRT_WRITE` at `0x0`), `Xid 43` (channel killed,
`pid=python3`).

---

## 5. Corroboration — worker fault → head restart, every time

The watchdog on the head and the vLLM worker on the second box keep independent logs. They line
up on every event where both windows overlap.

| Time | Node | Event |
|---|---|---|
| 01 Sep 23:36:49 | worker | **CUDA error: misaligned address** |
| 01 Sep 23:38:49 | head | generation TIMED OUT (120s) — 1/2 |
| 01 Sep 23:41:29 | worker | Worker proc VllmWorker-1 died unexpectedly |
| 01 Sep 23:41:49 | head | TIMED OUT 2/2 → hung engine: restarting |
| 01 Sep 23:42:20 | head | container restarted |
| 01 Sep 23:42:58 | worker | NCCL re-init, rank 1 rejoining |
| 01 Sep 23:46:20 | head | engine PROVEN ready — armed |

Fault to serving: **9 min 31 s**. The same pattern repeats on 2 Sep 08:28:54 → 08:43:25, a span
of **14 min 31 s**. That range is the 15–20 minutes observed in practice.

### The watchdog is a witness, not a cause

`sf-local-ai-vllm-watchdog-1` arms only after a proven completion, counts only generation
timeouts (not fast HTTP errors), and refuses more than one restart per 45 minutes. All six of
its restarts follow a genuine engine hang:

```
01 Sep 23:41:49   02 Sep 06:27:27   02 Sep 08:34:20
02 Sep 10:55:01   02 Sep 13:23:17   09 Sep 19:55:49
```

Each is followed ~5 minutes later by the worker's NCCL re-init.

Its own source comments record a `sed` bug that restarted a *healthy* engine three times on the
evening of 1 September — that bug is already fixed, which means raw restart counts overstate the
true fault rate.

---

## 6. Discriminator — this is not a hardware fault

Prometheus, `changes(process_start_time_seconds[14d])`:

| Service | GPU it runs on | Restarts in 14 days |
|---|---|---|
| **`vllm-main`** | **both (TP=2)** | **22** |
| router | head GPU | 3 |
| embed | head GPU | 3 |
| ocr | head GPU | 3 |
| reranker | head GPU | 3 |
| ocr (spark-2) | **worker GPU** | 2 |
| whisper asr (spark-2) | **worker GPU** | 1 |

The asymmetry is the argument. Four other models share the very same two GPUs, the same driver
and the same boxes. OCR and Whisper run on the worker's own GPU — the one throwing every Xid —
and restarted twice and once. A marginal GPU, a bad driver or unstable power would not single
out one model and spare four others sharing the same silicon.

The fault is specific to the main model's configuration, which is exactly what the source
analysis independently predicts. **There is nothing here to RMA.**

---

## 7. Why the analysis job dies with it

The cluster self-heals. The analysis run does not, and that is a separate defect.

In `interview_analysis/models/client.py`, the retry loop catches only
`(LLMError, ValidationError, json.JSONDecodeError)`. An `openai.APIConnectionError` is not in
that set, so it propagates straight out of `chat_json` and ends the run. The three attempts also
fire back-to-back with no backoff, so all three are consumed within a second of the outage
beginning — nothing waits out a reload that takes minutes. `AsyncOpenAI(timeout=300)` means a
request already in flight blocks for a further 5 minutes first.

Observable cost, from the 1–2 September sweep:

- **10 of 101 jobs stranded at state `preprocessed`** — preprocessing complete, analyze never
  finished. The sweep cannot recover them; it exits with
  `cannot refresh candidate analysis from state 'preprocessed'`.
- **All 172 sweep logs are 0 bytes**, written between 23:51 and 06:05. When this happens there
  is no diagnostic trail at all, which is much of why the cause stayed obscure.
- **The re-run is cheaper than it appears.** `run_all(retry=True)` rewinds to the last completed
  phase and reuses `out_dir`, so preprocessing is not paid twice — but only if the operator
  knows to invoke it.

---

## 8. What was ruled out

| Hypothesis | Verdict | Basis |
|---|---|---|
| Memory exhaustion / OOM | Ruled out | At the 8 Sep event the box was 99.55% idle with 69 GB available. No `oom-kill` entry in any boot. |
| Suspend or idle sleep | Ruled out | `sleep-inactive-ac-type='nothing'`, `idle-delay=0`, zero suspend events across every boot on record. |
| Thermal throttling | Ruled out | All thermal zones 35–44 °C. Peak under full benchmark load was 76 °C with no throttle events. |
| Marginal GPU / faulty hardware | Ruled out | Four co-resident models on the same two GPUs restarted 1–3 times against the main model's 22. |
| Watchdog restarting a healthy engine | Ruled out | Every restart follows two proven generation timeouts. The one historical instance was a `sed` bug, already fixed. |
| Machine reboots | Out of scope | Six uncontrolled resets of both nodes between 24 Aug and 10 Sep, no clean shutdown, downtime 2 min to 5 h. Confirmed as internal decisions — excluded from this root cause. |
| **MTP spec-decode on hybrid-Mamba GDN** | **Cause** | vLLM source, three logged CUDA faults, matching kernel Xid records, restart asymmetry. |

---

## 9. Recommended remediation — none applied

1. **Remove `--speculative-config`.**
   This is the fix. It makes the faulting line unreachable rather than less likely. Cost is
   roughly 10–20% of decode throughput on a workload whose ~10k-token prompts are
   prefill-dominated — where MTP contributes least.

2. **Remove `--enable-prefix-caching`.**
   Measured benefit on this model is 0.99× — none, because the architecture is hybrid-Mamba.
   vLLM marks it experimental on Mamba layers, and it has already caused one crash-loop on this
   cluster. Pure risk against no payoff.

3. **Catch `APIConnectionError` and `APITimeoutError` with backoff.**
   Gate retries on polling `/health` until it answers 200, with a ceiling around 20 minutes.
   Converts a restart from a lost job into a paused one. Worth doing regardless of the engine
   change.

4. **Auto-retry jobs stranded at `preprocessed`.**
   The sweep should invoke the existing `retry=True` path rather than exiting. Fix the 0-byte
   log redirection at the same time to restore the diagnostic trail.

5. **Consider lowering `--distributed-timeout-seconds`.**
   300 → ~90 s removes several minutes from every event. Needs judgement: too low risks aborting
   a legitimately long prefill at 1M context.

### Change path

Both engine changes must go through the head's `.env` followed by `scripts/cluster-sync.sh`.
`worker.env` is generated and carries a do-not-edit header; hand-editing it will be overwritten
and will break the byte-identical-args requirement the `mp` executor depends on.

---

## 10. Limits of this analysis

- The head's vLLM log begins `2026-09-07T22:21:51` and does not cover the 1–2 September faults.
  Its apparent cleanliness in that window is **not** evidence that rank 0 never faults — only
  that we cannot see it. The worker's log covers the period and is where all three CUDA faults
  were captured.
- Restart counts from Prometheus include machine boots and the six already-fixed spurious
  watchdog restarts, so 22 is an upper bound on genuine faults. The count of confirmed CUDA
  faults with a full traceback is three.
- No fix has been tested on this cluster. The claim that removing `--speculative-config`
  eliminates the fault rests on control flow read from the installed source — strong, but not
  the same as an observed clean run. Validation would be a sustained multi-hour sweep at
  `text_concurrency: 10` with no restart.

---

## Evidence base

`journalctl` across 8 boots · `docker logs` on both nodes · Prometheus 14-day series ·
`sar` resource history · vLLM source read from the pinned container image · `fleet.db` ledger
and `sweep_jobs.json`.

No configuration, code or container state was modified in the course of this investigation.
