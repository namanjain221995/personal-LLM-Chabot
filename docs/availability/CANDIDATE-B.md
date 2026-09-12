# Candidate B — `vllm/vllm-openai` nightly-385dce36 with the FlashInfer SM120 GDN prefill kernel

Workstream: harness/candidate (CONTRACT §9 row *candidate*). Written 2026-09-12 from
facts verified **inside the candidate image on this head** (`docker run --rm` of throwaway
containers; nothing in production was restarted, stopped or reconfigured), from the running
production ranks (logs, `docker inspect`, `/proc`), from two throwaway GPU probes on the
**worker** (the head could not open a new CUDA context — §8.1), and from the two new tools
this workstream owns: `scripts/cluster-ab.py` (the A/B harness) and `scripts/cluster-cpu.sh`
(CPU topology, per-thread measurement, cpuset proposal, GPUDirect RDMA evidence).
Anything not verified is marked **unverified**.

The candidate is the *Track B* of `VLLM-UPGRADE-RESEARCH.md` §6.1: it moves only one kernel of
the implicated family (`chunk_gated_delta_rule`, the chunked GDN recurrence in prefill) off
Triton onto FlashInfer's SM120 CuTe-DSL kernel. It addresses one hypothesis, not the fault
class; it is an experiment, gated by the pass criteria in §7, run under the contract's controller
and one-model mode exactly as the pinned build is.

## 1. Identity (verified inside the image)

| item | value | how verified |
|---|---|---|
| manifest-list digest | `vllm/vllm-openai@sha256:819ec9c063412e5730d1b0e82046ba540d1bf991f3c4f661a849aae8a0c52374` | `docker image inspect` on the head and on the worker: **same image ID `sha256:5a0f8b914da56ea2ae2dbe569e4219a14605ce798e7eff12ae6db63328adc4f1`** on both nodes, created `2026-09-09T05:34:01Z`, `linux/arm64`, 22,172,559,299 bytes |
| arm64 image digest | `sha256:b0501f99fec5136f248f78d5850977a2ec32d55cd9a665f4a9ffef24cbdf7fe5` | from the research document (Docker Hub API); **unverified locally** — `docker image inspect` reports `RepoDigests: [vllm/vllm-openai@sha256:819ec9c0…]` only |
| tag | `nightly-385dce36bcee42309924a5ece951a96db3dce7f2` | labels `ai.vllm.image.tag`, `org.opencontainers.image.version`; env `VLLM_IMAGE_TAG` |
| build commit | `385dce36bcee42309924a5ece951a96db3dce7f2` | label `ai.vllm.build.commit`, `org.opencontainers.image.revision`, env `VLLM_BUILD_COMMIT`; build `https://buildkite.com/vllm/release-v2/builds/6303` |
| contains #55715 (f6326f5) | **yes** — `vllm/model_executor/layers/mamba/gdn/qwen_gdn_linear_attn.py` (77,101 bytes, dated Sep 9 05:33) carries `_resolve_gdn_prefill_backend` with the `is_device_capability_family(120)` branch (§2) | `grep -n` inside the image; the image is 51 commits after f6326f5 per the research doc (**the commit-ancestry itself is unverified locally** — no git metadata in the image; the code path is the proof) |
| vLLM version | `0.28.1rc1.dev580+g385dce36b` (`vllm/_version.py`: `__commit_id__ = 'g385dce36b'`) — note: **not** a 0.29 version string; main's version base stayed at 0.28.1rc1 after the v0.29.0 branch cut | `python3 -c 'import vllm; print(vllm.__version__)'` |
| flashinfer-python / -cubin / -jit-cache | `0.6.18` / `0.6.18` / `0.6.18+cu130` | `pip list` |
| torch / triton | `2.13.0+cu130` / `3.7.1` (torchaudio 2.11.0+cu130, torchvision 0.28.0+cu130) | `pip list` |
| CUDA / NCCL / cuDNN | CUDA 13.0.2 (`NV_CUDA_CUDART_VERSION=13.0.96-1`), `nvidia-nccl-cu13 2.30.7` (the *same* NCCL as the running A build: `NCCL version 2.30.7+cuda13.3` in both logs), `nvidia-cudnn-cu13 9.20.0.48`, `cuda-python 13.3.1`, `nvidia-cuda-nvcc 13.3.73` | `pip list`, image env |
| CuTe-DSL (needed by the SM120 GDN kernel) | `nvidia-cutlass-dsl 4.6.2` with `nvidia-cutlass-dsl-libs-cu13 4.6.2` (and `-cu12`) — the cu13 DSL is the one FlashInfer's SM12x PTX check demands (§2.4) | `pip list` |
| transformers / xgrammar | `5.16.1` / `0.2.6` | `pip list` |
| build arch list | `TORCH_CUDA_ARCH_LIST=8.0 8.7 8.9 9.0 10.0 11.0 12.0` (the running A build has the same list); sm_121 runs on 12.0-family binaries plus Triton/FlashInfer JIT | image env |
| rdma-core in the image | `libibverbs1 50.0-2ubuntu0.2`, `ibverbs-providers 50.0-2ubuntu0.2` (Ubuntu 24.04); `libmlx5.so.1.24.50.0`; `ibv_reg_dmabuf_mr` **present**, `mlx5dv_reg_dmabuf_mr` **absent** (A's image: rdma-core `39.0-1`, same symbol picture) | `ctypes.CDLL`, `dpkg-query` inside the image |

Comparison point A (production, pinned): `vllm/vllm-openai@sha256:24f2f8975d011ea7f7066a547886a08a1fd3c4bf0880463487fae4f01ce723c6`, image ID `60d84700e24f…`, build commit `6f91edf96d3f…`, created `2026-07-29T05:47:31Z`, `v0.26.1rc1.dev77+g6f91edf96`, flashinfer 0.6.15.post1.

## 2. The GDN prefill backend, from the source in the image

### 2.1 Resolver (`vllm/model_executor/layers/mamba/gdn/qwen_gdn_linear_attn.py:94-153`)

```python
def _resolve_gdn_prefill_backend(vllm_config) -> tuple[str, Literal["triton", "flashinfer", "cutedsl"]]:
    backend_cfg = additional_config.get("gdn_prefill_backend", "auto") if isinstance(additional_config, dict) else "auto"
    ...
    elif (current_platform.is_device_capability_family(120)
          and head_k_dim == 128
          and current_platform.get_cuda_runtime_major() >= 13):
        # The in-tree CuteDSL kernel targets SM100 only, so it stays off here.
        supports_flashinfer = True
    if backend in ["flashinfer", "auto"] and supports_flashinfer:
        return backend, "flashinfer"
    if backend == "cutedsl" and supports_cutedsl:
        return backend, "cutedsl"
    return backend, "triton"
```

Our checkpoint has `linear_key_head_dim = 128` (`head_k_dim=128` is printed in the A log today)
and the runtime is CUDA 13, so on the GB10 (capability 12.1, family 120) **`auto` already
resolves to `flashinfer` on this build**. Passing the flag explicitly is still the right thing:
it makes the choice visible in the argv of both ranks and in the log line (`requested=flashinfer`),
and it turns a silent fall-back into a warning (`GDN prefill backend 'flashinfer' is selected but
cannot use this kernel on the current platform. Falling back to Triton/FLA.`, line 250).

### 2.2 The CLI flag (`vllm/engine/arg_utils.py:1751-1756` and `:2586`)

```
--gdn-prefill-backend {flashinfer,triton,cutedsl}      default None (= the engine's "auto")
```
`EngineArgs.gdn_prefill_backend` is copied into `additional_config["gdn_prefill_backend"]` (line
2586) — the same key the resolver reads. `cutedsl` is SM100-only and falls back to Triton on the
GB10 with the warning above. The launcher already validates the same three values
(`launcher/techsara_cli/cluster.py: GDN_PREFILL_BACKENDS`, `CLUSTER_GDN_PREFILL_BACKEND`) and
renders the flag into `CLUSTER_ENGINE_ARGS` so both ranks get it byte-identically.

### 2.3 The exact log line (`_log_gdn_backend_decision`, lines 158-186)

```python
logger.info_once("Using %s GDN prefill kernel (requested=%s, head_k_dim=%s).", chosen, requested_backend, head_k_dim)
```
with `chosen ∈ {"FlashInfer", "CuteDSL", "Triton/FLA"}`. Rendered by vLLM's logger it reads, **per rank**:

```
(Worker_TP0 pid=<n>) INFO <mm-dd hh:mm:ss> [qwen_gdn_linear_attn.py:177] Using FlashInfer GDN prefill kernel (requested=flashinfer, head_k_dim=128).
(Worker_TP1 pid=<n>) INFO <mm-dd hh:mm:ss> [qwen_gdn_linear_attn.py:177] Using FlashInfer GDN prefill kernel (requested=flashinfer, head_k_dim=128).
```
(the head's line carries `Worker_TP0`, the worker's `Worker_TP1`; the trailing period is part of
the message; `requested=auto` if the flag is left out). For comparison, the A build prints today,
verified in both container logs:
`(Worker_TP1 pid=167) INFO 09-12 01:22:02 [qwen_gdn_linear_attn.py:150] Using Triton/FLA GDN prefill kernel (requested=auto, head_k_dim=128).`
The JIT warning `FlashInfer GDN prefill is JIT-compiled; first run may take a while. Set
--gdn-prefill-backend triton to skip JIT.` is emitted **only on SM90** (line 180) — on the GB10
there is no warning although the kernel *is* JIT-compiled (§2.5).

### 2.4 What the kernel is, and how it is compiled (FlashInfer 0.6.18 source in the image)

- `fi_chunk_gated_delta_rule` (vLLM, line 189) calls `flashinfer.gdn_prefill.chunk_gated_delta_rule`
  with `g=torch.exp(fi_g)`, fp32 state/gates, int64 `cu_seqlens`. On arch major 12 that API
  dispatches to **two different CuTe-DSL kernel sets** (`flashinfer/gdn_prefill.py:357-438, 519-529`):
  - **CP ("context-parallel") path** `cp_delta_rule_dsl_sm120` (`gdn_kernels/delta_rule_dsl/delta_rule_cp_sm120.py`,
    four kernels: T-precompute, MN-precompute, fixup, prefill) when `use_cp="auto"` and
    `should_use_cp_host(num_seqs × num_sab_heads, sm_count, device_name)` is true. For the GB10
    (`"NVIDIA GB10"` matches none of `geforce|rtx|workstation` → the HBM threshold 1/2; 48 SMs;
    `num_sab_heads = 32 value heads / TP 2 = 16`) the rule is `num_seqs × 16 × 2 < 48`, i.e.
    **CP is used exactly when ONE prefill sequence is in the chunk** (a lone long prefill), the
    fully-fused non-CP kernel `_FullyFusedDeltaRuleSm120` (`delta_rule_sm120.py:2119`) when
    **two or more prefill sequences share a step** (the mixed-batch shape of the incident).
  - Both are compiled with `cached_compile` (`delta_rule_dsl/custom_compile_cache.py`): an
    **in-process dict** (`_in_mem_compile_cache`) in front of `cute.compile[...]`, with an SM12x-only
    PTX check that raises `SM12x GDN kernel compilation produced unsupported cluster-scoped TMA loads.
    Install the CUDA 13 CUTLASS DSL compiler ('nvidia-cutlass-dsl[cu13]')` — the `-cu13` DSL libs
    are installed (§1), and the check **passed on this GB10** (§2.5). Compile keys are the kernel's
    dtype/tile/stage attributes (`manual_cache_key(...)`), **not** the sequence length or the chunk
    length, so one compile per (variant, dtype) covers every prompt length.
  - This path does **not** go through FlashInfer's locked `JitSpec.build_and_load()`
    (`flashinfer/jit/core.py:300-318`, `FileLock(self.lock_path)` under `~/.cache/flashinfer/<ver>/<archs>/cached_ops/tmp/`) —
    that lock protects the nvcc/ninja modules (and `JitSpecCuteDsl` users such as the b12x MoE), not the GDN prefill kernel.
- **JIT caches on disk.** FlashInfer: `FLASHINFER_WORKSPACE_BASE` (default `$HOME` → `/root`) →
  `~/.cache/flashinfer/0.6.18/<archs>/{cached_ops,generated}` (`flashinfer/jit/env.py:60-176`);
  prebuilt cubins come from `flashinfer-cubin`, prebuilt JIT modules from `flashinfer-jit-cache`
  (version-checked against `flashinfer-python`). The CuTe-DSL compiler has its own cache,
  `CUTE_DSL_CACHE_DIR` (default `$TMPDIR/<user>/cutlass_python_cache` = **`/tmp/root/cutlass_python_cache`
  inside the container**, `cutlass/base_dsl/cache_helpers.py:66-88`), written atomically
  (unique `tmp.pid_<pid>_<uuid>` directory + `os.replace`, lines 226-244) and read with a CRC-32
  check — but it stores **MLIR bytecode only**; a "JIT cache hit IN-FILE" still runs
  `compiler_provider.jit(module)` (PTX → cubin) per process (`dsl.py:2158-2170`). Measured (§2.5):
  the GDN kernels left **zero** files there, so today the SM120 GDN prefill kernel is **compiled at
  every process start, per rank, in memory**, and the contract's `vllm-kernel-cache` volume
  (`/root/.cache/vllm` + `/root/.cache/flashinfer`, §6.6) cannot shorten it.

### 2.5 Measured on this hardware (throwaway containers of the candidate image, worker GB10, 2026-09-12 01:35Z)

Inputs shaped like vLLM's call (bf16 q/k/v, L2-normalised q/k, log-space gates, fp32 state, per-rank
16 heads × 128), FlashInfer SM120 vs vLLM's Triton/FLA reference in the **same** image:

