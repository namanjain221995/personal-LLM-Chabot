# Video understanding — decisions made on the owner's behalf

Every ambiguity in the brief was settled here, with the reason, so the
choices can be reviewed and reversed. Dated 2026-09-09; measurements are from
this cluster (two DGX Spark GB10 nodes) on that day unless stated.

## 1. Built WITH vision — captions from the router, looks from the main model

The brief said "Qwen3.6-35B-A3B is text-only" and asked me to check before
promising. **It is not text-only.** Its checkpoint is
`Qwen3_5MoeForConditionalGeneration` with a vision tower, `image_token_id`
and `video_token_id`, the served engine carries the multimodal RoPE
override, and — the test that matters — sending it a 1280×720 slide as an
`image_url` returned the title, the Python function and the "Decision:"
line verbatim, with colours and positions, in 5.2 s. The router
(Qwen3-VL-8B-Instruct-FP8) reads the same slide in 13.7 s cold / ~2 s warm.
The OCR sidecar reads dense text but returns nothing to a "describe" prompt;
it needs its `document parsing` prompt.

So there are three readers, and each is used for what it is best at:

| job | model | why |
|---|---|---|
| on-screen TEXT | Unlimited-OCR (`vllm-ocr`) | verbatim, dense text, tables, code — "read the code on the slide" quotes from this |
| frame CAPTIONS (type + what is shown) | router Qwen3-VL-8B | ~2 s a frame at 896 px, 0.6 s effective at 4 in flight, 523 tokens/frame; running on the router keeps a hundred captions off the chat model everyone is waiting on |
| FUSION and Q&A | main Qwen3.6-35B-A3B | text over the evidence pack; at question time it also LOOKS at up to `VIDEO_ANSWER_FRAMES` (3) frames when the question is visual or names a timestamp |

The alternative — feeding keyframes straight into the fusion prompt as
images — was rejected: ~525 tokens a frame at 896 px means sixty frames is
30k tokens of vision prefill on the tensor-parallel chat engine, per video,
per re-run, and the result is not persisted or indexable. Captions are
text: they go into the evidence pack, into LanceDB, and into
`screen_text.txt`.

Native `video_url` input (the router accepts it — verified with an 8-frame
clip) was not used: a 49k-token window, one request at a time on its KV
pool, no timestamps back, and nothing measured. It is the upgrade path
(§ "next"), not the default.

## 2. The analysis is a detached, hash-keyed job — not part of the chat turn

Two facts about this codebase forced this. A new message in the same
conversation cancels the running generation, and so does the Stop button
(main.py); a `LiveGeneration` buffers every event in memory and is gone when
it finishes. A two-hour recording takes longer to transcribe than anyone
waits before asking a second question, and a page reload must not restart
twenty minutes of work.

So `video/pipeline.py` runs stages in its own task, started at UPLOAD time
(before the person has even pressed Send), keyed by the sha256 of the bytes
in a `video_analyses` row and a directory outside the 24-hour workspace
sweep. The chat turn subscribes and forwards progress. Uploading the same
file twice — by anyone — is one analysis; a failed one is re-queued by the
next upload and resumes at the first stage without an output file.

Per-stage state is one `stages` jsonb, not nine column triples: the stages
are a pipeline the code owns and adding one must not need a migration.

The cache has a version. `PIPELINE_VERSION` in `pipeline.py` is stamped on
the row when a run starts; a row finished under an older number is put back
to work in full the next time its file is attached, source kept, every
other stage file overwritten. This was not theoretical: the 12-second clip
was analysed under v1 with no words (the silence-gate call below), and a
content-addressed cache with no version would have served that answer to
every person who ever uploaded the clip, fix or no fix. Bumps are rare and
deliberate — each one costs a full analysis per re-attached video — so
they are listed with their reason next to the constant.

## 3. Progress rides the existing `step` event, not a new SSE event

`sse.py` is a closed registry, five test files assert its exact set, and
the frontend parser drops unknown names — a new `progress` event is a
cross-cutting change with a compatibility window. `step` already carries
`{id, title, status, detail}`, is merged by id, renders live in the
AgentTimeline with its own elapsed clock, and is folded into `meta.steps`
so a reloaded chat shows the timeline. Stage ids are fixed (probe=1 …
artifacts=9); the percentage and the stage's own clock ride in `detail`
("43% · 5:00 of 10:32 · 71s"). Updates are coalesced to one per stage
transition plus at most one every three seconds, so a two-hour job is a few
hundred frames in the generation buffer, not tens of thousands.

## 4. Evidence lives in its own LanceDB directory, keyed by analysis

