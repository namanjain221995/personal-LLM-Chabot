# Video understanding

A person attaches a video in the chat. The platform transcribes what was
said (with timestamps), reads what was on screen, describes what was shown,
fuses the three into an understanding — summary, chapters, key points,
decisions, action items, people and terms, and what the video does *not*
cover — answers questions about it with `[m:ss]` citations, and hands back
the transcript as downloadable files. Everything runs on this hardware;
nothing leaves the building.

Decisions and their reasons are in [DECISIONS.md](DECISIONS.md). This page
is what it is, how it runs, and what was measured.

---

## Built with vision

The brief assumed the main model was text-only. It is not: Qwen3.6-35B-A3B
reads a slide — title, code, decision line, colours, positions — and so does
the router model. So three readers share the work, each on what it does
best:

| reader | model | what it contributes |
|---|---|---|
| on-screen text | `baidu/Unlimited-OCR` (sidecar) | verbatim text of every distinct frame — what "read the code on the slide" quotes |
| frame captions | router `Qwen3-VL-8B-Instruct-FP8` | frame type (slide / code / terminal / webpage / document / person / diagram) and a factual description |
| fusion and Q&A | main `Qwen3.6-35B-A3B-NVFP4` | the understanding, and answers; it LOOKS at up to three frames when a question is visual or names a time |
| speech | `openai/whisper-large-v3` (both Sparks) | timestamped segments, 99 languages, code-switching kept |

The UI's Activity panel says which readers saw what; `meta.video.vision`
records `main+router`.

---

## What a person sees

1. Attach a video (picker or drag-and-drop; `.mp4 .m4v .mov .webm .mkv` or
   any `video/*`). The chip says **VIDEO**. The upload starts at once and so
   does the analysis — before Send is pressed.
2. Send, with or without a question. The reply shows named stages with a
   percentage and a clock while the analysis runs: *Probing the file ·
   Extracting audio · Transcribing 43% · Picking frames · Reading on-screen
   text · Describing frames · Understanding the video · Indexing evidence ·
   Writing transcripts*. A reload re-attaches to the same timeline.
3. With no question, the answer is the understanding itself. With a
   question, the answer cites timestamps — `[3:20]` — and says "the video
   doesn't cover X" when it doesn't.
4. **Files** on the message: `transcript.txt`, `.srt`, `.vtt`, `.json`,
   `screen_text.txt`, `screen_text.json`, `summary.md`.
5. Ask again forty minutes later, after the transcript has scrolled out of
   context: the evidence is retrieved from the index. Several videos in one
   chat are kept apart and named in the answer.

---

## Where it runs

```
 browser ──upload (chunked >90 MB)──► orchestrator ─┐
                                                    │ job (detached, 1 at a time)
   /uploads purpose=video                           ▼
   sha256 ─► video_analyses row ─► /data/video/<hash>/
                                     source.mp4 (hard link)
        probe ── ffprobe                probe.json
        audio ── ffmpeg → 16 kHz PCM   audio.wav
   transcript ── VAD windows → whisper (both Sparks, batch pool of 1)   transcript.json
       frames ── ffmpeg scene+floor → pHash dedupe → cap                frames/, frames.json
          ocr ── Unlimited-OCR, 2-wide, deadline per batch              ocr.json
       vision ── router captions, 2-wide                                screen.json
       fusion ── main model, direct ≤60k tokens else map-reduce         understanding.json
        index ── embed → LanceDB /data/lancedb-video (video_chunks)
    artifacts ── txt/srt/vtt/json/md                                    artifacts/
```

Every stage writes its file, then stamps itself done on the row. A crash,
a restart, or the same file uploaded next week resumes at the first stage
without a file. The row also carries the `PIPELINE_VERSION` it was produced
with: after a bump, the next attach of that file re-runs every stage rather
than serving a result the fix never reached. `video_attachments` says which
conversations may see which analysis; nothing in the index is reachable
without that join.

