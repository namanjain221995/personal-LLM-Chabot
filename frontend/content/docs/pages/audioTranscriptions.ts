import type { DocPage } from '../types';
import { API_BASE_URL, EXAMPLE_STATUS, WHISPER_MODEL_ID } from '../samples';

// 2026-09-13, owner request: speech to text is on /v1. Two facts shape this
// page more than the request format does. The engine decodes one clip at a
// time and shares both GPUs with the chat model, so public transcription is
// deliberately one clip at a time and yields to chat (CONTRACT §12.3). And a
// forced language translates rather than transcribes (measured 2026-09-10),
// which a caller would otherwise learn from a customer.
import { NO_TIMEOUT_LIVE } from './longOutput';
import { FILES_API_PUBLISHED } from './files';
import { SIDECARS_NO_TIMEOUT_LIVE } from './sidecarsLive';

// 2026-09-13, no-timeout design (revision 2): audio of any length — the file
// streams to disk, is cut into windows of at most 90 seconds at pauses, and is
// transcribed window by window — a 90 MiB request, `stream`, and a `file_id`
// once the Files API is published. Shipped on 2026-09-14: built from
// SIDECARS_NO_TIMEOUT_LIVE (pages/sidecarsLive.ts), or NO_TIMEOUT_LIVE.

