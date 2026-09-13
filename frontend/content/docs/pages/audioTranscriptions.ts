import type { DocPage } from '../types';
import { API_BASE_URL, EXAMPLE_STATUS, WHISPER_MODEL_ID } from '../samples';

// 2026-09-13, owner request: speech to text is on /v1. Two facts shape this
// page more than the request format does. The engine decodes one clip at a
// time and shares both GPUs with the chat model, so public transcription is
// deliberately one clip at a time and yields to chat (CONTRACT §12.3). And a
// forced language translates rather than transcribes (measured 2026-09-10),
// which a caller would otherwise learn from a customer.
export const audioTranscriptions: DocPage = {
  slug: 'audio-transcriptions',
  title: 'Audio transcriptions',
  summary:
    'POST /v1/audio/transcriptions turns up to five minutes of speech into ' +
    'text with techsara-whisper, with optional segment timestamps.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
~~~http
POST /v1/audio/transcriptions
~~~

Requires the \`audio.write\` scope. Keys created before 2026-09-13 do not have
it — see [authentication](/docs/authentication#scopes).

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

## Limits

| Limit | Value | Over it |
| --- | --- | --- |
| Audio length | 300 seconds | \`413 request_too_large\` |
| File size | 25 MiB | \`413 request_too_large\` |
| Request body | 26 MiB | \`413 request_too_large\` |

For longer recordings, cut the audio into clips of at most five minutes —
ideally at pauses — transcribe them in order, and join the text.

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

## Usage

Each call is one request in your [usage](/docs/usage). A transcription has no
token counts, so it adds none; the audio seconds are recorded with the request
but are **not** part of \`GET /v1/usage\`, which reports requests, tokens and
errors.
`.trim(),
};
