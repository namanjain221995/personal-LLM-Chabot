# Context capacity and capability registry (draft)

Phase A task A-05. Drafted 2026-10-03 (about 08:00 UTC) from read-only runtime metadata and configuration.

**Status: DRAFT, METADATA-BASED.** Every number below is a *configured or reported* value, or arithmetic on such values. None of it is a verified capability (§2: configured limit ≠ verified capability). No request that generates tokens was sent. Long-context verification (prefill timing at 128K–1M, recall, synthesis, generation room near the limit) is Phase E work and is `TEST_NOT_RUN` here.

Endpoints are named by role. Addresses, host names and ports are host-only (`~/.llm-autopilot/host.json`).

---

## 1. Evidence: what was run

All commands were read-only. `$MAIN`, `$ROUTER`, `$EMBED`, `$RERANK`, `$OCR` and `$WHISPER` stand for each role's base URL from the host-only configuration.

| # | Command (shape) | What it gave |
|---|---|---|
| E1 | `curl -s $X/version` and `curl -s $X/v1/models` on each vLLM endpoint | vLLM build, served model id, checkpoint directory (with revision prefix), `max_model_len` as served |
| E2 | `curl -s -X POST $X/tokenize -d '{"model":…,"messages":[{"role":"user","content":"x"}]}'` (main, router, OCR) and `{"prompt":"hello world"}` (embed, reranker): five calls in total | Whether `/tokenize` returns `max_model_len`, which `orchestrator/app/context.py` (`count_tokens`, `model_window`) uses as W |
| E3 | `curl -s $X/metrics` filtered to `vllm:cache_config_info` and three request gauges | KV dtype, block size, number of GPU blocks, KV pool tokens, max concurrency at full length, prefix caching, GDN/mamba state dtypes |
| E4 | `docker inspect --format '{{json .Args}}' <engine>` piped through a filter that drops the value after any flag whose name contains key, token, secret or password (integer values of vLLM sizing flags such as `--max-num-batched-tokens` were kept) | Launch flags |
| E5 | `docker logs <engine>` piped through `grep` for KV-cache, model-loading and CUDA-graph lines only (worker rank 1 of the main engine; the router) | Weights, KV reservation and CUDA-graph memory per rank; router KV pool. The head rank's start-up lines have rotated out of its log. |
| E6 | `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader` on both nodes; `free -g` | GPU memory per engine process; node memory |
| E7 | Prometheus instant queries over 7 days: `max_over_time` and `quantile_over_time(0.99, …)` of `vllm:kv_cache_usage_perc`; `max_over_time` of `vllm:num_requests_running` and `vllm:num_requests_waiting_by_reason` | Observed KV use and concurrency |
| E8 | `curl -s <orchestrator>/health` (the `context` block and `checks.vllm.engine.admission`) | The window the orchestrator believes, its budget numbers, the admission lanes and the KV ledger |
| E9 | Read `config.json`, `generation_config.json`, `tokenizer_config.json`, `chat_template.jinja` and the download record `.complete.json` in each served model's directory (no weights) | Geometry, sampling defaults, chat template, revision |
| E10 | Code read: `orchestrator/app/context.py`, `app/config.py`, `app/kv_budget.py`, `app/engines/ocr.py`, `app/continuation.py`, `app/publicapi/registry.py`, `launcher/techsara_cli/modelshape.py`, `config/model-manifest.yaml`, `compose/whisper/server.py` | How the application consumes these limits |

The arithmetic in §4 was run as a Python script (formulas shown inline).

---

## 2. Registry: one row per endpoint