const INTRO = `
~~~http
POST /v1/audio/transcriptions
~~~

Requires the \`audio.write\` scope. Keys created before 2026-09-13 do not have
it — see [authentication](/docs/authentication#scopes).
`;
const S_TRANSCRIBE_A_FILE = `
## Transcribe a file

The request is \`multipart/form-data\`: the audio file and a few form fields.

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/audio/transcriptions \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -F "file=@standup.m4a;type=audio/mp4" \\
  -F "model=${WHISPER_MODEL_ID}"
~~~

~~~json
{
  "text": "Yesterday I finished the key rotation work. Today I am on the webhook retries.",
  "usage": { "type": "duration", "seconds": 7 }
}
~~~

**Always name the file's type.** The \`;type=\` after the file name sets the
part's \`Content-Type\`, and a part whose type is not an audio type the API
accepts is refused. Left to itself, curl labels many audio files
\`application/octet-stream\`.
`;
const S_THE_FIELDS = `
## The fields

| Field | Rule |
| --- | --- |
| \`file\` | Required. One audio file, at most **25 MiB**, with an accepted \`Content-Type\` (below). |
| \`model\` | Required. \`${WHISPER_MODEL_ID}\`. |
| \`language\` | Optional. An ISO-639-1 code such as \`en\`, \`hi\` or \`de\`, or \`auto\` — the default. Read [languages](#languages) before you set it. |
| \`response_format\` | Optional. \`json\` (the default), \`text\` or \`verbose_json\`. |
| \`timestamp_granularities[]\` | Optional. Only \`segment\`, and only with \`verbose_json\`. |

Any other field is a \`400\` naming it — \`temperature\` and \`prompt\`
included — and so are the \`srt\` and \`vtt\` formats. The whole request body is
at most 26 MiB.

Accepted audio types: \`audio/webm\`, \`audio/ogg\`, \`audio/mp4\`,
\`audio/mpeg\`, \`audio/mpga\`, \`audio/wav\`, \`audio/x-wav\`, \`audio/wave\`,
\`audio/flac\`, \`audio/x-flac\`, \`audio/aac\`, \`audio/m4a\`, \`audio/x-m4a\`,
\`audio/opus\`, \`audio/3gpp\`, and \`video/webm\` and \`video/mp4\` for the
audio-only recordings browsers label that way.
`;
const S_RESPONSE_FORMATS = `
## Response formats

\`json\` — the text, and how much audio it was:

~~~json
{ "text": "…", "usage": { "type": "duration", "seconds": 42 } }
~~~

The \`seconds\` in \`usage\` is the clip's length, rounded up to a whole second.

\`text\` — the transcript alone, as a \`text/plain\` body.

\`verbose_json\` — the text with the detected language, the duration, and
timestamped segments:

~~~json
{
  "task": "transcribe",
  "language": "english",
  "duration": 41.6,
  "text": "Yesterday I finished the key rotation work. Today I am on the webhook retries.",
  "segments": [
    { "id": 0, "start": 0.0, "end": 3.9, "text": "Yesterday I finished the key rotation work." },
    { "id": 1, "start": 4.2, "end": 7.1, "text": "Today I am on the webhook retries." }
  ],
  "usage": { "type": "duration", "seconds": 42 }
}
~~~

\`language\` is the language's name in lower case, or \`null\` when none was
detected. A clip that is silence — or close enough — comes back with empty
\`text\` rather than words invented to fill it.
`;
const S_LANGUAGES = `
## Languages

**Let the model detect the language.** Leave \`language\` out, or send
\`auto\`.

Setting \`language\` does not *hint* the language of the speech; it decides the
language of the *output*. Force \`en\` on a recording in Hindi and you get a
fluent English translation, not a Hindi transcript — which reads as a correct
answer and is not one. Set \`language\` only when you know every clip is in that
language, and never as a way to ask for English.

Accuracy is not the same in every language. Widely spoken languages transcribe
well; some — Gujarati among them — are noticeably weaker and slower. Check a
sample of real recordings in each language you depend on.
`;
const S_LIMITS = `
## Limits

| Limit | Value | Over it |
| --- | --- | --- |
| Audio length | 300 seconds | \`413 request_too_large\` |
| File size | 25 MiB | \`413 request_too_large\` |
| Request body | 26 MiB | \`413 request_too_large\` |

For longer recordings, cut the audio into clips of at most five minutes —
ideally at pauses — transcribe them in order, and join the text.
`;
const S_CAPACITY_ONE_CLIP_AT_A_TIME = `
## Capacity: one clip at a time

The speech engine decodes **one clip at a time**, runs on the same machines as
the TechSara chat model, and a busy speech engine slows chat for everyone. So
public transcription is deliberately limited to one clip at a time across the
whole deployment, and it waits while someone is chatting or dictating in the
TechSara application. This is the engine's capacity, shared by every caller —
not a limit on your key.

What that means for your client:

* A five-minute clip takes roughly 35 to 45 seconds to transcribe once it
  starts, and it may wait up to 30 seconds for its turn first — so through the
  public hostname, where a synchronous request must finish within about 100
  seconds, keep clips at five minutes or less.
* When the turn does not come within that wait, the answer is
  \`503 model_unavailable\` with \`Retry-After\`. Wait for it, add jitter, and
  retry.
* Sending clips in parallel does not make a batch faster: they queue behind
  each other. Send them one after another.
`;
const S_ERRORS = `
## Errors

| You see | Because | Do |
| --- | --- | --- |
| \`400 invalid_request_error\` | A missing \`file\` or \`model\`, an unsupported field or format, an unaccepted \`Content-Type\`, a file that is not decodable audio, or an \`Idempotency-Key\` header. | Fix the request. |
| \`413 request_too_large\` | Over 300 seconds, 25 MiB, or a 26 MiB body. | Cut the audio into shorter clips. |
| \`503 model_unavailable\` | The engine is down, or busy with other audio or with the chat application. | Wait for \`Retry-After\` and retry. |
| \`504 timeout\` | The transcription did not finish within 240 seconds. | Retry with a shorter clip. |

A transcription stores nothing, so an \`Idempotency-Key\` is refused rather than
ignored; the same clip gives the same text, and a plain retry is safe. The
audio itself is never stored.
`;
const S_FROM_PYTHON = `
## From Python

~~~python
import os
import httpx

with open("standup.m4a", "rb") as audio:
    response = httpx.post(
        "${API_BASE_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.environ['TECHSARA_API_KEY']}"},
        files={"file": ("standup.m4a", audio, "audio/mp4")},
        data={"model": "${WHISPER_MODEL_ID}", "response_format": "verbose_json"},
        timeout=300.0,
    )
response.raise_for_status()
body = response.json()
for segment in body["segments"]:
    print(f"{segment['start']:7.1f}s  {segment['text']}")
~~~
`;
const S_USAGE = `
## Usage

Each call is one request in your [usage](/docs/usage). A transcription has no
token counts, so it adds none; the audio seconds are recorded with the request
but are **not** part of \`GET /v1/usage\`, which reports requests, tokens and
errors.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_THE_FIELDS = `
## The fields

