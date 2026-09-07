> # ⚠️ RETIRED HISTORICAL BASELINE
>
> **Qwen3-ASR-1.7B was removed from this platform on 2026-09-08** — its service,
> image, weights, provider and configuration are all gone, and voice input is
> disabled until a replacement engine is installed.
>
> This document is kept only as historical evidence of what was measured while
> that engine ran. **It configures nothing**: no service reads it, no test
> requires it, and nothing here should be treated as a description of the
> current system. The numbers are not reproducible without reinstalling the
> engine.

# Qwen3-ASR-1.7B — measured baseline

**2026-09-08.** Everything below was measured against the live engine on the
day it is dated. Nothing is copied from `docs/VOICE.md`; where the two disagree,
the disagreement is recorded rather than resolved.

Machine-readable form: [`results/qwen3-asr-baseline.json`](results/qwen3-asr-baseline.json).
(The freeze record that accompanied this was deleted with the engine — it described
an implementation that no longer exists.)

---

## What was measured

| | |
|---|---|
| model | `Qwen/Qwen3-ASR-1.7B` @ `7278e1e70fe2`, BF16 |
| runtime | vLLM, `techsara-asr:local` (nightly + `av`/`soundfile`/`librosa`) |
| endpoint | `http://192.168.9.68:30006/v1` |
| node | spark-2 (worker), NVIDIA GB10 |
| decoding | chat path, `temperature=0.0`, `max_tokens=1024`, language auto |
| audio | the model authors' published `asr_en.wav` (15.1 s, 48 kHz mono), looped to length; generated digital silence |
| requests | 40 issued, **0 failed** |

Production's decoding settings exactly. A run at different settings measures a
different system.

---

## Latency

Five repeats per length, warm engine, sequential. Spread was under 4 % at every
length.

| audio | median | min | max | real time |
|------:|-------:|----:|----:|----------:|
| 5 s   | **0.464 s** | 0.459 | 0.490 | 10.8× |
| 15 s  | **0.990 s** | 0.987 | 0.996 | 15.2× |
| 30 s  | **1.914 s** | 1.909 | 1.951 | 15.7× |
| 60 s  | **3.784 s** | 3.762 | 3.910 | 15.9× |

Latency is linear in audio length past about 15 seconds, and the real-time
factor flattens around 16×.

**First request of the session:** 1020 ms at 15 s — indistinguishable from the
warm median. The engine was already loaded, so this is *not* a cold-start
number, and no cold-start number is reported.

**No p50 or p95.** Five samples per length supports a median and nothing more.
A p95 computed from this would be quoted by somebody later and could not be
defended.

**Silence** returns in ~0.11 s. **The fallback path**
(`/v1/audio/transcriptions`) cost 1.002 s at 15 s against the chat path's
0.990 s — the two are the same price, and the chat path is preferred only
because it also reports the language.

---

## Concurrency

15-second clips issued simultaneously against one engine.

| in flight | wall clock | slowest request | throughput |
|----------:|-----------:|----------------:|-----------:|
| 1  | 1.011 s | 1.010 s | 14.8 audio-s/wall-s |
| 4  | 0.964 s | 0.963 s | **62.2** audio-s/wall-s |
| 8  | 1.131 s | 1.129 s | **106.1** audio-s/wall-s |

Four concurrent clips finish in the wall-clock time of one — the engine batches
them. Eight cost 13 % more wall clock than a single clip.

This is the evidence for a claim the code makes about itself: `ASR_MAX_CONCURRENT=4`
is **not** protecting the speech engine, which barely notices. It protects the
**chat model** sharing the GPU. That cost was not re-measured here — doing so
means loading the production chat model while people are using it.

---

## Resources

| | |
|---|---|
| ASR process | `VLLM::EngineCore` pid 3875691, spark-2 |
| **GPU memory** | **10.14 GB** |
| spark-2 total allocated | 40.44 GB |
| spark-1 total allocated | 70.01 GB |

Source: Prometheus `dgx_gpu_process_memory_bytes`, 2026-09-08.

