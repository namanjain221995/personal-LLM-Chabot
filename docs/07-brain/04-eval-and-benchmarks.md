# Evaluation and benchmarks

Everything here is reproducible with the tools in `orchestrator/tools/`
inside the orchestrator container, and nothing in the dataset is
hand-written: questions are generated from pages the platform has actually
read, so the eval follows the corpus wherever it goes.

## 1. Retrieval eval (`tools/rag_eval.py`)

```
python -m tools.rag_eval generate --n 60 --out /data/eval/dataset-v1.json
python -m tools.rag_eval run --dataset /data/eval/dataset-v1.json \
    --variants legacy,dense,lexical_and,lexical_or,unified --label <label> \
    --out /data/eval/report-<label>.json
```

`generate` samples stored pages (≤ 2 per domain, distinct content), cuts one
paragraph-aligned ~1,400-char window, and asks the router model for one
question that window answers plus the short answer copied from it; items
whose answer is not literally in the window are dropped. Gold = the page
(any page with identical text counts).

Metrics: Recall@1/5, MRR, NDCG@5 (page identity) and **answer@5** — the gold
answer string is present in the text the prompt would actually see. The
last one is the number that predicts a faithful answer and the one the
design is judged by; the identity metrics are diagnostics (they reward the
generating chunk and can be gamed by lexical entanglement, as the design
critique noted).

### Baseline (before ADR-0001, dataset v1, 60 items, 2026-09-03)

| variant | recall@1 | recall@5 | mrr | ndcg@5 | answer@5 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| legacy (Fast path as deployed) | 0.433 | 0.617 | 0.505 | 0.533 | 0.517 | 171 | 10,085 |
| dense only | 0.483 | 0.783 | 0.618 | 0.655 | 0.600 | 63 | 10,030 |
| lexical AND | 0.533 | 0.683 | 0.590 | 0.611 | 0.617 | 3 | 17 |
| lexical OR (as deployed) | 0.067 | 0.217 | 0.144 | 0.147 | 0.250 | 88 | 140 |

Reading: the deployed hybrid was WORSE than either of its halves alone,
because the OR half injected term-frequency noise and the recency partition
dropped answering pages. The p95 of ~10 s on the dense variants is the
embedding client's connection setup on a cold pool (one client per call);
fixed by client reuse.

### After deployment (dataset v1, same 60 items, 2026-09-03)

| variant | recall@1 | recall@5 | mrr | ndcg@5 | answer@5 | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| unified, first deployment | 0.500 | 0.633 | 0.546 | 0.568 | 0.550 | 532 | 786 |
| dense only (instruction-prefixed query) | 0.633 | 0.867 | 0.731 | 0.761 | 0.683 | 25 | 32 |
| lexical AND (raw words, normalisation 32) | 0.533 | 0.683 | 0.590 | 0.611 | 0.617 | 18 | 26 |
| **unified, after the supersession/collapse/candidate fix** | **0.650** | **0.850** | **0.723** | **0.754** | **0.717** | 558 | 791 |

Reading: the first deployment's unified pipeline was *below* its own dense
half. A stage-by-stage trace of the 17 questions dense found and unified
lost (`scratchpad/trace-lost.py`) showed 13 retired by the supersession
rule — a newer page that merely *related* (answer probability ≥ 0.30)
retiring an older page judged as *answering* at 1.0 — 3 never judged because
recency weighting pushed them below the 12-candidate cut, and 1 collapsed
into its sibling release page. Three rule changes followed: supersession
runs only for replaceable facts (office holder, live value, explicit
current/latest, router-confirmed, present-year) and only between passages
that both answer strongly; the judged set always includes the top of each
half; near-duplicates that differ in a question term are kept apart. The
result beats every single-signal variant on the metric that matters
(answer@5 0.717 vs 0.683 dense, 0.617 lexical, 0.517 baseline) and costs
~0.5 s p50 (embedding + two scans + a 12-passage rerank).

The dense half's own gain (answer@5 0.600 → 0.683, p95 10 s → 32 ms) is the
Qwen3-Embedding query instruction plus client pooling.

### Forensic reproduction after deployment (`forensic-after.txt`)

| account | effort / search | route | TTFT | decision | answer |
|---|---|---|---|---|---|
| owner | fast / auto | chat, from store | 1.8 s | local | names the CEO, cites the org-chart page |
| member | fast / auto | chat, from store | 0.9 s | local | same evidence, same answer, same citation |
| owner | think / auto | chat, **local-first** (search cancelled) | 14.4 s | local | same |
| member | think / auto | chat, local-first | 7.6 s | local | same |
| owner / member | fast / on | live search + 3 stored passages | 7.6 / 4.8 s | — | same, cited |

Both accounts now receive identical evidence on every route; the Fast
answer cites the page that states the fact; Think no longer spends 17–19 s
on a live search the store answers (its remaining latency is the model's
own reasoning pass).

