# Live dictation benchmarks

These tools measured the choices behind real-time dictation (docs/voice/REALTIME.md). Every
number below was taken on this cluster on 2026-09-29, on the worker Spark (spark-476e,
GB10, 20 Arm cores), unless a row says otherwise. Run the tools again after any engine,
model or hardware change. A number in a document is only as good as its last measurement.

## What is measured, and how

| Tool | Question it answers |
|---|---|
| `screen.py` | How accurate is a streaming model on real speech? It feeds 40 ms frames the way the browser does and emits finals by text difference, never resetting the recognizer mid-speech. It reports WER, the delay to the first partial and to the final (in audio time), and the decode step time. |
| `batch.py`, `multi.py` | How many live streams fit on N cores in real time? Streams are fed at wall-clock pace in chunk-sized ticks. `multi.py` runs K decode threads (sherpa-onnx releases the GIL in `decode_streams`). |
| `shared.py` | Can one recognizer be shared by several decode threads? It checks that the outputs are identical and reports RSS. |
| `chat_probe.py` | What does ASR load cost the chat model? It measures single-stream decode tok/s of the main model on the head. Run it before, during and after a load. |
| `stream_bench.py` | End to end through the real WebSocket protocol: engine, or gateway with a session cookie. N concurrent real-time sessions; first-partial, partial-lag and final latency percentiles, and WER. |
| `mucs_eval.py`, `rescore.py` | Hinglish accuracy on MUCS 2021 Hindi-English (OpenSLR 104, CC BY-SA 4.0): whole lectures scored at document level, so the dataset's loose segment timestamps do not count as errors, plus a script-agnostic "skeleton" WER. The skeleton WER forgives an English word written in Devanagari (डॉक्यूमेंट) or in Latin (document), never a wrong or missing word. |
| `whisper_sets.py` | The production whisper-large-v3 replica on exactly the utterances `screen.py` used. |

**Normalisation.** Every scorer does the same thing to both references and hypotheses:
- lower-cases;
- removes characters whose Unicode category is punctuation or symbol;
- keeps combining marks;
- folds nukta and chandrabindu.

A regular-expression word class (`\w`) must not be used. It strips Devanagari vowel signs and
splits every Hindi word into fragments. Our first Hindi numbers had that bug and were redone.

## Data (not in git)

```bash
# on the worker, into ~/rtvoice-bench/data (or set RTVOICE_DATA / MUCS_ROOT)
curl -sSL -o test-clean.tar.gz https://www.openslr.org/resources/12/test-clean.tar.gz && tar xzf test-clean.tar.gz
for L in en_us hi_in gu_in; do mkdir -p fleurs/$L
  curl -sSL -o fleurs/$L/test.tsv    https://huggingface.co/datasets/google/fleurs/resolve/main/data/$L/test.tsv
  curl -sSL -o fleurs/$L/test.tar.gz https://huggingface.co/datasets/google/fleurs/resolve/main/data/$L/audio/test.tar.gz
  (cd fleurs/$L && tar xzf test.tar.gz); done
mkdir -p mucs && curl -sSL -o mucs/hi-en_test.tar.gz https://openslr.trmal.net/resources/104/Hindi-English_test.tar.gz && (cd mucs && tar xzf hi-en_test.tar.gz)
# models: sherpa-onnx int8 exports, from their Hugging Face mirrors
python3 hfget.py csukuangfj2/sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-160ms-int8-2026-06-11 models/n35-160
python3 hfget.py csukuangfj2/sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25 models/en-160
```

The venv needs `sherpa-onnx numpy soundfile websockets`, which are pip wheels for aarch64.

## Results (2026-09-29)

### Accuracy: same utterances, same normaliser

| Set | whisper-large-v3 (production) | Nemotron 3.5 streaming, 160 ms | Nemotron EN streaming, 160 ms |
|---|---:|---:|---:|
| MUCS Hindi-English lectures, 34 min, document skeleton-WER | 40.2 % | **19.4 %** (auto) / 19.5 % (hi) | 69.9 % |
| MUCS, document WER (script-sensitive) | 55.8 % | **41.7 %** | 80.0 % |
| FLEURS Hindi (40) | 41.9 % | **10.9 %** (hi) / 11.8 % (auto) | – |
| LibriSpeech test-clean (60) | 4.21 % | 6.23 % (auto) | **4.13 %** |
| FLEURS English (40) | **6.81 %** | 13.3 % (en) | 10.2 % |

- whisper wrote 73 of the 364 MUCS segments (20 %) in Urdu script and 5 in English (a
  translation). On FLEURS Hindi, 9 of 40 came back in Urdu script.
- Larger chunks do not help Hinglish: 3.5 at 560 ms scores 20.0 % and at 1120 ms 20.7 %
  (skeleton WER). They help English a little: EN at 560 ms scores 3.72 % on LibriSpeech.
- Gujarati is not supported by either Nemotron model (FLEURS Gujarati: 104 % WER).

### Latency, in audio time, from `screen.py`

| Model, 160 ms chunks, endpoint 0.6 s | First partial after speech onset, p50 | Final after speech end, p50 / p95 |
|---|---:|---:|
| Nemotron EN | 510 ms | 680 / 1299 ms |
| Nemotron 3.5 (auto) | 510 ms | 830 / 1340 ms |
| Nemotron 3.5 on Hindi | 720 ms | 710 / 1223 ms |

The first-partial figure includes the time it takes to say the first word.

### Capacity and cost to chat

Measured on the worker CPU (sherpa-onnx 1.13.8, onnxruntime 1.28.2, int8):
- **One decode step.** Nemotron 0.6B at 160 ms takes 31.6 ms on 2 threads.
- **One decode loop, 4 threads.** About 7 real-time streams at 160 ms and about 14 at 560 ms.
- **4 decode threads × 2 on the ten Cortex-X925 cores.** 12 real-time streams at 160 ms, with a tick p95 of 99–102 ms against a 160 ms budget and about 5 cores busy.
- **Chat, 12 streams (5 cores busy).** Main-model decode ran at 107.0–107.9 tok/s, against a 107.2–108.9 baseline: no measurable change.
- **Chat, all 20 cores saturated (48 streams).** 89.6–95.9 tok/s (−11 to −16 %), and TTFT rose. This is why the engine is pinned and capped.
- **Sharing one recognizer across 4 threads.** Outputs were identical (16 of 16), and RSS was 1.45 GB instead of one 0.7 GB copy per thread.

For comparison, a saturated whisper-large-v3 replica on either Spark takes chat from 107 to
53 tok/s. That is why live partials never use whisper.
