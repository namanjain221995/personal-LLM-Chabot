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
  both, measured 2026-09-28 on the main model, Qwen3.6-35B-A3B). A CPU decode does not touch the
  GPU. Its cost is memory bandwidth, and that cost is measured below, mostly on
  nvidia/Qwen3.8-27B-NVFP4, which was the main model only from 15:49 to 22:38 IST on 2026-09-30.
  The main model is Qwen3.6-35B-A3B again, so those numbers do not describe the running model.
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
  of time. cpus 0-4 and 10-14 are the slower A725 cluster. **Nothing keeps the chat model's
  worker rank off the X925 cores:** its container has no cpuset (`docker inspect` shows
  `CpusetCpus` empty), so the scheduler may run its threads on the same ten cores as this replica.
  Its busiest thread was on cpu 3 when sampled, but that is the scheduler's choice, not a rule.
  The measured chat cost below includes whatever sharing happened. Pinning the vLLM worker away
  from these cores would need a model restart and a new chat-cost measurement, so it is not done.
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
- **A decoder that cannot be restarted counts as a death too.** Before 2026-09-30 it was a 500
  that was never counted, so a replica whose decoder could no longer start (its model gone from
  the bind mount, a load that dies) answered 500 to every clip and Docker never restarted it.
  Found and fixed in the end-to-end run below. A restart that never says ready is killed after
  60 s (the hang budget's fixed part) and counted the same way (fixed 2026-10-01).

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
evenly, 576 s). Hindi was then widened to 200 FLEURS utterances. The GPU numbers are the
**production replica's own per-utterance answers**, recorded the same morning with verbose_json
and the gate on. The 160 added Hindi utterances came from a throwaway replica of the production
image, whose server.py is byte-identical. The GPU totals reproduce the published baselines
exactly: 4.21 / 6.81 / 41.93 %. The comparison is therefore paired, utterance by utterance.

**Every pair below is `response_format=verbose_json`**, the format of recording-session windows,
video windows and the legacy dictation's second pass. The legacy dictation's first pass asks for
`json` (timestamps off for clips under 30 s), and whisper.cpp's text is not the same in that mode:
the independent verifier's re-run of the committed image reproduced the recorded verbose_json
answers on 10 of 10 clips, and in json mode came back with different text on 8 of 24
(punctuation, case and three Hindi words). **json mode has not been paired against the GPU replica's json mode.**
json-mode pairing: not measured yet (for the measuring agent to fill in: clips per set, CPU − GPU
WER in pp with the paired-bootstrap 95 % CI, and identical text n/N).

**The worker was shared while this was measured.** Other tracks' throwaway GPU whisper replicas,
test Postgres servers and benchmarks were running, and they added one to two and a half cores of
other load on the same ten cores. Every speed below includes that contention.

### Accuracy: the image's decoder (whisper.cpp q8_0, 443-token windows) against the GPU replica (fp16), paired

| set | words | GPU WER | CPU WER | CPU − GPU (95 % CI, paired bootstrap) | utterances better / worse / same | identical text | languages agree |
|---|---:|---:|---:|---|---|---:|---:|
| LibriSpeech test-clean (60) | 1,235 | 4.21 % | **3.89 %** | −0.32 pp (−1.51, +1.10) | 8 / 5 / 47 | 46 / 60 | 60 / 60 |
| FLEURS English (40) | 896 | 6.81 % | **7.03 %** | +0.22 pp (−0.22, +0.65) | 1 / 3 / 36 | 36 / 40 | 40 / 40 |
| FLEURS Hindi, the baseline 40 | 997 | 41.93 % | **42.53 %** | +0.60 pp (−0.50, +1.82) | 4 / 6 / 30 | 20 / 40 | 40 / 40 |
| FLEURS Hindi, 200 | 4,856 | 39.42 % | **39.11 %** | −0.31 pp (−1.90, +0.82) | 20 / 36 / 144 | 101 / 200 | 198 / 200 |
| MUCS Hindi-English, 10 min (107) | 1,133 | 65.40 % | **69.99 %** | +4.59 pp (−5.19, +18.5) | 16 / 14 / 77 | 56 / 107 | — |

