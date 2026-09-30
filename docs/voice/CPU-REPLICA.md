# The CPU speech replica

A third copy of `openai/whisper-large-v3` that runs on ten CPU cores of the worker Spark
(spark-476e) and no GPU. The two GPU replicas (`scripts/whisper.sh`, one per Spark) still serve
speech. The orchestrator sends this copy a clip only when **every GPU replica is already decoding
one**, and only when the clip **can finish inside its deadline at this copy's measured speed**.

Owner decision, 2026-09-30: "use both GPU and CPU": yes, a CPU copy of whisper-large-v3 on the
worker's fast cores, GPU replicas preferred, the same accuracy measured before it is enabled.

---

## Why a CPU copy

- **Chat pays for every GPU decode.** The chat model is tensor-parallel across both Sparks, so a
  busy GPU speech replica on either one slows every chat answer (107 → 53 tok/s for one, 28 for
  both, measured 2026-09-28). A CPU decode does not touch the GPU. Its cost is memory bandwidth,
  and that cost is measured below.
- **The GPU replicas decode one clip at a time.** When both are busy, for example during a video
  analysis (`VIDEO_ASR_CONCURRENCY=2`, one clip per Spark), the next dictation waits behind a clip of
  up to 90 s. The CPU copy can take that dictation instead.
- **A GPU replica can go down.** The CPU copy is also the last resort when every GPU replica fails
  the same call.

## Where it runs

```
   head (Spark 1)                          worker (Spark 2, spark-476e)
 ┌───────────────────┐   ASR_BASE_URLS   ┌──────────────────────────────────────────┐
 │ orchestrator      ├──────────────────►│ whisper (GPU)        :30007  preferred    │
 │  RoutedProvider   │                   │                                          │
 │   GPU first,      │ ASR_CPU_BASE_URLS │ whisper-cpu          :30008  overflow     │
 │   CPU on overflow ├──────────────────►│  cpuset 5-9,15-19 (Cortex-X925), cpus 8  │
 └──────┬────────────┘                   │  no GPU, read-only root, uid 10008       │
        │ 172.17.0.1:30007 (head GPU)    └──────────────────────────────────────────┘
        ▼
```

- **Worker only.** The head's memory is off limits for new services (owner, 2026-09-16), and
  `scripts/whisper-cpu.sh` refuses anything but dual mode.
- **The ten Cortex-X925 cores** (cpus 5-9 and 15-19, 3.9 GHz), with a quota of eight cores' worth
  of time. cpus 0-4 and 10-14 are the slower A725 cluster, where the chat model's worker-rank
  threads, dockerd and the OCR engine's host side keep running.
- **Its own Compose project** (`sf-local-ai-whisper-cpu`, `compose/compose.whisper-cpu.yaml`), on
  its own port (30008). It binds the worker's management address itself with host networking, the
  same as the GPU replica and for the same reason: a published port does not survive a reboot there.
- **OOM order 850**: after the OCR engine (900), before the GPU speech replica (800). This copy is
  overflow capacity, so the kernel kills it before the replica that serves speech normally. OCR goes
  first because it frees about 15 GiB of the unified memory a UVM-driven OOM is short of; this copy
  frees about 2.3 GiB of host memory.
- **A 4 GiB memory ceiling with no swap** (`mem_limit`, `memswap_limit`). This is the only hard
  memory limit in the project, recorded as a decided exception in
  `launcher/tests/test_oom_score_adj.py`. The rule against limits exists because GB10 engine
  memory is not charged to the cgroup. None of that applies here: no GPU code runs, so all of this
  service's memory is host RSS. The measured peak is 2.0 GiB (2.002 GiB in the container with a
  4 GiB limit).

  Without the limit, a leak would reach the global OOM killer. That killer takes the OCR engine
  (900) before this replica (850). With the limit, only this copy is killed, and the router falls
  back to the GPU replicas. The model is never swapped out either, because a swapped model would
  decode slower than the router's estimate.
