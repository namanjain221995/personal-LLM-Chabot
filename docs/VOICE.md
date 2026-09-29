# Voice input

Dictation in the chat composer, transcribed on this platform's own hardware.
No audio leaves the building, and no external speech API is called.

**The recording and its transcript are stored** in the person's account. That
has been so since 2026-09-29 (migration V42, the owner's decision that
dictation must be kept and must work for an hour or more); see
[Privacy](#privacy).

This page describes the durable path: the stored recording and
whisper-large-v3's transcript of it. Two pages build on it:

- [`voice/REALTIME.md`](voice/REALTIME.md): the words appearing while the
  person is still speaking. They come from a separate streaming engine on the
  worker's CPU.
- [`voice/MEETINGS.md`](voice/MEETINGS.md): meeting transcripts.

---

## What a person sees

Press the microphone in the composer. The browser asks for permission the
first time. The controls row is replaced by a recording bar — a cancel
control, a live waveform, an elapsed timer and a stop button — and the
waveform moves with the actual microphone signal, so a muted input reads as
flat rather than being animated over.

While the person talks, the recording goes to the server in 5-second parts,
and each part is stored before the server acknowledges it. A panel above the
bar shows whisper's text in steps, as the speaker pauses. The steps come at
best every five seconds, because the text travels back with the answer to each
part. Under the text, a line says *Saved to your account*, linking to
**Recordings** (`/recordings`). When the live path is configured, the words
appear in that panel as they are spoken instead
([`voice/REALTIME.md`](voice/REALTIME.md)).

Press stop; the bar says *Transcribing…* while the last of the recording is
transcribed. Then the words appear **in the message box**, joined to the end
of whatever was already typed. On a candidate stack on 2026-09-30, that took
1.66 s after Stop for a 28.8 s English dictation and 5.5 s for a 67.2 s Hindi
one. The words are not sent. They are a draft, editable exactly like typed
text, and pressing Send is a separate, deliberate act. For Hindi and Hinglish,
the text that goes in may be the live transcript rather than whisper's
([why](voice/REALTIME.md#which-transcript-goes-into-the-draft)).

Press cancel instead and the recording is discarded: deleted from the server,
not transcribed. A recording longer than a minute asks first. Either way the
microphone is released the instant recording ends, and the browser's capture
indicator goes out.

**Recordings** lists a person's recordings. Each can be played, its transcript
copied, or deleted.

---

## Where it runs

```
   browser                    Spark 1 (head)                 Spark 2 (worker)
 ┌──────────┐ WebM/Opus in  ┌────────────────┐  WAV window  ┌────────────────┐
 │ Composer ├─5 s parts────►│  orchestrator  ├─────────────►│ whisper-large  │
 │  ~80 KB  │  /api/audio/  │ stores them,   │ 192.168.9.68 │ -v3   ~5 GiB   │
 │  a part  │  sessions/…   │ cuts ≤ 30 s    │    :30007    │ warm, always   │
 └──────────┘◄──────────────┤ windows at     │              └────────────────┘
      text                  │ pauses; auth,  │   second replica,
                            │ limits, pacing ├──► 172.17.0.1:30007 (same node)
                            └────────────────┘
```

**Two roads into it.** The recording session above (`/audio/sessions/*`,
`orchestrator/app/dictation.py`) is the default. The browser uploads numbered
parts. The orchestrator stores them, decodes the stored file once into 16 kHz
PCM, cuts windows of at most 30 s at pauses, and sends them to whisper one at a
time across the whole fleet. It waits up to 20 s before each window while a
chat answer is streaming.

The older one-request path (`POST /audio/transcribe`: the whole recording as
one body, at most 600 s, nothing stored) remains as the fallback. It is used
only when the server answers `404 sessions_off` or `503 capacity_full`. The
live path runs beside the session on the worker's CPU and uses no GPU
([`voice/REALTIME.md`](voice/REALTIME.md)).

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

`openai/whisper-large-v3`, Apache-2.0, pinned at revision `06f233fe06e7`.
1.55B parameters, 3.1 GB in float16, ~5 GiB resident while warm. The licence is
the model card's, and `MODEL_LICENSE` in `compose/whisper/server.py` and
`config/model-manifest.yaml` record the same one.

It identifies and transcribes **100 languages**: the full Whisper set,
including the Cantonese that large-v3 added. Compared with the previous engine,
that adds Gujarati, Marathi, Bengali, Tamil, Telugu, Punjabi, Urdu, Nepali,
Sinhala, Kannada, Malayalam, Assamese and Sanskrit, among many others.

`orchestrator/app/asr.py` keeps the language names as a literal list,
`SUPPORTED_LANGUAGES`. A test (`tests/test_voice_input.py`) asserts that the
list equals the closed set of language labels the metrics accept, so a language
the engine reports is never folded into `other` on a dashboard. Nothing checks
the list against the tokenizer, so a model change must update it by hand.

Auto-detection is the default and should stay it: a person dictating should not
have to declare a language before speaking. **No language is sent to the engine
at all**, which is deliberate. This deployment code-switches mid-sentence
("kal ki meeting reschedule kar do for 3 PM"), and forcing a language
mistranscribes the other half.

The request is always `task=transcribe`, never translate, but that is a
request, not a guarantee. When Whisper's detector decides a passage is English,
it writes Hindi or Gujarati speech as fluent English: it translates. On a 2 h
23 min Gujarati/Hindi/English meeting it chose English for 71 % of the cues,
and most of those were translations of what was said. It also writes Hindi in
Urdu script: 73 of 364 segments of Hindi-English lectures, and 9 of 40 FLEURS
Hindi sentences. The measurements are in
[Accuracy](#accuracy-on-this-workspaces-languages) and
[`video-understanding/LANGUAGE-EVIDENCE-2026-09-11.md`](video-understanding/LANGUAGE-EVIDENCE-2026-09-11.md).

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
utterance actually ended. The model card recommends sequential when accuracy
matters more than speed, which here it does. Concretely: passing
`chunk_length_s` selects chunked, and `compose/whisper/server.py` does not pass
it. That single omission IS the long-form strategy, which is why it is written
down rather than left to be inferred.

**The decoder does not carry text across the seam.** The previous window's
words are not fed back as a prompt: `condition_on_prev_tokens` stays at its
default, off. The rest of the model card's long-form recipe is not used
either: no compression-ratio threshold, no temperature fallback. That recipe was
measured here on 2026-09-08 and rejected. On a 128-second Hindi-English
recording it was worse: it produced 1,080 characters against 1,364, dropped
whole utterances and reordered one, and took 71 s instead of 38 s.

Recording sessions send windows of at most 30 s, cut at pauses by the
orchestrator. So for dictation the long-form algorithm matters only on the
one-request path, and each session window is one pass of the model.

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

The gate judges only the first 30 s of a clip. So on the one-request path, a
quiet opening silences everything after it. This was measured on the live
worker engine on 2026-09-29 with the owner's reproduction, 25 s of quiet
followed by 156 s of speech. The one-request path returned nothing 5 times out
of 5. A recording session transcribed it 3 times out of 3 (424 words), because
a session window is at most 30 s and each window is judged on its own. If the
gate empties a window in which the orchestrator's voice-activity detector
heard 3 s or more of speech, the window is asked again with the gate off.

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
the concurrency ceiling is small. It was re-measured on 2026-09-28 with 20-29 s
clips. Throughput stayed between 8.1 and 12.1 s of audio per wall-second from
1 concurrent clip to 16, and eight simultaneous clips on one replica took
18.8 s.

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
was under the previous engine. Main-model single-stream decode, measured
2026-09-28:

| state | chat decode | change |
|---|---:|---:|
| idle | 106–107 tok/s | |
| one node's engine saturated (either node) | 53 tok/s | **−50 %** |
| both saturated | 28 tok/s | **−74 %** |

The first measurement, on 2026-09-08, was taken when chat decoded at 67–73
tok/s. On 2026-09-11 the main model's engine was reconfigured, with
speculative decoding and prefix caching switched off, and single-stream decode
rose from 69 to 101 tok/s
([`ISSUE/gdn-spec-decode-remediation-2026-09-11.md`](ISSUE/gdn-spec-decode-remediation-2026-09-11.md)).
The 2026-09-08 figures were:

- worker engine saturated: 24.6 tok/s (−66 %);
- head engine saturated: 23.0 tok/s (−68 %);
- both saturated: 14.7 tok/s (−79 %).

Both are saturation tests. The conclusion did not change between them.

**A loaded but idle engine costs nothing detectable.** On 2026-09-08, repeated
single-stream baselines on this box scattered across 54–74 tok/s run to run.
The ~5 tok/s difference an idle engine appeared to make is inside that noise,
so it is not claimed here. The saturated rows are far outside that band, which
is why they are.

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

Recording sessions go further in three ways:

- **One window at a time.** At most one session window decodes at a time
  across the whole fleet (`VOICE_SESSION_ASR_CONCURRENCY`, 1), so sessions
  alone never saturate both nodes.
- **Chat first.** Each window waits up to 20 s while a chat answer is
  streaming (`VOICE_SESSION_PACE_LIVE_S`).
- **Spread out.** The cost is spread across the recording, instead of arriving
  as one block after Stop.

It does not make the cost disappear. That is why the words-while-speaking
preview does not use whisper at all: it runs on the worker's CPU, where twelve
streams left chat decode unchanged
([`voice/REALTIME.md`](voice/REALTIME.md#the-cpu-not-a-gpu)).

### Honest comparison with what this replaced

The engine before this was `Qwen/Qwen3-ASR-1.7B` on vLLM, and swapping it in
for whisper-large-v3 was **not** a free upgrade:

| | Qwen3-ASR-1.7B | whisper-large-v3 |
|---|---:|---:|
| 15 s clip | 1.04 s | 2.02 s |
| 8 concurrent clips | 1.10 s | 15.99 s (one engine) |
| audio per wall-second | ~117 s/s | ~7 s/s |
| languages | 30 | 100 |

The two engines' effect on chat is **not** directly comparable and should not
be put in that table: the old figure (−5.8 % at four concurrent dictations)
was taken on an engine that batched those four and was done in about a second,
while −50 % here (−66 % at the first measurement) is a saturation test that
keeps a GPU busy indefinitely. The
comparable statement is the weaker and truer one: whisper occupies a GPU for
roughly fifteen times as long per second of audio, and the chat model feels
whatever occupies either GPU.

Whisper is about twice as slow on a single clip and roughly fifteen times
lower in throughput, because vLLM gave the old engine continuous batching and
the transformers pipeline here gives none. What it buys is **language
breadth** — 30 languages to 100, including the Indic languages the previous
model could not transcribe at all — and a decoder whose long-form behaviour is
the reference implementation's. On English and French specifically, published
WER favours the model that was removed.

That trade was made deliberately and can be revisited: the provider
abstraction in `orchestrator/app/asr.py` is one class per engine, and
`RoutedProvider` does not care what is behind an endpoint.

### Accuracy on this workspace's languages

Measured 2026-09-29 on the production replica, against the two streaming
models the live path uses. The utterances were the same for every system, and
so was the normaliser, which is described in
[`../benchmarks/voice-live/README.md`](../benchmarks/voice-live/README.md). Word
error rate, lower is better:

| set | whisper-large-v3 | Nemotron 3.5 streaming | Nemotron EN streaming |
|---|---:|---:|---:|
| MUCS Hindi-English lectures, 34 min, script set aside | 40.2 % | **19.4 %** | 69.9 % |
| MUCS, script-sensitive | 55.8 % | **41.7 %** | 80.0 % |
| FLEURS Hindi (40) | 41.9 % | **10.9 %** (hi) / 11.8 % (auto) | – |
| LibriSpeech test-clean (60) | 4.21 % | 6.23 % | **4.13 %** |
| FLEURS English (40) | **6.81 %** | 13.3 % (en) | 10.2 % |

**On English, whisper is the best system here.** It is best on FLEURS English
and level with the English streaming model on read speech, so English
dictation keeps whisper's transcript.

**On Hindi and Hinglish it has twice the errors of the streaming model.**

- It wrote 73 of the 364 MUCS segments in Urdu script, and 5 as English
  translations.
- On FLEURS Hindi it wrote 9 of 40 sentences in Urdu script.
- End to end in a browser on 2026-09-30, four FLEURS Hindi sentences came back
  at 34.07 % from whisper, which dropped a whole clause, against 10.37 % from
  the live stream.

So for a Hindi or Hinglish session, the text inserted into the draft is the
live transcript when the live stream heard the whole recording
([`voice/REALTIME.md`](voice/REALTIME.md#which-transcript-goes-into-the-draft)).
The stored transcript is still whisper's.

**Gujarati is whisper's alone,** and it is weak there. On the 2026-09-11
meeting, Gujarati came back in Punjabi script or in Hindi letters, or
translated into English. Forced to `gu`, it produced mangled Gujarati "at
2.4–4.6× real time"
([`video-understanding/LANGUAGE-EVIDENCE-2026-09-11.md`](video-understanding/LANGUAGE-EVIDENCE-2026-09-11.md)).
Neither streaming model reads Gujarati: FLEURS Gujarati came back at 104 %.
Evaluating an Indic-language model is still open.

---

## Format

The browser records WebM/Opus (Safari: MP4/AAC) and uploads exactly what its
encoder produced, and the stored file is that byte stream. The same 15-second
clip is 2.1 MB as WAV and 145 KB as WebM/Opus, and transcribes identically.

On the session road the orchestrator decodes the stored file once, through one
long-lived ffmpeg per recording, into 16 kHz mono PCM (`audio.pcm`, deleted
when the recording is finished). It sends whisper each window as a WAV built in
memory: a 30 s window is 960,044 bytes. The one-request road sends the
recording as it was recorded, and the engine decodes it with ffmpeg as below.
The live path is the one place where the browser converts audio itself: its
worklet resamples the microphone to 16 kHz PCM
([`voice/REALTIME.md`](voice/REALTIME.md#raw-pcm-over-one-websocket)).

In the engine, `_decode` streams the upload through ffmpeg's *stdin* and reads a
16 kHz mono waveform off its *stdout*, so ffmpeg itself never writes a file.
The command is an allow-list and nothing more:
demux, decode, downmix, resample. `-nostdin` is not decoration — without it
ffmpeg competes with the parent process for the terminal's stdin.

---

## Privacy

- **The recording is stored.** This has been so since 2026-09-29 (migration
  V42): the owner decided on 2026-09-28 that dictation must be kept and must
  run for an hour or more. Each recording lives under
  `VOICE_DATA_DIR/<user_id>/<session_id>/` (`/data/voice`), with directories
  mode 0700 and files 0600. The directory holds three kinds of file:
  - `source.<ext>`, exactly as the browser encoded it;
  - the per-part and per-window records;
  - `transcript.json` and `transcript.txt`.

  Each part is checksummed and written to disk before the server acknowledges
  it. The `voice_sessions` row holds control state only, never words.
- **Who can read it:** its owner, on **Recordings**, and a super admin,
  through audited admin routes.
- **How long it is kept:** `VOICE_RETENTION_DAYS` is the only automatic
  deletion, and its default, 0, keeps recordings forever. Otherwise a recording
  is deleted only by its owner or by a super admin. Removing a member deletes
  nothing: the recordings stay listable and deletable by a super admin.
- **How much can be stored:** 50 GiB per person (`VOICE_USER_QUOTA_BYTES`).
  New audio is refused when the filesystem holding the recordings has less than
  250 GiB free (`VOICE_MIN_FREE_BYTES`).
- **The transcript reaches a conversation only if the person presses Send.**
  It is stored beside the recording, and becomes a message only then, stored
  exactly like anything they typed.
- **On the one-request road, the orchestrator writes nothing to disk.** The
  recording arrives as the request BODY, not as a multipart field, because
  `UploadFile` would have handed it to Starlette's multipart parser, which
  spools every part over 1 MiB to a temporary file. The duration and the forced
  language are query parameters, which is all they ever needed to be.
- **The engine does write temporary files.** It takes the clip as a multipart
  field, and Starlette spools any part over 1,048,576 bytes to the engine
  container's `/tmp` for the length of the decode (measured on starlette 1.6.0,
  2026-09-28). The old promise that the audio "exists in memory on both ends
  and nowhere else" was never true for a one-request dictation over about a
  minute, nor for video windows. Session windows (at most 960,044 bytes) stay
  under the threshold, and in memory.
- **The live path adds no storage for dictation.** Its PCM is never written
  anywhere, and its words live in the page. A meeting's live transcript is
  stored beside its recording ([`voice/MEETINGS.md`](voice/MEETINGS.md)).
- **Metadata:** `voice_transcriptions` (migration V19) records *metadata only*:
  who, how long, which language, how fast, and whether it worked. A finished
  recording adds one row. It has no column that could hold a word anybody said.
- **Failures count.** Failed attempts are recorded too: an error rate computed
  only from successes is not an error rate.
- **Not yet decided by the owner:** encryption at rest (the files are
  protected by their mode only); the plain-HTTP hop from the head to the
  worker's engine; and a retention window.

---

## Access control

Two independent gates, both enforced server-side:

1. **A signed-in user.** An open ASR endpoint on a shared GPU is a free
   denial-of-service against the chat model.
2. **`Feature.VOICE_INPUT`**, per member, on the admin Access page like every
   other tool. The composer hides the microphone when it is off, but hiding is
   a courtesy: the route answers 403 regardless of what the client sends.

Plus a per-user rate limit (`ASR_RATE_PER_MIN`, default 20/minute) and the
concurrency pool above. Recording sessions add their own limits:

- `VOICE_SESSION_CREATE_PER_MIN` (10) and `VOICE_PART_PER_MIN` (120);
- one live recording per person (with meetings, one of each kind:
  [`voice/MEETINGS.md`](voice/MEETINGS.md#the-server-side));
- the storage quota;
- one session window decoding at a time across the fleet.

The live path checks every stream again on its own, and re-checks every
minute: [`voice/REALTIME.md`](voice/REALTIME.md#security).

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

The live engine is a separate service on the worker's CPU, with its own script:
`scripts/stt-stream.sh up|down|status|verify|url|logs`
([`voice/REALTIME.md`](voice/REALTIME.md#running-it)). Whisper does not depend on
it. It rides on recording sessions, so it needs `ASR_ENABLED` and
`VOICE_SESSIONS_ENABLED`, but it keeps working while whisper is down.

---

## Operating it

- **Prometheus does not scrape whisper, deliberately.** `server.py` serves
  `/health`, `/v1/models` and `/v1/audio/transcriptions` and nothing else —
  there is no `/metrics` — so a scrape job for it could only ever report a
  target that is permanently down, and a red target that is red by
  construction teaches people to ignore red targets. The reasoning is written
  where the job used to be, in `monitoring/prometheus/prometheus.yml`. The live
  engine is different: it does serve `/metrics`, and job `stt-stream` scrapes
  it ([`voice/REALTIME.md`](voice/REALTIME.md#monitoring)).
- **The orchestrator** exposes three groups of metrics:
  - **Engine calls:** `asr_requests_total`, `asr_request_duration_seconds`,
    `asr_errors_total`, `asr_queue_depth`, `asr_active_requests` and
    `asr_detected_language_total`. The language label is a closed set, so a
    mis-parsed engine reply cannot mint a new series.
  - **Recording sessions:** `voice_sessions_live`,
    `voice_sessions_started_total`, `voice_sessions_total`,
    `voice_sessions_discarded_total`, `voice_session_parts_total`,
    `voice_session_windows_total` and `voice_session_pace_seconds`.
  - **The session gate in front of the engines:** `asr_session_gate_active`,
    `asr_session_gate_waiting`, `asr_session_windows_total` and
    `asr_session_window_duration_seconds`.

  Live dictation's are `voice_stream_*`.
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
  that back through a second door. Its `live` block reports the live path: the
  open streams, and each engine by number, never by address.

## Known limits

- **No batching, and that is the big one.** The engine decodes one clip at a
  time behind a `_gpu_lock`, so the fleet's capacity is exactly the number of
  engines running. The previous vLLM-hosted engine batched eight concurrent
  clips in the time of one; this does not, and the tables above are what that
  costs. Serving whisper under vLLM instead (`WhisperForConditionalGeneration`
  is in its registry) would recover continuous batching, at the price of the
  sequential long-form decoder this deployment chose it for. That is the next
  real improvement available here, and it is a genuine trade, not a free win.
- **Whisper does not stream.** Its text arrives window by window. A session
  window is committed after the speaker pauses or when 30 s fill, and its text
  reaches the browser with the answer to the next part. The words that appear
  while someone is still talking come from a different engine, on the worker's
  CPU, described in [`voice/REALTIME.md`](voice/REALTIME.md). That engine
  exists because re-running whisper for every partial would cost a full 30 s
  encoder pass each time, on a GPU the chat model shares.
- **Hindi, Hinglish and Gujarati are its weak side**
  ([Accuracy](#accuracy-on-this-workspaces-languages)). It sometimes
  translates instead of transcribing, and writes Hindi in Urdu script. For a
  Hindi or Hinglish dictation the draft gets the live transcript when it is
  complete, but the stored transcript is still whisper's.
- **Timestamps are kept, not shown.** Session windows ask the engine for
  `verbose_json` segments, and `transcript.json` stores the segments in
  milliseconds. The composer inserts text only.
- The rate limiter is in-process, so it bounds one orchestrator container.
  That is the whole deployment today; the concurrency pool is what actually
  protects the GPU.