## 5. Load (`scratchpad/load.py`, Fast, unique corpus-derived questions)

First deployment (reranker: 4 slots, 2 reserved, 0.25 s Fast wait):

| concurrency | wall | TTFT p50 / p95 | total p50 / p95 | decisions | reranker queue peak |
|---|---|---|---|---|---|
| 1 | 10.4 s | 1.5 / 1.5 s | 10.4 / 10.4 s | topical 1 | 0 |
| 5 | 11.6 s | 3.7 / 9.5 s | 6.6 / 11.6 s | local 2, topical 2, lookup 1 | 1 |
| 10 | 19.9 s | 6.5 / 8.1 s | 14.0 / 19.9 s | local 3, topical 4, lookup 1, model 2 | 31 |
| 25 | 29.2 s | 12.2 / 14.6 s | 19.1 / 28.1 s | local 5, **degraded_busy 16**, stale 2, model 2 | 29 |

Reading: to 10 concurrent users the pipeline judges every answer; at 25 the
0.25 s Fast wait for a reranker slot expired for 16 of 25 requests and they
answered from the labelled floor — safely, but unjudged — while the main
model's own prefill queue put the first token ~12 s out regardless. The
reranker is a 0.6B model that batches sequences, so the app-side bound was
the bottleneck, not the GPU: the defaults moved to 8 slots (4 reserved) and
a 1 s Fast wait (2 s Think), invisible next to the model's queue at that
load. The embedding service never queued (query LRU + bounded batches).

Modelled beyond 25: the main model's prefill is the limit. Each Fast turn
carries ~1–1.5k tokens of grounding on top of the prompt; at the measured
~12 s p50 TTFT for 25 simultaneous arrivals, 50 → ~25 s, 100 → ~50 s,
250 → minutes — a queueing problem for the model, not for retrieval, whose
per-request cost stayed ~0.5 s. Serving 100+ simultaneous first tokens in
seconds would need `MAIN_MODEL_MAX_NUM_SEQS` tuning and load shedding on
the grounding budget (skip topical grounding and recall under pressure),
which is recorded as the next optimisation rather than done blind.

## 2. Vector index benchmark (`scratchpad/ann-bench.py`, on a COPY)

90k-row Salesforce table, 1024-d, 40 perturbed stored vectors as queries,
recall against the flat scan:

| index | build | p50 | p95 | recall@10 |
|---|---|---|---|---|
| flat scan | — | ~150 ms | — | 1.000 |
| IVF_PQ (256 × 64 sub-vectors), 20 probes | 26.5 s | 9.2 ms | 11.2 ms | 0.810 |
| IVF_FLAT (256), 8 probes | 8.9 s | 7.1 ms | 12.6 ms | 0.960 |
| IVF_FLAT (256), 20 probes | 8.9 s | 7.2 ms | 10.5 ms | 0.982 |
| **IVF_FLAT (256), 50 probes** | 8.9 s | 9.4 ms | 11.7 ms | **0.995** |
| IVF_HNSW_SQ, any probes | 7.3 s | 5.7–6.6 ms | 7.4–8.5 ms | 0.882 |

Decision (D8): IVF_FLAT, sqrt(rows) partitions clamped to [32, 1024],
50 probes, refine factor 2, above 50k rows. Below that the flat scan
(19 ms at 9k rows) is not worth an index that must be rebuilt as rows are
appended.

## 3. Reranker prompt (`scratchpad/rerank-sanity.py`)

Six controlled passages, one office-holder question:

| passage | raw `/score` | templated |
|---|---|---|
| org chart naming the person | 0.890 | 0.9997 |
| register listing the board | 0.345 | 0.9947 |
| company social post | 0.487 | 0.648 |
| careers page | 0.702 | 0.088 |
| "CEO" encyclopaedia article | 0.269 | 0.004 |
| placement PDF | 0.477 | 0.0000 |

Latency (live service, `/score`): 8 passages 82 ms, 15 → 105 ms, 30 → 172 ms.

## 4. Forensic reproduction (`scratchpad/forensic.py`, `ab-recall.py`)

Before: same question, two accounts, Fast/auto → identical two non-answering
sources; owner "names the CEO" (3/3 with the recalled earlier answer, 0/3
without), member "not in the sources". Think/auto 17.5–18.9 s from live
results only.

After: re-run after deployment; recorded in the ADR §9.

## 5. Load (filled in after deployment)

`scratchpad/load.py`: unique questions (cache-neutral), concurrency 1/5/10/25,
orchestrator-side TTFT p50/p95 per route, plus `vllm:num_requests_waiting`
for the main, embedding and reranker services. 50/100/250 are modelled from
the measured per-stage costs with the sidecar concurrency (4 each), the
thread pool (40) and the DB pool (16) as the queueing points.
