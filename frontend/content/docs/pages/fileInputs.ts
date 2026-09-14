import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EMBED_MODEL_ID,
  EXAMPLE_RESPONSE_ID,
  EXAMPLE_STATUS,
  MODEL_ID,
  OCR_MODEL_ID,
  RERANK_MODEL_ID,
  VISION_MODEL_ID,
  WHISPER_MODEL_ID,
} from '../samples';
import { EXAMPLE_FILE_ID } from './files';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, Files API programme: the model-input half, as built in
// orchestrator/app/apifiles/{service,context,retrieval,citations,inline}.py.
// Published with files.ts and uploads.ts under FILES_API_PUBLISHED. What the
// page has to get across: a file becomes context differently per kind and per
// model; a big document is retrieved, not inlined, unless you ask; and a
// citation is only ever made from content the model was actually shown.

/** The synchronous row of the readiness table, per no-timeout state. */
function syncReadiness(noTimeout: boolean): string {
  if (noTimeout) {
    return (
      '| Synchronous | Waits as long as the file takes. The response starts within 15 seconds and ' +
      'is kept open with whitespace until the answer is ready — see [long outputs](/docs/long-output#the-wall-clock). |'
    );
  }
  return (
    '| Synchronous | Waits up to 30 seconds. A file still not ready then is `409 file_not_ready` with ' +
    '`Retry-After: 5` (documents, tables, images) or `Retry-After: 30` (audio, video): retry after that, or ' +
    'use streaming or background, which never get this error. |'
  );
}

/** The paragraph on a synchronous request's shared budget — today only. */
function syncBudget(noTimeout: boolean): string {
  if (noTimeout) return '';
  return `
**A synchronous request has one budget for all of this.** Everything that
happens before its answer starts — waiting for files, searching them, reading
page images, transcribing \`input_audio\` — shares about 45 seconds, because a
response that stays silent much longer is cut off on its way to you. When the
budget runs short, the optional steps give way rather than the request:
reranking is skipped and search falls back to keyword ranking, and page images
are left out while the text stays. Streaming and background requests have no
such budget.
`;
}

/** The refusals of a synchronous request's shared budget — today only. */
function syncBudgetErrors(noTimeout: boolean): string {
  if (noTimeout) return '';
  return (
    '| 400 | `invalid_request_error` | An inline `file_data` that could not be read within a synchronous ' +
    "request's budget: upload it with `/v1/files`, or use streaming or background. |\n" +
    '| 503 | `model_unavailable` | `input_audio` that could not be transcribed within a synchronous ' +
    "request's budget: \"The model is at capacity right now.\" Safe to retry after `Retry-After: 30`, or use streaming or background. |\n"
  );
}

/** The synchronous rule for `input_audio`, which exists only while the budget does. */
function syncAudio(noTimeout: boolean): string {
  if (noTimeout) return '';
  return `
A **synchronous** request may carry at most 300 seconds of \`input_audio\` in
total — and an MP3 counts as the full 300, because its length is unknown until
it is decoded. More than that is a \`400\` asking for streaming or background.
`;
}

