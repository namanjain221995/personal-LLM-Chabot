# Meeting transcripts

**Live transcript**, in the composer's "+" menu, transcribes a meeting or a
talk as it happens. It captures two channels:

- **Others:** the meeting's own audio.
- **You:** the person's microphone.

Both are streamed through the live dictation pipe described in
[`REALTIME.md`](REALTIME.md) and recorded as one V42 recording
([`../VOICE.md`](../VOICE.md)).

Added 2026-09-30. Not all of it exists yet, and each section below says which
state it is in:

| part | state on 2026-09-30 |
|---|---|
| the server side: V43 meeting recordings, the consent flag, two live streams per meeting, the engine's meeting mode, the saved live transcript, echo marking | built and tested (branch `feat/rtv-meeting-backend`) |
| the **Live transcript** panel in the composer | being built; this page describes its design |
| a desktop companion for audio the browser cannot capture | designed, not built |
| speaker labels after the meeting (Stage B) | designed, not built |
| a summary, decisions, action items and search, through the video rail | designed, not built |

The browser facts below come from primary sources read on 2026-09-29: MDN
browser-compat-data 8.1.3 (published 2026-09-24), the Chromium `main` source,
and each vendor's documentation. Where a fact could not be double-sourced, it
says so.

---

## What a person sees

*Design. The panel is being built.*

"Live transcript" appears in the "+" menu next to *Add photos & files*, *Web
search* and *Deep research*, with the hint *Transcribe a meeting or a talk as
it happens*. It is shown only when both of these are true:

- the account may use it (`meeting_transcription`);
- the server's live path is configured.

A control that cannot work is not shown. The microphone button keeps plain
dictation.

1. **Setup.** The person first chooses a source:
   - *A meeting in another tab (Google Meet, Zoom or Teams in the browser)*;
   - *A meeting app or my whole screen (Zoom/Teams desktop; Windows, ChromeOS,
     macOS 14.2+)*;
   - *Only my microphone*.

   An *Include my microphone* toggle is on by default. A headphones tip
   follows, then the consent checkbox, *Everyone here knows this is being
   transcribed.* **Start** stays disabled until that box is ticked, and no
   capture API is called before the click. A browser that cannot capture a
   meeting's audio is offered only *Only my microphone*, with one sentence
   saying why.
2. **Live.** One timeline of caption bubbles, each labelled **You** or
   **Others** and timed m:ss. Committed words are shown in ink, and the part
   still being heard in muted grey. The newest bubble is at the bottom, and the
   view scrolls with it unless the person scrolled up. A red *Transcribing* dot,
   a timer, **Stop** and **Discard**. Stopping the screen share ends the
   transcript gracefully.
3. **Done.** A *Saved to Recordings* link. **Add to chat** attaches the
   transcript to the composer as a text file, *Live transcript \<date
   time\>.txt*, with lines `[mm:ss] You: …` and `[mm:ss] Others: …`. It goes
   through the composer's existing file-attachment path, so asking "summarise
   this" or "what are the action items?" uses the document pipeline that
   already exists. **Copy** copies the text.

---

## Two channels, because of headphones

People wear headphones in meetings, and that decides the design (the owner,
2026-09-29). With headphones, the microphone hears only the local person. The
other participants exist only in the computer's audio output, so a meeting
transcript has to capture two sources at once:

- **You:** the microphone (`getUserMedia`, echo cancellation on).
- **Others:** the meeting's own audio. That is the browser tab, through
  `getDisplayMedia`, or the system output for a desktop app.

Each source is its own live stream on the same recording, with its own sample
numbering, utterance numbering, ring buffer and resume, and the two are shown
as one timeline. With headphones the separation is perfect: no echo, no
duplicated words, and a speaker label for free (the approach Granola takes).

The stored recording is the **mix** of both sources. The browser sums them in
its one AudioContext into a `MediaStreamDestination`, which feeds the
MediaRecorder and its 5 s parts. So the stored file and whisper's full pass
cover everything said, as for a dictation.