**Pacing.** Before every GPU-heavy unit — an ASR window, an OCR batch, a
caption — the job asks whether a real chat generation is in flight and, if
so, waits (up to `VIDEO_PACE_MAX_WAIT_S`, 20 s) before continuing. A quiet
workspace runs the job flat out; a busy one runs it in the gaps.

---

## Measured: the live run (2026-09-09)

A 10:32 screen recording — a Longbourn Ltd "weekly planning" slide deck
(pricing table, Q4 budget, a Python `allocate()` function, a terminal
session, a hiring plan held for three minutes, a picture-only slide, a black
stretch, a decisions slide) — with, as narration, a LibriVox reading of
*Pride and Prejudice* chapter 54. Speech and screen deliberately disagree,
so a fabricated fusion would be visible.

| stage | wall | what it produced |
|---|---:|---|
| probe | 0.2 s | 10:32 · 1280×720 h264 · audio |
| audio | 0.9 s | 10:32 of 16 kHz PCM |
| transcript | 95.6 s | 3 VAD windows → 93 segments, `en`; **WER 3.5 %** against the Gutenberg text (including the reader's spoken header) |
| frames | 13.7 s | 161 candidates → **10 distinct** → 10 kept (the 3-minute slide became one span) |
| ocr | 97.9 s | 10/10 frames legible, 27,436 chars (~10 s per text-dense frame) |
| vision | 18.1 s | 10/10 captioned by the router |
| fusion | 15.7 s | 5,487 tokens of evidence, one pass: meeting · 5 chapters · 2 decisions |
| index | 0.2 s | 36 evidence chunks |
| artifacts | 0.0 s | 7 files |
| **total** | **4:02** | first answer at 4:02 (the attach turn waits for the analysis) |

Peak orchestrator RSS during the run: **883 MiB**.

**The generated understanding** (verbatim, trimmed):

> The video presents a juxtaposition of a business meeting and a literary
> reading. The visual evidence consists of slides from a 'Weekly Planning
> Meeting' for an entity named 'Longbourn Ltd', covering topics such as
> pricing, budget allocation, hiring, and decisions. The speech transcript,
> however, is a continuous reading of Chapter Fifty-Four from Jane Austen's
> 'Pride and Prejudice' …
>
> **Chapters** [0:00] Introduction and Pricing Proposal · [1:24] Budget
> Allocation and Narrative Continuation · [3:20] Technical Implementation and
> Social Dynamics · [4:35] Hiring Plan and Dinner Party · [8:23] Post-Dinner
> Reflections and Decisions
>
> **Decisions** Proposal to raise the Team tier price to $59 from November
> (visible on screen, not explicitly confirmed as final in speech) …
>
> **Not covered** The speech does not discuss the business topics shown on
> the slides … No specific owners for the action items are named in the
> speech, only the roles.

That last paragraph is the point: it did not invent a meeting decision.

**Follow-up questions** (same conversation, Think effort on the unpaced build):

| question | answer | TTFT |
|---|---|---:|
| What did they decide about the Team tier price, and who owns the follow-up? | "the video does not show an actual decision being made … a slide with a red banner **proposing** … [1:24–2:08] … **no specific owners are named**" | 22 s |
| Read the code on the slide about the billing service. | the `allocate()` function, verbatim, `[3:20]` — three frames were shown to the model | 24 s |
| When do they discuss the hiring plan, and what roles are open? | "[4:35] … Senior backend engineer (Pune), Data engineer (remote) … presented visually and not verbally discussed" | 15 s |
| What was said about Q1 revenue? | "**The video doesn't cover Q1 revenue.**" | 17 s |
| What is on screen at 4:00? | the billing-service slide, read from the frame — see "does badly" below | 72 s |

After that run, question turns were switched to answer without thinking
below Max (the TTFT column is what thinking cost), and a question that
names a time now decodes the frame at exactly that time.

---

## Chat speed before / during / after

Single-stream decode of the main model, three runs, median:

| state | tok/s |
|---|---:|
| before, idle | **75.1** |
| during transcription (unpaced build) | 35.1 |
| during frame selection | 42.1 |
| during OCR | 20.2 – 22.3 |
| during fusion / index | 65.8 |
| after, idle | **75.4** |

That "during OCR" number is why the job paces itself now: see
[DECISIONS.md §5](DECISIONS.md) and the paced figures below. The short
version: with pacing and 90-second ASR windows, a person chatting
**without a pause** through a whole analysis sees a median 50.6 tok/s
(p25 39) against 75 idle, and sub-second first tokens; one who chats
normally, with gaps, sees 61.

### With pacing (the shipped build)

Measured through `POST /chat` as a second user, from `usage_events`
(`output_tokens` over `duration − ttft`), while a first user's video was
being analysed:

| state | decode, as the chatting user sees it |
|---|---:|
| idle, through `/chat` | **75.3 tok/s** |
| during a paced job — typical sample (transcription, OCR, captions yielding to chat) | **62–67 tok/s** |
| during a paced job — worst samples (the job's own fusion call on the main model; a unit past the 20 s pacing cap) | 32–40 tok/s |
| unpaced build, same stages, for comparison | 20–35 tok/s |

The price is paid by the job instead: a 12-second clip that analyses in
~20 s on a quiet box took 96 s while two other users chatted continuously,
and the 2-minute Hindi tutorial's transcription stretched from ~25 s to
107 s. That is the intended trade — the person waiting for an answer is
never the one who pays for a background video.

---

## The awkward inputs

Every case below went through `scripts/video_edge_cases.sh` — the real
upload rail, the real chat turn, the real follow-ups — on 2026-09-09. The
first six ran while two other accounts were chatting continuously, so
their stage times include the job yielding to chat (the whole point).

| input | what happened | follow-up |
|---|---|---|
| **12 s clip**, speech + one slide | The first pass came back "no speech detected": Whisper's own silence gate judges the first 30 s and scored the clip's quiet lead-in at 0.62 against a 0.6 threshold. The pipeline now sends its voice-activity windows with that gate off (`PIPELINE_VERSION` 2) and the cached analysis re-ran on the next attach: transcript in 1.6 s, 27 s end to end on a quiet box. | "What is said in this clip?" → the LibriVox notice, verbatim, `[0:00–0:12]`, 3 evidence chunks |
| **90 s with no audio track** | probe says *NO audio*; audio and transcript stages are *skipped* with a reason; 23 candidate frames → 3 distinct; summary reads the pricing table and the red banner; 2:07 end to end under chat load | "What did the speaker say?" → "The video does not contain any spoken audio" |
| **2 min talking head** (narration over one static diagram) | 30 candidates → **1** distinct frame; classed *lecture*; three chapters that follow the narration; 2:13 under load | "What is on screen?" → the static diagram, and that its repeated text is not identified |
| **corrupt file** (300 KB of noise with an .mp4 name) | probe fails in 0.2 s: *"probing the file failed: source.mp4: Invalid data found when processing input"*; the chat gets one sentence and no files; the row is `failed` and a re-upload retries | — |
| **VP8/WebM screencast, 7 s, no audio** (exotic container) | decoded like any other; 2 distinct frames; the on-screen architecture note read verbatim | "What happens in this recording?" → both frames, timestamped |
| **Hindi + English tutorial, 2 min** (Thunderbird walkthrough in Hinglish over the meeting slides) | language `hi`, 18 segments with the English terms kept; summary in English (see "does badly" 5); the same file attached in a **second conversation** by another account answered in **0.1 s** — `reused: true`, every stage from the cache | "यह tutorial किस बारे में है?" → answered in Hindi with citations; "What is the tutorial about?" → answered, and the note that the slides are unrelated to the audio |
| **33 min of audio in a 42-minute file** (four LibriVox chapters over the looping deck) | 9 VAD windows, 297 segments, transcription 453 s (373 s in the engine, 80 s yielded to chat); keyframes-only above 20 min: 264 candidates → **40** distinct; OCR 282 s; fusion in one pass (18 s); 127 evidence chunks; **13:24 end to end** | "What is on screen around 20:00?" → the frame at exactly 20:00 was decoded and read (the black stretch with the confidential footer) |

Two things the long run showed that are worth knowing. The chapter list was
right about *content* and *order* but the model numbered two chapters
wrongly (it called the end of chapter 30 "Chapter 27" and chapter 27
"Chapter 28"; the reader announces the real numbers in the transcript). And
"which chapters are read" was answered from the pinned summary — inheriting
the wrong numbers — rather than from the transcript where the announcements
are. Both are on the "does badly" list.

### Three people at once

Account A attached the 10-minute meeting; five seconds later account B
attached the 2-minute lecture; account C chatted without a pause until both
had their answers (20 batches of 3 turns). One job runs at a time, so B
queued behind A. Decode speed is what C's turns recorded in `usage_events`
(45 turns over 300 tokens), not what the job reported:

| | |
|---|---|
| A: meeting analysed, answered | **9:05** (4:02 on a quiet box) — transcript 180 s, OCR 213 s, captions 117 s, fusion 20 s |
| B: lecture analysed, answered | **11:10** — 9:01 of it queued, 2:09 of its own work |
| C: decode, median | **61.0 tok/s** (idle: 75.3) |
| C: decode, quartiles | p25 36.7 · p75 74.6 · min 26.2 · max 80.7 |
| C: time to first token, mean | 1.8 s |
| orchestrator memory, peak | **1.59 GiB** of 121.7 (two analyses, three users) |

Both follow-ups were right: A's "what was decided about the Team tier
price?" got the proposal with its timestamps and *"not possible to confirm
if this proposal was officially decided"*; B's "who is invited to Rosings?"
got the Collins party with the quoted line.

The bottom quartile is the ASR window: once a clip is in the engine the job
cannot yield until it returns, and at the 240-second ceiling used in this
run a clip held the engine ~41 s. The ceiling shipped is 90 s
(`VIDEO_ASR_WINDOW_S`), which makes the longest un-yieldable unit ~15 s;
the section below has that measured.

### 90-second windows, measured

The same 10-minute meeting again (fresh bytes, so a full analysis) with
`VIDEO_ASR_WINDOW_S=90` live, while account C chatted **without any gap**
— 56 turns of ~300 words in 11 minutes, so the job was yielding to chat
before every single unit and then running that unit under chat anyway.
Decode as C's turns recorded it in `usage_events`:

| phase of the job | C's decode, median | p25 – p75 | n |
|---|---:|---:|---:|
| transcription, 240 s windows (the run above) | 36.7 tok/s | 35.7 – 46.0 | 9 |
| transcription, **90 s windows** | **48.9 tok/s** | 38.8 – 63.4 | 25 |
| OCR (10 frames) | 61.3 tok/s | 36.4 – 67.1 | 15 |
| whole job, start to answer | **50.6 tok/s** | 39.2 – 64.2 | 56 |
| idle, for reference | 75.3 tok/s | | |

C's time to first token averaged 0.74 s throughout. The job paid for it:
10:53 to the first answer instead of 4:02 on a quiet box (transcription
305 s instead of 96, eight clips each waiting up to 20 s for a gap that
never came). That is the trade the policy makes on purpose, and the
Activity panel shows the person who attached the video why it is slow.

What is left of the dip is the unit itself: a 90-second clip holds a
Spark's speech engine ~15 s, an OCR frame ~7–10 s, and the chat model —
tensor-parallel across both Sparks — runs at the slower rank's speed for
exactly that long. Going lower than 90 s buys little (Whisper's own
receptive field is 30 s) and costs seams; the next real gain is a GPU the
chat model does not share, which this hardware does not have.

---

## Running and operating it

```bash
# deployment switch (folded into /auth/me, so the picker hides when off)
VIDEO_ANALYSIS_ENABLED=true          # .env; every other knob is in .env.example

# the same path the browser takes, from a script
VIDEO_SMOKE_EMAIL=… VIDEO_SMOKE_PASSWORD=… \
scripts/video_smoke.py --video meeting.mp4 --ask "what was decided?"
scripts/video_edge_cases.sh <dir-with-the-test-videos>

# status of a running analysis
GET /video/{conversation_id}/{upload_id}/status
```

* Per-member switch: **Video understanding** on the admin Access page
  (requires *Photos, files and datasets*). Server-side: 403 for the member,
  404 for a deployment with it off — in that order.
* Metrics: `video_stage_seconds{stage}`, `video_stage_total{stage,result}`,
  `video_jobs_total{result}`, `video_pace_seconds`,
  `asr_batch_requests_total`, `asr_batch_request_duration_seconds`.
* Storage: `/data/video/<hash>/` (data-stores exporter label `video`),
  `/data/lancedb-video` (`lancedb_video`; in the backup script's loop).
* Housekeeping: analyses no conversation refers to are deleted after
  `VIDEO_ORPHAN_TTL_HOURS` (72) by the pipeline's own maintenance loop.
* Sharing: route `video` and meta key `video` are PRIVATE — an answer
  grounded on a video cannot be published on a public link.

---

## What it does badly today

1. **OCR is slow on text-dense frames** — ~10 s each on this sidecar. Ten
   frames is fine; four hundred is not, so above `VIDEO_OCR_MAX_FRAMES`
   (120) only the longest-held frames are read and the rest keep their
   captions. A long screencast of scrolling code loses some verbatim text.
2. **The first answer waits for the whole analysis** — four minutes for a
   ten-minute video, proportionally more for a long one. The stages stream,
   but nothing is answered early. A partial answer from the transcript
   while frames are still being read is the obvious next step.
3. **Frame selection is bounded by the periodic floor** for slides with
   similar backgrounds (a dark code slide into a dark terminal did not trip
   the scene detector; the floor caught it 4 s later). A named-time question
   now decodes the exact frame, but chapter boundaries can still be a few
   seconds late.
4. **No speaker labels.** Whisper does not diarise; "she said" resolves to
   the transcript, not to who said it.
5. **Language detection is per video** (the dominant language); a bilingual
   meeting's segments carry their own codes. The summary's language is the
   model's call, not the detected code: the Hindi-with-English tutorial was
   transcribed as `hi` and summarised in English, while a Hindi question
   about it was answered in Hindi.
6. **The evidence pack is text.** The fusion model does not see the frames;
   it reads their captions and OCR. Frames reach the main model only at
   question time.
7. **Chapter titles are the model's inference**, and it can misnumber them:
   on the 33-minute run two of five chapter numbers were wrong while the
   boundaries and descriptions were right. A follow-up that is answered
   from the pinned summary inherits such a mistake; one that retrieves from
   the transcript does not. Titles should be checked against announcements
   in the speech before they are trusted.

---

## Next three upgrades, by value against effort

1. **Answer early from the transcript** (medium effort, high value): the
   attach turn streams a transcript-based summary as soon as `transcript`
   finishes and revises it when `fusion` lands. Cuts time-to-first-answer
   from the whole pipeline to roughly a third of it.
2. **OCR only where it pays** (low effort, high value on long videos): run
   captions BEFORE OCR and OCR only frames the router classed as slide /
   code / terminal / document. On a talking-head recording that is zero OCR
   calls; on a screencast it is all of them.
3. **Native video input for short clips** (medium effort, medium value): the
   router accepts `video_url` today (verified). For clips under ~2 minutes,
   one call that sees motion beats sampled frames for "what happened" —
   behind a flag, alongside the frame path, because it returns no
   timestamps and holds the router's KV pool for the whole call.