| Field | Rule |
| --- | --- |
| \`file\` | Required${FILES_API_PUBLISHED ? ', unless \`file_id\` is sent' : ''}. One audio file, at most **89 MiB**, with an accepted \`Content-Type\` (below). Any length. |
${FILES_API_PUBLISHED ? '| \`file_id\` | Instead of \`file\`: an audio or video file already uploaded to this project through [uploads](/docs/uploads) — any size. |\n' : ''}| \`model\` | Required. \`${WHISPER_MODEL_ID}\`. |
| \`language\` | Optional. An ISO-639-1 code such as \`en\`, \`hi\` or \`de\`, or \`auto\` — the default. Read [languages](#languages) before you set it. |
| \`response_format\` | Optional. \`json\` (the default), \`text\` or \`verbose_json\`. |
| \`timestamp_granularities[]\` | Optional. Only \`segment\`, and only with \`verbose_json\`. |
| \`stream\` | Optional, default \`false\`. \`true\` sends the transcript as it is written — recommended above about 10 minutes of audio. |

Any other field is a \`400\` naming it — \`temperature\` and \`prompt\`
included — and so are the \`srt\` and \`vtt\` formats. The whole request body is
at most 90 MiB.

Accepted audio types: \`audio/webm\`, \`audio/ogg\`, \`audio/mp4\`,
\`audio/mpeg\`, \`audio/mpga\`, \`audio/wav\`, \`audio/x-wav\`, \`audio/wave\`,
\`audio/flac\`, \`audio/x-flac\`, \`audio/aac\`, \`audio/m4a\`, \`audio/x-m4a\`,
\`audio/opus\`, \`audio/3gpp\`, and \`video/webm\` and \`video/mp4\` for the
audio-only recordings browsers label that way.
`;
const LATER_RESPONSE_FORMATS = `
## Response formats

\`json\` — the text, and how much audio it was:

~~~json
{ "text": "…", "usage": { "type": "duration", "seconds": 42 } }
~~~

The \`seconds\` in \`usage\` is the recording's length, rounded up to a whole
second.

\`text\` — the transcript alone, as a \`text/plain\` body. **Strip it.** A
transcription that takes longer than 15 seconds sends its \`200\` early and keeps
the connection alive with spaces, which then lead the text.

\`verbose_json\` — the text with the detected language, the duration, and
timestamped segments:

~~~json
{
  "task": "transcribe",
  "language": "english",
  "duration": 41.6,
  "text": "Yesterday I finished the key rotation work. Today I am on the webhook retries.",
  "segments": [
    { "id": 0, "start": 0.0, "end": 3.9, "text": "Yesterday I finished the key rotation work." },
    { "id": 1, "start": 4.2, "end": 7.1, "text": "Today I am on the webhook retries." }
  ],
  "usage": { "type": "duration", "seconds": 42 }
}
~~~

\`language\` is the language's name in lower case, or \`null\` when none was
detected. A clip that is silence — or close enough — comes back with empty
\`text\` rather than words invented to fill it.

With \`"stream": true\` the transcript arrives as server-sent events:
\`transcript.text.delta\` events carrying text as it is confirmed, then one
\`transcript.text.done\` with the whole text and the usage. Waiting shows as
\`: queued\` and \`: ping\` comments. Text already sent is never taken back.
`;
const LATER_LIMITS = `
## Limits

| Limit | Value | Over it |
| --- | --- | --- |
| Audio length | None | — |
| File size | 89 MiB | \`413 request_too_large\` |
| Request body | 90 MiB | \`413 request_too_large\` |

A long recording is cut at pauses into windows of at most 90 seconds, which are
transcribed in order and joined so that no word at a boundary is lost or
repeated. You send one file and get one transcript.
${FILES_API_PUBLISHED ? '\nA file larger than 89 MiB goes through [uploads](/docs/uploads) in parts; then send its \`file_id\`.\n' : ''}
`;
const LATER_CAPACITY_ONE_CLIP_AT_A_TIME = `
## Capacity: one window at a time

The speech engine decodes **one clip at a time**, runs on the same machines as
the TechSara chat model, and a busy speech engine slows chat for everyone. So
public transcription sends one window at a time across the whole deployment,
and waits while someone is chatting or dictating in the TechSara application.
This is the engine's capacity, shared by every caller — not a limit on your key.

What that means for your client:

* A recording takes roughly a seventh of its length to transcribe once its turn
  comes — about 9 minutes for an hour — and it waits for its turn first, however
  long that is. **Nothing is refused for being busy**, and there is no time limit:
  the connection is kept alive with a byte at least every 15 seconds.
* Above about 10 minutes of audio, send \`"stream": true\` so you see the text
  as it is written.
* If the connection drops, send the same file again: windows already transcribed
  are reused, and a retry picks up the job still running.
* Sending recordings in parallel does not make a batch faster: they queue behind
  each other.
`;
const LATER_ERRORS = `
## Errors

| You see | Because | Do |
| --- | --- | --- |
| \`400 invalid_request_error\` | A missing \`file\` or \`model\`, an unsupported field or format, an unaccepted \`Content-Type\`, a file that is not decodable audio, or an \`Idempotency-Key\` header. | Fix the request. |
| \`413 request_too_large\` | A file over 89 MiB, or a body over 90 MiB. | ${FILES_API_PUBLISHED ? 'Upload it in parts and send its \`file_id\`.' : 'Cut the audio into smaller files.'} |
| \`503 model_unavailable\` | The engine is down, a physical safeguard tripped (too little free disk to decode the file), or the engine restarted twice under your request (then with \`x-should-retry: false\`). | Wait for \`Retry-After\` and retry — unless told not to. |

An \`Idempotency-Key\` header is refused here rather than ignored. Sending the
same file again is already safe — the same audio gives the same text, and
finished work is reused. The audio is kept on
disk only while it is being transcribed, and finished windows for 24 hours so a
retry can reuse them.
`;
const LATER_FROM_PYTHON = `
## From Python

~~~python
import os
import httpx

with open("standup.m4a", "rb") as audio:
    response = httpx.post(
        "${API_BASE_URL}/audio/transcriptions",
        headers={"Authorization": f"Bearer {os.environ['TECHSARA_API_KEY']}"},
        files={"file": ("standup.m4a", audio, "audio/mp4")},
        data={"model": "${WHISPER_MODEL_ID}", "response_format": "verbose_json"},
        timeout=httpx.Timeout(10.0, read=None),
    )
response.raise_for_status()
body = response.json()
for segment in body["segments"]:
    print(f"{segment['start']:7.1f}s  {segment['text']}")
~~~
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function audioTranscriptionsPage({
  noTimeout,
  sidecarsLive = SIDECARS_NO_TIMEOUT_LIVE,
}: {
  noTimeout: boolean;
  sidecarsLive?: boolean;
}): DocPage {
  const live = noTimeout || sidecarsLive;
  const sections = live
    ? [INTRO, S_TRANSCRIBE_A_FILE, LATER_THE_FIELDS, LATER_RESPONSE_FORMATS, S_LANGUAGES, LATER_LIMITS, LATER_CAPACITY_ONE_CLIP_AT_A_TIME, LATER_ERRORS, LATER_FROM_PYTHON, S_USAGE]
    : [INTRO, S_TRANSCRIBE_A_FILE, S_THE_FIELDS, S_RESPONSE_FORMATS, S_LANGUAGES, S_LIMITS, S_CAPACITY_ONE_CLIP_AT_A_TIME, S_ERRORS, S_FROM_PYTHON, S_USAGE];
  return {
    slug: 'audio-transcriptions',
    title: 'Audio transcriptions',
    summary: live
      ? 'POST /v1/audio/transcriptions turns speech of any length into text ' +
        'with techsara-whisper, with optional segment timestamps and streaming.'
      : 'POST /v1/audio/transcriptions turns up to five minutes of speech into ' +
      'text with techsara-whisper, with optional segment timestamps.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const audioTranscriptions: DocPage = audioTranscriptionsPage({ noTimeout: NO_TIMEOUT_LIVE });