| Role | Node | Served model (revision) | vLLM build | Served W (`/v1/models`) | `/tokenize` returns W | Single-call output cap at the engine | Reasoning / tool parser | KV dtype | KV pool (reported) |
|---|---|---|---|---|---|---|---|---|---|
| **main** (chat, Think/Max, `/v1`; vision by code default) | both: TP=2, rank 0 on the head, rank 1 on the worker | `nvidia/Qwen3.6-35B-A3B-NVFP4` (`491c2f1e`) | 0.28.1rc1.dev580+g385dce36b | **1,000,000** | yes, 1,000,000 | W − P (no `max_new_tokens` in `generation_config.json`) | `qwen3` / `qwen3_xml`, auto tool choice on | fp8 | 8 GiB per rank → 800 blocks × 2,096 tokens; reported 1,663,201 tokens; 1.66× at 1M |
| **router** (intent and route calls; agent sub-steps by code default) | head | `Qwen/Qwen3-VL-8B-Instruct-FP8` (`9cdc6310`) | 0.26.1rc1.dev77+g6f91edf96 | **49,152** | yes, 49,152 | W − P | none / none | fp8 | 3.61 GiB → 3,286 blocks × 16 = 52,576 tokens; 1.07× at 49,152 |
| **embed** | head | `Qwen/Qwen3-Embedding-0.6B` (`97b0c614`) | 0.20.1+7124b12a.dev (NGC image) | **4,096** | yes, 4,096 | n/a (pooling runner) | n/a | auto (bf16) | 2 GiB → 1,170 blocks × 16 = 18,720 tokens |
| **reranker** | head | `Qwen/Qwen3-Reranker-0.6B` (`e61197ed`) | 0.20.1+7124b12a.dev (NGC image) | **4,096** | yes, 4,096 | n/a (pooling runner, classify) | n/a | auto (bf16) | 2 GiB → 1,170 blocks × 16 = 18,720 tokens |
| **ocr** | worker | `baidu/Unlimited-OCR` (`07dea832`) | 0.26.1rc1.dev77+g6f91edf96 | **8,192** | yes, 8,192 | W − P (no `generation_config.json`) | none / none | auto (bf16) | 3 GiB → 3,276 blocks × 16 = 52,416 tokens; 6.4× at 8,192 |
| **whisper** (two replicas: head and worker) | head, worker | `openai/whisper-large-v3` (`06f233fe`) | not vLLM (own FastAPI server) | not a token window: 30 s audio windows, 448 decoder tokens each | n/a | 448 tokens per 30 s window | n/a | n/a | n/a |

Evidence per row: E1, E2, E3, E4, E9 for every vLLM endpoint; whisper from its `/health` (model, revision, `long_form: sequential`) and its `config.json` (`max_target_positions` 448).

The roles "vision" and "agent" are not separate engines. By code default `VISION_BASE_URL`/`VISION_MODEL` point at the main model and `AGENT_BASE_URL`/`AGENT_MODEL` at the router (`app/config.py:182-200`). The live values come from environment files, which this task does not read.

### 2.1 Launch flags that matter

From E4. A blank cell means the flag is absent, so the vLLM default applies.

