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
 ┌──────────┐   WebM/Opus   ┌────────────────┐   multipart  ┌────────────────┐
 │ Composer ├──────────────►│  orchestrator  ├─────────────►│ whisper-large  │
 │  ~145 KB │  /api/audio/  │ /audio/        │ 192.168.9.68 │ -v3   ~5 GiB   │
 │  per 15s │   transcribe  │  transcribe    │    :30007    │ warm, always   │
 └──────────┘◄──────────────┤ auth · feature │              └────────────────┘
      text                  │ gate · limits  │   optional second engine,
                            │   RoutedProv.  ├──► 172.17.0.1:30007 (same node)
                            └────────────────┘
```

**The worker first, and that was a memory decision.** Measured 2026-09-08,
Spark 1 held 65 GB of allocated GPU memory (the main model's rank 0, the
vision router, the embedder, OCR and the reranker) against 33 GB on Spark 2.
The engine fits on either; only one of them had room to spare without
thinking about it.

**Its own Compose project** (`sf-local-ai-whisper`), never part of
`sf-local-ai-worker`. That other project is the main model's second shard:
starting, restarting or removing speech-to-text must never come near it.

**Its own bind.** On the worker the engine listens on the *management*
address, not the 10.100.x RoCE addresses — audio does not share the fabric the
tensor-parallel model runs over. It binds inside the container with host
networking rather than through a published port, because a published
`192.168.9.68:30007:30007` does not survive a reboot: Docker binds before the
NIC has its address and the container dies before its process starts, where
`restart: unless-stopped` never engages. That lesson is written down in
`compose/compose.monitoring-worker.yaml` and it applies here unchanged. On the
head the engine binds the docker bridge gateway (`172.17.0.1`), which the
orchestrator's container can reach and nothing off-box can.

---

## The model

`openai/whisper-large-v3`, MIT, pinned at revision `06f233fe06e7`. 1.55B
parameters, 3.1 GB in float16, ~5 GiB resident while warm.

It identifies and transcribes **99 languages** — the full Whisper set, which
adds to what came before it Gujarati, Marathi, Bengali, Tamil, Telugu, Punjabi,
Urdu, Nepali, Sinhala, Kannada, Malayalam, Assamese and Sanskrit, among many
others. `orchestrator/app/asr.py` derives `SUPPORTED_LANGUAGES` from the
model's own tokenizer rather than from a hand-kept list, and a test asserts the
two agree, so the set cannot drift from what the engine actually does.

Auto-detection is the default and should stay it: a person dictating should not
have to declare a language before speaking. **No language is sent to the engine
at all**, which is deliberate — this deployment code-switches mid-sentence
("kal ki meeting reschedule kar do for 3 PM"), and forcing a language
mistranscribes the other half. It is transcribed as spoken. This is speech to
text, not translation.

### Large-v3 and not turbo

Turbo is the same encoder with a four-layer decoder and it is roughly twice as
fast. It was deliberately not installed. A person dictates "don't delete the
Salesforce account", and the difference between the transcript keeping *don't*
and losing it is the difference between the right action and the wrong one.

### Long form is sequential, which is a choice

Whisper sees thirty seconds at a time, and there are two documented ways past
that. **Chunked** cuts the audio into fixed windows and transcribes them
independently — fast and batchable, and it decides sentence boundaries with a
stride rather than with the model. **Sequential** slides the window using the
model's own timestamp predictions, so each window starts where the last
utterance actually ended and the decoder carries its context across the seam.
The model card recommends sequential when accuracy matters more than speed,
which here it does. Concretely: passing `chunk_length_s` selects chunked, and
`compose/whisper/server.py` does not pass it. That single omission IS the
long-form strategy, which is why it is written down rather than left to be
inferred.

### Silence

Whisper answers digital silence with a plausible sentence — on this deployment,
"Thank you." every time. The obvious fix, dropping transcripts matching a list
of stock phrases, is a trap: it also deletes a person who really did say thank
you, and it never generalises to the next phrase the model invents. The engine
gates on `<|nospeech|>`, the token Whisper itself emits when it hears no
speech, at the first decoding step. Measured across the validation set on
2026-09-08: digital silence scores 0.708, and the most marginal real utterance
— a three-second Hinglish clip — scores 0.238. The documented default of 0.6
sits inside a 0.47-wide gap, so it separates them without being fitted to
either.

---

## Measured on this hardware

2026-09-08, whisper-large-v3 on a GB10, float16, real speech (the JFK clip,
tiled to length):

| audio | latency | real time |
|------:|--------:|----------:|
| 5 s   | 0.72 s  | 6.9× |
| 15 s  | 2.02 s  | 7.4× |
| 30 s  | 2.98 s  | 10.1× |
| 60 s  | 5.63 s  | 10.7× |

**One engine decodes one clip at a time.** `server.py` holds a `_gpu_lock`
around every transcription, so concurrency does not batch the way the previous
vLLM-hosted engine did — throughput is flat and latency grows linearly:

| concurrent | wall clock | p50 latency | audio per wall-second |
|---:|---:|---:|---:|
| 1  |  2.12 s |  2.12 s | 7 s/s |
| 2  |  4.00 s |  3.01 s | 7 s/s |
| 4  |  7.99 s |  5.01 s | 8 s/s |
| 8  | 15.99 s |  9.02 s | 8 s/s |
| 16 | 32.02 s | 16.97 s | 7 s/s |

That flat column is the whole reason the second engine exists, and the reason
the concurrency ceiling is small.

### Two engines

`scripts/whisper.sh up --all-nodes` starts a second copy on the head and routes
each clip to whichever engine has the fewest requests in flight
(`RoutedProvider`). Same clips, one engine against two:

| concurrent | one engine | two engines | |
|---:|---:|---:|---:|
| 1  |  2.12 s |  2.03 s | 1.00× |
| 2  |  4.00 s |  2.05 s | **1.95×** |
| 4  |  7.99 s |  4.16 s | **1.92×** |
| 8  | 15.99 s |  8.31 s | **1.92×** |
| 16 | 32.02 s | 16.79 s | **1.91×** |

**Read the first row before the others.** A second engine buys a second
concurrent *speaker*, not a faster transcript. One clip is decoded by one
engine start to finish; two nodes double how many people can dictate at once
and do not shorten anyone's wait. The 1.00× at one concurrent clip is not a
disappointment, it is the prediction.

**These are replicas, not shards.** Splitting one 1.55B model across two Sparks
would put every layer's activations on the link the main model's own
tensor-parallel traffic already uses. Cross-node tensor parallelism costs
*latency* per collective (~12.8 µs a round trip, on activations a few KB wide)
rather than bandwidth, so the fabric being fast (~109 Gb/s a rail — see
[`CLUSTER.md`](CLUSTER.md)) does not recover it. All of that to save memory
that was never short: the weights are 3.1 GB and either node holds them several
times over. Two whole copies add throughput without one byte of cross-node
chatter, and degrade to one engine gracefully when a node goes away.

### What it costs the chat model

This is the number that actually constrains dictation, and it is larger than it
was under the previous engine. Main-model single-stream decode, measured the
same afternoon:

| state | chat decode | change |
|---|---:|---:|
| idle, no engine / engine loaded | 67–73 tok/s | not measurable |
| **worker** engine saturated | 24.6 tok/s | **−66 %** |
| **head** engine saturated | 23.0 tok/s | **−68 %** |
| both saturated | 14.7 tok/s | **−79 %** |

**A loaded but idle engine costs nothing detectable.** Repeated single-stream
baselines on this box scatter across 54–74 tok/s run to run, so the ~5 tok/s
difference an idle engine appears to make is inside the noise and is not
claimed here. The saturated rows are far outside that band, which is why they
are.

**Saturating either node costs the same**, which is the counter-intuitive part
and the important one: the chat model is tensor-parallel across both Sparks and
runs at the speed of its slower rank, so a busy GPU on the worker throttles
chat exactly as a busy GPU on the head does. "Put speech on the worker so it
does not disturb chat" was a *memory* argument, and it never was a compute one.

Those rows are a saturation test — back-to-back clips with no gaps — and real
dictation is nothing like that duty cycle: someone speaks for fifteen seconds,
reads the result, and thinks. But it is the worst case, and it is why
`ASR_MAX_CONCURRENT` is 2 per engine rather than 4. Two is one clip decoding
and one ready to start, so the GPU never idles between clips and nobody queues
behind more than one other person; past that a caller is told to try again
rather than silently extending the window in which everyone's answers are slow.

### Honest comparison with what this replaced

The engine before this was `Qwen/Qwen3-ASR-1.7B` on vLLM, and swapping it in
for whisper-large-v3 was **not** a free upgrade:

| | Qwen3-ASR-1.7B | whisper-large-v3 |
|---|---:|---:|
| 15 s clip | 1.04 s | 2.02 s |
| 8 concurrent clips | 1.10 s | 15.99 s (one engine) |
| audio per wall-second | ~117 s/s | ~7 s/s |
| languages | 30 | 99 |

The two engines' effect on chat is **not** directly comparable and should not
be put in that table: the old figure (−5.8 % at four concurrent dictations)
was taken on an engine that batched those four and was done in about a second,
while −66 % here is a saturation test that keeps a GPU busy indefinitely. The
comparable statement is the weaker and truer one: whisper occupies a GPU for
roughly fifteen times as long per second of audio, and the chat model feels
whatever occupies either GPU.

Whisper is about twice as slow on a single clip and roughly fifteen times
lower in throughput, because vLLM gave the old engine continuous batching and
the transformers pipeline here gives none. What it buys is **language
breadth** — 30 languages to 99, including the Indic languages the previous
model could not transcribe at all — and a decoder whose long-form behaviour is
the reference implementation's. On English and French specifically, published
WER favours the model that was removed.

That trade was made deliberately and can be revisited: the provider
abstraction in `orchestrator/app/asr.py` is one class per engine, and
`RoutedProvider` does not care what is behind an endpoint.

---

## Format

The browser records WebM/Opus and the engine decodes it with ffmpeg.
**Nothing converts audio anywhere in this platform.** The same 15-second clip
is 2.1 MB as WAV and 145 KB as WebM/Opus, and transcribes identically. Safari
records MP4/AAC and that works the same way.

`_decode` streams the upload through ffmpeg's *stdin* and reads a 16 kHz mono
waveform off its *stdout*, so a recording never becomes a file that somebody
has to remember to delete. The command is an allow-list and nothing more:
demux, decode, downmix, resample. `-nostdin` is not decoration — without it
ffmpeg competes with the parent process for the terminal's stdin.

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
scripts/whisper.sh up              # fetch weights, build, start, wait, record the URL
scripts/whisper.sh up --all-nodes  # ...and a second engine on the head
scripts/whisper.sh status          # container state and the engine's own /health
scripts/whisper.sh verify          # transcribe a real clip and print what came back
scripts/whisper.sh url             # the endpoint(s) the orchestrator should use
scripts/whisper.sh logs
scripts/whisper.sh down            # stops dictation; touches nothing else
```

