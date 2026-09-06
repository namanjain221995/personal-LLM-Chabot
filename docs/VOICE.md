# Voice input

Dictation in the chat composer, transcribed on this platform's own hardware.
No audio leaves the building; no external speech API is called; nothing is
stored.

---

## What a person sees

Press the microphone in the composer. The browser asks for permission the
first time. The controls row is replaced by a recording bar — a cancel
control, a live waveform, an elapsed timer and a stop button — and the
waveform moves with the actual microphone signal, so a muted input reads as
flat rather than being animated over.

Press stop; the bar says *Transcribing…*; a second or so later the words
appear **in the message box**, joined to whatever was already typed. They are
not sent. They are a draft, editable exactly like typed text, and pressing
Send is a separate, deliberate act.

Press cancel instead and the recording is discarded without being
transcribed. Either way the microphone is released the instant recording
ends — the browser's capture indicator goes out.

---

## Where it runs

```
   browser                    Spark 1 (head)                 Spark 2 (worker)
 ┌──────────┐   WebM/Opus   ┌────────────────┐  multipart   ┌────────────────┐
 │ Composer ├──────────────►│  orchestrator  ├─────────────►│  vLLM + Qwen3  │
 │  ~145 KB │  /api/audio/  │ /audio/        │ 192.168.9.68 │  ASR-1.7B      │
 │  per 15s │   transcribe  │  transcribe    │    :30006    │  ~6 GiB        │
 └──────────┘◄──────────────┤ auth · feature ├◄─────────────┤  warm, always  │
      text                  │ gate · limits  │   text+lang  └────────────────┘
                            └────────────────┘
```

**The worker, not the head, and that was measured.** Before this existed,
Spark 1 held 76.1 GB of allocated GPU memory (the main model's rank 0, the
vision router, the embedder, OCR and the reranker) against 30.0 GB on Spark 2,
which runs only the main model's tensor-parallel rank 1. The engine fits on
either; only one of them had room to spare.

**Its own Compose project** (`sf-local-ai-asr`), never part of
`sf-local-ai-worker`. That other project is the main model's second shard:
starting, restarting or removing speech-to-text must never come near it.

**Its own bind.** The engine listens on the worker's *management* address, not
the 10.100.x RoCE addresses — audio does not share the fabric the
tensor-parallel model runs over. It binds inside the container with host
networking rather than through a published port, because a published
`192.168.9.68:30006:30006` does not survive a reboot: Docker binds before the
NIC has its address and the container dies before its process starts, where
`restart: unless-stopped` never engages. That lesson is written down in
`compose/compose.monitoring-worker.yaml` and it applies here unchanged.

---

## The model

`Qwen/Qwen3-ASR-1.7B`, Apache-2.0, pinned at revision `7278e1e70fe2`.

It identifies and transcribes **30 languages** — including Hindi, English,
Arabic, Chinese, Japanese, Korean, Spanish, French, German, Russian,
Portuguese, Indonesian, Italian, Thai, Vietnamese, Turkish, Malay, Dutch,
Swedish, Danish, Finnish, Polish, Czech, Filipino, Persian, Greek, Romanian,
Hungarian, Macedonian and Cantonese — plus 22 Chinese dialects.

**It does not support Gujarati, Marathi, Bengali, Tamil, Telugu, Punjabi or
Urdu.** Those are not in the model's published set and the platform does not
claim them. Auto-detection is the default and should stay it: a person
dictating should not have to declare a language before speaking.

Mixed-language speech ("kal ki meeting reschedule kar do for 3 PM") is
transcribed as spoken. This is speech to text, not translation.

### Why not Whisper

Whisper large-v3 was not chosen because Qwen3-ASR measured better on the
things this deployment needs — Hindi and Indian-accented English — and because
the vLLM already running here supports it natively with no new runtime.
`WhisperForConditionalGeneration` **is** in the same engine's registry, so
switching is a change of two environment variables, not a rewrite. The
provider abstraction in `orchestrator/app/asr.py` exists for exactly that.

---

## Measured on this hardware

2026-09-04, Qwen3-ASR-1.7B on Spark 2, bf16, `--gpu-memory-utilization 0.08`:

| audio | latency | real time |
|------:|--------:|----------:|
| 5 s   | 0.48 s  | 10.4× |
| 15 s  | 1.04 s  | 14.4× |
| 30 s  | 2.00 s  | 15.0× |
| 60 s  | 1.85 s  | 32.4× |

Eight simultaneous 15-second clips finished in **1.10 s of wall clock** — the
engine batches them, so concurrency is nearly free on the ASR side.

**Cost to the chat model**, which is the number that actually constrains this:

| dictations in flight | chat decode | change |
|---:|---:|---:|
| 0 (baseline) | 69–70 tok/s | — |
| 4 (the configured limit) | 65 tok/s | **−5.8 %** |
| 8 | 63 tok/s | −10.1 % |

`ASR_MAX_CONCURRENT=4` is that trade-off, made explicitly. It is not there to
protect the speech engine — it is there so a person waiting for an *answer* is
never slowed down by someone else's dictation.

The engine holds about **6 GiB** while warm and is never unloaded.

### Two engines

One engine **saturates at eight concurrent clips**: past that, throughput is
flat at ~123 seconds of audio per wall-second and latency grows linearly.
`scripts/asr.sh up --all-nodes` starts a second copy on the head and routes to
whichever has the fewest requests in flight:

| concurrent | one engine | two engines | |
|---:|---:|---:|---:|
| 8 | 1.00 s · 117 s/s | 0.91 s · 128 s/s | 1.10× |
| 16 | 1.54 s · 123 s/s | 1.05 s · 214 s/s | **1.46×** |
| 32 | 2.70 s · 122 s/s | 1.46 s · 241 s/s | **1.85×** |

