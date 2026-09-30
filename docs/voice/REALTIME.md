# Live dictation

The words while they are spoken. While someone dictates into the chat
composer, a partial transcript follows their voice about 0.3 s behind, and each
utterance settles just under a second after they pause ([Latency](#latency)).
It comes from a streaming speech model on the worker Spark's CPU, running
beside the stored recording, which stays the record.

Added 2026-09-30. Every number here was measured on this cluster, on the date
given. The durable path this sits beside (V42 recording sessions,
whisper-large-v3) is described in [`../VOICE.md`](../VOICE.md). Meetings use the
same pipe with two sources and are described in [`MEETINGS.md`](MEETINGS.md).
The tools that produced the numbers are in
[`../../benchmarks/voice-live/`](../../benchmarks/voice-live/README.md).

---

## What a person sees

Press the microphone. The recording bar appears as before, with cancel, the
waveform, the timer and stop. Above it the words appear while the person is
still talking. The utterance being heard is shown in muted grey and is
rewritten as the model hears more. Each finished utterance turns to the normal
ink colour once they pause. The box shows the newest four lines, and older
lines leave by the top.

The bar also has a small language control: **Auto**, **English** and
**हिन्दी**. The browser remembers the choice (`localStorage` key
`techsara-voice-language`). Changing it mid-recording restarts the live stream
in the new language from the last committed word. The recording itself is not
touched.

Press stop. One transcript goes into the message box, and which one, and
when, depends on the language ([why](#which-transcript-goes-into-the-draft)).
It is a draft and is never sent on its own:

- **Hindi or Hinglish, known from the live words:** the person chose हिन्दी,
  or at least 20 % of the live transcript's letters are Devanagari. If the
  live stream heard the whole recording, its transcript goes in as soon as the
  stream settles, without waiting for whisper: 0.43 s after Stop for a 67.2 s
  Hindi dictation ([Latency](#latency)). A quiet line says *Inserted the live
  transcript.*, with nothing to press yet. The full pass (whisper over the
  stored recording) carries on, and only when it is done does the line offer
  **Use the other one**. If the full pass fails, the line adds *The full-pass
  transcript could not be made, so there is no other one to swap in.* The live
  words stay, and there is no Retry.
- **English, and everything else:** the bar says *Transcribing…* until the
  full pass is done, and then the full-pass whisper transcript goes in, as
  before live dictation existed: 1.65 s after Stop for a 28.8 s English
  dictation. The quiet line says *Inserted the full-pass transcript.*, with
  **Use the other one** beside it. The exception: when whisper heard Hindi or
  Urdu and the person had not chosen English, the complete live transcript
  goes in instead, at that moment, because on Hindi and Hinglish whisper makes
  about twice the errors ([Accuracy](#accuracy)).

Words from the English-only model never stand in for Hindi. If any part of the
live transcript came from a connection that ran on English, the full pass goes
in and the live text stays one click away. That happens after a switch from
English to हिन्दी mid-recording. It would also happen to an `auto` dictation
that the server ran on English. Release 1's gateway never routes `auto`; the
meeting server change (release 2, V43) does
([routing](#two-streaming-models-chosen-by-language)), and the browser already
follows the language a server's `ready` reports.

**Use the other one** swaps the text in place, as long as the person has not
edited those words. Where pieces of text meet, in the panel and in the draft,
a piece that starts with closing punctuation joins the one before it without a
space. The engine can hand the danda or comma that ends one utterance to the
next one ([the protocols](#the-protocols)), so "है" followed by "। जंगल" reads
"है। जंगल", not "है । जंगल".

When live dictation fails, nothing else does. The panel falls back to the
session's own preview, which is whisper text arriving every five seconds or
so, as before. The transcript after Stop is not affected. If the full pass
fails or finds no words, and the live words did not already go in, the
follow-up line offers **Insert live transcript** whenever the live stream heard
some. If the live stream missed part of the recording, the offer reads *Insert
what was heard live (part of the recording)* instead.

Live dictation is shown only when two things are true: the deployment has a
live engine configured (`VOICE_LIVE_ENGINE_URLS`), and the person has
`VOICE_INPUT` access. Without an engine, the composer records exactly as it did
before and opens no socket.

---

## Where it runs

```
                    LIVE PREVIEW (this page)            DURABLE RECORD (V42, unchanged)
 BROWSER            AudioWorklet                        MediaRecorder
 one microphone,    16 kHz mono PCM16,                  Opus, one 5 s part at a time,
 one AudioContext   640-sample (40 ms) frames,          kept in IndexedDB until the
                    75 s ring buffer for resume         server acknowledges it
                          │ WSS /api/audio/                   │ HTTPS PUT /api/audio/
                          │ sessions/{id}/live                │ sessions/{id}/parts/{n}
 ══ Cloudflare tunnel ════╪═══════════════════════════════════╪══════════════════════
 SPARK 1 (head)           ▼                                   ▼
   frontend :3000   server-ws-relay.cjs                 route handler (_forward.ts)
                          │ ws://orchestrator:8080/           │
                          ▼ audio/sessions/{id}/live          ▼
   orchestrator     app/voice_live.py, the gateway:     app/dictation.py: stores the
   :8080            Origin, sign-in, VOICE_INPUT,       recording (0600), cuts windows
                    ownership, limits, metrics          of at most 30 s at pauses
                          │ ws://192.168.9.68:30009/          │ WAV window
                          │ v1/stream, Bearer token           │ (verbose_json)
 ── 1 GbE management LAN ─┼───────────────────────────────────┼──────────────────────
 SPARK 2 (worker)         ▼                                   ▼
                    stt-stream :30009 (CPU only)        whisper-large-v3 :30007 (GPU)
                    Nemotron streaming 0.6B int8,       one clip at a time; a second
                    sherpa-onnx, cpuset 5-9,15-19       replica runs on the head
                          │                                   │
                    partial and final text,             the transcript, after Stop
                    back up the same sockets            (the long-poll answers)
```

**One microphone, one AudioContext, two consumers.** The level meter and the
PCM tap (`frontend/public/voice/pcm-capture-worklet.js`) read one
`MediaStreamAudioSourceNode`, and MediaRecorder records the same `getUserMedia`
stream directly. The live path cannot record something the stored recording
did not, and neither path can take the microphone from the other. Public users
arrive through the Cloudflare tunnel, and on the office LAN the frontend's port
is reached directly. Either way, the browser only ever talks to the frontend.

**Nothing new runs on the head.** The engine runs on the worker, because the
head's memory is spoken for (owner rule, 2026-09-16). It has its own Compose
project, `sf-local-ai-stt`, so starting or removing it can never touch
`sf-local-ai-worker`, which is the main model's second rank. It binds the
worker's management address, 192.168.9.68. It never binds 0.0.0.0, and never a
10.100.x RoCE address, which carries the main model's tensor-parallel traffic.
It uses host networking and binds that address itself, for the reboot reason
[`VOICE.md`](../VOICE.md#where-it-runs) gives for whisper.

**The frontend relays, the orchestrator decides.** Browsers reach only
`frontend:3000`, and nothing inside Next can carry a WebSocket. A route handler
never sees an upgrade. Next's own upgrade handler calls `socket.end()` on any
WebSocket whose path matches a route (measured against the 16.3.3 standalone
build, 2026-09-29). A rewrite is frozen into the build and forwards every
header the client wrote. So `frontend/server-ws-relay.cjs` does the job
instead. It has no dependencies, and `server-preload.cjs` installs it before
Next registers anything. It owns exactly `/api/audio/sessions/{id}/live` and
pipes bytes to the orchestrator. The relay only filters out junk. The
orchestrator is reachable on the LAN, so every check that matters is repeated in
`orchestrator/app/voice_live.py`.

**The gateway stands between the browser and the engine.** The engine knows
nothing about users, sessions or cookies, and its token never leaves the head.
The gateway does four jobs:

- It authenticates the socket.
- It applies the feature gate and the limits.
- It counts what happened, with closed metric labels.
- It rebuilds every engine event from its known fields before passing it on.
  Model names, profiles and timings stay behind.

---

## The protocols

### Browser and gateway: `techsara.voice.v1`

The URL is `wss://<host>/api/audio/sessions/{session_id}/live`, or `ws://` on a
plain-HTTP LAN address, with the subprotocol `techsara.voice.v1`. The session
is a V42 recording session in status `recording`. The durable path creates it
first, and its create response says whether live dictation is on: `live` is
`{"path", "path_template", "sample_rate": 16000, "frame_ms": 40,
"resume_max_s": 60}`, or `null` when this deployment has no engine. The legacy
one-request path never goes live.

From the browser:

| message | meaning |
|---|---|
| `{"type":"start","v":1,"encoding":"pcm_s16le","sample_rate":16000,"channels":1,"frame_ms":40,"source":"mic","resume_from_sample":N,"next_u":K,"clock_offset_ms":C,"language":"auto"}` | Must be the first message, within 10 s of accept. `resume_from_sample` is the 16 kHz index of the first sample that follows (0 on a fresh stream). `next_u` is the next utterance number. `clock_offset_ms` is the capture time of sample 0 minus the time `MediaRecorder.start()` ran, which maps live samples onto the stored recording's time base; it is held to ±60,000. `language` is one of `VOICE_LIVE_LANGUAGES` (default `auto,en,hi`). `source` is `mic`; a meeting also sends `tab` ([MEETINGS.md](MEETINGS.md)). |
| binary | Little-endian signed 16-bit mono PCM at 16 kHz, one frame per message: normally 640 samples (1,280 bytes), always an even length from 2 to 16,384 bytes. |
| `{"type":"flush"}` | Stop: no more audio. The pending utterance is committed, its final is sent, then `done`, then close 1000. The browser gets its `done` within 5 s whatever the engine does. |
| `{"type":"ping","t":x}` | Answered with `{"type":"pong","t":x}`. The browser pings every 10 s and treats 10 s without an answer as a dead connection. |
| `{"type":"client_stats","partial_ms":[…],"final_ms":[…]}` | The browser's own capture-to-render latencies, at most one message per 5 s and 64 numbers per list, each clamped to [0, 60,000] ms. Telemetry only: a malformed one is ignored. |

From the gateway, every message is JSON text:

| message | meaning |
|---|---|
| `{"type":"ready","v":1,"sample_rate":16000,"frame_ms":40,"max_frame_bytes":16384,"resume_from_sample":N,"next_u":K}` | Sent once the engine has accepted the stream. With the meeting server change (release 2) it also says the `language` actually used ([routing](#two-streaming-models-chosen-by-language)). The browser takes that as the language the connection ran on; release 1 sends none, and the language the `start` asked for stands. |
| `{"type":"partial","u":k,"text":…,"start_sample":a,"end_sample":b}` | The WHOLE current hypothesis of utterance *k*. It replaces the previous partial and is never appended to it. |
| `{"type":"final", …same fields…}` | Utterance *k* is committed and its partial cleared. Finals arrive in order of *u*. An utterance with no words produces no final. |
| `{"type":"speech","active":true,"sample":n}` | An utterance's first words appeared (`true`), or it was committed (`false`). The engine derives it from the transcript. The UI may ignore it. |
| `{"type":"done"}` | A flush has completed. |
| `{"type":"error","code":…,"message":…,"retryable":…}` | Sent once, then the socket closes with one of the codes below. |

Times are sample indexes, so a reconnect keeps one time base. The browser
places a live utterance on the recording's clock at `sample / 16 +
clock_offset_ms`. That is how the live words are merged with whisper's preview
while recording: the preview covers the audio up to `transcribed_ms`, and a
live utterance is shown only past that point.

**An utterance can start with punctuation.** The model decides how a sentence
ends only once it hears the next one begin. So a committed utterance's comma or
danda can arrive as the first token of the next one: `", and I was told"`,
`"। लेकिन"`. This happened to 5 of 40 LibriSpeech finals and 11 of 40 FLEURS
Hindi finals on the worker. The mark is kept, because a Hindi transcript
without its dandas is wrong. A consumer that joins utterances must join one
that starts with closing punctuation *without* a space, and every other one
with a single space. The browser does, everywhere it joins text
([joining](#which-transcript-goes-into-the-draft)).

### Close codes

Every refusal but one first accepts the socket, sends one `error` message and
closes with a code, because a browser never shows a handshake's status to
script. The exception is a foreign or missing Origin, which gets a bare HTTP
403 before accept.

| code | `error.code` | when | the browser |
|---|---|---|---|
| 1000 | | after `done`, or the browser's own cancel | stops |
| 1012 | | the orchestrator is restarting (a deploy) | reconnects and resumes |
| 4400 | `protocol`, `unsupported_language` | a bad start, frame or message; a language this deployment does not offer | stops |
| 4401 | `signed_out` | not signed in, or signed out since (the re-check) | stops |
| 4403 | `voice_off` | `VOICE_INPUT` is off, now or since | stops |
| 4404 | `live_unavailable`, `not_found`, `session_closed` | no live path here; not the caller's recording (the same answer as an unknown id); the recording is no longer recording | stops |
| 4408 | `idle` | no audio frame for about 60 s: the engine's `STT_IDLE_S` (60 s) closes the stream first, and the gateway's `VOICE_LIVE_IDLE_S` (120 s) is a backstop | reconnects while still recording |
| 4409 | `superseded` | a newer connection took this recording over | stops |
| 4413 | `frame_too_large` | a frame over `VOICE_LIVE_MAX_FRAME_BYTES` | stops |
| 4429 | `rate_limited`, `capacity` | over `VOICE_LIVE_CONNECTS_PER_MIN`; audio or messages faster than the clock allows; the gateway is full, or no engine has room for the language (every profile for it is full, or the decode budget is spent) | backs off and retries |
| 4500 | `internal` | the gateway failed | backs off and retries |
| 4503 | `engine_unavailable` | no engine answered, or the engine went away mid-stream | backs off and retries |

A socket the relay refuses never reaches the gateway. The relay answers with a
plain HTTP status and no body, and logs the reason: 405, 400, 426, 403
(`cross_site`, `origin_missing`, `origin_mismatch`), 401 (`no_session`) or 503
(`capacity`, `draining`). If the orchestrator is unreachable the relay answers
502, and if it does not answer the handshake within 15 s, 504. The browser sees
all of these as an abnormal close (1006), which it retries like a drop.

### Resume after a drop

A deploy recreates the orchestrator and the frontend. The tunnel drops long
connections, and phones change networks. So the browser keeps the last 75 s of
PCM: a minute of replay, plus 15 s of headroom so that the microphone cannot
overwrite the resume point during the handshake.

After an unexpected close it reconnects with backoff while still recording:
0.5, 1, 2, 4 and 8 s, then every 15 s, each ±20 %. The backoff starts over only
once a connection has proven itself, by bringing words back or by staying up
for 10 s. The new `start` carries `resume_from_sample`: the end of the last
final, but never more than 60 s back. It also carries `next_u`. The buffered
PCM from that point is replayed, then the stream continues live. Partials for
replayed audio arrive faster than real time.

An endpoint final's `end_sample` lies inside the silence that closed it, so a
resume from it neither repeats the last word nor clips the next one. The model
emits a word a median 100 ms (p90 286 ms) before the word ends. Before the
engine placed `end_sample` in the silence, resuming from the last word's
emission time re-decoded its tail as a new first word in 6 of 60 real
reconnects. With `end_sample` in the silence it happened in 0 of 60.

### Gateway and engine

The gateway opens `ws://<engine>/v1/stream` with the header `Authorization:
Bearer <VOICE_LIVE_ENGINE_TOKEN>`. The engine compares the token in constant
time. Without it, the engine closes before accept, which uvicorn answers with a
bare HTTP 403. The messages have the browser protocol's shapes, with these
differences:

- **start:** `{"type":"start","sample_rate":16000,"encoding":"pcm_s16le","first_sample":N,"first_u":K,"mode":"dictation","language":"auto"}`.
  `mode` is `dictation` or `meeting`. An optional `frame_ms` (40 when absent)
  sizes the engine's message ceiling. The engine answers `ready`, which names
  the profile it chose.
- **events:** partials and finals also carry `compute_ms`: the decode wall time
  charged to the stream since its previous event, not CPU time. Every stream in
  a `decode_streams` batch is charged the whole call, the rule the real-time
  factor uses ([Monitoring](#monitoring)). Finals carry `endpoint_ms`,
  the trailing silence that decided the endpoint. The gateway drops both
  fields, and the profile.
- **refusals:** 4400 for a protocol error or an unsupported language or mode;
  4408 idle (`STT_IDLE_S`, 60 s); 4413 frame too large; 4429 capacity (a
  profile or the engine's decode budget is full) or arrival rate; 4500
  internal; 4503 still loading. On shutdown every stream closes 1012.

The gateway handles an engine's answer in three ways:

- **Full, or not transcribing that language:** the engine is healthy, so it is
  not stood down. The gateway tries the next engine, and if none takes the
  stream it passes the refusal on (4429 `capacity`, or 4400).
- **Unreachable, not ready within `VOICE_LIVE_ENGINE_CONNECT_S` (5 s), or
  refusing for another reason:** the engine is stood down for 20 s, doubling
  with each consecutive failure up to 600 s, as `asr.RoutedProvider` does for
  whisper. The next engine is tried.
- **No engine healthy:** the browser gets 4503.

The engine also serves `GET /health` and `GET /metrics`. `/health` returns
readiness, open streams, capacity and model labels, never a file path.
`/metrics` is Prometheus text with closed labels and no ids.

---

## Why it is built this way

### The CPU, not a GPU

Chat is tensor-parallel across both Sparks and runs at the speed of its slower
rank. A busy GPU on either node therefore slows every conversation. One
saturated whisper replica took single-stream chat decode from 107 to 53 tok/s,
and one on each node took it to 28 (2026-09-28). The GB10 cannot partition
its GPU: MIG reads N/A, and work from different CUDA contexts is time-sliced,
not run side by side (NVIDIA's MPS documentation). A streaming engine running
continuously on either GPU would be a permanent share of every chat answer.

Streaming whisper would be worse, not better. Its encoder always takes a
30-second window, so a partial every half second costs a full encoder pass
each time. The vLLM realtime endpoint's Qwen3-ASR hard-codes 5 s segments, so
its first partial cannot come sooner than 5 s.

The worker's CPU is almost idle: 2.4 % on average over 24 hours (2026-09-29).
The candidate was measured there first:

| on the worker's CPU (sherpa-onnx 1.13.8, int8, 2026-09-29) | result |
|---|---|
| one decode step, Nemotron 0.6B, 160 ms chunk, 2 threads | 31.6 ms |
| 4 decode threads × 2, 160 ms chunks | 12 real-time streams on about 5 cores; decode tick p95 99-102 ms of the 160 ms budget |
| chat decode while those 12 streams ran | 107.0-107.9 tok/s against a 107.2-108.9 baseline: no measurable change |
| all 20 cores saturated (48 streams) | 89.6-95.9 tok/s (−11 to −16 %), and TTFT rose |

The CPU is not free either. The CPU and the GPU share the same 273 GB/s of
memory. So the engine is held to the ten Cortex-X925 cores (`cpuset:
"5-9,15-19"`) and at most eight cores' worth of time (`cpus: 8`), and it
refuses streams beyond its measured capacity instead of letting them fall
behind ([Capacity](#capacity)).

### Two streaming models, chosen by language

Only two open models from NVIDIA stream natively, with each 80 ms frame
encoded once. Both are 0.6B cache-aware FastConformer transducers, and both
come as int8 exports for sherpa-onnx, which runs on aarch64 without a GPU:

- **Nemotron Speech Streaming EN 0.6B** (NVIDIA Open Model License): English
  only.
- **Nemotron 3.5 ASR Streaming 0.6B** (OpenMDW-1.1): 40 locales, including
  hi-IN, with its own language identification. Output is cased and punctuated.

The two models are complementary:

- **English:** the English model makes a third fewer errors on English
  (LibriSpeech 4.13 % against 6.23 %).
- **Hindi and Hinglish:** only the multilingual model writes Hindi, and on
  Hinglish it has half whisper's error rate ([Accuracy](#accuracy)).

An independent leaderboard agrees. The Hugging Face Open ASR leaderboard,
updated 2026-09-25, scores the models offline, not streaming:

- On Monsoon hi-IN, which is heavily code-mixed and scored with OIWER, the 3.5
  model scores 13.85 against whisper-large-v3's 28.17.
- On Monsoon en-IN, the English model scores 4.44 against the multilingual
  model's 7.30.

So the engine loads four profiles: each model at two chunk sizes. A stream goes
to the first profile, in this order, that allows its language and has room:

| profile | model | chunk | languages | streams |
|---|---|---:|---|---:|
| `en-fast` | Nemotron Speech Streaming EN, int8 | 160 ms | en | 8 |
| `multi-fast` | Nemotron 3.5 ASR Streaming, int8 | 160 ms | auto, hi, en | 8 |
| `en-wide` | Nemotron Speech Streaming EN, int8 | 560 ms | en | 12 |
| `multi-wide` | Nemotron 3.5 ASR Streaming, int8 | 560 ms | auto, hi, en | 12 |

The 160 ms profiles answer first. The 560 ms profiles take the overflow: they
cost half as much CPU per stream, but they decode in 560 ms steps, so their
words appear later.
`auto` and `hi` go to the multilingual model. `en` goes to the English model,
and to the multilingual one only when every English profile ahead of it is
full. The profile list is part of `compose/compose.stt-stream.yaml`: changing it
is a reviewed edit, not an environment knob.

**Gujarati is not offered.** Neither model reads it: FLEURS Gujarati came back
at 104 % WER. `VOICE_LIVE_LANGUAGES` must never list `gu`. A Gujarati speaker
still gets whisper's transcript after Stop, as before.

**Routing `auto` per person** comes with the meeting server change (V43,
release 2). Release 1's gateway does not route, and its `ready` carries no
`language`. With the change, when the browser asks for `auto`, the gateway
routes the stream to `en` if the person's last five finished dictations were
all English. Otherwise `auto` stays. The rule never applies to a meeting's
`tab` source, because the other people's language is not the person's. It
never routes to a language the deployment does not offer, and a history it
cannot read leaves `auto`. `ready` reports the language used, and the browser
already honours it: the words of a stream the server ran on `en` are the
English-only model's, so they keep the full pass in the draft
([Which transcript goes into the draft](#which-transcript-goes-into-the-draft)).
Choosing English or हिन्दी in the bar overrides the routing.

**There is no separate voice-activity detector.** An utterance begins when its
first words are recognised. It ends on the recognizer's own endpoint rule:
trailing silence after text. Every chunk is decoded, speech or silence, so an
open stream costs the same whether or not anyone is talking, and capacity is
counted in open streams. The research's Silero gate, measured at about
0.1-0.25 of one core per 100 streams, was meant to keep silence off a GPU. It
is not built.

### Segmentation never resets mid-speech

The obvious design resets the recognizer at every endpoint and starts each
utterance clean. Measured on LibriSpeech (30 utterances joined by 0.8 s
pauses, 160 ms export), that more than doubles the word error rate: 5.86 %
becomes 13.71 %, or 12.94 % with a lead-in pad after each reset. A 0.6 s pause
is a short one. The next word's onset is often already inside the encoder's
context, and a reset throws it away.

So the engine keeps decoding one growing hypothesis and remembers how many of
its characters are already committed:

- **After each decode step:** the text past that point is the partial for the
  current utterance.
- **When the endpoint rule fires:** that is, the trailing silence after text
  reaches `STT_ENDPOINT_S` (0.6 s; 0.9 s for meetings). If the partial has
  words, it becomes a final.
- **Speech that runs past 30 s** (25 s in a meeting) is forced out as a final,
  all but its last three words, which may be a word still in progress.
- **Streams are renewed only at safe points:** right after an endpoint final,
  once the silence has lasted `STT_RENEW_SILENCE_S`. A renewal swaps the
  stream for a fresh one, which starts inside the silence and re-decodes the
  audio from there. This is what a reconnect does, too. A long-lived stream
  drops whole clauses, and a fresh one sometimes drops the first word after
  it, so the threshold decides the balance. It was measured through the engine
  on the worker (2026-09-30: 4 sessions × 5 utterances, 2.5 s between
  utterances; WER):

  | renewal | FLEURS Hindi | LibriSpeech, English model |
  |---|---:|---:|
  | off | 15.78 % | 3.97 % |
  | at every 0.6 s endpoint | 13.78 % | 6.20 % |
  | after 1.2 s of silence | 12.22 % | 4.96 % |
  | after 2.0 s of silence | 12.44 % | 4.47 % |
  | **after 2.0 s of silence, with the level check (the default)** | **12.89 %** | **4.47 %** |

  At 0.6 s every short pause inside a sentence renews the stream, and errors on
  first words rose from 3 to 8 in English. The build spec asked for 0.6 s; 2.0 s
  keeps most of the Hindi gain at half a point of English. The level check
  skips a renewal when the newest audio already stands out from the pause: a
  word has begun, and its first token is not out yet. Without it, English first
  partials came about 250 ms later (p50 543 to 792 ms).

Two more measured fixes:

- **160 ms of silence in front of every new stream.** Nemotron 3.5 drops the
  opening words of a stream that starts on speech. The first word survived
  4/30 times with no padding, 11/30 with 80 ms and 27/30 with 160 ms
  (`STT_LEAD_PAD_MS`).
- **Silence after the last word at Stop.** The model decodes its last chunk
  only once a whole right context has arrived. The last word survived 1/30
  times with no padding, 14/30 with 250 ms and 26/30 with 500 ms. A flush pads
  800 ms at 160 ms chunks.

### Which transcript goes into the draft

Both recognisers heard the same speech, and the better one depends on the
language:

- **English:** whisper-large-v3 and the English streaming model are close on
  read speech (LibriSpeech 4.21 % against 4.13 %). Whisper is clearly ahead on
  FLEURS English (6.81 % against 10.2 %). So the full pass goes in.
- **Hindi-English lectures:** on 34 minutes of MUCS lectures, whisper's error
  rate was twice the live model's (40.2 % against 19.4 %, script set aside).
  Whisper also wrote 73 of the 364 segments in Urdu script and translated 5
  into English. So the live transcript goes in, but only a complete one: the
  stream ended with `done`, the ring buffer had no hole, and no reconnect
  skipped audio. A live stream that missed part of the recording never stands
  in for all of it.

A session counts as Hindi or Hinglish when the person chose हिन्दी, when at
least 20 % of the live transcript's letters are Devanagari, or when whisper
heard Hindi or Urdu and the person had not chosen English (`chooseFinalText`
in `frontend/lib/voiceLive.ts`).

**Never the English-only model's words.** On the MUCS lectures that model
scored 69.9 % with script set aside and 80.0 % script-sensitive, against
whisper's 40.2 % and 55.8 %. So if any connection of the recording ran on `en`
and committed words, the full pass goes in, and the live text stays one click
away. That covers a switch from English to हिन्दी mid-recording: the new
stream resumes after the last final, so what came before it is the English
model's. It also covers an `auto` stream the server ran on English. The
browser takes each connection's language from `ready.language` when the server
sends one, and from its own `start` otherwise. Release 1's gateway does not
route and sends no `language`, so there the choice in the bar is the whole
story. The meeting server change (release 2) routes `auto` to `en` for people
whose last five dictations were English
([routing](#two-streaming-models-chosen-by-language)), and the browser already
honours what its `ready` says.

**When it goes in.** The finish request never waits for the live stream. The
live stream settles at most 3.3 s after Stop (0.3 s for the worklet's last
frame, then 3 s for the stream's `done`); in the Hindi
[browser tests](#latency) its `done` came 0.2 s after the browser's `flush`.
The full pass takes seconds to minutes. So:

- **When the live words decide alone,** the live transcript goes in as soon as
  the stream settles. That is when the person chose हिन्दी or its letters are
  at least 20 % Devanagari, and the live transcript is complete, has words,
  and has none from the English-only model. Whisper's language could only add
  a reason to choose the live words, so the full pass cannot change the
  answer. In Run 3, the browser test of the build that ships, a 67.2 s Hindi
  dictation's live transcript was in the draft 0.43 s after Stop. The
  pre-review Run 2 waited for the full pass, which took 44 s of whisper's time
  and ended 26.56 s after Stop, and then inserted the live transcript anyway.
  The recording bar closes once the server has accepted the finish, which does
  not wait for whisper. The line offers **Use the other one** only when the
  full pass is done, and the full pass never writes into the draft by itself.
  If it fails, a quiet note says there is no other transcript to swap in. It
  offers no Retry, which would write whisper's text over the live words.
- **Otherwise the full pass is waited for,** English and Auto without
  Devanagari above all. Once it is in, the insert waits at most 1 s longer for
  the live stream's last words, and only while they could still change the
  choice. For a stream heard in English (asked for `en`, or an `auto` the
  server ran on English) with under a fifth of its letters Devanagari, they
  cannot, and nothing waits. In Run 3 a 28.8 s English dictation's full pass
  was in the draft 1.65 s after Stop; the pre-review Run 2 waited for the live
  stream and took 4.69 s. A live stream not done within that second counts as
  incomplete.
- **A recording the server stopped** (storage or quota full, or closed
  elsewhere) always waits for the full pass, whose result says why it stopped.

**Use the other one** puts the other transcript where the first one went, and
only over the exact words that went in. Once the person has edited them,
nothing is swapped, and the line says so.

**Joining.** A piece of text that starts with closing punctuation (`,` `.`
`;` `:` `!` `?` `।` `॥` `)` `]` `}` `…`, and their full-width and ideographic
forms) joins the text before it without a space. The browser applies this in
the panel, in the live transcript it inserts, and where a transcript meets
words already typed (`joinPreview` and `mergeTranscript` in
`frontend/lib/voice.ts`). So "है" followed by "। जंगल" reads "है। जंगल", and
"told" followed by ", and" reads "told, and".

### No second pass

The build spec's first accuracy plan (section 9A) re-decoded every committed
utterance on a wider-chunk profile and replaced the text a second later. It was
measured and dropped:

- **Wider chunks do not help Hinglish.** On MUCS the 3.5 model scored 19.4 % at
  160 ms, 20.0 % at 560 ms and 20.7 % at 1,120 ms.
- **English finals come from whisper anyway.**
- **The CPU a second pass needs is better spent on concurrency.**

The protocol therefore has no revision field. A final is final.

### Raw PCM over one WebSocket

At 16 kHz mono, PCM16 is 256 kbit/s, or about 115 MB an hour. That is small
next to the 1 GbE LAN between the Sparks, and it costs no CPU to decode. The
stored recording is already the Opus parts, so nothing here needs compressing
twice. `permessage-deflate` is off everywhere: PCM does not compress, and
deflate would spend CPU the decoders need.

The worklet resamples explicitly, and does not trust the browser to do it.
Firefox ignores a `sampleRate` constraint on the microphone and only recently
started resampling MediaStream input (Firefox 148 and 151). The worklet uses one
Kaiser-windowed sinc low-pass (−6 dB at 7 kHz) read at the output instants. Its
measured response on sine waves:

- within 0.01 dB of the input level up to 6 kHz;
- −62.5 to −63 dB at 8.5 kHz;
- −71 dB (48 kHz input) and −75 dB (44.1 kHz input) at 12 kHz.

The position is kept as an exact fraction, so an hour of 44.1 kHz audio does
not drift by a sample.

---

## Security

The live path handles someone's voice and words, so every layer checks what it
can, and the layers after the browser assume nothing about the ones before.

**The browser** opens the socket only to its own origin. The page's CSP gains
no `ws:` or `wss:` source, because a same-origin socket is covered by `'self'`
(`frontend/tests/edge-csp.test.ts` pins that).

**The relay** (`frontend/server-ws-relay.cjs`) refuses before any connection to
the orchestrator exists. It requires:

- `GET` with `Upgrade: websocket`, a well-formed key and version 13;
- `Sec-Fetch-Site`, when present, of `same-origin` or `none`;
- an `Origin` that is the Host's own origin (hostname and effective port) or is
  listed in `FRONTEND_WS_ALLOWED_ORIGINS`;
- a `ts_session` cookie;
- fewer than `FRONTEND_WS_MAX_RELAYS` (512) relays already open.

Upstream it sends a named header set only: the handshake's own headers,
`cookie`, `origin`, `user-agent`, the original Host as `x-forwarded-host`, and
`x-forwarded-for`/`x-forwarded-proto` only as `lib/proxy.ts` would. No
forwarding header a client wrote gets through.

Two timeouts bound each relay: 150 s with no byte in either direction (uvicorn
pings every 20 s, so a live socket is never that quiet) and 15 s for the
handshake. On SIGTERM, relays are cut 2 s into the drain, so a deploy never
waits on them. Upgrades on any other path that nobody answers are destroyed
after 10 s. One of those once held a deploy's exit open for as long as its
client liked (2026-09-29).

**The gateway** repeats every check, because the orchestrator is reachable on
the LAN and a WebSocket passes this app's middleware untouched: the cross-site
write check, CORS and the body cap all ignore it. In order, before a byte of
audio is taken:

1. **Origin, before accept.** It must be in `CORS_ALLOW_ORIGINS`, or be the
   host the browser connected to. `X-Forwarded-Host` is believed only from one
   of our own proxies. Anything else gets a bare 403. A WebSocket answer is
   readable cross-origin, and `SameSite=Lax` lets a sibling subdomain's page
   send the cookie, so without this check a page elsewhere could read somebody's
   dictation.
2. **The sign-in session,** resolved from the cookie (4401).
3. **`Feature.VOICE_INPUT`** (4403).
4. **The deployment's switches:** `ASR_ENABLED`, `VOICE_SESSIONS_ENABLED`,
   `VOICE_LIVE_ENABLED`, and at least one engine URL (4404).
5. **The recording:** it must be the caller's and still recording. A
   stranger's recording and an unknown id get the same 404.
6. **`VOICE_LIVE_CONNECTS_PER_MIN`** (30 per person per minute, 4429).
7. **`VOICE_LIVE_MAX_STREAMS`** (64 per process, 4429), with one stream per
   recording: a new connection supersedes the old one (4409).

**The checks do not stop at the handshake.** A socket resolves its sign-in
once, and nothing pushes a revocation. So every `VOICE_LIVE_REVALIDATE_S` (60 s)
an open stream resolves its cookie again, past the per-connection cache, and
re-reads the feature and the recording's status. Signing out, a deactivation, a
removal or voice being switched off ends the stream within a minute.

**Everything that arrives is bounded:**

| limit | value | on breach |
|---|---|---|
| audio frame | even length, 2 to `VOICE_LIVE_MAX_FRAME_BYTES` (16,384) bytes; uvicorn refuses any message over 1 MiB first (`--ws-max-size 1048576`) | 4413 / 4400 |
| audio rate | never more samples than (wall seconds since start + 60 + 5) × 16,000; a reconnect may replay its buffer at once, a microphone cannot outrun the clock | 4429 |
| message rate | never more messages, audio or text, than (wall seconds + 65) × 50 at 40 ms frames; this is what stops a flood of 2-byte frames or pings, which each cost the shared event loop about what a real frame costs | 4429 |
| text message | 16,384 characters; nesting too deep to parse is a protocol error | 4400 |
| start | within 10 s of accept | 4400 |
| silence of the socket | no audio frame for about 60 s: the engine's `STT_IDLE_S` closes first, and the gateway's `VOICE_LIVE_IDLE_S` (120 s) is a backstop | 4408 |
| text in an event | 4,000 characters; the gateway drops a longer one | — |

**The engine** authenticates the gateway with its own token, which is 32 random
bytes. It lives in two places:

- on the head, in `.runtime/secrets.env` as `VOICE_LIVE_ENGINE_TOKEN`;
- on the worker, in `~/.techsara-cluster/stt-stream/stt.env` as `STT_TOKEN`.

Both files are mode 0600, and the token is never on a command line. Compose
does not name it under `environment:`, so a recreate without the secrets layer
cannot blank it. The engine refuses to start without a token, or with one
shorter than 16 characters.

The container is locked down: a read-only root filesystem, `/tmp` as tmpfs, no
capabilities, `no-new-privileges`, uid 10009, at most 256 processes,
`mem_limit: 8g` and `oom_score_adj: 900`. The OOM score puts it first in line
for the kernel's OOM killer on the worker, beside the OCR engine: losing it
costs only the preview.

**The host guard.** The engine's stream demands the token, but its `/health`
and `/metrics` do not. They carry no ids and no text, but they belong behind the
worker's packet filter like every other engine port. The repository's
`scripts/host-guard.sh` lists 30009 for the worker. The filter actually running
on the worker is the copy installed as root, so the port is judged only once
that copy is reinstalled ([Running it](#running-it)). Until then, 30009
answers the office LAN.

**Logs carry no audio and no words.** No cookie, no token, no query string.
Each component logs one structured line when a stream closes:

- **The relay:** `ws_refused`, with the reason, `ws_open`, and `ws_close`,
  with the duration and byte counts. It never logs a session id, and logs the
  Origin only when the Origin was the reason for a refusal.
- **The gateway:** the session id, the outcome, the close code, the duration,
  seconds of audio, utterances, and whether the stream was a reconnect.
- **The engine:** counts and durations only.

Metric labels are closed sets, and no metric names a person or a session.

**What is stored, and for how long.** The live path adds no storage of its own
for dictation:

- **PCM** exists only in the browser's 75 s ring and in the engine's memory.
  None of it is ever written to disk.
- **Dictation's live words** stay in the page, and in the draft if they are
  inserted.
- **The recording** is the V42 session's stored Opus file. Whisper's transcript
  is stored beside it.
- **A meeting's live transcript** is appended to `live.jsonl` in the same
  session directory ([MEETINGS.md](MEETINGS.md)).

Retention is therefore the V42 session's. The person or a super admin can
delete a recording, and so can `VOICE_RETENTION_DAYS`, whose default of 0 keeps
recordings forever. Removing a member deletes nothing.

---

## Capacity

| where | limit | default |
|---|---|---|
| engine, per profile | `max_streams` in `STT_PROFILES` | 8, 8, 12, 12 |
| engine, all profiles together | `STT_MAX_COST` decode budget: a 160 ms stream costs 2 units and a 560 ms stream 1 | 32 units, the cost of 16 streams at 160 ms; with the shipped profiles, 20 `auto` streams or 16 `en` ones (below) |
| engine, threads | `STT_WORKERS` decode threads per profile × `STT_THREADS` ONNX Runtime threads each | 2 × 2 (the compose file) |
| gateway, per orchestrator process | `VOICE_LIVE_MAX_STREAMS` | 64 |
| gateway, per person | `VOICE_LIVE_CONNECTS_PER_MIN`; one stream per recording (per source in a meeting) | 30 a minute |
| relay | `FRONTEND_WS_MAX_RELAYS` (0 refuses every live socket) | 512 |

**Where the numbers come from.** Twelve 160 ms streams kept 4.93 cores busy on
the worker, which is 0.41 of a core each. One four-thread decode loop held
about 7 real-time streams at 160 ms and about 14 at 560 ms, so a 560 ms stream
costs half as much. Sixteen streams at 160 ms, at 0.41 of a core each, take 6.6
of the eight cores and leave about 1.4 for the event loop, feature extraction
and the p95 of a decode step. At 2 units a stream, those sixteen are the
32-unit budget. The per-profile caps add up to 40 streams, about 11.5 cores of
decode, so the caps alone would admit more than the cores can decode.

**How many streams fit.** Both limits apply at once. A stream takes the first
profile in the list that allows its language, has a free slot and still fits
the budget. So a language's 560 ms profiles take streams only when its 160 ms
profiles are full, or when the budget has one unit left. The engine's own
admission code, run with the shipped `STT_PROFILES` and `STT_MAX_COST=32`
(2026-09-30), admits:

| traffic | streams | where they sit | what refuses the next one |
|---|---:|---|---|
| all `auto` (or all `hi`), the default | 20 | `multi-fast` 8, `multi-wide` 12: 28 of the 32 units | the profile caps: both profiles for the language are full |
| all `en` | 16 | `en-fast` 8, `multi-fast` 8: all 32 units | the budget: `en-wide` never gets a stream |
| `auto` first, then `en` | 22 | `multi-fast` 8, `multi-wide` 12, `en-fast` 2 | the budget |

No mix of languages gets past 22 while streams only open. Streams closing and
reopening in a particular order can hold 24. Nothing reaches 32 streams,
although `/health` reports a `capacity` of 32 (the budget divided by the
cheapest profile's cost). So for the default language it is the profile caps,
not the budget, that refuse first; for English it is the budget.

**At the cap, in production** (2026-09-30, about 04:00 IST). The production
engine, `sf-local-ai-stt-stt-stream-1` on the worker (cores 5-9 and 15-19,
8 CPUs, 8 GiB, the four shipped profiles, `STT_MAX_COST` 32), was loaded with
`stream_bench.py --target engine --set librispeech --language en --sessions 16
--utterances 8`, the client on cores 0-4. All 16 streams were admitted,
`en-fast` 8 and `multi-fast` 8 for all 32 units, as the table above predicts
for English, and 128 utterances streamed in 99.5 s:

| 16 streams at 160 ms, English | p50 | p90 | p99 |
|---|---:|---:|---:|
| first partial, from the frame that holds the speech onset | 673 ms | 1,603 ms | 2,974 ms |
| final, from the last voiced frame (the endpoint's silence wait included) | 876 ms | 1,352 ms | 2,808 ms |
| partial lag, behind the audio each partial covers | 372 ms | 896 ms | 2,536 ms |

The WER was 3.83 %, with 0 stream errors. The main model's single-stream
decode, probed from the head (`chat_probe.py`), ran at a median of about
103 tok/s during the load and about 108 tok/s after it: about −4 %. Slow
outliers appeared with and without the load, from other production traffic.

Each profile loads one recognizer, and all of that profile's decode threads
share it. Four threads decoding different streams through one recognizer gave
16 of 16 transcripts identical to a sequential run. The engine's memory was
3.58 GB resident with its four models loaded, and 4.08 GB with twelve streams
open. Its memory, unlike a GPU engine's, is charged to the container's cgroup,
so `mem_limit: 8g`, twice the measured peak, bounds the whole engine.

**At capacity** the engine answers `capacity`. The gateway passes it on as a
retryable 4429, and the browser retries while it records: 0.5, 1, 2, 4 and 8 s,
then every 15 s. Meanwhile the person sees the session's own preview, and the
transcript after Stop is complete as always. Nothing queues, because a preview
that falls behind the voice is worse than none.

Raising capacity trades against chat, and the decision is the owner's. Twelve
streams cost chat nothing measurable. Sixteen English streams at the
production cap cost it about 4 % (above). Forty-eight streams with every core
busy cost it 11-16 % of its decode speed.

Network: 256 kbit/s a stream, so 64 streams are about 16 Mbit/s on the 1 GbE
management LAN.

---

## Monitoring

There are two sources. The orchestrator's gateway exposes `voice_stream_*` on
its own `/metrics` (job `orchestrator`). The engine exposes `stt_stream_*` (job
`stt-stream`, a static target at 192.168.9.68:30009, scraped every 15 s, with
the labels `node: spark-2` and `role: worker`).

| metric | what it counts |
|---|---|
| `voice_stream_sessions_active` | streams open in the process (a gauge read at scrape time) |
| `voice_stream_sessions_started_total` | streams admitted |
| `voice_stream_sessions_total{outcome}` | how each admitted stream ended: `completed`, `client_closed`, `disconnected`, `superseded`, `idle`, `rejected`, `engine_unavailable`, `error`; settled exactly once per stream |
| `voice_stream_rejections_total{reason}` | `origin`, `signed_out`, `voice_off`, `unavailable`, `not_found`, `rate_limited`, `capacity`, `protocol`, `frame_too_large` |
| `voice_stream_errors_total{reason}` | `engine_unavailable`, `engine_timeout`, `engine_protocol`, `internal`; one per failed engine attempt or rejected engine event, so not a per-stream count |
| `voice_stream_audio_received_seconds_total` | seconds of audio received; a replay counts again |
| `voice_stream_utterances_total` | finals relayed |
| `voice_stream_first_partial_seconds` | an utterance's first sample arriving at the gateway, to its first partial written |
| `voice_stream_final_seconds` | an utterance's last sample arriving, to its final written |
| `voice_stream_event_lag_seconds{event}` | the newest sample an event covers arriving, to the event written |
| `voice_stream_client_e2e_seconds{event}` | capture to render, as the browser measured it (untrusted, clamped) |
| `stt_stream_active_streams{profile}`, `stt_stream_streams_total{profile,outcome}` | the engine's own streams |
| `stt_stream_audio_seconds_total{profile}`, `stt_stream_compute_seconds_total{profile}` | the real-time factor's two halves |
| `stt_stream_decode_step_seconds{profile}`, `stt_stream_batch_size{profile}` | one `decode_streams` call against its chunk budget |
| `stt_stream_rejections_total{reason}` | `unauthorized`, `not_ready`, `protocol`, `language`, `capacity`, `other` |
| `stt_stream_events_total{kind}` | partials, and endpoint, forced and flush finals apart: forced finals should be rare |

The four latency histograms share one bucket ladder: 0.05, 0.1, 0.15, 0.2,
0.3, 0.4, 0.5, 0.75, 1, 1.5, 2, 3, 5, 10 and 30 s.

**The real-time factor** is compute seconds over audio seconds, per profile,
over 5 minutes. The engine charges every stream in a `decode_streams` batch the
batch's full wall time. So 1.0 means a decode worker needs a whole chunk's
length to decode one chunk: every stream on it is falling behind. A saturated
engine reads exactly 1.0, however far behind it is, and the latency alerts show
how far.

**Rules:** `monitoring/prometheus/rules/voice-stream.yml` holds 9 recording
rules and 6 alerts. Every alert is a warning, because a live failure costs the
preview and never the transcript.

| alert | fires when | for |
|---|---|---|
| `VoiceStreamFirstPartialSlow` | first-words p95 over 1.5 s, with at least 20 utterances per 5 minutes | 10m |
| `VoiceStreamFinalSlow` | final p95 over 2 s, with at least 20 finals per 5 minutes | 10m |
| `VoiceStreamEngineFallingBehind` | `cause="decode_rtf"`: a profile's real-time factor over 0.8 while it carries at least half a real-time stream; `cause="capacity_refusals"`: at least 3 capacity refusals in every 5-minute window | 5m |
| `VoiceStreamErrorRatioHigh` | over 10 % of the streams that ran ended `engine_unavailable` or `error`, at least 3 per 5 minutes | 10m |
| `VoiceStreamDisconnectsAbnormal` | over 25 % ended `disconnected` or `superseded`, at least 5 per 5 minutes; `idle` is normal use (a phone whose screen turned off) | 10m |
| `VoiceStreamEngineDown` | the engine's scrape fails AND the gateway failed to reach an engine in the last 15 minutes | 2m |

The thresholds are not calibrated. Nothing had run in production when they
were set, so they come from the design targets and the worker measurements,
and each latency threshold sits on a bucket edge. Recalibrate them from the
first weeks of real use.

The `stt-stream` target is static, so it reads down whenever live dictation is
off. That is why the engine-down alert also needs the gateway's own failures:
a down target alone never alerts. No alert reaches a person: there is no
Alertmanager, by the owner's decision of 2026-09-15.

**Tests:** `monitoring/prometheus/tests/voice_stream.yml` has 27 promtool cases
covering every rule, both firing and not firing. The cases include:

- one reconnect replaying a minute of audio;
- a frontend deploy dropping six streams at once;
- a one-minute engine restart;
- one person turned away at capacity for half a minute.

28 deliberate breaks of the rules file each made `promtool test rules` fail
(2026-09-29).

**Dashboard:** the Developer API dashboard (`dgx-developer-api.json`) has a
row, **Real-time speech to text**, with nine panels:

- live streams and speaking load;
- first words and final p50/p95;
- event lag;
- engine real-time factor;
- outcomes;
- errors by reason;
- refusals from both sides;
- decode step p95;
- browser capture-to-render.

Triage for each alert, and why each is shaped the way it is, are in
[`../../monitoring/developer-api/README.md`](../../monitoring/developer-api/README.md#live-dictation-real-time-speech-to-text).

---

## Running it

The engine is deployed on its own, separately from the application. An
application deploy with no engine address changes nothing a person can see.

**Run everything below from the deploy checkout,
`/home/techsphere/Documents/project/personal-LLM-Chabot`.** `stt-stream.sh`
writes that checkout's `.env` and `.runtime/secrets.env`, and `./techsara up`
refuses to run against a stack another checkout started.

```bash
scripts/stt-stream.sh up             # fetch the four pinned models onto the worker (sha256-checked),
                                     # build the image there, start the engine, wait for /health ready;
                                     # record VOICE_LIVE_ENGINE_URLS in .env and the token in
                                     # .runtime/secrets.env
scripts/stt-stream.sh status         # container state and the engine's own /health
scripts/stt-stream.sh verify         # stream two real clips through it and print what it heard
scripts/stt-stream.sh url            # ws://192.168.9.68:30009, the address the orchestrator uses
scripts/stt-stream.sh logs           # follow the engine log (no audio, no text)
scripts/stt-stream.sh down           # stop and remove the engine (the models stay) and take its
                                     # address out of .env
scripts/stt-stream.sh up --no-env    # a candidate engine: .env and .runtime/secrets.env untouched;
                                     # the token comes from STT_TOKEN_FILE or the existing secrets.env
scripts/stt-stream.sh down --no-env  # stop a candidate without touching .env
```

The models are pinned by Hugging Face commit, and every file is checked by
sha256 before the engine may load it. They land in the cluster's model cache
on the worker (`~/Documents/project/Model/repos/`) and are mounted read-only.
Nothing is downloaded when the engine starts. Only the worker runs it: on a
single-node deployment `stt-stream.sh` refuses.

**1. The orchestrator picks up the address when it is recreated.** `compose.yaml`
passes `VOICE_LIVE_ENGINE_URLS` from `.env`, and the token arrives from
`.runtime/secrets.env` through `env_file`. Both are read once per process. Run
the same command the routine deploy runs, which leaves a serving main model
alone:

```bash
TECHSARA_PRESERVE_MAIN_MODEL=1 ./techsara up
```

Until that recreate, the create response's `live` stays `null`, and no browser
opens a socket.

**2. Prometheus picks up the scrape job** only on a restart. `prometheus.yml`
is a single-file bind mount, and `/-/reload` does not follow a replaced inode:

```bash
./scripts/monitoring.sh restart
```

**3. The host guard needs a root step, and the step is the owner's.** Until it
is done, `stt-stream.sh up` warns and prints these commands. First copy this
checkout's guard script to the worker. Use the worker's LAN address:
`scp` over the rail address 10.100.184.2 has been refused before.

```bash
scp scripts/host-guard.sh techsphere@192.168.9.68:.techsara-cluster/host-guard.sh
```

Then, on the worker:

```bash
sudo bash ~/.techsara-cluster/host-guard.sh install-boot --role worker   # the boot-time copy
sudo bash ~/.techsara-cluster/host-guard.sh apply --role worker          # load the new table now
bash ~/.techsara-cluster/host-guard.sh verify --role worker
```

`apply` refuses before calling nft if the new rules would cut off a consumer it
knows about. The head's address, 192.168.9.54, stays allowed on 30009, because
both the gateway and Prometheus reach the engine from it.

**4. Check it end to end.** The orchestrator's `GET /audio/health` now has a
`live` block: `configured`, the open streams, and the engines by number with
`healthy`, `active` and `stood_down_s`. Engine addresses are never listed. The
route answers only a super admin's session, and the frontend has no route to
it, so call the orchestrator directly on the head's port 8080. Then dictate a
sentence and watch the words appear.

**Replacing the token.** Delete the `VOICE_LIVE_ENGINE_TOKEN` line from
`.runtime/secrets.env`. Then run `scripts/stt-stream.sh up`, which mints a new
token and writes it on both nodes, and recreate the orchestrator as in step 1.
Between the two commands, streams fail with 4503 and browsers retry.

**Switching it off (rollback).** Remove the address and recreate the
orchestrator. Live dictation disappears, and the durable path does not notice:

```bash
scripts/stt-stream.sh down                     # stops the engine, sets VOICE_LIVE_ENGINE_URLS= in .env
TECHSARA_PRESERVE_MAIN_MODEL=1 ./techsara up   # the create response's live is null again
```

The live path can also be turned off without stopping the engine, in two
places, each of which takes effect when its container is next recreated:

- `VOICE_LIVE_ENABLED=false` for the orchestrator;
- `FRONTEND_WS_MAX_RELAYS=0` for the frontend.

Leave the `stt-stream` scrape job in place: its target reads down and does not
alert. Delete it only if live dictation is retired for good.

**Tests.** None of them needs a GPU, a model or a real engine:

```bash
# the engine: fake recognizers, no sherpa-onnx (CI runs it in orchestrator shard 1)
python -m pytest compose/stt-stream/tests -q
# the gateway: a fake engine behind the real handler; a private TEST_DATABASE_URL
cd orchestrator && python -m pytest tests/test_voice_live.py -q
# the relay, the worklet, the live model, the stream client, the panel
cd frontend && npx vitest run tests/server-ws-relay.test.ts tests/voice-live-*.test.ts tests/voice-live-*.test.tsx
```

The promtool commands are in
[`../../monitoring/developer-api/README.md`](../../monitoring/developer-api/README.md#validation).

---

## Measured

### Latency

**In the browser, end to end: Run 3, the build that ships.** Measured
2026-09-30 at about 05:03 IST on integration `a95aa829`, which carries the
engine at `670bc253` and the browser at `f625622d`. It ran on a candidate
stack on the worker:

- an engine with two 160 ms profiles on cores 15-19;
- the CPU orchestrator image and the standalone frontend with the relay;
- headless Chromium on the head, whose fake microphone played real speech, over
  an SSH tunnel.

Capture to render, as the page measured it, and what went into the draft:

| Run 3 | partial p50 / p90 | final p50 / p90 | n (partials, finals) | in the draft after Stop |
|---|---:|---:|---:|---|
| English (LibriSpeech, 4 utterances, 28.8 s, `auto`) | 269 / 348 ms | 486 / 488 ms | 60, 6 | 1.65 s: the full pass, WER 4.44 % |
| Hindi (FLEURS, 4 utterances, 67.2 s, `hi`) | 300 / 341 ms | 469 / 500 ms | 236, 10 | 0.43 s: the live transcript, WER 13.33 % |

The English run sent 747 PCM frames up and the Hindi run 1,710, with no
console errors. The harness (`browser_e2e.py`) reads the message box every
0.2 s, so an insert time can be up to that much late.

**Pre-review builds.** Two earlier runs on the same stack and audio used
builds from before the review fixes. Their numbers do not describe what ships:

| pre-review run, 2026-09-30 | English partial / final p50 | Hindi partial / final p50 | in the draft after Stop |
|---|---:|---:|---|
| Run 1, about 01:00 IST: `c96a5f45` (engine `204a5ca5`, browser `17041034`) | 245 / 884 ms | 279 / 884 ms | whisper's text in both: English 1.66 s, Hindi 5.5 s |
| Run 2, about 02:53 IST: engine `6435d990`, browser `15e9c174` | 376 / 527 ms | 321 / 495 ms | English 4.69 s (whisper's text), Hindi 26.56 s (the live transcript, after waiting for whisper) |

A final's latency counts from its `end_sample`. The engines of Runs 2 and 3
put an endpoint final's `end_sample` inside the closing silence, about 0.3 s
later than Run 1's engine did, so Run 1's finals are not comparable with the
later ones.

This path runs over the loopback interface and an SSH tunnel. In production
the Cloudflare tunnel and the LAN hop to the worker are added, and they have
not been measured yet.

**In the engine, in audio time** (`benchmarks/voice-live/screen.py`, 160 ms
chunks, endpoint 0.6 s, 2026-09-29):

| model | first partial after speech onset, p50 | final after speech end, p50 / p95 |
|---|---:|---:|
| Nemotron EN | 510 ms | 680 / 1,299 ms |
| Nemotron 3.5, `auto` | 510 ms | 830 / 1,340 ms |
| Nemotron 3.5, on Hindi | 720 ms | 710 / 1,223 ms |

The first-partial figure includes the time it takes to say the first word. A
freshly renewed stream shows its first Hindi partial about 230 ms later.
Through the engine's socket on the worker (`stream_bench.py`, 2026-09-30), the
Hindi first-partial p50 was 571 ms with renewal off and 801 ms with it on (p90
1,181 ms).

### Accuracy

The same utterances and the same normaliser for every system (2026-09-29).
The normaliser lower-cases, drops Unicode punctuation and symbols, keeps
combining marks, and folds nukta and chandrabindu. It uses no regular-expression
`\w`, which strips Devanagari vowel signs: the first Hindi numbers had that bug
and were redone. Word error rate, lower is better:

| set | whisper-large-v3 (the full pass) | Nemotron 3.5, 160 ms | Nemotron EN, 160 ms |
|---|---:|---:|---:|
| MUCS Hindi-English lectures, 34 min, document WER, script set aside | 40.2 % | **19.4 %** (auto) / 19.5 % (hi) | 69.9 % |
| MUCS, document WER, script-sensitive | 55.8 % | **41.7 %** | 80.0 % |
| FLEURS Hindi (40 utterances) | 41.9 % | **10.9 %** (hi) / 11.8 % (auto) | – |
| LibriSpeech test-clean (60) | 4.21 % | 6.23 % (auto) | **4.13 %** |
| FLEURS English (40) | **6.81 %** | 13.3 % (en) | 10.2 % |

These numbers need three caveats:

- **Script.** "Script set aside" forgives an English word written in Devanagari
  (डॉक्यूमेंट) or in Latin (document), but never a wrong or missing word.
  Whisper wrote 73 of the 364 MUCS segments in Urdu script and 5 in English (a
  translation). On FLEURS Hindi it wrote 9 of 40 in Urdu script.
- **Chunk size.** The English model at 560 ms scores 3.72 % on LibriSpeech.
  Wider chunks do not help the 3.5 model on Hinglish.
- **Gujarati.** Both streaming models read FLEURS Gujarati at 104 %.

End to end in the browser (the runs above), scored by the harness
(`browser_e2e.py`), whose normaliser does not fold nukta or chandrabindu:

- **Hindi:** the live transcript that Run 3 put in the draft scored 13.33 %.
  Whisper's text for the same recording, which the pre-review Run 1 put in,
  scored 35.56 %: it dropped a whole clause. With the normaliser above, the
  same two texts score 11.11 % and 34.07 %.
- **English:** the draft text, which is the full pass, scored 4.44 % in all
  three runs.

---

## Known limits

- **No Gujarati live.** Neither streaming model reads it. Gujarati speech still
  gets whisper's transcript after Stop.
- **The live transcript of a dictation is not stored on the server.** Only a
  meeting's is (`live.jsonl`). For a Hindi dictation, the inserted text lives
  in the draft, and the server keeps the recording and whisper's transcript. A
  reload loses the choice between the two. A reload while the full pass is
  still running after the live words went in makes the next page offer
  whisper's text as a recording interrupted when the page closed, with
  *Insert it*; it is never inserted unasked. The recording can always be
  transcribed again.
- **Safari and iOS are not verified on a device.** The code path is the same
  one: a worklet on one AudioContext, resumed when the browser starts it
  suspended. Chromium on the desktop was measured, and a headless Chromium is
  not a phone. Whether a mobile browser keeps capturing in a background tab is
  also unverified. A screen that turns off stops the audio, and the stream
  closes `idle` after about 60 s.
- **CPU capacity is finite, and it is the chat model's CPU too.** The engine
  takes 20 streams in the default `auto` language, 16 if everyone chose
  English, and at most 24 in any mix, not the 32 its `/health` reports
  ([Capacity](#capacity)). More people than that get the session's own preview
  and retry. Raising the caps is the owner's trade against chat speed.
- **The engine answers the office LAN until the host guard is reinstalled as
  root.** The stream needs the token. `/health` and `/metrics` do not, and they
  carry model labels and counts but no ids and no text.
- **The hop between the Sparks is not encrypted.** Audio and text between the
  orchestrator and the engine cross the 1 GbE management LAN as plain `ws://`,
  as whisper's clips cross it as plain HTTP. Encrypting that hop is an open
  owner decision. The browser leg is TLS through the tunnel.
- **The alert thresholds are uncalibrated,** and the production latency through
  the tunnel is unmeasured (above).
- **One orchestrator process.** The rate limit and `VOICE_LIVE_MAX_STREAMS` are
  held in process, as the dictation limits are. That covers today's
  deployment.