The brief asked for LanceDB with video id, start, end and modality. The two
existing directories cannot take a new table: the sidecar pins one table
per directory, the Salesforce corpus renders every hit as a CRM citation,
and the web corpus is searched for every user on every turn with no
identity filter. `LANCEDB_VIDEO_DIR` (`/data/lancedb-video`, table
`video_chunks`) has an explicit Arrow schema and a `write_lock` like the
web index.

Rows carry `analysis_id`, not `conversation_id`: analyses are shared by
hash, and WHICH conversations may see one is the `video_attachments` table
in PostgreSQL. Every query takes an explicit list of analysis ids resolved
from that join, so two videos in one session never bleed and one person's
video never surfaces in another's chat. The one-writer invariant test was
extended to name exactly two LanceDB writers.

## 5. Concurrency policy: one job, one batch ASR slot, captions two-wide

Measured 2026-09-08: a saturated speech engine on EITHER node takes the
chat model from 71 tok/s to ~24, because the chat model is tensor-parallel
across both Sparks and runs at the speed of its slower rank. The policy:

* `VIDEO_MAX_CONCURRENT_JOBS=1` — one video at a time, queue in the DB.
* Inside a job, speech and screen run AT THE SAME TIME: audio → transcript
  on the whisper engines, frames → OCR → captions on the OCR and router
  engines, fusion after both. They never read each other's output, and
  they use different engines, so running them in series was pure waiting:
  the 10-minute meeting went from 4:02 to 1:55 the day this changed
  (README, "same video, after").
* ASR windows go through a SEPARATE batch pool, not dictation's, of
  `VIDEO_ASR_CONCURRENCY=2` — one clip per Spark. The first cut held ONE
  clip so dictation always had a free engine; the operator asked for both
  GPUs to be used, so now a person dictating during a video's transcription
  may wait for one clip (~15 s at the 90-second window). The router's
  least-active order is what spreads the two clips over the two nodes.
* OCR four-wide with a per-batch deadline; captions three-wide (the
  router's four slots are shared with the classification call every chat
  message makes, so one is left for it).
* Frames and audio are decoded by `ffmpeg` child processes with 8 threads
  (`VIDEO_FFMPEG_THREADS`, clamped to the core count), never in the event
  loop — the measured cause of an earlier TTFT regression here was CPU
  work on the loop.

That was not enough. With the slots above, chat still fell to 20 tok/s
while a video's frames were being OCR'd — each unit is short but there is
always a next one. So the job **paces itself**: before every GPU unit (an
ASR window, an OCR batch, a caption) it asks whether a real chat generation
is in flight — `main._live_generations`, minus the generation that is
itself waiting for this video — and if so waits, up to
`VIDEO_PACE_MAX_WAIT_S` (20 s), then proceeds regardless so a busy
workspace never starves the job entirely. A quiet box runs flat out. The
person waiting for an answer is never the one who pays for a background
video; the video takes longer instead, and the Activity panel shows why.

The unit the job cannot yield during is the ASR window, so its ceiling is
the size of the worst dip: at 240 s a window held the engine ~41 s and a
chatting user saw 31–37 tok/s for that long; `VIDEO_ASR_WINDOW_S` is 90 s
so the dip is nearer 15 s. OCR (~7–10 s a frame) and captions (~2 s) are
already short.

The measured cost of the policy is in the README (before / during / after).

## 6. Long media: VAD windows for speech, keyframes-only for frames, map-reduce for fusion

* Speech: `webrtcvad` marks speech; windows open on speech, never span a
  pause over 2 s, cap at 240 s (the engine refuses 600), and overlap by 3 s
  only where continuous speech had to be cut. `stitch` de-duplicates the
  seam by time and then by text. A window that opened on silence would be
  judged silent by the engine's first-30-s gate and lost — this is why
  windows open on speech.
* Frames: ffmpeg's scene detector plus a periodic floor, then perceptual
  hash dedupe (64-bit pHash in NumPy — `imagehash` would drag in SciPy), then
  a cap of one frame per 8 s clamped to [24, 400]. Above 20 minutes only
  keyframes are decoded, which is the difference between minutes and tens of
  minutes on a 1080p recording; a slide change is then noticed at the next
  keyframe rather than the exact frame.
* Fusion: one pass while the evidence pack is under 60k tokens; above it,
  parts of 32k are summarised into structured notes and the notes are
  fused. The window is a million tokens, but a prompt that size holds the
  engine at 1.49x concurrency; this is a ceiling on what one video may ask
  of it at once.

## 7. Artifacts go through `meta.report_files`, copied per user

`report_files.filename` is a global primary key bound to whoever first
advertised it, and the analysis is shared by hash — so the files are copied
into the reports directory under `<slug>-<hash8>-u<user>.<kind>.<ext>`.
Zero new frontend surface: the existing Files section renders them and the
existing download proxy serves them with ownership checks.