- **A hung decode is killed.** A decode that runs past `60 s + 2.5 × seconds of audio` is killed.
  That bound is five to seven times the slowest clip measured. The request gets a 503, the next
  clip starts a fresh decoder (about 9 s to load), and three deaths in a row end the process so
  that Docker restarts it. This is the GPU replica's CUDA-death rule. Without the kill, a native
  hang would hold the one-clip lock for good while `/health` kept saying ready. Verified in the
  built image with a 1 s bound: 503, 503, 503, then exit 3.

## The same model, the same decode, the same contract

- **Weights.** The GPU replicas' pinned revision (`06f233fe06e7`) is converted on the worker by
  whisper.cpp's own converter. This happens once, in a network-less container, from the files
  already in the model cache. The result is quantised to **q8_0** (8-bit blocks of 32 weights, one
  fp16 scale each). Both files are checked against SHA-256 pins in `scripts/whisper-cpu.sh`
  (f16 `e4fb6f47…`, q8_0 `37efc6b6…`, reproduced by two different whisper.cpp builds). The server
  checks the q8_0 hash again at every start and refuses any other file.
- **Decode settings** match the transformers pipeline's defaults one by one
  (`compose/whisper-cpu/wcpp_worker.cpp`):
  - greedy, temperature 0, no temperature fallback;
  - no conditioning on previous text;
  - transcribe, never translate;
  - blank and non-speech tokens suppressed;
  - first timestamp no later than 1.0 s;
  - timestamps only for segments or clips of 30 s and longer;
  - sequential long form;
  - no per-window silence skipping.
- **The silence gate and the language are measured the way the GPU replica measures them.** The
  pre-pass runs the first 30 s with `<|startoftranscript|>` alone and reads P(`<|nospeech|>`) at
  that position, then takes the language as the argmax over the language tokens at the same
  position. whisper.cpp's own `no_speech_prob` is read after the whole prompt. That is a different
  number, so it is not used.
- **HTTP contract.** `compose/whisper-cpu/server.py` exposes compose/whisper/server.py's routes,
  form fields, response fields, gate, limits and error statuses. `/health` carries a few extra keys:
  `backend`, `busy`, `threads`, `seconds_per_audio_second` and `model_sha256`.
  `orchestrator/tests/test_whisper_cpu_server.py` reads both files and fails when they drift apart.
- **When the GPU replica's decode settings change, this file must follow.** The
  whisper-accuracy work (2026-09-30) is measuring decode and language changes for the GPU replicas.
  Any change that ships there (a language hint, a prompt, beam search) has to be made in
  `wcpp_worker.cpp` too, and measured on the same sets, before both replicas answer alike again.

## Measured (2026-09-30, worker, pinned to cpus 5-9,15-19, 8 threads)

The same clips and the same normaliser as the GPU baselines (NFKC, lower case, Unicode P*/S*
dropped, combining marks kept): LibriSpeech test-clean 60, FLEURS en 40, FLEURS hi 40, and a
10-minute MUCS 2021 Hindi-English subset (107 of the 364 segments the GPU was scored on, spread
evenly, 576 s). The GPU numbers are the **production replica's own per-utterance answers**,
recorded the same morning with verbose_json and the gate on. Their totals reproduce the published
baselines exactly: 4.21 / 6.81 / 41.93 %. The comparison is therefore paired, utterance by
utterance.

**The worker was shared while this was measured.** Another track's throwaway GPU whisper replica
and a test Postgres were running, and they added one to two and a half cores of other load on the
same ten cores. Every speed below includes that contention.

### Accuracy: whisper.cpp q8_0 (CPU) against the GPU replica (fp16), paired