export function fileInputsPage({ noTimeout }: { noTimeout: boolean }): DocPage {
  return {
    slug: 'file-inputs',
    title: 'Files as model input',
    summary:
      'Put documents, spreadsheets, images, PDF pages, audio and video into a request ' +
      'by file_id — inlined when they fit, retrieved with page and timestamp citations when they do not.',
    section: 'API reference',
    examples: EXAMPLE_STATUS,
    body: `
A [file](/docs/files) is used in a request the way an image is: as a content
part of a \`user\` message, on [\`/v1/responses\`](/docs/responses) or
[\`/v1/chat/completions\`](/docs/chat-completions). What the model then
receives depends on what the file is and which model you ask — this page is
that table, and the rules behind it.

A request that names a \`file_id\` needs the \`files.read\` scope as well as
\`responses.write\`. The scope is checked before any id is looked up, so a key
without it learns nothing about which files exist. Bytes sent inline, with
\`file_data\`, need no file scope: you already hold them.

## A document in a question

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL_ID}",
    "input": [
      {
        "role": "user",
        "content": [
          { "type": "input_file", "file_id": "${EXAMPLE_FILE_ID}" },
          { "type": "input_text", "text": "What drove the change in gross margin? Cite the pages." }
        ]
      }
    ]
  }'
~~~

~~~json
{
  "id": "${EXAMPLE_RESPONSE_ID}",
  "object": "response",
  "created_at": 1789300400,
  "status": "completed",
  "model": "${MODEL_ID}",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [
        {
          "type": "output_text",
          "text": "Gross margin fell two points, mostly on freight costs [q3-report.pdf p.842].",
          "annotations": [
            { "type": "file_citation", "file_id": "${EXAMPLE_FILE_ID}", "filename": "q3-report.pdf", "index": 54, "page": 842 }
          ]
        }
      ]
    }
  ],
  "max_output_tokens": 8192,
  "incomplete_details": null,
  "usage": { "input_tokens": 33412, "output_tokens": 21, "total_tokens": 33433 }
}
~~~

The same question with the \`openai\` Python package, asking for a larger
retrieval budget through \`extra_body\`:

~~~python
import os
from openai import OpenAI

client = OpenAI(base_url="${API_BASE_URL}", api_key=os.environ["TECHSARA_API_KEY"])

answer = client.responses.create(
    model="${MODEL_ID}",
    input=[{"role": "user", "content": [
        {"type": "input_file", "file_id": "${EXAMPLE_FILE_ID}"},
        {"type": "input_text", "text": "What drove the change in gross margin? Cite the pages."},
    ]}],
    extra_body={"file_context": {"mode": "retrieval", "max_tokens": 48000}},
)

for item in answer.output:
    for part in getattr(item, "content", None) or []:
        if part.type != "output_text":
            continue
        print(part.text)
        for note in part.annotations:
            if note.type == "file_citation":          # branch on type: it is not a url_citation
                print("  ", note.filename, "page", getattr(note, "page", None))
~~~

And on Chat Completions, with a smaller model:

~~~python
completion = client.chat.completions.create(
    model="${VISION_MODEL_ID}",
    messages=[{"role": "user", "content": [
        {"type": "file", "file": {"file_id": "${EXAMPLE_FILE_ID}"}},
        {"type": "text", "text": "List every action item and its owner."},
    ]}],
)
print(completion.choices[0].message.content)
~~~

## The content parts

On \`/v1/responses\`:

| Part | Fields | Takes |
| --- | --- | --- |
| \`input_file\` | \`file_id\` **or** \`file_data\` (with \`filename\`); optional \`detail\`: \`low\`, \`auto\`, \`high\` | Any file kind. |
| \`input_image\` | \`file_id\`; optional \`detail\`: \`low\`, \`auto\`, \`high\`, \`original\` | A file of kind \`image\`. (\`image_url\` with a \`data:\` URL works as on [images](/docs/images).) |
| \`input_video\` | \`file_id\`; optional \`detail\` | A file of kind \`audio\` or \`video\`. A TechSara addition — \`input_file\` with the same id does exactly the same. |

On \`/v1/chat/completions\`:

| Part | Fields | Takes |
| --- | --- | --- |
| \`file\` | \`{"file": {"file_id": …}}\` or \`{"file": {"file_data": …, "filename": …}}\` | Any file kind, including images and video. |
| \`input_audio\` | \`{"input_audio": {"data": <base64>, "format": "wav" or "mp3"}}\` | Up to 300 seconds of speech, transcribed into the prompt. |
| \`image_url\` | as on [images](/docs/images) | An inline image. |

And one request-level field on both, \`file_context\`, described
[below](#documents-inline-or-retrieved).

The rules:

* File parts go in \`user\` messages only.
* A part has exactly one of \`file_id\` and \`file_data\`. \`file_url\` is
  refused: files are never fetched from a URL — upload the file and pass its
  id.
* At most **20 file parts** in one request, of which at most **3** are audio
  or video. An \`input_audio\` clip counts as a file part and as one of the
  three.
* \`input_image\` must name an image, and \`input_video\` a recording; the
  \`400\` suggests \`input_file\` for anything else.
* The TypeScript types of the \`openai\` Node package know \`input_file\` but
  not \`input_video\` or \`file_context\`: use \`input_file\` for video, and
  \`// @ts-expect-error\` above \`file_context\`. In Python, send
  \`file_context\` through \`extra_body\`.

## What each model accepts

| Model | Files it takes |
| --- | --- |
| \`${MODEL_ID}\` | Every kind. Documents and tables as text, images as pixels (up to 16 per request), PDF pages as images too, audio and video as transcripts with frames. The largest window: up to 1,000,000 tokens of files in \`full\` mode. |
| \`${VISION_MODEL_ID}\` | Every kind, like \`${MODEL_ID}\`, up to 8 images per request, within a 24,576-token window — so documents of any size are usually retrieved rather than inlined. |
| \`${OCR_MODEL_ID}\` | **Exactly one image**, from a file or inline, and nothing else. It returns the text in the image. |
| \`${EMBED_MODEL_ID}\`, \`${RERANK_MODEL_ID}\` | None: they do not serve these endpoints. Download a file's extracted \`text.txt\` from [derived data](/docs/files#derived-data) and embed that. |
| \`${WHISPER_MODEL_ID}\` | None directly — it transcribes every audio and video **file** during processing. Download \`transcript.txt\`, \`.srt\`, \`.vtt\` or \`.json\` from [derived data](/docs/files#derived-data). |

What a file becomes, by kind:

| Kind | The model receives |
| --- | --- |
| \`pdf\`, \`document\`, \`presentation\`, \`text\`, \`html\` | The text, labelled by page, section or slide — all of it, or the relevant excerpts. On a PDF, also images of a few pages, depending on \`detail\`. |
| \`spreadsheet\`, \`tabular\` | A profile of the columns, then the rows in labelled blocks of 200 — all of them, or the relevant blocks. |
| \`image\` | The picture, at the size \`detail\` asks for. |
| \`audio\` | A header, the summary, the chapters and the timestamped transcript — all of it, or the relevant stretches. |
| \`video\` | The same as audio plus text read off the screen, and frames when the question is about what is shown. |

## Documents: inline or retrieved

\`file_context\` decides how file text is put into the prompt:

~~~json
{ "file_context": { "mode": "auto", "max_tokens": 32000 } }
~~~

| \`mode\` | What happens |
| --- | --- |
| \`auto\` (default) | If the text of **all** the files in the request fits in 100,000 estimated tokens, every word goes in. If not, each file's relevant excerpts are retrieved, up to the retrieval budget. |
| \`retrieval\` | Always excerpts, up to \`max_tokens\`: 32,000 by default, 200,000 at most. |
| \`full\` | Every word of every file, up to the model's window — minus your own text, the output ceiling and a 2,048-token reserve. Over that is \`400 context_length_exceeded\`, with \`param\` \`file_context.mode\` and a suggestion to use \`retrieval\`. |

**Why 100,000 in \`auto\`.** Reading a prompt takes longer the longer it is:
a nearly full window takes around 15 minutes before the first output token —
measured at 878 seconds for about 950,000 tokens. Keeping automatic requests
at 100,000 tokens keeps them quick. \`full\` is there when you need every word
and can wait: use it with [streaming or background](/docs/long-output).

**On \`${VISION_MODEL_ID}\`** the window is the budget: with the default
8,192-token output ceiling, files inline up to 14,336 tokens, and retrieval
packs at most 12,288.

**How retrieval chooses.** During processing a document is cut into chunks of
up to 1,500 characters, never across a page boundary, and indexed. For a
request, the question is embedded once, each file is searched, the candidates
are reordered by \`${RERANK_MODEL_ID}\`, and the best are packed into the
budget in page order — with the chunk after each one and the start of the
document when there is room. If the reranker is busy the search order is used
instead, and if the question cannot be embedded, keyword ranking: retrieval
never fails a request. A request with no question of its
own ("summarise this") gets excerpts spread evenly across the whole document
instead of the most similar ones.

**The question** the search aims at is the text of the last \`user\`
message — or \`instructions\`, when that message has no text. Put the real
question there: a request with neither is treated as "summarise this".

**Inside the prompt**, each file's text sits in a block marked as data, one
location label per page — \`[q3-report.pdf p.12]\` — so the model can cite
it. Text in a file that tries to close that block or forge a label is escaped.
That is a mitigation, not a guarantee: a document can still contain
instructions a model might follow. Do not use a file you do not trust to make
decisions the reader of the answer cannot check.
${syncBudget(noTimeout)}
## Spreadsheets and tables

Every table starts with its profile — sheets, columns, types, counts and
examples — and the profile is always sent, even when the rows are retrieved.
Rows follow in blocks of 200 with the header repeated, labelled
\`[sales.xlsx rows 201-400]\`. A question about totals across millions of
rows is better answered from the profile, or by downloading \`sheet-1.csv\`
and computing it yourself, than by hoping the right rows are retrieved.

## Images from files

\`input_image\` with a \`file_id\` sends one of the sizes stored when the image
was processed:

| \`detail\` | Long edge | About, on \`${MODEL_ID}\` |
| --- | --- | --- |
| \`low\` | 896 pixels | 525 tokens |
| \`auto\` (default) | 1,600 pixels | 1,413 tokens for a 16:9 image |
| \`high\`, \`original\` | 2,560 pixels | 3,613 tokens for a 16:9 image |

A smaller original is sent at its own size. Images per request: 16 on
\`${MODEL_ID}\`, 8 on \`${VISION_MODEL_ID}\`, 1 on \`${OCR_MODEL_ID}\` — and
the images you attached count first. Page images and video frames only use
what is left, and are left out, not refused, when nothing is.

**To read text out of an image**, send one image file to \`${OCR_MODEL_ID}\`
with no text part; the server adds the one-word instruction \`OCR\`, which is
what the model reads best with.

## PDF pages as images

On a PDF, \`input_file.detail\` decides whether page images go with the text:

| \`detail\` | Page images |
| --- | --- |
| \`low\` | None — text only. |
| \`auto\` (default) | Up to 2. |
| \`high\` | Up to 6. |

The pages chosen are the best-matching pages when the file is retrieved, and
otherwise the scanned or nearly empty pages first — where the text layer says
least — then the opening pages. Each image is sized like an image file of the
same \`detail\`, is made for the request and not stored, and counts against
the model's images. A presentation is sent as its slide text and speaker notes,
without images.

## Scanned documents

A PDF page whose text layer has fewer than 200 characters is read with OCR
while the file is processed, up to 1,000 pages per file; \`processing.facts\`
reports \`ocr_pages\` and \`ocr_skipped_pages\`. A request then uses that text
like any other — there is nothing to ask for.

OCR is best effort, and a page it did not read does not fail the file: pages
past the budget, pages OCR could not read, and every scanned page when OCR is
not available on the service all keep only their text layer, and the file
still ends \`processed\`. The model then has nothing from those pages, and an
answer about them can only say so. In \`pages.json\`, a page read by OCR has
\`"source": "ocr"\` — check it before relying on a scanned document, and see
[the facts](/docs/files#kinds-and-what-processing-does).

## Audio and video

A recording is processed once: transcribed by \`${WHISPER_MODEL_ID}\`,
summarised, and divided into chapters; for a video, frames are sampled, the
text on screen is read, and what is shown is described. Up to 4 hours; a
3-hour recording is typically ready in 25 to 90 minutes.

In a request, the model receives:

1. **Always** a header — duration, type, language — the summary, up to 30
   chapters with their start times, and what the analysis did not cover.
2. **The whole timestamped transcript**, with the on-screen text of a video,
   when it fits the \`auto\` budget — a 3-hour talk usually does. Otherwise the
   stretches most relevant to the question, plus 45 seconds either side of up
   to three times the question names ("what was said at 1:02:15?").
3. **Frames**, from a video, on a vision model, when the question is about
   what is shown or names a time, or \`detail\` is \`high\`: up to 3 frames
   (\`auto\`) or 8 (\`high\`), each about 525 tokens. \`detail: "low"\` sends
   none.

~~~python
answer = client.responses.create(
    model="${MODEL_ID}",
    input=[{"role": "user", "content": [
        {"type": "input_file", "file_id": "${EXAMPLE_FILE_ID}", "detail": "auto"},
        {"type": "input_text", "text": "What does the slide on screen at 42:10 say, and who is presenting it?"},
    ]}],
)
~~~

Citations into a recording are timestamps: \`[standup.mp4 42:10]\`, or
\`h:mm:ss\` past the first hour.

**\`input_audio\`** on Chat Completions is for a short clip that is not worth
uploading: base64 WAV or MP3 of up to 300 seconds, inside the 20 MiB request
body — about 15 MiB of audio. It is transcribed and the transcript is put in
the prompt, marked as a transcript. Anything longer, upload as a file.
${syncAudio(noTimeout)}
## Inline file_data

\`file_data\` sends the file inside the request instead of by id, as base64 or
as a \`data:\` URL, with a \`filename\`:

~~~json
{ "type": "input_file", "filename": "memo.docx", "file_data": "data:application/vnd.openxmlformats-officedocument.wordprocessingml.document;base64,UEsDBBQABgAIAAAAIQ…" }
~~~

* The base64 counts toward the 20 MiB request body, so the file itself can be
  at most about **15 MiB**.
* Documents, tables, text and images only. Audio and video are refused: they
  need the processing a file gets — upload them.
* A PDF needing OCR is read in the request, and the two kinds of request
  treat a long scan differently. A **synchronous** request with more than 8
  scanned pages is refused with a \`400\` pointing to \`/v1/files\`, before
  any page is read. A **streamed or background** request is never refused for
  it: OCR reads the first 40 scanned pages, in page order, and **every scanned
  page after those reaches the model as its text layer — usually nearly
  empty — with no error**. Upload a scanned document instead, where OCR
  covers up to 1,000 pages.
* The bytes are kept only while the response runs and are never listed. An
  inline file has no id, so an answer's citations of it stay plain text and
  produce no annotation.

For anything you will ask about twice, upload it: processing happens once
instead of on every request.

## A file that is still processing

You do not have to wait for \`status: "processed"\` before naming a file. A
request that names one waits for it, in a way that depends on how you asked:

| Request | While the file is still processing |
| --- | --- |
${syncReadiness(noTimeout)}
| \`"stream": true\` | Starts at once and waits inside the stream, however long processing takes, with comments showing progress — \`: file ${EXAMPLE_FILE_ID} transcript 40%\`. Every SSE client ignores comments. |
| \`"background": true\` | Answers \`202\` at once; the response stays \`queued\` until its files are ready. Cancelling it stops the wait. |

A file that ends in \`error\` — or was \`unsupported\` from the start — is a
\`400\` naming its part, with the fixed sentence of its code from
[the processing errors](/docs/files#errors). That is the file's
\`status_details\`, except for \`file_too_complex\`, whose \`400\` leaves out
which ceiling it was. On a stream that has already started, it arrives as
\`response.failed\`.

Once the answer is being generated, deleting the file changes nothing about
it.

## Citations

The model is told to cite what it uses with the location labels it was shown:

| File | Label |
| --- | --- |
| PDF | \`[q3-report.pdf p.842]\` |
| Word document, text, HTML | \`[notes.docx §3]\` |
| Presentation | \`[deck.pptx slide 4]\` |
| Spreadsheet, table | \`[sales.xlsx rows 201-400]\` |
| Audio, video | \`[standup.mp4 42:10]\` |

After generation, every label is checked against what the model was actually
given. **Only a label that names content that was in the prompt becomes an
annotation**; a page the model never saw — which models do sometimes cite —
stays plain text. So an annotation can be linked with confidence.

~~~json
{ "type": "file_citation", "file_id": "${EXAMPLE_FILE_ID}", "filename": "standup.mp4", "index": 118, "timestamp_s": 2530.0 }
~~~

| Field | Meaning |
| --- | --- |
| \`file_id\`, \`filename\` | The file. When two files share a name, the second is labelled \`report (2).pdf\`, and \`filename\` is still the real name. |
| \`index\` | Where the label starts in the text, counted in **UTF-16 code units** — JavaScript string indices. In Python, convert before slicing a string that contains characters outside the Basic Multilingual Plane, such as most emoji. |
| \`page\` | The page, section, slide or row block. |
| \`timestamp_s\` | For audio and video, instead of \`page\`: seconds from the start. |

Where the annotations are:

* **Responses:** on the \`output_text\` part, in \`annotations\`. A stream
  sends one \`response.output_text.annotation.added\` event per annotation,
  after \`response.output_text.done\`, and \`response.completed\` carries
  them all.
* **Chat Completions:** \`choices[0].message.annotations\`, and in a stream on
  the last chunk's \`delta.annotations\`.
* **A response read back later:** a response fetched with
  \`GET /v1/responses/{id}\` — which is how a background request's answer is
  delivered — has no annotations. The labels are still in its text, but they
  have not been checked against what the model was given, so do not link
  them as citations.

The SDKs type annotations as a union, and a \`file_citation\` has no
\`url_citation\` field: always branch on \`annotation.type\`.

## Errors

| Status | Code | When |
| --- | --- | --- |
| 403 | \`insufficient_scope\` | A \`file_id\` in a request from a key without \`files.read\`. |
| 404 | \`file_not_found\` | The id is absent, deleted, expired or another project's. \`param\` names the part, like \`input.0.content.0.file_id\`. |
| 409 | \`file_not_ready\` | A synchronous request whose file did not finish processing in time. Retry after \`Retry-After\`. |
| 400 | \`invalid_request_error\` | A file that failed or is unsupported (with its code's sentence); \`file_url\`; more than 20 file parts, or more than 3 recordings and \`input_audio\` clips together; a part in a non-\`user\` message; an image file to a model that cannot see; more than one image, or anything but an image, to \`${OCR_MODEL_ID}\`; \`input_image\` naming something that is not an image. |
| 400 | \`context_length_exceeded\` | \`full\` mode, and the files do not fit the window. |
| 413 | \`request_too_large\` | \`file_data\` pushing the body past 20 MiB. |
${syncBudgetErrors(noTimeout)}
As everywhere, the envelope is on [errors](/docs/errors#the-envelope).

## Limits and why

| Ceiling | Value | Why |
| --- | --- | --- |
| File parts per request | 20 | Each file is resolved, read and possibly searched before generation starts. |
| Audio and video per request | 3 | A recording brings its transcript, summary and frames; three is already a long prompt. |
| Automatic inlining | 100,000 estimated tokens, across all files | Keeps an automatic request's prompt quick to read; \`full\` is there for more. |
| Retrieval budget | 32,000 tokens by default, 200,000 at most | About 55 dense pages of excerpts by default; the maximum keeps even a retrieved prompt far from a full window. |
| Page images per PDF | 2 (\`auto\`), 6 (\`high\`) | Each is over a thousand image tokens at the default size. |
| Frames per video | 3 (\`auto\`), 8 (\`high\`) | About 525 tokens each. |
| \`file_data\` | About 15 MiB | Base64 inside the 20 MiB request body. |
| OCR for inline PDFs | 8 pages synchronous, 40 streamed or background | OCR takes seconds a page. Past 8, a synchronous request is refused; past 40, a streamed or background request sends the remaining scanned pages with only their text layer, and no error — upload a long scan as a file. |
| \`input_audio\` | 300 seconds, WAV or MP3 | A clip for a question, not a recording; recordings are files. |
`.trim(),
  };
}

export const fileInputs: DocPage = fileInputsPage({ noTimeout: NO_TIMEOUT_LIVE });