**These are replicas, not shards.** The "13 Gb/s RoCE link" this argument
originally rested on was a unit error — the link measures ~109 Gb/s per rail
(see [`CLUSTER.md`](CLUSTER.md)) — but the conclusion survives on its own
merits, which are stronger than the bandwidth claim was. Splitting one 1.7B
model across two Sparks would put every layer's activations on a link that the
main model's own tensor-parallel traffic already uses, and cross-node tensor
parallelism costs *latency* per collective (12.8 µs per round trip, on
activations only a few KB wide) rather than bandwidth — so a faster wire does
not recover it. All of that to save memory that was never short — the weights are 4.4 GB and either node holds them twice over.
Two whole copies add throughput without one byte of cross-node chatter, and
degrade to one engine gracefully when a node goes away.

The second engine is not free: it costs the head node 8.3 GiB and takes the
chat model from −5.8 % to **−6.7 %** under dictation load. Below eight
concurrent dictations it buys nothing, because one engine was never busy.

---

## Format

The browser records WebM/Opus and the engine decodes it natively through PyAV
(bundled ffmpeg). **Nothing converts audio anywhere in this platform.** The
same 15-second clip is 2.1 MB as WAV and 145 KB as WebM/Opus, and transcribes
identically. Safari records MP4/AAC and that works the same way.

The one thing the stock vLLM image lacks is the decoder itself — the
`vllm[audio]` extra — so `compose/asr/Dockerfile` adds `av`, `soundfile` and
`librosa` on top of the image the cluster already runs. Without it every
request fails with *"Please install vllm[audio] for audio support"*.

---

## Privacy

- Audio is held in memory for one request and dropped. No temporary file is
  written, at any layer — which is why the recording is posted as the request
  BODY rather than as a multipart field. `UploadFile` would have handed it to
  Starlette's multipart parser, which spools every part into a
  `SpooledTemporaryFile` whose 1 MB ceiling is a class attribute: any dictation
  past about ninety seconds would have been written to the container's disk
  before a line of our own code ran. The duration and the forced language are
  query parameters, which is all they ever needed to be.
- The transcript is returned to the browser and **not persisted**. It becomes
  a message only if the person presses Send, and then it is stored exactly
  like anything they typed.
- `voice_transcriptions` (migration V19) records *metadata only*: who, how
  long, which language, how fast, and whether it worked. It has no column that
  could hold a word anybody said.
- Failed attempts are recorded too — an error rate computed only from
  successes is not an error rate.

---

## Access control

Two independent gates, both enforced server-side:

1. **A signed-in user.** An open ASR endpoint on a shared GPU is a free
   denial-of-service against the chat model.
2. **`Feature.VOICE_INPUT`**, per member, on the admin Access page like every
   other tool. The composer hides the microphone when it is off, but hiding is
   a courtesy: the route answers 403 regardless of what the client sends.

Plus a per-user rate limit (`ASR_RATE_PER_MIN`, default 20/minute) and the
concurrency pool above.

---

## Running it

```bash
scripts/asr.sh up        # fetch weights, build, start, wait, record the URL
scripts/asr.sh status    # container state and the engine's own /v1/models
scripts/asr.sh verify    # transcribe a real clip and print what came back
scripts/asr.sh bench     # the table above, regenerated
scripts/asr.sh logs
scripts/asr.sh down      # stops dictation; touches nothing else
```

`up` writes `ASR_ENABLED=true` and `ASR_BASE_URL` into `.env`, then the
orchestrator needs a restart to read them (`./techsara up`). Until that has
happened the feature is invisible: no microphone button, and the route answers
404. That is deliberate — a button that cannot work is worse than no button.

Configuration is documented in `.env.example` under *Speech to text*.

---

## Operating it

- **Prometheus** scrapes the engine as job `vllm-asr`, labelled
  `service=asr, node=spark-2`, giving the same vLLM metrics as every other
  engine: queue depth, KV cache, tokens per second, request latencies.
- **The orchestrator** exposes `asr_requests_total`,
  `asr_request_duration_seconds`, `asr_errors_total`, `asr_queue_depth`,
  `asr_active_requests` and `asr_detected_language_total`. The language label
  is a closed set — a mis-parsed engine reply cannot mint a new series.
- **The admin console** has a Voice page under Analytics (super admin only):
  transcriptions, people, minutes recorded, latency percentiles, success rate
  and the languages detected. Every figure on it comes from
  `voice_transcriptions`, so it reports what dictation DID, not what the
  engine is doing right now.
- `GET /audio/health` is the live half: whether dictation can work this
  second, and why not when it cannot. It names the model and the pool's queue
  depth, so it takes `analytics.read` — super admin — and answers 404 to
  anyone else. `POST /audio/transcribe` goes to some trouble never to tell a
  member which model answered; a signed-in-only health route would have handed
  that back through a second door.

## Known limits

- **Streaming (partial text while speaking) is not implemented.** The engine
  supports it — `Qwen3ASRRealtimeGeneration` is in the same vLLM registry —
  and the transport here was kept simple enough to add it later. Stop-then-
  transcribe at ~1 s for a normal sentence did not justify the complexity yet.
- **Timestamps are not returned.** They need `Qwen3-ForcedAligner-0.6B` as a
  second model; nothing in the composer would use them.
- The rate limiter is in-process, so it bounds one orchestrator container.
  That is the whole deployment today; the concurrency pool is what actually
  protects the GPU.