| shape | FlashInfer SM120 | Triton/FLA | max abs Δ output | mean abs Δ | max abs Δ final state |
|---|---|---|---|---|---|
| 1 seq, T=64 (vLLM's warm-up shape → CP path) — **first call = JIT** | **5,266 ms** | 34,430 ms (Triton autotune) | 0.0005 | 2e-5 | 0.0054 |
| 1 seq, T=64 again | 0.4 ms | 0.8 ms | 0.0005 | 2e-5 | 0.0046 |
| 2 seqs, T=64 (mixed batch → non-CP path) — **first call = JIT** | **2,968 ms** | 1.0 ms | 0.0010 | 2e-5 | 0.0052 |
| 2 seqs again | 0.3 ms | 0.5 ms | 0.0005 | 2e-5 | 0.0048 |
| 1 seq, T=8,192 (one chunked-prefill step) | 1.7 ms | 8.7 ms | 0.0010 | 2e-5 | 0.0042 |
| 4 seqs, T=2,048 | 1.1 ms | 3.8 ms | 0.0010 | 2e-5 | 0.0044 |

Facts established: the kernel **compiles and runs on sm_121** (PTX check passed); outputs are finite
and agree with the Triton reference within bf16 tolerance (rel ≤ 1.4 % of max |o|); the kernel is
3.5-5× faster than Triton on the two realistic shapes (consistent with the PR's 3.8-4.5× on a DGX Spark);
a second *process* in the same container paid the JIT again (5.1 s + 2.3 s) — **no on-disk reuse**.
(An earlier probe with un-normalised random q/k produced non-finite outputs on both kernels — an input
artefact, not a kernel fault; noted so nobody repeats it.)
**Unverified:** end-to-end numerics through the whole model (needle recall, greedy-equivalence on 20
prompts) — that is what the B matrix measures.

### 2.6 Warm-up: does the JIT happen before the engine is READY?

Partly. `Qwen3NextGatedDeltaNet._warmup_prefill_kernels` (lines 1075-1200) runs during the V1
**profile run** (`_forward_core` sees `attn_metadata is None`, lines 1289-1290) — i.e. inside
`determine_available_memory()` → `profile_run()`, **before** KV-cache allocation, `kernel_warmup`,
CUDA-graph capture and `Application startup complete` (`v1/worker/gpu_worker.py:525-542, 762-804`).
It calls `self.chunk_gated_delta_rule(...)` with **one** sequence of `T = FLA_CHUNK_SIZE = 64`,
`initial_state` set, `output_final_state=True` — which on the GB10 selects the **CP** variant
(§2.4). So:
- the ~5.3 s CP compile is paid during start-up on each rank, before readiness (it is inside the
  contract's `COLD_START_BUDGET_S=900`; the controller's readiness canary never sees it);
- the **non-CP variant (~3 s) is NOT warmed**: it compiles at the first engine step that contains
  ≥ 2 prefill sequences — the first concurrent prefills after every start — stalling that step for
  ~3 s on each rank (they compile in parallel, one process per node). Below every timeout (the
  controller's canary is 60 s, `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS` 300 s), but it is a first-request
  latency artefact the A/B must not measure as a regression: the run plan (§6) fires a c=2 warm-up
  batch after every B start before any phase is timed.
- On the V2 model runner the additional `warmup_kernels()` (`v1/worker/gpu/warmup.py:218`) runs
  "scheduler-realistic prefill and decode steps" — whether one of them batches ≥ 2 prefills so the
  non-CP variant is warmed is **unverified** (read the function before relying on it).
- vLLM's `--compilation-config` has `compile_sizes` / `cudagraph_capture_sizes` (batch-size
  warm-ups, `gpu_worker.py:762-790`); there is **no** engine option to pre-run a multi-sequence GDN
  prefill, and `VLLM_SKIP_WARMUP` does not exist in this build. The one-request warm-up under the
  engine lock is therefore a harness/runbook step, not an engine flag.

### 2.7 The TP start-up race, assessed

On this pair each node runs **one** rank process (`VLLM::Worker_TP0` on the head, `VLLM::Worker_TP1`
on the worker — `docker top` verified) and each node has its own filesystem, so two ranks never
compile into the same cache: **the only possible race is intra-node, and there is no second
compiling process on either node.** Were there one (a single-node TP=2 layout), the picture is:
FlashInfer's nvcc/ninja JIT modules are protected by `FileLock` with a double-checked `try_load`
(`jit/core.py:300-318`) and a global `flashinfer_jit.lock` for batch builds (line 630); the CuTe-DSL
IR cache writes are atomic per-process (`os.replace` from a pid+uuid temp dir, CRC-checked reads),
so a concurrent identical compile is wasted work, never a torn file; and the GDN SM120 kernel itself
is cached only in memory, so two processes simply both compile (~8 s each, in parallel). No
lock-related failure mode applies to the pair. What *does* apply: both ranks must resolve the **same**
backend (`ChunkGatedDeltaRule` is a `CustomOp` created in every rank), which the byte-identical
`CLUSTER_ENGINE_ARGS` guarantees and the both-rank grep of §6.3 proves.

Precompile once per node — what exists and what does not:
- **exists:** the profile-run warm-up (CP variant) on every start; the persistent `vllm-kernel-cache`
  volume for torch.compile artefacts (`~/.cache/vllm`) and FlashInfer's nvcc JIT modules
  (`~/.cache/flashinfer`); `CUTE_DSL_CACHE_DIR` for the DSL's IR bytecode (IR only, not cubins;
  optional request to SRE in §9).
- **does not exist:** an on-disk cache of the compiled SM120 GDN kernels — every rank start pays
  ~5 s (CP) + ~3 s (non-CP, at first use). The practical "precompile" is the warm-up request
  under the engine lock after each start (§6.3 step 5), which the run plan makes routine.

## 3. Other engine facts the switch depends on (verified in the image)

| topic | candidate B | what we do and why |
|---|---|---|
| attention backend | `AttentionConfig.backend` default `None` (auto-select, `vllm/config/attention.py:24`); `--attention-backend` accepts the `AttentionBackendEnum` names (`FLASHINFER`, `FLASH_ATTN`, `TRITON_ATTN`, `B12X`, …, `v1/attention/backends/registry.py:44-135`) | **keep the explicit `--attention-backend flashinfer`** (`config/model-manifest.yaml:210` startup_arguments): the auto choice on sm_121 with fp8 KV is not pinned upstream and a silent change of the full-attention backend would be a second variable |
| Model Runner V2 | `VllmConfig.use_v2_model_runner` (`config/vllm.py:656-690`) returns **`True` by default** unless `VLLM_USE_V2_MODEL_RUNNER` is set or an unsupported feature is present. The fall-back triggers (lines 2550-2618): stock torch.compile mode, SP with TP>1, external_launcher+PP, ngram/other spec methods, parallel drafting, DBO, elastic EP, custom logits processors / `vllm.logits_processors` plugins, KV-sharing fast prefill, `mamba_cache_mode == "all"` (default `"none"`, `config/cache.py:189`). **Our config matches none → MRV2 would run by default.** | **MRV2 is the default, so B sets `VLLM_USE_V2_MODEL_RUNNER=0`** (launcher key `CLUSTER_VLLM_USE_V2_MODEL_RUNNER=0` → `.runtime/engine.env` on both ranks) to keep the V1 runner A uses: the A/B then has one variable, the GDN kernel. MRV2 (what upstream now tests, what the 2026-09-07 two-Spark datapoint ran, with the #55341 workspace fix in this build) is its own secondary test (§7.2). The env is documented as deprecated upstream with removal targeted at v0.32 — a B that passes is re-run on MRV2 before that |
| FlashInfer all-reduce | `VLLM_ALLREDUCE_USE_FLASHINFER` **default `1`** (`envs.py:1894`); consumed by `cuda_communicator.py:63` for TP groups; `_resolve_fi_ar_backend()` picks **`mnnvl` for every topology** and allows a `trtllm` fall-back only when `get_node_count() == 1` (`flashinfer_all_reduce.py:149-160`); mnnvl "needs NVSwitch multicast" — the GB10 reports `CU_DEVICE_ATTRIBUTE_MULTICAST_SUPPORTED = 0` (§4.4), so the workspace would fail and `FlashInferAllReduce._ensure_workspace` sets `disabled = True` (line 416) | set **`VLLM_ALLREDUCE_USE_FLASHINFER=0`** on both ranks (`CLUSTER_VLLM_ALLREDUCE_USE_FLASHINFER=0` → `.runtime/engine.env`, shipped to the worker): it skips a doomed multi-node mnnvl initialisation on a RoCE pair and keeps the all-reduce on PYNCCL like A (`Using ['PYNCCL'] all-reduce backends` is what A logs today), so the collective path is not a variable |
| `VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS` | default `300` (`envs.py:242`) — unchanged | unchanged; the controller/sentinel remain the fast path |
| GDN decode kernel | `VLLM_GDN_DECODE_KERNEL` default `cuda` (`envs.py:131`) — the fused CUDA MTP decode kernel from #51674; MTP is off so the packed Triton decode kernel stays | unchanged |
| prefix caching / batched tokens defaults | changed upstream (on / 16384) — we pass `--no-enable-prefix-caching --max-num-batched-tokens 8192` explicitly | unchanged, explicit |
| MoE backend | `--moe-backend` accepts `auto|…|flashinfer_b12x|b12x|marlin` (`config/kernel.py:122-136`); `auto` selects Marlin on GB10 (`Using 'MARLIN' NvFp4 MoE backend` in A's log) | unchanged for B (one variable); `flashinfer_b12x` is a *secondary* test (§7.2) |
| partial-prefill knobs named in CONTRACT §6.7 | **`--max-num-partial-prefills` and `--max-long-partial-prefills` do not exist in the candidate — nor in the pinned A build** (`grep -rn max_num_partial_prefills` over the installed `vllm/` finds nothing in either); only `--long-prefill-token-threshold` (`config/scheduler.py:70`, default 0) exists | the "exclusive long-prefill admission" secondary test uses `--long-prefill-token-threshold` plus the orchestrator's LONG lane (§7.2); **the contract's knob list needs amending (lead)** |
| kv-cache dtype | `--kv-cache-dtype {auto,float16,bfloat16,fp8,…}` (`config/cache.py:39-45`); ours is `fp8` | secondary test `bfloat16` (§7.2) |
| CUDA graphs | `cudagraph_mode` default resolves to `FULL_AND_PIECEWISE` (`config/vllm.py:308-331`); `--enforce-eager` exists (`arg_utils.py:894`) | unchanged for B; eager is a secondary test |

## 4. §CPU — topology, container limits, per-thread measurement, plan (from `scripts/cluster-cpu.sh`, 2026-09-12)

Run read-only against production: `CLUSTER_MODE=dual CLUSTER_HEAD_IP=10.100.184.1 CLUSTER_WORKER_IP=10.100.184.2 scripts/cluster-cpu.sh --recommend --probe --seconds 6`
(the env override is only because the worktree has no `.env`; from the deploy checkout the script reads it).

### 4.1 Topology (identical on both nodes)

```
== head (spark-0e68, 6.17.0-1031-nvidia) ==            == worker (spark-476e, 6.17.0-1029-nvidia) ==
  CPU(s): 20, 1 socket, 1 NUMA node, L3 24 MiB (2 instances), 1 thread/core
  big    (MIDR part 0xd85, Cortex-X925, max 3900 MHz): cpus 5-9,15-19  [10]
  LITTLE (MIDR part 0xd87, Cortex-A725, max 2808 MHz): cpus 0-4,10-14  [10]   MIDR/MAXMHZ agree: True
```
Core class is read from `/sys/devices/system/cpu/cpu*/regs/identification/midr_el1` (part number
bits [15:4]) and cross-checked against `lscpu -e` MAXMHZ; `cpu_capacity` agrees (bigs 997-1024,
LITTLEs 718-731). **Every container on both nodes runs with `CpusetCpus=""` (all 20), `NanoCpus=0`,
`CpuShares=0`, `CpuQuota=0`** — nothing is pinned or capped today; every engine process has
`Cpus_allowed_list 0-19`.

### 4.2 Per-process and per-thread CPU of the engine (6-s window, 8 streamed 800-token completions in flight)

```
head    VLLM::Worker_TP0  pid 3237984  207.7 %  rss 9,914 MB  82 threads   last cpu 6 (big)
        VLLM::EngineCore  pid 3237652  103.2 %  rss 1,863 MB  59 threads   last cpu 18 (big)
        vllm (API server) pid 3236053    7.2 %  rss 3,845 MB  89 threads   last cpu 1 (LITTLE)
        busiest thread: VLLM::Worker 3237984/3243527  100.2 %  cpu 5 (big)  role nccl_proxy?
        idle % per core: 0-4: 94-96 | 5: 53  6: 55  7: 56  8: 94  9: 64 | 10-14: 92-96 | 15: 59  16: 65  17: 84  18: 74  19: 61
        PASS  20 cores >= 50 % idle; least idle cpu5 53 % (big), cpu6 55 %, cpu7 56 %, cpu15 59 % (all big)
worker  VLLM::Worker_TP1  pid 2798032  120.7 %  rss 10,427 MB  80 threads  last cpu 19 (big)
        vllm (headless)   pid 2797570    0.0 %  rss 1,384 MB  40 threads
        busiest thread: VLLM::Worker 2798032/2798163   99.0 %  cpu 16 (big)  role nccl_proxy?
        idle % per core: everything 97-100 % except cpu16: 11 %, cpu19: 86 %, cpu15: 89 %
        PASS  19 cores >= 50 % idle
load average  head 2.25 / 1.96 / 3.49   worker 0.72 / 0.71 / 0.89
NCCL env on both ranks: NCCL_SOCKET_IFNAME=enp1s0f1np1 NCCL_IB_HCA=rocep1s0f1,roceP2p1s0f1 NCCL_DEBUG=INFO (no NCCL_SET_THREAD_NAME → proxy threads keep the comm "VLLM::Worker")
NIC/GPU IRQs by count (head): 429 nvidia 35.2 M; 381 mlx5_comp17@0000:01:00.1 1.65 M; 363/406/341 mlx5_async0 0.6 M each
```
Reading: under load the head's rank process is **two full cores** (its main thread ≈ 100 % + the
NCCL proxy thread ≈ 100 %), EngineCore a third, the API server a few percent; the worker's rank is
its proxy thread (99 %) plus ~20 % on the main thread. The scheduler already places all three
pollers on big cores (the `last cpu` column, and the big cores are the least idle), and at idle
they drop to ~0 % (the earlier idle sample: every engine process 0.0 %). With 8 streams in flight
16 of 20 cores are still ≥ 50 % idle on the head and 19 on the worker: **the "some cores must
remain free" check passes on both nodes**; CPU is not the bottleneck of this pair.

### 4.3 Proposed cpuset plan (proposal only — nothing applied; `scripts/cluster-cpu.sh --recommend`)

```
head:   keep cpus 18-19 (big) FREE for the kernel / NIC IRQs / ssh; rank on 5-9,15; secondary engines share 16-17 + LITTLE; sidecars on 0-4,10-14
  sf-local-ai-vllm-1                 cpuset 5-9,15            rank main thread + EngineCore + NCCL proxy: busy-pollers on big cores; API server rides along
  sf-local-ai-vllm-router-1          cpuset 0-4,10-14,16-17   secondary vLLM engine: LITTLE cores plus two shared bigs for its own EngineCore/worker pollers
  sf-local-ai-vllm-embed-1           cpuset 0-4,10-14,16-17   (same)
  sf-local-ai-vllm-reranker-1        cpuset 0-4,10-14,16-17   (same)
  sf-local-ai-whisper-whisper-1, orchestrator, postgres, prometheus, grafana, exporters, cadvisor, searxng, cloudflared, frontend, sync-worker, watchdog/controller
                                     cpuset 0-4,10-14         sidecars: LITTLE cores only
  (stray containers pg-test, litellm-dgx, portainer, zealous_williamson, nostalgic_pascal, techsara-e2e-*: LITTLE, flagged "review")
worker: keep cpus 18-19 (big) FREE; rank on 5-9,15; OCR shares 16-17 + LITTLE; whisper + exporters + the tenant's postgres on 0-4,10-14
```
How it would be applied (not here): `cpuset:` per service in the SRE-owned overlays for both nodes,
rendered by `./techsara up` under the engine lock (a cpuset change recreates the container = a pair
restart), or `docker update --cpuset-cpus` for a one-off trial on a sidecar only; measured before/after
with `scripts/cluster-ab.py` (`c10_short`, `decode_burst`), one variable at a time, never together with
the GDN A/B. Expected effect: determinism, not throughput — the scheduler already does the right thing.

### 4.4 GPUDirect RDMA (`scripts/cluster-cpu.sh --gdr`) — a separate experiment, and on this hardware not one that can be run

Evidence from the running ranks (both nodes, `NCCL_DEBUG=INFO`), verbatim:
```
NCCL INFO NET/IB : GPU Direct RDMA Disabled for HCA 0 'rocep1s0f1'
NCCL INFO NET/IB : GPU Direct RDMA Disabled for HCA 1 'roceP2p1s0f1'
NCCL INFO NET/IB : Using [0]rocep1s0f1:1/RoCE [1]roceP2p1s0f1:1/RoCE [RO]; OOB enp1s0f1np1:10.100.184.1<0>
NCCL INFO Connected all rings, use ring PXN 0 GDR 0
NCCL INFO dlvsym failed on mlx5dv_reg_dmabuf_mr - /lib/aarch64-linux-gnu/libmlx5.so: undefined symbol: mlx5dv_reg_dmabuf_mr, version MLX5_1.25
NCCL INFO Symmetric memory is not supported. cuMemEnable 1, globalGinSupport 0, cuMemGdrSupport 0
NCCL version 2.30.7+cuda13.3
```
Kernel side (both nodes): driver `580.173.02` (open kernel module); `nvidia_peermem` **not loaded**
(the module file ships: `/lib/modules/<kernel>/kernel/nvidia-580-open/nvidia-peermem.ko`);
`/sys/kernel/mm/memory_peers` absent; `nvidia-smi topo -m`: GPU0 ↔ every NIC = **NODE**.
The decisive facts — CUDA device attributes of the GB10 (queried from the pinned image, 1 s):
```
CU_DEVICE_ATTRIBUTE_INTEGRATED 1        CU_DEVICE_ATTRIBUTE_DMA_BUF_SUPPORTED 0
CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_SUPPORTED 0     CU_DEVICE_ATTRIBUTE_GPU_DIRECT_RDMA_WITH_CUDA_VMM_SUPPORTED 0
CU_DEVICE_ATTRIBUTE_MULTICAST_SUPPORTED 0           CU_DEVICE_ATTRIBUTE_PAGEABLE_MEMORY_ACCESS 1     driver 13000
```
NCCL enables GDR for an HCA only when it can register GPU memory with the NIC through DMA-BUF
(needs `DMA_BUF_SUPPORTED=1` plus `ibv_reg_dmabuf_mr`, which the image has) or through
`nvidia_peermem` (needs `GPU_DIRECT_RDMA_SUPPORTED=1`). **The 580.173.02 driver reports 0 for both on
the GB10** (an integrated GPU on coherent LPDDR5X), so no NCCL variable, no rdma-core upgrade and no
module load can turn GDR on; loading `nvidia_peermem` against a driver that reports no support is an
unsupported experiment and must not be tried on the production pair. Even with driver support the
experiment would also need `NCCL_NET_GDR_LEVEL=SYS` on both ranks (the GPU-NIC distance is NODE,
above the default PXB) and the both-rank grep for `GPU Direct RDMA Enabled` before any number is
compared. What is lost today is one GPU→host copy per chunk on the same physical memory; the fabric
already delivers 171.6 Gb/s NCCL busbw (97 % of NVIDIA's reference), so the upside would be
decode-step latency, not bandwidth. Verdict: **not enableable on this driver; documented, parked; never
combined with the GDN A/B.**

## 5. A — the baseline as measured today (smoke, `scripts/cluster-ab.py --label A --smoke`)

Run 2026-09-12 01:45:12Z against the production engine (image `24f2f897…`, engine argv sha256
`d52c6568b17c`, git `321cf70`), 2 requests per phase, needle at 8K; exit 0, 0 failed requests,
correctness 1.0 wherever judged, **no alarms** (0 Xid, 0 restarts, 0 fault lines on both nodes,
26 monitor samples). Outputs under `.runtime/ab/A-<stamp>/` (this run: the workstream scratchpad,
because the worktree's `.runtime/` is root-owned — §8.2).

| phase | ok | wall s | TTFT p50 / p95 s | decode tok/s p50 | prefill tok/s p50 | gap p95 s | correct | GPU util head/worker % | head CPU max % (Worker_TP0 / EngineCore / API) | RoCE tx per rail (head) |
|---|---|---|---|---|---|---|---|---|---|---|
| c1_short | 2/2 | 1.6 | 0.109 / 0.115 | 114.2 | — | 0.010 | — | 44 / 0 | 141 / 47 / 4.6 | 0.13 / 0.15 GB |
| c10_short | 2/2 | 1.3 | 0.093 / 0.191 | 109.7 | — | 0.011 | — | 0 / 0 | 136 / 42 / 3.1 | 0.09 / 0.12 |
| c16_short | 2/2 | 1.5 | 0.099 / 0.201 | 77.2 | — | 0.012 | — | 93 / 74 | 146 / 49 / 3.4 | 0.12 / 0.18 |
| prefill_32k (32,7xx tokens) | 2/2 | 9.3 | **3.973 / 3.992** | 110.9 | **8,210** | 0.011 | — | 96 / 96 | 193 / 97 / 9.0 | 11.6 / 11.6 |
| prefill_128k (131,0xx tokens) | 2/2 | 54.1 | **25.85 / 26.21** | 132.8 | **4,997** | 0.014 | — | 96 / 96 | 202 / 100 / 15.1 | 46.2 / 46.2 |
| needle (8,121 tokens, 3 needles) | 1/1 | 2.0 | — | — | 6,835 | — | **1.0 (3/3)** | 96 / 92 | 164 / 54 / 2.6 | 1.4 / 1.5 |
| decode_burst (2 × 256, ignore_eos) | 2/2 | 3.8 | 0.051 / 0.100 | 85.9 | — | 0.013 | — | 94 / 93 | 196 / 95 / 6.1 | 0.09 / 0.48 |
| mixed (c=2) | 2/2 | 6.6 | 0.330 / 0.967 | 73.9 | — | 0.014 | — | 94 / 47 | 198 / 96 / 5.8 | 1.7 / 2.3 |
| cancellations (2 cancelled + probe) | 3/3 | 1.0 | 0.050 / 0.096 | — | — | — | **1.0**, engine kept serving, running drained | 45 / 0 | 115 / 18 / 3.0 | — |
| json_structured | 2/2 | 1.8 | — | — | — | — | **1.0** (json.loads ok) | 91 / 0 | 156 / 56 / 2.4 | — |
| streaming | 2/2 | 3.3 | 0.103 / 0.199 | 83.9 | — | 0.013 | — | 47 / 47 | 194 / 95 / 5.7 | 0.16 / 0.47 |

These are 2-request smoke numbers — reference points for the harness, not the baseline of record.
The 32K prefill (3.97 s TTFT) reproduces the 2026-09-11 bench (4.10 s); the 128K prefill costs
25.9 s (≈ 5,000 tok/s), which is the number the #53787 long-prefill regression check in §7.1 is
measured against. GPU-util "0 %" rows are 1-s phases sampled between steps, not idle engines.

## 6. The run plan

Everything below is run from the deploy checkout by the SRE/lead (the harness and the CPU script
are read-only; the switch is `./techsara up` under the engine lock, SRE-owned). Every start, grep
and measurement is recorded under `.runtime/ab/`.

### 6.1 A — the full baseline on the production image (no restart)

```
orchestrator/.venv/bin/python scripts/cluster-ab.py --label A --phase all --worker techsphere@10.100.184.2
```
≈ 35-40 min: 11 phases (`mixed` is 10 min at c=10, `needle_950k` ≈ 3-4 min at ~5,000 tok/s,
`prefill_128k` 3 × 26 s), monitor every 5 s into `monitor.jsonl`. Preconditions: engine READY
(controller `/state` READY/BUSY, `scripts/cluster-verify-engine.sh --probe` all PASS), no second-tenant
run in flight (the tenant's concurrency-10 load would be measured as ours), `.runtime/ab/` writable.
Record the run directory; it is the A of every comparison.

### 6.2 The switch to B (SRE, under the engine lock, one change window)

`.env` on the head (all three already validated by the launcher, `launcher/techsara_cli/{environment,cluster}.py`):
```
MAIN_MODEL_IMAGE=vllm/vllm-openai@sha256:819ec9c063412e5730d1b0e82046ba540d1bf991f3c4f661a849aae8a0c52374
CLUSTER_GDN_PREFILL_BACKEND=flashinfer
CLUSTER_VLLM_ALLREDUCE_USE_FLASHINFER=0
CLUSTER_VLLM_USE_V2_MODEL_RUNNER=0     # MRV2 is the default on this build (§3): keep V1 so the kernel is the only variable
```
Only the `vllm` service's image moves (`compose/compose.dgx-spark.yaml:41`); router, embed,
reranker and OCR stay on the pinned digest. Then `scripts/cluster-sync.sh` (ships `worker.env`,
`engine.env`, the image digest — the sha256 checks must PASS), and `./techsara up` under the
engine lock (`scripts/lib/engine-lock.sh` — the controller must not be mid-recovery; the
`cluster-up.sh` wrapper takes the lock). Expect a cold `torch.compile` (new build → new cache key)
plus the FlashInfer GDN JIT: budget the contract's `COLD_START_BUDGET_S=900`; measure the actual
`started_at → Application startup complete → first canary` on both ranks and record it as B's
cold-start (`techsara_vllm_cold_start_seconds`).

### 6.3 Verification after every B start (both ranks)

**Executed 2026-09-12** (the switch: `./techsara up` at 08:39Z under the engine lock,
`.runtime/logs/techsara-up-candidateB-20260912T0839Z.log` — `cluster-sync` 13 pass, image ID
`sha256:5a0f8b91…` on both nodes, `engine.env` 2 variables sha256-matched, the worker's
`kernel-cache` volume created; worker 08:39:29Z, head 08:39:59Z). What each step below returned:

| step | result | record |
|---|---|---|
| 1 verify probe | 23 pass / 2 fail on the first start; the 2 = the since-boot Xid baseline (2 per node, unchanged all day); args identical on both ranks, NCCL `Using network IB`, both GPUs 93 % / 92 % | `.runtime/incidents/20260911T223140Z-vllm-down/80-verify-engine-B-first-start.txt` |
| 2 the kernel line | `(Worker_TP0 pid=508) … 08:41:04 … Using FlashInfer GDN prefill kernel (requested=flashinfer, head_k_dim=128).` and `(Worker_TP1 pid=305) … 08:41:04 …` on the first start; on both ranks on the three drill-16 restarts (the drill greps both ranks: 08:46:17, 08:51:20, 08:56:28), on drill 3's start (09:46:35, both ranks, `…095052Z/head-logs-1.txt` + `worker-diagnostics-1.txt`) and on drill 6's start (10:44:09 / 10:44:10, `…104339Z/*-repair-20260912T104336Z.txt`); no `Falling back` line in any captured log. The pair serving now (head 10:46:27Z) has no captured record of the line — run the §14 grep of `RUNBOOK.md` before relying on it. `VLLM_USE_V2_MODEL_RUNNER=0` in both ranks' environment (`ab/B-meta.json` `engine_env`). CUDA graphs `mixed prefill-decode, PIECEWISE` 51 + `decode, FULL` 35; `speculative_config` absent; `--no-enable-prefix-caching`. The model-runner banner wording, the all-reduce and MoE-backend lines were not asserted from the log today | `.runtime/incidents/20260912T084520Z/head-logs-1.txt`, `worker-diagnostics-1.txt`; `.runtime/drills/20260912T084519Z/` |
| 3 controller | READY through the readiness sequence after every start (two ≈ 200-token participation probes, head 94 % / worker 92 %); `last_failure_category` after the first start was `none` until drill 16 set `manual` | `curl 127.0.0.1:9838/state`, Prometheus `techsara_vllm_last_failure_category` |
| 4 repeated TP=2 starts | drill 16: three `POST /recover` cycles, READY **188 / 186 / 180 s** after each request; cold start on the first B start `torch.compile 20.56 s`, `init engine 61.28 s`, API accepting after ≈ 3 m 46 s; the restarts **4.96 / 3.27 s** and **27.2 / 28.8 s** (the `vllm-kernel-cache` volume did shorten them; FlashInfer autotuner `Config cache hit`); the GDN line on both ranks each time; no `--clear-kernel-cache` needed | `.runtime/drills/20260912T084519Z/drill-16-tp2-repeated-startup.log`; `.runtime/incidents/20260912T08{50,55}*/head-logs-1.txt` |
| 5 warm-up | not run as written; the controller's two concurrent participation probes (commit `309356c`) warm the multi-sequence GDN kernel before READY instead | controller `/state .readiness.participation_probes` |

1. `scripts/cluster-verify-engine.sh --probe` — all PASS bar the pre-existing Xid count (record the
   count before, require it not to increase), args byte-identical on both ranks, NCCL `Using network IB`,
   both GPUs ≥ 30 % during the probe.
2. The kernel line, **on both ranks**, exact format from §2.3:
   ```
   docker logs sf-local-ai-vllm-1 2>&1 | grep -F 'Using FlashInfer GDN prefill kernel (requested=flashinfer, head_k_dim=128).'
   ssh techsphere@10.100.184.2 "docker logs sf-local-ai-worker-vllm-worker-1 2>&1 | grep -F 'Using FlashInfer GDN prefill kernel (requested=flashinfer, head_k_dim=128).'"
   ```
   one hit each, prefixed `(Worker_TP0 pid=…)` and `(Worker_TP1 pid=…)`; **any** `Falling back to
   Triton/FLA` line on either rank = the switch did not take effect = stop. Also assert from the
   banners, not from the launch script: `Initializing a V1 LLM engine (v0.28.1rc1.dev580+g385dce36b)`,
   the resolved model runner (`VLLM_USE_V2_MODEL_RUNNER=0` present in the rank's environment —
   `docker inspect` both containers — and the V1 runner in the banner; **the exact wording of the
   model-runner line is unverified**), `cudagraph_mode` FULL_AND_PIECEWISE,
   `speculative_config=None`, `enable_prefix_caching=False`, `Using ['PYNCCL'] all-reduce backends`
   (no FlashInfer all-reduce initialisation), `Using 'MARLIN' NvFp4 MoE backend`.
3. Controller: `/state` reaches READY through the §5 v2 readiness sequence; `techsara_vllm_last_failure_category{category="none"} == 1`.
4. **Repeated TP=2 starts:** three consecutive `cluster-down.sh` / `cluster-up.sh` cycles under the
   lock, each with steps 1-3, each recording cold-start seconds, whether the `vllm-kernel-cache`
   volume shortened the second and third start (torch.compile), and that the GDN line appears on both
   ranks every time. A start that needs a `--clear-kernel-cache` is a finding, not a retry.
5. **Warm-up before measuring** (§2.6): one c=2 prefill batch so the non-CP kernel is compiled on
   both ranks — `scripts/cluster-ab.py --label B --smoke --phase c10_short --out /tmp/warmup` (2
   requests at c=2), discarded; then `--phase prefill_32k --smoke` once, discarded.

### 6.4 The B matrix and the comparison

```
orchestrator/.venv/bin/python scripts/cluster-ab.py --label B --phase all --worker techsphere@10.100.184.2
orchestrator/.venv/bin/python scripts/cluster-ab.py --compare .runtime/ab/A-<stamp> .runtime/ab/B-<stamp>
```
then the reproducer that fired at 27 min on 2026-09-11: `scripts/cluster-soak.py --minutes 120
--concurrency 10`, then `orchestrator/scripts/validate_long_context.py --sizes 8192,32768,131072,262144`,
then the orchestrator smoke (a thinking turn, a tool-call turn, a `json_completion`, a vision turn,
a Deep Research step). The compare table (B − A per phase and per node, ✓/✗) is the artefact for
the decision; the pass criteria are §7.1.

**Executed 2026-09-12.** A = run `20260912T0514Z` on the production image (10 phases, 856 s,
git `caa97e9`); B = run `20260912T0859Z` (11 phases, 1,639 s, git `16fe5cd`); the comparison is
[`ab/compare-A-vs-B-20260912.md`](ab/compare-A-vs-B-20260912.md), the per-run tables
[`ab/A-20260912T0514Z-SUMMARY.md`](ab/A-20260912T0514Z-SUMMARY.md) and
[`ab/B-20260912T0859Z-SUMMARY.md`](ab/B-20260912T0859Z-SUMMARY.md). Against §7.1:

| criterion | result | verdict |
|---|---|---|
| 2 — 0 failed requests, correctness 1.0 where judged, no alarms | B: 0 failed in every phase; needle 3/3 at 949,9xx tokens (799 s, 1,189 tok/s prefill); `json_structured` 30/30; `cancellations` 21/21; 0 Xid, 0 restarts, 0 fault samples on both nodes; alarms: none | pass |
| 3 — `prefill_128k` TTFT p50 ≤ 1.3 × A | 25.0 vs 26.3 s = **0.95 ×** | pass |
| 3 — `needle_950k` total ≤ 1.3 × A | A has no figure (the shell watchdog restarted the head under A's needle at 01:20Z, `INCIDENT-2026-09-11-vllm.md` §7.1) | not measurable |
| 3 — `prefill_32k` TTFT p50 ≤ 1.1 × A | 3.79 vs 4.19 s = 0.91 × | pass |
| 3 — decode tok/s p50 ≥ 0.9 × A in `c1_short`, `c10_short`, `decode_burst` | 109.6 / 111.5 = 0.98; 26.6 / 25.4 = 1.04; 40.0 / 40.1 = 1.00 | pass |
| 3 — `c10_short`, `c16_short` TTFT p95 ≤ 1.1 × A | 0.317 / 0.604 = 0.53; 0.366 / 0.662 = 0.55 | pass |
| 3 — `streaming` gap p95 ≤ 1.2 × A | 0.016 / 0.016 = 0.98 | pass |
| 3 — aggregate completion tok/s ≥ 0.9 × A in `c10_short`, `c16_short`, `mixed` | 213.2 / 205.6; 306.7 / 269.6; 200.6 / 160.4 (`mixed`: **692 vs 550** requests in 10 min at c=10) | pass |
| 4 — both-rank grep on every start, cold start ≤ 900 s, no fallback / JIT-error signature | every start (§6.3); cold ≈ 226 s to API accepting, warm restarts 152–166 s to READY | pass |
| 1 — the 120-min soak | `scripts/cluster-soak.py --minutes 120 --concurrency 10`, started 10:50Z (`.runtime/logs/soak-B-20260912T1050Z.log`, baseline head RestartCount 1, worker 0, Xid 2/2): **in progress at the time of writing; result appended by the lead** | pending |
| 5 — `validate_long_context.py` on B | not recorded today (the ~950K needle of the matrix is the only long-context figure on B) | not measured |
| 6 — the orchestrator smoke on B | not recorded as a run; the drills' chats and the canaries completed on the primary | not measured |
| 7 — ≥ 7 clean production days | B has served since 08:39Z; the 48–72 h canary has not started | **still unproven** |

Observed on B, not a §7.1 criterion: one 2.8 s inter-token stall in `c10_short` (gap max 0.362 s
on A); `decode tok/s p50` in `prefill_32k`/`prefill_128k` reads lower on B (113 vs 232 / 176); those
phases generate very few tokens (aggregate completion 3.5–3.8 tok/s against 7–8K prompt tok/s),
so the per-request decode figure rests on a handful of tokens — the aggregate and TTFT columns
are the measure there, and the decode criterion of §7.1 names `c1_short`, `c10_short` and
`decode_burst` for that reason. During B's `needle_950k` the controller went WEDGED (09:04–09:16Z)
and a pair restart was refused only by the spent budget — the finding of
`INCIDENT-2026-09-11-vllm.md` §7.2, a controller matter, not an engine one.

### 6.5 Rollback

`.env`: remove `MAIN_MODEL_IMAGE`, `CLUSTER_GDN_PREFILL_BACKEND`, `CLUSTER_VLLM_ALLREDUCE_USE_FLASHINFER`
and `CLUSTER_VLLM_USE_V2_MODEL_RUNNER` → the compose default `vllm/vllm-openai@sha256:24f2f8975…`
(image ID `60d84700e24f…`, cached on both nodes — the tag is gone from Docker Hub, so the insurance
`docker save` of §6.2 of the research doc must exist on both nodes before the pull) →
`scripts/cluster-sync.sh --env-only` (worker.env + engine.env sha256 PASS) → `./techsara up` under the
lock → §6.3 steps 1-3 with the A line `Using Triton/FLA GDN prefill kernel (requested=auto, head_k_dim=128).`
on both ranks. Time: one cold start (~5 min 20 s measured cold). The kernel-cache volumes keep the B
artefacts beside A's (keyed by build); `scripts/cluster-recover.sh --clear-kernel-cache` only if a
start fails with a compile-error signature.

## 7. Pass criteria and the secondary tests

### 7.1 Pass criteria for B (all must hold; a fail on any one is a rollback)

From the research document §6.4 and the harness:
1. `scripts/cluster-soak.py` **VERDICT: PASS** for 120 min at c=10 (zero CUDA-fault lines in both
   engine logs, zero new Xid on both kernels, zero container restarts, no frozen
   `vllm:generation_tokens_total` with requests running, every request answered); the controller shows
   no incident of category ≠ `none` for the whole window.
2. `scripts/cluster-ab.py --label B --phase all` exit 0: **0 failed requests** in every phase,
   `correctness_rate == 1.0` where judged (needle 3/3 at ~950K, every `json_structured` answer parses,
   `cancellations` with `engine_kept_serving` and `running_drained` true), **no alarms** (Xid, restarts,
   fault lines) on either node.
3. Comparison, B vs A (`--compare`): `prefill_128k` TTFT p50 ≤ **1.3 × A** (the #53787 long-prefill
   regression is a fail above that; A today: 25.9 s); `needle_950k` total ≤ 1.3 × A; `prefill_32k`
   TTFT p50 ≤ 1.1 × A (expected *faster*: the PR measured 5.6-7.4 % TTFT at ISL 32K); decode
   `tok_s_p50` in `c1_short`, `c10_short`, `decode_burst` ≥ **0.9 × A**; `c10_short` and `c16_short`
   TTFT p95 ≤ 1.1 × A; `streaming` gap p95 ≤ 1.2 × A; aggregate completion tok/s in `c10_short`,
   `c16_short`, `mixed` ≥ 0.9 × A. (Bench cross-check against 2026-09-11: c=1 100.7 tok/s, c=4 238.7,
   c=10 283.9, 32K TTFT 4.10 s — within −10 %.)
4. Both-rank grep of §6.3 positive on **every** start of the window; cold start ≤ 900 s each time;
   no `Falling back to Triton/FLA`, no `flashinfer.jit` / `cutlass` / `nvcc` / `cuda_nvrtc` error signature.
5. `validate_long_context.py`: needles 3/3 at every size; 262K latency ≤ 1.3 × 78.5 s.
6. Orchestrator smoke: all five turns complete on the primary (one-model mode: nothing answered elsewhere).
7. A 120-min pass is necessary, not sufficient: the class is declared closed only after ≥ 7 days
   of production with zero controller incidents of category ≠ `none` and
   `changes(process_start_time_seconds{job="vllm-main"}[7d]) == 0` outside planned restarts.

### 7.2 Secondary tests, same model, one variable at a time, each = a fresh A/B pair

Run only after B passes or fails cleanly, each from the pinned build **or** from B as stated, each
its own `--label A`/`--label B` pair (the label means "before/after this one change"), never two
at once, never with the GDR experiment (§4.4). Exact flags verified in the candidate's `arg_utils.py`
/ config unless marked.

| test | exact change (both ranks, via `config/model-manifest.yaml:210` `startup_arguments` → `CLUSTER_ENGINE_ARGS`, or the `CLUSTER_VLLM_*` env keys) | what to measure |
|---|---|---|
| BF16 KV cache | `--kv-cache-dtype bfloat16` (choices `auto,float16,bfloat16,fp8,…`); the explicit `--kv-cache-memory-bytes 8589934592` then holds **half** the tokens (KV budget 1,663,201 → ~831,600 tokens per MEMORY-BUDGET) — `needle_950k` will be **rejected (400)** unless the budget is raised or the needle run at 500K; note it as expected | fault rate (soak), `needle_950k`/`prefill_128k` correctness and TTFT, `c10_short` TTFT p95 and decode tok/s (bf16 KV = 2× KV traffic), KV-cache usage % in the monitor |
| lower batched tokens | `--max-num-batched-tokens 4096` (ours 8192) | `prefill_32k`/`prefill_128k` TTFT (expect ↑), `c10_short`/`c16_short` TTFT p95 and gap p95 (expect ↓: shorter steps), soak fault rate under mixed batches |
| lower max_num_seqs | `--max-num-seqs 10` (default `DEFAULT_MAX_NUM_SEQS`, `config/scheduler.py:63`; the tenant's load is exactly 10) | `c16_short` (6 of 16 must queue: TTFT p95 ↑, `num_requests_waiting` in the monitor), decode tok/s p50 at c=10 unchanged, soak fault rate |
| exclusive long-prefill admission | **engine side:** `--long-prefill-token-threshold 32768` (the only partial-prefill knob that exists in either build — `--max-num-partial-prefills` / `--max-long-partial-prefills` do **not**, §3); **orchestrator side:** the CONTRACT §6.7 LONG lane (`ADMISSION_LONG_MAX=1`, wait for `requests_running ≤ 0`) | `mixed` phase: TTFT p95 of the short requests while a 32K paste prefills (expect ↓), `prefill_128k` TTFT (expect ↑ by the wait), and above all the soak: the incident's batch shape (a long prefill chunk beside 9 decodes) no longer occurs — the monitor's `running`/`waiting` series and the fault count are the evidence |
| non-Marlin MoE backend (Track A of the research doc) | `--moe-backend flashinfer_b12x` on the **pinned** build first (selectable there, `config/kernel.py:132`; +~5 GiB weights/rank; W4A16 checkpoint executed as W4A4 — #56535 — so quality is a variable) | greedy-equivalence on 20 prompts vs Marlin (identical prompts, `temperature 0`, `seed 7`), tool-call JSON validity, needles 3/3 at 8K/32K/131K, head `MemAvailable ≥ 15 GiB` at idle after warm-up (the head already fails new CUDA contexts today — §8.1), the soak; log line `Using 'FLASHINFER_B12X' NvFp4 MoE backend` on both ranks |
| Model Runner V2 | drop `CLUSTER_VLLM_USE_V2_MODEL_RUNNER=0` on B (MRV2 is the build's default, §3); verify the runner from the banner on both ranks | the same matrix; specifically `decode_burst` and `c16_short` (MRV2's batching), the soak (the 2026-09-07 two-Spark fault ran on MRV2), and start-up time (MRV2 runs the extra `warmup_kernels()` pass — whether it warms the non-CP GDN kernel is the unverified item of §2.6) |
| enforce-eager | `--enforce-eager` (no CUDA graphs; `cudagraph_mode` NONE) | decode tok/s p50 (expect −8…−34 %; FULL graphs were worth +45 % on this pair), `decode_burst` aggregate, `c10_short` TTFT, and the soak fault rate (the only arm that survived #54331's dissection on sm_120) |
| `CUDA_LAUNCH_BLOCKING=1` + eager | `--enforce-eager` plus process env `CUDA_LAUNCH_BLOCKING=1` on both ranks — **needs a new launcher key**: `CLUSTER_ENGINE_ENV_KEYS` accepts only the two `CLUSTER_VLLM_*` keys today (request to SRE, §9) | diagnosis, not performance: the first fault's traceback then names the launching kernel instead of a sticky-error observation point (the class's open question); soak until the first fault or 120 min; expect ~37 % throughput loss reported upstream |

## 8. Findings from this workstream that the lead/SRE need

### 8.1 The head cannot open a new CUDA context (2026-09-12 07:11 IST, still true at 07:20)

A throwaway `docker run --gpus all … torch.zeros(1024, device="cuda")` of **either** image fails on
the head with `CUDA error: out of memory` while `free -g` shows 32 GiB available (88 GiB used, 6 free,
31 buff/cache, 12+ GiB in swap), and the kernel logs
`NVRM: … Out of memory [NV_ERR_NO_MEMORY] … kgrctxAllocCtxBuffers … kernel_graphics_object.c:215`
and `_memdescAllocInternal … mem_desc.c:1359`. The driver could not allocate a graphics context for a
new process — exactly the MEMORY-BUDGET §"swap-stall risk" (`NV_ERR_NO_MEMORY` during a model start
beside the other residents, seen 2026-09-11 07:20Z). Consequence: **a head engine restart could fail
to create its CUDA context** until memory is reclaimed (the residents on the head plus developer
tooling — the workstreams' pytest/venvs — are the swing). The GPU probes for this document were run
on the worker for that reason. Not my file to fix; the controller's `cold_start_timeout` would be the
symptom.

### 8.2 The production engine faulted again during this work (2026-09-12 01:20Z)

While another session ran `validate_long_context.py` at 631,689 tokens, the engine stopped
(`Avg generation throughput: 0.2 tokens/s, Running: 3`, then no `/metrics`); the head's **legacy
healthcheck** killed the head at 01:20:02Z/01:20:32Z (`kill` events in `docker events`), Docker
recreated it (StartedAt `01:20:32Z`), the worker rank lost its TCPStore (`Failed to check the "should
dump" flag on TCPStore … Broken pipe`) and was restarted by its own healthcheck at 01:21:38Z
(RestartCount 3). The pair was serving again by 01:26Z. The head kernel shows 2 Xid lines since boot.
This is the 2026-09-11 choreography, one more time, on the pinned image — the controller/sentinel
(deployed 08:29Z the same day; the watchdog removed) is what changes it. The A smoke in §5 was run
after the recovery. The incident report's §7.1 carries this event with its timeline; per the
incident bundle's `00-TIMELINE.md` addendum the kill was the **shell watchdog's** `docker restart
-t 30` (SIGTERM 01:20:02Z, SIGKILL 01:20:32Z — the 30 s gap is its `-t 30`), not the healthcheck,
and the request in flight was the 950,000-token needle (949,915 prompt tokens, `Server
disconnected` at 381.1 s, `.runtime/ab/A-baseline-20260912T0109Z/needles.txt`).
Also: the worktree's `.runtime/` is `root:root 0755` (a docker-run promtool/compose render by another
workstream), so `scripts/cluster-ab.py` refuses its default output dir with a clear message and the
runs went to the scratchpad; from the deploy checkout the default works.

### 8.3 Contract items to amend (lead)

- §6.7 names `--max-num-partial-prefills 1` and `--max-long-partial-prefills 1`: **neither flag exists**
  in the pinned build or the candidate (§3). Replace with `--long-prefill-token-threshold` + the LONG lane.
- §6.6 says the kernel-cache volumes let "a recovery reuse the validated torch.compile / FlashInfer
  JIT artefacts"; true for torch.compile and FlashInfer's nvcc modules, **not** for the SM120 GDN
  prefill kernels, which are compiled in memory on every rank start (~5 s + ~3 s at first mixed batch, §2.4-2.6).

## 9. Requests to other workstreams

- **SRE (`launcher/techsara_cli/cluster.py`, compose overlays):** (a) consider adding
  `CUTE_DSL_CACHE_DIR=/root/.cache/flashinfer/cute-dsl` to `.runtime/engine.env` so the DSL's IR cache
  lands on the persistent volume — a small win (IR only; the cubin JIT still runs) and harmless;
  (b) a generic `CLUSTER_VLLM_EXTRA_ENV` or a `CUDA_LAUNCH_BLOCKING` key so the diagnosis arm of §7.2
  can be rendered byte-identically on both ranks without editing compose; (c) `.runtime/` ownership
  in shared worktrees.
- **Controller:** the §5 v2 readiness sequence sends single requests only, so on B the non-CP GDN
  kernel is still un-compiled at MARK_READY; the first concurrent prefills after READY stall ~3 s per
  rank. Either accept (documented here) or add a two-request participation probe.
- **Docs:** RUNBOOK's candidate section should point at §6.2-6.5 here for the switch and the rollback.

## 10. What is verified and what is not

Verified inside the image or on the hardware: everything in §1-3 unless marked, §2.5 (worker GB10),
§4 (both nodes, read-only), §5 (production engine, smoke). **Verified 2026-09-12 in production** (§6.3, §6.4): the GDN line on both ranks on every start;
B's cold start (torch.compile 20.56 s, init engine 61.28 s, ≈ 3 m 46 s to API accepting) and the
kernel-cache reuse across restarts (4.96 / 3.27 s, 27.2 / 28.8 s); the §7.1 B column for
criteria 2, 3 (bar the 950K ratio) and 4; the needle 3/3 at ~950K. **Still unverified:** the
arm64 child digest (§1); the git ancestry "51 commits after f6326f5" (only the code path is
proven); the exact wording of the model-runner banner line on B; whether MRV2's
`warmup_kernels()` batches ≥ 2 prefills; the 120-min soak (in progress); `validate_long_context.py`
and the orchestrator smoke on B; the 48–72 h canary (not started); every secondary test.