| set | words | GPU WER | CPU WER | CPU − GPU (95 % CI, paired bootstrap) | utterances better / worse / same | identical text | languages agree |
|---|---:|---:|---:|---|---|---:|---:|
| LibriSpeech test-clean (60) | 1,235 | 4.21 % | **3.89 %** | −0.32 pp (−1.51, +1.10) | 8 / 5 / 47 | 46 / 60 | 60 / 60 |
| FLEURS English (40) | 896 | 6.81 % | **7.03 %** | +0.22 pp (−0.22, +0.65) | 1 / 3 / 36 | 36 / 40 | 40 / 40 |
| FLEURS Hindi (40) | 997 | 41.93 % | **44.73 %** | +2.81 pp (−0.22, +6.62) | 4 / 7 / 29 | 20 / 40 | 40 / 40 |
| MUCS Hindi-English, 10 min (107) | 1,133 | 65.40 % | **65.84 %** | +0.44 pp (−5.37, +6.72) | 16 / 14 / 77 | 56 / 107 | — |

- **Every interval contains zero.** The sign tests give p = 0.58, 0.63, 0.55 and 0.86, so no
  difference is established on any set. The CPU copy picked the same language as the GPU replica on
  every one of the 140 utterances that record one. That includes the same nine FLEURS Hindi
  utterances written in Urdu script, a whisper-large-v3 behaviour that the CPU copy reproduces too.
- **Hindi has the widest interval and a positive point estimate** (+2.8 pp). It is inside the noise
  of 40 utterances. The f16 control below separates quantisation from engine differences.
- **The silence gate is the GPU replica's gate.** P(no speech) differed from the GPU replica's by at
  most 0.015 / 0.063 / 0.008 on the three sets, and no utterance landed on the other side of 0.6.
  Ten seconds of digital silence scores 0.710 on the CPU against 0.708 on the GPU.
  The three MUCS clips the GPU replica gated were gated here too.
- **Long form works.** The 60 LibriSpeech utterances joined into four 1.6-2.8 min clips (the
  sequential long-form path, 24 windows) scored **3.24 %** against 3.89 % for the same words as
  short clips. So nothing is dropped or repeated at the window seams. Two 5-minute MUCS lecture
  spans decoded as one clip each scored 31.65 % document WER (skeleton 29.72 %).

### Engines and builds compared