## 8. The attach turn renders the understanding without a model call

When the video arrives with no question, the answer is the fusion stage's
output rendered as Markdown — summary, chapters with `[m:ss]`, key points,
decisions, action items, entities, "not covered". A second model pass over
the same evidence would only add a chance to drift. A question on the
attach turn, or any later turn, goes through retrieval + the main model.

## 9. Follow-ups route on a cue OR a distance

A later text-only turn in a conversation with videos carries a compact
pinned block (summary + chapters + decisions, ≤ 6k chars) exactly as
documents do, AND goes to the video engine when the message reads like it
is about a recording (cue words: said, slide, screen, decided, at 12:34 …)
or when the nearest indexed chunk is within L2 1.45 — one embedding call.
An unrelated question ("write a haiku") stays on the normal engine chain
with the block for context. The threshold was first set at 1.05 and
re-measured on the live runs: a same-language question sits at 0.5–0.9, an
English question over a Hindi transcript at 1.29 (which 1.05 sent to plain
chat), and the haiku at 1.66. 1.45 is the middle of that gap; it is a
setting (`VIDEO_FOLLOWUP_DISTANCE`) because the next embedding model will
move it.

## 10. Feature flag folds the deployment switch into `/auth/me`

`Feature.VIDEO_ANALYSIS` (per member, requires attachments, default on) is
gated 403-before-404 like voice. Unlike voice, the deployment switch
`VIDEO_ANALYSIS_ENABLED` is folded into the features map the composer reads,
so a deployment without ffmpeg or a speech engine shows no video picker at
all instead of a picker that fails.

## 11. The OCR engine runs on the worker Spark

Measured 2026-09-09 before the change: the head held 108 of 121 GB with
13 GB available; the worker held 52 GB with 68 GB available and its GPU
idle outside transcription. On a unified-memory Spark the sidecars' KV
caches are sized as fractions of the whole 121 GB, so the OCR engine —
a 6.4 GB model — held 17.4 GB, the router 17.6 GB, the main model's rank
27 GB. And every sidecar sat on the head, which is also tensor-parallel
rank 0: all of a video's OCR and caption work landed on the rank the chat
model is already waiting on.

So `scripts/ocr.sh up` runs the same OCR engine, same image digest, on
the worker — its own compose project there, an explicit 3 GiB KV budget
(52k tokens, 6 pages of 8k at once), bound to the management address —
and records `OCR_REMOTE_BASE_URL` in `.env`. The orchestrator's compose
entry prefers that key over the generated head address, and the launcher
drops the head's `ocr` profile and retires its container while the key
is set, so a later `techsara up` does not start the engine again on the
node being emptied. `scripts/ocr.sh down` reverses all of it.

After: head 86 GB used, 34 GB available; worker 72 GB used, 49 GB
available; the worker's engine holds 11.6 GB where the head's held 17.4.
A silent 90-second screen recording, fresh bytes, took 28 s end to end
with its three OCR calls answered from the worker in ~5 s each. The
router stays on the head on purpose: every chat message's classification
goes through it, and a worker outage must not take chat routing with it.
Whisper stays on both nodes (the transcript uses both).

## Smaller calls

* ffmpeg via apt in all three orchestrator images rather than PyAV: same
  binary the speech engine already trusts, a child process is bounded and
  killable, and the wheel would have run decoding on the event loop's cores.
* `webrtcvad-wheels`, not `webrtcvad` (sdist, no compiler in the CPU image)
  and not `silero-vad` (drags torchaudio, which would replace the NGC torch).
* Only the head's upload sweep is avoided; the raw workspace copy is
  hard-linked into the analysis directory, so it costs no extra disk.
* Whisper's server gained `response_format=verbose_json` (segments) and a
  `no_speech_check` switch; the ASR client gained `transcribe_segments`.
  Dictation's path is byte-identical to before.
* The pipeline sends every window with `no_speech_check=false`. The engine's
  gate judges a clip on its first 30 s, and a 12-second LibriVox intro with
  a quiet lead-in scored 0.62 against the 0.6 threshold and came back with
  no words at all; a four-minute window opening on a pause would lose the
  whole four minutes. The voice-activity windows ARE the gate. The cost is
  that a track of pure noise the detector mistakes for speech can yield a
  hallucinated sentence — the smaller of the two failures.
* A media error reaching the chat names the file, never the storage path.
* `_MAX_PARTS` on the chunked upload rail went 64 → 128 so a 4 GB video has
  headroom (the client's 64 MiB parts made 4 GiB exactly 64 parts).
* Orphan analyses (no conversation refers to them) are reaped after
  72 hours by the pipeline's own maintenance loop.