10.14 GB is `--gpu-memory-utilization 0.08` of the GB10's 128 GB unified memory
(≈10.24 GB). The weights are ~4.4 GB on disk; the rest is the reservation vLLM
claims at startup and never releases.

Not measured, and not guessed: **model load time** and **VRAM before load**
(both need a restart), and **host CPU memory** (the container is on the worker
and this session had no SSH access to it).

---

## Quality

**Not measured. There is no benchmark corpus.**

WER and CER require human-recorded clips with reference transcripts, and this
repository contains none. `benchmarks/asr/evaluate.py` is written and verified
end to end against this engine — it scores, aggregates by category, refuses to
report a p95 under 20 samples, and refuses to run at all on the empty template.
It is waiting on audio, not on code.

### What *could* be established without references

| check | result |
|---|---|
| language identification, English sample | `English` ✓ |
| language identification, Chinese sample | `Chinese` ✓ |
| hallucination on 2 s of silence | none — empty string ✓ |
| hallucination on 10 s of silence | none — empty string ✓ |
| output contract `language X<asr_text>…` | held on every request ✓ |
| determinism at temperature 0 | transcripts byte-identical across repeats ✓ |

On silence the engine emits the literal `language None<asr_text>`, and
`app/asr.normalise_language` rejects `None` as unsupported and returns null
rather than inventing a language. That is correct, and it is a behaviour worth
re-checking on every candidate model.

---

## Where the documentation and the runtime disagree

These were recorded against `docs/VOICE.md`, which was **deleted along with the
engine** on 2026-09-08. They are kept because they are the measurement history,
not because either document still exists.

**1. The 60-second latency row.** The (now-deleted) `docs/VOICE.md` reported 1.85 s / 32.4×.
Measured: **3.784 s / 15.9×**, five times, spread under 4 %. The 5 s, 15 s and
30 s rows all reproduce within 8 %, and 15.9× is the value consistent with
them — a real-time factor flattens with length, it does not double. The
documented figure looks like a clip that was shorter than 60 seconds.

**2. Engine memory.** `docs/VOICE.md` said "about 6 GiB while warm". Measured:
**10.14 GB**. 6 GiB is close to the manifest's `approximate_loaded_weight_bytes`
— the weights alone. The process holds the full 0.08 reservation. Both are
real numbers about different things, and the documented one understates what a
second engine costs a node.

**3. Half the fleet is gone.** `ASR_BASE_URLS` names two engines. The worker
(`192.168.9.68:30006`) answers in 2.4 ms; the head (`172.17.0.1:30006`) refuses
the connection and runs no ASR container. Dictation still works —
`RoutedProvider` sorts by in-flight count, both start at zero, so the healthy
worker is tried first — but under load a request reaches the dead endpoint,
fails, and stands it down for 20 s. Moot now: both endpoints are closed.

**4. There is no `.env`.** ⚠️ The repository has no `.env` file, yet the running
orchestrator container holds eleven `ASR_*` variables including
`ASR_ENABLED=true`. `ASR_ENABLED` defaults to **false**, and `scripts/asr.sh`
is what writes the `.env` that turns it on. **The running container is the only
copy of this configuration**, so the next `./techsara up` would start the
orchestrator with voice input silently disabled. Addressed on 2026-09-08: the running container's full environment (407 variables,
396 of them unrelated to ASR) was captured to a dated backup under
`~/.techsara-env-backup-*/orchestrator-runtime.env` before the ASR cleanup.

---

## What is still needed

1. **Benchmark audio.** ≥10 human recordings in each of the eight categories,
   WebM/Opus mono from a browser `MediaRecorder`, with verbatim references.
   Hindi, Indian English and Hinglish matter most — they are why this model was
   chosen over Whisper, and they are entirely unmeasured.
2. **A scored run** of `evaluate.py` against this frozen engine once that audio
   exists, to fill in the WER and CER this document leaves blank.
3. **A decision** on whether cold-start time is worth one controlled restart.