| backend | build / type | WER LibriSpeech / FLEURS-en / FLEURS-hi | seconds per 30 s window (encoder) | decode ms/token | RTF | peak RSS |
|---|---|---|---:|---:|---:|---:|
| **whisper.cpp v1.9.4 q8_0** | armv8.6-a + dotprod + i8mm + fp16 + KleidiAI | **3.89 / 7.03 / 44.73 %** | **1.9** | 15-28 | **0.45-0.60** short sets, 0.21-0.46 long form | **2.0 GiB** |
| whisper.cpp f16 | same build | 4.21 / 6.92 / 43.53 % | 4.9 | 39-45 | 1.25-1.73 | 3.3 GiB |
| whisper.cpp q5_0 | same build | 4.13 / — / 44.13 % | 6.4 | 42 | 1.26-1.71 | 1.5 GiB |
| faster-whisper 1.2.1 / CTranslate2 4.8.2 int8 (= int8_float32 on CPU) | PyPI aarch64 wheel (NEON, Ruy, OpenBLAS) | 2.59 / — / — (a different decode, below) | 3.3-4.4 | 105-285 | 2.33 | 3.0 GiB |
| CTranslate2 float32 | same | not run | 6.6 | 251 | — | — |
| transformers 5.16.1 fp32 (the GPU replica's own code path) | torch 2.14 CPU, oneDNN | not run | 3.0-3.5 | 256 | — | 6.5 GiB |

The RTF column is for the same short sets, one clip after another, pinned to the ten X925 cores
with 8 threads. The worker was shared throughout, as described above. Paired against the GPU
replica in the same way as the chosen build:

| backend | LibriSpeech CPU − GPU (95 % CI) | FLEURS-en | FLEURS-hi |
|---|---|---|---|
| whisper.cpp f16 | 0.00 pp (−1.13, +1.40), text identical 47/60 | +0.11 (−0.36, +0.58) | +1.60 (−0.85, +5.30) |
| **whisper.cpp q8_0** | −0.32 (−1.51, +1.10), 46/60 | +0.22 (−0.22, +0.65) | +2.81 (−0.22, +6.62) |
| whisper.cpp q5_0 | −0.08 (−1.27, +1.35), 45/60 | — | +2.21 (−0.39, +5.92) |
| faster-whisper int8 | **−1.62 (−3.04, −0.52)**, 47/60 | — | — |

- **f16 reproduces the GPU replica's LibriSpeech score exactly** (4.21 %). That shows the
  conversion and the decode settings are faithful. q8_0 costs nothing measurable against f16 and
  decodes 2.5-3 times faster. f16 and q5_0 are slower than real time on short clips, because
  KleidiAI's kernels cover q8_0 and q4_0 but not q5_0 or f16.
- **faster-whisper is not the same decode.** Its lower LibriSpeech score is mostly quotation
  marks. The transformers pipeline and whisper.cpp both write dialogue as `'…'`. The normaliser
  keeps apostrophes, so those marks count as errors. faster-whisper decodes in timestamp mode and
  leaves them out. Its no-speech probabilities are up to 0.15 away from the GPU replica's.

  It is also slower than real time here (RTF 2.33). So it would be a different product, and a
  slower one.

whisper.cpp build flags matter more than the quantisation. Encoder seconds for one 30 s window,
q8_0, 8 threads:

| build | encoder |
|---|---:|
| `-mcpu=native` (gcc 13 does not know the X925: plain armv8, no dotprod / i8mm / fp16) | 6.0-6.4 s |
| armv9.2-a + SVE2 + i8mm + bf16 | 9.0 s (ggml's SVE paths are slower here) |
| armv8.6-a + dotprod + i8mm + fp16 | 2.4-2.9 s |
| **... + Arm KleidiAI** (the image's build) | **1.9 s** |

**Why whisper.cpp q8_0.** It is the only engine whose decoder is fast on these cores: 15-28 ms per
token, against 105-285 for CTranslate2's aarch64 wheel and 256 for transformers. It is also the
fastest whisper.cpp variant here, 2.5-3 times faster than f16 or q5_0. Its accuracy is the GPU
replica's within noise, and it holds 2.0 GiB.

### Speed of the chosen replica

Per clip, with the pre-pass (1.9 s) and one encoder window per ~20-30 s of audio:

| clips | audio | time | seconds per audio second |
|---|---:|---:|---:|
| ≤ 10 s (186) | 1,046 s | 945 s | 0.90 (the fixed cost dominates) |
| 10-30 s (59) | 801 s | 338 s | 0.42 |
| > 30 s (6 + long form) | 559 s | 126 s | 0.23 |
| English long form, 1.6-2.8 min (4) | 498 s | 106 s | 0.19-0.25 |
| Hindi-English long form, 5 min (2) | 595 s | 257 s | 0.41-0.46 |

The GPU replica's long form ran at 0.45 s/s quiet (2026-09-18). So the CPU copy is slower on short
clips and **about as fast on long ones**.

**The router's estimate**, `ASR_CPU_FIXED_S + seconds × ASR_CPU_S_PER_AUDIO_S` = **8.5 s + 0.45 s/s**.
0.45 is the slowest long-form rate measured. At that rate, 8.5 s puts every one of the 257
measured clips under the line (95 % are under 4.4 s + 0.45 s/s). The router then multiplies the
estimate by `ASR_CPU_DEADLINE_MARGIN` (1.5):

| clip | deadline | estimate × 1.5 | sent to the CPU when the GPUs are busy? |
|---|---:|---:|---|
| 30 s recording window | 120 s (`VOICE_SESSION_WINDOW_TIMEOUT_S`) | 33 s | yes |
| 90 s video window | 600 s | 74 s | yes |
| WebM of unknown length (costed at 600 s) | 600 s | 418 s | yes |
| 30 s window, if the window timeout were 30 s | 30 s | 33 s | no, waits for a GPU |

### Cost to the chat model

`chat_probe`: 300 tokens streamed from the head's vLLM, `enable_thinking` false, temperature 0,
decode tok/s between the first and the last token. The CPU replica decoded two-minute English
clips back to back, the worst case. It was frozen (SIGSTOP, "off") and resumed ("on") in
interleaved pairs, so that drift in the other tenants' load lands on both arms equally.

| state | probes | decode tok/s, mean (median) |
|---|---:|---:|
| before (no CPU replica process) | 6 | 66.3 (62.2) |
| **off**: loaded, idle | 26 | **70.7 (70.5)** |
| **on**: decoding back to back | 26 | **68.2 (66.5)** |
| after | 6 | 68.6 (58.0) |

**On against off: −3.6 %, 95 % CI −11.6 % to +5.3 %.** The paired differences were −5.9 tok/s
over 6 pairs and −1.5 (median 0.0) over 20. No slowdown is established, and a slowdown larger than
about 12 % is ruled out.

**The noise floor was set by another track.** A throwaway GPU whisper replica on the worker was
decoding (14-37 % SM) during every one of the 64 probes, so chat ran at 55-93 tok/s instead of its
quiet ~107. That is also the state the CPU replica is built for, since it only works while the GPU
replicas are busy. A quiet-GPU re-measurement before enabling is on the checklist below.

## How the orchestrator uses it

`orchestrator/app/asr.py`, `RoutedProvider` ("THE CPU REPLICA"):

1. **A GPU replica with nothing in flight takes the clip.** Least active first, exactly as before.
   With no CPU replica configured, the order is identical to what it always was (a test holds this).
2. **Every GPU replica busy or standing down → the CPU replica goes first, if:**
   - it has no clip in flight (one at a time, `_CPU_MAX_ACTIVE`);
   - it is not standing down after a failure;
   - `(ASR_CPU_FIXED_S + seconds × ASR_CPU_S_PER_AUDIO_S) × ASR_CPU_DEADLINE_MARGIN` fits the call's
     deadline. That deadline is the window timeout for a recording-session window and
     `ASR_TIMEOUT_S` otherwise. A WAV clip's length comes from its header (`asr.clip_seconds`). A
     clip of unknown length is costed at `ASR_MAX_AUDIO_SECONDS`.

   Otherwise the clip queues on the least busy GPU replica as before. **A clip that would miss its
   deadline is never sent to the CPU.**
3. **The CPU replica is the last resort after every GPU replica has failed the same call**, under
   the same deadline rule.
4. **A CPU failure falls back to the GPU queue.** A CPU read timeout is not re-sent: the replica
   still has the clip, and the caller's deadline has passed. This is the same rule as for a GPU
   replica.
5. **Dictation's optional second decode** (gated clips) is costed with the CPU's own numbers
   (`VLLMAudioProvider.decode_cost_s`) when it runs on the CPU. GPU replicas keep the loaded GPU rate.
6. **Pool sizes.** The legacy dictation pool gains exactly the CPU replica's one slot. The
   recording-session gate (`VOICE_SESSION_ASR_CONCURRENCY`) and the video pool
   (`VIDEO_ASR_CONCURRENCY`) are unchanged, and `/v1` still routes over `ASR_BASE_URLS` only.

### When "every GPU replica is busy" actually happens

"Busy" means that the replica has a clip in flight that this orchestrator started. That includes
dictation, recording-session windows, video windows and `/v1` public clips (`counted_in_dictation_routing`
counts those too). Other tenants that call the raw port directly are not counted.

**Recording sessions alone never make both GPU replicas busy.** `VOICE_SESSION_ASR_CONCURRENCY`
defaults to 1: one session window decodes at a time across the whole fleet, and the others wait in
the session gate. This is the owner's trade, which caps the chat cost of dictation at the one-node
figure. So while only people dictating are using speech, one GPU replica is always free, and the
CPU replica is never reached. It is reached when:

- a **video analysis** holds both GPU replicas (`VIDEO_ASR_CONCURRENCY=2`, 90 s windows), and a
  dictation window, a legacy clip or a public clip arrives;
- **public `/v1` audio or the legacy dictation path** fills the second GPU replica while a session
  window holds the first;
- **a GPU replica is standing down or fails a call** (a node rebooting, a CUDA death).

**What that buys.** A 30 s window behind a 90 s video window waits up to about 15 s on a GPU
replica and then decodes in 2-3 s. On the CPU it takes about 13-15 s. So for a long window the
latency is roughly a tie. The gains are elsewhere:
- a short window (5-10 s) is back in about 5-9 s instead of waiting;
- a second or third queued clip does not wait behind the first;
- the GPU replicas do no extra work, so the chat cost stays where the video already put it;
- dictation keeps working when a GPU replica is down.

**Owner decision (not built): a CPU lane for recording sessions.** The session gate could admit a
second window only onto the CPU replica. That window would be pinned to the CPU tier, so the free
GPU is not used and the chat cost stays at the one-node figure. Recording bursts from several
people would then use the CPU even when no video is running. This changes the gate's
one-at-a-time rule, which is a product trade, so it is not in this branch.

**Metrics**:
- `asr_route_total{tier="gpu"|"cpu"}`: clips sent to each tier.
- `asr_cpu_overflow_total{outcome}`: what happened when every GPU replica was busy: `sent`,
  `cpu_busy`, `cpu_down` or `too_long` (deadline).
- `GET /audio/health` lists every replica with its `tier`, in-flight count and stand-down state.
- The replica's own `/health` reports `busy` and its running `seconds_per_audio_second`.

## Running it

```bash
scripts/whisper-cpu.sh up       # convert + verify the weights (once), build, start, wait, record ASR_CPU_BASE_URLS
scripts/whisper-cpu.sh status   # container state and the replica's /health
scripts/whisper-cpu.sh verify   # transcribe the JFK clip and print the time taken
scripts/whisper-cpu.sh logs
scripts/whisper-cpu.sh down     # stop it and empty ASR_CPU_BASE_URLS; nothing else is touched
```

`up` writes `ASR_CPU_BASE_URLS` into `.env`. The orchestrator reads it on its next recreate
(`./techsara up`, a routine up; the main model is not restarted). Until then nothing routes to the
replica. `down` empties the key.

**Before the first `up`** (owner, root):
- Copy this checkout's `scripts/host-guard.sh` to the worker.
- Run `sudo bash ~/.techsara-cluster/host-guard.sh apply --role worker`, then `verify`. The guard
  must accept 30008 from the head only, like 30007. The replica has no authentication, as the GPU
  replica has none.

## Checklist before enabling (not yet done)

1. The host guard update above (sudo).
2. `scripts/whisper-cpu.sh up` on the worker, then `verify`.
3. A chat-cost re-measurement with the GPU replicas quiet. The 2026-09-30 number was taken with
   another tenant decoding on the GPU throughout.
4. Re-read the paired accuracy table if the GPU replica's decode settings change first (the
   whisper-accuracy work).

## Known limits

- **Short clips are slower than on a GPU**: about 5 s for a 10 s clip, against ~1.2 s on a quiet
  GPU replica. That is the point of "overflow only". A person waits for the CPU only when they
  would otherwise have waited behind someone else's clip.
- **One clip at a time.** whisper.cpp could decode two clips on four threads each, but the total
  would not be higher, and the chat cost would be.
- **No batching, no streaming**, the same as the GPU replica.
- **Other workloads on the X925 cores slow it.** The estimate above was measured with 1-2.5 cores of
  other load present. The 1.5 margin is the headroom, and `seconds_per_audio_second` on `/health`
  shows the replica's real running rate.