- **Every interval contains zero.** English shows no difference at all: the sign tests give
  p = 0.58 and 0.63, and the transcripts are the ones the decoder gave before the window-cap patch
  below.
- **Hindi (200): equal overall, with a small tilt per utterance.** The pooled rate is within noise,
  and the CPU copy is ahead only because of two utterances where the GPU replica wrote the Hindi in
  Urdu script (the two language disagreements; the CPU copy scored 25 and 20 errors fewer). On the
  other 198 the CPU copy makes about **0.6 pp more errors** (39.25 against 38.63 %), almost all
  one-word spelling differences. It is worse on 36 utterances (27 of them by one word) and better
  on 20 (15 by one word), and that imbalance is unlikely to be chance (sign test p = 0.044).
  **About half of it is q8_0 and half is whisper.cpp.** The f16 weights through the same decoder,
  run on the 56 utterances where q8_0 and the GPU differ, put the 36 worse ones down to q8_0 alone
  on 16, whisper.cpp on 17 and both on 3. Without the two Urdu-script utterances, those 54 score
  31.09 % on the GPU, 32.11 % with f16 and 33.28 % with q8_0. f16 against the GPU shows no
  significant tilt (14 better / 20 worse, p = 0.39); q8_0 against f16 does (7 / 21, p = 0.013).
  f16 wrote the same two utterances in Urdu script as the GPU did, so q8_0's lead there is luck
  in the language pick.
- **MUCS: one clip.** The whole +4.6 pp is one 8-second utterance. The CPU copy decodes it into a
  repetition loop that runs until the window's 443-token limit (75 errors on 24 words). The GPU
  replica decodes it cleanly (9 errors). Without that clip the CPU copy is 1.3 pp *better*
  (64.74 against 66.01 %), and the other 106 utterances split 16 better / 13 worse. Before the
  window-cap patch the same loop was cut at 220 tokens (28 errors), so the unpatched MUCS number
  (65.84 %) looked closer. Neither replica has a loop guard: the GPU replica's temperature fallback
  was measured and rejected on 2026-09-08.
- **What q8_0 costs on noisy Hindi-English, and what whisper.cpp itself gives back.** The f16
  weights through the same decoder, run on the 30 MUCS utterances where q8_0 and the GPU differ:
  the loop clip decodes cleanly (4 errors), and so do two clips q8_0 left empty (below). Of the 14
  utterances where q8_0 is worse than the GPU, 6 are q8_0's own doing (f16 scores what the GPU
  scores), 6 are whisper.cpp's and 2 are mixed. Of the 16 where q8_0 is better, 13 are
  whisper.cpp's. On those 30 utterances f16 scores 49.7 %, the GPU 65.7 % and q8_0 79.5 %
  (f16 better than the GPU on 18, worse on 6, sign test p = 0.02). So whisper.cpp's own decode
  is, if anything, better than the GPU pipeline on this audio, and q8_0 gives part of that back.