| Flag | main | router | embed | reranker | ocr |
|---|---|---|---|---|---|
| `--tensor-parallel-size` / `--pipeline-parallel-size` | 2 / 1, `--nnodes 2`, `mp` executor, rank 1 `--headless` | | | | |
| `--max-model-len` | 1000000 | 49152 | 4096 | 4096 | 8192 |
| `--hf-overrides` | `text_config.rope_parameters`: `rope_type yarn`, `factor 3.82`, `original_max_position_embeddings 262144`, `rope_theta 1e7`, `partial_rotary_factor 0.25`, `mrope_section [11,11,10]`, `mrope_interleaved` | | | `Qwen3ForSequenceClassification`, `classifier_from_token [no, yes]` | |
| `--gpu-memory-utilization` | 0.30 (not used for KV sizing: see note) | 0.15 | 0.04 | 0.04 | 0.10 |
| `--kv-cache-memory-bytes` | 8589934592 (8 GiB per rank) | | 2147483648 | 2147483648 | 3221225472 |
| `--kv-cache-dtype` | fp8 | fp8 | | | |
| `--max-num-seqs` | default (value not visible: see §7) | default | default | default | 8 |
| `--max-num-batched-tokens` | 8192 | default | default | default | default |
| `--enable-chunked-prefill` | on | | | | |
| prefix caching | **off** (`--no-enable-prefix-caching`) | on (default) | on (default) | on (default) | off (reported) |
| speculative / MTP | **none** (the checkpoint has one MTP layer; not enabled) | | | | |
| `--reasoning-parser` | qwen3 | | | | |
| `--tool-call-parser` / `--enable-auto-tool-choice` | qwen3_xml / on | | | | |
| `--chat-template` | none (the checkpoint's template) | none | | | none |
| `--quantization` | modelopt (NVFP4) | (FP8 checkpoint) | | | |
| `--attention-backend` / `--gdn-prefill-backend` | flashinfer / flashinfer | | | | |
| other | `--trust-remote-code`, `--distributed-timeout-seconds 300` | | `--runner pooling` | `--runner pooling --convert classify` | `--enforce-eager`, `--trust-remote-code` |

Note on the main engine. Rank 1's log says: "reserved 8.0 GiB memory for KV Cache as specified by kv_cache_memory_bytes config and skipped memory profiling. This does not respect the gpu_memory_utilization config." So the pool is fixed at 8 GiB per rank, whatever the utilisation fraction.

### 2.2 Generation defaults and chat templates

- **main.** `generation_config.json`: `temperature 1.0`, `top_k 20`, `top_p 0.95`, no `max_new_tokens`. With vLLM's default `--generation-config auto` these become the default sampling parameters. That is inferred: the head's start-up line confirming it has rotated out, but the router logs the same mechanism. There is no hidden output cap. A request that names no `max_tokens` may run to W − P. The chat template is the checkpoint's `chat_template.jinja` (no override). It has `enable_thinking` and `preserve_thinking` switches, emits `<think>` blocks and `reasoning_content`, and raises `System message must be at the beginning.` when a second system message appears; `count_tokens` folds system messages first for that reason. `tokenizer_config.json` says `model_max_length 262144`. That is tokenizer metadata, not the served limit.
- **router.** The start-up log confirms the override: "Default vLLM sampling parameters have been overridden by the model's `generation_config.json`: `{'repetition_penalty': 1.0, 'temperature': 0.7, 'top_k': 20, 'top_p': 0.8}`". It is an Instruct model and has no reasoning parser.
- **embed / reranker.** Pooling runners. The `generation_config.json` values (embed `max_new_tokens 2048`; reranker sampling values) do not apply.
- **ocr.** No `generation_config.json`. A one-word chat message tokenizes to 2 tokens, so the chat template adds no role wrapping; the application sets the prompt format.

---

## 3. What the application does with these limits

From code (E10), not from tests.

- **W for fitting** comes from `/tokenize` → `max_model_len`, cached per base URL (`app/context.py`, `count_tokens` / `model_window`). All five vLLM endpoints return it (E2). The orchestrator's `/health` reports `configured_max_model_len 1,000,000` and `served_max_model_len 1,000,000` (E8).
- **Budget block in `/health`** (E8): `reserved_output_default 8,192`, `reserved_output_high 16,384`, `safety_margin 8,192`, `max_input_tokens 975,424` (= 1,000,000 − 16,384 − 8,192). The per-call fit (`fit_request`) uses a different margin, `CONTEXT_SAFETY_MARGIN` 512 (`app/config.py:1032`). See §7.
- **Admission** (E8, `app/kv_budget.py`, `app/admission.py`): NORMAL lane capacity 10; LONG lane (above 131,072 prompt tokens) capacity **1**; long-output lane capacity 2. The KV ledger takes the pool as 1,663,201 tokens (source "setting"; it reads the engine's `/metrics` live only when a long request's decision depends on it). Reserve 0.35 gives a budget of 1,081,080 tokens; headroom 0.15 gives a managed limit of 1,413,720 tokens. The charge per sequence is block-exact: `(3 + ceil(min(P + max_tokens, W) / 2096)) × 2096`.

---

## 4. §18.4 capacity math

### 4.1 Main model geometry (`config.json`, `text_config`)

| Field | Value |
|---|---|
| architecture | `Qwen3_5MoeForConditionalGeneration` (`qwen3_5_moe`), with a 27-layer vision encoder |
| decoder layers | 40: **10 `full_attention`** (every 4th, `full_attention_interval 4`) and **30 `linear_attention`** (Gated DeltaNet) |
| attention heads / KV heads / head dim | 16 / **2** / **256** |
| linear attention | `linear_num_key_heads 16`, `linear_num_value_heads 32`, key and value head dim 128, conv kernel 4, `mamba_ssm_dtype float32` |
| experts | 256 routed, 8 per token, plus a shared expert |
| native positions | `max_position_embeddings 262144`; `rope_type default`, `rope_theta 1e7`, `partial_rotary_factor 0.25`, interleaved M-RoPE `[11,11,10]` |
| MTP | `mtp_num_hidden_layers 1` (present in the checkpoint; not enabled at serve time) |

### 4.2 KV bytes per token (full-attention layers only)

```
KV bytes/token = 2 × full_layers × KV_heads × head_dim × bytes_per_element
whole model    = 2 × 10 × 2 × 256 × 1 (fp8) = 10,240 B
per rank, TP=2 = 2 × 10 × 1 × 256 × 1       =  5,120 B   (matches launcher/techsara_cli/modelshape.py)
```

### 4.3 Fixed per-sequence state of the 30 GDN layers

```
SSM state / layer  = value_heads × key_dim × value_dim × 4 B (fp32) = 32 × 128 × 128 × 4 = 2,097,152 B
conv state / layer = (2 × key_heads × key_dim + value_heads × value_dim) × (kernel − 1) × 2 B (bf16)
                   = (2×16×128 + 32×128) × 3 × 2 = 8,192 × 3 × 2 = 49,152 B
per layer          = 2,146,304 B whole, 1,073,152 B per rank
30 layers          = 61.4 MiB per sequence whole, 30.7 MiB per rank, independent of sequence length
```

### 4.4 How vLLM lays out the pool (hybrid allocator)

- vLLM sets the attention block to **2,096 tokens** so that one attention page equals one GDN state page. Rank 1's log: "Setting attention block size to 2096 tokens to ensure that attention page size is >= mamba page size". Per layer and rank: `2096 × 512 B = 1,073,152 B`, which is exactly the GDN page in §4.3.
- The 40 layers form four groups of 10: three GDN groups and one attention group. The orchestrator's notes record the engine log as `kv cache group sizes [1000000, 1000000, 1000000, 2096]` (`app/kv_budget.py`, 2026-09-13). One block spans 10 layers: `10 × 1,073,152 = 10,731,520 B` per rank.
- Pool: `8,589,934,592 / 10,731,520 = 800.44` → **800 blocks** (the engine reports `num_gpu_blocks 800`). vLLM keeps one null block, so **799 are usable**.
- A sequence of L tokens holds `3 + ceil(L / 2096)` blocks: one block per GDN group (`mamba_cache_mode none`), plus the attention blocks.

### 4.5 Resident tokens and concurrency (main)

```
1M sequence           = 3 + ceil(1,000,000 / 2096) = 481 blocks  (4.81 GiB per rank of the 8 GiB pool)
vLLM's own report     = 800 / 481 = 1.6632× at max_model_len → kv_cache_size_tokens 1,663,201
longest one sequence  = (799 − 3) × 2096 = 1,668,416 tokens (above W, so W binds)
left beside one 1M    = 799 − 481 = 318 blocks
two 1M sequences      = 962 blocks > 799 → never coexist
```

| Sequence length (P + G) | Blocks each | Fit alone (799 blocks) | Fit beside one running 1M sequence (318 blocks) |
|---|---|---|---|
| 2,096 | 4 | 199 | 79 |
| 8,192 | 7 | 114 | 45 |
| 32,768 | 19 | 42 | 16 |
| 131,072 | 66 | 12 | 4 |
| 262,144 | 129 | 6 | 2 |
| 500,000 | 242 | 3 | 1 |
| 1,000,000 | 481 | 1 | 0 |

Observed load, 7 days (E7), current model only: peak `kv_cache_usage_perc` **0.548** (about 438 of 799 blocks), p99 **0.049**; peak running requests **22**; peak waiting with `reason="capacity"` **11** (cause not determined here).

### 4.6 Available KV memory per node (§18.4 formula)

On these unified-memory nodes the pool is not whatever is left. It is an explicit 8 GiB per rank (`--kv-cache-memory-bytes`), and that bypasses memory profiling. The per-rank budget of the main engine (E5, E6):

```
process GPU memory (rank 0 and rank 1, each)   25,506 MiB = 24.91 GiB
  weights (rank 1 log: "Model loading took")      10.66 GiB
  KV pool (fixed)                                  8.00 GiB
  CUDA graphs (rank 1 log)                         2.67 GiB
  remainder: activations, NCCL, allocator          3.58 GiB
```

Node context at the time of reading (`free -g`): head 121 GiB total, 73 used, 48 available; worker 121 total, 57 used, 63 available. Other GPU processes on the head: router about 16.5 GiB, embed and reranker about 3.9 GiB each, whisper about 4.9 GiB. On the worker: OCR about 13.3 GiB and whisper about 5.5 GiB.

Constraint: tensor parallelism needs the same pool on both ranks, so the pool cannot grow without more head memory. The owner's standing rule is that nothing new uses head memory. The launcher's own record (`docs/CLUSTER.md`, engine tuning table) says a 16 GiB pool exhausted unified memory during a ~950K prefill, which is why 8 GiB was chosen. That record predates the current vLLM build.

### 4.7 Long-context method (§18.4 item 2)

- `factor = ceil(1,000,000 / 262,144 × 100) / 100 = 3.82`, and `3.82 × 262,144 = 1,001,390 ≥ 1,000,000`. This is `yarn_factor()` in `modelshape.py`, capped at 4.0.
- The local model card in the checkpoint directory is NVIDIA's quantized card. It states "Context length up to 262K" and gives no long-context or YaRN instructions. The 1M window is a deployment choice beyond the card's stated context.
- vLLM applies this YaRN override statically to every request, short ones included. Its effect on short-prompt quality has not been measured in this programme (A/B required by §18.4 item 2).

### 4.8 Sidecar endpoints

| Role | KV bytes per token | Pool | Full-window sequences |
|---|---|---|---|
| router | `2 × 36 layers × 8 KV heads × 128 × 1 (fp8) = 73,728 B` | 52,576 tokens = 3.61 GiB (log: "Available KV cache memory: 3.61 GiB", "GPU KV cache size: 52,576 tokens") | 1.07 at 49,152 |
| embed, reranker | `2 × 28 × 8 × 128 × 2 (bf16) = 114,688 B` | `2 GiB / 114,688 = 18,725` → 18,720 tokens | 4.57 at 4,096 |
| ocr | `2 × 12 × 10 × 128 × 2 (bf16) = 61,440 B` | `3 GiB / 61,440 = 52,428.8` → 52,416 tokens | 6.40 at 8,192; `--max-num-seqs 8` |

---

## 5. The three §20 numbers per endpoint

A = maximum single-call output; B = maximum combined context (W); C = maximum cumulative document output across many calls. A is never C.

| Role | A: single-call output (configured) | B: W (served) | C: cumulative (configured) |
|---|---|---|---|
| main | Engine: W − P (no hidden cap). Application caps per call: chat ceiling `MODEL_MAX_OUTPUT` 8,192 by default and 16,384 at high effort; Fast segment 8,000; with thinking on, every Think/Max call asks for at least `MAX_OUTPUT_TOKENS` 65,536, shared by reasoning and answer; always `min(ceiling, W − P − 512)`. `/v1`: `PUBLIC_API_MAX_OUTPUT_TOKENS` 1,000,000 by code default, narrowed by W, default 8,192. A single call is also bounded by `GEN_WALL_CLOCK_S` (code default 1,800 s) × decode speed. | 1,000,000 | Partly present, as **chat and `/v1` continuation** (`app/continuation.py`): `MAX_LOGICAL_OUTPUT_TOKENS` and `CONTINUATION_BUDGET_*` 1,000,000, at most 400 segments, a 21,600 s deadline, each segment re-prompted with the request, a 6,000-character tail and the headings written so far. This is **not** the §20 durable document job (saved outline and constraints, persisted sections, resume after a worker kill, cross-section checks). That job is not implemented. |
| router | Engine: 49,152 − P. Application: 40 (auto-plan), 4 (freshness), 200/50 (route), input clipped to 2,000 characters; agent sub-steps use this endpoint by code default. | 49,152 | none |
| embed | n/a | 4,096 | n/a |
| reranker | n/a | 4,096 | n/a |
| ocr | Engine: 8,192 − P. Application: `min(6,000, 8,192 − 2,200) = 5,992` per image (`app/engines/ocr.py:output_limit`) | 8,192 | n/a (one call per page) |
| whisper | 448 tokens per 30 s window; per request at most `WHISPER_MAX_AUDIO_SECONDS` (code default 600 s) | n/a | n/a (sequential long-form within one request) |

Values marked "code default" may be overridden in environment files, which this task did not read.

---

## 6. Verdicts (§18.4), METADATA-BASED

These verdicts rest only on served and configured values and arithmetic. None is a verified capability. Each needs Phase E measurement before it can be advertised.

| Role | Verdict | Numbers and constraints |
|---|---|---|
| **main, up to the native 262,144** | supported-with-constraints (metadata) | W 1,000,000 covers it; the pool holds 6 sequences of 262,144 at once (2 beside a running 1M job). Constraint: static YaRN ×3.82 applies to these requests too, and its short-prompt quality effect is unmeasured. |
| **main, ~1M** | supported-with-constraints (metadata) | Served W 1,000,000; the pool fits **exactly one** ~1M sequence (481 of 799 blocks) with 318 blocks left for everything else (for example 45 sequences of 8K or 79 of 2K). A second long sequence cannot coexist; the orchestrator's LONG lane is already 1. W − P bounds the output, so a 990K prompt leaves about 10K. On an earlier build a 949,915-token request completed in 878 s with needles 3/3 (`docs/CLUSTER.md`, CHANGELOG). That build had a different pool (1,494,824 tokens vs 1,663,201 today) and a different vLLM version, so the result does not carry over. The window is beyond the checkpoint card's stated 262K. |
| **router** | supported-with-constraints (metadata) for its short-call role; ~1M **unsupported** | W 49,152; the pool holds 1.07 full-window sequences, so long agent sub-steps queue behind each other. |
| **embed** | supported for chunked inputs ≤ 4,096 tokens (metadata); ~1M **unsupported** (not applicable to a retrieval model) | Native 32,768 but served 4,096. |
| **reranker** | supported for query + passage ≤ 4,096 tokens (metadata); ~1M **unsupported** | Native 40,960 but served 4,096. |
| **ocr** | supported per page ≤ 8,192 tokens (metadata); ~1M **unsupported** | 5,992-token output cap per image; 8 sequences at most. |
| **whisper** | not a context-window endpoint | 30 s windows, sequential long form; 600 s per request by code default. |

---

## 7. Discrepancies between configured and served values

Found during this draft. Not fixed here: the task is read-only and this file is the only one in scope.

1. **The manifest's `context_limit` differs from the served W** (`config/model-manifest.yaml`). main: 262,144 vs 1,000,000 served (an explicit `MAIN_MODEL_MAX_LEN` is not clamped); router: 65,536 vs 49,152; embed: 32,768 vs 4,096; reranker: 32,768 vs 4,096 (its `config.json` says 40,960). OCR matches at 8,192.
2. **The router's tool-calling claim.** The manifest says `supports_tool_calling: true` for the router model, but the router runs without `--enable-auto-tool-choice` or `--tool-call-parser`. `tool_choice: "auto"` requests to it would be refused. That follows from how vLLM behaves and was not tested.
3. **Two margins for one W.** `/health` budgets `1,000,000 − 16,384 − 8,192 = 975,424` input tokens (`MAIN_MODEL_CONTEXT_SAFETY_MARGIN` 8,192). `fit_request` uses `CONTEXT_SAFETY_MARGIN` 512 and a different output ceiling.
4. **`GEN_WALL_CLOCK_S`.** The code default is 1,800 s (`app/config.py:969`). The long-form comment in the same file says 4,200 s ("bounds any SINGLE call at ~190,000"). The live value was not read.
5. **The window fallback is not per endpoint.** When `/tokenize` fails for an endpoint, `model_window` falls back to `settings.model_max_context` (the main model's configured window) for any base URL, including the router (49,152) and OCR (8,192). Code read only.
6. **The launcher's `HYBRID_KV_USABLE_FRACTION` 0.88** was measured on the earlier 27B model. On the current model the reported pool is `1,663,201 / (8 GiB / 5,120 = 1,677,721) = 99.1 %` of the naive arithmetic. The constant errs on the safe side (it can refuse a window the engine would hold).
7. **`tokenizer_config.json` `model_max_length` 262,144** on the main checkpoint. Any client-side Hugging Face tokenizer use would warn or truncate at 262K. Whether any application path does so was not checked.

---

## 8. Open questions (could not be determined read-only)

1. **Main engine `max_num_seqs`.** The flag is not passed, so the vLLM default for this build and device applies. The head rank's start-up configuration lines have rotated out of its log, and `/metrics` does not expose it.
2. **The head rank's own start-up KV lines** ("GPU KV cache size", "Maximum concurrency") have rotated out. The pool was taken from `vllm:cache_config_info` instead, and the two agree arithmetically (§4.5).
3. **Upstream long-context guidance for Qwen3.6-35B-A3B** (YaRN factor and limits). The local card is NVIDIA's and covers only 262K. Reading the upstream card needs network access (Phase E).
4. **Live environment values**, not read under the secrets rule: `GEN_WALL_CLOCK_S`, `PUBLIC_API_MAX_OUTPUT_TOKENS`, `MAX_LOGICAL_OUTPUT_TOKENS`, `CONTINUATION_*`, `WHISPER_MAX_AUDIO_SECONDS`, `VISION_BASE_URL`, `AGENT_BASE_URL`. The orchestrator `/health` exposes only the context block. A non-secret settings dump endpoint would close this.
5. **Prefill and decode speed on the current build** at 128K, 256K, 512K and 1M: Phase E (dev resources or an off-peak window; never on production without the operator, per operator decision 3).
6. **What drove the 7-day peaks**: KV use 0.548, and 11 requests waiting for capacity.
7. **Image tokens.** `count_tokens` falls back to a character estimate for multimodal payloads (`app/context.py`), so W − P for vision turns uses an estimated P. The size of that error is unmeasured.

---

## 9. Prior records kept from the earlier version of this file

| Item | Prior record | Source | Status after this draft |
|---|---|---|---|
| Served `max_model_len` of the main model | 1,000,000 (YaRN 3.82) | `GET /v1/models` | Re-observed 2026-10-03 (E1, E2, E8). Served, not verified. |
| Needle recall at 949,915 tokens | 3 of 3, 878 s, KV 8 GiB | `docs/CLUSTER.md`, CHANGELOG | Historical, on an earlier build with a 1,494,824-token pool. Not re-run (`TEST_NOT_RUN`). |
| KV bytes per token (35B-A3B, TP=2, fp8) | 5,120 | `modelshape.py` | Re-derived from `config.json` (§4.2). |
| KV pool | 1,663,201 tokens, prefix caching off | README | Re-observed in `vllm:cache_config_info` and re-derived (§4.4–4.5). |
| Probe script | `orchestrator/scripts/validate_long_context.py` (defaults to the production engine; sizes 65,536–240,000 unless `--sizes`) | repository | Not run (it generates tokens). Phase E must point it at a dev engine. |

The ~250K symptom (§18.3) and the ~1M path (§18.4) are worked in Phases C and E with the budget law P + G + M ≤ W. Production inference changes are prepared and tested on dev resources and handed to the operator (operator decision 3).
