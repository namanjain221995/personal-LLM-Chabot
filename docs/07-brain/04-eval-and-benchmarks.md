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

### After (filled in after deployment)

See §9 of the ADR; the table is appended by the post-deploy run.

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