- **The silence gate matches on clean speech, not always on noisy Hindi-English.** P(no speech)
  differed from the GPU replica's by at most 0.015 / 0.063 / 0.047 on LibriSpeech, FLEURS English
  and FLEURS Hindi, and no utterance landed on the other side of 0.6. Ten seconds of digital
  silence scores 0.710 on the CPU against 0.708 on the GPU. On MUCS, though, the CPU copy gated
  3 of the 107 clips (P = 0.63-0.66) that the GPU replica transcribed. f16 scores two of them
  0.21-0.22: q8_0 raises P(no speech) by 0.1-0.45 on 7 of the 30 MUCS clips measured both ways
  (median +0.004). Both dictation paths ask a gated window again without the gate when
  voice-activity detection heard speech in it (`dictation.py`, and the legacy path's second pass),
  so this costs a second decode rather than the words. The first version of this page said the GPU
  had gated those clips too; its reference has no empty answer on this subset.
- **Long form works.** The 60 LibriSpeech utterances joined into four 1.6-2.8 min clips (the
  sequential long-form path, 24 windows) scored **3.24 %** against 3.89 % for the same words as
  short clips. So nothing is dropped or repeated at the window seams. Two 5-minute MUCS lecture
  spans decoded as one clip each scored **23.77 %** document WER (skeleton 21.62 %). The GPU
  replica's long form of the same two spans has not been recorded yet (below).

### The 220-token window cap (found and fixed 2026-09-30)

The first measurement put Hindi at +2.8 pp against the GPU (44.73 against 41.93 % on the 40
baseline clips). The cause was not the quantisation and not the model. Upstream whisper.cpp stops
a 30 s window after `n_text_ctx / 2 - 4` = **220 new tokens**, because it keeps half of the
448-token context for past text. The replica never passes past text (`no_context`). The GPU
replica's transformers pipeline allows the whole context minus the prompt.

Dense Hindi reaches 220 tokens. On 5 of 200 FLEURS Hindi clips (17-24 s long), whisper.cpp cut the
window mid-character and decoded its tail again as a second window. So a phrase was written twice,
costing 2 to 19 extra word errors per clip against the GPU's answer.

The image patches that one line to `n_text_ctx - prompt - 1` (**443**), and the build fails if the
line is not found exactly once (`compose/whisper-cpu/Dockerfile`, held by
`test_the_image_lifts_whisper_cpps_220_token_window_cap`). With the patch:

| | before (220) | after (443) | GPU |
|---|---:|---:|---:|
| the five clips: windows / word errors | 2 each / 111 | 1 each / 58 | 55 |
| FLEURS Hindi, 40 baseline clips | 44.73 % | 42.53 % | 41.93 % |
| FLEURS Hindi, 200 clips | 40.49 % | 39.11 % | 39.42 % |
| FLEURS Hindi, 160 clips: seconds per audio second | 0.80 | 0.54 | |
| MUCS Hindi-English long form, 2 × 5 min: document WER (skeleton) | 31.65 % (29.72) | 23.77 % (21.62) | not recorded |
| LibriSpeech, FLEURS English, LibriSpeech long form | 3.89 / 7.03 / 3.24 % | the same | 4.21 / 6.81 / — |

Four of the five clips now score exactly the GPU's errors. English never reaches 220 tokens in a
window, so its transcripts are unchanged. Hindi is also faster, because no window is decoded twice.
MUCS long form improved the most (−7.9 pp). Its GPU long-form reference was being recorded when the
run was paused for the main-model swap, so that one row is not paired yet.

### Engines and builds compared

These runs came first and used whisper.cpp's upstream 220-token window, so their FLEURS Hindi
numbers carry the repeated tails described above. The image's decoder (443-token windows) scores
3.89 / 7.03 / 42.53 % on the same three sets.

| backend | build / type | WER LibriSpeech / FLEURS-en / FLEURS-hi | seconds per 30 s window (encoder) | decode ms/token | RTF | peak RSS |
|---|---|---|---:|---:|---:|---:|
| **whisper.cpp v1.9.4 q8_0** | armv8.6-a + dotprod + i8mm + fp16 + KleidiAI | **3.89 / 7.03 / 44.73 %** (220-token windows) | **1.9** | 15-28 | **0.45-0.60** short sets, 0.21-0.46 long form | **2.0 GiB** |
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
fastest whisper.cpp variant here, 2.5-3 times faster than f16 or q5_0, which are slower than real
time on short clips. Its word error rate is the GPU replica's within noise on every set, and it
holds 2.0 GiB. Where it is measurably not identical (the small per-utterance tilt on Hindi, and
one loop and three extra gated clips on noisy Hindi-English), part is q8_0's and part is
whisper.cpp's own decode. f16 decodes the loop clip cleanly, does not gate the two gated clips it
was run on, and has half the Hindi tilt. It would close that part at 2.5-3 times the time and
3.3 GiB, so a 30 s window would take ~45 s, slower than waiting for a GPU replica behind a video
window. That trade is left to the owner.

### Speed of the chosen replica

Per clip, with the pre-pass (1.9 s) and one encoder window per ~20-30 s of audio. These are the 413
clips above, decoded one after another by the image's decoder, with other tenants on the same cores:

| clips | audio | time | seconds per audio second |
|---|---:|---:|---:|
| ≤ 10 s (260) | 1,637 s | 1,565 s | 0.96 (the fixed cost dominates) |
| 10-30 s (145) | 2,052 s | 944 s | 0.46 |
| > 30 s (8, long form included) | 1,154 s | 273 s | 0.24 |
| English long form, 1.6-2.8 min (4) | 498 s | 87 s | 0.17 |
| Hindi-English long form, 5 min (2) | 595 s | 167 s | 0.28 |

The GPU replica's long form ran at 0.45 s/s quiet (2026-09-18). So the CPU copy is slower on short
clips and **as fast or faster on long ones**.

**Other load on the worker slows it, a lot.** The MUCS set ran at 1.38 s/s in the evening run
against 1.00 s/s in the morning, although only one of its 107 transcripts changed. Another
track's GPU whisper replica, test Postgres servers and other processes were busy on the worker at
the time. One 9 s clip took 20.0 s instead of 6.7 s. The replica's `/health` reports its running
`seconds_per_audio_second`, so this is visible.

**The router's estimate**, `ASR_CPU_FIXED_S + seconds × ASR_CPU_S_PER_AUDIO_S` = **8.5 s + 0.45 s/s**.
0.45 is the slowest long-form rate measured. With the image's decoder, 406 of the 413 clips are
under that line, and 412 are under the line × 1.5. The one exception is the 9 s clip above
(20.0 s against 18.8 s). The estimate is an admission check against deadlines of 120 s and 600 s,
not a latency promise, so none of these comes near a deadline. The router multiplies the estimate
by `ASR_CPU_DEADLINE_MARGIN` (1.5):

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
interleaved pairs, so that drift in the other tenants' load lands on both arms equally. Around every
probe the GPU use of both nodes was sampled (`nvidia-smi pmon`), and in the second batch also the
vLLM engine's own token counters, so a probe that shared the engine with another user's prefill
could be seen.

**nvidia/Qwen3.8-27B-NVFP4 (dense, tensor-parallel across both Sparks), the main model from 15:49
to 22:38 IST on 2026-09-30 and not since, 17:51-18:25, 30 pairs in two batches, the image's
decoder.** These numbers describe the 27B only:

| state | probes | decode tok/s, mean (median) | of which no GPU whisper decoding at either sample: mean |
|---|---:|---:|---:|
| before (no CPU replica process) | 12 | 16.5 (15.9) | 18.9 (3 probes) |
| **off**: loaded, idle | 30 | **17.0 (15.9)** | **20.6** (6) |
| **on**: decoding back to back | 30 | **16.3 (17.2)** | **19.2** (8) |
| after | 12 | 17.8 (18.0) | 20.4 (3) |

- **All 30 pairs: on against off −4.1 %, 95 % CI −13.7 % to +6.6 %.** On was slower in 18 pairs
  and faster in 12 (sign test p = 0.36). The two batches gave +1.6 % (14 pairs) and −8.5 %
  (16 pairs).
- **With the GPU quiet, the cost shows: −6.8 %, 95 % CI −11.2 % to −2.0 %** (8 on-probes against
  6 off-probes), or −4.7 % (CI −9.6 % to +0.9 %) against every quiet probe without CPU load. The
  other stratum, with a GPU whisper replica decoding, gives −5.2 % (CI −15.1 % to +5.4 %).
- **So the CPU replica costs the 27B about 5 % of its decode speed while it decodes.** The dense
  27B reads its whole weight set for every token, so it feels memory-bandwidth contention on the
  worker more than the previous 3B-active MoE did. That is right at the 5 % the owner set as the
  gate for a CPU replica on the head (2026-09-30), not clearly under it.
- **A GPU whisper replica costs the 27B about 19 %** in the same data, and probably more while it
  decodes without pause. Probes taken while another track's throwaway GPU replica was decoding ran
  at 16.2 tok/s against 20.1 without it; 43 of the 44 probes under 17 tok/s had it decoding. A clip
  takes the CPU copy four to five times as long as a quiet GPU replica, at about a quarter of the
  cost per second, so per clip the two cost chat roughly the same. What the CPU replica adds is
  capacity and resilience, not a cheaper decode.

**The noise came from the other tenants.** The 27B decodes at ~22 tok/s alone. During these probes
other people were chatting (another request was decoding during all 32 paired probes of the second
batch), and another track's throwaway GPU whisper replica on the worker was decoding in bursts
(64 of the 84 probes saw it; the production replicas were idle at every sample). A re-run on a
quiet engine, counting only probes that ran alone, would narrow the interval; the head-copy branch
(`feat/cpu-whisper-head`) carries `scripts/whisper-cpu-chat-gate.py` for exactly that gate.

**The main model now, Qwen3.6-35B-A3B** (back since 22:38 IST on 2026-09-30; measured that
morning, 26 pairs, the upstream decoder): −3.6 %, 95 % CI −11.6 % to +5.3 %, chat at 55-93 tok/s
with another track's GPU replica decoding. This is the only measurement on the running model.

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
   deadline is not sent to the CPU while the router's picture of the replica is right.** That
   picture is the router's own in-flight count, and it forgets a decode whose caller let go of it
   (a cancelled call, a read timeout, an orchestrator restart) while the replica keeps decoding,
   up to the hang watchdog's `60 s + 2.5 × seconds`. Such a replica is costed as free. It used to
   queue the next clip behind the abandoned decode (the independent verifier: a 30 s window timed
   out at 120 s behind an abandoned 298 s dictation). **Now the replica refuses any clip while it
   is decoding, with a 503 at once** ("busy: the CPU replica is decoding another clip",
   `compose/whisper-cpu/server.py`), and the router takes that clip to the GPU queue in the same
   call. So a clip is never left waiting behind a decode on the CPU. The cost: the replica stands
   down after the refusal (20 s, doubling on repeats up to 600 s, as for any failure), so it may
   sit idle a while after the abandoned decode ends.
3. **The CPU replica is the last resort after every GPU replica has failed the same call**, under
   the same deadline rule.
4. **A CPU failure falls back to the GPU queue.** A CPU read timeout is not re-sent: the replica
   still has the clip, and the caller's deadline has passed. This is the same rule as for a GPU
   replica.
5. **Dictation's optional second decode** (gated clips) is costed with the CPU's own numbers
   (`VLLMAudioProvider.decode_cost_s`) when it runs on the CPU. GPU replicas keep the loaded GPU rate.
6. **Pool sizes.** The legacy dictation pool's permits are the GPU replicas' alone
   (`ASR_MAX_CONCURRENT` × GPU replicas), as before. A CPU replica's one slot is **lent** to a
   clip only when the router would send that clip to the CPU replica first at that moment, and
   given back when the clip ends (`asr._Pool`, `RoutedProvider.cpu_takes_next`). With the CPU
   replica free the pool holds five clips on a two-GPU fleet, as before. While the CPU replica
   stands down, decodes someone else's window or cannot meet the deadline, the pool holds four and
   the fifth caller is told "busy" after `ASR_QUEUE_WAIT_S`. The first version counted the CPU's
   slot as a permit, and that fifth clip went to a GPU replica as its third in flight, past "one
   decoding, one ready" (the independent verifier). The recording-session gate
   (`VOICE_SESSION_ASR_CONCURRENCY`) and the video pool (`VIDEO_ASR_CONCURRENCY`) are unchanged,
   and `/v1` still routes over `ASR_BASE_URLS` only. `ASR_CPU_BASE_URLS` lists each replica once:
   a URL listed twice, with or without a trailing slash, is one replica.

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
scripts/whisper-cpu.sh up       # check the host guard, build, convert + verify the weights (once), start, wait, record ASR_CPU_BASE_URLS
scripts/whisper-cpu.sh status   # container state and the replica's /health
scripts/whisper-cpu.sh verify   # transcribe the JFK clip and print the time taken
scripts/whisper-cpu.sh logs
scripts/whisper-cpu.sh down     # stop it and empty ASR_CPU_BASE_URLS; nothing else is touched
```

`up` writes `ASR_CPU_BASE_URLS` into `.env`. The orchestrator reads it on its next recreate
(`./techsara up`, a routine up; the main model is not restarted). Until then nothing routes to the
replica. `down` empties the key.

**Before the first `up`** (owner, root). The replica has no authentication, as the GPU replica has
none, and the worker's packet filter judges only the ports it lists: until it lists 30008, the
office LAN and the tailnet can reach it. The filter loaded on the worker since 2026-09-27 does not
list it. It has to be changed twice over: in the table loaded now, and in the boot copy
(`/usr/local/sbin/techsara-host-guard`) that loads it again at every reboot, or the next reboot
reopens the port while Docker brings the replica back.

```bash
# on the head, from the deploy checkout (after the merge); CLUSTER_WORKER_SSH, as the scripts use:
scp scripts/host-guard.sh techsphere@10.100.184.2:.techsara-cluster/host-guard.sh
# on the worker:
sudo bash ~/.techsara-cluster/host-guard.sh install-boot --role worker   # the boot copy (does not load)
sudo bash ~/.techsara-cluster/host-guard.sh apply --role worker          # the table, now
sudo bash ~/.techsara-cluster/host-guard.sh verify --role worker         # must pass, boot copy not drifted
```

**`up` enforces this.** Before it builds or starts anything it reads, without root, the table
loaded now (`/run/techsara-host-guard/ruleset.nft`) and the boot copy's own `plan --role worker`,
and it stops unless both list the port in `guarded_ports` and in `head_lan_ports`, printing the
three commands above. `launcher/tests/test_whisper_cpu_guard.py` holds it. Run against the worker
on 2026-09-30, it refused, as it should.

## Verified end to end (2026-09-30, throwaway, nothing in production touched)

**The deploy path, as `up` runs it.** `sync_files`, `build_images` and `ensure_model` were run
from the committed script against the worker, with throwaway image tags, remote directory and
model cache. It was the first build with BuildKit, which `up` uses (the earlier images were built
with the legacy builder): sync 2 s, image 1 min 31 s, convert stage + conversion + both SHA-256
checks 1 min 44 s. The converted f16 and q8_0 files reproduced their pins, and a second
`ensure_model` found the verified file and converted nothing. The first attempt failed on a DNS
blip (see Known limits).

**The orchestrator's own router against a live replica.** A throwaway container from that image
and model, started with the committed compose file (only the image tag, port 30200 and a loopback
bind overridden), was reached over an SSH tunnel. Every setting applied as written: uid 10008,
read-only root, cpuset 5-9,15-19, 8 CPUs, 4 GiB with no swap, oom_score_adj 850, cap_drop ALL,
no-new-privileges, pids 256. It was ready 5.6 s after start. The two GPU replicas were stand-ins in
the test process, speaking compose/whisper/server.py's contract, that could hold a clip (busy)
or fail it (503). Every call went through the product's own entry points (`asr.transcribe`,
`asr.transcribe_segments`, `asr.transcribe_session_window`):

| # | scenario | result |
|---|---|---|
| 1 | a GPU replica is free | it takes the clip; the CPU replica is not touched |
| 2 | both GPU replicas busy, a 10.4 s session window | CPU replica, WER 0 against the LibriSpeech reference, 6.9 s (estimate 13.2 s, ×1.5 = 19.8 s); `sent` |
| 3 | a third clip while the CPU replica decodes | `cpu_busy`, queued on a GPU replica |
| 4 | a clip whose estimate ×1.5 (19.8 s) misses its 15 s deadline | `too_long`, never sent to the CPU |
| 5 | Hindi dictation (legacy path), GPUs busy | CPU replica, Devanagari, `hi`, 5.3 s (estimate 12.7 s) |
| 6 | a 90 s video window, GPUs busy | CPU replica, 11 timestamped segments to 90.0 s, 23.8 s (RTF 0.27, estimate 49 s) |
| 7 | 10 s of digital silence, dictation | gated on the CPU, decoded once more without the gate (CPU-costed), empty draft marked `silent` |
| 8 | both GPU replicas answer 503 | the CPU replica is the last resort and answers; both GPUs stand down |
| 9 | the CPU container stopped | the clip falls back to the GPU queue and the CPU stands down; the next overflow is `cpu_down`; `docker start` ready in 8 s |
| 10 | the decoder killed mid-decode (90 s window) | 503, CPU stood down, the window finishes on a GPU replica; the next clip decodes on a fresh decoder |
| 11 | three decoder deaths in a row | 503 ×3, the process ends, Docker restarts it (RestartCount 0 → 1), ready again in 6.1 s |

**Two defects found this way, both fixed on this branch:**
- **Silence became "you".** Asked again without the gate, the CPU replica answers 10 s of digital
  silence with "you" timed 0.00-0.62 s. The GPU pipeline answers "Thank you." timed to the end
  of its window (0.0-29.98 s). The same code with the same pins was run on CPU fp32 for 3, 10
  and 30 s of silence, and each time the words ran to the end of the clip. The dictation retry
  judged such text by words per covered second only, so "you" scored 1.0 words/s, exactly the
  floor, and reached the composer as a low-confidence "you". On the CPU replica,
  `speech_is_plausible` now drops a gated retry that is nothing but stock phrases, however it is
  timed. That is the rule a gated session window already had (`window_is_plausible`). Every
  measured case keeps its verdict
  (`test_asr_long_and_noisy.py::test_an_invention_timed_to_the_word_is_dropped_too`).
  **The GPU replicas are not affected.** The first version applied the rule to every replica, so a
  real quiet "Okay.", "Thank you." or "Bye." that a GPU replica's retry recovered came back empty
  and marked silent. It now runs only when `VLLMAudioProvider.tier` is "cpu", and GPU dictation is
  what it was before this branch: 0 of 200,000 random cases and none of the verifier's eight
  changed their verdict against the fork point
  (`test_the_stock_phrase_drop_is_the_cpu_tiers_alone`,
  `test_a_gpu_replicas_recovered_short_reply_still_reaches_the_composer`). The builder's owner
  action 5 (accept that change on GPU dictation) is therefore no longer needed.
- **A decoder that could not restart was a 500, never counted.** It is now a 503 that counts toward
  the three-in-a-row exit (`test_whisper_cpu_server.py::test_a_decoder_that_cannot_restart_is_a_503_counted_toward_the_restart`).

**Found by the independent verifier (2026-09-30) and fixed (2026-10-01):**
- **A busy replica queued callers** (medium). A clip that arrived while the replica decoded one
  its caller had let go of waited behind it and could miss its deadline. The replica now answers
  503 "busy" at once, checked before the upload is read and again with no await before the lock
  (`test_a_second_clip_while_one_decodes_is_refused_at_once_not_queued`), and the router takes the
  clip to the GPU queue in the same call
  (`test_a_decode_the_router_let_go_of_meets_a_busy_refusal_and_the_clip_takes_the_gpu_queue`).
- **The stock-phrase rule changed GPU dictation** (low). Now the CPU tier's alone (above).
- **The hang watchdog did not cover a decoder restart** (low). A restart that never printed its
  ready line held the lock with no bound while `/health` said ready. The restart is now under the
  same kind of timer, bounded by the hang budget's fixed part (60 s; a load takes ~9 s): killed at
  the bound, "decoder did not become ready", a 503 counted toward the three-in-a-row exit
  (`test_a_decoder_restart_that_never_says_ready_is_killed_at_its_bound_and_counted`).
- **Duplicate entries in `ASR_CPU_BASE_URLS`** (low) gave one replica two router slots. Each URL is
  now kept once (`test_a_cpu_replica_listed_twice_is_one_replica_with_one_router_slot`).
- **The legacy pool counted the CPU's slot while the CPU could not take a clip** (low). The slot is
  now lent only to a clip the router sends there (How the orchestrator uses it, item 6;
  `test_the_dictation_pool_admits_no_extra_clip_while_the_cpu_is_standing_down`).
- **json-mode accuracy is unpaired** (low): stated under Measured, with a placeholder for the pairing.
- **The A725 sentence** (info): nothing pins the chat model's worker rank; corrected under Where it
  runs. The worker rank is not pinned now (that would restart the model).

## Checklist before enabling (not yet done)

1. The host guard update above (owner, sudo: `install-boot`, `apply`, `verify` on the worker).
   `up` refuses until it is done.
2. `scripts/whisper-cpu.sh up` on the head (it builds and runs on the worker), then `verify`.
3. **Decide on the chat cost.** It was about 5 % on the 27B while the replica decoded (above), but
   the 27B is no longer the main model. On the running Qwen3.6-35B-A3B the one measurement is
   −3.6 % (CI −11.6 % to +5.3 %, 26 pairs, the upstream decoder). A quiet-hours re-run on it with
   more pairs would narrow that. Fewer threads (`WHISPER_CPU_THREADS` and `cpus` in
   compose/compose.whisper-cpu.yaml) would lower it and slow the replica, and the router's
   estimate would then have to be re-measured.
4. **Pair json mode** (the legacy dictation's first pass) against the GPU replica, as described
   under Measured.
5. Re-read the paired accuracy table if the GPU replica's decode settings change first (the
   whisper-accuracy work).
6. Record the GPU replica's long form of the two MUCS spans (five minutes of worker GPU, off-hours)
   to pair the one long-form Hindi-English row.

## Known limits

- **Short clips are slower than on a GPU**: about 6 s for a 10 s clip, against ~1.2 s on a quiet
  GPU replica. That is the point of "overflow only". A person waits for the CPU only when they
  would otherwise have waited behind someone else's clip.
- **One clip at a time.** whisper.cpp could decode two clips on four threads each, but the total
  would not be higher, and the chat cost would be. A second clip is refused with a 503, never
  queued (How the orchestrator uses it, item 2).
- **No batching, no streaming**, the same as the GPU replica.
- **Other workloads on the worker slow it.** The estimate above was measured with 1-2.5 cores of
  other load present, and one evening run was 1.4 times slower than the morning's on the same
  clips. The 1.5 margin is the headroom, and `seconds_per_audio_second` on `/health` shows the
  replica's real running rate.
- **A repetition loop costs more here.** Neither replica has a loop guard (the GPU replica's
  temperature fallback was measured and rejected on 2026-09-08). A window that loops runs to the
  443-token limit, about 12.7 s of decoding on these cores (the MUCS clip). On a clip of 30 s or
  more, where timestamps drive the sequential long form, a loop can also make the window advance
  slowly (a looping 60 s window took 68-277 s on the GPU replica, 2026-09-11). On the CPU that
  would pass a session window's 120 s and end as the caller's timeout or the watchdog's 503, where
  the GPU replica might have finished late. The loops the two replicas fall into are on different
  clips (one MUCS clip here).
- **Building the image needs the network**: apt, GitHub (whisper.cpp at its pin), PyPI and the
  PyTorch CPU index. A DNS blip on the worker fails it. On 2026-09-30 apt could not resolve
  ports.ubuntu.com inside BuildKit for about 40 s; minutes later the same kind of build resolved
  it (through 8.8.8.8, which Docker substitutes for the host's 127.0.0.53 stub), and the second
  attempt went through. `up` then stops with "the image build failed on the worker". Run it
  again: the build cache keeps every finished step, and nothing was started.
- **Timestamps are the model's own, and they differ from the GPU pipeline's on inventions.** Both
  replicas take segment times from the decoded timestamp tokens (whisper.cpp's token-level
  timestamps are off). On silence, whisper.cpp ended its invented "you" at 0.62 s where the
  pipeline ran "Thank you." to the end of the window. Anything that judges words per second of a
  segment must not assume one engine's habit. The dictation retry no longer does on the CPU
  replica (above). On a GPU replica it still relies on the pipeline's habit, which is what its
  density rule was measured on.