Two practical details:

- **Bluetooth headsets drop to their low-quality call profile** when a
  microphone opens.
- **The meeting app and this page can share the microphone.** Browsers and
  operating systems allow shared capture, so neither takes it exclusively.

---

## What a browser can capture

| scenario | how | works on | does not work on |
|---|---|---|---|
| Google Meet, Zoom web, Teams web, in a browser tab | `getDisplayMedia`; the person picks the meeting **tab** and keeps *Share tab audio* ticked (it is pre-ticked) | Chrome and Edge on the desktop: Windows, macOS, Linux, ChromeOS | Firefox, Safari, every mobile browser |
| Zoom or Teams desktop app, or any other desktop audio | `getDisplayMedia`; the person picks the **entire screen** and ticks *Share system audio* (it starts unticked) | Chrome and Edge on Windows and ChromeOS; on macOS only Chrome 141 or later on macOS 14.2 or later | Linux (off by default in Chromium); macOS before 14.2; Firefox, Safari, mobile |
| a single desktop app without sharing the screen | not possible from a browser today; the [desktop companion](#desktop-companion) | — | — |
| Firefox, Safari, any phone or tablet | the microphone only, with a message saying why | every browser | — |

**Tab audio.** Chromium captures a tab's audio on every desktop OS. For Meet,
Zoom web and Teams web the tab plays only the other participants, so it is
exactly the **Others** channel.

**System audio.** In Chromium `main`, system-audio capture is available on
Windows and ChromeOS. On macOS it needs Core Audio tap support, which arrived
in macOS 14.2. On Linux it sits behind `kPulseaudioLoopbackForScreenShare`,
which is disabled by default. Chrome 141 (stable 2025-09-30) is the first
version with system audio on macOS. That version comes from a third-party
report (addpipe), which the Chromium source agrees with; Chrome's release notes
do not announce it. System audio carries **everything** the machine plays, not
only the meeting.

**Per-app audio.** In Chromium `main`, `windowAudio: 'window'` becomes
per-application loopback on Windows 11 and macOS 14.2 and later. MDN still says
stable Chrome accepts only `exclude` and `system`. It is untested here on
Chrome 154 (stable 2026-09-22) and is not used.

**Firefox, Safari and phones** return a video-only stream and raise no error.
Firefox bug 1541425 ("Implement audio capture for getDisplayMedia") is still
NEW, P3 and unassigned. No mobile browser supports `getDisplayMedia` at all.
The panel detects an empty `getAudioTracks()` and says so.

**Rules every call must follow:**

- **A user gesture.** The spec requires transient activation. Firefox and
  Safari enforce it, and Chrome lists its enforcement as "in development".
- **A video track.** `video: false` throws a `TypeError`. The panel asks for
  the smallest one: `frameRate: {max: 1}`, `width: {max: 320}`.
- **No `min` or `exact` constraints.** Either throws a `TypeError`.
- **A fresh picker every time.** Permission is not persistent.
- **Never `suppressLocalAudioPlayback: true`.** It mutes the meeting for the
  person.
- **An empty `getAudioTracks()`** means the person unticked the audio box. The
  panel says *No audio was shared. Choose the meeting tab again and keep
  'Share tab audio' ticked.* and offers Retry.
- **The track's `ended` event** means the person pressed *Stop sharing*. The
  meeting then finishes with `ended_by: "share_stopped"`.

The call the design uses:

```js
navigator.mediaDevices.getDisplayMedia({
  video: { displaySurface: 'browser', frameRate: { max: 1 }, width: { max: 320 } },
  audio: { suppressLocalAudioPlayback: false },
  systemAudio: 'include',
  selfBrowserSurface: 'exclude',
  surfaceSwitching: 'include',
  preferCurrentTab: false,
});
```

The microphone is a separate `getUserMedia` call with the composer's own
constraints (echo cancellation, noise suppression, automatic gain, one
channel).

**Linux and macOS before 14.2** get tab audio only. Chromium's PulseAudio
backend hides monitor (loopback) sources from `getUserMedia`, so a web page
cannot reach system audio there by any other door.

---

## Speakers instead of headphones: echo

On speakers the microphone also hears the other participants, so **You** would
repeat what **Others** said.

- **A meeting in a browser tab:** Chrome's browser-wide echo canceller removes
  Chrome's own playout from the microphone. It is enabled by default on
  Windows, macOS and Linux, so the tab's voices are cancelled out of **You**.
- **A desktop app on speakers:** Chrome's canceller does not hear another
  process's playout unless the microphone asks for `echoCancellation: 'all'`.
  That mode uses the system loopback as its reference and needs Chrome 141 or
  later on Windows 11 or macOS 14.2 or later. Only the Chromium flags were read;
  it is **untested** against Zoom or Teams desktop. So the panel shows the
  headphones tip: *Wearing headphones? Perfect — the meeting's voices and yours
  are captured separately.*

**The server removes what is left** (built). A **You** final is marked as echo
when three things hold:

- it has three words or more;
- at least 80 % of its words, counted over the longer of the two texts,
  appear in an **Others** final or in the current **Others** partial;
- the two overlap in time, give or take 1 s.

An echo final is sent with `"echo": true`. The panel hides it and drops its
partial, and it is not saved. A shorter **You** final is always kept: a "yes"
said together with the others is the person's own reply. The stored recording
is one mixed track, so a reply wrongly dropped as echo could not be recovered
from it. A missed short echo costs a duplicated word instead.

---

## The server side

*Built on 2026-09-29 and 2026-09-30, on branch `feat/rtv-meeting-backend`.*

**Migration V43.** `voice_sessions` changes in four ways:

- It gains `kind` (`'dictation'` or `'meeting'`, NOT NULL, default
  `'dictation'`, with a CHECK). The constant default makes the ADD COLUMN
  metadata-only, so every existing row reads as a dictation.
- The unique index that allowed one live recording per person becomes one per
  person **per kind**. A meeting and a dictation can therefore record at the
  same time; a second meeting is still refused with 409 `session_active`.
- `ended_by` gains `'share_stopped'`.
- It gains `consented_at` (below).

**Creating a meeting** is `POST /api/audio/sessions` with
`{"kind": "meeting", "consent": true, …}`, and the server checks, in order:

1. **Voice input:** refused 403 `voice_off`.
2. **The meeting feature:** refused 403 `meeting_off`.
   `Feature.MEETING_TRANSCRIPTION`, labelled *Live transcript*, requires
   `VOICE_INPUT`. It is **on by default**, the owner's decision of 2026-09-29
   over the draft's "off for members". It is managed per member on the admin
   Access page.
3. **The deployment's switches.**
4. **The body:** without `"consent": true`, the answer is 400
   `consent_required`, *Confirm that everyone in the meeting knows it is being
   transcribed.* The row keeps the time consent was given.

Parts, finish and retranscribe of a meeting answer `meeting_off` too if the
feature has been turned off since. `GET /api/auth/me` reports
`meeting_transcription: true` only when the account may use it **and** the live
path is configured, which is exactly when the "+" menu shows the item.

**Two live streams per meeting.** Both sockets use the same URL and session id
as a dictation ([protocol](REALTIME.md#the-protocols)), and the `start`
message's `source` tells them apart:

- **Sources.** `mic` (You) and `tab` (Others, which is tab or system audio). A
  dictation accepts `mic` only (4400).
- **Supersede.** There is one stream per recording and source. A reconnect
  supersedes only the older stream of its own source (4409), and the other
  source's stream is untouched.
- **Engine mode.** The engine is asked for mode `meeting`. People pause
  mid-thought when they talk to each other, so an utterance ends after 0.9 s of
  silence instead of 0.6 s, and long speech is forced out at 25 s instead of
  30 s. An engine deployed before it knew the meeting mode refuses such a
  stream: the browser gets a retryable 4503, and the engine is **not** stood
  down, so dictation keeps working on it.
- **Revocation.** Turning the feature off ends a meeting's streams with 4403
  `meeting_off`, at the handshake or at the next minute's re-check.
- **Routing.** The `tab` source is never routed by the person's language
  history. The other people's language is not the person's.
- **Starting sockets.** Until its `start` names a source, a socket holds a
  stream slot. So a recording may have at most one such socket per source it
  may stream (4429). Otherwise one member could park sockets that never start.

**Time.** Each source sends its own `clock_offset_ms`: the capture time of that
source's sample 0, minus the time `MediaRecorder.start()` ran. A live utterance
sits at `sample / 16 + clock_offset_ms` on the stored recording's clock, so
the **You** and **Others** lines interleave correctly and line up with the
recording's playback.

**The saved live transcript.** Every final of a meeting stream is appended to
`live.jsonl` in the session's directory, as exactly `{source, u, start_ms,
end_ms, text}`. The write is careful in four ways:

- **Safe to the disk:** the file is mode 0600, and the write happens in a
  thread, never on the event loop.
- **Never resurrects a discard:** the write never recreates a directory that
  was removed, so a late final cannot bring back a discarded meeting.
- **A replay replaces:** each connection's first saved final follows a resume
  mark, so a reconnect's replay replaces what it hears again instead of
  repeating it.
- **Echo stays out:** echo finals are never saved.

`GET /api/audio/sessions/{id}` returns them as `live_segments`: sorted by
start, one per `(source, u)`, the last saved winning. They are returned once
the meeting is done or failed, or while it records when asked with `?live=1`.
They are left out of every other answer, so a two-hour meeting's transcript,
about 1 MB, is not re-read at each of the recorder's long-polls. Only the owner
can read them, like the segments. The super admin's audited transcript read
includes them. Discarding the meeting, or the retention sweep, deletes them
with the directory.

**Dictation is unchanged.** Its live words are still not saved.

---

## Consent and the law

This is not legal advice. The facts below come from the research of
2026-09-29, with sources, and counsel should confirm them before this is
offered outside the company. Recording a meeting records other people, so the
product assumes the strictest rule: **everyone present must know**.

**India (DPDP).** Four rules matter here:

- **Consent.** The Digital Personal Data Protection Act 2023 requires consent
  to be "free, specific, informed, unconditional and unambiguous with a clear
  affirmative action" (s.6(1)).
- **Language of the notice.** The notice must be available in English or any
  language of the Eighth Schedule, which includes Hindi and Gujarati (s.5(3)).
- **When the rules bite.** The DPDP Rules were notified 2025-11-14. Rules 3 and
  5 to 16 (notice, security, breach, retention) apply from about 2027-05-14.
  Rule 6 asks for access control, logs kept for a year, and encryption or
  masking. Rule 7 asks for a detailed breach report to the Board within 72
  hours.
- **Retention.** Rule 8(3) keeps data and logs for at least a year. Counsel
  should confirm how that sits with a person's request to delete a recording,
  and which sections of the Act are already in force.

**United States.** About eleven or twelve states require all parties' consent
to record: California, Connecticut, Florida, Illinois, Maryland,
Massachusetts, Montana, New Hampshire, Oregon, Pennsylvania and Washington,
with nuances in Hawaii and Michigan. That list is from a secondary source.
California Penal Code §632 applies, and §637.2 allows $5,000 per violation.

The law on AI note-takers is unsettled, and the notable case is at the
pleading stage. In *In re Otter.AI Privacy Litigation* (N.D. Cal., order of
2026-08-13), the Wiretap Act, CIPA §631 and BIPA claims were allowed to
proceed, because a vendor that retains or trains on meeting audio may count as
a third-party eavesdropper. That is not a finding of liability. Illinois BIPA
requires a written release before voiceprints are collected.

**European Union.** EDPB Guidelines 02/2021 treat identifying a person by voice
as Article 9 biometric processing, which needs explicit consent and a
non-biometric alternative. The AI Act has prohibited inferring emotions in
workplaces and education since 2025-02-02 (Art. 5(1)(f)).

**What the product enforces:**

- **Consent is enforced on the server, not only in the page.** A meeting
  recording is created only when the request carries `"consent": true`, and
  the time is kept (`consented_at`). The panel's checkbox sets it, and Start is
  disabled until it is ticked.
- **Capture only after the click.** No capture API runs before the person
  presses Start. A red *Transcribing* dot and a timer stay on screen, with
  Stop and Discard. The browser's own capture indicators are never
  suppressed.
- **The audio stays here.** It goes to this platform's own servers and is
  stored in the person's account. No third party receives it, and nothing
  here trains a model on it.
- **No voiceprints.** Speakers are channels (**You** and **Others**). Stage B
  adds only anonymous, per-meeting labels (*Speaker 1*, *Speaker 2*), with no
  enrolment.
- **No emotion or sentiment inference.**

**Not done yet, and open for the owner:**

- the notice in Hindi and Gujarati;
- an optional announcement in the meeting's chat, or a spoken notice.
  Fireflies' botless mode turns on three notices by default: a spoken one about
  10 s in, a chat message (on a Mac), and a camera watermark;
- a retention window: `VOICE_RETENTION_DAYS` defaults to 0, which keeps
  recordings forever;
- encryption at rest, and on the head-to-worker hop.

---

## Official platform APIs, and why local capture instead

Each platform has an official way to get meeting audio. None of them suits
this deployment today:

- **Zoom RTMS.** Open to all developers since 2025-06-25, and bought as
  credits, self-service since May 2026. The host must approve, and every
  participant sees a disclosure. Audio arrives as one mixed 16 kHz mono L16
  stream (the WebSocket default) or as 48 kHz Opus per participant (the SDK
  default). The SDK is built only for linux-x64 and darwin-arm64, so not for
  the aarch64 Sparks; the WebSocket protocol would have to be implemented here,
  or hosted on x86. It remains an option for an enterprise customer who wants
  host-approved audio. It should use the mixed stream: per-participant streams
  multiply the speech work.
- **Zoom Meeting SDK bots.** Since 2026-03-02, a bot joining another account's
  meeting needs an OBF token, and it ends when the authorising user leaves.
  Zoom itself points to RTMS for continuous recording.
- **Google Meet Media API.** Still a Developer Preview (2026-09-03). The Cloud
  project, the OAuth principal and every participant must be enrolled, and it
  cannot join encrypted or watermarked meetings. Not usable.
- **Microsoft Teams media bots.** C#/.NET only, on Windows Server in Azure,
  still in developer preview. Since June 2026 Teams sends detected third-party
  bots to the lobby (MC1251206). An auto-block policy is rolling out through
  September 2026 (MC1459141). Not viable for a self-hosted platform.

Local capture works with any meeting the person can hear. No bot joins, and
browser meetings need nothing installed. The audio goes only to this platform,
and the person who is in the meeting is the one who asks for consent.

---

## Desktop companion

*Designed, not built.*

A browser cannot capture everything. On Linux, system audio is disabled in
Chromium. macOS before 14.2 has no system audio for the browser. There is no
way to capture one app without sharing the whole screen, and no way at all
from Firefox or Safari. For these cases the design is a small native helper
that captures the microphone and the meeting app's output. It speaks the same
WebSocket protocol, with the two sources `mic` and `tab`, into the same V43
meeting recording, and it uploads the mixed recording through the same parts.

| OS | capture | notes |
|---|---|---|
| macOS 14.2 and later | Core Audio process taps (`AudioHardwareCreateProcessTap`) on the meeting app | `NSAudioCaptureUsageDescription`; asks only for audio permission, not screen recording |
| macOS 13, or wherever taps fail | ScreenCaptureKit (`capturesAudio` from 13.0; `captureMicrophone` needs 15.0) | on macOS 15 Sequoia, apps that capture this way are reportedly asked to re-approve every month |
| Windows | WASAPI loopback; process loopback (`AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS`) to isolate the meeting app | process loopback needs build 20348 or later; Chromium treats it as Windows 11 only |
| Linux | the PipeWire monitor source | — |

The framework would be Tauri and Rust:

- `cpal` 0.17 or later, which added macOS loopback (0.18 fixed silent-loopback
  bugs);
- `wasapi-rs` for loopback and per-application capture;
- `screencapturekit` or `cidre`;
- the `pipewire` crate.

Electron works only with `NSAudioCaptureUsageDescription` in its Info.plist.
From Electron 39 on, a missing entry gives a silent, dead track with no error.
It also has an open macOS bug with custom pickers (#52738).

**Never install a virtual audio driver automatically.** BlackHole is GPL-3.0
(a licence is required for non-GPL projects) and installs by restarting
`coreaudiod` with sudo. VB-CABLE needs an administrator install and a reboot.

**One open question: how the companion signs in.** The gateway accepts only a
page's Origin and the `ts_session` cookie today. A native client needs its own
credential and its own Origin rule. Answering that is part of building the
companion.

---

## Speaker labels after the meeting (Stage B)

*Designed, not built. It runs after the meeting, once measured on real
meetings.*

Only the **Others** channel needs diarising: **You** is already one person.
The design:

1. **Diarise** the stored Others audio with **NVIDIA Nemotron-3-Diarization**,
   released 2026-09-23 and chosen for three reasons:
   - **Coverage:** 100M parameters, up to 8 speakers, and training data that
     includes Hindi (DISPLACE). NVIDIA lists DGX Spark as supported hardware.
   - **Licence:** OpenMDW-1.1, commercial use allowed. The separate
     `-preview` artifact is evaluation-only and must not be used.
   - **Speed:** its chunked 30.4 s mode runs on the CPU. On 4 Cortex-X925
     cores it processed 1,220 s of audio in 23.2 s: RTF 0.019, 1.3 GB
     resident, fp32 through Transformers. A one-hour meeting is about 70 s of
     CPU, with no GPU and no cost to chat. That was measured on the head's CPU,
     pinned to 4 cores at nice 19; the worker has the same GB10 part, but the
     measurement has not been repeated there.
2. **Fall back to pyannote community-1** when more than 8 speakers are
   expected, or when Nemotron's speaker count looks wrong. It is pyannote.audio
   4.0.x, with CC-BY-4.0 gated weights, no speaker cap, and an "exclusive"
   diarization (one speaker at a time). It needs its own pinned image, because
   torchaudio's latest release is 2.11 against the platform's torch 2.14. Its
   telemetry is **on by default** and reports to `otel.pyannote.ai`, so set
   `PYANNOTE_METRICS_ENABLED=0` and block its outbound traffic.
3. **Assign each word to a speaker** on the exclusive diarization: first by
   the largest time overlap, then by the word's midpoint, then by the nearest
   segment. Then smooth: no speaker change shorter than about 0.5 s or two
   words unless a pause and a diarization change agree. Regions where the
   overlapping diarization shows two or more voices are marked. A normal ASR
   picks the dominant voice there, and the only overlap-aware ASR is
   English-only.
4. **Label** the Others lines as *Speaker 1..N*: anonymous, per meeting, and
   never enrolled.

**How good is it?** NVIDIA's own DER at 0 s collar, in 30.4 s mode:

| set | Nemotron-3 | Sortformer v2.1 |
|---|---:|---:|
| AMI-SDM | 11.1 | 21.4 |
| DIHARD III | 12.7 | 19.1 |
| NOTSOFAR-SC | 11.0 | 30.5 |

On the Voice Arena bench (22 h, 2 to 8 speakers) it ranks first at 14.7,
against 30.6 for pyannote community-1, but that bench's independence is not
established. Nobody had reproduced these numbers in the six days after the
release. A synthetic test here, four TTS voices from one voice family, was
merged into two or three speakers, which says nothing about real meetings.

So before building, measure on 2 to 5 hours of in-domain audio: Indian
English, Hinglish and Gujarati, 2 to 10 speakers, laptop and phone
microphones, with RTTM and word references. Measure:

- DER at 0 and 0.25 s collar;
- the speaker-count error;
- word-level speaker error.

Compare Nemotron-3, community-1 and Sortformer v2.1.

**Not live.** Live speaker labels do not scale on the CPU. The 1.04 s streaming
preset costs about 0.74 of a core per stream (p90 832 ms per 720 ms step).
Live meetings get channel labels only.

---

## Meeting summaries and questions, through the video rail

*Designed, not built.* Until it exists, **Add to chat** hands the transcript to
the existing document pipeline.

A finished meeting becomes an audio analysis on the existing video pipeline.
It does not become a second retrieval platform:

1. **Import it without transcribing it again.** Create the analysis under a
   user-keyed content hash, `sha256('meeting\0' + user_id + '\0' +
   source_sha256)`. Seed `probe.json` with the duration from
   `voice_sessions.audio_ms`, and `transcript.json` with the speaker-labelled
   transcript. Stamp the `probe`, `audio` and `transcript` stages done, so
   whisper never runs twice. The frame, OCR and vision stages skip themselves,
   because there is no video stream.
2. **Fusion** runs unchanged on the main model with its JSON schema. It
   produces the summary, chapters, key points, **decisions**, **action
   items**, entities and what the meeting did not cover. It works in one pass
   up to 60k tokens, and map-reduces above that.
3. **Index** runs unchanged. It makes speech chunks of about 45 s and 700
   characters, embeds them with Qwen3-Embedding-0.6B into the LanceDB table
   `video_chunks`, and retrieves with the id prefilter and the reranker.
   Speakers travel as an optional `speaker` field on the transcript segment,
   and as a `[Speaker 2]` prefix in chunk text, so the LanceDB schema does not
   change. `CHUNKER_VERSION` and `PIPELINE_VERSION` are bumped, with scoped
   re-runs.
4. **Ask and search.** Questions go through the unchanged video route, with
   decisions, action items and key points added to its prompt blocks. Search
   across meetings is `index.retrieve` over the analysis ids resolved with
   `user_id = %s`.
5. **Access and deletion.** Access goes through `video_attachments`, plus a
   user-owned meeting table fenced from the 72-hour orphan reaper. Discard and
   retention must also remove the analysis directory, its LanceDB rows and its
   row.

The Artifact Studio's `meeting_summary` template can then turn the same
understanding into DOCX or PDF minutes.

---

## Known limits

- **Meeting audio needs desktop Chrome or Edge.** Firefox, Safari and every
  phone get the microphone only.
- **System audio has platform floors.** It works on Windows and ChromeOS, and
  on macOS only from Chrome 141 on macOS 14.2. It does not work on Linux. The
  macOS permission prompt Chrome shows was not verified.
- **Some capture behaviour is unverified.** Nobody has confirmed that stopping
  the mandatory video track keeps the audio track alive on every OS and
  surface; only secondary reports say so. Per-app capture (`windowAudio:
  'window'`) was not tried.
- **Tab audio cannot be faked in a headless browser.** The tab-capture path is
  verified by hand, not by the automated browser test.
- **Speakers without headphones rely on untested cancellation.** For a desktop
  app on speakers, the echo cancellation is untested, and the server's
  text-overlap rule catches only echoes of three words or more.
- **Speaker labels are channels only** (You and Others) until Stage B.
- **A meeting takes two engine streams** for its whole length, or one with
  *Only my microphone*. The engine's capacity ([REALTIME.md](REALTIME.md#capacity))
  counts them like dictations.
- **Gujarati is not transcribed live**, as for dictation. Whisper's full pass
  still covers it.
- **The consent notice exists only in English**, and nothing announces the
  transcript inside the meeting itself.