`up` writes `ASR_ENABLED=true` and `ASR_BASE_URLS` into `.env`, then the
orchestrator needs a restart to read them (`./techsara up`). Until that has
happened the feature is invisible: no microphone button, and the route answers
404. That is deliberate — a button that cannot work is worse than no button.

**`down` is per node.** `scripts/whisper.sh down` stops the engines on the
nodes `WHISPER_NODES` names, and rewrites `ASR_BASE_URLS` to whatever is left,
so dropping the head's engine is one command and does not need `.env` edited
by hand. It never touches `sf-local-ai-worker`, which is the main model.

Configuration is documented in `.env.example` under *Speech to text*.

---

## Operating it

- **Prometheus does not scrape the engine, deliberately.** `server.py` serves
  `/health`, `/v1/models` and `/v1/audio/transcriptions` and nothing else —
  there is no `/metrics` — so a scrape job for it could only ever report a
  target that is permanently down, and a red target that is red by
  construction teaches people to ignore red targets. The reasoning is written
  where the job used to be, in `monitoring/prometheus/prometheus.yml`.
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

- **No batching, and that is the big one.** The engine decodes one clip at a
  time behind a `_gpu_lock`, so the fleet's capacity is exactly the number of
  engines running. The previous vLLM-hosted engine batched eight concurrent
  clips in the time of one; this does not, and the tables above are what that
  costs. Serving whisper under vLLM instead (`WhisperForConditionalGeneration`
  is in its registry) would recover continuous batching, at the price of the
  sequential long-form decoder this deployment chose it for. That is the next
  real improvement available here, and it is a genuine trade, not a free win.
- **Streaming (partial text while speaking) is not implemented.** The
  transport was kept simple enough to add it later. Stop-then-transcribe at
  ~2 s for a normal sentence did not justify the complexity yet.
- **Timestamps are not returned.** The sequential decoder predicts them
  internally — it needs them to slide its window — but nothing in the composer
  would use them, so they are not surfaced.
- The rate limiter is in-process, so it bounds one orchestrator container.
  That is the whole deployment today; the concurrency pool is what actually
  protects the GPU.
